# ADR 0202: Cached Atlassian accounts are reported each cycle, and erased when they cannot be

## Status

Accepted — 2026-10-19. Implemented: `src/privacyfence/atlassian_users.py`,
`src/privacyfence/jira_client.py`, `src/privacyfence/confluence_client.py`,
`src/privacyfence/daemon_main.py`. Amends [ADR 0117](0117-atlassian-account-ids-resolve-through-a-lazy-shared-cache.md):
cache entries are now erased, not only refetched.

## Context

The account-id → display-name cache ([ADR 0117](0117-atlassian-account-ids-resolve-through-a-lazy-shared-cache.md))
stores Atlassian personal data on disk, and kept every entry indefinitely: its 7-day TTL decides
when a name is fetched again, not when it is deleted. Atlassian requires an OAuth 2.0 (3LO) app
that stores personal data to report the account ids it holds to the personal data reporting API
(`POST https://api.atlassian.com/app/report-accounts/`) once per cycle (7 days by default, or the
`Cycle-Period` response header), in batches of at most 90, each with the time its data was
retrieved, and to erase or refresh an account Atlassian answers `closed` or `updated` for. The
developer console asks an administrator to confirm this before an app that declares personal data
can be shared.

Atlassian documents no scope for this endpoint for 3LO apps and recommends the token of the app's
owner. PrivacyFence has no central server and no owner token: each install, or each user of an
organization server, holds only its own token. An organization server builds a user's connectors
only while that user is active, so an absent user's cache would never be reported.

## Decision

- Each cache file is reported with the token of the user it belongs to. The Jira and Confluence
  clients start a report on a background thread after a successful connection check and after a
  lookup or `*_find_users`, at most once an hour (or after `Retry-After` on a 429). An account is
  reported once per cycle; the cycle Atlassian returns is followed, clamped to between an hour and
  30 days, and saved with the cache.
- An account Atlassian answers `closed` or `updated` for is erased from memory and disk, and not
  fetched again for an hour. `updated` is erased rather than refreshed in place; the next read that
  needs the name fetches it again.
- **Erasure is the fallback for everything that cannot be reported.** An entry neither retrieved
  nor reported within its cycle and one day is erased, by the directory itself and by a daemon
  sweep over `data_dir()` and every `data_dir()/users/*` at startup and every six hours. The sweep
  needs no token, so it also covers organization-server users who are not active, a report the
  endpoint refuses, and an install that has lost its token. An unreadable cache file is deleted,
  since it cannot be reported.
- No new OAuth scope is requested. The report is sent with the scopes the user already granted.
- The audit log is not reported or rewritten. It is the operator's tamper-evident record
  ([ADR 0071](0071-audit-log-integrity-is-a-keyed-hash-chain-plus-off-host-forwarding.md)), kept under the operator's own
  retention policy, and erasing names from it would break the chain that makes it evidence.

## Alternatives considered

- **Request `report:personal-data`.** Every existing user would have to reconnect, Atlassian's
  scope list for 3LO Jira apps does not include it, and Atlassian's issue ECO-533 reports that a 3LO
  token requested with it and other scopes comes back without it. If the endpoint refuses ordinary
  tokens, the erasure fallback still keeps the cache within one cycle of its last retrieval.
- **Stop caching names.** Atlassian's simplest option, but every read that shows a name, and every
  approval card naming a person, would cost a Jira user-API call per batch of ids.
- **Report only from a timer in the daemon.** It would need every organization user's token
  unsealed in the background ([ADR 0072](0072-org-mode-persists-only-sealed-refresh-tokens.md)), so
  the timer only erases, and reporting rides on activity, when the token is in use anyway.
- **Redact closed accounts in the audit log.** Rejected for the chain reason above; an operator who
  must erase there does it under their own retention process.

## Consequences

- A name is never on disk for longer than a cycle and a day after it was last retrieved or reported.
- A user who stops using PrivacyFence loses the cached names within about eight days; they are
  fetched again on next use.
- Whether the endpoint accepts an ordinary user token is not covered by the offline tests and needs
  a live check on a shared app. A refused report is logged as "Atlassian personal data report not
  sent" and retried hourly.
- The audit log still holds names and account ids; the setup guide says so.

## Verification

- `tests/unit/test_atlassian_users.py`: `TestReportAccounts`, `TestDirectoryReporting`,
  `TestReportSoon`, `TestOverdueErasure`, `TestEraseOverdueCacheFile`, `TestEraseOverdueCaches`.
- `TestPersonalDataReporting` in `tests/unit/test_jira_client.py` and
  `tests/unit/test_confluence_client.py`; `TestPersonalDataSweep` in
  `tests/unit/test_daemon_main.py`.

## Related

- [ADR 0117](0117-atlassian-account-ids-resolve-through-a-lazy-shared-cache.md): the cache this
  applies to.
- Atlassian's [user privacy developer guide](https://developer.atlassian.com/cloud/jira/platform/user-privacy-developer-guide/).
