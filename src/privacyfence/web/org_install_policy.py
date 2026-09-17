"""Applying an install-wide privacy/PII policy change from org mode's admin
settings surface (#400 C3e).

``web/routes_org_settings.py``'s first cut (#400 C3d) rendered this policy
read-only and said so on the page, because the issue left one thing
undecided: "Install-wide writes need a restart story. ``init_privacy_filter``
runs once at startup. Either the UI writes settings.yaml and says a restart is
required, or the privacy filter learns to reload." **This module is the
second answer.** A restart-required banner would have been smaller, but it
also would have shipped an editor whose edits do nothing until somebody with
shell access on the server restarts the daemon -- which is most of the
friction #400 exists to remove, kept and given a button.

Reloading turned out to be the smaller change anyway, because most of it
already existed and was simply never reachable from org mode:
``settings_controller._save_and_reload_privacy`` has re-run
``init_privacy_filter`` after every local-mode settings.yaml write since
long before this. What org mode adds is the two things that make that
correct for more than one user:

- **Every principal, not just the editor's.** ``privacy_filter._REGISTRY``
  and ``pii_detector._REGISTRY`` are ``PrincipalRegistry`` instances, so
  ``init_privacy_filter``/``init_pii_detection`` only ever write the entry
  of whoever is scoped when they're called. In local mode that is the only
  entry there is. In org mode it would be the admin's own -- every other
  signed-in principal would go on enforcing the policy loaded when their
  entry was first built, with the page cheerfully reporting the new one.
  ``privacy_filter.reload_for_all_principals`` /
  ``pii_detector.reload_for_all_principals`` are the fan-out versions.
- **``org_managed=True``.** ``settings_controller``'s own reload call passes
  neither ``org_managed`` nor anything else, so it takes
  ``init_privacy_filter``'s local-mode default of ``org_managed=False``.
  Reached from org mode, that single omission would swap org mode's
  fail-closed ``block`` default for a genuinely absent group back to
  ``allow`` -- an edit to one group silently widening what the filter lets
  through for every group nobody had configured. Every reload here is
  explicitly org-managed, and ``reload_for_all_principals`` defaults that
  argument the other way round for the same reason.

``settings`` throughout is the *live* install-wide config dict --
``daemon_main.run_app``'s own ``config``, carried on
``OrgAuth.install_wide_settings``. Updating it in place is not incidental:
it is the same object ``daemon_main._load_principal_settings`` reads when it
builds a *newly* signed-in principal's registry entries, so a principal who
arrives after an edit picks up the new policy with no sweep of their own,
and the admin page re-renders from the same object it just wrote.

A write that fails leaves nothing half-applied: the change is made on a copy of
settings.yaml as it currently stands *on disk*, persisted, and only adopted into
the live dict (and reloaded) once that write has actually landed.

Reading from disk rather than mutating a deep copy of the live ``settings`` dict is
deliberate, and fixes two related problems ``org-mode-setup-guide.md`` creates by
telling operators "an admin can still hand-edit it" (settings.yaml) alongside the
browser editor this module backs:

- **An admin's hand-edited comments used to vanish on the next browser save.**
  ``yaml.safe_dump``-ing the live dict has no memory of the comments the *file*
  carried, only the values ``init_privacy_filter`` parsed out of it at startup.
  Reading and rewriting through ``ruamel.yaml``'s round-trip loader/dumper
  (``_ROUND_TRIP_YAML`` above) instead keeps an operator's comments, key order and
  quoting intact across a browser edit, the same way a hand edit would.
- **A hand edit made on disk between two browser edits used to be silently
  discarded.** Basing the change on the live dict assumed nothing but this module
  itself had touched settings.yaml since the process read it at startup -- false
  precisely because the guide invites operators to hand-edit the same file. Basing
  it on a fresh read instead makes disk the one source of truth both paths agree
  on, so a hand edit either lands (if this call doesn't happen to touch the same
  key) or is the one that's superseded (if it does) -- never invisibly reverted by
  a change to some other field.

``_WRITE_LOCK`` closes the third gap the same underlying assumption left open: two
admins submitting around the same moment could each read, mutate their own copy and
write it back with no idea of each other, so whichever write lands second wins
outright rather than layering its change on top of the first's. The lock forces
that same read-modify-write-reload cycle to run start to finish for one submission
before the next one begins.
"""
from __future__ import annotations

import io
import logging
import threading
from typing import Any, Callable

import yaml
from ruamel.yaml import YAML

from .. import pii_detector, privacy_filter
from ..audit_log import compute_security_config_hash, set_security_config_hash_for_all_principals
from ..secure_files import atomic_write_text
from ..settings_controller import PRIVACY_CATEGORY_LABELS, PRIVACY_GROUP_LABELS

logger = logging.getLogger(__name__)

