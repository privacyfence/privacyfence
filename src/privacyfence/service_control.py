"""The companion as daemon manager (ADR 0026): start/stop/restart the separated daemon's system service, with a real
elevation prompt.

The counterpart to ``daemon_status.py`` -- see that module's own docstring
for why the two are split: status needs no privileges, and everything in
*this* module does. There is exactly one public entry point,
``run_elevated()``, and it always ends up running one of the platform
scripts' own ``daemon start``/``daemon stop``/``daemon restart``
subcommands (``scripts/macos_privilege_separation.sh``, ``scripts/
linux_privilege_separation.sh``, ``scripts/windows_privilege_separation.
ps1``) as root/admin -- never a raw ``launchctl``/``systemctl``/``sc.exe``
invocation from here. Those scripts already own the retry/backoff and
verification logic a real start needs (``ensure-running``'s wait-for-pid-
and-control-socket loop); duplicating even a piece of that in this module,
on top of an elevation prompt, would be the one place a fix to that logic
could silently miss.

**Elevation, not a raw ``sudo``.** Each platform's own interactive prompt --
macOS's ``osascript ... with administrator privileges``, Linux's
``pkexec``, Windows' UAC -- the same three ``privilege_separation.py``
already uses for ``enable --for-user`` (``_macos_admin_argv``/
``_pkexec_argv``/``_windows_runas_argv``, shared with this module rather
than reimplemented -- see that module's own comments on each). A companion
running in the user's own session cannot call ``sudo`` itself (nothing
would supply the password), and must not try: an askpass-less ``sudo``
from a backgrounded process just hangs.

**Only ever a script the installer shipped.** ``run_elevated()`` resolves
the platform script through ``privilege_separation.installer_script_path()``
and refuses to elevate to it unless ``_elevation_script_problem()`` finds it
root-owned and not group/world-writable (on Windows: not writable by
anything but SYSTEM/Administrators) -- the identical check ``complete_
per_user_separation()`` already applies before *its* elevation. Skipping
that check here would make this module a one-shot local privilege
escalation: an admin-password prompt that a compromised agent could aim at
a file it can still rewrite (ADR 0058).
"""
from __future__ import annotations

import contextlib
import logging
import shlex
import subprocess  # nosec B404  # argv built entirely from privilege_separation.py's own quoting helpers below
from typing import Literal

from . import privilege_separation

logger = logging.getLogger(__name__)

DaemonAction = Literal["start", "stop", "restart"]

_ACTION_PAST_TENSE: dict[DaemonAction, str] = {
    "start": "started", "stop": "stopped", "restart": "restarted",
}

#: How long a ``daemon start``/``stop``/``restart`` elevated run may take
#: end to end, prompt included -- longer than an ordinary command timeout on
#: purpose, since the clock does not start until a human has answered the
#: password dialog, and ``ensure-running``'s own retry loop (macOS's
#: bootstrap/bootout race) can itself take up to half a minute.
_ELEVATION_TIMEOUT_SECONDS = 120


def _cancelled(action: str, result: "subprocess.CompletedProcess[str]") -> bool:
    """Best-effort: was this a declined password prompt rather than a real
    failure of ``daemon <action>`` itself? Worth telling apart because the
    companion's own UI should say "cancelled" and stop, not show a
    stack-trace-shaped error for a human just clicking Cancel.

    macOS's ``osascript`` reports a declined admin prompt as AppleScript
    error -128 ("User canceled."); ``pkexec`` reserves exit code 126 for
    "authorization was not obtained" (dismissed dialog or wrong password,
    indistinguishable from here, which is fine -- both are "try again",
    never "run something else"). Windows has no equivalent signal to read:
    ``_windows_runas_argv``'s own launcher exits 1 both when UAC is declined
    and when the elevated script itself fails, so a declined UAC prompt on
    Windows falls through to the generic failure message below instead of
    this one -- a smaller loss than guessing wrong in the other direction.
    """
    platform = privilege_separation.current_platform()
    if platform == "darwin":
        return "-128" in (result.stderr or "") or "User canceled" in (result.stderr or "")
    if platform == "linux":
        return result.returncode == 126
    return False


