# Scoped tokens can call `review` and `popup` upstream tools, approved by an administrator's tick

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An administrator opens Settings → Scoped tokens → New token. Under each upstream server a group,
"Upstream tools that change data or release results unseen", lists its `popup` and `review` tools, each
marked "writes data" or "result not reviewed" with the operator's `effect` text, all unticked. Ticking one approves that tool for every call the token makes, so an unattended n8n workflow can call
it with no card; each call writes the usual `upstream_forwarded` audit entry. A token cannot be created
listing such a tool without the tick, and a token whose approval no longer matches the declaration is
refused with a clear error before anything reaches the upstream. A non-administrator sees no such group.

**Scope:** New: a token-only rule predicate `upstream.approved_tool` pinned to a digest of the operator's
declaration; an `upstream_approvals` field in the create request; the form group; a refusal before
forwarding (an existing token that lists a gated tool is covered too: a `review` call no longer reaches the
upstream before being denied); the PII scan of a `review` result covers the structured part it returns (it
can flag more personal data on a person's own `review` cards); a `gate` field in the audit metadata; ADR 0300. Unchanged: claim_version 1, audience narrowing,
verified-email rules, `allowed_principal_types`, AI proposals, `auto` upstream tools, Auto-accept and "Always allow".

**Security:** A token may now write to an upstream with no card. The approval is a rule inside the token
(ADR 0164 and 0236 hold), made only by an administrator on the Settings form, and void when the operator
changes the tool's declaration. The predicate is token-only: a person's rules and imports never carry it.
Personal data in a `review` result still denies the call. A demoted administrator's token works until expiry.

**Review focus:** who may create an approval, the order of the create rules and why a proposal cannot carry
one (3.4); the token-only guard and where it is enforced (3.3); the refusal before forwarding and why it is
not in the connector (3.5); `review` results under a token and the PII scan (3.6).

## 1. Goal

An unattended n8n workflow cannot call any upstream write tool today: every write must be declared
`popup`, `popup` needs a person's card, and a scoped token never gets a card, so
`gate._deny_scoped` refuses it (`docs/upstream-mcp.md`, "Known limitation"). An administrator can now
approve such a tool for one token when creating it. Issue: #1121.

## 2. Current state

- Under a scoped token only the token's own rules can auto-accept
  (`src/privacyfence/gate.py:700-730` `_evaluate_auto_accept`, `_rules_in_scope`); anything else ends in
  `_deny_scoped` (`gate.py:1742`). ADR 0164 and ADR 0236 state this.
- Upstream tools are registered at startup into `TOOL_TO_GATE`, `TOOL_TO_OPERATION` (`"<id>.<tool>"`),
  `TOOL_TO_VERB` and `EFFECT_BY_TOOL` (`src/privacyfence/upstream_mcp_registry.py:30-50`). They have no
  scope-catalogue entry, so no rule can cover one (ADR 0285, Consequences).
- `connectors/upstream_mcp.py`: `_popup` calls `gated_call` first and forwards after; `_review` forwards
  first and gates the result (`:109-130`). `_review` scans `_text(result)` for personal data but returns
  `_value(result)`, which can be the structured part (`:109-120`, `:268-275`).
- `ScopedTokenStore.create` accepts any name in `TOOL_TO_GATE` (`scoped_tokens.py:802-804`), so a token can
  already list a gated upstream tool; it is simply denied at the gate. Only
  `privacyfence_update_group_membership` is administrator-only (`:679-682`); `auto` upstream tools can be
  ticked by anyone.
- The New token form lists a gated tool only when a chosen rule covers it, which none can
  (`settings_window_html.py:1590-1660`, `web/server.py:355-455`).
- `privacyfence_propose_scoped_token` refuses any upstream tool by name prefix
  (`scoped_token_proposals.py:157-166`) and a review can only narrow a proposal (`:196`).
- A custom `ScopeRow` (scope `None`, own predicate) round-trips through the token file and compiles to one
  engine rule (`policy/scope_rows.py:116-135`, `:201-235`). The engine looks selectors up in one global
  table (`policy/engine.py:57-58`).
- The claim for a token call already carries `principal_type: n8n_workflow`, the token's tools, `approved_by`
  and `token_id` (`upstream_mcp_runtime.py:174-186`).

## 3. Design

### 3.1 What replaces the card

The administrator's tick at token creation is the approval, stored as a rule in the token. No card is
shown per call. Every call still writes `upstream_forwarded` (claim `jti`, `sub`, `req`, outcome); a
`popup`/`review` call under a token also writes an `auto_accepted` row whose `auto_accept_rule` is the
approval row's id.

Rejected:
- **A token field `approved_upstream_tools` checked in the gate.** A second, non-rule route to
  auto-accept in `gate.py`, repeated in `_evaluate_auto_accept` and `preflight_auto_accept`, with no rule id
  for the audit; ADR 0164/0236 say a call auto-accepts only through one of the token's rules.
- **Deriving the approval from membership in `tools`.** A token can already list a gated upstream tool
  today; it would silently widen existing tokens.
- **A scope-catalogue group per gated tool.** It would expose a person-wide rule for an upstream write to
  the Auto-accept page, `privacyfence_propose_policy_change` and "Always allow" (ADR 0285 kept these out).
- **A card per call, answered by a person, for a token.** Contradicts ADR 0164 ("a scheduled job has
  nobody to answer").

### 3.2 The approval row

- Predicate `upstream.approved_tool`, defined in `policy/scopes.py` `NEW_SCOPE_SELECTORS`, `ResolvesFrom.ARGS`,
  `ScopeKind.ATTRIBUTE`, scope type `upstream.approved_tool`.
- Module constants in `policy/scopes.py`:
  `TOKEN_ONLY_PREDICATES: frozenset[str] = frozenset({"upstream.approved_tool"})` and
  `UPSTREAM_DECLARATIONS: dict[str, str] = {}` (operation key to digest; filled by
  `upstream_mcp_registry.register`, cleared by `unregister_all`).
- Value: one string `"<operation>@<digest>"`, for example `kh.propose_change@3f2a…` (the digest is the first 32 hex
  characters). One string, not a dict, because rule ids, the settings import and the token page all expect a
  string or a list.
- Digest: `upstream_mcp_config.declaration_digest(upstream: UpstreamServer, tool: DeclaredTool) -> str` =
  `sha256` of `json.dumps({"audience","effect","gate","identity_claim","operation","url","verb"}, sort_keys=True,
  separators=(",", ":"))`, first 32 hex characters. Any change to one of those voids the approval.
  Rejected: adding a digest of the listed `inputSchema` (it arrives after startup, so the table could not be
  filled at registration, and a benign upstream edit would void every approval; the operator already controls
  which server and tool is approved). Recorded under Risks.
- Selector `matches(value, ctx)` never reads `ctx`; it returns `True` only when `value` is a `str` with
  exactly one `@`, `UPSTREAM_DECLARATIONS.get(operation)` is not `None` and equals the digest part. An empty
  table (local mode, or before `register`) matches nothing.
- Row: `ScopeRow(id="r-" + sha256(repr(("upstream", value))).hexdigest()[:10], scope=None, value=value,
  verbs=frozenset({Verb(tool.verb)}), operations=frozenset({operation}), predicate="upstream.approved_tool",
  conditions=())`. A row is valid only when its operations equal exactly the operation named in its value;
  `_from_json` drops (with a warning) a token row with this predicate that is not valid, which only narrows.

### 3.3 The predicate is token-only

The engine's selector table is global, so a person's own rule list carrying this predicate would auto-accept
an upstream write in an interactive session. The engine is reached only through the three callers in
`gate.py` (`_evaluate_auto_accept`, `preflight_auto_accept`, `rule_values`), all via `_rules_in_scope`, so
guard 1 is where the rule is enforced; guards 2 to 5 keep the row out of a person's files and tokens as
defence in depth. Each has a test:
1. `gate._rules_in_scope(None)` drops every rule whose predicate is in `TOKEN_ONLY_PREDICATES`.
2. `scope_rows.rows_from_section` drops such a row, and the two v2 read paths
   (`scope_rows.rows_from_config` v2 branch via `migrate_v2`, and `policy/store.compile_rules_from_config`)
   skip a rule with such a predicate.
3. The personal-settings import already skips every custom row (scope `None`, `personal_settings.py:269-274`),
   and an approval row is a custom row; no code change, one regression test.
4. `scoped_tokens.owned_entry` (legacy token conversion) drops such a row.
5. `ScopedTokenStore.create` refuses a `copy` or `duplicate` request whose base row has the predicate.

### 3.4 Creating the approval

- The create request gains `upstream_approvals: list[{"tool": str, "digest": str}]`.
  `normalise_create_request` validates the shape (both strings, list of dicts, no extra keys), de-duplicates
  and sorts by `tool`, and returns it, so it is inside the step-up fingerprint.
- `ScopedTokenStore.create(..., upstream_approvals: Sequence[Mapping[str, str]] = ())`. For the tools in
  `tools`, a **gated upstream tool** is one whose `TOOL_TO_OPERATION` value is a key of
  `UPSTREAM_DECLARATIONS` and whose `TOOL_TO_GATE` value is `review` or `popup`; never decided by a name
  prefix. Rules, in this order, each `ScopedTokenError`:
  1. approvals present and `not is_admin` → `Only an administrator can create a token for <tool>.` (the same
     sentence as the group-membership tool, with the first approval's tool name);
  2. a gated upstream tool in `tools` with no approval → `<tool> changes data or releases results unseen; an
     administrator must approve it on the New token form.`;
  3. an approval for a tool that is not in `tools`, is not gated upstream, or whose digest differs from
     `UPSTREAM_DECLARATIONS[operation]` → `The server's declaration of <tool> changed; reload the page and
     review it again.`
  Then one approval row per approval is appended to the token's rows. `auto` upstream tools are untouched.
- `create` is the one place that decides. The proposal path cannot carry an approval, three times over:
  `scoped_token_proposals.narrowing_error` returns `WIDENED_MESSAGE` when the final request has any
  `upstream_approvals` (an approval is a widening, ADR 0250); the organization proposal-accept route passes
  `upstream_approvals=()` to `_create_and_deliver` explicitly, whatever the browser's body says; and rule 2
  refuses a gated tool without one.
- Rejected: reading `is_admin` live when the token is used. Following ADR 0299 ("no live admin lookup"), a
  creator who later loses the role keeps the token until it expires; documented.
- The organization create route passes `bound["upstream_approvals"]`; the local route passes `()` and
  refuses a non-empty list with `Upstream tools are for organization servers.` (HTTP 400) at the top of the
  local `_create_and_deliver`, which covers both local callers. The third caller of `normalise_create_request`
  (`web/mcp_dispatch.py`, `propose_scoped_token`) needs no change: it gets `[]`. The settings
  audit line of a created token lists each approved `<id>_<tool> (<gate>)`.

### 3.5 Refusing before forwarding

`_review` forwards before it gates, and `_popup` can write its `auto_accepted` row before a forward that
then fails. For a token call that no rule covers, the upstream must not be reached at all. A new function
`gate.check_scoped_upstream_approval(connector: str, tool: str, args: dict) -> None` does nothing unless
`current_token_scope()` is a token (a `REVOKED_SCOPE` is refused too: it fails closed), the tool is a gated upstream tool (3.4 definition) and
`preflight_auto_accept(TOOL_TO_OPERATION[tool], args, tool=tool)[0] != "auto_accept"`; then it calls
`audit_scoped_refusal(connector, tool, scope, "No approval for this upstream tool in this token")` and raises
`GateDeniedError("Request denied: this token has no administrator approval for <tool>, or the server's
declaration of it changed after the token was created.")`. `McpDispatcher.call` calls it inside its `with`
block, right before `connector.call(tool, args)`. It is not in the connector: ADR 0036/0286 and
`test_the_connector_module_never_reads_the_caller` keep the caller out of connector code.

### 3.6 `review` under a token

"The person sees the result" has no person here: the administrator's tick is the decision to release that
tool's results to that token unseen. The call is allowed, the result returned, and the form says so. The
audit marks it: `upstream_forwarded` metadata gains `gate` (`auto`, `review` or `popup`).
Two things stay as they are, and the form, ADR and docs say so:
- Personal data found in a `review` result still denies the call (`denied_scoped_token`): upstream tools are
  in neither `PERSONAL_DATA_READ_TOOLS` nor `PERSONAL_DATA_RULE_TOOLS`, so `allow_personal_data_reads` does
  not cover them. Letting it cover them is a widening and needs its own ADR.
- The scan must cover what is returned: `connectors/upstream_mcp.py` `_review` passes
  `pii_scan_text=_scanned(result)`, where `_scanned` is the text plus, when `structured is not None`, its
  JSON (`json.dumps(structured, sort_keys=True, default=str)`), joined by a newline. `details_text` keeps
  `_text(result)`.
Rejected: refusing `review` tools to tokens (the owner's proposal allows them, and a hub's read tool may
reasonably be `review`).

### 3.7 The form

- `web/server.py` `scoped_token_page_data`: gated upstream tools are left out of `tools` and `auto_tools`.
  For an administrator (`principal.is_admin`) each entry of `upstreams` gains
  `"gated_tools": [{"name", "gate", "verb", "effect", "digest"}]` (digest from `UPSTREAM_DECLARATIONS`);
  for anyone else `gated_tools` is `[]`. Each token entry gains `"approved_upstream_tools": [{"name",
  "gate", "effect"}]` taken from its approval rows, and approval rows are left out of `rules` so Duplicate
  never offers them.
- `settings_window_html.py`: per upstream with gated tools, a fieldset `data-st-gated-for="<id>"` with the
  legend `Upstream tools that change data or release results unseen`, one chip per tool
  (`data-st-gated-tool="<name>"`, `role="checkbox"`, unticked), the marker text `writes data` for `popup`
  and `result not reviewed by a person` for `review` (`data-st-gated-marker`), and under it the effect
  text (`data-st-gated-effect`; for `review` the sentence `The result goes to the token without a person
  seeing it. Personal data in it still blocks the call.`). Hint under the fieldset: `Ticking approves this
  tool for every call the token makes, with no card. If the operator later changes how the tool is
  declared, the approval stops working.`
- A ticked gated tool counts as a ticked tool of its upstream (the audiences fieldset appears) and is
  included in `tools`; the create request also sends `upstream_approvals` with its digest. Duplicate (`duplicateScopedToken`)
  ticks only the token's tools that are listed in `SCOPED.tools` or `SCOPED.auto_tools`, so gated tools stay
  unticked, for an administrator and for anyone else.
- Local mode and non-administrators never see the fieldset.

### 3.8 Nothing widens

`caller_for` is unchanged: audiences are the owner's current audiences ∩ the ticked ones, unverified owners
get none, `allowed_principal_types` still refuses an `n8n_workflow` the operator excluded (the form hides
nothing for that; the call is refused at the connector as before and audited `refused`). AI proposals still
refuse upstream tools by name and cannot carry approvals. Claim format unchanged.

### 3.9 Testing approach

Unit tests per phase (named in the briefs), one integration file extended
(`tests/integration/test_upstream_mcp_org.py`) with a real organization server, the reference upstream and
a real scoped token. Browser tests for the form skip where no Chromium launches (as in this container); CI
runs them.

## 4. ADRs

- **ADR 0300**: An administrator approves a `review` or `popup` upstream tool for a scoped token by a tick
  that becomes a token-only rule pinned to the operator's declaration. Amends 0164, 0236 and 0285. It
  records: the token-only guard, the digest fields, `review` results released unseen but PII still denied,
  creation-time-only admin check (ADR 0299 precedent), refusal before forwarding, and the rejected
  alternatives of 3.1. The number is fixed here; if it is taken by the time the PR is opened, the owner renumbers it (rule 6 of the ADR README).

## 5. Manual steps

**After implementation**

`ma1-real-token-call` (about 15 minutes; needs a real n8n and a real administrator sign-in, which CI has not):
1. Sign in to your organization server as an administrator and open `https://<your PrivacyFence address>/settings`,
   then Scoped tokens → New token. You see the New token form.
2. Under your upstream's name find "Upstream tools that change data or release results unseen". Tick one
   `popup` tool and one audience. The tool shows "writes data" and its effect sentence.
3. Create the token. A dialog shows the secret once; copy it into an n8n credential as described in
   [docs/n8n-node.md](https://github.com/privacyfence/privacyfence/blob/feature/scoped-token-gated-upstream-tools/docs/n8n-node.md).
4. Run an n8n workflow whose PrivacyFence node calls that tool. The workflow finishes with the upstream's
   result and no approval card appears.
5. In the upstream's own log, find the call with a PrivacyFence claim whose `principal_type` is `n8n_workflow`.
Report back, as a pass/fail line on the pull request: steps 2, 4 and 5.

## 6. Risks and open questions

- **Missed guard path (3.3).** If any path lets a person's own rule list carry the predicate, an
  interactive session skips a card. A worker that finds a reader of rules not listed in 3.3 stops with
  `status=blocked`.
- **Review tools denied by the PII scan.** Hub results often contain names and emails; most `review` calls
  by a token may be denied. If p5's test shows that, the follow-up is a separate ADR, not this plan.
- **Silent voiding.** A changed declaration voids approvals; the symptom is the clear refusal of 3.5. The
  form hint and `privacyfence_check_policy` (which predicts from the token's rules) say so.
- **No schema pin.** An upstream that changes a tool's schema or description at restart keeps the approval
  (3.2); the operator controls which server is approved.
- **Stale administrator.** A demoted administrator's tokens keep working until expiry; documented.
- **Chromium absent here.** Form behaviour is proven by unit tests on the generated HTML and page data; the
  browser tests run in CI.
- If a call of `policy_engine.find_matching_rule`, `evaluate` or `preflight` exists outside `gate.py`
  (`grep -rn "find_matching_rule\|policy_engine.evaluate\|engine.preflight" src/privacyfence`), guard 1 does not
  cover it: the worker of p1 stops with `status=blocked` and names it.
- Definition of done: p3 edits `connectors/upstream_mcp.py`, which matches the `connectors/**` row of
  `verify.dod_conditional`, but `upstream_mcp` has no entry in `scripts/qa_fixture_recorder.py` and no live
  provider, so no QA fixture is owed. p3 and p4 edit `web/mcp_dispatch.py` and `web/server.py`, whose
  integration row applies only to `ToolSpec`, `ToolParam` and socket-binding changes; none is made.

## Implementation manifest

```yaml
plan_slug: scoped-token-gated-upstream-tools
feature_branch: feature/scoped-token-gated-upstream-tools
tracking_issue: 1121
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-real-token-call
    title: Call a popup upstream tool from an n8n workflow with an administrator-approved token
    why: CI cannot run a real n8n workflow against a real organization server and administrator session
verify_after_merge:
  - python3 -m pytest tests/unit -q
  - ruff check .
final_checks:
  - "`tld plan deviations check --file docs/1121/deviations.md --plan docs/1121/plan.md --all` exits 0"
  - ADR 0300 exists in docs/adr/, has Status Accepted, and has a row in docs/adr/README.md
  - CHANGELOG.md has the entry under "## [Unreleased]" and no new version heading
phases:
  - id: p1-token-only-approval-rule
    title: The token-only approval predicate, the declaration table and the guards
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/scope_rows.py
      - src/privacyfence/gate.py
      - src/privacyfence/policy/store.py
      - src/privacyfence/upstream_mcp_config.py
      - src/privacyfence/upstream_mcp_registry.py
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/test_scope_rows.py
      - tests/unit/test_gate.py
      - tests/unit/test_personal_settings.py
      - tests/unit/policy/test_store.py
      - tests/unit/test_upstream_mcp_config.py
      - tests/unit/test_upstream_mcp_registry.py
    brief: |
      Read first: docs/adr/0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md,
      docs/adr/0236-a-scoped-token-holds-its-own-rules.md, docs/adr/0285-upstream-mcp-servers-are-declared-connectors.md,
      docs/coding-and-testing-guidelines.md, and sections 3.2, 3.3 and 3.5 of docs/1121/plan.md.
      1. `upstream_mcp_config.py`: add `declaration_digest(upstream, tool)` exactly as plan 3.2 (fields
         audience, effect, gate, identity_claim, operation, url, verb; sort_keys, compact separators; first
         32 hex characters of sha256). Import `hashlib` and `json` if absent.
      2. `policy/scopes.py`: add `TOKEN_ONLY_PREDICATES`, `UPSTREAM_DECLARATIONS`, a function
         `_upstream_approved_tool_matches(value, ctx)` implementing the selector of plan 3.2 (never reads
         `ctx`; fails closed), and the `"upstream.approved_tool"` entry in `NEW_SCOPE_SELECTORS` (scope_type
         `upstream.approved_tool`, `ScopeKind.ATTRIBUTE`, `ResolvesFrom.ARGS`). Update the module docstring
         list of new scope types by one clause.
      3. `upstream_mcp_registry.py`: in `register`, after the collision check, write
         `scopes.UPSTREAM_DECLARATIONS[tool.operation] = declaration_digest(upstream, tool)` for every
         declared tool of every upstream (all gates); in `unregister_all`, pop every operation `register`
         added (keep a module set `_added_operations` beside `_added`). Update the module docstring.
      4. `gate.py`: in `_rules_in_scope`, for `scope is None` return
         `[r for r in get_policy_v2_store_rules() if r.predicate not in policy_scopes.TOKEN_ONLY_PREDICATES]`
         (add `from .policy import scopes as policy_scopes` beside the file's other `policy_` imports at `gate.py:177-181`
         and use `policy_scopes.TOKEN_ONLY_PREDICATES`). Add
         `check_scoped_upstream_approval(connector, tool, args)` exactly as plan 3.5, plus a helper
         `is_gated_upstream_tool(tool) -> bool` (operation in `UPSTREAM_DECLARATIONS` and gate in
         `("review", "popup")`) that p2 also uses; put both next to `audit_scoped_refusal`.
      5. `policy/scope_rows.py`: make `rows_from_section` drop a row whose predicate is in
         `TOKEN_ONLY_PREDICATES`, and make the v2 branch of `rows_from_config` (`:259-262`, which calls
         `migrate_v2`) drop any resulting row with such a predicate. `policy/store.py`
         `compile_rules_from_config` (`:207-215`): skip a rule from `rule_from_dict` whose predicate is in
         `TOKEN_ONLY_PREDICATES`. Do not change `row_from_dict` (tokens use it).
      6. `personal_settings.py` needs no change: `plan_import` already skips every custom row
         (`:269-274`). The test of step 7 is a regression test of that.
      7. Tests (write them first): `tests/unit/policy/test_scopes.py::TestUpstreamApprovedTool`:
         `test_selector_matches_only_the_current_digest`, `test_selector_fails_closed_with_an_empty_table`,
         `test_selector_refuses_a_malformed_value`. `tests/unit/test_gate.py::TestTokenOnlyPredicates`:
         `test_a_persons_own_rules_never_include_a_token_only_row`,
         `test_a_scoped_call_keeps_token_only_rows`, `test_a_scoped_call_without_an_approval_is_refused_before_forwarding`,
         `test_an_approved_scoped_call_passes_the_check`, `test_an_unscoped_call_is_never_checked`,
         `test_a_built_in_tool_with_an_upstream_looking_name_is_not_gated_upstream`.
         `tests/unit/policy/test_scope_rows.py`: `test_rows_from_section_drops_a_token_only_row`.
         `tests/unit/test_personal_settings.py`: `test_import_skips_a_token_only_row`.
         `tests/unit/policy/test_store.py`: `test_compile_rules_from_config_skips_a_token_only_rule`.
         `tests/unit/test_upstream_mcp_config.py`: `test_declaration_digest_changes_with_each_pinned_field`
         (one assertion per field) and `test_declaration_digest_ignores_the_order_of_other_tools` (the same tool declared in a file whose
         other tools are listed in another order gives the same digest).
         `tests/unit/test_upstream_mcp_registry.py`: `test_register_fills_and_unregister_clears_the_declaration_table`.
      8. `tests/conftest.py` already unregisters upstream tools after each test; if
         `UPSTREAM_DECLARATIONS` still holds entries after `unregister_all`, stop with `status=blocked`.
      Stop with `status=blocked` if the grep named in the plan's Risks finds a call of the engine outside `gate.py`.
    acceptance:
      - python3 -m pytest tests/unit/policy/test_scopes.py tests/unit/policy/test_scope_rows.py tests/unit/policy/test_store.py tests/unit/test_gate.py tests/unit/test_personal_settings.py tests/unit/test_upstream_mcp_config.py tests/unit/test_upstream_mcp_registry.py -q passes
      - python3 -m pytest tests/unit/policy/test_scopes.py::TestUpstreamApprovedTool tests/unit/test_gate.py::TestTokenOnlyPredicates -q passes
      - ruff check . passes
      - python3 -m pytest tests/unit/test_systemic_gate_invariants.py -q passes
  - id: p2-create-with-approval
    title: The create request carries upstream approvals; only an administrator can make them
    depends_on: [p1-token-only-approval-rule]
    complexity: M
    touches:
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/scoped_token_proposals.py
      - src/privacyfence/web/routes_settings.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_scoped_token_proposals.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/unit/web/test_routes_settings.py
    brief: |
      Read first: sections 3.2, 3.3, 3.4 and 3.8 of docs/1121/plan.md,
      docs/adr/0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md and
      docs/adr/0299-group-membership-is-updated-through-a-scoped-token-tool.md.
      1. `scoped_tokens.py`: `normalise_create_request` reads `payload.get("upstream_approvals", [])`; a value
         that is not a list of dicts with exactly the string keys `tool` and `digest` returns `None`; the
         result gets `"upstream_approvals"`: the list de-duplicated and sorted by `tool`. (The step-up
         fingerprint is computed from this output, so no other change is needed there.)
      2. `ScopedTokenStore.create`: add `upstream_approvals: Sequence[Mapping[str, str]] = ()`. After the
         group-membership check (`:680`) and before the audiences check, apply the three rules of plan 3.4 in that order, using
         `gate.is_gated_upstream_tool` (function-local import; the module is import-cycle sensitive) and
         `scopes.UPSTREAM_DECLARATIONS`. After the token's rows are built from `rules`, append one
         `ScopeRow` per approval exactly as plan 3.2 (row id, scope `None`, predicate, value
         `"<operation>@<digest>"`, verbs `{Verb(TOOL_TO_VERB[tool])}`, operations `{operation}`); the
         operation comes from `TOOL_TO_OPERATION[tool]`, never from the request. The 50-rule limit becomes
         `len(rules) + len(upstream_approvals) > MAX_RULES_PER_TOKEN` in `_prepare`, with its existing message
         (pass the approvals count to `_prepare`).
      3. `_check_locked`: a `copy` or `duplicate` base row whose predicate is in `TOKEN_ONLY_PREDICATES`
         raises `ScopedTokenError("That rule cannot be copied.")`.
      4. `_from_json`: after `row_from_dict`, a row whose predicate is in `TOKEN_ONLY_PREDICATES` is kept only
         when `isinstance(row.value, str)`, `row.value.count("@") == 1` and
         `row.operations == frozenset({row.value.split("@")[0]})`; otherwise skip it with the existing
         "cannot be read" warning.
      5. `owned_entry`: drop rows whose predicate is in `TOKEN_ONLY_PREDICATES` before building
         `entry["rules"]`.
      6. `scoped_token_proposals.py` `narrowing_error`: return `WIDENED_MESSAGE` when
         `final.get("upstream_approvals")` is non-empty. `web/routes_settings.py` `_create_and_deliver` gains a
         keyword `upstream_approvals: Sequence[Mapping[str, str]] = ()` and passes it to `store.create`;
         `scoped_token_create` (New token) passes `bound["upstream_approvals"]`; the proposal-accept route
         (`scoped_token_create_from_proposal`) passes nothing, so it stays `()`; extend the settings audit line with
         `, upstream tools approved: kh_propose_change (popup), ...` (only when non-empty, sorted by name, the
         gate from `TOOL_TO_GATE`). The local `_create_and_deliver` (`routes_settings.py` near `:1304`): a non-empty
         `bound["upstream_approvals"]` returns HTTP 400 `{"error": "Upstream tools are for organization
         servers."}` before anything is created. `mcp_dispatch.py:581` (`propose_scoped_token`) calls
         `normalise_create_request` too and needs no change.
      7. Tests (first): `tests/unit/test_scoped_tokens.py::TestUpstreamApprovals`:
         `test_an_administrator_ticking_a_popup_tool_gets_a_row_pinned_to_the_declaration`,
         `test_a_non_administrator_is_refused_a_gated_upstream_tool`,
         `test_a_gated_tool_without_an_approval_is_refused`,
         `test_a_stale_digest_is_refused`, `test_an_approval_for_an_auto_tool_is_refused`,
         `test_approvals_are_in_the_normalised_request`,
         `test_copy_and_duplicate_of_an_approval_row_are_refused`,
         `test_a_row_naming_another_tool_is_dropped_on_load`,
         `test_owned_entry_drops_a_token_only_row`, `test_an_auto_only_token_is_unchanged`.
         `tests/unit/test_scoped_token_proposals.py::TestNarrowing::test_the_review_cannot_add_an_approval`
         (`narrowing_error` returns `WIDENED_MESSAGE`), and in `tests/unit/web/test_routes_org_settings.py`
         `test_accepting_a_proposal_ignores_approvals_in_the_body` (the accept route is posted a body with
         `upstream_approvals`; the created token has none and the response is a refusal). `tests/unit/web/test_routes_org_settings.py`: `test_create_audit_line_names_each_approved_upstream_tool`
         and `test_a_non_administrator_create_with_a_gated_tool_is_400`.
         `tests/unit/web/test_routes_settings.py`: `test_local_create_refuses_upstream_approvals`.
         Use the fixtures the neighbouring tests use (`_create`, `_store`, `held_audiences`); register a
         popup upstream tool in the test with `upstream_mcp_registry.register(parse_upstreams(...))`.
      Stop with `status=blocked` if `create` is called from a path not named here that passes `is_admin`.
    acceptance:
      - python3 -m pytest tests/unit/test_scoped_tokens.py::TestUpstreamApprovals -q passes
      - python3 -m pytest tests/unit/test_scoped_token_proposals.py::TestNarrowing -q passes
      - python3 -m pytest tests/unit/test_scoped_token_proposals.py -k "upstream_tool_is_refused" -q passes
      - python3 -m pytest tests/unit/web/test_routes_org_settings.py tests/unit/web/test_routes_settings.py tests/unit/test_scoped_tokens.py -q passes
      - ruff check . passes
  - id: p3-refuse-before-forwarding
    title: Refuse an uncovered token call before forwarding; scan the whole review result; mark the gate in the audit
    depends_on: [p1-token-only-approval-rule]
    complexity: S
    touches:
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/connectors/upstream_mcp.py
      - src/privacyfence/audit_log.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/connectors/test_upstream_mcp_connector.py
    brief: |
      Read first: sections 3.5 and 3.6 of docs/1121/plan.md and docs/adr/0286-privacyfence-sends-the-upstream-a-signed-caller-claim.md.
      1. `web/mcp_dispatch.py` `McpDispatcher.call`: inside the `with (...)` block, immediately before
         `result = await connector.call(tool, args)`, add `check_scoped_upstream_approval(connector_name, tool, args)`
         (import it from `..gate` beside the module's other gate imports). It raises `GateDeniedError`, which
         the existing `except Exception` branch turns into the tool error; do not catch it.
      2. `connectors/upstream_mcp.py`: add `_scanned(result)` returning `result.text`, or, when
         `result.structured is not None`, `result.text + "\n" + json.dumps(result.structured, sort_keys=True, default=str)`
         if `result.text` else just the JSON. `_review` passes `pii_scan_text=_scanned(result)` and keeps
         `details_text=text`. In `_forward_audit`, add `metadata["gate"] = declared.gate`. Do not import any
         caller module.
      3. `audit_log.py`: update the `metadata` field comment to list `gate`.
      4. Tests (first): `tests/unit/web/test_mcp_dispatch.py::TestScopedUpstreamApproval`:
         `test_an_uncovered_token_call_never_reaches_the_connector`,
         `test_a_covered_token_call_reaches_the_connector`, `test_an_unscoped_call_is_unaffected`.
         `tests/unit/connectors/test_upstream_mcp_connector.py::TestReviewPiiScan`:
         `test_the_scan_covers_structured_content`, `test_the_scan_of_text_only_is_unchanged`;
         `TestForwardedAudit::test_the_gate_is_recorded_in_metadata` (parametrise auto, review, popup).
      Stop with `status=blocked` if `test_the_connector_module_never_reads_the_caller` fails.
    acceptance:
      - python3 -m pytest tests/unit/web/test_mcp_dispatch.py::TestScopedUpstreamApproval tests/unit/connectors/test_upstream_mcp_connector.py -q passes
      - python3 -m pytest tests/unit/test_systemic_gate_invariants.py tests/unit/test_audit_log.py -q passes
      - ruff check . passes
  - id: p4-token-form
    title: The New token form lists gated upstream tools with their effect text, for administrators only
    depends_on: [p2-create-with-approval]
    complexity: M
    touches:
      - src/privacyfence/web/server.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/web/test_server.py
      - tests/unit/test_settings_window_html.py
      - tests/integration/test_settings_scoped_token_form_browser.py
    brief: |
      Read first: section 3.7 of docs/1121/plan.md, docs/adr/0236-a-scoped-token-holds-its-own-rules.md
      and `tests/integration/test_settings_scoped_token_form_browser.py` (the `upstream_page` fixture).
      1. `web/server.py` `scoped_token_page_data`: exclude gated upstream tools
         (`gate.is_gated_upstream_tool`) from `by_connector` and `auto_tools`; for each `upstreams` row add
         `"gated_tools"` as plan 3.7 (administrators only: `principal.is_admin`; `[]` otherwise; `effect` from
         `write_effects.EFFECT_BY_TOOL` for popup and the literal review sentence of plan 3.7 for review;
         `digest` is the part after `@` of `f"{op}@{UPSTREAM_DECLARATIONS[op]}"`, i.e. `UPSTREAM_DECLARATIONS[op]`).
         For each token add `"approved_upstream_tools"` from its rows whose predicate is
         `upstream.approved_tool` (name from the row's operation via `TOOL_TO_OPERATION` reversed; gate; effect)
         and leave those rows out of `token_rules`.
      2. `settings_window_html.py`: render the fieldset of plan 3.7 with exactly the strings given there,
         tick state in `ui.stGated` (a dict keyed by tool name), clicking toggles it, and a ticked gated tool
         also sets `ui.stTools[name]`. `scopedUpstreamsWithTools` counts a ticked gated tool so the audiences
         fieldset appears. The create post gains `upstream_approvals: [{tool, digest}]` for each ticked gated
         tool, sorted by tool. In `duplicateScopedToken` (`settings_window_html.py:1413-1428`) tick only the token's tools that are
         in `SCOPED.tools` or `SCOPED.auto_tools`. `scopedUpstreamsWithTools` (`:1463-1466`) also returns an
         upstream when any of its `gated_tools` names is ticked in `ui.stGated`. Show each token's `approved_upstream_tools` in the
         token list line as `<tool> (writes data)` or `<tool> (results unseen)`.
      3. Tests (first): `tests/unit/web/test_server.py::TestScopedTokenPageData`:
         `test_an_administrator_page_lists_gated_tools_with_effect_and_digest`,
         `test_a_non_administrator_page_lists_no_gated_tool`, `test_gated_tools_are_not_in_tools_or_auto_tools`,
         `test_approval_rows_are_left_out_of_a_tokens_rules`. `tests/unit/test_settings_window_html.py`:
         `test_the_gated_fieldset_text_is_exact`. Browser (skip without Chromium, run in CI), in
         `test_settings_scoped_token_form_browser.py`:
         `test_a_gated_upstream_tool_shows_its_effect_and_a_writes_data_marker`,
         `test_a_gated_tool_is_never_ticked_by_default`,
         `test_ticking_a_gated_tool_posts_an_approval_with_its_digest`,
         `test_a_page_without_gated_tools_shows_no_fieldset`, `test_duplicate_leaves_gated_tools_unticked`.
         Extend `UPSTREAM_DATA` with a `gated_tools` entry (a popup tool with an effect and a review tool).
    acceptance:
      - python3 -m pytest tests/unit/web/test_server.py tests/unit/test_settings_window_html.py -q passes
      - python3 -m pytest tests/integration/test_settings_scoped_token_form_browser.py -q passes (tests skip where no Chromium launches; CI runs them)
      - ruff check . passes
  - id: p5-end-to-end
    title: A real token calls a ticked popup tool unattended over a real organization server
    depends_on: [p2-create-with-approval, p3-refuse-before-forwarding]
    complexity: S
    touches:
      - tests/integration/test_upstream_mcp_org.py
      - scripts/upstream_mcp_reference_server.py
      - tests/unit/test_upstream_mcp_reference_server.py
    brief: |
      Read first: `tests/integration/test_upstream_mcp_org.py` (the `stack` fixture) and sections 3.4 to 3.6
      of docs/1121/plan.md.
      1. In `stack`, declare one more tool `ref_list_proposals` (`gate: review`, `verb: read`) beside
         `propose_change`. In `scripts/upstream_mcp_reference_server.py` add a module-level
         `PROPOSALS = [{"id": "p1", "audience": "general", "text": "contact: bob@example.org"}]`, the
         `list_tools` entry `tool("list_proposals", "List change proposals.", [])`, and a handler that returns
         `CallToolResult(content=[TextContent(type="text", text="1 proposal")], structured_content={"proposals": [...]})`
         with the proposals filtered by the caller's audiences through the same `allowed()` the other tools
         use (the personal data is only in the structured part, so the test exercises `_scanned`). Add
         `test_list_proposals_returns_proposals_for_the_callers_audiences` in
         `tests/unit/test_upstream_mcp_reference_server.py` and update the two assertions that count the tools:
         `test_upstream_mcp_reference_server.py:131-133` (now four names) and `test_upstream_mcp_org.py:180-182`
         (now `("ref", "ready", 4, [])`).
      2. Add a fixture `token_stack` over `stack` that creates tokens in the store the running server already
         uses (`scoped_tokens.get_store()`; `ScopedTokenVerifier` reads it on every request, `mcp_auth.py:229-246`):
         `store.create(Principal(id="u1", email="ann@example.com", email_verified=True, is_admin=True), label=...,
         days=30, rules=[], tools=[...], policy=scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=365),
         upstream_audiences={"ref": ["general"]}, held_audiences=upstream_mcp_runtime.held_audiences(principal, True),
         principal_email_verified=True, is_admin=True, upstream_approvals=[...])`. The token owner needs
         `principal_email_verified=True`, or the claim has no audiences and no document is returned. The
         fixture yields a function `make_token(tools, approved)` returning the secret. Do not pass
         `scoped_token_policy` to `WebServer` (it has no such parameter).
      3. Tests, each driving the `mcp` client with the token as bearer:
         `test_a_token_with_a_ticked_popup_tool_can_call_it_unattended` (result returned, upstream saw one
         more POST, no pending approval); `test_a_token_without_the_tool_cannot_call_it` (a second token with
         `tools=["ref_search"]` calling `ref_propose_change` gets `this token may not call`) and
         `test_creating_a_token_with_a_gated_tool_and_no_approval_is_refused` (`create` raises
         `ScopedTokenError`); `test_each_call_writes_an_upstream_forwarded_entry_with_the_token` (read the audit log
         rows: `decision == "upstream_forwarded"`, `claim_principal_type == "n8n_workflow"`, `claim_jti`
         non-empty, `claim_req` non-empty, `metadata["token_id"]`, `metadata["gate"] == "popup"`);
         `test_a_changed_declaration_voids_the_approval` (after the token exists,
         `monkeypatch.setitem(scopes.UPSTREAM_DECLARATIONS, "ref.propose_change", "0" * 32)`; the call is
         refused with `no administrator approval` and the upstream's POST count is unchanged); `test_a_review_result_with_personal_data_is_still_denied_for_a_token`
         (token with an approval for `ref_list_proposals`; the call is refused and the review result is not
         returned); `test_an_auto_only_token_behaves_as_before` (token with `ref_search` only returns the
         general documents).
      Stop with `status=blocked` if the token cannot be accepted by the `stack` server without changing
      `src/` (a change in `src/` belongs to an earlier phase).
    acceptance:
      - python3 -m pytest tests/integration/test_upstream_mcp_org.py -q passes
      - python3 -m pytest tests/unit/test_upstream_mcp_reference_server.py -q passes
      - ruff check . passes
  - id: p6-retire-adr-docs-changelog
    title: ADR 0300, docs, generated tools reference and changelog
    depends_on: [p4-token-form, p5-end-to-end]
    complexity: S
    touches:
      - docs/adr/0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md
      - docs/adr/README.md
      - docs/upstream-mcp.md
      - docs/tools-reference.md
      - scripts/generate_tools_reference.py
      - CHANGELOG.md
    brief: |
      Read first: docs/adr/README.md (template, status rules), sections 3 and 4 of docs/1121/plan.md,
      docs/upstream-mcp.md ("What a person and a scoped token see") and scripts/generate_tools_reference.py
      (the "## Gates" text).
      1. Write `docs/adr/0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md`
         from the template: Status `Accepted — <today>. Implemented.` plus `Amends 0164`, `Amends 0236`,
         `Amends 0285` lines; Context (section 1 and 2 of the plan, without path:line); Decision (3.1 to
         3.8, one bullet each); Alternatives considered (the four rejected in 3.1, the schema digest in 3.2,
         refusing `review` in 3.6, live admin lookup in 3.4); Consequences (the Risks of the plan);
         Verification (the named tests of p1 to p5). Link no plan document. If a file
         numbered 0300 already exists when you start, stop with `status=blocked`. Add the row to
         `docs/adr/README.md` after the 0299 row; add one `Amended by` line to the Status of ADR 0164, 0236
         and 0285 (the only permitted edit of an accepted ADR).
      2. `docs/upstream-mcp.md`: replace the whole "**Known limitation.**" paragraph with a subsection
         `### Letting a token call a gated upstream tool` that says: an administrator ticks the tool in
         Settings → Scoped tokens → New token, under "Upstream tools that change data or release results
         unseen"; the tick approves the tool for every call of that token with no card; the effect text is shown
         next to the tick; a changed declaration (gate, verb, effect, url, audience, identity claim mode) voids
         the approval and the call is refused before it reaches the upstream; `review` results go to the token
         unseen but personal data still blocks them; an AI client cannot propose such a token; an
         administrator who loses the role keeps the token until it expires; the audit entries to look for
         (`upstream_forwarded` with `metadata.gate`, and `auto_accepted` whose rule is the approval row).
         In "## Reference upstream" add `list_proposals` to the list of tools the reference server serves, and
         in the operator-file example there. Link ADR 0300.
      3. `scripts/generate_tools_reference.py`: in the "## Gates" text add one paragraph: `Tools of an
         upstream MCP server (declared by an operator, see [Upstream MCP servers](upstream-mcp.md)) are
         not listed here. Their gate is the one the operator declared, and an administrator can approve a
         \`review\` or \`popup\` one for a scoped token ([ADR 0300](adr/0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md)).`
         Then run `python3 scripts/generate_tools_reference.py` and commit `docs/tools-reference.md`.
      4. `CHANGELOG.md` under `## [Unreleased]` → `### Added` (do not open a version heading): one entry,
         `**An administrator can let a scoped token call a gated upstream tool.** ...` in the style of the
         neighbouring entries, linking the docs section and ADR 0300.
      5. Run `python3 -m pytest tests/unit/test_docs_tools_reference.py tests/unit/test_docs_references_exist.py -q`.
    acceptance:
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes
      - grep -c "Known limitation" docs/upstream-mcp.md prints 0
      - grep -n "Letting a token call a gated upstream tool" docs/upstream-mcp.md matches
      - grep -n "ADR 0300" docs/tools-reference.md matches
      - grep -n "0300" docs/adr/README.md matches
      - grep -n "gated upstream tool" CHANGELOG.md matches
      - python3 -m pytest tests/unit -q passes
```