# Round-trip loader/dumper (comments, key order, quoting style) for the settings.yaml
# apply_change reads and rewrites -- see apply_change's own docstring for why a plain
# yaml.safe_load/safe_dump round trip isn't good enough here. Module-level and reused
# rather than constructed per call: a ``YAML()`` instance carries no per-document state,
# only parser/dumper configuration, so there's nothing wrong with sharing one.
_ROUND_TRIP_YAML = YAML()
_ROUND_TRIP_YAML.preserve_quotes = True
_ROUND_TRIP_YAML.width = 4096  # don't rewrap an operator's long lines differently than they wrote them

# Serializes the whole read-modify-write-reload cycle in apply_change, one process-wide
# lock rather than one per config_path: this module only ever has one install-wide
# settings.yaml live at a time (daemon_main starts one OrgAuth per process), so there's
# only ever one path to serialize. Without it, two admins submitting around the same
# moment each read the file, mutate their own copy and write it back -- the second
# write's copy was taken before the first's landed, so it silently overwrites the
# first admin's change instead of layering on top of it.
_WRITE_LOCK = threading.Lock()

# The action names this module can apply, deliberately the same strings
# `org_settings_scope.ADMIN_ONLY_ACTIONS` authorizes (#400 C3b/C3c) and
# `routes_settings._ALLOWED_ACTIONS` dispatches in local mode -- one name per
# concept across all three, so a route authorizes and applies the same
# string rather than translating between two vocabularies.
#
# `toggle_calendar_free_busy` and `set_log_level` are the two admin-only
# actions deliberately absent. Both are install-wide and both would belong
# here eventually, but neither is privacy/PII *policy* and each needs a
# reload path of its own that doesn't exist yet: the first rebuilds
# connectors (per principal, through `ConnectorRegistry`, not the single
# `refresh_connectors()` local mode calls), the second reconfigures
# process-wide logging. Adding either is additive -- a mutator below and a
# control on the page -- not a reshape of this module.
SUPPORTED_ACTIONS: frozenset[str] = frozenset({
    "set_default_policy", "set_category_policy",
    "toggle_pii_detection", "toggle_pii_category",
})


class PolicyChangeRejected(ValueError):
    """An install-wide policy change that names a group, category or policy
    value this install doesn't have.

    Raised, not silently ignored the way ``SettingsController.
    set_default_policy`` returns an unchanged snapshot for a bad policy
    value: that controller is driven by a native UI whose only inputs are
    the radio buttons it drew itself, while this one is driven by an HTTP
    form body. The route maps it to a 400 -- an admin who gets a rejection
    should see one, not a page that re-renders looking like their change
    was applied.
    """


def _require_group(group: str) -> None:
    if group not in PRIVACY_GROUP_LABELS:
        raise PolicyChangeRejected(f"unknown privacy group {group!r}")


def _require_policy(policy: str) -> None:
    if policy not in privacy_filter.VALID_POLICIES:
        raise PolicyChangeRejected(
            f"policy must be one of {', '.join(privacy_filter.VALID_POLICIES)}, got {policy!r}"
        )


def _group_section(settings: dict[str, Any], group: str) -> dict[str, Any]:
    """The group's own mapping in ``settings``, created empty if absent.

    The ``isinstance`` check is for a settings.yaml hand-edited *after* the
    daemon started: ``init_privacy_filter`` (SEC-07) refuses to start on a
    group that isn't a mapping, so it can't be there at boot, but this dict
    outlives boot. Without the check, ``settings.setdefault(group, {})[...]``
    on a string group raises ``TypeError`` and the route 500s.
    """
    _require_group(group)
    section = settings.setdefault(group, {})
    if not isinstance(section, dict):
        raise PolicyChangeRejected(f"settings.yaml's {group!r} section is not a mapping")
    return section


def _bool_field(payload: dict[str, Any], key: str) -> bool:
    """An explicit desired value, not a flip of whatever the server
    currently holds.

    Local mode's ``toggle_pii_detection``/``toggle_pii_category`` read the
    current value and invert it, which is right for a menu item that
    renders from live state at the moment of the click. A browser form can
    be submitted from a page rendered minutes ago, or twice; a flip would
    then land the *opposite* of what the admin saw themselves choosing. The
    action keeps its ``toggle_`` name (that name is the authorization key
    `org_settings_scope` gates on, shared with local mode), but the
    payload carries the value.
    """
    raw = payload.get(key)
    if raw in ("true", "1", "on", True):
        return True
    if raw in ("false", "0", "off", False):
        return False
    raise PolicyChangeRejected(f"{key} must be true or false, got {raw!r}")


def _set_default_policy(settings: dict[str, Any], payload: dict[str, Any]) -> str:
    group = str(payload.get("group", ""))
    policy = str(payload.get("policy", ""))
    _require_policy(policy)
    _group_section(settings, group)["default_policy"] = policy
    return f"Set {PRIVACY_GROUP_LABELS[group]} default privacy policy to {policy!r}"


