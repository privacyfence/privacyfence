"""Shared Google OAuth 2.0 helper for org mode's server-redirect flow (P8).

Local mode keeps using ``google-auth-oauthlib``'s own ``InstalledAppFlow``
loopback implementation directly -- each of gmail_client.py/drive_client.py/
calendar_client.py/contacts_client.py/tasks_client.py's own
``authorize_interactive()`` is unchanged by this module and calls
``flow.run_local_server(port=0)`` exactly as before. This module is
additive, used only by ``web/routes_connect.py``'s org-mode routes, where a
remote browser (a phone, say) can't have PrivacyFence open a local port and
a local browser window on its own behalf -- see ``oauth_loopback.py``'s own
module docstring for why that assumption breaks down.

``google_auth_oauthlib.flow.Flow`` is the lower-level counterpart of
``InstalledAppFlow`` that takes an explicit ``redirect_uri`` instead of
managing a loopback listener itself -- exactly the plan document's own
words for this phase ("Google's InstalledAppFlow becomes google_auth_
oauthlib.flow.Flow with an explicit redirect_uri"). ``Flow`` auto-generates
its own PKCE ``code_verifier``/``code_challenge`` pair (see its own
``authorization_url()``), so unlike the Slack/Salesforce/Atlassian helpers
this module has no ``code_challenge`` parameter of its own to plumb through
-- callers just need to persist ``Flow.code_verifier`` between the "start"
and "callback" requests (two separate HTTP requests, and therefore two
separate ``Flow`` instances) and pass it back in on the second one.

Google Cloud Console OAuth clients are typed at creation ("Desktop app" vs.
"Web application"); only a "Web application" client can have an arbitrary
HTTPS redirect URI registered against it. An org running ``mode: org``
needs its ``org_config.json`` "google" section to hold a *Web application*
OAuth client's credentials (registered with ``{issuer_url}/oauth/callback/
<service>`` for each Google connector), separate from whatever "Desktop
app" client an install might otherwise use for local mode's loopback flow
-- ``_web_client_config`` below wraps the same flat "google" section
daemon_main.py's own ``_google_client_config`` reads, just under the "web"
top-level key ``Flow.from_client_config`` needs instead of "installed".
"""
from __future__ import annotations

import logging
from typing import Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from .secure_files import atomic_write_text

logger = logging.getLogger(__name__)


class GoogleOAuthError(Exception):
    """Raised for unrecoverable problems in the org-mode server-redirect flow."""


def web_client_config(google_org: dict[str, Any]) -> dict[str, Any]:
    """Wraps ``org_config.json``'s flat "google" section into the "web"
    client-config shape ``Flow.from_client_config`` requires (see module
    docstring). Returns ``{}`` if the required fields aren't present --
    same "connector skipped, not fatal" posture every other missing-config
    check in this codebase takes."""
    if not google_org.get("client_id") or not google_org.get("client_secret"):
        return {}
    if not google_org.get("auth_uri") or not google_org.get("token_uri"):
        return {}
    return {"web": google_org}


def build_flow(client_config: dict[str, Any], scopes: list[str], redirect_uri: str) -> Flow:
    return Flow.from_client_config(client_config, scopes=scopes, redirect_uri=redirect_uri)


def authorize_url(client_config: dict[str, Any], scopes: list[str], redirect_uri: str, state: str) -> tuple[str, str]:
    """Returns ``(authorize_url, code_verifier)`` -- the caller must persist
    ``code_verifier`` (keyed by ``state``) and hand it back to
    ``exchange_code`` below on the matching callback request."""
    flow = build_flow(client_config, scopes, redirect_uri)
    # No ``include_granted_scopes``. That is Google's incremental
    # authorization: it asks Google to issue a grant covering every scope
    # this OAuth client already holds for this user, which is the opposite
    # of what web/routes_connect.py is built around -- five separate
    # authorize buttons because "each is a distinct OAuth grant with its own
    # scopes and its own token file". It also broke every exchange it
    # touched: the token then comes back carrying that accumulated union,
    # oauthlib compares it against what was requested, and raises "Scope has
    # changed from ... to ..." instead of returning credentials. Authorizing
    # a second Google connector failed for that reason alone; so did the
    # first, whenever the same client also served org mode's OIDC sign-in
    # (its openid/userinfo.* scopes are in the union too).
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state)
    # Flow.authorization_url() always sets it (autogenerate_code_verifier=True).
    assert flow.code_verifier is not None  # nosec B101  # invariant narrowing, not input validation
    return url, flow.code_verifier


