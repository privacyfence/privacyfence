"""Tests for the Peek client and device sign-in.

The invariant: a Peek token is only ever sent to the server it was issued
for. The credential file records that server, ``resolve_server`` refuses a
mismatch with an organization pin, every request goes to the recorded
server, and redirects are never followed. Requests are faked at the
``requests.Session.request`` boundary; nothing touches the network.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
import requests

from privacyfence import peek_client
from privacyfence.peek_client import (
    PeekClient,
    PeekClientError,
    PeekComment,
    PeekGrant,
    PeekVersion,
    bundle_base_url,
    load_token_file,
    normalize_base_url,
    normalize_visibility,
    poll_device_login,
    resolve_server,
    save_token_file,
    start_device_login,
    validate_grant,
    validate_slug,
)

pytestmark = pytest.mark.unit

BASE = "https://peek.example.com"
TOKEN = "tok-SECRET-123"
DEVICE = "dev-SECRET-456"

LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "peek"


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, *, raw: bool = False) -> None:
        self.status_code = status
        self._body = body
        self._raw = raw

    def json(self) -> Any:
        if self._raw or self._body is None:
            raise ValueError("not json")
        return self._body


class Recorder:
    """Replaces ``requests.Session.request`` and records every call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        monkeypatch.setattr(requests.Session, "request", self._request)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def client() -> PeekClient:
    return PeekClient(BASE, TOKEN)


def ok(body: Any) -> FakeResponse:
    return FakeResponse(200, body)


class TestNormalizeBaseUrl:
    @pytest.mark.parametrize("raw", ["", "   ", "peek.example.com", "https://", "http://[::1"])
    def test_empty_or_no_host(self, raw):
        with pytest.raises(PeekClientError, match="Enter the Peek server address, such as https://peek.example.com."):
            normalize_base_url(raw)

    @pytest.mark.parametrize("raw", ["http://peek.example.com", "ftp://peek.example.com", "http://10.0.0.1"])
    def test_scheme_rule(self, raw):
        with pytest.raises(PeekClientError) as exc:
            normalize_base_url(raw)
        assert str(exc.value) == (
            "The Peek server address must start with https:// (http:// is allowed only for localhost)."
        )

    @pytest.mark.parametrize(
        "raw",
        ["https://u:p@peek.example.com", "https://peek.example.com?x=1", "https://peek.example.com/#frag"],
    )
    def test_userinfo_query_fragment(self, raw):
        with pytest.raises(PeekClientError) as exc:
            normalize_base_url(raw)
        assert str(exc.value) == "The Peek server address must not contain a user name, password, query or fragment."

    def test_lowercases_strips_and_keeps_path_prefix(self):
        assert normalize_base_url("  HTTPS://Peek.Example.COM/Team/  ") == "https://peek.example.com/Team"

    def test_port_is_kept(self):
        assert normalize_base_url("https://peek.example.com:8443/") == "https://peek.example.com:8443"

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("http://localhost:7700", "http://localhost:7700"),
            ("http://127.0.0.1", "http://127.0.0.1"),
            ("http://[::1]:7700/", "http://[::1]:7700"),
        ],
    )
    def test_loopback_http_accepted(self, raw, expected):
        assert normalize_base_url(raw) == expected


class TestValidateSlug:
    @pytest.mark.parametrize("slug", ["PhiUs-lMbZE_Sw", "a" * 67, "a" * 128])
    def test_accepts(self, slug):
        assert validate_slug(slug) == slug

    @pytest.mark.parametrize("slug", ["../x", "a/b", "a?b", "", "a" * 129])
    def test_rejects(self, slug):
        with pytest.raises(PeekClientError, match="Not a Peek page slug"):
            validate_slug(slug)


class TestBundleBaseUrl:
    def test_absent_is_empty(self):
        assert bundle_base_url({}, org_mode=False) == ""
        assert bundle_base_url({"base_url": ""}, org_mode=False) == ""

    def test_org_mode_without_url(self):
        with pytest.raises(PeekClientError) as exc:
            bundle_base_url({}, org_mode=True)
        assert str(exc.value) == "Peek organization config not installed"

    def test_normalized(self):
        assert bundle_base_url({"base_url": "https://Peek.example.com/"}, org_mode=True) == BASE

    def test_bad_url(self):
        with pytest.raises(PeekClientError, match="Peek organization config is not usable: The Peek server address must start"):
            bundle_base_url({"base_url": "http://peek.example.com"}, org_mode=False)


