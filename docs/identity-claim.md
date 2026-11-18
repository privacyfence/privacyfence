# The PrivacyFence identity claim (contract)

The contract between PrivacyFence and an upstream MCP server. It says what a signed claim contains, how
it is tied to one tool call, and what an upstream must check before it trusts it. Read it when you
write or review an upstream's verifier, or when a claim is refused and you need to know why. How an
operator declares an upstream is in [upstream-mcp.md](upstream-mcp.md). The claim format is
`claim_version` 1.

## Purpose

PrivacyFence decides who a caller is and which audiences they hold. It proves that to an upstream with
a short signed claim bound to one tool call, so the upstream can enforce the audiences in its own
queries. PrivacyFence holds the approvals and the audit trail; the upstream holds the data and never
needs to see PrivacyFence's other state. PrivacyFence never filters an upstream's results.

## Transport

- The claim is sent in the HTTP header `X-PrivacyFence-Claim`, on every HTTP request PrivacyFence makes
  to the upstream's MCP endpoint, a new claim each time. One tool call is several HTTP requests
  (`initialize`, `notifications/initialized`, `tools/call`, session delete).
- `Authorization` is never set or read by PrivacyFence. It is free for the upstream's own use.
- A claim is a compact JWT (JWS). Answer `401` with no detail for any failure.

## The JWT

Header:

```json
{"alg": "EdDSA", "kid": "<kid>", "typ": "pf-identity-claim+jwt"}
```

`alg` is exactly `EdDSA` (Ed25519). Refuse `none`, HMAC and RSA algorithms. `typ` is exactly
`pf-identity-claim+jwt`, which keeps a claim apart from the OpenID Connect tokens PrivacyFence also signs.

Payload. A key is omitted, never `null`, when it does not apply.

| Claim | Value |
|---|---|
| `claim_version` | `1` (always present). |
| `iss` | PrivacyFence's issuer URL with no path suffix, for example `https://pf.example.org`. |
| `aud` | The `audience` the operator declared for this upstream. Accept only your own. |
| `sub` | The principal's id: the person's id, `n8n-token:<token id>` for a scoped token, `privacyfence` for a service claim. |
| `principal_type` | `claude_client`, `n8n_workflow`, `service` or `human` (see Principals). |
| `email` | The verified email. Present only when `sub` is a person and the address is verified. With `email_for_visibility: true` it is lower-cased and NFC (ASCII only: an address with a non-ASCII character gives no `email`). |
| `approved_by` | The id of the person who created the token. Present only for `n8n_workflow`. |
| `token_id` | The scoped token's id. Present only for `n8n_workflow`. |
| `act` | `{"sub": <OAuth client id>, "agent": <attested agent id>}` for `claude_client` (the RFC 8693 actor). `agent` only when the client is attested; `sub` only when known. Absent for any other type. |
| `audiences` | Sorted list of strings; `[]` when none or unresolved. Always present. |
| `tools` | Sorted list of the upstream's own tool names this principal may call on this upstream. Always present. **Information only**: never use it to decide what a query returns. |
| `req` | The request binding. Present on a `tools/call` request only (next sections). |
| `iat`, `exp`, `jti` | Timing and the replay id. `jti` is a UUID string. |

`iss` has no path suffix. The public keys are at `<issuer>/upstream-mcp/jwks`.

