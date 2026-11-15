# Plan: keep an expired n8n credential and let a person extend its token

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** In Settings → Scoped tokens every token row gets an **Extend** button. The person types a number of days, confirms (and answers the passkey prompt where the install requires one), and the token expires that many days from today. The token keeps its secret, rules and tools. A token delivered to n8n keeps its n8n credential when it expires, so after Extend the same credential works again with no change in n8n.

**Scope:** The cleanup sweeper stops deleting the n8n credential of an expired token; it still deletes it when the token is revoked or never got delivered. A new store operation, two gated routes (local and organization mode), one UI dialog, an `expired` answer from `privacyfence_runtime_credential_status`, the Admin label "Longest lifetime (days)" becoming "Longest lifetime or extension (days)", and the `privacyfence_status` hint for an expiring token now saying to ask the owner to extend it. Not changed: how tokens are created, revoked or proposed, the n8n API client, the delivery records, the endpoint-moved cleanup.

**Security:** Extend revives a credential that lapsed, so it is a human-only action behind the same gates as creating a token. No AI-client tool can extend a token. A kept credential holds a dead secret until it is extended or revoked; an extended token's secret is exactly the one n8n already holds and is never rotated. The administrator's `max_days` now caps each extension, not the token's total life.

**Review focus:** the sweeper change and its leftover credentials (3.2); that Extend never shortens a token and what `max_days` now means (3.1); the gate parity with create and rename on both routes (3.4); the new `expired` status answer (3.3); the two ADRs and the 0164/0165 amendment (3.6).

## 1. Goal

When a scoped token delivered to n8n expires, PrivacyFence today deletes the n8n credential within five minutes (ADR 0281), so a workflow that stopped because its token ran out cannot be brought back without a new proposal, a new review and a new credential id wired into every MCP Client node. After this change the credential stays in n8n, and a person extends the token with one gated click in Settings. Tracking issue: https://github.com/privacyfence/privacyfence/issues/1098.

## 2. Current state

- `src/privacyfence/runtime_credential_sweeper.py:_sweep_record` deletes the credential and the delivery record of a `delivered` record whose `token_state` is `absent` ("revoked") or `expired` ("expired"). ADR 0281 describes this.
- `src/privacyfence/scoped_tokens.py`: `ScopedToken.expires_at` is fixed at creation (`ScopedTokenStore.create`, `created + days * _SECONDS_PER_DAY`); an expired token stays in `scoped_tokens.json` and `find` stops returning it (`find`, `token.expired(current)`). `rename` (same file) is the pattern to copy for an in-place edit of a stored token: it holds `self._lock`, calls `_refresh`, replaces the entry and calls `_write`.
- `src/privacyfence/runtime_credentials.py:status_for` answers `not_delivered` for a delivered record whose token is not `live`.
- Routes: `src/privacyfence/web/routes_settings.py` has two scoped-token route sets, local mode (`_scoped_token_request`, `ScopedTokenWiring`, routes registered in the `if scoped_tokens is not None` list) and organization mode (`_org_scoped_token_request`, `scoped_token_rename`, routes in the org route list). `_BESPOKE_SENSITIVE_ROUTE_PATHS` lists every token route that earns the step-up gate. `src/privacyfence/web/server.py:build_scoped_token_wiring` builds the local wiring.
- UI: `src/privacyfence/settings_window_html.py:scopedTokenRow` renders Duplicate, Rename and Revoke; `renameScopedToken` is the dialog pattern; the click handler is near `data-st-rename`; the generic action JS in `routes_settings.py` (`scopedRoutes`) maps UI actions to routes and reloads the page on success.

## 3. Design

Decisions confirmed by the owner on the board (ask `a-7ee930d4c220bdb3`, 2026-11-14): keep the credential at expiry; remove it only when the token is revoked; Extend on every token; same gate as create; status reports `expired`.

### 3.1 The store operation

In `scoped_tokens.py`:

```python
EXTEND_NOT_LONGER = "The token already lasts that long. Choose more days."

def extend(
    self, principal: Principal, token_id: str, days: int, *, policy: ScopedTokenPolicy, now: float | None = None,
) -> tuple[float, ScopedToken] | None:
```

- When `not policy.enabled`, raise the same `ScopedTokenError` text `create` raises through `_prepare` (`Scoped tokens are not enabled on this install. Your administrator turns them on on the Admin page.`), before anything else, so no future caller of the store can revive a token on an install that turned tokens off.
- `days` outside `1..policy.max_days` raises `ScopedTokenError(f"A token lasts 1 to {policy.max_days} days.")` (the text `_check_locked` uses).
- Under `self._lock`, after `self._refresh(principal)`: find the token by id; `None` when absent. The new expiry is `(time.time() if now is None else now) + days * _SECONDS_PER_DAY`. When it is not later than the token's current `expires_at`, raise `ScopedTokenError(EXTEND_NOT_LONGER)`: Extend never shortens a token. Otherwise replace the entry with `dataclasses.replace(token, expires_at=new)`, `self._write(principal)`, and return `(old_expires_at, extended_token)`.
- The id, label, digest, secret, rules, tools and flags are untouched, so the `_index` needs no change and a client holding the secret works again at once. The cap is "days from today", so a token never ends more than `max_days` after an extension. That changes what the administrator's `max_days` means (ADR 0164 says a token "expires on its own", ADR 0165 that it lives at most the cap): it now caps each extension, and repeated extends keep one secret alive. The Admin label "Longest lifetime (days)" becomes "Longest lifetime or extension (days)" (p4) and the ADR records the change (3.6).
- Rejected: adding days to the old expiry (a long-expired token would stay expired, a live one could exceed `max_days`); minting a new secret on extend (it would break every holder, and for n8n it would need a credential update call, a new failure mode).

