"""source.call: the ordered checks, the six adapters, the payload cap and the audit trail."""
from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.calendar_client import CalendarEvent
from privacyfence.confluence_client import ConfluenceClient, ConfluencePage
from privacyfence.jira_client import JiraClientError, JiraIssue
from privacyfence.plugins import cursors, source_ops
from privacyfence.plugins.constants import (
    CALENDAR_PAGE_SIZE_MAX,
    DRIVE_CHUNK_BYTES,
    JIRA_PAGE_SIZE_MAX,
    MAX_SOURCE_RESULT_BYTES,
    SOURCE_OPERATIONS,
)
from privacyfence.plugins.protocol import RpcError
from privacyfence.plugins.source_ops import SOURCE_ADAPTERS, handle_source_call
from privacyfence.plugins.spool import DownloadSpool
from privacyfence.principal import current_principal
from privacyfence.salesforce_client import SalesforceClient, SalesforceClientError

SENTINEL = "SENTINEL-do-not-log-4f1c"

ATTRS = {
    "salesforce": "_sf", "jira": "_jira", "drive": "_drive", "confluence": "_confluence", "calendar": "_calendar",
}


def _issue(key="PF-1", summary="s", **kw):
    return JiraIssue(key=key, summary=summary, status="Open", issue_type="Task", **kw)


def _event(event_id="e1", title="Standup"):
    return CalendarEvent(
        id=event_id, calendar_id="primary", title=title, description="", start_time="2026-01-01T09:00:00Z",
        end_time="2026-01-01T09:15:00Z", all_day=False, organizer_email="a@example.com", attendees=[],
        location="", hangout_link="", conference_link="", status="confirmed", html_link="",
    )


class Env:
    def __init__(self, tmp_path, ops=SOURCE_OPERATIONS, **clients):
        self.manifest = SimpleNamespace(source_operations=frozenset(ops))
        self.clients = clients
        self.connectors = {
            name: SimpleNamespace(**{ATTRS[name]: client}) for name, client in clients.items()
        }
        self.state = {name: (True, None) for name in ATTRS}
        self.spool = DownloadSpool(tmp_path / "spool")
        self.introspecting = False

    async def call(self, operation, params=None, *, principal="local", **extra):
        return await handle_source_call(
            {"principal": principal, "operation": operation, "params": params or {}, **extra},
            plugin="today",
            manifest=self.manifest,
            introspecting=self.introspecting,
            connectors_provider=lambda: self.connectors,
            connector_state=lambda name: self.state[name],
            spool=self.spool,
        )


@pytest.fixture
def audit_dir(tmp_path):
    path = tmp_path / "audit"
    init_audit_logger(str(path))
    return path


def audit_lines(audit_dir):
    lines = []
    for file in audit_dir.glob("*.jsonl"):
        lines += [json.loads(line) for line in file.read_text().splitlines() if line]
    return lines


async def _code(awaitable) -> RpcError:
    with pytest.raises(RpcError) as err:
        await awaitable
    return err.value


class TestAllowlist:
    async def test_operation_outside_manifest_refused(self, tmp_path, audit_dir):
        env = Env(tmp_path, ops=("jira.search",), calendar=MagicMock())
        err = await _code(env.call("calendar.list_events", {"time_min": "a", "time_max": "b"}))
        assert err.code == "operation_not_allowed"

    async def test_operation_outside_allowlist_refused(self, tmp_path, audit_dir):
        env = Env(tmp_path, ops=("gmail.read",), jira=MagicMock())
        err = await _code(env.call("gmail.read"))
        assert err.code == "operation_not_allowed"
        assert audit_lines(audit_dir)[0]["tool"] == "unknown"


class TestPrincipal:
    async def test_unknown_principal(self, tmp_path, audit_dir):
        env = Env(tmp_path, jira=MagicMock())
        err = await _code(env.call("jira.search", {"jql": "x"}, principal="alice"))
        assert err.code == "unknown_principal"


class TestOrgOnly:
    async def test_credential_rejected(self, tmp_path, audit_dir):
        env = Env(tmp_path, jira=MagicMock())
        err = await _code(env.call("jira.search", {"jql": "x"}, credential="secret"))
        assert err.code == "org_only_field"

    async def test_unparsable_params_are_invalid(self, tmp_path, audit_dir):
        env = Env(tmp_path)
        err = await _code(handle_source_call(
            {"principal": "local"}, plugin="today", manifest=env.manifest, introspecting=False,
            connectors_provider=lambda: {}, connector_state=lambda n: (True, None), spool=env.spool,
        ))
        assert err.code == "invalid_params"


class TestIntrospection:
    async def test_refused_before_anything_else(self, tmp_path, audit_dir):
        env = Env(tmp_path, jira=MagicMock())
        env.introspecting = True
        err = await _code(env.call("jira.search", {"jql": "x"}, credential="secret", principal="alice"))
        assert err.code == "introspection_only"
        assert len(audit_lines(audit_dir)) == 1


class TestConnectorUnavailable:
    @pytest.mark.parametrize(
        ("state", "built", "reason"),
        [
            ((False, None), True, "disabled"),
            ((False, "not_authenticated"), True, "disabled"),
            ((True, "not_authenticated"), True, "not_authenticated"),
            ((True, None), False, "not_authenticated"),
            ((True, "client_missing"), True, "unavailable"),
        ],
    )
    async def test_reasons(self, tmp_path, audit_dir, state, built, reason):
        env = Env(tmp_path, jira=MagicMock()) if built else Env(tmp_path)
        env.state["jira"] = state
        err = await _code(env.call("jira.search", {"jql": "x"}))
        assert err.code == "connector_unavailable"
        assert err.to_error()["data"]["reason"] == reason

    async def test_missing_client_is_unavailable(self, tmp_path, audit_dir):
        env = Env(tmp_path, jira=None)
        err = await _code(env.call("jira.search", {"jql": "x"}))
        assert err.to_error()["data"]["reason"] == "unavailable"


