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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from . import auto_accept, paths, secure_files
from .principal import Principal

if TYPE_CHECKING:
    from .policy.scope_rows import V2Migration

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "pfst_"  # nosec B105  # a public prefix, not a credential value
DEFAULT_DAYS = 90
LOCAL_MAX_DAYS = 365
WARN_DAYS = 14
MAX_TOKENS_PER_PRINCIPAL = 50
LABEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}\Z")
STORE_FILE_NAME = "scoped_tokens.json"
ACTIVITY_FILE_NAME = "scoped_token_activity.json"
# A runner looping on calls writes the activity file at most this often per person.
USE_WRITE_INTERVAL_SECONDS = 60.0
_LABEL_MESSAGE = (
    "A token name is 1 to 60 letters, digits, spaces, dots, dashes or underscores, "
    "starting with a letter or digit."
)

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
    # Per rule id the token carries: the (predicate, operation) pairs that may auto-accept under it
    # for this token. A rule id with no entry is used whole (a token never upgraded).
    rule_limits: dict[str, frozenset[tuple[str, str]]] = field(default_factory=dict, hash=False)

    def days_left(self, now: float) -> int:
        return max(0, math.ceil((self.expires_at - now) / _SECONDS_PER_DAY))

    def expired(self, now: float) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True)
class TokenActivity:
    """When a token last made an allowed call and when it was last refused (epoch seconds)."""

    last_used_at: float | None = None
    last_denied_at: float | None = None
    last_denied_tool: str = ""


def _activity_to_json(activity: TokenActivity) -> dict[str, Any]:
    return {
        "last_used_at": activity.last_used_at,
        "last_denied_at": activity.last_denied_at,
        "last_denied_tool": activity.last_denied_tool,
    }


def _activity_from_json(raw: Any) -> TokenActivity:
    used, denied, tool = raw["last_used_at"], raw["last_denied_at"], raw["last_denied_tool"]
    for stamp in (used, denied):
        if stamp is not None and (isinstance(stamp, bool) or not isinstance(stamp, (int, float))):
            raise ValueError("not a timestamp")
    if not isinstance(tool, str):
        raise ValueError("not a tool name")
    return TokenActivity(
        last_used_at=None if used is None else float(used),
        last_denied_at=None if denied is None else float(denied),
        last_denied_tool=tool,
    )


def _merge_activity(a: TokenActivity, b: TokenActivity) -> TokenActivity:
    """The newer ``last_used_at`` of the two; ``last_denied_at`` and its tool from the same side."""
    used = [t for t in (a.last_used_at, b.last_used_at) if t is not None]
    denied_side = b if (b.last_denied_at or 0.0) > (a.last_denied_at or 0.0) else a
    return TokenActivity(
        last_used_at=max(used) if used else None,
        last_denied_at=denied_side.last_denied_at,
        last_denied_tool=denied_side.last_denied_tool,
    )


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
    if token.rule_limits:
        data["rule_limits"] = {rid: sorted([p, o] for p, o in pairs) for rid, pairs in sorted(token.rule_limits.items())}
    return data


def _limits_from_json(raw: Any) -> dict[str, frozenset[tuple[str, str]]]:
    """Strict read: anything but ``{row_id: [[predicate, operation], ...]}`` raises, so the entry is skipped."""
    if not isinstance(raw, dict):
        raise TypeError("rule_limits must be an object")
    limits: dict[str, frozenset[tuple[str, str]]] = {}
    for rid, pairs in raw.items():
        if not isinstance(rid, str) or not isinstance(pairs, list):
            raise TypeError("rule_limits entries must map a rule id to a list")
        parsed = set()
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(x, str) for x in pair):
                raise TypeError("a rule limit is a [predicate, operation] pair")
            parsed.add((pair[0], pair[1]))
        limits[rid] = frozenset(parsed)
    return limits


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
        rule_limits=_limits_from_json(raw.get("rule_limits", {})),
    )


def _migrated(token: ScopedToken, migration: "V2Migration") -> tuple[ScopedToken, bool]:
    """``token`` re-pointed at the migration's rows, and whether its personal-data flag was newly
    set. Returns ``token`` itself when nothing needs changing, so a second run is a no-op."""
    rule_ids = set(token.rule_ids)
    limits = dict(token.rule_limits)
    flag = token.allow_personal_data
    newly_flagged = not flag and bool(token.rule_ids & migration.personal_data_ids)
    if newly_flagged:
        flag = True
    for row_id, members in migration.members.items():
        carried = members & token.rule_ids
        if not carried:
            continue
        rule_ids -= carried
        rule_ids.add(row_id)
        for old in carried:
            limits.pop(old, None)
        limits[row_id] = frozenset().union(*(migration.member_pairs.get(m, frozenset()) for m in carried))
    for rid in rule_ids:
        # A custom row keeps its old id, and so is in member_pairs but not id_map.
        if rid not in limits and rid not in migration.id_map and rid in migration.member_pairs:
            limits[rid] = migration.member_pairs[rid]
    result = replace(token, rule_ids=frozenset(rule_ids), rule_limits=limits, allow_personal_data=flag)
    return (token, False) if result == token else (result, newly_flagged)


