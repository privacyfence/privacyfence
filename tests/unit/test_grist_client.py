"""Tests for the Grist REST client.

``requests.Session.request`` is replaced (no network), so every test sees the
exact method, URL, parameters, body and headers the client sends. The
invariants: redirects are never followed, a 401 triggers one forced refresh
only for a credential that can refresh, a 403 is its own error type, an id is
validated before any request, and no token reaches a message or a repr.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from privacyfence.grist_auth import GristApiKey
from privacyfence.grist_client import (
    GristAccessDenied,
    GristClient,
    GristClientError,
    GristColumn,
    GristDocument,
    GristRecord,
    GristTable,
    validate_doc_id,
    validate_identifier,
)

pytestmark = pytest.mark.unit

SERVER = "https://grist.example.com:8484"
LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "grist"


class RefreshingCredential:
    can_refresh = True

    def __init__(self) -> None:
        self.forced = 0

    def access_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh:
            self.forced += 1
        return "at-2" if self.forced else "at-1"


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, *, content: bytes | None = None) -> None:
        self.status_code = status
        self._body = body
        if content is not None:
            self.content = content
        else:
            self.content = b"" if body is None else json.dumps(body).encode()

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("not json")
        return self._body


class Net:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers: list[Any] = []
        monkeypatch.setattr(
            requests.Session, "request",
            lambda _session, method, url, **kw: self._request(method, url, **kw),
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def reply(self, *answers: Any) -> None:
        self.answers = list(answers)


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Net:
    return Net(monkeypatch)


@pytest.fixture
def cred() -> RefreshingCredential:
    return RefreshingCredential()


@pytest.fixture
def client(net: Net, cred: RefreshingCredential) -> GristClient:
    return GristClient(SERVER, cred)


@pytest.fixture
def key_client(net: Net) -> GristClient:
    return GristClient(SERVER, GristApiKey("key-1"))


def ok(body: Any) -> FakeResponse:
    return FakeResponse(200, body)


class TestValidateIds:
    @pytest.mark.parametrize("value", ["8CAN8gKdxY7z", "a~b-c_d", "x" * 128])
    def test_doc_id_accepted(self, value: str) -> None:
        assert validate_doc_id(value) == value

    @pytest.mark.parametrize("value", ["../x", "a/b", "a?b", "", "x" * 129, "a b", "a\n"])
    def test_doc_id_rejected(self, value: str) -> None:
        with pytest.raises(GristClientError, match="Not a Grist document id"):
            validate_doc_id(value)

    @pytest.mark.parametrize("value", ["Contacts", "_x", "A1_b", "T" * 64])
    def test_identifier_accepted(self, value: str) -> None:
        assert validate_identifier(value, "table") == value

    @pytest.mark.parametrize("value", ["1abc", "a-b", "a b", "", "T" * 65, "a/b", "é"])
    def test_identifier_rejected(self, value: str) -> None:
        with pytest.raises(GristClientError, match="Not a Grist column id: "):
            validate_identifier(value, "column")

    def test_non_string_rejected(self) -> None:
        with pytest.raises(GristClientError):
            validate_doc_id(5)  # type: ignore[arg-type]


class TestRequestErrors:
    def test_connection_error(self, net: Net, client: GristClient) -> None:
        net.reply(requests.ConnectionError("secret at-1 detail"))
        with pytest.raises(GristClientError) as exc:
            client.get_document("doc1")
        assert str(exc.value) == (
            "Could not reach the Grist server at grist.example.com:8484: ConnectionError"
        )

    def test_redirect(self, net: Net, client: GristClient) -> None:
        net.reply(FakeResponse(302, {}))
        with pytest.raises(GristClientError) as exc:
            client.get_document("doc1")
        assert str(exc.value) == (
            "The Grist server answered with a redirect (HTTP 302). Check the Grist server address."
        )

    def test_401_then_success_after_one_forced_refresh(
        self, net: Net, client: GristClient, cred: RefreshingCredential,
    ) -> None:
        net.reply(FakeResponse(401, {}), ok({"name": "D"}))
        assert client.get_document("doc1").name == "D"
        assert cred.forced == 1
        assert [c["headers"]["Authorization"] for c in net.calls] == ["Bearer at-1", "Bearer at-2"]

    def test_401_twice(self, net: Net, client: GristClient, cred: RefreshingCredential) -> None:
        net.reply(FakeResponse(401, {}))
        with pytest.raises(GristClientError) as exc:
            client.get_document("doc1")
        assert str(exc.value) == (
            "Grist refused the sign-in (HTTP 401). The API key or sign-in may be wrong, expired or "
            "revoked. Use Authenticate… in PrivacyFence Settings to connect again."
        )
        assert len(net.calls) == 2
        assert cred.forced == 1
        assert "at-" not in str(exc.value)

    def test_401_with_api_key_does_not_retry(self, net: Net, key_client: GristClient) -> None:
        net.reply(FakeResponse(401, {}))
        with pytest.raises(GristClientError, match="HTTP 401"):
            key_client.get_document("doc1")
        assert len(net.calls) == 1
        assert net.calls[0]["headers"] == {"Authorization": "Bearer key-1"}

    def test_403_is_access_denied(self, net: Net, client: GristClient) -> None:
        net.reply(FakeResponse(403, {"error": "nope"}))
        with pytest.raises(GristAccessDenied) as exc:
            client.get_document("doc1")
        assert str(exc.value) == (
            "Grist refused the request (HTTP 403). Your Grist account, or what you allowed "
            "PrivacyFence when signing in, does not cover it."
        )
        assert len(net.calls) == 1

    def test_404(self, net: Net, client: GristClient) -> None:
        net.reply(FakeResponse(404, {}))
        with pytest.raises(GristClientError) as exc:
            client.get_document("doc1")
        assert not isinstance(exc.value, GristAccessDenied)
        assert str(exc.value) == "Grist found no such document, table or record (HTTP 404)."

    def test_500_with_error_detail_cut(self, net: Net, client: GristClient) -> None:
        net.reply(FakeResponse(500, {"error": "x" * 500}))
        with pytest.raises(GristClientError) as exc:
            client.get_document("doc1")
        assert str(exc.value) == f"Grist API error (HTTP 500): {'x' * 200}"

    def test_500_without_json(self, net: Net, client: GristClient) -> None:
        net.reply(FakeResponse(500, None, content=b"<html>oops</html>"))
        with pytest.raises(GristClientError) as exc:
            client.get_document("doc1")
        assert str(exc.value) == "Grist API error (HTTP 500): no detail"

    def test_200_with_html_body(self, net: Net, client: GristClient) -> None:
        net.reply(FakeResponse(200, None, content=b"<html></html>"))
        with pytest.raises(GristClientError) as exc:
            client.get_document("doc1")
        assert str(exc.value) == (
            "The Grist server answered with something other than JSON (HTTP 200). "
            "Check the Grist server address."
        )

    def test_empty_body_is_none(self, net: Net, client: GristClient) -> None:
        net.reply(FakeResponse(200, None))
        assert client._request("GET", "/api/x") is None

    def test_request_shape(self, net: Net, client: GristClient) -> None:
        net.reply(ok({}))
        client.get_document("doc1")
        call = net.calls[0]
        assert call["method"] == "GET"
        assert call["url"] == f"{SERVER}/api/docs/doc1"
        assert call["allow_redirects"] is False
        assert call["timeout"] == 30
        assert call["headers"] == {"Authorization": "Bearer at-1"}


class TestCheckConnection:
    def test_refreshing_credential_makes_no_request(self, net: Net, client: GristClient) -> None:
        net.reply(ok([]))
        assert client.check_connection() == "grist.example.com:8484"
        assert net.calls == []

    def test_api_key_gets_orgs(self, net: Net, key_client: GristClient) -> None:
        net.reply(ok([]))
        assert key_client.check_connection() == "grist.example.com:8484"
        assert [(c["method"], c["url"]) for c in net.calls] == [("GET", f"{SERVER}/api/orgs")]

    def test_api_key_401_raises(self, net: Net, key_client: GristClient) -> None:
        net.reply(FakeResponse(401, {}))
        with pytest.raises(GristClientError, match="HTTP 401"):
            key_client.check_connection()


class TestHost:
    def test_host_has_port(self, client: GristClient) -> None:
        assert client.host == "grist.example.com:8484"

    def test_repr_hides_credential(self, key_client: GristClient) -> None:
        assert "key-1" not in repr(key_client)
        assert "grist.example.com" in repr(key_client)


class TestListDocuments:
    def test_uses_document_id_sorts_and_names_team(self, net: Net, client: GristClient) -> None:
        net.reply(
            ok([{"id": 2, "name": "Beta"}, {"id": 1, "name": "Acme"}]),
            ok([{"name": "WS", "docs": [
                {"id": "idB", "urlId": "short", "name": "Zed"},
                {"id": "idA", "name": "Abe"},
                {"name": "no id"},
            ]}]),
            ok([{"name": "W1", "docs": [{"id": "idC", "name": "Mid"}]}, "junk"]),
        )
        docs = client.list_documents()
        assert [(c["method"], c["url"]) for c in net.calls] == [
            ("GET", f"{SERVER}/api/orgs"),
            ("GET", f"{SERVER}/api/orgs/2/workspaces"),
            ("GET", f"{SERVER}/api/orgs/1/workspaces"),
        ]
        assert docs == [
            GristDocument("idC", "Mid", "W1", "Acme"),
            GristDocument("idA", "Abe", "WS", "Beta"),
            GristDocument("idB", "Zed", "WS", "Beta"),
        ]

    def test_org_without_a_usable_id_is_skipped(self, net: Net, client: GristClient) -> None:
        net.reply(
            ok([{"name": "no id"}, {"id": "x", "name": "bad"}, {"id": None}, "junk", {"id": 3, "name": "Ok"}]),
            ok([{"name": "W", "docs": [{"id": "idA", "name": "Abe"}]}]),
        )
        assert client.list_documents() == [GristDocument("idA", "Abe", "W", "Ok")]
        assert [c["url"] for c in net.calls] == [f"{SERVER}/api/orgs", f"{SERVER}/api/orgs/3/workspaces"]

    def test_org_cap(self, net: Net, client: GristClient) -> None:
        net.reply(ok([{"id": i, "name": f"o{i}"} for i in range(30)]), ok([]))
        client.list_documents()
        assert len(net.calls) == 1 + 20

    def test_document_cap(self, net: Net, client: GristClient) -> None:
        docs = [{"id": f"d{i:04}", "name": f"n{i:04}"} for i in range(600)]
        net.reply(ok([{"id": 1, "name": "T"}]), ok([{"name": "W", "docs": docs}]))
        assert len(client.list_documents()) == 500


class TestGetDocument:
    def test_parses_workspace_and_org(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"id": "ignored", "name": "D", "workspace": {"name": "W", "org": {"name": "T"}}}))
        assert client.get_document("doc1") == GristDocument("doc1", "D", "W", "T")

    def test_missing_keys_give_empty(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"name": "D"}))
        assert client.get_document("doc1") == GristDocument("doc1", "D", "", "")
        net.reply(ok({"name": "D", "workspace": {"name": "W"}}))
        assert client.get_document("doc1") == GristDocument("doc1", "D", "W", "")

    def test_bad_id_before_request(self, net: Net, client: GristClient) -> None:
        with pytest.raises(GristClientError):
            client.get_document("a/b")
        assert net.calls == []


class TestListColumns:
    def test_parses_and_label_falls_back(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"columns": [
            {"id": "Name", "fields": {"label": "Full name", "type": "Text", "isFormula": False}},
            {"id": "Calc", "fields": {"type": "Numeric", "isFormula": True}},
            {"id": "Bare"},
        ]}))
        cols = client.list_columns("doc1", "People")
        assert net.calls[0]["url"] == f"{SERVER}/api/docs/doc1/tables/People/columns"
        assert cols == [
            GristColumn("Name", "Full name", "Text", False),
            GristColumn("Calc", "Calc", "Numeric", True),
            GristColumn("Bare", "Bare", "", False),
        ]

    def test_bad_ids_before_request(self, net: Net, client: GristClient) -> None:
        with pytest.raises(GristClientError, match="document"):
            client.list_columns("a/b", "People")
        with pytest.raises(GristClientError, match="table"):
            client.list_columns("doc1", "../x")
        assert net.calls == []


class TestListTables:
    def test_tables_with_columns(self, net: Net, client: GristClient) -> None:
        net.reply(
            ok({"tables": [{"id": "A"}, {"id": "B"}]}),
            ok({"columns": [{"id": "x", "fields": {"label": "X", "type": "Int"}}]}),
            ok({"columns": []}),
        )
        tables = client.list_tables("doc1")
        assert [c["url"] for c in net.calls] == [
            f"{SERVER}/api/docs/doc1/tables",
            f"{SERVER}/api/docs/doc1/tables/A/columns",
            f"{SERVER}/api/docs/doc1/tables/B/columns",
        ]
        assert tables == [GristTable("A", [GristColumn("x", "X", "Int", False)]), GristTable("B", [])]

    def test_table_cap(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"tables": [{"id": f"T{i}"} for i in range(150)]}), ok({"columns": []}))
        assert len(client.list_tables("doc1")) == 100

    def test_bad_id_before_request(self, net: Net, client: GristClient) -> None:
        with pytest.raises(GristClientError):
            client.list_tables("a?b")
        assert net.calls == []


class TestGetRecords:
    def test_requests_one_extra_and_truncates(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"records": [{"id": i, "fields": {"A": i}} for i in range(1, 4)]}))
        page = client.get_records("doc1", "T", limit=2)
        call = net.calls[0]
        assert call["method"] == "GET"
        assert call["url"] == f"{SERVER}/api/docs/doc1/tables/T/records"
        assert call["params"] == {"limit": "3", "sort": "id"}
        assert page.truncated is True
        assert page.records == [GristRecord(1, {"A": 1}), GristRecord(2, {"A": 2})]
        assert page.next_after_id == 2

    def test_not_truncated(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"records": [{"id": 1, "fields": {}}]}))
        page = client.get_records("doc1", "T", limit=5)
        assert page.truncated is False
        assert page.next_after_id is None
        assert len(page.records) == 1

    def test_filter_and_sort(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"records": [{"id": i, "fields": {}} for i in (9, 3, 5)]}))
        page = client.get_records("doc1", "T", filters={"S": ["Ä", 1]}, sort="-A,B", limit=2)
        params = net.calls[0]["params"]
        assert json.loads(params["filter"]) == {"S": ["Ä", 1]}
        assert params["sort"] == "-A,B"
        assert params["limit"] == "3"
        assert page.truncated is True
        assert page.next_after_id is None

    def test_after_id_widens_the_fetch_and_skips_up_to_it(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"records": [{"id": i, "fields": {}} for i in (2, 7, 8, 11, 12)]}))
        page = client.get_records("doc1", "T", filters={"S": ["x"]}, limit=2, after_id=7)
        params = net.calls[0]["params"]
        assert params["limit"] == "10"
        assert params["sort"] == "id"
        assert json.loads(params["filter"]) == {"S": ["x"]}
        assert [r.id for r in page.records] == [8, 11]
        assert page.truncated is True
        assert page.next_after_id == 11

    def test_after_id_with_sort_is_refused_before_request(self, net: Net, client: GristClient) -> None:
        with pytest.raises(GristClientError, match="cannot be combined with sort"):
            client.get_records("doc1", "T", sort="Name", after_id=3)
        assert net.calls == []

    def test_bad_ids_before_request(self, net: Net, client: GristClient) -> None:
        with pytest.raises(GristClientError):
            client.get_records("a/b", "T")
        with pytest.raises(GristClientError):
            client.get_records("doc1", "1T")
        assert net.calls == []


class FakeGristTable:
    """The records endpoint's own semantics (filter, sort by id, limit), over a table that can
    change between calls, so paging is tested against the server's rules rather than a script."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, ids: list[int]) -> None:
        self.ids = list(ids)
        self.calls = 0
        monkeypatch.setattr(
            requests.Session, "request", lambda _session, method, url, **kw: self._request(**kw),
        )

    def _request(self, *, params: dict[str, str], **_: Any) -> FakeResponse:
        self.calls += 1
        assert params["sort"] == "id"
        rows = sorted(self.ids)[:int(params["limit"])]
        return ok({"records": [{"id": i, "fields": {"N": i}} for i in rows]})


