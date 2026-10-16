"""Tests for privacyfence.salesforce_writes, the pure checks the Salesforce write tools run before
their approval card.

The invariant that matters most: no error message contains a value read from Salesforce, because
a refusal before approval would otherwise show provider data nobody reviewed (docs/adr/0148).
"""
from __future__ import annotations

import json

import pytest

from privacyfence.salesforce_client import SalesforceField
from privacyfence.salesforce_writes import (
    MAX_BATCH_RECORDS,
    MAX_PAYLOAD_CHARS,
    WRITABLE_TYPES,
    cell_text,
    field_index,
    key_norm,
    key_text,
    parse_fields_object,
    parse_records,
    resolve_fields,
    resolve_key_field,
    values_differ,
)

pytestmark = pytest.mark.unit


def _f(name, type_="string", *, createable=True, updateable=True, nillable=True, external_id=False):
    return SalesforceField(
        name=name, label=name + " label", type=type_, createable=createable,
        updateable=updateable, nillable=nillable, external_id=external_id,
    )


INDEX = field_index([
    _f("Id", "id", createable=False, updateable=False, nillable=False),
    _f("Name"),
    _f("Industry", "picklist"),
    _f("Required__c", "string", nillable=False),
    _f("CreateOnly__c", "string", updateable=False),
    _f("UpdateOnly__c", "string", createable=False),
    _f("Address", "address"),
    _f("Active__c", "boolean"),
    _f("Count__c", "int"),
    _f("Amount__c", "double"),
    _f("Born__c", "date"),
    _f("Seen__c", "datetime"),
    _f("Cust__c", "string", external_id=True),
    _f("Num__c", "double", external_id=True),
    _f("Plain__c", "string"),
])


class TestParse:
    def test_valid_object(self):
        assert parse_fields_object('{"Name": "A", "N": 1, "F": 1.5, "B": true, "X": null}', "fields") == {
            "Name": "A", "N": 1, "F": 1.5, "B": True, "X": None,
        }

    def test_invalid_json(self):
        with pytest.raises(ValueError, match=r"^fields: invalid JSON: Expecting property name enclosed in double quotes$"):
            parse_fields_object("{nope", "fields")

    def test_not_an_object(self):
        with pytest.raises(ValueError) as e:
            parse_fields_object("[1]", "fields")
        assert str(e.value) == "fields: must be a JSON object of field names to values"

    def test_empty_object(self):
        with pytest.raises(ValueError) as e:
            parse_fields_object("{}", "fields")
        assert str(e.value) == "fields: names no field"

    @pytest.mark.parametrize("raw", ['{"A": {"b": 1}}', '{"A": [1]}'])
    def test_nested_value(self, raw):
        with pytest.raises(ValueError) as e:
            parse_fields_object(raw, "fields")
        assert str(e.value) == "fields: A must be text, a number, true/false or null"

    @pytest.mark.parametrize("raw", ['{"A": NaN}', '{"A": Infinity}', '{"A": -Infinity}'])
    def test_non_finite(self, raw):
        with pytest.raises(ValueError) as e:
            parse_fields_object(raw, "fields")
        assert str(e.value) == "fields: A must be a finite number"

    def test_repeated_name(self):
        with pytest.raises(ValueError) as e:
            parse_fields_object('{"A": 1, "A": 2}', "fields")
        assert str(e.value) == "fields: A is named twice"

    def test_too_large(self):
        with pytest.raises(ValueError) as e:
            parse_fields_object(" " * (MAX_PAYLOAD_CHARS + 1), "fields")
        assert str(e.value) == "fields: larger than 1,000,000 characters"

    def test_records_valid(self):
        assert parse_records('[{"A": 1}, {"B": "x"}]') == [{"A": 1}, {"B": "x"}]

    def test_records_not_a_list(self):
        with pytest.raises(ValueError) as e:
            parse_records('{"A": 1}')
        assert str(e.value) == "records: must be a JSON array of objects"

    def test_records_invalid_json(self):
        with pytest.raises(ValueError, match=r"^records: invalid JSON"):
            parse_records("[")

    def test_records_too_large(self):
        with pytest.raises(ValueError) as e:
            parse_records(" " * (MAX_PAYLOAD_CHARS + 1))
        assert str(e.value) == "records: larger than 1,000,000 characters"

    def test_zero_records(self):
        with pytest.raises(ValueError) as e:
            parse_records("[]")
        assert str(e.value) == "records: must hold 1 to 200 records, got 0"

    def test_too_many_records(self):
        raw = json.dumps([{"A": 1}] * (MAX_BATCH_RECORDS + 1))
        with pytest.raises(ValueError) as e:
            parse_records(raw)
        assert str(e.value) == "records: must hold 1 to 200 records, got 201"

    def test_max_records_ok(self):
        assert len(parse_records(json.dumps([{"A": 1}] * MAX_BATCH_RECORDS))) == MAX_BATCH_RECORDS

    def test_element_not_an_object(self):
        with pytest.raises(ValueError) as e:
            parse_records('[{"A": 1}, {"A": 1}, "x"]')
        assert str(e.value) == "records[2]: must be a JSON object of field names to values"

    def test_element_repeated_name(self):
        with pytest.raises(ValueError) as e:
            parse_records('[{"A": 1}, {"A": 1, "A": 2}]')
        assert str(e.value) == "records[1]: A is named twice"


