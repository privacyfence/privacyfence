# Plan: remove project-history tags from code (issue #715)

Temporary plan document. It exists so that the parallel sessions working on #715 share one contract,
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
`tests/fixtures/`, lockfiles, binary files, and git history.

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
5. The §2.7 definition of done passes, including the off-box rows (see step 6).
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
   ADR yourself.** Add an entry to your slice's ledger (below). ADR numbers are assigned in step 6 so
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
8. **Do not touch** `docs/`, `CHANGELOG.md`, `tests/fixtures/`, or any file outside your slice. If a
   file outside your slice has to change (for example, a test in another slice asserts on your
   string), record it in the ledger instead, and step 6 applies it.

## Steps

The work runs as **step 0**, then **steps 1–5 in parallel**, then **step 6**. Steps 1–5 each own a
disjoint set of files, so their branches merge without conflicts.

### Step 0: scaffolding (orchestrator, on `claude/amazing-bohr-faqr6s`)

1. Add `tests/unit/test_code_no_history.py`, modelled on `test_docs_no_history.py`:
   - It scans `git ls-files` minus the out-of-scope set above.
   - Its patterns cover phase names, phase IDs (`P\d{1,2}(\.\d+)?`), bare `#\d{3,4}` issue
     numbers, `owner/repo#NNN`, `SEC-`/`TST-` finding IDs, `§` section references,
     version-qualified history, and letter+number plan IDs (`[BDF]\d{1,2}`). Letter+number IDs are
     not matched inside string literals, so data such as `"F1"` does not count.
   - `_ALLOWED` holds `(path, matched text)` pairs, each with a reason. Start it with the real false
     positives the sweep finds, such as colour literals.
   - It includes a `test_the_patterns_catch_the_shapes_they_exist_for` self-test, like the docs guard.
   - **Pending lists:** it skips every path named in `tests/unit/code_history_pending/step-N.txt`
     (one file per step, one path per line). It also has a test that fails if any hit is in a file
     that no pending list names. The guard is therefore live and blocking from step 0: new tags fail
     everywhere, and a step is finished when its pending file can be deleted.
2. Generate `step-1.txt` … `step-5.txt` from the slice definitions below. Assign every file that
   has a hit to exactly one step, and check with a script that no file is in two lists and no hit
   is unassigned.
3. Create empty ledgers `docs/code-history-tags/ledger-step-N.md`, one per step, so that no two
   steps edit the same file.
4. Commit, run the full suite (it must pass, because every current hit is pending), and push.

### Steps 1–5: one slice each (child sessions, in parallel)

Each child session starts from `claude/amazing-bohr-faqr6s` after step 0 and pushes to its own
branch, `claude/715-step-N-<slug>`. The slice definitions are path prefixes. Tests go with the
code they test, so that a string change and its assertion land together.

