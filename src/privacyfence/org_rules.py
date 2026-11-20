"""Install-wide auto-accept rules set by an administrator (issue 1167).

Unlike a person's rules (per principal, ``auto_accept._REGISTRY``), these are one process-wide
list read by every request. They live in ``org/admin_config.json`` under ``org_rules`` as
``scope_rows`` rows; this module holds the loaded rows, their compiled rules and the change
listeners. ``scope_rows`` and ``admin_config`` are imported inside functions: ``admin_config``
imports ``scoped_tokens``, which imports this module.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .policy.engine import PolicyRule
    from .policy.scope_rows import ScopeRow

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_rows: list[ScopeRow] = []
_compiled: list[PolicyRule] | None = None
_listeners: list[Callable[[], None]] = []


def rows() -> list[ScopeRow]:
    """The organization's rule rows (a copy)."""
    with _lock:
        return list(_rows)


def rules() -> list[PolicyRule]:
    """The compiled rules, the same objects until the next change."""
    global _compiled
    from .policy import scope_rows

    with _lock:
        if _compiled is None:
            _compiled = scope_rows.compile_rows(_rows)
        return list(_compiled)


def set(new_rows: list[ScopeRow]) -> None:  # noqa: A001 - the module's public name
    """Replace the rows and drop the compiled cache. Does not notify."""
    global _rows, _compiled
    with _lock:
        _rows = list(new_rows)
        _compiled = None


def _valid_rows(admin: Any) -> list[ScopeRow]:
    """The storable rows of an admin config's ``org_rules``; anything else is dropped with a warning."""
    from . import admin_config
    from .policy import scope_rows
    from .policy.scopes import TOKEN_ONLY_PREDICATES

    raw = admin.get("org_rules") if isinstance(admin, dict) else None
    if not isinstance(raw, list):
        return []
    kept: list[ScopeRow] = []
    for item in raw:
        row = scope_rows.row_from_dict(item)
        if row is None:
            logger.warning("Dropped an organization rule that does not parse.")
        elif row.scope is None:
            logger.warning("Dropped organization rule %s: a custom rule cannot be an organization rule.", row.id)
        elif row.predicate in TOKEN_ONLY_PREDICATES:
            logger.warning("Dropped organization rule %s: its predicate is for scoped tokens only.", row.id)
        elif len(kept) >= admin_config.MAX_ORG_RULES:
            logger.warning("Dropped organization rule %s: more than %d rules.", row.id, admin_config.MAX_ORG_RULES)
        else:
            kept.append(row)
    return kept


def _load(admin: Any) -> None:
    from .policy import scope_rows

    loaded = _valid_rows(admin)
    set(loaded)
    digest = hashlib.sha256(
        json.dumps([scope_rows.row_to_dict(r) for r in loaded], sort_keys=True).encode()
    ).hexdigest()[:12]
    logger.info("Loaded %d organization auto-accept rules (%s).", len(loaded), digest)


def load_from_config(admin: dict[str, Any]) -> None:
    """Load the rows from an admin config at startup, without notifying."""
    _load(admin)


def reload_from_admin_config() -> None:
    """Read the admin config file, replace the rows, then notify the listeners."""
    from . import admin_config

    _load(admin_config.load())
    notify_changed()


def add_changed_listener(callback: Callable[[], None]) -> None:
    """Register ``callback`` (idempotent)."""
    with _lock:
        if callback not in _listeners:
            _listeners.append(callback)


def remove_changed_listener(callback: Callable[[], None]) -> None:
    with _lock:
        if callback in _listeners:
            _listeners.remove(callback)


def notify_changed() -> None:
    """Call every listener, outside the lock; one that raises does not stop the rest."""
    with _lock:
        callbacks = list(_listeners)
    for callback in callbacks:
        try:
            callback()
        except Exception:
            logger.exception("An organization-rules listener failed.")


def reset() -> None:
    """Forget the rows and the listeners (tests)."""
    global _rows, _compiled
    with _lock:
        _rows = []
        _compiled = None
        _listeners.clear()
