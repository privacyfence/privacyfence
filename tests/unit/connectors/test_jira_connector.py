"""Unit tests for privacyfence.connectors.jira.JiraConnector.

Same approach as the other connector tests: JiraClient is mocked and
gate.gated_call is stubbed to capture what's sent into the gate.
JiraIssue keeps reporter/assignee/description as flat top-level fields
(unlike SalesforceRecord's nested "fields" dict), so asdict(issue)
already matches what auto_accept's _rule_i_am_reporter/_rule_i_am_assignee
expect -- no equivalent bug here.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from privacyfence import atlassian_users as au
from privacyfence.atlassian_users import AtlassianUser
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import jira as jira_module
from privacyfence.connectors.jira import JiraConnector
from privacyfence.jira_client import (
    JiraClient,
    JiraClientError,
    JiraComment,
    JiraIssue,
    JiraProject,
    JiraTransition,
)

from ...helpers import (
    assert_all_tools_leave_an_audit_trail,
    assert_no_placeholder_fields,
    assert_tool_definitions_complete,
)


def make_connector(my_email="me@example.com"):
    client = MagicMock()
    client.custom_field_kind.return_value = "other"
    connector = JiraConnector(client)
    connector.my_email = my_email
    return connector, client


def make_real_client(config: dict | None = None) -> JiraClient:
    """A real JiraClient (real _parse_issue and friends) with only the
    underlying atlassian-python-api object mocked -- same pattern as
    test_jira_client.py's make_client(). Used by TestFieldCompleteness to
    exercise the real raw-response -> dataclass -> popup-preview path end
    to end, instead of a hand-built JiraIssue like every other test here.
    """
    base = {"access_token": "tok", "cloud_id": "cloud-1", "site_url": "https://acme.atlassian.net"}
    base.update(config or {})
    client = JiraClient(config=base)
    client._client = MagicMock()
    return client


def make_issue(**overrides):
    defaults = dict(
        key="ENG-42", summary="Fix login bug", status="In Progress", issue_type="Bug",
        priority="High", assignee="bob@example.com", reporter="alice@example.com",
        description="Users can't log in with SSO.",
    )
    defaults.update(overrides)
    return JiraIssue(**defaults)


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(jira_module, "gated_call", fake_gated_call)
    return calls


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Jira tool"):
            await connector.call("jira_does_not_exist", {})


JIRA_SIBLINGS: dict[str, tuple[str, ...]] = {
    "jira_list_projects": ("jira_search_issues",),
    "jira_search_issues": ("jira_get_issue",),
    "jira_get_issue": ("jira_search_issues",),
    "jira_create_issue": ("jira_list_projects",),
    "jira_update_issue": ("jira_transition_issue",),
    "jira_add_comment": ("jira_update_issue",),
    "jira_get_transitions": ("jira_transition_issue",),
    "jira_transition_issue": ("jira_get_transitions",),
}


class TestToolDefinitions:
    """What an AI client reads to choose and call these tools: every parameter described, what
    each tool returns, the approval wording its gate implies, and the related tool to use
    instead. Glama's Tool Definition Quality Score grades exactly this."""

    def test_every_tool_definition_is_complete(self):
        assert_tool_definitions_complete(JiraConnector(MagicMock()), JIRA_SIBLINGS)