def read_all(client: GristClient, limit: int, between_pages: Any = None) -> tuple[list[int], int]:
    ids: list[int] = []
    after_id, pages = 0, 0
    while True:
        page = client.get_records("doc1", "T", limit=limit, after_id=after_id)
        pages += 1
        ids += [r.id for r in page.records]
        if not page.truncated:
            assert page.next_after_id is None
            return ids, pages
        assert page.next_after_id == page.records[-1].id
        after_id = page.next_after_id
        if between_pages:
            between_pages()


class TestPaging:
    def test_first_middle_and_last_page(self, monkeypatch: pytest.MonkeyPatch, client: GristClient) -> None:
        FakeGristTable(monkeypatch, list(range(1, 3501)))
        first = client.get_records("doc1", "T", limit=500)
        assert [first.records[0].id, first.records[-1].id, first.next_after_id] == [1, 500, 500]
        middle = client.get_records("doc1", "T", limit=500, after_id=1500)
        assert [middle.records[0].id, middle.records[-1].id, middle.next_after_id] == [1501, 2000, 2000]
        last = client.get_records("doc1", "T", limit=500, after_id=3000)
        assert [last.records[0].id, last.records[-1].id] == [3001, 3500]
        assert last.truncated is False and last.next_after_id is None

    def test_whole_table_with_gaps_reads_every_row_once(
        self, monkeypatch: pytest.MonkeyPatch, client: GristClient,
    ) -> None:
        table = [i for i in range(1, 4001) if i % 7]
        FakeGristTable(monkeypatch, table)
        ids, pages = read_all(client, 500)
        assert ids == table
        assert pages == 7

    def test_exact_multiple_of_limit_ends_with_an_empty_page(
        self, monkeypatch: pytest.MonkeyPatch, client: GristClient,
    ) -> None:
        FakeGristTable(monkeypatch, list(range(1, 1001)))
        ids, pages = read_all(client, 500)
        assert ids == list(range(1, 1001))
        assert pages == 2
        assert client.get_records("doc1", "T", limit=500, after_id=1000).records == []

    def test_empty_table(self, monkeypatch: pytest.MonkeyPatch, client: GristClient) -> None:
        FakeGristTable(monkeypatch, [])
        page = client.get_records("doc1", "T", limit=500)
        assert page.records == [] and page.truncated is False and page.next_after_id is None

    def test_record_added_between_pages_is_read_once(
        self, monkeypatch: pytest.MonkeyPatch, client: GristClient,
    ) -> None:
        server = FakeGristTable(monkeypatch, list(range(1, 1201)))

        def add_row() -> None:
            server.ids.append(max(server.ids) + 1)

        ids, _ = read_all(client, 500, between_pages=add_row)
        assert ids == sorted(set(ids))
        assert ids == server.ids

    def test_record_deleted_between_pages_shifts_nothing(
        self, monkeypatch: pytest.MonkeyPatch, client: GristClient,
    ) -> None:
        server = FakeGristTable(monkeypatch, list(range(1, 1201)))

        def delete_an_early_row() -> None:
            server.ids.remove(min(server.ids))

        ids, _ = read_all(client, 500, between_pages=delete_an_early_row)
        assert ids == list(range(1, 1201))


