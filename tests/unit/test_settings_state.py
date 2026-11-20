"""privacyfence.settings_state -- the helpers the settings page and its routes share: the
main-thread dispatcher seam, ``_run_async``, the pure state builders and the Auto-accept row
renderers."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest


from privacyfence import resource_names
from privacyfence import settings_state as ss
from privacyfence.policy import propose as policy_propose
from privacyfence.policy import scope_rows
from privacyfence.resource_names import get_resolver


def wait_until(predicate, timeout=2.0, interval=0.005) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class TestRunAsyncMarshaling:
    """_run_async is the mechanism every threaded flow in this module funnels
    through. If it ever regresses to invoking on_done directly on the worker
    thread, the "on_done touches self/the page, so it must run on the main
    thread" invariant every mutation depends on breaks silently."""

    def test_success_result_never_delivered_directly_on_worker_thread(self, monkeypatch):
        recorded = []
        ss.set_main_dispatcher(lambda f, *a, **k: recorded.append((f, a, k)))

        done_calls = []
        work_thread = {}

        def work():
            work_thread["thread"] = threading.current_thread()
            return "alice@example.com"

        def done(ok, result):
            done_calls.append((ok, result))

        try:
            ss._run_async(work, done)

            assert wait_until(lambda: recorded)
            assert work_thread["thread"] is not threading.current_thread()
            assert done_calls == []

            func, args, kwargs = recorded[0]
            assert args == (True, "alice@example.com")
            func(*args, **kwargs)
            assert done_calls == [(True, "alice@example.com")]
        finally:
            ss.set_main_dispatcher(None)

    def test_exception_in_work_is_also_marshaled_not_raised_on_worker_thread(self, monkeypatch):
        recorded = []
        ss.set_main_dispatcher(lambda f, *a, **k: recorded.append((f, a, k)))
        boom = ValueError("auth failed")

        def work():
            raise boom

        done_calls = []
        try:
            ss._run_async(work, lambda ok, result: done_calls.append((ok, result)))

            assert wait_until(lambda: recorded)
            func, args, kwargs = recorded[0]
            assert args == (False, boom)
            func(*args, **kwargs)
            assert done_calls == [(False, boom)]
        finally:
            ss.set_main_dispatcher(None)


class TestCallOnMain:
    """The dispatcher seam: call_on_main resolves to a registered
    set_main_dispatcher(), else runs inline -- the inline case is what makes
    _run_async's on_done actually observable when nothing has attached a
    dispatcher yet (a standalone import, or a test with no web server
    running), instead of dying with AttributeError inside the worker thread
    where nothing surfaces it (the exact bug this seam fixes -- see this
    module's git history)."""

    def test_dispatcher_registered_is_used(self, monkeypatch):
        recorded = []
        ss.set_main_dispatcher(lambda f, *a: recorded.append((f, a)))
        try:
            calls = []
            ss.call_on_main(lambda x: calls.append(x), "hi")
            assert calls == []
            func, args = recorded[0]
            func(*args)
            assert calls == ["hi"]
        finally:
            ss.set_main_dispatcher(None)

    def test_no_dispatcher_runs_inline(self, monkeypatch):
        # This is the case that used to raise AttributeError against a None
        # AppHelper -- authenticate_connector's failure path (and every
        # other _run_async caller) has to surface an error when nothing has
        # attached a dispatcher, not silently die on the worker thread.
        ss.set_main_dispatcher(None)
        calls = []
        ss.call_on_main(lambda x: calls.append(x), "hi")
        assert calls == ["hi"]

    def test_run_async_on_done_runs_with_no_dispatcher_registered(self, monkeypatch):
        ss.set_main_dispatcher(None)
        done_calls = []
        boom = RuntimeError("auth failed")

        def work():
            raise boom

        ss._run_async(work, lambda ok, result: done_calls.append((ok, result)))
        assert wait_until(lambda: done_calls)
        assert done_calls == [(False, boom)]


