"""Tests for SalesforceClient's refresh-and-retry session logic and result
normalization. The refresh-on-expired-session behavior (_call/_try_refresh)
is the most bug-prone part of this client -- it's what keeps a long-running
daemon from forcing re-authentication every time a session token expires --
so it gets the deepest coverage here.

``authorize_interactive`` (the browser-loopback Web Server + PKCE flow) is tested by
mocking ``run_browser_oauth`` -- the ``oauth_loopback`` module boundary --
rather than mocking ``authorize_interactive`` itself or skipping straight to
a canned token response. The fake ``run_browser_oauth`` invokes the real
``exchange`` closure it was given, so the code-exchange HTTP call and the
``SalesforceClientError`` wrapping around a failed exchange (lines ~122-139)
are exercised for real, with only ``requests.post`` mocked underneath.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import MagicMock

import sys

import pytest
import requests

from privacyfence.oauth_loopback import OAuthLoopbackError
from privacyfence.salesforce_client import (
    DEFAULT_REPORT_MAX_PAGES,
    MAX_REPORT_FILTERS,
    REPORT_PAGING_REASONS,
    ReportFilter,
    ReportPage,
    ReportPagingError,
    SalesforceClient,
    SalesforceClientError,
    SalesforceRecord,
    SalesforceReport,
    _escape_sosl_term,
    _is_expired_session_error,
    _validate_object_type_name,
    _validate_salesforce_id,
    authorize_interactive,
    build_authorize_url,
    build_keyset_metadata,
    build_report_metadata,
    exchange_code,
    _oauth_error_detail,
    load_token_file,
    save_token_file,
    report_page_info,
    report_page_keys,
)

LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "salesforce"
REPORT_ID = "00O5e000004AbCdEAK"
ACCOUNT_ID = "001xx000003DGb2AAG"


def make_client(config: dict | None = None, token_file: str | None = None) -> SalesforceClient:
    base_config = {"access_token": "tok", "instance_url": "https://my.salesforce.com"}
    base_config.update(config or {})
    return SalesforceClient(config=base_config, token_file=token_file)


def with_fake_sf(client: SalesforceClient, sf: MagicMock) -> SalesforceClient:
    client._get_sf = lambda: sf
    return client


# ---------------------------------------------------------------------------- #
# load_token_file
# ---------------------------------------------------------------------------- #

class TestLoadTokenFile:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(SalesforceClientError, match="No Salesforce token found"):
            load_token_file(str(tmp_path / "nope.json"))

    def test_loads_valid_json(self, tmp_path):
        path = tmp_path / "token.json"
        path.write_text('{"access_token": "t", "instance_url": "https://x.com"}')
        assert load_token_file(str(path)) == {"access_token": "t", "instance_url": "https://x.com"}


# ---------------------------------------------------------------------------- #
# authorize_interactive: browser-loopback Web Server + PKCE flow.
# ``run_browser_oauth`` (the oauth_loopback module boundary) is mocked; the
# fake implementation invokes the real ``exchange``/``build_authorize_url``
# closures it receives so the code-exchange HTTP call and error wrapping run
# for real, with only ``requests.post`` mocked below that.
# ---------------------------------------------------------------------------- #

def _invoke_exchange(build_authorize_url, exchange, port, path, redirect_host):
    """Fake run_browser_oauth: skip the real browser/HTTP server, and just
    call the exchange closure with a fake authorization code -- this is what
    drives the exchange()'s own requests.post + error-wrapping logic."""
    redirect_uri = f"http://{redirect_host}:{port}{path}"
    return exchange("auth-code-123", redirect_uri, "code-verifier-abc")


class TestAuthorizeInteractive:
    def test_code_exchange_http_failure_becomes_salesforce_client_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr("privacyfence.salesforce_client.run_browser_oauth", _invoke_exchange)
        def raise_it(*a, **kw):
            raise requests.RequestException("network error")
        monkeypatch.setattr("requests.post", raise_it)

        with pytest.raises(SalesforceClientError, match="Salesforce OAuth exchange failed: network error"):
            authorize_interactive("ck", "cs", str(tmp_path / "token.json"))

    def test_code_exchange_http_error_status_becomes_salesforce_client_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr("privacyfence.salesforce_client.run_browser_oauth", _invoke_exchange)
        response = MagicMock()
        response.raise_for_status.side_effect = requests.HTTPError("400 Client Error: invalid_grant")
        monkeypatch.setattr("requests.post", lambda *a, **kw: response)

        with pytest.raises(SalesforceClientError, match="Salesforce OAuth exchange failed.*invalid_grant"):
            authorize_interactive("ck", "cs", str(tmp_path / "token.json"))

    def test_loopback_failure_becomes_salesforce_client_error(self, monkeypatch, tmp_path):
        def raiser(*a, **kw):
            raise OAuthLoopbackError("timed out waiting for sign-in")
        monkeypatch.setattr("privacyfence.salesforce_client.run_browser_oauth", raiser)

        with pytest.raises(SalesforceClientError, match="Salesforce sign-in failed.*timed out"):
            authorize_interactive("ck", "cs", str(tmp_path / "token.json"))

    def test_response_missing_access_token_raises(self, monkeypatch, tmp_path):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"instance_url": "https://my.salesforce.com"}  # no access_token
        monkeypatch.setattr("requests.post", lambda *a, **kw: response)
        monkeypatch.setattr("privacyfence.salesforce_client.run_browser_oauth", _invoke_exchange)

        with pytest.raises(SalesforceClientError, match="did not return a usable token"):
            authorize_interactive("ck", "cs", str(tmp_path / "token.json"))

    def test_response_missing_instance_url_raises(self, monkeypatch, tmp_path):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"access_token": "tok"}  # no instance_url
        monkeypatch.setattr("requests.post", lambda *a, **kw: response)
        monkeypatch.setattr("privacyfence.salesforce_client.run_browser_oauth", _invoke_exchange)

        with pytest.raises(SalesforceClientError, match="did not return a usable token"):
            authorize_interactive("ck", "cs", str(tmp_path / "token.json"))

    def test_exchange_posts_expected_params_to_login_url(self, monkeypatch, tmp_path):
        captured = {}
        def fake_post(url, data=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            captured["timeout"] = timeout
            response = MagicMock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"access_token": "tok", "instance_url": "https://my.salesforce.com"}
            return response
        monkeypatch.setattr("requests.post", fake_post)
        monkeypatch.setattr("privacyfence.salesforce_client.run_browser_oauth", _invoke_exchange)

        authorize_interactive("ck", "cs", str(tmp_path / "token.json"), login_url="https://test.salesforce.com/")

        assert captured["url"] == "https://test.salesforce.com/services/oauth2/token"
        assert captured["data"]["grant_type"] == "authorization_code"
        assert captured["data"]["code"] == "auth-code-123"
        assert captured["data"]["client_id"] == "ck"
        assert captured["data"]["client_secret"] == "cs"
        assert captured["data"]["code_verifier"] == "code-verifier-abc"
        assert captured["data"]["redirect_uri"] == "http://localhost:53683/callback"

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_successful_flow_saves_token_with_restricted_permissions(self, monkeypatch, tmp_path):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "access_token": "tok", "refresh_token": "rt", "instance_url": "https://my.salesforce.com",
        }
        monkeypatch.setattr("requests.post", lambda *a, **kw: response)
        monkeypatch.setattr("privacyfence.salesforce_client.run_browser_oauth", _invoke_exchange)
        token_file = tmp_path / "nested" / "token.json"

        result = authorize_interactive("ck", "cs", str(token_file))

        assert result == {"access_token": "tok", "refresh_token": "rt", "instance_url": "https://my.salesforce.com"}
        saved = json.loads(token_file.read_text(encoding="utf-8"))
        assert saved == result
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    def test_authorize_url_includes_pkce_challenge_and_scopes(self, monkeypatch, tmp_path):
        captured = {}
        def fake_run_browser_oauth(build_authorize_url, exchange, port, path, redirect_host):
            captured["url"] = build_authorize_url("http://localhost:53683/callback", "state-xyz", "challenge-xyz")
            return {"access_token": "tok", "instance_url": "https://my.salesforce.com"}
        monkeypatch.setattr("privacyfence.salesforce_client.run_browser_oauth", fake_run_browser_oauth)

        authorize_interactive("my-client-id", "cs", str(tmp_path / "token.json"))

        url = captured["url"]
        assert url.startswith("https://login.salesforce.com/services/oauth2/authorize?")
        assert "client_id=my-client-id" in url
        assert "code_challenge=challenge-xyz" in url
        assert "code_challenge_method=S256" in url
        assert "state=state-xyz" in url
        assert "scope=api+refresh_token" in url


