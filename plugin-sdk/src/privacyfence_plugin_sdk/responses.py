"""Page responses, results of the host calls, and the SDK's errors."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "Bytes", "ConfirmResult", "DownloadedFile", "Html", "SourceError", "SourceResult", "Text",
    "ToolDefinitionError",
]


class ToolDefinitionError(ValueError):
    """A tool definition that PrivacyFence would refuse; raised when the tool is registered."""


class SourceError(Exception):
    """A ``source.call`` or confirmation call that the host refused or that failed upstream."""

    def __init__(self, code: str, detail: str = "", reason: str | None = None) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.reason = reason


@dataclass(frozen=True)
class SourceResult:
    data: Any
    bytes: int
    next_cursor: str | None


@dataclass(frozen=True)
class ConfirmResult:
    status: str
    decided_at: str | None = None


@dataclass(frozen=True)
class DownloadedFile:
    path: Path
    size: int
    revision: str
    mime_type: str


@dataclass(frozen=True)
class Html:
    """An HTML page. It must be self-contained: inline its CSS, scripts and images."""

    body: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    def to_wire(self) -> dict:
        return _wire(self.status, "text/html; charset=utf-8", self.body.encode("utf-8"), self.headers)


@dataclass(frozen=True)
class Text:
    body: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    def to_wire(self) -> dict:
        return _wire(self.status, "text/plain; charset=utf-8", self.body.encode("utf-8"), self.headers)


@dataclass(frozen=True)
class Bytes:
    body: bytes
    content_type: str = "application/octet-stream"
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    def to_wire(self) -> dict:
        return _wire(self.status, self.content_type, self.body, self.headers, binary=True)


def _wire(status: int, content_type: str, body: bytes, headers: dict[str, str], *, binary: bool = False) -> dict:
    merged = {k.lower(): v for k, v in headers.items()}
    merged["content-type"] = merged.get("content-type", content_type)
    if binary:
        return {
            "status": status,
            "headers": merged,
            "body": base64.b64encode(body).decode("ascii"),
            "body_encoding": "base64",
        }
    return {"status": status, "headers": merged, "body": body.decode("utf-8"), "body_encoding": "utf8"}
