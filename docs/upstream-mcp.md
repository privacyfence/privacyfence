# Upstream MCP servers behind PrivacyFence

An organization server can put another MCP server (an "upstream", for example a knowledge hub) behind
PrivacyFence. The operator declares which of its tools exist and how each is gated; PrivacyFence
enforces that tool policy with cards, scoped tokens and the audit log, like a built-in connector.
Every request to the upstream carries a 60-second signed claim naming the person, the data domains they
may see and what is calling, and the upstream filters its own queries by it.

Why it is built this way: [ADR 0285](adr/0285-upstream-mcp-servers-are-declared-connectors.md),
[ADR 0286](adr/0286-privacyfence-sends-the-upstream-a-signed-caller-claim.md) and
[ADR 0287](adr/0287-data-domains-come-from-grants-on-a-verified-email-and-tokens-only-narrow-them.md).

- **Operators:** [write the file](#the-operator-file), [grant domains](#domain-grants), restart.
- **Upstream implementers:** [verify the claim and filter by `domains`](#for-upstream-implementers).
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
    grants:
      - match: "*"
        domains: [general]
```

The AI client sees each declared tool as `<id>_<tool>`, here `kh_search` and `kh_propose_change`. Any
mistake in the declaration refuses the start; the message names the path of the bad value
(`PRIVACYFENCE_UPSTREAM_MCP_FILE: upstreams[0].tools.search.gate: ...`) and never quotes the value.

| Rule | Detail |
|---|---|
| Keys | `version` must be `1`. Only `version` and `upstreams` at the top; only `id`, `label`, `url`, `audience`, `tools`, `grants` in an upstream; only `gate`, `verb`, `effect` in a tool. Anything else is refused. |
| Size | At most 8 upstreams, 50 tools and 200 grants per upstream; the file or inline text is at most 256 KiB. |
| Repeated key | A key that appears twice in one mapping or object is refused (`the file repeats a key.` or `the text repeats a key.`), so a repeated tool name cannot silently keep the last gate. |
| Format | A YAML file is checked by the same rules as JSON; a wrong top level still reads `must be a JSON object`. |
| `id` | 2-16 characters, lowercase letters and digits, starting with a letter; unique. It may not collide with a built-in connector name (such as `gmail`, `drive`, `privacyfence`): `upstream id "<id>" collides with the built-in connector "<name>"`. |
| `label` | 1-60 characters, no control characters. It names the server on cards and in the tool descriptions. |
| `url` | `https://` with any host, or `http://` only for `localhost`, a loopback address or a single-word host name (a container on the same Docker network, such as `http://knowledge-hub:8080/mcp`). No user name, no fragment. |
| `audience` | Required, 1-200 characters, no whitespace. It becomes the `aud` of every claim; the upstream accepts only claims with its own audience. |
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

### Domain grants

A *domain* is the upstream's own label for a slice of its data (`general`, `hr`, `finance`). Which people
hold which domains is set in `admin_config.json` under `upstream_mcp` (on an organization server,
`org/admin_config.json`; see the [configuration reference](configuration-reference.md#admin-config-orgadmin_configjson)):

```json
"upstream_mcp": {
  "kh": {"grants": [
    {"match": "*",               "domains": ["general"]},
    {"match": "@hr.example.com", "domains": ["hr"]},
    {"match": "ceo@example.com", "domains": ["hr", "finance"]}
  ]}
}
```

- `match` is `*` (any verified person), `@<domain>` (an email domain, case-insensitive) or one email
  address (case-insensitive). A person's domains are the union of every grant that matches them.
- A domain name is 1-63 characters: lowercase letters, digits, underscores and hyphens, starting with a
  letter or digit. At most 200 grants per upstream.
- A grant for an id the declaration does not declare is logged once and ignored. An invalid section is
  reported as `admin_config.json: upstream_mcp.<id>.grants[<n>]: <problem>`; at startup it refuses the
  start, and in a running server the last good grants stay in force and the error is logged.
- **"Verified" matters.** Grants match only an email address PrivacyFence treats as verified: the
  identity provider said the address is verified, or the identity provider's `trust_unverified_email` is on
  and the address is in an allowed domain (see [Who may sign in](org-mode-setup-guide.md#who-may-sign-in)).
  The single-user person is verified. An unverified or empty address gets no domains, so the upstream returns no data for
  that person. Sessions that began before this feature carry no verification bit and give no domains until
  the person signs in again.
- **Seeding grants.** An upstream may carry `grants` in the declaration itself (a list of `match` and
  `domains`, as above). On start they are copied into `admin_config.json` once per upstream id. An id that
  is already there is never touched, even when its grants are empty, and nothing is removed: taking an
  upstream out of the declaration removes nothing. After the first start `admin_config.json` owns the
  grants ([ADR 0288](adr/0288-the-upstream-mcp-declaration-can-be-yaml-or-inline-and-seeds-the-grants.md)).
- **Grant edits apply without a restart:** PrivacyFence re-reads the file when its modification time
  changes, and the next call uses the new grants. There is no Admin page control for grants; it is a file
  edit. Because every claim lives 60 seconds, a revoked grant stops working within a minute.

### What a person and a scoped token see

- **A person** (through an AI client) gets the domains their grants give them.
- **A scoped token** gets only the domains its creator ticked, and never more than the creator holds.
  In Settings → Scoped tokens → New token, tick an upstream tool and a fieldset `Data domains for <label>`
  appears with one box per domain you hold, none ticked. A token made without ticking any sees no
  domain. A token only narrows: it cannot add a domain the person lacks, and if the person later loses a
  grant the token loses it too.
- A scoped token's calls carry `act.type` `workflow`; a person's carry `ai_client`.
- An AI client cannot propose a scoped token that includes an upstream tool: the proposal is refused with
  `Upstream tools cannot be proposed; create the token in Settings.`

**Known limitation.** An upstream tool declared `review` or `popup` has no scope-catalogue entry yet, so the
New token form lists only the `auto` upstream tools. Scoped-token rules for upstream tools are a later
change.

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

PrivacyFence sends `Authorization: Bearer <jwt>` on **every HTTP request** to your MCP endpoint, a new
token each time. Treat it as the only proof of who is asking; PrivacyFence holds the approvals and the
audit trail, you hold the data.

**Verify it** (the [reference server](#reference-upstream) does all of this):

- Fetch the keys from `<PrivacyFence address>/upstream-mcp/jwks` (public, no sign-in; cache it, honor the
  `kid`, refetch on an unknown `kid`). Accept **RS256 only**.
- Require the header `typ` `pf-upstream-claim+jwt`. It keeps a claim apart from the OpenID Connect ID tokens
  PrivacyFence also signs, with a different key.
- Require `aud` equal to the `audience` the operator declared for you, `iss` equal to
  `<PrivacyFence address>/upstream-mcp`, and `exp` (60 seconds after `iat`); check `nbf` too. Use no clock
  leeway beyond a second or two.
- Keep a `jti` cache for the token's lifetime and refuse a `jti` you have seen.
- Answer `401` for any failure, with no detail.

**The claim**

| Claim | Meaning |
|---|---|
| `sub` | The person's id; `privacyfence` for a service claim. |
| `email`, `email_verified` | The person's address when verified, otherwise `""` and `false`. |
| `domains` | Sorted list of the data domains the caller may see. Filter inside your own queries by it. |
| `act` | What is calling: `{"type": "ai_client", "attested": bool, "name": "...", "claimed_name": "..."}`, `{"type": "workflow", "token_id": "...", "label": "..."}` (a scoped token), or `{"type": "service"}`. |
| `pf_upstream`, `pf_tool` | The upstream id, and the upstream tool name for a `tools/call` (`null` otherwise). |
| `iat`, `nbf`, `exp`, `jti` | Timing and the replay id. |

**Only `sub`, `domains` and `act.type` may change what a query returns.** Everything else (`email`,
`act.name`, `act.claimed_name`, `act.label`, `pf_tool`) is for your own audit: a client name is only claimed
unless `attested` is true, and a label is whatever the token's creator typed.

**The service actor.** PrivacyFence's startup `tools/list` is signed with `act.type` `service`, `sub`
`privacyfence`, `domains: []`. Return the tool list for it and no data: an upstream that follows the
contract returns nothing for a caller with no domains.

**Check ids where the data lives.** Filter by `domains` inside the query, not afterwards, so ranking and
pagination stay correct and a guessed document id is still refused. PrivacyFence never filters your results.

### Row-level security sketch (PostgreSQL)

Set the caller's domains once per transaction and let the database enforce them:

```sql
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;

CREATE POLICY documents_by_domain ON documents
  USING (domain = ANY (string_to_array(current_setting('app.domains'), ',')));
```

```sql
BEGIN;
SET LOCAL app.domains = 'general,hr';   -- the claim's domains, comma-joined, after verification
SELECT id, title FROM documents WHERE body ILIKE '%policy%';
COMMIT;
```

Bind the list as a parameter (`SELECT set_config('app.domains', $1, true)`), never by string
concatenation. An empty list must give an empty `app.domains`, which matches no row. Connect as a role that is
not the table owner and has no `BYPASSRLS`.

## Reference upstream

`scripts/upstream_mcp_reference_server.py` is a small upstream for trying all of this and for tests. It is
not for production. It verifies claims as above and serves `search`, `get_doc` and `propose_change` over four
in-memory documents in the `general` (two), `hr` and `finance` domains, returning only the caller's
domains; the service claim gets the tool list and no documents.

Run it against a PrivacyFence server (the signing keys come from its JWKS address):

```
python3 scripts/upstream_mcp_reference_server.py --host 127.0.0.1 --port 8099 \
    --audience ref-upstream --issuer https://privacyfence.example.org/upstream-mcp \
    --jwks-url https://privacyfence.example.org/upstream-mcp/jwks
```

Use `--jwks-file <file>` (a saved copy of the JWKS) or `--public-key-file <pem>` instead of `--jwks-url`
when the upstream cannot reach PrivacyFence. The `--issuer` is your PrivacyFence address followed by
`/upstream-mcp`.

The operator file the manual check uses (`ref`, tools exposed as `ref_search`, `ref_get_doc`,
`ref_propose_change`):

```json
{
  "version": 1,
  "upstreams": [
    {
      "id": "ref",
      "label": "Reference upstream",
      "url": "http://refup:8099/mcp",
      "audience": "ref-upstream",
      "tools": {
        "search":         {"gate": "auto",  "verb": "search"},
        "get_doc":        {"gate": "auto",  "verb": "read"},
        "propose_change": {"gate": "popup", "verb": "add", "effect": "Adds a change proposal to the reference upstream."}
      }
    }
  ]
}
```

With PrivacyFence running in the Docker container `privacyfence` (organization image, data in
`/var/lib/privacyfence-org/.privacyfence`), set grants by editing `admin_config.json` in the container.
Grant every domain to the single-user person and `general` to everyone else:

```
docker exec -i privacyfence python3 - <<'PY'
import json, os
path = "/var/lib/privacyfence-org/.privacyfence/org/admin_config.json"
config = json.load(open(path))
config.setdefault("upstream_mcp", {})["ref"] = {"grants": [
    {"match": "*", "domains": ["general"]},
    {"match": "admin@privacyfence.localhost", "domains": ["general", "hr", "finance"]},
]}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(config, f, indent=2)
st = os.stat(path)
os.chmod(tmp, st.st_mode & 0o7777)
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, path)
PY
```

Then keep only the `*` grant; the next `ref_search` returns `general` documents only, with no restart:

```
docker exec -i privacyfence python3 - <<'PY'
import json, os
path = "/var/lib/privacyfence-org/.privacyfence/org/admin_config.json"
config = json.load(open(path))
config.setdefault("upstream_mcp", {})["ref"] = {"grants": [{"match": "*", "domains": ["general"]}]}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(config, f, indent=2)
st = os.stat(path)
os.chmod(tmp, st.st_mode & 0o7777)
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, path)
PY
```

Do this only while no one is editing the Admin page: the server also writes this file.

## Testing

An upstream has no live provider to sign in to and nothing external to drift, so it has no live QA fixture
([testing policy](testing-policy.md#layer-5-live-connector)). The reference server stands in for it: unit
tests drive it in process, and an integration test runs it on loopback against a real organization server.
