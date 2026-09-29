# ADR 0109: Gemini Enterprise is registered through the existing `/register` endpoint; no pre-registration command

## Status

Accepted — 2026-09-29. Documented in `docs/connect-gemini-enterprise.md`; nothing to implement.
Part of [issue 393](https://github.com/privacyfence/privacyfence/issues/393).

## Context

Gemini Enterprise's custom MCP server data store supports neither dynamic client registration nor
OAuth discovery. An administrator types a client ID and secret into the Google Cloud console, and
Google's redirect URI is fixed at `https://vertexaisearch.cloud.google.com/oauth-redirect`
([M3.1 findings](https://github.com/privacyfence/privacyfence/issues/393#issuecomment-5887866056)).

Issue 393 was opened on the assumption that this blocks Gemini Enterprise: the only way a client
lands in `org/oauth_clients.json` is `OrgOAuthProvider.register_client`, called from the SDK's
`/register` route, so it proposed an admin command that writes a client into that file directly,
exempt from the stale-client prune.

The maintainer's check on 2026-09-29 registered the client by calling that same `/register` with
`curl`, with `client_name` `Gemini Enterprise`, Google's redirect URI and the
`authorization_code` and `refresh_token` grants, and used the returned ID and secret in the data
store. Gemini Enterprise signed in, loaded the tools, and ran a read and an approved write
([M4 result](https://github.com/privacyfence/privacyfence/issues/393#issuecomment-5889486339)).
The two problems found on the way were server bugs, not registration gaps, and are fixed by
[PR 810](https://github.com/privacyfence/privacyfence/pull/810)
([ADR 0108](0108-oauth-sign-in-popups-keep-their-opener-and-basic-clients-need-not-repeat-their-id.md)).

## Decision

**An administrator registers Gemini Enterprise by calling the deployment's existing `POST
/register`** with the metadata `docs/connect-gemini-enterprise.md` prescribes, including the exact
`client_name` `Gemini Enterprise`. PrivacyFence gets no separate command, code path or storage
flag for clients registered by hand: such a client is an ordinary DCR client in every respect,
including the 180-day stale-client prune.

## Alternatives considered

- **An admin command that writes a pre-registered client into `org/oauth_clients.json`**
  (`--register-oauth-client`, with a `source` field that exempts such clients from the prune).
  Rejected:
  - `/register` already produces exactly the client Gemini Enterprise needs: a client ID and
    secret, Google's redirect URI, the grants, and the token endpoint authentication method the
    administrator asks for. A second path to the same record would be more code, tests and
    documentation for no new capability.
  - The prune is not a threat to a client that is used. It removes only a client nobody has used
    for 180 days, and every `/authorize`, `/token` and `/revoke` call that names the client
    counts as use, so a connector in use refreshes it with every token refresh.
  - Attribution does not need it. The administrator prescribes the `client_name` in the
    `/register` call, so the name every Gemini Enterprise call is attributed to is deterministic,
    just as ChatGPT and claude.ai are matched by the names they register with
    ([ADR 0103](0103-chatgpt-is-matched-by-its-registered-name.md)).

## Consequences

- Setting up Gemini Enterprise needs no shell access to the server, no daemon restart and no
  PrivacyFence release beyond the one that carries ADR 0108's fixes; `/register` is reachable
  wherever the data store's URLs are.
- **Accepted risk:** a Gemini Enterprise connector nobody uses for 180 days is pruned the next time
  any client registers. Unlike a client that registers itself, it cannot recover on its own: an
  administrator registers it again and updates the data store's client ID and secret. A pin on
  that registration stops applying and stays listed as stale.
- Anyone who can reach `/register` can register a client called `Gemini Enterprise`, exactly as
  for every other name. The name stays a claim until an administrator pins the registration on
  Settings → AI systems ([ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)).
- If another client turns up that cannot use `/register` at all, this decision does not cover it,
  and a pre-registration path is reconsidered then.

## Verification

- `docs/connect-gemini-enterprise.md`, step 3, prescribes the `/register` call and its
  `client_name`.
- The stale-client prune and its use-based `last_used_at` are
  `OrgOAuthProvider._prune_stale_clients_locked` and `OrgOAuthProvider.get_client` in
  `src/privacyfence/web/oauth_provider.py`.

## Related

- [Issue 393](https://github.com/privacyfence/privacyfence/issues/393), and its comments on the
  [working setup order](https://github.com/privacyfence/privacyfence/issues/393#issuecomment-5889243926)
  and the [100-action limit](https://github.com/privacyfence/privacyfence/issues/393#issuecomment-5889315044).
- [PR 810](https://github.com/privacyfence/privacyfence/pull/810),
  [ADR 0108](0108-oauth-sign-in-popups-keep-their-opener-and-basic-clients-need-not-repeat-their-id.md).
- [ADR 0107](0107-gemini-cli-and-antigravity-are-not-supported-clients.md): Gemini support means
  Gemini Enterprise.
