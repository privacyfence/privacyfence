"""Export and import of a person's own settings: their always-allow rows and step-up level.

The file carries personal settings only (no install-wide policy, credentials, passkeys, tokens or
audit log), and import is merge-only: it adds or extends rows and widens the step-up scope, and never
removes or narrows anything. The module is pure: it reads and writes no files itself. The caller
supplies the current rows and the writer (``merge_rules``), and ``apply_import`` is the only function
that has a side effect, through the callable it is given and ``step_up_config.set_personal_scope``.

Rows come from outside, so ``parse_document`` bounds their size and shape, and ``plan_import``
decides each row by simulating the real merge (``scope_rows.add_rules``) rather than trusting an id
or a verb list in the file. A custom row (no catalogue scope) is never imported: it carries its own
predicate and operations, which a preview sentence cannot describe faithfully.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Iterable

from . import step_up_config
from .policy import describe, propose, scope_rows, store
from .policy.engine import PolicyRule
from .policy.scope_rows import ScopeRow
from .step_up_config import STEP_UP_SCOPES, StepUpScope

if TYPE_CHECKING:
    from .principal import Principal

FORMAT = "privacyfence-personal-settings"
FORMAT_VERSION = 1
FILE_NAME = "privacyfence-personal-settings.json"
MAX_IMPORT_BYTES = 262_144
MAX_IMPORT_ROWS = 500

_MAX_VALUE_CHARS = 512
_MAX_VALUE_ITEMS = 200
_MAX_EXPORTED_AT_CHARS = 64

NOT_PORTABLE: tuple[tuple[str, str], ...] = (
    (
        "Connector sign-ins and tokens",
        "Connect your accounts again on the new install; resource ids in rules belong to the account they were made with.",
    ),
    ("Passkeys", "Enroll a passkey on the new install at /security."),
    ("Scoped tokens", "Create new ones on the new install if your administrator allows them."),
    (
        "Privacy filter and PII detection",
        "Install-wide: a desktop install's own, an administrator's on an organization server.",
    ),
    (
        "Connector on/off switches, notification detail, AI system names, update checks, log level",
        "Desktop-only settings; set them again if you move back to a desktop install.",
    ),
    ("Audit log", "Stays on the install that wrote it."),
)
_CUSTOM_RULES_NOT_PORTABLE = (
    "Custom rules",
    "Rules written by hand in settings.yaml are not exported; add them again on the Auto-accept page.",
)

_NOT_AN_EXPORT = "This is not a PrivacyFence settings export."
_TOO_LARGE = "This file is too large to be a PrivacyFence settings export."
_NEWER = "This file was made by a newer PrivacyFence. Update this install and try again."
_NO_RULES = "This settings export has no rules section."
_TOO_MANY_ROWS = f"This settings export has more than {MAX_IMPORT_ROWS} rules."
_ID_WARNING = (
    "Rules name resources (folders, channels, projects) by id. They only match if you connect the same accounts here."
)


class PersonalSettingsError(ValueError):
    """A problem with an uploaded file; the message is safe to show to the person."""


@dataclass(frozen=True)
class PersonalSettingsDocument:
    rows: tuple[ScopeRow, ...]
    dropped_rows: int
    step_up_scope: StepUpScope | None
    source_mode: str
    exported_at: str


class ItemStatus(str, Enum):
    ADD = "add"
    EXTEND = "extend"
    PRESENT = "present"
    SKIP = "skip"


@dataclass(frozen=True)
class ImportItem:
    row: ScopeRow
    status: ItemStatus
    reason: str
    sentence: str


class StepUpStatus(str, Enum):
    APPLY = "apply"
    UNCHANGED = "unchanged"
    SKIP = "skip"


@dataclass(frozen=True)
class StepUpItem:
    status: StepUpStatus
    file_scope: StepUpScope | None
    reason: str


@dataclass(frozen=True)
class ImportPlan:
    items: tuple[ImportItem, ...]
    dropped_rows: int
    step_up: StepUpItem

    def counts(self) -> dict[str, int]:
        return {status.value: sum(1 for item in self.items if item.status is status) for status in ItemStatus}

    def to_preview(self, *, sha256: str, document: PersonalSettingsDocument) -> dict[str, Any]:
        counts = self.counts()
        warnings: list[str] = []
        if counts["add"] or counts["extend"]:
            warnings.append(_ID_WARNING)
        if self.dropped_rows:
            noun = "entry" if self.dropped_rows == 1 else "entries"
            warnings.append(f"{self.dropped_rows} {noun} in the file could not be read and were left out.")
        return {
            "sha256": sha256,
            "source_mode": document.source_mode,
            "exported_at": document.exported_at,
            "counts": counts,
            "dropped_rows": self.dropped_rows,
            "items": [
                {"id": item.row.id, "sentence": item.sentence, "status": item.status.value, "reason": item.reason}
                for item in self.items
            ],
            "step_up": {
                "status": self.step_up.status.value,
                "scope": self.step_up.file_scope,
                "reason": self.step_up.reason,
            },
            "warnings": warnings,
        }


def build_export(
    rows: Iterable[ScopeRow],
    personal_step_up_scope: StepUpScope | None,
    *,
    mode: str,
    version: str,
    now: datetime,
) -> dict[str, Any]:
    """The export document for a person's rows. Custom rows are left out (and noted), and every
    row's ``former_ids`` (this install's own v2 ids) are dropped."""
    all_rows = list(rows)
    portable = [dataclasses.replace(row, former_ids=()) for row in all_rows if row.scope is not None]
    not_portable = [{"item": item, "note": note} for item, note in NOT_PORTABLE]
    if len(portable) != len(all_rows):
        item, note = _CUSTOM_RULES_NOT_PORTABLE
        not_portable.append({"item": item, "note": note})
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "exported_at": now.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        "source": {"mode": mode, "version": version},
        store.AUTO_ACCEPT_CONFIG_KEY: scope_rows.rows_to_config(portable),
        "step_up": {"scope": personal_step_up_scope},
        "not_portable": not_portable,
    }


def export_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=False) + "\n").encode("utf-8")


def _value_ok(value: Any) -> bool:
    def short_str(item: Any) -> bool:
        return isinstance(item, str) and len(item) <= _MAX_VALUE_CHARS

    if value is None or short_str(value):
        return True
    return isinstance(value, list) and len(value) <= _MAX_VALUE_ITEMS and all(short_str(item) for item in value)


def parse_document(raw: bytes) -> PersonalSettingsDocument:
    """Parse and harden an uploaded export. Raises ``PersonalSettingsError`` with a message that is
    safe to show; never trusts the file's ids (``former_ids`` are cleared)."""
    if len(raw) > MAX_IMPORT_BYTES:
        raise PersonalSettingsError(_TOO_LARGE)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise PersonalSettingsError(_NOT_AN_EXPORT) from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise PersonalSettingsError(_NOT_AN_EXPORT)
    version = data.get("format_version")
    if type(version) is not int:
        raise PersonalSettingsError(_NOT_AN_EXPORT)
    if version > FORMAT_VERSION:
        raise PersonalSettingsError(_NEWER)
    if version != FORMAT_VERSION:
        raise PersonalSettingsError(_NOT_AN_EXPORT)
    section = data.get(store.AUTO_ACCEPT_CONFIG_KEY)
    entries = section.get("rules") if isinstance(section, dict) else None
    if not isinstance(entries, list):
        raise PersonalSettingsError(_NO_RULES)
    if len(entries) > MAX_IMPORT_ROWS:
        raise PersonalSettingsError(_TOO_MANY_ROWS)

    rows: list[ScopeRow] = []
    dropped = 0
    for entry in entries:
        row = scope_rows.row_from_dict(entry)
        if row is None or not _value_ok(row.value):
            dropped += 1
            continue
        rows.append(dataclasses.replace(row, former_ids=()))

    step_up = data.get("step_up")
    scope = step_up.get("scope") if isinstance(step_up, dict) else None
    source = data.get("source")
    mode = source.get("mode") if isinstance(source, dict) else None
    exported_at = data.get("exported_at")
    return PersonalSettingsDocument(
        rows=tuple(rows),
        dropped_rows=dropped,
        step_up_scope=scope if isinstance(scope, str) and scope in STEP_UP_SCOPES else None,  # type: ignore[arg-type]
        source_mode=mode if mode in ("local", "org") else "",
        exported_at=exported_at if isinstance(exported_at, str) and len(exported_at) <= _MAX_EXPORTED_AT_CHARS else "",
    )