class TestSendErrors:
    def _error(self, monkeypatch, client, response) -> PeekClientError:
        Recorder(monkeypatch, response)
        with pytest.raises(PeekClientError) as exc:
            client.list_pages()
        return exc.value

    def test_connection_error(self, monkeypatch, client):
        err = self._error(monkeypatch, client, requests.ConnectionError("boom " + TOKEN))
        assert str(err) == "Could not reach Peek at peek.example.com: ConnectionError"
        assert err.status is None

    def test_redirect(self, monkeypatch, client):
        err = self._error(monkeypatch, client, FakeResponse(302))
        assert str(err) == "Peek answered with a redirect (HTTP 302). Check the Peek server address."
        assert err.status == 302

    def test_401(self, monkeypatch, client):
        err = self._error(monkeypatch, client, FakeResponse(401, {"error": "bad token"}))
        assert str(err) == (
            "Peek refused the saved sign-in (HTTP 401). "
            "Use Authenticate… in PrivacyFence Settings to connect again."
        )
        assert err.status == 401

    def test_403(self, monkeypatch, client):
        err = self._error(monkeypatch, client, FakeResponse(403, {"error": "not owner"}))
        assert str(err) == "Peek refused the request (HTTP 403): not owner"
        assert err.status == 403

    def test_404(self, monkeypatch, client):
        err = self._error(monkeypatch, client, FakeResponse(404, {"error": "not found"}))
        assert str(err) == (
            "Peek found no such page (HTTP 404). It may have been renamed or deleted: "
            "call peek_list_pages to see the current slugs."
        )
        assert err.status == 404

    def test_404_is_page_not_found_even_with_renamed_text(self, monkeypatch, client):
        err = self._error(monkeypatch, client, FakeResponse(404, {"error": "page was renamed; use slug new-1"}))
        assert str(err).startswith("Peek found no such page (HTTP 404).")
        assert "new-1" not in str(err)
        assert err.status == 404

    @pytest.mark.parametrize(
        "status, body, detail",
        [
            (403, {"error": "not owner"}, "not owner"),
            (404, {"error": "version not found"}, "version not found"),
            (409, {"error": "slug taken"}, "slug taken"),
            (500, {"error": "x" * 300}, "x" * 200),
            (500, None, "no detail"),
        ],
    )
    def test_error_carries_detail(self, monkeypatch, client, status, body, detail):
        err = self._error(monkeypatch, client, FakeResponse(status, body))
        assert err.detail == detail
        assert err.status == status

    def test_error_without_an_answer_has_no_detail(self, monkeypatch, client):
        err = self._error(monkeypatch, client, requests.ConnectionError("boom"))
        assert err.detail == ""

    def test_429(self, monkeypatch, client):
        err = self._error(monkeypatch, client, FakeResponse(429, {"error": "too many comments, slow down"}))
        assert str(err) == (
            "Peek is limiting how fast these requests can be made (HTTP 429). Wait a minute, then try again."
        )
        assert err.status == 429
        assert err.detail == "too many comments, slow down"

    def test_415(self, monkeypatch, client):
        err = self._error(monkeypatch, client, FakeResponse(415, {"error": "file does not look like HTML"}))
        assert str(err) == "Peek refused the file (HTTP 415): file does not look like HTML"
        assert err.status == 415

    def test_500_detail_is_cut_to_200(self, monkeypatch, client):
        err = self._error(monkeypatch, client, FakeResponse(500, {"error": "x" * 300}))
        assert str(err) == f"Peek API error (HTTP 500): {'x' * 200}"
        assert err.status == 500

    @pytest.mark.parametrize("body", [None, ["a"], {"error": ""}, {"error": 5}])
    def test_500_without_usable_detail(self, monkeypatch, client, body):
        err = self._error(monkeypatch, client, FakeResponse(500, body))
        assert str(err) == "Peek API error (HTTP 500): no detail"

    def test_html_body_on_200(self, monkeypatch, client):
        err = self._error(monkeypatch, client, FakeResponse(200, raw=True))
        assert str(err) == "Peek answered with something other than JSON (HTTP 200). Check the Peek server address."
        assert err.status == 200

    def test_every_request_has_timeout_and_no_redirects(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok([]))
        client.list_pages()
        assert rec.calls[0]["timeout"] == 30
        assert rec.calls[0]["allow_redirects"] is False

    def test_non_list_answer_is_rejected(self, monkeypatch, client):
        err = self._error(monkeypatch, client, ok({"a": 1}))
        assert "something unexpected" in str(err)


LOGIN = {
    "device_code": DEVICE,
    "user_code": "ABCD-1234",
    "verification_url": f"{BASE}/cli/login?code=ABCD-1234",
    "interval": 3,
    "expires_in": 900,
}


