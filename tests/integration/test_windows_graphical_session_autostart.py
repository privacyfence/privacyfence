"""Real Task Scheduler autostart verification for the Windows installer.
See `docs/platform-support.md`'s "Known open items" for the full history of
this verification -- the real Task Scheduler XML definition, the
crash-restart-on-failure behavior, and what still can't be observed from a
hosted CI runner.

``test_windows_packaged_smoke.py`` (Phase 6.2) already proves the installer
registers *a* Task Scheduler autostart task (``schtasks /query`` against it)
and that the alias exe it points at, once started, serves a real
daemon/MCP/approval/audit round trip -- but it starts that alias exe itself,
directly, as a subprocess, out of that test's own ``tmp_path``. Nothing
there ever asks Task Scheduler to run anything, and nothing there looks at
what the task actually says.

This module does both:

1. **The registered definition matches the autostart contract.** Not the
   template file in this repo -- the definition read back out of Task
   Scheduler itself (``schtasks /query /xml``), i.e. what the service
   actually parsed, normalized and stored: a ``LogonTrigger`` with no
   ``UserId`` (so it is scoped to any interactive logon, not the installing
   account), a ``Builtin\\Users`` principal bound to the ``Actions`` element
   by a matching ``id``/``Context`` pair, ``LeastPrivilege``, ``Parallel``
   multiple-instances, the real installed ``privacyfence-app.exe`` path as
   the action's ``Command``, the ``RestartOnFailure`` interval/count, and the
   two battery settings that would otherwise default to "don't start on
   battery power". Each of those has been a real,
   shipped bug at least once -- see ``installer/privacyfence-task.xml.tmpl``'s
   own header comment -- and every one of them was invisible to a test that
   only asked "does a task with this name exist". The same contract is
   asserted against the shipped template on every PR, on any OS, by
   ``tests/unit/test_windows_autostart_task_template.py``; both call
   ``tests/windows_task_contract.py``.
2. **Task Scheduler really starts the packaged daemon.** The service is
   asked to run the installed task; the process it launches is confirmed to
   be the installed exe, running as the logged-on user (``Win32_Process``'s
   ``GetOwner``, not assumed). This is what found the defect that had kept
   Windows autostart from ever working: started with no console, the
   windowed build had no ``sys.stdout`` for uvicorn's log formatter to
   probe, and the daemon exited 1 before binding its port (see
   ``privacyfence/std_streams.py``). Every other automated start of this app
   in this repo hands it a redirected stdout, so nothing else could have.
3. **...and what that daemon does next is separate this install, hand over,
   and stop** (ADR 0003 decision 6). A packaged daemon that finds itself
   unseparated does not serve: it elevates ``enable`` through UAC, and on
   these runners that succeeds. Seconds after Task Scheduler starts it, the
   data directory has moved to ``%ProgramData%``, a ``PrivacyFence`` service
   is running the daemon under ``NT SERVICE\\PrivacyFence``, the companion
   task is registered, the task that started all this is ``Disabled``, and
   the Scheduler-started process itself has exited 0
   (``privilege_separation.SeparationHandover``). So that is what the first
   test asserts, end to end, against the real service.

   It used to assert the other thing -- a control pipe under
   ``%LOCALAPPDATA%``, and a full daemon/MCP/approval/audit round trip
   against a daemon living in the signed-in user's own session -- and that
   is why this module was red on every ``main`` push from #555 until this
   was written: the arrangement it was asserting is one decision 6 retired.
   ``test_windows_packaged_smoke.py`` gave up its own unseparated scenario
   for the same reason and says so at its own call site;
   ``test_linux_graphical_session_autostart.py`` took the same re-scoping in
   the other direction (no polkit agent on that runner, so the *refusal* is
   what is observable there rather than the repair -- see #560).

   The separated install's full contract -- ACLs, service account, group
   membership, the handoff directory, the marker's own fields -- stays in
   ``test_windows_packaged_smoke.py``'s
   ``test_windows_install_separates_with_no_manual_enable`` and is
   deliberately not repeated here. What is here is the part only this module
   can answer: that the thing which separated the install was *Task
   Scheduler starting the daemon at sign-in*, not Setup.
4. **Real crash-restart** (Phase 13 item 4). The second test kills the
   running daemon outright and asserts that a *new* pid turns up, with no
   further action from this test. This is the positive assertion a first
   measurement pass could not make: killing the action showed
   ``<RestartOnFailure>`` does nothing for a crashed daemon at all (Task
   Scheduler logs the dead action as a *successfully completed* task, so the
   setting never engages) -- see ``platform-support.md``'s "Known open
   items" for that measurement, and
   ``installer/privacyfence-task.xml.tmpl``'s own header comment for the
   repeating ``<TimeTrigger>`` that replaced it. It is the *service* that
   answers here, though, not either of those: crash restart moved with the
   daemon, and ``sc failure PrivacyFence actions= restart/5000/...``
   (``windows_privilege_separation.ps1``'s ``Install-DaemonService``) is the
   mechanism on a separated install. The ``<TimeTrigger>`` cannot be what
   answers: its task is ``Disabled`` by then, which the first test asserts
   directly. This test is the successor of, and replaces, the negative
   assertion this module used to carry
   (``test_restart_on_failure_does_not_cover_a_crashed_daemon``, preserved
   in git history).

   the positive assertion provable.

**The one deliberate substitution, and the history behind it.** Task
Scheduler is asked to run the task *on demand* rather than by a user signing
in. ``AllowStartOnDemand`` and the trigger share every step that follows the
decision to run -- resolving the ``Builtin\\Users`` principal to a concrete
logged-on member, running the action with that member's ``LeastPrivilege``
token, in their profile -- so everything above is the real mechanism. What
it does not cover is the trigger's own firing, i.e. Task Scheduler deciding
*when*.

That gap is deliberate, and it replaces a substitution that could not work.
This module used to create a throwaway local account, call PowerShell's
``Start-Process -Credential`` for it, call that "signing in", and assert the
daemon turned up. It never did, on any run: ``CreateProcessWithLogonW``
(what that cmdlet, and ``runas.exe``, are built on) creates a *logon
session* but not the Terminal Services *session* logon that Task Scheduler's
``LogonTrigger`` subscribes to, so the trigger was never evaluated at all.
Task Scheduler said so itself once the failure message started asking it --
run 24 of ``windows-graphical-session.yml``, against ``main``, with the task
correctly registered::

    Status:                Ready
    Scheduled Task State:  Enabled
    Run As User:           Users
    Last Run Time:         11/30/1999 12:00:00 AM
    Last Result:           267011        (SCHED_S_TASK_HAS_NOT_RUN)

Registered, enabled, ready, never attempted. Nothing was wrong on the
installer side; the test was asserting something a hosted runner cannot
produce. So the trigger's own firing is now covered where it can actually be
covered: ``release-testing.md``'s Windows human checks, on a real machine
with a real sign-in, tracked on
`privacyfence/privacyfence#121 <https://github.com/privacyfence/privacyfence/issues/121>`_.
(An RDP loopback into the runner would create a genuine session logon, and
was considered -- it needs an RDP client that can run without a desktop of
its own, which a hosted runner does not have, so it would trade a gap that
is honestly described for one that is merely harder to see.)

The throwaway account is gone with it, for a reason worth stating because it
is a property of the shipped task rather than of the test: a task whose
principal is a *group* runs with the interactive token of a member who is
**signed in**, and the daemon lands in that member's own profile. On a
hosted runner exactly one account has a real interactive session -- the
runner's own -- so that is the account this module can have the task run
for, and a throwaway account (which, as above, cannot be given a session at
all) buys nothing. Run 25 showed the same thing from the other side: asking
for the run as that throwaway account returned ``ERROR: Access is denied.``

So, like ``test_linux_graphical_session_autostart.py`` and unlike every
other packaged test in this repo, this module does **not** isolate the
daemon's home directory under ``tmp_path``: Task Scheduler starts the daemon
with no injected environment, in the real profile of the real logged-on
account, which is the whole point. It refuses to run at all (skips) if that
account already has PrivacyFence state, and removes whatever it creates
afterwards -- see ``_real_home_state``. **Only ever run this against a
disposable CI account.**

The install also goes to a custom machine-wide directory rather than
``installer/privacyfence.iss``'s own ``DefaultDirName``, though both are
machine-wide now: ``PrivilegesRequired`` is ``admin`` (previously
``lowest``, changed once a real non-admin install turned out to fail
``RegisterAutostartTask()`` outright -- see ``docs/platform-support.md``'s
"Known open items" for the full story, including why this very module,
gated on ``_is_admin()`` below, never once exercised that failure), so
``{autopf}`` always resolves to ``{pf}`` (``%ProgramFiles%``) regardless.
``INSTALL_DIR`` exists for this module's own isolation from a real
``%ProgramFiles%\\PrivacyFence`` some other install might already occupy,
not to force a machine-wide layout the installer wouldn't otherwise pick.
It is created administrators-only before Setup is pointed at it
(``_admin_only_writable_dir``), because ADR 0003 decision 4 has the install
separate itself and ``enable`` refuses an install directory the signed-in
user can rewrite -- see that helper's own docstring.

**And the installer's own separation is undone first, every time.** ADR
0003 decision 4 has Setup separate the install itself, which leaves the
daemon's autostart task ``Disabled`` -- nothing for Task Scheduler to start,
and so nothing for this module to watch it start. So
``_disable_installer_enabled_privilege_separation()`` runs right after the
install in the fixture below, the same way
``test_deb_packaged_lifecycle.py`` has undone the ``.deb``'s own
``enable --auto`` since #428 D1. That is a *starting* state, not the
subject: what each test then watches is the daemon putting the separation
back, which is decision 6's whole point and the thing only an autostart run
can exercise.

The installer's own separation -- the thing being undone -- is asserted in
``test_windows_packaged_smoke.py``'s own
``test_windows_install_separates_with_no_manual_enable``, against the
installer's own default directory rather than this module's
``/DIR=``-overridden one. Whether the two are supposed to behave the same is
exactly what privacyfence/privacyfence#561 is still open on: this module's own
install has, at least once, come out of that same installer-run ``enable``
with no marker to show for it despite Setup reporting success, so the
disable call below tolerates either starting state
(``require_separated=False``) rather than assuming this install got
separated.

Skipped entirely unless running on real Windows, elevated (installing
machine-wide and managing a Task Scheduler task needs it), with a just-built
``dist/PrivacyFence-*-setup.exe`` on disk -- same posture as
``test_windows_packaged_smoke.py``, and, like the Linux module, run from its
own ``.github/workflows/windows-graphical-session.yml`` (packaging-related
``main`` pushes, weekly, and on demand) rather than ``build.yml``'s
tag-triggered release pipeline or ``tests.yml``'s per-PR jobs: this is the
same most-expensive tier in ``docs/automated-test-strategy-plan.md``'s
taxonomy (Phase 7's own objective) the Linux module already lives in, so
its runtime cost must not sit on an actual release's critical path. The
module and workflow keep their "graphical session" names, which now read
as the tier they belong to rather than a literal description of what this
module drives -- renaming them would break the workflow's own run history and
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
from urllib.parse import urlsplit

import pytest

pytest.importorskip("mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'")

from tests.control_channel_client import resolve_windows_pipe_name, windows_pipe_exists  # noqa: E402
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
    MCP_TOKEN_FILE_NAME,
    SEPARATED_HANDOFF_DIR,
    SEPARATED_SETTINGS_PATH,
    SEPARATED_WEB_BASE_URL_PATH,
    TASK_NAME,
    _admin_only_writable_dir,
    _assert_separated_service_serves_mcp,
    _built_installers,
    _data_dir,
    _disable_installer_enabled_privilege_separation,
    _free_port,
    _prepare_home,
    _run_installer,
    _service_config,
    _task_exists,
    _task_state,
    _tear_down_separation,
    _wait_for_separated_service,
    _wait_until_connectable,
)
from tests.windows_task_contract import assert_task_xml_matches_autostart_contract  # noqa: E402

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
    # A real silent install, a real Scheduler-driven daemon cold start, and a
    # full MCP/approval/audit round trip -- comfortably slower than the
    # suite's default timeout=30, same reasoning as every other
    # packaged/system test in this repo.
    pytest.mark.timeout(300),
]


# --------------------------------------------------------------------------- #
# Task Scheduler: what it stored, what it says happened, what it will do now
# --------------------------------------------------------------------------- #

def _task_state_summary() -> str:
    """The registered task's own view of what happened, as ``schtasks
    /query /v`` reports it.

    A task existing and a task having actually run are two different
    things, and the assertions below can only observe the second one
    indirectly (no daemon process turned up). Task Scheduler knows which of
    them failed: "Last Run Time" and "Last Result" say whether it ever tried
    to run the action at all, and "Scheduled Task State" / "Status" say
    whether the task is even enabled and ready. Putting those lines straight
    into the failure message is the same move that turned the registration
    failure underneath this one from eight opaque runs into a single
    readable error -- the schtasks output was always there, it just was not
    anywhere a failing run could show it.
    """
    result = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME, "/v", "/fo", "list"],
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

    Load-bearing for reading a failure here rather than merely decorative: a
    group-principal task runs with the interactive token of a *signed-in*
    member, so "no session" and "no daemon" are the same failure, and this is
    the only thing that tells them apart from a broken action."""
    result = subprocess.run(["query", "user"], capture_output=True, text=True, timeout=15)
    return (result.stdout + result.stderr).strip() or "(query user returned nothing)"


