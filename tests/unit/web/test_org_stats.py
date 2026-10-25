"""Tests for web/org_stats.py: the admin's Stats page counts.

The invariant that matters most: the answer carries counts only. No principal id, address or
rule value may appear in it, whatever the files on disk hold.
"""
from __future__ import annotations

import json

import pytest
import yaml

from privacyfence import paths
from privacyfence.policy import catalogue as policy_catalogue
from privacyfence.policy import scope_rows
from privacyfence.web import org_stats

pytestmark = pytest.mark.unit

_ORG_CONFIG = {
    "google": {
        "client_id": "gcid", "client_secret": "gcsecret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
    },
    "slack": {"client_id": "scid", "client_secret": "scsecret"},
}


@pytest.fixture(autouse=True)
def _fake_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


def _user(tmp_path, principal_id: str, *, tokens=(), rules=None, settings_text: str | None = None):
    root = tmp_path / "users" / principal_id
    (root / "credentials").mkdir(parents=True, exist_ok=True)
    for token in tokens:
        (root / "credentials" / token).write_text("{}", encoding="utf-8")
    if rules is not None or settings_text is not None:
        config_dir = root / "authority" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        if settings_text is None:
            rows, _ = scope_rows.add_rules([], list(rules))
            settings_text = yaml.safe_dump({"auto_accept": scope_rows.rows_to_config(rows)})
        (config_dir / "settings.yaml").write_text(settings_text, encoding="utf-8")
    return root


def _rules(group: str, verb: str, value=None) -> list:
    return policy_catalogue.rules_for_catalogue_entry(group, value, policy_catalogue.parse_verbs([verb]))


def _connector(stats: dict, service: str) -> dict:
    return next(c for c in stats["connectors"] if c["service"] == service)


class TestEmptyInstall:
    def test_no_users_directory_counts_nothing_and_creates_nothing(self, tmp_path):
        stats = org_stats.collect(_ORG_CONFIG)
        assert stats["users"] == {"total": 0, "with_connectors": 0, "with_rules": 0}
        assert stats["rules"] == {"total": 0, "by_connector": []}
        assert not (tmp_path / "users").exists()

    def test_offered_connectors_are_listed_with_zero_users(self):
        stats = org_stats.collect(_ORG_CONFIG)
        assert _connector(stats, "gmail") == {"service": "gmail", "label": "Gmail", "configured": True, "users": 0}
        assert _connector(stats, "slack")["users"] == 0
        assert "salesforce" not in {c["service"] for c in stats["connectors"]}

    def test_empty_matches_an_empty_install(self):
        assert org_stats.collect({}) == org_stats.empty()


class TestUsersAndConnectors:
    def test_counts_users_and_connected_services(self, tmp_path):
        _user(tmp_path, "alice", tokens=["token.json", "slack_token.json"])
        _user(tmp_path, "bob", tokens=["token.json"])
        _user(tmp_path, "carol")
        stats = org_stats.collect(_ORG_CONFIG)
        assert stats["users"]["total"] == 3
        assert stats["users"]["with_connectors"] == 2
        assert _connector(stats, "gmail")["users"] == 2
        assert _connector(stats, "slack")["users"] == 1
        assert _connector(stats, "drive")["users"] == 0

    def test_a_connector_the_bundle_dropped_still_shows_its_users(self, tmp_path):
        _user(tmp_path, "alice", tokens=["salesforce_token.json"])
        stats = org_stats.collect(_ORG_CONFIG)
        assert _connector(stats, "salesforce") == {
            "service": "salesforce", "label": "Salesforce", "configured": False, "users": 1,
        }

    def test_entries_paths_would_never_produce_are_skipped(self, tmp_path):
        (tmp_path / "users").mkdir()
        (tmp_path / "users" / "stray-file").write_text("", encoding="utf-8")
        (tmp_path / "users" / "bad name").mkdir()
        _user(tmp_path, "alice")
        assert org_stats.collect(_ORG_CONFIG)["users"]["total"] == 1


class TestRules:
    def test_counts_rules_and_their_users_per_connector(self, tmp_path):
        _user(tmp_path, "alice", rules=_rules("gmail.sender_domain", "read", ["example.com"]) + _rules("drive.folder", "read", ["folder-a"]))
        _user(tmp_path, "bob", rules=_rules("drive.folder", "read", ["folder-b"]))
        stats = org_stats.collect(_ORG_CONFIG)
        assert stats["users"]["with_rules"] == 2
        assert stats["rules"]["total"] == 3
        assert stats["rules"]["by_connector"] == [
            {"connector": "drive", "label": "Drive", "rules": 2, "users": 2},
            {"connector": "gmail", "label": "Gmail", "rules": 1, "users": 1},
        ]

    def test_an_unreadable_settings_file_counts_as_no_rules(self, tmp_path, caplog):
        """One damaged per-user file must cost the admin that user's rules, not the whole page."""
        _user(tmp_path, "alice", settings_text="auto_accept: [unclosed\n")
        _user(tmp_path, "bob", rules=_rules("drive.folder", "read", ["folder-b"]))
        stats = org_stats.collect(_ORG_CONFIG)
        assert stats["users"]["total"] == 2
        assert stats["rules"]["total"] == 1
        assert "Could not read auto-accept rules" in caplog.text

    def test_the_answer_never_carries_ids_addresses_or_rule_values(self, tmp_path):
        _user(
            tmp_path, "alice@example.com", tokens=["token.json"],
            rules=_rules("gmail.sender_domain", "read", ["secret-partner.com"]) + _rules("drive.folder", "read", ["folder-xyz"]),
        )
        text = json.dumps(org_stats.collect(_ORG_CONFIG))
        for leaked in ("alice", "example.com", "secret-partner.com", "folder-xyz"):
            assert leaked not in text
