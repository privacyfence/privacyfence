"""plugins/pages.py: path normalization, query parsing, the response filter and one page request."""
from __future__ import annotations

import base64

import pytest

from privacyfence.plugins import pages
from privacyfence.plugins.constants import MAX_PAGE_BODY_BYTES, MAX_PAGE_PATH_CHARS
from privacyfence.plugins.protocol import RpcError
from privacyfence.principal import LOCAL_PRINCIPAL

pytestmark = pytest.mark.unit


class TestNormalizePath:
    @pytest.mark.parametrize(("raw", "expected"), [
        ("/", "/"),
        ("", "/"),
        ("/index.html", "/index.html"),
        ("notes/today", "/notes/today"),
        ("/a%20b", "/a b"),
        ("/%2e/x", "/./x"),
        ("/a..b/c", "/a..b/c"),
        ("/dir/", "/dir/"),
        ("/%252e%252e/x", "/%2e%2e/x"),  # decoded once, not twice
        ("/" + "a" * (MAX_PAGE_PATH_CHARS - 1), "/" + "a" * (MAX_PAGE_PATH_CHARS - 1)),
    ])
    def test_accepted(self, raw, expected):
        assert pages.normalize_path(raw) == expected

    @pytest.mark.parametrize("raw", [
        "/..",
        "/../etc/passwd",
        "/a/../b",
        "/%2e%2e/x",
        "/a/%2E%2E",
        "..",
        "/a\x00b",
        "/a%00b",
        "/a\\b",
        "/a%5cb",
        "//x",
        "/a//b",
        "/a%2f%2fb",
        "/" + "a" * MAX_PAGE_PATH_CHARS,
        "/" + "%61" * MAX_PAGE_PATH_CHARS,
    ])
    def test_rejected(self, raw):
        assert pages.normalize_path(raw) is None


class TestParseQuery:
    def test_last_value_wins_and_blanks_are_kept(self):
        assert pages.parse_query("a=1&b=&a=2&c=x%20y") == {"a": "2", "b": "", "c": "x y"}

    def test_empty(self):
        assert pages.parse_query("") == {}


def _ok(**overrides):
    result = {"status": 200, "headers": {"content-type": "text/html"}, "body": "<p>hi</p>", "body_encoding": "utf8"}
    result.update(overrides)
    return result


class TestFilterResponse:
    @pytest.mark.parametrize("status", [200, 204, 400, 404, 500])
    def test_allowed_statuses_pass(self, status):
        assert pages.filter_response(_ok(status=status))[0] == status

    def test_204_is_sent_without_a_body(self):
        status, _, body = pages.filter_response(_ok(status=204, body="not allowed"))
        assert (status, body) == (204, b"")

    @pytest.mark.parametrize("status", [201, 301, 302, 401, 403, 418, 502, 503, True, "200", None])
    def test_other_statuses_become_502(self, status):
        code, headers, body = pages.filter_response(_ok(status=status))
        assert code == 502
        assert headers["content-type"] == "text/plain; charset=utf-8"
        assert body == pages.BAD_RESPONSE

    @pytest.mark.parametrize(("given", "served"), [
        ("text/html", "text/html"),
        ("text/html; charset=utf-8", "text/html; charset=utf-8"),
        ("TEXT/HTML;CHARSET=UTF-8", "text/html; charset=utf-8"),
        ("text/plain", "text/plain"),
        ("text/css", "text/css"),
        ("application/javascript", "application/javascript"),
        ("application/json; charset=utf-8", "application/json; charset=utf-8"),
        ("image/png", "image/png"),
        ("image/svg+xml", "image/svg+xml"),
        ("image/jpeg", "image/jpeg"),
        ("text/html; charset=iso-8859-1", "application/octet-stream"),
        ("application/xhtml+xml", "application/octet-stream"),
        ("text/xml", "application/octet-stream"),
        ("multipart/x-mixed-replace", "application/octet-stream"),
        (42, "application/octet-stream"),
    ])
    def test_content_type_allowlist(self, given, served):
        _, headers, _ = pages.filter_response(_ok(headers={"Content-Type": given}))
        assert headers["content-type"] == served

    def test_missing_content_type_is_octet_stream(self):
        _, headers, _ = pages.filter_response(_ok(headers={}))
        assert headers["content-type"] == "application/octet-stream"

    def test_non_dict_headers_are_ignored(self):
        _, headers, _ = pages.filter_response(_ok(headers=["content-type", "text/html"]))
        assert headers["content-type"] == "application/octet-stream"

    def test_plugin_cookie_csp_and_cache_control_are_dropped(self):
        _, headers, _ = pages.filter_response(_ok(headers={
            "content-type": "text/html",
            "set-cookie": "pf_session=stolen",
            "Content-Security-Policy": "default-src *",
            "cache-control": "public, max-age=31536000",
            "access-control-allow-origin": "*",
            "x-frame-options": "ALLOWALL",
            "location": "https://evil.example",
        }))
        assert headers == {"cache-control": "private, no-store", "content-type": "text/html"}

    def test_base64_body_is_decoded(self):
        data = b"\x89PNG\r\n\x1a\n\x00\xff"
        _, _, body = pages.filter_response(_ok(body=base64.b64encode(data).decode(), body_encoding="base64"))
        assert body == data

    def test_utf8_is_the_default_encoding(self):
        result = _ok(body="é")
        del result["body_encoding"]
        assert pages.filter_response(result)[2] == "é".encode()

    @pytest.mark.parametrize("result", [
        _ok(body="not base64!", body_encoding="base64"),
        _ok(body_encoding="latin-1"),
        _ok(body=b"bytes"),
        _ok(body=None),
        _ok(body="\ud800"),
        {"headers": {}},
        "not a dict",
        None,
    ])
    def test_invalid_results_become_502(self, result):
        assert pages.filter_response(result) == (
            502, {"cache-control": "private, no-store", "content-type": "text/plain; charset=utf-8"},
            pages.BAD_RESPONSE,
        )

    def test_body_at_the_cap_is_served(self):
        status, _, body = pages.filter_response(_ok(body="a" * MAX_PAGE_BODY_BYTES))
        assert status == 200
        assert len(body) == MAX_PAGE_BODY_BYTES

    def test_oversize_body_becomes_502(self):
        status, _, body = pages.filter_response(_ok(body="a" * (MAX_PAGE_BODY_BYTES + 1)))
        assert status == 502
        assert body == pages.BAD_RESPONSE

    def test_oversize_base64_body_becomes_502(self):
        encoded = base64.b64encode(b"\x00" * (MAX_PAGE_BODY_BYTES + 1)).decode()
        assert pages.filter_response(_ok(body=encoded, body_encoding="base64"))[0] == 502