| Step | Slug | Files (approx. hits) |
|---|---|---|
| 1 | `install` | Install, privilege separation and packaging: `privilege_separation.py`, `windows_acl.py`, `windows_service.py`, `service_control.py`, `daemon_main.py`, `daemon_status.py`, `paths.py`, `secure_files.py`, `principal.py`; `scripts/*privilege_separation*`, `scripts/build_{pkg,dmg,deb,installer,mcpb}*`, `scripts/check_deb_glibc_floor.py`, `scripts/check_graphical_session_coverage.py`, `scripts/update_dependency_locks.sh`; `installer/`, `debian/`, `resources/`, `PrivacyFenceApp*.spec`, `com.privacyfence.app.plist`, `privacyfence.service`; `tests/platform/`, `tests/integration/test_{deb,macos,windows,linux}_*`, `tests/windows_task_contract.py`, `tests/packaged_policy_probe.py`, and the matching `tests/unit/test_*.py`. (56 files, ~330) |
| 2 | `control` | Control channel, server, companion and shim: `companion.py`, `web_shell.py`, `web/{control_channel,server,state_stream,csp,routes_connect}.py`; `mcpb/`; `tests/control_channel_client.py`, `tests/integration/test_{browser_smoke,mcp_daemon_contract}.py`, and the matching unit tests, **excluding** `tests/unit/web/test_server_org_mode.py` (step 4). (21 files, ~230) |
| 3 | `approvals` | Approvals, settings and step-up: `approval*.py`, `settings_*.py`, `step_up_config.py`, `webauthn_stepup.py`, `dialog_window_html.py`, `web/{routes_settings,routes_security,routes_approvals,approval_step_up,step_up_decide}.py`; `tests/system/`, `tests/integration/test_deferred_approval_round_trip.py`, and the matching unit tests. (29 files, ~370) |
| 4 | `org-policy` | Org mode, auth, audit and policy: `org_*.py`, `audit_*.py`, `audit_log.py`, `gate.py`, `policy/`, `web/{oauth_provider,session_auth,org_*,routes_org_*,mcp_auth,sealed_refresh_store}.py`; `scripts/{build_org_bundle,verify_audit_log}.py`; `tests/unit/policy/`, `tests/unit/abuse/`, `tests/unit/test_p6_principal_isolation.py`, `tests/unit/test_systemic_gate_invariants.py`, `tests/unit/web/test_server_org_mode.py`, `tests/integration/mock_idp.py`, and the matching unit tests. (48 files, ~330) |
| 5 | `connectors` | Everything else: connectors and clients, `local_files.py`, upload/download staging, `text_extraction.py`, `pii_detector.py`, `web/{routes_mcp,mcp_tools,routes_file_bridge,routes_downloads,__init__}.py`, `connector_registry.py`, `connector_host.py`, OAuth helpers, remaining `scripts/` (QA, release, site), `pyproject.toml`, `.gitignore`, `src/privacyfence/resources/sw.js`, and their tests. Also `test_docs_no_history.py` and `test_docs_references_exist.py`, where the hits are probably pattern self-test samples that belong in `_ALLOWED`, not rewrites. (65 files, ~260) |

Step 0's pending lists are the authoritative assignment. This table is the rule used to generate
them.

**Each child session's definition of done:**

1. Its `step-N.txt` is deleted, and `pytest tests/unit/test_code_no_history.py` passes.
2. `ruff check .` and the full `pytest -q` pass. Step 2 also runs `npm test && npm run typecheck`
   in `mcpb/shim/`. Step 1 also runs `bash -n` on every shell script it touched, and `shellcheck`
   if it is installed.
3. `git diff main -- <slice>` has been re-read against rules 1–8. Spot-check: no comment was
   emptied into nothing where a why was needed, and no string changed that a test elsewhere asserts
   on.
4. Its ledger `docs/code-history-tags/ledger-step-N.md` lists: ADR candidates (the decision, the
   files that cite it, and why it meets the bar); changed user-visible strings; cross-slice edits it
   needs; bugs noticed; and issue links kept under rule 3.
5. It pushes its branch and ends with a short summary: files changed, hits removed, and the number
   of ledger entries. It does not open a PR.

Commit messages are written for `main`'s history (PRs merge with a merge commit). Commit per area
within the slice, for example `Drop tracker tags from privilege separation comments`, and never put
a model name in them.

### Step 6: consolidate and ship (child session, then the orchestrator)

1. Merge steps 1–5 into `claude/amazing-bohr-faqr6s` (the orchestrator does this; see below).
2. Apply the ledgers' cross-slice edits.
3. **ADRs:** for each ADR candidate, check that it meets the bar, then write the ADR with the next
   free numbers (after 0055), add it to `docs/adr/README.md`'s index, and change the citing comments
   to `ADR NNNN`. Add one more ADR for this change itself: code carries no project history; a
   blocking guard enforces it; open issues may be cited only by full URL. It records the rejected
   alternative of keeping IDs and relying on a tracker.
