"""The org-mode read-only settings surface (#400, first cut).

Two purpose-built pages, not a port of ``web/routes_settings.py``'s ~25-action
local-mode surface -- ``web/org_settings_scope.py``'s own docstring explains
why porting that dispatcher wholesale is the wrong shape for org mode, and
``server.py``'s module docstring lists what deciding the ownership split
(#400 C3b) and wiring ``Principal.is_admin`` (#400 C3c) still left undone:
a route that actually consumes both.

``GET /settings`` -- every signed-in principal's own auto-accept rules and
resource grants (``auto_accept.py``/``resource_grants.py``), read-only except
for adding or removing a rule row and removing a grant row: an admin has no
more mutation power here over another principal's rules than that principal
does over their own, since every mutation is always scoped to
``current_principal()``, never a path parameter naming someone else's id.

``GET /settings/privacy`` -- admin-only (``Principal.is_admin``, #400 C3c),
the install-wide PII/privacy policy: which ``privacy``/``drive_privacy``/...
group is explicitly configured versus silently relying on org mode's own
fail-safe ``block`` default (``privacy_filter.init_privacy_filter``'s own
``fail_safe_default`` -- see that function's docstring), and, since #400
C3e, **editable** rather than only shown. C3d shipped it read-only because
the issue left the restart story open; ``web/org_install_policy.py`` is
where that got answered (hot-reload, not a restart-required banner) and
holds the whole write path -- validation, the atomic settings.yaml write,
and making the change live for every principal rather than only the admin
who made it. This module's two ``POST /api/settings/privacy/...`` routes
are the HTTP shell around it: authenticate, CSRF/origin, authorize through
``org_settings_scope.is_action_permitted`` under the same action names
local mode dispatches, apply, audit.

Every mutation here is audit-logged under the principal who made it
(``_record_settings_audit``), which #400 calls for by name: "Changing an
org's privacy policy is exactly the kind of act that belongs in the audit
log under the principal who did it."

Both pages go through ``web_shell.wrap()`` with ``web_shell.ORG_NAV_ITEMS``,
the same shell ``routes_org_approvals.py``/``routes_connect.py``/``routes_
security.py`` use -- this is an org-mode surface, so it should look like
``/approvals``/``/connect``/``/security``, not like the desktop-app-shaped
local settings page, and (since those other three pages all carry the same
header) it should carry the persistent top nav they do rather than being the
one org-mode page a signed-in principal can navigate into and get stuck on.
"""
from __future__ import annotations

import html
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import auto_accept, pii_detector, resource_grants
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..principal import Principal, principal_scope
from ..privacy_filter import VALID_POLICIES, PrivacyFilterConfigError
from ..privacy_filter import _parse_group as _parse_privacy_group
from ..settings_controller import (
    OPERATION_LABELS,
    PRIVACY_CATEGORY_LABELS,
    PRIVACY_GROUP_LABELS,
    RULES_BY_OPERATION,
    RULES_INT_VALUE,
    RULES_LIST_VALUE,
)
from .. import web_shell
from . import org_install_policy, org_session
from .csp import nonce_for as _csp_nonce_for
from .org_session import OrgSessionStore
from .org_settings_scope import is_action_permitted

logger = logging.getLogger(__name__)

_PAGE_CSS = """
.pf-wrap{max-width:720px;margin:0 auto;padding:24px 20px}
h1{font-size:1.3rem} h2{font-size:1.05rem;margin-top:2em}
table{width:100%;border-collapse:collapse;margin:0.5em 0}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--color-border,#ddd);font-size:0.92em}
.pf-fallback{color:var(--color-warning,#a94500);font-weight:600}
.pf-empty{color:var(--color-muted,#777);font-style:italic}
.pf-note{color:var(--color-muted,#777);font-size:0.9em}
form.pf-remove{display:inline}
form.pf-set{display:inline}
button.pf-remove{font-size:0.85em;padding:2px 8px}
"""