class TestResolveFields:
    def test_canonical_names_in_input_order(self):
        out = resolve_fields({"industry": "Retail", "NAME": "x"}, INDEX, "Account", "create", "fields")
        assert list(out.items()) == [("Industry", "Retail"), ("Name", "x")]

    def test_unknown_field(self):
        with pytest.raises(ValueError) as e:
            resolve_fields({"Nope": 1}, INDEX, "Account", "create", "fields")
        assert str(e.value) == "fields: Account has no field 'Nope'"

    def test_same_field_twice_in_different_case(self):
        with pytest.raises(ValueError) as e:
            resolve_fields({"Name": "a", "name": "b"}, INDEX, "Account", "create", "records[3]")
        assert str(e.value) == "records[3]: field Name is named twice"

    def test_not_createable_but_updateable(self):
        with pytest.raises(ValueError) as e:
            resolve_fields({"UpdateOnly__c": "x"}, INDEX, "Account", "create", "fields")
        assert str(e.value) == "fields: Account.UpdateOnly__c cannot be set when creating a record"
        assert resolve_fields({"UpdateOnly__c": "x"}, INDEX, "Account", "update", "fields") == {
            "UpdateOnly__c": "x"
        }

    def test_not_updateable_but_createable(self):
        with pytest.raises(ValueError) as e:
            resolve_fields({"CreateOnly__c": "x"}, INDEX, "Account", "update", "fields")
        assert str(e.value) == "fields: Account.CreateOnly__c cannot be set when updating a record"
        assert resolve_fields({"CreateOnly__c": "x"}, INDEX, "Account", "create", "fields") == {
            "CreateOnly__c": "x"
        }

    def test_unwritable_type(self):
        with pytest.raises(ValueError) as e:
            resolve_fields({"Address": "x"}, INDEX, "Account", "create", "fields")
        assert str(e.value) == (
            "fields: Account.Address has type address, which PrivacyFence does not write"
        )

    def test_null_on_create(self):
        with pytest.raises(ValueError) as e:
            resolve_fields({"Name": None}, INDEX, "Account", "create", "fields")
        assert str(e.value) == "fields: Name is null; leave it out instead"

    def test_null_on_update(self):
        assert resolve_fields({"Name": None}, INDEX, "Account", "update", "f") == {"Name": None}
        with pytest.raises(ValueError) as e:
            resolve_fields({"Required__c": None}, INDEX, "Account", "update", "fields")
        assert str(e.value) == "fields: Required__c cannot be cleared"

    GOOD = {
        "text": ("Name", "x"),
        "bool": ("Active__c", True),
        "int": ("Count__c", 3),
        "number": ("Amount__c", 2.5),
        "date": ("Born__c", "2026-10-14"),
        "datetime": ("Seen__c", "2026-10-14T10:00:00Z"),
    }
    BAD = {
        "text": ("Name", 5, "text"),
        "bool": ("Active__c", "true", "true or false"),
        "int": ("Count__c", True, "a whole number"),
        "number": ("Amount__c", "5", "a number"),
        "date": ("Born__c", "14/10/2026", "a date as YYYY-MM-DD"),
        "datetime": ("Seen__c", "2026-10-14 10:00", "a date-time in ISO 8601"),
    }

    @pytest.mark.parametrize("kind", sorted(GOOD))
    def test_good_value_per_kind(self, kind):
        name, value = self.GOOD[kind]
        assert resolve_fields({name: value}, INDEX, "Account", "create", "f") == {name: value}

    @pytest.mark.parametrize("kind", sorted(BAD))
    def test_bad_value_per_kind(self, kind):
        name, value, expected = self.BAD[kind]
        with pytest.raises(ValueError) as e:
            resolve_fields({name: value}, INDEX, "Account", "create", "fields")
        assert str(e.value) == f"fields: {name} must be {expected}"

    def test_number_accepts_int_and_int_refuses_float(self):
        assert resolve_fields({"Amount__c": 5}, INDEX, "A", "create", "f") == {"Amount__c": 5}
        with pytest.raises(ValueError, match="a whole number"):
            resolve_fields({"Count__c": 1.5}, INDEX, "A", "create", "f")

    def test_number_refuses_bool(self):
        with pytest.raises(ValueError, match="a number"):
            resolve_fields({"Amount__c": False}, INDEX, "A", "create", "f")

    def test_datetime_variants(self):
        for ok in ("2026-10-14T10:00Z", "2026-10-14T10:00:00.123+0200", "2026-10-14T10:00:00-02:00"):
            assert resolve_fields({"Seen__c": ok}, INDEX, "A", "create", "f") == {"Seen__c": ok}
        with pytest.raises(ValueError):
            resolve_fields({"Seen__c": "2026-10-14T10:00:00"}, INDEX, "A", "create", "f")

    def test_every_writable_type_has_a_known_kind(self):
        assert set(WRITABLE_TYPES.values()) == set(self.GOOD)


