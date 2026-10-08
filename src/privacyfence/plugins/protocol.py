"""Wire messages of the plugin protocol and the validators that guard them.

Everything a plugin sends is untrusted input, so each ``from_wire`` fails closed: a wrong type, a
missing key, an over-long string or a pattern miss raises ``RpcError("invalid_params")`` and nothing
partial is returned. Unknown keys are ignored (a newer minor version may add some), except the
org-only fields, which local mode rejects with ``org_only_field``. Error details name keys and
limits, never the values a plugin sent, so a hostile plugin cannot smuggle content into logs or
audit entries. The validators are hand-written and stdlib only; ``docs/plugin-protocol/
protocol.schema.json`` is the published description of the same shapes (ADR 0120).

Blocks inside ``preview`` and ``payload`` are checked here only to be lists of objects. Full block
validation is injected through ``validate_blocks`` so this module stays free of the sanitizer.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Self

from privacyfence.plugins.constants import (
    ERROR_CODES,
    GATES,
    INLINE_RESULT_BYTES,
    MAX_DESCRIPTION_CHARS,
    MAX_EFFECT_CHARS,
    MAX_PAGE_BODY_BYTES,
    MAX_PREVIEW_BYTES,
    MAX_SCOPE_VALUE_CHARS,
    MAX_SCOPE_VALUES,
    MAX_TITLE_CHARS,
    SCOPE_TYPE_RE,
    TOOL_NAME_RE,
)
from privacyfence.principal import Principal

ValidateBlocks = Callable[..., list[dict]]


class RpcError(Exception):
    """A protocol error: ``code`` is a name from ``ERROR_CODES``."""

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        retryable: bool = False,
        extra: dict | None = None,
    ) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.extra = dict(extra) if extra else {}

    def to_error(self) -> dict:
        # An unknown name still serializes (a plugin may report one the daemon does not know);
        # it travels as internal_error's number so the object stays a valid JSON-RPC error.
        number = ERROR_CODES.get(self.code, ERROR_CODES["internal_error"])
        data = {**self.extra, "code": self.code, "detail": self.detail, "retryable": self.retryable}
        return {"code": number, "message": self.code, "data": data}


def _bad(detail: str) -> RpcError:
    return RpcError("invalid_params", detail)


def _obj(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise _bad(f"{where} must be an object")
    return value


def _req(obj: dict, key: str, where: str) -> Any:
    if key not in obj:
        raise _bad(f"{where}.{key} is required")
    return obj[key]


def _str(value: Any, where: str, *, min_len: int = 0, max_len: int | None = None) -> str:
    if not isinstance(value, str):
        raise _bad(f"{where} must be a string")
    if len(value) < min_len:
        raise _bad(f"{where} must not be empty" if min_len == 1 else f"{where} is too short")
    if max_len is not None and len(value) > max_len:
        raise _bad(f"{where} is longer than {max_len} characters")
    return value


def _opt_str(obj: dict, key: str, where: str, *, max_len: int | None = None) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    return _str(value, f"{where}.{key}", min_len=1, max_len=max_len)


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise _bad(f"{where} must be a boolean")
    return value


def _int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(f"{where} must be an integer")
    return value


def _str_list(value: Any, where: str, *, max_items: int | None = None, max_len: int | None = None) -> list[str]:
    if not isinstance(value, list):
        raise _bad(f"{where} must be a list")
    if max_items is not None and len(value) > max_items:
        raise _bad(f"{where} has more than {max_items} items")
    return [_str(v, f"{where}[{i}]", min_len=1, max_len=max_len) for i, v in enumerate(value)]


def _block_list(value: Any, where: str) -> list[dict]:
    if not isinstance(value, list):
        raise _bad(f"{where} must be a list")
    for i, block in enumerate(value):
        if not isinstance(block, dict):
            raise _bad(f"{where}[{i}] must be an object")
    return value


def _noop_validate_blocks(blocks: list[dict], **_: Any) -> list[dict]:
    return blocks


def _wire_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _str_map(value: Any, where: str) -> dict[str, str]:
    mapping = _obj(value, where)
    for v in mapping.values():
        if not isinstance(v, str):
            raise _bad(f"{where} values must be strings")
    return dict(mapping)


def _check_mode(mode: str) -> None:
    if mode not in ("local", "org"):
        raise ValueError(f"unknown mode: {mode}")


@dataclass(frozen=True)
class PrincipalContext:
    id: str
    display_name: str
    storage_dir: str
    roles: tuple[str, ...] | None = None

    WIRE_KEYS: ClassVar[frozenset[str]] = frozenset({"id", "display_name", "storage_dir", "roles"})

    @classmethod
    def from_wire(cls, obj: Any, *, mode: str = "local") -> Self:
        _check_mode(mode)
        data = _obj(obj, "principal")
        roles = None
        if "roles" in data:
            if mode == "local":
                raise RpcError("org_only_field", "principal.roles is only valid in org mode")
            roles = tuple(_str_list(data["roles"], "principal.roles"))
        return cls(
            id=_str(_req(data, "id", "principal"), "principal.id", min_len=1),
            display_name=_str(_req(data, "display_name", "principal"), "principal.display_name"),
            storage_dir=_str(_req(data, "storage_dir", "principal"), "principal.storage_dir", min_len=1),
            roles=roles,
        )

    def to_wire(self) -> dict:
        out: dict[str, Any] = {"id": self.id, "display_name": self.display_name, "storage_dir": self.storage_dir}
        if self.roles is not None:
            out["roles"] = list(self.roles)
        return out


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict
    read_only: bool
    destructive: bool
    gate: str
    scopes: tuple[str, ...] = ()
    effect: str | None = None
    title: str | None = None

    WIRE_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"name", "description", "parameters", "read_only", "destructive", "gate", "scopes", "effect", "title"}
    )

    @classmethod
    def from_wire(cls, obj: Any, *, mode: str = "local") -> Self:
        _check_mode(mode)
        data = _obj(obj, "tool")
        name = _str(_req(data, "name", "tool"), "tool.name")
        if not TOOL_NAME_RE.fullmatch(name):
            raise _bad("tool.name does not match the tool name pattern")
        gate = _str(_req(data, "gate", "tool"), "tool.gate")
        if gate not in GATES:
            raise _bad(f"tool.gate must be one of {', '.join(GATES)}")
        scopes = _str_list(data.get("scopes", []), "tool.scopes")
        for scope in scopes:
            if not SCOPE_TYPE_RE.fullmatch(scope):
                raise _bad("tool.scopes has a name that does not match the scope type pattern")
        return cls(
            name=name,
            description=_str(
                _req(data, "description", "tool"), "tool.description", min_len=1, max_len=MAX_DESCRIPTION_CHARS
            ),
            parameters=_obj(_req(data, "parameters", "tool"), "tool.parameters"),
            read_only=_bool(_req(data, "read_only", "tool"), "tool.read_only"),
            destructive=_bool(_req(data, "destructive", "tool"), "tool.destructive"),
            gate=gate,
            scopes=tuple(scopes),
            effect=_opt_str(data, "effect", "tool", max_len=MAX_EFFECT_CHARS),
            title=_opt_str(data, "title", "tool", max_len=MAX_TITLE_CHARS),
        )

    def to_wire(self) -> dict:
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "read_only": self.read_only,
            "destructive": self.destructive,
            "gate": self.gate,
            "scopes": list(self.scopes),
        }
        if self.effect is not None:
            out["effect"] = self.effect
        if self.title is not None:
            out["title"] = self.title
        return out


def _scope_types(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise _bad("scope_types must be a list")
    out = []
    for i, item in enumerate(value):
        entry = _obj(item, f"scope_types[{i}]")
        name = _str(_req(entry, "name", f"scope_types[{i}]"), f"scope_types[{i}].name")
        if not SCOPE_TYPE_RE.fullmatch(name):
            raise _bad(f"scope_types[{i}].name does not match the scope type pattern")
        description = _str(entry.get("description", ""), f"scope_types[{i}].description")
        out.append({"name": name, "description": description})
    return out


@dataclass(frozen=True)
class InitializeResult:
    protocol_version: str
    plugin_name: str
    plugin_version: str
    scope_types: list[dict] = field(default_factory=list)
    tools: list[ToolDef] = field(default_factory=list)

    WIRE_KEYS: ClassVar[frozenset[str]] = frozenset({"protocol_version", "plugin", "scope_types", "tools"})

    @classmethod
    def from_wire(cls, obj: Any, *, mode: str = "local") -> Self:
        _check_mode(mode)
        data = _obj(obj, "initialize result")
        plugin = _obj(_req(data, "plugin", "initialize result"), "plugin")
        raw_tools = _req(data, "tools", "initialize result")
        if not isinstance(raw_tools, list):
            raise _bad("tools must be a list")
        return cls(
            protocol_version=_str(
                _req(data, "protocol_version", "initialize result"), "protocol_version", min_len=1
            ),
            plugin_name=_str(_req(plugin, "name", "plugin"), "plugin.name", min_len=1),
            plugin_version=_str(_req(plugin, "version", "plugin"), "plugin.version", min_len=1),
            scope_types=_scope_types(data.get("scope_types", [])),
            tools=[ToolDef.from_wire(t, mode=mode) for t in raw_tools],
        )

    def to_wire(self) -> dict:
        return {
            "protocol_version": self.protocol_version,
            "plugin": {"name": self.plugin_name, "version": self.plugin_version},
            "scope_types": [dict(s) for s in self.scope_types],
            "tools": [t.to_wire() for t in self.tools],
        }


def _scopes_map(value: Any) -> dict[str, list[str]]:
    scopes = _obj(value, "scopes")
    out: dict[str, list[str]] = {}
    for scope_type, values in scopes.items():
        if not SCOPE_TYPE_RE.fullmatch(scope_type):
            raise _bad("scopes has a key that does not match the scope type pattern")
        out[scope_type] = _str_list(
            values, f"scopes.{scope_type}", max_items=MAX_SCOPE_VALUES, max_len=MAX_SCOPE_VALUE_CHARS
        )
    return out


@dataclass(frozen=True)
class PrepareResult:
    preview: list[dict]
    scopes: dict[str, list[str]]
    payload: list[dict] | None = None

    WIRE_KEYS: ClassVar[frozenset[str]] = frozenset({"preview", "payload", "scopes"})

    @classmethod
    def from_wire(
        cls,
        obj: Any,
        *,
        mode: str = "local",
        validate_blocks: ValidateBlocks = _noop_validate_blocks,
    ) -> Self:
        """``validate_blocks(blocks, max_bytes=...)`` is the full block validator; the preview is
        checked with ``MAX_PREVIEW_BYTES`` and the payload with ``None`` (it has its own cap)."""
        _check_mode(mode)
        data = _obj(obj, "prepare result")
        preview = _block_list(_req(data, "preview", "prepare result"), "preview")
        scopes = _scopes_map(data.get("scopes", {}))
        payload = None
        if data.get("payload") is not None:
            payload = _block_list(data["payload"], "payload")
            if _wire_size({"blocks": payload}) > INLINE_RESULT_BYTES:
                raise RpcError("payload_too_large", "payload is larger than the inline result limit")
        try:
            preview = validate_blocks(preview, max_bytes=MAX_PREVIEW_BYTES)
            if payload is not None:
                payload = validate_blocks(payload, max_bytes=None)
        except ValueError as exc:
            raise RpcError("invalid_blocks", str(exc)) from None
        return cls(preview=preview, scopes=scopes, payload=payload)

    def to_wire(self) -> dict:
        out: dict[str, Any] = {"preview": self.preview, "scopes": self.scopes}
        if self.payload is not None:
            out["payload"] = self.payload
        return out


@dataclass(frozen=True)
class ExecuteResult:
    result: Any = None
    approval_id: str | None = None

    WIRE_KEYS: ClassVar[frozenset[str]] = frozenset({"result", "approval_id"})

    @classmethod
    def from_wire(cls, obj: Any, *, mode: str = "local") -> Self:
        _check_mode(mode)
        data = _obj(obj, "execute result")
        return cls(result=data.get("result"), approval_id=_opt_str(data, "approval_id", "execute result"))

    def to_wire(self) -> dict:
        out: dict[str, Any] = {"result": self.result}
        if self.approval_id is not None:
            out["approval_id"] = self.approval_id
        return out


@dataclass(frozen=True)
class SourceCallParams:
    principal: str
    operation: str
    params: dict = field(default_factory=dict)
    credential: Any = None

    WIRE_KEYS: ClassVar[frozenset[str]] = frozenset({"principal", "operation", "params", "credential"})

    @classmethod
    def from_wire(cls, obj: Any, *, mode: str = "local") -> Self:
        _check_mode(mode)
        data = _obj(obj, "source.call params")
        if "credential" in data and mode == "local":
            raise RpcError("org_only_field", "source.call.credential is only valid in org mode")
        return cls(
            principal=_str(_req(data, "principal", "source.call"), "source.call.principal", min_len=1),
            operation=_str(_req(data, "operation", "source.call"), "source.call.operation", min_len=1),
            params=_obj(data.get("params", {}), "source.call.params"),
            credential=data.get("credential"),
        )

    def to_wire(self) -> dict:
        out: dict[str, Any] = {"principal": self.principal, "operation": self.operation, "params": self.params}
        if self.credential is not None:
            out["credential"] = self.credential
        return out


@dataclass(frozen=True)
class ConfirmRequestParams:
    principal: str
    kind: str
    title: str
    preview: list[dict]
    require_step_up: bool = True

    WIRE_KEYS: ClassVar[frozenset[str]] = frozenset({"principal", "kind", "title", "preview", "require_step_up"})

    @classmethod
    def from_wire(
        cls,
        obj: Any,
        *,
        mode: str = "local",
        validate_blocks: ValidateBlocks = _noop_validate_blocks,
    ) -> Self:
        _check_mode(mode)
        data = _obj(obj, "confirm.request params")
        preview = _block_list(_req(data, "preview", "confirm.request"), "preview")
        try:
            preview = validate_blocks(preview, max_bytes=MAX_PREVIEW_BYTES)
        except ValueError as exc:
            raise RpcError("invalid_blocks", str(exc)) from None
        step_up = data.get("require_step_up", True)
        return cls(
            principal=_str(_req(data, "principal", "confirm.request"), "confirm.request.principal", min_len=1),
            kind=_str(
                _req(data, "kind", "confirm.request"), "confirm.request.kind", min_len=1, max_len=MAX_TITLE_CHARS
            ),
            title=_str(
                _req(data, "title", "confirm.request"), "confirm.request.title", min_len=1, max_len=MAX_TITLE_CHARS
            ),
            preview=preview,
            require_step_up=_bool(step_up, "confirm.request.require_step_up"),
        )

    def to_wire(self) -> dict:
        return {
            "principal": self.principal,
            "kind": self.kind,
            "title": self.title,
            "preview": self.preview,
            "require_step_up": self.require_step_up,
        }


@dataclass(frozen=True)
class WebResponse:
    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)
    body_encoding: str = "utf8"

    WIRE_KEYS: ClassVar[frozenset[str]] = frozenset({"status", "headers", "body", "body_encoding"})

    @classmethod
    def from_wire(cls, obj: Any, *, mode: str = "local") -> Self:
        _check_mode(mode)
        data = _obj(obj, "web.request result")
        encoding = _str(data.get("body_encoding", "utf8"), "body_encoding")
        if encoding not in ("utf8", "base64"):
            raise _bad("body_encoding must be utf8 or base64")
        body = _str(_req(data, "body", "web.request result"), "body")
        if encoding == "base64":
            try:
                size = len(base64.b64decode(body, validate=True))
            except (binascii.Error, ValueError):
                raise _bad("body is not valid base64") from None
        else:
            size = len(body.encode("utf-8", "surrogatepass"))
        if size > MAX_PAGE_BODY_BYTES:
            raise RpcError("payload_too_large", "body is larger than the page body limit")
        return cls(
            status=_int(_req(data, "status", "web.request result"), "status"),
            body=body,
            headers=_str_map(data.get("headers", {}), "headers"),
            body_encoding=encoding,
        )

    def to_wire(self) -> dict:
        return {
            "status": self.status,
            "headers": self.headers,
            "body": self.body,
            "body_encoding": self.body_encoding,
        }


def args_digest(args: dict) -> str:
    """Digest of a call's arguments, independent of key order, that both sides compute."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def principal_context(principal: Principal, storage_dir: Path, *, mode: str = "local") -> dict:
    """The ``PrincipalContext`` the daemon sends with a request made for ``principal``."""
    _check_mode(mode)
    ctx = PrincipalContext(
        id=principal.id,
        display_name=principal.display_name,
        storage_dir=str(storage_dir),
        roles=None if mode == "local" else (("admin",) if principal.is_admin else ()),
    )
    return ctx.to_wire()


__all__ = [
    "ConfirmRequestParams",
    "ExecuteResult",
    "InitializeResult",
    "PrepareResult",
    "PrincipalContext",
    "RpcError",
    "SourceCallParams",
    "ToolDef",
    "WebResponse",
    "args_digest",
    "principal_context",
]
