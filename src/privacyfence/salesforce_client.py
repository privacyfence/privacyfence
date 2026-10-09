"""Salesforce REST API client.

Uses the `simple-salesforce` library if available; otherwise raises a clear
error. Authentication is OAuth 2.0 (Web Server Flow with PKCE), driven from the
PrivacyFence Settings via ``authorize_interactive`` below — no username/
password/security-token entry. The Connected App (consumer key/secret) is
organization-level config installed via "Install/Update Organization Config…";
the resulting access/refresh token is per-user, stored in a token file.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re as _re
from dataclasses import dataclass
from typing import Any, Callable, TypeVar
from urllib.parse import urlencode

import requests

from .oauth_loopback import OAuthLoopbackError, run_browser_oauth
from .secure_files import atomic_write_json

logger = logging.getLogger(__name__)

SALESFORCE_OAUTH_PORT = 53683
SALESFORCE_REDIRECT_PATH = "/callback"
DEFAULT_LOGIN_URL = "https://login.salesforce.com"
DEFAULT_SCOPES = "api refresh_token"

T = TypeVar("T")


class SalesforceClientError(Exception):
    """Raised for unrecoverable Salesforce client problems (config, API)."""


DEFAULT_REPORT_MAX_PAGES = 50
REPORT_PAGING_REASONS = frozenset({
    "bad_page_by", "not_unique", "not_advancing", "page_limit", "not_flat", "rows_lost",
})


class ReportPagingError(SalesforceClientError):
    """A paged report read that cannot continue. ``reason`` is one of REPORT_PAGING_REASONS.

    The message never contains a cell value, only column API names and counts.
    """

    def __init__(self, reason: str, message: str) -> None:
        if reason not in REPORT_PAGING_REASONS:
            raise ValueError(f"unknown report paging reason: {reason!r}")
        super().__init__(message)
        self.reason = reason


@dataclass
class SalesforceReport:
    id: str
    name: str
    report_type: str
    folder_name: str
    description: str


@dataclass
class SalesforceRecord:
    object_type: str
    id: str
    fields: dict


@dataclass
class ReportFilter:
    column: str          # report column API name, e.g. "Opportunity.Opp_Id__c" or "ACCOUNT.NAME"
    operator: str        # one of REPORT_FILTER_OPERATORS
    values: list[str]    # one or more values; several values = match any (or: none, for negative operators)


@dataclass
class ReportPage:
    result: dict         # the run's report result, as Salesforce returned it
    keys: list[str]      # the page_by key text of each detail row, in row order
    all_data: bool       # Salesforce's allData for this run
    row_count: int       # the run's RowCount aggregate: every row matching this run, not only those returned


# ------------------------------------------------------------------ #
# SOSL query building — search() below assembles a query string from
# caller-supplied text, so every interpolated piece is validated or escaped
# first. Object/field names aren't quoted in SOSL, so they need identifier-
# level validation rather than string escaping; account_id is inserted as a
# quoted WHERE-clause literal, so it's validated against Salesforce's own ID
# format instead of just escaping quotes.
# ------------------------------------------------------------------ #

_OBJECT_TYPE_NAME_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SALESFORCE_ID_RE = _re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")
_SOSL_RESERVED_CHARS = '?&|!{}[]()^~*:"\'+-'


def _validate_object_type_name(name: str) -> str:
    name = name.strip()
    if not _OBJECT_TYPE_NAME_RE.match(name):
        raise SalesforceClientError(f"Invalid Salesforce object type name: {name!r}")
    return name


def _validate_salesforce_id(value: str, field_name: str = "id") -> str:
    value = value.strip()
    if not _SALESFORCE_ID_RE.match(value):
        raise SalesforceClientError(
            f"{field_name} must be a 15- or 18-character Salesforce ID, got {value!r}"
        )
    return value


def _escape_sosl_term(term: str) -> str:
    """Escape SOSL FIND-clause reserved characters so a search term can't
    break out of the FIND{...} clause or be misread as a SOSL operator —
    this is user/agent-supplied text going straight into a query string."""
    escaped = term.replace("\\", "\\\\")
    for ch in _SOSL_RESERVED_CHARS:
        escaped = escaped.replace(ch, "\\" + ch)
    return escaped


# ------------------------------------------------------------------ #
# Report run overrides — run_report() can narrow one run of a saved report
# (a subset of its own columns, extra row filters). Everything the caller
# supplies is validated before it is sent, and an override can only ever
# narrow the saved report: columns must be a subset of its own, and filters
# are ANDed onto its own. The approved_report_ids policy rule relies on that.
# ------------------------------------------------------------------ #

REPORT_FILTER_OPERATORS = frozenset({
    "equals", "notEqual", "lessThan", "greaterThan", "lessOrEqual", "greaterOrEqual",
    "contains", "notContain", "startsWith", "includes", "excludes", "within",
})
_NEGATIVE_REPORT_OPERATORS = frozenset({"notEqual", "notContain", "excludes"})   # chunks joined with AND
_MULTI_VALUE_REPORT_OPERATORS = frozenset({"equals", "notEqual", "contains", "notContain",
                                           "startsWith", "includes", "excludes"})
MAX_REPORT_FILTERS = 20              # Salesforce's per-report filter limit
_REPORT_FILTER_CHUNK_VALUES = 10     # max values joined into one filter's comma list
_REPORT_FILTER_CHUNK_CHARS = 240     # max length of that joined value string
_REPORT_COLUMN_RE = _re.compile(r"^[A-Za-z0-9_.$]+$")   # $ appears in custom-report-type columns, e.g. Account$Name


def _chunk_report_values(values: list[str]) -> list[list[str]]:
    chunks: list[list[str]] = []
    for value in values:
        if chunks:
            candidate = chunks[-1] + [value]
            if (len(candidate) <= _REPORT_FILTER_CHUNK_VALUES
                    and len(",".join(candidate)) <= _REPORT_FILTER_CHUNK_CHARS):
                chunks[-1] = candidate
                continue
        chunks.append([value])
    return chunks


def build_report_metadata(
    saved: dict, columns: list[str] | None, filters: list[ReportFilter] | None,
) -> dict:
    """The reportMetadata to POST for one narrowed run of a saved report.

    ``saved`` is the ``reportMetadata`` from the report's describe call and is
    not mutated. Raises SalesforceClientError for anything that isn't a
    narrowing of the saved report.
    """
    metadata = copy.deepcopy(saved)

    if columns:
        saved_columns = saved.get("detailColumns") or []
        requested: list[str] = []
        for raw_name in columns:
            name = raw_name.strip()
            if name not in saved_columns:
                raise SalesforceClientError(
                    f"column {name!r} is not one of this report's columns: {', '.join(saved_columns)}"
                )
            if name in requested:
                raise SalesforceClientError(f"column {name!r} is listed twice")
            requested.append(name)
        metadata["detailColumns"] = requested

    if filters:
        report_filters = list(metadata.get("reportFilters") or [])
        n_saved = len(report_filters)
        groups: list[str] = []
        for flt in filters:
            column, operator = flt.column, flt.operator
            if not _REPORT_COLUMN_RE.match(column):
                raise SalesforceClientError(f"Invalid report filter column: {column!r}")
            if operator not in REPORT_FILTER_OPERATORS:
                raise SalesforceClientError(
                    f"Unknown report filter operator {operator!r}; "
                    f"use one of: {', '.join(sorted(REPORT_FILTER_OPERATORS))}"
                )
            values = [v for v in (str(raw).strip() for raw in flt.values) if v]
            if not values:
                raise SalesforceClientError(f"report filter on {column!r} has no values")
            for value in values:
                if "," in value:
                    raise SalesforceClientError(
                        f"report filter value {value!r} contains a comma, "
                        "which Salesforce reads as a list separator"
                    )
            if operator not in _MULTI_VALUE_REPORT_OPERATORS and len(values) > 1:
                raise SalesforceClientError(f"operator {operator!r} takes exactly one value")
            indices = []
            for chunk in _chunk_report_values(values):
                report_filters.append({"column": column, "operator": operator, "value": ",".join(chunk)})
                indices.append(str(len(report_filters)))
            if len(indices) == 1:
                groups.append(indices[0])
            else:
                joiner = " AND " if operator in _NEGATIVE_REPORT_OPERATORS else " OR "
                groups.append("(" + joiner.join(indices) + ")")
        if len(report_filters) > MAX_REPORT_FILTERS:
            raise SalesforceClientError(
                f"report would have {len(report_filters)} filters; Salesforce allows at most "
                f"{MAX_REPORT_FILTERS}. Pass fewer values or fewer filters."
            )
        saved_logic = saved.get("reportBooleanFilter")
        if isinstance(saved_logic, str) and saved_logic.strip():
            base = f"({saved_logic})"
        elif n_saved > 0:
            base = " AND ".join(str(i) for i in range(1, n_saved + 1))
        else:
            base = ""
        metadata["reportFilters"] = report_filters
        metadata["reportBooleanFilter"] = " AND ".join([base, *groups] if base else groups)

    return metadata


# ------------------------------------------------------------------ #
# Keyset paging — Salesforce returns at most 2,000 detail rows per run. A
# paged read runs the saved report once per page, sorted by a unique column
# (page_by) and filtered to values after the previous page's last one. A paged
# read finishes or fails; it never returns a silently cut result. No error
# message here contains a cell value: MCP errors reach the AI client before any
# approval card, so a value in a message would bypass the gate.
# ------------------------------------------------------------------ #

_PAGE_ROWS_KEY = "T!T"
_NOT_FLAT_MESSAGE = "Salesforce did not return the report as one table, so it cannot be paged"
_UNFLATTENABLE_GRANULARITIES = frozenset({None, "None", "Day"})


def build_keyset_metadata(
    saved: dict, columns: list[str] | None, filters: list[ReportFilter] | None,
    page_by: str, after: str | None,
) -> dict:
    """The reportMetadata to POST for one keyset page of a saved report.

    Sorted ascending by ``page_by`` and, when ``after`` is given, narrowed to
    rows whose ``page_by`` is greater than it. A grouped report is flattened to
    a tabular run, which never shows more than the saved report does. ``saved``
    is not mutated.
    """
    if saved.get("topRows"):
        raise ReportPagingError(
            "bad_page_by",
            "page_by cannot page a report that has a row limit; remove the row limit from the saved report",
        )
    if saved.get("reportFormat") == "MULTI_BLOCK":
        raise ReportPagingError("bad_page_by", "page_by cannot page a joined report")

    flatten = saved.get("reportFormat") != "TABULAR"
    grouping_columns: list[str] = []
    if flatten:
        for grouping in [*(saved.get("groupingsDown") or []), *(saved.get("groupingsAcross") or [])]:
            if grouping.get("dateGranularity") not in _UNFLATTENABLE_GRANULARITIES:
                raise ReportPagingError(
                    "bad_page_by",
                    "page_by cannot page a report grouped by week, month, quarter or year: "
                    "reading it as one table would show exact dates the saved report does not",
                )
            if grouping["name"] not in grouping_columns:
                grouping_columns.append(grouping["name"])

    extra_filters = list(filters or [])
    if after is None:
        # Reserve the key filter's slot so a read that would exceed the filter
        # limit on page 2 fails on page 1, before any approval card.
        build_report_metadata(saved, columns, [*extra_filters, ReportFilter(page_by, "greaterThan", ["0"])])
    else:
        extra_filters.append(ReportFilter(page_by, "greaterThan", [after]))
    metadata = build_report_metadata(saved, columns, extra_filters)

    if flatten:
        cols = metadata.get("detailColumns") or []
        metadata["reportFormat"] = "TABULAR"
        metadata["groupingsDown"] = []
        metadata["groupingsAcross"] = []
        metadata["aggregates"] = ["RowCount"]
        metadata["chart"] = None
        metadata["customSummaryFormula"] = None
        metadata["detailColumns"] = [g for g in grouping_columns if g not in cols] + cols

    final_columns = metadata.get("detailColumns") or []
    if page_by not in final_columns:
        raise ReportPagingError(
            "bad_page_by",
            f"page_by {page_by!r} is not a column of this run: {', '.join(final_columns)}",
        )
    metadata["sortBy"] = [{"sortColumn": page_by, "sortOrder": "Asc"}]
    aggregates = list(metadata.get("aggregates") or [])
    if "RowCount" not in aggregates:
        aggregates.append("RowCount")
    metadata["aggregates"] = aggregates
    return metadata


def _key_text(value: Any) -> str | None:
    """A cell value as page key text, or None if it cannot be used to page."""
    if isinstance(value, str):
        if value and "," not in value and value == value.strip():
            return value
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return None


def report_page_keys(
    result: dict, page_by: str, after: str | None, remaining: int | None,
) -> tuple[list[str], int]:
    """The ``page_by`` key text of each row of one page, and the run's RowCount.

    Raises ReportPagingError when the page cannot be trusted to continue a
    complete read: the keys are unusable, repeat, do not advance, or rows were
    lost between pages.
    """
    not_flat = ReportPagingError("not_flat", _NOT_FLAT_MESSAGE)
    try:
        fact_map = result["factMap"]
        if set(fact_map) != {_PAGE_ROWS_KEY}:
            raise not_flat
        table = fact_map[_PAGE_ROWS_KEY]
        rows = table["rows"]
        meta = result["reportMetadata"]
        index = meta["detailColumns"].index(page_by)
        cells = [row["dataCells"][index].get("value") for row in rows]
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise not_flat from None

    keys: list[str] = []
    for cell in cells:
        key = _key_text(cell)
        if key is None:
            raise ReportPagingError(
                "bad_page_by",
                f"page_by {page_by!r} has a value that cannot be paged by "
                "(empty, containing a comma, or not text or a number); choose an auto-number column",
            )
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise ReportPagingError(
            "not_unique", f"page_by {page_by!r} is not unique: a value repeats within one page",
        )
    if after is not None and after in keys:
        raise ReportPagingError(
            "not_advancing",
            f"page_by {page_by!r} did not advance: Salesforce returned the previous page's last value "
            "again, so the column cannot be paged with greaterThan; choose an auto-number column",
        )
    all_data = result.get("allData") is not False
    if not keys and not all_data:
        raise ReportPagingError(
            "not_advancing",
            f"page_by {page_by!r} did not advance: a page had no rows but Salesforce reported more",
        )
    try:
        row_count = table["aggregates"][(meta.get("aggregates") or []).index("RowCount")]["value"]
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise not_flat from None
    if isinstance(row_count, bool) or not isinstance(row_count, int):
        raise not_flat
    if remaining is not None and row_count != remaining:
        raise ReportPagingError(
            "rows_lost",
            f"page_by {page_by!r} lost rows between pages: {remaining} rows were left but the next "
            f"page matched {row_count}; the column has repeated or blank values, compares "
            "case-insensitively, or the report changed during the read",
        )
    if all_data and len(keys) != row_count:
        raise ReportPagingError(
            "rows_lost", f"page_by {page_by!r} lost rows: the last page returned {len(keys)} of {row_count} rows",
        )
    return keys, row_count


def report_page_info(number: int, rows_before: int, count: int, more: bool) -> dict:
    """The ``page`` block attached to a paged report result."""
    return {
        "number": number,
        "first_row": rows_before + 1 if count else 0,
        "last_row": rows_before + count if count else 0,
        "more": more,
    }


def build_authorize_url(
    consumer_key: str, redirect_uri: str, state: str, code_challenge: str,
    login_url: str = DEFAULT_LOGIN_URL,
) -> str:
    """Salesforce's OAuth 2.0 Web Server flow authorize URL -- separate
    from ``authorize_interactive`` so ``web/routes_connect.py``'s
    org-mode server-redirect flow can build the same URL without going
    through ``oauth_loopback.run_browser_oauth``'s local listener."""
    login_url = (login_url or DEFAULT_LOGIN_URL).rstrip("/")
    params = {
        "response_type": "code",
        "client_id": consumer_key,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": DEFAULT_SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{login_url}/services/oauth2/authorize?" + urlencode(params)


def exchange_code(
    consumer_key: str, consumer_secret: str, code: str, redirect_uri: str, code_verifier: str,
    login_url: str = DEFAULT_LOGIN_URL,
) -> dict[str, Any]:
    """Exchanges an authorization code for a Salesforce token and returns
    the normalized token record -- *not* yet saved to disk (see
    ``save_token_file`` below). Shared by ``authorize_interactive``'s
    local-mode loopback flow and org mode's server-redirect flow;
    raises ``SalesforceClientError`` on any failure."""
    login_url = (login_url or DEFAULT_LOGIN_URL).rstrip("/")
    try:
        resp = requests.post(
            f"{login_url}/services/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": consumer_key,
                "client_secret": consumer_secret,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SalesforceClientError(f"Salesforce OAuth exchange failed: {exc}") from exc
    response = resp.json()

    access_token = response.get("access_token", "")
    instance_url = response.get("instance_url", "")
    if not access_token or not instance_url:
        raise SalesforceClientError(f"Salesforce OAuth did not return a usable token: {response}")

    logger.info("Salesforce OAuth complete for instance %s", instance_url)
    return {
        "access_token": access_token,
        "refresh_token": response.get("refresh_token", ""),
        "instance_url": instance_url,
    }


def save_token_file(token_file: str, token_record: dict[str, Any]) -> None:
    _save_token_file(token_file, token_record)


def authorize_interactive(
    consumer_key: str,
    consumer_secret: str,
    token_file: str,
    login_url: str = DEFAULT_LOGIN_URL,
    port: int = SALESFORCE_OAUTH_PORT,
) -> dict[str, Any]:
    """Run Salesforce's OAuth 2.0 Web Server flow and persist the token.

    ``consumer_key``/``consumer_secret``/``login_url`` come from the
    organization config bundle (the Connected App IT registered). Returns the
    saved token record; raises ``SalesforceClientError`` on failure.
    """

    def _build(redirect_uri: str, state: str, code_challenge: str) -> str:
        return build_authorize_url(consumer_key, redirect_uri, state, code_challenge, login_url)

    def _exchange(code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
        return exchange_code(consumer_key, consumer_secret, code, redirect_uri, code_verifier, login_url)

    try:
        token_record = run_browser_oauth(
            _build, _exchange, port=port, path=SALESFORCE_REDIRECT_PATH, redirect_host="localhost",
        )
    except OAuthLoopbackError as exc:
        raise SalesforceClientError(f"Salesforce sign-in failed: {exc}") from exc

    _save_token_file(token_file, token_record)
    return token_record


def load_token_file(token_file: str) -> dict[str, Any]:
    """Load a previously saved Salesforce token record, or raise SalesforceClientError."""
    if not os.path.exists(token_file):
        raise SalesforceClientError(
            f"No Salesforce token found at '{token_file}'. Use Authenticate… in "
            "PrivacyFence Settings to sign in."
        )
    with open(token_file, encoding="utf-8") as fh:
        return json.load(fh)


def _save_token_file(token_file: str, token_record: dict[str, Any]) -> None:
    atomic_write_json(token_file, token_record)


def _oauth_error_detail(exc: requests.RequestException) -> str:
    """The ``error``/``error_description`` pair from a token-endpoint error
    response, e.g. `` (invalid_grant: expired access/refresh token)``, or
    ``""``. A bare "400 Bad Request" can't tell a spent refresh token from a
    revoked app or an IP restriction; these two fields can, and carry no
    credential."""
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict) or not body.get("error"):
        return ""
    description = body.get("error_description")
    return f" ({body['error']}: {description})" if description else f" ({body['error']})"


def _is_expired_session_error(exc: Exception) -> bool:
    text = str(exc)
    return "INVALID_SESSION_ID" in text or "Session expired" in text


class SalesforceClient:
    """Salesforce client backed by simple-salesforce, authenticated via OAuth.

    ``config`` merges organization-level Connected App credentials
    (``consumer_key``, ``consumer_secret``, ``login_url``) with the per-user
    token (``access_token``, ``refresh_token``, ``instance_url``). When the
    access token expires mid-session, the client refreshes it once and
    retries automatically; if ``token_file`` is given, the refreshed token is
    persisted back to disk.
    """

    def __init__(self, config: dict[str, Any], token_file: str | None = None) -> None:
        self._config = dict(config)
        self._token_file = token_file
        self._sf = None  # lazily initialized
        self.report_max_pages = DEFAULT_REPORT_MAX_PAGES

    def _build_sf(self):
        try:
            from simple_salesforce import Salesforce
        except ImportError as exc:
            raise SalesforceClientError(
                "The 'simple-salesforce' package is not installed. "
                "Run: pip install simple-salesforce"
            ) from exc

        access_token = self._config.get("access_token", "")
        instance_url = self._config.get("instance_url", "")
        if not access_token or not instance_url:
            raise SalesforceClientError(
                "Salesforce is not authenticated. Use Authenticate… in the "
                "PrivacyFence Settings to sign in."
            )
        instance = instance_url.replace("https://", "").replace("http://", "").rstrip("/")
        try:
            return Salesforce(instance=instance, session_id=access_token)
        except Exception as exc:
            raise SalesforceClientError(f"Salesforce authentication failed: {exc}") from exc

    def _get_sf(self):
        if self._sf is None:
            self._sf = self._build_sf()
        return self._sf

    def _try_refresh(self) -> bool:
        """Attempt to refresh the access token in place. Returns True on success."""
        refresh_token = self._config.get("refresh_token", "")
        consumer_key = self._config.get("consumer_key", "")
        consumer_secret = self._config.get("consumer_secret", "")
        login_url = (self._config.get("login_url") or DEFAULT_LOGIN_URL).rstrip("/")
        if not refresh_token or not consumer_key or not consumer_secret:
            return False
        try:
            resp = requests.post(
                f"{login_url}/services/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": consumer_key,
                    "client_secret": consumer_secret,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.warning("Salesforce token refresh failed: %s%s", exc, _oauth_error_detail(exc))
            return False

        self._config["access_token"] = data.get("access_token", self._config.get("access_token"))
        self._config["instance_url"] = data.get("instance_url", self._config.get("instance_url"))
        # With refresh token rotation on (an External Client App option), the
        # response carries a new refresh token and the one just sent is spent.
        # Persisting the old one works until the next access-token expiry, then
        # every refresh fails with 400 invalid_grant.
        self._config["refresh_token"] = data.get("refresh_token") or refresh_token
        self._sf = None
        if self._token_file:
            _save_token_file(self._token_file, {
                "access_token": self._config["access_token"],
                "refresh_token": self._config["refresh_token"],
                "instance_url": self._config["instance_url"],
            })
        logger.info("Salesforce access token refreshed")
        return True

    def _call(self, fn: Callable[[Any], T]) -> T:
        """Run ``fn(sf)`` with one automatic refresh-and-retry on an expired session."""
        try:
            return fn(self._get_sf())
        except SalesforceClientError:
            raise
        except Exception as exc:
            if _is_expired_session_error(exc) and self._try_refresh():
                try:
                    return fn(self._get_sf())
                except Exception as retry_exc:
                    raise SalesforceClientError(str(retry_exc)) from retry_exc
            raise SalesforceClientError(str(exc)) from exc

    def check_connection(self) -> str:
        """Verify credentials. Returns the org name."""
        def _run(sf):
            result = sf.query("SELECT Id, Name FROM Organization LIMIT 1")
            records = result.get("records", [])
            return records[0].get("Name", "unknown") if records else "unknown"

        org_name = self._call(_run)
        logger.info("Connected to Salesforce org: %s", org_name)
        return org_name

    def list_reports(self) -> list[SalesforceReport]:
        """List reports accessible to the authenticated user."""
        def _run(sf):
            return sf.query(
                "SELECT Id, Name, Description, FolderName, DeveloperName "
                "FROM Report ORDER BY Name LIMIT 200"
            )

        result = self._call(_run)
        reports = [
            SalesforceReport(
                id=raw.get("Id", ""),
                name=raw.get("Name", ""),
                report_type=raw.get("DeveloperName", ""),
                folder_name=raw.get("FolderName", ""),
                description=raw.get("Description", ""),
            )
            for raw in result.get("records", [])
        ]
        logger.info("list_reports returned %d report(s)", len(reports))
        return reports

    def get_record(self, object_type: str, record_id: str) -> SalesforceRecord:
        """Fetch a single record by object type and id."""
        if not object_type or not record_id:
            raise SalesforceClientError("get_record requires object_type and record_id")

        def _run(sf):
            try:
                obj = getattr(sf, object_type)
            except AttributeError as exc:
                raise SalesforceClientError(f"Unknown Salesforce object type: {object_type!r}") from exc
            return obj.get(record_id)

        raw = self._call(_run)
        fields = {k: v for k, v in raw.items() if not k.startswith("attributes")}
        return SalesforceRecord(object_type=object_type, id=record_id, fields=fields)

    def search(
        self, search_term: str, object_types: str = "", account_id: str = "", max_results: int = 20,
    ) -> list[SalesforceRecord]:
        """Search Salesforce by name or id, the same mechanism (SOSL) behind
        the search bar at the top of the Salesforce UI.

        Returns lightweight Id/Name matches per requested object type — call
        get_record for full field details on a match, the same
        search-then-drill-in split ``jira_search_issues``/``jira_get_issue``
        already use.

        ``object_types`` is a comma-separated list of Salesforce object API
        names (e.g. "Opportunity,Contact"); leave empty to search
        Salesforce's default set of globally-searchable objects.
        ``account_id`` scopes results to one Account's related records
        (``WHERE AccountId = ...``) and requires ``object_types`` to be
        given, since not every Salesforce object has an AccountId field —
        there's no single scoping clause that's valid for an unspecified
        object.
        """
        if not search_term or not search_term.strip():
            raise SalesforceClientError("search requires a non-empty search_term")
        types = [_validate_object_type_name(t) for t in object_types.split(",") if t.strip()]
        if account_id and not types:
            raise SalesforceClientError("search: account_id requires object_types to be specified")
        max_results = max(1, min(int(max_results), 200))
        escaped_term = _escape_sosl_term(search_term.strip())

        if types:
            account_id_valid = _validate_salesforce_id(account_id, "account_id") if account_id else ""
            clauses = []
            for obj in types:
                clause = f"{obj}(Id, Name"
                if account_id_valid:
                    clause += f" WHERE AccountId = '{account_id_valid}'"
                clause += f" LIMIT {max_results})"
                clauses.append(clause)
            sosl = f"FIND {{{escaped_term}}} IN ALL FIELDS RETURNING {', '.join(clauses)}"
        else:
            sosl = f"FIND {{{escaped_term}}} IN ALL FIELDS LIMIT {max_results}"

        def _run(sf):
            return sf.search(sosl)

        raw = self._call(_run)
        result_records = raw.get("searchRecords", []) if isinstance(raw, dict) else []
        records = []
        for r in result_records:
            attrs = r.get("attributes") or {}
            fields = {k: v for k, v in r.items() if k != "attributes"}
            records.append(
                SalesforceRecord(object_type=attrs.get("type", ""), id=r.get("Id", ""), fields=fields)
            )
        logger.info("search %r returned %d record(s)", search_term, len(records))
        return records

    def run_report(
        self, report_id: str, columns: list[str] | None = None,
        filters: list[ReportFilter] | None = None, summary_only: bool = False,
    ) -> dict:
        """Run a Salesforce report and return its result as a dict.

        With no ``columns``/``filters`` the saved report runs as is. Otherwise
        the saved definition is fetched first and narrowed for this one run;
        the saved report itself never changes. Detail rows are requested
        explicitly (``includeDetails``) because Salesforce's default may
        return aggregates only.
        """
        if not report_id:
            raise SalesforceClientError("run_report requires a report_id")
        params = {"includeDetails": "false" if summary_only else "true"}
        path = f"analytics/reports/{report_id}"

        def _run(sf):
            if not columns and not filters:
                return sf.restful(path, params=params)
            describe = sf.restful(f"{path}/describe")
            saved = (describe.get("reportMetadata") if isinstance(describe, dict) else None) or {}
            metadata = build_report_metadata(saved, columns, filters)
            return sf.restful(path, params=params, method="POST", json={"reportMetadata": metadata})

        result = self._call(_run)
        logger.info("run_report %s completed (%s)", report_id, "summary" if summary_only else "details")
        return result

    def run_report_page(
        self, report_id: str, page_by: str, columns: list[str] | None = None,
        filters: list[ReportFilter] | None = None, after: str | None = None, pages_done: int = 0,
        remaining: int | None = None,
    ) -> ReportPage:
        """Run one keyset page of a saved report, in order of the unique column ``page_by``.

        ``after`` is the last ``page_by`` value of the previous page,
        ``pages_done`` the runs made so far and ``remaining`` the rows that were
        left after the previous page. Raises ReportPagingError if the read
        cannot continue; the saved report never changes.
        """
        if not report_id:
            raise SalesforceClientError("run_report_page requires a report_id")
        if pages_done >= self.report_max_pages:
            raise ReportPagingError(
                "page_limit",
                f"stopped after {self.report_max_pages} pages without reaching the end of the report; "
                "narrow it with filters or raise salesforce.report_max_pages",
            )
        path = f"analytics/reports/{report_id}"

        def _run(sf):
            describe = sf.restful(f"{path}/describe")
            saved = (describe.get("reportMetadata") if isinstance(describe, dict) else None) or {}
            metadata = build_keyset_metadata(saved, columns, filters, page_by, after)
            result = sf.restful(
                path, params={"includeDetails": "true"}, method="POST", json={"reportMetadata": metadata},
            )
            keys, row_count = report_page_keys(result, page_by, after, remaining)
            return ReportPage(result, keys, result.get("allData") is not False, row_count)

        page = self._call(_run)
        logger.info("run_report_page %s completed (%d rows)", report_id, len(page.keys))
        return page