def _page(title: str, body: str, *, nonce: str, principal_label: str = "") -> str:
    """``nonce`` is the response's own CSP nonce (web/csp.py), threaded
    through to both this page's own ``<style>`` element and to
    ``web_shell.wrap()``'s -- one document, one CSP header, one nonce, same
    convention every other org-mode page follows (web_shell.py's own
    ``wrap()`` docstring). ``active="settings"`` covers both callers below
    (``/settings`` and the admin-only ``/settings/privacy``) -- there is no
    separate nav entry for the privacy sub-page.
    """
    wrapped_body = f'<style nonce="{html.escape(nonce, quote=True)}">{_PAGE_CSS}</style><div class="pf-wrap">{body}</div>'
    return web_shell.wrap(
        wrapped_body, title=f"PrivacyFence — {title}", active="settings", nonce=nonce,
        nav_items=web_shell.ORG_NAV_ITEMS, principal_label=principal_label,
        live_updates=False, notifications_enabled=False,
    )


def _record_settings_audit(principal: Principal, summary: str) -> None:
    # Same free-form AuditEntry shape daemon_main.log_org_config_bundle_hash
    # uses for an install-level event that isn't a connector call -- this is
    # a settings mutation, not a gated tool call, so "connector"/"tool" stay
    # empty and "decision" carries a custom, non-approval value.
    try:
        get_audit_logger().record(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            week=current_week(),
            request_id=uuid.uuid4().hex[:12],
            connector="", tool="", tool_name="",
            summary=summary,
            sender=principal.email or principal.id,
            decision="settings_change",
            auto_accept_rule="", latency_seconds=0.0, pii_detected=False,
        ))
    except Exception as exc:
        logger.warning("Audit log write failed for a settings change: %s", exc)


def _rules_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for op_key, entries in sorted((cfg.get("auto_accept_rules") or {}).items()):
        for entry in entries:
            rows.append({
                "op_key": op_key,
                "op_label": OPERATION_LABELS.get(op_key, op_key),
                "rule": entry.get("rule", ""),
                "value": entry.get("value"),
            })
    return rows


def _grant_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    grants_cfg = cfg.get("auto_accept_grants") or {}
    rows: list[dict[str, Any]] = []
    for rt in resource_grants.GRANT_RESOURCE_TYPES:
        for entry in resource_grants.get_grant_entries(grants_cfg, rt):
            enabled = [cap.label for key, cap in rt.capabilities.items() if entry.get(key)]
            rows.append({
                "connector": rt.connector,
                "config_key": rt.config_key,
                "label": rt.label,
                "resource_id": rt.id_of(entry),
                "name": entry.get("name") or rt.id_of(entry),
                "tab": entry.get("tab") or "",
                "capabilities": ", ".join(enabled) if enabled else "(none enabled)",
            })
    return rows


def _rule_type_label(rule_name: str) -> str:
    # Same "replace underscores, capitalize the first letter" shape
    # settings_window_html.py's own client-side ruleTypeLabel() uses for the
    # local-mode picker -- kept in sync by eye rather than shared code since
    # one is Python building a <select> server-side and the other is JS
    # building one in the browser, but a reviewer should see the same label
    # for the same rule name on either surface.
    s = rule_name.replace("_", " ")
    return s[:1].upper() + s[1:]


def _parse_rule_value_field(rule_name: str, raw_text: str) -> Any:
    # settings_controller._parse_rule_value's own logic, against the same
    # RULES_LIST_VALUE/RULES_INT_VALUE registry -- duplicated rather than
    # imported since that function is private to settings_controller.py and
    # this is a handful of lines, not a shared algorithm worth coupling two
    # modules over. Empty text means "boolean rule, no value", matching that
    # function's own docstring.
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return None
    if rule_name in RULES_LIST_VALUE:
        return [v.strip() for v in raw_text.split(",") if v.strip()]
    if rule_name in RULES_INT_VALUE:
        try:
            return int(raw_text)
        except ValueError:
            return raw_text
    return raw_text


