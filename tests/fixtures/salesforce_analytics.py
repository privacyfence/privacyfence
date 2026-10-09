"""A synthetic Salesforce Analytics API for paging tests.

``FakeAnalytics.restful`` stands in for ``sf.restful`` and serves the two calls
``SalesforceClient.run_report_page`` makes: the ``describe`` of one saved report
and POSTed runs of it, answered from in-memory rows with Salesforce's 2,000-row
cut and RowCount aggregate. Shapes are checked against the recorded live run in
``tests/unit/test_salesforce_report_paging.py``.
"""
from __future__ import annotations

import re
from typing import Any, Callable

KEY = "Account.PF_QA_Number__c"
NAME = "ACCOUNT.NAME"
CITY = "BILLING_CITY"
TYPE = "TYPE"
COLUMNS = [KEY, NAME, CITY, TYPE]
CITIES = ["Boston", "Chicago", "Denver"]
REPORT_ID = "00OEXAMPLE0000001"


def make_rows(n: int, *, start: int = 1) -> list[dict]:
    """``n`` account rows numbered from ``start``; keys are zero-padded so str order is numeric order."""
    return [
        {
            KEY: f"PFQA-{i:05d}",
            NAME: f"Account {i}",
            CITY: CITIES[i % len(CITIES)],
            TYPE: "Customer" if i % 2 else "Partner",
        }
        for i in range(start, start + n)
    ]


def tabular_report(**overrides: Any) -> dict:
    """The saved ``reportMetadata`` of a tabular report with the four columns and no filters."""
    saved: dict[str, Any] = {
        "reportFormat": "TABULAR",
        "detailColumns": list(COLUMNS),
        "reportFilters": [],
        "reportBooleanFilter": None,
        "groupingsDown": [],
        "groupingsAcross": [],
        "aggregates": [],
        "sortBy": [],
        "topRows": None,
        "chart": None,
        "customSummaryFormula": None,
    }
    saved.update(overrides)
    return saved


_TOKEN_RE = re.compile(r"\s*(\(|\)|\d+|AND\b|OR\b|NOT\b)")