def _set_category_policy(settings: dict[str, Any], payload: dict[str, Any]) -> str:
    group = str(payload.get("group", ""))
    category = str(payload.get("category", ""))
    policy = str(payload.get("policy", ""))
    _require_group(group)
    _require_policy(policy)
    if category not in PRIVACY_CATEGORY_LABELS.get(group, {}):
        raise PolicyChangeRejected(f"unknown category {category!r} for privacy group {group!r}")
    _group_section(settings, group).setdefault("categories", {})[category] = policy
    return (
        f"Set {PRIVACY_GROUP_LABELS[group]} / {PRIVACY_CATEGORY_LABELS[group][category]} "
        f"privacy policy to {policy!r}"
    )


def _toggle_pii_detection(settings: dict[str, Any], payload: dict[str, Any]) -> str:
    enabled = _bool_field(payload, "enabled")
    settings.setdefault("pii_detection", {})["enabled"] = enabled
    return f"{'Enabled' if enabled else 'Disabled'} install-wide PII detection"


def _toggle_pii_category(settings: dict[str, Any], payload: dict[str, Any]) -> str:
    category_key = str(payload.get("category_key", ""))
    if category_key not in pii_detector.optional_category_keys():
        raise PolicyChangeRejected(f"unknown PII category {category_key!r}")
    enabled = _bool_field(payload, "enabled")
    pii_cfg = settings.setdefault("pii_detection", {})
    if not pii_cfg.get("enabled", True):
        # Same guard local mode's own toggle_pii_category keeps (there, the
        # submenu item is greyed out without a callback): the optional
        # categories are meaningless while the master switch is off, and
        # writing one anyway would persist a value the running filter is
        # not using.
        raise PolicyChangeRejected("enable PII detection before changing an individual category")
    pii_cfg[category_key] = enabled
    label = pii_detector.optional_category_label(category_key)
    return f"{'Enabled' if enabled else 'Disabled'} install-wide PII detection for {label}"


_MUTATORS: dict[str, Callable[[dict[str, Any], dict[str, Any]], str]] = {
    "set_default_policy": _set_default_policy,
    "set_category_policy": _set_category_policy,
    "toggle_pii_detection": _toggle_pii_detection,
    "toggle_pii_category": _toggle_pii_category,
}


def apply_change(
    settings: dict[str, Any], config_path: str, *, action: str, payload: dict[str, Any],
) -> str:
    """Apply one install-wide policy change and make it live everywhere,
    returning a one-line summary for the caller's audit entry.

    Raises ``PolicyChangeRejected`` for an unsupported action or a payload
    naming something this install doesn't have, and ``OSError`` if
    settings.yaml can't be read back or written -- in both cases ``settings``
    is untouched and nothing has been reloaded.
    """
    if action not in _MUTATORS:
        raise PolicyChangeRejected(f"unsupported install-wide action {action!r}")
    if not config_path:
        raise PolicyChangeRejected(
            "this daemon was started without an install-wide settings.yaml path, "
            "so the policy cannot be edited from the browser"
        )

    with _WRITE_LOCK:
        try:
            with open(config_path, encoding="utf-8") as f:
                on_disk = _ROUND_TRIP_YAML.load(f)
        except FileNotFoundError:
            on_disk = None
        if on_disk is None:
            on_disk = {}
        summary = _MUTATORS[action](on_disk, payload)
        buf = io.StringIO()
        _ROUND_TRIP_YAML.dump(on_disk, buf)
        atomic_write_text(config_path, buf.getvalue())

        # Only now that it's on disk: adopt it into the live dict every other
        # reader of the install-wide config already holds a reference to (see
        # this module's docstring), then make it live for every principal.
        # Re-parsed with plain yaml.safe_load rather than reusing `on_disk`
        # itself, so the object every other module treats as a plain dict
        # (isinstance checks included) never becomes a ruamel CommentedMap.
        updated = yaml.safe_load(buf.getvalue()) or {}
        settings.clear()
        settings.update(updated)
        _reload_everywhere(updated)
    logger.info("Install-wide policy change applied: %s", summary)
    return summary


def _reload_everywhere(settings: dict[str, Any]) -> None:
    privacy_filter.reload_for_all_principals(settings, org_managed=True)
    pii_detector.reload_for_all_principals(settings.get("pii_detection", {}) or {})
    # SEC-23: the same thing settings_controller._save_config does after a
    # local-mode write, fanned out -- every entry recorded from here on
    # fingerprints the policy that actually governed it.
    set_security_config_hash_for_all_principals(compute_security_config_hash(settings))


__all__ = ["SUPPORTED_ACTIONS", "PolicyChangeRejected", "apply_change"]
