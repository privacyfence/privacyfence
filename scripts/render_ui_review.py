#!/usr/bin/env python3
"""Render the app's UI for a human design review: ``render_ui_review.py --out DIR``.

Writes PNGs into DIR, plus an ``index.html`` that lays them out side by side:

- ``style-guide-<width>-<theme>.png``: every shared component (resources/design/base.css: the
  layout primitives, buttons, labels, focus) and every app component (resources/design/app.css:
  field, toggle, tabstrip, badge, card, panel, the status colours, and the shell's header) in each
  of its states -- default, hover, focus-visible, disabled, and the component's own (checked,
  selected, invalid). The style guide is ``style-guide.html``, a static page this script builds
  from the design files themselves; it is not a route the app serves. Hover and focus-visible are
  forced through the DevTools protocol on the elements that ask for them (``data-force``), so the
  screenshot shows the state rather than a description of it.
- ``<page>-<width>-<theme>.png`` for /approvals, one approval card of each kind, the PII
  confirmation and the choice dialog, every /settings section (an org admin's six, and local
  mode's Connectors), /connect (and its Telegram sign-in at each step), /security, and the
  fallback pages with no shell (no longer pending, preparing, not authorized, local mode's
  /security), from
  tests/integration/ui_review_capture.py, which this script runs with pytest so the pages come
  from the same in-process servers the browser tests use.

Widths are 393 px (the phone emulation of tests/integration/test_browser_smoke.py) and 1280 px;
themes are light and dark. Synthetic data only. Needs the test extra and Chromium
(``pip install -e '.[test]'``; see docs/testing-policy.md). Output is a review artifact, never a
test: nothing here passes or fails on how the pages look.
"""
from __future__ import annotations

import argparse
import html
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CAPTURE = REPO / "tests" / "integration" / "ui_review_capture.py"
WIDTHS = {"393": {"width": 393, "height": 852, "mobile": True}, "1280": {"width": 1280, "height": 900, "mobile": False}}
SCHEMES = ("light", "dark")


# ---- The style guide ----------------------------------------------------------------------

def _states(label: str, variants: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<figure class="sg-state"><div class="sg-sample">{markup}</div><figcaption>{html.escape(name)}</figcaption></figure>'
        for name, markup in variants
    )
    return f'<section class="stack sg-section"><h3 class="kicker">{html.escape(label)}</h3><div class="cluster sg-row">{cells}</div></section>'


