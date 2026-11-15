# Signed identity and audience claim for upstream MCP servers

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An operator lists an upstream in `PRIVACYFENCE_UPSTREAM_MCP_FILE` with `identity_claim` (default `required`) and maps groups to audiences in `admin_config.json`. A n8n job sends the Workspace group membership to the tool `privacyfence_update_group_membership` with a scoped token an admin made. Every call PrivacyFence forwards then carries an EdDSA claim in `X-PrivacyFence-Claim` naming the person, their audiences and the exact tool call; the upstream checks it with `contrib/verifier/pf_claim_verify.py`. `privacyfence-app --rotate-upstream-claim-key` rotates the signing key.

**Scope:** The shipped RS256 `Authorization: Bearer` claim (ADR 0286) and the email grants (ADR 0287) are replaced, not kept beside the new ones; nothing is released. New: the claim contract and `docs/identity-claim.md`, the key set and rotation, group membership with a static and a snapshot resolver, one audit entry per forwarded call, the verifier, and three ADRs. Not changed: the JWKS path `/upstream-mcp/jwks`, tool gates, cards. Out of scope: filtering results in PrivacyFence, the Google Directory API, any hub-specific code.

**Security:** This is the identity PrivacyFence asserts to other servers. A claim lives at most 60 seconds and `req` binds it to one call. A request that is not a tool call carries no audiences. Membership removals apply at once; additions to any audience but `all` wait for an admin on the Admin page. The sync tool needs a scoped token an admin created.

**Review focus:** the `req` binding and the empty-audience rule for non-call requests (3.2, 3.3); the approval model and the sync tool's trust limits (3.7, 3.8); the key file, its locking, the delayed signing start and when a retired key's seed is dropped (3.4); the phase order that keeps every merge green, with the old claim and the new one side by side until phase 10 (manifest).

## 1. Goal

