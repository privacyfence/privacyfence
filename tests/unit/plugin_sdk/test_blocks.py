"""Block builders and validation."""
from __future__ import annotations

import pytest

from privacyfence_plugin_sdk import blocks


class TestBuilders:
    def test_shapes(self):
        assert blocks.heading("Title") == {"type": "heading", "text": "Title", "level": 2}
        assert blocks.heading("T", 3)["level"] == 3
        assert blocks.fields({"A": 1, "B": "x"}) == {
            "type": "fields", "items": [{"label": "A", "value": "1"}, {"label": "B", "value": "x"}]}
        assert blocks.fields([("A", "1")])["items"] == [{"label": "A", "value": "1"}]
        assert blocks.table([("a", "A")], [{"a": 1}, {"a": None}]) == {
            "type": "table", "columns": [{"key": "a", "label": "A"}], "rows": [{"a": 1}, {"a": None}]}
        assert blocks.text("hi") == {"type": "text", "text": "hi"}
        assert blocks.code("x = 1", "python") == {"type": "code", "text": "x = 1", "language": "python"}
        assert blocks.code("x") == {"type": "code", "text": "x"}
        assert blocks.diff("+a") == {"type": "diff", "format": "unified", "text": "+a"}

    @pytest.mark.parametrize("build", [
        lambda: blocks.heading("t", 4),
        lambda: blocks.heading("t", True),
        lambda: blocks.heading(5),
        lambda: blocks.fields({}),
        lambda: blocks.fields({str(i): "v" for i in range(51)}),
        lambda: blocks.table([], []),
        lambda: blocks.table([("a", "A"), ("a", "B")], []),
        lambda: blocks.table([("a", "A")], [{"b": 1}]),
        lambda: blocks.table([("a", "A")], [{"a": [1]}]),
        lambda: blocks.table([(str(i), "x") for i in range(21)], []),
        lambda: blocks.code("x", "Not Valid"),
        lambda: blocks.text(None),
    ])
    def test_invalid_input_raises_value_error(self, build):
        with pytest.raises(ValueError):
            build()


class TestSanitizing:
    def test_controls_and_bidi_are_stripped(self):
        dirty = "a\x00b\x1bc\x7fd\x85e‮f⁦g‎h؜i\nj\tk"
        assert blocks.text(dirty)["text"] == "abcdefghi\nj\tk"
        assert blocks.fields({"l‮": "v\x01"})["items"] == [{"label": "l", "value": "v"}]

    def test_long_cells_are_truncated(self):
        row = blocks.table([("a", "A")], [{"a": "x" * 5000}])["rows"][0]["a"]
        assert len(row) == 4096 and row.endswith("…")


class TestValidateBlocks:
    def test_returns_copies(self):
        original = [{"type": "text", "text": "a\x00"}]
        out = blocks.validate_blocks(original)
        assert out == [{"type": "text", "text": "a"}] and original[0]["text"] == "a\x00"

    @pytest.mark.parametrize("bad", [
        "text", [1], [{"type": "html"}], [{"type": "text"}], [{"type": "text", "text": "x", "extra": 1}],
        [{"type": "diff", "format": "context", "text": "x"}],
        [{"type": "fields", "items": [{"label": "a"}]}],
    ])
    def test_rejects(self, bad):
        with pytest.raises(ValueError):
            blocks.validate_blocks(bad)

    def test_count_and_byte_caps(self):
        many = [blocks.text("x")] * 51
        with pytest.raises(ValueError, match="at most 50"):
            blocks.validate_blocks(many)
        assert len(blocks.validate_blocks(many, max_blocks=60)) == 51
        big = [blocks.text("y" * 40_000), blocks.text("y" * 40_000)]
        with pytest.raises(ValueError, match="limit"):
            blocks.validate_blocks(big)
        assert len(blocks.validate_blocks(big, max_bytes=None)) == 2
