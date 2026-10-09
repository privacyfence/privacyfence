"""The test host's copy of the daemon's source-call parameter rules gives the daemon's verdicts."""
from __future__ import annotations

import pytest

from privacyfence.connectors import salesforce as daemon_salesforce
from privacyfence.plugins import constants, source_ops
from privacyfence.plugins.protocol import RpcError as DaemonRpcError
from privacyfence.salesforce_client import REPORT_FILTER_OPERATORS
from privacyfence_plugin_sdk._rpc import RpcError
from privacyfence_plugin_sdk.testing import _params

_FILTER = {"column": "Amount", "operator": "equals", "value": "1"}
_TIMES = {"time_min": "2025-01-01T00:00:00Z", "time_max": "2025-01-02T00:00:00Z"}
_SHEET = {"spreadsheet_id": "S1", "range": "A1:B2"}

CASES: list[tuple[str, dict]] = [
    ("salesforce.report_run", {"report_id": "R1"}),
    ("salesforce.report_run", {"report_id": "R1", "filters": [_FILTER]}),
    ("salesforce.report_run", {}),
    ("salesforce.report_run", {"report_id": None}),
    ("salesforce.report_run", {"report_id": ""}),
    ("salesforce.report_run", {"report_id": 5}),
    ("salesforce.report_run", {"report_id": "x" * 257}),
    ("salesforce.report_run", {"report_id": "x" * 256}),
    ("salesforce.report_run", {"report_id": "R1", "filters": "nope"}),
    ("salesforce.report_run", {"report_id": "R1", "filters": [{**_FILTER, "operator": "regex"}]}),
    ("salesforce.report_run", {"report_id": "R1", "filters": ["not a dict"]}),
    ("salesforce.report_run", {"report_id": "R1", "filters": [{"column": "A", "operator": "equals"}]}),
    ("salesforce.report_run", {"report_id": "R1", "filters": [{**_FILTER, "value": []}]}),
    ("salesforce.report_run", {"report_id": "R1", "filters": [{**_FILTER, "value": ["a", 1]}]}),
    ("salesforce.report_run", {"report_id": "R1", "filters": [{**_FILTER, "column": "  "}]}),
    ("jira.search", {"jql": "project = X"}),
    ("jira.search", {"jql": "project = X", "page_size": 100, "cursor": "abc"}),
    ("jira.search", {}),
    ("jira.search", {"jql": ""}),
    ("jira.search", {"jql": "x" * 8193}),
    ("jira.search", {"jql": "x" * 8192}),
    ("jira.search", {"jql": "p", "page_size": True}),
    ("jira.search", {"jql": "p", "page_size": 0}),
    ("jira.search", {"jql": "p", "page_size": 101}),
    ("jira.search", {"jql": "p", "max_results": 500}),
    ("jira.search", {"jql": "p", "max_results": 501}),
    ("jira.search", {"jql": "p", "cursor": "c" * 4097}),
    ("jira.search", {"jql": "p", "cursor": ""}),
    ("drive.download", {"file_id": "F1"}),
    ("drive.download", {"file_id": "F1", "offset": 10, "length": 5}),
    ("drive.download", {"file_id": "F1", "offset": 1, "cursor": "abc"}),
    ("drive.download", {"file_id": "F1", "offset": True}),
    ("drive.download", {"file_id": "F1", "offset": -1}),
    ("drive.download", {"file_id": "F1", "length": 0}),
    ("drive.download", {"file_id": "F1", "length": constants.DRIVE_CHUNK_BYTES + 1}),
    ("drive.download", {"file_id": "F1", "length": "9"}),
    ("drive.download", {"file_id": ""}),
    ("sheets.get_values", dict(_SHEET)),
    ("sheets.get_values", {**_SHEET, "value_render_option": "FORMULA"}),
    ("sheets.get_values", {**_SHEET, "value_render_option": "RAW"}),
    ("sheets.get_values", {"value_render_option": "RAW"}),
    ("sheets.get_values", {"range": "A1"}),
    ("sheets.get_values", {"spreadsheet_id": "S1"}),
    ("sheets.get_values", {**_SHEET, "range": "r" * 513}),
    ("sheets.get_values", {**_SHEET, "spreadsheet_id": ""}),
    ("sheets.get_values", {**_SHEET, "value_render_option": 3}),
    ("confluence.get_page", {"page_id": "P1"}),
    ("confluence.get_page", {}),
    ("confluence.get_page", {"page_id": 12}),
    ("confluence.get_page", {"page_id": "p" * 257}),
    ("confluence.get_page", {"page_id": "P1", "cursor": "c" * 4097}),
    ("calendar.list_events", dict(_TIMES)),
    ("calendar.list_events", {**_TIMES, "calendar_id": "work", "page_size": 250}),
    ("calendar.list_events", {"time_max": "2025"}),
    ("calendar.list_events", {"time_min": "2025"}),
    ("calendar.list_events", {**_TIMES, "calendar_id": ""}),
    ("calendar.list_events", {**_TIMES, "time_min": "t" * 65}),
    ("calendar.list_events", {**_TIMES, "page_size": 251}),
    ("calendar.list_events", {**_TIMES, "page_size": False}),
    ("calendar.list_events", {**_TIMES, "max_results": 251}),
    ("calendar.list_events", {**_TIMES, "max_results": 250}),
]


