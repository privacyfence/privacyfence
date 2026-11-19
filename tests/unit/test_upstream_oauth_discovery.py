"""Discovery and client registration for hosted upstream MCP servers (ADR 0330)."""
from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import httpx2
import pytest

from privacyfence import upstream_oauth_discovery as disc
from privacyfence.upstream_mcp_config import UpstreamOAuthSettings, UpstreamServer
from privacyfence.upstream_oauth_discovery import OAuthEndpoints, RegistrationStore, discover
from privacyfence.upstream_oauth_tokens import OAuthClient, UpstreamOAuthError

PUBLIC = "93.184.216.34"
REAL_RESOLVE = disc._resolve
REDIRECT = "https://pf.example.org/upstream-mcp/canva/callback"


def upstream(url: str = "https://mcp.canva.com/mcp", **oauth: Any) -> UpstreamServer:
    return UpstreamServer(
        id="canva", label="Canva", url=url, oauth=UpstreamOAuthSettings(**oauth),
        allowed_principal_types=("claude_client",),
    )


class FakeServers:
    """Routes ``METHOD url`` to a canned (status, body, headers) and records every request."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: list[httpx2.Request] = []

    def add(self, method: str, url: str, status: int = 200, body: Any = None, headers: dict[str, str] | None = None) -> None:
        self.routes[f"{method} {url}"] = (status, body, headers or {})

    def transport(self) -> httpx2.MockTransport:
        def handler(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            status, body, headers = self.routes.get(f"{request.method} {request.url}", (404, None, {}))
            if isinstance(body, (bytes, bytearray)):
                return httpx2.Response(status, content=bytes(body), headers=headers)
            if isinstance(body, BaseException):
                raise body
            return httpx2.Response(status, json=body, headers=headers)

        return httpx2.MockTransport(handler)

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def as_metadata(issuer: str, **extra: Any) -> dict[str, Any]:
    base = issuer.rstrip("/")
    doc = {
        "issuer": issuer,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "authorization_response_iss_parameter_supported": True,
    }
    doc.update(extra)
    return {k: v for k, v in doc.items() if v is not None}


def canva_servers() -> FakeServers:
    s = FakeServers()
    s.add("POST", "https://mcp.canva.com/mcp", 401, {}, {
        "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.canva.com/.well-known/oauth-protected-resource/mcp"',
    })
    s.add("GET", "https://mcp.canva.com/.well-known/oauth-protected-resource/mcp", 200, {
        "resource": "https://mcp.canva.com/mcp",
        "authorization_servers": ["https://mcp.canva.com"],
        "scopes_supported": ["design:read", "design:write"],
    })
    s.add("GET", "https://mcp.canva.com/.well-known/oauth-authorization-server", 200, as_metadata("https://mcp.canva.com"))
    return s


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    async def resolve(host: str) -> list[str]:
        asked.append(host)
        return [PUBLIC]

    monkeypatch.setattr(disc, "_resolve", resolve)
    return asked


def refused(coro: Any, kind: str = "invalid_metadata") -> None:
    with pytest.raises(UpstreamOAuthError) as caught:
        asyncio.run(coro)
    assert str(caught.value) == kind


# ----------------------------------------------------------------------------- discover


class TestDiscoverHappyPaths:
    def test_canva_shaped_server(self, public_dns: list[str]) -> None:
        s = canva_servers()
        endpoints = asyncio.run(discover(upstream(), transport=s.transport()))
        assert endpoints == OAuthEndpoints(
            issuer="https://mcp.canva.com",
            authorization_endpoint="https://mcp.canva.com/authorize",
            token_endpoint="https://mcp.canva.com/token",
            registration_endpoint="https://mcp.canva.com/register",
            resource="https://mcp.canva.com/mcp",
            scope="design:read design:write",
            token_auth_methods=("none",),
            iss_supported=True,
            loopback=False,
        )
        assert public_dns == ["mcp.canva.com"]
        first = s.requests[0]
        assert first.method == "POST"
        assert first.headers["accept"] == "application/json, text/event-stream"
        assert json.loads(first.content)["method"] == "initialize"

    def test_framer_shaped_server_with_an_issuer_path_on_another_host(self) -> None:
        s = FakeServers()
        s.add("POST", "https://mcp.framer.com/mcp", 401, {}, {})
        s.add("GET", "https://mcp.framer.com/.well-known/oauth-protected-resource/mcp", 200, {
            "resource": "https://mcp.framer.com/mcp",
            "authorization_servers": ["https://auth.framer.com/tenant"],
        })
        s.add("GET", "https://auth.framer.com/.well-known/oauth-authorization-server/tenant", 200, as_metadata(
            "https://auth.framer.com/tenant",
            authorization_endpoint="https://auth.framer.com/tenant/authorize",
            token_endpoint="https://auth.framer.com/tenant/token",
            registration_endpoint=None,
            token_endpoint_auth_methods_supported=None,
            authorization_response_iss_parameter_supported=None,
        ))
        endpoints = asyncio.run(discover(upstream("https://mcp.framer.com/mcp"), transport=s.transport()))
        assert endpoints.issuer == "https://auth.framer.com/tenant"
        assert endpoints.registration_endpoint is None
        assert endpoints.token_auth_methods == ("client_secret_basic",)
        assert endpoints.iss_supported is False
        assert endpoints.scope is None

    def test_a_hint_on_the_upstreams_host_is_followed(self) -> None:
        s = canva_servers()
        asyncio.run(discover(upstream(), transport=s.transport()))
        assert s.urls[1] == "https://mcp.canva.com/.well-known/oauth-protected-resource/mcp"

    def test_a_hint_on_another_host_is_skipped(self) -> None:
        s = canva_servers()
        s.add("POST", "https://mcp.canva.com/mcp", 401, {}, {
            "WWW-Authenticate": 'Bearer resource_metadata="https://evil.example.net/prm"',
        })
        asyncio.run(discover(upstream(), transport=s.transport()))
        assert not any("evil.example.net" in url for url in s.urls)

    def test_no_hint_falls_back_to_the_well_known_urls(self) -> None:
        s = canva_servers()
        s.add("POST", "https://mcp.canva.com/mcp", 200, b"event: message\n\n")
        asyncio.run(discover(upstream(), transport=s.transport()))
        assert s.urls[1] == "https://mcp.canva.com/.well-known/oauth-protected-resource/mcp"

    def test_a_failing_candidate_moves_on_to_the_next(self) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-protected-resource/mcp", 404, {})
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-protected-resource", 200, {
            "resource": "https://mcp.canva.com/mcp", "authorization_servers": ["https://mcp.canva.com"],
        })
        s.add("POST", "https://mcp.canva.com/mcp", 401, {}, {})
        endpoints = asyncio.run(discover(upstream(), transport=s.transport()))
        assert endpoints.scope is None

    def test_a_non_object_or_invalid_document_moves_on(self) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-protected-resource/mcp", 200, [1])
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-protected-resource", 200, {"resource": "x"})
        refused(discover(upstream(), transport=s.transport()))

    def test_the_sdk_fallbacks_are_never_requested(self) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-authorization-server", 200, as_metadata(
            "https://mcp.canva.com", registration_endpoint=None,
        ))
        asyncio.run(discover(upstream(), transport=s.transport()))
        for url in s.urls:
            assert not url.endswith(("/register", "/token"))
        assert s.urls[-1] == "https://mcp.canva.com/.well-known/oauth-authorization-server"
        assert all(not u.endswith("/.well-known/openid-configuration") for u in s.urls)


class TestDiscoverRefusals:
    def test_no_metadata_at_all(self) -> None:
        s = FakeServers()
        s.add("POST", "https://mcp.canva.com/mcp", 401, {}, {})
        refused(discover(upstream(), transport=s.transport()))

    @pytest.mark.parametrize("document", [
        {"authorization_servers": ["https://mcp.canva.com"]},
        {"resource": "https://mcp.canva.com/mcp"},
        {"resource": "https://mcp.canva.com/mcp", "authorization_servers": []},
    ])
    def test_a_document_without_resource_or_servers(self, document: dict[str, Any]) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-protected-resource/mcp", 200, document)
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-protected-resource", 200, document)
        refused(discover(upstream(), transport=s.transport()))

    def test_a_resource_that_is_not_the_upstream(self) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-protected-resource/mcp", 200, {
            "resource": "https://other.canva.com/mcp", "authorization_servers": ["https://mcp.canva.com"],
        })
        refused(discover(upstream(), transport=s.transport()))

    def test_a_pinned_issuer_that_is_not_listed(self) -> None:
        refused(discover(upstream(issuer="https://elsewhere.example.com"), transport=canva_servers().transport()))

    @pytest.mark.parametrize(("pinned", "listed"), [
        ("https://mcp.canva.com/", "https://mcp.canva.com"),
        ("https://mcp.canva.com", "https://mcp.canva.com/"),
        ("https://mcp.canva.com", "https://mcp.canva.com"),
    ])
    def test_a_pinned_issuer_that_is_listed_is_used(self, pinned: str, listed: str) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-protected-resource/mcp", 200, {
            "resource": "https://mcp.canva.com/mcp",
            "authorization_servers": ["https://other.example.com", listed],
        })
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-authorization-server", 200, as_metadata(listed))
        endpoints = asyncio.run(discover(upstream(issuer=pinned), transport=s.transport()))
        assert endpoints.issuer == listed

    def test_an_issuer_mismatch_in_the_metadata(self) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-authorization-server", 200, as_metadata("https://other.example.com"))
        s.add("GET", "https://mcp.canva.com/.well-known/openid-configuration", 200, as_metadata("https://other.example.com"))
        refused(discover(upstream(), transport=s.transport()))

    def test_unparseable_server_metadata(self) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-authorization-server", 200, {"issuer": "https://mcp.canva.com"})
        refused(discover(upstream(), transport=s.transport()))

    @pytest.mark.parametrize("field", ["authorization_endpoint", "token_endpoint", "registration_endpoint"])
    @pytest.mark.parametrize("bad", [
        "https://other.example.com/x",
        "https://mcp.canva.com:8443/x",
        "http://mcp.canva.com/x",
        "https://user:pw@mcp.canva.com/x",
        "https://mcp.canva.com/x#frag",
    ])
    def test_an_endpoint_that_is_not_on_the_issuers_host(self, field: str, bad: str) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-authorization-server", 200, as_metadata(
            "https://mcp.canva.com", **{field: bad},
        ))
        refused(discover(upstream(), transport=s.transport()))

    @pytest.mark.parametrize("methods", [["plain"], []])
    def test_s256_is_required(self, methods: list[str]) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-authorization-server", 200, as_metadata(
            "https://mcp.canva.com", code_challenge_methods_supported=methods,
        ))
        refused(discover(upstream(), transport=s.transport()))

    def test_an_absent_s256_list(self) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-authorization-server", 200, as_metadata(
            "https://mcp.canva.com", code_challenge_methods_supported=None,
        ))
        refused(discover(upstream(), transport=s.transport()))

    def test_a_redirect_is_not_followed(self) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-authorization-server", 302, None, {"Location": "http://169.254.169.254/"})
        s.add("GET", "https://mcp.canva.com/.well-known/openid-configuration", 302, None, {"Location": "http://169.254.169.254/"})
        refused(discover(upstream(), transport=s.transport()))
        assert not any("169.254" in url for url in s.urls)

    def test_a_body_over_64_kib(self) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-protected-resource/mcp", 200, b"{" + b" " * 70000 + b"}")
        refused(discover(upstream(), transport=s.transport()), "unreachable")

    def test_a_transport_error(self) -> None:
        s = FakeServers()
        s.add("POST", "https://mcp.canva.com/mcp", body=httpx2.ConnectError("down"))
        refused(discover(upstream(), transport=s.transport()), "unreachable")


class TestUrlRule:
    @pytest.mark.parametrize("address", [
        "10.0.0.1", "127.0.0.1", "169.254.169.254", "192.168.1.1", "[::1]", "[fc00::1]",
        "0.0.0.0", "224.0.0.1", "[fe80::1]",
    ])
    def test_a_private_ip_literal_is_refused(self, address: str) -> None:
        with pytest.raises(UpstreamOAuthError):
            disc._checked_url(f"https://{address}/x", host=None, loopback=False)

    def test_a_public_ip_literal_is_allowed(self) -> None:
        disc._checked_url("https://93.184.216.34/x", host=None, loopback=False)

    @pytest.mark.parametrize("url", [
        "https://user@example.com/x", "https://u:p@example.com/x", "https://example.com/x#f",
        "http://example.com/x", "ftp://example.com/x", "https://localhost/x", "https://intranet/x",
        "https://router.internal/x", "https://printer.local/x", "https://nas.home.arpa/x",
        "https://box.lan/x", "https:///x", "https://example.com:notaport/x",
    ])
    def test_other_refusals(self, url: str) -> None:
        with pytest.raises(UpstreamOAuthError) as caught:
            disc._checked_url(url, host=None, loopback=False)
        assert str(caught.value) == "invalid_metadata"

    def test_the_port_is_part_of_the_host_match(self) -> None:
        disc._checked_url("https://Example.com:8443/x", host="example.com:8443", loopback=False)
        for other in ("example.com", "example.com:9000"):
            with pytest.raises(UpstreamOAuthError):
                disc._checked_url("https://example.com:8443/x", host=other, loopback=False)

    def test_an_ipv6_literal_host_keeps_its_brackets_in_the_match(self) -> None:
        disc._checked_url("http://[::1]:9000/x", host="[::1]:9000", loopback=True)

    def test_loopback_relaxes_only_loopback_hosts(self) -> None:
        for url in ("http://localhost:8000/x", "http://127.0.0.1:8000/x", "https://localhost/x", "http://[::1]/x"):
            disc._checked_url(url, host=None, loopback=True)
        for url in ("http://169.254.169.254/x", "https://10.0.0.1/x", "http://example.com/x", "https://intranet/x"):
            with pytest.raises(UpstreamOAuthError):
                disc._checked_url(url, host=None, loopback=True)


class TestLoopbackUpstream:
    def local(self) -> tuple[FakeServers, UpstreamServer]:
        s = FakeServers()
        s.add("POST", "http://localhost:9000/mcp", 401, {}, {})
        s.add("GET", "http://localhost:9000/.well-known/oauth-protected-resource/mcp", 200, {
            "resource": "http://localhost:9000/mcp", "authorization_servers": ["http://localhost:9000"],
        })
        s.add("GET", "http://localhost:9000/.well-known/oauth-authorization-server", 200, as_metadata("http://localhost:9000"))
        return s, upstream("http://localhost:9000/mcp")

    def test_a_local_upstream_works_over_http_without_a_dns_lookup(self, public_dns: list[str]) -> None:
        s, up = self.local()
        endpoints = asyncio.run(discover(up, transport=s.transport()))
        assert endpoints.loopback is True
        assert endpoints.token_endpoint == "http://localhost:9000/token"
        assert public_dns == []

    @pytest.mark.parametrize("target", ["http://169.254.169.254/token", "https://10.0.0.5/token", "https://internal.local/token"])
    def test_a_local_upstream_cannot_point_at_a_private_address(self, target: str) -> None:
        s, up = self.local()
        s.add("GET", "http://localhost:9000/.well-known/oauth-authorization-server", 200, as_metadata(
            "http://localhost:9000", token_endpoint=target,
        ))
        refused(discover(up, transport=s.transport()))

    def test_a_local_upstream_naming_a_private_issuer_is_refused(self) -> None:
        s, up = self.local()
        s.add("GET", "http://localhost:9000/.well-known/oauth-protected-resource/mcp", 200, {
            "resource": "http://localhost:9000/mcp", "authorization_servers": ["http://169.254.169.254"],
        })
        refused(discover(up, transport=s.transport()))

    def test_a_malformed_upstream_url_is_refused(self) -> None:
        refused(discover(upstream("https://example.com:badport/mcp")))


class TestRequirePublic:
    @pytest.fixture
    def run(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        def go(answer: Any, loopback: bool = False, host: str = "auth.example.com") -> None:
            async def resolve(_: str) -> list[str]:
                if isinstance(answer, BaseException):
                    raise answer
                return answer

            monkeypatch.setattr(disc, "_resolve", resolve)
            asyncio.run(disc._require_public(host, loopback))

        return go

    @pytest.mark.parametrize("address", ["10.0.0.1", "127.0.0.1", "169.254.169.254", "::1", "fe80::1%eth0", "not-an-address"])
    def test_a_non_public_answer_is_refused(self, run: Any, address: str) -> None:
        with pytest.raises(UpstreamOAuthError) as caught:
            run([PUBLIC, address])
        assert str(caught.value) == "invalid_metadata"

    def test_a_resolution_error_is_unreachable(self, run: Any) -> None:
        with pytest.raises(UpstreamOAuthError) as caught:
            run(OSError("nxdomain"))
        assert str(caught.value) == "unreachable"

    def test_public_addresses_pass(self, run: Any) -> None:
        run([PUBLIC, "2606:4700::1111"])

    def test_loopback_skips_the_check_only_for_a_loopback_host(self, run: Any) -> None:
        run(["10.0.0.1"], loopback=True, host="localhost")
        run(["10.0.0.1"], loopback=True, host="127.0.0.1")
        with pytest.raises(UpstreamOAuthError):
            run(["10.0.0.1"], loopback=True, host="auth.example.com")


class TestRealResolve:
    def test_resolves_through_getaddrinfo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(disc.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", (PUBLIC, 0))])
        assert asyncio.run(REAL_RESOLVE("example.com")) == [PUBLIC]


class TestScopeSelection:
    def test_declared_scopes_win(self) -> None:
        endpoints = asyncio.run(discover(upstream(scopes=("a", "b")), transport=canva_servers().transport()))
        assert endpoints.scope == "a b"

    def test_scopes_supported_is_the_fallback(self) -> None:
        assert asyncio.run(discover(upstream(), transport=canva_servers().transport())).scope == "design:read design:write"

    def test_none_when_nothing_is_listed(self) -> None:
        s = canva_servers()
        s.add("GET", "https://mcp.canva.com/.well-known/oauth-protected-resource/mcp", 200, {
            "resource": "https://mcp.canva.com/mcp", "authorization_servers": ["https://mcp.canva.com"], "scopes_supported": [],
        })
        assert asyncio.run(discover(upstream(), transport=s.transport())).scope is None


def test_the_sdk_names_this_module_uses_exist() -> None:
    from mcp.client.auth.exceptions import OAuthFlowError
    from mcp.client.auth.utils import (
        build_oauth_authorization_server_metadata_discovery_urls,
        build_protected_resource_metadata_discovery_urls,
        extract_resource_metadata_from_www_auth,
        validate_metadata_issuer,
    )
    from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, ProtectedResourceMetadata
    from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url

    assert all(callable(n) for n in (
        OAuthFlowError, build_oauth_authorization_server_metadata_discovery_urls,
        build_protected_resource_metadata_discovery_urls, extract_resource_metadata_from_www_auth,
        validate_metadata_issuer, OAuthClientInformationFull, OAuthMetadata, ProtectedResourceMetadata,
        check_resource_allowed, resource_url_from_server_url,
    ))


# ----------------------------------------------------------------------------- registration


def endpoints(**changes: Any) -> OAuthEndpoints:
    base = OAuthEndpoints(
        issuer="https://mcp.canva.com", authorization_endpoint="https://mcp.canva.com/authorize",
        token_endpoint="https://mcp.canva.com/token", registration_endpoint="https://mcp.canva.com/register",
        resource="https://mcp.canva.com/mcp", scope="design:read", token_auth_methods=("none",),
        iss_supported=True, loopback=False,
    )
    return OAuthEndpoints(**{**base.__dict__, **changes})


def registering(client_id: str = "dyn-1", **extra: Any) -> FakeServers:
    s = FakeServers()
    s.add("POST", "https://mcp.canva.com/register", 201, {"client_id": client_id, "redirect_uris": [REDIRECT], **extra})
    return s


def ensure(store: RegistrationStore, up: UpstreamServer | None = None, ep: OAuthEndpoints | None = None, redirect: str = REDIRECT) -> OAuthClient:
    return asyncio.run(store.ensure_client(up or upstream(), ep or endpoints(), redirect))


class TestDeclaredClient:
    def test_without_a_secret(self, tmp_path: Path) -> None:
        store = RegistrationStore(tmp_path / "reg")
        client = ensure(store, upstream(client_id="cid"))
        assert client == OAuthClient("cid", None, "none")
        assert not (tmp_path / "reg").exists()

    @pytest.mark.parametrize(("methods", "expected"), [
        (("client_secret_post", "client_secret_basic"), "client_secret_post"),
        (("client_secret_basic",), "client_secret_basic"),
        (("none",), "client_secret_basic"),
    ])
    def test_with_a_secret(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, methods: tuple[str, ...], expected: str) -> None:
        monkeypatch.setenv("CANVA_SECRET", "s3cret")
        store = RegistrationStore(tmp_path / "reg")
        client = ensure(store, upstream(client_id="cid", client_secret_env="CANVA_SECRET"), endpoints(token_auth_methods=methods))
        assert client == OAuthClient("cid", "s3cret", expected)

    def test_an_empty_variable_is_not_configured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CANVA_SECRET", "")
        with pytest.raises(UpstreamOAuthError) as caught:
            ensure(RegistrationStore(tmp_path), upstream(client_id="cid", client_secret_env="CANVA_SECRET"))
        assert str(caught.value) == "not_configured"


class TestDynamicRegistration:
    @pytest.mark.parametrize(("methods", "sent", "echo", "secret", "granted"), [
        (("client_secret_post", "none"), "none", None, None, "none"),
        (("client_secret_basic", "client_secret_post"), "client_secret_post", "client_secret_post", "sek", "client_secret_post"),
        (("client_secret_basic",), "client_secret_basic", None, "sek", "client_secret_basic"),
    ])
    def test_the_request_and_the_cache_file(
        self, tmp_path: Path, methods: tuple[str, ...], sent: str, echo: str | None, secret: str | None, granted: str,
    ) -> None:
        extra: dict[str, Any] = {"client_secret": secret, "token_endpoint_auth_method": echo, "client_secret_expires_at": 0}
        s = registering(**extra)
        store = RegistrationStore(tmp_path / "reg", transport=s.transport())
        client = ensure(store, ep=endpoints(token_auth_methods=methods))
        assert client == OAuthClient("dyn-1", secret, granted)
        assert json.loads(s.requests[0].content) == {
            "client_name": "PrivacyFence", "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": sent, "scope": "design:read",
        }
        path = tmp_path / "reg" / "canva.json"
        saved = json.loads(path.read_text())
        assert saved["version"] == 1
        assert {k: saved[k] for k in ("upstream_id", "issuer", "redirect_uri", "client_id", "client_secret", "auth_method")} == {
            "upstream_id": "canva", "issuer": "https://mcp.canva.com", "redirect_uri": REDIRECT,
            "client_id": "dyn-1", "client_secret": secret, "auth_method": granted,
        }
        assert saved["registered_at"] > 0
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    def test_a_secret_with_an_echoed_none_method_becomes_basic(self, tmp_path: Path) -> None:
        s = registering(client_secret="sek", token_endpoint_auth_method="none")
        assert ensure(RegistrationStore(tmp_path, transport=s.transport())).auth_method == "client_secret_basic"

    def test_scope_is_omitted_when_there_is_none(self, tmp_path: Path) -> None:
        s = registering()
        ensure(RegistrationStore(tmp_path, transport=s.transport()), ep=endpoints(scope=None))
        assert "scope" not in json.loads(s.requests[0].content)

    def test_a_second_call_sends_no_request(self, tmp_path: Path) -> None:
        s = registering()
        store = RegistrationStore(tmp_path, transport=s.transport())
        first = ensure(store)
        second = ensure(store)
        assert first == second
        assert len(s.requests) == 1

    @pytest.mark.parametrize("change", ["redirect", "issuer"])
    def test_a_changed_redirect_or_issuer_registers_again(self, tmp_path: Path, change: str) -> None:
        s = registering()
        store = RegistrationStore(tmp_path, transport=s.transport())
        ensure(store)
        if change == "redirect":
            s.add("POST", "https://mcp.canva.com/register", 201, {"client_id": "dyn-2", "redirect_uris": ["https://new.example.org/cb"]})
            client = ensure(store, redirect="https://new.example.org/cb")
        else:
            s.add("POST", "https://mcp.canva.com/register", 201, {"client_id": "dyn-2", "redirect_uris": [REDIRECT]})
            client = ensure(store, ep=endpoints(issuer="https://mcp.canva.com/other"))
        assert client.client_id == "dyn-2"
        assert len(s.requests) == 2

    @pytest.mark.parametrize(("expires_in", "again"), [(10, True), (-5, True), (3600, False), (0, False)])
    def test_an_expiring_secret_registers_again(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expires_in: int, again: bool) -> None:
        monkeypatch.setattr(disc.time, "time", lambda: 1000.0)
        expires_at = 0 if expires_in == 0 else 1000 + expires_in
        s = registering(client_secret="sek", client_secret_expires_at=expires_at)
        store = RegistrationStore(tmp_path, transport=s.transport())
        ensure(store)
        ensure(store)
        assert len(s.requests) == (2 if again else 1)

    def test_concurrent_calls_register_once(self, tmp_path: Path) -> None:
        s = registering()
        store = RegistrationStore(tmp_path, transport=s.transport())

        async def both() -> list[OAuthClient]:
            return list(await asyncio.gather(
                store.ensure_client(upstream(), endpoints(), REDIRECT), store.ensure_client(upstream(), endpoints(), REDIRECT),
            ))

        first, second = asyncio.run(both())
        assert first == second
        assert len(s.requests) == 1

    @pytest.mark.parametrize("answer", [
        (201, {"redirect_uris": [REDIRECT]}),
        (201, {"client_id": "x", "redirect_uris": ["https://elsewhere.example.org/cb"]}),
        (201, {"client_id": "", "redirect_uris": [REDIRECT]}),
        (400, {"error": "invalid_redirect_uri", "error_description": "SENTINEL-BODY"}),
        (201, None),
    ])
    def test_a_bad_answer_is_registration_failed(self, tmp_path: Path, answer: tuple[int, Any], caplog: pytest.LogCaptureFixture) -> None:
        s = FakeServers()
        s.add("POST", "https://mcp.canva.com/register", *answer)
        with pytest.raises(UpstreamOAuthError) as caught:
            ensure(RegistrationStore(tmp_path / "reg", transport=s.transport()))
        assert str(caught.value) == "registration_failed"
        assert "SENTINEL-BODY" not in caplog.text and "SENTINEL-BODY" not in repr(caught.value)
        assert not (tmp_path / "reg" / "canva.json").exists()

    def test_an_answer_without_redirect_uris_is_accepted(self, tmp_path: Path) -> None:
        s = FakeServers()
        s.add("POST", "https://mcp.canva.com/register", 200, {"client_id": "dyn-1"})
        assert ensure(RegistrationStore(tmp_path, transport=s.transport())).client_id == "dyn-1"

    def test_no_registration_endpoint_without_a_declared_id(self, tmp_path: Path) -> None:
        with pytest.raises(UpstreamOAuthError) as caught:
            ensure(RegistrationStore(tmp_path), ep=endpoints(registration_endpoint=None))
        assert str(caught.value) == "registration_failed"


class TestClientFor:
    def test_the_declared_client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CANVA_SECRET", "s3cret")
        up = upstream(client_id="cid", client_secret_env="CANVA_SECRET")
        store = RegistrationStore(tmp_path, upstreams=[up])
        assert store.client_for("canva") == OAuthClient("cid", "s3cret", "client_secret_basic")
        ensure(store, up, endpoints(token_auth_methods=("client_secret_post",)))
        assert store.client_for("canva").auth_method == "client_secret_post"

    def test_the_cached_registration(self, tmp_path: Path) -> None:
        s = registering(client_secret="sek")
        store = RegistrationStore(tmp_path, transport=s.transport())
        registered = ensure(store, upstream(), endpoints(token_auth_methods=("client_secret_basic",)))
        assert RegistrationStore(tmp_path, upstreams=[upstream()]).client_for("canva") == registered

    def test_a_declared_upstream_with_no_client_id_falls_to_the_cache(self, tmp_path: Path) -> None:
        with pytest.raises(UpstreamOAuthError) as caught:
            RegistrationStore(tmp_path, upstreams=[upstream()]).client_for("canva")
        assert str(caught.value) == "not_configured"

    @pytest.mark.parametrize("content", [
        None, "not json", "[]", '{"version": 2, "client_id": "a", "auth_method": "none"}',
        '{"version": 1, "client_id": 3, "auth_method": "none"}',
        '{"version": 1, "client_id": "a", "auth_method": "none", "client_secret": 5}',
    ])
    def test_nothing_usable_is_not_configured(self, tmp_path: Path, content: str | None) -> None:
        if content is not None:
            (tmp_path / "canva.json").write_text(content)
        with pytest.raises(UpstreamOAuthError) as caught:
            RegistrationStore(tmp_path).client_for("canva")
        assert str(caught.value) == "not_configured"

    def test_forget_removes_the_file_and_is_idempotent(self, tmp_path: Path) -> None:
        s = registering()
        store = RegistrationStore(tmp_path, transport=s.transport())
        ensure(store)
        store.forget("canva")
        store.forget("canva")
        assert not (tmp_path / "canva.json").exists()
        with pytest.raises(UpstreamOAuthError):
            store.client_for("canva")