def _enable_task_scheduler_event_log() -> None:
    """Best-effort: make sure Task Scheduler's own operational channel is
    recording, since it is the only place that says *why* a task did or did
    not run again. Enabled by default on current Windows; enabling it is
    idempotent and costs nothing when it already is."""
    subprocess.run(
        ["wevtutil", "sl", "Microsoft-Windows-TaskScheduler/Operational", "/e:true"],
        capture_output=True, text=True, timeout=20,
    )


def _task_scheduler_events(count: int = 40) -> str:
    """The last *count* Task Scheduler operational events, newest first.

    ``schtasks /query /v`` reports only the outcome of the last run --
    "Last Result: 1" and nothing about what the service decided afterwards.
    Whether a restart was attempted, skipped, or never considered is in this
    log and nowhere else."""
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


def _registered_task_xml() -> str:
    """The task definition as *Task Scheduler itself* stores it.

    Deliberately not installer/privacyfence-task.xml.tmpl and not the
    substituted copy Setup handed to ``schtasks``: the service parses,
    validates, normalizes and stores its own version of that document, and
    the gap between the two is exactly where this task's real bugs have
    lived (an ``id``/``Context`` pair that registered fine but bound the
    principal to nothing that runs; a schema version whose absence silently
    dropped the 1.2-only Settings elements; schema defaults the template
    never mentioned, which is how the battery settings were found)."""
    result = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME, "/xml", "ONE"], capture_output=True, timeout=20,
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


