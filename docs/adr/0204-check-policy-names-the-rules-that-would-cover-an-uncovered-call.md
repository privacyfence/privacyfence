# ADR 0204: `privacyfence_check_policy` names the rules that would cover an uncovered call

## Status

Accepted — 2026-10-20. Implemented: `src/privacyfence/policy/suggest.py` and
`mcp_dispatch.check_policy`.

## Context

A scheduled runner gets no approval cards, so every tool step of a recipe needs an auto-accept rule
beforehand. `privacyfence_check_policy` said whether a call would pass, but not which rule would
make it pass. A runner (PrivacyFence Cockpit, or any cron job) therefore kept its own table of
tool to scope, resource and verb, which has to be edited every time a connector or a verb changes.

The scope catalogue is visible only through `privacyfence_list_policy`, and a scoped token may not
call that tool: it would show every rule the person has ([ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md)).
The derivation itself already exists: the approval card's "Always allow" button builds a rule from
a tool and its arguments (`policy/propose.py`).

## Decision

`privacyfence_check_policy` always returns `suggested_rules`: an empty list when the call is
`auto`-gated or would auto-accept, otherwise the rules that would cover it. Each entry is
`{group, label, value, value_known, verb, operation}`, in the catalogue's own terms.

- The list is derived from the call's own arguments, with no fetched content, by
  `policy.suggest.suggested_rules`. It walks the proposable scopes of the tool's connector, then
  the extra scopes (Grist document, Peek page, Apps Script project), keeps those whose verb and
  operation fit, and builds the value the way the card does.
- A scope that needs a value the arguments do not carry (a Drive file's folder, say) is listed with
  `value: null` and `value_known: false`: the rule names what the call reaches at run time.
- A rule that matches everything (`gmail.anything`, the calendar condition scopes, the value-less
  `gmail.filters` and `slack.group_chats`) is never suggested.
- A builder or selector that raises skips its candidate: no suggestion rather than a wrong one.
- Scoped tokens get the field too. It uses only the caller's own arguments and the public
  catalogue, so it reveals none of the person's rules.

## Alternatives considered

- **A tool-to-rule table inside `privacyfence-sync`**, which is what the cockpit carries today.
  It duplicates `policy/propose.py` and `policy/catalogue.py` and drifts the moment a connector
  or verb changes. The server is the only source of truth.
- **Letting scoped tokens call `privacyfence_list_policy`.** It shows every rule the person has,
  the reason ADR 0164 hides it. A suggestion built from the call's own arguments shows nothing new.
- **Calling `proposals_for`, the card's function.** The card proposes from the fetched content
  (`raw_data`), which a prediction does not have, and `proposals_for` cannot express "known only
  at run time".

## Consequences

- A runner and `privacyfence-sync permissions` can list the exact rules a recipe needs from the
  server, with no table of their own.
- `check_policy`'s answer is a little longer, and its tool description is at 983 of the 1,024
  characters a meta-tool description may have.
- The suggestion reflects the arguments only. A rule that is also needed because the fetched data
  reaches another resource is not predicted, and `check_policy` still says `unknown` there.

## Verification

`tests/unit/policy/test_suggest.py` (argument-derived values, run-time resources, never-suggested
scopes, fail-closed on a raising builder or selector); `tests/unit/web/test_mcp_dispatch.py` (the
`suggested_rules` key on every result); `tests/unit/web/test_tool_schema_portability.py` (the
description length). Client side: `sync/tests/test_permissions.py`.

## Related

[ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
[ADR 0161](0161-privacyfence-sync-is-a-separate-plain-mcp-client.md), [ADR 0172](0172-one-stored-auto-accept-rule-per-scope.md).
Source plan: `git show 7251c292^:docs/sync-scheduling-support-plan.md`.
