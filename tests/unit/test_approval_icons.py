"""Tests for approval_icons.py -- shared icon-asset loading for both the
native (AppKit) and web approval hosts. Pure filesystem + base64, no AppKit
-- importable and testable on any platform.
"""
from __future__ import annotations

from privacyfence import approval_icons


class TestShieldIcon:
    def test_shield_icon_path_resolves_to_a_real_bundled_file(self):
        path = approval_icons.shield_icon_path()
        assert path is not None
        assert path.endswith(".png")

    def test_shield_icon_data_uri_is_a_base64_png(self):
        uri = approval_icons.icon_data_uri(approval_icons.shield_icon_path())
        assert uri.startswith("data:image/png;base64,")


class TestConnectorIcon:
    def test_unknown_connector_returns_no_path(self):
        assert approval_icons.connector_icon_path("not-a-real-connector") is None

    def test_empty_connector_returns_no_path(self):
        assert approval_icons.connector_icon_path("") is None

    def test_gmail_connector_icon_resolves(self):
        # gmail.png ships in resources/connector_icons/ -- see that
        # directory's README.
        path = approval_icons.connector_icon_path("gmail")
        assert path is not None
        assert path.endswith("gmail.png")


class TestIconDataUri:
    def test_missing_path_is_empty_string_not_an_error(self):
        assert approval_icons.icon_data_uri(None) == ""

    def test_repeated_calls_are_cached_and_return_the_same_value(self):
        path = approval_icons.shield_icon_path()
        first = approval_icons.icon_data_uri(path)
        second = approval_icons.icon_data_uri(path)
        assert first == second
        assert path in approval_icons._icon_data_uri_cache


class TestAllConnectorIcons:
    """approval_list_html.py's live re-render needs every bundled icon
    available at first paint, not just the ones with something pending
    right then -- this is the whole bundled set it draws on."""

    def test_includes_every_bundled_connector(self):
        icons = approval_icons.all_connector_icons()
        assert "gmail" in icons
        assert "slack" in icons
        assert icons["gmail"].startswith("data:image/png;base64,")

    def test_matches_connector_icon_path_for_each_key(self):
        icons = approval_icons.all_connector_icons()
        for connector, uri in icons.items():
            assert uri == approval_icons.icon_data_uri(approval_icons.connector_icon_path(connector))

    def test_excludes_a_connector_with_no_bundled_icon(self):
        assert "not-a-real-connector" not in approval_icons.all_connector_icons()


class TestAgentIcons:
    """resources/agent_icons/ -- one bundled mark per agent_identity.REGISTRY
    entry (ADR 0035 decision 4), never fetched and never caller-supplied."""

    def test_every_registry_entry_has_a_bundled_png(self):
        from privacyfence.agent_identity import REGISTRY

        icons = approval_icons.all_agent_icons()
        assert set(icons) == {entry.agent_id for entry in REGISTRY}
        for agent_id, uri in icons.items():
            assert uri.startswith("data:image/png;base64,"), agent_id
            assert uri == approval_icons.icon_data_uri(approval_icons.agent_icon_path(agent_id))

    def test_empty_id_returns_no_path(self):
        # agent_label.AgentLabel.icon_id is "" for every non-attested tier.
        assert approval_icons.agent_icon_path("") is None

    def test_unknown_id_returns_no_path(self):
        assert approval_icons.agent_icon_path("unknown:claude") is None

    def test_no_bundled_directory_is_an_empty_set(self, monkeypatch, tmp_path):
        monkeypatch.setattr(approval_icons, "_RESOURCES", tmp_path)
        assert approval_icons.all_agent_icons() == {}
        assert approval_icons.all_connector_icons() == {}