class TestKeyField:
    def test_id_accepted(self):
        assert resolve_key_field("id", INDEX, "Account").name == "Id"

    def test_external_id_string_accepted(self):
        assert resolve_key_field("cust__c", INDEX, "Account").name == "Cust__c"

    def test_non_external_refused(self):
        with pytest.raises(ValueError) as e:
            resolve_key_field("Plain__c", INDEX, "Account")
        assert str(e.value) == (
            "key_field: Account.Plain__c is not an External ID field; "
            "use Id or a field marked External ID"
        )

    def test_external_id_of_wrong_type_refused(self):
        with pytest.raises(ValueError, match="is not an External ID field"):
            resolve_key_field("Num__c", INDEX, "Account")

    def test_unknown_refused(self):
        with pytest.raises(ValueError) as e:
            resolve_key_field("Zip", INDEX, "Account")
        assert str(e.value) == "key_field: Account has no field 'Zip'"

    @pytest.mark.parametrize("bad", ["", "x" * 256, "a\tb", 5, None])
    def test_key_text_refuses(self, bad):
        with pytest.raises(ValueError) as e:
            key_text(bad, "records[1]", "Cust__c")
        assert str(e.value) == (
            "records[1]: Cust__c must be non-empty text without control characters, "
            "at most 255 characters"
        )

    def test_key_text_accepts(self):
        assert key_text("x" * 255, "w", "K") == "x" * 255

    def test_key_norm(self):
        assert key_norm("001000000000001AAA", True) == key_norm("001000000000001", True)
        assert key_norm("001000000000001AAA", True) != key_norm("001000000000002AAA", True)
        assert key_norm("ABC", False) == key_norm("abc", False)