def _add_rule_form_html(csrf_esc: str) -> str:
    # One <select> with an <optgroup> per operation rather than a
    # connector-nav sidebar like settings_window_html.py's local-mode
    # picker -- this page is plain server-rendered forms with no JS (see
    # module docstring: "reuse routes_org_approvals.py's minimal
    # doctype+tokens.css shell"), so there is no client-side way to filter a
    # second <select>'s options by a first one's choice. Grouping by
    # operation keeps the ~135 (operation, rule type) pairs navigable
    # without needing one.
    optgroups = []
    for op_key, rule_names in RULES_BY_OPERATION.items():
        op_label = html.escape(OPERATION_LABELS.get(op_key, op_key), quote=True)
        options = "".join(
            f"<option value=\"{html.escape(op_key, quote=True)}|{html.escape(rule_name, quote=True)}\">"
            f"{html.escape(_rule_type_label(rule_name))}</option>"
            for rule_name in rule_names
        )
        optgroups.append(f"<optgroup label=\"{op_label}\">{options}</optgroup>")
    return (
        "<form class=\"pf-set\" method=\"post\" action=\"/api/settings/rules/add\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf_esc}\">"
        "<select name=\"rule_choice\" required aria-label=\"Operation and rule type\">"
        "<option value=\"\">Select an operation and rule type…</option>"
        + "".join(optgroups) +
        "</select> "
        "<input type=\"text\" name=\"value\" "
        "placeholder=\"Value -- comma-separated list, a number, or leave blank\" "
        "aria-label=\"Rule value\"> "
        "<button type=\"submit\">Add rule</button>"
        "</form>"
    )


def _render_settings_page(cfg: dict[str, Any], *, principal: Principal, csrf: str) -> str:
    rule_rows = _rules_rows(cfg)
    grant_rows = _grant_rows(cfg)
    csrf_esc = html.escape(csrf, quote=True)

    rules_html = "<p class=\"pf-empty\">No auto-accept rules configured.</p>"
    if rule_rows:
        body_rows = "".join(
            f"<tr><td>{html.escape(r['op_label'])}</td><td>{html.escape(str(r['rule']))}</td>"
            f"<td>{html.escape(json.dumps(r['value']) if r['value'] is not None else '')}</td><td>"
            f"<form class=\"pf-remove\" method=\"post\" action=\"/api/settings/rules/remove\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf_esc}\">"
            f"<input type=\"hidden\" name=\"op_key\" value=\"{html.escape(r['op_key'], quote=True)}\">"
            f"<input type=\"hidden\" name=\"rule\" value=\"{html.escape(str(r['rule']), quote=True)}\">"
            f"<input type=\"hidden\" name=\"value\" value=\"{html.escape(json.dumps(r['value']), quote=True)}\">"
            f"<button class=\"pf-remove\" type=\"submit\">Remove</button></form></td></tr>"
            for r in rule_rows
        )
        rules_html = (
            "<table><thead><tr><th>Operation</th><th>Rule</th><th>Value</th><th></th></tr></thead>"
            f"<tbody>{body_rows}</tbody></table>"
        )

    grants_html = "<p class=\"pf-empty\">No trusted resources configured.</p>"
    if grant_rows:
        body_rows = "".join(
            f"<tr><td>{html.escape(g['label'])}</td><td>{html.escape(g['name'])}</td>"
            f"<td>{html.escape(g['capabilities'])}</td><td>"
            f"<form class=\"pf-remove\" method=\"post\" action=\"/api/settings/grants/remove\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf_esc}\">"
            f"<input type=\"hidden\" name=\"connector\" value=\"{html.escape(g['connector'], quote=True)}\">"
            f"<input type=\"hidden\" name=\"config_key\" value=\"{html.escape(g['config_key'], quote=True)}\">"
            f"<input type=\"hidden\" name=\"resource_id\" value=\"{html.escape(g['resource_id'], quote=True)}\">"
            f"<input type=\"hidden\" name=\"tab\" value=\"{html.escape(g['tab'], quote=True)}\">"
            f"<button class=\"pf-remove\" type=\"submit\">Remove</button></form></td></tr>"
            for g in grant_rows
        )
        grants_html = (
            "<table><thead><tr><th>Type</th><th>Resource</th><th>Capabilities</th><th></th></tr></thead>"
            f"<tbody>{body_rows}</tbody></table>"
        )

    admin_link = (
        "<p><a href=\"/settings/privacy\">Install-wide privacy/PII policy (admin)</a></p>"
        if principal.is_admin else ""
    )

    return (
        f"<h1>Settings</h1>"
        f"<p>Signed in as {html.escape(principal.email or principal.id)}.</p>"
        f"{admin_link}"
        f"<h2>Auto-accept rules</h2>{rules_html}{_add_rule_form_html(csrf_esc)}"
        f"<h2>Trusted resources</h2>{grants_html}"
    )


