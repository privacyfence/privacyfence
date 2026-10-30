# ADR 0259: Organization servers use the built-in apps through a domain-verified relay

## Status

Accepted — 2026-10-30. Implemented: `cloudflare/oauth-relay/`, `src/privacyfence/relay_client.py`.
Relates to [ADR 0011](0011-org-mode-runs-its-own-oauth-authorization-server.md) and [ADR 0186](0186-the-download-worker-keeps-no-request-logs.md).

## Context

A built-in app ([ADR 0255](0255-built-in-oauth-apps-ship-in-every-distribution.md)) is one OAuth client
shared by every server, so its redirect URI cannot be each server's own address: a provider app holds
a fixed list of redirect URIs, and Atlassian exactly one. Slack, Miro and Atlassian also refuse to
distribute an app with an `http` redirect. Something at one fixed `https` address has to receive the
provider's redirect and hand it on to the server that started the sign-in, without being able to
send a sign-in code to a server that merely asks.

## Decision

- **A PrivacyFence-hosted redirect relay at `https://oauth.privacyfence.eu`,** a Cloudflare Worker with
  one SQLite-backed Durable Object (`cloudflare/oauth-relay/`). It is deployed from `main` by
  `.github/workflows/deploy-oauth-relay-worker.yml` on its own track, never by a release tag, and its
  contract only grows: every relay version keeps serving every released 7.x server, and a breaking
  change is a new path with the old one kept.
- **It forwards only to a host that proved control of its name.** A server serves a random token at
  `/.well-known/privacyfence-relay` (the route answers 404 unless a relayed service is in use) and
  registers with `POST /register`; the relay fetches the token with redirects off and a 5-second
  timeout. Only a successful proof creates a registration. The server renews daily; a registration
  older than 35 days is treated as unregistered.
- **What is relayed.** `GET /callback/<grant>` for the connector grants and the three sign-in grants
  of [ADR 0267](0267-an-organization-server-may-opt-in-to-the-built-in-google-sign-in.md). The `state`
  the server sends is `<host>~<random>`; the relay answers 302 to
  `https://<host>/oauth/callback/<grant>` with the original query unchanged. It forwards only a request
  with exactly one `state` and exactly one `code` or one `error`, so a token returned in a URL
  fragment is never carried on to a host. Anything else is a 400 with a text that names the relay
  terms.
- **Host rules.** An `https://host[:port]` issuer with no path; at least two labels; an alphabetic last
  label (which rejects every IP-address form); not `localhost`, `.local`, `.internal`, `.home.arpa` or
  `.lan`; and, in the relay, not under a public suffix from the private section of the Public Suffix
  List (`github.io`, `pages.dev`, `workers.dev`, `netlify.app`, tunnelling domains), because anyone
  can create a name there. `relay_client` and the Worker implement the same rules.
- **Loopback without registration.** A state whose host is exactly `localhost` or `127.0.0.1` (optional
  port) is forwarded to `http://<host>/oauth/callback/<grant>` without consulting the registry. The
  browser performs that redirect, so the code reaches only the machine the person is using. A
  loopback host cannot register, and cannot use the sign-in grants.
- **No logs, no public list.** The Worker writes no logs and observability stays off, in the manner of
  [ADR 0186](0186-the-download-worker-keeps-no-request-logs.md); the runbook checks that no Logpush job
  or logging rule covers the host. `GET /registrations` needs the maintainer bearer token and answers
  401 without it, and the privacy page does not mention a list. Maintainers can revoke a host
  (`POST /admin/revoke`) and restore it (`POST /admin/restore`, after which the host must prove
  control again).
- **Registrations are limited and notified.** Failed attempts are counted apart from registrations and
  never block a later valid proof; there are per-host-and-address and per-address limits, and at most
  five new hosts per address per day (`too_many_new_hosts`). Each first registration emails the
  maintainers through the Gmail API with a send-only token from an account restricted to internal
  delivery; there is no waiting period, because review and revocation cover a bad host. At most 50
  such emails go out per day, and a registration that sent none is flagged `"notified": false` on
  `/registrations`.
- **Deploy control.** The relay lives in the existing Cloudflare account; a Workers deploy token
  covers every Worker in it, so the `oauth-relay-worker` GitHub environment has required reviewers
  with self-review prevented.
- **The server side.** `relay_client` registers while any section uses the relay, keeps `last_ok_at`
  in `org/relay_status.json` so a restart inside the 35 days keeps relayed sign-in open, and treats
  `too_soon` and `too_many_new_hosts` as "retry later" without clearing an earlier success. Until a
  registration has succeeded, a sign-in that would go through the relay is refused on the Connect
  page. `org/relay_token.txt` is created if missing and is kept: replacing a token after a recorded
  success logs an error.
- **Terms.** The relay is offered as is, under the relay terms published at https://privacyfence.eu/relay-terms/, with
  no warranty and liability limited as far as the law allows.

## Alternatives considered

- **No relay; every server registers its own provider apps.** That is the situation this removes.
- **A list of servers approved by a maintainer.** Rejected: it does not scale and makes a person the
  availability of every installation.
- **Forward to any server that asks.** Rejected: anyone could receive a sign-in code addressed to
  another server.

## Consequences

- An organization server must be reachable from the internet at the well-known path (and so allowed
  through any IP restriction), have an `https` issuer with no path and outbound HTTPS to the relay. A
  server that cannot is limited to its own apps.
- **Residual risks, accepted and documented.** Slack and Miro send no PKCE, so a relayed code passes
  through the Worker in the browser redirect; the Worker stores nothing and the code is single-use,
  but a compromised relay could exchange it first. Anyone who owns a public domain can register a
  server and run sign-ins under the PrivacyFence name; domain proof makes that cost a domain and a
  running server, and revocation removes a reported host. A compromised deploy path could change the
  relay; required reviewers are the control. The Gmail token that sends notifications could send mail
  as its alias; the sending account's outbound mail is restricted to PrivacyFence's own domains, and
  its rotation is in the runbook.
- The relay terms are not legal advice and are to be reviewed by a lawyer.

## Verification

`cloudflare/oauth-relay/test/` (`relay.test.ts`, `validate.test.ts`, `notify.test.ts`,
`notify-cap.test.ts`), `tests/unit/test_relay_client.py`, `tests/unit/web/test_routes_connect.py`.
Code: `cloudflare/oauth-relay/src/`, `src/privacyfence/relay_client.py`,
`.github/workflows/deploy-oauth-relay-worker.yml`. Runbook: `docs/oauth-relay.md`.

## Related

ADR 0011, 0186, 0255, 0267.
