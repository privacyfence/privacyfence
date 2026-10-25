"""One serializer for every value embedded in a ``<script>`` element.

``json.dumps`` leaves ``</script>``, ``<!--`` and the JavaScript line
separators U+2028/U+2029 as they are, so a connector- or client-controlled
string could end the script early and inject markup. ``json_for_script``
writes those characters as JSON ``\\u`` escapes instead: ``JSON.parse`` and a
JavaScript literal read back exactly the value that went in.
"""

from __future__ import annotations

import json
from typing import Any


def json_for_script(value: Any) -> str:
    """``json.dumps(value)``, safe to place inside a ``<script>`` element."""
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
