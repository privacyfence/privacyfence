"""Shape checks for the resource ids an auto-accept rule is created with.

A rule's value (a Drive folder id, a Slack channel id, a Jira project key, ...) is later compared
verbatim by the selectors in ``policy.scopes``, so a title or a pasted URL saved in its place is a
rule that can never match. ``normalise_rule_value`` refuses such a value when the rule is created,
with a message saying which id to enter. It is pure: no network and no connector classes, only the
regular expressions the clients already use for their own ids.

The checks cover character set and shape, deliberately with no minimum lengths. Known limit: a
one-word title with no spaces or punctuation (``Marketing``, ``Engineering``) is shaped like a key
or an id and is not caught here; the Settings page marks it ``invalid`` after a live lookup.
Checking that the id exists at write time is not done on purpose: the resolvers in
``policy.resource_registry`` return ``None`` both for "not found" and "network down", so it would
block offline setup.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..assets_client import _ID_RE as _ASSETS_ID_RE
from ..grist_client import DOC_ID_RE as _GRIST_DOC_ID_RE
from ..peek_client import _SLUG_RE as _PEEK_SLUG_RE

_SHOWN_CHARS = 60


class RuleValueError(ValueError):
    """A rule value that is not shaped like the id its scope matches on."""


@dataclass(frozen=True)
class ValueFormat:
    pattern: re.Pattern[str]
    what: str
    how: str
    example: str


def _fmt(pattern: str | re.Pattern[str], what: str, how: str, example: str) -> ValueFormat:
    compiled = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)
    return ValueFormat(compiled, what, how, example)


_DRIVE_FOLDER = _fmt(
    r"root|[A-Za-z0-9_-]+", "a Drive folder id",
    "the id from the folder's URL (the part after /folders/), not its name or the URL",
    "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
)
_SLACK_ID = _fmt(
    r"[CDGUW][A-Z0-9]+", "a Slack ID",
    "the ID from the channel's or person's details (it starts with C, D, G, U or W), not the #name",
    "C0123456789",
)
_TELEGRAM_CHAT = _fmt(
    r"-?[0-9]+", "a Telegram chat id", "the numeric chat id, not the chat's name", "-100987654321",
)
_JIRA_PROJECT = _fmt(
    r"[A-Za-z][A-Za-z0-9_]*", "a Jira project key",
    "the short project key (the prefix of its issue keys), not the project's name", "MYPROJ",
)
_ASSETS_SCHEMA = _fmt(
    _ASSETS_ID_RE, "an Assets schema id", "the numeric schema id, not the schema's name", "12",
)
_DOMAIN = _fmt(
    r"(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]{2,}", "a domain",
    "a bare domain such as example.com, not an email address or URL", "example.com",
)

VALUE_FORMATS: dict[str, ValueFormat] = {
    "approved_folder": _DRIVE_FOLDER,
    "approved_sandbox_folder": _DRIVE_FOLDER,
    "parent_folder_allowlist": _DRIVE_FOLDER,
    "move_within_approved_folders": _DRIVE_FOLDER,
    "approved_channel": _SLACK_ID,
    "approved_channel_all_results": _SLACK_ID,
    "approved_recipient": _SLACK_ID,
    "approved_chats": _TELEGRAM_CHAT,
    "approved_chats_all_results": _TELEGRAM_CHAT,
    "approved_project_keys": _JIRA_PROJECT,
    "approved_project_keys_all_results": _JIRA_PROJECT,
    "approved_schema_ids": _ASSETS_SCHEMA,
    "approved_schema_ids_all_results": _ASSETS_SCHEMA,
    "approved_space_keys": _fmt(
        r"~?[A-Za-z0-9][A-Za-z0-9_.-]*", "a Confluence space key",
        "the space key from the space's URL, not the space's name", "TEAM",
    ),
    "personal_calendar": _fmt(
        r"primary|[^\s@]+@[^\s@]+", "a calendar id",
        "`primary`, or the calendar id from its settings (it looks like an email address), "
        "not the calendar's name",
        "primary",
    ),
    "approved_report_ids": _fmt(
        r"00O[A-Za-z0-9]{12}([A-Za-z0-9]{3})?", "a Salesforce report id",
        "the 15 or 18 character id that starts with 00O, not the report's name", "00O000000000001",
    ),
    "approved_object_types": _fmt(
        r"[A-Za-z][A-Za-z0-9_]*", "a Salesforce object API name",
        "the API name, such as Account or MyThing__c, not the label", "Account",
    ),
    "approved_task_list": _fmt(
        r"@default|[A-Za-z0-9_-]+={0,2}", "a Google Tasks list id",
        "the list's id, not its title", "MDAwMDAwMDAwMDAwMDAwMDAwMDA6MDow",
    ),
    "apps_script.project": _fmt(
        r"[A-Za-z0-9_-]+", "an Apps Script project id",
        "the script id from the project's settings, not its title", "1A2b3C4d5E6f7G8h",
    ),
    "grist.document": _fmt(
        _GRIST_DOC_ID_RE, "a Grist document id",
        "the full Document ID from the document's Settings page (docs/grist-setup.md), "
        "not its name or the shorter id in its address",
        "8CAN8gKdxY7zq3FvPXbHdM",
    ),
    "peek.page": _fmt(
        _PEEK_SLUG_RE, "a Peek page slug", "the page's slug, not its title", "team-dashboard",
    ),
    "trusted_sender_domain": _DOMAIN,
    "approved_recipient_domain": _DOMAIN,
}

# Value-taking predicates whose values are names by nature.
UNVALIDATED_VALUE_PREDICATES: frozenset[str] = frozenset({"label_name_allowlist", "file_type_allowlist"})


def _shown(item: str) -> str:
    return item if len(item) <= _SHOWN_CHARS else item[:_SHOWN_CHARS] + "…"


def normalise_rule_value(predicates: Iterable[str], value: Any) -> list[str] | None:
    """The rule value as a deduplicated list of stripped strings, or ``None`` for no value.

    ``value`` may be a string, an int or a list of those. Each item must fully match the format of
    every predicate in ``predicates`` that has one; a URL is rejected, never rewritten into an id.
    Raises ``RuleValueError`` with a message meant for the person adding the rule.
    """
    if value is None or value == []:
        return None
    raw_items = value if isinstance(value, list) else [value]
    items: list[str] = []
    for raw in raw_items:
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise RuleValueError("The rule value must be a text id or a list of ids.")
        item = str(raw).strip()
        if not item:
            raise RuleValueError("The rule value has an empty entry.")
        items.append(item)
    formats = [VALUE_FORMATS[p] for p in predicates if p in VALUE_FORMATS]
    for item in items:
        for fmt in formats:
            if not fmt.pattern.fullmatch(item):
                raise RuleValueError(
                    f"{_shown(item)!r} is not {fmt.what}. Use {fmt.how}, for example {fmt.example}."
                )
    return list(dict.fromkeys(items))


__all__ = [
    "UNVALIDATED_VALUE_PREDICATES",
    "VALUE_FORMATS",
    "RuleValueError",
    "ValueFormat",
    "normalise_rule_value",
]
