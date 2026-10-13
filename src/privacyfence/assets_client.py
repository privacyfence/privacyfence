"""Jira Assets (Jira Service Management's CMDB) read client.

Reads schemas, object types, attribute definitions, objects (by id, by AQL and by following
references) and the Jira tickets connected to an object, through Atlassian's OAuth gateway. It
never writes to Assets.

Required config keys are the merged ``atlassian_config`` the Jira client gets: ``access_token``,
``cloud_id`` and ``scopes`` (the scopes Atlassian granted, which must include
``ASSETS_REQUIRED_SCOPE``); ``client_id``/``client_secret``/``refresh_token`` are needed to
refresh an expired access token. ``assets_workspace_id`` (optional) is the site's Assets workspace
id; without it the client looks the id up, which Atlassian allows only with the JSM scope
``read:servicedesk-request`` that PrivacyFence does not request (see ADR 0171).
"""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import requests

from .atlassian_oauth import (
    AtlassianOAuthError,
    is_unauthorized,
    load_token_file,
    refresh as atlassian_refresh,
    save_token_file,
)
from .atlassian_users import UNKNOWN_USER_LABEL, mask_emails

logger = logging.getLogger(__name__)

ASSETS_REQUIRED_SCOPE = "read:cmdb-object:jira"
NEEDS_ASSETS_PERMISSION = (
    "Jira Assets needs an extra Atlassian permission. "
    "Use Authenticate… on Jira Assets in PrivacyFence Settings."
)
NOT_AUTHENTICATED = "Jira Assets is not authenticated. Use Authenticate… in PrivacyFence Settings."
NEEDS_WORKSPACE_ID = (
    "Jira Assets could not look up this site's Assets workspace id. Set it as "
    "connectors.assets.workspace_id in PrivacyFence settings, or in the organization bundle "
    "(build_org_bundle.py --atlassian-assets-workspace-id); see docs/atlassian-setup.md."
)

MAX_PAGES = 10
AQL_PAGE_SIZE = 50
SEARCH_MAX = 100
EXPORT_MAX_OBJECTS = 10_000
EXPORT_PAGE_SIZE = 100
RELATED_MAX_OBJECTS = 200
RELATED_MAX_DEPTH = 3
ID_BATCH = 50

_SCHEMA_PAGE_SIZE = 100
_HTTP_TIMEOUT_SECONDS = 30
_ID_RE = re.compile(r"[0-9]{1,12}")
WORKSPACE_ID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_MAX_AQL_CHARS = 4000
_DIRECTIONS = ("outbound", "inbound", "both")
_ATTRIBUTE_KINDS = {
    0: "default", 1: "reference", 2: "user", 3: "confluence",
    4: "group", 5: "version", 6: "project", 7: "status",
}

_T = TypeVar("_T")


class AssetsClientError(Exception):
    """Raised for unrecoverable Jira Assets client problems (auth, config, API)."""


@dataclass(frozen=True)
class AssetsSchema:
    id: str
    name: str
    key: str
    description: str
    object_count: int
    object_type_count: int


@dataclass(frozen=True)
class AssetsObjectType:
    id: str
    name: str
    schema_id: str
    parent_id: str
    description: str
    object_count: int


@dataclass(frozen=True)
class AssetsAttributeDef:
    id: str
    name: str
    kind: str
    value_type: str
    reference_type_id: str
    reference_type_name: str
    reference_kind: str
    min_cardinality: int
    max_cardinality: int
    is_label: bool
    is_system: bool


@dataclass(frozen=True)
class AssetsReference:
    id: str
    key: str
    label: str


