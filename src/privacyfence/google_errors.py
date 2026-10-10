"""Turns a Google "not found / not shared / no permission" answer into a
message the AI client may see.

Every Google connector's ``_fetch`` re-raises its ``*ClientError`` as a bare
``RuntimeError``, which ``safe_errors.public_message()`` deliberately turns
into the generic "Tool call failed" text, because that error wraps Google's
own response text. For the handful of answers that only mean "the connected
account can't reach the ID you gave me" that leaves the agent (and the user)
guessing. ``unavailable_error()`` recognises those answers off the original
``googleapiclient.errors.HttpError`` in the ``__cause__`` chain and returns
a ``GoogleResourceUnavailableError`` instead.

Why it is safe to show: the message is written entirely here. It reveals only
that the connected account cannot reach an ID the caller supplied, plus that
account's own address (when it looks like one). No Google response text is
copied into it.

See docs/adr/0106-google-not-found-and-not-shared-errors-reach-the-agent.md.
"""

from __future__ import annotations

import json
import re

from googleapiclient.errors import HttpError

NOT_FOUND = "not_found"
NO_PERMISSION = "no_permission"

_MAX_CAUSE_DEPTH = 5
_ADDRESS = re.compile(r"[^@\s()]+@[^@\s()]+\.[^@\s()]+")


class GoogleResourceUnavailableError(RuntimeError):
    """A Google API said an item does not exist, is not shared with the
    connected account, or that account lacks permission on it.

    A named ``RuntimeError`` subclass on purpose: ``safe_errors.public_message()``
    passes it through (its text is written in this module), and every
    best-effort ``except RuntimeError:`` fallback in the connectors still
    catches it."""


def _find_http_error(exc: BaseException) -> HttpError | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None or id(current) in seen:
            return None
        if isinstance(current, HttpError):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None


def _error_body(http_error: HttpError) -> dict:
    try:
        content = http_error.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        result = json.loads(content)
    except Exception:  # noqa: BLE001 - any unparseable body is just "no details"
        return {}
    if not isinstance(result, dict):
        return {}
    body = result.get("error")
    return body if isinstance(body, dict) else {}


def classify_http_error(exc: BaseException) -> str | None:
    """``NOT_FOUND`` / ``NO_PERMISSION`` when ``exc`` (or a ``__cause__`` up to
    five steps down) is a Google ``HttpError`` with one of the recognised
    shapes, else ``None``. Every other 403 stays unclassified on purpose: it
    is also Google's answer for rate limits, a missing OAuth scope, domain
    policy and quota, none of which mean "not shared"."""
    http_error = _find_http_error(exc)
    if http_error is None:
        return None
    raw = getattr(getattr(http_error, "resp", None), "status", None)
    if raw is None:
        return None
    try:
        status = int(raw)
    except (TypeError, ValueError):
        return None
    body = _error_body(http_error)
    reasons: set[str] = set()
    for key in ("errors", "details"):
        items = body.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("reason"), str):
                    reasons.add(item["reason"])

    if status == 404:
        return NOT_FOUND
    if status == 410 and "deleted" in reasons:
        return NOT_FOUND
    if status == 403 and reasons & {"insufficientFilePermissions", "requiredAccessLevel"}:
        return NO_PERMISSION
    if (
        status == 403
        and body.get("status") == "PERMISSION_DENIED"
        and body.get("message") == "The caller does not have permission"
    ):
        return NO_PERMISSION
    return None


# (NOT_FOUND, NO_PERMISSION) per connector.
_MESSAGES: dict[str, tuple[str, str]] = {
    "drive": (
        "Google Drive says this file does not exist or is not shared with {who}. "
        "Check the link or file ID, or share the file with that account.",
        "Google Drive says {who} does not have permission for this file. "
        "Ask the file's owner to share it with that account, with edit access if this was a change.",
    ),
    "gmail": (
        "Gmail says this message, thread, draft, label or attachment does not exist "
        "in the mailbox of {who}. It may have been deleted, or the ID may belong to a different account.",
        "Gmail says {who} does not have permission for this item.",
    ),
    "calendar": (
        "Google Calendar says this event or calendar does not exist or is not shared with {who}. "
        "It may have been deleted, or the ID may belong to a different account.",
        "Google Calendar says {who} does not have permission for this event or calendar. "
        "Ask the calendar's owner to share it with that account, "
        'with "Make changes to events" if this was a change.',
    ),
    "contacts": (
        "Google Contacts says this contact does not exist for {who}. "
        "It may have been deleted, or the ID may belong to a different account.",
        "Google Contacts says {who} does not have permission for this contact.",
    ),
    "tasks": (
        "Google Tasks says this task or task list does not exist for {who}. "
        "It may have been deleted, or the ID may belong to a different account.",
        "Google Tasks says {who} does not have permission for this task or task list.",
    ),
    "apps_script": (
        "Google Apps Script says this project does not exist or is not shared with {who}. "
        "Check the script ID, or share the project with that account.",
        "Google Apps Script says {who} does not have permission for this project. "
        "Ask the project's owner to share it with that account, with edit access if this was a change.",
    ),
}


def unavailable_error(
    connector: str, exc: BaseException, account: str
) -> GoogleResourceUnavailableError | None:
    """A ``GoogleResourceUnavailableError`` for ``exc`` when it is a
    recognised not-found / no-permission answer, else ``None``. ``account`` is
    named in the message only when it looks like an address (Contacts and Tasks
    report a summary string, and Google sometimes omits it: ``"unknown"``)."""
    kind = classify_http_error(exc)
    if kind is None:
        return None
    who = (
        f"the connected Google account ({account})"
        if _ADDRESS.fullmatch(account or "")
        else "the connected Google account"
    )
    template = _MESSAGES[connector][0 if kind == NOT_FOUND else 1]
    return GoogleResourceUnavailableError(template.format(who=who))
