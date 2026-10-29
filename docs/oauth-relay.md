# OAuth relay runbook

How to run the OAuth redirect relay at `https://oauth.privacyfence.eu`: provisioning it, checking a
deploy, reviewing and revoking registrations, rotating secrets and rolling back. The Worker's
contract and development notes are in [`../cloudflare/oauth-relay/README.md`](../cloudflare/oauth-relay/README.md).
The terms are <https://privacyfence.eu/relay-terms/> and the privacy statement is
<https://privacyfence.eu/privacy/#relay>.

The relay is a service PrivacyFence runs, like the download Worker. It is not an artifact operators
install. A provider accepts only fixed redirect URLs for an app, so every organization server that
uses a built-in app sends the provider's redirect to the relay, and the relay forwards it to the
server that proved it controls its own domain.

## How the relay is released

- **Production deploys.** [`deploy-oauth-relay-worker.yml`](../.github/workflows/deploy-oauth-relay-worker.yml)
  deploys the Worker when a change under `cloudflare/oauth-relay/**` reaches `main`, or when someone
  runs the workflow on `main`.
- **Tags do not deploy it.** A release tag never changes the relay, and a relay change never needs an
  app release.
- **Order.** The relay must be live before the first release whose organization servers use built-in
  apps. [`releasing.md`](releasing.md) and the `/cut-release` command check
  `curl -fsSI https://oauth.privacyfence.eu/health` before tagging.
- **Compatibility rule.** Every relay version must keep serving every released server that uses it.
  The contract (`/register`, `/callback/:grant`, the `state` format, the loopback rule) only grows:
  a route or a field may be added, and an existing one is never changed or removed while a supported
  release uses it. A breaking change is a new path (for example `/v2/callback/...`) used by the new
  release, with the old path kept.

## Provisioning