class TestStartDeviceLogin:
    def test_parses(self, monkeypatch):
        rec = Recorder(monkeypatch, ok(LOGIN))
        monkeypatch.setattr(peek_client.time, "time", lambda: 1000.0)
        login = start_device_login(BASE)
        assert (login.device_code, login.user_code, login.interval) == (DEVICE, "ABCD-1234", 3)
        assert login.verification_url == LOGIN["verification_url"]
        assert login.expires_at == 1900.0
        call = rec.calls[0]
        assert (call["method"], call["url"], call["json"]) == ("POST", f"{BASE}/api/cli/login/start", {})
        assert "headers" not in call
        assert call["allow_redirects"] is False and call["timeout"] == 30

    @pytest.mark.parametrize("key", ["device_code", "user_code", "verification_url", "expires_in"])
    def test_missing_key(self, monkeypatch, key):
        Recorder(monkeypatch, ok({k: v for k, v in LOGIN.items() if k != key}))
        with pytest.raises(PeekClientError) as exc:
            start_device_login(BASE)
        assert str(exc.value) == "Peek answered the sign-in request without a sign-in code."

    @pytest.mark.parametrize("url", ["https://evil.example.com/cli", f"{BASE}.evil.com/cli", f"{BASE}"])
    def test_verification_url_on_another_host(self, monkeypatch, url):
        Recorder(monkeypatch, ok({**LOGIN, "verification_url": url}))
        with pytest.raises(PeekClientError) as exc:
            start_device_login(BASE)
        assert str(exc.value) == (
            "Peek answered with a sign-in address on another server. Check the Peek server address."
        )

    def test_verification_url_on_another_path_prefix(self, monkeypatch):
        Recorder(monkeypatch, ok({**LOGIN, "verification_url": f"{BASE}/other/cli"}))
        with pytest.raises(PeekClientError):
            start_device_login(f"{BASE}/team")

    def test_interval_below_one_becomes_two(self, monkeypatch):
        Recorder(monkeypatch, ok({**LOGIN, "interval": 0}))
        assert start_device_login(BASE).interval == 2

    @pytest.mark.parametrize("status", [404, 405])
    def test_no_device_sign_in(self, monkeypatch, status):
        Recorder(monkeypatch, FakeResponse(status, {"error": "x"}))
        with pytest.raises(PeekClientError) as exc:
            start_device_login(BASE)
        assert str(exc.value) == (
            f"Peek has no device sign-in at this address (HTTP {status}). Check the Peek server address."
        )
        assert exc.value.status == status

    def test_other_errors_pass_through(self, monkeypatch):
        Recorder(monkeypatch, FakeResponse(500, {"error": "down"}))
        with pytest.raises(PeekClientError, match="HTTP 500"):
            start_device_login(BASE)

    def test_repr_hides_the_device_code(self, monkeypatch):
        Recorder(monkeypatch, ok(LOGIN))
        assert DEVICE not in repr(start_device_login(BASE))


class TestPollDeviceLogin:
    def test_request(self, monkeypatch):
        rec = Recorder(monkeypatch, ok({"status": "pending"}))
        assert poll_device_login(BASE, DEVICE).status == "pending"
        assert rec.calls[0]["url"] == f"{BASE}/api/cli/login/poll"
        assert rec.calls[0]["json"] == {"device_code": DEVICE}

    def test_approved(self, monkeypatch):
        Recorder(monkeypatch, ok({"status": "approved", "token": TOKEN}))
        poll = poll_device_login(BASE, DEVICE)
        assert (poll.status, poll.token) == ("approved", TOKEN)
        assert TOKEN not in repr(poll)

    @pytest.mark.parametrize("status", ["pending", "denied", "expired", "consumed"])
    def test_other_statuses_carry_no_token(self, monkeypatch, status):
        Recorder(monkeypatch, ok({"status": status, "token": "ignored"}))
        poll = poll_device_login(BASE, DEVICE)
        assert (poll.status, poll.token) == (status, "")

    @pytest.mark.parametrize("body", [{"status": "approved"}, {"status": "approved", "token": ""}, {"status": "weird"}, {}])
    def test_unexpected(self, monkeypatch, body):
        Recorder(monkeypatch, ok(body))
        with pytest.raises(PeekClientError) as exc:
            poll_device_login(BASE, DEVICE)
        assert str(exc.value) == "Peek answered the sign-in check with something unexpected."
        assert TOKEN not in str(exc.value) and DEVICE not in str(exc.value)

    def test_405(self, monkeypatch):
        Recorder(monkeypatch, FakeResponse(405, {"error": "x"}))
        with pytest.raises(PeekClientError, match="no device sign-in at this address \\(HTTP 405\\)"):
            poll_device_login(BASE, DEVICE)


class TestTokenFile:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "credentials" / "peek_token.json")
        save_token_file(path, BASE, TOKEN)
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
        assert saved == {"base_url": BASE, "token": TOKEN}
        assert load_token_file(path) == {"base_url": BASE, "token": TOKEN}

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_mode_is_0600(self, tmp_path):
        path = tmp_path / "peek_token.json"
        save_token_file(str(path), BASE, TOKEN)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_missing_file(self, tmp_path):
        with pytest.raises(PeekClientError) as exc:
            load_token_file(str(tmp_path / "nope.json"))
        assert str(exc.value) == "Peek is not authenticated. Use Authenticate… in PrivacyFence Settings."

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            "[]",
            json.dumps({"base_url": BASE}),
            json.dumps({"base_url": BASE, "token": ""}),
            json.dumps({"token": TOKEN}),
            json.dumps({"base_url": "http://peek.example.com", "token": TOKEN}),
        ],
    )
    def test_unreadable(self, tmp_path, content):
        path = tmp_path / "peek_token.json"
        path.write_text(content)
        with pytest.raises(PeekClientError) as exc:
            load_token_file(str(path))
        assert str(exc.value) == (
            "Peek's saved sign-in could not be read. Use Authenticate… in PrivacyFence Settings to connect again."
        )
        assert TOKEN not in str(exc.value)

    def test_a_directory_is_unreadable(self, tmp_path):
        with pytest.raises(PeekClientError, match="could not be read"):
            load_token_file(str(tmp_path))

    def test_trailing_slash_normalized_on_load(self, tmp_path):
        path = tmp_path / "peek_token.json"
        path.write_text(json.dumps({"base_url": BASE + "/", "token": TOKEN}))
        assert load_token_file(str(path))["base_url"] == BASE