def _daemon_log_tail(home: Path, *, max_chars: int = 4000) -> str:
    """The daemon's own log, which is the only thing it leaves behind when
    Task Scheduler starts it.

    ``daemon_main.setup_logging`` writes to ``<data dir>/logs/
    privacyfence.log`` -- for a bundled app, ``%LOCALAPPDATA%\\PrivacyFence\\
    logs\\privacyfence.log`` -- and ``main()`` logs an explicit
    ``Fatal error: ...`` with a traceback there before returning 1. A
    Scheduler-launched process has no console and no redirected stdout, so
    unlike ``test_windows_packaged_smoke.py`` (which captures the daemon's
    stdout to a file it can quote on failure) this file is the *only* place
    a daemon that died on startup says why. ``Last Result: 1`` from
    ``schtasks /query`` says only that it did.

    Both roots, newest last: decision 6's automatic ``enable`` *moves* that
    file from ``%LOCALAPPDATA%`` to ``%ProgramData%`` mid-run, so which of
    the two has the interesting lines depends on how far the thing being
    diagnosed got -- and a failure that has to be told apart from a
    successful separation needs whichever one exists."""
    parts = []
    for log_path in (_data_dir(home) / "logs" / "privacyfence.log",
                     WINDOWS_SYSTEM_ROOT / "logs" / "privacyfence.log"):
        if not log_path.exists():
            continue
        text = log_path.read_text(errors="replace")
        parts.append(f"-- {log_path} --\n{text[-max_chars:] if text else '(empty)'}")
    if not parts:
        return "(no privacyfence.log under either the per-user or the separated root)"
    return "\n".join(parts)


