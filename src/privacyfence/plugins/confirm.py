"""Plugin confirmations: ``confirm.request`` and ``confirm.await`` (ADR 0122).

A plugin may ask a human to confirm something. The request becomes an ordinary confirm card in the
approvals registry, shown on the approvals page like any other, with the plugin's display name in
its title and the plugin's preview blocks rendered by the card's own escaping renderer.

Three properties hold by construction:

- **No rule can accept it.** The card is ``kind="confirm"`` with no operation key, so
  ``PendingApprovalRegistry.reevaluate_all`` never looks at it. Only a human answer finalizes it.
- **Step-up stays.** ``require_step_up`` (on unless the plugin turns it off) marks the card
  ``sensitive``, which holds the decide route to a human session and, where required, a passkey.
- **Nobody confirms for an absent human.** While any MCP session is unattended the request is
  refused. The request comes from the plugin process, not from inside one MCP call, so the daemon
  cannot tell which session caused it, and refusing whenever any session is unattended fails
  closed.

``request`` returns at once. A finalizer, run in the injected executor, waits for the human's
answer and calls ``registry.finalize``, which is what ``registry.await_status`` (and so
``privacyfence_await_approval`` and ``confirm.await``) reads. The only other finalize is the
host's shutdown, which expires the cards nobody answered. There is no deny note: a confirm dialog
posts only ``confirm`` or ``cancel``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from privacyfence.approvals import CONFIRM_RESULTS, PendingApprovalRegistry
from privacyfence.dialog_window_html import build_confirmation_html
from privacyfence.plugins.blocks import clean_line, to_card_blocks, validate_blocks
from privacyfence.plugins.constants import (
    CONFIRM_AWAIT_MAX_MS,
    MAX_PENDING_CONFIRMS,
    MAX_PENDING_CONFIRMS_PER_PLUGIN,
    PENDING_CARD_SECONDS,
)
from privacyfence.plugins.protocol import ConfirmRequestParams, RpcError
from privacyfence.principal import LOCAL_PRINCIPAL_ID

logger = logging.getLogger(__name__)

KIND_RE = re.compile(r"[a-z][a-z0-9_]{0,30}")     # always .fullmatch()
POLL_SECONDS = 0.5

# registry.await_status's vocabulary, mapped to confirm.await's. "unknown" means the registry no
# longer holds the card, which for the plugin is the same as a card nobody answered.
_AWAIT_STATUS = {"approved": "approved", "denied": "denied", "expired": "expired", "unknown": "expired"}


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class _Owned:
    plugin: str
    kind: str
    registry: PendingApprovalRegistry


class ConfirmationService:
    """Handles one daemon's plugin confirmations. Every plugin shares one service; an approval id
    is only ever answered to the plugin that asked for it."""

    def __init__(
        self,
        *,
        registry_provider: Callable[[], PendingApprovalRegistry],
        unattended_active: Callable[[], bool],
        executor: Executor,
        audit: Callable[[str, str, str], None],
    ) -> None:
        self._registry_provider = registry_provider
        self._unattended_active = unattended_active
        self._executor = executor
        self._audit_fn = audit
        self._lock = threading.Lock()
        self._owned: dict[str, _Owned] = {}
        self._finished: dict[str, float] = {}      # approval id -> monotonic time it finished
        self._active_total = 0
        self._active_by_plugin: dict[str, int] = {}
        self.retain_finished_seconds = PENDING_CARD_SECONDS
        self._finalizers: set[asyncio.Future] = set()
        self.poll_seconds = POLL_SECONDS

    async def request(self, plugin: str, display_name: str, params: dict, *, introspecting: bool) -> dict:
        if introspecting:
            raise RpcError("introspection_only", "confirm.request is not available while introspecting")
        parsed = ConfirmRequestParams.from_wire(params, validate_blocks=validate_blocks)
        if not KIND_RE.fullmatch(parsed.kind):
            raise RpcError("invalid_params", "confirm.request.kind must match [a-z][a-z0-9_]{0,30}")
        if parsed.principal != LOCAL_PRINCIPAL_ID:
            raise RpcError("unknown_principal", "no such principal")
        if self._unattended_active():
            raise RpcError(
                "confirmation_refused", "an unattended session is active", extra={"reason": "unattended_session"},
            )
        # Cleaned as one string, with the same stripping every block string gets, so neither the
        # display name nor the title can reorder the text a human reads on the card.
        title = clean_line(f"{display_name}: {parsed.title}")
        if not clean_line(parsed.title).strip():
            raise RpcError("invalid_params", "confirm.request.title is empty after cleaning")
        self._reserve(plugin)
        registry = None
        card = None
        try:
            registry = self._registry_provider()
            card = registry.register_confirm(sensitive=parsed.require_step_up, notify=True)
            registry.set_html(card.id, build_confirmation_html(
                title=title,
                message_lines=[],
                cancel_label="Deny",
                confirm_label="Approve",
                body_blocks=to_card_blocks(parsed.preview),
            ))
            with self._lock:
                self._owned[card.id] = _Owned(plugin, parsed.kind, registry)
        except BaseException:
            if registry is not None and card is not None:
                registry.finalize(card.id, "deny")
            self._release(plugin)
            raise
        self._audit(plugin, parsed.kind, "requested")
        try:
            loop = asyncio.get_running_loop()
            finalizer = loop.run_in_executor(self._executor, self._finalize_when_answered, card.id)
        except Exception:
            logger.warning("A plugin confirmation finalizer could not be scheduled", exc_info=True)
            registry.finalize(card.id, "deny")
            self._audit(plugin, parsed.kind, "denied")
            self._finish(card.id, plugin)
            raise
        self._finalizers.add(finalizer)
        finalizer.add_done_callback(lambda f, i=card.id, p=plugin: self._finalizer_done(f, i, p))
        return {"approval_id": card.id, "expires_at": _rfc3339(card.expires_at)}

    async def await_(self, plugin: str, params: dict) -> dict:
        approval_id, timeout_ms = _await_params(params)
        with self._lock:
            owned = self._owned.get(approval_id)
        if owned is None or owned.plugin != plugin:
            raise RpcError("invalid_params", "approval_id is not one of this plugin's confirmations")
        registry = owned.registry
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while True:
            status = registry.await_status(approval_id)
            if status != "pending":
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RpcError("timeout", "the confirmation is still pending", retryable=True)
            await asyncio.sleep(min(self.poll_seconds, remaining))
        result: dict[str, Any] = {"status": _AWAIT_STATUS.get(status, "expired")}
        card = registry.get(approval_id)
        if card is not None and card.decided_at is not None:
            result["decided_at"] = _rfc3339(card.decided_at)
        return result

    async def close(self, timeout: float = 5.0) -> None:
        """Expire every confirmation still waiting for a human and wait for its finalizer, so the
        outcome is audited before the audit log closes and no finalizer outlives the host."""
        with self._lock:
            waiting = [(i, o.registry) for i, o in self._owned.items() if i not in self._finished]
        for approval_id, registry in waiting:
            registry.finalize(approval_id, "expired")
        finalizers = list(self._finalizers)
        if finalizers:
            await asyncio.wait(finalizers, timeout=timeout)

    def owns(self, plugin: str, approval_id: str) -> bool:
        """True when ``approval_id`` is a confirmation card this service opened for ``plugin``."""
        with self._lock:
            owned = self._owned.get(approval_id)
        return owned is not None and owned.plugin == plugin

    def expire_plugin(self, plugin: str) -> int:
        """Expire every confirmation of ``plugin`` still waiting for a human; returns how many
        this call expired. The finalizer threads wake and audit the outcome."""
        with self._lock:
            waiting = [
                (i, o.registry) for i, o in self._owned.items()
                if o.plugin == plugin and i not in self._finished
            ]
        return sum(1 for i, registry in waiting if registry.finalize(i, "expired"))

    def _finalize_when_answered(self, approval_id: str) -> None:
        with self._lock:
            owned = self._owned[approval_id]
        registry = owned.registry
        card = registry.get(approval_id)
        if card is None:
            return
        answered = card.event.wait(timeout=max(0.0, card.expires_at - time.time()))
        if not answered:
            registry.finalize(approval_id, "expired")
        elif card.result == CONFIRM_RESULTS[0]:  # "confirm"
            registry.finalize(approval_id, "accept")
        else:
            # Any other answer, and a card the registry already finalized itself (its expiry
            # sweep sets the event with no result): finalize is first-decision-wins, so the
            # registry's own decision stands.
            registry.finalize(approval_id, "deny")
        status = _AWAIT_STATUS.get(registry.await_status(approval_id), "expired")
        self._audit(owned.plugin, owned.kind, status)

    def _reserve(self, plugin: str) -> None:
        """Take one finalizer slot, or refuse; called before anything is registered."""
        with self._lock:
            if (self._active_total >= MAX_PENDING_CONFIRMS
                    or self._active_by_plugin.get(plugin, 0) >= MAX_PENDING_CONFIRMS_PER_PLUGIN):
                raise RpcError(
                    "confirmation_refused", "too many confirmations are pending",
                    extra={"reason": "too_many_pending"},
                )
            self._active_total += 1
            self._active_by_plugin[plugin] = self._active_by_plugin.get(plugin, 0) + 1

    def _release(self, plugin: str) -> None:
        with self._lock:
            self._active_total -= 1
            left = self._active_by_plugin.get(plugin, 1) - 1
            if left > 0:
                self._active_by_plugin[plugin] = left
            else:
                self._active_by_plugin.pop(plugin, None)

    def _finish(self, approval_id: str, plugin: str) -> None:
        """Give the slot back and drop ownership records that finished long enough ago for a
        ``confirm.await`` to have collected the answer."""
        self._release(plugin)
        now = time.monotonic()
        with self._lock:
            self._finished[approval_id] = now
            for done, at in list(self._finished.items()):
                if now - at >= self.retain_finished_seconds:
                    del self._finished[done]
                    self._owned.pop(done, None)

    def _finalizer_done(self, future: asyncio.Future | Future, approval_id: str, plugin: str) -> None:
        self._finalizers.discard(future)  # type: ignore[arg-type]
        self._finish(approval_id, plugin)
        if not future.cancelled() and future.exception() is not None:
            logger.warning("A plugin confirmation finalizer failed", exc_info=future.exception())

    def _audit(self, plugin: str, kind: str, status: str) -> None:
        try:
            self._audit_fn(plugin, kind, status)
        except Exception as exc:
            logger.warning("Audit log write failed for a plugin confirmation: %s", type(exc).__name__)


def _await_params(params: Any) -> tuple[str, int]:
    if not isinstance(params, dict):
        raise RpcError("invalid_params", "confirm.await params must be an object")
    approval_id = params.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        raise RpcError("invalid_params", "confirm.await.approval_id must be a non-empty string")
    timeout_ms = params.get("timeout_ms", CONFIRM_AWAIT_MAX_MS)
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
        raise RpcError("invalid_params", "confirm.await.timeout_ms must be an integer")
    if not 0 <= timeout_ms <= CONFIRM_AWAIT_MAX_MS:
        raise RpcError("invalid_params", f"confirm.await.timeout_ms must be between 0 and {CONFIRM_AWAIT_MAX_MS}")
    return approval_id, timeout_ms
