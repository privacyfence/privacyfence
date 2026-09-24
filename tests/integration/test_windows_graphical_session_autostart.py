"""Real Task Scheduler and service verification for the Windows installer.

A PrivacyFence install on Windows starts two things on its own, and this
module watches both of them do it on a real machine:

* the **daemon**, as the ``PrivacyFence`` Windows service running as
  ``NT SERVICE\\PrivacyFence`` -- installed and started by the installer's own
  ``privilege-separation.ps1 enable`` (ADR 0003 decision 4); and
* the **companion**, in the signed-in user's own session, by the
  ``PrivacyFenceCompanion`` Scheduled Task that same step registers.

There is no daemon sign-in task any more (ADR 0042's cleanup removed it, and
the ``disable`` that used to hand the daemon back to it; ADR 0041), so
nothing here undoes the installer's separation to get a starting state: every
test runs against the install exactly as Setup left it.

``test_windows_packaged_smoke.py`` already proves the installer separates the
install and that the service serves MCP, but nothing there asks Task
Scheduler to run anything, and nothing there looks at what the registered
task actually says, or kills the service. This module does:

1. **The registered companion task matches its contract, and Task Scheduler
   really starts the packaged companion with it.** Not the template file in
   this repo -- the definition read back out of Task Scheduler itself
   (``schtasks /query /xml``), i.e. what the service actually parsed,
   normalized and stored, checked against
   ``tests/windows_task_contract.py`` (the same contract
   ``tests/unit/test_privilege_separation.py`` holds the shipped template to
   on every PR). Then the task is run and the process it launches is
   confirmed to be the installed ``PrivacyFenceCompanion.exe``, running as the
   logged-on user (``Win32_Process``'s ``GetOwner``, not assumed), and still
   running a few seconds later rather than having crashed on startup -- the
   one thing only a Scheduler-started, console-less launch can show (see
   ``privacyfence/std_streams.py`` for the defect of that shape that once kept
   Windows autostart from working at all).
2. **Real crash-restart of the service.** The second test kills the running
   daemon outright and asserts that a *new* pid turns up, with no further
   action from this test: ``sc failure PrivacyFence actions=
   restart/5000/...`` (``windows_privilege_separation.ps1``'s
   ``Install-DaemonService``) is the mechanism.

**The one deliberate substitution.** Task Scheduler is asked to run the task
*on demand* rather than by a user signing in. ``AllowStartOnDemand`` and the
trigger share every step that follows the decision to run -- resolving the
``Builtin\\Users`` principal to a concrete logged-on member, running the
action with that member's ``LeastPrivilege`` token, in their profile -- so
everything above is the real mechanism. What it does not cover is the
trigger's own firing, i.e. Task Scheduler deciding *when*: a hosted runner
cannot produce a Terminal Services session logon (``CreateProcessWithLogonW``
-- ``Start-Process -Credential``, ``runas.exe`` -- creates a logon session but
not the session logon a ``LogonTrigger`` subscribes to; an earlier version of
this module tried, and Task Scheduler reported ``Last Result: 267011``,
``SCHED_S_TASK_HAS_NOT_RUN``, on every run). So the trigger's own firing is
covered by ``release-testing.md``'s Windows human checks, on a real machine
with a real sign-in, tracked on
`privacyfence/privacyfence#121 <https://github.com/privacyfence/privacyfence/issues/121>`_.

A task whose principal is a *group* runs with the interactive token of a
member who is **signed in**. On a hosted runner exactly one account has a real
interactive session -- the runner's own -- so that is the account this module
has the task run for. **Only ever run this against a disposable CI account.**

The install goes to a custom machine-wide directory (``INSTALL_DIR``) rather
than ``installer/privacyfence.iss``'s own ``DefaultDirName``, for isolation
from a real ``%ProgramFiles%\\PrivacyFence`` some other install might already
occupy. It is created administrators-only before Setup is pointed at it
(``_admin_only_writable_dir``), because ``enable`` refuses an install
directory the signed-in user can rewrite -- see that helper's own docstring.

Skipped entirely unless running on real Windows, elevated (installing
machine-wide and managing a Task Scheduler task needs it), with a just-built
``dist/PrivacyFence-*-setup.exe`` on disk -- same posture as
``test_windows_packaged_smoke.py``, and run from its own
``.github/workflows/windows-graphical-session.yml`` (packaging-related
``main`` pushes, weekly, and on demand) rather than ``build.yml``'s
tag-triggered release pipeline or ``tests.yml``'s per-PR jobs: this is the
most expensive tier in ``docs/testing-policy.md``'s taxonomy, so its runtime
cost must not sit on an actual release's critical path. The module and
workflow keep their "graphical session" names, which read as the tier they
belong to -- renaming them would break the workflow's own run history and
``paths:`` triggers for no gain.
"""
from __future__ import annotations