def _install_log_tail(log_path: Path, *, max_chars: int = 8000) -> str:
    """The last *max_chars* of Inno's own ``/LOG=`` output, or a plain
    ``(missing)``/``(empty)`` marker. Never the whole file: this onedir
    bundle's own [Files] copy log alone runs to tens of thousands of
    lines, and CurStepChanged(ssPostInstall)'s own RegisterAutostartTask
    call -- the part actually worth seeing on a registration failure --
    is logged only after every one of those file-copy lines, so returning
    the whole file risks it never actually reaching whatever captured
    this assertion's own output (a CI log viewer's own size limit,
    included)."""
    if not log_path.exists():
        return "(missing)"
    text = log_path.read_text(errors="replace")
    if not text:
        return "(empty)"
    return text[-max_chars:]


# --------------------------------------------------------------------------- #
# PowerShell helper -- Win32_Process/GetOwner has no schtasks.exe-style
# command-line-only equivalent, so this module drops into PowerShell for
# exactly that (everything else here uses the same plain
# cmd-tool-via-subprocess style as test_windows_packaged_smoke.py/
# test_deb_packaged_lifecycle.py).
# --------------------------------------------------------------------------- #

def _windows_powershell_env() -> dict[str, str]:
    """Returns an environment for spawning Windows PowerShell (``powershell.exe``,
    the 5.1 engine) that can actually autoload its own built-in modules.

    This pytest process itself runs under a PowerShell *7* (``pwsh``) step in
    CI (see this workflow's own ``shell:`` line), and pwsh sets ``$env:
    PSModulePath`` to its own module search path -- one that doesn't include
    Windows PowerShell 5.1's module directory. A nested ``powershell.exe``
    process (spawned below) inherits that env var as plain process
    environment and, unlike a real top-level 5.1 session, never recomputes
    it -- so autoloading its own built-in cmdlets (``Get-CimInstance`` from
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
    # quoting hazard a multi-line script with embedded single/double quotes
    # would otherwise hit going through subprocess's argv on Windows.
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


def _wait_for_alias_process(
    exe_path: str, *, timeout: float, different_from: str | None = None,
) -> tuple[str, str] | None:
    """Polls for the installed alias exe, optionally requiring a *different*
    pid than one already seen (which is what makes "Task Scheduler
    relaunched it" distinguishable from "the old process is still here")."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = _find_process_by_exe_path(exe_path)
        if found and (different_from is None or found[0] != different_from):
            return found
        time.sleep(0.5)
    return None


def _wait_until_pid_gone(pid: str, *, timeout: float) -> bool:
    """Whether *that* process has left the process table.

    By pid, deliberately, and not by image path like
    ``_find_process_by_exe_path()`` above: on a separated install the
    service runs the very same ``privacyfence-app.exe`` (its ``binPath`` is
    that image plus ``--windows-service``), so "no process with this
    executable path" is false for as long as the install is working
    correctly. Asking whether the process Task Scheduler started is gone is
    a different question from asking whether any daemon is running, and this
    module needs the first one.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _run_powershell(
            f"if (Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}",
            timeout=15,
        )
        if result.returncode != 0:
            return True
        time.sleep(0.5)
    return False


def _wait_for_marker(*, timeout: float) -> bool:
    """Whether ``privilege-separation.json`` turns up under ``%ProgramData%``
    within ``timeout``.

    This is the module's central wait now, and it is deliberately the
    *marker* rather than the service, the companion task or the moved data
    directory: ``Write-Marker`` is the second-to-last thing ``enable`` does
    and the only one of those four that no half-finished run leaves behind
    (see ``windows_privilege_separation.ps1``'s ``Undo-PartialEnable``,
    which removes it first precisely so a rolled-back enable cannot be
    mistaken for a finished one). So its presence means "this install got
    separated", not "something started separating it".

    Generous by default at the call sites: decision 6's automatic ``enable``
    elevates through UAC, creates a virtual service account, rewrites the
    ACLs of a whole directory tree and moves the data into it, on a runner
    with no warm caches.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if MARKER_PATH.exists():
            return True
        time.sleep(0.5)
    return MARKER_PATH.exists()


# The files a *previous* boot leaves behind for the companion and the shim to
# find it by. `enable` moves them into the separated ``handoff\`` directory
# along with everything else, so anything stale in the per-user profile
# reappears on the other side looking exactly like this boot's own.
_DISCOVERY_FILE_NAMES = (MCP_TOKEN_FILE_NAME, "mcp_url", "web_base_url")


def _clear_stale_discovery_files(home: Path) -> None:
    """Remove the per-user profile's daemon-discovery files before any test
    starts a daemon.

    Every one of them is written when a daemon's web server *binds* and
    removed when it stops gracefully -- so their presence is supposed to
    mean "a daemon is serving right now, here". That only holds if nothing
    older is lying around, and in this fixture something older is: the
    install's own decision-4 ``enable`` starts a service before this module
    has seeded anything, so that service binds the default port and writes
    these three files naming it. ``disable`` then moves them back into the
    per-user profile, and the automatic ``enable`` under test moves them
    into the separated ``handoff\`` directory again -- where a test polling
    for "the service is running and has reported a base URL" reads the old
    port and waits for a socket nobody is listening on.

    A service also reports ``RUNNING`` to the SCM the moment its control
    handler is installed, which is well before ``run_app()`` gets anywhere
    near binding a port, so there is a real window in which that read
    happens. Clearing them here closes it at the source: nothing this
    module asserts about may predate the boot it is asserting about.
    """
    for name in _DISCOVERY_FILE_NAMES:
        (_data_dir(home) / name).unlink(missing_ok=True)


def _wait_for_separated_service_serving(installed: "_Installed") -> tuple[str, str]:
    """The separated service, serving *this* install's configuration.

    The port comes first and comes from this module, not from the handoff
    directory: this fixture seeded ``settings.yaml`` with a free port before
    the daemon ever ran, so a separated service that came up on that port is
    a service that read the configuration the automatic ``enable`` moved for
    it. Reading ``web_base_url`` first would instead believe whatever the
    last boot wrote there -- see ``_clear_stale_discovery_files()``, which
    removes that possibility, and this, which would still catch it.
    """
    _wait_until_connectable("localhost", installed.port, timeout=120.0)
    base_url, mcp_token = _wait_for_separated_service()
    assert urlsplit(base_url).port == installed.port, (
        f"the {WINDOWS_SERVICE_NAME} service is advertising {base_url!r}, but this install's "
        f"settings.yaml names port {installed.port} -- so that file is from an earlier boot and "
        f"the token beside it is too\n"
        f"---- {SEPARATED_HANDOFF_DIR} ----\n{_handoff_listing()}\n"
        f"---- daemon log (tail) ----\n{_daemon_log_tail(installed.home)}"
    )
    return base_url, mcp_token


def _handoff_listing() -> str:
    if not SEPARATED_HANDOFF_DIR.exists():
        return f"({SEPARATED_HANDOFF_DIR} missing)"
    names = sorted(entry.name for entry in SEPARATED_HANDOFF_DIR.iterdir())
    web_base_url = (
        SEPARATED_WEB_BASE_URL_PATH.read_text(encoding="utf-8").strip()
        if SEPARATED_WEB_BASE_URL_PATH.exists()
        else "(absent)"
    )
    return f"{', '.join(names) or '(empty)'}\nweb_base_url: {web_base_url}"


def _service_pid() -> str | None:
    """The ``PrivacyFence`` service's current process id, or None when it is
    not running.

    ``Win32_Service``'s own ``ProcessId`` rather than a ``tasklist`` match on
    the image name: the point of the crash-restart test is that a *different*
    process is now serving, and the only identity that answers that is the
    one the SCM itself hands out. A stopped service reports 0, which this
    reports as None.
    """
    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            f"(Get-CimInstance -ClassName Win32_Service -Filter \"Name='{WINDOWS_SERVICE_NAME}'\")"
            ".ProcessId",
        ],
        capture_output=True, text=True, timeout=60,
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