class TestUpstreamError:
    async def test_message_not_leaked(self, tmp_path, audit_dir, caplog):
        jira = MagicMock()
        jira.search_issues_page.side_effect = JiraClientError(f"boom {SENTINEL}")
        env = Env(tmp_path, jira=jira)
        err = await _code(env.call("jira.search", {"jql": "x"}))
        assert err.code == "upstream_error"
        assert SENTINEL not in err.detail and SENTINEL not in str(err.to_error())
        assert SENTINEL not in caplog.text
        assert SENTINEL not in json.dumps(audit_lines(audit_dir))
        assert "JiraClientError" in caplog.text

    async def test_runtime_error_is_upstream_too(self, tmp_path, audit_dir):
        jira = MagicMock()
        jira.search_issues_page.side_effect = RuntimeError("x")
        err = await _code(Env(tmp_path, jira=jira).call("jira.search", {"jql": "x"}))
        assert err.code == "upstream_error"

    async def test_unexpected_exception_is_internal_error(self, tmp_path, audit_dir):
        jira = MagicMock()
        jira.search_issues_page.side_effect = KeyError(SENTINEL)
        err = await _code(Env(tmp_path, jira=jira).call("jira.search", {"jql": "x"}))
        assert err.code == "internal_error" and SENTINEL not in str(err.to_error())
        assert audit_lines(audit_dir)[0]["summary"].endswith("error=internal_error")


class TestPayloadCap:
    async def test_result_over_the_cap_refused(self, tmp_path, audit_dir):
        drive = MagicMock()
        drive.get_sheet_values.return_value = [["x" * (MAX_SOURCE_RESULT_BYTES + 1)]]
        err = await _code(Env(tmp_path, drive=drive).call("sheets.get_values", {"spreadsheet_id": "s", "range": "A1"}))
        assert err.code == "payload_too_large"

    async def test_result_under_the_cap_served(self, tmp_path, audit_dir):
        drive = MagicMock()
        drive.get_sheet_values.return_value = [["x" * (MAX_SOURCE_RESULT_BYTES - 100_000)]]
        result = await Env(tmp_path, drive=drive).call("sheets.get_values", {"spreadsheet_id": "s", "range": "A1"})
        assert result["bytes"] < MAX_SOURCE_RESULT_BYTES

    async def test_an_unpaged_result_over_the_cap_is_still_refused(self, tmp_path, audit_dir):
        sf = MagicMock()
        sf.run_report.return_value = {"rows": ["x" * (MAX_SOURCE_RESULT_BYTES + 1)]}
        err = await _code(Env(tmp_path, salesforce=sf).call("salesforce.report_run", {"report_id": "00O1"}))
        assert err.code == "payload_too_large"

    async def test_non_ascii_counts_utf8_bytes(self, tmp_path, audit_dir, monkeypatch):
        monkeypatch.setattr(source_ops, "MAX_SOURCE_RESULT_BYTES", 100)
        drive = MagicMock()
        drive.get_sheet_values.return_value = [["é" * 20]]
        result = await Env(tmp_path, drive=drive).call("sheets.get_values", {"spreadsheet_id": "s", "range": "A1"})
        assert result["bytes"] == len(json.dumps(result["data"], ensure_ascii=False).encode())
        assert result["bytes"] <= 100


class TestAuditNoContent:
    async def test_response_bytes_absent_from_audit(self, tmp_path, audit_dir):
        drive = MagicMock()
        drive.get_sheet_values.return_value = [[SENTINEL, 1]]
        env = Env(tmp_path, drive=drive)
        result = await env.call("sheets.get_values", {"spreadsheet_id": "sheet1", "range": "A1:C9"})
        assert SENTINEL in json.dumps(result)
        for line in audit_dir.glob("*.jsonl"):
            assert SENTINEL not in line.read_text()
        (entry,) = audit_lines(audit_dir)
        assert entry["connector"] == "plugin:today"
        assert entry["tool"] == "sheets.get_values"
        assert entry["tool_name"] == "today source read"
        assert entry["decision"] == "plugin_source"
        assert entry["request_id"] == "" and entry["sender"] == ""
        assert entry["summary"] == f"sheet1; A1:C9; bytes={result['bytes']}"
        assert entry["week"] == current_week()

    async def test_one_entry_per_error(self, tmp_path, audit_dir):
        env = Env(tmp_path, jira=MagicMock())
        await _code(env.call("jira.search", {}))
        (entry,) = audit_lines(audit_dir)
        assert entry["summary"] == "-; error=invalid_params"
        assert entry["tool"] == "jira.search"

    async def test_audit_failure_never_fails_the_call(self, tmp_path, audit_dir, monkeypatch):
        monkeypatch.setattr(source_ops, "get_audit_logger", MagicMock(side_effect=OSError("disk")))
        jira = MagicMock()
        jira.search_issues_page.return_value = ([], None)
        assert (await Env(tmp_path, jira=jira).call("jira.search", {"jql": "x"}))["data"] == []


class TestPrincipalScope:
    async def test_adapter_runs_as_local(self, tmp_path, audit_dir):
        seen = []
        jira = MagicMock()
        jira.search_issues_page.side_effect = lambda *a: seen.append(current_principal().id) or ([], None)
        await Env(tmp_path, jira=jira).call("jira.search", {"jql": "x"})
        assert seen == ["local"]


