"""Delivery records for scoped tokens put into an agent runtime, and the two-phase deliver flow.

A delivered token is an ordinary scoped token whose secret PrivacyFence also writes into a runtime
(n8n first) as a credential (ADR 0278). Every credential has a record in
``agent_runtime_credentials.json`` *before* the token exists, so a token that works in the runtime
is never without a record a cleanup can find (ADR 0281). A record never holds the secret, and
nothing here logs, audits or returns it. This module has no web imports.
"""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from . import agent_runtimes, audit_log, paths, secure_files
from .n8n_api import N8nApi, N8nApiClientError
from .principal import Principal, principal_scope
from .scoped_token_proposals import ProposalRegistry, TokenProposal
from .scoped_tokens import ScopedToken, ScopedTokenError, ScopedTokenPolicy, ScopedTokenStore, new_token_id

logger = logging.getLogger(__name__)

CREATING, DELIVERED, ABANDONED = "creating", "delivered", "abandoned"
BOOT_ID = secrets.token_hex(8)  # fixed for the process: a "creating" record from another boot is a crash's
STORE_FILE_NAME = "agent_runtime_credentials.json"
_TOOL_NAME = "privacyfence_runtime_credential"

DELIVERY_FAILED = (
    "PrivacyFence could not create the credential in n8n, so it revoked the token. "
    "Try again, or ask your administrator to check the n8n connection."
)
DELIVERY_FAILED_NOT_REVOKED = (
    "PrivacyFence could not create the credential in n8n and could not revoke "
    "the token. Revoke it now in Settings → Scoped tokens."
)
RECORD_FAILED = (
    "PrivacyFence could not write its delivery record, so it created nothing. "
    "Ask your administrator to check the server's log."
)
NODE_NOT_INSTALLED = (
    "n8n does not know the PrivacyFence credential type. "
    "Ask the n8n administrator to install the community node n8n-nodes-privacyfence, then try again."
)
_UNEXPECTED_ERROR = "an unexpected error occurred"


@dataclass(frozen=True)
class RuntimeCredential:
    principal_id: str
    token_id: str
    proposal_id: str
    runtime: str
    endpoint: tuple[str, str, int]
    project_id: str
    credential_id: str
    credential_name: str
    workflow_id: str
    mcp_url: str
    state: str
    created_at: float
    boot_id: str
    credential_type: str = "httpBearerAuth"


def credential_data(credential_type: str, mcp_url: str, secret: str) -> dict[str, str]:
    """The ``data`` of the n8n credential. Holds the secret: never log or audit it."""
    if credential_type == "privacyFenceApi":
        return {"mcpUrl": mcp_url, "token": secret}
    return {"token": secret}


class RuntimeCredentialStoreError(RuntimeError):
    """The records file cannot be read, so it is not rewritten."""


def _to_json(record: RuntimeCredential) -> dict[str, Any]:
    data = {name: getattr(record, name) for name in record.__dataclass_fields__}
    data["endpoint"] = list(record.endpoint)
    return data


def _from_json(raw: dict[str, Any]) -> RuntimeCredential:
    scheme, host, port = raw["endpoint"]
    return RuntimeCredential(
        principal_id=str(raw["principal_id"]), token_id=str(raw["token_id"]),
        proposal_id=str(raw["proposal_id"]), runtime=str(raw["runtime"]),
        endpoint=(str(scheme), str(host), int(port)), project_id=str(raw["project_id"]),
        credential_id=str(raw["credential_id"]), credential_name=str(raw["credential_name"]),
        workflow_id=str(raw["workflow_id"]), mcp_url=str(raw["mcp_url"]), state=str(raw["state"]),
        created_at=float(raw["created_at"]), boot_id=str(raw["boot_id"]),
        credential_type=str(raw.get("credential_type", "httpBearerAuth")),
    )


