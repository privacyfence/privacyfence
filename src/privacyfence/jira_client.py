"""Jira Cloud API client.

Authenticated via Atlassian OAuth 2.0 (3LO) — see ``atlassian_oauth.py``. The
OAuth app (client id/secret) is organization-level config; the resulting
access token + cloud id are per-user, shared with the Confluence client.

Required config keys:
  access_token – OAuth bearer token from atlassian_oauth.authorize_interactive
  cloud_id     – the Atlassian site's cloud id, used to build the
                 api.atlassian.com/ex/jira/{cloud_id} proxy URL
  site_url     – the human-facing site URL (for issue links), optional

Optional config keys (needed to refresh an expired access token — see
``_try_refresh`` below):
  client_id / client_secret – the organization's Atlassian OAuth app
  refresh_token             – from the same OAuth grant as access_token

Account ids in issue descriptions and comments (ADF mentions) are resolved to
names through the ``AtlassianUserDirectory`` shared with the Confluence client
(ADR 0117); a mention renders as ``@[Name](accountId)``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import requests
from atlassian import Jira

from .atlassian_oauth import (
    AtlassianOAuthError,
    is_unauthorized,
    load_token_file,
    refresh as atlassian_refresh,
    save_token_file,
)

from . import atlassian_users
from .atlassian_users import (
    ACCOUNT_ID_RE,
    UNKNOWN_USER_LABEL,
    AtlassianUser,
    AtlassianUserDirectory,
    AtlassianUsersError,
    mask_emails,
    mention_markup,
)

logger = logging.getLogger(__name__)

MAX_PAGES = 10
# The fields _parse_issue reads for a search result; description is left out on purpose.
SEARCH_FIELDS = ("summary", "status", "issuetype", "priority", "assignee", "reporter", "labels", "created", "updated")
# Keys a simplified field value never carries: links back into the API, avatars, and email addresses.
_DROPPED_VALUE_KEYS = frozenset({"self", "_links", "avatarUrls", "iconUrl", "emailAddress", "expand"})
# A Jira Service Management SLA field: it has a name, but its data is in the cycles, so it is not collapsed to the name.
_SLA_KEYS = frozenset({"ongoingCycle", "completedCycles"})
_CF_JQL_RE = re.compile(r"cf\[(\d+)\]", re.IGNORECASE)


class JiraClientError(Exception):
    """Raised for unrecoverable Jira client problems (auth, config, API)."""


def _text_to_adf(text: str, names: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Wrap plain text in a single-paragraph Atlassian Document Format node.

    Jira Cloud REST API v3 (which this client targets, matching how
    _parse_issue/_parse_comment already read descriptions and comments back
    as ADF) requires description and comment bodies to be ADF objects, not
    plain strings. ``@[Name](accountId)`` markup becomes a real ADF mention
    node; ``names`` (the user directory's names) overrides the agent's label.
    """
    matches = list(atlassian_users.MENTION_MARKUP_RE.finditer(text))
    if not matches:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    else:
        content = []
        pos = 0
        for m in matches:
            if m.start() > pos:
                content.append({"type": "text", "text": text[pos:m.start()]})
            label, account_id = m.group(1), m.group(2)
            name = names[account_id] if names and account_id in names else label
            content.append(
                {"type": "mention", "attrs": {"id": account_id, "text": "@" + name}}
            )
            pos = m.end()
        if pos < len(text):
            content.append({"type": "text", "text": text[pos:]})
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": content}],
    }