class TestAdapterSalesforce:
    async def test_report_run_is_raw_and_filters_are_parsed(self, tmp_path, audit_dir):
        sf = MagicMock()
        raw = {"allData": True, "factMap": {"T!T": {"rows": [SENTINEL]}}}
        sf.run_report.return_value = raw
        filters = [{"column": "ACCOUNT.NAME", "operator": "equals", "value": "Acme"}]
        result = await Env(tmp_path, salesforce=sf).call(
            "salesforce.report_run", {"report_id": "00O1", "filters": filters}
        )
        assert result["data"] == raw and result["next_cursor"] is None
        args, kwargs = sf.run_report.call_args
        assert args == ("00O1",) and kwargs["filters"][0].values == ["Acme"]
        assert audit_lines(audit_dir)[0]["summary"].startswith("00O1; filters=1; bytes=")

    async def test_no_filters_passes_none(self, tmp_path, audit_dir):
        sf = MagicMock()
        sf.run_report.return_value = {}
        await Env(tmp_path, salesforce=sf).call("salesforce.report_run", {"report_id": "00O1"})
        assert sf.run_report.call_args.kwargs["filters"] is None

    async def test_bad_filter_operator_is_invalid_params(self, tmp_path, audit_dir):
        sf = MagicMock()
        filters = [{"column": "ACCOUNT.NAME", "operator": "drop table", "value": "x"}]
        err = await _code(Env(tmp_path, salesforce=sf).call(
            "salesforce.report_run", {"report_id": "00O1", "filters": filters}
        ))
        assert err.code == "invalid_params"
        sf.run_report.assert_not_called()

    @pytest.mark.parametrize("filters", ["x", [1], [{"column": "A", "operator": "equals"}], [{"column": "", "operator": "equals", "value": "x"}]])
    async def test_malformed_filters_are_invalid_params(self, tmp_path, audit_dir, filters):
        err = await _code(Env(tmp_path, salesforce=MagicMock()).call(
            "salesforce.report_run", {"report_id": "00O1", "filters": filters}
        ))
        assert err.code == "invalid_params"

    async def test_client_error_is_upstream(self, tmp_path, audit_dir):
        sf = MagicMock()
        sf.run_report.side_effect = SalesforceClientError("nope")
        err = await _code(Env(tmp_path, salesforce=sf).call("salesforce.report_run", {"report_id": "00O1"}))
        assert err.code == "upstream_error"


class TestAdapterJira:
    async def test_search_is_normalized(self, tmp_path, audit_dir):
        jira = MagicMock()
        jira.search_issues_page.return_value = ([_issue(labels=["a"])], None)
        jql = "project = PF"
        result = await Env(tmp_path, jira=jira).call("jira.search", {"jql": jql, "page_size": 5})
        assert result["data"][0]["key"] == "PF-1" and result["data"][0]["labels"] == ["a"]
        assert result["next_cursor"] is None
        assert jira.search_issues_page.call_args.args == (jql, 5, None)
        summary = audit_lines(audit_dir)[0]["summary"]
        assert summary.startswith(f"jql:{hashlib.sha256(jql.encode()).hexdigest()[:16]}; page_size=5")
        assert jql not in summary

    async def test_default_page_size(self, tmp_path, audit_dir):
        jira = MagicMock()
        jira.search_issues_page.return_value = ([], None)
        await Env(tmp_path, jira=jira).call("jira.search", {"jql": "x"})
        assert jira.search_issues_page.call_args.args == ("x", JIRA_PAGE_SIZE_MAX, None)

    @pytest.mark.parametrize("params", [
        {}, {"jql": ""}, {"jql": 1}, {"jql": "x" * 9000}, {"jql": "x", "max_results": 0},
        {"jql": "x", "max_results": 501}, {"jql": "x", "max_results": True}, {"jql": "x", "max_results": "5"},
        {"jql": "x", "page_size": 0}, {"jql": "x", "page_size": 101}, {"jql": "x", "page_size": True},
        {"jql": "x", "cursor": ""}, {"jql": "x", "cursor": 5}, {"jql": "x", "cursor": "x" * 4097},
    ])
    async def test_invalid_params(self, tmp_path, audit_dir, params):
        err = await _code(Env(tmp_path, jira=MagicMock()).call("jira.search", params))
        assert err.code == "invalid_params"
        assert SENTINEL not in err.detail


def _binary_drive(data: bytes, revision: str = "r1", size: int | None = None):
    drive = MagicMock()
    drive.get_file_metadata.return_value = SimpleNamespace(
        size=len(data) if size is None else size, mime_type="text/plain", modified_time=revision
    )
    drive.download_range.side_effect = lambda file_id, offset, length: data[offset:offset + length]
    return drive


class TestAdapterDrive:
    async def test_download_returns_chunks_and_a_cursor(self, tmp_path, audit_dir):
        data = b"0123456789"
        drive = _binary_drive(data)
        env = Env(tmp_path, drive=drive)
        first = await env.call("drive.download", {"file_id": "f1", "length": 4})
        assert first["next_cursor"] and first["data"]["revision"] == "r1"
        assert base64.b64decode(first["data"]["content_base64"]) == b"0123"
        second = await env.call("drive.download", {"file_id": "f1", "cursor": first["next_cursor"], "length": 100})
        assert base64.b64decode(second["data"]["content_base64"]) == b"456789"
        assert second["data"]["eof"] is True and second["next_cursor"] is None
        summaries = [line["summary"] for line in audit_lines(audit_dir)]
        assert summaries[0].startswith("f1; offset=0; length=4; bytes=") and summaries[0].endswith("; more")
        assert summaries[1].startswith("f1; offset=cursor; length=100; page; bytes=")
        assert not summaries[1].endswith("; more")
        assert "0123" not in " ".join(summaries) and "MDEyMw" not in " ".join(summaries)

    async def test_offset_and_cursor_together_are_invalid(self, tmp_path, audit_dir):
        err = await _code(Env(tmp_path, drive=MagicMock()).call(
            "drive.download", {"file_id": "f1", "offset": 1, "cursor": "abc"}
        ))
        assert err.code == "invalid_params"

    @pytest.mark.parametrize("params", [
        {}, {"file_id": "f", "length": 0}, {"file_id": "f", "length": DRIVE_CHUNK_BYTES + 1},
        {"file_id": "f", "offset": -1},
    ])
    async def test_invalid_params(self, tmp_path, audit_dir, params):
        err = await _code(Env(tmp_path, drive=MagicMock()).call("drive.download", params))
        assert err.code == "invalid_params"

    async def test_a_file_has_no_size_cap(self, tmp_path, audit_dir):
        drive = MagicMock()
        drive.get_file_metadata.return_value = SimpleNamespace(size=10**9, mime_type="video/mp4", modified_time="r1")
        drive.download_range.return_value = b"x" * 4
        result = await Env(tmp_path, drive=drive).call("drive.download", {"file_id": "f1", "length": 4})
        assert result["data"]["total_size_bytes"] == 10**9 and result["next_cursor"]


    async def test_a_google_form_is_not_downloadable(self, tmp_path, audit_dir):
        drive = MagicMock()
        drive.get_file_metadata.return_value = SimpleNamespace(
            size=0, mime_type="application/vnd.google-apps.form", modified_time="r1"
        )
        err = await _code(Env(tmp_path, drive=drive).call("drive.download", {"file_id": "f1"}))
        assert err.code == "invalid_params" and err.extra["reason"] == "not_downloadable"
        drive.download_range.assert_not_called()
        drive.download_file_bytes.assert_not_called()


