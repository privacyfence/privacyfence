# ADR 0035: agent attribution reads the handshake on every call, and org mode attests only admin-pinned clients

## Status

Accepted — 2026-09-24. Not implemented; this answers the questions
[ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md) left open "before the first
PR", so that PR has nothing left to decide.
Amends [ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md).

## Context

ADR 0006 accepted the mechanism for attributing a request to the AI system that made it: the MCP
handshake (`clientInfo`) in local mode, the OAuth client in org mode, a per-credential override
for both, and a ranked `agent_source` recording which of those produced the answer. Its
*Verification* section left three questions open, and several facts it rests on have moved since
it was written. ADRs are frozen, so the corrections are recorded here rather than in 0006's body.

### Facts that have drifted since ADR 0006

Each was checked against the tree on 2026-09-24.

- **The SDK is `mcp` 2.2.0, pinned `mcp>=2.2,<3.0.0`** (`pyproject.toml`). ADR 0006 quotes
  `>=1.28,<3.0`. Its Status already carries a note to that effect (issue #618), and the two
  `routes_mcp.py` docstrings that repeated the old range (`_connection_of`, and the
  `_RehomeStaleInitialize` liveness check) already say `mcp>=2.2,<3.0.0`, so no code needed
  changing for it.
- **Line numbers in *Where this lands in the code* are stale.** In `web/routes_mcp.py`,
  `_track_session` is at :269 (not :303), `handle_call_tool` at :333, and the
  `principal_scope(principal)` that `agent_scope` sits beside is at :354 (not :317).
  `OrgOAuthProvider.register_client` is at `web/oauth_provider.py:331`, unchanged.
- **`StaticTokenVerifier` no longer exists.** [ADR 0008](0008-one-principal-per-os-user.md)
  replaced it with `PerUserTokenVerifier` (`web/mcp_auth.py:147`), which still mints
  `client_id="local"` (`:189`). The token is now one per OS user, at `handoff_dir()/mcp_token` on
  an unseparated install and `authority_dir(principal)/mcp_token` on a separated one. ADR 0006's
  "every AI system on that machine holds the same credential" therefore now reads "every AI system
  *one OS user* runs holds the same credential". The argument is unchanged: the adversary is still
  an agent running as that user, and it can still read that user's token.
- **The "Claude" count is 154 occurrences in 40 modules, not 142 in 35** (`grep -ro Claude
  src/privacyfence --include=*.py | wc -l`, and `grep -rl` for the module count). The
  user-visible, per-request ones are still the `approval_window_html.py` card copy ADR 0006
  lists.

### Open question 1: is the handshake reachable without parsing the body?

Yes, through a public property, but not where ADR 0006 proposed to read it.

- `ServerSession.client_params` (`mcp/server/session.py:65`) is public and returns
  `InitializeRequestParams | None`. It delegates to `Connection.client_params`
  (`mcp/server/connection.py:243`). `InitializeRequestParams.client_info` is an `Implementation`
  carrying `name`, `title`, `version`, `description`, `website_url` and `icons`. A handler reaches
  it as `ctx.session.client_params`, with no private attribute and no `_connection_of`.
- **Capturing once at `_track_session` does not work.** `StreamableHTTPSessionManager._handle_request`
  (`mcp/server/streamable_http_manager.py:193`) routes any request whose `MCP-Protocol-Version`
  header is not a handshake-era version to `handle_modern_request`. That path skips `initialize`
  and builds a fresh `Connection.from_envelope(...)` per request (`connection.py:260`), taking the
  client info from that request's own `_meta` envelope. There is no session id, so
  `_track_session` returns early at `_SESSIONLESS_KEY` and never sees it.
- `from_envelope` synthesizes `client_params` only when the envelope carries **both** a
  well-formed client info and well-formed capabilities (`connection.py:288`). Client info is
  optional on the modern protocol, so `None` is a normal value, not an SDK failure.

### Open question 3's missing fact: reading a client in org mode has a side effect

`get_access_token().client_id` is available inside `handle_call_tool` (the call at :350 already
uses the token). The name lives at `OrgOAuthProvider._clients[client_id].info.client_name`
(`_clients` is built at `web/oauth_provider.py:240`). But the public accessor,
`get_client()` (:322), bumps `last_used_at` and rewrites the whole of `oauth_clients.json` on every
call. It does that deliberately, so the TTL prune can tell a live registration from an abandoned
one. Calling it once per tool call to attribute that call would turn every gated request into a
disk write.

## Decision

### 1. The handshake is read from `ctx.session.client_params` on every tool call

`handle_call_tool` reads `ctx.session.client_params` on each call and resolves the agent from it
there, beside `principal_scope`. It is never captured once per session at `_track_session`, and
never cached by session key. The same connection object serves both protocol eras: the handshake
era sets `client_params` at `initialize`, and the modern era sets it per request from `_meta`. A
per-call read is therefore correct for both, and it costs one attribute access.

The read goes through `getattr` and degrades to *unknown* (`agent_source: ""`) on a missing
attribute, a `None` value or a mis-shaped object. It never raises. That is the same posture
`_connection_of` takes for the same reason: the pin is a range. The request body is still never
parsed for this, so the line `_is_initialize` draws stays where it is.

### 2. The initial registry, and what an unmatched client renders as

A registry entry is an `agent_id`, a display name, and a list of **exact** `clientInfo.name`
strings. Matching is case-insensitive equality on the sanitized name. There is no prefix, substring
or regex matching: a claimed name that merely *contains* `claude` is not Claude, and saying so
costs nothing because the tier is shown anyway.

| `agent_id` | Display name | `clientInfo.name` match | Source of the string |
|---|---|---|---|
| `claude-code` | Claude Code | `claude-code` | **Verified.** The shipped Claude Code binary constructs its MCP client as `{name: "claude-code", title: "Claude Code", …}` |
| `claude` | Claude | `claude-ai` | **Guess.** Widely reported in server logs for Claude Desktop and for claude.ai's remote connectors; Anthropic does not publish it. The two cannot be told apart by this string, so they share one entry. In local mode Desktop reaches `/mcp` through the `.mcpb` shim, which is a transport proxy (`mcpb/shim/src/index.ts`) and passes Desktop's own `initialize` through unchanged |
| `chatgpt` | ChatGPT | `openai-mcp` | **Guess.** Reported in server logs for ChatGPT connectors; OpenAI does not publish it |
| `gemini-cli` | Gemini CLI | `gemini-cli-mcp-client` | **Verified.** `google-gemini/gemini-cli`, `packages/core/src/tools/mcp-client.ts`, `new Client({name: 'gemini-cli-mcp-client', …})` |
| `cursor` | Cursor | `cursor-vscode` | **Guess.** Reported in server logs; Cursor does not publish it |

A guessed string that turns out wrong costs one mislabelled-as-unknown client, never a
mis-attribution, because matching is exact. The implementing PR corrects it from a real
handshake. An unrecognised name is never promoted to an entry by default.

**An unmatched client** renders as **"Unrecognised AI system"**, followed by its claimed name. The
name is sanitized to the 64-character cap with control and bidi characters stripped, and escaped
when rendered. It gets no icon and no brand colour. Its `agent_id` is `unknown:<sanitized name>`,
or `""` when no name was sent at all. Its `agent_source` is whichever signal produced the name
(`client_info` for a handshake). "Unknown" is thus a rendered state that carries the claim, not a
blank and not a fallback to "Claude". A client that sent nothing renders as unknown with
`agent_source: ""`, exactly as ADR 0006's third verification test requires.

`clientInfo.title`, `version` and DCR `client_name` get the same sanitize-and-escape treatment.
`clientInfo.icons` and `website_url` are never fetched and never rendered: a caller-supplied URL
on the approval card would be both a tracking beacon and a borrowed brand mark.

### 3. Org mode: an admin pins a client to an AI system; an unpinned client's name is a claim

*Decided by the maintainer on 2026-09-24, in the planning of this work.*

- **The pin is an admin-only Settings surface.** It persists a `client_id` → `agent_id` map in its
  own file under `org_dir()`, beside `oauth_clients.json`. It is not in `org_config.json`: DCR
  `client_id`s are UUIDs the server mints at runtime, so a file an admin prepares and signs ahead
  of time has nothing stable to name.
- **A pin is a sensitive settings write.** It creates attested identity, the only kind ADR 0006
  decision 3 lets a future rule key on. It is therefore classified sensitive under
  [ADR 0014](0014-every-bespoke-route-is-classified-or-the-app-refuses-to-start.md) and
  requires step-up under [ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md).
- **Only a pinned client is attested.** A call whose token's `client_id` has a pin records the
  pinned `agent_id` with `agent_source: "oauth_client"`.
- **An unpinned registration's DCR `client_name` is recorded as `agent_source: "client_info"`**,
  the claimed tier, and resolved through the same registry and unmatched rendering as a handshake
  name. The DCR name is preferred to the per-request handshake name when both exist, because it is
  the one an admin can see and pin. Neither makes the other attested.
- **A pin names a registration, not a name.** It never transfers to another `client_id`, including
  a re-registration with the same `client_name`. When the TTL prune removes a pinned registration,
  its pin becomes inert and the Settings surface shows it as stale. It does not silently move.
- **Per-call attribution reads without the side effect.** `OrgOAuthProvider` gains a read-only
  accessor that returns a registration's `client_name` without bumping `last_used_at` or
  rewriting `oauth_clients.json`. `get_client()` keeps its current behavior for the OAuth flows
  that need it.

**The argument.** ADR 0006 framed this as "the safe answer is `client_info`, the useful answer is
`oauth_client`". The pin removes that tradeoff: it makes the useful answer available only where
the safe one's condition holds.

`client_name` is self-declared at registration, by whatever did the registering. DCR is open to
any caller that can reach the authorization server. Treating an unpinned name as attested would
let anything able to register a client present itself as "ChatGPT" in the attested tier. That is
decision 3's bypass, moved from the handshake to the registration endpoint. Treating every name as
merely claimed would leave org mode with no attested identity at all, and org mode is where ADR
0006 said the strong version is built first. An admin pin is the one step that is not
caller-supplied: it is made by a human with step-up, is reviewable, and ends when the registration
ends. That is exactly what ADR 0006 option C said attestation requires.

### 4. Registry icons are the vendors' real brand marks, bundled

*Decided by the maintainer on 2026-09-24.*

The registry's icons are each vendor's own published brand mark, downloaded once by the
implementing PR and committed under `resources/agent_icons/`. They are not neutral glyphs. The PR
records where each was taken from. The marks are never fetched at runtime and never taken from
`clientInfo.icons`. ADR 0006 decision 4 still governs where a mark may appear: the attested tier
gets the icon, and a claimed identity does not present itself identically. A real brand mark is
the reason that rule matters, not a reason to relax it.

### Provenance ranking, unchanged

`override` > `oauth_client` > `client_info` > `endpoint` > `""`, as ADR 0006 decision 2 set out. The
first signal present wins. No step converts a caller-supplied signal into an attested
`agent_source`.

## Alternatives considered

- **Capture the handshake once at `_track_session`**, as ADR 0006 sketched. It sees nothing on
  the modern protocol path, which has no session id and no `initialize`, so every modern-protocol
  client would be attributed as unknown.
- **Parse `clientInfo` out of the `initialize` request body.** Not needed: the SDK exposes it
  publicly. It would also cross the line `_is_initialize` draws deliberately, and it would miss
  the modern path's per-request `_meta` in any case.
- **Prefix or substring matching in the registry** (`claude*`). This makes a name like
  `claude-exfil` render as Claude. A claimed identity cannot change an outcome, but the card is
  still a trust surface. Exact matching fails towards "Unrecognised", which is the honest result.
- **Treat an unpinned DCR registration as `oauth_client`.** Rejected in decision 3: DCR is
  open registration, so this attests a string the caller chose.
- **Put the pins in `org_config.json`.** Rejected in decision 3: that file is prepared, and
  optionally signed ([ADR 0016](0016-org-config-bundle-hash-log-and-signing.md)), ahead of time,
  before the `client_id`s it would name exist.
- **Call `get_client()` to read the name.** This makes every tool call rewrite
  `oauth_clients.json`.
- **Neutral glyphs instead of brand marks.** The maintainer chose real marks. The protection
  against a borrowed mark is decision 4 of ADR 0006, which withholds the mark from the claimed
  tier. Withholding the real mark from everyone buys nothing more.

## Consequences

- Attribution works identically for handshake-era and modern-protocol clients, at the cost of a
  per-call read that is one attribute access.
- The registry is small and partly guessed. Most clients will render as "Unrecognised AI system"
  with their claimed name until an entry is confirmed. That is the intended failure direction.
- Org mode gains a new persisted file and a new sensitive Settings action. An admin who never
  pins anything gets claimed-tier attribution only, which is still correct.
- Pins do not survive their registration's TTL prune. A client that goes unused past the TTL has
  to be re-pinned after it re-registers.
- Bundling real brand marks takes on the vendors' trademark-usage terms. The implementing PR
  records each mark's source so that a mark can be replaced if its terms change.

## Verification

ADR 0006's three verification tests stand. This ADR adds:

- `handle_call_tool` resolves the agent from `ctx.session.client_params` per call. A test drives
  one call on the modern protocol path, with a `_meta` client info and no session id, and sees
  the name attributed.
- A registry test pins exact, case-insensitive matching: `claude-code` matches, `claude-code-x`
  does not.
- An unmatched name renders as "Unrecognised AI system" plus the escaped name, with no icon, and
  records `unknown:<name>`. A missing name records `""`.
- In org mode an unpinned client records `client_info`, and a pinned one records `oauth_client`.
  Attributing a call leaves `oauth_clients.json`'s modification time unchanged.
- The pin route is classified sensitive, which the startup guard from ADR 0014 enforces.

## Related

- [ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md): the mechanism this
  amends
- [ADR 0008](0008-one-principal-per-os-user.md): the per-OS-user token that replaced
  `StaticTokenVerifier`
- [ADR 0009](0009-use-the-official-mcp-sdk.md): the SDK whose public `client_params` this relies on
- [ADR 0011](0011-org-mode-runs-its-own-oauth-authorization-server.md): the DCR registrations a pin
  names
- [ADR 0014](0014-every-bespoke-route-is-classified-or-the-app-refuses-to-start.md),
  [ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md): why the pin write is
  step-up gated
- Issues #579, #580
- `src/privacyfence/web/routes_mcp.py` (`handle_call_tool`, `_track_session`, `_is_initialize`),
  `src/privacyfence/web/oauth_provider.py` (`get_client`, `register_client`),
  `src/privacyfence/web/mcp_auth.py` (`PerUserTokenVerifier`)
