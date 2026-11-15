# Upstream MCP servers behind PrivacyFence

An organization server can put another MCP server (an "upstream", for example a knowledge hub) behind
PrivacyFence. The operator declares which of its tools exist and how each is gated; PrivacyFence
enforces that tool policy with cards, scoped tokens and the audit log, like a built-in connector.
Every call PrivacyFence forwards carries a short signed claim, bound to that one call, that names the
person, the audiences they hold and what is calling; the upstream filters its own queries by it.

Why it is built this way: [ADR 0285](adr/0285-upstream-mcp-servers-are-declared-connectors.md),
[ADR 0297](adr/0297-the-upstream-claim-is-an-eddsa-identity-claim-in-its-own-header-bound-to-one-call.md),
[ADR 0298](adr/0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md) and
[ADR 0299](adr/0299-group-membership-is-updated-through-a-scoped-token-tool.md).

- **Operators:** [write the file](#the-operator-file), [map groups to audiences](#groups-and-audiences),
  [keep membership current](#group-membership), restart.
- **Upstream implementers:** [verify the claim and filter by `audiences`](#for-upstream-implementers).
- **Try it:** [the reference upstream](#reference-upstream).

## For operators

Organization servers only. In any other mode (single-user, desktop) a non-empty
`PRIVACYFENCE_UPSTREAM_MCP_FILE` or `PRIVACYFENCE_UPSTREAM_MCP` refuses the start. On a Docker install
see [Upstream MCP file](org-mode-docker.md#upstream-mcp-file).

### The operator file

Set `PRIVACYFENCE_UPSTREAM_MCP_FILE` to the path of a YAML or JSON file (see the
[configuration reference](configuration-reference.md)). A name ending in `.yaml` or `.yml` is read as
YAML, any other name as JSON. Or put the same text in `PRIVACYFENCE_UPSTREAM_MCP` (always read as YAML,
so one-line JSON works too). Set one of the two, never both: both refuse the start. The declaration is
read once, at startup; **a change to it needs a restart**.

```json
{
  "version": 1,
  "upstreams": [
    {
      "id": "kh",
      "label": "Knowledge hub",
      "url": "https://kh.example.internal/mcp",
      "audience": "https://kh.example.internal",
      "tools": {
        "search":         {"gate": "auto",  "verb": "search"},
        "get_doc":        {"gate": "auto",  "verb": "read"},
        "propose_change": {"gate": "popup", "verb": "add",    "effect": "Adds a change proposal to the knowledge hub."},
        "write_derived":  {"gate": "popup", "verb": "update", "effect": "Writes a derived document to the knowledge hub."}
      }
    }
  ]
}
```

The same declaration in YAML (quote an `id` or tool name that YAML reads as a boolean, such as `"no"` or `"on"`):

```yaml
version: 1
upstreams:
  - id: kh
    label: Knowledge hub
    url: https://kh.example.internal/mcp
    audience: https://kh.example.internal
    tools:
      search: {gate: auto, verb: search}
      propose_change: {gate: popup, verb: add, effect: Adds a change proposal to the knowledge hub.}
```

The AI client sees each declared tool as `<id>_<tool>`, here `kh_search` and `kh_propose_change`. Any
mistake in the declaration refuses the start; the message names the path of the bad value
(`PRIVACYFENCE_UPSTREAM_MCP_FILE: upstreams[0].tools.search.gate: ...`) and never quotes the value.

| Rule | Detail |
|---|---|
| Keys | `version` must be `1`. Only `version`, `upstreams`, `claim` and `membership` at the top; only `id`, `label`, `url`, `audience`, `tools`, `identity_claim`, `claim_lifetime_seconds` and `allowed_principal_types` in an upstream (an old `grants` key is refused with `grants were replaced by groups`); only `gate`, `verb`, `effect` in a tool. Anything else is refused. |
| Size | At most 8 upstreams and 50 tools per upstream; the file or inline text is at most 256 KiB. |
| Repeated key | A key that appears twice in one mapping or object is refused (`the file repeats a key.` or `the text repeats a key.`), so a repeated tool name cannot silently keep the last gate. |
| Format | A YAML file is checked by the same rules as JSON; a wrong top level still reads `must be a JSON object`. |
| `id` | 2-16 characters, lowercase letters and digits, starting with a letter; unique. It may not collide with a built-in connector name (such as `gmail`, `drive`, `privacyfence`): `upstream id "<id>" collides with the built-in connector "<name>"`. |
| `label` | 1-60 characters, no control characters. It names the server on cards and in the tool descriptions. |
| `url` | `https://` with any host, or `http://` only for `localhost`, a loopback address or a single-word host name (a container on the same Docker network, such as `http://knowledge-hub:8080/mcp`). No user name, no fragment. |
| `audience` | Required, 1-200 characters, no whitespace. It becomes the `aud` of every claim; the upstream accepts only claims with its own audience. Not to be confused with the *audiences* a person holds. |
| `identity_claim` | `required` (the default) signs every call; `off` sends no claim and resolves no caller. |
| `claim_lifetime_seconds` | 5-60, default 60: how long a claim is valid. |
| `allowed_principal_types` | A non-empty list from `claude_client` (a person's AI client) and `n8n_workflow` (any scoped token); default both. A caller of another type is refused before anything is sent. |
| tool name | 1-48 letters, digits, underscores or hyphens; `<id>_<tool>` is at most 64 characters and must not collide with an existing tool. |
| `gate` | `auto` (runs at once), `review` (runs, then the person sees the result before it is returned) or `popup` (the person approves before the call is sent). |
| `verb` | One of the policy verbs. A **read verb** (`read`, `download`, `search`) takes `auto` or `review` and no `effect`. **Every other verb** takes `popup` and must carry an `effect`: 1-200 characters, ending in a full stop, shown on the card. |

So an ungated write cannot be declared, as long as the verb says it is a write.

> **Declaring a writing tool with a read verb skips the approval card.** PrivacyFence cannot see what an
> upstream tool does, only what you declared. A tool that changes data must carry a verb such as `add`,
> `update` or `delete`.

PrivacyFence asks the upstream for its tool list once, at startup, and retries with a growing delay until
it answers. It never fetches the list again: **restart PrivacyFence to pick up a change in the upstream's
tools or descriptions.** A declared tool the upstream does not list, or lists with a schema PrivacyFence
cannot pass on (not an object schema, a `reason` property of its own, over 32 KB, or any `$ref`, `$defs`
or `definitions`), is skipped and logged. Each exposed tool gets one extra required `reason` parameter,
like every PrivacyFence tool. The schemas the upstream sends are passed to AI clients as they are, so
whether a given client accepts a nested type or an `enum` is the operator's responsibility to check.

Approval cards show the server, the tool and the argument names, never the argument values. A popup
tool's card also offers the arguments' text for review. Upstream tools have no "Always allow" button and no
auto-accept rules yet.

### Groups and audiences

An *audience* is the upstream's own label for a slice of its data (`all`, `mgmt`, `finance`). A person holds
the audiences of the Workspace groups they belong to. The map from groups to audiences is set in
`admin_config.json` under `upstream_mcp` (on an organization server, `org/admin_config.json`; see the
[configuration reference](configuration-reference.md#admin-config-orgadmin_configjson)):

```json
"upstream_mcp": {
  "kh": {"groups": {
    "everyone@company.example": ["all"],
    "mgmt@company.example":     ["mgmt"],
    "ceo-staff@company.example": ["mgmt", "finance"]
  }}
}
```

- A group name is lower case and email-shaped; an audience is 1-63 characters (`a-z`, `0-9`, `_`, `-`,
  starting with a letter or digit). At most 200 groups per upstream. A person's audiences are the union over
  every group they belong to.
- `all` is the one unrestricted audience: an addition to it applies at once. Every other audience is
  restricted and needs an administrator's approval for each person ([below](#group-membership)).
- **Grants were replaced by groups.** A `grants` key in the declaration is refused at
  start with this message; nothing is seeded from the declaration any more, and `admin_config.json` holds `groups`.
- A group map for an id the declaration does not declare is logged once and ignored. An invalid section is
  reported as `admin_config.json: upstream_mcp.<id>.groups: <problem>`; at startup it refuses the start, and
  in a running server the last good map stays in force and the error is logged.
- **"Verified" matters.** Audiences come only from an email address PrivacyFence treats as verified: the
  identity provider said the address is verified, or the identity provider's `trust_unverified_email` is on
  and the address is in an allowed domain (see [Who may sign in](org-mode-setup-guide.md#who-may-sign-in)).
  The single-user person is verified. An unverified or empty address gets no audiences, so the upstream
  returns no data for that person.
- **Edits apply without a restart:** PrivacyFence re-reads the file when its modification time changes. There
  is no Admin page control for the map; it is a file edit. Do it while no one is saving on the Admin page.

### Operator keys

Besides the per-upstream keys in the table above, the operator file has two top-level objects:

| Key | Meaning |
|---|---|
| `claim.key_overlap_hours` | 1-720, default 24: how long a rotated key stays in the key set after it stops signing. |
| `membership.source` | `snapshot` (the default: membership comes from the [sync tool](#group-membership)) or `static`. |
| `membership.file` | With `static` only: the path of a JSON file `{"version": 1, "groups": {"mgmt@company.example": ["a@company.example"]}}`, re-read when it changes. No approvals; for a first try and for tests. |
| `membership.max_additions` | 1-1000, default 20: how many additions one snapshot and the last 24 hours together may hold before the snapshot is rejected. |

`identity_claim`, `claim_lifetime_seconds` and `allowed_principal_types` are described in the table of rules
above. A change to any of these needs a restart.

### Group membership

PrivacyFence does not read your directory. A job of yours (an n8n workflow that lists the Workspace groups)
tells it who is in each group by calling the tool **`privacyfence_update_group_membership`** with
`{"groups": {"mgmt@company.example": ["a@company.example", "b@company.example"]}}`. Each call is a snapshot of
the groups it names. The tool answers `{"status": "applied" | "rejected", "added", "removed", "parked",
"ignored_groups", "reason"}`.

- **Only a scoped token can call it, and only an administrator can create that token.** In Settings → Scoped
  tokens → New token an administrator ticks `privacyfence_update_group_membership` under "Tools that need no
  rule". An AI client cannot call the tool or propose a token for it. Keep the secret in the job's credential
  store, never in a chat.
- **Removals apply at once.** A person who is absent from a group in the snapshot loses that group's audiences
  at the next call.
- **Additions to `all` apply at once; additions to any other audience wait.** The person is *parked* until an
  administrator opens **Admin → Group membership** and approves or declines them for that group and audience
  (one person, or everyone pending for that group and audience). An approval is only for that group and
  audience, and is forgotten when the person leaves the group.
- **Groups no upstream's map names are ignored** and never stored; mapping a group later makes it appear in
  the next snapshot.
- **Limits.** At most 500 groups and 20000 member entries per snapshot. A snapshot is rejected, and nothing
  is applied, when its additions together with those of the last 24 hours exceed `membership.max_additions`
  (the very first snapshot is exempt), or when more than 500 approvals would be pending. A rejection is an
  audit entry, a log line and a row on the Admin page.
- Every call, approval and decline is in the audit log with counts and group names, never addresses.

The tool is also listed in the [tools reference](tools-reference.md).

### What a person and a scoped token see

- **A person** (through an AI client) gets the audiences their groups give them, with `principal_type`
  `claude_client`.
- **A scoped token** gets only the audiences its creator ticked, and never more than the creator holds. In
  Settings → Scoped tokens → New token, tick an upstream tool and a fieldset `Audiences for <label>` appears
  with one box per audience you hold, none ticked. A token made without ticking any sees no audience. A token
  only narrows: if the person later loses an audience, the token loses it too. Its claim has `principal_type`
  `n8n_workflow` and no email.
- An AI client cannot propose a scoped token that includes an upstream tool: the proposal is refused with
  `Upstream tools cannot be proposed; create the token in Settings.`

### Letting a token call a gated upstream tool

An upstream tool declared `popup` or `review` needs a person's card, and a scoped token has nobody to answer
one. An administrator can approve such a tool for one token instead
([ADR 0300](adr/0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md)):

- In Settings → Scoped tokens → New token, an administrator ticks the tool under "Upstream tools that change
  data or release results unseen". Nobody else sees that group, and a local server never does.
- The tick approves the tool for every call of that token, with no card. The tool's effect text is shown next
  to the tick, with "writes data" for a `popup` tool and "result not reviewed by a person" for a `review` one.
- If the operator changes the tool's declaration (gate, verb, effect, url, audience or identity claim mode),
  the approval stops working and the call is refused before it reaches the upstream.
- A `review` result goes to the token without a person seeing it, but personal data in it still blocks the
  call.
- An AI client cannot propose such a token.
- An administrator who later loses the role keeps the token until it expires or is revoked.
- In the audit log look for `upstream_forwarded` with `metadata.gate` (`auto`, `review` or `popup`), and for
  `auto_accepted` whose rule is the token's approval row.

### Rotating the claim key

Claims are signed with an Ed25519 key kept in `upstream_claim_keys.json` in the org directory and published at
`<PrivacyFence address>/upstream-mcp/jwks`. To rotate it, run `privacyfence-app --rotate-upstream-claim-key` as
the account the daemon runs as. The new key is published at once and starts signing five minutes later, so an
upstream that caches the key set for five minutes has it first; the old key stays published for
`claim.key_overlap_hours`. `--revoke-upstream-claim-key KID` removes a key from the key set at once (for a
leaked key) and refuses the only signing key. See the
[configuration reference](configuration-reference.md#command-line-options).

### Audit

Every call PrivacyFence forwards to an upstream writes one `upstream_forwarded` audit entry with the claim's
`jti`, `sub`, principal type, audiences and `req` and how the call ended (including a refusal or a signing
failure), so your upstream's log can be matched to PrivacyFence's. The claim and the arguments are never logged.

### `privacyfence_status`

On a server with upstreams, `privacyfence_status` carries an `upstreams` list:

```json
{"id": "kh", "label": "Knowledge hub", "tools_exposed": 4, "state": "ready", "missing_tools": []}
```

`state` is `waiting_for_tool_list` until the first successful `tools/list` (`tools_exposed` is 0 and AI
clients see no tools from that upstream until then), then `ready`. `missing_tools` names declared tools
the upstream did not list or whose schema was skipped.

### Errors and size

If the upstream cannot be reached, the AI client gets `The <label> server could not be reached.` A result
over 1 MB is refused (`ask for less`). If the upstream answers with an error, its text reaches the AI
client, with control characters removed and cut to 500 characters, like its results do: do not put
secrets in an upstream's error messages. Each call opens one short MCP session to the upstream.

## For upstream implementers

PrivacyFence sends the header `X-PrivacyFence-Claim: <jwt>` on **every HTTP request** to your MCP endpoint, a
new claim each time. It never sets `Authorization`; that stays free for your own use. Treat the claim as the
only proof of who is asking; PrivacyFence holds the approvals and the audit trail, you hold the data.

The full contract, with the claim fields, the `req` binding of a claim to one tool call, the verification
checklist and the versioning, is [identity-claim.md](identity-claim.md). The short form:

- Verify with `contrib/verifier/pf_claim_verify.py`, one file you can vendor (Python, with `PyJWT` and
  `cryptography`). Give it your `audience`, PrivacyFence's issuer address and the JWKS address
  `<PrivacyFence address>/upstream-mcp/jwks` (Ed25519 keys, cache five minutes, honor the `kid`). Pass it the
  raw request body so it can check `req`.
- **Only `sub`, `audiences` and `principal_type` may change what a query returns.** Everything else is for
  your own audit.
- A request that is not a tool call carries no audiences. A service claim (PrivacyFence's startup tool list)
  has none either: return the tool list for it and no data.
- Filter by `audiences` inside the query, not afterwards, so ranking and pagination stay correct and a guessed
  document id is still refused. PrivacyFence never filters your results. A row-level security sketch is in
  [identity-claim.md](identity-claim.md#row-level-security-sketch-postgresql).
- Answer `401` for any failure, with no detail.

## Reference upstream

`scripts/upstream_mcp_reference_server.py` is a small upstream for trying all of this and for tests. It is
not for production. It verifies claims with `pf_claim_verify.py` and serves `search`, `get_doc`,
`list_proposals` and `propose_change` over four in-memory documents in the `general` (two), `hr` and `finance` audiences, returning
only the caller's audiences; a service claim gets the tool list and no documents.

Run it against a PrivacyFence server (the Ed25519 keys come from its JWKS address):

```
python3 scripts/upstream_mcp_reference_server.py --host 127.0.0.1 --port 8099 \
    --audience ref-upstream --issuer https://privacyfence.example.org \
    --jwks-url https://privacyfence.example.org/upstream-mcp/jwks
```

Use `--jwks-file <file>` (a saved copy of the JWKS) instead of `--jwks-url` when the upstream cannot reach
PrivacyFence. The `--issuer` is your PrivacyFence address with no suffix.

The operator file the manual check uses (`ref`, tools exposed as `ref_search`, `ref_get_doc`,
`ref_list_proposals`, `ref_propose_change`), with a static membership file so no sync job is needed:

```json
{
  "version": 1,
  "membership": {"source": "static", "file": "/etc/privacyfence/membership.json"},
  "upstreams": [
    {
      "id": "ref",
      "label": "Reference upstream",
      "url": "http://refup:8099/mcp",
      "audience": "ref-upstream",
      "tools": {
        "search":         {"gate": "auto",  "verb": "search"},
        "get_doc":        {"gate": "auto",  "verb": "read"},
        "list_proposals": {"gate": "review", "verb": "read"},
        "propose_change": {"gate": "popup", "verb": "add", "effect": "Adds a change proposal to the reference upstream."}
      }
    }
  ]
}
```

The static membership file says who is in which group:

```json
{"version": 1, "groups": {"everyone@example.org": ["admin@privacyfence.localhost"],
                          "finance@example.org":  ["admin@privacyfence.localhost"]}}
```

Map the groups to audiences in `admin_config.json` under `upstream_mcp.ref.groups`
(`"everyone@example.org": ["general"]`, `"finance@example.org": ["finance"]`); the file is re-read when it
changes, so the next `ref_search` returns the `general` and `finance` documents with no restart. Remove the
person from the `finance` group in the membership file and the next call returns `general` only. Do this only
while no one is editing the Admin page: the server also writes `admin_config.json`.

## Testing

An upstream has no live provider to sign in to and nothing external to drift, so it has no live QA fixture
([testing policy](testing-policy.md#layer-5-live-connector)). The reference server stands in for it: unit
tests drive it in process, and an integration test runs it on loopback against a real organization server.
