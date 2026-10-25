# ADR 0178: A sign-in or connect flow is finished in the session that started it

## Status

Accepted — 2026-10-14. Implemented.

## Context

Two org-mode browser flows ended at a callback that did not check who finished them. `/connect`'s
provider callback resolved the principal from the single-use `state` alone, on purpose, so a
consent link opened by someone else saved the provider's token into the starter's account (SEC-B2).
`/login` set no cookie, so the callback issued a session to whichever browser presented the
`state`: a login link handed to a victim signed the victim in as the attacker (SEC-B6). The
10-minute and 5-minute attempt TTLs were declared but not checked on `pop`.

## Decision

- **The provider callback needs the starting session.** `routes_connect.callback` requires
  `org_session.authenticated(...)` to return the principal that started the attempt. Otherwise it
  logs at info, redirects to `/connect?error=<service>`, and saves nothing. The `state` is burnt
  by the attempt, whatever the outcome.
- **The sign-in is bound to its browser.** `/login` sets `pf_org_login` (HttpOnly, Secure,
  SameSite=Lax, path `/oauth/idp/login-callback`) holding the SHA-256 of the `state`.
  `login_callback` pops the attempt and then requires the cookie to match
  (`hmac.compare_digest`); a mismatch is the same 400 as an unknown state, with no session. On
  success the session cookie is set first and the binding cookie is deleted after it, so the
  session cookie stays the first `Set-Cookie` header (the release smoke test reads it).
- **Attempts expire.** The `/connect` pending store, the login attempt store and the IdP step-up
  attempt store all return nothing for an entry older than their TTL.

## Alternatives considered

- **Bind `/connect`'s state to its own cookie.** Rejected: the signed-in session already
  identifies the principal, so a second cookie adds nothing.
- **Keep resolving the principal from `state` alone.** Rejected: that is the hole; the earlier
  comment calling it the better design is gone from `routes_connect.py` and `org_session.py`.

## Consequences

- A connect link cannot be handed to a colleague; they must start it in their own session.
- The binding cookie is one slot per browser, so of two `/login` tabs opened at once only the
  later one completes. The other gets the 400 and the person signs in again.
- A person whose session expired between starting a connect and returning from the provider is
  sent to `/connect?error=<service>` and starts again.

## Verification

- `web/routes_connect.py` (`callback`, `_PendingAuthStore.pop`), `web/routes_org_identity.py`
  (`LOGIN_BINDING_COOKIE`, `login`, `login_callback`), `web/routes_org_stepup.py`
  (`_StepUpAuthAttemptStore.pop`).
- `tests/unit/web/test_routes_connect.py` (`different_signed_in_principal`, `no_session_is_rejected`,
  `after_ttl`), `test_routes_org_identity.py` (`starting_browser_cookie`, `another_attempts_cookie`,
  `binding_cookie`, `after_ttl`), `tests/integration/test_org_ubuntu_release_smoke.py`.

## Related

- ADR [0011](0011-org-mode-runs-its-own-oauth-authorization-server.md)
