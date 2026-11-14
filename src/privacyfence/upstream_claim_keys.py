"""The Ed25519 key set that signs the identity claim sent to upstream MCP servers.

The set lives in ``org_dir()/upstream_claim_keys.json`` (mode 0600). Each key has a
``kid`` (the first 16 characters of the base64url SHA-256 of its raw public key),
its public half ``x``, its private ``seed`` (``None`` once the key is retired), and
three instants: ``created_at``, ``signs_from`` and ``retired_at``.

* The key that signs is the newest one with ``signs_from <= now`` that is not retired.
* ``rotate()`` publishes a new key at once but starts signing with it only
  ``JWKS_CACHE_SECONDS`` later, so a verifier's cached JWKS already holds it. The
  previous key keeps signing until then; its seed is dropped only after its
  ``retired_at`` has passed, when the set is pruned or loaded -- never in ``rotate()``.
* The JWKS lists every key that is not retired, or whose ``retired_at`` plus the
  overlap is still in the future. A key past that is pruned when the set loads.
* Every read-modify-write holds an exclusive ``flock`` on ``upstream_claim_keys.lock``
  (a process-wide threading lock where ``fcntl`` is missing), so the server and the
  command line cannot lose each other's change. A running server notices a change
  from (inode, mtime, size) and keeps the last good set if the file turns malformed.

Plan section 3.4 of ``docs/1103/plan.md`` is the design.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import secure_files

try:  # POSIX only; Windows falls back to a process-wide lock.
    import fcntl
except ImportError:  # pragma: no cover -- exercised on Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

FILENAME = "upstream_claim_keys.json"
LOCK_FILENAME = "upstream_claim_keys.lock"
JWKS_CACHE_SECONDS = 300
CLAIM_TYPE = "pf-identity-claim+jwt"
FILE_VERSION = 1

_PROCESS_LOCK = threading.Lock()


class KeySetError(RuntimeError):
    """The key set is missing, malformed, or has no usable signing key."""


@dataclass(frozen=True)
class ClaimKey:
    kid: str
    x: str
    seed: bytes | None
    created_at: float
    signs_from: float
    retired_at: float | None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _kid_for(public_raw: bytes) -> str:
    return _b64url(hashlib.sha256(public_raw).digest())[:16]


def _public_raw(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )


def _new_key(now: float, signs_from: float) -> ClaimKey:
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = _public_raw(private)
    return ClaimKey(
        kid=_kid_for(public), x=_b64url(public), seed=seed,
        created_at=now, signs_from=signs_from, retired_at=None,
    )


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Exclusive lock for one read-modify-write of the key file at ``path``."""
    if fcntl is None:  # pragma: no cover -- exercised on Windows
        with _PROCESS_LOCK:
            yield
        return
    secure_files.secure_mkdir(path.parent)
    fd = os.open(path.parent / LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _key_to_json(key: ClaimKey) -> dict[str, Any]:
    return {
        "kid": key.kid,
        "x": key.x,
        "seed": None if key.seed is None else _b64url(key.seed),
        "created_at": key.created_at,
        "signs_from": key.signs_from,
        "retired_at": key.retired_at,
    }


def _key_from_json(raw: Any) -> ClaimKey:
    if not isinstance(raw, dict):
        raise ValueError("a key entry is not an object")
    kid, x, seed = raw.get("kid"), raw.get("x"), raw.get("seed")
    if not isinstance(kid, str) or not isinstance(x, str):
        raise ValueError("a key entry has no kid or x")
    if len(_unb64url(x)) != 32:
        raise ValueError("a public key is not 32 bytes")
    seed_bytes: bytes | None = None
    if seed is not None:
        if not isinstance(seed, str):
            raise ValueError("a seed is not a string")
        seed_bytes = _unb64url(seed)
        if len(seed_bytes) != 32:
            raise ValueError("a seed is not 32 bytes")
    def _instant(field: str, *, optional: bool = False) -> float | None:
        value = raw.get(field)
        if value is None and optional:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise ValueError(f"a key entry has a bad {field}")

    created_at, signs_from = _instant("created_at"), _instant("signs_from")
    if created_at is None or signs_from is None:  # pragma: no cover -- _instant raises first
        raise ValueError("a key entry has no created_at or signs_from")
    return ClaimKey(
        kid=kid, x=x, seed=seed_bytes,
        created_at=created_at, signs_from=signs_from, retired_at=_instant("retired_at", optional=True),
    )


class KeySet:
    """The claim signing keys in one file. See the module docstring."""

    def __init__(
        self, path: Path, *, overlap_seconds: int = 86400, clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._overlap = overlap_seconds
        self._clock = clock
        self._mutex = threading.RLock()
        self._keys: list[ClaimKey] | None = None
        self._signature: tuple[int, int, int] | None = None

    # -- file access ------------------------------------------------------ #

    def _stat_signature(self) -> tuple[int, int, int] | None:
        try:
            st = self._path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _read_file(self) -> list[ClaimKey]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != FILE_VERSION:
                raise ValueError("unsupported file version")
            raw_keys = data.get("keys")
            if not isinstance(raw_keys, list):
                raise ValueError("keys is not a list")
            return [_key_from_json(item) for item in raw_keys]
        except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
            raise KeySetError(f"The upstream claim key file could not be read: {exc}") from exc

    def _write_file(self, keys: list[ClaimKey]) -> None:
        ordered = sorted(keys, key=lambda k: (k.signs_from, k.created_at, k.kid))
        secure_files.atomic_write_json(
            self._path,
            {"version": FILE_VERSION, "keys": [_key_to_json(k) for k in ordered]},
            indent=2, sort_keys=True,
        )
        self._keys = ordered
        self._signature = self._stat_signature()

    def _pruned(self, keys: list[ClaimKey], now: float) -> list[ClaimKey]:
        kept: list[ClaimKey] = []
        for key in keys:
            if key.retired_at is not None:
                if key.retired_at + self._overlap <= now:
                    continue
                if key.retired_at <= now and key.seed is not None:
                    key = replace(key, seed=None)
            kept.append(key)
        return kept

    def _refresh(self, now: float) -> list[ClaimKey]:
        """The current set, re-read when the file changed. Persists a prune."""
        with self._mutex:
            signature = self._stat_signature()
            if self._keys is None:
                if signature is None:
                    raise KeySetError("The upstream claim key file does not exist yet.")
                loaded = self._read_file()
            elif signature is None or signature == self._signature:
                if signature is None:
                    logger.warning("The upstream claim key file is gone; keeping the last good key set.")
                loaded = None
            else:
                try:
                    loaded = self._read_file()
                except KeySetError as exc:
                    logger.warning("%s; keeping the last good key set.", exc)
                    self._signature = signature
                    loaded = None
            if loaded is not None:
                self._keys = loaded
                self._signature = signature
            if self._keys is None:  # pragma: no cover -- set just above
                raise KeySetError("The upstream claim key set is not loaded.")
            if self._pruned(self._keys, now) != self._keys:
                self._persist_prune(now)
            return self._keys

    def _persist_prune(self, now: float) -> None:
        try:
            self._mutate(lambda keys: keys, now)
        except (OSError, KeySetError) as exc:
            # Read-only data directory: serve the pruned set from memory.
            logger.warning("Could not write the pruned upstream claim key set: %s", exc)
            if self._keys is not None:
                self._keys = self._pruned(self._keys, now)

    def _mutate(
        self, change: Callable[[list[ClaimKey]], list[ClaimKey]], now: float, *, create: bool = False,
    ) -> list[ClaimKey]:
        """Re-read under the lock, apply ``change``, prune, write when different."""
        with self._mutex, _locked(self._path):
            if self._path.exists():
                keys = self._read_file()
            elif create:
                keys = []
            else:
                raise KeySetError("The upstream claim key file does not exist yet.")
            before = list(keys)
            keys = self._pruned(change(list(keys)), now)
            if keys != before or not self._path.exists():
                self._write_file(keys)
            else:
                self._keys = keys
                self._signature = self._stat_signature()
            return keys

    # -- public API ------------------------------------------------------- #

    def ensure(self) -> None:
        """Create the first key when the file is missing, and prune."""
        now = self._clock()

        def _add_first(keys: list[ClaimKey]) -> list[ClaimKey]:
            return keys or [_new_key(now, now)]

        self._mutate(_add_first, now, create=True)

    def signing_key(self, now: float | None = None) -> ClaimKey:
        now = self._clock() if now is None else now
        keys = self._refresh(now)
        usable = [
            k for k in keys
            if k.signs_from <= now and k.seed is not None and (k.retired_at is None or k.retired_at > now)
        ]
        if not usable:
            raise KeySetError("No upstream claim signing key is usable.")
        return max(usable, key=lambda k: (k.signs_from, k.created_at))

    def sign(self, payload: dict[str, Any], now: float | None = None) -> str:
        key = self.signing_key(now)
        if key.seed is None:  # pragma: no cover -- signing_key only returns keys with a seed
            raise KeySetError("No upstream claim signing key is usable.")
        private = Ed25519PrivateKey.from_private_bytes(key.seed)
        return jwt.encode(payload, private, algorithm="EdDSA", headers={"kid": key.kid, "typ": CLAIM_TYPE})

    def jwks(self, now: float | None = None) -> dict[str, Any]:
        now = self._clock() if now is None else now
        published = [
            k for k in self._refresh(now)
            if k.retired_at is None or k.retired_at + self._overlap > now
        ]
        return {
            "keys": [
                {"kty": "OKP", "crv": "Ed25519", "x": k.x, "use": "sig", "alg": "EdDSA", "kid": k.kid}
                for k in sorted(published, key=lambda k: (k.signs_from, k.kid))
            ],
        }

    def rotate(self, now: float | None = None) -> ClaimKey:
        """Add a key that starts signing ``JWKS_CACHE_SECONDS`` from now."""
        now = self._clock() if now is None else now
        new = _new_key(now, now + JWKS_CACHE_SECONDS)

        def _add(keys: list[ClaimKey]) -> list[ClaimKey]:
            retired = [
                replace(k, retired_at=new.signs_from) if k.retired_at is None else k for k in keys
            ]
            return [*retired, new]

        self._mutate(_add, now)
        return new

    def revoke(self, kid: str, now: float | None = None) -> bool:
        """Remove ``kid`` from the set at once. False when there is no such key."""
        now = self._clock() if now is None else now
        found = False

        def _remove(keys: list[ClaimKey]) -> list[ClaimKey]:
            nonlocal found
            target = next((k for k in keys if k.kid == kid), None)
            if target is None:
                return keys
            found = True
            remaining = [k for k in keys if k.kid != kid]
            can_sign = [
                k for k in remaining
                if k.seed is not None and k.signs_from <= now and (k.retired_at is None or k.retired_at > now)
            ]
            if not can_sign:
                raise KeySetError(f"Key {kid} is the only key that can sign; rotate first and wait.")
            # A key that was waiting to hand over to the revoked one keeps signing.
            return [
                replace(k, retired_at=None)
                if k.retired_at is not None and k.retired_at > now
                and not any(o.signs_from > k.signs_from for o in remaining)
                else k
                for k in remaining
            ]

        self._mutate(_remove, now)
        return found

    def prune(self, now: float | None = None) -> None:
        """Drop seeds of retired keys and keys past their overlap, and persist."""
        now = self._clock() if now is None else now
        self._mutate(lambda keys: keys, now)
