"""Plugin approvals that persist: ``approval.request``, ``approval.check`` and ``approval.await``
(ADR 0127).

A plugin asks a human to approve a *thing* -- a piece of code, a template, a mapping -- named by
``(kind, subject_id, digest)``. The request becomes a confirm card in the approvals registry that
shows PrivacyFence's own fields (plugin, kind, subject, digest), the plugin's preview blocks and,
when the plugin asks for one, its own page in a sandboxed frame below those fields.

An approval binds to ``(plugin, principal, kind, subject_id, digest)`` and is kept in
``plugin-approvals.json`` until a human revokes it in Settings. Only approved decisions are kept:
a denied or expired request leaves nothing behind, and a file that cannot be read reads as empty,
so ``approval.check`` then answers ``unknown`` (fail closed).

The same properties as a plugin confirmation (ADR 0122) hold by construction:

- **No rule can accept it.** The card is ``kind="confirm"`` with no operation key, so
  ``PendingApprovalRegistry.reevaluate_all`` never looks at it. Only a human answer finalizes it.
- **Step-up stays.** ``require_step_up`` (on unless the plugin turns it off) marks the card
  ``sensitive``.
- **Nobody approves for an absent human.** While any MCP session is unattended a request that
  would show a card is refused.

``request`` returns at once. A finalizer, run in the injected executor, waits for the human's
answer. On an approval it stores the record *before* it finalizes the card, so by the time
``approval.await`` reads ``approved``, ``approval.check`` does too.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from privacyfence.approvals import CONFIRM_RESULTS, PendingApprovalRegistry
from privacyfence.dialog_window_html import build_plugin_approval_html
from privacyfence.plugins import pages
from privacyfence.plugins.blocks import clean_line, to_card_blocks, validate_blocks
from privacyfence.plugins.constants import (
    CONFIRM_AWAIT_MAX_MS,
    MAX_PENDING_CONFIRMS,
    MAX_PENDING_CONFIRMS_PER_PLUGIN,
    PENDING_CARD_SECONDS,
)
from privacyfence.plugins.manifest import Manifest
from privacyfence.plugins.protocol import (
    ApprovalAwaitParams,
    ApprovalCheckParams,
    ApprovalRequestParams,
    RpcError,
)
from privacyfence.principal import LOCAL_PRINCIPAL_ID
from privacyfence.secure_files import atomic_write_json

logger = logging.getLogger(__name__)

STORE_FILENAME = "plugin-approvals.json"
STORE_VERSION = 1
POLL_SECONDS = 0.5

# registry.await_status's vocabulary, mapped to approval.await's. "unknown" means the registry no
# longer holds the card, which for the plugin is the same as a card nobody answered.
_AWAIT_STATUS = {"approved": "approved", "denied": "denied", "expired": "expired", "unknown": "expired"}

_RECORD_KEYS = ("approval_id", "plugin", "principal", "kind", "subject_id", "digest", "title", "decided_at")

_Key = tuple[str, str, str, str, str]      # plugin, principal, kind, subject_id, digest


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now() -> str:
    return _rfc3339(time.time())


class _CorruptStoreError(ValueError):
    pass


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    plugin: str
    principal: str
    kind: str
    subject_id: str
    digest: str
    title: str
    decided_at: str
    revoked_at: str | None

    @property
    def key(self) -> _Key:
        return (self.plugin, self.principal, self.kind, self.subject_id, self.digest)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: Any) -> ApprovalRecord:
        if not isinstance(raw, dict):
            raise _CorruptStoreError("an approval record is not an object")
        if not all(isinstance(raw.get(k), str) for k in _RECORD_KEYS):
            raise _CorruptStoreError("an approval record has a missing or non-string field")
        revoked_at = raw.get("revoked_at")
        if "revoked_at" not in raw or not (revoked_at is None or isinstance(revoked_at, str)):
            raise _CorruptStoreError("an approval record has a malformed revoked_at")
        return cls(**{k: raw[k] for k in _RECORD_KEYS}, revoked_at=revoked_at)


class ApprovalStore:
    """Reads and writes ``plugin-approvals.json``. Every write rereads the file first, under a
    lock, and writes it atomically with mode 0600."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def find(self, plugin: str, principal: str, kind: str, subject_id: str, digest: str) -> ApprovalRecord | None:
        """The latest record for the tuple by ``decided_at``, revoked or not; a later record wins a
        tie."""
        key = (plugin, principal, kind, subject_id, digest)
        latest: ApprovalRecord | None = None
        for record in self._load():
            if record.key == key and (latest is None or record.decided_at >= latest.decided_at):
                latest = record
        return latest

    def add(self, record: ApprovalRecord) -> None:
        with self._lock:
            records = self._load()
            records.append(record)
            self._save(records)

    def revoke(self, plugin: str, approval_id: str, *, now: str) -> ApprovalRecord | None:
        """Mark the record revoked and return it; ``None`` when this plugin has no such record. A
        record already revoked keeps its first ``revoked_at``."""
        with self._lock:
            records = self._load()
            for i, record in enumerate(records):
                if record.plugin == plugin and record.approval_id == approval_id:
                    if record.revoked_at is not None:
                        return record
                    records[i] = revoked = ApprovalRecord(**{**record.to_json(), "revoked_at": now})
                    self._save(records)
                    return revoked
        return None

    def discard(self, approval_id: str) -> bool:
        """Remove the record with this id, whatever its plugin. Only the service uses it, to take
        back a record whose card the registry had already finalized some other way."""
        with self._lock:
            records = self._load()
            kept = [r for r in records if r.approval_id != approval_id]
            if len(kept) == len(records):
                return False
            self._save(kept)
            return True

    def for_plugin(self, plugin: str) -> list[ApprovalRecord]:
        """Every record of the plugin, newest first."""
        records = [r for r in self._load() if r.plugin == plugin]
        records.reverse()   # a later record wins a decided_at tie, as in find
        records.sort(key=lambda r: r.decided_at, reverse=True)
        return records

    def forget_plugin(self, plugin: str) -> int:
        """Remove every record of the plugin; returns how many were removed."""
        with self._lock:
            records = self._load()
            kept = [r for r in records if r.plugin != plugin]
            removed = len(records) - len(kept)
            if removed:
                self._save(kept)
            return removed

    def _load(self) -> list[ApprovalRecord]:
        """Every record. A missing file is empty; a corrupt one is logged and read as empty."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Could not read %s, so no plugin approval holds: %s", self.path, exc)
            return []
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
                raise _CorruptStoreError(f"expected an object with version {STORE_VERSION}")
            approvals = data.get("approvals")
            if not isinstance(approvals, list):
                raise _CorruptStoreError("approvals must be a list")
            return [ApprovalRecord.from_json(raw) for raw in approvals]
        except ValueError as exc:
            logger.warning("%s is corrupt, so no plugin approval holds: %s", self.path, exc)
            return []

    def _save(self, records: list[ApprovalRecord]) -> None:
        payload = {"version": STORE_VERSION, "approvals": [r.to_json() for r in records]}
        atomic_write_json(self.path, payload, mode=0o600, indent=2)


@dataclass(frozen=True)
class _Pending:
    key: _Key
    title: str
    frame_src: str
    registry: PendingApprovalRegistry
    epoch: int

    @property
    def plugin(self) -> str:
        return self.key[0]

    @property
    def kind(self) -> str:
        return self.key[2]


class ApprovalService:
    """Handles one daemon's plugin approvals. Every plugin shares one service; an approval id is
    only ever answered to the plugin that asked for it."""

    def __init__(
        self,
        *,
        store: ApprovalStore,
        registry_provider: Callable[[], PendingApprovalRegistry],
        unattended_active: Callable[[], bool],
        executor: Executor,
        audit: Callable[[str, str, str], None],
    ) -> None:
        self._store = store
        self._registry_provider = registry_provider
        self._unattended_active = unattended_active
        self._executor = executor
        self._audit_fn = audit
        self._lock = threading.Lock()
        self._owned: dict[str, _Pending] = {}
        self._epochs: dict[str, int] = {}          # plugin -> times its cards were expired
        self._pending: dict[_Key, str] = {}       # the tuple of a card still waiting -> its id
        self._finished: dict[str, float] = {}      # approval id -> monotonic time it finished
        self._active_total = 0
        self._active_by_plugin: dict[str, int] = {}
        self.retain_finished_seconds = PENDING_CARD_SECONDS
        self._finalizers: set[asyncio.Future] = set()
        self.poll_seconds = POLL_SECONDS

    async def request(
        self, plugin: str, display_name: str, manifest: Manifest, params: dict, *, introspecting: bool,
    ) -> dict:
        if introspecting:
            raise RpcError("introspection_only", "approval.request is not available while introspecting")
        parsed = ApprovalRequestParams.from_wire(params, validate_blocks=validate_blocks)
        page = ""
        if parsed.page is not None:
            if not manifest.pages:
                raise RpcError("invalid_params", "page needs pages: true in the manifest")
            normalized = pages.normalize_path(parsed.page)
            # Checked after decoding, so an encoded %3F or %23 cannot hide pf_approval in a query
            # or a fragment of the frame's address.
            if normalized is None or "?" in normalized or "#" in normalized:
                raise RpcError("invalid_params", "page is not a valid path")
            page = normalized
        if parsed.principal != LOCAL_PRINCIPAL_ID:
            raise RpcError("unknown_principal", "no such principal")
        # Cleaned as one string, with the same stripping every block string gets, so neither the
        # display name nor the title can reorder the text a human reads on the card.
        if not clean_line(parsed.title).strip():
            raise RpcError("invalid_params", "approval.request.title is empty after cleaning")
        key: _Key = (plugin, parsed.principal, parsed.kind, parsed.subject_id, parsed.digest)

        record = self._store.find(*key)
        if record is not None and record.revoked_at is None:
            return {"approval_id": record.approval_id, "status": "approved"}
        with self._lock:
            waiting_id = self._pending.get(key)
            waiting = self._owned.get(waiting_id) if waiting_id is not None else None
        if waiting_id is not None and waiting is not None:
            card = waiting.registry.get(waiting_id)
            if card is not None and not card.is_finalized():
                return {"approval_id": waiting_id, "status": "pending", "expires_at": _rfc3339(card.expires_at)}
        if self._unattended_active():
            raise RpcError(
                "confirmation_refused", "an unattended session is active", extra={"reason": "unattended_session"},
            )

        self._reserve(plugin)
        registry = None
        card = None
        try:
            registry = self._registry_provider()
            card = registry.register_confirm(sensitive=parsed.require_step_up, notify=True)
            card.frame_src = f"/plugins/{plugin}{page}?pf_approval={card.id}" if page else ""
            registry.set_html(card.id, build_plugin_approval_html(
                title=clean_line(f"{display_name}: {parsed.title}"),
                fields=[
                    ("Plugin", clean_line(f"{display_name} ({plugin})")),
                    ("Kind", parsed.kind),
                    ("Subject", parsed.subject_id),
                    ("Digest", parsed.digest),
                ],
                body_blocks=to_card_blocks(parsed.preview),
                frame_src=card.frame_src,
                frame_title=clean_line(f"Page from {display_name}"),
            ))
            with self._lock:
                self._owned[card.id] = _Pending(
                    key, clean_line(parsed.title), card.frame_src, registry, self._epochs.get(plugin, 0),
                )
                self._pending[key] = card.id
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
            logger.warning("A plugin approval finalizer could not be scheduled", exc_info=True)
            registry.finalize(card.id, "deny")
            self._audit(plugin, parsed.kind, "denied")
            self._finish(card.id, plugin)
            raise
        self._finalizers.add(finalizer)
        finalizer.add_done_callback(lambda f, i=card.id, p=plugin: self._finalizer_done(f, i, p))
        return {"approval_id": card.id, "status": "pending", "expires_at": _rfc3339(card.expires_at)}

    async def check(self, plugin: str, params: dict) -> dict:
        parsed = ApprovalCheckParams.from_wire(params)
        if parsed.principal != LOCAL_PRINCIPAL_ID:
            raise RpcError("unknown_principal", "no such principal")
        record = self._store.find(plugin, parsed.principal, parsed.kind, parsed.subject_id, parsed.digest)
        if record is None:
            return {"status": "unknown"}
        status = "approved" if record.revoked_at is None else "revoked"
        return {"status": status, "approval_id": record.approval_id, "decided_at": record.decided_at}

    async def await_(self, plugin: str, params: dict) -> dict:
        parsed = ApprovalAwaitParams.from_wire(params)
        approval_id = parsed.approval_id
        timeout_ms = CONFIRM_AWAIT_MAX_MS if parsed.timeout_ms is None else parsed.timeout_ms
        with self._lock:
            owned = self._owned.get(approval_id)
        if owned is None or owned.plugin != plugin:
            # An id approval.request answered "approved" for at once never had a card here.
            for record in self._store.for_plugin(plugin):
                if record.approval_id == approval_id and record.revoked_at is None:
                    return {"status": "approved", "decided_at": record.decided_at}
            raise RpcError("invalid_params", "approval_id is not one of this plugin's approvals")
        registry = owned.registry
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while True:
            status = registry.await_status(approval_id)
            if status != "pending":
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RpcError("timeout", "the approval is still pending", retryable=True)
            await asyncio.sleep(min(self.poll_seconds, remaining))
        result: dict[str, Any] = {"status": _AWAIT_STATUS.get(status, "expired")}
        card = registry.get(approval_id)
        if card is not None and card.decided_at is not None:
            result["decided_at"] = _rfc3339(card.decided_at)
        return result

    def embed_allowed(self, plugin: str, approval_id: str, frame_path: str) -> bool:
        """True only while ``approval_id`` is this plugin's card, still waiting for a human, and
        its frame shows exactly ``frame_path``: the normalized page path, as the page route sees
        it."""
        with self._lock:
            owned = self._owned.get(approval_id)
            waiting = owned is not None and self._pending.get(owned.key) == approval_id
        if owned is None or not waiting or owned.plugin != plugin or not owned.frame_src:
            return False
        card = owned.registry.get(approval_id)
        if card is None or card.is_finalized():
            return False
        return owned.frame_src.partition("?")[0] == f"/plugins/{plugin}{frame_path}"

    async def close(self, timeout: float = 5.0) -> None:
        """Expire every approval still waiting for a human and wait for its finalizer, so the
        outcome is audited before the audit log closes and no finalizer outlives the host."""
        with self._lock:
            waiting = [(i, o.registry) for i, o in self._owned.items() if i not in self._finished]
        for approval_id, registry in waiting:
            registry.finalize(approval_id, "expired")
        finalizers = list(self._finalizers)
        if finalizers:
            await asyncio.wait(finalizers, timeout=timeout)

    def expire_plugin(self, plugin: str) -> int:
        """Expire every approval of ``plugin`` still waiting for a human; returns how many this
        call expired. A card answered "confirm" after this is not stored either (see ``_decide``)."""
        with self._lock:
            self._epochs[plugin] = self._epochs.get(plugin, 0) + 1
            waiting = [
                (i, o.registry) for i, o in self._owned.items()
                if o.plugin == plugin and i not in self._finished
            ]
        return sum(1 for i, registry in waiting if registry.finalize(i, "expired"))

    def _finalize_when_answered(self, approval_id: str) -> None:
        with self._lock:
            owned = self._owned[approval_id]
        try:
            self._decide(approval_id, owned)
        finally:
            with self._lock:
                if self._pending.get(owned.key) == approval_id:
                    del self._pending[owned.key]

    def _decide(self, approval_id: str, owned: _Pending) -> None:
        registry = owned.registry
        card = registry.get(approval_id)
        if card is None:
            return
        answered = card.event.wait(timeout=max(0.0, card.expires_at - time.time()))
        if not answered:
            registry.finalize(approval_id, "expired")
        elif card.result == CONFIRM_RESULTS[0] and not card.is_finalized():  # "confirm"
            with self._lock:
                current = self._epochs.get(owned.plugin, 0) == owned.epoch
            if not current:
                registry.finalize(approval_id, "expired")
                self._audit(owned.plugin, owned.kind, "expired")
                return
            plugin, principal, kind, subject_id, digest = owned.key
            record = ApprovalRecord(
                approval_id=approval_id, plugin=plugin, principal=principal, kind=kind,
                subject_id=subject_id, digest=digest, title=owned.title, decided_at=_now(), revoked_at=None,
            )
            try:
                self._store.add(record)
            except Exception:
                logger.warning("A plugin approval could not be stored, so it is denied", exc_info=True)
                registry.finalize(approval_id, "deny")
            else:
                if not registry.finalize(approval_id, "accept") and registry.await_status(approval_id) != "approved":
                    # The registry finalized the card some other way between the two steps.
                    self._store.discard(approval_id)
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
                    "confirmation_refused", "too many approvals are pending",
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
        """Give the slot back, forget the pending tuple, and drop ownership records that finished
        long enough ago for an ``approval.await`` to have collected the answer."""
        self._release(plugin)
        now = time.monotonic()
        with self._lock:
            owned = self._owned.get(approval_id)
            if owned is not None and self._pending.get(owned.key) == approval_id:
                del self._pending[owned.key]
            self._finished[approval_id] = now
            for done, at in list(self._finished.items()):
                if now - at >= self.retain_finished_seconds:
                    del self._finished[done]
                    self._owned.pop(done, None)

    def _finalizer_done(self, future: asyncio.Future | Future, approval_id: str, plugin: str) -> None:
        self._finalizers.discard(future)  # type: ignore[arg-type]
        self._finish(approval_id, plugin)
        if not future.cancelled() and future.exception() is not None:
            logger.warning("A plugin approval finalizer failed", exc_info=future.exception())

    def _audit(self, plugin: str, kind: str, status: str) -> None:
        try:
            self._audit_fn(plugin, kind, status)
        except Exception as exc:
            logger.warning("Audit log write failed for a plugin approval: %s", type(exc).__name__)
