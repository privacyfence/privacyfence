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
from privacyfence.confluence_client import ConfluencePage
from privacyfence.jira_client import JiraClientError, JiraIssue
from privacyfence.plugins import source_ops
from privacyfence.plugins.constants import (
    DRIVE_CHUNK_BYTES,
    MAX_SOURCE_RESULT_BYTES,
    SOURCE_OPERATIONS,
)
from privacyfence.plugins.protocol import RpcError
from privacyfence.plugins.source_ops import SOURCE_ADAPTERS, handle_source_call
from privacyfence.plugins.spool import DownloadSpool
from privacyfence.principal import current_principal
from privacyfence.salesforce_client import SalesforceClientError

SENTINEL = "SENTINEL-do-not-log-4f1c"

ATTRS = {
    "salesforce": "_sf", "jira": "_jira", "drive": "_drive", "confluence": "_confluence", "calendar": "_calendar",
}


def _issue(**kw):
    return JiraIssue(key="PF-1", summary="s", status="Open", issue_type="Task", **kw)


def _event():
    return CalendarEvent(
        id="e1", calendar_id="primary", title="Standup", description="", start_time="2026-01-01T09:00:00Z",
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
        jira.search_issues.side_effect = JiraClientError(f"boom {SENTINEL}")
        env = Env(tmp_path, jira=jira)
        err = await _code(env.call("jira.search", {"jql": "x"}))
        assert err.code == "upstream_error"
        assert SENTINEL not in err.detail and SENTINEL not in str(err.to_error())
        assert SENTINEL not in caplog.text
        assert SENTINEL not in json.dumps(audit_lines(audit_dir))
        assert "JiraClientError" in caplog.text

    async def test_runtime_error_is_upstream_too(self, tmp_path, audit_dir):
        jira = MagicMock()
        jira.search_issues.side_effect = RuntimeError("x")
        err = await _code(Env(tmp_path, jira=jira).call("jira.search", {"jql": "x"}))
        assert err.code == "upstream_error"

    async def test_unexpected_exception_is_internal_error(self, tmp_path, audit_dir):
        jira = MagicMock()
        jira.search_issues.side_effect = KeyError(SENTINEL)
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
        drive.get_sheet_values.return_value = [["x" * (MAX_SOURCE_RESULT_BYTES - 1000)]]
        result = await Env(tmp_path, drive=drive).call("sheets.get_values", {"spreadsheet_id": "s", "range": "A1"})
        assert result["bytes"] < MAX_SOURCE_RESULT_BYTES


class TestAuditNoContent:
    async def test_response_bytes_absent_from_audit(self, tmp_path, audit_dir):
        drive = MagicMock()
        drive.get_sheet_values.return_value = [[SENTINEL, 1]]
        env = Env(tmp_path, drive=drive)
        result = await env.call("sheets.get_values", {"spreadsheet_id": "sheet1", "range": "A1:B2"})
        assert SENTINEL in json.dumps(result)
        for line in audit_dir.glob("*.jsonl"):
            assert SENTINEL not in line.read_text()
        (entry,) = audit_lines(audit_dir)
        assert entry["connector"] == "plugin:today"
        assert entry["tool"] == "sheets.get_values"
        assert entry["tool_name"] == "today source read"
        assert entry["decision"] == "plugin_source"
        assert entry["request_id"] == "" and entry["sender"] == ""
        assert entry["summary"] == f"sheet1; A1:B2; bytes={result['bytes']}"
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
        jira.search_issues.return_value = []
        assert (await Env(tmp_path, jira=jira).call("jira.search", {"jql": "x"}))["data"] == []


class TestPrincipalScope:
    async def test_adapter_runs_as_local(self, tmp_path, audit_dir):
        seen = []
        jira = MagicMock()
        jira.search_issues.side_effect = lambda *a: seen.append(current_principal().id) or []
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
        jira.search_issues.return_value = [_issue(labels=["a"])]
        jql = "project = PF"
        result = await Env(tmp_path, jira=jira).call("jira.search", {"jql": jql, "max_results": 5})
        assert result["data"][0]["key"] == "PF-1" and result["data"][0]["labels"] == ["a"]
        assert jira.search_issues.call_args.args == (jql, 5)
        summary = audit_lines(audit_dir)[0]["summary"]
        assert summary.startswith(f"jql:{hashlib.sha256(jql.encode()).hexdigest()[:16]}; max_results=5")
        assert jql not in summary

    async def test_default_max_results(self, tmp_path, audit_dir):
        jira = MagicMock()
        jira.search_issues.return_value = []
        await Env(tmp_path, jira=jira).call("jira.search", {"jql": "x"})
        assert jira.search_issues.call_args.args == ("x", 100)

    @pytest.mark.parametrize("params", [
        {}, {"jql": ""}, {"jql": 1}, {"jql": "x" * 9000}, {"jql": "x", "max_results": 0},
        {"jql": "x", "max_results": 501}, {"jql": "x", "max_results": True}, {"jql": "x", "max_results": "5"},
    ])
    async def test_invalid_params(self, tmp_path, audit_dir, params):
        err = await _code(Env(tmp_path, jira=MagicMock()).call("jira.search", params))
        assert err.code == "invalid_params"
        assert SENTINEL not in err.detail


class TestAdapterDrive:
    async def test_download_returns_chunks_and_a_cursor(self, tmp_path, audit_dir):
        data = b"0123456789"
        drive = MagicMock()
        drive.get_file_metadata.return_value = SimpleNamespace(size=10, modified_time="r1")
        drive.download_file_bytes.return_value = {"data": data, "name": "n", "mime_type": "text/plain", "size_bytes": 10}
        env = Env(tmp_path, drive=drive)
        first = await env.call("drive.download", {"file_id": "f1", "length": 4})
        assert first["next_cursor"] and first["data"]["revision"] == "r1"
        assert base64.b64decode(first["data"]["content_base64"]) == b"0123"
        second = await env.call("drive.download", {"file_id": "f1", "cursor": first["next_cursor"], "length": 100})
        assert base64.b64decode(second["data"]["content_base64"]) == b"456789"
        assert second["data"]["eof"] is True and second["next_cursor"] is None
        summaries = [line["summary"] for line in audit_lines(audit_dir)]
        assert summaries[0].startswith("f1; offset=0; length=4; bytes=")
        assert summaries[1].startswith("f1; offset=cursor; length=100; bytes=")
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

    async def test_oversize_file_is_payload_too_large(self, tmp_path, audit_dir):
        drive = MagicMock()
        drive.get_file_metadata.return_value = SimpleNamespace(size=10**9, modified_time="r1")
        err = await _code(Env(tmp_path, drive=drive).call("drive.download", {"file_id": "f1"}))
        assert err.code == "payload_too_large"


class TestAdapterSheets:
    async def test_values_are_raw(self, tmp_path, audit_dir):
        drive = MagicMock()
        drive.get_sheet_values.return_value = [["a", 1], ["b", 2.5]]
        result = await Env(tmp_path, drive=drive).call(
            "sheets.get_values", {"spreadsheet_id": "s1", "range": "Sheet1!A1:B2", "value_render_option": "FORMULA"}
        )
        assert result["data"] == {"values": [["a", 1], ["b", 2.5]]}
        assert drive.get_sheet_values.call_args.args == ("s1", "Sheet1!A1:B2", "FORMULA")

    async def test_default_render_option(self, tmp_path, audit_dir):
        drive = MagicMock()
        drive.get_sheet_values.return_value = []
        await Env(tmp_path, drive=drive).call("sheets.get_values", {"spreadsheet_id": "s1", "range": "A1"})
        assert drive.get_sheet_values.call_args.args[2] == "FORMATTED_VALUE"

    @pytest.mark.parametrize("params", [
        {"range": "A1"}, {"spreadsheet_id": "s"}, {"spreadsheet_id": "s", "range": "A1", "value_render_option": "RAW"},
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
        assert audit_lines(audit_dir)[0]["summary"].startswith("9; bytes=")

    async def test_page_id_is_required(self, tmp_path, audit_dir):
        err = await _code(Env(tmp_path, confluence=MagicMock()).call("confluence.get_page", {}))
        assert err.code == "invalid_params"


class TestAdapterCalendar:
    async def test_events_are_normalized(self, tmp_path, audit_dir):
        calendar = MagicMock()
        calendar.list_events.return_value = [_event()]
        params = {"time_min": "2026-01-01T00:00:00Z", "time_max": "2026-01-02T00:00:00Z"}
        result = await Env(tmp_path, calendar=calendar).call("calendar.list_events", params)
        assert result["data"][0]["title"] == "Standup"
        assert calendar.list_events.call_args.args == ("primary", 250, params["time_min"], params["time_max"])
        assert audit_lines(audit_dir)[0]["summary"].startswith(
            "primary; 2026-01-01T00:00:00Z; 2026-01-02T00:00:00Z; bytes="
        )

    async def test_explicit_calendar_and_limit(self, tmp_path, audit_dir):
        calendar = MagicMock()
        calendar.list_events.return_value = []
        await Env(tmp_path, calendar=calendar).call(
            "calendar.list_events", {"calendar_id": "team", "time_min": "a", "time_max": "b", "max_results": 3}
        )
        assert calendar.list_events.call_args.args == ("team", 3, "a", "b")

    async def test_non_json_values_are_serialized_as_strings(self, tmp_path, audit_dir):
        from datetime import datetime
        drive = MagicMock()
        drive.get_sheet_values.return_value = [[datetime(2026, 1, 1)]]
        result = await Env(tmp_path, drive=drive).call("sheets.get_values", {"spreadsheet_id": "s", "range": "A1"})
        assert result["data"]["values"] == [["2026-01-01 00:00:00"]]

    @pytest.mark.parametrize("params", [{"time_min": "a"}, {"time_max": "b"}, {"time_min": "a", "time_max": "b", "max_results": 251}])
    async def test_invalid_params(self, tmp_path, audit_dir, params):
        err = await _code(Env(tmp_path, calendar=MagicMock()).call("calendar.list_events", params))
        assert err.code == "invalid_params"


def test_every_operation_has_an_adapter():
    assert set(SOURCE_ADAPTERS) == set(SOURCE_OPERATIONS)
