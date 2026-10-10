"""Grist REST client.

The credential (an OAuth access token or a personal API key) comes from
``grist_auth``. The client never follows a redirect and logs the host and
counts only, never a credential or a cell value.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests

from .grist_auth import GristAccessDenied, GristClientError, GristCredential

__all__ = [
    "GristAccessDenied",
    "GristClient",
    "GristClientError",
    "GristColumn",
    "GristDocument",
    "GristRecord",
    "GristRecordPage",
    "GristTable",
    "validate_doc_id",
    "validate_identifier",
]

logger = logging.getLogger(__name__)

DOC_ID_RE = re.compile(r"[A-Za-z0-9_~-]{1,128}")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

_TIMEOUT = 30
_DETAIL_LIMIT = 200
_MAX_ORGS = 20
_MAX_DOCUMENTS = 500
_MAX_TABLES = 100

_UNAUTHORIZED = (
    "Grist refused the sign-in (HTTP 401). The API key or sign-in may be wrong, expired or revoked. "
    "Use Authenticate… in PrivacyFence Settings to connect again."
)
_FORBIDDEN = (
    "Grist refused the request (HTTP 403). Your Grist account, or what you allowed PrivacyFence "
    "when signing in, does not cover it."
)
_NOT_FOUND = "Grist found no such document, table or record (HTTP 404)."


@dataclass
class GristDocument:
    id: str
    name: str
    workspace: str
    team: str


@dataclass
class GristColumn:
    id: str
    label: str
    type: str
    is_formula: bool


@dataclass
class GristTable:
    id: str
    columns: list[GristColumn] = field(default_factory=list)


@dataclass
class GristRecord:
    id: int
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class GristRecordPage:
    records: list[GristRecord]
    truncated: bool
    next_after_id: int | None = None


def validate_doc_id(value: str) -> str:
    """The document id, or GristClientError: ids go into the URL path."""
    if not isinstance(value, str) or not DOC_ID_RE.fullmatch(value):
        raise GristClientError(f"Not a Grist document id: {value!r}")
    return value


def validate_identifier(value: str, kind: str) -> str:
    """A table or column id (``kind`` is ``"table"`` or ``"column"``), or GristClientError."""
    if not isinstance(value, str) or not IDENT_RE.fullmatch(value):
        raise GristClientError(f"Not a Grist {kind} id: {value!r}")
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


class GristClient:
    """Grist REST calls for one server and one credential."""

    def __init__(self, server_url: str, credential: GristCredential) -> None:
        self._server_url = server_url
        self._credential = credential
        self._session = requests.Session()

    def __repr__(self) -> str:
        return f"GristClient({self.host})"

    @property
    def host(self) -> str:
        return urlsplit(self._server_url).netloc

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #

    def _send(
        self, method: str, path: str, token: str, params: dict[str, str] | None, json_body: Any,
    ) -> requests.Response:
        try:
            return self._session.request(
                method,
                self._server_url + path,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise GristClientError(
                f"Could not reach the Grist server at {self.host}: {type(exc).__name__}"
            ) from None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        resp = self._send(method, path, self._credential.access_token(), params, json_body)
        if resp.status_code == 401 and self._credential.can_refresh:
            resp = self._send(
                method, path, self._credential.access_token(force_refresh=True), params, json_body,
            )
        status = resp.status_code
        logger.debug("Grist %s %s -> HTTP %s", method, self.host, status)
        if 300 <= status < 400:
            raise GristClientError(
                f"The Grist server answered with a redirect (HTTP {status}). Check the Grist server address."
            )
        if status == 401:
            raise GristClientError(_UNAUTHORIZED)
        if status == 403:
            raise GristAccessDenied(_FORBIDDEN)
        if status == 404:
            raise GristClientError(_NOT_FOUND)
        if status >= 400:
            detail = "no detail"
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("error"):
                detail = str(body["error"])[:_DETAIL_LIMIT]
            raise GristClientError(f"Grist API error (HTTP {status}): {detail}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise GristClientError(
                f"The Grist server answered with something other than JSON (HTTP {status}). "
                "Check the Grist server address."
            ) from None

    def check_connection(self) -> str:
        """Prove the credential works; returns the server host."""
        self._credential.access_token()
        if not self._credential.can_refresh:
            self._request("GET", "/api/orgs")
        return self.host

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def list_documents(self) -> list[GristDocument]:
        documents: list[GristDocument] = []
        for org in _as_list(self._request("GET", "/api/orgs"))[:_MAX_ORGS]:
            org = _as_dict(org)
            team = str(org.get("name") or "")
            workspaces = self._request("GET", f"/api/orgs/{int(org['id'])}/workspaces")
            for workspace in _as_list(workspaces):
                workspace = _as_dict(workspace)
                for doc in _as_list(workspace.get("docs")):
                    doc = _as_dict(doc)
                    if not doc.get("id"):
                        continue
                    documents.append(GristDocument(
                        id=str(doc["id"]),
                        name=str(doc.get("name") or ""),
                        workspace=str(workspace.get("name") or ""),
                        team=team,
                    ))
        documents.sort(key=lambda d: (d.team, d.workspace, d.name))
        logger.info("Grist: %d document(s) on %s", min(len(documents), _MAX_DOCUMENTS), self.host)
        return documents[:_MAX_DOCUMENTS]

    def get_document(self, doc_id: str) -> GristDocument:
        validate_doc_id(doc_id)
        body = _as_dict(self._request("GET", f"/api/docs/{doc_id}"))
        workspace = _as_dict(body.get("workspace"))
        org = _as_dict(workspace.get("org"))
        return GristDocument(
            id=doc_id,
            name=str(body.get("name") or ""),
            workspace=str(workspace.get("name") or ""),
            team=str(org.get("name") or ""),
        )

    def list_columns(self, doc_id: str, table_id: str) -> list[GristColumn]:
        validate_doc_id(doc_id)
        validate_identifier(table_id, "table")
        body = _as_dict(self._request("GET", f"/api/docs/{doc_id}/tables/{table_id}/columns"))
        columns: list[GristColumn] = []
        for col in _as_list(body.get("columns")):
            col = _as_dict(col)
            fields = _as_dict(col.get("fields"))
            col_id = str(col.get("id") or "")
            columns.append(GristColumn(
                id=col_id,
                label=str(fields.get("label") or col_id),
                type=str(fields.get("type") or ""),
                is_formula=bool(fields.get("isFormula")),
            ))
        return columns

    def list_tables(self, doc_id: str) -> list[GristTable]:
        validate_doc_id(doc_id)
        body = _as_dict(self._request("GET", f"/api/docs/{doc_id}/tables"))
        tables: list[GristTable] = []
        for entry in _as_list(body.get("tables"))[:_MAX_TABLES]:
            table_id = str(_as_dict(entry).get("id") or "")
            tables.append(GristTable(id=table_id, columns=self.list_columns(doc_id, table_id)))
        return tables

    @staticmethod
    def _records(body: Any) -> list[GristRecord]:
        return [
            GristRecord(id=int(_as_dict(r).get("id", 0)), fields=_as_dict(_as_dict(r).get("fields")))
            for r in _as_list(_as_dict(body).get("records"))
        ]

    def get_records(
        self,
        doc_id: str,
        table_id: str,
        *,
        filters: dict[str, list[Any]] | None = None,
        sort: str = "",
        limit: int = 100,
        after_id: int = 0,
    ) -> GristRecordPage:
        """One page of records. Without ``sort`` the page is in record-id order and, when more
        follow, ``next_after_id`` is the ``after_id`` of the next page (ADR 0146)."""
        validate_doc_id(doc_id)
        validate_identifier(table_id, "table")
        if after_id and sort:
            raise GristClientError("after_id pages in record-id order and cannot be combined with sort.")
        # The records endpoint has no offset. At most after_id records have an id <= after_id,
        # so this many rows in id order always reach limit + 1 rows past it.
        params: dict[str, str] = {"limit": str(after_id + limit + 1), "sort": sort or "id"}
        if filters:
            params["filter"] = json.dumps(filters, ensure_ascii=False)
        records = [
            r for r in self._records(self._request(
                "GET", f"/api/docs/{doc_id}/tables/{table_id}/records", params=params,
            ))
            if r.id > after_id
        ]
        truncated = len(records) > limit
        records = records[:limit]
        next_after_id = records[-1].id if truncated and not sort else None
        return GristRecordPage(records=records, truncated=truncated, next_after_id=next_after_id)

    def get_records_by_id(self, doc_id: str, table_id: str, ids: list[int]) -> list[GristRecord]:
        validate_doc_id(doc_id)
        validate_identifier(table_id, "table")
        return self._records(self._request(
            "GET",
            f"/api/docs/{doc_id}/tables/{table_id}/records",
            params={"filter": json.dumps({"id": ids})},
        ))

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #

    def add_records(self, doc_id: str, table_id: str, rows: list[dict[str, Any]]) -> list[int]:
        validate_doc_id(doc_id)
        validate_identifier(table_id, "table")
        body = self._request(
            "POST",
            f"/api/docs/{doc_id}/tables/{table_id}/records",
            json_body={"records": [{"fields": row} for row in rows]},
        )
        return [int(_as_dict(r)["id"]) for r in _as_list(_as_dict(body).get("records"))]

    def update_records(
        self, doc_id: str, table_id: str, rows: list[tuple[int, dict[str, Any]]],
    ) -> None:
        validate_doc_id(doc_id)
        validate_identifier(table_id, "table")
        self._request(
            "PATCH",
            f"/api/docs/{doc_id}/tables/{table_id}/records",
            json_body={"records": [{"id": rec_id, "fields": fields} for rec_id, fields in rows]},
        )

    @staticmethod
    def _column_body(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for col in columns:
            col_id = validate_identifier(col.get("id"), "column")  # type: ignore[arg-type]
            out.append({"id": col_id, "fields": {
                "label": col.get("label") or col_id,
                "type": col.get("type") or "Text",
            }})
        return out

    def add_table(self, doc_id: str, table_id: str, columns: list[dict[str, Any]]) -> str:
        validate_doc_id(doc_id)
        validate_identifier(table_id, "table")
        body = self._request(
            "POST",
            f"/api/docs/{doc_id}/tables",
            json_body={"tables": [{"id": table_id, "columns": self._column_body(columns)}]},
        )
        tables = _as_list(_as_dict(body).get("tables"))
        return str(_as_dict(tables[0]).get("id") or table_id) if tables else table_id

    def add_columns(self, doc_id: str, table_id: str, columns: list[dict[str, Any]]) -> list[str]:
        validate_doc_id(doc_id)
        validate_identifier(table_id, "table")
        body = self._request(
            "POST",
            f"/api/docs/{doc_id}/tables/{table_id}/columns",
            json_body={"columns": self._column_body(columns)},
        )
        return [str(_as_dict(c).get("id") or "") for c in _as_list(_as_dict(body).get("columns"))]