def _classify_step_up(
    file_scope: StepUpScope | None,
    *,
    step_up_active: bool,
    step_up_floor: str | None,
    current_personal_scope: str | None,
) -> StepUpItem:
    if file_scope is None:
        return StepUpItem(StepUpStatus.UNCHANGED, None, "The file sets no personal step-up level.")
    if not step_up_active:
        return StepUpItem(StepUpStatus.SKIP, file_scope, "Passkey step-up is not turned on on this install.")
    baseline = step_up_config.widest_scope(*(s for s in (step_up_floor, current_personal_scope) if s))
    if step_up_config.widest_scope(baseline, file_scope) == baseline:
        return StepUpItem(StepUpStatus.UNCHANGED, file_scope, "Already at this level or wider.")
    return StepUpItem(StepUpStatus.APPLY, file_scope, "")


def plan_import(
    document: PersonalSettingsDocument,
    existing_rows: Iterable[ScopeRow],
    *,
    step_up_active: bool,
    step_up_floor: str | None,
    current_personal_scope: str | None,
) -> ImportPlan:
    """Classify every file row against the install's rows by simulating the merge. Touches no file."""
    current_rows = list(existing_rows)
    items: list[ImportItem] = []
    for row in document.rows:
        sentence = describe.row_sentence(row)
        if row.scope is None:
            items.append(ImportItem(
                row, ItemStatus.SKIP,
                "Custom rules are not imported; add them again on the Auto-accept page.", sentence,
            ))
            continue
        if not row.operations <= propose.ALL_OPERATIONS:
            items.append(ImportItem(row, ItemStatus.SKIP, "Needs a newer PrivacyFence (unknown tool).", sentence))
            continue
        rules = scope_rows.compile_row(row)
        if not rules:
            items.append(ImportItem(row, ItemStatus.SKIP, "This rule cannot be applied by this version.", sentence))
            continue
        merged, touched = scope_rows.add_rules(current_rows, rules)
        real = dataclasses.replace(row, id=touched[0])
        if merged == current_rows:
            items.append(ImportItem(real, ItemStatus.PRESENT, "", sentence))
            continue
        old = next((r for r in current_rows if r.id == touched[0]), None)
        if old is None:
            items.append(ImportItem(real, ItemStatus.ADD, "", sentence))
        else:
            new = next(r for r in merged if r.id == touched[0])
            n = len(new.operations) - len(old.operations)
            items.append(ImportItem(real, ItemStatus.EXTEND, f"Adds {n} tool(s) to a rule you already have.", sentence))
        current_rows = merged
    return ImportPlan(
        items=tuple(items),
        dropped_rows=document.dropped_rows,
        step_up=_classify_step_up(
            document.step_up_scope,
            step_up_active=step_up_active,
            step_up_floor=step_up_floor,
            current_personal_scope=current_personal_scope,
        ),
    )


