# ADR 0264: An organization server can start from environment variables

## Status

Accepted — 2026-10-30. Implemented: `src/privacyfence/org_mode.py` (`idp_section`), `deploy/docker/privacyfence.env.example`.
Amends [ADR 0016](0016-org-config-bundle-hash-log-and-signing.md): organization mode no longer always needs a signed bundle.
Amends [ADR 0011](0011-org-mode-runs-its-own-oauth-authorization-server.md): the IdP may come from environment variables.
Relates to [ADR 0241](0241-privacyfence-provisions-peek-accounts-with-a-scoped-key-and-only-calls-peek.md) and [ADR 0244](0244-grist-api-keys-come-from-a-grist-side-provisioner.md).

## Context

With built-in apps ([ADR 0255](0255-built-in-oauth-apps-ship-in-every-distribution.md)) and the relay, the
identity provider is the only app an operator still registers, and the signed bundle carried little
besides the IdP and the server address, both of which fit in `privacyfence.env`. A multi-user server's
bootstrap becomes: point DNS at the host, register one OIDC app with three redirect URIs, write
`privacyfence.env`, run `docker compose up -d`, sign in as an administrator, offer services.

## Decision

- **The IdP comes from `PRIVACYFENCE_IDP_ISSUER` and `PRIVACYFENCE_IDP_CLIENT_ID`** (both or neither;
  one alone refuses the start rather than falling into single-user mode), with the secret in
  `PRIVACYFENCE_IDP_CLIENT_SECRET` or, preferably, the file named by
  `PRIVACYFENCE_IDP_CLIENT_SECRET_FILE` (a Docker secret). Setting both refuses the start. Optional:
  `PRIVACYFENCE_IDP_ADMIN_GROUP_CLAIM`, `PRIVACYFENCE_IDP_ADMIN_GROUP_VALUES`,
  `PRIVACYFENCE_IDP_STEP_UP_ACR_VALUES` (comma-separated), `PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL`
  and `PRIVACYFENCE_ADMIN_EMAILS` ([ADR 0265](0265-admins-can-be-named-by-verified-email.md)).
- **A bundle's `idp` section wins, whole.** Fields are never mixed between the bundle and the
  environment. When both exist, the environment variables are ignored with one warning. No bundle
  option delegates the IdP to the environment, because organization bundles can no longer be built
  ([ADR 0269](0269-organization-servers-use-signed-connector-configs-instead-of-a-bundle.md)). A
  legacy organization bundle without an `idp` section refuses the start and names
  `privacyfence-app --retire-bundle`.
- **A server stays a server.** The first start with an IdP records `org_server` in the admin config
  before IdP discovery, and that, with the other signs of a former server, makes the single-user
  fallback refuse
  ([ADR 0260](0260-organization-mode-can-start-from-the-environment-and-run-single-user-on-localhost.md)).
- **The server and Peek/Grist addresses** come from `PRIVACYFENCE_ISSUER_URL`, `PRIVACYFENCE_PORT`,
  `PRIVACYFENCE_BIND_HOST`, `PRIVACYFENCE_TRUSTED_PROXIES`, `PRIVACYFENCE_PEEK_BASE_URL`,
  `PRIVACYFENCE_GRIST_SERVER_URL` and `PRIVACYFENCE_GRIST_PROVISIONER_URL`. The provisioning keys of
  [ADR 0241](0241-privacyfence-provisions-peek-accounts-with-a-scoped-key-and-only-calls-peek.md) and
  [ADR 0244](0244-grist-api-keys-come-from-a-grist-side-provisioner.md) also accept a `_FILE` form.
- **The secret never appears** in a log line, an audit entry, the Admin page or an error message, and
  `IdpConfig.client_secret` is excluded from its repr.
- **Signing.** An environment-only server has no signature. Its trust root is whoever controls the
  container's environment, which is the party that chooses the image. An upgraded server's legacy
  bundle must still be signed.
- `deploy/docker/privacyfence.env.example` and `compose.yaml` show the variables and a Docker-secret
  example; the entrypoint says whether it uses the IdP from the environment or starts single-user.

## Alternatives considered

- **Keeping a signed bundle mandatory for organization mode.** Rejected: it is the thing that made the
  bootstrap take a signing key before the first start.
- **Reading every bundle key from the environment.** Rejected: only the IdP and the server need to
  exist before the first sign-in. App credentials are signed connector configs, and operational
  settings are the admin config.
- **Mixing bundle and environment fields within the idp section.** Rejected: a partly overridden
  provider is harder to reason about than one source.

## Consequences

- An environment-only server's identity provider is as trustworthy as its environment. An operator who
  wants a signed configuration keeps an upgraded server's bundle until 8.x.
- A multi-tenant issuer such as `accounts.google.com` accepts any Google account, so the docs pair it
  with allowed domains or an Internal consent screen. One Google OAuth client can also serve Peek and
  Grist sign-in; the docs recommend a separate client per service so each secret rotates on its own.

## Verification

`tests/unit/test_org_mode.py`, `tests/unit/test_org_identity.py`,
`tests/integration/test_org_docker_deploy_files.py`. Code: `src/privacyfence/org_mode.py`
(`idp_section`, `is_single_user`), `src/privacyfence/org_identity.py` (`IdpConfig`),
`src/privacyfence/daemon_main.py`, `deploy/docker/`.

## Related

ADR 0011, 0016, 0241, 0244, 0255, 0260, 0265, 0269.
