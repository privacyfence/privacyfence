# ADR 0099: Unverified AI-client setup instructions never merge to `main`

## Status

Accepted (recorded retroactively on 2026-09-28; decided by the maintainer on 2026-09-26, before
[PR 764](https://github.com/privacyfence/privacyfence/pull/764) and
[PR 766](https://github.com/privacyfence/privacyfence/pull/766)).

## Context

Every published doc on `main` ships in the next stable release's `/docs/` on privacyfence.eu
([ADR 0052](0052-docs-are-built-with-zensical-from-the-latest-stable-tag.md)). There is no
"draft" state between merging a doc and publishing it, and a release is cut from `main`'s tip for
reasons unrelated to any one doc.

Instructions for connecting an AI client depend on the client's own UI, its registration
behaviour and its confirmation prompts, none of which PrivacyFence controls or can test from its
own code alone. Several assumptions about the Claude clients held up only until the real clients
were run: the names they send
([PR 781](https://github.com/privacyfence/privacyfence/pull/781)), and how claude.ai handles a
large tool result ([PR 779](https://github.com/privacyfence/privacyfence/pull/779)). Running the
real client is what `docs/ai-client-qa.md`'s manual check
([PR 764](https://github.com/privacyfence/privacyfence/pull/764)) is for.

The Claude clients' pages ([PR 766](https://github.com/privacyfence/privacyfence/pull/766))
consolidated instructions that had already shipped in earlier releases, with the claims not yet
observed marked as such; the question this ADR answers arises for the next client, whose
instructions exist nowhere yet.

## Decision

**A setup doc for a new AI client merges to `main` only after that client's manual check in
`docs/ai-client-qa.md` has passed against the instructions as written.** Until then the draft doc
waits in a draft pull request. An existing client's page may be corrected on `main` as usual; a
claim that has not been observed yet is either left out or stated as unverified in the page
itself.

## Alternatives considered

- **Merge the doc with an "unverified" or "beta" banner.** Rejected: the banner ships to users
  just the same, and readers follow steps regardless of a banner. It also has to be removed by a
  later PR that nobody is forced to write.
- **Merge the doc but keep it out of `docs/README.md`'s published list until verified.**
  Rejected: guardrail 5 (`test_website_docs_allowlist.py`) requires every `docs/*.md` to be in
  exactly one half of that list, and a contributor-half entry would publish it on GitHub under the
  release tag anyway.
- **Verify after the release, fix in the next.** Rejected: users on the release would follow
  wrong instructions for a whole cycle, for a client the website may already name.

## Consequences

- A client's docs PR can stay open for as long as the manual check takes; plans that add a client
  schedule the check before the docs merge, not after.
- The website page follows the doc by one release
  ([ADR 0098](0098-an-ai-agents-website-page-lands-after-the-release-that-carries-its-doc.md)),
  so a client is never named on the site before its instructions were run.
- The rule covers new client pages. It does not stop an ordinary docs fix to an already verified
  page.

## Verification

- `docs/ai-client-qa.md`: the checklist and evidence format a client's docs PR cites.
- `docs/release-testing.md` → "Human checks": the pointer to that checklist.

## Related

- [ADR 0097](0097-each-ai-agent-has-its-own-setup-doc-and-website-page.md): the per-client docs
  this rule governs.
