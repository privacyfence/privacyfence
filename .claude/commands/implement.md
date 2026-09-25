---
description: Orchestrate a docs/*-plan.md — one child session per phase, merged into one feature branch, one PR to main
argument-hint: "<URL of the plan file on GitHub, e.g. https://github.com/privacyfence/privacyfence/blob/<ref>/docs/foo-plan.md>"
---

You are the **orchestrator** for the plan at:

$ARGUMENTS

You do not implement phases yourself. You start one child session per phase, merge each finished
phase into one feature branch, verify the merge, and at the end open **one** pull request from that
feature branch to `main` and drive it to green. The user started you with this command. That is
their explicit permission to create and push the feature branch named in the manifest, and to push
merge commits to it. It is not permission to push anywhere else.

## 0. Load the plan

1. Parse the URL as `https://github.com/<owner>/<repo>/blob/<ref>/<path>`, then
   `git fetch origin <ref>` and read the file with `git show FETCH_HEAD:<path>`. Read it all.
2. Find the fenced ` ```yaml ` block under the plan's `## Implementation manifest` heading and
   parse it. If there is no manifest, stop and tell the user: this command only runs plans that
   have one. Do not invent phases from prose.
3. Validate the manifest before starting anything. Every `depends_on` must name an existing phase
   id; the graph must have no cycles; every phase needs `brief` and `acceptance`. Report any error
   and stop.

## 1. The feature branch

- `feature_branch` comes from the manifest (it must follow CLAUDE.md's `<type>/<kebab-case>` rule).
- **If it does not exist on origin yet:** create it from `<ref>` (the plan's own branch, so the plan
  and this command travel with it), then merge `origin/main` into it (a merge commit, not a rebase)
  and push. Try `git push -u origin <feature_branch>` first. If the git proxy refuses the push
  (403, or "not allowed"), create the branch with the GitHub MCP `create_branch` tool from
  `<ref>`'s SHA, then retry the push. If the push is still refused, stop and tell the user in one
  line. Do not quietly fall back to your own session branch, because child sessions would then
  merge somewhere the user did not choose.
- **If it already exists:** this is a resumed run. Check it out and continue from §2. A phase is
  done if the feature branch's history holds a merge commit whose message carries the trailer
  `Plan-Phase: <plan slug>/<phase id>` (`git log --grep`). Do not re-run done phases.

## 2. Scheduling

Keep an orchestration ledger as a comment on a GitHub issue if the manifest names `tracking_issue`.
Otherwise keep it in your own replies: for each phase record its state (`pending` / `running` /
`merged` / `failed`), its session id, and its branch. Update the ledger on every state change.

A phase is **ready** when every phase in its `depends_on` is `merged`. Start every ready phase at
once, up to `max_parallel` (manifest, default 2). Phases in the same wave touch different files by
design. If a merge conflicts anyway, §4 handles it.

## 3. Starting a phase session

Phase branch: `<feature_branch>--<phase id>` (for example `feature/org-mode-mobile--p2-settings`).
Before starting, record the feature branch's current SHA as the phase's base.

Call `create_session` with:

- `source_url`: the repository's git URL
- `source_revision`: `<feature_branch>` (so the child sees every phase already merged)
- `outcome_branch`: the phase branch
- `title`: `<plan slug> <phase id>: <phase title>`
- `permission_mode`: omit it, so the child inherits yours. Never `plan`, because nobody is watching
  a child to approve a plan.
- `prompt`: the **child brief** below, filled in.

> You are implementing **one phase** of a multi-phase plan. An orchestrator session will merge your
> branch; you do not open a pull request, and you do not push to any branch except
> `<phase branch>`.
>
> Plan: `<path>` in this checkout. Read it in full first; it has the context, constraints and
> measurements this brief does not repeat. Your phase is `<phase id>`, "<phase title>". Its entry
> in the plan's `## Implementation manifest` is authoritative. Here it is verbatim:
>
> ```yaml
> <the phase's manifest entry>
> ```
>
> Rules:
> 1. Stay inside this phase's `brief`. If you find work that belongs to another phase, note it in
>    your final report; do not do it. If the brief turns out to be wrong or impossible, stop and
>    say so in your final report instead of improvising a different design.
> 2. Follow CLAUDE.md, `docs/coding-and-testing-guidelines.md` and `.claude/skills/steward/SKILL.md`.
>    Add user-visible changes under `CHANGELOG.md`'s `## [Unreleased]`. Never add a version
>    heading.
> 3. Before your last push, run `/dod`. Every blocking row must pass. Also check every item in
>    your phase's `acceptance` list, and say how you checked each one.
> 4. Your final commit message ends with the trailer `Plan-Phase: <plan slug>/<phase id>`.
> 5. End your last turn with a report in exactly this shape, which the orchestrator reads:
>    `PHASE-REPORT <phase id> status=<done|blocked> head=<sha>` on its own line, then the `/dod`
>    table, the acceptance checklist, and any out-of-scope findings.

## 4. Waiting, collecting and merging

Child sessions do not report back when they finish cleanly. Between checks, end your turn after
arming a check-in with `send_later` (about 20 minutes; 45 when only long phases are running). Never
poll with `sleep`. A `<child-session-event>` wake (a failed turn or a restarted worker) is handled
immediately.

On each check-in, for each `running` phase, call `get_session`:

- `status_bucket` `working`: leave it alone.
- `blocked` or `review_ready`/`completed`: read its last report. Find the `PHASE-REPORT` line.
  - `status=done`: go to **merge** below.
  - `status=blocked`, or no report: read why. If the fix is small and clearly inside the phase,
    send the child a steering message (`send_message`, if you have it) or start a follow-up
    session on the same phase branch with the specific instruction. Otherwise mark the phase
    `failed` and **ask the user**, quoting the child's reason. Do not start dependent phases.
- `failed`: start one retry session on the same phase branch, with a brief that says what the
  first attempt got to (read the branch). If a second session also fails, mark the phase `failed`
  and ask the user.

**Merge** (one phase at a time, even when several finish together):

1. `git fetch origin <phase branch> <feature_branch>`. The phase branch head must equal the
   reported `head=` SHA, and its tip commit must carry the `Plan-Phase` trailer. Otherwise it is not
   done.
2. Review the diff against the phase's recorded base (`git diff <base>...origin/<phase branch>`)
   adversarially: is it inside the brief, does it touch files other phases own, has it disabled or
   skipped a test, has it deleted an `xfail` that belongs to a different phase? Anything wrong
   goes back to the child, as above.
3. `git checkout <feature_branch> && git merge --no-ff origin/<phase branch>` with the message
   `Merge <plan slug> <phase id>: <title>` and the trailer `Plan-Phase: <plan slug>/<phase id>`.
   - **Conflict:** resolve it yourself only when it is mechanical (both sides added to the same
     list, or adjacent edits). Regenerate lockfiles and generated files (compiled CSS,
     `requirements/*.lock.txt`) with the repo's tools, never by hand-editing. If a conflict is
     semantic, abort the merge and start a short session on the phase branch whose brief is "merge
     `<feature_branch>` into this branch and resolve the conflict"; then merge again.
4. Verify **after the merge**, on the feature branch: `ruff check .`,
   `python3 -m pytest tests/unit -q`, and the manifest's `verify_after_merge` commands. If anything
   is red, `git merge --abort` is no longer possible, so revert the merge commit
   (`git revert -m 1`), push, send the failure back to the phase, and mark the phase `running`
   again.
5. Push the feature branch. Mark the phase `merged`, delete the phase branch on origin, and
   `archive_session` the child. Then start any phases that just became ready.

## 5. Finishing

When every phase is `merged`:

1. Merge `origin/main` into the feature branch once more and run the full `/dod`.
2. Check the manifest's `final_checks` (for example: no `xfail` left that this plan added, and the
   plan document is deleted and its decisions extracted into ADRs, per CLAUDE.md "Decisions, plans
   and ADRs").
3. Open **one** pull request from the feature branch to `main`, following the PR template if the
   repo has one. The body lists each phase with its merge commit, the combined acceptance
   checklist, anything the manifest marks `manual` (such as real-device checks) as unchecked items
   for a human, and every out-of-scope finding the children reported.
4. Subscribe to the PR and drive it to green as `.claude/skills/steward/SKILL.md` says. You own
   this PR. A failure goes to a new phase-style session on a `<feature_branch>--fix-<n>` branch,
   merged the same way, or you fix it yourself when it is small.
5. Tell the user the PR link, what is still manual, and anything a child flagged.

Never: push to `main`; merge the PR yourself; rebase or force-push the feature branch; run a phase's
work in your own session to "save time"; skip a phase whose dependency failed.
