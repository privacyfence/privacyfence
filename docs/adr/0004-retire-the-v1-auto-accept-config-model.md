# ADR 0004: retire the v1 auto-accept config model

## Status

Accepted; implemented. No tracked GitHub issue number — like P3/P4/P6/P7/P8 before it, this phase's
own CHANGELOG entries describe the work inline rather than against an issue. This is P9, the final
phase of the policy v2 redesign (P0–P9) — see [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md)
and [ADR 0003](0003-separated-installs-only.md) for the two unrelated ADRs immediately before this
one in sequence, and the P3/P4/P6/P7/P8 entries under `CHANGELOG.md`'s `## [Unreleased]` heading for
the phases that got here.

## Context

Through 4.1, PrivacyFence had two overlapping ways to trust a resource: `auto_accept_rules`, a
per-operation list of `{rule, value}` entries evaluated by hand-written predicate functions in
`auto_accept.py`, and `auto_accept_grants`, a resource-scoped list (a trusted Drive sandbox folder,
say) that expanded into several operations' worth of rule matches at once via `resource_grants.py`.
Both were hand-maintained, neither described itself in terms of the other, and three operation
groups — Apps Script's tools, Gmail's filter tools, Slack's group-chat creation — had no config
model that could govern them at all (F5, found during P1's registry work).

P3 built a second evaluator, `policy/engine.py`, on a single scope+verb+condition model and ran it
in shadow mode alongside the original `AutoAcceptEvaluator`, comparing verdicts without acting on
either disagreement. P4 gave that model an on-disk shape (`auto_accept:` in `config/settings.yaml`)
and a one-time migration off the v1 sections. P6 and P7 each wired one live surface — the Settings
page, then the MCP bridge — to write the v2 shape directly, both gated behind `policy.engine: v1 |
v2` staying authoritative for whether v2's verdict was actually used. P8 added per-decision rule
attribution. Every phase up to P8 was additive: v1 stayed live, evaluated, and the fallback.

P9's own charter (the redesign plan this ADR's issue-less lineage traces back to) says: *"Delete
`compat.py`'s compiler, `resource_grants.py`, the seven tables, the shadow-mode plumbing and the
`policy.engine` switch. Rewrite the two docs sections into one; regenerate
`always-allow-rules-reference.md` from the registry instead of maintaining it by hand; add an ADR
recording the decision."* Exit criteria: *"No module imports `auto_accept_grants`; docs describe one
model."*

Auditing what actually still wrote to v1 at the start of this phase found the charter's own
one-paragraph scope was narrower than what "no module imports `auto_accept_grants`" requires to be
true without breaking something: two live surfaces still read and wrote v1 directly and had never
been ported —

- `gate.py`'s own interactive **Always allow** button on the approval popup (the most-used
  surface of the three, and the one every other surface's UX modeled itself on) still called
  `AutoAcceptEvaluator.add_auto_accept_rule`/`mutate_grants` and rendered its confirmation text
  from `auto_accept.describe_rule`/`SUGGESTION_FAMILIES` — none of which had a v2 equivalent wired
  in, only v2-shaped modules (`policy/propose.py`, `policy/describe.py`) sitting unused since P5.
- Org mode's own per-principal settings page (`web/routes_org_settings.py`) still rendered and
  edited `auto_accept_rules`/`auto_accept_grants` directly, untouched by P6's Settings-page rewrite
  (which only ever covered local mode's page).

Deleting `resource_grants.py` and the compiler out from under either surface without porting it
first would have been a straightforward regression: the most security-relevant interactive flow in
the product, and a whole per-principal admin surface, would stop working. This was put to the
person who owns the plan directly, with the narrower reading (delete only what's already unused,
leave both live surfaces on v1 permanently) offered alongside the full one; **full scope was
chosen**: port both remaining surfaces to v2, then delete.

## Decision

**Every live writer and reader of `auto_accept_rules`/`auto_accept_grants` moves to the v2
`auto_accept:` model in this phase, and the v1 machinery is then deleted rather than kept as a
second, unused code path.**

Concretely:

1. **The approval popup's Always-allow flow** (`gate.py`'s `_interact` closures inside
   `gated_call`) now uses `policy.propose.proposals_for()` for candidate scopes,
   `policy.describe.button_label`/`confirmation_text` for the UI text, and
   `auto_accept.add_policy_v2_rules()` to persist — the same primitives P6's Settings page and P7's
   bridge tool already used, rather than a fourth, popup-specific writer.
