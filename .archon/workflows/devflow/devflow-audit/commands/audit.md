You are the **security auditor** (Opus). You change no tracked file and send nothing anywhere; you
write one document into `$ARTIFACTS_DIR`.

Scope: `$INPUTS.scope`. If that is empty, the whole repository at `origin/main`
(`git fetch origin main` first; `git log -1 origin/main` is the commit you pin: every `path:line`
refers to it).

Method, from the devflow toolkit's `/review` command (security dimension only; tests and docs are
not your job here): load the `secure-code-review` skill (`devflow:secure-code-review`, otherwise
`skills/secure-code-review/SKILL.md` under the devflow root, found with
`ls -d ~/.claude/plugins/synced/*/devflow ~/.claude/plugins/cache/*/devflow/*`) and the stack skill
for the profile's `stacks`. Read `$ARTIFACTS_DIR/profile.json`, every doc in its `docs.must_read`,
`SECURITY.md`, `NOTICE`, every ADR whose title touches security, trust, auth, secrets, the gate or
the audit log, and the threat-model material under `docs/`. What an ADR decided, or what
`SECURITY.md` puts out of scope, is not a finding.

1. **Inventory**: entry points, trust boundaries (network, CLI, files, environment, webhooks,
   issue and PR text in CI, third-party APIs), where secrets are read and written, where privilege
   changes, what CI runs and with which token permissions, dependencies and how they are pinned,
   container images and installers.
2. **Measure** (never anything that deploys, publishes, sends data out or needs a credential;
   build and cache only under a temporary directory):
   - `bandit -c pyproject.toml -r src` (from `.venv/bin`; prefix with `export PATH="$PWD/.venv/bin:$PATH"`).
   - A dependency audit with a tool the venv or host already has, against the lock files in
     `requirements/` and the npm lockfiles. If none is available, check versions against advisories only where
     you can cite the advisory; otherwise write "not audited" for that manifest.
   - Secrets in history: search `git log -p --all` for the patterns in the skill.
   - `.github/workflows/`: token permissions, `pull_request_target`, untrusted input in `run:`,
     actions pinned to a SHA, secrets reaching fork-triggered jobs.
3. **Audit the code** against the skill's checklist, trust boundary by trust boundary, reading the
   code that matters yourself (use `Explore` subagents for broad sweeps only). For each candidate,
   trace the path from the untrusted input to the sink, and write the path down.

Evidence bar: a finding needs the input, the path, the sink and the missing control, each with
`path:line`; or the measured tool output. "Could be", "might" and pattern matches without a
reachable path are dropped. A hardening suggestion with no attack path is `low` and says so.

Write `$ARTIFACTS_DIR/audit.md`, first line `CONFIDENTIAL: do not commit, file an issue from, or quote publicly.`
Then a header (`review_slug: audit-<date>`, `status: audit-complete`, the pinned commit), the inventory,
what you measured with each tool's result (and what you could not run, with the reason), and the
findings, each:

`### SEC-<n> · <critical|high|medium|low> · <title>` then `Where:` (`path:line`), `Path:` (input to
sink), `Impact:` (who is exposed in local mode and in organization mode), `Fix:` (specific), and
`Acceptance:` (a named test, a grep or a command).

Return `findings` (the count) and `measured` (one line: the tools run and the tools unavailable).
