"""``SealedRefreshStore`` -- the on-disk half of org mode's OAuth refresh
tokens (#402), sealed to the bearer that presents them.

Before this module, every store backing org-mode auth was in-process only,
so restarting the daemon forced every connected MCP client through a full
``authorize -> IdP redirect -> sign-in -> code exchange`` round trip. For a
human at a browser that is a minor annoyance. For a *scheduled or background*
tool call it is a dead end: there is nobody present to complete the redirect,
and the call simply fails. That -- not the browser case -- is what this
exists to fix.

**Only refresh tokens are persisted.** Access tokens live an hour and the
refresh path re-mints them without anyone's involvement, so persisting them
buys nothing the refresh token doesn't already buy. Browser sessions
(``org_session.py``) stay in memory too: their whole point is that a human
is sitting in front of them, and that human can sign in again. What survives
a restart here is exactly the credential whose loss needs a human who isn't
there.

Sealed, not encrypted-at-rest
-----------------------------
The obvious implementation -- one store encrypted under a key the daemon
holds -- does not actually buy what it appears to. The natural place for
that key is next to the data (an env var, the same ``org_dir()``, the host
keyring), so anyone who can read the token file can typically also read the
key. It moves the secret; it doesn't protect it.

So no key is stored at all. Each record is encrypted with AES-256-GCM under
a key derived by HKDF-SHA256 from *the refresh token itself* -- the same
construction ``download_staging.py`` already uses for staged files, and for
the same reason: the only copy of the key material is the one the legitimate
holder presents on the next request. A stolen disk image yields a map of
SHA-256 hashes to ciphertext and nothing else. It cannot be decrypted, and a
forged token cannot be made to hash to a stored key, so the file is inert
without a token that was already valid.

What this does *not* defend against is someone who has the token anyway --
but such a person already holds a working credential, so the store adds no
exposure there.

What is deliberately in the clear
---------------------------------
Two fields per record, because both have to be usable *without* a token in
hand:

- ``principal_id`` -- so "sign out everywhere for this principal" can find
  the records to delete. ``paths.user_dir()`` already creates
  ``data_dir()/users/<principal.id>`` per principal, so the set of principal
  ids is on this disk either way; putting it here leaks nothing new.
- ``chain_expires_at`` -- so a lapsed chain can be pruned by the load/write
  path rather than surviving until someone happens to present it.

Neither is a credential. Everything that is (the client binding, the
principal's claims, the chain's issuance) is inside the ciphertext.

Revocation
----------
``OrgOAuthProvider`` cascades every revocation path -- logout, explicit
``/revoke``, access-token expiry, refresh rotation, the SEC-12 absolute-
lifetime lapse -- through its own ``_revoke_pair_locked``, which calls
``discard`` here. That single choke point is what keeps "gone from memory"
and "gone from disk" from drifting apart; a new revocation path that skips
it would leave a record that outlives its intended lifetime, which is the
one correctness risk persistence introduces that the in-memory design
didn't have.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..secure_files import atomic_write_json

logger = logging.getLogger(__name__)

REFRESH_STORE_FILE_NAME = "oauth_refresh.json"

# AES-256-GCM with a fresh 12-byte nonce per record -- see download_staging.
# py's own constants for why 96 bits is the right size and why the nonce
# sits unprotected beside the ciphertext it belongs to.
_NONCE_LEN = 12
_KEY_LEN = 32  # AES-256
_HKDF_INFO = b"privacyfence-org-refresh-token-v1"

# Ceiling on stored records, enforced after expired ones are pruned. One
# live refresh chain per signed-in MCP client per principal is the normal
# shape, so this clears a large org comfortably while still bounding the
# file -- the same posture as oauth_provider.py's own _MAX_REGISTERED_
# CLIENTS. Over the cap, the chains closest to expiry are dropped first:
# they are the ones whose holder has least left to lose, and dropping one
# costs a sign-in, never a security gap.
_MAX_RECORDS = 5000


def _derive_key(token: bytes) -> bytes:
    """HKDF-SHA256 over the raw refresh token. The only key material this
    module ever produces, and only ever in memory -- see module docstring."""
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_LEN, salt=None, info=_HKDF_INFO).derive(token)


def _lookup_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SealedRefreshRecord:
    """The sealed half of one refresh token: everything
    ``OrgOAuthProvider.load_refresh_token`` needs to rebuild an
    ``_OrgRefreshToken`` from a presented token alone, with no in-memory
    state left over from before the restart.
    """

    client_id: str
    scopes: list[str]
    subject: str
    email: str
    display_name: str
    is_admin: bool
    issued_at: float


@dataclass
class _SealedEntry:
    principal_id: str
    chain_expires_at: float
    nonce: bytes
    ciphertext: bytes


class SealedRefreshStore:
    """Keyed by ``sha256(token)``; the record itself is encrypted under a key
    only a presented token can derive. Holds the whole map in memory and
    rewrites the file atomically on every mutation, exactly as
    ``OrgOAuthProvider._save_clients_locked`` does for ``oauth_clients.json``
    -- the store is bounded (``_MAX_RECORDS``) and a refresh is not a hot
    path, so the simplicity is worth more than incremental writes.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._entries: dict[str, _SealedEntry] = self._load()
        self._restored_count = len(self._entries)

    # ------------------------------------------------------------------ #
    # Disk
    # ------------------------------------------------------------------ #

    def _load(self) -> dict[str, _SealedEntry]:
        """Never raises. A missing file is the ordinary first-run case; an
        unreadable or malformed one starts empty rather than refusing to
        serve, matching ``_load_clients``' own posture -- the cost of losing
        this file is that everyone signs in again, which is precisely the
        behavior that predates it."""
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("refresh-token store is not a JSON object")
            now = time.time()
            entries: dict[str, _SealedEntry] = {}
            for lookup_id, data in raw.items():
                entry = _SealedEntry(
                    principal_id=str(data["principal_id"]),
                    chain_expires_at=float(data["chain_expires_at"]),
                    # validate=True: b64decode otherwise *silently ignores*
                    # non-alphabet characters, so a corrupted field would load
                    # as plausible-looking garbage and only surface as an
                    # InvalidTag on the next get(). Failing here instead keeps
                    # "damaged file" one behavior rather than two.
                    nonce=base64.b64decode(data["nonce"], validate=True),
                    ciphertext=base64.b64decode(data["ciphertext"], validate=True),
                )
                if entry.chain_expires_at > now:
                    entries[lookup_id] = entry
        except (OSError, ValueError, KeyError, TypeError, binascii.Error) as exc:
            logger.warning("Ignoring unreadable OAuth refresh-token store at '%s': %s", self._path, exc)
            return {}
        return entries

    def _save_locked(self) -> None:
        """Caller already holds ``self._lock``."""
        payload = {
            lookup_id: {
                "principal_id": entry.principal_id,
                "chain_expires_at": entry.chain_expires_at,
                "nonce": base64.b64encode(entry.nonce).decode("ascii"),
                "ciphertext": base64.b64encode(entry.ciphertext).decode("ascii"),
            }
            for lookup_id, entry in self._entries.items()
        }
        try:
            atomic_write_json(self._path, payload)
        except OSError as exc:
            # A refresh that can't be written down still works for this
            # process's lifetime -- degrading to the pre-#402 behavior beats
            # failing a sign-in over a full or read-only disk.
            logger.warning("Could not persist OAuth refresh-token store to '%s': %s", self._path, exc)

    # ------------------------------------------------------------------ #
    # Mutation
    # ------------------------------------------------------------------ #

    def put(
        self, token: str, *, principal_id: str, chain_expires_at: float, record: SealedRefreshRecord,
    ) -> None:
        plaintext = json.dumps({
            "client_id": record.client_id, "scopes": record.scopes, "subject": record.subject,
            "email": record.email, "display_name": record.display_name,
            "is_admin": record.is_admin, "issued_at": record.issued_at,
        }).encode("utf-8")
        nonce = secrets.token_bytes(_NONCE_LEN)
        key = _derive_key(token.encode("utf-8"))
        entry = _SealedEntry(
            principal_id=principal_id, chain_expires_at=chain_expires_at,
            nonce=nonce, ciphertext=AESGCM(key).encrypt(nonce, plaintext, None),
        )
        with self._lock:
            self._entries[_lookup_id(token)] = entry
            self._prune_locked()
            self._save_locked()

    def _prune_locked(self) -> None:
        """Caller already holds ``self._lock``. Expired chains first, then
        the ones closest to expiry if the cap is still exceeded."""
        now = time.time()
        for lookup_id in [lid for lid, e in self._entries.items() if e.chain_expires_at <= now]:
            del self._entries[lookup_id]
        if len(self._entries) <= _MAX_RECORDS:
            return
        by_expiry = sorted(self._entries.items(), key=lambda kv: kv[1].chain_expires_at)
        for lookup_id, _ in by_expiry[: len(self._entries) - _MAX_RECORDS]:
            del self._entries[lookup_id]
        logger.warning(
            "OAuth refresh-token store is at its %d-record cap; dropped the chains closest to expiry",
            _MAX_RECORDS,
        )

    def discard(self, token: str) -> None:
        """Idempotent -- a token this store never held (the common case, for
        any revocation happening inside the process that minted it and never
        restarted) is not an error."""
        with self._lock:
            if self._entries.pop(_lookup_id(token), None) is None:
                return
            self._save_locked()

    def discard_all_for(self, principal_id: str) -> int:
        """Every stored chain belonging to one principal -- the disk half of
        ``OrgSessionStore.destroy_all_for``'s "sign out everywhere". Returns
        how many were removed."""
        with self._lock:
            stale = [lid for lid, e in self._entries.items() if e.principal_id == principal_id]
            for lookup_id in stale:
                del self._entries[lookup_id]
            if stale:
                self._save_locked()
            return len(stale)

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #

    def get(self, token: str) -> SealedRefreshRecord | None:
        """The sealed record for ``token``, or ``None`` for one this store
        doesn't hold, one whose chain has lapsed, or one whose ciphertext
        won't open. Never raises: an unknown or tampered-with token is "not
        authenticated", not a 500.

        The chain-expiry check here is deliberately redundant with
        ``OrgOAuthProvider.load_refresh_token``'s own SEC-12 check. That one
        is the authority; this one exists so a store file that outlived its
        contents can't hand back a chain the provider would then have to
        reject.
        """
        lookup_id = _lookup_id(token)
        with self._lock:
            entry = self._entries.get(lookup_id)
            if entry is None:
                return None
            if entry.chain_expires_at <= time.time():
                del self._entries[lookup_id]
                self._save_locked()
                return None
            try:
                plaintext = AESGCM(_derive_key(token.encode("utf-8"))).decrypt(
                    entry.nonce, entry.ciphertext, None,
                )
            except InvalidTag:
                # Only the token that produced this record derives its key, and
                # that token is what just unlocked the lookup above -- so this
                # is a corrupted or hand-edited store, not a wrong guess.
                logger.warning("Discarding an OAuth refresh-token record that would not decrypt")
                del self._entries[lookup_id]
                self._save_locked()
                return None
        data = json.loads(plaintext)
        return SealedRefreshRecord(
            client_id=data["client_id"], scopes=list(data["scopes"]), subject=data["subject"],
            email=data["email"], display_name=data["display_name"],
            is_admin=bool(data["is_admin"]), issued_at=float(data["issued_at"]),
        )

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    @property
    def restored_count(self) -> int:
        """How many unexpired records were on disk when this store was
        opened -- what web/oauth_provider.py logs at startup so an operator
        can tell "clients will reconnect silently" from "everyone is signing
        in again" without reproducing it."""
        return self._restored_count

    @property
    def record_count(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = ["REFRESH_STORE_FILE_NAME", "SealedRefreshRecord", "SealedRefreshStore"]
