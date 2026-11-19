# ADR 0310: `email` is an opt-in visibility input of the identity claim

## Status

Accepted — 2026-11-15. Implemented: `src/privacyfence/group_membership.py` (`lookup_email`),
`src/privacyfence/upstream_mcp_config.py`, `src/privacyfence/upstream_mcp_runtime.py`.
Amends [ADR 0297](0297-the-upstream-claim-is-an-eddsa-identity-claim-in-its-own-header-bound-to-one-call.md).
Relates to [ADR 0298](0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md).
Issue [#1133](https://github.com/privacyfence/privacyfence/issues/1133).

**Superseded by [ADR 0329](0329-hosted-mcp-servers-sign-in-each-person-with-their-own-oauth-and-the-claim-feature-is-removed.md), 2026-11-19.**

## Context

ADR 0297 made `sub`, `principal_type`, `audiences` and `req` the claim fields an upstream may use to decide
what a query returns, and called `email` audit-only. The knowledge hub now needs to show attendee-only
records by the person's address. The `email` in the claim is whatever the identity provider returned, so it
is not lower-cased, and an address with a look-alike character (the Kelvin sign U+212A, the Angstrom sign,
`İ`, a zero-width space) could differ from the address a list holds without anyone noticing. If `email`
becomes a visibility input, its filling is a contract, and it has to be the same address by construction as
the one `audiences` is looked up by.

## Decision

1. **A per-upstream opt-in.** The upstream declaration gets `email_for_visibility`, a boolean, default
   `false`. A value that is not exactly `true` or `false` is refused, and so is `true` together with
   `identity_claim: off`. Without the opt-in the claim is byte-for-byte what it was, and `email` stays
   audit-only.
2. **What the claim carries when opted in.** For a `claude_client` person whose address is verified, `email`
   is the address, stripped and lower-cased. A scoped token (`n8n_workflow`) and a `service` never carry
   `email`, opted in or not. `sub` stays when the person's address changes and `email` follows.
3. **"Verified" is what PrivacyFence treats as verified at sign-in.** That is `principal.email_verified`:
   the identity provider said so, or the operator trusts unverified addresses for an allowed domain
   (`trust_unverified_email`, ADR 0273, ADR 0298 Consequences). ADR 0298's consequence carries over to
   email visibility unchanged, and the contract says so.
4. **One normaliser, ASCII only.** `group_membership.lookup_email` returns `None` unless the value is a
   string with exactly one `@`, a non-empty local part and domain, no whitespace or control character, and
   only ASCII characters; otherwise it returns the lower-cased, stripped address. The claim `email`, the
   `audiences` lookup and the `groups` lookup (ADR 0311) all use it, so they are the same address by
   construction. ASCII is the rule `org_identity` already applies, and it closes every look-alike without a
   case list; an ASCII address is already NFC. An address with a non-ASCII character gets no `email`, no
   `audiences` and no `groups` (default deny). **This also tightens `audiences`**: before, an audience
   lookup used `strip().lower()` on any string; now a non-ASCII address holds no audience.
5. **The key is not in the declaration digest.** A scoped token never carries `email`, so no ADR 0300
   approval depends on it.
6. **`claim_version` stays 1.** The opt-in is declared by the operator per upstream and changes nothing for
   an upstream that does not opt in, as for an added optional claim. Any later change to how `email` is
   filled when the opt-in is on (the source, the normalisation, the cases where it is present) is a
   breaking contract change and needs `claim_version` 2 and a new `typ`. The contract (`docs/identity-claim.md`)
   says so.
7. **The verifier does not change.** `contrib/verifier/pf_claim_verify.py` already passes `email` through,
   so no hash is republished.
8. **What an upstream does with it.** It filters inside the query by exact match against its own list, uses
   `email` only on a `tools/call` whose `req` matched (as for `audiences`), and treats an absent `email` as
   matching nothing.

## Alternatives considered

- **Always sending a lower-cased email.** It would change every upstream's claim, including ones that
  treat `email` as audit-only, and turn the field into a visibility input for people who never asked.
- **A separate claim name** (`visibility_email`). The upstream would then hold two addresses for one person
  and could confuse them; the opt-in per upstream is the smaller change.
- **Loosening to unverified addresses.** An unverified address is a claim by the person, so it may not
  decide what they see. The operator's `trust_unverified_email` is the one explicit exception, and it is the
  operator's.
- **Changing the verifier.** Unneeded: the verifier passes `email` through, and a change would force every
  upstream to republish a hash.
- **A case list of look-alike characters** instead of ASCII only: it is never complete, and an
  internationalised address that does not match a list is better denied than guessed.

## Consequences

- An operator turns the opt-in on per upstream and the hub (or any upstream) can filter by address with the
  contract's promises.
- A genuine internationalised address gets no `email`, no `audiences` and no `groups`; the sign-in itself
  still works. This tightens `audiences` for such addresses.
- With `trust_unverified_email` on for a domain, a person of that domain is "verified" for email
  visibility too, as for audiences.
- A change to how `email` is filled later costs a `claim_version` bump and a new `typ`.

## Verification

- `tests/unit/test_group_membership.py::TestLookupEmail` (`test_lowercases_and_strips`,
  `test_malformed_gives_none`, `test_look_alikes_give_none`).
- `tests/unit/test_upstream_mcp_config.py`: `test_email_for_visibility_defaults_to_false`,
  `test_email_for_visibility_true_is_read`, `test_email_for_visibility_refuses_a_non_boolean`,
  `test_email_for_visibility_needs_the_claim_on` and, in `TestDeclarationDigest`,
  `test_declaration_digest_ignores_email_for_visibility`.
- `tests/unit/test_upstream_mcp_runtime.py::TestCallerFor`: `test_opted_in_a_verified_email_is_lowercased`,
  `test_opted_in_a_non_ascii_email_gives_none`, `test_opted_in_an_unverified_email_gives_none`,
  `test_opted_in_a_scoped_token_never_carries_email`; `TestAudiencesForEmail`:
  `test_a_look_alike_address_holds_no_audience`.

## Related

[ADR 0273](0273-privacyfence-is-an-oidc-provider-for-apps-the-operator-names.md),
[ADR 0297](0297-the-upstream-claim-is-an-eddsa-identity-claim-in-its-own-header-bound-to-one-call.md),
[ADR 0298](0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md),
[ADR 0300](0300-an-administrators-tick-approves-a-gated-upstream-tool-for-a-scoped-token.md),
[ADR 0311](0311-the-oidc-provider-sends-approved-group-membership-to-apps-that-opt-in.md).
