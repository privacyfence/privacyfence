"""Shared fixtures. Resets module-level singletons that auto_accept.py,
audit_log.py, approval_ui.py, resource_names.py, and web_approval_ui.py use,
so tests don't leak state into each other via import-time globals.

Five of these (auto_accept, audit_log, pii_detector, privacy_filter,
resource_names) are per-*principal* registries as of P6, not bare singletons -- resetting
means clearing every principal's cached instance, not just the local one,
so a test that used principal_scope() directly doesn't leak into the next
test either. approval_ui and web_approval_ui stay true process-wide
singletons by design (see principal.py's own docstring on why) --
still reset the same way as before this phase. download_staging is the
same shape as approval_ui/web_approval_ui (one registry serves every
principal internally, per its own module docstring), reset the same way.

Also carries the one hook implementation the whole tree shares: pytest only
ever collects a hookimpl from a conftest.py/plugin, never from an ordinary
test module -- see ``pytest_runtest_makereport`` below.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from privacyfence import (
    approval_ui,
    auto_accept,
    audit_log,
    daemon_main,
    download_staging,
    gate,
    local_files,
    pii_detector,
    privacy_filter,
    privilege_separation,
    resource_names,
    settings_controller,
    upload_staging,
    web_approval_ui,
)
from privacyfence.web import state_stream
from tests.diagnostics import capture_failure_diagnostics, suite_name_for


def _reset() -> None:
    auto_accept._REGISTRY.reset()
    audit_log._REGISTRY.reset()
    approval_ui._INSTANCE = None
    pii_detector._REGISTRY.reset()
    privacy_filter._REGISTRY.reset()
    resource_names._REGISTRY.reset()
    web_approval_ui._INSTANCE = None
    download_staging._INSTANCE = None
    # ADR 0007: upload_staging is the upload-side mirror of download_staging
    # above, reset the same way for the same reason. local_files'
    # configure_file_bridge() is gate.configure_popup_executor()'s own
    # shape (a startup-set module-level value a test can override) --
    # reset to its documented default so one test's override can't leak.
    upload_staging._INSTANCE = None
    local_files.configure_file_bridge()
    local_files.force_bridge_for_tests(False)
    settings_controller._main_dispatch = None
    state_stream._loop = None
    # #428 Phase 4: privilege_separation caches the parsed marker file for
    # the life of the process (it can't change under a running daemon), so a
    # test that provisions a fake separated layout would otherwise leave that
    # answer cached for every test after it -- including the ones asserting
    # the *un*separated paths.
    privilege_separation.reset_cache()
    # daemon_main._shutdown_event (P10): a test that calls request_shutdown()
    # (directly, or via SettingsController.quit_app()) must not leave it set
    # for the next test's own _wait_for_shutdown() call to find already
    # signaled.
    daemon_main._shutdown_event.clear()
    daemon_main._deferred_warnings.clear()
    daemon_main._last_startup_error = None
    # gate._popup_executor (Phase 0 of the approval-binder plan): sized
    # against the real PendingApprovalRegistry's own max_pending by
    # daemon_main.py's configure_popup_executor() call -- a test that
    # exercises that wiring (e.g. test_daemon_main.py's own web.approvals.
    # max_pending overrides) would otherwise permanently shrink or grow the
    # one process-wide executor every other test's real popups run on.
    # configure_popup_executor() is a no-op once the size already matches,
    # so this costs nothing on every other test.
    gate.configure_popup_executor(gate.DEFAULT_MAX_PENDING)


@pytest.fixture(autouse=True)
def _reset_singletons():
    _reset()
    yield
    _reset()


# Stashes each phase's own outcome (setup/call/teardown) onto the test item
# as ``rep_<phase>`` -- the standard pytest pattern for "did the test body
# itself fail?" from inside a fixture's teardown code. Originally lived only
# in tests/integration/conftest.py (test_browser_smoke.py's own Phase 4.5
# ``_capture_failure_artifacts`` was its only consumer); moved up to this
# repo-wide conftest.py by Phase 10 so tests/system/test_local_mode_system.py
# (outside tests/integration/) can use ``request.node.rep_call`` too, without
# a second, near-duplicate hookimpl in a tests/system/conftest.py.
#
# Also where Phase 10's own CI-diagnostics capture (docs/testing-policy.md's
# Phase 10, tests/diagnostics.py) hooks in: on a failing
# ``packaged``/``system``-marked test, write that test's own environment
# info/installed-file manifest/daemon-and-audit logs under test-results/ (see
# tests/diagnostics.py's own module docstring), and record where they landed
# as an extra report section -- so a CI failure's own output already answers
# item 2's "where its diagnostic artifacts landed", not just "what failed"
# (pytest's own assertion-rewriting already gives the expected-vs-actual
# half of that for every plain ``assert``).
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    setattr(item, f"rep_{rep.when}", rep)
    if rep.when == "call" and rep.failed and (
        item.get_closest_marker("packaged") or item.get_closest_marker("system")
    ):
        tmp_path = item.funcargs.get("tmp_path")
        if tmp_path is not None:
            suite = suite_name_for(item.location[0])
            dest = capture_failure_diagnostics(item.nodeid, Path(tmp_path), suite=suite)
            rep.sections.append((
                "PrivacyFence CI diagnostics (Phase 10)",
                "Failure diagnostics (environment info, an installed-file manifest, and any "
                f"daemon/install/audit logs found under this test's own tmp_path) were written to: "
                f"{dest}\n"
                "In CI, .github/workflows/*.yml uploads the whole test-results/ tree as a build "
                "artifact whenever this job fails -- download it from the failed run's Summary page "
                "rather than re-running locally.",
            ))
    return rep
