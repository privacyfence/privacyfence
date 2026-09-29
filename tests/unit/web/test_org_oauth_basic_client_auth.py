"""``client_secret_basic`` clients that send their client ID only in the
``Authorization`` header, as RFC 6749 section 2.3.1 allows (Gemini
Enterprise does, with "Use HTTP Basic Authentication" ticked). The SDK's
own ClientAuthenticator reads the ID from the form body only;
web/routes_mcp.py's ``_BasicAuthClientId`` fills it in from the header.
"""
from __future__ import annotations

import base64

import httpx
import pytest
from starlette.applications import Starlette

from privacyfence.web.oauth_provider import OrgOAuthProvider
from privacyfence.web.routes_mcp import _BasicAuthClientId, _client_id_from_basic, mount_org_oauth
from tests.unit.web.conftest import ISSUER, authorize_and_get_code, isolate_org_oauth_stores, org_idp, register_client

GEMINI_REDIRECT_URI = "https://vertexaisearch.cloud.google.com/oauth-redirect"


def _basic(client_id: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()


def _app(tmp_path, monkeypatch) -> Starlette:
    isolate_org_oauth_stores(tmp_path, monkeypatch)
    provider = OrgOAuthProvider(org_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback")
    return Starlette(routes=mount_org_oauth(provider, issuer_url=ISSUER))


async def _registered_basic_client(client: httpx.AsyncClient) -> dict:
    return await register_client(client, {
        "client_name": "Gemini Enterprise", "redirect_uris": [GEMINI_REDIRECT_URI],
        "token_endpoint_auth_method": "client_secret_basic",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    })


async def _code(client, reg, monkeypatch) -> tuple[str, str]:
    return await authorize_and_get_code(
        client, client_id=reg["client_id"], monkeypatch=monkeypatch, claims={"sub": "alice"},
        redirect_uri=GEMINI_REDIRECT_URI,
    )


async def test_token_exchange_with_the_client_id_only_in_the_basic_header(tmp_path, monkeypatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path, monkeypatch)), base_url=ISSUER) as c:
        reg = await _registered_basic_client(c)
        code, verifier = await _code(c, reg, monkeypatch)
        r = await c.post("/token", headers={"Authorization": _basic(reg["client_id"], reg["client_secret"])}, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": GEMINI_REDIRECT_URI,
            "code_verifier": verifier,
        })
        assert r.status_code == 200, r.text
        tokens = r.json()
        assert tokens["access_token"] and tokens["refresh_token"]

        refreshed = await c.post("/token", headers={"Authorization": _basic(reg["client_id"], reg["client_secret"])},
                                 data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
        assert refreshed.status_code == 200, refreshed.text


async def test_the_secret_is_still_checked(tmp_path, monkeypatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path, monkeypatch)), base_url=ISSUER) as c:
        reg = await _registered_basic_client(c)
        code, verifier = await _code(c, reg, monkeypatch)
        r = await c.post("/token", headers={"Authorization": _basic(reg["client_id"], "wrong")}, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": GEMINI_REDIRECT_URI,
            "code_verifier": verifier,
        })
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_client"
        assert "Missing client_id" not in r.text


async def test_a_body_client_id_is_left_alone_and_must_match_the_header(tmp_path, monkeypatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path, monkeypatch)), base_url=ISSUER) as c:
        reg = await _registered_basic_client(c)
        other = await _registered_basic_client(c)
        code, verifier = await _code(c, reg, monkeypatch)
        r = await c.post("/token", headers={"Authorization": _basic(reg["client_id"], reg["client_secret"])}, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": GEMINI_REDIRECT_URI,
            "code_verifier": verifier, "client_id": other["client_id"],
        })
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_client"


async def test_revoke_with_the_client_id_only_in_the_basic_header(tmp_path, monkeypatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(tmp_path, monkeypatch)), base_url=ISSUER) as c:
        reg = await _registered_basic_client(c)
        code, verifier = await _code(c, reg, monkeypatch)
        auth = {"Authorization": _basic(reg["client_id"], reg["client_secret"])}
        tokens = (await c.post("/token", headers=auth, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": GEMINI_REDIRECT_URI,
            "code_verifier": verifier,
        })).json()
        r = await c.post("/revoke", headers=auth, data={"token": tokens["refresh_token"]})
        assert r.status_code == 200, r.text
        # Revoked: the refresh token no longer redeems.
        again = await c.post("/token", headers=auth, data={"grant_type": "refresh_token",
                                                           "refresh_token": tokens["refresh_token"]})
        assert again.status_code == 400


class TestClientIdFromBasic:
    @pytest.mark.parametrize("value, expected", [
        (_basic("abc", "s3cr3t"), "abc"),
        (_basic("a%3Ab", "s:3"), "a:b"),  # URL-decoded, and the secret may contain colons
        ("basic " + base64.b64encode(b"abc:x").decode(), "abc"),  # the scheme is case-insensitive
        ("Bearer abc", None),
        ("Basic !!!not-base64!!!", None),
        ("Basic " + base64.b64encode(b"no-colon").decode(), None),
        ("Basic " + base64.b64encode(b":secret-only").decode(), None),
    ])
    def test_parses(self, value, expected):
        assert _client_id_from_basic([(b"authorization", value.encode())]) == expected

    def test_no_authorization_header(self):
        assert _client_id_from_basic([(b"content-type", b"application/x-www-form-urlencoded")]) is None


class TestBasicAuthClientIdMiddleware:
    """The middleware on its own, against a stub app that records what it received."""

    @staticmethod
    async def _run(body_chunks: list[bytes], headers: list[tuple[bytes, bytes]], method: str = "POST",
                   path: str = "/token"):
        seen: dict = {}

        async def app(scope, receive, send):
            body = b""
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body", False):
                    break
            seen["body"] = body
            seen["headers"] = dict(scope["headers"])

        messages = [
            {"type": "http.request", "body": chunk, "more_body": i < len(body_chunks) - 1}
            for i, chunk in enumerate(body_chunks)
        ]

        async def receive():
            return messages.pop(0)

        scope = {"type": "http", "method": method, "path": path, "headers": headers}
        await _BasicAuthClientId(app)(scope, receive, None)
        return seen

    async def test_adds_the_client_id_and_fixes_content_length(self):
        body = b"grant_type=authorization_code&code=abc"
        seen = await self._run([body[:10], body[10:]], [
            (b"authorization", _basic("id with space", "s").encode()), (b"content-length", str(len(body)).encode()),
        ])
        assert seen["body"] == body + b"&client_id=id%20with%20space"
        assert seen["headers"][b"content-length"] == str(len(seen["body"])).encode()

    async def test_on_revoke_also_adds_the_unused_client_secret_field(self):
        seen = await self._run([b"token=abc"], [(b"authorization", _basic("id", "s").encode())], path="/revoke")
        assert seen["body"] == b"token=abc&client_id=id&client_secret="

    async def test_leaves_a_request_without_basic_auth_untouched(self):
        seen = await self._run([b"grant_type=x"], [(b"content-length", b"12")])
        assert seen["body"] == b"grant_type=x"

    async def test_leaves_an_oversized_body_untouched(self, monkeypatch):
        monkeypatch.setattr("privacyfence.web.routes_mcp._MAX_BUFFERED_FORM_BYTES", 8)
        chunks = [b"grant_type=", b"authorization_code", b"&code=abc"]
        seen = await self._run(chunks, [(b"authorization", _basic("id", "s").encode())])
        assert seen["body"] == b"".join(chunks)
