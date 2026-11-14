# ADR 0278: A scoped token can be delivered into an agent runtime, n8n first, as a credential PrivacyFence writes itself

## Status

Accepted — 2026-11-13. Implemented.
Amends [ADR 0165](0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md) and [ADR 0250](0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md).
Amended by [0284](0284-the-delivered-credential-type-is-an-administrators-choice.md) and [0285](0285-the-privacyfenceapi-credential-holds-the-address-and-the-token.md).

## Context

Claude builds n8n workflows through the n8n MCP and wants those workflows to call PrivacyFence's `/mcp`.
The MCP Client node needs a bearer credential, and Claude may not write one: no n8n MCP tool creates a
credential or reads a secret (https://docs.n8n.io/connect/connect-to-n8n-mcp-server/mcp-server-tools-reference/,
read 2026-11-13). A scoped token ([ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md))
is already the right identity for an unattended workflow, and an AI client can already propose one
([ADR 0250](0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md)), but the person
then had to copy the token into n8n by hand. Tracking issue: https://github.com/privacyfence/privacyfence/issues/1090.

## Decision

- A delivered credential **is a scoped token**. No new principal object is added. The token is deny-only,
  expires, is revoked alone and is named in every audit entry as `scoped-token:<id>`. A call that no token
  rule covers is denied at once (ADR 0164), so an n8n run fails fast instead of waiting for an approval.
- Delivery is an adapter. `src/privacyfence/agent_runtimes.py` holds what is runtime-neutral (the `deliver_to`
  request, the target, the config) and `src/privacyfence/n8n_api.py` is the only n8n-specific code, so a later
  runtime reuses the proposal, approval, expiry, revocation, records and sweeper and adds only a client.
- After the person confirms a proposal that carries `deliver_to`, PrivacyFence creates an n8n `httpBearerAuth`
  credential holding the token, with an id it chooses, and the browser never receives the secret.
- The workflow id the client reports in `deliver_to.workflow_id` is advisory. It is shown on the review page and
  written to the record and the audit log, and never enforced: any workflow that can reach the credential can use it.
- **The secret is readable through n8n.** An `httpBearerAuth` credential can be attached to any node of any
  workflow owned by the n8n user that holds it, including an HTTP Request node pointed at an echo endpoint, so the
  proposing client can read the token through n8n's execution data. This amends ADR 0250's "the client never
  receives the token" for delivered proposals, and ADR 0165's "never stored": n8n stores it, encrypted under its
  own key. The review page, the confirmation dialog, the tool texts and the docs say so, and narrow rules and a
  short lifetime are the protection.
- Delivery uses the same gates as any token creation: signed-in session, CSRF, origin, typed name, and step-up where
  the install requires it.

Facts the n8n client relies on (research, read 2026-11-13):

- `POST /api/v1/credentials` (scope `credential:create`) takes `name`, `type`, `data`, an optional `id` of 1 to 16
  characters and an optional `projectId` (from n8n 2.40.0), and returns no secrets
  (https://cdn.jsdelivr.net/npm/n8n@2.47.7/dist/public-api/v1/openapi.yml).
- `httpBearerAuth` has one data field, `token`
  (https://cdn.jsdelivr.net/npm/n8n-nodes-base@latest/dist/credentials/HttpBearerAuth.credentials.js).
- `DELETE /api/v1/credentials/{id}` needs `credential:delete` and the caller must own the credential
  (the same openapi.yml; https://docs.n8n.io/connect/n8n-api/credential).
- API-key scopes are Enterprise-only; other editions give a key full access (https://docs.n8n.io/api/authentication/).

## Alternatives considered

- **A Wait node that blocks on `privacyfence_await_approval`.** Scoped tokens are deny-only already, and runs that
  wait for days are an operational mess.
- **A new principal object for the runtime.** It would duplicate the token's expiry, revocation, audit identity and
  rules, which is the reasoning of [ADR 0220](0220-a-routines-rule-profile-is-a-scoped-token.md).
- **Claude finding the credential by name with n8n's `list_credentials`.** It couples Claude to the n8n MCP and to
  a naming rule; a status tool was chosen ([ADR 0280](0280-privacyfence-runtime-credential-status-reports-a-delivered-credential-to-its-proposer.md)).
- **Forcing a passkey for a delivery confirmation.** It is the same gate as showing a new token's secret once in
  organization mode; the risk delivery adds is n8n's reach, which the review text states.
- **`trust_env=False` on the n8n session.** It would break organization proxies; the Grist and Peek clients keep
  `requests`' defaults too. An internal CA is trusted through `REQUESTS_CA_BUNDLE`.
- **Restricting the credential to the PrivacyFence host with n8n's credential domain allow-list.** Not researched;
  an open follow-up that would narrow the read-through exposure.

## Consequences

- A delivered token is as exposed as n8n: anyone who can edit workflows there, the proposing AI client included, can
  use and read it until it expires or is revoked. This is accepted and stated on the review page.
- Data a workflow reads through PrivacyFence is kept in n8n's execution history.
- n8n may ignore a caller-chosen credential id on some versions. The client then refuses the answer, deletes the
  returned id and revokes the token, so delivery fails closed.
- The API key's user must be the n8n MCP's user, or the credential is not usable in Claude's workflows.
- The minimum n8n version is 2.40.0. It is documented, not checked.

## Verification

- `tests/unit/test_n8n_api.py`: `TestCreate`, `TestDelete`, `TestCheck`, `TestErrors`, `TestNoSecretLeaks`.
- `tests/unit/test_runtime_credentials.py`: `TestDeliverHappyPath`, `TestDeliverFailures`, `TestCrashPoints`.
- `tests/unit/web/test_routes_org_settings.py`: `TestDeliveryFromProposal` (the answer carries no secret).

## Related

- Issue: [issue 1090](https://github.com/privacyfence/privacyfence/issues/1090); sources `src/privacyfence/agent_runtimes.py`, `src/privacyfence/n8n_api.py`, `src/privacyfence/runtime_credentials.py`.
- [ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
  [ADR 0165](0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md),
  [ADR 0220](0220-a-routines-rule-profile-is-a-scoped-token.md),
  [ADR 0250](0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md),
  ADRs 0279 to 0282.
