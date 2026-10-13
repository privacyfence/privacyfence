"""Scoped AI client tokens (ADR 0164): named, expiring credentials that carry a
subset of the person's auto-accept rules and tools.

Only the sha256 of a token's secret is stored (ADR 0072's rule for secrets at
rest); the secret is returned once, by ``create``. The store has no web
imports so ``gate.py`` can read ``current_token_scope()``.

Each principal's tokens live in ``authority_dir(principal)/scoped_tokens.json``.
A malformed file or entry is skipped with a warning, never raised: the token
then fails closed. ``find`` re-reads a principal's file when it changed on
disk, so a revocation written by another process takes effect on the next call.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from . import auto_accept, paths, secure_files
from .principal import Principal

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "pfst_"  # nosec B105  # a public prefix, not a credential value
DEFAULT_DAYS = 90
LOCAL_MAX_DAYS = 365
WARN_DAYS = 14
MAX_TOKENS_PER_PRINCIPAL = 50
LABEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}\Z")
STORE_FILE_NAME = "scoped_tokens.json"

_SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class ScopedToken:
    id: str  # "st-" + 12 hex chars
    label: str
    digest: str  # sha256 hex of the full secret
    principal_id: str
    principal_email: str  # org mode, for the principal's claims; "" in local mode
    principal_display_name: str
    created_at: float
    expires_at: float
    rule_ids: frozenset[str]
    tools: frozenset[str]
    allow_personal_data: bool = False  # ADR 0173: exports, downloads and page uploads may carry personal data

    def days_left(self, now: float) -> int:
        return max(0, math.ceil((self.expires_at - now) / _SECONDS_PER_DAY))

    def expired(self, now: float) -> bool:
        return now >= self.expires_at


# A token whose record is gone (revoked between verification and use): it allows nothing,
# and never falls back to the person's full access.
REVOKED_SCOPE = ScopedToken(
    id="revoked",
    label="revoked token",
    digest="",
    principal_id="",
    principal_email="",
    principal_display_name="",
    created_at=0,
    expires_at=0,
    rule_ids=frozenset(),
    tools=frozenset(),
)


@dataclass(frozen=True)
class ScopedTokenPolicy:
    enabled: bool
    max_days: int


class ScopedTokenError(ValueError):
    """Shown to the user verbatim; never carries a secret."""


def mint_secret() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def policy_from_org_config(org_config: dict, *, org_mode: bool) -> ScopedTokenPolicy:
    if not org_mode:
        return ScopedTokenPolicy(True, LOCAL_MAX_DAYS)
    section = org_config.get("scoped_tokens")
    if not isinstance(section, dict):
        section = {}
    enabled = section.get("enabled") is True
    max_days = section.get("max_days", DEFAULT_DAYS)
    if isinstance(max_days, bool) or not isinstance(max_days, int) or not 1 <= max_days <= LOCAL_MAX_DAYS:
        logger.warning("scoped_tokens.max_days must be an integer from 1 to %d; using %d", LOCAL_MAX_DAYS, DEFAULT_DAYS)
        max_days = DEFAULT_DAYS
    return ScopedTokenPolicy(enabled, max_days)


def _to_json(token: ScopedToken) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": token.id,
        "label": token.label,
        "digest": token.digest,
        "principal_email": token.principal_email,
        "principal_display_name": token.principal_display_name,
        "created_at": token.created_at,
        "expires_at": token.expires_at,
        "rule_ids": sorted(token.rule_ids),
        "tools": sorted(token.tools),
    }
    if token.allow_personal_data:
        data["allow_personal_data"] = True
    return data


def _from_json(principal_id: str, raw: Any) -> ScopedToken:
    return ScopedToken(
        id=str(raw["id"]),
        label=str(raw["label"]),
        digest=str(raw["digest"]),
        principal_id=principal_id,
        principal_email=str(raw["principal_email"]),
        principal_display_name=str(raw["principal_display_name"]),
        created_at=float(raw["created_at"]),
        expires_at=float(raw["expires_at"]),
        rule_ids=frozenset(str(r) for r in raw["rule_ids"]),
        tools=frozenset(str(t) for t in raw["tools"]),
        allow_personal_data=raw.get("allow_personal_data", False) is True,
    )


# (st_mtime_ns, st_ino) of a principal's file; the atomic replace gives a new inode, so a
# same-second edit on a filesystem with coarse timestamps is still seen.
_Stamp = tuple[int, int] | None


class ScopedTokenStore:
    def __init__(self, principal_dirs: Callable[[], Iterable[Principal]]) -> None:
        self._principal_dirs = principal_dirs
        self._lock = threading.Lock()
        self._principals: dict[str, Principal] = {}
        self._tokens: dict[str, list[ScopedToken]] = {}
        self._stamps: dict[str, _Stamp] = {}
        self._index: dict[str, tuple[str, str]] = {}  # digest -> (principal_id, token_id)

    def _path(self, principal: Principal) -> Path:
        return paths.authority_dir(principal) / STORE_FILE_NAME

    def _stamp(self, principal: Principal) -> _Stamp:
        try:
            st = self._path(principal).stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_ino)

    def _reload(self, principal: Principal) -> None:
        """Re-read one principal's file into memory. The lock is held."""
        stamp = self._stamp(principal)
        tokens: list[ScopedToken] = []
        if stamp is not None:
            try:
                data = json.loads(self._path(principal).read_text(encoding="utf-8"))
                entries = data["tokens"]
                for raw in entries:
                    try:
                        tokens.append(_from_json(principal.id, raw))
                    except (KeyError, TypeError, ValueError):
                        logger.warning("Skipping a malformed scoped token entry for %s", principal.id)
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning("Skipping an unreadable scoped token file for %s", principal.id)
                tokens = []
        for old in self._tokens.get(principal.id, []):
            self._index.pop(old.digest, None)
        self._principals[principal.id] = principal
        self._tokens[principal.id] = tokens
        self._stamps[principal.id] = stamp
        for token in tokens:
            self._index[token.digest] = (principal.id, token.id)

    def _refresh(self, principal: Principal) -> None:
        """Reload when the file changed since it was last read. The lock is held."""
        if principal.id not in self._stamps or self._stamps[principal.id] != self._stamp(principal):
            self._reload(principal)

    def _write(self, principal: Principal) -> None:
        secure_files.atomic_write_json(
            self._path(principal),
            {"version": 1, "tokens": [_to_json(t) for t in self._tokens[principal.id]]},
        )
        self._stamps[principal.id] = self._stamp(principal)

    def load(self) -> None:
        with self._lock:
            for principal in self._principal_dirs():
                self._reload(principal)

    def tokens_for(self, principal: Principal) -> list[ScopedToken]:
        with self._lock:
            self._refresh(principal)
            return list(self._tokens[principal.id])

    def create(
        self,
        principal: Principal,
        *,
        label: str,
        days: int,
        rule_ids: Iterable[str],
        tools: Iterable[str],
        policy: ScopedTokenPolicy,
        allow_personal_data: bool = False,
        now: float | None = None,
    ) -> tuple[ScopedToken, str]:
        if not policy.enabled:
            raise ScopedTokenError(
                "Scoped tokens are not enabled on this install. "
                "Your administrator turns them on in the organization config bundle."
            )
        if not LABEL_RE.match(label):
            raise ScopedTokenError(
                "A token name is 1 to 60 letters, digits, spaces, dots, dashes or underscores, "
                "starting with a letter or digit."
            )
        rules = frozenset(rule_ids)
        tool_set = frozenset(tools)
        with self._lock:
            self._refresh(principal)
            existing = self._tokens[principal.id]
            if any(t.label == label for t in existing):
                raise ScopedTokenError("You already have a token with that name.")
            if not 1 <= days <= policy.max_days:
                raise ScopedTokenError(f"A token lasts 1 to {policy.max_days} days.")
            if not rules and not tool_set:
                raise ScopedTokenError("Choose at least one tool for the token.")
            for name in sorted(tool_set):
                if name not in auto_accept.TOOL_TO_GATE:
                    raise ScopedTokenError(f"{name} is not a PrivacyFence tool.")
            known_rules = {rule.id for rule in auto_accept.get_policy_v2_store_rules()}
            if not rules <= known_rules:
                raise ScopedTokenError("One of the chosen rules no longer exists; reload the page.")
            if len(existing) >= MAX_TOKENS_PER_PRINCIPAL:
                raise ScopedTokenError(f"You have {MAX_TOKENS_PER_PRINCIPAL} tokens already; revoke one first.")
            created = time.time() if now is None else now
            secret = mint_secret()
            token = ScopedToken(
                id="st-" + secrets.token_hex(6),
                label=label,
                digest=_digest(secret),
                principal_id=principal.id,
                principal_email=principal.email,
                principal_display_name=principal.display_name,
                created_at=created,
                expires_at=created + days * _SECONDS_PER_DAY,
                rule_ids=rules,
                tools=tool_set,
                allow_personal_data=allow_personal_data,
            )
            self._tokens[principal.id] = [*existing, token]
            self._write(principal)
            self._index[token.digest] = (principal.id, token.id)
            return token, secret

    def revoke(self, principal: Principal, token_id: str) -> bool:
        with self._lock:
            self._refresh(principal)
            kept = [t for t in self._tokens[principal.id] if t.id != token_id]
            if len(kept) == len(self._tokens[principal.id]):
                return False
            for gone in self._tokens[principal.id]:
                if gone.id == token_id:
                    self._index.pop(gone.digest, None)
            self._tokens[principal.id] = kept
            self._write(principal)
            return True

    def revoke_all(self, principal_id: str) -> int:
        with self._lock:
            principal = self._principals.get(principal_id) or Principal(id=principal_id)
            try:
                self._refresh(principal)
            except ValueError:  # an id that cannot name a directory has no tokens
                return 0
            removed = self._tokens[principal_id]
            if not removed:
                return 0
            for gone in removed:
                self._index.pop(gone.digest, None)
            self._tokens[principal_id] = []
            self._write(principal)
            return len(removed)

    def find(self, secret: str, now: float | None = None) -> ScopedToken | None:
        if not secret.startswith(TOKEN_PREFIX):
            return None
        with self._lock:
            hit = self._index.get(_digest(secret))
            if hit is None:
                return None
            principal = self._principals[hit[0]]
            self._refresh(principal)
            hit = self._index.get(_digest(secret))
            if hit is None:
                return None
            token = next((t for t in self._tokens[hit[0]] if t.id == hit[1]), None)
            current = time.time() if now is None else now
            if token is None or token.expired(current):
                return None
            return token

    def get(self, principal_id: str, token_id: str) -> ScopedToken | None:
        with self._lock:
            principal = self._principals.get(principal_id)
            if principal is None:
                return None
            self._refresh(principal)
            return next((t for t in self._tokens[principal_id] if t.id == token_id), None)


_STORE: ScopedTokenStore | None = None


def get_store() -> ScopedTokenStore:
    global _STORE
    if _STORE is None:
        _STORE = ScopedTokenStore(lambda: [])
    return _STORE


def set_store(store: ScopedTokenStore | None) -> None:
    global _STORE
    _STORE = store


_scope_ctx: contextvars.ContextVar[ScopedToken | None] = contextvars.ContextVar(
    "privacyfence_scoped_token", default=None
)


def current_token_scope() -> ScopedToken | None:
    return _scope_ctx.get()


class token_scope:  # noqa: N801 (context-manager-style name, like gate.unattended_scope)
    """Run the wrapped code as the call of ``token`` (``None`` for an unscoped call)."""

    def __init__(self, token: ScopedToken | None) -> None:
        self._token = token
        self._reset: contextvars.Token | None = None

    def __enter__(self) -> "token_scope":
        self._reset = _scope_ctx.set(self._token)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._reset is not None:
            _scope_ctx.reset(self._reset)
