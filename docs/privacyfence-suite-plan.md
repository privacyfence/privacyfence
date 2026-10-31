# privacyfence-suite: PrivacyFence, Peek and Grist in one Docker stack, already connected

> This plan is for the new repository `privacyfence/privacyfence-suite`. It sits on a `plan/` branch
> of `privacyfence/privacyfence` only until that repository exists. `manual_before` item
> `mb1-create-repo` copies it there, with the profile in [Appendix A](#appendix-a-claudetoolkityaml),
> and `/implement` runs it from there.

## Goal

An organization starts PrivacyFence (organization mode), Peek and Grist with one
`docker compose up -d`. Every person who signs in to PrivacyFence finds Peek and Grist already
connected, and signs in to Peek and Grist **through PrivacyFence**. Nobody creates a provisioning
key, copies a key between containers, registers Peek or Grist with an identity provider, or turns a
switch on the Admin page.

The repository ships four configurations, each a self-contained folder:

| Folder | Proxy | PrivacyFence signs people in with | Peek and Grist sign in with |
|---|---|---|---|
| `configs/caddy-idp` | Caddy (automatic HTTPS) | the organization's own OIDC identity provider (one client registration, for PrivacyFence only) | PrivacyFence |
| `configs/caddy-google` | Caddy (automatic HTTPS) | PrivacyFence's built-in Google sign-in (nothing to register) | PrivacyFence |
| `configs/nginx-google` | nginx with the operator's certificate | the same as `caddy-google` | PrivacyFence |
| `configs/localhost` | none | single-user mode: setup code, then passkey | PrivacyFence |

PrivacyFence is the OpenID Connect provider for Peek and Grist (the user's decision). Whatever
PrivacyFence uses upstream reaches Peek and Grist as the same identity.

## Current state

- **Nothing exists yet.** The repository does not exist. The starting point is the compose file in
  the request (PrivacyFence, Peek, Grist and Caddy on `172.30.87.0/24`, Caddy at `.2` with network
  aliases for the three public names) and PrivacyFence's own `deploy/docker/` files
  (`compose.yaml`, `Caddyfile`, `privacyfence.env.example`, explained in `docs/org-mode-docker.md`).
- **It needs features released in three repositories first** (separate plans, `manual_before`
  `mb2-upstream-releases`):
  - **privacyfence** needs two plans:
    - `plan/env-automatic-accounts`: `PRIVACYFENCE_PEEK_AUTO_PROVISION=true` and
      `PRIVACYFENCE_GRIST_AUTO_PROVISION=true` offer the services and turn automatic accounts on,
      including in single-user mode, where the one person is `admin@localhost`.
    - `plan/oidc-provider`: `PRIVACYFENCE_OIDC_CLIENTS` plus
      `PRIVACYFENCE_OIDC_CLIENT_<NAME>_REDIRECT_URIS` and `…_SECRET_FILE` make PrivacyFence an OIDC
      provider at `<issuer>/oidc`, with no consent page and no `end_session_endpoint`.
  - **peek** (`plan/env-bootstrap`):
    - `PEEK_ADMIN_EMAILS`.
    - Sign-in from the environment: `PEEK_OIDC_ISSUER_URL`, `PEEK_OIDC_CLIENT_ID`,
      `PEEK_OIDC_CLIENT_SECRET_FILE`, `PEEK_OIDC_SIGNUP` and `PEEK_ALLOWED_EMAIL_DOMAIN`, locked on the
      Admin page. `/setup` is skipped when SSO or Google comes from the environment, or when
      `PEEK_ADMIN_EMAILS` is set.
    - `PEEK_PROVISIONING_KEY_FILE`.
    - Peek's existing `PEEK_OIDC_ALLOW_PRIVATE_ISSUER` lets it use an issuer at a private address or
      at `http://localhost`.
  - **grist-provisioner** (single-session change): `PROVISIONER_KEY_FILE`, plus
    `PROVISIONER_GRIST_URL`, `PROVISIONER_GRIST_TEAM_SITE` and `PROVISIONER_GRIST_BOOT_KEY_FILE`, which
    create the Grist user through Grist's API and add them to the team site. The team then requires
    `PROVISIONER_ALLOWED_DOMAINS`.
- **Facts the design rests on:**
  - **PrivacyFence** accepts `http://` addresses only for `localhost`, `127.0.0.1` and `::1`. Its
    image is uid 10001, read-only, with data in `/var/lib/privacyfence-org/.privacyfence`.
  - **Peek's image** is `FROM scratch`, uid 65532, with no shell; it serves `/healthz`. Its OIDC
    callback is `<base>/oauth/oidc/callback`. It requires a verified email and adopts an existing
    account with the same email, which is how provisioned accounts sign in.
  - **Grist's image** starts as root, chowns `/persist` and drops to the `grist` user (uid 1001,
    inferred: checked in CI). It reads secrets only from the environment and serves `/status`.
  - **Grist's OIDC:**
    - the callback is `<APP_HOME_URL>/oauth2/callback`;
    - discovery runs server-side at start, so PrivacyFence must be up first;
    - it requires `email_verified` in userinfo;
    - it needs `GRIST_OIDC_IDP_SKIP_END_SESSION_ENDPOINT=true` when the provider has no end-session
      endpoint.
  - **New Grist users** do not join the team site by themselves.
  - **Grist officially lists OIDC as a full-edition feature,** but the code runs in `gristlabs/grist`.
    The user chose this approach knowing that.

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
docs/operations.md             upgrade, backup, rotating secrets, removing people, troubleshooting
.github/workflows/ci.yml       lint (below)
.github/workflows/e2e-localhost.yml
```

Each folder is self-contained because the request asks for "a stack" an operator copies and runs,
and Compose's `extends` across folders would tie every folder to the repository layout. The cost is
duplication, which `scripts/check_configs.py` keeps honest.

### Services and addresses (all configurations)

| Service | Image | Fixed address (proxy configs) | Port inside |
|---|---|---|---|
| `init` | `alpine:${ALPINE_IMAGE_TAG}` | none (`network_mode: none`) | |
| `privacyfence` | `ghcr.io/privacyfence/privacyfence-org:${PRIVACYFENCE_IMAGE_TAG}` | `172.30.87.10` | 8765 |
| `peek` | `ghcr.io/privacyfence/peek:${PEEK_IMAGE_TAG}` | `172.30.87.11` | 7700 |
| `grist` | `gristlabs/grist:${GRIST_IMAGE_TAG}` | `172.30.87.12` | 8484 |
| `grist-provisioner` | `ghcr.io/privacyfence/grist-provisioner:${GRIST_PROVISIONER_IMAGE_TAG}` | `172.30.87.13` | 8485 |
| `caddy` / `nginx` | `caddy:${CADDY_IMAGE_TAG}` / `nginx:${NGINX_IMAGE_TAG}` | `172.30.87.2` | 80, 443 |

- **Image tags.** Every image tag is `${NAME_IMAGE_TAG:?pin a version in .env}`; `.env.example`
  pins exact versions, never `latest`. The request's `ghcr.io/andras-tkcs/peek` is replaced by
  `ghcr.io/privacyfence/peek`, where Peek's release workflow publishes.
- **Hardening.** `privacyfence`, `peek` and `grist-provisioner` are `read_only: true` with
  `cap_drop: [ALL]` and `security_opt: [no-new-privileges:true]`. `privacyfence` and
  `grist-provisioner` add `tmpfs: [/tmp]`. `grist` keeps the image defaults (its entrypoint chowns
  `/persist` as root and drops privileges itself). Every service except `init` has
  `restart: unless-stopped`.
- **Network.** Network `pf` with subnet `172.30.87.0/24`. The proxy has the aliases `${PF_HOSTNAME}`,
  `${PEEK_HOSTNAME}` and `${GRIST_HOSTNAME}`, so the containers reach the public `https://` names
  through the proxy without hairpin NAT. That covers PrivacyFence calling Peek and the provisioner,
  Peek and Grist calling PrivacyFence's `/oidc` endpoints, and the provisioner calling Grist.
- **`depends_on`:**
  - every service except `init`: `init` with `condition: service_completed_successfully`;
  - `grist`: also `privacyfence` with `condition: service_healthy` (Grist fetches the OIDC discovery
    document at start and refuses to start without it);
  - `grist-provisioner`: also `grist` with `condition: service_healthy`;
  - the proxy: the four apps with `condition: service_started`.
- **Health checks:**
  - `grist`: `["CMD", "curl", "-fsS", "http://localhost:8484/status"]`, interval 10s, retries 30.
  - `grist-provisioner`:
    `["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8485/healthz', timeout=3)"]`.
  - `privacyfence` and `peek`: their images' own `HEALTHCHECK`.
- **Volumes.** `pf-data`, `peek-data`, `grist-data` (Grist's `/persist`, also mounted by the
  provisioner), `provisioner-data`, `suite-secrets`, plus `caddy-data` and `caddy-config` for Caddy.

### Secrets: generated once, never handled by a person

`init` runs `init.sh` as root on every `docker compose up`, with these mounts:

- `suite-secrets:/run/suite`, read-write;
- `./init.sh:/init.sh:ro`;
- `./secrets:/user-secrets:ro`, when present.

Every other service mounts `suite-secrets:/run/suite:ro`.

`init.sh` is POSIX `sh` using busybox only, with `set -eu` and `umask 077`.

**It generates each internal secret once, if the file is missing or empty.** The value is the prefix
followed by 32 random bytes from `/dev/urandom`, base64url without padding (43 characters). It is
written to a temp file and renamed. Each generation prints `init: generated <name>`, never the value.

| File in `/run/suite` | Prefix | Owner | Mode | Read by |
|---|---|---|---|---|
| `peek_provisioning_key` | `peekprov_` | `10001:65532` | `0440` | PrivacyFence (owner), Peek (group) |
| `grist_provisioning_key` | `gristprov_` | `10001:${GRIST_GID}` | `0440` | PrivacyFence, provisioner |
| `peek_oidc_client_secret` | none | `10001:65532` | `0440` | PrivacyFence (the OIDC client list), Peek |
| `grist_oidc_client_secret` | none | `10001:${GRIST_GID}` | `0440` | PrivacyFence, Grist (read as root by its entrypoint) |
| `grist_boot_key` | none | `${GRIST_UID}:${GRIST_GID}` | `0400` | Grist, provisioner |
| `grist_session_secret` | none | `${GRIST_UID}:${GRIST_GID}` | `0400` | Grist |

**It copies the operator's identity-provider secret on every run** (`caddy-idp` only), so a rotation
takes effect at the next `up`. If `/user-secrets/idp_client_secret` exists, it installs it as
`/run/suite/idp_client_secret`, owned `10001:10001`, mode `0400`.

`GRIST_UID` and `GRIST_GID` come from the environment, default `1001`.

The operator writes that file on the host with `install -m 0600 /dev/null secrets/idp_client_secret`
and puts the value in it. No secret is ever in `.env` or `compose.yaml`. `secrets/` is in
`.gitignore`.

Rejected:

- **Compose file secrets:** they cannot give a different owner per consuming container without swarm,
  and the images run as three different uids.
- **A `setup.sh` the operator runs once:** one more step, and the request is "no provision key
  needed".
- **Generating the keys in each app:** Peek and the provisioner would each have to export theirs to
  PrivacyFence.

### Grist's secrets from files

Grist reads only environment variables, so its service wraps the image entrypoint:

```yaml
entrypoint:
  - /bin/sh
  - -c
  - |
    export GRIST_SESSION_SECRET="$$(cat /run/suite/grist_session_secret)"
    export GRIST_BOOT_KEY="$$(cat /run/suite/grist_boot_key)"
    export GRIST_OIDC_IDP_CLIENT_SECRET="$$(cat /run/suite/grist_oidc_client_secret)"
    exec /grist/sandbox/docker_entrypoint.sh "$$@"
  - grist
command: ["node", "./sandbox/supervisor.mjs"]
```

The entrypoint path and the command must match what `docker image inspect gristlabs/grist:<tag>`
reports; `ci.yml` checks it (below).

### Sign-in wiring: PrivacyFence as the OIDC provider (all configurations)

`<PF>`, `<PEEK>` and `<GRIST>` are the public origins:

- proxy configurations: `https://${PF_HOSTNAME}`, `https://${PEEK_HOSTNAME}`,
  `https://${GRIST_HOSTNAME}`;
- localhost: `http://localhost:8765`, `http://localhost:7700`, `http://localhost:8484`.

**privacyfence:**

```
PRIVACYFENCE_OIDC_CLIENTS: peek,grist
PRIVACYFENCE_OIDC_CLIENT_PEEK_REDIRECT_URIS: <PEEK>/oauth/oidc/callback
PRIVACYFENCE_OIDC_CLIENT_PEEK_SECRET_FILE: /run/suite/peek_oidc_client_secret
PRIVACYFENCE_OIDC_CLIENT_GRIST_REDIRECT_URIS: <GRIST>/oauth2/callback
PRIVACYFENCE_OIDC_CLIENT_GRIST_SECRET_FILE: /run/suite/grist_oidc_client_secret
```

**peek:**

```
PEEK_OIDC_ISSUER_URL: <PF>/oidc
PEEK_OIDC_CLIENT_ID: peek
PEEK_OIDC_CLIENT_SECRET_FILE: /run/suite/peek_oidc_client_secret
PEEK_OIDC_ALLOW_PRIVATE_ISSUER: "true"
```

The flag is needed because, inside the stack, `${PF_HOSTNAME}` resolves to the proxy's private
address, and on localhost the issuer is `http://localhost`.

**grist:**

```
GRIST_OIDC_IDP_ISSUER: <PF>/oidc
GRIST_OIDC_IDP_CLIENT_ID: grist
GRIST_OIDC_IDP_SCOPES: openid email profile
GRIST_OIDC_IDP_SKIP_END_SESSION_ENDPOINT: "true"
```

The client secret comes through the entrypoint wrapper.

Signing out of Peek or Grist ends only that app's session; the PrivacyFence session stays.

### Per-app settings shared by the proxy configurations

**privacyfence**, in `environment:` plus the optional `env_file: [{path: privacyfence.env, required: false}]`
for extras such as an Entra group claim:

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
PrivacyFence's fixed address (`172.30.87.10`), and answers 404 to everyone else. Grist has no
`/api/provisioning` route, so nothing clashes, and no fourth hostname or certificate is needed.

**peek:**

```
PEEK_BASE_URL: https://${PEEK_HOSTNAME}
PEEK_TRUSTED_PROXY: "true"
PEEK_TRUSTED_PROXY_CIDRS: 172.30.87.2/32
PEEK_ADMIN_EMAILS: ${ADMIN_EMAILS}
PEEK_ALLOWED_EMAIL_DOMAIN: ${ALLOWED_DOMAIN}
PEEK_OIDC_SIGNUP: "true"
PEEK_PROVISIONING_KEY_FILE: /run/suite/peek_provisioning_key
```

`PEEK_OIDC_SIGNUP` lets anyone in the domain whom PrivacyFence admits sign in to Peek before their
provisioning has run.

**grist:**

```
APP_HOME_URL: https://${GRIST_HOSTNAME}
GRIST_SINGLE_ORG: ${GRIST_TEAM_SITE:-team}
GRIST_ADMIN_EMAIL: ${GRIST_ADMIN_EMAIL:?set GRIST_ADMIN_EMAIL in .env}
GRIST_IN_SERVICE: "true"
GRIST_ORG_CREATION_ANYONE: "false"
GRIST_PERSONAL_ORGS: "false"
GRIST_FORCE_LOGIN: "true"
```

The request's comment asked to check these against Grist's docs. The research confirmed what they do:

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

`GRIST_ADMIN_EMAIL` is never provisioned. `.env.example` says to use a dedicated admin address, one
that is not also used with PrivacyFence.

### `caddy-idp`: your identity provider, registered once for PrivacyFence

`.env` holds:

- `PF_HOSTNAME`, `PEEK_HOSTNAME`, `GRIST_HOSTNAME`
- `ADMIN_EMAILS`, `ALLOWED_DOMAIN`, `GRIST_ADMIN_EMAIL`
- `IDP_ISSUER`, `IDP_CLIENT_ID`
- the image tags

The secret goes in `secrets/idp_client_secret`.

The operator registers **one** confidential OIDC client for PrivacyFence, with the three redirect
URIs listed in `.env.example` and the README:

- `https://<PF_HOSTNAME>/oauth/idp/callback`
- `https://<PF_HOSTNAME>/oauth/idp/login-callback`
- `https://<PF_HOSTNAME>/oauth/stepup/callback`

The privacyfence service adds:

- `PRIVACYFENCE_IDP_ISSUER: ${IDP_ISSUER:?…}`
- `PRIVACYFENCE_IDP_CLIENT_ID: ${IDP_CLIENT_ID:?…}`
- `PRIVACYFENCE_IDP_CLIENT_SECRET_FILE: /run/suite/idp_client_secret`

### `caddy-google` and `nginx-google`: Google Workspace, nothing to register

`.env` is as in `caddy-idp`, without the `IDP_*` variables, and there is no secret file.

The privacyfence service adds `PRIVACYFENCE_IDP: builtin-google`. PrivacyFence's built-in app signs
people in through `oauth.privacyfence.eu`. The operator publishes the `_privacyfence-relay` DNS TXT
records that `docker compose exec privacyfence privacyfence-app --relay-dns-records` lists. That is
PrivacyFence's existing procedure, and the README links it.

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

- **Mounts and certificate.** `./templates:/etc/nginx/templates:ro` (the image's envsubst renders
  `default.conf.template` with the three hostnames) and `./certs:/etc/nginx/certs:ro`. The operator
  supplies `certs/fullchain.pem` and `certs/privkey.pem`, covering all three names (SAN or wildcard).
- **Rejected: an ACME companion container.** It is a second moving part that Caddy already covers,
  and an operator who wants nginx usually has a certificate process.
- **Port 80** answers `return 301 https://$host$request_uri;`.
- **One `server` block per hostname**, each with:
  - `listen 443 ssl;`, `http2 on;` and the certificate lines;
  - `client_max_body_size 100m;`;
  - `proxy_set_header` for `Host $host`, `X-Forwarded-For $proxy_add_x_forwarded_for` and
    `X-Forwarded-Proto $scheme`.
- **PrivacyFence and Peek** also get `proxy_buffering off;` and `proxy_read_timeout 3600s;`, for MCP
  streams and Peek's server-sent events.
- **Grist** also gets `proxy_http_version 1.1;`, `proxy_set_header Upgrade $http_upgrade;` and
  `proxy_set_header Connection $connection_upgrade;`, with a top-level
  `map $http_upgrade $connection_upgrade { default upgrade; '' close; }`, because Grist uses
  websockets. It also gets
  `location /api/provisioning/ { allow 172.30.87.10; deny all; proxy_pass http://grist-provisioner:8485; }`.
- **Ports:** it publishes `80:80` and `443:443`.

### `localhost`: one person, no proxy

- **Shared network namespace.** `privacyfence` owns one network namespace and publishes
  `127.0.0.1:8765:8765`, `127.0.0.1:7700:7700` and `127.0.0.1:8484:8484`. `peek`, `grist` and
  `grist-provisioner` use `network_mode: "service:privacyfence"`, so every app reaches the others at
  `http://localhost:<port>`. That is also what the browser uses, and the only `http://` form
  PrivacyFence accepts. There are no fixed addresses and no `pf` network. The browser's cookies are
  per host, not per port, so PrivacyFence's session cookie is the one the `/oidc/authorize` redirect
  needs, and the three apps' cookie names do not collide.
- **privacyfence.** It has no identity provider variables, so it runs in single-user mode at
  `http://localhost:8765`, with the sign-in wiring above and:
  - `PRIVACYFENCE_PEEK_BASE_URL: http://localhost:7700`
  - `PRIVACYFENCE_GRIST_SERVER_URL: http://localhost:8484`
  - `PRIVACYFENCE_GRIST_PROVISIONER_URL: http://localhost:8485`
  - the two key files and the two `…_AUTO_PROVISION: "true"`

  The one person is `admin@localhost`, fixed in PrivacyFence. It is provisioned at start and is
  verified (ADR 0272 in PrivacyFence).
- **peek.** It has the sign-in wiring above and:
  - `PEEK_BASE_URL: http://localhost:7700`
  - `PEEK_PROVISIONING_KEY_FILE: /run/suite/peek_provisioning_key`

  There are no admin emails and no sign-up: setup is skipped because SSO comes from the environment.
  The account PrivacyFence provisioned for `admin@localhost` is adopted at the first SSO sign-in.
  Nobody is a Peek admin on localhost; every Peek setting comes from the environment.
- **grist.** It has the sign-in wiring above and:
  - `APP_HOME_URL: http://localhost:8484`
  - `GRIST_ADMIN_EMAIL: grist-admin@localhost`
  - `GRIST_SINGLE_ORG: team`
  - `GRIST_IN_SERVICE: "true"`
  - `GRIST_ORG_CREATION_ANYONE: "false"`
  - `GRIST_PERSONAL_ORGS: "false"`
  - `GRIST_FORCE_LOGIN: "true"`

  `admin@localhost` is not the Grist install admin, so the provisioner serves it.
- **grist-provisioner.** It sets:
  - `GRIST_ADMIN_EMAIL: grist-admin@localhost`
  - `PROVISIONER_GRIST_URL: http://localhost:8484`
  - `PROVISIONER_GRIST_TEAM_SITE: team`
  - `PROVISIONER_ALLOWED_DOMAINS: localhost`
  - the key and boot-key files
- **Exposure.** Everything is published on `127.0.0.1` only. The README repeats PrivacyFence's warning
  that anyone who can run `docker` on the computer controls the install.

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
`IDP_CLIENT_ID=ci`.

`e2e-localhost.yml` runs on push, pull request and `workflow_dispatch`, with a 20-minute timeout:

1. `cd configs/localhost && cp .env.example .env && docker compose up -d --wait --wait-timeout 300`.
2. Poll for up to 180 s, every 5 s, until both files exist:
   `docker compose exec -T privacyfence test -s /var/lib/privacyfence-org/.privacyfence/users/single-user/credentials/peek_token.json`
   and the same for `grist_token.json`.
3. `docker compose exec -T grist-provisioner grist-provisioner check` prints a line matching
   `^ok: [0-9]+ Grist users, 1 linked$`.
4. `docker compose logs peek` contains `first-run setup is skipped`.
5. `curl -fsS http://127.0.0.1:7700/healthz` and `curl -fsS http://127.0.0.1:8484/status` succeed.
   `curl -fsS http://127.0.0.1:8765/oidc/.well-known/openid-configuration` returns JSON whose
   `issuer` is `http://localhost:8765/oidc`.
6. **The OIDC sign-in, scripted.** `tests/e2e/oidc_signin.py` (stdlib only, run on the runner) shows
   that Grist and Peek accept PrivacyFence as their provider without a browser:
   - It reads the single-user setup code from `docker compose logs privacyfence`.
   - It signs in to PrivacyFence by POSTing the code to `/login/setup`, as the login page's form
     does, with the setup-binding cookie it set. That gives it a session cookie. If the form's field
     names differ from what the page renders, it parses them from the page.
   - It opens `http://localhost:8484/` with a cookie jar and follows redirects through
     `/oidc/authorize` back to Grist.
   - It asserts that the final response is 200 and that `GET /api/profile/user` with the Grist
     session cookie returns `email == "admin@localhost"`.
   - It does the same for Peek: `http://localhost:7700/oauth/oidc/start` (the route name comes from
     `internal/server/routes.go` in Peek), ending on Peek's signed-in page.
7. `docker compose down`, then `docker compose up -d --wait`: the second start generates nothing.
   The `init` log has no `init: generated` line, and step 3 still passes.
8. On failure, it prints `docker compose ps` and `docker compose logs --no-color`. The logs carry no
   secret except the single-user setup code, which is per-run and used by step 6.

## Manual steps

The steps are in the [step-by-step page](https://claude.ai/artifact/S4o9VWjyncbxGMNUREgaq1), whose
source is `docs/privacyfence-suite-plan-manual-steps.html`.

- **Before:**
  - `mb1-create-repo`: create the repository with its profile and this plan.
  - `mb2-upstream-releases`: release PrivacyFence (both plans), Peek and grist-provisioner with their
    features, with public images.
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
- **Grist labels OIDC a full-edition feature.** If a Grist release ever gates it in code, every
  configuration loses Grist sign-in. That is a deliberate, known risk (the user's choice);
  the README says so in one sentence.
- **An upstream release is missing a feature.** The phase briefs read the versions from each
  repository's latest release and check its changelog for the feature. If a feature is absent, the
  phase stops with `status=blocked`.
- **Peek or Grist reject PrivacyFence's ID token or userinfo** (a claim they need, an algorithm, the
  issuer string). The e2e step 6 catches it on localhost. The worker stops with `status=blocked` and
  reports the app's log line, because the fix belongs in privacyfence's OIDC provider, not here.
- **The scripted single-user sign-in** depends on the login form's field names. The script reads them
  from the page; if the page's flow cannot be followed without JavaScript, the worker stops with
  `status=blocked` and step 6 moves to `manual_after` `ma1-localhost`.
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
    title: Release PrivacyFence (env-automatic-accounts and oidc-provider), Peek and grist-provisioner with the features this stack needs, with public images
    why: p2 to p4 pin those versions in .env.example, and e2e-localhost.yml pulls them; without them every container check fails
    done_when: The latest GitHub release of privacyfence/privacyfence lists PRIVACYFENCE_PEEK_AUTO_PROVISION and PRIVACYFENCE_OIDC_CLIENTS, privacyfence/peek's lists PEEK_OIDC_ISSUER_URL and PEEK_PROVISIONING_KEY_FILE, privacyfence/grist-provisioner's lists PROVISIONER_KEY_FILE, and `docker pull` of each image at that version works without signing in
manual_after:
  - id: ma1-localhost
    title: Run configs/localhost on your own computer and use all three apps
    why: The e2e scripts the sign-in; only a person can check the passkey, the Sign in with SSO buttons in a real browser, and an AI client using the connected Peek and Grist
  - id: ma2-caddy-google
    title: Deploy configs/caddy-google on a server with real DNS
    why: CI cannot get public certificates, use Google sign-in or the PrivacyFence relay, or exercise the OIDC flow through a proxy
  - id: ma3-caddy-idp
    title: Deploy configs/caddy-idp with your own identity provider (skip if you have none)
    why: Checks PrivacyFence against a real identity provider and that the identity reaches Peek and Grist
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
         modes (six generated files), the GRIST_UID/GRIST_GID defaults (1001), the generate-once
         rule, the copy of /user-secrets/idp_client_secret to /run/suite/idp_client_secret
         (10001:10001, 0400), and the log lines. POSIX sh only (busybox ash): no bash arrays, no [[ ]], no `local`.
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
      - tests/e2e/oidc_signin.py
    brief: |
      Read docs/privacyfence-suite-plan.md: "Services and addresses", "Secrets", "Grist's secrets from
      files", "Sign-in wiring", "localhost", "CI" (e2e-localhost.yml) and "Risks".
      1. Versions: with the GitHub MCP get_latest_release tool, read the latest release of
         privacyfence/privacyfence, privacyfence/peek and privacyfence/grist-provisioner. Check that
         the release notes mention, respectively, PRIVACYFENCE_PEEK_AUTO_PROVISION and
         PRIVACYFENCE_OIDC_CLIENTS; PEEK_OIDC_ISSUER_URL and PEEK_PROVISIONING_KEY_FILE; and
         PROVISIONER_KEY_FILE. Stop condition: if one is missing,
         stop with status=blocked naming it. Use those versions (without the leading v) as the image
         tags. Use GRIST_IMAGE_TAG=1.7.22 and ALPINE_IMAGE_TAG=3.22.
      2. configs/localhost/compose.yaml: `name: privacyfence-localhost`, the five services as
         specified (init with network_mode: none; privacyfence owning the namespace and publishing the
         three 127.0.0.1 ports; the other three with network_mode: "service:privacyfence"), the Grist
         entrypoint wrapper verbatim, health checks, depends_on, volumes (no caddy volumes, no pf
         network). A header comment of three lines saying what the folder is and pointing to
         docs/configurations.md.
      3. configs/localhost/.env.example: the image tags and commented GRIST_UID/GRIST_GID=1001, each
         with a one-line comment.
      4. configs/localhost/init.sh: copy common/init.sh byte for byte.
      5. tests/e2e/oidc_signin.py (standard library only: urllib.request with an HTTPCookieProcessor,
         http.cookiejar, html.parser) as e2e step 6 describes. It exits non-zero with a one-line
         reason on any failed assertion and never prints a cookie or code. PrivacyFence's single-user
         form posts to /login/setup (src/privacyfence/web/single_user_auth.py in privacyfence); Peek's
         OIDC start route is GET /oauth/oidc/start.
      6. .github/workflows/e2e-localhost.yml exactly as the "CI" section lists the steps, with pinned
         actions as in ci.yml.
      7. Run python3 scripts/check_configs.py (it must print "ok: 1 configurations"). Push and read
         the ci.yml and e2e-localhost.yml runs on the branch. Stop conditions, each a stop with
         status=blocked and the relevant log lines in the report, never a change to upstream behaviour
         from here:
         - the grist-image job fails (report its output; see Risks);
         - e2e fails at the token files (the privacyfence and grist-provisioner provisioning lines);
         - e2e step 6 fails (the grist, peek and privacyfence lines around the OIDC sign-in).
    acceptance:
      - python3 scripts/check_configs.py prints "ok: 1 configurations"
      - cmp common/init.sh configs/localhost/init.sh exits 0
      - the e2e-localhost.yml run on the pushed branch is green, including the scripted OIDC sign-in
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
      files", "Sign-in wiring", "Per-app settings shared by the proxy configurations", "caddy-idp",
      "caddy-google and nginx-google" and "Caddyfile".
      1. Versions: as p2 step 1 (same check and stop condition). If p2 has already merged into the
         feature branch, use the tags from configs/localhost/.env.example instead, so check 8 passes.
         Add CADDY_IMAGE_TAG=2.10.2.
      2. configs/caddy-idp/compose.yaml (`name: privacyfence-caddy-idp`) and
         configs/caddy-google/compose.yaml (`name: privacyfence-caddy-google`): the five services plus
         caddy with the fixed addresses, the pf network and the aliases, the shared per-app settings and
         the configuration's own settings, verbatim from the Design. privacyfence keeps the optional
         `env_file: [{path: privacyfence.env, required: false}]`.
      3. Each folder: Caddyfile verbatim, init.sh copied from common/init.sh, .env.example with every
         variable and a one-line comment. For caddy-idp, list PrivacyFence's three redirect URIs in a
         comment block, and add `secrets/.gitkeep` and a two-line `secrets/README.md` naming
         idp_client_secret and the `install -m 0600 /dev/null secrets/idp_client_secret` command. For
         caddy-google, a comment that nothing is registered and the relay DNS records come from
         `privacyfence-app --relay-dns-records`; no secrets/ folder.
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
         configs/nginx-google/init.sh copied from common/init.sh; no secrets/ folder, as in
         caddy-google.
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
           secret file (caddy-idp only), `docker compose up -d`, then the first sign-in. For
           localhost, include the setup code in `docker compose logs privacyfence`.
         - A section "What is already connected", explaining that:
           - Peek and Grist sign people in through PrivacyFence (Sign in with SSO), so whatever
             PrivacyFence uses upstream applies to all three;
           - each person who signs in to PrivacyFence gets their Peek account and Grist key connected;
           - Grist users are created and added to the team site;
           - the Peek and Grist install admins are not provisioned;
           - signing out of Peek or Grist leaves the PrivacyFence session.
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
         - Rotating the IdP secret: replace secrets/idp_client_secret, then `docker compose up -d`.
           Rotating an OIDC client secret: delete it as above; PrivacyFence and the app both pick up
           the new file at the restart.
         - Removing a person: PrivacyFence's `--peek-release` and `--grist-release` through
           `docker compose exec`.
         - Troubleshooting: the init log lines, and where each app logs provisioning.
      4. CHANGELOG.md under "## [Unreleased]", "### Added":
         "- The four configurations: caddy-idp, caddy-google, nginx-google and localhost, with generated provisioning keys and OIDC client secrets, Peek and Grist signing in through PrivacyFence, and connected at sign-in."
      5. Delete docs/privacyfence-suite-plan.md and docs/privacyfence-suite-plan-manual-steps.html;
         `grep -rn "privacyfence-suite-plan" .` finds nothing.
    acceptance:
      - test ! -e docs/privacyfence-suite-plan.md && test ! -e docs/privacyfence-suite-plan-manual-steps.html
      - grep -rn "privacyfence-suite-plan" . finds nothing
      - grep -n "What is already connected" README.md finds the section
      - grep -c "^| " docs/configurations.md prints 20 or more
```
