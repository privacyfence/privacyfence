# ADR 0056: Code carries no project history; a blocking test enforces it

## Status

Accepted — 2026-09-25. Implemented for
[#715](https://github.com/privacyfence/privacyfence/issues/715), enforced by
`tests/unit/test_code_no_history.py`.

## Context

The published and contributor docs were already held to "no history" by
`tests/unit/test_docs_no_history.py` (`ffdf9902`): history lives in `CHANGELOG.md` and the *why*
lives in `docs/adr/`. The code had no such rule. A sweep of `main` at `456a4c1b` (after
[#731](https://github.com/privacyfence/privacyfence/pull/731) had cleaned the files #715 listed)
still found about 1,300 tags in about 215 files: phase names of plans that had been deleted, phase
IDs, review finding IDs, letter-and-number items of deleted plans, bare issue and PR numbers,
section numbers into documents that no longer exist, and "as of version N" phrasing.

Each tag was a pointer the reader had to resolve somewhere else, and most pointed at nothing a new
reader could open: the plans were deleted by design (see `CLAUDE.md`'s "Decisions, plans and
ADRs"), the reviews were never checked in, and a bare issue number does not say whether the issue
is still open. A few leaked into user-visible strings: log lines, refusal messages, a Windows
service description, `--help` text. Where the tag stood in for a reason, the reason was not in the
code at all.

## Decision

1. **Code, tests, scripts, workflows, installers and packaging carry no project history.** A
   comment, docstring or user-visible string says why in its own words. When the why is a decision,
   it names the ADR (`ADR 0003`, `ADR 0003 decision 6`, `ADR 0002 §5a`). When it points at work
   that is still open, it gives the issue's full URL and describes the limitation next to it. Closed
   issues, merged PRs, deleted plans and review findings are not cited from code.
2. **`tests/unit/test_code_no_history.py` enforces it as a blocking unit test** over every tracked
   file except `docs/` (which has its own guard), `CHANGELOG.md`, `tests/fixtures/`, binary files,
   lockfiles and the two guards themselves. Its `_PATTERNS` define a tag by the shapes these tags
   actually took. A match is exempt when it cites something that still exists (a named ADR, an RFC,
   the Debian policy, a named Markdown document in the tree), and anything else that only looks
   like a tag (a CSS colour, a spreadsheet cell, Cloudflare D1, an ISO week in an audit log's file
   name) is either excluded by a pattern rule or listed in `_ALLOWED` with its reason.
3. **Decisions a tag used to stand for were written down.** Where a removed tag carried a decision
   that meets `docs/adr/README.md`'s bar and had no ADR, the same change recorded it (ADRs
   0057–0076) and the comments now cite it.

## Alternatives considered

- **Keep the IDs and rely on the tracker.** A tag is short and, in principle, resolvable: open the
  issue, find the plan in git history. Rejected: most tags resolved to a deleted plan or a review
  that was never in the repo, so the "lookup" was `git log -S` archaeology; a reader cannot tell a
  phase name from a feature name or a closed issue from an open one; and a tag stands in for the
  reason instead of stating it, so the reason is lost when the plan is. The tracker keeps the
  history either way. The code only needs to keep the why.
- **Allow any issue number, open or closed.** Rejected for the same reason: a closed issue is
  history. An open issue is cited by full URL so that its state is one click away, and the
  limitation is stated in words so the comment still reads correctly once it closes.
- **A non-blocking lint, or a review checklist item.** The docs guard showed that the narrow,
  blocking form works and stays cheap: a false positive gets an `_ALLOWED` line with its reason,
  not a looser pattern. Anything softer would let the tags return one PR at a time.

## Consequences

- A comment that loses its tag must gain its reason, which costs words; that is the point.
- The patterns cannot catch every shape (for example a plan's section named in words). Review still
  applies; the guard catches the shapes that recur.
- Data that looks like a tag needs an `_ALLOWED` entry with a reason, or a pattern rule when the
  shape is general (the ISO week rule).
- Accepted ADRs keep their history: `docs/adr/` is outside both guards by design.

## Verification

- `tests/unit/test_code_no_history.py`: `test_code_carries_no_history` over the tree, and the two
  self-tests of the patterns and the citation exemptions.
- `docs/coding-and-testing-guidelines.md` §1.2 states the rule for contributors.

## Related

- [#715](https://github.com/privacyfence/privacyfence/issues/715) — the issue.
- [#731](https://github.com/privacyfence/privacyfence/pull/731) — the first pass over the files the
  issue listed.
- `tests/unit/test_docs_no_history.py` (`ffdf9902`) — the same rule for the docs.
- [ADR README](README.md) — where history and decisions live instead.