### 3.2 The sweeper keeps an expired credential

`_sweep_record`, `DELIVERED` branch: delete only when `token_state` is `absent` (reason string `"revoked"`); `expired`, `live` and `unknown` leave the credential and record alone. Everything else in the sweeper is unchanged (a `creating` record, an `abandoned` record, the endpoint-moved path, the delete-failure audit). Update the module docstring and `ADR 0281`'s status line (3.6).

- A credential for a token that is never extended or revoked stays in n8n and in `agent_runtime_credentials.json` indefinitely. The owner chose this (revoke is the cleanup, the same as expired tokens in PrivacyFence today). The token page shows the row as Expired, so the person can revoke it, which deletes the credential within five minutes.
- Credentials the old sweeper already deleted for tokens that expired before this change are not recreated. Extending such a token revives it in PrivacyFence only; the person asks the AI client to propose a new delivery. State this in the ADR's Consequences.
- Rejected: deleting after 30 days (an extra timer and a second rule to explain for no stated need); deleting and recreating the credential on Extend (needs the secret again, which PrivacyFence never keeps).

### 3.3 The status tool says `expired`

`runtime_credentials.status_for`: for a delivered record read the token once (`token_store.token_state` only to turn `unknown` and `absent` into `not_delivered`, then the token from `tokens_for`) and decide from `token.expired(now)` on that one copy, so an extend between two reads can never produce an `expired` answer with a future expiry. A live token's answer is unchanged; an expired token answers

```python
{"state": "expired", "runtime": "n8n", "credential_id": ..., "credential_name": ..., "credential_type": "httpBearerAuth",
 "node_authentication": "bearerAuth", "mcp_url": ..., "token_expired_at": <ISO 8601 UTC of token.expires_at>,
 "message": EXPIRED_MESSAGE}
```

with `EXPIRED_MESSAGE = "The token has expired, but its n8n credential is kept. Ask the person to open PrivacyFence Settings, then Scoped tokens, and press Extend on this token; the same credential then works again. Do not propose a new token."`. It still never returns a token id, digest or secret, or the runtime's host. The tool description in `web/mcp_tools.py` (`RUNTIME_CREDENTIAL_STATUS_TOOL`) adds the `expired` state to its list of states. The older `privacyfence_status` line for an expiring scoped token (`web/routes_mcp.py`, `ask its owner for a new one`) now reads `ask its owner to extend it in PrivacyFence Settings, Scoped tokens.`, so the two messages agree.

### 3.4 The routes and the gate

New action `extend_scoped_token`, route `POST /api/settings/scoped_tokens/extend`, in both modes, built from the existing gates exactly like rename and revoke: signed-in person, CSRF, origin, `confirmed: true`, the human-session requirement where `require_human_session` is on (local), the "scoped tokens enabled" check (org), and the passkey step-up where the install requires it. Body: `{"token_id": str, "days": int, "confirmed": true, "csrf": ..., "webauthn_assertion": ...?}`; `days` that is a bool or not an int is an invalid request (400). Step-up binds to `{"token_id", "days"}`.

Answers: `200 {"extended": {"id", "label", "expires_at"}}`; `404 {"error": "No such token."}`; `400 {"error": <ScopedTokenError text>}` for days out of range or `EXTEND_NOT_LONGER`. Audit entries (the same `_record_settings_audit` as rename): local `Extended scoped token '<label>' (<id>) to <YYYY-MM-DD>, <days> days`; organization the same text plus `, delivered to n8n as '<credential_name>'` when a delivered record exists for the token, then ` (principal=<id>)`.

The path goes into `_BESPOKE_SENSITIVE_ROUTE_PATHS` (the existing coverage test fails otherwise). `ScopedTokenWiring` gains `store_extend: Callable[[str, int], tuple[float, ScopedToken] | None]`; `server.build_scoped_token_wiring` builds it from `scoped_tokens.get_store().extend(LOCAL_PRINCIPAL, token_id, days, policy=policy)`. In the settings JS, `scopedRoutes` gains `extend_scoped_token: ['extend', null]`: no `window.confirm`, the dialog is the confirmation, as for rename.

No AI-client tool, and no MCP path, can extend a token: the proposal tool still only creates.

### 3.5 The UI

Each token row gets an **Extend** button between Rename and Revoke (`class="button primary"` when the token is expired, `class="button"` otherwise), `aria-label="Extend <label>"`, `data-st-extend="<token id>"`. Clicking opens a dialog built like `renameScopedToken`: title `Extend token`; description `The token expires this many days from today. Its secret, rules and tools stay the same.` plus, when the token has a delivered credential, ` Its n8n credential '<name>' works again.`; a number input (`min` 1, `max` `SCOPED.max_days`, value `SCOPED.default_days`, `aria-label="Days from today"`); buttons Cancel and Extend. An invalid number keeps the dialog open and sets `aria-invalid`. Submit posts `extend_scoped_token` with `{token_id, days}`; the page reloads on success and an error from the server is shown by the existing alert path. For an expired delivered token the existing "In n8n as ..." hint gains ` · kept until you extend or revoke the token`.