@dataclass(frozen=True)
class AssetsObject:
    id: str
    key: str
    label: str
    type_id: str
    type_name: str
    schema_id: str
    created: str
    updated: str
    attributes: dict[str, list[str]]
    references: dict[str, list[AssetsReference]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "label": self.label,
            "type": self.type_name,
            "type_id": self.type_id,
            "schema_id": self.schema_id,
            "created": self.created,
            "updated": self.updated,
            "attributes": {
                name: values[0] if len(values) == 1 else list(values)
                for name, values in self.attributes.items()
            },
            "references": {
                name: [{"id": r.id, "key": r.key, "label": r.label} for r in refs]
                for name, refs in self.references.items()
            },
        }


@dataclass(frozen=True)
class AssetsEdge:
    source_id: str
    target_id: str
    attribute: str


@dataclass(frozen=True)
class AssetsGraph:
    root_id: str
    objects: list[AssetsObject]
    edges: list[AssetsEdge]
    truncated: bool


@dataclass(frozen=True)
class AssetsTicket:
    key: str
    summary: str
    status: str
    issue_type: str
    priority: str


def _text(raw: Any, key: str) -> str:
    value = raw.get(key)
    return "" if value is None else str(value)


def _name_of(raw: Any, key: str) -> str:
    return _text(raw.get(key) or {}, "name")


def _int(raw: Any, key: str) -> int:
    value = raw.get(key)
    return value if isinstance(value, int) else 0


def _check_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise AssetsClientError(f"{what} must be a numeric Assets id")
    return value


def _check_aql(aql: str) -> str:
    if not isinstance(aql, str) or not 1 <= len(aql) <= _MAX_AQL_CHARS or not aql.strip():
        raise AssetsClientError(f"aql must be 1-{_MAX_AQL_CHARS} characters")
    return aql


def _scope_refused(exc: requests.HTTPError) -> bool:
    """True for Atlassian's answer to a token without the endpoint's scope: a 403, or a 401
    saying "scope does not match" (a bare 401 is an expired or revoked token)."""
    response = exc.response
    status = getattr(response, "status_code", None)
    if status == 403:
        return True
    return status == 401 and "scope does not match" in (getattr(response, "text", "") or "")


