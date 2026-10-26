"""Tests for policy_upgrade: the token-aware v2 -> v3 upgrade (ADR 0172) and the start-up conversion
of scoped tokens to their own rules (ADR 0236)."""
from __future__ import annotations

import json
import os
import shutil

import pytest
import yaml

from privacyfence import paths, policy_upgrade, scoped_tokens
from privacyfence.policy import scope_rows, store
from privacyfence.policy.engine import PolicyRule
from privacyfence.principal import LOCAL_PRINCIPAL
from privacyfence.policy.registry import Verb
from privacyfence.scoped_tokens import STORE_FILE_NAME

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


@pytest.fixture
def config_path(tmp_path):
    return str(tmp_path / "settings.yaml")


def _write_v2(config_path, rules=V2_RULES):
    cfg = {"auto_accept": {"version": 2, "rules": [dict(r) for r in rules]}, "logging": {"level": "INFO"}}
    with open(config_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh)
    return cfg


def _token_file(tmp_path):
    return tmp_path / "authority" / STORE_FILE_NAME


def _make_token(label="sync", rule_ids=(NARROW_ID,)):
    """Add a token entry as a version before ADR 0236 wrote it: rule ids, no rules of its own."""
    path = paths.data_dir() / "authority" / STORE_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "tokens": []}
    data["tokens"].append({
        "id": f"st-{len(data['tokens'])}", "label": label, "digest": f"d-{label}", "principal_email": "",
        "principal_display_name": "", "created_at": 0.0, "expires_at": 1e12, "rule_ids": list(rule_ids),
        "tools": ["gmail_list_messages"],
    })
    path.write_text(json.dumps(data), encoding="utf-8")


def _tokens(tmp_path):
    return {t["label"]: t for t in json.loads(_token_file(tmp_path).read_text(encoding="utf-8"))["tokens"]}


def _read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _read(config_path):
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_v2_is_converted_and_backed_up(config_path, tmp_path):
    original = _write_v2(config_path)
    original_text = _read_text(config_path)
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
    assert _read_text(f"{config_path}.pre-scope-rules.bak") == original_text
    (tok,) = json.loads(_token_file(tmp_path).read_text(encoding="utf-8"))["tokens"]
    assert "rule_ids" not in tok
    (copy,) = tok["rules"]
    copied = scope_rows.compile_row(scope_rows.row_from_dict(copy))
    assert copy["id"] == NARROW_ID
    assert {op for r in copied for op in r.operations} == {"drive.download_file"}
    assert logged == [
        "Scoped token 'sync' now holds its own copy of 1 rule(s); 0 rule(s) it named no longer existed",
        f"{config_path}: auto-accept rules converted to one rule per scope (version 3); "
        f"the previous file is kept at {config_path}.pre-scope-rules.bak"
    ]


def test_backup_is_not_overwritten(config_path):
    _write_v2(config_path)
    backup = f"{config_path}.pre-scope-rules.bak"
    with open(backup, "w", encoding="utf-8") as fh:
        fh.write("older backup")

    assert policy_upgrade.ensure_scope_rows(_read(config_path), config_path, LOCAL_PRINCIPAL, log=lambda m: None)

    assert _read_text(backup) == "older backup"


def test_v3_is_left_alone(config_path):
    cfg = {"auto_accept": scope_rows.rows_to_config([])}
    with open(config_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh)
    before = _read_text(config_path)
    logged: list[str] = []

    assert policy_upgrade.ensure_scope_rows(cfg, config_path, LOCAL_PRINCIPAL, log=logged.append) is True

    assert _read_text(config_path) == before
    assert logged == []
    assert not os.path.exists(f"{config_path}.pre-scope-rules.bak")


def test_missing_config_file_makes_no_backup(config_path, tmp_path):
    cfg = {"auto_accept": {"version": 2, "rules": [dict(r) for r in V2_RULES]}}

    assert policy_upgrade.ensure_scope_rows(cfg, config_path, LOCAL_PRINCIPAL, log=lambda m: None) is True

    assert not (tmp_path / "settings.yaml.pre-scope-rules.bak").exists()
    assert cfg["auto_accept"]["version"] == 3


@pytest.mark.parametrize("error", [OSError("disk full"), ValueError("disk full")])
def test_token_failure_leaves_v2_and_returns_false(config_path, monkeypatch, error):
    _write_v2(config_path)
    before = _read_text(config_path)
    cfg = _read(config_path)
    logged: list[str] = []

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(scoped_tokens, "own_legacy_rules", boom)

    assert policy_upgrade.ensure_scope_rows(cfg, config_path, LOCAL_PRINCIPAL, log=logged.append) is False

    assert cfg["auto_accept"]["version"] == 2
    assert _read_text(config_path) == before
    assert logged == [f"{LOCAL_PRINCIPAL.id}: could not update scoped tokens for the new rule format: disk full"]


