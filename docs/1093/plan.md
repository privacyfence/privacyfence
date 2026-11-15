# Retire release.yml, scope build.yml permissions, make the admin policy the one source for rulesets, finish the move to tld

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** To cut a release, the maintainer (or `/cut-release`) dispatches `tld-release.yml` on `main` with an explicit `version`; the dry run is the default, the owner approves the `tld-release` environment, and the run shows every check plus the `CHANGELOG.md` body that will ship. A real cut from `/cut-release` also waits for the owner's yes on the board. `release.yml` is gone. Required checks and merge rules are edited in `.tld/admin-policy.yaml` and applied with `tld ops repo-admin apply`; `scripts/update_branch_protection.py` is gone, and so is the `releases/*` integration-branch convention. Plans live at `docs/<issue>/plan.md` and stay on `main`.

**Scope:** `build.yml` gets a top-level `permissions: contents: read`. A new `scripts/release_check.py` runs the old pre-flight checks as `tld-release.yml`'s `check_script`; `tag_release.py` gains `--check-only`. The admin policy lists all ten required checks and sets merge-commit-only; a new test keeps that list in line with the workflows. The `releases/**` triggers and docs go; the QA recording workflow keeps refusing such branches. The release docs, `/cut-release`, `CLAUDE.md`, `CONTRIBUTING.md`, §2.7, the PR template, the steward skill and the profile describe tld, not devflow. Five ADRs are added. Unchanged: what `build.yml`, `publish-pypi.yml` and the other tag-triggered workflows do, the version scheme, every secret.

**Security:** The tag is created through the REST API with `TLD_RELEASE_TOKEN`, an owner token in the owner-approved `tld-release` environment; the check script runs in a clone with no token. Four overlapping branch and tag rulesets guard the repository today, and the old `main` ruleset has no bypass actor, which cancels the policy's `admin_bypass`. The policy's `tld-main` ruleset tightens review (one approval, stale approvals dismissed, last push approved by someone else) and loosens two settings (up-to-date branches, code-owner review); the owner deletes the duplicates by hand after applying it (§3.6). No secret is created, read or moved by any phase.

**Review focus:**
- §3.6: replacing four live rulesets with `tld-main` and `tld-tags`, with the tightened and loosened settings.
- §3.2: `release_check.py` runs `tag_release.py --no-fetch --check-only` in tld-release's token-less clone.
- §3.3 and §3.5: the post-tag "did `build.yml` start" poll moves to `/cut-release` and the docs, and a real run needs the board's yes.
- §3.8: which protected-path entries the last phase removes.

## 1. Goal

Issue: https://github.com/privacyfence/privacyfence/issues/1093.

The owner asked for three things in the issue: a top-level `permissions:` block in `build.yml`, the leftover devflow / claude-toolkit wording cleaned up, and `tld-release.yml` replacing `release.yml` as the only way to cut a release with the old pre-flight checks kept. On 2026-11-15 the owner added: since the plan was first written the desktop apps were dropped, the repository must be reviewed in detail, branch protection moved to the repository admin policy (`.tld/admin-policy.yaml`), and the duplicate rulesets reviewed. The owner's answers: retire `scripts/update_branch_protection.py` (the policy's `required_status_checks` becomes the one reviewed list, all ten checks, and the owner deletes the old `main` ruleset by hand); drop the `releases/*` integration-branch convention, its CI triggers and its ruleset. Earlier answer (`adopt_tld`): plans stay at `docs/<n>/plan.md` with `deviations.md` on `main`, `plan_dir` stays `docs/`.

## 2. Current state

Measured on `main` at 3563d2538 (the plan branch is merged with it).

- **Already done on `main`, so not planned again:**
  - The docs tests skip tld plan records: `tests/unit/test_docs_links.py:62` and `tests/unit/test_docs_references_exist.py:97-101` treat `docs/<digits>/plan.md` as a plan document, and `docs/1095`, `docs/1096` and others are on `main`.
  - `hooks.protect_paths_allow` in `.tld/toolkit.yaml:129-141` holds twelve entries, among them `build.yml`, `release.yml`, `tests.yml`, `website-build.yml`, `dependency-audit.yml` and `.tld/toolkit.yaml`. It does not hold `qa-record-fixture.yml`.
  - The desktop drop (ADR 0301, ADR 0309): `docs/downloads-and-release-kpi.md` is deleted; `build.yml` has four jobs (`dedupe`, `build-org-image`, `sbom`, `finalize-release`); `docs/releasing.md:63-64` still says "confirm `build`, `build-org-image` ...", and no job called `build` exists any more.
  - ADRs go up to 0309; the next free number is 0310.