# ---------------------------------------------------------------------------- #
# build_authorize_url / exchange_code -- the hoisted functions authorize_
# interactive itself now delegates to, called directly here rather than through run_browser_oauth's
# local listener -- this is the shape web/routes_connect.py's org-mode
# server-redirect flow calls them in.
# ---------------------------------------------------------------------------- #

class TestBuildAuthorizeUrlAndExchangeCode:
    def test_build_authorize_url_matches_authorize_interactives_own_shape(self):
        url = build_authorize_url("my-client-id", "https://pf.example.com/oauth/callback/salesforce", "state-xyz", "challenge-xyz")
        assert url.startswith("https://login.salesforce.com/services/oauth2/authorize?")
        assert "client_id=my-client-id" in url
        assert "redirect_uri=https%3A%2F%2Fpf.example.com%2Foauth%2Fcallback%2Fsalesforce" in url
        assert "code_challenge=challenge-xyz" in url
        assert "state=state-xyz" in url

    def test_build_authorize_url_honors_a_custom_login_url(self):
        url = build_authorize_url("cid", "https://pf.example.com/cb", "s", "c", login_url="https://test.salesforce.com/")
        assert url.startswith("https://test.salesforce.com/services/oauth2/authorize?")

    def test_exchange_code_returns_the_normalized_record_without_saving(self, monkeypatch, tmp_path):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"access_token": "tok", "refresh_token": "rt", "instance_url": "https://my.salesforce.com"}
        monkeypatch.setattr("requests.post", lambda *a, **kw: response)

        record = exchange_code("cid", "csec", "auth-code", "https://pf.example.com/cb", "verifier-abc")

        assert record == {"access_token": "tok", "refresh_token": "rt", "instance_url": "https://my.salesforce.com"}
        assert not (tmp_path / "token.json").exists()  # exchange_code never touches disk

    def test_exchange_code_missing_access_token_raises(self, monkeypatch):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"instance_url": "https://my.salesforce.com"}
        monkeypatch.setattr("requests.post", lambda *a, **kw: response)

        with pytest.raises(SalesforceClientError, match="did not return a usable token"):
            exchange_code("cid", "csec", "code", "https://pf.example.com/cb", "verifier")


# ---------------------------------------------------------------------------- #
# _is_expired_session_error
# ---------------------------------------------------------------------------- #

class TestIsExpiredSessionError:
    def test_invalid_session_id_detected(self):
        assert _is_expired_session_error(Exception("INVALID_SESSION_ID: Session expired or invalid"))

    def test_session_expired_text_detected(self):
        assert _is_expired_session_error(Exception("Session expired"))

    def test_unrelated_error_not_detected(self):
        assert not _is_expired_session_error(Exception("MALFORMED_QUERY"))


# ---------------------------------------------------------------------------- #
# _build_sf: config validation + instance URL normalization
# ---------------------------------------------------------------------------- #

class TestBuildSf:
    def test_missing_access_token_raises_not_authenticated(self):
        client = SalesforceClient(config={"instance_url": "https://x.com"})
        with pytest.raises(SalesforceClientError, match="not authenticated"):
            client._build_sf()

    def test_missing_instance_url_raises_not_authenticated(self):
        client = SalesforceClient(config={"access_token": "t"})
        with pytest.raises(SalesforceClientError, match="not authenticated"):
            client._build_sf()

    def test_instance_url_scheme_stripped_before_passing_to_salesforce(self, monkeypatch):
        captured = {}
        class FakeSalesforce:
            def __init__(self, **kwargs):
                captured.update(kwargs)
        monkeypatch.setattr("simple_salesforce.Salesforce", FakeSalesforce)

        client = make_client({"instance_url": "https://my.salesforce.com/"})
        client._build_sf()

        assert captured == {"instance": "my.salesforce.com", "session_id": "tok"}

    def test_salesforce_constructor_error_becomes_client_error(self, monkeypatch):
        class FakeSalesforce:
            def __init__(self, **kwargs):
                raise RuntimeError("bad session")
        monkeypatch.setattr("simple_salesforce.Salesforce", FakeSalesforce)

        client = make_client()
        with pytest.raises(SalesforceClientError, match="Salesforce authentication failed"):
            client._build_sf()


# ---------------------------------------------------------------------------- #
# _try_refresh
# ---------------------------------------------------------------------------- #

