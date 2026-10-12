"""Puts a JSON document into an HTML dashboard template. Pure: no I/O of its own."""

from __future__ import annotations

import json
from typing import Any

PLACEHOLDER = "/*PRIVACYFENCE_SYNC_DATA*/null"

_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", "\u2028": "\\u2028", "\u2029": "\\u2029"}


class TemplateError(ValueError):
    """The template cannot take the data."""


def render_template(template_text: str, data: Any) -> str:
    if template_text.count(PLACEHOLDER) != 1:
        raise TemplateError(f"the template must contain {PLACEHOLDER} exactly once")
    text = json.dumps(data, ensure_ascii=False)
    for char, escape in _ESCAPES.items():
        text = text.replace(char, escape)
    head, tail = template_text.split(PLACEHOLDER)
    return head + text + tail
