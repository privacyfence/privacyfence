"""Unit tests for privacyfence.connectors.peek.PeekConnector."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import peek as peek_module
from privacyfence.connectors.peek import PeekConnector
from privacyfence.peek_client import (
    PeekClient,
    PeekClientError,
    PeekComment,
    PeekPage,
    PeekRenameResult,
    PeekStats,
    PeekUploadResult,
    PeekVisit,
)

from ...helpers import (
    assert_all_tools_leave_an_audit_trail,
    assert_no_placeholder_fields,
    assert_tool_definitions_complete,
)

pytestmark = pytest.mark.unit


def make_connector():
    client = MagicMock()
    client.host = "peek.example.com"
    return PeekConnector(client), client


def make_page(slug: str, created_at: str = "2026-01-01T00:00:00Z") -> PeekPage:
    return PeekPage(
        slug=slug, name=f"{slug}.html", owner="ann", size_bytes=120, visibility="private",
        url=f"https://peek.example.com/p/{slug}", created_at=created_at, version=2,
    )


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(peek_module, "gated_call", fake_gated_call)
    return calls


class TestHelpers:
    def test_client_property(self):
        connector, client = make_connector()
        assert connector.client is client

    async def test_audit_failure_does_not_break_the_call(self, monkeypatch):
        connector, client = make_connector()
        client.list_pages.return_value = []

        def boom():
            raise OSError("disk full")

        monkeypatch.setattr(peek_module, "get_audit_logger", boom)
        assert await connector.call("peek_list_pages", {}) == []


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Peek tool"):
            await connector.call("peek_does_not_exist", {})


PEEK_SIBLINGS: dict[str, tuple[str, ...]] = {
    "peek_list_pages": ("peek_get_comments", "peek_get_stats"),
    "peek_get_comments": ("peek_add_comment",),
    "peek_get_stats": ("peek_get_comments",),
    "peek_upload_page": ("peek_set_visibility",),
    "peek_set_visibility": ("peek_upload_page",),
    "peek_add_comment": ("peek_get_comments",),
    "peek_delete_page": ("peek_list_pages",),
    "peek_rename_page": ("peek_list_pages",),
}


class TestToolDefinitions:
    def test_every_tool_definition_is_complete(self):
        assert_tool_definitions_complete(PeekConnector(None), PEEK_SIBLINGS)


class TestListPages:
    async def test_returns_pages_without_gating_and_audits(self, tmp_path, gated_call_spy):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_pages.return_value = [make_page("a"), make_page("b")]

        result = await connector.call("peek_list_pages", {})

        assert [p["slug"] for p in result] == ["a", "b"]
        assert set(result[0]) == {
            "slug", "name", "owner", "size_bytes", "visibility", "url", "created_at", "version",
        }
        assert gated_call_spy == []
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(entries) == 1
        assert '"decision": "auto_accepted"' in entries[0]
        assert '"tool": "peek_list_pages"' in entries[0]

    async def test_truncates_to_max_results(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_pages.return_value = [make_page(s) for s in "abc"]

        result = await connector.call("peek_list_pages", {"max_results": 2})

        assert [p["slug"] for p in result] == ["a", "b"]

    @pytest.mark.parametrize("bad", [0, 201])
    async def test_max_results_out_of_range_raises(self, bad):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="max_results must be between 1 and 200."):
            await connector.call("peek_list_pages", {"max_results": bad})
        client.list_pages.assert_not_called()

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_pages.side_effect = PeekClientError("Peek refused the saved sign-in")
        with pytest.raises(RuntimeError, match="Peek refused the saved sign-in"):
            await connector.call("peek_list_pages", {})


def make_comment(kind="page", selector="", element_text="", body="Looks good", cid=1):
    return PeekComment(
        id=cid, author="bob", body=body, anchor_kind=kind, selector=selector,
        element_text=element_text, created_at="2026-10-10T11:07:00Z", version=2,
    )


class TestGetComments:
    async def test_gates_with_minimal_preview_and_full_details(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_comments.return_value = [
            make_comment("element", "#hero", "", "Fix the title", 1),
            make_comment("text", "p", "Quoted", "Typo here", 2),
        ]

        result = await connector.call("peek_get_comments", {"slug": "slug1"})

        call = gated_call_spy[0]
        assert call["gate"] == "review"
        assert call["tool"] == "peek_get_comments"
        assert list(call["preview"]) == ["Server", "Page", "Link", "Version"]
        assert call["preview"]["Server"] == "peek.example.com"
        assert call["preview"]["Version"] == "2 of 2"
        assert "Fix the title" not in str(call["preview"])
        assert "bob (Element #hero): Fix the title" in call["details_text"]
        assert "Text \u201cQuoted\u201d" in call["details_text"]
        assert call["pii_scan_text"] == "Fix the title\nTypo here\nQuoted"
        assert call["preview_tables"][0]["headers"] == ["Author", "Date", "Anchored to", "Comment"]
        assert call["table_only"] is True
        assert call["args"] == {"slug": "slug1", "version": 0}
        assert result["version"] == 2
        assert [c["id"] for c in result["comments"]] == [1, 2]

    async def test_earlier_version(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_comments.return_value = []

        result = await connector.call("peek_get_comments", {"slug": "slug1", "version": 1})

        assert gated_call_spy[0]["preview"]["Version"] == "1 of 2"
        assert gated_call_spy[0]["args"] == {"slug": "slug1", "version": 1}
        assert result["version"] == 1

    async def test_no_comments(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_comments.return_value = []

        await connector.call("peek_get_comments", {"slug": "slug1"})

        assert gated_call_spy[0]["details_text"] == "(no comments)"
        assert gated_call_spy[0]["preview_tables"] == []

    @pytest.mark.parametrize("kind,selector,text,expected", [
        ("page", "", "", "Whole page"),
        ("element", "#a", "", "Element #a"),
        ("text", "p", "hi", "Text \u201chi\u201d"),
    ])
    async def test_anchor_kinds(self, gated_call_spy, kind, selector, text, expected):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_comments.return_value = [make_comment(kind, selector, text)]

        await connector.call("peek_get_comments", {"slug": "slug1"})

        assert expected in gated_call_spy[0]["details_text"]
        assert gated_call_spy[0]["preview_tables"][0]["rows"][0][2] == expected

    async def test_negative_version_raises(self, gated_call_spy):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="version must be 0"):
            await connector.call("peek_get_comments", {"slug": "slug1", "version": -1})
        assert gated_call_spy == []
        client.get_page.assert_not_called()

    async def test_bad_slug_raises(self, gated_call_spy):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="Not a Peek page slug"):
            await connector.call("peek_get_comments", {"slug": "../x"})
        assert gated_call_spy == []
        client.get_page.assert_not_called()

    async def test_client_error_becomes_runtime_error(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.side_effect = PeekClientError("Peek found no such page")
        with pytest.raises(RuntimeError, match="Peek found no such page"):
            await connector.call("peek_get_comments", {"slug": "slug1"})
        assert gated_call_spy == []


class TestGetStats:
    async def test_gates_and_returns_names_and_times_only(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_stats.return_value = PeekStats(
            "slug1", "slug1.html", 5, 3,
            [PeekVisit("Ann", "2026-10-10T11:07:00Z"), PeekVisit("", "2026-10-10T12:00:00Z")],
        )

        result = await connector.call("peek_get_stats", {"slug": "slug1"})

        call = gated_call_spy[0]
        assert call["gate"] == "review"
        assert list(call["preview"]) == ["Server", "Page", "Link"]
        assert call["preview"]["Server"] == "peek.example.com"
        assert list(call["new_info"]) == ["Visits", "Unique visitors", "Recent visitors"]
        assert "(no name given)" in call["details_text"]
        assert call["pii_scan_text"] == "Ann"
        assert call["args"] == {"slug": "slug1"}
        assert all(set(v) == {"visitor_name", "visited_at"} for v in result["recent"])
        assert result["total_visits"] == 5
        assert result["unique_visitors"] == 3

    async def test_no_visits(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_stats.return_value = PeekStats("slug1", "slug1.html", 0, 0, [])

        await connector.call("peek_get_stats", {"slug": "slug1"})

        assert gated_call_spy[0]["details_text"] == "(no visits)"
        assert gated_call_spy[0]["preview_tables"] == []

    async def test_bad_slug_raises(self, gated_call_spy):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="Not a Peek page slug"):
            await connector.call("peek_get_stats", {"slug": "a b"})
        assert gated_call_spy == []

    async def test_client_error_becomes_runtime_error(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_stats.side_effect = PeekClientError("Peek API error (HTTP 500)")
        with pytest.raises(RuntimeError, match="HTTP 500"):
            await connector.call("peek_get_stats", {"slug": "slug1"})
        assert gated_call_spy == []


def _upload_result() -> PeekUploadResult:
    return PeekUploadResult(
        "new1", "https://peek.example.com/p/new1", "private", 1,
        "https://peek.example.com/p/new1/v/1",
    )


@pytest.fixture
def fake_local_files(monkeypatch):
    state = SimpleNamespace(required=[], read=[], committed=0, data=b"<p>file</p>")
    lf = peek_module.local_files

    def require(paths, *, max_total_bytes, download_mode):
        state.required.append(list(paths))

    def read(path, *, download_mode):
        state.read.append(path)
        return state.data

    def commit():
        state.committed += 1

    monkeypatch.setattr(lf, "require_local_files", require)
    monkeypatch.setattr(lf, "read_local_file", read)
    monkeypatch.setattr(lf, "commit_uploads", commit)
    return state


class TestUploadPage:
    async def test_html_new_page(self, gated_call_spy, fake_local_files):
        connector, client = make_connector()
        client.upload_page.return_value = _upload_result()
        html = "<h1>Hello</h1><script>evil()</script>"

        result = await connector.call("peek_upload_page", {"html": html, "name": "r.html"})

        assert result == {
            "slug": "new1", "url": "https://peek.example.com/p/new1",
            "visibility": "private", "version": 1,
            "version_url": "https://peek.example.com/p/new1/v/1",
        }
        call = gated_call_spy[0]
        assert call["gate"] == "popup"
        assert call["tool"] == "peek_upload_page"
        assert call["preview"] == {
            "Server": "peek.example.com", "Name": "r.html", "Visibility": "private",
            "Size": f"{len(html):,} bytes",
        }
        head, _, source = call["details_text"].partition("--- HTML source")
        assert "Hello" in head and "evil()" not in head
        assert source.endswith(html)
        assert call["upload_pii_scan_text"] == html
        for field in ("preview", "args", "raw_data"):
            assert "<h1>" not in repr(call[field])
        client.upload_page.assert_called_once_with(
            html.encode(), "r.html", "private", "", slug="",
        )

    async def test_password_visibility_hides_password(self, gated_call_spy, fake_local_files):
        connector, client = make_connector()
        client.upload_page.return_value = _upload_result()

        await connector.call("peek_upload_page", {
            "html": "<p>x</p>", "visibility": "password", "password": "s3cret",
        })

        call = gated_call_spy[0]
        assert call["preview"]["Password"] == "Set (not shown)"
        assert list(call["preview"]) == ["Server", "Name", "Visibility", "Size", "Password"]
        for field in ("preview", "args", "raw_data", "details_text"):
            assert "s3cret" not in repr(call[field])
        assert client.upload_page.call_args.args[3] == "s3cret"

    async def test_new_version(self, gated_call_spy, fake_local_files):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.upload_page.return_value = _upload_result()

        await connector.call("peek_upload_page", {"html": "<p>v3</p>", "slug": "slug1"})

        call = gated_call_spy[0]
        assert list(call["preview"]) == [
            "Server", "Page", "Link", "New version", "Visibility", "Size",
        ]
        assert call["preview"]["New version"] == "3"
        assert call["preview"]["Visibility"] == "private (unchanged)"
        assert call["args"]["slug"] == "slug1"
        assert client.upload_page.call_args.args[2:4] == ("", "")
        assert client.upload_page.call_args.kwargs == {"slug": "slug1"}

    @pytest.mark.parametrize("extra", [{"visibility": "public"}, {"password": "pw"}])
    async def test_slug_with_visibility_or_password(self, gated_call_spy, extra):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="A new version keeps the page's visibility"):
            await connector.call(
                "peek_upload_page", {"html": "<p>x</p>", "slug": "slug1", **extra},
            )
        client.get_page.assert_not_called()
        assert gated_call_spy == []

    async def test_name_defaults(self, gated_call_spy, fake_local_files):
        connector, client = make_connector()
        client.upload_page.return_value = _upload_result()

        await connector.call("peek_upload_page", {"html": "<p>x</p>"})
        await connector.call("peek_upload_page", {"html": "<p>x</p>", "name": "a/b/c.html"})
        await connector.call("peek_upload_page", {"local_path": "/home/u/docs/r.html"})

        assert [c["preview"]["Name"] for c in gated_call_spy] == ["page.html", "c.html", "r.html"]

    async def test_local_path_reads_bytes(self, gated_call_spy, fake_local_files):
        connector, client = make_connector()
        client.upload_page.return_value = _upload_result()

        await connector.call("peek_upload_page", {"local_path": "~/r.html"})

        assert fake_local_files.required == [["~/r.html"]]
        assert fake_local_files.read == ["~/r.html"]
        assert client.upload_page.call_args.args[0] == b"<p>file</p>"

    async def test_upload_id_becomes_upload_reference(self, gated_call_spy, fake_local_files):
        connector, client = make_connector()
        client.upload_page.return_value = _upload_result()

        await connector.call("peek_upload_page", {"upload_id": " abc "})

        assert fake_local_files.required == [["upload:abc"]]
        assert fake_local_files.read == ["upload:abc"]

    async def test_local_path_refused_in_org_mode(self, gated_call_spy, fake_local_files):
        client = MagicMock()
        connector = PeekConnector(client, download_mode="org")
        with pytest.raises(ValueError, match="not available on an organization-managed"):
            await connector.call("peek_upload_page", {"local_path": "/x/r.html"})
        assert fake_local_files.read == [] and gated_call_spy == []

    async def test_commit_after_gate_then_upload(self, monkeypatch, fake_local_files):
        connector, client = make_connector()
        order = []

        async def fake_gated(**kwargs):
            order.append("gate")

        monkeypatch.setattr(peek_module, "gated_call", fake_gated)
        fake_local_files_commit = peek_module.local_files.commit_uploads
        monkeypatch.setattr(
            peek_module.local_files, "commit_uploads",
            lambda: (order.append("commit"), fake_local_files_commit())[0],
        )
        client.upload_page.side_effect = lambda *a, **k: (
            order.append("upload"), _upload_result())[1]

        await connector.call("peek_upload_page", {"upload_id": "abc"})

        assert order == ["gate", "commit", "upload"]
        assert fake_local_files.committed == 1

    async def test_gate_refusal_stops_everything(self, monkeypatch, fake_local_files):
        connector, client = make_connector()

        async def refuse(**kwargs):
            raise RuntimeError("denied")

        monkeypatch.setattr(peek_module, "gated_call", refuse)

        with pytest.raises(RuntimeError, match="denied"):
            await connector.call("peek_upload_page", {"upload_id": "abc"})

        assert fake_local_files.committed == 0
        client.upload_page.assert_not_called()

    @pytest.mark.parametrize("args, message", [
        ({}, "give exactly one of html, local_path and upload_id"),
        ({"html": "<p>x</p>", "upload_id": "a"}, "give exactly one of"),
        ({"html": "<p>x</p>", "visibility": "restricted"}, "visibility must be public"),
        ({"html": "<p>x</p>", "visibility": "secret"}, "visibility must be public"),
        ({"html": "<p>x</p>", "visibility": "password"}, "A password is required"),
        ({"html": "<p>x</p>", "visibility": "public", "password": "p"},
         "password must be empty unless"),
        ({"html": "<p>x</p>", "visibility": "password", "password": "p" * 73},
         "72 bytes or fewer"),
        ({"html": "<p>x</p>", "visibility": "password", "password": " p"},
         "must not start or end with a space"),
        ({"html": "x" * (10 * 1024 * 1024 + 1)}, "larger than 10 MB"),
        ({"html": "<p>x</p>", "slug": "a b"}, "Not a Peek page slug"),
    ])
    async def test_validation(self, gated_call_spy, args, message):
        connector, client = make_connector()
        with pytest.raises(ValueError, match=message):
            await connector.call("peek_upload_page", args)
        assert gated_call_spy == []
        client.get_page.assert_not_called()
        client.upload_page.assert_not_called()

    async def test_empty_file(self, gated_call_spy, fake_local_files):
        connector, _ = make_connector()
        fake_local_files.data = b""
        with pytest.raises(ValueError, match="the page is empty"):
            await connector.call("peek_upload_page", {"upload_id": "a"})
        assert gated_call_spy == []

    async def test_non_utf8(self, gated_call_spy, fake_local_files):
        connector, _ = make_connector()
        fake_local_files.data = b"\xff\xfe\x00"
        with pytest.raises(ValueError, match="not UTF-8 text"):
            await connector.call("peek_upload_page", {"upload_id": "a"})
        assert gated_call_spy == []

    async def test_oversized_file(self, gated_call_spy, fake_local_files):
        connector, _ = make_connector()
        fake_local_files.data = b"x" * (10 * 1024 * 1024 + 1)
        with pytest.raises(ValueError, match="larger than 10 MB"):
            await connector.call("peek_upload_page", {"upload_id": "a"})
        assert gated_call_spy == []


def _comment_result() -> PeekComment:
    return PeekComment(7, "ann", "Thanks", "element", "#a", "", "2026-10-10T11:07:00Z", 0)


def _expect_gate_then_write(spy, write_mock):
    """The write ran, and only once the gate had been passed."""
    assert len(spy) == 1
    write_mock.assert_called_once()


async def _assert_stops_before_gate(connector, client, spy, tool, args, write):
    with pytest.raises(ValueError, match="Not a Peek page slug"):
        await connector.call(tool, {**args, "slug": "a b"})
    client.get_page.side_effect = PeekClientError("nope")
    with pytest.raises(RuntimeError, match="nope"):
        await connector.call(tool, args)
    assert spy == []
    write.assert_not_called()


async def _assert_write_needs_gate(monkeypatch, connector, write, tool, args):
    async def refuse(**kwargs):
        raise RuntimeError("denied")

    monkeypatch.setattr(peek_module, "gated_call", refuse)
    with pytest.raises(RuntimeError, match="denied"):
        await connector.call(tool, args)
    write.assert_not_called()


class TestSetVisibility:
    async def test_public(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.set_visibility.return_value = "public"

        result = await connector.call("peek_set_visibility", {"slug": "s1", "visibility": "public"})

        assert result == {"slug": "s1", "visibility": "public"}
        call = gated_call_spy[0]
        assert call["gate"] == "popup" and call["tool"] == "peek_set_visibility"
        assert call["preview"] == {
            "Server": "peek.example.com", "Page": "s1.html",
            "Link": "https://peek.example.com/p/s1", "Visibility": "private \u2192 public",
        }
        client.set_visibility.assert_called_once_with("s1", "public", "")

    async def test_password_never_shown(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.set_visibility.return_value = "password"

        await connector.call(
            "peek_set_visibility",
            {"slug": "s1", "visibility": "password", "password": "s3cret"},
        )

        call = gated_call_spy[0]
        assert call["preview"]["Password"] == "Set (not shown)"
        assert "s3cret" not in repr(call)
        assert client.set_visibility.call_args.args == ("s1", "password", "s3cret")

    @pytest.mark.parametrize("args, message", [
        ({"visibility": "restricted"}, "visibility must be public, password or private."),
        ({"visibility": "x"}, "visibility must be public, password or private."),
        ({"visibility": "password"}, "A password is required"),
        ({"visibility": "public", "password": "p"}, "password must be empty"),
        ({"visibility": "password", "password": "x" * 73}, "72 bytes or fewer"),
        ({"visibility": "password", "password": " p"}, "start or end with a space"),
    ])
    async def test_validation(self, gated_call_spy, args, message):
        connector, client = make_connector()
        with pytest.raises(ValueError, match=message):
            await connector.call("peek_set_visibility", {"slug": "s1", **args})
        assert gated_call_spy == []
        client.get_page.assert_not_called()

    async def test_restricted_page_refused(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = PeekPage(
            "s1", "S", "ann", 1, "restricted", "https://peek.example.com/p/s1",
            "2026-01-01T00:00:00Z", 1,
        )
        with pytest.raises(ValueError, match="restricted to people granted access"):
            await connector.call("peek_set_visibility", {"slug": "s1", "visibility": "public"})
        assert gated_call_spy == []
        client.set_visibility.assert_not_called()

    async def test_stops_before_gate(self, gated_call_spy):
        connector, client = make_connector()
        await _assert_stops_before_gate(
            connector, client, gated_call_spy, "peek_set_visibility",
            {"slug": "s1", "visibility": "public"}, client.set_visibility,
        )

    async def test_write_needs_the_gate(self, monkeypatch):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        await _assert_write_needs_gate(
            monkeypatch, connector, client.set_visibility, "peek_set_visibility",
            {"slug": "s1", "visibility": "public"},
        )


class TestAddComment:
    @pytest.mark.parametrize("extra, anchor", [
        ({}, "Whole page"),
        ({"selector": "#a"}, "Element #a"),
        ({"selector": "#a", "element_text": "hi"}, "Text \u201chi\u201d"),
    ])
    async def test_anchor_texts(self, gated_call_spy, extra, anchor):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.add_comment.return_value = _comment_result()

        result = await connector.call(
            "peek_add_comment", {"slug": "s1", "body": "Nice", **extra},
        )

        call = gated_call_spy[0]
        assert call["gate"] == "popup" and call["details_text"] == "Nice"
        assert call["preview"] == {
            "Server": "peek.example.com", "Page": "s1.html",
            "Link": "https://peek.example.com/p/s1", "Version": "2", "Anchored to": anchor,
        }
        assert result == {
            "slug": "s1", "version": 2,
            "comment": {
                "id": 7, "author": "ann", "body": "Thanks", "anchor_kind": "element",
                "selector": "#a", "element_text": "", "created_at": "2026-10-10T11:07:00Z",
            },
        }
        client.add_comment.assert_called_once_with(
            "s1", "Nice", extra.get("selector", ""), extra.get("element_text", ""),
        )

    async def test_long_body_summary_is_cut(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.add_comment.return_value = _comment_result()
        await connector.call("peek_add_comment", {"slug": "s1", "body": "b" * 100})
        assert gated_call_spy[0]["summary"] == "Comment on s1.html: " + "b" * 80 + "\u2026"

    @pytest.mark.parametrize("args, message", [
        ({"body": "  "}, "body must be 1 to 4000 characters."),
        ({"body": "x" * 4001}, "body must be 1 to 4000 characters."),
        ({"body": "x", "selector": "s" * 501}, "selector must be 500 characters or fewer."),
        ({"body": "x", "selector": "a", "element_text": "t" * 201},
         "element_text must be 200 characters or fewer."),
        ({"body": "x", "element_text": "t"}, "element_text needs a selector."),
    ])
    async def test_validation(self, gated_call_spy, args, message):
        connector, client = make_connector()
        with pytest.raises(ValueError, match=message):
            await connector.call("peek_add_comment", {"slug": "s1", **args})
        assert gated_call_spy == []
        client.get_page.assert_not_called()

    async def test_stops_before_gate(self, gated_call_spy):
        connector, client = make_connector()
        await _assert_stops_before_gate(
            connector, client, gated_call_spy, "peek_add_comment",
            {"slug": "s1", "body": "hi"}, client.add_comment,
        )

    async def test_write_needs_the_gate(self, monkeypatch):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        await _assert_write_needs_gate(
            monkeypatch, connector, client.add_comment, "peek_add_comment",
            {"slug": "s1", "body": "hi"},
        )


class TestRenamePage:
    async def test_rename(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.rename_page.return_value = PeekRenameResult(
            "renamed", "https://peek.example.com/p/renamed", "Renamed",
        )

        result = await connector.call("peek_rename_page", {"slug": "s1", "name": "Renamed"})

        assert result == {
            "old_slug": "s1", "slug": "renamed",
            "url": "https://peek.example.com/p/renamed", "name": "Renamed",
        }
        call = gated_call_spy[0]
        assert call["gate"] == "popup"
        assert list(call["preview"]) == ["Server", "Page", "New name", "Link"]
        assert "keeps redirecting" in call["details_text"]
        client.rename_page.assert_called_once_with("s1", "Renamed")

    @pytest.mark.parametrize("name", ["", "   ", "n" * 201])
    async def test_name_validation(self, gated_call_spy, name):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="name must be 1 to 200 characters."):
            await connector.call("peek_rename_page", {"slug": "s1", "name": name})
        assert gated_call_spy == []
        client.get_page.assert_not_called()

    async def test_stops_before_gate(self, gated_call_spy):
        connector, client = make_connector()
        await _assert_stops_before_gate(
            connector, client, gated_call_spy, "peek_rename_page",
            {"slug": "s1", "name": "N"}, client.rename_page,
        )

    async def test_write_needs_the_gate(self, monkeypatch):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        await _assert_write_needs_gate(
            monkeypatch, connector, client.rename_page, "peek_rename_page",
            {"slug": "s1", "name": "N"},
        )


class TestDeletePage:
    async def test_delete(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")

        result = await connector.call("peek_delete_page", {"slug": "s1"})

        assert result == {"deleted": "s1"}
        call = gated_call_spy[0]
        assert call["gate"] == "popup"
        assert call["preview"] == {
            "Server": "peek.example.com", "Page": "s1.html",
            "Link": "https://peek.example.com/p/s1", "Visibility": "private",
            "Created": "2026-01-01T00:00:00Z",
        }
        client.delete_page.assert_called_once_with("s1")

    def test_spec_is_destructive(self):
        spec = next(
            s for s in PeekConnector(None).tool_specs() if s.name == "peek_delete_page"
        )
        assert spec.destructive and not spec.read_only

    async def test_stops_before_gate(self, gated_call_spy):
        connector, client = make_connector()
        await _assert_stops_before_gate(
            connector, client, gated_call_spy, "peek_delete_page", {"slug": "s1"},
            client.delete_page,
        )

    async def test_write_needs_the_gate(self, monkeypatch):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        await _assert_write_needs_gate(
            monkeypatch, connector, client.delete_page, "peek_delete_page", {"slug": "s1"},
        )


class TestFieldCompleteness:
    async def test_comments_preview_has_no_placeholder(self, monkeypatch, gated_call_spy):
        pages = [{
            "slug": "slug1", "name": "QA page", "owner": "qa", "size": 10,
            "visibility": "private", "url": "https://peek.example.com/p/slug1",
            "created_at": 1760094420, "version": 2,
        }]
        comments = [{
            "id": 1, "selector": "#a", "element_text": "x", "anchor_kind": "element",
            "author": "bob", "body": "hi", "created_at": 1760094420, "version": 2,
        }]

        def fake_request(self, method, url, **kwargs):
            payload = comments if "/comments" in url else pages
            return SimpleNamespace(status_code=200, json=lambda: payload)

        monkeypatch.setattr("requests.Session.request", fake_request)
        connector = PeekConnector(PeekClient("https://peek.example.com", "tok"))

        await connector.call("peek_get_comments", {"slug": "slug1"})

        assert_no_placeholder_fields(gated_call_spy[0]["preview"])


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        client.list_pages.return_value = []
        client.get_page.return_value = PeekPage(
            "slug1", "QA page", "qa", 10, "private",
            "https://peek.example.com/p/slug1", "2026-10-10T11:07:00Z", 2,
        )
        client.get_comments.return_value = []
        client.get_stats.return_value = PeekStats("slug1", "QA page", 0, 0, [])
        client.upload_page.return_value = PeekUploadResult(
            "new1", "https://peek.example.com/p/new1", "private", 1,
            "https://peek.example.com/p/new1/v/1",
        )
        client.set_visibility.return_value = "public"
        client.add_comment.return_value = _comment_result()
        client.rename_page.return_value = PeekRenameResult(
            "renamed", "https://peek.example.com/p/renamed", "Renamed",
        )
        await assert_all_tools_leave_an_audit_trail(
            connector, peek_module, monkeypatch, tmp_path,
            arg_overrides={
                "peek_get_comments": {"slug": "slug1"},
                "peek_get_stats": {"slug": "slug1"},
                "peek_upload_page": {"html": "<p>hi</p>"},
                "peek_set_visibility": {"slug": "slug1", "visibility": "public"},
                "peek_add_comment": {"slug": "slug1", "body": "hi"},
                "peek_delete_page": {"slug": "slug1"},
                "peek_rename_page": {"slug": "slug1", "name": "Renamed"},
            },
        )
