"""Equivalence tests for privacyfence.policy.scopes (P2 of the policy v2 redesign).

Every `SCOPE_SELECTORS` entry is checked against the old `AutoAcceptEvaluator._rule_*` method it
replaces on a table of fixtures per predicate, mirroring `test_auto_accept.py`'s own edge cases
(a match, a non-match, an empty/falsy value, and -- for a FETCHED predicate -- the shapes its old
counterpart is known to accept: dict vs. object raw_data, a `.file`-wrapped Drive object, a
non-list raw_data standing in for a single result). Agreement on every fixture is P2's exit
criterion for this module: a red run here is the equivalence harness described in the redesign
proposal's P0 catching a real behavioral drift, not a false alarm to silence.

`TestIdentitySpoofResistance` below carries the same SEC-02 payloads `test_auto_accept.py` uses to
prove the old rules compare a parsed address rather than a substring -- reusing `_address_of`
(imported, not reimplemented, in `policy/scopes.py`) means every new identity selector inherits
that fix for free, but the fixture is repeated here so a future refactor that stops reusing
`_address_of` fails loudly in this module too.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from privacyfence.policy.scopes import (
    NEW_SCOPE_SELECTORS,
    SCOPE_SELECTORS,
    ResolvesFrom,
    ScopeKind,
    scope_type_to_predicates,
)

from ...helpers import make_ctx
from ._v1_reference import ARGS_ONLY_RULES, DATA_DEPENDENT_RULES, V1_CONDITION_NAMES, V1Reference

_EV = V1Reference()


def _old(predicate: str):
    return getattr(_EV, f"_rule_{predicate}")


# predicate name -> [(value, ctx), ...] fixtures, exercised against both the old _rule_* method
# and the new selector's matches().
FIXTURES: dict[str, list] = {
    "dm_with_myself": [
        (None, make_ctx(args={"channel_id": "D12345", "is_self_dm": True})),
        (None, make_ctx(args={"channel_id": "D67890", "is_self_dm": False})),
        (None, make_ctx(args={"channel_id": "D12345"})),
        (None, make_ctx(args={"channel_id": "C12345"})),
    ],
    "send_to_myself": [
        (None, make_ctx(args={"channel_id": "D12345", "is_self_dm": True})),
        (None, make_ctx(args={"channel_id": "D67890", "is_self_dm": False})),
        (None, make_ctx(args={"channel_id": "D12345"})),
        (None, make_ctx(args={"channel_id": "C12345"})),
    ],
    "group_dm": [
        (None, make_ctx(args={"channel_id": "G123", "is_group_dm": True})),
        (None, make_ctx(args={"channel_id": "C123", "is_group_dm": False})),
        (None, make_ctx(args={"channel_id": "G123"})),
    ],
    "approved_channel": [
        (["C123"], make_ctx(args={"channel_id": "C123"})),
        (["C999"], make_ctx(args={"channel_id": "C123"})),
        (["C123"], make_ctx(args={"channel": "C123"})),
        ([], make_ctx(args={"channel_id": "C123"})),
    ],
    "approved_recipient": [
        (["C123"], make_ctx(args={"channel_id": "C123"})),
        (["C999"], make_ctx(args={"channel_id": "C123"})),
    ],
    "approved_channel_all_results": [
        (["C1", "C2"], make_ctx(raw_data=[SimpleNamespace(channel_id="C1"), SimpleNamespace(channel_id="C2")])),
        (["C1", "C2"], make_ctx(raw_data=[SimpleNamespace(channel_id="C1"), SimpleNamespace(channel_id="C9")])),
        (["C1", "C2"], make_ctx(raw_data=[])),
        ([], make_ctx(raw_data=[SimpleNamespace(channel_id="C1")])),
        (["C1"], make_ctx(raw_data=SimpleNamespace(channel_id="C1"))),
    ],
    "public_channels_only": [
        (None, make_ctx(raw_data=[SimpleNamespace(is_private=False), SimpleNamespace(is_private=False)])),
        (None, make_ctx(raw_data=[SimpleNamespace(is_private=False), SimpleNamespace(is_private=True)])),
        (None, make_ctx(raw_data=SimpleNamespace(is_private=False))),
    ],
    "personal_calendar": [
        (["primary"], make_ctx(args={"calendar_id": "primary"})),
        (["work"], make_ctx(args={"calendar_id": "primary"})),
        ([], make_ctx(args={"calendar_id": "primary"})),
    ],
    "i_am_organizer": [
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(organizer_email="me@example.com"))),
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(organizer_email="other@example.com"))),
        (None, make_ctx(my_email="me@example.com", raw_data={"organizer_email": "me@example.com"})),
    ],
    "i_am_author": [
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(author="me@example.com"))),
        (None, make_ctx(my_email="me@example.com", raw_data={"author": "me@example.com"})),
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(author="other@example.com"))),
    ],
    "approved_space_keys": [
        (["ENG"], make_ctx(args={"space_key": "eng"}, raw_data={})),
        (["ENG"], make_ctx(args={}, raw_data={"space_key": "eng"})),
        (["ENG"], make_ctx(args={}, raw_data=None)),
        ([], make_ctx(args={"space_key": "eng"})),
    ],
    "created_this_session": [
        (None, make_ctx(raw_data=SimpleNamespace(id="file123"), session_created_ids={"file123"})),
        (None, make_ctx(raw_data=SimpleNamespace(id="other"), session_created_ids={"file123"})),
    ],
    "file_type_allowlist": [
        (["application/pdf"], make_ctx(raw_data=SimpleNamespace(mime_type="application/pdf"))),
        (["text/plain"], make_ctx(raw_data=SimpleNamespace(mime_type="application/pdf"))),
        ([], make_ctx(raw_data=SimpleNamespace(mime_type="application/pdf"))),
    ],
    "approved_folder": [
        (["folder1"], make_ctx(raw_data=SimpleNamespace(parent_ids=["folder1", "folder2"]))),
        (["folder9"], make_ctx(raw_data=SimpleNamespace(parent_ids=["folder1", "folder2"]))),
        ([], make_ctx(raw_data=SimpleNamespace(parent_ids=["folder1"]))),
        (["folder1"], make_ctx(raw_data={"file": SimpleNamespace(parent_ids=["folder1"])})),
    ],
    "approved_sandbox_folder": [
        (["folder1"], make_ctx(raw_data=SimpleNamespace(parent_ids=["folder1", "folder2"]))),
        (["folder9"], make_ctx(raw_data=SimpleNamespace(parent_ids=["folder1"]))),
    ],
    "move_within_approved_folders": [
        (["folder1"], make_ctx(raw_data=SimpleNamespace(parent_ids=["folder1", "folder2"]))),
        (["folder9"], make_ctx(raw_data=SimpleNamespace(parent_ids=["folder1"]))),
        (["folder1", "folder3"], make_ctx(
            args={"destination_folder_id": "folder3"}, raw_data=SimpleNamespace(parent_ids=["folder1"]))),
        (["folder1"], make_ctx(
            args={"destination_folder_id": "folder3"}, raw_data=SimpleNamespace(parent_ids=["folder1"]))),
        (["folder1"], make_ctx(raw_data={
            "file": SimpleNamespace(parent_ids=["folder1"]), "destination_folder_id": "folder1"})),
    ],
    "parent_folder_allowlist": [
        (["folderA"], make_ctx(args={"parent_folder_id": "folderA"})),
        (["folderB"], make_ctx(args={"parent_folder_id": "folderA"})),
        ([], make_ctx(args={"parent_folder_id": "folderA"})),
    ],
    "i_am_owner": [
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(owners=["me@example.com"]))),
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(owners=["other@example.com"]))),
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(file=SimpleNamespace(owners=["me@example.com"])))),
        (None, make_ctx(my_email="", raw_data=SimpleNamespace(owners=["me@example.com"]))),
    ],
    "created_by_me": [
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(owners=["me@example.com"]))),
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(owners=["other@example.com"]))),
    ],
    "i_am_sender": [
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(sender="Me <me@example.com>"))),
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(sender="someone-else@example.com"))),
        (None, make_ctx(my_email="", raw_data=SimpleNamespace(sender=""))),
    ],
    "trusted_sender_domain": [
        (["trusted.com"], make_ctx(raw_data=SimpleNamespace(sender="Alice <alice@trusted.com>"))),
        (["trusted.com"], make_ctx(raw_data=SimpleNamespace(sender="alice@trusted.com"))),
        (["trusted.com"], make_ctx(raw_data=SimpleNamespace(sender="Alice <alice@untrusted.com>"))),
        (["trusted.com"], make_ctx(raw_data=SimpleNamespace(sender="Alice <alice@Trusted.COM>"))),
        ([], make_ctx(raw_data=SimpleNamespace(sender="Alice <alice@trusted.com>"))),
        (["netflix.com"], make_ctx(raw_data=SimpleNamespace(sender="Netflix <info@members.netflix.com>"))),
        (["trusted.com"], make_ctx(raw_data=SimpleNamespace(sender="Alice <alice@mail.members.trusted.com>"))),
        (["trusted.com"], make_ctx(raw_data=SimpleNamespace(sender="Alice <alice@eviltrusted.com>"))),
    ],
    "label_match": [
        (["newsletter"], make_ctx(raw_data=SimpleNamespace(labels=["INBOX", "Newsletter"]))),
        (["promotions"], make_ctx(raw_data=SimpleNamespace(labels=["INBOX", "Newsletter"]))),
        (None, make_ctx(raw_data=SimpleNamespace(labels=["INBOX"]))),
    ],
    "label_name_allowlist": [
        (["newsletter"], make_ctx(args={"label_name": "Newsletter"})),
        (["promotions"], make_ctx(args={"label_name": "Newsletter"})),
        ([], make_ctx(args={"label_name": "Newsletter"})),
    ],
    "i_am_sole_recipient": [
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(recipients=["Me <me@example.com>"]))),
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(recipients=["me@example.com", "other@example.com"]))),
        (None, make_ctx(my_email="", raw_data=SimpleNamespace(recipients=["me@example.com"]))),
    ],
    "to_is_myself": [
        (None, make_ctx(my_email="me@example.com", args={"to": "Me <me@example.com>"})),
        (None, make_ctx(my_email="me@example.com", args={"to": ["me@example.com", "Me <me@example.com>"]})),
        (None, make_ctx(my_email="me@example.com", args={"to": ["me@example.com", "other@example.com"]})),
        (None, make_ctx(my_email="me@example.com", args={"to": ""})),
    ],
    "approved_recipient_domain": [
        (["trusted.com"], make_ctx(args={"to": ["Alice <alice@trusted.com>", "bob@trusted.com"]})),
        (["trusted.com"], make_ctx(args={"to": ["alice@trusted.com", "eve@external.com"]})),
        (["trusted.com"], make_ctx(args={"to": []})),
        ([], make_ctx(args={"to": ["alice@trusted.com"]})),
    ],
    "approved_project_keys": [
        (["ENG"], make_ctx(args={"project_key": "eng"})),
        (["ENG"], make_ctx(args={"issue_key": "ENG-123"})),
        (["ENG"], make_ctx(args={"issue_key": "OPS-1"})),
        ([], make_ctx(args={"project_key": "eng"})),
    ],
    "i_am_reporter": [
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(reporter="me@example.com"))),
        (None, make_ctx(my_email="me@example.com", raw_data={"reporter": "me@example.com"})),
        (None, make_ctx(my_email="me@example.com", raw_data={"reporter": "other@example.com"})),
    ],
    "i_am_assignee": [
        (None, make_ctx(my_email="me@example.com", raw_data={"assignee": "me@example.com"})),
        (None, make_ctx(my_email="me@example.com", raw_data={"assignee": "other@example.com"})),
    ],
    "approved_object_types": [
        (["account"], make_ctx(args={"object_type": "Account"})),
        (["contact"], make_ctx(args={"object_type": "Account"})),
        ([], make_ctx(args={"object_type": "Account"})),
        (["opportunity", "contact"], make_ctx(args={"object_types": "Opportunity,Contact"})),
        (["opportunity"], make_ctx(args={"object_types": "Opportunity,Contact"})),
        (["opportunity"], make_ctx(args={"object_types": ""})),
    ],
    "approved_report_ids": [
        (["00O123"], make_ctx(args={"report_id": "00O123"})),
        (["00O999"], make_ctx(args={"report_id": "00O123"})),
        ([], make_ctx(args={"report_id": "00O123"})),
    ],
    "approved_task_list": [
        (["list1"], make_ctx(args={"task_list_id": "list1", "task_id": "t1"})),
        (["list2"], make_ctx(args={"task_list_id": "list1"})),
        ([], make_ctx(args={"task_list_id": "list1"})),
        (None, make_ctx(args={"task_list_id": "list1"})),
        (["list1", "list2"], make_ctx(args={"source_list_id": "list1", "destination_list_id": "list2"})),
        (["list1", "list2"], make_ctx(args={"source_list_id": "list1", "destination_list_id": "list3"})),
        (["list1"], make_ctx(args={})),
        ("list1", make_ctx(args={"task_list_id": "list1"})),
    ],
    "approved_chats": [
        ([12345], make_ctx(args={"chat_id": 12345})),
        (["12345"], make_ctx(args={"chat_id": 12345})),
        ([99999], make_ctx(args={"chat_id": 12345})),
        ([], make_ctx(args={"chat_id": 12345})),
    ],
    "approved_chats_all_results": [
        (["111", "222"], make_ctx(raw_data=[SimpleNamespace(chat_id=111), SimpleNamespace(chat_id=222)])),
        ([111, 222], make_ctx(raw_data=[SimpleNamespace(chat_id=111), SimpleNamespace(chat_id=222)])),
        (["111", "222"], make_ctx(raw_data=[SimpleNamespace(chat_id=111), SimpleNamespace(chat_id=999)])),
        (["111", "222"], make_ctx(raw_data=[])),
        ([], make_ctx(raw_data=[SimpleNamespace(chat_id=111)])),
    ],
    "always_allow": [
        (None, make_ctx()),
        (None, make_ctx(args={"anything": "at all"})),
    ],
}


class TestEverySelectorAgreesWithItsCounterpart:
    @pytest.mark.parametrize("predicate", sorted(FIXTURES))
    def test_agrees_on_every_fixture(self, predicate):
        selector = SCOPE_SELECTORS[predicate]
        old_fn = _old(predicate)
        for value, ctx in FIXTURES[predicate]:
            old_result = old_fn(value, ctx)
            new_result = selector.matches(value, ctx)
            assert new_result == old_result, (predicate, value, ctx, old_result, new_result)


# The same SEC-02 payloads test_auto_accept.py uses -- every parsed-address identity selector must
# keep rejecting them (see this module's docstring).
_SEC02_MY_EMAIL = "me@example.com"

_SEC02_SPOOF_PAYLOADS = [
    pytest.param(f"{_SEC02_MY_EMAIL} <attacker@evil.com>", id="spoofed-display-name"),
    pytest.param(f"Notify <{_SEC02_MY_EMAIL}.attacker.net>", id="lookalike-domain-suffix"),
    pytest.param(f"not{_SEC02_MY_EMAIL}", id="lookalike-localpart-prefix"),
    pytest.param(f"{_SEC02_MY_EMAIL.upper()} <attacker@evil.com>", id="spoofed-display-name-mixed-case"),
]

_SEC02_IDENTITY_SELECTORS = [
    pytest.param(
        "i_am_sender",
        lambda payload: make_ctx(my_email=_SEC02_MY_EMAIL, raw_data=SimpleNamespace(sender=payload)),
        id="i_am_sender",
    ),
    pytest.param(
        "i_am_sole_recipient",
        lambda payload: make_ctx(my_email=_SEC02_MY_EMAIL, raw_data=SimpleNamespace(recipients=[payload])),
        id="i_am_sole_recipient",
    ),
    pytest.param(
        "i_am_owner",
        lambda payload: make_ctx(my_email=_SEC02_MY_EMAIL, raw_data=SimpleNamespace(owners=[payload])),
        id="i_am_owner",
    ),
    pytest.param(
        "created_by_me",
        lambda payload: make_ctx(my_email=_SEC02_MY_EMAIL, raw_data=SimpleNamespace(owners=[payload])),
        id="created_by_me",
    ),
]


class TestIdentitySpoofResistance:
    @pytest.mark.parametrize("predicate,build_ctx", _SEC02_IDENTITY_SELECTORS)
    @pytest.mark.parametrize("payload", _SEC02_SPOOF_PAYLOADS)
    def test_rejects_spoofed_identity(self, predicate, build_ctx, payload):
        selector = SCOPE_SELECTORS[predicate]
        ctx = build_ctx(payload)
        assert selector.matches(None, ctx) is False, f"{predicate} matched spoofed identity {payload!r}"


class TestVocabularyCompleteness:
    def test_every_scope_predicate_is_classified_upstream(self):
        for predicate in SCOPE_SELECTORS:
            assert predicate in ARGS_ONLY_RULES or predicate in DATA_DEPENDENT_RULES

    def test_resolves_from_matches_old_classification(self):
        for predicate, selector in SCOPE_SELECTORS.items():
            if selector.resolves_from is ResolvesFrom.ARGS:
                assert predicate in ARGS_ONLY_RULES, predicate
            else:
                assert predicate in DATA_DEPENDENT_RULES, predicate

    def test_every_fixture_predicate_has_a_selector(self):
        assert set(FIXTURES) == set(SCOPE_SELECTORS)

    def test_scopes_and_conditions_together_cover_every_v1_predicate(self):
        condition_predicates = {p for v1_names in V1_CONDITION_NAMES.values() for p in v1_names}
        covered = set(SCOPE_SELECTORS) | condition_predicates
        assert covered == ARGS_ONLY_RULES | DATA_DEPENDENT_RULES
        assert len(ARGS_ONLY_RULES | DATA_DEPENDENT_RULES) == 47

    def test_scope_type_to_predicates_round_trips(self):
        by_type = scope_type_to_predicates()
        assert by_type["drive.folder"] == (
            "approved_folder", "approved_sandbox_folder", "move_within_approved_folders", "parent_folder_allowlist",
        )
        # label_name_allowlist's tuple scope_type is filed under both of its types.
        assert "label_name_allowlist" in by_type["gmail.label"]
        assert "label_name_allowlist" in by_type["contacts.label"]
        for predicates in by_type.values():
            assert all(p in SCOPE_SELECTORS for p in predicates)


class TestKindIsAssignedToEverySelector:
    @pytest.mark.parametrize("predicate,selector", sorted(SCOPE_SELECTORS.items()))
    def test_kind_is_a_scope_kind(self, predicate, selector):
        assert isinstance(selector.kind, ScopeKind)


class TestSelfDmFailsClosed:
    """Every IM id starts with "D"; only the connector's resolved verdict may make a DM "mine"."""

    @pytest.mark.parametrize("predicate", ["dm_with_myself", "send_to_myself"])
    def test_a_dm_with_someone_else_does_not_match(self, predicate):
        ctx = make_ctx(args={"channel_id": "D67890", "is_self_dm": False})
        assert SCOPE_SELECTORS[predicate].matches(None, ctx) is False

    @pytest.mark.parametrize("predicate", ["dm_with_myself", "send_to_myself"])
    def test_a_dm_with_no_verdict_does_not_match(self, predicate):
        ctx = make_ctx(args={"channel_id": "D12345"})
        assert SCOPE_SELECTORS[predicate].matches(None, ctx) is False

    @pytest.mark.parametrize("predicate", ["dm_with_myself", "send_to_myself"])
    def test_a_truthy_non_bool_verdict_does_not_match(self, predicate):
        ctx = make_ctx(args={"channel_id": "D12345", "is_self_dm": "true"})
        assert SCOPE_SELECTORS[predicate].matches(None, ctx) is False

    @pytest.mark.parametrize("predicate", ["dm_with_myself", "send_to_myself"])
    def test_the_self_dm_matches(self, predicate):
        ctx = make_ctx(args={"channel_id": "D12345", "is_self_dm": True})
        assert SCOPE_SELECTORS[predicate].matches(None, ctx) is True