class TestAdapterSheets:
    async def test_values_are_raw(self, tmp_path, audit_dir):
        drive = MagicMock()
        drive.get_sheet_values.return_value = [["a", 1], ["b", 2.5]]
        result = await Env(tmp_path, drive=drive).call(
            "sheets.get_values", {"spreadsheet_id": "s1", "range": "Sheet1!A1:C9", "value_render_option": "FORMULA"}
        )
        assert result["data"] == {"values": [["a", 1], ["b", 2.5]], "first_row": 0}
        assert drive.get_sheet_values.call_args.args == ("s1", "Sheet1!A1:C9", "FORMULA")

    async def test_default_render_option(self, tmp_path, audit_dir):
        drive = MagicMock()
        drive.get_sheet_values.return_value = []
        await Env(tmp_path, drive=drive).call("sheets.get_values", {"spreadsheet_id": "s1", "range": "A1"})
        assert drive.get_sheet_values.call_args.args[2] == "FORMATTED_VALUE"

    @pytest.mark.parametrize("params", [
        {"range": "A1"}, {"spreadsheet_id": "s"}, {"spreadsheet_id": "s", "range": "A1", "value_render_option": "RAW"},
        {"spreadsheet_id": "s", "range": "A1", "cursor": 3},
    ])
    async def test_invalid_params(self, tmp_path, audit_dir, params):
        err = await _code(Env(tmp_path, drive=MagicMock()).call("sheets.get_values", params))
        assert err.code == "invalid_params"


class TestAdapterConfluence:
    async def test_page_is_normalized(self, tmp_path, audit_dir):
        confluence = MagicMock()
        confluence.get_page.return_value = ConfluencePage(
            id="9", title="T", space_key="PF", body="<p>hi</p>", mentions={"u1": "Ann"}
        )
        result = await Env(tmp_path, confluence=confluence).call("confluence.get_page", {"page_id": "9"})
        assert result["data"]["body"] == "<p>hi</p>" and result["data"]["mentions"] == {"u1": "Ann"}
        assert result["data"]["body_offset"] == 0 and result["data"]["body_total_chars"] == 9
        assert result["next_cursor"] is None
        assert audit_lines(audit_dir)[0]["summary"].startswith("9; bytes=")

    async def test_page_id_is_required(self, tmp_path, audit_dir):
        err = await _code(Env(tmp_path, confluence=MagicMock()).call("confluence.get_page", {}))
        assert err.code == "invalid_params"


class TestAdapterCalendar:
    async def test_events_are_normalized(self, tmp_path, audit_dir):
        calendar = MagicMock()
        calendar.list_events_page.return_value = ([_event()], None)
        params = {"time_min": "2026-01-01T00:00:00Z", "time_max": "2026-01-02T00:00:00Z"}
        result = await Env(tmp_path, calendar=calendar).call("calendar.list_events", params)
        assert result["data"][0]["title"] == "Standup"
        assert calendar.list_events_page.call_args.args == ("primary", 250, params["time_min"], params["time_max"], None)
        assert audit_lines(audit_dir)[0]["summary"].startswith(
            "primary; 2026-01-01T00:00:00Z; 2026-01-02T00:00:00Z; bytes="
        )

    async def test_explicit_calendar_and_limit(self, tmp_path, audit_dir):
        calendar = MagicMock()
        calendar.list_events_page.return_value = ([], None)
        await Env(tmp_path, calendar=calendar).call(
            "calendar.list_events", {"calendar_id": "team", "time_min": "a", "time_max": "b", "page_size": 3}
        )
        assert calendar.list_events_page.call_args.args == ("team", 3, "a", "b", None)

    async def test_non_json_values_are_serialized_as_strings(self, tmp_path, audit_dir):
        from datetime import datetime
        drive = MagicMock()
        drive.get_sheet_values.return_value = [[datetime(2026, 1, 1)]]
        result = await Env(tmp_path, drive=drive).call("sheets.get_values", {"spreadsheet_id": "s", "range": "A1"})
        assert result["data"]["values"] == [["2026-01-01 00:00:00"]]

    @pytest.mark.parametrize("params", [{"time_min": "a"}, {"time_max": "b"}, {"time_min": "a", "time_max": "b", "max_results": 251}, {"time_min": "a", "time_max": "b", "page_size": 251}])
    async def test_invalid_params(self, tmp_path, audit_dir, params):
        err = await _code(Env(tmp_path, calendar=MagicMock()).call("calendar.list_events", params))
        assert err.code == "invalid_params"


# --- paging ------------------------------------------------------------------------------------


def _size(value) -> int:
    return len(json.dumps(value, default=str, ensure_ascii=False).encode())


class PagedJira:
    """Serves ``pages`` by token: the token ``"n"`` names page ``n``, so a page can be asked for again."""

    def __init__(self, pages):
        self.pages = pages
        self.tokens: list[str | None] = []
        self.sizes: list[int] = []

    def search_issues_page(self, jql, page_size, token=None):
        self.tokens.append(token)
        self.sizes.append(page_size)
        index = int(token or 0)
        return self.pages[index], str(index + 1) if index + 1 < len(self.pages) else None


class PagedCalendar:
    def __init__(self, pages):
        self.pages = pages
        self.tokens: list[str | None] = []

    def list_events_page(self, calendar_id, page_size, time_min, time_max, token=None):
        self.tokens.append(token)
        index = int(token or 0)
        return self.pages[index], str(index + 1) if index + 1 < len(self.pages) else None


JIRA = ("jira.search", {"jql": "project = PF"})
CALENDAR = ("calendar.list_events", {"time_min": "a", "time_max": "b"})
SHEETS = ("sheets.get_values", {"spreadsheet_id": "s1", "range": "A1:B9"})
CONFLUENCE = ("confluence.get_page", {"page_id": "9"})


