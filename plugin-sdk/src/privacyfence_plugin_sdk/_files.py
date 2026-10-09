"""File parameters: the spec constants, type detection and the incoming-file objects.

The values and rules are copies of the daemon's (the SDK never imports ``privacyfence``, ADR 0126); a
test compares the constants.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ._rpc import RpcError

FILE_PARAM_KEY = "x-privacyfence-file"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILE_PARAMS_PER_TOOL = 1
FILE_MEDIA_TYPES = (
    "text/html", "text/plain", "application/json", "application/pdf",
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "font/woff", "font/woff2", "font/ttf", "font/otf",
    "application/octet-stream",
)

_EXTENSION_TYPES = {
    ".html": "text/html", ".htm": "text/html",
    ".txt": "text/plain", ".csv": "text/plain", ".md": "text/plain",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
}
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"wOFF", "font/woff"),
    (b"wOF2", "font/woff2"),
    (b"\x00\x01\x00\x00", "font/ttf"),
    (b"%PDF-", "application/pdf"),
)
_HEAD_CHARS = 1024
_BOM = b"\xef\xbb\xbf"
_OCTET_STREAM = "application/octet-stream"


def declared_media_type(name: str) -> str:
    """The type the file's name claims, from a fixed map on the lowercased extension."""
    return _EXTENSION_TYPES.get(os.path.splitext(name)[1].lower(), _OCTET_STREAM)


def sniff_media_type(data: bytes) -> str:
    """The type the bytes show: magic numbers first, then text (HTML, JSON, plain), else binary."""
    for magic, media_type in _MAGIC:
        if data.startswith(magic):
            return media_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    try:
        text = (data[len(_BOM):] if data.startswith(_BOM) else data).decode("utf-8")
    except UnicodeDecodeError:
        return _OCTET_STREAM
    if "\x00" in text:
        return _OCTET_STREAM
    head = text[:_HEAD_CHARS].lstrip().lower()
    if "<!doctype html" in head or "<html" in head:
        return "text/html"
    try:
        json.loads(text)
    except ValueError:
        return "text/plain"
    return "application/json"


@dataclass(frozen=True)
class IncomingFile:
    """A file the AI gave a tool. The bytes are in ``content`` in the execute function only."""

    name: str
    size: int
    media_type: str
    sniffed_type: str
    sha256: str
    data: bytes | None = field(default=None, repr=False)

    @property
    def content(self) -> bytes:
        if self.data is None:
            raise RuntimeError("the file's bytes are available in the execute function only")
        return self.data


def file_param(description: str = "", *, max_bytes: int, media_types: Sequence[str]) -> dict:
    """The parameter schema of a file: pass it in ``params=`` of ``@plugin.tool``."""
    spec: dict[str, Any] = {"type": "string"}
    if description:
        spec["description"] = description
    spec[FILE_PARAM_KEY] = {"max_bytes": max_bytes, "media_types": list(media_types)}
    return spec


def file_specs(properties: Mapping[str, Mapping]) -> dict[str, Mapping]:
    """The file parameters of a tool's ``properties``, by name."""
    return {p: s[FILE_PARAM_KEY] for p, s in properties.items() if FILE_PARAM_KEY in s}


def parse_files(raw: Any, specs: Mapping[str, Mapping], *, with_content: bool) -> dict[str, IncomingFile]:
    """The wire ``files`` as ``IncomingFile``s; ``invalid_params`` for anything that is not as specified."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RpcError("invalid_params", "files must be an object")
    out: dict[str, IncomingFile] = {}
    for param, item in raw.items():
        where = f"files.{param}"
        spec = specs.get(param)
        if spec is None:
            raise RpcError("invalid_params", f"{where}: not a file parameter of this tool")
        if not isinstance(item, dict):
            raise RpcError("invalid_params", f"{where}: must be an object")
        name = _field(item, "name", str, where)
        size = _field(item, "size", int, where)
        media_type = _field(item, "media_type", str, where)
        sniffed = _field(item, "sniffed_type", str, where)
        sha256 = _field(item, "sha256", str, where)
        if size < 0 or size > spec["max_bytes"]:
            raise RpcError("invalid_params", f"{where}: size exceeds {spec['max_bytes']} bytes")
        if sniffed not in spec["media_types"]:
            raise RpcError("invalid_params", f"{where}: sniffed_type {sniffed} is not accepted")
        data = None
        if with_content:
            encoded = _field(item, "content_base64", str, where)
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                raise RpcError("invalid_params", f"{where}: content_base64 is not valid base64") from None
            if len(data) != size:
                raise RpcError("invalid_params", f"{where}: content length differs from size")
            if hashlib.sha256(data).hexdigest() != sha256:
                raise RpcError("invalid_params", f"{where}: content differs from sha256")
        out[param] = IncomingFile(name, size, media_type, sniffed, sha256, data)
    return out


def _field(item: dict, key: str, kind: type, where: str) -> Any:
    value = item.get(key)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise RpcError("invalid_params", f"{where}: {key} is missing or not a {kind.__name__}")
    return value
