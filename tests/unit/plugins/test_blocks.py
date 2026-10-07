"""Unit tests for privacyfence.plugins.blocks."""
from __future__ import annotations

import pytest

from privacyfence.plugins.blocks import (
    BlockError,
    fields_dict,
    flatten_text,
    to_card_blocks,
    validate_blocks,
)
from privacyfence.plugins.constants import MAX_CELL_CHARS


class TestValidate:
    def test_not_a_list(self):
        with pytest.raises(BlockError):
            validate_blocks({"type": "text", "text": "x"})

    def test_block_not_an_object(self):
        with pytest.raises(BlockError):
            validate_blocks(["x"])

    @pytest.mark.parametrize("block", [{"type": "image", "text": "x"}, {"text": "x"}, {"type": 3}])
    def test_unknown_type_rejected(self, block):
        with pytest.raises(BlockError, match="unknown type"):
            validate_blocks([block])

    def test_error_is_value_error(self):
        assert issubclass(BlockError, ValueError)

    def test_heading(self):
        assert validate_blocks([{"type": "heading", "text": "H", "level": 3}]) == [
            {"type": "heading", "text": "H", "level": 3}]
        assert validate_blocks([{"type": "heading", "text": "H"}]) == [{"type": "heading", "text": "H"}]

    @pytest.mark.parametrize("level", [1, 4, "2", True])
    def test_heading_level_rejected(self, level):
        with pytest.raises(BlockError):
            validate_blocks([{"type": "heading", "text": "H", "level": level}])

    def test_extra_field_rejected(self):
        with pytest.raises(BlockError, match="unknown field"):
            validate_blocks([{"type": "text", "text": "x", "html": "<b>"}])

    def test_missing_field_rejected(self):
        with pytest.raises(BlockError, match="missing"):
            validate_blocks([{"type": "text"}])

    def test_text_must_be_string(self):
        with pytest.raises(BlockError):
            validate_blocks([{"type": "text", "text": 5}])

    def test_fields(self):
        blocks = [{"type": "fields", "items": [{"label": "a", "value": "b"}]}]
        assert validate_blocks(blocks) == blocks

    @pytest.mark.parametrize("items", [[], "x", [{"label": "a"}], [{"label": "a", "value": 1}],
                                       [{"label": "a", "value": "b", "x": "y"}], ["x"],
                                       [{"label": "a", "value": "b"}] * 51])
    def test_fields_invalid(self, items):
        with pytest.raises(BlockError):
            validate_blocks([{"type": "fields", "items": items}])

    def test_fields_fifty_ok(self):
        validate_blocks([{"type": "fields", "items": [{"label": "a", "value": "b"}] * 50}])

    def _table(self, **over):
        block = {"type": "table", "columns": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
                 "rows": [{"a": "x", "b": 1}, {"a": None, "b": 2.5}, {"a": True}]}
        block.update(over)
        return [block]

    def test_table(self):
        assert validate_blocks(self._table()) == self._table()

    def test_table_undeclared_key(self):
        with pytest.raises(BlockError, match="undeclared"):
            validate_blocks(self._table(rows=[{"zzz": 1}]))

    def test_table_cell_type(self):
        with pytest.raises(BlockError, match="string, number"):
            validate_blocks(self._table(rows=[{"a": [1]}]))

    def test_table_nonfinite_cell(self):
        with pytest.raises(BlockError, match="finite"):
            validate_blocks(self._table(rows=[{"a": float("nan")}]))

    @pytest.mark.parametrize("columns", [
        [], "x", [{"key": "a"}], [{"key": "", "label": "A"}], [{"key": "a", "label": "A"}] * 2,
        [{"key": f"k{i}", "label": "x"} for i in range(21)], ["x"],
    ])
    def test_table_columns_invalid(self, columns):
        with pytest.raises(BlockError):
            validate_blocks(self._table(columns=columns))

    @pytest.mark.parametrize("rows", ["x", ["x"]])
    def test_table_rows_invalid(self, rows):
        with pytest.raises(BlockError):
            validate_blocks(self._table(rows=rows))

    def test_code(self):
        blocks = [{"type": "code", "text": "x", "language": "c++"}]
        assert validate_blocks(blocks) == blocks
        assert validate_blocks([{"type": "code", "text": "x"}]) == [{"type": "code", "text": "x"}]

    @pytest.mark.parametrize("language", ["Python", "", "a" * 21, "py thon", 3])
    def test_code_language_invalid(self, language):
        with pytest.raises(BlockError):
            validate_blocks([{"type": "code", "text": "x", "language": language}])

    def test_diff(self):
        blocks = [{"type": "diff", "format": "unified", "text": "+a"}]
        assert validate_blocks(blocks) == blocks

    def test_diff_other_format_rejected(self):
        with pytest.raises(BlockError):
            validate_blocks([{"type": "diff", "format": "context", "text": "x"}])

    def test_returns_copies(self):
        src = [{"type": "text", "text": "x"}]
        out = validate_blocks(src)
        out[0]["text"] = "y"
        assert src[0]["text"] == "x"


