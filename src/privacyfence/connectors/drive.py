"""Google Drive connector."""
from __future__ import annotations

import asyncio
import binascii
import json
import logging
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .. import local_files
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..download_staging import get_download_staging_store
from ..drive_client import (
    DriveClient,
    DriveClientError,
    _parse_a1_range,
    resolve_download_name,
)
from ..google_errors import unavailable_error
from ..gate import current_reason, gated_call
from ..org_mode import DownloadDeliveryConfig, base64_length
from ..principal import current_principal
from ..privacy_filter import apply_list, apply_text, category_policy
from ..text_extraction import EXTRACTABLE_MIME_TYPES, extract_text, guess_mime_type, is_prefetch_worthy, preview_blocks_for

logger = logging.getLogger(__name__)

# Cap on how big an uploaded file we'll read into memory pre-approval just to build an upload
# preview.
_UPLOAD_PREVIEW_MAX_BYTES = 5_000_000

# The ceiling passed to local_files.require_local_files() for drive_upload_file's upload_id --
# an upload slot is buffered entirely in memory, so this is a real memory cap, not just a
# preview-read cap like _UPLOAD_PREVIEW_MAX_BYTES above.
_UPLOAD_MAX_BYTES = 50_000_000

# Reuses the same 5MB value gmail.py's
# _ATTACHMENT_PREFETCH_MAX_BYTES already uses for "small enough to just
# fetch the whole thing instead of a bounded prefetch" -- see
# _download_file's own PII-scan comment for why a *truncated* prefetch is
# actively wrong for a format like PDF, not just incomplete.
_FULL_FETCH_PII_SCAN_MAX_BYTES = 5_000_000

# drive_get_file_content reads a PDF/DOCX/PPTX/XLSX in full, up to this
# size, to return its text rather than a pointer to drive_download_file
# (see _get_file_content).
_EXTRACT_FULL_FETCH_MAX_BYTES = 20_000_000

# The most text drive_get_file_content returns from an extracted file,
# measured as the JSON the tool result carries it in: json.dumps escapes
# every non-ASCII character as \uXXXX, so a Hungarian or Greek document is
# longer on the wire than in characters. Same figure, and same reason, as
# org mode's inline-download limit: two thirds of the ~150,000-character
# tool result claude.ai and Claude Desktop accept (ADR 0092).
_EXTRACTED_TEXT_MAX_CHARS = 100_000

# modified_after: YYYY-MM-DD, or an RFC 3339 UTC time ending in Z.
_MODIFIED_AFTER_PATTERN = (
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?Z)?"
)


# Parameter descriptions shared by many tools (kept in one place so they stay in step).
_FILE_ID_SOURCES = "drive_list_files, drive_list_folder or a Drive URL (the id is the long token after /d/ or /folders/)"
_FILE_ID_HELP = "Id of the Drive file, from " + _FILE_ID_SOURCES + "."
_SPREADSHEET_ID_HELP = (
    "Id of the spreadsheet (a Drive file id), from drive_list_files, drive_sheets_create or "
    "a spreadsheet URL (the token after /d/); not a tab name."
)
_SHEET_ID_HELP = (
    "Numeric id of the tab (its sheet_id), from drive_sheets_get_metadata; not the tab's "
    "title and not the spreadsheet_id."
)


def _truncate_to_json_chars(text: str, max_chars: int) -> tuple[str, bool]:
    """The longest prefix of ``text`` whose JSON string encoding (quotes
    excluded) is at most ``max_chars`` long, and whether it was cut."""
    def encoded_length(prefix: str) -> int:
        return len(json.dumps(prefix)) - 2

    if encoded_length(text) <= max_chars:
        return text, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if encoded_length(text[:middle]) <= max_chars:
            low = middle
        else:
            high = middle - 1
    return text[:low], True


def _parse_json_str_list(value: str) -> list[str] | None:
    """Parse a JSON array-of-strings tool argument, or None if empty/invalid."""
    if not value or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) and all(isinstance(v, str) for v in parsed) else None


def _parse_json_2d_list(value: str) -> list[list] | None:
    """Parse a JSON array-of-arrays tool argument, or None if invalid."""
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


_SHEET_ROW_LIMIT = 50  # shared between _format_sheet_rows (legacy details_text) and
# _sheets_get_values's own v2 preview_tables build, so the two can never disagree
# about where the cap/"more row(s)" footer lands.


def _format_sheet_rows(rows: list[list], limit: int = _SHEET_ROW_LIMIT) -> str:
    """Render 2D sheet cell data as comma-joined lines, capped at `limit` rows."""
    shown = "\n".join(", ".join(str(cell) for cell in row) for row in rows[:limit])
    if len(rows) > limit:
        shown += f"\n… and {len(rows) - limit} more row(s)"
    return shown


def _sheet_values_table(values: list[list], limit: int = _SHEET_ROW_LIMIT) -> dict:
    """The same 2D cell data _format_sheet_rows renders as a comma-joined
    text dump, instead built as a preview_tables-shaped table dict --
    headerless, since an arbitrary A1-notation range has no guaranteed
    header row (the first row is just more data, not necessarily a label,
    same reasoning _sheets_get_values already used), capped at `limit`
    rows with a footer noting the rest. Shared by every sheet tool --
    read (_sheets_get_values) and write (_sheets_write_range) -- that
    shows real cell data in v2's right pane, so they can never disagree
    about where the cap/footer lands."""
    table: dict[str, Any] = {"rows": [[str(cell) for cell in row] for row in values[:limit]]}
    if len(values) > limit:
        table["footer"] = f"… and {len(values) - limit} more row(s)"
    return table


