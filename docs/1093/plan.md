# Retire release.yml, scope build.yml permissions, finish the move from devflow to tld

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** To cut a release, the maintainer (or `/cut-release`) dispatches `tld-release.yml` on `main` with an explicit `version`. The dry run is the default. The owner approves the `tld-release` environment, and the run shows every check plus the `CHANGELOG.md` body that will ship. `release.yml` is gone. Contributors and sessions now find plans at `docs/<issue>/plan.md`, next to `deviations.md`, and both stay on `main` as the record of the change.

**Scope:** Changes: `build.yml` gets a top-level `permissions: contents: read`. A new `scripts/release_check.py` runs the old pre-flight checks and becomes `tld-release.yml`'s `check_script`. `tag_release.py` gains `--check-only`. `.tld/admin-policy.yaml` gains `check_script`. `release.yml` is deleted. The release docs, `/cut-release`, the steward skill, `CLAUDE.md`, `CONTRIBUTING.md`, §2.7, the PR template, the ADR README, `testing-policy.md` and `.tld/toolkit.yaml` describe tld instead of devflow and the kept-plan lifecycle instead of deleted plans. Two docs tests treat `docs/<n>/plan.md` and `deviations.md` as plan records. Three ADRs are added. Unchanged: how `build.yml`, `publish-pypi.yml` and the other tag-triggered workflows behave, the version scheme, and every secret.

**Security:** The tag is now created through the REST API with `TLD_RELEASE_TOKEN`, an owner token in the `tld-release` environment, where the owner approves every run. This replaces `RELEASE_TAG_TOKEN` in `release`. The check script runs in a clone with no token. `build.yml`'s default token scope shrinks to `contents: read`. No secret is created, read or moved by any phase.

**Review focus:**
- §3.2: `release_check.py` runs `tag_release.py --no-fetch --check-only` inside tld-release's token-less clone.
- §3.3: the post-tag "did `build.yml` start" poll moves from the workflow to `/cut-release` and the docs.
- §3.6: plans stay on `main`, and the docs tests exempt plan records entirely.
- §3.5: `/cut-release` dispatches a real tld-release run only after the owner's yes on the board, as tld's pr-steward policy requires.
- §3.8: the protected-path unlock and its removal.

## 1. Goal

Issue: https://github.com/privacyfence/privacyfence/issues/1093.

The owner asked for three things:
1. A top-level `permissions:` block in `build.yml`, flagged by `tld ops repo-admin` lint.
2. The devflow / claude-toolkit wording that the tld migration left behind is cleaned up, without rewriting frozen ADR bodies.
3. `tld-release.yml` replaces `release.yml` as the only way to cut a release, with the old workflow's pre-flight checks preserved.

On item 2, the owner also decided that the repository adopts tld's plan lifecycle (answer `adopt_tld` on the board). Plans live at `docs/<n>/plan.md` and `docs/<n>/deviations.md` and stay on `main`. `docs.plan_dir` stays `docs/`.

## 2. Current state

- **`build.yml`.** `.github/workflows/build.yml:1-8` has `on:` and then `jobs:`, with no top-level `permissions:`. Every job declares its own:
  - `dedupe` (`:14-16`, `contents: read`, `actions: write`);
  - `build` (`:46`), `build-windows` (`:362`), `build-deb` (`:601`), `build-org-image` (`:756`) and `sbom` (`:886`), each `contents: read`;
  - `finalize-release` (`:1022-1024`, `contents: write`, `actions: read`).

  It is the only workflow without a top-level block: `grep -n '^permissions' .github/workflows/*.yml` lists every other file.
- **`release.yml`** (the "Cut release tag" workflow) checks out `main` with `RELEASE_TAG_TOKEN` in environment `release`. It then runs:
  - `scripts/r2_release.py channel`;
  - `scripts/changelog_section.py` (stable only, into the step summary);
  - `scripts/tag_release.py [--push]`;
  - after a real push, a 2-minute poll for a `build.yml` push run on the commit.
- **`tld-release.yml`** is rendered by `tld ops repo-admin` from `.tld/admin-policy.yaml`, `workflows: - template: release`. Its params are `tag_prefix: "v"`, `version_style: pep440` and `create_release: "false"`. Its steps:
  1. Decide the tag: explicit `version` or a bump; refuses an existing tag.
  2. Check that the commit is green (`:112`): every check run on `github.sha` other than itself must be completed with success, skipped or neutral, and the legacy commit status must not be failure or error.
  3. Run the project's release check (`:138`): with `CHECK_SCRIPT: ""` (`:145`) it does nothing. Otherwise it fetches every tag and `main` into a fresh clone, checks out `main` at `github.sha` with upstream `origin/main`, unsets the token, and runs `python3 <script> <version>`.
  4. Show the release notes (`:177`): GitHub's generated notes, not the changelog.
  5. Publish: an annotated tag object and ref through the REST API with `TLD_RELEASE_TOKEN`. `create_release` is false, so `build.yml`'s `finalize-release` makes the GitHub Release.

  The job runs in environment `tld-release` and only for `refs/heads/main`. Rendering the template with `check_script: scripts/release_check.py` gives a file that differs from the current one only at `:145`. The planner checked this with `tld`'s own `render_template`.
- **Who depends on `release.yml`** (`grep -rn 'release\.yml'`, excluding `tld-release.yml` and `CHANGELOG.md`):
  - `docs/releasing.md:20-75`;
  - `docs/release-testing.md:52-55,203`;
  - `docs/downloads-and-release-kpi.md:14`;
  - `.claude/commands/cut-release.md:12,30,45,52`;
  - `.claude/skills/steward/SKILL.md:72`;
  - `CLAUDE.md:26-28`;
  - `.tld/toolkit.yaml:123-125` (`ci.dispatchable`);
  - `scripts/tag_release.py:4,36,49` (docstring).

  Frozen ADR bodies (0021, 0022, 0023, 0030, 0111, 0185) also name it; only Status lines may change (ADR README rule 2). No test reads `release.yml`.
- **`scripts/tag_release.py`.** `main()` at `:258-300` always creates a local tag and prints a push hint. `--no-fetch` exists (`:268`). Tests are in `tests/unit/test_tag_release.py` and use real temporary git repositories.
- **devflow wording:**
  - `.tld/toolkit.yaml:1,3`;
  - `CLAUDE.md:16-50` (the devflow `/make-plan` and `/implement` behaviour: `plan/` branches, `docs/<slug>-plan.md`, deletion);
  - `.claude/skills/steward/SKILL.md:3,9` ("the devflow `pr-steward` skill");
  - `docs/testing-policy.md:304`;
  - ADR index row 0132 (`docs/adr/README.md:232`).

  `.tld/admin-policy.yaml:1` names the repository `tkcs-lab/tkcs-lab-devflow`. That is a real repository name and stays.
- **Plan lifecycle text that tld made untrue:**
  - `CONTRIBUTING.md:16-18,55-56,63-66,69-70`;
  - `docs/adr/README.md:8,15,44-48`;
  - `docs/coding-and-testing-guidelines.md:366-369` (§2.7);
  - `.github/pull_request_template.md:28-31`;
  - `.tld/toolkit.yaml` `verify.dod_manual` second row.
