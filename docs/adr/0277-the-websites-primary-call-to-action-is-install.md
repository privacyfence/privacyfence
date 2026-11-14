# ADR 0277: The website's primary call to action is Install

## Status

Accepted — 2026-11-01. Implemented: `scripts/build_site.py` (header defaults), `website/install/`.

## Context

From 7.0 the way most people start is a Docker install, single-user for one person or an organization
server, and the desktop installers are deprecated. The site's highlighted header item was
**Download**, which leads to the installers. A visitor who follows it lands on the deprecated path.

## Decision

The header's highlighted item is **Install**, pointing at `/install/` (`scripts/build_site.py` header
defaults `cta_href="/install/"`, `cta_label="Install"`). `/install/` is an interactive picker over
the Docker install modes: who uses PrivacyFence, the bundle (Peek and Grist or not), how people sign
in, which apps connect the services, and what sits in front. It gives the commands for the chosen
setup and does not mention desktop mode.

`/download/` stays for the deprecated desktop installers and keeps its own `All releases` header
override, but is off the header. The homepage's two primary buttons say "Install PrivacyFence" and
point at `/install/`.

## Alternatives considered

- **A static table instead of a picker** — the maintainer chose the picker.
- **Redirecting `/download/` to `/install/`** — release links, the Cloudflare Worker, `pyproject.toml`
  and the `.mcpb` text all point at `/download/`.
- **An Install link next to Download** — one highlighted item is the design.

## Consequences

- Hand-written links to the download page become Install links, except the ones about verifying or
  setting up a desktop install.
- The picker's page text must never name the desktop installers; a test enforces it.
- The desktop installers remain reachable from the download page, the releases page and the docs.

## Verification

- `tests/unit/test_build_site.py` (the default header renders `<a class="nav-cta" href="/install/">Install</a>`).
- `tests/unit/test_website_download_cta.py::test_homepage_install_ctas_point_at_the_install_page`
  and `::test_homepage_install_ctas_do_not_point_at_github`.
- `tests/unit/test_website_install_page.py` (the five radio groups, the eight results, no desktop
  installers inside `<main>`).
- `tests/integration/test_install_page.py` (the picker's behaviour in a browser).

## Related

[ADR 0275](0275-the-single-user-person-is-admin-at-privacyfence-localhost.md),
[ADR 0272](0272-a-single-user-install-provisions-its-one-person-as-verified.md).
