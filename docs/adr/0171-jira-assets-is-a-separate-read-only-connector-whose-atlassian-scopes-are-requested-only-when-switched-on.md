# ADR 0171: Jira Assets is a separate read-only connector whose Atlassian scopes are requested only when it is switched on

## Status

Accepted — 2026-10-13. Implemented: `src/privacyfence/atlassian_oauth.py`,
`src/privacyfence/assets_client.py`, `src/privacyfence/connectors/assets.py`,
`scripts/build_org_bundle.py`.

## Context

Jira Service Management Assets (the CMDB formerly called Insight) holds structured objects linked
to each other and to Jira tickets. The Atlassian sign-in is shared by Jira and Confluence, and
Assets needs five more permissions (`read:cmdb-schema:jira`, `read:cmdb-type:jira`,
`read:cmdb-attribute:jira`, `read:cmdb-object:jira`, and `read:servicedesk-request` for the
workspace id). Those permissions must exist on the Atlassian app, and asking for one the app
lacks makes the whole consent screen fail.

## Decision

- **`assets` is its own connector** (display name Jira Assets), read-only, reusing the Atlassian
  token. It defaults to off.
- **The extra scopes are requested only when Assets is on.** `atlassian_oauth.scopes_for(assets=...)`
  adds `ASSETS_SCOPES` to `DEFAULT_SCOPES`. `assets_enabled` lets an explicit
  `connectors.assets.enabled` win, and otherwise takes the org bundle's `atlassian.assets` flag
  (`build_org_bundle.py --atlassian-assets`) as the default. Settings, the CLI and org-mode
  `/connect` all sign in through it.
- **Granted scopes are recorded.** `resolve_resource_and_save` stores the union of the scopes of
  the accessible resources matching the chosen site in the token record, and both token refreshes
  keep the key.
- **A token without `read:cmdb-object:jira` asks to authenticate again.** `AssetsClient` raises an
  error that contains "Use Authenticate…", which the daemon classifies as `not_authenticated`, so
  Settings offers the button.

## Alternatives considered

- **Adding the scopes to `DEFAULT_SCOPES`** — every Jira and Confluence user would have to
  reconnect, and an organisation whose Atlassian app lacks the `read:cmdb-*` scopes would see every
  Atlassian sign-in fail at the consent screen.
- **A separate API-token credential** — a second Atlassian secret on disk, with a different setup
  path from Jira and Confluence.
- **Inferring the scopes from a 401** — Atlassian's 401 for a missing scope looks the same as one
  for an expired token, and the existing clients treat 401 as "refresh".

## Consequences

- Users who never switch Assets on see no change; switching it on costs one extra consent.
- Tokens made before this change carry no recorded scopes, so they are treated as lacking Assets
  until the user re-authenticates.
- An organisation must add the five scopes to its Atlassian app before setting
  `--atlassian-assets`.

## Verification

`tests/unit/test_assets_client.py` (`TestConstructor`, `TestWorkspace`, `TestRefresh`),
`tests/unit/test_atlassian_oauth.py`, `tests/unit/test_daemon_main.py`,
`tests/unit/test_settings_controller.py`.

## Related

ADRs [0115](0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md),
[0116](0116-list-tools-page-inside-the-client-and-keep-their-return-shape.md),
[0134](0134-extra-jira-fields-go-through-a-reviewed-search.md),
[0135](0135-the-jira-field-list-is-auto-approved.md),
[0157](0157-a-rule-can-let-personal-data-through-tools-whose-result-holds-no-row.md),
[0159](0159-jira-export-issues-is-covered-by-the-project-search-rule.md).
