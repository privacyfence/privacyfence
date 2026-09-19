"""Tests for web/session_auth.py: the local-mode session store and the
SEC-06 bootstrap-code store. Mirrors test_org_session.py's own pattern for
OrgSessionStore -- LocalSessionStore is deliberately its local-mode counterpart, minus the
per-principal identity org mode needs and local mode doesn't.
"""
from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from starlette.requests import Request
from starlette.responses import Response

from privacyfence.web import session_auth as sa


def _request_with_cookie(cookie_value: str | None) -> Request:
    headers = []
    if cookie_value is not None:
        headers.append((b"cookie", f"{sa.SESSION_COOKIE}={cookie_value}".encode()))
    scope = {"type": "http", "headers": headers, "method": "GET", "path": "/"}
    return Request(scope)


class TestLocalSessionStore:
    def test_create_then_touch_returns_true(self):
        store = sa.LocalSessionStore()
        session_id = store.create()
        assert store.touch(session_id) is True

    def test_unknown_session_id_returns_false(self):
        store = sa.LocalSessionStore()
        assert store.touch("does-not-exist") is False

    def test_two_sessions_get_two_distinct_ids(self):
        store = sa.LocalSessionStore()
        a = store.create()
        b = store.create()
        assert a != b

    def test_idle_expired_session_is_dropped(self, monkeypatch):
        store = sa.LocalSessionStore(idle_timeout_seconds=60, absolute_timeout_seconds=10_000)
        fake_now = [1000.0]
        monkeypatch.setattr(sa.time, "time", lambda: fake_now[0])
        session_id = store.create()

        fake_now[0] += 120  # older than idle_timeout_seconds
        assert store.touch(session_id) is False
        assert store.session_count == 0

    def test_touch_slides_the_idle_timeout_forward(self, monkeypatch):
        store = sa.LocalSessionStore(idle_timeout_seconds=60, absolute_timeout_seconds=10_000)
        fake_now = [1000.0]
        monkeypatch.setattr(sa.time, "time", lambda: fake_now[0])
        session_id = store.create()

        fake_now[0] += 50  # inside the window -- touches last_seen_at
        assert store.touch(session_id) is True
        fake_now[0] += 50  # would be expired from creation, but not from the touch above
        assert store.touch(session_id) is True

    def test_absolute_expired_session_is_dropped_even_with_recent_activity(self, monkeypatch):
        # The idle timeout alone can't save a session past its absolute
        # cap -- SEC-06's own "cookie with idle + absolute expiry": a
        # session touched every minute for a week must still die once the
        # absolute timeout from *creation* lapses.
        store = sa.LocalSessionStore(idle_timeout_seconds=10_000, absolute_timeout_seconds=100)
        fake_now = [1000.0]
        monkeypatch.setattr(sa.time, "time", lambda: fake_now[0])
        session_id = store.create()

        fake_now[0] += 50
        assert store.touch(session_id) is True  # well within the idle window
        fake_now[0] += 60  # 110s since creation -- past the absolute cap
        assert store.touch(session_id) is False
        assert store.session_count == 0

    def test_destroy_removes_the_session(self):
        store = sa.LocalSessionStore()
        session_id = store.create()
        store.destroy(session_id)
        assert store.touch(session_id) is False

    def test_destroy_unknown_session_is_a_no_op(self):
        store = sa.LocalSessionStore()
        store.destroy("does-not-exist")  # must not raise

    def test_session_count_reflects_live_sessions(self):
        store = sa.LocalSessionStore()
        assert store.session_count == 0
        store.create()
        assert store.session_count == 1


