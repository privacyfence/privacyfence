"""Unit tests for privacyfence.grist_csv (ADR 0148)."""
from __future__ import annotations

import calendar
from datetime import date

import pytest

from privacyfence import grist_csv
from privacyfence.grist_client import GristColumn
from privacyfence.grist_csv import (
    CsvRow,
    GristCsvError,
    base_type,
    key_index,
    parse_csv,
    same_key,
)

pytestmark = pytest.mark.unit

COLUMNS = [
    GristColumn("Name", "Full name", "Text", False),
    GristColumn("Age", "Age", "Int", False),
    GristColumn("Score", "Score", "Numeric", False),
    GristColumn("Active", "Active", "Bool", False),
    GristColumn("Born", "Born on", "Date", False),
    GristColumn("Kind", "Kind", "Choice", False),
    GristColumn("Misc", "Misc", "Any", False),
    GristColumn("Total", "Total", "Numeric", True),
    GristColumn("Boss", "Boss", "Ref:People", False),
    GristColumn("Tags", "Tags", "ChoiceList", False),
    GristColumn("Twin1", "Twin", "Text", False),
    GristColumn("Twin2", "Twin", "Text", False),
]


def parse(text: str | bytes, columns=None):
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    return parse_csv(data, COLUMNS if columns is None else columns, "People")


def error_of(text: str | bytes, columns=None) -> str:
    with pytest.raises(GristCsvError) as info:
        parse(text, columns)
    return str(info.value)


class TestBaseType:
    def test_strips_the_target_table(self):
        assert base_type("Ref:People") == "Ref"
        assert base_type("Text") == "Text"


class TestDecodingAndDelimiter:
    def test_not_utf8(self):
        assert error_of(b"Name\n\xff\xfe\n") == "The CSV file is not UTF-8 text."

    def test_a_bom_is_dropped(self):
        parsed = parse(b"\xef\xbb\xbfName\nAda\n")
        assert parsed.column_ids == ["Name"]
        assert parsed.text.startswith("Name")

    @pytest.mark.parametrize("data", [b"", b"\n", b"\xef\xbb\xbf", b"\nName\nAda\n", b"   \n"])
    def test_empty_file_or_first_line(self, data):
        assert error_of(data) == "The CSV file is empty."

    def test_semicolon_delimiter(self):
        parsed = parse("Name;Age\nAda;36\n")
        assert parsed.column_ids == ["Name", "Age"]
        assert parsed.rows[0].values == {"Name": "Ada", "Age": 36}

    def test_tab_delimiter(self):
        parsed = parse("Name\tAge\nAda\t36\n")
        assert parsed.rows[0].values == {"Name": "Ada", "Age": 36}

    def test_comma_wins_a_tie(self):
        # one comma and one semicolon: two fields either way
        assert error_of("Name,Age;x\nAda,36;1\n").startswith("Unknown column(s)")

    def test_single_column_file(self):
        assert parse("Name\nAda\n").column_ids == ["Name"]


