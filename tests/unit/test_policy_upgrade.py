"""Tests for policy_upgrade.ensure_scope_rows: the token-aware v2 -> v3 upgrade (ADR 0172)."""
from __future__ import annotations

import json
import os
import shutil
from types import SimpleNamespace

import pytest
import yaml

from privacyfence import auto_accept, paths, policy_upgrade, scoped_tokens
from privacyfence.policy import scope_rows, store
from privacyfence.policy.engine import PolicyRule
from privacyfence.principal import LOCAL_PRINCIPAL
from privacyfence.scoped_tokens import STORE_FILE_NAME, ScopedTokenPolicy, ScopedTokenStore

NARROW_ID = "r-narrow"
FLAGGED_ID = "r-flagged"


def _raw(rule_id, operations, flagged=False):
    data = store.rule_to_dict(PolicyRule(
        id=rule_id, predicate="approved_folder", value=["F1"], operations=frozenset(operations),
    ))
    if flagged:
        data["allow_personal_data"] = True
    return data


V2_RULES = [
    _raw(NARROW_ID, ["drive.download_file"]),
    _raw(FLAGGED_ID, ["drive.read_file_contents", "drive.download_file"], flagged=True),
]


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(scoped_tokens, "_STORE", None)
    monkeypatch.setattr(
        auto_accept, "get_policy_v2_store_rules",
        lambda: [SimpleNamespace(id=i, predicate="approved_folder", operations=frozenset({"x"}))
                 for i in (NARROW_ID, FLAGGED_ID)],
    )


@pytest.fixture
def config_path(tmp_path):
    return str(tmp_path / "settings.yaml")


def _write_v2(config_path, rules=V2_RULES):
    cfg = {"auto_accept": {"version": 2, "rules": [dict(r) for r in rules]}, "logging": {"level": "INFO"}}
    with open(config_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh)
    return cfg


def _make_token(label="sync", rule_ids=(NARROW_ID,)):
    token_store = ScopedTokenStore(lambda: [LOCAL_PRINCIPAL])
    token, _ = token_store.create(
        LOCAL_PRINCIPAL, label=label, days=30, rule_ids=list(rule_ids), tools=["gmail_list_messages"],
        policy=ScopedTokenPolicy(enabled=True, max_days=365),
    )
    return token


def _token_file(tmp_path):
    return tmp_path / "authority" / STORE_FILE_NAME


def _read(config_path):
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_v2_is_converted_and_backed_up(config_path, tmp_path):
    original = _write_v2(config_path)
    original_text = open(config_path, encoding="utf-8").read()
    _make_token()
    logged: list[str] = []
    cfg = yaml.safe_load(original_text)

    assert policy_upgrade.ensure_scope_rows(cfg, config_path, LOCAL_PRINCIPAL, log=logged.append) is True

    written = _read(config_path)
    assert written["auto_accept"]["version"] == 3
    assert cfg["auto_accept"] == written["auto_accept"]
    assert written["logging"] == original["logging"]
    (row,) = written["auto_accept"]["rules"]
    assert row["scope"] == "drive.folder"
    assert sorted(row["former_ids"]) == sorted([NARROW_ID, FLAGGED_ID])
    assert open(f"{config_path}.pre-scope-rules.bak", encoding="utf-8").read() == original_text
    (tok,) = json.loads(_token_file(tmp_path).read_text(encoding="utf-8"))["tokens"]
    assert tok["rule_ids"] == [row["id"]]
    assert logged == [
        f"{config_path}: auto-accept rules converted to one rule per scope (version 3); "
        f"the previous file is kept at {config_path}.pre-scope-rules.bak"
    ]


def test_backup_is_not_overwritten(config_path):
    _write_v2(config_path)
    backup = f"{config_path}.pre-scope-rules.bak"
    with open(backup, "w", encoding="utf-8") as fh:
        fh.write("older backup")

    assert policy_upgrade.ensure_scope_rows(_read(config_path), config_path, LOCAL_PRINCIPAL, log=lambda m: None)

    assert open(backup, encoding="utf-8").read() == "older backup"


