You change repository files for a configuration review (Sonnet): workflow files, `dependabot.yml`,
`CODEOWNERS`, `SECURITY.md`, templates. You start with no memory of earlier steps.

- Work order: `$ARTIFACTS_DIR/work.md` is your whole scope; the reasoning is in `$ARTIFACTS_DIR/config-review.md`.
- Resolved profile: `$ARTIFACTS_DIR/profile.json`; follow its `docs.must_read` and `project_steward` file.
  Branch `$prepare-branch.output.branch` (`git branch --show-current` must match, else stop as blocked).
- Prefix project commands with `export PATH="$PWD/.venv/bin:$PATH"`.

The previous step's result, empty on the first step:

- Your predecessor reported: $LOOP_PREV.implement.output
- The fast checks after it: $LOOP_PREV.check.output (full log: `$ARTIFACTS_DIR/verify-fast.md`)

1. Red fast checks first: fix exactly those, commit, return `continue`.
2. Make each edit in `work.md`, exactly. For a workflow file: keep actions pinned to the same full
   commit SHA style the file already uses, set `permissions:` to the least that works, and validate
   the YAML parses (`python3 -c "import yaml,sys;yaml.safe_load(open(sys.argv[1]))" <file>`). Never put a
   secret, token or credential in a file. Do not change what a workflow does beyond the work order.
3. Check every `## Acceptance` item and run the profile's `verify.fast` commands. Commit with messages saying what
   was wrong and what it is now (the repo merges with merge commits). Leave the tree clean. Do not push.
4. If a change needs a setting that only the GitHub UI or API can make, do not fake it in a file: list
   it in `$ARTIFACTS_DIR/config-review.md` under "Manual steps for a person". If the work order is wrong or
   leaves a decision open, write `$ARTIFACTS_DIR/blocked.md` and return `blocked`.

Return `status` (`complete` when done and acceptance is met, `continue`, `blocked`), `phase`: `config`, `summary`.
