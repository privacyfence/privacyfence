# ADR 0147: The whole-tree mypy run is blocking

## Status

Accepted — 2026-10-10. Implemented in the PR that adds this ADR.

## Context

`mypy src/privacyfence` reported 95 errors in 23 files, and the `continue-on-error` step that ran it
let new errors hide among them
([#865](https://github.com/privacyfence/privacyfence/issues/865)). The strict per-module ratchet
in [`scripts/mypy_strict_modules.py`](../../scripts/mypy_strict_modules.py) was already blocking,
but it checks only the modules promoted so far.

## Decision

`mypy src/privacyfence` at `[tool.mypy]`'s settings blocks every merge, alongside the strict
per-module ratchet in [`scripts/mypy_strict_modules.py`](../../scripts/mypy_strict_modules.py),
which is unchanged. Both run in [`tests.yml`](../../.github/workflows/tests.yml) without
`continue-on-error`.

## Alternatives considered

- **Keep the whole-tree run informational** — #865 shows the noise hid new errors: a run that
  always reports a pile of old errors cannot tell a reviewer which ones the PR added.
- **Promote modules to the strict list one at a time, and nothing more** — this leaves most of the
  tree unchecked for years, and every module outside the list can regress unseen meanwhile.

## Consequences

Every PR is held to a type-clean tree at the project's default settings. mypy is not pinned
(`mypy>=1.10` in the `lint` extra), so a new mypy release can turn `main` red. The answer is then a
fix PR, not a return to `continue-on-error`.

## Verification

The mypy steps of [`tests.yml`](../../.github/workflows/tests.yml) and
[`scripts/mypy_strict_modules.py`](../../scripts/mypy_strict_modules.py).

## Related

- [#865](https://github.com/privacyfence/privacyfence/issues/865)
