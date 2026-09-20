"""Packaged-artifact lifecycle test for the Linux ``.deb``.

This lifecycle -- install/validate/remove/purge, and, partially, that
reinstalling the same build over itself leaves ``$HOME`` alone -- used to be
proven only by hand. This module turns that into a repeatable CI job, the same role
``tests/integration/test_macos_packaged_smoke.py`` (TST-15) already plays for
the DMG: install the actual built artifact, not a source checkout or an
editable dev install, and exercise it as closely as possible to how a real
user would.

1. **Install**: real ``dpkg -i`` against the ``.deb`` ``scripts/build_deb.sh``
   just produced -- not `--target`, not a source checkout (contrast
   ``test_org_ubuntu_release_smoke.py``'s own "Why a --target install, not
   the .deb" docstring section: that module deliberately avoids the ``.deb``
   because its own layer doesn't need it; this module's whole point is that
   the ``.deb`` specifically is what's being proven).
2. **Validate the autostart entry**: ``desktop-file-validate`` against the
   installed ``/etc/xdg/autostart/privacyfence.desktop`` -- the one thing a
   malformed ``.desktop`` file would silently no-op on at the next graphical
   login rather than fail loudly, so this is the one place that would catch
   it before release.
3. **Start the real installed daemon** (``/usr/bin/privacyfence-app``, the
   wrapper ``debian/install`` puts on ``PATH`` -- not the PyInstaller onedir
   output directly, so this also proves the wrapper script itself works) and
   run the Phase 3 (``tests/system/test_local_mode_system.py``) daemon → MCP
   → approval → audit contract's own shape against it: a real bootstrap
   session, an MCP tool call resolved through the real HTTP decide route
   both Allow and Deny, the audit log read back from disk, and a graceful
   shutdown via the real "Quit PrivacyFence" action.

   **One deliberate substitution** from Phase 3's own scenario, for the same
   reason ``test_macos_packaged_smoke.py`` already made it: Phase 3 injects a
   synthetic ``Connector`` by monkeypatching ``daemon_main.build_connectors``
   *before* ``daemon_main`` is ever imported -- only possible when the test
   controls the Python import itself. A packaged, frozen daemon started as
   its own binary offers no such hook. This module instead drives
   ``privacyfence_propose_auto_accept_rule_change``, the one built-in
   meta-tool that "ALWAYS blocks on a native confirmation dialog" with no
   connector/credential of any kind behind it (see that tool's own
   description in ``web/mcp_tools.py``) -- same reasoning
   ``test_macos_packaged_smoke.py``'s own docstring gives for the identical
   choice. Unlike that module (which drives a real headless-Chromium click),
   this one resolves the pending card the same way Phase 3 itself does: a
   direct HTTP POST to ``/api/approvals/<id>/decide`` with the bootstrap-
   minted session cookie doubling as the CSRF token -- no Node/Playwright
   dependency needed here, keeping this job's prerequisites to exactly what
   ``scripts/build_deb.sh`` itself already needs (Python + ``dpkg``/
   ``lintian``).
4. **Remove** (``dpkg -r``): package-owned files gone
   (``/opt/privacyfence``, ``/usr/bin/privacyfence-app``); the autostart
   ``.desktop`` file -- a ``conffile`` -- deliberately survives a plain
   remove (dpkg's own conffile contract). ``prerm``'s own ``remove`` case
   also undoes separation (``disable``), which moves state from the system
   root back into ``$HOME`` -- the one deliberate exception to P2.2's "the
   package never reaches into `$HOME`" contract, documented in ``debian/
   prerm`` itself; this module confirms the state that lands there is the
   same state scenario 3 created.
5. **Purge** (``dpkg -P``): the conffile is now gone too; the state
   ``remove`` already restored to ``$HOME`` is still untouched by purge
   itself (there is nothing left separated to undo a second time).
6. **Upgrade in place** (P7.3): install version N, use it to create real
   on-disk state (an applied auto-accept rule, via the same MCP round trip
   as step 3), install a synthetically-bumped version N+1 of the identical
   build over it, confirm the state survived and the upgraded binary still
   starts and serves. Building a genuine second PyInstaller bundle just to
   get a "real" N+1 for this one assertion would multiply this module's
   already-heavy setup cost for no additional coverage (the file-level
   ``$HOME`` isolation this proves doesn't depend on what changed inside the
   package) -- see ``_synthetic_next_version_deb``'s own docstring for why a
   version-string bump on the same build is sufficient here, closing the
   exact gap P7.3's own "partially checked" note (which only re-installed
   the *identical* version) left open: a real version transition, not just a
   reinstall.
7. **The postinst's own failure policy** (ADR 0003 decision 5): an
   *unattended* install -- ``dpkg -i`` with no ``$SUDO_USER`` behind it, the
   MDM/``unattended-upgrades`` case -- configures successfully and still ends
   up separated, with only the group membership pending; and a machine half
   made to fail leaves dpkg with a half-configured package rather than an
   installed-looking, unseparated one. These two are the only tests in this
   module that assert *about* privilege separation rather than around it.

Scenarios 1-6 above run against the real, separated install a plain
``sudo dpkg -i``/``sudo apt install`` leaves behind: issue #428 D1 made
``debian/postinst`` provision privilege separation on every install *and*
upgrade, and ADR 0003 decision 5 made its machine half unconditional (the
per-user half still runs only when ``$SUDO_USER`` resolves to a real,
non-root account -- true for this module's own passwordless-``sudo`` CI
account, same as a real human's ``sudo dpkg -i``). That moves the daemon to
its own system account/unit (``privacyfence-daemon.service``, already
running by the time ``_dpkg("-i", ...)`` returns) and renames away the
autostart entry this module's step 2 validates -- see
``test_linux_graphical_session_autostart.py`` for that autostart-triggering
path itself, which this module doesn't re-prove.

This wasn't always true: before ADR 0003 decision 6
(``privilege_separation.enforce_separation()``, 4bcafc0/776128f) a packaged
daemon that found itself unseparated still served, just without the
guarantee, so scenarios 3 and 6 above used to run a second, directly-Popen'd
daemon against a scratch, deliberately-unseparated ``$HOME`` instead --
faster, and isolated from real system state. Decision 6 retired that option
outright (no override, on any packaged build, including a test's own --
see that function's own docstring for why), so what those two scenarios
exercise now is the real ``privacyfence-daemon.service`` itself, through
``sudo``-gated reads of its root-owned files -- see this module's own
"Real-daemon helpers" section for the mechanics and why every one of them
needs ``sudo -n`` rather than a direct read.

Skipped entirely unless running on real Linux with a just-built ``.deb`` on
disk, ``dpkg``/``dpkg-deb``/``desktop-file-validate`` on ``PATH``, and
passwordless root (via ``sudo -n``, or already running as root) -- installing
and removing a package needs it. This only makes sense as a step in
``.github/workflows/build.yml``'s ``build-deb`` job, right after
``scripts/build_deb.sh`` -- never runs as part of the ordinary ``pytest``
invocation in ``tests.yml``'s per-PR jobs, same posture as
``test_macos_packaged_smoke.py``.
"""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import platform
import re
import shlex
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

