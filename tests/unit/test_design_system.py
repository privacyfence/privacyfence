"""The app and the website share one design system (ADR 0078): tokens and primitives live in
``src/privacyfence/resources/design/`` and nowhere else, and renderers compose them instead of
writing their own breakpoints and colours.

Three source checks, each with an allow-list seeded with what the code had when the checks
arrived. Every entry names the phase of docs/org-mode-mobile-plan.md that removes it, and an entry
that no longer matches anything fails too, so an allow-list only ever shrinks:

(a) no ``@media`` width/height query in a renderer or in resources/approval_window/styles.css
    (``prefers-color-scheme`` and ``prefers-reduced-motion`` are fine): the app lays out with the
    shared primitives and container queries;
(b) no hex, ``rgb()``/``rgba()`` or ``hsl()``/``hsla()`` colour literal in those files: colours
    are tokens;
(c) no CSS custom property that could be a token is defined outside resources/design/: a page
    may set the knobs the shared primitives expose (``--split-cols`` and the like), nothing else.

The browser half of the shared rules (no sideways scroll, 44 px targets, ...) is
tests/integration/test_browser_smoke.py's ``TestPhoneLayout``, and on the website
tests/integration/test_website_layout.py.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from privacyfence import design_css

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "privacyfence"
DESIGN = SRC / "resources" / "design"

# A renderer is a module under src/privacyfence that writes a page's markup and styles: it has a
# <style> element, a doctype or an inline style attribute. These match that test and are not one.
NOT_RENDERERS = {
    "design_css.py": "loads the shared design files for the renderers",
    "email_markdown.py": "renders the body of an outgoing email, not a PrivacyFence page",
    "web/csp.py": "the Content-Security-Policy; it names <style> in prose only",
}
_RENDERER_MARKERS = re.compile(r'<style|<!DOCTYPE|style=\\?"')


def _renderers() -> list[Path]:
    found = [
        path for path in SRC.rglob("*.py")
        if _RENDERER_MARKERS.search(path.read_text(encoding="utf-8"))
        and path.relative_to(SRC).as_posix() not in NOT_RENDERERS
    ]
    return sorted(found) + [SRC / "resources" / "approval_window" / "styles.css"]


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _code(path: Path) -> str:
    """The file without its comments: CSS and HTML comments, and Python comment lines."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    if path.suffix == ".py":
        text = "\n".join("" if line.lstrip().startswith("#") else line for line in text.splitlines())
    return text


# ---- (a) no viewport @media queries in renderers --------------------------------------------

_WIDTH_MEDIA = re.compile(
    r"@media\b[^{]*?\b(?:min-|max-)?(?:device-)?(?:width|height|aspect-ratio|orientation)\b"
)

# file -> (number of width/height @media queries it may still have, phase that removes them)
MEDIA_ALLOWED: dict[str, tuple[int, str]] = {
    "src/privacyfence/approval_list_html.py": (1, "p6-remaining-pages"),
    "src/privacyfence/approval_window_html.py": (1, "p5-card-containers"),
    "src/privacyfence/dialog_window_html.py": (1, "p5-card-containers"),
    "src/privacyfence/resources/approval_window/styles.css": (2, "p5-card-containers"),
    "src/privacyfence/web_shell.py": (1, "p2-design-system"),
}


# ---- (b) no colour literals in renderers ----------------------------------------------------

_HEX = re.compile(r"(?<![&\w])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])")
_COLOUR_FUNCTION = re.compile(r"\b(?:rgba?|hsla?)\(")

# file -> (number of colour literals it may still have, phase that removes them)
COLOUR_ALLOWED: dict[str, tuple[int, str]] = {
    "src/privacyfence/approval_list_html.py": (3, "p6-remaining-pages"),
    "src/privacyfence/resources/approval_window/styles.css": (72, "p5-card-containers"),
    "src/privacyfence/settings_window_html.py": (29, "p3-settings"),
    "src/privacyfence/web/routes_connect.py": (14, "p6-remaining-pages"),
    "src/privacyfence/web/routes_security.py": (15, "p6-remaining-pages"),
    "src/privacyfence/web/session_auth.py": (1, "p6-remaining-pages"),
    "src/privacyfence/web_shell.py": (6, "p2-design-system"),
}


