# ADR 0329: Hosted MCP servers sign in each person with their own OAuth, and the claim-based upstream feature is removed

## Status

Accepted — 2026-11-19. Implemented.
Supersedes [ADR 0285](0285-upstream-mcp-servers-are-declared-connectors.md),
[ADR 0286](0286-privacyfence-sends-the-upstream-a-signed-caller-claim.md),
[ADR 0287](0287-data-domains-come-from-grants-on-a-verified-email-and-tokens-only-narrow-them.md),
[ADR 0288](0288-the-upstream-mcp-declaration-can-be-yaml-or-inline-and-seeds-the-grants.md),
[ADR 0297](0297-the-upstream-claim-is-an-eddsa-identity-claim-in-its-own-header-bound-to-one-call.md),
[ADR 0298](0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md),
[ADR 0299](0299-group-membership-is-updated-through-a-scoped-token-tool.md),
[ADR 0310](0310-email-is-an-opt-in-visibility-input-of-the-identity-claim.md),
[ADR 0325](0325-an-upstream-can-be-the-source-of-its-own-audiences.md),
[ADR 0326](0326-a-service-claim-may-call-the-declared-audiences-tool.md).
Relates to [ADR 0330](0330-oauth-upstreams-discover-their-server-register-a-client-and-keep-tokens-per-person.md),
[ADR 0331](0331-the-administrator-manages-each-upstream-servers-tools.md),
[ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
[ADR 0300](0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md).
[ADR 0323](0323-the-oidc-provider-sends-no-group-membership.md) stays as it is. Issue
[#1155](https://github.com/privacyfence/privacyfence/issues/1155); the record of the change is
[`docs/1155/plan.md`](../1155/plan.md) and its `deviations.md`.

## Context

An upstream MCP server was a server the operator ran or trusted to read a signed *caller claim*
(ADR 0286, 0297): PrivacyFence vouched for who was calling, and the upstream decided what that person
could see from audiences PrivacyFence kept (ADR 0298, 0299, 0325). The hosted MCP servers people actually
want behind PrivacyFence (Canva, Framer and similar) cannot read such a claim, so the feature served only
servers written for it. Those servers all offer OAuth 2.1 with discovery, and each person already has an
account there. The result of an OAuth sign-in is the thing the vendor trusts, so PrivacyFence should use it
instead of asking the vendor to trust a new header.

## Decision

1. **Each person signs in at the vendor.** An upstream is declared by the operator (`id`, `label`, `url`,
   optional `oauth`), and each person presses **Connect** for it on the Connect page. PrivacyFence then
   calls the vendor with that person's own bearer token ([ADR 0330](0330-oauth-upstreams-discover-their-server-register-a-client-and-keep-tokens-per-person.md)).
   No claim header is ever sent.
2. **The claim-based feature is removed, a breaking change.** The claim, its signing keys, the audiences
   and the group-membership connector and tool (`privacyfence_update_group_membership`) are deleted, with
   the verifier in `contrib/`, the reference upstream that read the claim, the examples and the
   deployment lines. The ten ADRs above are superseded; their reasoning stays as the trail.
3. **The operator file is version 2.** `version: 2` and at most 8 `upstreams`; `tools`, `audience`,
   `identity_claim` and the top-level `claim` and `membership` are `is not a known key`. A version 1
   file is refused at start with `version: this release reads version 2 only; version 1 files (tools,
   identity claim, audiences) are no longer supported`. Nothing is migrated. The loader (file or inline
   variable, never both, at most 256 KiB, repeated keys refused) is the old one; see
   `src/privacyfence/upstream_mcp_config.py`.
4. **What the removal leaves, and what is done about it.** Nothing is deleted by the server.
   - *On disk:* `org/upstream_claim_keys.json`, `org/group_membership.json` and an `upstream_mcp`
     section in `admin_config.json`. At every start the daemon logs one warning for each it finds
     (`Left over from the removed claim-based upstream feature: …`); the files are never read.
   - *In scoped tokens:* a token that names `privacyfence_update_group_membership`, carries an
     `upstream_audiences` key or names an upstream tool that is not registered loads unchanged and is not
     rewritten without that part. An unknown tool name stays in the token, so loading before the
     runtime has registered the saved lists does not damage it, and it never appears in `tools/list`.
     Approvals of the old feature are void, because their digests were of the old declarations.
   - *In the audit log:* the legacy fields stay in `AuditEntry` and are never written by the new code,
     so old lines keep reading and the log's integrity checks still pass.
5. **No Disconnect.** Owner decision. **Connect** on a connected upstream reads **Reconnect** and
   overwrites the token file; a person leaves a vendor by removing the grant there. A Disconnect that
   only deleted PrivacyFence's copy would suggest the vendor's grant was gone.
6. **The principal-type check runs for every call.** `allowed_principal_types` (`claude_client`,
   `n8n_workflow`; both by default) is checked by `admit_caller` before each call, so the old gap where
   `identity_claim: off` skipped it is gone with the claim.
7. **A scoped token acts as its creator at the vendor.** Owner decision. A scoped token (or a workflow
   using one) calls through the connection of the person who created it, and refreshes through that
   person's refresh token with nobody present. The ADR 0300 tick and the per-token rules of
   [ADR 0331](0331-the-administrator-manages-each-upstream-servers-tools.md) still gate which tools the
   token may call, and the token form says whose connection it uses. A token whose creator has not
   connected, or whose connection has lapsed, gets the connector's `not_connected` or `auth_expired`
   message.

## Alternatives considered

- **Keep the claim feature beside the OAuth one.** Owner decision, dropped: two trust models in one
  connector, and the claim feature served no vendor that exists.
- **Migrate version 1 files.** A version 1 file declares tools and audiences that have no meaning under
  OAuth, so there is nothing to carry over; a clear refusal is cheaper than a silent partial reading.
- **Delete the leftover files at start.** The server would be deleting signing keys and group data an
  operator may want to keep or archive; a warning is enough.
- **A Disconnect button.** Owner decision, left out (see above).
- **A service account at the vendor for scoped tokens.** Hosted vendors have no such notion; acting
  as the creator keeps the vendor's own authorization in force.

## Consequences

- Any hosted MCP server with OAuth discovery can sit behind PrivacyFence without a change at the vendor.
- The vendor, not PrivacyFence, decides what each person may see; PrivacyFence decides which tools run,
  with what gate, and who is asked.
- Operators of the old feature must rewrite their file and clean up the leftovers by hand.
- A person who leaves is not disconnected by PrivacyFence; the vendor-side grant is what ends access.

## Verification

- `tests/unit/test_upstream_mcp_config.py`: the version 2 rules, the version 1 refusal, the leftover warnings.
- `tests/unit/test_upstream_mcp_runtime.py`: `admit_caller` and the principal-type check for every call.
- `tests/unit/test_scoped_tokens.py`, `tests/unit/test_audit_log.py`: old tokens and old audit lines still load.
- `tests/integration/test_upstream_oauth_org.py`: a person's call and a scoped token's call through the creator's connection.

## Related

Issue [#1155](https://github.com/privacyfence/privacyfence/issues/1155),
[ADR 0330](0330-oauth-upstreams-discover-their-server-register-a-client-and-keep-tokens-per-person.md),
[ADR 0331](0331-the-administrator-manages-each-upstream-servers-tools.md),
[ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
[ADR 0300](0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md),
[ADR 0323](0323-the-oidc-provider-sends-no-group-membership.md).