def _guarded(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Re-raise anything but an ``AssetsClientError`` as ``AssetsClientError("<method> failed: …")``."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        try:
            return fn(*args, **kwargs)
        except AssetsClientError:
            raise
        except Exception as exc:
            raise AssetsClientError(f"{fn.__name__} failed: {exc}") from exc
    return wrapper


def _parse_schema(raw: dict[str, Any]) -> AssetsSchema:
    return AssetsSchema(
        id=_text(raw, "id"),
        name=_text(raw, "name"),
        key=_text(raw, "objectSchemaKey"),
        description=_text(raw, "description"),
        object_count=_int(raw, "objectCount"),
        object_type_count=_int(raw, "objectTypeCount"),
    )


def _parse_object_type(raw: dict[str, Any]) -> AssetsObjectType:
    return AssetsObjectType(
        id=_text(raw, "id"),
        name=_text(raw, "name"),
        schema_id=_text(raw, "objectSchemaId"),
        parent_id=_text(raw, "parentObjectTypeId"),
        description=_text(raw, "description"),
        object_count=_int(raw, "objectCount"),
    )


def _parse_attribute_def(raw: dict[str, Any]) -> AssetsAttributeDef:
    raw_type = raw.get("type")
    kind = _ATTRIBUTE_KINDS.get(raw_type, "other") if isinstance(raw_type, int) else "other"
    return AssetsAttributeDef(
        id=_text(raw, "id"),
        name=_text(raw, "name"),
        kind=kind,
        value_type=_name_of(raw, "defaultType") if kind == "default" else "",
        reference_type_id=_text(raw, "referenceObjectTypeId"),
        reference_type_name=_name_of(raw, "referenceObjectType"),
        reference_kind=_name_of(raw, "referenceType"),
        min_cardinality=_int(raw, "minimumCardinality"),
        max_cardinality=_int(raw, "maximumCardinality"),
        is_label=bool(raw.get("label")),
        is_system=bool(raw.get("system")),
    )


def _parse_ticket(raw: dict[str, Any]) -> AssetsTicket:
    return AssetsTicket(
        key=_text(raw, "key"),
        summary=_text(raw, "title"),
        status=_name_of(raw, "status"),
        issue_type=_name_of(raw, "type"),
        priority=_name_of(raw, "priority"),
    )


class AssetsClient:
    """Jira Assets client backed by the shared Atlassian OAuth 2.0 bearer token.

    ``config`` is the same merged ``atlassian_config`` the Jira client gets. When the access
    token has expired the client refreshes it once and retries; if ``token_file`` is given the
    refreshed token is written back (keeping the granted ``scopes``).
    """

    def __init__(self, config: dict[str, Any], token_file: str | None = None) -> None:
        self._config = dict(config)
        self._token_file = token_file
        access_token = self._config.get("access_token", "")
        cloud_id = self._config.get("cloud_id", "")
        if not access_token or not cloud_id:
            raise AssetsClientError(NOT_AUTHENTICATED)
        if ASSETS_REQUIRED_SCOPE not in (self._config.get("scopes") or []):
            raise AssetsClientError(NEEDS_ASSETS_PERMISSION)
        self._jira_url = f"https://api.atlassian.com/ex/jira/{cloud_id}"
        workspace_id = str(self._config.get("assets_workspace_id") or "").strip()
        if workspace_id and not WORKSPACE_ID_RE.fullmatch(workspace_id):
            raise AssetsClientError(f"The configured Assets workspace id is not a valid id: {workspace_id!r}")
        self._workspace_id: str | None = workspace_id or None
        self._attribute_cache: dict[str, list[AssetsAttributeDef]] = {}
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {access_token}"

    # ------------------------------------------------------------------ #
    # Token refresh and HTTP
    # ------------------------------------------------------------------ #

    def _try_refresh(self) -> bool:
        """Attempt to refresh the access token in place. Returns True on success."""
        client_id = self._config.get("client_id", "")
        client_secret = self._config.get("client_secret", "")
        refresh_token = self._config.get("refresh_token", "")
        if self._token_file:
            # The token file is shared with the Jira and Confluence clients; if one of them
            # already refreshed (and Atlassian rotated the refresh token), use its latest value.
            try:
                refresh_token = load_token_file(self._token_file).get("refresh_token") or refresh_token
            except AtlassianOAuthError:
                # An unreadable token file is not fatal: refresh with the token already in config.
                pass
        if not client_id or not client_secret or not refresh_token:
            return False
        try:
            data = atlassian_refresh(client_id, client_secret, refresh_token)
        except AtlassianOAuthError as exc:
            logger.warning("Assets token refresh failed: %s", exc)
            return False
        access_token = data.get("access_token", "")
        if not access_token:
            return False
        self._config["access_token"] = access_token
        self._config["refresh_token"] = data.get("refresh_token", refresh_token)
        self._session.headers["Authorization"] = f"Bearer {access_token}"
        if self._token_file:
            save_token_file(self._token_file, {
                "access_token": access_token,
                "refresh_token": self._config["refresh_token"],
                "cloud_id": self._config.get("cloud_id", ""),
                "site_url": self._config.get("site_url", ""),
                "account_email": self._config.get("account_email", ""),
                "scopes": self._config.get("scopes", []),
            })
        logger.info("Assets access token refreshed")
        return True

    def _request(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call ``fn`` with one automatic refresh-and-retry on an expired token."""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if is_unauthorized(exc) and self._try_refresh():
                return fn(*args, **kwargs)
            raise

    def _send(
        self, method: str, url: str, params: dict[str, Any] | None = None, json: Any = None,
    ) -> Any:
        response = self._session.request(
            method, url, params=params, json=json, timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()

    def _call(
        self, method: str, url: str, params: dict[str, Any] | None = None, json: Any = None,
    ) -> Any:
        return self._request(self._send, method, url, params=params, json=json)

    def _api(self) -> str:
        """The workspace-scoped Assets API base URL. The workspace id is the configured one, or
        else looked up once; the lookup needs a JSM scope the token normally lacks, so a refusal
        asks for the id to be configured."""
        if self._workspace_id is None:
            try:
                data = self._call("GET", f"{self._jira_url}/rest/servicedeskapi/assets/workspace")
            except requests.HTTPError as exc:
                if _scope_refused(exc):
                    raise AssetsClientError(NEEDS_WORKSPACE_ID) from exc
                raise
            values = data.get("values") or []
            workspace_id = (values[0].get("workspaceId") if values else "") or ""
            if not workspace_id:
                raise AssetsClientError(
                    "This Atlassian site has no Assets workspace "
                    "(Jira Service Management Assets is not enabled)."
                )
            if not WORKSPACE_ID_RE.fullmatch(workspace_id):
                raise AssetsClientError(f"Atlassian returned an unexpected Assets workspace id: {workspace_id!r}")
            self._workspace_id = workspace_id
        return f"{self._jira_url}/jsm/assets/workspace/{self._workspace_id}/v1"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._call("GET", f"{self._api()}/{path}", params=params)

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    @_guarded
    def check_connection(self) -> None:
        """Resolve the Assets workspace id and, when it was configured rather than looked up,
        prove it with one cheap read."""
        configured = self._workspace_id is not None
        self._api()
        if configured:
            self._get("objectschema/list", {"startAt": 0, "maxResults": 1})

    # ------------------------------------------------------------------ #
    # Schemas, types, attributes
    # ------------------------------------------------------------------ #

    @_guarded
    def list_schemas(self, max_results: int = 100) -> list[AssetsSchema]:
        max_results = max(1, max_results)
        schemas: list[AssetsSchema] = []
        for _ in range(MAX_PAGES):
            page = self._get("objectschema/list", {
                "startAt": len(schemas),
                "maxResults": min(max_results - len(schemas), _SCHEMA_PAGE_SIZE),
            })
            values = page.get("values") or []
            schemas.extend(_parse_schema(v) for v in values)
            if page.get("isLast") or not values or len(schemas) >= max_results:
                break
        return schemas[:max_results]

    @_guarded
    def get_schema(self, schema_id: str) -> AssetsSchema:
        return _parse_schema(self._get(f"objectschema/{_check_id(schema_id, 'schema id')}"))

    @_guarded
    def list_object_types(self, schema_id: str) -> list[AssetsObjectType]:
        raw = self._get(f"objectschema/{_check_id(schema_id, 'schema id')}/objecttypes/flat")
        return [_parse_object_type(t) for t in raw]

    @_guarded
    def list_attributes(self, type_id: str) -> list[AssetsAttributeDef]:
        _check_id(type_id, "object type id")
        if type_id not in self._attribute_cache:
            raw = self._get(f"objecttype/{type_id}/attributes")
            self._attribute_cache[type_id] = [_parse_attribute_def(a) for a in raw]
        return list(self._attribute_cache[type_id])

    # ------------------------------------------------------------------ #
    # Objects
    # ------------------------------------------------------------------ #

    def _attribute_name(
        self, entry: dict[str, Any], names: dict[str, str], type_id: str,
    ) -> str:
        embedded = (entry.get("objectTypeAttribute") or {}).get("name")
        if embedded:
            return str(embedded)
        attribute_id = _text(entry, "objectTypeAttributeId")
        if attribute_id in names:
            return names[attribute_id]
        by_type = {a.id: a.name for a in self.list_attributes(type_id)}
        return by_type.get(attribute_id, attribute_id)

    @staticmethod
    def _value_text(item: dict[str, Any]) -> str:
        user = item.get("user")
        if user is not None:
            return mask_emails(user.get("displayName") or "", user.get("key") or "") or UNKNOWN_USER_LABEL
        for kind in ("status", "group"):
            if item.get(kind):
                return _text(item[kind], "name")
        return _text(item, "displayValue") or _text(item, "value")

    def _parse_object(self, raw: dict[str, Any], names: dict[str, str] | None = None) -> AssetsObject:
        object_type = raw.get("objectType") or {}
        type_id = _text(object_type, "id")
        attributes: dict[str, list[str]] = {}
        references: dict[str, list[AssetsReference]] = {}
        for entry in raw.get("attributes") or []:
            items = entry.get("objectAttributeValues") or []
            if not items:
                continue
            name = self._attribute_name(entry, names or {}, type_id)
            for item in items:
                ref = item.get("referencedObject")
                if ref:
                    reference = AssetsReference(_text(ref, "id"), _text(ref, "objectKey"), _text(ref, "label"))
                    references.setdefault(name, []).append(reference)
                    attributes.setdefault(name, []).append(f"{reference.label} ({reference.key})")
                else:
                    attributes.setdefault(name, []).append(self._value_text(item))
        return AssetsObject(
            id=_text(raw, "id"),
            key=_text(raw, "objectKey"),
            label=_text(raw, "label"),
            type_id=type_id,
            type_name=_text(object_type, "name"),
            schema_id=_text(object_type, "objectSchemaId"),
            created=_text(raw, "created"),
            updated=_text(raw, "updated"),
            attributes=attributes,
            references=references,
        )

    def _run_aql(
        self, aql: str, limit: int, page_size: int, max_pages: int | None,
    ) -> list[AssetsObject]:
        """Page through an AQL result until ``limit`` objects, ``isLast``, an empty page or
        ``max_pages``. ``hasMoreResults`` and ``total`` are not trusted."""
        objects: list[AssetsObject] = []
        pages = 0
        while len(objects) < limit and (max_pages is None or pages < max_pages):
            page = self._call(
                "POST", f"{self._api()}/object/aql",
                params={
                    "startAt": len(objects),
                    "maxResults": min(limit - len(objects), page_size),
                    "includeAttributes": "true",
                },
                json={"qlQuery": aql},
            )
            pages += 1
            values = page.get("values") or []
            names = {_text(a, "id"): _text(a, "name") for a in page.get("objectTypeAttributes") or []}
            objects.extend(self._parse_object(v, names) for v in values)
            if page.get("isLast") or not values:
                break
        return objects[:limit]

    @_guarded
    def search_objects(self, aql: str, max_results: int = 25) -> list[AssetsObject]:
        _check_aql(aql)
        return self._run_aql(aql, max(1, min(max_results, SEARCH_MAX)), AQL_PAGE_SIZE, MAX_PAGES)

    @_guarded
    def get_object(self, object_id: str) -> AssetsObject:
        _check_id(object_id, "object id")
        raw = self._get(f"object/{object_id}")
        attributes = self._get(f"object/{object_id}/attributes")
        return self._parse_object({**raw, "attributes": attributes})

    @_guarded
    def get_objects(self, ids: list[str]) -> list[AssetsObject]:
        unique = list(dict.fromkeys(_check_id(i, "object id") for i in ids))
        found: dict[str, AssetsObject] = {}
        for start in range(0, len(unique), ID_BATCH):
            batch = unique[start:start + ID_BATCH]
            aql = f"objectId IN ({','.join(batch)})"
            for obj in self._run_aql(aql, len(batch), AQL_PAGE_SIZE, MAX_PAGES):
                found[obj.id] = obj
        return [found[i] for i in unique if i in found]

    @_guarded
    def export_objects(self, aql: str) -> list[AssetsObject]:
        _check_aql(aql)
        objects = self._run_aql(aql, EXPORT_MAX_OBJECTS + 1, EXPORT_PAGE_SIZE, None)
        if len(objects) > EXPORT_MAX_OBJECTS:
            raise AssetsClientError(
                "The AQL query matches more than 10,000 objects; narrow it and export in parts."
            )
        return objects

    # ------------------------------------------------------------------ #
    # Linked objects
    # ------------------------------------------------------------------ #

    @_guarded
    def related_objects(
        self, object_id: str, direction: str = "both", depth: int = 1, max_objects: int = 50,
    ) -> AssetsGraph:
        if direction not in _DIRECTIONS:
            raise AssetsClientError("direction must be outbound, inbound or both")
        depth = max(1, min(depth, RELATED_MAX_DEPTH))
        max_objects = max(1, min(max_objects, RELATED_MAX_OBJECTS))
        root = self.get_object(object_id)
        seen: dict[str, AssetsObject] = {root.id: root}
        frontier = [root]
        edges: list[AssetsEdge] = []
        truncated = False
        for _ in range(depth):
            added: list[AssetsObject] = []
            if direction != "inbound":
                level_edges, fresh, more = self._outbound_level(frontier, seen, max_objects)
                edges.extend(level_edges)
                seen.update((o.id, o) for o in fresh)
                added.extend(fresh)
                truncated = truncated or more
            if direction != "outbound":
                level_edges, fresh, more = self._inbound_level(frontier, seen, max_objects)
                edges.extend(level_edges)
                seen.update((o.id, o) for o in fresh)
                added.extend(fresh)
                truncated = truncated or more
            if truncated or not added:
                break
            frontier = added
        kept = [e for e in dict.fromkeys(edges) if e.source_id in seen and e.target_id in seen]
        return AssetsGraph(root.id, list(seen.values()), kept, truncated)

    def _outbound_level(
        self, frontier: list[AssetsObject], seen: dict[str, AssetsObject], max_objects: int,
    ) -> tuple[list[AssetsEdge], list[AssetsObject], bool]:
        """Edges from the frontier's references, the objects they lead to that are new (at most
        the remaining room) and whether more than that room was wanted."""
        edges: list[AssetsEdge] = []
        wanted: list[str] = []
        for obj in frontier:
            for attribute, refs in obj.references.items():
                for ref in refs:
                    edges.append(AssetsEdge(obj.id, ref.id, attribute))
                    if ref.id not in seen:
                        wanted.append(ref.id)
        candidates = list(dict.fromkeys(wanted))
        room = max_objects - len(seen)
        return edges, self.get_objects(candidates[:room]), len(candidates) > room

    def _inbound_level(
        self, frontier: list[AssetsObject], seen: dict[str, AssetsObject], max_objects: int,
    ) -> tuple[list[AssetsEdge], list[AssetsObject], bool]:
        """As ``_outbound_level``, for objects that reference a frontier object."""
        frontier_ids = {o.id for o in frontier}
        edges: list[AssetsEdge] = []
        found: dict[str, AssetsObject] = {}
        room = max_objects - len(seen)
        ids = [o.id for o in frontier]
        for start in range(0, len(ids), ID_BATCH):
            batch = ids[start:start + ID_BATCH]
            aql = f"object HAVING outboundReferences(objectId IN ({','.join(batch)}))"
            # Seen objects can come back too; ask for enough that more than ``room`` new
            # objects shows up as such.
            for obj in self._run_aql(aql, room + len(seen) + 1, AQL_PAGE_SIZE, MAX_PAGES):
                found.setdefault(obj.id, obj)
                for attribute, refs in obj.references.items():
                    edges.extend(
                        AssetsEdge(obj.id, ref.id, attribute) for ref in refs if ref.id in frontier_ids
                    )
        fresh = [o for o in found.values() if o.id not in seen]
        return edges, fresh[:room], len(fresh) > room

    # ------------------------------------------------------------------ #
    # Connected Jira tickets
    # ------------------------------------------------------------------ #

    @_guarded
    def connected_tickets(self, object_id: str) -> list[AssetsTicket]:
        _check_id(object_id, "object id")
        raw = self._get(f"objectconnectedtickets/{object_id}/tickets")
        return [_parse_ticket(t) for t in raw.get("tickets") or []]
