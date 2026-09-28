# ADR 0092: Claude clients are matched by the names they were seen sending, and an unrecognised DCR name yields to a recognised handshake name

## Status

Accepted — 2026-09-28. Implemented.
Amends [ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)
(decision 2's registry and exact-match rule, and decision 3's "the DCR name is preferred").
Part of [issue 46](https://github.com/privacyfence/privacyfence/issues/46).

## Context

ADR 0035 decision 2 seeded `agent_identity.REGISTRY` with one verified Claude name (`claude-code`)
and one guess (`claude-ai` for both claude.ai and Claude Desktop). It said the implementing work
corrects a guess from a real handshake. The maintainer's manual QA against 5.0.0a2 on 2026-09-28
([M1.1](https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865032001),
[M1.2](https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865527889),
[M1.3](https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865753049),
[M1.4](https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865556451)) is that
handshake. It found:

| Client | Mode | DCR `client_name` | Handshake `clientInfo` | Recorded before this ADR |
|---|---|---|---|---|
| claude.ai | org | `Claude` | not observable; version `1.0.0` | `unknown:Claude` |
| Claude Code 2.1.283 | org | `Claude Code (privacyfence)` | `claude-code` 2.1.283 | `unknown:Claude Code (privacyfence)` |
| Claude Code 2.1.283 | local | — | `claude-code` 2.1.283 | `claude-code` |
| Claude Desktop 2.9939.2, `.mcpb` extension | local | — | `local-agent-mode-privacyfence` 1.0.0 | `unknown:local-agent-mode-privacyfence` |

Nobody was seen sending `claude-ai`. Two of the observed names embed a name the **user** chose:
the part in parentheses in Claude Code's DCR name is the server name given to `claude mcp add`,
and the suffix of Claude Desktop's handshake name is the extension's server name. ADR 0035's
exact-match rule cannot match either for every user.

The org-mode Claude Code run also showed a precedence gap. `_resolve_agent` preferred the DCR name
whenever it gave any identity at all, `unknown:<name>` included, so an unrecognised DCR name
stopped the lookup and a handshake name the registry does know (`claude-code`) was never tried.

Claude Desktop's organization-mode custom connector (M1.5) was not run: no account available
allowed custom connectors. It most likely registers through the same claude.ai account, as
`Claude`, but that is unverified.

## Decision

### 1. The registry holds the observed names, and only those

- `claude-code` matches `claude-code` (handshake) and the template `Claude Code ({server})` (DCR).
- `claude` matches `Claude` (claude.ai's DCR name). The guess `claude-ai` is removed.
- A new entry, `claude-desktop` / "Claude Desktop", matches the template
  `local-agent-mode-{server}` (the extension's handshake). It gets its own mark,
  `resources/agent_icons/claude-desktop.png`, a copy of the Claude mark, with its row in that
  directory's README. claude.ai and Claude Desktop now present distinct names, so ADR 0035's
  reason for one shared entry no longer holds.

claude.ai's handshake name stays unknown and is not guessed. It matters only if its DCR name
stops matching.

### 2. A name template is a fixed prefix and suffix around the user's server name

A registry entry may carry name templates beside its exact names. A template contains exactly one
`{server}` placeholder. It matches a whole sanitized name made of the template's fixed prefix, a
non-empty server name, and its fixed suffix, case-insensitively. This is the only relaxation of
ADR 0035's exact-match rule: the fixed parts are the vendor's own, the one variable part is the
one the user chose, and no entry may carry an arbitrary pattern. A name that merely contains
`claude` still matches nothing.

The risk is unchanged from ADR 0035's: a matched name is still a claim (`client_info`), shown as
"Says it is …" with no mark, and never keys an outcome. A program that sends
`Claude Code (anything)` could already have sent `claude-code`.

### 3. An unrecognised DCR name yields to a recognised handshake name

In organization mode, attribution now resolves, strongest first:

1. an admin pin of the registration (`oauth_client`), unchanged;
2. a local override, unchanged;
3. the DCR `client_name`, if the registry recognises it;
4. otherwise the handshake `clientInfo.name`, if the registry recognises it;
5. otherwise the DCR name as `unknown:<name>`, the name an admin sees and can pin;
6. otherwise the handshake name as `unknown:<name>`, or unknown when neither was sent.

Every step after the pin records `client_info`. ADR 0035 preferred the DCR name because it is the
one an admin can see and pin; that still decides between two unrecognised names, and between two
recognised ones. It no longer lets an unrecognised name hide a recognised one, since both are
claims and neither is ever attested.

## Consequences

- Claude Code in organization mode is attributed to Claude Code without a pin, whatever server
  name the user chose. claude.ai is attributed to Claude. Both still need a pin to be verified.
- The audit log names Claude Desktop's extension as `claude-desktop`. Local-mode cards still show
  every requester as "Undetected"
  ([ADR 0088](0088-local-mode-shows-every-requester-as-undetected.md)).
- A registration pinned before this change keeps its pin; pins name an `agent_id`, and
  `claude` and `claude-code` keep theirs.
- The replay fixtures under `tests/fixtures/ai_clients/` are the captured ones, including
  claude.ai's `client_secret_post` authentication, which the seeded fixture had as `none`.
- When M1.5 runs, its result either confirms that Claude Desktop's connector registers as `Claude`
  or adds its name here in a new ADR.

## Alternatives considered

- **A prefix match on `Claude Code (`.** Rejected: an open-ended prefix is the start of the
  substring matching ADR 0035 ruled out, and it would match a name with anything after it. The
  template also fixes the closing parenthesis.
- **Exact names for the default server names only** (`Claude Code (privacyfence)`,
  `local-agent-mode-privacyfence`). Rejected: the Claude Code server name is whatever the user
  typed, so an exact name would only cover the users who kept the documented one.
- **Keep `claude-ai` in case claude.ai's handshake sends it.** Rejected: ADR 0035's rule is that a
  guess is corrected from a real handshake, and no client was seen sending it.
- **Let the handshake name always win over the DCR name.** Rejected: the DCR name is the one an
  admin sees on Settings → AI systems when deciding what to pin, so it stays first whenever it is
  recognised, and when neither is.