class TestResolveServer:
    record = {"base_url": BASE, "token": TOKEN}

    def test_no_pin(self):
        assert resolve_server("", self.record) == BASE

    def test_equal_pin(self):
        assert resolve_server(BASE, self.record) == BASE

    def test_different_pin(self):
        with pytest.raises(PeekClientError) as exc:
            resolve_server("https://other.example.com", self.record)
        assert str(exc.value) == (
            "Peek was connected to a different server than your organization uses. "
            "Use Authenticate… in PrivacyFence Settings to connect again."
        )
        assert TOKEN not in str(exc.value)


def page_json(slug: str = "p1", created_at: int = 1760094420, **extra: Any) -> dict[str, Any]:
    return {
        "slug": slug, "name": f"{slug}.html", "owner": "qa", "size": 10, "visibility": "private",
        "url": f"{BASE}/p/{slug}", "created_at": created_at, "version": 2, **extra,
    }


def auth_of(call: dict[str, Any]) -> str:
    return call["headers"]["Authorization"]


class TestClientBasics:
    def test_repr_and_host(self, client):
        assert repr(client) == "PeekClient(peek.example.com)"
        assert TOKEN not in repr(client)
        assert client.host == "peek.example.com"
        assert client.base_url == BASE

    def test_check_connection(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok([]))
        assert client.check_connection() == "peek.example.com"
        assert (rec.calls[0]["method"], rec.calls[0]["url"]) == ("GET", f"{BASE}/api/uploads")
        assert auth_of(rec.calls[0]) == f"Bearer {TOKEN}"


class TestListPages:
    def test_newest_first_and_parsed(self, monkeypatch, client):
        Recorder(monkeypatch, ok([page_json("old", 1000), page_json("new", 1760094420, visibility="restricted")]))
        pages = client.list_pages()
        assert [p.slug for p in pages] == ["new", "old"]
        first = pages[0]
        assert first.created_at == "2025-10-10T11:07:00Z"
        assert (first.version, first.size_bytes, first.owner, first.visibility) == (2, 10, "qa", "restricted")
        assert first.url == f"{BASE}/p/new"

    def test_missing_keys(self, monkeypatch, client):
        Recorder(monkeypatch, ok([{}, "junk"]))
        page = client.list_pages()[0]
        assert (page.slug, page.created_at, page.version, page.size_bytes) == ("", "", 0, 0)

    def test_bool_is_not_a_number(self, monkeypatch, client):
        Recorder(monkeypatch, ok([{"version": True}]))
        assert client.list_pages()[0].version == 0

    def test_text_time_passes_through(self, monkeypatch, client):
        Recorder(monkeypatch, ok([{"created_at": "2026-01-01T00:00:00Z"}]))
        assert client.list_pages()[0].created_at == "2026-01-01T00:00:00Z"


class TestGetPage:
    def test_found(self, monkeypatch, client):
        Recorder(monkeypatch, ok([page_json("a"), page_json("b")]))
        assert client.get_page("b").slug == "b"

    def test_missing(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok([page_json("a")]))
        with pytest.raises(PeekClientError) as exc:
            client.get_page("zzz")
        assert str(exc.value) == (
            "No Peek page 'zzz' among the pages this account can manage. "
            "It may have been renamed or deleted: call peek_list_pages to see the current slugs."
        )
        assert len(rec.calls) == 1


def comment_json(id: int, created_at: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": id, "selector": "p", "element_text": "hi", "anchor_kind": "element", "author": "qa",
        "body": f"c{id}", "created_at": created_at, "version": 2, **extra,
    }


class TestGetComments:
    def test_sorted_and_parsed_without_version(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok([comment_json(3, 20), comment_json(2, 10), comment_json(1, 10)]))
        comments = client.get_comments("p1")
        assert [c.id for c in comments] == [1, 2, 3]
        assert comments[0] == PeekComment(1, "qa", "c1", "element", "p", "hi", "1970-01-01T00:00:10Z", 2)
        call = rec.calls[0]
        assert (call["method"], call["url"]) == ("GET", f"{BASE}/api/uploads/p1/comments")
        assert "params" not in call
        assert auth_of(call) == f"Bearer {TOKEN}"

    def test_version_sends_v(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok([]))
        assert client.get_comments("p1", 3) == []
        assert rec.calls[0]["params"] == {"v": "3"}

    def test_404_with_version(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(404, {"error": "version not found"}))
        with pytest.raises(PeekClientError) as exc:
            client.get_comments("p1", 3)
        assert str(exc.value) == "Peek has no version 3 of this page (HTTP 404). Retention may have removed it."
        assert exc.value.status == 404

    def test_404_without_version_is_the_plain_404(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(404, {"error": "not found"}))
        with pytest.raises(PeekClientError, match="Peek found no such page"):
            client.get_comments("p1")

    def test_other_error_with_version_passes_through(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(403, {"error": "not owner"}))
        with pytest.raises(PeekClientError, match="HTTP 403"):
            client.get_comments("p1", 2)


