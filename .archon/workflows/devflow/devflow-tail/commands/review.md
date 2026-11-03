Run facts are in `$ARTIFACTS_DIR/context.json`: `main_branch` (written `<main>` below), `devflow_root`, `branch`
(`<branch>`), `slug`, `title`. The resolved profile is `$ARTIFACTS_DIR/profile.json`. The work order is
`$ARTIFACTS_DIR/plan.md` when it exists (with phases and a manifest), otherwise `$ARTIFACTS_DIR/work.md`
(an outcome and acceptance, no phases); `<work order>` below means whichever exists. When the work order
says the work is confidential security work, nothing public (PR title and body, comments, commit
messages) describes the vulnerability: name the class of fix only, never an exploit step.

You are the **final reviewer** (Opus) for a plan that Sonnet workers implemented. You change no
code, commit nothing and push nothing. Do not rely on any worker's account of what it did; read the
code.

Read:

- The work order: <work order> (a plan: Design, each phase's `acceptance`, `final_checks`; or a work.md: its Outcome and Acceptance, with no phases).
- The resolved profile `$ARTIFACTS_DIR/profile.json`, every doc in its `docs.must_read`, and its
  `project_steward` file.
- The `review-checklist` and `secure-code-review` skills (`devflow:` prefixed in your skill list,
  or `<devflow_root>/skills/<name>/SKILL.md`). Apply both.
- The change, against the main branch `<main>` (call it `<main>`):
  `git fetch origin <main>`, then `git diff origin/<main>...HEAD`, and `git log` for the commits.

Earlier rounds (empty on the first review): the last round's result was
$LOOP_PREV.review.output. If it is not empty, `$ARTIFACTS_DIR/review.md` is that round's report and
`$ARTIFACTS_DIR/review-responses.md` (if present) holds the fixer's answers to findings it
disputed. First confirm each earlier blocking finding is fixed, or validly answered, citing the
commit; then review the diff since the head it names, and finally the whole change once more for
seams.

When the diff changes documentation or the website, also load the `docs-review` skill and apply it to those files. Check what `/implement`'s final review checks: behaviour that differs from the Design, a design
decision a worker made that the plan did not, seams between phases, a missing test for an
acceptance item, a weakened or skipped test, a security or privacy regression, docs that describe
something other than the code, every `final_checks` item. Also check, against the actual diff:

- every `verify.dod_manual` row in the profile (each is `ok`, `needs attention` with `path:line`,
  or `n/a`);
- every `verify.dod_conditional` row whose `when` globs match a changed file: say whether its `do`
  is satisfied, or what is owed. A row that needs a dispatch or a credential is owed in the PR,
  not a finding against the code.

Run the profile's `verify.fast` commands yourself (prefix them with
`export PATH="$PWD/.venv/bin:$PATH"`).

Severity follows the `review-checklist` skill: **blocking** is anything you would refuse to merge;
style, naming, small simplifications and follow-ups are **non-blocking**. Evidence bar: every
finding cites `path:line` and says what is wrong and what the fix is. Drop anything that rests on
"might".

Write the report to `$ARTIFACTS_DIR/review.md` (replace the previous one; first copy it to
`$ARTIFACTS_DIR/review-<short head of the previous round>.md` if it exists): the verdict line
`REVIEW-REPORT verdict=<approve|changes>`, then the findings as
`<n>. blocking|non-blocking · path:line · phase id · what is wrong · the fix`, then the
`dod_manual` and `dod_conditional` tables, then the status of each earlier finding.

Return `verdict` (`changes` if and only if `blocking` > 0), the counts `blocking` and
`non_blocking`, and `head` (`git rev-parse HEAD`).
