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


class TestProtocolOneOneConstants:
    @pytest.mark.parametrize("value", ["sha256:" + "0" * 64, "sha256:" + "abcdef0123456789" * 4])
    def test_digest_accepts(self, value):
        assert c.DIGEST_RE.fullmatch(value)

    @pytest.mark.parametrize(
        "value",
        ["", "sha256:" + "0" * 63, "sha256:" + "0" * 65, "sha256:" + "G" * 64,
         "SHA256:" + "0" * 64, "sha256:" + "0" * 64 + "\n", "0" * 64],
    )
    def test_digest_rejects(self, value):
        assert not c.DIGEST_RE.fullmatch(value)

    @pytest.mark.parametrize("kind", ["a", "layout", "a-b_c", "a" + "b" * 40])
    def test_approval_kind_accepts(self, kind):
        assert c.APPROVAL_KIND_RE.fullmatch(kind)

    @pytest.mark.parametrize("kind", ["", "A", "1a", "_a", "-a", "a b", "a\n", "a" + "b" * 41])
    def test_approval_kind_rejects(self, kind):
        assert not c.APPROVAL_KIND_RE.fullmatch(kind)

    def test_output_types_map_to_dotted_lowercase_extensions(self):
        assert set(c.OUTPUT_TYPES) == {
            "application/json", "text/csv", "text/html", "text/plain", "text/markdown",
        }
        for exts in c.OUTPUT_TYPES.values():
            assert exts and all(e.startswith(".") and e == e.lower() for e in exts)
        assert c.OUTPUT_TYPES["text/html"] == (".html", ".htm")

    def test_default_output_types_are_supported(self):
        assert c.DEFAULT_OUTPUT_TYPES == ("application/json", "text/csv")
        assert set(c.DEFAULT_OUTPUT_TYPES) <= set(c.OUTPUT_TYPES)

    def test_page_budget_leaves_room_for_the_envelope(self):
        assert c.SOURCE_PAGE_BUDGET_BYTES == c.MAX_SOURCE_RESULT_BYTES - 64 * 1024
        assert c.OUTPUT_READ_PAGE_BYTES < c.INLINE_RESULT_BYTES

    def test_approval_request_has_a_timeout(self):
        assert c.TIMEOUT_SECONDS["approval.request"] == 5.0

    def test_audit_sources(self):
        assert (c.AUDIT_PLUGIN_APPROVAL, c.AUDIT_PLUGIN_OUTPUT) == ("plugin_approval", "plugin_output")

    def test_protocol_version_and_drive_cap_are_untouched(self):
        assert c.PROTOCOL_VERSION == "1.0.0"
        assert c.DRIVE_MAX_FILE_BYTES == 64 * 1024 * 1024