from tests.control_channel_client import mint_bootstrap_code_posix, resolve_posix_socket_path  # noqa: E402
from tests.diagnostics import failure_dir, suite_name_for  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "dist"
SETTINGS_EXAMPLE = REPO_ROOT / "src" / "privacyfence" / "resources" / "settings.yaml.example"

PACKAGE_NAME = "privacyfence"
DAEMON_BIN = Path("/usr/bin/privacyfence-app")
OPT_DIR = Path("/opt/privacyfence")
AUTOSTART_DESKTOP_FILE = Path("/etc/xdg/autostart/privacyfence.desktop")

# ADR 0003 decisions 3 and 5, as the postinst leaves them on disk. Spelled out
# here rather than imported from privilege_separation: this module asserts what
# the *installed package* did, and reading the constants from the same module
# the package's own script is held against (tests/unit/test_privilege_
# separation.py) would let both drift together.
SEPARATION_TOOL = Path("/usr/sbin/privacyfence-privilege-separation")
SYSTEM_ROOT = Path("/var/lib/privacyfence")
PRIVILEGE_SEPARATION_MARKER = SYSTEM_ROOT / "privilege-separation.json"
SERVICE_GROUP = "privacyfence"
DAEMON_SYSTEM_UNIT_FILE = Path("/etc/systemd/system/privacyfence-daemon.service")
DAEMON_UNIT_NAME = DAEMON_SYSTEM_UNIT_FILE.name

# ADR 0003 decision 6 (4bcafc0/776128f) made a packaged daemon refuse to
# serve at all unless genuinely separated -- see this module's own
# "Real-daemon helpers" section below for what that means for this module.
# These are privilege_separation.py's own paths.data_dir()/authority_dir()/
# handoff_dir() resolution, spelled out the same "not imported" way as the
# constants above, for a *separated* Linux install specifically (data_dir()
# == SYSTEM_ROOT once the marker exists -- privilege_separation.separation()).
AUTHORITY_DIR = SYSTEM_ROOT / "authority"
HANDOFF_DIR = SYSTEM_ROOT / "handoff"
SEPARATED_SETTINGS_PATH = AUTHORITY_DIR / "config" / "settings.yaml"
SEPARATED_AUDIT_DIR = AUTHORITY_DIR / "logs" / "audit"

MCP_TOKEN_FILE_NAME = "mcp_token"
SEPARATED_MCP_TOKEN_PATH = HANDOFF_DIR / MCP_TOKEN_FILE_NAME
# web/control_channel.py's own WEB_BASE_URL_FILE_NAME -- what the companion
# itself reads to learn the daemon's base_url() without hardcoding the
# default port, and cleared on WebServer.stop() (read_base_url()'s own
# docstring), which is what lets _wait_for_real_daemon() tell "not up yet"
# apart from "still the previous boot's value" across a quit/restart.
SEPARATED_WEB_BASE_URL_PATH = HANDOFF_DIR / "web_base_url"


def _built_debs() -> list[Path]:
    return sorted(DIST_DIR.glob(f"{PACKAGE_NAME}_*.deb")) if DIST_DIR.is_dir() else []


def _can_install_packages() -> bool:
    """``dpkg -i``/``-r``/``-P`` all need root. ``sudo -n`` succeeds
    immediately (no password prompt, no hang) both when already root -- this
    is how this plan's own P7.1 note says its implementation environment was
    exercised -- and on a passwordless-sudo CI runner (``ubuntu-latest``'s
    default ``runner`` user); anything else (a real password required, no
    sudo at all) fails fast here rather than hanging the test on a prompt
    nothing will ever answer."""
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.packaged,
    pytest.mark.skipif(
        platform.system() != "Linux",
        reason="only meaningful against a real .deb",
    ),
    pytest.mark.skipif(
        not _built_debs(),
        reason=(
            "no dist/privacyfence_*.deb built yet -- this is the release-workflow smoke test "
            "build.yml's build-deb job runs after scripts/build_deb.sh; run that script locally "
            "first to exercise this test outside CI"
        ),
    ),
    pytest.mark.skipif(
        shutil.which("dpkg") is None or shutil.which("dpkg-deb") is None,
        reason="dpkg/dpkg-deb not on PATH (apt-get install dpkg-dev)",
    ),
    pytest.mark.skipif(
        shutil.which("desktop-file-validate") is None,
        reason="desktop-file-validate not on PATH (apt-get install desktop-file-utils)",
    ),
    pytest.mark.skipif(
        not _can_install_packages(),
        reason="installing/removing a .deb needs root -- run as root or with passwordless sudo",
    ),
    # A real dpkg install/remove/purge cycle, two real daemon subprocess
    # boots, and several real HTTP/MCP round trips per boot -- comfortably
    # slower than the suite's default timeout=30, same reasoning as every
    # other packaged/system test in this repo.
    pytest.mark.timeout(180),
]


# --------------------------------------------------------------------------- #
# dpkg helpers
# --------------------------------------------------------------------------- #

