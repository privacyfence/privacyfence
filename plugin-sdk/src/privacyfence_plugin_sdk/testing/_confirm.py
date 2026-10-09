"""Confirmations as the daemon plays them: a card a human decides, and a plugin that awaits it."""
from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import blocks as _blocks
from .._rpc import RpcError

_MAX_TITLE_CHARS = 120
_KIND_RE = re.compile(r"[a-z][a-z0-9_]{0,30}")
_AWAIT_MAX_MS = 300_000
_CARD_LIFETIME = timedelta(seconds=900)  # an approval card's pending lifetime
_STATUS_OF_DECISION = {"approve": "approved", "deny": "denied", "expire": "expired"}


@dataclass
class Confirmation:
    """One confirmation card. ``status`` is ``pending`` until decided, then ``approved``, ``denied`` or ``expired``."""

    approval_id: str
    kind: str
    title: str
    preview: list[dict]
    require_step_up: bool
    principal: str
    expires_at: str
    status: str = "pending"
    decided_at: str | None = None


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class Confirmations:
    """The test host's confirmation service. Nothing decides a card until the test does."""

    def __init__(self, plugin: str, principals: Callable[[], set[str]], audit: Callable[[dict], None]) -> None:
        self._plugin = plugin
        self._principals = principals
        self._audit = audit
        self._cards: dict[str, Confirmation] = {}
        self._decided: dict[str, asyncio.Event] = {}

    @property
    def cards(self) -> list[Confirmation]:
        return [Confirmation(**vars(c)) for c in self._cards.values()]

    def _record(self, card: Confirmation, summary_status: str) -> None:
        self._audit({
            "connector": f"plugin:{self._plugin}", "tool": "plugin_confirm",
            "tool_name": f"{self._plugin} confirmation", "summary": f"{card.kind}; {summary_status}",
            "decision": "plugin_confirm",
        })

    async def request(self, params: dict) -> dict:
        kind, title = params.get("kind"), params.get("title")
        if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind):
            raise RpcError("invalid_params", "kind must match [a-z][a-z0-9_]{0,30}")
        if not isinstance(title, str) or not 1 <= len(title) <= _MAX_TITLE_CHARS:
            raise RpcError("invalid_params", f"title must be 1 to {_MAX_TITLE_CHARS} characters")
        step_up = params.get("require_step_up", True)
        if not isinstance(step_up, bool):
            raise RpcError("invalid_params", "require_step_up must be a boolean")
        try:
            preview = _blocks.validate_blocks(params.get("preview"))
        except ValueError as exc:
            raise RpcError("invalid_blocks", str(exc)) from None
        principal = params.get("principal")
        if not isinstance(principal, str) or principal not in self._principals():
            raise RpcError("unknown_principal", "no such principal")
        card = Confirmation(
            approval_id=f"confirm-{uuid.uuid4().hex}", kind=kind, title=title, preview=preview,
            require_step_up=step_up, principal=principal,
            expires_at=_stamp(datetime.now(timezone.utc) + _CARD_LIFETIME),
        )
        self._cards[card.approval_id] = card
        self._decided[card.approval_id] = asyncio.Event()
        self._record(card, "requested")
        return {"approval_id": card.approval_id, "expires_at": card.expires_at}

    async def await_(self, params: dict) -> dict:
        approval_id = params.get("approval_id")
        card = self._cards.get(approval_id) if isinstance(approval_id, str) else None
        if card is None:
            raise RpcError("invalid_params", "no confirmation with this id")
        timeout_ms = params.get("timeout_ms", _AWAIT_MAX_MS)
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 0 <= timeout_ms <= _AWAIT_MAX_MS:
            raise RpcError("invalid_params", f"timeout_ms must be 0 to {_AWAIT_MAX_MS}")
        try:
            await asyncio.wait_for(self._decided[card.approval_id].wait(), timeout_ms / 1000)
        except asyncio.TimeoutError:
            raise RpcError("timeout", "the confirmation is still pending") from None
        return {"status": card.status, "decided_at": card.decided_at}

    def decide(self, approval_id: str, decision: str) -> Confirmation:
        """Answer a card the way the human would: ``approve``, ``deny`` or ``expire``. There is no deny note."""
        if decision not in _STATUS_OF_DECISION:
            raise ValueError("decision must be 'approve', 'deny' or 'expire'")
        card: Any = self._cards.get(approval_id)
        if card is None:
            raise KeyError(f"no confirmation with the id {approval_id!r}")
        if card.status != "pending":
            raise ValueError(f"confirmation {approval_id} is already {card.status}")
        card.status = _STATUS_OF_DECISION[decision]
        card.decided_at = _stamp(datetime.now(timezone.utc))
        self._decided[approval_id].set()
        self._record(card, card.status)
        return Confirmation(**vars(card))
