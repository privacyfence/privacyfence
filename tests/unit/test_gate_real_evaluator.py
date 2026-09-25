"""Integration tests for gate.gated_call() driven by *real* v2 auto-accept rules
(policy/engine.py + policy/scopes.py + policy/conditions.py), not the FakeEvaluator
test_gate.py uses for its state-machine coverage.

A FakeEvaluator-style stub returns a canned (bool, str) with no rule-matching logic of its
own, so test_gate.py's ~50 tests prove gated_call's state machine is correct given *some*
auto-accept verdict, but not that any specific rule entry actually produces that verdict for
a given connector call. That's exactly what a human currently checks by hand, rule by rule,
connector by connector, in docs/connector-qa.md's per-connector exploratory checks ("reads
without a card" / "prompts" instructions). Each class below ports one of those checks
into a deterministic test: real v2 rules (built by ``tests.helpers.policy_rules`` from a
compact ``{operation_key: [{"predicate": name, "value": value}]}`` table), args/raw_data shaped
the way the real
connector module builds them, and an assertion on both the return value and the resulting
AuditEntry fields -- not just "a popup would/wouldn't show."

The popup layer (``gate.show_read_popup``/``gate.show_popup``, which delegate to whichever
``ApprovalUI`` is current, i.e. ``WebApprovalUI``) is monkeypatched to a scripted answer; only
the auto-accept side runs for real. The actual card
construction has its own coverage in test_approval_window_html.py/test_web_approval_ui.py.

salesforce.read_record's approved_object_types rule already has a real-rule regression test
in test_gate.py::TestApprovedObjectTypesNeverPopsUp (added after a live QA discrepancy) --
not duplicated here.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from privacyfence import auto_accept, gate
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.policy import store as policy_store

from ..helpers import policy_rules


@pytest.fixture
def audit_dir(tmp_path):
    init_audit_logger(str(tmp_path))
    return tmp_path


def read_audit_entries(audit_dir):
    week_file = audit_dir / f"{current_week()}.jsonl"
    if not week_file.exists():
        return []
    return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]


FILTERED = object()


def make_kwargs(**overrides):
    kwargs = dict(
        connector="gmail",
        tool="gmail_get_message",
        tool_name="Read Gmail message",
        summary="test call",
        sender="",
        raw_data=SimpleNamespace(),
        filtered_data=FILTERED,
        gate="review",
        preview={},
        details_text="ordinary, non-sensitive content",
        my_email="me@example.com",
        args={},
    )
    kwargs.update(overrides)
    return kwargs


def rule_id(predicate: str, value=None, conditions: tuple = ()) -> str:
    """The canonical, content-derived rule id a matched v2-store rule reports as its
    ``auto_accept_rule`` (P8/P9: ``gate._evaluate_auto_accept`` trusts ``matched.id`` directly
    rather than the raw predicate name -- see that function's own docstring). Every
    ``install_rules()``-installed rule below goes through ``policy.store.merge_rules``, which
    always mints this id, so a plain regular-match ("auto_accepted") audit entry never reports
    the bare predicate name the way a v1 ``matched_rule`` used to -- only the "Always allow"
    flow's own ``accepted_via_accept_all`` entry still does (``chosen.scope.predicate``, gate.py's
    own accept_all handling), since that path hasn't gone through the store yet at the moment it's
    audited.
    """
    return policy_store.rule_id_for(predicate, value, conditions)


def install_rules(table: dict) -> None:
    """Install ``policy_rules(table)`` as the current principal's hot-reloaded rule set -- what
    ``gate._evaluate_auto_accept`` reads via ``auto_accept.get_policy_v2_store_rules()``."""
    auto_accept.set_policy_v2_store_rules(policy_rules(table))


def fail_if_popup_shown(monkeypatch, *, review=True, popup=True):
    """Assert neither popup function is called -- the auto-accept path must resolve without
    ever reaching the interactive layer."""
    def boom(*a, **k):
        raise AssertionError("a native popup must not be shown for an auto-accepted call")
    if review:
        monkeypatch.setattr(gate, "show_read_popup", boom)
    if popup:
        monkeypatch.setattr(gate, "show_popup", boom)


class TestGmailTrustedSenderDomain:
    """connector-qa.md "Gmail checks", trusted sender domain: trusted_sender_domain
    must match subdomains of the configured value, not just an exact match."""

    RULES = {"gmail.read_message": [{"predicate": "trusted_sender_domain", "value": "trusted.com"}]}

    async def test_subdomain_sender_auto_accepts_with_no_popup(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            raw_data=SimpleNamespace(sender="Alice <alice@mail.trusted.com>"),
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("trusted_sender_domain", "trusted.com")

    async def test_unrelated_domain_still_prompts(self, monkeypatch, audit_dir):
        # Contrast case: proves the rule above is actually reachable, not
        # vacuously matching everything.
        install_rules(self.RULES)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**make_kwargs(
            raw_data=SimpleNamespace(sender="mallory@eviltrusted.com"),
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"


class TestDriveApprovedFolder:
    """connector-qa.md "Drive checks": trusted folder (plain auto-accept) and
    PII on reads only / PII overrides a matching rule (PII detection overrides
    a matching approved_folder rule on the read side, but a write to the same
    folder is never scanned)."""

    RULES = {"drive.read_file_contents": [{"predicate": "approved_folder", "value": ["qa-folder-id"]}]}

    async def test_read_in_approved_folder_auto_accepts(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_get_file_content", gate="review",
            raw_data=SimpleNamespace(parent_ids=["qa-folder-id"], owners=[]),
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("approved_folder", ["qa-folder-id"])

    async def test_pii_content_overrides_the_matching_folder_rule(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_get_file_content", gate="review",
            raw_data=SimpleNamespace(parent_ids=["qa-folder-id"], owners=[]),
            details_text="His SSN is 123-45-6789 on file.",
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        # Not auto_accepted, even though the folder rule matches -- the PII
        # gate routes it to the interactive popup regardless (gate.py's
        # module docstring).
        assert entries[0]["decision"] == "approved"
        assert entries[0]["auto_accept_rule"] == ""
        assert entries[0]["pii_detected"] is True

    async def test_write_of_the_same_pii_content_is_never_scanned(self, monkeypatch, audit_dir):
        # Same folder, same fake-PII body, but a write: gate="popup" never
        # runs the PII scan (gate.py's module docstring) or consults
        # approved_folder (writes never auto-accept via that rule).
        install_rules(self.RULES)
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_write_doc_content", gate="popup",
            raw_data=SimpleNamespace(parent_ids=["qa-folder-id"], owners=[]),
            details_text="His SSN is 123-45-6789 on file.",
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["pii_detected"] is False


class TestDriveSandboxFolderCoveragePastComment:
    """drive.comment_file/drive.upload_file/drive.move_file are now targets
    of the same sandbox_folders grant capability as the six pre-existing
    write ops -- confirms each one's own existing rule name still auto-
    accepts end to end (the wiring was purely additive)."""

    async def test_comment_on_a_file_in_the_sandbox_folder_auto_accepts(self, monkeypatch, audit_dir):
        install_rules({"drive.comment_file": [{"predicate": "approved_sandbox_folder", "value": ["qa-folder-id"]}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_add_comment", gate="popup",
            raw_data={"file": SimpleNamespace(parent_ids=["qa-folder-id"], owners=[]), "comment": "hi"},
            args={"file_id": "file-abc"},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("approved_sandbox_folder", ["qa-folder-id"])

    async def test_upload_into_the_allowlisted_folder_auto_accepts(self, monkeypatch, audit_dir):
        install_rules({"drive.upload_file": [{"predicate": "parent_folder_allowlist", "value": ["qa-folder-id"]}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_upload_file", gate="popup",
            raw_data=SimpleNamespace(),
            args={"parent_folder_id": "qa-folder-id"},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("parent_folder_allowlist", ["qa-folder-id"])

    async def test_move_of_a_file_from_the_allowlisted_folder_auto_accepts(self, monkeypatch, audit_dir):
        install_rules({"drive.move_file": [{"predicate": "move_within_approved_folders", "value": ["qa-folder-id"]}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_move_file", gate="popup",
            raw_data={"file": SimpleNamespace(parent_ids=["qa-folder-id"], owners=[]), "destination_folder_id": "other"},
            args={"file_id": "file-abc", "destination_folder_id": "other"},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("move_within_approved_folders", ["qa-folder-id"])


class TestDriveTempAccept:
    """connector-qa.md "Drive checks", temp-accept window: accepting one
    temp-accept-eligible call must silently auto-accept a second call for
    the same file, against the real, in-memory temp-accept store
    (auto_accept.register_temp_accept/is_temp_accepted -- module-level
    functions gate.py calls directly, P9) -- not test_gate.py::TestTempAccept's
    FakeEvaluator, which only proves gate.py's own decision routing around
    whatever that store reports."""

    async def test_second_call_for_the_same_file_auto_accepts(self, monkeypatch, audit_dir):
        install_rules({})
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        first = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_add_comment", gate="popup",
            args={"file_id": "file-abc"},
        ))
        assert first is FILTERED
        first_entry = read_audit_entries(audit_dir)[0]
        assert first_entry["decision"] == "accepted_via_temp_session"
        assert first_entry["auto_accept_rule"] == "session_temp_accept"

        fail_if_popup_shown(monkeypatch)  # second call must never reach the popup
        second = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_add_comment", gate="popup",
            args={"file_id": "file-abc"},
        ))

        assert second is FILTERED
        entries = read_audit_entries(audit_dir)
        assert len(entries) == 2
        assert entries[1]["decision"] == "auto_accepted"
        assert entries[1]["auto_accept_rule"] == "session_temp_accept"

    async def test_a_different_file_is_not_covered(self, monkeypatch, audit_dir):
        install_rules({})
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))
        await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_add_comment", gate="popup",
            args={"file_id": "file-abc"},
        ))

        result = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_add_comment", gate="popup",
            args={"file_id": "file-different"},
        ))

        assert result is FILTERED
        # The first file's grace window doesn't cover this one -- its own
        # popup still has to show (no auto-accept skip).
        assert len(popup_calls) == 2
        entries = read_audit_entries(audit_dir)
        assert entries[1]["decision"] == "accepted_via_temp_session"


class TestSlackGroupDm:
    """Group DMs (Slack's mpim conversation type) get their own rule instead
    of requiring each group's channel ID to be individually allowlisted
    under approved_channel."""

    RULES = {"slack.read_messages": [{"predicate": "group_dm"}]}

    async def test_group_dm_channel_auto_accepts(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="slack", tool="slack_get_channel_history", gate="review",
            raw_data=[SimpleNamespace(channel_id="G1")],
            args={"channel_id": "G1", "is_group_dm": True},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("group_dm")

    async def test_regular_channel_still_prompts_even_with_the_rule_configured(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**make_kwargs(
            connector="slack", tool="slack_get_channel_history", gate="review",
            raw_data=[SimpleNamespace(channel_id="C1")],
            args={"channel_id": "C1", "is_group_dm": False},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"


class TestSlackSearchAllResults:
    """slack_search_messages carries no single channel_id in args (a search
    can span any number of channels), so approved_channel/dm_with_myself can
    never fire for it -- approved_channel_all_results is the fix, evaluated
    against every result in raw_data instead of a single arg. Three scenarios
    per the original bug report: every result approved (auto-accept), a
    partial match (still gated, as one unit), and no match (gated)."""

    RULES = {"slack.read_messages": [{"predicate": "approved_channel_all_results", "value": ["C1", "C2"]}]}

    async def test_all_results_in_approved_channels_auto_accepts(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="slack", tool="slack_search_messages", gate="review",
            raw_data=[SimpleNamespace(channel_id="C1"), SimpleNamespace(channel_id="C2")],
            args={"query": "hello world"},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("approved_channel_all_results", ["C1", "C2"])

    async def test_one_unapproved_result_gates_the_whole_call(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**make_kwargs(
            connector="slack", tool="slack_search_messages", gate="review",
            raw_data=[SimpleNamespace(channel_id="C1"), SimpleNamespace(channel_id="C-unapproved")],
            args={"query": "hello world"},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["auto_accept_rule"] == ""

    async def test_no_results_in_approved_channels_gates(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**make_kwargs(
            connector="slack", tool="slack_search_messages", gate="review",
            raw_data=[SimpleNamespace(channel_id="C-other")],
            args={"query": "hello world"},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"


class TestTelegramSearchAllResults:
    """Same bug/fix as TestSlackSearchAllResults, for
    telegram_search_messages -- which now shares telegram.read_chat_messages
    with telegram_get_messages instead of its own operation key."""

    RULES = {"telegram.read_chat_messages": [{"predicate": "approved_chats_all_results", "value": ["111", "222"]}]}

    async def test_all_results_in_approved_chats_auto_accepts(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="telegram", tool="telegram_search_messages", gate="review",
            raw_data=[SimpleNamespace(chat_id=111), SimpleNamespace(chat_id=222)],
            args={"query": "hello world"},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("approved_chats_all_results", ["111", "222"])

    async def test_one_unapproved_chat_gates_the_whole_call(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**make_kwargs(
            connector="telegram", tool="telegram_search_messages", gate="review",
            raw_data=[SimpleNamespace(chat_id=111), SimpleNamespace(chat_id=999)],
            args={"query": "hello world"},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_configured_rule_still_covers_a_plain_chat_read(self, monkeypatch, audit_dir):
        # The merged operation key must not break telegram_get_messages's
        # existing single-chat approved_chats rule.
        install_rules({"telegram.read_chat_messages": [{"predicate": "approved_chats", "value": ["111"]}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="telegram", tool="telegram_get_messages", gate="review",
            raw_data=[SimpleNamespace(chat_id=111)],
            args={"chat_id": 111},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("approved_chats", ["111"])


class TestAlwaysAllowUnconditionalRule:
    """always_allow is the one rule with no resource identity to scope to --
    for drafts (any recipient) and calendar_create_out_of_office/
    calendar_set_working_location (no calendar_id arg at all)."""

    async def test_gmail_draft_auto_accepts_regardless_of_recipient(self, monkeypatch, audit_dir):
        install_rules({"gmail.create_draft": [{"predicate": "always_allow"}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="gmail", tool="gmail_create_draft", gate="popup",
            raw_data={"to": "anyone@example.com", "subject": "x", "body": "y", "cc": "", "bcc": ""},
            args={"to": "anyone@example.com", "subject": "x"},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("always_allow")

    async def test_gmail_draft_still_prompts_without_the_rule_configured(self, monkeypatch, audit_dir):
        install_rules({})
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**make_kwargs(
            connector="gmail", tool="gmail_create_draft", gate="popup",
            raw_data={"to": "anyone@example.com", "subject": "x", "body": "y", "cc": "", "bcc": ""},
            args={"to": "anyone@example.com", "subject": "x"},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"

    async def test_calendar_out_of_office_auto_accepts(self, monkeypatch, audit_dir):
        install_rules({"calendar.out_of_office": [{"predicate": "always_allow"}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="calendar", tool="calendar_create_out_of_office", gate="popup",
            raw_data={"title": "OOO"}, args={},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("always_allow")

    async def test_calendar_working_location_auto_accepts(self, monkeypatch, audit_dir):
        install_rules({"calendar.working_location": [{"predicate": "always_allow"}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="calendar", tool="calendar_set_working_location", gate="popup",
            raw_data={"location": "home"}, args={},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("always_allow")


class TestCalendarIAmOrganizer:
    """connector-qa.md "Calendar checks", own events."""

    RULES = {"calendar.read_event_details": [{"predicate": "i_am_organizer"}]}

    async def test_own_event_auto_accepts(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="calendar", tool="calendar_get_event_details", gate="review",
            raw_data=SimpleNamespace(organizer_email="me@example.com"),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "auto_accepted"

    async def test_someone_elses_event_still_prompts(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**make_kwargs(
            connector="calendar", tool="calendar_get_event_details", gate="review",
            raw_data=SimpleNamespace(organizer_email="someone-else@example.com"),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"


class TestJiraRules:
    """connector-qa.md "Jira checks": approved project (approved_project_keys,
    with an out-of-allowlist contrast) and own issues (i_am_reporter /
    i_am_assignee auto-accept independent of the project rule)."""

    async def test_issue_in_approved_project_auto_accepts(self, monkeypatch, audit_dir):
        install_rules({"jira.read_issue": [{"predicate": "approved_project_keys", "value": ["PFQA"]}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="jira", tool="jira_get_issue", gate="review",
            args={"issue_key": "PFQA-1"},
            raw_data=SimpleNamespace(reporter="", assignee=""),
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("approved_project_keys", ["PFQA"])

    async def test_issue_in_other_project_still_prompts(self, monkeypatch, audit_dir):
        install_rules({"jira.read_issue": [{"predicate": "approved_project_keys", "value": ["PFQA"]}]})
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**make_kwargs(
            connector="jira", tool="jira_get_issue", gate="review",
            args={"issue_key": "OTHER-5"},
            raw_data=SimpleNamespace(reporter="", assignee=""),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"

    async def test_reporter_auto_accepts_even_outside_the_approved_project(self, monkeypatch, audit_dir):
        install_rules({"jira.read_issue": [{"predicate": "i_am_reporter"}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="jira", tool="jira_get_issue", gate="review",
            args={"issue_key": "OTHER-99"},
            raw_data=SimpleNamespace(reporter="me@example.com", assignee="someone-else@example.com"),
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == rule_id("i_am_reporter")

    async def test_assignee_auto_accepts_independent_of_reporter_rule(self, monkeypatch, audit_dir):
        install_rules({"jira.read_issue": [{"predicate": "i_am_assignee"}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="jira", tool="jira_get_issue", gate="review",
            args={"issue_key": "OTHER-100"},
            raw_data=SimpleNamespace(reporter="someone-else@example.com", assignee="me@example.com"),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["auto_accept_rule"] == rule_id("i_am_assignee")


class TestConfluenceRules:
    """connector-qa.md "Confluence checks": approved space (approved_space_keys,
    with an out-of-allowlist contrast) and own pages (i_am_author auto-accepts
    independent of the space rule)."""

    async def test_page_in_approved_space_auto_accepts(self, monkeypatch, audit_dir):
        install_rules({"confluence.read_page": [{"predicate": "approved_space_keys", "value": ["PFQA"]}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="confluence", tool="confluence_get_page", gate="review",
            args={"space_key": "PFQA"},
            raw_data=SimpleNamespace(author=""),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["auto_accept_rule"] == rule_id("approved_space_keys", ["PFQA"])

    async def test_page_in_other_space_still_prompts(self, monkeypatch, audit_dir):
        install_rules({"confluence.read_page": [{"predicate": "approved_space_keys", "value": ["PFQA"]}]})
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**make_kwargs(
            connector="confluence", tool="confluence_get_page", gate="review",
            args={"space_key": "OTHERSPACE"},
            raw_data=SimpleNamespace(author=""),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"

    async def test_author_auto_accepts_even_outside_the_approved_space(self, monkeypatch, audit_dir):
        install_rules({"confluence.read_page": [{"predicate": "i_am_author"}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="confluence", tool="confluence_get_page", gate="review",
            args={"space_key": "OTHERSPACE"},
            raw_data=SimpleNamespace(author="me@example.com"),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["auto_accept_rule"] == rule_id("i_am_author")


class TestContactsNoContactInfoChange:
    """connector-qa.md "Contacts checks", contact-info edits: a name/note-only edit may
    auto-accept; the same rule must not cover an edit that also touches
    email/phone."""

    RULES = {"contacts.edit": [{"predicate": "always_allow", "conditions": [("no_contact_info_change", None)]}]}

    async def test_name_only_edit_auto_accepts(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="contacts", tool="contacts_update", gate="popup",
            args={"contact_id": "c1", "display_name": "New Name (edited)"},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "auto_accepted"

    async def test_email_change_still_prompts_even_with_the_rule_configured(self, monkeypatch, audit_dir):
        install_rules(self.RULES)
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**make_kwargs(
            connector="contacts", tool="contacts_update", gate="popup",
            args={"contact_id": "c1", "emails": ["new@example.com"]},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"


class TestTasksApprovedTaskList:
    """connector-qa.md "Tasks checks": approved list (create/update in an
    approved list) and moves, which -- unlike every other operation
    this rule covers -- requires BOTH the source and destination list to be
    on the allowlist (policy/scopes.py's own approved_task_list docstring)."""

    async def test_update_in_approved_list_auto_accepts(self, monkeypatch, audit_dir):
        install_rules({"tasks.update_task": [{"predicate": "approved_task_list", "value": ["list-a"]}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="tasks", tool="tasks_update_task", gate="popup",
            args={"task_list_id": "list-a"},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "auto_accepted"

    async def test_update_in_unapproved_list_still_prompts(self, monkeypatch, audit_dir):
        install_rules({"tasks.update_task": [{"predicate": "approved_task_list", "value": ["list-a"]}]})
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**make_kwargs(
            connector="tasks", tool="tasks_update_task", gate="popup",
            args={"task_list_id": "list-b"},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"

    async def test_move_auto_accepts_only_when_both_ends_are_approved(self, monkeypatch, audit_dir):
        install_rules({"tasks.move_task": [{"predicate": "approved_task_list", "value": ["list-a", "list-b"]}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="tasks", tool="tasks_move_task", gate="popup",
            args={"source_list_id": "list-a", "destination_list_id": "list-b"},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "auto_accepted"

    async def test_move_to_an_unapproved_destination_still_prompts(self, monkeypatch, audit_dir):
        install_rules({"tasks.move_task": [{"predicate": "approved_task_list", "value": ["list-a", "list-b"]}]})
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**make_kwargs(
            connector="tasks", tool="tasks_move_task", gate="popup",
            args={"source_list_id": "list-a", "destination_list_id": "list-c"},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"


class TestAcceptAllPersistsARealRule:
    """connector-qa.md's Always allow checks ("Gmail checks", "Drive checks"):
    confirming 'Always allow' on one call must persist a real rule that then
    silently covers a second, different-but-matching call -- exercised here
    against the real on-disk persistence path (gate.py's own accept_all
    handling, unmocked: ``policy.propose.proposals_for``/``rules_for_proposal``
    and ``auto_accept.add_policy_v2_rules``), not just the in-memory
    FakeEvaluator assertions test_gate.py::TestAcceptAll already covers for
    the state-machine side of this flow.
    """

    async def test_second_matching_call_is_silently_auto_accepted(self, monkeypatch, audit_dir, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("auto_accept: {}\n", encoding="utf-8")
        auto_accept.init_config_path(str(config_path))
        install_rules({})

        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)

        first = await gate.gated_call(**make_kwargs(
            connector="gmail", tool="gmail_get_message", gate="review",
            raw_data=SimpleNamespace(sender="alice@example.com"),
        ))
        assert first is FILTERED
        first_entry = read_audit_entries(audit_dir)[0]
        assert first_entry["decision"] == "accepted_via_accept_all"
        # i_am_sender doesn't match (my_email is "me@example.com", not alice's), so the only
        # proposal policy.propose.proposals_for offers for this call is trusted_sender_domain
        # -- see policy/propose.py's PROPOSABLE_SCOPES gmail ordering (i_am_sender first, but
        # filtered out here since it doesn't match this item).
        assert first_entry["auto_accept_rule"] == "trusted_sender_domain"

        on_disk = config_path.read_text(encoding="utf-8")
        assert "auto_accept:" in on_disk
        assert "trusted_sender_domain" in on_disk
        assert "example.com" in on_disk

        fail_if_popup_shown(monkeypatch)  # the newly created rule must cover this one silently
        second = await gate.gated_call(**make_kwargs(
            connector="gmail", tool="gmail_get_message", gate="review",
            raw_data=SimpleNamespace(sender="bob@example.com"),  # different sender, same domain
        ))

        assert second is FILTERED
        entries = read_audit_entries(audit_dir)
        assert len(entries) == 2
        assert entries[1]["decision"] == "auto_accepted"
        assert entries[1]["auto_accept_rule"] == rule_id("trusted_sender_domain", ["example.com"])


class TestAcceptAllPersistsARealRuleForWrites:
    """Write-side counterpart to TestAcceptAllPersistsARealRule: confirming
    Always allow on gmail_add_label must persist a real label_name_allowlist
    rule that then silently covers a second call adding the same label,
    against the real on-disk persistence path -- not just
    test_gate.py::TestAcceptAllWrites's in-memory FakeEvaluator assertions."""

    async def test_second_matching_label_add_is_silently_auto_accepted(self, monkeypatch, audit_dir, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("auto_accept: {}\n", encoding="utf-8")
        auto_accept.init_config_path(str(config_path))
        install_rules({})

        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)

        first = await gate.gated_call(**make_kwargs(
            connector="gmail", tool="gmail_add_label", gate="popup",
            raw_data=SimpleNamespace(sender="alice@example.com"),
            args={"message_id": "m1", "label_name": "Newsletters"},
        ))
        assert first is FILTERED
        first_entry = read_audit_entries(audit_dir)[0]
        assert first_entry["decision"] == "accepted_via_accept_all"
        assert first_entry["auto_accept_rule"] == "label_name_allowlist"

        on_disk = config_path.read_text(encoding="utf-8")
        assert "label_name_allowlist" in on_disk
        assert "Newsletters" in on_disk

        fail_if_popup_shown(monkeypatch)  # the newly created rule must cover this one silently
        second = await gate.gated_call(**make_kwargs(
            connector="gmail", tool="gmail_add_label", gate="popup",
            raw_data=SimpleNamespace(sender="bob@example.com"),  # different message, same label
            args={"message_id": "m2", "label_name": "Newsletters"},
        ))

        assert second is FILTERED
        entries = read_audit_entries(audit_dir)
        assert len(entries) == 2
        assert entries[1]["decision"] == "auto_accepted"
        assert entries[1]["auto_accept_rule"] == rule_id("label_name_allowlist", ["Newsletters"])

    # test_a_request_queued_behind_an_in_progress_accept_all_sees_the_new_rule
    # lived here through P2: two concurrent gated_call()s for *different*
    # args (so not a coalescing case) racing to create the same rule via
    # Always allow, serialized deterministically by _popup_lock so the
    # second was guaranteed to see the first's freshly-created rule via the
    # in-lock re-check rather than showing its own dialog. P3 removes
    # _popup_lock entirely and
    # with it the guaranteed ordering this test depended on -- two
    # concurrent calls for genuinely different args now race independently,
    # with no serialization point left to assert a fixed outcome against.
    # test_gate.py's TestCoalescing covers the case P3 actually guarantees
    # instead: two concurrent calls for the *same* args share one card and
    # one decision.