def style_guide_html() -> str:
    sys.path.insert(0, str(REPO / "src"))
    from privacyfence import web_shell  # noqa: PLC0415 -- needs src/ on the path first
    from privacyfence.design_css import DOCUMENT_CSS  # noqa: PLC0415

    def button(cls: str, text: str, extra: str = "") -> str:
        return f'<button type="button" class="button {cls}"{extra}>{text}</button>'

    def field(tag: str, extra: str = "", value: str = "alice@example.com") -> str:
        if tag == "select":
            return f'<select class="field"{extra}><option>Standard</option><option>Detailed</option></select>'
        if tag == "textarea":
            return f'<textarea class="field"{extra}>A longer note, over two lines.</textarea>'
        return f'<input class="field" type="text" value="{value}" aria-label="Email"{extra}>'

    def toggle(checked: bool, extra: str = "", label: str = "Notify me") -> str:
        c = " checked" if checked else ""
        return (f'<label class="toggle"><input type="checkbox" role="switch"{c}{extra}>'
                f'<span class="toggle-track"></span><span>{label}</span></label>')

    tabs = ('<div class="tabstrip" role="tablist">'
            '<button class="tab" role="tab" aria-selected="true">General</button>'
            '<button class="tab" role="tab" aria-selected="false" data-force="hover">Auto-accept (hover)</button>'
            '<button class="tab" role="tab" aria-selected="false" data-force="focus-visible">Privacy (focus)</button>'
            '<button class="tab" role="tab" aria-selected="false" aria-disabled="true">Agents (disabled)</button>'
            '<button class="tab" role="tab" aria-selected="false">Audit log</button></div>')

    sections = [
        _states("Button: primary", [
            ("default", button("primary", "Allow once")), ("hover", button("primary", "Allow once", ' data-force="hover"')),
            ("focus-visible", button("primary", "Allow once", ' data-force="focus-visible"')),
            ("disabled", button("primary", "Allow once", " disabled"))]),
        _states("Button: secondary", [
            ("default", button("secondary", "Cancel")), ("hover", button("secondary", "Cancel", ' data-force="hover"')),
            ("focus-visible", button("secondary", "Cancel", ' data-force="focus-visible"')),
            ("disabled", button("secondary", "Cancel", " disabled"))]),
        _states("Button: danger (outlined, never filled)", [
            ("default", button("danger", "Deny")), ("hover", button("danger", "Deny", ' data-force="hover"')),
            ("focus-visible", button("danger", "Deny", ' data-force="focus-visible"')),
            ("disabled", button("danger", "Deny", " disabled"))]),
        _states("Field: input", [
            ("default", field("input")), ("hover", field("input", ' data-force="hover"')),
            ("focus-visible", field("input", ' data-force="focus-visible"')), ("disabled", field("input", " disabled")),
            ("invalid", field("input", ' aria-invalid="true"', "alice@")),
            ("placeholder", '<input class="field" type="text" placeholder="name@example.com" aria-label="Email">')]),
        _states("Field: select and textarea", [
            ("select", field("select")), ("select, focus-visible", field("select", ' data-force="focus-visible"')),
            ("select, disabled", field("select", " disabled")), ("textarea", field("textarea")),
            ("textarea, disabled", field("textarea", " disabled"))]),
        _states("Field with label and help", [(
            "label + help", '<label><span class="field-label">Work email</span>' + field("input")
            + '<div class="field-help">Where approval notifications go.</div></label>')]),
        _states("Toggle", [
            ("off", toggle(False)), ("on", toggle(True)), ("hover", toggle(False).replace('class="toggle"', 'class="toggle" data-force="hover"')),
            ("focus-visible", toggle(True, ' data-force="focus-visible"')),
            ("disabled, off", toggle(False, " disabled")), ("disabled, on", toggle(True, " disabled"))]),
        _states("Tabstrip (selected, hover, focus-visible, disabled)", [("", tabs)]),
        _states("Badge", [
            ("neutral", '<span class="badge">Not configured</span>'),
            ("accent", '<span class="badge badge-accent">Read</span>'),
            ("warning", '<span class="badge badge-warning">Write</span>'),
            ("danger", '<span class="badge badge-danger">PII detected</span>'),
            ("info", '<span class="badge badge-info">Step-up required</span>'),
            ("success", '<span class="badge badge-success">Connected</span>'),
            ("solid", '<span class="badge badge-solid">Verified</span>'),
            ("dashed", '<span class="badge badge-dashed">Not verified</span>')]),
        _states("Card", [
            ("default", '<div class="card stack"><p class="kicker">Gmail</p><p>Quarterly numbers from Alice</p></div>'),
            ("interactive, hover", '<a href="#" class="card card-interactive" data-force="hover">Open the request</a>'),
            ("disabled", '<div class="card" aria-disabled="true">No longer pending</div>'),
            ("danger", '<div class="card card-danger"><strong>⚠️ Review carefully before approving</strong></div>'),
            ("warning", '<div class="card card-warning"><strong>⚠️ This message appears to contain</strong></div>'),
            ("info", '<div class="card card-info">Step-up required: confirm with your passkey.</div>'),
            ("success", '<div class="card card-success">✓ Passkey added.</div>')]),
        _states("Panel", [
            ("panel", '<div class="panel stack" style="width: min(340px, 80vw)"><p class="kicker">Privacy Filter</p><p>Panels group cards or a section.</p>'
                      '<div class="card">A card inside a panel</div></div>'),
            ("panel-soft", '<div class="panel panel-soft">A recessed panel</div>')]),
        _states("Labels", [
            ("kicker", '<p class="kicker">What will be provided</p>'),
            ("eyebrow", '<span class="eyebrow">Local-first</span>')]),
        _states("Colour tokens", [(f"--{t}", f'<span class="sg-swatch" style="background:var(--{t})"></span>') for t in (
            "bg", "surface", "surface-soft", "line", "control-line", "ink", "ink-soft", "muted", "accent",
            "accent-dark", "accent-soft", "danger", "danger-soft", "warning", "warning-soft", "info", "info-soft",
            "success", "success-soft", "focus-ring")]),
        '<section class="stack sg-section"><h3 class="kicker">Primitives: .split, .grid-auto, .cluster</h3>'
        '<div class="split" style="--split-gap: 24px"><div class="card">.split, first</div><div class="card">.split, second</div></div>'
        '<div class="grid-auto" style="--min: 140px"><div class="card">grid</div><div class="card">auto</div>'
        '<div class="card">columns</div><div class="card">that wrap</div></div></section>',
    ]
    header = web_shell.header_html(
        "approvals", web_shell.ORG_NAV_ITEMS,
        live_html=('<div class="pf-shell-live"><span class="pf-shell-live-dot live"></span>'
                   '<span class="pf-shell-live-label">live</span></div>'),
        principal_label="carol@example.com",
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>PrivacyFence app style guide</title>
<style>{DOCUMENT_CSS}{web_shell._SHELL_CSS}
.sg-page {{ padding-block: var(--space-m) var(--space-xl); --stack-gap: var(--space-l); }}
.sg-page h1 {{ font-size: 28px; margin: 0; }}
.sg-page p.lede {{ color: var(--ink-soft); max-width: var(--measure); }}
.sg-section {{ --stack-gap: var(--space-xs); }}
.sg-row {{ --cluster-gap: var(--space-s); align-items: flex-start; }}
.sg-state {{ margin: 0; display: grid; gap: 6px; }}
.sg-state figcaption {{ font-size: 12px; color: var(--muted); }}
.sg-sample {{ min-width: 150px; }}
.sg-swatch {{ display: block; width: 64px; height: 40px; border-radius: var(--radius-s); border: 1px solid var(--line); }}
</style></head>
<body>
{header}
<main class="shell stack sg-page">
<div class="stack" style="--stack-gap: 8px"><p class="eyebrow">Style guide</p><h1>PrivacyFence app components</h1>
<p class="lede">The shared design files (tokens.css, base.css) and the app layer (app.css), in every state. Built by
scripts/render_ui_review.py from the files in src/privacyfence/resources/design/.</p></div>
{"".join(sections)}
</main>
</body></html>
"""


def render_style_guide(out: Path) -> list[str]:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 -- test extra

    guide = out / "style-guide.html"
    guide.write_text(style_guide_html(), encoding="utf-8")
    written = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, size in WIDTHS.items():
            for scheme in SCHEMES:
                ctx = browser.new_context(
                    viewport={"width": size["width"], "height": size["height"]}, color_scheme=scheme,
                    is_mobile=size["mobile"], has_touch=size["mobile"], device_scale_factor=2,
                )
                page = ctx.new_page()
                page.goto(guide.as_uri())
                page.wait_for_load_state("load")
                _force_states(page)
                target = out / f"style-guide-{name}-{scheme}.png"
                page.screenshot(path=str(target), full_page=True)
                written.append(target.name)
                ctx.close()
        browser.close()
    return written


def _force_states(page) -> None:
    """Hold every ``data-force`` element in the pseudo-class it names (hover, focus-visible)."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("DOM.enable")
    cdp.send("CSS.enable")
    root = cdp.send("DOM.getDocument", {"depth": -1})["root"]["nodeId"]
    for state in ("hover", "focus-visible"):
        nodes = cdp.send("DOM.querySelectorAll", {"nodeId": root, "selector": f'[data-force="{state}"]'})["nodeIds"]
        for node in nodes:
            # A focus-visible input also needs :focus for its own :focus-visible rules to match.
            forced = ["focus", "focus-visible"] if state == "focus-visible" else [state]
            cdp.send("CSS.forcePseudoState", {"nodeId": node, "forcedPseudoClasses": forced})


# ---- The pages ------------------------------------------------------------------------------

def render_pages(out: Path) -> int:
    env = {**os.environ, "PF_UI_REVIEW_OUT": str(out)}
    command = [sys.executable, "-m", "pytest", str(CAPTURE), "-q", "-p", "no:cacheprovider", "-o", "addopts="]
    return subprocess.run(command, cwd=REPO, env=env, check=False).returncode


def write_index(out: Path) -> None:
    images = sorted(p.name for p in out.glob("*.png"))
    groups: dict[str, list[str]] = {}
    for name in images:
        stem = name.removesuffix(".png").rsplit("-", 2)[0]
        groups.setdefault(stem, []).append(name)
    body = "".join(
        f"<section><h2>{html.escape(stem)}</h2><div class=row>"
        + "".join(f'<figure><a href="{n}"><img src="{n}" alt="{html.escape(n)}"></a><figcaption>{html.escape(n)}'
                  "</figcaption></figure>" for n in names)
        + "</div></section>"
        for stem, names in groups.items()
    )
    (out / "index.html").write_text(
        "<!DOCTYPE html><meta charset=utf-8><title>UI review</title><style>body{font:14px system-ui;margin:24px}"
        ".row{display:flex;gap:16px;align-items:flex-start;overflow-x:auto}figure{margin:0}"
        "img{max-width:420px;max-height:900px;border:1px solid #ccc}figcaption{font-size:12px}</style>"
        f"<h1>UI review</h1>{body}",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--out", required=True, type=Path, help="directory to write the PNGs into")
    parser.add_argument("--style-guide-only", action="store_true", help="skip the product pages")
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    written = render_style_guide(out)
    print(f"style guide: {len(written)} images")
    status = 0 if args.style_guide_only else render_pages(out)
    write_index(out)
    print(f"{len(list(out.glob('*.png')))} images in {out} (index.html lays them out)")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