class TestValuesDiffer:
    def test_empties(self):
        assert not values_differ(None, "", "string")
        assert not values_differ("", None, "string")
        assert values_differ("a", None, "string")
        assert values_differ(None, "a", "string")
        assert values_differ(0, None, "int")

    def test_numbers(self):
        assert not values_differ(3, 3.0, "double")
        assert values_differ(3, 4, "int")
        assert not values_differ("3", 3.0, "currency")
        assert values_differ("x", 3, "percent")

    def test_booleans(self):
        assert not values_differ(True, "true", "boolean")
        assert not values_differ(False, "false", "boolean")
        assert values_differ(True, False, "boolean")

    def test_datetimes(self):
        assert not values_differ("2026-10-14T10:00:00.000+0000", "2026-10-14T10:00:00Z", "datetime")
        assert values_differ("2026-10-14T10:00:00Z", "2026-10-14T11:00:00Z", "datetime")
        assert not values_differ("2026-10-14T12:00:00+0200", "2026-10-14T10:00:00Z", "datetime")

    def test_unparseable_datetime_compares_strings(self):
        assert values_differ("nonsense", "other", "datetime")
        assert not values_differ("nonsense", "nonsense", "datetime")

    def test_naive_against_aware_datetime_compares_strings(self):
        assert values_differ("2026-10-14T10:00:00", "2026-10-14T10:00:00Z", "datetime")

    def test_multipicklist(self):
        assert not values_differ("a;b", "b;a", "multipicklist")
        assert values_differ("a;b", "a;c", "multipicklist")

    def test_reference(self):
        assert not values_differ("001000000000001", "001000000000001AAA", "reference")
        assert values_differ("001000000000001", "001000000000002AAA", "reference")
        assert values_differ("not an id", "001000000000001AAA", "reference")
        assert not values_differ("same", "same", "reference")

    def test_other_types_compare_text(self):
        assert values_differ("Acme", "acme", "string")
        assert not values_differ("Acme", "Acme", "string")


class TestCellText:
    def test_empty_and_bool(self):
        assert cell_text(None) == "(empty)"
        assert cell_text("") == "(empty)"
        assert cell_text(True) == "true"
        assert cell_text(False) == "false"
        assert cell_text(0) == "0"

    def test_cut(self):
        assert cell_text("x" * 200) == "x" * 200
        assert cell_text("x" * 201) == "x" * 200 + "…"
        assert cell_text("x" * 20, 5) == "xxxxx…"

    def test_no_limit(self):
        assert cell_text("x" * 500, None) == "x" * 500


class TestNoValueInMessages:
    SECRET = "SECRET-VALUE-123"

    def test_resolve_fields_failures(self):
        cases = [
            ({"Name": self.SECRET, "name": self.SECRET}, "create"),
            ({"Nope": self.SECRET}, "create"),
            ({"UpdateOnly__c": self.SECRET}, "create"),
            ({"CreateOnly__c": self.SECRET}, "update"),
            ({"Address": self.SECRET}, "create"),
            ({"Active__c": self.SECRET}, "create"),
            ({"Count__c": self.SECRET}, "create"),
            ({"Amount__c": self.SECRET}, "create"),
            ({"Born__c": self.SECRET}, "create"),
            ({"Seen__c": self.SECRET}, "create"),
            ({"Name": 12}, "create"),
        ]
        for values, mode in cases:
            with pytest.raises(ValueError) as e:
                resolve_fields(values, INDEX, "Account", mode, "records[0]")
            assert self.SECRET not in str(e.value)

    def test_parse_failures(self):
        for raw in (
            json.dumps({"A": {"x": self.SECRET}}),
            json.dumps({"A": [self.SECRET]}),
            '{"A": "%s", "A": 1}' % self.SECRET,
        ):
            with pytest.raises(ValueError) as e:
                parse_fields_object(raw, "fields")
            assert self.SECRET not in str(e.value)

    def test_key_failures(self):
        with pytest.raises(ValueError) as e:
            key_text(self.SECRET + "\n", "records[0]", "Cust__c")
        assert self.SECRET not in str(e.value)