class TestBootstrapStore:
    def test_mint_then_consume_succeeds_exactly_once(self):
        store = sa.BootstrapStore()
        code = store.mint()
        assert store.consume(code) == sa.PROVENANCE_UNATTESTED
        assert store.consume(code) is None  # single-use -- burned by the line above

    def test_consume_returns_the_provenance_the_code_was_minted_with(self):
        store = sa.BootstrapStore()
        assert store.consume(store.mint(provenance=sa.PROVENANCE_HUMAN)) == sa.PROVENANCE_HUMAN
        # The default is the one a bare control-channel MINT gets, and it is
        # the safe one: nothing about that request says a human asked.
        assert store.consume(store.mint()) == sa.PROVENANCE_UNATTESTED

    def test_two_mints_produce_distinct_codes(self):
        store = sa.BootstrapStore()
        assert store.mint() != store.mint()

    def test_unknown_code_is_rejected(self):
        store = sa.BootstrapStore()
        assert store.consume("not-a-real-code") is None

    def test_empty_code_is_rejected(self):
        store = sa.BootstrapStore()
        assert store.consume("") is None

    def test_expired_code_is_rejected_and_still_consumed(self, monkeypatch):
        store = sa.BootstrapStore(ttl_seconds=60)
        fake_now = [1000.0]
        monkeypatch.setattr(sa.time, "time", lambda: fake_now[0])
        code = store.mint()

        fake_now[0] += 120  # past the TTL

        assert store.consume(code) is None
        # ...and it's gone either way -- a second attempt (e.g. a replay
        # racing the first) doesn't get to try again just because the
        # first attempt failed on expiry rather than success.
        fake_now[0] = 1000.0  # even rewinding time doesn't resurrect it
        assert store.consume(code) is None


class TestAuthenticated:
    # authenticated() reads the session cookie only -- there is no ``?token=``
    # (or ``?bootstrap=``) query-string path here to unit-test any more.
    # Pre-SEC-06, this module's authenticated() *was* the ``?token=`` check;
    # SEC-06 (module docstring above) replaced that with the cookie-based
    # LocalSessionStore this class exercises, and moved the query string to
    # a one-time, pre-authentication *exchange* (BOOTSTRAP_QUERY_PARAM,
    # consumed by web/server.py before authenticated() is ever called, not
    # by authenticated() itself). The regression coverage that an old-style
    # ``?token=<secret>`` URL is no longer honored by any route lives at the
    # route level in test_server.py's TestBootstrapFlow (it needs a live app
    # and dispatcher to demonstrate a *route*, not this function, rejects
    # it) -- see that class's own comment for the assertion.

    def test_no_cookie_is_not_authenticated(self):
        store = sa.LocalSessionStore()
        assert sa.authenticated(_request_with_cookie(None), store) is False

    def test_valid_cookie_authenticates(self):
        store = sa.LocalSessionStore()
        session_id = store.create()
        assert sa.authenticated(_request_with_cookie(session_id), store) is True

    def test_forged_cookie_is_not_authenticated(self):
        store = sa.LocalSessionStore()
        store.create()
        assert sa.authenticated(_request_with_cookie("forged-session-id"), store) is False

    def test_query_string_alone_does_not_authenticate(self):
        # No cookie at all, only a query param shaped like the retired
        # token link -- must not authenticate. authenticated() never even
        # looks at query_params, but this pins that contract so a future
        # change can't silently reintroduce a URL-carried credential here.
        store = sa.LocalSessionStore()
        session_id = store.create()
        scope = {
            "type": "http", "headers": [], "method": "GET", "path": "/",
            "query_string": f"token={session_id}".encode(),
        }
        assert sa.authenticated(Request(scope), store) is False


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


class TestSessionCookieHelpers:
    def test_set_session_cookie_is_httponly_samesite_strict(self):
        response = Response()
        sa.set_session_cookie(response, "sess-123")
        set_cookie = response.headers.get("set-cookie", "")
        assert "sess-123" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "samesite=strict" in set_cookie.lower()

    def test_set_session_cookie_omits_secure_in_local_mode(self):
        # The flag that actually differs between the two modes (module
        # docstring; contrast web/org_session.py's set_session_cookie,
        # which passes secure=True). Local mode's own transport is
        # deliberate plain-HTTP loopback (D1, security-and-compliance.md),
        # and a Secure cookie is silently *dropped* by the browser over
        # plain HTTP -- so asserting its absence here isn't pedantry, it's
        # the difference between the session cookie working at all and a
        # user stuck unable to sign in.
        response = Response()
        sa.set_session_cookie(response, "sess-123")
        set_cookie = response.headers.get("set-cookie", "")
        assert "secure" not in set_cookie.lower()

    def test_clear_session_cookie_expires_it(self):
        response = Response()
        sa.clear_session_cookie(response)
        set_cookie = response.headers.get("set-cookie", "")
        assert sa.SESSION_COOKIE in set_cookie


