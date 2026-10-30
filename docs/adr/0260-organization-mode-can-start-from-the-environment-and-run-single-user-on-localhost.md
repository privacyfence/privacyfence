# ADR 0260: Organization mode can start from the environment and run single-user on localhost

## Status

Accepted — 2026-10-30. Implemented: `src/privacyfence/org_mode.py`, `src/privacyfence/web/single_user_auth.py`, `deploy/docker/`.
Relates to [ADR 0011](0011-org-mode-runs-its-own-oauth-authorization-server.md), [ADR 0177](0177-an-org-mode-mcp-client-needs-the-persons-consent-once.md) and [ADR 0213](0213-the-organization-image-runs-organization-mode-only.md).

## Context

Trying PrivacyFence on one computer should not need a signed bundle, an identity provider and a
public server. The organization image ([ADR 0213](0213-the-organization-image-runs-organization-mode-only.md))
already runs organization mode only, and built-in apps reach a `localhost` server through the relay's
loopback forwarding ([ADR 0259](0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md)).

## Decision

- **`mode` and the server address may come from the environment.** `org_mode.resolve_mode` takes the
  bundle's `mode`, else `PRIVACYFENCE_MODE` (`local` or `org`), else local; a bundle and a variable
  that disagree refuse the start. `PRIVACYFENCE_BIND_HOST`, `PRIVACYFENCE_PORT`,
  `PRIVACYFENCE_ISSUER_URL` and `PRIVACYFENCE_TRUSTED_PROXIES` fill the `server` values a bundle
  leaves out. `trusted_proxies` refuses `*`, `0.0.0.0/0` and `::/0`, and the Host allowlist never
  contains a wildcard bind address.
- **Organization mode without an IdP is single-user mode:** one administrator
  (`SINGLE_USER_PRINCIPAL`, id `single-user`), allowed only at `http://localhost` or
  `http://127.0.0.1`. Anything else raises a configuration error that says an `idp` is needed.
- **Sign-in is a setup code, then a passkey.** At start, while no passkey is enrolled, the server logs
  `Setup code: XXXX-XXXX`: eight hex digits, 32 bits, kept in memory. Entering it at `/login` creates
  the session and leads to Passkeys. Five wrong attempts across the whole process discard the code
  until the next start; a correct use discards it; it stops working for good once a passkey is
  enrolled (checked on every request); the check and the consume are under one lock. Every later
  sign-in uses the passkey. AI clients sign in through `/oauth/single-user/continue`, with the person's
  consent as on any organization server ([ADR 0177](0177-an-org-mode-mcp-client-needs-the-persons-consent-once.md)).
  Session cookies are `Secure` only when the issuer is `https`, because Safari does not keep `Secure`
  cookies on `http://localhost`.
- **Hardened by default.** A single-user install starts with step-up on and a passkey required.
- **A real server never falls back to single-user mode.** It is refused when the data directory
  belonged to an organization server (a pinned bundle key under either name, `org_server` in the admin
  config, or an imported bundle), so deleting the IdP settings cannot turn a production server into a
  setup-code server.
- **The Docker image starts in organization mode** (`PRIVACYFENCE_MODE=org`, bind `0.0.0.0` in the
  image). With no IdP and no bundle the entrypoint says it starts in single-user mode and the
  container runs as the documented `docker run -p 127.0.0.1:8765:8765 …`. The image cannot verify the
  `127.0.0.1` publish, so the docs say plainly that it is what keeps the port off the network, and to
  open `http://localhost:8765` (the Host allowlist and the passkey's relying-party id are both
  `localhost`).
- A lost passkey is recovered by stopping the container and deleting the passkey credentials file in
  the volume; the next start prints a new code.

## Alternatives considered

- **Single-user mode that still needs an IdP.** Rejected: the point is to need nothing.
- **Signing in with Google through the relay.** Rejected for the single-user case: it makes trying the
  software depend on a third party and on the person having a Workspace account
  ([ADR 0267](0267-an-organization-server-may-opt-in-to-the-built-in-google-sign-in.md) is the opt-in for servers).
- **A fixed password.** Rejected: a default credential, or one more secret to generate, store and leak.

## Consequences

- **Single-user mode is no protection against an agent that can run `docker`.** Anyone who can run
  docker, read `docker logs` or open a page on another localhost port can take over the install. The
  docs and the deprecation notice say so and recommend a separate operating-system account (rootless
  Docker), a virtual machine or a server.
- The setup code is short because it protects a loopback-only server for a few minutes; its attempt
  limit is global, so one local process can use it up, which only costs a restart.
- Single-user data is not reachable by identity-provider users if an IdP is added later; the start
  logs that.

## Verification

`tests/unit/web/test_single_user_auth.py`, `tests/unit/test_org_mode.py`,
`tests/integration/test_org_docker_image.py`, `tests/integration/test_org_docker_deploy_files.py`.
Code: `src/privacyfence/web/single_user_auth.py`, `src/privacyfence/org_mode.py` (`resolve_mode`,
`is_single_user`), `deploy/docker/Dockerfile`, `deploy/docker/docker-entrypoint.sh`.

## Related

ADR 0011, 0177, 0213, 0259, 0267.
