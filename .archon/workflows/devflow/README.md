# devflow pack for Archon

Seven workflows built on the devflow toolkit ([andras-tkcs/claude-toolkit](https://github.com/andras-tkcs/claude-toolkit)),
the only workflows this repo exposes (`.archon/config.yaml` turns the bundled `archon-*` ones off).

| Workflow | Use it for | Pauses for a person when |
|---|---|---|
| `devflow-ship` | An issue or change that needs a plan, through to a merged PR | The planner has a decision only you can make, or `manual_before` steps to confirm |
| `devflow-quick` | A small edit with no plan (a typo, a one-file fix); refuses anything bigger | Never; it cancels with the reason if the edit is not small |
| `devflow-docs` | Review the docs and the website, fix what is wrong | Never; undecidable items are listed in the PR |
| `devflow-audit` | Security audit, then verify the findings, plan and fix them | The verifier leaves a decision to the maintainer |
| `devflow-release` | Changelog PR, `build.yml` pre-flight, tag dry run, real tag | Confirming the facts, merging the changelog PR, approving the real cut |
| `devflow-config` | Review a repo's rulesets, Actions, access and security settings and workflow files; apply what you approve | Choosing which settings changes to apply (needs an admin token) |
| `devflow-deps` | Repair and push Dependabot PRs; fix vulnerability alerts no PR covers | Never; PRs it cannot fix are handed to a person with the evidence |

`devflow-tail` is the shared building block (review, fixes, definition of done, PR, watch) that the
first three include (`devflow-audit` reaches it through `devflow-ship`); it is not run on its own. No
workflow merges.

## devflow-ship, step by step

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
need `bun`; `devflow-deps` also needs `node` and `npm`. `setup-env` builds `.venv` with uv the way `.claude/hooks/session-start.sh` does for a
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

Fixtures named for a red path (`review-never-clears`, `dod-stays-red`, `not-small`, `nothing-to-fix`,
`reject-real-cut`, ...) must stay red; the ones that execute a gate set `exec-code: true`.

## The other workflows

- **devflow-quick** writes a short `work.md` instead of a plan, makes the edit in one Sonnet session
  with `verify.fast`, then runs the tail. Its scoping agent refuses a change that needs a design
  decision, touches the gate, auth or the audit log, meets the ADR bar, adds a connector or bumps a
  dependency, and says to use `devflow-ship`.
- **devflow-docs** has an Opus reviewer apply the `docs-review` checklist and measure (website build,
  documented commands, links, changelog coverage). The fixes become `work.md`; what is a product or
  wording decision goes to the PR's "Needs a person" section instead of into edits.
- **devflow-audit** runs an auditor, then a fresh verifier that tries to disprove every finding and
  decides fix, not doing or needs a decision, then includes `devflow-ship` with the surviving findings
  as the work. `SECURITY.md` forbids public issues for vulnerabilities, so the findings never leave the
  run's artifacts: no issue, and the PR names the class of fix, not how to attack it.
- **devflow-release** reads facts at `origin/main`'s tip, opens a changelog PR when a stable version has
  no section (and waits for you to merge it), asks you to confirm the facts, dispatches `build.yml`
  against that exact commit and requires every release job green, runs `scripts/tag_release.py` in a
  fresh clone of `main` as a dry run (all its checks, no push), and only after your approval runs it
  again with `--push` for the same commit. It does not dispatch `release.yml`. It then reports the tag
  and the `build.yml` and `publish-pypi.yml` runs, and fails loudly if the tag was pushed but no build
  started. `--input version=7.1.0` is required and never inferred. The `release tags` ruleset refuses
  `v*` tags to anyone without a bypass, so the credential Archon runs with must be able to push them;
  the confirmation shows whether the token holds admin. If the pre-flight fails, fix `main` and start a
  new run: a resumed run would reuse the old commit.
- **devflow-config** snapshots a repo's configuration through the GitHub API (read-only; each endpoint
  records whether this token could read it, and a 404 for a non-admin token counts as unknown, not
  off), then an Opus reviewer compares it with the repo's documented intent, its workflow files and,
  if `--input reference=owner/name` is given, a repository whose setup you want to copy
  (`privacyfence/privacyfence` is the model). Settings changes go through an allow-list that refuses
  visibility, name, archiving, secrets, webhooks, deploy keys and removing an admin, then you choose
  which to apply; each call records the before and after in `config-changes.md`. Without admin the run
  ends with the review as a report. File changes (workflow files, `dependabot.yml`, `CODEOWNERS`) go
  through a PR and the shared tail. It reviews the repository of the project it runs in.
- **devflow-deps** reads the open Dependabot PRs and alerts, has Opus review each PR as a supply-chain
  change (version jump, new transitive packages, install scripts, release notes), then repairs the
  chosen PRs one at a time on their own branches: checks run, locks regenerated with the repo's tool,
  fixes pushed to the Dependabot branch, CI read back, at most two attempts each. Alerts no PR covers
  are fixed on a new branch through the shared tail. It never merges and never dismisses an alert.
  Needs `node`/`npm` and `uv` on the host.

Run any of them with `archon workflow run <name> "<the work>" --detach`
(`--input version=...` for the release, `--input scope=...` for docs and audit,
`--input reference=...` for config).