class TestExtractDriveId:
    def test_bare_id_is_accepted_as_is(self):
        assert ss._extract_drive_id("FOLDER1") == "FOLDER1"

    def test_folder_url_extracts_the_id(self):
        url = "https://drive.google.com/drive/folders/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms?usp=sharing"
        assert ss._extract_drive_id(url) == "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

    def test_file_url_extracts_the_id(self):
        url = "https://docs.google.com/spreadsheets/d/1AbCdEf12345/edit#gid=0"
        assert ss._extract_drive_id(url) == "1AbCdEf12345"

    def test_unparseable_text_returns_empty_string(self):
        assert ss._extract_drive_id("not a url or id, has spaces") == ""


class TestShortId:
    def test_short_id_passes_through_unchanged(self):
        assert ss._short_id("F1") == "F1"

    def test_long_id_is_truncated_with_ellipsis(self):
        long_id = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        result = ss._short_id(long_id)
        assert result.startswith("1BxiMVs0")
        assert result.endswith("E2upms")
        assert "…" in result


class TestParseValueList:
    def test_splits_on_comma(self):
        assert ss._parse_value_list("a.com, b.com") == ["a.com", "b.com"]

    def test_empty_text_is_none(self):
        assert ss._parse_value_list("   ") is None

    def test_blank_entries_are_dropped(self):
        assert ss._parse_value_list("a.com, , b.com,") == ["a.com", "b.com"]


class TestPolicyScopeCatalogue:
    """The Auto-accept page's "add a rule" scope picker -- one entry
    per policy.propose.SCOPES_BY_GROUP widening group, plus ss._POLICY_EXTRA_SCOPES."""

    def test_covers_every_propose_group_and_every_extra(self):
        ids = {entry["id"] for entry in ss._policy_scope_catalogue()}
        assert set(policy_propose.SCOPES_BY_GROUP) <= ids
        assert set(ss._POLICY_EXTRA_SCOPES) <= ids

    def test_apps_script_entry_needs_a_value_and_offers_read_and_update(self):
        entry = next(e for e in ss._policy_scope_catalogue() if e["id"] == "apps_script.project")
        assert entry["needs_value"] is True
        assert set(entry["verbs"]) == {"read", "update"}
        assert entry["connector"] == "apps_script"

    def test_gmail_and_slack_unconditional_extras_need_no_value(self):
        by_id = {e["id"]: e for e in ss._policy_scope_catalogue()}
        assert by_id["gmail.filters"]["needs_value"] is False
        assert by_id["gmail.filters"]["verbs"] == ["restructure"]
        assert by_id["slack.group_chats"]["needs_value"] is False
        assert by_id["slack.group_chats"]["verbs"] == ["send"]

    def test_drive_folder_entry_unions_verbs_across_every_predicate_in_the_group(self):
        entry = next(e for e in ss._policy_scope_catalogue() if e["id"] == "drive.folder")
        # approved_folder (read/download) + approved_sandbox_folder (update/restructure/delete)
        # + parent_folder_allowlist (add) + move_within_approved_folders (move).
        assert set(entry["verbs"]) == {
            "read", "download", "update", "restructure", "delete", "add", "move",
        }


class _DriveFailing:
    """A Drive client whose every metadata lookup fails with the given HTTP status."""

    def __init__(self, status: int) -> None:
        self.status = status

    def get_file_metadata(self, file_id: str):
        error = RuntimeError(f"HTTP {self.status}")
        error.resp = SimpleNamespace(status=self.status)
        raise error


@pytest.fixture(autouse=True)
def _isolated_name_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(resource_names, "_cache_file", lambda: tmp_path / "resource_name_cache.json")


