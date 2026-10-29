"""Scoped AI client tokens (ADR 0164, ADR 0236): named, expiring credentials that carry their own
auto-accept rules and a set of tools.

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
from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence

from . import auto_accept, paths, secure_files
from .principal import Principal

if TYPE_CHECKING:
    from .policy.engine import PolicyRule
    from .policy.scope_rows import ScopeRow

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "pfst_"  # nosec B105  # a public prefix, not a credential value
DEFAULT_DAYS = 90
LOCAL_MAX_DAYS = 365
WARN_DAYS = 14
MAX_TOKENS_PER_PRINCIPAL = 50
MAX_RULES_PER_TOKEN = 50
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

# A token's limit records (predicate, operation) pairs as they compiled when the token was made.
# A token made before a scope split one predicate per verb (ADR 0228) recorded the old pair for
# both verbs, so for such a token -- and only such a token -- the old pair stands for both.
_LIMIT_PAIR_SUCCESSORS: dict[tuple[str, str], tuple[str, str]] = {
    ("slack.mentions_of_me", "slack.read_messages"):
        ("slack.mentions_of_me_all_results", "slack.read_messages"),
}


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
    tools: frozenset[str]
    allow_personal_data: bool = False  # ADR 0173: exports, downloads and page uploads may carry personal data
    allow_personal_data_reads: bool = False  # Gmail message/thread and Slack reads may carry personal data
    # The token's own rules (ADR 0236), in the auto_accept: row format.
    rules: tuple["ScopeRow", ...] = field(default=(), hash=False)
    # The stored entry of a token written before tokens held their own rules, kept as read until it
    # is converted (``own_legacy_rules``); such a token has no rules meanwhile.
    legacy_raw: dict[str, Any] | None = field(default=None, compare=False, hash=False, repr=False)

    # Compiled once per instance; ``dataclasses.replace`` does not copy an ``init=False`` field, so a
    # token with other rules starts with no cache.
    _compiled: list["PolicyRule"] | None = field(default=None, init=False, compare=False, hash=False, repr=False)

    def compiled_rules(self) -> list["PolicyRule"]:
        """The engine rules of the token's own rows (``scope_rows.compile_rows``), compiled once."""
        compiled = self._compiled
        if compiled is None:
            from .policy import scope_rows  # function-local: scope_rows reaches auto_accept (import cycle)

            compiled = scope_rows.compile_rows(self.rules)
            object.__setattr__(self, "_compiled", compiled)
        return compiled

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
    tools=frozenset(),
)


@dataclass(frozen=True)
class ScopedTokenPolicy:
    enabled: bool
    max_days: int
    allow_ai_proposals: bool = False


PROPOSALS_DISABLED_MESSAGE = (
    "Token proposals are not enabled on this install. "
    "Your administrator turns them on on the Admin page (Scoped tokens)."
)


class ScopedTokenError(ValueError):
    """Shown to the user verbatim; never carries a secret."""


def mint_secret() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def policy_from_org_config(org_config: dict, *, org_mode: bool) -> ScopedTokenPolicy:
    if not org_mode:
        return ScopedTokenPolicy(True, LOCAL_MAX_DAYS, True)
    section = org_config.get("scoped_tokens")
    if not isinstance(section, dict):
        section = {}
    enabled = section.get("enabled") is True
    max_days = section.get("max_days", DEFAULT_DAYS)
    if isinstance(max_days, bool) or not isinstance(max_days, int) or not 1 <= max_days <= LOCAL_MAX_DAYS:
        logger.warning("scoped_tokens.max_days must be an integer from 1 to %d; using %d", LOCAL_MAX_DAYS, DEFAULT_DAYS)
        max_days = DEFAULT_DAYS
    raw_proposals = section.get("allow_ai_proposals", False)
    if not isinstance(raw_proposals, bool):
        logger.warning("scoped_tokens.allow_ai_proposals must be true or false; using false")
        raw_proposals = False
    return ScopedTokenPolicy(enabled, max_days, raw_proposals and enabled)


