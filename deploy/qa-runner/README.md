# The QA server's host files

`https://runner.privacyfence.eu` runs the organization server built from `main`, so the QA accounts
can be connected in a browser (`docs/connector-qa.md`, "The QA server"). `.github/workflows/deploy-qa-server.yml`
redeploys it. These files are the host's side of that, installed once by hand. No secret is in any of
them.

| File | What it is | Installed as |
|---|---|---|
| `pf-qa-deploy` | POSIX shell script, no arguments. Checks the image tarball, loads it, refuses any image but `privacyfence-org:qa-main`, runs `docker compose up -d --no-build`, waits up to 120 s for health, prunes, deletes the tarball. | `/usr/local/sbin/pf-qa-deploy`, root-owned, mode 0755 |
| `sudoers.pf-qa-deploy` | `pfrunner` may run that script with no arguments, as root, and nothing else. | `/etc/sudoers.d/pf-qa-deploy`, mode 0440 |
| `compose.override.yaml` | Used with `deploy/docker/compose.yaml`: the local image, user `10001:10001`, the shared data directory, the Caddyfile and origin certificate, and Peek. | `/opt/pf-qa/run/compose.override.yaml` |
| `Caddyfile` | The proxy. Answers 404 for `/mcp` and every MCP OAuth endpoint; trusts only Cloudflare's ranges (`PF_CF_RANGES`). | `/opt/pf-qa/run/Caddyfile` |
| `privacyfence.env.example` | The server's environment, without the secrets. | `/opt/pf-qa/run/privacyfence.env`, filled in |

## Installing

As root on the host, from a checkout of this repository:

1. Create `pfrunner` with uid and gid 10001, not in the `docker` group, and
   `/home/pfrunner/qa-org-data` owned by it, mode 0700.
2. `install -m 0755 -o root -g root deploy/qa-runner/pf-qa-deploy /usr/local/sbin/pf-qa-deploy`
3. `install -m 0440 -o root -g root deploy/qa-runner/sudoers.pf-qa-deploy /etc/sudoers.d/pf-qa-deploy`,
   after `visudo -cf deploy/qa-runner/sudoers.pf-qa-deploy`.
4. `mkdir -p /opt/pf-qa/run` and copy into it `deploy/docker/compose.yaml`, then this directory's
   `compose.override.yaml` and `Caddyfile`, and `privacyfence.env` made from `privacyfence.env.example`.
   Put the QA Google OIDC client secret in `/opt/pf-qa/run/idp_client_secret` (mode 0600), and set
   `PF_HOSTNAME=runner.privacyfence.eu` and `PF_CF_RANGES` (Cloudflare's published ranges from
   `https://www.cloudflare.com/ips-v4` and `-v6`, space-separated) in `/opt/pf-qa/run/.env`.
5. Put the Cloudflare Origin CA certificate and key in `/etc/pf-qa/` as `origin.pem` and `origin.key`.
6. Add a Cloudflare WAF custom rule on `runner.privacyfence.eu` that blocks the paths the Caddyfile
   blocks, so `/mcp` stays closed even if the Caddyfile is changed by mistake.

The repository copies are the source of truth; reinstall a file when it changes here. The
workflow redeploys only the image: it does not touch these host files.