class TestGetRecordsById:
    def test_filters_on_id(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"records": [{"id": 5, "fields": {"A": "x"}}]}))
        recs = client.get_records_by_id("doc1", "T", [5, 6])
        call = net.calls[0]
        assert call["method"] == "GET"
        assert json.loads(call["params"]["filter"]) == {"id": [5, 6]}
        assert recs == [GristRecord(5, {"A": "x"})]

    @pytest.mark.parametrize("record", [{"fields": {}}, {"id": "x"}, {"id": None}])
    def test_record_without_a_usable_id_is_a_client_error(
        self, net: Net, client: GristClient, record: dict,
    ) -> None:
        net.reply(ok({"records": [record]}))
        with pytest.raises(GristClientError, match="usable id"):
            client.get_records_by_id("doc1", "T", [1])

    def test_bad_ids_before_request(self, net: Net, client: GristClient) -> None:
        with pytest.raises(GristClientError):
            client.get_records_by_id("a/b", "T", [1])
        with pytest.raises(GristClientError):
            client.get_records_by_id("doc1", "a-b", [1])
        assert net.calls == []


class TestAddRecords:
    def test_created_record_without_a_usable_id_is_a_client_error(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"records": [{"id": 7}, {"id": "x"}]}))
        with pytest.raises(GristClientError, match="usable id"):
            client.add_records("doc1", "T", [{"A": 1}, {"A": 2}])

    def test_posts_fields_and_returns_ids(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"records": [{"id": 7}, {"id": 8}]}))
        ids = client.add_records("doc1", "T", [{"A": 1}, {"A": None}])
        call = net.calls[0]
        assert (call["method"], call["url"]) == ("POST", f"{SERVER}/api/docs/doc1/tables/T/records")
        assert call["json"] == {"records": [{"fields": {"A": 1}}, {"fields": {"A": None}}]}
        assert ids == [7, 8]

    def test_bad_ids_before_request(self, net: Net, client: GristClient) -> None:
        with pytest.raises(GristClientError):
            client.add_records("a/b", "T", [{}])
        with pytest.raises(GristClientError):
            client.add_records("doc1", "a b", [{}])
        assert net.calls == []