def _dpkg(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["sudo", "-n", "dpkg", *args], capture_output=True, text=True, timeout=60)
    if check:
        assert result.returncode == 0, f"dpkg {' '.join(args)} failed:\n{result.stdout}{result.stderr}"
    return result


def _disable_auto_enabled_privilege_separation() -> None:
    """Undoes the separation ``postinst`` performs on every ``dpkg -i``
    (install *and* upgrade) -- since ADR 0003 decision 5 the machine half of
    it runs unconditionally, and the per-user half additionally runs whenever
    ``$SUDO_USER`` resolves to a real, non-root account, exactly what this CI
    runner's own passwordless-``sudo``-invoking account satisfies, same as a
    real human's ``sudo dpkg -i``/``sudo apt install`` would. This module's whole scenario
    (an autostart ``.desktop`` conffile that survives a plain remove, a daemon
    started directly via the installed wrapper against an isolated ``$HOME``)
    is the pre-D1, unseparated lifecycle -- ``test_linux_graphical_session_
    autostart.py``'s own "unseparated path" test pins the identical mechanism
    the identical way, right after its own ``_dpkg("-i", ...)``, for the same
    reason: once separation auto-enables, the daemon runs as its own system
    account under ``privacyfence-daemon.service``, the legacy autostart entry
    is renamed to ``.disabled``, and a second, directly-launched instance
    against an isolated ``$HOME`` either can't bind its ports/sockets or gets
    refused outright by ``check_runtime_identity`` -- none of which is what
    this module is testing. Must be re-run after every ``_dpkg("-i", ...)``
    in this module, including the upgrade-in-place one: the postinst fires on
    upgrade too, not just on a fresh install."""
    subprocess.run(
        ["sudo", "-n", "privacyfence-privilege-separation", "disable", "--user", getpass.getuser()],
        check=True, capture_output=True, text=True, timeout=30,
    )


def _reset_service_group_membership() -> None:
    """Strips this CI account back out of ``${SERVICE_GROUP}``, best-effort.

    ``cmd_disable()`` (``scripts/linux_privilege_separation.sh``) leaves the
    ``privacyfence`` group's membership alone on purpose -- its own printed
    note says so: keeping the account/group around means a later ``enable``
    doesn't have to pick a new uid. That is the right call for a real
    machine, where a human who was added stays added until they ask
    otherwise, but it means neither ``_disable_auto_enabled_privilege_
    separation()`` nor a plain ``dpkg -P`` (whose ``prerm`` also only calls
    plain ``disable``) actually returns this runner's own account to "not a
    member" between tests -- every test in this module runs real ``sudo
    dpkg -i``, which resolves ``$SUDO_USER`` to this same CI account, and
    once any one of them adds it, it stays added for the rest of the
    process. ``test_unattended_install_separates_the_machine_and_defers_the_
    membership`` asserts nobody is in the group after *its own* unattended
    install specifically -- an assertion only a genuinely clean slate can
    make. Root-only (``getent``/``gpasswd`` need it), so this goes through
    ``sudo -n`` like every other machine-state reset in this module; a
    missing group (nothing installed yet this run) is not an error."""
    subprocess.run(
        ["sudo", "-n", "gpasswd", "--delete", getpass.getuser(), SERVICE_GROUP],
        capture_output=True, text=True, timeout=10,
    )


def _reset_owner_home_state() -> None:
    """Best-effort cleanup of ``~/.privacyfence`` -- real state under this CI
    account's *real* ``$HOME``, not a ``tmp_path`` scratch copy, ever since
    this module started running its scenarios against the real separated
    daemon (see the "Real-daemon helpers" section). Every purge below
    triggers ``prerm``'s own ``disable`` call, which restores separated
    state to exactly this path -- deliberately, it is the documented
    "give the human their data back" gesture disabling performs, not a bug
    -- but leaving it there between tests would let one test's settings.yaml
    silently seed the next's, the same failure mode the old scratch-``$HOME``
    design existed to prevent. This account is disposable in CI (same
    posture test_linux_graphical_session_autostart.py's own module
    docstring already states for the identical reason), so deleting it here
    is safe; a real user's ``~/.privacyfence`` is never touched by anything
    in this module, since nothing in this module runs anywhere but a
    disposable CI runner (this module's own pytestmark requires
    passwordless sudo to even collect)."""
    shutil.rmtree(Path.home() / ".privacyfence", ignore_errors=True)


def _purge_if_present() -> None:
    """Best-effort cleanup -- covers both "fully installed" (a fresh
    ``dpkg -P`` needed) and "removed but not purged" (conffiles still on
    disk from a previous run's remove step), so a test that fails partway
    through never leaves the runner with this package in either state.

    Also resets this account's own ``${SERVICE_GROUP}`` membership and real
    ``$HOME`` state -- see ``_reset_service_group_membership()``/
    ``_reset_owner_home_state()`` -- since a plain purge alone does neither."""
    status = subprocess.run(["dpkg-query", "-W", "-f=${Status}", PACKAGE_NAME], capture_output=True, text=True)
    if status.returncode == 0 and status.stdout.strip() not in ("", "unknown ok not-installed"):
        _dpkg("-P", PACKAGE_NAME, check=False)
    _reset_service_group_membership()
    _reset_owner_home_state()