class TestTryRefresh:
    def test_missing_refresh_token_returns_false_without_http_call(self, monkeypatch):
        called = []
        monkeypatch.setattr("requests.post", lambda *a, **kw: called.append(1))
        client = make_client({"consumer_key": "ck", "consumer_secret": "cs"})
        assert client._try_refresh() is False
        assert called == []

    def test_missing_consumer_credentials_returns_false(self):
        client = make_client({"refresh_token": "rt"})
        assert client._try_refresh() is False

    def test_successful_refresh_updates_config_and_clears_cached_sf(self, monkeypatch):
        response = MagicMock()
        response.json.return_value = {"access_token": "new-tok", "instance_url": "https://new.salesforce.com"}
        monkeypatch.setattr("requests.post", lambda *a, **kw: response)

        client = make_client({"refresh_token": "rt", "consumer_key": "ck", "consumer_secret": "cs"})
        client._sf = "stale-sf-object"

        assert client._try_refresh() is True
        assert client._config["access_token"] == "new-tok"
        assert client._config["instance_url"] == "https://new.salesforce.com"
        assert client._sf is None

    def test_successful_refresh_persists_to_token_file_when_given(self, monkeypatch, tmp_path):
        response = MagicMock()
        response.json.return_value = {"access_token": "new-tok", "instance_url": "https://new.salesforce.com"}
        monkeypatch.setattr("requests.post", lambda *a, **kw: response)

        token_file = str(tmp_path / "token.json")
        client = make_client(
            {"refresh_token": "rt", "consumer_key": "ck", "consumer_secret": "cs"}, token_file=token_file,
        )

        client._try_refresh()

        saved = load_token_file(token_file)
        assert saved == {"access_token": "new-tok", "refresh_token": "rt", "instance_url": "https://new.salesforce.com"}

    def test_rotated_refresh_token_is_kept_and_persisted(self, monkeypatch, tmp_path):
        response = MagicMock()
        response.json.return_value = {
            "access_token": "new-tok", "refresh_token": "rt-2", "instance_url": "https://new.salesforce.com",
        }
        sent = []
        monkeypatch.setattr("requests.post", lambda url, data, **kw: sent.append(data["refresh_token"]) or response)

        token_file = str(tmp_path / "token.json")
        client = make_client(
            {"refresh_token": "rt", "consumer_key": "ck", "consumer_secret": "cs"}, token_file=token_file,
        )

        client._try_refresh()
        client._try_refresh()

        assert sent == ["rt", "rt-2"]
        assert load_token_file(token_file)["refresh_token"] == "rt-2"

    def test_http_error_logs_oauth_error_description(self, monkeypatch, caplog):
        import requests
        error_response = MagicMock()
        error_response.json.return_value = {
            "error": "invalid_grant", "error_description": "expired access/refresh token",
        }
        def raise_it(*a, **kw):
            raise requests.HTTPError("400 Client Error: Bad Request", response=error_response)
        monkeypatch.setattr("requests.post", raise_it)

        client = make_client({"refresh_token": "rt", "consumer_key": "ck", "consumer_secret": "cs"})
        with caplog.at_level("WARNING", logger="privacyfence.salesforce_client"):
            assert client._try_refresh() is False

        assert "(invalid_grant: expired access/refresh token)" in caplog.text

    def test_http_failure_returns_false(self, monkeypatch):
        import requests
        def raise_it(*a, **kw):
            raise requests.RequestException("network error")
        monkeypatch.setattr("requests.post", raise_it)

        client = make_client({"refresh_token": "rt", "consumer_key": "ck", "consumer_secret": "cs"})
        assert client._try_refresh() is False

    def test_login_url_defaults_when_absent(self, monkeypatch):
        captured_urls = []
        response = MagicMock()
        response.json.return_value = {"access_token": "t", "instance_url": "https://x.com"}
        def fake_post(url, **kw):
            captured_urls.append(url)
            return response
        monkeypatch.setattr("requests.post", fake_post)

        client = make_client({"refresh_token": "rt", "consumer_key": "ck", "consumer_secret": "cs"})
        client._try_refresh()

        assert captured_urls[0] == "https://login.salesforce.com/services/oauth2/token"


# ---------------------------------------------------------------------------- #
# _call: the refresh-and-retry wrapper
# ---------------------------------------------------------------------------- #

class TestCall:
    def test_happy_path_returns_fn_result(self):
        client = with_fake_sf(make_client(), MagicMock())
        result = client._call(lambda sf: "ok")
        assert result == "ok"

    def test_salesforce_client_error_propagates_without_retry(self):
        client = with_fake_sf(make_client(), MagicMock())
        def raiser(sf):
            raise SalesforceClientError("already a client error")
        with pytest.raises(SalesforceClientError, match="already a client error"):
            client._call(raiser)

    def test_non_expired_error_wraps_without_attempting_refresh(self, monkeypatch):
        refresh_called = []
        client = with_fake_sf(make_client(), MagicMock())
        monkeypatch.setattr(client, "_try_refresh", lambda: refresh_called.append(1) or True)

        def raiser(sf):
            raise RuntimeError("MALFORMED_QUERY: bad SOQL")

        with pytest.raises(SalesforceClientError, match="MALFORMED_QUERY"):
            client._call(raiser)
        assert refresh_called == []

    def test_expired_session_triggers_refresh_and_retry_succeeds(self, monkeypatch):
        client = with_fake_sf(make_client(), MagicMock())
        monkeypatch.setattr(client, "_try_refresh", lambda: True)

        calls = {"n": 0}
        def fn(sf):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("INVALID_SESSION_ID")
            return "retried-ok"

        result = client._call(fn)
        assert result == "retried-ok"
        assert calls["n"] == 2

    def test_expired_session_but_refresh_fails_reraises_original_wrapped(self, monkeypatch):
        client = with_fake_sf(make_client(), MagicMock())
        monkeypatch.setattr(client, "_try_refresh", lambda: False)

        def raiser(sf):
            raise RuntimeError("INVALID_SESSION_ID")

        with pytest.raises(SalesforceClientError, match="INVALID_SESSION_ID"):
            client._call(raiser)

    def test_expired_session_refresh_succeeds_but_retry_also_fails(self, monkeypatch):
        client = with_fake_sf(make_client(), MagicMock())
        monkeypatch.setattr(client, "_try_refresh", lambda: True)

        def fn(sf):
            raise RuntimeError("INVALID_SESSION_ID")

        with pytest.raises(SalesforceClientError, match="INVALID_SESSION_ID"):
            client._call(fn)


# ---------------------------------------------------------------------------- #
# check_connection / list_reports / get_record / run_report
# ---------------------------------------------------------------------------- #

class TestCheckConnection:
    def test_returns_org_name_from_query(self):
        sf = MagicMock()
        sf.query.return_value = {"records": [{"Name": "Acme Corp"}]}
        client = with_fake_sf(make_client(), sf)
        assert client.check_connection() == "Acme Corp"

    def test_no_records_returns_unknown(self):
        sf = MagicMock()
        sf.query.return_value = {"records": []}
        client = with_fake_sf(make_client(), sf)
        assert client.check_connection() == "unknown"


class TestListReports:
    def test_maps_query_results(self):
        sf = MagicMock()
        sf.query.return_value = {"records": [
            {"Id": "r1", "Name": "Sales Report", "Description": "d", "FolderName": "f", "DeveloperName": "Sales_Report"},
        ]}
        client = with_fake_sf(make_client(), sf)

        reports = client.list_reports()

        assert reports == [SalesforceReport(
            id="r1", name="Sales Report", report_type="Sales_Report", folder_name="f", description="d",
        )]


class TestGetRecord:
    def test_requires_object_type_and_record_id(self):
        client = make_client()
        with pytest.raises(SalesforceClientError, match="requires object_type and record_id"):
            client.get_record("", "id1")
        with pytest.raises(SalesforceClientError, match="requires object_type and record_id"):
            client.get_record("Account", "")

    def test_fetches_record_and_strips_attributes_key(self):
        sf = MagicMock()
        sf.Account.get.return_value = {
            "attributes": {"type": "Account", "url": "/x"}, "Id": ACCOUNT_ID, "Name": "Acme",
        }
        client = with_fake_sf(make_client(), sf)

        record = client.get_record("Account", ACCOUNT_ID)

        assert record == SalesforceRecord(
            object_type="Account", id=ACCOUNT_ID, fields={"Id": ACCOUNT_ID, "Name": "Acme"},
        )
        sf.Account.get.assert_called_once_with(ACCOUNT_ID)

    @pytest.mark.parametrize("record_id", ["../../query?q=SELECT+Name+FROM+Contact", "001?x=1", "001"])
    def test_non_id_record_id_is_refused_before_any_request(self, record_id):
        sf = MagicMock()
        client = with_fake_sf(make_client(), sf)

        with pytest.raises(SalesforceClientError, match="record_id must be a 15- or 18-character"):
            client.get_record("Account", record_id)

        assert sf.mock_calls == []

    def test_non_identifier_object_type_is_refused_before_any_request(self):
        sf = MagicMock()
        client = with_fake_sf(make_client(), sf)

        with pytest.raises(SalesforceClientError, match="Invalid Salesforce object type name"):
            client.get_record("../query", ACCOUNT_ID)

        assert sf.mock_calls == []

    def test_unknown_object_type_raises_client_error(self):
        sf = MagicMock(spec=[])  # no attributes at all -> AttributeError on getattr
        client = with_fake_sf(make_client(), sf)
        with pytest.raises(SalesforceClientError, match="Unknown Salesforce object type"):
            client.get_record("NotAThing", ACCOUNT_ID)


