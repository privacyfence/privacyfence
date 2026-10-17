"""Tests for privacyfence.personal_settings: the pure export/import of a person's own settings."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from privacyfence import paths, personal_settings as ps, step_up_config
from privacyfence.policy import catalogue, propose, scope_rows
from privacyfence.policy.registry import Verb
from privacyfence.policy.scope_rows import ScopeRow
from privacyfence.principal import Principal

NOW = datetime(2026, 10, 16, 9, 30, 0, 123, tzinfo=timezone.utc)
NOT_AN_EXPORT = "This is not a PrivacyFence settings export."


def _scoped(scope, value, verbs=None):
    wanted = tuple(verbs) if verbs is not None else scope_rows.scope_verbs(scope)
    return ScopeRow(
        id=scope_rows.row_id_for(scope, value), scope=scope, value=value,
        verbs=frozenset(wanted), operations=scope_rows.implied_operations(scope, wanted),
    )


def _group_scope():
    return next(s for s in propose.SCOPES_BY_GROUP if scope_rows.scope_verbs(s))


def _extra_scope():
    return next(s for s in catalogue.EXTRA_SCOPES if scope_rows.scope_verbs(s))


def _two_rows():
    return [_scoped(_group_scope(), ["FOLDER1"]), _scoped(_extra_scope(), ["PROJECT1"])]


def _custom():
    return ScopeRow(
        id="custom-1", scope=None, value=None, verbs=frozenset({Verb.READ}),
        operations=frozenset({"gmail.search_messages"}), predicate="always_allow",
    )


def _doc_bytes(rows=(), **overrides):
    document = ps.build_export(rows, None, mode="local", version="6.3.0", now=NOW)
    document.update(overrides)
    return json.dumps(document).encode()


def _parse(rows=(), **overrides):
    return ps.parse_document(_doc_bytes(rows, **overrides))


def _plan(document, existing=(), *, active=True, floor="writes", personal=None):
    return ps.plan_import(
        document, list(existing), step_up_active=active, step_up_floor=floor if active else None,
        current_personal_scope=personal,
    )


def _status_of(rows, existing=()):
    return [item.status for item in _plan(_parse(rows), existing).items]


class TestRoundTrip:
    def test_export_parse_plan_apply_reproduces_the_rows(self):
        originals = _two_rows()
        document = ps.parse_document(ps.export_bytes(ps.build_export(
            originals, None, mode="local", version="6.3.0", now=NOW,
        )))
        plan = _plan(document, [])
        assert [item.status for item in plan.items] == [ps.ItemStatus.ADD, ps.ItemStatus.ADD]
        rebuilt: list[ScopeRow] = []
        for item in plan.items:
            rebuilt, _ = scope_rows.add_rules(rebuilt, scope_rows.compile_row(item.row))
        assert [scope_rows.row_to_dict(r) for r in rebuilt] == [scope_rows.row_to_dict(r) for r in originals]


class TestExport:
    def test_top_level_keys_and_header_fields(self):
        document = ps.build_export(_two_rows(), "writes_and_reads", mode="org", version="6.3.0", now=NOW)
        assert list(json.loads(ps.export_bytes(document))) == [
            "format", "format_version", "exported_at", "source", "auto_accept", "step_up", "not_portable",
        ]
        assert document["format"] == ps.FORMAT and document["format_version"] == 1
        assert document["exported_at"] == "2026-10-16T09:30:00+00:00"
        assert document["source"] == {"mode": "org", "version": "6.3.0"}
        assert document["step_up"] == {"scope": "writes_and_reads"}
        assert document["auto_accept"]["version"] == 3

    def test_exported_at_is_utc(self):
        local = NOW.astimezone(timezone(timedelta(hours=2)))
        document = ps.build_export([], None, mode="local", version="1", now=local)
        assert document["exported_at"] == "2026-10-16T09:30:00+00:00"

    def test_export_bytes_is_indented_utf8_with_trailing_newline(self):
        raw = ps.export_bytes({"a": 1})
        assert raw == b'{\n  "a": 1\n}\n'

    def test_former_ids_are_dropped(self):
        row = dataclasses.replace(_two_rows()[0], former_ids=("old-1",))
        document = ps.build_export([row], None, mode="local", version="1", now=NOW)
        assert "former_ids" not in document["auto_accept"]["rules"][0]

    def test_custom_rows_are_omitted_and_noted_only_then(self):
        plain = ps.build_export(_two_rows(), None, mode="local", version="1", now=NOW)
        assert plain["not_portable"] == [{"item": i, "note": n} for i, n in ps.NOT_PORTABLE]
        assert len(ps.NOT_PORTABLE) == 6

        mixed = ps.build_export([*_two_rows(), _custom()], None, mode="local", version="1", now=NOW)
        assert len(mixed["auto_accept"]["rules"]) == 2
        assert {"item": "Custom rules", "note": (
            "Rules written by hand in settings.yaml are not exported; add them again on the Auto-accept page."
        )} in mixed["not_portable"]
        assert len(mixed["not_portable"]) == 7

    def test_nothing_identifying_is_written(self):
        text = ps.export_bytes(ps.build_export(_two_rows(), None, mode="local", version="1", now=NOW)).decode()
        assert "principal" not in text.lower()


class TestParseDocumentErrors:
    def _error(self, raw):
        with pytest.raises(ps.PersonalSettingsError) as caught:
            ps.parse_document(raw)
        return str(caught.value)

    def test_too_large(self):
        assert self._error(b" " * (ps.MAX_IMPORT_BYTES + 1)) == (
            "This file is too large to be a PrivacyFence settings export."
        )

    def test_exactly_the_limit_is_not_too_large(self):
        assert self._error(b" " * ps.MAX_IMPORT_BYTES) == NOT_AN_EXPORT

    @pytest.mark.parametrize("raw", [b"\xff\xfe", b"not json", b"[1, 2]", b'"text"', b"null"])
    def test_not_utf8_not_json_or_not_an_object(self, raw):
        assert self._error(raw) == NOT_AN_EXPORT

    def test_deeply_nested_input(self):
        assert self._error(b"[" * 300 + b"]" * 300) == NOT_AN_EXPORT
        assert self._error(b"[" * 200_000) == NOT_AN_EXPORT

    def test_wrong_format(self):
        assert self._error(_doc_bytes(format="something-else")) == NOT_AN_EXPORT

    def test_newer_format_version(self):
        assert self._error(_doc_bytes(format_version=2)) == (
            "This file was made by a newer PrivacyFence. Update this install and try again."
        )

    @pytest.mark.parametrize("version", [0, -1, "1", 1.0, True, None, [1]])
    def test_other_format_versions(self, version):
        assert self._error(_doc_bytes(format_version=version)) == NOT_AN_EXPORT

    @pytest.mark.parametrize("section", [None, [], {}, {"rules": "x"}, {"rules": {}}, "text"])
    def test_missing_rules_section(self, section):
        assert self._error(_doc_bytes(auto_accept=section)) == "This settings export has no rules section."

    def test_empty_rules_list_is_valid(self):
        document = _parse(auto_accept={"version": 3, "rules": []})
        assert document.rows == () and document.dropped_rows == 0

    def test_too_many_rows(self):
        rules = [{}] * (ps.MAX_IMPORT_ROWS + 1)
        assert self._error(_doc_bytes(auto_accept={"rules": rules})) == (
            "This settings export has more than 500 rules."
        )
        assert _parse(auto_accept={"rules": [{}] * ps.MAX_IMPORT_ROWS}).dropped_rows == ps.MAX_IMPORT_ROWS


class TestParseDocument:
    def _with_value(self, value):
        row = scope_rows.row_to_dict(_scoped(_group_scope(), ["x"]))
        row["value"] = value
        return _parse(auto_accept={"version": 3, "rules": [row]})

    def test_a_good_export_reads_back(self):
        originals = _two_rows()
        document = _parse(originals)
        assert list(document.rows) == originals
        assert document.dropped_rows == 0
        assert document.source_mode == "local"
        assert document.exported_at == "2026-10-16T09:30:00+00:00"
        assert document.step_up_scope is None

    def test_malformed_entries_are_counted(self):
        good = scope_rows.row_to_dict(_two_rows()[0])
        document = _parse(auto_accept={"rules": [good, "junk", {"id": "x"}, 5, None]})
        assert len(document.rows) == 1 and document.dropped_rows == 4

    @pytest.mark.parametrize(
        "value", [{"a": 1}, "x" * 513, ["x"] * 201, ["ok", 5], 5, True],
    )
    def test_bad_value_shapes_are_dropped(self, value):
        document = self._with_value(value)
        assert document.rows == () and document.dropped_rows == 1

    @pytest.mark.parametrize("value", [None, "x" * 512, ["x" * 512] * 200, [], "folder"])
    def test_good_value_shapes_are_kept(self, value):
        assert len(self._with_value(value).rows) == 1

    def test_former_ids_from_the_file_are_cleared(self):
        row = scope_rows.row_to_dict(_two_rows()[0])
        row["former_ids"] = ["old"]
        document = _parse(auto_accept={"rules": [row]})
        assert document.rows[0].former_ids == ()

    def test_unknown_top_level_keys_are_ignored(self):
        assert _parse(extra="x", more={"a": 1}).dropped_rows == 0

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("writes", "writes"), ("writes_and_reads", "writes_and_reads"), ("bogus", None), (3, None),
         (["writes"], None), (None, None)],
    )
    def test_step_up_scope(self, raw, expected):
        assert _parse(step_up={"scope": raw}).step_up_scope == expected

    @pytest.mark.parametrize("step_up", [None, "x", [], {}])
    def test_step_up_section_of_the_wrong_shape(self, step_up):
        assert _parse(step_up=step_up).step_up_scope is None

    @pytest.mark.parametrize(
        ("source", "expected"),
        [({"mode": "local"}, "local"), ({"mode": "org"}, "org"), ({"mode": "other"}, ""),
         ({"mode": ["org"]}, ""), ({}, ""), ("x", ""), (None, "")],
    )
    def test_source_mode_is_coerced(self, source, expected):
        assert _parse(source=source).source_mode == expected

    @pytest.mark.parametrize(
        ("stamp", "expected"),
        [("2026-10-16T09:30:00+00:00", "2026-10-16T09:30:00+00:00"), ("x" * 64, "x" * 64),
         ("x" * 65, ""), (5, ""), (None, "")],
    )
    def test_exported_at_is_coerced(self, stamp, expected):
        assert _parse(exported_at=stamp).exported_at == expected


class TestPlanImportRows:
    def test_custom_row_is_skipped_with_the_exact_reason(self):
        item = _plan(_parse_rows([_custom()])).items[0]
        assert item.status is ps.ItemStatus.SKIP
        assert item.reason == "Custom rules are not imported; add them again on the Auto-accept page."

    def test_a_hostile_custom_row_never_becomes_add(self):
        hostile = {
            "id": "r-evil", "predicate": "always_allow", "value": None, "verbs": ["read"],
            "operations": ["gmail.send_message"], "conditions": [],
        }
        document = _parse(auto_accept={"version": 3, "rules": [hostile]})
        assert len(document.rows) == 1
        plan = _plan(document)
        assert plan.items[0].status is ps.ItemStatus.SKIP
        assert plan.counts() == {"add": 0, "extend": 0, "present": 0, "skip": 1}

    def test_unknown_tool_is_skipped(self):
        row = dataclasses.replace(_two_rows()[0], operations=frozenset({"drive.not_a_real_tool"}))
        item = _plan(_parse_rows([row])).items[0]
        assert item.status is ps.ItemStatus.SKIP
        assert item.reason == "Needs a newer PrivacyFence (unknown tool)."

    def test_uncompilable_row_is_skipped(self):
        foreign = _two_rows()[1].operations  # known operations, none of them the group scope's
        row = dataclasses.replace(_two_rows()[0], operations=foreign)
        item = _plan(_parse_rows([row])).items[0]
        assert item.status is ps.ItemStatus.SKIP
        assert item.reason == "This rule cannot be applied by this version."

    def test_add(self):
        item = _plan(_parse_rows(_two_rows()[:1])).items[0]
        assert item.status is ps.ItemStatus.ADD and item.reason == ""
        assert item.sentence

    def test_present(self):
        rows = _two_rows()
        assert _status_of(rows, rows) == [ps.ItemStatus.PRESENT, ps.ItemStatus.PRESENT]

    def test_extend_names_the_number_of_added_tools(self):
        scope = _group_scope()
        verbs = scope_rows.scope_verbs(scope)
        narrow = _scoped(scope, ["FOLDER1"], verbs[:1])
        full = _scoped(scope, ["FOLDER1"], verbs)
        expected = len(full.operations) - len(narrow.operations)
        assert expected > 0
        item = _plan(_parse_rows([full]), [narrow]).items[0]
        assert item.status is ps.ItemStatus.EXTEND
        assert item.reason == f"Adds {expected} tool(s) to a rule you already have."

    def test_forged_id_is_not_trusted(self):
        existing = _scoped(_group_scope(), ["FOLDER1"])
        forged = dataclasses.replace(existing, id="r-forged0000")
        item = _plan(_parse_rows([forged]), [existing]).items[0]
        assert item.status is ps.ItemStatus.PRESENT
        assert item.row.id == existing.id

        narrow = _scoped(_group_scope(), ["FOLDER1"], scope_rows.scope_verbs(_group_scope())[:1])
        item = _plan(_parse_rows([forged]), [narrow]).items[0]
        assert item.status is ps.ItemStatus.EXTEND
        assert item.row.id == narrow.id

    def test_forged_id_on_a_new_row_gets_the_real_id(self):
        row = _two_rows()[0]
        forged = dataclasses.replace(row, id="r-forged0000")
        item = _plan(_parse_rows([forged])).items[0]
        assert item.status is ps.ItemStatus.ADD and item.row.id == row.id

    def test_duplicate_of_an_earlier_file_row_is_present(self):
        row = _two_rows()[0]
        document = _parse_rows([row, row])
        assert [i.status for i in _plan(document).items] == [ps.ItemStatus.ADD, ps.ItemStatus.PRESENT]

    def test_a_later_file_row_extends_an_earlier_one(self):
        scope = _group_scope()
        verbs = scope_rows.scope_verbs(scope)
        first, second = _scoped(scope, ["F"], verbs[:1]), _scoped(scope, ["F"], verbs)
        assert [i.status for i in _plan(_parse_rows([first, second])).items] == [
            ps.ItemStatus.ADD, ps.ItemStatus.EXTEND,
        ]

    def test_existing_rows_are_not_mutated(self):
        existing = [_scoped(_group_scope(), ["FOLDER1"], scope_rows.scope_verbs(_group_scope())[:1])]
        before = list(existing)
        _plan(_parse_rows(_two_rows()), existing)
        assert existing == before

    def test_counts(self):
        scope = _group_scope()
        verbs = scope_rows.scope_verbs(scope)
        narrow = _scoped(scope, ["E"], verbs[:1])
        have = _scoped(scope, ["P"])
        rows = [_scoped(scope, ["E"], verbs), have, _scoped(scope, ["A"]), _custom()]
        plan = _plan(_parse_rows(rows), [narrow, have])
        assert plan.counts() == {"add": 1, "extend": 1, "present": 1, "skip": 1}


def _parse_rows(rows):
    return _parse(
        auto_accept={"version": 3, "rules": [scope_rows.row_to_dict(r) for r in rows]},
    )


class TestPlanImportStepUp:
    def _item(self, file_scope, **kwargs):
        document = _parse(step_up={"scope": file_scope})
        return _plan(document, **kwargs).step_up

    def test_no_scope_in_the_file_is_unchanged(self):
        item = self._item(None)
        assert item.status is ps.StepUpStatus.UNCHANGED
        assert item.file_scope is None
        assert item.reason == "The file sets no personal step-up level."

    def test_no_scope_in_the_file_is_unchanged_even_when_step_up_is_off(self):
        assert self._item(None, active=False).status is ps.StepUpStatus.UNCHANGED

    def test_step_up_off_is_skipped(self):
        item = self._item("writes_and_reads", active=False)
        assert item.status is ps.StepUpStatus.SKIP
        assert item.reason == "Passkey step-up is not turned on on this install."

    def test_wider_than_the_floor_applies(self):
        item = self._item("writes_and_reads", floor="writes")
        assert item.status is ps.StepUpStatus.APPLY
        assert item.file_scope == "writes_and_reads" and item.reason == ""

    def test_at_the_floor_is_unchanged(self):
        item = self._item("writes", floor="writes_and_pii_reads")
        assert item.status is ps.StepUpStatus.UNCHANGED
        assert item.reason == "Already at this level or wider."

    def test_covered_by_the_current_personal_scope_is_unchanged(self):
        item = self._item("writes_and_pii_reads", floor="writes", personal="writes_and_reads")
        assert item.status is ps.StepUpStatus.UNCHANGED

    def test_wider_than_the_current_personal_scope_applies(self):
        item = self._item("writes_and_reads", floor="writes", personal="writes_and_pii_reads")
        assert item.status is ps.StepUpStatus.APPLY

    def test_an_invalid_current_personal_scope_is_ignored(self):
        item = self._item("writes_and_pii_reads", floor="writes", personal="bogus")
        assert item.status is ps.StepUpStatus.APPLY


class TestToPreview:
    def test_exact_keys_and_values(self):
        rows = [*_two_rows(), _custom()]
        document = _parse_rows(rows)
        plan = _plan(document, [_two_rows()[1]])
        preview = plan.to_preview(sha256="abc", document=document)
        assert list(preview) == [
            "sha256", "source_mode", "exported_at", "counts", "dropped_rows", "items", "step_up", "warnings",
        ]
        assert preview["sha256"] == "abc"
        assert preview["source_mode"] == "local"
        assert preview["counts"] == {"add": 1, "extend": 0, "present": 1, "skip": 1}
        assert [list(i) for i in preview["items"]] == [["id", "sentence", "status", "reason"]] * 3
        assert preview["items"][0]["status"] == "add"
        assert preview["items"][0]["id"] == _two_rows()[0].id
        assert preview["step_up"] == {"status": "unchanged", "scope": None, "reason": (
            "The file sets no personal step-up level."
        )}

    def test_step_up_block(self):
        document = _parse(step_up={"scope": "writes_and_reads"})
        preview = _plan(document).to_preview(sha256="h", document=document)
        assert preview["step_up"] == {"status": "apply", "scope": "writes_and_reads", "reason": ""}

    def test_warning_when_something_will_be_written(self):
        document = _parse_rows(_two_rows())
        warnings = _plan(document).to_preview(sha256="h", document=document)["warnings"]
        assert warnings == [
            "Rules name resources (folders, channels, projects) by id. "
            "They only match if you connect the same accounts here."
        ]

    def test_no_id_warning_when_nothing_is_written(self):
        rows = _two_rows()
        document = _parse_rows(rows)
        assert _plan(document, rows).to_preview(sha256="h", document=document)["warnings"] == []

    def test_dropped_rows_warning_pluralises(self):
        good = scope_rows.row_to_dict(_two_rows()[0])
        for junk, expected in (
            (["j"], "1 entry in the file could not be read and were left out."),
            (["j", "k"], "2 entries in the file could not be read and were left out."),
        ):
            document = _parse(auto_accept={"rules": [good, *junk]})
            warnings = _plan(document, _two_rows()[:1]).to_preview(sha256="h", document=document)["warnings"]
            assert warnings == [expected]


class TestApplyImport:
    PRINCIPAL = Principal(id="alice")

    @pytest.fixture(autouse=True)
    def _data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "authority_dir", lambda principal=None: tmp_path)

    def test_merges_exactly_the_add_and_extend_rows(self):
        scope = _group_scope()
        verbs = scope_rows.scope_verbs(scope)
        narrow = _scoped(scope, ["E"], verbs[:1])
        have = _scoped(scope, ["P"])
        extend, add = _scoped(scope, ["E"], verbs), _scoped(scope, ["A"])
        plan = _plan(_parse_rows([extend, have, add, _custom()]), [narrow, have])
        calls: list[list] = []

        def merge(rules):
            calls.append(rules)
            return True

        result = ps.apply_import(plan, principal=self.PRINCIPAL, step_up_floor="writes", merge_rules=merge)
        assert calls == [scope_rows.compile_rows([extend, add])]
        assert result == {"added": 1, "extended": 1, "present": 1, "skipped": 1, "step_up_applied": False}

    def test_nothing_to_write_does_not_call_the_writer(self):
        rows = _two_rows()
        plan = _plan(_parse_rows(rows), rows)
        calls = []
        result = ps.apply_import(
            plan, principal=self.PRINCIPAL, step_up_floor="writes",
            merge_rules=lambda rules: calls.append(rules) or True,
        )
        assert calls == []
        assert result["added"] == 0 and result["present"] == 2

    def test_step_up_is_set_only_on_apply(self):
        document = _parse(step_up={"scope": "writes_and_reads"})
        plan = _plan(document, floor="writes")
        result = ps.apply_import(plan, principal=self.PRINCIPAL, step_up_floor="writes", merge_rules=lambda r: True)
        assert result["step_up_applied"] is True
        assert step_up_config.personal_scope(self.PRINCIPAL) == "writes_and_reads"

    @pytest.mark.parametrize("active", [True, False])
    def test_step_up_is_left_alone_when_not_apply(self, active):
        document = _parse(step_up={"scope": "writes"})
        plan = _plan(document, active=active, floor="writes_and_reads")
        result = ps.apply_import(plan, principal=self.PRINCIPAL, step_up_floor=None, merge_rules=lambda r: True)
        assert result["step_up_applied"] is False
        assert step_up_config.personal_scope(self.PRINCIPAL) is None

    def test_a_missing_floor_falls_back_to_the_narrowest_rung(self):
        document = _parse(step_up={"scope": "writes_and_reads"})
        plan = _plan(document, floor="writes")
        ps.apply_import(plan, principal=self.PRINCIPAL, step_up_floor=None, merge_rules=lambda r: True)
        assert step_up_config.personal_scope(self.PRINCIPAL) == "writes_and_reads"
