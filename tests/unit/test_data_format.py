"""Tests for data_format: the data-format marker and the start-up pass that earns it."""
from __future__ import annotations

import json
import os

import pytest
import yaml

from privacyfence import __version__, admin_config, daemon_main, data_format, paths, policy_upgrade, scoped_tokens
from privacyfence.org_mode import ConfigurationError
from privacyfence.policy.registry import Verb
from privacyfence.scoped_tokens import STORE_FILE_NAME


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(daemon_main, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(scoped_tokens, "_STORE", None)


@pytest.fixture
def admin(tmp_path):
    """The admin config exists, as it does after ``load_admin_config``."""
    admin_config.save(admin_config._default_config())


def _settings(tmp_path, principal="local"):
    base = tmp_path if principal == "local" else tmp_path / "users" / principal
    return base / "authority" / "config" / "settings.yaml"


def _tokens(tmp_path, principal="local"):
    base = tmp_path if principal == "local" else tmp_path / "users" / principal
    return base / "authority" / STORE_FILE_NAME


def _write_settings(path, cfg):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _row(row_id="r-1", verbs=("read",), **extra):
    return {
        "id": row_id, "scope": "drive.folder", "value": ["V1"], "verbs": list(verbs),
        "operations": ["drive.download_file", "drive.copy_file"], **extra,
    }


def _v3(rows):
    return {"auto_accept": {"version": 3, "rules": rows}}


def _v2():
    rule = {"id": "r-a", "predicate": "approved_folder", "value": ["V1"], "operations": ["drive.download_file"]}
    return {"auto_accept": {"version": 2, "rules": [rule]}}


def _write_tokens(path, tokens):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "tokens": tokens}), encoding="utf-8")


def _legacy_token(token_id="st-old"):
    return {
        "id": token_id, "label": "old", "digest": "d", "principal_email": "", "principal_display_name": "",
        "created_at": 0.0, "expires_at": 1e12, "rule_ids": ["r-gone"], "tools": ["gmail_list_messages"],
    }


def _migrate():
    messages: list[str] = []
    problems = data_format.migrate_everything(log=messages.append)
    return problems, messages


class TestMarker:
    def test_marker_path_is_in_the_data_directory(self, tmp_path):
        assert data_format.marker_path() == tmp_path / "data_format.json"

    def test_missing_marker_reads_as_none(self):
        assert data_format.read_marker() is None

    @pytest.mark.parametrize("content", [
        "not json", "[]", "{}", '{"format": "7"}', '{"format": 7.5}', '{"format": true}', '{"format": null}',
    ])
    def test_malformed_marker_reads_as_none(self, tmp_path, content, caplog):
        (tmp_path / "data_format.json").write_text(content)

        assert data_format.read_marker() is None
        assert "not a valid data-format marker" in caplog.text

    def test_unreadable_marker_reads_as_none(self, tmp_path, caplog):
        (tmp_path / "data_format.json").mkdir()

        assert data_format.read_marker() is None
        assert "could not be read" in caplog.text

    def test_valid_marker_is_returned(self, tmp_path):
        (tmp_path / "data_format.json").write_text('{"format": 7, "written_by": "7.0.0"}')

        assert data_format.read_marker() == {"format": 7, "written_by": "7.0.0"}

    def test_check_not_newer_accepts_none_current_and_older(self):
        for marker in (None, {"format": 7}, {"format": 6}):
            data_format.check_not_newer(marker)

    def test_check_not_newer_refuses_format_8_with_the_exact_message(self):
        with pytest.raises(ConfigurationError) as caught:
            data_format.check_not_newer({"format": 8, "written_by": "8.0.0"})

        assert str(caught.value) == (
            f"This data was last used by PrivacyFence 8.0.0 (data format 8). "
            f"This release, {__version__}, cannot read it. Install 8.0.0 or newer."
        )


