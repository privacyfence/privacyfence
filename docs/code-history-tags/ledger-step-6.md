# Ledger: step 6

## ADR candidates

1. **The default step-up scope is `writes_and_pii_reads`.** Neither `writes` nor `writes_and_reads`.
   `writes` leaves every read a session can release on its own, PII-flagged reads included, and
   exfiltration is what an agent with code execution wants. `writes_and_reads` asks for a passkey on
   reads nothing marks as sensitive, and people learn to click through prompts like that. The
   default is the same in both modes, and an install with an explicit `scope:` keeps it. Cited in
   `src/privacyfence/step_up_config.py` (`DEFAULT_STEP_UP_SCOPE` comment, `StepUpConfig.scope`
   comment). Why it meets the bar: it is a trust-boundary default, and it rejects two alternatives
   for reasons that are not obvious. ADR 0003's *Out of scope* amendment covers whether
   `require_passkey` defaults on, not which scope is used.
2. **Enrolling a passkey is itself gated.** With a credential already enrolled, enrolling another
   needs a fresh assertion bound to `enroll-credential|<principal>`. The first enrollment needs the
   companion's `CONFIRM ENROLL` in local mode. In org mode it rests on the IdP session.
   `register_verify` refuses a challenge that did not pass either gate, and every refusal is
   audited. Cited in `src/privacyfence/web/routes_security.py` (module docstring,
   `register_verify`'s tripwire comment). Why it meets the bar: it is a trust boundary. Ungated
   enrollment is a full bypass, because a session could enroll a key it made itself. ADR 0055
   covers only authenticator attachment.
3. **Step-up can be turned on from the UI but only turned off by a config edit and a restart.**
   `enable_step_up` and `LiveStepUpConfig.update()` go one way only. That is what makes
   `step_up_disabled_notice`'s "treat this install as compromised" banner trustworthy: a disable
   never comes from a button click. Cited in `src/privacyfence/step_up_config.py`
   (`LiveStepUpConfig`), `src/privacyfence/settings_controller.py` (`enable_step_up`),
   `src/privacyfence/webauthn_stepup.py` (module docstring, "Requirement enable/disable tracking"),
   and `src/privacyfence/settings_window_html.py` (the "turn step-up on" control comment). Why it
   meets the bar: it is a trust boundary and a deliberate asymmetry that someone might "fix" later.
4. **With `require_passkey` set and nothing enrolled, the daemon starts, releases nothing, and shows
   a persistent banner instead of refusing to boot.** Refusing to start would remove the only path
   to `/security`, the page that fixes the problem. Cited in `src/privacyfence/step_up_config.py`
   (`local_enrollment_banner`). Why it meets the bar: it rejects the more obvious reading of "fail
   closed" for a reason that is not obvious. Check whether ADR 0003 or ADR 0034 already records it
   before writing a new one.
5. **Batch step-up: `single_assertion` is the default, and `per_item` refuses instead of
   downgrading.** In `per_item` mode the batch decide endpoint releases nothing that needs step-up,
   rather than quietly running one assertion per item. Cited in `src/privacyfence/step_up_config.py`
   (`DEFAULT_STEP_UP_BATCH_MODE` comment) and `src/privacyfence/web/approval_step_up.py`
   (`batch_step_up_response`, which never offers an IdP fallback in either mode). Why it meets the
   bar: it is a trust-boundary default with a rejected alternative.
6. **Connector enable is sensitive, and disable is not.** "An agent with connector access gains
   nothing new" only holds for disabling. Re-enabling a connector a human switched off is new
   access. Cited in `src/privacyfence/web/routes_settings.py` (module docstring,
   `_SENSITIVE_ACTIONS`/`_NON_SENSITIVE_ACTIONS` comments) and
   `src/privacyfence/settings_window_html.py` (the connector toggle comment). Why it meets the bar:
   it is a trust-boundary classification. Check whether ADR 0034 already lists it.

## Changed user-visible strings

1. `src/privacyfence/step_up_config.py`, `StepUpConfig.from_local_config`: the
   `ConfigurationError` raised when `require_passkey: true` is set on an unseparated install now
   says `is not privilege-separated (ADR 0003)` instead of
   `is not privilege-separated (#428 Phase 4 / ADR 0003)`. No test asserts on that fragment.
2. `src/privacyfence/web/routes_settings.py`, `build_routes`: the `RuntimeError` for an
   unclassified bespoke POST route now reads
   `... classification (ADR 0014) -- add it to one of the two above so it cannot bypass
   _SENSITIVE_ACTIONS-shaped gating` instead of
   `... classification (3.3 of the self-approval review) -- add it to one of the two above before
   it can bypass _SENSITIVE_ACTIONS-shaped gating the way org_config_upload used to (F5)`. This is
   developer-facing: it fires only when the app is built with an unclassified route. The tests in
   `tests/unit/web/test_routes_settings.py` match on the route path and "classification", which
   are unchanged.

## Cross-slice edits needed

None. `git grep` finds neither old string outside this slice.

## Bugs noticed

None in behavior.

Tag shapes the guard does not catch (for step 7 to decide whether the guard should): this slice
still contains `PSC-2a`/`PSC-4a`/`PSC-4b`/`PSC-5` (policy surface consolidation IDs, about 30 in
`routes_settings.py`, `settings_window_html.py`, `settings_controller.py`,
`web/approval_step_up.py` and `tests/unit/test_settings_window_html.py`) and `AGT-5`. They were
left alone under rule 7, except where they shared a sentence with a guarded tag. Also, a plan item
ID followed by `'s` (`F9's`) slips past the plan-item-ID pattern's `(?!["'])` lookahead. The one
instance here was removed along with its sentence. Plain-word references such as "plan item 1.3"
and "C3e" were removed where they shared a sentence with a guarded tag.

## Open issues kept as URLs

None. Every issue referenced in this slice is closed (checked with the GitHub MCP), so every
reference was removed: #120, #145, #151, #396, #400, #406, #426, #427, #428, #579, #588 and #614. `#555` and `#888` are CSS hex
colours, not issues; they are in `_ALLOWED` for `web/routes_security.py`. `W31` in
`tests/unit/test_settings_window_html.py` is an ISO week in an audit log file name, also in
`_ALLOWED`.