class TestUpdateRecords:
    def test_patches_rows(self, net: Net, client: GristClient) -> None:
        net.reply(FakeResponse(200, None))
        assert client.update_records("doc1", "T", [(5, {"A": "x"})]) is None
        call = net.calls[0]
        assert (call["method"], call["url"]) == ("PATCH", f"{SERVER}/api/docs/doc1/tables/T/records")
        assert call["json"] == {"records": [{"id": 5, "fields": {"A": "x"}}]}

    def test_bad_ids_before_request(self, net: Net, client: GristClient) -> None:
        with pytest.raises(GristClientError):
            client.update_records("a/b", "T", [(1, {})])
        with pytest.raises(GristClientError):
            client.update_records("doc1", "a/b", [(1, {})])
        assert net.calls == []


class TestAddTable:
    def test_posts_table_and_returns_assigned_id(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"tables": [{"id": "Notes2"}]}))
        table_id = client.add_table("doc1", "Notes", [
            {"id": "Title", "label": "The title", "type": "Text"},
            {"id": "Count"},
        ])
        call = net.calls[0]
        assert (call["method"], call["url"]) == ("POST", f"{SERVER}/api/docs/doc1/tables")
        assert call["json"] == {"tables": [{"id": "Notes", "columns": [
            {"id": "Title", "fields": {"label": "The title", "type": "Text"}},
            {"id": "Count", "fields": {"label": "Count", "type": "Text"}},
        ]}]}
        assert table_id == "Notes2"

    def test_falls_back_to_requested_id(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"tables": []}))
        assert client.add_table("doc1", "Notes", []) == "Notes"

    def test_bad_ids_before_request(self, net: Net, client: GristClient) -> None:
        with pytest.raises(GristClientError):
            client.add_table("a/b", "T", [])
        with pytest.raises(GristClientError):
            client.add_table("doc1", "a/b", [])
        with pytest.raises(GristClientError, match="column"):
            client.add_table("doc1", "T", [{"id": "a/b"}])
        assert net.calls == []


