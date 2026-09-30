"""Google Contacts connector."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..contacts_client import ContactsClient, ContactsClientError
from ..google_errors import unavailable_error
from ..gate import current_reason, gated_call
from ..privacy_filter import apply_text

logger = logging.getLogger(__name__)


class ContactsConnector(Connector):
    def __init__(self, client: ContactsClient) -> None:
        self._contacts = client
        self.my_email: str = ""

    @property
    def name(self) -> str:
        return "contacts"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="contacts_list",
                description=(
                    "List contacts from the user's Google address book. Google blends "
                    "personally-saved contacts together with Workspace directory profiles "
                    "(colleagues) by default; use 'source' to split them apart. "
                    "Returns a list of contacts as {resource_name, display_name, given_name, "
                    "family_name, emails [{value, type}], phones [{value, type}], organization, "
                    "job_title, notes, photo_url, source, source_types}, where source is "
                    "'personal', 'directory', 'both' (a saved contact who is also a colleague) "
                    "or 'other', and notes may be redacted by the user's privacy "
                    "settings. Reads up to max_results contacts across pages; 'source' is applied before counting. "
                    "Use contacts_search instead to find a contact by name or email. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("max_results", "int", required=False, default=50,
                              description="Most contacts to fetch. Default 50, capped at 1000."),
                    ToolParam("source", "str", required=False, default="both",
                              description="Which contacts to return: 'personal' (contacts with a "
                                           "saved-contact source), 'directory' (contacts with a "
                                           "Workspace directory source; a contact can match both), "
                                           "or 'both' (default, no filtering). Anything else is an error."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="contacts_search",
                description=(
                    "Search contacts by name or email address. The default 'both' searches "
                    "saved contacts only; only 'directory' scans Workspace directory contacts. Note: 'directory' search only finds directory profiles you "
                    "already have some contact history with; there is no full company-directory "
                    "search under this app's permissions. Returns a list of contacts in the "
                    "same shape contacts_list returns (source is 'personal', 'directory', "
                    "'both' or 'other'), at most max_results; notes may be "
                    "redacted by the user's privacy settings. Use contacts_list instead to "
                    "browse without a query, and get a contact's full record with "
                    "contacts_get. Auto-approved."
                ),
                params=[
                    ToolParam("query", "str",
                              description="Text to look for in contacts' names and email "
                                           "addresses (case-insensitive substring match for "
                                           "'directory')."),
                    ToolParam("max_results", "int", required=False, default=20,
                              description="Most contacts to return. Default 20, capped at 30 for "
                                           "saved contacts, up to 1000 for directory."),
                    ToolParam("source", "str", required=False, default="both",
                              description="Which contacts to search: 'personal' (contacts with a "
                                           "saved-contact source), 'directory' (contacts with a "
                                           "Workspace directory source; a contact can match both), "
                                           "or 'both' (default, saved contacts only). Anything else is an error."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="contacts_get",
                description=(
                    "Fetch a single contact by resource name (e.g. 'people/c12345'). "
                    "'source' asserts the expected kind of contact ('personal', 'directory', "
                    "or 'both'/default); the call fails if the resource doesn't match. "
                    "Returns one contact in the same shape contacts_list returns; notes may be "
                    "redacted by the user's privacy settings. Get the resource name from "
                    "contacts_search or contacts_list. Auto-approved."
                ),
                params=[
                    ToolParam("resource_name", "str",
                              description="Resource name of the contact ('people/c12345'), "
                                           "from contacts_search or contacts_list (the "
                                           "resource_name field)."),
                    ToolParam("source", "str", required=False, default="both",
                              description="Kind of contact you expect: 'personal', 'directory', "
                                           "or 'both' (default, accepts either). The call "
                                           "fails if the contact is not of that kind."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="contacts_update",
                description=(
                    "Update a contact's fields. Provide only the fields you want to change. "
                    "Requires user approval. "
                    "emails and phones are JSON strings, e.g. "
                    "'[{\"value\": \"a@b.com\", \"type\": \"work\"}]'. "
                    "An empty value leaves that field as it is, except that emails and phones "
                    "passed as '[]' clear the list. Returns the updated contact in the same "
                    "shape contacts_get returns. Get the resource name from contacts_search, "
                    "and read the current values with contacts_get first."
                ),
                params=[
                    ToolParam("resource_name", "str",
                              description="Resource name of the contact to update "
                                           "('people/c12345'), from contacts_search or "
                                           "contacts_list."),
                    ToolParam("display_name", "str", required=False, default="",
                              description="New full name, split on spaces into given and family "
                                           "name. Empty leaves the name unchanged."),
                    ToolParam("emails", "str", required=False, default="",
                              description="JSON array of {value, type} objects, e.g. "
                                           "'[{\"value\": \"a@b.com\", \"type\": \"work\"}]'. "
                                           "Replaces all the contact's emails, so include the "
                                           "ones to keep. Empty leaves them unchanged; "
                                           "'[]' clears them."),
                    ToolParam("phones", "str", required=False, default="",
                              description="JSON array of {value, type} objects, e.g. "
                                           "'[{\"value\": \"+15551234567\", \"type\": \"mobile\"}]'. "
                                           "Replaces all the contact's phones, so include the "
                                           "ones to keep. Empty leaves them unchanged; "
                                           "'[]' clears them."),
                    ToolParam("organization", "str", required=False, default="",
                              description="New company name. Empty leaves it unchanged."),
                    ToolParam("job_title", "str", required=False, default="",
                              description="New job title. Empty leaves it unchanged."),
                    ToolParam("notes", "str", required=False, default="",
                              description="New notes, replacing the current ones. Empty leaves "
                                           "the notes unchanged."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="contacts_create",
                description=(
                    "Create a new contact in the user's Google address book. "
                    "Requires user approval. "
                    "emails and phones are JSON strings, e.g. "
                    "'[{\"value\": \"a@b.com\", \"type\": \"work\"}]'. "
                    "Contact deletion is not supported. Returns the created contact, with its "
                    "new resource_name, in the same shape contacts_get returns. Search with "
                    "contacts_search first to avoid creating a duplicate."
                ),
                params=[
                    ToolParam("display_name", "str",
                              description="Full name of the new contact, split on spaces into "
                                           "given and family name."),
                    ToolParam("emails", "str", required=False, default="",
                              description="JSON array of {value, type} objects, e.g. "
                                           "'[{\"value\": \"a@b.com\", \"type\": \"work\"}]'. "
                                           "Empty means no emails."),
                    ToolParam("phones", "str", required=False, default="",
                              description="JSON array of {value, type} objects, e.g. "
                                           "'[{\"value\": \"+15551234567\", \"type\": \"mobile\"}]'. "
                                           "Empty means no phones."),
                    ToolParam("organization", "str", required=False, default="",
                              description="Company name. Empty means none."),
                    ToolParam("job_title", "str", required=False, default="",
                              description="Job title. Empty means none."),
                    ToolParam("notes", "str", required=False, default="",
                              description="Free-text notes about the contact. Empty means "
                                           "no notes."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="contacts_add_label",
                description=(
                    "Add a label to a contact, creating the label if it doesn't already exist. "
                    "Returns {resource_name, label_added}. Labels are matched by name without "
                    "regard to case. Use contacts_remove_label to take a label off again. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("resource_name", "str",
                              description="Resource name of the contact ('people/c12345'), "
                                           "from contacts_search or contacts_list."),
                    ToolParam("label_name", "str",
                              description="Name of the label (contact group) to add, for "
                                           "example 'Clients'. An unknown name creates a new label."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="contacts_remove_label",
                description=(
                    "Remove a label from a contact. Returns {resource_name, label_removed}, "
                    "with a note of 'label not found' if no label has that name (nothing "
                    "changes then). Use contacts_add_label to put a label on a contact. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("resource_name", "str",
                              description="Resource name of the contact ('people/c12345'), "
                                           "from contacts_search or contacts_list."),
                    ToolParam("label_name", "str",
                              description="Name of the label (contact group) to remove, "
                                           "matched without regard to case. The label itself "
                                           "is not deleted."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "contacts_list":
            return await self._contacts_list(**args)
        if tool == "contacts_search":
            return await self._contacts_search(**args)
        if tool == "contacts_get":
            return await self._contacts_get(**args)
        if tool == "contacts_update":
            return await self._contacts_update(**args)
        if tool == "contacts_create":
            return await self._contacts_create(**args)
        if tool == "contacts_add_label":
            return await self._contacts_add_label(**args)
        if tool == "contacts_remove_label":
            return await self._contacts_remove_label(**args)
        raise ValueError(f"Unknown Contacts tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _contacts_list(self, max_results: int = 50, source: str = "both") -> Any:
        t0 = time.time()
        contacts = await self._fetch(self._contacts.list_contacts, max_results, source)
        result = [_redact_notes(c.to_dict()) for c in contacts]
        self._auto_audit("contacts_list", "List Contacts",
                         f"List contacts (max {max_results}, source={source})", f"{len(result)} contact(s)", t0)
        return result

    async def _contacts_search(self, query: str, max_results: int = 20, source: str = "both") -> Any:
        t0 = time.time()
        contacts = await self._fetch(self._contacts.search_contacts, query, max_results, source)
        result = [_redact_notes(c.to_dict()) for c in contacts]
        self._auto_audit("contacts_search", "Search Contacts",
                         f"Search: {query!r} (source={source})", f"{len(result)} result(s)", t0)
        return result

    async def _contacts_get(self, resource_name: str, source: str = "both") -> Any:
        t0 = time.time()
        contact = await self._fetch(self._contacts.get_contact, resource_name, source)
        result = _redact_notes(contact.to_dict())
        self._auto_audit("contacts_get", "Get Contact",
                         f"Get: {resource_name}", contact.display_name or resource_name, t0)
        return result

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _contacts_update(
        self,
        resource_name: str,
        display_name: str = "",
        emails: str = "",
        phones: str = "",
        organization: str = "",
        job_title: str = "",
        notes: str = "",
    ) -> Any:
        emails_list: list[dict] | None = _parse_json_list(emails, "emails")
        phones_list: list[dict] | None = _parse_json_list(phones, "phones")

        existing = await self._fetch(self._contacts.get_contact, resource_name)
        contact_name = existing.display_name or resource_name

        # Name/Emails/Phones always appear -- each is the plain current
        # value if unchanged, or "old → new" if this call is changing it
        # (same treatment as calendar_update_event's Event/Start/End),
        # instead of the old separate "Contact" (always current name) plus
        # a redundant "Name" row only when it happened to be changing.
        # Organization/Job title stay conditional -- optional fields that
        # are often blank entirely, unlike a contact's name/emails/phones.
        def _diff_or_value(new_value: str, old_value: str) -> str:
            return f"{old_value} → {new_value}" if new_value and new_value != old_value else old_value

        old_emails = ", ".join(e.value for e in existing.emails) or "(none)"
        old_phones = ", ".join(p.value for p in existing.phones) or "(none)"
        new_emails = "(cleared)" if emails_list == [] else ", ".join(e["value"] for e in emails_list or [])
        new_phones = "(cleared)" if phones_list == [] else ", ".join(p["value"] for p in phones_list or [])

        preview = {
            "Name": _diff_or_value(display_name, contact_name),
            "Emails": _diff_or_value(new_emails, old_emails),
            "Phones": _diff_or_value(new_phones, old_phones),
        }
        changed_field_names = [
            k for k, v in preview.items() if " → " in v
        ]
        if organization and organization != existing.organization:
            preview["Organization"] = f"{existing.organization or '(none)'} → {organization}"
            changed_field_names.append("Organization")
        if job_title and job_title != existing.job_title:
            preview["Job title"] = f"{existing.job_title or '(none)'} → {job_title}"
            changed_field_names.append("Job title")

        args = {
            "resource_name": resource_name, "display_name": display_name,
            "emails": emails, "phones": phones, "organization": organization,
            "job_title": job_title, "notes": notes,
        }
        if notes:
            details_text = notes
        else:
            changed_fields = ", ".join(changed_field_names) or "no fields"
            details_text = f"{changed_fields} will be updated; notes unchanged."
        await gated_call(
            connector=self.name,
            tool="contacts_update",
            tool_name="Update Contact",
            summary=f"Update contact: {contact_name}",
            sender=contact_name,
            raw_data=args,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details_text,
            my_email=self.my_email,
            args=args,
        )
        updated = await self._fetch(
            self._contacts.update_contact,
            resource_name,
            display_name or None,
            emails_list,
            phones_list,
            organization or None,
            job_title or None,
            notes or None,
        )
        return updated.to_dict()

    async def _contacts_create(
        self,
        display_name: str,
        emails: str = "",
        phones: str = "",
        organization: str = "",
        job_title: str = "",
        notes: str = "",
    ) -> Any:
        emails_list = _parse_json_list(emails, "emails")
        phones_list = _parse_json_list(phones, "phones")

        preview = {"Name": display_name}
        if emails_list:
            preview["Emails"] = ", ".join(e.get("value", "") for e in emails_list)
        if phones_list:
            preview["Phones"] = ", ".join(p.get("value", "") for p in phones_list)
        if organization:
            preview["Organization"] = organization
        if job_title:
            preview["Job title"] = job_title

        args = {
            "display_name": display_name, "emails": emails, "phones": phones,
            "organization": organization, "job_title": job_title, "notes": notes,
        }
        await gated_call(
            connector=self.name,
            tool="contacts_create",
            tool_name="Create Contact",
            summary=f"Create contact: {display_name}",
            sender=display_name,
            raw_data=args,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=notes or "No notes provided; see preview for contact details.",
            my_email=self.my_email,
            args=args,
        )
        created = await self._fetch(
            self._contacts.create_contact,
            display_name, emails_list, phones_list,
            organization or None, job_title or None, notes or None,
        )
        return created.to_dict()

    async def _contacts_add_label(self, resource_name: str, label_name: str) -> Any:
        contact_name = await self._contact_name_for(resource_name)
        args = {"resource_name": resource_name, "label_name": label_name}
        await gated_call(
            connector=self.name,
            tool="contacts_add_label",
            tool_name="Add Contact Label",
            summary=f"Add label '{label_name}' to: {contact_name}",
            sender=contact_name,
            raw_data=args,
            filtered_data=None,
            gate="popup",
            preview={"Name": contact_name, "Label": label_name},
            details_text="Label will be added to this contact; no other fields change.",
            my_email=self.my_email,
            args=args,
        )
        return await self._fetch(self._contacts.add_label, resource_name, label_name)

    async def _contacts_remove_label(self, resource_name: str, label_name: str) -> Any:
        contact_name = await self._contact_name_for(resource_name)
        args = {"resource_name": resource_name, "label_name": label_name}
        await gated_call(
            connector=self.name,
            tool="contacts_remove_label",
            tool_name="Remove Contact Label",
            summary=f"Remove label '{label_name}' from: {contact_name}",
            sender=contact_name,
            raw_data=args,
            filtered_data=None,
            gate="popup",
            preview={"Name": contact_name, "Label": label_name},
            details_text="Label will be removed from this contact; no other fields change.",
            my_email=self.my_email,
            args=args,
        )
        return await self._fetch(self._contacts.remove_label, resource_name, label_name)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _contact_name_for(self, resource_name: str) -> str:
        try:
            current = await self._fetch(self._contacts.get_contact, resource_name)
            return current.display_name or resource_name
        except Exception:
            return resource_name

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except ContactsClientError as exc:
            logger.error("Contacts fetch failed: %s", exc)
            unavailable = unavailable_error("contacts", exc, self.my_email)
            if unavailable is not None:
                raise unavailable from exc
            raise RuntimeError(str(exc)) from exc

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


def _parse_json_list(value: str, field: str) -> list[dict] | None:
    """Parse an emails/phones argument. Empty means "not given" (None); anything
    else must be a JSON list of objects with a non-empty string ``value``."""
    if not value or not value.strip():
        return None
    message = (
        f"{field} must be a JSON list such as "
        "[{\"value\": \"a@b.com\", \"type\": \"work\"}]"
    )
    try:
        parsed = json.loads(value)
    except ValueError as exc:
        raise ValueError(message) from exc
    if not isinstance(parsed, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("value"), str)
        and item["value"].strip()
        for item in parsed
    ):
        raise ValueError(message)
    return parsed


def _redact_notes(contact_dict: dict[str, Any]) -> dict[str, Any]:
    """Apply contacts_privacy's "notes" category to a contact's free-text
    biography field -- the one field on a contact that can carry arbitrary
    personal content, unlike the structured name/email/phone/org fields
    around it, none of which have a category of their own (see
    privacy_filter.py's module docstring for scope)."""
    contact_dict["notes"] = apply_text("contacts_privacy", "notes", contact_dict.get("notes", "") or "")
    return contact_dict
