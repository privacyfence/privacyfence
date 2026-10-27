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
    PeekGrant,
    PeekPage,
    PeekRenameResult,
    PeekStats,
    PeekUploadResult,
    PeekVersion,
    PeekVisibilityResult,
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
        slug=slug, name=f"{slug}.html", owner="ann", size_bytes=120, visibility="signed-in",
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
    "peek_set_visibility": ("peek_upload_page", "peek_grant_access"),
    "peek_add_comment": ("peek_get_comments", "peek_list_versions"),
    "peek_list_versions": ("peek_get_comments",),
    "peek_list_grants": ("peek_grant_access", "peek_revoke_access"),
    "peek_grant_access": ("peek_set_visibility", "peek_list_grants"),
    "peek_revoke_access": ("peek_list_grants",),
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


def make_comment(kind="page", selector="", element_text="", body="Looks good", cid=1, verified=False):
    return PeekComment(
        id=cid, author="bob", body=body, anchor_kind=kind, selector=selector,
        element_text=element_text, created_at="2026-10-10T11:07:00Z", version=2, verified=verified,
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
        assert call["preview_tables"][0]["headers"] == [
            "Author", "Signed in", "Date", "Anchored to", "Comment",
        ]
        assert [row[1] for row in call["preview_tables"][0]["rows"]] == ["no", "no"]
        assert call["table_only"] is True
        assert call["args"] == {"slug": "slug1", "version": 0}
        assert result["version"] == 2
        assert [c["id"] for c in result["comments"]] == [1, 2]

    async def test_get_comments_returns_verified_and_signed_in_column(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_comments.return_value = [
            make_comment(cid=1, verified=True), make_comment(cid=2, verified=False),
        ]

        result = await connector.call("peek_get_comments", {"slug": "slug1"})

        assert [c["verified"] for c in result["comments"]] == [True, False]
        assert [row[1] for row in gated_call_spy[0]["preview_tables"][0]["rows"]] == ["yes", "no"]

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
        assert gated_call_spy[0]["preview_tables"][0]["rows"][0][3] == expected

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
        "new1", "https://peek.example.com/p/new1", "restricted", 1,
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
            "visibility": "restricted", "version": 1,
            "version_url": "https://peek.example.com/p/new1/v/1",
        }
        call = gated_call_spy[0]
        assert call["gate"] == "popup"
        assert call["tool"] == "peek_upload_page"
        assert call["preview"] == {
            "Server": "peek.example.com", "Name": "r.html", "Visibility": "restricted",
            "Size": f"{len(html):,} bytes",
        }
        head, _, source = call["details_text"].partition("--- HTML source")
        assert "Hello" in head and "evil()" not in head
        assert source.endswith(html)
        assert call["upload_pii_scan_text"] == html
        for field in ("preview", "args", "raw_data"):
            assert "<h1>" not in repr(call[field])
        client.upload_page.assert_called_once_with(
            html.encode(), "r.html", "restricted", slug="",
        )

    async def test_upload_defaults_to_restricted(self, gated_call_spy, fake_local_files):
        connector, client = make_connector()
        client.upload_page.return_value = _upload_result()

        await connector.call("peek_upload_page", {"html": "<p>x</p>"})

        assert gated_call_spy[0]["preview"]["Visibility"] == "restricted"
        assert gated_call_spy[0]["args"]["visibility"] == "restricted"
        assert client.upload_page.call_args.args[2] == "restricted"

    async def test_upload_private_is_published_as_signed_in(self, gated_call_spy, fake_local_files):
        connector, client = make_connector()
        client.upload_page.return_value = _upload_result()

        await connector.call("peek_upload_page", {"html": "<p>x</p>", "visibility": "private"})

        assert gated_call_spy[0]["preview"]["Visibility"] == "signed-in"
        assert client.upload_page.call_args.args[2] == "signed-in"

    async def test_upload_password_keyword_is_refused(self, gated_call_spy, fake_local_files):
        connector, client = make_connector()

        with pytest.raises(ValueError, match="Peek no longer has page passwords"):
            await connector.call("peek_upload_page", {
                "html": "<p>x</p>", "visibility": "restricted", "password": "s3cret",
            })

        assert gated_call_spy == []
        client.upload_page.assert_not_called()

    async def test_upload_name_over_200_characters_is_refused(self, gated_call_spy, fake_local_files):
        connector, client = make_connector()

        with pytest.raises(ValueError, match="name must be 1 to 200 characters."):
            await connector.call("peek_upload_page", {"html": "<p>x</p>", "name": "n" * 201})

        assert gated_call_spy == []
        client.upload_page.assert_not_called()

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
        assert call["preview"]["Visibility"] == "signed-in (unchanged)"
        assert call["args"]["slug"] == "slug1"
        assert client.upload_page.call_args.args[2] == ""
        assert client.upload_page.call_args.kwargs == {"slug": "slug1"}

    async def test_slug_with_visibility(self, gated_call_spy):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="A new version keeps the page's visibility"):
            await connector.call(
                "peek_upload_page", {"html": "<p>x</p>", "slug": "slug1", "visibility": "public"},
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
        ({"html": "<p>x</p>", "visibility": "secret"}, "visibility must be public, signed-in or restricted."),
        ({"html": "<p>x</p>", "visibility": "password"}, "Peek no longer has page passwords"),
        ({"html": "<p>x</p>", "slug": "slug1", "password": "p"}, "Peek no longer has page passwords"),
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
    return PeekComment(7, "ann", "Thanks", "element", "#a", "", "2026-10-10T11:07:00Z", 0, True)


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


def _set_visibility_result(visibility="public", notified=False):
    return PeekVisibilityResult(visibility, notified)


class TestSetVisibility:
    async def test_public(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.set_visibility.return_value = _set_visibility_result("public")

        result = await connector.call("peek_set_visibility", {"slug": "s1", "visibility": "public"})

        assert result == {"slug": "s1", "visibility": "public", "notified": False}
        call = gated_call_spy[0]
        assert call["gate"] == "popup" and call["tool"] == "peek_set_visibility"
        assert call["preview"] == {
            "Server": "peek.example.com", "Page": "s1.html",
            "Link": "https://peek.example.com/p/s1", "Visibility": "signed-in \u2192 public",
        }
        assert call["details_text"] == (
            "Who can open https://peek.example.com/p/s1 changes from signed-in to public."
        )
        client.set_visibility.assert_called_once_with("s1", "public")

    async def test_set_visibility_returns_notified(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.set_visibility.return_value = _set_visibility_result("restricted", True)

        result = await connector.call("peek_set_visibility", {"slug": "s1", "visibility": "restricted"})

        assert result == {"slug": "s1", "visibility": "restricted", "notified": True}

    async def test_set_visibility_password_keyword_is_refused(self, gated_call_spy):
        connector, client = make_connector()

        with pytest.raises(ValueError, match="Peek no longer has page passwords"):
            await connector.call(
                "peek_set_visibility",
                {"slug": "s1", "visibility": "restricted", "password": "s3cret"},
            )

        assert gated_call_spy == []
        client.get_page.assert_not_called()
        client.set_visibility.assert_not_called()

    @pytest.mark.parametrize("args, message", [
        ({"visibility": "x"}, "visibility must be public, signed-in or restricted."),
        ({"visibility": "password"}, "Peek no longer has page passwords"),
    ])
    async def test_validation(self, gated_call_spy, args, message):
        connector, client = make_connector()
        with pytest.raises(ValueError, match=message):
            await connector.call("peek_set_visibility", {"slug": "s1", **args})
        assert gated_call_spy == []
        client.get_page.assert_not_called()

    async def test_private_is_set_as_signed_in(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.set_visibility.return_value = _set_visibility_result("signed-in")

        await connector.call("peek_set_visibility", {"slug": "s1", "visibility": "private"})

        client.set_visibility.assert_called_once_with("s1", "signed-in")

    async def test_set_visibility_changes_a_restricted_page(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = PeekPage(
            "s1", "S", "ann", 1, "restricted", "https://peek.example.com/p/s1",
            "2026-01-01T00:00:00Z", 1,
        )
        client.set_visibility.return_value = _set_visibility_result("public")

        result = await connector.call("peek_set_visibility", {"slug": "s1", "visibility": "public"})

        assert result["visibility"] == "public"
        assert gated_call_spy[0]["preview"]["Visibility"] == "restricted \u2192 public"
        client.set_visibility.assert_called_once_with("s1", "public")

    async def test_set_visibility_to_restricted_explains_emails(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.set_visibility.return_value = _set_visibility_result("restricted", True)

        await connector.call("peek_set_visibility", {"slug": "s1", "visibility": "restricted"})

        details = gated_call_spy[0]["details_text"]
        assert "Only you, Peek admins and the people granted access can open it." in details
        assert "Peek emails the link to everyone already granted access." in details

    async def test_no_email_line_when_already_restricted(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.get_page.return_value = PeekPage(
            "s1", "S", "ann", 1, "restricted", "https://peek.example.com/p/s1",
            "2026-01-01T00:00:00Z", 1,
        )
        client.set_visibility.return_value = _set_visibility_result("restricted")

        await connector.call("peek_set_visibility", {"slug": "s1", "visibility": "restricted"})

        assert "Peek emails" not in gated_call_spy[0]["details_text"]
        assert "no longer limited" not in gated_call_spy[0]["details_text"]

    async def test_set_visibility_from_restricted_keeps_grants_text(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = PeekPage(
            "s1", "S", "ann", 1, "restricted", "https://peek.example.com/p/s1",
            "2026-01-01T00:00:00Z", 1,
        )
        client.set_visibility.return_value = _set_visibility_result("signed-in")

        await connector.call("peek_set_visibility", {"slug": "s1", "visibility": "signed-in"})

        assert (
            "The page is no longer limited to the people granted access. Their grants stay and "
            "apply again if the page is restricted later."
        ) in gated_call_spy[0]["details_text"]

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
                "id": 7, "author": "ann", "verified": True, "body": "Thanks",
                "anchor_kind": "element",
                "selector": "#a", "element_text": "", "created_at": "2026-10-10T11:07:00Z",
            },
        }
        client.add_comment.assert_called_once_with(
            "s1", "Nice", extra.get("selector", ""), extra.get("element_text", ""), 0,
        )

    async def test_add_comment_on_a_version(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.add_comment.return_value = _comment_result()

        result = await connector.call("peek_add_comment", {"slug": "s1", "body": "Nice", "version": 1})

        assert gated_call_spy[0]["preview"]["Version"] == "1"
        assert result["version"] == 1
        client.add_comment.assert_called_once_with("s1", "Nice", "", "", 1)

    async def test_add_comment_version_above_latest_is_refused_before_the_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")

        with pytest.raises(ValueError, match="This page's latest version is 2."):
            await connector.call("peek_add_comment", {"slug": "s1", "body": "Nice", "version": 3})

        assert gated_call_spy == []
        client.add_comment.assert_not_called()

    async def test_add_comment_negative_version_is_refused(self, gated_call_spy):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="version must be 0"):
            await connector.call("peek_add_comment", {"slug": "s1", "body": "Nice", "version": -1})
        client.get_page.assert_not_called()

    async def test_add_comment_result_has_verified(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.add_comment.return_value = _comment_result()

        result = await connector.call("peek_add_comment", {"slug": "s1", "body": "Nice"})

        assert result["comment"]["verified"] is True

    async def test_add_comment_limits_are_bytes(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.add_comment.return_value = _comment_result()

        await connector.call("peek_add_comment", {"slug": "s1", "body": "\u00e9" * 1500})
        with pytest.raises(ValueError, match=r"body must be 1 to 4000 bytes \(UTF-8\)."):
            await connector.call("peek_add_comment", {"slug": "s1", "body": "\u00e9" * 2001})

    async def test_long_body_summary_is_cut(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.add_comment.return_value = _comment_result()
        await connector.call("peek_add_comment", {"slug": "s1", "body": "b" * 100})
        assert gated_call_spy[0]["summary"] == "Comment on s1.html: " + "b" * 80 + "\u2026"

    @pytest.mark.parametrize("args, message", [
        ({"body": "  "}, r"body must be 1 to 4000 bytes \(UTF-8\)."),
        ({"body": "x" * 4001}, r"body must be 1 to 4000 bytes \(UTF-8\)."),
        ({"body": "x", "selector": "s" * 501}, r"selector must be 500 bytes or fewer \(UTF-8\)."),
        ({"body": "x", "selector": "a", "element_text": "t" * 201},
         r"element_text must be 200 bytes or fewer \(UTF-8\)."),
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


def make_restricted_page(slug: str) -> PeekPage:
    return PeekPage(
        slug=slug, name=f"{slug}.html", owner="ann", size_bytes=120, visibility="restricted",
        url=f"https://peek.example.com/p/{slug}", created_at="2026-01-01T00:00:00Z", version=2,
    )


class TestListVersions:
    async def test_returns_versions_without_gating_and_audits(self, tmp_path, gated_call_spy):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.get_page.return_value = make_page("s1")
        client.list_versions.return_value = [
            PeekVersion(2, 300, "2026-10-11T00:00:00Z", "https://peek.example.com/p/s1/v/2"),
            PeekVersion(1, 120, "2026-10-10T00:00:00Z", "https://peek.example.com/p/s1/v/1"),
        ]

        result = await connector.call("peek_list_versions", {"slug": "s1"})

        assert result["slug"] == "s1"
        assert result["name"] == "s1.html"
        assert [v["version"] for v in result["versions"]] == [2, 1]
        assert set(result["versions"][0]) == {"version", "size_bytes", "created_at", "url"}
        assert gated_call_spy == []
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(entries) == 1
        assert '"decision": "auto_accepted"' in entries[0]
        assert '"tool": "peek_list_versions"' in entries[0]
        assert "2 version(s)" in entries[0]

    async def test_bad_slug_and_client_error(self):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="Not a Peek page slug"):
            await connector.call("peek_list_versions", {"slug": "a b"})
        client.get_page.side_effect = PeekClientError("nope")
        with pytest.raises(RuntimeError, match="nope"):
            await connector.call("peek_list_versions", {"slug": "s1"})


class TestListGrants:
    async def test_gates_and_returns_grants(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_restricted_page("s1")
        client.list_grants.return_value = [
            PeekGrant("group", "team@example.com", "2026-10-10T11:07:00Z"),
            PeekGrant("user", "bob@example.com", "2026-10-11T11:07:00Z"),
        ]

        result = await connector.call("peek_list_grants", {"slug": "s1"})

        assert result == {
            "slug": "s1",
            "name": "s1.html",
            "visibility": "restricted",
            "grants": [
                {"kind": "group", "email": "team@example.com", "created_at": "2026-10-10T11:07:00Z"},
                {"kind": "user", "email": "bob@example.com", "created_at": "2026-10-11T11:07:00Z"},
            ],
        }
        call = gated_call_spy[0]
        assert call["gate"] == "review"
        assert call["tool_name"] == "Read Peek Page Access"
        assert call["summary"] == "2 grant(s) on s1.html"
        assert list(call["preview"]) == ["Server", "Page", "Link", "Visibility"]
        assert list(call["new_info"]) == ["Grants", "Email addresses"]
        assert call["table_only"] is True
        assert call["args"] == {"slug": "s1"}
        assert call["pii_scan_text"] == "team@example.com\nbob@example.com"
        assert call["preview_tables"][0]["headers"] == ["Kind", "Email", "Added"]
        assert len(call["preview_tables"][0]["rows"]) == 2
        assert "bob@example.com (user)" in call["details_text"]

    async def test_no_grants(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_restricted_page("s1")
        client.list_grants.return_value = []

        result = await connector.call("peek_list_grants", {"slug": "s1"})

        assert result["grants"] == []
        call = gated_call_spy[0]
        assert call["details_text"] == "(no grants)"
        assert call["preview_tables"] == []

    async def test_bad_slug_and_client_error(self, gated_call_spy):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="Not a Peek page slug"):
            await connector.call("peek_list_grants", {"slug": "a b"})
        client.get_page.side_effect = PeekClientError("nope")
        with pytest.raises(RuntimeError, match="nope"):
            await connector.call("peek_list_grants", {"slug": "s1"})
        assert gated_call_spy == []


class TestGrantAccess:
    async def test_grant_on_a_restricted_page(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_restricted_page("s1")
        client.add_grant.return_value = True

        result = await connector.call(
            "peek_grant_access", {"slug": "s1", "email": " Bob@Example.com "},
        )

        assert result == {"slug": "s1", "kind": "user", "email": "bob@example.com", "notified": True}
        call = gated_call_spy[0]
        assert call["gate"] == "popup"
        assert call["tool_name"] == "Grant Access to Peek Page"
        assert call["summary"] == "Let bob@example.com open s1.html"
        assert list(call["preview"]) == ["Server", "Page", "Link", "Visibility", "Grant to"]
        assert call["preview"]["Grant to"] == "bob@example.com (user)"
        assert call["details_text"] == (
            "bob@example.com can open https://peek.example.com/p/s1. Peek emails them the link "
            "unless they already had access."
        )
        assert call["args"] == {"slug": "s1"}
        client.add_grant.assert_called_once_with("s1", "user", "bob@example.com")

    async def test_grant_on_a_public_page_says_nothing_changes_yet(self, gated_call_spy):
        connector, client = make_connector()
        page = make_page("s1")
        client.get_page.return_value = PeekPage(
            page.slug, page.name, page.owner, page.size_bytes, "public", page.url,
            page.created_at, page.version,
        )
        client.add_grant.return_value = False

        result = await connector.call(
            "peek_grant_access", {"slug": "s1", "email": "team@example.com", "kind": "group"},
        )

        assert result["notified"] is False
        assert result["kind"] == "group"
        assert gated_call_spy[0]["details_text"] == (
            "team@example.com can open https://peek.example.com/p/s1 once it is restricted. "
            "It is public now, so the grant changes nothing until then and no email is sent."
        )
        client.add_grant.assert_called_once_with("s1", "group", "team@example.com")

    @pytest.mark.parametrize(
        ("kind", "email", "message"),
        [
            ("user", "nobody", "Enter one email address"),
            ("user", "a@b@c", "Enter one email address"),
            ("team", "a@b.c", "kind must be user or group."),
        ],
    )
    async def test_invalid_grant_is_refused_before_the_gate(
        self, gated_call_spy, kind, email, message,
    ):
        connector, client = make_connector()
        with pytest.raises(ValueError, match=message):
            await connector.call(
                "peek_grant_access", {"slug": "s1", "email": email, "kind": kind},
            )
        assert gated_call_spy == []
        client.get_page.assert_not_called()
        client.add_grant.assert_not_called()

    async def test_stops_before_gate(self, gated_call_spy):
        connector, client = make_connector()
        await _assert_stops_before_gate(
            connector, client, gated_call_spy, "peek_grant_access",
            {"slug": "s1", "email": "bob@example.com"}, client.add_grant,
        )

    async def test_write_needs_the_gate(self, monkeypatch):
        connector, client = make_connector()
        client.get_page.return_value = make_restricted_page("s1")
        await _assert_write_needs_gate(
            monkeypatch, connector, client.add_grant, "peek_grant_access",
            {"slug": "s1", "email": "bob@example.com"},
        )

    async def test_group_lookup_error_surfaces(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_restricted_page("s1")
        client.add_grant.side_effect = PeekClientError("This Peek server cannot check Google group")
        with pytest.raises(RuntimeError, match="cannot check Google group"):
            await connector.call(
                "peek_grant_access", {"slug": "s1", "email": "t@example.com", "kind": "group"},
            )


class TestRevokeAccess:
    async def test_revoke(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_restricted_page("s1")

        result = await connector.call(
            "peek_revoke_access", {"slug": "s1", "email": "Bob@Example.com"},
        )

        assert result == {"slug": "s1", "kind": "user", "email": "bob@example.com", "removed": True}
        call = gated_call_spy[0]
        assert call["gate"] == "popup"
        assert call["tool_name"] == "Revoke Access to Peek Page"
        assert call["summary"] == "Remove bob@example.com from s1.html"
        assert list(call["preview"]) == ["Server", "Page", "Link", "Visibility", "Revoke from"]
        assert call["preview"]["Revoke from"] == "bob@example.com (user)"
        assert call["details_text"] == (
            "bob@example.com can no longer open https://peek.example.com/p/s1 while it is "
            "restricted."
        )
        client.remove_grant.assert_called_once_with("s1", "user", "bob@example.com")

    async def test_revoke_unknown_grant_message(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_restricted_page("s1")
        client.remove_grant.side_effect = PeekClientError(
            "Peek has no such grant on this page (HTTP 404). Call peek_list_grants to see who has access.",
            status=404,
        )
        with pytest.raises(RuntimeError, match="Peek has no such grant on this page"):
            await connector.call("peek_revoke_access", {"slug": "s1", "email": "bob@example.com"})

    async def test_invalid_grant_is_refused_before_the_gate(self, gated_call_spy):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="Enter one email address"):
            await connector.call("peek_revoke_access", {"slug": "s1", "email": "nobody"})
        assert gated_call_spy == []
        client.remove_grant.assert_not_called()

    async def test_stops_before_gate(self, gated_call_spy):
        connector, client = make_connector()
        await _assert_stops_before_gate(
            connector, client, gated_call_spy, "peek_revoke_access",
            {"slug": "s1", "email": "bob@example.com"}, client.remove_grant,
        )

    async def test_write_needs_the_gate(self, monkeypatch):
        connector, client = make_connector()
        client.get_page.return_value = make_restricted_page("s1")
        await _assert_write_needs_gate(
            monkeypatch, connector, client.remove_grant, "peek_revoke_access",
            {"slug": "s1", "email": "bob@example.com"},
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
            "Link": "https://peek.example.com/p/s1", "Visibility": "signed-in",
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
            "visibility": "signed-in", "url": "https://peek.example.com/p/slug1",
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
            "slug1", "QA page", "qa", 10, "signed-in",
            "https://peek.example.com/p/slug1", "2026-10-10T11:07:00Z", 2,
        )
        client.get_comments.return_value = []
        client.get_stats.return_value = PeekStats("slug1", "QA page", 0, 0, [])
        client.upload_page.return_value = PeekUploadResult(
            "new1", "https://peek.example.com/p/new1", "restricted", 1,
            "https://peek.example.com/p/new1/v/1",
        )
        client.set_visibility.return_value = _set_visibility_result("public")
        client.add_comment.return_value = _comment_result()
        client.rename_page.return_value = PeekRenameResult(
            "renamed", "https://peek.example.com/p/renamed", "Renamed",
        )
        client.list_versions.return_value = []
        client.list_grants.return_value = []
        client.add_grant.return_value = False
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
                "peek_list_versions": {"slug": "slug1"},
                "peek_list_grants": {"slug": "slug1"},
                "peek_grant_access": {"slug": "slug1", "email": "bob@example.com"},
                "peek_revoke_access": {"slug": "slug1", "email": "bob@example.com"},
            },
        )