class TestScopeRowHelpers:
    FOLDER = "2RollingCashflowFolder"

    def _v2_cfg(self):
        return {"auto_accept": {"version": 2, "rules": [
            {"id": "r-narrow", "predicate": "approved_folder", "value": [self.FOLDER],
             "operations": ["drive.download_file"], "conditions": []},
            {"id": "r-flagged", "predicate": "approved_folder", "value": [self.FOLDER],
             "operations": ["drive.read_file_contents", "drive.download_file"], "conditions": [],
             "allow_personal_data": True},
        ]}}

    def test_usage_sums_former_ids(self):
        usage = {
            "r-narrow": {"count": 2, "last_matched": "2026-07-06T12:00:00+00:00"},
            "r-flagged": {"count": 3, "last_matched": "2026-07-08T12:00:00+00:00"},
            "r-unrelated": {"count": 9, "last_matched": "2026-07-09T12:00:00+00:00"},
        }
        rows = scope_rows.rows_from_config(self._v2_cfg())
        state = ss._auto_accept_state_from_rows(
            rows, usage, resolve_value=lambda row: "v", resolver=get_resolver(),
        )
        (row,) = state["rules"]
        assert row["match_count"] == 5
        assert row["never_matched"] is False
        assert row["last_matched"] == ss._relative_time("2026-07-08T12:00:00+00:00")

    def test_an_id_a_live_lookup_could_not_find_is_invalid(self):
        (scope_row,) = scope_rows.rows_from_config(self._v2_cfg())
        rt = ss.RULE_NAME_TO_RESOURCE_TYPE["approved_folder"]
        resolver = get_resolver()
        gone = scope_row.value[0]
        resolver.resolve(rt, gone, _DriveFailing(status=404))
        (item, *_) = ss.row_value_items(scope_row, resolver)
        assert item["id"] == gone and item["status"] == "invalid"

    def test_an_id_whose_lookup_failed_is_not_invalid(self):
        (scope_row,) = scope_rows.rows_from_config(self._v2_cfg())
        rt = ss.RULE_NAME_TO_RESOURCE_TYPE["approved_folder"]
        resolver = get_resolver()
        resolver.resolve(rt, scope_row.value[0], _DriveFailing(status=429))
        (item, *_) = ss.row_value_items(scope_row, resolver)
        assert item["status"] == "pending"

    def test_a_row_that_compiles_to_nothing_lists_its_values_as_plain(self):
        row = scope_rows.ScopeRow(
            id="r-empty", scope="drive.folder", value=["a", "b"], verbs=frozenset(), operations=frozenset(),
        )
        assert [(i["id"], i["status"]) for i in ss.row_value_items(row, get_resolver())] == [
            ("a", "plain"), ("b", "plain"),
        ]
        assert ss.sentence_parts(scope_rows.ScopeRow(
            id="r-none", scope="drive.folder", value=None, verbs=frozenset(), operations=frozenset(),
        )) is None


class TestOrgRuleRows:
    def test_rows_have_no_usage_fields_and_keep_the_rest(self):
        rows = scope_rows.rows_from_config(TestScopeRowHelpers()._v2_cfg())
        result = ss.org_rule_rows(rows, resolve_value=lambda row: "v", resolver=get_resolver())
        assert len(result) == len(rows)
        for entry in result:
            assert not {"match_count", "last_matched", "never_matched"} & set(entry)
            assert entry["value"] == "v" and entry["sentence"] and entry["verbs"] is not None
            assert "value_items" in entry

    def test_no_rows_is_an_empty_list(self):
        assert ss.org_rule_rows([], resolve_value=lambda row: "", resolver=get_resolver()) == []


class TestRelativeTime:
    def _ago(self, **delta):
        from datetime import datetime, timedelta, timezone

        return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()

    def test_buckets(self):
        assert ss._relative_time(self._ago(seconds=5)) == "just now"
        assert ss._relative_time(self._ago(minutes=5, seconds=1)) == "5m ago"
        assert ss._relative_time(self._ago(hours=3, seconds=1)) == "3h ago"
        assert ss._relative_time(self._ago(days=2, seconds=1)) == "2d ago"

    def test_naive_timestamp_is_read_as_utc(self):
        naive = self._ago(minutes=5, seconds=1).split("+")[0]
        assert ss._relative_time(naive) == "5m ago"

    def test_unparseable_timestamp_is_empty(self):
        assert ss._relative_time("not a time") == ""


class TestCachedRowValue:
    def test_row_that_compiles_to_nothing_shows_its_raw_values(self):
        row = scope_rows.ScopeRow(
            id="r", scope="drive.folder", value=["a", "b"], verbs=frozenset(), operations=frozenset(),
        )
        assert ss.cached_row_value(row, get_resolver()) == "a, b"

    def test_row_with_a_rule_shows_the_short_id_until_a_name_is_cached(self):
        long_id = "1" * 40
        cfg = {"auto_accept": {"version": 2, "rules": [
            {"id": "r1", "predicate": "approved_folder", "value": [long_id],
             "operations": ["drive.download_file"], "conditions": []},
        ]}}
        (row,) = scope_rows.rows_from_config(cfg)
        assert ss.cached_row_value(row, get_resolver()) == ss._short_id(long_id)


