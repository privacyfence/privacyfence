"""Unit tests for privacyfence.connectors.salesforce.SalesforceConnector.

Two real bugs found while writing these tests, both fixed in
connectors/salesforce.py:

1. salesforce_get_record read record_dict.get("Name") after
   dataclasses.asdict(SalesforceRecord(...)), but SalesforceRecord nests
   actual Salesforce fields under a "fields" key -- "Name" is never a
   top-level key, so the preview/summary always fell back to showing the
   raw record_id instead of the record's real name.
2. salesforce_run_report read result_dict.get("name")/get("reportName") at
   the top level, but Salesforce's Analytics REST API nests the report's
   name under reportMetadata.name -- same class of bug, same always-None
   result.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from privacyfence import export_files
from privacyfence.org_mode import DownloadDeliveryConfig
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import salesforce as salesforce_module
from privacyfence.connectors.salesforce import SalesforceConnector
from privacyfence import cursors
from privacyfence import safe_errors
from privacyfence.connectors.salesforce import (
    SalesforceWriteRefusedError,
    SalesforceWriteUnconfirmedError,
)
from privacyfence.salesforce_client import (
    ReportFilter,
    ReportPage,
    ReportPagingError,
    SalesforceClient,
    SalesforceClientError,
    SalesforceField,
    SalesforceObjectNotFoundError,
    SalesforceRecord,
    SalesforceReport,
    SalesforceWriteError,
    SalesforceWriteUncertainError,
    WriteFailure,
)

from ...fixtures.salesforce_analytics import (
    COLUMNS,
    KEY,
    REPORT_ID,
    FakeAnalytics,
    make_rows,
    tabular_report,
)
from ...helpers import (
    assert_all_tools_leave_an_audit_trail,
    assert_no_placeholder_fields,
    assert_tool_definitions_complete,
)


def make_connector(my_email="me@example.com"):
    client = MagicMock()
    connector = SalesforceConnector(client)
    connector.my_email = my_email
    return connector, client


def make_real_client(sf: MagicMock) -> SalesforceClient:
    """A real SalesforceClient (real get_record/attributes-stripping) with
    only the underlying simple-salesforce object mocked -- same pattern as
    test_salesforce_client.py's with_fake_sf(). Used by
    TestFieldCompleteness to exercise the real raw-response ->
    SalesforceRecord -> popup-preview path end to end, instead of a
    hand-built SalesforceRecord like every other test in this file --
    this module's own docstring documents a real bug in exactly that gap
    (record_dict.get("Name") missing the nested "fields" key).
    """
    client = SalesforceClient(config={"access_token": "tok", "instance_url": "https://my.salesforce.com"})
    client._get_sf = lambda: sf
    return client


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(salesforce_module, "gated_call", fake_gated_call)
    return calls


def export_run(rows, *, row_limit=2000, **metadata):
    """One report run over ``rows`` as the Analytics API returns it (factMap T!T)."""
    saved = tabular_report(name="Pipeline", **metadata)
    return FakeAnalytics(saved, COLUMNS, rows, row_limit=row_limit)._run(saved)


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Salesforce tool"):
            await connector.call("salesforce_does_not_exist", {})


SALESFORCE_SIBLINGS: dict[str, tuple[str, ...]] = {
    "salesforce_list_reports": ("salesforce_run_report",),
    "salesforce_run_report": ("salesforce_list_reports",),
    "salesforce_export_report": ("salesforce_run_report",),
    "salesforce_search": ("salesforce_get_record",),
    "salesforce_get_record": ("salesforce_search",),
    "salesforce_create_record": ("salesforce_get_record",),
    "salesforce_update_record": ("salesforce_get_record",),
}


class TestToolDefinitions:
    """What an AI client reads to choose and call these tools: every parameter described, what
    each tool returns, the approval wording its gate implies, and the related tool to use
    instead. Glama's Tool Definition Quality Score grades exactly this."""

    def test_every_tool_definition_is_complete(self):
        assert_tool_definitions_complete(SalesforceConnector(MagicMock()), SALESFORCE_SIBLINGS)