async def _follow(env, operation, params, extract, limit=50):
    """Call until ``next_cursor`` is ``None``; returns the extracted items and every result."""
    results, items, cursor = [], [], None
    for _ in range(limit):
        result = await env.call(operation, {**params, **({"cursor": cursor} if cursor else {})})
        results.append(result)
        items.extend(extract(result["data"]))
        cursor = result["next_cursor"]
        if cursor is None:
            return items, results
    raise AssertionError("the cursor never ended")


class TestPaging:
    async def test_jira_walks_two_provider_pages(self, tmp_path, audit_dir):
        jira = PagedJira([[_issue("PF-1"), _issue("PF-2")], [_issue("PF-3")]])
        keys, results = await _follow(Env(tmp_path, jira=jira), *JIRA, lambda data: [i["key"] for i in data])
        assert keys == ["PF-1", "PF-2", "PF-3"]
        assert jira.tokens == [None, "1"]
        assert results[-1]["next_cursor"] is None and results[0]["next_cursor"]

    async def test_jira_skips_within_a_page_when_the_budget_is_small(self, tmp_path, audit_dir, monkeypatch):
        issues = [_issue(f"PF-{n}", "s" * 200) for n in range(5)]
        jira = PagedJira([issues[:4], issues[4:]])
        one = _size([i.__dict__ for i in issues[:1]])
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", one * 2 + one // 2)
        keys, results = await _follow(Env(tmp_path, jira=jira), *JIRA, lambda data: [i["key"] for i in data])
        assert keys == [f"PF-{n}" for n in range(5)]
        assert [len(r["data"]) for r in results] == [2, 2, 1]
        # The first provider page is fetched again by its own token for each slice of it.
        assert jira.tokens == [None, None, "1"]

    async def test_jira_last_page_has_no_cursor(self, tmp_path, audit_dir):
        jira = PagedJira([[_issue("PF-1")]])
        result = await Env(tmp_path, jira=jira).call(*JIRA)
        assert result["next_cursor"] is None

    async def test_calendar_walks_two_provider_pages(self, tmp_path, audit_dir):
        calendar = PagedCalendar([[_event("e1"), _event("e2")], [_event("e3")]])
        ids, results = await _follow(Env(tmp_path, calendar=calendar), *CALENDAR, lambda data: [e["id"] for e in data])
        assert ids == ["e1", "e2", "e3"] and calendar.tokens == [None, "1"]
        assert results[-1]["next_cursor"] is None

    async def test_calendar_skips_within_a_page_when_the_budget_is_small(self, tmp_path, audit_dir, monkeypatch):
        calendar = PagedCalendar([[_event(f"e{n}", "t" * 300) for n in range(4)]])
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", 1300)
        ids, results = await _follow(Env(tmp_path, calendar=calendar), *CALENDAR, lambda data: [e["id"] for e in data])
        assert ids == ["e0", "e1", "e2", "e3"] and len(results) > 1
        assert set(calendar.tokens) == {None}
        assert results[-1]["next_cursor"] is None

    async def test_sheets_splits_rows(self, tmp_path, audit_dir, monkeypatch):
        rows = [[f"r{n}", "x" * 100] for n in range(10)]
        drive = MagicMock()
        drive.get_sheet_values.return_value = rows
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", _size(rows[:3]) + 10)
        got, results = await _follow(Env(tmp_path, drive=drive), *SHEETS, lambda data: data["values"])
        assert got == rows
        assert [r["data"]["first_row"] for r in results] == [0, 3, 6, 9]
        assert results[-1]["next_cursor"] is None
        assert drive.get_sheet_values.call_count == 1

    async def test_sheets_continuation_never_refetches(self, tmp_path, audit_dir, monkeypatch):
        rows = [[f"r{n}", "x" * 100] for n in range(10)]
        drive = MagicMock()
        drive.get_sheet_values.return_value = rows
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", _size(rows[:3]) + 10)
        env = Env(tmp_path, drive=drive)
        first = await env.call(*SHEETS)
        drive.get_sheet_values.return_value = [["changed"]] * 10
        got, results = [], [first]
        got.extend(first["data"]["values"])
        cursor = first["next_cursor"]
        while cursor:
            result = await env.call(SHEETS[0], {**SHEETS[1], "cursor": cursor})
            results.append(result)
            got.extend(result["data"]["values"])
            cursor = result["next_cursor"]
        assert got == rows
        assert drive.get_sheet_values.call_count == 1

    async def test_sheets_expired_snapshot_is_cursor_expired(self, tmp_path, audit_dir, monkeypatch):
        rows = [[f"r{n}", "x" * 100] for n in range(10)]
        drive = MagicMock()
        drive.get_sheet_values.return_value = rows
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", _size(rows[:3]) + 10)
        env = Env(tmp_path, drive=drive)
        first = await env.call(*SHEETS)
        env.spool.clear()
        err = await _code(env.call(SHEETS[0], {**SHEETS[1], "cursor": first["next_cursor"]}))
        assert err.code == "upstream_error" and err.extra["reason"] == "cursor_expired"

    async def test_sheets_single_page_writes_no_spool_file(self, tmp_path, audit_dir):
        drive = MagicMock()
        drive.get_sheet_values.return_value = [["a"], ["b"]]
        result = await Env(tmp_path, drive=drive).call(*SHEETS)
        assert result["next_cursor"] is None
        assert [p for p in (tmp_path / "spool").rglob("*") if p.is_file()] == []

    async def test_confluence_version_change_mid_read_is_revision_changed(self, tmp_path, audit_dir, monkeypatch):
        body = "".join(chr(97 + n % 26) for n in range(1000))
        confluence = MagicMock()
        confluence.get_page.side_effect = [
            ConfluencePage(id="9", title="T", space_key="PF", body=body, version=1),
            ConfluencePage(id="9", title="T", space_key="PF", body=body, version=2),
        ]
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", 600)
        env = Env(tmp_path, confluence=confluence)
        first = await env.call(*CONFLUENCE)
        err = await _code(env.call(CONFLUENCE[0], {**CONFLUENCE[1], "cursor": first["next_cursor"]}))
        assert err.code == "upstream_error" and err.extra["reason"] == "revision_changed"

    async def test_confluence_splits_the_body(self, tmp_path, audit_dir, monkeypatch):
        body = "".join(chr(97 + n % 26) for n in range(1000))
        confluence = MagicMock()
        confluence.get_page.return_value = ConfluencePage(id="9", title="T", space_key="PF", body=body, version=3)
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", 600)
        slices, results = await _follow(
            Env(tmp_path, confluence=confluence), *CONFLUENCE, lambda data: [data["body"]]
        )
        assert "".join(slices) == body and len(results) > 1
        offset = 0
        for result in results:
            assert result["data"]["body_offset"] == offset
            assert result["data"]["body_total_chars"] == 1000
            assert result["data"]["title"] == "T"
            assert result["bytes"] <= 600
            offset += len(result["data"]["body"])

    async def test_confluence_empty_body_is_one_page(self, tmp_path, audit_dir):
        confluence = MagicMock()
        confluence.get_page.return_value = ConfluencePage(id="9", title="T", space_key="PF", body="")
        result = await Env(tmp_path, confluence=confluence).call(*CONFLUENCE)
        assert result["data"]["body"] == "" and result["next_cursor"] is None
        assert (result["data"]["body_offset"], result["data"]["body_total_chars"]) == (0, 0)

    async def test_drive_cursor_carries_the_revision_and_offset(self, tmp_path, audit_dir):
        env = Env(tmp_path, drive=_binary_drive(b"0123456789", "rev-7"))
        first = await env.call("drive.download", {"file_id": "f1", "length": 4})
        state = cursors.decode(first["next_cursor"], "drive.download", {"file_id": "f1"})
        assert state == {"r": "rev-7", "o": 4}

    async def test_drive_random_offset_still_works(self, tmp_path, audit_dir):
        env = Env(tmp_path, drive=_binary_drive(b"0123456789"))
        result = await env.call("drive.download", {"file_id": "f1", "offset": 7, "length": 2})
        assert base64.b64decode(result["data"]["content_base64"]) == b"78"
        assert result["data"]["offset"] == 7 and result["next_cursor"]

    async def test_drive_serves_a_file_larger_than_64_mib(self, tmp_path, audit_dir):
        size = 100 * 1024 * 1024
        drive = _binary_drive(b"", size=size)
        drive.download_range.side_effect = lambda file_id, offset, length: b"z" * length
        result = await Env(tmp_path, drive=drive).call(
            "drive.download", {"file_id": "f1", "offset": size - 5, "length": 100}
        )
        assert result["data"]["length"] == 5 and result["data"]["eof"] is True
        assert result["data"]["total_size_bytes"] == size and result["next_cursor"] is None

    async def test_drive_revision_change_between_chunks(self, tmp_path, audit_dir):
        drive = _binary_drive(b"0123456789", "r1")
        env = Env(tmp_path, drive=drive)
        first = await env.call("drive.download", {"file_id": "f1", "length": 4})
        drive.get_file_metadata.return_value = SimpleNamespace(size=10, mime_type="text/plain", modified_time="r2")
        err = await _code(env.call("drive.download", {"file_id": "f1", "cursor": first["next_cursor"]}))
        assert err.code == "upstream_error" and err.extra == {"reason": "revision_changed"}

    async def test_audit_marks_a_page_and_more(self, tmp_path, audit_dir):
        jira = PagedJira([[_issue("PF-1")], [_issue("PF-2")]])
        env = Env(tmp_path, jira=jira)
        first = await env.call(*JIRA)
        await env.call(JIRA[0], {**JIRA[1], "cursor": first["next_cursor"]})
        first_line, second_line = (line["summary"] for line in audit_lines(audit_dir))
        assert first_line.endswith("; more") and "; page;" not in first_line
        assert "; page; bytes=" in second_line and not second_line.endswith("; more")


