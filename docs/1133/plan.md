# Email as an opt-in visibility input, and a groups claim from the OIDC provider

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An operator adds `email_for_visibility: true` to an upstream in the upstream MCP file; its claims then carry the person's verified, lower-cased email, and the upstream may use it to decide what a query returns. An operator adds `PRIVACYFENCE_OIDC_CLIENT_FORGEJO_GROUPS=true` (and optionally `..._GROUPS_ALLOWED=kh-*`) for an OIDC app; its ID token and userinfo then carry `groups`, the Workspace groups the person belongs to whose additions an administrator has approved. The Forgejo recipe is a new section of the setup guide. The release (8.0.0) is cut afterwards with `/tld:release`, not in this plan.

**Scope:** Adds the declaration key, the claim rule, the per-client groups variables, the resolver method behind them, two ADRs (0310, 0311), docs and a changelog entry. The verifier `contrib/verifier/pf_claim_verify.py` and `claim_version` do not change, so no hash is republished. Without the new settings nothing changes. Out of scope: the knowledge hub, Forgejo team creation, n8n workflows, a separate approval for OIDC groups, and the `privacyfence-suite` repo (its needs go in the PR body).

**Security:** `email` becomes a visibility input, so its rules are a contract: verified only, `claude_client` only, lower-cased and NFC, look-alike addresses refused (Design 3.1). `groups` carries access-granting data to a third-party app: approved membership only, opt-in per client, allowlist per client, fail closed to an empty list (Design 4).