class TestRunReport:
    def test_requires_report_id(self):
        client = make_client()
        with pytest.raises(SalesforceClientError, match="requires a report_id"):
            client.run_report("")

    def test_plain_run_is_a_get_with_include_details(self):
        sf = MagicMock()
        sf.restful.return_value = {"factMap": {}}
        client = with_fake_sf(make_client(), sf)

        result = client.run_report(REPORT_ID)

        assert result == {"factMap": {}}
        sf.restful.assert_called_once_with(f"analytics/reports/{REPORT_ID}", params={"includeDetails": "true"})

    def test_15_character_report_id_still_runs(self):
        sf = MagicMock()
        sf.restful.return_value = {"factMap": {}}
        client = with_fake_sf(make_client(), sf)

        client.run_report(REPORT_ID[:15])

        sf.restful.assert_called_once_with(
            f"analytics/reports/{REPORT_ID[:15]}", params={"includeDetails": "true"},
        )

    @pytest.mark.parametrize("report_id", [
        "../../query?q=SELECT Name,Email FROM Contact",
        f"{REPORT_ID}?includeDetails=false",
        f"{REPORT_ID}/../../query",
        "report-1",
    ])
    @pytest.mark.parametrize("narrowed", [False, True])
    def test_non_id_report_id_is_refused_before_any_request(self, report_id, narrowed):
        sf = MagicMock()
        client = with_fake_sf(make_client(), sf)
        kwargs = {"filters": [ReportFilter("A", "equals", ["x"])]} if narrowed else {}

        with pytest.raises(SalesforceClientError, match="report_id must be a 15- or 18-character"):
            client.run_report(report_id, **kwargs)

        assert sf.mock_calls == []

    def test_summary_only_sends_include_details_false(self):
        sf = MagicMock()
        sf.restful.return_value = {"factMap": {}}
        client = with_fake_sf(make_client(), sf)

        client.run_report(REPORT_ID, summary_only=True)

        sf.restful.assert_called_once_with(f"analytics/reports/{REPORT_ID}", params={"includeDetails": "false"})

    def test_overrides_describe_then_post(self):
        saved = {"detailColumns": ["A", "B"], "reportFilters": []}
        sf = MagicMock()
        sf.restful.side_effect = [{"reportMetadata": saved}, {"factMap": {}}]
        client = with_fake_sf(make_client(), sf)
        flt = ReportFilter("A", "equals", ["x"])

        result = client.run_report(REPORT_ID, columns=["B"], filters=[flt])

        assert result == {"factMap": {}}
        built = build_report_metadata(saved, ["B"], [flt])
        assert sf.restful.call_args_list == [
            ((f"analytics/reports/{REPORT_ID}/describe",), {}),
            (
                (f"analytics/reports/{REPORT_ID}",),
                {"params": {"includeDetails": "true"}, "method": "POST", "json": {"reportMetadata": built}},
            ),
        ]

    def test_non_dict_describe_is_treated_as_empty(self):
        sf = MagicMock()
        sf.restful.side_effect = [None, {"factMap": {}}]
        client = with_fake_sf(make_client(), sf)

        client.run_report(REPORT_ID, filters=[ReportFilter("A", "equals", ["x"])])

        posted = sf.restful.call_args_list[1].kwargs["json"]["reportMetadata"]
        assert posted == {
            "reportFilters": [{"column": "A", "operator": "equals", "value": "x"}],
            "reportBooleanFilter": "1",
        }

    def test_build_error_propagates_as_client_error(self):
        sf = MagicMock()
        sf.restful.return_value = {"reportMetadata": {"detailColumns": ["A"]}}
        client = with_fake_sf(make_client(), sf)

        with pytest.raises(SalesforceClientError, match="not one of this report's columns: A"):
            client.run_report(REPORT_ID, columns=["Z"])

        sf.restful.assert_called_once_with(f"analytics/reports/{REPORT_ID}/describe")


def _ids(n: int) -> list[str]:
    return [f"006{i:015d}" for i in range(n)]


class TestBuildReportMetadata:
    SAVED = {
        "detailColumns": ["A", "B", "C"],
        "reportFilters": [
            {"column": "X", "operator": "equals", "value": "1"},
            {"column": "Y", "operator": "equals", "value": "2"},
        ],
    }

    def test_column_subset_and_order(self):
        built = build_report_metadata(self.SAVED, [" C ", "A"], None)
        assert built["detailColumns"] == ["C", "A"]

    def test_unknown_column_lists_saved_columns(self):
        with pytest.raises(SalesforceClientError, match="'Z' is not one of this report's columns: A, B, C"):
            build_report_metadata(self.SAVED, ["Z"], None)

    def test_saved_without_columns_rejects_any_column(self):
        with pytest.raises(SalesforceClientError, match="not one of this report's columns"):
            build_report_metadata({}, ["A"], None)

    def test_duplicate_column(self):
        with pytest.raises(SalesforceClientError, match="'A' is listed twice"):
            build_report_metadata(self.SAVED, ["A", "A"], None)

    def test_saved_metadata_not_mutated(self):
        import copy
        before = copy.deepcopy(self.SAVED)
        build_report_metadata(self.SAVED, ["A"], [ReportFilter("A", "equals", ["x"])])
        assert self.SAVED == before

    def test_twenty_ids_split_into_two_filters(self):
        built = build_report_metadata(self.SAVED, None, [ReportFilter("A", "equals", _ids(20))])
        assert len(_ids(20)[0]) == 18
        added = built["reportFilters"][2:]
        assert len(added) == 2
        assert [len(f["value"].split(",")) for f in added] == [10, 10]
        assert built["reportBooleanFilter"] == "1 AND 2 AND (3 OR 4)"

    def test_saved_logic_is_wrapped(self):
        saved = {**self.SAVED, "reportBooleanFilter": "1 OR 2"}
        built = build_report_metadata(saved, None, [ReportFilter("A", "equals", ["x"])])
        assert built["reportBooleanFilter"] == "(1 OR 2) AND 3"

    def test_no_saved_filters_single_value(self):
        built = build_report_metadata({"detailColumns": ["A"]}, None, [ReportFilter("A", "equals", ["x"])])
        assert built["reportBooleanFilter"] == "1"
        assert built["reportFilters"] == [{"column": "A", "operator": "equals", "value": "x"}]

    def test_negative_operator_chunks_joined_with_and(self):
        built = build_report_metadata({}, None, [ReportFilter("A", "notEqual", _ids(12))])
        assert built["reportBooleanFilter"] == "(1 AND 2)"

    def test_chunks_split_on_character_length(self):
        values = ["v" * 100, "w" * 100, "x" * 100]
        built = build_report_metadata({}, None, [ReportFilter("A", "equals", values)])
        assert [f["value"] for f in built["reportFilters"]] == [f"{values[0]},{values[1]}", values[2]]

    def test_single_oversized_value_is_its_own_chunk(self):
        built = build_report_metadata({}, None, [ReportFilter("A", "equals", ["v" * 300, "w"])])
        assert [len(f["value"]) for f in built["reportFilters"]] == [300, 1]

    def test_multiple_filters_become_separate_groups(self):
        built = build_report_metadata(
            {}, None, [ReportFilter("A", "equals", ["x"]), ReportFilter("B", "greaterThan", ["5"])],
        )
        assert built["reportBooleanFilter"] == "1 AND 2"

    def test_value_with_comma(self):
        with pytest.raises(SalesforceClientError, match="contains a comma"):
            build_report_metadata({}, None, [ReportFilter("A", "equals", ["a,b"])])

    def test_unknown_operator(self):
        with pytest.raises(SalesforceClientError, match="Unknown report filter operator 'like'; use one of: "):
            build_report_metadata({}, None, [ReportFilter("A", "like", ["a"])])

    def test_bad_column(self):
        with pytest.raises(SalesforceClientError, match="Invalid report filter column"):
            build_report_metadata({}, None, [ReportFilter("A; DROP", "equals", ["a"])])

    def test_empty_values(self):
        with pytest.raises(SalesforceClientError, match="has no values"):
            build_report_metadata({}, None, [ReportFilter("A", "equals", ["", "  "])])

    def test_multi_value_less_than(self):
        with pytest.raises(SalesforceClientError, match="takes exactly one value"):
            build_report_metadata({}, None, [ReportFilter("A", "lessThan", ["1", "2"])])

    def test_too_many_filters(self):
        filters = [ReportFilter("A", "equals", ["x"])] * (MAX_REPORT_FILTERS + 1)
        with pytest.raises(SalesforceClientError, match="21 filters; Salesforce allows at most 20"):
            build_report_metadata({}, None, filters)

    def test_boolean_filter_untouched_when_only_columns(self):
        saved = {**self.SAVED, "reportBooleanFilter": "1 OR 2"}
        built = build_report_metadata(saved, ["A"], None)
        assert built["reportBooleanFilter"] == "1 OR 2"
        assert built["reportFilters"] == saved["reportFilters"]