def migrate_token_file(path: Path, migration: "V2Migration") -> list[str]:
    """Re-point the tokens in ``path`` at the migration's rows; return the labels of tokens whose
    ``allow_personal_data`` this set. Idempotent; a missing or unreadable file is left alone.
    Entries that cannot be read are kept as they are. Raises ``OSError`` if the write fails."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = list(data["tokens"])
    except (OSError, ValueError, KeyError, TypeError):
        return []
    labels: list[str] = []
    changed = False
    out: list[Any] = []
    for raw in entries:
        try:
            token = _from_json("", raw)
        except (KeyError, TypeError, ValueError):
            out.append(raw)
            continue
        new, newly_flagged = _migrated(token, migration)
        if new is token:
            out.append(raw)
            continue
        changed = True
        out.append(_to_json(new))
        if newly_flagged:
            labels.append(new.label)
    if changed:
        secure_files.atomic_write_json(path, {"version": 1, "tokens": out})
    return labels


def _without_rules(token: ScopedToken, rule_ids: frozenset[str]) -> ScopedToken:
    """``token`` minus ``rule_ids`` and their limits; ``token`` itself when it carries none of them.
    A token left with no rules and no tools stays: it is not revoked."""
    if not (token.rule_ids & rule_ids) and not any(k in rule_ids for k in token.rule_limits):
        return token
    return replace(
        token,
        rule_ids=token.rule_ids - rule_ids,
        rule_limits={k: v for k, v in token.rule_limits.items() if k not in rule_ids},
    )


def forget_rules_in_file(path: Path, rule_ids: Iterable[str]) -> list[str]:
    """Drop ``rule_ids`` from every token in ``path``; return the labels of the tokens that changed.
    A missing or unreadable file is left alone, and entries that cannot be read are kept as they
    are. Raises ``OSError`` if the write fails."""
    gone = frozenset(rule_ids)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = list(data["tokens"])
    except (OSError, ValueError, KeyError, TypeError):
        return []
    labels: list[str] = []
    out: list[Any] = []
    for raw in entries:
        try:
            token = _from_json("", raw)
        except (KeyError, TypeError, ValueError):
            out.append(raw)
            continue
        new = _without_rules(token, gone)
        if new is token:
            out.append(raw)
            continue
        out.append(_to_json(new))
        labels.append(new.label)
    if labels:
        secure_files.atomic_write_json(path, {"version": 1, "tokens": out})
    return labels


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
        # Last use and last denial (ADR 0206): kept apart from the token file, which create, rename
        # and revoke rewrite, and written at most once per USE_WRITE_INTERVAL_SECONDS per principal.
        self._activity: dict[str, dict[str, TokenActivity]] = {}
        self._activity_written: dict[str, float] = {}
        self._activity_loaded: set[str] = set()

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
            raise ScopedTokenError(_LABEL_MESSAGE)
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
            compiled = auto_accept.get_policy_v2_store_rules()
            known_rules = {rule.id for rule in compiled}
            if not rules <= known_rules:
                raise ScopedTokenError("One of the chosen rules no longer exists; reload the page.")
            if len(existing) >= MAX_TOKENS_PER_PRINCIPAL:
                raise ScopedTokenError(f"You have {MAX_TOKENS_PER_PRINCIPAL} tokens already; revoke one first.")
            limits = {
                rid: frozenset((r.predicate, op) for r in compiled if r.id == rid for op in r.operations)
                for rid in rules
            }
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
                rule_limits=limits,
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

    def rename(self, principal: Principal, token_id: str, label: str) -> tuple[str, ScopedToken] | None:
        """Change only the token's name; the secret, digest, id, expiry and rules stay as they are."""
        if not LABEL_RE.match(label):
            raise ScopedTokenError(_LABEL_MESSAGE)
        with self._lock:
            self._refresh(principal)
            tokens = self._tokens[principal.id]
            position = next((i for i, t in enumerate(tokens) if t.id == token_id), None)
            if position is None:
                return None
            token = tokens[position]
            if token.label == label:
                return label, token
            if any(t.label == label for t in tokens):
                raise ScopedTokenError("You already have a token with that name.")
            renamed = replace(token, label=label)
            tokens[position] = renamed
            self._write(principal)
            return token.label, renamed

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

    def apply_migration(self, principal: Principal, migration: "V2Migration") -> list[str]:
        """``migrate_token_file`` for a live store: under the lock, on the freshly read tokens."""
        with self._lock:
            self._refresh(principal)
            labels: list[str] = []
            changed = False
            tokens: list[ScopedToken] = []
            for token in self._tokens[principal.id]:
                new, newly_flagged = _migrated(token, migration)
                changed = changed or new is not token
                tokens.append(new)
                if newly_flagged:
                    labels.append(new.label)
            if changed:
                self._tokens[principal.id] = tokens
                self._write(principal)
            return labels

    def forget_rules(self, principal: Principal, rule_ids: Iterable[str]) -> list[str]:
        """``forget_rules_in_file`` for a live store: under the lock, on the freshly read tokens."""
        gone = frozenset(rule_ids)
        with self._lock:
            self._refresh(principal)
            labels: list[str] = []
            tokens: list[ScopedToken] = []
            for token in self._tokens[principal.id]:
                new = _without_rules(token, gone)
                if new is not token:
                    labels.append(new.label)
                tokens.append(new)
            if labels:
                self._tokens[principal.id] = tokens
                self._write(principal)
            return labels

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

    def _activity_path(self, principal: Principal) -> Path:
        return paths.authority_dir(principal) / ACTIVITY_FILE_NAME

    def _load_activity(self, principal: Principal) -> None:
        """Merge a principal's activity file into memory, once since start-up. The lock is held."""
        if principal.id in self._activity_loaded:
            return
        self._activity_loaded.add(principal.id)
        memory = self._activity.setdefault(principal.id, {})
        path = self._activity_path(principal)
        if not path.exists():
            return
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))["tokens"]
            stored = {str(token_id): _activity_from_json(raw) for token_id, raw in entries.items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            logger.warning("Skipping an unreadable scoped token activity file for %s", principal.id)
            return
        for token_id, activity in stored.items():
            known = memory.get(token_id)
            memory[token_id] = activity if known is None else _merge_activity(activity, known)

    def _record_activity(
        self, token: ScopedToken, update: Callable[[TokenActivity], TokenActivity], now: float,
    ) -> None:
        if token is REVOKED_SCOPE:
            return
        with self._lock:
            try:
                principal = self._principals.get(token.principal_id) or Principal(id=token.principal_id)
                self._load_activity(principal)
                memory = self._activity.setdefault(principal.id, {})
                memory[token.id] = update(memory.get(token.id, TokenActivity()))
                written = self._activity_written.get(principal.id)
                if written is not None and now - written <= USE_WRITE_INTERVAL_SECONDS:
                    return
                self._activity_written[principal.id] = now
                secure_files.atomic_write_json(
                    self._activity_path(principal),
                    {"version": 1, "tokens": {tid: _activity_to_json(a) for tid, a in memory.items()}},
                )
            except Exception:  # a failure to record activity must never break a call
                logger.warning("Could not record scoped token activity for %s", token.id, exc_info=True)

    def note_use(self, token: ScopedToken, now: float | None = None) -> None:
        """Record an allowed call by ``token``. In memory; the file follows at most once a minute."""
        stamp = time.time() if now is None else now
        self._record_activity(token, lambda a: replace(a, last_used_at=stamp), stamp)

    def note_denial(self, token: ScopedToken, tool: str, now: float | None = None) -> None:
        """Record a refused call by ``token``: the time and the tool travel together."""
        stamp = time.time() if now is None else now
        self._record_activity(token, lambda a: replace(a, last_denied_at=stamp, last_denied_tool=tool), stamp)

    def activity_for(self, principal: Principal) -> dict[str, TokenActivity]:
        """The activity of ``principal``'s current tokens (revoked tokens' entries are left out)."""
        with self._lock:
            self._refresh(principal)
            self._load_activity(principal)
            memory = self._activity.get(principal.id, {})
            return {t.id: memory[t.id] for t in self._tokens[principal.id] if t.id in memory}

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


def current_store() -> ScopedTokenStore | None:
    """The store set by ``set_store``, or ``None``; unlike ``get_store`` it never creates one."""
    return _STORE


def migrate_tokens(principal: Principal, migration: "V2Migration") -> list[str]:
    """Re-point ``principal``'s tokens through the live store when one is set (so its lock and
    in-memory copy stay consistent), else straight in the file."""
    store = current_store()
    if store is not None:
        return store.apply_migration(principal, migration)
    return migrate_token_file(paths.authority_dir(principal) / STORE_FILE_NAME, migration)


def forget_rules(principal: Principal, rule_ids: Iterable[str]) -> list[str]:
    """Drop deleted rules from ``principal``'s tokens through the live store when one is set, else
    straight in the file. Returns the labels of the tokens that changed."""
    gone = frozenset(rule_ids)
    if not gone:
        return []
    store = current_store()
    if store is not None:
        return store.forget_rules(principal, gone)
    return forget_rules_in_file(paths.authority_dir(principal) / STORE_FILE_NAME, gone)


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
