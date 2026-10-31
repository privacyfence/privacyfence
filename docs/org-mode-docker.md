# Organization deployment with Docker

This page runs PrivacyFence from the container image: on your own computer for yourself ([Run it on
this computer](#run-it-on-this-computer)), or as an organization server with its reverse proxy
([Quick start](#quick-start)). The server flow replaces
[section 3](org-mode-setup-guide.md#3-service-account-and-install) (service account and install),
[section 7](org-mode-setup-guide.md#7-reverse-proxy-and-tls) (reverse proxy and TLS) and
[section 8](org-mode-setup-guide.md#8-hardened-systemd-unit) (systemd unit) of the
[organization guide](org-mode-setup-guide.md), and the Upgrade and Backup parts of its
[section 14](org-mode-setup-guide.md#14-operations). Sections
[4](org-mode-setup-guide.md#4-identity-provider),
[5](org-mode-setup-guide.md#5-connector-apps),
[9](org-mode-setup-guide.md#9-first-sign-in-and-validation),
[10](org-mode-setup-guide.md#10-install-wide-and-per-user-policy),
[11](org-mode-setup-guide.md#11-ai-systems),
[12](org-mode-setup-guide.md#12-approvals-and-step-up),
[13](org-mode-setup-guide.md#13-file-delivery) apply unchanged, and
[section 6](org-mode-setup-guide.md#6-server-settings-and-your-own-apps) applies with the environment
file being `privacyfence.env`.

## The image

| | |
|---|---|
| Image | `ghcr.io/privacyfence/privacyfence-org` |
| Tags | A stable release gets `X.Y.Z`, `X.Y`, `X` and `latest`. `X.Y`, `X` and `latest` move forward only: each follows the newest stable release in its scope, and a patch release on an older line never moves them back. Pin `X.Y` in production. |
| Releases | Every release. A pre-release (`X.Y.ZaN`, `bN`, `rcN`) gets only its exact version as a tag, so `X.Y`, `X` and `latest`, and with them any unattended upgrade, never reach a pre-release; to try one, set `PRIVACYFENCE_IMAGE_TAG` to its exact version (ADR 0268). |
| Platforms | `linux/amd64`, `linux/arm64` |
| User | `privacyfence-org`, uid and gid `10001` |
| Data volume | `/var/lib/privacyfence-org/.privacyfence` |
| Port | `8765` (published to `127.0.0.1` only by the single-user command below, and not published by the Compose file) |
| Mode | The image sets `PRIVACYFENCE_MODE=org` and `PRIVACYFENCE_BIND_HOST=0.0.0.0`; see [Environment variables](#environment-variables) |
| Stop signal | `SIGINT`, so `docker stop` shuts the daemon down cleanly |
| Health check | Probes the daemon's well-known endpoint from inside the container, on `PRIVACYFENCE_PORT` (8765 by default) |
| With no identity provider | Starts in single-user mode on localhost ([below](#run-it-on-this-computer)). It does not refuse to start. |

## Run it on this computer

This is PrivacyFence for one person, in a container, with its own built-in sign-in for Google, Slack,
Salesforce, Atlassian and Miro (organization mode and this image only; desktop mode, which is
deprecated, has none). It is the same image and the same mode as a server, with no identity
provider: one administrator, served only at `http://localhost`.

> **Anyone who can run `docker` on this computer controls the install.** They can read the setup
> code from `docker logs`, open the data volume, or start another container against it. Single-user mode
> protects against other computers and other web pages, not against a local user or an AI agent that has a
> shell and the Docker socket. Run it under a separate operating system account (with rootless
> Docker) or in a virtual machine, or use an organization server instead.

```bash
docker run --rm -p 127.0.0.1:8765:8765 \
  -v pf-data:/var/lib/privacyfence-org/.privacyfence \
  ghcr.io/privacyfence/privacyfence-org:latest
```

- **The `127.0.0.1:` in `-p` matters.** Without it Docker publishes the port on every network
  interface, and anyone on your network can reach a server whose only protection is a setup code. The
  image cannot check how you published it, so this is on you. Publish the same port number the
  container listens on (`PRIVACYFENCE_PORT`, 8765 by default): the browser's address must be the one
  the server believes.
- **The data volume `pf-data`** keeps your passkey, your connected accounts and your settings. Without
  `-v` they are lost when the container goes. To run in the background, add `-d --name privacyfence`
  and drop `--rm`; use `docker logs privacyfence` instead of the terminal.
- **Open `http://localhost:8765`, not `http://127.0.0.1:8765`.** The server accepts only requests
  addressed to `localhost`, and your passkey is bound to that name.

**Sign in.** The first start prints, in the container's output (`docker logs`):

```text
Setup code: XXXX-XXXX. Open http://localhost:8765/login to set up your passkey.
```

Open that address and enter the code (capitals and the dash do not matter). It signs you in as the
one administrator and takes you to **Passkeys**, where you add a passkey; every later sign-in uses the
passkey, and so do approvals. The code works once, is used up by five wrong attempts (restart the
container for a new one), and **stops working for good once a passkey is enrolled**: a restart with no
passkey enrolled prints a new code, and with one enrolled prints none. Step-up is on from the start, and
every change on the Admin page asks for the passkey.

**Connect a service.** A fresh install offers Google, Slack, Salesforce, Atlassian, Miro and Telegram;
change that under **Admin → Services**. Open `/connect` and connect your accounts. The built-in apps
sign in through the PrivacyFence relay, which needs nothing from you on `localhost` and sends the
browser straight back to this computer ([built-in apps](org-mode-setup-guide.md#built-in-apps-through-the-relay)).
Your container needs outbound HTTPS to `oauth.privacyfence.eu` for that.

**Add it to an AI client.** Use `http://localhost:8765/mcp` as the connector address in a client that
runs on this computer (Claude Desktop, Claude Code). claude.ai cannot reach `localhost`. The client
sends you to `/login` to sign in with your passkey and then to a consent page; after adding your
passkey for the first time, a **Continue** link on Passkeys takes the pending authorization on.
[Section 9](org-mode-setup-guide.md#add-privacyfence-to-an-ai-client) has the steps per client.

**If you lose your passkey.** Stop the container, delete the passkey file from the volume, and start
it again; the next start prints a new setup code. With the volume above:

```bash
docker run --rm -v pf-data:/data --entrypoint sh ghcr.io/privacyfence/privacyfence-org:latest \
  -c 'find /data/users/single-user -name webauthn_credentials.json -delete'
```

The file is `users/single-user/authority/webauthn_credentials.json` in the volume. Nothing else is
lost. That is also why the volume belongs to you alone.

### From one computer to a server

Single-user mode never turns into a server by itself. To serve other people, give the same volume an
identity provider and a public address: the `PRIVACYFENCE_IDP_*` variables in `privacyfence.env` and
a public `PF_HOSTNAME` in `.env` ([Quick start](#quick-start)), or the `idp` section of
a signed 6.x bundle. Before that, note what carries over: your single-user connections and settings
stay in `users/single-user/`, which no identity-provider account can reach (the start logs that), and
passkeys are bound to `localhost`. A new data volume is the cleaner start.

## Quick start

A multi-user server with the environment alone, no bundle to build:

1. **DNS and ports.** Point a public hostname at the Docker host and open TCP 443; Caddy gets the
   certificate itself. Also open TCP 80 if you can: it gives certificate renewal a second route and
   redirects `http://` to `https://`. UDP 443 is optional: it enables HTTP/3, and without it clients
   use TCP. With the built-in Google sign-in or built-in apps, TCP 443 must be reachable from the
   whole internet, not only from given addresses: the PrivacyFence relay checks the server from
   outside ([Built-in apps through the relay](org-mode-setup-guide.md#built-in-apps-through-the-relay)).
   If the DNS is on Cloudflare, set the record to **DNS only**: Caddy cannot get its certificate
   through Cloudflare's proxy (the browser then shows Cloudflare's "SSL handshake failed" error), and
   the proxy also cuts long MCP connections after 100 seconds.
2. **Register one OIDC app** with your identity provider, with the three redirect URIs for your
   hostname ([the organization guide, section 4](org-mode-setup-guide.md#4-identity-provider)). Or, for
   a small Google Workspace team, use the [built-in Google sign-in](org-mode-setup-guide.md#built-in-google-sign-in)
   and register nothing: set `PRIVACYFENCE_IDP=builtin-google`, the admin emails and the allowed
   domains instead of step 5's identity provider lines.
3. **Create the deploy folder.** Keep everything for this server in one folder owned by root. Compose
   names the project after the folder and reads `.env` from it, and backups and upgrades are simpler
   with all of it in one place:

   ```bash
   sudo install -d -o root -g root -m 0750 /opt/privacyfence-org
   cd /opt/privacyfence-org
   ```

   Run every `docker compose` command in this folder with `sudo`. Anyone who can run Docker controls
   the install anyway, so a separate service user adds nothing unless you run rootless Docker; do not
   add people to the `docker` group for this. The files end up like this:

   | File | Owner | Mode | Why |
   |---|---|---|---|
   | `compose.yaml`, `Caddyfile` | root | 0644 | No secrets. |
   | `.env` | root | 0600 | Host name and image tag, read by Compose as root. |
   | `privacyfence.env` | root | 0600 | May hold plain secrets. |
   | `idp_client_secret`, `peek_provisioning_key`, `grist_provisioning_key` | `10001:10001` | 0400 | Read inside the container by the image's user, through Docker secrets. |

   PrivacyFence's own data does not go in this folder: it stays in the named volume `pf-data` (for a
   host directory instead, see [A bind-mounted data directory](#a-bind-mounted-data-directory)). Back
   the folder up along with the volume: `privacyfence.env`, `.env` and the secret files are not in it
   ([Backup and restore](#backup-and-restore)).
4. **Fetch the deploy files** for the release you run. Replace `vX.Y.Z` with that release, the same
   version you will put in `PRIVACYFENCE_IMAGE_TAG`, so the files match the image:

   ```bash
   sudo curl -fsSLO https://raw.githubusercontent.com/privacyfence/privacyfence/vX.Y.Z/deploy/docker/compose.yaml
   sudo curl -fsSLO https://raw.githubusercontent.com/privacyfence/privacyfence/vX.Y.Z/deploy/docker/Caddyfile
   sudo curl -fsSL -o privacyfence.env https://raw.githubusercontent.com/privacyfence/privacyfence/vX.Y.Z/deploy/docker/privacyfence.env.example
   sudo curl -fsSL -o .env https://raw.githubusercontent.com/privacyfence/privacyfence/vX.Y.Z/deploy/docker/env.example
   sudo chmod 600 privacyfence.env .env
   ```

   Releases from before `env.example` existed answer the last download with a 404. For those, create
   `.env` yourself instead of the fourth `curl` line (step 6 says what goes in it):

   ```bash
   sudo install -m 0600 /dev/null .env
   sudo tee .env >/dev/null <<'EOF'
   PF_HOSTNAME=
   PRIVACYFENCE_IMAGE_TAG=
   EOF
   ```

   [Files](#files) explains each of them.
5. **Fill in `privacyfence.env`** (`sudo nano privacyfence.env`, or your editor): the identity
   provider and who is an admin. The file's comments say what each line does. The server address and
   the proxy address are not in it: `compose.yaml` sets them from `PF_HOSTNAME` in `.env` (step 6), so
   the domain is typed once.

   Keep the identity provider's client secret out of `docker inspect`: put it in a file owned by the
   image's user and let Compose mount it as a Docker secret. Create the empty file with the right owner
   and mode first, then write the secret into it, in this order:

   ```bash
   sudo install -m 0400 -o 10001 -g 10001 /dev/null idp_client_secret
   sudo sh -c 'cat > idp_client_secret'   # paste the secret, then press Ctrl-D
   ```

   In `privacyfence.env` set `PRIVACYFENCE_IDP_CLIENT_SECRET_FILE=/run/secrets/idp_client_secret`, and
   in `compose.yaml` uncomment the `secrets:` lines (under the service and at the bottom). With the
   built-in Google sign-in there is no client secret: skip this part.
6. **Fill in `.env`:** set `PF_HOSTNAME` to the server's public host name, with no `https://` and no
   path, such as `pf.acme.example.com`, and `PRIVACYFENCE_IMAGE_TAG` to the release from step 4, such
   as `7.4` ([.env](#env)). This is the only place the domain is set: Caddy serves and gets the
   certificate for it, and `compose.yaml` gives PrivacyFence `PRIVACYFENCE_ISSUER_URL=https://<PF_HOSTNAME>`.
   Compose refuses to start without it. For a pre-release, such as `7.0.0a1`, set its exact version: a
   pre-release gets no `X.Y`, `X` or `latest` tag. Do not leave `PRIVACYFENCE_IMAGE_TAG` empty either:
   `compose.yaml` then falls back to `latest`, the newest stable release, not the one whose files you
   fetched.
7. **Start it:** `sudo docker compose up -d`, then `sudo docker compose logs -f privacyfence` until the
   `Org mode active -- MCP-over-HTTP at https://...` line.
8. **Sign in** as a listed admin (an email in `PRIVACYFENCE_ADMIN_EMAILS`, or a person in the admin
   group), and open `/security` to add a passkey. With the built-in Google sign-in, first publish the
   DNS records the log lists (`sudo docker compose exec privacyfence privacyfence-app --relay-dns-records`).
   Until the server has registered with the relay, `/login` says `Sign-in is starting up`; until a
   domain's record is checked, it says `Sign-in is waiting for DNS`. In the log
   (`sudo docker compose logs privacyfence | grep -i relay`):
   - `Registration with the PrivacyFence sign-in relay failed: proof_failed`: the relay could not fetch
     `https://<PF_HOSTNAME>/.well-known/privacyfence-relay` from outside. Usually Caddy has no
     certificate yet, or a proxy or firewall is in front (step 1). The server retries by itself.
   - `… failed: too_soon`: the relay limits how often a server registers. Wait; the server retries by
     itself.
   - After publishing a DNS record, restart once (`sudo docker compose restart privacyfence`) to have
     the domain checked sooner than the hourly check. Do not restart repeatedly: each restart counts
     against the relay's limit.
9. **Admin → Services → offer** the services you want. Services that use a built-in app work at
   once (the server registers with the relay by itself; Admin → Services shows the status); to use your
   own app for a provider, [sign and upload a connector config](org-mode-setup-guide.md#signed-connector-configs).
10. **Optional, Peek and Grist:** give the server their addresses in `privacyfence.env`
    (`PRIVACYFENCE_PEEK_BASE_URL`, `PRIVACYFENCE_GRIST_SERVER_URL`, `PRIVACYFENCE_GRIST_PROVISIONER_URL`),
    put each provisioning key in its own secret file the same way as step 5
    (`PRIVACYFENCE_PEEK_PROVISIONING_KEY_FILE=/run/secrets/peek_provisioning_key`, likewise
    `PRIVACYFENCE_GRIST_PROVISIONING_KEY_FILE`), uncomment those `secrets:` lines, restart, and turn
    automatic accounts on under Admin → Services ([Peek setup](peek-setup.md#automatic-accounts-organization-mode),
    [Grist setup](grist-setup.md#automatic-api-keys-organization-mode)).

Everything else (file delivery, step-up, sign-in restrictions, scoped tokens, audit forwarding) is on
the [Admin page](org-mode-setup-guide.md#the-admin-page). Outbound HTTPS from the container must reach your identity
provider, the connectors' APIs, and `oauth.privacyfence.eu` when you use built-in apps or the built-in
Google sign-in.

## Prerequisites

- Docker Engine 24 or newer with the Compose plugin 2.24 or newer.
- A public hostname with DNS pointing at the server (not proxied by Cloudflare), and TCP 443
  reachable; TCP 80 is recommended and UDP 443 optional ([Quick start](#quick-start), step 1). Caddy
  obtains the certificate itself.
- Everything else in the guide's [Prerequisites](org-mode-setup-guide.md#2-prerequisites) except Python.

## Files

The deploy folder (`/opt/privacyfence-org`) holds four files from `deploy/docker/` of the release you
run: `compose.yaml`, `Caddyfile`, `privacyfence.env` (from `privacyfence.env.example`) and `.env`
(from `env.example`). [Quick start](#quick-start), steps 3 and 4, creates the folder and fetches them.

### Compose file

`compose.yaml` runs the daemon read-only, with no capabilities and no published port. Only Caddy is
reachable from outside. Both containers sit on a private network with fixed addresses, so
`PRIVACYFENCE_TRUSTED_PROXIES` can name Caddy as the one trusted proxy. The file sets that and
`PRIVACYFENCE_ISSUER_URL` (from `PF_HOSTNAME` in `.env`) itself; they win over the same names in
`privacyfence.env`.

```yaml
# PrivacyFence organization mode with Caddy in front. docs/org-mode-docker.md explains every line.
name: privacyfence-org

services:
  privacyfence:
    image: ghcr.io/privacyfence/privacyfence-org:${PRIVACYFENCE_IMAGE_TAG:-latest}
    restart: unless-stopped
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    stop_grace_period: 30s
    # Set here from .env, so the domain is typed once. These win over the same names in
    # privacyfence.env.
    environment:
      PRIVACYFENCE_ISSUER_URL: https://${PF_HOSTNAME:?set PF_HOSTNAME in .env}
      PRIVACYFENCE_TRUSTED_PROXIES: 172.30.87.2
    env_file:
      - path: privacyfence.env
        required: false
    # Keep the identity provider's client secret out of `docker inspect`: put it in a file, name
    # that file with PRIVACYFENCE_IDP_CLIENT_SECRET_FILE=/run/secrets/idp_client_secret in
    # privacyfence.env, and uncomment this and the top-level `secrets:` block below. The Peek and
    # Grist provisioning keys work the same way, with PRIVACYFENCE_PEEK_PROVISIONING_KEY_FILE and
    # PRIVACYFENCE_GRIST_PROVISIONING_KEY_FILE.
    # secrets:
    #   - idp_client_secret
    #   - peek_provisioning_key
    #   - grist_provisioning_key
    volumes:
      - pf-data:/var/lib/privacyfence-org/.privacyfence
    networks:
      pf:
        ipv4_address: 172.30.87.10

  caddy:
    image: caddy:2
    restart: unless-stopped
    depends_on:
      - privacyfence
    environment:
      PF_HOSTNAME: ${PF_HOSTNAME:?set PF_HOSTNAME in .env}
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    networks:
      pf:
        ipv4_address: 172.30.87.2

networks:
  pf:
    ipam:
      config:
        - subnet: 172.30.87.0/24

# secrets:
#   idp_client_secret:
#     file: ./idp_client_secret
#   peek_provisioning_key:
#     file: ./peek_provisioning_key
#   grist_provisioning_key:
#     file: ./grist_provisioning_key

volumes:
  pf-data:
  caddy-data:
  caddy-config:
```

### Caddyfile

```
{$PF_HOSTNAME} {
    reverse_proxy privacyfence:8765
}
```

### .env

Next to `compose.yaml`, in `.env` (from `env.example`):

```ini
PF_HOSTNAME=pf.acme.example.com
PRIVACYFENCE_IMAGE_TAG=X.Y
```

`PF_HOSTNAME` is the server's public host name, the only place the domain is set: Caddy gets its
certificate for it, and `compose.yaml` sets `PRIVACYFENCE_ISSUER_URL` to `https://<PF_HOSTNAME>`.
Compose refuses to start without it. `PRIVACYFENCE_IMAGE_TAG` is the release you fetched the files
from; a pre-release needs its exact version.

## Start

```bash
docker compose up -d
docker compose logs -f privacyfence
```

Wait for the `Org mode active -- MCP-over-HTTP at https://...` line, then go on with
[section 9](org-mode-setup-guide.md#9-first-sign-in-and-validation). A change to `privacyfence.env`
needs `docker compose up -d` again (it recreates the container); a change on the Admin page that
needs a restart needs `docker compose restart privacyfence`.

## Environment variables

`privacyfence.env` is read as the daemon's environment. It is optional: with nothing in it the image
starts in single-user mode. The image already sets `PRIVACYFENCE_MODE=org` and
`PRIVACYFENCE_BIND_HOST=0.0.0.0`.

| Variable | Default | Use |
|---|---|---|
| `PRIVACYFENCE_MODE` | `org` (set by the image) | Leave it. |
| `PRIVACYFENCE_BIND_HOST` | `0.0.0.0` (set by the image) | The address inside the container. Only Caddy reaches it. |
| `PRIVACYFENCE_PORT` | `8765` | The port inside the container. Change it only together with the Caddyfile (`reverse_proxy privacyfence:<port>`). |
| `PRIVACYFENCE_ISSUER_URL` | `https://<PF_HOSTNAME>` (set by `compose.yaml`) | The public origin, such as `https://pf.acme.example.com`. `compose.yaml` builds it from `PF_HOSTNAME` in `.env`; a value in `privacyfence.env` is ignored. Without Compose (single-user `docker run`) it defaults to `http://localhost:<port>`. |
| `PRIVACYFENCE_TRUSTED_PROXIES` | `172.30.87.2` (set by `compose.yaml`) | The Caddy container's fixed address in `compose.yaml`. Without it the daemon ignores `X-Forwarded-For` and `X-Forwarded-Proto`; a value in `privacyfence.env` is ignored. |
| `PRIVACYFENCE_IDP_ISSUER`, `PRIVACYFENCE_IDP_CLIENT_ID` | none | Your identity provider. Both or neither. |
| `PRIVACYFENCE_IDP_CLIENT_SECRET_FILE` | none | A Docker secret file (`/run/secrets/idp_client_secret`). `PRIVACYFENCE_IDP_CLIENT_SECRET` is the plain form; never both. |
| `PRIVACYFENCE_ADMIN_EMAILS` | none | Admins by verified email. Microsoft Entra ID uses `PRIVACYFENCE_IDP_ADMIN_GROUP_CLAIM` and `PRIVACYFENCE_IDP_ADMIN_GROUP_VALUES` instead ([guide](org-mode-setup-guide.md#who-is-an-admin)). |
| `PRIVACYFENCE_IDP`, `PRIVACYFENCE_ALLOWED_DOMAINS` | none | `builtin-google` for the [built-in Google sign-in](org-mode-setup-guide.md#built-in-google-sign-in), and its allowed domains (first start only). |
| `PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL` | `false` | `true` on Microsoft Entra ID so allowed domains work. |
| `PRIVACYFENCE_PEEK_BASE_URL`, `PRIVACYFENCE_GRIST_SERVER_URL`, `PRIVACYFENCE_GRIST_PROVISIONER_URL` | none | The Peek and Grist addresses. |
| `PRIVACYFENCE_PEEK_PROVISIONING_KEY_FILE`, `PRIVACYFENCE_GRIST_PROVISIONING_KEY_FILE` | none | The provisioning keys as Docker secret files. The plain `…_PROVISIONING_KEY` forms also work; never both. |

The [configuration reference](configuration-reference.md#environment-variables) has the full list.
For audit forwarding to an HTTPS endpoint with a bearer token, put `NAME=token` in `privacyfence.env`
and name `NAME` as `audit_forwarding.http.bearer_token_env` in `org/admin_config.json`
([configuration reference](configuration-reference.md#admin-config-orgadmin_configjson)). Never put
a token or a key in the Compose file.

## What stays in the data volume

Besides each person's data, the volume holds, in `org/`, `admin_config.json` (everything set on the
Admin page), `connector_configs/` and its signing-key pin (your own apps),
`relay_token.txt` and `relay_status.json`. All of them are part of the backup. Replacing
`relay_token.txt` means publishing the built-in Google sign-in's `_privacyfence-relay` DNS TXT
records again with the new value, which the log lists. The configuration reference lists every file
([org directory](configuration-reference.md#files-in-the-org-directory)).

## Your own apps

Built-in apps through the relay need no setup. For your organization's own app for a provider, sign
its client ID and secret as a connector config and upload it on **Admin → Services**: both signing
tools, the fingerprint check on the first upload, key rotation and the serial rule are in
[the organization guide](org-mode-setup-guide.md#signed-connector-configs). There is no bundle to build
or install: the files an organization server takes are `privacyfence.env`, the Admin page and
connector configs.

## An upgraded server's 6.x bundle

A server upgraded from 6.x has `org/org_config.json` installed in its volume; 7.x copies it to
`org/org_bundle.json` and reads it only for the server and identity-provider settings. To move those
to `privacyfence.env` and retire the bundle:

```bash
docker compose exec privacyfence privacyfence-app --print-env-from-bundle \
  --idp-secret-file /var/lib/privacyfence-org/.privacyfence/org/idp_client_secret
docker compose cp privacyfence:/var/lib/privacyfence-org/.privacyfence/org/idp_client_secret ./idp_client_secret
docker compose exec privacyfence rm /var/lib/privacyfence-org/.privacyfence/org/idp_client_secret
# put the printed lines in privacyfence.env, mount ./idp_client_secret as the Docker secret, then:
docker compose up -d
docker compose exec privacyfence privacyfence-app --retire-bundle
```

[Section 6 of the guide](org-mode-setup-guide.md#a-6x-bundle-on-an-upgraded-server) explains each
step. Keep `org/org_config.json` if you may go back to 6.x
([Going back to 6.x](org-mode-setup-guide.md#going-back-to-6x)).

## Upgrade

Change `PRIVACYFENCE_IMAGE_TAG` in `.env`, then:

```bash
docker compose pull
docker compose up -d
```

The advice in the guide's [Upgrade](org-mode-setup-guide.md#upgrade) applies: read the release's
changelog entry first and back up first. Going back is safe only if the older version reads the data as
the newer one left it ([Going back to 6.x](org-mode-setup-guide.md#going-back-to-6x)). Before moving
to 8.x, check the Admin page says `Ready for PrivacyFence 8: yes.`
([Upgrading to 8.x](org-mode-setup-guide.md#upgrading-to-8x)). After an upgrade, each person sees a
strip linking to What's new (`/whats-new`) the first time they open PrivacyFence, listing the
changelog sections since the version they last used.

## Backup and restore

Stop the daemon, archive the data volume, start it again:

```bash
docker compose stop privacyfence
docker compose run --rm --no-deps -T --entrypoint tar privacyfence -C /var/lib/privacyfence-org/.privacyfence -czf - . > privacyfence-backup.tgz
docker compose start privacyfence
```

To restore into an empty volume, run the same command with `-xzf - < privacyfence-backup.tgz`
instead. The guide's [Backup](org-mode-setup-guide.md#backup) lists what the directory holds,
`org/admin_config.json`, `org/relay_token.txt` and `org/relay_status.json` among it. The tarball holds
every person's connector credentials and your connector configs' client secrets: encrypt it and restrict
access. `privacyfence.env`, `.env` and the Docker secret files are outside the volume: back them up
separately.

## Administrative commands

Run them in the running container, for the same fixed-address reason as above:

```bash
docker compose exec privacyfence privacyfence-app --revoke-scoped-tokens PRINCIPAL_ID
docker compose exec privacyfence privacyfence-app --peek-release PRINCIPAL_ID
docker compose exec privacyfence privacyfence-app --grist-release PRINCIPAL_ID
docker compose exec privacyfence privacyfence-app --relay-dns-records
```

## Verify the image

```bash
gh attestation verify oci://ghcr.io/privacyfence/privacyfence-org:X.Y.Z --owner privacyfence
```

"Verification succeeded" proves GitHub built that image from the tagged commit of this repository,
with the `build.yml` and `publish-org-image.yml` workflows.

## A bind-mounted data directory

Instead of the named volume, a host directory must be owned by `10001:10001` with mode `0700`, or
the daemon refuses to start:

```bash
sudo install -d -o 10001 -g 10001 -m 700 /srv/privacyfence-org
```
