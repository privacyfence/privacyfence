"""Shared fixtures. Resets module-level singletons that auto_accept.py,
audit_log.py, approval_ui.py, resource_names.py, and web_approval_ui.py use,
so tests don't leak state into each other via import-time globals.

Five of these (auto_accept, audit_log, pii_detector, privacy_filter,
resource_names) are per-*principal* registries, not bare singletons -- resetting
means clearing every principal's cached instance, not just the local one,
so a test that used principal_scope() directly doesn't leak into the next
test either. approval_ui and web_approval_ui stay true process-wide
singletons by design (see principal.py's own docstring on why) and are
reset by clearing the one instance. download_staging is the
same shape as approval_ui/web_approval_ui (one registry serves every
principal internally, per its own module docstring), reset the same way.

Also carries the one hook implementation the whole tree shares: pytest only
ever collects a hookimpl from a conftest.py/plugin, never from an ordinary
test module -- see ``pytest_runtest_makereport`` below.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from privacyfence import (
    admin_config,
    agent_label,
    app_credentials,
    approval_ui,
    auto_accept,
    audit_log,
    daemon_main,
    data_format,
    download_staging,
    gate,
    grist_auth,
    grist_provisioning,
    local_files,
    peek_provisioning,
    pii_detector,
    privacy_filter,
    privilege_separation,
    relay_client,
    resource_names,
    scoped_token_proposals,
    scoped_tokens,
    settings_controller,
    upload_staging,
    web_approval_ui,
    whats_new,
)
from privacyfence.web import control_channel, state_stream
from tests.diagnostics import capture_failure_diagnostics, suite_name_for


_REQUIRE_PACKAGED = "PRIVACYFENCE_REQUIRE_PACKAGED"
_REQUIRE_BROWSER = "PRIVACYFENCE_REQUIRE_BROWSER"
# A browser test may skip for a reason unrelated to the browser (the docs site
# was not built in a job without zensical); only a missing Chromium/Playwright
# is a CI misconfiguration.
_PACKAGED_SKIP_REASON = re.compile(
    r"(?i)no dist/|built yet|not installed|not on PATH|missing after build|could not build"
    r"|needs root|elevated|only meaningful|systemd|not present|chromium|playwright"
)
# Skips a dispatched (unsigned, un-notarized) build legitimately takes -- "is unsigned for this
# build", "is signed but not notarized" -- match none of the above and stay skips.
_BROWSER_SKIP_REASON = re.compile(r"(?i)chromium|playwright")
_BROWSER_COLLECT_SKIP_REASON = re.compile(r"(?i)playwright")


def _skip_reason(longrepr) -> str:
    """The human-readable reason of a skipped report's ``longrepr``."""
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ")
    return str(longrepr)


def _required_run_switch(markers, reason: str) -> str | None:
    """The REQUIRE switch (if any) that turns this skip into a failure."""
    if (
        os.environ.get(_REQUIRE_PACKAGED) == "1"
        and "packaged" in markers
        and _PACKAGED_SKIP_REASON.search(reason)
    ):
        return _REQUIRE_PACKAGED
    if (
        os.environ.get(_REQUIRE_BROWSER) == "1"
        and "browser" in markers
        and _BROWSER_SKIP_REASON.search(reason)
    ):
        return _REQUIRE_BROWSER
    return None


