"""Real graphical-session autostart verification for the Linux ``.deb``.

``test_deb_packaged_lifecycle.py`` (Phase 6.3) already proves the install/
validate/remove/purge/upgrade lifecycle, including that ``desktop-file-
validate`` accepts the installed ``/etc/xdg/autostart/privacyfence.desktop``.
What it deliberately does *not* prove -- see that module's own docstring,
which only starts the daemon directly -- is this module's own open
question: does a real graphical login *itself* start the daemon via that
autostart entry, with nothing else telling it to?

There is no real display manager (gdm/lightdm/sddm) available on a CI
runner to log into, so this module makes one deliberate substitution
instead of a hand-rolled one: it drives the *real* OS mechanism a modern
systemd-based desktop session already uses for XDG autostart --
``systemd-xdg-autostart-generator`` (systemd >= 247; shipped as part of
``systemd`` itself, confirmed present by this module's own skip condition)
turns every ``/etc/xdg/autostart/*.desktop`` entry into a transient
``app-<name>@autostart.service`` unit, gated behind
``xdg-desktop-autostart.target`` -- the exact target a real GNOME/KDE/Sway
session's own compositor/session manager starts once it comes up. Nothing
in this module parses or interprets ``privacyfence.desktop`` itself; it
only brings up a real ``systemd --user`` manager for the account (the same
``user@<uid>.service`` unit ``pam_systemd`` starts at a real login) and
starts that one real target -- standing in for the missing physical
login, standing in for a real or VM desktop session.
Everything downstream -- the generated unit's shape, whether it actually
gets pulled in, whether starting it actually launches the packaged binary,
whether the daemon then serves a real MCP/approval/audit round trip, and
whether "Quit PrivacyFence" cleanly stops the *systemd unit*, not just the
process -- is exercised for real.

Unlike every other packaged/system test in this repo, this one does **not**
isolate ``$HOME`` under ``tmp_path``: the whole point is that systemd starts
the daemon the same way a real login would, with no injected environment.
It refuses to run at all (skips) if this account already has real
PrivacyFence state under ``$HOME``, and always removes whatever it creates
there afterwards -- see ``_real_home_state``. Only ever run this against a
disposable CI account.

#428 D1 (4.1): a plain ``dpkg -i``/``sudo apt install`` of this ``.deb`` no
longer leaves the daemon's own XDG autostart entry in place -- ``debian/
postinst`` now runs ``privacyfence-privilege-separation enable --auto`` on
every install, which separates the daemon into its own system account and
system unit whenever ``$SUDO_USER`` resolves to a real, non-root account (as
it does for this workflow's own ``sudo``-invoking ``runner`` CI account, and
for a real human's ``sudo apt install``). This module now has two autostart
scenarios instead of one, because that changed *what* is supposed to
autostart in the login session, not just whether it does:

- **Separated (the new default)**: the daemon is already up under
  ``privacyfence-daemon.service`` (a system unit) *before* any login at
  all -- ``enable --auto`` starts it synchronously from ``postinst``. What
  the login session's own XDG autostart now activates is the *companion*
  app's own control channel (``privacyfence-companion --serve``, ADR 0002
  decision 5b), not the daemon -- the daemon has no desktop session of its
  own to autostart into any more.
- **Unseparated**: still what a bare ``pip``/``pipx`` install gets today
  (nothing there ever runs ``enable --auto`` -- that hook is ``debian/
  postinst``'s alone), and still reachable from a ``.deb`` install by
  running ``disable``. This is the pre-D1 mechanism this module always
  tested: the daemon's own XDG autostart entry starts the daemon directly
  in the login session.

Both are exercised below as separate tests.

A third, independent test in this module covers the OAuth loopback
browser-opening flow, where practical, in the one way a real graphical
session uniquely enables:
``tests/platform/test_browser_launch_default.py`` (Phase 2.3) already
proves ``oauth_loopback.run_browser_oauth()``'s default path reaches the
real ``webbrowser.open`` function object, but it does so by *monkeypatching
that function itself*, headless, with no ``$DISPLAY`` -- by its own
docstring's admission, the stdlib ``webbrowser`` module's own real
browser-detection-and-subprocess-launch logic (the part that differs
between "no GUI available" and "a real X session is up") is never actually
exercised end to end anywhere in this repo. This module's third test runs
that unmocked, under a real Xvfb ``$DISPLAY``, pointed (via the stdlib's own
supported ``$BROWSER`` override) at a tiny real executable that performs the
actual loopback HTTP round trip itself, as a real subprocess -- proving the
genuine OS launch chain, not an injected stand-in.

All three tests are skipped entirely unless running on real Linux, booted
with systemd as PID 1
(``/run/systemd/system`` -- a container typically fails this, a
GitHub-hosted ``ubuntu-latest`` runner, a real VM, passes it), with whatever
else each specific test additionally needs (a just-built ``.deb`` and
passwordless root for the autostart tests; ``Xvfb`` for the browser test).
This is the most expensive tier in docs/testing-policy.md's
test taxonomy (layer 6, packaged-artifact) by design -- scheduled on
packaging-related ``main`` changes, nightly/periodic runs, and
release-candidate tags via its own
``.github/workflows/linux-graphical-session.yml``, deliberately kept out of
both the per-PR ``tests.yml`` jobs and ``build.yml``'s tag-triggered release
pipeline: unlike ``test_deb_packaged_lifecycle.py``, whose failure correctly
does block a release, this tier is too heavy to gate every PR or release on.
"""
from __future__ import annotations