class TestGetStats:
    def test_ip_and_user_agent_dropped(self, monkeypatch, client):
        rec = Recorder(
            monkeypatch,
            ok({
                "slug": "p1", "name": "P", "total_visits": 5, "unique_visitors": 2,
                "recent": [{"name": "Ann", "ip": "10.1.2.3", "user_agent": "UA/1", "visited_at": 60}, "junk"],
            }),
        )
        stats = client.get_stats("p1")
        assert rec.calls[0]["url"] == f"{BASE}/api/uploads/p1/stats"
        assert (stats.slug, stats.name, stats.total_visits, stats.unique_visitors) == ("p1", "P", 5, 2)
        assert [(v.visitor_name, v.visited_at) for v in stats.recent] == [
            ("Ann", "1970-01-01T00:01:00Z"), ("", ""),
        ]
        assert set(vars(stats.recent[0])) == {"visitor_name", "visited_at"}
        assert "10.1.2.3" not in repr(stats) and "UA/1" not in repr(stats)

    def test_no_recent(self, monkeypatch, client):
        Recorder(monkeypatch, ok({"total_visits": 0}))
        assert client.get_stats("p1").recent == []


class TestNormalizeVisibility:
    @pytest.mark.parametrize("value", ["public", "signed-in", "restricted"])
    def test_peeks_values_pass(self, value):
        assert normalize_visibility(value) == value

    def test_private_becomes_signed_in(self):
        assert normalize_visibility("private") == "signed-in"
        assert normalize_visibility(" private ") == "signed-in"

    def test_password_is_refused_with_a_hint(self):
        with pytest.raises(PeekClientError) as exc:
            normalize_visibility("password")
        assert str(exc.value) == (
            "Peek no longer has page passwords. Use restricted, then grant access to people with peek_grant_access."
        )

    @pytest.mark.parametrize("value", ["", "other", "Public"])
    def test_other_values_are_refused(self, value):
        with pytest.raises(PeekClientError) as exc:
            normalize_visibility(value)
        assert str(exc.value) == "visibility must be public, signed-in or restricted."


class TestUploadPage:
    result = {"slug": "n1", "url": f"{BASE}/p/n1", "visibility": "signed-in", "version": 1, "version_url": f"{BASE}/p/n1/v/1"}

    def test_new_page(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok(self.result))
        res = client.upload_page(b"<p>hi</p>", "r.html", "signed-in")
        call = rec.calls[0]
        assert (call["method"], call["url"]) == ("POST", f"{BASE}/api/upload")
        assert call["files"] == {"file": ("r.html", b"<p>hi</p>", "text/html")}
        assert call["data"] == {"visibility": "signed-in"}
        assert auth_of(call) == f"Bearer {TOKEN}"
        assert (res.slug, res.url, res.visibility, res.version, res.version_url) == (
            "n1", f"{BASE}/p/n1", "signed-in", 1, f"{BASE}/p/n1/v/1",
        )

    def test_private_is_sent_as_signed_in(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok(self.result))
        client.upload_page(b"x", "r.html", "private")
        assert rec.calls[0]["data"] == {"visibility": "signed-in"}

    def test_never_sends_a_password(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok(self.result))
        client.upload_page(b"x", "r.html", "restricted")
        assert "password" not in rec.calls[0]["data"]

    def test_new_version_sends_only_slug(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok(self.result))
        client.upload_page(b"x", "r.html", "", slug="p1")
        assert rec.calls[0]["data"] == {"slug": "p1"}

    def test_new_version_rejects_visibility(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok(self.result))
        with pytest.raises(PeekClientError) as exc:
            client.upload_page(b"x", "r.html", "public", slug="p1")
        assert str(exc.value) == "A new version keeps the page's visibility; change it with set_visibility."
        assert rec.calls == []

    @pytest.mark.parametrize("visibility", ["password", "", "other"])
    def test_bad_visibility(self, monkeypatch, client, visibility):
        rec = Recorder(monkeypatch, ok(self.result))
        with pytest.raises(PeekClientError):
            client.upload_page(b"x", "r.html", visibility)
        assert rec.calls == []


