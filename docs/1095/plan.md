# A PrivacyFence node for n8n

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** In n8n you install the community node `n8n-nodes-privacyfence`, add a PrivacyFence credential (your server's `/mcp` address and a scoped token) and drop one **PrivacyFence** node into a workflow. You pick a tool from a list that shows only what the token may call, type a reason and the tool's arguments, and get the tool's JSON as the item; a refused call stops the node with PrivacyFence's own text, or becomes an error item with Continue On Fail. A second operation lists the tools. An administrator can switch **Admin → Agent runtimes → Credential type in n8n** to the PrivacyFence node, after which delivered credentials (issue 1090) are PrivacyFence credentials, not bearer ones; without the community node installed, delivery says so and mints no token.

**Scope:** A new npm package in `n8n/` (credential, one node usable as an AI Agent tool, a small MCP client of its own), published by a new workflow on each stable release tag with npm provenance. On the daemon side one new setting, `agent_runtimes.n8n.credential_type`, carried through delivery, the records, the status tool and the texts; the default stays `httpBearerAuth`, so nothing changes until an administrator switches. CI, the definition of done, docs and five ADRs follow. The scoped-token model and the gate do not change.

**Security:** The node sends the token only to the credential's own address, never follows a redirect and never puts the token or a request into an error or an item. The new credential holds the address as well as the token, so whoever can edit it can point the token elsewhere (§3.6). The package has no runtime dependencies and is published only from GitHub Actions with provenance.

**Review focus:** the node's hand-written MCP handshake client and why it speaks only the handshake revision (§3.3); how errors and items are built so the token cannot leak (§3.4); the credential shape without an `authenticate` block (§3.2); the delivery changes, the fail-closed check that maps only a 404 to "node not installed", and the old-records rule (§3.5); the publish workflow, its rerun skip and the token-then-trusted-publishing path (§3.7).

## 1. Goal

Today a workflow that calls PrivacyFence uses n8n's generic **MCP Client** node: the author types the endpoint, picks n8n's built-in bearer credential, sees raw MCP content arrays and has to know the tool names and that a `reason` argument exists. This change gives n8n a PrivacyFence-aware node instead: the address comes from the credential, the picker lists only what the token can call, `reason` is a first-class field, the result is the tool's JSON and a refusal is a clear error. It also lets the delivery of issue 1090 hand an AI client a credential for that node. Tracking issue: https://github.com/privacyfence/privacyfence/issues/1095. Owner decisions, answered on the board on 2026-11-14: one action node that is also usable as an AI Agent tool; an Admin choice of credential type, default unchanged; the package lives in this repository and is published on each stable release tag; built to n8n's verification rules now, submission later.

## 2. Current state

- **Delivery** (issue 1090, ADRs 0278-0282). `agent_runtimes.py` holds `N8nRuntimeConfig` (`enabled`, `base_url`, `project_id`, `mcp_url`), `DELIVERY_MESSAGE` and `mcp_url_for`; `n8n_api.py` creates and deletes only `httpBearerAuth` credentials (`_CREDENTIAL_TYPE`, `create_bearer_credential`, `check`); `runtime_credentials.py` `deliver` posts `{"token": secret}` and `status_for` answers `credential_type: "httpBearerAuth"` and `node_authentication: "bearerAuth"`; the sweeper calls `api.check()` at start-up (`runtime_credential_sweeper.py:153`). `web/mcp_dispatch.py:562` and `:627` use `mcp_url_for` and `DELIVERY_MESSAGE`. The status tool's description names "the MCP Client node with authentication bearerAuth" (`web/mcp_tools.py:359-368`).
- **The Admin page** has Agent runtimes with `enabled`, `project_id` and `mcp_url` (`admin_config._apply_agent_runtimes` `:871`, `web/routes_admin.py` `ADMIN_PARAMS["set_agent_runtimes"]` `:158`, `settings_window_html.renderRuntimes` `:2552`).
- **The MCP endpoint** serves the handshake revisions statefully (`web/routes_mcp.py:937`, `json_response=False`, so a reply may be JSON or an SSE stream). A POST without `Mcp-Session-Id` is accepted only for the 2026-07-28 revision, which needs a `_meta` envelope (`web/routes_mcp.py:153-158`, `:327-334`); an ordinary session-less `tools/call` gets 400. A scoped token's `tools/list` returns its own tools plus `SCOPED_META_TOOLS` (`web/routes_mcp.py:107-110`, `:461-470`). A refused call returns `isError: true` with a text starting `Request denied` (`gate.py:1700-1760`). `reason` is read from the tool's arguments (`web/mcp_dispatch.py:260`). A result that needs a file from the person's computer carries `_meta["privacyfence.eu/file-bridge"]` (`local_files.META_KEY`).
- **n8n's side** (read from the `@n8n/node-cli@0.56.4`, `@n8n/eslint-plugin-community-nodes@0.40.0` (the CLI itself pins 0.39.0) and `n8n@2.47.7` npm packages, 2026-11-14). The community-node lint rules include `no-credential-reuse` (a node may use only credentials of its own package, so the delivered `httpBearerAuth` cannot back a community node), `credential-test-required` (a credential needs a `test` property or a node `testedBy`), `no-runtime-dependencies` (empty `dependencies`), `no-http-request-with-manual-auth` (no `getCredentials()` and `httpRequest()` in one function), `valid-peer-dependencies` (`n8n-workflow` only), `package-name-convention` (`n8n-nodes-<name>`), and `node-usable-as-tool`. The CLI's CI release runs lint, build and `npm publish` with `NPM_CONFIG_PROVENANCE=true` and `RELEASE_MODE=true`; its local `release` runs release-it, which this repository must not use. `POST /api/v1/credentials` validates `data` against the credential's declared fields with `additionalProperties: false` (`n8n@2.47.7/dist/public-api/v1/handlers/credentials/credentials.utils.js`), so `data` must be exactly the declared fields. For an unknown type, `GET /api/v1/credentials/schema/:type` answers 404 (`controllers/credentials.public.controller.js`).
- **The pattern to copy for the contract test** is `tests/integration/test_shim_mcp_contract.py` (real `WebServer`, built artifact, real client); for a Node package with its own CI, `mcpb/shim/` (`package.json` scripts, `tests.yml:55-98`, `dependency-audit.yml:55-68` and `:200-222`, `dependabot.yml`); for a tag-driven publish, `.github/workflows/publish-pypi.yml` (`dedupe`, `wait_for_build`, channel gate).

## 3. Design

### 3.1 The package `n8n/`

Its own npm package, not part of `mcpb/shim/`. Name `n8n-nodes-privacyfence`, license `Apache-2.0` (the repository's), `repository` `{"type": "git", "url": "git+https://github.com/privacyfence/privacyfence.git", "directory": "n8n"}` (npm provenance checks it against the workflow's repository), `author` `{"name": "PrivacyFence", "email": "info@privacyfence.eu"}` (the lint rule `valid-author` needs both), `homepage` `https://github.com/privacyfence/privacyfence/blob/main/docs/n8n-node.md`, `keywords` `["n8n-community-node-package", "privacyfence", "mcp"]`, `files` `["dist"]`, version `0.0.0-dev` in the repository (CI sets the real one). The `n8n` block is `{"n8nNodesApiVersion": 1, "strict": true, "credentials": ["dist/credentials/PrivacyFenceApi.credentials.js"], "nodes": ["dist/nodes/PrivacyFence/PrivacyFence.node.js"]}`. No `dependencies`; `peerDependencies` `{"n8n-workflow": "*"}`; every `devDependencies` entry pinned to an exact version: `@n8n/node-cli` `0.56.4`, `tsx` `4.23.15`, and `typescript`, `eslint` and `prettier` at the versions the CLI's own template `package.json` pins. Scripts are the CLI template's (`build`, `dev`, `lint`, `lint:fix`, `prepublishOnly: n8n-node prerelease`) plus `typecheck` (`tsc --noEmit`, which covers the package sources, not `test/`) and `test` (`node --import tsx --test test/*.test.mts`); there is no `release` script, because the workflow publishes with `npm publish` (§3.7). Test files are `.mts` and the contract driver is `.mjs` on purpose: the CLI's strict lint (`"strict": true` requires the template ESLint config byte for byte) applies the community rules to `**/*.ts`, and those rules forbid `node:test`, `node:assert`, `console` and `process`.

Layout:

```
n8n/package.json  tsconfig.json  eslint.config.mjs  .gitignore  README.md  LICENSE
n8n/credentials/PrivacyFenceApi.credentials.ts
n8n/nodes/PrivacyFence/PrivacyFence.node.ts  PrivacyFence.node.json  privacyfence.svg  privacyfence.dark.svg
n8n/nodes/PrivacyFence/transport/mcpClient.ts  sse.ts  result.ts
n8n/test/mcpClient.test.mts  sse.test.mts  result.test.mts  node.test.mts  credential.test.mts  contract-driver.mjs
```

### 3.2 The credential `privacyFenceApi`

`PrivacyFenceApi.credentials.ts`: class `PrivacyFenceApi implements ICredentialType`, `name` `'privacyFenceApi'`, `displayName` `'PrivacyFence API'`, `documentationUrl` `'https://github.com/privacyfence/privacyfence/blob/main/docs/n8n-node.md'`, and `icon = { light: 'file:../nodes/PrivacyFence/privacyfence.svg', dark: 'file:../nodes/PrivacyFence/privacyfence.dark.svg' } as const` (the lint rule `cred-class-field-icon-missing` needs it). Two properties, and no others (n8n rejects extra `data` keys):

- `mcpUrl`: `type: 'string'`, `displayName: 'PrivacyFence MCP address'`, `required: true`, `default: ''`, `placeholder: 'https://privacyfence.example.org/mcp'`.
- `token`: `type: 'string'`, `displayName: 'Token'`, `typeOptions: { password: true }`, `required: true`, `default: ''`.

**No `authenticate` block and no `test` property.** With `authenticate`, n8n's HTTP Request node could apply the credential to any URL and read the token back, which is the exposure ADR 0278 already accepts for bearer credentials and which this credential must not widen; the node sets the header itself. The credential is tested by the node's `testedBy` method (§3.4), which the lint rule `credential-test-required` accepts.

### 3.3 The MCP client (`transport/`)

Pure TypeScript, no n8n runtime imports, no dependencies: `fetch`-free and driven through an injected sender so the n8n adapter and the contract test share it.

```ts
export interface HttpRequest { method: 'POST' | 'DELETE'; url: string; headers: Record<string, string>; body?: string; timeoutMs: number }
export interface HttpResponse { status: number; headers: Record<string, string>; body: string }   // header names lower-case
export type HttpSend = (request: HttpRequest) => Promise<HttpResponse>;
export class McpTransportError extends Error {}                                   // always a fixed text, see below
export interface McpTool { name: string; description: string; inputSchema: unknown; annotations?: unknown }
export interface CallToolResult { content: Array<{ type: string; text?: string }>; isError?: boolean; structuredContent?: unknown; _meta?: Record<string, unknown> }
export interface McpSession { listTools(): Promise<McpTool[]>; callTool(name: string, args: Record<string, unknown>): Promise<CallToolResult>; close(): Promise<void> }
export function openSession(send: HttpSend, options: { url: string; token: string; timeoutMs: number; clientVersion: string }): Promise<McpSession>;
```

Protocol, handshake revision only (the architect review found that the daemon routes a session-less ordinary POST to a 400, and the 2026-07-28 path is new and marked temporary in the SDK; the node never sends `2026-07-28`):

1. `POST <url>` with `initialize` (`protocolVersion: "2025-11-25"`, `capabilities: {}`, `clientInfo: {name: "n8n-nodes-privacyfence", version: clientVersion}`), headers `Authorization: Bearer <token>`, `Content-Type: application/json`, `Accept: application/json, text/event-stream`. The answer's `Mcp-Session-Id` header is required, and the answered `protocolVersion` must be one of `2025-11-25`, `2025-06-18`, `2025-03-26` (else an error). Phase p2 first confirms these three are in the installed `mcp` package's `HANDSHAKE_PROTOCOL_VERSIONS` and stops if `2025-11-25` is not.
2. `POST notifications/initialized` (expects 202), then every request carries `Mcp-Session-Id` and `MCP-Protocol-Version: <answered version>`.
3. `tools/list` follows `nextCursor` for at most 10 pages. `tools/call` takes `{name, arguments}`; JSON-RPC ids are integers from 1.
4. `close()` sends `DELETE <url>` with the session headers and never throws.
5. A reply is parsed by content type: `application/json` as one JSON-RPC message; `text/event-stream` with `sse.ts` (events separated by blank lines; the data of an event is its `data:` lines joined with `\n`; messages that are notifications or whose `id` differs are skipped; the first message whose `id` matches is the answer). A body over 10,000,000 characters is an error.

**Fixed error texts** (`<host>` is the URL's host; nothing else is ever put into an error, and no error keeps the cause):

- `PrivacyFence at <host> could not be reached.` (the sender threw, or timed out)
- `PrivacyFence at <host> answered HTTP <status>.` (any other non-success status)
- `PrivacyFence at <host> refused the token (HTTP <status>); it may be revoked or expired.` (401, 403)
- `PrivacyFence at <host> redirected the request; check the PrivacyFence MCP address in the credential.` (3xx)
- `PrivacyFence at <host> did not speak MCP as expected.` (a missing session id, an unsupported version, an unparseable body, no answer in the stream)
- `PrivacyFence at <host> answered an MCP error: <message>` where `<message>` is the JSON-RPC error's `message`, control characters removed, cut to 300 characters.

### 3.4 The node

`PrivacyFence.node.ts`, class `PrivacyFence implements INodeType`. Description: `displayName 'PrivacyFence'`, `name 'privacyFence'`, `group ['transform']`, `version 1`, `subtitle` `={{$parameter["operation"] === "listTools" ? "List tools" : $parameter["tool"]}}`, `description 'Call a tool your PrivacyFence server lets this workflow use'`, `defaults {name: 'PrivacyFence'}`, `usableAsTool: true`, main input and output, `credentials: [{name: 'privacyFenceApi', required: true, testedBy: 'privacyFenceApiTest'}]`, icons `file:privacyfence.svg` and `file:privacyfence.dark.svg` (§3.8).

Parameters:

| name | type | notes |
|---|---|---|
| `operation` | options | `callTool` ("Call a tool", default) and `listTools` ("List tools") |
| `tool` | options, `loadOptionsMethod: 'getTools'` | shown for `callTool`; required; the option's `description` is the tool's own description cut to 200 characters |
| `reason` | string | shown for `callTool`; required; default `={{ "n8n workflow: " + $workflow.name }}`; description "Why this call is made. PrivacyFence records it in its audit log." |
| `arguments` | json | shown for `callTool`; default `{}`; description "The tool's arguments as a JSON object, without reason." |
| `options` | collection | `timeout` (number, ms, default 60000, minimum 1000) |

Behaviour:

- **Credentials and sending.** `readConnection(this)` (one function, only `getCredentials`) returns `{url, token}`; the sender is built by `makeSend(this)` (another function, only `this.helpers.httpRequest`). The sender calls `httpRequest({method, url, headers, body, timeout, returnFullResponse: true, ignoreHttpStatusErrors: true, disableFollowRedirect: true, encoding: 'text', json: false})` and maps the answer to `HttpResponse`. The call sits in a `try` that only sets a flag; after the `try` the sender throws `new Error('unreachable')` when the flag is set, which the client turns into the fixed text (no throw inside a `catch`). The lint rule `no-http-request-with-manual-auth` matches `this.getCredentials` and `this.helpers.httpRequest` in one function scope, so the two functions never trigger it; if it reports anyway, the worker stops with status=blocked.
- **One session per `execute()`**, opened lazily at the first item and closed in `finally`. `loadOptions.getTools` and `testedBy` open their own and close it.
- **`getTools`** lists the session's tools, drops every name starting `privacyfence_` except `privacyfence_status`, and sorts by name; `listTools` returns the same list as items `{name, description, readOnly}` (every item of every operation carries `pairedItem`: `{item: itemIndex}` for a call, `{item: 0}` for `listTools`; the lint rule `missing-paired-item` needs it) where `readOnly` is `annotations.readOnlyHint === true`.
- **`callTool`** per item: parameters read per item; `arguments` must parse to a JSON object (else `NodeOperationError` `The arguments must be a JSON object.`); the call sends `{...arguments, reason}` (the field wins over a `reason` key inside the JSON). The result goes through `result.ts`:
  1. `_meta["privacyfence.eu/file-bridge"]` present: error `This tool needs a file from the person's computer, which n8n cannot provide.`
  2. `isError` true: error whose message is the text blocks joined with a newline, control characters removed, cut to 1000 characters.
  3. `structuredContent` a plain object: that object.
  4. Otherwise the first text block parsed as JSON: an object is the item; an array or scalar becomes `{result: <value>}`; text that is not JSON becomes `{text: <text>}`. Blocks that are not text add `nonTextBlocks: <count>` to the item.
- **Errors**: `McpTransportError` and the errors of `result.ts` become `NodeOperationError(this.getNode(), <text>, {itemIndex})`; with `continueOnFail()` the item is `{json: {error: <text>}, pairedItem: {item}}`. Any other caught error becomes the text `The PrivacyFence node failed unexpectedly.`, built inside the `catch` as a `NodeOperationError` (the lint rule `require-node-api-error` allows only `NodeApiError` and `NodeOperationError` inside a `catch` and forbids rethrowing the caught value). No other error text is ever built: the node never passes a caught error, a request, a header or a credential value to `NodeOperationError` or `NodeApiError`.
- **`privacyFenceApiTest`** (`methods.credentialTest`): opens a session with the credential's data, lists tools, closes it, and returns `{status: 'OK', message: 'Connected to PrivacyFence. <n> tools are available to this token.'}` or `{status: 'Error', message: <fixed text>}`. It uses whichever of `helpers.httpRequest` or `helpers.request` the type `ICredentialTestFunctions` declares, with the same semantics (full response, no status errors, no redirects, text body).
- The node has no URL parameter and no way to override the credential's address.

### 3.5 PrivacyFence side

- `agent_runtimes.py`: `CREDENTIAL_TYPES = ("httpBearerAuth", "privacyFenceApi")`, `DEFAULT_CREDENTIAL_TYPE = "httpBearerAuth"`, `N8N_NODE_PACKAGE = "n8n-nodes-privacyfence"`, `N8N_NODE_TYPE = "n8n-nodes-privacyfence.privacyFence"`. `N8nRuntimeConfig` gains `credential_type: str = DEFAULT_CREDENTIAL_TYPE` as its last field; `from_org_config` reads `n8n.get("credential_type", "httpBearerAuth")`, refuses another value with `org_mode.ConfigurationError("agent_runtimes.n8n.credential_type must be httpBearerAuth or privacyFenceApi.")`, and refuses `privacyFenceApi` with an empty `mcp_url`: `"agent_runtimes.n8n.mcp_url is required for the privacyFenceApi credential type: the credential keeps this server's address."`. `DELIVERY_MESSAGE` stays (the bearer text); a function `delivery_message(credential_type: str) -> str` returns it for `httpBearerAuth` and, for `privacyFenceApi`:

  > Give the person this link. They review the token in PrivacyFence Settings, can narrow it, and confirm it themselves. When they do, PrivacyFence creates an n8n credential of type privacyFenceApi holding the token and this server's address; call privacyfence_runtime_credential_status with this proposal_id to get the credential's id for the PrivacyFence node (community node n8n-nodes-privacyfence, which must be installed in that n8n). Anyone who can edit workflows in that n8n can use the credential, so keep its tools and rules narrow. Calls that would need an approval are refused at once. Tools in uncovered_tools have no rule, so the token's calls to them will be denied.

  `web/mcp_dispatch.py:627` uses `delivery_message(runtime_config.credential_type)`.
- `n8n_api.py`: `create_bearer_credential` is replaced by `create_credential(*, credential_id, name, credential_type, data, project_id)` (same request, same checks, but the answered `type` must equal `credential_type`; it logs the id only, never `data`) and `check()` by `check_type(credential_type)` (`GET /api/v1/credentials/schema/<type>`, `ValueError` unless the type matches `^[A-Za-z][A-Za-z0-9]{0,63}$`). Error texts are unchanged.
- `runtime_credentials.py`: `RuntimeCredential` gains `credential_type: str = "httpBearerAuth"` as its last field; `_from_json` reads it with `raw.get("credential_type", "httpBearerAuth")` (records written before this change have no such key, and the file version stays 1). A function `credential_data(credential_type, mcp_url, secret) -> dict` returns `{"token": secret}` for `httpBearerAuth` and `{"mcpUrl": mcp_url, "token": secret}` for `privacyFenceApi`. `deliver` first, for `privacyFenceApi` only, calls `api.check_type("privacyFenceApi")` before adding a record or minting a token: an `N8nApiClientError` whose status is 404 (n8n does not know the type) answers `DeliveryOutcome(None, None, NODE_NOT_INSTALLED, 503)`; every other error (an unreachable n8n, a 401 from a revoked API key, a 3xx, a 5xx) answers `DELIVERY_FAILED` with 502. Neither leaves a record or a token, and both write the audit entry `runtime_credential_delivery_failed` through `record_event` with the unsaved record and `_failed_summary`. `NODE_NOT_INSTALLED = "n8n does not know the PrivacyFence credential type. Ask the n8n administrator to install the community node n8n-nodes-privacyfence, then try again."` It then calls `create_credential` with `credential_data(...)`. The record carries `credential_type=config.credential_type`. `status_for` answers from the **record's** type: unchanged for `httpBearerAuth`; for `privacyFenceApi`:

  ```json
  {"state": "delivered", "runtime": "n8n", "credential_id": "...", "credential_name": "...",
   "credential_type": "privacyFenceApi", "node": "n8n-nodes-privacyfence.privacyFence",
   "mcp_url": "...", "token_expires_at": "<ISO 8601>"}
  ```

- `runtime_credential_sweeper.py`: the start-up check is `api.check_type(config.credential_type)`.
- `web/mcp_tools.py`: the status tool's description becomes, verbatim (under the 1024-character limit): "Report whether a proposed token was delivered to n8n, and the n8n credential to use. Call it with the proposal_id privacyfence_propose_scoped_token returned for a proposal with deliver_to, after the person has confirmed it. Returns state 'delivered' with credential_id, credential_name, credential_type, mcp_url and token_expires_at, plus node (the PrivacyFence node to put the credential on) for credential_type privacyFenceApi, or node_authentication bearerAuth for httpBearerAuth, which goes on an MCP Client node whose endpoint is mcp_url; 'pending' while the person has not decided; or 'not_delivered' for anything else. It never returns the token. A scoped token cannot call it." `docs/tools-reference.md` is regenerated (`scripts/generate_tools_reference.py`).
- **Admin**: `set_agent_runtimes` takes a fourth parameter `credential_type` (str, required); `_apply_agent_runtimes` keeps `n8n` fields not given and validates the candidate through `N8nRuntimeConfig.from_org_config`; the default section is `{"enabled": false, "project_id": "", "mcp_url": "", "credential_type": "httpBearerAuth"}`. `renderRuntimes` gets a select **Credential type in n8n** (fields `credential_type`; options "MCP Client node (n8n's built-in bearer credential)" = `httpBearerAuth`, "PrivacyFence node (community node n8n-nodes-privacyfence)" = `privacyFenceApi`) with the hint "The PrivacyFence node needs the community node n8n-nodes-privacyfence installed in n8n and a PrivacyFence MCP address above. The setting is server-wide: with it, an AI Agent in n8n uses one PrivacyFence node per call instead of the MCP Client Tool." Changing it needs the restart the other runtime fields need.
- The review page, the confirmation dialog and `security-and-compliance.md` keep the sentence that anyone who can edit workflows in that n8n can use and read the token; it holds for both types.

### 3.6 What the new credential exposes

A `privacyFenceApi` credential holds the token **and the address it is sent to**. Anyone who can edit that credential in n8n can change the address and receive the token at their own host; for `httpBearerAuth` the same person could attach the token to an HTTP Request node. The reach is the same (whoever has workflow edit access), the ADR says so, and the node adds no URL override. The exposure is documented, not reduced, until n8n's own credential domain allow-list is researched (open since ADR 0278).

### 3.7 CI, definition of done and publishing

- `tests.yml`: a job `n8n-node` (ubuntu, Node 22, `npm ci`, `npm run lint`, `npm run typecheck`, `npm run build`, `npm test` in `n8n/`, then `npm pack` and upload of the tarball as the artifact `n8n-nodes-privacyfence-tgz`); the `test` job installs `n8n/` dependencies before pytest so the contract test runs; every job that excludes `test_shim_mcp_contract.py` also excludes `test_n8n_node_contract.py`.
- `dependency-audit.yml` audits `n8n/package-lock.json` (path filters and the npm-audit job), `dependabot.yml` gets an npm entry for `/n8n` grouped as `n8n-node-dependencies`.
- A `dod_conditional` row in `.tld/toolkit.yaml` and the matching bullet in `docs/coding-and-testing-guidelines.md` §2.7 for `n8n/**`: the five npm commands in `n8n/` and `pytest tests/integration/test_n8n_node_contract.py -v`. The file says its `verify.*` mirrors §2.7: both change together.
- `scripts/update_branch_protection.py` lists the required checks (`REQUIRED_STATUS_CHECKS`; its test `tests/unit/test_update_branch_protection.py` pins no list and needs no change); the `n8n-node` job's name is added (applying the script to GitHub is a manual step, `ma3`).
- **`.github/workflows/publish-n8n-node.yml`**, modelled on `publish-pypi.yml`: triggers `push: tags: ['v*']` and `workflow_dispatch` (input `dry_run`, default `true`; dispatched on a tag ref like `publish-pypi.yml`, whose version step the new one copies: the version is derived from `GITHUB_REF_NAME` and checked with `scripts/r2_release.py check-tag`); jobs `dedupe` (copied, with its workflow file name and warning text changed to `publish-n8n-node.yml`, and the callers list in `scripts/release_run_guard.py:11` extended), `wait_for_build` (copied: blocks on `build.yml` for the same commit), and `publish`, which needs both, runs only when `scripts/r2_release.py channel <version>` prints `stable`, runs in the GitHub environment `npm` with `permissions: {id-token: write, contents: read}`, and: checks out, sets up Node 22 with `registry-url: https://registry.npmjs.org`, installs npm `11.21.2` (`npm install -g npm@11.21.2`, the current 11.x; trusted publishing needs a recent npm, §6), `npm ci` in `n8n/`, derives `VERSION` from the tag (`v7.1.0` becomes `7.1.0`), `npm version "$VERSION" --no-git-tag-version`, `npm run lint`, `npm run build`, skips the rest only when `[ "$(npm view n8n-nodes-privacyfence@"$VERSION" version 2>/dev/null)" = "$VERSION" ]` (a rerun; the comparison is of the output, because `npm view` can exit 0 with empty output), and otherwise publishes with `RELEASE_MODE=true npm publish --provenance --access public` (`--dry-run` when `dry_run` is true). As in the node CLI's own `publish.yml` template, a step writes the registry token with `npm config set //registry.npmjs.org/:_authToken "$NPM_TOKEN"` only when `secrets.NPM_TOKEN` is set; unset, npm uses trusted publishing. It does **not** call `n8n-node release` (release-it would bump, commit and tag).
- Each stable PrivacyFence tag publishes a new package version, also when `n8n/` did not change (§6).

### 3.8 Icon

`privacyfence.svg` and `privacyfence.dark.svg` are the same 60x60 shield, one stroke colour each (`#1f2937` and `#f3f4f6`):

```svg
<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60" viewBox="0 0 60 60" fill="none" stroke="#1f2937" stroke-width="4" stroke-linejoin="round"><path d="M30 6 L50 14 V30 C50 42 41 51 30 55 C19 51 10 42 10 30 V14 Z"/><path d="M21 30 L28 37 L40 23" stroke-linecap="round"/></svg>
```

### 3.9 Rejected alternatives

These become the ADR text.

- **Keeping only the MCP Client node and documenting it.** It leaves the endpoint, the tool list, `reason`, the content arrays and the refusal text to every workflow author, and cannot show only what a token may call.
- **Depending on `@modelcontextprotocol/sdk`.** n8n's verification forbids runtime dependencies, and the SDK would be bundled code to audit.
- **The 2026-07-28 single-exchange revision, or trying it and falling back.** It needs the `_meta` envelope and `resultType` handling, the daemon's routing for it is new and marked temporary in the SDK, and a fallback doubles the surface and the tests.
- **An `authenticate` block, or `httpRequestWithAuthentication`.** It would let the HTTP Request node apply the credential anywhere and read the token back.
- **A separate "PrivacyFence Tools" sub-node for AI Agents.** Declined by the owner for this change; `usableAsTool` covers an agent calling one tool.
- **Always delivering `privacyFenceApi`.** It would make the community node required in every n8n and drop the MCP Client path; the default stays `httpBearerAuth`.
- **Publishing with the node CLI's `release`.** It runs release-it (bump, commit, tag, push), which fights the tag-driven versions.
- **A separate repository.** The node's tests need the real daemon, and its version must follow PrivacyFence's.
- **Checking the node at propose time.** It would make an MCP call depend on n8n; the check is made at delivery, where failure is already fail-closed.

## 4. ADRs

- **0283** The n8n node ships from this repository as the npm package `n8n-nodes-privacyfence`, versioned with PrivacyFence and published by GitHub Actions with provenance on stable tags (distribution path).
- **0284** The credential type of a delivery is an administrator's choice, and delivery of the new type is checked against n8n before a token is minted (trust boundary; amends 0278 and 0280).
- **0285** The `privacyFenceApi` credential holds the address and the token and has no `authenticate` block (security; the exposure of §3.6).
- **0286** The node carries its own handshake-only MCP client with no dependencies (rejects the SDK and the 2026-07-28 revision).
- **0287** The node never follows a redirect and never builds an error or an item from a caught error, a request or a credential value (security).

## 5. Manual steps

Everything the phases can do is in them (the packaged tarball is built by CI, the contract test runs the real daemon). What is left needs an npm account and a real n8n.

### Before implementation

None.

### After implementation

**ma1-npm-publisher: let GitHub publish the package to npm.** About 15 minutes, before you cut the first stable release that includes this change (a release cut earlier makes only the `publish-n8n-node.yml` job fail).

1. Sign in to npm at https://www.npmjs.com/login with the account that will own `n8n-nodes-privacyfence`. You see your profile.
2. Open your account menu on npmjs.com (top right), choose **Access Tokens**, then **Generate New Token → Granular Access Token**. Name `privacyfence-n8n-publish`, expiry 30 days, **Packages and scopes: Read and write**, all packages (the package does not exist yet). You see the token once.
3. In GitHub open https://github.com/privacyfence/privacyfence/settings/environments, create the environment `npm` (no reviewers needed), and under **Environment secrets** add `NPM_TOKEN` with the token from step 2. You see the secret listed.

The steps that follow the first release (checking the provenance badge, switching to trusted publishing, deleting the token) are the first-release procedure in [`docs/releasing.md`](https://github.com/privacyfence/privacyfence/blob/feature/n8n-privacyfence-node/docs/releasing.md) (written in p7).

Report back: `ma1: pass` once the `npm` environment lists the secret `NPM_TOKEN`, or `ma1: fail <what npm or GitHub said>`.

**ma2-real-n8n: run the node in a real n8n (2.40.0 or later, self-hosted).** About 30 minutes, with the PrivacyFence server in organization mode and `PRIVACYFENCE_N8N_URL` and key already set as the 1090 guide says.

1. Download the artifact `n8n-nodes-privacyfence-tgz` from the pull request's latest `tests.yml` run (Actions tab of https://github.com/privacyfence/privacyfence/actions/workflows/tests.yml) and unzip it.
2. On the n8n host run `cd ~/.n8n/nodes && npm install /path/to/n8n-nodes-privacyfence-*.tgz`, then restart n8n. In the editor's node picker you see **PrivacyFence**. (This is n8n's manual community-node install; it was not re-checked here.)
3. In PrivacyFence **Admin → Agent runtimes**, set **Credential type in n8n** to the PrivacyFence node, set **PrivacyFence MCP address for n8n** to your server's `/mcp` address, save and restart as asked.
4. Ask Claude (connected to PrivacyFence and the n8n MCP) to build a workflow that calls one auto-approved tool through the PrivacyFence node, with `deliver_to: {"runtime": "n8n"}`. Confirm the token in PrivacyFence Settings. In n8n you see a credential `PrivacyFence <label>` of type PrivacyFence API; open it and press **Test**: you see "Connected to PrivacyFence".
5. Run the workflow: the tool list in the node shows only the token's tools and the item is the tool's JSON. Change the node to a tool the token does not cover: the run stops with `Request denied`.
6. Revoke the token in PrivacyFence Settings; within five minutes the credential is gone from n8n.
7. Switch the setting back to MCP Client and repeat step 4: delivery still creates a bearer credential.

Report back: one pass/fail line per step 4, 5, 6 and 7, with what you saw on a failure.

**ma3-branch-protection: require the new check.** About 5 minutes, after this pull request merges. Run `python3 scripts/update_branch_protection.py` as the repository admin as that script's docstring says, or add the status check `n8n-node` in https://github.com/privacyfence/privacyfence/settings/rules. Report back: `ma3: pass` once a new pull request lists `n8n-node` as required.

## 6. Risks and open questions

- **The strict lint may reject a shape** (a display name that must be Title Case, for instance: p1 may fix the case of display names and nothing else). Any other rejection on the first `npm run lint` is a stop with `status=blocked` and the rule's text in the report.
- **`ICredentialTestFunctions` may offer only `helpers.request`.** §3.4 says to use whichever the type declares; if neither takes the options listed, the worker stops.
- **n8n's 404 for an unknown type is read from its source, not from a live n8n.** Only a 404 from `check_type` means "install the node"; every other failure is a generic delivery failure, and both fail closed (no token is minted). If a real n8n accepts the `privacyFenceApi` type but refuses the `data` (ma2 step 4), the delivery revokes the token and says so; the field names in `n8n/credentials/PrivacyFenceApi.credentials.ts` and `credential_data` are pinned together by a test (p6).
- **npm trusted publishing** is not verified here: whether it can be set before a first publish, and which npm version it needs (the workflow installs the current 11.x). ma1 uses a token for the first version, which works either way.
- **The package license.** n8n's verification may require MIT; the package is Apache-2.0 like the repository. If submission shows a requirement, a separate license for `n8n/` is the owner's decision then.
- **Every stable tag publishes a new package version**, also when `n8n/` is unchanged, and a verified listing may be reviewed again. Accepted for now (owner decision 3).
- **In AI Agent tool mode** the agent sees the node's own parameters, not the PrivacyFence tool's input schema; `arguments` is a JSON object the agent writes. ma2 does not cover it; if it is awkward in practice, a later change adds the sub-node the owner declined for now.
- **The daemon's session count.** Each execution opens and deletes one session; a leak would show as growing sessions in repeated executions. The contract test checks that a closed session is no longer accepted.
- A worker that sees the daemon refuse `2025-11-25` stops: the whole transport design rests on §3.3.

## Implementation manifest

```yaml
plan_slug: n8n-privacyfence-node
feature_branch: feature/n8n-privacyfence-node
tracking_issue: 1095
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-npm-publisher
    title: Create the npm token and the npm environment for the first publish
    why: Only the owner's npm account can create the package's first token; without the secret the first stable release's publish job fails.
  - id: ma2-real-n8n
    title: Run the node and the delivery against a real n8n
    why: The tests fake n8n; only a real n8n proves the credential type is accepted, the node loads, the credential test passes and revocation deletes the credential.
  - id: ma3-branch-protection
    title: Require the n8n-node check on main
    why: Branch protection is a GitHub setting an admin applies; the pull request only names the check.
verify_after_merge:
  - npm ci --prefix n8n && npm test --prefix n8n && npm run lint --prefix n8n
final_checks:
  - "`tld plan deviations check --file docs/1095/deviations.md --plan docs/1095/plan.md --all` exits 0"
  - "each of ADRs 0283 to 0287 exists in docs/adr, is Accepted, and has a row in docs/adr/README.md"
  - "CHANGELOG.md has entries under `## [Unreleased]` for this change and no new version heading"
phases:
  - id: p1-package
    title: The n8n/ package skeleton, the credential and the node description
    depends_on: []
    complexity: M
    touches:
      - n8n/package.json
      - n8n/package-lock.json
      - n8n/tsconfig.json
      - n8n/eslint.config.mjs
      - n8n/.gitignore
      - n8n/credentials/PrivacyFenceApi.credentials.ts
      - n8n/nodes/PrivacyFence/PrivacyFence.node.ts
      - n8n/nodes/PrivacyFence/PrivacyFence.node.json
      - n8n/nodes/PrivacyFence/privacyfence.svg
      - n8n/nodes/PrivacyFence/privacyfence.dark.svg
      - n8n/test/credential.test.mts
    brief: |
      Build the package skeleton exactly as plan §3.1, §3.2, §3.4 (description and parameters only) and §3.8 say.
      1. Create `n8n/package.json` as §3.1 lists it. Start from the CLI's own template: run `npm install --save-dev --save-exact @n8n/node-cli@0.56.4 tsx@4.23.15` in `n8n/` after writing the file, then read `n8n/node_modules/@n8n/node-cli/dist/template/templates/programmatic/example/template/package.json` and copy its `scripts`, its `tsconfig.json` and the exact versions it pins for `typescript`, `eslint` and `prettier` (install those with `--save-exact`); copy `.../templates/shared/default/eslint.config.mjs` to `n8n/eslint.config.mjs` byte for byte (strict mode refuses any other config). Also set `author`, `homepage`, `repository` as §3.1 says. Every `devDependencies` version is an exact pin (no `^`, no `~`). `dependencies` is absent. `peerDependencies` is `{"n8n-workflow": "*"}`. Add scripts `typecheck` and `test` as §3.1 says, and no `release` script.
      2. Write `n8n/credentials/PrivacyFenceApi.credentials.ts` exactly as §3.2: the `icon` field, two properties `mcpUrl` and `token`, no `authenticate`, no `test`.
      3. Write `n8n/nodes/PrivacyFence/PrivacyFence.node.ts` with the full `description` of §3.4 (display name, subtitle, `usableAsTool`, credentials with `testedBy: 'privacyFenceApiTest'`, every parameter of the table) and `methods: { loadOptions: { getTools }, credentialTest: { privacyFenceApiTest } }` whose bodies throw `new Error('not implemented')`, and an `execute` that throws the same; p3 fills them in. Write `PrivacyFence.node.json` (the codex file the template shows: `node`, `nodeVersion`, `codexVersion`, `categories: ["Development"]`, `resources` with the credential and primary documentation links to `https://github.com/privacyfence/privacyfence/blob/main/docs/n8n-node.md`). Write the two SVGs from §3.8 (the dark one with stroke `#f3f4f6`).
      4. Write `n8n/.gitignore` with `node_modules`, `dist`, `*.tgz`.
      5. Write `n8n/test/credential.test.mts` (node:test; `.mts` so the community lint rules do not apply to it): the credential has exactly the property names `mcpUrl` and `token`, `token` has `typeOptions.password`, it has an `icon` with `light` and `dark`, and it has no `authenticate` and no `test` key.
      6. Run, in `n8n/`: `npm install`, `npm run lint`, `npm run typecheck`, `npm run build`, `npm test`. Commit `package-lock.json`.
      7. If `npm run lint` reports only display-name capitalisation (Title Case) findings from `eslint-plugin-n8n-nodes-base`, fix the case of those display names and nothing else. Any other lint rule that rejects the shape: stop with status=blocked and quote the rule. `n8n-node lint` needs no README or LICENSE (p3 adds them).
    acceptance:
      - "In n8n/: `npm run lint` exits 0"
      - "In n8n/: `npm run build` exits 0 and `n8n/dist/credentials/PrivacyFenceApi.credentials.js` and `n8n/dist/nodes/PrivacyFence/PrivacyFence.node.js` exist"
      - "In n8n/: `npm test` passes"
      - "`grep -c '\"dependencies\"' n8n/package.json` prints 0"
      - "`node -e \"process.exit(Object.values(require('./n8n/package.json').devDependencies).some(v=>/[\\^~*]/.test(v))?1:0)\"` exits 0"
      - "`grep -c '\"release\"' n8n/package.json` prints 0"
  - id: p2-mcp-client
    title: The hand-written MCP handshake client and the result mapping
    depends_on: [p1-package]
    complexity: M
    touches:
      - n8n/nodes/PrivacyFence/transport/mcpClient.ts
      - n8n/nodes/PrivacyFence/transport/sse.ts
      - n8n/nodes/PrivacyFence/transport/result.ts
      - n8n/test/mcpClient.test.mts
      - n8n/test/sse.test.mts
      - n8n/test/result.test.mts
    brief: |
      Implement plan §3.3 and the output rules of §3.4 as pure TypeScript with no n8n imports and no dependencies.
      1. Confirm first: run `.venv/bin/python -c "from mcp.server._streamable_http_modern import *; import mcp.server.streamable_http_manager as m; print(m.HANDSHAKE_PROTOCOL_VERSIONS)"` (or grep the installed `mcp` package for `HANDSHAKE_PROTOCOL_VERSIONS`). If `2025-11-25` is not in it, stop with status=blocked.
      2. `transport/sse.ts`: `export function parseSse(body: string): unknown[]` returns the JSON of every event's joined `data:` lines, skipping events without data and comment lines (`:`), tolerating `\r\n`; invalid JSON in an event is skipped.
      3. `transport/mcpClient.ts`: the interfaces and `openSession` of §3.3, the three supported versions as a constant, the fixed error texts verbatim, `McpTransportError`. The token appears only in the `Authorization` header built inside one function; no error message, no thrown object and no log line contains it or the request. Follow `nextCursor` for at most 10 pages. `close()` swallows every error. A 3xx answer is the redirect text; a thrown sender error is the unreachable text without the cause (`throw new McpTransportError(text)` from outside the `catch`, as `n8n_api.py` does with `unreachable`).
      4. `transport/result.ts`: `export function toItem(result: CallToolResult): { json: Record<string, unknown> } | { error: string }` implementing the four numbered rules of §3.4 (file-bridge key `privacyfence.eu/file-bridge`, `isError`, `structuredContent`, text parsing with the `result`/`text`/`nonTextBlocks` rules, 1000-character cut, control characters removed).
      5. Tests (files `*.test.mts`, with a fake `HttpSend`): `mcpClient.test.mts` covers the full handshake order (initialize, initialized, calls, DELETE), the headers on every request, an SSE answer with a notification before the answer, version negotiation (an unsupported version errors), a missing session id, 401 and 403, 3xx, a sender that throws, a JSON-RPC error, pagination to the 10-page cap, a body over the cap, and a test that the token string and the request URL path appear in no thrown error message (run every failure case and assert `!message.includes(token)`). `sse.test.mts` and `result.test.mts` cover each rule.
      6. Run `npm run lint`, `npm run typecheck`, `npm test` in `n8n/`, and the coverage command of the last acceptance line.
    acceptance:
      - "In n8n/: `npm test` passes"
      - "In n8n/: `npm run lint` and `npm run typecheck` exit 0"
      - "In n8n/: `node --import tsx --test --experimental-test-coverage --test-coverage-lines=95 --test-coverage-include='nodes/PrivacyFence/transport/**' test/*.test.mts` exits 0"
      - "`grep -rn 'getCredentials\\|NodeOperationError' n8n/nodes/PrivacyFence/transport` matches nothing"
  - id: p3-node-logic
    title: The node's execute, tool picker and credential test
    depends_on: [p2-mcp-client]
    complexity: M
    touches:
      - n8n/nodes/PrivacyFence/PrivacyFence.node.ts
      - n8n/test/node.test.mts
      - n8n/README.md
      - n8n/LICENSE
    brief: |
      Fill in the node as plan §3.4 specifies; the parameters and description from p1 stay as they are.
      1. In `PrivacyFence.node.ts` write `readConnection(context)` (only `getCredentials('privacyFenceApi')`; returns `{url, token}`; an empty `url` or `token` is `NodeOperationError` `The PrivacyFence credential needs an address and a token.`) and, as a separate function, `makeSend(context)` (only `context.helpers.httpRequest`, with the options of §3.4, mapping the full response to `HttpResponse`; the `httpRequest` call sits in a `try` that only sets a flag, and after the `try` the function throws `new Error('unreachable')` when the flag is set; never throw inside a `catch`). If `no-http-request-with-manual-auth` reports after this split, stop with status=blocked.
      2. `methods.loadOptions.getTools`: open a session with a 60000 ms timeout, list the tools, drop names starting `privacyfence_` except `privacyfence_status`, sort by name, return `{name, value: name, description: <description cut to 200 characters>}`, close in `finally`. A transport error is rethrown as `NodeOperationError(this.getNode(), <its fixed text>)`.
      3. `execute`: one lazily opened session closed in `finally`; per item read `operation`, `tool`, `reason`, `arguments`, `options.timeout` with `getNodeParameter(name, itemIndex, default)`; `listTools` returns one item per tool `{name, description, readOnly}` (same filter and order as `getTools`); `callTool` validates `arguments` (an object after `JSON.parse` of a string, else `The arguments must be a JSON object.`), sends `{...arguments, reason}`, maps through `toItem`, and builds errors and error items exactly as §3.4 says (every returned item has `pairedItem`: `{item: itemIndex}`, and `{item: 0}` for `listTools`; `continueOnFail()` gives `{json: {error}, pairedItem}`; any caught error that is not a `McpTransportError` or a `toItem` error becomes `NodeOperationError` with the text `The PrivacyFence node failed unexpectedly.`, built inside the `catch`). Never pass a caught error, request or credential value to `NodeOperationError` or `NodeApiError`: build the text first, then the error.
      4. `methods.credentialTest.privacyFenceApiTest`: as §3.4; use the helper the type `ICredentialTestFunctions` declares (read `n8n/node_modules/n8n-workflow/dist/**/*.d.ts` for `ICredentialTestFunctions`). If it declares neither `httpRequest` nor `request`, stop with status=blocked.
      5. Write `n8n/README.md` (what the node is, install from npm, the credential, a three-line usage example, a link to `https://github.com/privacyfence/privacyfence/blob/main/docs/n8n-node.md`) and copy the repository's `LICENSE` to `n8n/LICENSE`.
      6. `n8n/test/node.test.mts` (node:test; it needs `n8n-workflow` installed, which the peer dependency provides) with a fake execution context object: a call returns the structured item; an `isError` result throws with the daemon's text; with `continueOnFail` it returns an error item; invalid `arguments` JSON is refused; the picker hides `privacyfence_check_policy` and keeps `privacyfence_status`; the session is closed when the call throws; the credential test returns OK and Error shapes; and across every failure case the credential's token string is in no thrown message, no error item and no `getTools` failure.
      7. Run `npm run lint`, `npm run typecheck`, `npm run build`, `npm test` in `n8n/`.
    acceptance:
      - "In n8n/: `npm test` passes"
      - "In n8n/: `npm run lint`, `npm run typecheck` and `npm run build` exit 0"
      - "`ls n8n/README.md n8n/LICENSE` lists both files"
      - "`grep -c \"not implemented\" n8n/nodes/PrivacyFence/PrivacyFence.node.ts` prints 0"
      - "`grep -n 'httpRequestWithAuthentication' n8n/nodes/PrivacyFence/PrivacyFence.node.ts` matches nothing"
  - id: p4-delivery-core
    title: Credential type through delivery, records, status and texts
    depends_on: [p1-package]
    complexity: M
    touches:
      - src/privacyfence/agent_runtimes.py
      - src/privacyfence/n8n_api.py
      - src/privacyfence/runtime_credentials.py
      - src/privacyfence/runtime_credential_sweeper.py
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/web/mcp_tools.py
      - docs/tools-reference.md
      - tests/unit/test_agent_runtimes.py
      - tests/unit/test_n8n_api.py
      - tests/unit/test_runtime_credentials.py
      - tests/unit/test_runtime_credential_sweeper.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/web/test_routes_org_settings.py
    brief: |
      Implement plan §3.5 except the Admin page (p5). Write each failing test first.
      1. `agent_runtimes.py`: add `CREDENTIAL_TYPES`, `DEFAULT_CREDENTIAL_TYPE`, `N8N_NODE_PACKAGE`, `N8N_NODE_TYPE`; add the field `credential_type` to `N8nRuntimeConfig` with its default, read and validated in `from_org_config` with the two exact `ConfigurationError` texts of §3.5; add `delivery_message(credential_type)` returning `DELIVERY_MESSAGE` for `httpBearerAuth` and the literal privacyFenceApi text of §3.5 otherwise (the constant for it is `DELIVERY_MESSAGE_PRIVACYFENCE_NODE`).
      2. `n8n_api.py`: replace `create_bearer_credential` with `create_credential(*, credential_id, name, credential_type, data, project_id)` (payload `type` and the success check use `credential_type`; the cleanup of a wrongly-ided answer stays); replace `check` with `check_type(credential_type)` with the `ValueError` rule; remove `_CREDENTIAL_TYPE`. Update the module docstring's first sentence to "creates and deletes credentials of a given type". Texts are unchanged.
      3. `runtime_credentials.py`: add `credential_type: str = "httpBearerAuth"` as the last `RuntimeCredential` field; `_from_json` uses `raw.get("credential_type", "httpBearerAuth")`; add `credential_data` and `NODE_NOT_INSTALLED` (exact text in §3.5); in `deliver`, for `privacyFenceApi` only, call `api.check_type` before `records.add`, mapping an `N8nApiClientError` with status 404 to `DeliveryOutcome(None, None, NODE_NOT_INSTALLED, 503)` and every other error to `DeliveryOutcome(None, None, DELIVERY_FAILED, 502)`, leaving no record and no token and writing `record_event("runtime_credential_delivery_failed", record, _failed_summary(record, bound["label"], error))` for both (build the unsaved `record` first); then call `create_credential(credential_type=config.credential_type, data=credential_data(config.credential_type, proposal.mcp_url, secret), ...)` and set `credential_type=config.credential_type` on the record. `status_for` answers from `record.credential_type` with the two shapes of §3.5.
      4. `runtime_credential_sweeper.py`: the start-up check is `api.check_type(config.credential_type)`.
      5. `web/mcp_dispatch.py`: use `agent_runtimes.delivery_message(runtime_config.credential_type)` where `DELIVERY_MESSAGE` was used. `web/mcp_tools.py`: set the status tool's description to the verbatim text of §3.5. Run `.venv/bin/python scripts/generate_tools_reference.py` and commit the regenerated `docs/tools-reference.md`.
      6. Tests to extend: `tests/unit/test_agent_runtimes.py` (the new field, both error texts, `delivery_message`), `tests/unit/test_n8n_api.py` (`TestCreate` posts the given type and data, a wrong answered type is refused, `TestCheck` uses the given type and rejects a bad type string, `TestNoSecretLeaks` also covers `data`), `tests/unit/test_runtime_credentials.py` (`TestDeliverHappyPath` for both types with the exact `data`; a 404 from `check_type` answers 503 with no token and no record; an unreachable n8n, a 401 and a 500 answer 502 with no token and no record; each writes the audit entry; a records file written without `credential_type` still parses and reads as `httpBearerAuth`; `status_for` shapes), `tests/unit/test_runtime_credential_sweeper.py` (start-up check uses the configured type), `tests/unit/web/test_mcp_dispatch.py` (`TestProposeWithDelivery`: the message follows the configured type). `tests/unit/web/test_routes_org_settings.py` has a fake with `create_bearer_credential` (around `:1892`): rename it to `create_credential` with the new signature and add `check_type`; extend `TestDeliveryFromProposal` with a `privacyFenceApi` delivery that answers 503 with the `NODE_NOT_INSTALLED` text and restores the proposal. Remove every use of `create_bearer_credential` and `check`.
      7. Run `ruff check .`, `python3 -m mypy src/privacyfence`, `python3 scripts/mypy_strict_modules.py` and the tests named above and `tests/unit/web/test_tool_schema_portability.py`. Stop with status=blocked if `scripts/mypy_strict_modules.py` lists one of the touched modules and the change cannot satisfy it.
    acceptance:
      - "`python3 -m pytest tests/unit/test_agent_runtimes.py tests/unit/test_n8n_api.py tests/unit/test_runtime_credentials.py tests/unit/test_runtime_credential_sweeper.py tests/unit/web/test_mcp_dispatch.py tests/unit/web/test_routes_org_settings.py tests/unit/web/test_tool_schema_portability.py -q` passes"
      - "`grep -rn 'create_bearer_credential\\|\\.check()' src/privacyfence/n8n_api.py src/privacyfence/runtime_credentials.py src/privacyfence/runtime_credential_sweeper.py` matches nothing"
      - "`ruff check .` and `python3 -m mypy src/privacyfence` exit 0"
      - "`python3 -m pytest tests/unit -q -k tools_reference` passes"
  - id: p5-admin-setting
    title: The Credential type choice on Admin, Agent runtimes
    depends_on: [p4-delivery-core]
    complexity: S
    touches:
      - src/privacyfence/admin_config.py
      - src/privacyfence/web/routes_admin.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_admin_config.py
      - tests/unit/web/test_routes_admin.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Implement the Admin part of plan §3.5.
      1. `admin_config.py`: the default `agent_runtimes` section (`_default_config`, around `:154`) gains `"credential_type": "httpBearerAuth"`; `_apply_agent_runtimes` (around `:871`) takes `credential_type` from `params`, builds the candidate with it and validates through `N8nRuntimeConfig.from_org_config` as it does today, so both `ConfigurationError` texts of §3.5 reach the person.
      2. `web/routes_admin.py`: `ADMIN_PARAMS["set_agent_runtimes"]` (around `:158`) gets `("credential_type", str, True)` as its last entry.
      3. `settings_window_html.py`: in `renderRuntimes` (around `:2552`) add the select **Credential type in n8n** with the two options and the hint text of §3.5, and send the value as `credential_type` in the `set_agent_runtimes` form; add the key to the defaults dict at around `:3399`. Follow how the section's existing text and check fields are built.
      4. Tests to extend: `tests/unit/test_admin_config.py::TestAgentRuntimes` (accepts both types, refuses another with the exact text, refuses `privacyFenceApi` with an empty `mcp_url`, keeps the other fields), `tests/unit/web/test_routes_admin.py::TestAgentRuntimes` (the param is required and reaches the config, the state carries it), `tests/unit/test_settings_window_html.py` (the select and its two option values render; the form posts `credential_type`).
      5. Run `ruff check .`, `python3 -m mypy src/privacyfence` and the three test files.
    acceptance:
      - "`python3 -m pytest tests/unit/test_admin_config.py tests/unit/web/test_routes_admin.py tests/unit/test_settings_window_html.py -q` passes"
      - "`grep -n 'credential_type' src/privacyfence/admin_config.py src/privacyfence/web/routes_admin.py src/privacyfence/settings_window_html.py` matches in all three files"
      - "`ruff check .` and `python3 -m mypy src/privacyfence` exit 0"
  - id: p6-contract-test
    title: The contract test against the real daemon and the field-name pin
    depends_on: [p3-node-logic, p4-delivery-core]
    complexity: M
    touches:
      - tests/integration/test_n8n_node_contract.py
      - tests/unit/test_n8n_credential_fields.py
      - n8n/test/contract-driver.mjs
    brief: |
      Copy the structure of `tests/integration/test_shim_mcp_contract.py` (real `WebServer`, `EchoConnector`-style connector, a scoped token for it, skip when `node` is missing).
      1. `n8n/test/contract-driver.mjs` (`.mjs` so the community lint rules do not apply): a small script run with `node --import tsx` that imports `../nodes/PrivacyFence/transport/mcpClient.ts` and `result.ts`, and takes the MCP URL and the token as arguments, builds an `HttpSend` over Node's global `fetch` (`redirect: 'manual'`, text body, lower-case header map), opens a session with `openSession`, lists tools, calls one named tool, closes, and prints one JSON line `{tools: [...names], result: <toItem output>, closedSessionRefused: <bool>}` where `closedSessionRefused` is true when a further `tools/list` with the closed session's id gets a non-2xx. It is not run by `npm test` (it is not named `*.test.mts`).
      2. `tests/integration/test_n8n_node_contract.py`: a fixture runs `npm ci` in `n8n/` once per session (skip, as the shim test does, when it times out); tests start the real server with a scoped token that covers the echo tool and not a second tool, and assert: the listed tools are exactly the token's tools plus `privacyfence_status` and the other scoped meta tools (the node filters later); the call returns the echo tool's JSON; calling the uncovered tool returns `error` text starting `Request denied`; a revoked token gives the refused-token text; `closedSessionRefused` is true. The pytest timeout follows the shim test's.
      3. `tests/unit/test_n8n_credential_fields.py`: read `n8n/credentials/PrivacyFenceApi.credentials.ts` as text, extract the property `name:` strings with a regex, and assert they equal `set(runtime_credentials.credential_data("privacyFenceApi", "https://x/mcp", "t"))`; also assert the credential's `name` equals the type string used in `agent_runtimes.CREDENTIAL_TYPES`.
      4. Run `pytest tests/integration/test_n8n_node_contract.py tests/unit/test_n8n_credential_fields.py -v` and `ruff check .`.
    acceptance:
      - "`python3 -m pytest tests/integration/test_n8n_node_contract.py tests/unit/test_n8n_credential_fields.py -v -rs` passes with no SKIPPED line"
      - "`ruff check .` exits 0"
      - "`grep -c 'Request denied' tests/integration/test_n8n_node_contract.py` prints at least 1"
  - id: p7-publish-workflow
    title: The tag-driven npm publish workflow and its release docs
    depends_on: [p1-package]
    complexity: M
    touches:
      - .github/workflows/publish-n8n-node.yml
      - docs/releasing.md
      - scripts/release_run_guard.py
      - tests/unit/test_n8n_publish_workflow.py
    brief: |
      Implement the last bullet of plan §3.7.
      1. Read `.github/workflows/publish-pypi.yml` and copy its `dedupe` and `wait_for_build` jobs and its SHA-pinned `uses:` lines (take the `actions/setup-node` SHA from `.github/workflows/tests.yml`). Write `.github/workflows/publish-n8n-node.yml` as §3.7 describes (in the copied `dedupe` job replace the hard-coded `publish-pypi.yml` file name and the warning text with `publish-n8n-node.yml`, and add the new workflow to the callers list in `scripts/release_run_guard.py`'s docstring at `:11`; the `workflow_dispatch` input is only `dry_run`, and the version step is the one `publish-pypi.yml` has at `:238-240`): triggers, jobs `dedupe`, `wait_for_build`, `publish` (needs both; environment `npm`; permissions `id-token: write` and `contents: read` on that job only; a step that runs `python3 scripts/r2_release.py channel "$VERSION"` and stops the job unless it prints `stable`; every step of §3.7 in that order; `dry_run` adds `--dry-run` to the publish; the header comment explains the first-publish token and trusted publishing in four lines and points to `docs/releasing.md`). Workflow-dispatch with `dry_run: true` must not publish.
      2. `docs/releasing.md`: add a section **Publishing the n8n node to npm** after "Publishing to PyPI": what the workflow does, that it publishes on stable tags only, the `npm` environment and `NPM_TOKEN` (first publish) versus trusted publishing, that a rerun skips an already published version, and that it never uses `n8n-node release`. Add the new workflow to the list of workflows a tag starts (`docs/releasing.md` around `:139-150`), and add a **First release of the n8n node** paragraph: after the first stable tag that publishes it, open https://www.npmjs.com/package/n8n-nodes-privacyfence and check the version and the Provenance badge; then on the package's Settings page add the trusted publisher (owner `privacyfence`, repository `privacyfence`, workflow `publish-n8n-node.yml`, environment `npm`), and delete the `NPM_TOKEN` environment secret and the token on npm. Do not mention history.
      3. `tests/unit/test_n8n_publish_workflow.py`: model it on `tests/unit/test_live_check_workflows.py`; assert the workflow parses as YAML, `publish` has `id-token: write`, no other job does, every `uses:` is pinned to a 40-character SHA, the publish command contains `--provenance`, no step calls `n8n-node release` or `npm run release`, the rerun skip step compares the output of `npm view` with the version (the step text contains `= \"$VERSION\"`), and `workflow_dispatch` has no input named `tag`.
      4. Run `python3 -m pytest tests/unit/test_n8n_publish_workflow.py tests/unit/test_live_check_workflows.py -q` and `ruff check .`. Stop with status=blocked if a repository test pins the list of workflow files and fails for the new one in a way that needs a decision.
    acceptance:
      - "`python3 -m pytest tests/unit/test_n8n_publish_workflow.py -q` passes"
      - "`grep -c 'provenance' .github/workflows/publish-n8n-node.yml` prints at least 1"
      - "`grep -n 'n8n-node release\\|npm run release' .github/workflows/publish-n8n-node.yml` matches nothing"
      - "`grep -n 'Publishing the n8n node to npm' docs/releasing.md` matches"
  - id: p8-ci-dod
    title: CI job, audit, dependabot, branch-protection list and the definition of done
    depends_on: [p6-contract-test]
    complexity: M
    touches:
      - .github/workflows/tests.yml
      - .github/workflows/dependency-audit.yml
      - .github/dependabot.yml
      - scripts/update_branch_protection.py
      - .tld/toolkit.yaml
      - docs/coding-and-testing-guidelines.md
      - docs/testing-policy.md
    brief: |
      Implement the first four bullets of plan §3.7.
      1. `tests.yml`: add the job `n8n-node` (name exactly `n8n-node`) as §3.7 says, with the artifact upload; in the `test` job add a step `npm ci` with `working-directory: n8n` before pytest (copy the shim's install step and cache keys, with `n8n/package-lock.json`); everywhere the file excludes `tests/integration/test_shim_mcp_contract.py` add the same exclusion for `tests/integration/test_n8n_node_contract.py`, and update the comment above `test-python-compat` to name both.
      2. `dependency-audit.yml`: add `n8n/package.json` and `n8n/package-lock.json` to both path filters (around `:55-56` and `:67-68`) and add an `npm audit` step for `n8n/` beside the shim's (around `:200-222`), copying its flags.
      3. `.github/dependabot.yml`: add an `npm` entry for `/n8n`, weekly, group `n8n-node-dependencies`; update the header comment's count and list.
      4. `scripts/update_branch_protection.py`: add the required check name `n8n-node` to `REQUIRED_STATUS_CHECKS` (around `:108`); `tests/unit/test_update_branch_protection.py` pins no list, so no test change is needed, but it must still pass.
      5. `.tld/toolkit.yaml`: add to `verify.dod_conditional` a row `when: ["n8n/**"]`, `do:` "In n8n/: `npm ci`, `npm run lint`, `npm run typecheck`, `npm run build` and `npm test` must pass, then `pytest tests/integration/test_n8n_node_contract.py -v`." Add the matching bullet to `docs/coding-and-testing-guidelines.md` §2.7 beside the `sync/**` and `cloudflare/**` ones, and a sentence in `docs/testing-policy.md` where it lists the shim contract test. If the hook that guards `.tld/` refuses the edit, stop with status=blocked and say so.
      6. Run `ruff check .` and `python3 -m pytest tests/unit -q -k "workflow or branch_protection or docs"`; run `tld profile validate .tld/toolkit.yaml` (`"$TLD" profile validate .tld/toolkit.yaml` where `TLD` is the toolkit's `cli/tld`) and show it exits 0.
    acceptance:
      - "`grep -n 'n8n-node' .github/workflows/tests.yml scripts/update_branch_protection.py` matches in both files"
      - "`grep -n 'n8n/package-lock.json' .github/workflows/dependency-audit.yml` matches"
      - "`grep -n 'directory: \"/n8n\"' .github/dependabot.yml` matches"
      - "`grep -n 'n8n/\\*\\*' .tld/toolkit.yaml docs/coding-and-testing-guidelines.md` matches in both files"
      - "`python3 -m pytest tests/unit -q -k 'workflow or branch_protection or docs'` passes"
  - id: p9-docs
    title: The n8n node doc, the guides and the doc registrations
    depends_on: [p5-admin-setting, p7-publish-workflow, p8-ci-dod]
    complexity: M
    touches:
      - docs/n8n-node.md
      - docs/org-mode-setup-guide.md
      - docs/security-and-compliance.md
      - docs/configuration-reference.md
      - docs/README.md
      - docs/how-it-works.md
      - CONTRIBUTING.md
      - tests/unit/test_docs_no_history.py
    brief: |
      Write the user docs for what p1-p8 built. Docs describe today's behaviour and never mention history (`tests/unit/test_docs_no_history.py` enforces it).
      1. Create `docs/n8n-node.md`: what the node is, requirements (self-hosted n8n that allows community nodes; delivery needs n8n 2.40.0 or later), install from npm (`n8n-nodes-privacyfence`), the credential (PrivacyFence MCP address and token), a worked example (a PrivacyFence node calling one auto-approved tool, the `reason` field, the arguments JSON, the item it returns), using it as an AI Agent tool, what a refusal looks like (`Request denied ...`, and Continue On Fail), the delivery switch (**Admin → Agent runtimes → Credential type in n8n**, that it is server-wide and what that means for an AI Agent's MCP Client Tool, that the MCP address is required for it), the exposure of §3.6 in two sentences, and troubleshooting for each fixed text of §3.3 and `NODE_NOT_INSTALLED`.
      2. Update `docs/org-mode-setup-guide.md` (the "Delivering tokens to n8n" section around `:1132`: the credential type setting, the node, and the API-key scope note), `docs/security-and-compliance.md` (the "Tokens delivered to n8n" section: the new credential holds the address and who can change it), `docs/configuration-reference.md` (the new `credential_type` key; `tests/unit/test_docs_configuration_reference.py` says how keys are covered), `docs/how-it-works.md` where it names n8n, and `CONTRIBUTING.md` "Code Style" (one line for `n8n/`: TypeScript, an npm package published from CI, strict n8n community-node lint).
      3. Register `docs/n8n-node.md` the way the comparable doc `docs/org-mode-docker.md` is registered: run `grep -rln "org-mode-docker" docs tests scripts website src --include='*' | grep -v 'docs/adr\|egg-info\|node_modules'` and make the same kind of entry (the `docs/README.md` index row, and the published-docs or contributor-docs lists in `tests/unit/test_docs_no_history.py` and in the site builder and its tests, whichever the grep shows). Do not register it as an AI client: no `website/_data/clients.json` entry and no `website/ai-agents/` page.
      4. Report `docs-changed: added docs/n8n-node.md` and any other doc added.
      5. Run `python3 -m pytest tests/unit -q -k "docs or build_site or website"` and `ruff check .`.
    acceptance:
      - "`python3 -m pytest tests/unit -q -k 'docs or build_site or website'` passes"
      - "`grep -n 'n8n-node' docs/README.md` matches"
      - "`grep -n 'credential_type' docs/configuration-reference.md` matches"
      - "`grep -n 'n8n/' CONTRIBUTING.md` matches"
      - "`grep -rn 'n8n-node.md' n8n/package.json n8n/credentials n8n/README.md` matches in each of the three"
  - id: p10-adrs-changelog
    title: ADRs and changelog (retirement phase)
    depends_on: [p9-docs]
    complexity: M
    touches:
      - docs/adr/0283-the-n8n-node-ships-from-this-repository-and-is-published-with-provenance.md
      - docs/adr/0284-the-delivered-credential-type-is-an-administrators-choice.md
      - docs/adr/0285-the-privacyfenceapi-credential-holds-the-address-and-the-token.md
      - docs/adr/0286-the-n8n-node-carries-its-own-handshake-only-mcp-client.md
      - docs/adr/0287-the-n8n-node-never-follows-redirects-or-builds-errors-from-caught-values.md
      - docs/adr/0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md
      - docs/adr/0280-privacyfence-runtime-credential-status-reports-a-delivered-credential-to-its-proposer.md
      - docs/adr/README.md
      - CHANGELOG.md
    brief: |
      Extract the plan's decisions into ADRs and add the changelog entries. Read `docs/adr/README.md` first for the template, the status vocabulary, the one-decision-per-ADR rule, how amendments are recorded (rule 3: the amended ADR's Status gains one line pointing forward, and nothing else in its body changes) and how index rows are written; read `docs/adr/0278-*.md` and `docs/adr/0280-*.md` for the style.
      1. Write the five ADRs with the file names above, each `Accepted — 2026-11-14. Implemented.`, each linking issue 1095 (`https://github.com/privacyfence/privacyfence/issues/1095`) and the source files, never the plan. Content, from the plan: 0283 is §3.1 and §3.7 (distribution and publishing), rejecting "a separate repository" and "publishing with the node CLI's release"; 0284 is §3.5's setting, the 404-only "node not installed" check before minting, the old-records rule, and rejects "always delivering privacyFenceApi" and "checking the node at propose time"; 0285 is §3.2 and §3.6 (the credential holds the address and the token, no `authenticate` block, who can change the address) and rejects "an authenticate block"; 0286 is §3.3 (handshake revision only, own client, no dependencies) and rejects the SDK, the 2026-07-28 revision and the fallback; 0287 is §3.4's rules (`disableFollowRedirect`, fixed texts, no caught error, request or credential value reaches an error or an item) and the lint rules behind them.
      2. Add one line at the end of the `## Status` section of `docs/adr/0278-*.md` and of `docs/adr/0280-*.md`: `Amended by [0284](0284-the-delivered-credential-type-is-an-administrators-choice.md).` (0278 also by `[0285](0285-the-privacyfenceapi-credential-holds-the-address-and-the-token.md)`). Change nothing else in them.
      3. Add the five rows to `docs/adr/README.md` in its table format, and the amendment marks its rules ask for on the 0278 and 0280 rows.
      4. `CHANGELOG.md`, under `## [Unreleased]` in the existing **Added** and **Changed** subsections (create them only if missing): an **Added** entry for the n8n node (`n8n-nodes-privacyfence`: what it does, that it is published on stable releases, link `docs/n8n-node.md`) and a **Changed** entry for the Admin credential type (default unchanged, what switching does). No version heading.
      5. Run `python3 -m pytest tests/unit -q -k "docs or adr or changelog"`.
    acceptance:
      - "`ls docs/adr/0283-*.md docs/adr/0284-*.md docs/adr/0285-*.md docs/adr/0286-*.md docs/adr/0287-*.md` lists five files"
      - "`grep -c '| \\[028[3-7]\\]' docs/adr/README.md` prints 5"
      - "`grep -c 'Amended by' docs/adr/0278-*.md docs/adr/0280-*.md` prints at least 1 for each file"
      - "`awk '/^## \\[Unreleased\\]/{f=1;next} /^## \\[/{f=0} f' CHANGELOG.md | grep -c 'n8n-nodes-privacyfence'` prints at least 1"
      - "`grep -m1 '^## \\[[0-9]' CHANGELOG.md` prints `## [7.0.2] — 2026-11-06`"
      - "`python3 -m pytest tests/unit -q -k 'docs or adr or changelog'` passes"
```
