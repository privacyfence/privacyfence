"""Plugin approvals as the daemon plays them: a card a human decides, and a stored result.

An approval names a thing by ``(kind, subject_id, digest)``. A request for something already
approved answers ``approved`` with no card; otherwise a card waits until the test decides it. Only an
approved decision is stored, and ``revoke`` marks the stored record revoked.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

from .. import blocks as _blocks
from .._rpc import RpcError

_MAX_TITLE_CHARS = 120
_KIND_RE = re.compile(r"[a-z][a-z0-9_-]{0,40}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SUBJECT_ID_MAX_CHARS = 200
_PAGE_MAX_CHARS = 512
_AWAIT_MAX_MS = 300_000
_CARD_LIFETIME = timedelta(seconds=900)
_STATUS_OF_DECISION = {"approve": "approved", "deny": "denied", "expire": "expired"}
# Control characters (but not newline or tab) and the bidirectional controls, as the daemon strips.
_HIDDEN_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩‎‏؜]")

_Key = tuple[str, str, str, str]  # principal, kind, subject_id, digest


@dataclass
class Approval:
    """One approval card. ``status`` is ``pending`` until decided, then ``approved``, ``denied`` or ``expired``.

    ``revoked_at`` is set once ``host.revoke_approval`` took the approval back. ``frame_path`` is
    the path and query the card would frame when the request named a ``page``: pass it to
    ``host.get`` to see what the page shows next to the card.
    """

    approval_id: str
    kind: str
    subject_id: str
    digest: str
    title: str
    preview: list[dict]
    require_step_up: bool
    principal: str
    page: str | None
    expires_at: str
    status: str = "pending"
    decided_at: str | None = None
    revoked_at: str | None = None

    @property
    def frame_path(self) -> str | None:
        return None if self.page is None else f"{self.page}?pf_approval={self.approval_id}"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _normalize_page(raw: str) -> str | None:
    """The daemon's page-path rules; ``None`` means the path is rejected."""
    if len(raw) > 3 * _PAGE_MAX_CHARS:
        return None
    path = unquote(raw)
    if "\x00" in path or "\\" in path:
        return None
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > _PAGE_MAX_CHARS or "//" in path or ".." in path.split("/"):
        return None
    return path


def _bad(message: str) -> RpcError:
    return RpcError("invalid_params", message)


def _tuple(params: dict, where: str) -> tuple[str, str, str, str]:
    principal, kind = params.get("principal"), params.get("kind")
    subject_id, digest = params.get("subject_id"), params.get("digest")
    if not isinstance(principal, str) or not principal:
        raise _bad(f"{where}.principal must be a string")
    if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind):
        raise _bad(f"{where}.kind does not match {_KIND_RE.pattern}")
    if not isinstance(subject_id, str) or not 1 <= len(subject_id) <= _SUBJECT_ID_MAX_CHARS:
        raise _bad(f"{where}.subject_id must be 1 to {_SUBJECT_ID_MAX_CHARS} characters")
    if _HIDDEN_RE.search(subject_id):
        raise _bad(f"{where}.subject_id must not contain control or bidirectional characters")
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise _bad(f"{where}.digest must be sha256: followed by 64 lowercase hex digits")
    return principal, kind, subject_id, digest


