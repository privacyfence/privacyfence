# Plan: remove project-history tags from code (issue #715)

Temporary plan document. It sits in a subdirectory of `docs/` so that the site build's
docs allowlist (`test_website_docs_allowlist.py`) never has to classify it. It exists so that the parallel sessions working on #715 share one contract,
and it is deleted by the last step of the same PR (see `CLAUDE.md`, "Decisions, plans and ADRs").
Nothing may link to it.

## Where things stand

PR #731 cleaned every file in #715's table (all workflow step names, `conftest.py`, `mcp_dispatch.py`,
`gate.py`, `approvals.py`, `policy/propose.py`, `debian/control`, the Worker). The issue was
reopened because the table was only a sample. A sweep of `main` at `456a4c1b` with the docs guard's
own patterns, extended to the shapes code uses, still finds **about 1,300 tags in about 215 files**,
excluding `docs/` and `CHANGELOG.md`:

| Shape | Examples | Approx. |
|---|---|---|
| Phase name | `#428 Phase 4`, `this plan's Phase 2`, `release-publishing plan Phase 4` | 450 |
| Phase ID | `P9`, `P6 of the policy v2 redesign`, `P7.2`, `As of P9` | 370 |
| Issue/PR number | `(#428 B10)`, `#406`, `privacyfence/privacyfence#374` | 480 |
| Finding ID | `SEC-23`, `SEC-09`, `TST-02` | 250 |
| Letter+number ID from a deleted plan | `B9`, `F5`, `D1`, `§10.6/§15 D7` | 300+ |
| Version-qualified history | `(#428 D1, 4.1)`, `As of P9` | some |

Most are in comments and docstrings. Some are not, and those need their tests updated in the same
change:

- **Runtime strings:** `build_org_bundle.py` `--help` text and section titles (`SEC-22`, `SEC-23`,
  `SEC-05`, `#406`); `verify_audit_log.py`'s description; `check_graphical_session_coverage.py`
  error messages (`privacyfence/privacyfence#374`); `note` lines in
  `scripts/{linux,macos}_privilege_separation.sh` (`#428 D1, 4.1`); a daemon/shim message that
  `mcpb/shim/test/daemon.test.ts` matches on (`#428 Phase 4`).
- **Names:** `tests/unit/test_p6_principal_isolation.py`,
  `test_the_f5_operations_stay_unproposed`, `test_separated_layout_problems_are_reported_under_sec_09`.
- **Data that only looks like a tag and must stay:** Slack file IDs such as `"F1"` in test data,
  CSS/HTML hex colours such as `#333`, bytes inside `.woff2` fonts.

Out of scope: `docs/adr/` (accepted ADRs are frozen and are history by design), `CHANGELOG.md`,
`tests/fixtures/`, lockfiles, binary files, git history, and `docs/code-history-tags/` itself
(this plan and its ledgers).

## Done means

1. No tag of the shapes above is left in any tracked file in scope, except entries in the guard's
   allowlist, each with a stated reason.
2. A new test, `tests/unit/test_code_no_history.py`, keeps it that way. It is the code counterpart of
   `test_docs_no_history.py`.
3. Every comment that lost a tag still says **why**, in its own words. Where the why is a decision,
   the comment names its ADR (`ADR 0003`). A decision that has no ADR and meets `CLAUDE.md`'s bar
   gets one in this PR.
4. Behaviour is unchanged, apart from the wording of user-visible strings, and those get one
   `CHANGELOG.md` line under `## [Unreleased]`.
5. The §2.7 definition of done passes, including the off-box rows (see step 7).
6. This plan document is deleted, and the PR description lists the decisions it made and the ADRs
   they went into.
7. **One PR**, from `claude/amazing-bohr-faqr6s` into `main`, closing #715.

## Rewrite rules (every session follows these)

1. **Delete the tag, keep the reason.** `# SEC-10: refuse before the connector sees the args`
   becomes `# Refuse before the connector sees the args, so ...`. If the tag was the whole comment,
   either write the reason or delete the comment. Never replace a tag with a vaguer tag like "a
   security review" or "the redesign".
2. **Decisions link ADRs.** If a tag pointed at a decision, find its ADR in `docs/adr/README.md`'s
   index and cite it as `ADR NNNN`. If there is none and the decision meets the bar (hard to reverse,
   a trust boundary, the release path, or a non-obvious rejected alternative), **do not write the
   ADR yourself.** Add an entry to your slice's ledger (below). ADR numbers are assigned in step 7 so
   that parallel sessions cannot collide.
3. **Open issues may stay, closed ones may not.** A pointer to work that is still open (a known
   limitation, a follow-up) may stay as a full URL, `https://github.com/privacyfence/privacyfence/issues/NNN`,
   with the limitation described in words next to it. Check the issue's state with the GitHub MCP
   before keeping it. Closed issues, merged PRs and deleted plans go.
4. **Current behaviour only.** "As of P9 this backs ...", "used to", "replaced the old ...",
   "(4.1)" describe history. Say what the code does now. If the code still handles an older on-disk
   state, say that in present tense: "an install whose layout predates privilege separation ...".