def _reset() -> None:
    auto_accept._REGISTRY.reset()
    audit_log._REGISTRY.reset()
    audit_log._INSTALL_SETTINGS = audit_log._InstallAuditSettings()
    approval_ui._INSTANCE = None
    pii_detector._REGISTRY.reset()
    privacy_filter._REGISTRY.reset()
    resource_names._REGISTRY.reset()
    web_approval_ui._INSTANCE = None
    download_staging._INSTANCE = None
    grist_auth._DISCOVERY_CACHE.clear()
    # The Grist provisioner is a process-wide singleton started by the org server.
    grist_provisioning._INSTANCE = None
    grist_provisioning._LOCKS.clear()
    # ADR 0007: upload_staging is the upload-side mirror of download_staging
    # above, reset the same way for the same reason. local_files'
    # configure_file_bridge() is gate.configure_popup_executor()'s own
    # shape (a startup-set module-level value a test can override) --
    # reset to its documented default so one test's override can't leak.
    upload_staging._INSTANCE = None
    # whats_new caches the parsed changelog for the life of the process.
    whats_new.load_changelog.cache_clear()
    local_files.configure_file_bridge()
    local_files.force_bridge_for_tests(False)
    settings_controller._main_dispatch = None
    daemon_main._bundle_adopted_this_start = False
    state_stream._loop = None
    control_channel._mint_nonces.clear()
    # privilege_separation caches the parsed marker file for
    # the life of the process (it can't change under a running daemon), so a
    # test that provisions a fake separated layout would otherwise leave that
    # answer cached for every test after it -- including the ones asserting
    # the *un*separated paths.
    privilege_separation.reset_cache()
    # daemon_main._shutdown_event: a test that calls request_shutdown()
    # (directly, or via SettingsController.quit_app()) must not leave it set
    # for the next test's own _wait_for_shutdown() call to find already
    # signaled.
    daemon_main._shutdown_event.clear()
    daemon_main._deferred_warnings.clear()
    daemon_main._last_startup_error = None
    # gate._popup_executor: sized
    # against the real PendingApprovalRegistry's own max_pending by
    # daemon_main.py's configure_popup_executor() call -- a test that
    # exercises that wiring (e.g. test_daemon_main.py's own web.approvals.
    # max_pending overrides) would otherwise permanently shrink or grow the
    # one process-wide executor every other test's real popups run on.
    # configure_popup_executor() is a no-op once the size already matches,
    # so this costs nothing on every other test.
    gate.configure_popup_executor(gate.DEFAULT_MAX_PENDING)
    # agent_label's install mode is set once by daemon_main at startup
    # (ADR 0088); a test that starts the daemon in org mode must not leave
    # every later test rendering org-mode labels.
    agent_label.set_local_mode(True)
    # The request a gated call released (ADR 0093) is scoped per tool
    # call by the dispatcher; a test that calls gate._audit directly sets
    # it outside any such scope, so it must not carry into the next test.
    audit_log.set_released_request_id("")
    # The scoped-token store is a process-wide singleton over the data dir (ADR 0164).
    scoped_tokens.set_store(None)
    scoped_token_proposals.set_registry(None)
    # The Peek provisioner (organization mode) is a process-wide singleton; its per-person locks
    # are keyed by principal id and must not carry over.
    peek_provisioning._INSTANCE = None
    peek_provisioning._LOCKS.clear()
    # The relay registration state is process-wide.
    relay_client.reset_for_tests()
    data_format.reset_for_tests()


