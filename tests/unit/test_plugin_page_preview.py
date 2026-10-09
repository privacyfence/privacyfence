"""The plugin page preview script: its host and its arguments, without starting a server."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "plugin_page_preview.py"

_spec = importlib.util.spec_from_file_location("plugin_page_preview", SCRIPT)
assert _spec is not None and _spec.loader is not None
preview = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preview)


class TestPreviewHost:
    def test_serves_the_body_for_preview(self):
        host = preview.PreviewHost("<p>hi</p>")
        result = asyncio.run(host.web_request("preview", "/", {}, None))
        assert result["status"] == 200
        assert result["body"] == "<p>hi</p>"
        assert result["headers"]["content-type"].startswith("text/html")

    def test_other_names_raise_lookup_error(self):
        host = preview.PreviewHost("x")
        with pytest.raises(LookupError):
            asyncio.run(host.web_request("other", "/", {}, None))

    def test_page_new_tabs_only_with_the_flag_and_the_preview_name(self):
        assert preview.PreviewHost("x", new_tabs=True).page_new_tabs("preview") is True
        assert preview.PreviewHost("x", new_tabs=True).page_new_tabs("other") is False
        assert preview.PreviewHost("x").page_new_tabs("preview") is False


class TestParser:
    def test_accepts_exactly_one_source(self):
        parser = preview.build_parser()
        assert parser.parse_args(["--check-page"]).check_page is True
        assert parser.parse_args(["--html", "a.html", "--new-tabs", "--port", "9"]).port == 9

    def test_refuses_neither_and_both(self):
        parser = preview.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])
        with pytest.raises(SystemExit):
            parser.parse_args(["--html", "a.html", "--check-page"])

    def test_unreadable_html_exits_2(self, tmp_path, capsys):
        assert preview.main(["--html", str(tmp_path / "missing.html")]) == 2
        assert "Cannot read" in capsys.readouterr().err


def test_check_page_has_the_elements_the_checks_use():
    for needle in ('id="newtab"', 'id="sametab"', 'id="own"', 'id="nogesture"', "securitypolicyviolation"):
        assert needle in preview.CHECK_PAGE