def apply_import(
    plan: ImportPlan,
    *,
    principal: "Principal",
    step_up_floor: str | None,
    merge_rules: Callable[[list[PolicyRule]], bool],
) -> dict[str, Any]:
    """Write a plan: the ADD and EXTEND rows through ``merge_rules`` (once, when there are any), then
    the step-up scope when its status is APPLY. The caller is inside ``principal``'s scope. Merge-only:
    nothing is removed or narrowed."""
    counts = plan.counts()
    to_write = [item.row for item in plan.items if item.status in (ItemStatus.ADD, ItemStatus.EXTEND)]
    if to_write:
        merge_rules(scope_rows.compile_rows(to_write))
    applied = plan.step_up.status is StepUpStatus.APPLY and plan.step_up.file_scope is not None
    if applied:
        step_up_config.set_personal_scope(
            principal, str(plan.step_up.file_scope), floor=step_up_floor or STEP_UP_SCOPES[0],
        )
    return {
        "added": counts["add"],
        "extended": counts["extend"],
        "present": counts["present"],
        "skipped": counts["skip"],
        "step_up_applied": applied,
    }


__all__ = [
    "FILE_NAME",
    "FORMAT",
    "FORMAT_VERSION",
    "MAX_IMPORT_BYTES",
    "MAX_IMPORT_ROWS",
    "NOT_PORTABLE",
    "ImportItem",
    "ImportPlan",
    "ItemStatus",
    "PersonalSettingsDocument",
    "PersonalSettingsError",
    "StepUpItem",
    "StepUpStatus",
    "apply_import",
    "build_export",
    "export_bytes",
    "parse_document",
    "plan_import",
]