import asyncio
import contextlib
import getpass
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

import httpx
import pytest

pytest.importorskip("mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'")

from privacyfence.privilege_separation import (  # noqa: E402
    HANDOFF_DIR_NAME,
    LINUX_SYSTEM_ROOT,
    MARKER_FILE_NAME,
)
from privacyfence.web.control_channel import companion_socket_path_under, socket_path_under  # noqa: E402
from tests.control_channel_client import resolve_posix_socket_path  # noqa: E402
from tests.diagnostics import (  # noqa: E402
    capture_directory_manifest,
    failure_dir,
    suite_name_for,
    write_environment_info,
)
from tests.integration.test_deb_packaged_lifecycle import (  # noqa: E402
    MCP_TOKEN_FILE_NAME,
    OPT_DIR,
    AUTOSTART_DESKTOP_FILE,
    _bootstrap_session,
    _built_debs,
    _can_install_packages,
    _dpkg,
    _free_port,
    _prepare_home,
    _propose_trusted_sender_rule,
    _purge_if_present,
    _quit,
    _resolve_pending_card,
    _wait_until_connectable,
)

# #428 D1 -- the separated layout's own root and marker, on this platform.
# Imported rather than re-declared so this test can never drift from
# scripts/linux_privilege_separation.sh's own constants (the same reasoning
# AUTOSTART_UNIT_NAME below already applies to the legacy autostart entry).
SYSTEM_ROOT = LINUX_SYSTEM_ROOT
HANDOFF_DIR = SYSTEM_ROOT / HANDOFF_DIR_NAME
PRIVILEGE_SEPARATION_MARKER = SYSTEM_ROOT / MARKER_FILE_NAME

# systemd reserves a bare "-" as the unit-name hierarchy separator, so
# systemd-xdg-autostart-generator escapes any "-" inside the desktop file's
# own stem (systemd.unit(5)'s unit-name string escaping) before splicing it
# into the generated unit name. Confirmed against the real systemd-escape
# binary shipped in this environment (systemd 255, Ubuntu -- same lineage as
# GitHub's ubuntu-latest runner image): `systemd-escape privacyfence-companion`
# -> `privacyfence\x2dcompanion`, while `systemd-escape privacyfence` (no
# dash) is unchanged -- which is why AUTOSTART_UNIT_NAME below always worked
# (the daemon's stem has no dash) while COMPANION_AUTOSTART_UNIT_NAME didn't
# (the companion's does).
_UNIT_NAME_SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_."
)


def _systemd_escape_unit_name_component(text: str) -> str:
    """Mirrors systemd-escape(1)'s unit-name string escaping: every byte
    outside the safe set above -- "-" included, since systemd reserves a
    bare "-" as the unit-name hierarchy separator -- becomes a C-style
    \\xAB hex escape."""
    return "".join(c if c in _UNIT_NAME_SAFE_CHARS else f"\\x{ord(c):02x}" for c in text)


# Empirically confirmed against the real systemd-xdg-autostart-generator
# binary shipped in this environment: a source .desktop file named
# "<stem>.desktop" becomes unit "app-<escaped-stem>@autostart.service",
# listed under xdg-desktop-autostart.target.wants/. Derived from
# AUTOSTART_DESKTOP_FILE rather than hardcoded so it can never silently
# drift from the actual installed filename.
AUTOSTART_UNIT_NAME = f"app-{_systemd_escape_unit_name_component(AUTOSTART_DESKTOP_FILE.stem)}@autostart.service"