def _count(pattern_count, path: Path) -> int:
    return pattern_count(_code(path))


def _media_count(text: str) -> int:
    return len(_WIDTH_MEDIA.findall(text))


def _colour_count(text: str) -> int:
    return len(_HEX.findall(text)) + len(_COLOUR_FUNCTION.findall(text))


@pytest.mark.parametrize(
    ("counter", "allowed", "what"),
    [
        (_media_count, MEDIA_ALLOWED, "@media width/height queries"),
        (_colour_count, COLOUR_ALLOWED, "colour literals"),
    ],
    ids=["media-queries", "colour-literals"],
)
def test_renderers_use_the_shared_primitives_and_tokens(counter, allowed, what):
    renderers = _renderers()
    counts = {_rel(path): _count(counter, path) for path in renderers}
    over = {
        name: f"{count} {what}, {allowed.get(name, (0, ''))[0]} allowed"
        for name, count in counts.items() if count > allowed.get(name, (0, ""))[0]
    }
    assert not over, (
        f"Renderers with {what} (use the shared tokens and primitives in "
        f"src/privacyfence/resources/design/, and container queries, instead): {over}"
    )
    # The allow-list only shrinks: an entry above what its file has now must be lowered (or
    # removed at zero) by the change that fixed it.
    stale = {
        name: f"allows {limit}, has {counts.get(name, 0)} ({phase})"
        for name, (limit, phase) in allowed.items() if counts.get(name, 0) != limit
    }
    assert not stale, f"Lower these allow-list entries to what the file has now: {stale}"


def test_every_allow_list_entry_names_a_phase_and_a_renderer():
    renderers = {_rel(path) for path in _renderers()}
    for allowed in (MEDIA_ALLOWED, COLOUR_ALLOWED):
        for name, (_limit, phase) in allowed.items():
            assert name in renderers, name
            assert re.fullmatch(r"p[1-8]-[a-z-]+", phase), phase
    for (name, _pattern), phase in TOKEN_DEFINITIONS_ALLOWED.items():
        assert (REPO / name).is_file(), name
        assert re.fullmatch(r"p[1-8]-[a-z-]+", phase), phase


def test_the_check_sees_what_it_is_meant_to():
    # The patterns themselves, on the shapes they must and must not catch.
    assert _media_count("@media (max-width: 700px) {") == 1
    assert _media_count("@media screen and (min-width:560px){") == 1
    assert _media_count("@media (prefers-color-scheme: dark) {") == 0
    assert _media_count("@media (prefers-reduced-motion: reduce) {") == 0
    assert _colour_count("color:#fff;background:#1d1d1f;border:1px solid rgba(0,0,0,.1)") == 3
    assert _colour_count('href="#pf-content" &#8230; #add-passkey #fade-in') == 0
    assert _colour_count("color: var(--ink); color-mix(in srgb, var(--ink) 16%, transparent)") == 0


# ---- (c) tokens are defined in resources/design/ only ---------------------------------------

_DEFINITION = re.compile(r"(?<![\w-])--([a-zA-Z][\w-]*)\s*:")


def _primitive_knobs() -> set[str]:
    """The custom properties base.css's primitives read with a fallback -- ``var(--min, 260px)``
    -- which is what makes them a knob a page sets, not a token."""
    return set(re.findall(r"var\(--([\w-]+)\s*,", design_css.BASE_CSS))


# Properties another tool owns, which it documents and reads itself.
FOREIGN_PROPERTIES = re.compile(r"md-[\w-]+")  # the /docs/ generator's theme (website/_docs/)

