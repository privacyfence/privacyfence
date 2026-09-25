# Ledger: step 2

## ADR candidates

1. **Audience separation between the MCP token and the browser session.** In local mode the `/mcp`
   bearer token is never accepted on approval/settings routes, and the `pf_session` cookie/CSRF
   token is never accepted on `/mcp`; the two are different secrets checked in different
   middleware. Cited in `src/privacyfence/web/server.py` (`build_app` docstring),
   `tests/unit/web/test_server.py` (`TestAudienceSeparation`) and `mcpb/shim/test/index.test.ts`.
   Meets the bar: a trust boundary, and a middleware reorder silently breaks it. ADR 0011 covers the
   org-mode side only.
2. **Only a companion-attested session may approve, and the gate is on exactly when the install is
   privilege-separated.** A bare `MINT` yields an `unattested` session that may view but not approve
   or change sensitive settings; `MINT COMPANION`/`SHOW <path>` round-trip through the companion to
   attest. Cited in `src/privacyfence/web/server.py` (`require_human_session`),
   `src/privacyfence/web/control_channel.py`, `src/privacyfence/companion.py` (`_open_path`),
   `tests/control_channel_client.py`, `tests/unit/web/test_control_channel.py`
   (`TestAttestedMintCommands`, `TestEveryMintIsAudited`), `tests/unit/web/test_server.py`
   (`TestHumanSessionWiring`). Meets the bar: trust boundary. ADRs 0013, 0031 and 0033 touch it but
   none records the decision itself; step 7 should check before writing one.
3. **The one-time recovery code is delivered only through the companion, never in an HTTP
   response**, and reissuing it always mints a new code after a companion-side confirmation.
   Cited in `src/privacyfence/web/control_channel.py` (module docstring, `send_recovery_code`),
   `src/privacyfence/web/server.py` (`reissue_local_recovery_code`), `src/privacyfence/companion.py`,
   `tests/unit/web/test_server.py` (`TestRecoveryCodeDelivery`), `tests/unit/web/test_control_channel.py`
   (`TestRecoveryCommand`). Meets the bar: trust boundary (a reset token must not be readable by a
   process holding a session). ADR 0027 describes the attack but not this delivery decision.
4. **The web UI's CSP is `default-src 'none'` with per-response nonces for scripts and styles,
   not `'unsafe-inline'`**, plus `object-src`/`frame-src data:` for the PDF preview. Cited in
   `src/privacyfence/web/csp.py`, `src/privacyfence/web/server.py` (`_SecurityHeadersMiddleware`,
   the CSP comment), `tests/integration/test_browser_smoke.py`, `tests/unit/web/test_csp.py`.
   Meets the bar: security posture with a non-obvious rejected alternative (a blanket
   `'unsafe-inline'` grant).
5. **Browser notifications stay on the machine (title badge and a local service-worker
   notification only, no push/VAPID), with a per-level content allowlist** (`minimal`: count only;
   `standard`: connector and direction; `detailed`: the row's summary). Cited in
   `src/privacyfence/web_shell.py` (`_STREAM_JS` comment, `notificationBody()`) and
   `tests/unit/test_web_shell.py` (`TestNotifications`). Meets the bar: privacy boundary (what
   gated content may appear in an OS notification).
6. **Minting a sign-in code goes over a Unix socket/named pipe, not a persistent secret presented
   over loopback HTTP; the browser only ever exchanges a single-use bootstrap code for a cookie.**
   Cited in `src/privacyfence/web/control_channel.py` (module docstring) and
   `src/privacyfence/web/server.py` (module docstring, `_BootstrapMiddleware`). Probably already
   covered by ADR 0002 (the companion as the sign-in channel, "a chain doesn't get stronger when you
   strengthen its middle"); step 7 should confirm and, if so, cite ADR 0002 instead of writing one.

## Changed user-visible strings

- `mcpb/shim/src/daemon.ts`: the shim's "Daemon not running" wait message on a separated install
  drops `(#428 Phase 4)`: "… this install runs it as <manager> under its own account, so waiting for
  the service …". `mcpb/shim/test/daemon.test.ts` now finds the line by "under its own account".
  Shown in Claude Desktop's MCP log only.

## Cross-slice edits needed

None. `git grep` finds no test outside this slice that asserts on the changed wait message.

## Bugs noticed

None. One leftover that is not a bug and was left alone under rule 7: `TestSettingsPageRendering` in
`tests/integration/test_browser_smoke.py` still writes its screenshots to
`test-results/psc5-settings-screenshots/`. That directory name carries an old plan tag the guard
does not match; renaming it changes where CI finds the screenshots, so it is out of scope here.

## Open issues kept as URLs

None. Every issue reference in this slice was removed. All of them were closed as completed when
checked: #396, #400, #423, #426, #428, #431, #576 and #579.

## Allowlist entries added

- `("web/routes_connect.py", "#555")` and `("web/routes_connect.py", "#888")`: CSS hex colours in the
  connect page's stylesheet.