class RuntimeCredentialStore:
    """The delivery records. One lock guards every read-modify-write and the in-flight set."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._warned = False

    def _read_locked(self) -> list[RuntimeCredential] | None:
        """The records, ``[]`` for no file, ``None`` for a file that cannot be parsed."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            self._warn_unreadable()
            return None
        try:
            return [_from_json(raw) for raw in json.loads(text)["records"]]
        except (ValueError, KeyError, TypeError):
            self._warn_unreadable()
            return None

    def _warn_unreadable(self) -> None:
        if not self._warned:
            self._warned = True
            logger.warning("The agent runtime credential records cannot be read; they are left as they are")

    def _write_locked(self, records: list[RuntimeCredential]) -> None:
        secure_files.atomic_write_json(self._path, {"version": 1, "records": [_to_json(r) for r in records]})

    def _for_update_locked(self) -> list[RuntimeCredential]:
        records = self._read_locked()
        if records is None:
            raise RuntimeCredentialStoreError("The agent runtime credential records cannot be read.")
        return records

    def add(self, record: RuntimeCredential) -> None:
        with self._lock:
            records = self._for_update_locked()
            self._write_locked([*records, record])

    def set_state(self, credential_id: str, expected: str, new: str) -> bool:
        """Compare-and-set: ``False`` when the record is gone or is not in ``expected``."""
        with self._lock:
            records = self._for_update_locked()
            for index, record in enumerate(records):
                if record.credential_id == credential_id:
                    if record.state != expected:
                        return False
                    records[index] = replace(record, state=new)
                    self._write_locked(records)
                    return True
            return False

    def remove(self, credential_id: str) -> None:
        with self._lock:
            records = self._for_update_locked()
            kept = [r for r in records if r.credential_id != credential_id]
            if len(kept) != len(records):
                self._write_locked(kept)

    def all(self) -> list[RuntimeCredential]:
        with self._lock:
            return self._read_locked() or []

    def for_token(self, principal_id: str, token_id: str) -> RuntimeCredential | None:
        return next((r for r in self.all() if r.principal_id == principal_id and r.token_id == token_id), None)

    def for_proposal(self, principal_id: str, proposal_id: str) -> RuntimeCredential | None:
        """The newest record for the proposal that is not abandoned, so a retry after an abandoned
        attempt is found rather than the old failure; ``None`` when every record is abandoned."""
        live = [
            r for r in self.all()
            if r.principal_id == principal_id and r.proposal_id == proposal_id and r.state != ABANDONED
        ]
        return live[-1] if live else None

    @contextlib.contextmanager
    def in_flight(self, credential_id: str) -> Iterator[None]:
        """Marks a delivery that is still running, so a sweep does not treat its record as a crash's."""
        with self._lock:
            self._in_flight.add(credential_id)
        try:
            yield
        finally:
            with self._lock:
                self._in_flight.discard(credential_id)

    def is_in_flight(self, credential_id: str) -> bool:
        with self._lock:
            return credential_id in self._in_flight


_store: RuntimeCredentialStore | None = None
_store_lock = threading.Lock()


def get_store() -> RuntimeCredentialStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = RuntimeCredentialStore(paths.org_dir() / STORE_FILE_NAME)
        return _store


def set_store(store: RuntimeCredentialStore | None) -> None:
    global _store
    with _store_lock:
        _store = store


def strip_control(text: str) -> str:
    """``text`` without control characters, for audit summaries that carry text from an AI client."""
    return "".join(ch for ch in text if ch.isprintable())


def record_event(decision: str, record: RuntimeCredential, summary: str) -> None:
    """Audit one delivery or cleanup event under the record's person. Failures are logged and swallowed."""
    try:
        with principal_scope(Principal(id=record.principal_id)):
            audit_log.get_audit_logger().record(audit_log.AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=audit_log.current_week(),
                request_id=record.proposal_id,
                connector="policy",
                tool=_TOOL_NAME,
                tool_name=_TOOL_NAME,
                summary=summary,
                sender=record.principal_id,
                decision=decision,
                auto_accept_rule="",
                latency_seconds=max(0.0, time.time() - record.created_at),
            ))
    except Exception as exc:
        logger.warning("Audit log write failed: %s", exc)