class TestMoveWithinApprovedFolders:
    """A move matches only when it starts and ends inside the approved set."""

    selector = SCOPE_SELECTORS["move_within_approved_folders"]

    def _ctx(self, source, destination):
        return make_ctx(
            args={"file_id": "f1", "destination_folder_id": destination},
            raw_data={"file": SimpleNamespace(parent_ids=[source]), "destination_folder_id": destination},
        )

    def test_a_move_within_the_approved_folders_matches(self):
        assert self.selector.matches(["sandbox", "sandbox/sub"], self._ctx("sandbox", "sandbox/sub")) is True

    def test_a_move_out_of_the_approved_folder_does_not_match(self):
        assert self.selector.matches(["sandbox"], self._ctx("sandbox", "elsewhere")) is False

    def test_a_move_into_the_approved_folder_from_outside_does_not_match(self):
        assert self.selector.matches(["sandbox"], self._ctx("elsewhere", "sandbox")) is False

    def test_a_move_with_no_destination_does_not_match(self):
        ctx = make_ctx(raw_data=SimpleNamespace(parent_ids=["sandbox"]))
        assert self.selector.matches(["sandbox"], ctx) is False

    def test_approved_folder_is_unchanged_and_ignores_the_destination(self):
        assert SCOPE_SELECTORS["approved_folder"].matches(["sandbox"], self._ctx("sandbox", "elsewhere")) is True


class TestNewScopeSelectors:
    """drive.file and apps_script.project have no v1 predicate (redesign proposal §04) -- checked
    against their own logic directly rather than an old counterpart."""

    def test_drive_file_matches_the_fetched_files_own_id(self):
        selector = NEW_SCOPE_SELECTORS["drive.file"]
        matching = make_ctx(raw_data=SimpleNamespace(id="file123"))
        other = make_ctx(raw_data=SimpleNamespace(id="file999"))
        wrapped = make_ctx(raw_data=SimpleNamespace(file=SimpleNamespace(id="file123")))
        assert selector.matches(["file123"], matching) is True
        assert selector.matches(["file123"], other) is False
        assert selector.matches(["file123"], wrapped) is True
        assert selector.matches([], matching) is False

    def test_apps_script_project_matches_script_id_from_args(self):
        selector = NEW_SCOPE_SELECTORS["apps_script.project"]
        ctx = make_ctx(args={"script_id": "abc123"})
        assert selector.matches(["abc123"], ctx) is True
        assert selector.matches(["other"], ctx) is False
        assert selector.matches([], ctx) is False