class _Host:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result if result is not None else _ok()
        self.error = error
        self.calls: list[tuple] = []

    async def web_request(self, name, path, query, principal):
        self.calls.append((name, path, query, principal))
        if self.error is not None:
            raise self.error
        return self.result


class TestRenderPluginPage:
    async def test_forwards_the_normalized_path_query_and_principal(self):
        host = _Host()
        status, headers, body = await pages.render_plugin_page(
            host, "today", "/notes%20x", {"q": "1"}, LOCAL_PRINCIPAL,
        )
        assert (status, body) == (200, b"<p>hi</p>")
        assert headers["content-type"] == "text/html"
        assert host.calls == [("today", "/notes x", {"q": "1"}, LOCAL_PRINCIPAL)]

    async def test_rejected_path_is_400_without_calling_the_plugin(self):
        host = _Host()
        status, headers, body = await pages.render_plugin_page(host, "today", "/%2e%2e/x", {}, LOCAL_PRINCIPAL)
        assert (status, body) == (400, pages.BAD_PATH)
        assert headers["cache-control"] == "private, no-store"
        assert host.calls == []

    async def test_not_serving_is_404(self):
        host = _Host(error=LookupError("Plugin today is not serving pages."))
        status, _, body = await pages.render_plugin_page(host, "today", "/", {}, LOCAL_PRINCIPAL)
        assert (status, body) == (404, pages.NOT_FOUND)

    @pytest.mark.parametrize("code", ["timeout", "internal_error", "invalid_params"])
    async def test_rpc_error_is_502(self, code):
        host = _Host(error=RpcError(code, "x"))
        status, headers, body = await pages.render_plugin_page(host, "today", "/", {}, LOCAL_PRINCIPAL)
        assert (status, body) == (502, b"The plugin did not answer.")
        assert headers["content-type"] == "text/plain; charset=utf-8"

    async def test_plugin_answer_is_filtered(self):
        host = _Host(result=_ok(status=302, headers={"location": "https://evil.example"}))
        status, headers, _ = await pages.render_plugin_page(host, "today", "/", {}, LOCAL_PRINCIPAL)
        assert status == 502
        assert "location" not in headers


class TestCsp:
    def test_exact_string(self):
        assert pages.CSP == (
            "sandbox allow-scripts; default-src 'self' data: 'unsafe-inline'; "
            "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        assert "allow-same-origin" not in pages.CSP