def _wait_for_task_last_result(expected: str, *, timeout: float) -> str | None:
    """Poll "Last Result" until it reads *expected*, and return whatever it
    reads in the end.

    Polled rather than read once: Task Scheduler records the action's exit
    code when it notices the process has ended, which is not the same
    instant the process actually ends, and a single read taken right after
    the handover can still be reporting the run before it.
    """
    deadline = time.monotonic() + timeout
    last = _task_last_result()
    while time.monotonic() < deadline:
        if last == expected:
            return last
        time.sleep(1.0)
        last = _task_last_result()
    return last


def _task_last_result() -> str | None:
    """``schtasks /query /v``'s "Last Result" for the daemon task -- the exit
    code Task Scheduler recorded for the action it ran."""
    result = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME, "/v", "/fo", "list"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Last Result:"):
            return stripped.split(":", 1)[1].strip()
    return None


def _kill_alias_processes() -> None:
    """Best-effort: end any daemon *or companion* this module's install left
    running, so a failed test never leaks a process holding the install
    directory open.

    The companion matters as much as the daemon and is easier to forget: the
    installer's own ADR 0003 decision 4 ``enable`` starts one
    (``Install-CompanionTask`` runs ``schtasks /run`` on the task it just
    registered), and the ``disable`` this module runs right afterwards
    removes that task without ending the process it already spawned. Left
    alive, it holds ``INSTALL_DIR`` open and the *next* test's silent install
    dies at RestartManager's "Some applications could not be shut down"
    (Inno exit 5, install rolled back) -- which is how a leak here surfaces
    as a setup error in the test that follows rather than in the one that
    caused it."""
    for image in (ALIAS_EXE_NAME, COMPANION_EXE_NAME):
        subprocess.run(
            ["taskkill", "/f", "/im", image], capture_output=True, text=True, timeout=30,
        )


def _separation_state_summary() -> str:
    """How far decision 6 got, and how it left the install.

    A Scheduler-started *packaged* daemon has exactly two outcomes, and the
    one thing both look like from outside is "no daemon serving where you
    expected one":

    * it repaired the install -- ``enforce_separation()``'s elevated
      ``enable`` took, so the data directory has moved from
      ``%LOCALAPPDATA%`` to ``%ProgramData%``, a service owns the daemon,
      the companion task is registered, the task that started all this is
      disabled, and the process itself has handed over and exited. That is
      the outcome both tests here are asserting.
    * it refused to serve -- the ``enable`` did not take (declined UAC, a
      rollback, a script that could not run at all), so decision 6 raised
      rather than opening /mcp or the approvals UI.

    The marker file, the service, and the two tasks' enabled states say
    which happened; nothing else here does, and a failure message that
    cannot tell them apart sends the next reader after the wrong bug -- see
    privacyfence/privacyfence#599, which was one of these misread as the
    other for ten consecutive red runs."""
    lines = [f"marker ({MARKER_PATH}): {'present' if MARKER_PATH.exists() else 'absent'}"]
    service = subprocess.run(
        ["sc.exe", "query", WINDOWS_SERVICE_NAME], capture_output=True, text=True, timeout=30,
    )
    lines.append(f"---- sc query {WINDOWS_SERVICE_NAME} ----\n{service.stdout}{service.stderr}")
    for task in (TASK_NAME, WINDOWS_COMPANION_TASK_NAME):
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


