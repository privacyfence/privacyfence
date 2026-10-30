"""Peek auto-provisioning in organization mode: a Peek account for each person who signs in.

PrivacyFence only ever calls Peek, with a scoped provisioning key. Provisioning starts at sign-in,
never blocks it, and runs on one worker thread. A person's own device sign-in always wins; a Peek
admin's account is never adopted. See ``PeekProvisioningConfig`` for the bundle section.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import org_mode, paths, secure_files
from .peek_client import PeekClientError, PeekProvisioningClient, load_token_file, save_token_file
from .principal import Principal

logger = logging.getLogger(__name__)

TOKEN_DAYS = 90
RENEW_BEFORE_SECONDS = 30 * 86400
INACTIVE_AFTER_SECONDS = 90 * 86400
SWEEP_INTERVAL_SECONDS = 6 * 60 * 60
PACE_SECONDS = 0.5
QUEUE_MAX = 1000
STATE_FILE = "credentials/peek_provisioning.json"
TOKEN_FILE = "credentials/peek_token.json"  # nosec B105  # a filename, not a credential value

_ADMIN_DETAIL_PREFIX = "account is a Peek admin"
_NAME_MAX = 100
_STATUS_PENDING = "pending"

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
# Guards the read-modify-write of every state file: the sign-in hook and the worker both write it.
_STATE_LOCK = threading.Lock()


def principal_lock(principal_id: str) -> threading.Lock:
    """The one lock per person that a provisioning save and a ``/connect`` device sign-in share."""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(principal_id)
        if lock is None:
            lock = _LOCKS[principal_id] = threading.Lock()
        return lock


def _user_path(principal_id: str, relative: str) -> Path:
    return paths.user_dir(Principal(id=principal_id)) / relative


def _read_state(principal_id: str) -> dict[str, Any]:
    try:
        raw = json.loads(_user_path(principal_id, STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state(principal_id: str, **changes: Any) -> None:
    """Merge ``changes`` into the person's state file, under the state lock."""
    with _STATE_LOCK:
        state = _read_state(principal_id)
        now = int(time.time())
        record = {
            "email": str(state.get("email", "")),
            "display_name": str(state.get("display_name", "")),
            "status": str(state.get("status", _STATUS_PENDING)),
            "detail": str(state.get("detail", "")),
            "last_sign_in": state["last_sign_in"] if isinstance(state.get("last_sign_in"), int) else 0,
            "updated_at": now,
        }
        record.update(changes)
        path = _user_path(principal_id, STATE_FILE)
        secure_files.secure_mkdir(path.parent)
        secure_files.atomic_write_json(path, record)


def _token_record(principal_id: str) -> dict[str, str] | None:
    """The person's token file, or ``None`` when there is no usable one."""
    try:
        return load_token_file(str(_user_path(principal_id, TOKEN_FILE)))
    except PeekClientError:
        return None


def _is_device_file_for(record: dict[str, str] | None, base_url: str) -> bool:
    return record is not None and record["source"] == "device" and record["base_url"] == base_url


def _delete_provisioned_file(principal_id: str) -> bool:
    """Delete the token file if it is a provisioned one. Returns whether it did. Caller holds the lock."""
    record = _token_record(principal_id)
    if record is None or record["source"] != "provisioned":
        return False
    try:
        _user_path(principal_id, TOKEN_FILE).unlink()
    except FileNotFoundError:
        return False
    return True


def provision_principal(
    principal_id: str,
    email: str,
    display_name: str,
    *,
    client: PeekProvisioningClient,
    base_url: str,
    on_change: Callable[[str], None],
    now: float,
) -> str:
    """Create or adopt the person's Peek account and keep a provisioned token for it. Returns the
    status written to the state file. Nothing is raised."""
    status, detail = _provision(principal_id, email, display_name, client, base_url, on_change, now)
    _write_state(principal_id, email=email, display_name=display_name, status=status, detail=detail)
    if status == "error":
        logger.warning("Peek provisioning for %s failed: %s", principal_id, detail)
    else:
        logger.info("Peek provisioning for %s: %s", principal_id, status)
    return status


def _provision(
    principal_id: str,
    email: str,
    display_name: str,
    client: PeekProvisioningClient,
    base_url: str,
    on_change: Callable[[str], None],
    now: float,
) -> tuple[str, str]:
    if not email:
        return "no_email", ""
    token_path = str(_user_path(principal_id, TOKEN_FILE))
    try:
        with principal_lock(principal_id):
            record = _token_record(principal_id)
        if _is_device_file_for(record, base_url):
            return "user_connected", ""
        name = "" if display_name in (email, principal_id) else display_name[:_NAME_MAX]
        account = client.upsert_account(principal_id, email, name)
        if account.disabled:
            return "disabled", ""
        fresh = (
            record is not None
            and record["source"] == "provisioned"
            and record["base_url"] == base_url
            and account.has_token
            and int(record["expires_at"]) - now > RENEW_BEFORE_SECONDS
        )
        if not fresh:
            minted = client.mint_token(principal_id, TOKEN_DAYS)
            with principal_lock(principal_id):
                if _is_device_file_for(_token_record(principal_id), base_url):
                    return "user_connected", ""
                save_token_file(token_path, base_url, minted.token, source="provisioned", expires_at=minted.expires_at)
            on_change(principal_id)
        return "provisioned", ""
    except PeekClientError as exc:
        if exc.status in (403, 409) and exc.detail.startswith(_ADMIN_DETAIL_PREFIX):
            with principal_lock(principal_id):
                removed = _delete_provisioned_file(principal_id)
            if removed:
                on_change(principal_id)
            return "admin", ""
        return "error", str(exc)


