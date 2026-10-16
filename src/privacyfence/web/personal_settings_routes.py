"""The personal-settings export/import routes, one factory for both modes.

``build_personal_routes`` is given a ``PersonalSettingsHooks`` that says how a mode authenticates,
checks CSRF/origin, enters a principal's scope, reads and writes that principal's rows and runs the
passkey step-up, so local and organization mode cannot drift apart. The file format, the preview and
the merge-only apply live in ``privacyfence.personal_settings``; this module is only the HTTP shell.

Three routes, none of which the generic ``POST /api/settings/{action}`` can swallow (two path
segments): ``GET .../personal/export`` downloads the file, ``POST .../personal/preview`` plans an
upload and changes nothing, ``POST .../personal/import`` re-plans the same upload against the rows as
they are now and applies it (merge-only), gated by the passkey step-up bound to the file's hash.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from .. import __version__, personal_settings, step_up_config
from ..policy.engine import PolicyRule
from ..policy.scope_rows import ScopeRow
from ..principal import Principal

STEP_UP_SUBJECT_KEY = "import_personal_settings"

_TOO_LARGE_BODY = {"error": "This file is too large to be a PrivacyFence settings export."}
# Slack over MAX_IMPORT_BYTES for the multipart envelope (boundaries, the other fields).
_ENVELOPE_SLACK = 65_536


@dataclass(frozen=True)
class PersonalSettingsHooks:
    authenticate: Callable[[Request], Principal | None]
    check_mutation: Callable[[Request, str | None], Response | None]
    open_scope: Callable[[Principal], AbstractContextManager[object]]
    scope_error: Callable[[Principal, Exception], Response | None]
    read_rows: Callable[[], list[ScopeRow]]
    merge_rules: Callable[[list[PolicyRule]], bool]
    step_up_state: Callable[[], tuple[bool, str | None]]
    step_up_gate: Callable[[Principal, dict[str, Any], object], Response | None]
    audit: Callable[[Principal, str], None]
    mode: str


def _unauthorized() -> Response:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def _parse_assertion(value: Any) -> dict[str, Any] | None:
    """The ``webauthn_assertion`` form field is a JSON-encoded string; a malformed or missing one
    reads as no assertion (the gate then answers with a challenge), never as a 500."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _too_large() -> Response:
    return JSONResponse(_TOO_LARGE_BODY, status_code=413)


def build_personal_routes(hooks: PersonalSettingsHooks) -> list[BaseRoute]:
    def _scoped(principal: Principal, work: Callable[[], Response]) -> Response:
        try:
            with hooks.open_scope(principal):
                return work()
        except Exception as exc:
            response = hooks.scope_error(principal, exc)
            if response is None:
                raise
            return response

    def _plan(
        principal: Principal, document: personal_settings.PersonalSettingsDocument,
    ) -> personal_settings.ImportPlan:
        # Inside open_scope: the rows and the step-up preference are the current principal's.
        active, floor = hooks.step_up_state()
        return personal_settings.plan_import(
            document, hooks.read_rows(),
            step_up_active=active, step_up_floor=floor,
            current_personal_scope=step_up_config.personal_scope(principal),
        )

    async def _read_upload(request: Request) -> tuple[Any, Response | None]:
        """The multipart form, or a refusal. The size check on ``Content-Length`` comes before
        ``request.form()`` so an oversized body is never buffered."""
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > personal_settings.MAX_IMPORT_BYTES + _ENVELOPE_SLACK:
            return None, _too_large()
        return await request.form(), None

    async def _read_file(form: Any) -> tuple[bytes | None, Response | None]:
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return None, JSONResponse({"error": "missing file"}, status_code=400)
        raw: bytes = await upload.read(personal_settings.MAX_IMPORT_BYTES + 1)
        if len(raw) > personal_settings.MAX_IMPORT_BYTES:
            return None, _too_large()
        return raw, None

    async def export(request: Request) -> Response:
        principal = hooks.authenticate(request)
        if principal is None:
            return _unauthorized()

        def work() -> Response:
            rows = hooks.read_rows()
            document = personal_settings.build_export(
                rows, step_up_config.personal_scope(principal),
                mode=hooks.mode, version=__version__, now=datetime.now(timezone.utc),
            )
            exported = len(document["auto_accept"]["rules"])
            hooks.audit(principal, f"Exported personal settings ({exported} rule(s)) (principal={principal.id})")
            return Response(
                personal_settings.export_bytes(document),
                media_type="application/json",
                headers={
                    "Content-Disposition": f'attachment; filename="{personal_settings.FILE_NAME}"',
                    "Cache-Control": "no-store",
                },
            )

        return await run_in_threadpool(_scoped, principal, work)

    async def preview(request: Request) -> Response:
        principal = hooks.authenticate(request)
        if principal is None:
            return _unauthorized()
        form, refusal = await _read_upload(request)
        if refusal is not None:
            return refusal
        csrf = form.get("csrf")
        rejected = hooks.check_mutation(request, csrf if isinstance(csrf, str) else None)
        if rejected is not None:
            return rejected
        raw, refusal = await _read_file(form)
        if refusal is not None:
            return refusal
        assert raw is not None  # nosec B101  # _read_file returns raw or a refusal
        try:
            document = personal_settings.parse_document(raw)
        except personal_settings.PersonalSettingsError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        digest = hashlib.sha256(raw).hexdigest()

        def work() -> Response:
            plan = _plan(principal, document)
            return JSONResponse(plan.to_preview(sha256=digest, document=document))

        return await run_in_threadpool(_scoped, principal, work)

    async def import_(request: Request) -> Response:
        principal = hooks.authenticate(request)
        if principal is None:
            return _unauthorized()
        form, refusal = await _read_upload(request)
        if refusal is not None:
            return refusal
        csrf = form.get("csrf")
        rejected = hooks.check_mutation(request, csrf if isinstance(csrf, str) else None)
        if rejected is not None:
            return rejected
        if form.get("confirmed") != "true":
            return JSONResponse({"error": "confirmation required"}, status_code=400)
        raw, refusal = await _read_file(form)
        if refusal is not None:
            return refusal
        assert raw is not None  # nosec B101  # _read_file returns raw or a refusal
        try:
            document = personal_settings.parse_document(raw)
        except personal_settings.PersonalSettingsError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        digest = hashlib.sha256(raw).hexdigest()
        if form.get("sha256") != digest:
            return JSONResponse({"error": "file_changed"}, status_code=400)
        gated = hooks.step_up_gate(principal, {"sha256": digest}, _parse_assertion(form.get("webauthn_assertion")))
        if gated is not None:
            return gated

        def work() -> Response:
            # Re-planned against the rows as they are now: a stale preview is never applied.
            plan = _plan(principal, document)
            _, floor = hooks.step_up_state()
            result = personal_settings.apply_import(
                plan, principal=principal, step_up_floor=floor, merge_rules=hooks.merge_rules,
            )
            hooks.audit(
                principal,
                f"Imported personal settings: {result['added']} added, {result['extended']} extended, "
                f"{result['present']} already present, {result['skipped']} skipped (principal={principal.id})",
            )
            return JSONResponse(result)

        return await run_in_threadpool(_scoped, principal, work)

    return [
        Route("/api/settings/personal/export", export),
        Route("/api/settings/personal/preview", preview, methods=["POST"]),
        Route("/api/settings/personal/import", import_, methods=["POST"]),
    ]


__all__ = ["STEP_UP_SUBJECT_KEY", "PersonalSettingsHooks", "build_personal_routes"]
