# ADR 0300: An administrator's tick approves a gated upstream tool for a scoped token

## Status

Accepted — 2026-11-15. Implemented.
Amends [ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
[ADR 0236](0236-a-scoped-token-holds-its-own-rules.md) and
[ADR 0285](0285-upstream-mcp-servers-are-declared-connectors.md).
Issue [#1121](https://github.com/privacyfence/privacyfence/issues/1121).

**Amended by [ADR 0331](0331-the-administrator-manages-each-upstream-servers-tools.md), 2026-11-19.**

## Context

An unattended n8n workflow could not call any upstream write tool. Every upstream write has to be declared
`popup`, a `popup` needs a person's card, and a scoped token never gets a card, so the gate refused it. A
`review` tool was no better: its result is released only after a person has seen it.

Under a scoped token only the token's own rules can auto-accept (ADR 0164, ADR 0236). Upstream tools are
registered at startup and have no scope-catalogue entry, so no rule could cover one (ADR 0285). A token could
already list a gated upstream tool; it was simply denied at the gate. The organization needs a way for an
administrator to say "this token may call this write tool with nobody present", without handing that power to
an AI client, a person's own rules or a token's creator who is not an administrator.

## Decision

1. **The administrator's tick is the approval and is a rule inside the token.** In Settings → Scoped tokens →
   New token, an administrator ticks a `popup` or `review` upstream tool under "Upstream tools that change
   data or release results unseen". The tick is stored as one custom row in the token's own rules. No card is
   shown per call. Every call still writes `upstream_forwarded`, and a `popup` or `review` call under a token
   also writes an `auto_accepted` row whose rule is the approval row.
2. **The row is pinned to the operator's declaration.** The predicate `upstream.approved_tool` has the value
   `<operation>@<digest>`, where the digest is the first 32 hex characters of a SHA-256 over the tool's
   audience, effect, gate, identity claim mode, operation, url and verb. The registry fills a table of current
   digests at startup. The selector matches only when the value's digest equals the table's, so a changed
   declaration voids the approval, and an empty table matches nothing.
3. **The predicate is token-only.** The engine's selector table is global, so a person's own rule list
   carrying the predicate would skip a card in an interactive session. The gate drops every rule with a
   token-only predicate when no token is in scope (the enforcement point); the scope-row and policy-store
   readers, the personal-settings import, the legacy token conversion and `copy`/`duplicate` of a token row
   keep the row out of a person's files and tokens as defence in depth. A token row naming another operation
   than its value is dropped on load.
4. **Only an administrator creates an approval, and only at creation.** The create request carries
   `upstream_approvals` (`tool`, `digest`), inside the step-up fingerprint. `ScopedTokenStore.create` refuses,
   in order: approvals from a non-administrator; a gated upstream tool listed without an approval (for every
   caller, including a runtime-credential sync token); an approval for a tool that is not listed, is not
   gated upstream, or whose digest differs from the current declaration. An AI proposal cannot carry an
   approval (it would be a widening), the proposal-accept route passes none whatever the body says, and a
   local server refuses a non-empty list. A creator who later loses the role keeps the token until it
   expires: there is no live administrator lookup (as ADR 0299).
5. **A call no rule covers is refused before it reaches the upstream.** `check_scoped_upstream_approval` runs
   in the dispatcher right before the connector is called, and refuses a token call to a gated upstream tool
   that the token's rules would not auto-accept (an approval void after a declaration change, or a token that
   already listed the tool). It audits the refusal. It is not in the connector, which never reads the caller.
6. **`review` results go to the token unseen, but personal data still blocks them.** The tick is the decision
   to release that tool's results to that token without a person seeing them, and the form says so. Upstream
   tools are in neither personal-data tool set, so `allow_personal_data_reads` does not cover them and a
   result with personal data is still denied. The scan covers what is returned: the text and the structured
   content. The audit metadata of `upstream_forwarded` gains `gate`.
7. **The form shows the approval to administrators only.** A non-administrator and a local server see no such
   group. Each gated tool shows "writes data" or "result not reviewed by a person" and the operator's effect
   text, unticked. Duplicate never ticks a gated tool, and approval rows are not shown as rules.
8. **Nothing else widens.** Audiences stay the owner's current audiences narrowed by the ticked ones,
   `allowed_principal_types` still refuses an excluded `n8n_workflow`, claim_version stays 1, and AI proposals
   still refuse upstream tools by name.

## Alternatives considered

- **A token field `approved_upstream_tools` checked in the gate** — a second, non-rule route to auto-accept,
  with no rule id for the audit; ADR 0164 and 0236 say a call auto-accepts only through one of the token's
  rules.
- **Deriving the approval from membership in `tools`** — a token can already list a gated upstream tool, so
  it would silently widen existing tokens.
- **A scope-catalogue group per gated tool** — it would expose a person-wide rule for an upstream write to the
  Auto-accept page, `privacyfence_propose_policy_change` and "Always allow", which ADR 0285 kept out.
- **A card per call answered by a person** — contradicts ADR 0164: a scheduled job has nobody to answer.
- **A digest of the listed `inputSchema`** — the schema arrives after startup, so the table could not be
  filled at registration, and a benign upstream edit would void every approval.
- **Refusing `review` tools to a token** — a hub's read tool may reasonably be `review`.
- **A live administrator lookup on each call** — follows ADR 0299, which has none.

## Consequences

- A token may write to an upstream with no card. The approval is a rule inside the token, made only by an
  administrator on the Settings form, and void when the operator changes the declaration.
- An upstream that changes a tool's schema or description at restart keeps the approval; the operator
  controls which server is approved.
- A demoted administrator's tokens work until they expire or are revoked.
- Hub results often contain personal data, so many `review` calls by a token may be denied. Letting
  `allow_personal_data_reads` cover upstream tools is a widening and needs its own ADR.
- A voided approval shows as a clear refusal before forwarding, and `privacyfence_check_policy` predicts it
  from the token's rules.
- The reference upstream's sample proposal contains an IBAN, not an email: an email alone is not detected as
  personal data by the scan.

## Verification

- `tests/unit/policy/test_scopes.py::TestUpstreamApprovedTool` and
  `tests/unit/test_gate.py::TestTokenOnlyPredicates` (selector, token-only guard, refusal check).
- `tests/unit/policy/test_scope_rows.py`, `tests/unit/policy/test_store.py`,
  `tests/unit/test_personal_settings.py`, `tests/unit/test_upstream_mcp_config.py` (digest) and
  `tests/unit/test_upstream_mcp_registry.py` (declaration table).
- `tests/unit/test_scoped_tokens.py::TestUpstreamApprovals`,
  `tests/unit/test_scoped_token_proposals.py::TestNarrowing` and
  `tests/unit/web/test_routes_org_settings.py` / `test_routes_settings.py` (who may create an approval).
- `tests/unit/web/test_mcp_dispatch.py::TestScopedUpstreamApproval` and
  `tests/unit/connectors/test_upstream_mcp_connector.py` (refusal before forwarding, scan, `gate` metadata).
- `tests/unit/web/test_server.py::TestScopedTokenPageData`, `tests/unit/test_settings_window_html.py` and
  `tests/integration/test_settings_scoped_token_form_browser.py` (the form).
- `tests/integration/test_upstream_mcp_org.py` (a real token calls a ticked tool; a `review` result with
  personal data is still denied) and `tests/unit/test_upstream_mcp_reference_server.py`.