class DriveConnector(Connector):
    def __init__(self, client: DriveClient) -> None:
        self._drive = client
        self.my_email: str = ""
        # Set post-construction by daemon_main.py's build_connectors,
        # exactly like my_email above.
        self.download_config: DownloadDeliveryConfig | None = None
        self.download_base_url: str = ""
        self.session_created_ids: set[str] = set()
        # file_id -> Drive modifiedTime as of PrivacyFence's own last write to
        # that file. Lets a subsequent read of the *exact same, still-
        # unchanged* content skip the PII gate's forced confirmation (not PII
        # detection itself) -- see gate.py's pii_already_reviewed and its
        # module docstring. Same daemon-process lifetime and cross-chat
        # sharing as session_created_ids above, and the same
        # never-persisted-to-disk tradeoff -- restarting the daemon forgets
        # it, which is fine: the next read of a previously-"reviewed" file
        # just falls back to the ordinary PII gate once, not a security gap.
        self.own_write_revisions: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "drive"

    @property
    def client(self) -> DriveClient:
        return self._drive

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="drive_list_files",
                description=(
                    "Search Google Drive and return matching file metadata "
                    "(id, name, mime_type, owners, sharing status). Returns a list of {id, name, "
                    "mime_type, size, created_time, modified_time, owners, shared, web_view_link, "
                    "parent_ids, drive_id, thumbnail_link}: at most "
                    "max_results files (default 20, capped at 1000), read across pages, in the order the Drive API "
                    "gives for the query; fields may be redacted by the user's privacy settings. "
                    "Use drive_list_folder to list one folder's children, or "
                    "drive_get_file_metadata when you already have a file's id. Auto-approved."
                ),
                params=[
                    ToolParam("query", "str", description=(
                        "Drive API 'q' search query syntax, not a plain-text search "
                        "string, e.g. \"name contains 'Foo'\" or \"fullText contains 'Foo'\". "
                        "See https://developers.google.com/drive/api/guides/search-files"
                    )),
                    ToolParam("max_results", "int", required=False, default=20, description=(
                        "Most files to return. Default 20, capped at 1000. Reads up to "
                        "max_results items across pages."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_get_file_metadata",
                description=(
                    "Fetch metadata for a single Drive file by id "
                    "(name, owners, times, sharing status). Returns one file in the same shape "
                    "drive_list_files lists ({id, name, mime_type, size, created_time, "
                    "modified_time, owners, shared, web_view_link, parent_ids, drive_id, "
                    "thumbnail_link}), or only {id} if the user's privacy settings hide file "
                    "metadata. Use drive_get_file_content to read the file's text. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("file_id", "str", description=_FILE_ID_HELP),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_list_revisions",
                description=(
                    "List the saved versions (revision history) of a Drive file by id: when each "
                    "was saved and by whom. Returns a list of {id, modified_time, author_name, "
                    "author_email, size, keep_forever, before_cutoff}, newest first: at most "
                    "max_results (default 50, capped at 1000) plus the before_cutoff entry, read "
                    "across pages. Google merges a Doc's, Sheet's or Slides' frequent edits into "
                    "fewer versions, and does not say which cell or paragraph each one changed. If "
                    "the user's privacy settings hide file metadata, each entry is only {id, "
                    "before_cutoff}. Use drive_get_revision_content to read one version's text, "
                    "and drive_get_file_metadata for the file's current details. Auto-approved."
                ),
                params=[
                    ToolParam("file_id", "str", description=_FILE_ID_HELP),
                    ToolParam("modified_after", "str", required=False, default="", description=(
                        "Only versions saved at or after this UTC time, as YYYY-MM-DD or an RFC "
                        "3339 time such as 2026-10-05T00:00:00Z. The newest version saved before "
                        "it is added last, marked before_cutoff, as the 'before' side to compare "
                        "against. Empty returns the newest versions with no cutoff."
                    )),
                    ToolParam("max_results", "int", required=False, default=50, description=(
                        "Most versions to return, newest first, not counting the before_cutoff "
                        "entry. Default 50, capped at 1000. Older versions past the cap are left "
                        "out."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_list_folder",
                description=(
                    "List the direct children of a Drive folder by id. Returns a list of files in "
                    "the same shape drive_list_files returns: at most "
                    "max_results children (default 50, capped at 1000), read across pages, excluding trashed files, "
                    "in the order the Drive API gives. Use drive_list_files to search by name or "
                    "content instead of browsing one folder. Auto-approved."
                ),
                params=[
                    ToolParam("folder_id", "str", description=(
                        "Id of the folder whose children to list, from drive_list_files, another "
                        "file's parent_ids, or drive_list_shared_drives (a shared drive's id "
                        "lists its top level)."
                    )),
                    ToolParam("max_results", "int", required=False, default=50, description=(
                        "Most children to return. Default 50, capped at 1000. Reads up to "
                        "max_results items across pages."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_create_blank_file",
                description=(
                    "Create a new blank Drive file. Returns {id, name, mime_type} of the new, "
                    "empty file. Fill it with drive_write_doc_content (a Google Doc), "
                    "drive_write_file_content (a text file) or drive_upload_file (any file with "
                    "content); use drive_sheets_create for a spreadsheet. Auto-approved."
                ),
                params=[
                    ToolParam("name", "str", description="Name of the new file, as shown in Drive."),
                    ToolParam("mime_type", "str", description=(
                        "MIME type of the file to create, e.g. 'application/vnd.google-apps.document' "
                        "for a Google Doc or 'text/plain' for a text file."
                    )),
                    ToolParam("parent_folder_id", "str", required=False, default="", description=(
                        "Id of the folder to create it in. Empty creates it in My Drive's root."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_get_file_content",
                description=(
                    "Fetch the content of a Drive file by id. A Google Doc comes back as "
                    "Markdown (headings, **bold**, *italic*, ~~strikethrough~~, __underline__, "
                    "`code`, [link](url), ==highlight==, '---' dividers, nested bullet/numbered "
                    "lists with a 2-space indent per level, GFM pipe tables with alignment), the "
                    "syntax drive_write_doc_content accepts, so it round-trips into it or "
                    "drive_docs_edit_content. An exact highlight or text color Markdown can't "
                    "carry comes back in 'highlights'/'text_colors' ({text, hex} lists). A Sheet "
                    "comes back as CSV, Slides as plain text. A PDF, .docx, .pptx or .xlsx comes "
                    "back as its extracted text (a scanned PDF has none), with 'truncated': true "
                    "when cut to fit; a .zip as its list of entries. Other files, such as images, "
                    "only get a placeholder: use drive_download_file for those. Returns "
                    "{file_id, content}, plus truncated, highlights and text_colors when they "
                    "apply; content may be redacted by the user's privacy settings. Requires "
                    "user approval."
                ),
                params=[
                    ToolParam("file_id", "str", description=_FILE_ID_HELP),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_get_revision_content",
                description=(
                    "Fetch the content of one earlier saved version of a Drive file, by file id "
                    "and revision id. A Google Doc or Slides version comes back as plain text (not "
                    "the Markdown drive_get_file_content gives), a Sheet version as a Markdown "
                    "table per tab, a PDF, .docx, .pptx or .xlsx version as its extracted text, a "
                    ".zip as its list of entries, and a text file as its text; other files, such "
                    "as images, only get a placeholder with their size. Returns {file_id, "
                    "revision_id, modified_time, content}, plus 'truncated': true when content was "
                    "cut to fit; content may be redacted by the user's privacy settings. To "
                    "compare with the current version, read the newest revision from "
                    "drive_list_revisions with this tool rather than mixing in "
                    "drive_get_file_content's different format. Requires user approval."
                ),
                params=[
                    ToolParam("file_id", "str", description=_FILE_ID_HELP),
                    ToolParam("revision_id", "str", description=(
                        "Id of the version to read, from drive_list_revisions; the newest entry "
                        "there is the current version, so read it the same way to compare like "
                        "with like."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_write_file_content",
                description=(
                    "Write content to an existing Drive file. Replaces the file's whole content "
                    "with content, written as UTF-8 text. Returns {file_id, modified_time}. Use "
                    "drive_write_doc_content instead for a formatted Google Doc, or "
                    "drive_upload_file for a binary file or a new one. Requires user approval."
                ),
                params=[
                    ToolParam("file_id", "str", description=_FILE_ID_HELP),
                    ToolParam("content", "str", description=(
                        "The full new content of the file, as plain text. It replaces everything "
                        "already in the file."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_upload_file",
                description=(
                    "Upload any file (e.g. a PDF or image) to Drive as a new file — "
                    "use this instead of drive_write_file_content for any binary "
                    "file, since that tool only writes UTF-8 text. Provide exactly "
                    "one of upload_id (the id privacyfence_create_upload_slot "
                    "returned after you PUT the file's bytes to its upload_url) or "
                    "content_base64 (base64-encoded file bytes, for a small file; "
                    "'name' is then required). Returns {id, name, mime_type, "
                    "size_bytes}. Requires user approval."
                ),
                params=[
                    ToolParam("content_base64", "str", required=False, default="", description=(
                        "The file's bytes, base64-encoded. Requires name. Give exactly one of "
                        "content_base64 and upload_id; empty otherwise."
                    )),
                    ToolParam("upload_id", "str", required=False, default="", description=(
                        "Id returned by privacyfence_create_upload_slot after you PUT the bytes "
                        "to its upload_url. Give exactly one of content_base64 and upload_id; "
                        "empty otherwise."
                    )),
                    ToolParam("name", "str", required=False, default="", description=(
                        "Name for the new Drive file. Required with content_base64; with "
                        "upload_id, empty names it \"(unnamed file)\"."
                    )),
                    ToolParam("parent_folder_id", "str", required=False, default="", description=(
                        "Id of the folder to upload into. Empty uploads to My Drive's root."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_move_file",
                description=(
                    "Move a Drive file to a different folder. The file leaves every folder it is "
                    "in now. Returns {file_id, new_parent}. Get the destination's id from "
                    "drive_list_folder. Requires user approval."
                ),
                params=[
                    ToolParam("file_id", "str", description="Id of the file or folder to move, from " + _FILE_ID_SOURCES + "."),
                    ToolParam("destination_folder_id", "str", description=(
                        "Id of the folder to move it into, from drive_list_folder or "
                        "drive_list_files."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_copy_file",
                description=(
                    "Copy a Drive file into a folder. The original stays where it is, unchanged. "
                    "Returns {id, name, mime_type, web_view_link, source_file_id, parent}, where id "
                    "is the new copy's id. Folders cannot be copied. Get the destination's id from "
                    "drive_list_folder. Requires user approval."
                ),
                params=[
                    ToolParam("file_id", "str", description="Id of the file to copy, from " + _FILE_ID_SOURCES + "."),
                    ToolParam("destination_folder_id", "str", description=(
                        "Id of the folder to put the copy in, from drive_list_folder or "
                        "drive_list_files."
                    )),
                    ToolParam("name", "str", required=False, default="", description=(
                        "Name for the copy. Empty keeps the original file's name."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_add_comment",
                description=(
                    "Add a comment to a Drive file. Returns {file_id, comment_id, content}, "
                    "where comment_id is the new comment's id. The comment is posted as the "
                    "user and is not anchored to any text. Requires user approval."
                ),
                params=[
                    ToolParam("file_id", "str", description=_FILE_ID_HELP),
                    ToolParam("comment", "str", description="Text of the comment to post on the file."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_list_shared_drives",
                description=(
                    "List all Google Workspace Shared Drives the user can access "
                    "(returns id and name for each). Returns a list of {id, name}: at most "
                    "max_results drives (default 50, capped at 1000), read across pages. "
                    "Pass a drive's id as folder_id to drive_list_folder to browse it. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("max_results", "int", required=False, default=50, description=(
                        "Most shared drives to return. Default 50, capped at 1000. Reads up to "
                        "max_results items across pages."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_write_doc_content",
                description=(
                    "Write Markdown content to a Google Doc with rich formatting: "
                    "headings (# through ######), **bold**, *italic*, "
                    "***bold-italic***, ~~strikethrough~~, __underline__, `code`, "
                    "==highlight== (these five nest freely with each other, e.g. "
                    "==**bold and highlighted**==), [link](url) (escape a literal "
                    "'[' or ']' in the link text as '\\['/'\\]'), bullet/numbered "
                    "lists (indent a sub-list 2 spaces per nesting level), GFM pipe "
                    "tables (a '| --- |' separator row under the header; ':---'/"
                    "'---:'/':---:' for left/right/center column alignment), and "
                    "'---'/'***'/'___' on their own line as a horizontal-rule "
                    "divider. Clears the existing document content "
                    "before writing — use drive_docs_edit_content or "
                    "drive_docs_format_content instead for a change that "
                    "shouldn't touch the rest of the document. "
                    "Use this instead of drive_write_file_content when the target "
                    "is a Google Doc and you want formatted output. "
                    "Returns {file_id}. Requires user approval."
                ),
                params=[
                    ToolParam("file_id", "str", description=(
                        "Id of the Google Doc to overwrite, from " + _FILE_ID_SOURCES + "."
                    )),
                    ToolParam("markdown", "str", description=(
                        "Markdown for the document's new content, in the syntax listed above. "
                        "Replaces everything already in the Doc."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_docs_edit_content",
                description=(
                    "Replace one occurrence of existing text in a Google Doc "
                    "with new Markdown, without touching the rest of the "
                    "document. find_text must match exactly one location in "
                    "the document's plain, unformatted text — the words as "
                    "typed, with no Markdown syntax in them at all (this is "
                    "*not* the same as drive_get_file_content's output for a "
                    "Doc, which now renders formatting as Markdown; strip any "
                    "'**'/'#'/etc. markers back out of find_text first) — "
                    "include enough surrounding context to make it unique, "
                    "the same way a unique-match text editor requires; set "
                    "replace_all=true to replace every occurrence instead. "
                    "replace_markdown supports the same Markdown syntax as "
                    "drive_write_doc_content, including GFM pipe tables. "
                    "Returns {file_id, occurrences_replaced}. Use "
                    "drive_docs_format_content to change formatting without changing the text. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("file_id", "str", description=(
                        "Id of the Google Doc to edit, from " + _FILE_ID_SOURCES + "."
                    )),
                    ToolParam("find_text", "str", description=(
                        "Exact plain-text substring to locate, as typed in the document with no "
                        "Markdown markers. Must match exactly one place unless replace_all is true."
                    )),
                    ToolParam(
                        "replace_markdown", "str",
                        description=(
                            "Markdown to insert in place of the match, in the syntax "
                            "drive_write_doc_content accepts. Empty deletes the matched text."
                        ),
                    ),
                    ToolParam("replace_all", "bool", required=False, default=False, description=(
                        "Default false: find_text must match exactly one place or the call "
                        "fails. True replaces every occurrence."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_docs_format_content",
                description=(
                    "Apply formatting (bold, italic, highlight, text color) to "
                    "existing text in a Google Doc, located the same way as "
                    "drive_docs_edit_content, without changing the text itself. "
                    "Every formatting parameter is opt-in — its default means "
                    "'leave that aspect unchanged', so a call that only sets "
                    "highlight_color never touches bold/italic already on the "
                    "matched text. Returns {file_id, occurrences_formatted}. Use "
                    "drive_docs_edit_content to change the text itself. Requires user "
                    "approval."
                ),
                params=[
                    ToolParam("file_id", "str", description=(
                        "Id of the Google Doc to format, from " + _FILE_ID_SOURCES + "."
                    )),
                    ToolParam("find_text", "str", description=(
                        "Exact plain-text substring to format, as typed in the document with no "
                        "Markdown markers. Must match exactly one place unless replace_all is true."
                    )),
                    ToolParam("bold", "str", required=False, default="",
                              description="'true' or 'false'; omit to leave unchanged"),
                    ToolParam("italic", "str", required=False, default="",
                              description="'true' or 'false'; omit to leave unchanged"),
                    ToolParam("highlight_color", "str", required=False, default="",
                              description="hex color e.g. '#fff59d'; omit to leave unchanged"),
                    ToolParam("text_color", "str", required=False, default="",
                              description="hex color e.g. '#000000'; omit to leave unchanged"),
                    ToolParam("replace_all", "bool", required=False, default=False, description=(
                        "Default false: find_text must match exactly one place or the call "
                        "fails. True formats every occurrence."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_download_file",
                description=(
                    "Download a Drive file. Google Workspace documents are exported "
                    "as text/CSV. Returns {delivery: 'inline', name, mime_type, "
                    "size_bytes, content_base64} for a small file, or {delivery: "
                    "'link', name, size_bytes, download_url, expires_at, note} for a "
                    "larger one: a one-time link the human opens in their own browser. "
                    "Use drive_get_file_content instead to read a file's text. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("file_id", "str", description=_FILE_ID_HELP),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_sheets_create",
                description=(
                    "Create a new Google Sheets spreadsheet, optionally with "
                    "named tabs. Returns {id, name, web_view_link}; id is the spreadsheet_id "
                    "the other drive_sheets_* tools take. Use drive_sheets_add_sheet to add a "
                    "tab to a spreadsheet that already exists. Auto-approved."
                ),
                params=[
                    ToolParam("name", "str", description="Title of the new spreadsheet, as shown in Drive."),
                    ToolParam(
                        "sheet_titles", "str", required=False, default="",
                        description=(
                            'JSON array of tab names, e.g. ["Q1","Q2"]. '
                            "Defaults to a single 'Sheet1' tab if omitted."
                        ),
                    ),
                    ToolParam("parent_folder_id", "str", required=False, default="", description=(
                        "Id of the folder to move the new spreadsheet into. Empty leaves it in "
                        "My Drive's root."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_sheets_get_metadata",
                description=(
                    "List the tabs in a spreadsheet (id, title, index, row/column "
                    "count). Returns a list of {sheet_id, title, index, row_count, "
                    "column_count, hidden}, one per tab. Use drive_sheets_get_values to read "
                    "a tab's cells; sheet_id is the tab id other drive_sheets_* tools take. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("spreadsheet_id", "str", description=_SPREADSHEET_ID_HELP),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_sheets_get_values",
                description=(
                    "Read a range of cells from a spreadsheet: display values by "
                    "default, or the underlying values, or formulas instead of "
                    "computed results, and optionally cell formatting alongside "
                    "them. Returns the cells as a list of rows (each a list of values; the "
                    "Sheets API leaves out trailing empty cells), or {values, formatting} "
                    "with per-cell formatting dicts when include_formatting is true; values "
                    "may be redacted by the user's privacy settings. Use "
                    "drive_sheets_get_metadata to list the tab names and sizes first. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("spreadsheet_id", "str", description=_SPREADSHEET_ID_HELP),
                    ToolParam(
                        "range_a1", "str",
                        description=(
                            "Range in A1 notation including the tab name, e.g. "
                            "'Sheet1!A1:C10' or 'Sheet1!A:A'."
                        ),
                    ),
                    ToolParam(
                        "value_render_option", "str", required=False, default="FORMATTED_VALUE",
                        description=(
                            "FORMATTED_VALUE (default): displayed strings, e.g. '$1.00'. "
                            "UNFORMATTED_VALUE: the underlying value with no formatting, "
                            "e.g. 1. FORMULA: the formula text itself, e.g. '=A1+A2', "
                            "instead of its computed result -- use this to read formulas."
                        ),
                    ),
                    ToolParam(
                        "include_formatting", "bool", required=False, default=False,
                        description=(
                            "Default false: values only. True also fetches per-cell formatting (bold/italic/"
                            "text color/background color/number format/horizontal/vertical "
                            "alignment/text wrap -- the same aspects drive_sheets_format_range can set)."
                        ),
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_sheets_write_range",
                description=(
                    "Write values and/or formulas into a range of an existing "
                    "spreadsheet. A cell string starting with '=' is evaluated as "
                    "a formula, exactly as if typed into the Sheets UI — there is "
                    "no separate tool for formulas. Writing an empty row/column "
                    "clears those cells. Returns {spreadsheet_id, updated_range, "
                    "updated_cells}. Use drive_sheets_format_range to change how cells look, "
                    "not their values. Requires user approval."
                ),
                params=[
                    ToolParam("spreadsheet_id", "str", description=_SPREADSHEET_ID_HELP),
                    ToolParam(
                        "range_a1", "str",
                        description=(
                            "Range to write, in A1 notation including the tab name, e.g. "
                            "'Sheet1!A1:C10'. The values fill it from its top-left cell."
                        ),
                    ),
                    ToolParam(
                        "values", "str",
                        description=(
                            'JSON 2D array of rows, e.g. [["Name","Total"],'
                            '["Alice","=B2*2"]]. Each inner array is one row; a string starting '
                            "with '=' is written as a formula."
                        ),
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_sheets_add_sheet",
                description=(
                    "Add a new tab to an existing spreadsheet. Returns {sheet_id, title, index} "
                    "of the new tab. Use drive_sheets_create for a new spreadsheet, and "
                    "drive_sheets_get_metadata to see the tabs that exist. Requires user "
                    "approval."
                ),
                params=[
                    ToolParam("spreadsheet_id", "str", description=_SPREADSHEET_ID_HELP),
                    ToolParam("title", "str", description=(
                        "Name of the new tab. Must not be empty or already used by another tab."
                    )),
                    ToolParam("rows", "int", required=False, default=1000, description=(
                        "Number of rows the new tab starts with. Default 1000, at least 1."
                    )),
                    ToolParam("cols", "int", required=False, default=26, description=(
                        "Number of columns the new tab starts with. Default 26, at least 1."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_sheets_rename_sheet",
                description=(
                    "Rename an existing tab in a spreadsheet. There is no delete-sheet "
                    "tool — to mark a tab for removal, rename it (e.g. to "
                    "'TO BE DELETED - <original title>') and the user can delete it "
                    "by hand in the Sheets UI. Returns {spreadsheet_id, sheet_id, title} with "
                    "the new title. Get sheet_id from drive_sheets_get_metadata. Requires user "
                    "approval."
                ),
                params=[
                    ToolParam("spreadsheet_id", "str", description=_SPREADSHEET_ID_HELP),
                    ToolParam("sheet_id", "int", description=_SHEET_ID_HELP),
                    ToolParam("new_title", "str", description=(
                        "New name for the tab. Must not be empty or already used by another tab."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_sheets_format_range",
                description=(
                    "Apply formatting to a range in a spreadsheet: bold/italic, "
                    "colors, number format, horizontal/vertical alignment, text "
                    "wrap, column width, frozen rows/columns, and merged cells. "
                    "Every parameter is opt-in — its default means 'leave that "
                    "aspect unchanged', so a call that only sets a background "
                    "color never touches unrelated formatting already on the "
                    "range. Returns {spreadsheet_id, sheet_id, requests_applied}. Use "
                    "drive_sheets_write_range to change cell values. Requires user approval."
                ),
                params=[
                    ToolParam("spreadsheet_id", "str", description=_SPREADSHEET_ID_HELP),
                    ToolParam("sheet_id", "int", description=_SHEET_ID_HELP),
                    ToolParam(
                        "range_a1", "str",
                        description=(
                            "Plain A1 range scoped to sheet_id, e.g. 'A1:C10' "
                            "(no sheet-name prefix, must be fully bounded)"
                        ),
                    ),
                    ToolParam("bold", "str", required=False, default="",
                              description="'true' or 'false'; omit to leave unchanged"),
                    ToolParam("italic", "str", required=False, default="",
                              description="'true' or 'false'; omit to leave unchanged"),
                    ToolParam("background_color", "str", required=False, default="",
                              description="hex color e.g. '#ffcc00'; omit to leave unchanged"),
                    ToolParam("text_color", "str", required=False, default="",
                              description="hex color e.g. '#ffffff'; omit to leave unchanged"),
                    ToolParam("number_format", "str", required=False, default="",
                              description="Sheets number-format pattern, e.g. '0.00%', '$#,##0.00', 'yyyy-mm-dd'; omit to leave unchanged"),
                    ToolParam("horizontal_alignment", "str", required=False, default="",
                              description="'LEFT' / 'CENTER' / 'RIGHT'; omit to leave unchanged"),
                    ToolParam("vertical_alignment", "str", required=False, default="",
                              description="'TOP' / 'MIDDLE' / 'BOTTOM'; omit to leave unchanged"),
                    ToolParam("wrap_strategy", "str", required=False, default="",
                              description=(
                                  "'OVERFLOW_CELL' (overflow into empty neighboring cells) / "
                                  "'CLIP' (cut off at the cell boundary) / 'WRAP' (line break to "
                                  "fit the cell); omit to leave unchanged"
                              )),
                    ToolParam("freeze_rows", "int", required=False, default=-1,
                              description="Number of rows to freeze at the top (0 unfreezes); omit (-1) to leave unchanged"),
                    ToolParam("freeze_cols", "int", required=False, default=-1,
                              description="Number of columns to freeze at the left (0 unfreezes); omit (-1) to leave unchanged"),
                    ToolParam("column_width", "int", required=False, default=-1,
                              description="Pixel width for the range's columns; omit (-1) to leave unchanged"),
                    ToolParam("merge_type", "str", required=False, default="KEEP",
                              description="KEEP (default) / NONE (unmerge) / MERGE_ALL / MERGE_COLUMNS / MERGE_ROWS"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_sheets_insert_dimensions",
                description=(
                    "Insert blank rows or columns into a sheet tab, shifting "
                    "existing content after the insertion point. Values/"
                    "formulas are untouched, only their position shifts; "
                    "formulas referencing shifted cells are adjusted "
                    "automatically. Returns {spreadsheet_id, sheet_id, dimension, inserted}. "
                    "Use drive_sheets_delete_dimensions to remove them. Requires user "
                    "approval."
                ),
                params=[
                    ToolParam("spreadsheet_id", "str", description=_SPREADSHEET_ID_HELP),
                    ToolParam("sheet_id", "int", description=_SHEET_ID_HELP),
                    ToolParam("dimension", "str", description="What to insert: 'ROWS' or 'COLUMNS'."),
                    ToolParam("start_index", "int", description=(
                        "0-based index of the row or column to insert before (0 inserts at the "
                        "very top or left)."
                    )),
                    ToolParam("count", "int", required=False, default=1, description=(
                        "How many rows or columns to insert. Default 1."
                    )),
                    ToolParam(
                        "inherit_from_before", "bool", required=False, default=True,
                        description="Copy formatting from the row/column before the insertion point (Sheets UI default)",
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_sheets_delete_dimensions",
                description=(
                    "Delete rows or columns from a sheet tab, including any "
                    "values, formulas, and formatting they contain. This is "
                    "destructive — deleted cell content is not recoverable "
                    "through PrivacyFence. Remaining rows/columns shift to "
                    "close the gap. Returns {spreadsheet_id, sheet_id, dimension, deleted}. "
                    "Use drive_sheets_insert_dimensions to add blank ones. Requires user "
                    "approval."
                ),
                params=[
                    ToolParam("spreadsheet_id", "str", description=_SPREADSHEET_ID_HELP),
                    ToolParam("sheet_id", "int", description=_SHEET_ID_HELP),
                    ToolParam("dimension", "str", description="What to delete: 'ROWS' or 'COLUMNS'."),
                    ToolParam("start_index", "int", description=(
                        "0-based index of the first row or column to delete (row 1 or column A "
                        "is 0)."
                    )),
                    ToolParam("count", "int", required=False, default=1, description=(
                        "How many rows or columns to delete, starting at start_index. Default 1."
                    )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                destructive=True,
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "drive_list_files":
            return await self._list_files(**args)
        if tool == "drive_get_file_metadata":
            return await self._get_file_metadata(**args)
        if tool == "drive_get_file_content":
            return await self._get_file_content(**args)
        if tool == "drive_list_revisions":
            return await self._list_revisions(**args)
        if tool == "drive_get_revision_content":
            return await self._get_revision_content(**args)
        if tool == "drive_list_folder":
            return await self._list_folder(**args)
        if tool == "drive_create_blank_file":
            return await self._create_blank_file(**args)
        if tool == "drive_write_file_content":
            return await self._write_file_content(**args)
        if tool == "drive_upload_file":
            return await self._upload_file(**args)
        if tool == "drive_write_doc_content":
            return await self._write_doc_content(**args)
        if tool == "drive_move_file":
            return await self._move_file(**args)
        if tool == "drive_copy_file":
            return await self._copy_file(**args)
        if tool == "drive_add_comment":
            return await self._add_comment(**args)
        if tool == "drive_list_shared_drives":
            return await self._list_shared_drives(**args)
        if tool == "drive_download_file":
            return await self._download_file(**args)
        if tool == "drive_sheets_create":
            return await self._sheets_create(**args)
        if tool == "drive_sheets_get_metadata":
            return await self._sheets_get_metadata(**args)
        if tool == "drive_sheets_get_values":
            return await self._sheets_get_values(**args)
        if tool == "drive_sheets_write_range":
            return await self._sheets_write_range(**args)
        if tool == "drive_sheets_add_sheet":
            return await self._sheets_add_sheet(**args)
        if tool == "drive_sheets_rename_sheet":
            return await self._sheets_rename_sheet(**args)
        if tool == "drive_sheets_format_range":
            return await self._sheets_format_range(**args)
        if tool == "drive_sheets_insert_dimensions":
            return await self._sheets_insert_dimensions(**args)
        if tool == "drive_sheets_delete_dimensions":
            return await self._sheets_delete_dimensions(**args)
        if tool == "drive_docs_edit_content":
            return await self._docs_edit_content(**args)
        if tool == "drive_docs_format_content":
            return await self._docs_format_content(**args)
        raise ValueError(f"Unknown Drive tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_files(self, query: str, max_results: int = 20) -> Any:
        t0 = time.time()
        files = await self._fetch(self._drive.list_files, query, max_results)
        self._auto_audit("drive_list_files", "Search Drive Files",
                         f"List files: query={query!r}", f"{len(files)} result(s)", t0)
        # asdict(), not the raw DriveFile dataclass instances -- every other
        # connector's auto tools already do this (e.g. calendar.py, jira.py),
        # and ipc_server.py's json.dumps(..., default=str) would otherwise
        # silently collapse each DriveFile into a Python repr() string
        # instead of clean per-field JSON (confirmed: every field, including
        # size, was already reaching Claude this way -- just unusably, as
        # one opaque string per file, not structured data).
        result = [asdict(f) for f in files]
        return apply_list("drive_privacy", "file_list", result)

    async def _get_file_metadata(self, file_id: str) -> Any:
        t0 = time.time()
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        self._auto_audit("drive_get_file_metadata", "Get Drive File Info",
                         f"Get metadata: {getattr(drive_file, 'name', file_id)}",
                         ", ".join(getattr(drive_file, "owners", [])) or "(unknown)", t0)
        # file_metadata has no single "the whole record" shape to redact --
        # unlike apply_list's collection-of-records case, this is one
        # record, so block collapses it to just the id (still needed to
        # correlate the call), not an empty value.
        if category_policy("drive_privacy", "file_metadata") == "allow":
            return asdict(drive_file)
        return {"id": drive_file.id}

    async def _list_revisions(
        self, file_id: str, modified_after: str = "", max_results: int = 50
    ) -> Any:
        # Auto-approved per ADR 0168: the same class of metadata drive_get_file_metadata
        # returns without a card.
        if modified_after and not re.fullmatch(_MODIFIED_AFTER_PATTERN, modified_after):
            raise ValueError(
                "drive_list_revisions: 'modified_after' must be YYYY-MM-DD or an RFC 3339 "
                "UTC time ending in Z, e.g. 2026-10-05T00:00:00Z"
            )
        t0 = time.time()
        revisions = await self._fetch(self._drive.list_revisions, file_id, modified_after, max_results)
        self._auto_audit("drive_list_revisions", "List Drive File Versions",
                         f"List versions: {file_id}", f"{len(revisions)} version(s)", t0)
        if category_policy("drive_privacy", "file_metadata") == "allow":
            return [asdict(r) for r in revisions]
        return [{"id": r.id, "before_cutoff": r.before_cutoff} for r in revisions]

    async def _list_folder(self, folder_id: str, max_results: int = 50) -> Any:
        t0 = time.time()
        files = await self._fetch(self._drive.list_folder, folder_id, max_results)
        self._auto_audit("drive_list_folder", "List Drive Folder",
                         f"List folder: {folder_id}", f"{len(files)} item(s)", t0)
        return apply_list("drive_privacy", "folder_structure", [asdict(f) for f in files])

    async def _list_shared_drives(self, max_results: int = 50) -> Any:
        t0 = time.time()
        drives = await self._fetch(self._drive.list_shared_drives, max_results)
        self._auto_audit("drive_list_shared_drives", "List Shared Drives",
                         "List Shared Drives", f"{len(drives)} drive(s)", t0)
        return drives

    async def _create_blank_file(
        self, name: str, mime_type: str, parent_folder_id: str = ""
    ) -> Any:
        t0 = time.time()
        result = await self._fetch(self._drive.create_blank_file, name, mime_type, parent_folder_id)
        file_id = result.get("id", "") if isinstance(result, dict) else getattr(result, "id", "")
        if file_id:
            self.session_created_ids.add(file_id)
        self._auto_audit("drive_create_blank_file", "Create Drive File",
                         f"Create: {name} ({mime_type})", f"id={file_id}", t0)
        return result

    async def _sheets_create(
        self, name: str, sheet_titles: str = "", parent_folder_id: str = ""
    ) -> Any:
        t0 = time.time()
        titles = _parse_json_str_list(sheet_titles)
        result = await self._fetch(self._drive.create_spreadsheet, name, titles, parent_folder_id)
        spreadsheet_id = result.get("id", "")
        if spreadsheet_id:
            self.session_created_ids.add(spreadsheet_id)
        self._auto_audit("drive_sheets_create", "Create Spreadsheet",
                         f"Create spreadsheet: {name}", f"id={spreadsheet_id}", t0)
        return result

    async def _sheets_get_metadata(self, spreadsheet_id: str) -> Any:
        t0 = time.time()
        sheets = await self._fetch(self._drive.list_sheets, spreadsheet_id)
        self._auto_audit("drive_sheets_get_metadata", "Get Spreadsheet Metadata",
                         f"List tabs: {spreadsheet_id}", f"{len(sheets)} tab(s)", t0)
        return sheets

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_file_content(self, file_id: str) -> Any:
        content = await self._fetch(self._drive.get_file_content, file_id)
        drive_file = getattr(content, "file", None)
        name = getattr(drive_file, "name", None) or file_id
        owners = getattr(drive_file, "owners", [])
        size = getattr(drive_file, "size", "")
        modified = getattr(drive_file, "modified_time", "")
        mime_type = getattr(drive_file, "mime_type", "") or ""
        content_bytes = getattr(content, "content_bytes", b"") or b""
        # A PDF/DOCX/PPTX/XLSX comes back as its text, not as a pointer to
        # drive_download_file: in organization mode a file over about 75 KB
        # downloads only as a link, which a client whose sandbox cannot
        # reach this server cannot open, and the text is usually what the
        # agent wanted anyway. The capped fetch above cut a larger file
        # short, and pypdf cannot parse a PDF without its end, so a
        # truncated one is fetched again in full.
        extracted = ""
        extracted_truncated = False
        if content_bytes and mime_type in EXTRACTABLE_MIME_TYPES:
            extract_from = content_bytes
            if getattr(content, "truncated", False):
                extract_from = b""
                if isinstance(size, int) and 0 < size <= _EXTRACT_FULL_FETCH_MAX_BYTES:
                    full = await self._fetch(self._drive.get_file_content, file_id, size + 1)
                    if not full.truncated:
                        extract_from = full.content_bytes or b""
            if extract_from:
                extracted = await asyncio.to_thread(
                    extract_text, extract_from, mime_type, _EXTRACTED_TEXT_MAX_CHARS,
                )
        if extracted:
            raw_text = extracted
        elif content_bytes and mime_type in EXTRACTABLE_MIME_TYPES:
            raw_text = (
                f"[no text could be extracted from this {mime_type} file ({size or len(content_bytes)} "
                "bytes): a scanned PDF has no text layer, and a file over "
                f"{_EXTRACT_FULL_FETCH_MAX_BYTES:,} bytes is not read in full; drive_download_file "
                "returns the file itself]"
            )
        else:
            raw_text = getattr(content, "content_text", "") or (
                f"[binary content — {len(content_bytes)} bytes; use drive_download_file to save it]"
                if content_bytes else "(no content)"
            )
        text = apply_text("drive_privacy", "file_content", raw_text)
        if extracted:
            text, extracted_truncated = _truncate_to_json_chars(text, _EXTRACTED_TEXT_MAX_CHARS)
        # Native PDFView embed instead of the placeholder text above.
        # Gated on category_policy == "allow", the same condition
        # raw_text/text already require to
        # flow through unredacted -- a reviewer must never see a rendered
        # PDF that's richer than what the "AI will receive" checklist
        # already discloses Claude gets for this same call (see gate.py's
        # gated_call docstring). Also requires the fetch to be untruncated:
        # a partial PDF stream (get_file_content's max_bytes cap) almost
        # always fails to parse as a valid document anyway, and a page
        # silently missing its back half is worse than the plain-text
        # fallback, not better.
        pdf_bytes = b""
        if (
            content_bytes
            and not getattr(content, "truncated", False)
            and getattr(drive_file, "mime_type", "") == "application/pdf"
            and category_policy("drive_privacy", "file_content") == "allow"
        ):
            pdf_bytes = content_bytes
        file_display = apply_text("drive_privacy", "file_metadata", name)
        owner_display = apply_text("drive_privacy", "file_metadata", ", ".join(owners) if owners else "")
        size_display = apply_text("drive_privacy", "file_metadata", str(size) if size else "")
        modified_display = apply_text("drive_privacy", "file_metadata", str(modified) if modified else "")
        preview = {
            "File": file_display or "(unknown)",
            "Owner": owner_display or "(unknown)",
            "Size": size_display or "(unknown)",
            "Modified": modified_display or "(unknown)",
        }
        filtered: dict[str, Any] = {"file_id": file_id, "content": text}
        if extracted_truncated:
            filtered["truncated"] = True
        # Color sidecar -- Google-Doc-only, and only present when there's
        # something a plain ==highlight== marker can't already say on its
        # own: an exact color that isn't the tool's own default, or any
        # text color at all (Markdown has no syntax for that whatsoever).
        # Each entry's own text already appeared, unfiltered, in raw_text
        # above -- so it needs the same privacy filter applied here that
        # `text` itself got, not a free pass just because it's traveling in
        # a different field.
        for sidecar_key in ("highlights", "text_colors"):
            entries = getattr(content, sidecar_key, None) or []
            if entries:
                filtered[sidecar_key] = [
                    {"text": apply_text("drive_privacy", "file_content", entry["text"]), "hex": entry["hex"]}
                    for entry in entries
                ]
        return await gated_call(
            connector=self.name,
            tool="drive_get_file_content",
            tool_name="Read Drive File",
            summary=f"Read \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data=content,
            filtered_data=filtered,
            gate="review",
            preview=preview,
            # The whole of what the AI receives (up to get_file_content's
            # cap), not a prefix: a shorter card would let the reviewer
            # approve text they never saw, and PII past the cut would go
            # unflagged.
            details_text=text,
            pii_scan_text=text,
            visibility={
                "File metadata": category_policy("drive_privacy", "file_metadata"),
                "Document content": category_policy("drive_privacy", "file_content"),
            },
            pdf_bytes=pdf_bytes,
            pii_already_reviewed=self._pii_already_reviewed(file_id, modified),
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )

    async def _get_revision_content(self, file_id: str, revision_id: str) -> Any:
        content = await self._fetch(self._drive.get_revision_content, file_id, revision_id)
        drive_file = content.file
        name = getattr(drive_file, "name", None) or file_id
        owners = getattr(drive_file, "owners", [])
        size = getattr(drive_file, "size", "")
        modified = getattr(drive_file, "modified_time", "")
        extract_mime = content.content_mime_type or getattr(drive_file, "mime_type", "")
        extracted = ""
        if content.content_bytes and extract_mime in EXTRACTABLE_MIME_TYPES and not content.truncated:
            extracted = await asyncio.to_thread(
                extract_text, content.content_bytes, extract_mime, _EXTRACTED_TEXT_MAX_CHARS,
            )
        if extracted:
            raw_text = extracted
        elif content.content_text:
            raw_text = content.content_text
        elif content.content_bytes:
            raw_text = f"[no text could be extracted from this version ({len(content.content_bytes)} bytes)]"
        elif content.revision_size:
            raw_text = (
                f"[binary content — {content.revision_size} bytes; an earlier version of this "
                "file type cannot be read as text]"
            )
        else:
            raw_text = "(no content)"
        text = apply_text("drive_privacy", "file_content", raw_text)
        full_length = len(text)
        # A JSON length is at least the character count, so this slice is safe and
        # bounds the search below.
        text = text[:_EXTRACTED_TEXT_MAX_CHARS]
        text, cut = _truncate_to_json_chars(text, _EXTRACTED_TEXT_MAX_CHARS)
        truncated = cut or content.truncated or full_length > _EXTRACTED_TEXT_MAX_CHARS
        version_display = apply_text(
            "drive_privacy", "file_metadata", content.revision_modified_time or content.revision_id
        )
        preview = {
            "File": apply_text("drive_privacy", "file_metadata", name) or "(unknown)",
            "Version": version_display or "(unknown)",
            "Owner": apply_text("drive_privacy", "file_metadata", ", ".join(owners) if owners else "") or "(unknown)",
            "Size": apply_text("drive_privacy", "file_metadata", str(size) if size else "") or "(unknown)",
            "Modified": apply_text("drive_privacy", "file_metadata", str(modified) if modified else "") or "(unknown)",
        }
        filtered: dict[str, Any] = {
            "file_id": file_id,
            "revision_id": content.revision_id or revision_id,
            "modified_time": apply_text("drive_privacy", "file_metadata", content.revision_modified_time),
            "content": text,
        }
        if truncated:
            filtered["truncated"] = True
        return await gated_call(
            connector=self.name,
            tool="drive_get_revision_content",
            tool_name="Read Earlier Drive File Version",
            summary=f"Read version {content.revision_modified_time or revision_id} of \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            # .file is the current DriveFile, so folder and owner rules match.
            raw_data=content,
            filtered_data=filtered,
            gate="review",
            preview=preview,
            details_text=text,
            pii_scan_text=text,
            visibility={
                "File metadata": category_policy("drive_privacy", "file_metadata"),
                "Document content": category_policy("drive_privacy", "file_content"),
            },
            pdf_bytes=b"",
            # An earlier version was never PrivacyFence's own write.
            pii_already_reviewed=False,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id, "revision_id": revision_id},
        )

    async def _sheets_get_values(
        self,
        spreadsheet_id: str,
        range_a1: str,
        value_render_option: str = "FORMATTED_VALUE",
        include_formatting: bool = False,
    ) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])
        raw_values = await self._fetch(
            self._drive.get_sheet_values, spreadsheet_id, range_a1, value_render_option
        )
        formatting = (
            await self._fetch(self._drive.get_sheet_formatting, spreadsheet_id, range_a1)
            if include_formatting else None
        )
        values = apply_list("drive_privacy", "file_content", raw_values)
        # Formatting (colors/bold/number-format patterns, no free text) doesn't
        # carry the same PII risk as cell content, so it rides along outside the
        # privacy filter/PII scan rather than through apply_list.
        render_label = "" if value_render_option == "FORMATTED_VALUE" else f" ({value_render_option.lower()})"
        format_label = " + formatting" if include_formatting else ""
        # Spreadsheet/Owner are known via drive_get_file_metadata; Range is
        # Claude's own input to this very call (kept in the preview as identifying
        # context, not "new," since Claude already knows what it asked for
        # -- same reasoning as Salesforce's own-input record id). Cell
        # values is the actual new content, covered by the visibility row.
        preview = {"Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)", "Range": range_a1}
        rows_preview = _format_sheet_rows(values)
        # v2's right pane: actual cell data as a real table, not a comma-
        # joined text dump (see _sheet_values_table's own docstring).
        # table_only since details_text (kept for legacy/PII-scan) would
        # otherwise show the exact same values twice.
        table = _sheet_values_table(values)
        filtered_data: Any = {"values": values, "formatting": formatting} if include_formatting else values
        return await gated_call(
            connector=self.name,
            tool="drive_sheets_get_values",
            tool_name="Read Sheet Values",
            summary=f"Read {range_a1} from \"{name}\"{render_label}{format_label}",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "values": raw_values, "formatting": formatting},
            filtered_data=filtered_data,
            gate="review",
            preview=preview,
            details_text=rows_preview,
            pii_scan_text=rows_preview,
            visibility={"Cell values": category_policy("drive_privacy", "file_content")},
            preview_tables=[table] if values else [],
            table_only=True,
            pii_already_reviewed=self._pii_already_reviewed(
                spreadsheet_id, getattr(drive_file, "modified_time", "")
            ),
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={
                "spreadsheet_id": spreadsheet_id,
                "range_a1": range_a1,
                "value_render_option": value_render_option,
                "include_formatting": include_formatting,
            },
        )

    async def _download_file(self, file_id: str) -> Any:
        import os
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        owners = getattr(drive_file, "owners", [])
        modified = getattr(drive_file, "modified_time", "")
        name = os.path.basename(resolve_download_name(drive_file)) or drive_file.id or "file"

        cfg = self.download_config or DownloadDeliveryConfig()
        # Audit trail: the delivery path this call is about to take, estimated from metadata
        # size the same way the preview below is (ADR 0160).
        delivery = (
            "inline_base64" if cfg.fits_inline(drive_file.size, name, drive_file.mime_type)
            else "staged_link"
        )

        # File/Owner/Size/Modified are known via drive_get_file_metadata (or
        # this call's own fetch of it above) -- the only genuinely new fact
        # from approving this call is *how* the file reaches Claude, which
        # is mode/delivery-conditional (ADR 0017): local mode's bytes never leave
        # this machine; org mode's own preview must say plainly whether
        # bytes are about to flow into Claude's context or stay
        # server-side behind a one-time link, using drive_file.size (the
        # same field the "Size" row above already shows) as the size
        # estimate -- the real decision below is made against the actually
        # fetched byte count, which can differ slightly for a Google
        # Workspace export.
        preview = {
            "File": name,
            "Owner": ", ".join(owners) if owners else "(unknown)",
            "Size": f"{drive_file.size:,} bytes",
            "Modified": str(modified) if modified else "(unknown)",
        }
        if cfg.fits_inline(drive_file.size, name, drive_file.mime_type):
            new_info = {
                "Content returned to {agent}": (
                    f"Yes — file bytes are included in the tool result (file is "
                    f"{drive_file.size:,} bytes, {base64_length(drive_file.size):,} once "
                    f"base64-encoded, within this org's {cfg.inline_max_bytes:,}-byte "
                    "inline-delivery limit)"
                ),
            }
        else:
            new_info = {
                "Content returned to {agent}": (
                    "None — a one-time link is generated for you to open in your own browser"
                ),
            }
        details = "The file above will be delivered as described above."

        # Drive already generates a small preview image for many file types
        # (Docs/Sheets/Slides, images, PDFs) -- cheaper to fetch that than the
        # full file just to show the human something. Not every file has one
        # (thumbnail_link is empty when Drive hasn't generated one), and this
        # is best-effort: a fetch failure just falls back to today's
        # metadata-only preview rather than blocking the download.
        preview_bytes = b""
        preview_mime_type = ""
        if drive_file.thumbnail_link:
            try:
                thumbnail = await self._fetch(self._drive.fetch_thumbnail, drive_file.thumbnail_link)
                preview_bytes = thumbnail["data"]
                preview_mime_type = thumbnail["mime_type"]
            except RuntimeError:
                # _fetch() already turned the underlying DriveClientError into
                # a RuntimeError and logged it -- this is a best-effort
                # preview, not the actual download.
                pass

        # Best-effort extraction: reuses the same capped fetch
        # drive_get_file_content already does (100KB default) purely to get
        # something to scan/preview -- gmail_download_attachment's
        # equivalent is below in gmail.py. A fetch/parse failure just means
        # nothing gets scanned or previewed, same as today, rather than
        # blocking the download. Feeds both the PII scan and (via
        # preview_blocks_for below) a rich preview of the file's own
        # content -- since no file content ever reaches Claude for this
        # tool, showing the human the real content here is strictly more
        # useful than a visual-only thumbnail ever was.
        # A >100KB file would hit get_file_content()'s default 100KB
        # prefetch cap, so extract_text() would run on a truncated prefix --
        # fine for text, but pypdf needs a PDF's trailer, at the *end* of
        # the file, so a truncated prefix throws instead of returning
        # anything usable ("EOF marker not found"). Below a sane size, fetch
        # the whole file instead of guessing at a cap; above it, skip extract_text() on a
        # truncated result rather than feed it something it can't parse.
        pii_scan_text = ""
        try:
            if drive_file.size and drive_file.size <= _FULL_FETCH_PII_SCAN_MAX_BYTES:
                content = await self._fetch(self._drive.get_file_content, file_id, drive_file.size + 1)
            else:
                content = await self._fetch(self._drive.get_file_content, file_id)
            if content.content_text:
                pii_scan_text = content.content_text
            elif content.content_bytes:
                if content.truncated:
                    logger.debug(
                        "drive_download_file %s: %d-byte file exceeds the %d-byte full-fetch PII "
                        "scan cap and get_file_content truncated it -- skipping extract_text() "
                        "rather than feeding it a prefix some formats (e.g. PDF) can't parse.",
                        file_id, drive_file.size, _FULL_FETCH_PII_SCAN_MAX_BYTES,
                    )
                else:
                    pii_scan_text = await asyncio.to_thread(extract_text, content.content_bytes, drive_file.mime_type)
        except RuntimeError:
            pass

        # Gate before any delivery: gated_call raises on denial, and only a decision made here
        # should ever cause the file to be handed out. Mirrors gmail.py's _download_attachment.
        await gated_call(
            connector=self.name,
            tool="drive_download_file",
            tool_name="Download Drive File",
            summary=f"Download \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data=drive_file,
            filtered_data=None,
            gate="review",
            preview=preview,
            new_info=new_info,
            details_text=details,
            pii_scan_text=pii_scan_text,
            preview_bytes=preview_bytes,
            preview_mime_type=preview_mime_type,
            preview_blocks=preview_blocks_for(details, pii_scan_text),
            pii_already_reviewed=self._pii_already_reviewed(file_id, modified),
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
            delivery=delivery,
            content_reaches_result=(delivery == "inline_base64"),
        )
        # The estimate the gate was told about is binding (ADR 0157): a file approved as "not
        # inline" is never returned inline because its real size turned out smaller.
        return await self._deliver_org_download(
            file_id, drive_file.size, cfg, allow_inline=(delivery == "inline_base64"),
        )

    async def _deliver_org_download(
        self, file_id: str, metadata_size: int, cfg: DownloadDeliveryConfig, allow_inline: bool,
    ) -> Any:
        """org mode's own delivery, once approval is granted -- inline
        (base64, in the tool result) for anything under ``cfg.
        inline_max_bytes`` (the whole tool result, base64 included -- ADR
        0092), else a one-time staged link, else (staging disabled for this
        org) a clear refusal. ``allow_inline`` false (the gate was told the content does not reach
        the result) skips the inline branch. Never writes to this daemon's own disk except
        via download_staging.py's own encrypted-at-rest store."""
        if not cfg.allow_disk_staging and not (allow_inline and cfg.fits_inline(metadata_size)):
            # Bail before ever fetching bytes -- metadata_size (already
            # known, no extra round trip) already rules out inline, and
            # staging is off for this org, so there's nothing productive a
            # full fetch would accomplish.
            raise RuntimeError(cfg.over_inline_limit_message("This file", metadata_size))

        result = await self._fetch(self._drive.download_file_bytes, file_id)
        data: bytes = result["data"]
        name: str = result["name"]
        mime_type: str = result["mime_type"]
        size_bytes: int = result["size_bytes"]

        if allow_inline:
            inline = cfg.inline_result(name, mime_type, data)
            if inline is not None:
                return inline

        if not cfg.allow_disk_staging:
            raise RuntimeError(cfg.over_inline_limit_message(f"\"{name}\"", size_bytes))

        token = await asyncio.to_thread(
            get_download_staging_store().stage, current_principal(), data, name, mime_type,
            ttl_seconds=cfg.link_ttl_seconds,
        )
        local_files.mark_staged_download()
        return cfg.link_result(name, size_bytes, f"{self.download_base_url}{cfg.staged_link_path(token)}")

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _write_doc_content(self, file_id: str, markdown: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        preview = {"File": name, "Owner": ", ".join(owners) or "(unknown)"}
        await gated_call(
            connector=self.name,
            tool="drive_write_doc_content",
            tool_name="Write Google Doc",
            summary=f"Write rich content to \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "markdown_preview": markdown[:200]},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=markdown,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )
        result = await self._fetch(self._drive.write_doc_rich_content, file_id, markdown)
        await self._note_own_write(file_id)
        return result

    async def _docs_edit_content(
        self, file_id: str, find_text: str, replace_markdown: str, replace_all: bool = False
    ) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        preview = {
            "File": name, "Owner": ", ".join(owners) or "(unknown)",
            "Match": "every occurrence" if replace_all else "the one matching occurrence",
        }
        details = f"Find:\n{find_text}\n\nReplace with:\n{replace_markdown}"
        await gated_call(
            connector=self.name,
            tool="drive_docs_edit_content",
            tool_name="Edit Google Doc",
            summary=f"Replace text in \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "find_text_preview": find_text[:200]},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )
        result = await self._fetch(
            self._drive.edit_doc_content, file_id, find_text, replace_markdown, replace_all
        )
        await self._note_own_write(file_id)
        return result

    async def _docs_format_content(
        self,
        file_id: str,
        find_text: str,
        bold: str = "",
        italic: str = "",
        highlight_color: str = "",
        text_color: str = "",
        replace_all: bool = False,
    ) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])

        applied = []
        if bold:
            applied.append(f"bold={bold}")
        if italic:
            applied.append(f"italic={italic}")
        if highlight_color:
            applied.append(f"highlight={highlight_color}")
        if text_color:
            applied.append(f"text_color={text_color}")
        summary_detail = ", ".join(applied) or "(no changes)"

        preview = {
            "File": name, "Owner": ", ".join(owners) or "(unknown)",
            "Format": summary_detail,
        }
        details = f"Applying the formatting above to:\n{find_text}"
        await gated_call(
            connector=self.name,
            tool="drive_docs_format_content",
            tool_name="Format Google Doc Text",
            summary=f"Format text in \"{name}\": {summary_detail}",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "find_text_preview": find_text[:200], "format": summary_detail},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )
        result = await self._fetch(
            self._drive.format_doc_content, file_id, find_text, bold, italic, highlight_color, text_color, replace_all
        )
        await self._note_own_write(file_id)
        return result

    async def _upload_file(
        self,
        name: str = "",
        parent_folder_id: str = "",
        content_base64: str = "",
        upload_id: str = "",
    ) -> Any:
        import base64

        provided = (bool(content_base64.strip()), bool(upload_id.strip()))
        if sum(provided) != 1:
            raise ValueError("drive_upload_file: provide exactly one of content_base64 or upload_id")

        preview_bytes = b""
        preview_mime_type = ""
        upload_pii_scan_text = ""
        local_data: bytes | None = None

        # Capability uploads (ADR 0304): upload_id names bytes already staged by
        # privacyfence_create_upload_slot, via the local_files.py ``upload:`` convention.
        upload_ref = f"{local_files.UPLOAD_REF_PREFIX}{upload_id.strip()}" if upload_id.strip() else ""

        if upload_ref:
            # Raises immediately -- LocalFileAccessError if the slot is unknown, expired or
            # already used -- rather than failing only once the human has already approved.
            local_files.require_local_files([upload_ref], max_total_bytes=_UPLOAD_MAX_BYTES)
            display_name = name.strip() or "(unnamed file)"
            size_bytes = local_files.local_file_size(upload_ref)
            source = "uploaded via privacyfence_create_upload_slot"
            sender = "(uploaded file)"
            # Only types is_prefetch_worthy() recognizes are read for the preview and the PII
            # scan, under a sane cap.
            guessed_mime = guess_mime_type(display_name)
            if (
                guessed_mime and is_prefetch_worthy(guessed_mime)
                and 0 < size_bytes <= _UPLOAD_PREVIEW_MAX_BYTES
            ):
                try:
                    read_bytes = local_files.read_local_file(upload_ref)
                except local_files.LocalFileAccessError:
                    logger.warning(
                        "drive_upload_file: failed to read %r for preview/PII scan",
                        upload_ref, exc_info=True,
                    )
                else:
                    local_data = read_bytes
                    if guessed_mime.startswith("image/"):
                        preview_bytes = read_bytes
                        preview_mime_type = guessed_mime
                    upload_pii_scan_text = await asyncio.to_thread(extract_text, read_bytes, guessed_mime)
        else:
            display_name = name.strip() or "(unnamed file)"
            try:
                decoded = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError):
                decoded = b""
            size_bytes = len(decoded)
            source = "inline content"
            sender = "(inline content)"
            # Already fully decoded in memory above (needed just to measure size) -- no extra
            # cost to reuse it, and content_base64 arrives as an MCP tool argument, already
            # bounded by the wire-protocol size limit.
            guessed_mime = guess_mime_type(display_name) if name.strip() else None
            if guessed_mime and decoded:
                if guessed_mime.startswith("image/"):
                    preview_bytes = decoded
                    preview_mime_type = guessed_mime
                upload_pii_scan_text = await asyncio.to_thread(extract_text, decoded, guessed_mime)

        preview = {
            "File": display_name,
            "Source": source,
            "Size": f"{size_bytes:,} bytes",
            "Folder": parent_folder_id or "My Drive (root)",
        }
        details = "The file above will be uploaded to the destination shown."
        await gated_call(
            connector=self.name,
            tool="drive_upload_file",
            tool_name="Upload File to Drive",
            summary=f"Upload \"{display_name}\" to Drive",
            sender=sender,
            raw_data={"upload_id": upload_id, "name": display_name, "size_bytes": size_bytes},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details,
            preview_bytes=preview_bytes,
            preview_mime_type=preview_mime_type,
            preview_blocks=preview_blocks_for(details, upload_pii_scan_text),
            upload_pii_scan_text=upload_pii_scan_text,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={
                "upload_id": upload_id,
                "name": name,
                "parent_folder_id": parent_folder_id,
                "content_base64": content_base64,
            },
        )
        # ADR 0102: the upload slot is consumed only now, once the gate has passed.
        local_files.commit_uploads()
        if upload_ref:
            if local_data is None:
                local_data = local_files.read_local_file(upload_ref)
            result = await self._fetch(self._drive.upload_file_bytes, local_data, display_name, parent_folder_id)
        else:
            result = await self._fetch(
                self._drive.upload_file, name, parent_folder_id, content_base64
            )
        file_id = result.get("id", "")
        if file_id:
            self.session_created_ids.add(file_id)
            await self._note_own_write(file_id)
        return result

    async def _write_file_content(self, file_id: str, content: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        preview = {"File": name, "Owner": ", ".join(owners) or "(unknown)"}
        await gated_call(
            connector=self.name,
            tool="drive_write_file_content",
            tool_name="Write Drive File",
            summary=f"Write to \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "content_preview": content[:200]},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=content,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )
        result = await self._fetch(self._drive.write_file_content, file_id, content)
        await self._note_own_write(file_id, result.get("modified_time", ""))
        return result

    async def _folder_names(self, drive_file: Any, destination_folder_id: str) -> tuple[str, str]:
        """``(current, destination)`` folder names for a move or copy preview.

        Best-effort on both lookups: some folders (e.g. a Shared Drive root) aren't fetchable as a
        regular file, and a file with no recorded parent shows "(unknown)" rather than blocking the
        popup on a name lookup that can't succeed either way."""
        current_parent_id = (getattr(drive_file, "parent_ids", None) or [None])[0]
        if current_parent_id:
            try:
                current_folder = await self._fetch(self._drive.get_file_metadata, current_parent_id)
                current_name = getattr(current_folder, "name", "") or current_parent_id
            except RuntimeError:
                current_name = current_parent_id
        else:
            current_name = "(unknown)"
        try:
            destination_folder = await self._fetch(self._drive.get_file_metadata, destination_folder_id)
            destination_name = getattr(destination_folder, "name", "") or destination_folder_id
        except RuntimeError:
            destination_name = destination_folder_id
        return current_name, destination_name

    async def _move_file(self, file_id: str, destination_folder_id: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        # "Folder": old → new -- a move always changes the folder (that's
        # the whole point of the call), so this is always a diff, not
        # conditional on whether anything changed the way Event/Start/etc.
        # are elsewhere.
        current_name, destination_name = await self._folder_names(drive_file, destination_folder_id)
        preview = {
            "File": name, "Owner": ", ".join(owners) or "(unknown)",
            "Folder": f"{current_name} → {destination_name}",
        }
        await gated_call(
            connector=self.name,
            tool="drive_move_file",
            tool_name="Move Drive File",
            summary=f"Move \"{name}\" to new folder",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "destination_folder_id": destination_folder_id},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="File will be moved to the new folder; its content is unchanged.",
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id, "destination_folder_id": destination_folder_id},
        )
        return await self._fetch(self._drive.move_file, file_id, destination_folder_id)

    async def _copy_file(self, file_id: str, destination_folder_id: str, name: str = "") -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        if getattr(drive_file, "mime_type", "") == "application/vnd.google-apps.folder":
            raise ValueError("drive_copy_file copies files, not folders: Google Drive cannot copy a folder.")
        source_name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        copy_name = name or source_name
        current_name, destination_name = await self._folder_names(drive_file, destination_folder_id)
        preview = {
            "File": source_name, "Owner": ", ".join(owners) or "(unknown)",
            "Folder": f"{current_name} → {destination_name}",
            "Copy name": copy_name,
        }
        await gated_call(
            connector=self.name,
            tool="drive_copy_file",
            tool_name="Copy Drive File",
            summary=f"Copy \"{source_name}\" to another folder",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "destination_folder_id": destination_folder_id},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=(
                "A copy of the file will be created in the destination folder and you will own it; "
                "the original is unchanged."
            ),
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id, "destination_folder_id": destination_folder_id},
        )
        # Not recorded as an own write: PrivacyFence did not write the copy's content and no popup
        # showed it, so reading it later must still get the personal-data check.
        return await self._fetch(self._drive.copy_file, file_id, destination_folder_id, name)

    async def _add_comment(self, file_id: str, comment: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        preview = {"File": name, "Owner": ", ".join(owners) or "(unknown)"}
        await gated_call(
            connector=self.name,
            tool="drive_add_comment",
            tool_name="Add Drive Comment",
            summary=f"Comment on \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "comment": comment},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=comment,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )
        return await self._fetch(self._drive.add_comment, file_id, comment)

    async def _sheets_write_range(self, spreadsheet_id: str, range_a1: str, values: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])
        parsed_values = _parse_json_2d_list(values)
        if parsed_values is None:
            raise ValueError(
                "drive_sheets_write_range: 'values' must be a JSON 2D array, e.g. [[\"a\",\"b\"]]"
            )
        preview = {"Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)", "Range": range_a1}
        # v2's right pane: the actual values being written as a real table,
        # not a comma-joined text dump -- same treatment and same
        # reasoning as _sheets_get_values's own table (see
        # _sheet_values_table's docstring). table_only since details_text
        # (kept for legacy/PII-scan) would otherwise show the exact same
        # values twice.
        table = _sheet_values_table(parsed_values)
        await gated_call(
            connector=self.name,
            tool="drive_sheets_write_range",
            tool_name="Write Sheet Range",
            summary=f"Write {range_a1} in \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "values_preview": values[:200]},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=_format_sheet_rows(parsed_values),
            preview_tables=[table] if parsed_values else [],
            table_only=True,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"spreadsheet_id": spreadsheet_id, "range_a1": range_a1},
        )
        result = await self._fetch(self._drive.write_sheet_values, spreadsheet_id, range_a1, parsed_values)
        await self._note_own_write(spreadsheet_id)
        return result

    async def _sheets_add_sheet(
        self, spreadsheet_id: str, title: str, rows: int = 1000, cols: int = 26
    ) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])
        preview = {
            "Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)",
            "New tab": title, "Size": f"{rows} rows x {cols} cols",
        }
        await gated_call(
            connector=self.name,
            tool="drive_sheets_add_sheet",
            tool_name="Add Sheet Tab",
            summary=f"Add tab \"{title}\" to \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "title": title},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="A new tab will be added with the settings shown above.",
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"spreadsheet_id": spreadsheet_id, "title": title, "rows": rows, "cols": cols},
        )
        result = await self._fetch(self._drive.add_sheet, spreadsheet_id, title, rows, cols)
        await self._note_own_write(spreadsheet_id)
        return result

    async def _sheet_title_for(self, spreadsheet_id: str, sheet_id: int) -> str:
        """Best-effort tab title for `sheet_id` (the numeric id every
        sheets_* write tool takes, not a human-readable name on its own)
        -- falls back to the raw id as a string if the spreadsheet's own
        tab list can't be fetched, or none of its tabs match this id."""
        try:
            sheets = await self._fetch(self._drive.list_sheets, spreadsheet_id)
        except RuntimeError:
            return str(sheet_id)
        for sheet in sheets:
            if sheet.get("sheet_id") == sheet_id:
                return sheet.get("title") or str(sheet_id)
        return str(sheet_id)

    async def _sheets_rename_sheet(self, spreadsheet_id: str, sheet_id: int, new_title: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])
        current_title = await self._sheet_title_for(spreadsheet_id, sheet_id)
        preview = {
            "Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)",
            "Tab title": f"{current_title} → {new_title}",
        }
        await gated_call(
            connector=self.name,
            tool="drive_sheets_rename_sheet",
            tool_name="Rename Sheet Tab",
            summary=f"Rename tab {sheet_id} in \"{name}\" to \"{new_title}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "sheet_id": sheet_id, "new_title": new_title},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="The tab above will be renamed; its contents are unchanged.",
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "new_title": new_title},
        )
        result = await self._fetch(self._drive.rename_sheet, spreadsheet_id, sheet_id, new_title)
        await self._note_own_write(spreadsheet_id)
        return result

    async def _sheets_format_range(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        range_a1: str,
        bold: str = "",
        italic: str = "",
        background_color: str = "",
        text_color: str = "",
        number_format: str = "",
        horizontal_alignment: str = "",
        vertical_alignment: str = "",
        wrap_strategy: str = "",
        freeze_rows: int = -1,
        freeze_cols: int = -1,
        column_width: int = -1,
        merge_type: str = "KEEP",
    ) -> Any:
        # Validate the range syntax before gating, not after: format_sheet_range()
        # only discovers bad A1 syntax once it's already past the approval
        # popup, so a doomed call still cost the user an unnecessary approval
        # decision. Same parser format_sheet_range() itself uses, just run early.
        try:
            _parse_a1_range(range_a1)
        except DriveClientError as exc:
            raise RuntimeError(str(exc)) from exc
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])

        applied = []
        if bold:
            applied.append(f"bold={bold}")
        if italic:
            applied.append(f"italic={italic}")
        if background_color:
            applied.append(f"background={background_color}")
        if text_color:
            applied.append(f"text_color={text_color}")
        if number_format:
            applied.append(f"number_format={number_format}")
        if horizontal_alignment:
            applied.append(f"align={horizontal_alignment}")
        if vertical_alignment:
            applied.append(f"valign={vertical_alignment}")
        if wrap_strategy:
            applied.append(f"wrap={wrap_strategy}")
        if freeze_rows >= 0:
            applied.append(f"freeze_rows={freeze_rows}")
        if freeze_cols >= 0:
            applied.append(f"freeze_cols={freeze_cols}")
        if column_width >= 0:
            applied.append(f"column_width={column_width}px")
        if merge_type != "KEEP":
            applied.append(f"merge={merge_type}")
        summary_detail = ", ".join(applied) or "(no changes)"

        preview = {
            "Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)",
            "Range": range_a1, "Format": summary_detail,
        }
        await gated_call(
            connector=self.name,
            tool="drive_sheets_format_range",
            tool_name="Format Sheet Range",
            summary=f"Format {range_a1} in \"{name}\": {summary_detail}",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "range_a1": range_a1, "format": summary_detail},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="The formatting above will be applied to the range; other formatting is unchanged.",
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "range_a1": range_a1},
        )
        result = await self._fetch(
            self._drive.format_sheet_range, spreadsheet_id, sheet_id, range_a1,
            bold, italic, background_color, text_color, number_format,
            horizontal_alignment, vertical_alignment, wrap_strategy,
            freeze_rows, freeze_cols, column_width, merge_type,
        )
        await self._note_own_write(spreadsheet_id)
        return result

    async def _sheets_insert_dimensions(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        dimension: str,
        start_index: int,
        count: int = 1,
        inherit_from_before: bool = True,
    ) -> Any:
        # Validate before gating, not after -- same reasoning as
        # _sheets_format_range's early range_a1 check: a doomed call
        # shouldn't cost the user an unnecessary approval decision.
        dimension = dimension.strip().upper()
        if dimension not in ("ROWS", "COLUMNS"):
            raise ValueError(
                f"drive_sheets_insert_dimensions: dimension must be 'ROWS' or 'COLUMNS', got {dimension!r}"
            )
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])
        tab_title = await self._sheet_title_for(spreadsheet_id, sheet_id)
        preview = {
            "Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)",
            "Tab": tab_title, "Action": f"Insert {count} {dimension} before index {start_index}",
        }
        await gated_call(
            connector=self.name,
            tool="drive_sheets_insert_dimensions",
            tool_name="Insert Sheet Rows/Columns",
            summary=f"Insert {count} {dimension} in \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "dimension": dimension, "start_index": start_index, "count": count},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=(
                "Existing rows/columns from the insertion point shift accordingly; "
                "other cells are unchanged."
            ),
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={
                "spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id,
                "dimension": dimension, "start_index": start_index, "count": count,
            },
        )
        result = await self._fetch(
            self._drive.insert_dimensions, spreadsheet_id, sheet_id, dimension,
            start_index, count, inherit_from_before,
        )
        await self._note_own_write(spreadsheet_id)
        return result

    async def _sheets_delete_dimensions(
        self, spreadsheet_id: str, sheet_id: int, dimension: str, start_index: int, count: int = 1
    ) -> Any:
        dimension = dimension.strip().upper()
        if dimension not in ("ROWS", "COLUMNS"):
            raise ValueError(
                f"drive_sheets_delete_dimensions: dimension must be 'ROWS' or 'COLUMNS', got {dimension!r}"
            )
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])
        tab_title = await self._sheet_title_for(spreadsheet_id, sheet_id)
        preview = {
            "Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)",
            "Tab": tab_title, "Action": f"Delete {count} {dimension} starting at index {start_index}",
        }
        await gated_call(
            connector=self.name,
            tool="drive_sheets_delete_dimensions",
            tool_name="Delete Sheet Rows/Columns",
            summary=f"Delete {count} {dimension} in \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "dimension": dimension, "start_index": start_index, "count": count},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=(
                "Rows/columns and any values, formulas, or formatting they contain will be "
                "removed — not recoverable through PrivacyFence. Remaining rows/columns shift "
                "to close the gap."
            ),
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={
                "spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id,
                "dimension": dimension, "start_index": start_index, "count": count,
            },
        )
        result = await self._fetch(
            self._drive.delete_dimensions, spreadsheet_id, sheet_id, dimension, start_index, count,
        )
        await self._note_own_write(spreadsheet_id)
        return result

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except DriveClientError as exc:
            logger.error("Drive fetch failed: %s", exc)
            unavailable = unavailable_error("drive", exc, self.my_email)
            if unavailable is not None:
                raise unavailable from exc
            raise RuntimeError(str(exc)) from exc

    async def _note_own_write(self, file_id: str, modified_time: str = "") -> None:
        """Record that PrivacyFence itself just wrote ``file_id``'s current
        content, keyed by the file's resulting Drive ``modifiedTime`` -- see
        ``own_write_revisions``'s own docstring in ``__init__``.

        ``modified_time`` lets a caller that already has it (``write_
        file_content``'s own response carries ``modifiedTime``) skip an
        extra round trip; every other write tool goes through the Docs/
        Sheets APIs, whose responses don't carry Drive-level metadata, so
        this re-fetches it with one cheap ``get_file_metadata`` call.
        Best-effort and non-raising: the write itself already succeeded by
        the time this runs, so a failed metadata re-fetch here only costs
        the next read of this file the PII-gate shortcut, not the write.
        """
        if not modified_time:
            try:
                drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
            except RuntimeError:
                logger.warning("_note_own_write: metadata re-fetch failed for %s", file_id)
                return
            modified_time = getattr(drive_file, "modified_time", "") or ""
        if modified_time:
            self.own_write_revisions[file_id] = modified_time

    def _pii_already_reviewed(self, file_id: str, modified_time: str) -> bool:
        """True when ``file_id``'s current ``modifiedTime`` still matches the
        value recorded right after PrivacyFence's own last write to it --
        i.e. nothing (a human collaborator, another app, a different Claude
        session) has touched this exact content since. Passed straight
        through to ``gated_call``'s ``pii_already_reviewed`` -- see that
        parameter's docstring for what it does and doesn't suppress.
        """
        return bool(modified_time) and self.own_write_revisions.get(file_id) == modified_time

    def _auto_audit(
        self, tool: str, tool_name: str, summary: str, sender: str, created_at: float
    ) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool_name,
                summary=summary,
                sender=sender,
                decision="auto_accepted",
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
