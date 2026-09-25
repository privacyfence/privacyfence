"""Frozen pre-P9 v1 reference implementation, kept only so the P2 equivalence tests
(test_conditions.py, test_scopes.py) keep meaning what they always meant.

P9 deletes `AutoAcceptEvaluator` and all its `_rule_*` predicate methods, plus the
`ARGS_ONLY_RULES`/`DATA_DEPENDENT_RULES` classification sets, from
`privacyfence/auto_accept.py` -- the v1 rule engine they belonged to is gone, replaced by
`policy.scopes`/`policy.conditions`. But the whole point of `test_conditions.py`/
`test_scopes.py` is proving the v2 `ConditionSelector`/`ScopeSelector` objects behave
*identically* to what those old `_rule_*` methods always did, fixture by fixture -- so this
module is a verbatim, standalone copy of exactly what `_rule_*` did, `parseaddr()`-comparison
security fix and all, extracted from the last commit before P9's deletion
(`git show HEAD:src/privacyfence/auto_accept.py` as of this PR). It intentionally does not
import anything from `privacyfence.auto_accept` -- that module is expected to keep changing
under P9 and beyond, and this file's job is to stay exactly as it was the day v1 was deleted,
not to track whatever `auto_accept.py` does next. If a real behavioral difference between v1
and v2 is ever suspected, diff this file against the pre-P9 commit above rather than editing it
to "improve" it.

Only the pieces `test_conditions.py`/`test_scopes.py` actually exercise are kept: the
`_rule_*` predicate methods either file calls via `_old()`, the small set of private helpers
those methods depend on (`_file_from`, `_domain_of`, `_address_of`, `_attendee_email`), the
two classification frozensets both files assert selectors against, and `V1_CONDITION_NAMES` --
which v1 predicate names each v2 condition took over. That mapping used to live in
`policy/conditions.py` as `ConditionSelector.replaces`, for the one-time settings conversion; the
conversion is gone (ADR 0041), so only this harness still needs it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

# ── v2 condition name -> the v1 predicate names it took over ─────────────────────────────────

V1_CONDITION_NAMES: dict[str, tuple[str, ...]] = {
    "older_than_days": ("age_threshold_days",),
    "within_days": ("time_window_days",),
    "past_only": ("past_event",),
    "no_attachments": ("no_attachments", "no_file_attachments", "no_media_attachments"),
    "no_external_attendees": ("no_external_attendees",),
    "no_conferencing_link": ("no_conferencing_link",),
    "not_private": ("non_private_event",),
    "not_shared_drive": ("shared_drive_exclusion",),
    "no_contact_info_change": ("no_contact_info_change",),
    "in_existing_thread": ("reply_in_existing_thread",),
}


def condition_name_for_v1_predicate(predicate: str) -> str | None:
    """The v2 condition name that took over v1 ``predicate``, or ``None`` if it isn't one."""
    for name, v1_names in V1_CONDITION_NAMES.items():
        if predicate in v1_names:
            return name
    return None


# ── Classification sets (verbatim from pre-P9 auto_accept.py) ───────────────────────────────

ARGS_ONLY_RULES: frozenset[str] = frozenset({
    "dm_with_myself",
    "send_to_myself",
    "group_dm",
    "approved_channel",
    "approved_recipient",
    "reply_in_existing_thread",
    "personal_calendar",
    "approved_object_types",
    "approved_report_ids",
    "to_is_myself",
    "approved_recipient_domain",
    "label_name_allowlist",
    "parent_folder_allowlist",
    "no_contact_info_change",
    "approved_project_keys",
    "approved_chats",
    "approved_task_list",
    "approved_space_keys",
    "always_allow",
})

DATA_DEPENDENT_RULES: frozenset[str] = frozenset({
    "i_am_sender",
    "i_am_sole_recipient",
    "trusted_sender_domain",
    "label_match",
    "age_threshold_days",
    "no_attachments",
    "i_am_owner",
    "created_by_me",
    "approved_folder",
    "approved_sandbox_folder",
    "move_within_approved_folders",
    "file_type_allowlist",
    "created_this_session",
    "shared_drive_exclusion",
    "public_channels_only",
    "no_file_attachments",
    "i_am_organizer",
    "no_external_attendees",
    "past_event",
    "time_window_days",
    "no_conferencing_link",
    "i_am_reporter",
    "i_am_assignee",
    "i_am_author",
    "no_media_attachments",
    "approved_channel_all_results",
    "approved_chats_all_results",
    "non_private_event",
})