TABULAR_SAVED = {
    "reportFormat": "TABULAR",
    "detailColumns": ["Name", "Num__c", "City"],
    "reportFilters": [
        {"column": "X", "operator": "equals", "value": "1"},
        {"column": "Y", "operator": "equals", "value": "2"},
    ],
    "reportBooleanFilter": "1 OR 2",
    "aggregates": ["s!Amount"],
    "sortBy": [{"sortColumn": "City", "sortOrder": "Desc"}],
}
SUMMARY_SAVED = {
    "reportFormat": "SUMMARY",
    "detailColumns": ["Name", "Num__c"],
    "groupingsDown": [{"name": "Type", "dateGranularity": "None"}, {"name": "Owner", "dateGranularity": "Day"}],
    "groupingsAcross": [{"name": "Type", "dateGranularity": None}, {"name": "Stage"}],
    "aggregates": ["s!Amount", "RowCount"],
    "chart": {"chartType": "Bar"},
    "customSummaryFormula": {"F": {"label": "f"}},
    "reportFilters": [],
}
SECRET = "SECRET-ROW-VALUE"


class TestBuildKeysetMetadata:
    def test_sort_and_filter_anded_onto_saved_logic(self):
        built = build_keyset_metadata(TABULAR_SAVED, None, None, "Num__c", "PFQA-00003")
        assert built["sortBy"] == [{"sortColumn": "Num__c", "sortOrder": "Asc"}]
        assert built["reportFilters"][2:] == [{"column": "Num__c", "operator": "greaterThan", "value": "PFQA-00003"}]
        expected = build_report_metadata(
            TABULAR_SAVED, None, [ReportFilter("Num__c", "greaterThan", ["PFQA-00003"])],
        )["reportBooleanFilter"]
        assert built["reportBooleanFilter"] == expected == "(1 OR 2) AND 3"
        assert built["aggregates"] == ["s!Amount", "RowCount"]

    def test_saved_metadata_not_mutated(self):
        import copy
        for saved in (TABULAR_SAVED, SUMMARY_SAVED):
            before = copy.deepcopy(saved)
            build_keyset_metadata(saved, None, None, "Num__c", "a")
            assert saved == before

    def test_first_page_has_no_key_filter(self):
        built = build_keyset_metadata(TABULAR_SAVED, None, None, "Num__c", None)
        assert built["reportFilters"] == TABULAR_SAVED["reportFilters"]
        assert built["reportBooleanFilter"] == "1 OR 2"

    def test_caller_filters_and_after_filter_both_anded(self):
        flt = ReportFilter("City", "equals", ["Oslo"])
        built = build_keyset_metadata(TABULAR_SAVED, None, [flt], "Num__c", "a")
        assert [f["column"] for f in built["reportFilters"]] == ["X", "Y", "City", "Num__c"]
        assert built["reportBooleanFilter"] == "(1 OR 2) AND 3 AND 4"

    def test_columns_narrow_the_run(self):
        built = build_keyset_metadata(TABULAR_SAVED, ["City", "Num__c"], None, "Num__c", None)
        assert built["detailColumns"] == ["City", "Num__c"]

    def test_summary_report_is_flattened(self):
        built = build_keyset_metadata(SUMMARY_SAVED, None, None, "Num__c", "a")
        assert built["reportFormat"] == "TABULAR"
        assert built["groupingsDown"] == [] and built["groupingsAcross"] == []
        assert built["aggregates"] == ["RowCount"]
        assert built["chart"] is None and built["customSummaryFormula"] is None
        assert built["detailColumns"] == ["Type", "Owner", "Stage", "Name", "Num__c"]

    def test_grouping_column_in_columns_is_not_repeated(self):
        saved = {**SUMMARY_SAVED, "detailColumns": ["Type", "Name", "Num__c"]}
        built = build_keyset_metadata(saved, None, None, "Num__c", None)
        assert built["detailColumns"] == ["Owner", "Stage", "Type", "Name", "Num__c"]

    def test_flattened_report_can_page_by_a_grouping_column(self):
        built = build_keyset_metadata(SUMMARY_SAVED, None, None, "Type", None)
        assert built["sortBy"][0]["sortColumn"] == "Type"

    def test_report_without_format_or_groupings_is_flattened(self):
        built = build_keyset_metadata({"detailColumns": ["K"]}, None, None, "K", None)
        assert built["reportFormat"] == "TABULAR"
        assert built["detailColumns"] == ["K"]
        assert built["aggregates"] == ["RowCount"]

    def test_tabular_report_keeps_aggregates_and_gets_row_count(self):
        saved = {**TABULAR_SAVED, "aggregates": ["RowCount", "s!Amount"]}
        assert build_keyset_metadata(saved, None, None, "Num__c", None)["aggregates"] == ["RowCount", "s!Amount"]
        assert build_keyset_metadata(TABULAR_SAVED, None, None, "Num__c", None)["aggregates"] == ["s!Amount", "RowCount"]
        no_aggs = {k: v for k, v in TABULAR_SAVED.items() if k != "aggregates"}
        assert build_keyset_metadata(no_aggs, None, None, "Num__c", None)["aggregates"] == ["RowCount"]

    def test_page_by_must_be_a_column(self):
        with pytest.raises(ReportPagingError, match="'Nope' is not a column of this run: Name, Num__c, City") as exc:
            build_keyset_metadata(TABULAR_SAVED, None, None, "Nope", None)
        assert exc.value.reason == "bad_page_by"

    def test_page_by_must_be_in_the_narrowed_columns(self):
        with pytest.raises(ReportPagingError, match="not a column of this run: Name") as exc:
            build_keyset_metadata(TABULAR_SAVED, ["Name"], None, "Num__c", None)
        assert exc.value.reason == "bad_page_by"

    def test_top_rows_refused(self):
        with pytest.raises(ReportPagingError, match="row limit") as exc:
            build_keyset_metadata({**TABULAR_SAVED, "topRows": {"rowLimit": 5}}, None, None, "Num__c", None)
        assert exc.value.reason == "bad_page_by"

    def test_joined_report_refused(self):
        with pytest.raises(ReportPagingError, match="joined report") as exc:
            build_keyset_metadata({**SUMMARY_SAVED, "reportFormat": "MULTI_BLOCK"}, None, None, "Num__c", None)
        assert exc.value.reason == "bad_page_by"

    @pytest.mark.parametrize("granularity", ["Week", "Month", "Quarter", "Year", "FiscalYear"])
    def test_coarse_date_grouping_refused(self, granularity):
        saved = {**SUMMARY_SAVED, "groupingsAcross": [{"name": "Close", "dateGranularity": granularity}]}
        with pytest.raises(ReportPagingError, match="week, month, quarter or year") as exc:
            build_keyset_metadata(saved, None, None, "Num__c", None)
        assert exc.value.reason == "bad_page_by"

    @pytest.mark.parametrize("granularity", ["Day", "None"])
    def test_day_and_none_grouping_allowed(self, granularity):
        saved = {**SUMMARY_SAVED, "groupingsDown": [{"name": "Close", "dateGranularity": granularity}],
                 "groupingsAcross": []}
        assert build_keyset_metadata(saved, None, None, "Num__c", None)["reportFormat"] == "TABULAR"

    def test_coarse_date_grouping_allowed_on_a_tabular_report(self):
        saved = {**TABULAR_SAVED, "groupingsDown": [{"name": "Close", "dateGranularity": "Month"}]}
        assert build_keyset_metadata(saved, None, None, "Num__c", None)["reportFormat"] == "TABULAR"

    def test_twenty_filters_fail_on_the_first_page(self):
        flt = [ReportFilter("City", "equals", ["Oslo"])] * 18      # 2 saved + 18 caller = 20
        with pytest.raises(SalesforceClientError, match="report would have 21 filters; Salesforce allows at most 20"):
            build_keyset_metadata(TABULAR_SAVED, None, flt, "Num__c", None)
        with pytest.raises(SalesforceClientError, match="21 filters"):
            build_keyset_metadata(TABULAR_SAVED, None, flt, "Num__c", "a")

    def test_nineteen_filters_fit(self):
        flt = [ReportFilter("City", "equals", ["Oslo"])] * 17     # 2 saved + 17 caller + key = 20
        assert len(build_keyset_metadata(TABULAR_SAVED, None, flt, "Num__c", None)["reportFilters"]) == 19
        assert len(build_keyset_metadata(TABULAR_SAVED, None, flt, "Num__c", "a")["reportFilters"]) == 20

    def test_invalid_page_by_column_name_rejected_on_the_first_page(self):
        with pytest.raises(SalesforceClientError, match="Invalid report filter column"):
            build_keyset_metadata(TABULAR_SAVED, None, None, "Num; DROP", None)


