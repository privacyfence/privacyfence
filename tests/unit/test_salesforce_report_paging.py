"""Client-level keyset paging of Salesforce reports against a synthetic Analytics server.

``FakeAnalytics`` (tests/fixtures/salesforce_analytics.py) answers the describe and
POSTed runs ``run_report_page`` makes, with Salesforce's 2,000-row cut and RowCount
aggregate, so a 4,500-row report is read end to end without a Salesforce org.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from privacyfence.salesforce_client import (
    ReportFilter,
    ReportPage,
    ReportPagingError,
    SalesforceClient,
)
from tests.fixtures.salesforce_analytics import (
    CITY,
    COLUMNS,
    KEY,
    NAME,
    REPORT_ID,
    TYPE,
    FakeAnalytics,
    make_rows,
    tabular_report,
)

RECORDED = (
    Path(__file__).parent.parent / "fixtures" / "live" / "salesforce" / "run_report_page.json"
)


def client_for(fake: FakeAnalytics, monkeypatch: pytest.MonkeyPatch) -> SalesforceClient:
    client = SalesforceClient(config={"access_token": "tok", "instance_url": "https://my.salesforce.com"})
    monkeypatch.setattr(client, "_get_sf", lambda: SimpleNamespace(restful=fake.restful))
    return client


def read_all(
    client: SalesforceClient, page_by: str = KEY, filters: list[ReportFilter] | None = None,
    pages: list[ReportPage] | None = None,
) -> list[ReportPage]:
    """Page a report the way the tool does; ``pages`` receives each page before the next call can fail."""
    pages = [] if pages is None else pages
    after: str | None = None
    remaining: int | None = None
    while True:
        page = client.run_report_page(
            REPORT_ID, page_by, filters=filters, after=after, pages_done=len(pages), remaining=remaining,
        )
        pages.append(page)
        if page.all_data:
            return pages
        after = page.keys[-1]
        remaining = page.row_count - len(page.keys)


def rows_of(page: ReportPage) -> list[dict]:
    return page.result["factMap"]["T!T"]["rows"]


def test_4500_rows_come_back_once_in_key_order_over_three_pages(monkeypatch):
    rows = make_rows(4500)
    client = client_for(FakeAnalytics(tabular_report(), COLUMNS, rows), monkeypatch)

    pages = read_all(client)

    assert [len(p.keys) for p in pages] == [2000, 2000, 500]
    keys = [k for p in pages for k in p.keys]
    assert keys == [r[KEY] for r in rows]
    assert len(set(keys)) == 4500


def test_custom_filter_logic_is_anded_with_the_key_filter(monkeypatch):
    rows = make_rows(4500)
    saved = tabular_report(
        reportFilters=[
            {"column": CITY, "operator": "equals", "value": "Boston"},
            {"column": TYPE, "operator": "equals", "value": "Partner"},
        ],
        reportBooleanFilter="1 OR 2",
    )
    fake = FakeAnalytics(saved, COLUMNS, rows)
    client = client_for(fake, monkeypatch)

    pages = read_all(client, filters=[ReportFilter(NAME, "notEqual", ["Account 5"])])

    expected = [
        r[KEY] for r in rows
        if (r[CITY] == "Boston" or r[TYPE] == "Partner") and r[NAME] != "Account 5"
    ]
    assert len(expected) > 2000
    assert len(pages) >= 2
    assert [k for p in pages for k in p.keys] == expected
    posted = fake.calls[-1][1]
    assert posted["reportBooleanFilter"].startswith("(1 OR 2) AND ")


def test_summary_report_is_flattened_and_paged(monkeypatch):
    rows = make_rows(2500)
    saved = tabular_report(
        reportFormat="SUMMARY",
        detailColumns=[KEY, NAME, TYPE],
        groupingsDown=[{"name": CITY, "dateGranularity": "None", "sortOrder": "Asc"}],
        aggregates=["s!Account.Amount"],
    )
    client = client_for(FakeAnalytics(saved, COLUMNS, rows), monkeypatch)

    pages = read_all(client)

    assert [len(p.keys) for p in pages] == [2000, 500]
    for page in pages:
        assert set(page.result["factMap"]) == {"T!T"}
        assert page.result["reportMetadata"]["detailColumns"][0] == CITY
        assert page.result["reportMetadata"]["groupingsDown"] == []
        assert all(r["dataCells"][0]["value"] in {"Boston", "Chicago", "Denver"} for r in rows_of(page))
    assert [k for p in pages for k in p.keys] == [r[KEY] for r in rows]


class TestRefusals:
    def test_unknown_page_by(self, monkeypatch):
        client = client_for(FakeAnalytics(tabular_report(), COLUMNS, make_rows(10)), monkeypatch)
        pages: list[ReportPage] = []
        with pytest.raises(ReportPagingError) as exc:
            read_all(client, page_by="NOT_A_COLUMN", pages=pages)
        assert exc.value.reason == "bad_page_by"
        assert pages == []

    def test_non_unique_column(self, monkeypatch):
        client = client_for(FakeAnalytics(tabular_report(), COLUMNS, make_rows(4500)), monkeypatch)
        pages: list[ReportPage] = []
        with pytest.raises(ReportPagingError) as exc:
            read_all(client, page_by=TYPE, pages=pages)
        assert exc.value.reason == "not_unique"
        assert pages == []

    def test_server_that_ignores_greater_than(self, monkeypatch):
        fake = FakeAnalytics(tabular_report(), COLUMNS, make_rows(4500), ignore_greater_than=True)
        client = client_for(fake, monkeypatch)
        pages: list[ReportPage] = []
        with pytest.raises(ReportPagingError) as exc:
            read_all(client, pages=pages)
        assert exc.value.reason == "not_advancing"
        assert len(pages) == 1  # page 1 was fine; the refusal came on page 2

    def test_page_limit(self, monkeypatch):
        client = client_for(FakeAnalytics(tabular_report(), COLUMNS, make_rows(4500)), monkeypatch)
        client.report_max_pages = 2
        pages: list[ReportPage] = []
        with pytest.raises(ReportPagingError) as exc:
            read_all(client, pages=pages)
        assert exc.value.reason == "page_limit"
        assert len(pages) == 2

    def test_duplicate_key_straddling_a_page_boundary(self, monkeypatch):
        rows = make_rows(4500)
        rows[2000][KEY] = rows[1999][KEY]  # rows 2000 and 2001 share a key
        client = client_for(FakeAnalytics(tabular_report(), COLUMNS, rows), monkeypatch)
        pages: list[ReportPage] = []
        with pytest.raises(ReportPagingError) as exc:
            read_all(client, pages=pages)
        assert exc.value.reason == "rows_lost"
        assert len(pages) == 1

    def test_blank_keys_sorting_last(self, monkeypatch):
        rows = make_rows(2500)
        rows[-1][KEY] = ""
        fake = FakeAnalytics(tabular_report(), COLUMNS, rows, blank_last=True)
        client = client_for(fake, monkeypatch)
        pages: list[ReportPage] = []
        with pytest.raises(ReportPagingError) as exc:
            read_all(client, pages=pages)
        assert exc.value.reason == "rows_lost"
        assert len(pages) == 1

    def test_no_message_holds_a_row_value(self, monkeypatch):
        rows = make_rows(4500)
        rows[2000][KEY] = rows[1999][KEY]
        client = client_for(FakeAnalytics(tabular_report(), COLUMNS, rows), monkeypatch)
        with pytest.raises(ReportPagingError) as exc:
            read_all(client)
        assert "PFQA-" not in str(exc.value)


def test_fake_matches_recorded_shape():
    if not RECORDED.exists():
        pytest.skip("run_report_page.json not recorded yet")
    recorded = json.loads(RECORDED.read_text())
    saved = tabular_report(aggregates=["RowCount"], sortBy=[{"sortColumn": KEY, "sortOrder": "Asc"}])
    ours = FakeAnalytics(saved, COLUMNS, make_rows(3)).restful(
        f"analytics/reports/{REPORT_ID}", params={"includeDetails": "true"}, method="POST",
        json={"reportMetadata": saved},
    )

    assert set(ours) <= set(recorded)
    assert set(ours["factMap"]["T!T"]) <= set(recorded["factMap"]["T!T"])
    recorded_cell_keys = {k for row in recorded["factMap"]["T!T"]["rows"] for c in row["dataCells"] for k in c}
    our_cell_keys = {k for row in ours["factMap"]["T!T"]["rows"] for c in row["dataCells"] for k in c}
    assert our_cell_keys <= recorded_cell_keys
