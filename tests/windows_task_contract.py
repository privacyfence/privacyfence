"""The Windows companion sign-in task definition's contract, in one place.

``installer/windows/privacyfence-companion-task.xml.tmpl`` is registered by
``scripts/windows_privilege_separation.ps1``'s ``enable`` (``schtasks /create
/xml``) and is the only Scheduled Task a PrivacyFence install has: the daemon
is a Windows service. The daemon's own sign-in task that came before it
shipped broken more than once -- a trigger scoped to the installing account,
a missing ``version="1.2"`` that silently dropped the schema-1.2 Settings
elements, a ``Principal``/``Actions`` ``id``/``Context`` pair whose absence
left the group principal bound to nothing that runs -- and every one of those
registered without complaint, so "the task exists" never caught any of them.
That task is gone (it and its template are in git history); the lessons are
kept here, against the task that remains.

This module states what the definition has to say, so two very different
tests can assert the same thing about two different documents:

* ``tests/unit/test_privilege_separation.py`` checks the template this repo
  ships, on every PR, on any OS -- a regression here is caught in seconds
  instead of by a scheduled Windows-only workflow.
* ``tests/integration/test_windows_graphical_session_autostart.py`` checks
  what Task Scheduler itself stored after a real install (``schtasks /query
  /xml``), which is the document that actually governs, and which can differ
  from the template the service was handed.
"""
from __future__ import annotations

import defusedxml.ElementTree as ET

# The task-definition schema namespace every element in a task XML lives
# under (the companion template's own xmlns).
TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"

# The companion template's own placeholder for the installed
# PrivacyFenceCompanion.exe path, substituted by Install-CompanionTask.
EXEC_PATH_PLACEHOLDER = "__EXEC_PATH__"

# What `Builtin\Users` becomes once Task Scheduler has stored the task.
BUILTIN_USERS_SID = "S-1-5-32-545"


def assert_task_xml_matches_companion_contract(xml_text: str, *, exec_path: str) -> None:
    """Assert *xml_text* is a task definition that starts the companion
    (*exec_path*) for whichever user signs in, unelevated.

    Two choices differ from the daemon sign-in task this replaced, and are
    decisions rather than omissions:

    * **No ``TimeTrigger``.** The daemon's repeating trigger was its
      crash-restart mechanism and was harmless there because the
      single-instance lock made every redundant tick exit at once. The
      companion has no such lock, so the same trigger would stack up one
      tray icon per five minutes, forever. The daemon's crash-restart is the
      service's own ``sc failure`` configuration.
    * **``IgnoreNew``, not ``Parallel``.** Same reason. Task Scheduler
      scopes that policy per task instance and each user's logon produces
      its own, so this stays correct for several signed-in users at once.

    Everything else -- schema 1.2, the unscoped ``LogonTrigger``, the
    ``Principal``/``Actions`` id pair, ``LeastPrivilege``, both inverted
    battery defaults, ``RestartOnFailure`` -- was a real shipped bug on the
    daemon's task and is asserted here for that reason.
    """
    root = ET.fromstring(xml_text)
    context = f"---- companion task XML ----\n{xml_text}"

    assert root.get("version") == "1.2", f"<Task> does not declare schema version 1.2\n{context}"

    triggers = root.find(f"{TASK_NS}Triggers")
    assert triggers is not None, f"no <Triggers>\n{context}"
    logon_triggers = triggers.findall(f"{TASK_NS}LogonTrigger")
    assert len(logon_triggers) == 1, f"expected exactly one <LogonTrigger>\n{context}"
    logon_trigger = logon_triggers[0]
    enabled = logon_trigger.findtext(f"{TASK_NS}Enabled")
    assert enabled is None or enabled.strip().lower() == "true", f"<LogonTrigger> is disabled\n{context}"
    assert logon_trigger.find(f"{TASK_NS}UserId") is None, (
        f"<LogonTrigger> is scoped to one account; it must fire for any interactive logon\n{context}"
    )
    assert triggers.find(f"{TASK_NS}TimeTrigger") is None, (
        f"the companion task carries a <TimeTrigger>: with no single-instance lock in the "
        f"companion, a repeating trigger spawns one tray icon per tick\n{context}"
    )

    principals = root.find(f"{TASK_NS}Principals")
    assert principals is not None, f"no <Principals>\n{context}"
    principal = principals.find(f"{TASK_NS}Principal")
    assert principal is not None, f"no <Principal>\n{context}"
    group_id = (principal.findtext(f"{TASK_NS}GroupId") or "").strip()
    assert group_id.lower().endswith("users") or group_id.upper() == BUILTIN_USERS_SID, (
        f"principal is {group_id!r}, not the built-in Users group -- the companion would only ever "
        f"run for one account\n{context}"
    )
    run_level = (principal.findtext(f"{TASK_NS}RunLevel") or "LeastPrivilege").strip()
    # Not merely the default: the companion is deliberately on the *agent's*
    # side of the privilege-separation trust boundary (ADR 0003), and an
    # elevated one would be able to reach the authority directory that
    # boundary exists to take away.
    assert run_level == "LeastPrivilege", f"unexpected RunLevel {run_level!r}\n{context}"

    actions = root.find(f"{TASK_NS}Actions")
    assert actions is not None, f"no <Actions>\n{context}"
    principal_id = principal.get("id")
    assert principal_id, f"<Principal> carries no id for <Actions> to name\n{context}"
    assert actions.get("Context") == principal_id, (
        f"<Actions Context={actions.get('Context')!r}> does not name the principal id "
        f"{principal_id!r}\n{context}"
    )
    command = (actions.findtext(f"{TASK_NS}Exec/{TASK_NS}Command") or "").strip().strip('"')
    assert command.lower() == exec_path.lower(), (
        f"task action runs {command!r}, not {exec_path!r}\n{context}"
    )

    settings = root.find(f"{TASK_NS}Settings")
    assert settings is not None, f"no <Settings>\n{context}"
    assert (settings.findtext(f"{TASK_NS}Enabled") or "true").strip().lower() == "true", (
        f"the task itself is registered disabled\n{context}"
    )
    assert (settings.findtext(f"{TASK_NS}MultipleInstancesPolicy") or "").strip() == "IgnoreNew", (
        f"the companion task is not IgnoreNew: a second trigger in one session would start a "
        f"second tray icon\n{context}"
    )
    assert (settings.findtext(f"{TASK_NS}DisallowStartIfOnBatteries") or "").strip() == "false", (
        f"DisallowStartIfOnBatteries is not false: the companion would not start on battery power\n{context}"
    )
    assert (settings.findtext(f"{TASK_NS}StopIfGoingOnBatteries") or "").strip() == "false", (
        f"StopIfGoingOnBatteries is not false: the companion would be stopped when the machine "
        f"unplugs\n{context}"
    )
    restart = settings.find(f"{TASK_NS}RestartOnFailure")
    assert restart is not None, (
        f"no <RestartOnFailure>: a launch failure at sign-in would leave the user with no way into "
        f"the web UI until their next one\n{context}"
    )
    assert (restart.findtext(f"{TASK_NS}Interval") or "").strip() == "PT1M", context
    assert (restart.findtext(f"{TASK_NS}Count") or "").strip() == "3", context
