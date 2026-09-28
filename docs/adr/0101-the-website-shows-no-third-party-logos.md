# ADR 0101: The website shows no third-party logos; AI agents and connectors are named in text

## Status

Accepted (recorded retroactively on 2026-09-28; decided by the maintainer on 2026-09-26, before
[PR 785](https://github.com/privacyfence/privacyfence/pull/785)). Implemented: the AI agents pages
in [PR 785](https://github.com/privacyfence/privacyfence/pull/785); the connector pages already
followed it.

## Context

privacyfence.eu names the AI clients PrivacyFence is tested with (the "Works with" strip and the
`/ai-agents/` pages, [ADR 0098](0098-each-ai-agent-has-its-own-setup-doc-and-website-page.md))
and the services its connectors reach (`/connectors/`). Integration pages elsewhere usually show
each vendor's logo, and a newcomer building the next client's page would reasonably add one.

Those logos are trademarks. Each vendor publishes its own brand guidelines on how and where they
may be shown, and revises them on its own schedule. PrivacyFence has no agreement with any of
these vendors.

## Decision

**No page on privacyfence.eu shows a third-party logo. Every AI client and every connected service
is named in text, with its product name as the vendor writes it.** This covers the "Works with"
strip, `/ai-agents/`, `/connectors/` and every page generated from `clients.json`.

## Alternatives considered

- **Logos, following each vendor's brand guidelines.** Rejected: the guidelines differ per vendor
  and have to be re-checked for every client and connector, and again whenever they change. A
  vendor's logo next to "Works with" also reads as that vendor's endorsement, which PrivacyFence
  cannot claim.
- **Generic icons per client category.** Rejected: they add nothing the name does not already
  say.

## Consequences

- Adding a client or connector needs no image asset, licence check or brand review.
- The pages look plainer than a typical integrations grid.
- Reversing this for one vendor is possible only with that vendor's written permission, recorded
  in a new ADR.

## Verification

- `website/_data/clients.json` carries names only, no image field; `scripts/build_site.py`'s
  `render_clients()` renders each client as a text link.
- `website/ai-agents/` and `website/connectors/`: no `<img>` of a third-party mark.

## Related

- [ADR 0025](0025-no-certified-security-framework.md): the same rule for the site's claims, which
  say nothing PrivacyFence cannot back.
