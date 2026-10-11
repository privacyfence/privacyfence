from __future__ import annotations

import json

import pytest

from privacyfence_sync.template import PLACEHOLDER, TemplateError, render_template

TEMPLATE = f"<script>const DATA = {PLACEHOLDER};</script>"


def test_replaces_the_placeholder() -> None:
    out = render_template(TEMPLATE, {"a": [1, "é"]})
    assert out == '<script>const DATA = {"a": [1, "é"]};</script>'


@pytest.mark.parametrize("text", ["<script></script>", PLACEHOLDER + PLACEHOLDER])
def test_requires_exactly_one_placeholder(text: str) -> None:
    with pytest.raises(TemplateError, match="exactly once"):
        render_template(text, {})


def test_escapes_script_breakout() -> None:
    out = render_template(TEMPLATE, {"x": "</script>&"})
    assert out.count("</script>") == 1  # only the template's own closing tag
    assert "\\u003c/script\\u003e\\u0026" in out


def test_escapes_line_separators() -> None:
    out = render_template(TEMPLATE, {"x": "a\u2028b\u2029c"})
    assert "\u2028" not in out and "\u2029" not in out
    assert "\\u2028" in out and "\\u2029" in out
    assert json.loads(out.split("DATA = ")[1].split(";</script>")[0]) == {"x": "a\u2028b\u2029c"}