4. Add the rule to `docs/coding-and-testing-guidelines.md`'s comment conventions (§2.7's "Comments
   only where the *why* is non-obvious" row, and the section it refers to). That document is itself
   scanned by `test_docs_no_history.py`, so describe the shapes in words, not with literal examples.
5. In `test_code_no_history.py`, remove the pending-list mechanism and its directory. Nothing is
   pending any more, and the test now covers the whole tree without exceptions beyond `_ALLOWED`.
6. Add a `CHANGELOG.md` line under `## [Unreleased]` (Changed) for the user-visible strings from
   the ledgers.
7. Delete `docs/code-history-tags/`, which holds this plan and the ledgers. File any bugs from the ledgers as issues.
8. Run `/dod`. Because the diff touches connector clients, installers, `debian/` and privilege
   separation scripts, dispatch the off-box rows against the branch as `.claude/skills/steward/SKILL.md`
   directs: `build.yml` (packaged smoke tests on all three platforms), `linux-`, `macos-` and
   `windows-graphical-session.yml`, and `connector-live-check.yml` for §2.7's QA row. Link the runs
   in the PR.
9. The orchestrator opens the single PR and drives it to green.

## Orchestration runbook

This is for the session that is told "implement #715". That session is the **orchestrator**. It
owns `claude/amazing-bohr-faqr6s`, does step 0 itself, and creates one child session per remaining
step with the Claude Code Remote tools.

1. **Step 0** in the orchestrator itself, then push.
2. **Create steps 1–5** with `create_session`, five calls in one message:
   - `source_url`: `https://github.com/privacyfence/privacyfence`
   - `source_revision`: `claude/amazing-bohr-faqr6s` (after step 0 has been pushed)
   - `outcome_branch`: `claude/715-step-N-<slug>`
   - `title`: `#715 step N: <slug>`, and `tags`: `["issue-715"]`
   - `permission_mode`: leave unset so that it is inherited. Never use `plan`, because no one is
     watching the children.
   - `prompt`: *"You are step N of the plan in `docs/code-history-tags/plan.md` on this branch
     (issue #715). Read the whole plan first. Your slice is `tests/unit/code_history_pending/step-N.txt`;
     change no file outside it. Follow the rewrite rules, meet the child definition of done, push to
     your branch, and do not open a PR."*
3. **Monitor without polling tightly.** Schedule a `send_later` check-in about every 45 minutes.
   On each check-in, `get_session` each child. For `status_bucket` values:
   - `review_ready`/`completed` with the branch pushed: go to step 4 for that branch.
   - `failed` or `blocked`: read `list_events`, then either `send_message` a correction or recreate
     the session from the same branch.
   - Still working: re-arm the check-in and do nothing else.
4. **Merge each finished branch as it arrives**, not all at the end:
   `git fetch origin claude/715-step-N-<slug> && git merge --no-ff FETCH_HEAD`. Then check that
   `step-N.txt` is gone and that `pytest tests/unit/test_code_no_history.py` and `ruff check .`
   pass, and push. A conflict means a slice boundary was crossed. Resolve it in the child's favour
   only for its own files, and send the child a correction if needed.
5. **When all five have merged**, create the step 6 child session on `claude/amazing-bohr-faqr6s`
   with `outcome_branch` `claude/715-step-6-consolidate`, then merge it the same way. Alternatively,
   do step 6 in the orchestrator if its context is fresh enough.
6. **Open the PR** from `claude/amazing-bohr-faqr6s` into `main`. Use
   `.github/pull_request_template.md`, include `Closes #715`, link the step 6 workflow runs, and list
   the ADRs added. Subscribe to it, and drive it to green under the steward skill.
7. **Clean up:** `archive_session` each child once its branch has merged, and delete the
   `claude/715-step-*` branches after the PR merges.

Total: 7 sessions (the orchestrator, five slices, and consolidation). This is within the default
workflow size guideline.