def _accept_granted_superset(flow: Flow, exc: Warning, *, requested: list[str]) -> None:
    """Recover from oauthlib's "Scope has changed" refusal when Google
    granted a *superset* of what was requested.

    RFC 6749 §3.3 requires an authorization server to report the scope it
    actually granted when it differs from the request; it does not make a
    wider grant an error. oauthlib refuses anyway unless the process-wide
    ``OAUTHLIB_RELAX_TOKEN_SCOPE`` is set -- and that env var is both a
    blunt instrument (it silences this for every OAuth exchange in the
    process, local mode's included) and unsafe to toggle around a single
    call, since two concurrent authorizations would race on it.

    So the check is done here instead, keeping the half of it that is
    genuinely worth having: a grant *missing* something that was asked for
    is still a hard failure, because the connector built on it would
    otherwise fail later with a far less obvious permission error.

    A wider grant still reaches Google legitimately -- most often because
    one OAuth client serves both org mode's OIDC sign-in and its connectors,
    so ``openid``/``userinfo.*`` ride along on every connector exchange.
    That is a deployment's choice to make (see docs/org-mode-setup-guide.md
    §4.2), not something this function should reject.

    ``exc`` carries the already-parsed token oauthlib refused to return
    (``exc.token``) and the granted scopes (``exc.new_scope``), so nothing
    has to be re-fetched; assigning through ``OAuth2Session.token``'s own
    setter is what ``fetch_token`` would have done, and is what makes
    ``Flow.credentials`` constructible afterwards. The resulting
    ``Credentials`` records the requested scopes as ``scopes`` and the wider
    grant as ``granted_scopes``, which is exactly the distinction
    ``credentials_from_session`` keeps those two fields for.
    """
    token = getattr(exc, "token", None)
    granted = set(getattr(exc, "new_scope", None) or ())
    if not token or not granted:  # not oauthlib's scope-change Warning after all
        raise GoogleOAuthError(f"Google OAuth exchange failed: {exc}") from exc
    missing = sorted(set(requested) - granted)
    if missing:
        raise GoogleOAuthError(
            f"Google OAuth exchange failed: the granted scopes are missing {missing}"
        ) from exc
    logger.info(
        "Google granted a wider scope than requested (extra: %s) -- accepting",
        sorted(granted - set(requested)),
    )
    flow.oauth2session.token = token


def exchange_code(
    client_config: dict[str, Any], scopes: list[str], redirect_uri: str, code: str, code_verifier: str,
) -> Credentials:
    """Exchanges an authorization code for Google credentials. Raises
    ``GoogleOAuthError`` on failure -- ``Flow.fetch_token`` itself raises
    whatever ``requests_oauthlib``/``oauthlib`` raise for a rejected
    exchange (an expired/reused code, a redirect_uri mismatch, ...), which
    isn't a stable, user-presentable type on its own.

    A granted scope wider than the requested one is not a failure -- see
    ``_accept_granted_superset``, which is why the ``Warning`` arm below is
    caught ahead of the general one (oauthlib raises the builtin
    ``Warning``, an ordinary ``Exception`` subclass, for exactly that case).
    """
    flow = Flow.from_client_config(
        client_config, scopes=scopes, redirect_uri=redirect_uri,
        code_verifier=code_verifier, autogenerate_code_verifier=False,
    )
    try:
        flow.fetch_token(code=code)
    except Warning as exc:
        _accept_granted_superset(flow, exc, requested=scopes)
    except Exception as exc:  # noqa: BLE001 -- any provider-side failure ends the same way
        raise GoogleOAuthError(f"Google OAuth exchange failed: {exc}") from exc
    return flow.credentials


def save_credentials(token_file: str, creds: Credentials) -> None:
    """Same file format ``GmailClient._save_token``/etc. write and
    ``Credentials.from_authorized_user_file`` reads back -- a token
    obtained through this module's server-redirect flow is indistinguishable
    on disk from one obtained through the local-mode loopback flow."""
    atomic_write_text(token_file, creds.to_json())


__all__ = [
    "GoogleOAuthError",
    "authorize_url",
    "build_flow",
    "exchange_code",
    "save_credentials",
    "web_client_config",
]