def _privacy_policy_view(install_wide_settings: dict[str, Any]) -> list[dict[str, Any]]:
    """The install-wide PII/privacy policy, resolved the same way
    ``privacy_filter.init_privacy_filter`` resolves it for real enforcement
    (``fail_safe_default="block"`` -- org mode always, never the "allow"
    fallback local mode gets) -- plus, unlike ``category_policy()``'s own
    ``_REGISTRY`` (which only ever stores the resolved value, not where it
    came from), whether each group is present in ``install_wide_settings``
    at all. A group key genuinely absent from the raw config is exactly
    ``_parse_group``'s own ``raw is None`` case -- the one falling back to
    the org-mode ``block`` default nobody configured.
    """
    groups: list[dict[str, Any]] = []
    for group, label in PRIVACY_GROUP_LABELS.items():
        raw = install_wide_settings.get(group)
        try:
            parsed = _parse_privacy_group(raw, group=group, fail_safe_default="block")
        except PrivacyFilterConfigError as exc:
            logger.warning("Could not render effective %s policy: %s", group, exc)
            parsed = {"default_policy": "block", "categories": {}}
        categories = [
            {
                "key": cat_key, "label": cat_label,
                "policy": parsed["categories"].get(cat_key, parsed["default_policy"]),
            }
            for cat_key, cat_label in PRIVACY_CATEGORY_LABELS.get(group, {}).items()
        ]
        groups.append({
            "key": group, "label": label, "default_policy": parsed["default_policy"],
            "categories": categories, "falls_back_to_block_default": raw is None,
            # The explicitly-configured value, as distinct from the resolved
            # one above: a category with no entry of its own inherits the
            # group default, and the editor has to render that as "inherit"
            # rather than as a deliberate choice of whatever the default
            # currently happens to be -- otherwise merely opening the page
            # and saving would freeze every inherited category at today's
            # default, silently decoupling it from the next change.
            "explicit_default_policy": (raw or {}).get("default_policy") if isinstance(raw, dict) else None,
            "explicit_categories": (
                dict((raw or {}).get("categories") or {}) if isinstance(raw, dict) else {}
            ),
        })
    return groups


def _pii_view(install_wide_settings: dict[str, Any]) -> dict[str, Any]:
    """The install-wide PII gate as ``daemon_main.run_app`` reads it out of
    settings.yaml -- the master switch plus the two individually-toggleable
    categories (``pii_detector.optional_category_keys()``). Read from the
    config dict, not from ``pii_detector``'s own live registry: the registry
    is per-principal, and what this page is about is the install-wide file
    every principal's entry is built from.
    """
    pii_cfg = install_wide_settings.get("pii_detection", {}) or {}
    enabled = bool(pii_cfg.get("enabled", True))
    return {
        "enabled": enabled,
        "categories": [
            {
                "key": key,
                "label": pii_detector.optional_category_label(key),
                "enabled": bool(pii_cfg.get(key, True)),
            }
            for key in pii_detector.optional_category_keys()
        ],
    }