- **Docs tests.**
  - `tests/unit/test_docs_links.py:62-65` and `tests/unit/test_docs_references_exist.py:97-105` treat only top-level `docs/*-plan.md` as plan documents. This plan sits at `docs/1093/plan.md` and names a deviations file that does not exist yet.
  - `tests/unit/test_docs_no_history.py` forbids `#123`-style numbers, `Phase 4`, `P1`, `since v7.0` and "migration guide" in the contributor docs: `CLAUDE.md`, `CONTRIBUTING.md`, `docs/releasing.md`, `docs/release-testing.md`, `docs/downloads-and-release-kpi.md`, `docs/testing-policy.md` and others.
  - `tests/unit/test_code_no_history.py` applies the same rule to `scripts/`, `tests/` and `.github/`.
- **Protected paths.** The tld `protect-paths` hook blocks edits to `.github/workflows/**` and `.tld/toolkit.yaml` by default. `hooks.protect_paths_allow` is read only from the profile on `main` (`tld/guard/_common.py`, `list_key`).
  - `tld-release.yml` is a guarded workflow: it is writable only while `tld guard allow-workflow-install tld-release.yml` is set, for 15 minutes.
  - The `no-git-tag` hook lets a session dispatch `tld-release.yml` with `dry_run` true. A real run needs `tld guard allow-release tld-release.yml`, set for 10 minutes and cleared with `tld guard end-allow`.
- **Next free ADR number:** 0278 (`ls docs/adr` ends at 0277).

## 3. Design

### 3.1 build.yml

Insert these lines between the `on:` block (it ends with `workflow_dispatch:   # allow manual runs from the Actions tab`) and `jobs:`, with one blank line before and after:

```yaml
# Read-only by default; each job below declares exactly what it needs on top of this
# (dedupe: actions: write; finalize-release: contents: write, actions: read).
permissions:
  contents: read
```

Nothing else in `build.yml` changes. Every job already sets its own block, so runtime behaviour is identical.

### 3.2 scripts/release_check.py

The new file is stdlib only. It must run under whatever `python3` the runner has, with no install, because tld-release's check step has no setup-python and no `pip install`. The module docstring says so, and says that the script is `tld-release.yml`'s `check_script`, citing ADR 0278.

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

`main` takes one positional argument, `version` (help: `Version to check, e.g. 7.1.0 or 7.1.0a1 (no leading v)`). Steps, in this order. The first failure stops the script.

1. **Release channel**, `[SCRIPTS / "r2_release.py", "channel", version]`, captured. On a non-zero exit, print its stderr, then print `release check failed: release channel (exit <n>)` to stderr and return `<n>`. On success `channel = stdout.strip()`, printed as `channel: <channel>`.
2. **Release notes**, only when `channel == "stable"`: `[SCRIPTS / "changelog_section.py", version]`, captured. A failure is handled like step 1, with the step name `release notes`. On success, print the notes and call `write_summary(version, notes)`. Never pass `--allow-unreleased`.
3. **Tag checks**, `[SCRIPTS / "tag_release.py", version, "--no-fetch", "--check-only"]`, not captured, so its output streams. A failure is handled like step 1, with the step name `tag checks`.
4. Print `release check passed for v<version> (<channel>)` and return 0.

The script is called with `--no-fetch` because tld-release has just fetched `origin/main` into the clone, and the clone has no credentials left. Then `tag_release.py`'s "local main is origin/main" check compares `github.sha` against what was fetched, so a `main` that moved after the dispatch is refused. Rejected alternative: letting `tag_release.py` fetch. That needs network access to the remote from a token-less clone, and it adds nothing.

Rejected alternative: having the script also check for a successful `build.yml` pre-flight run on the SHA through the Actions API. The script has no token, and an unauthenticated API call adds rate-limit flakiness to an irreversible step. The pre-flight stays a documented `/cut-release` step (§3.3).

**`tests/unit/test_release_check.py`** imports the script by file path, the same way `tests/unit/test_tag_release.py:22-26` does. It fakes `subprocess.run` with `monkeypatch`, recording argv and returning `CompletedProcess` objects. Tests:
- `test_stable_runs_channel_notes_and_tag_checks_in_order`
- `test_prerelease_skips_the_release_notes_step`
- `test_tag_checks_run_with_no_fetch_and_check_only`
- `test_a_failing_channel_step_stops_everything` (returns that exit code; no later argv recorded)
- `test_a_failing_notes_step_stops_before_the_tag_checks`
- `test_a_failing_tag_check_returns_its_exit_code`
- `test_notes_are_appended_to_the_step_summary` (a `tmp_path` file, `GITHUB_STEP_SUMMARY` set with `monkeypatch.setenv`)
- `test_no_summary_is_written_when_the_variable_is_unset`
- `test_never_passes_allow_unreleased`
- `test_an_invalid_version_fails_at_the_channel_step`: no fake; the real `r2_release.py channel nope` exits 1.

### 3.3 scripts/tag_release.py `--check-only`

- Add `--check-only` (help: `Run every check but create no tag -- what scripts/release_check.py runs inside tld-release.yml`). Put it in an `argparse` mutually exclusive group with `--push`.
- In `main()`, right after `check_sequential(...)` succeeds and when `args.check_only` is set: print `checks passed for <name> at <short head>; no tag created (--check-only)` and return 0, without calling `create_tag`.
- Docstring edits:
  - Line 4: "which `release.yml` does by running this script (docs/releasing.md, ADR 0185)" becomes "which `.github/workflows/tld-release.yml` does after running these checks through `scripts/release_check.py` (docs/releasing.md, ADR 0278)".
  - Lines 35-37: "`release.yml` is the only documented way to cut a release (ADR 0185); running this by hand is for checking a version number." becomes "`tld-release.yml` is the only documented way to cut a release (ADR 0278) and creates the tag itself; running this by hand is for checking a version number."
  - The `--push` example: replace "(what `release.yml` runs)" with "(a by-hand cut; not the procedure)".
  - Add an example: `python3 scripts/tag_release.py 4.2.0 --no-fetch --check-only` with the result line `-> checks pass -> no tag (what scripts/release_check.py runs)`.
- New tests in `tests/unit/test_tag_release.py`, in the `main()` class next to `test_creates_local_tag_without_pushing`:
  - `test_check_only_creates_no_tag`
  - `test_check_only_and_push_are_mutually_exclusive` (SystemExit with code 2)
  - `test_check_only_still_fails_a_sequential_gap`

**Rejected:** a post-tag "did `build.yml` start" poll in `tld-release.yml`.
- Why rejected: `tld-release.yml` is a tld template, and a local edit beyond its params would show as drift in every `repo-admin` audit.
- What replaces it: `/cut-release` and `docs/releasing.md` tell whoever cuts to confirm a `push` run of `build.yml` and of `publish-pypi.yml` for the tagged commit within a few minutes. If neither appears, the tag exists but nothing started. The owner deletes the tag and cuts again.
- Why the risk stays low: `TLD_RELEASE_TOKEN` is an owner token, not `GITHUB_TOKEN`, so the tag starts both workflows. That is ADR 0021's invariant.

### 3.4 Workflow and policy files

