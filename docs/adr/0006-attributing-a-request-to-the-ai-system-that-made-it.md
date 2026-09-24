# ADR 0006: attributing a request to the AI system that made it

## Status

**Accepted; not implemented.** What is accepted is the *mechanism* — how PrivacyFence learns which
AI system is calling, and what that knowledge is allowed to be used for. No code has been written,
and the open questions in *Verification* below are open.

The `mcp` range quoted below (`>=1.28,<3.0`) was already stale the day this was written:
`pyproject.toml` had required `mcp>=2.2,<3.0.0` since issue #250, three days before this ADR's own
commit. `pyproject.toml` is authoritative for the pinned range, as ADR 0009's *Related* section
already notes. The reasoning is unaffected: it never rested on 1.x behavior, only on properties
`_connection_of` (`web/routes_mcp.py`) documents as holding under mcp 2.x's
`ServerRequestContext`/`Connection` shape, so it holds unchanged across `>=2.2,<3.0.0` (issue #618).

Written ahead of the work rather than alongside it, because the question was asked as "separate URL
endpoints or auto-detect?" and the honest answer is that neither phrasing names the thing that
matters. Deciding that in a commit message, once, inside the first PR that happens to need it,
is how a product ends up rendering an unverified brand mark on a security-decision card and
discovering a year later that it meant nothing.

Sequenced against the multi-system rollout it exists for: organization mode first, local models
after. That ordering is load-bearing here and not incidental — see *What org mode already has*.

## Context

### What is being asked for

Two user-visible things, both straightforward on their own:

1. The audit log records which AI system made each request — Claude, ChatGPT, Gemini, and whatever
   comes next.
2. The review card shows it: the AI system's icon beside the connector's, and the system named in
   the card's own copy.

Both are reporting features. Neither is hard. The decision worth recording is the one underneath
them: **where the name comes from, and what it is worth.**

### Nothing identifies a caller today, because nothing needed to

Every tool call arrives at `web/routes_mcp.py`'s `handle_call_tool`, which resolves exactly one
identity — the *human* (`principal_from_access_token`, `principal_scope`) — and nothing about the
software making the call. That was correct while there was one: `approval_window_html.py` says
"What will be provided to Claude" as a flat statement of fact, and it has been one.

It stops being one the moment a second system connects, and it stops being one *silently*. A card
that says "Claude" about a ChatGPT request is not a missing feature; it is the governance surface
asserting something false at the exact moment a human is deciding whether to release data.

### The three signals, and what each is actually worth

| Signal | Where it lives | What it proves |
|---|---|---|
| `initialize` → `clientInfo.name`/`version` | MCP protocol. Every client sends it, no configuration | The client sent that string |
| URL path (`/mcp/<system>`) | Install-time configuration | Whoever holds the credential chose that path |
| OAuth `client_id` → DCR `client_name` | `OrgOAuthProvider._clients`, bound to the presented token | A registration a human admin can inspect, revoke and pin |

The first two are **claims**. The third can be made an **attestation**, and only in org mode.

### The shared credential is the whole problem

This is the fact that decides the question, and it is one of ours, not a limitation of MCP.

Local mode has exactly one MCP credential. `mcp_auth.load_or_create_mcp_token()` writes a single
`mcp_token` into `paths.handoff_dir()` — deliberately readable by the agent's own OS user, which
#428 Phase 4 settled on the grounds that "mcp_token stays reachable by the agent. It is the
agent's own credential and the product doesn't work without it". `StaticTokenVerifier` checks that
one secret and mints `client_id="local"` for everything that presents it.

So every AI system on that machine holds the same credential. A separate endpoint per system is a
door with the same key in every lock: any process holding `mcp_token` can POST to `/mcp/claude`
exactly as easily as to `/mcp/chatgpt`. The path is a claim about the caller made *by the caller*,
which is what `clientInfo` already is — at zero configuration cost and with no second auth surface.

This is the same trust boundary [ADR 0003](0003-separated-installs-only.md) settled and
[ADR 0005](0005-moving-the-approval-decision-off-the-device.md) measured: **the adversary is an
agent with code execution on this machine, running as the signed-in user.** Nothing that party can
reach can identify that party. Attribution in local mode is therefore attribution of a claim, in
every design, and the only question is whether the product says so.

### What org mode already has

Org mode is where the strong version already exists, unused.

`OrgOAuthProvider.register_client()` (`web/oauth_provider.py:331`) stores the full
`OAuthClientInformationFull` for every client that registers via DCR — `client_name` included —
persisted in `oauth_clients.json` and pruned on a TTL. The `client_id` that registration mints is
bound to the access token the request presents, and `mcp_auth.principal_from_access_token`'s own
docstring already names exactly what it is:

> an OAuth client_id identifies *which Claude installation* registered via DCR, not *which human*
> is using it — `client_id` is deliberately never used as a principal id here

That is precisely the field this ADR wants, correctly identified and correctly refused for the job
it was being considered for. It is not a principal. It is an *agent* identity, and there was no
such concept to put it in.

`client_name` is still self-declared at registration — but unlike a per-request claim it is
declared once, persisted, reviewable, revocable, and pinnable to a name an administrator chooses.
That is the difference between "ChatGPT said it was ChatGPT" and "our admin bound this client to
ChatGPT", and it is the difference that survives an audit.

### The precedent this follows

[ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) decision 6 met the same shape of
problem — a caller that cannot be told apart from the agent — and refused the obvious patch:

> do not try to make session minting un-callable by the agent; make a session insufficient

The same move applies, one layer over. Do not try to make agent identity unspoofable in local mode;
it cannot be. **Make a claimed identity insufficient to change any outcome.** Then a spoofed one
costs an attacker nothing and buys them nothing, and the feature is free to be what it actually is:
a reporting feature.

## The decision to be made

**Where does the calling AI system's identity come from, and what may PrivacyFence do with it?**

## Options

### A. A separate URL endpoint per AI system (`/mcp/claude`, `/mcp/chatgpt`)

- **No better than a claim**, per *The shared credential is the whole problem* above — while looking
  considerably more authoritative than one, which is the worst property a governance signal can
  have.
- **Multiplies the auth surface.** §10.3's audience separation ("the MCP access token must never be
  accepted on approval-decision endpoints, and the browser session cookie must never be accepted on
  /mcp") is asserted today against one path and has one test holding it. Each new path is another
  row in that matrix.
- **Multiplies org mode's OAuth surface.** RFC 9728 protected-resource metadata is *per resource* —
  `build_resource_metadata_url`/`create_protected_resource_routes` would need one document per path,
  and each path becomes a distinct resource identifier a client must discover and an audience a
  token must be scoped to. This is real work for no assurance.
- **Costs the one-line setup.** `claude mcp add --transport http privacyfence http://127.0.0.1:PORT/mcp`
  becomes a per-system instruction the user can get wrong — and getting it wrong produces a
  confidently mislabelled audit trail rather than an error.
- **Its one genuine merit**: it can label a system that identifies itself uselessly. Held for A′
  below.

### B. Resolve from the MCP handshake (`clientInfo`)

- **Zero configuration, universal.** It is the field the protocol defines for this, and every client
  already sends it.
- **A claim**, and honestly labelled as one.
- **Needs a name registry.** `clientInfo.name` is a vendor string (`claude-code`, `cursor-vscode`,
  `gemini-cli`, …), not a display name, so a match table is required — which is also where the icon
  lands, so it is needed regardless.
- **Unknown clients are the normal case, not the error case**, once third-party and local systems
  connect. They must resolve to something that renders, carries the raw claimed string, and does not
  look like a failure.

### C. Resolve from the authenticated OAuth client (org mode)

- **The strongest thing available**, and available now — see *What org mode already has*.
- **Org mode only.** Local mode has no DCR and one static token.
- **Becomes an attestation only with a pinning step** — an admin-maintained `client_id` → agent
  mapping. Without it, `client_name` is a claim made once instead of per-request, which is better
  but not different in kind.

### D. Per-credential configuration (human-declared mapping)

- **The escape hatch**, for the local system that reports `clientInfo.name: "python-mcp-client"` and
  for the org admin who wants a registration pinned rather than trusted.
- **Configuration, so it is only as good as the file it lives in** — which, on a separated install,
  is under the service account and out of the agent's reach. That makes a local override genuinely
  stronger than `clientInfo`, on exactly the boundary ADR 0003 established.
- **Not a primary mechanism.** Requiring it would mean no attribution at all until someone
  configures one, which is the outcome where the audit log is silently wrong.

## Decision

**B and C are the mechanism, D is the override, and A is rejected as a detection mechanism** — with
one narrow, deferred exception (A′).

### 1. Identity is resolved from the connection, not declared by configuration

Org mode resolves from the OAuth client (C); local mode from the handshake (B); either may be
overridden per credential (D). Configuration is never required for an entry to be attributed.

### 2. Every attribution carries its provenance, and provenance is ranked

The audit entry records not only *who* but *how*, in an `agent_source` field with a fixed
vocabulary and a defined ranking:

| `agent_source` | Tier | Meaning |
|---|---|---|
| `override` | **attested** | A human wrote this mapping into a config file the agent cannot reach |
| `oauth_client` | **attested** | An administrator registered and pinned this OAuth client |
| `client_info` | **claimed** | The caller said so in its `initialize` frame |
| `endpoint` | **claimed** | The caller chose a path; anything holding the same credential could have |
| `""` | **unknown** | Nothing resolved — recorded as unknown, never guessed |

This field is the reason the feature is defensible rather than decorative. `agent_name` alone reads
identically in both tiers, and a compliance reviewer a year later has no way to recover the
difference. Recording the claim without recording that it *is* a claim is the failure mode this ADR
exists to prevent.

It follows the precedent already set for `claude_reason`, which the audit log describes in exactly
these terms: *"Self-reported and unverified — never treated as fact."*

### 3. Attribution informs humans; only attested identity informs decisions

The load-bearing rule:

> **A claimed agent identity may change what a human is shown. It may never change what the system
> decides.**

Concretely: no `policy/conditions.py` selector, no auto-accept rule, no gate outcome may key on
`agent_id` unless `agent_source` is in the attested tier. An auto-accept rule matching a claimed
identity is not a rule — it is a documented bypass, available to any local process willing to type
a name into its handshake.

Adding an agent-scoped condition later *under this constraint* is ordinary work. Retrofitting the
constraint onto a shipped rule type that users already depend on is not, and the intervening window
is one in which the product ships a bypass. That asymmetry is why the rule is decided here, before
the first condition exists, rather than when someone asks for one.

### 4. The card shows the claim, and shows that it is a claim

A brand mark on an approval card is a trust signal. A hostile local process claiming to be Claude
would be borrowing ours, on the one screen where borrowed trust does the most damage.

So the agent's identity renders, and its *tier* renders with it: the attested tier gets the icon and
the name; the claimed tier does not get to present itself identically. The precise visual treatment
belongs to the implementing PR — what is decided here is that the two tiers must be distinguishable
on the card, not only in the log.

This is the same disclosure posture ADR 0002 decision 6 took with its confidentiality asymmetry:
say the weaker thing out loud rather than imply a property the design does not have.

### 5. A path alias is a labeling convenience, not a detection mechanism (A′, deferred)

`/mcp` stays canonical and stays the only documented setup. If the local-model phase finds systems
that cannot be told apart from their handshake, `/mcp/a/<agent-id>` may be added as an *alias* —
one route that pre-seeds the identity at `agent_source="endpoint"` and delegates to the same ASGI
app, same verifier, same resource identifier, one auth surface, §10.3 untouched.

Not now, and not for org mode, which has C. Recorded so that the option is not re-litigated from
scratch, and so that it is clear this is not option A: it adds a *label*, it does not add a *door*.

## Where this lands in the code

Sketch, not a work plan — the implementing PR owns the details.

- **`agent_scope`**, a fourth member of the contextvar family this codebase already uses for exactly
  this (`gate.reason_scope`, `gate.unattended_scope`, `principal.principal_scope`). Entered once,
  beside `principal_scope(principal)` at `web/routes_mcp.py:317`, for the same reason `reason_scope`
  gives in its own comment: no connector signature changes and no threading through ~95 call sites.
- **Handshake capture** at `_track_session()` (`web/routes_mcp.py:303`), which already holds the
  per-session `Connection`. The `initialize` params should be read from the SDK object, degrading to
  unknown rather than raising if the pinned range (`mcp>=1.28,<3.0`) moves it — the same posture
  `_connection_of` already takes. Note that reading it out of the request body instead would cross a
  line `_is_initialize` draws deliberately ("the *only* thing this module reads out of a /mcp request
  body: which method is being called is JSON-RPC envelope framing, not knowledge of what the method
  does"); if the SDK path proves unavailable, that principle is what has to be revisited, explicitly.
- **An agent registry**, structurally a twin of the connector icon set: match patterns → display
  name → bundled `resources/agent_icons/<id>.png`, with `approval_icons.agent_icon_path()` mirroring
  `connector_icon_path()` and reusing its `icon_data_uri` cache.
- **Audit fields** `agent_id`, `agent_name`, `agent_version`, `agent_source`, taking
  `CURRENT_SCHEMA_VERSION` from 4 to 5, all defaulting to `""` so an entry written before they
  existed is legacy rather than misreported — the same treatment every prior field addition got.
- **Card plumbing** from `card_builder.build_card_html` through to
  `approval_window_html._header_html`, whose kicker is today `[connector icon] PrivacyFence`.
- **The copy.** "Claude" is hardcoded 142 times across 35 modules. The user-visible ones cluster in
  `approval_window_html.py` and are structural card copy, not incidental: "What will be provided to
  Claude", "Why Claude is doing this", "What Claude already knows", and `_DISCLOSURE_BLOCK`'s
  "None — not disclosed to Claude". Templating those on the resolved display name is a self-contained
  change and should land **before** the detection work, so the two do not tangle in one review.

## Consequences

- **The audit log gains a field whose absence is meaningful.** Entries written before this exist
  carry `agent_source: ""`, which reads correctly as "this install did not yet distinguish" rather
  than as an attribution failure.
- **Org mode gets the strong version on day one**, which is why the rollout order matters: the first
  multi-system deployment is the one where identity can actually be attested, so the attested path
  is built first and the claimed path arrives already knowing it is the weaker sibling. Building
  local mode first would have produced the opposite — a claimed-identity design with attestation
  bolted on afterwards.
- **Local mode's attribution is honestly weaker, permanently.** It cannot be otherwise while
  `mcp_token` is one shared, agent-readable file, and that file is a deliberate #428 decision, not
  an oversight. A local override (D) on a separated install is the one way past it, and it is
  opt-in.
- **Auto-accept gains no new capability here**, deliberately. The first user to ask for "auto-accept
  Gmail reads from Claude only" gets it in org mode, where it can be attested, and does not get it
  in local mode, where it would be a bypass. That conversation is easier to have with this ADR
  already written than without it.
- **`privacyfence_status` and the meta-tools are unaffected.** They are not gated calls and attribute
  nothing.

## Out of scope

- **Per-agent policy.** Explicitly deferred by decision 3, which states the precondition rather than
  the feature.
- **Making local-mode identity attestable.** That would need per-agent credentials, which needs a
  way to issue them to an agent the product does not install — a different problem, and plausibly
  one ADR 0005's second device touches before this does.
- **Authenticating the agent to a *connector*.** This is about which system PrivacyFence is talking
  to, not about what a downstream service sees.
- **Anything about how non-Claude systems are onboarded, documented or supported.** This ADR decides
  how one is identified once it connects.
- **The visual design of the card treatment.** Decision 4 fixes the requirement, not the pixels.

## Verification

The implementing PR carries its own tests. Three belong to *this* file, because they are the ones
that decide whether the decision was actually taken rather than merely written:

1. **A claimed identity cannot change an outcome.** Two runs of the same gated call, identical in
   every respect but the `clientInfo` name, produce the same decision, the same rule match and the
   same released data. If a future change makes them differ, decision 3 has been lost.
2. **Provenance is never upgraded.** No path produces `agent_source` in the attested tier from a
   signal the caller supplied. In particular a `/mcp/a/<id>` alias (A′), if it is ever built, must
   record `endpoint` and never `override`.
3. **An unattributed entry says so.** An agent that sends no usable `clientInfo` produces an entry
   with `agent_source: ""` and a rendering that reads as unknown — not a default, not a fallback to
   "Claude", and not a blank that looks like the field was never added.

Open before the first PR:

- Where the pinned `mcp` SDK exposes the `initialize` params on the per-connection object, and what
  happens across the whole `>=1.28,<3.0` range. Everything in decision 1's local-mode half rests on
  this being reachable without parsing the body.
- What the initial registry contains, and — more importantly — what an unmatched client renders as,
  since that is the common case the day a third system connects.
- Whether the org-mode pinning step (C's attestation) is a config file or a Settings surface, and
  whether an unpinned DCR registration is `oauth_client` or `client_info`. The safe answer is the
  latter; the useful answer is the former; it should be argued, not defaulted.

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) — decision 6, "make a session
  insufficient", is the reasoning move this ADR reuses; its confidentiality asymmetry is the
  disclosure posture decision 4 follows
- [ADR 0003](0003-separated-installs-only.md) — the uid boundary that makes a local override (D)
  stronger than a handshake claim, and the threat model that makes everything else a claim
- [ADR 0005](0005-moving-the-approval-decision-off-the-device.md) — the same ceiling, stated for the
  approval decision rather than for caller identity
- `docs/security-and-compliance.md` — where local mode's attribution tier will have to be stated in
  the product's own words, at the volume the claim is made
- `src/privacyfence/audit_log.py` — `claude_reason`'s "self-reported and unverified" treatment, the
  existing precedent for recording a claim as a claim
