"""Tests for web/csp.py -- the shared nonce/CSP helpers (ADR 0063).
web/test_server.py covers the middleware/header-emission side end-to-end;
this module covers the small pure helpers in isolation.
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from privacyfence.web.csp import (
    build_csp,
    frame_self_for,
    new_nonce,
    nonce_for,
    plugin_embed_for,
    set_frame_self,
    set_nonce,
    set_plugin_embed,
)


def _request(state: dict | None = None) -> Request:
    scope = {"type": "http", "headers": [], "state": state if state is not None else {}}
    return Request(scope)


class TestNewNonce:
    def test_returns_a_non_empty_string(self):
        assert isinstance(new_nonce(), str)
        assert new_nonce()

    def test_two_calls_differ(self):
        assert new_nonce() != new_nonce()


class TestNonceFor:
    def test_reads_the_value_set_by_the_middleware(self):
        request = _request({"csp_nonce": "from-middleware"})
        assert nonce_for(request) == "from-middleware"

    def test_falls_back_to_a_fresh_value_when_middleware_absent(self):
        # Several route modules' own tests build a bare create_app() with
        # none of web/server.py's middleware stack -- this must not raise.
        request = _request({})
        assert nonce_for(request)


class TestSetNonce:
    def test_overrides_what_nonce_for_then_returns(self):
        request = _request({"csp_nonce": "original"})
        set_nonce(request, "overridden")
        assert nonce_for(request) == "overridden"

    def test_visible_on_the_same_scope_state_dict_a_middleware_would_read(self):
        # web/server.py's _SecurityHeadersMiddleware reads scope["state"]
        # directly (not via Request.state) when it builds the response
        # header, after the route has already run -- set_nonce has to
        # mutate that same dict, not shadow it.
        scope = {"type": "http", "headers": [], "state": {"csp_nonce": "original"}}
        request = Request(scope)
        set_nonce(request, "overridden")
        assert scope["state"]["csp_nonce"] == "overridden"


def _directives(csp: str) -> dict[str, str]:
    return dict(part.strip().split(" ", 1) for part in csp.split(";") if part.strip())


class TestFrameSelf:
    def test_unset_by_default(self):
        assert frame_self_for(_request({})) is False
        assert frame_self_for({"type": "http"}) is False

    def test_set_is_visible_on_the_request_and_the_raw_scope(self):
        scope = {"type": "http", "headers": [], "state": {}}
        set_frame_self(Request(scope))
        assert frame_self_for(Request(scope)) is True
        assert frame_self_for(scope) is True

    def test_does_not_set_the_plugin_embed_flag(self):
        scope = {"type": "http", "headers": [], "state": {}}
        set_frame_self(Request(scope))
        assert plugin_embed_for(scope) is False


class TestPluginEmbed:
    def test_unset_by_default(self):
        assert plugin_embed_for(_request({})) is False
        assert plugin_embed_for({"type": "http"}) is False

    def test_set_is_visible_on_the_request_and_the_raw_scope(self):
        scope = {"type": "http", "headers": [], "state": {}}
        set_plugin_embed(Request(scope))
        assert plugin_embed_for(Request(scope)) is True
        assert plugin_embed_for(scope) is True

    def test_does_not_set_the_frame_self_flag(self):
        scope = {"type": "http", "headers": [], "state": {}}
        set_plugin_embed(Request(scope))
        assert frame_self_for(scope) is False


class TestBuildCspFrameSelf:
    def test_default_frame_src_is_data_only(self):
        assert _directives(build_csp("n"))["frame-src"] == "data:"
        assert build_csp("n") == build_csp("n", frame_self=False)

    @pytest.mark.parametrize("app_origin", ["", "https://org.example.com"])
    def test_frame_self_adds_self_to_frame_src_only(self, app_origin):
        plain = _directives(build_csp("n", app_origin=app_origin))
        framed = _directives(build_csp("n", app_origin=app_origin, frame_self=True))
        assert framed["frame-src"] == "data: 'self'"
        assert {k: v for k, v in framed.items() if k != "frame-src"} == {
            k: v for k, v in plain.items() if k != "frame-src"
        }
        assert framed["frame-ancestors"] == "'none'"