import base64
import getpass
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'")

from tests.diagnostics import (  # noqa: E402
    capture_directory_manifest,
    copy_named_logs,
    failure_dir,
    suite_name_for,
    write_environment_info,
)
from privacyfence.privilege_separation import (  # noqa: E402
    WINDOWS_COMPANION_TASK_NAME,
    WINDOWS_SERVICE_ACCOUNT_NAME,
    WINDOWS_SERVICE_NAME,
    WINDOWS_SYSTEM_ROOT,
)
from tests.integration.test_windows_packaged_smoke import (  # noqa: E402
    ALIAS_EXE_NAME,
    COMPANION_EXE_NAME,
    MARKER_PATH,
    REMOVED_DAEMON_TASK_NAME,
    _admin_only_writable_dir,
    _assert_separated_service_serves_mcp,
    _built_installers,
    _run_installer,
    _service_config,
    _task_exists,
    _tear_down_separation,
    _wait_for_separated_service,
)
from tests.windows_task_contract import assert_task_xml_matches_companion_contract  # noqa: E402

# Machine-wide, and deliberately not the installer's own default -- see the
# module docstring. No space in the path: Inno parses `/DIR=` off its own raw
# command line rather than argv, so a value that needs quoting is one more
# thing to get wrong in a test whose subject is something else entirely.
INSTALL_DIR = Path(os.environ.get("SystemDrive", "C:") + "\\") / "PrivacyFenceAutostartTest"


def _is_admin() -> bool:
    """Cross-platform-safe by construction (like ``test_deb_packaged_
    lifecycle.py``'s own ``_can_install_packages``): this is called from a
    ``pytestmark`` skipif, which is evaluated at collection time on every
    platform, not just Windows."""
    if platform.system() != "Windows":
        return False
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


pytestmark = [
    pytest.mark.packaged,
    pytest.mark.skipif(
        platform.system() != "Windows",
        reason="only meaningful against a real installer",
    ),
    pytest.mark.skipif(
        not _built_installers(),
        reason=(
            "no dist/PrivacyFence-*-setup.exe built yet -- this is the release-workflow smoke test "
            "build.yml's build-windows job runs after scripts/build_installer.ps1; run that script "
            "locally first to exercise this test outside CI"
        ),
    ),
    pytest.mark.skipif(
        not _is_admin(),
        reason="a machine-wide install and Task Scheduler task management need an elevated shell",
    ),
    # A real silent install and a service cold start -- comfortably slower
    # than the suite's default timeout=30, same reasoning as every other
    # packaged/system test in this repo.
    pytest.mark.timeout(300),
]


# --------------------------------------------------------------------------- #
# Task Scheduler: what it stored, what it says happened
# --------------------------------------------------------------------------- #

