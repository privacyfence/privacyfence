"""Jira connector."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from ..atlassian_users import display_markup, markup_mention_ids
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason, gated_call
from ..jira_client import JiraClient, JiraClientError, _text_to_adf
from ..preview_dates import format_preview_datetime

logger = logging.getLogger(__name__)


# Built-in fields jira_update_issue has a dedicated, name-resolving parameter for.
_DEDICATED_FIELD_PARAMS = {
    "summary": "summary",
    "description": "description",
    "priority": "priority",
    "assignee": "assignee_account_id",
}


def _account_ids_in(value: Any) -> list[str]:
    """Every account id inside a custom field value, at any depth: dicts with
    an ``accountId`` key and ADF ``mention`` nodes (``attrs.id``)."""
    found: list[str] = []

    def add(account_id: Any) -> None:
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("update_issue: custom_fields contains an invalid account id.")
        if account_id not in found:
            found.append(account_id)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "accountId" in node:
                add(node["accountId"])
            attrs = node.get("attrs")
            if node.get("type") == "mention" and isinstance(attrs, dict):
                add(attrs.get("id"))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def _one_user(value: Any, field_name: str) -> dict[str, str]:
    """A person value for a user field: ``{"accountId": id}`` or a bare id, nothing else."""
    if isinstance(value, str) and value:
        return {"accountId": value}
    if isinstance(value, dict) and set(value) == {"accountId"}:
        return {"accountId": value["accountId"]}
    raise ValueError(
        f"update_issue: '{field_name}' is a user field; give each person as "
        '{"accountId": "<id>"} or the bare account id (from jira_find_users). '
        "Other keys such as id, name, key, displayName or emailAddress are not accepted."
    )


def _normalise_user_value(kind: str, value: Any, field_name: str) -> Any:
    if kind == "user":
        return None if value is None else _one_user(value, field_name)
    return [_one_user(item, field_name) for item in value]


def _display_value(node: Any, names: dict[str, str]) -> Any:
    """``node`` for the approval card: every person (accountId object or ADF mention) is
    replaced by the directory's name; the agent's own labels for people never show."""
    if isinstance(node, dict):
        attrs = node.get("attrs")
        if "accountId" in node:
            return "@" + names[node["accountId"]]
        if node.get("type") == "mention" and isinstance(attrs, dict):
            return "@" + names[attrs["id"]]
        return {k: _display_value(v, names) for k, v in node.items()}
    if isinstance(node, list):
        return [_display_value(v, names) for v in node]
    return node


def _parse_json_object(value: str) -> dict[str, Any] | None:
    """Parse a JSON object tool argument, or None if empty/invalid."""
    if not value or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


class JiraConnector(Connector):
    def __init__(self, client: JiraClient) -> None:
        self._jira = client
        self.my_email: str = ""

    @property
    def client(self) -> JiraClient:
        return self._jira

    @property
    def name(self) -> str:
        return "jira"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="jira_list_projects",
                description=(
                    "List Jira projects accessible to the user (key, name, type, lead). "
                    "Returns a list of {key, name, project_type, description, lead}, at most "
                    "max_results (default 50, capped at 500), in the API's order. Use the "
                    "key as project_key in jira_create_issue, or in a JQL query for "
                    "jira_search_issues. Auto-approved."
                ),
                params=[
                    ToolParam("max_results", "int", required=False, default=50,
                              description="Maximum number of projects to return. Default 50, capped at 500."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="jira_search_issues",
                description=(
                    "Search Jira issues using JQL. Returns summary info for matching issues, "
                    "as a list of {key, summary, status, issue_type, priority, assignee, "
                    "reporter, labels, created, updated, url} with description left empty, "
                    "at most max_results (default 20, capped at 500), "
                    "in the order the JQL gives. Use jira_get_issue instead for one issue's "
                    "description and comments. Auto-approved."
                ),
                params=[
                    ToolParam("jql", "str",
                              description="A JQL query, e.g. \"project = MYPROJ AND status = 'In Progress' "
                                          "ORDER BY updated DESC\". Must not be empty."),
                    ToolParam("max_results", "int", required=False, default=20,
                              description="Maximum number of issues to return. Default 20, capped at 500."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="jira_get_issue",
                description=(
                    "Fetch full details of a Jira issue by key (e.g. PROJ-123), "
                    "including description and comments. Returns the issue as {key, summary, "
                    "status, issue_type, priority, assignee, reporter, description, labels, "
                    "created, updated, url, comments: a list of {id, author, body, created, "
                    "updated}}. Use jira_search_issues instead to find issues without "
                    "reading them in full. Mentions appear as @[Name](accountId). Requires user approval."
                ),
                params=[
                    ToolParam("issue_key", "str",
                              description="Key of the issue, e.g. PROJ-123, from jira_search_issues "
                                          "(its key field)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="jira_get_transitions",
                description=(
                    "List the status transitions available for a Jira issue right now (name and "
                    "target status), given its current workflow state. Use before "
                    "jira_transition_issue to see what transition names are valid. Returns a "
                    "list of {id, name, to_status}; pass a name to jira_transition_issue. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("issue_key", "str",
                              description="Key of the issue, e.g. PROJ-123, from jira_search_issues "
                                          "(its key field)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="jira_find_users",
                description=(
                    "Find Atlassian users by name or email and return their account ids. "
                    "Returns a list of {account_id, display_name, active, account_type}, "
                    "at most max_results entries; email addresses are never returned. "
                    "Auto-approved -- mention someone in a comment or description by writing "
                    "@[Name](accountId), or assign an issue with assignee_account_id. "
                    "Use jira_search_issues instead to find issues, not people."
                ),
                params=[
                    ToolParam("query", "str",
                              description="Part of a person's name or email address, e.g. 'jane' or "
                                          "'jane@example.com'. Must not be empty."),
                    ToolParam("max_results", "int", required=False, default=10,
                              description="Most users to return, 1 to 50. Defaults to 10."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="jira_refresh_user_cache",
                description=(
                    "Re-fetch the names of every Atlassian account id PrivacyFence has cached. "
                    "Auto-approved -- use this when a renamed or newly added person shows up "
                    "wrong; cached names otherwise refresh on their own after 7 days. "
                    "Returns {cached_users: n}, the number of account ids re-fetched. Use "
                    "jira_find_users instead to look up someone by name."
                ),
                params=[ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
                read_only=True,
            ),
            ToolSpec(
                name="jira_create_issue",
                description=(
                    "Create a new Jira issue. Returns the created issue, with its new key, in "
                    "the same shape jira_get_issue returns minus comments. Get project_key "
                    "from jira_list_projects. Requires user approval."
                ),
                params=[
                    ToolParam("project_key", "str",
                              description="Key of the project to create the issue in, e.g. MYPROJ, "
                                          "from jira_list_projects (its key field)."),
                    ToolParam("summary", "str", description="One-line title of the issue. Must not be empty."),
                    ToolParam("issue_type", "str", required=False, default="Task",
                              description="Name of the issue type, e.g. 'Task', 'Bug' or 'Story'; it "
                                          "must exist in the project. Default 'Task'."),
                    ToolParam("description", "str", required=False, default="",
                              description="Plain-text description of the issue. Empty means no description."),
                    ToolParam("priority", "str", required=False, default="",
                              description="Name of the priority, e.g. 'High', 'Medium' or 'Low'. Empty "
                                          "uses the project's default priority."),
                    ToolParam("assignee_account_id", "str", required=False, default="",
                              description="Atlassian account id to assign the issue to, from "
                                          "jira_find_users (its account_id field). Empty assigns "
                                          "nobody."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="jira_add_comment",
                description=(
                    "Add a comment to an existing Jira issue. Returns the new comment as {id, "
                    "author, body, created, updated}. Use jira_update_issue instead to change "
                    "the issue's own fields. Requires user approval."
                ),
                params=[
                    ToolParam("issue_key", "str",
                              description="Key of the issue, e.g. PROJ-123, from jira_search_issues "
                                          "(its key field)."),
                    ToolParam("body", "str",
                              description="Comment text, plain text (not Jira markup). Must not be empty. Mention "
                                          "someone with @[Name](accountId), the id from jira_find_users."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="jira_update_issue",
                description=(
                    "Update fields on an existing Jira issue (summary, description, priority, "
                    "and/or custom fields). Only the fields you pass non-empty change, and at "
                    "least one is required. Returns the updated issue in the same shape "
                    "jira_get_issue returns minus comments. Use jira_transition_issue instead "
                    "to change its status. Pass assignee_account_id to reassign it. Requires user approval."
                ),
                params=[
                    ToolParam("issue_key", "str",
                              description="Key of the issue, e.g. PROJ-123, from jira_search_issues "
                                          "(its key field)."),
                    ToolParam("summary", "str", required=False, default="",
                              description="New one-line summary. Empty leaves the summary unchanged."),
                    ToolParam("description", "str", required=False, default="",
                              description="New plain-text description, replacing the current one. Empty "
                                          "leaves the description unchanged."),
                    ToolParam("priority", "str", required=False, default="",
                              description="New priority name, e.g. 'High', 'Medium' or 'Low'. Empty "
                                          "leaves the priority unchanged."),
                    ToolParam("assignee_account_id", "str", required=False, default="",
                              description="Atlassian account id to assign the issue to, from "
                                          "jira_find_users (its account_id field). Empty leaves the "
                                          "assignee unchanged or unset."),
                    ToolParam("custom_fields", "str", required=False, default="",
                              description=(
                                  "JSON object mapping Jira Cloud custom field display names "
                                  "(as seen in the Jira UI, not their customfield_NNNNN id) to "
                                  "new values, e.g. {\"Story Points\": 5, \"Sprint\": \"Sprint 12\"}. "
                                  "Empty changes no custom fields."
                              )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="jira_transition_issue",
                description=(
                    "Move a Jira issue to a new status by transition name (e.g. \"Done\", "
                    "\"In Progress\") — call jira_get_transitions first to see what's valid from "
                    "the issue's current status. Returns the issue after the move, in the same "
                    "shape jira_get_issue returns minus comments; errors, listing the valid "
                    "names, if the name is not available. Requires user approval."
                ),
                params=[
                    ToolParam("issue_key", "str",
                              description="Key of the issue, e.g. PROJ-123, from jira_search_issues "
                                          "(its key field)."),
                    ToolParam("transition_name", "str",
                              description="Name of the transition to apply, e.g. 'Done' or 'In Progress', "
                                          "as jira_get_transitions returns it in name (case-insensitive)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "jira_list_projects":
            return await self._list_projects(**args)
        if tool == "jira_search_issues":
            return await self._search_issues(**args)
        if tool == "jira_get_issue":
            return await self._get_issue(**args)
        if tool == "jira_get_transitions":
            return await self._get_transitions(**args)
        if tool == "jira_find_users":
            return await self._find_users(**args)
        if tool == "jira_refresh_user_cache":
            return await self._refresh_user_cache(**args)
        if tool == "jira_create_issue":
            return await self._create_issue(**args)
        if tool == "jira_add_comment":
            return await self._add_comment(**args)
        if tool == "jira_update_issue":
            return await self._update_issue(**args)
        if tool == "jira_transition_issue":
            return await self._transition_issue(**args)
        raise ValueError(f"Unknown Jira tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_projects(self, max_results: int = 50) -> Any:
        t0 = time.time()
        projects = await self._fetch(self._jira.list_projects, max_results)
        data = [asdict(p) for p in projects]
        self._auto_audit("jira_list_projects", "List Jira Projects",
                         f"List projects (max {max_results})", f"{len(projects)} project(s)", t0)
        return data

    async def _search_issues(self, jql: str, max_results: int = 20) -> Any:
        t0 = time.time()
        issues = await self._fetch(self._jira.search_issues, jql, max_results)
        data = [asdict(i) for i in issues]
        self._auto_audit("jira_search_issues", "Search Jira Issues",
                         f"Search: {jql[:80]}", f"{len(issues)} issue(s)", t0)
        return data

    async def _get_transitions(self, issue_key: str) -> Any:
        t0 = time.time()
        transitions = await self._fetch(self._jira.get_transitions, issue_key)
        data = [asdict(t) for t in transitions]
        self._auto_audit("jira_get_transitions", "List Jira Transitions",
                         f"List transitions: {issue_key}", f"{len(transitions)} transition(s)", t0)
        return data

    async def _find_users(self, query: str, max_results: int = 10) -> Any:
        t0 = time.time()
        users = await self._fetch(self._jira.find_users, query, max_results)
        data = [asdict(u) for u in users]
        self._auto_audit("jira_find_users", "Find Jira Users",
                         "Find users", f"{len(users)} user(s)", t0)
        return data

    async def _refresh_user_cache(self) -> Any:
        t0 = time.time()
        count = await self._fetch(self._jira.refresh_user_cache)
        self._auto_audit("jira_refresh_user_cache", "Refresh Atlassian User Cache",
                         "Refresh Atlassian user cache", f"{count} user(s)", t0)
        return {"cached_users": count}

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_issue(self, issue_key: str) -> Any:
        issue = await self._fetch(self._jira.get_issue, issue_key)
        comments = await self._fetch(self._jira.get_issue_comments, issue_key)
        result = {**asdict(issue), "comments": [asdict(c) for c in comments]}
        # Project/Key/Summary/Status/Assignee are all known for free via
        # jira_search_issues; Description and Comments are only learned
        # once this call is approved -- jira_search_issues never returns
        # either. Neither has a fixed size (Description's length is
        # unbounded, Comments has no fixed count), so new_info gets one fixed
        # summary row for each rather than the literal text -- the real
        # content lives in the right-pane preview/details_text instead.
        preview = {
            "Project": getattr(issue, "project_name", "") or issue_key.split("-")[0],
            "Key": issue.key,
            "Summary": (issue.summary[:80] + "…") if len(issue.summary) > 80 else issue.summary,
            "Status": getattr(issue, "status", "") or "",
            "Assignee": getattr(issue, "assignee", "") or "(unassigned)",
        }
        new_info = {
            "Description": "Full description text",
            "Comments": "Author, created date, and body per comment",
        }
        description_text = display_markup(getattr(issue, "description", "") or "")
        comment_bodies = [display_markup(getattr(c, "body", "") or "") for c in comments]
        details_parts = []
        if len(issue.summary) > 80:
            # Preview truncates the summary at 80 chars -- the untruncated
            # text is only reachable here.
            details_parts.append(f"Summary: {issue.summary}\n")
        details_parts.append(
            f"Reporter: {getattr(issue, 'reporter', '')}\n\n"
            f"Description:\n{description_text or '(none)'}"
        )
        # details_text/pii_scan_text stay a flat string -- kept for legacy
        # display and the PII scan's default fallback, unrelated to how v2
        # renders the same content (preview_blocks below).
        details = "".join(details_parts)
        pii_scan_text = (
            f"{description_text}\n\n" + "\n".join(comment_bodies)
        )
        # v2's right pane: Reporter/Summary as standalone labeled fields
        # (same font as a table header -- see approval_window_html.py's
        # _field_block_html), Description as a heading + paragraph, then
        # Comments as its own table -- interleaved via preview_blocks
        # rather than one flat text blob, so "Reporter"/"Description"
        # visually match "Author"/"Date"/"Comment" instead of looking like
        # plain unstyled prose.
        blocks = []
        if len(issue.summary) > 80:
            blocks.append({"type": "field", "label": "Summary", "value": issue.summary})
        blocks.append({"type": "field", "label": "Reporter", "value": getattr(issue, "reporter", "") or ""})
        blocks.append({"type": "heading", "label": "Description"})
        blocks.append({"type": "text", "text": description_text or "(none)"})
        if comments:
            blocks.append({
                "type": "table",
                "caption": f"Comments ({len(comments)})",
                "headers": ["Author", "Date", "Comment"],
                "rows": [
                    [
                        getattr(c, "author", "unknown"),
                        format_preview_datetime(getattr(c, "created", "")),
                        body,
                    ]
                    for c, body in zip(comments, comment_bodies, strict=True)
                ],
            })
        return await gated_call(
            connector=self.name,
            tool="jira_get_issue",
            tool_name="Read Jira Issue",
            summary=f"{issue.key}: {issue.summary[:60]}",
            sender=getattr(issue, "reporter", "") or getattr(issue, "assignee", "") or issue_key,
            raw_data=result,
            filtered_data=result,
            gate="review",
            preview=preview,
            new_info=new_info,
            details_text=details,
            pii_scan_text=pii_scan_text,
            preview_blocks=blocks,
            my_email=self.my_email,
            args={"issue_key": issue_key},
        )

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str = "Task",
        description: str = "",
        priority: str = "",
        assignee_account_id: str = "",
    ) -> Any:
        names = await self._resolve_write_accounts(description, [assignee_account_id])
        payload = {
            "project_key": project_key, "summary": summary,
            "issue_type": issue_type, "description": description, "priority": priority,
        }
        if assignee_account_id:
            payload["assignee_account_id"] = assignee_account_id
        preview = {"Project": project_key, "Type": issue_type, "Summary": summary}
        if priority:
            preview["Priority"] = priority
        if assignee_account_id:
            preview["Assignee"] = names[assignee_account_id]
        if markup_mention_ids(description):
            preview["Mentions"] = ", ".join(names[i] for i in markup_mention_ids(description))
        shown_description = display_markup(description, names)
        # v2's right pane: a label-styled "Description" heading above the
        # body text, same treatment jira_get_issue's own Description
        # already gets (see approval_window_html.py's _field_block_html) --
        # instead of plain unstyled prose with no field name at all. Empty
        # when there's no description at all, same as get_issue's own
        # blocks list (build_preview_body_html falls back to details_text).
        blocks = []
        if description:
            blocks.append({"type": "heading", "label": "Description"})
            blocks.append({"type": "text", "text": shown_description})
        await gated_call(
            connector=self.name,
            tool="jira_create_issue",
            tool_name="Create Jira Issue",
            summary=f"Create {issue_type} in {project_key}: {summary[:60]}",
            sender=f"project={project_key}",
            raw_data=payload,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=shown_description,
            preview_blocks=blocks,
            my_email=self.my_email,
            args=payload,
        )
        issue = await self._fetch(
            self._jira.create_issue, project_key, summary, issue_type, description, priority,
            assignee_account_id, None, names,
        )
        return asdict(issue)

    async def _add_comment(self, issue_key: str, body: str) -> Any:
        names = await self._resolve_write_accounts(body, [])
        issue = await self._fetch(self._jira.get_issue, issue_key)
        preview = {"Issue": f"{issue.key} — {issue.summary}"}
        if markup_mention_ids(body):
            preview["Mentions"] = ", ".join(names[i] for i in markup_mention_ids(body))
        shown_body = display_markup(body, names)
        await gated_call(
            connector=self.name,
            tool="jira_add_comment",
            tool_name="Add Jira Comment",
            summary=f"Comment on {issue_key}: {shown_body[:80]}",
            sender=f"issue={issue_key}",
            raw_data={"issue_key": issue_key, "body": body},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=shown_body,
            my_email=self.my_email,
            args={"issue_key": issue_key, "body": body},
        )
        comment = await self._fetch(self._jira.add_comment, issue_key, body, names)
        return asdict(comment)

    async def _update_issue(
        self,
        issue_key: str,
        summary: str = "",
        description: str = "",
        priority: str = "",
        assignee_account_id: str = "",
        custom_fields: str = "",
    ) -> Any:
        custom_updates = _parse_json_object(custom_fields)
        if custom_fields and custom_updates is None:
            raise ValueError(
                "update_issue: custom_fields must be a JSON object, e.g. {\"Story Points\": 5}"
            )
        # Field ids are resolved up front so a built-in field cannot ride in
        # through custom_fields, and so every account id inside a custom value
        # is resolved with the rest before the approval card (ADR 0118).
        resolved_custom: list[tuple[str, str, Any, Any, list[str]]] = []  # name, id, shown, sent, people
        seen_field_ids: set[str] = set()
        for field_name, value in (custom_updates or {}).items():
            field_id, coerced = await self._fetch(self._jira.resolve_custom_field, field_name, value)
            if field_id in _DEDICATED_FIELD_PARAMS:
                raise ValueError(
                    f"update_issue: custom_fields cannot set '{field_name}'; "
                    f"use the dedicated {_DEDICATED_FIELD_PARAMS[field_id]} parameter instead."
                )
            if field_id in seen_field_ids:
                raise ValueError(f"update_issue: custom_fields sets '{field_name}' more than once.")
            seen_field_ids.add(field_id)
            kind = await self._fetch(self._jira.custom_field_kind, field_name)
            if kind != "other":
                coerced = _normalise_user_value(kind, coerced, field_name)
            resolved_custom.append(
                (field_name, field_id, coerced if kind != "other" else value, coerced, _account_ids_in(coerced))
            )
        custom_ids = [i for *_, ids in resolved_custom for i in ids]
        names = await self._resolve_write_accounts(description, [assignee_account_id, *custom_ids])
        issue = await self._fetch(self._jira.get_issue, issue_key)
        fields: dict[str, Any] = {}
        preview = {"Issue": f"{issue.key} — {issue.summary}"}
        if summary:
            fields["summary"] = summary
            preview["Summary"] = f"{issue.summary} → {summary}"
        if description:
            fields["description"] = _text_to_adf(description, names)
            preview["Description"] = "(updated — see below)"
            if markup_mention_ids(description):
                preview["Mentions"] = ", ".join(names[i] for i in markup_mention_ids(description))
        if priority:
            fields["priority"] = {"name": priority}
            preview["Priority"] = f"→ {priority}"
        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}
            preview["Assignee"] = f"→ {names[assignee_account_id]}"
        written: list[str] = []
        for field_name, field_id, shown, coerced, ids in resolved_custom:
            fields[field_id] = coerced
            if isinstance(shown, dict) and shown.get("type") == "doc":
                text = display_markup(JiraClient._extract_adf_text(shown, names), names)
                preview[field_name] = "→ (updated — see below)" + (f"; people: {', '.join(names[i] for i in ids)}" if ids else "")
                written.append(f"{field_name}:\n{text}")
            elif ids:
                named = _display_value(shown, names)
                preview[field_name] = "→ " + (", ".join(map(str, named)) if isinstance(named, list) else str(named))
            else:
                preview[field_name] = f"→ {shown}"
        if not fields:
            raise ValueError("update_issue: at least one field must be provided")
        if description:
            written.insert(0, display_markup(description, names) if not written else
                           f"description:\n{display_markup(description, names)}")
        if written:
            details_text = "\n\n".join(written)
        else:
            changed_fields = ", ".join(k for k in preview if k != "Issue")
            details_text = f"{changed_fields} will be updated; description is unchanged."
        await gated_call(
            connector=self.name,
            tool="jira_update_issue",
            tool_name="Update Jira Issue",
            summary=f"Update {issue_key}: {', '.join(k for k in preview if k != 'Issue')}",
            sender=f"issue={issue_key}",
            raw_data={"issue_key": issue_key, "fields": fields},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details_text,
            my_email=self.my_email,
            args={"issue_key": issue_key},
        )
        updated = await self._fetch(self._jira.update_issue, issue_key, fields)
        return asdict(updated)

    async def _transition_issue(self, issue_key: str, transition_name: str) -> Any:
        issue = await self._fetch(self._jira.get_issue, issue_key)
        preview = {
            "Issue": f"{issue.key} — {issue.summary}",
            "Status": f"{issue.status} → {transition_name}",
        }
        await gated_call(
            connector=self.name,
            tool="jira_transition_issue",
            tool_name="Transition Jira Issue",
            summary=f"Transition {issue_key}: {issue.status} → {transition_name}",
            sender=f"issue={issue_key}",
            raw_data={"issue_key": issue_key, "transition_name": transition_name},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="The status change above is the only change; no other fields are affected.",
            my_email=self.my_email,
            args={"issue_key": issue_key, "transition_name": transition_name},
        )
        updated = await self._fetch(self._jira.transition_issue, issue_key, transition_name)
        return asdict(updated)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _resolve_write_accounts(self, text: str, extra_ids: list[str]) -> dict[str, str]:
        """Directory names for every account id a write mentions or assigns.

        The approver is shown these names, never the agent's labels (ADR 0118);
        an id that cannot be resolved is refused before the approval card.
        """
        ids = list(dict.fromkeys(markup_mention_ids(text) + [i for i in extra_ids if i]))
        if not ids:
            return {}
        names = await self._fetch(self._jira.resolve_user_names, ids)
        missing = [i for i in ids if i not in names]
        if missing:
            raise ValueError(
                f"Unknown Atlassian account id(s): {', '.join(missing)}. "
                "Look the person up with jira_find_users and use their account_id."
            )
        return names

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except JiraClientError as exc:
            logger.error("Jira fetch failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

    def _auto_audit(
        self, tool: str, tool_name: str, summary: str, sender: str, created_at: float
    ) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool_name,
                summary=summary,
                sender=sender,
                decision="auto_accepted",
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
