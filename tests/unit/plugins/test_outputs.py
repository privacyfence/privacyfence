"""Unit tests for privacyfence.plugins.outputs: what a plugin's output folder publishes, the
canonical-path rule, paging, and the two tools over it.

The gate is spied on (``gated_call_spy``) where only the arguments sent into it matter, and is the
real ``gate.gated_call`` with stubbed popups where a policy rule decides (``TestRuleAllowsFolder``).
"""
from __future__ import annotations

import hashlib
import json
import sys

import pytest
import yaml

from privacyfence import approval_ui, auto_accept, gate
from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.plugins import cursors
from privacyfence.plugins import outputs as outputs_module
from privacyfence.plugins.constants import (
    AUDIT_PLUGIN_OUTPUT,
    OUTPUT_LIST_PAGE,
    OUTPUT_MAX_DEPTH,
    OUTPUT_READ_PAGE_BYTES,
)
from privacyfence.plugins.outputs import (
    NO_SUCH_FILE,
    PluginOutputsConnector,
    list_outputs,
    read_output,
)
from privacyfence.policy import propose, scopes
from privacyfence.policy.registry import TOOL_REGISTRY
from privacyfence.web_approval_ui import WebApprovalUI

from ...helpers import assert_all_tools_leave_an_audit_trail, policy_rules

pytestmark = pytest.mark.unit

TYPES = ("application/json", "text/csv")
symlinks = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