**Review focus:** the rule for which groups count as approved (Design 4.3, owner's answer A); the look-alike refusal in `visibility_email` (3.1); `groups` sent regardless of the requested scope (4.5); the dependency of groups on a declared upstream (Risks).

## 1. Goal

The knowledge hub shows attendee-only records by the claim's `email`, and Forgejo team membership has to follow the approved Workspace groups. Today `docs/identity-claim.md` calls `email` audit-only, and the OIDC provider publishes only `sub`, `email`, `email_verified` and `name`. After this change the contract names `email` as a fifth, opt-in input, and an OIDC app can receive the approved groups. Tracking issue: [#1133](https://github.com/privacyfence/privacyfence/issues/1133). The release follows the merge.

## 2. Current state

- `src/privacyfence/upstream_mcp_runtime.py:152` `caller_for` builds the claim caller. For a person it sets `email=principal.email if principal.email_verified else None` (line 170), unchanged, so the address is not lower-cased. A scoped token never carries `email` (the `n8n_workflow` branch).
- `src/privacyfence/identity_claim.py` `ClaimSigner.mint` writes `email` when it is set; `IdentityClaimAuth.auth_flow` keeps `email` on non-call requests too.
- `src/privacyfence/upstream_mcp_config.py:59` `_UPSTREAM_KEYS`, `:84` `UpstreamServer`, `:317` the `identity_claim` parse. An unknown key is refused. `declaration_digest` (`:95`) pins ADR 0300 approvals.
- `src/privacyfence/group_membership.py`: `MembershipStore.members_of`, `has_approval`; `SnapshotResolver` and `StaticFileResolver` answer `audiences_for` only. The store keeps only groups that some upstream's map names (ADR 0299). Additions to a restricted audience wait for an (email, group, audience) approval; additions to `all` apply at once.
- `src/privacyfence/oidc_provider.py:56` `OidcClient` (id, redirect URIs, secret), `:106` `clients_from_env`, `:265` `discovery` (scopes `openid email profile`, claims `sub email email_verified name`), `:348` `issue_tokens` builds the ID token and the userinfo claims. Tokens are issued at the token endpoint, five minutes long.
- `src/privacyfence/daemon_main.py:1199-1210` builds the provider after `_configure_upstream_mcp` (`:1355`), which builds the resolver only when an upstream is declared.
- Docs that list the declaration keys: `docs/upstream-mcp.md` (lines 73, 81-83, 152), `docs/configuration-reference.md:310-312` (OIDC variables), `docs/oidc-provider.md`, `docs/org-mode-docker.md:470`.
- Existing tests: `tests/unit/test_upstream_mcp_config.py` (`_doc`, `_parse`, `_refused`, `TestRefusals`), `tests/unit/test_upstream_mcp_runtime.py` (fixture `configured`, helpers `_caller` and `_scope`, `TestCallerFor`), `tests/unit/test_identity_claim.py`, `tests/unit/test_group_membership.py` (`TestResolvers`), `tests/unit/test_oidc_provider.py` (`_client_env`, `_provider`, `_code`, `_redeem`, `TestProviderState`), `tests/unit/web/test_routes_oidc.py` (`test_discovery_document_exactly` at line 27, `_Env`, `test_full_flow_basic_auth`).

## 3. Design: `email` as a visibility input

### 3.1 Declaration and rule

- New upstream key **`email_for_visibility`**, a JSON/YAML boolean, default `false`. `UpstreamServer.email_for_visibility: bool = False`. A value that is not exactly `true` or `false` (a string, number, null) is refused with `<source>: upstreams[i].email_for_visibility: must be true or false`. `true` together with `identity_claim: off` is refused with `<source>: upstreams[i].email_for_visibility: needs identity_claim required`. It is not part of `declaration_digest`: a scoped token never carries `email`, so no ADR 0300 approval depends on it.
- New function `identity_claim.visibility_email(value: str) -> str | None`:
  1. `stripped = value.strip()`; return `None` when it is empty, has no or more than one `@`, has whitespace, or has an empty local part or domain.
  2. `text = unicodedata.normalize("NFC", stripped.lower())`.
  3. Return `None` when `not stripped.isascii() and text.isascii()` (a look-alike such as the Kelvin sign U+212A, which lower-cases to `k`; same reason as `org_identity._normalized_email`).
  4. Return `text`.
- In `caller_for`, for a person: `email = principal.email if principal.email_verified else None`; when `upstream.email_for_visibility` and `email` is set, `email = identity_claim.visibility_email(email)`. Without the opt-in the line is unchanged, so the claim is byte-for-byte what it is today.
- The address is the one `audiences_for_email` uses (the session's `principal.email` and `principal.email_verified`), so `email` and `audiences` come from one source. When the person's verified address changes, `sub` (the principal id) stays and `email` follows. A scoped token never carries `email`, opted in or not; `service` claims never do.
- The claim key `email` is also sent on non-`tools/call` requests of a person (unchanged); the contract tells the upstream to use it only on a `tools/call` whose `req` matched, as for `audiences`.

### 3.2 Contract text (`docs/identity-claim.md`)

- Line 44 table row for `email` gets: present only when `sub` is a person and the address is verified; with `email_for_visibility: true` it is lower-cased and NFC.
- Line 55 becomes: `sub`, `principal_type`, `audiences` and `req` may change what a query returns, and `email` too, but only when the upstream declared `email_for_visibility: true`; `approved_by`, `token_id` and `act` are for audit.
- New section `## Email as a visibility input (opt-in)` after "Audiences": the promises of the request (claude_client only; never `n8n_workflow` or `service`; verified only; lower-case, NFC; a look-alike address gives no `email`; same verified source as `audiences`; `sub` stays when the address changes and `email` follows), what an upstream does (filter inside the query by exact match against its own list, never `email` on a request that is not a matched `tools/call`, an absent `email` matches nothing), and that without the opt-in `email` is audit-only.
- In "Versioning": any change to how `email` is filled when the opt-in is on (the source, the normalisation, the cases where it is present) is a breaking contract change and needs `claim_version` 2 and a new `typ`.
- The verifier file is not changed; the doc does not publish a hash.

## 4. Design: `groups` in the OIDC provider

### 4.1 Client variables (final names)

- `PRIVACYFENCE_OIDC_CLIENT_<NAME>_GROUPS`: `true` or `false` (exact, lower case), default `false`. Anything else: `ConfigurationError("<stem>GROUPS must be true or false")`.
- `PRIVACYFENCE_OIDC_CLIENT_<NAME>_GROUPS_ALLOWED`: comma-separated entries, lower-cased. An entry is an exact group name (`GROUP_NAME_RE` from `group_membership`) or a prefix ending in `*` (`[a-z0-9][a-z0-9._+@-]{0,63}\*`). Empty or unset: all groups pass. At most 50 entries. A bad entry: `ConfigurationError("<stem>GROUPS_ALLOWED has an invalid entry: <entry>")` (an entry holds no secret). Set without `<stem>GROUPS=true`: `ConfigurationError("<stem>GROUPS_ALLOWED needs <stem>GROUPS=true")`.
- `OidcClient` gains `groups: bool = False` and `groups_allowed: tuple[str, ...] = ()` after `secret`, and the method `allows_group(group) -> bool` (empty tuple allows all; an entry ending `*` matches by `startswith` on the part before `*`; otherwise equality).

### 4.2 What is sent

- For a client with `groups=True`, `issue_tokens` adds `"groups": [...]` to the ID token claims and to the userinfo claims (always a list, possibly empty, sorted). For other clients the key is absent, so their tokens are unchanged.
- The provider takes a new keyword `member_groups: Callable[[str, bool], Iterable[str]] | None = None`, called as `member_groups(principal.email, grant.email_verified)`. `None` gives `[]`. The result is lower-cased, de-duplicated, filtered by `client.allows_group`, sorted. A raised exception is caught (`# noqa: BLE001 -- fail closed`), logged as `OIDC groups lookup failed for client <client_id>` with no address, and gives `[]`. Groups are looked up at the token endpoint, per sign-in, so a removal applies at the next sign-in and an unapproved addition never appears.
- `discovery()`: `scopes_supported` is `["openid", "email", "profile", "groups"]` and `claims_supported` is `["sub", "email", "email_verified", "name", "groups"]`.

### 4.3 Approved membership (owner's answer A)

- New `groups_for(email, group_audiences) -> frozenset[str]` on the `MembershipResolver` protocol and both resolvers. `SnapshotResolver`: a group `g` in `members_of(email)` and in the map counts when every audience in `map[g]` is `all` or `store.has_approval(email, g, audience)`; a group mapped to no audience counts. `StaticFileResolver`: a group whose member list holds the email and that is in the map. A group not in the map never counts (default deny, as for audiences).
- New `upstream_mcp_runtime.approved_groups_for_email(email, verified) -> tuple[str, ...]`: `()` when unconfigured, when `not verified` or when the email is empty; otherwise `tuple(sorted(resolver.groups_for(email.strip().lower(), all_group_audiences())))`. `all_group_audiences()` merges every upstream's map, so a group mapped to a restricted audience in two upstreams counts only when all of its restricted audiences are approved.
- Consequence for the operator, written in the docs: a group reaches an app only if some upstream maps it in `admin_config.json`. To have a Forgejo team wait for an administrator's approval, map its group to a restricted audience (the hub need not use it); to send it at once, map it to `[]`. Mapping it to `all` also grants the hub's unrestricted audience, which is rarely wanted.

### 4.4 Wiring

`daemon_main` passes `member_groups=upstream_mcp_runtime.approved_groups_for_email` when it builds `OidcProvider`. After the provider is built, for each client with `groups` while `upstream_mcp_runtime.configured()` is false, it logs a warning `OIDC client <name> receives groups, but no upstream MCP file is declared, so its groups are always empty.`

### 4.5 Rejected alternatives

- A second sync job for Forgejo teams: it would add members without the approval step ADR 0299 requires.
- Identity-provider group claims: ADR 0298 already rejected them.
- Sending groups only when the `groups` scope is requested: a Forgejo that omits the scope would send nothing, and `--group-team-map-removal` would then remove everyone from their teams. Opt-in per client is the gate.
- Pending additions in the claim, or all stored groups: would grant access before approval.
- A separate OIDC-only group approval (owner's option C) and refusing groups with no restricted audience (option B): not chosen.

## 5. ADRs

- **0310** `email` is an opt-in visibility input of the identity claim. Accepted; amends 0297; relates 0298. Records 3.1 and 3.2, the look-alike refusal, why the key is per upstream, why the digest is untouched and why `claim_version` stays 1 (an opt-in addition, but a later change to its filling is a version bump). Rejected: always sending a lower-cased email, a separate claim name, loosening to unverified addresses, and changing the verifier.
- **0311** The OIDC provider sends approved group membership to apps that opt in. Accepted; amends 0273; relates 0298, 0299. Records 4.1-4.4: why PrivacyFence is the source of team membership (one approval step, the audit trail, no second sync), why only approved membership (an addition to a restricted audience must wait), the limits (read at sign-in only, a removal lands at the next sign-in, bounded by the app's session lifetime), fail-closed behaviour and the rejected alternatives of 4.5.

## 6. Manual steps

**After implementation** (before the PR merges), one item, about 20 minutes, on a staging server:

1. Open the Forgejo instance's admin page, Authentication sources, and add an OAuth2 source for PrivacyFence by the recipe in the new setup-guide section (the guide's link: `https://github.com/privacyfence/privacyfence/blob/feature/email-visibility-oidc-groups/docs/org-mode-setup-guide.md`).
2. Set `PRIVACYFENCE_OIDC_CLIENT_FORGEJO_GROUPS=true` on the PrivacyFence server and restart it.
3. Sign in as a person who is in a mapped group; confirm the team appears. Remove the person from the group via the sync tool, sign in again, confirm the team goes.
4. Report back: pass or fail, and the Forgejo version.

Release, after the PR merges: `/tld:release`, 8.0.0. Needs in the `privacyfence-suite` repo, to go in the PR body: (a) pin `PRIVACYFENCE_IMAGE_TAG` to the released tag in each `.env.example`; (b) make `PRIVACYFENCE_OIDC_CLIENTS` overridable in the five `configs/*/compose.yaml` files (it is hard-coded to `peek,grist` in the `environment:` block, which wins over `privacyfence.env`) so a `forgejo` client can be added, with its `PRIVACYFENCE_OIDC_CLIENT_FORGEJO_*` variables and a Docker secret for its client secret; (c) pass `PRIVACYFENCE_UPSTREAM_MCP_FILE` through and mount the declaration file into the privacyfence service. The PR body also lists the final names: `email_for_visibility`, `PRIVACYFENCE_OIDC_CLIENT_<NAME>_GROUPS`, `PRIVACYFENCE_OIDC_CLIENT_<NAME>_GROUPS_ALLOWED`.

## 7. Risks and open questions

- A client opted in on a server with no upstream declared always gets `groups: []`, because the membership store is wired only with an upstream. The daemon warns (4.4); a phase that sees the resolver wired some other way stops with `status=blocked`.
- `all_group_audiences` is read per sign-in and reads `admin_config.json`; if a test shows the lookup is slow or needs a lock the brief did not name, stop with `status=blocked`.
- Forgejo's flag names differ between versions; the guide marks them "check against your Forgejo version" and the manual step records the version.
- A static-file resolver has no approvals, so there every listed member's group is sent at once; the docs say so.
- Non-ASCII addresses are refused only when they normalise to ASCII; a genuine internationalised address is sent NFC-normalised. If `visibility_email` and `audiences_for_email` disagree on one address in a test, stop with `status=blocked`.

## Implementation manifest

```yaml
plan_slug: email-visibility-oidc-groups
feature_branch: feature/email-visibility-oidc-groups
tracking_issue: 1133
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-forgejo-signin
    title: Sign in to a real Forgejo through PrivacyFence with groups on, and check the team follows
    why: CI has no Forgejo; the claim shape and the team-map flags are only proven against a real instance.
verify_after_merge:
  - python3 -m pytest tests/unit/test_identity_claim.py tests/unit/test_upstream_mcp_config.py tests/unit/test_upstream_mcp_runtime.py tests/unit/test_group_membership.py tests/unit/test_oidc_provider.py tests/unit/web/test_routes_oidc.py -q
final_checks:
  - "`tld plan deviations check --file docs/1133/deviations.md --plan docs/1133/plan.md --all` exits 0"
  - "ADRs 0310 and 0311 exist, are Accepted, and are in docs/adr/README.md"
  - "CHANGELOG.md has entries under ## [Unreleased] and no version heading"
  - "the PR body lists email_for_visibility, PRIVACYFENCE_OIDC_CLIENT_<NAME>_GROUPS and PRIVACYFENCE_OIDC_CLIENT_<NAME>_GROUPS_ALLOWED, and the three suite-repo needs of section 6"
phases:
  - id: p1-email-visibility
    title: The email_for_visibility declaration and the claim rule
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/identity_claim.py
      - src/privacyfence/upstream_mcp_config.py
      - src/privacyfence/upstream_mcp_runtime.py
      - tests/unit/test_identity_claim.py
      - tests/unit/test_upstream_mcp_config.py
      - tests/unit/test_upstream_mcp_runtime.py
    brief: |
      Read first: docs/1133/plan.md section 3, src/privacyfence/upstream_mcp_runtime.py (caller_for), src/privacyfence/upstream_mcp_config.py (_parse_one), docs/identity-claim.md.
      1. identity_claim.py: add `import unicodedata` and the function `visibility_email(value: str) -> str | None` exactly as Design 3.1 lists its four steps.
      2. upstream_mcp_config.py: add "email_for_visibility" to `_UPSTREAM_KEYS`; add the field `email_for_visibility: bool = False` after `allowed_principal_types` in `UpstreamServer`; in `_parse_one`, after the `allowed_principal_types` check, read `raw.get("email_for_visibility", False)`, refuse a value whose `type(...)` is not `bool` with `fail(f"{base}.email_for_visibility", "must be true or false")`, refuse `True` with `claim_mode == "off"` with `fail(f"{base}.email_for_visibility", "needs identity_claim required")`, and pass it to `UpstreamServer`. Do not touch `declaration_digest`.
      3. upstream_mcp_runtime.py: in `caller_for`, in the `scope is None` branch, compute `email = principal.email if principal.email_verified else None`, then `if email and upstream.email_for_visibility: email = identity_claim.visibility_email(email)` (import the function from `.identity_claim`), and pass `email=email`. The scoped-token branch is unchanged.
      4. Tests. test_identity_claim.py: a `TestVisibilityEmail` class: lower-cases and strips (` Ann@HR.Example ` gives `ann@hr.example`), NFC (decomposed `e` + U+0301 in the local part gives the composed form), refuses the Kelvin sign (`ann@K.example`), refuses empty, two `@`, whitespace inside. test_upstream_mcp_config.py: `test_email_for_visibility_defaults_to_false`, `test_email_for_visibility_true_is_read`, and refusals through `_refused` for `"true"`, `1`, `None` and for `true` with `identity_claim: "off"`, each naming `upstreams[0].email_for_visibility`; and a test that `declaration_digest` is the same with the key on and off. test_upstream_mcp_runtime.py in `TestCallerFor`: opted in, a verified `Ann@HR.example` gives `ann@hr.example`; opted in, an unverified principal gives `email is None`; opted in, a scoped-token caller gives `email is None`; not opted in, `claim.email == "Ann@HR.example"` (the existing test must stay as it is). Build the opted-in upstream with `dataclasses.replace(configured[0], email_for_visibility=True)`.
      Stop with `status=blocked` if `caller_for` no longer builds the person's claim from `principal.email_verified` as Current state says.
    acceptance:
      - python3 -m pytest tests/unit/test_identity_claim.py tests/unit/test_upstream_mcp_config.py tests/unit/test_upstream_mcp_runtime.py -q passes
      - grep -n "email_for_visibility" src/privacyfence/upstream_mcp_config.py src/privacyfence/upstream_mcp_runtime.py matches in both files
      - grep -n "email_for_visibility" src/privacyfence/upstream_mcp_config.py shows no match inside declaration_digest
      - ruff check . passes
  - id: p2-oidc-groups-provider
    title: The groups claim in the OIDC provider
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/oidc_provider.py
      - tests/unit/test_oidc_provider.py
      - tests/unit/web/test_routes_oidc.py
    brief: |
      Read first: docs/1133/plan.md section 4 (4.1, 4.2), src/privacyfence/oidc_provider.py, docs/oidc-provider.md.
      1. oidc_provider.py: add `from collections.abc import Callable, Iterable` and `import logging` (with `logger = logging.getLogger(__name__)`). Add to `OidcClient` the fields `groups: bool = False` and `groups_allowed: tuple[str, ...] = ()` after `secret`, and the method `allows_group(self, group: str) -> bool` as in Design 4.1.
      2. In `clients_from_env`, after the secret, read `f"{stem}GROUPS"` and `f"{stem}GROUPS_ALLOWED"` with a new helper `_client_groups(stem) -> tuple[bool, tuple[str, ...]]` that raises the three `ConfigurationError` messages of Design 4.1, lower-cases the entries, drops empty parts, allows at most 50 entries (more: `ConfigurationError("<stem>GROUPS_ALLOWED lists more than 50 entries")`) and checks each entry against `group_membership.GROUP_NAME_RE` or the prefix pattern of 4.1 (import `from . import group_membership`; check for an import cycle first and stop with `status=blocked` if there is one).
      3. `OidcProvider.__init__` takes `member_groups: Callable[[str, bool], Iterable[str]] | None = None` as a keyword and stores it. Add the private method `_groups_for(self, client: OidcClient, principal: Principal, email_verified: bool) -> list[str]` as in 4.2 (lower-case, de-duplicate, filter, sort; exception gives `[]` and the log line of 4.2).
      4. In `issue_tokens`, after `user_claims` is built and before it is stored, `client = self._clients.get(grant.client_id)`; when `client is not None and client.groups`, set `user_claims["groups"] = groups` and, after `claims` is built, `claims["groups"] = groups`.
      5. `discovery()`: add `"groups"` to `scopes_supported` and `claims_supported` as in 4.2.
      6. Tests. test_oidc_provider.py: `TestClientsFromEnv` cases (GROUPS true parses, GROUPS `yes` refused, ALLOWED without GROUPS refused, a bad entry refused, prefix and exact entries parse, more than 50 entries refused); `TestClientGroups` in the provider tests with a client built as `oidc_provider.OidcClient("peek", ("https://p/cb",), SECRET, groups=True)` and `member_groups=lambda email, verified: [...]`: an opted-in client gets `groups` in the ID token and in `provider.userinfo(...)`; a client not opted in has no `groups` key in either; `groups_allowed=("kh-*", "exact@x.example")` filters; an unverified email passes `False` to the callable (assert with a recording lambda); `member_groups=None` gives `[]`; a callable that raises gives `[]` and the log text has no address (use `caplog`); a person with no membership gets `[]`. test_routes_oidc.py: update `test_discovery_document_exactly` to the new scope and claim lists; add `test_full_flow_userinfo_carries_groups_for_an_opted_in_client` that runs the existing full-flow helper pattern of `test_full_flow_basic_auth` with a groups client and checks the ID token and `/oidc/userinfo`.
      Stop with `status=blocked` if `_Env` in test_routes_oidc.py cannot build a provider with a client of your own; report what it builds.
    acceptance:
      - python3 -m pytest tests/unit/test_oidc_provider.py tests/unit/web/test_routes_oidc.py -q passes
      - python3 -m pytest tests/unit/test_oidc_provider.py -q --cov=privacyfence.oidc_provider --cov-branch --cov-report=term-missing reports 100% for oidc_provider.py
      - grep -n '"groups"' src/privacyfence/oidc_provider.py matches the discovery lists
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p3-approved-groups
    title: The resolvers answer which groups a person holds, approved only
    depends_on: [p1-email-visibility]
    complexity: S
    touches:
      - src/privacyfence/group_membership.py
      - src/privacyfence/upstream_mcp_runtime.py
      - tests/unit/test_group_membership.py
      - tests/unit/test_upstream_mcp_runtime.py
    brief: |
      Read first: docs/1133/plan.md section 4.3, src/privacyfence/group_membership.py (the resolvers), src/privacyfence/upstream_mcp_runtime.py (`all_group_audiences`, `audiences_for_email`).
      1. group_membership.py: add `groups_for(self, email: str, group_audiences: Mapping[str, frozenset[str]]) -> frozenset[str]` to the `MembershipResolver` protocol (docstring: the groups whose every restricted audience is approved for `email`; empty for an empty email). Implement it on `SnapshotResolver` and `StaticFileResolver` as Design 4.3 says; reuse `_normalized_map` and `UNRESTRICTED_AUDIENCE`.
      2. upstream_mcp_runtime.py: add `approved_groups_for_email(email: str, verified: bool) -> tuple[str, ...]` as in 4.3, next to `audiences_for_email`.
      3. Tests. test_group_membership.py in `TestResolvers` (use its existing `snap` helper and `MAP`): a member of a group mapped only to `all` is returned; a member of a group mapped to a restricted audience is not returned before `store.approve(...)` and is returned after; a group mapped to two audiences needs both approved; a group mapped to `frozenset()` is returned; a stored member of a group no longer in the map is not returned; after a snapshot that removes the person the group is gone at once; an empty email gives `frozenset()`; `StaticFileResolver` returns the groups that list the email and are in the map. test_upstream_mcp_runtime.py: `TestApprovedGroups` using the `configured` fixture: verified email gives the sorted tuple, unverified gives `()`, empty email gives `()`, unconfigured (`upstream_mcp_runtime.reset()` first) gives `()`, a group mapped in two upstreams to different audiences is merged.
      Stop with `status=blocked` if a third class implements `MembershipResolver` (grep for `audiences_for` in src and tests) and would need the new method.
    acceptance:
      - python3 -m pytest tests/unit/test_group_membership.py tests/unit/test_upstream_mcp_runtime.py -q passes
      - python3 -m pytest tests/unit/test_group_membership.py -q --cov=privacyfence.group_membership --cov-branch --cov-report=term-missing reports 100% for group_membership.py
      - grep -n "def groups_for" src/privacyfence/group_membership.py matches three times
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p4-wire-groups
    title: Wire approved groups into the OIDC provider at start
    depends_on: [p2-oidc-groups-provider, p3-approved-groups]
    complexity: S
    touches:
      - src/privacyfence/daemon_main.py
      - tests/unit/test_daemon_main.py
    brief: |
      Read first: docs/1133/plan.md section 4.4, src/privacyfence/daemon_main.py (lines around `oidc_provider.OidcProvider(`), the OIDC tests in tests/unit/test_daemon_main.py (`grep -n "OIDC\|oidc" tests/unit/test_daemon_main.py`).
      1. daemon_main.py: pass `member_groups=upstream_mcp_runtime.approved_groups_for_email` to `oidc_provider.OidcProvider(...)`. After the provider is built, loop over `oidc_clients.values()` and, for each client with `client.groups` while `not upstream_mcp_runtime.configured()`, `logger.warning("OIDC client %s receives groups, but no upstream MCP file is declared, so its groups are always empty.", client.client_id)`.
      2. tests/unit/test_daemon_main.py: extend the existing OIDC start test (or add one beside it, copying its setup) so that (a) the built provider's `member_groups` is `upstream_mcp_runtime.approved_groups_for_email`, and (b) with `PRIVACYFENCE_OIDC_CLIENT_<NAME>_GROUPS=true` and no upstream configured, the warning is logged (`caplog`).
      Stop with `status=blocked` if the existing tests cannot reach the OIDC branch without starting a server; report what they do instead.
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py -q passes
      - grep -n "member_groups=upstream_mcp_runtime.approved_groups_for_email" src/privacyfence/daemon_main.py matches
      - ruff check . passes
  - id: p5-reference-docs
    title: Reference docs for the key, the contract and the groups claim
    depends_on: [p4-wire-groups]
    complexity: M
    touches:
      - docs/identity-claim.md
      - docs/upstream-mcp.md
      - docs/oidc-provider.md
      - docs/configuration-reference.md
      - docs/org-mode-docker.md
    brief: |
      Read first: docs/1133/plan.md sections 3.2, 4 and 4.3, then the five files above.
      1. docs/identity-claim.md: make the edits of Design 3.2 (the `email` table row at line 44, the sentence at line 55, the new section `## Email as a visibility input (opt-in)` after the Audiences section, and the change-control note in Versioning). Do not add a verifier hash.
      2. docs/upstream-mcp.md: add `email_for_visibility` to the key list at line 73 and to the sentence at line 152, and a table row after `allowed_principal_types`: `true` or `false`, default `false`; the claim then carries the person's verified, lower-cased email for use in the upstream's own visibility rules; refused with `identity_claim: off` and for any value that is not a boolean; link to identity-claim.md#email-as-a-visibility-input-opt-in.
      3. docs/oidc-provider.md: add `…_GROUPS` and `…_GROUPS_ALLOWED` rows to the variable table; change the userinfo row of the endpoint table to say `groups` for apps that opt in; add the section `## Groups` after "Verified email" with: what is sent (approved Workspace groups, per Design 4.3; pending additions never; removals at the next sign-in), the allowlist forms (exact and prefix `kh-*`), `groups: []` for a person with no membership, groups sent whether or not the `groups` scope is requested, a group reaches an app only if an upstream maps it in `admin_config.json` (map it to a restricted audience to make additions wait, to `[]` to send at once), no upstream declared means always empty, and the limit: the app reads groups at sign-in only, so a removal takes effect at the person's next sign-in and the app's session lifetime bounds the delay; recommend a short session lifetime in the app. Link to ADR 0311.
      4. docs/configuration-reference.md: add the two variables after line 312, same wording as the oidc-provider table. docs/org-mode-docker.md line 470: append one sentence naming `…_GROUPS` and linking oidc-provider.md#groups.
      5. Run `python3 -m pytest tests/unit/test_upstream_mcp_examples.py -q` and any docs link test the dod names; fix a link you broke.
    acceptance:
      - grep -c "email_for_visibility" docs/identity-claim.md docs/upstream-mcp.md prints a count of 1 or more for both files
      - grep -n "_GROUPS_ALLOWED" docs/oidc-provider.md docs/configuration-reference.md matches in both files
      - grep -n "claim_version. 2" docs/identity-claim.md matches the change-control note
      - python3 -m pytest tests/unit/test_upstream_mcp_examples.py -q passes
  - id: p6-retire
    title: ADRs 0310 and 0311, the Forgejo guide section and the changelog
    depends_on: [p5-reference-docs]
    complexity: M
    touches:
      - docs/adr/0310-email-is-an-opt-in-visibility-input-of-the-identity-claim.md
      - docs/adr/0311-the-oidc-provider-sends-approved-group-membership-to-apps-that-opt-in.md
      - docs/adr/README.md
      - docs/org-mode-setup-guide.md
      - CHANGELOG.md
    brief: |
      Read first: docs/1133/plan.md sections 3, 4, 5 and 7, docs/adr/0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md (the template: Status, Context, Decision, Alternatives considered, Consequences, Verification, Related), docs/adr/README.md (row format).
      1. Write the two ADR files named in `touches`, following Design section 5 for their status lines, decisions, rejected alternatives and consequences, with today's date as the Accepted date and `Issue [#1133](https://github.com/privacyfence/privacyfence/issues/1133)`. Their Verification lists the test classes of phases p1 to p4 by file and name. 0310 amends 0297; 0311 amends 0273.
      2. docs/adr/README.md: add the rows for 0310 and 0311 after 0309, in the file's row format, and append `; amended by 0310` to the 0297 row's status cell and `; amended by 0311` to the 0273 row's status cell.
      3. docs/org-mode-setup-guide.md: add `### Signing Forgejo in with groups` as the last `###` of section 4 (after "Built-in Google sign-in", before `## 5. Connector apps`) and one line for it in the Contents list if the list names `###` sections (check lines 12-28; skip if it does not). It holds: the PrivacyFence side (`PRIVACYFENCE_OIDC_CLIENTS=forgejo`, the three `PRIVACYFENCE_OIDC_CLIENT_FORGEJO_*` variables with `_GROUPS=true` and `_GROUPS_ALLOWED=kh-*`, a group-map example in `admin_config.json` that maps `kh-mgmt@example.org` to `[]` and `kh-hr@example.org` to `["kh-hr"]`), the Forgejo side as a fenced `bash` command `forgejo admin auth add-oauth --name privacyfence --provider openidConnect --key forgejo --secret "$SECRET" --auto-discover-url https://pf.example.org/oidc/.well-known/openid-configuration --scopes "openid email profile groups" --group-claim-name groups --group-team-map '{"kh-mgmt@example.org":{"kh":["canonical-mgmt"]},"kh-hr@example.org":{"kh":["canonical-hr"]}}' --group-team-map-removal` with the sentence "These Forgejo flags are written against a recent Forgejo; check against your Forgejo version", and the limits (sign-in only, removal at the next sign-in, short session lifetime, additions to a restricted audience wait on Admin → Group membership). Link oidc-provider.md#groups and ADR 0311.
      4. CHANGELOG.md: under `## [Unreleased]` `### Added`, after the ADR 0300 bullet, add two bullets in the file's style: one for `email_for_visibility` (what the operator sets, what the claim carries, that it stays audit-only otherwise, link to the contract section and ADR 0310) and one for the `groups` claim (the variables, approved membership only, the sign-in limit, link to the guide section and ADR 0311). No version heading.
    acceptance:
      - test -f docs/adr/0310-email-is-an-opt-in-visibility-input-of-the-identity-claim.md && test -f docs/adr/0311-the-oidc-provider-sends-approved-group-membership-to-apps-that-opt-in.md succeeds
      - grep -n "0310\|0311" docs/adr/README.md matches two new rows and the two amended cells
      - grep -n "group-team-map-removal" docs/org-mode-setup-guide.md matches
      - grep -n "email_for_visibility\|_GROUPS" CHANGELOG.md matches under ## [Unreleased] and no `## [8.` heading was added
```