def _autostart_failure_context(installed: "_Installed") -> str:
    """Everything worth knowing when a Scheduler-started daemon comes up but
    never serves.

    ``_start_task_and_wait_for_daemon()`` already quotes the task state and
    the daemon log when the *process* never appears; this is the other half,
    for when it appears and then does nothing observable. The two questions
    are "what did the daemon say" and "is this still the install this module
    set up", so it collects both rather than making the next reader download
    this job's diagnostics artifact to find out."""
    return (
        f"---- separation state ----\n{_separation_state_summary()}\n"
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}\n"
        f"---- who is signed in (query user) ----\n{_session_table()}\n"
        f"---- daemon log (tail) ----\n{_daemon_log_tail(installed.home)}"
    )


def _remove_task() -> None:
    """Stop and delete the task *before* uninstalling, not after.

    The uninstaller removes it too ([UninstallRun]), but the crash-restart
    test leaves a just-relaunched daemon running when it finishes: left
    registered, the task's own <TimeTrigger> can relaunch the daemon again
    out of the directory the uninstaller is in the middle of deleting."""
    subprocess.run(["schtasks", "/end", "/tn", TASK_NAME], capture_output=True, text=True, timeout=20)
    subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"], capture_output=True, text=True, timeout=20)


# --------------------------------------------------------------------------- #
# Fixtures -- the real (un-isolated) home, and one real install per test
# --------------------------------------------------------------------------- #

@pytest.fixture
def _real_home_state(request):
    """The daemon's home is the real logged-on account's profile, not
    ``tmp_path``.

    Task Scheduler launches the action itself, with no injected environment
    -- that is the whole point here -- so there is nowhere to redirect
    ``%LOCALAPPDATA%`` (what the daemon's ``data_dir()`` actually resolves
    through on Windows -- see ``_data_dir()`` below) to even if this module
    wanted to. Same posture, and
    same safeguards, as ``test_linux_graphical_session_autostart.py``'s own
    identically-named fixture: skip rather than run if this account already
    has PrivacyFence state, and remove whatever this test creates.

    Also doubles as this module's own diagnostics capture, for the same
    reason ``test_linux_graphical_session_autostart.py``'s does -- the
    generic per-``tmp_path`` capture in ../conftest.py cannot see any of
    this, and a Scheduler-launched process has no redirected stdout of its
    own to collect either, so this reaches for what Task Scheduler knows
    (the task's own stored definition and last-run result) instead."""
    real_home = Path.home()
    state_dir = _data_dir(real_home)
    if state_dir.exists():
        pytest.skip(
            f"{state_dir} already exists -- this test boots the daemon into the real profile with "
            "no isolation (the whole point is a real, un-injected Task Scheduler launch); only run "
            "it against a disposable account with no existing PrivacyFence state"
        )
    try:
        yield real_home
    finally:
        rep_call = getattr(request.node, "rep_call", None)
        if rep_call is not None and rep_call.failed:
            dest = failure_dir(request.node.nodeid, suite=suite_name_for(__file__))
            write_environment_info(dest / "environment.txt")
            capture_directory_manifest(state_dir, dest / "manifest-profile-home.txt")
            (dest / "logs").mkdir(parents=True, exist_ok=True)
            task_info = subprocess.run(
                ["schtasks", "/query", "/tn", TASK_NAME, "/v", "/fo", "list"],
                capture_output=True, text=True,
            )
            (dest / "logs" / "schtasks-query.txt").write_text(
                task_info.stdout + task_info.stderr, encoding="utf-8",
            )
            # The stored definition, not this repo's template: which of the
            # two disagrees with the other is the whole question whenever a
            # task registers but then behaves unexpectedly.
            (dest / "logs" / "schtasks-query-xml.txt").write_text(
                _registered_task_xml_or_error(), encoding="utf-8",
            )
            (dest / "logs" / "query-user.txt").write_text(_session_table(), encoding="utf-8")
            (dest / "logs" / "taskscheduler-events.txt").write_text(
                _task_scheduler_events(120), encoding="utf-8",
            )
            # The daemon's own log, if it got far enough to write one -- see
            # _daemon_log_tail for why this is the only thing a
            # Scheduler-launched daemon leaves behind.
            copy_named_logs(state_dir, dest / "logs")
        shutil.rmtree(state_dir, ignore_errors=True)


class _Installed:
    """What a test needs to know about the installation the fixture made:
    the profile the daemon will boot into, the port its settings.yaml was
    seeded with, and where the installed alias exe actually is."""

    def __init__(self, *, home: Path, port: int, log_path: Path) -> None:
        self.home = home
        self.port = port
        self.log_path = log_path
        self.alias_exe = str(INSTALL_DIR / ALIAS_EXE_NAME)