def _task_state_summary(task: str = WINDOWS_COMPANION_TASK_NAME) -> str:
    """The registered task's own view of what happened, as ``schtasks
    /query /v`` reports it.

    A task existing and a task having actually run are two different things,
    and Task Scheduler knows which of them failed: "Last Run Time" and "Last
    Result" say whether it ever tried to run the action at all, and
    "Scheduled Task State" / "Status" say whether the task is even enabled and
    ready. Putting those lines straight into the failure message is what once
    turned eight opaque red runs into a single readable error.
    """
    result = subprocess.run(
        ["schtasks", "/query", "/tn", task, "/v", "/fo", "list"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return f"(schtasks /query failed, exit {result.returncode}): {result.stdout}{result.stderr}"
    wanted = (
        "Status:", "Last Run Time:", "Last Result:", "Next Run Time:",
        "Scheduled Task State:", "Run As User:", "Task To Run:",
    )
    lines = [
        line.strip() for line in result.stdout.splitlines()
        if line.strip().startswith(wanted)
    ]
    return "\n".join(lines) if lines else result.stdout


def _session_table() -> str:
    """Who is actually signed in, as Windows itself reports it.

    Load-bearing for reading a failure here: a group-principal task runs with
    the interactive token of a *signed-in* member, so "no session" and "no
    companion" are the same failure, and this is the only thing that tells
    them apart from a broken action."""
    result = subprocess.run(["query", "user"], capture_output=True, text=True, timeout=15)
    return (result.stdout + result.stderr).strip() or "(query user returned nothing)"


def _enable_task_scheduler_event_log() -> None:
    """Best-effort: make sure Task Scheduler's own operational channel is
    recording, since it is the only place that says *why* a task did or did
    not run. Enabled by default on current Windows; enabling it is idempotent."""
    subprocess.run(
        ["wevtutil", "sl", "Microsoft-Windows-TaskScheduler/Operational", "/e:true"],
        capture_output=True, text=True, timeout=20,
    )


def _task_scheduler_events(count: int = 40) -> str:
    """The last *count* Task Scheduler operational events, newest first."""
    result = subprocess.run(
        [
            "wevtutil", "qe", "Microsoft-Windows-TaskScheduler/Operational",
            f"/c:{count}", "/rd:true", "/f:text",
        ],
        capture_output=True, text=True, timeout=60,
    )
    text = (result.stdout + result.stderr).strip()
    return text or "(no Task Scheduler operational events -- is the channel enabled?)"


def _decode_console_output(raw: bytes) -> str:
    """``schtasks /query /xml`` writes UTF-16 (with a BOM) when its output
    is redirected, unlike the ANSI text every other ``schtasks`` output mode
    produces -- so this reads bytes and decides, rather than letting
    ``subprocess``'s own ``text=True`` guess the console code page and
    mangle it."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _registered_task_xml(task: str = WINDOWS_COMPANION_TASK_NAME) -> str:
    """The task definition as *Task Scheduler itself* stores it.

    Deliberately not the template and not the substituted copy the script
    handed to ``schtasks``: the service parses, validates, normalizes and
    stores its own version of that document, and the gap between the two is
    exactly where the daemon task this one replaced had its real bugs (an
    ``id``/``Context`` pair that registered fine but bound the principal to
    nothing that runs; a schema version whose absence silently dropped the
    1.2-only Settings elements; schema defaults the template never mentioned,
    which is how the battery settings were found)."""
    result = subprocess.run(
        ["schtasks", "/query", "/tn", task, "/xml", "ONE"], capture_output=True, timeout=20,
    )
    text = _decode_console_output(result.stdout) + _decode_console_output(result.stderr)
    assert result.returncode == 0, f"schtasks /query /xml failed (exit {result.returncode}):\n{text}"
    start = text.find("<?xml")
    if start < 0:
        start = text.find("<Task")
    assert start >= 0, f"schtasks /query /xml returned no task document:\n{text}"
    return text[start:]


def _registered_task_xml_or_error() -> str:
    """Diagnostics-path variant: never raises, so a teardown capture can't
    turn a real failure into an error inside the fixture."""
    try:
        return _registered_task_xml()
    except Exception as exc:  # noqa: BLE001 -- diagnostics only, any failure is itself the datum
        return f"(could not read the registered task XML: {exc!r})"


def _daemon_log_tail(*, max_chars: int = 4000) -> str:
    """The service's own log under the separated root -- the only place a
    daemon that died on startup says why."""
    log_path = WINDOWS_SYSTEM_ROOT / "logs" / "privacyfence.log"
    if not log_path.exists():
        return f"(no {log_path})"
    text = log_path.read_text(errors="replace")
    return f"-- {log_path} --\n{text[-max_chars:] if text else '(empty)'}"


def _install_log_tail(log_path: Path, *, max_chars: int = 8000) -> str:
    """The last *max_chars* of Inno's own ``/LOG=`` output. Never the whole
    file: this onedir bundle's own [Files] copy log alone runs to tens of
    thousands of lines, and CurStepChanged(ssPostInstall)'s own
    SeparateInstall call -- the part worth seeing -- is logged only after
    every one of them."""
    if not log_path.exists():
        return "(missing)"
    text = log_path.read_text(errors="replace")
    if not text:
        return "(empty)"
    return text[-max_chars:]


# --------------------------------------------------------------------------- #
# PowerShell helper -- Win32_Process/GetOwner has no command-line-only
# equivalent, so this module drops into PowerShell for exactly that.
# --------------------------------------------------------------------------- #

def _windows_powershell_env() -> dict[str, str]:
    """Returns an environment for spawning Windows PowerShell (``powershell.exe``,
    the 5.1 engine) that can actually autoload its own built-in modules.

    This pytest process itself runs under a PowerShell *7* (``pwsh``) step in
    CI, and pwsh sets ``$env:PSModulePath`` to its own module search path --
    one that doesn't include Windows PowerShell 5.1's module directory. A
    nested ``powershell.exe`` inherits that and never recomputes it, so
    autoloading its own built-in cmdlets (``Get-CimInstance`` from
    ``CimCmdlets``, etc.) fails with "the module could not be loaded", 100%
    reproducibly. Prepending Windows PowerShell 5.1's own system module
    directory restores the lookup those cmdlets need."""
    env = dict(os.environ)
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    system_modules = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "Modules")
    existing = env.get("PSModulePath", "")
    if system_modules.lower() not in existing.lower():
        env["PSModulePath"] = f"{system_modules};{existing}" if existing else system_modules
    return env


def _run_powershell(script: str, *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    # -EncodedCommand (UTF-16LE, base64) sidesteps every bit of cmd/argv
    # quoting hazard a multi-line script with embedded quotes would hit.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=timeout, env=_windows_powershell_env(),
    )


def _find_process_by_exe_path(exe_path: str) -> tuple[str, str] | None:
    """Returns ``(pid, owner)`` for the running process whose
    ``Win32_Process.ExecutablePath`` matches *exe_path* exactly, or ``None``
    -- confirming both that the task's action actually launched (not just
    that *some* process with a similar name exists somewhere) and, via
    ``GetOwner``, which account it's actually running as."""
    quoted = exe_path.replace("'", "''")
    script = (
        f"$p = Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -eq '{quoted}' }} "
        "| Select-Object -First 1\n"
        "if ($null -eq $p) { exit 1 }\n"
        "$owner = Invoke-CimMethod -InputObject $p -MethodName GetOwner\n"
        "Write-Output ($p.ProcessId.ToString() + '|' + $owner.User)\n"
    )
    result = _run_powershell(script, timeout=15)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    pid, _, owner = result.stdout.strip().partition("|")
    return pid, owner


def _wait_for_process(exe_path: str, *, timeout: float) -> tuple[str, str] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = _find_process_by_exe_path(exe_path)
        if found:
            return found
        time.sleep(0.5)
    return None


def _pid_alive(pid: str) -> bool:
    result = _run_powershell(
        f"if (Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}",
        timeout=15,
    )
    return result.returncode == 0


def _end_companions() -> None:
    """End every running companion, so the process this module finds after
    ``schtasks /run`` is the one Task Scheduler just started -- not the one
    ``enable`` itself started during the install (``Install-CompanionTask``
    runs the task once, best-effort)."""
    subprocess.run(
        ["schtasks", "/end", "/tn", WINDOWS_COMPANION_TASK_NAME], capture_output=True, text=True, timeout=20,
    )
    subprocess.run(["taskkill", "/f", "/im", COMPANION_EXE_NAME], capture_output=True, text=True, timeout=30)
    deadline = time.monotonic() + 30.0
    companion_exe = str(INSTALL_DIR / COMPANION_EXE_NAME)
    while _find_process_by_exe_path(companion_exe) and time.monotonic() < deadline:
        time.sleep(0.5)


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #

def _service_pid() -> str | None:
    """The ``PrivacyFence`` service's current process id, or None when it is
    not running.

    ``Win32_Service``'s own ``ProcessId`` rather than a ``tasklist`` match on
    the image name: the point of the crash-restart test is that a *different*
    process is now serving, and the only identity that answers that is the
    one the SCM itself hands out. A stopped service reports 0, which this
    reports as None.
    """
    result = _run_powershell(
        f"(Get-CimInstance -ClassName Win32_Service -Filter \"Name='{WINDOWS_SERVICE_NAME}'\").ProcessId",
        timeout=60,
    )
    pid = result.stdout.strip()
    return pid if pid and pid != "0" else None


def _wait_for_service_pid(*, different_from: str, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = _service_pid()
        if pid is not None and pid != different_from:
            return pid
        time.sleep(1.0)
    return None


def _service_failure_config() -> str:
    """``sc qfailure`` -- what the SCM will actually do about a crashed
    daemon, quoted only when it did not do it."""
    result = subprocess.run(
        ["sc.exe", "qfailure", WINDOWS_SERVICE_NAME], capture_output=True, text=True, timeout=30,
    )
    return f"{result.stdout}{result.stderr}"


def _separation_state_summary() -> str:
    """How the installer left the install: the marker, the service, both task
    names (the daemon one should not exist), the separated root and the
    PrivacyFence processes."""
    lines = [f"marker ({MARKER_PATH}): {'present' if MARKER_PATH.exists() else 'absent'}"]
    service = subprocess.run(
        ["sc.exe", "query", WINDOWS_SERVICE_NAME], capture_output=True, text=True, timeout=30,
    )
    lines.append(f"---- sc query {WINDOWS_SERVICE_NAME} ----\n{service.stdout}{service.stderr}")
    for task in (WINDOWS_COMPANION_TASK_NAME, REMOVED_DAEMON_TASK_NAME):
        query = subprocess.run(
            ["schtasks", "/query", "/tn", task, "/fo", "list"],
            capture_output=True, text=True, timeout=30,
        )
        lines.append(f"---- schtasks /query /tn {task} ----\n{query.stdout}{query.stderr}")
    listing = subprocess.run(
        ["cmd", "/c", "dir", "/s", "/b", str(WINDOWS_SYSTEM_ROOT)],
        capture_output=True, text=True, timeout=60,
    )
    lines.append(f"---- {WINDOWS_SYSTEM_ROOT} ----\n{listing.stdout}{listing.stderr}")
    processes = subprocess.run(
        ["tasklist", "/fi", "IMAGENAME eq PrivacyFence*"], capture_output=True, text=True, timeout=30,
    )
    lines.append(f"---- PrivacyFence processes ----\n{processes.stdout}{processes.stderr}")
    return "\n".join(lines)


def _kill_app_processes() -> None:
    """Best-effort: end any daemon *or companion* this module's install left
    running, so a failed test never leaks a process holding the install
    directory open -- which surfaces as the *next* test's silent install dying
    at RestartManager's "Some applications could not be shut down" (Inno exit
    5) rather than in the test that caused it."""
    for image in (ALIAS_EXE_NAME, COMPANION_EXE_NAME):
        subprocess.run(
            ["taskkill", "/f", "/im", image], capture_output=True, text=True, timeout=30,
        )


# --------------------------------------------------------------------------- #
# Fixture -- one real install per test, and its full teardown
# --------------------------------------------------------------------------- #

class _Installed:
    def __init__(self, *, log_path: Path) -> None:
        self.log_path = log_path
        self.companion_exe = str(INSTALL_DIR / COMPANION_EXE_NAME)


@pytest.fixture
def _installed(request, tmp_path):
    """One real silent install per test, exactly as Setup leaves it, and its
    full teardown -- neither test may leave a task, a service or a process
    behind for the other (or for whatever runs next on this machine).

    Also doubles as this module's diagnostics capture: the generic
    per-``tmp_path`` capture in ../conftest.py cannot see what Task Scheduler
    knows (the task's own stored definition and last-run result) or the
    service's log under ``%ProgramData%``."""
    if INSTALL_DIR.exists():  # a previous run that died before its own cleanup
        _kill_app_processes()
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)
    # Likewise for the machine-wide state an installer-run `enable` leaves.
    _tear_down_separation()
    _admin_only_writable_dir(INSTALL_DIR)

    _enable_task_scheduler_event_log()
    log_path = tmp_path / "install.log"
    setup_exe = _built_installers()[-1]
    install_result = _run_installer(
        str(setup_exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
        f"/DIR={INSTALL_DIR}", f"/LOG={log_path}",
    )
    assert install_result.returncode == 0, (
        f"installer failed (exit {install_result.returncode}):\n{install_result.stdout}{install_result.stderr}\n"
        f"---- install log (tail) ----\n{_install_log_tail(log_path)}"
    )
    # Setup reports 0 even when its separation step refused (see
    # privacyfence.iss's CurStepChanged), so the marker is what says it took.
    assert MARKER_PATH.is_file(), (
        f"{MARKER_PATH} missing after install -- Setup's own `enable` did not separate this install\n"
        f"---- install log (tail) ----\n{_install_log_tail(log_path)}"
    )

    try:
        yield _Installed(log_path=log_path)
    finally:
        rep_call = getattr(request.node, "rep_call", None)
        if rep_call is not None and rep_call.failed:
            dest = failure_dir(request.node.nodeid, suite=suite_name_for(__file__))
            write_environment_info(dest / "environment.txt")
            capture_directory_manifest(WINDOWS_SYSTEM_ROOT, dest / "manifest-system-root.txt")
            (dest / "logs").mkdir(parents=True, exist_ok=True)
            (dest / "logs" / "schtasks-query.txt").write_text(_task_state_summary(), encoding="utf-8")
            (dest / "logs" / "schtasks-query-xml.txt").write_text(
                _registered_task_xml_or_error(), encoding="utf-8",
            )
            (dest / "logs" / "query-user.txt").write_text(_session_table(), encoding="utf-8")
            (dest / "logs" / "taskscheduler-events.txt").write_text(
                _task_scheduler_events(120), encoding="utf-8",
            )
            (dest / "logs" / "separation-state.txt").write_text(_separation_state_summary(), encoding="utf-8")
            (dest / "logs" / "install-log-tail.txt").write_text(_install_log_tail(log_path), encoding="utf-8")
            copy_named_logs(WINDOWS_SYSTEM_ROOT, dest / "logs")
        # Down before the uninstaller: a running service and companion hold
        # INSTALL_DIR open. The floor rather than the script, deliberately:
        # this runs on the failure path too, and a teardown that can raise
        # replaces the failure the test was actually reporting.
        _tear_down_separation()
        _kill_app_processes()
        uninstaller = INSTALL_DIR / "unins000.exe"
        if uninstaller.is_file():
            _run_installer(str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
        # A silent uninstall keeps %ProgramData%\PrivacyFence (ADR 0042);
        # this is a disposable runner, so the floor removes it too.
        _tear_down_separation()
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)


# --------------------------------------------------------------------------- #
# The tests
# --------------------------------------------------------------------------- #

async def test_installed_companion_task_starts_the_companion_in_the_signed_in_session(_installed):
    """Task Scheduler starts the packaged companion, as the signed-in user,
    from the task the installer's own ``enable`` registered -- and the
    service it talks to is serving."""
    # ── What Task Scheduler stored, against the shared contract ───────────
    assert_task_xml_matches_companion_contract(_registered_task_xml(), exec_path=_installed.companion_exe)
    assert not _task_exists(REMOVED_DAEMON_TASK_NAME), (
        f"a {REMOVED_DAEMON_TASK_NAME!r} daemon sign-in task is registered -- the installer no "
        f"longer has one\n{_separation_state_summary()}"
    )

    # ── The daemon is the service, and it is serving ─────────────────────
    base_url, mcp_token = _wait_for_separated_service()
    await _assert_separated_service_serves_mcp(base_url, mcp_token)

    # ── A real on-demand run of the task. The module docstring's one
    # deliberate substitution for a user signing in: the trigger decides
    # *when*, and everything after that decision is the same code path. ───
    _end_companions()
    run = subprocess.run(
        ["schtasks", "/run", "/tn", WINDOWS_COMPANION_TASK_NAME], capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, (
        f"schtasks /run failed (exit {run.returncode}):\n{run.stdout}{run.stderr}\n"
        f"---- who is signed in (query user) ----\n{_session_table()}\n"
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}"
    )
    found = _wait_for_process(_installed.companion_exe, timeout=30.0)
    assert found, (
        f"{_installed.companion_exe} never appeared as a running process within 30s of Task "
        f"Scheduler being asked to run {WINDOWS_COMPANION_TASK_NAME!r}\n"
        f"---- who is signed in (query user) ----\n{_session_table()}\n"
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}"
    )
    pid, owner = found
    # A group principal runs the action as a signed-in member, in that
    # member's own profile -- which is the account running this test, since
    # it is the only one with a real session here. Never the service account:
    # the companion is on the agent's side of the trust boundary.
    assert getpass.getuser().lower() in owner.lower(), (
        f"{COMPANION_EXE_NAME} (pid {pid}) is running as {owner!r}, not the signed-in account "
        f"({getpass.getuser()!r}) the Builtin\\Users principal should have resolved to"
    )

    # ── ...and it stays up: a console-less, Scheduler-started process is the
    # one launch shape nothing else in this repo exercises, and a crash on
    # startup there is invisible to everything but this. ────────────────
    time.sleep(10.0)
    assert _pid_alive(pid), (
        f"{COMPANION_EXE_NAME} (pid {pid}) exited within 10s of Task Scheduler starting it\n"
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}\n"
        f"---- Task Scheduler events ----\n{_task_scheduler_events(20)}"
    )