def _daemon(operation: str, params: dict) -> tuple[str, str] | str:
    try:
        source_ops.SOURCE_ADAPTERS[operation].validate(params)
    except DaemonRpcError as exc:
        return exc.code, exc.detail
    return "ok"


def _host(operation: str, params: dict) -> tuple[str, str] | str:
    try:
        _params.validate(operation, params)
    except RpcError as exc:
        return exc.code, exc.detail
    return "ok"


@pytest.mark.parametrize("operation,params", CASES)
def test_same_verdict(operation, params):
    assert _host(operation, params) == _daemon(operation, params)


def test_every_operation_has_cases():
    assert {operation for operation, _ in CASES} == set(constants.SOURCE_OPERATIONS)
    for operation in constants.SOURCE_OPERATIONS:
        assert sum(1 for op, _ in CASES if op == operation) >= 3
        assert any(_daemon(operation, p) == "ok" for op, p in CASES if op == operation)
        assert any(_daemon(operation, p) != "ok" for op, p in CASES if op == operation)


def test_copied_limits_match():
    for name in ("_MAX_ID_CHARS", "_MAX_RANGE_CHARS", "_MAX_TIME_CHARS", "_MAX_JQL_CHARS", "_VALUE_RENDER_OPTIONS"):
        assert getattr(_params, name) == getattr(source_ops, name), name
    for name in ("CURSOR_MAX_CHARS", "JIRA_PAGE_SIZE_MAX", "CALENDAR_PAGE_SIZE_MAX", "DRIVE_CHUNK_BYTES"):
        assert getattr(_params, name) == getattr(constants, name), name
    assert _params.REPORT_FILTER_OPERATORS == REPORT_FILTER_OPERATORS
    assert _params._FILTERS_SHAPE_ERROR == daemon_salesforce._FILTERS_SHAPE_ERROR


def _serve(fixtures, operation, params):
    return fixtures._serve({"principal": "local", "operation": operation, "params": params}, "local")


def test_the_host_refuses_bad_params_before_matching_a_fixture():
    from privacyfence_plugin_sdk.testing import _source

    fixtures = _source.SourceFixtures()
    fixtures.load(_source.samples.get("jira.search"))
    with pytest.raises(RpcError) as refused:
        _serve(fixtures, "jira.search", {"jql": ""})
    assert (refused.value.code, refused.value.detail) == ("invalid_params", "params.jql must not be empty")


def test_a_connector_unavailable_fixture_wins_over_bad_params():
    from privacyfence_plugin_sdk.testing import _source

    fixtures = _source.SourceFixtures()
    fixtures.fail("jira.search", "connector_unavailable", "not_connected")
    with pytest.raises(RpcError) as refused:
        _serve(fixtures, "jira.search", {})
    assert refused.value.code == "connector_unavailable"