def test_a_failing_token_write_leaves_the_section_v2(config_path, tmp_path, monkeypatch):
    _write_v2(config_path)
    _make_token()
    tokens_before = _token_file(tmp_path).read_bytes()
    cfg = _read(config_path)

    def boom(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(scoped_tokens.secure_files, "atomic_write_json", boom)

    assert policy_upgrade.ensure_scope_rows(cfg, config_path, LOCAL_PRINCIPAL, log=lambda m: None) is False

    assert _read(config_path)["auto_accept"]["version"] == 2
    assert _token_file(tmp_path).read_bytes() == tokens_before


def test_backup_failure_leaves_v2_and_returns_false(config_path, monkeypatch):
    _write_v2(config_path)
    before = _read_text(config_path)
    cfg = _read(config_path)
    logged: list[str] = []
    migrated: list[object] = []

    def boom(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(shutil, "copy2", boom)
    monkeypatch.setattr(scoped_tokens, "own_legacy_rules", lambda *a, **k: migrated.append(a))

    assert policy_upgrade.ensure_scope_rows(cfg, config_path, LOCAL_PRINCIPAL, log=logged.append) is False

    assert cfg["auto_accept"]["version"] == 2
    assert _read_text(config_path) == before
    assert _read(config_path)["auto_accept"]["version"] == 2
    assert migrated == []
    assert logged == [
        f"{config_path}: could not keep a backup of the previous file, so the auto-accept rules "
        "stay in the previous format: read-only"
    ]


def test_settings_write_failure_keeps_v3_in_memory(config_path, monkeypatch):
    _write_v2(config_path)
    before = _read_text(config_path)
    cfg = _read(config_path)
    logged: list[str] = []

    def boom(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(policy_upgrade, "atomic_write_text", boom)

    assert policy_upgrade.ensure_scope_rows(cfg, config_path, LOCAL_PRINCIPAL, log=logged.append) is True

    assert cfg["auto_accept"]["version"] == 3
    assert _read_text(config_path) == before
    assert len(logged) == 1 and "read-only" in logged[0]


def test_logs_flagged_token_labels(config_path):
    _write_v2(config_path)
    _make_token(label="flagged one", rule_ids=(FLAGGED_ID,))
    logged: list[str] = []

    policy_upgrade.ensure_scope_rows(_read(config_path), config_path, LOCAL_PRINCIPAL, log=logged.append)

    assert logged[-1] == (
        "Scoped tokens now allowed to carry personal data, because they used a rule that allowed it: flagged one"
    )


def test_a_v2_conversion_copies_the_v2_rules_and_transfers_the_flag(config_path, tmp_path):
    _write_v2(config_path)
    _make_token(label="pii", rule_ids=(FLAGGED_ID, "r-gone"))
    _make_token(label="plain", rule_ids=(NARROW_ID,))

    policy_upgrade.ensure_scope_rows(_read(config_path), config_path, LOCAL_PRINCIPAL, log=lambda m: None)

    tokens = _tokens(tmp_path)
    assert tokens["pii"]["allow_personal_data"] is True
    assert "allow_personal_data" not in tokens["plain"]
    flagged = scope_rows.compile_rows(scope_rows.row_from_dict(d) for d in tokens["pii"]["rules"])
    assert {r.id for r in flagged} == {FLAGGED_ID}
    assert {op for r in flagged for op in r.operations} == {"drive.read_file_contents", "drive.download_file"}


def test_a_v3_section_writes_nothing_to_the_token_file(config_path, tmp_path):
    cfg = {"auto_accept": scope_rows.rows_to_config([])}
    _make_token()
    before = _token_file(tmp_path).read_bytes()

    assert policy_upgrade.ensure_scope_rows(cfg, config_path, LOCAL_PRINCIPAL, log=lambda m: None) is True

    assert _token_file(tmp_path).read_bytes() == before


def _v3_config(*rows):
    return {"auto_accept": scope_rows.rows_to_config(list(rows))}


FOLDER_ROW = scope_rows.ScopeRow(
    id="r-folder", scope="drive.folder", value=["F1"], verbs=frozenset({Verb.READ}),
    operations=scope_rows.implied_operations("drive.folder", [Verb.READ]),
)


class TestEnsureTokenRules:
    def test_converts_a_legacy_token_from_the_v3_rows(self, tmp_path):
        _make_token(rule_ids=(FOLDER_ROW.id, "r-gone"))
        logged: list[str] = []

        policy_upgrade.ensure_token_rules(_v3_config(FOLDER_ROW), LOCAL_PRINCIPAL, log=logged.append)

        assert _tokens(tmp_path)["sync"]["rules"] == [scope_rows.row_to_dict(FOLDER_ROW)]
        assert logged == [
            "Scoped token 'sync' now holds its own copy of 1 rule(s); 1 rule(s) it named no longer existed",
        ]

    def test_does_nothing_while_the_section_is_v2(self, tmp_path):
        _make_token()
        before = _token_file(tmp_path).read_bytes()

        policy_upgrade.ensure_token_rules(
            {"auto_accept": {"version": 2, "rules": list(V2_RULES)}}, LOCAL_PRINCIPAL, log=lambda m: None,
        )

        assert _token_file(tmp_path).read_bytes() == before

    def test_a_config_without_a_section_gives_legacy_tokens_no_rules(self, tmp_path):
        _make_token()

        policy_upgrade.ensure_token_rules({}, LOCAL_PRINCIPAL, log=lambda m: None)

        assert _tokens(tmp_path)["sync"]["rules"] == []

    @pytest.mark.parametrize("error", [OSError("disk full"), ValueError("unsafe principal id")])
    def test_a_failure_is_logged_not_raised(self, monkeypatch, error):
        def boom(*args, **kwargs):
            raise error

        monkeypatch.setattr(scoped_tokens, "own_legacy_rules", boom)
        logged: list[str] = []

        policy_upgrade.ensure_token_rules(_v3_config(), LOCAL_PRINCIPAL, log=logged.append)

        assert logged == [f"{LOCAL_PRINCIPAL.id}: could not give scoped tokens their own copy of their rules: {error}"]

