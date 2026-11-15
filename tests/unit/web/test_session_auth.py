"""Tests for web/session_auth.py: the CSRF and origin checks and the human-session refusal body."""
from __future__ import annotations

from starlette.requests import Request

from privacyfence.web import session_auth as sa


def _request_with_cookie(cookie_value: str | None) -> Request:
    headers = []
    if cookie_value is not None:
        headers.append((b"cookie", f"{sa.SESSION_COOKIE}={cookie_value}".encode()))
    scope = {"type": "http", "headers": headers, "method": "GET", "path": "/"}
    return Request(scope)




class TestCsrfAndOrigin:
    def test_matching_cookie_and_csrf_value_passes(self):
        request = _request_with_cookie("sess-abc")
        assert sa.check_csrf(request, "sess-abc") is True

    def test_mismatched_csrf_value_fails(self):
        request = _request_with_cookie("sess-abc")
        assert sa.check_csrf(request, "sess-different") is False

    def test_missing_cookie_fails_even_with_a_csrf_value(self):
        request = _request_with_cookie(None)
        assert sa.check_csrf(request, "sess-abc") is False

    def test_missing_csrf_value_fails_even_with_a_cookie(self):
        request = _request_with_cookie("sess-abc")
        assert sa.check_csrf(request, None) is False
        assert sa.check_csrf(request, "") is False

    def test_no_origin_header_is_accepted(self):
        scope = {"type": "http", "headers": [], "method": "POST", "path": "/"}
        assert sa.check_origin(Request(scope)) is True

    def test_matching_origin_is_accepted(self):
        scope = {
            "type": "http", "method": "POST", "path": "/",
            "headers": [(b"host", b"localhost:8765"), (b"origin", b"http://localhost:8765")],
            "scheme": "http", "server": ("localhost", 8765),
        }
        assert sa.check_origin(Request(scope)) is True

    def test_mismatched_origin_is_rejected(self):
        scope = {
            "type": "http", "method": "POST", "path": "/",
            "headers": [(b"host", b"localhost:8765"), (b"origin", b"https://evil.example.com")],
            "scheme": "http", "server": ("localhost", 8765),
        }
        assert sa.check_origin(Request(scope)) is False

    def test_check_csrf_compares_via_hmac_compare_digest(self, monkeypatch):
        # Not just "produces the right answer" -- check_csrf's own docstring
        # promises a constant-time compare specifically to avoid a timing
        # side-channel on the session id. Spy on hmac.compare_digest itself
        # so a future rewrite to a plain ``==`` (equally correct, but
        # timing-unsafe) fails this test even though every other assertion
        # in this class would still pass.
        calls = []
        real_compare_digest = sa.hmac.compare_digest
        monkeypatch.setattr(
            sa.hmac, "compare_digest",
            lambda a, b: calls.append((a, b)) or real_compare_digest(a, b),
        )
        request = _request_with_cookie("sess-abc")

        assert sa.check_csrf(request, "sess-abc") is True

        assert calls == [("sess-abc", "sess-abc")]

