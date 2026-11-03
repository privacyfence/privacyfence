# devflow pack for Archon

`devflow-ship` runs the devflow toolkit's flow ([andras-tkcs/claude-toolkit](https://github.com/andras-tkcs/claude-toolkit))
as one Archon run, from an ask to a merged pull request:

1. **make-plan** (large tier): researches and writes `plan.md` with an implementation manifest,
   validated by the toolkit's `validate_manifest.py`. When a decision is the user's, or
   `manual_before` steps need confirming, the run pauses in the attention queue and replans with the
   answer.
2. **implement** (medium tier): one fresh session per phase, each followed by the profile's
   `verify.fast`. Large plans merge each phase `--no-ff` with a `Plan-Phase:` trailer, as `/implement`
   does. A gate then checks that every manifest phase landed and the fast checks are green.
3. **review** (large tier, fresh context) with the `review-checklist` and `secure-code-review` skills.
4. **fix blocking**: fix, then review again, up to three reviews. A gate fails the run if blocking
   findings remain.
5. **fix non-blocking**: each finding is fixed or declined with a reason; the PR body quotes the table.
6. **definition of done** (`verify.dod`), then **one PR**, then a **watch** that polls every 10 minutes
   and hands conflicts, red checks, review threads and comments to a steward session. The steward
   follows `pr-steward` and `.claude/skills/steward/SKILL.md`. Each item is handed over once per head,
   so a red check the steward could not fix waits for a person rather than looping. The run ends
   when a person merges or closes the PR. It never merges.

Every project fact comes from `.claude/toolkit.yaml`; the workflow repeats none of it.

## Run it

```bash
archon workflow run devflow-ship "Fix https://github.com/privacyfence/privacyfence/issues/<n>" --detach
```

From chat, ask the orchestrator to run `devflow-ship` with the issue or the description.

## Host requirements

`preflight` checks these before any model is called: the devflow plugin installed for the user
running Archon, a valid `.claude/toolkit.yaml`, `gh` authenticated, and `uv` on `PATH`. Script nodes
need `bun`. `setup-env` builds `.venv` with uv the way `.claude/hooks/session-start.sh` does for a
cloud session. Commands the host cannot run (no `npm` here) are reported as `UNAVAILABLE` in the
definition of done and in the PR body, never ticked; CI runs them.

## Where it differs from the toolkit commands

- The plan is a run artifact, not a `plan/<slug>` branch. The retirement phase still writes the ADRs,
  docs and changelog line, but there is no plan document to delete.
- Phases run one after another in the run's worktree rather than as parallel cloud sessions.
- Questions for the user go through the attention queue instead of `AskUserQuestion`, and manual steps
  are written into the plan instead of an HTML artifact.
- Non-blocking findings are fixed or declined, rather than only listed in the PR body.
- The PR is watched until it merges, not only until it is green.

## Prove changes

```bash
archon validate workflows devflow-ship
archon workflow test devflow-ship
```

The `review-never-clears` and `dod-stays-red` fixtures execute the gates (`exec-code: true`) and
must stay red.