def normalise_create_request(payload: dict[str, Any]) -> dict[str, Any] | None:
    """What a create-token step-up ceremony is bound to, or ``None`` for a
    malformed body. Shared by both modes' routes (ADR 0164, ADR 0165)."""
    label, days = payload.get("label"), payload.get("days")
    rules, tools = normalise_rule_requests(payload.get("rules")), payload.get("tools")
    if not isinstance(label, str) or isinstance(days, bool) or not isinstance(days, int):
        return None
    if rules is None or not isinstance(tools, list):
        return None
    if not all(isinstance(name, str) for name in tools):
        return None
    allow_personal_data = payload.get("allow_personal_data", False)
    if not isinstance(allow_personal_data, bool):
        return None
    allow_personal_data_reads = payload.get("allow_personal_data_reads", False)
    if not isinstance(allow_personal_data_reads, bool):
        return None
    return {
        "label": label, "days": days, "rules": rules, "tools": sorted(tools),
        "allow_personal_data": allow_personal_data, "allow_personal_data_reads": allow_personal_data_reads,
    }


def _to_json(token: ScopedToken) -> dict[str, Any]:
    from .policy import scope_rows  # function-local: scope_rows reaches auto_accept (import cycle)

    if token.legacy_raw is not None:
        # Kept as read (only the label can change), so the entry stays convertible at the next start.
        return {**token.legacy_raw, "label": token.label}
    data: dict[str, Any] = {
        "id": token.id,
        "label": token.label,
        "digest": token.digest,
        "principal_email": token.principal_email,
        "principal_display_name": token.principal_display_name,
        "created_at": token.created_at,
        "expires_at": token.expires_at,
        "tools": sorted(token.tools),
    }
    if token.allow_personal_data:
        data["allow_personal_data"] = True
    if token.allow_personal_data_reads:
        data["allow_personal_data_reads"] = True
    data["rules"] = [scope_rows.row_to_dict(row) for row in token.rules]
    return data


def _with_limit_successors(
    limits: dict[str, frozenset[tuple[str, str]]],
) -> dict[str, frozenset[tuple[str, str]]]:
    return {
        rid: pairs | {succ for pair, succ in _LIMIT_PAIR_SUCCESSORS.items() if pair in pairs}
        for rid, pairs in limits.items()
    }


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
    """A token from its stored entry. An entry with ``rules`` holds its own rows; a row that cannot
    be read is left out (fail-closed: it only narrows the token). A legacy entry (``rule_ids``, no
    ``rules``) loads with no rules and keeps its entry in ``legacy_raw`` until it is converted."""
    from .policy import scope_rows  # function-local: scope_rows reaches auto_accept (import cycle)

    rows: list[ScopeRow] = []
    legacy_raw: dict[str, Any] | None = None
    if "rules" in raw:
        if not isinstance(raw["rules"], list):
            raise TypeError("rules must be a list")
        for item in raw["rules"]:
            row = scope_rows.row_from_dict(item)
            if row is None:
                logger.warning("Scoped token %s: skipping a rule that cannot be read", raw["id"])
                continue
            rows.append(row)
    else:
        if not isinstance(raw["rule_ids"], list):
            raise TypeError("rule_ids must be a list")
        legacy_raw = dict(raw)
    return ScopedToken(
        id=str(raw["id"]),
        label=str(raw["label"]),
        digest=str(raw["digest"]),
        principal_id=principal_id,
        principal_email=str(raw["principal_email"]),
        principal_display_name=str(raw["principal_display_name"]),
        created_at=float(raw["created_at"]),
        expires_at=float(raw["expires_at"]),
        tools=frozenset(str(t) for t in raw["tools"]),
        allow_personal_data=raw.get("allow_personal_data", False) is True,
        allow_personal_data_reads=raw.get("allow_personal_data_reads", False) is True,
        rules=tuple(rows),
        legacy_raw=legacy_raw,
    )


_LEGACY_KEYS = ("rule_ids", "rule_limits", "limits_split_verbs")


@dataclass(frozen=True)
class OwnedEntry:
    entry: dict[str, Any]
    newly_flagged: bool  # allow_personal_data newly set (ADR 0173's transfer from a v2 rule)
    copied: int  # rule ids that named a rule and were copied
    missing: int  # rule ids that named no rule any more, or whose limit left nothing


