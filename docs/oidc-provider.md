# Sign in to other apps with PrivacyFence

An organization server, or a single-user install on `localhost`, can be the OpenID Connect sign-in
provider for the web apps you name, such as Peek and Grist in the `privacyfence-suite` Docker stack.
People choose **Sign in with SSO** in the app, PrivacyFence signs them in if they are not already,
and the app receives the identity PrivacyFence has: your organization's identity provider, the
built-in Google sign-in, or the passkey on `localhost`.

Nothing is registered with your identity provider or with Google for these apps. You name each app,
its redirect address and its secret in PrivacyFence's environment. Only the apps you name can sign
people in, and PrivacyFence shows no consent page: you chose the app, so it does not ask each person.

## Set it up

Name the apps, then give each one a redirect address and a secret:

```
PRIVACYFENCE_OIDC_CLIENTS=peek,grist
PRIVACYFENCE_OIDC_CLIENT_PEEK_REDIRECT_URIS=https://peek.example.com/oauth/oidc/callback
PRIVACYFENCE_OIDC_CLIENT_PEEK_SECRET_FILE=/run/secrets/peek_oidc_client_secret
PRIVACYFENCE_OIDC_CLIENT_GRIST_REDIRECT_URIS=https://grist.example.com/oauth2/callback
PRIVACYFENCE_OIDC_CLIENT_GRIST_SECRET_FILE=/run/secrets/grist_oidc_client_secret
```

| Variable | What it sets |
|---|---|
| `PRIVACYFENCE_OIDC_CLIENTS` | The apps, comma-separated. Each name starts with a letter and has at most 32 lower-case letters, digits or `-`. The name is the app's `client_id`. |
| `PRIVACYFENCE_OIDC_CLIENT_<NAME>_REDIRECT_URIS` | The addresses the app may be sent back to, comma-separated, at least one. `<NAME>` is the app's name in capitals with `-` written as `_`. Each address is `https://`, or `http://` for `localhost`, `127.0.0.1` or `::1`, with no user name and no fragment. Addresses are compared exactly. |
| `PRIVACYFENCE_OIDC_CLIENT_<NAME>_SECRET_FILE` | The path of a file that holds the app's client secret (a Docker secret). |
| `PRIVACYFENCE_OIDC_CLIENT_<NAME>_SECRET` | The secret itself. Set this or the file, never both. |

A secret must be at least 32 characters after surrounding spaces are removed; `openssl rand -hex 32`
makes a good one. Give the same secret to the app (Peek and Grist take it as their OIDC client
secret). Restart PrivacyFence after changing any of these. The secret is never logged.

The variables work only on an organization server and in single-user mode. In local mode, setting
`PRIVACYFENCE_OIDC_CLIENTS` refuses the start. In organization mode with the web server off, the
daemon ignores them and says so in its log.

## Endpoints

The issuer is your server's `PRIVACYFENCE_ISSUER_URL` followed by `/oidc`, such as
`https://pf.acme.example.com/oidc`. In single-user mode it is `http://localhost:<port>/oidc`. An app
needs only the issuer: it finds the rest in the discovery document.

| Address | What it is |
|---|---|
| `<issuer>/.well-known/openid-configuration` | The discovery document. |
| `<issuer>/authorize` | Where the app sends the person's browser. |
| `<issuer>/token` | Where the app swaps the code for an ID token. |
| `<issuer>/userinfo` | The person's `sub`, `email`, `email_verified` and `name`. |
| `<issuer>/jwks` | The public key that signs ID tokens (RS256). |

The flow is the authorization code flow with PKCE (`S256`), which is mandatory. Access tokens and ID
tokens last 5 minutes, there are no refresh tokens, and there is no `end_session_endpoint`: the apps
sign out locally. These tokens work only at `/oidc/userinfo`, never as an AI client's token, and an
AI client's token does not work there.

## Peek and Grist

Register these redirect addresses in `PRIVACYFENCE_OIDC_CLIENT_<NAME>_REDIRECT_URIS`:

| App | Redirect address |
|---|---|
| Peek | `https://<peek address>/oauth/oidc/callback` |
| Grist | `https://<grist address>/oauth2/callback` |

- **Grist** must be started with `GRIST_OIDC_IDP_SKIP_END_SESSION_ENDPOINT=true`, because
  PrivacyFence has no end-session endpoint.
- **Peek** refuses an issuer on a private address or at `http://localhost` unless it is started with
  `PEEK_OIDC_ALLOW_PRIVATE_ISSUER=true`. Set it when Peek reaches PrivacyFence at a private address
  or at `localhost`.

The `privacyfence-suite` stack generates these settings for you.

## Verified email

