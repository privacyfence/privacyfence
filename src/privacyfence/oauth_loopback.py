"""Shared browser-loopback OAuth 2.0 helper.

Used by every local-mode connector authorize flow: Slack, Salesforce, Atlassian,
and -- through ``google_oauth.authorize_local()`` -- every Google connector.
Handles the parts every Authorization Code + PKCE flow needs: a short-lived local
HTTP server to catch the redirect, CSRF ``state`` verification, and PKCE
``code_verifier``/``code_challenge`` generation.

Slack/Salesforce/Atlassian all require an exact-match redirect URI in their app's
allow-list, so those callers pass a fixed port. Google's "Desktop app" OAuth
clients accept any loopback port, so its caller passes ``port=0`` and lets the
OS pick one.

``run_browser_oauth()`` opens a browser
on **the machine running the PrivacyFence daemon**, not on
whatever device the person clicking "Authenticate…" is holding -- by default
(``open_browser=None``) via ``_default_open_browser()``, which asks a running
companion app (#428 Phase 3, ADR 0002 decision 5) to do it and falls back to
calling ``webbrowser.open()`` directly when none is running, still the normal
case today. In ``local`` mode
that's the same machine by construction (this is the whole assumption `local`
mode makes), so it works as-is;
it stops being true the moment a browser tab reaches a `local`-mode daemon from
a different device (a phone tunneled to a laptop, say). ``local`` mode's web
settings page says so in its own copy next to a connector's Authenticate button
(settings_window_html.py's `renderConnectors`) rather than leaving it implicit
-- a cheap, honest statement of a real limitation beats a silent one.

P8 built the "next step" this
docstring used to describe here: ``org`` mode doesn't use this module's
loopback listener at all. ``web/routes_connect.py``'s ``GET /oauth/start/
{service}``/``GET /oauth/callback/{service}`` build the same authorize
URL/exchange the code server-side, against a real public redirect_uri
(``{issuer_url}/oauth/callback/{service}``), with the browser doing the
provider round trip instead of a loopback port on this machine. That's
exactly why ``build_authorize_url``/``exchange_code`` were hoisted out of
each of slack_client.py/salesforce_client.py/atlassian_oauth.py's own
``authorize_interactive`` into public, module-level functions (P8): this
module's ``run_browser_oauth`` is still what drives them in ``local`` mode
(unchanged, byte-identical), but ``org`` mode calls the same functions
directly and drives the provider round trip over real HTTP redirects
instead. Google's equivalent functions live in ``google_oauth.py``, whose
``authorize_local()`` drives them through this module's loopback flow too.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import socketserver
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)


class _LoopbackHTTPServer(HTTPServer):
    """HTTPServer, minus the reverse-DNS lookup HTTPServer.server_bind()
    normally does on every bind (``socket.getfqdn(host)``, purely to set
    ``self.server_name`` for access logging).

    That lookup can hang for a long time on machines/networks with slow or
    unusual DNS resolution — even for 127.0.0.1 — and it runs synchronously
    inside the constructor, before the accept loop's background thread ever
    starts, so a slow resolver here stalls the whole OAuth flow before the
    user even sees a browser window. We never read server_name (log_message
    is overridden to a no-op below), so skip HTTPServer's override entirely
    and fall back to TCPServer's plain bind.

    Also turns off ``HTTPServer.allow_reuse_address`` (on by default, purely
    for the usual "restart the dev server without waiting out TIME_WAIT"
    convenience, which a one-shot loopback listener never needs). On a
    privilege-separated install the agent runs as a different, less-trusted
    process than the daemon (ADR 0002) and could bind this fixed port first
    to intercept the Slack/Salesforce/Atlassian callback. On POSIX that
    squat already makes the daemon's own bind() fail loudly with the
    actionable ``OAuthLoopbackError`` below. On Windows, ``SO_REUSEADDR`` on
    the *new* socket lets it silently steal a port an existing socket is
    still actively listening on -- regardless of what the first socket set
    -- so leaving reuse enabled here would let the daemon's bind() succeed
    over a squatted port instead of detecting it, and which of the two
    processes then receives the provider's redirect becomes undefined. No
    reuse means bind() always fails cleanly when the port is already held.
    """

    allow_reuse_address = False

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


_SUCCESS_HTML = b"""<!doctype html><html><head><title>PrivacyFence</title></head>
<body style="font-family: -apple-system, sans-serif; text-align: center; padding-top: 4em;">
<h2>You're connected.</h2><p>You can close this window and return to PrivacyFence.</p>
</body></html>"""

_ERROR_HTML = b"""<!doctype html><html><head><title>PrivacyFence</title></head>
<body style="font-family: -apple-system, sans-serif; text-align: center; padding-top: 4em;">
<h2>Something went wrong.</h2><p>Close this window and try again from PrivacyFence.</p>
</body></html>"""


class OAuthLoopbackError(Exception):
    """Raised when the loopback OAuth flow fails (timeout, state mismatch, provider error)."""


def _default_open_browser(url: str) -> bool:
    """The default ``open_browser`` behind ``run_browser_oauth()`` below --
    ADR 0002 decision 5: try asking a running companion app to open ``url``
    first (what #428 Phase 4 makes mandatory on Windows, where a
    service-hosted daemon can't reach the user's desktop session to open a
    browser itself), and fall back to opening it directly when no companion
    is running -- still the normal case pre-Phase-4 (the companion isn't
    autostarted until then), and always the case on Linux, which has no
    persistent companion process to ask (see companion.py's own module
    docstring). Both imports are deferred: this module is imported broadly
    (every local-mode connector's ``authorize_interactive``), so it
    shouldn't pull in web/control_channel.py's own import graph at module
    load time for installs that never authorize a connector at all."""
    from .web.control_channel import request_open_url

    if request_open_url(url):
        return True
    import webbrowser

    return webbrowser.open(url)


@dataclass
class _CallbackResult:
    code: str | None = None
    error: str | None = None


def _make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE with the S256 method."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def run_browser_oauth(
    build_authorize_url: Callable[[str, str, str], str],
    exchange: Callable[[str, str, str], dict[str, Any]],
    port: int,
    path: str = "/callback",
    timeout: float = 180.0,
    open_browser: Callable[[str], bool] | None = None,
    redirect_host: str = "127.0.0.1",
) -> dict[str, Any]:
    """Run a browser-based Authorization Code + PKCE flow via a loopback redirect.

    ``build_authorize_url(redirect_uri, state, code_challenge)`` returns the full
    authorize URL to open in the browser.

    ``exchange(code, redirect_uri, code_verifier)`` trades the authorization code
    for tokens and returns the provider's token response.

    ``redirect_host`` is the hostname used in the redirect_uri sent to the
    provider (default ``127.0.0.1``). Some providers, e.g. Salesforce, require
    HTTPS callback URLs unless the host is the literal string ``localhost``, so
    callers for those providers should pass ``redirect_host="localhost"``. The
    local server itself always binds ``127.0.0.1:port`` — ``localhost``
    resolves there — only for the duration of this call, and is torn down as
    soon as the callback is received (or the timeout expires).

    The authorize URL is always printed, and a failed/no-op ``open_browser``
    (e.g. a headless host with no DISPLAY) does not end the flow -- it keeps
    the local server up and waiting the same as if a browser had opened, so
    a human can still complete it by visiting the printed URL manually
    (typically through an SSH tunnel or SOCKS proxy to this machine's
    loopback interface). ``timeout`` is what eventually gives up if nobody
    does.
    """
    state = secrets.token_urlsafe(24)
    code_verifier, code_challenge = _make_pkce_pair()

    result = _CallbackResult()
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # silence default access log
            pass

        def do_GET(self) -> None:  # noqa: N802 - required stdlib handler method name
            parsed = urlparse(self.path)
            if parsed.path != path:
                self.send_response(404)
                self.end_headers()
                return

            qs = parse_qs(parsed.query)
            got_state = (qs.get("state") or [""])[0]
            error = (qs.get("error_description") or qs.get("error") or [""])[0]
            code = (qs.get("code") or [""])[0]

            if error:
                result.error = error
            elif got_state != state:
                result.error = "state mismatch (possible CSRF) — please retry"
            elif not code:
                result.error = "no authorization code in callback"
            else:
                result.code = code

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_ERROR_HTML if result.error else _SUCCESS_HTML)
            done.set()

    try:
        server = _LoopbackHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise OAuthLoopbackError(
            f"Could not bind 127.0.0.1:{port} for the OAuth redirect — is another "
            f"PrivacyFence sign-in already in progress? ({exc})"
        ) from exc
    # Read back rather than using ``port``: ``port=0`` means the OS picks one.
    redirect_uri = f"http://{redirect_host}:{server.server_port}{path}"

    server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="oauth-loopback")
    server_thread.start()

    try:
        authorize_url = build_authorize_url(redirect_uri, state, code_challenge)
        logger.info("Opening browser for OAuth authorization (redirect_uri=%s)", redirect_uri)
        # Printed unconditionally, and before attempting to open anything --
        # not just when opener() below fails -- so it's there to copy on a
        # headless host (over SSH, no DISPLAY) without needing to wait for
        # that failure first. Matches google-auth-oauthlib's own
        # InstalledAppFlow.run_local_server(), which prints this same kind
        # of line unconditionally rather than only as a failure message.
        print(f"Please visit this URL to authorize PrivacyFence: {authorize_url}")
        opener = open_browser
        if opener is None:
            opener = _default_open_browser
        if not opener(authorize_url):
            # Used to raise here -- but the local server this flow just
            # bound is exactly what a manual visit (e.g. through an SSH
            # tunnel to a headless host) still needs, and killing it in the
            # same breath as telling the person to "visit manually" made
            # that instruction impossible to follow. Keep waiting instead;
            # done.wait() below still times out if nobody ever does.
            logger.warning("Could not open a browser automatically -- waiting for the URL above to be visited manually.")

        if not done.wait(timeout=timeout):
            raise OAuthLoopbackError("Timed out waiting for sign-in to complete in the browser.")
    finally:
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()

    if result.error:
        raise OAuthLoopbackError(result.error)
    # _handle_callback() only ever sets result.error XOR result.code.
    assert result.code is not None  # nosec B101  # invariant narrowing, not input validation

    return exchange(result.code, redirect_uri, code_verifier)