### 3.6 ADRs

Two ADRs, one decision each (docs/adr/README.md rule 1):

- **0283: Only a person can extend a scoped token, from today and capped by `max_days`.** Amends 0164 and 0165: `max_days` now caps each extension, not the total life of a token, and the secret is never rotated. Alternatives: adding days to the old expiry; extending only delivered tokens; an always-passkey gate; minting a new secret on extend.
- **0284: An expired token's n8n credential is kept until the token is revoked.** Amends 0281 (the sweeper no longer deletes at expiry) and 0280 (the `expired` state). Alternatives: delete-and-recreate on extend; auto-delete 30 days after expiry.

The Status section of 0164, 0165, 0280 and 0281 gains one `Amended by` line each.

## 4. ADRs

- 0283: Only a person can extend a scoped token, from today and capped by `max_days` (amends 0164, 0165).
- 0284: An expired token's n8n credential is kept until the token is revoked (amends 0280, 0281).

## 5. Manual steps

### After implementation

**ma1-extend-on-a-real-n8n-server** (about 10 minutes). CI cannot reach a real n8n or a real passkey.

1. On the organization server with n8n delivery turned on, open `https://<your PrivacyFence host>/settings/scoped_tokens` signed in as yourself. You see your tokens; a delivered one shows `In n8n as '<name>'`.
2. If you have a token that has already expired, first ask the AI client to call `privacyfence_runtime_credential_status` for its proposal: it answers `expired`. Then continue with that token below; otherwise use a live delivered token. On the token press **Extend**. A dialog titled "Extend token" opens with the number of days prefilled.
3. Enter more days than the token has left and press **Extend**. If your install requires a passkey, the browser asks for it. The page reloads and the row shows the new expiry date.
4. Open `https://<your PrivacyFence host>/admin` and the audit log there (or the audit export). You see `Extended scoped token '<label>' (<id>) to <date>, <n> days, delivered to n8n as '<name>'`.
5. In n8n open `https://<your n8n host>/home/credentials` and find the credential named in the row: it is still there, unchanged.
6. If you used an expired token, ask the AI client to call `privacyfence_runtime_credential_status` again: it now answers `delivered`.

Report back: one pass/fail line for the pull request, e.g. `ma1: pass, extended <label>` (add `, status expired -> delivered` if you used an expired token). Setup background: https://github.com/privacyfence/privacyfence/blob/feature/extend-n8n-credentials/docs/org-mode-setup-guide.md.

## 6. Risks and open questions

- The browser test for the dialog (`tests/integration/test_settings_scoped_token_form_browser.py`) skips when no Chromium launches. A worker that sees it skip reports that in its phase result; a skip is not a pass.
- Tests that assert the old behaviour (an expired delivered credential is deleted) must change; that is intended. A worker that finds a test failing for any other reason stops with `status=blocked`.
- If the new `_BESPOKE_SENSITIVE_ROUTE_PATHS` entry or the route table breaks a gate-coverage test in a way the brief does not describe, stop with `status=blocked`.
- Existing deployments: credentials the old sweeper deleted stay deleted (3.2).
- Cross-process lost update: the `privacyfence-app --revoke-scoped-tokens` command writes the token file from another process. If it lands between `extend`'s `_refresh` and `_write`, the extend writes the token back and the revoke is lost, and the n8n credential is then never deleted. The window is tiny and already exists for `rename` and `create`; it is not fixed here. ADR 0283 states it in Consequences, and the offboarding advice there is to run the revoke again if a token reappears.
- Dead credentials in n8n keep calling `/mcp` with a dead secret and get 401s; there is no lockout, only log volume.
- A credential for an abandoned, never-extended token is cleaned up only by revoke. That is the decision, noted here so a reviewer does not mistake it for a leak.

## Implementation manifest

```yaml
plan_slug: extend-n8n-credentials
feature_branch: feature/extend-n8n-credentials
tracking_issue: 1098
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-extend-on-a-real-n8n-server
    title: Extend a token delivered to n8n on a real organization server
    why: CI has no real n8n server and no real passkey, so the end-to-end Extend flow and the credential staying in n8n cannot be checked there.
verify_after_merge:
  - python3 -m pytest tests/unit -q
final_checks:
  - "`tld plan deviations check --file docs/1098/deviations.md --plan docs/1098/plan.md --all` exits 0"
  - "docs/adr/0283-only-a-person-can-extend-a-scoped-token-from-today-capped-by-max-days.md and docs/adr/0284-an-expired-tokens-n8n-credential-is-kept-until-the-token-is-revoked.md exist, are Accepted, and are in docs/adr/README.md"
  - "CHANGELOG.md has an entry under `## [Unreleased]` and no new version heading"
