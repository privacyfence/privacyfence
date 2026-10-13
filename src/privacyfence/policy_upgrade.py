"""The token-aware upgrade of ``settings.yaml``'s ``auto_accept:`` section from the per-rule
schema (version 2) to one rule per scope (version 3).

``scope_rows.migrate_v2`` is the pure conversion. This module is the part that touches files
and scoped tokens, so every writer and every load goes through the one function below and a
section is never converted without its tokens being re-pointed first.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Callable

import yaml

from . import scoped_tokens
from .policy import scope_rows
from .principal import Principal
from .secure_files import atomic_write_text

logger = logging.getLogger(__name__)

BACKUP_SUFFIX = ".pre-scope-rules.bak"


def _v2_rules(section: Any) -> list[Any] | None:
    """The raw rules of a version-2 section (or one with no version), else ``None``."""
    if not isinstance(section, dict):
        return None
    rules = section.get("rules")
    version = section.get("version")
    if version == 2 or (version is None and isinstance(rules, list)):
        return rules if isinstance(rules, list) else []
    return None


def ensure_scope_rows(
    cfg: dict[str, Any],
    config_path: str,
    principal: Principal,
    *,
    log: Callable[[str], None] = logger.warning,
) -> bool:
    """Bring ``cfg``'s ``auto_accept:`` section to version 3, in memory and on disk.

    Returns ``True`` when the section is version 3 on return (or needed nothing), ``False`` when
    it had to stay version 2 because the scoped tokens could not be updated.
    """
    raw_rules = _v2_rules(cfg.get("auto_accept"))
    if raw_rules is None:
        return True

    migration = scope_rows.migrate_v2(raw_rules)

    backup = f"{config_path}{BACKUP_SUFFIX}"
    try:
        if not os.path.exists(backup) and os.path.exists(config_path):
            shutil.copy2(config_path, backup)
    except OSError as exc:
        log(f"{config_path}: could not keep a backup of the previous file: {exc}")

    try:
        labels = scoped_tokens.migrate_tokens(principal, migration)
    except OSError as exc:
        log(f"{principal.id}: could not update scoped tokens for the new rule format: {exc}")
        return False

    cfg["auto_accept"] = scope_rows.rows_to_config(migration.rows)
    try:
        atomic_write_text(config_path, yaml.safe_dump(cfg, default_flow_style=False, allow_unicode=True))
    except OSError as exc:
        log(f"{config_path}: could not write the converted auto-accept rules: {exc}")
        return True

    log(
        f"{config_path}: auto-accept rules converted to one rule per scope (version 3); "
        f"the previous file is kept at {backup}"
    )
    if labels:
        log(
            "Scoped tokens now allowed to carry personal data, because they used a rule that "
            f"allowed it: {', '.join(labels)}"
        )
    return True