def _unseparated_detail() -> str:
    """What Start/Restart/Stop says on an install with no system service.
    Every installer PrivacyFence ships sets one up, so this is an install
    that did not finish -- and the one thing worth saying is the command that
    finishes it, spelled with the script this install actually has."""
    script = privilege_separation.installer_script_path()
    if script is None:
        layout = privilege_separation.platform_layout()
        command = layout.enable_command if layout is not None else None
    elif privilege_separation.current_platform() == "win32":
        command = f'powershell -ExecutionPolicy Bypass -File "{script}" enable   (from an elevated PowerShell)'
    else:
        command = f"sudo {shlex.quote(str(script))} enable"
    detail = (
        "PrivacyFence's background service isn't set up on this machine yet, so there is nothing "
        "to start or stop -- the installer normally does this."
    )
    return f"{detail} To finish it, run: {command}" if command else detail


def run_elevated(action: DaemonAction) -> tuple[bool, str]:
    """Run this platform's ``daemon <action>`` elevated, and report whether
    it worked.

    Returns ``(True, detail)`` on success, ``(False, "cancelled")`` when a
    human visibly declined the password prompt (see ``_cancelled()``), and
    ``(False, detail)`` for every other failure -- never raises, since
    every caller is a tray click or a CLI action with nothing useful to
    propagate an exception to, the same posture ``privilege_separation.
    complete_per_user_separation()`` already takes for the elevation this
    shares its argv builders with.
    """
    # A marker this process cannot read is a separated install whose root
    # mode has drifted (privilege_separation.shared_dir_mode()), not an
    # unseparated one: the service is there, and the daemon a restart starts
    # puts the mode back.
    if not privilege_separation.is_enabled() and not privilege_separation.marker_unreadable():
        return False, _unseparated_detail()
    script = privilege_separation.installer_script_path()
    if script is None:
        return False, "This install has no provisioning script to run -- see docs/platform-support.md."
    problem = privilege_separation._elevation_script_problem(script)  # noqa: SLF001 -- shared on purpose, see module docstring
    if problem is not None:
        logger.warning("refusing to elevate to %s: %s", script, problem)
        return False, f"could not verify {script} is safe to run as an administrator: {problem}"

    platform = privilege_separation.current_platform()
    with contextlib.ExitStack() as stack:
        transcript = stack.enter_context(privilege_separation._elevation_transcript())  # noqa: SLF001
        if platform == "darwin":
            command = f"{shlex.quote(str(script))} daemon {action}"
            argv = privilege_separation._macos_admin_argv(  # noqa: SLF001
                command,
                prompt=f"PrivacyFence needs an administrator password to {action} its background service.",
            )
        elif platform == "win32":
            argv = privilege_separation._windows_runas_argv(script, f"daemon {action}", transcript)  # noqa: SLF001
        else:
            pkexec_argv = privilege_separation._pkexec_argv(script, "daemon", action)  # noqa: SLF001
            if pkexec_argv is None:
                return False, (
                    "No pkexec is available to ask for a password from here. Run by hand: "
                    f"sudo {shlex.quote(str(script))} daemon {action}"
                )
            argv = pkexec_argv
        try:
            result = subprocess.run(  # nosec B603  # fixed argv built above, no shell, quoted per layer
                argv, capture_output=True, text=True, timeout=_ELEVATION_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("could not %s PrivacyFence's service", action, exc_info=True)
            return False, f"could not {action} PrivacyFence's service: {exc}"
        detail = privilege_separation._elevation_detail(result, transcript)  # noqa: SLF001

    if result.returncode != 0:
        if _cancelled(action, result):
            return False, "cancelled"
        logger.info("daemon %s did not complete: %s", action, detail)
        return False, detail
    return True, f"PrivacyFence's background service was {_ACTION_PAST_TENSE[action]}."
