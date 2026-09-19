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
   remove (dpkg's own conffile contract); ``$HOME``'s state is untouched
   (P2.2's "the package never reaches into `$HOME`" contract).
5. **Purge** (``dpkg -P``): the conffile is now gone too; ``$HOME`` is still
   untouched.
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

Scenarios 1-6 above are deliberately the pre-D1, unseparated lifecycle: issue
#428 D1 made ``debian/postinst`` provision privilege separation on every
install *and* upgrade, and ADR 0003 decision 5 made its machine half
unconditional (the per-user half still runs only when ``$SUDO_USER`` resolves
to a real, non-root account -- true for this module's own
passwordless-``sudo`` CI account, same as a real human's ``sudo dpkg -i``).
Left alone, that would move the daemon to its own system account/unit and
rename away the autostart entry this module validates, neither of which those
scenarios are testing (see ``test_linux_graphical_session_autostart.py`` for
the separated-by-default path instead).
``_disable_auto_enabled_privilege_separation()`` undoes it right after each
``_dpkg("-i", ...)`` call, pinning the mechanism those scenarios have always
tested -- still exactly what a bare ``pip``/``pipx`` install gets today, and
still reachable from a ``.deb`` install by running ``disable``.

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
import contextlib
import getpass
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
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

MCP_TOKEN_FILE_NAME = "mcp_token"


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


def _purge_if_present() -> None:
    """Best-effort cleanup -- covers both "fully installed" (a fresh
    ``dpkg -P`` needed) and "removed but not purged" (conffiles still on
    disk from a previous run's remove step), so a test that fails partway
    through never leaves the runner with this package in either state."""
    status = subprocess.run(["dpkg-query", "-W", "-f=${Status}", PACKAGE_NAME], capture_output=True, text=True)
    if status.returncode == 0 and status.stdout.strip() not in ("", "unknown ok not-installed"):
        _dpkg("-P", PACKAGE_NAME, check=False)


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
# Daemon process lifecycle -- same isolated-$HOME technique as
# test_macos_packaged_smoke.py's own running_packaged_daemon fixture, adapted
# to the .deb's installed binary instead of the DMG's frozen .app bundle.
# --------------------------------------------------------------------------- #

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


def _wait_for_path(path: Path, proc: subprocess.Popen, log_path: Path, timeout: float = 20.0) -> None:
    """Like ``_wait_for_file()`` but for a path with no meaningful text
    content of its own -- the control channel's Unix domain socket, in
    particular, whose ``read_text()`` wouldn't return anything sensible."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"daemon exited early (code {proc.poll()}) instead of starting -- log:\n"
                f"{log_path.read_text(errors='replace')}"
            )
        if path.exists():
            return
        time.sleep(0.1)
    raise AssertionError(f"{path} never appeared within {timeout}s -- log:\n{log_path.read_text(errors='replace')}")


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
        self.data_dir = home / ".privacyfence"
        self.port = port
        self.base_url = f"http://localhost:{port}"
        self.mcp_url = f"{self.base_url}/mcp"
        self.mcp_token = mcp_token


def _prepare_home(home: Path, *, port: int) -> None:
    """Pre-seeds (or re-seeds only the harness-convenience bits of) an
    isolated ``$HOME``'s ``settings.yaml``: a real free port (so repeated
    boots in this module never collide with each other or anything else on
    the runner) and update checks disabled (this tier makes no real outbound
    network calls). If ``settings.yaml`` already exists -- a second boot
    against a ``$HOME`` a previous boot in this same test already used --
    its existing content (e.g. an auto-accept rule the app itself applied)
    is loaded and only those two fields are overwritten, never replaced
    wholesale: overwriting it every boot would silently defeat the very
    state-survival assertions this module exists to make."""
    # #428 Phase 1: settings.yaml lives under an authority/ subdirectory of
    # data_dir(), same as the control channel's socket/the audit dir below --
    # not data_dir() itself.
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


@contextlib.contextmanager
def _running_daemon(home: Path):
    """Starts the real installed ``/usr/bin/privacyfence-app`` wrapper
    (not the PyInstaller onedir binary directly -- proving the wrapper
    script itself resolves and execs correctly is part of what this module
    is for) with an isolated ``$HOME``, and always terminates it on the way
    out."""
    assert DAEMON_BIN.is_file(), f"{DAEMON_BIN} missing -- was the package actually installed?"
    port = _free_port()
    _prepare_home(home, port=port)
    env = {**os.environ, "HOME": str(home)}
    log_path = home / "daemon.log"
    with open(log_path, "wb") as log_fh:
        proc = subprocess.Popen([str(DAEMON_BIN)], env=env, stdout=log_fh, stderr=subprocess.STDOUT)
        try:
            _wait_until_connectable("localhost", port)
            data_dir = home / ".privacyfence"
            _wait_for_path(resolve_posix_socket_path(data_dir), proc, log_path)
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


# --------------------------------------------------------------------------- #
# The daemon → MCP → approval → audit round trip -- Phase 3's own shape
# (bootstrap session, tools/list, a gated call resolved through the real HTTP
# decide route, audit log confirms the decision), against the one built-in
# meta-tool that needs no connector (see module docstring's point 3).
# --------------------------------------------------------------------------- #

async def _bootstrap_session(web_client: httpx.AsyncClient, data_dir: Path, *, path: str = "/settings") -> str:
    # #428 Phase 2: minted through the control channel (a real Unix domain
    # socket against this daemon's own data directory), not a bearer-
    # authenticated HTTP route -- see tests.control_channel_client's own
    # module docstring.
    code = mint_bootstrap_code_posix(resolve_posix_socket_path(data_dir))
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
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(mcp_url, http_client=http_client) as (read, write, _sid):
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

        session_id = await _bootstrap_session(web_client, daemon.data_dir)
        assert (await web_client.get("/settings")).status_code == 200

        # -- tools/list: the real MCP surface, no connector configured -----
        headers = {"Authorization": f"Bearer {daemon.mcp_token}"}
        async with httpx.AsyncClient(headers=headers) as http_client:
            async with streamable_http_client(daemon.mcp_url, http_client=http_client) as (read, write, _sid):
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
        assert allow_result.isError is not True, getattr(allow_result, "content", allow_result)
        assert allow_result.structuredContent["confirmed"] is True
        assert allow_result.structuredContent["changed"] is True
        assert "trusted_sender_domain" in allow_result.structuredContent["description"]

        # -- Deny round trip ----------------------------------------------------
        deny_task = asyncio.create_task(
            _propose_trusted_sender_rule(daemon.mcp_url, daemon.mcp_token, value=["denied.example.com"])
        )
        await _resolve_pending_card(web_client, session_id, decision="cancel")
        deny_result = await deny_task
        assert deny_result.isError is True

        # -- Audit log confirms both real decisions ------------------------------
        audit_dir = daemon.home / ".privacyfence" / "authority" / "logs" / "audit"
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

        # Same cross-platform-suite permission assertion Phase 3's own
        # scenario adds -- this module always runs on Linux (pytestmark
        # above), so no Windows skip needed here.
        assert audit_dir.stat().st_mode & 0o077 == 0

        # -- Graceful shutdown via the real "Quit PrivacyFence" action -----------
        await _quit(web_client, session_id)

    exit_code = daemon.process.wait(timeout=15)
    assert exit_code == 0, (
        f"daemon did not exit cleanly (code {exit_code}) -- log:\n"
        f"{(daemon.home / 'daemon.log').read_text(errors='replace')}"
    )


# --------------------------------------------------------------------------- #
# Test 1 -- P7.1: install / validate / start+scenario / remove / purge
# --------------------------------------------------------------------------- #

async def test_deb_install_validate_scenario_remove_purge_lifecycle(tmp_path):
    deb_path = _built_debs()[-1]
    home = tmp_path / "home"
    home.mkdir()

    # ── Install ──────────────────────────────────────────────────────────
    _dpkg("-i", str(deb_path))
    _disable_auto_enabled_privilege_separation()

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

    # ── Start the real installed daemon; run the Phase 3 scenario ────────
    with _running_daemon(home) as daemon:
        await _run_daemon_mcp_approval_audit_scenario(daemon)

    settings_path = home / ".privacyfence" / "authority" / "config" / "settings.yaml"
    assert "allowed.example.com" in settings_path.read_text(encoding="utf-8")

    # ── Remove (P7.1): package files gone; the autostart .desktop is a
    # conffile and survives a plain remove; $HOME is untouched ───────────
    _dpkg("-r", PACKAGE_NAME)
    assert not OPT_DIR.exists(), f"{OPT_DIR} should be gone after dpkg -r"
    assert not DAEMON_BIN.exists(), f"{DAEMON_BIN} should be gone after dpkg -r"
    assert AUTOSTART_DESKTOP_FILE.exists(), "a conffile must survive a plain `dpkg -r` (only purge removes it)"
    assert settings_path.exists(), "dpkg -r must never touch $HOME (P2.2)"

    remove_status = subprocess.run(["dpkg", "-s", PACKAGE_NAME], capture_output=True, text=True)
    assert "Status: deinstall ok config-files" in remove_status.stdout

    # ── Purge (P7.1): the conffile is now gone too; $HOME still untouched ──
    _dpkg("-P", PACKAGE_NAME)
    assert not AUTOSTART_DESKTOP_FILE.exists(), "dpkg -P must remove the conffile"
    purge_status = subprocess.run(["dpkg", "-s", PACKAGE_NAME], capture_output=True, text=True)
    assert purge_status.returncode != 0, f"package should be unknown to dpkg after purge:\n{purge_status.stdout}"
    assert settings_path.exists(), "dpkg -P must never touch $HOME (P2.2)"


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
    home = tmp_path / "home"
    home.mkdir()

    # ── Install version N; create real on-disk state the app itself
    # applied (an auto-accept rule confirmed through the real MCP/approval
    # round trip -- not a hand-written settings.yaml) ────────────────────
    _dpkg("-i", str(deb_n))
    _disable_auto_enabled_privilege_separation()
    with _running_daemon(home) as daemon:
        async with httpx.AsyncClient(base_url=daemon.base_url, follow_redirects=True) as web_client:
            session_id = await _bootstrap_session(web_client, daemon.data_dir)
            propose_task = asyncio.create_task(
                _propose_trusted_sender_rule(daemon.mcp_url, daemon.mcp_token, value=["preupgrade.example.com"])
            )
            await _resolve_pending_card(web_client, session_id, decision="confirm")
            result = await propose_task
            assert result.isError is not True, getattr(result, "content", result)
            assert result.structuredContent["changed"] is True

            await _quit(web_client, session_id)
        assert daemon.process.wait(timeout=15) == 0

    settings_path = home / ".privacyfence" / "authority" / "config" / "settings.yaml"
    assert "preupgrade.example.com" in settings_path.read_text(encoding="utf-8")

    # ── Install a synthetically-bumped version N+1 over it (P7.3) ────────
    deb_n1 = tmp_path / "upgrade-build" / "privacyfence_next.deb"
    new_version = _synthetic_next_version_deb(deb_n, deb_n1)
    _dpkg("-i", str(deb_n1))
    _disable_auto_enabled_privilege_separation()

    status = subprocess.run(["dpkg", "-s", PACKAGE_NAME], capture_output=True, text=True, check=True)
    assert f"Version: {new_version}" in status.stdout

    # ── State survived the upgrade untouched (P2.2/P7.3) ──────────────────
    assert "preupgrade.example.com" in settings_path.read_text(encoding="utf-8")

    # ── The upgraded binary still starts and serves, without clobbering
    # the state it just inherited ─────────────────────────────────────────
    with _running_daemon(home) as daemon:
        async with httpx.AsyncClient(base_url=daemon.base_url, follow_redirects=True) as web_client:
            session_id = await _bootstrap_session(web_client, daemon.data_dir)
            assert (await web_client.get("/settings")).status_code == 200
            await _quit(web_client, session_id)
        assert daemon.process.wait(timeout=15) == 0

    assert "preupgrade.example.com" in settings_path.read_text(encoding="utf-8")


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