Peek and Grist link a sign-in to an account by email, so they need to know the address was verified.
PrivacyFence reports `email_verified` as true when:

- the identity provider sent `email_verified` as `true`, which includes the built-in Google sign-in; or
- `PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL` is on and the person's address is in an allowed domain
  (Admin → Sign-in); or
- the install is single-user, where the address is fixed and there is exactly one person.

Peek and Grist refuse a sign-in whose email is not verified, the same bar as automatic accounts
([Peek](peek-setup.md#automatic-accounts-organization-mode), [Grist](grist-setup.md)).

## The signing key

PrivacyFence makes an RSA 2048 key on first start and keeps it in `org/oidc_signing_key.pem` in the
data directory (mode `0600`), only when `PRIVACYFENCE_OIDC_CLIENTS` is set. To rotate it, stop the
server, delete the file and start again: a new key is made and published at `/oidc/jwks`. ID tokens
last 5 minutes, so apps pick the new key up without anyone signing in again. Back the file up with
the rest of `org/` if you want the same key after a restore. A file that is not a usable RSA key of at
least 2048 bits refuses the start.

## Single-user mode

The issuer is `http://localhost:<port>/oidc`. Peek and Grist fetch the discovery document, the token
endpoint and the key server-side, and both require the issuer in the token to equal that string
exactly. So they must reach PrivacyFence at `localhost:<port>` from wherever they run. The
`privacyfence-suite` localhost stack runs Peek, Grist and PrivacyFence in one shared network
namespace, so this holds. A deployment with separate containers in separate network namespaces
cannot use single-user sign-in; use an organization server instead.

**Cookies on localhost.** Browsers do not separate cookies by port, so the `pf_org_session` cookie is
also sent to Peek and Grist on other `localhost` ports. It is HttpOnly, and PrivacyFence's own
state-changing routes check `Origin`, but those apps can see that a PrivacyFence session exists. This
matters only for single-user installs on one machine you alone use
([ADR 0273](adr/0273-privacyfence-is-an-oidc-provider-for-apps-the-operator-names.md)).

## Troubleshooting

At startup:

**`PRIVACYFENCE_OIDC_CLIENTS has an invalid name: <name>`** — a name does not start with a letter or
has characters other than lower-case letters, digits and `-`, or is longer than 32 characters.

**`PRIVACYFENCE_OIDC_CLIENT_<NAME>_REDIRECT_URIS must list at least one address`** — the app has no
redirect address.

**`…REDIRECT_URIS has an address that is not https:// (http:// only for localhost) or has a user name
or fragment: <uri>`** — fix the address; only `localhost`, `127.0.0.1` and `::1` may use `http://`.

**`Set …SECRET or …SECRET_FILE, not both.`** — keep one of the two.

**`…SECRET_FILE could not be read.`** — the path is wrong or the daemon cannot read it. The reason is
not logged, in case it quotes the secret.

**`…SECRET must be at least 32 characters`** — use a longer secret.

**`PRIVACYFENCE_OIDC_CLIENTS is set, but this is not an organization server.`** — remove the
variables from a local-mode install.

**`<path> is not a usable RSA signing key of at least 2048 bits; delete it to make a new one.`** —
`oidc_signing_key.pem` is damaged or is a different kind of key. Delete it (this rotates the key).

**`PRIVACYFENCE_OIDC_CLIENTS is ignored: the web server is off.`** (a log warning) — turn on the
web server (`web.mcp.enabled`).

When people sign in:

**`Unknown client.`** — the app's `client_id` is not in `PRIVACYFENCE_OIDC_CLIENTS`. The name must
match exactly, in lower case.

**`This redirect address is not registered for this client.`** — the address the app sends does not
equal one in `…_REDIRECT_URIS`, character for character. Compare scheme, host, port and path.

**`PKCE with S256 is required.`** — the app did not send a `code_challenge` with
`code_challenge_method=S256`. Peek and Grist do; a custom app must.

**`The openid scope is required.`** and **`Only response_type=code is supported.`** — the app
asked for something PrivacyFence does not offer.

**`This account has no email address.`** — the identity provider sent no email for this person.

**`Too many sign-ins are in progress; try again.`** — more than 10,000 sign-ins are waiting; try again.

**`invalid_client`** (HTTP 401 from `/oidc/token`) — the app's secret does not match.

**`invalid_grant`** (HTTP 400) — the code was already used, expired (it lasts 60 seconds), was issued
to another app or redirect address, or the PKCE verifier does not match.

**`invalid_request`** (HTTP 400 from `/oidc/token`) — the app sent its secret both in the
`Authorization` header and in the form.

**The app says the email is not verified.** — see [Verified email](#verified-email).
