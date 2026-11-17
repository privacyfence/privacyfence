"""Tests for the Drive tag helpers (names, parsing, properties encoding)."""

from __future__ import annotations

import pytest

from privacyfence.drive_tags import (
    MAX_TAGS_PER_CALL,
    TAG_PROPERTY_PREFIX,
    normalise_tag,
    parse_tag_list,
    tag_property_key,
    tags_from_properties,
)


class TestNormaliseTag:
    def test_strips_and_lowercases(self):
        assert normalise_tag("  Client-A ") == "client-a"

    @pytest.mark.parametrize("name", ["a", "0", "a.b_c-d", "x" * 40])
    def test_accepts_valid_names(self, name):
        assert normalise_tag(name) == name

    @pytest.mark.parametrize("raw", ["", "  ", "-a", ".a", "a b", "a/b", "a,b", "é", "x" * 41])
    def test_rejects_invalid_names(self, raw):
        with pytest.raises(ValueError, match="is not a valid tag name"):
            normalise_tag(raw)


class TestParseTagList:
    def test_splits_normalises_and_dedupes_keeping_order(self):
        assert parse_tag_list("B, a,, A , b") == ["b", "a"]

    def test_empty_text_gives_no_tags(self):
        assert parse_tag_list("") == []
        assert parse_tag_list(" , ,") == []

    def test_invalid_item_raises(self):
        with pytest.raises(ValueError, match="not a valid tag name"):
            parse_tag_list("ok, bad name")

    def test_ten_tags_allowed_eleven_rejected(self):
        ten = ",".join(f"t{i}" for i in range(MAX_TAGS_PER_CALL))
        assert len(parse_tag_list(ten)) == 10
        with pytest.raises(ValueError, match="At most 10 tags per call."):
            parse_tag_list(ten + ",t10")

    def test_duplicates_do_not_count_towards_the_limit(self):
        assert parse_tag_list(",".join(["a"] * 20)) == ["a"]


class TestTagPropertyKey:
    def test_prefixes_the_name(self):
        assert tag_property_key("x") == "privacyfence-tag.x"
        assert tag_property_key("x").startswith(TAG_PROPERTY_PREFIX)


class TestTagsFromProperties:
    def test_none_and_empty(self):
        assert tags_from_properties(None) == []
        assert tags_from_properties({}) == []

    def test_returns_sorted_names(self):
        props = {"privacyfence-tag.b": "1", "privacyfence-tag.a": "1"}
        assert tags_from_properties(props) == ["a", "b"]

    def test_ignores_foreign_properties(self):
        props = {"other-app": "1", "privacyfence-tag.a": "1", "x.privacyfence-tag.b": "1"}
        assert tags_from_properties(props) == ["a"]

    def test_ignores_wrong_value(self):
        assert tags_from_properties({"privacyfence-tag.a": "2", "privacyfence-tag.b": ""}) == []

    def test_ignores_invalid_names(self):
        props = {
            "privacyfence-tag.": "1",
            "privacyfence-tag.Upper": "1",
            "privacyfence-tag.-x": "1",
            "privacyfence-tag." + "x" * 41: "1",
        }
        assert tags_from_properties(props) == []