2. **Org mode's settings page** (`web/routes_org_settings.py`) is rewritten to list, add, and
   remove against `auto_accept.get_policy_v2_rules()`/`add_policy_v2_rules()`/
   `remove_policy_v2_rule()`, rendered with `policy.describe.rule_sentence()` and a scope+verb
   picker sourced from `policy.catalogue`, replacing its grant-rows-and-rule-rows editor entirely.
3. **The deprecated bridge aliases** (`privacyfence_list_auto_accept_rules`,
   `privacyfence_propose_auto_accept_rule_change`, kept from P7 for one minor release) are
   redirected to translate into v2 writes/reads rather than continuing to touch v1 sections
   directly, so they keep working after v1 evaluation is gone instead of silently going dark.
4. **`resource_grants.py` is deleted.** Its manifest of grant resource types, capability→verb
   expansions, and (critically, since nothing else in the codebase had this) the resolver callbacks
   `resource_names.py` uses to turn a grant's resource id into a display name are preserved in a
   new module, `policy/resource_registry.py`, with exactly the three call sites that still need
   them: the one-time v1→v2 migration, name resolution, and the deprecated bridge alias's
   grant-shaped input translation. Nothing evaluates a grant against a live call through this module
   — that authority moved to `policy/engine.py` in P3.
5. **`policy/compat.py`'s compiler keeps exactly one caller: `migrate_to_policy_v2`.** Every
   shadow-mode comparison, the `policy.engine` config key, and the `AutoAcceptEvaluator` class
   itself (along with `add_auto_accept_rule`/`remove_auto_accept_rule`/`get_current_config`/
   `mutate_grants`/`suggest_rule`/`describe_rule` and the rest of the v1-only surface) are deleted
   from `auto_accept.py`. What remains is genuinely shared state (temp-accept, rule-changed
   listeners) plus the seven tables/functions every phase from P0 onward already depended on
   verbatim (`TOOL_TO_OPERATION`, `TOOL_TO_GATE`, `ReviewContext`, and the extraction helpers), kept
   unchanged.