def _capture_installed_file_manifest(request) -> None:
    """An installed-file manifest -- unlike
    test_windows_packaged_smoke.py's own install directory (already under
    that test's own ``tmp_path``, so tests/diagnostics.py's generic
    per-``tmp_path`` manifest already covers it for free), a real
    ``dpkg -i`` installs into real system paths (``/opt/privacyfence``,
    ``/usr/bin``, ``/etc/xdg/autostart``) no ``tmp_path`` isolates -- this
    is the one packaged-artifact module that needs its own capture call for
    that reason. ``dpkg -L``/``dpkg -s`` are already exactly the manifest a
    human would reach for by hand, so this doesn't invent a new format."""
    rep_call = getattr(request.node, "rep_call", None)
    if rep_call is None or not rep_call.failed:
        return
    status = subprocess.run(["dpkg", "-s", PACKAGE_NAME], capture_output=True, text=True)
    if status.returncode != 0:
        return   # package isn't installed at all right now -- nothing to list
    listing = subprocess.run(["dpkg", "-L", PACKAGE_NAME], capture_output=True, text=True)
    dest = failure_dir(request.node.nodeid, suite=suite_name_for(__file__))
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "dpkg-status.txt").write_text(status.stdout + status.stderr, encoding="utf-8")
    (dest / "dpkg-installed-files.txt").write_text(listing.stdout + listing.stderr, encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_package_state(request):
    """Every test in this module installs/removes/purges the real system
    package -- global machine state, not something ``tmp_path`` isolates.
    Guarantee a clean slate on both sides of each test so failures don't
    cascade across tests or leave the runner with a stray install.

    Captures the installed-file manifest (see
    ``_capture_installed_file_manifest``) *before* the teardown purge below
    -- otherwise there'd be nothing left installed to list."""
    _purge_if_present()
    yield
    _capture_installed_file_manifest(request)
    _purge_if_present()


# --------------------------------------------------------------------------- #
# Real-daemon helpers.
#
# Before ADR 0003 decision 6 (4bcafc0/776128f, landed after v4.1.0a10 -- the
# last tag this module's own daemon-lifecycle helpers were green against),
# this module ran the packaged binary directly against a scratch,
# deliberately-unseparated $HOME -- undoing whatever postinst's machine half
# had just done via _disable_auto_enabled_privilege_separation() -- for a
# fast, isolated round trip that never touched real system state. Decision 6
# retired that option outright: a packaged daemon that finds itself
# unseparated now refuses to serve at all
# (privilege_separation.enforce_separation()), unconditionally, with no
# override for a test harness or anything else (see that function's own
# docstring). Since decision 5 already separates every `dpkg -i` (postinst's
# machine half runs unconditionally, and -- because _dpkg() goes through
# `sudo -n dpkg`, which sets $SUDO_USER to this CI account -- the per-user
# half runs too), what a real `sudo dpkg -i`/`sudo apt install` leaves
# running is the real, already-separated privacyfence-daemon.service. These
# helpers talk to *that* daemon instead of spawning a second, ad hoc one.
#
# Its files are root-owned (settings.yaml/the audit log under authority/,
# 0700; mcp_token/web_base_url/control.sock under handoff/, 2770 group
# ${SERVICE_GROUP}) -- and even though this CI account was just added to
# ${SERVICE_GROUP} by the per-user half above, a process that was already
# running when that happened never picks it up; only a fresh login does
# (same reasoning test_linux_graphical_session_autostart.py's own
# sudo-everything posture already documents for the identical problem on
# this same account). So every read below goes through `sudo -n`, exactly
# like that module's own helpers.
# --------------------------------------------------------------------------- #

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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _prepare_home(home: Path, *, port: int) -> None:
    """Pre-seeds (or re-seeds only the harness-convenience bits of) an
    isolated ``$HOME``'s ``settings.yaml``: a real free port and update
    checks disabled (this tier makes no real outbound network calls). If
    ``settings.yaml`` already exists, its existing content is loaded and
    only those two fields are overwritten, never replaced wholesale.

    Neither test in this module calls this any more -- ADR 0003 decision 6
    retired the "run the packaged binary directly against a scratch,
    unseparated $HOME" technique this was for (see this module's own
    "Real-daemon helpers" section) -- but the *unseparated* path it seeds
    is still a real, current product scenario elsewhere: a bare ``pip``/
    ``pipx`` install, or a ``.deb`` install with `disable` run against it.
    test_linux_graphical_session_autostart.py's own "unseparated path" test
    pins exactly that scenario, importing this function (and ``_free_port``
    below) to do it -- kept here, not there, so the two modules' own
    ``$HOME``-seeding stays byte-identical rather than drifting into two
    copies."""
    # #428 Phase 1: settings.yaml lives under an authority/ subdirectory of
    # data_dir(), same as the control channel's socket below -- not
    # data_dir() itself.
    config_dir = home / ".privacyfence" / "authority" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    settings_path = config_dir / "settings.yaml"
    if settings_path.exists():
        settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    else:
        settings = yaml.safe_load(SETTINGS_EXAMPLE.read_text(encoding="utf-8")) or {}
    settings.setdefault("web", {})["port"] = port
    settings.setdefault("update_check", {})["enabled"] = False
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")


def _sudo_capture(*args: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=timeout)


def _sudo_read_text(path: Path, *, timeout: float = 15) -> str | None:
    """``sudo -n cat`` -- see this section's own module comment. ``None``
    (not an exception) when the file does not exist yet, the same "not
    there" signal a direct ``.exists()`` would give a poll loop, which is
    this helper's main caller."""
    result = _sudo_capture("cat", str(path), timeout=timeout)
    return result.stdout if result.returncode == 0 else None


def _sudo_mint_bootstrap_code(socket_path: Path, *, timeout: float = 5.0) -> str:
    """Speaks the control channel's own one-line ``MINT`` protocol
    (tests/control_channel_client.py's ``mint_bootstrap_code_posix()``,
    which this can't call directly -- it has to run as root, and there is no
    reason ``sudo``'s own system ``python3`` would have the ``privacyfence``
    package importable) via a small stdlib-only inline script instead."""
    script = (
        "import socket,sys\n"
        f"s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        f"s.settimeout({timeout})\n"
        f"s.connect({str(socket_path)!r})\n"
        "s.sendall(b'MINT\\n')\n"
        "sys.stdout.write(s.recv(4096).decode('utf-8'))\n"
    )
    result = _sudo_capture("python3", "-c", script, timeout=timeout + 5)
    assert result.returncode == 0, f"minting a bootstrap code as root failed:\n{result.stdout}{result.stderr}"
    reply = result.stdout
    assert reply.startswith("OK "), f"control channel mint failed: {reply!r}"
    return reply[len("OK "):].strip()


class RunningDaemon:
    """The real, systemd-managed ``privacyfence-daemon.service`` -- not
    something this module owns a ``subprocess.Popen`` handle for any more,
    see this section's own module comment."""

    def __init__(self, base_url: str, mcp_token: str):
        self.data_dir = SYSTEM_ROOT
        self.base_url = base_url
        self.mcp_url = f"{base_url}/mcp"
        self.mcp_token = mcp_token


def _wait_for_real_daemon(*, timeout: float = 30.0) -> RunningDaemon:
    """Waits for the real ``privacyfence-daemon.service`` postinst's machine
    half just (re)started to actually come up, and returns a
    ``RunningDaemon`` for it. ``web_base_url`` (web/control_channel.py's own
    ``WEB_BASE_URL_FILE_NAME``) is the daemon's own way of telling the
    companion its port without either of them hardcoding a default, and is
    cleared on ``WebServer.stop()`` -- which is what lets this tell "not up
    yet" apart from "still the previous boot's value" the second time this
    module calls it, across a quit/restart against the same install."""
    deadline = time.monotonic() + timeout
    base_url = None
    while time.monotonic() < deadline:
        base_url = _sudo_read_text(SEPARATED_WEB_BASE_URL_PATH, timeout=5)
        if base_url and base_url.strip():
            base_url = base_url.strip()
            break
        base_url = None
        time.sleep(0.2)
    assert base_url, (
        f"{SEPARATED_WEB_BASE_URL_PATH} never appeared within {timeout}s -- {DAEMON_UNIT_NAME} status:\n"
        f"{_sudo_capture('systemctl', 'status', DAEMON_UNIT_NAME, '--no-pager', '-l').stdout}"
    )

    remaining = max(1.0, deadline - time.monotonic())
    parts = urlsplit(base_url)
    _wait_until_connectable(parts.hostname or "localhost", parts.port, timeout=remaining)

    mcp_token = None
    while time.monotonic() < deadline:
        mcp_token = _sudo_read_text(SEPARATED_MCP_TOKEN_PATH, timeout=5)
        if mcp_token and mcp_token.strip():
            mcp_token = mcp_token.strip()
            break
        mcp_token = None
        time.sleep(0.2)
    assert mcp_token, f"{SEPARATED_MCP_TOKEN_PATH} never appeared within {timeout}s"

    return RunningDaemon(base_url, mcp_token)


def _wait_for_daemon_unit_stopped(*, timeout: float = 15.0) -> None:
    """After the real "Quit PrivacyFence" action (``/api/settings/
    quit_app``, not the control channel's own ``QUIT`` verb -- a separated
    install's daemon refuses that outright, see installer/linux/
    privacyfence-daemon.service.tmpl's own ``Restart=`` comment for why: it
    is ``systemctl``'s unit to stop, not a line on a socket the agent can
    also reach), confirms the unit actually went down and *stayed* down --
    ``Restart=on-failure`` means a clean exit does not bounce it back, but a
    crash would, and that distinction is the point of checking at all."""
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        status = _sudo_capture("systemctl", "is-active", DAEMON_UNIT_NAME)
        if status.stdout.strip() != "active":
            break
        time.sleep(0.3)
    else:
        raise AssertionError(f"{DAEMON_UNIT_NAME} still active {timeout}s after quit_app")
    show = _sudo_capture("systemctl", "show", DAEMON_UNIT_NAME, "-p", "ExecMainStatus")
    assert "ExecMainStatus=0" in show.stdout, (
        f"{DAEMON_UNIT_NAME} did not exit cleanly after quit_app ({status.stdout.strip() if status else '?'}):\n"
        f"{show.stdout}{show.stderr}"
    )


# --------------------------------------------------------------------------- #
# The daemon → MCP → approval → audit round trip -- Phase 3's own shape
# (bootstrap session, tools/list, a gated call resolved through the real HTTP
# decide route, audit log confirms the decision), against the one built-in
# meta-tool that needs no connector (see module docstring's point 3).
# --------------------------------------------------------------------------- #

async def _bootstrap_session(
    web_client: httpx.AsyncClient, data_dir: Path = SYSTEM_ROOT, *, path: str = "/settings",
) -> str:
    # #428 Phase 2: minted through the control channel (a real Unix domain
    # socket against this daemon's own data directory), not a bearer-
    # authenticated HTTP route -- see tests.control_channel_client's own
    # module docstring. Defaults to the real separated system root (this
    # module's own two tests never pass anything else any more -- see the
    # "Real-daemon helpers" section for why they mint as root instead of
    # connecting directly). ``data_dir`` stays a parameter, not hardcoded,
    # because test_linux_graphical_session_autostart.py's own "unseparated
    # path" test imports this function and calls it against a scratch,
    # genuinely-unseparated ``$HOME`` instead, where a direct, unprivileged
    # connect is exactly correct (and the only thing that works -- nothing
    # there is root-owned).
    socket_path = resolve_posix_socket_path(data_dir)
    code = _sudo_mint_bootstrap_code(socket_path) if data_dir == SYSTEM_ROOT else mint_bootstrap_code_posix(socket_path)
    exchange_resp = await web_client.get(path, params={"bootstrap": code})
    assert exchange_resp.status_code == 200, exchange_resp.text
    session_id = web_client.cookies.get("pf_session")
    assert session_id, "bootstrap exchange did not set a pf_session cookie"
    return session_id


async def _propose_trusted_sender_rule(mcp_url: str, mcp_token: str, *, value: list[str]):
    """One real MCP session calling ``privacyfence_propose_auto_accept_rule_
    change`` -- a fresh session per call, since the tool's own confirmation
    dialog is a new "Claude tool-call turn" each time in production too, same
    reasoning as every other MCP helper in this repo's system/packaged
    tests."""
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
                        "reason": "tests/integration/test_deb_packaged_lifecycle.py packaged .deb lifecycle scenario",
                    },
                )