class TestMigrateEverything:
    def test_a_fresh_data_dir_gets_the_marker(self, tmp_path, admin):
        problems, messages = _migrate()

        assert problems == [] and messages == []
        marker = json.loads((tmp_path / "data_format.json").read_text())
        assert marker["format"] == 7 and marker["written_by"] == __version__
        assert marker["completed_at"].endswith("+00:00")
        if os.name != "nt":
            assert (tmp_path / "data_format.json").stat().st_mode & 0o777 == 0o600
        assert data_format.status() == {"ready": True, "problems": []}

    def test_without_the_admin_config_there_is_a_problem_and_no_marker(self, tmp_path):
        problems, messages = _migrate()

        assert problems == ["the admin config has not been created"]
        assert messages == ["Not ready for PrivacyFence 8: the admin config has not been created"]
        assert not (tmp_path / "data_format.json").exists()
        assert data_format.status() == {"ready": False, "problems": problems}

    def test_an_unreadable_admin_config_is_the_same_problem(self, tmp_path):
        (tmp_path / "org").mkdir()
        (tmp_path / "org" / "admin_config.json").write_text("not json")

        assert _migrate()[0] == ["the admin config has not been created"]

    def test_a_v2_section_is_converted_and_the_marker_written(self, tmp_path, admin):
        _write_settings(_settings(tmp_path), _v2())

        problems, _ = _migrate()

        assert problems == []
        assert yaml.safe_load(_settings(tmp_path).read_text())["auto_accept"]["version"] == 3
        assert (tmp_path / "data_format.json").exists()

    def test_a_v2_section_whose_rewrite_fails_blocks_the_marker(self, tmp_path, admin, monkeypatch):
        path = _settings(tmp_path)
        _write_settings(path, _v2())
        real = policy_upgrade.atomic_write_text

        def fail_for_settings(target, text, **kwargs):
            if str(target) == str(path):
                raise OSError("read-only")
            return real(target, text, **kwargs)

        monkeypatch.setattr(policy_upgrade, "atomic_write_text", fail_for_settings)

        problems, _ = _migrate()

        assert problems == ["local: rules are still in the version-2 format"]
        assert yaml.safe_load(path.read_text())["auto_accept"]["version"] == 2
        assert not (tmp_path / "data_format.json").exists()

    def test_a_marked_v1_section_is_cleaned_and_the_marker_written(self, tmp_path, admin):
        path = _settings(tmp_path)
        _write_settings(path, {**_v3([]), "auto_accept_rules": [], "auto_accept_grants": {}, policy_marker(): True})

        problems, _ = _migrate()

        assert problems == []
        written = yaml.safe_load(path.read_text())
        assert "auto_accept_rules" not in written and policy_marker() not in written
        assert (tmp_path / "data_format.json").exists()

    def test_a_failed_v1_clean_up_write_is_a_problem(self, tmp_path, admin, monkeypatch):
        path = _settings(tmp_path)
        _write_settings(path, {**_v3([]), "auto_accept_rules": [], policy_marker(): True})

        def fail(*args, **kwargs):
            raise OSError("read-only")

        monkeypatch.setattr(data_format, "atomic_write_text", fail)

        assert _migrate()[0] == ["local: could not rewrite settings.yaml"]
        assert not (tmp_path / "data_format.json").exists()

    def test_an_unmarked_v1_section_is_a_problem(self, tmp_path, admin):
        _write_settings(_settings(tmp_path), {"auto_accept_rules": []})

        problems, _ = _migrate()

        assert problems == ["local: settings.yaml still has v1 policy sections"]
        assert not (tmp_path / "data_format.json").exists()

    def test_a_token_that_only_names_rules_is_a_problem(self, tmp_path, admin, monkeypatch):
        _write_tokens(_tokens(tmp_path), [_legacy_token()])
        # The conversion itself fails here, so the entry is still there when the file is read back.
        monkeypatch.setattr(policy_upgrade, "ensure_token_rules", lambda cfg, principal, log: None)

        problems, _ = _migrate()

        assert problems == ["local: scoped token st-old still names rules"]
        assert not (tmp_path / "data_format.json").exists()

    def test_a_token_naming_rules_is_converted_when_it_can_be(self, tmp_path, admin):
        _write_tokens(_tokens(tmp_path), [_legacy_token()])

        problems, _ = _migrate()

        assert problems == []
        (entry,) = json.loads(_tokens(tmp_path).read_text())["tokens"]
        assert entry["rules"] == [] and "rule_ids" not in entry

    def test_a_legacy_verb_row_gets_only_its_verbs_rewritten(self, tmp_path, admin):
        path = _settings(tmp_path)
        row = _row(
            verbs=("read", "copy", "archive"), former_ids=["old-1"], unknown_extra={"k": [1, 2]},
        )
        custom = {
            "id": "r-custom", "predicate": "approved_folder", "value": ["V1"], "verbs": ["share"],
            "operations": ["drive.download_file"], "conditions": [["age_days", 3]], "mystery": 1,
        }
        _write_settings(path, {**_v3([row, custom]), "logging": {"level": "INFO"}})
        original = path.read_bytes()

        problems, _ = _migrate()

        assert problems == []
        written = yaml.safe_load(path.read_text())
        first, second = written["auto_accept"]["rules"]
        assert first == {**row, "verbs": sorted([Verb.READ.value, Verb.MOVE.value])}
        assert second == {**custom, "verbs": [Verb.SEND.value]}
        assert written["logging"] == {"level": "INFO"}
        assert (tmp_path / "authority" / "config" / "settings.yaml.pre-canonical-verbs.bak").read_bytes() == original
        assert (tmp_path / "data_format.json").exists()

    def test_a_token_rules_row_gets_only_its_verbs_rewritten(self, tmp_path, admin):
        path = _tokens(tmp_path)
        row = _row(verbs=("create", "read"), former_ids=["old-1"], extra_key=True)
        entry = {**_legacy_token(), "rules": [row]}
        del entry["rule_ids"]
        entry["future_key"] = {"a": 1}
        _write_tokens(path, [entry])
        original = path.read_bytes()

        problems, _ = _migrate()

        assert problems == []
        (written,) = json.loads(path.read_text())["tokens"]
        assert written == {**entry, "rules": [{**row, "verbs": sorted([Verb.ADD.value, Verb.READ.value])}]}
        assert path.with_name(STORE_FILE_NAME + ".pre-canonical-verbs.bak").read_bytes() == original

    def test_a_canonical_row_in_hand_made_order_is_not_rewritten(self, tmp_path, admin):
        path = _settings(tmp_path)
        _write_settings(path, _v3([_row(verbs=("send", "read"), operations=["z.b", "a.b"])]))
        tokens = _tokens(tmp_path)
        _write_tokens(tokens, [{**_legacy_token(), "rules": [_row(verbs=("send", "read"))]}])
        stamps = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (path, tokens)}

        assert _migrate()[0] == []

        assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in stamps} == stamps
        assert not list(tmp_path.rglob("*.pre-canonical-verbs.bak"))

    def test_an_unreadable_row_is_a_problem_and_left_as_it_is(self, tmp_path, admin):
        path = _settings(tmp_path)
        bad = {"id": "r-bad", "verbs": ["copy"]}
        _write_settings(path, _v3([bad]))
        before = path.read_bytes()

        problems, _ = _migrate()

        assert problems == ["local: rule r-bad could not be read"]
        assert path.read_bytes() == before
        assert not (tmp_path / "data_format.json").exists()

    def test_corrupt_files_of_one_person_do_not_stop_the_others(self, tmp_path, admin):
        _settings(tmp_path, "os-alice").parent.mkdir(parents=True)
        _settings(tmp_path, "os-alice").write_text("a: [unclosed")
        bob_tokens = _tokens(tmp_path, "bob")
        bob_tokens.parent.mkdir(parents=True)
        bob_tokens.write_text("{not json")
        _write_settings(_settings(tmp_path, "carol"), _v2())

        problems, messages = _migrate()

        assert problems == [
            "bob: scoped_tokens.json cannot be read",
            "os-alice: settings.yaml cannot be read",
        ]
        prefix = "Not ready for PrivacyFence 8: "
        assert [m.removeprefix(prefix) for m in messages if m.startswith(prefix)] == problems
        assert yaml.safe_load(_settings(tmp_path, "carol").read_text())["auto_accept"]["version"] == 3
        assert not (tmp_path / "data_format.json").exists()

    @pytest.mark.parametrize("content", ["- a list", "just text"])
    def test_a_settings_file_that_is_not_a_mapping_is_a_problem(self, tmp_path, admin, content):
        _settings(tmp_path).parent.mkdir(parents=True)
        _settings(tmp_path).write_text(content)

        assert _migrate()[0] == ["local: settings.yaml is not a mapping"]

    def test_a_settings_file_that_stops_being_a_mapping_after_the_conversion_is_a_problem(
        self, tmp_path, admin, monkeypatch,
    ):
        path = _settings(tmp_path)
        _write_settings(path, _v3([]))

        def damage(cfg, resolved, principal, *, log):
            path.write_text("- now a list")

        monkeypatch.setattr(policy_upgrade, "ensure_scope_rows", damage)

        assert _migrate()[0] == ["local: settings.yaml is not a mapping"]

    def test_a_settings_file_that_becomes_unreadable_after_the_conversion_is_a_problem(
        self, tmp_path, admin, monkeypatch,
    ):
        path = _settings(tmp_path)
        _write_settings(path, _v3([]))

        def damage(cfg, resolved, principal, *, log):
            path.write_text("a: [unclosed")

        monkeypatch.setattr(policy_upgrade, "ensure_scope_rows", damage)

        assert _migrate()[0] == ["local: settings.yaml cannot be read"]

    def test_an_org_principal_that_never_signed_in_is_migrated(self, tmp_path, admin):
        path = _settings(tmp_path, "dana")
        _write_settings(path, _v2())
        _write_tokens(_tokens(tmp_path, "dana"), [_legacy_token()])

        problems, _ = _migrate()

        assert problems == []
        assert yaml.safe_load(path.read_text())["auto_accept"]["version"] == 3
        (entry,) = json.loads(_tokens(tmp_path, "dana").read_text())["tokens"]
        assert "rules" in entry

    def test_a_marker_at_format_7_is_not_rewritten(self, tmp_path, admin):
        marker = tmp_path / "data_format.json"
        marker.write_text(json.dumps({"format": 7, "written_by": "7.0.0", "completed_at": "2030-01-01T00:00:00+00:00"}))
        before = (marker.read_bytes(), marker.stat().st_mtime_ns)

        assert _migrate()[0] == []

        assert (marker.read_bytes(), marker.stat().st_mtime_ns) == before

    def test_a_marker_below_format_7_is_replaced(self, tmp_path, admin):
        (tmp_path / "data_format.json").write_text('{"format": 6}')

        assert _migrate()[0] == []

        assert json.loads((tmp_path / "data_format.json").read_text())["format"] == 7

    def test_a_marker_that_cannot_be_written_is_a_problem(self, tmp_path, admin, monkeypatch):
        def fail(*args, **kwargs):
            raise OSError("read-only")

        monkeypatch.setattr(data_format, "atomic_write_json", fail)

        assert _migrate()[0] == ["the data-format marker could not be written"]
        assert data_format.status()["ready"] is False

    def test_a_users_directory_that_cannot_be_listed_is_a_problem(self, tmp_path, admin, monkeypatch):
        def fail():
            raise OSError("denied")
            yield  # pragma: no cover

        from privacyfence.web import mcp_auth

        monkeypatch.setattr(mcp_auth, "scoped_token_principals", fail)

        assert _migrate()[0] == ["the users directory cannot be read"]

    def test_an_unexpected_error_for_one_person_is_a_problem_and_never_raises(self, tmp_path, admin, monkeypatch):
        _write_settings(_settings(tmp_path, "erin"), _v3([]))
        real = policy_upgrade.ensure_token_rules

        def explode(cfg, principal, *, log):
            if principal.id == "erin":
                raise RuntimeError("boom")
            return real(cfg, principal, log=log)

        monkeypatch.setattr(policy_upgrade, "ensure_token_rules", explode)

        problems, _ = _migrate()

        assert problems == ["erin: could not be migrated"]

    def test_status_reflects_the_last_run(self, tmp_path):
        assert data_format.status() == {"ready": False, "problems": []}
        _migrate()
        assert data_format.status()["ready"] is False
        admin_config.save(admin_config._default_config())
        _migrate()
        assert data_format.status() == {"ready": True, "problems": []}
        data_format.reset_for_tests()
        assert data_format.status() == {"ready": False, "problems": []}

    def test_status_returns_a_copy(self):
        data_format.status()["problems"].append("x")
        assert data_format.status()["problems"] == []

    def test_a_token_entry_that_is_not_an_object_is_ignored_by_the_still_names_rules_check(self, tmp_path, admin):
        _write_tokens(_tokens(tmp_path), ["junk"])

        assert _migrate()[0] == []

    def test_a_tokens_file_without_a_token_list_is_unreadable(self, tmp_path, admin):
        _tokens(tmp_path).parent.mkdir(parents=True)
        _tokens(tmp_path).write_text('{"tokens": 5}')

        assert _migrate()[0] == ["local: scoped_tokens.json cannot be read"]


def policy_marker() -> str:
    from privacyfence.policy import store

    return store.CONVERTED_V1_MARKER

