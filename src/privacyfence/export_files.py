"""Pure helpers the export tools share: the file name and format, CSV and JSON
rendering, the card's sample table and details, and the result summary. No
I/O; local_files.deliver_file writes the file."""
from __future__ import annotations

import csv
import io
import json
from typing import Any

EXPORT_MAX_ROWS = 50_000
SAMPLE_ROWS = 20
SCAN_CHARS = 1_000_000
_SAMPLE_CELL_CHARS = 200
_MAX_NAME_CHARS = 200

FILE_NAME_ERROR = "file_name must be a plain file name ending in .csv or .json, such as Contacts.csv."
DESTINATION_ERROR = "destination_dir must be a folder path, absolute or starting with ~/."
MIME_TYPES = {"csv": "text/csv", "json": "application/json"}


def export_file_name(file_name: str, default_stem: str) -> tuple[str, str]:
    name = file_name.strip() or f"{default_stem}.csv"
    if (
        any(ch in name for ch in ("/", "\\", "\x00"))
        or name.startswith(".")
        or len(name) > _MAX_NAME_CHARS
    ):
        raise ValueError(FILE_NAME_ERROR)
    suffix = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if suffix not in MIME_TYPES:
        raise ValueError(FILE_NAME_ERROR)
    return name, suffix


def check_destination(destination_dir: str) -> str:
    stripped = destination_dir.strip()
    if not stripped:
        raise ValueError(DESTINATION_ERROR)
    return stripped


def refuse_org(tool: str, download_mode: str, alternative: str) -> None:
    if download_mode == "org":
        raise ValueError(
            f"{tool} writes a file on your computer, which an organization-managed install "
            f"cannot do. Use {alternative} instead."
        )


def csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def render(columns: list[str], rows: list[list[Any]], fmt: str) -> bytes:
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([csv_cell(v) for v in row])
        return buf.getvalue().encode("utf-8")
    text = json.dumps(
        [dict(zip(columns, row, strict=True)) for row in rows], ensure_ascii=False, separators=(",", ":"),
    )
    return (text + "\n").encode("utf-8")


def _short(text: str) -> str:
    if len(text) > _SAMPLE_CELL_CHARS:
        return text[:_SAMPLE_CELL_CHARS] + "…"
    return text


def sample_table(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    shown = [[_short(csv_cell(v)) for v in row] for row in rows[:SAMPLE_ROWS]]
    return {
        "caption": "First rows",
        "headers": columns,
        "rows": shown,
        "footer": f"Showing {len(shown)} of {len(rows)} rows.",
    }


def scan_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")[:SCAN_CHARS]


def card_details(
    preview: dict[str, str], rows: int, columns: list[str], saved_to: str, sha256: str,
) -> str:
    lines = [f"{key}: {value}" for key, value in preview.items()]
    lines += [
        f"Rows: {rows:,}",
        f"Columns: {', '.join(columns)}",
        f"Saved to: {saved_to}",
        f"SHA-256: {sha256}",
    ]
    return "\n".join(lines)


def export_summary(
    columns: list[str], rows: list[list[Any]], sha256: str, delivered: dict[str, Any],
) -> dict[str, Any]:
    return {"rows": len(rows), "columns": columns, "sha256": sha256, **delivered}