5. **User-visible strings** (help text, log lines, error messages, dialog text) get the same
   rewrite. Update every test that asserts on them. Record each changed string in the ledger for the
   CHANGELOG line.
6. **Renames:** rename a test file or test function only to drop a tag. Use `git mv` and check that
   the new name is free.
7. **No other changes.** No behaviour changes, no reformatting, and no comment edits beyond the
   tag's sentence and the sentences that depend on it. If you find a real bug, put it in the ledger;
   do not fix it here.
8. **`§` references name their document.** `ADR 0002 §5a`, `RFC 6749 §3.3` and
   `coding-and-testing-guidelines.md §2.7` are citations and pass the guard. A bare `§3` or `§10.6`
   points into a deleted plan or UX spec: describe the thing ("the dialog's disclosure rows") instead.
   The same applies to a plan item ID after an ADR: `ADR 0008 D3` passes, but a bare `D3` does not.
9. **Do not touch** `docs/`, `CHANGELOG.md`, `tests/fixtures/`, or any file outside your slice. If a
   file outside your slice has to change (for example, a test in another slice asserts on your
   string), record it in the ledger instead, and step 7 applies it.

## Steps

The work runs as **step 0**, then **steps 1–6 in parallel**, then **step 7**. Steps 1–6 each own a
disjoint set of files, so their branches merge without conflicts.

### Step 0: scaffolding (done, on `claude/amazing-bohr-faqr6s`)

- `tests/unit/test_code_no_history.py` is the guard. Its docstring, `_PATTERNS`, `_CITATION` and
  `_ALLOWED` are the exact definition of a tag, and the guard is blocking from now on. It skips every
  path named in `tests/unit/code_history_pending/step-N.txt`, and fails on a tag anywhere else. A
  step is done when it can delete its list.
- To list your slice's remaining tags, run:

  ```
  python -c "import sys; sys.path.insert(0, '.'); from tests.unit import test_code_no_history as t; \
  [print(h) for r in open('tests/unit/code_history_pending/step-N.txt').read().split() for h in t._file_hits(r)]"
  ```

- If you are sure a hit is not history (data, a product name, an external spec), add it to
  `_ALLOWED` with its reason, and mention it in your ledger. This is the one shared file a step may
  edit, and only by adding `_ALLOWED` lines. The orchestrator resolves the resulting trivial
  conflicts when it merges.
- Each step has its own ledger, `ledger-step-<N>.md` next to this plan.

### Steps 1–6: one slice each (child sessions, in parallel)

Each child session starts from `claude/amazing-bohr-faqr6s` and pushes to its own branch,
`claude/715-step-N-<slug>`. **Its pending list is its slice**; the table only says how the lists
were cut. Tests go with the code they test, so that a string change and its assertion land together.

| Step | Slug | Area | Files / tags |
|---|---|---|---|
| 1 | `install` | Install, privilege separation and packaging: `privilege_separation.py`, `windows_acl.py`, `daemon_main.py`, `paths.py` and the like; `scripts/*privilege_separation*`, `scripts/build_*`, `check_graphical_session_coverage.py`; `installer/`, `debian/`, `resources/`, the `.spec`/plist/unit files; `tests/platform/`, the packaged and graphical-session integration tests, and matching unit tests | 58 / 493 |
| 2 | `control` | Control channel, server, companion and shim: `companion.py`, `web_shell.py`, `web/{control_channel,server,state_stream,csp,routes_connect}.py`, `mcpb/`, and their tests | 23 / 326 |
| 3 | `approvals` | The approval window and list, cards, the deferred approval route: `approval*.py`, `card_builder.py`, `write_effects.py`, `dialog_window_html.py`, `auto_accept.py`, `web/routes_approvals.py`, `tests/system/`, and their tests | 18 / 216 |
| 4 | `org-policy` | Org mode, auth, audit and policy: `org_*.py`, `audit_*.py`, `gate.py`, `policy/`, `web/{oauth_provider,session_auth,org_*,routes_org_*,mcp_auth,sealed_refresh_store}.py`, `scripts/{build_org_bundle,verify_audit_log}.py`, `tests/unit/{policy,abuse}/`, `test_p6_principal_isolation.py`, `test_server_org_mode.py`, and their tests | 52 / 435 |
| 5 | `connectors` | Everything else: connectors and clients, file staging, text extraction, PII, `web/{routes_mcp,mcp_tools,routes_file_bridge,routes_downloads}.py`, remaining `scripts/`, `pyproject.toml`, `.gitignore`, `website/`, and their tests | 76 / 295 |
| 6 | `settings` | Settings and passkey step-up: `settings_controller.py`, `settings_window_html.py`, `step_up_config.py`, `webauthn_stepup.py`, `web/{routes_settings,routes_security,approval_step_up,step_up_decide}.py`, and their tests | 13 / 362 |

**Each child session's definition of done:**

1. It has deleted its `step-N.txt`, and `pytest tests/unit/test_code_no_history.py` passes.
2. `ruff check .` and the full `pytest -q` pass. Step 2 also runs `npm test && npm run typecheck`
   in `mcpb/shim/`. Step 1 also runs `bash -n` on every shell script it touched, and `shellcheck` if
   it is installed.