# ── Private helpers (verbatim from pre-P9 auto_accept.py) ───────────────────────────────────

def _file_from(raw: Any) -> Any:
    """Unwrap a Drive file object out of whatever shape a call's raw_data carries it in."""
    if isinstance(raw, dict):
        return raw.get("file", raw)
    return raw.file if hasattr(raw, "file") else raw


def _domain_of(sender: str) -> str:
    email_part = sender
    if "<" in sender and ">" in sender:
        email_part = sender[sender.index("<") + 1 : sender.index(">")]
    return email_part.split("@", 1)[-1].lower().strip()


def _address_of(raw: str) -> str:
    """Extract just the address out of a raw RFC 5322 header-shaped string, lower-cased."""
    return parseaddr(raw or "")[1].strip().lower()


def _attendee_email(attendee: Any) -> str:
    """Extract an email address from an attendee, whichever shape it's in."""
    if isinstance(attendee, dict):
        return attendee.get("email", "") or ""
    if isinstance(attendee, str):
        return attendee
    return getattr(attendee, "email", "") or ""


class V1Reference:
    """Standalone stand-in for the deleted `AutoAcceptEvaluator` -- every `_rule_*` method
    both equivalence test files call via `_old()`, unchanged from pre-P9 `auto_accept.py`.
    """

    # ── Gmail ──────────────────────────────────────────────────────────────

    def _rule_i_am_sender(self, _v, ctx):
        if not ctx.my_email:
            return False
        sender = getattr(ctx.raw_data, "sender", "") or ""
        return _address_of(sender) == ctx.my_email.lower()

    def _rule_i_am_sole_recipient(self, _v, ctx):
        if not ctx.my_email:
            return False
        recips = getattr(ctx.raw_data, "recipients", []) or []
        return len(recips) == 1 and _address_of(recips[0]) == ctx.my_email.lower()

    def _rule_trusted_sender_domain(self, value, ctx):
        if not value:
            return False
        raw_sender = getattr(ctx.raw_data, "sender", "") or ""
        email_part = raw_sender
        if "<" in raw_sender and ">" in raw_sender:
            email_part = raw_sender[raw_sender.index("<") + 1 : raw_sender.index(">")]
        domain = email_part.split("@", 1)[-1].lower().strip()
        allowed = {d.lower().strip() for d in (value if isinstance(value, list) else [value])}
        return any(domain == d or domain.endswith("." + d) for d in allowed)

    def _rule_label_match(self, value, ctx):
        if not value:
            return False
        labels = {label.lower() for label in (getattr(ctx.raw_data, "labels", []) or [])}
        allowed = {v.lower() for v in (value if isinstance(value, list) else [value])}
        return bool(labels & allowed)

    def _rule_age_threshold_days(self, value, ctx):
        if not value:
            return False
        date_str = getattr(ctx.raw_data, "date", "") or ""
        if not date_str:
            return False
        try:
            dt = parsedate_to_datetime(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).days >= int(value)
        except Exception:
            return False

    def _rule_no_attachments(self, _v, ctx):
        return len(getattr(ctx.raw_data, "attachments", []) or []) == 0

    # ── Drive ─────────────────────────────────────────────────────────────

    def _file_from(self, raw):
        return _file_from(raw)

    def _rule_i_am_owner(self, _v, ctx):
        if not ctx.my_email:
            return False
        f = self._file_from(ctx.raw_data)
        owners = getattr(f, "owners", []) or []
        return any(_address_of(o) == ctx.my_email.lower() for o in owners)

    def _rule_created_by_me(self, v, ctx):
        return self._rule_i_am_owner(v, ctx)

    def _rule_approved_folder(self, value, ctx):
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        f = self._file_from(ctx.raw_data)
        parents = getattr(f, "parent_ids", []) or []
        return bool(set(parents) & allowed)

    def _rule_approved_sandbox_folder(self, value, ctx):
        return self._rule_approved_folder(value, ctx)

    def _rule_move_within_approved_folders(self, value, ctx):
        return self._rule_approved_folder(value, ctx)

    def _rule_file_type_allowlist(self, value, ctx):
        if not value:
            return False
        allowed = {v.lower() for v in (value if isinstance(value, list) else [value])}
        f = self._file_from(ctx.raw_data)
        return (getattr(f, "mime_type", "") or "").lower() in allowed

    def _rule_created_this_session(self, _v, ctx):
        f = self._file_from(ctx.raw_data)
        return getattr(f, "id", "") in ctx.session_created_ids

    def _rule_shared_drive_exclusion(self, _v, ctx):
        f = self._file_from(ctx.raw_data)
        return not getattr(f, "drive_id", "")

    # ── Slack ─────────────────────────────────────────────────────────────

    def _rule_dm_with_myself(self, _v, ctx):
        # Deliberately not verbatim: v1 matched any "D"-prefixed id, i.e. every 1:1 DM. Both
        # engines now read the connector's self-DM verdict and fail closed without it.
        return ctx.args.get("is_self_dm") is True

    def _rule_send_to_myself(self, v, ctx):
        return self._rule_dm_with_myself(v, ctx)

    def _rule_group_dm(self, _v, ctx):
        return bool(ctx.args.get("is_group_dm", False))

    def _rule_approved_channel(self, value, ctx):
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        cid = ctx.args.get("channel_id", "") or ctx.args.get("channel", "") or ""
        return cid in allowed

    def _rule_approved_recipient(self, value, ctx):
        return self._rule_approved_channel(value, ctx)

    def _rule_approved_channel_all_results(self, value, ctx):
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        items = ctx.raw_data if isinstance(ctx.raw_data, list) else [ctx.raw_data]
        return bool(items) and all(getattr(m, "channel_id", None) in allowed for m in items)

    def _rule_public_channels_only(self, _v, ctx):
        raw = ctx.raw_data
        items = raw if isinstance(raw, list) else [raw]
        return all(not getattr(m, "is_private", True) for m in items)

    def _rule_no_file_attachments(self, _v, ctx):
        raw = ctx.raw_data
        items = raw if isinstance(raw, list) else [raw]
        return all(not (getattr(m, "files", None)) for m in items)

    def _rule_reply_in_existing_thread(self, _v, ctx):
        return bool(ctx.args.get("thread_ts"))

    # ── Calendar ──────────────────────────────────────────────────────────

    def _rule_i_am_organizer(self, _v, ctx):
        raw = ctx.raw_data
        organizer = (raw.get("organizer_email") if isinstance(raw, dict) else getattr(raw, "organizer_email", "")) or ""
        return bool(ctx.my_email) and ctx.my_email.lower() == organizer.lower()

    def _rule_no_external_attendees(self, _v, ctx):
        if not ctx.my_domain:
            return False
        raw = ctx.raw_data
        attendees = (raw.get("attendees") if isinstance(raw, dict) else getattr(raw, "attendees", None)) or []
        return all(ctx.my_domain in _attendee_email(a) for a in attendees)

    def _rule_personal_calendar(self, value, ctx):
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        return ctx.args.get("calendar_id", "") in allowed

    def _rule_past_event(self, _v, ctx):
        end_str = getattr(ctx.raw_data, "end_time", "") or ""
        if not end_str:
            return False
        try:
            dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt < datetime.now(timezone.utc)
        except Exception:
            return False

    def _rule_time_window_days(self, value, ctx):
        if not value:
            return False
        start_str = getattr(ctx.raw_data, "start_time", "") or ""
        if not start_str:
            return False
        try:
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days_ahead = (dt - datetime.now(timezone.utc)).days
            return 0 <= days_ahead <= int(value)
        except Exception:
            return False

    def _rule_no_conferencing_link(self, _v, ctx):
        raw = ctx.raw_data
        return not bool(getattr(raw, "conference_link", "") or getattr(raw, "hangout_link", ""))

    def _rule_non_private_event(self, _v, ctx):
        visibility = getattr(ctx.raw_data, "visibility", None)
        return (visibility or "default") != "private"

    # ── Salesforce ────────────────────────────────────────────────────────

    def _rule_approved_object_types(self, value, ctx):
        if not value:
            return False
        allowed = {v.lower() for v in (value if isinstance(value, list) else [value])}
        if "object_types" in ctx.args:
            requested = [t.strip().lower() for t in (ctx.args.get("object_types") or "").split(",") if t.strip()]
            return bool(requested) and all(t in allowed for t in requested)
        return ctx.args.get("object_type", "").lower() in allowed

    def _rule_approved_report_ids(self, value, ctx):
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        return ctx.args.get("report_id", "") in allowed

    # ── Gmail (writes) ───────────────────────────────────────────────────

    def _rule_to_is_myself(self, _v, ctx):
        to = ctx.args.get("to", "") or ""
        recipients = to if isinstance(to, list) else [to]
        recipients = [r for r in recipients if r]
        return bool(ctx.my_email) and bool(recipients) and all(ctx.my_email.lower() in r.lower() for r in recipients)

    def _rule_approved_recipient_domain(self, value, ctx):
        if not value:
            return False
        to = ctx.args.get("to", "") or ""
        recipients = to if isinstance(to, list) else [to]
        recipients = [r for r in recipients if r]
        allowed = {d.lower().strip() for d in (value if isinstance(value, list) else [value])}
        return bool(recipients) and all(_domain_of(r) in allowed for r in recipients)

    def _rule_label_name_allowlist(self, value, ctx):
        if not value:
            return False
        allowed = {v.lower() for v in (value if isinstance(value, list) else [value])}
        label = (ctx.args.get("label_name") or "").lower()
        return label in allowed

    # ── Drive (writes) ───────────────────────────────────────────────────

    def _rule_parent_folder_allowlist(self, value, ctx):
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        return (ctx.args.get("parent_folder_id") or "") in allowed

    # ── Contacts ──────────────────────────────────────────────────────────

    def _rule_no_contact_info_change(self, _v, ctx):
        return not (ctx.args.get("emails") or ctx.args.get("phones"))

    # ── Jira ──────────────────────────────────────────────────────────────

    def _rule_approved_project_keys(self, value, ctx):
        if not value:
            return False
        allowed = {v.upper() for v in (value if isinstance(value, list) else [value])}
        project_key = ctx.args.get("project_key", "") or ""
        if not project_key:
            issue_key = ctx.args.get("issue_key", "") or ""
            project_key = issue_key.split("-")[0] if "-" in issue_key else ""
        return bool(project_key) and project_key.upper() in allowed

    def _rule_i_am_reporter(self, _v, ctx):
        raw = ctx.raw_data
        reporter = (raw.get("reporter") if isinstance(raw, dict) else getattr(raw, "reporter", "")) or ""
        return bool(ctx.my_email) and ctx.my_email.lower() in reporter.lower()

    def _rule_i_am_assignee(self, _v, ctx):
        raw = ctx.raw_data
        assignee = (raw.get("assignee") if isinstance(raw, dict) else getattr(raw, "assignee", "")) or ""
        return bool(ctx.my_email) and ctx.my_email.lower() in assignee.lower()

    # ── Confluence ────────────────────────────────────────────────────────

    def _rule_approved_space_keys(self, value, ctx):
        if not value:
            return False
        allowed = {v.upper() for v in (value if isinstance(value, list) else [value])}
        raw = ctx.raw_data
        space_key = ctx.args.get("space_key") or (raw.get("space_key") if isinstance(raw, dict) else "") or ""
        return bool(space_key) and space_key.upper() in allowed

    def _rule_i_am_author(self, _v, ctx):
        raw = ctx.raw_data
        author = (raw.get("author") if isinstance(raw, dict) else getattr(raw, "author", "")) or ""
        return bool(ctx.my_email) and ctx.my_email.lower() in author.lower()

    # ── Telegram ──────────────────────────────────────────────────────────

    def _rule_approved_chats(self, value, ctx):
        if not value:
            return False
        allowed = {str(v) for v in (value if isinstance(value, list) else [value])}
        return str(ctx.args.get("chat_id", "")) in allowed

    def _rule_approved_chats_all_results(self, value, ctx):
        if not value:
            return False
        allowed = {str(v) for v in (value if isinstance(value, list) else [value])}
        items = ctx.raw_data if isinstance(ctx.raw_data, list) else [ctx.raw_data]
        return bool(items) and all(str(getattr(m, "chat_id", None)) in allowed for m in items)

    def _rule_no_media_attachments(self, _v, ctx):
        raw = ctx.raw_data
        items = raw if isinstance(raw, list) else [raw]
        return all(not getattr(m, "media_type", "") for m in items)

    # ── Tasks ─────────────────────────────────────────────────────────────

    def _rule_approved_task_list(self, value, ctx):
        if not value:
            return False
        allowed = set(value if isinstance(value, list) else [value])
        if "task_list_id" in ctx.args:
            return ctx.args.get("task_list_id", "") in allowed
        source = ctx.args.get("source_list_id", "")
        destination = ctx.args.get("destination_list_id", "")
        return bool(source) and bool(destination) and source in allowed and destination in allowed

    # ── Generic (no resource identity to scope to) ──────────────────────────

    def _rule_always_allow(self, _v, ctx):
        return True
