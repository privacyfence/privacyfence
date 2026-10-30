"""Registers a Grist API key for each person who signs in to an organization server.

A sign-in only records the person's email and queues them; one worker thread then asks the Grist
provisioner (a small service next to Grist) for the person's key and saves it as their Grist
connection. A sweep re-queues people whose key is due for renewal or whose account did not exist
yet. PrivacyFence sign-in never waits on any of it. A key the person pasted on /connect always
wins. Nothing here logs an email, a name or a key; only principal ids and statuses.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import org_mode, paths
from .grist_auth import GristClientError, key_id, load_token_file, save_api_key, token_source
from .grist_provisioner_api import GristProvisionerClient, GristProvisionerClientError
from .principal import Principal
from .secure_files import atomic_write_json

logger = logging.getLogger(__name__)

RENEW_BEFORE_SECONDS = 30 * 86400
INACTIVE_AFTER_SECONDS = 90 * 86400
SWEEP_INTERVAL_SECONDS = 6 * 60 * 60
PACE_SECONDS = 0.5
QUEUE_MAX = 1000
STATE_FILE = "credentials/grist_provisioning.json"
# The same path as daemon_main.TOKEN_FILES["grist"]; this module does not import daemon_main.
TOKEN_FILE = "credentials/grist_token.json"  # nosec B105  # a file path, not a credential

_SWEEP_STATUSES = frozenset({"pending", "provisioned", "adopted", "error", "no_account"})

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def principal_lock(principal_id: str) -> threading.Lock:
    """The one lock that serialises writes to this person's Grist files. Never held across a
    provisioner call, and never taken twice in a nested way."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(principal_id, threading.Lock())


def _user_root(principal_id: str) -> Path:
    return paths.user_dir(Principal(id=principal_id))


