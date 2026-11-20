# ADR 0333: Organization auto-accept rules are set by an administrator

## Status

Accepted — 2026-11-20. Implemented.
Amends [ADR 0257](0257-operational-settings-live-in-a-daemon-written-admin-config.md): the admin config also holds `org_rules`.

## Context

Only a person could create an auto-accept rule, and only for their own calls. An organization that
wants to say once, centrally, "reads of this Drive folder or this Grist document need no card for
anyone" had no way to do it: each person would have to make the same rule. The owner decided
(issue [#1167](https://github.com/privacyfence/privacyfence/issues/1167); plan
[`docs/1167/plan.md`](../1167/plan.md)):

- organization rules are fully general: every scope and every verb, with no restriction;
- a scoped-token call does not see them (a token's creator can copy one into the token);
- a person cannot switch an organization rule off.

## Decision

- **An administrator sets them.** The rules are rows in the same format as a person's rules
  (`scope_rows`), kept in the `org_rules` list of `org/admin_config.json` and managed on
  **Admin → Organization rules**. Three admin actions (`add_org_rule`, `remove_org_rule`,
  `set_org_rule_verbs`) are organization-only and always ask for a passkey. At most 100 rules, and
  only catalogue scopes: a custom row is refused. A load drops a row that does not parse, a custom
  row, a row with a token-only predicate and any row past the cap, so a hand-edited file cannot add
  what the page refuses; dropping only narrows.
- **One install-wide store.** `org_rules` holds the rows and their compiled rules in process memory,
  is filled at start-up and reloaded after each admin change, and tells listeners when it changes.
- **Order.** An unscoped call checks the person's own rules first, then the organization's, then the
  temp-accept window. The first match decides, and the source of the list that matched is kept for
  the audit entry ([ADR 0334](0334-the-audit-entry-names-who-allowed-a-call.md)).
- **Not under a scoped token.** A scoped call reads only the token's own rules (ADR
  [0236](0236-a-scoped-token-holds-its-own-rules.md)). A new token can copy an organization rule into
  itself; the copy is then the token's own row.
- **Read-only to people and AI clients.** A person sees them under "Set by your organization" on the
  Auto-accept page and an AI client sees them as `organization_rules` in `privacyfence_list_policy`;
  `privacyfence_check_policy` reports `rule_source`. `privacyfence_propose_policy_change` refuses an
  organization rule id before any popup, and an AI client cannot add one.
- **Pending cards are re-evaluated for every person.** Adding a rule can resolve a card another
  person is waiting on, so each pending card remembers its principal and is evaluated in that
  person's scope.
- **Changes are audited** with the decisions `org_rule_added`, `org_rule_removed` and
  `org_rule_verbs_changed`.
- **Unchanged:** PII confirmation overrides every rule, and the tag-writing tools stay exempt from
  rules.

### Accepted risks and gaps

- **Drive tags.** A rule on a Drive tag lets every person's AI use the tagged files, and anyone who
  can edit a tagged file can forge the tag ([ADR 0321](0321-drive-tags-are-plain-properties-and-collaborators-can-forge-them.md)). Organization rules stay fully general, so
  the risk applies to them; the Admin form says so and suggests read verbs.
- **Unattended sessions.** An organization rule auto-accepts inside a person's unattended
  (scheduled) session exactly as a person's rule does. It is a new reach an administrator now sets
  for everyone.
- **Security config hash.** It does not change with organization rules; the audit entry's `rule_id`
  and `allowed_by` identify the rule instead.
- **Short ids.** An organization rule's value shows as a short id on a person's page, because name
  resolution covers only the person's own rows.
- **Value-less "mine" scopes** (for example Drive files I own) as organization rules mean each
  person's own files.
- **No departure handling.** Rules persist until an administrator removes them; the Admin page lists
  them.
- **Audit view.** Settings → Audit log "Recent decisions" does not show who allowed a call; the new
  column is in the export and the JSONL.
- **Single-user installs** have one administrator; organization rules there duplicate personal
  rules and do no harm.

## Alternatives considered

- **Storing them in each person's `settings.yaml`** — the person could delete them, and they would
  drift from the administrator's list.
- **The install-wide `settings.yaml` that holds the privacy policy** — it is edited by other actions
  and read only at start-up for most keys; the admin config is what the Admin page already writes
  and hot-reloads (ADR 0257).
- **Organization rules inside scoped tokens** — every existing token would widen silently (ADR 0236).
- **Checking organization rules before the person's** — a person's "Matched Nx" counts would drift.
- **A per-person opt-out** — not wanted.
- **Restricting them to read verbs or excluding Drive tags** — the owner chose fully general.

## Consequences

- An administrator can allow a resource for everyone with one saved rule, and a bad rule is a
  server-wide bypass of the card; the passkey and the audit rows are the guard.
- Removing a rule re-asks the next call, and pending cards it had resolved are not reopened.
- Token creators see organization rows when copying, labelled as set by the organization.

## Verification

- `tests/unit/test_org_rules.py`: `TestStore`.
- `tests/unit/test_admin_config.py`: `TestOrgRules`.
- `tests/unit/test_approvals.py`: `TestReevaluateEveryPrincipal`.
- `tests/unit/test_gate.py`: `TestOrgRules`.
- `tests/unit/web/test_mcp_dispatch.py`: `TestListPolicy`, `TestCheckPolicy`.
- `tests/unit/test_settings_state.py`: `TestOrgRuleRows`.
- `tests/unit/web/test_routes_admin.py`: `TestOrgRuleActions`.
- `tests/unit/test_settings_window_html.py`: `TestOrgRulesUi`;
  `tests/integration/test_settings_org_rules_browser.py`.
- `tests/unit/test_scoped_tokens.py`: `TestCopyOrgRule`; `tests/unit/web/test_server.py`:
  `TestScopedTokenOrgRows`.

## Related

- Issue [#1167](https://github.com/privacyfence/privacyfence/issues/1167), plan
  [`docs/1167/plan.md`](../1167/plan.md)
- ADR [0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
  [0172](0172-one-stored-auto-accept-rule-per-scope.md),
  [0236](0236-a-scoped-token-holds-its-own-rules.md),
  [0257](0257-operational-settings-live-in-a-daemon-written-admin-config.md),
  [0320](0320-drive-rules-match-on-file-tags-not-folder-ancestry.md),
  [0321](0321-drive-tags-are-plain-properties-and-collaborators-can-forge-them.md),
  [0334](0334-the-audit-entry-names-who-allowed-a-call.md)
- [`org_rules.py`](../../src/privacyfence/org_rules.py),
  [`gate.py`](../../src/privacyfence/gate.py),
  [`admin_config.py`](../../src/privacyfence/admin_config.py)