# #428 D1/Phase 4 (B5b): the companion's own autostart entry -- what a
# separated install's login session activates instead of the daemon's now-
# disabled entry above. Same generator-derivation reasoning as
# AUTOSTART_UNIT_NAME, and the system unit `enable --auto` starts the daemon
# under once separated (installer/linux/privacyfence-daemon.service.tmpl).
COMPANION_AUTOSTART_DESKTOP_FILE = Path("/etc/xdg/autostart/privacyfence-companion.desktop")
COMPANION_AUTOSTART_UNIT_NAME = (
    f"app-{_systemd_escape_unit_name_component(COMPANION_AUTOSTART_DESKTOP_FILE.stem)}@autostart.service"
)
COMPANION_BIN = OPT_DIR / "PrivacyFenceCompanion"
DAEMON_SYSTEM_UNIT = "privacyfence-daemon.service"

_XDG_AUTOSTART_GENERATOR_PATHS = (
    Path("/usr/lib/systemd/user-generators/systemd-xdg-autostart-generator"),
    Path("/lib/systemd/user-generators/systemd-xdg-autostart-generator"),
)


def _has_xdg_autostart_generator() -> bool:
    return any(p.exists() for p in _XDG_AUTOSTART_GENERATOR_PATHS)


def _systemd_is_init() -> bool:
    """The standard, documented way to detect "booted with systemd as PID
    1" (systemd's own docs). Most containers -- including whatever this
    module might accidentally be run in outside CI -- fail this; a real
    GitHub-hosted ``ubuntu-latest`` runner (a real VM, not a container)
    passes it."""
    return Path("/run/systemd/system").exists()


def _sudo_path_exists(path: Path) -> bool:
    """Existence check for a path under a privilege-separated install's
    ``handoff/`` dir (2770, group ``privacyfence``): `enable` adds this
    test's own account to that group, but group membership only takes
    effect for a *new* session (see linux_privilege_separation.sh's own "log
    out and back in" note), and this already-running test process is not
    one. `sudo -n` sidesteps the permission check entirely instead, the same
    way `_dpkg` already does for `dpkg -i`/`-r`/`-P`."""
    return subprocess.run(
        ["sudo", "-n", "test", "-e", str(path)], capture_output=True, timeout=10,
    ).returncode == 0