- Delete `.github/workflows/release.yml` (`git rm`).
- In `.github/workflows/tld-release.yml`, change line 145 from `          CHECK_SCRIPT: ""` to `          CHECK_SCRIPT: "scripts/release_check.py"`. Nothing else changes. The worker runs `tld guard allow-workflow-install tld-release.yml` right before the edit and `tld guard end-allow` right after it. Both are the documented route (the tld repo-admin playbook, "committing it from this checkout").
- In `.tld/admin-policy.yaml`, the release entry becomes `params: { tag_prefix: "v", version_style: pep440, create_release: "false", check_script: scripts/release_check.py }`.
- `.tld/toolkit.yaml` `ci.dispatchable` replaces the `release.yml` row with:

  ```yaml
      - workflow: tld-release.yml
        use_when: Cutting a release tag (never for real from a session without the maintainer's say-so)
        notes: >-
          Inputs `version` (always explicit; never leave it empty), `dry_run` (default true); never pass `skip_checks`.
          Runs on `main` only; the owner approves the tld-release environment for every run. A real run from a
          session needs the owner's yes on the tld board, then `tld guard allow-release tld-release.yml` first and
          `tld guard end-allow` after. Dispatch the dry run, report it, wait for the yes. /cut-release is the procedure.
  ```

- No `release:` section is added to `.tld/toolkit.yaml`. Its `version_files` key needs a file that holds the version, and this repository has none: `setuptools_scm` reads the git tags. So `/tld:release` cannot serve this repository and `/cut-release` stays. ADR 0278 records this.

### 3.5 The release docs

- **`docs/releasing.md`, "Cutting a release"** (`:20-75`). Rewrite the paragraphs that name `release.yml`, keeping the section's heading and anchor (`#cutting-a-release`; `CLAUDE.md` links it). The new text states:
  1. Cutting a release is a tag, not a commit: dispatch `tld-release.yml` on `main` with `version` (PEP 440 short form, always explicit) and `dry_run` (default true). Never pass `skip_checks`. `prerelease` has no effect here, because `create_release` is false and `build.yml` decides the channel from the tag.
  2. `tld-release.yml` is the only way to cut a release.
     - It runs in the `tld-release` environment, which admits only `main`, and the owner approves every run.
     - It creates an annotated tag through the REST API with `TLD_RELEASE_TOKEN`, an owner token stored as that environment's secret.
     - The tld-tags ruleset (`rulesets.tags` in `.tld/admin-policy.yaml`) lets no workflow token create a tag.
     - A person with the admin role could still push a tag by hand. That is not the procedure.
     - Link ADR 0278 as plain text (the file arrives in the last phase).
  3. The steps the run takes, in order:
     - decide the tag;
     - check that the commit is green: every check run on `main`'s tip must be finished and green, so a red, queued or still-running run blocks, including a `build.yml` pre-flight that is still running, a scheduled run, and the self-hosted `deploy-qa-server` job when that runner is offline;
     - `scripts/release_check.py`, which runs `r2_release.py channel`, `changelog_section.py` (stable only, with the body that will ship written to the run summary) and `tag_release.py --no-fetch --check-only`;
     - show GitHub's generated notes, which do not ship;
     - on a real run, create the tag.
  4. `TLD_RELEASE_TOKEN` must not be `GITHUB_TOKEN`. Keep the existing recursion-guard explanation and the ADR 0021 link. The workflow does not check afterwards that `build.yml` started, so whoever cuts confirms a `push` run of `build.yml` and `publish-pypi.yml` for the tagged commit. If neither appears, the tag exists and nothing started: the owner deletes the tag and cuts again.
  5. If `main` moves while the run waits for approval, `tag_release.py` refuses (local `main` is not `origin/main`). Dispatch again.
  6. Pre-flight: keep the paragraph and the ADR 0030 link, but name `tld-release.yml` and its dry run instead of `release.yml`. Add one sentence: the green check refuses while the pre-flight is running or red, but cannot notice a pre-flight that was never dispatched.
  7. Relay check: "before dispatching `tld-release.yml`".
- **`docs/release-testing.md`.**
  - Gate 6 (`:52-54`) becomes: "**`tld-release.yml` dry run.** Dispatch it on `main` with `version` and `dry_run: true` (the default) and approve the `tld-release` environment. It checks that every check on the commit is green, runs `scripts/release_check.py` (the channel, the stable release notes, `tag_release.py`'s checks without creating a tag) and shows the notes. It builds and tags nothing."
  - Gate 7 (`:55-56`) becomes: "**Cut.** Re-dispatch with `dry_run: false` and approve again. Then confirm that `build.yml` and `publish-pypi.yml` started for the tagged commit; the workflow does not check this itself."
  - Evidence item 1 (`:203`): "the commit, the `build.yml` pre-flight run and the `tld-release.yml` dry-run URL;".