@pytest.mark.timeout(600)
async def test_crash_restart_relaunches_the_separated_daemon(_installed):
    """Kill the service's process outright, and a *new* pid turns up with no
    further action from this test -- ``sc failure PrivacyFence actions=
    restart/5000/...`` (``Install-DaemonService``) is the mechanism.

    The daemon sign-in task this replaced tried to do the same with
    ``<RestartOnFailure>`` and, when that was measured not to engage for a
    crashed action at all (Task Scheduler logs a killed action as a
    *successfully completed* task), a repeating ``<TimeTrigger>``. A service
    manager is the thing both were standing in for.
    """
    _wait_for_separated_service()

    first_pid = _service_pid()
    assert first_pid, (
        f"the {WINDOWS_SERVICE_NAME} service is not reporting a process id\n"
        f"{_separation_state_summary()}"
    )

    # A real crash, not a graceful stop: /f is a TerminateProcess, so the
    # service's own control handler never runs and the SCM sees a failure
    # rather than an orderly stop. That distinction is the whole point -- a
    # service that is *asked* to stop does not exercise failure actions.
    kill = subprocess.run(
        ["taskkill", "/pid", first_pid, "/f"], capture_output=True, text=True, timeout=30,
    )
    assert kill.returncode == 0, f"taskkill on pid {first_pid} failed:\n{kill.stdout}{kill.stderr}"

    relaunched = _wait_for_service_pid(different_from=first_pid, timeout=180.0)
    assert relaunched is not None, (
        f"the {WINDOWS_SERVICE_NAME} service never came back after taskkill /pid {first_pid} /f, "
        f"within 180s of a restart/5000 failure action\n"
        f"---- sc qc ----\n{_service_config()}\n"
        f"---- sc failure ----\n{_service_failure_config()}\n"
        f"{_separation_state_summary()}\n"
        f"---- daemon log (tail) ----\n{_daemon_log_tail()}"
    )
    # Still the service account, not something that happened to take the
    # name: a restart that came back as anyone else would be the #428
    # weakness reopening quietly.
    config = _service_config()
    assert config is not None and WINDOWS_SERVICE_ACCOUNT_NAME.lower() in config.lower(), (
        f"the relaunched {WINDOWS_SERVICE_NAME} service does not run as "
        f"{WINDOWS_SERVICE_ACCOUNT_NAME}:\n{config}"
    )
    # ...and serving again.
    _wait_for_separated_service()
