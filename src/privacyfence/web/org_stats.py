"""Install-wide usage counts for an org admin's Settings -> Stats page (ADR 0217).

Read straight from disk: one ``users/<id>/`` directory per principal that has ever signed in,
the connector credentials under it (the same existence checks ``/connect`` makes, via
``routes_connect._is_connected``), and the auto-accept rows in its
``authority/config/settings.yaml``. Nothing here goes through a principal's live
``ConnectorRegistry`` entry, so building the page neither builds anyone's connectors nor keeps
them from being evicted.

Counts only, by design: no principal id, address, rule value or rule sentence leaves this
module. An admin learns how many people connected Gmail or keep a Drive rule, never who or
which folder -- a rule's value (a sender, a folder id) is that user's own data, and the admin
already has the IdP for "who has an account".
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .. import paths
from ..policy import describe as policy_describe
from ..policy import propose as policy_propose
from ..policy import scope_rows
from ..principal import Principal
from . import routes_connect

logger = logging.getLogger(__name__)


def _principal_dirs() -> list[Path]:
    """Every ``users/<id>/`` directory ``paths.user_dir()`` could have produced. Existence-only,
    like ``paths.all_downloads_dirs()``: nothing is created."""
    users_root = paths.data_dir() / "users"
    if not users_root.is_dir():
        return []
    return [
        entry for entry in sorted(users_root.iterdir())
        if entry.is_dir() and paths._is_safe_principal_id(entry.name)
    ]


def _rule_connectors(user_root: Path) -> list[str]:
    """One connector name per auto-accept row in this principal's settings.yaml (``""`` for a row
    whose operations name none). A missing or unreadable file reads as no rows: one damaged file
    must not cost the admin the whole page."""
    settings_path = user_root / "authority" / "config" / "settings.yaml"
    if not settings_path.is_file():
        return []
    try:
        with open(settings_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        rows = scope_rows.rows_from_config(cfg)
    except Exception as exc:
        logger.warning("Could not read auto-accept rules under %s for stats: %s", user_root.name, exc)
        return []
    connectors = []
    for row in rows:
        names = sorted({policy_propose.connector_of_operation(op) for op in row.operations})
        connectors.append(names[0] if names else "")
    return connectors


def collect(org_config: dict[str, Any]) -> dict[str, Any]:
    """The Stats page's state: user totals, users per connector, and rule counts per connector.

    A connector is listed when the org config offers it or at least one user still holds a
    credential for it (one the bundle has since dropped still counts as connected there)."""
    services = list(routes_connect.SERVICE_LABELS)
    users_per_service = dict.fromkeys(services, 0)
    rules_per_connector: dict[str, int] = {}
    rule_users_per_connector: dict[str, int] = {}
    total = with_connectors = with_rules = rule_total = 0

    for user_root in _principal_dirs():
        total += 1
        principal = Principal(id=user_root.name)
        connected = [s for s in services if routes_connect._is_connected(principal, s, org_config)]
        for service in connected:
            users_per_service[service] += 1
        if connected:
            with_connectors += 1

        rule_connectors = _rule_connectors(user_root)
        if rule_connectors:
            with_rules += 1
        rule_total += len(rule_connectors)
        for connector in rule_connectors:
            rules_per_connector[connector] = rules_per_connector.get(connector, 0) + 1
        for connector in set(rule_connectors):
            rule_users_per_connector[connector] = rule_users_per_connector.get(connector, 0) + 1

    connectors = []
    for service in services:
        configured = routes_connect._is_configured(org_config, service)
        if configured or users_per_service[service]:
            connectors.append({
                "service": service, "label": routes_connect.SERVICE_LABELS[service],
                "configured": configured, "users": users_per_service[service],
            })
    rules = [
        {
            "connector": connector,
            "label": policy_describe.connector_label(connector) if connector else "Other",
            "rules": rules_per_connector[connector], "users": rule_users_per_connector[connector],
        }
        for connector in sorted(rules_per_connector, key=lambda c: (-rules_per_connector[c], c))
    ]
    return {
        "users": {"total": total, "with_connectors": with_connectors, "with_rules": with_rules},
        "connectors": connectors,
        "rules": {"total": rule_total, "by_connector": rules},
    }


def empty() -> dict[str, Any]:
    """What a non-admin's state carries in place of ``collect()``'s answer."""
    return {
        "users": {"total": 0, "with_connectors": 0, "with_rules": 0},
        "connectors": [],
        "rules": {"total": 0, "by_connector": []},
    }
