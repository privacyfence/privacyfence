"""Org-mode settings page rendering (#400, PSC-4b).

Moved verbatim out of the former `web/routes_org_settings.py` when PSC-4b
folded that module's routes into `web/routes_settings.py`'s single settings
dispatcher (`build_org_routes`) -- this module is rendering only, with no
auth/CSRF/dispatch concern of its own, the same split PSC-5 is expected to
carry further once local and org mode share one renderer too. Every
function here is unedited from its pre-merge form; only this docstring and
the imports are new.
"""
from __future__ import annotations

import html
import logging
from typing import Any

from .. import pii_detector
from .. import web_shell
from ..policy import catalogue as policy_catalogue
from ..policy import describe as policy_describe
from ..principal import Principal
from ..privacy_filter import VALID_POLICIES, PrivacyFilterConfigError
from ..privacy_filter import _parse_group as _parse_privacy_group
from ..settings_controller import PRIVACY_CATEGORY_LABELS, PRIVACY_GROUP_LABELS

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


def _rule_rows(rules: list) -> list[dict[str, Any]]:
    """One row per v2 rule -- the same fields local mode's Auto-accept page renders
    (``settings_controller.SettingsController._auto_accept_state``), minus the resolved-display-name
    machinery (a web-page-only affordance whose caches are keyed per this page's own connectors,
    which org mode's stateless-per-request rendering has no equivalent of)."""
    rows: list[dict[str, Any]] = []
    for rule in sorted(rules, key=policy_describe.rule_sentence):
        rows.append({
            "id": rule.id,
            "sentence": policy_describe.rule_sentence(rule),
            "covered_tools": policy_describe.covered_tools(rule),
        })
    return rows


def _parse_value_field(raw_text: str) -> list[str] | None:
    """The "value" field (a comma-separated list of resource ids/keys/domains) -> a v2 rule value,
    or ``None`` for a value-less scope's empty field -- same shape
    ``settings_controller._parse_value_list`` uses for the identical form on local mode's page."""
    values = [v.strip() for v in (raw_text or "").split(",") if v.strip()]
    return values or None


def _add_rule_form_html(csrf_esc: str) -> str:
    # One <select> whose options are (scope group, verb) pairs rather than a separate verb picker
    # -- this page is plain server-rendered forms with no JS (see module docstring: "reuse
    # routes_approvals.py's minimal doctype+tokens.css shell"), so there is no client-side way
    # to filter a second <select>'s options by a first one's choice, the same reason the v1-era
    # version of this form combined operation and rule name into one option value. Adding more than
    # one verb to the same scope is a second submission, same as adding a second scope value is.
    optgroups: dict[str, list[str]] = {}
    for entry in policy_catalogue.scope_catalogue():
        options = "".join(
            f"<option value=\"{html.escape(entry['id'], quote=True)}|{html.escape(verb, quote=True)}\">"
            f"{html.escape(entry['label'])} — allow {html.escape(verb)}</option>"
            for verb in entry["verbs"]
        )
        optgroups.setdefault(entry["connector"], []).append(options)
    grouped = "".join(
        f"<optgroup label=\"{html.escape(connector.replace('_', ' ').title(), quote=True)}\">"
        f"{''.join(options)}</optgroup>"
        for connector, options in sorted(optgroups.items())
    )
    return (
        "<form class=\"pf-set\" method=\"post\" action=\"/api/settings/rules/add\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf_esc}\">"
        "<select name=\"rule_choice\" required aria-label=\"Scope and verb\">"
        "<option value=\"\">Select a scope and verb…</option>"
        + grouped +
        "</select> "
        "<input type=\"text\" name=\"value\" "
        "placeholder=\"Value -- comma-separated list, or leave blank for a value-less scope\" "
        "aria-label=\"Rule value\"> "
        "<button type=\"submit\">Add rule</button>"
        "</form>"
    )


def _render_settings_page(rules: list, *, principal: Principal, csrf: str) -> str:
    rule_rows = _rule_rows(rules)
    csrf_esc = html.escape(csrf, quote=True)

    rules_html = "<p class=\"pf-empty\">No auto-accept rules configured.</p>"
    if rule_rows:
        body_rows = "".join(
            f"<tr><td>{html.escape(r['sentence'])}</td>"
            f"<td>{len(r['covered_tools'])} tool{'' if len(r['covered_tools']) == 1 else 's'}</td><td>"
            f"<form class=\"pf-remove\" method=\"post\" action=\"/api/settings/rules/remove\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf_esc}\">"
            f"<input type=\"hidden\" name=\"rule_id\" value=\"{html.escape(r['id'], quote=True)}\">"
            f"<button class=\"pf-remove\" type=\"submit\">Remove</button></form></td></tr>"
            for r in rule_rows
        )
        rules_html = (
            "<table><thead><tr><th>Rule</th><th>Unblocks</th><th></th></tr></thead>"
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
        "auto-accept rules, which are per-principal.</p>"
        if editable else
        "<p>Read-only: this daemon was started without a path to the "
        "<code>config/settings.yaml</code> these values came from, so there is nothing here "
        "to write back to. Change it on the server and restart the daemon. See "
        "<a href=\"/settings\">Settings</a> for auto-accept rules instead, "
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


__all__ = ["_page", "_render_settings_page", "_render_privacy_page", "_privacy_policy_view", "_pii_view"]