class Approvals:
    """The test host's approval service. Nothing decides a card until the test does."""

    def __init__(
        self,
        plugin: str,
        principals: Callable[[], set[str]],
        pages_enabled: Callable[[], bool],
        audit: Callable[[dict], None],
    ) -> None:
        self._plugin = plugin
        self._principals = principals
        self._pages_enabled = pages_enabled
        self._audit = audit
        self._cards: dict[str, Approval] = {}
        self._decided: dict[str, asyncio.Event] = {}
        self._pending: dict[_Key, str] = {}
        self._records: list[Approval] = []   # approved decisions, oldest first; the stored approvals

    @property
    def cards(self) -> list[Approval]:
        return [Approval(**vars(c)) for c in self._cards.values()]

    def _record(self, kind: str, status: str) -> None:
        self._audit({
            "connector": f"plugin:{self._plugin}", "tool": "approval",
            "tool_name": f"{self._plugin} approval", "summary": f"{kind}; {status}",
            "decision": "plugin_approval",
        })

    def _find(self, key: _Key) -> Approval | None:
        found = None
        for record in self._records:
            if (record.principal, record.kind, record.subject_id, record.digest) == key:
                if found is None or record.decided_at >= found.decided_at:  # type: ignore[operator]
                    found = record
        return found

    # ------------------------------------------------------------------ requests from the plugin

    async def request(self, params: dict) -> dict:
        principal, kind, subject_id, digest = _tuple(params, "approval.request")
        title = params.get("title")
        if not isinstance(title, str) or not 1 <= len(title) <= _MAX_TITLE_CHARS:
            raise _bad(f"approval.request.title must be 1 to {_MAX_TITLE_CHARS} characters")
        step_up = params.get("require_step_up", True)
        if not isinstance(step_up, bool):
            raise _bad("approval.request.require_step_up must be a boolean")
        try:
            preview = _blocks.validate_blocks(params.get("preview"))
        except ValueError as exc:
            raise RpcError("invalid_blocks", str(exc)) from None
        page = params.get("page")
        if page is not None:
            if not isinstance(page, str) or not page.startswith("/") or len(page) > _PAGE_MAX_CHARS:
                raise _bad("approval.request.page must be a path that starts with \"/\"")
            if not self._pages_enabled():
                raise _bad("page needs pages: true in the manifest")
            normalized = _normalize_page(page)
            if normalized is None or "?" in normalized or "#" in normalized:
                raise _bad("page is not a valid path")
            page = normalized
        if principal not in self._principals():
            raise RpcError("unknown_principal", "no such principal")

        key = (principal, kind, subject_id, digest)
        record = self._find(key)
        if record is not None and record.revoked_at is None:
            return {"approval_id": record.approval_id, "status": "approved"}
        waiting = self._cards.get(self._pending.get(key, ""))
        if waiting is not None and waiting.status == "pending":
            return {"approval_id": waiting.approval_id, "status": "pending", "expires_at": waiting.expires_at}
        card = Approval(
            approval_id=f"approval-{uuid.uuid4().hex}", kind=kind, subject_id=subject_id, digest=digest,
            title=title, preview=preview, require_step_up=step_up, principal=principal, page=page,
            expires_at=_stamp(datetime.now(timezone.utc) + _CARD_LIFETIME),
        )
        self._cards[card.approval_id] = card
        self._decided[card.approval_id] = asyncio.Event()
        self._pending[key] = card.approval_id
        self._record(kind, "requested")
        return {"approval_id": card.approval_id, "status": "pending", "expires_at": card.expires_at}

    async def check(self, params: dict) -> dict:
        principal, kind, subject_id, digest = _tuple(params, "approval.check")
        if principal not in self._principals():
            raise RpcError("unknown_principal", "no such principal")
        record = self._find((principal, kind, subject_id, digest))
        if record is None:
            return {"status": "unknown"}
        status = "approved" if record.revoked_at is None else "revoked"
        return {"status": status, "approval_id": record.approval_id, "decided_at": record.decided_at}

    async def await_(self, params: dict) -> dict:
        approval_id = params.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            raise _bad("approval.await.approval_id must be a string")
        timeout_ms = params.get("timeout_ms")
        if timeout_ms is None:
            timeout_ms = _AWAIT_MAX_MS
        elif isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 0 <= timeout_ms <= _AWAIT_MAX_MS:
            raise _bad(f"approval.await.timeout_ms must be between 0 and {_AWAIT_MAX_MS}")
        card = self._cards.get(approval_id)
        if card is None:
            # An id that approval.request answered "approved" for at once never had a card here.
            for record in self._records:
                if record.approval_id == approval_id and record.revoked_at is None:
                    return {"status": "approved", "decided_at": record.decided_at}
            raise _bad("approval_id is not one of this plugin's approvals")
        try:
            await asyncio.wait_for(self._decided[approval_id].wait(), timeout_ms / 1000)
        except asyncio.TimeoutError:
            raise RpcError("timeout", "the approval is still pending", retryable=True) from None
        return {"status": card.status, "decided_at": card.decided_at}

    # ------------------------------------------------------------------ the test's side

    def decide(self, approval_id: str, decision: str) -> Approval:
        """Answer a card the way the human would: ``approve``, ``deny`` or ``expire``."""
        if decision not in _STATUS_OF_DECISION:
            raise ValueError("decision must be 'approve', 'deny' or 'expire'")
        card = self._cards.get(approval_id)
        if card is None:
            raise KeyError(f"no approval with the id {approval_id!r}")
        if card.status != "pending":
            raise ValueError(f"approval {approval_id} is already {card.status}")
        card.status = _STATUS_OF_DECISION[decision]
        card.decided_at = _stamp(datetime.now(timezone.utc))
        if card.status == "approved":
            # Stored before the waiting plugin wakes, so check already says approved when await does.
            self._records.append(Approval(**vars(card)))
        self._pending.pop((card.principal, card.kind, card.subject_id, card.digest), None)
        self._decided[approval_id].set()
        self._record(card.kind, card.status)
        return Approval(**vars(card))

    def revoke(self, approval_id: str) -> Approval:
        """Mark the stored approval revoked, as ``PluginHost.revoke_approval`` does.

        Returns the record. One that is already revoked keeps its first ``revoked_at``.
        """
        stamp = _stamp(datetime.now(timezone.utc))
        for record in self._records:
            if record.approval_id == approval_id:
                if record.revoked_at is None:
                    record.revoked_at = stamp
                    card = self._cards.get(approval_id)
                    if card is not None:
                        card.revoked_at = stamp
                return Approval(**vars(record))
        raise KeyError(f"no approved approval with the id {approval_id!r}")