class TestAddColumns:
    def test_posts_columns_and_returns_ids(self, net: Net, client: GristClient) -> None:
        net.reply(ok({"columns": [{"id": "A"}, {"id": "B"}]}))
        ids = client.add_columns("doc1", "T", [{"id": "A", "type": "Int"}, {"id": "B", "label": "Bee"}])
        call = net.calls[0]
        assert (call["method"], call["url"]) == ("POST", f"{SERVER}/api/docs/doc1/tables/T/columns")
        assert call["json"] == {"columns": [
            {"id": "A", "fields": {"label": "A", "type": "Int"}},
            {"id": "B", "fields": {"label": "Bee", "type": "Text"}},
        ]}
        assert ids == ["A", "B"]

    def test_bad_ids_before_request(self, net: Net, client: GristClient) -> None:
        with pytest.raises(GristClientError):
            client.add_columns("a/b", "T", [])
        with pytest.raises(GristClientError):
            client.add_columns("doc1", "a/b", [])
        with pytest.raises(GristClientError, match="column"):
            client.add_columns("doc1", "T", [{"id": "1x"}])
        assert net.calls == []


class TestLiveFixtureParsing:
    """Replays fixtures recorded from the [QATEST]-tagged QA document by
    scripts/qa_fixture_recorder.py --record grist through the real parsers,
    with only ``_request`` faked. Skipped (not failed) until each fixture
    exists; see tests/fixtures/live/README.md and docs/testing-policy.md.
    """

    DOC = "qaDocId123"

    def _load(self, name: str) -> Any:
        path = LIVE_FIXTURES_DIR / name
        if not path.exists():
            pytest.skip(
                f"{path} not recorded yet -- run "
                "`python3 scripts/qa_fixture_recorder.py --record grist` locally first"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def _client_returning(self, raw: Any) -> GristClient:
        client = GristClient(SERVER, GristApiKey("key"))
        client._request = lambda *args, **kwargs: raw  # type: ignore[method-assign]
        return client

    def test_list_columns_fixture_still_parses(self):
        raw = self._load("list_columns.json")

        columns = self._client_returning(raw).list_columns(self.DOC, "QaSeed")

        assert [c.id for c in columns] == ["Name", "Note"]
        assert all(c.label and c.type == "Text" and not c.is_formula for c in columns)

    def test_get_records_fixture_still_parses(self):
        raw = self._load("get_records.json")

        page = self._client_returning(raw).get_records(self.DOC, "QaSeed")

        assert page.records and not page.truncated
        assert all("[QATEST]" in r.fields["Name"] and r.id for r in page.records)