def _read_state_at(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _read_state(principal_id: str) -> dict[str, Any]:
    return _read_state_at(_user_root(principal_id) / STATE_FILE)


def _update_state(principal_id: str, *, now: float, **fields: Any) -> dict[str, Any]:
    """The one writer of the state file: read, apply ``fields``, stamp ``updated_at``, write."""
    path = _user_root(principal_id) / STATE_FILE
    with principal_lock(principal_id):
        state = _read_state_at(path)
        state.update(fields)
        state["updated_at"] = int(now)
        atomic_write_json(path, state)
    return state


def _released(principal_id: str) -> bool:
    return _read_state(principal_id).get("status") == "released"


def _usable(path: Path, server_url: str, source: str) -> dict[str, Any] | None:
    """The saved Grist record when it is a readable API-key file of ``source`` for this server."""
    try:
        record = load_token_file(str(path))
    except GristClientError:
        return None
    if (
        record.get("auth") == "api_key"
        and token_source(record) == source
        and record["server_url"] == server_url
    ):
        return record
    return None


def _drop_provisioned(principal_id: str, on_change: Callable[[str], None]) -> None:
    """Delete a provisioned token file, if there is one, and tell the caller afterwards."""
    path = _user_root(principal_id) / TOKEN_FILE
    deleted = False
    with principal_lock(principal_id):
        try:
            record = load_token_file(str(path))
        except GristClientError:
            record = None
        if record is not None and token_source(record) == "provisioned":
            path.unlink(missing_ok=True)
            deleted = True
    if deleted:
        on_change(principal_id)


def provision_principal(
    principal_id: str,
    *,
    client: GristProvisionerClient,
    config: org_mode.GristProvisioningConfig,
    on_change: Callable[[str], None],
    now: float,
) -> str:
    """Bring one person's Grist connection up to date and return the status written. Never raises."""
    status, detail = _provision(principal_id, client=client, config=config, on_change=on_change, now=now)
    if status == "error":
        logger.warning("Grist provisioning for %s failed: %s", principal_id, detail)
    else:
        logger.info("Grist provisioning for %s: %s", principal_id, status)
    return status


def _undo_issue(principal_id: str, client: GristProvisionerClient) -> None:
    try:
        client.release_account(principal_id)
    except GristProvisionerClientError as exc:
        logger.warning(
            "Grist provisioning for %s: a key issued during a release could not be undone (%s); "
            "run --grist-release again",
            principal_id,
            exc,
        )


def _provision(
    principal_id: str,
    *,
    client: GristProvisionerClient,
    config: org_mode.GristProvisioningConfig,
    on_change: Callable[[str], None],
    now: float,
) -> tuple[str, str]:
    def done(status: str, detail: str = "") -> tuple[str, str]:
        # --grist-release runs in another process; never write over it.
        if _released(principal_id):
            return "released", ""
        _update_state(principal_id, now=now, status=status, detail=detail)
        return status, detail

    state = _read_state(principal_id)
    if state.get("status") == "released":
        return "released", ""
    email = state.get("email", "")
    if not email:
        return done("no_email")
    token_path = _user_root(principal_id) / TOKEN_FILE
    with principal_lock(principal_id):
        manual = _usable(token_path, config.server_url, "manual") is not None
        record = _usable(token_path, config.server_url, "provisioned")
    if manual:
        return done("user_connected")
    try:
        account = client.upsert_account(principal_id, email)
        if account.disabled:
            _drop_provisioned(principal_id, on_change)
            return done("disabled")
        if (
            record is not None
            and account.has_key
            and key_id(record["api_key"]) == account.key_id
            and (not record["managed"] or record["expires_at"] - now > RENEW_BEFORE_SECONDS)
        ):
            managed = record["managed"]
        else:
            if _released(principal_id):
                return "released", ""
            issued = client.issue_key(principal_id)
            managed = issued.managed
            with principal_lock(principal_id):
                lost_release = _released(principal_id)
                saved = False
                if not lost_release and _usable(token_path, config.server_url, "manual") is None:
                    save_api_key(
                        str(token_path),
                        config.server_url,
                        issued.key,
                        source="provisioned",
                        managed=issued.managed,
                        expires_at=issued.expires_at,
                    )
                    saved = True
            if lost_release:
                # The release finished before this key was issued; undo the issue so no key outlives it.
                _undo_issue(principal_id, client)
                return "released", ""
            if not saved:
                return done("user_connected")
            on_change(principal_id)
    except GristProvisionerClientError as exc:
        if exc.status == 409 and exc.detail.startswith("account is a Grist admin"):
            _drop_provisioned(principal_id, on_change)
            return done("admin")
        if exc.status == 403 and exc.detail == "account disabled":
            _drop_provisioned(principal_id, on_change)
            return done("disabled")
        if exc.status == 404 and exc.detail.startswith("no Grist account"):
            return done("no_account")
        return done("error", str(exc))
    return done("provisioned" if managed else "adopted")


class GristProvisioner:
    def __init__(
        self,
        *,
        client: GristProvisionerClient,
        config: org_mode.GristProvisioningConfig,
        on_change: Callable[[str], None],
        stop_event: threading.Event,
        clock: Callable[[], float] = time.time,
        check_client: GristProvisionerClient | None = None,
    ) -> None:
        self._client = client
        # requests.Session is not thread-safe, so the sweep thread's startup check gets its own.
        self._check_client = check_client or client
        self._config = config
        self._on_change = on_change
        self._stop_event = stop_event
        self._clock = clock
        self._queue: queue.Queue[str] = queue.Queue(maxsize=QUEUE_MAX)
        self._queued: set[str] = set()
        self._queued_lock = threading.Lock()

    def _enqueue(self, principal_id: str) -> bool:
        with self._queued_lock:
            if principal_id in self._queued:
                return False
            # Mark first: the worker may take the id the instant it is in the queue.
            self._queued.add(principal_id)
            try:
                self._queue.put_nowait(principal_id)
            except queue.Full:
                self._queued.discard(principal_id)
                logger.warning(
                    "Grist provisioning queue is full; %s waits for the next sweep or sign-in", principal_id
                )
                return False
        return True

    def request(self, principal: Principal, *, email_verified: bool) -> None:
        """Record a sign-in and queue the person. Never blocks."""
        if not principal.email:
            status = "no_email"
        elif not email_verified and not self._config.trust_unverified_email:
            status = "unverified_email"
        else:
            status = "pending"
        now = self._clock()
        _update_state(
            principal.id,
            now=now,
            email=principal.email,
            last_sign_in=int(now),
            detail="",
            status=status,
        )
        if status == "pending":
            self._enqueue(principal.id)

    def process_next(self, timeout: float) -> bool:
        try:
            principal_id = self._queue.get(timeout=timeout)
        except queue.Empty:
            return False
        with self._queued_lock:
            self._queued.discard(principal_id)
        try:
            provision_principal(
                principal_id,
                client=self._client,
                config=self._config,
                on_change=self._on_change,
                now=self._clock(),
            )
            self._stop_event.wait(PACE_SECONDS)
        except Exception as exc:
            logger.warning("Grist provisioning worker error: %s", exc)
        return True

    def sweep_once(self) -> int:
        users_root = paths.data_dir() / "users"
        if not users_root.is_dir():
            return 0
        now = self._clock()
        queued = 0
        for entry in sorted(users_root.iterdir()):
            if not entry.is_dir() or paths.safe_principal_id(entry.name) != entry.name:
                continue
            state = _read_state_at(entry / STATE_FILE)
            last_sign_in = state.get("last_sign_in")
            if (
                state.get("status") in _SWEEP_STATUSES
                and isinstance(last_sign_in, int)
                and now - last_sign_in <= INACTIVE_AFTER_SECONDS
                and self._enqueue(entry.name)
            ):
                queued += 1
        return queued

    def _work_loop(self) -> None:
        while not self._stop_event.is_set():
            self.process_next(1.0)

    def _sweep_loop(self) -> None:
        host = urlsplit(self._config.provisioner_url).netloc
        try:
            logger.info(
                "Grist provisioning ready at %s (%d accounts managed)", host, self._check_client.check()
            )
        except GristProvisionerClientError as exc:
            logger.warning("Grist provisioning check failed: %s", exc)
        while True:
            try:
                self.sweep_once()
            except Exception as exc:
                logger.warning("Grist provisioning sweep failed (will retry): %s", exc)
            if self._stop_event.wait(SWEEP_INTERVAL_SECONDS):
                return

    def start(self) -> None:
        threading.Thread(target=self._work_loop, name="grist-provisioning", daemon=True).start()
        threading.Thread(target=self._sweep_loop, name="grist-provisioning-sweep", daemon=True).start()


_INSTANCE: GristProvisioner | None = None


def start(
    org_config: dict[str, Any],
    *,
    on_change: Callable[[str], None],
    stop_event: threading.Event,
) -> GristProvisioner | None:
    """Start the worker and the sweep when the bundle turns provisioning on and the key is set."""
    global _INSTANCE
    config = org_mode.GristProvisioningConfig.from_org_config(org_config)
    if not config.enabled:
        return None
    key = org_mode.provisioning_key(config.key_env)
    if not key:
        logger.warning(
            "Grist auto-provisioning is on, but %s is not set; people connect Grist by hand on /connect.",
            config.key_env,
        )
        return None
    provisioner = GristProvisioner(
        client=GristProvisionerClient(config.provisioner_url, key),
        config=config,
        on_change=on_change,
        stop_event=stop_event,
        check_client=GristProvisionerClient(config.provisioner_url, key),
    )
    provisioner.start()
    _INSTANCE = provisioner
    return provisioner


def notify_sign_in(principal: Principal, *, email_verified: bool) -> None:
    """Called from both sign-in callbacks, off the event loop. Never raises."""
    instance = _INSTANCE
    if instance is None:
        return
    try:
        instance.request(principal, email_verified=email_verified)
    except Exception as exc:
        logger.warning("Grist provisioning could not take the sign-in of %s: %s", principal.id, exc)


def email_verified_claim(claims: dict[str, Any]) -> bool:
    # Not `in (True, "true")`: that would also accept the number 1, and the claim is the JSON
    # true or the string "true" only.
    value = claims.get("email_verified")
    return value is True or value == "true"


def release(principal_id: str) -> None:
    """Forget a person's Grist connection: mark ``released``, clear the email, drop a provisioned
    key file (a manual one stays). ``--grist-release`` calls this before the provisioner, so a
    worker already holding this person sees ``released`` and stops."""
    _update_state(principal_id, now=time.time(), status="released", email="", detail="")
    path = _user_root(principal_id) / TOKEN_FILE
    with principal_lock(principal_id):
        try:
            record = load_token_file(str(path))
        except GristClientError:
            return
        if token_source(record) == "provisioned":
            path.unlink(missing_ok=True)
