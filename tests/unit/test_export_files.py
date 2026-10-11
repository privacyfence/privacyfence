"""Tests for export_files.py: the pure helpers the export tools share (file
name and format, CSV and JSON rendering, card sample and details, summary)."""
from __future__ import annotations

import json

import pytest

from privacyfence import export_files

pytestmark = pytest.mark.unit


class TestExportFileName:
    def test_defaults_to_stem_csv(self):
        assert export_files.export_file_name("  ", "Contacts") == ("Contacts.csv", "csv")

    def test_picks_json_from_suffix(self):
        assert export_files.export_file_name("a.json", "x") == ("a.json", "json")
        assert export_files.export_file_name("A.JSON", "x") == ("A.JSON", "json")

    @pytest.mark.parametrize("name", ["a/b.csv", "a\\b.csv", ".x.csv", "x.txt", "x", "a" * 197 + ".csv"])
    def test_rejects_paths_dotfiles_and_other_suffixes(self, name):
        with pytest.raises(ValueError, match="plain file name"):
            export_files.export_file_name(name, "x")


class TestCheckDestination:
    def test_returns_stripped(self):
        assert export_files.check_destination("  ~/Downloads ") == "~/Downloads"

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="destination_dir"):
            export_files.check_destination("   ")


class TestRefuseOrg:
    def test_raises_only_in_org_mode(self):
        export_files.refuse_org("tool_x", "local", "other_tool")
        with pytest.raises(ValueError, match="tool_x writes a file.*Use other_tool instead"):
            export_files.refuse_org("tool_x", "org", "other_tool")


class TestCsvCell:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, ""), (True, "true"), (False, "false"), (3, "3"), (3.0, "3"), (2.5, "2.5"),
            ("x", "x"), (["a", "b"], '["a", "b"]'), ({"k": 1}, '{"k": 1}'),
        ],
    )
    def test_formats(self, value, expected):
        assert export_files.csv_cell(value) == expected


class TestRender:
    def test_csv_quotes_commas_and_newlines(self):
        data = export_files.render(["a", "b"], [["x,y", "l1\nl2"]], "csv").decode()
        assert data == 'a,b\n"x,y","l1\nl2"\n'

    def test_csv_has_no_bom_and_lf_line_ends(self):
        data = export_files.render(["a"], [["é"], ["z"]], "csv")
        assert not data.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in data
        assert data == "a\né\nz\n".encode()

    def test_json_is_a_list_of_objects(self):
        data = export_files.render(["a", "b"], [[1, "é"], [None, True]], "json")
        assert data == '[{"a":1,"b":"é"},{"a":null,"b":true}]\n'.encode()
        assert json.loads(data) == [{"a": 1, "b": "é"}, {"a": None, "b": True}]


class TestSampleTable:
    def test_shows_twenty_rows_and_truncates_long_cells(self):
        rows = [["x" * 250, i] for i in range(30)]
        table = export_files.sample_table(["a", "b"], rows)
        assert table["caption"] == "First rows"
        assert table["headers"] == ["a", "b"]
        assert len(table["rows"]) == 20
        assert table["rows"][0] == ["x" * 200 + "…", "0"]
        assert table["footer"] == "Showing 20 of 30 rows."


class TestScanText:
    def test_caps_at_one_million_chars(self):
        assert len(export_files.scan_text(b"a" * 1_000_005)) == 1_000_000
        assert export_files.scan_text(b"\xff") == "�"


class TestCardDetails:
    def test_lists_preview_then_counts_path_and_hash(self):
        text = export_files.card_details({"Table": "T"}, 1234, ["a", "b"], "~/d/f.csv", "abc")
        assert text.split("\n") == [
            "Table: T", "Rows: 1,234", "Columns: a, b", "Saved to: ~/d/f.csv", "SHA-256: abc",
        ]

    def test_has_no_agent_placeholder(self):
        assert "{agent}" not in export_files.card_details({}, 0, [], "p", "h")


class TestExportSummary:
    def test_merges_delivery(self):
        out = export_files.export_summary(["a"], [[1], [2]], "h", {"path": "/p", "delivery": "local_disk"})
        assert out == {"rows": 2, "columns": ["a"], "sha256": "h", "path": "/p", "delivery": "local_disk"}
