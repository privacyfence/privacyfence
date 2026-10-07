"""Source fixtures: the daemon's ``source.call`` answered from recorded or hand-written data."""
from __future__ import annotations

import base64
import binascii
import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .._rpc import ERROR_CODES, RpcError

# Copied from the protocol, like the limits in plugin.py. The daemon's own tests compare them.
SOURCE_OPERATIONS: tuple[str, ...] = (
    "salesforce.report_run", "jira.search", "drive.download",
    "sheets.get_values", "confluence.get_page", "calendar.list_events",
)
DRIVE_CHUNK_BYTES = 8 * 1024 * 1024
DRIVE_MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_RESULT_BYTES = 12 * 1024 * 1024

_SAMPLES_DIR = Path(__file__).resolve().parent / "samples"


class SourceFixtureMissing(LookupError):
    """The plugin made a ``source.call`` that no fixture answers."""


@dataclass(frozen=True)
class SourceCall:
    """One ``source.call`` the plugin made, as the daemon saw it."""

    operation: str
    params: dict
    principal: str


def encode_cursor(file_id: str, revision: str, offset: int) -> str:
    raw = json.dumps({"f": file_id, "r": revision, "o": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, str, int]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        file_id, revision, offset = data["f"], data["r"], data["o"]
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise RpcError("invalid_params", "cursor is not valid") from None
    if not isinstance(file_id, str) or not isinstance(revision, str) or isinstance(offset, bool) \
            or not isinstance(offset, int) or offset < 0:
        raise RpcError("invalid_params", "cursor is not valid")
    return file_id, revision, offset


@dataclass
class _Rule:
    operation: str
    params: dict
    data: Any = None
    next_cursor: str | None = None
    error: RpcError | None = None


class _When:
    """``host.source.when(op, **params)``: what to answer a call that carries these params."""

    def __init__(self, fixtures: SourceFixtures, operation: str, params: dict) -> None:
        self._fixtures = fixtures
        self._operation = operation
        self._params = params

    def returns(self, data: Any, next_cursor: str | None = None) -> None:
        self._fixtures._add(_Rule(self._operation, self._params, data=data, next_cursor=next_cursor))


def _error(code: str, reason: str | None) -> RpcError:
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code {code!r}")
    return RpcError(code, "fixture failure", extra={"reason": reason} if reason else None)


def _check_operation(operation: Any) -> str:
    if operation not in SOURCE_OPERATIONS:
        raise ValueError(f"unknown source operation {operation!r}; expected one of {', '.join(SOURCE_OPERATIONS)}")
    return operation


class SourceFixtures:
    """``host.source``. A call answers from the most specific fixture whose params it carries."""

    def __init__(self) -> None:
        self.calls: list[SourceCall] = []
        self._rules: list[_Rule] = []
        self._drive_files: dict[str, dict] = {}
        self._missing: SourceFixtureMissing | None = None

    # ------------------------------------------------------------------ setup

    def load(self, fixture: dict) -> None:
        """Add one fixture: ``samples.get(op)``, ``samples.drive_download(...)`` or your own copy.

        A fixture is ``{"operation", "params", "data", "next_cursor"}``. ``params`` is optional
        and means "any call that carries at least these params".
        """
        if not isinstance(fixture, dict):
            raise ValueError("a fixture must be a dict")
        operation = _check_operation(fixture.get("operation"))
        file = fixture.get("drive_file")
        if file is not None:
            if operation != "drive.download":
                raise ValueError("drive_file belongs to a drive.download fixture")
            self._drive_files[str(file["file_id"])] = copy.deepcopy(file)
            return
        if "data" not in fixture:
            raise ValueError("a fixture needs data")
        self._add(_Rule(
            operation, dict(fixture.get("params") or {}),
            data=copy.deepcopy(fixture["data"]), next_cursor=fixture.get("next_cursor"),
        ))

    def when(self, operation: str, **params: Any) -> _When:
        return _When(self, _check_operation(operation), params)

    def fail(self, operation: str, code: str, reason: str | None = None, **params: Any) -> None:
        """Make calls that carry ``params`` fail like the daemon would with ``code``."""
        self._add(_Rule(_check_operation(operation), params, error=_error(code, reason)))

    def _add(self, rule: _Rule) -> None:
        self._rules.append(rule)

    # ------------------------------------------------------------------ serving

    def take_missing(self) -> SourceFixtureMissing | None:
        missing, self._missing = self._missing, None
        return missing

    def handle(self, params: dict, *, mode: str, audit: Callable[[str, str], None]) -> dict:
        """The daemon's ``source.call`` for one request. ``audit(operation, summary)`` records it."""
        operation = params.get("operation")
        try:
            result = self._serve(params, mode)
        except RpcError as exc:
            audit(str(operation), f"error={exc.code}")
            raise
        audit(operation, f"bytes={result['bytes']}")
        return result

    def _serve(self, params: dict, mode: str) -> dict:
        if mode == "local" and "credential" in params:
            raise RpcError("org_only_field", "credential is only accepted in org mode")
        principal = params.get("principal")
        if not isinstance(principal, str) or not principal:
            raise RpcError("invalid_params", "principal must be a string")
        if mode == "local" and principal != "local":
            raise RpcError("unknown_principal", "unknown principal")
        operation = params.get("operation")
        if operation not in SOURCE_OPERATIONS:
            raise RpcError("operation_not_allowed", "the operation is not allowed")
        call_params = params.get("params")
        if not isinstance(call_params, dict):
            raise RpcError("invalid_params", "params must be an object")
        self.calls.append(SourceCall(operation, copy.deepcopy(call_params), principal))

        rule = self._best_rule(operation, call_params)
        if rule is not None and rule.error is not None:
            raise rule.error
        if rule is not None:
            data, cursor = copy.deepcopy(rule.data), rule.next_cursor
        elif operation == "drive.download" and str(call_params.get("file_id")) in self._drive_files:
            data, cursor = self._serve_drive(call_params)
        else:
            self._missing = SourceFixtureMissing(
                f"no fixture answers source.call {operation} with params {json.dumps(call_params, sort_keys=True)}; "
                "add one with host.source.load(...) or host.source.when(...).returns(...)"
            )
            raise RpcError("connector_unavailable", "no fixture", extra={"reason": "unavailable"})
        payload = json.dumps(data, default=str, ensure_ascii=False)
        size = len(payload.encode("utf-8"))
        if size > MAX_SOURCE_RESULT_BYTES:
            raise RpcError("payload_too_large", "the result is too large")
        return {"operation": operation, "data": json.loads(payload), "bytes": size, "next_cursor": cursor}

    def _best_rule(self, operation: str, call_params: dict) -> _Rule | None:
        best: _Rule | None = None
        for rule in self._rules:
            if rule.operation != operation:
                continue
            if any(key not in call_params or call_params[key] != value for key, value in rule.params.items()):
                continue
            if best is None or len(rule.params) >= len(best.params):
                best = rule
        return best

    def _serve_drive(self, params: dict) -> tuple[dict, str | None]:
        """One chunk of a fixture file, with the daemon's validation, cursors and ``revision_changed``."""
        file_id = params["file_id"]
        file = self._drive_files[file_id]
        content = base64.b64decode(file["content_base64"])
        offset, cursor = params.get("offset"), params.get("cursor")
        length = params.get("length", DRIVE_CHUNK_BYTES)
        if isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= DRIVE_CHUNK_BYTES:
            raise RpcError("invalid_params", f"params.length must be between 1 and {DRIVE_CHUNK_BYTES}")
        if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
            raise RpcError("invalid_params", "params.offset must be at least 0")
        if cursor is not None and not isinstance(cursor, str):
            raise RpcError("invalid_params", "params.cursor must be a string")
        if offset is not None and cursor is not None:
            raise RpcError("invalid_params", "offset and cursor cannot both be given")
        revision = file["revision"]
        if cursor is not None:
            cursor_file, cursor_revision, offset = decode_cursor(cursor)
            if cursor_file != file_id:
                raise RpcError("invalid_params", "cursor belongs to a different file")
            if cursor_revision != revision:
                raise RpcError(
                    "upstream_error", "the file changed while it was being downloaded",
                    extra={"reason": "revision_changed"},
                )
        start = offset or 0
        if start > len(content):
            raise RpcError("invalid_params", "offset is past the end of the file")
        end = min(start + length, len(content))
        chunk = content[start:end]
        eof = end >= len(content)
        data = {
            "file_id": file_id,
            "mime_type": file["mime_type"],
            "revision": revision,
            "total_size_bytes": len(content),
            "offset": start,
            "length": len(chunk),
            "eof": eof,
            "content_base64": base64.b64encode(chunk).decode("ascii"),
        }
        return data, None if eof else encode_cursor(file_id, revision, end)


class _Samples:
    """``samples``: hand-written, redacted data in the shape each source operation returns."""

    def get(self, operation: str) -> dict:
        """A fixture for ``operation`` that ``host.source.load`` accepts. Each call returns a fresh copy."""
        _check_operation(operation)
        return json.loads((_SAMPLES_DIR / f"{operation}.json").read_text(encoding="utf-8"))

    def drive_download(
        self, data: bytes, *, revision: str = "r1", file_id: str = "EXAMPLE-1",
        mime_type: str = "application/octet-stream",
    ) -> dict:
        """A fixture that serves ``data`` in chunks of ``DRIVE_CHUNK_BYTES`` with the daemon's cursors."""
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("data must be bytes")
        if len(data) > DRIVE_MAX_FILE_BYTES:
            raise ValueError(f"a Drive download is at most {DRIVE_MAX_FILE_BYTES} bytes")
        return {
            "operation": "drive.download",
            "drive_file": {
                "file_id": file_id,
                "revision": revision,
                "mime_type": mime_type,
                "content_base64": base64.b64encode(bytes(data)).decode("ascii"),
            },
        }


samples = _Samples()

