"""Unit tests for the pure audiences parser and source (ADR 0325)."""
from __future__ import annotations

import json

import pytest

from privacyfence import upstream_audiences as ua


def _answer(audiences, version=1):
    return {"version": version, "audiences": audiences}


def _reason(structured, text=""):
    with pytest.raises(ua.AudiencesResultError) as info:
        ua.parse_result(structured, text)
    return info.value.reason


class TestParseResult:
    def test_structured_is_preferred_over_text(self):
        snap = ua.parse_result(_answer({"a": ["ann@x.io"]}), json.dumps(_answer({"b": ["bob@x.io"]})))
        assert dict(snap.by_email) == {"ann@x.io": frozenset({"a"})}

    def test_text_is_the_fallback(self):
        snap = ua.parse_result(None, json.dumps(_answer({"a": ["Ann@X.io "]})))
        assert dict(snap.by_email) == {"ann@x.io": frozenset({"a"})}
        assert (snap.audience_count, snap.people_count, snap.dropped) == (1, 1, 0)

    def test_repeated_key_in_text(self):
        text = '{"version": 1, "version": 1, "audiences": {}}'
        assert _reason(None, text) == "the answer repeats a key"

    @pytest.mark.parametrize("text", ["not json", "[1]", '"x"', ""])
    def test_not_an_object(self, text):
        assert _reason(None, text) == "the answer is not a JSON object"

    @pytest.mark.parametrize("value", [
        {"version": 1}, {"version": 1, "audiences": {}, "extra": 1}, {},
    ])
    def test_keys_must_be_exact(self, value):
        assert _reason(value) == "the answer must hold exactly version and audiences"

    @pytest.mark.parametrize("version", [2, "1", True])
    def test_version_must_be_the_int_one(self, version):
        assert _reason(_answer({}, version)) == "version must be 1"

    @pytest.mark.parametrize("name", ["Bad", "", "-a", "a b", "x" * 64])
    def test_audience_name_bad(self, name):
        assert _reason(_answer({name: []})).startswith("an audience name must be 1-63 characters")

    def test_audiences_not_a_dict(self):
        assert _reason(_answer([])) == "at most 200 audiences"

    def test_too_many_audiences(self):
        assert _reason(_answer({f"a{i}": [] for i in range(201)})) == "at most 200 audiences"
        assert ua.parse_result(_answer({f"a{i}": [] for i in range(200)}), "").audience_count == 200

    def test_too_many_entries(self):
        many = {f"a{i}": ["p@x.io"] * 1000 for i in range(20)}
        assert ua.parse_result(_answer(many), "").people_count == 1
        many["z"] = ["p@x.io"]
        assert _reason(_answer(many)) == "at most 20000 entries"

    def test_non_list_value(self):
        assert _reason(_answer({"a": "ann@x.io"})) == "each audience must list addresses as strings"

    def test_non_string_member(self):
        assert _reason(_answer({"a": ["ann@x.io", 3]})) == "each audience must list addresses as strings"

    def test_oversized_structured(self):
        big = _answer({"a": ["x" * (ua.MAX_ANSWER_CHARS + 1)]})
        assert _reason(big) == "the answer is larger than 1 MB"

    def test_unusable_addresses_are_dropped_and_counted(self):
        snap = ua.parse_result(_answer({"a": ["ann@x.io", "bön@x.io", "nope", "k@x.io"]}), "")
        assert set(snap.by_email) == {"ann@x.io", "k@x.io"}
        assert snap.dropped == 2

    def test_duplicates_collapse_and_audiences_union(self):
        snap = ua.parse_result(_answer({"a": ["ann@x.io", "ANN@x.io"], "b": ["ann@x.io"], "c": ["bob@x.io"]}), "")
        assert snap.by_email["ann@x.io"] == frozenset({"a", "b"})
        assert (snap.audience_count, snap.people_count) == (3, 2)

    def test_audience_without_usable_address_still_counts(self):
        snap = ua.parse_result(_answer({"a": ["bad"], "all": []}), "")
        assert (snap.audience_count, snap.people_count, snap.dropped) == (2, 0, 1)

    def test_digest_changes_on_a_swap_with_equal_counts(self):
        one = ua.parse_result(_answer({"a": ["ann@x.io"]}), "")
        two = ua.parse_result(_answer({"a": ["bob@x.io"]}), "")
        same = ua.parse_result(_answer({"a": ["ann@x.io"]}), "")
        assert one.digest != two.digest and one.digest == same.digest
        assert (one.audience_count, one.people_count) == (two.audience_count, two.people_count)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _source(refresh=30):
    clock = _Clock()
    return ua.UpstreamAudienceSource(refresh, clock=clock, wall=lambda: 1234.5), clock


def _snap(address="ann@x.io"):
    return ua.parse_result(_answer({"a": [address]}), "")


class TestSource:
    def test_waiting_before_the_first_snapshot(self):
        source, _ = _source()
        assert source.state() == "waiting"
        assert source.audiences_for("ann@x.io") == frozenset()
        assert source.summary() == {
            "state": "waiting", "fetched_at": None, "audience_count": 0,
            "people_count": 0, "dropped": 0, "last_error": "",
        }

    def test_ready_after_apply(self):
        source, _ = _source()
        assert source.apply(_snap(), source.begin()) is True
        assert source.state() == "ready"
        assert source.audiences_for("ann@x.io") == frozenset({"a"})
        assert source.audiences_for("zed@x.io") == frozenset()
        assert source.summary() == {
            "state": "ready", "fetched_at": 1234.5, "audience_count": 1,
            "people_count": 1, "dropped": 0, "last_error": "",
        }

    def test_stale_at_exactly_three_intervals_plus_one(self):
        source, clock = _source(30)
        source.apply(_snap(), source.begin())
        clock.now += 90
        assert source.state() == "ready"
        clock.now += 1
        assert source.state() == "stale"
        assert source.audiences_for("ann@x.io") == frozenset()
        assert source.summary()["state"] == "stale"

    def test_failure_keeps_the_snapshot_and_apply_clears_the_error(self):
        source, _ = _source()
        source.apply(_snap(), source.begin())
        assert source.record_failure("unreachable", source.begin()) is True
        assert source.summary()["last_error"] == "unreachable"
        assert source.audiences_for("ann@x.io") == frozenset({"a"})
        assert source.apply(_snap("bob@x.io"), source.begin()) is True
        assert source.summary()["last_error"] == ""

    def test_older_seq_is_ignored(self):
        source, _ = _source()
        first, second = source.begin(), source.begin()
        assert source.apply(_snap("bob@x.io"), second) is True
        assert source.apply(_snap("ann@x.io"), first) is False
        assert source.record_failure("invalid", first) is False
        assert source.audiences_for("bob@x.io") == frozenset({"a"})
        assert source.summary()["last_error"] == ""
        third = source.begin()
        assert source.record_failure("tool_error", third) is True
        fourth_older = third - 1
        assert source.apply(_snap(), fourth_older) is False