@dataclass(frozen=True)
class DeliveryOutcome:
    token: ScopedToken | None
    record: RuntimeCredential | None
    error: str  # "" on success
    status: int  # 200, 400, 500, 502 or 503


def _delivered_summary(record: RuntimeCredential, token: ScopedToken) -> str:
    text = (
        f"Delivered scoped token '{strip_control(token.label)}' ({token.id}) to n8n at {record.endpoint[1]} "
        f"as credential '{strip_control(record.credential_name)}' ({record.credential_id})"
    )
    if record.workflow_id:
        text += f", for workflow {strip_control(record.workflow_id)} as reported by the AI client"
    return text


def _failed_summary(record: RuntimeCredential, label: str, error: Exception) -> str:
    reason = str(error) if isinstance(error, N8nApiClientError) else _UNEXPECTED_ERROR
    return (
        f"Could not deliver scoped token '{strip_control(label)}' ({record.token_id}) to n8n at "
        f"{record.endpoint[1]}: {reason}"
    )


def deliver(
    *, principal: Principal, bound: dict[str, Any], proposal: TokenProposal,
    config: agent_runtimes.N8nRuntimeConfig, api: N8nApi, token_store: ScopedTokenStore,
    policy: ScopedTokenPolicy, on_abandoned: Callable[[], None] | None = None,
    held_audiences: Mapping[str, frozenset[str]] | None = None, principal_email_verified: bool = False,
) -> DeliveryOutcome:
    """Create the token and its credential in the runtime, recording both before either exists
    (ADR 0281). ``bound`` is the confirmed request; the secret goes to the runtime and nowhere else."""
    target = proposal.delivery
    if target is None:
        raise ValueError("The proposal has no delivery target.")
    records = get_store()
    token_id = new_token_id()
    credential_id = secrets.token_hex(8)
    record = RuntimeCredential(
        principal_id=principal.id, token_id=token_id, proposal_id=proposal.id, runtime=target.runtime,
        endpoint=agent_runtimes.endpoint(config.base_url), project_id=config.project_id,
        credential_id=credential_id, credential_name=agent_runtimes.credential_name(bound["label"]),
        workflow_id=target.workflow_id, mcp_url=proposal.mcp_url, state=CREATING,
        created_at=time.time(), boot_id=BOOT_ID, credential_type=config.credential_type,
    )

    def fail(error: Exception, *, delete_credential: bool) -> DeliveryOutcome:
        try:
            token_store.revoke(principal, token_id)
        except Exception as exc:
            logger.error("Could not revoke scoped token %s after a failed delivery: %s", token_id, type(exc).__name__)
            record_event(
                "runtime_credential_delivery_failed", record, _failed_summary(record, bound["label"], error),
            )
            return DeliveryOutcome(None, None, DELIVERY_FAILED_NOT_REVOKED, 500)
        if delete_credential:
            try:
                api.delete_credential(credential_id)
            except Exception as exc:
                logger.warning("Could not delete n8n credential %s: %s", credential_id, type(exc).__name__)
        with contextlib.suppress(RuntimeCredentialStoreError):
            records.set_state(credential_id, CREATING, ABANDONED)
        if on_abandoned is not None:
            on_abandoned()
        record_event("runtime_credential_delivery_failed", record, _failed_summary(record, bound["label"], error))
        return DeliveryOutcome(None, None, DELIVERY_FAILED, 502)

    if config.credential_type == "privacyFenceApi":
        try:
            api.check_type(config.credential_type)
        except Exception as exc:
            record_event(
                "runtime_credential_delivery_failed", record, _failed_summary(record, bound["label"], exc),
            )
            if isinstance(exc, N8nApiClientError) and exc.status == 404:
                return DeliveryOutcome(None, None, NODE_NOT_INSTALLED, 503)
            return DeliveryOutcome(None, None, DELIVERY_FAILED, 502)

    with records.in_flight(credential_id):
        try:
            records.add(record)
        except RuntimeCredentialStoreError:
            return DeliveryOutcome(None, None, RECORD_FAILED, 500)
        try:
            token, secret = token_store.create(
                principal, label=bound["label"], days=bound["days"], rules=bound["rules"],
                tools=bound["tools"], policy=policy,
                allow_personal_data=bound["allow_personal_data"],
                allow_personal_data_reads=bound["allow_personal_data_reads"],
                upstream_audiences=bound.get("upstream_audiences") or None,
                held_audiences=held_audiences,
                principal_email_verified=principal_email_verified,
                token_id=token_id,
            )
        except ScopedTokenError as exc:
            with contextlib.suppress(RuntimeCredentialStoreError):
                records.remove(credential_id)
            return DeliveryOutcome(None, None, str(exc), 400)
        try:
            api.create_credential(
                credential_id=credential_id, name=record.credential_name,
                credential_type=config.credential_type,
                data=credential_data(config.credential_type, proposal.mcp_url, secret),
                project_id=config.project_id,
            )
        except Exception as exc:
            return fail(exc, delete_credential=False)
        try:
            claimed = records.set_state(credential_id, CREATING, DELIVERED)
        except RuntimeCredentialStoreError as exc:
            return fail(exc, delete_credential=True)
        if not claimed:
            return fail(RuntimeCredentialStoreError("The delivery record vanished."), delete_credential=True)
    delivered = replace(record, state=DELIVERED)
    record_event("runtime_credential_delivered", delivered, _delivered_summary(delivered, token))
    return DeliveryOutcome(token, delivered, "", 200)


