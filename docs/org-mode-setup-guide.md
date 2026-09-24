# Org mode setup guide

Org mode runs PrivacyFence as a centralized Linux service for multiple authenticated users. The daemon serves the MCP endpoint and browser approval/settings surfaces, while identity comes from the organization's configured OIDC provider.

## 1. Deployment model

A typical deployment contains:

1. a dedicated Linux service account running the PrivacyFence Python environment;
2. a signed organization configuration bundle (`org_config.json`, built by `scripts/build_org_bundle.py`);
3. an HTTPS reverse proxy in front of the daemon;
4. an OIDC identity provider your users already sign in to;
5. per-user connector authorization state, stored under that account's `~/.privacyfence/users/<principal>/` and managed entirely through the web UI — there is no shared credential set across users.

Org mode is not the desktop `.deb` autostart path or the repo-root `privacyfence.service` (a systemd **`--user`** unit for a single-user desktop `pip`/`pipx` install — see its own header comment). This guide walks the system-service path end to end instead.

## 2. Prerequisites

- A Linux host with Python 3.11+ and a way to run a long-lived service (systemd, below).
- A dedicated, unprivileged service account (e.g. `privacyfence`) — don't run this under a personal or shared login.
- DNS pointing your chosen public hostname (e.g. `pf.acme.example.com`) at the host, and a TLS certificate for it (either terminated by your reverse proxy, or handed directly to the daemon — see [§6](#6-reverse-proxy)).
- Your organization's OIDC identity provider, and enough access to it to register an OAuth client (client id/secret, redirect URIs).
- The `cryptography` Python package (`pip install cryptography`) to run `scripts/build_org_bundle.py --generate-signing-key`/`--sign-key` — org mode refuses to start with an unsigned bundle, so this is not optional for this deployment model. It's needed only to run the script; it doesn't have to be installed on the server itself if you build the bundle elsewhere.

## 3. Install PrivacyFence

As the service account:

```bash
python3 -m venv /opt/privacyfence/venv
/opt/privacyfence/venv/bin/pip install privacyfence
```

A real (non-editable) install like this keeps all of its state under the service account's home directory, `~/.privacyfence/` (`paths.data_dir()`) — including `org/org_config.json`, per-principal connector credentials under `users/<principal>/`, and the audit log. Keep that directory writable only by the service account; treat `~/.privacyfence/org/` in particular as security-sensitive deployment configuration, since it holds the bundle carrying your IdP client secret and every connector app's credentials.

## 4. Identity: the OIDC sign-in client and the optional Google connector client

Org mode makes two independent decisions that are easy to conflate because they can both point at the same provider: **who is allowed to sign in at all** (this section), and **which per-connector app a signed-in user can then authorize** (each connector's own setup guide — `google-cloud-setup.md`, `slack-setup.md`, `salesforce-setup.md`, `atlassian-setup.md`). Register both before building the bundle in [§5](#5-build-the-organization-config-bundle).

### 4.1 The OIDC sign-in client (required)

With your identity provider, register an OAuth/OIDC client for PrivacyFence itself and note its issuer URL, client id, and client secret. The IdP must publish standard OIDC discovery — `<issuer>/.well-known/openid-configuration` reachable over HTTPS and containing `issuer`, `authorization_endpoint`, `token_endpoint`, and `jwks_uri` (`org_identity.py`'s `discover_idp`). ID tokens are verified as `RS256` or `ES256`.

Register **both** of these as redirect URIs on that client, substituting your own public hostname (this daemon's `--server-issuer-url` from [§5](#5-build-the-organization-config-bundle)):

- `https://pf.acme.example.com/oauth/idp/callback` — used when Claude (or any MCP client) signs a user in, as part of this daemon's own OAuth 2.1 authorization-server flow (`web/oauth_provider.py`).
- `https://pf.acme.example.com/oauth/idp/login-callback` — used when a human visits this daemon's browser UI directly (`/login`, `web/routes_org_identity.py`).

Every IdP endpoint must be HTTPS; the daemon refuses a plain-HTTP issuer/discovery/authorization/token/JWKS URL. The one exception is `PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP=1` in the daemon process's environment, which lifts that check for testing against a local plain-HTTP IdP (a devcontainer Keycloak, a loopback OIDC test server) — never set it on a real deployment; it removes the one guarantee (an unspoofable IdP endpoint in transit) org mode's whole trust model depends on.

Optionally, decide who counts as an admin: an ID token claim (e.g. `groups`) and the value(s) in it that grant admin — `--idp-admin-group-claim`/`--idp-admin-group-value` in [§5](#5-build-the-organization-config-bundle). Leave it unset and nobody is an admin via this mechanism (fail-closed default). Independently, you can restrict *who may sign in at all* by email domain or by a (possibly different) group claim — `--authz-*`, also in [§5](#5-build-the-organization-config-bundle) — layered on top of the IdP's own authentication, not a replacement for it.

**Google as the sign-in IdP: use `email`, not `groups`.** Google's ID tokens carry no group membership and no Workspace admin role, so `--idp-admin-group-claim groups` never matches anyone and every user, Workspace super-admins included, signs in as a non-admin. That hides the admin-only Settings pages (**Privacy Filter**, **AI systems**) without any error. Name the admins by their exact email address instead: the sign-in flow always requests the `email` scope (`org_identity.py`'s `DEFAULT_SCOPE`), and a single-string claim is matched the same way as a list (`principal_from_claims`).

```bash
  --idp-admin-group-claim email \
  --idp-admin-group-value alice@acme.example.com \
  --idp-admin-group-value bob@acme.example.com \
```

Do not use `hd` (the Workspace domain) as the admin claim: it is the same for every account in the domain and would make all of them admins. Admin status is decided at each sign-in and held in the browser session, so after installing a bundle with a changed admin list and restarting the daemon, an admin signs out and back in at `/login` to pick it up. With `--merge`, the whole `idp` section is rebuilt from the flags of that run, so pass `--mode org`, `--server-issuer-url` and every `--idp-*` flag again, not just the admin ones.

### 4.2 The Google connector client (optional)

This section is the general pattern every connector's org-mode registration follows — Slack, Salesforce, and Atlassian's own setup guides each link back here for it, substituting their own OAuth console and redirect path.

Your OIDC sign-in client above (§4.1) is unrelated to whether you offer the Google connector (Gmail/Drive/Calendar/Contacts/Tasks) to users — it's entirely possible, and common, to sign in via Google but still need a *second*, separate Google OAuth client for the connector itself, because the two use different flows:

- Local desktop installs use a **Desktop app** OAuth client with a loopback redirect (`http://127.0.0.1:.../callback`) — see `google-cloud-setup.md`.
- Org mode needs a **Web application** OAuth client instead, with an explicit HTTPS redirect URI registered up front: `https://pf.acme.example.com/oauth/callback/google` (substituting your own hostname; `web/routes_connect.py` builds this from the daemon's own base URL). The two client types can't be interchanged — a Desktop app client has no field to register this redirect URI, and a Web application client requires one.

Create that Web application client in Google Cloud Console (**APIs & Services → Credentials → + Create Credentials → OAuth client ID**, type **Web application**, with the redirect URI above), download its `client_secret.json`, and pass it to `--google-client-secret` in [§5](#5-build-the-organization-config-bundle) — the same flag local mode uses; `scripts/build_org_bundle.py` reads whichever of the `installed`/`web` keys the file has.

**Use a client dedicated to the connector, not the one from [§4.1](#41-the-oidc-sign-in-client-required).** Nothing stops you registering all three redirect URIs on a single client and using it for both jobs — it works, and PrivacyFence handles it. But Google then reports the union of every scope that one client holds for the user on each connector's token exchange, so the sign-in client's `openid`/`userinfo.email`/`userinfo.profile` show up in every connector's granted scopes and in its stored credentials. Two clients keeps each grant's scope list saying only what that connector actually asked for, which is what you want when reading an audit trail or revoking one connector at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

Slack, Salesforce, and Atlassian follow the identical shape: each needs its own org-mode redirect URI registered on its app (`https://pf.acme.example.com/oauth/callback/{slack,salesforce,atlassian}` respectively — for Jira and Confluence, `atlassian`, since they share one grant), which can coexist on the same app registration as any local-desktop redirect URI you've already registered. See each connector's own guide for the exact console steps.

## 5. Build the organization config bundle

`scripts/build_org_bundle.py` is stdlib-only (no PrivacyFence install required to run it) and produces the single `org_config.json` file you install on the server. Run `python3 scripts/build_org_bundle.py --help` for the full flag reference — every flag group in it maps to one top-level key in the bundle: `google`/`slack`/`salesforce`/`atlassian` (per-connector app credentials, §4.2), `mode`/`server`/`idp` (this section), `authz`, `step_up`, `download_delivery`, `audit_forwarding`, `unattended_sessions`.

First, generate a signing key once per organization and keep it secret — org mode refuses to start with an unsigned bundle, and the *first* signed bundle any install sees pins that key (trust-on-first-use) and rejects anything that doesn't verify against it afterwards:

```bash
python3 scripts/build_org_bundle.py --generate-signing-key /secure/path/org-signing-key.pem
```

Then build the bundle, passing every connector flag from the setup guides you've followed plus `--mode org` and the flags from [§4](#4-identity-the-oidc-sign-in-client-and-the-optional-google-connector-client):

```bash
python3 scripts/build_org_bundle.py \
  --org-name "Acme Corp" \
  --mode org \
  --server-issuer-url https://pf.acme.example.com \
  --idp-issuer https://idp.acme.example.com \
  --idp-client-id <client id from §4.1> \
  --idp-client-secret <client secret from §4.1> \
  --google-client-secret /path/to/client_secret.json \
  --slack-client-id ... --slack-client-secret ... \
  --sign-key /secure/path/org-signing-key.pem \
  -o org_config.json
```

Adjust `--server-bind-host`/`--server-port` (default `0.0.0.0:8765` — what the reverse proxy in [§6](#6-reverse-proxy) forwards to) and `--server-tls-cert`/`--server-tls-key` only if the daemon itself terminates TLS instead of the proxy. `--merge` lets you add one more service to an already-distributed bundle later without re-entering everything (re-run with the same `--sign-key`).

Install the resulting `org_config.json` on the server at `~/.privacyfence/org/org_config.json` (the service account's `paths.org_dir()`) before first starting the daemon, by copying it there — that is the only way to install it in org mode. There is no in-app equivalent: the local-mode Settings page's **Install/Update Organization Config…** action lives on `/settings`, which org mode deliberately never mounts (`web/server.py`'s module docstring, and `_build_org_app`'s route set). The daemon validates and pins the signature on startup either way. To rotate a signing key, an administrator deletes the previously pinned `~/.privacyfence/org/org_config_signing_pubkey.txt` on the server first — otherwise the new bundle is rejected as failing verification against the old key.

## 6. Reverse proxy

Terminate HTTPS at the supported reverse proxy (nginx, Caddy, or similar) and forward traffic to the PrivacyFence daemon on its configured internal `bind_host`/`port` (default `0.0.0.0:8765`, or `localhost:8765` if `org_config.json`'s `server.bind_host` is left unset entirely rather than written by the build script).

Do not expose that internal listener directly to the Internet. Set the proxy to forward `X-Forwarded-For`/`X-Forwarded-Proto`, and list the proxy's own IP address(es) with `--server-trusted-proxy` when building the bundle (§5) — those headers are honored only when at least one trusted proxy is configured, never by default, so redirect/origin validation would otherwise see the proxy's own address instead of the real client. Test sign-in/redirect behavior through the same public hostname users will use.

A minimal Caddy config doing exactly that (Caddy sets `X-Forwarded-*` automatically):

```
pf.acme.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

With `--server-trusted-proxy 127.0.0.1` in the bundle (§5), since Caddy and the daemon are on the same host here.

`/register`, `/authorize`, and `/token` (this daemon's own OAuth 2.1 authorization-server endpoints, `web/oauth_provider.py`) are worth rate-limiting at the proxy alongside whatever else you rate-limit — `/register` in particular is unauthenticated by design (that's what dynamic client registration means), and while the daemon itself caps total registered clients and prunes stale ones, the proxy is the first line against an anonymous request loop.

## 7. Start the daemon

Nothing in the repository ships a system-level unit for this deployment model — the repo-root `privacyfence.service` is deliberately the `--user` unit for a single-user desktop install (it would need `loginctl enable-linger` to survive without an interactive login, and runs as whichever user enables it, not a dedicated service account). Write your own system unit instead, for example at `/etc/systemd/system/privacyfence-org.service`:

```ini
[Unit]
Description=PrivacyFence (org mode)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=privacyfence
ExecStart=/opt/privacyfence/venv/bin/privacyfence-app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now privacyfence-org
journalctl -u privacyfence-org -f
```

Before starting, confirm `config/settings.yaml`'s `web.mcp.enabled` is `true` (the packaged default) — org mode's web server does not start at all if it's `false`, silently, since the MCP endpoint is the only thing org mode's server exists to serve (`daemon_main._maybe_start_web_server`). `web.settings.enabled` has no effect here: org mode returns from `_maybe_start_web_server` before that key is read, since `/settings` is never mounted in this mode. The per-connector "Connect" flow below is served by org mode's own `/connect` page, which is mounted unconditionally and needs no settings key turned on.

Run a single active daemon per state directory: PrivacyFence takes a `portalocker`-backed single-instance lock on `~/.privacyfence/` and refuses to start a second process against the same directory.

## 8. First sign-in and connecting a service

1. Visit `https://pf.acme.example.com/login` and sign in with your organization's identity provider (the OIDC client from [§4.1](#41-the-oidc-sign-in-client-required)). This establishes a principal-scoped browser session — the same identity Claude's own MCP sign-in resolves to, so a user can never see or act on another principal's approvals or connectors.
2. On the `/connect` page, click **Connect** next to each connector you've registered (§4.2 and each connector's own setup guide). Each redirects to that service's own consent screen and lands you back on `/connect` showing it connected — nothing to install or restart, since `ConnectorRegistry` builds and caches that principal's connector set lazily and rebuilds it as soon as credentials change.
3. Point Claude (Desktop, Cowork, or any Streamable HTTP MCP client) at `https://pf.acme.example.com/mcp`. The client's own OAuth 2.1 dynamic client registration and sign-in against this daemon triggers the same IdP redirect as step 1 — there's no bearer token to copy anywhere, unlike local mode's `~/.privacyfence/mcp_token`.

## 9. Where PII policy and auto-accept rules live

`/settings` in org mode (#400) is scoped very differently from local mode's combined settings page — it does **not** mount that page's ~30-action editor (connector management, the update banner, Telegram's interactive auth stay local-mode-only; `web/org_settings_scope.py`'s `ACTION_SCOPES` declares, per action, which mode(s) it actually has a route in). What it does give every signed-in principal, linked from `/approvals`'s own footer:

- **`GET /settings`** — that principal's own auto-accept rules (PrivacyFence's single scope+verb
  policy model — see [`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md#auto-accept)): a Remove
  button per row, rendered as the same plain-language sentence local mode's own Auto-accept
  Settings page uses, and an "Add a rule" form (a scope+verb picker constrained to the same
  catalogue local mode's picker and the MCP bridge both validate against, plus a value field) that
  adds a new one. Both the add and every removal go through the same `org_session` CSRF/origin
  checks as `/approvals`, write straight to that principal's own on-disk `auto_accept:` section,
  and are written to the audit log. No principal can see, add to, or remove another's rules from
  here.
- **`GET /settings/privacy`** — admin-only (`Principal.is_admin`, see [§4.1](#41-the-oidc-sign-in-client-required) for how that's resolved from IdP group claims), the effective install-wide PII/privacy policy described below, including which groups are relying on the fail-safe default versus explicitly configured. An admin can change it here: each group's default policy, each category's policy, the PII-detection master switch and its two individually-toggleable categories. Every change rewrites the server's own `settings.yaml`, takes effect for every principal immediately (no daemon restart — see below), and is written to the audit log under the admin who made it. A signed-in principal who is not an admin gets a 403 from the page *and* from the write endpoints, which re-check `is_admin` themselves rather than trusting that the page was reachable.

Beyond adding and removing a rule row, editing `/settings`'s per-principal rules further (narrowing an existing rule in place, rather than removing it and adding a narrower one) from the browser is still out of scope; `/settings/privacy`'s policy fields, described above, can be edited directly. Each of the two settings below is configured, and takes effect, differently.

**PII/privacy policy is install-wide.** The `privacy`/`drive_privacy`/`slack_privacy`/`contacts_privacy`/`tasks_privacy`/`confluence_privacy` sections of the *server's own* `~/.privacyfence/config/settings.yaml` (`privacy_filter.py`, see `resources/settings.yaml.example`) apply to every principal on this install — there is no per-user override, and `/settings/privacy` above reads this exact file. Editing that file **by hand** on the server requires restarting the daemon (`systemctl restart privacyfence-org`, [§7](#7-start-the-daemon)) before the change takes effect, because it's read once at startup. Editing it **from `/settings/privacy`** does not: that path writes the same file and then reloads the privacy filter and PII gate for every principal in the running process, so the change applies to everyone's next request. Both routes end up at the same file, but don't mix them: `/settings/privacy`'s write rewrites the whole file from its own in-memory copy (`web/org_install_policy.py`'s `apply_change`), so any hand edit made since the daemon last loaded it — including comments and formatting — is silently discarded the next time an admin saves from the browser, restarted or not. Pick one editing path for a given change and see it through: hand-edit and restart, or use `/settings/privacy` and leave the file alone in between. A category genuinely absent from that file falls back to a group's `default_policy`, and a group section absent altogether falls back to a **default of `block`** in org mode specifically (`allow` in local mode) — an organization's centrally deployed `settings.yaml` is expected to state its own privacy policy explicitly, not silently inherit the permissive default a single-user desktop install gets. Check the deployed file for any group you expect to be restrictive; leaving it out entirely still fails closed (`/settings/privacy` flags it as falling back to that default so an admin doesn't have to hand-read the file to find out), but naming it explicitly is what documents the intended policy to the next administrator who reads it.

**Auto-accept rules are per-principal.** Each signed-in user's "Always allow" decisions — the same
`_interact` closures inside `gate.py`'s `gated_call` that local mode's popup uses, offered from the
approval popup ([§10](#10-approvals) below) — persist to that principal's own
`~/.privacyfence/users/<principal>/config/settings.yaml`, under the single `auto_accept:` section
described in [Auto-accept](TECHNICAL_REFERENCE.md#auto-accept) — the same file layout local mode
uses for its one local principal, just rooted under that user's own directory instead of the
top-level one. One principal's rules are invisible to and cannot be edited by another, including an
admin — `/settings` above only ever acts on `current_principal()`'s own file, never a path parameter
naming someone else's id; an administrator who genuinely needs to review or revoke *another*
principal's rules still needs server filesystem access to hand-edit that file directly — unlike the
install-wide policy above, which an admin can now edit from the browser.

## 10. Approvals

Org-mode approval routes are principal-aware: a signed-in user can act only on approvals authorized for that principal. Sensitive write approvals can require WebAuthn step-up when configured (`--step-up-enabled` in §5), and that step-up ordinarily accepts either an enrolled passkey or a fresh IdP re-authentication. `--step-up-require-passkey` closes the IdP-reauth path entirely for organizations that want hardware-bound WebAuthn as a hard requirement (e.g. to defend against a compromised or phished IdP session satisfying step-up on its own): a principal with no enrolled passkey gets a hard failure directing them to `/security` to enroll one instead of a silent fallback to re-authentication.

The UI behavior itself is the same embedded browser approval surface documented in [`approval-list-ui-ux.md`](approval-list-ui-ux.md).

## 11. Downloads

Centralized deployments cannot write directly to a user's local filesystem. Org-mode file delivery therefore uses inline content (up to `download_delivery.inline_max_bytes`, default 8MB, `--downloads-inline-max-bytes` in §5) or encrypted short-lived staged links (`download_delivery.link_ttl_seconds`, default 300s, `--downloads-link-ttl-seconds`) as documented in [`org-mode-download-delivery.md`](org-mode-download-delivery.md).

## 12. Operations

Before production use, define backup/restore, upgrades/rollback, monitoring, audit retention/forwarding (`--audit-forwarding-*` in §5), and service restart procedures. See [`org-mode-operational-readiness.md`](org-mode-operational-readiness.md).

## 13. Validation

Validate the deployment through the public HTTPS origin:

- unauthenticated requests are rejected/redirected appropriately;
- OIDC sign-in establishes the intended principal;
- users cannot see or decide another principal's approvals;
- connector authorization is stored under the signed-in principal;
- MCP requests apply the signed-in/authorized principal's policy and connectors;
- audit entries contain the correct principal;
- restart preserves intended persistent state.

Automated org-mode coverage is described in [`testing-policy.md`](testing-policy.md); see [`platform-support.md`](platform-support.md) for this deployment path's real-install verification status.