def simplify_field_value(value: Any, names: Mapping[str, str] | None = None) -> Any:
    """Reduce a raw Jira field value to what an agent needs: names instead of objects, no
    API links, avatars or email addresses. ``names`` resolves ADF mentions."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [simplify_field_value(v, names) for v in value]
    if isinstance(value, dict):
        if value.get("type") == "doc":
            return JiraClient._extract_adf_text(value, names)
        if "accountId" in value:
            return mask_emails(value.get("displayName") or "", value["accountId"]) or UNKNOWN_USER_LABEL
        if "value" in value:
            head = simplify_field_value(value["value"], names)
            child = value.get("child")
            if isinstance(child, dict) and "value" in child:
                return f"{head} > {simplify_field_value(child['value'], names)}"
            return head
        if "name" in value and not _SLA_KEYS & value.keys():
            return simplify_field_value(value["name"], names)
        if "key" in value:
            return value["key"]
        return {
            k: simplify_field_value(v, names)
            for k, v in value.items()
            if k not in _DROPPED_VALUE_KEYS
        }
    return str(value)


@dataclass
class JiraProject:
    key: str
    name: str
    project_type: str = ""
    description: str = ""
    lead: str = ""

    def short_summary(self) -> str:
        return f"[{self.key}] {self.name}"


@dataclass
class JiraIssue:
    key: str
    summary: str
    status: str
    issue_type: str
    priority: str = ""
    assignee: str = ""
    reporter: str = ""
    description: str = ""
    labels: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    url: str = ""

    def __post_init__(self) -> None:
        # Not a dataclass field, so asdict() (what the agent receives) never carries it: the
        # description as the approval preview shows it, mentions as @Name and every other
        # character as written. None when the description has no ADF form.
        self.display_description: str | None = None
        # Not a dataclass field either: the requested extra fields, {key: simplified value}, set only by a
        # search or get_issue that asked for them (keys from JiraClient.extra_field_keys). None otherwise.
        self.extra_fields: dict[str, Any] | None = None

    def short_summary(self) -> str:
        snippet = self.summary[:60] + "…" if len(self.summary) > 60 else self.summary
        return f"{self.key} ({self.status}): {snippet}"


@dataclass
class JiraComment:
    id: str
    author: str
    body: str
    created: str = ""
    updated: str = ""

    def __post_init__(self) -> None:
        # as ``JiraIssue.display_description``: not a field, preview-only
        self.display_body: str | None = None


@dataclass
class JiraTransition:
    id: str
    name: str
    to_status: str


@dataclass
class JiraField:
    id: str            # "customfield_10016", "duedate"
    name: str          # "Story Points", "Due date"
    custom: bool
    type: str          # schema type: "number", "option", "array of user"; "" when Jira gives none
    jql_names: list[str] = field(default_factory=list)   # Jira's clauseNames, e.g. ["cf[10016]", "Story Points"]


class JiraClient:
    """Jira Cloud client backed by an Atlassian OAuth 2.0 bearer token.

    ``config`` merges the organization's Atlassian OAuth app credentials
    (``client_id``, ``client_secret``) with the per-user token
    (``access_token``, ``refresh_token``, ``cloud_id``, ``site_url``). When
    the access token has expired (Atlassian tokens are short-lived), the
    client refreshes it once and retries automatically; if ``token_file`` is
    given, the refreshed token is persisted back to disk so the next app
    launch doesn't need a fresh sign-in.
    """

    def __init__(
        self,
        config: dict[str, Any],
        token_file: str | None = None,
        user_directory: AtlassianUserDirectory | None = None,
    ) -> None:
        self._config = dict(config)
        self._token_file = token_file
        # Populated lazily by _get_field_descriptor: a site's field list
        # (system + custom) rarely changes within a process lifetime, so one
        # fetch covers every custom-field-by-name lookup for this client.
        self._field_cache: list[dict[str, Any]] | None = None
        access_token = self._config.get("access_token", "")
        cloud_id = self._config.get("cloud_id", "")
        site_url = (self._config.get("site_url") or "").rstrip("/")

        if not access_token or not cloud_id:
            raise JiraClientError(
                "Jira is not authenticated. Use Authenticate… in PrivacyFence Settings."
            )

        self._users = user_directory or AtlassianUserDirectory(cloud_id=cloud_id)
        api_url = f"https://api.atlassian.com/ex/jira/{cloud_id}"
        self._base_url = site_url or api_url
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {access_token}"
        try:
            self._client = Jira(url=api_url, session=self._session, cloud=True, api_version="3")
        except Exception as exc:
            raise JiraClientError(f"Failed to initialise Jira client: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Token refresh
    # ------------------------------------------------------------------ #

    def _try_refresh(self) -> bool:
        """Attempt to refresh the access token in place. Returns True on success."""
        client_id = self._config.get("client_id", "")
        client_secret = self._config.get("client_secret", "")
        refresh_token = self._config.get("refresh_token", "")
        if self._token_file:
            # The token file is shared with ConfluenceClient; if it already
            # refreshed (and Atlassian rotated the refresh token), pick up
            # its latest value instead of retrying a spent one.
            try:
                refresh_token = load_token_file(self._token_file).get("refresh_token") or refresh_token
            except AtlassianOAuthError:
                pass
        if not client_id or not client_secret or not refresh_token:
            return False
        try:
            data = atlassian_refresh(client_id, client_secret, refresh_token)
        except AtlassianOAuthError as exc:
            logger.warning("Jira token refresh failed: %s", exc)
            return False
        access_token = data.get("access_token", "")
        if not access_token:
            return False
        self._config["access_token"] = access_token
        self._config["refresh_token"] = data.get("refresh_token", refresh_token)
        self._session.headers["Authorization"] = f"Bearer {access_token}"
        if self._token_file:
            save_token_file(self._token_file, {
                "access_token": access_token,
                "refresh_token": self._config["refresh_token"],
                "cloud_id": self._config.get("cloud_id", ""),
                "site_url": self._config.get("site_url", ""),
                "account_email": self._config.get("account_email", ""),
            })
        logger.info("Jira access token refreshed")
        return True

    def _request(self, fn, *args: Any, **kwargs: Any) -> Any:
        """Call ``fn`` with one automatic refresh-and-retry on an expired token."""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if is_unauthorized(exc) and self._try_refresh():
                return fn(*args, **kwargs)
            raise

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def check_connection(self) -> str:
        """Verify credentials. Returns the site URL on success."""
        try:
            myself = self._request(self._client.myself)
            display = myself.get("displayName", "unknown user")
            logger.info("Connected to Jira at %s as %r", self._base_url, display)
            return f"{display} @ {self._base_url}"
        except Exception as exc:
            raise JiraClientError(f"Jira connection check failed: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Users
    # ------------------------------------------------------------------ #

    def _fetch_users_bulk(self, account_ids: list[str]) -> list[AtlassianUser]:
        return self._request(
            atlassian_users.fetch_users_bulk,
            self._session,
            self._config.get("cloud_id", ""),
            account_ids,
        )

    def resolve_user_names(self, account_ids: list[str]) -> dict[str, str]:
        """Map account ids to display names; ids that cannot be resolved are omitted."""
        return self._users.resolve(account_ids, self._fetch_users_bulk)

    def find_users(self, query: str, max_results: int = 10) -> list[AtlassianUser]:
        if not query or not query.strip():
            raise JiraClientError("find_users requires a non-empty query")
        max_results = max(1, min(max_results, atlassian_users.FIND_USERS_MAX_RESULTS))
        try:
            users = self._request(
                atlassian_users.search_users,
                self._session,
                self._config.get("cloud_id", ""),
                query,
                max_results,
            )
        except Exception as exc:
            raise JiraClientError(
                f"find_users failed: {atlassian_users.redact_query(exc, query)}"
            ) from exc
        self._users.remember(users)
        return users

    def refresh_user_cache(self) -> int:
        try:
            return self._users.refresh(self._fetch_users_bulk)
        except AtlassianUsersError as exc:
            raise JiraClientError(str(exc)) from exc

    # ------------------------------------------------------------------ #
    # Projects
    # ------------------------------------------------------------------ #

    def list_projects(self, max_results: int = 50) -> list[JiraProject]:
        max_results = max(1, min(max_results, 500))
        try:
            raw = self._request(self._client.projects, included_archived=None)
        except Exception as exc:
            raise JiraClientError(f"list_projects failed: {exc}") from exc
        projects = [self._parse_project(p) for p in (raw or [])][:max_results]
        logger.info("list_projects returned %d project(s)", len(projects))
        return projects

    # ------------------------------------------------------------------ #
    # Issues
    # ------------------------------------------------------------------ #

    def search_issues_page(
        self,
        jql: str,
        page_size: int = 100,
        page_token: str | None = None,
        extra: list[JiraField] | None = None,
    ) -> tuple[list[JiraIssue], str | None]:
        """One provider request: a page of issues and the next page token (None when last)."""
        if not jql:
            raise JiraClientError("search_issues_page requires a non-empty JQL query")
        try:
            result = self._request(
                self._client.enhanced_jql,
                jql,
                nextPageToken=page_token,
                limit=page_size,
                fields=[*SEARCH_FIELDS, *(f.id for f in extra or [] if f.id not in SEARCH_FIELDS)],
            )
        except Exception as exc:
            raise JiraClientError(f"search_issues_page failed: {exc}") from exc
        issues = []
        for raw in result.get("issues") or []:
            issue = self._parse_issue(raw)
            if extra is not None:
                issue.extra_fields = self._extra_fields(raw.get("fields") or {}, extra)
            issues.append(issue)
        next_token = None if result.get("isLast", True) else result.get("nextPageToken")
        return issues, next_token or None

    def search_issues(
        self, jql: str, max_results: int = 20, fields: list[str] | None = None
    ) -> list[JiraIssue]:
        if not jql:
            raise JiraClientError("search_issues requires a non-empty JQL query")
        max_results = max(1, min(max_results, 500))
        extra = self.resolve_fields(fields) if fields else None
        issues: list[JiraIssue] = []
        page_token: str | None = None
        try:
            for _ in range(MAX_PAGES):
                page, page_token = self.search_issues_page(
                    jql, min(max_results - len(issues), 100), page_token, extra
                )
                issues.extend(page)
                if not page_token or len(issues) >= max_results:
                    break
        except JiraClientError as exc:
            raise JiraClientError(f"search_issues failed: {exc}") from exc
        issues = issues[:max_results]
        logger.info("search_issues jql=%r returned %d issue(s)", jql, len(issues))
        return issues

    def get_issue(self, issue_key: str, fields: list[str] | None = None) -> JiraIssue:
        if not issue_key:
            raise JiraClientError("get_issue requires an issue key")
        extra = self.resolve_fields(fields) if fields else None
        try:
            raw = self._request(self._client.issue, issue_key)
        except Exception as exc:
            raise JiraClientError(f"get_issue({issue_key!r}) failed: {exc}") from exc
        description = (raw.get("fields") or {}).get("description")
        ids = self._collect_adf_mention_ids(description) if isinstance(description, dict) else []
        names = self.resolve_user_names(ids) if ids else None
        issue = self._parse_issue(raw, include_description=True, names=names)
        if extra is not None:
            issue.extra_fields = self._extra_fields(raw.get("fields") or {}, extra)
        logger.info("get_issue %s: %s", issue_key, issue.short_summary())
        return issue

    def get_issue_comments(self, issue_key: str) -> list[JiraComment]:
        if not issue_key:
            raise JiraClientError("get_issue_comments requires an issue key")
        try:
            raw = self._request(self._client.issue, issue_key, fields="comment")
            comments_raw = (raw.get("fields", {}).get("comment") or {}).get("comments", [])
        except Exception as exc:
            raise JiraClientError(f"get_issue_comments({issue_key!r}) failed: {exc}") from exc
        ids: list[str] = []
        for c in comments_raw:
            body = c.get("body")
            if isinstance(body, dict):
                ids.extend(i for i in self._collect_adf_mention_ids(body) if i not in ids)
        names = self.resolve_user_names(ids) if ids else None
        comments = [self._parse_comment(c, names) for c in comments_raw]
        logger.info("get_issue_comments %s returned %d comment(s)", issue_key, len(comments))
        return comments

    def create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str = "Task",
        description: str = "",
        priority: str = "",
        assignee_account_id: str = "",
        labels: list[str] | None = None,
        mention_names: Mapping[str, str] | None = None,
    ) -> JiraIssue:
        if not project_key or not summary:
            raise JiraClientError("create_issue requires project_key and summary")
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
        if description:
            fields["description"] = _text_to_adf(description, mention_names)
        if priority:
            fields["priority"] = {"name": priority}
        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}
        if labels:
            fields["labels"] = labels
        try:
            raw = self._request(self._client.create_issue, fields=fields)
        except Exception as exc:
            raise JiraClientError(f"create_issue failed: {exc}") from exc
        key = raw.get("key", "")
        logger.info("create_issue created %s", key)
        return self.get_issue(key)

    def add_comment(
        self, issue_key: str, body: str, mention_names: Mapping[str, str] | None = None
    ) -> JiraComment:
        if not issue_key or not body:
            raise JiraClientError("add_comment requires issue_key and body")
        try:
            raw = self._request(
                self._client.issue_add_comment, issue_key, _text_to_adf(body, mention_names)
            )
        except Exception as exc:
            raise JiraClientError(f"add_comment({issue_key!r}) failed: {exc}") from exc
        comment = self._parse_comment(raw)
        logger.info("add_comment: added comment %s to %s", comment.id, issue_key)
        return comment

    def update_issue(self, issue_key: str, fields: dict[str, Any]) -> JiraIssue:
        if not issue_key or not fields:
            raise JiraClientError("update_issue requires issue_key and fields")
        try:
            self._request(self._client.update_issue_field, issue_key, fields)
        except Exception as exc:
            raise JiraClientError(f"update_issue({issue_key!r}) failed: {exc}") from exc
        logger.info("update_issue %s: updated fields %s", issue_key, list(fields.keys()))
        return self.get_issue(issue_key)

    def resolve_custom_field(self, field_name: str, value: Any) -> tuple[str, Any]:
        """Resolve a Jira Cloud field's display name to its API id and shape
        ``value`` for that field's schema, so callers only ever deal in the
        field's plain display name (as seen in the Jira UI) and a plain
        Python value -- never the internal ``customfield_NNNNN`` id.

        Select-list fields (single- and multi-option) need
        ``{"value": ...}`` wrappers rather than a bare string/list; this is
        a best-effort shaping covering those and generic array fields.
        Fields needing a structured reference the caller can't supply by
        name alone (e.g. a user-picker field, which needs an ``accountId``)
        are passed through as-is and will surface Jira's own validation
        error if the shape is wrong.
        """
        field = self._get_field_descriptor(field_name)
        field_id = field.get("id", "")
        schema = field.get("schema") or {}
        field_type = schema.get("type", "")
        items_type = schema.get("items", "")
        if field_type == "option":
            coerced: Any = {"value": value}
        elif field_type == "array" and items_type == "option":
            values = value if isinstance(value, list) else [value]
            coerced = [{"value": v} for v in values]
        elif field_type == "array" and not isinstance(value, list):
            coerced = [value]
        else:
            coerced = value
        return field_id, coerced

    def custom_field_kind(self, field_name: str) -> str:
        """``"user"`` for a single-user field, ``"user_list"`` for a multi-user picker,
        else ``"other"``. Lets callers hold person-valued fields to the accountId shape."""
        schema = self._get_field_descriptor(field_name).get("schema") or {}
        if schema.get("type") == "user":
            return "user"
        if schema.get("type") == "array" and schema.get("items") == "user":
            return "user_list"
        return "other"

    def _all_fields(self) -> list[dict[str, Any]]:
        if self._field_cache is None:
            try:
                raw = self._request(self._client.get_all_fields)
            except Exception as exc:
                raise JiraClientError(f"failed to list Jira fields: {exc}") from exc
            self._field_cache = raw or []
        return self._field_cache

    def _get_field_descriptor(self, field_name: str) -> dict[str, Any]:
        matches = [f for f in self._all_fields() if (f.get("name") or "").lower() == field_name.lower()]
        if not matches:
            raise JiraClientError(
                f"no Jira field named {field_name!r}; jira_list_fields lists the field names"
            )
        if len(matches) > 1:
            ids = ", ".join(m.get("id", "") for m in matches)
            raise JiraClientError(
                f"multiple Jira fields are named {field_name!r} ({ids}); rename one in Jira "
                "or ask your Jira admin to disambiguate them"
            )
        return matches[0]

    @staticmethod
    def _to_jira_field(raw: dict[str, Any]) -> JiraField:
        schema = raw.get("schema") or {}
        field_type = schema.get("type", "")
        if field_type == "array" and schema.get("items"):
            field_type = f"array of {schema['items']}"
        return JiraField(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            custom=bool(raw.get("custom")),
            type=field_type,
            jql_names=list(raw.get("clauseNames") or []),
        )

    def list_fields(
        self, query: str = "", custom_only: bool = False, max_results: int = 50
    ) -> list[JiraField]:
        max_results = max(1, min(max_results, 200))
        needle = query.strip().lower()
        fields = [
            self._to_jira_field(raw)
            for raw in self._all_fields()
            if needle in (raw.get("name") or "").lower() or needle in (raw.get("id") or "").lower()
        ]
        if custom_only:
            fields = [f for f in fields if f.custom]
        fields.sort(key=lambda f: (f.name.lower(), f.id))
        fields = fields[:max_results]
        logger.info("list_fields returned %d field(s)", len(fields))
        return fields

    def resolve_fields(self, refs: list[str]) -> list[JiraField]:
        """Turn field names, ids and ``cf[N]`` JQL ids into fields, in the order given."""
        resolved: list[JiraField] = []
        for ref in refs:
            ref = ref.strip()
            if not ref:
                raise JiraClientError("field names must not be empty")
            all_fields = self._all_fields()
            cf = _CF_JQL_RE.fullmatch(ref)
            if cf:
                wanted_id = f"customfield_{cf.group(1)}"
                match = next((f for f in all_fields if f.get("id") == wanted_id), None)
                if match is None:
                    raise JiraClientError(
                        f"no Jira field with id {ref!r}; jira_list_fields lists the field names"
                    )
            else:
                match = next((f for f in all_fields if f.get("id") == ref), None)
            if match is None:
                by_name = [f for f in all_fields if (f.get("name") or "").lower() == ref.lower()]
                if not by_name:
                    raise JiraClientError(
                        f"no Jira field named {ref!r}; jira_list_fields lists the field names"
                    )
                if len(by_name) > 1:
                    ids = ", ".join(m.get("id", "") for m in by_name)
                    raise JiraClientError(
                        f"multiple Jira fields are named {ref!r} ({ids}); pass one of these ids instead"
                    )
                match = by_name[0]
            jira_field = self._to_jira_field(match)
            if all(jira_field.id != r.id for r in resolved):
                resolved.append(jira_field)
        return resolved

    @staticmethod
    def extra_field_keys(wanted: list[JiraField]) -> list[str]:
        """The key each requested field has in ``extra_fields``: its name, or ``name (id)`` when
        two requested fields share a name."""
        counts: dict[str, int] = {}
        for f in wanted:
            counts[f.name] = counts.get(f.name, 0) + 1
        return [f"{f.name} ({f.id})" if counts[f.name] > 1 else f.name for f in wanted]

    def _extra_fields(self, raw_fields: Mapping[str, Any], wanted: list[JiraField]) -> dict[str, Any]:
        ids: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, list):
                for v in value:
                    collect(v)
            elif isinstance(value, dict) and value.get("type") == "doc":
                ids.extend(i for i in self._collect_adf_mention_ids(value) if i not in ids)

        for f in wanted:
            collect(raw_fields.get(f.id))
        names = self.resolve_user_names(ids) if ids else None
        return {
            k: simplify_field_value(raw_fields.get(f.id), names)
            for k, f in zip(self.extra_field_keys(wanted), wanted, strict=True)
        }

    def get_transitions(self, issue_key: str) -> list[JiraTransition]:
        if not issue_key:
            raise JiraClientError("get_transitions requires an issue key")
        try:
            raw = self._request(self._client.get_issue_transitions, issue_key)
        except Exception as exc:
            raise JiraClientError(f"get_transitions({issue_key!r}) failed: {exc}") from exc
        transitions = [
            JiraTransition(id=str(t["id"]), name=t["name"], to_status=t["to"])
            for t in (raw or [])
        ]
        logger.info("get_transitions %s returned %d transition(s)", issue_key, len(transitions))
        return transitions

    def transition_issue(self, issue_key: str, transition_name: str) -> JiraIssue:
        if not issue_key or not transition_name:
            raise JiraClientError("transition_issue requires issue_key and transition_name")
        transitions = self.get_transitions(issue_key)
        match = next((t for t in transitions if t.name.lower() == transition_name.lower()), None)
        if match is None:
            available = ", ".join(t.name for t in transitions) or "(none available)"
            raise JiraClientError(
                f"transition_issue({issue_key!r}): {transition_name!r} is not a valid transition "
                f"from the issue's current status. Available: {available}"
            )
        try:
            self._request(self._client.set_issue_status_by_transition_id, issue_key, match.id)
        except Exception as exc:
            raise JiraClientError(f"transition_issue({issue_key!r}) failed: {exc}") from exc
        logger.info("transition_issue %s: %s -> %s", issue_key, transition_name, match.to_status)
        return self.get_issue(issue_key)

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #

    def _parse_project(self, raw: dict[str, Any]) -> JiraProject:
        return JiraProject(
            key=raw.get("key", ""),
            name=raw.get("name", ""),
            project_type=raw.get("projectTypeKey", ""),
            description=raw.get("description", "") or "",
            lead=(raw.get("lead") or {}).get("displayName", ""),
        )

    def _parse_issue(
        self,
        raw: dict[str, Any],
        include_description: bool = False,
        names: Mapping[str, str] | None = None,
    ) -> JiraIssue:
        f = raw.get("fields") or {}
        key = raw.get("key", "")
        desc = ""
        display_desc = None
        if include_description:
            desc_raw = f.get("description")
            if isinstance(desc_raw, str):
                desc = desc_raw
            elif isinstance(desc_raw, dict):
                desc = self._extract_adf_text(desc_raw, names)
                display_desc = self._extract_adf_text(desc_raw, names, display=True)
        issue = JiraIssue(
            key=key,
            summary=f.get("summary", ""),
            status=(f.get("status") or {}).get("name", ""),
            issue_type=(f.get("issuetype") or {}).get("name", ""),
            priority=(f.get("priority") or {}).get("name", ""),
            assignee=(f.get("assignee") or {}).get("displayName", ""),
            reporter=(f.get("reporter") or {}).get("displayName", ""),
            description=desc,
            labels=f.get("labels") or [],
            created=f.get("created", ""),
            updated=f.get("updated", ""),
            url=f"{self._base_url}/browse/{key}" if key else "",
        )
        issue.display_description = display_desc
        return issue

    @staticmethod
    def _parse_comment(raw: dict[str, Any], names: Mapping[str, str] | None = None) -> JiraComment:
        body_raw = raw.get("body", "")
        display_body = None
        if isinstance(body_raw, dict):
            body = JiraClient._extract_adf_text(body_raw, names)
            display_body = JiraClient._extract_adf_text(body_raw, names, display=True)
        else:
            body = str(body_raw)
        comment = JiraComment(
            id=raw.get("id", ""),
            author=(raw.get("author") or {}).get("displayName", ""),
            body=body,
            created=raw.get("created", ""),
            updated=raw.get("updated", ""),
        )
        comment.display_body = display_body
        return comment

    @staticmethod
    def _extract_adf_text(
        node: dict[str, Any], names: Mapping[str, str] | None = None, display: bool = False
    ) -> str:
        """Extract plain text from an Atlassian Document Format node.

        A mention renders as ``@[Name](accountId)``; the name comes from
        ``names`` when known, else from the label Jira put on the node. With ``display`` it
        renders ``@Name`` instead, for the approval preview, so literal text that merely looks
        like the markup is left as written.
        """
        if not isinstance(node, dict):
            return str(node)
        if node.get("type") == "text":
            return node.get("text", "")
        if node.get("type") == "mention":
            attrs = node.get("attrs") or {}
            account_id = attrs.get("id")
            label = attrs.get("text")
            label = label if isinstance(label, str) else ""
            if not isinstance(account_id, str) or not ACCOUNT_ID_RE.fullmatch(account_id):
                return mask_emails(label, "")
            # Jira's own label can be an email (service-desk customers): mask it like a directory name.
            name = (names or {}).get(account_id) or mask_emails(label.lstrip("@"), account_id) or UNKNOWN_USER_LABEL
            return "@" + name if display else mention_markup(name, account_id)
        parts: list[str] = []
        for child in node.get("content") or []:
            parts.append(JiraClient._extract_adf_text(child, names, display))
        return " ".join(p for p in parts if p)

    @staticmethod
    def _collect_adf_mention_ids(node: Any) -> list[str]:
        """Every valid mention account id in an ADF tree, in document order, deduplicated."""
        found: list[str] = []

        def walk(n: Any) -> None:
            if not isinstance(n, dict):
                return
            if n.get("type") == "mention":
                account_id = (n.get("attrs") or {}).get("id")
                if (
                    isinstance(account_id, str)
                    and ACCOUNT_ID_RE.fullmatch(account_id)
                    and account_id not in found
                ):
                    found.append(account_id)
            for child in n.get("content") or []:
                walk(child)

        walk(node)
        return found