EXPIRED_MESSAGE = (
    "The token has expired, but its n8n credential is kept. Ask the person to open PrivacyFence Settings, "
    "then Scoped tokens, and press Extend on this token; the same credential then works again. "
    "Do not propose a new token."
)


def status_for(
    principal_id: str, proposal_id: str, *, token_store: ScopedTokenStore, registry: ProposalRegistry,
    now: float | None = None,
) -> dict[str, Any]:
    """The answer of ``privacyfence_runtime_credential_status`` (ADR 0280), looked up only by the
    caller's own id and the proposal id: ``delivered`` while the token is live, ``expired`` once it ran
    out (its credential is kept and the person can extend the token), ``pending`` and ``not_delivered``.
    Never carries a token id, digest, secret or the runtime's host."""
    now = time.time() if now is None else now
    not_delivered: dict[str, Any] = {"state": "not_delivered"}
    record = get_store().for_proposal(principal_id, proposal_id)
    if record is not None and record.state == DELIVERED:
        principal = Principal(id=principal_id)
        if token_store.token_state(principal, record.token_id, now) not in ("live", "expired"):
            return not_delivered
        token = next((t for t in token_store.tokens_for(principal) if t.id == record.token_id), None)
        if token is None:
            return not_delivered
        answer: dict[str, Any] = {
            "state": "delivered", "runtime": record.runtime, "credential_id": record.credential_id,
            "credential_name": record.credential_name, "credential_type": record.credential_type,
        }
        if record.credential_type == "privacyFenceApi":
            answer["node"] = agent_runtimes.N8N_NODE_TYPE
        else:
            answer["node_authentication"] = "bearerAuth"
        answer["mcp_url"] = record.mcp_url
        when = datetime.fromtimestamp(token.expires_at, timezone.utc).isoformat()
        if not token.expired(now):
            return {**answer, "token_expires_at": when}
        return {**answer, "state": "expired", "token_expired_at": when, "message": EXPIRED_MESSAGE}
    proposal = registry.get(principal_id, proposal_id, now)
    if record is not None and record.state == CREATING or (record is None and proposal is not None):
        pending: dict[str, Any] = {"state": "pending"}
        if proposal is not None:
            pending["expires_at"] = datetime.fromtimestamp(proposal.expires_at, timezone.utc).isoformat()
        return pending
    return not_delivered