class TestSetVisibility:
    def test_request(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok({"visibility": "restricted", "notified": True}))
        res = client.set_visibility("p1", "restricted")
        call = rec.calls[0]
        assert (call["method"], call["url"]) == ("POST", f"{BASE}/api/uploads/p1/visibility")
        assert call["json"] == {"visibility": "restricted"}
        assert (res.visibility, res.notified) == ("restricted", True)

    def test_private_is_sent_as_signed_in(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok({"visibility": "signed-in", "notified": False}))
        assert client.set_visibility("p1", "private").notified is False
        assert rec.calls[0]["json"] == {"visibility": "signed-in"}

    @pytest.mark.parametrize("visibility", ["password", "other"])
    def test_checks_before_any_request(self, monkeypatch, client, visibility):
        rec = Recorder(monkeypatch, ok({}))
        with pytest.raises(PeekClientError):
            client.set_visibility("p1", visibility)
        assert rec.calls == []

    def test_missing_answer_keys(self, monkeypatch, client):
        Recorder(monkeypatch, ok({}))
        res = client.set_visibility("p1", "public")
        assert (res.visibility, res.notified) == ("", False)


class TestDeletePage:
    def test_request(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok({"deleted": "p1"}))
        assert client.delete_page("p1") is None
        assert (rec.calls[0]["method"], rec.calls[0]["url"]) == ("DELETE", f"{BASE}/api/uploads/p1")
        assert auth_of(rec.calls[0]) == f"Bearer {TOKEN}"


class TestRenamePage:
    def test_request_and_result(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok({"slug": "new-name", "url": f"{BASE}/p/new-name", "name": "New name"}))
        res = client.rename_page("p1", "New name")
        assert (rec.calls[0]["method"], rec.calls[0]["url"]) == ("POST", f"{BASE}/api/uploads/p1/name")
        assert rec.calls[0]["json"] == {"name": "New name"}
        assert (res.slug, res.url, res.name) == ("new-name", f"{BASE}/p/new-name", "New name")

    @pytest.mark.parametrize("name", ["", "   ", "x" * 201])
    def test_bad_name_before_any_request(self, monkeypatch, client, name):
        rec = Recorder(monkeypatch, ok({}))
        with pytest.raises(PeekClientError) as exc:
            client.rename_page("p1", name)
        assert str(exc.value) == "A Peek page name is 1 to 200 characters."
        assert rec.calls == []


class TestAddComment:
    def test_request_and_result(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok({"id": 9, "author": "qa", "body": "hello", "anchor_kind": "text",
                                        "selector": "p", "element_text": "hi", "created_at": 60}))
        comment = client.add_comment("p1", "hello", "p", "hi")
        call = rec.calls[0]
        assert (call["method"], call["url"]) == ("POST", f"{BASE}/api/uploads/p1/account-comments")
        assert call["json"] == {"body": "hello", "selector": "p", "element_text": "hi"}
        assert (comment.id, comment.author, comment.body, comment.version) == (9, "qa", "hello", 0)

    @pytest.mark.parametrize("status", [404, 405])
    def test_no_endpoint(self, monkeypatch, client, status):
        Recorder(monkeypatch, FakeResponse(status, {"error": "nope"}))
        with pytest.raises(PeekClientError) as exc:
            client.add_comment("p1", "hello", "", "")
        assert str(exc.value) == (
            "This Peek server does not accept comments from PrivacyFence: "
            "it has no account comment endpoint. See the Peek setup guide."
        )
        assert exc.value.status == status

    def test_other_errors_pass_through(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(403, {"error": "not owner"}))
        with pytest.raises(PeekClientError, match="HTTP 403"):
            client.add_comment("p1", "hello", "", "")

    def test_version_is_sent_only_above_zero(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok({"id": 9}))
        client.add_comment("p1", "hello", "", "")
        client.add_comment("p1", "hello", "", "", version=0)
        client.add_comment("p1", "hello", "", "", version=3)
        assert "params" not in rec.calls[0] and "params" not in rec.calls[1]
        assert rec.calls[2]["params"] == {"v": "3"}

    def test_version_not_found(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(404, {"error": "version not found"}))
        with pytest.raises(PeekClientError) as exc:
            client.add_comment("p1", "hello", "", "", version=7)
        assert str(exc.value) == "Peek has no version 7 of this page (HTTP 404). Retention may have removed it."
        assert exc.value.status == 404

    @pytest.mark.parametrize("version, detail", [(0, "version not found"), (7, "not found")])
    def test_other_404s_are_the_missing_endpoint(self, monkeypatch, client, version, detail):
        Recorder(monkeypatch, FakeResponse(404, {"error": detail}))
        with pytest.raises(PeekClientError, match="no account comment endpoint"):
            client.add_comment("p1", "hello", "", "", version=version)


class TestVerified:
    @pytest.mark.parametrize("extra, expected", [({"verified": True}, True), ({"verified": False}, False), ({}, False)])
    def test_parsed(self, monkeypatch, client, extra, expected):
        Recorder(monkeypatch, ok([comment_json(1, 10, **extra)]))
        assert client.get_comments("p1")[0].verified is expected

    @pytest.mark.parametrize("value", ["true", 1, None])
    def test_only_json_true_counts(self, monkeypatch, client, value):
        Recorder(monkeypatch, ok([comment_json(1, 10, verified=value)]))
        assert client.get_comments("p1")[0].verified is False

    def test_add_comment_result_carries_it(self, monkeypatch, client):
        Recorder(monkeypatch, ok({"id": 9, "author": "qa", "body": "b", "created_at": 60, "verified": True}))
        assert client.add_comment("p1", "b", "", "").verified is True