class PeekProvisioner:
    """One worker thread and one sweep thread; sign-ins and the sweep only enqueue."""

    def __init__(
        self,
        *,
        client: PeekProvisioningClient,
        config: org_mode.PeekProvisioningConfig,
        on_change: Callable[[str], None],
        stop_event: threading.Event,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._config = config
        self._on_change = on_change
        self._stop = stop_event
        self._clock = clock
        self._queue: queue.Queue[str] = queue.Queue(maxsize=QUEUE_MAX)
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        # The worker waits for the startup check, so the check and the worker never share the client.
        self._checked = threading.Event()

    def _enqueue(self, principal_id: str) -> bool:
        with self._lock:
            if principal_id in self._queued:
                return False
            try:
                self._queue.put_nowait(principal_id)
            except queue.Full:
                logger.warning(
                    "Peek provisioning queue is full; %s waits for the next sweep or sign-in", principal_id
                )
                return False
            self._queued.add(principal_id)
            return True

    def request(self, principal: Principal, *, email_verified: bool) -> None:
        changes: dict[str, Any] = {
            "email": principal.email,
            "display_name": principal.display_name,
            "last_sign_in": int(self._clock()),
        }
        skip = ""
        if not principal.email:
            skip = "no_email"
        elif not email_verified and not self._config.trust_unverified_email:
            skip = "unverified_email"
        if skip:
            changes.update(status=skip, detail="")
        _write_state(principal.id, **changes)
        if not skip:
            self._enqueue(principal.id)

    def process_next(self, timeout: float) -> bool:
        try:
            principal_id = self._queue.get(timeout=timeout)
        except queue.Empty:
            return False
        with self._lock:
            self._queued.discard(principal_id)
        try:
            state = _read_state(principal_id)
            provision_principal(
                principal_id,
                str(state.get("email", "")),
                str(state.get("display_name", "")),
                client=self._client,
                base_url=self._config.base_url,
                on_change=self._on_change,
                now=self._clock(),
            )
            self._stop.wait(PACE_SECONDS)
        except Exception as exc:
            logger.warning("Peek provisioning worker error: %s", exc)
        return True

    def sweep_once(self) -> int:
        users_root = paths.data_dir() / "users"
        if not users_root.is_dir():
            return 0
        now = self._clock()
        enqueued = 0
        for entry in sorted(users_root.iterdir()):
            if not entry.is_dir() or paths.safe_principal_id(entry.name) != entry.name:
                continue
            state = _read_state(entry.name)
            last = state.get("last_sign_in", 0)
            if (
                state.get("status") in ("provisioned", "error")
                and isinstance(last, int)
                and now - last <= INACTIVE_AFTER_SECONDS
                and self._enqueue(entry.name)
            ):
                enqueued += 1
        return enqueued

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            if not self._checked.wait(1.0):
                continue
            self.process_next(1.0)

    def _sweep_loop(self) -> None:
        try:
            try:
                count = self._client.check()
                logger.info("Peek provisioning ready at %s (%d accounts managed)", self._client.host, count)
            except PeekClientError as exc:
                logger.warning("Peek provisioning check failed: %s", exc)
        finally:
            self._checked.set()
        while True:
            try:
                self.sweep_once()
            except Exception as exc:
                logger.warning("Peek provisioning sweep failed (will retry): %s", exc)
            if self._stop.wait(SWEEP_INTERVAL_SECONDS):
                return

    def start(self) -> None:
        threading.Thread(target=self._work_loop, name="peek-provisioning", daemon=True).start()
        threading.Thread(target=self._sweep_loop, name="peek-provisioning-sweep", daemon=True).start()


_INSTANCE: PeekProvisioner | None = None


def start(
    org_config: dict[str, Any], *, on_change: Callable[[str], None], stop_event: threading.Event
) -> PeekProvisioner | None:
    global _INSTANCE
    config = org_mode.PeekProvisioningConfig.from_org_config(org_config)
    if not config.enabled:
        return None
    key = org_mode.provisioning_key(config.key_env)
    if not key:
        logger.warning(
            "Peek auto-provisioning is on, but %s is not set; people connect Peek by hand on /connect.",
            config.key_env,
        )
        return None
    provisioner = PeekProvisioner(
        client=PeekProvisioningClient(config.base_url, key), config=config, on_change=on_change, stop_event=stop_event
    )
    provisioner.start()
    _INSTANCE = provisioner
    return provisioner


def notify_sign_in(principal: Principal, *, email_verified: bool) -> None:
    """Hand a sign-in to the provisioner. Never blocks and never raises."""
    if _INSTANCE is None:
        return
    try:
        _INSTANCE.request(principal, email_verified=email_verified)
    except Exception as exc:
        logger.warning("Peek provisioning could not take the sign-in of %s: %s", principal.id, exc)


def email_verified_claim(claims: dict[str, Any]) -> bool:
    value = claims.get("email_verified")
    return value is True or value == "true"


def release(principal_id: str) -> None:
    """Delete a provisioned token file (a device file stays) and stop renewing: status ``released``, no email."""
    with principal_lock(principal_id):
        _delete_provisioned_file(principal_id)
    _write_state(principal_id, email="", status="released", detail="")
