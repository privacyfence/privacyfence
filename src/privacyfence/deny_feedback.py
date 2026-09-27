"""What a human may tell the agent when they deny an approval (ADR 0082).

A denial can carry two optional parts, both authored only by the person deciding the card: an
``intent`` from the fixed vocabulary in ``INTENTS`` (picked from chips, so the agent gets a
structured instruction even when nothing is typed), and a free-text ``note``. This module is the
one place that validates them (``parse``), cleans the note (``sanitize_note``) and turns them into
what the agent receives, on the synchronous path (``denial_message``, the text of
``gate.GateDeniedError.by_user``) and on the ``privacyfence_await_approval`` path
(``await_entry``, one value under that tool's reserved ``denial_feedback`` key).

The note is the deciding human's own words and nothing else. It is never content-filtered, and
never written to the audit log, a log line, the approvals SSE stream or a web push (ADR 0083): only
its length and the intent are audited. What keeps it from posing as PrivacyFence's own text is
structure, not screening -- a fixed label in front of it, and JSON quoting, so it can never close
its own string.

Pure functions only: no I/O, no logging.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# value -> (chip label, guidance text the agent receives). The order is the chips' order.
INTENTS: dict[str, tuple[str, str]] = {
    "stop": (
        "Stop — don't retry",
        "The user does not want this done. Do not retry this or a similar call; ask the user "
        "before doing anything further toward it.",
    ),
    "wrong_target": (
        "Wrong target",
        "The user says the target is wrong (recipient, file, folder, record or account). Correct "
        "the target, then ask again.",
    ),
    "rewrite": (
        "Change the content",
        "The user wants the content changed. Revise it, then ask again.",
    ),
    "different_approach": (
        "Try another way",
        "The user wants a different tool or approach for this, not a retry of this call.",
    ),
}

# Counted after sanitize_note(). A longer note is rejected, never truncated: silently cutting what
# someone wrote to an agent is worse than refusing, and the textarea's maxlength stops a real user
# before this does.
MAX_NOTE_CHARS = 500

# privacyfence_await_approval's one reserved top-level key. Approval ids are uuid4().hex, so no id
# can ever equal it.
DENIAL_FEEDBACK_KEY = "denial_feedback"

# Every denial message starts with exactly this -- client-side matching relies on it.
DENIAL_PREFIX = "Request denied by user."

_DEFAULT_INSTRUCTION = "Don't retry the same call; ask the user how to proceed."

_NOTE_LABEL = "User's note (written by the person who denied this request; JSON string):"

_EXCESS_NEWLINES = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class DenialFeedback:
    """One denial's feedback. Both fields "" when none was given -- the plain Deny."""

    intent: str = ""
    note: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.intent and not self.note


def _is_stripped_char(ch: str) -> bool:
    # Cc: control characters (the newline is kept by the caller). Cf: format characters -- bidi
    # overrides and isolates, zero-width spaces and joiners. Zl/Zp: line and paragraph separators.
    return ch != "\n" and unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp")


def sanitize_note(value: str) -> str:
    """NFC, CR/CRLF to LF, every Cc/Cf/Zl/Zp character except LF dropped, runs of more than two
    newlines collapsed to two, surrounding whitespace stripped. Modelled on
    ``agent_identity.sanitize_client_string``, but keeps line breaks and never truncates."""
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if not _is_stripped_char(ch))
    text = _EXCESS_NEWLINES.sub("\n\n", text)
    return text.strip()


def parse(payload: dict[str, Any]) -> DenialFeedback:
    """Read ``intent`` and ``note`` from a decide request's JSON body. A missing or null key means
    none. Raises ``ValueError`` with a static message (safe to return to the browser) for an
    unknown intent, a non-string note, or a note longer than ``MAX_NOTE_CHARS`` after
    sanitization."""
    raw_intent = payload.get("intent")
    if raw_intent is None or raw_intent == "":
        intent = ""
    elif isinstance(raw_intent, str) and raw_intent in INTENTS:
        intent = raw_intent
    else:
        raise ValueError("unknown intent")

    raw_note = payload.get("note")
    if raw_note is None:
        note = ""
    elif isinstance(raw_note, str):
        note = sanitize_note(raw_note)
    else:
        raise ValueError("note must be a string")
    if len(note) > MAX_NOTE_CHARS:
        raise ValueError(f"note is longer than {MAX_NOTE_CHARS} characters")
    return DenialFeedback(intent=intent, note=note)


def denial_message(feedback: DenialFeedback) -> str:
    """The text a human denial reaches the agent with. Always starts with ``DENIAL_PREFIX``. The
    note, when there is one, comes last, behind a fixed label and JSON-quoted, so nothing in it can
    end its string early or read as PrivacyFence's own text."""
    parts = [DENIAL_PREFIX]
    if feedback.intent:
        label, guidance = INTENTS[feedback.intent]
        parts.append(f'The user chose "{label}": {guidance[0].lower()}{guidance[1:]}')
    elif not feedback.note:
        parts.append(_DEFAULT_INSTRUCTION)
    if feedback.note:
        parts.append(f"{_NOTE_LABEL} {json.dumps(feedback.note, ensure_ascii=False)}")
    return " ".join(parts)


def await_entry(feedback: DenialFeedback) -> dict[str, str | None]:
    """One approval's value under ``privacyfence_await_approval``'s reserved ``denial_feedback``
    key. ``intent``/``note``/``guidance`` are ``None`` when absent."""
    return {
        "intent": feedback.intent or None,
        "note": feedback.note or None,
        "guidance": INTENTS[feedback.intent][1] if feedback.intent else None,
    }
