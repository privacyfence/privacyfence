"""#428 Phase 4 (B5c): hosting the daemon as a real Windows service.

macOS and Linux had a service manager to hand: B5a swapped a LaunchAgent for
a LaunchDaemon, B5b a ``--user`` systemd unit for a system one, and in both
cases the thing being started was the same unchanged executable, started by
a different manager under a different account. Windows cannot do that. Its
Service Control Manager does not merely *launch* a binary -- it launches it
and then waits for that process to call ``StartServiceCtrlDispatcher`` and
report a status back, and a process that never does is killed after 30
seconds with error 1053 ("the service did not respond to the start request
in a timely fashion"). Pointing ``sc create``'s ``binPath`` straight at
``privacyfence-app.exe`` produces exactly that, every time.

So the Windows leg of Phase 4 needs one thing its POSIX siblings did not: a
service *host*. This module is it, and it is deliberately the thinnest one
that can work -- it starts nothing of its own, owns no state, and makes no
decision the ordinary entry point does not already make:

    SvcDoRun  -> daemon_main.main([])      (the same call ``privacyfence-app`` makes)
    SvcStop   -> daemon_main.request_shutdown()  (what the web UI's Quit button calls)

``request_shutdown()`` is the same signal ``settings_controller.quit_app()``
and the control channel's ``QUIT`` command already send, so a service stop
takes the identical shutdown path a human clicking Quit does, rather than a
second one that would have to be kept working alongside it.

**No new dependency.** ``pywin32`` is already required on Windows for the
Phase 2 named pipes and arrives transitively through ``mcp`` besides, and
``win32serviceutil.ServiceFramework`` is part of it. ADR 0002 decision 4's
budget is about the *companion*; this is the daemon, whose dependency set
that decision says must not change, and it does not.

**Frozen builds are the real deployment.** ``scripts/windows_privilege_
separation.ps1`` registers the service as ``<installed exe> --windows-
service``, and ``daemon_main.main()`` routes that flag here (see
``parse_args``). A PyInstaller executable can host a service this way
because ``servicemanager.PrepareToHostSingle()`` exists precisely for a
process that *is* the service rather than one hosted by pywin32's own
``PythonService.exe`` -- which a frozen build has no copy of. The same flag
works from a source checkout, which is what makes this testable at all.
"""
from __future__ import annotations

import logging
import sys

# What ``sc.exe create`` is told, and what Windows turns into the virtual
# account ``NT SERVICE\PrivacyFence``. Imported from privilege_separation
# rather than spelled again here: Windows derives the account name from the
# service name, so these are one fact and a drift between them would produce
# a service running as an account no ACL on disk mentions.
from .privilege_separation import WINDOWS_SERVICE_NAME

logger = logging.getLogger(__name__)

SERVICE_DISPLAY_NAME = "PrivacyFence"
SERVICE_DESCRIPTION = (
    "Runs the PrivacyFence approval daemon under its own account, so the AI agent it "
    "governs cannot read its session-minting channel, rewrite its policy, forge a passkey "
    "or read its audit key (issue #428 Phase 4)."
)


def _service_class():  # noqa: ANN202 -- the base class only exists on Windows
    """Build the ``ServiceFramework`` subclass lazily.

    A module-level ``class PrivacyFenceService(win32serviceutil.Service
    Framework)`` would make importing this module require pywin32 -- and
    therefore require Windows -- which would put a platform import in the
    daemon's own import graph for a code path only a service ever reaches.
    Every test that wants to check what this module *does* can then only run
    on Windows. Built here instead, so the module imports everywhere and
    only ``run_service()`` needs the real thing.
    """
    import servicemanager
    import win32service
    import win32serviceutil

    # daemon_main is imported inside the two methods rather than here: this
    # runs before StartServiceCtrlDispatcher (run_service() below), the SCM
    # gives that call 30 seconds, and importing daemon_main -- every connector
    # and its client library -- is not needed to make it. By SvcDoRun the
    # framework has already reported SERVICE_RUNNING.
    class PrivacyFenceService(win32serviceutil.ServiceFramework):
        _svc_name_ = WINDOWS_SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def SvcStop(self) -> None:  # noqa: N802 -- the framework's own name
            # Reported *before* the shutdown signal, not after: the SCM
            # gives a service 30 seconds to acknowledge a stop, and
            # run_app()'s teardown (web server, connector threads, the
            # instance lock) can legitimately take longer than that on a
            # busy install. Reporting STOP_PENDING first is what buys that
            # time rather than having the SCM kill the process mid-teardown
            # and leave a stale lock file behind.
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            from . import daemon_main

            daemon_main.request_shutdown()

        def SvcDoRun(self) -> None:  # noqa: N802 -- the framework's own name
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            # Runs on this thread and blocks until SvcStop above (or a
            # fatal startup error) ends it. No worker thread: the SCM has
            # already been told the service is running by the framework's
            # own start sequence, and threading the real work would only
            # add a second place for an exception to be lost.
            from . import daemon_main

            code = daemon_main.main([])
            if code:
                servicemanager.LogErrorMsg(startup_failure_message(code, daemon_main.last_startup_error()))

    return PrivacyFenceService


def startup_failure_message(code: int, reason: str | None) -> str:
    """The Event Log text for a service start that ``daemon_main.main()`` refused.

    The one place a service has to say what a terminal would have printed to
    stderr: a service has no stderr, and the refusals that matter most -- an
    unreadable or rejected settings.yaml, check_runtime_identity() finding the
    wrong account -- happen before the daemon has a log file to write to. So
    the reason goes here, where ``Get-WinEvent`` and Event Viewer show it.
    """
    if reason:
        return f"PrivacyFence exited with status {code}: {reason}"
    return (
        f"PrivacyFence exited with status {code}; see the daemon's own log under "
        "%ProgramData%\\PrivacyFence for the reason."
    )


def run_service() -> int:
    """Hand this process to the Service Control Manager.

    Only ever reached through ``privacyfence-app --windows-service``, which
    is what the installer registers as the service's ``binPath``. Running it
    from a shell does not start the daemon: ``StartServiceCtrlDispatcher``
    fails with ``ERROR_FAILED_SERVICE_CONTROLLER_CONNECT`` (1063) when the
    caller is not the SCM, which this turns into a message saying so rather
    than a pywin32 traceback -- a plausible thing for someone to try once
    after reading the service's ``binPath`` out of ``sc qc``.
    """
    import servicemanager
    import winerror

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(_service_class())
    try:
        servicemanager.StartServiceCtrlDispatcher()
    except Exception as exc:
        if getattr(exc, "winerror", None) == winerror.ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            print(
                "--windows-service is how the Service Control Manager starts PrivacyFence; it "
                f"does nothing when run by hand. Use 'sc.exe start {WINDOWS_SERVICE_NAME}' (or "
                "run privacyfence-app with no arguments for an ordinary foreground daemon).",
                file=sys.stderr,
            )
            return 1
        raise
    return 0


__all__ = [
    "SERVICE_DESCRIPTION",
    "SERVICE_DISPLAY_NAME",
    "WINDOWS_SERVICE_NAME",
    "run_service",
    "startup_failure_message",
]