def owned_entry(
    raw: Any, compiled: Sequence["PolicyRule"], personal_data_ids: frozenset[str] = frozenset(),
) -> OwnedEntry | None:
    """A legacy token entry (``rule_ids``, no ``rules``) with its own copy of each rule it named,
    narrowed to its ``rule_limits``; ``None`` for an entry that already holds its rules. Raises
    ``KeyError``, ``TypeError`` or ``ValueError`` for an entry that cannot be read."""
    from .policy import scope_rows  # function-local: scope_rows reaches auto_accept (import cycle)

    if not isinstance(raw, dict):
        raise TypeError("not a token entry")
    if "rules" in raw:
        return None
    ids = raw["rule_ids"]
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise TypeError("rule_ids must be a list of strings")
    limits = _limits_from_json(raw.get("rule_limits", {}))
    if raw.get("limits_split_verbs") is not True:
        limits = _with_limit_successors(limits)
    rows: list[Any] = []
    copied = missing = 0
    for rid in sorted(set(ids)):
        matching = [r for r in compiled if r.id == rid]
        limit = limits.get(rid)
        if limit is not None:
            matching = [
                replace(r, operations=frozenset(op for op in r.operations if (r.predicate, op) in limit))
                for r in matching
            ]
            matching = [r for r in matching if r.operations]
        if not matching:
            missing += 1
            continue
        rows.extend(scope_rows.rows_for_rules(matching, rid))
        copied += 1
    entry = {k: v for k, v in raw.items() if k not in _LEGACY_KEYS}
    entry["rules"] = [scope_rows.row_to_dict(row) for row in rows]
    newly_flagged = raw.get("allow_personal_data") is not True and bool(set(ids) & personal_data_ids)
    if newly_flagged:
        entry["allow_personal_data"] = True
    return OwnedEntry(entry, newly_flagged, copied, missing)