@pytest.fixture
def _installed(_real_home_state, tmp_path):
    """One real silent install per test, and its full teardown -- both tests
    below need the same installed-and-registered starting state, and neither
    may leave a task or a daemon behind for the other (or for whatever runs
    next on this machine)."""
    home = _real_home_state
    port = _free_port()

    if INSTALL_DIR.exists():  # a previous run that died before its own cleanup
        _kill_alias_processes()
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)
    # Likewise for the machine-wide state an installer-run `enable` leaves --
    # a service and a %ProgramData% directory outlive an interrupted run the
    # same way a stray install directory does.
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
    # Back to an unseparated install -- the *starting* state this module is
    # about, not the ending one. The installer separates by itself (ADR 0003
    # decision 4), and an install that arrives here already separated has no
    # enabled daemon task to run and nothing left for decision 6 to do, so
    # there would be no autostart left to test. Undoing it also re-enables
    # the task `enable` disabled, which is what makes the assertion below
    # mean what it always meant. See the module docstring.
    #
    # require_separated=False: privacyfence/privacyfence#561 -- a `/DIR=`-
    # overridden install has come out of the installer's own `enable` call
    # with no privilege-separation.json to show for it, despite Setup itself
    # reporting success, so `disable` refusing to undo a separation that
    # never happened must not fail this fixture. See
    # `_disable_installer_enabled_privilege_separation`'s own docstring.
    _disable_installer_enabled_privilege_separation(INSTALL_DIR, require_separated=False)
    # Seeded only now, not before the install: `enable` *moves* this profile's
    # data directory under %ProgramData% and `disable` moves it back, so a
    # settings.yaml written beforehand would make its round trip part of this
    # fixture's setup for no reason. Nothing reads it until a test asks Task
    # Scheduler to start the daemon, which is well after this returns.
    _prepare_home(home, port=port)
    # ...and nothing older than that seeding may survive into the boot under
    # test -- see _clear_stale_discovery_files(), which is about the install's
    # own service having already written a set of these naming the default
    # port before this fixture seeded anything.
    _clear_stale_discovery_files(home)
    # RegisterAutostartTask (installer/privacyfence.iss's [Code] section)
    # doesn't abort Setup on its own failure, so a silent install can still
    # exit 0 with no task actually registered -- the install log (Inno's
    # own /LOG= output, which records every [Code] Exec call and its
    # result, schtasks' own stdout/stderr included) is the only way to see
    # why, short of downloading this test's own diagnostics artifact by
    # hand. Only the *tail*: this onedir bundle's own per-file [Files] copy
    # log alone runs to tens of thousands of lines, which previously pushed
    # the actually useful part (CurStepChanged(ssPostInstall)'s own
    # RegisterAutostartTask call, logged only after every file is already
    # copied) past what a CI log viewer -- or this test's own captured
    # stdout -- keeps readily available.
    assert _task_exists(), (
        f"Task Scheduler task {TASK_NAME!r} missing after install\n"
        f"---- install log (tail) ----\n{_install_log_tail(log_path)}"
    )

    # A silent install's own [Run] "launch now" step is skipifsilent -- it
    # must never fire under /VERYSILENT (test_windows_packaged_smoke.py's
    # own lifecycle test relies on the same fact); only Task Scheduler
    # should ever start the daemon in this module.
    assert not windows_pipe_exists(resolve_windows_pipe_name(_data_dir(home))), (
        "a silent install must never itself start the daemon -- only the autostart task should"
    )

    try:
        yield _Installed(home=home, port=port, log_path=log_path)
    finally:
        _remove_task()
        # Separation comes down *before* the uninstaller now, not after.
        # Both tests end with this install separated, which means a service
        # and a companion running out of INSTALL_DIR by the time this runs --
        # and Setup aborts at RestartManager ("Some applications could not be
        # shut down", Inno exit 5, install rolled back) rather than replacing
        # files a live process holds open. The floor rather than `disable`
        # deliberately: this runs on the failure path too, and a teardown that
        # can raise replaces the failure the test was actually reporting.
        _tear_down_separation()
        _kill_alias_processes()
        uninstaller = INSTALL_DIR / "unins000.exe"
        if uninstaller.is_file():
            _run_installer(str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)


def _assert_registered_task_matches_autostart_contract(exec_path: str) -> None:
    """The same contract tests/unit/test_windows_autostart_task_template.py
    holds the shipped template to (tests/windows_task_contract.py), asserted
    here against the document that actually governs: the one Task Scheduler
    parsed, normalized and stored when the installer registered it."""
    assert_task_xml_matches_autostart_contract(_registered_task_xml(), exec_path=exec_path)


def _start_task() -> None:
    """Ask Task Scheduler to run the installed task now.

    The module docstring's one deliberate substitution for a user signing
    in: the trigger decides *when*, and everything after that decision --
    resolving the ``Builtin\\Users`` principal to a signed-in member, that
    member's ``LeastPrivilege`` token, their profile, the action itself --
    is the same code path either way."""
    result = subprocess.run(
        ["schtasks", "/run", "/tn", TASK_NAME], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"schtasks /run failed (exit {result.returncode}):\n{result.stdout}{result.stderr}\n"
        f"---- who is signed in (query user) ----\n{_session_table()}\n"
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}"
    )


def _start_task_and_wait_for_daemon(installed: _Installed) -> tuple[str, str]:
    _start_task()
    found = _wait_for_alias_process(installed.alias_exe, timeout=30.0)
    assert found, (
        f"{installed.alias_exe} never appeared as a running process within 30s of Task Scheduler "
        f"being asked to run {TASK_NAME!r}\n"
        f"---- who is signed in (query user) ----\n{_session_table()}\n"
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}\n"
        f"---- daemon log (tail) ----\n{_daemon_log_tail(installed.home)}"
    )
    pid, owner = found
    # A group principal runs the action as a signed-in member, in that
    # member's own profile -- which is the account running this test, since
    # it is the only one with a real session here.
    assert getpass.getuser().lower() in owner.lower(), (
        f"{ALIAS_EXE_NAME} (pid {pid}) is running as {owner!r}, not the signed-in account "
        f"({getpass.getuser()!r}) the Builtin\\Users principal should have resolved to"
    )
    return pid, owner


# --------------------------------------------------------------------------- #
# The tests -- Phase 7 item 2 / 8.2 (Scheduler-started daemon, real system
# contract) and Phase 13 item 4 (crash-restart).
# --------------------------------------------------------------------------- #