def _page_result(
    keys, *, count=None, all_data=True, page_by="Num__c", aggregates=("RowCount",), fact_keys=("T!T",),
):
    columns = ["Name", page_by]
    rows = [{"dataCells": [{"value": "n", "label": "n"}, {"value": k, "label": str(k)}]} for k in keys]
    n = len(keys) if count is None else count
    fact = {fk: {"rows": rows, "aggregates": [{"label": str(n), "value": n} for _ in aggregates]}
            for fk in fact_keys}
    return {
        "allData": all_data,
        "reportMetadata": {"detailColumns": columns, "aggregates": list(aggregates)},
        "factMap": fact,
    }


class TestReportPageKeys:
    def test_text_int_and_float_keys(self):
        result = _page_result(["a-1", 7, 2.0, 2.5])
        assert report_page_keys(result, "Num__c", None, None) == (["a-1", "7", "2", "2.5"], 4)

    def test_returns_row_count_not_page_length(self):
        result = _page_result(["a", "b"], count=5, all_data=False)
        assert report_page_keys(result, "Num__c", None, 5) == (["a", "b"], 5)

    @pytest.mark.parametrize("value", ["", "a,b", " padded ", {"x": 1}, True, None, [1]])
    def test_unusable_key_is_bad_page_by(self, value):
        with pytest.raises(ReportPagingError, match="has a value that cannot be paged by") as exc:
            report_page_keys(_page_result(["ok", value]), "Num__c", None, None)
        assert exc.value.reason == "bad_page_by"

    def test_duplicate_key(self):
        with pytest.raises(ReportPagingError, match="not unique") as exc:
            report_page_keys(_page_result(["a", "b", "a"]), "Num__c", None, None)
        assert exc.value.reason == "not_unique"

    def test_after_in_keys_is_not_advancing(self):
        with pytest.raises(ReportPagingError, match="did not advance") as exc:
            report_page_keys(_page_result(["a", "b"], all_data=False, count=9), "Num__c", "b", 9)
        assert exc.value.reason == "not_advancing"

    def test_empty_page_with_more_is_not_advancing(self):
        with pytest.raises(ReportPagingError, match="a page had no rows but Salesforce reported more") as exc:
            report_page_keys(_page_result([], all_data=False, count=3), "Num__c", "a", 3)
        assert exc.value.reason == "not_advancing"

    def test_empty_last_page_is_fine(self):
        assert report_page_keys(_page_result([]), "Num__c", "a", 0) == ([], 0)

    def test_row_count_must_equal_remaining(self):
        with pytest.raises(ReportPagingError, match="3 rows were left but the next page matched 2") as exc:
            report_page_keys(_page_result(["a", "b"]), "Num__c", "0", 3)
        assert exc.value.reason == "rows_lost"

    def test_complete_page_must_return_every_row(self):
        with pytest.raises(ReportPagingError, match="returned 2 of 5 rows") as exc:
            report_page_keys(_page_result(["a", "b"], count=5), "Num__c", None, None)
        assert exc.value.reason == "rows_lost"

    def test_missing_row_count_aggregate(self):
        with pytest.raises(ReportPagingError, match="not as one table|one table") as exc:
            report_page_keys(_page_result(["a"], aggregates=("s!Amount",)), "Num__c", None, None)
        assert exc.value.reason == "not_flat"

    def test_non_int_row_count(self):
        for bad in ("1", True, 1.5, None):
            result = _page_result(["a"])
            result["factMap"]["T!T"]["aggregates"][0]["value"] = bad
            with pytest.raises(ReportPagingError) as exc:
                report_page_keys(result, "Num__c", None, None)
            assert exc.value.reason == "not_flat"

    def test_other_fact_map_key_is_not_flat(self):
        with pytest.raises(ReportPagingError) as exc:
            report_page_keys(_page_result(["a"], fact_keys=("T!T", "0!T")), "Num__c", None, None)
        assert exc.value.reason == "not_flat"
        with pytest.raises(ReportPagingError) as exc:
            report_page_keys(_page_result(["a"], fact_keys=("0!T",)), "Num__c", None, None)
        assert exc.value.reason == "not_flat"

    @pytest.mark.parametrize("mutate", [
        lambda r: r.pop("factMap"),
        lambda r: r["factMap"]["T!T"].pop("rows"),
        lambda r: r["reportMetadata"].update(detailColumns=["Name"]),
        lambda r: r["factMap"]["T!T"]["rows"][0].pop("dataCells"),
        lambda r: r["factMap"]["T!T"].pop("aggregates"),
    ])
    def test_malformed_result_is_not_flat(self, mutate):
        result = _page_result(["a"])
        mutate(result)
        with pytest.raises(ReportPagingError) as exc:
            report_page_keys(result, "Num__c", None, None)
        assert exc.value.reason == "not_flat"

    def test_messages_never_contain_row_values(self):
        cases = [
            (_page_result([SECRET, ""]), None, None),
            (_page_result([SECRET, "x,y"]), None, None),
            (_page_result([SECRET, SECRET]), None, None),
            (_page_result([SECRET], all_data=False, count=9), SECRET, 9),
            (_page_result([SECRET], count=9), None, None),
            (_page_result([SECRET], count=2), SECRET + "0", 8),
            (_page_result([SECRET], fact_keys=("T!T", "0!T")), None, None),
        ]
        for result, after, remaining in cases:
            with pytest.raises(ReportPagingError) as exc:
                report_page_keys(result, "Num__c", after, remaining)
            assert SECRET not in str(exc.value)

    def test_build_messages_never_contain_row_values(self):
        for call in (
            lambda: build_keyset_metadata(TABULAR_SAVED, None, None, "Nope", SECRET),
            lambda: build_keyset_metadata({**TABULAR_SAVED, "topRows": {}, "reportFormat": "MULTI_BLOCK"},
                                          None, None, "Num__c", SECRET),
            lambda: build_keyset_metadata({**TABULAR_SAVED, "topRows": {"rowLimit": 1}}, None, None, "Num__c", SECRET),
        ):
            with pytest.raises(ReportPagingError) as exc:
                call()
            assert SECRET not in str(exc.value)


