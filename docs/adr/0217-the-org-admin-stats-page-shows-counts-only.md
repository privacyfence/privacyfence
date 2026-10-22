# ADR 0217: The organization admin's Stats page shows counts only

## Status

Accepted — 2026-10-22. Implemented.
Relates to ADR 0032, ADR 0033 and ADR 0035.

## Context

An organization admin had no way to see how the server is used: how many people have signed in,
which connectors they use, and whether they rely on auto-accept rules. Each principal's connector
credentials and rules live under its own `users/<id>/` directory, and until now no page read
across principals: every Settings section showed the signed-in principal's own data, except the
install-wide privacy policy and the AI-system pins, which belong to no single user.

## Decision

- **A Stats section in org-mode Settings, for admins only.** `settings_window_html._capabilities_for`
  shows it when `mode` is org and `is_admin` is set, like AI systems. Local mode never shows it:
  it has one user.
- **Counts only.** `web/org_stats.py` returns the number of principals with a `users/<id>/`
  directory, how many hold any connector credential and any auto-accept rule, users per connector,
  and rules (and the users keeping them) per connector. No principal id, address, rule value or
  rule sentence leaves the module. A rule's value (a sender, a folder id) is the user's own data,
  and the admin's IdP already answers who has an account.
- **Read from disk, not from live connectors.** Connected means a credential file exists, the same
  check `/connect` makes; rules are the rows of each principal's `settings.yaml`. Building the page
  never builds a principal's connectors, calls a provider, or touches the per-principal registries.
- **Computed per request.** The state is built fresh for each admin page load, like the rest of
  org-mode Settings; nothing is cached or stored.

## Alternatives considered

- **A per-user table.** It would show the admin which colleague connected what and which rules
  they keep. That moves a trust boundary this page does not need to move, and principal ids are
  often hashed IdP subjects that mean nothing on their own.
- **Counting from the live `ConnectorRegistry`.** It only holds principals active recently, so the
  numbers would change with idle eviction, and asking it for everyone would build every user's
  connectors.
- **A metrics endpoint.** Useful for monitoring, but a separate decision about authentication and
  scraping; the page answers the admin's question without it.

## Consequences

- "Connected" counts a credential that exists, not one that still works: a revoked token counts
  until the user reconnects or disconnects.
- The page reads one settings file and a few credential paths per principal on every admin load.
  That is fine for the user counts org mode targets; a much larger install would want a cache.
- A user who signed in once and never came back still counts, because their directory remains.
