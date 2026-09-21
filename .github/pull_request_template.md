## What this changes

<!-- Why the change is needed, not just what it does (CONTRIBUTING.md #3). -->

## Definition of done

The checklist from [`docs/coding-and-testing-guidelines.md` §2.7](../docs/coding-and-testing-guidelines.md#27-definition-of-done-for-a-pr-touching-this-repo),
which is the authoritative copy — if the two ever disagree, that one wins and this file needs
updating. Tick what you ran; strike through anything genuinely not applicable with a one-line
reason rather than leaving it blank.

### Every PR

- [ ] `pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json`
      passes at 100%, and `python scripts/check_coverage_floor.py coverage.json` passes.
- [ ] `ruff check .`, `bandit -c pyproject.toml -r src` and `python3 scripts/mypy_strict_modules.py`
      all pass. A genuine Bandit false positive gets a `# nosec BXXX  # <reason>` at the call
      site, never a suppression in `pyproject.toml`.
- [ ] A user-visible change has a line under `CHANGELOG.md`'s `## [Unreleased]` heading — **not**
      under a concrete `## [X.Y.Z]` heading, which a feature branch must never open (CLAUDE.md,
      "Release notes come from CHANGELOG.md"). Internal-only changes don't need one.
- [ ] Every new/changed tool call still resolves through `gated_call` or an explicit
      always-auto-approve connector, and leaves an audit trail either way.
- [ ] No preview dict carries full content; no log line carries a credential or a
      message/document body.
- [ ] New client code has a matching `<Name>ClientError`; new connector code catches it and
      re-raises as `RuntimeError`.
- [ ] New module-level singletons have a reset added to `tests/conftest.py`.
- [ ] Comments only where the *why* is non-obvious; no restated-*what* comments.

### Only if this PR touches those files

- [ ] **`src/privacyfence/*_client.py` or `src/privacyfence/connectors/**`** —
      `scripts/qa_fixture_recorder.py --check <connector>` run against a real QA account, report
      pasted below.
- [ ] **`web/mcp_dispatch.py`, `web/routes_mcp.py`, `connector.py`'s `ToolSpec`/`ToolParam`, or
      `web/server.py`'s socket-binding/lifecycle** — `pytest tests/integration -v` run locally,
      `test_mcp_daemon_contract.py` still passes.
- [ ] **`web/mcp_auth.py`, `web/server.py`'s `mcp_url` discovery-file writing, or anything under
      `mcpb/shim/src/`** — `pytest tests/integration -v` run locally (needs Node on PATH),
      `test_shim_mcp_contract.py` still passes.
- [ ] **A dependency in `pyproject.toml`** — `scripts/update_dependency_locks.sh` run (needs `uv`)
      and the resulting `requirements/*.lock.txt` committed, or `dependency-audit.yml`'s
      `lockfile-freshness` job fails.

## QA fixture report

<!--
Paste the `qa_fixture_recorder.py --check` report here if the row above applies; delete this
section otherwise.

No live QA credentials to hand (a Claude Code on the web session never has them)? Don't skip the
row — dispatch `connector-live-check.yml` or `qa-record-fixture.yml` and link the run instead of
pasting. See .claude/skills/steward/SKILL.md, "Work that has to leave this machine".
-->

## Notes for review

<!-- Anything a reviewer should look at first, or a decision worth a second opinion. -->