class TestReportPagingError:
    def test_reason_attribute_and_subclass(self):
        exc = ReportPagingError("rows_lost", "msg")
        assert exc.reason == "rows_lost" and str(exc) == "msg"
        assert isinstance(exc, SalesforceClientError)

    def test_unknown_reason_rejected(self):
        with pytest.raises(ValueError):
            ReportPagingError("nope", "msg")

    def test_reasons(self):
        assert REPORT_PAGING_REASONS == {
            "bad_page_by", "not_unique", "not_advancing", "page_limit", "not_flat", "rows_lost",
        }


class TestReportPageInfo:
    def test_empty_page(self):
        assert report_page_info(3, 10, 0, False) == {"number": 3, "first_row": 0, "last_row": 0, "more": False}

    def test_rows(self):
        assert report_page_info(2, 3, 2, True) == {"number": 2, "first_row": 4, "last_row": 5, "more": True}


class TestRunReportPage:
    SAVED = {"reportFormat": "TABULAR", "detailColumns": ["Name", "Num__c"], "reportFilters": []}

    def _client(self, run_result, saved=None):
        sf = MagicMock()
        sf.restful.side_effect = [{"reportMetadata": saved or self.SAVED}, run_result]
        return with_fake_sf(make_client(), sf), sf

    def test_requires_report_id(self):
        with pytest.raises(SalesforceClientError, match="requires a report_id"):
            make_client().run_report_page("", "Num__c")

    def test_defaults(self):
        assert make_client().report_max_pages == DEFAULT_REPORT_MAX_PAGES == 50

    def test_page_limit_makes_no_call(self):
        sf = MagicMock()
        client = with_fake_sf(make_client(), sf)
        client.report_max_pages = 3
        with pytest.raises(ReportPagingError, match="stopped after 3 pages") as exc:
            client.run_report_page("r1", "Num__c", pages_done=3)
        assert exc.value.reason == "page_limit"
        sf.restful.assert_not_called()

    def test_last_allowed_page_runs(self):
        client, sf = self._client(_page_result(["a"]))
        client.report_max_pages = 3
        assert client.run_report_page("r1", "Num__c", pages_done=2).keys == ["a"]

    def test_describe_then_keyset_post(self):
        result = _page_result(["a", "b"], count=7, all_data=False)
        client, sf = self._client(result)
        flt = ReportFilter("Name", "equals", ["x"])

        page = client.run_report_page("r1", "Num__c", ["Name", "Num__c"], [flt], after="0", pages_done=1, remaining=7)

        assert page == ReportPage(result, ["a", "b"], False, 7)
        built = build_keyset_metadata(self.SAVED, ["Name", "Num__c"], [flt], "Num__c", "0")
        assert sf.restful.call_args_list == [
            (("analytics/reports/r1/describe",), {}),
            (
                ("analytics/reports/r1",),
                {"params": {"includeDetails": "true"}, "method": "POST", "json": {"reportMetadata": built}},
            ),
        ]

    def test_all_data_true_when_absent_or_true(self):
        result = _page_result(["a"])
        del result["allData"]
        client, _ = self._client(result)
        assert client.run_report_page("r1", "Num__c").all_data is True

    def test_non_dict_describe_is_treated_as_empty(self):
        sf = MagicMock()
        sf.restful.side_effect = [None, _page_result(["a"])]
        client = with_fake_sf(make_client(), sf)
        with pytest.raises(ReportPagingError, match="not a column of this run: $") as exc:
            client.run_report_page("r1", "Num__c")
        assert exc.value.reason == "bad_page_by"

    def test_paging_error_from_keys_propagates_unchanged(self):
        client, _ = self._client(_page_result(["a", "a"]))
        with pytest.raises(ReportPagingError) as exc:
            client.run_report_page("r1", "Num__c")
        assert exc.value.reason == "not_unique"

    def test_page_info_for_the_page(self):
        client, _ = self._client(_page_result(["a", "b"], count=4, all_data=False))
        page = client.run_report_page("r1", "Num__c", remaining=None)
        assert report_page_info(2, 3, len(page.keys), not page.all_data) == {
            "number": 2, "first_row": 4, "last_row": 5, "more": True,
        }


class TestClientPlumbing:
    def test_get_sf_builds_once_and_caches(self):
        client = make_client()
        built = []
        client._build_sf = lambda: built.append(1) or object()
        assert client._get_sf() is client._get_sf()
        assert built == [1]

    def test_missing_simple_salesforce_package(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "simple_salesforce", None)
        with pytest.raises(SalesforceClientError, match="'simple-salesforce' package is not installed"):
            make_client()._build_sf()

    def test_save_token_file_round_trips(self, tmp_path):
        path = str(tmp_path / "token.json")
        save_token_file(path, {"access_token": "t"})
        assert load_token_file(path) == {"access_token": "t"}

    @pytest.mark.parametrize("body", [ValueError("not json"), ["list"], {"no": "error"}])
    def test_oauth_error_detail_ignores_unusable_bodies(self, body):
        response = MagicMock()
        if isinstance(body, Exception):
            response.json.side_effect = body
        else:
            response.json.return_value = body
        assert _oauth_error_detail(requests.HTTPError(response=response)) == ""


# ---------------------------------------------------------------------------- #
# SOSL query-building validation/escaping helpers
# ---------------------------------------------------------------------------- #

VALID_ID_15 = "001000000000001"
VALID_ID_18 = "001000000000001AAA"


class TestValidateObjectTypeName:
    @pytest.mark.parametrize("name", ["Account", "Opportunity", "My_Custom_Object__c", "_Leading"])
    def test_valid_names_pass_through(self, name):
        assert _validate_object_type_name(name) == name

    def test_strips_surrounding_whitespace(self):
        assert _validate_object_type_name("  Account  ") == "Account"

    @pytest.mark.parametrize("name", ["Account)", "Ac count", "1Account", "Account;DROP", ""])
    def test_invalid_names_raise(self, name):
        with pytest.raises(SalesforceClientError, match="Invalid Salesforce object type name"):
            _validate_object_type_name(name)