class TestHeader:
    def test_empty_header_cell(self):
        assert error_of("Name,,Age\nAda,x,1\n") == "Header column 2 is empty."

    def test_trailing_comma_makes_an_empty_header_cell(self):
        assert error_of("Name,\nAda,\n") == "Header column 2 is empty."

    def test_matches_by_label(self):
        parsed = parse("Full name,Born on\nAda,1815-12-10\n")
        assert parsed.column_ids == ["Name", "Born"]
        assert parsed.column_types == {"Name": "Text", "Born": "Date"}

    def test_id_wins_over_a_label(self):
        columns = [
            GristColumn("A", "B", "Text", False),
            GristColumn("B", "Other", "Text", False),
        ]
        assert parse("B\nx\n", columns).column_ids == ["B"]

    def test_names_are_stripped_and_case_sensitive(self):
        assert parse(" Name \nAda\n").column_ids == ["Name"]
        assert error_of("name\nAda\n").startswith("Unknown column(s) in table People: name.")

    def test_ambiguous_label(self):
        assert error_of("Twin\nx\n") == (
            "Header Twin matches more than one column label; use the column id."
        )

    def test_unknown_columns_are_sorted(self):
        assert error_of("Name,Zed,Alpha\nAda,1,2\n") == (
            "Unknown column(s) in table People: Alpha, Zed. The header row must use column ids "
            "or labels; call grist_list_tables to see them."
        )

    def test_same_column_twice(self):
        assert error_of("Name,Full name\nAda,Ada\n") == (
            "Column Name appears more than once in the header row."
        )

    def test_too_many_columns(self):
        columns = [GristColumn(f"C{i}", f"C{i}", "Text", False) for i in range(grist_csv.MAX_COLUMNS + 1)]
        header = ",".join(c.id for c in columns)
        assert error_of(f"{header}\n{','.join('x' * len(columns))}\n", columns) == (
            "The CSV file has more than 100 columns."
        )

    def test_exactly_the_maximum_columns_is_fine(self):
        columns = [GristColumn(f"C{i}", f"C{i}", "Text", False) for i in range(grist_csv.MAX_COLUMNS)]
        header = ",".join(c.id for c in columns)
        parsed = parse(f"{header}\n{','.join('x' * len(columns))}\n", columns)
        assert len(parsed.column_ids) == grist_csv.MAX_COLUMNS

    def test_formula_columns(self):
        assert error_of("Name,Total\nAda,1\n") == (
            "Column(s) Total in table People are formula columns and cannot be written."
        )

    def test_several_formula_columns_are_sorted(self):
        columns = [
            GristColumn("Z", "Z", "Text", True),
            GristColumn("A", "A", "Text", True),
        ]
        assert error_of("Z,A\n1,2\n", columns) == (
            "Column(s) A, Z in table People are formula columns and cannot be written."
        )

    def test_unsupported_type_names_the_first_in_header_order(self):
        assert error_of("Tags,Boss\nx,y\n") == (
            "Column Tags has type ChoiceList, which CSV import does not support. "
            "Supported: Any, Bool, Choice, Date, Int, Numeric, Text."
        )
        assert "Column Boss has type Ref:People" in error_of("Name,Boss\nx,y\n")


class TestRows:
    def test_line_numbers_and_blank_lines(self):
        parsed = parse("Name\nAda\n\n  \nGrace\n")
        assert [(r.line, r.values["Name"]) for r in parsed.rows] == [(2, "Ada"), (5, "Grace")]

    def test_a_row_of_empty_cells_is_dropped(self):
        parsed = parse("Name,Age\n,\nAda,1\n")
        assert [r.line for r in parsed.rows] == [3]

    def test_quoted_embedded_delimiter_and_newline_keep_the_line_of_the_record_start(self):
        parsed = parse('Name,Age\n"Lovelace, Ada",1\n"multi\nline",2\nGrace,3\n')
        assert [(r.line, r.values["Name"]) for r in parsed.rows] == [
            (2, "Lovelace, Ada"), (3, "multi\nline"), (5, "Grace"),
        ]

    def test_crlf_and_bare_cr(self):
        assert [r.line for r in parse("Name\r\nAda\r\nGrace\r\n").rows] == [2, 3]

    def test_wrong_cell_count(self):
        assert error_of("Name,Age\nAda\nGrace,1,2\n") == (
            "Line 2: expected 2 cells, found 1.\nLine 3: expected 2 cells, found 3."
        )

    def test_no_data_rows(self):
        assert error_of("Name,Age\n") == "The CSV file has no data rows."
        assert error_of("Name,Age\n,\n\n") == "The CSV file has no data rows."

    def test_oversized_field_is_malformed(self):
        huge = "x" * 200_000
        assert error_of(f"Name\nAda\n{huge}\n") == "Line 3: the CSV file is malformed."

    def test_oversized_header_field_is_malformed(self):
        huge = "x" * 200_000
        assert error_of(f"{huge}\nAda\n") == "Line 1: the CSV file is malformed."

    def test_more_than_max_rows(self):
        body = "x\n" * (grist_csv.MAX_ROWS + 1)
        assert error_of(f"Name\n{body}") == "The CSV file has more than 20000 rows; split it."

    def test_max_rows_discards_collected_errors(self):
        body = "Ada,1,extra\n" + "x\n" * grist_csv.MAX_ROWS
        assert error_of(f"Name\n{body}") == "The CSV file has more than 20000 rows; split it."

    def test_exactly_max_rows_is_fine(self):
        body = "x\n" * grist_csv.MAX_ROWS
        assert len(parse(f"Name\n{body}").rows) == grist_csv.MAX_ROWS


