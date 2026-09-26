"""Every text/background and UI-boundary token pair the app uses meets WCAG 2.2 AA, in both
themes (ADR 0079).

The light theme is ``resources/design/tokens.css`` (the website's values) plus the app's own
tokens in ``app.css``; the dark theme is the same with ``app.css``'s
``@media (prefers-color-scheme: dark)`` block applied on top. Contrast is computed from the token
files themselves, not from rendered pages, so a pair that fails here fails before any page is
drawn with it.

Thresholds are WCAG's: 4.5:1 for text (1.4.3), 3:1 for large text (1.4.3) and for the edges of
controls and focus indicators (1.4.11). No token is used only for large text, so every text pair
is held to 4.5:1. ``--line`` is not in the boundary list: it is a decorative hairline between
things that are already distinguishable, which 1.4.11 does not cover; the edge of a control is
``--control-line``.
"""
from __future__ import annotations

import re

import pytest

from privacyfence import design_css

TEXT = 4.5
LARGE_TEXT = 3.0
BOUNDARY = 3.0

_BACKGROUNDS = ("bg", "surface", "surface-soft")

# (foreground, background, minimum ratio, where it is used)
PAIRS: list[tuple[str, str, float, str]] = [
    *[(fg, bg, TEXT, "body and secondary text, links, kickers and status text on every surface")
      for fg in ("ink", "ink-soft", "muted", "accent", "accent-dark", "danger", "warning", "info", "success")
      for bg in _BACKGROUNDS],
    ("accent-dark", "accent-soft", TEXT, "read pill, notice strip, accent badge"),
    ("accent-dark", "accent-wash", TEXT, ".eyebrow"),
    ("danger", "danger-soft", TEXT, "PII card, danger badge, deny hover"),
    ("warning", "warning-soft", TEXT, "write pill, content-flag card, warning badge"),
    ("info", "info-soft", TEXT, "info badge and card"),
    ("success", "success-soft", TEXT, "success badge and card"),
    ("ink", "danger-soft", TEXT, "body text in a danger card"),
    ("ink", "warning-soft", TEXT, "body text in a warning card"),
    ("on-ink", "ink", TEXT, "primary button, verified badge, toast"),
    ("on-ink", "ink-hover", TEXT, "primary button, hovered"),
    ("on-accent", "accent", TEXT, "accent-filled controls"),
    ("surface", "danger", TEXT, "the shell's passkey-required banner"),
    *[(edge, bg, BOUNDARY, "the edge of a field, toggle or outlined button; the focus ring")
      for edge in ("control-line", "focus-ring", "danger")
      for bg in _BACKGROUNDS],
    ("ink", "bg", BOUNDARY, "the primary button's fill against the page"),
    ("accent", "surface-soft", BOUNDARY, "a toggle that is on, against one that is off"),
]

_DECLARATION = re.compile(r"--([\w-]+)\s*:\s*([^;]+);")


def _block(css: str, start: int) -> str:
    """The text inside the braces that open at or after ``start``."""
    open_at = css.index("{", start)
    depth, i = 0, open_at
    while True:
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        if depth == 0:
            return css[open_at + 1:i]
        i += 1


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _declarations(css: str) -> dict[str, str]:
    return {name: value.strip() for name, value in _DECLARATION.findall(css)}


def _themes() -> dict[str, dict[str, str]]:
    tokens = _strip_comments(design_css.TOKENS_CSS)
    app = _strip_comments(design_css.APP_CSS)
    dark_at = app.index("@media (prefers-color-scheme: dark)")
    light = {**_declarations(_block(tokens, tokens.index(":root"))), **_declarations(_block(app, app.index(":root")))}
    dark_media = _block(app, dark_at)
    dark = {**light, **_declarations(_block(dark_media, dark_media.index(":root")))}
    return {"light": light, "dark": dark}


def _rgba(value: str, theme: dict[str, str]) -> tuple[float, float, float, float]:
    value = value.strip()
    ref = re.fullmatch(r"var\(--([\w-]+)\)", value)
    if ref:
        return _rgba(theme[ref.group(1)], theme)
    hexa = re.fullmatch(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})", value)
    if hexa:
        digits = hexa.group(1)
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return (*(int(digits[i:i + 2], 16) for i in (0, 2, 4)), 1.0)
    func = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", value)
    if func:
        r, g, b, a = func.groups()
        return float(r), float(g), float(b), float(a) if a is not None else 1.0
    raise ValueError(f"not a colour this test can read: {value!r}")


def _opaque(value: str, theme: dict[str, str], under: str = "surface") -> tuple[float, float, float]:
    """The colour as seen: a translucent token composited over ``--surface``, where it is used."""
    r, g, b, a = _rgba(value, theme)
    if a >= 1:
        return r, g, b
    br, bg, bb = _opaque(theme[under], theme)
    return r * a + br * (1 - a), g * a + bg * (1 - a), b * a + bb * (1 - a)


def _luminance(rgb: tuple[float, float, float]) -> float:
    def channel(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str, theme: dict[str, str]) -> float:
    lighter, darker = sorted(
        (_luminance(_opaque(theme[fg], theme)), _luminance(_opaque(theme[bg], theme))), reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


THEMES = _themes()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_token_pair_meets_wcag_aa(theme):
    failures = [
        f"--{fg} on --{bg}: {contrast(fg, bg, THEMES[theme]):.2f}:1 < {minimum}:1 ({use})"
        for fg, bg, minimum, use in PAIRS
        if contrast(fg, bg, THEMES[theme]) < minimum
    ]
    assert not failures, f"{theme} theme:\n" + "\n".join(failures)


def test_dark_mode_overrides_the_shared_names_and_changes_every_surface_and_text_token():
    light, dark = THEMES["light"], THEMES["dark"]
    for name in ("bg", "surface", "surface-soft", "ink", "ink-soft", "muted", "line", "accent", "accent-dark"):
        assert _opaque(light[name], light) != _opaque(dark[name], dark), name
    # The dark block may only redefine tokens that exist in the light theme: it overrides the
    # shared names, it does not invent a parallel set.
    dark_only = set(_declarations(_block(_strip_comments(design_css.APP_CSS), design_css.APP_CSS.index(
        "@media (prefers-color-scheme: dark)")))) - set(light)
    assert not dark_only, dark_only


def test_the_dark_palette_is_the_websites_privacy_card():
    # website/styles.css's .privacy-card: the one dark surface on the site (ADR 0079).
    dark = THEMES["dark"]
    assert dark["surface"] == "#142b2b"
    assert dark["ink"] == "#eaf4f3"
    assert dark["accent"] == "#8fd3ca"
    assert dark["ink-soft"] == "#c6d8d5"


def test_the_contrast_arithmetic():
    # WCAG's own reference points: black on white is 21:1, a colour on itself 1:1.
    theme = {"a": "#000000", "b": "#ffffff", "c": "rgba(255, 255, 255, .5)", "surface": "#000000"}
    assert contrast("a", "b", theme) == pytest.approx(21.0)
    assert contrast("b", "b", theme) == pytest.approx(1.0)
    # Half-transparent white over black is mid grey.
    assert 4.0 < contrast("c", "a", theme) < 6.0
    assert LARGE_TEXT <= TEXT