class TestValidateSalesforceId:
    def test_15_char_id_accepted(self):
        assert _validate_salesforce_id(VALID_ID_15) == VALID_ID_15

    def test_18_char_id_accepted(self):
        assert _validate_salesforce_id(VALID_ID_18) == VALID_ID_18

    def test_strips_surrounding_whitespace(self):
        assert _validate_salesforce_id(f"  {VALID_ID_15}  ") == VALID_ID_15

    @pytest.mark.parametrize("value", ["", "short", "001' OR '1'='1", "0" * 20])
    def test_invalid_ids_raise(self, value):
        with pytest.raises(SalesforceClientError, match="must be a 15- or 18-character Salesforce ID"):
            _validate_salesforce_id(value)

    def test_error_includes_custom_field_name(self):
        with pytest.raises(SalesforceClientError, match="account_id must be"):
            _validate_salesforce_id("bad", "account_id")


class TestEscapeSoslTerm:
    def test_plain_text_unchanged(self):
        assert _escape_sosl_term("Acme Corp") == "Acme Corp"

    def test_curly_braces_escaped(self):
        # Prevents breaking out of the FIND{...} clause.
        assert _escape_sosl_term("}} IN ALL FIELDS RETURNING User") == r"\}\} IN ALL FIELDS RETURNING User"

    def test_backslash_escaped_first_so_later_escapes_are_not_doubled(self):
        assert _escape_sosl_term("a\\b") == r"a\\b"

    def test_all_reserved_characters_escaped(self):
        term = '?&|!{}[]()^~*:"\'+-'
        escaped = _escape_sosl_term(term)
        assert escaped == "".join(f"\\{ch}" for ch in term)


# ---------------------------------------------------------------------------- #
# search: SOSL building, scoping, and result normalization
# ---------------------------------------------------------------------------- #

class TestSearch:
    def test_requires_non_empty_search_term(self):
        client = make_client()
        with pytest.raises(SalesforceClientError, match="non-empty search_term"):
            client.search("")
        with pytest.raises(SalesforceClientError, match="non-empty search_term"):
            client.search("   ")

    def test_account_id_without_object_types_raises(self):
        client = make_client()
        with pytest.raises(SalesforceClientError, match="account_id requires object_types"):
            client.search("Acme", account_id=VALID_ID_15)

    def test_invalid_object_type_raises(self):
        client = make_client()
        with pytest.raises(SalesforceClientError, match="Invalid Salesforce object type name"):
            client.search("Acme", object_types="Account, Bad Name")

    def test_invalid_account_id_raises(self):
        client = make_client()
        with pytest.raises(SalesforceClientError, match="account_id must be"):
            client.search("Acme", object_types="Opportunity", account_id="not-an-id")

    def test_unscoped_search_builds_plain_find_query(self):
        sf = MagicMock()
        sf.search.return_value = {"searchRecords": []}
        client = with_fake_sf(make_client(), sf)

        client.search("Acme Corp")

        sf.search.assert_called_once_with("FIND {Acme Corp} IN ALL FIELDS LIMIT 20")

    def test_scoped_search_builds_returning_clause_with_limit(self):
        sf = MagicMock()
        sf.search.return_value = {"searchRecords": []}
        client = with_fake_sf(make_client(), sf)

        client.search("Acme", object_types="Opportunity,Contact", max_results=5)

        sf.search.assert_called_once_with(
            "FIND {Acme} IN ALL FIELDS RETURNING "
            "Opportunity(Id, Name LIMIT 5), Contact(Id, Name LIMIT 5)"
        )

    def test_account_id_scoping_adds_where_clause_to_every_object(self):
        sf = MagicMock()
        sf.search.return_value = {"searchRecords": []}
        client = with_fake_sf(make_client(), sf)

        client.search("Acme", object_types="Opportunity", account_id=VALID_ID_15, max_results=10)

        sf.search.assert_called_once_with(
            f"FIND {{Acme}} IN ALL FIELDS RETURNING "
            f"Opportunity(Id, Name WHERE AccountId = '{VALID_ID_15}' LIMIT 10)"
        )

    def test_search_term_is_escaped_in_query(self):
        sf = MagicMock()
        sf.search.return_value = {"searchRecords": []}
        client = with_fake_sf(make_client(), sf)

        client.search("Acme (West)")

        sf.search.assert_called_once_with(r"FIND {Acme \(West\)} IN ALL FIELDS LIMIT 20")

    def test_max_results_clamped_to_reasonable_bounds(self):
        sf = MagicMock()
        sf.search.return_value = {"searchRecords": []}
        client = with_fake_sf(make_client(), sf)

        client.search("Acme", object_types="Opportunity", max_results=10000)

        assert "LIMIT 200)" in sf.search.call_args.args[0]

    def test_maps_search_records_including_type_from_attributes(self):
        sf = MagicMock()
        sf.search.return_value = {
            "searchRecords": [
                {"attributes": {"type": "Opportunity", "url": "/x"}, "Id": "006x"},
                {"attributes": {"type": "Contact", "url": "/y"}, "Id": "003y"},
            ]
        }
        client = with_fake_sf(make_client(), sf)

        records = client.search("Acme")

        assert records == [
            SalesforceRecord(object_type="Opportunity", id="006x", fields={"Id": "006x"}),
            SalesforceRecord(object_type="Contact", id="003y", fields={"Id": "003y"}),
        ]

    def test_missing_search_records_key_yields_empty_list(self):
        sf = MagicMock()
        sf.search.return_value = {}
        client = with_fake_sf(make_client(), sf)

        assert client.search("Acme") == []


class TestLiveFixtureParsing:
    """Replays fixtures recorded from real, [QATEST]-tagged seed artifacts
    by scripts/qa_fixture_recorder.py --record salesforce -- real API
    shape, not hand-authored, with identity fields already redacted.
    Skipped (not failed) until each fixture exists; see
    tests/fixtures/live/README.md and
    docs/testing-policy.md. Unlike
    Confluence/Jira, there's no separate _parse_* method to call directly
    -- list_reports/get_record parse inline -- so these mock at the
    _get_sf() boundary instead and call the real public method.
    Re-record via that script if this ever starts failing after a
    genuine Salesforce API change.
    """

    def _load(self, name: str) -> dict:
        path = LIVE_FIXTURES_DIR / name
        if not path.exists():
            pytest.skip(
                f"{path} not recorded yet -- run "
                "`python3 scripts/qa_fixture_recorder.py --record salesforce` locally first"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_get_record_fixture_still_parses(self):
        raw = self._load("get_record.json")
        object_type = (raw.get("attributes") or {}).get("type", "Account")
        sf = MagicMock()
        getattr(sf, object_type).get.return_value = raw
        client = with_fake_sf(make_client(), sf)

        # The recorder redacts the fixture's own Id, which is no longer a valid Salesforce id.
        record = client.get_record(object_type, ACCOUNT_ID)

        assert record.id and record.fields.get("Name")

    def test_list_reports_fixture_still_parses(self):
        raw = self._load("list_reports.json")
        sf = MagicMock()
        sf.query.return_value = raw
        client = with_fake_sf(make_client(), sf)

        reports = client.list_reports()

        assert reports, "recorded list_reports.json has no records"
        assert all(r.id and r.name for r in reports)