class TestListVersions:
    def test_sorted_newest_first_with_iso_times(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok([
            {"version": 1, "size": 10, "created_at": 10, "url": f"{BASE}/p/p1/v/1"},
            {"version": 3, "size": 30, "created_at": 30, "url": f"{BASE}/p/p1/v/3"},
            {"version": 2, "size": 20, "created_at": 20, "url": f"{BASE}/p/p1/v/2"},
        ]))
        versions = client.list_versions("p1")
        assert [v.version for v in versions] == [3, 2, 1]
        assert versions[0] == PeekVersion(3, 30, "1970-01-01T00:00:30Z", f"{BASE}/p/p1/v/3")
        call = rec.calls[0]
        assert (call["method"], call["url"]) == ("GET", f"{BASE}/api/uploads/p1/versions")
        assert auth_of(call) == f"Bearer {TOKEN}"

    def test_non_list_answer_is_rejected(self, monkeypatch, client):
        Recorder(monkeypatch, ok({"a": 1}))
        with pytest.raises(PeekClientError, match="something unexpected"):
            client.list_versions("p1")


class TestGrants:
    def test_list_sorted_by_kind_then_email(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok([
            {"id": 1, "kind": "user", "email": "b@x.io", "created_at": 10},
            {"id": 2, "kind": "group", "email": "z@x.io", "created_at": 20},
            {"id": 3, "kind": "user", "email": "a@x.io", "created_at": 30},
        ]))
        grants = client.list_grants("p1")
        assert [(g.kind, g.email) for g in grants] == [("group", "z@x.io"), ("user", "a@x.io"), ("user", "b@x.io")]
        assert grants[0] == PeekGrant("group", "z@x.io", "1970-01-01T00:00:20Z")
        assert (rec.calls[0]["method"], rec.calls[0]["url"]) == ("GET", f"{BASE}/api/uploads/p1/grants")

    @pytest.mark.parametrize("notified", [True, False])
    def test_add_body_and_notified(self, monkeypatch, client, notified):
        rec = Recorder(monkeypatch, ok({"id": 1, "kind": "user", "email": "alice@example.com", "notified": notified}))
        assert client.add_grant("p1", "user", " Alice@Example.com ") is notified
        call = rec.calls[0]
        assert (call["method"], call["url"]) == ("POST", f"{BASE}/api/uploads/p1/grants")
        assert call["json"] == {"kind": "user", "email": "alice@example.com"}

    def test_remove_body(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok({"removed": True}))
        assert client.remove_grant("p1", "group", "Team@Example.com") is None
        call = rec.calls[0]
        assert (call["method"], call["url"]) == ("DELETE", f"{BASE}/api/uploads/p1/grants")
        assert call["json"] == {"kind": "group", "email": "team@example.com"}

    def test_remove_grant_not_found(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(404, {"error": "grant not found"}))
        with pytest.raises(PeekClientError) as exc:
            client.remove_grant("p1", "user", "a@b.co")
        assert str(exc.value) == (
            "Peek has no such grant on this page (HTTP 404). Call peek_list_grants to see who has access."
        )
        assert (exc.value.status, exc.value.detail) == (404, "grant not found")

    def test_remove_other_404_is_the_plain_404(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(404, {"error": "not found"}))
        with pytest.raises(PeekClientError, match="Peek found no such page"):
            client.remove_grant("p1", "user", "a@b.co")

    @pytest.mark.parametrize("call", [
        lambda c: c.add_grant("p1", "group", "team@example.com"),
        lambda c: c.remove_grant("p1", "group", "team@example.com"),
    ])
    def test_group_lookup_message(self, monkeypatch, client, call):
        Recorder(monkeypatch, FakeResponse(400, {"error": "group grants need Google settings"}))
        with pytest.raises(PeekClientError) as exc:
            call(client)
        assert str(exc.value) == (
            "This Peek server cannot check Google group membership, so it does not accept group grants. "
            "Grant access to people one by one, or ask the Peek admin to set up group lookup."
        )
        assert exc.value.status == 400

    @pytest.mark.parametrize("call", [
        lambda c: c.add_grant("p1", "user", "a@b.co"),
        lambda c: c.remove_grant("p1", "user", "a@b.co"),
    ])
    def test_other_errors_pass_through(self, monkeypatch, client, call):
        Recorder(monkeypatch, FakeResponse(400, {"error": "invalid email"}))
        with pytest.raises(PeekClientError, match=r"HTTP 400\): invalid email"):
            call(client)

    @pytest.mark.parametrize("call", [
        lambda c: c.list_versions("../x"),
        lambda c: c.list_grants("../x"),
        lambda c: c.add_grant("../x", "user", "a@b.co"),
        lambda c: c.remove_grant("../x", "user", "a@b.co"),
    ])
    def test_bad_slug_rejected_before_any_request(self, monkeypatch, client, call):
        rec = Recorder(monkeypatch, ok({}))
        with pytest.raises(PeekClientError, match="Not a Peek page slug"):
            call(client)
        assert rec.calls == []

    @pytest.mark.parametrize("call", [
        lambda c: c.add_grant("p1", "team", "a@b.co"),
        lambda c: c.remove_grant("p1", "user", "nobody"),
    ])
    def test_bad_grant_rejected_before_any_request(self, monkeypatch, client, call):
        rec = Recorder(monkeypatch, ok({}))
        with pytest.raises(PeekClientError):
            call(client)
        assert rec.calls == []

    def test_no_log_line_contains_an_email(self, monkeypatch, client, caplog):
        email = "secret.person@example.com"
        Recorder(monkeypatch, ok([{"kind": "user", "email": email, "created_at": 1}]))
        with caplog.at_level(logging.DEBUG, logger="privacyfence.peek_client"):
            client.list_grants("p1")
            Recorder(monkeypatch, ok({"notified": True}))
            client.add_grant("p1", "user", email)
            Recorder(monkeypatch, ok({"removed": True}))
            client.remove_grant("p1", "user", email)
        assert caplog.records
        assert "@" not in caplog.text and email not in caplog.text