class TestCursorBinding:
    async def _jira_cursor(self, env_dir, **params):
        jira = PagedJira([[_issue("PF-1")], [_issue("PF-2")]])
        env = Env(env_dir, jira=jira)
        return env, (await env.call("jira.search", {"jql": "project = PF", **params}))["next_cursor"]

    async def test_a_cursor_works_with_its_own_parameters(self, tmp_path, audit_dir):
        env, cursor = await self._jira_cursor(tmp_path)
        result = await env.call("jira.search", {"jql": "project = PF", "cursor": cursor})
        assert result["data"][0]["key"] == "PF-2"

    @pytest.mark.parametrize("changed", [{"jql": "project = OTHER"}, {"page_size": 7}])
    async def test_a_changed_parameter_is_refused(self, tmp_path, audit_dir, changed):
        env, cursor = await self._jira_cursor(tmp_path)
        err = await _code(env.call("jira.search", {"jql": "project = PF", **changed, "cursor": cursor}))
        assert err.code == "invalid_params" and err.detail == "cursor belongs to a different call"

    async def test_a_cursor_of_another_operation_is_refused(self, tmp_path, audit_dir):
        _, cursor = await self._jira_cursor(tmp_path)
        env = Env(tmp_path, calendar=PagedCalendar([[]]))
        err = await _code(env.call(CALENDAR[0], {**CALENDAR[1], "cursor": cursor}))
        assert err.detail == "cursor belongs to a different call"

    async def test_drive_cursor_of_another_file_is_refused(self, tmp_path, audit_dir):
        env = Env(tmp_path, drive=_binary_drive(b"0123456789"))
        first = await env.call("drive.download", {"file_id": "f1", "length": 4})
        err = await _code(env.call("drive.download", {"file_id": "f2", "cursor": first["next_cursor"]}))
        assert err.code == "invalid_params" and err.detail == "cursor belongs to a different call"

    async def test_sheets_cursor_of_another_range_is_refused(self, tmp_path, audit_dir, monkeypatch):
        rows = [["x" * 100] for _ in range(4)]
        drive = MagicMock()
        drive.get_sheet_values.return_value = rows
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", _size(rows[:2]) + 5)
        env = Env(tmp_path, drive=drive)
        first = await env.call(*SHEETS)
        err = await _code(env.call(SHEETS[0], {**SHEETS[1], "range": "C1:D9", "cursor": first["next_cursor"]}))
        assert err.detail == "cursor belongs to a different call"

    @pytest.mark.parametrize("cursor", ["!!!", "e30", "bm90LWpzb24"])
    async def test_a_malformed_cursor_is_refused(self, tmp_path, audit_dir, cursor):
        err = await _code(Env(tmp_path, jira=MagicMock()).call("jira.search", {"jql": "x", "cursor": cursor}))
        assert err.code == "invalid_params" and err.detail == "cursor is not valid"