3. It has re-read its own diff against rules 1–9. Spot-check that no comment was emptied where a
   why was needed, and that no changed string is asserted on by a test outside the slice (`git grep`
   the old string).
4. Its ledger lists: ADR candidates (the decision, the files that cite it, and why it meets the
   bar); changed user-visible strings; cross-slice edits it needs; bugs noticed; and issue URLs kept
   under rule 3, with each issue's state.
5. It has pushed its branch, and it ends with a short summary: files changed, tags removed, and the
   number of ledger entries. It does not open a PR.

Commit messages are written for `main`'s history, because PRs merge with a merge commit. Commit per
area within the slice, for example `Drop tracker tags from privilege separation comments`. Never put
a model name in them.

### Step 7: consolidate and ship (child session, then the orchestrator)

1. The orchestrator merges steps 1–6 into `claude/amazing-bohr-faqr6s` (see the runbook below).
2. Apply the ledgers' cross-slice edits.
3. **ADRs:** check each ADR candidate against the bar, then write the ADR with the next free number
   (after 0055), add it to `docs/adr/README.md`'s index, and change the citing comments to
   `ADR NNNN`. Add one more ADR for this change itself: code carries no project history, a blocking
   guard enforces it, and open issues are cited only by full URL. It records the rejected
   alternative of keeping IDs and relying on the tracker.
4. Add the rule to `docs/coding-and-testing-guidelines.md`'s comment conventions (the section behind
   §2.7's "Comments only where the *why* is non-obvious" row). That document is scanned by
   `test_docs_no_history.py`, so describe the shapes in words, not with literal examples.
5. In `test_code_no_history.py`, remove the pending-list mechanism (`PENDING_DIR`, `_pending`,
   `test_pending_lists_name_each_existing_file_once`, and the directory itself). The guard then
   covers the whole tree, with no exceptions beyond `_ALLOWED`.
6. Add a `CHANGELOG.md` line under `## [Unreleased]` (Changed) for the user-visible strings from the
   ledgers.
7. File the ledgers' bugs as issues, then delete `docs/code-history-tags/` (this plan and the
   ledgers).
8. Run `/dod`. The diff touches connector clients, installers, `debian/` and the privilege
   separation scripts, so dispatch the off-box rows against the branch as
   `.claude/skills/steward/SKILL.md` directs: `build.yml` (packaged smoke tests on all three
   platforms), `linux-`, `macos-` and `windows-graphical-session.yml`, and `connector-live-check.yml`
   for §2.7's QA row. Record the run links for the PR.
9. The orchestrator opens the single PR and drives it to green.

## Orchestration runbook

This is for the session that is told "implement #715". That session is the **orchestrator**. It
owns `claude/amazing-bohr-faqr6s`, has done step 0, and creates one child session per remaining step
with the Claude Code Remote tools.

1. **Create steps 1–6** with `create_session`, six calls in one message:
   - `source_url`: `https://github.com/privacyfence/privacyfence`
   - `source_revision`: `claude/amazing-bohr-faqr6s`
   - `outcome_branch`: `claude/715-step-N-<slug>`
   - `title`: `#715 step N: <slug>`, and `tags`: `["issue-715"]`
   - `permission_mode`: leave unset so that it is inherited. Never use `plan`, because no one is
     watching the children.
   - `prompt`: the step's number, slug and branch, telling it to read this plan first, change only
     the files in its pending list (plus `_ALLOWED` lines and its own ledger), meet the child
     definition of done, push, and not open a PR.
2. **Monitor without polling tightly.** Schedule a `send_later` check-in about every 45 minutes. On
   each check-in, run `get_session` for each child that has not merged yet:
   - Idle, `review_ready` or `completed`, with the branch pushed and its list deleted: merge it
     (step 3 of this runbook).
   - `failed`, `blocked`, or idle without a finished branch: read `list_events`, then `send_message`
     a correction, or recreate the session from its branch.
   - Still working: do nothing.
3. **Merge each finished branch as it arrives**, not all at the end:
   `git fetch origin claude/715-step-N-<slug> && git merge --no-ff FETCH_HEAD`. Then check that
   `step-N.txt` is gone and that `pytest tests/unit/test_code_no_history.py` and `ruff check .`
   pass, and push. A conflict outside `_ALLOWED` means a slice boundary was crossed.
4. **When all six have merged**, run step 7 in a child session on `claude/amazing-bohr-faqr6s`
   (`outcome_branch` `claude/715-step-7-consolidate`) and merge it the same way, or do step 7 in the
   orchestrator.
5. **Open the PR** from `claude/amazing-bohr-faqr6s` into `main`. Use
   `.github/pull_request_template.md`, include `Closes #715`, link step 7's workflow runs, and list
   the ADRs added. Subscribe to it, and drive it to green under the steward skill.
6. **Clean up:** `archive_session` each child once its branch has merged, and delete the
   `claude/715-step-*` branches after the PR merges.
