"""``json_for_script`` is the only way a value gets into a ``<script>`` element."""

from __future__ import annotations

import json

import pytest

from privacyfence.html_embed import json_for_script


@pytest.mark.parametrize("char, escape", [
    ("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), (" ", "\\u2028"), (" ", "\\u2029"),
])
def test_each_dangerous_character_is_written_as_a_json_escape(char, escape):
    out = json_for_script("a" + char + "b")
    assert char not in out
    assert escape in out


def test_a_script_end_tag_cannot_survive():
    assert "</script" not in json_for_script({"k": "</script><b>x"})


def test_the_value_round_trips():
    value = {"a": ["</script>", "x&y", "  ", 1, None, True], "b": {"c": "<!--"}}
    assert json.loads(json_for_script(value)) == value
