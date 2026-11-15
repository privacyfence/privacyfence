# Release testing

The one home for everything a human checks before a release: the gates in the order they run,
then the manual checks. What CI proves automatically, and where, is in
[`testing-policy.md`](testing-policy.md). How a tag is cut is in [`releasing.md`](releasing.md)
and `/cut-release` (`.claude/commands/cut-release.md`).

## What stays manual

A check stays manual only when automation cannot reliably decide pass or fail. That leaves:

- visual and subjective judgment — contrast, spacing, light vs. dark, phone width;
- a first-time OAuth/consent screen, which the provider renders and which differs per account;
- one real client per supported AI-client family, run only when the client-facing surface
  changed ([`ai-client-qa.md`](ai-client-qa.md#when-to-run-it));
- real-device presentation — a phone's browser and an installed Home Screen app;
- review of provider drift the live check detected (the drift PR, see
  [`testing-policy.md`](testing-policy.md#layer-5-live-connector)).

Nothing is added to this list unless it meets that bar, and nothing on it is repeated across every
connector combination. Do not rerun the automated suite by hand because a release is being cut.

## Gates, in order

1. **CI green on the commit to tag.** `origin/main`'s tip
   with every required check passing. For a release that touched connectors, the latest
   `connector-live-check.yml` run is green and no `chore/connector-live-fixture-drift` PR is open
   unreviewed.
2. **`scripts/pre_release_check.py`**, locally from the repo root. It runs CI's blocking
   commands — `pytest` with branch coverage, `check_coverage_floor.py`, `ruff check .`,
   `mypy src/privacyfence`, `mypy_strict_modules.py`, `bandit`, and the `sync/` tests, `mypy` and
   `bandit` — reports PASS/FAIL for each, and exits non-zero on any failure. It checks no packaged artifact.
3. **`build.yml` pre-flight.** Dispatch `build.yml` (no inputs) against the exact commit to tag and
   confirm `build-org-image (amd64)`, `build-org-image (arm64)` and `sbom` succeed. This is the only run of the
   packaged-artifact tests before a tag; every publish step is gated on a tag ref, so it publishes
   nothing ([ADR 0030](adr/0030-preflight-dispatches-build-yml-before-tagging.md)). A failure is
   fixed on `main` and the pre-flight re-run.
4. **Human checks** below, against this run's image artifacts (`org-image-amd64`, `org-image-arm64`)
   — builds of the commit that will be tagged, carrying a dev version.
5. **`tld-release.yml` dry run.** Dispatch it on `main` with `version` and `dry_run: true` (the
   default) and approve the `tld-release` environment. It checks that every check on the commit is
   green, runs `scripts/release_check.py` (the channel, the stable release notes, `tag_release.py`'s
   checks without creating a tag) and shows the notes. It builds and tags nothing.
6. **Cut.** Re-dispatch with `dry_run: false` and approve again. Then confirm that `build.yml` and
   `publish-pypi.yml` started for the tagged commit; the workflow does not check this itself.

## Human checks

Run against the image the pre-flight run built, with a fresh data volume, never against a volume whose data you need.

### Every release

1. **Fresh install**, every stable release: follow [`getting-started.md`](getting-started.md)
   verbatim with the image, through "Finish setup" and "Check the install is working". Any step
   where the guide and the product disagree is a release blocker for the guide or the product.
2. **UI**, when approval, settings, notification or web-shell code changed: the approval list and
   cards, settings pages, notifications, and responsive layout in light and dark.
3. **First-time consent**, when authentication setup changed or a provider's consent behavior is in
   question: a real OAuth flow for the affected connector, from a new account.
4. **Connector QA**, for a new connector, a major connector rewrite, a broad change to `gate.py`,
   `auto_accept.py`, `policy/resource_registry.py` or the approval UI, or an unexplained provider
   regression: [`connector-qa.md`](connector-qa.md).
5. **AI clients**, when `/mcp`, the organization-mode OAuth server, `web/routes_mcp.py`,
   `web/mcp_tools.py` or `agent_identity.py` changed: every client in
   [`ai-client-qa.md`](ai-client-qa.md)'s results table, through its per-client script, under both
   annotation modes, ending with a saved fixture.

### Org mode on a phone

When org-mode pages, the design files (`src/privacyfence/resources/design/`), the approval card,
`pdf_render.py`, `web_push.py` or `resources/sw.js` changed, and for every stable release that
ships org mode. The layout itself is automated (`TestPhoneLayout`, see
[`testing-policy.md`](testing-policy.md#the-shared-design-system)); these are what a phone emulation
cannot prove.

1. **Real devices**: on a real iPhone, in Safari and again in the app installed to the Home Screen,
   and on a real Android phone in Chrome:
   - sign in through the IdP;
   - approve a write with passkey step-up;
   - approve a write with the IdP step-up fallback (`web/routes_org_stepup.py`);
   - read a PDF review card: the page images show, with the "Showing pages 1–N of M" note;
   - open a staged download link from inside the Claude app: it survives the sign-in bounce and
     the file lands in Files/Downloads;
   - receive a push notification for a new approval, and tap it through to `/approvals`
     ([ADR 0081](adr/0081-org-mode-sends-a-count-only-web-push.md)).
2. **Look**: light and dark, at phone and desktop width, in `scripts/render_ui_review.py --out DIR`'s
   output and in `scripts/qa_web_smoke.py`'s report. The app should read as the website does
   ([ADR 0079](adr/0079-the-apps-visual-language-is-the-websites.md)), and on every approval card
   the same decision must still look like the default.

## The artifact under test

Test the image that will ship — the pre-flight run's, from the commit being tagged — never a
source checkout standing in for it. [`org-mode-docker.md`](org-mode-docker.md) describes how it
starts.

## Evidence

Record in the release PR or discussion, not in this document:

1. the commit, the `build.yml` pre-flight run and the `tld-release.yml` dry-run URL;
2. which human checks applied to this release's changes, and their results;
3. any provider drift, image, sign-in or startup issue seen.