@pytest.fixture(autouse=True)
def _reset_singletons():
    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _clear_server_environment(monkeypatch):
    # These variables choose the mode and the listener; a developer's shell must never change what
    # a test sees.
    for name in (
        "PRIVACYFENCE_MODE", "PRIVACYFENCE_BIND_HOST", "PRIVACYFENCE_PORT",
        "PRIVACYFENCE_ISSUER_URL", "PRIVACYFENCE_TRUSTED_PROXIES",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_whats_new_state(monkeypatch, request):
    # whats_new writes per-principal state on page views; without this, any test that renders an
    # org page would write users/<id>/whats_new.json into the checkout (paths.data_dir() is the
    # repository root in a source checkout). tmp_path is resolved lazily so tests that never
    # touch it don't pay for a directory.
    monkeypatch.setattr(
        whats_new, "state_path",
        lambda principal: request.getfixturevalue("tmp_path") / "whats_new_state" / principal.id / whats_new.STATE_FILENAME,
    )


# Modules that test the real functions below patch paths.data_dir to tmp_path themselves, so the
# real path functions run and count toward their coverage.
_ADMIN_CONFIG_FIXTURE_EXEMPT = ("test_app_credentials", "test_admin_config", "test_relay_client", "test_data_format")


@pytest.fixture(autouse=True)
def _isolate_admin_config_and_builtin_apps(monkeypatch, request):
    # In a source checkout paths.data_dir() is the repository root, so a test that forgot to patch
    # it would write org/admin_config.json into the checkout; and a developer with a generated
    # _builtin_apps.py or PRIVACYFENCE_BUILTIN_APPS set would see unrelated tests change. Any test
    # that needs built-in apps patches app_credentials.builtin_app itself.
    if request.module.__name__.endswith(_ADMIN_CONFIG_FIXTURE_EXEMPT):
        return
    monkeypatch.setattr(
        admin_config, "admin_config_path", lambda: request.getfixturevalue("tmp_path") / "admin_config.json"
    )
    monkeypatch.setattr(app_credentials, "builtin_app", lambda key: None)
    monkeypatch.setattr(data_format, "marker_path", lambda: request.getfixturevalue("tmp_path") / "data_format.json")
    monkeypatch.setattr(relay_client, "relay_token_path", lambda: request.getfixturevalue("tmp_path") / "relay_token.txt")
    monkeypatch.setattr(
        relay_client, "relay_status_path", lambda: request.getfixturevalue("tmp_path") / "relay_status.json"
    )
    # An org server started by a test must not leave a registration thread behind that outlives
    # the test's patched paths and could reach the real relay.
    monkeypatch.setattr(relay_client, "start_registration_loop", lambda issuer_url, org_config, stop: None)


# Stashes each phase's own outcome (setup/call/teardown) onto the test item
# as ``rep_<phase>`` -- the standard pytest pattern for "did the test body
# itself fail?" from inside a fixture's teardown code. It lives in this
# repo-wide conftest.py rather than tests/integration/conftest.py so that
# both test_browser_smoke.py's ``_capture_failure_artifacts`` and
# tests/system/test_local_mode_system.py (outside tests/integration/) can use
# ``request.node.rep_call``, without a second, near-duplicate hookimpl in a
# tests/system/conftest.py.
#
# Also where the CI-diagnostics capture (docs/testing-policy.md,
# tests/diagnostics.py) hooks in: on a failing
# ``packaged``/``system``-marked test, write that test's own environment
# info/installed-file manifest/daemon-and-audit logs under test-results/ (see
# tests/diagnostics.py's own module docstring), and record where they landed
# as an extra report section -- so a CI failure's own output already answers
# "where did its diagnostic artifacts land", not just "what failed"
# (pytest's own assertion-rewriting already gives the expected-vs-actual
# half of that for every plain ``assert``).
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    setattr(item, f"rep_{rep.when}", rep)
    # An xfail also reports as skipped (with ``wasxfail``) and is not a missing prerequisite.
    if rep.skipped and not hasattr(rep, "wasxfail"):
        reason = _skip_reason(rep.longrepr)
        markers = {m.name for m in item.iter_markers()}
        name = _required_run_switch(markers, reason)
        if name:
            rep.outcome = "failed"
            rep.longrepr = f"skipped while {name}=1 requires it to run: {reason}"
    if rep.when == "call" and rep.failed and (
        item.get_closest_marker("packaged") or item.get_closest_marker("system")
    ):
        tmp_path = item.funcargs.get("tmp_path")
        if tmp_path is not None:
            suite = suite_name_for(item.location[0])
            dest = capture_failure_diagnostics(item.nodeid, Path(tmp_path), suite=suite)
            rep.sections.append((
                "PrivacyFence CI diagnostics",
                "Failure diagnostics (environment info, an installed-file manifest, and any "
                f"daemon/install/audit logs found under this test's own tmp_path) were written to: "
                f"{dest}\n"
                "In CI, .github/workflows/*.yml uploads the whole test-results/ tree as a build "
                "artifact whenever this job fails -- download it from the failed run's Summary page "
                "rather than re-running locally.",
            ))
    return rep


# A module-level ``pytest.skip(..., allow_module_level=True)`` or a failed
# ``importorskip`` skips the whole module at collection, before any item (and
# so any marker) exists -- the switches therefore apply to the collection
# report itself: each packaged CI step runs one module, so any collection skip
# there means the artifact or its env was not provided. This is stricter than the
# per-test rule (which matches the reason), and is correct only while every
# packaged step runs a single module; revisit it before a step runs several.
@pytest.hookimpl(wrapper=True)
def pytest_collectreport(report):
    # Mutated before the yield so the session's own collectreport handling
    # (which counts failed collections and aborts the run) sees the failure.
    if report.skipped:
        reason = _skip_reason(report.longrepr)
        name = None
        if os.environ.get(_REQUIRE_PACKAGED) == "1":
            name = _REQUIRE_PACKAGED
        elif os.environ.get(_REQUIRE_BROWSER) == "1" and _BROWSER_COLLECT_SKIP_REASON.search(reason):
            name = _REQUIRE_BROWSER
        if name:
            report.outcome = "failed"
            report.longrepr = f"skipped while {name}=1 requires it to run: {reason}"
    return (yield)
