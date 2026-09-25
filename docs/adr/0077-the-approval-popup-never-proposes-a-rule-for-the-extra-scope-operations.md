# ADR 0077: The approval popup never proposes a rule for the extra-scope operations

## Status

Accepted (recorded retroactively on 2026-09-25; decided in `f9f9093c`, which introduced
`policy/propose.py` with these operations left out, and kept when `a1074344` and `0c8f166d` made
them configurable from Settings and the MCP bridge; all three merged in
[#541](https://github.com/privacyfence/privacyfence/pull/541)). Implemented.

## Context

The approval popup's **Always allow** button offers one rule candidate per scope in
`policy/propose.py`'s `PROPOSABLE_SCOPES` whose selector confirms the item under review
(`proposals_for`). It is a reaction to one gated call: the reviewer sees one item and is offered a
rule derived from it.

Six operation keys fit no such scope:

- `apps_script.read_content`, `apps_script.write_content` and `apps_script.read_execution_log`
  are governed by `apps_script.project`, a scope keyed on a script id.
- `slack.create_group_chat`'s subject is an audience. No scope type measures one, so the only rule
  that could cover it is unconditional ("anything in Slack").
- `gmail.create_filter` and `gmail.update_filter` can only be scoped by "anything in Gmail". A
  filter can silently archive or forward mail indefinitely, so a rule for it is an unconditional
  rule for a high-consequence operation.

A rule for any of them is a wide, standing trust decision. The popup shows one call and asks for a
quick answer, which is the wrong moment to make one.

## Decision

The popup never proposes a rule for these six operations. `policy/propose.py`'s
`PROPOSABLE_SCOPES` has no scope that governs them, so `proposals_for` returns nothing for their
tools and the popup renders no Always allow button for them: Deny and Allow once only.

They are still governable, deliberately. `policy/catalogue.py`'s `EXTRA_SCOPES` declares three
extra scope groups, `apps_script.project` (read, update), `gmail.configure` (configure, predicate
`gmail.anything`) and `slack.share_anything` (share, predicate `slack.anything`). The Settings
Auto-accept page and `privacyfence_propose_policy_change` (`gate.propose_policy_change`) both offer
them through `catalogue.scope_catalogue()` and `rules_for_catalogue_entry`, where the person picks
the connector and verb and sees what the rule unblocks before committing.

## Alternatives considered

- **Offer them from the popup like any other scope.** For Gmail filters and Slack group chats the
  only candidate is an unconditional rule, and one click on a popup would create it without its
  width being shown or chosen. For Apps Script a script-scoped proposal is possible, but a rule
  that covers reading and rewriting a script's source belongs with the same deliberate choice.
- **Leave them ungovernable (Allow once only, everywhere).** Every call would need a popup forever,
  even for a person who has decided they trust one script or want filters managed freely. The
  extra scopes give that person a way to say so on purpose.

## Consequences

- A reviewer who wants to stop being asked about these operations has to go to Settings (or have
  the AI client propose the change through `privacyfence_propose_policy_change`, which still asks
  for confirmation). The popup does not point there.
- Nothing in the popup can create an unconditional Gmail or Slack rule.
- The grant resource-type manifest (`policy/resource_registry.py`'s `GRANT_RESOURCE_TYPES`) does
  not reach these operations either; the tool registry (`policy/registry.py`) still gives each a
  verb and scope subject so rule descriptions can name them.
- Adding a `PROPOSABLE_SCOPES` entry that governs one of these verbs on these connectors would
  reverse this decision, and needs a new ADR.

## Verification

- `tests/unit/policy/test_propose.py`'s `test_the_extra_scope_operations_stay_unproposed`: no
  proposal for any of the six operations' tools.
- `tests/unit/test_settings_controller.py`'s `TestAddPolicyRule`
  (`test_each_of_the_six_previously_unreachable_operations_becomes_addable`) and
  `tests/unit/policy/test_catalogue.py`: each extra scope can be added deliberately.
- `tests/unit/policy/test_registry.py`'s `TestGrantManifestUnreachableOperationsAreCovered`: the
  six operations are absent from the grant manifest and still have a verb.

## Related

- `src/privacyfence/policy/propose.py` (module docstring, "What the popup deliberately does not
  propose"), `src/privacyfence/policy/catalogue.py` (`EXTRA_SCOPES`).
- [ADR 0075](0075-apps-script-gets-no-run-tool.md): the Apps Script tools these rules cover.
- [ADR 0074](0074-auto-accept-rules-are-identified-by-a-content-derived-id.md): how a decision is
  attributed to the rule that allowed it.
