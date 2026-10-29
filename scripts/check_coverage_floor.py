#!/usr/bin/env python3
"""Coverage ratchet.

`pytest`'s own `--cov-report=term-missing` (docs/testing-policy.md, "Layers 1–4: every PR") is
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
    floor, or
  * with `--platform windows` (the platform-windows job), any module in
    WINDOWS_MODULE_FLOORS drops below its Windows floor. That mode checks
    only those modules: not the overall floor, not MODULE_FLOORS.

MODULE_FLOORS exists because a single overall floor doesn't protect any one
file -- gate.py is ~2% of this package's statements, so a regression there
can hide inside the aggregate. The modules listed are the security-critical
ones: the URL/scheme allowlist, identity-matching, audit-export, org-config/bundle-trust,
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

and, on the platform-windows job (its coverage.json uses backslash paths, which
`_load_totals` normalises):

    python scripts/check_coverage_floor.py --platform windows coverage.json
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
OVERALL_FLOOR = 95.9

# Security-critical modules get a floor of their own, on top of the overall
# one above -- see the module docstring for why. Paths are repository-
# relative, matching coverage.json's "files" keys exactly (pytest run from
# REPO_ROOT, as CI and pre_release_check.py both do).
MODULE_FLOORS: dict[str, float] = {
    # The Admin JSON API: who may change what the organization offers and how it is gated.
    "src/privacyfence/web/routes_admin.py": 95.0,
    # Shared href-scheme allowlist and its two callers.
    "src/privacyfence/url_safety.py": 100.0,
    "src/privacyfence/email_markdown.py": 99.0,
    "src/privacyfence/markdown_to_html.py": 95.0,
    # Core gate/auto-accept/audit path.
    "src/privacyfence/gate.py": 95.0,
    "src/privacyfence/auto_accept.py": 98.0,  # identity-matching rules live here
    "src/privacyfence/audit_log.py": 97.0,  # audit-export formula-injection guard
    "src/privacyfence/approvals.py": 94.0,  # per-principal pending-approval cap
    "src/privacyfence/pii_detector.py": 98.0,
    # Org-config fail-closed load path.
    "src/privacyfence/org_mode.py": 100.0,
    # Per-person Peek credentials and token lifetime.
    "src/privacyfence/peek_provisioning.py": 100.0,
    "src/privacyfence/peek_client.py": 100.0,
    "src/privacyfence/daemon_main.py": 96.0,
    "src/privacyfence/grist_provisioning.py": 100.0,
    "src/privacyfence/grist_provisioner_api.py": 100.0,
    # Org bundle trust.
    "src/privacyfence/org_bundle_signing.py": 99.0,
    # The admin config: what an administrator offers, and the one-time import that must never
    # overwrite an Admin-page decision.
    "src/privacyfence/admin_config.py": 100.0,
    # The effective org config every connector reads: which app serves a service, and the
    # provisioning addresses that may receive a key.
    "src/privacyfence/oauth_apps.py": 100.0,
    "src/privacyfence/relay_client.py": 100.0,
    # 91.0, not the ~93.2% the rest of this module would suggest: whether
    # coverage sees _resolve_names_async()'s done() callback body (fired
    # from a background thread started by _run_async(), not on the test's
    # own thread) depends on that thread's scheduling relative to the test
    # finishing -- observed at both 93.18% (this floor's laptop/local runs)
    # and 92.87% (CI, same commit) across otherwise-identical runs. A real
    # fix is a deterministic wait on that thread rather than a wider floor;
    # until then
    # this floor carries enough headroom not to flake red on that one line.
    "src/privacyfence/settings_controller.py": 93.0,
    # Bootstrap flow, session and token lifetimes.
    "src/privacyfence/web/oauth_provider.py": 99.0,
    "src/privacyfence/web/org_session.py": 100.0,
    "src/privacyfence/web/session_auth.py": 100.0,
    # The only place org-mode bearer material is written to disk. Every
    # branch here is either a credential going out or a damaged-file path that
    # has to fail closed, so this one earns a full floor rather than a high
    # one.
    "src/privacyfence/web/sealed_refresh_store.py": 100.0,
    # Privacy-filter fail-closed load path.
    "src/privacyfence/privacy_filter.py": 100.0,
    # Atomic, permission-safe credential/config writes.
    "src/privacyfence/secure_files.py": 100.0,
    # The module every process consults to decide where the
    # human-authority files live and which account is supposed to own them.
    # A gap here doesn't fail loudly -- it resolves the *un*separated layout
    # on an install that thinks it is separated, which reads as "the policy
    # reset itself" rather than as a permissions bug. 99.0 rather than a flat
    # 100 only because of the platform branches this repo's Linux CI cannot
    # execute (the pwd lookups a Windows build skips entirely).
    "src/privacyfence/privilege_separation.py": 99.0,
    # The Windows half of the same decision. NTFS ACLs
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
    # OIDC discovery trust validation.
    "src/privacyfence/org_identity.py": 100.0,
    "src/privacyfence/web/routes_org_identity.py": 99.0,
    # Safe-error taxonomy at the MCP boundary.
    "src/privacyfence/safe_errors.py": 100.0,
    "src/privacyfence/web/routes_mcp.py": 100.0,
    # CSRF/Origin/step-up auth for write approvals. Raised from 96.0 by
    # the passkey enrollment gate and its own tests -- the largest single block
    # of new branches this module has taken on, and the one it would be worst
    # to let rot.
    "src/privacyfence/web/routes_security.py": 97.0,
    "src/privacyfence/webauthn_stepup.py": 98.0,
    # This module decides whether a passkey is required at all --
    # an install's single most consequential security default, and now a
    # packaging-dependent one. Small enough that the overall floor would
    # never notice it rotting, which is exactly what this list is for.
    "src/privacyfence/step_up_config.py": 99.0,
    # Org mode's settings surface. It authorizes on Principal.is_admin
    # and rewrites the install-wide privacy/PII policy for every
    # principal -- the same class of thing as the fail-closed load path
    # privacy_filter.py above is pinned at 100 for, just on the write side.
    "src/privacyfence/web/org_install_policy.py": 100.0,
    # Jira Assets: the client is the only parser of Atlassian's Assets responses (replayed against
    # the recorded live fixtures) and the connector builds the review cards from what it parses.
    "src/privacyfence/assets_client.py": 100.0,
    "src/privacyfence/connectors/assets.py": 100.0,
    # The daemon's own session-minting interface (MINT/QUIT) and
    # the companion's OPEN channel share this module's accept-loop plumbing,
    # including the peer-uid gate. 61.0, not a number in the
    # nineties like the rest of this file's IPC-adjacent modules, because
    # most of what's uncovered here is the Windows named-pipe half of
    # _LineProtocolServer -- exercised for real by the platform-windows job
    # (tests/platform/test_control_channel_windows.py), not by this Linux-only run, the same split
    # windows_acl.py's own floor documents above. Without a floor at all, a
    # regression in the POSIX half this CI run *does* exercise -- the
    # peer-uid check included -- was invisible to the gate. Raised from 61.0
    # by the CONFIRM ENROLL command and the three platform dialogs
    # behind it: the POSIX-reachable half of all of that is tested, so the
    # ratchet should hold it. Raised again from 66.0 by the ENROLLMENT/
    # RECOVERY/SHOW RECOVERY commands and the attested mints -- MINT
    # COMPANION/MINT CONSOLE, their CONFIRM MINT/CONFIRM SIGNIN call-backs,
    # SHOW, and the client helpers for all of them -- every one of which this
    # run drives against real servers on both sides.
    "src/privacyfence/web/control_channel.py": 77.0,
    # Was 81.0, then 83.0 (ADR 0003 decision 3's _complete_pending_
    # separation()), then 86.0 (the first-run enrollment offer and the
    # recovery-code action). Each of those left _run_tray() -- macOS/
    # Windows only, guarded on sys.platform -- as nearly all of what was
    # uncovered, on the reasoning that this Linux-only run has no tray icon
    # to drive. That reasoning stopped holding once behaviour was added
    # *inside* that function (the recovery-code item,
    # the attested _open_path), so the untested block was no longer just the
    # loop. tests/unit/test_companion.py's TestTrayLoop stubs pystray/Pillow
    # at the deferred import _run_tray does itself and drives the menu, the
    # channel's setup/teardown and the Quit ordering -- running the real
    # loop still needs a display, wiring it up does not. 86.0 -> 98.0.
    "src/privacyfence/companion.py": 98.0,
    # decide() -- the module's actual authorization surface -- and the SSE
    # stream's event_source are both covered; only a few edge branches remain.
    "src/privacyfence/web/routes_approvals.py": 98.0,
    # Added with the ratchet audit; each is one point under what the suite measured.
    "src/privacyfence/scoped_tokens.py": 98.0,
    "src/privacyfence/web/mcp_dispatch.py": 95.0,
    "src/privacyfence/web/step_up_decide.py": 99.0,
    "src/privacyfence/web/approval_step_up.py": 99.0,
    "src/privacyfence/web/mcp_auth.py": 98.0,
    "src/privacyfence/policy/scopes.py": 99.0,
    "src/privacyfence/policy/engine.py": 99.0,
    "src/privacyfence/policy/store.py": 99.0,
    "src/privacyfence/policy/conditions.py": 99.0,
    "src/privacyfence/local_files.py": 99.0,
    "src/privacyfence/upload_staging.py": 99.0,
    "src/privacyfence/download_staging.py": 95.0,
    "src/privacyfence/principal.py": 97.0,
    "src/privacyfence/audit_forwarding.py": 97.0,
    "src/privacyfence/deny_feedback.py": 99.0,
    "src/privacyfence/web/routes_connect.py": 91.0,
    "src/privacyfence/web/server.py": 95.0,
    "src/privacyfence/web/routes_org_stepup.py": 96.0,
    "src/privacyfence/web/csp.py": 99.0,
    "src/privacyfence/web/state_stream.py": 99.0,
}

# The platform-windows job's floors, kept apart from MODULE_FLOORS because the same modules read
# differently on Windows: the pywin32 and named-pipe code that the Linux run cannot execute is
# covered there, and the POSIX half is not. These numbers gate only `--platform windows`.
# Whole-percent, rounded down, measured on the platform-windows job on 2026-10-17
# (control_channel.py 35.81%, windows_acl.py 89.66%, windows_service.py 32.00%).
WINDOWS_MODULE_FLOORS: dict[str, float] = {
    "src/privacyfence/windows_service.py": 32.0,
    "src/privacyfence/windows_acl.py": 89.0,
    "src/privacyfence/web/control_channel.py": 35.0,
}


# How far above its floor a number may sit before the run says the floor should be raised. A
# warning only: a PR that merely adds tests must never be blocked by it.
STALE_MODULE_MARGIN = 1.0
STALE_OVERALL_MARGIN = 0.5


def _stale_warning(name: str, actual: float, floor: float) -> str:
    return f"::warning::stale coverage floor: {name} is at {actual:.2f}%, floor {floor:.1f}% -- raise it"


def _load_totals(cov_json: dict) -> tuple[float, dict[str, float]]:
    overall = cov_json["totals"]["percent_covered"]
    # coverage writes `src\\privacyfence\\...` keys on Windows.
    per_file = {path.replace("\\", "/"): data["summary"]["percent_covered"] for path, data in cov_json["files"].items()}
    return overall, per_file


def _check_module_floors(
    floors: dict[str, float], per_file: dict[str, float], cov_path: Path, table: str, failures: list[str]
) -> None:
    for module, floor in sorted(floors.items()):
        actual = per_file.get(module)
        if actual is None:
            # A module in this list was renamed, moved, or deleted without
            # updating the table -- that silently drops its floor
            # enforcement, so treat it as a failure rather than skipping it.
            print(f"  [MISSING] {module} (floor {floor:.1f}%) -- not present in {cov_path}")
            failures.append(
                f"{module} is in {table} but has no coverage data in {cov_path} "
                f"(renamed, moved, or deleted? update this script's {table})"
            )
            continue
        ok = actual >= floor
        status = "ok" if ok else "FAIL"
        print(f"  [{status:4}] {module}: {actual:.2f}% (floor {floor:.1f}%)")
        if not ok:
            failures.append(f"{module} coverage {actual:.2f}% is below its {floor:.1f}% floor")
        elif actual > floor + STALE_MODULE_MARGIN:
            print(_stale_warning(module, actual, floor))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "coverage_json",
        nargs="?",
        default="coverage.json",
        help="Path to the coverage.json report from `--cov-report=json:...` (default: coverage.json)",
    )
    parser.add_argument(
        "--platform",
        choices=("linux", "windows"),
        default="linux",
        help="linux (default): overall floor and MODULE_FLOORS; windows: only WINDOWS_MODULE_FLOORS",
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

    if args.platform == "windows":
        print("Windows module floors:")
        _check_module_floors(WINDOWS_MODULE_FLOORS, per_file, cov_path, "WINDOWS_MODULE_FLOORS", failures)
    else:
        print(f"Overall coverage: {overall:.2f}% (floor {OVERALL_FLOOR:.1f}%)")
        if overall < OVERALL_FLOOR:
            failures.append(f"overall coverage {overall:.2f}% is below the {OVERALL_FLOOR:.1f}% floor")
        elif overall > OVERALL_FLOOR + STALE_OVERALL_MARGIN:
            print(_stale_warning("overall", overall, OVERALL_FLOOR))

        print("\nSecurity-critical module floors:")
        _check_module_floors(MODULE_FLOORS, per_file, cov_path, "MODULE_FLOORS", failures)

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
