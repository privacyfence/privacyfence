"""The org rule store: process-wide rows, their compiled rules, validation on load, listeners."""
from __future__ import annotations

import json

import pytest

from privacyfence import admin_config, org_rules, paths
from privacyfence.policy import scope_rows
from privacyfence.policy.registry import Verb


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


def _row(value="folder1", scope="drive.folder", verbs=(Verb.READ,)):
    ops = scope_rows.implied_operations(scope, verbs)
    return scope_rows.ScopeRow(
        id=scope_rows.row_id_for(scope, [value]), scope=scope, value=[value],
        verbs=frozenset(verbs), operations=ops,
    )


def _dicts(*rows):
    return {"org_rules": [scope_rows.row_to_dict(r) for r in rows]}


class TestStore:
    def test_set_rows_and_rules(self):
        row = _row()
        org_rules.set([row])
        assert org_rules.rows() == [row]
        compiled = org_rules.rules()
        assert compiled and compiled == scope_rows.compile_rows([row])
        assert org_rules.rules()[0] is compiled[0]

    def test_set_invalidates_the_compiled_rules(self):
        org_rules.set([_row("a")])
        first = org_rules.rules()
        org_rules.set([_row("b")])
        assert org_rules.rules()[0] is not first[0]

    def test_non_parsing_row_is_dropped(self):
        org_rules.load_from_config({"org_rules": [{"nonsense": True}, "x", scope_rows.row_to_dict(_row())]})
        assert [r.id for r in org_rules.rows()] == [_row().id]

    def test_custom_row_is_dropped(self):
        custom = scope_rows.ScopeRow(
            id="c1", scope=None, value=["x"], verbs=frozenset({Verb.READ}),
            operations=frozenset({"drive_get_file"}), predicate="drive.folder",
        )
        org_rules.load_from_config({"org_rules": [scope_rows.row_to_dict(custom)]})
        assert org_rules.rows() == []

    def test_token_only_predicate_row_is_dropped(self):
        custom = scope_rows.ScopeRow(
            id="c2", scope=None, value=["x"], verbs=frozenset({Verb.READ}),
            operations=frozenset({"drive_get_file"}), predicate="upstream.approved_tool",
        )
        org_rules.load_from_config({"org_rules": [scope_rows.row_to_dict(custom)]})
        assert org_rules.rows() == []

    def test_rows_past_the_cap_are_dropped(self):
        many = [_row(f"f{i}") for i in range(admin_config.MAX_ORG_RULES + 5)]
        org_rules.load_from_config(_dicts(*many))
        assert len(org_rules.rows()) == admin_config.MAX_ORG_RULES

    def test_listener_registered_twice_is_called_once(self):
        calls = []
        cb = lambda: calls.append(1)  # noqa: E731
        org_rules.add_changed_listener(cb)
        org_rules.add_changed_listener(cb)
        org_rules.notify_changed()
        assert calls == [1]
        org_rules.remove_changed_listener(cb)
        org_rules.notify_changed()
        assert calls == [1]

    def test_a_failing_listener_does_not_stop_the_next(self):
        calls = []

        def bad():
            raise RuntimeError("boom")

        org_rules.add_changed_listener(bad)
        org_rules.add_changed_listener(lambda: calls.append(1))
        org_rules.notify_changed()
        assert calls == [1]

    def test_load_from_config_does_not_notify(self):
        calls = []
        org_rules.add_changed_listener(lambda: calls.append(1))
        org_rules.load_from_config(_dicts(_row()))
        assert calls == [] and len(org_rules.rows()) == 1

    def test_reload_reads_the_admin_config_file_and_notifies(self, tmp_path):
        path = tmp_path / "org" / "admin_config.json"
        path.parent.mkdir(parents=True)
        config = admin_config._default_config()
        config["org_rules"] = [scope_rows.row_to_dict(_row())]
        path.write_text(json.dumps(config), encoding="utf-8")
        calls = []
        org_rules.add_changed_listener(lambda: calls.append(1))
        org_rules.reload_from_admin_config()
        assert [r.id for r in org_rules.rows()] == [_row().id]
        assert calls == [1]