Done once, in this order. The Cloudflare resources are listed in
[`downloads-and-release-kpi.md`](downloads-and-release-kpi.md#cloudflare-resources).

1. **Zone and account.** The relay lives in the same Cloudflare account and zone (`privacyfence.eu`)
   as the download Worker. `oauth.privacyfence.eu` must have no DNS record: the Worker's
   `[[routes]] custom_domain = true` creates it on the first deploy.
2. **Bot protection.** Bot Fight Mode is off for the zone, or a WAF skip rule covers `/register`,
   `/callback/*` and `/admin/*` on `oauth.privacyfence.eu`. Otherwise servers' register calls and
   the providers' redirects are challenged.
3. **The deploy token.** An account-owned API token that can edit Workers scripts and routes, made
   the way [`downloads-and-release-kpi.md`](downloads-and-release-kpi.md#use-account-owned-api-tokens)
   describes and verified before it is stored.
4. **The GitHub environment `oauth-relay-worker`**, under repo Settings → Environments:
   - **Deployment branches:** `main` only.
   - **Required reviewers:** at least one person other than the author of the change, with "Prevent
     self-review" on. A Workers deploy token covers every Worker in the account, so the relay's token
     could replace the download Worker and the other way round. The relay stays in the one account, and
     the required reviewers are the control. Set the same on `downloads-worker`.
5. **The environment secrets**, all in `oauth-relay-worker`:

   | Secret | Holds |
   | ------ | ----- |
   | `CF_OAUTH_RELAY_WORKER_API_TOKEN` | the deploy token |
   | `CF_OAUTH_RELAY_WORKER_ACCOUNT_ID` | the Cloudflare account ID |
   | `CF_OAUTH_RELAY_REVOKE_TOKEN` | the maintainer token (32 or more random characters, for example `openssl rand -base64 32`) |
   | `CF_OAUTH_RELAY_GMAIL_CLIENT_ID`, `CF_OAUTH_RELAY_GMAIL_CLIENT_SECRET`, `CF_OAUTH_RELAY_GMAIL_REFRESH_TOKEN` | the credential for the registration mail (below) |

6. **The first deploy.** Run `Deploy OAuth relay Worker` on `main` from the Actions tab. It deploys
   the Worker, creates the Durable Object class `Registry` from the migration, sets the Worker
   secrets from the environment secrets, and checks `/health`.
7. **The checks below**, then the smoke tests.

### The registration mail

Every first registration emails the maintainers, so a suspicious host is seen the same day. The
Worker sends through the Gmail API, with `fetch` only, as `relay-notify@privacyfence.eu`, to the
constant `info@privacyfence.eu` in `src/notify.ts`. `privacyfence.eu` mail is on Google Workspace,
so Cloudflare Email Routing cannot be enabled on it.

- **The sender.** `relay-notify@privacyfence.eu` is an alias of `test@privacyfence.eu`, set up as a
  Gmail "Send mail as" address. `NOTIFY_FROM` in `wrangler.toml` holds it.
- **The credential.** An OAuth client of type Web application with audience **Internal**, in a Google
  Cloud project of the `privacyfence.eu` Workspace. An Internal app needs no verification and its
  refresh tokens do not expire after seven days.
- **Minting the refresh token.** In the project add
  `https://developers.google.com/oauthplayground` as an authorized redirect URI of the client. In the
  OAuth 2.0 Playground open the settings, tick "Use your own OAuth credentials" and enter the client
  ID and secret. Authorize the single scope `https://www.googleapis.com/auth/gmail.send` signed in as
  `test@privacyfence.eu`, exchange the code, and copy the refresh token. It can send but never read
  mail. Store the client ID, client secret and refresh token as the three `CF_OAUTH_RELAY_GMAIL_*`
  environment secrets, then run the workflow on `main`.
- **Containing a stolen token.** Whoever holds the token can send mail as `relay-notify@` or as
  `test@` itself. So `test@privacyfence.eu` sits in its own organizational unit with a Gmail **content
  compliance** rule for **outbound** mail only (Admin console → Apps → Google Workspace → Gmail →
  Compliance): a message whose envelope recipient is not in `privacyfence.eu` or the maintainer's
  other own domain is rejected. A leaked token cannot phish anyone outside. Use content compliance,
  not "Restrict delivery": that also blocks incoming mail, and the QA account must keep receiving
  the providers' sign-up and verification emails. The QA account never sends mail through
  PrivacyFence; its tests only draft.
- **Rotating the token.** Revoke the old one at <https://myaccount.google.com/permissions> as
  `test@privacyfence.eu`, mint a new one as above, update the environment secret and run the workflow
  on `main`.
- **When it fails.** `GET /registrations` returns the counters in the `X-Notify-Failures`,
  `X-Notify-Last-Error` and `X-Notify-Skipped` headers; the last error is a short code
  (`token_invalid_grant`, `token_http_<status>`, `send_http_<status>`, `network`). `token_invalid_grant`
  means the refresh token was revoked, for example after a password change on `test@`: mint a new
  one. A registration is never refused because its mail failed, and a registration whose mail was
  not sent is listed with `"notified": false`.
- **The limit.** At most 50 mails a UTC day. The first registration over the limit sends one mail,
  `More new registrations today than the notification limit; see /registrations.`, and the rest of
  the day sends none, so review the `notified: false` rows.

### The Worker's checks

Run these at setup and on every review:

- No Logpush job and no logging WAF rule cover `oauth.privacyfence.eu`. The Worker writes no logs
  and `wrangler.toml` keeps observability off.
- `wrangler tail` access is limited to the maintainer token. Anyone who can tail the Worker can read
  authorization codes in transit.
- The GitHub environment still has required reviewers and the `main` branch restriction.

## Checking a deploy

With `RELAY=https://oauth.privacyfence.eu`, `REVOKE_TOKEN` the maintainer token and
`NONCE=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA`:

```bash
# /health returns 200
curl -fsS "$RELAY/health"

# /registrations is a JSON list (it needs the maintainer token)
curl -fsS -H "Authorization: Bearer $REVOKE_TOKEN" "$RELAY/registrations"

# /register for a host that cannot prove itself returns 403 proof_failed
curl -sS -X POST "$RELAY/register" -H "Content-Type: application/json" \
  -d '{"host":"smoke-test.example.org","token":"'"$NONCE"'"}'

# a loopback /callback returns a 302 to http://localhost:...
curl -sSI "$RELAY/callback/slack?state=localhost%3A8765~$NONCE&code=smoke"

# an unregistered host's /callback returns 400
curl -sSI "$RELAY/callback/slack?state=unregistered.example.org~$NONCE&code=smoke"

# a loopback /callback with no code and no error returns 400
curl -sSI "$RELAY/callback/slack?state=localhost%3A8765~$NONCE"
```

The deploy workflow itself only checks `/health`.

## Review new registrations

- Act on each notification mail the same day: the host, the time, the IP address and country of the
  request, the number of active registrations, and whether the host registered for the built-in
  Google sign-in and which domains it proved.
- Once a week read `/registrations`, starting with the rows marked `"notified": false`.
- A host that looks like a phishing page, or that a provider or a user reports, is revoked within one
  working day of the report.

A registered host can ask any Google account to consent to the connector app, so the trust in the
built-in app extends to every registered host. The provider docs therefore tell administrators not
to mark the built-in PrivacyFence app as trusted or pre-approved. The relay refuses hosts under a
suffix of the Public Suffix List's private section (`x.github.io`, `a.pages.dev`, `a.workers.dev`
and similar), which would otherwise let anyone with a free account register.

## Revoking and restoring a host

```bash
curl -X POST https://oauth.privacyfence.eu/admin/revoke \
  -H "Authorization: Bearer $REVOKE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"host":"pf.example.org"}'
```

The host is refused a new registration (403 `revoked`) and its callbacks answer 400 at once. A
revoked row is created even for a host the relay never saw. A mistaken revocation is undone with
`/admin/restore`, which takes the same body and clears the host's verification, so the host proves
control again with its next daily registration before callbacks are forwarded:

```bash
curl -X POST https://oauth.privacyfence.eu/admin/restore \
  -H "Authorization: Bearer $REVOKE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"host":"pf.example.org"}'
```

## Reading the registrations

`GET /registrations` with the maintainer token lists the non-revoked, verified hosts, sorted, each
with `host`, `registered_at`, `notified` and the verified `signin_domains`. There is no public list.
A host that has not registered for 35 days is still listed but is no longer forwarded to.

## Rotating the maintainer token

Update the environment secret `CF_OAUTH_RELAY_REVOKE_TOKEN`, then run `Deploy OAuth relay Worker` on
`main`. The workflow stores it as the Worker secret `REVOKE_TOKEN`, so the old value stops working
when the run finishes.

## Rolling back

`npx wrangler rollback` from `cloudflare/oauth-relay/` with the deploy token, or the Cloudflare
dashboard's Deployments tab for the Worker `privacyfence-oauth-relay`. The registry lives in the
Durable Object and is not touched by a rollback of the code.

## The built-in sign-in

A server that uses the built-in Google sign-in also proves the Google Workspace domains it signs
people in from: one DNS TXT record per domain, at `_privacyfence-relay.<domain>` with the value
`pf-relay=<hex sha256 of the server's relay token>`. The relay checks them with DNS over HTTPS on
every successful registration and returns the missing ones with the record to publish. It forwards a
sign-in code only when Google's `hd` redirect parameter names a verified domain.

`hd` is observed behaviour, not a documented contract. If Google ever stops sending it, built-in
sign-ins fail at the relay. The Worker variable `SIGNIN_REQUIRE_HD` in `wrangler.toml` (default
`"true"`) switches the relay-side check off when set to `"false"`; the server checks `hd` in the
ID token as well. Change it in a reviewed commit and let the workflow deploy it.

## Updating the Public Suffix List

`cloudflare/oauth-relay/src/psl_icann.ts` and `src/psl_private.ts` are generated from
`public_suffix_list.dat` at a pinned commit of <https://github.com/publicsuffix/list>, recorded in
each file's header. To update, run `node scripts/update-psl.mjs <commit>` in `cloudflare/oauth-relay/`,
read the diff (a removed private suffix lets its hosts register), run the tests and commit.

## Rotating a built-in app secret

Add the new secret next to the old one where the provider allows two (Google and Salesforce do),
ship a release that carries the new one, and retire the old one in the provider console after that
release is widely adopted. Servers keep working during the overlap because they hold the credential
the release they run was built with.

## The privacy and abuse picture

The relay stores each registered server's host name, its registration dates, its verified
sign-in domains and whether its mail went out. It keeps short-lived request counters for the rate
limits and prunes them within the hour. It writes no logs, and it never stores an authorization
code. Slack and Miro send no PKCE, so a relayed code for them passes through the Worker in the
browser redirect; the Worker keeps no logs and the code is single use.