def put(root, rel, data="x"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


@pytest.fixture
def root(tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    return out


def paths_of(root, **kw):
    return [f.path for f in list_outputs(root, TYPES, **kw)[0]]


def make_connector(root, name="today"):
    table = {name: ("Today", root, TYPES)}
    return PluginOutputsConnector(lambda: table)


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls: list[dict] = []

    async def spy(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(outputs_module, "gated_call", spy)
    return calls


@pytest.fixture
def audit_dir(tmp_path):
    init_audit_logger(str(tmp_path / "audit"))
    return tmp_path / "audit"


def audit_entries(audit_dir):
    file = audit_dir / f"{current_week()}.jsonl"
    if not file.exists():
        return []
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]


class TestVisibility:
    def test_regular_files_with_known_extensions_are_published(self, root):
        put(root, "a.csv")
        put(root, "reports/b.json")

        assert paths_of(root) == ["a.csv", "reports/b.json"]

    def test_dot_files_and_dot_directories_are_hidden(self, root):
        put(root, ".hidden.csv")
        put(root, ".dir/a.csv")
        put(root, "ok/.b.csv")

        assert paths_of(root) == []

    def test_tmp_files_are_hidden(self, root):
        put(root, ".report.csv.tmp")
        put(root, "report.csv.tmp")

        assert paths_of(root) == []

    def test_wrong_extension_is_hidden(self, root):
        put(root, "a.txt")
        put(root, "b.md")
        put(root, "noext")

        assert paths_of(root) == []

    def test_extensions_follow_the_plugins_declared_types(self, root):
        put(root, "a.txt")

        files, _ = list_outputs(root, ("text/plain",))
        assert [(f.path, f.mime_type) for f in files] == [("a.txt", "text/plain")]

    def test_unknown_declared_type_publishes_nothing(self, root):
        put(root, "a.csv")

        assert list_outputs(root, ("application/zip",)) == ([], None)

    def test_extension_match_ignores_case(self, root):
        put(root, "A.CSV")

        assert paths_of(root) == ["A.CSV"]

    @symlinks
    def test_symlink_to_a_file_is_hidden(self, root, tmp_path):
        target = put(tmp_path, "elsewhere.csv")
        (root / "link.csv").symlink_to(target)
        put(root, "real.csv")
        (root / "alias.csv").symlink_to(root / "real.csv")

        assert paths_of(root) == ["real.csv"]

    @symlinks
    def test_symlink_to_a_directory_is_not_followed(self, root, tmp_path):
        put(tmp_path / "other", "x.csv")
        (root / "dir").symlink_to(tmp_path / "other", target_is_directory=True)
        put(root, "real/y.csv")
        (root / "alias").symlink_to(root / "real", target_is_directory=True)

        assert paths_of(root) == ["real/y.csv"]

    def test_depth_is_limited(self, root):
        ok = "/".join(["d"] * (OUTPUT_MAX_DEPTH - 1)) + "/f.csv"
        too_deep = "/".join(["d"] * OUTPUT_MAX_DEPTH) + "/f.csv"
        put(root, ok)
        put(root, too_deep)

        assert paths_of(root) == [ok]

    def test_a_file_outside_the_root_is_never_published(self, root, tmp_path):
        put(tmp_path, "outside.csv")

        assert paths_of(root) == []
        with pytest.raises(ValueError, match=NO_SUCH_FILE):
            read_output(root, TYPES, "../outside.csv")

    def test_missing_root_publishes_nothing(self, tmp_path):
        assert list_outputs(tmp_path / "gone", TYPES) == ([], None)

    def test_a_directory_named_like_a_file_is_not_published(self, root):
        (root / "dir.csv").mkdir()

        assert paths_of(root) == []

    def test_files_report_size_modified_and_type(self, root):
        put(root, "a.csv", "12345")

        [f] = list_outputs(root, TYPES)[0]
        assert (f.path, f.size, f.mime_type) == ("a.csv", 5, "text/csv")
        assert f.modified.endswith("Z") and "T" in f.modified


class TestCanonicalPath:
    @pytest.mark.parametrize("path", [
        "a\\..\\b.csv", "a//b.csv", "/a.csv", "a/./b.csv", "C:x.csv", "a/b.csv/", "a/../a/b.csv",
        "a/.b.csv", ".a/b.csv", "", "a\0b.csv", "a\\b.csv", "a/", "reports", "a/b.txt",
    ])
    def test_non_canonical_spellings_are_not_files(self, root, path):
        put(root, "a/b.csv")
        put(root, "a.csv")

        with pytest.raises(ValueError) as err:
            read_output(root, TYPES, path)
        assert str(err.value) == NO_SUCH_FILE

    def test_the_canonical_spelling_is_read(self, root):
        put(root, "a/b.csv", "hello")

        assert read_output(root, TYPES, "a/b.csv")["text"] == "hello"

    def test_non_string_path_is_refused(self, root):
        with pytest.raises(ValueError, match=NO_SUCH_FILE):
            read_output(root, TYPES, None)  # type: ignore[arg-type]

    def test_too_deep_is_refused(self, root):
        deep = "/".join(["d"] * OUTPUT_MAX_DEPTH) + "/f.csv"
        put(root, deep)

        with pytest.raises(ValueError, match=NO_SUCH_FILE):
            read_output(root, TYPES, deep)

    def test_missing_file_and_directory_are_refused(self, root):
        put(root, "a/b.csv")

        for path in ("a/c.csv", "a", "nope/b.csv"):
            with pytest.raises(ValueError, match=NO_SUCH_FILE):
                read_output(root, TYPES, path)

    def test_a_directory_named_like_a_file_is_refused(self, root):
        (root / "dir.csv").mkdir()

        with pytest.raises(ValueError, match=NO_SUCH_FILE):
            read_output(root, TYPES, "dir.csv")

    @symlinks
    def test_symlinks_anywhere_on_the_way_are_refused(self, root, tmp_path):
        target = put(tmp_path / "other", "x.csv")
        (root / "link.csv").symlink_to(target)
        (root / "dir").symlink_to(tmp_path / "other", target_is_directory=True)

        for path in ("link.csv", "dir/x.csv"):
            with pytest.raises(ValueError, match=NO_SUCH_FILE):
                read_output(root, TYPES, path)

    @symlinks
    def test_a_symlinked_root_is_fine_when_the_path_is_canonical(self, tmp_path):
        real = tmp_path / "real"
        put(real, "a.csv", "ok")
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        assert read_output(link, TYPES, "a.csv")["text"] == "ok"

    def test_a_differing_case_is_not_a_second_spelling(self, root):
        put(root, "Reports/a.csv")

        for path in ("reports/a.csv", "Reports/A.csv"):
            try:
                read_output(root, TYPES, path)
            except ValueError as exc:
                assert str(exc) == NO_SUCH_FILE
            else:           # a case-insensitive filesystem resolved it to the same file
                assert (root / path).resolve().relative_to(root.resolve()).as_posix() == path


class TestList:
    def test_sorted_by_path(self, root):
        for rel in ("b.csv", "a/z.csv", "a/b.csv", "A.csv"):
            put(root, rel)

        assert paths_of(root) == ["A.csv", "a/b.csv", "a/z.csv", "b.csv"]

    def test_prefix_filters(self, root):
        for rel in ("reports/a.csv", "reports2/a.csv", "other.csv"):
            put(root, rel)

        assert paths_of(root, prefix="reports/") == ["reports/a.csv"]
        assert paths_of(root, prefix="rep") == ["reports/a.csv", "reports2/a.csv"]

    def test_after_skips_up_to_and_including_the_path(self, root):
        for rel in ("a.csv", "b.csv", "c.csv"):
            put(root, rel)

        assert paths_of(root, after="b.csv") == ["c.csv"]

    def test_limit_returns_a_continuation_only_when_more_follow(self, root):
        for rel in ("a.csv", "b.csv", "c.csv"):
            put(root, rel)

        files, nxt = list_outputs(root, TYPES, limit=2)
        assert ([f.path for f in files], nxt) == (["a.csv", "b.csv"], "b.csv")
        files, nxt = list_outputs(root, TYPES, limit=3)
        assert ([f.path for f in files], nxt) == (["a.csv", "b.csv", "c.csv"], None)
        files, nxt = list_outputs(root, TYPES, after="b.csv", limit=2)
        assert ([f.path for f in files], nxt) == (["c.csv"], None)

    async def test_the_tool_pages_at_the_page_size(self, root):
        for i in range(OUTPUT_LIST_PAGE + 5):
            put(root, f"f{i:04}.csv")
        conn = make_connector(root)

        first = await conn.call("plugin_outputs_list", {"plugin": "today"})
        second = await conn.call("plugin_outputs_list", {"plugin": "today", "cursor": first["next_cursor"]})

        assert len(first["files"]) == OUTPUT_LIST_PAGE
        assert first["next_cursor"]
        assert [f["path"] for f in second["files"]] == [f"f{i:04}.csv" for i in range(OUTPUT_LIST_PAGE, OUTPUT_LIST_PAGE + 5)]
        assert second["next_cursor"] is None

    async def test_a_cursor_is_bound_to_plugin_and_prefix(self, root):
        for i in range(OUTPUT_LIST_PAGE + 1):
            put(root, f"f{i:04}.csv")
        conn = make_connector(root)
        first = await conn.call("plugin_outputs_list", {"plugin": "today"})

        with pytest.raises(cursors.CursorError, match="different call"):
            await conn.call("plugin_outputs_list", {"plugin": "today", "prefix": "f", "cursor": first["next_cursor"]})
        with pytest.raises(cursors.CursorError, match="not valid"):
            await conn.call("plugin_outputs_list", {"plugin": "today", "cursor": "junk"})

    async def test_a_cursor_without_a_path_is_not_valid(self, root):
        put(root, "a.csv")
        cursor = cursors.encode("plugin_outputs.list", {"plugin": "today", "prefix": ""}, {"a": 5})

        with pytest.raises(cursors.CursorError, match="not valid"):
            await make_connector(root).call("plugin_outputs_list", {"plugin": "today", "cursor": cursor})


class TestRead:
    def test_small_file_in_one_page(self, root):
        put(root, "a.csv", "a,b\n1,2\n")

        out = read_output(root, TYPES, "a.csv")

        assert out == {
            "path": "a.csv", "mime_type": "text/csv", "size": 8,
            "sha256": hashlib.sha256(b"a,b\n1,2\n").hexdigest(),
            "offset": 0, "length": 8, "next_offset": None, "text": "a,b\n1,2\n",
        }

    def test_pages_chain_and_reassemble_the_file(self, root):
        data = "".join(f"line {i}\n" for i in range(50))
        put(root, "a.csv", data)

        text, offset, digests = "", 0, set()
        while offset is not None:
            out = read_output(root, TYPES, "a.csv", offset=offset, max_bytes=40)
            digests.add(out["sha256"])
            assert out["offset"] == (offset or 0)
            text += out["text"]
            offset = out["next_offset"]

        assert text == data
        assert digests == {hashlib.sha256(data.encode()).hexdigest()}

    def test_default_page_is_the_page_constant(self, root):
        put(root, "a.csv", "x" * (OUTPUT_READ_PAGE_BYTES + 10))

        out = read_output(root, TYPES, "a.csv")

        assert out["length"] == OUTPUT_READ_PAGE_BYTES
        assert out["next_offset"] == OUTPUT_READ_PAGE_BYTES

    def test_page_ends_on_a_utf8_boundary(self, root):
        data = "é" * 10        # two bytes each
        put(root, "a.csv", data)

        out = read_output(root, TYPES, "a.csv", max_bytes=5)

        assert (out["text"], out["length"], out["next_offset"]) == ("éé", 4, 4)
        rest = read_output(root, TYPES, "a.csv", offset=4, max_bytes=100)
        assert rest["text"] == "é" * 8 and rest["next_offset"] is None

    @pytest.mark.parametrize("char", ["é", "€", "😀"])
    def test_every_cut_point_is_a_character_boundary(self, root, char):
        data = char * 6
        put(root, "a.csv", data)
        raw = data.encode()

        for cut in range(len(char.encode()), len(raw)):
            out = read_output(root, TYPES, "a.csv", max_bytes=cut)
            assert out["text"] == raw[:out["length"]].decode("utf-8")
            assert out["length"] % len(char.encode()) == 0

    def test_a_page_too_small_for_one_character_still_advances(self, root):
        put(root, "a.csv", "😀")

        out = read_output(root, TYPES, "a.csv", max_bytes=2)

        assert out["length"] == 2 and out["next_offset"] == 2

    def test_ascii_tail_is_not_backed_off(self, root):
        put(root, "a.csv", "aéb")

        assert read_output(root, TYPES, "a.csv", max_bytes=1)["text"] == "a"

    def test_last_page_keeps_a_trailing_partial_character(self, root):
        put(root, "a.csv", b"ab\xc3")

        out = read_output(root, TYPES, "a.csv")

        assert out["length"] == 3 and out["next_offset"] is None

    def test_sha256_covers_the_whole_file_not_the_page(self, root):
        put(root, "a.csv", "abcdef")

        out = read_output(root, TYPES, "a.csv", offset=2, max_bytes=2)

        assert out["text"] == "cd"
        assert out["sha256"] == hashlib.sha256(b"abcdef").hexdigest()
        assert out["size"] == 6 and out["next_offset"] == 4

    def test_a_file_larger_than_the_hash_chunk(self, root, monkeypatch):
        monkeypatch.setattr(outputs_module, "_HASH_CHUNK", 7)
        data = "0123456789" * 5
        put(root, "a.csv", data)

        out = read_output(root, TYPES, "a.csv", offset=12, max_bytes=20)

        assert out["text"] == data[12:32]
        assert out["sha256"] == hashlib.sha256(data.encode()).hexdigest()

    def test_offset_past_the_end_is_refused(self, root):
        put(root, "a.csv", "abc")

        for offset in (3, 4, 100):
            with pytest.raises(ValueError, match="past the end"):
                read_output(root, TYPES, "a.csv", offset=offset)

    def test_negative_offset_is_refused(self, root):
        put(root, "a.csv", "abc")

        with pytest.raises(ValueError, match="negative"):
            read_output(root, TYPES, "a.csv", offset=-1)

    def test_empty_file_reads_at_offset_zero(self, root):
        put(root, "a.csv", "")

        out = read_output(root, TYPES, "a.csv")

        assert (out["text"], out["length"], out["next_offset"], out["size"]) == ("", 0, None, 0)

    def test_invalid_utf8_is_replaced_not_raised(self, root):
        put(root, "a.csv", b"a\xffb")

        assert read_output(root, TYPES, "a.csv")["text"] == "a�b"

    def test_a_file_that_vanishes_is_no_such_file(self, root, monkeypatch):
        put(root, "a.csv")
        monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))

        with pytest.raises(ValueError, match=NO_SUCH_FILE):
            read_output(root, TYPES, "a.csv")


