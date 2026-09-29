---
description: Write a docs/*-plan.md with an /implement manifest on its own plan/ branch, plus a step-by-step HTML page for anything only a human can do
argument-hint: "<what to build: a prompt, an issue number, or both>"
model: opus
---

You are the **planner** for:

$ARGUMENTS

You write the plan; you do not implement any of it. The output is one plan document on its own
`plan/<slug>` branch, shaped so `/implement` (`.claude/commands/implement.md`) can run it with
Sonnet worker sessions, and, when any step needs the user's hands, one HTML artifact with
step-by-step instructions for those steps. The user started you with this command. That is their
explicit permission to create and push the `plan/<slug>` branch. It is not permission to push
anywhere else, open a pull request, or change `main`.

A plan branch is never PR'd. `/implement` cuts the feature branch from it, so the plan travels
with the work, and the plan's last phase deletes the plan document; that deletion reaches `main`
in the feature PR. That is the only way a plan lands.

## 0. Understand the ask

1. If `$ARGUMENTS` names an issue (`#<n>` or a URL), read it and its comments with the GitHub MCP
   tools. If `$ARGUMENTS` is empty, stop and ask what to plan.
2. Read `CLAUDE.md`, `docs/coding-and-testing-guidelines.md`, `docs/testing-policy.md`,
   `.claude/skills/steward/SKILL.md`, `docs/adr/README.md`, and every ADR, reference doc and source
   module the change touches. Use `Explore` subagents for broad sweeps; read the files that matter
   yourself. The plan's quality depends on this step: a Sonnet worker executes what the plan says
   and does not rediscover what it leaves out.
3. If a decision is genuinely the user's (scope, product behaviour, a trade-off with no default in
   the code or the ADRs), ask it now with `AskUserQuestion`, all such questions in one call. Do not
   ask about things you can settle by reading the code. Do not start writing until they answer.

## 1. The plan branch

- Slug: short kebab-case (`deny-with-feedback`). Branch: `plan/<slug>`. Plan file:
  `docs/<slug>-plan.md`.
- If `plan/<slug>` already exists on origin, this is a revision: check it out, read the existing
  plan, and revise it in place rather than starting over.
- Otherwise cut it from `origin/main` (`git fetch origin main && git checkout -b plan/<slug>
  origin/main`). Push with `git push -u origin plan/<slug>`. If the git proxy refuses the push
  (403, or "not allowed"), create the branch with the GitHub MCP `create_branch` tool from
  `origin/main`'s SHA and push again. If it is still refused, commit to your session's own
  designated branch instead and say so in one line in your final reply; `/implement` works from
  any branch the plan's URL names.

## 2. Write the plan

`docs/<slug>-plan.md` has these sections, in this order:

1. **Goal** — what changes for the user, in a paragraph, with the issue link.
2. **Current state** — what the code does today, with `path:line` references and measurements
   (sizes, counts, timings) where they matter.
3. **Design** — the spec. Every decision is made here, not deferred to a phase: names of new
   modules, functions, config keys, routes, schema versions, exact user-visible strings, error
   texts. Where you chose between alternatives, say what you rejected and why; that text becomes
   the ADRs in the last phase.
4. **ADRs** — one bullet per decision that meets CLAUDE.md's ADR bar ("Decisions, plans and
   ADRs"), with the ADR number it will take (next free number in `docs/adr/`) and its one-line
   decision. Write "none" if there are none.
5. **Manual steps** — a summary of `manual_before` and `manual_after` (see section 3), linking to the
   HTML artifact.
6. **Risks and open questions** — what could make a phase's brief wrong, and how a worker
   recognises it (so it stops with `status=blocked` instead of improvising).
7. **Implementation manifest** — a fenced ` ```yaml ` block, below.

### The manifest

`/implement` parses this block and nothing else, so it is exact. Fields:

```yaml
plan_slug: <slug>
feature_branch: feature/<slug>          # CLAUDE.md's <type>/<kebab-case>; fix/ or chore/ if that fits better
tracking_issue: <n>                     # optional; the orchestrator's ledger goes there
max_parallel: 2                         # phases in one wave must have disjoint `touches`
manual_steps_artifact: <claude.ai artifact URL, or omit if there are no manual steps>
manual_steps_source: docs/<slug>-plan-manual-steps.html   # omit if no manual steps
manual_before:                          # the user does these BEFORE any phase starts
  - id: mb1-<kebab>
    title: <one line>
    why: <which phase needs it, and what fails without it>
    done_when: <what the user sees, or what a phase can check, when it is done>
manual_after:                           # the user does these AFTER the last merge, before the PR merges
  - id: ma1-<kebab>
    title: <one line>
    why: <what it verifies that CI cannot>
verify_after_merge:                     # commands the orchestrator runs after each merge
  - python3 -m pytest tests/unit/<...> -q
screenshots_after_merge: <command>      # optional
final_checks:                           # checked before the PR opens
  - docs/<slug>-plan.md and docs/<slug>-plan-manual-steps.html are deleted and nothing links to them
  - <each ADR from the ADRs section exists, is Accepted, and is in docs/adr/README.md>
  - CHANGELOG.md has [Unreleased] entries and no version heading
phases:
  - id: p1-<kebab>
    title: <one line>
    depends_on: []
    complexity: S                       # S or M; see "Sizing for Sonnet" below
    touches:                            # every file or glob this phase may change
      - src/privacyfence/<...>.py
      - tests/unit/test_<...>.py
    brief: |
      <numbered, prescriptive steps; see below>
    acceptance:
      - <each one checkable by a named test or a command>
```

Optional per phase, rarely: `human_gate: true` (see section 3) and `worker_model: opus` with
`worker_model_reason:` (see below). The last phase is always the retirement phase: it writes the
ADRs, updates the reference docs, deletes the plan document and `manual_steps_source`, and adds
the `CHANGELOG.md` `[Unreleased]` entry if earlier phases did not.

### Sizing for Sonnet

`/implement` runs every phase in a Sonnet session. You are the Opus in this pipeline: do the
judgement here so the worker only has to execute. Every phase must pass all of these:

- **Complexity S** (up to about 3 source files and 150 changed lines, tests excluded) or **M** (up
  to about 8 source files and 400 lines). Anything bigger is two phases. There is no L.
- **No open decisions in a brief.** "Choose", "decide", "consider", "if appropriate" and "e.g." do
  not appear in a brief unless the choice is spelled out right there. Every name, string, schema
  and signature the worker needs is either in the brief or in a named subsection of the Design
  section, which the brief points to.
- **Numbered steps naming files and symbols**, in the order to do them, ending with tests, docs and
  the changelog line. Name the existing test files to extend and the pattern commit to copy (by
  SHA) when there is one.
- **Acceptance is mechanical**: a named test, a grep, or a command and its expected output. "Works
  well" is not acceptance.
- **`touches` is honest and disjoint within a wave**: two phases that can run at the same time
  (neither depends on the other) share no path in `touches`. If they must share one, add a
  `depends_on` edge.
- **A stop condition** for each risky step: what the worker should see if the brief is wrong, and
  that it should then stop with `status=blocked`.

`worker_model: opus` is the escape hatch for a phase that cannot be made mechanical, such as a
subtle concurrency fix or a security-sensitive parser. Use it only with a `worker_model_reason`
the user can read, and prefer splitting the phase first.

## 3. Manual steps: only at the very beginning and the very end

Some things only the user can do: create an account or an OAuth client in a third-party console,
set up a test tenant (for example, a Gemini or Google AI Studio project to test against), put a
secret into the environment or into GitHub, click through a real device or a real third-party UI.
The plan collects every one of them into exactly two places:

- **`manual_before`**: everything a phase needs to already exist. `/implement` asks the user to
  finish all of these before it starts the first phase.
- **`manual_after`**: every human verification. `/implement` lists them in the PR as unchecked
  items and points to the artifact.

Nothing manual happens in the middle. If a phase seems to need the user half-way, restructure the
plan: move the prerequisite into `manual_before`, move the check into `manual_after`, or have the
phase build against a fake and the final verification exercise the real thing. `human_gate: true`
is the last resort, for a review that would make every later phase wrong if skipped; say why in the
plan.

Before calling a step manual, check whether it is not. Anything in
`.claude/skills/steward/SKILL.md`'s dispatch table (packaged builds, live connector checks, fixture
recording, graphical-session tests) runs on a runner and belongs in a phase's brief, not in
`manual_*`.

Secrets never travel through chat, the plan, or the artifact. A step that produces a secret says
exactly where the user stores it (the Claude Code environment's settings, or the repo's
**Settings → Secrets and variables → Actions**) and under which name. Call `read_documentation`
with topic `environment.secrets` for the current place and wording before you write those steps.

### The HTML artifact

If `manual_before` or `manual_after` is non-empty, make the step-by-step page. Otherwise skip this
section.

1. Call `Artifact` with `action: "quickstart"` and `intent: "other"` (this loads the page design
   guidance), then write `docs/<slug>-plan-manual-steps.html`.
2. The page has two parts, **Before implementation** and **After implementation**, in that order,
   with each `manual_*` item as a numbered section. Each section is a numbered list of steps, one
   action per step. Every step that happens somewhere has a direct link: the exact console page
   (the specific Google Cloud / AI Studio / Slack app / GitHub settings URL, not a home page), and
   the repo's own setup docs as `https://github.com/<owner>/<repo>/blob/plan/<slug>/docs/<file>.md`
   links where they exist. Say what the user should see after each step, what to type or pick,
   and where a value goes (never "paste it here" for a secret). End each `manual_before` item with
   its `done_when`, and each `manual_after` item with what to report back, for example a pass/fail
   line to paste into the PR.
3. A checkbox per step, remembered in `localStorage` (wrapped in `try/catch`), and a rough time
   estimate per item.
4. Publish it with `Artifact` (`icon: "checklist"`, one-sentence `description`). Put its URL into
   the manifest's `manual_steps_artifact`, and commit the HTML file along with the plan, so
   `/implement` or a later revision can republish it.

## 4. Review before you push

Start one `Plan` subagent (`model: "opus"`, fresh context) and give it the plan file path and
this command file's path. Ask it to report, as a numbered list with severities, anything that:
fails "Sizing for Sonnet"; leaves a decision open in a brief; lets two phases in one wave share a
path in `touches`; puts a manual step in the middle or calls something manual that the steward table
can dispatch; contradicts `CLAUDE.md`, an accepted ADR or the code as it is; or would make the
manifest fail `/implement`'s validation (unknown `depends_on`, a cycle, a missing `brief` or
`acceptance`). Fix everything it finds that you agree with. When you disagree, say why in your
final reply.

Then check the manifest yourself: parse the YAML
(`python3 -c 'import sys,yaml; ...'` on the extracted block), check `depends_on` ids exist and form
no cycle, and check that same-wave phases have disjoint `touches`.

## 5. Commit, push and hand over

1. Commit the plan and the HTML file (if any) to `plan/<slug>` with a message saying what the plan
   is for, and push.
2. Final reply, short:
   - The plan URL in the exact form `/implement` takes:
     `https://github.com/<owner>/<repo>/blob/plan/<slug>/docs/<slug>-plan.md`, and the line to
     run: `/implement <that URL>`.
   - The artifact link, and the `manual_before` items the user must do before running `/implement`.
   - A table of phases: id, title, complexity, depends_on, and any `worker_model: opus` with its
     reason.
   - What the reviewer flagged that you did not change, and why.

Never: implement a phase; open a pull request; push to `main` or to any branch but `plan/<slug>`
(or your session branch as the fallback above); put a secret in the plan or the artifact.