- **`build.yml`** has `on:` at `:3` and `jobs:` at `:8` with no top-level `permissions:`. The jobs declare their own: `dedupe` (`:14`, `contents: read`, `actions: write`), `build-org-image` (`:68`, `contents: read`), `sbom` (`:208`, `contents: read`), `finalize-release` (`:285`, `contents: write`). It is the only workflow without a top-level block.
- **`release.yml`** ("Cut release tag", environment `release`, secret `RELEASE_TAG_TOKEN`) runs `scripts/r2_release.py channel`, `scripts/changelog_section.py` (stable only), `scripts/tag_release.py [--push]`, and after a real push polls two minutes for a `build.yml` run.
- **`tld-release.yml`** (rendered by `tld ops repo-admin` from `.tld/admin-policy.yaml`, `workflows: - template: release`, params `tag_prefix: "v"`, `version_style: pep440`, `create_release: "false"`): decides the tag, checks that the commit is green (every check run on `github.sha` completed and not failed; the legacy status not failure or error), runs `CHECK_SCRIPT` (`:145`, now `""`, which makes the step do nothing; otherwise it fetches every tag and `main` into a fresh clone, checks out `main` at `github.sha` with upstream `origin/main`, unsets the token and runs `python3 <script> <version>`), shows GitHub's generated notes, and creates an annotated tag through the REST API with `TLD_RELEASE_TOKEN`. Job environment `tld-release`; only `refs/heads/main` (`:55-56`). Rendering the template with `check_script: scripts/release_check.py` changes only `:145` (checked against `tld`'s `render_template` when the plan was first written).
- **Who depends on `release.yml`** (`grep -rn 'release\.yml'`, excluding `tld-release.yml`, `CHANGELOG.md`, ADRs and plans): `docs/releasing.md:22,25,32,46,52-53,62,69,74`; `docs/release-testing.md:40,100`; `.claude/commands/cut-release.md:12,30,43,50`; `.claude/skills/steward/SKILL.md:69`; `CLAUDE.md:28`; `.tld/toolkit.yaml:112` (`ci.dispatchable`) and `:131` (the allow list); `scripts/tag_release.py:4,36,49` (docstring). Frozen ADR bodies (0021, 0022, 0023, 0030, 0111, 0185) also name it: only Status lines may change. No test reads `release.yml`.
- **`scripts/tag_release.py`:** `main()` at `:261` always creates a local tag (`create_tag`, `:289`) and prints a push hint; `--no-fetch` exists (`:267`), `--push` at `:271`. `tests/unit/test_tag_release.py` has `class TestMain` at `:220` and uses real temporary git repositories.
- **Rulesets.** The owner pasted the five live rulesets on 2026-11-15 (the owner reported no classic branch protection rule; confirm that at https://github.com/privacyfence/privacyfence/settings/branches in step 2 of ma2). Compared with what `.tld/admin-policy.yaml` and `tld ops repo-admin` produce:

  | Live ruleset | Target | Rules | Bypass | Verdict |
  |---|---|---|---|---|
  | `tld-tags` (27271543) | every tag (`~ALL`) | creation, update, deletion | admin role, always | Policy-managed (`rulesets.tags: true`). Keep. |
  | `release tags` (25164351) | `refs/tags/v*` | creation, update, deletion, non_fast_forward | admin role, always | Duplicate of `tld-tags` for `v*` tags. Delete. |
  | `main` (22647766) | `refs/heads/main` | 9 required checks with `strict`, PR (0 approvals, code-owner review, merge commits only), deletion, non_fast_forward | none | Written by `update_branch_protection.py`. Delete after applying the policy. |
  | `default-branch` (26375620) | `~DEFAULT_BRANCH` | the same 9 checks, not strict, PR (0 approvals, code-owner review, all merge methods), deletion, non_fast_forward | admin role, pull_request | Overlaps `main` on the same branch. Delete after applying the policy. |
  | `releases/**` (23724726) | `refs/heads/releases/**` | 8 checks with `strict`, PR, non_fast_forward | none | For the convention the owner dropped. Delete. |
  | `tld-main` | `~DEFAULT_BRANCH` | | | Not among the pasted rulesets, so `repo-admin apply` has not created it (or it was deleted). |

  Consequences: `main` and `default-branch` guard the same branch, and `main` has no bypass actor, so it cancels the policy's `admin_bypass: true` (a bypass actor in one ruleset does not bypass another); the live lists lack `n8n-node`, which the script lists (applying it was a manual step that never ran); the policy's own list has 5 of the 10 checks.
- **`scripts/update_branch_protection.py`** writes `REQUIRED_STATUS_CHECKS` (10 entries, `:96-107`) into the ruleset named by `--branch` (`main` or `releases/**`). `tests/unit/test_update_branch_protection.py` tests it and, in `TestRequiredChecksList` (`:353-395`), also keeps the list equal to the jobs of `tests.yml` and `website-build.yml`. `tests/unit/test_definition_of_done_drift.py:72-87` imports the list to check that `docs/testing-policy.md` names every check. Mentioned in `docs/testing-policy.md:86-92`, `CONTRIBUTING.md:42`, `.github/workflows/tests.yml:276` and `.github/workflows/website-build.yml:30` (comments).
- **`releases/**` convention:** `CONTRIBUTING.md:32-47` (a subsection), `CLAUDE.md:6`, `.claude/skills/steward/SKILL.md:66`, `docs/release-testing.md:25`, `docs/testing-policy.md:51,91,140,151,228`, `docs/connector-qa.md:645`, `.claude/commands/qa-record.md:26`; triggers `branches: [main, "releases/**"]` in `tests.yml:8`, `website-build.yml:18`, `dependency-audit.yml:47`, with comments at `tests.yml:5-7`, `website-build.yml:3-4`, `dependency-audit.yml:45-46`; and a refusal of `releases/*` branches in `qa-record-fixture.yml:99-108`. No `releases/*` branch exists on origin.
- **devflow wording:** `.tld/toolkit.yaml:1,3`; `CLAUDE.md:18-21` (the whole "Commands and skills" section describes devflow's `plan/` branches and deleted plans); `.claude/skills/steward/SKILL.md:3,9`; `docs/testing-policy.md:272`; ADR index row 0132 (`docs/adr/README.md:232`). `.tld/admin-policy.yaml:1` names the repository `tkcs-lab/tkcs-lab-devflow`, a real name that stays.
- **Plan lifecycle text that tld made untrue:** `CONTRIBUTING.md:16-18,54-55,62-65,68-70`; `docs/adr/README.md:8,15,44-48`; `docs/coding-and-testing-guidelines.md:350-352` (§2.7); `.github/pull_request_template.md:27-29`; `.tld/toolkit.yaml` `verify.dod_manual` row 2.
- **Protected paths.** The tld `protect-paths` hook blocks `.github/workflows/**` and `.tld/toolkit.yaml` by default; `hooks.protect_paths_allow` is read only from `main`. `.tld/admin-policy.yaml` and the docs are not protected. `tld-release.yml` is writable only between `tld guard allow-workflow-install tld-release.yml` and `tld guard end-allow`. A session may dispatch `tld-release.yml` with `dry_run` true; a real run needs `tld guard allow-release tld-release.yml`.
- **Status of the first run.** The implement run of this plan's earlier version is parked on manual step mb2 and started no phase (`tld plan phase-status` on the feature branch: `started: false`), so this revision replaces the plan in place.

## 3. Design

### 3.1 build.yml

Insert these lines between the `on:` block (it ends with `workflow_dispatch:   # allow manual runs from the Actions tab`) and `jobs:`, one blank line before and after:

```yaml
# Read-only by default; each job below declares exactly what it needs on top of this
# (dedupe: actions: write; finalize-release: contents: write).
permissions:
  contents: read
```

Nothing else in `build.yml` changes: every job already sets its own block, so behaviour is identical.

### 3.2 scripts/release_check.py

Stdlib only; it runs under whatever `python3` the runner has, because tld-release's check step has no setup-python and no `pip install`. The module docstring says so, says the script is `tld-release.yml`'s `check_script`, and cites ADR 0310.

```python
SCRIPTS = Path(__file__).resolve().parent

def run_step(name: str, argv: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]
    # subprocess.run([sys.executable, *argv], text=True, capture_output=capture, check=False)
    # argv[0] is a path under SCRIPTS. Prints "==> <name>" to stdout before running.

def write_summary(version: str, notes: str) -> None
    # If GITHUB_STEP_SUMMARY is set and non-empty, append:
    #   "### Release notes that will ship for v<version>\n\n<notes>\n"
    # Otherwise do nothing.

def main(argv: list[str] | None = None) -> int
```

`main` takes one positional argument, `version` (help: `Version to check, e.g. 7.1.0 or 7.1.0a1 (no leading v)`). Steps, in order; the first failure stops the script:

1. **Release channel**, `[SCRIPTS / "r2_release.py", "channel", version]`, captured. On a non-zero exit print its stderr, then `release check failed: release channel (exit <n>)` to stderr and return `<n>`. On success `channel = stdout.strip()`, printed as `channel: <channel>`.
2. **Release notes**, only when `channel == "stable"`: `[SCRIPTS / "changelog_section.py", version]`, captured. A failure is handled like step 1 with the step name `release notes`. On success print the notes and call `write_summary(version, notes)`. Never pass `--allow-unreleased`.
3. **Tag checks**, `[SCRIPTS / "tag_release.py", version, "--no-fetch", "--check-only"]`, not captured so its output streams. A failure is handled like step 1 with the step name `tag checks`.
4. Print `release check passed for v<version> (<channel>)` and return 0.

The script passes `--no-fetch` because tld-release has just fetched `origin/main` into the clone and the clone holds no credentials; `tag_release.py`'s "local main is origin/main" check then compares `github.sha` with what was fetched, so a `main` that moved after the dispatch is refused. Rejected: letting `tag_release.py` fetch (needs network access to the remote from a token-less clone and adds nothing). Rejected: checking for a successful `build.yml` pre-flight run on the SHA through the Actions API (the script has no token, and an unauthenticated API call adds rate-limit flakiness to an irreversible step); the pre-flight stays a documented `/cut-release` step (§3.5).

**`tests/unit/test_release_check.py`** imports the script by file path, the way `tests/unit/test_tag_release.py:22-26` does, and fakes `subprocess.run` with `monkeypatch`, recording argv. Tests:
- `test_stable_runs_channel_notes_and_tag_checks_in_order`
- `test_prerelease_skips_the_release_notes_step`
- `test_tag_checks_run_with_no_fetch_and_check_only`
- `test_a_failing_channel_step_stops_everything` (returns that exit code; no later argv recorded)
- `test_a_failing_notes_step_stops_before_the_tag_checks`
- `test_a_failing_tag_check_returns_its_exit_code`
- `test_notes_are_appended_to_the_step_summary` (a `tmp_path` file, `GITHUB_STEP_SUMMARY` set with `monkeypatch.setenv`)
- `test_no_summary_is_written_when_the_variable_is_unset`
- `test_never_passes_allow_unreleased`
- `test_an_invalid_version_fails_at_the_channel_step` (no fake; the real `r2_release.py channel nope` exits 1)

### 3.3 scripts/tag_release.py `--check-only`

- Add `--check-only` (help: `Run every check but create no tag -- what scripts/release_check.py runs inside tld-release.yml`) in an `argparse` mutually exclusive group with `--push`.
- In `main()`, right after `check_sequential(...)` succeeds and when `args.check_only` is set: print `checks passed for <name> at <short head>; no tag created (--check-only)` and return 0, without calling `create_tag`.
- Docstring edits:
  - Lines 4-5: "which `release.yml` does by running this script (docs/releasing.md, ADR 0185)" becomes "which `.github/workflows/tld-release.yml` does after running these checks through `scripts/release_check.py` (docs/releasing.md, ADR 0310)".
  - Lines 35-37: "`release.yml` is the only documented way to cut a release (ADR 0185); running this by hand is for checking a version number." becomes "`tld-release.yml` is the only documented way to cut a release (ADR 0310) and creates the tag itself; running this by hand is for checking a version number."
  - The `--push` example (`:48-49`): "(what `release.yml` runs)" becomes "(a by-hand cut; not the procedure)".
  - Add an example: `python3 scripts/tag_release.py 4.2.0 --no-fetch --check-only` with the result line `-> checks pass -> no tag (what scripts/release_check.py runs)`.
- New tests in `tests/unit/test_tag_release.py`, in `TestMain` next to `test_creates_local_tag_without_pushing`: `test_check_only_creates_no_tag`; `test_check_only_and_push_are_mutually_exclusive` (SystemExit with code 2); `test_check_only_still_fails_a_sequential_gap`.

**Rejected:** a post-tag "did `build.yml` start" poll in `tld-release.yml`. `tld-release.yml` is a tld template, and an edit beyond its params shows as drift in every `repo-admin` audit. What replaces it: `/cut-release` and `docs/releasing.md` tell whoever cuts to confirm a `push` run of `build.yml` and of `publish-pypi.yml` for the tagged commit within a few minutes. Why the risk stays low: `TLD_RELEASE_TOKEN` is an owner token, not `GITHUB_TOKEN`, so the tag starts both workflows (ADR 0021's invariant).

### 3.4 Workflow and profile files

- Delete `.github/workflows/release.yml` (`git rm`).
- In `.github/workflows/tld-release.yml` change `:145` from `          CHECK_SCRIPT: ""` to `          CHECK_SCRIPT: "scripts/release_check.py"`; nothing else. The worker runs `tld guard allow-workflow-install tld-release.yml` right before the edit and `tld guard end-allow` right after.
- `.tld/admin-policy.yaml` release params gain `check_script` (§3.6 lists all admin-policy edits, which one phase makes).
- `.tld/toolkit.yaml` `ci.dispatchable`: the `release.yml` row becomes

  ```yaml
      - workflow: tld-release.yml
        use_when: Cutting a release tag (never for real from a session without the maintainer's say-so)
        notes: >-
          Inputs `version` (always explicit; never leave it empty), `dry_run` (default true); never pass `skip_checks`.
          Runs on `main` only; the owner approves the tld-release environment for every run. A real run from a
          session needs the owner's yes on the tld board, then `tld guard allow-release tld-release.yml` first and
          `tld guard end-allow` after. Dispatch the dry run, report it, wait for the yes. /cut-release is the procedure.
  ```

- No `release:` section is added to `.tld/toolkit.yaml`: its `version_files` key needs a file that holds the version, and this repository takes it from `setuptools_scm` tags. So `/tld:release` cannot serve this repository and `/cut-release` stays. ADR 0310 records this.

### 3.5 The release docs

- **`docs/releasing.md`, "Cutting a release"** (`:20-75`). Rewrite the paragraphs that name `release.yml`, keeping the heading and the anchor `#cutting-a-release` (`CLAUDE.md` links it). The new text states:
  1. Cutting a release is a tag, not a commit: dispatch `tld-release.yml` on `main` with `version` (PEP 440 short form, always explicit) and `dry_run` (default true). Never pass `skip_checks`. `prerelease` has no effect, because `create_release` is false and `build.yml` decides the channel from the tag.
  2. `tld-release.yml` is the only way to cut a release. It runs in the `tld-release` environment, which admits only `main`, and the owner approves every run. It creates an annotated tag through the REST API with `TLD_RELEASE_TOKEN`, an owner token stored as that environment's secret. The `tld-tags` ruleset (`rulesets.tags` in `.tld/admin-policy.yaml`) lets no workflow token create a tag. A person with the admin role could still push a tag by hand; that is not the procedure. Name ADR 0310 in plain text (the file arrives in the last phases).
  3. The steps the run takes, in order: decide the tag; check that the commit is green (every check run on `main`'s tip must be finished and green, so a red, queued or still-running run blocks, including a `build.yml` pre-flight that is still running, a scheduled run, and the self-hosted `deploy-qa-server` job when that runner is offline); `scripts/release_check.py`, which runs `r2_release.py channel`, `changelog_section.py` (stable only, with the body that will ship written to the run summary) and `tag_release.py --no-fetch --check-only`; show GitHub's generated notes, which do not ship; on a real run, create the tag.
  4. `TLD_RELEASE_TOKEN` must not be `GITHUB_TOKEN`. Keep the recursion-guard explanation and the ADR 0021 link. The workflow does not check afterwards that `build.yml` started, so whoever cuts confirms a `push` run of `build.yml` and `publish-pypi.yml` for the tagged commit. If neither appears, the tag exists and nothing started: the owner deletes the tag and cuts again.
  5. If `main` moves while the run waits for approval, `tag_release.py` refuses (local `main` is not `origin/main`). Dispatch again.
  6. Pre-flight: keep the paragraph and the ADR 0030 link, name `tld-release.yml` and its dry run instead of `release.yml`, and fix the job list to "confirm `build-org-image` (both architectures) and `sbom` all succeed" (there is no `build` job any more). Add one sentence: the green check refuses while the pre-flight is running or red, but cannot notice a pre-flight that was never dispatched.
  7. Relay check: "before dispatching `tld-release.yml`".
- **`docs/release-testing.md`.**
  - Gate 1 (`:25`): "`origin/main`'s tip (or the `releases/*` branch being merged)" becomes "`origin/main`'s tip".
  - Gate 5 (`:40-42`) becomes: "**`tld-release.yml` dry run.** Dispatch it on `main` with `version` and `dry_run: true` (the default) and approve the `tld-release` environment. It checks that every check on the commit is green, runs `scripts/release_check.py` (the channel, the stable release notes, `tag_release.py`'s checks without creating a tag) and shows the notes. It builds and tags nothing."
  - Gate 6 (`:43-44`) becomes: "**Cut.** Re-dispatch with `dry_run: false` and approve again. Then confirm that `build.yml` and `publish-pypi.yml` started for the tagged commit; the workflow does not check this itself."
  - Evidence item 1 (`:100`): "the commit, the `build.yml` pre-flight run and the `tld-release.yml` dry-run URL;".
- **`.claude/commands/cut-release.md`.** Same front matter with the description `Pre-flight and cut a release tag through tld-release.yml`. The body:
  1. Keep "If no version was given above, stop and ask", with its reason.
  2. "This container cannot push `refs/tags/*`, so the tag is cut by `.github/workflows/tld-release.yml`." Keep the warning not to tag by hand.
  3. Keep the four "report the state" bullets unchanged.
  4. Keep the `build.yml` pre-flight section and the relay check, naming `tld-release.yml` where they named `release.yml`; add that the pre-flight must have finished, because tld-release's green check refuses a commit with a running or failed check run.
  5. **Dry run.** Once the pre-flight is green, dispatch `tld-release.yml` on `main` with `version: <version>` and `dry_run: "true"`. Never leave `version` empty and never pass `skip_checks`. The owner approves the `tld-release` environment in GitHub before the job starts. Report the run URL. For a stable version, report the notes from the "Run the project's release check" step's summary; the "Show the release notes" step lists GitHub's generated notes, which do not ship. If "Check that the commit is green" fails, list the check runs it names and stop: a red or queued run on `main`'s tip is fixed or re-run, never skipped. If `tag_release.py` says local `main` is not `origin/main`, `main` moved while the run waited for approval: report it and dispatch the dry run again.
  6. **Real run.** Only after the owner's yes on the tld board, which tld's pr-steward policy requires for any real release dispatch from a session. Ask with `tld board ask` (the session's tld runtime says how to post and wait), one `choice` question, id `cut`, titled `Release <version>: cut for real?`, its text the dry-run URL and the channel, options `real` (recommended) and `stop`. A yes typed in chat alone is not enough. On `real`: run `tld guard allow-release tld-release.yml`, dispatch with `dry_run: "false"`, then always run `tld guard end-allow`, also when the dispatch failed. The owner approves the environment again. On `stop`: report that nothing was cut.
  7. **After a real cut.** List `build.yml` and `publish-pypi.yml` runs. A `push` run on the tagged commit must appear for each within 5 minutes. If none does, tell the maintainer at once: the tag exists and the release did not start. Never dispatch again for the same version. Report the tag and both run URLs.

### 3.6 Rulesets come from the admin policy

The decision (owner, 2026-11-15): `.tld/admin-policy.yaml` is the one place the required checks and merge rules are written. `tld ops repo-admin apply` owns two rulesets by name, `tld-main` and `tld-tags`, and leaves every other ruleset alone, so the duplicates in §2 stay until the owner deletes them.

**Edits to `.tld/admin-policy.yaml`** (one phase makes them all):

- `rulesets.default_branch.required_status_checks` becomes these ten names, in this order (the script's order):

  ```yaml
      required_status_checks:       # exact GitHub check names; every job runs on every PR (tests.yml has no path filters)
        - test
        - "Test (Python 3.11, core suite)"
        - "Test (Python 3.12, core suite)"
        - "Test (Python 3.14, core suite)"
        - static-analysis
        - org-mode-smoke
        - "Sync client (Python 3.11)"
        - "Sync client (Python 3.13)"
        - website-build
        - n8n-node
  ```

- `repo_settings` gains `allow_merge_commit: true`, `allow_squash_merge: false` and `allow_rebase_merge: false`. The live `main` ruleset allowed merge commits only, and `CONTRIBUTING.md` and the steward skill say PRs merge with a real merge commit; `tld-main` has no merge-method rule, so the repository settings keep the property.
- The release workflow's params become `{ tag_prefix: "v", version_style: pep440, create_release: "false", check_script: scripts/release_check.py }`.

**What `tld-main` changes against the live rulesets** (accepted, recorded in ADR 0313): one approval is required, stale approvals are dismissed and the last push must be approved by someone else (live: 0 approvals, code-owner review required); `strict_required_status_checks_policy` is false (live `main`: true, so branches had to be up to date); the code-owner review requirement goes (`.github/CODEOWNERS` stays and still requests the owner's review); the admin role can bypass through a pull request (live `main`: nobody could). Because the agent never is a bypass actor and the owner is the only approver, the day-to-day flow is the same. Rejected: keeping the script to hold `strict: true` (it needs an owner token with ruleset admin rights and a second source of truth for the same list).

**The script and its tests.**
- Delete `scripts/update_branch_protection.py` and `tests/unit/test_update_branch_protection.py`.
- New `tests/unit/test_required_checks.py` (module docstring: the policy's check list must equal what the workflows report; a listed check that never reports blocks every merge). It reads `.tld/admin-policy.yaml` with `yaml.safe_load` (`pyyaml` is a runtime dependency) and has these tests, ported from `TestRequiredChecksList` of the deleted file:
  - `test_the_list_has_no_duplicates`
  - `test_plain_jobs_exist_in_tests_yml`: `test`, `static-analysis`, `org-mode-smoke` and `n8n-node` are job names or `name:` values in `tests.yml`
  - `test_python_matrix_legs_are_required`: `Test (Python {v}, core suite)` for every `v` in `jobs.test-python-compat.strategy.matrix.python-version`, and no other check starting with `Test (Python `
  - `test_sync_client_matrix_legs_are_required`: the same for `Sync client (Python {v})` and `jobs.sync`
  - `test_website_build_is_required_and_runs_on_every_pull_request`: the assertions of the deleted test of that name, against `website-build.yml` (the workflow's `on` key is read as `True` by PyYAML)
- `tests/unit/test_definition_of_done_drift.py`: `_required_status_checks()` (`:72-77`) reads the policy list instead of importing the script, and the failure message at `:85` says `the required checks` instead of `REQUIRED_STATUS_CHECKS`.
- Comment edits: in `tests.yml:276-277` the words "scripts/update_branch_protection.py's\n  # REQUIRED_STATUS_CHECKS: it needs" become "the required checks of .tld/admin-policy.yaml:\n  # it needs" (the sentence then reads "Deliberately not in the required checks of .tld/admin-policy.yaml: it needs a Docker daemon ..."); `website-build.yml:30` "so what scripts/update_branch_protection.py requires" becomes "so what .tld/admin-policy.yaml requires".
- `docs/testing-policy.md:86-92`, the "Required checks" paragraph, becomes: "**Required checks.** `rulesets.default_branch.required_status_checks` in `.tld/admin-policy.yaml` is the reviewed list: `test`, the three `Test (Python 3.x, core suite)` checks, `static-analysis`, `org-mode-smoke`, the two `Sync client (Python 3.x)` checks, `website-build` (`website-build.yml`'s job, which also runs on every PR) and `n8n-node`. Update it in the PR that adds, renames or removes one of those jobs; `tests/unit/test_required_checks.py` fails when the list and the workflows disagree. The owner applies it to the live `tld-main` ruleset with `tld ops repo-admin apply` after the PR merges; no CI job does it, and `tld ops repo-admin audit` shows what differs." Also `:272`: "- **`/dod`** (from the devflow toolkit; its rows are `verify` in `.tld/toolkit.yaml`)" becomes "- **`/tld:dod`** (from the tld plugin; its rows are `verify` in `.tld/toolkit.yaml`)".

**Live changes (manual, after the merge, §5).** The owner applies the policy first, so `tld-main` exists with all ten checks, then deletes `main`, `default-branch`, `release tags` and `releases/**` by hand. Deleting before applying would leave the default branch unprotected for the gap.

### 3.7 The `releases/*` convention is dropped

Remove, in two phases (docs, then workflows):
- `CONTRIBUTING.md`: the subsection `### Release integration branches (`releases/*`)` (`:32-47`, up to the blank line before `## Decisions, plans and ADRs`).
- `CLAUDE.md:6`: "branch naming, pull requests, `releases/*` integration\n  branches, and the plan/ADR lifecycle." becomes "branch naming, pull requests, and the plan/ADR lifecycle."
- `.claude/skills/steward/SKILL.md:66`: the one-line bullet "**`releases/*` is protected like `main`.** If work is targeting one, branch from it and PR back into it." (line 67 is a different bullet and stays).
- `docs/testing-policy.md`: `:51` "every push to `main` or `releases/**`" becomes "every push to `main`"; `:140` "a push to `main`/`releases/**`" becomes "a push to `main`"; `:151` "every push to `main`/`releases/**`" becomes "every push to `main`". Line 228 (the QA workflow's refusal list) stays.
- `.github/workflows/tests.yml:5-8`: the three comment lines go and the trigger becomes `branches: [main]`. `website-build.yml`: header lines 3-4 become `# Builds privacyfence.eu with scripts/build_site.py on every pull request and on pushes to main,` and `# without deploying anything (pages.yml deploys, from main only). It proves both ways`, and `:18` becomes `branches: [main]`. `dependency-audit.yml:45-47`: the two comment lines go, `branches: [main]`.
- `.github/workflows/qa-record-fixture.yml:99`: only the first comment line changes, from "# releases/* is protected the same as main (see CONTRIBUTING.md), and so is stable --" to "# main and stable are protected, and releases/* stays refused (ADR 0181) though no such branch is used any more --". The guard itself keeps refusing `releases/*`: it is a deny list, ADR 0181 names the refusal, `docs/connector-qa.md:645`, `.claude/commands/qa-record.md:26` and `docs/testing-policy.md:228` describe it, and a refusal of a branch nobody uses costs nothing. `pages.yml` is untouched: it never had the pattern.

Rejected: keeping the convention with a hand-made `releases/**` ruleset when one is cut. No such branch has existed in this repository's history that the owner relies on, the live ruleset is stale (8 checks), and the second ruleset was the main reason the script took a `--branch` argument.

### 3.8 tld wording, the lifecycle docs and the protected-path lock

- **`.tld/toolkit.yaml:1-3`** becomes:

  ```yaml
  # tld project profile (the tld plugin's profile reference documents every key).
  # verify.* mirrors docs/coding-and-testing-guidelines.md §2.7: change both together.
  # The tld plugin comes from the Claude Project's plugins, not from a SessionStart hook.
  ```

- **Plan lifecycle.** One sentence, used by every doc below: *a plan is a GitHub issue, or `docs/<issue>/plan.md` with `docs/<issue>/deviations.md` written on the feature branch; it reaches `main` with the feature PR and stays there as the record of why the change was made, and nobody edits it after that; its decisions become ADRs before it merges.*
  - **`CONTRIBUTING.md:16-18`:** "`plan/` branches hold a plan document while its work is open (see [Decisions, plans and ADRs](#decisions-plans-and-adrs)) and are never PR'd themselves." becomes "A plan is written on its feature branch as `docs/<issue>/plan.md` (see [Decisions, plans and ADRs](#decisions-plans-and-adrs)) and reaches `main` with that branch's PR."
  - **`CONTRIBUTING.md:54-55`** (Plans bullet): "**Plans** (what we are about to do, and in what order) are a GitHub issue, or `docs/<issue>/plan.md` with its `deviations.md` (what the implementation did differently). They are written on the feature branch, reach `main` with the feature PR, and stay there as the record of why the change was made; they are not edited after that."
  - **`CONTRIBUTING.md:62-65`:** "**A plan's decisions become ADRs before it merges.** The PR that brings a plan to `main` adds or amends an ADR for every decision the plan made — anything hard to reverse, touching a trust boundary or the release/distribution path, or rejecting an alternative for a non-obvious reason — or says in its description that the plan made none."
  - **`CONTRIBUTING.md:68-70`:** replace only the sentence "ADRs link to issues, PRs, commits and\n  source files, never to a plan document, which will not outlive it." with "ADRs link to issues, PRs, commits and source files. A plan under `docs/<issue>/` may be cited as the record of a change, but an ADR states its decision in full and never relies on the plan to explain it." The sentence before it ("if it meets the bar above, it gets an ADR in the same PR.") stays.
  - **`docs/adr/README.md`:** line 8 "Plans come and go" becomes "Plans record one change as it was planned"; the Plan table row: Home "A GitHub issue, or `docs/<issue>/plan.md` with `deviations.md`, written on the feature branch", Lifecycle "Kept. Reaches `main` with the feature PR and stays as the record of the change; not edited after that. Its decisions are extracted here **before** it merges"; rule 4 "**Link only to things that last.** Issues, PRs, commit SHAs, source files, other ADRs, and a plan under `docs/<issue>/`. A top-level `docs/*-plan.md` from before plans were kept was deleted when its work landed: to cite one, give the commit that deleted it and the path, e.g. `git show 96cd5af4^:docs/https-connector-refactor-plan.md`."; rule 5 "**A plan reaching `main` is gated on this directory.** The PR that brings a plan to `main` either adds or amends the ADRs for every decision the plan contained, or states in its description that the plan contained none." (the rest of rule 5 is unchanged).
  - **§2.7** (`docs/coding-and-testing-guidelines.md:350-352`), **PR template** (`.github/pull_request_template.md:27-29`) and **`.tld/toolkit.yaml` `dod_manual` row 2:** replace "A PR that deletes a plan document extracts that plan's decisions into ADRs first" (toolkit: "A deleted plan document needs its decisions extracted into ADRs first") with "A PR that brings a plan document (`docs/<issue>/plan.md`) to `main` extracts that plan's decisions into ADRs first" (toolkit: "A plan document reaching `main` (`docs/<issue>/plan.md`) needs its decisions extracted into ADRs first"). The rest of each row is unchanged.
- **`CLAUDE.md`, "## Commands and skills".** Replace the whole section, from its heading up to "## Parallel sessions & worktrees", with:

  ```markdown
  ## Commands and skills

  The workflow commands (`/tld:make-plan`, `/tld:implement`, `/tld:dod`, `/tld:review` and the rest) come
  from the tld plugin, enabled in the Claude Project; the plugin's own playbooks say what each one does. The
  project facts they read are in `.tld/toolkit.yaml`; ADR 0311 says why.

  - `.claude/skills/steward/SKILL.md` — PrivacyFence-specific steward policy, on top of the plugin's
    pr-steward policy. The workflows a session dispatches instead of running locally are
    `ci.dispatchable` in `.tld/toolkit.yaml`.
  - `/cut-release` (`.claude/commands/cut-release.md`) runs the `build.yml` pre-flight that
    [`docs/releasing.md`](docs/releasing.md#cutting-a-release) requires, then dispatches
    `tld-release.yml`.
  - `/qa-record` records a connector's live QA fixture on the self-hosted runner.
  - **Plans.** `/tld:make-plan` writes a plan to `docs/<issue>/plan.md` on its feature branch, and
    `/tld:implement` adds `docs/<issue>/deviations.md` and opens one PR to `main`, where both stay as the
    record of the change (ADR 0312; the lifecycle is in [`CONTRIBUTING.md`](CONTRIBUTING.md#decisions-plans-and-adrs)).
    Steps only the user can do (third-party console setup, secrets, real-device checks) go only into the
    manifest's `manual_before` and `manual_after`, never between phases.
  - **New connectors.** A new connector is two PRs, connect then tools, and `/tld:make-plan` plans them that
    way with the browser connection on the QA server between them as a manual step. The procedure is in
    [`docs/coding-and-testing-guidelines.md`](docs/coding-and-testing-guidelines.md#3-adding-a-connector), "Adding a connector".
  ```

  Phase p10 turns "ADR 0311" and "ADR 0312" into links once the files exist.
- **`.claude/skills/steward/SKILL.md`:** in the description "the devflow `pr-steward` skill" becomes "the tld plugin's pr-steward policy"; `:9` "sits on top of the `pr-steward` skill" becomes "sits on top of the tld plugin's pr-steward policy"; `:69` "Dispatch `release.yml` (dry run first) instead." becomes "Use `/cut-release`, which dispatches `tld-release.yml` (dry run first)."
- **ADR index row 0132** (phase p9): its Status cell becomes "Superseded by 0311". Its title cell stays: titles quote the ADR.
- **The protected-path lock.** Manual step mb1 had the owner merge `hooks.protect_paths_allow` on `main` (done: PRs 1111 and 1115). This plan edits one protected file that list lacks, `.github/workflows/qa-record-fixture.yml`, so `mb1-allow-qa-record-fixture` adds it (only an allow list on `main` counts). The retirement phase removes exactly the entries this plan used, as its last edit to `.tld/toolkit.yaml`: `.github/workflows/build.yml`, `.github/workflows/release.yml`, `.github/workflows/tests.yml`, `.github/workflows/website-build.yml`, `.github/workflows/dependency-audit.yml`, `.github/workflows/qa-record-fixture.yml` and `.tld/toolkit.yaml`. The other entries (`publish-pypi.yml`, `publish-org-image.yml`, `publish-n8n-node.yml`, `deploy-oauth-relay-worker.yml`, `ai-client-canary.yml`, `pages.yml`) belong to the desktop removal and stay; the final reply tells the owner they look stale. The removal takes effect when the feature PR merges. Rejected: leaving the allow list in place (it would keep the workflows and the profile editable by every later session).

### 3.9 The ADRs (phases p9 and p10)

Each uses the template in `docs/adr/README.md`, has the Status `Accepted — <the date the phase runs, YYYY-MM-DD>` and is 40-80 lines long. Files under `docs/adr/`.

- **0310**, `0310-a-release-tag-is-created-only-by-tld-release-yml.md`. Title: "a release tag is created only by `tld-release.yml`".
  - Status lines: "Supersedes 0185." and "Amends 0021: the token is `TLD_RELEASE_TOKEN` in environment `tld-release`, and the tag is an annotated one created through the REST API."
  - Context: `release.yml` and `tld-release.yml` coexisted after the tld migration.
  - Decision: `tld-release.yml`, rendered by `tld ops repo-admin` from `.tld/admin-policy.yaml`, is the only route to a `v*` tag; its `check_script` is `scripts/release_check.py` (channel, stable notes, `tag_release.py --no-fetch --check-only`); the `tld-tags` ruleset and the owner-approved `tld-release` environment replace the `release tags` ruleset and the `release` environment.
  - Alternatives: keep `release.yml` (two routes to one tag); edit `tld-release.yml` beyond its params to keep the post-tag poll (drift in every audit; the poll moves to `/cut-release`); check the `build.yml` pre-flight from the check script (no token, unauthenticated API flakiness); a `release:` profile section with `/tld:release` (needs `version_files`, and the version comes from `setuptools_scm` tags).
  - Consequences: the first tld-made tag is annotated where recent ones were lightweight; `setuptools_scm`, `r2_release.py check-tag` and `release_run_guard.py` are unaffected; the green check blocks on any red or queued check run on `main`'s tip; ADR 0030's pre-flight is still a procedure, not a gate; ADR 0023's preview now comes from `release_check.py`.
  - Verification: `tests/unit/test_release_check.py` and `tests/unit/test_tag_release.py`.
- **0311**, `0311-workflow-commands-come-from-the-tld-plugin.md`. Title: "workflow commands come from the tld plugin". Status: "Supersedes 0132." Decision: `/tld:*` commands, the guard hooks (`protect-paths`, `no-git-tag`, `plan-gate`, `board-gate`) and the status board come from the tld plugin enabled in the Claude Project; the project facts are `.tld/toolkit.yaml`, validated by `tld profile validate`; `.claude/skills/steward/SKILL.md` keeps only PrivacyFence policy; there is no SessionStart install of the plugin. Alternatives: stay on devflow; install tld from a SessionStart hook pinned to a tag (as 0132 rejected for devflow). Consequences: protected files (`.github/workflows/**`, `.tld/toolkit.yaml`) need an allow list on `main` before a session may edit them; a real release dispatch from a session needs `tld guard allow-release`; the plugin version is not pinned in this repository.
- **0312**, `0312-plans-stay-on-main-as-the-record-of-their-change.md`. Title: "plans stay on `main` as the record of their change". Decision: the lifecycle sentence of §3.8, and that the docs tests skip plan records entirely (already in `tests/unit/test_docs_links.py` and `tests/unit/test_docs_references_exist.py`). Alternatives: keep deleting plans (tld's implement cannot: no phase may touch the plan); move plans to `docs/plans/<n>/` (rejected by the owner: `plan_dir` stays `docs/`); hold plan records to the reference checks (a later rename would force editing a historical record). Consequences: ADR README rules 4 and 5 change as in §3.8; `build_site.py` publishes only top-level `docs/*.md`, so plans never reach the website.
- **0313**, `0313-rulesets-and-required-checks-live-in-the-admin-policy.md`. Title: "rulesets and required checks live in the admin policy". Decision: `.tld/admin-policy.yaml` is the one list of required checks and merge-method settings; `tld ops repo-admin apply` writes `tld-main` and `tld-tags`; `scripts/update_branch_protection.py` is retired; `tests/unit/test_required_checks.py` keeps the list equal to the workflows; the owner deletes rulesets the policy does not own. Alternatives: keep the script for `main` (two sources); keep it read-only (it needs an owner token and compares against a ruleset the policy no longer writes); a CI job that applies the policy (the token would need ruleset admin rights). Consequences: §3.6's loosened settings (no strict up-to-date rule, no code-owner review requirement, one approval with an admin bypass through a pull request); merge methods are restricted by `repo_settings`, not by a ruleset; the policy's `admin_bypass` works only once the old `main` ruleset is deleted. Verification: `tests/unit/test_required_checks.py`.
- **0314**, `0314-the-releases-integration-branch-convention-is-retired.md`. Title: "the `releases/*` integration-branch convention is retired". Decision: no long-lived `releases/*` branches; `main` is the only integration branch; no CI trigger, ruleset or contributor document keeps the pattern. The QA recording workflow keeps refusing `releases/*` branches as a harmless deny-list entry (ADR 0181 is unchanged). Alternatives: keep the convention with a hand-made ruleset when a branch is cut; keep a script just for `releases/**`. Consequences: a batch of work that must wait is a feature branch or a feature flag, and the `v*` tags are cut from `main` only (`tag_release.py` already refuses another branch).
- **Status lines on old ADRs** (phase p10; README rule 3: one line each, nothing else in the body): 0185 gains "Superseded by [ADR 0310](0310-a-release-tag-is-created-only-by-tld-release-yml.md)."; 0021 gains "Amended by [ADR 0310](0310-a-release-tag-is-created-only-by-tld-release-yml.md): the tag is created by `tld-release.yml` with `TLD_RELEASE_TOKEN`."; 0132 gains "Superseded by [ADR 0311](0311-workflow-commands-come-from-the-tld-plugin.md)." ADRs 0030 and 0023 are unchanged.
- **Index rows** (`docs/adr/README.md`, phase p9): five new rows at the end in the existing format, Status cells: 0310 "Accepted; supersedes 0185, amends 0021; relates to 0023, 0030", 0311 "Accepted; supersedes 0132", 0312 "Accepted", 0313 "Accepted", 0314 "Accepted"; row 0185's Status cell becomes "Superseded by 0310"; row 0021's becomes "Accepted (retroactive); amended by 0185, 0310" (read the current cell and append only "; amended by 0310"); row 0132's becomes "Superseded by 0311".

## 4. ADRs

- 0310: a release tag is created only by `tld-release.yml` (supersedes 0185, amends 0021).
- 0311: workflow commands come from the tld plugin (supersedes 0132).
- 0312: plans stay on `main` as the record of their change.
- 0313: rulesets and required checks live in the admin policy (the branch-protection script is retired).
- 0314: the `releases/*` integration-branch convention is retired.

## 5. Manual steps

These steps follow the playbook's two places: `manual_before` before the first phase, `manual_after` after the last phase merges into the feature branch. Because `tld-release` and `tld-main` act only on `main`, the three `manual_after` items are done after the feature PR merges and reported there as a follow-up.

### Before implementation

**1. mb1-allow-qa-record-fixture: let sessions edit `qa-record-fixture.yml` (about 5 minutes).**
1. On your own machine check out `main` and pull.
2. Open `.tld/toolkit.yaml`. Under `hooks:`, in the `protect_paths_allow` list, add one line: `    - .github/workflows/qa-record-fixture.yml`. The other entries stay.
3. Run `tld profile validate .tld/toolkit.yaml`. You see `OK`.
4. Commit it (for example `chore: let the release retirement edit qa-record-fixture.yml`), push a branch and open a PR. Its CI must pass. Merge it.
5. Done when `git show origin/main:.tld/toolkit.yaml` lists `.github/workflows/qa-record-fixture.yml` under `hooks.protect_paths_allow`.

**2. mb2-tld-release-ready: prove `tld-release.yml` works before `release.yml` is deleted (about 15 minutes).**
1. The `tld-tags` ruleset already exists, so the admin policy was applied at least once. Check the rest at https://github.com/privacyfence/privacyfence/settings/environments: `tld-release` lists you as required reviewer. If it is missing, run `tld ops repo-admin audit --policy .tld/admin-policy.yaml` on your own machine, signed in as an organisation owner (`gh auth login`, or an owner token in the `GH_TOKEN` variable of that shell, never in chat), then `apply` the same way, first without and then with `--yes`. The guide is https://github.com/tkcs-lab/tkcs-lab-devflow/blob/main/docs/guides/admin.md. Delete no ruleset yet.
2. Open https://github.com/privacyfence/privacyfence/settings/environments, then **tld-release**. Under **Environment secrets** there must be `TLD_RELEASE_TOKEN`. If there is not: create a fine-grained token on your account with **Contents: read and write** on `privacyfence/privacyfence` at https://github.com/settings/personal-access-tokens/new, then **Add environment secret** named `TLD_RELEASE_TOKEN` with the token as its value. The token goes only there, never into chat, the plan or an ask.
3. Open https://github.com/privacyfence/privacyfence/actions/workflows/tld-release.yml, then **Run workflow**. Choose branch `main`, set `version` to the next pre-release you would cut (for example the next `a` number after the newest tag), and leave `dry_run` ticked.
4. Approve the deployment to `tld-release` when GitHub asks.
5. Done when the run ends green, with "Check that the commit is green" and "Show the release notes" passing. A red green-check means a check run on `main`'s tip is red or queued; the step's log names it.

### After implementation

**3. ma1-dry-run-with-check-script: after the feature PR merges, dry-run `tld-release.yml` with the new check script (about 10 minutes).**
1. Open https://github.com/privacyfence/privacyfence/actions/workflows/tld-release.yml, then **Run workflow** on `main`, with the next pre-release `version` (a stable dry run fails until a release PR has opened its `CHANGELOG.md` section) and `dry_run` ticked. Approve the environment.
2. Open the run. "Run the project's release check" must list `==> release channel`, then `==> tag checks`, and must end with a line that starts `release check passed for v<version>`.
3. Report back as a PR line: `tld-release dry run with release_check.py: pass|fail <run URL>`.

**4. ma2-apply-the-policy-and-delete-the-old-rulesets: make `tld-main` the only ruleset on the default branch (about 15 minutes, after the merge).**
1. On your own machine, signed in as an organisation owner as in step 1 of mb2, pull `main`.
2. Run `tld ops repo-admin audit --policy .tld/admin-policy.yaml`. Expect findings for the missing or changed `tld-main` ruleset and the three merge-method settings; read them.
3. Run `tld ops repo-admin apply --policy .tld/admin-policy.yaml` (a dry run that lists changes), then the same with `--yes`. At https://github.com/privacyfence/privacyfence/settings/rules `tld-main` now exists and lists all ten required checks, including `n8n-node`.
4. Only now delete the old rulesets, one at a time, at https://github.com/privacyfence/privacyfence/settings/rules: open `main`, then **Delete ruleset**; the same for `default-branch`, `release tags` and `releases/**`. Keep `tld-main` and `tld-tags`.
5. Check the merge methods at https://github.com/privacyfence/privacyfence/settings: under **Pull Requests** only **Allow merge commits** is ticked.
6. Open any new pull request: its merge box lists the ten checks.
7. Report back: `rulesets: tld-main and tld-tags only: yes|no; checks listed: 10|<n>`.

**5. ma3-remove-the-old-release-settings: delete what only `release.yml` used (about 5 minutes, after the merge).**
1. At https://github.com/privacyfence/privacyfence/settings/environments, open **release** and click **Delete environment**; the list no longer shows it. This also deletes its `RELEASE_TAG_TOKEN` secret.
2. At https://github.com/privacyfence/privacyfence/settings/secrets/actions, delete a repository-level `RELEASE_TAG_TOKEN` if one exists.
3. Revoke the fine-grained token that was `RELEASE_TAG_TOKEN` at https://github.com/settings/personal-access-tokens.
4. Report back: `old release environment and token removed: yes|no`.

## 6. Risks and open questions

- **Protected paths.** If a worker editing a workflow or `.tld/toolkit.yaml` is refused with "is a protected path", the allow list on `main` lacks that file (mb1 covers `qa-record-fixture.yml`): stop with `status=blocked`, never work around the hook. For `tld-release.yml` the refusal names `tld guard allow-workflow-install`: run exactly that, then end-allow.
- **The policy cannot reproduce two live settings.** `tld-main` has no up-to-date-branch rule (live `main`: strict) and no code-owner review rule. Accepted in ADR 0313; if the owner wants either back, it is a tld feature request, not a script.
- **Between merge and ma2 the old rulesets still guard** and still lack `n8n-node`; nothing gets weaker meanwhile. Applying the policy before deleting is what keeps the branch protected at every moment.
- **`stable` is not covered by any ruleset** (ADR 0169 needs `GITHUB_TOKEN` to write it). Not in scope; stated so nobody assumes the policy protects it.
- **The green check can block a cut on unrelated runs:** a scheduled run on `main`'s tip that failed, or the self-hosted `deploy-qa-server` job waiting for an offline runner. Documented, not worked around; `skip_checks` stays forbidden.
- **The first annotated tag.** `git describe --tags` (`setuptools_scm`), `r2_release.py check-tag` and `release_run_guard.py` handle annotated tags (20 old annotated tags exist). Not proven: whether `github.sha` on a push of an annotated tag created through the API is the commit that `release_run_guard.py` compares. `/cut-release` step 7 watches every real cut for the `build.yml` and `publish-pypi.yml` runs, and a pre-release is preferred for the first one.
- **Token scope.** If the real cut fails at "Publish the tag" with a 403 that mentions workflows, the fine-grained token also needs **Workflows: write**. A settings fix for the owner, found at the first real cut.
- **`ma1` happens after the merge**, because tld-release runs only on `main`; the PR carries it as an unchecked item. Before the merge the script is proven by `tests/unit/test_release_check.py` and by p2's local run against a clone.
- **ADR numbers.** If another PR takes 0310-0314 first, renumber in p9 (README rule 6) and update every "ADR 031x" mention on this branch; record the mechanical rename as a deviation.
- **A required check that never reports blocks every merge.** `tests/unit/test_required_checks.py` guards names and matrices; it cannot see a job-level `if:` added later, so a reviewer of a `tests.yml` change checks that too.

## Implementation manifest

```yaml
plan_slug: retire-release-yml
feature_branch: chore/retire-release-yml
tracking_issue: 1093
max_parallel: 2
manual_before:
  - id: mb1-allow-qa-record-fixture
    title: Add qa-record-fixture.yml to hooks.protect_paths_allow on main
    why: p8 edits that workflow; the protect-paths hook reads its allow list only from main, so without it the phase is refused.
    done_when: git show origin/main:.tld/toolkit.yaml lists .github/workflows/qa-record-fixture.yml under hooks.protect_paths_allow.
  - id: mb2-tld-release-ready
    title: TLD_RELEASE_TOKEN set in tld-release and one green tld-release dry run on main
    why: p3 deletes release.yml; tld-release.yml must already work, or the repository has no way to cut a release.
    done_when: A tld-release workflow_dispatch run on main with dry_run true ended green.
manual_after:
  - id: ma1-dry-run-with-check-script
    title: After merging, dry-run tld-release.yml and see release_check.py pass
    why: The check step runs only on main in a token-less clone; CI cannot run it from the feature branch.
  - id: ma2-apply-the-policy-and-delete-the-old-rulesets
    title: After merging, apply the admin policy, then delete the rulesets main, default-branch, release tags and releases/**
    why: The live rulesets are only changed by an owner token; applying first keeps the default branch protected, and the old main ruleset has no bypass actor, which cancels admin_bypass.
  - id: ma3-remove-the-old-release-settings
    title: After merging, delete the release environment and RELEASE_TAG_TOKEN
    why: They are unused once release.yml is gone, and a leftover tag-writing token is a standing risk.
verify_after_merge:
  - python3 -m pytest tests/unit -q
final_checks:
  - "`tld plan deviations check --file docs/1093/deviations.md --plan docs/1093/plan.md --all` exits 0"
  - ADRs 0310 to 0314 exist under docs/adr/, are Accepted, and have rows in docs/adr/README.md
  - CHANGELOG.md has no new version heading and no new entry (nothing user-visible changes)
  - "`test ! -e .github/workflows/release.yml && test ! -e scripts/update_branch_protection.py` exits 0"
  - "`git grep -n 'release\\.yml' -- . ':!CHANGELOG.md' ':!docs/adr/' ':(exclude,glob)docs/[0-9]*/**' | grep -v tld-release` prints nothing"
  - "`git grep -n 'releases/\\*' -- . ':!CHANGELOG.md' ':!docs/adr/' ':(exclude,glob)docs/[0-9]*/**' ':!.github/workflows/qa-record-fixture.yml' ':!docs/connector-qa.md' ':!.claude/commands/qa-record.md' ':!docs/testing-policy.md'` prints nothing"
  - "`git grep -n 'releases/\\*\\*' -- docs/testing-policy.md` prints nothing"
  - "`git grep -n -i 'devflow\\|claude-toolkit' -- . ':!CHANGELOG.md' ':!docs/adr/' ':(exclude,glob)docs/[0-9]*/**' | grep -v 'tkcs-lab-devflow'` prints nothing"
phases:
  - id: p1-build-permissions
    title: Top-level read-only permissions in build.yml
    depends_on: []
    complexity: S
    worker_level: fast
    touches:
      - .github/workflows/build.yml
    brief: |
      Design: docs/1093/plan.md section 3.1.
      1. In .github/workflows/build.yml insert the four lines of 3.1 (two comment lines, `permissions:`,
         `  contents: read`) between the line `  workflow_dispatch:   # allow manual runs from the Actions tab` and
         `jobs:`, with one blank line before and one after. Change nothing else.
      2. Run `python3 -c "import yaml; d=yaml.safe_load(open('.github/workflows/build.yml')); assert d['permissions']=={'contents':'read'}"`.
      3. Run `python3 -m pytest tests/unit -q`.
      Stop condition: if the edit is refused as a protected path, the allow list on main lost build.yml: stop with status=blocked.
    acceptance:
      - grep -n '^permissions:' .github/workflows/build.yml matches exactly once, before the line '^jobs:'
      - git diff origin/main -- .github/workflows/build.yml shows only added lines
      - python3 -m pytest tests/unit -q passes
  - id: p2-release-check-script
    title: scripts/release_check.py and tag_release.py --check-only
    depends_on: []
    complexity: M
    touches:
      - scripts/release_check.py
      - tests/unit/test_release_check.py
      - scripts/tag_release.py
      - tests/unit/test_tag_release.py
    brief: |
      Design: docs/1093/plan.md sections 3.2 and 3.3. Read tests/unit/test_tag_release.py first and copy its
      import-by-path pattern and style.
      1. scripts/tag_release.py: add `--check-only` in a mutually exclusive group with `--push`, the early
         return in main() after check_sequential, and the docstring edits, all exactly as in 3.3.
      2. tests/unit/test_tag_release.py: add the three tests named in 3.3 in `TestMain`, using the same helper
         that `test_creates_local_tag_without_pushing` uses.
      3. Create scripts/release_check.py exactly as specified in 3.2 (stdlib only, `from __future__ import
         annotations`, type hints, module docstring naming tld-release.yml's check_script and ADR 0310,
         `if __name__ == "__main__": sys.exit(main())`).
      4. Create tests/unit/test_release_check.py with the ten tests named in 3.2 (`pytestmark = pytest.mark.unit`).
         Fake `subprocess.run` on the imported module with monkeypatch; only
         test_an_invalid_version_fails_at_the_channel_step runs the real scripts.
      5. Local proof, recorded in your report. In the repository run `git fetch -q --tags origin main`. Let
         NEWEST be the highest release tag by PEP 440: `git tag --list 'v*' | sed 's/^v//' | python3 -c "import sys; from packaging.version import Version; print(max(sys.stdin.read().split(), key=Version))"`;
         the version is NEWEST with its pre-release number plus one (7.1.0a2 gives 7.1.0a3), or, when NEWEST is stable,
         its patch plus one followed by a1 (7.0.2 gives 7.0.3a1). Then in a scratch directory run
         `git clone -q --no-local <repo path> chk && cd chk && git fetch -q --tags`, then
         `git checkout -q -B main $(git -C <repo path> rev-parse origin/main) && git update-ref refs/remotes/origin/main HEAD && git branch -q --set-upstream-to=origin/main main`,
         copy in scripts/release_check.py and scripts/tag_release.py from your branch, commit them, run
         `git update-ref refs/remotes/origin/main HEAD`, then `python3 scripts/release_check.py <version>`.
         It must end with a line starting `release check passed for v<version>`. Quote the last five lines.
      6. Run `python3 -m pytest tests/unit/test_release_check.py tests/unit/test_tag_release.py -q` and `ruff check scripts tests/unit`.
      Comments and docstrings carry no issue numbers, phase ids or plan references (tests/unit/test_code_no_history.py).
      Stop condition: if step 5 fails, run the same command with `scripts/tag_release.py <version> --no-fetch
      --check-only` directly in the scratch clone; if that also fails, stop with status=blocked and quote both errors.
    acceptance:
      - python3 -m pytest tests/unit/test_release_check.py -q passes with 10 tests
      - python3 -m pytest tests/unit/test_tag_release.py -q passes
      - python3 -m pytest tests/unit/test_tag_release.py -q -k check_only passes with 3 tests
      - grep -n '"--no-fetch", "--check-only"' scripts/release_check.py matches
      - grep -n 'release\.yml' scripts/tag_release.py | grep -v tld-release prints nothing
      - ruff check scripts/release_check.py scripts/tag_release.py tests/unit/test_release_check.py tests/unit/test_tag_release.py passes
  - id: p3-release-workflows
    title: Delete release.yml, set tld-release's check_script
    depends_on: [p2-release-check-script]
    complexity: S
    touches:
      - .github/workflows/release.yml
      - .github/workflows/tld-release.yml
    brief: |
      Design: docs/1093/plan.md section 3.4 (the first two bullets only).
      1. `git rm .github/workflows/release.yml`.
      2. Run `tld guard allow-workflow-install tld-release.yml`, change the line `          CHECK_SCRIPT: ""` of
         .github/workflows/tld-release.yml (line 145 at the time of planning; find it with grep) to
         `          CHECK_SCRIPT: "scripts/release_check.py"`, then run `tld guard end-allow`. Run end-allow even if the edit failed.
      3. Run `python3 -m pytest tests/unit -q`.
      Stop condition: if any edit is refused as a protected path, stop with status=blocked and quote the refusal.
    acceptance:
      - test ! -e .github/workflows/release.yml
      - 'grep -n ''CHECK_SCRIPT: "scripts/release_check.py"'' .github/workflows/tld-release.yml matches'
      - 'git diff origin/main --numstat -- .github/workflows/tld-release.yml shows 1 added and 1 deleted line'
      - python3 -m pytest tests/unit -q passes
  - id: p4-release-docs
    title: Release docs and /cut-release describe tld-release.yml
    depends_on: [p2-release-check-script]
    complexity: M
    touches:
      - docs/releasing.md
      - docs/release-testing.md
      - .claude/commands/cut-release.md
    brief: |
      Design: docs/1093/plan.md section 3.5 (all of it) and 3.3 ("Rejected: a post-tag poll").
      1. docs/releasing.md: rewrite "Cutting a release" as 3.5 lists, points 1-7, keeping the heading, the
         ADR 0021 and ADR 0030 links, and every paragraph that does not name release.yml unchanged.
         Name ADR 0310 in plain text, without a link. Fix the job list in the pre-flight paragraph as point 6 says.
      2. docs/release-testing.md: gate 1, gates 5 and 6 and evidence item 1 with the literal text of 3.5.
      3. .claude/commands/cut-release.md: the procedure of 3.5, points 1-7, in the file's existing voice; its
         pre-flight job list is "`build-org-image (amd64)`, `build-org-image (arm64)`, and `sbom`" as it is today.
      4. These are contributor docs under tests/unit/test_docs_no_history.py: no `#<number>`, no "Phase <n>",
         no "P<n>", no "since v<x>". Run `python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q`.
      Stop condition: if a paragraph you must change also states a rule this plan does not mention, keep the rule,
      reword only the workflow name, and list it in your Deviations line.
    acceptance:
      - grep -n 'release\.yml' docs/releasing.md docs/release-testing.md .claude/commands/cut-release.md | grep -v tld-release prints nothing
      - grep -n 'tld guard allow-release tld-release.yml' .claude/commands/cut-release.md matches
      - grep -n 'skip_checks' docs/releasing.md .claude/commands/cut-release.md matches in both files
      - grep -n 'check-only' docs/releasing.md matches
      - grep -n 'RELEASE_TAG_TOKEN' docs/releasing.md .claude/commands/cut-release.md prints nothing
      - grep -n 'tld board ask' .claude/commands/cut-release.md matches
      - grep -n 'releases/\*' docs/release-testing.md prints nothing
      - python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q passes
  - id: p5-rulesets-in-policy
    title: The admin policy holds the required checks; retire update_branch_protection.py
    depends_on: []
    complexity: M
    touches:
      - .tld/admin-policy.yaml
      - scripts/update_branch_protection.py
      - tests/unit/test_update_branch_protection.py
      - tests/unit/test_required_checks.py
      - tests/unit/test_definition_of_done_drift.py
      - docs/testing-policy.md
      - .github/workflows/tests.yml
      - .github/workflows/website-build.yml
    brief: |
      Read first: docs/1093/plan.md section 3.6; tests/unit/test_update_branch_protection.py lines 353-395 (the tests you port).
      1. .tld/admin-policy.yaml: replace the five-entry `required_status_checks` list with the ten of 3.6, add
         `allow_merge_commit: true`, `allow_squash_merge: false` and `allow_rebase_merge: false` under `repo_settings`,
         and add `check_script: scripts/release_check.py` to the release workflow's params, as 3.6 gives them.
         Keep every comment that is not made untrue by the edit. Run `tld ops repo-admin validate-policy .tld/admin-policy.yaml`.
      2. Create tests/unit/test_required_checks.py with the five tests named in 3.6, porting the logic of
         `TestRequiredChecksList`. Load the list with `yaml.safe_load(...)["rulesets"]["default_branch"]["required_status_checks"]`.
      3. tests/unit/test_definition_of_done_drift.py: make `_required_status_checks()` read the same list from
         `.tld/admin-policy.yaml`; remove the importlib use if nothing else needs it.
      4. `git rm scripts/update_branch_protection.py tests/unit/test_update_branch_protection.py`.
      5. docs/testing-policy.md: the "Required checks" paragraph and the `/dod` line, with the literal text of 3.6.
      6. .github/workflows/tests.yml and website-build.yml: only the two comment edits of 3.6.
      7. Run `python3 -m pytest tests/unit/test_required_checks.py tests/unit/test_definition_of_done_drift.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_links.py -q`,
         `python3 -m pytest tests/unit -q` and `ruff check .`.
      Stop condition: if a required name from 3.6 is not reported by any job (the new tests fail on it), stop with
      status=blocked and name it; do not delete it from the list. If an edit to a workflow is refused as a protected path, stop with status=blocked.
    acceptance:
      - python3 -m pytest tests/unit/test_required_checks.py -q passes with 5 tests
      - python3 -m pytest tests/unit/test_definition_of_done_drift.py -q passes
      - test ! -e scripts/update_branch_protection.py
      - "`git grep -n update_branch_protection -- . ':!CHANGELOG.md' ':!docs/adr/' ':(exclude,glob)docs/[0-9]*/**' ':!CONTRIBUTING.md'` prints nothing"
      - "`python3 -c \"import yaml; d=yaml.safe_load(open('.tld/admin-policy.yaml')); print(len(d['rulesets']['default_branch']['required_status_checks']))\"` prints 10"
      - grep -n 'allow_squash_merge: false' .tld/admin-policy.yaml matches
      - 'grep -n ''check_script: scripts/release_check.py'' .tld/admin-policy.yaml matches'
      - tld ops repo-admin validate-policy .tld/admin-policy.yaml exits 0
      - python3 -m pytest tests/unit -q passes
  - id: p6-tld-lifecycle-docs
    title: Contributor docs describe tld and the kept-plan lifecycle
    depends_on: []
    complexity: M
    touches:
      - CLAUDE.md
      - CONTRIBUTING.md
      - docs/coding-and-testing-guidelines.md
      - .github/pull_request_template.md
      - .claude/skills/steward/SKILL.md
    brief: |
      Design: docs/1093/plan.md section 3.8 (the lifecycle bullets for CONTRIBUTING, §2.7 and the PR template; the CLAUDE.md block; the steward edits). Do not edit docs/adr/, docs/testing-policy.md or .tld/toolkit.yaml; other phases do.
      1. CONTRIBUTING.md: the four replacements of 3.8, with the literal text given there.
      2. docs/coding-and-testing-guidelines.md §2.7 and .github/pull_request_template.md: the row replacement of 3.8.
         Keep each checklist item's line wrapping style.
      3. CLAUDE.md: replace "## Commands and skills" up to "## Parallel sessions & worktrees" with the block of 3.8.
         Keep "ADR 0311" and "ADR 0312" as plain text (the files do not exist yet).
      4. .claude/skills/steward/SKILL.md: the three edits of 3.8.
      5. Run `python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_definition_of_done_drift.py -q`.
      Stop condition: if test_definition_of_done_drift.py fails, the §2.7 edit changed a command span: revert to
      changing only the plan-document wording, and stop with status=blocked if it still fails.
    acceptance:
      - grep -n -i 'devflow\|claude-toolkit\|plan/<slug>\|-plan\.md' CLAUDE.md .claude/skills/steward/SKILL.md prints nothing
      - grep -n 'deletes a plan document' docs/coding-and-testing-guidelines.md .github/pull_request_template.md prints nothing
      - grep -n 'docs/<issue>/plan.md' CONTRIBUTING.md CLAUDE.md docs/coding-and-testing-guidelines.md .github/pull_request_template.md matches in each file
      - grep -n 'release\.yml' CLAUDE.md .claude/skills/steward/SKILL.md | grep -v tld-release prints nothing
      - python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_definition_of_done_drift.py -q passes
  - id: p7-release-branch-docs
    title: Drop the releases/* convention from the contributor docs
    depends_on: [p5-rulesets-in-policy, p6-tld-lifecycle-docs]
    complexity: S
    touches:
      - CONTRIBUTING.md
      - CLAUDE.md
      - .claude/skills/steward/SKILL.md
      - docs/testing-policy.md
    brief: |
      Design: docs/1093/plan.md section 3.7, the bullets for CONTRIBUTING.md, CLAUDE.md:6, the steward skill and docs/testing-policy.md (lines 51, 140, 151 only; line 228 stays).
      1. CONTRIBUTING.md: delete the subsection `### Release integration branches (`releases/*`)` through the paragraph that ends
         "never ships to production ahead of that branch's eventual merge into `main`.", with the blank line that followed it, so that
         `## Decisions, plans and ADRs` follows the "Before opening a PR" list as it did before the subsection.
      2. CLAUDE.md line 6: the literal edit of 3.7.
      3. .claude/skills/steward/SKILL.md: delete the one-line bullet at line 66 and nothing else.
      4. docs/testing-policy.md: the three literal edits of 3.7.
      5. Run `python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q` and `python3 -m pytest tests/unit -q`.
      Stop condition: if a test reads text you removed, stop with status=blocked and quote it.
    acceptance:
      - grep -n 'Release integration branches' CONTRIBUTING.md prints nothing
      - grep -n 'releases/\*' CONTRIBUTING.md CLAUDE.md .claude/skills/steward/SKILL.md prints nothing
      - grep -n 'releases/\*\*' docs/testing-policy.md prints nothing
      - python3 -m pytest tests/unit -q passes
  - id: p8-release-branch-workflows
    title: Drop the releases/** triggers from the workflows
    depends_on: [p5-rulesets-in-policy]
    complexity: S
    touches:
      - .github/workflows/tests.yml
      - .github/workflows/website-build.yml
      - .github/workflows/dependency-audit.yml
      - .github/workflows/qa-record-fixture.yml
    brief: |
      Design: docs/1093/plan.md section 3.7, the workflow bullets.
      1. .github/workflows/tests.yml: delete the three comment lines above `branches: [main, "releases/**"]` (they start `# releases/** carries`)
         and change that line to `branches: [main]`.
      2. .github/workflows/website-build.yml: replace the two header lines 3-4 with the two literal lines of 3.7, and change
         `branches: [main, "releases/**"]` to `branches: [main]`.
      3. .github/workflows/dependency-audit.yml: delete the two comment lines above `branches: [main, "releases/**"]` (they start `# releases/** is protected`)
         and change that line to `branches: [main]`.
      4. .github/workflows/qa-record-fixture.yml: change only the first of the four comment lines at line 99, with the literal of 3.7. Leave the guard condition and its error message unchanged.
      5. Run `python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]" .github/workflows/tests.yml .github/workflows/website-build.yml .github/workflows/dependency-audit.yml .github/workflows/qa-record-fixture.yml`,
         then `python3 -m pytest tests/unit -q` and `ruff check .`.
      Stop condition: if an edit is refused as a protected path, mb1 is not on main or the allow list lost the file: stop with status=blocked. If a test reads the removed text, stop with status=blocked and quote it.
    acceptance:
      - grep -n 'releases/\*\*' .github/workflows/tests.yml .github/workflows/website-build.yml .github/workflows/dependency-audit.yml prints nothing
      - grep -n 'branches: \[main\]' .github/workflows/tests.yml .github/workflows/website-build.yml .github/workflows/dependency-audit.yml matches in each file
      - grep -n 'CONTRIBUTING' .github/workflows/qa-record-fixture.yml prints nothing
      - grep -n 'releases/\*' .github/workflows/qa-record-fixture.yml still matches (the guard is kept)
      - python3 -m pytest tests/unit -q passes
  - id: p9-adrs
    title: ADRs 0310-0314 and the ADR index
    depends_on: [p1-build-permissions, p3-release-workflows, p4-release-docs, p7-release-branch-docs, p8-release-branch-workflows]
    complexity: M
    touches:
      - docs/adr/0310-a-release-tag-is-created-only-by-tld-release-yml.md
      - docs/adr/0311-workflow-commands-come-from-the-tld-plugin.md
      - docs/adr/0312-plans-stay-on-main-as-the-record-of-their-change.md
      - docs/adr/0313-rulesets-and-required-checks-live-in-the-admin-policy.md
      - docs/adr/0314-the-releases-integration-branch-convention-is-retired.md
      - docs/adr/README.md
    brief: |
      Design: docs/1093/plan.md sections 3.9 (ADRs and index rows) and 3.8 (the docs/adr/README.md lifecycle edits).
      1. Check `ls docs/adr` and the open PRs' ADR files: if 0310-0314 is taken, take the next free numbers, use them everywhere below, and record the renumbering as a deviation.
      2. Write the five ADRs of 3.9 from the template in docs/adr/README.md, Accepted with today's date (UTC), 40-80 lines each. Content comes only from plan sections 3.3-3.9; do not cite the plan, a phase id or an issue number inside an ADR.
      3. docs/adr/README.md: line 8, the Plan table row and rules 4 and 5 (3.8), and the five new index rows and the three changed Status cells (0185, 0021, 0132) of 3.9.
      4. Run `python3 -m pytest tests/unit -q -k "adr or docs"` and `ruff check .`.
      Stop condition: if an ADR index test rejects a row format, copy the format of the row above it and stop with status=blocked if it still fails.
    acceptance:
      - ls docs/adr/031[0-4]-*.md lists five files (or the renumbered ones named in deviations)
      - grep -c -E '^\| \[031[0-4]\]' docs/adr/README.md prints 5
      - grep -n 'Superseded by 0310' docs/adr/README.md matches
      - python3 -m pytest tests/unit -q -k "adr or docs" passes
  - id: p10-retirement
    title: Old-ADR status lines, profile wording, ADR links and the protected-path lock
    depends_on: [p9-adrs]
    complexity: M
    touches:
      - docs/adr/0021-release-tag-push-never-uses-github-token.md
      - docs/adr/0132-workflow-commands-come-from-the-devflow-toolkit-plugins.md
      - docs/adr/0185-a-release-tag-is-created-only-by-release-yml.md
      - .tld/toolkit.yaml
      - CLAUDE.md
      - docs/releasing.md
    brief: |
      Design: docs/1093/plan.md sections 3.4 (the ci.dispatchable row), 3.8 (toolkit header, dod_manual row 2, the lock) and 3.9 (Status lines).
      1. Add the one Status line each to ADRs 0185, 0021 and 0132 (3.9), using the final numbers of p9. Change nothing else in those files.
      2. CLAUDE.md: turn "ADR 0311" and "ADR 0312" into [ADR 0311](docs/adr/0311-workflow-commands-come-from-the-tld-plugin.md)
         and [ADR 0312](docs/adr/0312-plans-stay-on-main-as-the-record-of-their-change.md) (use the renumbered files if p9 renumbered).
         docs/releasing.md: turn the plain "ADR 0310" mentions into [ADR 0310](adr/0310-a-release-tag-is-created-only-by-tld-release-yml.md).
      3. .tld/toolkit.yaml: the header of 3.8, the dod_manual row 2 of 3.8, and the ci.dispatchable row of 3.4 (replace the `release.yml` row).
         Run `tld profile validate .tld/toolkit.yaml` and `python3 -m pytest tests/unit/test_definition_of_done_drift.py -q`.
      4. Last edit of the phase: delete from `hooks.protect_paths_allow` in .tld/toolkit.yaml exactly the seven entries listed in 3.8
         (`build.yml`, `release.yml`, `tests.yml`, `website-build.yml`, `dependency-audit.yml`, `qa-record-fixture.yml` and `.tld/toolkit.yaml`) and keep the other six. Run `tld profile validate .tld/toolkit.yaml` again.
      5. CHANGELOG.md gets no entry: nothing user-visible changes.
      6. Run `python3 -m pytest tests/unit -q` and `ruff check .`.
      Stop condition: if `tld profile validate` rejects the profile, stop with status=blocked and quote it. If the allow list on this branch does not hold the thirteen entries (the twelve of section 2 plus qa-record-fixture.yml from mb1), remove only the seven named ones and report the difference.
    acceptance:
      - grep -n 'Amended by \[ADR 0310\]' docs/adr/0021-release-tag-push-never-uses-github-token.md matches
      - grep -n 'Superseded by \[ADR 0310\]' docs/adr/0185-a-release-tag-is-created-only-by-release-yml.md matches
      - grep -n 'Superseded by \[ADR 0311\]' docs/adr/0132-workflow-commands-come-from-the-devflow-toolkit-plugins.md matches
      - grep -n 'workflow: release.yml' .tld/toolkit.yaml prints nothing
      - grep -n 'workflow: tld-release.yml' .tld/toolkit.yaml matches
      - grep -n -i 'devflow\|claude-toolkit' .tld/toolkit.yaml prints nothing
      - 'grep -n ''github/workflows/\(build\|release\|tests\|website-build\|dependency-audit\|qa-record-fixture\).yml'' .tld/toolkit.yaml prints nothing'
      - 'grep -n ''^    - .tld/toolkit.yaml'' .tld/toolkit.yaml prints nothing'
      - grep -c 'protect_paths_allow' .tld/toolkit.yaml prints 1
      - grep -n 'publish-pypi.yml' .tld/toolkit.yaml still matches in the allow list
      - tld profile validate .tld/toolkit.yaml exits 0
      - python3 -m pytest tests/unit -q passes
```