`sub`, `principal_type`, `audiences` and `req` may change what a query returns, and `email` too, but only
when the upstream declared `email_for_visibility: true`
([Email as a visibility input](#email-as-a-visibility-input-opt-in)). `approved_by`, `token_id` and
`act` are for your own audit.

## Principals

| `principal_type` | Meaning | `sub` |
|---|---|---|
| `claude_client` | A person's AI client acting for them. | The person's principal id. `email` is the verified address; `act` names the client. |
| `n8n_workflow` | Any scoped token, whether delivered to n8n or not. | `n8n-token:<token id>`. No `email`: a workflow does not act live for a person, so it cannot match attendee-only rows. `approved_by` is the creator, for audit. |
| `service` | PrivacyFence's own requests: the startup tool listing and, when the operator declared `audiences_tool`, the call of that tool. | `privacyfence`, with `audiences: []`. Return the tool list and no data, except for the audiences tool ([below](#the-audiences-tool-opt-in)). |
| `human` | Reserved. PrivacyFence never mints it. | Refuse it unless you have a use for it. |

An operator can limit an upstream to a subset of `claude_client` and `n8n_workflow`
(`allowed_principal_types`); a caller of another type is refused before anything is sent.

## Audiences

`audiences` is the union of the audiences of the groups the person belongs to, from the verified email
only (an unverified or missing email gives `[]`). `all` is the one unrestricted audience; every other
audience is restricted. A scoped token carries `owner's audiences now ∩ the audiences chosen at creation`,
so a creator who loses an audience takes it from the token on its next call.

Filter by `audiences` inside your own query, not afterwards, so ranking and pagination stay correct and a
guessed document id is still refused. An empty list matches no row.

For an upstream that declared `audiences_tool`, a person's audiences come from that tool's answer instead of the groups.

## Email as a visibility input (opt-in)

By default `email` is for your audit only. An operator can make it a fifth input to what a query returns
by declaring `email_for_visibility: true` on the upstream ([upstream-mcp.md](upstream-mcp.md)), for
example so a knowledge hub can show an attendee-only record to the people on its attendee list. With the
opt-in on, PrivacyFence promises, for every request it signs:

- `email` is present only when `principal_type` is `claude_client`. A scoped token (`n8n_workflow`) and a
  `service` claim never carry it, opted in or not.
- It is the verified address only. "Verified" is what PrivacyFence treats as verified at sign-in: the
  identity provider said so, or the operator trusts unverified addresses for an allowed domain
  (`PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL`), which makes the address count as verified for that domain.
  An address that is not verified gives no `email`.
- It is lower-cased, in Unicode NFC, and ASCII only. An address with a non-ASCII character (a look-alike
  such as the Kelvin sign, or a genuine internationalised address) gives no `email` and no `audiences`; the sign-in itself still works.
- It comes from the same verified source and goes through the same normaliser as `audiences`, so the
  address in `email` is the address that decided the audiences.
- `sub` stays when the person's address changes, and `email` follows.

What the upstream does:

- Filter inside your own query by exact match of `email` against your own list (an attendee list, say),
  as for `audiences`, so ranking and pagination stay correct.
- Use `email` only on a `tools/call` whose `req` matched. PrivacyFence also sends `email` on the other
  requests of a person (`initialize`, `tools/list` and so on); never return data for those.
- An absent `email` matches nothing.

An upstream that did not opt in must keep treating `email` as audit-only.

## The audiences tool (opt-in)

An operator can declare `audiences_tool: <name>` on an upstream ([upstream-mcp.md](upstream-mcp.md#audiences-from-the-upstream)),
so that the upstream tells PrivacyFence who holds each of its audiences. PrivacyFence then sends `tools/call`
of `<name>` with `{}` and a `service` claim, at startup and on an interval. The claim's `req` is bound to
that call, as for any `tools/call`, and its `tools` is `["<name>"]` (information only, as everywhere: never
decide on it). The upstream answers with the table **only** when all of these hold:

- the claim verified, and `principal_type` is `service` and `sub` is `privacyfence`;
- `req` matched the request body;
- the tool name in `params.name` is its own audiences tool.

Every other request with a service claim still gets no data. The answer, as structured content or as the
text of the result, is a JSON object with exactly the keys `version` (the number 1) and `audiences` (an
object of audience name to a list of addresses):

```json
{"version": 1, "audiences": {"mgmt": ["ann@example.com", "bob@example.com"], "finance": ["bob@example.com"]}}
```

It holds audience names and addresses only, never documents. Audience names are 1-63 characters (`a-z`,
`0-9`, `_`, `-`, starting with a letter or digit), at most 200 of them, at most 20000 addresses in all, and
the answer is at most 1 MB; anything else is refused as a whole, and the previous answer stays. Addresses
should be the lower-cased form of the identity provider's verified address: PrivacyFence applies the same
normaliser as for `email` (ASCII only) and drops, counts and ignores any address that does not pass. The
name `all` is an ordinary name here: the upstream may list anyone under it, and there is no approval.
A change to the shape of the answer bumps the answer's own `version`, not `claim_version`
([ADR 0326](adr/0326-a-service-claim-may-call-the-declared-audiences-tool.md)).

## The request binding

`req` ties a claim to one `tools/call`:

```
req = base64url_nopad(SHA-256(canonical_request(tool, arguments)))
```

`canonical_request` is the UTF-8 bytes of

```python
json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True,
           separators=(",", ":"), ensure_ascii=False, allow_nan=False)
```

1. `tool` is the upstream's own tool name (`search`, not `knowledgehub_search`).
2. `arguments` is the `params.arguments` object of the `tools/call` request, without `reason`; an absent
   object is `{}`.
3. Keys are sorted by Unicode code point, at every depth. Separators are `,` and `:` with no whitespace.
4. Non-ASCII text is written as UTF-8, never as `\uXXXX` escapes. Control characters, `"` and `\` use the
   JSON short escapes.
5. Integers are decimal, with no limit on size. Other numbers use Python's shortest round-trip `repr`
   (`1.5`). This is the one place another language can differ: check it against the vectors.
6. `NaN` and `Infinity` are not valid.
7. Duplicate keys in the request body are refused by the verifier.

The six vectors below are the contract; they are also in
[`contrib/verifier/req_test_vectors.json`](../contrib/verifier/req_test_vectors.json), computed
independently of any implementation. An implementation that reproduces all six has the canonicalisation
right.

| Name | Canonical text | `req` |
|---|---|---|
| empty-arguments | `{"arguments":{},"tool":"search"}` | `_jS9stFeCkVA2Ed7FqOzjA5kl4RdO11hSbb_QDrlYEw` |
| key-order-is-sorted | `{"arguments":{"limit":5,"query":"policy"},"tool":"search"}` | `L1g2NbgUryfvpWA8DUp2BKfiUaA39GNhc2N-7LbSmaI` |
| nested-and-arrays | `{"arguments":{"filters":{"a":null,"b":[3,1,2]},"id":"d-42"},"tool":"get_doc"}` | `77eDoXOlibw6zBWIRyXkbHv9q8uxz-RlZvQN0u_N85w` |
| unicode-not-escaped | `{"arguments":{"query":"Größe café 日本語"},"tool":"search"}` | `opa6QE9p8-IKxdFU_PDTAeX0wSPlUiO2_Pj8iQXi12A` |
| astral-and-escapes | `{"arguments":{"query":"line\nbreak \"quoted\" \\ 😀"},"tool":"search"}` | `fBlyH6L_1srMHjsC-c6KcTLGRwLYc7XmaqdElibG-9Y` |
| numbers-and-booleans | `{"arguments":{"big":12345678901234567890,"f":1.5,"n":0,"neg":-7,"no":false,"ok":true},"tool":"list"}` | `j0MCtwK9UI17zZyw2aimBWRq8vxDl5ODknEIYcJkp0M` |

In the `astral-and-escapes` row, `\n`, `\"` and `\\` are the two-character JSON escapes, and the emoji is
written as UTF-8.

**What the rules mean for the upstream**

- A `tools/call` claim carries `req`. Refuse a `tools/call` without a matching `req`, with a different
  one, or a `req` on any other request.
- Every other request (`initialize`, `notifications/initialized`, session delete, `tools/list`) carries
  `audiences: []`, `tools: []` and no `req`. Return no audiences for it and no data.
- A JSON-RPC batch (a JSON array body) is refused. A claim covers exactly one request.
- Only the tool name and the arguments are bound. `params._meta` is not. **Dispatch only the tool name
  and arguments you hashed**, and ignore everything else in the request.

A captured claim therefore cannot be replayed for another call, another argument set, or a non-call
request.

## Lifetime and clock skew

`exp - iat` is the upstream's `claim_lifetime_seconds`: 5 to 60, default 60. Refuse a claim whose
lifetime is above 60 seconds or not positive. Allow clock leeway of at most 5 seconds on `exp` and `iat`.
Keep a `jti` cache for the claim's lifetime plus the leeway and refuse a `jti` you have seen.

## Keys and rotation

- Public keys are served at `<issuer>/upstream-mcp/jwks` (public, no sign-in) as an OKP/Ed25519 JWKS:
  `{"kty": "OKP", "crv": "Ed25519", "x": ..., "use": "sig", "alg": "EdDSA", "kid": ...}`. The `kid` is the
  first 16 characters of the base64url SHA-256 of the raw public key.
- The response may be cached for 300 seconds. Honor the `kid`; on an unknown `kid`, refetch once, and not
  more often than every 30 seconds.
- Rotation (`privacyfence-app --rotate-upstream-claim-key`) publishes the new key at once but it starts
  signing 300 seconds later, so a verifier that caches for 300 seconds has it before it signs. The previous
  key keeps signing until then and stays in the JWKS for the operator's overlap (`claim.key_overlap_hours`,
  default 24 hours), so a claim already minted still verifies.
- `--revoke-upstream-claim-key KID` removes a key from the JWKS at once. After the cache expires and one
  refetch, a claim signed by it is an unknown `kid`.

## Verification checklist

Do these in order and answer `401` on the first failure:

1. The header `alg` is exactly `EdDSA` and `typ` is `pf-identity-claim+jwt`.
2. The header `kid` is in the JWKS (after at most one refetch) and the key is an Ed25519 OKP key.
3. The signature is valid.
4. `iss` equals your PrivacyFence issuer URL and `aud` equals your own audience.
5. `exp`, `iat` and the leeway: not expired, not issued in the future, lifetime at most 60 seconds.
6. `claim_version` is `1`.
7. `principal_type` is one you know.
8. `audiences` and `tools` are lists of strings.
9. The request body: parse it with duplicate keys refused; refuse a batch; for a `tools/call`, `req` must
   equal the hash of the tool name and arguments in this body; for any other request there must be no `req`
   and you take no audiences.
10. The `jti` has not been seen.

`claim_version` and the checks above are what `pf_claim_verify.py` implements.

## Using `pf_claim_verify.py`

[`contrib/verifier/pf_claim_verify.py`](../contrib/verifier/pf_claim_verify.py) is one file you vendor into
your server. It needs Python 3.9 or later, the standard library, PyJWT and `cryptography`.

```python
from pf_claim_verify import CLAIM_HEADER, ClaimError, ClaimVerifier

verifier = ClaimVerifier(
    issuer="https://pf.example.org",
    audience="knowledge-hub",
    jwks_url="https://pf.example.org/upstream-mcp/jwks",
)

def authenticate(headers, body: bytes):
    try:
        principal = verifier.verify(headers[CLAIM_HEADER], body=body)
    except ClaimError as exc:
        raise PermissionError(exc.code)  # answer 401, log the code, never the claim
    return principal  # principal.audiences is () unless body is a tools/call
```

`verify(token, *, body=b"")` takes the raw request body. `verify_tool_call(token, tool, arguments)`
checks a call you already parsed. The returned `Principal` is a frozen dataclass with `sub`,
`principal_type`, `email`, `audiences`, `tools`, `approved_by`, `token_id`, `act`, `jti`, `expires_at` and
`claim_version`. A replayed `jti` is refused by default; plug a shared store in through `replay_cache`
(an object with `add_if_new(jti, expires_at) -> bool`) when you run several processes.

## Row-level security sketch (PostgreSQL)

Set the caller's audiences once per transaction and let the database enforce them:

```sql
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;

CREATE POLICY documents_by_audience ON documents
  USING (audience = ANY (string_to_array(current_setting('app.audiences'), ',')));
```

```sql
BEGIN;
SET LOCAL app.audiences = 'all,hr';   -- the claim's audiences, comma-joined, after verification
SELECT id, title FROM documents WHERE body ILIKE '%policy%';
COMMIT;
```

Bind the list as a parameter (`SELECT set_config('app.audiences', $1, true)`), never by string
concatenation. An empty list must give an empty `app.audiences`, which matches no row. Connect as a role
that is not the table owner and has no `BYPASSRLS`.

## Versioning

This document describes `claim_version` 1. Adding an optional claim keeps the version at 1, so ignore
claims you do not know. An opt-in the operator declares per upstream, such as `email_for_visibility`,
changes nothing for an upstream that does not opt in, so it keeps the version at 1. The audiences tool is such an opt-in too: it keeps the version at 1, and a later change to the shape of its result bumps the result's own `version`. Once an upstream
has opted in, though, any change to how `email` is filled (its source, its normalisation, the cases
where it is present) is a breaking contract change: it needs `claim_version` 2 and a new `typ`. Any other change (a changed meaning, a removed or newly required claim, another
canonicalisation) is `claim_version` 2 and a new `typ`, so a verifier for version 1 refuses it by
design.
