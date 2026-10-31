# privacyfence-suite: PrivacyFence, Peek and Grist in one Docker stack, already connected

> This plan is for the new repository `privacyfence/privacyfence-suite`. It sits on a `plan/` branch
> of `privacyfence/privacyfence` only until that repository exists. `manual_before` item
> `mb1-create-repo` copies it there, with the profile in [Appendix A](#appendix-a-claudetoolkityaml),
> and `/implement` runs it from there.

## Goal

An organization starts PrivacyFence (organization mode), Peek and Grist with one
`docker compose up -d`, and every person who signs in to PrivacyFence finds Peek and Grist already
connected. Nobody creates a provisioning key, copies a key between containers, or turns a switch on
the Admin page.

The repository ships four configurations, each a self-contained folder:

| Folder | Proxy | Sign-in |
|---|---|---|
| `configs/caddy-idp` | Caddy (automatic HTTPS) | The organization's own OIDC identity provider for all three apps |
| `configs/caddy-google` | Caddy (automatic HTTPS) | Google Workspace: PrivacyFence's built-in Google sign-in, plus one Google OAuth client for Peek and Grist |
| `configs/nginx-google` | nginx with the operator's certificate | The same as `caddy-google` |
| `configs/localhost` | none | One person on this computer: PrivacyFence single-user mode (setup code and passkey) |

Every app signs in to the identity provider directly (the user's decision). The provider is the
organization's own, or Google. PrivacyFence is not an identity provider for Peek or Grist.

## Current state

- **Nothing exists yet.** The repository does not exist. The starting point is the compose file in
  the request (PrivacyFence, Peek, Grist and Caddy on `172.30.87.0/24`, Caddy at `.2` with network
  aliases for the three public names) and PrivacyFence's own `deploy/docker/` files
  (`compose.yaml`, `Caddyfile`, `privacyfence.env.example`, explained in `docs/org-mode-docker.md`).
- **It needs features released in three repositories first** (separate plans, `manual_before`
  `mb2-upstream-releases`):
  - **privacyfence** (`plan/env-automatic-accounts`): `PRIVACYFENCE_PEEK_AUTO_PROVISION=true` and
    `PRIVACYFENCE_GRIST_AUTO_PROVISION=true` offer the services and turn automatic accounts on,
    including in single-user mode, where the one person is `admin@localhost`.
  - **peek** (`plan/env-bootstrap`): `PEEK_ADMIN_EMAILS` (skips `/setup`) and
    `PEEK_ADMIN_PASSWORD_FILE`. Sign-in comes from the environment: `PEEK_GOOGLE_CLIENT_ID`,
    `PEEK_GOOGLE_CLIENT_SECRET_FILE`, `PEEK_GOOGLE_ORG_SIGNUP`, `PEEK_OIDC_ISSUER_URL`,
    `PEEK_OIDC_CLIENT_ID`, `PEEK_OIDC_CLIENT_SECRET_FILE`, `PEEK_OIDC_SIGNUP` and
    `PEEK_ALLOWED_EMAIL_DOMAIN`. `PEEK_PROVISIONING_KEY_FILE` registers the key.
  - **grist-provisioner** (single-session change): `PROVISIONER_KEY_FILE` registers the key.
    `PROVISIONER_GRIST_URL`, `PROVISIONER_GRIST_TEAM_SITE` and `PROVISIONER_GRIST_BOOT_KEY_FILE`
    create the Grist user through Grist's own API and add them to the team site.
- **Facts the design rests on:**
  - PrivacyFence accepts `http://` addresses only for `localhost`, `127.0.0.1` and `::1`.
  - Peek's image is `FROM scratch` with uid 65532 and no shell, and Peek serves `/healthz`.
  - Grist reads its secrets only from the environment. Its image starts as root, chowns `/persist`
    and drops to the `grist` user (uid 1001, inferred: checked in CI). It serves `/status`.
  - Grist's OIDC needs `GRIST_OIDC_IDP_SKIP_END_SESSION_ENDPOINT=true` for Google. A new Grist user
    does not join the team site by itself.
  - Grist officially lists OIDC as a full-edition feature, but the code runs in `gristlabs/grist`.
    The user chose it.
  - PrivacyFence's image is uid 10001, read-only, data in `/var/lib/privacyfence-org/.privacyfence`.

## Design

### Repository layout

```
README.md                      quick start per configuration, what "already connected" means
CHANGELOG.md                   "## [Unreleased]"
LICENSE                        Apache-2.0 (created with the repository)
.claude/toolkit.yaml           Appendix A (created with the repository)
requirements-dev.txt           pyyaml==6.0.2, pytest==8.4.2, ruff==0.14.1
common/init.sh                 the one copy of the secret-generating script
scripts/check_configs.py       consistency checks (below), standard library + PyYAML
tests/test_check_configs.py    tests for it, with small fixture trees under tests/fixtures/
configs/<name>/compose.yaml    self-contained: an operator copies one folder
configs/<name>/.env.example    every variable the folder uses, image tags pinned
configs/<name>/init.sh         byte-identical copy of common/init.sh (checked)
configs/caddy-*/Caddyfile
configs/nginx-google/templates/default.conf.template
configs/nginx-google/certs/README.md   which two files to put there
docs/configurations.md         every variable, per configuration
docs/operations.md             upgrade, backup, rotating secrets, adding people, troubleshooting
.github/workflows/ci.yml       lint (below)
.github/workflows/e2e-localhost.yml
```

Each folder is self-contained because the request asks for "a stack" an operator copies and runs,
and Compose's `extends` across folders would tie every folder to the repository layout. The cost
is duplication, which `scripts/check_configs.py` keeps honest.

### Services and addresses (all configurations)

| Service | Image | Fixed address (proxy configs) | Port inside |
|---|---|---|---|
| `init` | `alpine:${ALPINE_IMAGE_TAG}` | none (no network: `network_mode: none`) | |
| `privacyfence` | `ghcr.io/privacyfence/privacyfence-org:${PRIVACYFENCE_IMAGE_TAG}` | `172.30.87.10` | 8765 |
| `peek` | `ghcr.io/privacyfence/peek:${PEEK_IMAGE_TAG}` | `172.30.87.11` | 7700 |
| `grist` | `gristlabs/grist:${GRIST_IMAGE_TAG}` | `172.30.87.12` | 8484 |
| `grist-provisioner` | `ghcr.io/privacyfence/grist-provisioner:${GRIST_PROVISIONER_IMAGE_TAG}` | `172.30.87.13` | 8485 |
| `caddy` / `nginx` | `caddy:${CADDY_IMAGE_TAG}` / `nginx:${NGINX_IMAGE_TAG}` | `172.30.87.2` | 80, 443 |

- **Image tags are pinned.** Every image tag is `${NAME_IMAGE_TAG:?pin a version in .env}`;
  `.env.example` pins exact versions, never `latest`.
- **Peek's image.** The request's `ghcr.io/andras-tkcs/peek` is replaced by
  `ghcr.io/privacyfence/peek`, where Peek's release workflow publishes.
- **Hardening.** `privacyfence`, `peek` and `grist-provisioner` are `read_only: true` with
  `cap_drop: [ALL]` and `security_opt: [no-new-privileges:true]`. `privacyfence` and
  `grist-provisioner` add `tmpfs: [/tmp]`. `grist` keeps the image defaults, because its entrypoint
  chowns `/persist` as root and drops privileges itself. Every service except `init` has
  `restart: unless-stopped`.
- **Network.** Network `pf` with subnet `172.30.87.0/24`. The proxy has the aliases `${PF_HOSTNAME}`,
  `${PEEK_HOSTNAME}` and `${GRIST_HOSTNAME}`, so PrivacyFence and the provisioner reach the public
  `https://` names through the proxy without hairpin NAT. That is why no PrivacyFence address rule
  changes.
- **Start order.** Every service except `init` has
  `depends_on: {init: {condition: service_completed_successfully}}`. `grist-provisioner` also
  depends on `grist` with `condition: service_healthy`, and the proxy on all four apps with
  `condition: service_started`.
- **Health checks.**
  - `grist`: `["CMD", "curl", "-fsS", "http://localhost:8484/status"]`, interval 10s, retries 30.
  - `grist-provisioner`:
    `["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8485/healthz', timeout=3)"]`.
  - `privacyfence` and `peek`: their images' own `HEALTHCHECK`.
- **Volumes.** `pf-data`, `peek-data`, `grist-data` (Grist's `/persist`, also mounted by the
  provisioner), `provisioner-data`, `suite-secrets`, plus `caddy-data` and `caddy-config` for Caddy.

### Secrets: generated once, never handled by a person

`init` runs `init.sh` as root on every `docker compose up`. Its mounts are `suite-secrets:/run/suite`
(read-write), `./init.sh:/init.sh:ro` and, when present, `./secrets:/user-secrets:ro`. Every other
service mounts `suite-secrets:/run/suite:ro`.

`init.sh` (POSIX `sh`, busybox only, `set -eu`, `umask 077`) does the following.

**It generates each internal secret once, if the file is missing or empty.** The value is
`<prefix>` plus 32 random bytes from `/dev/urandom`, base64url without padding (43 characters). It is
written to a temp file and renamed. Each generation prints one line, `init: generated <name>`, never
the value.

| File in `/run/suite` | Prefix | Owner | Mode | Read by |
|---|---|---|---|---|
| `peek_provisioning_key` | `peekprov_` | `10001:65532` | `0440` | PrivacyFence (owner), Peek (group) |
| `grist_provisioning_key` | `gristprov_` | `10001:${GRIST_GID}` | `0440` | PrivacyFence, provisioner |
| `grist_boot_key` | none | `${GRIST_UID}:${GRIST_GID}` | `0400` | Grist (read as root by its entrypoint), provisioner |
| `grist_session_secret` | none | `${GRIST_UID}:${GRIST_GID}` | `0400` | Grist |
| `peek_admin_password` | none | `65532:65532` | `0400` | Peek; only when `SUITE_PEEK_ADMIN_PASSWORD=generate` (localhost) |

When it generates `peek_admin_password`, it prints once:
`init: Peek admin password for <PEEK_ADMIN_EMAIL>: <value> (stored in the suite-secrets volume as peek_admin_password)`.
This is the one value a person needs, on localhost only, and it is shown the same way PrivacyFence
shows its setup code.

**It copies operator-provided secrets on every run**, so a rotation reaches every app at the next
`up`. For each of `idp_client_secret` and `google_client_secret` present in `/user-secrets/`, it
installs three copies with `install -m 0400`:

- `<name>.pf` owned by `10001:10001`
- `<name>.peek` owned by `65532:65532`
- `<name>.grist` owned by `${GRIST_UID}:${GRIST_GID}`

`GRIST_UID` and `GRIST_GID` come from the environment, with default `1001`.

The operator writes those files on the host with `install -m 0600 /dev/null secrets/<name>` and pastes
the value; no secret is ever in `.env` or `compose.yaml`. `secrets/` is in `.gitignore`.

**Rejected alternatives:**

- **Compose file secrets.** They cannot set a different owner per consuming container without
  swarm, and the three images run as three different uids.
- **A `setup.sh` the operator runs once.** That is one more step to forget, and the request is "no
  provision key needed".
- **Generating secrets in each app.** Peek and the provisioner would each have to export their keys
  to PrivacyFence.

### Grist's secrets from files

Grist reads only environment variables, so its service wraps the image entrypoint:

```yaml
entrypoint:
  - /bin/sh
  - -c
  - |
    export GRIST_SESSION_SECRET="$$(cat /run/suite/grist_session_secret)"
    export GRIST_BOOT_KEY="$$(cat /run/suite/grist_boot_key)"
    if [ -n "$${GRIST_OIDC_IDP_CLIENT_SECRET_FILE:-}" ]; then
      export GRIST_OIDC_IDP_CLIENT_SECRET="$$(cat "$$GRIST_OIDC_IDP_CLIENT_SECRET_FILE")"
    fi
    exec /grist/sandbox/docker_entrypoint.sh "$$@"
  - grist
command: ["node", "./sandbox/supervisor.mjs"]
```

The entrypoint path and command must match what `docker image inspect gristlabs/grist:<tag>`
reports. `ci.yml` checks it (below).

### Per-app settings shared by the proxy configurations

**privacyfence** (`environment:` in compose, plus the optional `env_file: [{path: privacyfence.env, required: false}]`
for extras, such as an Entra group claim):

```
PRIVACYFENCE_ISSUER_URL: https://${PF_HOSTNAME}
PRIVACYFENCE_TRUSTED_PROXIES: 172.30.87.2
PRIVACYFENCE_ADMIN_EMAILS: ${ADMIN_EMAILS:?set ADMIN_EMAILS in .env}
PRIVACYFENCE_ALLOWED_DOMAINS: ${ALLOWED_DOMAIN:?set ALLOWED_DOMAIN in .env}
PRIVACYFENCE_PEEK_BASE_URL: https://${PEEK_HOSTNAME}
PRIVACYFENCE_PEEK_PROVISIONING_KEY_FILE: /run/suite/peek_provisioning_key
PRIVACYFENCE_PEEK_AUTO_PROVISION: "true"
PRIVACYFENCE_GRIST_SERVER_URL: https://${GRIST_HOSTNAME}
PRIVACYFENCE_GRIST_PROVISIONER_URL: https://${GRIST_HOSTNAME}
PRIVACYFENCE_GRIST_PROVISIONING_KEY_FILE: /run/suite/grist_provisioning_key
PRIVACYFENCE_GRIST_AUTO_PROVISION: "true"
```

The provisioner shares Grist's hostname. The proxy sends `/api/provisioning/*` to it only from
PrivacyFence's fixed address (`172.30.87.10`) and answers 404 to everyone else. Grist has no
`/api/provisioning` route, so there is no clash, and there is no fourth hostname or certificate.

**peek**:

```
PEEK_BASE_URL: https://${PEEK_HOSTNAME}
PEEK_TRUSTED_PROXY: "true"
PEEK_TRUSTED_PROXY_CIDRS: 172.30.87.2/32
PEEK_ADMIN_EMAILS: ${ADMIN_EMAILS}
PEEK_ALLOWED_EMAIL_DOMAIN: ${ALLOWED_DOMAIN}
PEEK_PROVISIONING_KEY_FILE: /run/suite/peek_provisioning_key
```

**grist**:

```
APP_HOME_URL: https://${GRIST_HOSTNAME}
GRIST_SINGLE_ORG: ${GRIST_TEAM_SITE:-team}
GRIST_ADMIN_EMAIL: ${GRIST_ADMIN_EMAIL:?set GRIST_ADMIN_EMAIL in .env}
GRIST_IN_SERVICE: "true"
GRIST_ORG_CREATION_ANYONE: "false"
GRIST_PERSONAL_ORGS: "false"
GRIST_FORCE_LOGIN: "true"
GRIST_OIDC_IDP_SCOPES: openid email profile
```

The request's comment said to check these against Grist's docs first. The research confirmed:

- `GRIST_ORG_CREATION_ANYONE=false` lets only admins create team sites.
- `GRIST_PERSONAL_ORGS=false` gives new users no personal site.
- `GRIST_FORCE_LOGIN=true` sends anonymous visitors to sign-in.

**grist-provisioner** (`user: "${GRIST_UID:-1001}:${GRIST_GID:-1001}"`, volumes
`grist-data:/persist`, `provisioner-data:/data`, `suite-secrets:/run/suite:ro`):

```
GRIST_ADMIN_EMAIL: ${GRIST_ADMIN_EMAIL}
PROVISIONER_KEY_FILE: /run/suite/grist_provisioning_key
PROVISIONER_GRIST_URL: https://${GRIST_HOSTNAME}
PROVISIONER_GRIST_TEAM_SITE: ${GRIST_TEAM_SITE:-team}
PROVISIONER_GRIST_BOOT_KEY_FILE: /run/suite/grist_boot_key
PROVISIONER_ALLOWED_DOMAINS: ${ALLOWED_DOMAIN}
```

`GRIST_ADMIN_EMAIL` is never provisioned. `.env.example` says to use a dedicated admin address.

### `caddy-idp`: one OIDC client for all three apps

`.env`: `PF_HOSTNAME`, `PEEK_HOSTNAME`, `GRIST_HOSTNAME`, `ADMIN_EMAILS`, `ALLOWED_DOMAIN`,
`GRIST_ADMIN_EMAIL`, `IDP_ISSUER`, `IDP_CLIENT_ID`, `GRIST_OIDC_SKIP_END_SESSION` (default `false`),
and the image tags. The secret goes in `secrets/idp_client_secret`.

The operator registers **one** confidential OIDC client with five redirect URIs, listed in
`.env.example` and the README:

- `https://<PF_HOSTNAME>/oauth/idp/callback`
- `https://<PF_HOSTNAME>/oauth/idp/login-callback`
- `https://<PF_HOSTNAME>/oauth/stepup/callback`
- `https://<PEEK_HOSTNAME>/oauth/oidc/callback`
- `https://<GRIST_HOSTNAME>/oauth2/callback`

The settings on top of the shared ones:

- privacyfence: `PRIVACYFENCE_IDP_ISSUER: ${IDP_ISSUER:?…}`, `PRIVACYFENCE_IDP_CLIENT_ID: ${IDP_CLIENT_ID:?…}`,
  `PRIVACYFENCE_IDP_CLIENT_SECRET_FILE: /run/suite/idp_client_secret.pf`.
- peek: `PEEK_OIDC_ISSUER_URL: ${IDP_ISSUER}`, `PEEK_OIDC_CLIENT_ID: ${IDP_CLIENT_ID}`,
  `PEEK_OIDC_CLIENT_SECRET_FILE: /run/suite/idp_client_secret.peek`, `PEEK_OIDC_SIGNUP: "true"`.
- grist: `GRIST_OIDC_IDP_ISSUER: ${IDP_ISSUER}`, `GRIST_OIDC_IDP_CLIENT_ID: ${IDP_CLIENT_ID}`,
  `GRIST_OIDC_IDP_CLIENT_SECRET_FILE: /run/suite/idp_client_secret.grist`,
  `GRIST_OIDC_IDP_SKIP_END_SESSION_ENDPOINT: ${GRIST_OIDC_SKIP_END_SESSION:-false}`.

Rejected: three clients. Each app uses the code flow with its own redirect URI and checks its own
nonce/state, so one client is safe and saves the operator two registrations. Separate clients
remain possible by editing the folder.

### `caddy-google` and `nginx-google`: Google Workspace

`.env` is as in `caddy-idp`, without the `IDP_*` variables, plus `GOOGLE_CLIENT_ID`. The secret goes
in `secrets/google_client_secret`.

- **privacyfence:** `PRIVACYFENCE_IDP: builtin-google`. Nothing is registered: PrivacyFence's
  built-in app signs in through `oauth.privacyfence.eu`. The operator publishes the
  `_privacyfence-relay` DNS TXT records that
  `docker compose exec privacyfence privacyfence-app --relay-dns-records` lists. That is
  PrivacyFence's existing procedure, linked from the README.
- **One Google OAuth client for Peek and Grist.** It is a Web client, with an **Internal** consent
  screen and two redirect URIs:
  - `https://<PEEK_HOSTNAME>/oauth/google/callback`
  - `https://<GRIST_HOSTNAME>/oauth2/callback`
- **peek:** `PEEK_GOOGLE_CLIENT_ID: ${GOOGLE_CLIENT_ID:?…}`,
  `PEEK_GOOGLE_CLIENT_SECRET_FILE: /run/suite/google_client_secret.peek`,
  `PEEK_GOOGLE_ORG_SIGNUP: "true"` (automatic accounts for the allowed domain, as asked).
- **grist:** `GRIST_OIDC_IDP_ISSUER: https://accounts.google.com`, `GRIST_OIDC_IDP_CLIENT_ID: ${GOOGLE_CLIENT_ID}`,
  `GRIST_OIDC_IDP_CLIENT_SECRET_FILE: /run/suite/google_client_secret.grist`,
  `GRIST_OIDC_IDP_SKIP_END_SESSION_ENDPOINT: "true"`. Grist itself admits any Google account, so the
  Internal consent screen is what restricts sign-in to the organization, as the request's comment
  says.

### Caddyfile (both Caddy configurations)

```
{$PF_HOSTNAME} {
	reverse_proxy privacyfence:8765
}

{$PEEK_HOSTNAME} {
	reverse_proxy peek:7700
}

{$GRIST_HOSTNAME} {
	@provisioner {
		path /api/provisioning/*
		remote_ip 172.30.87.10
	}
	handle @provisioner {
		reverse_proxy grist-provisioner:8485
	}
	handle /api/provisioning/* {
		respond 404
	}
	handle {
		reverse_proxy grist:8484
	}
}
```

The caddy service gets `PF_HOSTNAME`, `PEEK_HOSTNAME` and `GRIST_HOSTNAME` in `environment:` and
publishes `80:80`, `443:443` and `443:443/udp`.

### nginx (`nginx-google`)

- **Mounts.** `./templates:/etc/nginx/templates:ro`, so the image's envsubst renders
  `default.conf.template` with the three hostnames, and `./certs:/etc/nginx/certs:ro`.
- **Certificate.** The operator supplies `certs/fullchain.pem` and `certs/privkey.pem`, covering all
  three names (SAN or wildcard). Rejected: an ACME companion container, because it is a second moving
  part that Caddy already covers; an operator who wants nginx usually has a certificate process.
- **Port 80.** It answers `return 301 https://$host$request_uri;`.
- **One `server` block per hostname**, each with `listen 443 ssl;`, `http2 on;`, the certificate
  lines, `client_max_body_size 100m;`, and `proxy_set_header` for `Host $host`,
  `X-Forwarded-For $proxy_add_x_forwarded_for` and `X-Forwarded-Proto $scheme`.
- **PrivacyFence and Peek** also get `proxy_buffering off;` and `proxy_read_timeout 3600s;`, for MCP
  streams and Peek's server-sent events.
- **Grist** gets `proxy_http_version 1.1;`, `proxy_set_header Upgrade $http_upgrade;` and
  `proxy_set_header Connection $connection_upgrade;`, with a top-level
  `map $http_upgrade $connection_upgrade { default upgrade; '' close; }` (Grist uses websockets).
  It also gets `location /api/provisioning/ { allow 172.30.87.10; deny all; proxy_pass http://grist-provisioner:8485; }`.
- **Ports.** It publishes `80:80` and `443:443`.

### `localhost`: one person, no proxy

- **One network namespace.** `privacyfence` owns it and publishes `127.0.0.1:8765:8765`,
  `127.0.0.1:7700:7700` and `127.0.0.1:8484:8484`. `peek`, `grist` and `grist-provisioner` use
  `network_mode: "service:privacyfence"`.
- **Why one namespace.** Every app reaches the others at `http://localhost:<port>`, which is also
  what the browser uses and the only `http://` form PrivacyFence accepts. There are no fixed
  addresses and no `pf` network.
- **privacyfence.** No identity provider variables, so it runs in single-user mode at
  `http://localhost:8765`. It sets:
  - `PRIVACYFENCE_PEEK_BASE_URL: http://localhost:7700`
  - `PRIVACYFENCE_GRIST_SERVER_URL: http://localhost:8484`
  - `PRIVACYFENCE_GRIST_PROVISIONER_URL: http://localhost:8485`
  - the two key files and the two `…_AUTO_PROVISION: "true"`

  The one person is `admin@localhost` (fixed in PrivacyFence).
- **peek.** It sets:
  - `PEEK_BASE_URL: http://localhost:7700`
  - `PEEK_ADMIN_EMAILS: ${PEEK_ADMIN_EMAIL:-peek-admin@localhost}`
  - `PEEK_ADMIN_PASSWORD_FILE: /run/suite/peek_admin_password`
  - `PEEK_PROVISIONING_KEY_FILE: /run/suite/peek_provisioning_key`

  `init` gets `SUITE_PEEK_ADMIN_PASSWORD: generate` and `PEEK_ADMIN_EMAIL` with the same default.
  PrivacyFence provisions `admin@localhost` as a normal Peek account; the person opens Peek in the
  browser as `peek-admin@localhost` with the printed password, and sees every page as admin.
- **grist.** No login system, so Grist's minimal login makes every visitor `GRIST_DEFAULT_EMAIL`. It
  sets:
  - `APP_HOME_URL: http://localhost:8484`
  - `GRIST_DEFAULT_EMAIL: admin@localhost`
  - `GRIST_ADMIN_EMAIL: grist-admin@localhost`
  - `GRIST_SINGLE_ORG: team`
  - `GRIST_IN_SERVICE: "true"`
  - `GRIST_ORG_CREATION_ANYONE: "false"`
  - `GRIST_PERSONAL_ORGS: "false"`

  The browser user and PrivacyFence's person are the same Grist user, `admin@localhost`, which is not
  the install admin, so the provisioner serves it.
- **grist-provisioner.** It sets:
  - `GRIST_ADMIN_EMAIL: grist-admin@localhost` (and deliberately no `GRIST_DEFAULT_EMAIL`)
  - `PROVISIONER_GRIST_URL: http://localhost:8484`
  - `PROVISIONER_GRIST_TEAM_SITE: team`
  - the key and boot-key files
- Everything is published on `127.0.0.1` only. The README repeats PrivacyFence's warning that anyone
  who can run `docker` on the computer controls the install.

### `scripts/check_configs.py`

Exit 0 and print `ok: <n> configurations` when every check passes. Otherwise print one line per
problem, `<config>: <problem>`, and exit 1. It is standard library plus PyYAML, and reads
`configs/*/`. The checks:

1. `compose.yaml`, `.env.example` and `init.sh` exist; `init.sh` is byte-identical to `common/init.sh`
   (`init.sh differs from common/init.sh`).
2. The services include `init`, `privacyfence`, `peek`, `grist` and `grist-provisioner`.
3. Every `image:` contains `${<NAME>_IMAGE_TAG:?`.
4. In `.env.example`, every `*_IMAGE_TAG` value is set and is not `latest`
   (`<VAR> must pin a version`).
5. Every `${VAR` referenced in `compose.yaml`, `Caddyfile`, `templates/*.template` (only names
   matching `[A-Z][A-Z0-9_]*` and not inside a `$$`), except those with a `:-` default, is assigned
   in `.env.example` (`<VAR> is used but not in .env.example`).
6. Every service except `init` has `depends_on.init.condition == service_completed_successfully`.
7. `privacyfence`, `peek` and `grist-provisioner` have `read_only: true`, `cap_drop: [ALL]` and
   `no-new-privileges:true`.
8. For each of `privacyfence`, `peek`, `grist` and `grist-provisioner`, the `image` string is the same
   in every configuration (`<service> image differs from configs/<first>`).
9. In configurations with a proxy, `PRIVACYFENCE_TRUSTED_PROXIES` equals the proxy's `ipv4_address`,
   and the proxy's aliases list the three hostnames.
10. No value in any `compose.yaml` `environment:` matches `(?i)(secret|password|_key)$` on the key
    side, unless the key ends in `_FILE`, or is `GRIST_OIDC_IDP_SCOPES` or `PRIVACYFENCE_TRUSTED_PROXIES`
    (`<service>.<key> must come from a file`).

### CI

`ci.yml` runs on push and pull request, on `ubuntu-latest`. These jobs block:

1. **`check`:** `pip install -r requirements-dev.txt`, `pytest tests -q`,
   `python3 scripts/check_configs.py`, `ruff check .`.
2. **`compose`:** for each `configs/*/`, `cp .env.example .env`, then fill the placeholders with the
   values from the "CI placeholders" table, then run `docker compose config -q`.
3. **`proxies`:**
   - `docker run --rm -v ./configs/caddy-idp/Caddyfile:/etc/caddy/Caddyfile:ro -e PF_HOSTNAME=pf.example.com -e PEEK_HOSTNAME=peek.example.com -e GRIST_HOSTNAME=grist.example.com caddy:<tag> caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile`
     (and the same for `caddy-google`).
   - For nginx, a self-signed certificate made with `openssl req -x509` into a temp `certs/`, then
     `docker run --rm` the nginx image with the template and certificate mounted, `--add-host` for
     `privacyfence`, `peek`, `grist` and `grist-provisioner` pointing at `127.0.0.1`, and the command
     `nginx -t`.
4. **`shellcheck`:** `shellcheck -s sh common/init.sh`.
5. **`grist-image`:** for the `GRIST_IMAGE_TAG` in `configs/localhost/.env.example`, check that:
   - `docker image inspect` shows `Entrypoint` ending in `sandbox/docker_entrypoint.sh`;
   - `Cmd == ["node","./sandbox/supervisor.mjs"]`;
   - `docker run --rm --entrypoint id gristlabs/grist:<tag> -u grist` prints `1001`.

CI placeholders: `PF_HOSTNAME=pf.example.com`, `PEEK_HOSTNAME=peek.example.com`,
`GRIST_HOSTNAME=grist.example.com`, `ADMIN_EMAILS=admin@example.com`, `ALLOWED_DOMAIN=example.com`,
`GRIST_ADMIN_EMAIL=grist-admin@example.com`, `IDP_ISSUER=https://idp.example.com`,
`IDP_CLIENT_ID=ci`, `GOOGLE_CLIENT_ID=ci.apps.googleusercontent.com`.

`e2e-localhost.yml` runs on push, pull request and `workflow_dispatch`, with a 20-minute timeout:

1. `cd configs/localhost && cp .env.example .env && docker compose up -d --wait --wait-timeout 300`.
2. Poll for up to 180 s, every 5 s, until both files exist:
   `docker compose exec -T privacyfence test -s /var/lib/privacyfence-org/.privacyfence/users/single-user/credentials/peek_token.json`
   and the same for `grist_token.json`.
3. `docker compose exec -T grist-provisioner grist-provisioner check` prints a line matching
   `^ok: [0-9]+ Grist users, 1 linked$`.
4. `docker compose logs init` contains `init: Peek admin password for peek-admin@localhost:`, and
   `docker compose logs peek` contains `first-run setup is skipped`.
5. `curl -fsS http://127.0.0.1:8765/.well-known/oauth-authorization-server`,
   `curl -fsS http://127.0.0.1:7700/healthz` and `curl -fsS http://127.0.0.1:8484/status` succeed.
6. `docker compose down`, then `docker compose up -d --wait`: the second start generates nothing.
   The `init` log has no `init: generated` line, and step 3 still passes.
7. On failure, it prints `docker compose ps` and `docker compose logs --no-color`. The logs carry no
   secret except the localhost Peek password, which is per-run and random.

## Manual steps

The steps are in the [step-by-step page](https://claude.ai/artifact/S4o9VWjyncbxGMNUREgaq1), whose
source is `docs/privacyfence-suite-plan-manual-steps.html`.

- **Before:**
  - `mb1-create-repo`: create the repository with its profile and this plan.
  - `mb2-upstream-releases`: release PrivacyFence, Peek and grist-provisioner with their features,
    with public images.
- **After:**
  - `ma1-localhost`: run the localhost stack on your own computer.
  - `ma2-caddy-google`: deploy `caddy-google` on a server.
  - `ma3-caddy-idp`: deploy `caddy-idp` with your own identity provider, if you have one.
  - `ma4-nginx-google`: start `nginx-google` with a real certificate.

## Risks and open questions

- **Grist's entrypoint or uid differs from the research** (`docker_entrypoint.sh`,
  `node ./sandbox/supervisor.mjs`, uid 1001). The `grist-image` CI job fails. The worker stops with
  `status=blocked` and reports what `docker image inspect` shows, rather than guessing.
- **Grist answers the provisioner's API call differently behind the proxy** (host or org routing with
  `GRIST_SINGLE_ORG`). Only the real deployments in `manual_after` exercise the proxy path; the e2e
  covers `localhost`. If `ma2` fails on Grist provisioning, the fix belongs in grist-provisioner,
  not in this repository.
- **Grist labels OIDC a full-edition feature.** If a Grist release ever gates it in code, the
  `caddy-*` configurations lose Grist sign-in. That is a deliberate, known risk (the user's choice);
  the README says so in one sentence.
- **An upstream release is missing a feature.** The phase briefs read the versions from each
  repository's latest release and check its changelog for the feature. If a feature is absent, the
  phase stops with `status=blocked`.
- **Compose version.** `env_file` with `required: false` needs Compose 2.24, and `--wait-timeout`
  needs 2.20. The README states Docker Engine 24+ and Compose 2.24+.

## Appendix A: `.claude/toolkit.yaml`

```yaml
project:
  name: PrivacyFence suite
docs:
  must_read:
    - README.md
  plan_dir: docs
  changelog:
    file: CHANGELOG.md
    section: "## [Unreleased]"
git:
  main_branch: main
  branch_pattern: "<type>/<kebab-case>"
  branch_types: [feature, fix, chore, docs]
  merge_style: merge-commit
verify:
  fast:
    - python3 -m pip install -q -r requirements-dev.txt
    - python3 -m pytest tests -q
    - python3 scripts/check_configs.py
  dod:
    - python3 -m pip install -q -r requirements-dev.txt
    - python3 -m pytest tests -q
    - python3 scripts/check_configs.py
    - ruff check .
  dod_manual:
    - "A change to a configuration's compose.yaml, proxy config or .env.example needs the ci.yml run on the pushed branch to be green (docker compose config, caddy validate, nginx -t, shellcheck)."
    - "A change that can affect the localhost stack needs the e2e-localhost.yml run on the pushed branch to be green."
ci:
  dispatchable:
    - workflow: ci.yml
      use_when: "docker compose config for every configuration, caddy validate, nginx -t and shellcheck (the cloud session has no Docker)"
      notes: "Runs by itself on every push and pull request; read that run."
    - workflow: e2e-localhost.yml
      use_when: "The localhost stack started for real: every container healthy and PrivacyFence connected to Peek and Grist"
      notes: "Runs by itself on every push and pull request; workflow_dispatch works once it is on main."
stacks:
  - python
models:
  planner: opus
  orchestrator: sonnet
  worker: sonnet
  reviewer: opus
```

## Implementation manifest

```yaml
plan_slug: privacyfence-suite
feature_branch: feature/privacyfence-suite
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/S4o9VWjyncbxGMNUREgaq1
manual_steps_source: docs/privacyfence-suite-plan-manual-steps.html
manual_before:
  - id: mb1-create-repo
    title: Create privacyfence/privacyfence-suite with its profile, and put this plan on its plan/privacyfence-suite branch
    why: Every phase works in that repository, and /implement reads its .claude/toolkit.yaml
    done_when: https://github.com/privacyfence/privacyfence-suite/blob/plan/privacyfence-suite/docs/privacyfence-suite-plan.md opens, main has README.md, LICENSE and .claude/toolkit.yaml, and a Claude Code session can clone the repository
  - id: mb2-upstream-releases
    title: Release PrivacyFence, Peek and grist-provisioner with the features this stack needs, with public images
    why: p2 to p4 pin those versions in .env.example, and e2e-localhost.yml pulls them; without them every container check fails
    done_when: The latest GitHub release of each of privacyfence/privacyfence, privacyfence/peek and privacyfence/grist-provisioner lists the feature in its notes, and `docker pull` of each image at that version works without signing in
manual_after:
  - id: ma1-localhost
    title: Run configs/localhost on your own computer and use all three apps
    why: The e2e checks files and health endpoints; only a person can check the passkey sign-in and that the connected Peek and Grist work in the browser
  - id: ma2-caddy-google
    title: Deploy configs/caddy-google on a server with real DNS and a Google OAuth client
    why: CI cannot get public certificates, use Google sign-in or the PrivacyFence relay
  - id: ma3-caddy-idp
    title: Deploy configs/caddy-idp with your own identity provider (skip if you have none)
    why: Checks the shared OIDC client across all three apps against a real identity provider
  - id: ma4-nginx-google
    title: Start configs/nginx-google with a real certificate
    why: Checks websockets (Grist), server-sent events (Peek) and MCP streaming (PrivacyFence) through nginx
verify_after_merge:
  - python3 -m pip install -q -r requirements-dev.txt && python3 -m pytest tests -q && python3 scripts/check_configs.py
final_checks:
  - docs/privacyfence-suite-plan.md and docs/privacyfence-suite-plan-manual-steps.html are deleted and nothing links to them
  - CHANGELOG.md has entries under "## [Unreleased]" and no version heading
  - the ci.yml and e2e-localhost.yml runs on the feature branch's head are green
phases:
  - id: p1-skeleton
    title: Consistency checker, init script, CI workflow and repository basics
    depends_on: []
    complexity: M
    touches:
      - .gitignore
      - requirements-dev.txt
      - pyproject.toml
      - common/init.sh
      - scripts/check_configs.py
      - tests/test_check_configs.py
      - tests/fixtures/**
      - .github/workflows/ci.yml
      - CHANGELOG.md
    brief: |
      Read docs/privacyfence-suite-plan.md: "Repository layout", "Secrets: generated once",
      "scripts/check_configs.py" and "CI". There are no configs/ folders yet; later phases add them.
      1. .gitignore: `.env`, `secrets/`, `configs/*/secrets/`, `configs/*/certs/*.pem`, `__pycache__/`,
         `.pytest_cache/`. requirements-dev.txt with the three pins from the layout. pyproject.toml
         with only [tool.ruff] (line-length = 120, target-version = "py312") and
         [tool.pytest.ini_options] testpaths = ["tests"].
      2. common/init.sh exactly as the "Secrets" section specifies: file names, prefixes, owners,
         modes, the GRIST_UID/GRIST_GID defaults (1001), the generate-once rule, the
         SUITE_PEEK_ADMIN_PASSWORD=generate case with its one printed line, the copy of
         /user-secrets/idp_client_secret and google_client_secret into .pf/.peek/.grist, and the
         log lines. POSIX sh only (busybox ash): no bash arrays, no [[ ]], no `local`.
         Random value: `head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n'`.
      3. scripts/check_configs.py with the ten checks and messages from the Design, a
         `main(root: Path) -> int`, and `if __name__ == "__main__": sys.exit(main(Path(__file__).resolve().parent.parent))`.
         With no configs/ folders it prints "ok: 0 configurations" and exits 0.
      4. tests/test_check_configs.py: one passing fixture tree (tests/fixtures/good/ with
         common/init.sh and configs/a/ holding a minimal compose.yaml with the five services,
         .env.example and init.sh) and one test per check that copies the good tree to tmp_path,
         breaks one thing, and asserts the exact message and exit code 1.
      5. .github/workflows/ci.yml with the jobs check, compose, proxies, shellcheck and grist-image
         as specified. The compose, proxies and grist-image jobs iterate over configs/*/ and skip
         configurations that do not exist yet (the caddy, nginx or localhost folder), so the workflow is
         green in this phase. Pin every action by full commit SHA with a version comment, copying
         actions/checkout and actions/setup-python from privacyfence/grist-provisioner's
         .github/workflows/real-grist.yml. Use `permissions: contents: read`.
      6. CHANGELOG.md with "# Changelog" and an empty "## [Unreleased]".
      7. Run `python3 -m pytest tests -q`, `python3 scripts/check_configs.py` and `ruff check .`. Push,
         and read the ci.yml run on the branch (it runs by itself); it must be green.
    acceptance:
      - python3 -m pytest tests -q passes, with one failing-fixture test per check
      - python3 scripts/check_configs.py prints "ok: 0 configurations"
      - grep -c "service_completed_successfully" scripts/check_configs.py prints 1 or more
      - the ci.yml run on the pushed branch is green

  - id: p2-localhost
    title: The localhost configuration and its end-to-end workflow
    depends_on: [p1-skeleton]
    complexity: M
    touches:
      - configs/localhost/**
      - .github/workflows/e2e-localhost.yml
    brief: |
      Read docs/privacyfence-suite-plan.md: "Services and addresses", "Secrets", "Grist's secrets from
      files", "localhost" and "CI" (e2e-localhost.yml).
      1. Versions: with the GitHub MCP get_latest_release tool, read the latest release of
         privacyfence/privacyfence, privacyfence/peek and privacyfence/grist-provisioner. Check that
         the release notes mention, respectively, PRIVACYFENCE_PEEK_AUTO_PROVISION, PEEK_ADMIN_EMAILS
         and PEEK_PROVISIONING_KEY_FILE, and PROVISIONER_KEY_FILE. Stop condition: if one is missing,
         stop with status=blocked naming it. Use those versions (without the leading v) as the image
         tags. Use GRIST_IMAGE_TAG=1.7.22 and ALPINE_IMAGE_TAG=3.22.
      2. configs/localhost/compose.yaml: `name: privacyfence-localhost`, the five services as
         specified (init with network_mode: none; privacyfence owning the namespace and publishing the
         three 127.0.0.1 ports; the other three with network_mode: "service:privacyfence"), the Grist
         entrypoint wrapper verbatim, health checks, depends_on, volumes (no caddy volumes, no pf
         network). A header comment of three lines saying what the folder is and pointing to
         docs/configurations.md.
      3. configs/localhost/.env.example: the image tags, PEEK_ADMIN_EMAIL=peek-admin@localhost, and
         commented GRIST_UID/GRIST_GID=1001, each with a one-line comment.
      4. configs/localhost/init.sh: copy common/init.sh byte for byte.
      5. .github/workflows/e2e-localhost.yml exactly as the "CI" section lists the steps, with pinned
         actions as in ci.yml.
      6. Run python3 scripts/check_configs.py (it must print "ok: 1 configurations"). Push and read
         the ci.yml and e2e-localhost.yml runs on the branch. Stop conditions: if the grist-image job
         fails, stop with status=blocked and report its output (see Risks). If e2e fails at the token
         files, attach the privacyfence and grist-provisioner log lines that mention provisioning to
         the report and stop with status=blocked; do not change upstream behaviour from here.
    acceptance:
      - python3 scripts/check_configs.py prints "ok: 1 configurations"
      - cmp common/init.sh configs/localhost/init.sh exits 0
      - the e2e-localhost.yml run on the pushed branch is green
      - the ci.yml run on the pushed branch is green

  - id: p3-caddy
    title: The caddy-idp and caddy-google configurations
    depends_on: [p1-skeleton]
    complexity: M
    touches:
      - configs/caddy-idp/**
      - configs/caddy-google/**
    brief: |
      Read docs/privacyfence-suite-plan.md: "Services and addresses", "Secrets", "Grist's secrets from
      files", "Per-app settings shared by the proxy configurations", "caddy-idp", "caddy-google and
      nginx-google" and "Caddyfile".
      1. Versions: as p2 step 1 (same check and stop condition). If p2 has already merged into the
         feature branch, use the tags from configs/localhost/.env.example instead, so check 8 passes.
         Add CADDY_IMAGE_TAG=2.10.2.
      2. configs/caddy-idp/compose.yaml (`name: privacyfence-caddy-idp`) and
         configs/caddy-google/compose.yaml (`name: privacyfence-caddy-google`): the five services plus
         caddy with the fixed addresses, the pf network and the aliases, the shared per-app settings and
         the configuration's own settings, verbatim from the Design. privacyfence keeps the optional
         `env_file: [{path: privacyfence.env, required: false}]`.
      3. Each folder: Caddyfile verbatim, init.sh copied from common/init.sh, .env.example with every
         variable and a one-line comment. For caddy-idp, list the five redirect URIs in a comment
         block; for caddy-google, list the two Google redirect URIs and "Internal" consent screen.
         Include `secrets/.gitkeep` and a `secrets/README.md` of two lines naming the one file the
         folder needs and the `install -m 0600 /dev/null secrets/<name>` command.
      4. Run python3 scripts/check_configs.py (it prints "ok: 2 configurations", or 3 once p2 merged).
         Push and read ci.yml on the branch: compose and proxies must be green for both folders.
    acceptance:
      - python3 scripts/check_configs.py exits 0
      - cmp common/init.sh configs/caddy-idp/init.sh and cmp common/init.sh configs/caddy-google/init.sh exit 0
      - grep -n "remote_ip 172.30.87.10" configs/caddy-idp/Caddyfile configs/caddy-google/Caddyfile finds both
      - the ci.yml run on the pushed branch is green

  - id: p4-nginx
    title: The nginx-google configuration
    depends_on: [p3-caddy]
    complexity: S
    touches:
      - configs/nginx-google/**
    brief: |
      Read docs/privacyfence-suite-plan.md: "nginx (nginx-google)" and "caddy-google and nginx-google".
      1. Copy configs/caddy-google/compose.yaml to configs/nginx-google/compose.yaml, rename the
         project to privacyfence-nginx-google, and replace the caddy service with an nginx service
         (image nginx:${NGINX_IMAGE_TAG:?…}, the same fixed address 172.30.87.2 and aliases, the two
         mounts, ports 80 and 443, no caddy volumes). Add NGINX_IMAGE_TAG=1.28.0 to .env.example
         (otherwise copied from caddy-google, without CADDY_IMAGE_TAG).
      2. configs/nginx-google/templates/default.conf.template with the map, the port-80 redirect
         server and the three server blocks exactly as the Design lists, using ${PF_HOSTNAME},
         ${PEEK_HOSTNAME} and ${GRIST_HOSTNAME}. nginx's own variables ($host, $scheme, …) stay as they
         are (the image substitutes only defined environment variables).
      3. configs/nginx-google/certs/README.md: the two file names, that they must cover all three
         names, and that the private key file must be readable by the nginx container.
         configs/nginx-google/init.sh copied from common/init.sh; secrets/ as in caddy-google.
      4. Run python3 scripts/check_configs.py. Push and read ci.yml: the nginx -t step must be green.
    acceptance:
      - python3 scripts/check_configs.py exits 0
      - grep -n "allow 172.30.87.10" configs/nginx-google/templates/default.conf.template finds the line
      - grep -n "connection_upgrade" configs/nginx-google/templates/default.conf.template finds the map
      - the ci.yml run on the pushed branch is green

  - id: p5-docs-retire
    title: README, configuration and operations docs, changelog, delete the plan
    depends_on: [p2-localhost, p4-nginx]
    complexity: M
    touches:
      - README.md
      - docs/configurations.md
      - docs/operations.md
      - CHANGELOG.md
      - docs/privacyfence-suite-plan.md
      - docs/privacyfence-suite-plan-manual-steps.html
    brief: |
      1. README.md:
         - What the suite is, and the table of the four configurations from the Goal.
         - Prerequisites (Docker Engine 24+, Compose 2.24+; DNS and ports 80/443 for the proxy
           configurations).
         - A quick start per configuration: copy the folder, `cp .env.example .env`, edit, write the
           secret file, `docker compose up -d`, then the first sign-in. For localhost, include the
           setup code in `docker compose logs privacyfence` and the Peek password in
           `docker compose logs init`.
         - A section "What is already connected", explaining that each person who signs in to
           PrivacyFence gets their Peek account and Grist key connected; that Grist users are created
           and added to the team site; and that the Peek and Grist install admins are not provisioned.
         - The Grist full-edition sentence from Risks.
         - Links to PrivacyFence's docs/org-mode-docker.md and docs/org-mode-setup-guide.md (built-in
           Google sign-in, relay DNS records), to Peek's auth docs, and to grist-provisioner's README.
      2. docs/configurations.md: one table per configuration with every .env variable, its meaning and
         where it ends up (which container variable), and the generated files in suite-secrets with
         their owners.
      3. docs/operations.md:
         - Upgrade: change the tags, then `docker compose pull && docker compose up -d`.
         - Backup: stop, archive each volume with a `docker run --rm -v <volume>:/v alpine tar`
           command, start.
         - Rotating a generated secret: delete the file from suite-secrets with
           `docker compose run --rm init rm /run/suite/<name>`, then `docker compose up -d` and restart
           every service.
         - Rotating the IdP or Google secret: replace secrets/<name>, then `docker compose up -d`.
         - Removing a person: PrivacyFence's `--peek-release` and `--grist-release` through
           `docker compose exec`.
         - Troubleshooting: the init log lines, and where each app logs provisioning.
      4. CHANGELOG.md under "## [Unreleased]", "### Added":
         "- The four configurations: caddy-idp, caddy-google, nginx-google and localhost, with generated provisioning keys and Peek and Grist connected at sign-in."
      5. Delete docs/privacyfence-suite-plan.md and docs/privacyfence-suite-plan-manual-steps.html;
         `grep -rn "privacyfence-suite-plan" .` finds nothing.
    acceptance:
      - test ! -e docs/privacyfence-suite-plan.md && test ! -e docs/privacyfence-suite-plan-manual-steps.html
      - grep -rn "privacyfence-suite-plan" . finds nothing
      - grep -n "What is already connected" README.md finds the section
      - grep -c "^| " docs/configurations.md prints 20 or more
```