def _policy_select(name: str, selected: str | None, *, include_inherit: bool) -> str:
    options = []
    if include_inherit:
        options.append(
            f"<option value=\"\"{' selected' if selected is None else ''}>(inherit group default)</option>"
        )
    for policy in VALID_POLICIES:
        options.append(
            f"<option value=\"{policy}\"{' selected' if policy == selected else ''}>{policy}</option>"
        )
    return f"<select name=\"{name}\">{''.join(options)}</select>"


def _hidden(name: str, value: str) -> str:
    return f"<input type=\"hidden\" name=\"{html.escape(name, quote=True)}\" value=\"{html.escape(value, quote=True)}\">"


def _render_privacy_page(
    groups: list[dict[str, Any]], pii: dict[str, Any], *, editable: bool, csrf: str,
) -> str:
    """``editable`` is False only when this daemon has no install-wide
    settings.yaml path to write back to (``build_routes``'s own
    ``install_wide_settings_path``, empty for a caller that never had a real
    file behind the dict) -- then this renders exactly the read-only page
    #400 C3d shipped, rather than drawing controls whose POST the write
    routes would reject anyway. It is not the admin check: a non-admin never
    reaches this function at all, the route 403s them first.
    """
    csrf_field = _hidden("csrf", csrf)
    sections = []
    for g in groups:
        fallback_note = (
            "<span class=\"pf-fallback\">falling back to the org-mode block default "
            "(not present in settings.yaml)</span>"
            if g["falls_back_to_block_default"] else "explicitly configured"
        )
        if editable:
            default_control = (
                f"<form class=\"pf-set\" method=\"post\" action=\"/api/settings/privacy/policy\">"
                f"{csrf_field}{_hidden('action', 'set_default_policy')}{_hidden('group', g['key'])}"
                f"{_policy_select('policy', g['default_policy'], include_inherit=False)}"
                f"<button type=\"submit\">Save</button></form>"
            )
            cat_rows = "".join(
                f"<tr><td>{html.escape(c['label'])}</td><td>"
                f"<form class=\"pf-set\" method=\"post\" action=\"/api/settings/privacy/policy\">"
                f"{csrf_field}{_hidden('action', 'set_category_policy')}{_hidden('group', g['key'])}"
                f"{_hidden('category', c['key'])}"
                f"{_policy_select('policy', g['explicit_categories'].get(c['key']), include_inherit=True)}"
                f"<button type=\"submit\">Save</button></form>"
                f"</td><td>{html.escape(c['policy'])}</td></tr>"
                for c in g["categories"]
            )
            cat_table = (
                "<table><thead><tr><th>Category</th><th>Configured</th><th>In effect</th></tr></thead>"
                f"<tbody>{cat_rows}</tbody></table>" if cat_rows else ""
            )
        else:
            default_control = f"<strong>{html.escape(g['default_policy'])}</strong>"
            cat_rows = "".join(
                f"<tr><td>{html.escape(c['label'])}</td><td>{html.escape(c['policy'])}</td></tr>"
                for c in g["categories"]
            )
            cat_table = (
                f"<table><thead><tr><th>Category</th><th>Policy</th></tr></thead>"
                f"<tbody>{cat_rows}</tbody></table>" if cat_rows else ""
            )
        sections.append(
            f"<h3>{html.escape(g['label'])}</h3>"
            f"<p>Default policy: {default_control} -- {fallback_note}</p>"
            f"{cat_table}"
        )

    if editable:
        pii_master = (
            f"<form class=\"pf-set\" method=\"post\" action=\"/api/settings/privacy/pii\">"
            f"{csrf_field}{_hidden('action', 'toggle_pii_detection')}"
            f"{_hidden('enabled', 'false' if pii['enabled'] else 'true')}"
            f"<button type=\"submit\">{'Disable' if pii['enabled'] else 'Enable'}</button></form>"
        )
        pii_rows = "".join(
            f"<tr><td>{html.escape(c['label'])}</td>"
            f"<td>{'on' if c['enabled'] else 'off'}</td><td>"
            f"<form class=\"pf-set\" method=\"post\" action=\"/api/settings/privacy/pii\">"
            f"{csrf_field}{_hidden('action', 'toggle_pii_category')}{_hidden('category_key', c['key'])}"
            f"{_hidden('enabled', 'false' if c['enabled'] else 'true')}"
            f"<button type=\"submit\"{'' if pii['enabled'] else ' disabled'}>"
            f"{'Disable' if c['enabled'] else 'Enable'}</button></form></td></tr>"
            for c in pii["categories"]
        )
    else:
        pii_master = "<strong>on</strong>" if pii["enabled"] else "<strong>off</strong>"
        pii_rows = "".join(
            f"<tr><td>{html.escape(c['label'])}</td><td>{'on' if c['enabled'] else 'off'}</td></tr>"
            for c in pii["categories"]
        )
    pii_note = "" if pii["enabled"] else (
        "<p class=\"pf-note\">Individual categories are inert while PII detection is off.</p>"
    )

    intro = (
        "<p>This is the server's own <code>config/settings.yaml</code>. It is install-wide: "
        "there is no per-user override, and a change here applies to every principal "
        "immediately -- no daemon restart. See <a href=\"/settings\">Settings</a> for "
        "auto-accept rules and trusted resources, which are per-principal.</p>"
        if editable else
        "<p>Read-only: this daemon was started without a path to the "
        "<code>config/settings.yaml</code> these values came from, so there is nothing here "
        "to write back to. Change it on the server and restart the daemon. See "
        "<a href=\"/settings\">Settings</a> for auto-accept rules and trusted resources instead, "
        "which are per-principal.</p>"
    )
    return (
        "<h1>Install-wide privacy / PII policy</h1>"
        + intro
        + "<h2>PII detection</h2>"
        + f"<p>Detection is currently {'on' if pii['enabled'] else 'off'}: {pii_master}</p>"
        + (f"<table><tbody>{pii_rows}</tbody></table>" if pii_rows else "")
        + pii_note
        + "<h2>Privacy filter</h2>"
        + "".join(sections)
    )