def test_v3_is_left_alone(config_path):
    cfg = {"auto_accept": scope_rows.rows_to_config([])}
    with open(config_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh)
    before = open(config_path, encoding="utf-8").read()
    logged: list[str] = []

    assert policy_upgrade.ensure_scope_rows(cfg, config_path, LOCAL_PRINCIPAL, log=logged.append) is True

    assert open(config_path, encoding="utf-8").read() == before
    assert logged == []
    assert not os.path.exists(f"{config_path}.pre-scope-rules.bak")


def test_missing_config_file_makes_no_backup(config_path, tmp_path):
    cfg = {"auto_accept": {"version": 2, "rules": [dict(r) for r in V2_RULES]}}

    assert policy_upgrade.ensure_scope_rows(cfg, config_path, LOCAL_PRINCIPAL, log=lambda m: None) is True

    assert not (tmp_path / "settings.yaml.pre-scope-rules.bak").exists()
    assert cfg["auto_accept"]["version"] == 3


def test_token_failure_leaves_v2_and_returns_false(config_path, monkeypatch):
    _write_v2(config_path)
    before = open(config_path, encoding="utf-8").read()
    cfg = _read(config_path)
    logged: list[str] = []

    def boom(principal, migration):
        raise OSError("disk full")

    monkeypatch.setattr(scoped_tokens, "migrate_tokens", boom)

    assert policy_upgrade.ensure_scope_rows(cfg, config_path, LOCAL_PRINCIPAL, log=logged.append) is False

    assert cfg["auto_accept"]["version"] == 2
    assert open(config_path, encoding="utf-8").read() == before
    assert logged == [f"{LOCAL_PRINCIPAL.id}: could not update scoped tokens for the new rule format: disk full"]


def test_backup_failure_leaves_v2_and_returns_false(config_path, monkeypatch):
    _write_v2(config_path)
    before = open(config_path, encoding="utf-8").read()
    cfg = _read(config_path)
    logged: list[str] = []
    migrated: list[object] = []

    def boom(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(shutil, "copy2", boom)
    monkeypatch.setattr(scoped_tokens, "migrate_tokens", lambda *a, **k: migrated.append(a))

    assert policy_upgrade.ensure_scope_rows(cfg, config_path, LOCAL_PRINCIPAL, log=logged.append) is False

    assert cfg["auto_accept"]["version"] == 2
    assert open(config_path, encoding="utf-8").read() == before
    assert _read(config_path)["auto_accept"]["version"] == 2
    assert migrated == []
    assert logged == [
        f"{config_path}: could not keep a backup of the previous file, so the auto-accept rules "
        "stay in the previous format: read-only"
    ]


def test_settings_write_failure_keeps_v3_in_memory(config_path, monkeypatch):
    _write_v2(config_path)
    before = open(config_path, encoding="utf-8").read()
    cfg = _read(config_path)
    logged: list[str] = []

    def boom(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(policy_upgrade, "atomic_write_text", boom)

    assert policy_upgrade.ensure_scope_rows(cfg, config_path, LOCAL_PRINCIPAL, log=logged.append) is True

    assert cfg["auto_accept"]["version"] == 3
    assert open(config_path, encoding="utf-8").read() == before
    assert len(logged) == 1 and "read-only" in logged[0]


def test_logs_flagged_token_labels(config_path):
    _write_v2(config_path)
    _make_token(label="flagged one", rule_ids=(FLAGGED_ID,))
    logged: list[str] = []

    policy_upgrade.ensure_scope_rows(_read(config_path), config_path, LOCAL_PRINCIPAL, log=logged.append)

    assert logged[-1] == (
        "Scoped tokens now allowed to carry personal data, because they used a rule that allowed it: flagged one"
    )

