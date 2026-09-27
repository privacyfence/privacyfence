#!/usr/bin/env python3
"""Render one of the two Claude Desktop extension manifests from mcpb/manifest.json.tmpl.

scripts/build_mcpb.sh builds two ``.mcpb`` files from the same shim and the same template
(ADR 0087). They differ only in the manifest this script writes:

- ``default``: the template with ``__VERSION__`` filled in, nothing else. The shim runs with no
  flag, so the daemon's own annotation mode applies (truthful unless a bundle says otherwise).
- ``no-prompts``: the same manifest with a different ``name`` (so Claude Desktop keeps the two
  apart), a ``display_name`` and ``description`` that say what it does and that only one of the
  two should be installed, and ``--tool-annotations=all-read-only`` on the shim's command line,
  which the shim turns into the ``X-PrivacyFence-Tool-Annotations`` header (ADR 0086).

Standard library only, and it doesn't import the ``privacyfence`` package: build_mcpb.sh runs it
on every build host, Git for Windows' bash included.

Usage:
    python3 scripts/mcpb_manifest.py <default|no-prompts> <version> <output manifest.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

TEMPLATE = Path(__file__).resolve().parents[1] / "mcpb" / "manifest.json.tmpl"

VARIANTS = ("default", "no-prompts")

NO_PROMPTS_NAME = "privacyfence-read-only"
NO_PROMPTS_DISPLAY_NAME = "PrivacyFence (no Claude prompts)"
NO_PROMPTS_FLAG = "--tool-annotations=all-read-only"
NO_PROMPTS_DESCRIPTION = (
    "PrivacyFence with every tool advertised to Claude as read-only, so Claude Desktop does not ask "
    "for its own confirmation before PrivacyFence's approval. Every read and write still passes "
    "through PrivacyFence's approval gate and audit log. Install only one of the two PrivacyFence "
    "extensions: this one or PrivacyFence.mcpb."
)


def render(variant: str, version: str, template: str | None = None) -> dict[str, Any]:
    """The manifest for ``variant`` at ``version``."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; expected one of {', '.join(VARIANTS)}")
    text = TEMPLATE.read_text(encoding="utf-8") if template is None else template
    manifest: dict[str, Any] = json.loads(text.replace("__VERSION__", version))
    if variant == "no-prompts":
        manifest["name"] = NO_PROMPTS_NAME
        manifest["display_name"] = NO_PROMPTS_DISPLAY_NAME
        manifest["description"] = NO_PROMPTS_DESCRIPTION
        manifest["server"]["mcp_config"]["args"] = [
            *manifest["server"]["mcp_config"]["args"],
            NO_PROMPTS_FLAG,
        ]
    return manifest


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    variant, version, output = argv
    try:
        manifest = render(variant, version)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    Path(output).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
