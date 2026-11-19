# Upstream MCP servers behind PrivacyFence

An operator declares a hosted MCP server (Canva, Framer and similar) in a small file. Each person signs in there with their own account on the Connect page, and an administrator reviews the server's tools once on the Admin page. After that the tools reach AI clients as `<id>_<tool>`, behind PrivacyFence's approval gates.
This is organization mode only. The earlier claim-based upstream feature of 8.0.0 is removed ([ADR 0329](adr/0329-hosted-mcp-servers-sign-in-each-person-with-their-own-oauth-and-the-claim-feature-is-removed.md)); [Leftovers of the removed feature](#leftovers-of-the-removed-feature) says how to clean up.

## For operators

Declare the servers in a file named by `PRIVACYFENCE_UPSTREAM_MCP_FILE` (YAML for `.yaml` or `.yml`, any other name as JSON), or inline in `PRIVACYFENCE_UPSTREAM_MCP`. Set one, never both; the text is at most 256 KiB and a repeated key is refused. Both are read once at start, so a change needs a restart. The [Docker guide](org-mode-docker.md#upstream-mcp-file) shows where the file goes.

```yaml
version: 2
upstreams:
  - id: canva
    label: Canva
    url: https://mcp.canva.com/mcp
    oauth:
      scopes: [design:content:read, design:meta:read]
  - id: framer
    label: Framer
    url: https://mcp.framer.com/mcp
    allowed_principal_types: [claude_client]
```

The file holds `version: 2` and `upstreams` (at most 8). A file with another version, including a version 1 file written for the removed feature, refuses the start. An error reads `<source>: upstreams[<i>].<key>: <problem>`; any other key, `tools` and `audience` included, is `is not a known key`.

| Key | Rule |
|---|---|
| `id` | Required. A lower-case letter then 1-15 lower-case letters or digits, unique. It may not be a built-in connector name, nor `docs` or `sheets`. It prefixes every tool: `canva_<tool>`. |
| `label` | Required. 1-60 characters, no control character. Shown to people. |
| `url` | Required. The server's MCP address: `https://`, or `http://` only for `localhost` or a loopback address; no user name or fragment. |
| `allowed_principal_types` | Optional. A non-empty list from `claude_client` and `n8n_workflow`. Default: both. Checked on every call. |
| `oauth.scopes` | Optional. 1-20 scopes, each 1-100 characters with no whitespace. See [Cautions](#cautions). |
| `oauth.client_id` | Optional. Use an app you registered with the vendor instead of registering dynamically. 1-200 characters, no whitespace. |
| `oauth.client_secret_env` | Optional, only with `client_id`. The *name* of an environment variable that holds the secret (capital letters, digits, underscores). A variable that is not set or is empty refuses the start. |
| `oauth.issuer` | Optional. Pins the authorization server: an `https://` address the server's metadata must list. |

PrivacyFence finds the vendor's sign-in endpoints from the server address itself (RFC 9728 and RFC 8414 metadata), and registers itself as a client on the first Connect (RFC 7591) unless you declared a `client_id`. It requires S256 PKCE. Addresses in that metadata must be `https://` and public, or loopback when the server itself is. The redirect address is `<server address>/upstream-mcp/<id>/callback`; a vendor that refuses it needs a declared client ([ADR 0330](adr/0330-oauth-upstreams-discover-their-server-register-a-client-and-keep-tokens-per-person.md)).

A declared client with a secret keeps working after a restart: the sign-in method that worked at Connect is read back from the stored token.

What the server keeps, in the data directory: each person's tokens in `users/<id>/credentials/upstream_mcp/<id>.json` (plaintext at mode 0600, like every other third-party token; [ADR 0330](adr/0330-oauth-upstreams-discover-their-server-register-a-client-and-keep-tokens-per-person.md) says why they are not encrypted), the dynamic registrations in `org/upstream_oauth/` and the saved tool lists in `org/upstream_tools/`. Back them up with the rest of the data.

## For administrators

Nothing is exposed until an administrator has reviewed the tools. Connect first on `/connect`, then open **Admin → Upstream MCP servers**. One card per server shows its address, the sign-in (`OAuth at <host>, <n> person(s) connected, client declared|dynamic`) and its tools.

1. **Refresh tools** lists the server's tools with *your own* connection and shows them as a pending list. Without a connection the button is off and says to connect first. Nothing changes for anyone yet.
2. Review each tool. The default gate is `review` when the server says the tool is read-only and not destructive, and `popup` for everything else. A tool that is new or whose description or schema changed is marked `new` or `changed` and `Not exposed until you save.`
3. **Save** (with your passkey) makes the pending list the saved list. It takes effect at once, even for people who are already connected, and AI clients are told the tool list changed. **Save gates** changes gates on the saved tools without a refresh.

**Hold until Save.** Until you save, AI clients keep seeing the previous saved list, whatever the server now lists or says in its descriptions. A server cannot add a tool or rewrite one behind your back. A `changed` tool comes back at its default gate, not its saved one, and its people's Always allow rules stop matching until you choose `auto` again. A tool the server lists that PrivacyFence cannot carry (an unusual name or schema, more than 50 tools) is marked unsupported with the reason and never saved. If another administrator refreshes while you review, Save answers `The tool list changed since you refreshed it. Refresh again.`

**Annotations are only a default.** `readOnlyHint` and `destructiveHint` come from the server, which PrivacyFence does not trust. They seed the default and the verb (`read`, `update` or `delete`) that scoped-token scopes use, and you decide. A `review` call runs *before* the person sees the result, so the list warns `Runs before the person sees anything.` on a `review` tool that is not read-only. See [Cautions](#cautions).

**Gates.**

| Gate | What a person meets |
|---|---|
| `review` | The call runs, then the person sees the result and may deny its release. |
| `popup` | The person approves the call first. |
| `auto` | A ceiling, not a gate: the tool keeps asking as `review` or `popup` until a person makes an **Always allow** rule for that one tool. |

Every tool has its own Always allow rule, shown on the approval card; one rule never covers another tool of the same server. Lowering a tool later leaves the stored rule in place but matching nothing.

**Scoped tokens.** A token acts through its creator's connection to the server. A scoped token that lists a gated upstream tool needs either your tick on the New token form ([ADR 0300](adr/0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md), pinned to the tool's description, schema, gate, verb and server address) or its own Always allow rule for that tool, which only an `auto` tool offers. The form names the connection the token uses. Details: [ADR 0331](adr/0331-the-administrator-manages-each-upstream-servers-tools.md).

The Admin actions answer with these errors: `Unknown upstream.` (400), `Gate must be review, popup or auto.` (400), `That tool is not in the saved list.` (400), `Refresh the tool list first.` (409), `The tool list changed since you refreshed it. Refresh again.` (409), and a 502 with the connection message when the refresh fails (below).

## For people

Open `/connect`. Each declared server has a row. **Connect** sends you to the vendor to sign in and approve; you return to `/connect` with `<label> connected.` The badge then reads `Connected` and the link `Reconnect`, which signs in again. A declined or failed sign-in reads `Could not connect <label> -- sign-in was declined, or the server could not be reached.` Rows are listed in sorted id order. There is no Disconnect button: remove the grant on the vendor's side.

Calls the AI client makes read as:

- `<label> is not connected. Connect <label> on the Connect page (/connect).`
- `The <label> sign-in has expired or was revoked. Connect <label> on the Connect page (/connect) to sign in again.`
- `<label> needs more access than PrivacyFence was granted. Connect <label> again on the Connect page (/connect).`
- `The <label> server could not be reached.`
- `The <label> result is larger than 1 MB; ask for less.`
- `The <label> server returned an error: <text>` (the vendor's own text, cut to 500 characters). The tool list comes from the administrator's account, so a person whose account lacks a tool gets the vendor's error for it.
- `PrivacyFence does not allow this caller to use the <label> server.` (`allowed_principal_types`).

## What `privacyfence_status` reports

An `upstreams` row per declared server: `id`, `label`, `tools_exposed`, `state` (`waiting_for_admin` while no list is saved, with `admin_hint` `An administrator has to refresh the tool list on the Admin page.`; else `ready`), `connected` for the caller and, when not connected, `connect_hint`. A person with no connection to a server sees none of its tools.

## Errors and size

A tool result over 1,000,000 characters is refused (`too_large`). A tool's description is cut to 700 characters for the AI client, and every tool gains a required `reason` argument that the approval card shows. No token, code, secret or response body is logged or shown in an error.

## Audit

Each forwarded call writes `upstream_forwarded` (the server id, the gate, `auth: oauth` and the result kind; never an argument, a token or server text). Admin actions write `upstream_tools_refreshed`, `upstream_tools_saved` and `upstream_tool_gates_changed`, with the server id and counts only.

## Cautions

- **Declare `oauth.scopes`.** Without them PrivacyFence asks for every scope the server lists, and logs `Upstream <id> declares no oauth.scopes; it will ask for every scope the server lists.` at start. Ask for the least that the tools you want need.
- **A reverse proxy that logs the callback query** keeps the one-time sign-in code. PKCE bounds the damage, but turn that logging off for `/upstream-mcp/*/callback`.
- **The client secret variable.** Anything that can read the daemon's environment can read `oauth.client_secret_env`. Use your orchestrator's secret store, never the declaration file, and never put it in chat.
- **A server that marks a write tool read-only** gets that tool the `review` default, and `review` forwards the call before anyone sees it. Read each default before you save, and set such a tool to `popup`.
- **One shared tool list.** Refresh reads a server-wide list with the refreshing administrator's account; it changes nothing at the vendor.
- **Token acts as its creator.** A scoped token calls the vendor as the person who made it.

## Leftovers of the removed feature

PrivacyFence never reads or deletes these, and logs one warning each at start. On every server that used the claim-based feature:

1. Delete `org/upstream_claim_keys.json`, `org/upstream_claim_keys.lock` and `org/group_membership.json` from the data directory.
2. Remove `PRIVACYFENCE_UPSTREAM_MCP_FILE` or `PRIVACYFENCE_UPSTREAM_MCP`, or replace the file with a version 2 one.
3. Remove the `upstream_mcp` section from `org/admin_config.json`.

Tools that depended on it, such as the `kh_*` tools, stop working, and an existing scoped-token approval of an upstream tool is void until an administrator ticks it again. Old audit lines keep reading.

## The reference server for development

`scripts/upstream_mcp_oauth_reference_server.py` plays the vendor's side with no real accounts. It approves every sign-in at once, keeps all state in memory and is not for production:

```
python3 scripts/upstream_mcp_oauth_reference_server.py --base-url http://127.0.0.1:8098 --port 8098
```

Declare `url: http://127.0.0.1:8098/mcp` (loopback is allowed over `http://`), then Connect, Refresh and Save as above. It serves the tools `whoami`, `echo` (read-only), `rename_thing` (unannotated) and `wipe_thing` (destructive), and `?login_hint=<name>` on its sign-in picks the account.
