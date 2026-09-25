"""Packaged-artifact lifecycle test for the Windows installer.

The same role ``tests/integration/test_macos_packaged_smoke.py`` (TST-15)
plays for the DMG and ``tests/integration/test_deb_packaged_lifecycle.py``
(Phase 6.3) plays for the ``.deb``: install the actual built artifact -- not
a source checkout, not an editable dev install -- and exercise it as closely
as possible to how a real user would.

Every install separates itself (ADR 0003 decision 4): ``installer/
privacyfence.iss``'s ``CurStepChanged(ssPostInstall)`` runs
``privilege-separation.ps1 enable``, which installs the daemon as the
``PrivacyFence`` Windows service running as ``NT SERVICE\\PrivacyFence``
and registers the companion's sign-in task. There is no daemon sign-in task
and no unseparated lifecycle to test (ADR 0041), so every scenario here runs
against the service the installer itself started.

1. **Install, serve, uninstall**
   (``test_windows_install_validate_scenario_uninstall_lifecycle``): a real
   silent run of ``scripts/build_installer.ps1``'s
   ``dist/PrivacyFence-<version>-setup.exe`` (Inno Setup 6) --
   ``/VERYSILENT /SUPPRESSMSGBOXES``, same as a user clicking through the
   wizard with every default accepted; the service serves MCP; then a real
   silent run of the installer's own generated ``unins000.exe``. Program
   files, the service and the companion task are gone afterwards; the data
   under ``%ProgramData%\\PrivacyFence`` is not (ADR 0042: a silent
   uninstall never purges), and nothing is moved into ``%LOCALAPPDATA%``.

   ``/DIR=`` is overridden to a scratch directory under this test's own
   ``tmp_path`` rather than the real ``%ProgramFiles%`` purely for isolation
   from whatever else is on the runner, not to dodge elevation:
   ``installer/privacyfence.iss`` is ``PrivilegesRequired=admin``, and this
   test relies on the hosted runner's own account already carrying a full,
   unfiltered admin token. That scratch directory is created with an
   administrators-only ACL before Setup is pointed at it
   (``_admin_only_writable_dir``), because ``enable``'s
   ``Assert-ImageProtected`` refuses an install directory the signed-in user
   can rewrite -- a service runs whatever its ``binPath`` names. Test 5 is
   the same fact asserted from the other side. The scratch directory also
   carries a space in its name (``INSTALL_DIR_NAME``), because a real
   install's ``C:\\Program Files\\PrivacyFence`` does and a runner's
   ``tmp_path`` does not -- see that constant for the defect that hid behind
   the difference.
2. **Upgrade in place** (``test_windows_upgrade_in_place_preserves_user_state``):
   install version N, then a synthetically-bumped version N+1 -- the
   identical PyInstaller ``dist/PrivacyFenceApp`` onedir output, re-packaged
   through a second ``iscc.exe`` invocation with a bumped ``/DAppVersion``
   (the same technique ``test_deb_packaged_lifecycle.py``'s
   ``_synthetic_next_version_deb`` uses) -- over it, at the same install
   directory, and confirm the service's own state survived byte for byte.
   ``installer/privacyfence.iss``'s fixed ``AppId`` is what makes this a real
   in-place upgrade rather than a side-by-side install.
3. **The install separates itself**
   (``test_windows_install_separates_with_no_manual_enable``): the marker,
   the service against the installed image and running as its own account,
   the installing account in ``PrivacyFenceUsers``, and the companion task
   -- with no separate ``enable`` call from this module, and no daemon task
   registered at all.
4. **Remove keeps data, reinstall picks it up, purge deletes it**
   (``test_windows_uninstall_keeps_data_and_purge_deletes_it``, ADR 0042):
   the Windows spelling of the ``.deb``'s ``apt remove``/``apt purge``.
5. **An install that cannot separate is not an install**
   (``test_windows_install_fails_when_the_image_is_user_writable``): Setup
   pointed at an ordinary, user-writable directory says why in its log and
   leaves no separation behind, rather than reporting success for an install
   whose approval UI would mean less than it says.

The approval round trip itself is not driven here; see
``_assert_separated_service_serves_mcp`` for why a CI job cannot reach the
control channel of a separated Windows install, and which modules cover it
on the other platforms.

Skipped entirely unless running on real Windows with a just-built
``dist/PrivacyFence-*-setup.exe`` on disk -- this only makes sense as a step
in ``.github/workflows/build.yml``'s ``build-windows`` job, right after
``scripts/build_installer.ps1``, never as part of the ordinary ``pytest``
invocation in ``tests.yml``'s per-PR jobs (same posture as the macOS/Linux
packaged tests). The upgrade test additionally needs ``iscc.exe`` on
``PATH`` and ``dist/PrivacyFenceApp``/``build/privacyfence.ico`` on disk --
both already there right after ``scripts/build_installer.ps1``'s own steps.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import warnings
from pathlib import Path
from typing import NamedTuple, NoReturn
from urllib.parse import urlsplit

import httpx2
import pytest

mcp_client = pytest.importorskip(
    "mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'"
)
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from privacyfence.privilege_separation import (  # noqa: E402
    MARKER_FILE_NAME,
    MARKER_VERSION,
    WINDOWS_COMPANION_TASK_NAME,
    WINDOWS_SERVICE_ACCOUNT_NAME,
    WINDOWS_SERVICE_GROUP_NAME,
    WINDOWS_SERVICE_NAME,
    WINDOWS_SYSTEM_ROOT,
)
from tests.packaged_policy_probe import PROBE_TOOL  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "dist"

# The name the daemon sign-in task had before ADR 0042's cleanup removed it.
# Asserted *absent*: the installer registers nothing it later disables.
REMOVED_DAEMON_TASK_NAME = "PrivacyFence"
MAIN_EXE_NAME = "PrivacyFenceApp.exe"
ALIAS_EXE_NAME = "privacyfence-app.exe"  # the service's binPath image, and what the mcpb shim looks for
COMPANION_EXE_NAME = "PrivacyFenceCompanion.exe"  # installer/privacyfence.iss's #define CompanionExeName

MCP_TOKEN_FILE_NAME = "mcp_token"  # web/mcp_auth.py's MCP_TOKEN_FILE_NAME

# installer/privacyfence.iss copies scripts/windows_privilege_separation.ps1
# into {app} under this name; [Code]'s SeparateInstall runs it from there.
SEPARATION_SCRIPT_NAME = "privilege-separation.ps1"
MARKER_PATH = WINDOWS_SYSTEM_ROOT / MARKER_FILE_NAME

# The scratch install directory's name, and the space in it is the point.
#
# Every real install goes to `C:\Program Files\PrivacyFence`; this module's
# `/DIR=` override used to point at a plain `install` directory under
# `tmp_path`, which on a hosted runner has no space anywhere in it. That one
# difference hid a defect that broke every real install for as long as Windows
# separation has existed: `sc create`'s binPath= value is
# `"<path>" --windows-service`, a single argument with quotes inside it, and
# Windows PowerShell 5.1's native-argument binder re-quotes such an argument
# without escaping the quotes already there. With no space in the path the
# mangled result still parses as one argument and the service is created; with
# one, sc.exe reads binPath= as `C:\Program`, rejects the rest as an unknown
# option, and the install dies at exit 1639 -- green here, broken everywhere
# else. See scripts/windows_privilege_separation.ps1's Invoke-NativeCommandLine.
#
# So the scratch directory carries a space on purpose now: a path shaped like
# the one users actually install into is the only input that exercises the
# quoting at all.
INSTALL_DIR_NAME = "Program Folder"

# How long one silent Setup / uninstaller run may take before it is treated as
# hung, from twelve build.yml runs on fresh windows-latest runners (2026-09-24;
# the run list is in the commit that set these values):
#
#   first (cold) install    28.2-58.3 s in eleven runs, 102.5 s in one
#                           (median 36.6 s)                      p100 102.5 s
#     of which `sc start`   16.6-27.6 s -- nearly all of it before the service
#                           process exists (15.2 / 17.8 s where measured; the
#                           executable then reaches its dispatcher in 0.3 s)
#     PowerShell's start    0.4-31.6 s;  file copy 6.4-26.7 s
#   every later install     8.5-19.0 s  (`sc start` 0.6-1.1 s)
#   uninstaller             1.6-12.9 s  (the first one on a runner is slowest)
#
# The 102.5 s run was slow everywhere at once (file copy 26.7 s against ~7 s,
# PowerShell's start 31.6 s against ~0.4 s): a slow runner rather than a slow
# step, and the shape v4.5.0a1's tag run most likely had when its first install
# overran the old 120 s. The installer gets ~3.5x that p100. The uninstaller
# keeps 120 s -- ~9x its p100 -- because `uninstall` itself may legitimately
# spend 60 s waiting on the service and 30 s on the companion before it moves on.
INSTALLER_TIMEOUT_S = 360.0
UNINSTALLER_TIMEOUT_S = 120.0
# pytest-timeout must outlast the subprocess timeouts above, or it kills the
# run before an installer's TimeoutExpired can say which step hung. One hung
# Setup or uninstaller run plus the service's own start/serve waits (~120 s).
# Observed whole tests, green: 8-78 s; the whole module 158-236 s.
TEST_TIMEOUT_S = INSTALLER_TIMEOUT_S + UNINSTALLER_TIMEOUT_S + 120.0
# Tests 2 and 4 install twice: the same one-hang allowance, plus 3x the
# slowest such test observed (78 s) for everything else they do.
MULTI_INSTALL_TEST_TIMEOUT_S = TEST_TIMEOUT_S + 3 * 78.0

# The separated layout's own files, as `enable` leaves them on disk. Readable
# directly rather than through an elevation shim, unlike the POSIX modules'
# `sudo` reads: Set-Layout grants Administrators full control of the whole
# tree, and every test in this module already runs elevated (the installer
# needs it -- see this module's own docstring).
SEPARATED_HANDOFF_DIR = WINDOWS_SYSTEM_ROOT / "handoff"
SEPARATED_AUTHORITY_DIR = WINDOWS_SYSTEM_ROOT / "authority"
# ADR 0008 D3: the owner's own mcp_token lives under the *authority* root,
# not handoff/ -- handoff/ is unaffected (still web_base_url and the
# control channel's named pipe, which lives outside the filesystem here).
SEPARATED_MCP_TOKEN_PATH = SEPARATED_AUTHORITY_DIR / MCP_TOKEN_FILE_NAME
SEPARATED_WEB_BASE_URL_PATH = SEPARATED_HANDOFF_DIR / "web_base_url"
SEPARATED_SETTINGS_PATH = SEPARATED_AUTHORITY_DIR / "config" / "settings.yaml"


def _built_installers() -> list[Path]:
    return sorted(DIST_DIR.glob("PrivacyFence-*-setup.exe")) if DIST_DIR.is_dir() else []


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
    # A real silent install + a real daemon cold start + a real silent uninstall is comfortably
    # slower than the suite's default timeout=30 -- same reasoning as every other packaged/system
    # test in this repo. See TEST_TIMEOUT_S for how long.
    pytest.mark.timeout(TEST_TIMEOUT_S),
]


# --------------------------------------------------------------------------- #
# Task Scheduler helpers
# --------------------------------------------------------------------------- #

def _task_exists(name: str) -> bool:
    result = subprocess.run(["schtasks", "/query", "/tn", name], capture_output=True, text=True, timeout=15)
    return result.returncode == 0


# --------------------------------------------------------------------------- #
# Privilege separation -- what the install does to itself (ADR 0003
# decision 4), and how it comes back down (ADR 0042).
# --------------------------------------------------------------------------- #

# Well-known SIDs rather than names, for the reason
# scripts/windows_privilege_separation.ps1's own constants give: "BUILTIN\Users"
# is "BUILTIN\Utilisateurs" on a French Windows and icacls would reject it.
SID_SYSTEM = "*S-1-5-18"
SID_ADMINISTRATORS = "*S-1-5-32-544"
SID_USERS = "*S-1-5-32-545"


def _icacls(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["icacls", *args], capture_output=True, text=True, timeout=60)
    if check:
        assert result.returncode == 0, f"icacls {' '.join(args)} failed:\n{result.stdout}{result.stderr}"
    return result


def _admin_only_writable_dir(path: Path) -> Path:
    """Creates ``path`` with the ACL a real ``%ProgramFiles%`` install has:
    full control for SYSTEM and Administrators, read-and-execute for everyone
    else, and no inheritance from whatever ``tmp_path`` sits under.

    Needed because the two halves of what this module wants now pull against
    each other. Isolation wants the install somewhere disposable; ADR 0003
    decision 4 has Setup run ``enable`` on itself, and ``enable`` refuses an
    install directory the signed-in user can rewrite (``Assert-ImageProtected``
    -- a service runs whatever its ``binPath`` names, so a writable image is a
    way for the agent to run its own code *as* the service account). A plain
    ``tmp_path`` subdirectory fails that, correctly. Rather than dodge the
    check, this reproduces the property a real install has, explicitly, so the
    rest of the module keeps its scratch directory.

    ``/setowner`` too, not only the grants: an object's owner holds WRITE_DAC
    implicitly on Windows, so leaving it owned by the account this test runs as
    would make the ACL above advisory -- the same reasoning
    ``Set-Layout`` gives for re-owning the separated root itself.
    """
    path.mkdir(parents=True, exist_ok=True)
    _icacls(str(path), "/inheritance:r", "/q")
    _icacls(
        str(path), "/grant:r",
        f"{SID_SYSTEM}:(OI)(CI)(F)", f"{SID_ADMINISTRATORS}:(OI)(CI)(F)", f"{SID_USERS}:(OI)(CI)(RX)",
        "/q",
    )
    _icacls(str(path), "/setowner", SID_ADMINISTRATORS, "/t", "/c", "/q")
    return path


def _run_separation_script(install_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """The installed script, run the way ``installer/privacyfence.iss``'s own
    ``SeparateInstall`` runs it -- same interpreter flags, so a failure here
    and a failure there are the same failure."""
    script = install_dir / SEPARATION_SCRIPT_NAME
    assert script.is_file(), f"{script} missing -- installer/privacyfence.iss's [Files] entry for it changed?"
    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), *args,
        ],
        capture_output=True, text=True, timeout=180,
    )
    if check:
        assert result.returncode == 0, (
            f"{SEPARATION_SCRIPT_NAME} {' '.join(args)} failed (exit {result.returncode}):\n"
            f"{result.stdout}{result.stderr}"
        )
    return result


def _service_is_running(name: str = WINDOWS_SERVICE_NAME) -> bool:
    result = subprocess.run(["sc.exe", "query", name], capture_output=True, text=True, timeout=30)
    return result.returncode == 0 and "RUNNING" in result.stdout


def _wait_for_separated_service(*, timeout: float = 90.0) -> tuple[str, str]:
    """Waits for the real ``PrivacyFence`` service the installer just created
    and started, and returns its ``(base_url, mcp_token)``.

    ``web_base_url`` is the daemon's own way of telling the companion which
    port it bound (web/control_channel.py's WEB_BASE_URL_FILE_NAME), and it is
    cleared on WebServer.stop(), so its presence means *this* boot is serving
    rather than that some earlier one left a file behind."""
    deadline = time.monotonic() + timeout
    base_url = None
    while time.monotonic() < deadline:
        if _service_is_running() and SEPARATED_WEB_BASE_URL_PATH.exists():
            candidate = SEPARATED_WEB_BASE_URL_PATH.read_text(encoding="utf-8").strip()
            if candidate:
                base_url = candidate
                break
        time.sleep(0.25)
    assert base_url, (
        f"the {WINDOWS_SERVICE_NAME} service never reported a web_base_url within {timeout}s "
        f"(running={_service_is_running()}, config=\n{_service_config()})"
    )
    mcp_token = None
    while time.monotonic() < deadline:
        if SEPARATED_MCP_TOKEN_PATH.exists():
            candidate = SEPARATED_MCP_TOKEN_PATH.read_text(encoding="utf-8").strip()
            if candidate:
                mcp_token = candidate
                break
        time.sleep(0.25)
    assert mcp_token, f"{SEPARATED_MCP_TOKEN_PATH} never appeared within {timeout}s"
    parts = urlsplit(base_url)
    _wait_until_connectable(parts.hostname or "localhost", parts.port or 80, timeout=30.0)
    return base_url, mcp_token


async def _assert_separated_service_serves_mcp(base_url: str, mcp_token: str) -> None:
    """The real service's own /mcp surface, over the token it wrote to the
    handoff directory -- the furthest this module can take a separated install
    from inside a CI job, and why it stops there:

    An approval round trip needs a session PrivacyFence can attribute to a
    person, which means minting over the daemon's control channel. That pipe's
    DACL (web/control_channel.py's _current_user_security_attributes) names the
    service account and ``PrivacyFenceUsers`` -- not Administrators -- and
    while `enable` does add the installing human to that group, Windows puts
    group memberships in the *logon token*: the script says so itself ("sign
    out and back in ... the session you are in right now still does not know it
    is in PrivacyFenceUsers"). A CI job has no sign-out, so this process cannot
    open that pipe however elevated it is.

    So the approval half of the round trip is covered on the separated path by
    test_deb_packaged_lifecycle.py and test_macos_packaged_smoke.py, which have
    a `sudo` that really does change identity, and what stays here is what only
    Windows can answer: the installer produced a service that runs, binds, and
    serves MCP as its own account. Reproducing the approval round trip here
    needs a helper launched with a freshly-built token (a one-shot scheduled
    task would do it) and is deliberately left as follow-up rather than guessed
    at."""
    headers = {"Authorization": f"Bearer {mcp_token}"}
    async with httpx2.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(f"{base_url}/mcp", http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
    assert "privacyfence_check_policy" in names, names
    assert PROBE_TOOL in names, names


def _service_config(name: str = WINDOWS_SERVICE_NAME) -> str | None:
    """``sc.exe qc`` output for a service, or None if it does not exist."""
    result = subprocess.run(["sc.exe", "qc", name], capture_output=True, text=True, timeout=30)
    return result.stdout if result.returncode == 0 else None


EVENT_SOURCE_KEY = rf"SYSTEM\CurrentControlSet\Services\EventLog\Application\{WINDOWS_SERVICE_NAME}"


def _event_message_file() -> str | None:
    """The EventMessageFile `enable` registered for the service's Event Log
    source, or None if the source is not registered."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, EVENT_SOURCE_KEY) as key:
            return str(winreg.QueryValueEx(key, "EventMessageFile")[0])
    except FileNotFoundError:
        return None


def _service_event_messages() -> list[str]:
    """The rendered text of the service's recent Application-log entries.

    Filtering by provider name is itself part of the check: Get-WinEvent
    rejects a -FilterHashtable naming a provider Windows has no registration
    for, which is how an unregistered source first showed up on a real
    install."""
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-WinEvent -FilterHashtable @{LogName='Application'; "
            f"ProviderName='{WINDOWS_SERVICE_NAME}'}} -MaxEvents 20 | ForEach-Object {{ $_.Message }}",
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"Get-WinEvent failed:\n{result.stdout}\n{result.stderr}"
    return [line for line in result.stdout.splitlines() if line.strip()]


def _local_group_members(group: str) -> list[str]:
    result = subprocess.run(["net", "localgroup", group], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []
    # `net localgroup` frames its member list between a dashed rule and the
    # "The command completed successfully." trailer.
    lines = result.stdout.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if set(line.strip()) == {"-"}) + 1
    except StopIteration:
        return []
    return [line.strip() for line in lines[start:] if line.strip() and not line.startswith("The command")]


def _task_state(name: str) -> str | None:
    result = subprocess.run(
        ["schtasks", "/query", "/tn", name, "/fo", "list", "/v"], capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return None
    match = re.search(r"^Scheduled Task State:\s*(.+)$", result.stdout, re.MULTILINE)
    return match.group(1).strip() if match else None


def _tear_down_separation() -> tuple[list[_Process], list[_Process]]:
    """Removes whatever an installer-run ``enable`` left behind, without going
    through the script, and returns ``(found, survivors)``: every
    PrivacyFence-related process alive before the sweep started, and every one
    still alive once it gave up waiting -- empty on a clean machine and after a
    successful sweep respectively. Judging either is the caller's business
    (see ``_clean_separation_state``).

    ``uninstall -Purge`` is the supported route; this is the floor under it,
    for a run that died between ``enable`` and its own teardown, or one where
    the install directory (and with it the script) is already gone -- which
    after a silent uninstall is the ordinary case, since that keeps the data
    (ADR 0042). Machine-wide state -- a service, a scheduled task, a directory
    under ``%ProgramData%`` and the processes behind them -- is not something
    ``tmp_path`` isolates, so leaving any of it behind would poison whatever
    runs next on this runner.

    Inno Setup processes go first, so that an installer still running
    ``enable`` (or an uninstaller's clean-up helper still deleting files) is
    not re-creating what the rest of the sweep removes. Every process is then
    waited for until it is gone from the process table, not just until
    ``taskkill`` returned: ``taskkill /F`` and ``sc.exe stop`` both only ask,
    and a dying process still holds its files and its image open.

    ``takeown`` before the delete because a separated root is owned by
    Administrators and its ``authority\\`` subtree grants the service account
    and nothing else; ``shutil.rmtree`` on its own would stop at the first
    directory it cannot open. The ``PrivacyFenceUsers`` group is deliberately
    left alone -- a plain ``uninstall`` leaves it too, and re-adding a member
    is idempotent.
    """
    found = _privacyfence_processes()
    for process in _inno_processes():
        subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, text=True, timeout=30,
            )
    subprocess.run(["sc.exe", "stop", WINDOWS_SERVICE_NAME], capture_output=True, text=True, timeout=60)
    subprocess.run(["sc.exe", "delete", WINDOWS_SERVICE_NAME], capture_output=True, text=True, timeout=60)
    subprocess.run(
        ["schtasks", "/delete", "/tn", WINDOWS_COMPANION_TASK_NAME, "/f"],
        capture_output=True, text=True, timeout=30,
    )
    # Deleting the task does not end the companion the `enable` that registered
    # it already started (Install-CompanionTask runs `schtasks /run` on it
    # itself, so every separated install leaves one running). A live
    # PrivacyFenceCompanion.exe holds the install directory open, which makes
    # the *next* silent install abort at RestartManager's "Some applications
    # could not be shut down" -- Inno exit 5, a rolled-back install, and a
    # failure that lands in whatever test asked for that install rather than
    # in the one that leaked the process. Same reasoning as the service stop
    # above: what `enable` started, the floor under it has to end. The daemon's
    # images are killed too because `sc.exe stop` only asks; the service is
    # already marked for deletion by then, so the SCM's restart-on-failure
    # action cannot bring it back.
    survivors = _wait_for_no_privacyfence_processes(timeout=_SWEEP_TIMEOUT)
    if WINDOWS_SYSTEM_ROOT.exists():
        subprocess.run(
            ["takeown", "/f", str(WINDOWS_SYSTEM_ROOT), "/r", "/d", "Y"],
            capture_output=True, text=True, timeout=120,
        )
        _icacls(str(WINDOWS_SYSTEM_ROOT), "/grant", f"{SID_ADMINISTRATORS}:(OI)(CI)(F)", "/t", "/c", "/q",
                check=False)
        shutil.rmtree(WINDOWS_SYSTEM_ROOT, ignore_errors=True)
    return found, survivors


# The images a PrivacyFence install runs: the app, its alias (the service's
# binPath image) and the companion. Inno Setup's own processes are matched by
# _inno_processes()'s rules instead.
_PRIVACYFENCE_IMAGE_NAMES = frozenset(name.lower() for name in (MAIN_EXE_NAME, ALIAS_EXE_NAME, COMPANION_EXE_NAME))
# How long a sweep waits for what it killed to leave the process table.
_SWEEP_TIMEOUT = 30.0


def _privacyfence_processes() -> list[_Process]:
    """Every live process a test in this module can have started, directly or
    through the installer: Inno Setup's (``_inno_processes``) and the
    installed app's own images."""
    inno = _inno_processes()
    inno_pids = {process.pid for process in inno}
    return inno + [
        process for process in _win32_processes()
        if process.name.lower() in _PRIVACYFENCE_IMAGE_NAMES and process.pid not in inno_pids
    ]


def _wait_for_no_privacyfence_processes(timeout: float) -> list[_Process]:
    """Kills every ``_privacyfence_processes()`` match and polls until none is
    left, or ``timeout`` runs out; returns whatever was still alive then.

    Kills on every round, not once, because what the first round kills can
    have started a successor in between (an installer's child, a restarted
    service) -- the wait is for the table to be empty, not for one PID."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = _privacyfence_processes()
        if not remaining or time.monotonic() >= deadline:
            return remaining
        for process in remaining:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, text=True, timeout=30,
            )
        time.sleep(0.5)


def _process_list(processes: list[_Process]) -> str:
    return "\n".join(f"  {process}" for process in processes)


@pytest.fixture(autouse=True)
def _clean_separation_state(request):
    """Guaranteed on both sides: a service, a scheduled task, a directory
    under ``%ProgramData%`` and the processes behind them are machine-wide, not
    something ``tmp_path`` isolates, and every test here creates all of them.

    **The leak rule.** Every test in this module ends with nothing of
    PrivacyFence running: tests 1-4 finish by uninstalling (the uninstaller's
    own ``privilege-separation.ps1 uninstall`` stops the service and ends the
    companion, and waits for both to be gone), and test 5's install never gets
    as far as starting either. So once a test body has *passed*, any
    PrivacyFence-related process still alive -- an Inno Setup installer or
    uninstaller or one of their ``.tmp`` helpers, ``PrivacyFenceApp.exe``,
    ``privacyfence-app.exe`` or ``PrivacyFenceCompanion.exe`` -- is something
    that test started and did not see finish, and the teardown half fails that
    test with the process list. That is the same shape as every instability
    this module has had (a timed-out install's ``enable`` still running, an
    uninstall helper outliving the uninstall, a companion outliving its task):
    a failure here lands in the test that leaked, not in whichever test next
    asked for an install.

    A body that failed or was skipped leaves whatever it was in the middle of
    -- the service and companion of a half-finished test are expected there,
    not a second defect -- so its leftovers are only reported as a warning.
    What survives the sweep itself fails the teardown either way: it would
    carry into the next test regardless of whose it was.

    The setup half sweeps and waits the same way but never fails: anything it
    finds belongs to whatever ran before this test (which, if it was a test in
    this module, has already failed for it), so it is only warned about.
    """
    found, survivors = _tear_down_separation()
    if found:
        warnings.warn(
            "PrivacyFence processes were already running before this test and were swept:\n"
            + _process_list(found),
            stacklevel=1,
        )
    if survivors:
        warnings.warn(
            f"PrivacyFence processes were still running {_SWEEP_TIMEOUT:.0f}s after the setup sweep "
            f"killed them -- this test runs against a machine something else is still changing:\n"
            + _process_list(survivors),
            stacklevel=1,
        )
    yield
    found, survivors = _tear_down_separation()
    call = getattr(request.node, "rep_call", None)
    body_passed = call is not None and call.passed
    problems = []
    if found and body_passed:
        problems.append(
            "this test passed but left PrivacyFence processes running when it returned -- every test in "
            "this module ends uninstalled, so these are something it started and did not wait for (see "
            "_clean_separation_state's docstring):\n" + _process_list(found)
        )
    elif found:
        warnings.warn(
            "swept PrivacyFence processes this (failed or skipped) test left behind:\n" + _process_list(found),
            stacklevel=1,
        )
    if survivors:
        problems.append(
            f"PrivacyFence processes were still running {_SWEEP_TIMEOUT:.0f}s after the teardown sweep "
            f"killed them, and will carry into the next test:\n" + _process_list(survivors)
        )
    if problems:
        pytest.fail("\n\n".join(problems), pytrace=False)


def _wait_until_connectable(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.1)
    raise TimeoutError(f"{host}:{port} never became connectable") from last_exc


# --------------------------------------------------------------------------- #
# Inno Setup processes -- running Setup or the uninstaller, and knowing when
# every process it started has really gone
# --------------------------------------------------------------------------- #
#
# Neither `setup.exe` nor `unins000.exe` is the process that does the work.
# Setup extracts its real installer to `%TEMP%\is-XXXXX.tmp\<name>.tmp` and
# runs that as a child (which in turn runs `privilege-separation.ps1 enable`);
# the uninstaller copies itself to a `_iu*.tmp` in `%TEMP%` and runs the copy,
# and a `_un*.tmp` helper deletes `unins000.exe`, `unins000.dat` and the
# install directory *after* the process that was waited on has exited. So a
# timeout that kills only the process `subprocess` started leaves the
# installer running -- still registering the service, the companion task and
# `%ProgramData%` state underneath the fixture teardown and whatever test runs
# next -- and "the uninstaller returned" does not mean "the uninstall is
# finished". The helpers below are how this module handles both.

# Setup's bootstrap and the uninstaller's own image, by name. The helpers they
# start all run from a `.tmp` image (see above), which nothing else on a CI
# runner does, so those are matched by extension rather than by the exact
# naming scheme of whichever Inno Setup version built the installer.
_INNO_LAUNCHER_NAME = re.compile(r"^(unins\d{3}\.exe|PrivacyFence-.*setup\.exe)$", re.IGNORECASE)
# How many lines of an installer's /LOG= file a timeout failure quotes.
_INSTALL_LOG_TAIL_LINES = 100


class _Process(NamedTuple):
    """One row of ``Win32_Process``. ``path``/``command_line`` are None where
    Windows declines to say (a protected process, or one already exiting)."""

    pid: int
    parent_pid: int
    name: str
    path: str | None
    command_line: str | None

    def __str__(self) -> str:
        return f"pid {self.pid} (parent {self.parent_pid}): {self.command_line or self.path or self.name}"


def _win32_processes() -> list[_Process]:
    """Every live process, with executable path and command line --
    ``tasklist`` reports neither, and the path (``is-XXXXX.tmp``) and command
    line (which ``privilege-separation.ps1`` step) are what a hung install's
    diagnosis needs."""
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_Process | "
        "Select-Object ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine)"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, f"listing processes failed (exit {result.returncode}):\n{result.stderr}"
    rows = json.loads(result.stdout or "[]")
    if isinstance(rows, dict):  # defensive: a single object rather than a one-element array
        rows = [rows]
    return [
        _Process(
            pid=int(row["ProcessId"]),
            parent_pid=int(row["ParentProcessId"] or 0),
            name=row["Name"] or "",
            path=row["ExecutablePath"],
            command_line=row["CommandLine"],
        )
        for row in rows
    ]


def _inno_processes() -> list[_Process]:
    """Every live Inno Setup process on the machine: Setup's bootstrap
    (``PrivacyFence-*setup.exe``) and its ``is-*.tmp\\*.tmp`` installer, and
    the uninstaller (``unins000.exe``) and its ``_iu*.tmp``/``_un*.tmp``
    copies and clean-up helpers.

    Machine-wide, not scoped to one run: an Inno process left over from an
    earlier step is exactly as able to change machine state under a later one
    as the current run's own. Includes a run the caller is itself still
    waiting on, so call this after that run has returned."""
    return [
        process for process in _win32_processes()
        if process.name.lower().endswith(".tmp") or _INNO_LAUNCHER_NAME.match(process.name)
    ]


def _wait_for_no_inno_processes(timeout: float = 60.0) -> list[_Process]:
    """Polls until ``_inno_processes()`` is empty, or ``timeout`` runs out.

    Returns whatever was still alive at the deadline -- empty on success --
    rather than failing itself, so that each caller can say in its own
    assertion what the survivors mean for the step it was waiting on."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = _inno_processes()
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(0.5)


def _descendants(pid: int, processes: list[_Process]) -> list[_Process]:
    children: dict[int, list[_Process]] = {}
    for process in processes:
        if process.pid != process.parent_pid:  # the System Idle Process is its own parent
            children.setdefault(process.parent_pid, []).append(process)
    found: list[_Process] = []
    seen = {pid}
    pending = [pid]
    while pending:
        for child in children.get(pending.pop(), []):
            if child.pid not in seen:  # a reused PID can make the parent links loop
                seen.add(child.pid)
                found.append(child)
                pending.append(child.pid)
    return found


def _log_tail(log_path: Path | None) -> str:
    if log_path is None:
        return "(this run was not given a /LOG= file)"
    if not log_path.exists():
        return f"({log_path} was never written)"
    lines = log_path.read_text(errors="replace").splitlines()
    return "\n".join(lines[-_INSTALL_LOG_TAIL_LINES:])


# `installer/privacyfence.iss`'s SeparateInstall runs `enable` through
# `cmd /C "... > "<is-XXXXX.tmp>\privilege-separation.out" 2>&1"` and copies
# that file into the /LOG= file only once `enable` returns -- so for an install
# that hangs *in* `enable`, the log's last line is "SeparateInstall: running"
# and what `enable` was doing is only in the redirect target, which a killed
# installer leaves behind in its temp directory.
_REDIRECT_TARGET = re.compile(r'>\s*"([^"]+)"')


def _redirect_tails(tree: list[_Process]) -> str:
    sections = []
    for member in tree:
        for target in _REDIRECT_TARGET.findall(member.command_line or ""):
            sections.append(f"---- last {_INSTALL_LOG_TAIL_LINES} lines of {target} ----\n{_log_tail(Path(target))}")
    return "\n".join(sections)


def _kill_installer_tree(process: subprocess.Popen, timeout: float, log_path: Path | None) -> NoReturn:
    """Ends an installer run that outlived ``timeout``, all of it, and fails
    with what it was doing.

    ``subprocess.run(timeout=)`` would kill ``process`` alone, which is the
    one process in the tree doing nothing but waiting (see this section's
    header). So the tree is walked from ``process`` while it is still alive
    to anchor it -- ``taskkill /T`` finds children through their parent PID --
    and then nothing is reported until no Inno process remains, so that the
    fixture teardown this failure hands over to runs against a machine
    nothing else is still changing."""
    snapshot = _win32_processes()
    tree = [p for p in snapshot if p.pid == process.pid] + _descendants(process.pid, snapshot)
    killed = subprocess.run(
        ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, text=True, timeout=60,
    )
    if killed.returncode != 0:
        # The root exited between the timeout and the taskkill, so /T had
        # nothing to walk from; end what the snapshot saw under it instead.
        for member in tree:
            subprocess.run(["taskkill", "/F", "/PID", str(member.pid)], capture_output=True, text=True, timeout=15)
    survivors = _wait_for_no_inno_processes(timeout=20.0)
    for survivor in survivors:
        subprocess.run(["taskkill", "/F", "/PID", str(survivor.pid)], capture_output=True, text=True, timeout=15)
    still_alive = _wait_for_no_inno_processes(timeout=20.0) if survivors else []
    try:
        process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        pass  # something outside the tree still holds its pipes; the failure below matters more
    pytest.fail(
        f"{process.args[0]} did not finish within {timeout:.0f}s, so its whole process tree was killed.\n"
        f"---- process tree at the timeout ----\n"
        + ("\n".join(str(p) for p in tree) or "(the root had already exited)")
        + f"\n---- taskkill /T /F /PID {process.pid} (exit {killed.returncode}) ----\n"
        f"{killed.stdout}{killed.stderr}"
        f"---- Inno Setup processes that survived it, and were killed by PID ----\n"
        + ("\n".join(str(p) for p in survivors) or "(none)")
        + "\n---- Inno Setup processes still alive after that ----\n"
        + ("\n".join(str(p) for p in still_alive) or "(none)")
        + f"\n---- last {_INSTALL_LOG_TAIL_LINES} lines of the install log ----\n{_log_tail(log_path)}\n"
        + _redirect_tails(tree)
    )


def _run_installer(
    *args: str, timeout: float = INSTALLER_TIMEOUT_S, log_path: Path | None = None,
) -> subprocess.CompletedProcess:
    """Runs Setup or an uninstaller and returns its result. Past ``timeout``,
    kills the whole Inno process tree and fails with the tail of ``log_path``
    (the run's ``/LOG=`` file, when it has one) -- see _kill_installer_tree."""
    process = subprocess.Popen(list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_installer_tree(process, timeout, log_path)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


# --------------------------------------------------------------------------- #
# Install timings -- where a silent install's time goes, in a *passing* job's
# log too. Setup's /LOG timestamps every line; `enable`'s own lines arrive in
# it only after the script exits (all stamped with that one moment), so they
# carry their own [HH:mm:ss.fff] (scripts/windows_privilege_separation.ps1's
# Write-Note) and are timed from that instead.
# --------------------------------------------------------------------------- #

_INNO_LINE = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d\.\d{3})\s+(.*)$")
_ENABLE_NOTE = re.compile(r"^SeparateInstall: (?:WARNING: )?\[(\d\d:\d\d:\d\d\.\d{3})\] (?:-> )?(.*)$")
# Checkpoints in the order Setup reaches them; each phase runs from its own
# checkpoint to the next one found.
_INSTALL_PHASES = (
    ("start", "Log opened."),
    ("prepare", "PrepareToInstall:"),
    ("files", "Starting the installation process."),
    ("enable", "SeparateInstall: running"),
    ("finish", "SeparateInstall: exit code"),
    ("end", "Log closed."),
)
_terminal_reporter = None


def _seconds(clock: str) -> float:
    hours, minutes, seconds = clock.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _install_timing_summary(log_path: Path) -> list[str]:
    """Two lines: Setup's phases, then `enable`'s own steps. Timings only --
    nothing here asserts, so a log in an unexpected shape reports less rather
    than failing a test that otherwise passed."""
    if not log_path.exists():
        return [f"install timings ({log_path.name}): no log"]
    stamped = []
    for line in log_path.read_text(errors="replace").splitlines():
        match = _INNO_LINE.match(line)
        if match:
            stamped.append((_seconds(match.group(2)), match.group(3)))
    if not stamped:
        return [f"install timings ({log_path.name}): no timestamped lines"]
    found = []
    for name, marker in _INSTALL_PHASES:
        hit = next((t for t, text in stamped if text.startswith(marker)), None)
        if hit is not None:
            found.append((name, hit))
    last = stamped[-1][0]
    total = (last - stamped[0][0]) % 86400
    phases = [
        f"{name} {((found[i + 1][1] if i + 1 < len(found) else last) - t) % 86400:.1f}s"
        for i, (name, t) in enumerate(found) if name != "end"
    ]
    lines = [f"install timings ({log_path.name}): total {total:.1f}s | " + " | ".join(phases)]

    notes = [(_seconds(m.group(1)), m.group(2)) for m in
             (_ENABLE_NOTE.match(text) for _, text in stamped) if m]
    if notes:
        enable_start = next((t for name, t in found if name == "enable"), notes[0][0])
        enable_end = next((t for name, t in found if name == "finish"), notes[-1][0])
        steps = [f"powershell start {(notes[0][0] - enable_start) % 86400:.1f}s"]
        for i, (t, text) in enumerate(notes):
            until = notes[i + 1][0] if i + 1 < len(notes) else enable_end
            steps.append(f"{text[:48]} {(until - t) % 86400:.1f}s")
        lines.append(f"  enable steps ({log_path.name}): " + " | ".join(steps))
    return lines


def _report(lines: list[str]) -> None:
    """Into the job log even when the test passes (pytest's own capture would
    swallow a print), and into the step summary when there is one."""
    if _terminal_reporter is not None:
        for line in lines:
            _terminal_reporter.write_line(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("".join(f"    {line}\n" for line in lines))


@pytest.fixture(autouse=True)
def _installer_timings(pytestconfig, monkeypatch):
    """Reports every Setup/uninstaller run's wall time, whichever helper or
    test made it, by wrapping ``_run_installer`` for the test's duration."""
    global _terminal_reporter
    _terminal_reporter = pytestconfig.pluginmanager.get_plugin("terminalreporter")
    module = sys.modules[__name__]
    wrapped = module._run_installer

    def _timed(*args, **kwargs):
        started = time.monotonic()
        outcome = "timed out"
        try:
            result = wrapped(*args, **kwargs)
            outcome = f"exit {result.returncode}"
            return result
        finally:
            _report([f"installer run {Path(args[0]).name}: {time.monotonic() - started:.1f}s ({outcome})"])

    monkeypatch.setattr(module, "_run_installer", _timed)
    yield
    _terminal_reporter = None


def _install(setup_exe: Path, install_dir: Path, log_path: Path) -> None:
    try:
        result = _run_installer(
            str(setup_exe),
            "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
            f"/DIR={install_dir}",
            f"/LOG={log_path}",
            timeout=INSTALLER_TIMEOUT_S,
            log_path=log_path,
        )
    finally:
        _report(_install_timing_summary(log_path))
    assert result.returncode == 0, (
        f"installer failed (exit {result.returncode}):\n{result.stdout}{result.stderr}\n"
        f"---- install log ----\n{log_path.read_text(errors='replace') if log_path.exists() else '(missing)'}"
    )


def _uninstall_leftovers(install_dir: Path) -> list[str]:
    """Everything still under ``install_dir`` -- the uninstaller's own
    ``unins000.exe``/``unins000.dat`` included -- which an uninstall that has
    really finished leaves none of.

    The directory itself may stay, empty. Inno removes ``{app}`` only if Setup
    created it (``MakeDir`` in is-6_7_3's ``Setup.Install.pas`` logs a
    directory for uninstall only when it did not exist yet), and every test
    here pre-creates it with ``_admin_only_writable_dir``."""
    return sorted(str(path) for path in install_dir.iterdir()) if install_dir.is_dir() else []


def _silent_uninstall(install_dir: Path, settle_timeout: float = 60.0) -> None:
    """``unins000.exe /VERYSILENT``, and wait until the uninstall has really
    finished: ``unins000.exe``, ``unins000.dat`` and everything else under
    ``install_dir`` gone, and no Inno Setup process left alive.

    ``unins000.exe`` returning is not that. Per is-6_7_3's
    ``Setup.Uninstall.pas``, it is only the first phase: it copies itself to
    ``%TEMP%\\is-XXXXXXXXXX-uninstall.tmp\\_unins.tmp`` and waits for that
    second phase, which does the uninstall and, at the very end
    (``DeleteUninstallDataFiles``), deletes ``unins000.dat``, tells the first
    phase to exit, waits for it, sleeps 500 ms, deletes ``unins000.exe``
    (retrying for up to ~3 s), removes the directories it could not remove
    before, and only then exits itself. So for a moment after this function's
    ``_run_installer`` returns, a process is still deleting files by *name*
    under ``install_dir``. A caller that reinstalls into the same directory
    straight away -- ``test_windows_uninstall_keeps_data_and_purge_deletes_it``
    does, as a real user would -- can have the new install's ``unins000.exe``
    deleted by the old uninstall's second phase, which is how the v4.5.0a1
    tag's first ``build-windows`` run failed.

    Fails, naming what is still there and which processes are still alive,
    if that has not happened within ``settle_timeout`` seconds, rather than
    carrying on into a step that would race it."""
    uninstaller = install_dir / "unins000.exe"
    assert uninstaller.is_file(), f"{uninstaller} missing -- was the install actually silent/complete?"
    result = _run_installer(
        str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", timeout=UNINSTALLER_TIMEOUT_S,
    )
    assert result.returncode == 0, f"uninstall failed (exit {result.returncode}):\n{result.stdout}{result.stderr}"
    deadline = time.monotonic() + settle_timeout
    while True:
        # Processes first: once none is left, nothing can delete anything
        # else, so leftovers seen after that are really left over.
        processes = _inno_processes()
        leftovers = _uninstall_leftovers(install_dir)
        if not processes and not leftovers:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    pytest.fail(
        f"{uninstaller} /VERYSILENT exited 0, but the uninstall had not finished {settle_timeout:.0f}s later.\n"
        f"---- still under {install_dir} ----\n"
        + ("\n".join(leftovers) or "(nothing)")
        + "\n---- Inno Setup processes still alive ----\n"
        + ("\n".join(str(p) for p in processes) or "(none)")
    )


def _local_app_data_privacyfence() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    return Path(local_app_data) / "PrivacyFence" if local_app_data else None


# --------------------------------------------------------------------------- #
# Test 1 -- install / validate / start+scenario / uninstall
# --------------------------------------------------------------------------- #

async def test_windows_install_validate_scenario_uninstall_lifecycle(tmp_path):
    setup_exe = _built_installers()[-1]
    install_dir = _admin_only_writable_dir(tmp_path / INSTALL_DIR_NAME)
    per_user = _local_app_data_privacyfence()
    per_user_preexisting = per_user is not None and per_user.exists()

    # ── Install ──────────────────────────────────────────────────────────
    # /DIR keeps the install under this test's own tmp_path; the directory is
    # pre-created administrators-only so that the install's own `enable` step
    # accepts it -- see _admin_only_writable_dir.
    _install(setup_exe, install_dir, tmp_path / "install.log")
    main_exe = install_dir / MAIN_EXE_NAME
    alias_exe = install_dir / ALIAS_EXE_NAME
    assert main_exe.is_file(), f"{main_exe} missing after silent install"
    assert alias_exe.is_file(), f"{alias_exe} missing after silent install"
    assert list(install_dir.glob("*.mcpb")), f"no .mcpb found in {install_dir} after install"

    # ── The installer registers nothing it later disables: the daemon is a
    # service, and the companion's is the only sign-in task ──────────────
    assert _task_exists(WINDOWS_COMPANION_TASK_NAME), (
        f"the {WINDOWS_COMPANION_TASK_NAME!r} task was not registered"
    )
    assert not _task_exists(REMOVED_DAEMON_TASK_NAME), (
        f"a {REMOVED_DAEMON_TASK_NAME!r} daemon sign-in task is registered -- the installer no "
        f"longer has one to register"
    )

    # ── The service the install created is actually serving ──────────────
    base_url, mcp_token = _wait_for_separated_service()
    await _assert_separated_service_serves_mcp(base_url, mcp_token)
    # Written by the daemon itself under the separated root, not by this test.
    assert SEPARATED_SETTINGS_PATH.is_file(), (
        f"{SEPARATED_SETTINGS_PATH} missing -- the service never wrote its own authority config"
    )

    # ── Uninstall (silent), with no `disable` or anything else first: the
    # uninstaller runs `privilege-separation.ps1 uninstall` itself ────────
    _silent_uninstall(install_dir)
    assert not main_exe.exists(), f"{main_exe} should be gone after silent uninstall"
    assert not alias_exe.exists(), f"{alias_exe} should be gone after silent uninstall"
    assert _service_config() is None, f"the {WINDOWS_SERVICE_NAME} service survived uninstall"
    assert not _task_exists(WINDOWS_COMPANION_TASK_NAME), (
        f"the {WINDOWS_COMPANION_TASK_NAME!r} task survived uninstall"
    )

    # ── ...and the data did survive it, where it was (ADR 0042: a silent
    # uninstall never purges), with nothing moved into the user's profile
    # (ADR 0041) ─────────────────────────────────────────────────────────
    assert SEPARATED_SETTINGS_PATH.is_file(), (
        f"{SEPARATED_SETTINGS_PATH} is gone -- a silent uninstall must keep PrivacyFence's data"
    )
    assert MARKER_PATH.is_file(), f"{MARKER_PATH} is gone -- uninstall keeps the marker for a reinstall"
    if per_user is not None and not per_user_preexisting:
        assert not per_user.exists(), f"uninstall created {per_user} -- nothing moves data into a profile"


# --------------------------------------------------------------------------- #
# Test 2 -- upgrade in place preserves user state (Phase 6 item 20)
# --------------------------------------------------------------------------- #

def _synthetic_next_version_installer(setup_exe: Path, output_dir: Path) -> tuple[Path, str]:
    """Builds a second, standalone installer from the *same* already-built
    ``dist/PrivacyFenceApp`` onedir output as ``setup_exe``, labeled with a
    version string guaranteed different from the original -- a second
    genuine ``iscc.exe`` invocation, not a repackaged copy of ``setup_exe``
    itself, since Inno Setup's own compiler is what actually needs to run
    twice to prove anything (unlike ``dpkg-deb --build``, there's no cheap
    way to relabel an already-compiled ``.exe`` after the fact). This is the
    same *inputs* as ``scripts/build_installer.ps1``'s own step 7, just
    invoked a second time with a bumped ``/DAppVersion`` and a scratch
    ``/DOutputDir`` -- deliberately not a real second PyInstaller build (see
    this module's own docstring, step 5, for why that would only add cost,
    not coverage, for what this test needs proven).

    Unlike ``test_deb_packaged_lifecycle.py``'s ``_synthetic_next_version_deb``,
    the version string here doesn't need to be *orderable* as "newer" --
    Inno Setup's own upgrade-detection keys off ``AppId`` (fixed in
    ``installer/privacyfence.iss``), not a version comparison, so any
    different ``AppVersion`` string is enough to prove this is a distinct
    reinstall rather than the identical bytes being re-applied.
    """
    iscc = shutil.which("iscc.exe") or shutil.which("iscc")
    assert iscc, "iscc.exe not on PATH -- Inno Setup 6 not installed (see scripts/build_installer.ps1's own prerequisites)"

    dist_onedir = REPO_ROOT / "dist" / "PrivacyFenceApp"
    assert dist_onedir.is_dir(), f"{dist_onedir} missing -- was scripts/build_installer.ps1 actually run?"
    mcpb_candidates = sorted(DIST_DIR.glob("PrivacyFence-*.mcpb"))
    assert mcpb_candidates, f"no PrivacyFence-*.mcpb found in {DIST_DIR} -- was scripts/build_installer.ps1 actually run?"
    mcpb_path = mcpb_candidates[-1]
    icon_path = REPO_ROOT / "build" / "privacyfence.ico"
    assert icon_path.is_file(), f"{icon_path} missing -- was scripts/build_installer.ps1 actually run?"

    version_match = re.match(r"^PrivacyFence-(.+)-setup\.exe$", setup_exe.name)
    assert version_match, f"unexpected installer filename shape: {setup_exe.name}"
    new_version = f"{version_match.group(1)}+upgradetest1"

    output_dir.mkdir(parents=True, exist_ok=True)
    setup_base_name = "PrivacyFence-upgradetest-setup"
    result = subprocess.run(
        [
            iscc,
            f"/DAppVersion={new_version}",
            f"/DDistDir={dist_onedir}",
            f"/DMcpbPath={mcpb_path}",
            f"/DIconPath={icon_path}",
            f"/DOutputDir={output_dir}",
            f"/DSetupBaseName={setup_base_name}",
            str(REPO_ROOT / "installer" / "privacyfence.iss"),
        ],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, f"iscc.exe (synthetic upgrade build) failed:\n{result.stdout}{result.stderr}"
    new_setup = output_dir / f"{setup_base_name}.exe"
    assert new_setup.is_file(), f"{new_setup} missing after iscc.exe"
    return new_setup, new_version


@pytest.mark.timeout(MULTI_INSTALL_TEST_TIMEOUT_S)   # builds a second installer *and* boots the
                                                     # daemon twice -- see that constant.
async def test_windows_upgrade_in_place_preserves_user_state(tmp_path):
    setup_exe_n = _built_installers()[-1]
    install_dir = _admin_only_writable_dir(tmp_path / INSTALL_DIR_NAME)

    # ── Install version N; let the service create its own on-disk state ──
    _install(setup_exe_n, install_dir, tmp_path / "install-n.log")
    alias_exe = install_dir / ALIAS_EXE_NAME

    # The state this preserves across the upgrade is state the *service*
    # wrote for itself under the separated root -- its MCP token and its own
    # authority config -- rather than a rule this test drove through an
    # approval round trip it cannot reach from here (see
    # _assert_separated_service_serves_mcp).
    base_url, mcp_token_before = _wait_for_separated_service()
    await _assert_separated_service_serves_mcp(base_url, mcp_token_before)
    assert SEPARATED_SETTINGS_PATH.is_file(), (
        f"{SEPARATED_SETTINGS_PATH} missing -- the service never wrote its own authority config"
    )
    settings_before = SEPARATED_SETTINGS_PATH.read_text(encoding="utf-8")

    # ── Build and silently install a synthetically-bumped version N+1 over
    # it, at the same install directory (installer/privacyfence.iss's fixed
    # AppId is what makes this an upgrade rather than a side-by-side
    # install) ─────────────────────────────────────────────────────────────
    setup_exe_n1, new_version = _synthetic_next_version_installer(setup_exe_n, tmp_path / "upgrade-build")
    upgrade_log_path = tmp_path / "install-n1.log"

    # No sweep of our own and no retry: the service and the companion that
    # version N started are still running here, exactly as on a real
    # upgrade, and stopping them is the installer's job (PrepareToInstall,
    # ADR 0045). This test used to stop and taskkill them itself and retry
    # Setup once past RestartManager's "Some applications could not be shut
    # down" (f3883ac6, 3079c985, df1a403d), which proved the harness could
    # clear the way rather than that the installer does.
    upgrade_result = _run_installer(
        str(setup_exe_n1),
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
        f"/DIR={install_dir}",
        f"/LOG={upgrade_log_path}",
        timeout=INSTALLER_TIMEOUT_S,
        log_path=upgrade_log_path,
    )
    _report(_install_timing_summary(upgrade_log_path))
    assert upgrade_result.returncode == 0, (
        f"upgrade install (version {new_version}) failed (exit {upgrade_result.returncode}):\n"
        f"{upgrade_result.stdout}{upgrade_result.stderr}\n"
        f"---- install log ----\n"
        f"{upgrade_log_path.read_text(errors='replace') if upgrade_log_path.exists() else '(missing)'}"
    )
    assert alias_exe.is_file(), f"{alias_exe} missing after upgrade install"
    upgrade_log = upgrade_log_path.read_text(errors="replace")
    assert "PrepareToInstall: no PrivacyFence process is still running" in upgrade_log, (
        "the upgrade succeeded, but PrepareToInstall did not confirm it had ended every "
        "PrivacyFence process before copying files -- RestartManager or luck did its job:\n"
        f"{upgrade_log}"
    )

    # ── The companion task is still registered -- `enable` re-registers it
    # (with /f) on every install, upgrades included ────────────────────────
    assert _task_exists(WINDOWS_COMPANION_TASK_NAME), (
        f"the {WINDOWS_COMPANION_TASK_NAME!r} task should still be registered after an upgrade install"
    )

    # ── The upgraded build starts and serves, and the state it inherited is
    # byte-for-byte what the previous one left. `enable` runs on an upgrade
    # too (installer/privacyfence.iss's CurStepChanged makes no distinction),
    # so this is the *upgraded* service answering. ───────────────────────────
    base_url_after, mcp_token_after = _wait_for_separated_service()
    await _assert_separated_service_serves_mcp(base_url_after, mcp_token_after)
    assert SEPARATED_SETTINGS_PATH.read_text(encoding="utf-8") == settings_before, (
        "the upgrade rewrote the authority config it should have inherited untouched"
    )
    assert mcp_token_after == mcp_token_before, (
        "the upgrade minted a new MCP token -- every configured client would stop working"
    )

    # ── Cleanup: silent uninstall, same as test 1 (the autouse fixture
    # removes the data it keeps) ──────────────────────────────────────────
    _silent_uninstall(install_dir)


# --------------------------------------------------------------------------- #
# Test 3 -- the install separates itself (ADR 0003 decision 4)
# --------------------------------------------------------------------------- #

def test_windows_install_separates_with_no_manual_enable(tmp_path):
    """A plain silent install ends up privilege-separated, and this module
    makes no ``enable`` call to get it there.

    The Windows half of the claim ``test_macos_pkg_install.py`` makes for the
    ``.pkg`` and ``test_deb_packaged_lifecycle.py`` for the ``.deb``. Until ADR
    0003 decision 4 there was nothing here to assert: Windows shipped
    ``privilege-separation.ps1`` and left running it to a human who had to find
    out it existed, which meant the one platform where the install flow said
    nothing about the subject was also the one where the product's central
    claim -- the agent cannot approve its own request -- was false by default.

    Everything below is a *consequence* of the installer's own `enable`, so
    each assertion names the step of it that would have to have gone wrong:
    the marker (Write-Marker), the service and its account (Install-Daemon\
    Service), the group membership (Add-OwnerToServiceGroup), the companion
    task (Install-CompanionTask).
    """
    setup_exe = _built_installers()[-1]
    install_dir = _admin_only_writable_dir(tmp_path / INSTALL_DIR_NAME)
    log_path = tmp_path / "install.log"

    _install(setup_exe, install_dir, log_path)

    try:
        # ── The marker: written by `enable`, and the one file every other
        # process in this product reads to decide which layout it is looking
        # at (privilege_separation.separation()). ────────────────────────────
        assert MARKER_PATH.is_file(), (
            f"{MARKER_PATH} missing after a silent install -- installer/privacyfence.iss's "
            f"CurStepChanged(ssPostInstall) did not separate the install:\n"
            f"{log_path.read_text(errors='replace') if log_path.exists() else '(missing log)'}"
        )
        marker = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
        assert marker["version"] == MARKER_VERSION
        assert marker["platform"] == "win32"
        assert marker["service_account"] == WINDOWS_SERVICE_ACCOUNT_NAME
        assert marker["service_group"] == WINDOWS_SERVICE_GROUP_NAME
        # Setup ran as this account, so `enable`'s optional owner resolution
        # found one -- the pending-membership case (ADR 0003 decision 3) is an
        # MDM/SYSTEM-context install, which a hosted runner cannot produce.
        assert marker["owner_user"].lower() == os.environ["USERNAME"].lower(), (
            f"the marker records owner_user={marker['owner_user']!r}, not the account that ran Setup"
        )

        # ── The service, against the image the installer just placed. A
        # binPath pointing anywhere else is the whole #407 failure mode. ─────
        config = _service_config()
        assert config is not None, f"the {WINDOWS_SERVICE_NAME} service does not exist after install"
        assert str(install_dir / ALIAS_EXE_NAME).lower() in config.lower(), (
            f"the {WINDOWS_SERVICE_NAME} service's binPath does not name the installed daemon:\n{config}"
        )
        assert "--windows-service" in config, f"binPath is missing the service-host flag:\n{config}"
        # And the image path is *quoted* inside binPath, which matters twice
        # over on a path with a space in it (INSTALL_DIR_NAME, deliberately).
        # The SCM runs ImagePath as a command line, so an unquoted
        # `C:\Program Files\...\privacyfence-app.exe --windows-service` sends
        # CreateProcess hunting for `C:\Program.exe` first -- the
        # unquoted-service-path hijack, in the one service whose whole purpose
        # is to keep the agent from running its own code as this account. It is
        # also the assertion that catches the quoting going back through
        # PowerShell's argument binder, which cannot carry it (see
        # scripts/windows_privilege_separation.ps1's Invoke-NativeCommandLine).
        assert f'"{install_dir / ALIAS_EXE_NAME}" --windows-service'.lower() in config.lower(), (
            f"the {WINDOWS_SERVICE_NAME} service's binPath does not quote the image path:\n{config}"
        )
        assert WINDOWS_SERVICE_ACCOUNT_NAME.lower() in config.lower(), (
            f"the {WINDOWS_SERVICE_NAME} service does not run as {WINDOWS_SERVICE_ACCOUNT_NAME}:\n{config}"
        )

        # ── The service's Event Log source (Register-EventSource). Without
        # it every entry the service writes -- including why it refused to
        # start -- reads as an empty message. ─────────────────────────────
        message_file = _event_message_file()
        assert message_file is not None, f"HKLM\\{EVENT_SOURCE_KEY} was not registered"
        assert message_file.lower().startswith(str(install_dir).lower()), (
            f"the Event Log source names a message file outside the install: {message_file}"
        )
        assert Path(message_file).is_file(), f"the registered message file does not exist: {message_file}"

        # ── The per-user half (ADR 0003 decision 3), closed at install time
        # because Setup had a real account to resolve. ──────────────────────
        members = [member.lower() for member in _local_group_members(WINDOWS_SERVICE_GROUP_NAME)]
        assert any(os.environ["USERNAME"].lower() in member for member in members), (
            f"{os.environ['USERNAME']} is not in {WINDOWS_SERVICE_GROUP_NAME}: {members}"
        )

        # ── The one scheduled task. The companion is the only thing left in
        # the user's session once the daemon is a service (ADR 0002 decision
        # 5); there is no daemon sign-in task to register or disable. ──────
        assert _task_exists(WINDOWS_COMPANION_TASK_NAME), (
            f"the {WINDOWS_COMPANION_TASK_NAME!r} task was not registered"
        )
        assert not _task_exists(REMOVED_DAEMON_TASK_NAME), (
            f"a {REMOVED_DAEMON_TASK_NAME!r} daemon sign-in task is registered"
        )
    finally:
        # _silent_uninstall, not a bare _run_installer: unins000.exe returning
        # is only the uninstall's first phase, and a second phase still running
        # when this test returns is a leak _clean_separation_state fails it for.
        if (install_dir / "unins000.exe").is_file():
            _silent_uninstall(install_dir)


# --------------------------------------------------------------------------- #
# Test 4 -- remove keeps data, reinstall picks it up, purge deletes it (ADR 0042)
# --------------------------------------------------------------------------- #

@pytest.mark.timeout(MULTI_INSTALL_TEST_TIMEOUT_S)   # two installs, two service cold starts and a purge
async def test_windows_uninstall_keeps_data_and_purge_deletes_it(tmp_path):
    """The Windows spelling of the ``.deb``'s ``apt remove``/``apt purge``.

    A silent uninstall stops and removes the service and the companion task
    and keeps ``%ProgramData%\\PrivacyFence`` -- data and marker -- and the
    ``PrivacyFenceUsers`` group. A reinstall's ``enable`` then serves that
    same data: same authority config, same MCP token, so every configured
    client keeps working. ``uninstall -Purge`` -- what the uninstaller's
    "Delete PrivacyFence data" checkbox adds -- deletes the directory and the
    group as well. The checkbox itself is interactive-only (a silent
    uninstall never purges), so the purge is driven through the installed
    script, the same command the uninstaller runs.
    """
    setup_exe = _built_installers()[-1]
    install_dir = _admin_only_writable_dir(tmp_path / INSTALL_DIR_NAME)

    # ── Install, and let the service write its own state ─────────────────
    _install(setup_exe, install_dir, tmp_path / "install-1.log")
    base_url, mcp_token_before = _wait_for_separated_service()
    await _assert_separated_service_serves_mcp(base_url, mcp_token_before)
    settings_before = SEPARATED_SETTINGS_PATH.read_text(encoding="utf-8")
    # The service has started, so it has written at least its start event,
    # and with the source registered that event reads as text.
    messages = _service_event_messages()
    assert any(WINDOWS_SERVICE_NAME in message for message in messages), (
        f"the {WINDOWS_SERVICE_NAME} service's Event Log entries render no text: {messages}"
    )

    # ── Remove: data, marker and group stay ──────────────────────────────
    _silent_uninstall(install_dir)
    assert _service_config() is None, f"the {WINDOWS_SERVICE_NAME} service survived uninstall"
    assert _event_message_file() is None, "the Event Log source outlived the program files it names"
    assert SEPARATED_SETTINGS_PATH.read_text(encoding="utf-8") == settings_before
    assert SEPARATED_MCP_TOKEN_PATH.read_text(encoding="utf-8").strip() == mcp_token_before
    assert MARKER_PATH.is_file(), f"{MARKER_PATH} is gone after a plain uninstall"
    assert _local_group_members(WINDOWS_SERVICE_GROUP_NAME), (
        f"{WINDOWS_SERVICE_GROUP_NAME} lost its members after a plain uninstall"
    )

    # ── Reinstall: the same data is served again ──────────────────────────
    _admin_only_writable_dir(install_dir)
    _install(setup_exe, install_dir, tmp_path / "install-2.log")
    base_url, mcp_token_after = _wait_for_separated_service()
    await _assert_separated_service_serves_mcp(base_url, mcp_token_after)
    assert mcp_token_after == mcp_token_before, (
        "the reinstall minted a new MCP token -- it did not pick up the data uninstall kept"
    )
    assert SEPARATED_SETTINGS_PATH.read_text(encoding="utf-8") == settings_before

    # ── Purge: everything goes ─────────────────────────────────────────────
    _run_separation_script(install_dir, "uninstall", "-Purge")
    assert _service_config() is None, f"the {WINDOWS_SERVICE_NAME} service survived `uninstall -Purge`"
    assert not _task_exists(WINDOWS_COMPANION_TASK_NAME)
    assert not WINDOWS_SYSTEM_ROOT.exists(), f"{WINDOWS_SYSTEM_ROOT} survived `uninstall -Purge`"
    group = subprocess.run(
        ["net", "localgroup", WINDOWS_SERVICE_GROUP_NAME], capture_output=True, text=True, timeout=30,
    )
    assert group.returncode != 0, f"{WINDOWS_SERVICE_GROUP_NAME} survived `uninstall -Purge`:\n{group.stdout}"

    # And the uninstaller after it finds nothing left to fail on.
    _silent_uninstall(install_dir)


# --------------------------------------------------------------------------- #
# Test 5 -- an install that cannot separate is not an install
# --------------------------------------------------------------------------- #

def test_windows_install_fails_when_the_image_is_user_writable(tmp_path):
    """Setup pointed at a directory the signed-in user can rewrite fails,
    loudly, instead of finishing successfully without mentioning the subject.

    The negative half of ADR 0003 decision 1. It matters because every default
    around it points the other way: Inno ignores a ``[Run]`` entry's exit code
    entirely, and Setup's exit code is already fixed by the time post-install
    runs -- so "finished successfully" is what a post-install problem looks
    like here unless something is done about it, and what it would have
    meant in this case is an install whose approval UI, passkey enrollment and
    audit log all say more than they can keep.

    An ordinary ``tmp_path`` directory is exactly the input that produces it:
    ``enable``'s ``Assert-ImageProtected`` refuses a user-writable image
    because a service runs whatever its ``binPath`` names. No ACL is applied
    here on purpose -- this is ``_admin_only_writable_dir`` not being called.
    """
    setup_exe = _built_installers()[-1]
    install_dir = tmp_path / f"user-writable {INSTALL_DIR_NAME}"
    install_dir.mkdir()
    log_path = tmp_path / "install.log"

    result = _run_installer(
        str(setup_exe),
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
        f"/DIR={install_dir}",
        f"/LOG={log_path}",
        timeout=INSTALLER_TIMEOUT_S,
        log_path=log_path,
    )
    install_log = log_path.read_text(errors="replace") if log_path.exists() else ""

    # Not a returncode assertion, deliberately: Inno Setup's Pascal Scripting
    # only lets Abort()/RaiseException change Setup's own exit code when
    # raised from InitializeSetup, InitializeWizard or
    # CurStepChanged(ssInstall) (see that function's own reference entry) --
    # SeparateInstall's RaiseException runs from CurStepChanged(ssPostInstall),
    # after Setup's exit-code table (codes 3/4/7/8) already considers "the
    # actual installation process" finished, so Setup reports 0 here no
    # matter how loudly [Code] refuses to register the service. See
    # privacyfence.iss's own CurStepChanged(ssPostInstall) comment. The
    # install log and the absence of any separation artifact are what
    # actually distinguish this from a real success, which is what the rest
    # of this test checks instead.
    assert "SeparateInstall" in install_log, (
        f"nothing in the install log says the separation step ran:\n{install_log}\n"
        f"---- setup.exe stdout/stderr (exit {result.returncode}) ----\n{result.stdout}{result.stderr}"
    )
    assert "refusing to enable" in install_log, (
        f"the install log does not carry `enable`'s own refusal:\n{install_log}"
    )
    assert not MARKER_PATH.exists(), f"{MARKER_PATH} was written by an install that failed"
    assert _service_config() is None, f"a failed install left the {WINDOWS_SERVICE_NAME} service behind"
