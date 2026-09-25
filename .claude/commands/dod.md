---
description: Run the full §2.7 definition-of-done gate and report a pass/fail table
argument-hint: "[optional: a path to narrow the pytest run]"
---

Run this repo's definition of done — `docs/coding-and-testing-guidelines.md` §2.7 — and report
the result as a table. Run every command from the repo root.

$ARGUMENTS

If arguments were given above, treat them as a narrower pytest target (e.g. `tests/unit/test_gate.py`)
and say so in the report; the coverage ratchet is meaningless on a partial run, so skip it and mark
that row `n/a (partial run)` rather than reporting a number that isn't comparable.

**The blocking gate, in this order** (all four also block in CI — `.github/workflows/tests.yml`):

1. `pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json`
2. `python3 scripts/check_coverage_floor.py coverage.json`
3. `ruff check .` and `bandit -c pyproject.toml -r src` and `python3 scripts/mypy_strict_modules.py`
4. In `mcpb/shim/`: `npm test` and `npm run typecheck`

The whole-tree `mypy src/privacyfence` run is informational only (`continue-on-error` in CI) — run
it if you like, but never report it as a failure of this gate.

**Then check the conditional rows against the actual diff** (`git diff --stat origin/main...HEAD`),
and for each one that applies, say whether it has been satisfied — do not silently drop it:

- Touched `src/privacyfence/*_client.py` or `connectors/**`? → a
  `scripts/qa_fixture_recorder.py --check <connector>` report against a dedicated QA account per
  `docs/connector-qa.md` is owed. No live credentials here: see `.claude/skills/steward/SKILL.md`
  and dispatch `connector-live-check.yml` rather than marking the row done.
- Touched `web/mcp_dispatch.py`, `web/routes_mcp.py`, `connector.py`'s `ToolSpec`/`ToolParam`, or
  `web/server.py`'s socket-binding/lifecycle? → `pytest tests/integration -v`, and
  `test_mcp_daemon_contract.py` must pass.
- Touched `web/mcp_auth.py`, `web/server.py`'s `mcp_url` discovery-file writing, or `mcpb/shim/src/`?
  → `pytest tests/integration -v`, and `test_shim_mcp_contract.py` must pass.
- Touched a dependency in `pyproject.toml`? → `scripts/update_dependency_locks.sh` (needs `uv`), and
  the resulting `requirements/*.lock.txt` must be committed.
- Touched `cloudflare/downloads/`? → in that directory, `npm test`, `npm run typecheck` and
  `npm run dry-run` must pass.

**Manual review items.** §2.7's other "every PR" rows are judgements no command can make. Read the
diff for each one and report it as `ok`, `needs attention` (with the file and line) or `n/a` —
never PASS, since nothing was run:

- Is there a user-visible change? → it needs a line under `CHANGELOG.md`'s `## [Unreleased]`, never
  under a concrete version heading. Internal-only changes don't need one.
- A decision that is hard to reverse, moves a trust boundary, changes the build/release/
  distribution path, or rejects a non-obvious alternative? → it needs an ADR in `docs/adr/`. A
  deleted plan document needs its decisions extracted into ADRs first, or a PR description saying
  it made none (`docs/adr/README.md`).
- Every new or changed tool call still resolves through `gated_call` or an explicit
  always-auto-approve connector, and leaves an audit trail either way.
- No preview dict carries full content; no log line carries a credential or a message/document
  body.
- New client code has a matching `<Name>ClientError`; new connector code catches it and re-raises
  as `RuntimeError`.
- New module-level state has a reset added to `tests/conftest.py`.
- Comments only where the *why* is non-obvious; no restated-*what* comments.

**Report** one row per check: the command (or the manual item), PASS/FAIL/`n/a` (`ok`/`needs
attention`/`n/a` for a manual item), and for a failure the actual error — not
a paraphrase. Do not fix anything unless I ask; this command reports, it doesn't repair. End with a
one-line verdict on whether this branch is ready to open or update a PR.