def _tokenize(logic: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    text = logic.strip()
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if match is None:
            raise AssertionError(f"unknown token in boolean filter {logic!r} at {pos}")
        tokens.append(match.group(1))
        pos = match.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return tokens


def _parse_logic(logic: str) -> Callable[[list[bool]], bool]:
    """Compile ``1 AND (2 OR NOT 3)`` to a function of the per-filter results (recursive descent, no eval)."""
    tokens = _tokenize(logic)
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        if pos >= len(tokens):
            raise AssertionError(f"boolean filter {logic!r} ended early")
        pos += 1
        return tokens[pos - 1]

    def parse_or() -> Callable[[list[bool]], bool]:
        node = parse_and()
        while peek() == "OR":
            take()
            left, right = node, parse_and()
            node = (lambda a, b: lambda r: a(r) or b(r))(left, right)
        return node

    def parse_and() -> Callable[[list[bool]], bool]:
        node = parse_not()
        while peek() == "AND":
            take()
            left, right = node, parse_not()
            node = (lambda a, b: lambda r: a(r) and b(r))(left, right)
        return node

    def parse_not() -> Callable[[list[bool]], bool]:
        if peek() == "NOT":
            take()
            inner = parse_not()
            return lambda r: not inner(r)
        return parse_atom()

    def parse_atom() -> Callable[[list[bool]], bool]:
        token = take()
        if token == "(":
            inner = parse_or()
            if take() != ")":
                raise AssertionError(f"unbalanced parentheses in boolean filter {logic!r}")
            return inner
        if not token.isdigit():
            raise AssertionError(f"unexpected token {token!r} in boolean filter {logic!r}")
        index = int(token) - 1
        return lambda r: r[index]

    tree = parse_or()
    if pos != len(tokens):
        raise AssertionError(f"trailing tokens in boolean filter {logic!r}")
    return tree


def _matches(row: dict, flt: dict) -> bool:
    column, operator = flt["column"], flt["operator"]
    if column not in row:
        raise AssertionError(f"filter on unknown column {column!r}")
    cell = str(row[column])
    values = str(flt["value"]).split(",")
    if operator == "equals":
        return cell in values
    if operator == "notEqual":
        return cell not in values
    if operator == "greaterThan":
        return any(cell > v for v in values)
    if operator == "lessThan":
        return any(cell < v for v in values)
    if operator == "contains":
        return any(v in cell for v in values)
    raise AssertionError(f"unsupported filter operator {operator!r}")


class FakeAnalytics:
    """sf.restful stand-in: serves describe and POSTed runs of one saved report from in-memory rows.

    ``ignore_greater_than`` makes a server that does not honour ``greaterThan``;
    ``blank_last`` sorts empty values after every other value (and, like
    ``greaterThan`` on Salesforce, a blank never matches it).
    """

    def __init__(
        self, saved_metadata: dict, columns: list[str], rows: list[dict], *, row_limit: int = 2000,
        extended: dict | None = None, ignore_greater_than: bool = False, blank_last: bool = False,
    ) -> None:
        self.saved = saved_metadata
        self.columns = columns
        self.rows = rows
        self.row_limit = row_limit
        self.extended = extended
        self.ignore_greater_than = ignore_greater_than
        self.blank_last = blank_last
        self.calls: list[tuple[str, dict | None]] = []

    def restful(self, path, params=None, method="GET", json=None) -> dict:
        report_path = f"analytics/reports/{REPORT_ID}"
        if method == "GET" and path == f"{report_path}/describe":
            self.calls.append((path, None))
            return {"reportMetadata": self.saved, "reportExtendedMetadata": self.extended or {}}
        if method == "POST" and path == report_path:
            assert params == {"includeDetails": "true"}, params
            assert json is not None and set(json) == {"reportMetadata"}, json
            self.calls.append((path, json["reportMetadata"]))
            return self._run(json["reportMetadata"])
        raise AssertionError(f"unexpected call {method} {path}")

    def _select(self, m: dict) -> list[dict]:
        filters = m.get("reportFilters") or []
        if self.ignore_greater_than:
            filters_kept = [(i, f) for i, f in enumerate(filters) if f["operator"] != "greaterThan"]
        else:
            filters_kept = list(enumerate(filters))
        logic = (m.get("reportBooleanFilter") or "").strip()
        tree = _parse_logic(logic) if logic else None
        selected = []
        for row in self.rows:
            results = [True] * len(filters)
            for i, flt in filters_kept:
                results[i] = _matches(row, flt)
            if (tree(results) if tree else all(results)):
                selected.append(row)
        return selected

    def _run(self, m: dict) -> dict:
        if m.get("reportFormat") != "TABULAR" and (m.get("groupingsDown") or m.get("groupingsAcross")):
            raise AssertionError("the client must flatten a grouped report before posting it")
        matched = self._select(m)
        if m.get("sortBy"):
            column = m["sortBy"][0]["sortColumn"]
            if self.blank_last:
                matched = sorted(matched, key=lambda r: (str(r[column]) == "", str(r[column])))
            else:
                matched = sorted(matched, key=lambda r: str(r[column]))
        shown = matched[: self.row_limit]
        aggregates = [
            {"label": str(len(matched)), "value": len(matched)} if name == "RowCount"
            else {"label": "0", "value": 0}
            for name in m.get("aggregates") or []
        ]
        return {
            "attributes": {"reportId": REPORT_ID, "reportName": "Fake report", "type": "Report"},
            "allData": len(matched) <= self.row_limit,
            "hasDetailRows": True,
            "reportMetadata": m,
            "reportExtendedMetadata": self.extended or {},
            "groupingsDown": {"groupings": []},
            "groupingsAcross": {"groupings": []},
            "factMap": {"T!T": {
                "rows": [
                    {"dataCells": [{"value": r[c], "label": r[c]} for c in m["detailColumns"]]}
                    for r in shown
                ],
                "aggregates": aggregates,
            }},
        }
