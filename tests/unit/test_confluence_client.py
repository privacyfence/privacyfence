"""Tests for ConfluenceClient's parsing logic, refresh-and-retry token
logic, and result normalization. Mirrors test_jira_client.py's approach --
same Atlassian OAuth refresh regression target (commit 862ff43), same
"construct atlassian.Confluence for real, swap in a MagicMock for ._client"
pattern.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

from privacyfence import atlassian_users
from privacyfence import confluence_client as confluence_client_module
from privacyfence.atlassian_oauth import AtlassianOAuthError
from privacyfence.confluence_client import (
    ConfluenceAttachment,
    ConfluenceClient,
    ConfluenceClientError,
    ConfluencePage,
    ConfluenceSearchResult,
    ConfluenceSpace,
    resolve_attachment_destination,
)

AUTHOR = "acc-author-0001"
MENTIONED = "acc-mention-0002"
OTHER = "acc-other-000003"


def _mention(account_id: str) -> str:
    return f'<ac:link><ri:user ri:account-id="{account_id}" /></ac:link>'


def patch_bulk(monkeypatch, names: dict[str, str] | None = None) -> list[list[str]]:
    """Replace fetch_users_bulk; return the list of id batches it was called with."""
    calls: list[list[str]] = []
    known = names or {}

    def fake(session, cloud_id, account_ids):
        calls.append(list(account_ids))
        return [
            atlassian_users.AtlassianUser(account_id=i, display_name=known[i])
            for i in account_ids if i in known
        ]

    monkeypatch.setattr(atlassian_users, "fetch_users_bulk", fake)
    return calls


LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "confluence"


def make_client(config: dict | None = None, token_file: str | None = None) -> ConfluenceClient:
    base = {"access_token": "tok", "cloud_id": "cloud-1", "site_url": "https://acme.atlassian.net"}
    base.update(config or {})
    client = ConfluenceClient(config=base, token_file=token_file)
    client._client = MagicMock()
    return client


def unauthorized_error() -> Exception:
    return error_with_status(401, "401 Unauthorized")


def error_with_status(status_code: int, message: str) -> Exception:
    exc = Exception(message)
    response = MagicMock()
    response.status_code = status_code
    exc.response = response
    return exc


# ---------------------------------------------------------------------------- #
# Construction
# ---------------------------------------------------------------------------- #

class TestConstruction:
    def test_missing_access_token_raises(self):
        with pytest.raises(ConfluenceClientError, match="not authenticated"):
            ConfluenceClient(config={"cloud_id": "c1"})

    def test_missing_cloud_id_raises(self):
        with pytest.raises(ConfluenceClientError, match="not authenticated"):
            ConfluenceClient(config={"access_token": "t"})

    def test_base_url_prefers_site_url(self):
        client = ConfluenceClient(config={"access_token": "t", "cloud_id": "c1", "site_url": "https://acme.atlassian.net/"})
        assert client._base_url == "https://acme.atlassian.net"

    def test_base_url_falls_back_to_wiki_api_url(self):
        client = ConfluenceClient(config={"access_token": "t", "cloud_id": "c1"})
        assert client._base_url == "https://api.atlassian.com/ex/confluence/c1/wiki"


# ---------------------------------------------------------------------------- #
# _parse_space
# ---------------------------------------------------------------------------- #

class TestParseSpace:
    def test_full_space(self):
        client = make_client()
        raw = {"key": "ENG", "name": "Engineering", "type": "global",
               "description": {"plain": {"value": "desc"}}}
        space = client._parse_space(raw)
        assert space == ConfluenceSpace(
            key="ENG", name="Engineering", space_type="global", description="desc",
            url="https://acme.atlassian.net/wiki/spaces/ENG",
        )

    def test_missing_description_defaults_empty(self):
        client = make_client()
        space = client._parse_space({"key": "ENG", "name": "Engineering"})
        assert space.description == ""

    def test_short_summary(self):
        assert ConfluenceSpace(key="ENG", name="Engineering").short_summary() == "[ENG] Engineering"


# ---------------------------------------------------------------------------- #
# _parse_page_v2
# ---------------------------------------------------------------------------- #

class TestParsePageV2:
    def test_full_page_without_body(self):
        client = make_client()
        raw = {
            "id": "123", "title": "My Page", "spaceId": "999",
            "version": {"number": 3, "createdAt": "updated-date"},
            "authorId": "acc-1", "createdAt": "created-date",
            "_links": {"webui": "/spaces/ENG/pages/123"},
        }
        page = client._parse_page_v2(raw, space_key="ENG")
        assert page.id == "123"
        assert page.title == "My Page"
        assert page.space_key == "ENG"
        assert page.version == 3
        assert page.author == "acc-1"
        assert page.created == "created-date"
        assert page.updated == "updated-date"
        assert page.body == ""
        assert page.url == "https://acme.atlassian.net/wiki/spaces/ENG/pages/123"

    def test_body_included_only_when_requested(self):
        client = make_client()
        raw = {"id": "1", "body": {"storage": {"value": "<p>content</p>"}}}
        without_body = client._parse_page_v2(raw, include_body=False)
        with_body = client._parse_page_v2(raw, include_body=True)
        assert without_body.body == ""
        assert with_body.body == "<p>content</p>"

    def test_missing_optional_fields_default_sensibly(self):
        client = make_client()
        # No spaceId, so _resolve_space_key is skipped rather than firing an
        # HTTP call for an empty id.
        page = client._parse_page_v2({})
        assert page.id == ""
        assert page.version == 0
        assert page.author == ""
        assert page.space_key == ""

    def test_space_key_resolved_from_space_id_when_not_passed_in(self):
        client = make_client()
        client._client.get.return_value = {"key": "ENG"}
        page = client._parse_page_v2({"id": "1", "spaceId": "999"})
        assert page.space_key == "ENG"
        assert client._client.get.call_args.args[0] == "api/v2/spaces/999"

    def test_author_stays_raw_id_and_name_comes_from_names(self):
        client = make_client()
        page = client._parse_page_v2({"authorId": AUTHOR}, names={AUTHOR: "Ada Lovelace"})
        assert page.author == AUTHOR
        assert page.author_name == "Ada Lovelace"

    def test_author_name_empty_when_unresolved(self):
        client = make_client()
        assert client._parse_page_v2({"authorId": AUTHOR}).author_name == ""
        page = client._parse_page_v2({"authorId": AUTHOR}, names={OTHER: "Someone"})
        assert page.author == AUTHOR
        assert page.author_name == ""

    def test_mentions_only_resolved_ids_present_in_body(self):
        client = make_client()
        body = f"<p>{_mention(MENTIONED)} and {_mention(OTHER)}</p>"
        raw = {"id": "1", "body": {"storage": {"value": body}}}
        page = client._parse_page_v2(
            raw, include_body=True, names={MENTIONED: "Grace", AUTHOR: "Ada"},
        )
        assert page.mentions == {MENTIONED: "Grace"}

    def test_mentions_empty_without_body_or_names(self):
        client = make_client()
        raw = {"id": "1", "body": {"storage": {"value": _mention(MENTIONED)}}}
        assert client._parse_page_v2(raw, include_body=False, names={MENTIONED: "G"}).mentions == {}
        assert client._parse_page_v2(raw, include_body=True).mentions == {}

    def test_short_summary_truncates_long_title(self):
        page = ConfluencePage(id="1", title="x" * 100, space_key="ENG")
        assert page.short_summary().startswith("[ENG]")
        assert page.short_summary().endswith("…")


# ---------------------------------------------------------------------------- #
# _parse_search_result: space resolution priority
# ---------------------------------------------------------------------------- #

class TestParseSearchResult:
    def test_space_resolved_from_content_when_present(self):
        client = make_client()
        raw = {
            "title": "Result", "entityType": "page", "excerpt": "...", "url": "/x",
            "content": {"id": "1", "space": {"key": "ENG", "name": "Engineering"}},
            "resultGlobalContainer": {"title": "Fallback Name"},
        }
        result = client._parse_search_result(raw)
        assert result.space_key == "ENG"
        assert result.space_name == "Engineering"

    def test_falls_back_to_global_container_title_when_no_content_space(self):
        client = make_client()
        raw = {"title": "Result", "resultGlobalContainer": {"title": "Container Name"}}
        result = client._parse_search_result(raw)
        assert result.space_key == ""
        assert result.space_name == "Container Name"

    def test_url_prefixed_with_base_url_and_wiki(self):
        client = make_client()
        result = client._parse_search_result({"url": "/spaces/ENG/pages/1"})
        assert result.url == "https://acme.atlassian.net/wiki/spaces/ENG/pages/1"

    def test_short_summary(self):
        result = ConfluenceSearchResult(id="1", title="Found It", content_type="page", space_key="ENG")
        assert result.short_summary() == "[ENG] Found It"


# ---------------------------------------------------------------------------- #
# _try_refresh / _request: same reauth-on-restart regression target as Jira
# ---------------------------------------------------------------------------- #

class TestTryRefresh:
    def test_missing_credentials_returns_false(self):
        client = make_client({"refresh_token": "rt"})
        assert client._try_refresh() is False

    def test_successful_refresh_updates_config_and_session_header(self, monkeypatch):
        monkeypatch.setattr(confluence_client_module, "atlassian_refresh",
                             lambda cid, cs, rt: {"access_token": "new-tok", "refresh_token": "new-rt"})
        client = make_client({"client_id": "ci", "client_secret": "cs", "refresh_token": "rt"})

        assert client._try_refresh() is True
        assert client._config["access_token"] == "new-tok"
        assert client._session.headers["Authorization"] == "Bearer new-tok"

    def test_picks_up_refresh_token_already_rotated_by_jira_client(self, monkeypatch, tmp_path):
        captured = {}
        def fake_refresh(cid, cs, rt):
            captured["used"] = rt
            return {"access_token": "new-tok", "refresh_token": "newer-rt"}
        monkeypatch.setattr(confluence_client_module, "atlassian_refresh", fake_refresh)
        monkeypatch.setattr(confluence_client_module, "load_token_file", lambda path: {"refresh_token": "rotated-by-jira"})
        monkeypatch.setattr(confluence_client_module, "save_token_file", lambda path, record: None)

        client = make_client(
            {"client_id": "ci", "client_secret": "cs", "refresh_token": "stale-rt"},
            token_file=str(tmp_path / "shared_token.json"),
        )
        client._try_refresh()

        assert captured["used"] == "rotated-by-jira"

    def test_refresh_api_failure_returns_false(self, monkeypatch):
        def raiser(cid, cs, rt):
            raise AtlassianOAuthError("refresh failed")
        monkeypatch.setattr(confluence_client_module, "atlassian_refresh", raiser)
        client = make_client({"client_id": "ci", "client_secret": "cs", "refresh_token": "rt"})
        assert client._try_refresh() is False

    def test_persists_to_shared_token_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(confluence_client_module, "atlassian_refresh",
                             lambda cid, cs, rt: {"access_token": "new-tok", "refresh_token": "new-rt"})
        monkeypatch.setattr(
            confluence_client_module, "load_token_file",
            lambda path: (_ for _ in ()).throw(AtlassianOAuthError("no file yet")),
        )
        saved = {}
        monkeypatch.setattr(confluence_client_module, "save_token_file", lambda path, record: saved.update(record))

        client = make_client(
            {"client_id": "ci", "client_secret": "cs", "refresh_token": "rt", "account_email": "me@x.com"},
            token_file=str(tmp_path / "atlassian_token.json"),
        )
        client._try_refresh()

        assert saved == {
            "access_token": "new-tok", "refresh_token": "new-rt",
            "cloud_id": "cloud-1", "site_url": "https://acme.atlassian.net", "account_email": "me@x.com",
        }


class TestRequest:
    def test_happy_path(self):
        client = make_client()
        assert client._request(lambda: "ok") == "ok"

    def test_401_triggers_refresh_and_retry(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(client, "_try_refresh", lambda: True)
        calls = {"n": 0}
        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise unauthorized_error()
            return "retried-ok"
        assert client._request(fn) == "retried-ok"

    def test_401_refresh_fails_reraises(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(client, "_try_refresh", lambda: False)
        def fn():
            raise unauthorized_error()
        with pytest.raises(Exception, match="401"):
            client._request(fn)

    @pytest.mark.parametrize("status_code", [403, 404])
    def test_non_401_stale_token_status_triggers_refresh_and_retry(self, monkeypatch, status_code):
        # api.atlassian.com's Confluence proxy doesn't reliably return 401 for
        # an expired/invalid token: v2 REST endpoints 404, and the legacy
        # CQL-backed endpoints 403 ("Current user not permitted to use
        # Confluence"). Both need the same refresh-and-retry as a real 401,
        # or a stale token silently masquerades as "not found"/"not permitted".
        client = make_client()
        monkeypatch.setattr(client, "_try_refresh", lambda: True)
        calls = {"n": 0}
        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise error_with_status(status_code, "boom")
            return "retried-ok"
        assert client._request(fn) == "retried-ok"

    @pytest.mark.parametrize("status_code", [403, 404])
    def test_non_401_stale_token_status_refresh_fails_reraises(self, monkeypatch, status_code):
        client = make_client()
        monkeypatch.setattr(client, "_try_refresh", lambda: False)
        def fn():
            raise error_with_status(status_code, "boom")
        with pytest.raises(Exception, match="boom"):
            client._request(fn)

    def test_unrelated_status_code_does_not_trigger_refresh(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(client, "_try_refresh", lambda: (_ for _ in ()).throw(AssertionError("should not be called")))
        def fn():
            raise error_with_status(500, "server error")
        with pytest.raises(Exception, match="server error"):
            client._request(fn)


# ---------------------------------------------------------------------------- #
# check_connection / list_spaces / search / cql_search
# ---------------------------------------------------------------------------- #

class TestCheckConnection:
    def test_returns_base_url_on_success(self):
        client = make_client()
        client._client.get.return_value = {"results": []}
        assert client.check_connection() == "https://acme.atlassian.net"

    def test_error_becomes_confluence_client_error(self):
        client = make_client()
        client._client.get.side_effect = RuntimeError("boom")
        with pytest.raises(ConfluenceClientError, match="Confluence connection check failed"):
            client.check_connection()


class TestListSpaces:
    def test_maps_results(self):
        client = make_client()
        client._client.get.return_value = {"results": [{"key": "ENG", "name": "Engineering"}]}
        spaces = client.list_spaces()
        assert spaces[0].key == "ENG"

    def test_uses_v2_spaces_endpoint_with_type_filter(self):
        client = make_client()
        client._client.get.return_value = {"results": []}
        client.list_spaces(space_type="personal")
        args, kwargs = client._client.get.call_args
        assert args[0] == "api/v2/spaces"
        assert kwargs["params"]["type"] == "personal"

    def test_default_omits_type_filter(self):
        client = make_client()
        client._client.get.return_value = {"results": []}
        client.list_spaces()
        _args, kwargs = client._client.get.call_args
        assert "type" not in kwargs["params"]

    def test_none_response_yields_empty_list(self):
        client = make_client()
        client._client.get.return_value = None
        assert client.list_spaces() == []

    def test_http_error_becomes_confluence_client_error(self):
        client = make_client()
        client._client.get.side_effect = RuntimeError("boom")
        with pytest.raises(ConfluenceClientError, match="list_spaces failed"):
            client.list_spaces()


class TestSearch:
    def test_requires_query(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="non-empty query"):
            client.search("")

    def test_wraps_query_in_cql_text_search(self):
        client = make_client()
        client._client.cql.return_value = {"results": []}
        client.search("budget")
        cql = client._client.cql.call_args.args[0]
        assert 'text ~ "budget"' in cql

    def test_maps_results(self):
        client = make_client()
        client._client.cql.return_value = {"results": [{"title": "Doc", "entityType": "page"}]}
        results = client.search("q")
        assert results[0].title == "Doc"

    def test_http_error_becomes_confluence_client_error(self):
        client = make_client()
        client._client.cql.side_effect = RuntimeError("boom")
        with pytest.raises(ConfluenceClientError, match="search\\('q'\\) failed"):
            client.search("q")


class TestCqlSearch:
    def test_requires_cql(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="non-empty CQL"):
            client.cql_search("")

    def test_passes_cql_through_unmodified(self):
        client = make_client()
        client._client.cql.return_value = {"results": []}
        client.cql_search('space = "ENG" and type = "page"')
        assert client._client.cql.call_args.args[0] == 'space = "ENG" and type = "page"'


# ---------------------------------------------------------------------------- #
# list_pages_in_space / get_page / get_page_by_title
# ---------------------------------------------------------------------------- #

class TestListPagesInSpace:
    def test_requires_space_key(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="requires a space_key"):
            client.list_pages_in_space("")

    def test_maps_pages(self):
        client = make_client()
        client._client.get.side_effect = [
            {"results": [{"id": "999", "key": "ENG"}]},  # space id resolution
            {"results": [{"id": "1", "title": "Page"}]},  # pages in space
        ]
        pages = client.list_pages_in_space("ENG")
        assert pages[0].title == "Page"
        assert pages[0].space_key == "ENG"
        assert client._client.get.call_args_list[1].args[0] == "api/v2/spaces/999/pages"

    def test_one_bulk_call_for_all_authors(self, monkeypatch):
        calls = patch_bulk(monkeypatch, {AUTHOR: "Ada", OTHER: "Grace"})
        client = make_client()
        client._client.get.side_effect = [
            {"results": [{"id": "999"}]},
            {"results": [
                {"id": "1", "authorId": AUTHOR},
                {"id": "2", "authorId": OTHER},
                {"id": "3", "authorId": AUTHOR},
                {"id": "4"},
            ]},
        ]
        pages = client.list_pages_in_space("ENG")
        assert len(calls) == 1
        assert sorted(calls[0]) == sorted([AUTHOR, OTHER])
        assert [p.author_name for p in pages] == ["Ada", "Grace", "Ada", ""]
        assert [p.author for p in pages] == [AUTHOR, OTHER, AUTHOR, ""]

    def test_no_bulk_call_without_authors(self, monkeypatch):
        calls = patch_bulk(monkeypatch)
        client = make_client()
        client._client.get.side_effect = [
            {"results": [{"id": "999"}]},
            {"results": [{"id": "1", "title": "Page"}]},
        ]
        client.list_pages_in_space("ENG")
        assert calls == []

    def test_space_not_found_raises_confluence_client_error(self):
        client = make_client()
        client._client.get.return_value = {"results": []}
        with pytest.raises(ConfluenceClientError, match="Space not found"):
            client.list_pages_in_space("ENG")

    def test_space_not_found_404_raises_confluence_client_error(self):
        # The v2 spaces endpoint 404s (instead of 200 + empty results) when
        # the `keys` filter matches no space.
        client = make_client()
        exc = Exception("404 Client Error: Not Found for url: ...")
        exc.response = MagicMock(status_code=404)
        client._client.get.side_effect = exc
        with pytest.raises(ConfluenceClientError, match="Space not found: 'ENG'"):
            client.list_pages_in_space("ENG")

    def test_http_error_becomes_confluence_client_error(self):
        client = make_client()
        client._client.get.side_effect = [
            {"results": [{"id": "999"}]},
            RuntimeError("boom"),
        ]
        with pytest.raises(ConfluenceClientError, match="list_pages_in_space"):
            client.list_pages_in_space("ENG")


class TestGetPage:
    def test_requires_page_id(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="requires a page_id"):
            client.get_page("")

    def test_fetches_with_body(self):
        client = make_client()
        client._client.get.return_value = {
            "id": "1", "title": "Page", "body": {"storage": {"value": "content"}},
        }
        page = client.get_page("1")
        assert page.body == "content"
        assert client._client.get.call_args.args[0] == "api/v2/pages/1"
        assert client._client.get.call_args.kwargs["params"]["body-format"] == "storage"

    def test_no_bulk_call_without_author_or_mentions(self, monkeypatch):
        calls = patch_bulk(monkeypatch)
        client = make_client()
        client._client.get.return_value = {
            "id": "1", "title": "Page", "body": {"storage": {"value": "<p>plain</p>"}},
        }
        page = client.get_page("1")
        assert calls == []
        assert page.author_name == ""
        assert page.mentions == {}

    def test_one_bulk_call_with_author_and_every_mention(self, monkeypatch):
        calls = patch_bulk(monkeypatch, {AUTHOR: "Ada", MENTIONED: "Grace", OTHER: "Linus"})
        client = make_client()
        body = f"<p>{_mention(MENTIONED)} {_mention(OTHER)} {_mention(MENTIONED)}</p>"
        client._client.get.return_value = {
            "id": "1", "authorId": AUTHOR, "body": {"storage": {"value": body}},
        }
        page = client.get_page("1")
        assert len(calls) == 1
        assert sorted(calls[0]) == sorted([AUTHOR, MENTIONED, OTHER])
        assert page.author == AUTHOR
        assert page.author_name == "Ada"
        assert page.mentions == {MENTIONED: "Grace", OTHER: "Linus"}
        assert page.body == body  # byte-for-byte unchanged

    def test_mentions_without_author_still_resolved(self, monkeypatch):
        calls = patch_bulk(monkeypatch, {MENTIONED: "Grace"})
        client = make_client()
        client._client.get.return_value = {
            "id": "1", "body": {"storage": {"value": _mention(MENTIONED)}},
        }
        page = client.get_page("1")
        assert calls == [[MENTIONED]]
        assert page.mentions == {MENTIONED: "Grace"}


class TestGetPageByTitle:
    def test_requires_space_key_and_title(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="requires space_key and title"):
            client.get_page_by_title("", "Title")
        with pytest.raises(ConfluenceClientError, match="requires space_key and title"):
            client.get_page_by_title("ENG", "")

    def test_not_found_raises_confluence_client_error(self):
        client = make_client()
        client._client.get.side_effect = [
            {"results": [{"id": "999"}]},  # space id resolution
            {"results": []},  # no matching page
        ]
        with pytest.raises(ConfluenceClientError, match="Page not found"):
            client.get_page_by_title("ENG", "Nonexistent")

    def test_found_returns_parsed_page(self):
        client = make_client()
        client._client.get.side_effect = [
            {"results": [{"id": "999"}]},
            {"results": [{"id": "1", "title": "Found"}]},
        ]
        page = client.get_page_by_title("ENG", "Found")
        assert page.title == "Found"
        assert page.space_key == "ENG"

    def test_one_bulk_call_and_body_unchanged(self, monkeypatch):
        calls = patch_bulk(monkeypatch, {AUTHOR: "Ada", MENTIONED: "Grace"})
        client = make_client()
        body = f"<p>{_mention(MENTIONED)}</p>"
        client._client.get.side_effect = [
            {"results": [{"id": "999"}]},
            {"results": [{"id": "1", "authorId": AUTHOR, "body": {"storage": {"value": body}}}]},
        ]
        page = client.get_page_by_title("ENG", "Found")
        assert len(calls) == 1
        assert sorted(calls[0]) == sorted([AUTHOR, MENTIONED])
        assert page.author_name == "Ada"
        assert page.mentions == {MENTIONED: "Grace"}
        assert page.body == body

    def test_no_bulk_call_without_ids(self, monkeypatch):
        calls = patch_bulk(monkeypatch)
        client = make_client()
        client._client.get.side_effect = [
            {"results": [{"id": "999"}]},
            {"results": [{"id": "1", "title": "Found"}]},
        ]
        client.get_page_by_title("ENG", "Found")
        assert calls == []


# ---------------------------------------------------------------------------- #
# Users: resolve_user_names / find_users / refresh_user_cache
# ---------------------------------------------------------------------------- #

def _user(account_id: str, name: str) -> atlassian_users.AtlassianUser:
    return atlassian_users.AtlassianUser(account_id=account_id, display_name=name)


class TestUserLookups:
    def test_uses_injected_directory(self):
        directory = atlassian_users.AtlassianUserDirectory(cloud_id="cloud-1")
        client = ConfluenceClient(
            config={"access_token": "tok", "cloud_id": "cloud-1"}, user_directory=directory,
        )
        assert client._users is directory

    def test_resolve_user_names_maps_ids_and_caches(self, monkeypatch):
        calls = patch_bulk(monkeypatch, {AUTHOR: "Ada"})
        client = make_client()
        assert client.resolve_user_names([AUTHOR]) == {AUTHOR: "Ada"}
        assert client.resolve_user_names([AUTHOR]) == {AUTHOR: "Ada"}
        assert calls == [[AUTHOR]]

    def test_resolve_user_names_never_raises(self, monkeypatch):
        def boom(session, cloud_id, account_ids):
            raise RuntimeError("down")

        monkeypatch.setattr(atlassian_users, "fetch_users_bulk", boom)
        client = make_client()
        assert client.resolve_user_names([AUTHOR]) == {}

    def test_bulk_call_passes_session_and_cloud_id(self, monkeypatch):
        seen = {}

        def fake(session, cloud_id, account_ids):
            seen.update(session=session, cloud_id=cloud_id, ids=account_ids)
            return []

        monkeypatch.setattr(atlassian_users, "fetch_users_bulk", fake)
        client = make_client()
        client.resolve_user_names([AUTHOR])
        assert seen == {"session": client._session, "cloud_id": "cloud-1", "ids": [AUTHOR]}

    def test_find_users_blank_query_raises(self):
        client = make_client()
        for query in ("", "   "):
            with pytest.raises(ConfluenceClientError, match="find_users requires a non-empty query"):
                client.find_users(query)

    def test_find_users_returns_users_and_remembers_them(self, monkeypatch):
        seen = {}

        def fake(session, cloud_id, query, max_results):
            seen.update(query=query, max_results=max_results)
            return [_user(AUTHOR, "Ada")]

        monkeypatch.setattr(atlassian_users, "search_users", fake)
        calls = patch_bulk(monkeypatch, {})
        client = make_client()
        users = client.find_users("ada", max_results=5)
        assert [u.account_id for u in users] == [AUTHOR]
        assert seen == {"query": "ada", "max_results": 5}
        # remembered: resolving it needs no bulk call
        assert client.resolve_user_names([AUTHOR]) == {AUTHOR: "Ada"}
        assert calls == []

    def test_find_users_clamps_max_results(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            atlassian_users, "search_users",
            lambda session, cloud_id, query, max_results: seen.append(max_results) or [],
        )
        client = make_client()
        client.find_users("a", max_results=0)
        client.find_users("a", max_results=10_000)
        assert seen == [1, atlassian_users.FIND_USERS_MAX_RESULTS]

    def test_find_users_never_logs_the_query(self, monkeypatch, caplog):
        query = "jane@example.com"
        monkeypatch.setattr(atlassian_users, "search_users",
                            lambda *a: [atlassian_users.AtlassianUser("acc-jane-000001", "Jane")])
        with caplog.at_level(logging.DEBUG):
            make_client().find_users(query)
        assert caplog.records
        assert not any(query in r.getMessage() or query in str(r.args) for r in caplog.records)

    def test_find_users_error_text_omits_the_query(self, monkeypatch):
        def boom(session, cloud_id, query, max_results):
            raise RuntimeError(f"400 Client Error for url: https://x/user/search?query=jane%40example.com&q={query}")

        monkeypatch.setattr(atlassian_users, "search_users", boom)
        with pytest.raises(ConfluenceClientError) as err:
            make_client().find_users("jane@example.com")
        assert "jane" not in str(err.value)

    def test_find_users_failure_becomes_client_error(self, monkeypatch):
        def boom(session, cloud_id, query, max_results):
            raise RuntimeError("nope")

        monkeypatch.setattr(atlassian_users, "search_users", boom)
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="find_users failed: nope"):
            client.find_users("ada")

    def test_refresh_user_cache_returns_count(self, monkeypatch):
        patch_bulk(monkeypatch, {AUTHOR: "Ada"})
        client = make_client()
        assert client.refresh_user_cache() == 0
        client.resolve_user_names([AUTHOR])
        assert client.refresh_user_cache() == 1

    def test_refresh_user_cache_error_becomes_client_error(self, monkeypatch):
        patch_bulk(monkeypatch, {AUTHOR: "Ada"})
        client = make_client()
        client.resolve_user_names([AUTHOR])

        def boom(session, cloud_id, account_ids):
            raise RuntimeError("down")

        monkeypatch.setattr(atlassian_users, "fetch_users_bulk", boom)
        with pytest.raises(ConfluenceClientError, match="refresh failed"):
            client.refresh_user_cache()

    # -- _request_jira_api: refresh only on 401 ------------------------------- #

    def _fail_refresh(self, client, monkeypatch):
        def refresh():
            raise AssertionError("_try_refresh must not be called")

        monkeypatch.setattr(client, "_try_refresh", refresh)

    def test_success_needs_no_refresh(self, monkeypatch):
        client = make_client()
        self._fail_refresh(client, monkeypatch)
        assert client._request_jira_api(lambda x: x + 1, 1) == 2

    @pytest.mark.parametrize("status", [403, 404, 500])
    def test_non_401_does_not_refresh(self, monkeypatch, status):
        client = make_client()
        self._fail_refresh(client, monkeypatch)
        exc = error_with_status(status, "boom")

        def fn():
            raise exc

        with pytest.raises(Exception) as info:
            client._request_jira_api(fn)
        assert info.value is exc

    def test_404_from_bulk_call_does_not_refresh(self, monkeypatch):
        def fake(session, cloud_id, account_ids):
            raise error_with_status(404, "Not Found")

        monkeypatch.setattr(atlassian_users, "fetch_users_bulk", fake)
        client = make_client()
        self._fail_refresh(client, monkeypatch)
        assert client.resolve_user_names([AUTHOR]) == {}

    def test_non_http_error_does_not_refresh(self, monkeypatch):
        client = make_client()
        self._fail_refresh(client, monkeypatch)

        def fn():
            raise RuntimeError("plain")

        with pytest.raises(RuntimeError):
            client._request_jira_api(fn)

    def test_401_refreshes_once_and_retries(self, monkeypatch):
        client = make_client()
        refreshes = []
        monkeypatch.setattr(client, "_try_refresh", lambda: refreshes.append(1) or True)
        attempts = []

        def fn():
            attempts.append(1)
            if len(attempts) == 1:
                raise unauthorized_error()
            return "ok"

        assert client._request_jira_api(fn) == "ok"
        assert len(refreshes) == 1
        assert len(attempts) == 2
        assert client._jira_api_denied_at is None

    def test_401_with_failed_refresh_reraises_without_marking_denied(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(client, "_try_refresh", lambda: False)
        exc = unauthorized_error()

        def fn():
            raise exc

        with pytest.raises(Exception) as info:
            client._request_jira_api(fn)
        assert info.value is exc
        assert client._jira_api_denied_at is None

    def test_non_401_after_refresh_is_reraised_without_marking_denied(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(client, "_try_refresh", lambda: True)
        attempts = []

        def fn():
            attempts.append(1)
            raise unauthorized_error() if len(attempts) == 1 else error_with_status(500, "boom")

        with pytest.raises(Exception, match="boom"):
            client._request_jira_api(fn)
        assert client._jira_api_denied_at is None

    def test_401_surviving_refresh_backs_off_for_an_hour(self, monkeypatch):
        client = make_client()
        refreshes = []
        monkeypatch.setattr(client, "_try_refresh", lambda: refreshes.append(1) or True)

        def fn():
            raise unauthorized_error()

        with freeze_time("2026-01-01 12:00:00") as clock:
            with pytest.raises(Exception, match="401"):
                client._request_jira_api(fn)
            assert len(refreshes) == 1
            assert client._jira_api_denied_at is not None

            clock.tick(1800)  # within the hour: no refresh
            with pytest.raises(Exception, match="401"):
                client._request_jira_api(fn)
            assert len(refreshes) == 1

            clock.tick(1800 + 1)  # past the hour: refreshes again
            with pytest.raises(Exception, match="401"):
                client._request_jira_api(fn)
            assert len(refreshes) == 2


# ---------------------------------------------------------------------------- #
# create_page / update_page
# ---------------------------------------------------------------------------- #

class TestCreatePage:
    def test_requires_space_key_and_title(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="requires space_key and title"):
            client.create_page("", "Title", "body")
        with pytest.raises(ConfluenceClientError, match="requires space_key and title"):
            client.create_page("ENG", "", "body")

    def test_creates_and_refetches_full_page(self):
        client = make_client()
        client._client.get.side_effect = [
            {"results": [{"id": "999"}]},  # space id resolution
            {"id": "99", "title": "New Page"},  # refetch after create
        ]
        client._client.post.return_value = {"id": "99"}

        page = client.create_page("ENG", "New Page", "<p>body</p>", parent_id="1")

        create_kwargs = client._client.post.call_args.kwargs
        assert create_kwargs["data"]["spaceId"] == "999"
        assert create_kwargs["data"]["parentId"] == "1"
        assert create_kwargs["data"]["body"] == {"representation": "storage", "value": "<p>body</p>"}
        assert page.title == "New Page"

    def test_no_parent_id_key_omitted(self):
        client = make_client()
        client._client.get.side_effect = [{"results": [{"id": "999"}]}, {"id": "99"}]
        client._client.post.return_value = {"id": "99"}
        client.create_page("ENG", "New Page", "body")
        assert "parentId" not in client._client.post.call_args.kwargs["data"]

    def test_http_error_becomes_confluence_client_error(self):
        client = make_client()
        client._client.get.return_value = {"results": [{"id": "999"}]}
        client._client.post.side_effect = RuntimeError("boom")
        with pytest.raises(ConfluenceClientError, match="create_page failed"):
            client.create_page("ENG", "Title", "body")


class TestUpdatePage:
    def test_requires_page_id_and_title(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="requires page_id and title"):
            client.update_page("", "Title", "body")
        with pytest.raises(ConfluenceClientError, match="requires page_id and title"):
            client.update_page("1", "", "body")

    def test_bumps_version_number_from_current(self):
        client = make_client()
        client._client.get.side_effect = [
            {"version": {"number": 5}},  # fetch current version
            {"id": "1", "title": "Updated", "version": {"number": 6}},  # refetch after update
        ]
        page = client.update_page("1", "Updated", "new body")

        update_kwargs = client._client.put.call_args.kwargs
        assert update_kwargs["data"]["version"]["number"] == 6
        assert page.version == 6

    def test_missing_version_defaults_to_one_then_bumps_to_two(self):
        client = make_client()
        client._client.get.side_effect = [
            {},  # no version field at all
            {"id": "1", "title": "Updated"},
        ]
        client.update_page("1", "Updated", "body")
        assert client._client.put.call_args.kwargs["data"]["version"]["number"] == 2

    def test_http_error_becomes_confluence_client_error(self):
        client = make_client()
        client._client.get.side_effect = RuntimeError("boom")
        with pytest.raises(ConfluenceClientError, match="update_page"):
            client.update_page("1", "Title", "body")


# ---------------------------------------------------------------------------- #
# list_attachments / _parse_attachment
# ---------------------------------------------------------------------------- #

class TestListAttachments:
    def test_requires_page_id(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="requires a page_id"):
            client.list_attachments("")

    def test_maps_results(self):
        client = make_client()
        client._client.get.return_value = {
            "results": [
                {"id": "att1", "title": "diagram.png", "mediaType": "image/png", "fileSize": 2048},
            ],
        }
        attachments = client.list_attachments("1")
        assert attachments == [
            ConfluenceAttachment(
                name="diagram.png", media_type="image/png", size=2048, attachment_id="att1",
            ),
        ]
        assert client._client.get.call_args.args[0] == "api/v2/pages/1/attachments"

    def test_missing_size_defaults_to_zero(self):
        client = make_client()
        client._client.get.return_value = {"results": [{"title": "f.txt", "mediaType": "text/plain"}]}
        attachments = client.list_attachments("1")
        assert attachments[0].size == 0

    def test_none_response_yields_empty_list(self):
        client = make_client()
        client._client.get.return_value = None
        assert client.list_attachments("1") == []

    def test_http_error_becomes_confluence_client_error(self):
        client = make_client()
        client._client.get.side_effect = RuntimeError("boom")
        with pytest.raises(ConfluenceClientError, match="list_attachments"):
            client.list_attachments("1")


# ---------------------------------------------------------------------------- #
# resolve_attachment_destination: path-traversal sanitization -- same shape
# as gmail_client's test of the same name for its own copy of this helper.
# ---------------------------------------------------------------------------- #

class TestResolveAttachmentDestination:
    def test_joins_basename_with_destination_dir(self, tmp_path):
        result = resolve_attachment_destination("report.pdf", str(tmp_path))
        assert result == str(tmp_path / "report.pdf")

    def test_strips_directory_traversal_from_filename(self, tmp_path):
        result = resolve_attachment_destination("../../.ssh/authorized_keys", str(tmp_path))
        assert result == str(tmp_path / "authorized_keys")

    def test_strips_absolute_path_prefix_from_filename(self, tmp_path):
        result = resolve_attachment_destination("/etc/passwd", str(tmp_path))
        assert result == str(tmp_path / "passwd")

    def test_empty_filename_falls_back_to_generic_name(self, tmp_path):
        assert resolve_attachment_destination("", str(tmp_path)) == str(tmp_path / "attachment")

    def test_empty_destination_dir_raises(self):
        with pytest.raises(ConfluenceClientError, match="destination_dir"):
            resolve_attachment_destination("report.pdf", "")

    def test_whitespace_only_destination_dir_raises(self):
        with pytest.raises(ConfluenceClientError, match="destination_dir"):
            resolve_attachment_destination("report.pdf", "   ")


# ---------------------------------------------------------------------------- #
# fetch_attachment_bytes / save_attachment_bytes / download_attachment
# ---------------------------------------------------------------------------- #

class TestFetchAttachmentBytes:
    def test_requires_page_id_and_attachment_id(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="non-empty page_id and attachment_id"):
            client.fetch_attachment_bytes("", "att1")
        with pytest.raises(ConfluenceClientError, match="non-empty page_id and attachment_id"):
            client.fetch_attachment_bytes("1", "")

    def test_fetches_via_the_v1_download_redirect_endpoint(self):
        # Deliberately NOT the v2 attachments-list response's own
        # _links.download/downloadLink -- that link 401s for an OAuth 3LO
        # bearer token regardless of scope (browser/cookie-session only).
        # This is Atlassian's actual supported replacement for 3LO apps.
        client = make_client()
        response = MagicMock(content=b"file bytes")
        client._session = MagicMock()
        client._session.get.return_value = response

        data = client.fetch_attachment_bytes("1", "att1")

        assert data == b"file bytes"
        client._session.get.assert_called_once_with(
            "https://api.atlassian.com/ex/confluence/cloud-1/wiki"
            "/rest/api/content/1/child/attachment/att1/download"
        )
        response.raise_for_status.assert_called_once()

    def test_http_error_becomes_confluence_client_error(self):
        client = make_client()
        response = MagicMock()
        response.raise_for_status.side_effect = RuntimeError("boom")
        client._session = MagicMock()
        client._session.get.return_value = response

        with pytest.raises(ConfluenceClientError, match="fetch_attachment_bytes"):
            client.fetch_attachment_bytes("1", "att1")


class TestSaveAttachmentBytes:
    def test_sanitizes_filename_before_writing(self, tmp_path):
        client = make_client()

        result = client.save_attachment_bytes(b"data", "../../evil.txt", str(tmp_path))

        assert result == {"path": str(tmp_path / "evil.txt"), "name": "evil.txt", "size_bytes": 4}
        assert (tmp_path / "evil.txt").read_bytes() == b"data"

    def test_creates_destination_directory_if_missing(self, tmp_path):
        nested = tmp_path / "nested" / "dir"
        client = make_client()

        client.save_attachment_bytes(b"data", "f.txt", str(nested))

        assert (nested / "f.txt").read_bytes() == b"data"

    def test_unwritable_destination_becomes_confluence_client_error(self, tmp_path, monkeypatch):
        # A disk-level failure (permission denied, a read-only/synthetic
        # mount point that rejects mkdir, etc.) must surface as
        # ConfluenceClientError like every other failure in this method --
        # not a bare OSError, which coding-and-testing-guidelines.md §1.4
        # requires every *_client.py public method to never leak.
        client = make_client()
        monkeypatch.setattr(
            confluence_client_module.os, "makedirs",
            MagicMock(side_effect=OSError(45, "Operation not supported")),
        )

        with pytest.raises(ConfluenceClientError, match="could not write"):
            client.save_attachment_bytes(b"data", "f.txt", str(tmp_path))


class TestDownloadAttachment:
    def test_downloads_and_saves_content(self, tmp_path):
        client = make_client()
        response = MagicMock(content=b"file contents")
        client._session = MagicMock()
        client._session.get.return_value = response

        result = client.download_attachment("1", "att1", "report.pdf", str(tmp_path))

        dest = tmp_path / "report.pdf"
        assert dest.read_bytes() == b"file contents"
        assert result == {"path": str(dest), "name": "report.pdf", "size_bytes": len(b"file contents")}
        client._session.get.assert_called_once_with(
            "https://api.atlassian.com/ex/confluence/cloud-1/wiki"
            "/rest/api/content/1/child/attachment/att1/download"
        )

    def test_sanitizes_filename_before_writing(self, tmp_path):
        client = make_client()
        response = MagicMock(content=b"data")
        client._session = MagicMock()
        client._session.get.return_value = response

        result = client.download_attachment("1", "att1", "../../evil.txt", str(tmp_path))

        assert result == {"path": str(tmp_path / "evil.txt"), "name": "evil.txt", "size_bytes": 4}

    def test_http_error_becomes_confluence_client_error(self, tmp_path):
        client = make_client()
        response = MagicMock()
        response.raise_for_status.side_effect = RuntimeError("boom")
        client._session = MagicMock()
        client._session.get.return_value = response

        # download_attachment delegates the actual fetch to
        # fetch_attachment_bytes -- the error originates there.
        with pytest.raises(ConfluenceClientError, match="fetch_attachment_bytes"):
            client.download_attachment("1", "att1", "f.pdf", str(tmp_path))


class TestLiveFixtureParsing:
    """Replays fixtures recorded from a real, [QATEST]-tagged seed page by
    scripts/qa_fixture_recorder.py --record confluence -- real API shape,
    not hand-authored, with identity fields already redacted. Skipped
    (not failed) until that fixture exists; see
    tests/fixtures/live/README.md and
    docs/testing-policy.md. Re-record via that
    script if this ever starts failing after a genuine Confluence API
    change.
    """

    def _load(self, name: str) -> dict:
        path = LIVE_FIXTURES_DIR / name
        if not path.exists():
            pytest.skip(
                f"{path} not recorded yet -- run "
                "`python3 scripts/qa_fixture_recorder.py --record confluence` locally first"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_get_page_fixture_still_parses(self):
        client = make_client()
        raw = self._load("get_page.json")
        # get_page.json is the {"results": [...]} envelope from
        # get_page_by_title when the manifest has no seed_page_id yet --
        # unwrap the same way ConfluenceClient.get_page_by_title does.
        page_raw = raw["results"][0] if "results" in raw else raw
        page = client._parse_page_v2(page_raw, include_body=True)
        assert page.title and page.author and page.updated and page.space_key

    def test_list_spaces_fixture_still_parses(self):
        client = make_client()
        raw = self._load("list_spaces.json")
        spaces = [client._parse_space(s) for s in raw.get("results", [])]
        assert spaces, "recorded list_spaces.json has no results"
        assert all(s.key and s.name for s in spaces)