class TestCells:
    def test_empty_cells_are_left_out_and_cells_are_stripped(self):
        parsed = parse("Name,Age,Kind\n  Ada  ,,x\n")
        assert parsed.rows[0].values == {"Name": "Ada", "Kind": "x"}

    def test_text_choice_any_are_kept_as_they_are(self):
        parsed = parse("Name,Kind,Misc\n007,1e3,true\n")
        assert parsed.rows[0].values == {"Name": "007", "Kind": "1e3", "Misc": "true"}

    def test_cell_too_long(self):
        assert error_of(f"Name\n{'x' * 10_001}\n") == (
            "Line 2, column Name: the value is longer than 10000 characters."
        )

    def test_cell_at_the_limit_is_fine(self):
        assert len(parse(f"Name\n{'x' * 10_000}\n").rows[0].values["Name"]) == 10_000

    @pytest.mark.parametrize("text,value", [("36", 36), ("+5", 5), ("-7", -7), ("007", 7)])
    def test_int(self, text, value):
        assert parse(f"Age\n{text}\n").rows[0].values["Age"] == value

    @pytest.mark.parametrize("text", ["1.5", "1_000", "٣", "1e3", "abc", "0x10", "--1", "1 000"])
    def test_int_rejected(self, text):
        assert error_of(f"Age\n{text}\n") == "Line 2, column Age: not a whole number."

    def test_int_bound(self):
        assert parse(f"Age\n{2**53}\n").rows[0].values["Age"] == 2**53
        assert parse(f"Age\n-{2**53}\n").rows[0].values["Age"] == -(2**53)
        assert error_of(f"Age\n{2**53 + 1}\n") == "Line 2, column Age: not a whole number."
        assert error_of(f"Age\n{'9' * 5000}\n") == "Line 2, column Age: not a whole number."

    @pytest.mark.parametrize("text,value", [
        ("3", 3), ("3.0", 3.0), ("-2.5", -2.5), (".5", 0.5), ("5.", 5.0), ("1e3", 1000.0),
        ("1.5E-2", 0.015), ("+1", 1),
    ])
    def test_numeric(self, text, value):
        got = parse(f"Score\n{text}\n").rows[0].values["Score"]
        assert got == value
        assert isinstance(got, int) == ("." not in text and "e" not in text.lower())

    def test_numeric_beyond_the_int_bound_is_a_float(self):
        got = parse(f"Score\n{2**53 + 1}\n").rows[0].values["Score"]
        assert isinstance(got, float)

    @pytest.mark.parametrize("text", ["1,5", "1_000", "٣", "abc", "1e999", "inf", "nan", "1e", "."])
    def test_numeric_rejected(self, text):
        # a decimal comma only fits inside quotes, which the header's delimiter would also split
        assert error_of(f'Score\n"{text}"\n') == "Line 2, column Score: not a number."

    @pytest.mark.parametrize("text,value", [
        ("true", True), ("TRUE", True), ("Yes", True), ("1", True),
        ("false", False), ("No", False), ("0", False),
    ])
    def test_bool(self, text, value):
        assert parse(f"Active\n{text}\n").rows[0].values["Active"] is value

    @pytest.mark.parametrize("text", ["maybe", "2", "y", "t"])
    def test_bool_rejected(self, text):
        assert error_of(f"Active\n{text}\n") == "Line 2, column Active: not true or false."

    def test_date_is_seconds_at_utc_midnight(self):
        got = parse("Born\n1970-01-02\n").rows[0].values["Born"]
        assert got == 86400 and isinstance(got, int)
        assert parse("Born\n2024-02-29\n").rows[0].values["Born"] == calendar.timegm(
            date(2024, 2, 29).timetuple()
        )

    @pytest.mark.parametrize("text", ["20240101", "2024-13-01", "2023-02-29", "2024-1-1", "٢٠٢٤-٠١-٠١", "01/02/2024"])
    def test_date_rejected(self, text):
        assert error_of(f"Born\n{text}\n") == (
            "Line 2, column Born: not a date in YYYY-MM-DD form."
        )


