"""Tests for the upstream OAuth token module (ADR 0329, 0330).

The invariants: every request is capped and never follows a redirect, a token file is plaintext
JSON at 0600 and nothing else, a refresh happens once per stale token, and no raised message holds
a code, a verifier, a secret or a token.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from base64 import b64decode
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from privacyfence import paths, upstream_oauth_tokens as tokens
from privacyfence.principal import Principal
from privacyfence.upstream_oauth_tokens import (
    OAuthClient,
    TokenRecord,
    UpstreamOAuthError,
    UpstreamTokenProvider,
    build_authorize_url,
    delete_token,
    exchange_code,
    http_json,
    load_token,
    origin_of,
    save_token,
    token_path,
    token_paths,
)

pytestmark = pytest.mark.unit

TOKEN_URL = "https://auth.example.com/token"
CODE = "sentinel-code-9f3a"
VERIFIER = "sentinel-verifier-77c1"
SECRET = "sentinel-secret-5b2e"
ACCESS = "sentinel-access-1d4c"
REFRESH = "sentinel-refresh-8e6a"


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


def _record(**over) -> TokenRecord:
    base = dict(
        upstream_id="canva", access_token=ACCESS, refresh_token=REFRESH, expires_at=time.time() + 3600,
        scope="mcp:read", issuer="https://auth.example.com", client_id="cid", auth_method="none",
        token_endpoint=TOKEN_URL, resource="https://mcp.example.com/mcp", connected_at=1000.0,
    )
    base.update(over)
    return TokenRecord(**base)


def _mock(handler):
    return httpx2.MockTransport(handler)


def _json_response(payload, status=200):
    return httpx2.Response(status, json=payload)


# --------------------------------------------------------------------------- #
# http_json
# --------------------------------------------------------------------------- #


class TestHttpJson:
    async def test_returns_status_object_and_headers(self):
        seen = {}

        def handler(request):
            seen["request"] = request
            return httpx2.Response(201, json={"a": 1}, headers={"X-Thing": "yes"})

        status, body, headers = await http_json(
            "POST", "https://x.example.com/p", transport=_mock(handler), headers={"Accept": "application/json"},
            json_body={"k": "v"},
        )
        assert (status, body, headers["x-thing"]) == (201, {"a": 1}, "yes")
        assert json.loads(seen["request"].content) == {"k": "v"}
        assert seen["request"].headers["accept"] == "application/json"

    async def test_form_and_auth_are_sent(self):
        seen = {}

        def handler(request):
            seen["request"] = request
            return _json_response({})

        await http_json(
            "POST", "https://x.example.com/p", transport=_mock(handler), form={"a": "b"},
            auth=httpx2.BasicAuth("u", "p"),
        )
        assert parse_qs(seen["request"].content.decode()) == {"a": ["b"]}
        assert seen["request"].headers["authorization"].startswith("Basic ")

    @pytest.mark.parametrize("content", [b"[1, 2]", b"not json", b'"text"', b""])
    async def test_a_body_that_is_not_an_object_is_none(self, content):
        status, body, _ = await http_json(
            "GET", "https://x.example.com/p", transport=_mock(lambda r: httpx2.Response(200, content=content)),
        )
        assert (status, body) == (200, None)

    async def test_a_body_over_64_kib_is_refused(self):
        big = b'{"a": "' + b"x" * (64 * 1024) + b'"}'
        with pytest.raises(UpstreamOAuthError, match="^unreachable$"):
            await http_json("GET", "https://x.example.com/p", transport=_mock(lambda r: httpx2.Response(200, content=big)))

    async def test_a_body_of_exactly_64_kib_is_read(self):
        body = b'{"a": "' + b"x" * (64 * 1024 - 9) + b'"}'
        assert len(body) == 64 * 1024
        status, parsed, _ = await http_json(
            "GET", "https://x.example.com/p", transport=_mock(lambda r: httpx2.Response(200, content=body)),
        )
        assert status == 200 and parsed is not None

    async def test_a_redirect_is_not_followed(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx2.Response(302, headers={"Location": "https://evil.example.com/"})

        status, body, headers = await http_json("GET", "https://x.example.com/p", transport=_mock(handler))
        assert (status, body) == (302, None)
        assert headers["location"] == "https://evil.example.com/"
        assert calls == ["https://x.example.com/p"]

    async def test_a_transport_error_is_unreachable(self):
        def handler(request):
            raise httpx2.ConnectError("boom")

        with pytest.raises(UpstreamOAuthError, match="^unreachable$"):
            await http_json("GET", "https://x.example.com/p", transport=_mock(handler))

    async def test_a_timeout_is_unreachable(self):
        def handler(request):
            raise httpx2.ReadTimeout("slow")

        with pytest.raises(UpstreamOAuthError, match="^unreachable$"):
            await http_json("GET", "https://x.example.com/p", transport=_mock(handler))


def test_the_error_text_is_its_kind():
    err = UpstreamOAuthError("not_connected")
    assert str(err) == "not_connected" and err.kind == "not_connected"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://MCP.Example.com/path?q=1", "https://mcp.example.com"),
        ("https://mcp.example.com:8443/mcp", "https://mcp.example.com:8443"),
        ("http://localhost:9000/mcp", "http://localhost:9000"),
    ],
)
def test_origin_of(url, expected):
    assert origin_of(url) == expected


# --------------------------------------------------------------------------- #
# Token files
# --------------------------------------------------------------------------- #


class TestTokenFiles:
    def test_round_trip_at_0600_in_a_0700_directory(self, tmp_path):
        path = token_path(Principal(id="alice"), "canva")
        assert path == tmp_path / "users" / "alice" / "credentials" / "upstream_mcp" / "canva.json"
        record = _record()
        save_token(path, record)
        assert load_token(path) == record
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
        assert json.loads(path.read_text())["version"] == 1

    def test_the_record_does_not_show_a_secret_in_its_repr(self):
        text = repr(_record())
        assert ACCESS not in text and REFRESH not in text
        assert SECRET not in repr(OAuthClient("cid", SECRET, "client_secret_post"))

    def test_none_fields_round_trip(self, tmp_path):
        path = tmp_path / "t.json"
        record = _record(refresh_token=None, expires_at=None, resource=None)
        save_token(path, record)
        assert load_token(path) == record

    def test_absent_file_is_none(self, tmp_path):
        assert load_token(tmp_path / "nope.json") is None

    @pytest.mark.parametrize(
        "content",
        ["{not json", "[1]", json.dumps({"version": 2}), json.dumps({"upstream_id": "canva"})],
    )
    def test_malformed_or_wrong_version_is_none(self, tmp_path, content):
        path = tmp_path / "t.json"
        path.write_text(content)
        assert load_token(path) is None

    @pytest.mark.parametrize(
        "field, value",
        [
            ("access_token", 5),
            ("refresh_token", 5),
            ("expires_at", "soon"),
            ("expires_at", True),
            ("connected_at", None),
            ("scope", None),
        ],
    )
    def test_a_field_of_the_wrong_type_is_none(self, tmp_path, field, value):
        path = tmp_path / "t.json"
        save_token(path, _record())
        data = json.loads(path.read_text())
        data[field] = value
        path.write_text(json.dumps(data))
        assert load_token(path) is None

    def test_delete_token_is_idempotent(self, tmp_path):
        path = tmp_path / "t.json"
        save_token(path, _record())
        delete_token(path)
        delete_token(path)
        assert not path.exists()

    def test_token_paths_newest_first_and_only_this_upstream(self, tmp_path):
        old = token_path(Principal(id="alice"), "canva")
        new = token_path(Principal(id="bob"), "canva")
        other = token_path(Principal(id="alice"), "framer")
        for path in (old, new, other):
            save_token(path, _record())
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        assert token_paths("canva") == [new, old]
        assert token_paths("framer") == [other]
        assert token_paths("nothing") == []


# --------------------------------------------------------------------------- #
# Authorize URL
# --------------------------------------------------------------------------- #


class TestAuthorizeUrl:
    def test_every_parameter(self):
        url = build_authorize_url(
            "https://auth.example.com/authorize", "cid", "https://pf.example.com/cb", "st", "chal",
            "a b", "https://mcp.example.com/mcp",
        )
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://auth.example.com/authorize"
        assert {k: v[0] for k, v in parse_qs(parts.query).items()} == {
            "response_type": "code", "client_id": "cid", "redirect_uri": "https://pf.example.com/cb",
            "state": "st", "code_challenge": "chal", "code_challenge_method": "S256", "scope": "a b",
            "resource": "https://mcp.example.com/mcp",
        }

    def test_scope_and_resource_are_omitted_when_none(self):
        query = parse_qs(urlsplit(build_authorize_url("https://a.example.com/auth", "c", "r", "s", "x", None, None)).query)
        assert "scope" not in query and "resource" not in query

    def test_an_endpoint_with_a_query_is_extended(self):
        url = build_authorize_url("https://a.example.com/auth?tenant=1", "c", "r", "s", "x", None, None)
        assert url.startswith("https://a.example.com/auth?tenant=1&response_type=code")


# --------------------------------------------------------------------------- #
# exchange_code
# --------------------------------------------------------------------------- #


async def _exchange(handler, client, **over):
    args = dict(
        token_endpoint=TOKEN_URL, client=client, code=CODE, redirect_uri="https://pf.example.com/cb",
        code_verifier=VERIFIER, resource="https://mcp.example.com/mcp", upstream_id="canva",
        issuer="https://auth.example.com", transport=_mock(handler),
    )
    args.update(over)
    return await exchange_code(**args)


class TestExchangeCode:
    async def test_public_client(self):
        seen = {}

        def handler(request):
            seen["request"] = request
            return _json_response({"access_token": ACCESS, "token_type": "bearer", "expires_in": 100, "refresh_token": REFRESH, "scope": "s"})

        before = time.time()
        record = await _exchange(handler, OAuthClient("cid", None, "none"))
        form = {k: v[0] for k, v in parse_qs(seen["request"].content.decode()).items()}
        assert form == {
            "grant_type": "authorization_code", "code": CODE, "redirect_uri": "https://pf.example.com/cb",
            "client_id": "cid", "code_verifier": VERIFIER, "resource": "https://mcp.example.com/mcp",
        }
        assert "authorization" not in seen["request"].headers
        assert (record.upstream_id, record.access_token, record.refresh_token, record.scope) == ("canva", ACCESS, REFRESH, "s")
        assert (record.issuer, record.client_id, record.auth_method) == ("https://auth.example.com", "cid", "none")
        assert (record.token_endpoint, record.resource) == (TOKEN_URL, "https://mcp.example.com/mcp")
        assert record.expires_at is not None and before + 99 <= record.expires_at <= time.time() + 101
        assert before <= record.connected_at <= time.time()

    async def test_client_secret_post(self):
        seen = {}

        def handler(request):
            seen["request"] = request
            return _json_response({"access_token": ACCESS, "token_type": "Bearer"})

        await _exchange(handler, OAuthClient("cid", SECRET, "client_secret_post"), resource=None)
        form = parse_qs(seen["request"].content.decode())
        assert form["client_secret"] == [SECRET] and "resource" not in form
        assert "authorization" not in seen["request"].headers

    async def test_client_secret_basic_is_percent_encoded(self):
        seen = {}

        def handler(request):
            seen["request"] = request
            return _json_response({"access_token": ACCESS, "token_type": "Bearer"})

        await _exchange(handler, OAuthClient("c id", "s:ec/ret", "client_secret_basic"))
        header = seen["request"].headers["authorization"]
        assert b64decode(header.removeprefix("Basic ")).decode() == "c%20id:s%3Aec%2Fret"
        assert "client_secret" not in parse_qs(seen["request"].content.decode())

    async def test_no_expires_in_means_no_expiry(self):
        record = await _exchange(
            lambda r: _json_response({"access_token": ACCESS, "token_type": "Bearer"}), OAuthClient("cid"),
        )
        assert record.expires_at is None and record.refresh_token is None and record.scope == ""

    @pytest.mark.parametrize(
        "payload",
        [{"token_type": "Bearer"}, {"access_token": "", "token_type": "Bearer"}, {"access_token": ACCESS, "token_type": "mac"}, None],
    )
    async def test_an_unusable_token_answer_is_unreachable(self, payload):
        with pytest.raises(UpstreamOAuthError, match="^unreachable$"):
            await _exchange(lambda r: httpx2.Response(200, content=b"nope") if payload is None else _json_response(payload), OAuthClient("cid"))

    @pytest.mark.parametrize("status", [400, 401])
    async def test_a_rejection_is_auth_expired(self, status):
        with pytest.raises(UpstreamOAuthError, match="^auth_expired$"):
            await _exchange(lambda r: _json_response({"error": "invalid_grant"}, status), OAuthClient("cid"))

    async def test_another_failure_is_unreachable(self):
        with pytest.raises(UpstreamOAuthError, match="^unreachable$"):
            await _exchange(lambda r: _json_response({"error": "oops"}, 500), OAuthClient("cid"))

    @pytest.mark.parametrize("status", [200, 400, 500])
    async def test_no_error_text_holds_a_secret(self, status):
        client = OAuthClient("cid", SECRET, "client_secret_post")
        payload = {"error": f"{CODE} {VERIFIER} {SECRET}", "access_token": 7}
        with pytest.raises(UpstreamOAuthError) as caught:
            await _exchange(lambda r: _json_response(payload, status), client)
        text = f"{caught.value!s} {caught.value!r}"
        for sentinel in (CODE, VERIFIER, SECRET):
            assert sentinel not in text


# --------------------------------------------------------------------------- #
# UpstreamTokenProvider
# --------------------------------------------------------------------------- #

CLIENT = OAuthClient("cid", None, "none")


class _Refresher:
    def __init__(self, responses=None):
        self.requests: list[httpx2.Request] = []
        self.responses = list(responses or [])

    def __call__(self, request):
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        return _json_response({"access_token": "new-access", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "new-refresh"})

    def form(self, index=0):
        return {k: v[0] for k, v in parse_qs(self.requests[index].content.decode()).items()}


def _provider(refresher, client=CLIENT, **kw):
    kw.setdefault("transport", _mock(refresher))
    return UpstreamTokenProvider(lambda upstream_id: client, **kw)


def _saved(tmp_path, **over) -> Path:
    path = tmp_path / "canva.json"
    save_token(path, _record(**over))
    return path


class TestProviderFresh:
    async def test_a_fresh_token_needs_no_request(self, tmp_path):
        refresher = _Refresher()
        assert await _provider(refresher).bearer(_saved(tmp_path)) == ACCESS
        assert refresher.requests == []

    async def test_a_token_without_expiry_never_refreshes(self, tmp_path):
        refresher = _Refresher()
        assert await _provider(refresher).bearer(_saved(tmp_path, expires_at=None)) == ACCESS
        assert refresher.requests == []

    async def test_an_absent_file_is_not_connected(self, tmp_path):
        with pytest.raises(UpstreamOAuthError, match="^not_connected$"):
            await _provider(_Refresher()).bearer(tmp_path / "nope.json")

    async def test_a_file_that_vanishes_while_waiting_is_not_connected(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 5)
        provider = _provider(_Refresher())
        lock = provider._locks.setdefault(str(path), asyncio.Lock())
        async with lock:
            waiter = asyncio.create_task(provider.bearer(path))
            await asyncio.sleep(0)
            delete_token(path)
        with pytest.raises(UpstreamOAuthError, match="^not_connected$"):
            await waiter


class TestProviderRefresh:
    async def test_a_token_about_to_expire_is_refreshed_and_saved(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 30)
        refresher = _Refresher()
        assert await _provider(refresher).bearer(path) == "new-access"
        assert refresher.form() == {
            "grant_type": "refresh_token", "refresh_token": REFRESH, "client_id": "cid",
            "resource": "https://mcp.example.com/mcp",
        }
        assert str(refresher.requests[0].url) == TOKEN_URL
        saved = load_token(path)
        assert saved is not None
        assert (saved.access_token, saved.refresh_token) == ("new-access", "new-refresh")
        assert (saved.issuer, saved.client_id, saved.token_endpoint, saved.connected_at) == (
            "https://auth.example.com", "cid", TOKEN_URL, 1000.0,
        )
        assert saved.expires_at is not None and saved.expires_at > time.time() + 3000
        assert saved.scope == "mcp:read"

    async def test_the_injected_clock_decides_staleness(self, tmp_path):
        path = _saved(tmp_path, expires_at=5000.0)
        refresher = _Refresher()
        provider = _provider(refresher, clock=lambda: 4000.0)
        assert await provider.bearer(path) == ACCESS
        provider = _provider(refresher, clock=lambda: 4950.0)
        assert await provider.bearer(path) == "new-access"

    async def test_an_absent_refresh_token_keeps_the_old_one(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 1, resource=None)
        refresher = _Refresher([_json_response({"access_token": "n", "token_type": "Bearer", "scope": "wide"})])
        await _provider(refresher).bearer(path)
        assert "resource" not in refresher.form()
        saved = load_token(path)
        assert saved is not None and saved.refresh_token == REFRESH and saved.expires_at is None
        assert saved.scope == "wide"

    async def test_a_client_secret_is_sent_by_its_method(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 1, auth_method="client_secret_post")
        refresher = _Refresher()
        await _provider(refresher, OAuthClient("cid", SECRET, "client_secret_post")).bearer(path)
        assert refresher.form()["client_secret"] == SECRET

    async def test_two_concurrent_calls_refresh_once(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 1)
        refresher = _Refresher()
        provider = _provider(refresher)
        results = await self._both_wait_for_the_lock(provider, path, force=False)
        assert results == ["new-access", "new-access"]
        assert len(refresher.requests) == 1

    async def test_force_refresh_refreshes_a_fresh_token(self, tmp_path):
        path = _saved(tmp_path)
        refresher = _Refresher()
        assert await _provider(refresher).bearer(path, force_refresh=True) == "new-access"
        assert len(refresher.requests) == 1

    async def test_two_forced_calls_on_one_token_refresh_once(self, tmp_path):
        path = _saved(tmp_path)
        refresher = _Refresher()
        provider = _provider(refresher)
        results = await self._both_wait_for_the_lock(provider, path, force=True)
        assert results == ["new-access", "new-access"]
        assert len(refresher.requests) == 1

    @staticmethod
    async def _both_wait_for_the_lock(provider, path, *, force):
        # Both calls read the old token, then queue on the lock; the first one in refreshes.
        lock = provider._locks.setdefault(str(path), asyncio.Lock())
        async with lock:
            tasks = [asyncio.create_task(provider.bearer(path, force)) for _ in range(2)]
            await asyncio.sleep(0)
        return await asyncio.gather(*tasks)

    @pytest.mark.parametrize("status", [400, 401])
    async def test_a_rejection_deletes_the_file(self, tmp_path, status):
        path = _saved(tmp_path, expires_at=time.time() + 1)
        invalid = []
        provider = _provider(_Refresher([_json_response({"error": "invalid_grant"}, status)]), on_invalid_client=invalid.append)
        with pytest.raises(UpstreamOAuthError, match="^auth_expired$"):
            await provider.bearer(path)
        assert not path.exists() and invalid == []

    async def test_invalid_client_is_reported(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 1)
        invalid = []
        provider = _provider(_Refresher([_json_response({"error": "invalid_client"}, 401)]), on_invalid_client=invalid.append)
        with pytest.raises(UpstreamOAuthError, match="^auth_expired$"):
            await provider.bearer(path)
        assert not path.exists() and invalid == ["canva"]

    async def test_invalid_client_without_a_callback_still_expires(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 1)
        provider = _provider(_Refresher([_json_response({"error": "invalid_client"}, 400)]))
        with pytest.raises(UpstreamOAuthError, match="^auth_expired$"):
            await provider.bearer(path)
        assert not path.exists()

    async def test_a_rejection_without_a_json_body_expires(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 1)
        invalid = []
        provider = _provider(_Refresher([httpx2.Response(400, content=b"bad")]), on_invalid_client=invalid.append)
        with pytest.raises(UpstreamOAuthError, match="^auth_expired$"):
            await provider.bearer(path)
        assert not path.exists() and invalid == []

    async def test_a_server_error_keeps_the_file(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 1)
        provider = _provider(_Refresher([_json_response({"error": "x"}, 500)]))
        with pytest.raises(UpstreamOAuthError, match="^unreachable$"):
            await provider.bearer(path)
        assert load_token(path) is not None

    async def test_a_transport_error_keeps_the_file(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 1)

        def handler(request):
            raise httpx2.ConnectError("down")

        with pytest.raises(UpstreamOAuthError, match="^unreachable$"):
            await _provider(handler).bearer(path)
        assert load_token(path) is not None

    async def test_another_client_id_deletes_the_file(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 1)
        refresher = _Refresher()
        with pytest.raises(UpstreamOAuthError, match="^auth_expired$"):
            await _provider(refresher, OAuthClient("other")).bearer(path)
        assert not path.exists() and refresher.requests == []

    async def test_a_stale_token_without_a_refresh_token_expires(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 1, refresh_token=None)
        refresher = _Refresher()
        with pytest.raises(UpstreamOAuthError, match="^auth_expired$"):
            await _provider(refresher).bearer(path)
        assert not path.exists() and refresher.requests == []

    async def test_a_client_that_is_not_configured_propagates(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 1)

        def source(upstream_id):
            raise UpstreamOAuthError("not_configured")

        provider = UpstreamTokenProvider(source, transport=_mock(_Refresher()))
        with pytest.raises(UpstreamOAuthError, match="^not_configured$"):
            await provider.bearer(path)
        assert load_token(path) is not None

    async def test_no_error_text_holds_a_token_or_secret(self, tmp_path):
        path = _saved(tmp_path, expires_at=time.time() + 1)
        provider = _provider(_Refresher([_json_response({"error": f"{ACCESS} {REFRESH} {SECRET}"}, 500)]))
        with pytest.raises(UpstreamOAuthError) as caught:
            await provider.bearer(path)
        text = f"{caught.value!s} {caught.value!r}"
        assert not any(s in text for s in (ACCESS, REFRESH, SECRET))


def test_the_sdk_names_this_module_uses_exist():
    from mcp.shared.auth import OAuthToken
    from mcp.shared.auth_utils import calculate_token_expiry

    assert OAuthToken is tokens.OAuthToken
    assert calculate_token_expiry(10) is not None
    assert OAuthToken(access_token="a", token_type="bearer").token_type == "Bearer"