def _forged(operation, bound, state):
    return cursors.encode(operation, bound, state)


JIRA_BOUND = {"jql": "project = PF", "page_size": 100}
SHEETS_BOUND = {"spreadsheet_id": "s1", "range": "A1:B9", "value_render_option": "FORMATTED_VALUE"}


class TestCursorState:
    async def _jira(self, tmp_path, state, page=2):
        jira = PagedJira([[_issue(f"PF-{n}") for n in range(page)], [_issue("PF-9")]])
        env = Env(tmp_path, jira=jira)
        return await env.call(JIRA[0], {**JIRA[1], "cursor": _forged("jira.search", JIRA_BOUND, state)})

    @pytest.mark.parametrize("state", [
        {"t": 5, "k": 0}, {"t": ["a"], "k": 0}, {"t": None, "k": -1}, {"t": None, "k": True}, {"t": None, "k": "1"},
        {"t": None, "k": 1.0}, {"t": None, "k": 3}, {"t": None}, {"k": 0}, {"t": None, "k": 0, "x": 1}, {},
    ])
    async def test_jira_state_is_checked(self, tmp_path, audit_dir, state):
        err = await _code(self._jira(tmp_path, state))
        assert err.code == "invalid_params" and err.detail == "cursor is not valid"

    async def test_jira_skip_equal_to_the_page_length_moves_to_the_next_token(self, tmp_path, audit_dir):
        result = await self._jira(tmp_path, {"t": None, "k": 2})
        assert result["data"] == []
        assert cursors.decode(result["next_cursor"], "jira.search", JIRA_BOUND) == {"t": "1", "k": 0}

    async def test_jira_skip_inside_the_page_returns_the_rest(self, tmp_path, audit_dir):
        result = await self._jira(tmp_path, {"t": None, "k": 1})
        assert [i["key"] for i in result["data"]] == ["PF-1"]

    @pytest.mark.parametrize("state", [{"t": 1, "k": 0}, {"t": None, "k": 5}, {"t": "0", "k": True}])
    async def test_calendar_state_is_checked(self, tmp_path, audit_dir, state):
        bound = {"calendar_id": "primary", "time_min": "a", "time_max": "b", "page_size": CALENDAR_PAGE_SIZE_MAX}
        env = Env(tmp_path, calendar=PagedCalendar([[_event()]]))
        err = await _code(env.call(CALENDAR[0], {**CALENDAR[1], "cursor": _forged("calendar.list_events", bound, state)}))
        assert err.detail == "cursor is not valid"

    @pytest.mark.parametrize("state", [
        {"k": 3}, {"k": 0, "s": "0123456789abcdef", "x": 1}, {"k": -1, "s": "0123456789abcdef"},
        {"k": True, "s": "0123456789abcdef"}, {"k": "0", "s": "0123456789abcdef"}, {"s": "0123456789abcdef"},
        {"k": 0, "s": 5}, {"k": 0, "s": "../../etc/passwd"}, {"k": 0, "s": "0123456789ABCDEF"},
        {"k": 0, "s": "0123456789abcde"}, {"k": 0, "s": "0123456789abcdef0"}, {},
    ])
    async def test_sheets_state_is_checked(self, tmp_path, audit_dir, state):
        drive = MagicMock()
        err = await _code(Env(tmp_path, drive=drive).call(
            SHEETS[0], {**SHEETS[1], "cursor": _forged("sheets.get_values", SHEETS_BOUND, state)}
        ))
        assert err.code == "invalid_params" and err.detail == "cursor is not valid"
        drive.get_sheet_values.assert_not_called()

    async def test_sheets_unknown_snapshot_is_cursor_expired(self, tmp_path, audit_dir):
        drive = MagicMock()
        err = await _code(Env(tmp_path, drive=drive).call(
            SHEETS[0],
            {**SHEETS[1], "cursor": _forged("sheets.get_values", SHEETS_BOUND, {"k": 0, "s": "0123456789abcdef"})},
        ))
        assert err.code == "upstream_error" and err.extra["reason"] == "cursor_expired"
        drive.get_sheet_values.assert_not_called()

    async def test_sheets_skip_past_the_row_count_is_not_valid(self, tmp_path, audit_dir, monkeypatch):
        rows = [[f"r{n}", "x" * 100] for n in range(10)]
        drive = MagicMock()
        drive.get_sheet_values.return_value = rows
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", _size(rows[:3]) + 10)
        env = Env(tmp_path, drive=drive)
        first = await env.call(*SHEETS)
        snapshot = cursors.decode(first["next_cursor"], "sheets.get_values", SHEETS_BOUND)["s"]
        err = await _code(env.call(
            SHEETS[0], {**SHEETS[1], "cursor": _forged("sheets.get_values", SHEETS_BOUND, {"k": 11, "s": snapshot})}
        ))
        assert err.detail == "cursor is not valid"
        result = await env.call(
            SHEETS[0], {**SHEETS[1], "cursor": _forged("sheets.get_values", SHEETS_BOUND, {"k": 10, "s": snapshot})}
        )
        assert result["data"] == {"values": [], "first_row": 10} and result["next_cursor"] is None

    @pytest.mark.parametrize("state", [
        {"o": 6, "v": 1}, {"o": -1, "v": 1}, {"o": True, "v": 1}, {"o": "0", "v": 1}, {"o": 0, "v": -1},
        {"o": 0, "v": True}, {"o": 0, "v": "1"}, {"o": 0}, {"v": 1}, {"o": 0, "v": 1, "x": 1}, {},
    ])
    async def test_confluence_state_is_checked(self, tmp_path, audit_dir, state):
        confluence = MagicMock()
        confluence.get_page.return_value = ConfluencePage(
            id="9", title="T", space_key="PF", body="abcde", version=1
        )
        err = await _code(Env(tmp_path, confluence=confluence).call(
            CONFLUENCE[0], {**CONFLUENCE[1], "cursor": _forged("confluence.get_page", {"page_id": "9"}, state)}
        ))
        assert err.detail == "cursor is not valid"

    @pytest.mark.parametrize("state", [
        {"r": "r1", "o": 11}, {"r": "r1", "o": -1}, {"r": "r1", "o": True}, {"r": 5, "o": 0}, {"o": 0}, {"r": "r1"},
    ])
    async def test_drive_state_is_checked(self, tmp_path, audit_dir, state):
        env = Env(tmp_path, drive=_binary_drive(b"0123456789"))
        err = await _code(env.call(
            "drive.download", {"file_id": "f1", "cursor": _forged("drive.download", {"file_id": "f1"}, state)}
        ))
        assert err.code == "invalid_params" and err.detail == "cursor is not valid"

    async def test_drive_offset_at_the_end_is_an_empty_last_chunk(self, tmp_path, audit_dir):
        env = Env(tmp_path, drive=_binary_drive(b"0123456789"))
        result = await env.call(
            "drive.download",
            {"file_id": "f1", "cursor": _forged("drive.download", {"file_id": "f1"}, {"r": "r1", "o": 10})},
        )
        assert result["data"]["eof"] is True and result["data"]["length"] == 0