class TestCollectedErrors:
    def test_errors_are_capped_at_ten(self):
        rows = "\n".join("x" for _ in range(13))
        message = error_of(f"Age\n{rows}\n")
        lines = message.split("\n")
        assert len(lines) == 11
        assert lines[0] == "Line 2, column Age: not a whole number."
        assert lines[9] == "Line 11, column Age: not a whole number."
        assert lines[10] == "… and 3 more."

    def test_exactly_ten_errors_have_no_tail(self):
        rows = "\n".join("x" for _ in range(10))
        assert "more" not in error_of(f"Age\n{rows}\n")

    def test_errors_come_in_file_order_across_kinds(self):
        message = error_of("Name,Age\nAda\nGrace,x\n")
        assert message == "Line 2: expected 2 cells, found 1.\nLine 3, column Age: not a whole number."

    def test_no_message_contains_the_offending_cell(self):
        secret = "S3CRET-VALUE"
        messages = [
            error_of(f"Age\n{secret}\n"),
            error_of(f"Score\n{secret}\n"),
            error_of(f"Active\n{secret}\n"),
            error_of(f"Born\n{secret}\n"),
            error_of(f"Name,Age\n{secret}\n"),
            error_of(f"Name\n{secret * 2000}\n"),
            error_of(f"Name\n{secret}\n{'x' * 200_000}\n"),
            error_of(f"Name,{secret}\nAda,1\n".replace(secret, "Nope")),
        ]
        for message in messages:
            assert secret not in message


class TestSameKey:
    def test_integral_float_equals_int(self):
        assert same_key(3.0) == same_key(3) == 3
        assert isinstance(same_key(3.0), int)

    def test_other_values_are_unchanged(self):
        assert same_key(3.5) == 3.5
        assert same_key("3") == "3" and same_key("3") != same_key(3)
        assert same_key(None) is None

    def test_list_and_dict_cells_are_hashable_and_distinct_from_text(self):
        key = same_key(["E", "#REF!"])
        hash(key)
        assert key != same_key("E")
        assert same_key({"a": 1}) == same_key({"a": 1})


def rows_of(*keys, column="Name"):
    return [CsvRow(line=i + 2, values={column: key} if key is not None else {}) for i, key in enumerate(keys)]


class TestKeyIndex:
    def test_builds_an_index_by_comparable_key(self):
        rows = rows_of(3, "b", column="K")
        index = key_index(rows, "K")
        assert index[3] is rows[0] and index["b"] is rows[1]

    def test_empty_key(self):
        assert_error(rows_of("a", None), "Line 3: the key column Name is empty.")

    def test_long_key(self):
        assert_error(rows_of("a", "x" * 201), "Line 3: the key in column Name is longer than 200 characters.")
        assert key_index(rows_of("x" * 200), "Name")

    def test_duplicate_key(self):
        assert_error(rows_of("a", "b", "a"), "Lines 2, 4 have the same key in column Name.")

    def test_duplicate_across_int_and_float(self):
        assert_error(rows_of(3, 3.0, column="K"), "Lines 2, 3 have the same key in column K.", "K")

    def test_empty_key_is_reported_before_a_duplicate(self):
        assert_error(rows_of("a", "a", None), "Line 4: the key column Name is empty.")


def assert_error(rows, message, column="Name"):
    with pytest.raises(GristCsvError) as info:
        key_index(rows, column)
    assert str(info.value) == message