# (file, property-name pattern) -> phase that removes the definitions.
TOKEN_DEFINITIONS_ALLOWED: dict[tuple[str, str], str] = {
    ("src/privacyfence/resources/tokens.css", r"(color|space|radius)-[\w-]+"): "p2-design-system",
    (
        "src/privacyfence/resources/approval_window/styles.css",
        r"(color|space|radius|font)-[\w-]+",
    ): "p2-design-system",
    ("src/privacyfence/resources/approval_window/styles.css", r"pii-w-[\w-]+"): "p5-card-containers",
    ("src/privacyfence/settings_window_html.py", r"pf-[\w-]+"): "p3-settings",
}


def _styled_files() -> list[Path]:
    website = REPO / "website"
    css = [path for path in SRC.rglob("*.css") if DESIGN not in path.parents]
    return sorted(set(_renderers()) | set(css) | set(website.rglob("*.css")))


def test_design_tokens_are_defined_only_in_resources_design():
    knobs = _primitive_knobs()
    assert {"min", "split-cols", "stack-gap", "cluster-gap"} <= knobs, knobs
    offenders: dict[str, set[str]] = {}
    used: set[tuple[str, str]] = set()
    for path in _styled_files():
        name = _rel(path)
        for prop in set(_DEFINITION.findall(_code(path))):
            if prop in knobs or FOREIGN_PROPERTIES.fullmatch(prop):
                continue
            entry = next(
                (key for key in TOKEN_DEFINITIONS_ALLOWED if key[0] == name and re.fullmatch(key[1], prop)),
                None,
            )
            if entry is not None:
                used.add(entry)
                continue
            offenders.setdefault(name, set()).add(f"--{prop}")
    assert not offenders, (
        "Custom properties defined outside src/privacyfence/resources/design/ (define the token "
        f"there, or use one that exists): { {k: sorted(v) for k, v in offenders.items()} }"
    )
    unused = {key: phase for key, phase in TOKEN_DEFINITIONS_ALLOWED.items() if key not in used}
    assert not unused, f"Remove these allow-list entries, nothing matches them any more: {unused}"


def test_every_token_is_defined_once():
    names = _DEFINITION.findall(design_css.TOKENS_CSS)
    assert len(names) == len(set(names))
    # And the shared primitives use no colour literal and no viewport query (tokens.css is where
    # colour values live).
    base = _code(DESIGN / "base.css")
    assert _colour_count(base) == 0
    assert _media_count(base) == 0


# ---- One physical source, shipped and used by both ------------------------------------------

def test_the_website_publishes_the_packaged_design_files():
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    import build_site  # noqa: PLC0415 -- scripts/ is not a package

    assert build_site.STATIC["tokens.css"] == "src/privacyfence/resources/design/tokens.css"
    assert build_site.STATIC["base.css"] == "src/privacyfence/resources/design/base.css"
    assert not (REPO / "website" / "tokens.css").exists()


def test_the_wheel_ships_the_design_files():
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "resources/design/*.css" in pyproject["tool"]["setuptools"]["package-data"]["privacyfence"]


def test_every_app_document_inlines_the_shared_css_before_its_own():
    from privacyfence import dialog_window_html, settings_window_html, web_shell
    from privacyfence.card_builder import build_card_html

    documents = {
        "web_shell": web_shell.wrap("<p>x</p>", title="t", active="approvals", nonce="n"),
        "settings": settings_window_html.build_html({}, nonce="n"),
        "card": build_card_html(
            title="Send email", preview={"To": "a@b.com"}, details_text="body", is_read=False, layout="narrow",
        ),
        "dialog": dialog_window_html.build_confirmation_html(
            title="t", message_lines=["m"], cancel_label="Cancel", confirm_label="Proceed",
        ),
    }
    for name, html in documents.items():
        style = html.split('<style nonce="', 1)[1].split('">', 1)[1]
        assert style.lstrip().startswith(design_css.SHARED_CSS), name
