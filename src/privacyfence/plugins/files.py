"""A plugin tool's file parameter, as the daemon sees it (protocol 1.3).

The AI names a file (a local path or an ``upload:<id>`` slot); the daemon reads it through
``local_files`` under the same rules as every other connector, works out what it is from its bytes
rather than from its name, and shows the human a block about it on the approval card. The plugin is
sent the metadata at ``tool.prepare`` and the bytes only at ``tool.execute``, after the gate passed.
No Starlette import: this module is pure data handling.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field

from privacyfence import local_files
from privacyfence.local_files import LocalFileAccessError
from privacyfence.plugins import blocks
from privacyfence.plugins.constants import UPLOAD_ID_RE
from privacyfence.plugins.protocol import FileParamSpec

FILE_PARAM_HINT = (
    "A file, not its content: a local file path (absolute or starting with ~/), or upload:<upload_id> "
    "after you PUT the file to the upload_url privacyfence_create_upload_slot returned. "
    "At most {max_bytes:,} bytes; accepted types: {types}."
)
UNNAMED_FILE = "(unnamed file)"

_UPLOAD_PREFIX = local_files.UPLOAD_REF_PREFIX
_OCTET_STREAM = "application/octet-stream"

_BY_EXTENSION = {
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
_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class IncomingFile:
    param: str
    name: str
    size: int
    media_type: str
    sniffed_type: str
    sha256: str
    source: str                     # "Upload slot", or the path as the AI gave it
    data: bytes = field(repr=False)

    def to_wire(self, *, with_content: bool) -> dict:
        """The ``ToolFile`` object; ``content_base64`` only when ``with_content``."""
        wire: dict = {
            "name": self.name, "size": self.size, "media_type": self.media_type,
            "sniffed_type": self.sniffed_type, "sha256": self.sha256,
        }
        if with_content:
            wire["content_base64"] = base64.b64encode(self.data).decode("ascii")
        return wire


def declared_media_type(name: str) -> str:
    """The type the file's name claims, from a fixed map on the lowercased extension."""
    return _BY_EXTENSION.get(os.path.splitext(name)[1].lower(), _OCTET_STREAM)


def sniff_media_type(data: bytes) -> str:
    """The type the bytes are, first match wins: a binary magic, then text, else octet-stream."""
    for magic, media_type in _MAGIC:
        if data.startswith(magic):
            return media_type
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    body = data[len(_BOM):] if data.startswith(_BOM) else data
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return _OCTET_STREAM
    if "\x00" in text:
        return _OCTET_STREAM
    head = text[:1024].lstrip().lower()
    if "<!doctype html" in head or "<html" in head:
        return "text/html"
    try:
        json.loads(text)
    except ValueError:
        return "text/plain"
    return "application/json"


def file_reference(value: str) -> str:
    """The reference ``local_files`` understands: a bare slot id gets its ``upload:`` prefix."""
    value = value.strip()
    if value.startswith(_UPLOAD_PREFIX):
        return value
    if UPLOAD_ID_RE.fullmatch(value):
        return _UPLOAD_PREFIX + value
    return value


def resolve_file(spec: FileParamSpec, value: str, *, tool_title: str) -> IncomingFile:
    """Read, check and describe the file ``value`` names. Call with ``local_files.call_context``
    entered; ``LocalFilesNeeded`` (the shim fetches the file and re-sends the call) and
    ``LocalFileAccessError`` propagate unchanged."""
    if not isinstance(value, str):
        raise LocalFileAccessError(f"{tool_title} needs a file path or upload id in {spec.param}.")
    ref = file_reference(value)
    if not ref:
        raise LocalFileAccessError(f"{tool_title} needs a file in {spec.param}.")
    # Plugins run in local mode only (the plugin host is the local-mode one).
    local_files.require_local_files([ref], max_total_bytes=spec.max_bytes, download_mode="local")
    data = local_files.read_local_file(ref, download_mode="local")
    if len(data) > spec.max_bytes:
        raise LocalFileAccessError(
            f"The file is {len(data):,} bytes, over the {spec.max_bytes:,}-byte limit of {tool_title}."
        )
    name = blocks.clean_line(local_files.resolved_name(ref))[:120] or UNNAMED_FILE
    sniffed = sniff_media_type(data)
    if sniffed not in spec.media_types:
        raise LocalFileAccessError(
            f"The file's content is {sniffed}, and {tool_title} accepts only {', '.join(spec.media_types)}."
        )
    return IncomingFile(
        spec.param, name, len(data), declared_media_type(name), sniffed,
        hashlib.sha256(data).hexdigest(),
        "Upload slot" if ref.startswith(_UPLOAD_PREFIX) else blocks.clean_line(value.strip())[:200],
        data,
    )


# The labels of the block above. A plugin's own fields block may not use one, so no row a plugin
# writes can pass for a row the daemon read from the file.
RESERVED_LABELS = ("File", "Source", "Size", "Declared type", "Detected type", "SHA-256")
CHECKED_HEADING = "Read and checked by PrivacyFence from the file's bytes"
PLUGIN_HEADING = "From the plugin"
RESERVED_LABEL_MESSAGE = "a preview or payload row uses a label PrivacyFence reserves for the file it checked"


def card_block(file: IncomingFile) -> dict:
    return {"type": "fields", "items": [
        {"label": "File", "value": file.name},
        {"label": "Source", "value": file.source},
        {"label": "Size", "value": f"{file.size:,} bytes"},
        {"label": "Declared type", "value": file.media_type},
        {"label": "Detected type", "value": file.sniffed_type},
        {"label": "SHA-256", "value": file.sha256},
    ]}


def checked_heading() -> dict:
    return {"type": "heading", "text": CHECKED_HEADING}


def plugin_heading() -> dict:
    return {"type": "heading", "text": PLUGIN_HEADING}


def refuse_reserved_labels(validated: list[dict]) -> None:
    """Raise ``ValueError`` if a validated ``fields`` block uses a label of ``card_block``,
    compared case-insensitively and trimmed."""
    reserved = {label.casefold() for label in RESERVED_LABELS}
    for block in validated:
        if block.get("type") == "fields" and any(
            item["label"].strip().casefold() in reserved for item in block["items"]
        ):
            raise ValueError(RESERVED_LABEL_MESSAGE)


def param_description(description: str, spec: FileParamSpec) -> str:
    hint = FILE_PARAM_HINT.format(max_bytes=spec.max_bytes, types=", ".join(spec.media_types))
    description = description.strip()
    return f"{description} {hint}" if description else hint


__all__ = [
    "CHECKED_HEADING", "FILE_PARAM_HINT", "IncomingFile", "PLUGIN_HEADING", "RESERVED_LABELS",
    "RESERVED_LABEL_MESSAGE", "UNNAMED_FILE", "card_block", "checked_heading", "declared_media_type",
    "file_reference", "param_description", "plugin_heading", "refuse_reserved_labels", "resolve_file",
    "sniff_media_type",
]