- **`docs/downloads-and-release-kpi.md:13-21`.** The diagram's first line becomes `tld-release.yml (tag vX.Y.Z)  ──►  build.yml / publish-pypi.yml`. The label is 4 characters longer than `release.yml`, so add 4 spaces in front of every following line of that block that starts with spaces and a `│`, `├` or `└` (the six lines before the empty line). The second block (`privacyfence.eu/download/ ...`) is unchanged.
- **`.claude/commands/cut-release.md`.** Same front matter, with the description `Pre-flight and cut a release tag through tld-release.yml`. The body:
  1. Keep "If no version was given, stop and ask", with its reason.
  2. State that the container cannot push `refs/tags/*`, so the tag is cut by `.github/workflows/tld-release.yml`.
  3. Keep the four "report the state" bullets unchanged.
  4. Keep the `build.yml` pre-flight section and the relay check, naming `tld-release.yml` where they named `release.yml`. Add that the pre-flight must have finished, because tld-release's green check refuses a commit with a running or failed check run.
  5. **Dry run.** Once the pre-flight is green, dispatch `tld-release.yml` on `main` with `version: <version>` and `dry_run: "true"`. Never leave `version` empty and never pass `skip_checks`. The owner approves the `tld-release` environment in GitHub before the job starts. Report the run URL. For a stable version, report the notes from the "Run the project's release check" step's summary; the "Show the release notes" step lists GitHub's generated notes, which do not ship. If "Check that the commit is green" fails, list the check runs it names and stop: a red or queued run on `main`'s tip is fixed or re-run, never skipped. If `tag_release.py` says local `main` is not `origin/main`, `main` moved while the run waited for approval: report it and dispatch the dry run again.
  6. **Real run.** Only after the owner's yes on the tld board, which tld's pr-steward policy requires for any real release dispatch from a session. Ask with `tld board ask` (the session's tld runtime says how to post and wait), with one `choice` question, id `cut`, titled `Release <version>: cut for real?`, its text being the dry-run URL and the channel, and options `real` (recommended) and `stop`. A yes typed in chat alone is not enough. On `real`: run `tld guard allow-release tld-release.yml`, dispatch with `dry_run: "false"`, then always run `tld guard end-allow`, also when the dispatch failed. The owner approves the environment again. On `stop`: report that nothing was cut.
  7. **After a real cut.** List `build.yml` and `publish-pypi.yml` runs. A `push` run on the tagged commit must appear for each within 5 minutes. If none does, tell the maintainer at once: the tag exists and the release did not start. Never dispatch again for the same version (the tag already exists). Report the tag, the `build.yml` run URL and the `publish-pypi.yml` run URL.

### 3.6 Plans stay on main (tld lifecycle)

- **Tests.** `tests/unit/test_docs_links.py` and `tests/unit/test_docs_references_exist.py` each get:

  ```python
  _PLAN_RECORD = re.compile(r"^docs/\d+/(?:plan|deviations)\.md$")

  def _is_plan_record(rel: str) -> bool:
      """A tld plan (`docs/<issue>/plan.md`) and its deviations file: a record of one change, kept as written."""
      return bool(_PLAN_RECORD.match(rel))
  ```

  - In `test_docs_links.py`, `_is_plan_doc(path)` also returns true when `_is_plan_record(path.relative_to(REPO_ROOT).as_posix())`, so the links of plan records are not checked. Extend its docstring with that sentence.
  - In `test_docs_references_exist.py`, `_references()` skips a file for which `_is_plan_record(rel)` is true, exactly like the `_EXEMPT` prefixes. The module docstring gains: "A tld plan record (`docs/<issue>/plan.md`, `docs/<issue>/deviations.md`) is skipped entirely: it records one change as it was planned, and a later rename must not force editing it."
  - Tests:
    - in `test_docs_links.py`, `test_numbered_plan_records_are_skipped`: true for `docs/1093/plan.md` and `docs/1093/deviations.md`; false for `docs/1093/notes.md`, `docs/x/plan.md`, `docs/adr/1093/plan.md` and `docs/plan.md`;
    - in `test_docs_references_exist.py`, `test_a_plan_record_is_not_checked` (the same cases against `_is_plan_record`).
- **Docs.** The lifecycle in one sentence, used by every doc below: *a plan is a GitHub issue, or `docs/<issue>/plan.md` with `docs/<issue>/deviations.md` written on the feature branch; it reaches `main` with the feature PR and stays there as the record of why the change was made, and nobody edits it after that; its decisions become ADRs before it merges.*
  - **`CONTRIBUTING.md:16-18`.** Replace the sentence "`plan/` branches hold a plan document while its work is open (see [Decisions, plans and ADRs](#decisions-plans-and-adrs)) and are never PR'd themselves." with "A plan is written on its feature branch as `docs/<issue>/plan.md` (see [Decisions, plans and ADRs](#decisions-plans-and-adrs)) and reaches `main` with that branch's PR."
  - **`CONTRIBUTING.md:55-56`** (Plans bullet): "**Plans** (what we are about to do, and in what order) are a GitHub issue, or `docs/<issue>/plan.md` with its `deviations.md` (what the implementation did differently). They are written on the feature branch, reach `main` with the feature PR, and stay there as the record of why the change was made; they are not edited after that."
  - **`CONTRIBUTING.md:63-66`.** "**A plan's decisions become ADRs before it merges.** The PR that brings a plan to `main` adds or amends an ADR for every decision the plan made — anything hard to reverse, touching a trust boundary or the release/distribution path, or rejecting an alternative for a non-obvious reason — or says in its description that the plan made none."
  - **`CONTRIBUTING.md:69-70`.** "ADRs link to issues, PRs, commits and source files. A plan under `docs/<issue>/` may be cited as the record of a change, but an ADR states its decision in full and never relies on the plan to explain it."
  - **`docs/adr/README.md`** (last phase):
    - line 8: "Plans come and go" becomes "Plans record one change as it was planned".
    - Table row Plan: Home becomes "A GitHub issue, or `docs/<issue>/plan.md` with `deviations.md`, written on the feature branch". Lifecycle becomes "Kept. Reaches `main` with the feature PR and stays as the record of the change; not edited after that. Its decisions are extracted here **before** it merges".
    - Rule 4: "**Link only to things that last.** Issues, PRs, commit SHAs, source files, other ADRs, and a plan under `docs/<issue>/`. A top-level `docs/*-plan.md` from before plans were kept was deleted when its work landed: to cite one, give the commit that deleted it and the path, e.g. `git show 96cd5af4^:docs/https-connector-refactor-plan.md`."
    - Rule 5: "**A plan reaching `main` is gated on this directory.** The PR that brings a plan to `main` either adds or amends the ADRs for every decision the plan contained, or states in its description that the plan contained none." The rest of the rule (the §2.7 link) is unchanged.
  - **§2.7 row** (`docs/coding-and-testing-guidelines.md:366-369`), **PR template** (`.github/pull_request_template.md:29-31`) and **`.tld/toolkit.yaml` `dod_manual` row 2.** Replace "A PR that deletes a plan document extracts that plan's decisions into ADRs first" (toolkit: "A deleted plan document needs its decisions extracted into ADRs first") with "A PR that brings a plan document (`docs/<issue>/plan.md`) to `main` extracts that plan's decisions into ADRs first" (toolkit: "A plan document reaching `main` (`docs/<issue>/plan.md`) needs its decisions extracted into ADRs first"). The rest of each row is unchanged.

### 3.7 tld wording

- **`.tld/toolkit.yaml:1-3`** becomes:

  ```yaml
  # tld project profile (the tld plugin's profile reference documents every key).
  # verify.* mirrors docs/coding-and-testing-guidelines.md §2.7: change both together.
  # The tld plugin comes from the Claude Project's plugins, not from a SessionStart hook.
  ```

- **`CLAUDE.md`, "## Commands and skills".** Replace the whole section, from its heading up to "## Parallel sessions & worktrees", with:

  ```markdown
  ## Commands and skills

  The workflow commands (`/tld:make-plan`, `/tld:implement`, `/tld:dod`, `/tld:review` and the rest) come
  from the tld plugin, enabled in the Claude Project; the plugin's own playbooks say what each one does. The
  project facts they read are in `.tld/toolkit.yaml`; ADR 0279 says why.

  - `.claude/skills/steward/SKILL.md` — PrivacyFence-specific steward policy, on top of the plugin's
    pr-steward policy. The workflows a session dispatches instead of running locally are
    `ci.dispatchable` in `.tld/toolkit.yaml`.
  - `/cut-release` (`.claude/commands/cut-release.md`) runs the `build.yml` pre-flight that
    [`docs/releasing.md`](docs/releasing.md#cutting-a-release) requires, then dispatches
    `tld-release.yml`.
  - `/qa-record` records a connector's live QA fixture on the self-hosted runner.
  - **Plans.** `/tld:make-plan` writes a plan to `docs/<issue>/plan.md` on its feature branch, and
    `/tld:implement` adds `docs/<issue>/deviations.md` and opens one PR to `main`, where both stay as the
    record of the change (ADR 0280; the lifecycle is in [`CONTRIBUTING.md`](CONTRIBUTING.md#decisions-plans-and-adrs)).
    Steps only the user can do (third-party console setup, secrets, real-device checks) go only into the
    manifest's `manual_before` and `manual_after`, never between phases.
  - **New connectors.** A new connector is two PRs, connect then tools, and `/tld:make-plan` plans them that
    way with the browser connection on the QA server between them as a manual step. The procedure is in
    [`docs/coding-and-testing-guidelines.md`](docs/coding-and-testing-guidelines.md#3-adding-a-connector), "Adding a connector".
  ```

  The last phase turns "ADR 0279" and "ADR 0280" into links once the files exist.
- **`.claude/skills/steward/SKILL.md`.**
  - In the description, "the devflow `pr-steward` skill" becomes "the tld plugin's pr-steward policy".
  - Line 9, "sits on top of the `pr-steward` skill", becomes "sits on top of the tld plugin's pr-steward policy".
  - Line 72, "Dispatch `release.yml` (dry run first) instead.", becomes "Use `/cut-release`, which dispatches `tld-release.yml` (dry run first)."
- **`docs/testing-policy.md:304`.** "**`/dod`** (from the devflow toolkit; its rows are `verify` in `.tld/toolkit.yaml`)" becomes "**`/tld:dod`** (from the tld plugin; its rows are `verify` in `.tld/toolkit.yaml`)".
- **ADR index row 0132** (last phase): its Status cell becomes "Superseded by 0279". Its title cell is left alone: titles quote the ADR.

### 3.8 The protected-path unlock

`manual_before` item `mb1-unlock-protected-paths` has the owner merge this to `main` (only an allow list on `main` counts):

```yaml
hooks:
  protect_paths_allow:
    - .github/workflows/build.yml
    - .github/workflows/release.yml
    - .tld/toolkit.yaml
```

The new key goes next to the existing `plan_gate` and `board_gate` keys. The retirement phase removes the `protect_paths_allow` key from `.tld/toolkit.yaml` as its very last edit. The removal takes effect when the feature PR merges, so `main` is protected again from then on. Rejected alternative: leaving the allow list in place. It would keep `build.yml` and the profile editable by every later session.

### 3.9 The ADRs (last phase)

Each uses the template in `docs/adr/README.md`, has the Status `Accepted — <the date the phase runs, YYYY-MM-DD>` and is 40-80 lines long. The files go under `docs/adr/`.

- **0278**, file `0278-a-release-tag-is-created-only-by-tld-release-yml.md`. Title: "a release tag is created only by `tld-release.yml`".
  - Status lines: "Supersedes 0185." and "Amends 0021: the token is `TLD_RELEASE_TOKEN` in environment `tld-release`, and the tag is an annotated one created through the REST API."
  - Context: `release.yml` and `tld-release.yml` coexisted after the tld migration.
  - Decision:
    - `tld-release.yml`, rendered by `tld ops repo-admin` from `.tld/admin-policy.yaml`, is the only route to a `v*` tag;
    - its `check_script` is `scripts/release_check.py` (channel, stable notes, `tag_release.py --no-fetch --check-only`);
    - the tld-tags ruleset and the owner-approved `tld-release` environment replace the `v*` admin-role ruleset and the `release` environment.
  - Alternatives:
    - keep `release.yml` (two routes to one tag);
    - edit `tld-release.yml` beyond its params to keep the post-tag `build.yml` poll (drift from the template in every audit; the poll moves to `/cut-release`);
    - check the `build.yml` pre-flight from the check script (no token, unauthenticated API flakiness);
    - a `release:` profile section with `/tld:release` (needs `version_files`, and the version comes from `setuptools_scm` tags).
  - Consequences: the first tld-made tag is annotated where recent ones were lightweight; `setuptools_scm`, `r2_release.py check-tag` and `release_run_guard.py` are unaffected. The green check blocks on any red or queued check run on `main`'s tip. ADR 0030's pre-flight is still a procedure, not a gate. ADR 0023's preview now comes from `release_check.py`.
  - Verification: `tests/unit/test_release_check.py` and `tests/unit/test_tag_release.py`.
- **0279**, file `0279-workflow-commands-come-from-the-tld-plugin.md`. Title: "workflow commands come from the tld plugin".
  - Status: "Supersedes 0132."
  - Decision: `/tld:*` commands, the guard hooks (`protect-paths`, `no-git-tag`, `plan-gate`, `board-gate`) and the status board come from the tld plugin enabled in the Claude Project; the project facts are `.tld/toolkit.yaml`, validated by `tld profile validate`; `.claude/skills/steward/SKILL.md` keeps only PrivacyFence policy; there is no SessionStart install of the plugin.
  - Alternatives: stay on devflow; install tld from a SessionStart hook pinned to a tag (as 0132 rejected for devflow).
  - Consequences:
    - protected files (`.github/workflows/**`, `.tld/toolkit.yaml`) need an allow list on `main` before a session may edit them;
    - a real release dispatch from a session needs `tld guard allow-release`;
    - the plugin version is not pinned in this repository.
- **0280**, file `0280-plans-stay-on-main-as-the-record-of-their-change.md`. Title: "plans stay on `main` as the record of their change".
  - Decision: the lifecycle sentence of §3.6, and that the docs tests skip plan records entirely.
  - Alternatives:
    - keep deleting plans (tld's implement cannot: no phase may touch the plan);
    - move plans to `docs/plans/<n>/` (rejected by the owner: `plan_dir` stays `docs/`);
    - hold plan records to the reference checks (a later rename would force editing a historical record).
  - Consequences: ADR README rules 4 and 5 change as in §3.6; `build_site.py` publishes only top-level `docs/*.md`, so plans never reach the website.
- **Status lines on old ADRs** (README rule 3: one line each, nothing else in the body):
  - 0185 gains "Superseded by [ADR 0278](0278-a-release-tag-is-created-only-by-tld-release-yml.md)."
  - 0021 gains "Amended by [ADR 0278](0278-a-release-tag-is-created-only-by-tld-release-yml.md): the tag is created by `tld-release.yml` with `TLD_RELEASE_TOKEN`."
  - 0132 gains "Superseded by [ADR 0279](0279-workflow-commands-come-from-the-tld-plugin.md)."
  - 0030 and 0023 are unchanged.
- **Index rows** (`docs/adr/README.md`):
  - three new rows at the end, in the existing format, with these Status cells: 0278 "Accepted; supersedes 0185, amends 0021; relates to 0023, 0030", 0279 "Accepted; supersedes 0132", 0280 "Accepted";
  - row 0185's Status cell becomes "Superseded by 0278";
  - row 0021's becomes "Accepted (retroactive); amended by 0185, 0278";
  - row 0132's becomes "Superseded by 0279".

## 4. ADRs

- 0278: a release tag is created only by `tld-release.yml` (supersedes 0185, amends 0021).
- 0279: workflow commands come from the tld plugin (supersedes 0132).
- 0280: plans stay on `main` as the record of their change.

## 5. Manual steps

### Before implementation

**1. mb1-unlock-protected-paths: let sessions edit `build.yml`, `release.yml` and the profile (about 5 minutes).**
1. On your own machine, check out `main` and pull, so you are at the merge of the admin-policy PR or later.
2. Open `.tld/toolkit.yaml`. Under the existing `hooks:` key, next to `plan_gate:` and `board_gate:`, add the `protect_paths_allow` list from §3.8 of this plan (three paths).
3. Run `tld profile validate .tld/toolkit.yaml`. You see `OK`.
4. Commit it (for example `chore: let the release retirement edit build.yml, release.yml and the profile`), push a branch and open a PR. Its CI must pass. Merge it.
5. Done when `git show origin/main:.tld/toolkit.yaml` shows the three `protect_paths_allow` entries.

**2. mb2-tld-release-ready: prove `tld-release.yml` works before `release.yml` is deleted (about 15 minutes).**
1. If you have not applied the admin policy yet, do it on your own machine, signed in as an organisation owner (`gh auth login`, or an owner token in the `GH_TOKEN` environment variable of that shell, never in chat). Run `tld ops repo-admin audit --policy .tld/admin-policy.yaml`: it prints a findings report. Then run `tld ops repo-admin apply --policy .tld/admin-policy.yaml`: it lists the changes it would make and makes none. Then run the same with `--yes`: it makes them. The guide is https://github.com/tkcs-lab/tkcs-lab-devflow/blob/main/docs/guides/admin.md. Afterwards, https://github.com/privacyfence/privacyfence/settings/environments lists `tld-release` with you as required reviewer, and https://github.com/privacyfence/privacyfence/settings/rules lists `tld-tags`.
2. Open https://github.com/privacyfence/privacyfence/settings/environments, then **tld-release**. Under **Environment secrets** there must be `TLD_RELEASE_TOKEN`. If there is not: create a fine-grained token on your account with **Contents: read and write** on `privacyfence/privacyfence` at https://github.com/settings/personal-access-tokens/new, then **Add environment secret** with the name `TLD_RELEASE_TOKEN` and the token as its value. The token goes only there, never into chat, the plan or an ask.
3. Open https://github.com/privacyfence/privacyfence/actions/workflows/tld-release.yml, then **Run workflow**. Choose branch `main`, set `version` to the next pre-release you would cut (for example the next `a` number after the newest tag), and leave `dry_run` ticked.
4. Approve the deployment to `tld-release` when GitHub asks.
5. Done when the run ends green, with "Check that the commit is green" and "Show the release notes" passing. A red green-check means a check run on `main`'s tip is red or queued; the step's log names it.

### After implementation

**3. ma1-dry-run-with-check-script: after the feature PR merges, dry-run `tld-release.yml` with the new check script (about 10 minutes).**
1. Open https://github.com/privacyfence/privacyfence/actions/workflows/tld-release.yml, then **Run workflow** on `main`, with the next pre-release `version` (a stable dry run fails until a release PR has opened its `CHANGELOG.md` section) and `dry_run` ticked. Approve the environment.
2. Open the run. "Run the project's release check" must list `==> release channel`, then `==> tag checks`, and must end with `release check passed for v<version>`.
3. Report back as a PR line: `tld-release dry run with release_check.py: pass|fail <run URL>`.

**4. ma2-remove-the-old-release-settings: delete what only `release.yml` used (about 5 minutes, after the merge).**
1. At https://github.com/privacyfence/privacyfence/settings/environments, open **release** and click **Delete environment**; the list no longer shows it. This also deletes its `RELEASE_TAG_TOKEN` secret.
2. At https://github.com/privacyfence/privacyfence/settings/secrets/actions, delete a repository-level `RELEASE_TAG_TOKEN` if one exists.
3. Revoke the fine-grained token that was `RELEASE_TAG_TOKEN` at https://github.com/settings/personal-access-tokens.
4. At https://github.com/privacyfence/privacyfence/settings/rules, keep `tld-tags` and check that it targets `v*` tags. Delete the older `v*` ruleset that admitted only the admin role.
5. Report back: `old release environment, token and ruleset removed: yes|no`.

## 6. Risks and open questions

- **The plan is on the branch before the docs tests know about it.** p1 runs first, and nothing else may merge before it. If `python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q` fails on `docs/1093/` after p1, the regex in §3.6 is wrong: stop with `status=blocked`.
- **Protected paths.** If a worker editing `build.yml`, `release.yml` or `.tld/toolkit.yaml` is refused with "is a protected path", `mb1` is not on `main` yet: stop with `status=blocked`. Never work around the hook. For `tld-release.yml`, the refusal names `tld guard allow-workflow-install`: run exactly that, then end-allow. After the retirement phase removes the allow list, any later fix to those files on this branch is still allowed, because `main` keeps the list until the PR merges.
- **The green check can block a cut on unrelated runs:** a scheduled run on `main`'s tip that failed, or the self-hosted `deploy-qa-server` job waiting for an offline runner. This is documented, not worked around; `skip_checks` stays forbidden.
- **The first annotated tag.** `git describe --tags` (`setuptools_scm`), `r2_release.py check-tag` and `release_run_guard.py` handle annotated tags (the architect checked the code; 20 old annotated tags exist). Not proven: whether `github.sha` on a push of an annotated tag created through the API is the commit, which `release_run_guard.py` compares. It is the commit, not the tag object, for pushes in general. `/cut-release` step 7 watches every real cut for the `build.yml` and `publish-pypi.yml` runs, and a pre-release is preferred for the first one.
- **Token scope.** If the real cut fails at "Publish the tag" with a 403 that mentions workflows, the fine-grained token also needs **Workflows: write**. That is a settings fix for the owner, found at the first real cut.
- **`ma1` happens after the merge**, because tld-release runs only on `main`. The PR therefore carries it as an unchecked item that is ticked after merging. Before the merge, the script is proven by `tests/unit/test_release_check.py` and by p3's local run against a clone.
- **ADR numbers.** If another PR takes 0278-0280 first, renumber in the retirement phase (README rule 6) and update every "ADR 0278/0279/0280" mention in this branch. That mechanical rename is recorded as a deviation.

## Implementation manifest

```yaml
plan_slug: retire-release-yml
feature_branch: chore/retire-release-yml
tracking_issue: 1093
max_parallel: 2
manual_before:
  - id: mb1-unlock-protected-paths
    title: Merge hooks.protect_paths_allow for build.yml, release.yml and .tld/toolkit.yaml to main
    why: p2, p4 and p7 edit protected files; the protect-paths hook reads its allow list only from main, so without it those phases are refused.
    done_when: git show origin/main:.tld/toolkit.yaml lists .github/workflows/build.yml, .github/workflows/release.yml and .tld/toolkit.yaml under hooks.protect_paths_allow.
  - id: mb2-tld-release-ready
    title: Admin policy applied, TLD_RELEASE_TOKEN set in tld-release, and one green tld-release dry run on main
    why: p4 deletes release.yml; tld-release.yml must already work, or the repository has no way to cut a release.
    done_when: A tld-release workflow_dispatch run on main with dry_run true ended green.
manual_after:
  - id: ma1-dry-run-with-check-script
    title: After merging, dry-run tld-release.yml and see release_check.py pass
    why: The check step runs only on main in a token-less clone; CI cannot run it from the feature branch.
  - id: ma2-remove-the-old-release-settings
    title: After merging, delete the release environment, RELEASE_TAG_TOKEN and the old v* ruleset
    why: They are unused once release.yml is gone, and a leftover tag-writing token is a standing risk.
verify_after_merge:
  - python3 -m pytest tests/unit -q
final_checks:
  - "`tld plan deviations check --file docs/1093/deviations.md --plan docs/1093/plan.md --all` exits 0"
  - ADRs 0278, 0279 and 0280 exist under docs/adr/, are Accepted, and have rows in docs/adr/README.md
  - CHANGELOG.md has no new version heading and no new entry (nothing user-visible changes)
  - "`test ! -e .github/workflows/release.yml` and `git grep -n 'release\\.yml' -- . ':!CHANGELOG.md' ':!docs/adr/' ':!docs/1093/' | grep -v tld-release` prints nothing"
  - "`git grep -n -i 'devflow\\|claude-toolkit' -- . ':!CHANGELOG.md' ':!docs/adr/' ':!docs/1093/' | grep -v 'tkcs-lab-devflow'` prints nothing"
phases:
  - id: p1-plan-record-tests
    title: Docs tests treat docs/<n>/plan.md and deviations.md as plan records
    depends_on: []
    complexity: S
    touches:
      - tests/unit/test_docs_links.py
      - tests/unit/test_docs_references_exist.py
    brief: |
      Design: docs/1093/plan.md section 3.6, "Tests".
      1. In tests/unit/test_docs_links.py add `_PLAN_RECORD` and `_is_plan_record` exactly as in 3.6, and make
         `_is_plan_doc(path)` also return True for a plan record (pass `path.relative_to(REPO_ROOT).as_posix()`).
         Extend `_is_plan_doc`'s docstring with: "A tld plan record (docs/<issue>/plan.md, deviations.md) is skipped too."
      2. Add `test_numbered_plan_records_are_skipped` next to `test_only_top_level_plan_docs_are_skipped`, with the
         true and false cases listed in 3.6.
      3. In tests/unit/test_docs_references_exist.py add the same `_PLAN_RECORD` and `_is_plan_record`, and in
         `_references()` skip a file when `rel.startswith(_EXEMPT) or _is_plan_record(rel)`. Add the docstring
         sentence from 3.6 after the paragraph about `docs/*-plan.md`.
      4. Add `test_a_plan_record_is_not_checked` (3.6).
      5. Run `python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q`
         and `ruff check tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py`.
      Stop condition: if either test still reports a path under docs/1093/ after step 3, stop with
      status=blocked and quote the failure; do not widen the regex beyond 3.6.
    acceptance:
      - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q passes
      - python3 -m pytest tests/unit/test_docs_links.py::test_numbered_plan_records_are_skipped -q passes
      - python3 -m pytest tests/unit/test_docs_references_exist.py::test_a_plan_record_is_not_checked -q passes
      - grep -n '_PLAN_RECORD = re.compile' tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py matches twice
  - id: p2-build-permissions
    title: Top-level read-only permissions in build.yml
    depends_on: [p1-plan-record-tests]
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
      Stop condition: if the edit is refused as a protected path, mb1 is not on main: stop with status=blocked.
    acceptance:
      - grep -n '^permissions:' .github/workflows/build.yml matches exactly once, before the line '^jobs:'
      - git diff origin/main -- .github/workflows/build.yml shows only added lines
      - python3 -m pytest tests/unit -q passes
  - id: p3-release-check-script
    title: scripts/release_check.py and tag_release.py --check-only
    depends_on: [p1-plan-record-tests]
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
      2. tests/unit/test_tag_release.py: add the three tests named in 3.3, using `_init_repo_with_remote`.
      3. Create scripts/release_check.py exactly as specified in 3.2 (stdlib only, `from __future__ import
         annotations`, type hints, module docstring naming tld-release.yml's check_script and ADR 0278,
         `if __name__ == "__main__": sys.exit(main())`).
      4. Create tests/unit/test_release_check.py with the ten tests named in 3.2 (`pytestmark = pytest.mark.unit`).
         Fake `subprocess.run` on the imported module with monkeypatch; only
         test_an_invalid_version_fails_at_the_channel_step runs the real scripts.
      5. Local proof, recorded in your report. In the repository run `git fetch -q --tags origin main`. Let
         NEWEST be the output of `git tag --list 'v*' --sort=-v:refname | head -1`; the version is NEWEST without
         the leading v, with its pre-release number plus one (v7.1.0a2 gives 7.1.0a3), or, when NEWEST is stable,
         its patch plus one followed by a1 (v7.0.2 gives 7.0.3a1). Then in a scratch directory run
         `git clone -q --no-local <repo path> chk && cd chk && git fetch -q --tags`, then
         `git checkout -q -B main origin/main && git branch -q --set-upstream-to=origin/main main`,
         copy in scripts/release_check.py and scripts/tag_release.py from your branch, commit them, run
         `git update-ref refs/remotes/origin/main HEAD`, then `python3 scripts/release_check.py <version>`.
         It must end with `release check passed`. Quote the last five lines.
      6. Run `python3 -m pytest tests/unit/test_release_check.py tests/unit/test_tag_release.py -q`, `ruff check scripts tests/unit`.
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
  - id: p4-release-workflows
    title: Delete release.yml, set tld-release's check_script
    depends_on: [p3-release-check-script]
    complexity: S
    touches:
      - .github/workflows/release.yml
      - .github/workflows/tld-release.yml
      - .tld/admin-policy.yaml
    brief: |
      Design: docs/1093/plan.md section 3.4 (only its first three bullets; the toolkit.yaml row is the last phase's).
      1. `git rm .github/workflows/release.yml`.
      2. Run `tld guard allow-workflow-install tld-release.yml`, change line 145 of .github/workflows/tld-release.yml
         from `          CHECK_SCRIPT: ""` to `          CHECK_SCRIPT: "scripts/release_check.py"`, then run
         `tld guard end-allow`. Run end-allow even if the edit failed.
      3. In .tld/admin-policy.yaml add `check_script: scripts/release_check.py` to the release template's params,
         giving `params: { tag_prefix: "v", version_style: pep440, create_release: "false", check_script: scripts/release_check.py }`.
      4. Run `tld ops repo-admin validate-policy .tld/admin-policy.yaml` and `python3 -m pytest tests/unit -q`.
      Stop condition: if any edit is refused as a protected path, stop with status=blocked and quote the refusal.
    acceptance:
      - test ! -e .github/workflows/release.yml
      - 'grep -n ''CHECK_SCRIPT: "scripts/release_check.py"'' .github/workflows/tld-release.yml matches'
      - 'git diff origin/main --numstat -- .github/workflows/tld-release.yml shows 1 added and 1 deleted line'
      - 'grep -n ''check_script: scripts/release_check.py'' .tld/admin-policy.yaml matches'
      - tld ops repo-admin validate-policy .tld/admin-policy.yaml exits 0
      - python3 -m pytest tests/unit -q passes
  - id: p5-release-docs
    title: Release docs and /cut-release describe tld-release.yml
    depends_on: [p3-release-check-script]
    complexity: M
    touches:
      - docs/releasing.md
      - docs/release-testing.md
      - docs/downloads-and-release-kpi.md
      - .claude/commands/cut-release.md
    brief: |
      Design: docs/1093/plan.md section 3.5 (all of it) and 3.3 ("Rejected: a post-tag poll").
      1. docs/releasing.md: rewrite "Cutting a release" as 3.5 lists, points 1-7, keeping the heading, the
         ADR 0021 and ADR 0030 links, and every paragraph that does not name release.yml unchanged.
         Name ADR 0278 in plain text, without a link.
      2. docs/release-testing.md: gates 6 and 7 and evidence item 1 with the literal text of 3.5.
      3. docs/downloads-and-release-kpi.md: the diagram change of 3.5. Check that the `│`, `├` and `└` characters
         line up under the arrow's column in the rendered block.
      4. .claude/commands/cut-release.md: the procedure of 3.5, points 1-7, in the file's existing voice.
      5. These are contributor docs under tests/unit/test_docs_no_history.py: no `#<number>`, no "Phase <n>",
         no "P<n>", no "since v<x>". Run `python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q`.
      Stop condition: if a paragraph you must change also states a rule this plan does not mention, keep the rule,
      reword only the workflow name, and list it in your Deviations line.
    acceptance:
      - grep -n 'release\.yml' docs/releasing.md docs/release-testing.md docs/downloads-and-release-kpi.md .claude/commands/cut-release.md | grep -v tld-release prints nothing
      - grep -n 'tld guard allow-release tld-release.yml' .claude/commands/cut-release.md matches
      - grep -n 'skip_checks' docs/releasing.md .claude/commands/cut-release.md matches in both files
      - grep -n 'check-only' docs/releasing.md matches
      - grep -n 'RELEASE_TAG_TOKEN' docs/releasing.md .claude/commands/cut-release.md prints nothing
      - grep -n 'tld board ask' .claude/commands/cut-release.md matches
      - python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py -q passes
  - id: p6-tld-lifecycle-docs
    title: Contributor docs describe tld and the kept-plan lifecycle
    depends_on: [p1-plan-record-tests]
    complexity: M
    touches:
      - CLAUDE.md
      - CONTRIBUTING.md
      - docs/coding-and-testing-guidelines.md
      - .github/pull_request_template.md
      - docs/testing-policy.md
      - .claude/skills/steward/SKILL.md
    brief: |
      Design: docs/1093/plan.md sections 3.6 ("Docs": CONTRIBUTING, §2.7 row, PR template) and 3.7 (CLAUDE.md,
      steward, testing-policy). Do not edit docs/adr/ or .tld/toolkit.yaml; the last phase does.
      1. CONTRIBUTING.md: the four replacements of 3.6, with the literal text given there.
      2. docs/coding-and-testing-guidelines.md §2.7 and .github/pull_request_template.md: the row replacement of 3.6.
         Keep each checklist item's line wrapping style.
      3. CLAUDE.md: replace "## Commands and skills" up to "## Parallel sessions & worktrees" with the block of 3.7.
         Keep "ADR 0279" and "ADR 0280" as plain text (the files do not exist yet).
      4. .claude/skills/steward/SKILL.md: the three edits of 3.7.
      5. docs/testing-policy.md:304: the edit of 3.7.
      6. Run `python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_definition_of_done_drift.py -q`.
      Stop condition: if test_definition_of_done_drift.py fails, the §2.7 edit changed a command span: revert to
      changing only the plan-document wording, and stop with status=blocked if it still fails.
    acceptance:
      - grep -n -i 'devflow\|claude-toolkit\|plan/<slug>\|-plan\.md' CLAUDE.md .claude/skills/steward/SKILL.md docs/testing-policy.md prints nothing
      - grep -n 'deletes a plan document' docs/coding-and-testing-guidelines.md .github/pull_request_template.md prints nothing
      - grep -n 'docs/<issue>/plan.md' CONTRIBUTING.md CLAUDE.md docs/coding-and-testing-guidelines.md .github/pull_request_template.md matches in each file
      - grep -n 'release\.yml' CLAUDE.md .claude/skills/steward/SKILL.md | grep -v tld-release prints nothing
      - python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_definition_of_done_drift.py -q passes
  - id: p7-retirement
    title: ADRs 0278-0280, the ADR README, the profile, and the protected-path lock
    depends_on: [p2-build-permissions, p4-release-workflows, p5-release-docs, p6-tld-lifecycle-docs]
    complexity: M
    touches:
      - docs/adr/0278-a-release-tag-is-created-only-by-tld-release-yml.md
      - docs/adr/0279-workflow-commands-come-from-the-tld-plugin.md
      - docs/adr/0280-plans-stay-on-main-as-the-record-of-their-change.md
      - docs/adr/0021-release-tag-push-never-uses-github-token.md
      - docs/adr/0132-workflow-commands-come-from-the-devflow-toolkit-plugins.md
      - docs/adr/0185-a-release-tag-is-created-only-by-release-yml.md
      - docs/adr/README.md
      - .tld/toolkit.yaml
      - CLAUDE.md
      - docs/releasing.md
    brief: |
      Design: docs/1093/plan.md sections 3.9 (ADRs), 3.6 ("docs/adr/README.md"), 3.7 (toolkit header, index row
      0132), 3.4 (the toolkit.yaml ci.dispatchable row) and 3.8 (the lock).
      1. Check `ls docs/adr` and the open PRs' ADR files: if 0278, 0279 or 0280 is taken, take the next free
         numbers, use them everywhere below, and record the renumbering as a deviation.
      2. Write the three ADRs of 3.9 from the template in docs/adr/README.md, Accepted with today's date (UTC).
      3. Add the one Status line each to ADRs 0185, 0021 and 0132 (3.9). Change nothing else in those files.
      4. docs/adr/README.md: line 8, the Plan table row, rules 4 and 5 (3.6), and the index rows (3.9).
      5. CLAUDE.md: turn "ADR 0279" into [ADR 0279](docs/adr/0279-workflow-commands-come-from-the-tld-plugin.md)
         and "ADR 0280" into [ADR 0280](docs/adr/0280-plans-stay-on-main-as-the-record-of-their-change.md).
         docs/releasing.md: turn the plain "ADR 0278" mentions into [ADR 0278](adr/0278-a-release-tag-is-created-only-by-tld-release-yml.md).
      6. .tld/toolkit.yaml: the header of 3.7, the dod_manual row of 3.6, and the ci.dispatchable row of 3.4.
         Run `tld profile validate .tld/toolkit.yaml` and `python3 -m pytest tests/unit/test_definition_of_done_drift.py -q`.
      7. Last edit of the phase: delete the `protect_paths_allow` key (and only it) from `hooks:` in .tld/toolkit.yaml.
         Run `tld profile validate .tld/toolkit.yaml` again.
      8. CHANGELOG.md gets no entry: nothing user-visible changes.
      9. Run `python3 -m pytest tests/unit -q` and `ruff check .`.
      Stop condition: if `tld profile validate` rejects the profile, stop with status=blocked and quote it.
    acceptance:
      - ls docs/adr/0278-*.md docs/adr/0279-*.md docs/adr/0280-*.md lists three files (or the renumbered ones named in deviations)
      - grep -c -E '^\| \[02(78|79|80)\]' docs/adr/README.md prints 3
      - grep -n 'Amended by \[ADR 0278\]' docs/adr/0021-release-tag-push-never-uses-github-token.md matches
      - grep -n 'Superseded by \[ADR 0278\]' docs/adr/0185-a-release-tag-is-created-only-by-release-yml.md matches
      - grep -n 'Superseded by \[ADR 0279\]' docs/adr/0132-workflow-commands-come-from-the-devflow-toolkit-plugins.md matches
      - grep -n 'protect_paths_allow' .tld/toolkit.yaml prints nothing
      - 'grep -n ''workflow: release.yml'' .tld/toolkit.yaml prints nothing'
      - 'grep -n ''workflow: tld-release.yml'' .tld/toolkit.yaml matches'
      - grep -n -i 'devflow\|claude-toolkit' .tld/toolkit.yaml prints nothing
      - tld profile validate .tld/toolkit.yaml exits 0
      - python3 -m pytest tests/unit -q passes
```
