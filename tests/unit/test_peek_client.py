"""Tests for the Peek client and device sign-in.

The invariant: a Peek token is only ever sent to the server it was issued
for. The credential file records that server, ``resolve_server`` refuses a
mismatch with an organization pin, every request goes to the recorded
server, and redirects are never followed. Requests are faked at the
``requests.Session.request`` boundary; nothing touches the network.
"""
from __future__ import annotations

import json
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
    bundle_base_url,
    load_token_file,
    normalize_base_url,
    poll_device_login,
    resolve_server,
    save_token_file,
    start_device_login,
    validate_slug,
)

pytestmark = pytest.mark.unit

BASE = "https://peek.example.com"
TOKEN = "tok-SECRET-123"
DEVICE = "dev-SECRET-456"
PASSWORD = "pw-SECRET-789"

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

    def test_404_renamed(self, monkeypatch, client):
        err = self._error(monkeypatch, client, FakeResponse(404, {"error": "page was renamed; use slug new-1"}))
        assert str(err) == "Peek says this page was renamed; its slug is now new-1. Use that slug."
        assert err.status == 404

    def test_404_renamed_with_malformed_slug_is_the_plain_404(self, monkeypatch, client):
        err = self._error(monkeypatch, client, FakeResponse(404, {"error": "page was renamed; use slug ../x"}))
        assert str(err).startswith("Peek found no such page (HTTP 404).")

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


class TestUploadPage:
    result = {"slug": "n1", "url": f"{BASE}/p/n1", "visibility": "private", "version": 1, "version_url": f"{BASE}/p/n1/v/1"}

    def test_new_page(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok(self.result))
        res = client.upload_page(b"<p>hi</p>", "r.html", "private", "")
        call = rec.calls[0]
        assert (call["method"], call["url"]) == ("POST", f"{BASE}/api/upload")
        assert call["files"] == {"file": ("r.html", b"<p>hi</p>", "text/html")}
        assert call["data"] == {"visibility": "private"}
        assert auth_of(call) == f"Bearer {TOKEN}"
        assert (res.slug, res.url, res.visibility, res.version, res.version_url) == (
            "n1", f"{BASE}/p/n1", "private", 1, f"{BASE}/p/n1/v/1",
        )

    def test_password_only_for_password_visibility(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok(self.result))
        client.upload_page(b"x", "r.html", "password", PASSWORD)
        assert rec.calls[0]["data"] == {"visibility": "password", "password": PASSWORD}

    def test_new_version_sends_only_slug(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok(self.result))
        client.upload_page(b"x", "r.html", "", "", slug="p1")
        assert rec.calls[0]["data"] == {"slug": "p1"}

    @pytest.mark.parametrize("visibility, password", [("public", ""), ("", PASSWORD)])
    def test_new_version_rejects_visibility_or_password(self, monkeypatch, client, visibility, password):
        rec = Recorder(monkeypatch, ok(self.result))
        with pytest.raises(PeekClientError) as exc:
            client.upload_page(b"x", "r.html", visibility, password, slug="p1")
        assert str(exc.value) == "A new version keeps the page's visibility; change it with set_visibility."
        assert PASSWORD not in str(exc.value)
        assert rec.calls == []

    @pytest.mark.parametrize("visibility", ["restricted", "", "other"])
    def test_bad_visibility(self, monkeypatch, client, visibility):
        rec = Recorder(monkeypatch, ok(self.result))
        with pytest.raises(PeekClientError, match="visibility must be public, password or private."):
            client.upload_page(b"x", "r.html", visibility, "")
        assert rec.calls == []

    @pytest.mark.parametrize(
        "visibility, password",
        [("password", ""), ("public", PASSWORD), ("password", "x" * 73), ("password", "é" * 37)],
    )
    def test_password_rules(self, monkeypatch, client, visibility, password):
        rec = Recorder(monkeypatch, ok(self.result))
        with pytest.raises(PeekClientError) as exc:
            client.upload_page(b"x", "r.html", visibility, password)
        assert str(exc.value) == "A password of 1 to 72 bytes goes with password visibility, and only with it."
        assert rec.calls == []

    @pytest.mark.parametrize("password", [" abc", "abc ", "   "])
    def test_password_spaces(self, monkeypatch, client, password):
        rec = Recorder(monkeypatch, ok(self.result))
        with pytest.raises(PeekClientError) as exc:
            client.upload_page(b"x", "r.html", "password", password)
        assert str(exc.value) == "A Peek password cannot start or end with a space."
        assert rec.calls == []

    def test_72_bytes_is_fine(self, monkeypatch, client):
        Recorder(monkeypatch, ok(self.result))
        client.upload_page(b"x", "r.html", "password", "x" * 72)


class TestSetVisibility:
    def test_request(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok({"visibility": "password"}))
        assert client.set_visibility("p1", "password", PASSWORD) == "password"
        call = rec.calls[0]
        assert (call["method"], call["url"]) == ("POST", f"{BASE}/api/uploads/p1/visibility")
        assert call["json"] == {"visibility": "password", "password": PASSWORD}

    def test_checks_before_any_request(self, monkeypatch, client):
        rec = Recorder(monkeypatch, ok({}))
        with pytest.raises(PeekClientError):
            client.set_visibility("p1", "restricted", "")
        with pytest.raises(PeekClientError):
            client.set_visibility("p1", "public", PASSWORD)
        assert rec.calls == []

    def test_missing_answer_key(self, monkeypatch, client):
        Recorder(monkeypatch, ok({}))
        assert client.set_visibility("p1", "public", "") == ""


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


class TestSlugGuards:
    @pytest.mark.parametrize(
        "call",
        [
            lambda c, s: c.get_page(s),
            lambda c, s: c.get_comments(s),
            lambda c, s: c.get_stats(s),
            lambda c, s: c.upload_page(b"x", "a.html", "", "", slug=s),
            lambda c, s: c.set_visibility(s, "public", ""),
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
        Recorder(monkeypatch, requests.ConnectionError(f"{TOKEN} {DEVICE} {PASSWORD}"))
        with pytest.raises(PeekClientError) as exc:
            client.set_visibility("p1", "password", PASSWORD)
        text = str(exc.value) + repr(exc.value) + repr(client)
        assert not any(secret in text for secret in (TOKEN, DEVICE, PASSWORD))


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