async def _resolve_pending_card(web_client: httpx.AsyncClient, session_id: str, *, decision: str) -> None:
    """Polls the real ``/approvals`` page for the one pending card the
    concurrently-running MCP call above just opened, then resolves it via a
    direct HTTP POST to the real decide route -- exactly what a human's
    browser does clicking the card's own Confirm/Cancel button
    (``dialog_window_html.py``'s ``data-pf-action`` values), just without a
    real browser in front of it (see module docstring point 3 for why this
    module doesn't need one)."""
    deadline = time.monotonic() + 20.0
    approval_id = None
    while time.monotonic() < deadline:
        page = await web_client.get("/approvals")
        assert page.status_code == 200, page.text
        # A real id is always ``uuid.uuid4().hex`` (approvals.py's
        # ``register_confirm``/``register_card``) -- deliberately not just
        # ``[^"]+``, since the page's own live-refresh JS builds the exact
        # same HTML client-side from a template string
        # (``'<div class="pf-approval-row" data-approval-id="' + esc(row.id)
        # + '">'``), which itself contains that literal opening substring
        # and would match immediately, before anything is actually pending.
        # The class attribute's optional trailing " pf-approval-row-
        # unbatchable" modifier (approval_list_html.py's _row_html) is what
        # a confirm-kind card -- like the rule-confirmation one this
        # scenario drives, never batchable -- actually carries.
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
        # Unauthenticated first, same as Phase 3's own scenario.
        assert (await web_client.get("/approvals")).status_code == 401
        assert (await web_client.get("/settings")).status_code == 401

        session_id = await _bootstrap_session(web_client)
        assert (await web_client.get("/settings")).status_code == 200

        # -- tools/list: the real MCP surface, no connector configured -----
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
        # Root-owned (authority/, 0700) -- one `sudo cat` of every *.jsonl in
        # the directory rather than a per-file glob()/read_text(), neither
        # of which this account can do directly. See this module's own
        # "Real-daemon helpers" section.
        audit_dir = SEPARATED_AUDIT_DIR
        cat_all = _sudo_capture("bash", "-c", f"cat {shlex.quote(str(audit_dir))}/*.jsonl 2>/dev/null")
        decisions = []
        for line in cat_all.stdout.splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("connector") == "rule":
                decisions.append(entry.get("decision"))
        assert "rule_changed_via_bridge_proposal" in decisions
        assert "rejected" in decisions

        # Same cross-platform-suite permission assertion Phase 3's own
        # scenario adds -- this module always runs on Linux (pytestmark
        # above), so no Windows skip needed here.
        mode_result = _sudo_capture("stat", "-c", "%a", str(audit_dir))
        assert mode_result.returncode == 0, f"could not stat {audit_dir}: {mode_result.stderr}"
        mode = int(mode_result.stdout.strip(), 8)
        assert mode & 0o077 == 0, f"{audit_dir} is group/world accessible: {mode:04o}"

        # -- Graceful shutdown via the real "Quit PrivacyFence" action -----------
        await _quit(web_client, session_id)

    _wait_for_daemon_unit_stopped()


