# Organization deployment with Docker

This page runs the organization-mode daemon and its reverse proxy as containers. It replaces
[section 3](org-mode-setup-guide.md#3-service-account-and-install) (service account and install),
[section 7](org-mode-setup-guide.md#7-reverse-proxy-and-tls) (reverse proxy and TLS) and
[section 8](org-mode-setup-guide.md#8-hardened-systemd-unit) (systemd unit) of the
[organization guide](org-mode-setup-guide.md), and the Upgrade and Backup parts of its
[section 14](org-mode-setup-guide.md#14-operations). Sections
[4](org-mode-setup-guide.md#4-identity-provider),
[5](org-mode-setup-guide.md#5-connector-apps),
[6](org-mode-setup-guide.md#6-signing-key-and-bundle),
[9](org-mode-setup-guide.md#9-first-sign-in-and-validation),
[10](org-mode-setup-guide.md#10-install-wide-and-per-user-policy),
[11](org-mode-setup-guide.md#11-ai-systems),
[12](org-mode-setup-guide.md#12-approvals-and-step-up),
[13](org-mode-setup-guide.md#13-file-delivery) apply unchanged.

## The image

| | |
|---|---|
| Image | `ghcr.io/privacyfence/privacyfence-org` |
| Tags | `X.Y.Z` for every stable release. `X.Y`, `X` and `latest` move forward only: each follows the newest stable release in its scope, and a patch release on an older line never moves them back. Pin `X.Y` in production. |
| Releases | Stable releases only. A pre-release has no image. |
| Platforms | `linux/amd64`, `linux/arm64` |
| User | `privacyfence-org`, uid and gid `10001` |
| Data volume | `/var/lib/privacyfence-org/.privacyfence` |
| Port | `8765` (not published by the Compose file below) |
| Stop signal | `SIGINT`, so `docker stop` shuts the daemon down cleanly |
| Health check | Probes the daemon's well-known endpoint from inside the container |
| Without a bundle | Refuses to start (exit 78) and says how to install one |

## Prerequisites

- Docker Engine 24 or newer with the Compose plugin 2.24 or newer.
- A public hostname with DNS pointing at the server, and ports 80 and 443 (TCP and UDP) reachable.
  Caddy obtains the certificate itself.
- Everything else in the guide's [Prerequisites](org-mode-setup-guide.md#2-prerequisites) except Python.

## Files

Work in `/opt/privacyfence-org`. Fetch the two deploy files for the release you run (replace `vX.Y.Z`):

```bash
sudo install -d /opt/privacyfence-org && cd /opt/privacyfence-org
sudo curl -fsSLO https://raw.githubusercontent.com/privacyfence/privacyfence/vX.Y.Z/deploy/docker/compose.yaml
sudo curl -fsSLO https://raw.githubusercontent.com/privacyfence/privacyfence/vX.Y.Z/deploy/docker/Caddyfile
```

### Compose file

`compose.yaml` runs the daemon read-only, with no capabilities and no published port. Only Caddy is
reachable from outside. Both containers sit on a private network with fixed addresses, so the bundle
can name Caddy as the one trusted proxy.

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
    env_file:
      - path: privacyfence.env
        required: false
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

Next to `compose.yaml`, in `.env`:

```ini
PF_HOSTNAME=pf.acme.example.com
PRIVACYFENCE_IMAGE_TAG=X.Y
```

`PF_HOSTNAME` is the public hostname from the bundle's `--server-issuer-url`. Compose refuses to
start without it.

## Build the bundle

Build it as in [section 6](org-mode-setup-guide.md#6-signing-key-and-bundle), with
`--server-bind-host 0.0.0.0 --server-trusted-proxy 172.30.87.2` in place of
`--server-trusted-proxy 127.0.0.1`, and the default port. The daemon listens on the Compose network,
which only Caddy can reach, and Caddy's fixed address is the one proxy it trusts.

## Install the bundle

The first time, while nothing runs, from `/opt/privacyfence-org`:

```bash
docker compose run --rm --no-deps -T --entrypoint sh privacyfence -c 'umask 077 && cat > /var/lib/privacyfence-org/.privacyfence/org/org_config.json' < org_config.json
```

To replace it later, while the daemon runs, use `exec` and restart:

```bash
docker compose exec -T privacyfence sh -c 'umask 077 && cat > /var/lib/privacyfence-org/.privacyfence/org/org_config.json' < org_config.json
docker compose restart privacyfence
```

Use `exec` here, not `run`: a `run` container would claim the daemon's fixed address
`172.30.87.10` while the daemon holds it.

## Start

```bash
docker compose up -d
docker compose logs -f privacyfence
```

Wait for the `Org mode active -- MCP-over-HTTP at https://...` line, then go on with
[section 9](org-mode-setup-guide.md#9-first-sign-in-and-validation).

## Environment variables

For `--audit-forwarding-http-bearer-token-env NAME`, put `NAME=token` in `privacyfence.env` next to
`compose.yaml`, mode `0600`. The file is optional. Never put the token in the bundle.

For Peek auto-provisioning ([Peek setup](peek-setup.md#automatic-accounts-organization-mode)), put
`PRIVACYFENCE_PEEK_PROVISIONING_KEY=peekprov_…` (or the name you gave `--peek-provisioning-key-env`) in the
same file. Never put the key in the bundle.

## Upgrade

Change `PRIVACYFENCE_IMAGE_TAG` in `.env`, then:

```bash
docker compose pull
docker compose up -d
```

The advice in the guide's [Upgrade](org-mode-setup-guide.md#upgrade) applies: read the release's
changelog entry first, use the `build_org_bundle.py` that matches the release for future bundle
changes, and back up first. Going back is safe only if the older version reads the data as the newer
one left it. After an upgrade, each person sees a strip linking to What's new (`/whats-new`) the first
time they open PrivacyFence, listing the changelog sections since the version they last used.

## Backup and restore

Stop the daemon, archive the data volume, start it again:

```bash
docker compose stop privacyfence
docker compose run --rm --no-deps -T --entrypoint tar privacyfence -C /var/lib/privacyfence-org/.privacyfence -czf - . > privacyfence-backup.tgz
docker compose start privacyfence
```

To restore into an empty volume, run the same command with `-xzf - < privacyfence-backup.tgz`
instead. The guide's [Backup](org-mode-setup-guide.md#backup) lists what the directory holds. The
tarball holds every person's connector credentials: encrypt it and restrict access.

## Administrative commands

Run them in the running container, for the same fixed-address reason as above:

```bash
docker compose exec privacyfence privacyfence-app --revoke-scoped-tokens PRINCIPAL_ID
docker compose exec privacyfence privacyfence-app --peek-release PRINCIPAL_ID
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