class TestAutoTools:
    async def test_list_projects(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_projects.return_value = [JiraProject(key="ENG", name="Engineering")]

        result = await connector.call("jira_list_projects", {"max_results": 10})

        assert result == [{"key": "ENG", "name": "Engineering", "project_type": "", "description": "", "lead": ""}]
        client.list_projects.assert_called_once_with(10)
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]

    async def test_search_issues(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.search_issues.return_value = [make_issue()]

        result = await connector.call("jira_search_issues", {"jql": "project = ENG", "max_results": 5})

        assert result[0]["key"] == "ENG-42"
        client.search_issues.assert_called_once_with("project = ENG", 5)

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_projects.side_effect = JiraClientError("unauthorized")

        with pytest.raises(RuntimeError, match="unauthorized"):
            await connector.call("jira_list_projects", {})

    async def test_get_transitions(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.get_transitions.return_value = [
            JiraTransition(id="11", name="Start Progress", to_status="In Progress"),
        ]

        result = await connector.call("jira_get_transitions", {"issue_key": "ENG-42"})

        assert result == [{"id": "11", "name": "Start Progress", "to_status": "In Progress"}]
        client.get_transitions.assert_called_once_with("ENG-42")
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[-1]


class TestFindUsers:
    async def test_returns_users_audits_and_never_gates(self, tmp_path, gated_call_spy):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.find_users.return_value = [AtlassianUser(account_id="acc-jane-0001", display_name="Jane Doe")]

        result = await connector.call("jira_find_users", {"query": "jane", "max_results": 5})

        assert result == [
            {"account_id": "acc-jane-0001", "display_name": "Jane Doe", "active": True, "account_type": ""},
        ]
        client.find_users.assert_called_once_with("jane", 5)
        assert gated_call_spy == []
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"tool": "jira_find_users"' in entries[-1]
        assert '"decision": "auto_accepted"' in entries[-1]

    async def test_audit_entry_never_holds_the_search_text(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.find_users.return_value = []

        await connector.call("jira_find_users", {"query": "jane@example.com"})

        entry = json.loads((tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert entry["summary"] == "Find users"
        assert entry["sender"] == "0 user(s)"
        for part in ("jane", "example", "@"):
            assert part not in json.dumps(entry)

    async def test_customer_account_email_name_is_masked(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.find_users.return_value = [
            au.parse_user({"accountId": "acc-cust-0001", "displayName": "jo@example.com",
                           "accountType": "customer"})]

        result = await connector.call("jira_find_users", {"query": "jo"})

        assert "@" not in json.dumps(result)
        assert result[0]["display_name"] == "Customer account 0001"

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.find_users.side_effect = JiraClientError("find_users failed: boom")

        with pytest.raises(RuntimeError, match="find_users failed"):
            await connector.call("jira_find_users", {"query": "jane"})


class TestRefreshUserCache:
    async def test_returns_count_audits_and_never_gates(self, tmp_path, gated_call_spy):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.refresh_user_cache.return_value = 7

        result = await connector.call("jira_refresh_user_cache", {})

        assert result == {"cached_users": 7}
        assert gated_call_spy == []
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"tool": "jira_refresh_user_cache"' in entries[-1]

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.refresh_user_cache.side_effect = JiraClientError("refresh already in progress")

        with pytest.raises(RuntimeError, match="already in progress"):
            await connector.call("jira_refresh_user_cache", {})


class TestGetIssue:
    async def test_mentions_show_names_in_preview_but_result_keeps_markup(self, gated_call_spy):
        connector, client = make_connector()
        markup = "@[Jane Doe](acc-jane-0001)"
        client.get_issue.return_value = make_issue(description=f"Ask {markup} about it")
        client.get_issue_comments.return_value = [
            JiraComment(id="c1", author="bob", body=f"cc {markup}", created="2026-07-01"),
        ]

        client.get_issue.return_value.display_description = "Ask @Jane Doe about it"
        client.get_issue_comments.return_value[0].display_body = "cc @Jane Doe"

        result = await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        kwargs = gated_call_spy[0]
        assert "Ask @Jane Doe about it" in kwargs["details_text"]
        assert "acc-jane-0001" not in kwargs["details_text"]
        assert "@Jane Doe" in kwargs["pii_scan_text"]
        assert kwargs["preview_blocks"][2] == {"type": "text", "text": "Ask @Jane Doe about it"}
        assert kwargs["preview_blocks"][3]["rows"][0][2] == "cc @Jane Doe"
        assert result["description"] == f"Ask {markup} about it"
        assert result["comments"][0]["body"] == f"cc {markup}"

    async def test_preview_fields(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.get_issue_comments.return_value = [
            JiraComment(id="c1", author="bob@example.com", body="Looking into it", created="2026-07-01"),
        ]

        await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {
            "Project": "ENG",  # derived from issue_key prefix (JiraIssue has no project_name field)
            "Key": "ENG-42", "Summary": "Fix login bug",
            "Status": "In Progress", "Assignee": "bob@example.com",
        }
        assert kwargs["gate"] == "review"
        assert kwargs["args"] == {"issue_key": "ENG-42"}
        assert kwargs["sender"] == "alice@example.com"  # reporter takes priority

    async def test_new_info_has_description_and_comments_summary(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.get_issue_comments.return_value = [
            JiraComment(id="c1", author="bob@example.com", body="Looking into it", created="2026-07-01"),
        ]

        await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        kwargs = gated_call_spy[0]
        # Fixed summary sentences, not the literal text -- the real
        # description/comment content lives only in preview_blocks below
        # (duplicating it into new_info too would just repeat the same full
        # text twice, same reasoning Comments already had).
        assert kwargs["new_info"]["Description"] == "Full description text"
        assert kwargs["new_info"]["Comments"] == "Author, created date, and body per comment"
        # v2's right pane: Reporter as a label-styled field, Description as
        # a heading + paragraph, Comments as its own table -- interleaved
        # via preview_blocks, not a flat text blob (see connectors/jira.py).
        assert kwargs["preview_blocks"] == [
            {"type": "field", "label": "Reporter", "value": "alice@example.com"},
            {"type": "heading", "label": "Description"},
            {"type": "text", "text": "Users can't log in with SSO."},
            {
                "type": "table", "caption": "Comments (1)", "headers": ["Author", "Date", "Comment"],
                "rows": [["bob@example.com", "2026-07-01", "Looking into it"]],
            },
        ]

    async def test_no_comments_produces_no_table_block(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.get_issue_comments.return_value = []

        await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        blocks = gated_call_spy[0]["preview_blocks"]
        assert all(b["type"] != "table" for b in blocks)

    async def test_summary_truncated_in_preview_but_full_in_details(self, gated_call_spy):
        connector, client = make_connector()
        long_summary = "x" * 100
        client.get_issue.return_value = make_issue(summary=long_summary)
        client.get_issue_comments.return_value = []

        await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Summary"] == "x" * 80 + "…"
        assert long_summary in kwargs["details_text"]

    async def test_unassigned_shows_placeholder(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(assignee="")
        client.get_issue_comments.return_value = []

        await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        assert gated_call_spy[0]["preview"]["Assignee"] == "(unassigned)"

    async def test_sender_falls_back_to_assignee_then_issue_key(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(reporter="", assignee="bob@example.com")
        client.get_issue_comments.return_value = []
        await connector.call("jira_get_issue", {"issue_key": "ENG-42"})
        assert gated_call_spy[0]["sender"] == "bob@example.com"

        client.get_issue.return_value = make_issue(reporter="", assignee="")
        await connector.call("jira_get_issue", {"issue_key": "ENG-99"})
        assert gated_call_spy[1]["sender"] == "ENG-99"

    async def test_result_includes_comments_and_matches_raw_and_filtered(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.get_issue_comments.return_value = [
            JiraComment(id="c1", author="bob@example.com", body="ack", created="2026-07-01"),
        ]

        result = await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        assert result["comments"] == [{"id": "c1", "author": "bob@example.com", "body": "ack",
                                        "created": "2026-07-01", "updated": ""}]
        assert result["key"] == "ENG-42"
        kwargs = gated_call_spy[0]
        assert kwargs["raw_data"] is kwargs["filtered_data"]

    async def test_pii_scan_text_is_description_and_comments_only(self, gated_call_spy):
        # reporter/assignee/comment author default to email addresses,
        # present on every issue regardless of content -- the PII scan
        # must not see them, only the description and comment bodies.
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(description="nothing sensitive")
        client.get_issue_comments.return_value = [
            JiraComment(id="c1", author="bob@example.com", body="still nothing sensitive", created="2026-07-01"),
        ]

        await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        kwargs = gated_call_spy[0]
        assert "nothing sensitive" in kwargs["pii_scan_text"]
        assert "still nothing sensitive" in kwargs["pii_scan_text"]
        assert "alice@example.com" in kwargs["details_text"]  # reporter, still shown in the popup
        assert "alice@example.com" not in kwargs["pii_scan_text"]
        assert "bob@example.com" not in kwargs["pii_scan_text"]  # assignee and comment author

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_issue.side_effect = JiraClientError("issue not found")

        with pytest.raises(RuntimeError, match="issue not found"):
            await connector.call("jira_get_issue", {"issue_key": "ENG-1"})


class TestCreateIssue:
    async def test_preview_omits_priority_when_absent(self, gated_call_spy):
        connector, client = make_connector()
        client.create_issue.return_value = make_issue(key="ENG-100")

        await connector.call("jira_create_issue", {
            "project_key": "ENG", "summary": "New bug", "issue_type": "Bug",
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Project": "ENG", "Type": "Bug", "Summary": "New bug"}
        assert kwargs["gate"] == "popup"
        assert "Priority" not in kwargs["preview"]

    async def test_preview_includes_priority_when_present(self, gated_call_spy):
        connector, client = make_connector()
        client.create_issue.return_value = make_issue(key="ENG-100")

        await connector.call("jira_create_issue", {
            "project_key": "ENG", "summary": "New bug", "priority": "High",
        })

        assert gated_call_spy[0]["preview"]["Priority"] == "High"

    async def test_result_is_serialized_issue(self, gated_call_spy):
        connector, client = make_connector()
        client.create_issue.return_value = make_issue(key="ENG-100")

        result = await connector.call("jira_create_issue", {"project_key": "ENG", "summary": "New bug"})

        assert result["key"] == "ENG-100"
        client.create_issue.assert_called_once_with("ENG", "New bug", "Task", "", "", "", None, {})

    async def test_description_renders_as_a_labeled_heading_block(self, gated_call_spy):
        # v2's right pane: a label-styled "Description" heading above the
        # body, same treatment jira_get_issue's own Description gets --
        # instead of plain unstyled prose with no field name at all.
        connector, client = make_connector()
        client.create_issue.return_value = make_issue(key="ENG-100")

        await connector.call("jira_create_issue", {
            "project_key": "ENG", "summary": "New bug", "description": "Steps to reproduce...",
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview_blocks"] == [
            {"type": "heading", "label": "Description"},
            {"type": "text", "text": "Steps to reproduce..."},
        ]
        assert kwargs["details_text"] == "Steps to reproduce..."

    async def test_no_description_produces_no_blocks(self, gated_call_spy):
        connector, client = make_connector()
        client.create_issue.return_value = make_issue(key="ENG-100")

        await connector.call("jira_create_issue", {"project_key": "ENG", "summary": "New bug"})

        assert gated_call_spy[0]["preview_blocks"] == []


class TestAddComment:
    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.add_comment.return_value = JiraComment(id="c2", author="me@example.com", body="ack")

        result = await connector.call("jira_add_comment", {"issue_key": "ENG-42", "body": "On it"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Issue": "ENG-42 — Fix login bug"}
        assert kwargs["gate"] == "popup"
        assert kwargs["details_text"] == "On it"
        assert result["id"] == "c2"
        client.add_comment.assert_called_once_with("ENG-42", "On it", {})


ACC = "557058:aaaaaaaa-bbbb"
ACC2 = "557058:cccccccc-dddd"


class TestWriteMentions:
    async def test_comment_shows_directory_name_not_agent_label(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.resolve_user_names.return_value = {ACC: "Real Name"}
        client.add_comment.return_value = JiraComment(id="c3", author="me", body="x")
        body = f"hi @[Fake Name]({ACC})"

        await connector.call("jira_add_comment", {"issue_key": "ENG-42", "body": body})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Mentions"] == "Real Name"
        assert kwargs["details_text"] == "hi @Real Name"
        assert kwargs["summary"] == "Comment on ENG-42: hi @Real Name"
        assert "Fake Name" not in str(
            [kwargs["summary"], kwargs["details_text"], kwargs["preview"]]
        )
        assert kwargs["raw_data"]["body"] == body
        client.resolve_user_names.assert_called_once_with([ACC])
        client.add_comment.assert_called_once_with("ENG-42", body, {ACC: "Real Name"})

    async def test_blank_named_account_refused_before_gate(self, gated_call_spy):
        from privacyfence.atlassian_users import AtlassianUser, AtlassianUserDirectory

        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        directory = AtlassianUserDirectory()
        client.resolve_user_names.side_effect = lambda ids: directory.resolve(
            ids, lambda chunk: [AtlassianUser(i, "  ") for i in chunk])

        with pytest.raises(ValueError, match="Unknown Atlassian account id"):
            await connector.call(
                "jira_add_comment", {"issue_key": "ENG-42", "body": f"@[X]({ACC})"},
            )
        assert gated_call_spy == []
        client.add_comment.assert_not_called()

    async def test_unresolvable_id_refused_before_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.resolve_user_names.return_value = {}

        with pytest.raises(ValueError, match="Unknown Atlassian account id"):
            await connector.call(
                "jira_add_comment", {"issue_key": "ENG-42", "body": f"@[X]({ACC})"},
            )
        assert gated_call_spy == []
        client.add_comment.assert_not_called()

    async def test_no_mentions_makes_no_lookup(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.add_comment.return_value = JiraComment(id="c3", author="me", body="x")

        await connector.call("jira_add_comment", {"issue_key": "ENG-42", "body": "plain"})

        client.resolve_user_names.assert_not_called()
        assert "Mentions" not in gated_call_spy[0]["preview"]

    async def test_create_with_assignee_and_mention(self, gated_call_spy):
        connector, client = make_connector()
        client.resolve_user_names.return_value = {ACC: "Real Name", ACC2: "Bob Real"}
        client.create_issue.return_value = make_issue(key="ENG-100")

        await connector.call("jira_create_issue", {
            "project_key": "ENG", "summary": "S", "description": f"cc @[Fake]({ACC})",
            "assignee_account_id": ACC2,
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Assignee"] == "Bob Real"
        assert kwargs["preview"]["Mentions"] == "Real Name"
        assert kwargs["args"]["assignee_account_id"] == ACC2
        assert kwargs["details_text"] == "cc @Real Name"
        assert kwargs["preview_blocks"][1]["text"] == "cc @Real Name"
        assert "Fake" not in str([kwargs["details_text"], kwargs["preview"], kwargs["summary"]])
        client.create_issue.assert_called_once_with(
            "ENG", "S", "Task", f"cc @[Fake]({ACC})", "", ACC2, None,
            {ACC: "Real Name", ACC2: "Bob Real"},
        )

    async def test_create_unresolvable_assignee_refused(self, gated_call_spy):
        connector, client = make_connector()
        client.resolve_user_names.return_value = {}

        with pytest.raises(ValueError, match="jira_find_users"):
            await connector.call("jira_create_issue", {
                "project_key": "ENG", "summary": "S", "assignee_account_id": ACC,
            })
        assert gated_call_spy == []

    async def test_update_assignee_only_and_description_mentions(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.update_issue.return_value = make_issue()
        client.resolve_user_names.return_value = {ACC: "Real Name"}

        await connector.call(
            "jira_update_issue", {"issue_key": "ENG-42", "assignee_account_id": ACC},
        )
        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Assignee"] == "→ Real Name"
        assert kwargs["raw_data"]["fields"] == {"assignee": {"accountId": ACC}}
        client.update_issue.assert_called_once_with("ENG-42", {"assignee": {"accountId": ACC}})

        await connector.call(
            "jira_update_issue", {"issue_key": "ENG-42", "description": f"ping @[Fake]({ACC})"},
        )
        kwargs = gated_call_spy[1]
        assert kwargs["preview"]["Mentions"] == "Real Name"
        assert kwargs["details_text"] == "ping @Real Name"
        node = kwargs["raw_data"]["fields"]["description"]["content"][0]["content"][1]
        assert node == {"type": "mention", "attrs": {"id": ACC, "text": "@Real Name"}}

    async def test_update_unresolvable_assignee_refused(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.resolve_user_names.return_value = {}

        with pytest.raises(ValueError, match="Unknown Atlassian account id"):
            await connector.call(
                "jira_update_issue", {"issue_key": "ENG-42", "assignee_account_id": ACC},
            )
        assert gated_call_spy == []


class TestUpdateIssue:
    async def test_requires_at_least_one_field(self):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()

        with pytest.raises(ValueError, match="at least one field"):
            await connector.call("jira_update_issue", {"issue_key": "ENG-42"})

    async def test_preview_shows_summary_diff(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(summary="Old summary")
        client.update_issue.return_value = make_issue(summary="New summary")

        await connector.call("jira_update_issue", {"issue_key": "ENG-42", "summary": "New summary"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Summary"] == "Old summary → New summary"
        assert kwargs["gate"] == "popup"
        # Regression: details_text used to unconditionally echo the (here,
        # empty) description argument, which gate.py's fallback turns into
        # a raw JSON dump of the update payload instead of a plain-language line.
        assert kwargs["details_text"] == "Summary will be updated; description is unchanged."
        assert "{" not in kwargs["details_text"]

    async def test_description_preview_is_placeholder_not_full_text(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.update_issue.return_value = make_issue()

        await connector.call("jira_update_issue", {"issue_key": "ENG-42", "description": "Confidential new details"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Description"] == "(updated — see below)"
        assert "Confidential new details" not in str(kwargs["preview"])
        assert kwargs["details_text"] == "Confidential new details"

    async def test_priority_sent_as_name_dict(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.update_issue.return_value = make_issue()

        await connector.call("jira_update_issue", {"issue_key": "ENG-42", "priority": "Low"})

        client.update_issue.assert_called_once_with("ENG-42", {"priority": {"name": "Low"}})
        assert gated_call_spy[0]["preview"]["Priority"] == "→ Low"
        assert gated_call_spy[0]["details_text"] == "Priority will be updated; description is unchanged."

    async def test_multi_field_update_lists_all_changed_fields_in_details(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(summary="Old summary")
        client.update_issue.return_value = make_issue()

        await connector.call(
            "jira_update_issue", {"issue_key": "ENG-42", "summary": "New summary", "priority": "High"},
        )

        assert gated_call_spy[0]["details_text"] == (
            "Summary, Priority will be updated; description is unchanged."
        )

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_issue.side_effect = JiraClientError("not found")

        with pytest.raises(RuntimeError, match="not found"):
            await connector.call("jira_update_issue", {"issue_key": "ENG-1", "summary": "x"})

    async def test_custom_fields_resolved_by_name_and_shown_in_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.update_issue.return_value = make_issue()
        client.resolve_custom_field.return_value = ("customfield_10016", 5)

        await connector.call("jira_update_issue", {
            "issue_key": "ENG-42", "custom_fields": '{"Story Points": 5}',
        })

        client.resolve_custom_field.assert_called_once_with("Story Points", 5)
        client.update_issue.assert_called_once_with("ENG-42", {"customfield_10016": 5})
        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Story Points (field)"] == "→ 5"
        assert kwargs["details_text"] == "Story Points will be updated; description is unchanged."

    async def test_custom_fields_combine_with_standard_fields(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(summary="Old summary")
        client.update_issue.return_value = make_issue()
        client.resolve_custom_field.return_value = ("customfield_10016", 5)

        await connector.call("jira_update_issue", {
            "issue_key": "ENG-42", "summary": "New summary", "custom_fields": '{"Story Points": 5}',
        })

        client.update_issue.assert_called_once_with(
            "ENG-42", {"summary": "New summary", "customfield_10016": 5}
        )
        assert gated_call_spy[0]["details_text"] == (
            "Summary, Story Points will be updated; description is unchanged."
        )

    async def test_invalid_custom_fields_json_raises(self):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()

        with pytest.raises(ValueError, match="custom_fields must be a JSON object"):
            await connector.call("jira_update_issue", {"issue_key": "ENG-42", "custom_fields": "not json"})

    async def test_custom_fields_json_array_raises(self):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()

        with pytest.raises(ValueError, match="custom_fields must be a JSON object"):
            await connector.call("jira_update_issue", {"issue_key": "ENG-42", "custom_fields": "[1, 2]"})

    async def test_unresolvable_custom_field_propagates_as_runtime_error(self):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.resolve_custom_field.side_effect = JiraClientError("no Jira field named 'Nope'")

        with pytest.raises(RuntimeError, match="no Jira field named"):
            await connector.call("jira_update_issue", {
                "issue_key": "ENG-42", "custom_fields": '{"Nope": 1}',
            })


class TestCustomFieldPeople:
    @staticmethod
    def _adf_mention(account_id):
        return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
            {"type": "mention", "attrs": {"id": account_id, "text": "@Jane Doe"}},
        ]}]}

    async def test_reviewer_failing_input_is_refused_and_never_sent(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.resolve_user_names.return_value = {ACC: "Jane Doe"}
        client.resolve_custom_field.side_effect = lambda name, value: (name.lower(), value)
        custom = json.dumps({"ASSIGNEE": {"accountId": ACC2}, "DESCRIPTION": self._adf_mention(ACC2)})

        with pytest.raises(ValueError, match="dedicated assignee_account_id parameter"):
            await connector.call("jira_update_issue", {
                "issue_key": "ENG-1", "description": f"cc @[x]({ACC})",
                "assignee_account_id": ACC, "custom_fields": custom,
            })
        assert gated_call_spy == []
        client.update_issue.assert_not_called()

    @pytest.mark.parametrize("field_id,param", [
        ("description", "description"), ("summary", "summary"), ("priority", "priority"),
    ])
    async def test_dedicated_parameter_fields_refused(self, gated_call_spy, field_id, param):
        connector, client = make_connector()
        client.resolve_custom_field.return_value = (field_id, "x")

        with pytest.raises(ValueError, match=f"dedicated {param} parameter"):
            await connector.call("jira_update_issue", {
                "issue_key": "ENG-1", "custom_fields": '{"Whatever": "x"}',
            })
        assert gated_call_spy == []

    async def test_same_field_twice_refused(self, gated_call_spy):
        connector, client = make_connector()
        client.resolve_custom_field.return_value = ("customfield_1", 1)

        with pytest.raises(ValueError, match="more than once"):
            await connector.call("jira_update_issue", {
                "issue_key": "ENG-1", "custom_fields": '{"A": 1, "a": 2}',
            })
        assert gated_call_spy == []

    async def test_unknown_id_in_custom_value_refused_before_card(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.resolve_user_names.return_value = {}
        client.resolve_custom_field.return_value = ("customfield_9", {"accountId": ACC})

        with pytest.raises(ValueError, match="Unknown Atlassian account id"):
            await connector.call("jira_update_issue", {
                "issue_key": "ENG-1", "custom_fields": json.dumps({"Approver": {"accountId": ACC}}),
            })
        assert gated_call_spy == []
        client.update_issue.assert_not_called()

    async def test_user_picker_list_resolved_and_named(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.update_issue.return_value = make_issue()
        client.resolve_user_names.return_value = {ACC: "Jane Doe", ACC2: "Bob Real"}
        client.custom_field_kind.return_value = "user_list"
        value = [{"accountId": ACC}, {"accountId": ACC2}, {"accountId": ACC}]
        client.resolve_custom_field.return_value = ("customfield_9", value)

        await connector.call("jira_update_issue", {
            "issue_key": "ENG-1", "custom_fields": json.dumps({"Reviewers": value}),
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Reviewers (field)"] == "→ @Jane Doe, @Bob Real, @Jane Doe"
        client.resolve_user_names.assert_called_once_with([ACC, ACC2])
        client.update_issue.assert_called_once_with("ENG-1", {"customfield_9": value})

    async def test_adf_mention_in_rich_text_custom_field_named(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.update_issue.return_value = make_issue()
        client.resolve_user_names.return_value = {ACC: "Jane Doe"}
        adf = self._adf_mention(ACC)
        client.resolve_custom_field.return_value = ("customfield_7", adf)

        await connector.call("jira_update_issue", {
            "issue_key": "ENG-1", "custom_fields": json.dumps({"Notes": adf}),
        })

        preview = gated_call_spy[0]["preview"]
        assert preview["Notes (field)"] == "→ (updated — see below); people: Jane Doe"
        assert gated_call_spy[0]["details_text"] == "Notes:\n@Jane Doe"

    @pytest.mark.parametrize("bad", [{"accountId": 5}, {"accountId": ""}])
    async def test_invalid_account_id_shape_refused(self, gated_call_spy, bad):
        connector, client = make_connector()
        client.resolve_custom_field.return_value = ("customfield_9", bad)

        with pytest.raises(ValueError, match="invalid account id"):
            await connector.call("jira_update_issue", {
                "issue_key": "ENG-1", "custom_fields": json.dumps({"P": bad}),
            })
        assert gated_call_spy == []

    async def test_plain_text_custom_field_still_works_without_lookup(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.update_issue.return_value = make_issue()
        client.resolve_custom_field.return_value = ("customfield_3", "hello")

        await connector.call("jira_update_issue", {
            "issue_key": "ENG-1", "custom_fields": '{"Notes": "hello"}',
        })

        client.resolve_user_names.assert_not_called()
        assert gated_call_spy[0]["preview"]["Notes (field)"] == "→ hello"


class TestCustomFieldUserShapes:
    @staticmethod
    def _setup(kind, names=None):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.update_issue.return_value = make_issue()
        client.resolve_user_names.return_value = {ACC: "Jane Doe", ACC2: "Bob Real"} if names is None else names
        client.custom_field_kind.return_value = kind
        client.resolve_custom_field.side_effect = lambda name, value: (
            name.lower(), value if kind != "user_list" or isinstance(value, list) else [value])
        return connector, client

    async def test_reporter_id_shape_refused_and_never_shown(self, gated_call_spy):
        connector, client = self._setup("user")
        custom = json.dumps({"Reporter": {"id": ACC, "displayName": "Jane Doe"}})

        with pytest.raises(ValueError, match=r'\{"accountId"'):
            await connector.call("jira_update_issue", {"issue_key": "E-1", "custom_fields": custom})
        assert gated_call_spy == []
        client.update_issue.assert_not_called()

    async def test_multi_user_picker_id_shape_refused(self, gated_call_spy):
        connector, client = self._setup("user_list")

        with pytest.raises(ValueError, match="user field"):
            await connector.call("jira_update_issue", {
                "issue_key": "E-1", "custom_fields": json.dumps({"Reviewers": [{"id": ACC}]}),
            })
        assert gated_call_spy == []

    @pytest.mark.parametrize("bad", [{"accountId": ACC, "displayName": "x"}, {"name": "jane"}, 5, ""])
    async def test_other_user_shapes_refused(self, gated_call_spy, bad):
        connector, _client = self._setup("user")

        with pytest.raises(ValueError, match="user field"):
            await connector.call("jira_update_issue", {
                "issue_key": "E-1", "custom_fields": json.dumps({"Reporter": bad}),
            })
        assert gated_call_spy == []

    async def test_bare_id_normalised_resolved_and_named(self, gated_call_spy):
        connector, client = self._setup("user")

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": json.dumps({"Approver": ACC}),
        })

        assert gated_call_spy[0]["preview"]["Approver (field)"] == "→ @Jane Doe"
        client.update_issue.assert_called_once_with("E-1", {"approver": {"accountId": ACC}})

    async def test_bare_unknown_id_refused_before_card(self, gated_call_spy):
        connector, _client = self._setup("user", names={})

        with pytest.raises(ValueError, match="Unknown Atlassian account id"):
            await connector.call("jira_update_issue", {
                "issue_key": "E-1", "custom_fields": json.dumps({"Approver": ACC}),
            })
        assert gated_call_spy == []

    async def test_user_field_can_be_cleared(self, gated_call_spy):
        connector, client = self._setup("user")

        await connector.call("jira_update_issue", {"issue_key": "E-1", "custom_fields": '{"Approver": null}'})

        client.update_issue.assert_called_once_with("E-1", {"approver": None})
        client.resolve_user_names.assert_not_called()

    async def test_multi_user_bare_ids(self, gated_call_spy):
        connector, client = self._setup("user_list")

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": json.dumps({"Reviewers": [ACC, {"accountId": ACC2}]}),
        })

        assert gated_call_spy[0]["preview"]["Reviewers (field)"] == "→ @Jane Doe, @Bob Real"
        client.update_issue.assert_called_once_with(
            "E-1", {"reviewers": [{"accountId": ACC}, {"accountId": ACC2}]})

    async def test_select_field_with_id_object_still_works(self, gated_call_spy):
        connector, client = self._setup("other")

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": '{"Severity": {"id": "10001"}}',
        })

        client.resolve_user_names.assert_not_called()
        assert gated_call_spy[0]["preview"]["Severity (field)"] == "→ {'id': '10001'}"
        client.update_issue.assert_called_once_with("E-1", {"severity": {"id": "10001"}})

    async def test_non_user_field_account_id_shows_name_and_every_other_key(self, gated_call_spy):
        connector, _client = self._setup("other")
        value = {"accountId": ACC, "displayName": "Somebody Else"}

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": json.dumps({"Owner": value}),
        })

        row = gated_call_spy[0]["preview"]["Owner (field)"]
        assert row == "→ {'accountId': '@Jane Doe', 'displayName': 'Somebody Else'}"

    async def test_nested_people_in_a_plain_value_are_named(self, gated_call_spy):
        connector, _client = self._setup("other")
        value = {"note": "hi", "who": [{"accountId": ACC2, "displayName": "Fake"},
                                      {"type": "mention", "attrs": {"id": ACC, "text": "@Fake"}}]}

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": json.dumps({"Mixed": value}),
        })

        row = gated_call_spy[0]["preview"]["Mixed (field)"]
        assert row == (
            "→ {'note': 'hi', 'who': [{'accountId': '@Bob Real', 'displayName': 'Fake'}, '@Jane Doe']}"
        )

    async def test_rich_text_shown_on_card_with_names_for_two_fields(self, gated_call_spy):
        connector, client = self._setup("other")
        def doc(text):
            return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": text},
                {"type": "mention", "attrs": {"id": ACC, "text": "@Fake Label"}}]}]}

        custom = json.dumps({"Environment": doc("SECRET PAYLOAD TEXT"), "Notes": doc("SECOND BLOCK")})

        await connector.call("jira_update_issue", {"issue_key": "E-1", "custom_fields": custom})

        kwargs = gated_call_spy[0]
        details = kwargs["details_text"]
        assert "SECRET PAYLOAD TEXT" in details and "SECOND BLOCK" in details
        assert "@Jane Doe" in details and "Fake Label" not in details
        assert kwargs["preview"]["Environment (field)"] == "→ (updated — see below); people: Jane Doe"
        assert "Notes (field)" in kwargs["preview"]

    async def test_account_id_with_extra_keys_shows_them_cascading_select(self, gated_call_spy):
        connector, client = self._setup("other")
        value = {"value": "Parent", "child": {"value": "Secret"}, "accountId": ACC}

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": json.dumps({"Cascade": value}),
        })

        row = gated_call_spy[0]["preview"]["Cascade (field)"]
        assert "Secret" in row and "Parent" in row and "@Jane Doe" in row and ACC not in row
        client.update_issue.assert_called_once()

    @pytest.mark.parametrize("value,hidden", [
        ({"accountId": ACC, "payload": "HIDDEN TEXT"}, "HIDDEN TEXT"),
        ([{"accountId": ACC, "note": "HIDDEN"}], "HIDDEN"),
    ])
    async def test_extra_keys_next_to_account_id_shown_for_any_field(self, gated_call_spy, value, hidden):
        connector, _client = self._setup("other")

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": json.dumps({"Anything": value}),
        })

        row = gated_call_spy[0]["preview"]["Anything (field)"]
        assert hidden in row and "@Jane Doe" in row

    async def test_exact_account_id_object_collapses_to_name(self, gated_call_spy):
        connector, _client = self._setup("other")

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": json.dumps({"Owner": {"accountId": ACC}}),
        })

        assert gated_call_spy[0]["preview"]["Owner (field)"] == "→ @Jane Doe"

    @pytest.mark.parametrize("node,expected", [
        ({"type": "mention", "attrs": {"id": ACC}}, "→ @Jane Doe"),
        ({"type": "mention", "attrs": {"id": ACC, "text": "@Fake", "localId": "L1", "accessLevel": "CONTAINER"}},
         "→ {'type': 'mention', 'attrs': {'id': '@Jane Doe', 'text': '@Jane Doe', 'localId': 'L1', "
         "'accessLevel': 'CONTAINER'}}"),
        ({"type": "mention", "attrs": {"id": ACC}, "marks": [{"type": "strong"}]},
         "→ {'type': 'mention', 'attrs': {'id': '@Jane Doe'}, 'marks': [{'type': 'strong'}]}"),
    ])
    async def test_bare_mention_node_collapses_only_when_nothing_else_is_written(
        self, gated_call_spy, node, expected
    ):
        connector, _client = self._setup("other")

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": json.dumps({"Who": node}),
        })

        assert gated_call_spy[0]["preview"]["Who (field)"] == expected

    @pytest.mark.parametrize("node", [
        {"type": "mention"}, {"type": "mention", "attrs": {}}, {"type": "mention", "attrs": {"id": 5}},
        {"type": "mention", "attrs": {"id": ""}}, {"type": "mention", "attrs": "x"},
    ])
    @pytest.mark.parametrize("wrap", [False, True])
    async def test_mention_without_text_id_refused_before_card(self, gated_call_spy, node, wrap):
        connector, client = self._setup("other")
        value = _doc(_para(node)) if wrap else node

        with pytest.raises(ValueError, match="mention without an account id"):
            await connector.call("jira_update_issue", {
                "issue_key": "E-1", "custom_fields": json.dumps({"Notes": value}),
            })
        assert gated_call_spy == []
        client.update_issue.assert_not_called()

    async def test_render_adf_refuses_mention_without_id_itself(self):
        from privacyfence.connectors.jira import _render_adf
        with pytest.raises(ValueError, match="mention without an account id"):
            _render_adf({"type": "mention"}, {})

    async def test_ordered_list_start_number_shown(self, gated_call_spy):
        connector, _client = self._setup("other")
        item = {"type": "listItem", "content": [_para(_text("a"))]}
        doc = _doc({"type": "orderedList", "attrs": {"order": 1000}, "content": [item]})

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": json.dumps({"Notes": doc}),
        })

        assert "[orderedList order=1000]" in gated_call_spy[0]["details_text"]

    @pytest.mark.parametrize("name", ["Issue", "Mentions", "Assignee", "Summary", "Priority", "Description"])
    async def test_custom_field_rows_never_overwrite_card_rows(self, gated_call_spy, name):
        connector, client = self._setup("other")
        client.resolve_custom_field.side_effect = lambda n, v: ("cf_" + n.lower(), v)

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "summary": "New",
            "custom_fields": json.dumps({name: "zzz", name.upper() + "2": "yyy"}),
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Issue"].startswith("ENG-42")
        assert kwargs["preview"]["Summary"] == "Fix login bug → New"
        assert kwargs["preview"][f"{name} (field)"] == "→ zzz"
        assert kwargs["preview"][f"{name.upper()}2 (field)"] == "→ yyy"
        assert kwargs["summary"] == f"Update E-1: Summary, {name}, {name.upper()}2"

    async def test_card_text_lists_custom_field_names_without_empty_segment(self, gated_call_spy):
        connector, _client = self._setup("other")

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": '{"Issue": "zzz"}',
        })

        assert gated_call_spy[0]["summary"] == "Update E-1: Issue"
        assert gated_call_spy[0]["details_text"] == "Issue will be updated; description is unchanged."

    async def test_rich_text_and_description_both_on_card(self, gated_call_spy):
        connector, _client = self._setup("other")
        doc = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "RICH TEXT"}]}]}

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "description": "plain description",
            "custom_fields": json.dumps({"Environment": doc}),
        })

        details = gated_call_spy[0]["details_text"]
        assert "description:\nplain description" in details and "Environment:\nRICH TEXT" in details
        assert gated_call_spy[0]["preview"]["Environment (field)"] == "→ (updated — see below)"


def _doc(*blocks):
    return {"type": "doc", "version": 1, "content": list(blocks)}


def _para(*inline):
    return {"type": "paragraph", "content": list(inline)}


def _text(text, *marks):
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = list(marks)
    return node


class TestRichTextCard:
    @staticmethod
    def _setup(names=None):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.update_issue.return_value = make_issue()
        client.resolve_user_names.return_value = {ACC: "Jane Doe"} if names is None else names
        client.custom_field_kind.return_value = "other"
        client.resolve_custom_field.side_effect = lambda name, value: (name.lower(), value)
        return connector, client

    async def _update(self, connector, **fields):
        await connector.call("jira_update_issue", {"issue_key": "E-1", "custom_fields": json.dumps(fields)})

    async def test_link_hrefs_and_card_urls_are_on_the_card(self, gated_call_spy):
        connector, client = self._setup()
        doc = _doc(_para(
            _text("see docs", {"type": "link", "attrs": {"href": "https://evil.example/?secret=SSN123"}}),
            {"type": "inlineCard", "attrs": {"url": "https://evil.example/2"}},
        ), {"type": "blockCard", "attrs": {"url": "https://evil.example/3", "data": {"k": "v w"}}})

        await self._update(connector, Notes=doc)

        details = gated_call_spy[0]["details_text"]
        assert "see docs (https://evil.example/?secret=SSN123)" in details
        assert "[inlineCard url=https://evil.example/2]" in details
        assert 'url=https://evil.example/3 data={"k": "v w"}' in details
        client.update_issue.assert_called_once_with("E-1", {"notes": doc})

    async def test_literal_mention_markup_in_text_stays_literal(self, gated_call_spy):
        connector, _client = self._setup()
        literal = f"@[Alice]({ACC})"
        doc = _doc(_para(_text(literal), {"type": "mention", "attrs": {"id": ACC, "text": "@x"}}))

        await self._update(connector, Notes=doc)

        details = gated_call_spy[0]["details_text"]
        assert details == f"Notes:\n{literal}@Jane Doe"

    async def test_sent_mention_carries_directory_name_audit_keeps_original(self, gated_call_spy):
        connector, client = self._setup()
        doc = _doc(_para({"type": "mention", "attrs": {"id": ACC, "text": "@Fake Label"}}))

        await self._update(connector, Notes=doc)

        sent = client.update_issue.call_args.args[1]["notes"]
        assert sent["content"][0]["content"][0]["attrs"] == {"id": ACC, "text": "@Jane Doe"}
        assert doc["content"][0]["content"][0]["attrs"]["text"] == "@Fake Label"
        assert gated_call_spy[0]["raw_data"]["fields"]["notes"] == doc

    @pytest.mark.parametrize("bad", [
        {"type": "layoutSection", "content": []},
        {"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": [{"type": "annotation"}]}]},
        {"type": "paragraph", "content": ["not a node"]},
        {"type": "paragraph", "extra": "hidden"},
        {"type": "paragraph", "attrs": "x"},
        {"type": "paragraph", "content": "x"},
        {"type": "rule", "content": [{"type": "text", "text": "hidden"}]},
        {"type": "paragraph", "text": "hidden"},
        {"type": "text", "text": 5},
        {"type": "text"},
        {"type": "paragraph", "version": 1},
        {"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": "strong"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": ["strong"]}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": [{"type": "strong", "x": 1}]}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": [{"type": "strong", "attrs": 1}]}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": [{"type": "link", "attrs": {"href": 5}}]}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": [{"type": "link"}]}]},
    ])
    async def test_unsupported_or_malformed_adf_refused_before_card(self, gated_call_spy, bad):
        connector, client = self._setup()

        with pytest.raises(ValueError, match="cannot be shown completely"):
            await self._update(connector, Notes=_doc(bad))
        assert gated_call_spy == []
        client.update_issue.assert_not_called()

    async def test_refusal_names_the_node_type(self, gated_call_spy):
        connector, _client = self._setup()

        with pytest.raises(ValueError, match="'layoutSection'"):
            await self._update(connector, Notes=_doc({"type": "layoutSection"}))

    async def test_media_and_other_leaves_show_identifying_attributes(self, gated_call_spy):
        connector, _client = self._setup()
        doc = _doc(
            {"type": "mediaSingle", "attrs": {"layout": "center", "width": 50}, "content": [
                {"type": "media", "attrs": {"type": "file", "id": "abc-123", "collection": "contentId-9"}}]},
            _para(
                {"type": "emoji", "attrs": {"shortName": ":smile:", "text": "x"}},
                _text(" "),
                {"type": "status", "attrs": {"text": "Done now", "color": "green"}},
                _text(" "),
                {"type": "date", "attrs": {"timestamp": "1700000000000"}},
                {"type": "hardBreak"},
                {"type": "mediaInline", "attrs": {}},
            ))

        await self._update(connector, Notes=doc)

        details = gated_call_spy[0]["details_text"]
        assert "[media type=file id=abc-123 collection=contentId-9]" in details
        assert "[mediaSingle layout=center width=50]" in details
        assert "[emoji shortName=:smile: text=x]" in details
        assert '[status text="Done now" color=green]' in details
        assert "[date timestamp=1700000000000]" in details
        assert "[mediaInline]" in details

    async def test_structure_is_rendered(self, gated_call_spy):
        connector, _client = self._setup()
        def item(t):
            return {"type": "listItem", "content": [_para(_text(t))]}
        doc = _doc(
            {"type": "heading", "attrs": {"level": 2}, "content": [_text("Title")]},
            {"type": "bulletList", "content": [item("a"), item("b")]},
            {"type": "orderedList", "content": [item("c"), item("d")]},
            {"type": "rule"},
            {"type": "panel", "attrs": {"panelType": "info"}, "content": [_para(_text("careful"))]},
            {"type": "codeBlock", "attrs": {"language": "py"}, "content": [_text("x = 1")]},
            {"type": "table", "content": [{"type": "tableRow", "content": [
                {"type": "tableHeader", "content": [_para(_text("H"))]},
                {"type": "tableCell", "content": [_para(_text("C"))]}]}]},
            _para(_text("bold", {"type": "strong"}), _text("red", {"type": "textColor", "attrs": {"color": "#f00"}}),
                  _text("linked", {"type": "link", "attrs": {"href": "https://x.example", "title": "T"}})),
        )

        await self._update(connector, Notes=doc)

        details = gated_call_spy[0]["details_text"]
        assert details == (
            "Notes:\n[heading level=2]\nTitle\n- a\n- b\n1. c\n2. d\n---\n"
            "[panel panelType=info]\ncareful\n"
            "[codeBlock language=py]\nx = 1\n"
            "H | C\n"
            "boldred [textColor color=#f00]linked (https://x.example) [link title=T]"
        )

    async def test_inline_node_with_extra_attrs_shows_them(self, gated_call_spy):
        connector, _client = self._setup()
        doc = _doc(_para({"type": "mention", "attrs": {"id": ACC, "text": "@x", "accessLevel": "SITE"}}))

        await self._update(connector, Notes=doc)

        assert "@Jane Doe [mention accessLevel=SITE]" in gated_call_spy[0]["details_text"]

    async def test_two_rich_fields_each_shown(self, gated_call_spy):
        connector, _client = self._setup()

        await self._update(connector, A=_doc(_para(_text("ONE"))), B=_doc(_para(_text("TWO"))))

        assert gated_call_spy[0]["details_text"] == "A:\nONE\n\nB:\nTWO"