# --------------------------------------------------------------------------- #
# Test 1 -- P7.1: install / validate / start+scenario / remove / purge
# --------------------------------------------------------------------------- #

async def test_deb_install_validate_scenario_remove_purge_lifecycle():
    deb_path = _built_debs()[-1]

    # ── Install. ADR 0003 decision 5 separates it unconditionally as part
    # of `configure` -- see this module's own "Real-daemon helpers" section
    # for what that means for the rest of this test ─────────────────────
    _dpkg("-i", str(deb_path))

    assert DAEMON_BIN.is_file(), f"{DAEMON_BIN} missing after dpkg -i"
    assert os.access(DAEMON_BIN, os.X_OK), f"{DAEMON_BIN} is not executable after dpkg -i"
    assert (OPT_DIR / "PrivacyFenceApp").is_file()
    assert AUTOSTART_DESKTOP_FILE.is_file()

    status = subprocess.run(["dpkg", "-s", PACKAGE_NAME], capture_output=True, text=True, check=True)
    assert "Status: install ok installed" in status.stdout

    # ── Validate the autostart entry (P7.1) ─────────────────────────────
    validate = subprocess.run(
        ["desktop-file-validate", str(AUTOSTART_DESKTOP_FILE)], capture_output=True, text=True,
    )
    assert validate.returncode == 0, f"{AUTOSTART_DESKTOP_FILE} failed validation:\n{validate.stdout}{validate.stderr}"

    # ── The real, already-running privacyfence-daemon.service; run the
    # Phase 3 scenario against it ─────────────────────────────────────────
    daemon = _wait_for_real_daemon()
    await _run_daemon_mcp_approval_audit_scenario(daemon)

    settings_text = _sudo_read_text(SEPARATED_SETTINGS_PATH)
    assert settings_text and "allowed.example.com" in settings_text, settings_text

    owner = _marker_owner()
    assert owner == getpass.getuser(), (
        f"the per-user half should have recorded {getpass.getuser()!r} as the marker's owner_user, got {owner!r}"
    )
    # dpkg -r's own prerm runs `disable` (see
    # _disable_auto_enabled_privilege_separation()'s own docstring for the
    # identical mechanism run by hand), which moves separated state back to
    # the owner's real $HOME -- this CI account's own, same disposable-
    # account posture test_linux_graphical_session_autostart.py's module
    # docstring already states for writing into it directly.
    restored_settings_path = Path.home() / ".privacyfence" / "authority" / "config" / "settings.yaml"
    try:
        # ── Remove (P7.1): package files gone; the autostart .desktop is a
        # conffile and survives a plain remove; separation is undone,
        # restoring state to $HOME rather than leaving it stranded under a
        # system root nothing can reach any more ──────────────────────────
        _dpkg("-r", PACKAGE_NAME)
        assert not OPT_DIR.exists(), f"{OPT_DIR} should be gone after dpkg -r"
        assert not DAEMON_BIN.exists(), f"{DAEMON_BIN} should be gone after dpkg -r"
        assert AUTOSTART_DESKTOP_FILE.exists(), "a conffile must survive a plain `dpkg -r` (only purge removes it)"
        assert restored_settings_path.exists(), (
            f"dpkg -r's own `disable` call should have restored state to {restored_settings_path}"
        )
        assert "allowed.example.com" in restored_settings_path.read_text(encoding="utf-8")

        remove_status = subprocess.run(["dpkg", "-s", PACKAGE_NAME], capture_output=True, text=True)
        assert "Status: deinstall ok config-files" in remove_status.stdout

        # ── Purge (P7.1): the conffile is now gone too; $HOME still
        # untouched -- already unseparated, so purge's own prerm `disable`
        # call is the documented no-op for an install with no marker left
        # to find ───────────────────────────────────────────────────────
        _dpkg("-P", PACKAGE_NAME)
        assert not AUTOSTART_DESKTOP_FILE.exists(), "dpkg -P must remove the conffile"
        purge_status = subprocess.run(["dpkg", "-s", PACKAGE_NAME], capture_output=True, text=True)
        assert purge_status.returncode != 0, f"package should be unknown to dpkg after purge:\n{purge_status.stdout}"
        assert restored_settings_path.exists(), "dpkg -P must never touch $HOME (P2.2)"
    finally:
        shutil.rmtree(Path.home() / ".privacyfence", ignore_errors=True)


