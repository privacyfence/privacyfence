# Web and organization-mode hardening plan

Plan 2 of 4 made from the whole-repository review of 2026-10-14
([`docs/repo-2026-10-14-review.md` at c8416d94](https://github.com/privacyfence/privacyfence/blob/c8416d94981ac6eb776db7be8ee72de4af56f12a/docs/repo-2026-10-14-review.md),
reviewed commit `3766d50f`, still `main`'s tip). The other plans are `plan/approval-core-hardening`,
`plan/platform-and-ci-hardening` and `plan/docs-and-website-accuracy`. Finding ids are the review's;
its evidence is there.

## 1. Goal

Close the browser-flow and embedding holes an outsider or a hostile AI client can use against an
organization deployment or a local install:

- an org-mode MCP client can no longer get a token for a member who never saw it (SEC-B1);
- a provider grant or a sign-in started in one browser can no longer be finished in another person's
  (SEC-B2, SEC-B6);
- an agent-supplied link can no longer start an IdP step-up that releases an approval (SEC-B4);
- connector- and client-controlled strings can no longer break out of the settings page's or the
  approval card's markup (SEC-B3, SEC-B8);
- a second OS user's session no longer receives the owner's settings stream, the bootstrap link is
  no longer an open redirect, and one MCP session no longer gets another's in-flight result
  (SEC-B5, SEC-B7, SEC-A6);
- an `.xlsx` zip bomb is refused before it is parsed, a paged Salesforce report id is validated, and
  Jira and Salesforce CSV exports neutralise formula cells (SEC-C2, SEC-C3, SEC-C4);
- `privacyfence-sync` hides a `--token-env` token from scripts and refuses a download path outside
  its work folder (SEC-D7, SEC-D8);

each with the tests that pin it.

Findings in this plan: **SEC-B1, SEC-B2, SEC-B3, SEC-B4, SEC-B5, SEC-B6, SEC-B7, SEC-B8, SEC-A6,
SEC-C2, SEC-C3, SEC-C4, SEC-D7, SEC-D8, TST-B1, TST-B2, TST-B3, TST-B5, TST-B6, TST-B7, TST-B8,
TST-B9, TST-B10 (merged into SEC-C4), TST-B11, TST-B12, TST-B13, TST-B14, TST-B20.**

## 2. Current state

- **MCP authorization (org mode).** `web/oauth_provider.py:406-429` `authorize()` stores a
  `_PendingAuthorization(client_id, params, idp_nonce, idp_code_verifier, created_at)` in
  `self._pending` (in memory, `_PENDING_AUTHORIZATION_TTL_SECONDS = 5*60`, pruned only inside
  `authorize()` by `_prune_pending`, `:431-439`) and redirects to the IdP.
  `handle_idp_callback` (`:441-479`) pops the entry with no TTL check (`:453-454`), verifies the ID
  token, runs `check_authz_policy`, mints a code and returns
  `construct_redirect_uri(pending.params.redirect_uri, code=..., state=...)`. Nobody is shown the
  client. The route is `routes_mcp.py:1061-1077` (`idp_callback` inside `mount_org_oauth`, which
  also builds `/register`, unauthenticated by design). Client registrations are on disk
  (`org_dir()/oauth_clients.json`); `provider.client_name(client_id)` (`:345`) reads a name without
  touching `last_used_at`. `agent_identity.sanitize_client_string` (`:81`) strips control and bidi
  characters and truncates. `paths.user_dir(principal)` (`paths.py:211`) is a principal's own
  directory; `secure_files.atomic_write_json` (`:224`) writes JSON safely.
- **Pages and CSRF in org mode.** `org_session.authenticated(request, store)` (`:123`),
  `check_csrf(request, csrf)` (`:166`, double submit against the `pf_org_session` cookie),
  `check_origin(request)` (`:182`), `set_session_cookie` (`:130`, HttpOnly, Secure, SameSite=Lax).
  `web_shell.plain_page(body_html, *, title, nonce, head_html="", page_css="")` (`:783`) renders a
  shell-less page; `csp.nonce_for(request)` (`:57`). Org mode's CSP is `form-action 'self'`
  (`csp.py:85-137`), and browsers check it on every redirect a form submission follows, so a native
  form POST cannot end on another origin.
- **`/connect` callback.** `routes_connect.py:483-515` never reads the session: the docstring at
  `:484-487` says so on purpose. `_PendingAuthStore.pop` (`:218-242`) does not check the 10-minute
  TTL. The Telegram POST routes (`:546-648`) call `_check_form_post` (`:539-544`); only `start`'s
  CSRF rejection is tested (`tests/unit/web/test_routes_connect.py:484-490`), and
  `test_happy_path_saves_token_evicts_registry_and_needs_no_session_cookie` (`:336-368`, in
  `TestOAuthCallback` at `:314`) locks in the cookie-less callback.
- **`/login`.** `routes_org_identity.py:174-216`: `login` sets no cookie; `login_callback` pops the
  state and issues a session to whichever browser presents it.
- **IdP step-up.** `routes_org_stepup.py:91-132` `stepup_idp_start` is a GET reading `result`/`choice`
  from the query, checking only `step_up.require_passkey`; `_StepUpAuthAttemptStore.pop` has no TTL
  check. The card's 428 body carries
  `idp_stepup_url = "/api/approvals/<id>/stepup/idp?result=...&choice=..."`
  (`routes_approvals.py:201-235`), and `_org_bridge_shim` (`:324-400`) writes it into an `<a href>`.
  `StepUpConfig` (`step_up_config.py:222`) has `enabled`, `require_passkey`.
- **JSON in `<script>`.** `settings_window_html.py:2314-2335` embeds `state_json`, `caps_json`,
  `__pfAiClient` and `__pfInitialSection` unescaped (only `scoped_json`, `:2325`, replaces `<`).
  `approval_list_html.py:1379-1385` and `web_shell.py:706-714` substitute `json.dumps` values into
  scripts unescaped. No shared helper exists. `tests/unit/test_settings_window_html.py:90-93`'s
  non-greedy regex makes `:152-160` unable to fail. The JS `esc()` functions
  (`approval_list_html.py:349-352`, `settings_window_html.py:368-372`) feed `innerHTML` and have no
  DOM-level test.
- **Image data URIs.** `card_builder.py:173-176` builds `data:{preview_mime_type};base64,...` from a
  sender-controlled MIME type checked only for the `image/` prefix; `approval_window_html.py:450,
  467, 475, 1129, 1160, 1167` interpolate `src=` values unescaped (the file imports
  `html.escape as _html_escape` at `:69`).
- **Local web.** `web/server.py:913-940` `_state_stream_route` checks only `sessions.touch`; the
  stream (`web/state_stream.py`) sends `settings` events to every subscriber (`:132-158`,
  `:182-184`), wired at `server.py:1519-1538` with an unfiltered `settings_snapshot`. The
  `list_pending` lambda is principal-filtered, but no test exercises the server's wiring
  (`tests/unit/web/test_server.py:1322` builds its own `StateStream`). `server.py:899`
  `RedirectResponse(request.url.path, 303)` keeps a leading `//`.
  `web/mcp_dispatch.py:306-308` `_dedupe_key` has no session or agent; `:266-270` (the
  in-flight cancellation cleanup) is uncovered.
- **Extraction.** `text_extraction.py:404-415` hands `.xlsx` bytes to `openpyxl.load_workbook` with
  no bound on `xl/sharedStrings.xml`; DOCX/PPTX go through `_read_zip_member_bounded` (`:231`,
  `_MAX_ZIP_ENTRY_BYTES = 10 MiB`, `:77`). `extract_text` runs synchronously on the event loop at
  `gmail.py:1169`, `confluence.py:622`, `drive.py:1446, 1735, 1759, 1780`.
- **Exports.** `export_files.render(columns, rows, fmt)` (`:81-92`) writes cells as is;
  `audit_log._FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t", "\r")` (`:522`) is the audit
  export's list. Callers: `jira.py:759`, `salesforce.py:730`, `grist.py:873/961`.
  `salesforce_client.run_report_page` (`:808-828`) does not call `_validate_salesforce_id`;
  `run_report` does (`:791-794`).
- **Sync client.** `sync/src/privacyfence_sync/transform.py:23-24` hides only `PRIVACYFENCE_*`;
  `cli.py:62-63` reads the token with `os.environ.get(token_env)`. `runner.py:194-205`
  `_receive_file(step, result, dest, client)` moves whatever `result["path"]` names; callers at
  `:125` and `:133`.

## 3. Design

### 3.1 MCP consent page (SEC-B1, TST-B8, TST-B12 part)

New module `src/privacyfence/web/oauth_consent.py` (module docstring: the page an org member sees
before an MCP client they have not used before gets a code for them; ADR 0177). It holds the page
renderers and the consent record; `OrgOAuthProvider` holds the pending consents.

**Remembered consent.** `ConsentStore` with
`has(principal: Principal, client_id: str, redirect_uri: str) -> bool` and
`record(principal, client_id, redirect_uri) -> None`, backed by
`paths.user_dir(principal) / "oauth_consents.json"` holding
`{"clients": {client_id: {redirect_uri: consented_at_epoch_float}}}` (keyed by redirect URI too: a
client may register several, and the person allowed the one the page showed), written with
`secure_files.atomic_write_json`. A missing or malformed file reads as "no consent" (fail closed,
logged at warning for malformed).

**Provider changes** (`oauth_provider.py`):

- Constants `CONSENT_PATH = "/oauth/consent"` and `_PENDING_CONSENT_TTL_SECONDS = 5 * 60`; a
  dataclass `_PendingConsent(client_id: str, principal: Principal, params: AuthorizationParams,
  csrf: str, created_at: float)`; `self._pending_consents: dict[str, _PendingConsent]` capped by
  `_MAX_PENDING_AUTHORIZATIONS` (raise the same `temporarily_unavailable` style error via
  `ValueError("too many pending consents")` from `handle_idp_callback` when full); pruned in
  `_prune_pending` with the same TTL logic; `self._consents = ConsentStore()`.
- `handle_idp_callback`: after the pop, if `time.time() - pending.created_at >
  _PENDING_AUTHORIZATION_TTL_SECONDS`, raise `ValueError("invalid or expired authorization attempt")`
  (TST-B12). After `check_authz_policy`: if `self._consents.has(principal, pending.client_id,
  str(pending.params.redirect_uri))`, mint
  the code and return the client redirect exactly as today. Otherwise store a `_PendingConsent`
  under `consent_id = secrets.token_urlsafe(32)` with `csrf = secrets.token_urlsafe(32)` and return
  `f"{CONSENT_PATH}/{consent_id}"`. Factor the code minting into `_issue_code(client_id, principal,
  params) -> str` returning the client redirect URL, used by both paths.
- `consent_view(consent_id) -> ConsentView | None` (a frozen dataclass in `oauth_consent.py`:
  `client_name: str`, `redirect_host: str`, `principal_label: str`, `csrf: str`): None when missing
  or past the TTL. `client_name` is `sanitize_client_string(self.client_name(client_id) or "")` or,
  when empty, `"An unnamed AI client"`. `redirect_host` is `urlsplit(str(params.redirect_uri)).netloc`
  or, when empty (a custom scheme), `urlsplit(...).scheme + ":"`. `principal_label` is the
  principal's email when it has one, else its id.
- `decide_consent(consent_id, *, csrf: str, allow: bool) -> str`: pop the pending consent
  (single-use); raise `ValueError("invalid or expired consent")` when missing, past the TTL, or
  `not hmac.compare_digest(csrf, pending.csrf)`. On allow: `self._consents.record(...)` then
  `return self._issue_code(...)`. On deny: return
  `construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)`.

**Routes** (in `mount_org_oauth`, `routes_mcp.py`): `Route(f"{CONSENT_PATH}/{{consent_id}}",
consent_page, methods=["GET"])` and the same path with `methods=["POST"]` → `consent_post`.

- `consent_page`: `view = provider.consent_view(id)`; None → `PlainTextResponse("This sign-in has
  expired. Start again from your AI client.", 400)`. Otherwise `HTMLResponse(render_consent_page(view,
  consent_id=id, nonce=nonce_for(request)), headers={"Cache-Control": "no-store"})`.
- `consent_post`: `if not org_session.check_origin(request): 403 "Cross-origin request rejected."`;
  read the form (`csrf`, `decision`); `decision` must be `"allow"` or `"deny"` (else 400); call
  `decide_consent`; a `ValueError` → the same 400 text as above; success →
  `HTMLResponse(render_continue_page(redirect_url, redirect_host=..., nonce=...))` with no-store.
  A 200 page, not a redirect: org mode's `form-action 'self'` would block a form POST that
  redirects to the client's origin.

**Strings** (exact):

- Consent page title `Allow access?`; heading `Allow {client_name} to use PrivacyFence as
  {principal_label}?`; paragraph `It will be able to call your PrivacyFence tools. Anything your
  rules don't allow still needs your approval on a card.`; paragraph `You'll return to
  {redirect_host}.`; a `<form method="post">` with hidden `csrf`, and two buttons
  `name="decision"`: `value="allow"` labelled `Allow`, `value="deny"` labelled `Don't allow`.
- Continue page title `Returning to your AI client`; `head_html='<meta http-equiv="refresh"
  content="0;url={escaped redirect}">'`; body `Returning to {redirect_host}…` and a link `Continue`
  to the same URL.
- Every interpolated value goes through `html.escape(..., quote=True)`. Use `web_shell.plain_page`.

**Popup opener (ADR 0108).** `web/server.py`'s `_SecurityHeadersMiddleware` sends
`Cross-Origin-Opener-Policy: unsafe-none` only for `_OAUTH_POPUP_PATHS` (`:553`, used at `:634-636`);
every other response gets `same-origin`, which nulls a sign-in popup's `window.opener` and breaks a
client that hands the code back through it (Gemini Enterprise). The consent page and its continue
page are now in that chain, so the header becomes
`"unsafe-none" if path in _OAUTH_POPUP_PATHS or path.startswith(CONSENT_PATH + "/") else "same-origin"`,
and the comment above `_OAUTH_POPUP_PATHS` says the consent pages render a document but must keep
the opener. ADR 0177 records that it amends ADR 0108.

Rejected: **binding the consent to a browser cookie** (the consent id exists only in the browser
that completed the IdP sign-in, and the attacker cannot complete it); **an admin allow-list of
redirect hosts** (optional in the review, and a configuration surface nobody has asked for);
**a JS `fetch` + navigate** instead of the continue page (adds script to a page that needs none).

### 3.2 Callbacks bound to the browser that started them (SEC-B2, SEC-B6, TST-B5, TST-B12, TST-B13)

- `routes_connect.callback`: before any exchange, `signed_in = org_session.authenticated(request,
  sessions)`; if `signed_in is None or signed_in.id != pending.principal_id`, log at info
  (`"OAuth callback for %s ignored: not finished by the principal that started it"`, service) and
  redirect 302 to `f"/connect?error={pending.service}"` with no-store; nothing is saved. Replace the
  docstring sentence at `:484-487` with: the callback must be finished in the same signed-in
  session that started it, so a consent link cannot be handed to a colleague (ADR 0178). Also rewrite the module
  docstring paragraph at `routes_connect.py:24-46` (it calls resolving the principal from `state`
  alone the better design) and the last paragraph of `org_session.set_session_cookie`'s comment
  (`org_session.py:152-157`), which say the opposite of this.
- `_PendingAuthStore.pop`: return None when the entry is older than `self._ttl` (and delete it).
- `routes_org_identity`: constant `LOGIN_BINDING_COOKIE = "pf_org_login"`. `login` sets it on its
  302 to the IdP: value `hashlib.sha256(state.encode()).hexdigest()`, `httponly=True, secure=True,
  samesite="lax", path=LOGIN_CALLBACK_PATH, max_age=_LOGIN_ATTEMPT_TTL_SECONDS`. `login_callback`:
  pop the attempt first (the state is burnt either way), then require
  `hmac.compare_digest(request.cookies.get(LOGIN_BINDING_COOKIE, ""), sha256(state))`; on mismatch
  return the same 400 the callback already returns for an unknown state, with no session. On
  success, call `org_session.set_session_cookie(...)` first and then
  `response.delete_cookie(LOGIN_BINDING_COOKIE, path=LOGIN_CALLBACK_PATH)`, so the session cookie
  stays the first `Set-Cookie` header (the release smoke test reads the first one). Accepted
  consequence: the binding cookie is one slot, so of two `/login` tabs opened at once only the later
  one completes.
  `_LoginAttemptStore.pop` also returns None past the TTL.
- `routes_org_stepup._StepUpAuthAttemptStore.pop`: return None past the TTL.

### 3.3 IdP step-up starts only from the card, by POST (SEC-B4, TST-B7)

- `routes_org_stepup.stepup_idp_start` becomes POST-only (`Route(..., methods=["POST"])`, so a GET
  is 405). Order of checks: no session → 401 JSON `{"error": "signed_out"}`; `not step_up.enabled`
  → 404; `not org_session.check_origin(request)` → 403; body is JSON `{"result", "choice", "csrf"}`
  (a non-JSON body → 400); `not org_session.check_csrf(request, body.get("csrf"))` → 403;
  `step_up.require_passkey` → 403 (as today); `result` not in `_STEP_UP_RESULTS` → 400;
  `raw = body.get("choice"); choice = raw if isinstance(raw, int) and not isinstance(raw, bool) else None`. Then the
  existing attempt creation, and instead of a 302 return `JSONResponse({"redirect": <idp url>})`
  with no-store.
- `routes_approvals._org_step_up_response`: `idp_stepup_url` becomes
  `f"/api/approvals/{quote(approval_id)}/stepup/idp"` (no query string).
- `_org_bridge_shim`: in both places that write the link (`:362-364` and `:371-373`), write
  `'This approval needs extra verification. <button type="button" id="pf-idp-stepup">Verify by signing in again</button>'`
  and then attach a click listener (`document.getElementById('pf-idp-stepup').addEventListener('click', ...)`;
  no inline handler, CSP forbids it) that POSTs
  `JSON.stringify({result: payload.result, choice: payload.choice == null ? null : payload.choice, csrf: <csrf>})` with
  `credentials:'same-origin'` and `Content-Type: application/json` to `data.idp_stepup_url`, and on
  a 200 reads `{redirect}` and calls `window.location.assign(redirect)`; otherwise writes
  `_FAILED_MESSAGE`. Factor this into one JS function `pfIdpStepUp(url, payload)` inside the shim.
- The docstrings at `routes_approvals.py:36`, `:76` and `:977` that say `GET .../stepup/idp` say
  POST.

### 3.4 JSON in scripts and attribute escaping (SEC-B3, SEC-B8, TST-B1, TST-B2)

- New `src/privacyfence/html_embed.py` (docstring: one serializer for every value embedded in a
  `<script>` element) with
  `def json_for_script(value: Any) -> str` = `json.dumps(value)` followed by replacing each
  `<` with the six characters `<`, `>` with `>`, `&` with `&`, the character
  U+2028 with ` ` and U+2029 with ` ` (JSON escapes, so `JSON.parse` and JS literals
  read the same value; written in Python as `.replace("<", "\\u003c")` and so on, as
  `settings_window_html.py:2325` already does for `<`).
- Use it for: `settings_window_html.py` `state_json`, `caps_json`, the inline `ai_client` and
  `initial_section` dumps, and `scoped_json` (replacing its hand `.replace`); `approval_list_html.py`
  `:1379-1385` (all six values); `web_shell.py:706-714` (all four `json.dumps` values).
- `card_builder.py`: validate `preview_mime_type` with `re.fullmatch(r"image/[a-z0-9.+-]+",
  preview_mime_type.lower())`; a mismatch renders no image (as a non-image MIME type does today).
- `approval_window_html.py`: wrap every `src=` value at `:450, 467, 475, 1129, 1160, 1167` in
  `_html_escape(..., quote=True)`.

### 3.5 Local web (SEC-B5, SEC-B7, SEC-A6, TST-B3, TST-B6, TST-B14, TST-B20)

- `StateStream.subscribe(self, is_disconnected, touch=None, *, receives_settings: bool)` (keyword,
  required). `self._subscribers` becomes `dict[asyncio.Queue, bool]` (queue → receives_settings).
  The initial `settings` snapshot and every `_broadcast("settings", ...)` go only to queues whose
  flag is True; approvals events and `_CLOSED_EVENT` go to all. `_state_stream_route` computes
  `receives_settings = current_principal().id == LOCAL_PRINCIPAL.id` in the handler (before
  building the response) and passes it.
- `server.py:899`: `RedirectResponse("/" + request.url.path.lstrip("/\\"), status_code=303)`.
- `McpDispatcher._dedupe_key(principal_id, scope_id, session_key, agent_id, connector_name, tool,
  args)` includes `str(session_key)` and `current_agent().id` (import from `..agent_identity`).
  The retry dedupe is for one client retrying its own call; it never crosses MCP sessions.

### 3.6 Extraction, report ids and exports (SEC-C2, SEC-C3, SEC-C4, TST-B9, TST-B10, TST-B11)

- `text_extraction.py`: constant `_XLSX_MAX_TOTAL_BYTES = 4 * _MAX_ZIP_ENTRY_BYTES` and
  `_PPTX_MAX_SLIDES = 500`. In `_extract_xlsx_markdown`, before `openpyxl.load_workbook`, open the
  zip and read **every** member with `_read_zip_member_bounded` (which already enforces the
  per-member cap; openpyxl also parses `xl/styles.xml`, `[Content_Types].xml` and the `_rels`
  parts, so no member is exempt); sum the lengths; if any read hits the cap or the sum exceeds
  `_XLSX_MAX_TOTAL_BYTES`, return `""` (log at warning: `"Refusing to parse %s: decompressed size over the cap"`).
  In `_extract_pptx_markdown`, stop after `_PPTX_MAX_SLIDES` slides with the footer
  `f"*(showing the first {_PPTX_MAX_SLIDES} slides of {total})*"`.
- The six `extract_text(...)` call sites in `gmail.py`, `confluence.py`, `drive.py` become
  `await asyncio.to_thread(extract_text, ...)`.
- `salesforce_client.run_report_page`: right after its existing empty-`report_id` check,
  `report_id = _validate_salesforce_id(report_id, "report_id")`.
- `export_files.render(columns, rows, fmt, *, spreadsheet_safe: bool = False)`: when `fmt == "csv"`
  and `spreadsheet_safe`, a `str` cell whose first character is in `FORMULA_TRIGGER_CHARS` is
  written with a leading `'`. `FORMULA_TRIGGER_CHARS` is imported from `audit_log`
  (`from .audit_log import _FORMULA_TRIGGER_CHARS as FORMULA_TRIGGER_CHARS`, one list for every
  spreadsheet-bound export). `jira.py:759` and `salesforce.py:730` pass `spreadsheet_safe=True`
  (`asyncio.to_thread(export_files.render, columns, rows, fmt, spreadsheet_safe=True)`); Grist's
  calls stay as they are, because Grist exports must round-trip through `grist_update_csv` (the
  user's decision, recorded in ADR 0180). A numeric-looking string such as `"-5"` from Jira or
  Salesforce is written as `'-5`; ADR 0180 records that consequence.

### 3.7 Sync client (SEC-D7, SEC-D8)

- `cli._connect`: `token = os.environ.pop(token_env, None)`, so the variable is gone from the
  environment every script step inherits. Comment: the scripts this process runs must never see
  the bearer token, whatever variable carried it.
- `runner._receive_file(step, result, dest, client, work_dir: Path)`: for the whole first branch
  (`delivery == "local_disk"`, or no delivery and a `path`, which Drive downloads use),
  `named = Path(result["path"])`; refuse with `SyncError(f"step {step.id}: the server named a file
  outside the work folder")` when `named.is_symlink()` or
  `not named.resolve().is_relative_to(work_dir.resolve())`. The two callers pass `work_dir`.
- The whole bullet at `sync/README.md:372-375` becomes: "The script runs in the work folder and gets your
  environment without any variable whose name starts with `PRIVACYFENCE_`, and without the
  variable `--token-env` names, so it never sees the token. It runs as you, so it can read
  anything you can, including the stored sign-in file." Keep no sentence that says the script "has no PrivacyFence access". The whole
  file is copied byte for byte to
  `plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md`
  (`tests/unit/test_sync_plugin.py:25-26` checks it).

## 4. ADRs

- **0177** — An org-mode MCP client gets a code for a person only after that person allows it on a
  consent page, remembered per person, client and redirect URI. Amends 0108 (the consent pages keep
  the sign-in popup's opener). (q9)
- **0178** — An org-mode sign-in or provider-connect flow must be finished in the browser session
  that started it. (q9)
- **0179** — An IdP step-up for an approval starts only from the card, by a CSRF-checked POST.
  (q9)
- **0180** — Jira and Salesforce CSV exports neutralise formula-leading cells; Grist exports do not,
  so they round-trip. Amends 0155. (q9)

Plan 1 takes 0175-0176; plan 3 takes 0181-0185. Renumber per `docs/adr/README.md` rule 6 if a
number is taken first.

## 5. Manual steps

None before. One after, on the [step-by-step page](https://claude.ai/artifact/KRGXYAj6wbbtaByC3jdygM):
`ma1-real-idp-org-flows`, on a test organization deployment with a real IdP. CI fakes the IdP, so
only a real one shows that silent SSO re-authentication now stops at the consent page, that a
provider connect and a sign-in still work end to end with the new cookies, and that IdP step-up
from a card still releases the approval.

## 6. Risks and open questions

- **Tests that walk the old flow.** `tests/unit/web/conftest.py::authorize_and_get_code` asserts
  the IdP callback redirects straight to the client; q1 updates it. If
  `tests/integration/test_org_ubuntu_release_smoke.py` drives `/authorize` or `/login` end to end,
  q1/q2 must update it too; a worker that finds such a flow failing for any reason other than the
  new consent step or the binding cookie stops with `status=blocked`.
- **Secure cookie over plain HTTP in tests.** Unit tests use `https://` base URLs, so TestClient
  sends Secure cookies. `tests/integration/test_org_ubuntu_release_smoke.py` uses `LoopbackClient`
  (plain `http://127.0.0.1` with a hand-set Host, `:415-430`) and passes cookies by hand
  (`_extract_cookie`); q1 and q2 say exactly how to carry the consent and `pf_org_login` there.
- **Phone layout.** The consent and continue pages are `plain_page` documents like `/signed-out`,
  which has no `TestPhoneLayout` case either; none is added.
- **Plan 1 overlap.** `plan/approval-core-hardening` edits `McpDispatcher.call`'s `with` statement;
  q5 edits `_dedupe_key` and its one call line. The second PR to merge takes a small merge.
- **Plan 3 overlap.** `plan/platform-and-ci-hardening` changes the Chromium skip in
  `test_browser_smoke.py`'s `browser` fixture; q4 adds a test class to the same file.

## Implementation manifest

```yaml
plan_slug: web-and-org-hardening
feature_branch: fix/web-and-org-hardening
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/KRGXYAj6wbbtaByC3jdygM
manual_steps_source: docs/web-and-org-hardening-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-real-idp-org-flows
    title: On a test organization deployment with a real IdP, check the consent page, provider connect, sign-in and IdP step-up
    why: CI fakes the IdP; only a real one shows that silent SSO re-authentication stops at the consent page and that the new cookies and the POST step-up work in a real browser against a real provider
verify_after_merge:
  - python3 -m pytest tests/unit/web -q
  - python3 -m pytest tests/unit -q -k "settings_window or approval_list or card_builder or html_embed or text_extraction or export_files or salesforce" 
  - python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-fail-under=100
final_checks:
  - docs/web-and-org-hardening-plan.md and docs/web-and-org-hardening-plan-manual-steps.html are deleted and nothing links to them
  - ADRs 0177, 0178, 0179 and 0180 exist, are Accepted, and are listed in docs/adr/README.md; ADR 0155 and ADR 0108 carry a one-line Status pointer to 0180 and 0177
  - CHANGELOG.md has this plan's entries under "## [Unreleased]" and no new version heading
  - pytest tests/integration -v passed on the feature branch head (web/routes_mcp.py and web/mcp_dispatch.py changed), with test_mcp_daemon_contract.py green
  - connector-live-check.yml was dispatched against the feature branch (connectors gmail, confluence, drive, jira, salesforce and salesforce_client.py changed) and the run is linked in the PR
  - python3 scripts/qa_web_smoke.py was run (the approval card's bridge shim and the list/settings JS embeds changed) and its report is in the PR under "## Web smoke check"
phases:
  - id: q1-org-mcp-consent
    title: Show a consent page before an org-mode MCP client gets a code
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/web/oauth_provider.py
      - src/privacyfence/web/oauth_consent.py
      - src/privacyfence/web/routes_mcp.py
      - tests/unit/web/test_oauth_provider.py
      - tests/unit/web/test_oauth_consent.py
      - tests/unit/web/conftest.py
      - tests/unit/web/test_org_mcp_e2e.py
      - tests/integration/test_org_ubuntu_release_smoke.py
      - src/privacyfence/web/server.py
      - tests/unit/web/test_server_org_mode.py
    brief: |
      Read docs/web-and-org-hardening-plan.md sections 2 and 3.1; 3.1 is the spec (names, strings,
      status codes). Findings: SEC-B1, TST-B8, TST-B12 (the oauth_provider part).
      1. Write the failing tests first in tests/unit/web/test_org_mcp_e2e.py:
         `test_authorize_for_unconsented_client_stops_at_consent_page` — drive /register,
         /authorize and the faked IdP callback with the existing helpers; the IdP callback's
         Location is `/oauth/consent/<id>`, not the client's redirect_uri; GET that page shows the
         client name and `claude.ai` (the redirect host) and contains no `code=`. Register the client
         with `client_name="Claude"` (the default `register_client` body has none).
      2. Create src/privacyfence/web/oauth_consent.py with `ConsentView`, `ConsentStore`,
         `render_consent_page(view, *, consent_id, nonce) -> str` and
         `render_continue_page(redirect_url, *, redirect_host, nonce) -> str` per 3.1.
      3. src/privacyfence/web/oauth_provider.py: the constants, `_PendingConsent`,
         `_pending_consents`, `_issue_code`, the TTL check after the pop, the consent branch in
         `handle_idp_callback`, `consent_view`, `decide_consent`, pruning — all per 3.1. Update the
         class and `handle_idp_callback` docstrings (it now returns either the client's redirect or
         the consent page path; ADR 0177).
      4. src/privacyfence/web/routes_mcp.py `mount_org_oauth`: the GET and POST consent routes
         per 3.1; mention them in the function's docstring. src/privacyfence/web/server.py: the
         COOP change per 3.1 ("Popup opener"); tests/unit/web/test_server_org_mode.py
         `TestCrossOriginOpenerPolicyOrgMode`: a GET and a POST to `/oauth/consent/x` get
         `unsafe-none`, `/approvals` still gets `same-origin`.
      5. tests/unit/web/conftest.py `authorize_and_get_code`: after the IdP callback, if the
         Location starts with `/oauth/consent/`, GET it, read the hidden `csrf` value, POST
         `{"csrf": ..., "decision": "allow"}` with an `Origin: <ISSUER>` header, and take the URL
         from the continue page's `Continue` link (read its `href` with `html.unescape`); then
         continue as before. Every caller keeps working unchanged. Also make
         `isolate_org_oauth_stores` patch `privacyfence.paths.data_dir` to `tmp_path`, so consent
         files never land in the checkout.
      6. tests/unit/web/test_oauth_consent.py (new; docstring: the consent page is the only step
         that tells an org member which client is asking): `ConsentStore` round trip, malformed
         file reads as no consent; the page escapes a hostile client name
         (`</title><script>x</script>`) and shows `An unnamed AI client` for an empty one.
      7. tests/unit/web/test_oauth_provider.py: `_drive_full_flow` (:71-82),
         `test_idp_callback_redirects_to_the_original_clients_redirect_uri` (:157),
         `test_idp_callback_state_is_single_use`, `test_idp_callback_admitted_by_authz_policy_succeeds`,
         `test_load_authorization_code_for_a_different_client_id_returns_none` and every other user
         of `_drive_full_flow` expect a direct client redirect: have them call
         `provider._consents.record(Principal(id=<the claims' sub>), client.client_id, <redirect_uri>)`
         before `handle_idp_callback`. Only the new consent tests go through `decide_consent`. Add
         `test_authorize_shows_client_name_and_redirect_host_before_issuing_a_code`,
         `test_idp_callback_does_not_issue_a_code_until_consent_is_posted`,
         `test_consent_post_requires_csrf` (wrong csrf → ValueError, and the consent is consumed),
         `test_a_remembered_consent_skips_the_page`,
         `test_a_consent_for_one_redirect_uri_does_not_cover_another`,
         `test_deny_returns_access_denied_to_the_client`,
         and `test_idp_callback_after_ttl_without_a_later_authorize_raises` (advance `op.time.time`
         past the TTL with no second authorize).
      8. In test_org_mcp_e2e.py also add a route-level test that POSTs the consent with a foreign
         `Origin` → 403, and with `decision=maybe` → 400.
      9. tests/integration/test_org_ubuntu_release_smoke.py: `_mcp_bearer_token_for` (:467-503)
         and `test_dcr_authorize_token_and_a_real_tool_call...` (:666-700) walk /authorize to a
         code; make them GET the consent page and POST the consent with **no** `Origin` header
         (that client talks plain http to 127.0.0.1, so an https Origin would be refused; a missing
         Origin is accepted by `check_origin`), then follow the continue page's link.
      10. Run python3 -m pytest tests/unit/web -q, python3 -m pytest tests/integration -q,
          ruff check ., python3 -m mypy src/privacyfence.
      Stop condition: if any existing test needs the IdP callback to redirect straight to the
      client for a reason other than "no consent was given" (for example, a refresh-token flow),
      stop with status=blocked and quote it.
    acceptance:
      - "SEC-B1/TST-B8: python3 -m pytest tests/unit/web/test_oauth_provider.py -k consent -q collects at least 4 tests and passes"
      - "SEC-B1: python3 -m pytest tests/unit/web/test_org_mcp_e2e.py -k unconsented_client_stops_at_consent_page -q passes"
      - "SEC-B1 (ADR 0108 kept): python3 -m pytest tests/unit/web/test_server_org_mode.py -k CrossOriginOpenerPolicy -q passes"
      - "TST-B12: python3 -m pytest tests/unit/web/test_oauth_provider.py -k after_ttl_without_a_later_authorize -q passes"
      - python3 -m pytest tests/unit/web tests/integration -q passes

  - id: q2-org-callback-binding
    title: Finish provider-connect and sign-in callbacks only in the browser that started them
    depends_on: [q1-org-mcp-consent]
    complexity: M
    touches:
      - src/privacyfence/web/routes_connect.py
      - src/privacyfence/web/routes_org_identity.py
      - tests/unit/web/test_routes_connect.py
      - tests/unit/web/test_routes_org_identity.py
      - tests/integration/test_org_ubuntu_release_smoke.py
      - src/privacyfence/web/org_session.py
    brief: |
      Read docs/web-and-org-hardening-plan.md sections 2 and 3.2. Findings: SEC-B2, SEC-B6, TST-B5,
      TST-B12 (routes_connect and login parts), TST-B13.
      1. tests/unit/web/test_routes_connect.py, class TestOAuthCallback (:314): add
         `test_callback_with_a_different_signed_in_principal_is_rejected` (start as alice, call back
         with bob's cookie from `_signed_in(sessions, "bob")` → 302 to `/connect?error=slack`; no
         token file under either principal: assert
         `not (paths.user_dir(alice) / "credentials" / "slack_token.json").exists()` and the same
         for bob) and `test_callback_with_no_session_is_rejected`. Rename
         `test_happy_path_saves_token_evicts_registry_and_needs_no_session_cookie` to
         `test_happy_path_saves_token_and_evicts_registry` and send alice's cookie on the callback.
         Add `test_callback_after_ttl_is_rejected` (monkeypatch `rc.time.time` forward past
         `rc._PENDING_TTL_SECONDS` after start, no further put → 400). Every other existing
         `/oauth/callback/*` test that expects success (including
         `test_apps_script_callback_saves_its_own_token_with_its_own_scopes`, `test_state_is_single_use`,
         `test_atlassian_callback_lands_on_the_shared_grant_url_not_a_per_service_one` and the Grist
         OAuth callback tests in `TestGristOAuth`, :825-895) sends the starting principal's cookie
         (`cookies={org_session.SESSION_COOKIE: session_id}`) on the callback.
      2. src/privacyfence/web/routes_connect.py: the principal check, the docstring and
         module-docstring changes, and the TTL in `_PendingAuthStore.pop`, per 3.2;
         src/privacyfence/web/org_session.py: the comment paragraph per 3.2.
      3. TST-B13, same test file, class TestTelegramFlow: replace `test_start_without_csrf_is_rejected`
         with `test_post_without_csrf_is_rejected`, parametrized over
         `["start", "code", "2fa", "cancel"]`, signed in, `data={"csrf": "wrong", ...}` → 401; and
         `test_post_from_another_origin_is_rejected` with the right csrf and
         `headers={"Origin": "https://evil.example"}` → 403. Each monkeypatches
         `rc.telegram_auth.send_code`, `sign_in` and `sign_in_2fa` to record calls and asserts none
         was made, besides the status code.
      4. tests/unit/web/test_routes_org_identity.py: in TestLoginCallback add
         `test_login_callback_without_the_starting_browser_cookie_is_rejected` (valid state and
         code, no `pf_org_login` cookie → 400, no `pf_org_session` set),
         `test_login_callback_with_another_attempts_cookie_is_rejected`,
         `test_login_sets_the_binding_cookie` (HttpOnly, Secure, path `/oauth/idp/login-callback`),
         and `test_login_callback_after_ttl_is_rejected`. Update existing callback tests to carry
         the cookie the `/login` response set (the TestClient keeps it if both requests use the
         same client; check each test).
      5. src/privacyfence/web/routes_org_identity.py: per 3.2.
      6. Run python3 -m pytest tests/unit/web -q, python3 -m pytest tests/integration -q,
         ruff check ., python3 -m mypy src/privacyfence.
      tests/integration/test_org_ubuntu_release_smoke.py: in `_complete_browser_login` (:449-465)
      and `test_a_principal_outside_every_allowed_domain_is_denied_sign_in` (:884-895), read
      `pf_org_login` from the /login response's Set-Cookie headers (a variant of `_extract_cookie`
      that finds that cookie by name) and send it as `headers={"Cookie": f"pf_org_login={value}"}`
      on the callback request. That client is plain http, so the browser rule for Secure cookies
      does not apply to a hand-set header.
      Stop condition: if any other test signs in through /login and fails on the binding cookie,
      stop with status=blocked and name it.
    acceptance:
      - "SEC-B2/TST-B5: python3 -m pytest tests/unit/web/test_routes_connect.py -k \"different_signed_in_principal or no_session_is_rejected\" -q passes"
      - "TST-B12: python3 -m pytest tests/unit/web/test_routes_connect.py tests/unit/web/test_routes_org_identity.py -k after_ttl -q passes"
      - "TST-B13: python3 -m pytest tests/unit/web/test_routes_connect.py -k \"without_csrf or another_origin\" -q passes, and the same file run with --cov=privacyfence.web.routes_connect --cov-branch --cov-report=term-missing does not list the three telegram `return rejected` lines (581, 616, 648 at the base commit) as missing"
      - "SEC-B6: python3 -m pytest tests/unit/web/test_routes_org_identity.py -k \"starting_browser_cookie or another_attempts_cookie or binding_cookie\" -q passes"
      - python3 -m pytest tests/integration -q passes

  - id: q3-org-stepup-post
    title: Start the IdP step-up only from the card, by a CSRF-checked POST
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/web/routes_org_stepup.py
      - src/privacyfence/web/routes_approvals.py
      - tests/unit/web/test_routes_org_approvals.py
    brief: |
      Read docs/web-and-org-hardening-plan.md sections 2 and 3.3. Findings: SEC-B4, TST-B7,
      TST-B12 (step-up part).
      1. tests/unit/web/test_routes_org_approvals.py::TestIdpStepUp (:732): add
         `test_start_rejects_get_without_csrf` (a bare GET → 405),
         `test_start_from_a_cross_site_navigation_is_refused` (POST with the right csrf but
         `Origin: https://evil.example` → 403, and no attempt stored),
         `test_start_without_csrf_is_rejected` (403), `test_start_is_404_when_step_up_is_off`
         (`StepUpConfig(enabled=False)`), and `test_callback_after_ttl_is_rejected`. Convert every
         existing start test from GET-with-query to POST-with-JSON (`{"result": ..., "choice": ...,
         "csrf": <session id>}` with `Origin: ISSUER`) and assert the JSON `redirect` instead of a
         302 Location (the IdP URL checks — `prompt=login`, `max_age=0` — move onto it). Also
         convert `test_idp_stepup_start_is_refused_even_hit_directly` (:721, in the
         require_passkey class) to a POST with a valid csrf; it keeps its 403.
      2. src/privacyfence/web/routes_org_stepup.py: per 3.3, plus the TTL in
         `_StepUpAuthAttemptStore.pop`. Update the module docstring.
      3. src/privacyfence/web/routes_approvals.py: `_org_step_up_response`'s `idp_stepup_url`
         and `_org_bridge_shim`'s two link sites per 3.3 (one `pfIdpStepUp` function). Update the
         shim's docstring sentence about "a 428's idp_stepup_url fallback link", and the
         docstrings at :36, :76 and :977.
      4. Add to the same test file a check on the rendered shim: the 428 response for an IdP
         fallback carries `idp_stepup_url` with no `?`, and the shim source contains
         `addEventListener('click'` and no `<a href="' + data.idp_stepup_url`.
      5. Run python3 -m pytest tests/unit/web -q, python3 -m pytest tests/integration/test_browser_smoke.py -q
         (needs Chromium: set PRIVACYFENCE_TEST_CHROMIUM as the session hook says; a SKIPPED run
         is not a pass — report it), ruff check ., python3 -m mypy src/privacyfence.
      Stop condition: if `grep -n "stepup/idp" tests/integration/*.py` finds a test that follows
      the old link, stop with status=blocked and name it.
    acceptance:
      - "SEC-B4/TST-B7: python3 -m pytest tests/unit/web/test_routes_org_approvals.py -k \"get_without_csrf or cross_site_navigation or start_without_csrf or step_up_is_off\" -q passes"
      - "TST-B12: python3 -m pytest tests/unit/web/test_routes_org_approvals.py -k after_ttl -q passes"
      - python3 -m pytest tests/unit/web/test_routes_org_approvals.py -q passes
      - grep -n 'methods=\["POST"\]' src/privacyfence/web/routes_org_stepup.py matches the stepup/idp route

  - id: q4-script-embedding-and-escaping
    title: One escaping serializer for JSON in scripts, and escaped image sources
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/html_embed.py
      - src/privacyfence/settings_window_html.py
      - src/privacyfence/approval_list_html.py
      - src/privacyfence/web_shell.py
      - src/privacyfence/card_builder.py
      - src/privacyfence/approval_window_html.py
      - tests/unit/test_html_embed.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/test_approval_list_html.py
      - tests/unit/test_web_shell.py
      - tests/unit/test_card_builder.py
      - tests/unit/test_approval_window_html.py
      - tests/unit/web/test_agent_attestation.py
      - tests/integration/test_browser_smoke.py
    brief: |
      Read docs/web-and-org-hardening-plan.md sections 2 and 3.4. Findings: SEC-B3, SEC-B8, TST-B1,
      TST-B2.
      1. tests/unit/test_settings_window_html.py, TestDocumentShell: replace the assertion in
         `test_html_special_characters_in_state_do_not_break_the_script_tag` with
         `assert "</script><script>alert(1)" not in html` and
         `assert html.count("</script>") == html.count("<script")`, keep the round-trip check, and
         parametrize over each embed `build_html` takes a value for: `state`, `ai_client["mcp_url"]`,
         `initial_section` and a scoped-token rule sentence (read `build_html`'s signature for the
         parameter names; `capabilities` is computed from mode and role, so it is not a case). It must fail today:
         `python3 -m pytest tests/unit/test_settings_window_html.py -k special_characters`.
      2. Create src/privacyfence/html_embed.py and tests/unit/test_html_embed.py (`<`, `>`, `&`,
         U+2028, U+2029 escaped; `json.loads(json_for_script(v)) == v` for a nested value).
      3. Apply `json_for_script` at every site listed in 3.4 (settings_window_html.py,
         approval_list_html.py, web_shell.py). Add one test per module that a hostile value
         cannot close the script: approval_list_html with `approval_list_html.UNRECOGNISED_LABEL`
         monkeypatched to `</script><b>x`, and web_shell with a hostile `notifications_detail`.
      4. tests/unit/web/test_agent_attestation.py:
         `test_a_hostile_client_name_cannot_break_the_admin_page` — register a DCR client named
         `</script><a href=//evil.example style=position:fixed;inset:0>x`, render the admin
         `/settings` the way the file's existing settings tests do, and assert no parsed `<a>`
         with `href="//evil.example"` (use `html.parser.HTMLParser`) and the name appears
         escaped.
      5. src/privacyfence/card_builder.py and approval_window_html.py per 3.4.
         tests/unit/test_card_builder.py: `test_hostile_preview_mime_type_cannot_break_out_of_img_src`
         (mime `image/png"><iframe src="data:text/html,x">` → no image rendered and no parsed
         `<iframe>`); tests/unit/test_approval_window_html.py: an `src` value containing `"` is
         escaped.
      6. tests/integration/test_browser_smoke.py: add `class TestHostileContentStaysText` with
         `test_list_rerender_escapes_row_fields` — register with `_register_gated_card(web_ui,
         gate_kind="popup", dedupe_key="xss", summary="<img src=x id=pfxss>",
         tool="gmail_create_draft", tool_name="<img src=x id=pfxss2>")`, sign in, open `/approvals`,
         register a second card to trigger the SSE re-render, wait until two rows show, then
         assert `page.locator('#pfxss').count() == 0`, `page.locator('#pfxss2').count() == 0`, and
         the row text contains the literal `<img`; and `test_settings_rerender_escapes_state` —
         with `local_server_with_settings`, write an account display name of
         `<b id=pfxss3>x</b>` through the controller's normal path (read the fixture for what it
         exposes), push a snapshot as `test_connector_icons_survive_a_live_update` does, and
         assert `#pfxss3` count 0. Resolve every card in `finally`.
      7. Prove step 6 can fail: in a scratch copy only, make each JS `esc()` return
         `String(s == null ? '' : s)`; the matching test must fail; restore. Report it.
      8. Run python3 -m pytest tests/unit -q -k "settings_window or approval_list or web_shell or card_builder or approval_window or html_embed or agent_attestation",
         python3 -m pytest tests/integration/test_browser_smoke.py -q with PRIVACYFENCE_TEST_CHROMIUM
         set (a SKIPPED class is not a pass — report it), ruff check ., python3 -m mypy src/privacyfence.
      Stop condition: if `json_for_script` changes a value the page's JS reads (a test comparing
      raw `json.dumps` output fails), check the JS reads it through JSON/JS parsing; if it compares
      raw text, stop with status=blocked and name it.
    acceptance:
      - python3 -m pytest tests/unit/test_settings_window_html.py -k special_characters -q passes (and failed before the change, per the report)
      - python3 -m pytest tests/unit/web/test_agent_attestation.py -k hostile_client_name -q passes
      - python3 -m pytest tests/unit/test_card_builder.py -k hostile_preview_mime_type -q passes
      - python3 -m pytest tests/integration/test_browser_smoke.py -k HostileContentStaysText -q passes with 2 tests run, none skipped
      - grep -rn 'json.dumps(state)' src/privacyfence/settings_window_html.py returns nothing
      - "SEC-B3: grep -n \"json.dumps\" src/privacyfence/approval_list_html.py src/privacyfence/web_shell.py shows no value substituted into a <script> (each remaining hit is outside script embedding, said in the report)"
      - "TST-B2: the report shows both mutation runs of step 7 failing the matching browser test"

  - id: q5-local-web
    title: Owner-only settings stream, no bootstrap open redirect, per-session dedupe
    depends_on: [q1-org-mcp-consent]
    complexity: M
    touches:
      - src/privacyfence/web/server.py
      - src/privacyfence/web/state_stream.py
      - src/privacyfence/web/mcp_dispatch.py
      - tests/unit/web/test_server.py
      - tests/unit/web/test_state_stream.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/unit/web/test_routes_settings.py
    brief: |
      Read docs/web-and-org-hardening-plan.md sections 2 and 3.5. Findings: SEC-B5, SEC-B7, SEC-A6,
      TST-B3, TST-B6, TST-B14, TST-B20.
      1. tests/unit/web/test_server.py, extend TestStateStreamTouchesItsOwnSession (:1322) — use
         the server's own wiring: `WebServer(web_ui, port=0, controller=<a SettingsController as
         test_browser_smoke's local_server_with_settings builds it>).state_stream` and
         `_state_stream_route(server.state_stream, sessions=sessions)`; register one pending
         approval under LOCAL_PRINCIPAL and one under `Principal(id="os-1001")`; create a session
         for each (`sessions.create(principal_id=...)`); call the endpoint inside
         `principal_scope(...)` for that principal.
         - `test_stream_carries_only_the_requesting_principals_approvals`: os-1001's first
           `event: approvals` payload has only its own approval id.
         - `test_non_owner_stream_carries_no_settings_event`: os-1001's stream yields no
           `event: settings`, including after `server.state_stream.push_settings({"a": 1})`.
         - `test_owner_stream_still_carries_settings`.
         The first must fail if server.py's `list_pending` lambda drops its `principal_id=`
         (check in a scratch copy and report).
      2. src/privacyfence/web/state_stream.py and server.py `_state_stream_route` per 3.5. Update
         tests/unit/web/test_state_stream.py and tests/unit/web/test_routes_settings.py:287
         (`subscribe(disconnected)` → `receives_settings=True`) for the new required keyword and add a test that a
         `receives_settings=False` subscriber never gets a settings event.
      3. TST-B6 in TestBootstrapFlow (:760): `test_double_slash_path_never_redirects_off_origin`,
         parametrized over `//evil.example.com`, `/\\evil.example.com`, `//evil.example.com/%2e%2e`,
         driven through raw ASGI (build the scope dict as the file does at :224-236, path set to
         the value, query_string `bootstrap=x`, and collect `http.response.start` headers);
         assert `location.startswith("/")`, not `//`, not `/\\`. Then fix server.py:899 per 3.5.
      4. SEC-A6 in tests/unit/web/test_mcp_dispatch.py TestCall:
         `test_an_identical_call_from_another_session_is_not_deduped` (two sessions, one in flight
         on a slow FakeConnector → connector called twice) and
         `test_an_unattended_session_never_gets_another_sessions_result` (session s1 normal,
         session s2 after `begin_unattended_session("s2", "why")` with
         `unattended_sessions_enabled=True`; use `UnattendedAwareConnector` (:100) and assert s2's
         call ran itself with `observed_unattended == [False, True]`). Then change `_dedupe_key`
         per 3.5. `test_dedupe_still_applies_within_the_same_principal` and
         `test_two_identical_scoped_calls_still_dedupe` must still pass (same session).
      5. TST-B14, same class: `test_cancelled_leader_does_not_poison_the_dedupe_key` — a connector
         that awaits an asyncio.Event; start call A as a task, cancel it, await it
         (CancelledError), set the event, issue identical call B with
         `asyncio.wait_for(..., 1)`; B completes and `dispatcher._inflight` has no key left.
      6. TST-B20: replace the fixed sleeps with polls for the observable outcome:
         test_routes_org_settings.py:457-464 → poll `client.get("/settings").text` for
         `'"value": "Q3 Reports"'` up to 200 × 10 ms, then assert. Leave
         test_control_channel.py:1305-1308 alone: its sleep deliberately forces the losing
         interleaving (its docstring at :1280-1287 says so). test_server.py:970-982
         `_wait_until_connectable` is a readiness poll with a deadline, which §2.8 allows; leave it.
      7. Run python3 -m pytest tests/unit/web -q, python3 -m pytest tests/integration -q, ruff
         check ., python3 -m mypy src/privacyfence.
      Stop condition: if any route or test besides `/api/state/stream`, test_state_stream.py and
      test_routes_settings.py:287 calls `StateStream.subscribe`, stop with status=blocked and name it.
    acceptance:
      - "SEC-B5/TST-B3: python3 -m pytest tests/unit/web/test_server.py -k \"only_the_requesting_principals_approvals or non_owner_stream or owner_stream_still\" -q passes"
      - "SEC-B7/TST-B6: python3 -m pytest tests/unit/web/test_server.py -k double_slash_path -q passes"
      - "SEC-A6: python3 -m pytest tests/unit/web/test_mcp_dispatch.py -k \"another_session or unattended_session_never\" -q passes"
      - "TST-B14: python3 -m pytest tests/unit/web/test_mcp_dispatch.py -k cancelled_leader -q passes"
      - "TST-B20: grep -n \"time.sleep(0.05)\" tests/unit/web/test_routes_org_settings.py returns nothing"
      - python3 -m pytest tests/integration -q passes

  - id: q6-extraction-bounds
    title: Bound XLSX and PPTX extraction and run it off the event loop
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/text_extraction.py
      - src/privacyfence/connectors/gmail.py
      - src/privacyfence/connectors/confluence.py
      - src/privacyfence/connectors/drive.py
      - tests/unit/test_text_extraction.py
      - tests/unit/connectors/test_gmail_connector.py
      - tests/unit/connectors/test_confluence_connector.py
      - tests/unit/connectors/test_drive_connector.py
    brief: |
      Read docs/web-and-org-hardening-plan.md sections 2 and 3.6. Findings: SEC-C2, TST-B9.
      1. tests/unit/test_text_extraction.py::TestZipBombProtection (:315-375): add
         `test_xlsx_member_over_the_cap_returns_empty_string` (monkeypatch
         `text_extraction._MAX_ZIP_ENTRY_BYTES` to 10; `extract_text(make_xlsx({"S": [["a"]]}), XLSX_MIME) == ""`;
         use the file's existing xlsx builder, or write one with openpyxl if none exists),
         `test_xlsx_shared_strings_bomb_is_refused` (a hand-built zip whose
         `xl/sharedStrings.xml` decompresses to 50 MiB of one `<t>` element, written with
         `zipfile.ZIP_DEFLATED` streaming so the test itself stays small; the result is `""` and
         `tracemalloc` peak during `extract_text` is under 20 MiB),
         `test_xlsx_row_cap_shows_the_first_200_rows` (250 rows in total, one header and 249 data
         rows → the header and 199 body rows and the footer `*(showing the first 200 rows of 250)*`),
         `test_xlsx_bomb_in_styles_is_refused` (the same bomb placed in `xl/styles.xml`), and
         `test_pptx_slide_count_is_capped` (monkeypatch `text_extraction._PPTX_MAX_SLIDES` to 2, a 3-slide pptx
         → 2 slides and the footer). The first two fail today.
      2. src/privacyfence/text_extraction.py per 3.6. Update the zip-bomb comment at :68-77 to say
         XLSX members are bounded too.
      3. The six call sites per 3.6 (gmail.py:1169, confluence.py:622, drive.py:1446, 1735, 1759,
         1780): `await asyncio.to_thread(extract_text, ...)`. If any is not inside an `async def`,
         stop with status=blocked.
      4. Run python3 -m pytest tests/unit/test_text_extraction.py tests/unit/connectors -q; if a
         connector test patched `extract_text` in a way to_thread breaks, fix the test's patch
         target only. Then ruff check ., python3 -m mypy src/privacyfence.
      5. Do not dispatch the live check here; the plan's final checks do it once for the branch.
    acceptance:
      - "SEC-C2/TST-B9: python3 -m pytest tests/unit/test_text_extraction.py -k \"xlsx_member_over_the_cap or shared_strings_bomb or bomb_in_styles or row_cap or slide_count\" -q passes"
      - "SEC-C2: grep -nE \"=\\s*extract_text\\(\" src/privacyfence/connectors/gmail.py src/privacyfence/connectors/confluence.py src/privacyfence/connectors/drive.py returns nothing"
      - python3 -m pytest tests/unit/connectors -q passes

  - id: q7-exports-and-report-ids
    title: Validate paged Salesforce report ids, and neutralise formula cells in Jira and Salesforce CSVs
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/salesforce_client.py
      - src/privacyfence/export_files.py
      - src/privacyfence/connectors/jira.py
      - src/privacyfence/connectors/salesforce.py
      - tests/unit/test_salesforce_report_paging.py
      - tests/unit/test_salesforce_client.py
      - tests/unit/test_export_files.py
      - tests/unit/connectors/test_jira_connector.py
      - tests/unit/connectors/test_salesforce_connector.py
    brief: |
      Read docs/web-and-org-hardening-plan.md sections 2 and 3.6. Findings: SEC-C3, TST-B11, SEC-C4
      (with TST-B10). The user decided: Jira and Salesforce neutralise, Grist does not.
      1. tests/unit/test_salesforce_report_paging.py: `test_non_id_report_id_is_refused_before_any_request`,
         parametrized over the four bad ids `tests/unit/test_salesforce_client.py:580-595` uses for
         `run_report` (copy them): `pytest.raises(SalesforceClientError, match="report_id must be a
         15- or 18-character")` around `client.run_report_page(bad, "Id")`, then assert no
         Salesforce call was made (that file uses `FakeAnalytics`; assert it recorded no call, or
         build the client as test_salesforce_client.py does with
         `with_fake_sf(make_client(), sf := MagicMock())` and assert `sf.mock_calls == []`).
         Fails today. Fix salesforce_client.py per 3.6 (validation after the empty check). In
         tests/unit/test_salesforce_client.py:1072-1120, replace the non-id `"r1"` passed to
         `run_report_page` with the file's `REPORT_ID`; `test_requires_report_id` (:1060) keeps
         expecting "requires a report_id".
      2. tests/unit/test_export_files.py: `class TestRenderCsv` gains
         `test_formula_leading_cells_are_neutralised`, parametrized over `=1+1`, `+1`, `-1+1`,
         `@SUM(A1)`, `\t=1`, `\r=1`: `render(["a"], [[v]], "csv", spreadsheet_safe=True)` starts
         the cell with `'`; and `render(["a"], [["=1+1"]], "csv", spreadsheet_safe=True) ==
         b"a\n'=1+1\n"`. Also: the int `-5` is unchanged; `spreadsheet_safe=False` (the default)
         leaves `=1+1` as is; `fmt="json"` is unchanged either way.
      3. src/privacyfence/export_files.py per 3.6. If `ruff check .` rejects importing the private
         name, define `FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t", "\r")` in export_files.py
         with a comment that it must match `audit_log._FORMULA_TRIGGER_CHARS`, and add a unit test
         asserting the two are equal.
      4. src/privacyfence/connectors/jira.py:759 and salesforce.py:730 pass `spreadsheet_safe=True`.
         Add one test in each connector test file: an export whose row holds `=HYPERLINK("x")`
         writes `'=HYPERLINK("x")` to the CSV (follow the file's existing export test for setup).
         Add one Grist assertion nowhere; Grist is untouched.
      5. Run python3 -m pytest tests/unit/test_export_files.py tests/unit/test_salesforce_report_paging.py
         tests/unit/test_salesforce_client.py tests/unit/connectors -q, ruff check .,
         python3 -m mypy src/privacyfence.
    acceptance:
      - "SEC-C3/TST-B11: python3 -m pytest tests/unit/test_salesforce_report_paging.py -k non_id_report_id -q passes with 4 cases"
      - "SEC-C4/TST-B10: python3 -m pytest tests/unit/test_export_files.py -k formula_leading_cells -q passes"
      - python3 -m pytest tests/unit/test_salesforce_client.py -q passes
      - python3 -c "from privacyfence.export_files import render; assert render(['a'], [['=1+1']], 'csv', spreadsheet_safe=True) == b\"a\n'=1+1\n\"" exits 0
      - git diff --stat origin/main -- src/privacyfence/connectors/grist.py shows no change

  - id: q8-sync-client
    title: Hide a --token-env token from scripts and refuse a download path outside the work folder
    depends_on: []
    complexity: S
    touches:
      - sync/src/privacyfence_sync/cli.py
      - sync/src/privacyfence_sync/runner.py
      - sync/README.md
      - plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md
      - sync/tests/test_cli.py
      - sync/tests/test_runner.py
    brief: |
      Read docs/web-and-org-hardening-plan.md sections 2 and 3.7. Findings: SEC-D7, SEC-D8.
      Set up: python3 -m pip install -e "./sync[test]".
      1. sync/tests/test_runner.py: `test_custom_token_env_is_hidden_from_scripts` — run a recipe
         through the CLI path with `--token-env MY_TOKEN` (monkeypatch.setenv) and a script step
         that writes `os.environ.get("MY_TOKEN")` to an output file; the file holds `None`. If the
         runner tests cannot reach `cli._connect`, put this test in sync/tests/test_cli.py next to
         `test_token_env_names_another_variable` (:145) instead, asserting
         `"MY_TOKEN" not in os.environ` after `_connect` and that a script step run afterwards
         does not see it.
      2. sync/tests/test_runner.py: `test_export_on_local_disk_is_copied_and_checked` (:143-149)
         pins the old behaviour (an outside path is copied); turn it into the refusal test below.
         `test_a_download_path_outside_the_work_folder_is_refused` — a
         fake client returns `{"delivery": "local_disk", "path": str(tmp_path / "outside.txt")}`
         for a download step whose work folder is `tmp_path / "work"`; assert `SyncError` and that
         `outside.txt` still exists. Add `test_a_symlinked_download_path_is_refused` (a symlink
         inside the work folder pointing outside).
      3. sync/src/privacyfence_sync/cli.py and runner.py per 3.7.
      4. sync/README.md: replace the bullet per 3.7, then copy the whole file over the plugin copy
         (`cp sync/README.md plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md`)
         and run python3 -m pytest tests/unit/test_sync_plugin.py -q.
      5. Run python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch
         --cov-report=term-missing --cov-fail-under=100, python3 -m mypy sync/src,
         bandit -r sync/src.
    acceptance:
      - python3 -m pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-fail-under=100 passes
      - "SEC-D7: python3 -m pytest sync/tests -k token_env_is_hidden -q passes"
      - "SEC-D8: python3 -m pytest sync/tests -k \"outside_the_work_folder or symlinked_download\" -q passes"
      - python3 -m pytest tests/unit/test_sync_plugin.py -q passes
      - python3 -m mypy sync/src and bandit -r sync/src pass

  - id: q9-retire
    title: ADRs 0177-0180, reference docs, changelog, and delete the plan
    depends_on: [q1-org-mcp-consent, q2-org-callback-binding, q3-org-stepup-post, q4-script-embedding-and-escaping, q5-local-web, q6-extraction-bounds, q7-exports-and-report-ids, q8-sync-client]
    complexity: S
    touches:
      - docs/adr/0177-*.md
      - docs/adr/0178-*.md
      - docs/adr/0179-*.md
      - docs/adr/0180-*.md
      - docs/adr/0155-exports-write-a-file-and-return-only-counts-and-a-hash.md
      - docs/adr/0108-oauth-sign-in-popups-keep-their-opener-and-basic-clients-need-not-repeat-their-id.md
      - docs/adr/README.md
      - docs/org-mode-setup-guide.md
      - docs/security-and-compliance.md
      - docs/configuration-reference.md
      - CHANGELOG.md
      - docs/web-and-org-hardening-plan.md
      - docs/web-and-org-hardening-plan-manual-steps.html
    brief: |
      Read docs/web-and-org-hardening-plan.md (whole) and docs/adr/README.md (template and rules).
      1. Write four ADRs with the template, Status "Accepted — <today>.":
         - 0177-an-org-mode-mcp-client-needs-the-persons-consent-once.md: Context SEC-B1's
           confused-deputy problem (one fixed client at the IdP, silent SSO re-authentication for
           any DCR client); Decision 3.1; Alternatives 3.1's rejected list; Verification
           web/oauth_consent.py, oauth_provider.handle_idp_callback/decide_consent, the q1 tests.
           Status also says "Amends 0108." (the consent pages keep the sign-in popup's opener).
           Consequences: consent is remembered per person, client and redirect URI, and
           signing out everywhere does not clear it (there is no revocation yet).
         - 0178-a-sign-in-or-connect-flow-is-finished-in-the-session-that-started-it.md: 3.2
           (including the one-slot binding cookie consequence);
           Alternative: binding `/connect`'s state to its own cookie (rejected: the signed-in
           session already identifies the principal, a second cookie adds nothing).
         - 0179-an-idp-step-up-starts-only-from-the-card-by-post.md: 3.3; Alternative: landing the
           IdP callback back on the card for a final click (rejected: one more click on every
           step-up, and the POST already removes the agent-supplied link).
         - 0180-jira-and-salesforce-csv-exports-neutralise-formula-cells.md, "Amends 0155":
           3.6's export bullet; the user's decision that Grist exports keep round-tripping
           through `grist_update_csv`; Alternative: neutralising every export (rejected: a
           leading `'` would be written back into Grist).
      2. ADR 0155: add the Status line in the existing format,
         "Amended by [ADR 0180](0180-jira-and-salesforce-csv-exports-neutralise-formula-cells.md)."; ADR 0108:
         "Amended by [ADR 0177](0177-an-org-mode-mcp-client-needs-the-persons-consent-once.md)."
         docs/adr/README.md: four index rows; the 0155 and 0108 rows' status cells name the amending
         ADR.
      3. Reference docs:
         - docs/org-mode-setup-guide.md: where it explains connecting an MCP client by OAuth, add
           that each person sees a consent page naming the client and where it returns the first
           time they connect that client, and that the choice is remembered per person.
         - docs/security-and-compliance.md: in the step-up section (around :145-150, "accepts a
           fresh sign-in at the identity provider instead of a passkey"), add that the sign-in
           starts only from the card's own button.
         - docs/configuration-reference.md: no new key; check nothing documents
           `/api/approvals/{id}/stepup/idp` as a GET (grep) and fix it if it does.
      4. CHANGELOG.md under "## [Unreleased]" (never a version heading), Security unless noted:
         - "Organization mode: the first time an MCP client signs in for you, PrivacyFence shows
           which client is asking and where it returns, and issues nothing until you allow it."
         - "Organization mode: connecting Google, Slack, Atlassian or Grist, and signing in, must
           be finished in the browser that started them."
         - "Organization mode: the identity-provider check for an approval starts only from the
           approval card."
         - "Hostile client names and connector values can no longer break the Settings page or
           the approval card's markup."
         - "On a computer shared by several people, the live Settings updates go only to the
           owner, and the sign-in link no longer redirects to other sites."
         - "Large or malformed Excel attachments are refused before they are read."
         - "Jira and Salesforce CSV exports start formula-looking cells with `'` so a spreadsheet
           shows them as text."
         - "privacyfence-sync: scripts no longer see a token passed with `--token-env`, and a
           download path outside the work folder is refused."
      5. git rm docs/web-and-org-hardening-plan.md docs/web-and-org-hardening-plan-manual-steps.html;
         grep -rn "web-and-org-hardening-plan" . --include=*.md --include=*.html --include=*.py
         --include=*.yml returns nothing.
      6. Run python3 -m pytest tests/unit/test_code_no_history.py tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py -q and ruff check .
    acceptance:
      - ls docs/adr/0177-*.md docs/adr/0178-*.md docs/adr/0179-*.md docs/adr/0180-*.md lists four files, each containing "Accepted"
      - grep -n "Amended by \[ADR 0180\]" docs/adr/0155-*.md matches and grep -n "Amended by \[ADR 0177\]" docs/adr/0108-*.md matches
      - test ! -e docs/web-and-org-hardening-plan.md && test ! -e docs/web-and-org-hardening-plan-manual-steps.html
      - python3 -m pytest tests/unit/test_code_no_history.py tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py -q passes
```