class TestMultiUserNonList:
    @pytest.mark.parametrize("bad", [None, "just-a-string", {"accountId": ACC}, 5])
    async def test_multi_user_field_needs_a_list(self, gated_call_spy, bad):
        connector, client = TestCustomFieldUserShapes._setup("user_list")
        client.resolve_custom_field.side_effect = lambda name, value: (name.lower(), value)

        with pytest.raises(ValueError, match=r'list of those'):
            await connector.call("jira_update_issue", {
                "issue_key": "E-1", "custom_fields": json.dumps({"Reviewers": bad}),
            })
        assert gated_call_spy == []

    async def test_single_user_field_null_still_clears(self, gated_call_spy):
        connector, client = TestCustomFieldUserShapes._setup("user")

        await connector.call("jira_update_issue", {"issue_key": "E-1", "custom_fields": '{"Approver": null}'})

        client.update_issue.assert_called_once_with("E-1", {"approver": None})


class TestTransitionIssue:
    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(status="In Progress")
        client.transition_issue.return_value = make_issue(status="Done")

        result = await connector.call(
            "jira_transition_issue", {"issue_key": "ENG-42", "transition_name": "Done"}
        )

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {
            "Issue": "ENG-42 — Fix login bug", "Status": "In Progress → Done",
        }
        assert kwargs["gate"] == "popup"
        assert result["status"] == "Done"
        client.transition_issue.assert_called_once_with("ENG-42", "Done")

    async def test_invalid_transition_propagates_as_runtime_error(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.transition_issue.side_effect = JiraClientError(
            "'Cancelled' is not a valid transition. Available: Done"
        )

        with pytest.raises(RuntimeError, match="not a valid transition"):
            await connector.call(
                "jira_transition_issue", {"issue_key": "ENG-42", "transition_name": "Cancelled"}
            )

    async def test_client_error_on_get_issue_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_issue.side_effect = JiraClientError("not found")

        with pytest.raises(RuntimeError, match="not found"):
            await connector.call(
                "jira_transition_issue", {"issue_key": "ENG-1", "transition_name": "Done"}
            )


class TestFieldCompleteness:
    """End to end: a fully-populated raw REST response -> the real
    JiraClient._parse_issue -> the real connector's popup preview -- not a
    hand-built JiraIssue, unlike every other test in this file. Mirrors
    test_confluence_connector.py's TestFieldCompleteness, the shape of
    check that would catch a _parse_* field mapping silently degrading to
    a fallback before it ships, not after.
    """

    async def test_get_issue_preview_has_no_placeholder_fields(self, gated_call_spy):
        client = make_real_client()
        # jira_client.py's get_issue and get_issue_comments both call
        # self._client.issue(...) (with different args) -- one raw response
        # with both the full fields and an (empty) comment list satisfies
        # both call sites.
        client._client.issue.return_value = {
            "key": "PFQA-1",
            "fields": {
                "summary": "PrivacyFence QA seed issue [QATEST]",
                "status": {"name": "To Do"},
                "issuetype": {"name": "Task"},
                "reporter": {"displayName": "Real Reporter"},
                "assignee": {"displayName": "Real Assignee"},
                "description": "Synthetic PrivacyFence QA test issue.",
                "created": "2026-01-01T00:00:00Z",
                "updated": "2026-07-01T00:00:00Z",
                "comment": {"comments": []},
            },
        }

        connector = JiraConnector(client)
        connector.my_email = "me@example.com"
        await connector.call("jira_get_issue", {"issue_key": "PFQA-1"})

        assert_no_placeholder_fields(gated_call_spy[0]["preview"])


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        # get_issue/create_issue/add_comment/update_issue/transition_issue results
        # are asdict()'d unconditionally, so they need real dataclass instances --
        # a bare MagicMock isn't a dataclass. jira_update_issue also validates that
        # at least one field is being changed before reaching the gate, so its
        # stub args need a non-empty field.
        client.get_issue.return_value = make_issue()
        client.create_issue.return_value = make_issue()
        client.add_comment.return_value = JiraComment(id="c1", author="me@example.com", body="ack")
        client.update_issue.return_value = make_issue()
        client.get_transitions.return_value = [
            JiraTransition(id="11", name="Start Progress", to_status="In Progress"),
        ]
        client.transition_issue.return_value = make_issue()
        client.find_users.return_value = [AtlassianUser(account_id="acc-jane-0001", display_name="Jane Doe")]
        client.refresh_user_cache.return_value = 1

        await assert_all_tools_leave_an_audit_trail(
            connector, jira_module, monkeypatch, tmp_path,
            arg_overrides={"jira_update_issue": {"summary": "Updated summary"}},
        )


class TestReviewerInputsFix12:
    @staticmethod
    def _setup(kind="other", names=None):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.update_issue.return_value = make_issue()
        client.resolve_user_names.return_value = {ACC: "Real X"} if names is None else names
        client.custom_field_kind.return_value = kind
        return connector, client

    async def test_mention_inside_an_attribute_value_shows_the_directory_name(self, gated_call_spy):
        connector, client = self._setup()
        doc = _doc(_para({"type": "inlineCard", "attrs": {"data": {
            "type": "mention", "attrs": {"id": ACC, "text": "@CEO"}}}}))
        client.resolve_custom_field.side_effect = lambda name, value: (name.lower(), value)

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": json.dumps({"Rich": doc}),
        })

        details = gated_call_spy[0]["details_text"]
        assert "@Real X" in details and "@CEO" not in details
        sent = client.update_issue.call_args.args[1]["rich"]
        assert sent["content"][0]["content"][0]["attrs"]["data"]["attrs"]["text"] == "@Real X"

    async def test_mention_with_node_level_account_id_shows_the_name(self, gated_call_spy):
        connector, client = self._setup()
        value = {"type": "mention", "attrs": {"id": ACC, "text": "@CEO"}, "accountId": ACC}
        client.resolve_custom_field.side_effect = lambda name, value: (name.lower(), value)

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": json.dumps({"Any": value}),
        })

        row = gated_call_spy[0]["preview"]["Any (field)"]
        assert ACC not in row and "@CEO" not in row
        assert row.count("@Real X") == 3

    @pytest.mark.parametrize("wrap", [lambda d: {"value": d}, lambda d: [d]])
    async def test_adf_doc_on_option_or_array_field_card_equals_sent(self, gated_call_spy, wrap):
        connector, client = self._setup()
        doc = _doc(_para({"type": "mention", "attrs": {"id": ACC, "text": "@CEO"}}))
        client.resolve_custom_field.side_effect = lambda name, value: (name.lower(), wrap(value))

        await connector.call("jira_update_issue", {
            "issue_key": "E-1", "custom_fields": json.dumps({"Opt": doc}),
        })

        row = gated_call_spy[0]["preview"]["Opt (field)"]
        assert "@Real X" in row and "@CEO" not in row
        assert "details_text" in gated_call_spy[0]
        sent = client.update_issue.call_args.args[1]["opt"]
        assert "@Real X" in json.dumps(sent) and "@CEO" not in json.dumps(sent)

    async def test_literal_markup_text_in_read_preview_is_not_rewritten(self, gated_call_spy):
        connector, client = make_connector()
        literal = "@[Label](0123456789)"
        client.get_issue.return_value = make_issue(description=literal)
        client.get_issue.return_value.display_description = literal
        client.get_issue_comments.return_value = []

        await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        kwargs = gated_call_spy[0]
        assert literal in kwargs["details_text"]
        assert literal in kwargs["pii_scan_text"]

    async def test_real_client_read_preview_renders_nodes_only(self, gated_call_spy):
        client = make_real_client()
        literal = "@[Label](0123456789)"
        raw = {"key": "ENG-42", "fields": {"description": {"type": "doc", "content": [{
            "type": "paragraph", "content": [
                {"type": "text", "text": literal},
                {"type": "mention", "attrs": {"id": ACC, "text": "@Jane"}},
            ]}]}}}
        issue = client._parse_issue(raw, include_description=True, names={ACC: "Jane Doe"})
        comment = client._parse_comment(
            {"id": "c", "body": {"type": "doc", "content": [{"type": "paragraph", "content": [
                {"type": "mention", "attrs": {"id": ACC, "text": "@Jane"}}]}]}},
            {ACC: "Jane Doe"},
        )
        assert issue.display_description == f"{literal} @Jane Doe"
        assert issue.description == f"{literal} @[Jane Doe]({ACC})"
        assert comment.display_body == "@Jane Doe"
        assert "display_description" not in jira_module.asdict(issue)
        assert "display_body" not in jira_module.asdict(comment)
