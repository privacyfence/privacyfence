---
description: Orchestrate a docs/*-plan.md — one child session per phase, merged into one feature branch, one PR to main
argument-hint: "<GitHub URL of the plan file: https://github.com/<owner>/<repo>/blob/<ref>/<path>>"
model: sonnet
---

You are the **orchestrator** for the plan at:

$ARGUMENTS

You do not implement phases yourself. You start one child session per phase, merge each finished
phase into one feature branch, verify the merge, have one Opus session review the whole result,
and at the end open **one** pull request from that feature branch to `main` and drive it to green.
The user started you with this command. That is their explicit permission to create and push the
feature branch named in the manifest, and to push merge commits to it. It is not permission to
push anywhere else.

Plans for this command are written by `/make-plan` (`.claude/commands/make-plan.md`), which also
defines the manifest's fields. Hand-written plans work too, as long as they have a manifest.

**Models.** This command runs on Sonnet (the `model:` line above). That covers the turn the user
starts it in; later turns (check-ins, wakes) run on the session's own model, so for a Sonnet
orchestrator throughout, start the session on Sonnet. The plan was sized for Sonnet workers by an
Opus planner, so judgement calls should be rare; when one comes up that the plan does not settle,
ask the user rather than deciding it.

| Session | `create_session` `model` |
|---|---|
| Phase worker, fix session, conflict session | `claude-sonnet-5-5`, or `claude-opus-5-5` when the phase has `worker_model: opus` |
| Final review (section 5) | `claude-opus-5-5` |

If `create_session` rejects a model id, use the newest Sonnet or Opus id your system prompt lists,
and say which one in the ledger.

## 0. Load the plan

1. Parse the URL as `https://github.com/<owner>/<repo>/blob/<ref>/<path>`, then
   `git fetch origin <ref>` and read the file with `git show FETCH_HEAD:<path>`. Read it all.
2. Find the fenced ` ```yaml ` block under the plan's `## Implementation manifest` heading and
   parse it. If there is no manifest, stop and tell the user: this command only runs plans that
   have one. Do not invent phases from prose.
3. Validate the manifest before starting anything. Every `depends_on` must name an existing phase
   id; the graph must have no cycles; every phase needs `brief` and `acceptance`; `complexity`,
   when present, is `S` or `M`; a `worker_model: opus` phase has a `worker_model_reason`. Where
   phases carry `touches`, two phases that could run at the same time (neither reaches the other
   through `depends_on`) must not share a path. Report any error and stop. An older manifest's
   `manual:` list means the same as `manual_after:`.
4. **Manual steps before anything starts.** If `manual_before` is non-empty, the ledger does not
   already record the user's confirmation, and the feature branch does not exist on origin yet (it
   is only created after this confirmation, so its existence means a resumed run), stop here. Send the user the `manual_steps_artifact`
   link and the `manual_before` items (title and `done_when`), and ask one question: are these
   done? Do not create the feature branch, start any session or arm a check-in while you wait; the
   reply wakes you. Where a `done_when` is something you can check from here (an environment
   variable is set, a host is reachable), check it after they answer and say what you found.
   Record the confirmation in the ledger, then continue.

## 1. The feature branch

- `feature_branch` comes from the manifest (it must follow CONTRIBUTING.md's `<type>/<kebab-case>` rule).
- **If it does not exist on origin yet:** create it from `<ref>` (the plan's own branch, so the plan
  and this command travel with it), then merge `origin/main` into it (a merge commit, not a rebase)
  and push. Try `git push -u origin <feature_branch>` first. If the git proxy refuses the push
  (403, or "not allowed"), create the branch with the GitHub MCP `create_branch` tool from
  `<ref>`'s SHA, then retry the push. If the push is still refused, stop and tell the user in one
  line. Do not quietly fall back to your own session branch, because child sessions would then
  merge somewhere the user did not choose.
- **If it already exists:** this is a resumed run. Check it out and continue from "Scheduling" below. A phase is
  done if the feature branch's history holds a merge commit whose message carries the trailer
  `Plan-Phase: <plan slug>/<phase id>` (`git log --grep`). Do not re-run done phases.

## 2. Scheduling

Keep an orchestration ledger as a comment on a GitHub issue if the manifest names `tracking_issue`.
Otherwise keep it in your own replies: for each phase record its state (`pending` / `running` /
`merged` / `failed`), its session id, and its branch. Update the ledger on every state change.

A phase is **ready** when every phase in its `depends_on` is `merged`, and, if any of those
phases is marked `human_gate: true`, the user has approved it (see "Merge" step 6 below). Start every ready phase at
once, up to `max_parallel` (manifest, default 2). Phases in the same wave touch different files by
design. If a merge conflicts anyway, "Waiting, collecting and merging" below handles it.

## 3. Starting a phase session

Phase branch: `<feature_branch>--<phase id>` (for example `feature/org-mode-mobile--p2-settings`).
Before starting, record the feature branch's current SHA as the phase's base.

Call `create_session` with:

- `source_url`: the repository's git URL
- `source_revision`: `<feature_branch>` (so the child sees every phase already merged)
- `outcome_branch`: the phase branch
- `title`: `<plan slug> <phase id>: <phase title>`
- `model`: the worker model from the table at the top
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
> 1. Stay inside this phase's `brief`, and inside its `touches` list if it has one. If you find
>    work that belongs to another phase, note it in your final report; do not do it. If the brief
>    turns out to be wrong or impossible, or leaves a decision open that the plan's Design section
>    does not settle, stop and say so in your final report instead of improvising a different
>    design. Never do anything the plan lists under `manual_before` or `manual_after`; if a step
>    needs something from the user, stop with `status=blocked` and say what.
> 2. Follow CLAUDE.md, CONTRIBUTING.md, `docs/releasing.md`, `docs/coding-and-testing-guidelines.md` and `.claude/skills/steward/SKILL.md`.
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
   adversarially: is it inside the brief, does it change paths outside its `touches` (a small,
   explained addition such as a shared test fixture is fine; anything else goes back), does it
   touch files other phases own, has it disabled or
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
   `archive_session` the child. If the manifest has `screenshots_after_merge`, run it and send the
   user the images (`SendUserFile`) with a one-line caption naming the phase, so they can watch
   the result take shape and object early.
6. **Human gate.** If the phase is marked `human_gate: true`, stop scheduling. Send the user the
   phase's review material (its screenshots and its `PHASE-REPORT`), and ask one question:
   approve, or change what. Do not arm a check-in while you wait for their answer; the reply
   wakes you. On approval, record it in the ledger and continue. On requested changes, start a
   follow-up session on a fresh `<feature_branch>--<phase id>-rev<n>` branch cut from the
   feature branch, whose brief is the user's feedback verbatim plus the phase's original brief.
   Merge it the same way, and gate again.
7. Start any phases that just became ready.

## 5. Final review (Opus)

When every phase is `merged`, merge `origin/main` into the feature branch once more, push, and
start one review session with `create_session`: `source_revision` the feature branch, no
`outcome_branch`, `model` the review model from the table at the top, title
`<plan slug> final review`, and this prompt, filled in:

> You are the final reviewer for a plan that Sonnet sessions implemented phase by phase. You do not
> change code and you push nothing. Read the plan (`<path>` at `<ref>` — `git fetch origin <ref>`,
> then `git show FETCH_HEAD:<path>`, since the retirement phase has deleted it from this branch),
> `CLAUDE.md`, `CONTRIBUTING.md`, and `docs/coding-and-testing-guidelines.md`. Then review
> `git diff origin/main...HEAD` against the plan's Design section and each phase's `acceptance`:
> behaviour that differs from the design, a design decision a worker made that the plan did not,
> seams between phases (a function one phase added and another calls wrongly, duplicated
> helpers, inconsistent names or strings), missing tests for an acceptance item, a weakened or
> skipped test, a security or privacy regression, docs that describe something other than what
> the code does, and every `final_checks` item. Run `ruff check .` and
> `python3 -m pytest tests/unit -q` yourself.
>
> End with `REVIEW-REPORT verdict=<approve|changes>` on its own line, then one numbered finding per
> line: `blocking|non-blocking`, `path:line`, the phase id it belongs to, and what is wrong and what
> the fix is. `changes` means at least one finding is blocking. Only something you would refuse to
> merge is blocking.

Wait for it with check-ins as in section 4. Then:

- `approve`: go to Finishing.
- `changes`: group the blocking findings by phase and start one Sonnet fix session per group on a
  `<feature_branch>--fix-<n>` branch, whose brief is the findings verbatim plus the phase's
  original entry. Merge each exactly as a phase (section 4's **Merge**, trailer
  `Plan-Phase: <plan slug>/fix-<n>`). Then start one more review session with the same prompt plus
  "Earlier review findings: <list>; confirm each is fixed and review only the diff since
  `<sha>`". If that second review still says `changes`, stop and ask the user, quoting the
  findings.

`archive_session` each review session once you have read its report. Non-blocking findings go into
the PR body.

## 6. Finishing

1. Run the full `/dod` on the feature branch.
2. Check the manifest's `final_checks` (for example: no `xfail` left that this plan added, and the
   plan document is deleted and its decisions extracted into ADRs, per CONTRIBUTING.md "Decisions, plans
   and ADRs").
3. Open **one** pull request from the feature branch to `main`, following the PR template if the
   repo has one. The body lists each phase with its merge commit, the combined acceptance
   checklist, the final review's verdict and its non-blocking findings, every out-of-scope
   finding the children reported, and a **Manual verification** section: each `manual_after`
   item as an unchecked box, with the `manual_steps_artifact` link.
4. Subscribe to the PR and drive it to green as `.claude/skills/steward/SKILL.md` says. You own
   this PR. A failure goes to a new phase-style session on a `<feature_branch>--fix-<n>` branch,
   merged the same way, or you fix it yourself when it is small.
5. Tell the user the PR link, the artifact link with the `manual_after` steps they now need to do,
   and anything a child or the reviewer flagged. The plan branch (`plan/<slug>`) can be deleted
   once the PR merges.

Never: push to `main`; merge the PR yourself; rebase or force-push the feature branch; run a phase's
work in your own session to "save time"; skip a phase whose dependency failed; do a `manual_*` step
on the user's behalf.
