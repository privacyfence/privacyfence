"""Cleanup of the n8n credentials PrivacyFence delivered (ADR 0278-0282).

A delivered token is an ordinary scoped token, revoked and expired by the paths that already exist.
This sweeper reconciles the delivery records against the token store, so every revoke path is
covered without a hook: when a token is revoked it deletes the credential in the runtime, and it
finishes deliveries a crash left half done. An expired token's credential is kept until the token
is revoked or extended (ADR 0284). It never holds a secret and never deletes on a
host other than the configured one.
"""

from __future__ import annotations

import logging
import threading
import time

from . import agent_runtimes, runtime_credentials, scoped_tokens
from .n8n_api import N8nApi, N8nApiClientError
from .principal import Principal
from .runtime_credentials import (
    ABANDONED, CREATING, DELIVERED, RuntimeCredential, RuntimeCredentialStore, RuntimeCredentialStoreError,
    record_event, strip_control,
)
from .scoped_tokens import ScopedTokenStore

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 300
DELETE_FAILURE_AUDIT_SECONDS = 86400
_POLL_SECONDS = 0.5

_delete_failure_audited: dict[str, float] = {}


def _principal(record: RuntimeCredential) -> Principal:
    return Principal(id=record.principal_id)


def _label(record: RuntimeCredential) -> str:
    return f"'{strip_control(record.credential_name)}' ({record.credential_id})"


def _remove(records: RuntimeCredentialStore, record: RuntimeCredential, reason: str) -> None:
    records.remove(record.credential_id)
    _delete_failure_audited.pop(record.credential_id, None)
    record_event(
        "runtime_credential_deleted", record,
        f"Deleted n8n credential {_label(record)} at {record.endpoint[1]}: token {reason}",
    )


def _delete_and_remove(
    records: RuntimeCredentialStore, record: RuntimeCredential, api: N8nApi, reason: str, now: float,
) -> None:
    try:
        api.delete_credential(record.credential_id)
    except N8nApiClientError as exc:
        last = _delete_failure_audited.get(record.credential_id)
        if last is None or now - last >= DELETE_FAILURE_AUDIT_SECONDS:
            _delete_failure_audited[record.credential_id] = now
            record_event(
                "runtime_credential_delete_failed", record,
                f"Could not delete n8n credential {_label(record)} at {record.endpoint[1]}: {exc}",
            )
        return
    _remove(records, record, reason)


def _orphan(records: RuntimeCredentialStore, token_store: ScopedTokenStore, record: RuntimeCredential, now: float) -> None:
    state = token_store.token_state(_principal(record), record.token_id, now)
    if state == "unknown":
        return
    if state != "absent":
        try:
            token_store.revoke(_principal(record), record.token_id)
        except Exception as exc:
            logger.warning("Could not revoke scoped token %s: %s", record.token_id, type(exc).__name__)
            return
    record_event(
        "runtime_credential_orphaned", record,
        f"n8n credential {_label(record)} is at {record.endpoint[1]}, which is no longer this server's n8n; "
        "its token was revoked. Delete the credential there by hand.",
    )
    records.remove(record.credential_id)


def _abandoned(
    records: RuntimeCredentialStore, token_store: ScopedTokenStore, record: RuntimeCredential, api: N8nApi, now: float,
) -> None:
    state = token_store.token_state(_principal(record), record.token_id, now)
    if state == "unknown":
        return
    if state != "absent":
        try:
            token_store.revoke(_principal(record), record.token_id)
        except Exception as exc:
            logger.warning("Could not revoke scoped token %s: %s", record.token_id, type(exc).__name__)
            return
    _delete_and_remove(records, record, api, "not delivered", now)


def _sweep_record(
    record: RuntimeCredential, *, records: RuntimeCredentialStore, token_store: ScopedTokenStore,
    api: N8nApi, now: float,
) -> None:
    if record.state == DELIVERED:
        state = token_store.token_state(_principal(record), record.token_id, now)
        if state == "absent":
            _delete_and_remove(records, record, api, "revoked", now)
        return
    if record.state == CREATING:
        if records.is_in_flight(record.credential_id):
            return
        if not records.set_state(record.credential_id, CREATING, ABANDONED):
            return
    _abandoned(records, token_store, record, api, now)


def sweep_once(
    *, records: RuntimeCredentialStore, token_store: ScopedTokenStore,
    config: agent_runtimes.N8nRuntimeConfig, api: N8nApi | None, now: float,
) -> None:
    current = agent_runtimes.endpoint(config.base_url)
    warned = False
    for record in records.all():
        try:
            if record.endpoint != current:
                _orphan(records, token_store, record, now)
            elif api is None:
                if not warned:
                    warned = True
                    logger.warning("The n8n API key is not available; credential cleanup waits")
            else:
                _sweep_record(record, records=records, token_store=token_store, api=api, now=now)
        except RuntimeCredentialStoreError as exc:
            logger.warning("Credential cleanup skipped %s: %s", record.credential_id, exc)
        except Exception as exc:  # one bad record must not stop the others
            logger.warning("Credential cleanup skipped a record: %s", type(exc).__name__)


class _Sweeper:
    def __init__(self, work: threading.Thread | None = None) -> None:
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.thread = work


_INSTANCE: _Sweeper | None = None


def _run(
    sweeper: _Sweeper, *, stop_event: threading.Event, records: RuntimeCredentialStore,
    token_store: ScopedTokenStore, config: agent_runtimes.N8nRuntimeConfig, api: N8nApi,
) -> None:
    try:
        api.check()
    except N8nApiClientError as exc:
        logger.warning("The n8n API check failed: %s", exc)

    def done() -> bool:
        return stop_event.is_set() or sweeper.stopped.is_set()

    while not done():
        try:
            sweep_once(records=records, token_store=token_store, config=config, api=api, now=time.time())
        except Exception as exc:
            logger.warning("Credential cleanup failed: %s", type(exc).__name__)
        deadline = time.monotonic() + SWEEP_INTERVAL_SECONDS
        while not done() and time.monotonic() < deadline:
            if sweeper.wake.wait(_POLL_SECONDS):
                sweeper.wake.clear()
                break


def start(*, config: agent_runtimes.N8nRuntimeConfig, stop_event: threading.Event) -> None:
    global _INSTANCE
    api = agent_runtimes.api_for(config)
    if api is None:
        return
    stop()
    sweeper = _Sweeper()
    sweeper.thread = threading.Thread(
        target=_run, name="runtime-credential-sweeper", daemon=True,
        kwargs=dict(
            sweeper=sweeper, stop_event=stop_event, records=runtime_credentials.get_store(),
            token_store=scoped_tokens.get_store(), config=config, api=api,
        ),
    )
    _INSTANCE = sweeper
    sweeper.thread.start()


def wake() -> None:
    """Run a sweep now. A no-op when no sweeper runs."""
    if _INSTANCE is not None:
        _INSTANCE.wake.set()


def stop() -> None:
    global _INSTANCE
    sweeper, _INSTANCE = _INSTANCE, None
    _delete_failure_audited.clear()
    if sweeper is not None:
        sweeper.stopped.set()
        if sweeper.thread is not None and sweeper.thread is not threading.current_thread():
            sweeper.thread.join(timeout=5)
