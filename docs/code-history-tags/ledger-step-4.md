# Ledger: step 4

## ADR candidates

1. **Step-up is a WebAuthn passkey, with IdP re-authentication as org mode's fallback, which
   `step_up.require_passkey` closes.** The plan item behind this was cited as "D7". Where the
   fallback may use `acr_values`, it does. Files: `src/privacyfence/org_identity.py`
   (`IdpConfig.step_up_acr_values`), `src/privacyfence/web/routes_org_stepup.py`,
   `scripts/build_org_bundle.py` (the `WebAuthn step-up` group). It meets the bar because it is a
   trust boundary: what may release a gated write in org mode. Rejected alternative: IdP
   re-authentication as the only step-up. ADR 0055 covers enrollment only.
2. **The audit log's integrity comes from a keyed hash chain whose key sits next to the log, plus
   off-host forwarding as the real defence against a privileged local tamperer.** This was cited
   as "SEC-23". Files: `src/privacyfence/audit_log.py` (module docstring,
   `_load_or_create_chain_key`'s threat model), `src/privacyfence/audit_forwarding.py`,
   `scripts/verify_audit_log.py`. It meets the bar because it is a tamper-evidence boundary with an
   honest limit that is not obvious. The alternative is a key held outside the log directory.
3. **Org mode persists only refresh tokens, each sealed to its bearer. Access tokens, codes and
   browser sessions stay in memory.** This was cited as "#402". Files:
   `src/privacyfence/web/oauth_provider.py` (module docstring),
   `src/privacyfence/web/sealed_refresh_store.py`. It meets the bar because it puts a secret at
   rest. Rejected alternatives: persist nothing (a restart then breaks scheduled calls), or encrypt
   the tokens at rest under a key the daemon holds.
4. **The local browser session is a server-side session with idle and absolute expiry, entered
   through a single-use bootstrap code. The browser never holds a long-lived secret, and codes are
   minted on demand only over the control channel, never over HTTP.** This was cited as "SEC-06"
   and "#428 Phase 2". Files: `src/privacyfence/web/session_auth.py` (module docstring). It meets
   the bar because it is the local trust boundary. It may already be covered by ADR 0002; step 7
   should check that before writing a new ADR.
5. **An install-wide privacy policy edit from org mode's admin page is hot-reloaded, not applied
   at the next restart.** This was cited as "#400 C3e". File:
   `src/privacyfence/web/org_install_policy.py` (module docstring). It meets the bar because of the
   rejected alternative: a "restart required" banner, which would have been the smaller change.
6. **The decision ledger replays an approved read, but an approved write is single-use.** This was
   cited as "D3". Within the ledger TTL, an identical read may reuse an approval. A second
   identical write goes back through the gate. Files: `tests/unit/test_gate.py`
   (`test_write_gate_ledger_entry_is_single_use`) and `src/privacyfence/approvals.py` (step 3's
   file). It meets the bar because it is a trust boundary: how far one human approval reaches.
7. **An auto-accept rule is identified by a content-derived id, never by its name.** This was
   cited as "F9". `policy.store.rule_id_for_rule` computes the id, and `AuditEntry.rule_id`
   records it. Files: `src/privacyfence/audit_log.py`, `tests/unit/test_gate.py`
   (`TestRuleIdAttribution`), `tests/unit/policy/test_engine.py` and
   `tests/unit/policy/test_store.py`. It meets the bar because it is hard to reverse once audit logs
   carry the ids. Rejected alternative: attribute by rule name, which can repeat.
8. **Six operation keys are left ungoverned.** This was cited as "F5" and "P0.3". The keys are
   `gmail.create_filter`, `gmail.update_filter`, `slack.create_group_chat` and three
   `apps_script.*` keys. No rule, label, grant or proposal reaches them. Files:
   `tests/unit/policy/test_registry.py` (`_UNGOVERNABLE_OPERATIONS`) and
   `tests/unit/policy/test_propose.py`. This one is weaker: it is an ADR only if always-popup is a
   firm decision and not a known gap.
9. **App-level sign-in policy on top of the IdP (allowed domains, required groups).** This was
   cited as "SEC-22". Files: `src/privacyfence/org_identity.py`, `web/oauth_provider.py`,
   `web/routes_org_identity.py` and their tests. This one is borderline: it is a trust-boundary
   layer, but it may be too small a decision for an ADR.

## Changed user-visible strings

- `scripts/build_org_bundle.py` `--help`:
  - Group titles, before and after:
    - `Deployment mode (P7)` becomes `Deployment mode`.
    - `App-level authorization policy (SEC-22)` becomes `App-level authorization policy`.
    - `WebAuthn step-up (P9, D7)` becomes `WebAuthn step-up`.
    - `Centralized audit-log forwarding (org mode, SEC-23, src/privacyfence/audit_forwarding.py)`
      becomes `Centralized audit-log forwarding (org mode, src/privacyfence/audit_forwarding.py)`.
    - `Bundle signing (SEC-05 full signing, src/privacyfence/org_bundle_signing.py)` becomes
      `Bundle signing (src/privacyfence/org_bundle_signing.py)`.
  - `--google-client-secret` pointed at the setup guide's missing `§4.2`. It now names the guide's
    "Connector apps" section.
  - `--server-trusted-proxy`, `--step-up-enabled`, `--step-up-rp-id`, `--idp-step-up-acr-value` and
    `--step-up-require-passkey`: the plan section references and `#406` are gone. The reasons are
    now given in words. `--step-up-enabled` now says that local mode reads its step-up settings
    from `settings.yaml`. It used to say that local mode's trust model is physical possession, which
    is no longer true.
- `scripts/verify_audit_log.py`: the argparse description is now "Verify the append-integrity hash
  chain on an installed audit log." It was "Verify SEC-23's …".
- `src/privacyfence/web/session_auth.py` `_companion_availability_sentence()` (on the local
  not-authorized page) is now "This install runs it at login for you, so it should already be
  there." The `(#428 Phase 4)` is gone. `tests/unit/web/test_session_auth.py` asserts on a
  substring that is unchanged.

## Cross-slice edits needed

- `docs/code-history-tags/plan.md` still names `tests/unit/test_p6_principal_isolation.py` and
  `test_the_f5_operations_stay_unproposed`. They are now `tests/unit/test_principal_isolation.py`
  and `test_the_ungovernable_operations_stay_unproposed`. The plan is deleted in step 7 anyway.

Other than that, none. No test outside this slice asserts on a changed string (checked with `git grep`).

## Bugs noticed

- `scripts/build_org_bundle.py` `--downloads-disable-staging` help says 'see the plan doc's
  "Org-level opt-out" section'. That plan doc no longer exists, so the text is user-visible and
  stale. It is not a tag, so it is not changed here.
- These stale statements sat in the sentences this step rewrote, so the new wording states
  today's behaviour:
  - `web/org_session.py`'s docstring said that `/settings` is not mounted in org mode. It is
    (`routes_settings.build_org_routes`).
  - `web/routes_org_identity.py`'s docstring said that `/approvals` is not mounted in org mode.
  - `web/session_auth.py`'s `check_csrf` pointed at the deleted `ipc_server.py`.
  - `web/session_auth.py`'s `BootstrapStore` said codes are minted through `POST /api/bootstrap`,
    which no longer exists.
- `src/privacyfence/web/server.py:28` and `web/control_channel.py:2` still mention
  `POST /api/bootstrap` as history. Those files belong to step 2.
- **Guard gap (for step 7):** the plan-item-ID pattern's lookbehind skips an ID that directly
  follows a quote, so `"""F5 of the review ...` passes the guard. One case was found and rewritten
  here, in `tests/unit/test_org_bundle_signing.py`. Other slices may have more.
- The guard also misses several tag shapes, which this slice removed wherever it met them:
  - `PSC-4b`, `AGT-5` and `C3e`-style tags;
  - "the self-approval plan's Phase N", when no digit follows "Phase".
  Examples of each shape remain in `web/org_settings_scope.py`'s module docstring (PSC-4a/PSC-4b,
  AGT-5) and in `tests/unit/web/test_routes_approvals.py::TestSensitiveConfirmDialog` ("The
  self-approval review's Phase 4"). That test file belongs to step 3.
- History phrasing without a tag was left alone under rule 7:
  - "no longer writes the link to a file" is on the local not-authorized page, and
    `test_session_auth.py` asserts on it;
  - "used to be a blind time.sleep" in `test_audit_forwarding.py`;
  - `AuditEntry.rule_id`'s comment in `audit_log.py` mentions the v1 and v2 engines disagreeing.
    Only one engine exists now (ADR 0004), so that sentence is stale;
  - `tests/unit/test_principal_isolation.py`'s module docstring quotes a deleted plan's "exit
    criterion" wording;
  - `tests/unit/policy/test_describe.py` says that the popup and Settings "currently disagree" on
    width, which may be stale.

## Open issues kept as URLs

None. Every issue this slice referenced (#151, #400, #402, #406, #423, #426, #428, #579, #588) is
closed, and all of them were removed.