class TestSanitize:
    def test_controls_removed_but_newline_and_tab_kept(self):
        out = validate_blocks([{"type": "text", "text": "a\x00b\x07c\x1b[0m\x7f\x85\x9fd\ne\tf"}])
        assert out[0]["text"] == "abc[0md\ne\tf"

    @pytest.mark.parametrize("char", ["\u202a", "\u202e", "\u2066", "\u2069", "\u200e", "\u200f",
                                      "\u061c"])
    def test_bidi_override_removed(self, char):
        out = validate_blocks([{"type": "text", "text": f"pay{char}gnp.exe"}])
        assert out[0]["text"] == "paygnp.exe"

    def test_every_string_is_sanitized(self):
        bad = "\u202ex"
        blocks = [
            {"type": "heading", "text": bad},
            {"type": "fields", "items": [{"label": bad, "value": bad}]},
            {"type": "table", "columns": [{"key": "a", "label": bad}], "rows": [{"a": bad}]},
            {"type": "code", "text": bad}, {"type": "diff", "format": "unified", "text": bad},
        ]
        assert "\u202e" not in repr(validate_blocks(blocks))

    def test_html_in_text_is_kept_as_text(self):
        out = validate_blocks([{"type": "text", "text": "<b>hi</b><script>x</script>"}])
        assert out[0]["text"] == "<b>hi</b><script>x</script>"
        assert to_card_blocks(out) == [{"type": "text", "text": "<b>hi</b><script>x</script>"}]

    def test_html_in_table_cell_is_kept_as_text(self):
        out = validate_blocks([{"type": "table", "columns": [{"key": "a", "label": "<i>"}],
                                "rows": [{"a": "<b>"}]}])
        assert to_card_blocks(out) == [{"type": "table", "headers": ["<i>"], "rows": [["<b>"]]}]


class TestCaps:
    def test_cell_truncated_with_ellipsis(self):
        out = validate_blocks([{"type": "table", "columns": [{"key": "a", "label": "A"}],
                                "rows": [{"a": "x" * (MAX_CELL_CHARS + 10)}]}], max_bytes=None)
        cell = out[0]["rows"][0]["a"]
        assert len(cell) == MAX_CELL_CHARS and cell.endswith("…")

    def test_cell_at_limit_untouched(self):
        out = validate_blocks([{"type": "table", "columns": [{"key": "a", "label": "A"}],
                                "rows": [{"a": "x" * MAX_CELL_CHARS}]}], max_bytes=None)
        assert out[0]["rows"][0]["a"] == "x" * MAX_CELL_CHARS

    def test_too_many_blocks(self):
        blocks = [{"type": "text", "text": "x"}] * 51
        with pytest.raises(BlockError, match="more than 50"):
            validate_blocks(blocks)
        assert len(validate_blocks(blocks, max_blocks=51)) == 51

    def test_byte_cap(self):
        blocks = [{"type": "text", "text": "x" * 100}]
        with pytest.raises(BlockError, match="larger than"):
            validate_blocks(blocks, max_bytes=50)

    def test_byte_cap_counts_bytes_not_characters(self):
        blocks = [{"type": "text", "text": "é" * 40}]
        with pytest.raises(BlockError):
            validate_blocks(blocks, max_bytes=100)

    def test_no_byte_cap(self):
        assert validate_blocks([{"type": "text", "text": "x" * 500_000}], max_bytes=None)

    def test_default_byte_cap_is_preview_cap(self):
        with pytest.raises(BlockError):
            validate_blocks([{"type": "text", "text": "x" * 70_000}])


class TestToCardBlocks:
    def test_mapping(self):
        blocks = validate_blocks([
            {"type": "heading", "text": "H"},
            {"type": "fields", "items": [{"label": "a", "value": "1"}, {"label": "b", "value": "2"}]},
            {"type": "table", "columns": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
             "rows": [{"a": "x", "b": 3}, {"a": None}]},
            {"type": "text", "text": "t"},
            {"type": "code", "text": "c", "language": "py"},
            {"type": "code", "text": "d"},
            {"type": "diff", "format": "unified", "text": "+x"},
        ])
        assert to_card_blocks(blocks) == [
            {"type": "heading", "label": "H"},
            {"type": "field", "label": "a", "value": "1"},
            {"type": "field", "label": "b", "value": "2"},
            {"type": "table", "headers": ["A", "B"], "rows": [["x", "3"], ["", ""]]},
            {"type": "text", "text": "t"},
            {"type": "code", "text": "c", "language": "py"},
            {"type": "code", "text": "d", "language": ""},
            {"type": "diff", "text": "+x"},
        ]

    def test_empty(self):
        assert to_card_blocks([]) == []


class TestFieldsDict:
    def test_duplicate_labels_get_suffixes(self):
        blocks = [{"type": "fields", "items": [{"label": "a", "value": "1"}, {"label": "a", "value": "2"}]},
                  {"type": "text", "text": "skipped"},
                  {"type": "fields", "items": [{"label": "a", "value": "3"}, {"label": "b", "value": "4"}]}]
        assert fields_dict(blocks) == {"a": "1", "a (2)": "2", "a (3)": "3", "b": "4"}
        assert list(fields_dict(blocks)) == ["a", "a (2)", "a (3)", "b"]

    def test_none(self):
        assert fields_dict([{"type": "text", "text": "x"}]) == {}


class TestFlatten:
    def test_all_strings_one_per_line(self):
        blocks = [
            {"type": "heading", "text": "H"},
            {"type": "fields", "items": [{"label": "L", "value": "V"}]},
            {"type": "table", "columns": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
             "rows": [{"a": "x", "b": 7}, {"a": None}]},
            {"type": "text", "text": "T"},
            {"type": "code", "text": "C"},
            {"type": "diff", "format": "unified", "text": "D"},
        ]
        assert flatten_text(blocks) == "H\nL\nV\nA\nB\nx\n7\nT\nC\nD"

    def test_empty(self):
        assert flatten_text([]) == ""
