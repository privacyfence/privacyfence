"""Tests for scripts/mcpb_manifest.py: the two Claude Desktop extension manifests (ADR 0087).

The two ``.mcpb`` files must differ only where the decision says they do. Anything else drifting
between them (a different entry point, a lost field) would make the second extension a second
product rather than the same one with one flag.

Imported by file path (importlib) rather than as a package, since scripts/ isn't part of the
installed ``privacyfence`` distribution.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "mcpb_manifest.py"
_spec = importlib.util.spec_from_file_location("mcpb_manifest", _SCRIPT_PATH)
mcpb_manifest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcpb_manifest)

VERSION = "4.8.0"


class TestDefaultManifest:
    def test_is_the_template_with_the_version_filled_in(self):
        template = mcpb_manifest.TEMPLATE.read_text(encoding="utf-8")
        assert mcpb_manifest.render("default", VERSION) == json.loads(template.replace("__VERSION__", VERSION))

    def test_starts_the_shim_with_no_flag(self):
        manifest = mcpb_manifest.render("default", VERSION)
        assert manifest["name"] == "privacyfence"
        assert manifest["server"]["mcp_config"]["args"] == ["${__dirname}/server/shim.js"]
        assert "display_name" not in manifest


class TestNoPromptsManifest:
    def test_name_display_name_and_args(self):
        manifest = mcpb_manifest.render("no-prompts", VERSION)
        assert manifest["name"] == "privacyfence-read-only"
        assert manifest["display_name"] == "PrivacyFence (no Claude prompts)"
        assert manifest["server"]["mcp_config"]["args"] == [
            "${__dirname}/server/shim.js",
            "--tool-annotations=all-read-only",
        ]

    def test_description_says_what_it_does_and_to_install_only_one(self):
        description = mcpb_manifest.render("no-prompts", VERSION)["description"]
        assert "does not ask" in description
        assert "PrivacyFence's approval" in description
        assert "Install only one" in description
        assert "PrivacyFence.mcpb" in description

    def test_differs_from_the_default_only_in_the_four_decided_fields(self):
        default = mcpb_manifest.render("default", VERSION)
        no_prompts = mcpb_manifest.render("no-prompts", VERSION)
        for key in ("name", "display_name", "description"):
            default.pop(key, None)
            no_prompts.pop(key)
        default["server"]["mcp_config"].pop("args")
        no_prompts["server"]["mcp_config"].pop("args")
        assert no_prompts == default

    def test_flag_is_one_the_shim_and_the_daemon_both_accept(self):
        # The shim passes the value through verbatim as the header; the daemon answers any value
        # it does not know with 400 (ADR 0086). Pinned against the daemon's own table so a
        # rename on either side fails here rather than in a packaged smoke test.
        from privacyfence.org_mode import TOOL_ANNOTATIONS_ALL_READ_ONLY
        from privacyfence.web.mcp_tools import annotations_mode_from_header

        value = mcpb_manifest.NO_PROMPTS_FLAG.split("=", 1)[1]
        assert annotations_mode_from_header(value) == TOOL_ANNOTATIONS_ALL_READ_ONLY


class TestCli:
    @pytest.mark.parametrize("variant", mcpb_manifest.VARIANTS)
    def test_writes_the_rendered_manifest(self, tmp_path, variant):
        out = tmp_path / "manifest.json"
        assert mcpb_manifest.main([variant, VERSION, str(out)]) == 0
        assert json.loads(out.read_text(encoding="utf-8")) == mcpb_manifest.render(variant, VERSION)

    def test_rejects_an_unknown_variant(self, tmp_path, capsys):
        assert mcpb_manifest.main(["read-only", VERSION, str(tmp_path / "m.json")]) == 2
        assert "unknown variant" in capsys.readouterr().err
        assert not (tmp_path / "m.json").exists()

    def test_rejects_the_wrong_argument_count(self, capsys):
        assert mcpb_manifest.main(["default"]) == 2
        assert "Usage" in capsys.readouterr().err

    def test_render_rejects_an_unknown_variant(self):
        with pytest.raises(ValueError, match="unknown variant"):
            mcpb_manifest.render("both", VERSION)
