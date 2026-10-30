# PrivacyFence OAuth relay

The Cloudflare Worker behind `https://oauth.privacyfence.eu`. PrivacyFence ships OAuth apps of its
own (Google, Slack, Salesforce, Atlassian and Miro, plus a Google sign-in client). A provider
accepts only fixed redirect URLs for an app, but every organization runs its server on its own
domain. The relay is the one redirect URL registered with each provider: it takes the provider's
redirect and forwards it, with the query string unchanged, to the PrivacyFence server that started
the sign-in. It never sees a token, only the one-time authorization code in transit, and it writes
no logs.

The maintainer runbook is [`docs/oauth-relay.md`](../../docs/oauth-relay.md). The terms are at
<https://privacyfence.eu/relay-terms/> and the privacy statement at <https://privacyfence.eu/privacy/#relay>.

## The contract

A server registers with the relay, and later the provider's redirect arrives at the relay.

**Hosts.** The relay serves only an `issuer_url` of the form `https://host[:port]` with no path.
A host is valid when it is at most 253 characters, has at least two dot-separated labels (1 to 63 lower-case letters, digits or hyphens, starting and ending
with a letter or digit), ends in a label of 2 to 63 letters (so no IP address form
and no trailing dot), and carries no port or a decimal port from 1 to 65535 with no leading zero.
`localhost` and names ending in `.localhost`, `.local`, `.internal`, `.home.arpa` or `.lan` are
not public, and neither is a host under a suffix of the Public Suffix List's private section
(`x.github.io`, `a.pages.dev`, `a.workers.dev`, ...).

**`state`.** A relayed sign-in sets `state` to `<host>~<random>`.

| Route | Behaviour |
| ----- | --------- |
| `GET`, `HEAD /health` | 200, body `ok`. |
| `GET`, `HEAD /` and `/terms` | 302 to the relay terms. |
| `POST /register` | `Content-Type: application/json` (parameters allowed). Body `{"host", "secret", "signin_domains"?}`. `secret` is the server's 43 characters of `[A-Za-z0-9_-]`. The relay fetches `https://<host>/.well-known/privacyfence-relay` (no redirects, 5 s, 256 bytes) and compares it with the lower-case hex SHA-256 of `secret`. 200 `{"host", "verified_at", "terms", "signin_domains_verified", "signin_domains_missing", "signin_domains_refused"}`. Errors: 415 `unsupported_media_type`, 400 `invalid_request`, 400 `host_not_public`, 403 `revoked`, 403 `proof_failed`, 429 `too_soon`, 429 `too_many_new_hosts`. |
| `GET /callback/:grant` | `grant` is `gmail`, `drive`, `calendar`, `contacts`, `tasks`, `apps_script`, `slack`, `salesforce`, `atlassian` or `miro`, or one of the sign-in grants `idp`, `idp-login`, `stepup`. 302 to `https://<host>/oauth/callback/<grant>?<the original query>`; the sign-in grants forward to `/oauth/idp/callback`, `/oauth/idp/login-callback` and `/oauth/stepup/callback`. |
| `GET /registrations` | The maintainer token is required. A JSON list of the non-revoked hosts verified within the last 35 days (the ones the relay forwards for), sorted. The notification counters are in the `X-Notify-*` response headers. |
| `POST /admin/revoke`, `POST /admin/restore` | The maintainer token is required. Body `{"host"}`. |

Every response carries `Cache-Control: no-store`, `Referrer-Policy: no-referrer` and
`X-Content-Type-Options: nosniff`.

**Registration.** A host is forwarded to only while it is registered, not revoked and verified
within the last 35 days. A server registers again every day. Only a successful proof creates a
registration.

The well-known path serves only the hash of the secret; the secret itself goes to the relay alone.
So knowing what a host serves is not enough to register it or to change its registration (its
sign-in domains, its `verified_at`): that takes the secret. The relay never stores the secret; it
compares its hash with what the host serves on every request.

**Sign-in domains.** `signin_domains` (at most 20, each a host name with no port; a malformed one is
400 `invalid_request`) are the Google Workspace domains the server signs people in from. A
domain that can never be one (a public suffix such as `co.uk` or `github.io`, or `gmail.com`) is
listed in `signin_domains_refused` and the rest of the request goes ahead. Each other domain is
proven by a DNS TXT record at `_privacyfence-relay.<domain>` with the value

    pf-relay=v1:<lower-case hex SHA-256 of the UTF-8 string "v1|<host>|<proof>">

where `<host>` is the registered host exactly as in the request (lower-case, with `:port` when it
has one) and `<proof>` is the 64-character hex string the host serves at its well-known path. The
host is part of the value, so a record vouches for that one server: another server cannot reuse it,
even with a copy of the proof. The `v1` tag names this format; a different format would get a new
tag, and `v1` records keep their meaning. The relay checks the records with DNS over HTTPS on every
successful registration. A domain verified earlier is checked again even when the request leaves it
out; it is dropped only when DNS answers that the record is gone or has another value (a failed
lookup keeps it). The requested domains are checked first, and at most 20 in all.

**Notification mails.** The maintainers get a mail for a new host and for a host whose verified
sign-in domains gain one that no earlier mail named. Removing a domain, putting a named one back or
repeating a request sends none. One host causes at most 3 mails a UTC day, and all hosts together
at most 50; a registration whose news was not mailed is listed with `"notified": false`.

**Callbacks.** The query must carry exactly one `state` and exactly one `code` or exactly one
`error`, with no repeated parameter; anything else is a 400 with no redirect. A `state` whose host
is `localhost` or `127.0.0.1` (with an optional port) is forwarded to `http://<host>/...` without
consulting the registry, so a single-user install on the person's own computer can use the built-in
apps; the sign-in grants refuse such a host. A sign-in grant carrying a `code` is forwarded only
when Google's `hd` parameter names one of the host's verified sign-in domains.

**Limits.** A failed proof for the same host from the same address blocks a repeat for 60 seconds
(`too_soon`), an address may send 30 register requests in 10 minutes, and at most 5 new hosts a
UTC day. An IPv6 address counts as its /64. A failure never blocks a later valid proof from another
address.

## Revoking a host

A maintainer revokes a host with the maintainer token. The token comes from the GitHub environment
secret `CF_OAUTH_RELAY_REVOKE_TOKEN`, which the deploy workflow stores as the Worker secret
`REVOKE_TOKEN`:

```bash
curl -X POST https://oauth.privacyfence.eu/admin/revoke \
  -H "Authorization: Bearer $REVOKE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"host":"pf.example.org"}'
```

`/admin/restore` takes the same body, lifts the revocation and clears the host's verification, so
the host must register again before it is forwarded to. Everything else is in the runbook.

## Development

```bash
npm ci
npm run typecheck
npm test            # vitest in the workers runtime, with coverage thresholds
npm run dry-run     # bundles the Worker without deploying
```

The registry is a SQLite-backed Durable Object (`src/registry.ts`). The tests call the Worker's
`fetch` handler and stub its outbound `fetch` (the proof, DNS over HTTPS and the Gmail API) with
`vi.spyOn(globalThis, "fetch")`. `src/psl_icann.ts` and `src/psl_private.ts` are generated by
`node scripts/update-psl.mjs`; do not edit them by hand.