# --------------------------------------------------------------------------- #
# Test 2 -- P7.3: upgrade in place preserves user state
# --------------------------------------------------------------------------- #

def _synthetic_next_version_deb(src_deb: Path, dst_deb: Path) -> str:
    """Repackages ``src_deb`` under a version guaranteed newer than its own
    under ``dpkg --compare-versions`` -- Debian's version-ordering algorithm
    treats a string that is a strict prefix of another as the smaller one, so
    appending a non-empty suffix is always ">" the original regardless of the
    original's own shape (stable/pre-release/dev-local, see
    ``scripts/build_deb.sh``'s own version-string handling comment). This is
    the same *bytes* as version N, just relabeled -- deliberately: what P7.3
    needs proven is that ``dpkg -i``-over-an-existing-install (a real version
    transition, not a same-version reinstall -- the gap its own "partially
    checked" note names) never reaches into ``$HOME``, which doesn't depend
    on anything actually changing *inside* the package. Building a second
    genuine PyInstaller bundle just for a "real" code change would multiply
    this module's already-heavy setup cost for no additional coverage of
    that claim.
    """
    dst_deb.parent.mkdir(parents=True, exist_ok=True)
    stage = dst_deb.parent / "upgrade-stage"
    shutil.rmtree(stage, ignore_errors=True)
    subprocess.run(["dpkg-deb", "--raw-extract", str(src_deb), str(stage)], check=True, capture_output=True)

    control_path = stage / "DEBIAN" / "control"
    control = control_path.read_text(encoding="utf-8")
    version_match = re.search(r"^Version: (.+)$", control, flags=re.MULTILINE)
    assert version_match, f"no Version: field found in {control_path}"
    new_version = f"{version_match.group(1)}+upgradetest1"
    control_path.write_text(
        re.sub(r"^Version: .+$", f"Version: {new_version}", control, flags=re.MULTILINE), encoding="utf-8",
    )

    subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(stage), str(dst_deb)],
        check=True, capture_output=True, text=True,
    )
    shutil.rmtree(stage, ignore_errors=True)
    return new_version


async def test_upgrade_in_place_preserves_user_state(tmp_path):
    deb_n = _built_debs()[-1]

    # ── Install version N; create real on-disk state through the real,
    # already-separated daemon (postinst's machine+per-user halves both run
    # as part of this same `sudo dpkg -i` -- see this module's own
    # "Real-daemon helpers" section) ───────────────────────────────────────
    _dpkg("-i", str(deb_n))
    daemon = _wait_for_real_daemon()
    async with httpx.AsyncClient(base_url=daemon.base_url, follow_redirects=True) as web_client:
        session_id = await _bootstrap_session(web_client)
        propose_task = asyncio.create_task(
            _propose_trusted_sender_rule(daemon.mcp_url, daemon.mcp_token, value=["preupgrade.example.com"])
        )
        await _resolve_pending_card(web_client, session_id, decision="confirm")
        result = await propose_task
        assert result.is_error is not True, getattr(result, "content", result)
        assert result.structured_content["changed"] is True

        await _quit(web_client, session_id)
    _wait_for_daemon_unit_stopped()

    settings_text = _sudo_read_text(SEPARATED_SETTINGS_PATH)
    assert settings_text and "preupgrade.example.com" in settings_text, settings_text

    # ── Install a synthetically-bumped version N+1 over it (P7.3). prerm's
    # `upgrade` case stops the unit before dpkg unpacks the new files over
    # /opt/privacyfence; postinst's machine half (which runs on every
    # `configure`, upgrade included) starts it back up once they're in
    # place -- the same real path a `sudo apt upgrade` takes, whether or not
    # the daemon we just quit above had already stopped itself. ──────────
    deb_n1 = tmp_path / "upgrade-build" / "privacyfence_next.deb"
    new_version = _synthetic_next_version_deb(deb_n, deb_n1)
    _dpkg("-i", str(deb_n1))

    status = subprocess.run(["dpkg", "-s", PACKAGE_NAME], capture_output=True, text=True, check=True)
    assert f"Version: {new_version}" in status.stdout

    # ── State survived the upgrade untouched (P2.2/P7.3) ──────────────────
    settings_text = _sudo_read_text(SEPARATED_SETTINGS_PATH)
    assert settings_text and "preupgrade.example.com" in settings_text, settings_text

    # ── The upgraded binary still starts and serves, without clobbering
    # the state it just inherited ─────────────────────────────────────────
    daemon = _wait_for_real_daemon()
    async with httpx.AsyncClient(base_url=daemon.base_url, follow_redirects=True) as web_client:
        session_id = await _bootstrap_session(web_client)
        assert (await web_client.get("/settings")).status_code == 200
        await _quit(web_client, session_id)
    _wait_for_daemon_unit_stopped()

    settings_text = _sudo_read_text(SEPARATED_SETTINGS_PATH)
    assert settings_text and "preupgrade.example.com" in settings_text, settings_text


# --------------------------------------------------------------------------- #
# Test 3 -- ADR 0003 decision 5: the postinst's two halves and their two
# failure policies. Everything above this line deliberately reverts the
# separation the postinst performs; these two are what assert it happened,
# and how it behaves when it can't.
# --------------------------------------------------------------------------- #