class TestTools:
    def test_specs(self, root):
        specs = {s.name: s for s in make_connector(root).tool_specs()}

        assert set(specs) == {"plugin_outputs_list", "plugin_outputs_read"}
        assert all(s.read_only and not s.destructive for s in specs.values())
        assert [p.name for p in specs["plugin_outputs_list"].params] == ["plugin", "prefix", "cursor"]
        assert [p.name for p in specs["plugin_outputs_read"].params] == ["plugin", "path", "offset", "reason"]
        reason = specs["plugin_outputs_read"].params[-1]
        assert reason.required and reason.description == "One sentence: why are you calling this tool right now?"
        assert specs["plugin_outputs_list"].description.endswith("Runs without asking.")
        assert specs["plugin_outputs_read"].description.endswith("unless a rule allows this folder.")

    def test_name(self, root):
        assert make_connector(root).name == "plugin_outputs"

    async def test_unknown_tool(self, root):
        with pytest.raises(ValueError, match="Unknown plugin output tool"):
            await make_connector(root).call("plugin_outputs_write", {})

    @pytest.mark.parametrize("tool,args", [
        ("plugin_outputs_list", {"plugin": "ghost"}),
        ("plugin_outputs_read", {"plugin": "ghost", "path": "a.csv"}),
        ("plugin_outputs_read", {"plugin": None, "path": "a.csv"}),
    ])
    async def test_unknown_plugin(self, root, tool, args):
        with pytest.raises(RuntimeError) as err:
            await make_connector(root).call(tool, args)
        assert str(err.value) == f"No plugin named {args['plugin']} publishes outputs."

    async def test_list_is_auto_and_audited(self, root, audit_dir, gated_call_spy):
        put(root, "a.csv", "12")
        put(root, "r/b.json", "{}")

        result = await make_connector(root).call("plugin_outputs_list", {"plugin": "today"})

        assert result["plugin"] == "today" and result["next_cursor"] is None
        assert [f["path"] for f in result["files"]] == ["a.csv", "r/b.json"]
        assert set(result["files"][0]) == {"path", "size", "modified", "mime_type"}
        assert gated_call_spy == []
        [entry] = audit_entries(audit_dir)
        assert (entry["connector"], entry["tool"], entry["decision"], entry["auto_accept_rule"]) == (
            "plugin_outputs", "plugin_outputs_list", "auto_accepted", "auto",
        )

    async def test_list_prefix_is_applied(self, root):
        put(root, "a.csv")
        put(root, "r/b.csv")

        result = await make_connector(root).call("plugin_outputs_list", {"plugin": "today", "prefix": "r/"})

        assert [f["path"] for f in result["files"]] == ["r/b.csv"]

    async def test_read_goes_through_gated_call_with_metadata_only_preview(self, root, audit_dir, gated_call_spy):
        put(root, "reports/q3.csv", "secret,1\n")

        result = await make_connector(root).call("plugin_outputs_read", {"plugin": "today", "path": "reports/q3.csv"})

        [call] = gated_call_spy
        assert call["connector"] == "plugin_outputs" and call["tool"] == "plugin_outputs_read"
        assert call["gate"] == "review"
        assert call["tool_name"] == "Read Today output" and call["summary"] == "reports/q3.csv"
        assert call["raw_data"] == {"plugin": "today", "path": "reports/q3.csv"}
        assert call["preview"] == {"Plugin": "Today", "File": "reports/q3.csv", "Size": "9 bytes", "Part": "0-9"}
        assert "secret" not in json.dumps(call["preview"])
        assert call["details_text"] == "secret,1\n" and call["pii_scan_text"] == "secret,1\n"
        assert call["args"] == {"plugin": "today", "path": "reports/q3.csv", "offset": 0}
        assert call["filtered_data"] is result
        assert result["plugin"] == "today" and result["text"] == "secret,1\n"

    async def test_read_part_shows_the_byte_range(self, root, gated_call_spy):
        put(root, "a.csv", "abcdef")

        await make_connector(root).call("plugin_outputs_read", {"plugin": "today", "path": "a.csv", "offset": 2})

        assert gated_call_spy[0]["preview"]["Part"] == "2-6"
        assert gated_call_spy[0]["args"]["offset"] == 2

    async def test_read_attributes_the_read_to_the_plugin(self, root, audit_dir, gated_call_spy):
        put(root, "a.csv", "abcdef")

        await make_connector(root).call("plugin_outputs_read", {"plugin": "today", "path": "a.csv", "offset": 2})

        [entry] = audit_entries(audit_dir)
        assert entry["connector"] == "plugin:today" and entry["decision"] == AUDIT_PLUGIN_OUTPUT
        assert entry["summary"] == "read a.csv; offset=2; bytes=4"

    async def test_a_denied_read_leaves_no_attribution_entry(self, root, audit_dir, monkeypatch):
        put(root, "a.csv")

        async def deny(**kwargs):
            raise gate.GateDeniedError("denied")

        monkeypatch.setattr(outputs_module, "gated_call", deny)

        with pytest.raises(gate.GateDeniedError):
            await make_connector(root).call("plugin_outputs_read", {"plugin": "today", "path": "a.csv"})
        assert audit_entries(audit_dir) == []

    async def test_a_bad_path_never_reaches_the_gate(self, root, gated_call_spy):
        put(root, "a/b.csv")

        with pytest.raises(ValueError, match=NO_SUCH_FILE):
            await make_connector(root).call("plugin_outputs_read", {"plugin": "today", "path": "a/./b.csv"})
        assert gated_call_spy == []

    async def test_audit_failure_does_not_fail_the_call(self, root, gated_call_spy, monkeypatch):
        put(root, "a.csv")

        def boom():
            raise RuntimeError("no logger")

        monkeypatch.setattr(outputs_module, "get_audit_logger", boom)

        result = await make_connector(root).call("plugin_outputs_read", {"plugin": "today", "path": "a.csv"})
        assert result["text"] == "x"

    async def test_every_tool_leaves_an_audit_trail(self, root, tmp_path, monkeypatch):
        put(root, "a.csv")
        conn = make_connector(root, name="stub")

        await assert_all_tools_leave_an_audit_trail(
            conn, outputs_module, monkeypatch, tmp_path / "trail",
            arg_overrides={"plugin_outputs_read": {"path": "a.csv"}},
        )