phases:
  - id: p1-store-extend-and-keep-expired
    title: Add the store's extend operation and stop the sweeper deleting an expired credential
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/runtime_credential_sweeper.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_runtime_credential_sweeper.py
    brief: |
      Design: plan sections 3.1 and 3.2. Every new test's name contains the word `extend`.
      1. `src/privacyfence/scoped_tokens.py`: add the module constant `EXTEND_NOT_LONGER = "The token already lasts that long. Choose more days."` next to the other module constants, and the method `ScopedTokenStore.extend(self, principal, token_id, days, *, policy, now=None) -> tuple[float, ScopedToken] | None` directly after `rename`, exactly as plan section 3.1 describes, in this order: the enabled check (step 2a), then the range check (`ScopedTokenError(f"A token lasts 1 to {policy.max_days} days.")`), then under `self._lock` and `self._refresh(principal)` find the token (return `None` when absent), compute `new = (time.time() if now is None else now) + days * _SECONDS_PER_DAY`, raise `ScopedTokenError(EXTEND_NOT_LONGER)` when `new <= token.expires_at`, otherwise `replace(token, expires_at=new)` into the list (as `rename` does), `self._write(principal)`, and return `(token.expires_at, replaced)`. Give it a docstring of one or two sentences saying only the expiry changes and it never shortens.
      2. `src/privacyfence/runtime_credential_sweeper.py`: in `_sweep_record` change the `DELIVERED` branch to delete only when `state == "absent"`, with reason `"revoked"`, and do nothing for any other state. Rewrite the module docstring's second paragraph so it says an expired token's credential is kept until the token is revoked or extended (ADR 0284), and remove the now-unused `"expired"` reason.
      2a. The enabled check: at the start of `extend`, raise `ScopedTokenError("Scoped tokens are not enabled on this install. Your administrator turns them on on the Admin page.")` when `not policy.enabled` (the text `_prepare` uses; import or copy it from there without changing `_prepare`'s behaviour).
      3. `tests/unit/test_scoped_tokens.py`, class `TestStore` (pattern: `test_rename_changes_only_the_label`): add tests that extend (a) moves `expires_at` to `now + days*86400`, keeps id, digest, label, rules, tools and flags, and `store.find(secret, now)` returns the extended token; (b) revives an expired token (`find` returns None before, the token after); (c) persists (a fresh `_store()` reads the new expiry); (d) raises `ScopedTokenError` with `EXTEND_NOT_LONGER` for days that do not end later than the current expiry; (e) raises the `A token lasts 1 to <max> days.` text for 0 and for `max_days + 1`; (f) returns `None` for an unknown id and for another person's token id; (g) returns the old expiry as the first tuple item; (h) raises the not-enabled text for a policy with `enabled=False`.
      4. `tests/unit/test_runtime_credential_sweeper.py`, class `TestRules`: change `test_delivered_and_expired_is_deleted_as_expired` into `test_delivered_and_expired_is_kept_for_extend`: after `_sweep(..., now=LATER)` the record is still there, `api.deleted == []` and `_decisions(tmp_path) == []`. Add `test_extend_after_expiry_keeps_the_credential_usable` (token expired, sweep, `tokens.extend(...)`, sweep again at a time before the new expiry: still one record, nothing deleted) and `test_a_revoked_expired_token_deletes_its_credential_as_revoked_for_extend` (expired, sweep, `tokens.revoke`, sweep: record gone, `api.deleted` has the credential id, the audit summary ends with `token revoked`). Keep `test_delivered_and_absent_is_deleted_as_revoked` as it is.
      5. Stop with `status=blocked` if any other test in `tests/unit` fails for a reason other than the old expired-deletes assertion.
    acceptance:
      - "`python3 -m pytest tests/unit/test_scoped_tokens.py tests/unit/test_runtime_credential_sweeper.py -q` passes"
      - "`grep -n '\"expired\"' src/privacyfence/runtime_credential_sweeper.py` prints nothing"
      - "`python3 -m pytest tests/unit -q -k extend` passes"
      - "`ruff check src/privacyfence/scoped_tokens.py src/privacyfence/runtime_credential_sweeper.py tests/unit` passes"
  - id: p2-status-expired
    title: Report an expired delivered token as `expired` from the status tool
    depends_on: [p1-store-extend-and-keep-expired]
    complexity: S
    touches:
      - src/privacyfence/runtime_credentials.py
      - src/privacyfence/web/mcp_tools.py
      - tests/unit/test_runtime_credentials.py
      - tests/unit/web/test_mcp_tools.py
      - src/privacyfence/web/routes_mcp.py
      - tests/unit/web/test_routes_mcp.py
    brief: |
      Design: plan section 3.3. Every new test's name contains the word `extend`.
      1. `src/privacyfence/runtime_credentials.py`: add the constant `EXPIRED_MESSAGE` with exactly the text in plan section 3.3. Rewrite the delivered-record branch of `status_for` so that it reads `state = token_store.token_state(principal, record.token_id, now)`; any state other than `live` or `expired` returns `not_delivered`; then it looks the token up once as today (`None` returns `not_delivered`) and decides everything after that from `token.expired(now)` on that one token, never from the earlier `state` (so an extend between the two reads cannot give an `expired` answer with a future expiry); then it builds the shared fields (`runtime`, `credential_id`, `credential_name`, `credential_type`, `node_authentication`, `mcp_url`); a token that is not expired returns the existing answer unchanged (`state: delivered`, `token_expires_at`); an expired one returns `state: expired`, the shared fields, `token_expired_at` (the same ISO 8601 UTC form as `token_expires_at`) and `message: EXPIRED_MESSAGE`. Update the docstring of `status_for` to mention `expired`.
      2. `src/privacyfence/web/mcp_tools.py`, `RUNTIME_CREDENTIAL_STATUS_TOOL` description: after the `'pending'` clause add `'expired' when the token ran out: its credential is kept and the person can press Extend on the token in PrivacyFence Settings, Scoped tokens (do not propose a new one)`; keep the rest and keep the description one string built the way it is now.
      3. `tests/unit/test_runtime_credentials.py`, class `TestStatusFor`: replace `test_an_expired_token_is_not_delivered` by `test_an_expired_token_is_reported_expired_for_extend`: deliver, ask at `expires_at + 1`, expect exactly the dict of plan section 3.3 (credential id and name from the outcome's record, `mcp_url` `https://pf/mcp`) and that `outcome.token.id` is not in `json.dumps(answer)`. Add `test_an_extended_token_is_delivered_again_for_extend` (deliver, extend with `tokens.extend(ALICE, id, 30, policy=..., now=...)`, ask after the old expiry: `state == "delivered"` with the new `token_expires_at`). Keep the revoked, unknown-id and vanishing-token tests as they are.
      4. `tests/unit/web/test_mcp_tools.py`: add `test_the_status_tool_description_names_expired_for_extend` asserting `'expired'` and `Extend` appear in `RUNTIME_CREDENTIAL_STATUS_TOOL.description`.
      5. `tests/unit/web/test_mcp_dispatch.py` needs no change: no test there asserts an expired answer.
      5a. `src/privacyfence/web/routes_mcp.py` (the `expiring_soon` branch of the `privacyfence_status` result): change the appended text to `f" This token expires in {days_left} day(s); ask its owner to extend it in PrivacyFence Settings, Scoped tokens."`, and update the one assertion that pins the old text in `tests/unit/web/test_routes_mcp.py` (search `ask its owner for a new one`; the test name must keep or gain the word `extend` only if you rename it; do not rename it).
      6. Stop with `status=blocked` if the tools-reference doc test (`tests/unit/test_docs_tools_reference.py`) fails: this phase must not need a doc regeneration.
    acceptance:
      - "`python3 -m pytest tests/unit/test_runtime_credentials.py tests/unit/web/test_mcp_tools.py tests/unit/web/test_mcp_dispatch.py tests/unit/web/test_routes_mcp.py -q` passes"
      - "`grep -n 'EXPIRED_MESSAGE' src/privacyfence/runtime_credentials.py` matches the definition and its use"
      - "`python3 -m pytest tests/unit -q -k extend` passes"
      - "`ruff check src/privacyfence tests/unit` passes"
  - id: p3-extend-routes
    title: Add the gated extend route in local and organization mode
    depends_on: [p1-store-extend-and-keep-expired]
    complexity: M
    touches:
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/server.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_routes_org_settings.py
    brief: |
      Design: plan section 3.4. Pattern to copy: the rename route in both modes (`scoped_token_rename`) and its tests. Every new test's name contains the word `extend`.
      1. `src/privacyfence/web/routes_settings.py`: add `_scoped_extend_fingerprint_body(payload)` next to `_scoped_rename_fingerprint_body`: returns `{"token_id": token_id, "days": days}` when `token_id` is a `str` and `days` is an `int` that is not a `bool`, else `None`.
      2. Add `"/api/settings/scoped_tokens/extend"` to `_BESPOKE_SENSITIVE_ROUTE_PATHS` with a one-line comment that extending revives a lapsed credential, so it goes through the same gates as creating one.
      3. Add the field `store_extend: Callable[[str, int], tuple[float, ScopedToken] | None]` to `ScopedTokenWiring` right after `store_rename`, and one sentence in its docstring: it takes the token id and the number of days and returns the old expiry and the extended token (or `None` for an unknown id), raising `ScopedTokenError` like `store_create`.
      4. Local mode: add `async def scoped_token_extend(request)` next to the local `scoped_token_rename`: `_scoped_token_request(request, "extend_scoped_token", "extend a scoped token", _scoped_extend_fingerprint_body)`; call `scoped_tokens.store_extend(bound["token_id"], bound["days"])` through `run_in_threadpool`; `ScopedTokenError` returns `JSONResponse({"error": str(exc)}, status_code=400)`; `None` returns 404 `{"error": "No such token."}`; success audits with `_record_settings_audit(LOCAL_PRINCIPAL, f"Extended scoped token '{token.label}' ({token.id}) to {date}, {days} days")` inside `principal_scope(LOCAL_PRINCIPAL)` where `date` is `datetime.fromtimestamp(token.expires_at, timezone.utc).strftime("%Y-%m-%d")` (`datetime` and `timezone` are already imported in this module), and answers `{"extended": {"id", "label", "expires_at"}}`. Register `Route("/api/settings/scoped_tokens/extend", scoped_token_extend, methods=["POST"])` in the `if scoped_tokens is not None:` list.
      5. Organization mode: add `scoped_token_extend` next to the org `scoped_token_rename`: `_org_scoped_token_request(request, "extend_scoped_token", _scoped_extend_fingerprint_body)`; `store = scoped_token_store or get_store()`; call `store.extend(principal, bound["token_id"], bound["days"], policy=token_policy)` through `run_in_threadpool`, same 400/404 answers; audit text as the local one plus, when `runtime_credentials.get_store().for_token(principal.id, token.id)` returns a record whose `state == runtime_credentials.DELIVERED`, the suffix `, delivered to n8n as '<runtime_credentials.strip_control(record.credential_name)>'`, then ` (principal={principal.id})`, inside `principal_scope(principal)`. Register the route in the organization route list after the rename route.
      6. In the settings JS string (`scopedRoutes` in `_settings_bridge_shim`'s action script) add `extend_scoped_token: ['extend', null]` after `rename_scoped_token`.
      7. `src/privacyfence/web/server.py`, `build_scoped_token_wiring`: add `_extend(token_id, days)` that runs `scoped_tokens.get_store().extend(LOCAL_PRINCIPAL, token_id, days, policy=policy)` inside `principal_scope(LOCAL_PRINCIPAL)` and pass `store_extend=_extend` to `ScopedTokenWiring`.
      8. `tests/unit/web/test_routes_settings.py`: extend the `_scoped_wiring` helper with an `extend_error` keyword and a `_extend(token_id, days)` that appends `("extend", token_id, days)` to `calls` and returns `(100.0, _scoped_token(token_id))` (or `None` when `known` is false, or raises `ScopedTokenError(extend_error)`); add `EXTEND = "/api/settings/scoped_tokens/extend"` to `TestScopedTokenRoutes`; add tests that extend answers `{"extended": {...}}` and audits `Extended scoped token`, answers 404 for an unknown token, shows a validation text as 400, answers 400 for a body with a bool `days` or a missing `days`, is refused without `confirmed`, and is covered by the gate test `test_create_needs_confirmation_a_human_session_and_the_passkey`: add `EXTEND` to its parametrize list (its BODY already carries `token_id` and `days`). Add `"extend"` to the tuple in `test_the_shim_reloads_after_these_routes_instead_of_rendering_state` that loops over `("create", "revoke", "revoke_all", "rename")` and an assertion that the page script contains `extend_scoped_token: ['extend', null]`.
      9. `tests/unit/web/test_routes_org_settings.py`, next to `test_rename`: add `test_extend` (create a token with `days: 5`, extend it to 30 days: 200, `expires_at` later than before, the store's token has the new expiry and the same digest; another person's token id: 404 `No such token.`; days that do not end later (`days: 1` on a 5-day token): 400 with `scoped_tokens.EXTEND_NOT_LONGER`; days above the policy `max_days`: 400 with the range text; the audit entry reads `Extended scoped token '<label>' (<id>) to <date>, 30 days (principal=alice)`), `test_extend_is_refused_when_scoped_tokens_are_disabled` (403, like the rename one) and `test_extend_audits_the_n8n_credential_of_a_delivered_token` (add a `RuntimeCredential` record in state `DELIVERED` for the token through `runtime_credentials.get_store()`, extend, expect the audit text to end `, delivered to n8n as '<name>' (principal=alice)`).
      10. Stop with `status=blocked` if a gate-coverage test other than the ones named here fails after step 2.
    acceptance:
      - "`python3 -m pytest tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py -q` passes"
      - "`grep -n 'scoped_tokens/extend' src/privacyfence/web/routes_settings.py` matches the sensitive-path entry and both route registrations"
      - "`python3 -m pytest tests/unit -q -k extend` passes"
      - "`ruff check src/privacyfence tests/unit` passes and `python3 -m mypy src/privacyfence` passes"
  - id: p4-extend-ui
    title: Add the Extend button and its dialog to Settings
    depends_on: [p3-extend-routes]
    complexity: M
    touches:
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_settings_window_html.py
      - tests/integration/test_settings_scoped_token_form_browser.py
    brief: |
      Design: plan section 3.5. Pattern to copy: `renameScopedToken` and its click handler, and the test `test_a_token_can_be_renamed` in `tests/unit/test_settings_window_html.py`. Every new test's name contains the word `extend`.
      1. `src/privacyfence/settings_window_html.py`, `scopedTokenRow`: add an Extend button between Rename and Revoke: `<div class="button' + (t.expired ? ' primary' : '') + '" role="button" tabindex="0" aria-label="Extend ' + esc(t.label) + '" data-st-extend="' + esc(t.id) + '">Extend</div>`.
      1a. In `renderTokens` (the admin Scoped tokens section, the `numberFieldRow('Longest lifetime (days)', 'From 1 to 365.', 'max_days', ...)` call) change the label to `Longest lifetime or extension (days)` and the help text to `From 1 to 365. A person can also extend a token by up to this many days from today.`; update any assertion in `tests/unit/test_settings_window_html.py` that pins the old label (search `Longest lifetime`).
      2. In the same `scopedTokenRow`, in the `t.runtime_credential` hint, append `' · kept until you extend or revoke the token'` to `inRuntime` when `t.expired`.
      3. Add `function extendScopedToken(tokenId)` right after `renameScopedToken`, built the same way (overlay, `pf-modal` box, Enter/Escape only on the input, close removes the overlay): title `Extend token`; description `The token expires this many days from today. Its secret, rules and tools stay the same.` plus, when `t.runtime_credential` is set, ` Its n8n credential '` + name + `' works again.` (set through `textContent`); an `<input type="number">` with class `field`, `min` `1`, `max` `String(SCOPED.max_days)`, `step` `1`, value `String(SCOPED.default_days)`, `aria-label` `Days from today`; Cancel and `Extend` buttons (`aria-label` `Cancel extend` and `Confirm extend`). On confirm parse the field with `parseInt(value, 10)`; when it is not an integer from 1 to `SCOPED.max_days`, set `aria-invalid` to `true` on the field and return without closing; otherwise close and `post('extend_scoped_token', { token_id: tokenId, days: days })`.
      4. In the click handler next to `data-st-rename`, add `var stExtendEl = e.target.closest('[data-st-extend]'); if (stExtendEl) { extendScopedToken(stExtendEl.getAttribute('data-st-extend')); return; }`.
      5. `tests/unit/test_settings_window_html.py` next to `test_a_token_can_be_renamed`: add `test_a_token_can_be_extended` asserting the built html contains `data-st-extend="`, `aria-label="Extend ' + esc(t.label)`, `extend_scoped_token`, `Days from today`, `Extend token` and `kept until you extend or revoke the token`.
      6. `tests/integration/test_settings_scoped_token_form_browser.py`: add a browser test `test_extend_dialog_posts_the_days` following the pattern of `test_a_token_is_renamed_from_the_modal` in that file (the page is built with `build_html`, posts are captured by `_capture_posts` into `window.__posted`): click `[aria-label="Extend Daily sync 2"]` (the expired token `st-2` in `DATA`), fill `.pf-modal input[aria-label="Days from today"]` with `30`, click `.pf-modal [aria-label="Confirm extend"]`, and assert `window.__posted == [{"action": "extend_scoped_token", "token_id": "st-2", "days": 30}]` and that `.pf-modal` is gone. Add a second test that an out-of-range value (`0`) posts nothing and leaves the dialog open with `aria-invalid="true"`. When no Chromium launches the test skips, as the file's other tests do; do not add a skip of your own and do not weaken it.
      7. Stop with `status=blocked` if the CSP or inline-handler tests fail: the dialog uses no inline handler.
    acceptance:
      - "`python3 -m pytest tests/unit/test_settings_window_html.py -q` passes and `grep -n 'Longest lifetime or extension' src/privacyfence/settings_window_html.py` matches"
      - "`python3 -m pytest tests/integration/test_settings_scoped_token_form_browser.py -q` passes or reports only skips for the missing browser, and the phase result says which"
      - "`grep -c 'data-st-extend' src/privacyfence/settings_window_html.py` prints 2 or more"
      - "`python3 -m pytest tests/unit -q -k extend` passes"
      - "`ruff check src/privacyfence tests` passes"
  - id: p5-retire-adr-docs-changelog
    title: Write ADRs 0283 and 0284, update the reference docs and the changelog
    depends_on: [p2-status-expired, p4-extend-ui]
    complexity: S
    touches:
      - docs/adr/0283-only-a-person-can-extend-a-scoped-token-from-today-capped-by-max-days.md
      - docs/adr/0284-an-expired-tokens-n8n-credential-is-kept-until-the-token-is-revoked.md
      - docs/adr/README.md
      - docs/adr/0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md
      - docs/adr/0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md
      - docs/adr/0280-privacyfence-runtime-credential-status-reports-a-delivered-credential-to-its-proposer.md
      - docs/adr/0281-a-sweeper-deletes-the-credential-of-a-revoked-expired-or-undelivered-token.md
      - docs/org-mode-setup-guide.md
      - docs/security-and-compliance.md
      - docs/how-it-works.md
      - docs/configuration-reference.md
      - CHANGELOG.md
    brief: |
      Design: plan sections 3 and 4. Read `docs/adr/0281-*.md` first for the ADR layout (Status, Context, Decision, Alternatives considered, Consequences, Verification, Related). ADRs never link a plan (docs/adr/README.md rule 4): link issue 1098 and source files only.
      1. Create `docs/adr/0283-only-a-person-can-extend-a-scoped-token-from-today-capped-by-max-days.md`: title `ADR 0283: Only a person can extend a scoped token, from today and capped by max_days`; Status `Accepted — 2026-11-14. Implemented.` and `Amends ADR 0164 and ADR 0165.`; Context: a token's expiry was fixed at creation, so a lapsed token could only be replaced by a new one with a new secret; Decision: Extend on every scoped token in both modes; the new expiry is today plus the chosen days, 1 to `max_days`, and never earlier than the current expiry; same secret, rules and tools; same gates as creating a token (signed-in person, CSRF, origin, confirmation, passkey step-up where required); no AI-client or MCP path; `max_days` now caps each extension and not the total life of a token, and the Admin label says so; Alternatives considered: adding days to the old expiry, extending only delivered tokens, an always-passkey gate, minting a new secret on extend; Consequences: repeated extends keep one secret alive with no rotation; the offboarding command `privacyfence-app --revoke-scoped-tokens` writes the token file from another process, so an extend landing in the same instant can undo a revoke (the window already exists for rename and create): run the revoke again if a token reappears; Verification: `TestStore` (`tests/unit/test_scoped_tokens.py`), `TestScopedTokenRoutes` (`tests/unit/web/test_routes_settings.py`), `test_extend` (`tests/unit/web/test_routes_org_settings.py`), `test_a_token_can_be_extended` (`tests/unit/test_settings_window_html.py`); Related: issue 1098, `src/privacyfence/scoped_tokens.py`, `src/privacyfence/web/routes_settings.py`, ADRs 0164, 0165.
      1a. Create `docs/adr/0284-an-expired-tokens-n8n-credential-is-kept-until-the-token-is-revoked.md`: title `ADR 0284: An expired token's n8n credential is kept until the token is revoked`; Status `Accepted — 2026-11-14. Implemented.` and `Amends ADR 0280 and ADR 0281.`; Context: ADR 0281 deleted the credential at expiry, so a lapsed workflow needed a new proposal and a new credential id; Decision: the sweeper deletes a delivered credential only when its token is revoked (absent); an expired token's credential and record are kept so Extend revives the same credential; `privacyfence_runtime_credential_status` answers `expired` with `token_expired_at` and a message asking the person to extend; Alternatives considered: delete and recreate the credential on Extend, auto-delete 30 days after expiry; Consequences: a dead secret stays in n8n until extended or revoked; a credential the old sweeper already deleted is not recreated, so such a token needs a new delivery; dead credentials keep getting 401s from `/mcp`; Verification: `TestRules` (`tests/unit/test_runtime_credential_sweeper.py`), `TestStatusFor` (`tests/unit/test_runtime_credentials.py`); Related: issue 1098, `src/privacyfence/runtime_credential_sweeper.py`, `src/privacyfence/runtime_credentials.py`, ADRs 0278, 0280, 0281, 0283.
      2. `docs/adr/README.md`: add two rows after the 0282 row in the same format, and add `; amended by 0283` to the Status cell of the 0164 and 0165 rows and `; amended by 0284` to the 0280 and 0281 rows: `| [0283](<file name>) | Only a person can extend a scoped token, from today and capped by max_days | Accepted; amends 0164, 0165 |` and `| [0284](<file name>) | An expired token's n8n credential is kept until the token is revoked | Accepted; amends 0280, 0281; relates to 0283 |`.
      3. In `docs/adr/0164-*.md` and `docs/adr/0165-*.md` add to the Status section only the line `Amended by ADR 0283 (0283-only-a-person-can-extend-a-scoped-token-from-today-capped-by-max-days.md).`; in `docs/adr/0280-*.md` and `docs/adr/0281-*.md` add to the Status section only the line `Amended by ADR 0284 (0284-an-expired-tokens-n8n-credential-is-kept-until-the-token-is-revoked.md).`
      4. `docs/org-mode-setup-guide.md` (the sentence near line 1153 that says revoking or expiry deletes the credential within 5 minutes), `docs/security-and-compliance.md` (the Offboarding bullet near line 436, and the status-tool bullet near line 430, which must say a revoke turns the answer into `not_delivered` and an expiry into `expired`, carrying `token_expired_at` and a message asking the person to extend) and `docs/configuration-reference.md` (the `agent_runtime_credentials.json` row near line 500): say that revoking the token deletes the n8n credential within 5 minutes, and that an expired token's credential is kept until the person extends the token (Settings → Scoped tokens → Extend) or revokes it. `docs/how-it-works.md` (the `privacyfence_runtime_credential_status` row near line 200): add the `expired` state and that it carries `token_expired_at` and a message asking the person to extend. In `docs/configuration-reference.md` also find the `scoped_tokens.max_days` text (the row near line 423 and the CLI row near line 317) and say that `max_days` is the longest lifetime of a new token and the most a person can extend one by. Run `grep -rn "Rename" docs/*.md` and add one sentence about **Extend** wherever a doc lists a scoped token row's buttons; add nothing where none does.
      5. `CHANGELOG.md`: under `## [Unreleased]`, `### Changed`, add one bullet: `**A token delivered to n8n keeps its credential when it expires, and a person can extend the token.** Settings → Scoped tokens has an **Extend** button on every token: it sets the token to expire the chosen number of days from today, behind the same confirmation and passkey step-up as creating a token. An expired token's n8n credential is no longer deleted; it is deleted when the token is revoked, and works again when the token is extended. \`privacyfence_runtime_credential_status\` answers \`expired\` for such a token (ADR 0283 (docs/adr/0283-only-a-person-can-extend-a-scoped-token-from-today-capped-by-max-days.md), ADR 0284 (docs/adr/0284-an-expired-tokens-n8n-credential-is-kept-until-the-token-is-revoked.md)).` Create the `### Changed` heading under `## [Unreleased]` after `### Added` if it is not there. Do not open a version heading.
      6. Run `python3 -m pytest tests/unit/test_docs_tools_reference.py -q` and `ruff check .`; both pass.
    acceptance:
      - "`test -f docs/adr/0283-only-a-person-can-extend-a-scoped-token-from-today-capped-by-max-days.md && test -f docs/adr/0284-an-expired-tokens-n8n-credential-is-kept-until-the-token-is-revoked.md` succeeds and `grep -c '0283\\|0284' docs/adr/README.md` prints 2 or more"
      - "`grep -ln 'Amended by \\[ADR 028[34]\\]' docs/adr/0164-*.md docs/adr/0165-*.md docs/adr/0280-*.md docs/adr/0281-*.md` lists all four files"
      - "`grep -n 'ADR 0283' CHANGELOG.md` matches one line under `## [Unreleased]`"
      - "`grep -n 'letting it expire, deletes' docs/org-mode-setup-guide.md docs/security-and-compliance.md docs/configuration-reference.md` prints nothing"
      - "`ruff check .` passes"
```
