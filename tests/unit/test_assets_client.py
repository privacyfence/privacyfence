"""Tests for AssetsClient: workspace discovery, refresh-and-retry, paging, parsing and traversal.

The HTTP layer is replaced by a fake ``requests.Session`` that routes on method and URL, so
what is asserted is the request the client sends and what it makes of the response. The
fixtures in tests/fixtures/assets/ are hand-written from the shapes in the plan (not recorded).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import requests

from privacyfence import assets_client as assets_module
from privacyfence.assets_client import (
    ASSETS_REQUIRED_SCOPE,
    EXPORT_MAX_OBJECTS,
    MAX_PAGES,
    NEEDS_ASSETS_PERMISSION,
    NEEDS_WORKSPACE_ID,
    AssetsClient,
    AssetsClientError,
    AssetsEdge,
    AssetsObject,
    AssetsReference,
)
from privacyfence.atlassian_oauth import AtlassianOAuthError
from privacyfence.atlassian_users import UNKNOWN_USER_LABEL

FIXTURES = Path(__file__).parent.parent / "fixtures" / "assets"
LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "assets"
WORKSPACE = "11111111-2222-3333-4444-555555555555"
JIRA_URL = "https://api.atlassian.com/ex/jira/cloud-1"
API = f"{JIRA_URL}/jsm/assets/workspace/{WORKSPACE}/v1"
CONFIG = {
    "access_token": "tok", "refresh_token": "rt", "cloud_id": "cloud-1",
    "client_id": "ci", "client_secret": "cs", "site_url": "https://acme.atlassian.net",
    "account_email": "me@example.com", "account_id": "acct-1",
    "scopes": [ASSETS_REQUIRED_SCOPE, "read:jira-work"],
}


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, payload: Any = None, status: int = 200, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)  # type: ignore[arg-type]

    def json(self) -> Any:
        return self._payload


class FakeSession:
    """Routes ``request`` to ``handler(method, path, params, body)``; ``path`` is relative to
    the Assets API (or the Jira proxy for the workspace lookup). The handler returns a payload
    or a FakeResponse."""

    def __init__(self, handler) -> None:
        self.headers: dict[str, str] = {}
        self.handler = handler
        self.calls: list[tuple[str, str, dict | None, Any]] = []

    def request(self, method, url, params=None, json=None, timeout=None):
        assert timeout == 30
        path = url[len(API) + 1:] if url.startswith(API) else url[len(JIRA_URL):]
        self.calls.append((method, path, params, json))
        result = self.handler(method, path, params, json)
        return result if isinstance(result, FakeResponse) else FakeResponse(result)

    def paths(self, prefix: str = "") -> list[str]:
        return [c[1] for c in self.calls if c[1].startswith(prefix)]


WORKSPACE_PATH = "/rest/servicedeskapi/assets/workspace"


def routed(routes: dict[str, Any]):
    """A handler answering ``"METHOD path"`` keys from ``routes`` (plus the workspace lookup)."""
    def handler(method, path, params, body):
        if path == WORKSPACE_PATH:
            return fixture("workspace.json")
        value = routes[f"{method} {path}"]
        return value(params, body) if callable(value) else value
    return handler


def make_client(handler=None, token_file=None, **overrides) -> AssetsClient:
    client = AssetsClient({**CONFIG, **overrides}, token_file=token_file)
    client._session = FakeSession(handler or routed({}))  # type: ignore[assignment]
    return client


def session_of(client: AssetsClient) -> FakeSession:
    return client._session  # type: ignore[return-value]


def unauthorized() -> FakeResponse:
    return FakeResponse({}, status=401)


# --------------------------------------------------------------------------- #
# Constructor
# --------------------------------------------------------------------------- #

class TestConstructor:
    @pytest.mark.parametrize("override", [{"access_token": ""}, {"cloud_id": ""}])
    def test_missing_token_or_cloud_id(self, override):
        with pytest.raises(AssetsClientError) as exc:
            AssetsClient({**CONFIG, **override})
        assert str(exc.value) == "Jira Assets is not authenticated. Use Authenticate… in PrivacyFence Settings."

    @pytest.mark.parametrize("scopes", [[], None, ["read:jira-work"]])
    def test_missing_scope_needs_permission(self, scopes):
        with pytest.raises(AssetsClientError) as exc:
            AssetsClient({**CONFIG, "scopes": scopes})
        assert str(exc.value) == NEEDS_ASSETS_PERMISSION
        assert "Use Authenticate…" in str(exc.value)

    def test_bearer_header_and_no_network(self):
        client = AssetsClient(CONFIG)
        assert client._session.headers["Authorization"] == "Bearer tok"


# --------------------------------------------------------------------------- #
# Workspace discovery
# --------------------------------------------------------------------------- #

class TestWorkspace:
    def test_found_and_cached_across_reads(self):
        client = make_client(routed({
            "GET objectschema/2": {"id": "2", "name": "S"},
            "GET objectschema/3": {"id": "3", "name": "T"},
        }))
        assert client.get_schema("2").name == "S"
        assert client.get_schema("3").name == "T"
        assert session_of(client).paths(WORKSPACE_PATH) == [WORKSPACE_PATH]
        assert session_of(client).calls[0][:2] == ("GET", WORKSPACE_PATH)

    @pytest.mark.parametrize("payload", [{"values": []}, {}, {"values": [{}]}, {"values": [{"workspaceId": ""}]}])
    def test_no_workspace(self, payload):
        client = make_client(lambda *a: payload)
        with pytest.raises(AssetsClientError) as exc:
            client.check_connection()
        assert str(exc.value) == (
            "This Atlassian site has no Assets workspace (Jira Service Management Assets is not enabled)."
        )

    def test_check_connection_resolves_workspace_only(self):
        client = make_client()
        assert client.check_connection() is None
        assert session_of(client).paths() == [WORKSPACE_PATH]

    @pytest.mark.parametrize("response", [
        FakeResponse({}, status=403),
        FakeResponse({}, status=401, text='{"code":401,"message":"Unauthorized; scope does not match"}'),
    ])
    def test_refused_lookup_asks_for_the_workspace_id(self, response, monkeypatch):
        monkeypatch.setattr(assets_module, "atlassian_refresh", lambda *a: {"access_token": "new"})
        client = make_client(lambda *a: response)
        with pytest.raises(AssetsClientError) as exc:
            client.check_connection()
        assert str(exc.value) == NEEDS_WORKSPACE_ID
        assert "connectors.assets.workspace_id" in str(exc.value)

    def test_configured_id_skips_the_lookup(self):
        client = make_client(routed({"GET objectschema/2": {"id": "2", "name": "S"}}), assets_workspace_id=WORKSPACE)
        assert client.get_schema("2").name == "S"
        assert session_of(client).paths(WORKSPACE_PATH) == []

    def test_check_connection_proves_a_configured_id_with_one_read(self):
        client = make_client(routed({"GET objectschema/list": {"values": []}}), assets_workspace_id=WORKSPACE)
        assert client.check_connection() is None
        assert session_of(client).calls == [("GET", "objectschema/list", {"startAt": 0, "maxResults": 1}, None)]

    def test_check_connection_fails_for_a_wrong_configured_id(self):
        client = make_client(lambda *a: FakeResponse({}, status=404), assets_workspace_id=WORKSPACE)
        with pytest.raises(AssetsClientError, match=r"check_connection failed: 404 Error"):
            client.check_connection()

    @pytest.mark.parametrize("bad", ["../../rest/api/3/myself", "not-a-uuid", f"{WORKSPACE}/x"])
    def test_configured_id_must_be_a_uuid(self, bad):
        with pytest.raises(AssetsClientError, match="not a valid id"):
            AssetsClient({**CONFIG, "assets_workspace_id": bad})

    def test_looked_up_id_must_be_a_uuid(self):
        client = make_client(lambda *a: {"values": [{"workspaceId": "../x"}]})
        with pytest.raises(AssetsClientError, match="unexpected Assets workspace id"):
            client.check_connection()

    def test_http_failure_is_wrapped(self):
        client = make_client(lambda *a: FakeResponse({}, status=500))
        with pytest.raises(AssetsClientError, match=r"check_connection failed: 500 Error"):
            client.check_connection()


# --------------------------------------------------------------------------- #
# Token refresh
# --------------------------------------------------------------------------- #

class TestRefresh:
    @pytest.fixture
    def saved(self, monkeypatch):
        record: dict[str, Any] = {}
        monkeypatch.setattr(assets_module, "save_token_file", lambda path, rec: record.update(rec))
        monkeypatch.setattr(assets_module, "load_token_file", lambda path: {"refresh_token": "rotated"})
        monkeypatch.setattr(
            assets_module, "atlassian_refresh",
            lambda cid, cs, rt: {"access_token": "new-tok", "refresh_token": "new-rt", "_used": rt},
        )
        return record

    def test_401_refreshes_retries_and_keeps_scopes(self, saved, tmp_path):
        attempts = []

        def handler(method, path, params, body):
            if path == WORKSPACE_PATH:
                return fixture("workspace.json")
            attempts.append(path)
            return unauthorized() if len(attempts) == 1 else {"id": "2", "name": "S"}

        client = make_client(handler, token_file=str(tmp_path / "t.json"))
        assert client.get_schema("2").id == "2"
        assert attempts == ["objectschema/2", "objectschema/2"]
        assert client._session.headers["Authorization"] == "Bearer new-tok"
        assert saved == {
            "access_token": "new-tok", "refresh_token": "new-rt", "cloud_id": "cloud-1",
            "site_url": "https://acme.atlassian.net", "account_email": "me@example.com",
            "account_id": "acct-1", "scopes": [ASSETS_REQUIRED_SCOPE, "read:jira-work"],
        }

    def test_refresh_keeps_the_account_id_from_disk_when_config_lacks_it(self, monkeypatch, tmp_path):
        saved: dict[str, Any] = {}
        monkeypatch.setattr(
            assets_module, "load_token_file",
            lambda path: {"refresh_token": "rotated", "account_id": "acct-disk"},
        )
        monkeypatch.setattr(assets_module, "save_token_file", lambda path, rec: saved.update(rec))
        monkeypatch.setattr(assets_module, "atlassian_refresh", lambda cid, cs, rt: {"access_token": "n"})
        client = make_client(token_file=str(tmp_path / "t.json"), account_id="")

        assert client._try_refresh() is True
        assert saved["account_id"] == "acct-disk"

    def test_refresh_uses_refresh_token_rotated_by_another_client(self, monkeypatch, tmp_path):
        used = []
        monkeypatch.setattr(assets_module, "load_token_file", lambda path: {"refresh_token": "rotated"})
        monkeypatch.setattr(assets_module, "save_token_file", lambda path, rec: None)
        monkeypatch.setattr(
            assets_module, "atlassian_refresh",
            lambda cid, cs, rt: used.append(rt) or {"access_token": "n"},
        )
        client = make_client(token_file=str(tmp_path / "t.json"))
        assert client._try_refresh() is True
        assert used == ["rotated"]
        assert client._config["refresh_token"] == "rotated"

    def test_refresh_without_token_file_does_not_save(self, monkeypatch):
        monkeypatch.setattr(
            assets_module, "save_token_file", lambda *a: pytest.fail("saved without a token file"),
        )
        monkeypatch.setattr(assets_module, "atlassian_refresh", lambda *a: {"access_token": "n", "refresh_token": "r2"})
        client = make_client()
        assert client._try_refresh() is True
        assert client._config["refresh_token"] == "r2"

    def test_missing_token_file_falls_back_to_config_refresh_token(self, monkeypatch, tmp_path):
        used = []

        def missing(path):
            raise AtlassianOAuthError("no file")

        monkeypatch.setattr(assets_module, "load_token_file", missing)
        monkeypatch.setattr(assets_module, "save_token_file", lambda *a: None)
        monkeypatch.setattr(assets_module, "atlassian_refresh", lambda cid, cs, rt: used.append(rt) or {"access_token": "n"})
        client = make_client(token_file=str(tmp_path / "t.json"))
        assert client._try_refresh() is True
        assert used == ["rt"]
        assert client._config["refresh_token"] == "rt"

    def test_token_file_without_refresh_token_falls_back_to_config(self, monkeypatch, tmp_path):
        used = []
        monkeypatch.setattr(assets_module, "load_token_file", lambda path: {})
        monkeypatch.setattr(assets_module, "save_token_file", lambda *a: None)
        monkeypatch.setattr(assets_module, "atlassian_refresh", lambda cid, cs, rt: used.append(rt) or {"access_token": "n"})
        make_client(token_file=str(tmp_path / "t.json"))._try_refresh()
        assert used == ["rt"]

    @pytest.mark.parametrize("override", [{"client_id": ""}, {"client_secret": ""}, {"refresh_token": ""}])
    def test_incomplete_credentials_do_not_refresh(self, override, monkeypatch):
        monkeypatch.setattr(assets_module, "atlassian_refresh", lambda *a: pytest.fail("refreshed"))
        assert make_client(**override)._try_refresh() is False

    def test_refresh_error_is_logged_and_reported(self, monkeypatch, caplog):
        def boom(*a):
            raise AtlassianOAuthError("denied")

        monkeypatch.setattr(assets_module, "atlassian_refresh", boom)
        with caplog.at_level("WARNING"):
            assert make_client()._try_refresh() is False
        assert "Assets token refresh failed: denied" in caplog.text

    def test_refresh_without_access_token_fails(self, monkeypatch):
        monkeypatch.setattr(assets_module, "atlassian_refresh", lambda *a: {})
        assert make_client()._try_refresh() is False

    def test_401_with_failed_refresh_raises_the_original(self, monkeypatch):
        monkeypatch.setattr(assets_module, "atlassian_refresh", lambda *a: {})
        client = make_client(lambda *a: unauthorized())
        with pytest.raises(AssetsClientError, match=r"check_connection failed: 401 Error"):
            client.check_connection()

    def test_other_errors_do_not_refresh(self, monkeypatch):
        monkeypatch.setattr(assets_module, "atlassian_refresh", lambda *a: pytest.fail("refreshed"))
        client = make_client(lambda *a: FakeResponse({}, status=403), assets_workspace_id=WORKSPACE)
        with pytest.raises(AssetsClientError, match=r"check_connection failed: 403 Error"):
            client.check_connection()

    def test_request_returns_the_decoded_json(self):
        client = make_client(routed({"GET objectschema/2": {"id": "2", "name": "S"}}))
        result = client._request(client._send, "GET", f"{API}/objectschema/2")
        assert result == {"id": "2", "name": "S"}


# --------------------------------------------------------------------------- #
# Input checks
# --------------------------------------------------------------------------- #

class TestValidation:
    @pytest.mark.parametrize("bad", ["", "abc", "12 ", "1\n", "1234567890123", "../x", "-1", 12])
    def test_ids(self, bad):
        client = make_client()
        calls = [
            (client.get_schema, "schema id"),
            (client.list_object_types, "schema id"),
            (client.list_attributes, "object type id"),
            (client.get_object, "object id"),
            (client.connected_tickets, "object id"),
            (client.related_objects, "object id"),
            (lambda v: client.get_objects(["1", v]), "object id"),
        ]
        for fn, what in calls:
            with pytest.raises(AssetsClientError) as exc:
                fn(bad)
            assert str(exc.value) == f"{what} must be a numeric Assets id"
        assert session_of(client).calls == []

    @pytest.mark.parametrize("bad", ["", "   ", "x" * 4001, None])
    def test_aql(self, bad):
        client = make_client()
        for fn in (client.search_objects, client.export_objects):
            with pytest.raises(AssetsClientError) as exc:
                fn(bad)
            assert str(exc.value) == "aql must be 1-4000 characters"
        assert session_of(client).calls == []

    def test_aql_at_the_limit_is_accepted(self):
        client = make_client(lambda m, p, q, b: {"values": [], "isLast": True} if m == "POST" else fixture("workspace.json"))
        assert client.search_objects("x" * 4000) == []


# --------------------------------------------------------------------------- #
# Schemas, object types, attributes
# --------------------------------------------------------------------------- #

def schema_pages(counter: list[int] | None = None):
    def handler(method, path, params, body):
        if path == WORKSPACE_PATH:
            return fixture("workspace.json")
        assert path == "objectschema/list"
        if counter is not None:
            counter.append(params["startAt"])
        return fixture("objectschema_list_page1.json" if params["startAt"] == 0 else "objectschema_list_page2.json")
    return handler


class TestSchemas:
    def test_pages_until_is_last(self):
        client = make_client(schema_pages())
        schemas = client.list_schemas()
        assert [(s.id, s.key, s.name) for s in schemas] == [
            ("2", "PFQA", "PrivacyFence QA [QATEST]"), ("3", "PFQB", "Second QA schema [QATEST]"),
        ]
        first = schemas[0]
        assert (first.description, first.object_count, first.object_type_count) == ("Synthetic QA schema", 7, 3)
        assert [c[2] for c in session_of(client).calls[1:]] == [
            {"startAt": 0, "maxResults": 100}, {"startAt": 1, "maxResults": 99},
        ]

    def test_stops_at_max_results(self):
        starts: list[int] = []
        client = make_client(schema_pages(starts))
        assert [s.id for s in client.list_schemas(max_results=1)] == ["2"]
        assert starts == [0]

    def test_max_results_below_one_reads_one(self):
        client = make_client(schema_pages())
        assert len(client.list_schemas(max_results=-5)) == 1

    def test_stops_at_an_empty_page(self):
        calls = []
        client = make_client(lambda m, p, q, b: calls.append(p) or ({"values": []} if p == "objectschema/list" else fixture("workspace.json")))
        assert client.list_schemas() == []
        assert calls.count("objectschema/list") == 1

    def test_stops_at_max_pages(self):
        def handler(method, path, params, body):
            if path == WORKSPACE_PATH:
                return fixture("workspace.json")
            return {"isLast": False, "values": [{"id": str(params["startAt"]), "name": "n"}]}

        client = make_client(handler)
        assert len(client.list_schemas(max_results=1000)) == MAX_PAGES
        assert len(session_of(client).paths("objectschema")) == MAX_PAGES

    def test_sparse_schema_reads_as_defaults(self):
        client = make_client(routed({"GET objectschema/9": {"id": 9, "objectCount": None}}))
        schema = client.get_schema("9")
        assert (schema.id, schema.name, schema.key, schema.description) == ("9", "", "", "")
        assert (schema.object_count, schema.object_type_count) == (0, 0)

    def test_failure_is_wrapped_with_the_method_name(self):
        client = make_client(lambda m, p, q, b: FakeResponse({}, 500) if p != WORKSPACE_PATH else fixture("workspace.json"))
        with pytest.raises(AssetsClientError, match=r"^list_schemas failed: 500 Error"):
            client.list_schemas()


class TestObjectTypes:
    def test_flat_list(self):
        client = make_client(routed({"GET objectschema/2/objecttypes/flat": fixture("objecttypes_flat.json")}))
        types = client.list_object_types("2")
        assert [(t.id, t.name, t.schema_id, t.parent_id, t.object_count) for t in types] == [
            ("5", "Account", "2", "", 2), ("7", "Contract", "2", "", 2),
            ("9", "Asset", "2", "", 3), ("10", "Hardware asset", "2", "9", 0),
        ]
        assert types[0].description == "Customer accounts [QATEST]"


class TestAttributes:
    def make(self):
        return make_client(routed({"GET objecttype/7/attributes": fixture("objecttype_attributes_contract.json")}))

    def test_definitions(self):
        defs = {a.name: a for a in self.make().list_attributes("7")}
        name = defs["Name"]
        assert (name.id, name.kind, name.value_type, name.is_label, name.is_system) == ("70", "default", "Text", True, False)
        assert (name.min_cardinality, name.max_cardinality) == (1, 1)
        account = defs["Account"]
        assert (account.kind, account.value_type) == ("reference", "")
        assert (account.reference_type_id, account.reference_type_name, account.reference_kind) == ("5", "Account", "Reference")
        assert defs["Owner"].kind == "user"
        assert defs["Tags"].max_cardinality == -1
        assert defs["Status"].kind == "status"
        assert defs["Key"].is_system is True
        assert defs["Created"].value_type == "DateTime"

    @pytest.mark.parametrize("raw_type,kind", [
        (0, "default"), (1, "reference"), (2, "user"), (3, "confluence"), (4, "group"),
        (5, "version"), (6, "project"), (7, "status"), (8, "other"), (None, "other"),
    ])
    def test_kind_mapping(self, raw_type, kind):
        raw = [{"id": "1", "name": "A", "type": raw_type}]
        client = make_client(routed({"GET objecttype/7/attributes": raw}))
        attr = client.list_attributes("7")[0]
        assert attr.kind == kind
        assert attr.value_type == ""
        assert (attr.min_cardinality, attr.max_cardinality, attr.is_label, attr.is_system) == (0, 0, False, False)

    def test_cached_per_type(self):
        client = self.make()
        first = client.list_attributes("7")
        assert client.list_attributes("7") == first
        assert session_of(client).paths("objecttype") == ["objecttype/7/attributes"]


# --------------------------------------------------------------------------- #
# Objects
# --------------------------------------------------------------------------- #

def contract_routes():
    return {
        "GET object/123": fixture("object_contract.json"),
        "GET object/123/attributes": fixture("object_contract_attributes.json"),
    }


class TestGetObject:
    def test_reads_object_and_attributes(self):
        client = make_client(routed(contract_routes()))
        obj = client.get_object("123")
        assert session_of(client).paths("object") == ["object/123", "object/123/attributes"]
        assert obj.to_dict() == {
            "id": "123", "key": "PFQA-123", "label": "PrivacyFence QA seed contract [QATEST]",
            "type": "Contract", "type_id": "7", "schema_id": "2",
            "created": "2026-01-05T10:00:00.000Z", "updated": "2026-02-01T09:30:00.000Z",
            "attributes": {
                "Name": "PrivacyFence QA seed contract [QATEST]",
                "Account": "ACME Ltd [QATEST] (PFQA-45)",
                "Owner": "QA Owner [QATEST]",
                "Tags": ["renewal", "support"],
                "Status": "Active",
            },
            "references": {"Account": [{"id": "45", "key": "PFQA-45", "label": "ACME Ltd [QATEST]"}]},
        }
        # An embedded objectTypeAttribute needs no attribute-definition lookup.
        assert session_of(client).paths("objecttype") == []

    def test_sparse_object_reads_as_empty(self):
        client = make_client(routed({"GET object/1": {}, "GET object/1/attributes": []}))
        obj = client.get_object("1")
        assert obj == AssetsObject("", "", "", "", "", "", "", "", {}, {})
        assert obj.to_dict()["attributes"] == {}


class TestParseObject:
    def parse(self, entries, names=None, client=None):
        client = client or make_client()
        raw = {"id": "1", "objectKey": "K-1", "label": "L", "objectType": {"id": "7", "name": "Contract", "objectSchemaId": "2"},
               "attributes": entries}
        return client._parse_object(raw, names)

    @staticmethod
    def entry(values, **kw):
        return {"objectTypeAttribute": {"name": "Attr"}, "objectAttributeValues": values, **kw}

    def test_default_values_use_display_value_then_value(self):
        obj = self.parse([self.entry([{"displayValue": "shown", "value": "raw"}, {"value": 42}, {"displayValue": "", "value": "v"}, {}])])
        assert obj.attributes == {"Attr": ["shown", "42", "v", ""]}

    def test_reference_goes_to_both_maps(self):
        ref = {"referencedObject": {"id": "45", "objectKey": "K-45", "label": "Acme"}}
        obj = self.parse([self.entry([ref, ref])])
        assert obj.references == {"Attr": [AssetsReference("45", "K-45", "Acme")] * 2}
        assert obj.attributes == {"Attr": ["Acme (K-45)"] * 2}

    def test_user_names_are_masked_and_never_an_email(self):
        values = [
            {"user": {"displayName": "jo@example.com", "key": "account-id-1234"}},
            {"user": {"displayName": "Casey QA [QATEST]", "key": "x"}},
            {"user": {}},
            {"user": {"displayName": "x@example.com"}},
        ]
        obj = self.parse([self.entry(values)])
        assert obj.attributes["Attr"] == [
            "Customer account 1234", "Casey QA [QATEST]", UNKNOWN_USER_LABEL, UNKNOWN_USER_LABEL,
        ]
        assert "@" not in json.dumps(obj.to_dict())

    def test_status_and_group_names(self):
        obj = self.parse([self.entry([{"status": {"name": "Active"}}, {"group": {"name": "QA Group"}}])])
        assert obj.attributes["Attr"] == ["Active", "QA Group"]

    def test_attribute_without_values_is_left_out(self):
        obj = self.parse([self.entry([]), {"objectTypeAttribute": {"name": "B"}}])
        assert obj.attributes == {} and obj.references == {}

    def test_repeated_attribute_name_accumulates(self):
        obj = self.parse([self.entry([{"displayValue": "a"}]), self.entry([{"displayValue": "b"}])])
        assert obj.attributes == {"Attr": ["a", "b"]}

    def test_name_from_embedded_attribute_first(self):
        entry = self.entry([{"displayValue": "v"}], objectTypeAttributeId="70")
        obj = self.parse([entry], names={"70": "From names"})
        assert list(obj.attributes) == ["Attr"]

    def test_name_from_page_attribute_list_second(self):
        client = make_client(lambda *a: pytest.fail("looked up attributes"))
        entry = {"objectTypeAttributeId": "70", "objectAttributeValues": [{"displayValue": "v"}]}
        obj = self.parse([entry], names={"70": "Name"}, client=client)
        assert obj.attributes == {"Name": ["v"]}

    def test_name_from_attribute_definitions_last_and_cached(self):
        client = make_client(routed({"GET objecttype/7/attributes": fixture("objecttype_attributes_contract.json")}))
        entries = [
            {"objectTypeAttributeId": "70", "objectAttributeValues": [{"displayValue": "a"}]},
            {"objectTypeAttributeId": "73", "objectAttributeValues": [{"displayValue": "b"}]},
            {"objectTypeAttribute": {}, "objectTypeAttributeId": "71", "objectAttributeValues": [{"displayValue": "c"}]},
        ]
        obj = self.parse(entries, client=client)
        assert obj.attributes == {"Name": ["a"], "Tags": ["b"], "Account": ["c"]}
        assert session_of(client).paths("objecttype") == ["objecttype/7/attributes"]

    def test_unknown_attribute_id_is_used_as_the_name(self):
        client = make_client(routed({"GET objecttype/7/attributes": []}))
        entry = {"objectTypeAttributeId": "999", "objectAttributeValues": [{"displayValue": "v"}]}
        assert self.parse([entry], client=client).attributes == {"999": ["v"]}

    def test_to_dict_collapses_single_values_only(self):
        obj = self.parse([self.entry([{"displayValue": "a"}, {"displayValue": "b"}]),
                          {"objectTypeAttribute": {"name": "One"}, "objectAttributeValues": [{"displayValue": "x"}]}])
        assert obj.to_dict()["attributes"] == {"Attr": ["a", "b"], "One": "x"}
        assert obj.to_dict()["references"] == {}


class TestSearchObjects:
    def test_request_and_parse(self):
        client = make_client(routed({"POST object/aql": fixture("aql_page.json")}))
        objects = client.search_objects('objectType = "Contract"', max_results=5)
        assert [o.key for o in objects] == ["PFQA-123", "PFQA-124"]
        method, path, params, body = session_of(client).calls[-1]
        assert (method, path) == ("POST", "object/aql")
        assert params == {"startAt": 0, "maxResults": 5, "includeAttributes": "true"}
        assert body == {"qlQuery": 'objectType = "Contract"'}
        # Names come from the page's objectTypeAttributes: no attribute-definition request.
        assert objects[0].attributes["Name"] == ["PrivacyFence QA seed contract [QATEST]"]
        assert objects[0].references["Account"][0].id == "45"
        assert session_of(client).paths("objecttype") == []

    def test_clamps_max_results_to_100_in_pages_of_50(self):
        def page(params, body):
            start, size = params["startAt"], params["maxResults"]
            return {"isLast": False, "values": [{"id": str(start + i)} for i in range(size)]}

        client = make_client(routed({"POST object/aql": page}))
        assert len(client.search_objects("x", max_results=5000)) == 100
        assert [c[2]["maxResults"] for c in session_of(client).calls[1:]] == [50, 50]
        assert [c[2]["startAt"] for c in session_of(client).calls[1:]] == [0, 50]

    def test_max_results_below_one_reads_one(self):
        client = make_client(routed({"POST object/aql": {"isLast": False, "values": [{"id": "1"}, {"id": "2"}]}}))
        assert len(client.search_objects("x", max_results=0)) == 1

    def test_stops_at_an_empty_page(self):
        client = make_client(routed({"POST object/aql": {"isLast": False, "values": []}}))
        assert client.search_objects("x") == []
        assert len(session_of(client).paths("object/aql")) == 1

    def test_ignores_has_more_results_and_total(self):
        page = {"isLast": True, "hasMoreResults": True, "total": 999, "values": [{"id": "1"}]}
        client = make_client(routed({"POST object/aql": page}))
        assert len(client.search_objects("x")) == 1
        assert len(session_of(client).paths("object/aql")) == 1

    def test_max_pages_caps_the_loop(self):
        def page(params, body):
            return {"isLast": False, "values": [{"id": str(params["startAt"] + i)} for i in range(params["maxResults"])]}

        client = make_client(routed({"POST object/aql": page}))
        objects = client._run_aql("x", 1000, 10, MAX_PAGES)
        assert len(objects) == 100
        assert len(session_of(client).paths("object/aql")) == MAX_PAGES

    def test_failure_is_wrapped(self):
        client = make_client(routed({"POST object/aql": FakeResponse({}, 400)}))
        with pytest.raises(AssetsClientError, match=r"^search_objects failed: 400 Error"):
            client.search_objects("bad aql")


class TestGetObjects:
    @staticmethod
    def aql_by_ids(params, body):
        ids = re.search(r"IN \(([\d,]+)\)", body["qlQuery"]).group(1).split(",")
        return {"isLast": True, "values": [{"id": i, "objectKey": f"K-{i}"} for i in reversed(ids) if i != "404"]}

    def test_batches_of_50_keep_the_requested_order_and_skip_missing(self):
        client = make_client(routed({"POST object/aql": self.aql_by_ids}))
        ids = [str(i) for i in range(1, 52)] + ["404", "3"]
        objects = client.get_objects(ids)
        assert [o.id for o in objects] == [str(i) for i in range(1, 52)]
        queries = [c[3]["qlQuery"] for c in session_of(client).calls if c[0] == "POST"]
        assert len(queries) == 2
        assert queries[0].startswith("objectId IN (1,2,3,") and queries[0].count(",") == 49
        assert queries[1] == "objectId IN (51,404)"

    def test_empty_ids_make_no_request(self):
        client = make_client()
        assert client.get_objects([]) == []
        assert session_of(client).calls == []


class TestExport:
    def test_returns_every_object(self):
        client = make_client(routed({"POST object/aql": fixture("aql_page.json")}))
        assert [o.id for o in client.export_objects("objectSchemaId = 2")] == ["123", "124"]
        assert session_of(client).calls[-1][2]["maxResults"] == 100

    def test_over_ten_thousand_raises_the_exact_text(self):
        def page(params, body):
            return {"isLast": False, "values": [{"id": str(params["startAt"] + i)} for i in range(params["maxResults"])]}

        client = make_client(routed({"POST object/aql": page}))
        with pytest.raises(AssetsClientError) as exc:
            client.export_objects("objectSchemaId = 2")
        assert str(exc.value) == "The AQL query matches more than 10,000 objects; narrow it and export in parts."
        # No MAX_PAGES cap: 100 pages of 100, then the 10,001st object.
        assert len(session_of(client).paths("object/aql")) == EXPORT_MAX_OBJECTS // 100 + 1

    def test_exactly_ten_thousand_is_allowed(self):
        def page(params, body):
            start = params["startAt"]
            count = min(params["maxResults"], EXPORT_MAX_OBJECTS - start)
            return {"isLast": start + count >= EXPORT_MAX_OBJECTS, "values": [{"id": str(start + i)} for i in range(count)]}

        client = make_client(routed({"POST object/aql": page}))
        assert len(client.export_objects("x")) == EXPORT_MAX_OBJECTS


# --------------------------------------------------------------------------- #
# Linked objects
# --------------------------------------------------------------------------- #

class Cmdb:
    """An in-memory Assets site: ``refs`` maps an object id to {attribute: [target ids]}."""

    def __init__(self, refs: dict[str, dict[str, list[str]]]) -> None:
        self.refs = refs

    def raw(self, oid: str) -> dict[str, Any]:
        return {"id": oid, "objectKey": f"K-{oid}", "label": f"Object {oid}",
                "objectType": {"id": "7", "name": "Thing", "objectSchemaId": "2"},
                "attributes": self.attributes(oid)}

    def attributes(self, oid: str) -> list[dict[str, Any]]:
        return [
            {"objectTypeAttribute": {"name": attribute}, "objectAttributeValues": [
                {"referencedObject": {"id": t, "objectKey": f"K-{t}", "label": f"Object {t}"}} for t in targets]}
            for attribute, targets in self.refs[oid].items()
        ]

    def handler(self):
        def aql(params, body):
            query = body["qlQuery"]
            ids = set(re.search(r"IN \(([\d,]+)\)", query).group(1).split(","))
            if query.startswith("objectId IN"):
                hits = [o for o in self.refs if o in ids]
            else:
                assert query.startswith("object HAVING outboundReferences(objectId IN (")
                hits = [o for o in self.refs if any(t in ids for ts in self.refs[o].values() for t in ts)]
            page = hits[params["startAt"]:params["startAt"] + params["maxResults"]]
            return {"isLast": params["startAt"] + len(page) >= len(hits), "values": [self.raw(o) for o in page]}

        routes: dict[str, Any] = {"POST object/aql": aql}
        for oid in self.refs:
            routes[f"GET object/{oid}"] = {k: v for k, v in self.raw(oid).items() if k != "attributes"}
            routes[f"GET object/{oid}/attributes"] = self.attributes(oid)
        return routed(routes)

    def client(self) -> AssetsClient:
        return make_client(self.handler())


def ids_of(graph) -> list[str]:
    return [o.id for o in graph.objects]


def edge_set(graph) -> set[tuple[str, str, str]]:
    return {(e.source_id, e.target_id, e.attribute) for e in graph.edges}


CONTRACT_WORLD = {
    "123": {"Account": ["45"]},
    "45": {"Parent": ["46"]},
    "46": {},
    "200": {"Contract": ["123"]},
    "201": {"Contract": ["123"]},
}


class TestRelatedObjects:
    def test_outbound_depth_one(self):
        graph = Cmdb(CONTRACT_WORLD).client().related_objects("123", "outbound")
        assert graph.root_id == "123"
        assert ids_of(graph) == ["123", "45"]
        assert graph.edges == [AssetsEdge("123", "45", "Account")]
        assert graph.truncated is False

    def test_inbound_depth_one_uses_the_reference_query(self):
        client = Cmdb(CONTRACT_WORLD).client()
        graph = client.related_objects("123", "inbound")
        assert ids_of(graph) == ["123", "200", "201"]
        assert edge_set(graph) == {("200", "123", "Contract"), ("201", "123", "Contract")}
        queries = [c[3]["qlQuery"] for c in session_of(client).calls if c[0] == "POST"]
        assert queries == ["object HAVING outboundReferences(objectId IN (123))"]

    def test_both_at_depth_two_dedupes_edges(self):
        graph = Cmdb(CONTRACT_WORLD).client().related_objects("123", depth=2)
        assert ids_of(graph) == ["123", "45", "200", "201", "46"]
        # 123 -> 45 is found from the outbound side at level 1 and again from the inbound side
        # at level 2 (45 is then on the frontier); it is kept once.
        assert len(graph.edges) == len(edge_set(graph)) == 4
        assert edge_set(graph) == {
            ("123", "45", "Account"), ("45", "46", "Parent"),
            ("200", "123", "Contract"), ("201", "123", "Contract"),
        }
        assert graph.truncated is False

    def test_default_direction_is_both(self):
        graph = Cmdb(CONTRACT_WORLD).client().related_objects("123")
        assert ids_of(graph) == ["123", "45", "200", "201"]

    def test_depth_is_clamped_to_one_through_three(self):
        chain = {str(i): {"Next": [str(i + 1)]} for i in range(1, 7)} | {"7": {}}
        assert ids_of(Cmdb(chain).client().related_objects("1", "outbound", depth=99)) == ["1", "2", "3", "4"]
        assert ids_of(Cmdb(chain).client().related_objects("1", "outbound", depth=0)) == ["1", "2"]

    def test_stops_when_a_level_adds_nothing(self):
        client = Cmdb(CONTRACT_WORLD).client()
        graph = client.related_objects("46", "outbound", depth=3)
        assert ids_of(graph) == ["46"] and graph.edges == [] and graph.truncated is False
        assert session_of(client).paths("object/aql") == []

    def test_truncates_at_max_objects_and_drops_edges_to_unseen(self):
        graph = Cmdb(CONTRACT_WORLD).client().related_objects("123", max_objects=2)
        # Outbound takes the one free slot; inbound finds 200 and 201 but there is no room.
        assert ids_of(graph) == ["123", "45"]
        assert graph.edges == [AssetsEdge("123", "45", "Account")]
        assert graph.truncated is True

    def test_outbound_truncation(self):
        world = {"1": {"A": ["2", "3", "4"]}, "2": {}, "3": {}, "4": {}}
        graph = Cmdb(world).client().related_objects("1", "outbound", max_objects=2)
        assert ids_of(graph) == ["1", "2"]
        assert edge_set(graph) == {("1", "2", "A")}
        assert graph.truncated is True

    def test_full_budget_with_nothing_more_is_not_truncated(self):
        world = {"1": {"A": ["2", "3"]}, "2": {}, "3": {}}
        graph = Cmdb(world).client().related_objects("1", "both", max_objects=3)
        assert ids_of(graph) == ["1", "2", "3"] and graph.truncated is False

    def test_seen_objects_coming_back_inbound_do_not_use_budget(self):
        # 2 and 1 reference each other; with room for exactly one new object the result is not truncated.
        world = {"1": {"A": ["2"]}, "2": {"B": ["1"]}}
        graph = Cmdb(world).client().related_objects("1", "both", depth=2, max_objects=2)
        assert ids_of(graph) == ["1", "2"] and graph.truncated is False
        assert edge_set(graph) == {("1", "2", "A"), ("2", "1", "B")}

    def test_max_objects_is_clamped(self):
        world = {"1": {"A": ["2"]}, "2": {}}
        assert ids_of(Cmdb(world).client().related_objects("1", "outbound", max_objects=0)) == ["1"]
        assert Cmdb(world).client().related_objects("1", "outbound", max_objects=0).truncated is True

    def test_large_frontier_is_queried_in_batches_of_50(self):
        leaves = [str(1000 + i) for i in range(60)]
        world = {"1": {"Leaf": leaves}, "900": {"Linker": [leaves[0], leaves[-1]]}} | {leaf: {} for leaf in leaves}
        client = Cmdb(world).client()
        graph = client.related_objects("1", "both", depth=2, max_objects=200)
        assert ids_of(graph)[:2] == ["1", "1000"] and "900" in ids_of(graph) and len(graph.objects) == 62
        assert ("900", leaves[0], "Linker") in edge_set(graph) and ("900", leaves[-1], "Linker") in edge_set(graph)
        queries = [c[3]["qlQuery"] for c in session_of(client).calls if c[0] == "POST"]
        inbound = [q for q in queries if q.startswith("object HAVING")]
        assert len(inbound) == 1 + 2  # level 1: the root; level 2: 60 leaves in two batches
        assert [q for q in queries if q.startswith("objectId IN")].__len__() == 2  # 60 leaves in two batches

    def test_bad_direction(self):
        client = make_client()
        with pytest.raises(AssetsClientError) as exc:
            client.related_objects("1", "sideways")
        assert str(exc.value) == "direction must be outbound, inbound or both"
        assert session_of(client).calls == []

    def test_failure_in_the_middle_is_wrapped(self):
        client = make_client(routed({
            "GET object/1": {"id": "1"}, "GET object/1/attributes": [], "POST object/aql": FakeResponse({}, 500),
        }))
        with pytest.raises(AssetsClientError, match=r"^related_objects failed: 500 Error"):
            client.related_objects("1", "inbound")


# --------------------------------------------------------------------------- #
# Connected tickets
# --------------------------------------------------------------------------- #

class TestConnectedTickets:
    def test_parses_tickets(self):
        client = make_client(routed({"GET objectconnectedtickets/123/tickets": fixture("connected_tickets.json")}))
        tickets = client.connected_tickets("123")
        assert [(t.key, t.summary, t.status, t.issue_type, t.priority) for t in tickets] == [
            ("PFQA-1", "Renew ACME support contract [QATEST]", "Open", "Task", "Medium"),
        ]

    def test_missing_fields_read_as_empty(self):
        client = make_client(routed({"GET objectconnectedtickets/1/tickets": {"tickets": [{"key": "A-1", "status": None}]}}))
        ticket = client.connected_tickets("1")[0]
        assert (ticket.key, ticket.summary, ticket.status, ticket.issue_type, ticket.priority) == ("A-1", "", "", "", "")

    @pytest.mark.parametrize("payload", [{}, {"tickets": None}, {"tickets": []}])
    def test_no_tickets(self, payload):
        client = make_client(routed({"GET objectconnectedtickets/1/tickets": payload}))
        assert client.connected_tickets("1") == []


class TestLiveFixtureParsing:
    """Replays fixtures recorded from the real, [QATEST]-tagged PFQA schema by
    scripts/qa_fixture_recorder.py --record assets -- real API shape, not
    hand-authored, de-identified by _deidentify_assets. Skipped (not failed)
    until the files exist; see tests/fixtures/live/README.md and
    docs/testing-policy.md. The replay client raises on any HTTP call.
    """

    def _load(self, name: str) -> dict:
        path = LIVE_FIXTURES_DIR / name
        if not path.exists():
            pytest.skip(
                f"{path} not recorded yet -- run "
                "`python3 scripts/qa_fixture_recorder.py --record assets` locally first"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _replay_client() -> AssetsClient:
        def refuse(method, path, params, body):
            raise AssertionError(f"unexpected HTTP call {method} {path}")

        return make_client(refuse)

    def test_list_schemas_fixture_still_parses(self):
        raw = self._load("list_schemas.json")
        schemas = [assets_module._parse_schema(v) for v in raw["values"]]
        assert schemas, "recorded list_schemas.json has no values"
        assert all(s.id and s.key and s.name for s in schemas)
        assert all(s.key == "PFQA" for s in schemas)

    def test_get_object_fixture_still_parses(self):
        client = self._replay_client()
        raw = self._load("get_object.json")
        obj = client._parse_object({**raw["object"], "attributes": raw["attributes"]})
        assert obj.id and obj.key and obj.label and obj.type_name and obj.schema_id
        assert "[QATEST]" in obj.label
        assert obj.attributes

    def test_aql_search_fixture_still_parses(self):
        client = self._replay_client()
        raw = self._load("aql_search.json")
        names = {a["id"]: a["name"] for a in raw.get("objectTypeAttributes", [])}
        objects = [client._parse_object(v, names) for v in raw["values"]]
        assert objects, "recorded aql_search.json has no values"
        assert all("[QATEST]" in o.label for o in objects)

    def test_aql_search_fixture_attribute_ids_still_join(self):
        raw = self._load("aql_search.json")
        known = {a["id"] for a in raw["objectTypeAttributes"]}
        used = {e["objectTypeAttributeId"] for v in raw["values"] for e in v.get("attributes", [])}
        assert used and used <= known
        assert all(i.isdigit() for i in known | used)

    def test_get_object_fixture_attribute_ids_still_join(self):
        raw = self._load("get_object.json")
        for entry in raw["attributes"]:
            assert entry["objectTypeAttributeId"].isdigit()
            assert entry["objectTypeAttributeId"] == entry["objectTypeAttribute"]["id"]

    def test_reference_definitions_still_join_their_target_type(self):
        definitions = [e["objectTypeAttribute"] for e in self._load("get_object.json")["attributes"]]
        definitions += self._load("aql_search.json")["objectTypeAttributes"]
        references = [d for d in definitions if d.get("referenceObjectType")]
        assert references, "no reference attribute in the recorded fixtures"
        for raw in references:
            parsed = assets_module._parse_attribute_def(raw)
            assert parsed.kind == "reference"
            assert parsed.reference_type_id == raw["referenceObjectType"]["id"]
            assert assets_module._check_id(parsed.reference_type_id, "object type id")
            assert parsed.reference_type_name and parsed.reference_kind

    def test_get_and_search_replay_through_the_client(self):
        get_object = self._load("get_object.json")
        aql = self._load("aql_search.json")
        object_id = get_object["object"]["id"]
        client = make_client(routed({
            f"GET object/{object_id}": get_object["object"],
            f"GET object/{object_id}/attributes": get_object["attributes"],
            "POST object/aql": aql,
        }))

        obj = client.get_object(object_id)
        found = client.search_objects("objectSchemaId = 1", max_results=len(aql["values"]))

        # Attribute names come from the payloads themselves: no attribute-definition lookup.
        assert session_of(client).paths() == [
            WORKSPACE_PATH, f"object/{object_id}", f"object/{object_id}/attributes", "object/aql"]
        assert len(found) == len(aql["values"])
        refs = [r for o in [obj, *found] for rs in o.references.values() for r in rs]
        assert refs, "no reference value in the recorded fixtures"
        for o in [obj, *found]:
            for value in (o.id, o.type_id, o.schema_id):
                assets_module._check_id(value, "id")
        for ref in refs:
            assets_module._check_id(ref.id, "object id")
            assert ref.key.startswith("PFQA-") and "[QATEST]" in ref.label