class Popups:
    def __init__(self, monkeypatch, decision="accept"):
        self.decision = decision
        self.read = []
        monkeypatch.setattr(gate, "show_read_popup", self._read)
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

    def _read(self, *args, **kwargs):
        self.read.append((args, kwargs))
        return self.decision, None


class TestRuleAllowsFolder:
    @pytest.fixture
    def env(self, root, tmp_path, monkeypatch):
        init_audit_logger(str(tmp_path / "audit"))
        reg = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=reg))
        settings = tmp_path / "settings.yaml"
        settings.write_text(yaml.dump({}), encoding="utf-8")
        auto_accept.init_config_path(str(settings))
        conn = make_connector(root)
        conn.register()
        yield conn, Popups(monkeypatch), tmp_path / "audit"
        conn.unregister()

    def allow(self, value):
        auto_accept.add_policy_v2_rules(policy_rules({
            "plugin_outputs.read": [{"predicate": "plugin:today:output", "value": value}],
        }))

    async def test_a_read_under_the_folder_is_released_without_a_card(self, root, env):
        conn, popups, audit_dir = env
        put(root, "reports/2026/q3.csv", "data")
        self.allow(["reports/"])

        result = await conn.call("plugin_outputs_read", {"plugin": "today", "path": "reports/2026/q3.csv"})

        assert result["text"] == "data"
        assert popups.read == []
        decisions = [e["decision"] for e in audit_entries(audit_dir)]
        assert "auto_accepted" in decisions and AUDIT_PLUGIN_OUTPUT in decisions

    async def test_a_read_outside_the_folder_shows_a_card(self, root, env):
        conn, popups, _ = env
        put(root, "reports2/q3.csv", "data")
        put(root, "other.csv", "data")
        self.allow(["reports/"])

        await conn.call("plugin_outputs_read", {"plugin": "today", "path": "reports2/q3.csv"})
        await conn.call("plugin_outputs_read", {"plugin": "today", "path": "other.csv"})

        assert len(popups.read) == 2

    async def test_a_file_rule_covers_only_that_file(self, root, env):
        conn, popups, _ = env
        put(root, "a.csv")
        put(root, "b.csv")
        self.allow(["a.csv"])

        await conn.call("plugin_outputs_read", {"plugin": "today", "path": "a.csv"})
        assert popups.read == []
        await conn.call("plugin_outputs_read", {"plugin": "today", "path": "b.csv"})
        assert len(popups.read) == 1

    async def test_a_non_canonical_spelling_never_reaches_the_rule(self, root, env):
        conn, popups, _ = env
        put(root, "reports/q3.csv")
        put(root, "other/x.csv")
        self.allow(["reports/"])

        with pytest.raises(ValueError, match=NO_SUCH_FILE):
            await conn.call("plugin_outputs_read", {"plugin": "today", "path": "reports/../other/x.csv"})
        assert popups.read == []

    async def test_a_denied_card_denies_the_read(self, root, env):
        conn, popups, _ = env
        put(root, "a.csv")
        popups.decision = "deny"

        with pytest.raises(gate.GateDeniedError):
            await conn.call("plugin_outputs_read", {"plugin": "today", "path": "a.csv"})


