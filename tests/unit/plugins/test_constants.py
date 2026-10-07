"""The plugin naming rules keep plugins from shadowing built-in connectors or each other.

A plugin name becomes the prefix of an MCP tool name and of a policy key, so the reserved set must
cover every built-in connector and the patterns must refuse anything that could break the
"first underscore splits plugin from tool" rule.
"""
from __future__ import annotations

import pytest

from privacyfence import settings_controller
from privacyfence.plugins import constants as c

pytestmark = pytest.mark.unit


class TestReservedNames:
    def test_cover_every_built_in_connector(self):
        assert set(settings_controller.ALL_CONNECTORS) <= c.RESERVED_PLUGIN_NAMES

    def test_reserved_names_include_the_product_words(self):
        assert {"privacyfence", "plugin", "plugins", "settings", "mcp"} <= c.RESERVED_PLUGIN_NAMES


class TestPatterns:
    @pytest.mark.parametrize("name", ["ab", "today", "a-b-c", "a1", "a" + "b" * 30])
    def test_plugin_name_accepts(self, name):
        assert c.PLUGIN_NAME_RE.fullmatch(name)

    @pytest.mark.parametrize(
        "name", ["a", "", "1abc", "Abc", "a_b", "a b", "ab\n", "-ab", "a" + "b" * 31, "é1"]
    )
    def test_plugin_name_rejects(self, name):
        assert not c.PLUGIN_NAME_RE.fullmatch(name)

    @pytest.mark.parametrize("name", ["ab", "get_page", "a1_b2", "a" + "b" * 40])
    def test_tool_name_accepts(self, name):
        assert c.TOOL_NAME_RE.fullmatch(name)

    @pytest.mark.parametrize("name", ["a", "Get", "get-page", "_ab", "1ab", "ab\n", "a" + "b" * 41])
    def test_tool_name_rejects(self, name):
        assert not c.TOOL_NAME_RE.fullmatch(name)

    @pytest.mark.parametrize("name", ["a", "customer", "a_b", "a" + "b" * 30])
    def test_scope_type_accepts(self, name):
        assert c.SCOPE_TYPE_RE.fullmatch(name)

    @pytest.mark.parametrize("name", ["", "A", "a-b", "_a", "a" + "b" * 31, "a\n"])
    def test_scope_type_rejects(self, name):
        assert not c.SCOPE_TYPE_RE.fullmatch(name)

    def test_plugin_names_never_contain_an_underscore(self):
        assert not c.PLUGIN_NAME_RE.fullmatch("my_plugin")


class TestNameHelpers:
    def test_mcp_tool_name(self):
        assert c.mcp_tool_name("today", "get_agenda") == "today_get_agenda"

    def test_first_underscore_splits_plugin_from_tool(self):
        plugin, _, tool = c.mcp_tool_name("my-plugin", "get_x").partition("_")
        assert (plugin, tool) == ("my-plugin", "get_x")

    def test_operation_key(self):
        assert c.operation_key("today", "get_agenda") == "plugin.today.get_agenda"

    def test_scope_predicate(self):
        assert c.scope_predicate("today", "calendar") == "plugin:today:calendar"


class TestTables:
    def test_error_codes_are_unique_integers(self):
        assert len(set(c.ERROR_CODES.values())) == len(c.ERROR_CODES)

    def test_every_timed_method_has_a_positive_timeout(self):
        assert all(v > 0 for v in c.TIMEOUT_SECONDS.values())

    def test_backoff_grows_and_covers_the_crash_limit(self):
        assert list(c.RESTART_BACKOFF_SECONDS) == sorted(c.RESTART_BACKOFF_SECONDS)
        assert len(c.RESTART_BACKOFF_SECONDS) >= c.CRASH_LIMIT

    def test_protocol_major_matches_version(self):
        assert int(c.PROTOCOL_VERSION.split(".")[0]) == c.PROTOCOL_MAJOR