class TestValidateGrant:
    def test_normalizes(self):
        assert validate_grant("user", "Alice@Example.com ") == ("user", "alice@example.com")
        assert validate_grant("group", "team@example.com") == ("group", "team@example.com")

    @pytest.mark.parametrize(
        "email", ["a@b@c", "nobody", "a b@c.d", "a,b@c.d", "<a@b.c>", "a@" + "b" * 253, ""]
    )
    def test_refused(self, email):
        with pytest.raises(PeekClientError) as exc:
            validate_grant("user", email)
        assert str(exc.value) == "Enter one email address, such as alice@example.com."

    def test_254_characters_pass(self):
        email = "a@" + "b" * 252
        assert len(email) == 254
        assert validate_grant("user", email) == ("user", email)

    def test_unknown_kind(self):
        with pytest.raises(PeekClientError) as exc:
            validate_grant("team", "a@b.co")
        assert str(exc.value) == "kind must be user or group."


class TestSlugGuards:
    @pytest.mark.parametrize(
        "call",
        [
            lambda c, s: c.get_page(s),
            lambda c, s: c.get_comments(s),
            lambda c, s: c.get_stats(s),
            lambda c, s: c.upload_page(b"x", "a.html", "", slug=s),
            lambda c, s: c.set_visibility(s, "public"),
            lambda c, s: c.delete_page(s),
            lambda c, s: c.rename_page(s, "n"),
            lambda c, s: c.add_comment(s, "b", "", ""),
        ],
    )
    def test_bad_slug_rejected_before_any_request(self, monkeypatch, client, call):
        rec = Recorder(monkeypatch, ok({}))
        with pytest.raises(PeekClientError, match="Not a Peek page slug"):
            call(client, "../x")
        assert rec.calls == []


class TestSecretsStayOutOfErrors:
    def test_no_secret_in_raised_messages(self, monkeypatch, client):
        Recorder(monkeypatch, requests.ConnectionError(f"{TOKEN} {DEVICE}"))
        with pytest.raises(PeekClientError) as exc:
            client.set_visibility("p1", "public")
        text = str(exc.value) + repr(exc.value) + repr(client)
        assert not any(secret in text for secret in (TOKEN, DEVICE))


class TestLiveFixtureParsing:
    """Replays fixtures recorded from the QA Peek server's [QATEST]-tagged seed
    page by scripts/qa_fixture_recorder.py --record peek -- real response
    shape, not hand-authored, with identity fields already redacted. Skipped
    (not failed) until each fixture exists; see tests/fixtures/live/README.md
    and docs/testing-policy.md. Re-record via that script if this ever starts
    failing after a genuine Peek API change."""

    def _load(self, name: str) -> Any:
        path = LIVE_FIXTURES_DIR / name
        if not path.exists():
            pytest.skip(
                f"{path} not recorded yet -- run "
                "`python3 scripts/qa_fixture_recorder.py --record peek` locally first"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_list_pages_fixture_still_parses(self, monkeypatch, client):
        raw = self._load("list_pages.json")
        Recorder(monkeypatch, ok(raw))

        pages = client.list_pages()

        assert pages, "recorded list_pages.json has no pages"
        assert all(p.slug and p.name and p.visibility and p.created_at and p.version for p in pages)
        assert all(p.created_at.endswith("Z") for p in pages)

    def test_get_comments_fixture_still_parses(self, monkeypatch, client):
        raw = self._load("get_comments.json")
        Recorder(monkeypatch, ok(raw))

        comments = client.get_comments("p1")

        assert comments, "recorded get_comments.json has no comments"
        assert all(c.id and c.author and c.body and c.created_at for c in comments)
        assert all("[QATEST]" in c.body for c in comments)
        assert all(isinstance(c.verified, bool) for c in comments)