def _service_group_members() -> list[str]:
    """The *recorded* membership, straight out of ``/etc/group`` -- the same
    thing ``privilege_separation.service_group_members()`` reads, and
    deliberately not this process's own token (which predates any group change
    made during this test and would answer for a session, not for the file)."""
    result = subprocess.run(
        ["getent", "group", SERVICE_GROUP], capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return []
    return [name for name in result.stdout.strip().split(":")[-1].split(",") if name]


def _marker_owner() -> str:
    """``owner_user`` out of the marker the machine half wrote. World-readable
    on purpose (see the script's own ``write_marker``), so no ``sudo`` here."""
    return json.loads(PRIVILEGE_SEPARATION_MARKER.read_text(encoding="utf-8"))["owner_user"]


def _dpkg_status() -> str:
    return subprocess.run(
        ["dpkg", "-s", PACKAGE_NAME], capture_output=True, text=True,
    ).stdout


def test_unattended_install_separates_the_machine_and_defers_the_membership(tmp_path):
    """ADR 0003 decision 5's own stated case: "an unattended upgrade with no
    session behind it still succeeds *and* still ends up separated".

    ``env -u SUDO_USER`` is what makes this an unattended install rather than
    a human's: ``sudo`` sets ``SUDO_USER`` in the environment it hands dpkg,
    and stripping it back off reproduces exactly what an MDM push, a root
    shell or ``unattended-upgrades`` gives the postinst -- nothing to say
    which human this install is for. Before decision 3 that left the whole
    install unseparated; before decision 5 the postinst then swallowed the
    outcome with ``|| true``."""
    deb_path = _built_debs()[-1]

    result = subprocess.run(
        ["sudo", "-n", "env", "-u", "SUDO_USER", "dpkg", "-i", str(deb_path)],
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, (
        "an unattended `dpkg -i` must still configure successfully -- decision 3 is what makes "
        f"the machine half runnable with no human resolvable:\n{result.stdout}{result.stderr}"
    )
    assert "Status: install ok installed" in _dpkg_status()

    # Separated, not "opt-in": the machine half ran all of it.
    assert PRIVILEGE_SEPARATION_MARKER.is_file(), (
        f"{PRIVILEGE_SEPARATION_MARKER} missing after an unattended install -- the machine half "
        "either didn't run or didn't complete, and the postinst no longer has a `|| true` that "
        "would have hidden that"
    )
    assert DAEMON_SYSTEM_UNIT_FILE.is_file(), f"{DAEMON_SYSTEM_UNIT_FILE} missing after dpkg -i"

    # ...and what is outstanding is exactly one re-runnable step.
    assert _marker_owner() == "", (
        "nobody was resolvable, so the marker must record no owner -- an owner here means the "
        "machine half picked one up from somewhere an unattended install shouldn't have one"
    )
    assert _service_group_members() == [], (
        f"nobody should be in {SERVICE_GROUP} after an unattended install: "
        f"{_service_group_members()}"
    )

    status = subprocess.run(
        ["sudo", "-n", str(SEPARATION_TOOL), "status"], capture_output=True, text=True, timeout=30,
    )
    assert "privilege separation: ON" in status.stdout, status.stdout
    assert "PENDING USER" in status.stdout, (
        "the pending membership has to be reported as its own state -- reporting it as OFF would "
        f"say the daemon and the agent share an account, which is what is no longer true:\n"
        f"{status.stdout}"
    )

    # The per-user half closes it, from here, with no reinstall -- which is
    # what makes deferring it a supported state rather than a broken install.
    subprocess.run(
        ["sudo", "-n", str(SEPARATION_TOOL), "enable", "--for-user", getpass.getuser()],
        check=True, capture_output=True, text=True, timeout=60,
    )
    assert _marker_owner() == getpass.getuser()
    assert getpass.getuser() in _service_group_members()

    _disable_auto_enabled_privilege_separation()


def test_a_failing_machine_half_fails_the_package_install(tmp_path):
    """The other half of decision 5: the ``|| true`` is gone, so a machine
    half that cannot do its job stops the install where it is.

    The induced failure is a regular file sitting where ``/var/lib/
    privacyfence`` has to be a directory -- ``migrate_data``'s own
    ``mkdir -p`` is what trips on it, under the script's ``set -e``. Any
    failure of the machine half would do; this one is deterministic, needs no
    edit to the package being tested, and is undone by deleting one file."""
    deb_path = _built_debs()[-1]
    subprocess.run(["sudo", "-n", "rm", "-rf", str(SYSTEM_ROOT)], check=True, timeout=30)
    subprocess.run(["sudo", "-n", "touch", str(SYSTEM_ROOT)], check=True, timeout=30)
    try:
        result = subprocess.run(
            ["sudo", "-n", "dpkg", "-i", str(deb_path)], capture_output=True, text=True, timeout=120,
        )

        assert result.returncode != 0, (
            "a failed machine half must fail the package install -- this is the whole of decision "
            f"5:\n{result.stdout}{result.stderr}"
        )
        assert "post-installation script subprocess returned error" in result.stderr, result.stderr

        # dpkg's own record of it: something a human (or `apt`) will trip over
        # again, rather than a package that reports itself installed while the
        # claim it is installed *for* does not hold.
        status = _dpkg_status()
        assert "half-configured" in status, (
            f"the package should be left unconfigured, not installed:\n{status}"
        )
        assert not PRIVILEGE_SEPARATION_MARKER.exists(), (
            f"{PRIVILEGE_SEPARATION_MARKER} exists -- the machine half was supposed to have failed "
            "before writing it"
        )
    finally:
        subprocess.run(["sudo", "-n", "rm", "-f", str(SYSTEM_ROOT)], check=True, timeout=30)

    # With the obstruction gone, the very same configure step succeeds and
    # separates the install -- no reinstall, no repair mode, no leftovers from
    # the failed run.
    configure = subprocess.run(
        ["sudo", "-n", "dpkg", "--configure", PACKAGE_NAME],
        capture_output=True, text=True, timeout=120,
    )
    assert configure.returncode == 0, f"{configure.stdout}{configure.stderr}"
    assert "Status: install ok installed" in _dpkg_status()
    assert PRIVILEGE_SEPARATION_MARKER.is_file()

    _disable_auto_enabled_privilege_separation()