def _wait_for_path_as_root(path: Path, *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _sudo_path_exists(path):
            return
        time.sleep(0.1)
    raise AssertionError(f"{what} ({path}) never appeared within {timeout}s")


def _sudo_readlink(path: str) -> str:
    """``os.readlink()``, elevated -- needed for the separated daemon's own
    ``/proc/<pid>/exe``, which runs as the ``privacyfence`` service account,
    not this test's own uid."""
    result = subprocess.run(
        ["sudo", "-n", "readlink", path], capture_output=True, text=True, timeout=10, check=True,
    )
    return result.stdout.strip()


pytestmark = [
    pytest.mark.packaged,
    pytest.mark.skipif(
        platform.system() != "Linux",
        reason="only meaningful on real Linux",
    ),
    # Same heavy-setup timeout reasoning as test_deb_packaged_lifecycle.py:
    # a real dpkg install, a real systemd --user manager brought up from
    # scratch, a real daemon boot via a real login-equivalent target, and a
    # full MCP/approval/audit round trip.
    pytest.mark.timeout(180),
]


# --------------------------------------------------------------------------- #
# Small polling helpers -- same shape as test_deb_packaged_lifecycle.py's own
# _wait_until_connectable/_wait_for_file, adapted for a process this module
# doesn't itself parent (systemd does), so there is no Popen to poll.poll().
# --------------------------------------------------------------------------- #

def _wait_for_path(path: Path, *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.1)
    raise AssertionError(f"{what} ({path}) never appeared within {timeout}s")


def _wait_for_path_content(path: Path, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
        time.sleep(0.1)
    raise AssertionError(f"{path} never appeared/populated within {timeout}s")


def _wait_for_unit_property(systemctl_user, unit: str, prop: str, expected: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = systemctl_user("show", unit, "-p", prop, "--value").stdout.strip()
        if last == expected:
            return
        time.sleep(0.2)
    status = systemctl_user("status", unit, check=False).stdout
    raise AssertionError(
        f"{unit}'s {prop} never reached {expected!r} (last seen {last!r}) within {timeout}s:\n{status}"
    )


def _current_user() -> str:
    return getpass.getuser()


# --------------------------------------------------------------------------- #
# $HOME guard/cleanup -- see module docstring for why this is the one
# packaged/system test in this repo that deliberately does NOT isolate
# $HOME under tmp_path.
# --------------------------------------------------------------------------- #

def _capture_real_home_diagnostics(request, real_home: Path, state_dir: Path) -> None:
    """This module's own tests/diagnostics.py capture call, since ``_real_home_state`` (unlike
    every other packaged/system fixture in this repo) deliberately isolates
    nothing under ``tmp_path`` -- see this fixture's own docstring for why.
    Its own daemon never even logs to a file (`systemd --user` captures its
    stdout/stderr into the real user journal, not `$HOME`), so this reaches
    for the journal directly via `journalctl` rather than
    tests/diagnostics.py's generic ``copy_named_logs``, which would find
    nothing here."""
    rep_call = getattr(request.node, "rep_call", None)
    if rep_call is None or not rep_call.failed:
        return
    dest = failure_dir(request.node.nodeid, suite=suite_name_for(__file__))
    write_environment_info(dest / "environment.txt")
    capture_directory_manifest(state_dir, dest / "manifest.txt")
    (dest / "logs").mkdir(parents=True, exist_ok=True)
    # Best-effort, all three units this module can start: whichever test
    # failed, only the units it actually touched will have anything in
    # them. The system daemon unit's journal needs root to read.
    for unit, cmd in (
        (AUTOSTART_UNIT_NAME, ["journalctl", "--user", "-u", AUTOSTART_UNIT_NAME, "--no-pager"]),
        (
            COMPANION_AUTOSTART_UNIT_NAME,
            ["journalctl", "--user", "-u", COMPANION_AUTOSTART_UNIT_NAME, "--no-pager"],
        ),
        (DAEMON_SYSTEM_UNIT, ["sudo", "-n", "journalctl", "-u", DAEMON_SYSTEM_UNIT, "--no-pager"]),
    ):
        journal = subprocess.run(cmd, capture_output=True, text=True)
        (dest / "logs" / f"journalctl-{unit}.log").write_text(
            journal.stdout + journal.stderr, encoding="utf-8",
        )


@pytest.fixture
def _real_home_state(request):
    real_home = Path.home()
    state_dir = real_home / ".privacyfence"
    if state_dir.exists():
        pytest.skip(
            f"{state_dir} already exists -- this test boots the daemon into the real $HOME with "
            "no isolation (the whole point is a real, un-injected login); only run it against a "
            "disposable account with no existing PrivacyFence state"
        )
    _purge_if_present()
    try:
        yield real_home
    finally:
        _capture_real_home_diagnostics(request, real_home, state_dir)
        _purge_if_present()
        shutil.rmtree(state_dir, ignore_errors=True)


# Shared skip stack for every test below that installs the real .deb and
# drives a real login-equivalent systemd --user session -- factored out so
# the separated and unseparated cases below can't drift apart on when
# either is even meaningful to run.
def _needs_deb_login_session(fn):
    marks = (
        pytest.mark.skipif(
            not _built_debs(),
            reason=(
                "no dist/privacyfence_*.deb built yet -- run scripts/build_deb.sh first, same as "
                "test_deb_packaged_lifecycle.py's own identical skip"
            ),
        ),
        pytest.mark.skipif(
            shutil.which("dpkg") is None or shutil.which("dpkg-deb") is None,
            reason="dpkg/dpkg-deb not on PATH (apt-get install dpkg-dev)",
        ),
        pytest.mark.skipif(
            not _can_install_packages(),
            reason="installing a .deb needs root -- run as root or with passwordless sudo",
        ),
        pytest.mark.skipif(
            not _systemd_is_init(), reason="not booted with systemd as PID 1 -- needs a real VM, not a container",
        ),
        pytest.mark.skipif(
            not _has_xdg_autostart_generator(),
            reason="systemd-xdg-autostart-generator not present -- needs systemd >= 247 on a desktop-capable Ubuntu/Debian",
        ),
        pytest.mark.skipif(
            shutil.which("loginctl") is None or shutil.which("systemctl") is None or shutil.which("systemd-run") is None,
            reason="loginctl/systemctl/systemd-run not on PATH",
        ),
    )
    for mark in reversed(marks):
        fn = mark(fn)
    return fn


def _start_real_login_session(user: str, uid: int) -> tuple[dict, Callable[..., subprocess.CompletedProcess]]:
    """Brings up a real ``systemd --user`` manager for this account, exactly
    the unit ``pam_systemd`` starts at a real login, then triggers the same
    target a real desktop session's own compositor/session manager pulls in
    once it's up. See module docstring for why this is the one deliberate
    substitution both autostart tests below make -- everything else is the
    real OS mechanism. Returns the environment a ``systemctl --user`` call
    needs, and a ``systemctl_user()`` helper bound to it."""
    subprocess.run(
        ["sudo", "-n", "loginctl", "enable-linger", user], check=True, capture_output=True, text=True, timeout=15,
    )
    subprocess.run(
        ["sudo", "-n", "systemctl", "start", f"user@{uid}.service"],
        check=True, capture_output=True, text=True, timeout=15,
    )

    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}")
    _wait_for_path(runtime_dir / "bus", timeout=30, what="user session D-Bus socket")
    user_env = {
        **os.environ,
        "XDG_RUNTIME_DIR": str(runtime_dir),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus",
    }

    def systemctl_user(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", "--user", *args], env=user_env, capture_output=True, text=True, timeout=30, check=check,
        )

    # A real login's user manager always (re-)runs its generators
    # (systemd-xdg-autostart-generator among them) on its own startup;
    # daemon-reload makes that moment explicit and repeatable here, since
    # the .deb's postinst just dropped a new autostart entry after this
    # manager may already have been running for other CI reasons.
    systemctl_user("daemon-reload")
    return user_env, systemctl_user


def _trigger_graphical_session_target(user_env: dict) -> None:
    """The actual "login" moment -- the same target a real GNOME/KDE/Sway
    session starts once its compositor/session manager comes up.

    ``xdg-desktop-autostart.target`` ships from systemd itself with
    ``RefuseManualStart=yes`` (see ``units/user/xdg-desktop-autostart.target``
    in the systemd source): a plain ``systemctl --user start`` against it is
    refused outright -- exit 4 (``EXIT_NOPERMISSION``) -- on *any* real
    systemd, not just in CI. That's by design: it exists to be pulled in as
    a *dependency* of a real desktop session's own session-tracking unit,
    never started directly -- and ``RefuseManualStart`` explicitly still
    permits dependency-triggered starts. A transient oneshot unit that
    simply ``Wants=`` it, started via ``systemd-run``, is exactly that
    dependency trigger -- the same mechanism a real compositor/session-
    manager unit's own static ``Wants=`` achieves, just assembled on the fly
    here instead of shipped on disk."""
    subprocess.run(
        [
            "systemd-run", "--user", "--collect", "--quiet",
            "--unit=privacyfence-test-session-trigger",
            "--property=Type=oneshot",
            "--property=Wants=xdg-desktop-autostart.target",
            "/bin/true",
        ],
        env=user_env, check=True, capture_output=True, text=True, timeout=15,
    )


# --------------------------------------------------------------------------- #
# Test 1 -- #428 D1's new default: a separated install's daemon comes up
# under its own *system* unit (no login involved at all), and what the
# login session's XDG autostart activates instead is the companion's own
# control channel (ADR 0002 decision 5b) -- not the daemon.
# --------------------------------------------------------------------------- #

@_needs_deb_login_session
async def test_deb_autostart_starts_companion_while_daemon_runs_under_system_unit(_real_home_state):
    home = _real_home_state
    user = _current_user()
    uid = os.getuid()

    deb_path = _built_debs()[-1]
    port = _free_port()
    _prepare_home(home, port=port)

    # ── Install. #428 D1: postinst's `enable --auto` now separates the
    # install synchronously, inside `dpkg -i` itself, whenever $SUDO_USER
    # resolves to a real, non-root account -- true of this job's own
    # `sudo`-invoking `runner` CI account, same as a real human's `sudo apt
    # install`. ─────────────────────────────────────────────────────────
    _dpkg("-i", str(deb_path))

    assert PRIVILEGE_SEPARATION_MARKER.is_file(), (
        f"{PRIVILEGE_SEPARATION_MARKER} missing after dpkg -i -- this test assumes `enable --auto` "
        "separated the install the same way a real `sudo apt install`/`sudo dpkg -i` would; if "
        "$SUDO_USER isn't resolving to a real account here, see debian/postinst's own --auto gating"
    )

    # ── The legacy, single-account mechanism is gone: stop_legacy_autostart()
    # moved the daemon's own autostart entry aside, and the companion's own
    # entry -- which validates the same way the legacy one always has --
    # takes its place. ───────────────────────────────────────────────────
    assert not AUTOSTART_DESKTOP_FILE.exists(), f"{AUTOSTART_DESKTOP_FILE} should be disabled once separated"
    assert Path(f"{AUTOSTART_DESKTOP_FILE}.disabled").is_file()

    assert COMPANION_AUTOSTART_DESKTOP_FILE.is_file()
    validate = subprocess.run(
        ["desktop-file-validate", str(COMPANION_AUTOSTART_DESKTOP_FILE)], capture_output=True, text=True,
    )
    assert validate.returncode == 0, (
        f"{COMPANION_AUTOSTART_DESKTOP_FILE} failed validation:\n{validate.stdout}{validate.stderr}"
    )

    # ── The daemon is already up under its own system unit -- no login
    # needed at all, unlike the unseparated path (this module's other
    # autostart test). ───────────────────────────────────────────────────
    daemon_state = subprocess.run(
        ["systemctl", "is-active", DAEMON_SYSTEM_UNIT], capture_output=True, text=True,
    ).stdout.strip()
    assert daemon_state == "active", f"{DAEMON_SYSTEM_UNIT} is not active right after install: {daemon_state!r}"

    daemon_pid = subprocess.run(
        ["systemctl", "show", DAEMON_SYSTEM_UNIT, "-p", "MainPID", "--value"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert daemon_pid and daemon_pid != "0", f"{DAEMON_SYSTEM_UNIT} is active but reports no MainPID"
    daemon_exe = _sudo_readlink(f"/proc/{daemon_pid}/exe")
    expected_daemon_exe = str(OPT_DIR / "PrivacyFenceApp")
    assert daemon_exe == expected_daemon_exe, f"{DAEMON_SYSTEM_UNIT} runs {daemon_exe!r}, not {expected_daemon_exe!r}"
    daemon_owner = subprocess.run(
        ["ps", "-o", "user=", "-p", daemon_pid], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert daemon_owner == "privacyfence", f"the daemon should run as the service account, not {daemon_owner!r}"

    # Not just "systemd thinks it's active" -- the daemon's own control
    # channel and mcp_token genuinely came up, moved to handoff/ exactly as
    # privilege_separation.py's own module docstring lays out.
    _wait_for_path_as_root(socket_path_under(HANDOFF_DIR), timeout=20, what="the separated daemon's control channel socket")
    _wait_for_path_as_root(HANDOFF_DIR / MCP_TOKEN_FILE_NAME, timeout=20, what="the separated daemon's mcp_token")

    # ── "Log in": same real systemd --user manager + xdg-desktop-autostart
    # .target dance as the unseparated test -- what differs here is which
    # unit it's supposed to pull in. ────────────────────────────────────
    user_env, systemctl_user = _start_real_login_session(user, uid)

    unit = COMPANION_AUTOSTART_UNIT_NAME
    unit_def = systemctl_user("cat", unit)
    assert "/usr/bin/privacyfence-companion --serve" in unit_def.stdout, (
        f"{unit} wasn't generated correctly from the companion's autostart entry:\n{unit_def.stdout}"
    )
    assert "PartOf=graphical-session.target" in unit_def.stdout

    wants = systemctl_user("show", "xdg-desktop-autostart.target", "-p", "Wants", "--value")
    assert unit in wants.stdout.split(), (
        f"{unit} is not pulled in by xdg-desktop-autostart.target -- a real desktop session "
        f"would never start it at login:\n{wants.stdout}"
    )

    # B24: the earlier `assert not AUTOSTART_DESKTOP_FILE.exists()` above only
    # proves stop_legacy_autostart() renamed the file -- it does not prove
    # systemd actually stopped autostarting it. systemd-xdg-autostart-
    # generator does not filter the autostart directories by filename, so
    # the *renamed* file was still turned into AUTOSTART_UNIT_NAME and pulled
    # into this same target -- a second daemon, started as this logged-in
    # user, that check_runtime_identity then refused to run as the wrong
    # account. This is the one assertion in this module that would actually
    # have caught that: not the file move, but whether the generator honours
    # it.
    assert AUTOSTART_UNIT_NAME not in wants.stdout.split(), (
        f"{AUTOSTART_UNIT_NAME} is still pulled in by xdg-desktop-autostart.target -- "
        f"stop_legacy_autostart()'s rename of {AUTOSTART_DESKTOP_FILE} did not stop systemd's "
        f"generator from autostarting the daemon's old entry under its renamed name:\n{wants.stdout}"
    )

    _trigger_graphical_session_target(user_env)

    _wait_for_unit_property(systemctl_user, unit, "ActiveState", "active", timeout=20)
    companion_pid = systemctl_user("show", unit, "-p", "MainPID", "--value").stdout.strip()
    assert companion_pid and companion_pid != "0", (
        f"{unit} is active but reports no MainPID:\n{systemctl_user('status', unit, check=False).stdout}"
    )
    exe_link = os.readlink(f"/proc/{companion_pid}/exe")
    assert exe_link == str(COMPANION_BIN), f"systemd started {exe_link!r}, not the packaged companion at {COMPANION_BIN!r}"
    companion_owner = subprocess.run(
        ["ps", "-o", "user=", "-p", companion_pid], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert companion_owner == user, f"the companion should run as {user!r} (the logged-in human), not {companion_owner!r}"

    # Functional proof, not just "systemd thinks it's active": the
    # companion's own CompanionChannelServer actually bound its socket --
    # the same rigor the unseparated test applies to the daemon's own
    # control.sock.
    _wait_for_path_as_root(
        companion_socket_path_under(HANDOFF_DIR), timeout=20, what="the companion's own control channel socket",
    )

    # ── Cleanup: unlike the daemon, the companion's --serve mode has no
    # in-product "quit" of its own (companion.py only ever asks the *daemon*
    # to quit) -- it only ever exits on SIGTERM, which is what a real
    # session teardown sends it. `systemctl --user stop` is the direct
    # equivalent, so this test's own generated unit doesn't outlive it into
    # the next test in this module. ──────────────────────────────────────
    systemctl_user("stop", unit, check=False)


# --------------------------------------------------------------------------- #
# Test 2 -- the unseparated path: still what a bare pip/pipx install gets
# today (nothing there ever runs `enable --auto`), and reachable from a
# .deb install by running `disable` -- the pre-D1 mechanism this module
# always tested, where the daemon's own XDG autostart entry starts the
# daemon directly in the login session, which then serves a real daemon/
# MCP/approval/audit round trip (Phase 3's own contract shape), and "Quit
# PrivacyFence" stops the real systemd unit, not just the process.
# --------------------------------------------------------------------------- #

@_needs_deb_login_session
async def test_deb_autostart_activates_daemon_via_real_login_session(_real_home_state):
    home = _real_home_state
    user = _current_user()
    uid = os.getuid()

    deb_path = _built_debs()[-1]
    port = _free_port()

    # Pre-seed the real $HOME's settings.yaml the same way _prepare_home
    # does for every other packaged/system test -- a real free port, update
    # checks off (this tier makes no real outbound network calls) -- but
    # against the *real* $HOME, before the daemon's own first boot, exactly
    # what a real first login would find already in place from an earlier
    # "Authenticate..." session.
    _prepare_home(home, port=port)

    # ── Install, then explicitly undo #428 D1's now-automatic privilege
    # separation -- this pins the pre-D1 mechanism, which is *also* still
    # exactly what a bare pip/pipx install gets today: nothing there ever
    # runs `enable --auto` at all, since that hook is debian/postinst's
    # alone. See the first test in this module for the new, separated-by-
    # default path a plain `.deb` install now takes if left alone. ───────
    _dpkg("-i", str(deb_path))
    subprocess.run(
        ["sudo", "-n", "privacyfence-privilege-separation", "disable", "--user", user],
        check=True, capture_output=True, text=True, timeout=30,
    )

    # ── Confirm the postinst's own contract (P3.3): a fresh install --
    # having reverted the auto-enabled privilege separation -- must never
    # itself start the daemon; only the *next* graphical login's XDG
    # autostart should ───────────────────────────────────────────────────
    time.sleep(1.0)
    assert not resolve_posix_socket_path(home / ".privacyfence").exists(), (
        "installing the .deb (and reverting the auto-enabled privilege separation) must never "
        "itself start the daemon -- only the next login should"
    )

    # ── "Log in": same real systemd --user manager + xdg-desktop-autostart
    # .target dance as the separated test above. ────────────────────────
    user_env, systemctl_user = _start_real_login_session(user, uid)

    unit = AUTOSTART_UNIT_NAME
    unit_def = systemctl_user("cat", unit)
    assert "/usr/bin/privacyfence-app" in unit_def.stdout, (
        f"{unit} wasn't generated correctly from the installed autostart entry:\n{unit_def.stdout}"
    )
    assert "PartOf=graphical-session.target" in unit_def.stdout

    wants = systemctl_user("show", "xdg-desktop-autostart.target", "-p", "Wants", "--value")
    assert unit in wants.stdout.split(), (
        f"{unit} is not pulled in by xdg-desktop-autostart.target -- a real desktop session "
        f"would never start it at login:\n{wants.stdout}"
    )

    _trigger_graphical_session_target(user_env)

    _wait_for_unit_property(systemctl_user, unit, "ActiveState", "active", timeout=20)
    main_pid = systemctl_user("show", unit, "-p", "MainPID", "--value").stdout.strip()
    assert main_pid and main_pid != "0", (
        f"{unit} is active but reports no MainPID:\n{systemctl_user('status', unit, check=False).stdout}"
    )
    exe_link = os.readlink(f"/proc/{main_pid}/exe")
    expected_exe = str(OPT_DIR / "PrivacyFenceApp")
    assert exe_link == expected_exe, f"systemd started {exe_link!r}, not the packaged binary at {expected_exe!r}"

    _wait_for_path(resolve_posix_socket_path(home / ".privacyfence"), timeout=20, what="control channel socket")
    mcp_token = _wait_for_path_content(home / ".privacyfence" / MCP_TOKEN_FILE_NAME, timeout=20)
    _wait_until_connectable("localhost", port)

    base_url = f"http://localhost:{port}"
    mcp_url = f"{base_url}/mcp"

    # ── Phase 3's own daemon/MCP/approval/audit contract shape, against a
    # daemon that this test never itself started a process for ───────────
    async with httpx.AsyncClient(base_url=base_url, follow_redirects=True) as web_client:
        session_id = await _bootstrap_session(web_client, home / ".privacyfence")
        assert (await web_client.get("/settings")).status_code == 200

        allow_task = asyncio.create_task(
            _propose_trusted_sender_rule(mcp_url, mcp_token, value=["autostart.example.com"])
        )
        await _resolve_pending_card(web_client, session_id, decision="confirm")
        allow_result = await allow_task
        assert allow_result.isError is not True, getattr(allow_result, "content", allow_result)
        assert allow_result.structuredContent["changed"] is True

        await _quit(web_client, session_id)

    # ── Graceful shutdown propagates back to systemd: the unit deactivates
    # and the real process is actually gone -- not just unreachable over
    # HTTP -- proving "Quit PrivacyFence" stops the real systemd-managed
    # service, not merely the daemon's own event loop. ────────────────────
    _wait_for_unit_property(systemctl_user, unit, "ActiveState", "inactive", timeout=20)
    assert not Path(f"/proc/{main_pid}").exists(), f"pid {main_pid} still alive after Quit PrivacyFence"

    settings_path = home / ".privacyfence" / "authority" / "config" / "settings.yaml"
    assert "autostart.example.com" in settings_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Test 3 -- where practical, the real (unmocked) OAuth loopback
# browser-opening flow, under a real Xvfb $DISPLAY.
# --------------------------------------------------------------------------- #

def _free_x_display() -> int:
    for candidate in range(50, 100):
        if not Path(f"/tmp/.X11-unix/X{candidate}").exists():
            return candidate
    raise RuntimeError("no free X display number found in :50-:99")


@contextlib.contextmanager
def _xvfb_display():
    display_num = _free_x_display()
    proc = subprocess.Popen(
        ["Xvfb", f":{display_num}", "-screen", "0", "1024x768x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        socket_path = Path(f"/tmp/.X11-unix/X{display_num}")
        _wait_for_path(socket_path, timeout=10, what="Xvfb's X11 socket")
        yield f":{display_num}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(not _systemd_is_init(), reason="not booted with systemd as PID 1 -- needs a real VM, not a container")
@pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb not on PATH (apt-get install xvfb)")
@pytest.mark.timeout(60)
def test_oauth_loopback_reaches_a_real_launched_browser_process(tmp_path, monkeypatch):
    import webbrowser

    from privacyfence.oauth_loopback import run_browser_oauth

    capture_file = tmp_path / "captured_urls.txt"
    script_path = tmp_path / "fake-browser.py"
    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    # A real executable, launched as a real OS subprocess by the stdlib's
    # own (unmocked) webbrowser.open() -- it performs the actual loopback
    # HTTP round trip itself, the same way a real browser hits redirect_uri
    # after a user finishes consent, just scripted instead of clicked.
    # Explicitly disables proxying for this one loopback call, same
    # reasoning as tests/platform/test_browser_launch_default.py's own
    # _NO_PROXY_SESSION: a redirect to 127.0.0.1 must never go through
    # whatever HTTP(S)_PROXY the runner's environment happens to set.
    script_path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from urllib.parse import urlparse, parse_qs\n"
        "from urllib.request import build_opener, ProxyHandler\n"
        "\n"
        "url = sys.argv[1]\n"
        f"with open({str(capture_file)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(url + '\\n')\n"
        "state = parse_qs(urlparse(url).query)['state'][0]\n"
        "opener = build_opener(ProxyHandler({}))\n"
        f"opener.open({redirect_uri!r} + '?code=auth-code-123&state=' + state, timeout=5)\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    with _xvfb_display() as display:
        monkeypatch.setenv("DISPLAY", display)
        monkeypatch.setenv("BROWSER", str(script_path))
        # webbrowser caches its browser-detection result process-wide the
        # first time anything calls webbrowser.get() -- force it to
        # re-detect now that $DISPLAY/$BROWSER describe this real
        # graphical session (see module docstring for why this is the one
        # gap test_browser_launch_default.py's own monkeypatch-webbrowser.
        # open approach leaves open).
        monkeypatch.setattr(webbrowser, "_tryorder", None)

        def build_authorize_url(uri: str, state: str, code_challenge: str) -> str:
            return f"https://provider.example/authorize?state={state}&redirect_uri={uri}"

        exchanged: dict = {}

        def exchange(code: str, uri: str, verifier: str) -> dict:
            exchanged.update(code=code, redirect_uri=uri, verifier=verifier)
            return {"access_token": "tok-123"}

        result = run_browser_oauth(build_authorize_url, exchange, port=port, timeout=15)

    assert exchanged.get("code") == "auth-code-123"
    assert result == {"access_token": "tok-123"}
    captured = capture_file.read_text(encoding="utf-8").strip().splitlines()
    assert captured and captured[0].startswith("https://provider.example/authorize?state="), captured