class TestAuditRows:
    def test_unknown_agent_source_reads_as_no_signal(self):
        from privacyfence.agent_identity import AgentSource
        from privacyfence.audit_log import AuditEntry

        entry = AuditEntry(
            timestamp="2026-01-01T00:00:00+00:00", week="2026-W01", request_id="r1",
            connector="gmail", tool="gmail_list_messages", tool_name="List messages",
            summary="s", sender="a@example.com", decision="allowed", auto_accept_rule="",
            latency_seconds=0.0, pii_detected=False, agent_source="from-the-future",
        )
        assert ss._entry_agent(entry).source is AgentSource.NONE
        (row,) = ss.audit_rows([entry])
        assert row["connector"] == "Gmail"
        assert row["decision"] == "allowed"
        assert row["tool"] == "List messages"


class TestSmallBuilders:
    def test_connector_label_uses_the_override_else_capitalises(self):
        assert ss.connector_label("apps_script") == "Apps Script"
        assert ss.connector_label("gmail") == "Gmail"

    def test_google_client_config_needs_both_id_and_secret(self):
        assert ss._google_client_config({}) == {}
        assert ss._google_client_config({"google": {"client_id": "i"}}) == {}
        google = {"client_id": "i", "client_secret": "s"}
        assert ss._google_client_config({"google": google}) == {"installed": google}

    def test_pii_general_fields_default_to_on(self):
        assert ss._pii_general_fields({}) == {"pii_enabled": True, "pii_ip": True, "pii_financial": True}
        cfg = {"pii_detection": {"enabled": False, "detect_ip_addresses": False}}
        assert ss._pii_general_fields(cfg) == {"pii_enabled": False, "pii_ip": False, "pii_financial": True}

    def test_about_state_dict_names_the_repo_and_license(self):
        about = ss._about_state_dict()
        assert about["repo_url"] == ss.REPO_URL and about["license"] == ss.LICENSE_NAME


class TestPrivacyStateFromConfig:
    def test_absent_groups_take_the_fail_safe_default(self):
        allow = ss._privacy_state_from_config({})
        block = ss._privacy_state_from_config({}, fail_safe_default="block")
        assert set(allow["default_policy"]) == set(ss.PRIVACY_GROUP_LABELS)
        assert set(allow["default_policy"].values()) == {"allow"}
        assert set(block["default_policy"].values()) == {"block"}
        assert allow["calendar_free_busy"] is True and allow["gmail_append_signature"] is False

    def test_malformed_group_renders_as_allow_instead_of_raising(self, caplog):
        state = ss._privacy_state_from_config({"privacy": {"default_policy": "bogus-policy"}}, fail_safe_default="block")
        assert state["default_policy"]["privacy"] == "allow"
        assert state["default_policy"]["slack_privacy"] == "block"
        assert "Could not render current privacy settings" in caplog.text


class TestRuleUsageMap:
    def test_no_audit_directory_is_an_empty_map(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ss, "data_dir", lambda: tmp_path)
        assert ss._rule_usage_map() == {}


class TestCachedRuleValue:
    def _rule(self, predicate, value):
        from privacyfence.policy.engine import PolicyRule

        return PolicyRule(id="x", predicate=predicate, value=value, operations=frozenset(), conditions=())

    def test_opaque_ids_show_the_short_id_when_no_name_is_cached(self):
        long_id = "1" * 40
        rule = self._rule("approved_sandbox_folder", [long_id])
        assert ss.cached_rule_value(rule, get_resolver()) == ss._short_id(long_id)

    def test_non_resource_values_are_joined_as_is(self):
        rule = self._rule("sender_domain", ["a.com", "b.com"])
        assert ss.cached_rule_value(rule, get_resolver()) == "a.com, b.com"


