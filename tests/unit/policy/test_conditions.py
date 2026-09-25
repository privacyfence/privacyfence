"""Equivalence tests for privacyfence.policy.conditions (P2 of the policy v2 redesign).

Every `ConditionSelector` is checked against the old `AutoAcceptEvaluator._rule_*` method(s) it
replaces on a table of fixtures per old predicate name, mirroring `test_auto_accept.py`'s own
edge cases (a match, a non-match, and -- for every FETCHED condition -- the absence case with
`raw_data=None` or a missing attribute, since that's exactly the hazard `DATA_DEPENDENT_RULES`
exists to flag). Agreement on every fixture is P2's exit criterion for this module: a red run here
is the equivalence harness described in the redesign proposal's P0 catching a real behavioral
drift, not a false alarm to silence.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from privacyfence.policy.conditions import CONDITION_SELECTORS, ResolvesFrom

from ...helpers import make_ctx
from ._v1_reference import (
    ARGS_ONLY_RULES,
    DATA_DEPENDENT_RULES,
    V1_CONDITION_NAMES,
    V1Reference,
    condition_name_for_v1_predicate,
)

_EV = V1Reference()


def _old(predicate: str):
    return getattr(_EV, f"_rule_{predicate}")


# predicate name -> [(value, ctx), ...] fixtures, exercised against both the old _rule_* method
# and the new selector's holds().
FIXTURES: dict[str, list] = {
    "age_threshold_days": [
        (30, make_ctx(raw_data=SimpleNamespace(date="Mon, 01 Jan 2024 12:00:00 +0000"))),
        (30, make_ctx(raw_data=SimpleNamespace(date="Mon, 01 Jul 2026 12:00:00 +0000"))),
        # No UTC offset -- exercises the "naive datetime" tzinfo-assignment branch.
        (30, make_ctx(raw_data=SimpleNamespace(date="Mon, 01 Jan 2024 12:00:00"))),
        (30, make_ctx(raw_data=SimpleNamespace(date=""))),
        (30, make_ctx(raw_data=SimpleNamespace(date="not-a-date"))),
        (0, make_ctx(raw_data=SimpleNamespace(date="Mon, 01 Jan 2024 12:00:00 +0000"))),
        (30, make_ctx(raw_data=None)),
    ],
    "time_window_days": [
        (7, make_ctx(raw_data=SimpleNamespace(start_time="2026-07-08T12:00:00Z"))),
        (7, make_ctx(raw_data=SimpleNamespace(start_time="2026-08-08T12:00:00Z"))),
        # No "Z"/offset -- exercises the "naive datetime" tzinfo-assignment branch.
        (7, make_ctx(raw_data=SimpleNamespace(start_time="2026-07-08T12:00:00"))),
        (0, make_ctx(raw_data=SimpleNamespace(start_time="2026-07-08T12:00:00Z"))),
        (7, make_ctx(raw_data=SimpleNamespace(start_time=""))),
        (7, make_ctx(raw_data=SimpleNamespace(start_time="not-a-date"))),
        (7, make_ctx(raw_data=None)),
    ],
    "past_event": [
        (None, make_ctx(raw_data=SimpleNamespace(end_time="2020-01-01T00:00:00Z"))),
        (None, make_ctx(raw_data=SimpleNamespace(end_time="2030-01-01T00:00:00Z"))),
        # No "Z"/offset -- exercises the "naive datetime" tzinfo-assignment branch.
        (None, make_ctx(raw_data=SimpleNamespace(end_time="2020-01-01T00:00:00"))),
        (None, make_ctx(raw_data=SimpleNamespace(end_time=""))),
        (None, make_ctx(raw_data=SimpleNamespace(end_time="not-a-date"))),
        (None, make_ctx(raw_data=None)),
    ],
    "no_attachments": [
        (None, make_ctx(connector="gmail", raw_data=SimpleNamespace(attachments=[]))),
        (None, make_ctx(connector="gmail", raw_data=SimpleNamespace(attachments=["a.pdf"]))),
        (None, make_ctx(connector="gmail", raw_data=SimpleNamespace())),
        (None, make_ctx(connector="gmail", raw_data=None)),
    ],
    "no_file_attachments": [
        (None, make_ctx(connector="slack", raw_data=[SimpleNamespace(files=None), SimpleNamespace(files=[])])),
        (None, make_ctx(connector="slack", raw_data=[SimpleNamespace(files=["img.png"])])),
        (None, make_ctx(connector="slack", raw_data=SimpleNamespace(files=None))),
    ],
    "no_media_attachments": [
        (None, make_ctx(connector="telegram", raw_data=[SimpleNamespace(media_type=""), SimpleNamespace(media_type=None)])),
        (None, make_ctx(connector="telegram", raw_data=[SimpleNamespace(media_type="photo")])),
        (None, make_ctx(connector="telegram", raw_data=SimpleNamespace(media_type=""))),
    ],
    "no_conferencing_link": [
        (None, make_ctx(raw_data=SimpleNamespace(conference_link=""))),
        (None, make_ctx(raw_data=SimpleNamespace(hangout_link="https://x"))),
        (None, make_ctx(raw_data=SimpleNamespace())),
        (None, make_ctx(raw_data=None)),
    ],
    "non_private_event": [
        (None, make_ctx(raw_data=SimpleNamespace(visibility="private"))),
        (None, make_ctx(raw_data=SimpleNamespace(visibility="public"))),
        (None, make_ctx(raw_data=SimpleNamespace(visibility="default"))),
        (None, make_ctx(raw_data=SimpleNamespace())),
        (None, make_ctx(raw_data=None)),
    ],
    "shared_drive_exclusion": [
        (None, make_ctx(raw_data=SimpleNamespace(drive_id=""))),
        (None, make_ctx(raw_data=SimpleNamespace(drive_id="0AShared"))),
        (None, make_ctx(raw_data=SimpleNamespace(drive_id="", shared=True))),
        (None, make_ctx(raw_data=SimpleNamespace())),
        (None, make_ctx(raw_data=None)),
    ],
    "no_contact_info_change": [
        (None, make_ctx(args={})),
        (None, make_ctx(args={"emails": ["a@b.com"]})),
        (None, make_ctx(args={"phones": ["+1"]})),
    ],
    "no_external_attendees": [
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(
            attendees=[{"email": "a@example.com"}, {"email": "b@example.com"}]))),
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(
            attendees=[SimpleNamespace(email="a@example.com")]))),
        (None, make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(
            attendees=[{"email": "a@external.com"}]))),
        (None, make_ctx(my_email="", raw_data=SimpleNamespace(attendees=[]))),
        (None, make_ctx(my_email="me@example.com", raw_data=None)),
    ],
    "reply_in_existing_thread": [
        (None, make_ctx(args={"thread_ts": "123.45"})),
        (None, make_ctx(args={})),
    ],
}


class TestEverySelectorAgreesWithItsCounterpart:
    @freeze_time("2026-07-06 12:00:00", tz_offset=0)
    @pytest.mark.parametrize("predicate", sorted(FIXTURES))
    def test_agrees_on_every_fixture(self, predicate):
        name = condition_name_for_v1_predicate(predicate)
        assert name is not None, f"{predicate} has no ConditionSelector"
        selector = CONDITION_SELECTORS[name]
        old_fn = _old(predicate)
        for value, ctx in FIXTURES[predicate]:
            old_result = old_fn(value, ctx)
            new_result = selector.holds(value, ctx)
            assert new_result == old_result, (predicate, value, ctx, old_result, new_result)


class TestNoAttachmentsUnknownConnector:
    """`no_attachments` dispatches on `ctx.connector` to pick the right fetched-object field
    (attachments/files/media_type) -- a connector outside {gmail, slack, telegram} has no old
    counterpart to be equivalent *to* (none of the three old predicates this condition absorbs is
    ever configured for another connector's operation key), so this is a direct test of the new
    dispatch's own fail-closed default rather than an equivalence check.
    """

    def test_never_matches_for_an_unrecognized_connector(self):
        selector = CONDITION_SELECTORS["no_attachments"]
        ctx = make_ctx(connector="jira", raw_data=SimpleNamespace())
        assert selector.holds(None, ctx) is False


class TestNotSharedDrive:
    """Drive's `shared` flag means "shared with someone", not "lives in a shared drive"; only a
    non-empty `drive_id` identifies a shared-drive file."""

    def test_a_shared_drive_file_fails(self):
        selector = CONDITION_SELECTORS["not_shared_drive"]
        ctx = make_ctx(raw_data=SimpleNamespace(drive_id="0AShared", shared=False))
        assert selector.holds(None, ctx) is False

    def test_a_my_drive_file_shared_with_a_colleague_holds(self):
        selector = CONDITION_SELECTORS["not_shared_drive"]
        ctx = make_ctx(raw_data=SimpleNamespace(drive_id="", shared=True))
        assert selector.holds(None, ctx) is True


class TestVocabularyCompleteness:
    def test_every_condition_predicate_is_classified_upstream(self):
        for v1_names in V1_CONDITION_NAMES.values():
            for predicate in v1_names:
                assert predicate in ARGS_ONLY_RULES or predicate in DATA_DEPENDENT_RULES

    def test_resolves_from_matches_old_classification(self):
        for name, selector in CONDITION_SELECTORS.items():
            for predicate in V1_CONDITION_NAMES[name]:
                if selector.resolves_from is ResolvesFrom.ARGS:
                    assert predicate in ARGS_ONLY_RULES, predicate
                else:
                    assert predicate in DATA_DEPENDENT_RULES, predicate

    def test_every_fixture_predicate_has_a_selector(self):
        assert set(FIXTURES) == {p for v1_names in V1_CONDITION_NAMES.values() for p in v1_names}

    def test_every_condition_has_a_v1_mapping(self):
        assert set(V1_CONDITION_NAMES) == set(CONDITION_SELECTORS)

    def test_condition_name_for_v1_predicate_returns_none_for_a_scope_predicate(self):
        assert condition_name_for_v1_predicate("approved_folder") is None
        assert condition_name_for_v1_predicate("no_such_predicate") is None