class TestSingleRecordTooLarge:
    async def test_jira(self, tmp_path, audit_dir, monkeypatch):
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", 300)
        jira = PagedJira([[_issue("PF-1", "s" * 1000)]])
        err = await _code(Env(tmp_path, jira=jira).call(*JIRA))
        assert err.code == "payload_too_large" and "single record" in err.detail

    async def test_calendar(self, tmp_path, audit_dir, monkeypatch):
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", 300)
        err = await _code(Env(tmp_path, calendar=PagedCalendar([[_event("e1", "t" * 1000)]])).call(*CALENDAR))
        assert err.code == "payload_too_large"

    async def test_sheets_row(self, tmp_path, audit_dir, monkeypatch):
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", 300)
        drive = MagicMock()
        drive.get_sheet_values.return_value = [["ok"], ["x" * 1000]]
        env = Env(tmp_path, drive=drive)
        first = await env.call(*SHEETS)
        assert first["data"]["values"] == [["ok"]]
        err = await _code(env.call(SHEETS[0], {**SHEETS[1], "cursor": first["next_cursor"]}))
        assert err.code == "payload_too_large"

    async def test_confluence_page_without_its_body(self, tmp_path, audit_dir, monkeypatch):
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", 300)
        confluence = MagicMock()
        confluence.get_page.return_value = ConfluencePage(id="9", title="T" * 1000, space_key="PF", body="abc")
        err = await _code(Env(tmp_path, confluence=confluence).call(*CONFLUENCE))
        assert err.code == "payload_too_large"

    async def test_confluence_empty_body_page(self, tmp_path, audit_dir, monkeypatch):
        monkeypatch.setattr(source_ops, "SOURCE_PAGE_BUDGET_BYTES", 300)
        confluence = MagicMock()
        confluence.get_page.return_value = ConfluencePage(id="9", title="T" * 1000, space_key="PF", body="")
        err = await _code(Env(tmp_path, confluence=confluence).call(*CONFLUENCE))
        assert err.code == "payload_too_large"


class TestAlias:
    async def test_jira_max_results_maps_to_page_size(self, tmp_path, audit_dir):
        jira = PagedJira([[_issue()]])
        await Env(tmp_path, jira=jira).call("jira.search", {"jql": "x", "max_results": 7})
        assert jira.sizes == [7]

    async def test_jira_max_results_500_is_clamped_to_100(self, tmp_path, audit_dir):
        jira = PagedJira([[_issue()]])
        await Env(tmp_path, jira=jira).call("jira.search", {"jql": "x", "max_results": 500})
        assert jira.sizes == [JIRA_PAGE_SIZE_MAX]

    async def test_page_size_wins_over_max_results(self, tmp_path, audit_dir):
        jira = PagedJira([[_issue()]])
        await Env(tmp_path, jira=jira).call("jira.search", {"jql": "x", "max_results": 50, "page_size": 3})
        assert jira.sizes == [3]

    async def test_a_cursor_issued_for_the_alias_matches_the_clamped_page_size(self, tmp_path, audit_dir):
        jira = PagedJira([[_issue("PF-1")], [_issue("PF-2")]])
        env = Env(tmp_path, jira=jira)
        first = await env.call("jira.search", {"jql": "x", "max_results": 500})
        second = await env.call("jira.search", {"jql": "x", "page_size": 100, "cursor": first["next_cursor"]})
        assert second["data"][0]["key"] == "PF-2"

    async def test_calendar_max_results_maps_to_page_size(self, tmp_path, audit_dir):
        calendar = MagicMock()
        calendar.list_events_page.return_value = ([], None)
        await Env(tmp_path, calendar=calendar).call(CALENDAR[0], {**CALENDAR[1], "max_results": 9})
        assert calendar.list_events_page.call_args.args[1] == 9


def test_every_operation_has_an_adapter():
    assert set(SOURCE_ADAPTERS) == set(SOURCE_OPERATIONS)


class TestPathIdsNeverReachTheService:
    async def test_a_report_id_with_a_path_is_refused_before_any_request(self, tmp_path, audit_dir):
        sf = SalesforceClient.__new__(SalesforceClient)
        sf._call = MagicMock()
        err = await _code(Env(tmp_path, salesforce=sf).call(
            "salesforce.report_run", {"report_id": "../../query?q=SELECT Id FROM Contact"}
        ))
        assert err.code == "upstream_error" and err.detail == "the service returned an error"
        sf._call.assert_not_called()

    async def test_a_page_id_with_a_path_is_refused_before_any_request(self, tmp_path, audit_dir):
        cf = ConfluenceClient.__new__(ConfluenceClient)
        cf._request = MagicMock()
        cf._client = MagicMock()
        err = await _code(Env(tmp_path, confluence=cf).call("confluence.get_page", {"page_id": "../x"}))
        assert err.code == "upstream_error" and err.detail == "the service returned an error"
        cf._request.assert_not_called()
        cf._client.get.assert_not_called()