class TestRegistration:
    def snapshot(self):
        return (
            dict(auto_accept.TOOL_TO_GATE), dict(auto_accept.TOOL_TO_OPERATION), set(TOOL_REGISTRY),
            set(scopes.NEW_SCOPE_SELECTORS),
            {tool: list(propose._dynamic_entries_for(tool)) for tool in ("plugin_outputs_read",)},
        )

    def test_register_adds_tools_selectors_and_proposals(self, root):
        conn = make_connector(root)
        conn.register()
        try:
            assert auto_accept.TOOL_TO_GATE["plugin_outputs_list"] == "auto"
            assert auto_accept.TOOL_TO_GATE["plugin_outputs_read"] == "review"
            assert auto_accept.TOOL_TO_OPERATION["plugin_outputs_read"] == "plugin_outputs.read"
            assert "plugin:today:output" in scopes.NEW_SCOPE_SELECTORS
            assert [e.predicate for e in propose._dynamic_entries_for("plugin_outputs_read")] == ["plugin:today:output"]
        finally:
            conn.unregister()

    def test_unregister_restores_the_tables(self, root):
        before = self.snapshot()
        conn = make_connector(root)

        conn.register()
        assert self.snapshot() != before
        conn.unregister()

        assert self.snapshot() == before

    def test_register_reconciles_with_the_current_plugins(self, root):
        table = {"today": ("Today", root, TYPES), "other": ("Other", root, TYPES)}
        conn = PluginOutputsConnector(lambda: table)
        before = self.snapshot()
        conn.register()
        try:
            assert {"plugin:today:output", "plugin:other:output"} <= set(scopes.NEW_SCOPE_SELECTORS)
            del table["other"]
            conn.register()
            assert "plugin:other:output" not in scopes.NEW_SCOPE_SELECTORS
            assert "plugin:today:output" in scopes.NEW_SCOPE_SELECTORS
            assert [e.predicate for e in propose._dynamic_entries_for("plugin_outputs_read")] == ["plugin:today:output"]
        finally:
            conn.unregister()
        assert self.snapshot() == before

    def test_unregister_without_register_is_a_no_op(self, root):
        before = self.snapshot()

        make_connector(root).unregister()

        assert self.snapshot() == before
