#!/usr/bin/env python3
"""Coverage ratchet (TST-03).

`pytest`'s own `--cov-report=term-missing` (docs/testing-policy.md §1) is
informational only -- nothing before this script gated a merge on coverage
actually staying where it was, so a PR could silently drop coverage on a
security-critical module (an untested new branch in gate.py, an
exception-handling path in oauth_provider.py nothing exercises) and CI would
still go green. This script is the gate: it reads the `coverage.json` report
`pytest --cov-report=json:coverage.json` produces and fails if:

  * overall branch+line coverage (`totals.percent_covered` -- the same
    combined metric pytest-cov's own summary line reports) drops below
    OVERALL_FLOOR, or
  * any module in MODULE_FLOORS drops below its own, individually higher,
    floor.

MODULE_FLOORS exists because a single overall floor doesn't protect any one
file -- gate.py is ~2% of this package's statements, so a regression there
can hide inside the aggregate. The modules listed are the ones Phase 0/1 of
the remediation plan (SEC-01 through SEC-15) touched or added: the URL/
scheme allowlist, identity-matching, audit-export, org-config/bundle-trust,
browser-session and token-lifetime, privacy-filter fail-closed, secure-write,
OIDC-discovery-trust, MCP-boundary-error-taxonomy, and per-principal-cap
code paths.

Every floor below is this script's *initial* value: the module's actual
branch+line coverage the day this ratchet was turned on (2026-09-09,
`pytest -v --cov=src/privacyfence --cov-branch`, full suite, 4584 tests),
rounded down to the nearest whole percent as headroom against harmless
float jitter. A floor only ever moves in one direction, deliberately: if a
PR's own new tests raise a module's coverage, bump that module's number up
in the same PR (that's the point of a ratchet -- it should be raised often,
by whoever earns it); if a PR needs to lower one, that's a real coverage
regression and belongs in the PR description, not a silent edit here.

Usage (same as CI -- see .github/workflows/tests.yml and
scripts/pre_release_check.py):

    pytest --cov=src/privacyfence --cov-branch --cov-report=json:coverage.json
    python scripts/check_coverage_floor.py coverage.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Combined (line + branch) percentage, matching coverage.json's
# totals.percent_covered / pytest-cov's own summary "Cover" column.
#
# One decimal place, not the whole-percent rounding MODULE_FLOORS uses below:
# the "harmless float jitter" that rounding protects against is the
# percentage computation's own floating-point noise, which is negligible at
# this scale (17,000+ statements) -- a real regression big enough to matter
# moves this number by far more than 0.1%. Whole-percent headroom here would
# just let a real, module-sized regression hide inside the aggregate, which
# is the exact failure mode MODULE_FLOORS exists to close for the modules
# listed below; it shouldn't reopen for everything else.
OVERALL_FLOOR = 94.9

# Security-critical modules get a floor of their own, on top of the overall
# one above -- see the module docstring for why. Paths are repository-
# relative, matching coverage.json's "files" keys exactly (pytest run from
# REPO_ROOT, as CI and pre_release_check.py both do).
MODULE_FLOORS: dict[str, float] = {
    # SEC-01: shared href-scheme allowlist and its two callers.
    "src/privacyfence/url_safety.py": 100.0,
    "src/privacyfence/email_markdown.py": 99.0,
    "src/privacyfence/markdown_to_html.py": 95.0,
    # Core gate/auto-accept/audit path.
    "src/privacyfence/gate.py": 91.0,
    "src/privacyfence/auto_accept.py": 94.0,  # SEC-02 identity rules live here
    "src/privacyfence/audit_log.py": 95.0,  # SEC-03 formula-injection guard
    "src/privacyfence/approvals.py": 92.0,  # SEC-15 per-principal cap
    "src/privacyfence/pii_detector.py": 98.0,
    # SEC-04: org-config fail-closed load path.
    "src/privacyfence/org_mode.py": 100.0,
    "src/privacyfence/daemon_main.py": 96.0,
    # SEC-05: org bundle trust.
    "src/privacyfence/org_bundle_signing.py": 98.0,
    # 91.0, not the ~93.2% the rest of this module would suggest: whether
    # coverage sees _resolve_names_async()'s done() callback body (fired
    # from a background thread started by _run_async(), not on the test's
    # own thread) depends on that thread's scheduling relative to the test
    # finishing -- observed at both 93.18% (this floor's laptop/local runs)
    # and 92.87% (CI, same commit) across otherwise-identical runs. A real
    # fix is a deterministic wait on that thread rather than a wider floor
    # (TST-11); until then
    # this floor carries enough headroom not to flake red on that one line.
    "src/privacyfence/settings_controller.py": 91.0,
    # SEC-06/SEC-12/SEC-13: bootstrap flow, session and token lifetimes.
    "src/privacyfence/web/oauth_provider.py": 99.0,
    "src/privacyfence/web/org_session.py": 100.0,
    "src/privacyfence/web/session_auth.py": 100.0,
    # #402: the only place org-mode bearer material is written to disk. Every
    # branch here is either a credential going out or a damaged-file path that
    # has to fail closed, so this one earns a full floor rather than a high
    # one.
    "src/privacyfence/web/sealed_refresh_store.py": 100.0,
    # SEC-07: privacy-filter fail-closed load path.
    "src/privacyfence/privacy_filter.py": 100.0,
    # SEC-09: atomic, permission-safe credential/config writes.
    "src/privacyfence/secure_files.py": 100.0,
    # #428 Phase 4: the module every process consults to decide where the
    # human-authority files live and which account is supposed to own them.
    # A gap here doesn't fail loudly -- it resolves the *un*separated layout
    # on an install that thinks it is separated, which reads as "the policy
    # reset itself" rather than as a permissions bug. 99.0 rather than a flat
    # 100 only because of the platform branches this repo's Linux CI cannot
    # execute (the pwd lookups a Windows build skips entirely).
    "src/privacyfence/privilege_separation.py": 99.0,
    # #428 Phase 4 (B5c): the Windows half of the same decision. NTFS ACLs
    # are the only thing standing between the agent and the policy/passkey/
    # audit-key files there -- POSIX modes do not exist on that platform --
    # so a gap in the mask arithmetic below means the audit stops reporting a
    # data directory every account on the machine can enumerate.
    #
    # 83.0 rather than a number in the nineties, and deliberately not raised
    # by adding pragmas: this module is half pure logic (every audit
    # function, all of it covered) and half four pywin32 calls that cannot
    # execute on this repo's Linux CI at all. The floor protects the half
    # that can; tests/platform/test_windows_acls.py covers the other half on
    # the platform-windows job, against real ACLs icacls wrote. (Raised from
    # its initial 81.0 by the owner/OWNER RIGHTS resolution that first real
    # Windows run made necessary -- all of it pure, all of it tested.)
    "src/privacyfence/windows_acl.py": 83.0,
    # SEC-11: OIDC discovery trust validation.
    "src/privacyfence/org_identity.py": 100.0,
    "src/privacyfence/web/routes_org_identity.py": 99.0,
    # SEC-10: safe-error taxonomy at the MCP boundary.
    "src/privacyfence/safe_errors.py": 100.0,
    "src/privacyfence/web/routes_mcp.py": 100.0,
    # CSRF/Origin/step-up auth for write approvals.
    "src/privacyfence/web/routes_security.py": 96.0,
    "src/privacyfence/webauthn_stepup.py": 98.0,
    # #400: org mode's settings surface. It authorizes on Principal.is_admin
    # and, since C3e, rewrites the install-wide privacy/PII policy for every
    # principal -- the same class of thing as the fail-closed load path
    # privacy_filter.py above is pinned at 100 for, just on the write side.
    "src/privacyfence/web/org_install_policy.py": 100.0,
    "src/privacyfence/web/routes_org_settings.py": 98.0,
    # #428 B10: the daemon's own session-minting interface (MINT/QUIT) and
    # the companion's OPEN channel share this module's accept-loop plumbing,
    # including the peer-uid gate B10 added. 61.0, not a number in the
    # nineties like the rest of this file's IPC-adjacent modules, because
    # most of what's uncovered here is the Windows named-pipe half of
    # _LineProtocolServer -- exercised for real by the platform-windows job
    # (tests/platform/), not by this Linux-only run, the same split
    # windows_acl.py's own floor documents above. Without a floor at all, a
    # regression in the POSIX half this CI run *does* exercise -- the
    # peer-uid check included -- was invisible to the gate.
    "src/privacyfence/web/control_channel.py": 61.0,
    # _run_tray() (macOS/Windows only, guarded on sys.platform) is nearly
    # all of what's uncovered -- the tray icon this Linux-only run has
    # nothing to drive. 83.0 reflects that split honestly rather than
    # padding it with a pragma; raised from 81.0 by ADR 0003 decision 3's
    # _complete_pending_separation(), which is new code this run does cover
    # in full.
    "src/privacyfence/companion.py": 83.0,
    # The SSE stream's own generator body (approvals_stream's event_source,
    # a poll loop no test here consumes to exhaustion) plus a couple of
    # decide()'s edge branches (the bare-index "choice" coercion, the plain
    # "/" redirect) account for the gap. decide() itself -- the module's
    # actual authorization surface -- is otherwise well covered.
    "src/privacyfence/web/routes_approvals.py": 88.0,
}


def _load_totals(cov_json: dict) -> tuple[float, dict[str, float]]:
    overall = cov_json["totals"]["percent_covered"]
    per_file = {path: data["summary"]["percent_covered"] for path, data in cov_json["files"].items()}
    return overall, per_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "coverage_json",
        nargs="?",
        default="coverage.json",
        help="Path to the coverage.json report from `--cov-report=json:...` (default: coverage.json)",
    )
    args = parser.parse_args(argv)

    cov_path = Path(args.coverage_json)
    if not cov_path.is_file():
        print(
            f"error: {cov_path} not found -- run pytest with "
            "--cov=src/privacyfence --cov-branch --cov-report=json:coverage.json first",
            file=sys.stderr,
        )
        return 2

    cov_json = json.loads(cov_path.read_text())
    overall, per_file = _load_totals(cov_json)

    failures: list[str] = []

    print(f"Overall coverage: {overall:.2f}% (floor {OVERALL_FLOOR:.1f}%)")
    if overall < OVERALL_FLOOR:
        failures.append(f"overall coverage {overall:.2f}% is below the {OVERALL_FLOOR:.1f}% floor")

    print("\nSecurity-critical module floors:")
    for module, floor in sorted(MODULE_FLOORS.items()):
        actual = per_file.get(module)
        if actual is None:
            # A module in this list was renamed, moved, or deleted without
            # updating MODULE_FLOORS -- that silently drops its floor
            # enforcement, so treat it as a failure rather than skipping it.
            print(f"  [MISSING] {module} (floor {floor:.1f}%) -- not present in {cov_path}")
            failures.append(
                f"{module} is in MODULE_FLOORS but has no coverage data in {cov_path} "
                "(renamed, moved, or deleted? update this script's MODULE_FLOORS)"
            )
            continue
        ok = actual >= floor
        status = "ok" if ok else "FAIL"
        print(f"  [{status:4}] {module}: {actual:.2f}% (floor {floor:.1f}%)")
        if not ok:
            failures.append(f"{module} coverage {actual:.2f}% is below its {floor:.1f}% floor")

    if failures:
        print("\nCoverage ratchet failed:")
        for f in failures:
            print(f"  - {f}")
        print(
            "\nAdd or strengthen tests to get back above the floor, rather than "
            "lowering the floor in scripts/check_coverage_floor.py -- see this "
            "script's module docstring."
        )
        return 1

    print("\nCoverage ratchet passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