def build_routes(
    *,
    sessions: OrgSessionStore,
    install_wide_settings: dict[str, Any],
    install_wide_settings_path: str = "",
) -> list[Route]:
    """``install_wide_settings_path`` is the resolved path of the *server's
    own* settings.yaml -- the file ``install_wide_settings`` was loaded
    from, threaded down from ``daemon_main.run_app``'s ``--config``. #400
    C3e's write routes need it and nothing else here does, so it defaults
    to empty: a caller that only wants the read surface (this module's own
    tests, a hand-built ``OrgAuth``) keeps working, and a policy write
    attempted without one is rejected with a 400 explaining exactly that
    rather than guessing at a path to overwrite.
    """
    def _current_principal(request: Request) -> Principal | None:
        return org_session.authenticated(request, sessions)

    def _ensure_principal_settings_loaded() -> None:
        # Lazy import -- daemon_main.py pulls in the whole connector/client
        # stack at module level, the same reason every other cross-module
        # call into it from web/*.py (e.g. settings_controller.py's own
        # telegram_submit_2fa) imports it inside the function that needs it
        # rather than at module scope.
        from ..daemon_main import _load_principal_settings

        _load_principal_settings(install_wide_config=install_wide_settings)

    async def settings_page(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/settings", status_code=302, headers={"Cache-Control": "no-store"})
        # _PrincipalScopeMiddleware already enters this scope for the whole
        # request in production (server.py's own _build_org_app) -- entering
        # it again here, around exactly the calls that read per-principal
        # registries, makes this module correct standalone too (its own
        # tests build these routes directly, with no middleware wrapping
        # them), the same explicit-scope pattern gate.py's propose_rule_
        # change and this module's own test fixtures already use.
        with principal_scope(principal):
            _ensure_principal_settings_loaded()
            cfg = auto_accept.get_current_config()
        session_id = request.cookies.get(org_session.SESSION_COOKIE, "")
        body = _render_settings_page(cfg, principal=principal, csrf=session_id)
        return HTMLResponse(
            _page(
                "Settings", body, nonce=_csp_nonce_for(request),
                principal_label=principal.email or principal.display_name or principal.id,
            ),
            headers={"Cache-Control": "no-store"},
        )

    async def privacy_page(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse(
                "/login?next=/settings/privacy", status_code=302, headers={"Cache-Control": "no-store"},
            )
        if not principal.is_admin:
            return PlainTextResponse("Forbidden -- administrator access required.", status_code=403)
        body = _render_privacy_page(
            _privacy_policy_view(install_wide_settings), _pii_view(install_wide_settings),
            # A reachable admin page is an editable one: every control it
            # draws posts an action `is_action_permitted` gates on
            # `principal.is_admin` anyway, so rendering them read-only for
            # someone who just passed that same check would only hide
            # what they are allowed to do.
            editable=bool(install_wide_settings_path),
            csrf=request.cookies.get(org_session.SESSION_COOKIE, ""),
        )
        return HTMLResponse(
            _page(
                "Privacy policy", body, nonce=_csp_nonce_for(request),
                principal_label=principal.email or principal.display_name or principal.id,
            ),
            headers={"Cache-Control": "no-store"},
        )

    async def add_rule(request: Request) -> Response:
        """The counterpart of ``remove_rule`` below, wiring up
        ``add_rule_row`` -- previously stuck in ``org_settings_scope.
        PER_PRINCIPAL_ACTIONS_UNROUTED`` because nothing on this end
        consumed it (see that module's docstring). Same shape as
        ``remove_rule``: authenticate, CSRF, origin, ``is_action_permitted``,
        act scoped to ``current_principal()``, audit. ``rule_choice`` (an
        ``"{op_key}|{rule_name}"`` pair, the ``_add_rule_form_html`` select's
        own option values) is validated against ``RULES_BY_OPERATION`` --
        the same fixed menu local mode's own picker constrains its dropdown
        to -- so this route can never hand ``auto_accept.add_auto_accept_
        rule`` a rule name the evaluator wouldn't recognize for that
        operation.
        """
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/settings", status_code=302, headers={"Cache-Control": "no-store"})
        form = await request.form()
        if not org_session.check_csrf(request, form.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not org_session.check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        if not is_action_permitted("add_rule_row", principal):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        op_key, _, rule_name = str(form.get("rule_choice", "")).partition("|")
        if rule_name not in RULES_BY_OPERATION.get(op_key, ()):
            return JSONResponse({"error": "unknown operation/rule type"}, status_code=400)
        value = _parse_rule_value_field(rule_name, str(form.get("value", "")))
        with principal_scope(principal):
            _ensure_principal_settings_loaded()
            auto_accept.add_auto_accept_rule(op_key, rule_name, value)
            _record_settings_audit(
                principal, f"Added auto-accept rule {rule_name!r} for {op_key!r} (principal={principal.id})",
            )
        return RedirectResponse("/settings", status_code=303, headers={"Cache-Control": "no-store"})

    async def remove_rule(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/settings", status_code=302, headers={"Cache-Control": "no-store"})
        form = await request.form()
        if not org_session.check_csrf(request, form.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not org_session.check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        if not is_action_permitted("remove_rule_row", principal):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        op_key = str(form.get("op_key", ""))
        rule_name = str(form.get("rule", ""))
        try:
            value = json.loads(str(form.get("value", "null")))
        except json.JSONDecodeError:
            return JSONResponse({"error": "malformed value"}, status_code=400)
        with principal_scope(principal):
            _ensure_principal_settings_loaded()
            removed = auto_accept.remove_auto_accept_rule(op_key, rule_name, value)
            if removed:
                _record_settings_audit(
                    principal, f"Removed auto-accept rule {rule_name!r} for {op_key!r} "
                    f"(principal={principal.id})",
                )
        return RedirectResponse("/settings", status_code=303, headers={"Cache-Control": "no-store"})

    async def remove_grant(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/settings", status_code=302, headers={"Cache-Control": "no-store"})
        form = await request.form()
        if not org_session.check_csrf(request, form.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not org_session.check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        if not is_action_permitted("remove_grant_row", principal):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        connector = str(form.get("connector", ""))
        config_key = str(form.get("config_key", ""))
        resource_id = str(form.get("resource_id", ""))
        tab = str(form.get("tab", "")) or None
        rt = resource_grants.resource_type(connector, config_key)
        if rt is None:
            return JSONResponse({"error": "unknown resource type"}, status_code=404)
        with principal_scope(principal):
            _ensure_principal_settings_loaded()
            removed = auto_accept.mutate_grants(
                lambda cfg: resource_grants.apply_grant_removal(cfg, rt, resource_id, tab)
            )
            if removed:
                _record_settings_audit(
                    principal, f"Removed trusted {rt.singular} {resource_id!r} from {rt.label} "
                    f"(principal={principal.id})",
                )
        return RedirectResponse("/settings", status_code=303, headers={"Cache-Control": "no-store"})

    async def _apply_install_wide(request: Request, allowed: frozenset[str]) -> Response:
        """The shared body of both install-wide write routes (#400 C3e).

        The gate order matters and mirrors ``remove_rule``/``remove_grant``
        above exactly: authenticated, then CSRF, then origin, then
        authorization. ``is_action_permitted`` is the only thing here that
        consults ``principal.is_admin`` -- the page's own 403 above governs
        *rendering*, this governs *doing*, and a route that trusted the
        former would be one hand-written POST away from letting any
        signed-in principal rewrite the whole org's privacy policy.

        ``allowed`` narrows which actions this particular route will apply,
        so the form field naming the action can't be swapped for one of the
        other route's: a value outside it 404s the same way
        ``routes_settings.py``'s own allowlist check does for an unknown
        action name.
        """
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse(
                "/login?next=/settings/privacy", status_code=302, headers={"Cache-Control": "no-store"},
            )
        form = await request.form()
        csrf = form.get("csrf")
        if not org_session.check_csrf(request, csrf if isinstance(csrf, str) else None):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not org_session.check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        action = str(form.get("action", ""))
        if action not in allowed:
            return JSONResponse({"error": "unknown action"}, status_code=404)
        if not is_action_permitted(action, principal):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        # Text fields only: a multipart part carrying a file has no meaning
        # on this endpoint, and dropping it here keeps `org_install_policy`
        # a pure dict-of-strings consumer rather than one that has to know
        # what an UploadFile is.
        payload = {key: value for key, value in form.items() if key != "csrf" and isinstance(value, str)}
        try:
            summary = org_install_policy.apply_change(
                install_wide_settings, install_wide_settings_path, action=action, payload=payload,
            )
        except org_install_policy.PolicyChangeRejected as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            # The settings.yaml write itself failed -- nothing was applied
            # (see apply_change's own docstring), so this is a 500 with the
            # policy unchanged, not a partially-applied change.
            logger.error("Could not write the install-wide settings.yaml: %s", exc)
            return JSONResponse({"error": "could not write settings.yaml"}, status_code=500)
        with principal_scope(principal):
            _record_settings_audit(principal, f"{summary} (admin={principal.id})")
        return RedirectResponse("/settings/privacy", status_code=303, headers={"Cache-Control": "no-store"})

    async def set_privacy_policy(request: Request) -> Response:
        return await _apply_install_wide(request, frozenset({"set_default_policy", "set_category_policy"}))

    async def set_pii_policy(request: Request) -> Response:
        return await _apply_install_wide(request, frozenset({"toggle_pii_detection", "toggle_pii_category"}))

    return [
        Route("/settings", settings_page),
        Route("/settings/privacy", privacy_page),
        Route("/api/settings/rules/add", add_rule, methods=["POST"]),
        Route("/api/settings/rules/remove", remove_rule, methods=["POST"]),
        Route("/api/settings/grants/remove", remove_grant, methods=["POST"]),
        Route("/api/settings/privacy/policy", set_privacy_policy, methods=["POST"]),
        Route("/api/settings/privacy/pii", set_pii_policy, methods=["POST"]),
    ]


__all__ = ["build_routes"]
