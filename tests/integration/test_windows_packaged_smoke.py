"""Packaged-artifact lifecycle test for the Windows installer.

The same role ``tests/integration/test_macos_packaged_smoke.py`` (TST-15)
plays for the DMG and ``tests/integration/test_deb_packaged_lifecycle.py``
(Phase 6.3) plays for the ``.deb``: install the actual built artifact -- not
a source checkout, not an editable dev install -- and exercise it as closely
as possible to how a real user would.

1. **Install**: a real silent run of ``scripts/build_installer.ps1``'s
   ``dist/PrivacyFence-<version>-setup.exe`` (Inno Setup 6) --
   ``/VERYSILENT /SUPPRESSMSGBOXES``, same as a user clicking through the
   wizard with every default accepted. ``/DIR=`` is overridden to a scratch
   directory under this test's own ``tmp_path`` rather than the real
   ``%ProgramFiles%`` purely for isolation from whatever else is on the
   runner, not to dodge elevation: ``installer/privacyfence.iss`` is
   ``PrivilegesRequired=admin`` (a non-elevated install can never register
   the Task Scheduler autostart task at all -- see
   ``docs/platform-support.md``'s "Known open items" -- so admin is no
   longer optional), and this test relies on the hosted runner's own account
   already carrying a full, unfiltered admin token (no interactive UAC
   prompt to get in this test's way) rather than on the installer not
   needing one.

   That scratch directory is created with an administrators-only ACL before
   Setup is pointed at it (``_admin_only_writable_dir``), which is not
   cosmetic: since ADR 0003 decision 4 the install separates itself, and
   ``enable``'s ``Assert-ImageProtected`` refuses an install directory the
   signed-in user can rewrite, because a service runs whatever its
   ``binPath`` names. An ordinary ``tmp_path`` directory *is* user-writable,
   so isolation and installability now have to be arranged together. Test 3
   below is the same fact asserted from the other side. The scratch directory
   also carries a space in its name (``INSTALL_DIR_NAME``), because a real
   install's ``C:\\Program Files\\PrivacyFence`` does and a runner's ``tmp_path``
   does not -- see that constant for the defect that hid behind the difference.
2. **Validate the autostart entry**: ``installer/privacyfence.iss``'s
   ``[Code]`` section registers the Task Scheduler task as part of the
   (silent) install itself, not a separate opt-in step -- ``schtasks /query`` against it is
   the one thing that would silently no-op at next logon if the install step
   ever stopped wiring it up.
3. **Start the real installed daemon** (``privacyfence-app.exe``, not
   ``PrivacyFenceApp.exe`` directly -- the same alias name both the Task
   Scheduler task and the mcpb shim's own ``DEFAULT_APP_PATH`` look for, see
   ``build_installer.ps1`` step 4) and run the Phase 3
   (``tests/system/test_local_mode_system.py``) daemon -> MCP -> approval ->
   audit contract's own shape against it: a real bootstrap session, an MCP
   tool call resolved through the real HTTP decide route both Allow and
   Deny, the audit log read back from disk, and a graceful shutdown via the
   real "Quit PrivacyFence" action.

   **One deliberate substitution** from Phase 3's own scenario, for the same
   reason ``test_macos_packaged_smoke.py``/``test_deb_packaged_lifecycle.py``
   already made it: Phase 3 injects a synthetic ``Connector`` by
   monkeypatching ``daemon_main.build_connectors`` *before* ``daemon_main``
   is ever imported -- only possible when the test controls the Python
   import itself, which a packaged, frozen daemon started as its own binary
   never does. This module instead drives
   ``privacyfence_propose_auto_accept_rule_change``, the one built-in
   meta-tool that always blocks on a confirmation dialog with no
   connector/credential of any kind behind it -- same tool, same reasoning.
   Like ``test_deb_packaged_lifecycle.py`` (and unlike the macOS module's
   real headless-Chromium click), this one resolves the pending card via a
   direct HTTP POST to ``/api/approvals/<id>/decide`` with the
   bootstrap-minted session cookie as CSRF -- no Node/Playwright dependency
   needed here, keeping this job's prerequisites to exactly what
   ``scripts/build_installer.ps1`` itself already needs.
4. **Uninstall**: a real silent run of the installer's own generated
   ``unins000.exe`` -- package-owned files (the whole install directory) and
   the Task Scheduler task (removed by the ``.iss``'s own
   ``[UninstallRun]``) are gone afterward; per-user state under the
   isolated ``%LOCALAPPDATA%\\PrivacyFence\\`` this test pointed the daemon
   at is untouched (``installer/privacyfence.iss``'s own ``[UninstallDelete]``
   comment: the installer never reaches into that directory at all).
5. **Upgrade in place** (this plan's Phase 6 item 20 -- deliberately not built
   in the same PR as items 1-4 above): install version N, use it to create
   real on-disk state (an applied auto-accept rule, via the same MCP round
   trip as step 3), install a synthetically-bumped version N+1 -- the
   identical PyInstaller ``dist/PrivacyFenceApp`` onedir output, re-packaged
   through a second, separate ``iscc.exe`` invocation with a bumped
   ``/DAppVersion`` (same technique ``test_deb_packaged_lifecycle.py``'s
   ``_synthetic_next_version_deb`` already uses for the ``.deb``: a second
   genuine PyInstaller build just for a "real" N+1 would multiply this
   module's already-heavy setup cost for no additional coverage of a claim
   that doesn't depend on what changed *inside* the package) -- over it, at
   the same install directory, and confirms the state survived and the
   upgraded binary still starts and serves. ``installer/privacyfence.iss``'s
   fixed ``AppId`` is what makes this a real in-place-upgrade install rather
   than a side-by-side one, the same way a second real release's installer
   would behave against a machine that already has PrivacyFence installed.

6. **The install separates itself** (ADR 0003 decision 4, ``test_windows_
   install_separates_with_no_manual_enable``): the ``.iss``'s own
   ``CurStepChanged(ssPostInstall)`` runs ``privilege-separation.ps1
   enable``, so a plain silent install ends up with the marker written, the
   ``PrivacyFence`` service created against the installed image and running
   as ``NT SERVICE\\PrivacyFence``, the installing account in
   ``PrivacyFenceUsers``, the companion task registered and the daemon
   autostart task disabled -- **with no separate ``enable`` call from this
   module**. The same claim ``test_macos_pkg_install.py`` makes for the
   ``.pkg``'s ``postinstall`` and ``test_deb_packaged_lifecycle.py`` for the
   ``.deb``'s ``postinst``.
7. **An install that cannot separate is not an install**
   (``test_windows_install_fails_when_the_image_is_user_writable``): Setup
   pointed at an ordinary, user-writable directory exits non-zero and says
   why, rather than reporting success for an install whose approval UI would
   mean less than it says. Inno ignores a ``[Run]`` entry's exit code and
   the autostart step next to it deliberately only warns, so "finished
   successfully" is the default outcome of a post-install problem here --
   which is exactly why this one is asserted.

**Scenarios 1-5 deliberately run the unseparated lifecycle, and undo the
install's own separation to get it.** ``_disable_installer_enabled_privilege_
separation()`` runs right after every ``_run_installer(setup...)`` call in
those tests, for the same reason ``test_deb_packaged_lifecycle.py``'s
identically-shaped ``_disable_auto_enabled_privilege_separation()`` has run
after every ``dpkg -i`` since #428 D1: once separation is on, the daemon is a
service running as its own account, and a second copy started directly out of
``tmp_path`` against an isolated ``%LOCALAPPDATA%`` is refused outright by
``privilege_separation.check_runtime_identity()``. That lifecycle -- an
alias exe started by hand, its own profile, its own port -- is what scenarios
1-5 are about, and it is still exactly what a ``disable``d install gets.
Scenarios 6 and 7 are where the separated-by-default path is asserted, and
they make no ``enable`` call of their own.

Skipped entirely unless running on real Windows with a just-built
``dist/PrivacyFence-*-setup.exe`` on disk -- this only makes sense as a step
in ``.github/workflows/build.yml``'s ``build-windows`` job, right after
``scripts/build_installer.ps1``, never as part of the ordinary ``pytest``
invocation in ``tests.yml``'s per-PR jobs (same posture as the macOS/Linux
packaged tests). Step 5's upgrade test additionally needs ``iscc.exe`` on
``PATH`` and ``dist/PrivacyFenceApp``/``build/privacyfence.ico`` on disk --
both already there right after ``scripts/build_installer.ps1``'s own steps
3/1, the same prerequisites the first ``iscc.exe`` invocation (step 7) used
to build ``setup_exe`` in the first place.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import httpx2
import pytest
import yaml

mcp_client = pytest.importorskip(
    "mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'"
)
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from privacyfence.privilege_separation import (  # noqa: E402
    MARKER_FILE_NAME,
    MARKER_VERSION,
    WINDOWS_COMPANION_TASK_NAME,
    WINDOWS_DAEMON_TASK_NAME,
    WINDOWS_SERVICE_ACCOUNT_NAME,
    WINDOWS_SERVICE_GROUP_NAME,
    WINDOWS_SERVICE_NAME,
    WINDOWS_SYSTEM_ROOT,
)
from tests.control_channel_client import mint_bootstrap_code_windows, resolve_windows_pipe_name  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "dist"
SETTINGS_EXAMPLE = REPO_ROOT / "src" / "privacyfence" / "resources" / "settings.yaml.example"

TASK_NAME = "PrivacyFence"  # installer/privacyfence.iss's #define TaskName
MAIN_EXE_NAME = "PrivacyFenceApp.exe"
ALIAS_EXE_NAME = "privacyfence-app.exe"  # what the Task Scheduler task/mcpb shim both look for
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

# The separated layout's own files, as `enable` leaves them on disk. Readable
# directly rather than through an elevation shim, unlike the POSIX modules'
# `sudo` reads: Set-Layout grants Administrators full control of the whole
# tree, and every test in this module already runs elevated (the installer
# needs it -- see this module's own docstring).
SEPARATED_HANDOFF_DIR = WINDOWS_SYSTEM_ROOT / "handoff"
SEPARATED_MCP_TOKEN_PATH = SEPARATED_HANDOFF_DIR / MCP_TOKEN_FILE_NAME
SEPARATED_WEB_BASE_URL_PATH = SEPARATED_HANDOFF_DIR / "web_base_url"
SEPARATED_SETTINGS_PATH = WINDOWS_SYSTEM_ROOT / "authority" / "config" / "settings.yaml"


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
    # test in this repo.
    pytest.mark.timeout(180),
]


# --------------------------------------------------------------------------- #
# Task Scheduler helpers
# --------------------------------------------------------------------------- #

def _task_exists(name: str = TASK_NAME) -> bool:
    result = subprocess.run(["schtasks", "/query", "/tn", name], capture_output=True, text=True, timeout=15)
    return result.returncode == 0


def _delete_task_if_present(name: str = TASK_NAME) -> None:
    if _task_exists(name):
        subprocess.run(["schtasks", "/delete", "/tn", name, "/f"], capture_output=True, text=True, timeout=15)


def _stop_daemon_service(*, timeout: float = 60.0) -> None:
    """``sc stop`` the real service and wait for it to actually reach STOPPED.

    Not ``taskkill``, and the difference is the whole point:
    Install-DaemonService configures failure actions (``sc failure ... actions=
    restart/5000/restart/10000/restart/30000``), so a service process that dies
    *unexpectedly* is restarted by the SCM within five seconds. Killing it by
    image name therefore buys about five seconds -- which is exactly what Inno
    Setup's own four one-second DeleteFile retries were losing to:

        _internal\\PIL\\_imaging.cp312-win_amd64.pyd
        DeleteFile: The existing file appears to be in use (5). Retrying.
        ... DeleteFile failed; code 5. Access is denied.

    A clean stop is not an unexpected termination, so the SCM leaves it
    stopped, and every ``_internal`` DLL the daemon had mapped is released for
    good. A no-op when the service does not exist (``sc query`` exits
    non-zero), which is the unseparated case and every test that never
    installed."""
    if subprocess.run(
        ["sc.exe", "query", WINDOWS_SERVICE_NAME], capture_output=True, text=True, timeout=30,
    ).returncode != 0:
        return
    subprocess.run(["sc.exe", "stop", WINDOWS_SERVICE_NAME], capture_output=True, text=True, timeout=30)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["sc.exe", "query", WINDOWS_SERVICE_NAME], capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 or "STOPPED" in result.stdout:
            return
        time.sleep(0.5)


def _kill_stray_app_processes() -> None:
    """Best-effort ``taskkill`` sweep for any process still running against
    ``MAIN_EXE_NAME``/``ALIAS_EXE_NAME``, by image name rather than PID.

    v4.1.0a9's release build failed here: the upgrade-install step's Inno
    Setup run exited 5 ("Some applications could not be shut down") because
    RestartManager still found a running ``privacyfence-app`` at the moment
    it tried to close applications ahead of overwriting files -- even though
    this test's own daemon had already been confirmed exited beforehand.
    Whatever is actually holding the handle at that point (the OS's own
    deferred teardown of the just-exited process's image sections, or a
    second process this test never tracked), taskkill-by-image-name clears it
    either way; killing an already-gone process is simply a no-op (taskkill
    exits non-zero, which is why this ignores the result).

    ``COMPANION_EXE_NAME`` is in the sweep because a *separated* install has
    one running: ``Install-CompanionTask`` registers and starts it, and it is
    what RestartManager now names ("an application using one of our files:
    PrivacyFenceCompanion"). It could not appear here before, because until
    the Windows ``enable`` was fixed no install ever got far enough to start
    a companion at all.

    The daemon is stopped rather than killed, and before the sweep: on a
    separated install it is a *service*, and the SCM restarts a killed one
    within five seconds. See _stop_daemon_service()."""
    _stop_daemon_service()
    for image_name in (ALIAS_EXE_NAME, MAIN_EXE_NAME, COMPANION_EXE_NAME):
        subprocess.run(["taskkill", "/F", "/IM", image_name], capture_output=True, text=True, timeout=15)


@pytest.fixture(autouse=True)
def _clean_task_state():
    """Every test in this module installs/uninstalls the real Task Scheduler
    task -- machine-wide-per-user state, not something ``tmp_path`` isolates.
    Guarantee a clean slate on both sides so a failure partway through never
    leaves the runner with a stray task registered."""
    _delete_task_if_present()
    yield
    _delete_task_if_present()


# --------------------------------------------------------------------------- #
# Privilege separation -- what the install now does to itself, and how the
# scenarios that predate it get back to the lifecycle they were written for.
# ADR 0003 decision 4; see this module's own docstring.
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


def _disable_installer_enabled_privilege_separation(install_dir: Path, *, require_separated: bool = True) -> None:
    """Undoes ADR 0003 decision 4's installer-run ``enable``, which fires on
    every silent install *and* every upgrade over one.

    Exactly the role ``test_deb_packaged_lifecycle.py``'s own
    ``_disable_auto_enabled_privilege_separation()`` plays after each
    ``dpkg -i``, for the same reason: this module's scenarios are the
    unseparated lifecycle -- an alias exe started directly against an isolated
    ``%LOCALAPPDATA%`` -- and a separated install refuses that outright
    (``privilege_separation.check_runtime_identity()``), quite apart from the
    real service the installer just started competing for the same data
    directory. ``disable`` also re-enables the daemon autostart task that
    ``enable`` disabled, which is what leaves step 2's ``schtasks /query``
    assertion meaning what it always meant.

    Must be re-run after every install in those tests, the upgrade-in-place one
    included: ``enable`` runs on an upgrade too, not just a first install.

    ``require_separated=False`` is for a caller that cannot assume the install
    it just made got separated at all -- privacyfence/privacyfence#561: a
    ``/DIR=``-overridden install has, at least once, come out of
    ``CurStepChanged(ssPostInstall)``'s own ``enable`` call with no
    ``MARKER_PATH`` to show for it despite Setup itself reporting success, and
    ``disable`` refusing an install it did not separate is correct -- the bug
    that filed #561 was this fixture calling `disable` unconditionally and
    taking the whole module down on that refusal, not the refusal itself. With
    this flag, a missing marker is treated the same as an install this
    function has nothing to undo, rather than a failure.
    """
    if not require_separated and not MARKER_PATH.exists():
        return
    _run_separation_script(install_dir, "disable")
    assert not MARKER_PATH.exists(), f"{MARKER_PATH} survived `disable`"


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
    assert "privacyfence_propose_auto_accept_rule_change" in names, names


def _service_config(name: str = WINDOWS_SERVICE_NAME) -> str | None:
    """``sc.exe qc`` output for a service, or None if it does not exist."""
    result = subprocess.run(["sc.exe", "qc", name], capture_output=True, text=True, timeout=30)
    return result.stdout if result.returncode == 0 else None


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


def _tear_down_separation() -> None:
    """Removes whatever an installer-run ``enable`` left behind, without going
    through the script.

    ``disable`` is the supported route and every test that gets that far uses
    it; this is the floor under it, for a run that died between ``enable`` and
    its own teardown, or one where the install directory (and with it the
    script) is already gone. Machine-wide state -- a service, a scheduled task
    and a directory under ``%ProgramData%`` -- is not something ``tmp_path``
    isolates, so leaving any of it behind would poison whatever runs next on
    this runner.

    ``takeown`` before the delete because a separated root is owned by
    Administrators and its ``authority\\`` subtree grants the service account
    and nothing else; ``shutil.rmtree`` on its own would stop at the first
    directory it cannot open. The ``PrivacyFenceUsers`` group is deliberately
    left alone -- ``disable`` leaves it too (it owns nothing once the ACLs are
    gone), and re-adding a member is idempotent.
    """
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
    # above: what `enable` started, the floor under `disable` has to end.
    subprocess.run(
        ["taskkill", "/f", "/im", COMPANION_EXE_NAME], capture_output=True, text=True, timeout=30,
    )
    if WINDOWS_SYSTEM_ROOT.exists():
        subprocess.run(
            ["takeown", "/f", str(WINDOWS_SYSTEM_ROOT), "/r", "/d", "Y"],
            capture_output=True, text=True, timeout=120,
        )
        _icacls(str(WINDOWS_SYSTEM_ROOT), "/grant", f"{SID_ADMINISTRATORS}:(OI)(CI)(F)", "/t", "/c", "/q",
                check=False)
        shutil.rmtree(WINDOWS_SYSTEM_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_separation_state():
    """Guaranteed on both sides, same reasoning as ``_clean_task_state`` above.

    Also removes the real ``%LOCALAPPDATA%\\PrivacyFence`` this runner's own
    account ends up with -- but only if it did not already exist: ``disable``
    moves the separated root *back* there, so a module whose every install
    separates itself now creates that directory as a side effect even though
    every daemon it starts runs against an isolated profile instead. Anything
    that was there before this test is somebody else's.
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    legacy = Path(local_app_data) / "PrivacyFence" if local_app_data else None
    preexisting = legacy is not None and legacy.exists()
    _tear_down_separation()
    yield
    _tear_down_separation()
    if legacy is not None and not preexisting:
        shutil.rmtree(legacy, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Daemon process lifecycle -- same isolated-per-user-profile technique the
# macOS/Linux packaged tests use for $HOME, adapted to Windows: paths.py's
# data_dir() resolves under %LOCALAPPDATA% there (see its own
# windows_data_dir() docstring for why not the same ~/.privacyfence dotfile
# POSIX uses, reused under %USERPROFILE%), so isolating a daemon run means
# pointing LOCALAPPDATA at a scratch directory -- USERPROFILE/HOME are set
# alongside it defensively (see _running_daemon's own comment).
# --------------------------------------------------------------------------- #

def _data_dir(home: Path) -> Path:
    """Mirrors paths.py's ``windows_data_dir()`` for an isolated ``home``
    this module controls: ``<home>/AppData/Local/PrivacyFence``, the same
    shape ``_running_daemon`` points ``LOCALAPPDATA`` at below."""
    return home / "AppData" / "Local" / "PrivacyFence"

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


def _wait_for_file(path: Path, proc: subprocess.Popen, log_path: Path, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"daemon exited early (code {proc.poll()}) instead of starting -- log:\n"
                f"{log_path.read_text(errors='replace')}"
            )
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
        time.sleep(0.1)
    raise AssertionError(f"{path} never appeared within {timeout}s -- log:\n{log_path.read_text(errors='replace')}")


class RunningDaemon:
    def __init__(self, process: subprocess.Popen, home: Path, port: int, mcp_token: str):
        self.process = process
        self.home = home
        self.port = port
        self.base_url = f"http://localhost:{port}"
        self.mcp_url = f"{self.base_url}/mcp"
        self.mcp_token = mcp_token


def _prepare_home(home: Path, *, port: int) -> None:
    """Pre-seeds (or re-seeds only the harness-convenience bits of) an
    isolated per-user-profile's ``settings.yaml``: a real free port (so
    repeated boots in this module never collide with each other or
    anything else on the runner) and update checks disabled (this tier
    makes no real outbound network calls). If ``settings.yaml`` already
    exists -- a second boot against a profile a previous boot in this same
    test already used -- its existing content (e.g. an auto-accept rule the
    app itself applied) is loaded and only those two fields are
    overwritten, never replaced wholesale: overwriting it every boot would
    silently defeat the state-survival assertions
    ``test_windows_upgrade_in_place_preserves_user_state`` exists to make
    (same reasoning, same fix, as ``test_deb_packaged_lifecycle.py``'s
    identically-named helper)."""
    # #428 Phase 1: settings.yaml lives under an authority/ subdirectory of
    # data_dir(), not data_dir() itself.
    config_dir = _data_dir(home) / "authority" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    settings_path = config_dir / "settings.yaml"
    if settings_path.exists():
        settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    else:
        settings = yaml.safe_load(SETTINGS_EXAMPLE.read_text(encoding="utf-8")) or {}
    settings.setdefault("web", {})["port"] = port
    settings.setdefault("update_check", {})["enabled"] = False
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")


def _running_daemon(exe: Path, home: Path):
    """Starts the real installed ``privacyfence-app.exe`` alias (not
    ``PrivacyFenceApp.exe`` directly -- proving the alias itself resolves
    and execs correctly is part of what this module is for) with an
    isolated ``%LOCALAPPDATA%``, returning a context manager that always
    terminates it on the way out."""
    assert exe.is_file(), f"{exe} missing -- was the installer actually run?"
    port = _free_port()
    home.mkdir(parents=True, exist_ok=True)
    _prepare_home(home, port=port)
    # paths.py's data_dir() resolves under LOCALAPPDATA on Windows (its
    # windows_data_dir() branch), so that's the one variable that actually
    # isolates this run -- not USERPROFILE/HOME, which don't drive it
    # anymore. Both are still set alongside it defensively (some
    # third-party code, this daemon's own dependencies included, still
    # checks HOME first) and cost nothing to set.
    env = {
        **os.environ,
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "USERPROFILE": str(home), "HOME": str(home),
    }
    log_path = home / "daemon.log"

    @contextlib.contextmanager
    def _cm():
        with open(log_path, "wb") as log_fh:
            proc = subprocess.Popen([str(exe)], env=env, stdout=log_fh, stderr=subprocess.STDOUT)
            try:
                _wait_until_connectable("localhost", port)
                data_dir = _data_dir(home)
                mcp_token = _wait_for_file(data_dir / MCP_TOKEN_FILE_NAME, proc, log_path)
                yield RunningDaemon(proc, home, port, mcp_token)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)

    return _cm()


# --------------------------------------------------------------------------- #
# The daemon -> MCP -> approval -> audit round trip -- Phase 3's own shape,
# identical to test_deb_packaged_lifecycle.py's (see that module's docstring
# point 3 for why this substitution needs no connector/credential).
# --------------------------------------------------------------------------- #

async def _bootstrap_session(web_client: httpx.AsyncClient, data_dir: Path, *, path: str = "/settings") -> str:
    # #428 Phase 2: minted through the control channel (a real named pipe
    # against this daemon's own data directory, ACL'd to the current user),
    # not a bearer-authenticated HTTP route -- see tests.control_channel_
    # client's own module docstring.
    code = mint_bootstrap_code_windows(resolve_windows_pipe_name(data_dir))
    exchange_resp = await web_client.get(path, params={"bootstrap": code})
    assert exchange_resp.status_code == 200, exchange_resp.text
    session_id = web_client.cookies.get("pf_session")
    assert session_id, "bootstrap exchange did not set a pf_session cookie"
    return session_id


async def _propose_trusted_sender_rule(mcp_url: str, mcp_token: str, *, value: list[str]):
    headers = {"Authorization": f"Bearer {mcp_token}"}
    async with httpx2.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(mcp_url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(
                    "privacyfence_propose_auto_accept_rule_change",
                    {
                        "target": "rule",
                        "operation": "add",
                        "operation_key": "gmail.read_message",
                        "rule_name": "trusted_sender_domain",
                        "value": value,
                        "reason": "tests/integration/test_windows_packaged_smoke.py packaged installer lifecycle scenario",
                    },
                )


async def _resolve_pending_card(web_client: httpx.AsyncClient, session_id: str, *, decision: str) -> None:
    deadline = time.monotonic() + 20.0
    approval_id = None
    while time.monotonic() < deadline:
        page = await web_client.get("/approvals")
        assert page.status_code == 200, page.text
        # Matches both the plain and the binder's "unbatchable" modifier class
        # (approval_list_html.py's _row_html: a confirm-kind card, like the
        # rule-confirmation one this scenario drives, is never batchable) --
        # see approval_list_html.py's own row_class comment.
        match = re.search(
            r'<div class="pf-approval-row(?: pf-approval-row-unbatchable)?" data-approval-id="([0-9a-f]{16,})"',
            page.text,
        )
        if match:
            approval_id = match.group(1)
            break
        await asyncio.sleep(0.1)
    assert approval_id, "no pending approval card appeared on /approvals"
    decide_resp = await web_client.post(
        f"/api/approvals/{approval_id}/decide", json={"result": decision, "csrf": session_id},
    )
    assert decide_resp.status_code == 200, decide_resp.text
    assert decide_resp.json() == {"status": "ok"}


async def _quit(web_client: httpx.AsyncClient, session_id: str) -> None:
    resp = await web_client.post("/api/settings/quit_app", json={"csrf": session_id, "confirmed": True})
    assert resp.status_code == 200, resp.text


async def _run_daemon_mcp_approval_audit_scenario(daemon: RunningDaemon) -> None:
    async with httpx.AsyncClient(base_url=daemon.base_url, follow_redirects=True) as web_client:
        assert (await web_client.get("/approvals")).status_code == 401
        assert (await web_client.get("/settings")).status_code == 401

        session_id = await _bootstrap_session(web_client, _data_dir(daemon.home))
        assert (await web_client.get("/settings")).status_code == 200

        # -- MCP discovery: the real MCP surface, no connector configured ----
        headers = {"Authorization": f"Bearer {daemon.mcp_token}"}
        async with httpx2.AsyncClient(headers=headers) as http_client:
            async with streamable_http_client(daemon.mcp_url, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
        assert "privacyfence_propose_auto_accept_rule_change" in names
        assert "privacyfence_check_policy" in names

        # -- Allow round trip -------------------------------------------------
        allow_task = asyncio.create_task(
            _propose_trusted_sender_rule(daemon.mcp_url, daemon.mcp_token, value=["allowed.example.com"])
        )
        await _resolve_pending_card(web_client, session_id, decision="confirm")
        allow_result = await allow_task
        assert allow_result.is_error is not True, getattr(allow_result, "content", allow_result)
        assert allow_result.structured_content["confirmed"] is True
        assert allow_result.structured_content["changed"] is True
        # P9 of the policy v2 redesign: the confirmed-response description is the v2 rule's own
        # human-readable sentence now, not an echo of the v1 rule_name string.
        assert "Gmail - sender domain allowed.example.com: allow read" in allow_result.structured_content["description"]

        # -- Deny round trip ----------------------------------------------------
        deny_task = asyncio.create_task(
            _propose_trusted_sender_rule(daemon.mcp_url, daemon.mcp_token, value=["denied.example.com"])
        )
        await _resolve_pending_card(web_client, session_id, decision="cancel")
        deny_result = await deny_task
        assert deny_result.is_error is True

        # -- Audit log confirms both real decisions ------------------------------
        audit_dir = _data_dir(daemon.home) / "authority" / "logs" / "audit"
        decisions = []
        for jsonl_path in sorted(audit_dir.glob("*.jsonl")):
            for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("connector") == "rule":
                    decisions.append(entry.get("decision"))
        assert "rule_changed_via_bridge_proposal" in decisions
        assert "rejected" in decisions

        # -- Graceful shutdown via the real "Quit PrivacyFence" action -----------
        await _quit(web_client, session_id)

    exit_code = daemon.process.wait(timeout=15)
    assert exit_code == 0, (
        f"daemon did not exit cleanly (code {exit_code}) -- log:\n"
        f"{(daemon.home / 'daemon.log').read_text(errors='replace')}"
    )


# --------------------------------------------------------------------------- #
# Test 1 -- install / validate / start+scenario / uninstall
# --------------------------------------------------------------------------- #

def _run_installer(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    result = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    return result


async def test_windows_install_validate_scenario_uninstall_lifecycle(tmp_path):
    setup_exe = _built_installers()[-1]
    install_dir = _admin_only_writable_dir(tmp_path / INSTALL_DIR_NAME)
    log_path = tmp_path / "install.log"

    # ── Install ──────────────────────────────────────────────────────────
    # /DIR overrides installer/privacyfence.iss's DefaultDirName so this
    # test's install stays under its own tmp_path instead of the real
    # %ProgramFiles% -- isolation from whatever else is on the runner, not
    # an elevation dodge: PrivilegesRequired=admin means Setup needs an
    # elevated token regardless of which directory it's writing to, and
    # this module's own docstring explains why this test still runs
    # unattended (the hosted runner's account already has one). The
    # directory is pre-created administrators-only so that the install's own
    # `enable` step accepts it -- see _admin_only_writable_dir.
    result = _run_installer(
        str(setup_exe),
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
        f"/DIR={install_dir}",
        f"/LOG={log_path}",
    )
    assert result.returncode == 0, (
        f"installer failed (exit {result.returncode}):\n{result.stdout}{result.stderr}\n"
        f"---- install log ----\n{log_path.read_text(errors='replace') if log_path.exists() else '(missing)'}"
    )

    # No `disable` here any more. This scenario used to undo the install's own
    # separation and drive an alias exe against an isolated %LOCALAPPDATA%,
    # which ADR 0003 decision 6 has since made impossible: a packaged daemon
    # that finds itself unseparated auto-enables separation and, failing that,
    # refuses to serve -- deliberately with no developer override (see
    # privilege_separation.dev_allows_unseparated()'s own docstring). While
    # `enable` was broken on Windows that auto-enable always failed and this
    # scenario kept working by accident; once it started succeeding, it
    # re-separated the machine mid-test and the spawned daemon collided with
    # the real service over the control channel's named pipe. So this now runs
    # the same lifecycle the .deb and .pkg modules do: against the real service
    # the installer itself started.
    main_exe = install_dir / MAIN_EXE_NAME
    alias_exe = install_dir / ALIAS_EXE_NAME
    assert main_exe.is_file(), f"{main_exe} missing after silent install"
    assert alias_exe.is_file(), f"{alias_exe} missing after silent install"
    assert list(install_dir.glob("*.mcpb")), f"no .mcpb found in {install_dir} after install"

    # ── Validate the autostart entry (installer/privacyfence.iss's [Run]) ──
    assert _task_exists(), f"Task Scheduler task {TASK_NAME!r} missing after install"

    # ── The service the install created is actually serving ──────────────
    base_url, mcp_token = _wait_for_separated_service()
    await _assert_separated_service_serves_mcp(base_url, mcp_token)
    # Written by the daemon itself under the separated root, not by this test
    # -- the config it migrates to the policy v2 on-disk format at first boot.
    assert SEPARATED_SETTINGS_PATH.is_file(), (
        f"{SEPARATED_SETTINGS_PATH} missing -- the service never wrote its own authority config"
    )

    # ── `disable` before uninstalling, the documented order (and what
    # test_windows_install_separates_with_no_manual_enable asserts in full) ──
    _disable_installer_enabled_privilege_separation(install_dir)

    # ── Uninstall (silent) ─────────────────────────────────────────────────
    uninstaller = install_dir / "unins000.exe"
    assert uninstaller.is_file(), f"{uninstaller} missing -- was the install actually silent/complete?"
    uninstall_result = _run_installer(str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
    assert uninstall_result.returncode == 0, (
        f"uninstall failed (exit {uninstall_result.returncode}):\n"
        f"{uninstall_result.stdout}{uninstall_result.stderr}"
    )

    # Inno's uninstaller spawns a short-lived helper process to delete its
    # own directory/log after the foreground process it just waited on
    # exits -- poll rather than assume the directory is already gone the
    # instant the process above returns.
    deadline = time.monotonic() + 15.0
    while install_dir.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not main_exe.exists(), f"{main_exe} should be gone after silent uninstall"
    assert not alias_exe.exists(), f"{alias_exe} should be gone after silent uninstall"

    # ── The scheduled task is removed too (installer/privacyfence.iss's
    # [UninstallRun]) ──────────────────────────────────────────────────────
    assert not _task_exists(), f"Task Scheduler task {TASK_NAME!r} should be gone after uninstall"

    # ── User state is untouched (installer/privacyfence.iss's own
    # [UninstallDelete] comment: the installer never reaches into the user's
    # own PrivacyFence directory). `disable` above moved the separated root
    # back under the owner's real %LOCALAPPDATA%, which is where the data this
    # install accumulated now lives -- and where the uninstaller must have
    # left it. ──────────────────────────────────────────────────────────────
    restored_settings = (
        Path(os.environ["LOCALAPPDATA"]) / "PrivacyFence" / "authority" / "config" / "settings.yaml"
    )
    assert restored_settings.is_file(), (
        f"{restored_settings} is gone -- uninstall must never delete the user's own data, and "
        "`disable` is what put it back here"
    )


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


@pytest.mark.timeout(300)   # builds a second installer *and* boots the daemon twice -- the module's
                             # default timeout=180 (sized for test 1's single install/boot/uninstall)
                             # isn't enough headroom for both in one test.
async def test_windows_upgrade_in_place_preserves_user_state(tmp_path):
    setup_exe_n = _built_installers()[-1]
    install_dir = _admin_only_writable_dir(tmp_path / INSTALL_DIR_NAME)

    # ── Install version N; create real on-disk state the app itself
    # applied (an auto-accept rule confirmed through the real MCP/approval
    # round trip -- not a hand-written settings.yaml) ────────────────────
    install_result = _run_installer(
        str(setup_exe_n),
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
        f"/DIR={install_dir}",
        f"/LOG={tmp_path / 'install-n.log'}",
    )
    assert install_result.returncode == 0, (
        f"installer failed (exit {install_result.returncode}):\n{install_result.stdout}{install_result.stderr}"
    )
    alias_exe = install_dir / ALIAS_EXE_NAME

    # Against the real service, for the reason test 1 above spells out: ADR
    # 0003 decision 6 leaves no unseparated lifecycle for a packaged daemon to
    # run. The state this preserves across the upgrade is therefore state the
    # *service* wrote for itself under the separated root -- its MCP token and
    # its own migrated authority config -- rather than a rule this test drove
    # through an approval round trip it can no longer reach from here.
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

    # v4.1.0a9's release build failed exactly here: Setup exited 5 because
    # RestartManager found a still-running "privacyfence-app" and, under
    # /SUPPRESSMSGBOXES, defaulted the resulting Abort/Retry/Ignore prompt to
    # Abort rather than actually retrying -- see _kill_stray_app_processes's
    # own comment. Sweep for one before the attempt, and again before a
    # single retry if Setup still reports that exact failure, rather than
    # failing the whole release on what a real interactive install would
    # have shrugged off with one manual Retry click.
    _kill_stray_app_processes()
    for attempt in (1, 2):
        upgrade_result = _run_installer(
            str(setup_exe_n1),
            "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
            f"/DIR={install_dir}",
            f"/LOG={upgrade_log_path}",
        )
        if upgrade_result.returncode == 0:
            break
        log_text = upgrade_log_path.read_text(errors="replace") if upgrade_log_path.exists() else ""
        if attempt == 2 or upgrade_result.returncode != 5 or "could not be shut down" not in log_text:
            break
        _kill_stray_app_processes()
        time.sleep(2)

    assert upgrade_result.returncode == 0, (
        f"upgrade install (version {new_version}) failed (exit {upgrade_result.returncode}):\n"
        f"{upgrade_result.stdout}{upgrade_result.stderr}\n"
        f"---- install log ----\n"
        f"{upgrade_log_path.read_text(errors='replace') if upgrade_log_path.exists() else '(missing)'}"
    )
    assert alias_exe.is_file(), f"{alias_exe} missing after upgrade install"

    # ── The autostart task is still registered -- installer/privacyfence.iss's
    # [Run] section re-registers it (with /f) on every install, upgrades
    # included, not just a first install ──────────────────────────────────
    assert _task_exists(), f"Task Scheduler task {TASK_NAME!r} should still be registered after an upgrade install"

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

    # ── Cleanup: `disable` then silent uninstall, same order as test 1 ────
    _disable_installer_enabled_privilege_separation(install_dir)
    uninstaller = install_dir / "unins000.exe"
    assert uninstaller.is_file(), f"{uninstaller} missing -- was the upgrade install actually silent/complete?"
    uninstall_result = _run_installer(str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
    assert uninstall_result.returncode == 0, (
        f"uninstall failed (exit {uninstall_result.returncode}):\n{uninstall_result.stdout}{uninstall_result.stderr}"
    )


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
    task (Install-CompanionTask) and the daemon task's state
    (Disable-DaemonTask).
    """
    setup_exe = _built_installers()[-1]
    install_dir = _admin_only_writable_dir(tmp_path / INSTALL_DIR_NAME)
    log_path = tmp_path / "install.log"

    result = _run_installer(
        str(setup_exe),
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
        f"/DIR={install_dir}",
        f"/LOG={log_path}",
    )
    assert result.returncode == 0, (
        f"installer failed (exit {result.returncode}):\n{result.stdout}{result.stderr}\n"
        f"---- install log ----\n{log_path.read_text(errors='replace') if log_path.exists() else '(missing)'}"
    )

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

        # ── The per-user half (ADR 0003 decision 3), closed at install time
        # because Setup had a real account to resolve. ──────────────────────
        members = [member.lower() for member in _local_group_members(WINDOWS_SERVICE_GROUP_NAME)]
        assert any(os.environ["USERNAME"].lower() in member for member in members), (
            f"{os.environ['USERNAME']} is not in {WINDOWS_SERVICE_GROUP_NAME}: {members}"
        )

        # ── The two scheduled tasks. The companion is the only thing left in
        # the user's session once the daemon is a service (ADR 0002 decision
        # 5), and the daemon's own autostart task must be *disabled* rather
        # than left to start a second, refusing daemon at every sign-in. ────
        assert _task_exists(WINDOWS_COMPANION_TASK_NAME), (
            f"the {WINDOWS_COMPANION_TASK_NAME!r} task was not registered"
        )
        assert _task_state(WINDOWS_DAEMON_TASK_NAME) == "Disabled", (
            f"the {WINDOWS_DAEMON_TASK_NAME!r} autostart task is "
            f"{_task_state(WINDOWS_DAEMON_TASK_NAME)!r}, not Disabled -- it would start a second "
            f"daemon in the signed-in user's own session"
        )

        # ── `disable` is the way back out, and it is what the documented
        # "disable before uninstalling" order depends on. ───────────────────
        _disable_installer_enabled_privilege_separation(install_dir)
        assert _service_config() is None, f"the {WINDOWS_SERVICE_NAME} service survived `disable`"
        assert not _task_exists(WINDOWS_COMPANION_TASK_NAME)
        assert _task_state(WINDOWS_DAEMON_TASK_NAME) == "Enabled", (
            "`disable` must re-enable the daemon autostart task it turned off"
        )
    finally:
        uninstaller = install_dir / "unins000.exe"
        if uninstaller.is_file():
            _run_installer(str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")


# --------------------------------------------------------------------------- #
# Test 4 -- an install that cannot separate is not an install
# --------------------------------------------------------------------------- #

def test_windows_install_fails_when_the_image_is_user_writable(tmp_path):
    """Setup pointed at a directory the signed-in user can rewrite fails,
    loudly, instead of finishing successfully without mentioning the subject.

    The negative half of ADR 0003 decision 1. It matters because every default
    around it points the other way: Inno ignores a ``[Run]`` entry's exit code
    entirely, and ``CurStepChanged``'s own autostart step next door deliberately
    only warns -- so "finished successfully" is what a post-install problem
    looks like here unless something is done about it, and what it would have
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