def own_rules_in_file(
    path: Path,
    compiled: Callable[[], Sequence["PolicyRule"]],
    personal_data_ids: frozenset[str] = frozenset(),
    *,
    log: Callable[[str], None] = logger.warning,
) -> list[str]:
    """Give each legacy entry in ``path`` its own copy of the rules it names (``owned_entry``);
    return the labels of entries whose ``allow_personal_data`` this set. ``compiled`` is called
    only when a legacy entry exists. Idempotent; a missing or unreadable file is left alone, and
    an entry that cannot be read is kept as it is. Raises ``OSError`` if the write fails."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = list(data["tokens"])
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if all(isinstance(raw, dict) and "rules" in raw for raw in entries):
        return []
    rules = compiled()
    labels: list[str] = []
    changed = False
    out: list[Any] = []
    for raw in entries:
        try:
            owned = owned_entry(raw, rules, personal_data_ids)
        except (KeyError, TypeError, ValueError):
            log(f"Keeping an unreadable scoped token entry as it is ({path})")
            out.append(raw)
            continue
        if owned is None:
            out.append(raw)
            continue
        changed = True
        out.append(owned.entry)
        label = owned.entry.get("label")
        log(
            f"Scoped token {label!r} now holds its own copy of {owned.copied} rule(s); "
            f"{owned.missing} rule(s) it named no longer existed"
        )
        if owned.newly_flagged:
            labels.append(str(label))
    if changed:
        secure_files.atomic_write_json(path, {"version": 2, "tokens": out})
    return labels


# (st_mtime_ns, st_ino) of a principal's file; the atomic replace gives a new inode, so a
# same-second edit on a filesystem with coarse timestamps is still seen.
_Stamp = tuple[int, int] | None


_RULE_REQUEST_SHAPES: dict[str, dict[str, type]] = {
    "new": {"group": str, "value": str, "verbs": list},
    "copy": {"rule_id": str},
    "duplicate": {"token_id": str, "rule_id": str},
}


def normalise_rule_requests(raw: Any) -> list[dict[str, Any]] | None:
    """The create body's ``rules`` in one canonical order, or ``None`` for a malformed list (ADR 0236).

    Each item is a ``new``, ``copy`` or ``duplicate`` request with exactly that kind's keys. Verbs
    are sorted, duplicate requests dropped, and the list sorted, so the same rules always give the
    same step-up fingerprint."""
    if not isinstance(raw, list):
        return None
    keyed: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            return None
        shape = _RULE_REQUEST_SHAPES.get(item.get("kind"))  # type: ignore[arg-type]
        if shape is None or set(item) != {"kind", *shape}:
            return None
        if not all(isinstance(item[key], kind) for key, kind in shape.items()):
            return None
        entry = dict(item)
        if item["kind"] == "new":
            if not all(isinstance(verb, str) for verb in item["verbs"]):
                return None
            entry["verbs"] = sorted(set(item["verbs"]))
        keyed[json.dumps(entry, sort_keys=True)] = entry
    return [keyed[key] for key in sorted(keyed)]


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
        for token in tokens:
            if token.legacy_raw is not None:
                logger.warning(
                    "Scoped token %s was created before tokens held their own rules; it allows no "
                    "rule-covered call until PrivacyFence converts it at the next start",
                    token.id,
                )
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
            {"version": 2, "tokens": [_to_json(t) for t in self._tokens[principal.id]]},
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
        rules: Sequence[dict[str, Any]],
        tools: Iterable[str],
        policy: ScopedTokenPolicy,
        allow_personal_data: bool = False,
        allow_personal_data_reads: bool = False,
        now: float | None = None,
    ) -> tuple[ScopedToken, str]:
        """Create a token holding its own rules, built from ``rules`` (``normalise_rule_requests``'s
        output): a ``new`` rule, a ``copy`` of one of the person's Auto-accept rows, or a
        ``duplicate`` of a row of one of their tokens (ADR 0236)."""
        from .policy import scope_rows

        tool_set = frozenset(tools)
        new_rules, own_rows = self._prepare(label=label, rules=rules, policy=policy)
        with self._lock:
            self._refresh(principal)
            existing = self._tokens[principal.id]
            base_rows = self._check_locked(
                existing, label=label, days=days, rules=rules, tool_set=tool_set,
                own_rows=own_rows, policy=policy,
            )
            token_rows, _ = scope_rows.add_rules(base_rows, new_rules)
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
                tools=tool_set,
                allow_personal_data=allow_personal_data,
                allow_personal_data_reads=allow_personal_data_reads,
                rules=tuple(token_rows),
            )
            self._tokens[principal.id] = [*existing, token]
            self._write(principal)
            self._index[token.digest] = (principal.id, token.id)
            return token, secret

    def check_request(
        self,
        principal: Principal,
        *,
        label: str,
        days: int,
        rules: Sequence[dict[str, Any]],
        tools: Iterable[str],
        policy: ScopedTokenPolicy,
        check_existing: bool = True,
    ) -> None:
        """Run ``create``'s validation and raise its ``ScopedTokenError``; writes nothing.
        ``check_existing=False`` skips the duplicate-label and token-limit checks."""
        _, own_rows = self._prepare(label=label, rules=rules, policy=policy)
        with self._lock:
            self._refresh(principal)
            self._check_locked(
                self._tokens[principal.id], label=label, days=days, rules=rules,
                tool_set=frozenset(tools), own_rows=own_rows, policy=policy, check_existing=check_existing,
            )

    def _prepare(
        self, *, label: str, rules: Sequence[dict[str, Any]], policy: ScopedTokenPolicy,
    ) -> tuple[list[PolicyRule], dict[str, ScopeRow]]:
        """No lock: the policy, label and rule-count checks, the compiled ``new`` rules (request
        order) and the person's Auto-accept rows a ``copy`` rule may name."""
        from .policy import catalogue
        from .policy.value_format import RuleValueError

        if not policy.enabled:
            raise ScopedTokenError(
                "Scoped tokens are not enabled on this install. "
                "Your administrator turns them on on the Admin page."
            )
        if not LABEL_RE.match(label):
            raise ScopedTokenError(_LABEL_MESSAGE)
        if len(rules) > MAX_RULES_PER_TOKEN:
            raise ScopedTokenError(f"A token has at most {MAX_RULES_PER_TOKEN} rules.")
        # Read before the store lock: get_policy_rows takes the Auto-accept write lock, and
        # ensure_scope_rows takes that lock and then this one.
        own_rows = (
            {row.id: row for row in auto_accept.get_policy_rows()}
            if any(entry["kind"] == "copy" for entry in rules) else {}
        )
        new_rules: list[PolicyRule] = []
        for entry in rules:
            if entry["kind"] != "new":
                continue
            try:
                value = catalogue.parse_value_list(entry["value"], entry["group"])
                built = catalogue.rules_for_catalogue_entry(
                    entry["group"], value, catalogue.parse_verbs(entry["verbs"]),
                )
            except RuleValueError as exc:
                raise ScopedTokenError(str(exc)) from exc
            takes_value = any(g["id"] == entry["group"] and g["needs_value"] for g in catalogue.scope_catalogue())
            if not built or (takes_value and value is None):
                raise ScopedTokenError(
                    "A new rule needs a scope, at least one of its verbs, and a value if the scope takes one."
                )
            new_rules.extend(built)
        return new_rules, own_rows

    def _check_locked(
        self,
        existing: list[ScopedToken],
        *,
        label: str,
        days: int,
        rules: Sequence[dict[str, Any]],
        tool_set: frozenset[str],
        own_rows: dict[str, ScopeRow],
        policy: ScopedTokenPolicy,
        check_existing: bool = True,
    ) -> list[ScopeRow]:
        """Caller holds ``self._lock``. Returns the rows the token takes over from ``copy`` and
        ``duplicate`` rules. ``check_existing=False`` skips the duplicate-label and 50-tokens checks."""
        if check_existing and any(t.label == label for t in existing):
            raise ScopedTokenError("You already have a token with that name.")
        if not 1 <= days <= policy.max_days:
            raise ScopedTokenError(f"A token lasts 1 to {policy.max_days} days.")
        if not rules and not tool_set:
            raise ScopedTokenError("Choose at least one tool for the token.")
        for name in sorted(tool_set):
            if name not in auto_accept.TOOL_TO_GATE:
                raise ScopedTokenError(f"{name} is not a PrivacyFence tool.")
        base_rows: list[ScopeRow] = []
        for entry in rules:
            if entry["kind"] == "new":
                continue
            if entry["kind"] == "copy":
                base = own_rows.get(entry["rule_id"])
            else:
                source = next((t for t in existing if t.id == entry["token_id"]), None)
                base = next((r for r in source.rules if r.id == entry["rule_id"]), None) if source else None
            if base is None:
                raise ScopedTokenError("One of the chosen rules no longer exists; reload the page.")
            if all(row.id != base.id for row in base_rows):
                base_rows.append(replace(base, former_ids=()))
        if check_existing and len(existing) >= MAX_TOKENS_PER_PRINCIPAL:
            raise ScopedTokenError(f"You have {MAX_TOKENS_PER_PRINCIPAL} tokens already; revoke one first.")
        return base_rows

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

    def own_legacy_rules(
        self,
        principal: Principal,
        compiled: Callable[[], Sequence["PolicyRule"]],
        personal_data_ids: frozenset[str] = frozenset(),
        *,
        log: Callable[[str], None] = logger.warning,
    ) -> list[str]:
        """``own_rules_in_file`` for a live store: under the lock, on the freshly read tokens."""
        with self._lock:
            self._refresh(principal)
            if not any(t.legacy_raw is not None for t in self._tokens[principal.id]):
                return []
            rules = compiled()
            labels: list[str] = []
            converted: list[tuple[ScopedToken, OwnedEntry]] = []
            tokens: list[ScopedToken] = []
            for token in self._tokens[principal.id]:
                if token.legacy_raw is None:
                    tokens.append(token)
                    continue
                try:
                    owned = owned_entry({**token.legacy_raw, "label": token.label}, rules, personal_data_ids)
                    if owned is None:  # pragma: no cover - a token with legacy_raw has no "rules" key
                        tokens.append(token)
                        continue
                    new = _from_json(principal.id, owned.entry)
                except (KeyError, TypeError, ValueError):
                    log(f"Keeping an unreadable scoped token entry as it is ({principal.id})")
                    tokens.append(token)
                    continue
                tokens.append(new)
                converted.append((new, owned))
            if not converted:
                return []
            self._tokens[principal.id] = tokens
            self._write(principal)
            for new, owned in converted:
                log(
                    f"Scoped token {new.label!r} now holds its own copy of {owned.copied} rule(s); "
                    f"{owned.missing} rule(s) it named no longer existed"
                )
                if owned.newly_flagged:
                    labels.append(new.label)
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


def own_legacy_rules(
    principal: Principal,
    compiled: Callable[[], Sequence["PolicyRule"]],
    personal_data_ids: frozenset[str] = frozenset(),
    *,
    log: Callable[[str], None] = logger.warning,
) -> list[str]:
    """Convert ``principal``'s legacy tokens through the live store when one is set (so its lock
    and in-memory copy stay consistent), else straight in the file. Raises ``ValueError`` for a
    principal id that cannot name a directory, and ``OSError`` when the write fails."""
    store = current_store()
    if store is not None:
        return store.own_legacy_rules(principal, compiled, personal_data_ids, log=log)
    return own_rules_in_file(
        paths.authority_dir(principal) / STORE_FILE_NAME, compiled, personal_data_ids, log=log,
    )


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