PrivacyFence becomes the single place that decides who a caller is and which audiences they hold, and proves it to an upstream MCP server with a short signed claim bound to one tool call, so the upstream can enforce it in its own queries (issue [#1103](https://github.com/privacyfence/privacyfence/issues/1103)). Audiences come from Workspace groups, kept current by an n8n job through a PrivacyFence tool.

## 2. Current state

Both prerequisite features are on `main` (PR #1091/#1100, ADRs 0285–0287) and unreleased. The extension points:

- **Claim today** (`src/privacyfence/upstream_claims.py`): RS256, `Authorization: Bearer`, header `typ` `pf-upstream-claim+jwt`, payload `sub`, `email`, `email_verified`, `domains`, `act`, `pf_upstream`, `pf_tool`, `iat`, `nbf`, `exp`, `jti`; key file `upstream_claim_signing_key.pem` (`:27`); `ClaimAuth` (`:94`) mints one claim per HTTP request; `CallerClaim` (`:32`); `load_signer` (`:70`).
- **Who is calling** (`src/privacyfence/upstream_mcp_runtime.py:100`, `caller_for`): reads `current_principal()` and `current_token_scope()`, takes domains from `GrantStore` (`upstream_mcp_config.py:328`), intersects a scoped token's `upstream_domains` with what its owner holds now.
- **Config** (`upstream_mcp_config.py`): operator file parsed by `parse_upstreams` (`:123`) with unknown keys refused (`:_UPSTREAM_KEYS`); grants in `admin_config.json` `upstream_mcp.<id>.grants` (`:_parse_grants`).
- **Transport** (`upstream_mcp_client.py:60`): a fresh Streamable HTTP session per call, so one tool call is several HTTP requests (initialize, initialized, `tools/call`, delete).
- **Connector** (`connectors/upstream_mcp.py`, `_forward` at `:155`, `_auto_audit` at `:172`): `auto` writes an `auto_accepted` audit entry only after success (`_auto_audit`); `review` and `popup` audit through `gated_call`; a failed forward writes nothing.
- **Scoped tokens** (`scoped_tokens.py:76-100`): `upstream_domains`, `principal_email_verified`; `create` refuses a domain the creator does not hold (`:681`); the check runs again at every call.
- **JWKS route** (`web/routes_upstream_mcp.py`): `/upstream-mcp/jwks`, public.
- **Audit** (`audit_log.py:130-486`): `AuditEntry` with no result status, no extra fields; `CURRENT_SCHEMA_VERSION = 7` (`:73`).
- **CLI**: flat flags in `daemon_main.py` (`--revoke-scoped-tokens` at `:2680`, parsed at `:3033`, dispatched at `:3179`, after `check_runtime_identity`).
- **Caller identity**: the OAuth `client_id` is in scope only at `web/routes_mcp.py:490-514` and is in no contextvar; `AgentIdentity` has `id, name, version, source` (`agent_identity.py:63`).
- **No admin-token route exists**; `web/control_channel.py:5` rejects one on purpose. No group concept exists.

**Conflicts with the request, settled by the owner** (board ask `a-f4fd10c1707d51e1` and chat, 2026-11-14): replace the shipped claim; keep the JWKS path; rename `domains` to `audiences` ("domain" means portfolio/product/technical in the knowledge hub); groups only; `sub` is the person with the AI client in `act`; for a n8n token `sub` is `n8n-token:<id>` and `approved_by` (the creator) replaces the request's `on_behalf_of`; no inbound admin-token route, membership arrives through a tool called with a scoped token; connection narrowing deferred; the verifier is a single vendorable file.

## 3. Design

### 3.1 The claim (`claim_version: 1`)

New module `src/privacyfence/identity_claim.py` (replaces `upstream_claims.py`, deleted in phase 10). Constants: `CLAIM_VERSION = 1`, `CLAIM_TYPE = "pf-identity-claim+jwt"`, `CLAIM_HEADER = "X-PrivacyFence-Claim"`, `MIN_LIFETIME_SECONDS = 5`, `MAX_LIFETIME_SECONDS = 60`.

Header: `{"alg": "EdDSA", "kid": <kid>, "typ": "pf-identity-claim+jwt"}`. The claim is sent only in `X-PrivacyFence-Claim`; `Authorization` is never set or read.

Payload (a key is omitted, never null, when it does not apply):

| Claim | Value |
|---|---|
| `claim_version` | `1` |
| `iss` | the server's issuer URL, no suffix (for example `https://pf.example.org`); the shipped claim used `<issuer>/upstream-mcp` |
| `aud` | the upstream's `audience` from the operator file |
| `sub` | the person's principal id; `n8n-token:<token id>` for a scoped token; `privacyfence` for a service claim |
| `principal_type` | `claude_client` (a person's AI client), `n8n_workflow` (any scoped token), `service` (PrivacyFence's own startup tool list), `human` (reserved, never minted) |
| `email` | the verified email, only when `sub` is a person |
| `approved_by` | the creating person's id, only for `n8n_workflow` |
| `token_id` | the scoped token id, only for `n8n_workflow` |
| `act` | `{"sub": <OAuth client id>, "agent": <attested agent id>}` for `claude_client` (RFC 8693 actor; `agent` only when attested; `sub` only when known); absent otherwise |
| `audiences` | sorted list; `[]` when unresolved |
| `tools` | sorted upstream tool names this principal may call on this upstream, information only |
| `req` | on a `tools/call` request only (3.2) |
| `iat`, `exp`, `jti` | `exp - iat` is the upstream's `claim_lifetime_seconds` (5–60, default 60); `jti` is a UUID string (the shipped claim used `uuid4().hex`) |

A workflow does not act live for a person, so a `n8n_workflow` claim carries no `email` and cannot match attendee-only rows; `approved_by` is for audit.

### 3.2 `req`, canonical JSON and the request binding

`req = base64url_nopad(SHA-256(canonical_request(tool, arguments)))`, where `canonical_request` is `json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`. `tool` is the upstream's own tool name (`search`, not `knowledgehub_search`); `arguments` is the object of the `tools/call` request without `reason` (an absent object is `{}`). Keys sort by Unicode code point; non-ASCII is written as UTF-8, not escaped; integers are decimal; other numbers use Python's shortest `repr`; duplicate keys are refused by the verifier. The six vectors in `contrib/verifier/req_test_vectors.json` (computed independently of any implementation, listed in phase 5) are the contract.

`ClaimAuth` (new class `IdentityClaimAuth`, `requires_request_body = True`) reads the JSON-RPC body of each HTTP request it signs. For `method == "tools/call"` it hashes `params.name` and `params.arguments` as sent, so the hash covers what the upstream actually receives. For every other request (initialize, initialized, delete) the claim has `audiences: []`, `tools: []` and no `req`. The verifier refuses a `tools/call` without a matching `req`, refuses a `req` on anything else, refuses a JSON-RPC batch, and returns no audiences for a request that is not a call. This closes the hole the architect found (a captured claim without `req` carrying audiences for `resources/read` or a batch) while keeping the `{tool, arguments}` format the owner specified. `params._meta` is not covered; the contract says an upstream dispatches only the tool name and arguments it hashed.

### 3.3 When a call is signed, refused or fails closed

Per upstream in the operator file: `identity_claim` is `required` (default) or `off`; `claim_lifetime_seconds` 5–60 (default 60); `allowed_principal_types`, a non-empty subset of `["claude_client", "n8n_workflow"]` (default both).

- `off`: no claim, no `X-PrivacyFence-Claim` header, no caller resolution; the forwarded-call audit entry (3.9) says `identity_claim: off`.
- `required` and the caller's type is not allowed: nothing is sent; `UpstreamCallRefusedError` with the text `PrivacyFence does not allow this caller to use the <label> server.`; audit `upstream_status` `refused`.
- `required` and signing raises: nothing is sent; `UpstreamUnavailableError` with `The <label> call was not sent: PrivacyFence could not sign the caller claim.`; `upstream_status` `claim_failed`.
- `required` and membership unresolved: the call goes through with `audiences: []`.
- The startup tool list is a `service` claim with `audiences: []`. `service_audiences` configuration is not added: no service call exists today (listed under open decisions).

### 3.4 Keys, JWKS and rotation

New module `src/privacyfence/upstream_claim_keys.py`. File `org_dir()/upstream_claim_keys.json`, mode 0600, written with `secure_files.atomic_write_json`, shape `{"version": 1, "keys": [{"kid", "x", "seed", "created_at", "signs_from", "retired_at"}]}` (`x` is the base64url public key, `seed` the base64url 32-byte private seed or `null`). `kid` is the first 16 characters of the base64url SHA-256 of the raw public key.

- The key that signs is the newest key with `signs_from <= now` and not retired. First start creates one key with `signs_from = created_at`.
- `rotate()` adds a key with `signs_from = now + 300` (the JWKS `Cache-Control` of 300 s, so verifiers see it before it signs), sets the previous key's `retired_at` to that instant. The previous key keeps signing until then; its `seed` is dropped only once `retired_at <= now`, when the set is pruned or loaded, never in `rotate()`.
- JWKS lists every key that is not retired or whose `retired_at + overlap` is still in the future; each entry is `{"kty": "OKP", "crv": "Ed25519", "x", "use": "sig", "alg": "EdDSA", "kid"}`. Overlap is operator-file `claim.key_overlap_hours` (1–720, default 24).
- Every read-modify-write holds a `flock` on `upstream_claim_keys.lock` (a threading lock only where `fcntl` is missing). The server reloads on a change in (inode, `mtime_ns`, size) and keeps the last good set when the file is malformed. A key past `retired_at + overlap` is pruned when the server loads the set.
- CLI: `--rotate-upstream-claim-key` and `--revoke-upstream-claim-key KID` (removes the key from the JWKS at once; refuses the only signing key). Both print the result and run after `check_runtime_identity`.
- The old `upstream_claim_signing_key.pem` is ignored and never deleted.

### 3.5 Audiences and the resolver interface

Removed: `grants` in `admin_config.json` (an `upstream_mcp.<id>.grants` key is refused with `grants were replaced by groups`).

New in `admin_config.json`: `upstream_mcp.<id>.groups`, an object `{"mgmt@company": ["mgmt"], "everyone@company": ["all"]}`: a group name (lower case, email-shaped) to a list of audience names (`[a-z0-9][a-z0-9_-]{0,62}`); at most 200 groups. Hot-reloaded like grants were. `all` is the one unrestricted audience (`UNRESTRICTED_AUDIENCE`); every other audience is restricted. A group with one member covers a one-off exception.

`group_membership.MembershipResolver` (Protocol): `audiences_for(email: str, group_audiences: Mapping[str, frozenset[str]]) -> frozenset[str]`; `[]` for an empty or unverified email. Operator file `membership`: `{"source": "snapshot" | "static", "file": "<path>" (static only), "max_additions": 1–1000 (default 20)}`; default source `snapshot`.

- `StaticFileResolver(path)`: reads `{"version": 1, "groups": {"mgmt@company": ["a@x.example"]}}`, reloaded on mtime change, no approvals; for bootstrap and tests.
- `SnapshotResolver(store)`: reads the store written by the tool (3.7).

A person's audiences are the union over the groups they belong to, from the verified email only. A scoped token holds `upstream_audiences` chosen at creation (a subset of what its creator held then) and each call uses `owner's audiences now ∩ upstream_audiences`, so a creator who loses an audience takes it from the token on the next call (the rule `held_domains` already implements, renamed).

### 3.6 Naming

`domains` becomes `audiences` everywhere: `ScopedToken.upstream_domains` → `upstream_audiences` (JSON key too; a token written by a development build loses its domains, no migration), `held_domains` → `held_audiences`, the New token form (`Audiences for <label>`), the claim. Phase 8 does this rename alone while the old claim and the grants still run.

### 3.7 Group membership store and the sync tool

Module `src/privacyfence/group_membership.py`; file `org_dir()/group_membership.json` (atomic write, module lock, `get_store()` / `set_store(None)`, reset in `tests/conftest.py`). State: `members` (group → emails), `approvals` (list of `[email, group, audience]`), `declined` (same shape), `history` (timestamp and counts of the last snapshots), `updated_at`.

`apply_snapshot(groups, *, group_audiences, now, max_additions)`:

1. Emails are lower-cased and need exactly one `@`; group names are lower-cased; at most 500 groups and 20000 member entries in total, else `rejected`.
2. Groups not named in any upstream's `groups` map are ignored (counted in `ignored_groups`, never stored). Mapping a group later makes it appear in the next snapshot.
3. Additions are emails newly in a group; removals are stored members absent from the snapshot, and every member of a group missing from the snapshot. Removals apply at once and delete that member's approvals and declines for the group.
4. If the snapshot's additions plus those of the last 24 h exceed `max_additions`, the whole snapshot is `rejected` (`too_many_additions`) and nothing is applied. The first snapshot ever received is exempt (bootstrap). More than 500 pending approvals after the snapshot also rejects it.
5. Otherwise the members are stored. Pending approvals are derived, never stored: a member of a group whose map grants a restricted audience, with no approval and no decline for that (email, group, audience).

The resolver grants an audience only if the group's current map names it and (the audience is `all` or an approval exists) and the email is currently a member. So an addition to `all` applies at once, and an addition to `mgmt` waits. `approve(group, audience, email=None)` approves one person or, with no email, everyone pending for that (group, audience) (never across groups); `decline` is the mirror and is forgotten when the person leaves the group.

Tool `privacyfence_update_group_membership` (connector class `GroupMembershipConnector`, `name` `privacyfence`, file `connectors/group_membership.py`, gate `auto`, no verb, no operation, `read_only=False`). Input: `{"groups": {"<group>": ["<email>", ...]}}`. Result: `{"status": "applied" | "rejected", "added": n, "removed": n, "parked": n, "ignored_groups": [...], "reason": "..."}`. It runs only when `current_token_scope()` is not `None` and the scope's tools contain the tool name; any other caller gets `RuntimeError("privacyfence_update_group_membership can only be called with a scoped token.")` and a `group_membership_denied` audit entry, so a prompt-injected AI client cannot call it. Only an admin can create a token that includes it (3.8). Audit decisions: `group_membership_applied`, `group_membership_rejected`, `group_membership_denied`, `group_membership_approved`, `group_membership_declined`; summaries hold counts and group names, never email addresses.

Rejected alternatives and why: an inbound admin-token route (the control channel rejects it, ADR in `web/control_channel.py:5`); a popup gate for additions (a scoped token is denied, ADR 0164, so the card can never be answered); a 0600 digest file (kept as the fallback the owner named; not needed because the tool fits the scoped-token model). The fallback was not used, so the owner is not asked.

### 3.8 Who may create the sync token

`ScopedTokenStore.create` gains `is_admin: bool = False`; a token whose tools include `TOOL_NAME` of the connector is refused with `Only an administrator can create a token for privacyfence_update_group_membership.` unless it is true. `routes_settings` and the delivery path pass `principal.is_admin`; an AI proposal that includes the tool is refused (`This tool cannot be proposed; create the token in Settings.`). Scoped tokens are never admin at call time, so the check is at creation. A creator who later loses admin keeps a working token until it expires or is revoked (open decision).

### 3.9 Audit of every forwarded call

`AuditEntry` gets, appended after `expired_decision`: `claim_jti`, `claim_sub`, `claim_principal_type`, `claim_audiences` (list), `claim_act` (dict of strings), `claim_req`, `upstream_status`, `metadata` (dict of strings). `CURRENT_SCHEMA_VERSION` becomes 8 with a history line. `UpstreamMcpConnector._forward` writes one entry with decision `upstream_forwarded` for every gate and every outcome (`ok`, `tool_error`, `unreachable`, `too_large`, `refused`, `claim_failed`). `metadata` may hold only `upstream`, `claim_version`, `identity_claim`, `token_id`, `approved_by`. The claim string, argument values and upstream text never reach the audit log or a log line; a test greps for them. The existing `auto_accepted` entry stays (the policy decision); the new entry is the execution record.

### 3.10 The verifier and the contract

`contrib/verifier/pf_claim_verify.py`, one file, Python 3.9-compatible syntax, imports only the standard library, `jwt` (PyJWT) and `cryptography`. API: `ClaimVerifier(*, issuer, audience, jwks_url=None, jwks=None, fetch=None, leeway=5, jwks_ttl=300, replay_cache=<default on>, clock=time.time)`, `verify(token, *, body=b"") -> Principal`, `verify_tool_call(token, tool, arguments) -> Principal`; `canonical_request`, `request_hash`; `Principal` (frozen dataclass: `sub, principal_type, email, audiences, tools, approved_by, token_id, act, jti, expires_at, claim_version`); `ClaimError(code)`. It checks `alg` EdDSA, `typ`, `kid`, the signature, `iss`, `aud`, `exp`/`iat` with leeway at most 5 s, `exp - iat <= 60`, `claim_version == 1`, `principal_type`, the body rules of 3.2, and the `jti` (a replay is refused by default; `replay_cache=None` switches it off; a shared store plugs in through `add_if_new(jti, expires_at) -> bool`). Exactly one of `jwks_url` and `jwks` (a static dict) is required. JWKS is fetched with `urllib`, cached `jwks_ttl` seconds, refetched at most every 30 s on an unknown `kid`. `docs/identity-claim.md` is the contract and versions it.

### 3.11 What was not adopted from the architect's review

Hashing all of `params` (the owner fixed `{tool, arguments}`); a rolling cap and a step-up restore for removals (the owner decided removals apply at once); restricting addresses to the org's allowed domains; a live admin check at call time (no live admin lookup exists). Each is under open decisions.

## 4. ADRs

- **0288**: The upstream claim is an EdDSA identity claim in its own header, bound to one call (amends 0286): format, `req`, key set and rotation, why replaced in place.
- **0289**: Audiences come from group membership, and tokens only narrow them (amends 0287): groups map, resolvers, default deny, approvals bound to (email, group, audience), `sub` rules.
- **0290**: Group membership is updated through a scoped-token tool: removals apply, additions to restricted audiences wait for an admin (relates 0164, 0250): trust model, caps, why no admin route.

## 5. Manual steps

There is nothing to do before implementation.

**After implementation**

1. `ma1-sync-token` (about 15 minutes): an admin creates the sync token and checks the Admin card.
   1. Start an organization server with a `knowledgehub` upstream from `contrib/examples/knowledge-hub/` ([setup guide](https://github.com/privacyfence/privacyfence/blob/feature/upstream-identity-claim/docs/upstream-mcp.md)).
   2. Sign in as an admin and open Settings, Scoped tokens, New token. Tick `privacyfence_update_group_membership` under "Tools that need no rule", set a label and days, create the token and copy the secret into the n8n credential (never into chat).
   3. Call the tool from n8n with `{"groups": {"mgmt@company": ["<a person>"]}}`; the reply says `parked: 1`.
   4. Open Admin, Group membership, approve that person for `mgmt`; the next call of the person's AI client to `knowledgehub_search` carries `mgmt` in `audiences`.
   Report back: pass/fail for each of steps 3 and 4.
2. `ma2-hub-verification` (about 20 minutes): the real hub vendors `pf_claim_verify.py` and verifies a live claim.
   1. Copy `contrib/verifier/pf_claim_verify.py` into the hub and call `ClaimVerifier.verify` with the raw request body ([contract](https://github.com/privacyfence/privacyfence/blob/feature/upstream-identity-claim/docs/identity-claim.md)).
   2. Run `privacyfence-app --rotate-upstream-claim-key` on the server and call a tool again after six minutes.
   Report back: pass/fail for a call before and after the rotation.

## 6. Risks and open questions

Stop conditions: a worker that finds `httpx2.Auth` cannot read the request body, a tool name that cannot start with `privacyfence_` for a connector, a `TOOL_REGISTRY` invariant test that rejects an `auto` write tool, or an audit decision list it cannot extend by following `runtime_credential_status_read`, stops with `status=blocked`.

- Python float formatting in `req` is the one place another language can differ; the vectors include `1.5` and a 20-digit integer, and the contract says so.
- The old claim and the new one run side by side until phase 10; phases 1–9 add code that nothing calls yet, so every merge keeps the unit suite green.
- The Admin card has browser tests that skip in this cloud container (no Chromium); `ma1-sync-token` is the real check.

**Open decisions for the PR description** (not decided here):

1. An upstream's own `Authorization` credential: the header is free, but PrivacyFence cannot yet send a credential of its own to the upstream.
2. `service_audiences`: no service call exists; the key arrives with the first caller.
3. Any scoped token is typed `n8n_workflow`, delivered to n8n or not.
4. A token creator who loses admin keeps a working sync token until it expires; there is no live admin lookup.
5. Mass removals by a stolen sync token (denial of service) are not capped; a rolling cap and a restore from the last snapshots are suggested.
6. Membership addresses are not checked against the org's allowed domains.
7. Alerting on a rejected snapshot is an audit entry, a log line and an Admin card row; no push to admins.
8. `params._meta` is not bound by `req`.
9. The new key signs five minutes after rotation, not at once.
10. Narrowing by connection stays deferred; `act.sub` carries the OAuth client id so upstreams can log it.

## Implementation manifest

```yaml
plan_slug: upstream-identity-claim
feature_branch: feature/upstream-identity-claim
tracking_issue: 1103
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-sync-token
    title: An admin creates the sync token, parks and approves a member
    why: The Admin approval card and the token form are browser UI that CI cannot click through here, and the n8n job is outside the repository.
  - id: ma2-hub-verification
    title: The real hub verifies a live claim across a key rotation
    why: Only the hub has its own verifier integration and a real network path to PrivacyFence.
verify_after_merge: []
final_checks:
  - "`tld plan deviations check --file docs/1103/deviations.md --plan docs/1103/plan.md --all` exits 0"
  - "ADRs 0288, 0289 and 0290 exist, are Accepted and are in docs/adr/README.md"
  - "CHANGELOG.md has entries under ## [Unreleased] and no new version heading"
phases:
  - id: p1-claim-keys
    title: The Ed25519 claim key set and its rotation flags
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/upstream_claim_keys.py
      - src/privacyfence/daemon_main.py
      - tests/unit/test_upstream_claim_keys.py
      - tests/unit/test_daemon_main.py
    brief: |
      Read first: src/privacyfence/secure_files.py, src/privacyfence/paths.py (org_dir), src/privacyfence/oidc_provider.py (the RSA key code this replaces in spirit, not to import).
      Design: plan section 3.4. Pattern for the CLI flag and its tests: run_revoke_scoped_tokens (daemon_main.py, search the name), its parse_args entry and tests/unit/test_daemon_main.py TestRevokeScopedTokens.
      1. Create src/privacyfence/upstream_claim_keys.py (module docstring first, from __future__ import annotations). Define frozen dataclass ClaimKey(kid, x, seed, created_at, signs_from, retired_at) with seed bytes|None and retired_at float|None; class KeySetError(RuntimeError); constants FILENAME = "upstream_claim_keys.json", LOCK_FILENAME = "upstream_claim_keys.lock", JWKS_CACHE_SECONDS = 300.
      2. class KeySet(path: Path, *, overlap_seconds: int = 86400, clock: Callable[[], float] = time.time) with methods: ensure() (creates the first key when the file is missing, under the lock), signing_key(now=None) -> ClaimKey (the newest key with signs_from <= now and retired_at None or > now and seed not None; raises KeySetError("No upstream claim signing key is usable.") otherwise), sign(payload: dict, now=None) -> str (jwt.encode with algorithm "EdDSA", headers {"kid": key.kid, "typ": "pf-identity-claim+jwt"}, private key Ed25519PrivateKey.from_private_bytes(seed)), jwks(now=None) -> dict with the entries of plan 3.4, rotate(now=None) -> ClaimKey, revoke(kid) -> bool (refuses with KeySetError when kid is the only key that can sign), prune(now=None).
      3. kid = base64url without padding of sha256(raw public key bytes) cut to 16 chars. Storage: secure_files.atomic_write_json(path, data, indent=2, sort_keys=True). Locking: a module function _locked(path) context manager opening LOCK_FILENAME next to the file, fcntl.flock(LOCK_EX) when fcntl imports, else a module-level threading.Lock. Reload: remember (st_ino, st_mtime_ns, st_size) of the file; re-read when it changes; a malformed or unreadable file logs a warning with logger.warning and keeps the last good set (the first load raises KeySetError). rotate(): inside the lock re-read, add the new key with signs_from = now + JWKS_CACHE_SECONDS, set the previous signing key's retired_at to that signs_from (its seed stays until then), write. prune(now) first sets seed to None for every key with retired_at <= now, then drops keys whose retired_at + overlap_seconds is in the past; call it from ensure(), rotate() and every reload of the file.
      4. daemon_main.py: add run_rotate_upstream_claim_key() -> int and run_revoke_upstream_claim_key(kid: str) -> int beside run_revoke_scoped_tokens. They build KeySet(paths.org_dir() / FILENAME), call ensure() then rotate()/revoke(), print "Rotated: new key <kid> starts signing at <ISO time UTC>; the previous key stays published for the configured overlap." or "Revoked key <kid>." or, for an unknown kid, "No such key." on stderr and return 1. Add parse_args options --rotate-upstream-claim-key (action store_true) and --revoke-upstream-claim-key (metavar KID), and dispatch both in main() in the group after check_runtime_identity, next to --revoke-scoped-tokens.
      5. Tests tests/unit/test_upstream_claim_keys.py (inject clock, tmp_path): first start creates one key and the file is mode 0600; jwks has one OKP Ed25519 entry whose x verifies a signed token; rotate publishes the new key at once but signs with the old key until signs_from, then with the new key; during overlap both kids are in jwks, after retired_at + overlap only the new one; the retired key's seed is still in the file before retired_at and gone after the next prune past retired_at; revoke removes a key at once and refuses the last signing key; a malformed file after the first load keeps the last good set; a second KeySet on the same file sees a rotation (inode/mtime change); two threads calling rotate leave both new keys in the file. Extend tests/unit/test_daemon_main.py with a TestRotateUpstreamClaimKey class copying the shape of TestRevokeScopedTokens: output text, exit codes, and that main() runs the flags only after check_runtime_identity.
      6. Stop with status=blocked if org_dir() cannot be resolved without a running server, or secure_files.atomic_write_json does not set mode 0600.
    acceptance:
      - "python3 -m pytest tests/unit/test_upstream_claim_keys.py -q passes"
      - "python3 -m pytest tests/unit/test_daemon_main.py -q -k RotateUpstreamClaimKey passes"
      - "ruff check src/privacyfence/upstream_claim_keys.py src/privacyfence/daemon_main.py passes"
  - id: p2-membership-store
    title: The group membership store, resolvers and approvals
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/group_membership.py
      - tests/unit/test_group_membership.py
      - tests/conftest.py
    brief: |
      Read first: src/privacyfence/secure_files.py, src/privacyfence/scoped_tokens.py (get_store, set_store, current_store pattern at the end of the file), tests/conftest.py (_reset).
      Design: plan sections 3.5 and 3.7.
      1. Create src/privacyfence/group_membership.py. Constants: UNRESTRICTED_AUDIENCE = "all", FILENAME = "group_membership.json", MAX_GROUPS = 500, MAX_ENTRIES = 20000, MAX_PENDING = 500, WINDOW_SECONDS = 86400, DEFAULT_MAX_ADDITIONS = 20. Define GROUP_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._+-]{0,63}@[a-z0-9.-]{1,253}") and AUDIENCE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}") as public module constants (phase 9 reuses them); an email has exactly one "@" and no whitespace.
      2. Dataclasses (frozen): Pending(group, audience, emails: tuple[str, ...]); SnapshotResult(status: "applied"|"rejected", added: int, removed: int, parked: int, ignored_groups: tuple[str, ...], reason: str).
      3. class MembershipStore(path: Path) with a threading.Lock and atomic writes through secure_files.atomic_write_json; state keys members, approvals, declined, history, updated_at as in plan 3.7; methods apply_snapshot(groups, *, group_audiences: Mapping[str, frozenset[str]], now: float, max_additions: int) -> SnapshotResult implementing steps 1-5 of plan 3.7 exactly (rejection reasons: "invalid_snapshot", "too_many_additions", "too_many_pending"; the first snapshot ever, updated_at None, is exempt from the addition cap); pending(group_audiences) -> list[Pending] sorted by group then audience; approve(group, audience, email=None, *, group_audiences, now) -> int and decline(...) -> int (count changed; only for a current member and an audience the map names; email None means every pending person of that group and audience); members_of(email) -> frozenset[str] of groups; last_rejected() -> dict|None (the reason and time of the latest rejected snapshot, kept in the file under last_rejected); updated_at property.
      4. Protocol MembershipResolver with audiences_for(email: str, group_audiences: Mapping[str, frozenset[str]]) -> frozenset[str]. SnapshotResolver(store): for each group the email is a member of, for each audience of that group in group_audiences, grant it when it is UNRESTRICTED_AUDIENCE or an approval for (email, group, audience) exists. StaticFileResolver(path): reads {"version": 1, "groups": {...}} (lower-cases emails), re-reads when (st_ino, st_mtime_ns, st_size) changes, keeps the last good data on a malformed file with logger.warning, grants every audience of every group the email is in. Both return frozenset() for an empty email.
      5. Module-level get_store() / set_store(store | None) / current_store() like scoped_tokens (get_store builds MembershipStore(paths.org_dir() / FILENAME)). Add set_store(None) to tests/conftest.py _reset next to scoped_tokens.set_store(None).
      6. Tests tests/unit/test_group_membership.py (tmp_path, explicit now): the bootstrap snapshot is exempt from max_additions; an addition to an "all"-only group applies and grants "all" at once; an addition to a "mgmt" group is parked (parked == 1, resolver grants no "mgmt"), approve then grants it, a removal at once removes the audience and its approval; a snapshot over max_additions within 24 h is rejected and nothing changes; an unmapped group is ignored and not stored (ignored_groups); mapping it later makes its members appear on the next snapshot as additions; a pending person absent from the next snapshot is no longer pending; an approval does not survive the member leaving and rejoining; an approval for audience "mgmt" does not grant "finance" when the map later adds "finance" (finance is pending); more than 500 pending rejects; malformed input (no "@", non-list members, too many groups) is rejected with invalid_snapshot; StaticFileResolver reload and malformed-file behaviour; empty email gives frozenset().
    acceptance:
      - "python3 -m pytest tests/unit/test_group_membership.py -q passes"
      - "ruff check src/privacyfence/group_membership.py tests/conftest.py passes"
  - id: p3-audit-fields
    title: Audit entry fields for forwarded upstream calls
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/audit_log.py
      - tests/unit/test_audit_log.py
    brief: |
      Read first: src/privacyfence/audit_log.py (AuditEntry, CURRENT_SCHEMA_VERSION and its history comment, export_week_to_excel HEADERS, COL_WIDTHS and the row writer). Pattern: the last schema bump, "7 -- approval linkage (ADR 0093): + expired_decision", and its tests (TestDenyFeedbackFields in tests/unit/test_audit_log.py).
      1. Append to AuditEntry, after expired_decision and before the hash-chain block, these defaulted fields with a one-line comment each: claim_jti: str = "", claim_sub: str = "", claim_principal_type: str = "", claim_audiences: list[str] = field(default_factory=list), claim_act: dict[str, str] = field(default_factory=dict), claim_req: str = "", upstream_status: str = "", metadata: dict[str, str] = field(default_factory=dict). Add the decision "upstream_forwarded" (and, for later phases, group_membership_applied, group_membership_rejected, group_membership_denied, group_membership_approved, group_membership_declined) to the decision comment list, in the style of runtime_credential_*.
      2. Set CURRENT_SCHEMA_VERSION = 8 and add the history line "8 -- upstream identity claim (ADR 0288): + claim_jti, claim_sub, claim_principal_type, claim_audiences, claim_act, claim_req, upstream_status, metadata".
      3. Excel export: append five columns at the end in this order: "Claim jti", "Claim sub", "Principal type", "Audiences" (", ".join), "Upstream status", with a COL_WIDTHS entry each and the row values in the row writer.
      4. The decision strings are listed nowhere else in src (grep -rn "runtime_credential_status_read" src finds only audit_log.py's comment and web/mcp_dispatch.py where it is emitted), so only the AuditEntry comment of step 1 names them; no other file needs a change. If a render table or classifier of decisions turns up that does name them, stop with status=blocked.
      5. Tests in tests/unit/test_audit_log.py, copying the TestDenyFeedbackFields shape: schema_version is 8 on a new entry; an old line without the new keys loads with the defaults; a chain mixing an old and a new line verifies; editing each new field breaks the chain (the existing parametrised test must cover the new fields: make make_entry() set a non-default value for each, including a dict and a list); the Excel export has the five new header cells in order.
    acceptance:
      - "python3 -m pytest tests/unit/test_audit_log.py -q passes"
      - "grep -n 'CURRENT_SCHEMA_VERSION = 8' src/privacyfence/audit_log.py matches"
  - id: p4-client-id-context
    title: The OAuth client id as a call-scoped value
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/agent_identity.py
      - src/privacyfence/web/routes_mcp.py
      - tests/unit/test_agent_identity.py
      - tests/unit/web/test_routes_mcp.py
    brief: |
      Read first: src/privacyfence/agent_identity.py (the _agent_ctx contextvar, agent_scope, current_agent), src/privacyfence/web/routes_mcp.py (handle_call_tool around the with-statement that enters principal_scope, agent_scope and token_scope).
      1. In agent_identity.py add a ContextVar _client_id_ctx (default ""), a function current_client_id() -> str and a context manager class client_id_scope(client_id: str), written in the same style as agent_scope. The module docstring gets one sentence saying the value is the raw OAuth client id and is for upstream claims only.
      2. In routes_mcp.py handle_call_tool, add client_id_scope(access_token.client_id if access_token else "") to the with-tuple that already holds agent_scope(agent).
      3. Tests: tests/unit/test_agent_identity.py: default "", set and restored by the scope, nested scopes. In the tests/unit/web test file that already drives handle_call_tool (find it with grep -ln "handle_call_tool" tests/unit/web), add a test that a connector tool sees the client id of the AccessToken during the call and "" after it.
      4. Stop with status=blocked if handle_call_tool has no access_token in scope at the with-statement.
    acceptance:
      - "python3 -m pytest tests/unit/test_agent_identity.py -q passes"
      - "grep -n 'client_id_scope' src/privacyfence/web/routes_mcp.py matches"
  - id: p5-claim-core
    title: The v1 claim, canonical request hash and signer
    depends_on: [p1-claim-keys]
    complexity: M
    touches:
      - src/privacyfence/identity_claim.py
      - contrib/verifier/req_test_vectors.json
      - tests/unit/test_identity_claim.py
    brief: |
      Read first: src/privacyfence/upstream_claims.py (the module being replaced; leave it untouched in this phase), src/privacyfence/upstream_claim_keys.py (phase 1), plan sections 3.1 to 3.3.
      1. Create contrib/verifier/req_test_vectors.json with exactly this content (the values were computed independently of any implementation; do not regenerate them from the code under test):
         {"version": 1, "description": "Canonical JSON of {tool, arguments}: keys sorted by Unicode code point, separators , and : with no whitespace, non-ASCII written as UTF-8 (not escaped), NaN and Infinity rejected. req = base64url without padding of SHA-256 over the UTF-8 bytes.", "vectors": [
         {"name": "empty-arguments", "tool": "search", "arguments": {}, "canonical": "{\"arguments\":{},\"tool\":\"search\"}", "req": "_jS9stFeCkVA2Ed7FqOzjA5kl4RdO11hSbb_QDrlYEw"},
         {"name": "key-order-is-sorted", "tool": "search", "arguments": {"query": "policy", "limit": 5}, "canonical": "{\"arguments\":{\"limit\":5,\"query\":\"policy\"},\"tool\":\"search\"}", "req": "L1g2NbgUryfvpWA8DUp2BKfiUaA39GNhc2N-7LbSmaI"},
         {"name": "nested-and-arrays", "tool": "get_doc", "arguments": {"filters": {"b": [3, 1, 2], "a": null}, "id": "d-42"}, "canonical": "{\"arguments\":{\"filters\":{\"a\":null,\"b\":[3,1,2]},\"id\":\"d-42\"},\"tool\":\"get_doc\"}", "req": "77eDoXOlibw6zBWIRyXkbHv9q8uxz-RlZvQN0u_N85w"},
         {"name": "unicode-not-escaped", "tool": "search", "arguments": {"query": "Größe café 日本語"}, "canonical": "{\"arguments\":{\"query\":\"Größe café 日本語\"},\"tool\":\"search\"}", "req": "opa6QE9p8-IKxdFU_PDTAeX0wSPlUiO2_Pj8iQXi12A"},
         {"name": "astral-and-escapes", "tool": "search", "arguments": {"query": "line\nbreak \"quoted\" \\ 😀"}, "canonical": "{\"arguments\":{\"query\":\"line\\nbreak \\\"quoted\\\" \\\\ 😀\"},\"tool\":\"search\"}", "req": "fBlyH6L_1srMHjsC-c6KcTLGRwLYc7XmaqdElibG-9Y"},
         {"name": "numbers-and-booleans", "tool": "list", "arguments": {"n": 0, "neg": -7, "f": 1.5, "big": 12345678901234567890, "ok": true, "no": false}, "canonical": "{\"arguments\":{\"big\":12345678901234567890,\"f\":1.5,\"n\":0,\"neg\":-7,\"no\":false,\"ok\":true},\"tool\":\"list\"}", "req": "j0MCtwK9UI17zZyw2aimBWRq8vxDl5ODknEIYcJkp0M"}]}
         After writing, run a one-off python3 snippet that recomputes each req from "canonical" with hashlib and checks it equals the file's value; if one differs, stop with status=blocked.
      2. Create src/privacyfence/identity_claim.py (docstring first). Constants and payload per plan 3.1. Functions canonical_request(tool: str, arguments: Mapping[str, Any]) -> bytes and request_hash(tool, arguments) -> str exactly as plan 3.2. Frozen dataclass IdentityCaller(sub: str, principal_type: str, email: str | None = None, audiences: tuple[str, ...] = (), tools: tuple[str, ...] = (), approved_by: str | None = None, token_id: str | None = None, act: Mapping[str, str] | None = None). class CallerNotAllowedError(Exception). class ClaimSigningError(Exception).
      3. class ClaimSigner(*, issuer: str, keys: KeySet, clock=time.time) with jwks() -> dict (keys.jwks()) and mint(*, audience: str, caller: IdentityCaller, req: str | None, lifetime: int, now: float | None = None) -> MintedClaim. MintedClaim is a frozen dataclass(token: str = field(repr=False), jti: str, req: str | None). mint builds the payload of plan 3.1 (omit keys that are None or empty except audiences and tools, which are always present), sorts audiences and tools, raises ValueError when lifetime is outside 5..60, and wraps any KeySetError or signing failure in ClaimSigningError.
      4. class IdentityClaimAuth(httpx2.Auth) with requires_request_body = True; __init__(signer, audience, caller, lifetime). auth_flow(request) parses request.content as JSON (an unparseable or non-dict body, or a list, counts as "not a call"); for method "tools/call" with str params.name it sets req = request_hash(params.name, params.arguments or {}); otherwise it uses a copy of the caller with audiences=() and tools=() and req None. It sets request.headers[CLAIM_HEADER] and never touches Authorization. It records last_call_jti and last_call_req (only for tools/call requests) and increments minted_count. A ClaimSigningError propagates.
      5. def load_signer(issuer_url: str, *, overlap_seconds: int) -> ClaimSigner: builds KeySet(paths.org_dir() / upstream_claim_keys.FILENAME, overlap_seconds=...), calls ensure(), returns ClaimSigner(issuer=issuer_url.rstrip("/"), keys=...).
      6. Tests tests/unit/test_identity_claim.py: every vector's canonical bytes and req equal the file's values (load the JSON in the test); header and payload of a minted claim for each principal_type (claude_client with act, n8n_workflow with token_id and approved_by and no email, service); keys omitted when empty; lifetime outside 5..60 raises; IdentityClaimAuth with a tools/call body sets req for that body and keeps audiences, with an initialize body strips audiences and tools and sets no req, with a list body strips them, never sets Authorization and keeps an existing Authorization header unchanged; two requests get different jti; ClaimSigningError when the key set has no usable key; MintedClaim repr does not contain the token; after KeySet.rotate (injected clock) a claim minted before the signing switch verifies against the jwks and one minted after verifies against the new key (use jwt.decode with the jwks x via cryptography, not the code under test).
      7. Stop with status=blocked if httpx2.Auth does not read the body when requires_request_body is True (check with a tiny httpx2.AsyncClient and a MockTransport in the test).
    acceptance:
      - "python3 -m pytest tests/unit/test_identity_claim.py -q passes"
      - "python3 -c \"import json; v=json.load(open('contrib/verifier/req_test_vectors.json')); assert len(v['vectors'])==6\" exits 0"
      - "ruff check src/privacyfence/identity_claim.py passes"
  - id: p6-verifier
    title: The vendorable verifier and its tests
    depends_on: [p5-claim-core]
    complexity: M
    touches:
      - contrib/verifier/pf_claim_verify.py
      - tests/unit/test_pf_claim_verify.py
    brief: |
      Read first: contrib/verifier/req_test_vectors.json, src/privacyfence/identity_claim.py (phase 5), plan section 3.10 and 3.2.
      1. Create contrib/verifier/pf_claim_verify.py: module docstring with a usage example; from __future__ import annotations; stdlib, jwt and cryptography only; syntax valid on Python 3.9 (no match statements, no X | Y outside annotations, no dataclass(slots=True)). Implement exactly the API of plan 3.10: CLAIM_VERSION, CLAIM_TYPE, CLAIM_HEADER, MAX_LIFETIME_SECONDS, canonical_request, request_hash (same bytes as the PrivacyFence side, written again here so the file stands alone), Principal, ClaimError(code, message), InMemoryReplayCache, ClaimVerifier.
      2. ClaimError codes (the .code string): malformed, bad_algorithm, bad_type, unknown_key, bad_signature, bad_issuer, bad_audience, expired, not_yet_valid, bad_lifetime, bad_version, bad_principal_type, bad_body, batch_not_allowed, request_required, request_mismatch, request_unexpected, replayed.
      3. verify(token, *, body=b"") steps in this order: header parse (bad_algorithm unless alg is exactly "EdDSA", bad_type unless typ is CLAIM_TYPE, unknown_key unless kid is in the JWKS after at most one refetch); signature and registered claims with jwt.decode(token, key, algorithms=["EdDSA"], audience=..., issuer=..., leeway=leeway, options={"require": ["exp", "iat", "iss", "aud", "sub", "jti"]}) mapping PyJWT exceptions to the codes; leeway above 5 raises ValueError in the constructor; bad_lifetime when exp - iat > 60 or <= 0; bad_version unless claim_version == 1; bad_principal_type unless one of claude_client, n8n_workflow, service, human; audiences and tools must be lists of strings (else malformed). Body: parse body with json.loads(object_pairs_hook=reject duplicate keys, raising bad_body); a list raises batch_not_allowed; an empty body or a non-tools/call method is "not a call"; a tools/call with params.name str and params.arguments dict or absent is a call. For a call the claim must carry req equal to request_hash(name, arguments), else request_required (missing) or request_mismatch (different). For not a call, a claim that carries req raises request_unexpected, and the returned Principal has audiences=() and tools=(). Replay last: replay_cache.add_if_new(jti, exp + leeway) false raises replayed.
      4. verify_tool_call(token, tool, arguments) does the same with the call given directly (req required and compared). JWKS: fetch defaults to urllib.request.urlopen(url, timeout=5).read(); cache jwks_ttl seconds; an unknown kid refetches at most once per 30 seconds; only keys with kty OKP and crv Ed25519 are used, loaded with Ed25519PublicKey.from_public_bytes(base64url x). jwks may instead be passed as a dict (static) through a jwks= parameter; exactly one of jwks_url and jwks is required.
      5. Tests tests/unit/test_pf_claim_verify.py: import the module by path with importlib.util.spec_from_file_location (pattern: tests/unit/test_upstream_mcp_reference_server.py). Mint tokens in the test with cryptography and jwt only (not with PrivacyFence code) from a fixed Ed25519 key, so the verifier is tested against the written contract. Cover: a valid call claim returns the typed Principal; each of these is rejected with its code: wrong signature (token signed by another key), tampered payload, expired (injected clock), wrong aud, wrong iss, wrong typ, alg none and HS256 and RS256, unknown kid after one refetch, lifetime 61, claim_version 2, a replay of the same jti; a claim reused with different arguments is request_mismatch; a tools/call without req is request_required; a req on an initialize body is request_unexpected; a body that is a JSON list is batch_not_allowed; duplicate keys in the body are bad_body; a not-a-call body returns audiences (); both keys verify during a rotation (JWKS with two keys, the claim signed by each) and a key removed from the JWKS is unknown_key after the cache expires and one refetch; the unknown-kid refetch happens at most once per 30 seconds; replay_cache=None allows a repeat; every vector of contrib/verifier/req_test_vectors.json hashes as listed. One more test mints a claim with PrivacyFence's own identity_claim.ClaimSigner and verifies it with this module (the cross-check that both sides agree).
      6. Stop with status=blocked if PyJWT cannot decode an EdDSA token with a cryptography Ed25519 public key object.
    acceptance:
      - "python3 -m pytest tests/unit/test_pf_claim_verify.py -q passes"
      - "python3 -c \"import ast,sys; ast.parse(open('contrib/verifier/pf_claim_verify.py').read(), feature_version=(3,9))\" exits 0"
      - "ruff check contrib/verifier tests/unit/test_pf_claim_verify.py passes"
  - id: p7-reference-server-both
    title: The reference upstream also accepts the v1 claim
    depends_on: [p6-verifier]
    complexity: M
    touches:
      - scripts/upstream_mcp_reference_server.py
      - tests/unit/test_upstream_mcp_reference_server.py
    brief: |
      Read first: scripts/upstream_mcp_reference_server.py, tests/unit/test_upstream_mcp_reference_server.py, contrib/verifier/pf_claim_verify.py.
      Purpose: the PrivacyFence side still sends the old claim until phase 10, so this phase adds the new path and keeps the old one; phase 12 removes the old one.
      1. Load contrib/verifier/pf_claim_verify.py in the script by path (importlib.util.spec_from_file_location, relative to Path(__file__).parents[1] / "contrib" / "verifier").
      2. Add an ASGI middleware step: when the request has the X-PrivacyFence-Claim header, buffer the request body (at most 1 MiB, else 413 text "request too large"), verify with ClaimVerifier.verify(token, body=body), put this dict in the existing claims contextvar: {"sub": principal.sub, "domains": list(principal.audiences), "act": {"type": "service" if principal.principal_type == "service" else "ai_client"}} (the keys the old filter reads), replay the buffered body to the app through a wrapped receive, answer 401 with no detail on ClaimError. A request without that header keeps the old Authorization path unchanged.
      3. The issuer of the new path is the --issuer value with a trailing "/upstream-mcp" removed when present (the old path keeps comparing the full value). New CLI options --audience-claim-jwks-url / --audience-claim-jwks-file are not added: reuse the existing --jwks-url and --jwks-file options for the new path as well (the key set is the Ed25519 JWKS when the new header is used; document in the --help text that the old path wants RSA keys and the new path Ed25519 keys until the old path is removed).
      4. The documents keep the field "domain" for now (renamed in phase 12); the filter treats claim audiences as the allowed values of that field.
      5. Tests: a valid new-format claim returns only documents of its audiences; a tools/call claim with other arguments is refused with 401; a claim for initialize carries no audiences and lists no documents; a list body is refused; the old header path still passes its existing tests unchanged.
      6. Stop with status=blocked if buffering the body breaks the Streamable HTTP session manager (the existing session tests fail after the change).
    acceptance:
      - "python3 -m pytest tests/unit/test_upstream_mcp_reference_server.py -q passes"
  - id: p8-audience-rename
    title: Rename data domains to audiences in tokens, forms and the runtime
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/scoped_token_proposals.py
      - src/privacyfence/runtime_credentials.py
      - src/privacyfence/upstream_mcp_runtime.py
      - src/privacyfence/web/server.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_scoped_token_proposals.py
      - tests/unit/test_runtime_credentials.py
      - tests/unit/test_upstream_mcp_runtime.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/unit/web/test_server.py
      - tests/unit/web/test_server_org_mode.py
      - tests/integration/test_settings_scoped_token_form_browser.py
    brief: |
      Read first: plan section 3.6. This phase changes names and user-visible words only; behaviour, the grants and the old claim stay as they are. Grants keep their own JSON key "domains" inside admin_config.json until phase 9 replaces them.
      1. Run grep -rnE "upstream_domains|held_domains|stDomains|pf-st-domains|data-st-domains-for|Data domains|scopedChosenDomains|\.domains" src tests and rename, in the files of this phase only, per this map: ScopedToken.upstream_domains -> upstream_audiences (the dataclass field, the _to_json/_from_json key, _normalise_upstream_domains -> _normalise_upstream_audiences, _upstream_domains_from_json -> _upstream_audiences_from_json, the create() parameters upstream_domains -> upstream_audiences and held_domains -> held_audiences, the error text "You can only give a token domains you have: <x>" -> "You can only give a token audiences you have: <x>"); upstream_mcp_runtime.held_domains -> held_audiences (inside caller_for the lookup of scope.upstream_domains reads scope.upstream_audiences); the request payload key upstream_domains -> upstream_audiences in routes_settings.py, server.py (the "domains" list of each upstream row in scoped_token_page_data -> "audiences") and settings_window_html.py (JS names ui.stDomains -> ui.stAudiences, the data attribute data-st-domains-for -> data-st-audiences-for, the class pf-st-domains -> pf-st-audiences, legend "Data domains for <label>" -> "Audiences for <label>", hint text "The token sees only the audiences you tick, and never more than you can see yourself."); runtime_credentials.py and scoped_token_proposals.py kwargs and the refusal text "Upstream tools cannot be proposed; create the token in Settings." stays unchanged.
      2. Do not rename anything in upstream_mcp_config.py, upstream_claims.py or the reference server. In caller_for the CallerClaim(domains=...) keyword stays; only its inputs are renamed.
      3. Update every test of the files above to the new names (the grep above lists them); the integration browser test only needs its selectors changed. A token JSON file with the old key upstream_domains loads with no audiences (add one test for that in tests/unit/test_scoped_tokens.py).
      4. Stop with status=blocked if a rename would need a change in a file outside this phase's touches.
    acceptance:
      - "grep -rnE 'ui\\.stDomains|upstream_domains|held_domains' src tests returns no match"
      - "python3 -m pytest tests/unit -q passes"
      - "ruff check . passes"
  - id: p9-groups-config
    title: Operator keys and group-to-audience maps replace the grants
    depends_on: [p8-audience-rename, p2-membership-store, p1-claim-keys]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_config.py
      - src/privacyfence/upstream_mcp_runtime.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/admin_config.py
      - tests/unit/test_upstream_mcp_config.py
      - tests/unit/test_upstream_mcp_runtime.py
      - tests/unit/test_daemon_main.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/web/test_server_org_mode.py
      - tests/integration/test_upstream_mcp_org.py
    brief: |
      Read first: src/privacyfence/upstream_mcp_config.py, src/privacyfence/upstream_mcp_runtime.py, src/privacyfence/group_membership.py (phase 2), plan sections 3.3 and 3.5.
      1. upstream_mcp_config.py: add to UpstreamServer the fields identity_claim: str = "required", claim_lifetime_seconds: int = 60, allowed_principal_types: tuple[str, ...] = ("claude_client", "n8n_workflow") (defaults so existing constructors keep working) and parse the three keys in _parse_one with the validation of plan 3.3 (error messages in the existing style, "<source>: <path>: <problem>"); add them to _UPSTREAM_KEYS. Add frozen dataclasses ClaimSettings(key_overlap_hours: int = 24) and MembershipSettings(source: str = "snapshot", file: str | None = None, max_additions: int = 20); add the top-level operator keys "claim" and "membership" (validated: claim.key_overlap_hours 1..720; membership.source snapshot or static, file required for static and refused for snapshot, max_additions 1..1000; unknown keys refused) and a function parse_settings(data) -> tuple[ClaimSettings, MembershipSettings] plus load_settings(path). parse_upstreams must accept "claim" and "membership" as known top-level keys; its return type stays tuple[UpstreamServer, ...].
      2. Delete Grant, _grant_match_ok, _parse_grants, _parse_grant, parse_grants, domains_for, GrantStore and their constants. Add class GroupMapStore(path, upstream_ids) with the same mtime-reload and keep-last-good behaviour as GrantStore, reading admin_config.json upstream_mcp.<id>.groups per plan 3.5 (group_membership.GROUP_NAME_RE and group_membership.AUDIENCE_RE, at most 200 groups, at most 20 audiences per group; an unknown key other than "groups" is refused; the key "grants" is refused with the text "grants were replaced by groups"; an undeclared upstream id is logged once and ignored as before) and a method group_audiences(upstream_id) -> dict[str, frozenset[str]].
      3. upstream_mcp_runtime.py: _Runtime holds groups: GroupMapStore and resolver: MembershipResolver instead of grants; configure(...) takes groups= and resolver= in place of grants=; add function audiences_for_email(upstream_id, email, verified) -> frozenset[str] (empty when not verified, else resolver.audiences_for(email, groups.group_audiences(upstream_id))), used by caller_for and held_audiences in place of the grant lookup. caller_for still builds the old CallerClaim with domains=tuple(sorted(audiences)); the claim format changes in phase 10.
      4. daemon_main.py _configure_upstream_mcp: load ClaimSettings and MembershipSettings with load_settings, build GroupMapStore(admin_config path, ids) and the resolver (StaticFileResolver(Path(settings.file)) for "static", else SnapshotResolver(group_membership.get_store())) and pass them to upstream_mcp_runtime.configure.
      5. admin_config.py: the comment near line 156 describes the upstream_mcp section as holding grants; change it to say groups. Nothing else in the file changes.
      6. Update the tests named in touches: grant tests become group-map tests (hot reload, last good kept, bad section message names upstream_mcp.<id>.groups, "grants" refused), operator-key validation tests (each new key, its default, each bad value), settings parsing, and runtime tests where a person's audiences come from a static resolver and a snapshot resolver (verified email only; unverified or empty gives no audiences; a token gets owner-now ∩ chosen).
      7. Move the other users of the old names to the new ones: tests/unit/web/test_mcp_dispatch.py (around lines 991 and 1006), tests/unit/web/test_server_org_mode.py (around lines 802 and 815) and tests/integration/test_upstream_mcp_org.py (around lines 30 and 119) build GrantStore and pass grants= to configure; they now build a GroupMapStore on a tmp admin_config.json holding upstream_mcp.<id>.groups and pass resolver=StaticFileResolver(<tmp membership file>), groups=. The claim these tests exercise is still the old one.
      8. Stop with status=blocked if a test outside this phase's touches needs grants to pass.
    acceptance:
      - "python3 -m pytest tests/unit/test_upstream_mcp_config.py tests/unit/test_upstream_mcp_runtime.py -q passes"
      - "grep -rn 'GrantStore' src tests returns no match"
      - "python3 -m pytest tests/unit -q passes"
  - id: p10-claim-switch
    title: Send the v1 claim instead of the old one
    depends_on: [p5-claim-core, p7-reference-server-both, p9-groups-config, p4-client-id-context]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_client.py
      - src/privacyfence/connectors/upstream_mcp.py
      - src/privacyfence/upstream_mcp_runtime.py
      - src/privacyfence/web/routes_upstream_mcp.py
      - src/privacyfence/daemon_main.py
      - tests/unit/test_upstream_mcp_client.py
      - tests/unit/connectors/test_upstream_mcp_connector.py
      - tests/unit/test_upstream_mcp_runtime.py
      - tests/unit/test_daemon_main.py
      - tests/unit/web/test_routes_upstream_mcp.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/web/test_server_org_mode.py
    brief: |
      Read first: src/privacyfence/identity_claim.py (phase 5), src/privacyfence/upstream_mcp_runtime.py, src/privacyfence/upstream_mcp_client.py, src/privacyfence/connectors/upstream_mcp.py, plan sections 3.1 and 3.3.
      1. upstream_mcp_runtime.py: caller_for(upstream) now returns an identity_claim.IdentityCaller. Person (no token scope): sub = principal.id, principal_type "claude_client", email = principal.email only when principal.email_verified, audiences = audiences_for_email(...), act = {"sub": current_client_id()} plus "agent": current_agent().id when current_agent().is_attested(); the "sub" key only when current_client_id() is non-empty. Scoped token: sub = "n8n-token:" + scope.id, principal_type "n8n_workflow", token_id = scope.id, approved_by = scope.principal_id, no email, no act, audiences = owner's current audiences (from scope.principal_email and scope.principal_email_verified) intersected with scope.upstream_audiences.get(upstream.id, ()). tools: the upstream-native names (declared name without the "<id>_" prefix) of the exposed tools of that upstream (entry.catalog.specs()); for a token only those whose exposed name is in scope.tools. If upstream.allowed_principal_types does not contain the principal_type, raise identity_claim.CallerNotAllowedError. Remove the CallerClaim/domains code and the upstream_claims import; held_audiences stays. configure(...) takes signer: identity_claim.ClaimSigner.
      2. upstream_mcp_client.py: UpstreamMcpClient builds IdentityClaimAuth(signer, upstream.audience, caller, upstream.claim_lifetime_seconds) when upstream.identity_claim == "required" and httpx2 auth None when "off". list_tools signs a service IdentityCaller(sub="privacyfence", principal_type="service"). call_tool(tool, arguments, caller) returns CallResult with two new fields jti: str = "" and req: str = "" taken from auth.last_call_jti / last_call_req. A ClaimSigningError inside _run becomes UpstreamMcpClientError("claim_failed") (catch it before the generic except). Replace the CallerClaim import by IdentityCaller.
      3. connectors/upstream_mcp.py: the caller callable returns an IdentityCaller; _forward maps CallerNotAllowedError to a new UpstreamCallRefusedError(RuntimeError) with the text "PrivacyFence does not allow this caller to use the <label> server." (raised before any request) and the client kind "claim_failed" to UpstreamUnavailableError with the text "The <label> call was not sent: PrivacyFence could not sign the caller claim." Both are RuntimeError subclasses so safe_errors passes them. With identity_claim == "off" the connector does not call the caller at all.
      4. daemon_main.py: _configure_upstream_mcp builds the signer with identity_claim.load_signer(issuer_url, overlap_seconds=claim_settings.key_overlap_hours * 3600) and passes it on (the settings come from phase 9).
      5. Nothing may import src/privacyfence/upstream_claims.py any more (phase 12 deletes it with its test): web/routes_upstream_mcp.py changes its import to identity_claim.ClaimSigner and its docstring to name the Ed25519 key set; tests/unit/web/test_mcp_dispatch.py and tests/unit/web/test_server_org_mode.py (they import upstream_claims to build a signer, around lines 990 and 801) build identity_claim.ClaimSigner on a tmp upstream_claim_keys.KeySet instead; tests/unit/web/test_routes_upstream_mcp.py asserts the OKP entries. tests/unit/test_upstream_mcp_reference_server.py and the integration test keep using the old module until phase 12.
      6. Tests: update the client, connector, runtime and daemon tests to the new types; add: a person's call sends X-PrivacyFence-Claim and no Authorization, with audiences and tools; a scoped-token call has sub "n8n-token:<id>", principal_type "n8n_workflow", approved_by, no email; the initialize request of the same call carries no audiences; a not-allowed principal type is refused before any request is made; identity_claim "off" sends no header; a signing failure sends nothing and gives the claim_failed text; unresolved membership (no verified email) still forwards with audiences []; a token's audiences shrink at the next claim (with a SnapshotResolver over a tmp store, a snapshot removes the token creator from the mgmt group and the next caller_for for that token has no mgmt, then a later approval brings it back); a token created with an audience its creator does not hold is refused (the create-refusal test in tests/unit/test_scoped_tokens.py, the one that asserts \"You can only give a token audiences you have\", must still pass).
      7. Stop with status=blocked if tests/unit/test_upstream_mcp_client.py cannot run against the reference server of phase 7 with the new header.
    acceptance:
      - "grep -rln 'upstream_claims' src returns only src/privacyfence/upstream_claims.py"
      - "python3 -m pytest tests/unit/test_upstream_mcp_client.py tests/unit/connectors/test_upstream_mcp_connector.py tests/unit/test_upstream_mcp_runtime.py tests/unit/test_identity_claim.py -q passes"
      - "python3 -m pytest tests/unit -q passes"
  - id: p11-forward-audit
    title: One audit entry for every forwarded call
    depends_on: [p10-claim-switch, p3-audit-fields]
    complexity: S
    touches:
      - src/privacyfence/connectors/upstream_mcp.py
      - tests/unit/connectors/test_upstream_mcp_connector.py
    brief: |
      Read first: src/privacyfence/connectors/upstream_mcp.py (_forward, _auto_audit), src/privacyfence/audit_log.py (the new fields), plan section 3.9.
      1. In _forward, after the outcome is known (success, UpstreamMcpClientError of each kind, CallerNotAllowedError, claim failure) and before returning or raising, write exactly one AuditEntry with decision "upstream_forwarded", connector self.name, tool declared.exposed_name, tool_name f"{upstream.label}: {declared.name}", summary f"Forwarded {declared.name} to {upstream.label}", sender upstream.label, auto_accept_rule "", request_id "", latency_seconds, claude_reason current_reason(), claim_jti/claim_sub/claim_principal_type/claim_audiences/claim_act/claim_req from the IdentityCaller and the call result (empty when the caller was never resolved or identity_claim is "off"), upstream_status one of ok, tool_error, unreachable, too_large, refused, claim_failed, and metadata containing only upstream (the id), claim_version "1", identity_claim ("required" or "off"), and token_id and approved_by when set. Wrap the write in the same try/except-log pattern as _auto_audit; an audit failure never changes the call outcome. Never put the token, an argument value or upstream text in any field or log line.
      2. Keep _auto_audit (the auto_accepted entry is the policy record).
      3. Tests: one test per upstream_status value asserting the entry's fields; a test that no field of any recorded entry and no captured log record contains the claim token string (capture the token through the transport the test client uses) or an argument value; review and popup gates write the forwarded entry in addition to the gated_call entry; an audit write that raises does not change the result.
    acceptance:
      - "python3 -m pytest tests/unit/connectors/test_upstream_mcp_connector.py -q passes"
      - "grep -n 'upstream_forwarded' src/privacyfence/connectors/upstream_mcp.py matches"
  - id: p12-reference-server-final
    title: The reference upstream speaks only the v1 claim; the old claim module goes
    depends_on: [p10-claim-switch]
    complexity: M
    touches:
      - scripts/upstream_mcp_reference_server.py
      - src/privacyfence/upstream_claims.py
      - tests/unit/test_upstream_claims.py
      - tests/unit/test_upstream_mcp_reference_server.py
      - tests/unit/test_upstream_mcp_client.py
      - tests/integration/test_upstream_mcp_org.py
    brief: |
      Read first: scripts/upstream_mcp_reference_server.py, tests/integration/test_upstream_mcp_org.py, tests/unit/test_upstream_mcp_client.py.
      1. Remove the old Authorization/RS256 path and its _Verifier class, the --public-key-file option and the RSAAlgorithm import; the new path becomes the only one, with the options --audience, --issuer (the PrivacyFence issuer URL, no suffix), --jwks-url and --jwks-file (Ed25519 JWKS). Update the module docstring and --help text.
      2. Rename the document field "domain" to "audience" (DEFAULT_DOCUMENTS and the filter) and the operator-visible texts accordingly.
      3. Update the three test files to the new claim (the integration test runs the reference server on loopback against a real organization server; its grants section becomes an upstream_mcp.<id>.groups map plus a static membership file named in the operator file's "membership" key). Keep every existing scenario (a person sees only their audiences, a token sees the intersection, a revoked audience stops within the lifetime, a service claim lists tools only).
      4. The tests of the reference server mint claims with identity_claim.ClaimSigner on a tmp KeySet instead of upstream_claims. Delete src/privacyfence/upstream_claims.py and tests/unit/test_upstream_claims.py last, once nothing imports them (grep -rn upstream_claims src tests scripts must return no match).
      5. Stop with status=blocked if the integration test cannot start the organization server with the new operator keys.
    acceptance:
      - "python3 -m pytest tests/unit/test_upstream_mcp_reference_server.py tests/unit/test_upstream_mcp_client.py -q passes"
      - "python3 -m pytest tests/integration/test_upstream_mcp_org.py -q passes"
      - "grep -rnE 'RSAAlgorithm|_Verifier|--public-key-file|upstream_claims' src tests scripts returns no match"
      - "python3 -m pytest tests/unit -q passes"
  - id: p13-membership-tool
    title: The privacyfence_update_group_membership tool
    depends_on: [p2-membership-store, p10-claim-switch, p3-audit-fields]
    complexity: M
    touches:
      - src/privacyfence/connectors/group_membership.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/upstream_mcp_runtime.py
      - src/privacyfence/auto_accept.py
      - scripts/generate_tools_reference.py
      - scripts/pyinstaller_common.py
      - tests/unit/connectors/test_group_membership_connector.py
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_website_connector_pages.py
      - tests/unit/test_pyinstaller_hidden_imports.py
      - tests/unit/test_upstream_mcp_runtime.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
    brief: |
      Read first: src/privacyfence/connectors/upstream_mcp.py (a connector with an input_schema ToolSpec), src/privacyfence/connector.py (ToolSpec), src/privacyfence/group_membership.py, plan sections 3.7 and 3.8. The registration rules come from tests/unit/policy/test_registry.py (an auto tool carries no verb or operation) and tests/unit/connectors/test_readme_manifest_alignment.py.
      1. Create src/privacyfence/connectors/group_membership.py: TOOL_NAME = "privacyfence_update_group_membership"; class GroupMembershipConnector(Connector) with name "privacyfence" (property), tool_specs() returning one ToolSpec(name=TOOL_NAME, description (under 1024 characters, saying it takes the full membership snapshot of every group, applies removals at once and holds additions to restricted audiences for an administrator, and works only with a scoped token), params=[], read_only=False, destructive=False, input_schema={"type": "object", "properties": {"groups": {"type": "object", "description": "Group address to the list of member email addresses; the full snapshot.", "additionalProperties": {"type": "array", "items": {"type": "string"}}}}, "required": ["groups"]}); its constructor takes no arguments and stores nothing (the catalogue builds cls(MagicMock()) and cls(None); accept an optional first argument and ignore it).
      2. call(tool, args): raise ValueError for another tool. If scoped_tokens.current_token_scope() is None or TOOL_NAME is not in its tools, write an AuditEntry decision "group_membership_denied" and raise RuntimeError("privacyfence_update_group_membership can only be called with a scoped token."). Otherwise run group_membership.get_store().apply_snapshot(args["groups"], group_audiences=upstream_mcp_runtime.all_group_audiences(), now=time.time(), max_additions=upstream_mcp_runtime.max_additions()) in a worker thread (asyncio.to_thread), write one AuditEntry with decision "group_membership_applied" or "group_membership_rejected" (summary with counts and the reason only, sender = the token label, connector "privacyfence", tool TOOL_NAME, auto_accept_rule "auto"), and return the SnapshotResult as a dict (ignored_groups a list). A rejected snapshot also logs logger.warning("Group membership snapshot rejected: %s", reason).
      3. upstream_mcp_runtime.py: add all_group_audiences() -> dict[str, frozenset[str]] (the union over upstreams of GroupMapStore.group_audiences; the same group in two upstreams merges its audience sets) and max_additions() -> int (MembershipSettings.max_additions; configure() gains a membership_max_additions: int = 20 parameter set from daemon_main).
      4. daemon_main.py build_connectors: after the upstream connectors block, add `if org_mode.resolve_mode(org_config) == "org": connectors.append(GroupMembershipConnector())` using the mode value already computed in that function (search the existing org-mode check and reuse it); pass membership_max_additions from the settings in _configure_upstream_mcp.
      5. Policy registration: in src/privacyfence/auto_accept.py add "privacyfence_update_group_membership": "auto" to the literal TOOL_TO_GATE dict (line 218), as the last entry, with the comment `# PrivacyFence administration`; not in TOOL_TO_VERB, TOOL_TO_OPERATION or EFFECT_BY_TOOL. Guard tests that a new connector module trips: add "group_membership" to NO_PAGE_MODULES in tests/unit/test_website_connector_pages.py, and add "privacyfence.connectors.group_membership" to the hidden-imports list in scripts/pyinstaller_common.py in the style of the entries around the other connector modules (tests/unit/test_pyinstaller_hidden_imports.py must pass; edit it only if it pins a count).
      6. scripts/generate_tools_reference.py: add CONNECTOR_TITLES["privacyfence"] = "PrivacyFence administration" and CONNECTOR_SHORT["privacyfence"] = "group membership sync"; run python3 scripts/generate_tools_reference.py and python3 scripts/generate_always_allow_reference.py and commit the regenerated docs/tools-reference.md and docs/always-allow-rules-reference.md. Add GroupMembershipConnector to CONNECTOR_CLASSES in tests/unit/connectors/test_readme_manifest_alignment.py.
      7. Tests tests/unit/connectors/test_group_membership_connector.py: a call without a token scope is refused and audited; with a token scope an addition to an "all" group applies, an addition to a "mgmt" group is parked (parked == 1), a removal applies at once, a snapshot over the cap is rejected and audited as group_membership_rejected, an unmapped group is reported in ignored_groups; the audit summaries contain no email address; tests.helpers.assert_all_tools_leave_an_audit_trail passes for the connector. Add a runtime test for all_group_audiences.
      8. "privacyfence" is already in BUILTIN_NAMES (upstream_mcp_config.py); that only stops an operator's upstream from using the id and is expected. The meta tools (privacyfence_status and the others) are not in TOOL_TO_GATE, so web/server.py _tool_connector cannot file them under the new connector. Stop with status=blocked only if a test in tests/unit fails because two connectors share the name "privacyfence".
    acceptance:
      - "python3 -m pytest tests/unit/connectors/test_group_membership_connector.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/policy/test_registry.py tests/unit/test_systemic_gate_invariants.py tests/unit/test_docs_tools_reference.py -q passes"
      - "python3 -m pytest tests/unit -q passes"
  - id: p14-token-admin-only
    title: Only an administrator can create a sync token
    depends_on: [p13-membership-tool]
    complexity: M
    touches:
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/scoped_token_proposals.py
      - src/privacyfence/runtime_credentials.py
      - src/privacyfence/web/routes_settings.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_scoped_token_proposals.py
      - tests/unit/test_runtime_credentials.py
      - tests/unit/web/test_routes_org_settings.py
    brief: |
      Read first: src/privacyfence/scoped_tokens.py (ScopedTokenStore.create), src/privacyfence/web/routes_settings.py (_create_and_deliver and the create route), plan section 3.8.
      1. scoped_tokens.py: ScopedTokenStore.create gains is_admin: bool = False. When the tool set contains connectors.group_membership.TOOL_NAME (import it inside the function to avoid an import cycle) and is_admin is false, raise ScopedTokenError("Only an administrator can create a token for privacyfence_update_group_membership.") before anything is stored.
      2. routes_settings.py: every call of store.create from the organization handlers passes is_admin=principal.is_admin (search "store.create(" and "_create_and_deliver"); the local-mode handler passes nothing.
      3. scoped_token_proposals.py: the existing refusal of upstream tools also refuses TOOL_NAME with the text "This tool cannot be proposed; create the token in Settings."; runtime_credentials.py passes is_admin=False where it calls create.
      4. Tests: create refuses for a non-admin and succeeds for an admin; the org create route returns the refusal text to a non-admin and creates for an admin; an AI proposal that includes the tool is refused; an expired or revoked sync token no longer reaches the tool (use the existing token-state helpers).
    acceptance:
      - "python3 -m pytest tests/unit/test_scoped_tokens.py tests/unit/test_scoped_token_proposals.py tests/unit/web/test_routes_org_settings.py -q passes"
      - "grep -n 'Only an administrator can create a token' src/privacyfence/scoped_tokens.py matches"
  - id: p15-admin-approvals
    title: The Admin card to approve or decline pending group additions
    depends_on: [p13-membership-tool]
    complexity: M
    touches:
      - src/privacyfence/web/routes_admin.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/web/test_routes_admin.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Read first: src/privacyfence/web/routes_admin.py (ADMIN_ACTIONS, ADMIN_PARAMS, ROUTE_CLASSIFICATION, _dispatch and the branch used by non-admin_config actions such as the privacy actions, _admin_state, _agent_runtimes_status), src/privacyfence/settings_window_html.py (renderRuntimes and the section switch), src/privacyfence/group_membership.py. Pattern for a small action: set_web_push in routes_admin.py and its tests in tests/unit/web/test_routes_admin.py (ACTION_BODIES, TestStepUp, TestRouteClassification).
      1. routes_admin.py: add actions approve_group_membership and decline_group_membership: AdminAction(_ORG_ONLY, True, "self"), params (("group", str, True), ("audience", str, True), ("email", str, False)). Each handler calls group_membership.get_store().approve / decline with upstream_mcp_runtime.all_group_audiences(), now=time.time(); email omitted or "" means everyone pending for that group and audience; it audits through _record_settings_audit with the text "Approved membership for <n> person(s) in <group> for audience <audience> (admin=<id>)" or "Declined ..." (counts and names only, no emails), and the reply is the full _admin_state. A count of 0 returns the refusal helper the other actions use (search _error( in routes_admin.py) with status 409 and the text "Nothing is waiting for that approval.". Both actions are sensitive, so the existing step-up gate applies. Add both to ADMIN_PARAMS and to the test ACTION_BODIES (ROUTE_CLASSIFICATION is keyed by route path and the single /api/admin/{action} route already covers them). In _dispatch add a frozenset _GROUP_MEMBERSHIP_ACTIONS = frozenset({"approve_group_membership", "decline_group_membership"}) and a branch for it before the final else that sends actions to admin_config.apply_change, written in the shape of the _PRIVACY_ACTIONS branch.
      2. _admin_state gains state["group_membership"] = {"source": "snapshot" | "static", "updated_at": float | None, "pending": [{"group", "audience", "count", "emails"}], "last_rejected": {"reason", "at"} | None}; pending lists at most 50 emails per row and a count. For source "static" pending is empty and the section is read-only.
      3. settings_window_html.py: a new admin section "Group membership" (renderMembership) beside renderRuntimes: a read-only status row (source, last update, last rejected snapshot with reason), then one card per pending (group, audience) row with the count, the first emails, and two buttons wired with the same dataAttr/data-confirm pattern as connectorConfigRowHtml: "Approve all" (approve_group_membership) and "Decline all" (decline_group_membership); no button approves across groups. Add the section to the section switch and the section-to-action map. Visible strings: heading "Group membership"; row labels "Source", "Last update", "Last rejected snapshot"; empty text "Nothing is waiting for approval."; the confirm text "Give <count> person(s) in <group> access to the <audience> audience?".
      4. Tests: org-only (404 in local mode), 401 and 403 and csrf and origin cases through the existing World helpers; a 428 step-up on first call; approving makes the resolver grant the audience; declining removes the pending row; state shows the pending rows; the HTML helper test renders the empty state, a pending row and the static read-only state.
    acceptance:
      - "python3 -m pytest tests/unit/web/test_routes_admin.py tests/unit/test_settings_window_html.py -q passes"
      - "python3 -m pytest tests/unit -q passes"
  - id: p16-contract-and-examples
    title: The contract document and the knowledge-hub example
    depends_on: [p12-reference-server-final]
    complexity: S
    touches:
      - docs/identity-claim.md
      - contrib/examples/knowledge-hub/upstream-mcp.json
      - contrib/examples/knowledge-hub/admin_config.upstream_mcp.json
      - contrib/examples/knowledge-hub/group_membership_static.json
      - tests/unit/test_upstream_mcp_examples.py
    brief: |
      Read first: plan sections 3.1 to 3.5 and 3.10, contrib/verifier/req_test_vectors.json, contrib/verifier/pf_claim_verify.py, docs/upstream-mcp.md (the row-level security sketch to move).
      1. docs/identity-claim.md: the contract. Sections: Purpose; Transport (header, no Authorization); The JWT (header, payload table of plan 3.1, which keys are optional, that iss has no path suffix and jti is a UUID string); Principals (the four types and the sub rules); Audiences; The request binding (canonicalisation steps, the six vectors with their canonical text and req, non-call requests carry no audiences, batches refused, only the tool name and arguments are bound, the upstream dispatches only what it hashed); Lifetime and clock skew; Keys and rotation (JWKS at <issuer>/upstream-mcp/jwks, caching 300 s, refetch on an unknown kid, overlap, a new key signs 300 s after it is published); Verification checklist (numbered, in the order of plan 3.10); Using pf_claim_verify.py (a short code example); Row-level security sketch (copy the PostgreSQL sketch from docs/upstream-mcp.md, with audiences in place of domains, and leave the original for phase 17 to remove); Versioning (claim_version 1; adding an optional claim stays 1; any other change is 2 with a new typ). State that tools is information only.
      2. contrib/examples/knowledge-hub/upstream-mcp.json: an operator file with claim.key_overlap_hours 24, membership {"source": "snapshot", "max_additions": 20}, and one upstream: id "knowledgehub", label "Knowledge hub", url "https://hub.example.internal/mcp", audience "knowledge-hub", identity_claim "required", claim_lifetime_seconds 60, allowed_principal_types ["claude_client", "n8n_workflow"], tools search ({"gate": "auto", "verb": "search"}) and get_doc ({"gate": "auto", "verb": "read"}). admin_config.upstream_mcp.json: {"upstream_mcp": {"knowledgehub": {"groups": {"everyone@company": ["all"], "mgmt@company": ["mgmt"], "hr@company": ["hr"], "contracts@company": ["contracts"]}}}}. group_membership_static.json: version 1 with those four groups and two example addresses at example.org each (one person is in both everyone@company and mgmt@company).
      3. tests/unit/test_upstream_mcp_examples.py loads the operator file through upstream_mcp_config.parse_upstreams and parse_settings, the admin config through GroupMapStore on a tmp copy, and the static file through group_membership.StaticFileResolver, and asserts all validate and the person in mgmt@company gets {"all", "mgmt"} while the other gets {"all"}.
    acceptance:
      - "python3 -m pytest tests/unit/test_upstream_mcp_examples.py -q passes"
      - "grep -c 'claim_version' docs/identity-claim.md is at least 3"
  - id: p17-retire
    title: ADRs, operator docs, docs index rows and the changelog
    depends_on: [p16-contract-and-examples, p11-forward-audit, p14-token-admin-only, p15-admin-approvals]
    complexity: M
    touches:
      - docs/upstream-mcp.md
      - docs/configuration-reference.md
      - docs/org-mode-setup-guide.md
      - docs/how-it-works.md
      - docs/README.md
      - docs/adr/0288-the-upstream-claim-is-an-eddsa-identity-claim-in-its-own-header-bound-to-one-call.md
      - docs/adr/0289-audiences-come-from-group-membership-and-tokens-only-narrow-them.md
      - docs/adr/0290-group-membership-is-updated-through-a-scoped-token-tool.md
      - docs/adr/0286-privacyfence-sends-the-upstream-a-signed-caller-claim.md
      - docs/adr/0287-data-domains-come-from-grants-on-a-verified-email-and-tokens-only-narrow-them.md
      - docs/adr/README.md
      - CHANGELOG.md
    brief: |
      Read first: docs/adr/README.md (template, rules, index), docs/adr/0286-*.md, docs/adr/0287-*.md, docs/upstream-mcp.md, docs/configuration-reference.md, CHANGELOG.md lines about upstream MCP under ## [Unreleased], docs/identity-claim.md, plan sections 2 to 4.
      This phase edits docs/README.md and the ADR index: allowed, it is the retirement phase.
      1. ADRs 0288, 0289, 0290 with the template headings of docs/adr/README.md, Status "Accepted — 2026-11-14. Amends ADR 0286." (0289 amends 0287; 0290 relates to 0164 and 0250), Implemented: lines naming the modules (identity_claim.py, upstream_claim_keys.py, group_membership.py, connectors/group_membership.py, contrib/verifier/pf_claim_verify.py), the decisions of plan sections 3.1 to 3.9 including that iss lost its /upstream-mcp suffix and jti is a UUID string instead of hex, and the rejected alternatives named in the plan (keep both claims; hashing all of params; an inbound admin route; a popup gate for additions; the 0600 digest file kept as the unused fallback). Link issue #1103, never the plan. Add a pointer line "Amended by ADR 0288" / "Amended by ADR 0289" to the Status of ADRs 0286 and 0287 (a pointer only, as the earlier forward pointers), update their index rows and add the three new rows to docs/adr/README.md in the existing format.
      2. docs/upstream-mcp.md: replace the "Domain grants" and "For upstream implementers" claim sections by the group map, the operator keys (identity_claim, claim_lifetime_seconds, allowed_principal_types, claim.key_overlap_hours, membership), the membership tool and the Admin card, rotation, and a link to identity-claim.md; remove the row-level security sketch (it now lives in identity-claim.md); rename domains to audiences throughout; update the reference-server instructions (Ed25519 JWKS, a static membership file).
      3. docs/configuration-reference.md: the operator-file keys, the admin_config.json upstream_mcp.<id>.groups row (replacing the grants row), the files upstream_claim_keys.json and group_membership.json in "Files in the org directory", and rows for --rotate-upstream-claim-key and --revoke-upstream-claim-key in "Command-line options".
      4. docs/org-mode-setup-guide.md and docs/how-it-works.md: grep -n -i "data domain\|domains" in both, change every hit that talks about upstream data domains to audiences, and add one sentence to how-it-works.md that the audit log records one entry per call forwarded to an upstream, with the claim's jti and audiences.
      5. docs/README.md: add a row for identity-claim.md next to upstream-mcp.md in "Organization deployment and security" (a dash line with a link and "what it answers; read when" text of at least 12 characters outside the link) and fix the upstream-mcp.md row that says "data domains".
      6. CHANGELOG.md under ## [Unreleased]: replace the two existing upstream entries (the signed claim and "Data domains on scoped tokens") with entries describing the shipped behaviour: the EdDSA claim in X-PrivacyFence-Claim bound to one call with the verifier in contrib/verifier, audiences from Workspace groups, the privacyfence_update_group_membership tool with approvals on the Admin page, key rotation with --rotate-upstream-claim-key, one audit entry per forwarded call. No version heading.
      7. Run python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_configuration_reference.py -q and fix every failure in the docs you wrote.
    acceptance:
      - "python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_configuration_reference.py -q passes"
      - "ls docs/adr/0288-*.md docs/adr/0289-*.md docs/adr/0290-*.md lists three files"
      - "grep -n '0290' docs/adr/README.md matches"
      - "grep -rn 'data domains' docs/upstream-mcp.md docs/org-mode-setup-guide.md returns no match"
```