async def test_installed_task_starts_the_packaged_daemon_which_separates_the_install(_installed):
    """Task Scheduler starts the packaged exe, and what that exe does next is
    ADR 0003 decision 6: separate this install, hand the daemon to the
    service, and stop.

    The first half is unchanged and is still the subject of this module --
    the definition the *service* stored, a real on-demand run, the real
    installed image, running as the real signed-in account. The second half
    replaces the assertions this test carried before decision 6: a control
    pipe under ``%LOCALAPPDATA%`` and an approval round trip against a daemon
    living in the user's own session. A packaged daemon does not do that any
    more, by design -- see the module docstring.
    """
    _assert_registered_task_matches_autostart_contract(_installed.alias_exe)

    pid, _owner = _start_task_and_wait_for_daemon(_installed)

    # ── Decision 6, from the outside: the marker is the last thing `enable`
    # writes, so its presence means "finished", not "started" ─────────────
    assert _wait_for_marker(timeout=180), (
        f"{MARKER_PATH} never appeared after Task Scheduler started {ALIAS_EXE_NAME} (pid {pid}) "
        f"-- the packaged daemon should have separated this install itself\n"
        f"{_autostart_failure_context(_installed)}"
    )

    # ── ...and the service it handed the daemon to is really serving ──────
    base_url, mcp_token = _wait_for_separated_service_serving(_installed)
    await _assert_separated_service_serves_mcp(base_url, mcp_token)
    assert SEPARATED_SETTINGS_PATH.is_file(), (
        f"{SEPARATED_SETTINGS_PATH} missing -- the service never wrote its own authority config"
    )

    # ── The arrangement that leaves behind: the companion is what now runs
    # in the signed-in session (ADR 0002 decision 5), and the task this test
    # just ran is disabled rather than left to start a second daemon at
    # every sign-in ───────────────────────────────────────────────────────
    assert _task_exists(WINDOWS_COMPANION_TASK_NAME), (
        f"the {WINDOWS_COMPANION_TASK_NAME!r} task was not registered\n"
        f"{_separation_state_summary()}"
    )
    assert _task_state(TASK_NAME) == "Disabled", (
        f"the {TASK_NAME!r} autostart task is {_task_state(TASK_NAME)!r}, not Disabled\n"
        f"{_separation_state_summary()}"
    )

    # ── And the process Task Scheduler started is gone -- that pid, not
    # that image. It separated the install and then had nothing left to be:
    # the daemon is the service's now, running as an account this process is
    # not (privilege_separation.SeparationHandover). One still alive here is
    # one serving the layout it just separated, as the human -- the silent
    # policy reset check_runtime_identity() exists to prevent. ────────────
    assert _wait_until_pid_gone(pid, timeout=60.0), (
        f"{ALIAS_EXE_NAME} (pid {pid}) is still running after separating this install -- it should "
        f"have handed over to the {WINDOWS_SERVICE_NAME} service and exited\n"
        f"{_autostart_failure_context(_installed)}"
    )
    # Exited *cleanly*: an autostart task that reports a failed run at every
    # sign-in is a support ticket, and this is the one outcome decision 6 is
    # trying to reach. `daemon_main.main()` returns 0 on the handover for
    # exactly this reason.
    last_result = _wait_for_task_last_result("0", timeout=30.0)
    assert last_result == "0", (
        f"the {TASK_NAME!r} task reports Last Result {last_result!r}, not 0 -- separating "
        f"and handing over is a successful run\n"
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}\n"
        f"---- daemon log (tail) ----\n{_daemon_log_tail(_installed.home)}"
    )


@pytest.mark.timeout(600)
async def test_crash_restart_relaunches_the_separated_daemon(_installed):
    """The positive crash-restart assertion this module has owed since a
    first measurement pass could not make it -- kill it, wait, assert a
    *new* pid -- against the thing that owns crash restart now.

    That used to be the task's own ``<RestartOnFailure>``, and measuring it
    is what retired it: Task Scheduler logs a killed action as a
    *successfully completed* task (from that run's own operational log::

        Event ID 201:  Task Scheduler successfully completed task
                       "\\PrivacyFence", instance "{63cf2afb-...}", action
                       "C:\\...\\privacyfence-app.exe" with return code
                       2147942401.

    ``2147942401`` is ``0x80070001``, the action's own non-zero exit
    surfaced as an HRESULT), so the setting never engages: it only ever
    answers a task that fails to *run*, not an action that ran and then
    died. The repeating ``<TimeTrigger>`` that replaced it is standing in
    for a service manager -- and on a separated install there *is* one. The
    daemon is a Windows service, and ``sc failure PrivacyFence actions=
    restart/5000/...`` (windows_privilege_separation.ps1's
    ``Install-DaemonService``, which says exactly that) is the mechanism.

    It cannot be the ``<TimeTrigger>`` answering here: that task is
    Disabled by the time this kills anything, which the test above asserts
    directly.
    """
    _start_task_and_wait_for_daemon(_installed)
    assert _wait_for_marker(timeout=180), (
        f"{MARKER_PATH} never appeared -- decision 6 did not separate this install, so there is "
        f"no service to crash\n{_autostart_failure_context(_installed)}"
    )
    _wait_for_separated_service_serving(_installed)

    first_pid = _service_pid()
    assert first_pid, (
        f"the {WINDOWS_SERVICE_NAME} service is not reporting a process id\n"
        f"{_separation_state_summary()}"
    )

    # A real crash, not a graceful stop: /f is a TerminateProcess, so the
    # service's own control handler never runs and the SCM sees a failure
    # rather than an orderly stop. That distinction is the whole point --
    # a service that is *asked* to stop does not exercise failure actions
    # at all.
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
        f"{_separation_state_summary()}"
    )
    # Still the service account, not something that happened to take the
    # name: a restart that came back as anyone else would be the #428
    # weakness reopening quietly.
    config = _service_config()
    assert config is not None and WINDOWS_SERVICE_ACCOUNT_NAME.lower() in config.lower(), (
        f"the relaunched {WINDOWS_SERVICE_NAME} service does not run as "
        f"{WINDOWS_SERVICE_ACCOUNT_NAME}:\n{config}"
    )