class TestListReports:
    async def test_auto_accepts_and_serializes(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_reports.return_value = [
            SalesforceReport(id="00O1", name="Pipeline", report_type="Tabular", folder_name="Sales", description=""),
        ]

        result = await connector.call("salesforce_list_reports", {})

        assert result == [{
            "id": "00O1", "name": "Pipeline", "report_type": "Tabular",
            "folder_name": "Sales", "description": "",
        }]
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_reports.side_effect = SalesforceClientError("session expired")

        with pytest.raises(RuntimeError, match="session expired"):
            await connector.call("salesforce_list_reports", {})


class TestGetRecord:
    async def test_preview_shows_actual_record_name_not_record_id(self, gated_call_spy):
        # Regression test for bug #1: Name lives under record.fields, not at
        # the top level of asdict(record).
        connector, client = make_connector()
        client.get_record.return_value = SalesforceRecord(
            object_type="Account", id="001xx0000012345",
            fields={"Name": "Acme Corp", "Industry": "Technology"},
        )

        await connector.call("salesforce_get_record", {"object_type": "Account", "record_id": "001xx0000012345"})

        kwargs = gated_call_spy[0]
        # Name isn't known via any auto tool (Salesforce has no auto search
        # at all), so it's a new_info field, not preview.
        assert kwargs["new_info"]["Name"] == "Acme Corp"
        assert kwargs["new_info"]["Name"] != "001xx0000012345"
        assert kwargs["summary"] == "Read Account: Acme Corp"

    async def test_falls_back_to_record_id_when_no_name_field(self, gated_call_spy):
        connector, client = make_connector()
        client.get_record.return_value = SalesforceRecord(
            object_type="Task", id="00T1", fields={"Subject": "Follow up"},
        )

        await connector.call("salesforce_get_record", {"object_type": "Task", "record_id": "00T1"})

        assert gated_call_spy[0]["new_info"]["Name"] == "00T1"

    async def test_lowercase_name_field_also_recognized(self, gated_call_spy):
        connector, client = make_connector()
        client.get_record.return_value = SalesforceRecord(
            object_type="CustomObject__c", id="a001", fields={"name": "lowercase name"},
        )

        await connector.call("salesforce_get_record", {"object_type": "CustomObject__c", "record_id": "a001"})

        assert gated_call_spy[0]["new_info"]["Name"] == "lowercase name"

    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.get_record.return_value = SalesforceRecord(
            object_type="Contact", id="003xx", fields={"Name": "Bob Smith", "Email": "bob@example.com"},
        )

        result = await connector.call("salesforce_get_record", {"object_type": "Contact", "record_id": "003xx"})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "review"
        assert kwargs["preview"]["Object type"] == "Contact"
        assert kwargs["preview"]["Record ID"] == "003xx"
        assert kwargs["args"] == {"object_type": "Contact", "record_id": "003xx"}
        assert kwargs["raw_data"] == client.get_record.return_value
        assert result == {"object_type": "Contact", "id": "003xx", "fields": {"Name": "Bob Smith", "Email": "bob@example.com"}}
        # new_info names which fields are actually on this record (alphabetized);
        # the right-pane table carries the real values.
        assert kwargs["new_info"]["Field values"] == "Email, Name"
        assert kwargs["preview_tables"] == [
            {"headers": ["Field", "Value"], "rows": [["Email", "bob@example.com"], ["Name", "Bob Smith"]]},
        ]
        # v2's right pane shows only the table -- the plain-text field dump
        # (details_text, kept for legacy/PII-scan) would just duplicate it.
        assert kwargs["table_only"] is True

    async def test_unset_fields_excluded_from_field_list_and_table(self, gated_call_spy):
        connector, client = make_connector()
        client.get_record.return_value = SalesforceRecord(
            object_type="Account", id="001xx",
            fields={"Name": "Acme Corp", "Website": None, "Fax": ""},
        )

        await connector.call("salesforce_get_record", {"object_type": "Account", "record_id": "001xx"})

        kwargs = gated_call_spy[0]
        assert kwargs["new_info"]["Field values"] == "Name"
        assert kwargs["preview_tables"] == [{"headers": ["Field", "Value"], "rows": [["Name", "Acme Corp"]]}]

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_record.side_effect = SalesforceClientError("insufficient access")

        with pytest.raises(RuntimeError, match="insufficient access"):
            await connector.call("salesforce_get_record", {"object_type": "Account", "record_id": "x"})

    async def test_details_are_flat_sorted_field_lines_not_json(self, gated_call_spy):
        connector, client = make_connector()
        client.get_record.return_value = SalesforceRecord(
            object_type="Account", id="001xx0000012345",
            fields={"Name": "Acme Corp", "Industry": "Technology", "Website": None, "Fax": ""},
        )

        await connector.call(
            "salesforce_get_record", {"object_type": "Account", "record_id": "001xx0000012345"}
        )

        details = gated_call_spy[0]["details_text"]
        assert details == "Fields:\nIndustry: Technology\nName: Acme Corp"
        # No unset field shows up, and this is plain text, not a JSON blob.
        assert "Website" not in details
        assert "Fax" not in details
        assert "{" not in details

    async def test_details_when_no_fields_populated(self, gated_call_spy):
        connector, client = make_connector()
        client.get_record.return_value = SalesforceRecord(object_type="Task", id="00T1", fields={})

        await connector.call("salesforce_get_record", {"object_type": "Task", "record_id": "00T1"})

        assert "(no populated fields)" in gated_call_spy[0]["details_text"]


class TestRunReport:
    async def test_preview_shows_actual_report_name_from_report_metadata(self, gated_call_spy):
        # Regression test for bug #2: Salesforce's real report-run response
        # nests the name under reportMetadata.name, not top-level "name".
        connector, client = make_connector()
        client.run_report.return_value = {
            "reportMetadata": {"id": "00O1", "name": "Q3 Pipeline Report"},
            "factMap": {},
        }

        await connector.call("salesforce_run_report", {"report_id": "00O1"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Report"] == "Q3 Pipeline Report"
        assert kwargs["summary"] == "Run report: Q3 Pipeline Report"
        # Same "keep the table, not both" reasoning as get_record/search --
        # _report_tables() renders the exact factMap data details_text does,
        # just structured as tables (falls back to details_text when
        # preview_tables comes back empty, e.g. this fixture's factMap={}).
        assert kwargs["table_only"] is True

    async def test_falls_back_to_top_level_name_if_present(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {"name": "Flat Name Report"}

        await connector.call("salesforce_run_report", {"report_id": "00O2"})

        assert gated_call_spy[0]["preview"]["Report"] == "Flat Name Report"

    async def test_falls_back_to_report_id_when_name_unavailable(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {"factMap": {}}

        await connector.call("salesforce_run_report", {"report_id": "00O3"})

        assert gated_call_spy[0]["preview"]["Report"] == "00O3"

    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {"reportMetadata": {"name": "Report X"}}

        result = await connector.call("salesforce_run_report", {"report_id": "00O1"})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "review"
        assert kwargs["preview"]["Report ID"] == "00O1"
        assert kwargs["args"] == {"report_id": "00O1", "columns": "", "filters": "", "summary_only": False}
        assert kwargs["sender"] == "Salesforce"
        assert kwargs["new_info"] == {"Report data": "All report rows/aggregates"}
        assert "Columns" not in kwargs["preview"] and "Filters" not in kwargs["preview"]
        assert "Mode" not in kwargs["preview"]
        assert result == client.run_report.return_value
        client.run_report.assert_called_once_with("00O1", None, None, False)

    async def test_columns_and_filters_passed_to_client_and_shown_in_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {"reportMetadata": {"name": "R"}}
        ids = [f"006{i:015d}" for i in range(20)]
        filters = json.dumps([
            {"column": "Opportunity.Opp_Id__c", "operator": "equals", "value": ids},
            {"column": "STAGE", "operator": "notEqual", "value": "Closed Lost"},
        ])

        await connector.call("salesforce_run_report", {
            "report_id": "00O1", "columns": " OPPORTUNITY.NAME , AMOUNT,", "filters": filters,
        })

        client.run_report.assert_called_once_with(
            "00O1", ["OPPORTUNITY.NAME", "AMOUNT"],
            [
                ReportFilter("Opportunity.Opp_Id__c", "equals", ids),
                ReportFilter("STAGE", "notEqual", ["Closed Lost"]),
            ],
            False,
        )
        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Columns"] == "OPPORTUNITY.NAME, AMOUNT"
        assert kwargs["preview"]["Filters"] == (
            "Opportunity.Opp_Id__c equals 20 value(s); STAGE notEqual Closed Lost"
        )
        assert kwargs["new_info"]["Report data"] == "Report rows (selected columns) and totals"
        assert kwargs["args"]["filters"] == filters

    async def test_filters_only_new_info_and_long_single_value(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {"reportMetadata": {"name": "R"}}
        filters = json.dumps([{"column": "NAME", "operator": "contains", "value": "x" * 41}])

        await connector.call("salesforce_run_report", {"report_id": "00O1", "filters": filters})

        kwargs = gated_call_spy[0]
        assert kwargs["new_info"]["Report data"] == "Report rows matching the filters and totals"
        assert kwargs["preview"]["Filters"] == "NAME contains 1 value(s)"

    async def test_summary_only_preview_and_new_info(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {"reportMetadata": {"name": "R"}}

        await connector.call("salesforce_run_report", {"report_id": "00O1", "summary_only": True})

        client.run_report.assert_called_once_with("00O1", None, None, True)
        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Mode"] == "Totals only (no rows)"
        assert kwargs["new_info"]["Report data"] == "Report groupings and totals (no rows)"

    async def test_invalid_filters_json_rejected_before_fetch(self, gated_call_spy):
        connector, client = make_connector()

        with pytest.raises(ValueError, match="filters: invalid JSON"):
            await connector.call("salesforce_run_report", {"report_id": "00O1", "filters": "[{"})

        client.run_report.assert_not_called()
        assert gated_call_spy == []

    @pytest.mark.parametrize("bad", [
        '{"column": "A"}',
        "[1]",
        '[{"column": "", "operator": "equals", "value": "x"}]',
        '[{"column": "A", "operator": 3, "value": "x"}]',
        '[{"column": "A", "operator": "equals"}]',
        '[{"column": "A", "operator": "equals", "value": []}]',
        '[{"column": "A", "operator": "equals", "value": ["x", 2]}]',
        '[{"column": "A", "operator": "equals", "value": 5}]',
    ])
    async def test_filters_wrong_shape_rejected_before_fetch(self, gated_call_spy, bad):
        connector, client = make_connector()

        with pytest.raises(ValueError, match="filters: must be a JSON array"):
            await connector.call("salesforce_run_report", {"report_id": "00O1", "filters": bad})

        client.run_report.assert_not_called()
        assert gated_call_spy == []

    async def test_whitespace_filters_means_none(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {}

        await connector.call("salesforce_run_report", {"report_id": "00O1", "filters": "  "})

        client.run_report.assert_called_once_with("00O1", None, None, False)

    async def test_columns_with_summary_only_rejected_before_fetch(self, gated_call_spy):
        connector, client = make_connector()

        with pytest.raises(ValueError, match="columns has no effect with summary_only"):
            await connector.call(
                "salesforce_run_report", {"report_id": "00O1", "columns": "A", "summary_only": True},
            )

        client.run_report.assert_not_called()
        assert gated_call_spy == []

    async def test_all_data_false_is_surfaced(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {
            "reportMetadata": {"name": "R"}, "allData": False, "factMap": {},
        }

        result = await connector.call("salesforce_run_report", {"report_id": "00O1"})

        kwargs = gated_call_spy[0]
        assert kwargs["details_text"].startswith(
            "Salesforce returned only the first 2,000 detail rows; the report has more. "
            "Narrow it with filters.\n\n"
        )
        assert kwargs["new_info"]["Rows"] == "Cut off at Salesforce's 2,000-row limit"
        assert result["allData"] is False
        assert set(result) == {"reportMetadata", "allData", "factMap"}

    async def test_all_data_true_adds_no_truncation_note(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {"reportMetadata": {"name": "R"}, "allData": True}

        await connector.call("salesforce_run_report", {"report_id": "00O1"})

        assert "Rows" not in gated_call_spy[0]["new_info"]

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.run_report.side_effect = SalesforceClientError("report locked")

        with pytest.raises(RuntimeError, match="report locked"):
            await connector.call("salesforce_run_report", {"report_id": "00O1"})

    async def test_empty_fact_map_shows_no_data_returned_not_json(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {"reportMetadata": {"name": "Empty Report"}, "factMap": {}}

        await connector.call("salesforce_run_report", {"report_id": "00O1"})

        assert gated_call_spy[0]["details_text"] == "No data returned."

    async def test_tabular_report_renders_as_plain_text_table(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {
            "reportMetadata": {
                "name": "Open Opportunities",
                "detailColumns": ["OPPORTUNITY.NAME", "OPPORTUNITY.AMOUNT"],
            },
            "reportExtendedMetadata": {
                "detailColumnInfo": {
                    "OPPORTUNITY.NAME": {"label": "Opportunity Name"},
                    "OPPORTUNITY.AMOUNT": {"label": "Amount"},
                },
            },
            "factMap": {
                "T!T": {
                    "rows": [
                        {"dataCells": [{"label": "Acme Deal"}, {"label": "$10,000"}]},
                        {"dataCells": [{"label": "Globex Deal"}, {"label": "$5,000"}]},
                    ],
                    "aggregates": [{"label": "$15,000"}],
                },
            },
        }

        await connector.call("salesforce_run_report", {"report_id": "00O1"})

        details = gated_call_spy[0]["details_text"]
        assert details == (
            "Opportunity Name | Amount\n"
            "Acme Deal | $10,000\n"
            "Globex Deal | $5,000\n"
            "Total: $15,000"
        )
        assert "{" not in details

        tables = gated_call_spy[0]["preview_tables"]
        assert len(tables) == 1
        assert tables[0]["headers"] == ["Opportunity Name", "Amount"]
        assert tables[0]["rows"] == [["Acme Deal", "$10,000"], ["Globex Deal", "$5,000"]]
        assert tables[0]["footer"] == "Total: $15,000"

    async def test_empty_fact_map_produces_no_tables(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {"reportMetadata": {"name": "Empty Report"}, "factMap": {}}

        await connector.call("salesforce_run_report", {"report_id": "00O1"})

        assert gated_call_spy[0]["preview_tables"] == []

    async def test_grouped_report_renders_group_labels_from_groupings_down(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {
            "reportMetadata": {"name": "Pipeline by Stage"},
            "groupingsDown": {
                "groupings": [
                    {"key": "0", "label": "Prospecting", "groupings": []},
                    {"key": "1", "label": "Negotiation", "groupings": []},
                ],
            },
            "factMap": {
                "0!T": {"rows": [{"dataCells": [{"label": "Acme Deal"}]}], "aggregates": []},
                "1!T": {"rows": [{"dataCells": [{"label": "Globex Deal"}]}], "aggregates": []},
            },
        }

        await connector.call("salesforce_run_report", {"report_id": "00O2"})

        details = gated_call_spy[0]["details_text"]
        assert "Prospecting\nAcme Deal" in details
        assert "Negotiation\nGlobex Deal" in details

        tables = gated_call_spy[0]["preview_tables"]
        assert [t["caption"] for t in tables] == ["Prospecting", "Negotiation"]
        assert tables[0]["rows"] == [["Acme Deal"]]
        assert tables[1]["rows"] == [["Globex Deal"]]

    async def test_matrix_report_combines_down_and_across_grouping_labels(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {
            "reportMetadata": {"name": "Pipeline by Stage and Region"},
            "groupingsDown": {"groupings": [{"key": "0", "label": "Prospecting", "groupings": []}]},
            "groupingsAcross": {"groupings": [{"key": "0", "label": "West", "groupings": []}]},
            "factMap": {
                "0!0": {"rows": [{"dataCells": [{"label": "Acme Deal"}]}], "aggregates": []},
            },
        }

        await connector.call("salesforce_run_report", {"report_id": "00O4"})

        assert "Prospecting / West\nAcme Deal" in gated_call_spy[0]["details_text"]

    async def test_grouping_key_with_no_matching_label_falls_back_to_raw_key(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {
            "reportMetadata": {"name": "Pipeline by Stage"},
            "groupingsDown": {"groupings": [{"key": "0", "label": "Prospecting", "groupings": []}]},
            "factMap": {
                # "5" has no matching entry in groupingsDown -- label resolution fails.
                "5!T": {"rows": [{"dataCells": [{"label": "Mystery Deal"}]}], "aggregates": []},
            },
        }

        await connector.call("salesforce_run_report", {"report_id": "00O5"})

        assert "5!T\nMystery Deal" in gated_call_spy[0]["details_text"]

    async def test_tabular_report_rows_truncated_past_50(self, gated_call_spy):
        connector, client = make_connector()
        rows = [{"dataCells": [{"label": str(i)}]} for i in range(51)]
        client.run_report.return_value = {
            "reportMetadata": {"name": "Big Report"},
            "factMap": {"T!T": {"rows": rows, "aggregates": []}},
        }

        await connector.call("salesforce_run_report", {"report_id": "00O6"})

        details = gated_call_spy[0]["details_text"]
        assert "49\n… and 1 more row(s)" in details  # last of the 50 shown rows, then the truncation note
        assert "50" not in details  # the 51st row (index 50) never got rendered

    async def test_unexpected_fact_map_shape_falls_back_to_plain_language_summary(self, gated_call_spy):
        # A factMap entry that doesn't match the documented shape (e.g. a
        # future/unusual report type) must degrade to a short, non-technical
        # message -- never a raw JSON/repr dump.
        connector, client = make_connector()
        client.run_report.return_value = {
            "reportMetadata": {"name": "Weird Report"},
            "factMap": {"0!0": "not-a-dict", "1!0": "also-not-a-dict"},
        }

        await connector.call("salesforce_run_report", {"report_id": "00O3"})

        details = gated_call_spy[0]["details_text"]
        assert details == (
            "Report ran successfully — 2 data group(s). "
            "Structure too complex to preview here; open in Salesforce to view."
        )
        assert gated_call_spy[0]["preview_tables"] == []


class TestRunReportPaged:
    @staticmethod
    def _paged_connector(monkeypatch, rows=4500):
        fake = FakeAnalytics(tabular_report(), COLUMNS, make_rows(rows))
        client = SalesforceClient(config={"access_token": "tok", "instance_url": "https://my.salesforce.com"})
        monkeypatch.setattr(client, "_get_sf", lambda: SimpleNamespace(restful=fake.restful))
        connector = SalesforceConnector(client)
        connector.my_email = "me@example.com"
        return connector

    @staticmethod
    def _keys(result):
        rows = result["factMap"]["T!T"]["rows"]
        index = result["reportMetadata"]["detailColumns"].index(KEY)
        return [row["dataCells"][index]["value"] for row in rows]

    async def _read_all(self, connector):
        results, cursor = [], ""
        while True:
            result = await connector.call(
                "salesforce_run_report", {"report_id": REPORT_ID, "page_by": KEY, "cursor": cursor},
            )
            results.append(result)
            cursor = result["next_cursor"]
            if cursor is None:
                return results

    async def test_three_pages_return_every_row_once_in_key_order(self, monkeypatch, gated_call_spy):
        connector = self._paged_connector(monkeypatch)

        results = await self._read_all(connector)

        assert [self._keys(r) for r in results] == [
            [r[KEY] for r in make_rows(4500)[:2000]],
            [r[KEY] for r in make_rows(4500)[2000:4000]],
            [r[KEY] for r in make_rows(4500)[4000:]],
        ]
        assert [r["page"] for r in results] == [
            {"number": 1, "first_row": 1, "last_row": 2000, "more": True},
            {"number": 2, "first_row": 2001, "last_row": 4000, "more": True},
            {"number": 3, "first_row": 4001, "last_row": 4500, "more": False},
        ]
        assert results[0]["next_cursor"] and results[1]["next_cursor"]
        assert results[2]["next_cursor"] is None

    async def test_card_rows_text_and_no_cut_off_text(self, monkeypatch, gated_call_spy):
        connector = self._paged_connector(monkeypatch)

        await self._read_all(connector)

        assert [c["new_info"]["Rows"] for c in gated_call_spy] == [
            "Page 1: rows 1\u20132,000, more pages follow",
            "Page 2: rows 2,001\u20134,000, more pages follow",
            "Page 3: rows 4,001\u20134,500, last page",
        ]
        for call in gated_call_spy:
            assert call["preview"]["Paged by"] == KEY
            assert "Cut off" not in call["details_text"]
            assert "Cut off" not in json.dumps(call["new_info"])
            assert call["summary"] == "Run report: " + call["preview"]["Report"]
        assert gated_call_spy[0]["raw_data"]["allData"] is False
        assert "page" not in gated_call_spy[0]["raw_data"]

    async def test_preview_keeps_paged_by_after_filters(self, monkeypatch, gated_call_spy):
        connector = self._paged_connector(monkeypatch)
        filters = json.dumps([{"column": "TYPE", "operator": "equals", "value": "Customer"}])

        await connector.call(
            "salesforce_run_report", {"report_id": REPORT_ID, "page_by": f" {KEY} ", "filters": filters},
        )

        keys = list(gated_call_spy[0]["preview"])
        assert keys.index("Paged by") == keys.index("Filters") + 1

    async def test_no_rows_card_text(self, monkeypatch, gated_call_spy):
        connector = self._paged_connector(monkeypatch, rows=0)

        result = await connector.call("salesforce_run_report", {"report_id": REPORT_ID, "page_by": KEY})

        assert gated_call_spy[0]["new_info"]["Rows"] == "Page 1: no rows, last page"
        assert result["page"] == {"number": 1, "first_row": 0, "last_row": 0, "more": False}
        assert result["next_cursor"] is None

    async def test_args_include_page_by_and_cursor_on_paged_calls_only(self, monkeypatch, gated_call_spy):
        connector = self._paged_connector(monkeypatch)

        first = await connector.call("salesforce_run_report", {"report_id": REPORT_ID, "page_by": KEY})
        await connector.call(
            "salesforce_run_report",
            {"report_id": REPORT_ID, "page_by": KEY, "cursor": first["next_cursor"]},
        )
        plain, plain_client = make_connector()
        plain_client.run_report.return_value = {"reportMetadata": {"name": "R"}}
        await plain.call("salesforce_run_report", {"report_id": REPORT_ID})

        assert gated_call_spy[0]["args"] == {
            "report_id": REPORT_ID, "columns": "", "filters": "", "summary_only": False,
            "page_by": KEY, "cursor": "",
        }
        assert gated_call_spy[1]["args"]["cursor"] == first["next_cursor"]
        assert set(gated_call_spy[2]["args"]) == {"report_id", "columns", "filters", "summary_only"}

    async def test_unpaged_run_of_a_big_report_still_says_cut_off(self, monkeypatch, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {"reportMetadata": {"name": "R"}, "allData": False}

        await connector.call("salesforce_run_report", {"report_id": "00O1"})

        assert gated_call_spy[0]["new_info"]["Rows"] == "Cut off at Salesforce's 2,000-row limit"

    async def test_page_by_is_passed_to_the_client_with_cursor_state(self, gated_call_spy):
        connector, client = make_connector()
        bound = {"report_id": "00O1", "page_by": "A.B", "columns": ["X"], "filters": []}
        cursor = cursors.encode(
            "salesforce_run_report", bound, {"n": 2, "a": 4000, "l": "K-4000", "r": 500},
        )
        client.run_report_page.return_value = ReportPage(
            result={"reportMetadata": {"name": "R"}}, keys=["K-4001"], all_data=True, row_count=500,
        )

        await connector.call(
            "salesforce_run_report",
            {"report_id": "00O1", "columns": "X", "page_by": "A.B", "cursor": cursor},
        )

        client.run_report_page.assert_called_once_with("00O1", "A.B", ["X"], None, "K-4000", 2, 500)
        assert gated_call_spy[0]["new_info"]["Rows"] == "Page 3: rows 4,001\u20134,001, last page"

    @pytest.mark.parametrize("args, message", [
        ({"cursor": "abc"}, "cursor needs page_by"),
        ({"page_by": "A.B", "summary_only": True}, "page_by has no effect with summary_only"),
        ({"page_by": "A.B", "cursor": "not-a-cursor"}, "salesforce_run_report: "),
    ])
    async def test_bad_calls_rejected_before_fetch_or_gate(self, gated_call_spy, args, message):
        connector, client = make_connector()

        with pytest.raises(ValueError, match=message):
            await connector.call("salesforce_run_report", {"report_id": "00O1", **args})

        client.run_report.assert_not_called()
        client.run_report_page.assert_not_called()
        assert gated_call_spy == []

    @pytest.mark.parametrize("other", [
        {"filters": json.dumps([{"column": "A", "operator": "equals", "value": "x"}])},
        {"columns": "Y"},
        {"page_by": "A.C"},
        {"report_id": "00O2"},
    ])
    async def test_cursor_reused_with_other_arguments_rejected(self, gated_call_spy, other):
        connector, client = make_connector()
        bound = {"report_id": "00O1", "page_by": "A.B", "columns": ["X"], "filters": []}
        cursor = cursors.encode("salesforce_run_report", bound, {"n": 1, "a": 1, "l": "K-1", "r": 5})
        args = {"report_id": "00O1", "columns": "X", "page_by": "A.B", "cursor": cursor, **other}

        with pytest.raises(ValueError, match="salesforce_run_report: "):
            await connector.call("salesforce_run_report", args)

        client.run_report_page.assert_not_called()
        assert gated_call_spy == []

    @pytest.mark.parametrize("state", [
        {"n": 1, "a": 1, "l": "K-1"},
        {"n": 1, "a": 1, "l": "K-1", "r": 5, "x": 1},
        {"n": 0, "a": 1, "l": "K-1", "r": 5},
        {"n": True, "a": 1, "l": "K-1", "r": 5},
        {"n": 1, "a": -1, "l": "K-1", "r": 5},
        {"n": 1, "a": 1, "l": "", "r": 5},
        {"n": 1, "a": 1, "l": 5, "r": 5},
        {"n": 1, "a": 1, "l": "K-1", "r": "5"},
    ])
    async def test_invalid_cursor_state_rejected(self, gated_call_spy, state):
        connector, client = make_connector()
        bound = {"report_id": "00O1", "page_by": "A.B", "columns": [], "filters": []}
        cursor = cursors.encode("salesforce_run_report", bound, state)

        with pytest.raises(ValueError, match="cursor is not valid"):
            await connector.call(
                "salesforce_run_report", {"report_id": "00O1", "page_by": "A.B", "cursor": cursor},
            )

        client.run_report_page.assert_not_called()
        assert gated_call_spy == []

    async def test_paging_error_becomes_runtime_error_with_no_card(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report_page.side_effect = ReportPagingError("not_unique", "page_by 'A.B' is not unique")

        with pytest.raises(RuntimeError, match="page_by 'A.B' is not unique"):
            await connector.call("salesforce_run_report", {"report_id": "00O1", "page_by": "A.B"})

        assert gated_call_spy == []


class TestHelperEdges:
    def test_group_label_skips_unresolvable_keys_and_adds_across_labels(self):
        result = {
            "groupingsDown": {"groupings": [{"key": "0", "label": "Open"}]},
            "groupingsAcross": {"groupings": [{"key": "1", "label": "EMEA"}]},
        }
        label = salesforce_module._report_group_label
        assert label(result, "9!T") == "9!T"
        assert label(result, "0!T") == "Open"
        assert label(result, "0!1") == "Open / EMEA"
        assert label(result, "T!9") == "T!9"

    def test_client_property_returns_the_client(self):
        connector, client = make_connector()
        assert connector.client is client

    def test_audit_write_failure_is_logged_not_raised(self, monkeypatch, caplog):
        connector, _client = make_connector()

        def boom():
            raise OSError("disk full")

        monkeypatch.setattr(salesforce_module, "get_audit_logger", boom)
        connector._auto_audit("salesforce_list_reports", "List", "s", "Salesforce", 0.0)

        assert "Audit log write failed" in caplog.text


class TestSearch:
    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.search.return_value = [
            SalesforceRecord(object_type="Opportunity", id="006x", fields={"Id": "006x", "Name": "Big Deal"}),
        ]

        result = await connector.call("salesforce_search", {"search_term": "Big Deal"})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "review"
        assert kwargs["preview"] == {
            "Search term": "Big Deal", "Object types": "(default)",
        }
        assert kwargs["new_info"]["Results"] == "1"
        assert "Account ID" not in kwargs["preview"]
        assert kwargs["args"] == {"search_term": "Big Deal", "object_types": "", "account_id": ""}
        client.search.assert_called_once_with("Big Deal", "", "", 20)
        assert result == [{"object_type": "Opportunity", "id": "006x", "fields": {"Id": "006x", "Name": "Big Deal"}}]
        assert kwargs["preview_tables"] == [
            {"headers": ["Object type", "Name", "ID"], "rows": [["Opportunity", "Big Deal", "006x"]]},
        ]
        assert kwargs["table_only"] is True

    async def test_no_matches_produces_no_table(self, gated_call_spy):
        connector, client = make_connector()
        client.search.return_value = []

        await connector.call("salesforce_search", {"search_term": "nothing"})

        assert gated_call_spy[0]["preview_tables"] == []

    async def test_details_list_one_match_per_line(self, gated_call_spy):
        connector, client = make_connector()
        client.search.return_value = [
            SalesforceRecord(object_type="Opportunity", id="006x", fields={"Name": "Big Deal"}),
            SalesforceRecord(object_type="Contact", id="003y", fields={"Name": "Jane Doe"}),
        ]

        await connector.call("salesforce_search", {"search_term": "a"})

        details = gated_call_spy[0]["details_text"]
        assert "Opportunity — Big Deal (id=006x)" in details
        assert "Contact — Jane Doe (id=003y)" in details

    async def test_no_matches_shows_placeholder(self, gated_call_spy):
        connector, client = make_connector()
        client.search.return_value = []

        await connector.call("salesforce_search", {"search_term": "nothing"})

        assert gated_call_spy[0]["details_text"] == "(no matches)"

    async def test_missing_name_field_falls_back_to_placeholder(self, gated_call_spy):
        connector, client = make_connector()
        client.search.return_value = [SalesforceRecord(object_type="Task", id="00T1", fields={})]

        await connector.call("salesforce_search", {"search_term": "x"})

        assert "(no name)" in gated_call_spy[0]["details_text"]

    async def test_account_id_shown_in_preview_and_passed_to_client(self, gated_call_spy):
        connector, client = make_connector()
        client.search.return_value = []

        await connector.call(
            "salesforce_search",
            {"search_term": "Acme", "object_types": "Opportunity", "account_id": "001xx0000012345"},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Account ID"] == "001xx0000012345"
        client.search.assert_called_once_with("Acme", "Opportunity", "001xx0000012345", 20)

    async def test_account_id_without_object_types_rejected_before_gate(self, gated_call_spy):
        connector, client = make_connector()

        with pytest.raises(ValueError, match="account_id requires object_types"):
            await connector.call(
                "salesforce_search", {"search_term": "Acme", "account_id": "001xx0000012345"}
            )

        assert gated_call_spy == []
        client.search.assert_not_called()

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.search.side_effect = SalesforceClientError("MALFORMED_QUERY")

        with pytest.raises(RuntimeError, match="MALFORMED_QUERY"):
            await connector.call("salesforce_search", {"search_term": "x"})


class TestFieldCompleteness:
    """End to end: a fully-populated raw REST response -> the real
    SalesforceClient.get_record -> the real connector's popup preview --
    not a hand-built SalesforceRecord, unlike every other test in this
    file. Mirrors test_confluence_connector.py's TestFieldCompleteness;
    this module's own docstring already documents a real bug this exact
    shape of check would have caught (the record_dict.get("Name") vs.
    nested "fields" key mistake) before it shipped.
    """

    async def test_get_record_preview_has_no_placeholder_fields(self, gated_call_spy):
        sf = MagicMock()
        sf.Account.get.return_value = {
            "attributes": {"type": "Account", "url": "/x"},
            "Id": "001xx000003DGb2AAG",
            "Name": "PrivacyFence QA — Acme Test Co [QATEST]",
        }
        client = make_real_client(sf)
        connector = SalesforceConnector(client)
        connector.my_email = "me@example.com"

        await connector.call("salesforce_get_record", {"object_type": "Account", "record_id": "001xx000003DGb2AAG"})

        assert_no_placeholder_fields(gated_call_spy[0]["preview"])


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        # get_record's/search's results are asdict()'d unconditionally, so
        # they need real SalesforceRecord instances -- a bare MagicMock
        # isn't a dataclass instance.
        client.search.return_value = [SalesforceRecord(object_type="Account", id="001", fields={})]
        client.run_report.return_value = export_run(make_rows(2))
        client.describe_fields.return_value = ACCOUNT_FIELDS
        client.get_record.return_value = SalesforceRecord("Account", "001000000000001AAA", {"Name": "Old"})
        monkeypatch.setattr(
            salesforce_module.local_files, "deliver_export",
            lambda *a, **k: {"path": None, "name": "x.csv", "size_bytes": 0, "delivery": "local_disk"},
        )

        await assert_all_tools_leave_an_audit_trail(
            connector, salesforce_module, monkeypatch, tmp_path,
            arg_overrides={
                "salesforce_export_report": {
                    "report_id": "00O000000000001", "destination_dir": str(tmp_path),
                },
                "salesforce_create_record": {"fields": '{"Name": "A"}'},
                "salesforce_update_record": {
                    "record_id": "001000000000001AAA", "fields": '{"Name": "New"}',
                },
            },
        )


# ---------------------------------------------------------------------- #
# Single-record writes
# ---------------------------------------------------------------------- #

ACCOUNT_FIELDS = [
    SalesforceField("Id", "Record ID", "id", False, False, False, False),
    SalesforceField("Name", "Account Name", "string", True, True, False, False),
    SalesforceField("Phone", "Phone", "phone", True, True, True, False),
    SalesforceField("NumberOfEmployees", "Employees", "int", True, True, True, False),
]
RECORD_ID = "001000000000001AAA"


def write_connector():
    connector, client = make_connector()
    client.describe_fields.return_value = ACCOUNT_FIELDS
    client.get_record.return_value = SalesforceRecord(
        "Account", RECORD_ID, {"Name": "SECRET-CURRENT", "Phone": "555-0100", "NumberOfEmployees": 5},
    )
    return connector, client


class TestCreateRecord:
    async def test_gates_with_popup_and_returns_the_new_id(self, gated_call_spy):
        connector, client = write_connector()
        client.create_record.return_value = RECORD_ID

        result = await connector.call("salesforce_create_record", {
            "object_type": "Account", "fields": '{"name": "Acme", "NumberOfEmployees": 7}',
        })

        assert result == {"object_type": "Account", "id": RECORD_ID}
        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["tool"] == "salesforce_create_record"
        assert kwargs["sender"] == "Account"
        assert kwargs["filtered_data"] is None
        # Canonical field names are what reach the client, in input order.
        client.create_record.assert_called_once_with("Account", {"Name": "Acme", "NumberOfEmployees": 7})

    async def test_card_contents(self, gated_call_spy):
        connector, _client = write_connector()
        await connector.call("salesforce_create_record", {
            "object_type": "Account", "fields": '{"Name": "Acme"}',
        })
        kwargs = gated_call_spy[0]
        assert kwargs["summary"] == "Create Account record"
        assert kwargs["tool_name"] == "Create Salesforce Record"
        assert kwargs["preview"] == {"Object type": "Account", "Fields": "1"}
        assert kwargs["preview_tables"] == [{
            "headers": ["Field", "Label", "Value"],
            "rows": [["Name", "Account Name", "Acme"]],
        }]
        assert kwargs["details_text"] == "Name: Acme"
        assert kwargs["args"] == {"object_type": "Account"}
        assert kwargs["raw_data"] == {"object_type": "Account", "fields": ["Name"]}
        assert kwargs["table_only"] is True

    async def test_preview_args_and_raw_data_hold_no_value(self, gated_call_spy):
        connector, _client = write_connector()
        await connector.call("salesforce_create_record", {
            "object_type": "Account", "fields": '{"Name": "SECRET-NEW"}',
        })
        kwargs = gated_call_spy[0]
        assert "SECRET-NEW" not in json.dumps(kwargs["preview"])
        assert "SECRET-NEW" not in json.dumps(kwargs["args"])
        assert "SECRET-NEW" not in json.dumps(kwargs["raw_data"])

    async def test_malformed_json_costs_no_describe_and_no_card(self, gated_call_spy):
        connector, client = write_connector()
        with pytest.raises(ValueError, match="fields: invalid JSON"):
            await connector.call("salesforce_create_record", {"object_type": "Account", "fields": "{nope"})
        client.describe_fields.assert_not_called()
        assert gated_call_spy == []

    async def test_bad_object_type_is_a_value_error(self, gated_call_spy):
        connector, client = write_connector()
        with pytest.raises(ValueError, match="Invalid Salesforce object type name"):
            await connector.call("salesforce_create_record", {"object_type": "Account/x", "fields": '{"Name": "A"}'})
        client.describe_fields.assert_not_called()

    async def test_unknown_field_is_refused_before_the_card(self, gated_call_spy):
        connector, client = write_connector()
        with pytest.raises(ValueError, match="Account has no field 'Nope'"):
            await connector.call("salesforce_create_record", {"object_type": "Account", "fields": '{"Nope": 1}'})
        assert gated_call_spy == []
        client.create_record.assert_not_called()

    async def test_unknown_object_type_is_a_value_error_with_no_card(self, gated_call_spy):
        connector, client = write_connector()
        client.describe_fields.side_effect = SalesforceObjectNotFoundError(
            "Acount is not an object type your Salesforce user can see"
        )
        with pytest.raises(ValueError, match="Acount is not an object type"):
            await connector.call("salesforce_create_record", {"object_type": "Acount", "fields": '{"Name": "A"}'})
        assert gated_call_spy == []

    async def test_other_describe_failure_is_a_runtime_error(self, gated_call_spy):
        connector, client = write_connector()
        client.describe_fields.side_effect = SalesforceClientError("boom")
        with pytest.raises(RuntimeError, match="boom") as info:
            await connector.call("salesforce_create_record", {"object_type": "Account", "fields": '{"Name": "A"}'})
        assert type(info.value) is RuntimeError
        assert gated_call_spy == []

    async def test_refused_write_is_shown_to_the_model_verbatim(self, gated_call_spy):
        connector, client = write_connector()
        client.create_record.side_effect = SalesforceWriteError(
            [WriteFailure(0, "REQUIRED_FIELD_MISSING", ["Name"], "Required fields are missing")], 1,
        )
        with pytest.raises(SalesforceWriteRefusedError) as info:
            await connector.call("salesforce_create_record", {"object_type": "Account", "fields": '{"Phone": "1"}'})
        assert "REQUIRED_FIELD_MISSING (Name)" in str(info.value)
        assert safe_errors.public_message(info.value) == str(info.value)

    async def test_uncertain_write_is_shown_to_the_model_verbatim(self, gated_call_spy):
        connector, client = write_connector()
        client.create_record.side_effect = SalesforceWriteUncertainError(
            "The connection to Salesforce failed; the write may or may not have been saved."
        )
        with pytest.raises(SalesforceWriteUnconfirmedError) as info:
            await connector.call("salesforce_create_record", {"object_type": "Account", "fields": '{"Name": "A"}'})
        assert safe_errors.public_message(info.value) == str(info.value)

    async def test_any_other_client_error_is_a_plain_runtime_error(self, gated_call_spy):
        connector, client = write_connector()
        client.create_record.side_effect = SalesforceClientError("odd")
        with pytest.raises(RuntimeError) as info:
            await connector.call("salesforce_create_record", {"object_type": "Account", "fields": '{"Name": "A"}'})
        assert type(info.value) is RuntimeError
        assert safe_errors.public_message(info.value) != "odd"


class TestUpdateRecord:
    async def test_card_shows_current_and_new_without_leaking_the_current_name(self, gated_call_spy):
        connector, client = write_connector()

        result = await connector.call("salesforce_update_record", {
            "object_type": "Account", "record_id": RECORD_ID,
            "fields": '{"Phone": "555-0199", "numberofemployees": 5}',
        })

        assert result == {"object_type": "Account", "id": RECORD_ID, "fields": ["Phone", "NumberOfEmployees"]}
        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["sender"] == "Account"
        assert kwargs["tool_name"] == "Update Salesforce Record"
        assert kwargs["summary"] == "Update Account: SECRET-CURRENT"
        assert kwargs["preview"] == {"Object type": "Account", "Record ID": RECORD_ID, "Fields changed": "1"}
        assert "SECRET-CURRENT" not in json.dumps(kwargs["preview"])
        assert kwargs["preview_tables"] == [{
            "headers": ["Field", "Current", "New"],
            "rows": [["Phone", "555-0100", "555-0199"], ["NumberOfEmployees", "5", "5"]],
        }]
        assert kwargs["details_text"] == "Phone: 555-0100 → 555-0199\nNumberOfEmployees: 5 → 5"
        assert kwargs["args"] == {"object_type": "Account", "record_id": RECORD_ID}
        assert "555-0199" not in json.dumps(kwargs["args"])
        assert kwargs["raw_data"] == {"object_type": "Account", "fields": ["NumberOfEmployees", "Phone"]}
        # Only the changed field is written.
        client.update_record.assert_called_once_with("Account", RECORD_ID, {"Phone": "555-0199"})

    async def test_summary_falls_back_to_the_record_id(self, gated_call_spy):
        connector, client = write_connector()
        client.get_record.return_value = SalesforceRecord("Account", RECORD_ID, {"Phone": "1"})
        await connector.call("salesforce_update_record", {
            "object_type": "Account", "record_id": RECORD_ID, "fields": '{"Phone": "2"}',
        })
        assert gated_call_spy[0]["summary"] == f"Update Account: {RECORD_ID}"

    async def test_nothing_to_change_still_gates_and_writes_nothing(self, gated_call_spy):
        connector, client = write_connector()

        result = await connector.call("salesforce_update_record", {
            "object_type": "Account", "record_id": RECORD_ID, "fields": '{"Phone": "555-0100"}',
        })

        assert gated_call_spy[0]["preview"]["Fields changed"] == "0"
        client.update_record.assert_not_called()
        assert result == {"object_type": "Account", "id": RECORD_ID, "fields": ["Phone"]}

    async def test_null_clears_a_nillable_field(self, gated_call_spy):
        connector, client = write_connector()
        await connector.call("salesforce_update_record", {
            "object_type": "Account", "record_id": RECORD_ID, "fields": '{"Phone": null}',
        })
        assert gated_call_spy[0]["preview_tables"][0]["rows"] == [["Phone", "555-0100", "(empty)"]]
        client.update_record.assert_called_once_with("Account", RECORD_ID, {"Phone": None})

    async def test_bad_record_id_is_a_value_error_before_any_call(self, gated_call_spy):
        connector, client = write_connector()
        with pytest.raises(ValueError, match="record_id must be a 15- or 18-character"):
            await connector.call("salesforce_update_record", {
                "object_type": "Account", "record_id": "nope", "fields": '{"Phone": "1"}',
            })
        client.describe_fields.assert_not_called()
        assert gated_call_spy == []

    async def test_malformed_fields_and_unknown_fields_cost_no_card(self, gated_call_spy):
        connector, client = write_connector()
        with pytest.raises(ValueError, match="fields: invalid JSON"):
            await connector.call("salesforce_update_record", {
                "object_type": "Account", "record_id": RECORD_ID, "fields": "[",
            })
        client.describe_fields.assert_not_called()
        with pytest.raises(ValueError, match="has no field 'Nope'"):
            await connector.call("salesforce_update_record", {
                "object_type": "Account", "record_id": RECORD_ID, "fields": '{"Nope": 1}',
            })
        assert gated_call_spy == []
        client.get_record.assert_not_called()

    async def test_unknown_object_type_is_a_value_error(self, gated_call_spy):
        connector, client = write_connector()
        client.describe_fields.side_effect = SalesforceObjectNotFoundError("Acount is not an object type")
        with pytest.raises(ValueError, match="Acount"):
            await connector.call("salesforce_update_record", {
                "object_type": "Acount", "record_id": RECORD_ID, "fields": '{"Phone": "1"}',
            })
        assert gated_call_spy == []

    async def test_refused_and_uncertain_writes_keep_their_text(self, gated_call_spy):
        connector, client = write_connector()
        refused = SalesforceWriteError([WriteFailure(0, "FIELD_CUSTOM_VALIDATION_EXCEPTION", ["Phone"], "bad")], 1)
        client.update_record.side_effect = refused
        with pytest.raises(SalesforceWriteRefusedError) as info:
            await connector.call("salesforce_update_record", {
                "object_type": "Account", "record_id": RECORD_ID, "fields": '{"Phone": "1"}',
            })
        assert str(info.value) == str(refused)
        assert safe_errors.public_message(info.value) == str(info.value)

        client.update_record.side_effect = SalesforceWriteUncertainError("Salesforce returned HTTP 503; may or may not")
        with pytest.raises(SalesforceWriteUnconfirmedError) as info2:
            await connector.call("salesforce_update_record", {
                "object_type": "Account", "record_id": RECORD_ID, "fields": '{"Phone": "1"}',
            })
        assert safe_errors.public_message(info2.value) == str(info2.value)

        client.update_record.side_effect = SalesforceClientError("odd")
        with pytest.raises(RuntimeError) as info3:
            await connector.call("salesforce_update_record", {
                "object_type": "Account", "record_id": RECORD_ID, "fields": '{"Phone": "1"}',
            })
        assert type(info3.value) is RuntimeError

    async def test_read_failure_is_a_runtime_error(self, gated_call_spy):
        connector, client = write_connector()
        client.get_record.side_effect = SalesforceClientError("not found")
        with pytest.raises(RuntimeError, match="not found"):
            await connector.call("salesforce_update_record", {
                "object_type": "Account", "record_id": RECORD_ID, "fields": '{"Phone": "1"}',
            })
        assert gated_call_spy == []


# ---------------------------------------------------------------------- #
# Export to a file
# ---------------------------------------------------------------------- #

EXPORT_ARGS = {"report_id": REPORT_ID, "destination_dir": "~/out"}


@pytest.fixture
def delivered(monkeypatch):
    calls = []

    def fake_deliver(
        dest_dir, name, data, mime_type, *, download_mode, download_config=None,
        download_base_url="",
    ):
        calls.append({
            "dest_dir": dest_dir, "name": name, "data": data, "mime_type": mime_type,
            "download_mode": download_mode, "download_config": download_config,
            "download_base_url": download_base_url,
        })
        return {"path": f"{dest_dir}/{name}", "name": name, "size_bytes": len(data),
                "delivery": "local_disk"}

    monkeypatch.setattr(salesforce_module.local_files, "deliver_export", fake_deliver)
    return calls


def export_connector(result=None):
    connector, client = make_connector()
    client.run_report.return_value = export_run(make_rows(3)) if result is None else result
    return connector, client


def amount_run(**metadata):
    result = export_run(make_rows(1), **metadata)
    cells = result["factMap"]["T!T"]["rows"][0]["dataCells"]
    cells[1] = {"value": {"amount": 1200.0, "currency": "USD"}, "label": "$1,200.00"}
    cells[2] = {"value": None, "label": "-"}
    cells[3] = {"value": ["x"], "label": "X list"}
    return result


def both_run():
    result = export_run(make_rows(1))
    cells = result["factMap"]["T!T"]["rows"][0]["dataCells"]
    cells[1] = {"value": {"amount": 10.5, "currency": "EUR"}, "label": "EUR 10.50"}
    cells[2] = {"value": "001A", "label": "Acme"}
    result["reportExtendedMetadata"] = {"detailColumnInfo": {
        COLUMNS[1]: {"dataType": "currency"}, COLUMNS[2]: {"dataType": "string"},
    }}
    return result


class TestExportReport:
    async def test_formula_leading_cells_are_written_as_text(self, gated_call_spy, delivered):
        result = export_run(make_rows(1))
        result["factMap"]["T!T"]["rows"][0]["dataCells"][1] = {
            "value": '=HYPERLINK("x")', "label": '=HYPERLINK("x")',
        }
        connector, _client = export_connector(result)

        await connector.call("salesforce_export_report", EXPORT_ARGS)

        assert "\"'=HYPERLINK(\"\"x\"\")\"" in delivered[0]["data"].decode("utf-8")

    async def test_unpaged_tabular_report_writes_all_rows(self, gated_call_spy, delivered):
        connector, client = export_connector()

        await connector.call("salesforce_export_report", EXPORT_ARGS)

        client.run_report.assert_called_once_with(REPORT_ID, None, None, False)
        assert delivered[0]["name"] == f"{REPORT_ID}.csv"
        assert delivered[0]["dest_dir"] == "~/out"
        assert delivered[0]["mime_type"] == "text/csv"
        lines = delivered[0]["data"].decode("utf-8").splitlines()
        assert lines[0] == ",".join(COLUMNS)
        assert lines[1] == "PFQA-00001,Account 1,Chicago,Customer"
        assert len(lines) == 4

    async def test_paged_export_runs_until_all_data(self, gated_call_spy, delivered):
        connector, client = export_connector()
        client.run_report_page.side_effect = [
            ReportPage(export_run(make_rows(2)), ["PFQA-00001", "PFQA-00002"], False, 5),
            ReportPage(export_run(make_rows(2, start=3)), ["PFQA-00003", "PFQA-00004"], False, 3),
            ReportPage(export_run(make_rows(1, start=5)), ["PFQA-00005"], True, 1),
        ]

        result = await connector.call(
            "salesforce_export_report", {**EXPORT_ARGS, "page_by": f" {KEY} ", "columns": "A, B"},
        )

        assert [c.args for c in client.run_report_page.call_args_list] == [
            (REPORT_ID, KEY, ["A", "B"], None, None, 0, None),
            (REPORT_ID, KEY, ["A", "B"], None, "PFQA-00002", 1, 3),
            (REPORT_ID, KEY, ["A", "B"], None, "PFQA-00004", 2, 1),
        ]
        client.run_report.assert_not_called()
        assert result["rows"] == 5
        assert gated_call_spy[0]["preview"]["Report runs"] == "3"
        assert gated_call_spy[0]["preview"]["Paged by"] == KEY
        assert len(delivered[0]["data"].decode("utf-8").splitlines()) == 6

    async def test_grouped_report_without_page_by_is_refused(self, gated_call_spy, delivered):
        result = export_run(make_rows(2))
        result["factMap"]["0!T"] = {"rows": [], "aggregates": []}
        connector, _client = export_connector(result)

        with pytest.raises(ValueError, match="this report is grouped; pass page_by"):
            await connector.call("salesforce_export_report", EXPORT_ARGS)

        assert gated_call_spy == []
        assert delivered == []

    async def test_cut_off_report_without_page_by_is_refused(self, gated_call_spy, delivered):
        connector, _client = export_connector(export_run(make_rows(5), row_limit=2))

        with pytest.raises(ValueError, match="more than 2,000 rows; pass page_by"):
            await connector.call("salesforce_export_report", EXPORT_ARGS)

        assert gated_call_spy == []
        assert delivered == []

    async def test_value_format_uses_amount_and_raw_values(self, gated_call_spy, delivered):
        connector, _client = export_connector(amount_run())

        await connector.call(
            "salesforce_export_report", {**EXPORT_ARGS, "file_name": "r.json"},
        )

        assert delivered[0]["mime_type"] == "application/json"
        row = json.loads(delivered[0]["data"])[0]
        assert row[KEY] == "PFQA-00001"
        assert row[COLUMNS[1]] == 1200.0
        assert row[COLUMNS[2]] is None
        assert row[COLUMNS[3]] == "X list"

    async def test_label_format_uses_labels(self, gated_call_spy, delivered):
        connector, _client = export_connector(amount_run())

        await connector.call(
            "salesforce_export_report",
            {**EXPORT_ARGS, "cell_format": "label", "file_name": "r.json"},
        )

        row = json.loads(delivered[0]["data"])[0]
        assert row[COLUMNS[1]] == "$1,200.00"
        assert row[COLUMNS[2]] == "-"
        assert gated_call_spy[0]["args"]["cell_format"] == "label"

    async def test_unknown_cell_format_names_all_three(self, gated_call_spy, delivered):
        connector, client = export_connector()

        with pytest.raises(ValueError, match="cell_format must be value, label or both"):
            await connector.call("salesforce_export_report", {**EXPORT_ARGS, "cell_format": "raw"})

        client.run_report.assert_not_called()
        assert gated_call_spy == []

    async def test_both_writes_value_label_and_currency_columns(self, gated_call_spy, delivered):
        result = both_run()
        connector, _client = export_connector(result)

        await connector.call(
            "salesforce_export_report",
            {**EXPORT_ARGS, "cell_format": "both", "file_name": "r.json"},
        )

        rows = json.loads(delivered[0]["data"])
        assert list(rows[0]) == [
            KEY, f"{KEY}.label", COLUMNS[1], f"{COLUMNS[1]}.label", f"{COLUMNS[1]}.currency",
            COLUMNS[2], f"{COLUMNS[2]}.label", COLUMNS[3], f"{COLUMNS[3]}.label",
        ]
        assert rows[0][COLUMNS[1]] == 10.5
        assert rows[0][f"{COLUMNS[1]}.label"] == "EUR 10.50"
        assert rows[0][f"{COLUMNS[1]}.currency"] == "EUR"
        assert rows[0][COLUMNS[2]] == "001A"
        assert rows[0][f"{COLUMNS[2]}.label"] == "Acme"
        assert gated_call_spy[0]["args"]["cell_format"] == "both"

    async def test_both_adds_currency_only_for_money_columns(self, gated_call_spy, delivered):
        connector, _client = export_connector(both_run())

        await connector.call(
            "salesforce_export_report", {**EXPORT_ARGS, "cell_format": "both"},
        )

        header = delivered[0]["data"].decode("utf-8").splitlines()[0].split(",")
        assert [h for h in header if h.endswith(".currency")] == [f"{COLUMNS[1]}.currency"]

    async def test_both_columns_do_not_depend_on_the_data(self, gated_call_spy, delivered):
        result = both_run()
        for row in result["factMap"]["T!T"]["rows"]:
            row["dataCells"][1] = {"value": None, "label": "-"}
        connector, _client = export_connector(result)

        await connector.call(
            "salesforce_export_report", {**EXPORT_ARGS, "cell_format": "both"},
        )

        lines = delivered[0]["data"].decode("utf-8").splitlines()
        assert f"{COLUMNS[1]}.currency" in lines[0].split(",")
        assert lines[1].split(",")[2:5] == ["", "'-", ""]

    async def test_value_and_label_formats_are_unchanged(self, gated_call_spy, delivered):
        connector, _client = export_connector(amount_run())

        for cell_format in ("value", "label"):
            await connector.call(
                "salesforce_export_report",
                {**EXPORT_ARGS, "cell_format": cell_format, "file_name": "r.json"},
            )

        value_row, label_row = (json.loads(call["data"])[0] for call in delivered)
        assert list(value_row) == COLUMNS
        assert value_row[COLUMNS[1]] == 1200.0
        assert value_row[COLUMNS[2]] is None
        assert value_row[COLUMNS[3]] == "X list"
        assert list(label_row) == COLUMNS
        assert label_row[COLUMNS[1]] == "$1,200.00"
        assert label_row[COLUMNS[2]] == "-"

    async def test_filters_are_passed_to_each_run(self, gated_call_spy, delivered):
        connector, client = export_connector()
        filters = '[{"column": "LAST_UPDATE", "operator": "greaterOrEqual", "value": "2026-10-04"}]'
        expected = [ReportFilter(column="LAST_UPDATE", operator="greaterOrEqual", values=["2026-10-04"])]
        client.run_report_page.side_effect = [
            ReportPage(export_run(make_rows(2)), ["PFQA-00001", "PFQA-00002"], False, 3),
            ReportPage(export_run(make_rows(1, start=3)), ["PFQA-00003"], True, 1),
        ]

        await connector.call("salesforce_export_report", {**EXPORT_ARGS, "filters": filters})
        await connector.call(
            "salesforce_export_report", {**EXPORT_ARGS, "filters": filters, "page_by": KEY},
        )

        client.run_report.assert_called_once_with(REPORT_ID, None, expected, False)
        assert [c.args[3] for c in client.run_report_page.call_args_list] == [expected, expected]
        assert gated_call_spy[0]["preview"]["Filters"] == "LAST_UPDATE greaterOrEqual 2026-10-04"

    async def test_one_card_with_report_runs_line(self, gated_call_spy, delivered):
        connector, _client = export_connector()

        await connector.call("salesforce_export_report", EXPORT_ARGS)

        assert len(gated_call_spy) == 1
        call = gated_call_spy[0]
        assert call["gate"] == "review"
        assert call["tool"] == "salesforce_export_report"
        assert call["filtered_data"] is None
        assert call["preview"] == {"Report": "Pipeline", "Report ID": REPORT_ID, "Report runs": "1"}
        assert call["preview_tables"][0]["headers"] == COLUMNS
        assert call["preview_tables"][0]["rows"][0][1] == "Account 1"
        assert list(call["raw_data"]) == ["report_id", "rows", "columns", "sha256"]
        assert "Account 1" not in json.dumps(call["raw_data"])
        assert call["new_info"]["Saved to"] == f"~/out/{REPORT_ID}.csv"
        assert call["new_info"]["Rows"] == "3"
        assert "{agent}" not in call["details_text"]
        assert call["args"]["sha256"] == hashlib.sha256(delivered[0]["data"]).hexdigest()

    async def test_audit_delivery_is_passed(self, gated_call_spy, delivered):
        connector, _client = export_connector()

        await connector.call("salesforce_export_report", EXPORT_ARGS)

        assert gated_call_spy[0]["delivery"] == "local_disk"

    async def test_org_mode_delivers_a_one_time_link(self, gated_call_spy, delivered):
        connector, _client = export_connector()
        cfg = DownloadDeliveryConfig(link_ttl_seconds=60)
        connector.download_mode = "org"
        connector.download_config = cfg
        connector.download_base_url = "https://pf.example"

        await connector.call("salesforce_export_report", EXPORT_ARGS)

        assert delivered[0]["download_mode"] == "org"
        assert delivered[0]["download_config"] is cfg
        assert delivered[0]["download_base_url"] == "https://pf.example"
        assert gated_call_spy[0]["new_info"]["Saved to"] == export_files.ORG_SAVED_TO
        assert gated_call_spy[0]["delivery"] == "staged_link"

    async def test_org_mode_without_staging_is_refused_before_any_request(self, gated_call_spy, delivered):
        connector, client = export_connector()
        connector.download_mode = "org"
        connector.download_config = DownloadDeliveryConfig(allow_disk_staging=False)

        with pytest.raises(ValueError, match="allow_disk_staging"):
            await connector.call("salesforce_export_report", EXPORT_ARGS)

        client.run_report.assert_not_called()
        client.run_report_page.assert_not_called()
        assert gated_call_spy == []
        assert delivered == []

    async def test_deny_writes_nothing(self, monkeypatch, delivered):
        async def denied(**kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr(salesforce_module, "gated_call", denied)
        connector, _client = export_connector()

        with pytest.raises(PermissionError):
            await connector.call("salesforce_export_report", EXPORT_ARGS)

        assert delivered == []

    async def test_result_is_summary_only(self, gated_call_spy, delivered):
        connector, _client = export_connector()

        result = await connector.call("salesforce_export_report", EXPORT_ARGS)

        assert set(result) == {"rows", "columns", "sha256", "path", "name", "size_bytes", "delivery"}
        assert result["rows"] == 3
        assert result["columns"] == COLUMNS
        assert "Account 1" not in json.dumps(result)