class TestUnauthorizedHtml:
    def _scope(self):
        return {
            "type": "http", "headers": [], "method": "GET", "path": "/approvals",
            "scheme": "http", "server": ("localhost", 8765),
        }

    def test_is_no_store(self):
        # SEC-18: this page names a live control-channel path/pipe name
        # (the exact recovery command a reader is meant to copy-paste) -- it
        # must never be cached, and previously carried no Cache-Control
        # header at all.
        response = sa.unauthorized_html(Request(self._scope()))
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"

    def test_points_at_the_discovery_file_not_the_redacted_log(self):
        # Regression coverage for the actual bug this page used to send
        # readers straight into: privacyfence.log's startup line always
        # reads bootstrap=[REDACTED] (SecretRedactingFormatter, SEC-10), so
        # "open the newest sign-in link PrivacyFence logged" never worked --
        # see web/server.py's _write_bootstrap_url_file for where the real
        # link actually lands instead.
        body = sa.unauthorized_html(Request(self._scope())).body.decode()
        assert "approvals_url" in body
        assert "settings_url" in body
        assert "redact" in body.lower()
        assert "PrivacyFence logged" not in body

    def test_still_offers_the_on_demand_bootstrap_command(self):
        # #428 Phase 2: the on-demand mint goes through the control channel
        # now, not a bearer-authenticated HTTP route.
        body = sa.unauthorized_html(Request(self._scope())).body.decode()
        assert "MINT" in body
        assert "/api/bootstrap" not in body

    def test_does_not_send_the_reader_back_to_their_ai_client(self):
        """Issue #423 part 3 made "ask Claude" the lead here, because P10
        had removed the menu bar and the tool it named was the only way back
        in from inside a conversation. The self-approval plan's Phase 2
        retired that tool -- a live session is not something to hand the
        party it governs -- so the page must not still be recommending it."""
        body = sa.unauthorized_html(Request(self._scope())).body.decode()
        assert "privacyfence_get_sign_in_link" not in body
        assert "companion" in body
        # The lead is the companion, ahead of the "why you're here" line, in
        # the slot "ask Claude" used to hold.
        assert body.index("companion") < body.index("expired, was already used")

    def test_offers_the_break_glass_command_for_a_reader_with_no_companion(self):
        body = sa.unauthorized_html(Request(self._scope())).body.decode()
        assert "privacyfence-app --print-sign-in-link" in body

    def test_shows_the_posix_path_and_a_bash_command_by_default(self, monkeypatch):
        # PurePosixPath, not Path -- a real Path constructed from a POSIX-
        # looking string still renders with backslashes on a host that's
        # actually Windows (WindowsPath's own str()), regardless of what
        # is_windows() is mocked to return for *branch selection* below.
        # PurePosixPath's formatting is fixed to POSIX rules on any host.
        monkeypatch.setattr(sa.paths, "data_dir", lambda: PurePosixPath("/home/alice/.privacyfence"))
        monkeypatch.setattr(sa.paths, "is_windows", lambda: False)

        body = sa.unauthorized_html(Request(self._scope())).body.decode()

        assert "/home/alice/.privacyfence/approvals_url" in body
        assert "nc -U '/home/alice/.privacyfence/authority/control.sock'" in body
        assert "Get-Content" not in body
        assert "NamedPipeClientStream" not in body

    def test_shows_the_windows_path_and_a_powershell_command(self, monkeypatch):
        # paths.is_windows() (not os.name itself -- see that function's own
        # docstring) is the seam this branches on, so this test never
        # touches the real os.name. PureWindowsPath for the same
        # host-independent-formatting reason as the POSIX test above.
        monkeypatch.setattr(
            sa.paths, "data_dir", lambda: PureWindowsPath(r"C:\Users\alice\AppData\Local\PrivacyFence"),
        )
        monkeypatch.setattr(sa.paths, "is_windows", lambda: True)

        body = sa.unauthorized_html(Request(self._scope())).body.decode()

        assert r"C:\Users\alice\AppData\Local\PrivacyFence\approvals_url" in body
        assert "NamedPipeClientStream" in body
        assert "PrivacyFence-Control-" in body
        assert "nc -U" not in body

    def test_companion_sentence_follows_the_platform_not_just_the_marker(self, monkeypatch):
        # #428 Phase 4 changed this sentence, and B5b made it platform-
        # dependent. On a separated macOS/Windows install a tray item really
        # is started for the reader at login. On Linux what a separated
        # install autostarts is the invisible `--serve` channel -- so
        # "it should already be there" would send a locked-out reader
        # hunting for a tray icon ADR 0002 decision 4 says this platform
        # deliberately does not have.
        monkeypatch.setattr(sa.privilege_separation, "is_enabled", lambda: False)
        assert "Nothing installs or starts it automatically yet" in sa._companion_availability_sentence()

        monkeypatch.setattr(sa.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(sa.privilege_separation, "current_platform", lambda: "darwin")
        assert "runs it at login for you" in sa._companion_availability_sentence()

        monkeypatch.setattr(sa.privilege_separation, "current_platform", lambda: "linux")
        linux = sa._companion_availability_sentence()
        assert "Applications-menu entry" in linux
        assert "at login" not in linux


class TestProvenance:
    """The self-approval plan's Phase 2: a session records *how* it was
    established, because three paths reach one (ADR 0002 decision 6) and
    only one of them can be attributed to a person."""

    def _request(self, cookie: str | None):
        headers = [(b"cookie", f"{sa.SESSION_COOKIE}={cookie}".encode())] if cookie else []
        return Request({"type": "http", "headers": headers, "method": "GET", "path": "/"})

    def test_a_session_defaults_to_unattested(self):
        store = sa.LocalSessionStore()
        assert store.provenance(store.create()) == sa.PROVENANCE_UNATTESTED

    def test_a_session_keeps_the_provenance_it_was_created_with(self):
        store = sa.LocalSessionStore()
        assert store.provenance(store.create(provenance=sa.PROVENANCE_HUMAN)) == sa.PROVENANCE_HUMAN

    def test_an_unknown_session_has_no_provenance(self):
        assert sa.LocalSessionStore().provenance("nope") is None

    def test_reading_provenance_does_not_keep_an_idle_session_alive(self, monkeypatch):
        """An authorization question must not double as a heartbeat -- only
        ``touch()`` renews a session, and it is the authentication check that
        calls it."""
        fake_now = [1000.0]
        monkeypatch.setattr(sa.time, "time", lambda: fake_now[0])
        store = sa.LocalSessionStore(idle_timeout_seconds=60)
        session_id = store.create(provenance=sa.PROVENANCE_HUMAN)

        fake_now[0] += 50
        assert store.provenance(session_id) == sa.PROVENANCE_HUMAN
        fake_now[0] += 50  # 100s since creation, and nothing touched it

        assert store.touch(session_id) is False

    def test_is_human_session_reads_the_cookie_s_own_session(self):
        store = sa.LocalSessionStore()
        human = store.create(provenance=sa.PROVENANCE_HUMAN)
        unattested = store.create()

        assert sa.is_human_session(self._request(human), store) is True
        assert sa.is_human_session(self._request(unattested), store) is False
        assert sa.session_provenance(self._request(unattested), store) == sa.PROVENANCE_UNATTESTED

    def test_no_cookie_and_an_unknown_cookie_both_read_as_not_human(self):
        store = sa.LocalSessionStore()
        assert sa.session_provenance(self._request(None), store) is None
        assert sa.is_human_session(self._request(None), store) is False
        assert sa.is_human_session(self._request("not-a-session"), store) is False

    def test_the_refusal_body_is_a_403_that_names_the_way_back(self):
        body, status = sa.human_session_required_json("approve a decision")
        assert status == 403  # not 401: the session is valid, the answer is still no
        assert body["error"] == "human_session_required"
        assert "approve a decision" in body["message"]
        assert "companion" in body["message"]