6. **`always-allow-rules-reference.md` is generated, not hand-maintained**, from
   `policy.registry.TOOL_REGISTRY` and `policy.propose.PROPOSABLE_SCOPES`
   (`scripts/generate_always_allow_reference.py`), with a drift-guard test
   (`tests/unit/test_generate_always_allow_reference.py`) failing CI if the checked-in copy and a
   fresh generation disagree — closing the exact failure mode a hand-maintained doc has (it drifts
   the moment a scope is added or a tool's gate changes and nobody remembers to update the table).
7. **`docs/TECHNICAL_REFERENCE.md`'s two sections — "Auto-accept grants" and "Auto-accept rules" —
   become one "Auto-accept" section** describing the single scope+verb+condition model, the four
   surfaces that write it (popup, Settings, bridge, org mode), and what migration does to an
   unmigrated config. `docs/org-mode-setup-guide.md`'s own description of `/settings` is rewritten
   to match.

Migration (P4's `migrate_to_policy_v2`) is untouched by this phase except for a fix uncovered while
porting the surfaces above: `daemon_main.py`'s org-mode principal loader
(`_load_principal_settings`) never called migration at all — only local mode's `run_app()` did — so
an org principal whose config had never been touched by a local-mode-style startup would have its
v1 rules read (for the settings page rewrite in this phase to display) but never migrated into the
v2 section the rest of the system now evaluates. Both call sites now share one helper,
`daemon_main._migrate_settings_to_policy_v2()`.

## Rationale

**Why full scope instead of leaving the popup and org mode on v1 indefinitely.** The narrower
reading technically satisfies "no module imports `auto_accept_grants`" by simply not deleting the
module — but the charter's other exit criterion, "docs describe one model," cannot be made true
while two live surfaces still exercise a second model docs would then have to keep describing
_as live_, not as history. A codebase where the most-used approval surface writes an v1 shape that
nothing evaluates without the deleted shadow/compat path would be a worse outcome than not starting
this phase at all: it would look finished (imports gone, docs rewritten) while quietly being broken
the next time anyone clicked Always Allow.

**Why `resource_registry.py` instead of deleting `resource_grants.py` outright.** Three real
call sites still need the manifest of grant resource types after this phase — migration (needs to
know what a v1 grant's capabilities expand to), `resource_names.py`'s resolvers (unrelated to
auto-accept evaluation; used for showing a human-readable name in the audit log and Settings UI, a
feature this phase does not remove), and the deprecated bridge alias's `target: "grant"` input
shape. A module that still has real callers is refactored, not deleted; what P9 actually removes is
the manifest's role as an *evaluation* input, which ended when P3's engine became authoritative for
every surface in this phase.

**Why generate the reference doc instead of just rewriting it by hand once.** The charter says so
explicitly, and the reasoning holds independent of the charter: this doc's failure mode under P6/P7
was exactly what a hand-maintained table always risks — new scopes and gate changes landing in code
without a corresponding doc edit, silently, because nothing forced the two to move together. A
generator with a CI drift guard makes that impossible rather than merely discouraged.

## Consequences

- **`AutoAcceptEvaluator`, `resource_grants.py`, `policy_engine_config.py`, the `policy.engine`
  config key, and every v1-only helper in `auto_accept.py` no longer exist.** A future change to
  auto-accept behavior has exactly one evaluator to reason about (`policy/engine.py`) and one
  writer contract (`policy/propose.py` + `policy/store.py`) shared by all four surfaces, rather than
  needing to keep two models' behavior in sync by hand.
- **`auto_accept_rules`/`auto_accept_grants` remain readable on disk, indefinitely, but are never
  evaluated or written to again.** An install that has not restarted since before this phase shipped
  still migrates automatically on its next startup (P4's migration, now with the org-mode gap
  closed); nothing about migration's own behavior changes in this phase.
- **The two deprecated bridge tools keep working**, now backed by v2 underneath, rather than going
  dark the moment v1 evaluation was removed — consistent with P7's own "kept for one minor release"
  commitment.
- **Test coverage that exercised v1 predicates directly** (`test_auto_accept.py`'s ~30
  `AutoAcceptEvaluator`-based test classes, and `tests/unit/policy/test_conditions.py`/
  `test_scopes.py`'s equivalence checks against the live evaluator) moves to a frozen,
  standalone reference (`tests/unit/policy/_v1_reference.py`, extracted verbatim from the last
  commit before this phase's deletions) so the equivalence proof P4's migration relies on — "v2
  selectors agree with what v1 always did" — keeps being checked without a live dependency on code
  this phase deletes.
- **Nothing about what auto-accepts changes for an already-migrated install.** This phase is a
  structural retirement of a model that stopped being evaluated back in P6/P7 wherever
  `policy.engine: v2` was set; its user-visible effect is that the popup and org-mode settings page
  now render and confirm rules the same way the Settings page and bridge already did, and that a
  hand-maintained doc table cannot drift from the code again.

## Verification

- `grep -rn "auto_accept_grants\|resource_grants\|AutoAcceptEvaluator\|policy\.engine" src/` finds
  nothing outside comments describing migration/history and the (unevaluated) v1 config keys
  `migrate_to_policy_v2` still reads.
- `tests/unit/test_generate_always_allow_reference.py::test_checked_in_doc_matches_a_fresh_generation`
  fails CI the moment `docs/always-allow-rules-reference.md` disagrees with a fresh run of
  `scripts/generate_always_allow_reference.py`.
- `tests/unit/policy/_v1_reference.py`-backed equivalence tests
  (`tests/unit/policy/test_conditions.py`, `test_scopes.py`, `test_compat.py`) keep proving every
  v2 selector agrees with what the pre-P9 v1 implementation always did, without importing anything
  this phase deleted.
- `tests/unit/test_daemon_main.py::TestLoadPrincipalSettings` covers the org-principal migration
  gap this phase closed.
- Full unit suite green (`pytest tests/unit`); `docs/TECHNICAL_REFERENCE.md` and
  `docs/org-mode-setup-guide.md` each describe exactly one auto-accept model.

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) — unrelated subsystem.
- [ADR 0003](0003-separated-installs-only.md) — unrelated subsystem, immediately preceding ADR in
  sequence (merged into `releases/4.2-dev` while this phase was in progress).
- `CHANGELOG.md`'s `## [Unreleased]` → `### Added` — the P3/P4/P6/P7/P8/P9 entries record this
  redesign's phases in order.
- `docs/TECHNICAL_REFERENCE.md#auto-accept` — the single model this ADR's docs decision produced.
- `docs/always-allow-rules-reference.md` — the generated per-tool reference this phase introduced.
- [Issue #580](https://github.com/privacyfence/privacyfence/issues/580) — the deletion this ADR's
  decision 3 deferred ("kept ... for one minor release"): removing the two deprecated bridge
  aliases (`privacyfence_list_auto_accept_rules`/`privacyfence_propose_auto_accept_rule_change`)
  themselves, once a minor release has shipped with them present as v2-backed translators. Tracked
  as Phase 3 of the "Policy Surface Consolidation" plan alongside issue #579.
