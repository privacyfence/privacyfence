# ADR 0294: The supported Python floor is 3.11.5

## Status

Accepted — 2026-11-14. Implemented.

## Context

`beautifulsoup4` pulls in `soupsieve`. The releases of `soupsieve` that fix CVE-2026-126190,
CVE-2026-126191 and CVE-2026-126193 (3.0.2 and 4.x) install only on Python 3.11.5 and newer. With
`requires-python = ">=3.11"` the lock files had to carry a split (`soupsieve` 2.10 below 3.11.5), and
`floor-audit` in `dependency-audit.yml` resolves for the declared floor, so it could only choose the
vulnerable 2.10 and failed on every pull request.

## Decision

`requires-python` is `>=3.11.5`. `PYTHON_FLOOR` in `scripts/update_dependency_locks.sh` and the
`--python-version` of the audit workflow's resolutions follow it, and the locks no longer split on
`soupsieve`. The user-facing install text says 3.11.5 or newer.

## Alternatives considered

- **A floor on `soupsieve` for newer Pythons only.** It does not change what the audit resolves for the
  declared floor, so the check stays red.
- **Ignoring the three advisories in `floor-audit`.** It hides a real exposure on 3.11.0–3.11.4 and
  weakens a check that exists to catch exactly this.

## Consequences

- Python 3.11.0–3.11.4 is no longer supported. Those are early patch releases of 3.11, long superseded by
  security releases, and distribution Pythons (Debian 13, Ubuntu 24.04 and newer) are above the new floor.
- A future floor raise for a dependency that needs a newer Python is the same change: this ADR is its
  precedent.
