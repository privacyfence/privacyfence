#!/usr/bin/env python3
"""Build an example plugin into a folder that can be copied into PrivacyFence's plugins directory.

``build_example_plugin.py today [--with-crash-tool] [--out DIR]`` freezes ``examples/plugins/today``
with PyInstaller into a one-file executable for the current OS (``today-plugin``, or
``today-plugin.exe`` on Windows), and writes the manifest and ``build-flags.json`` beside it, into
``DIR/today/`` (default ``dist/plugins``). The SDK is bundled from ``plugin-sdk/src`` through
``--paths``, so it does not have to be installed. PyInstaller's work and spec files go to a
temporary directory, so nothing is left in the checkout. Nothing here ships in an installer.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # nosec B404  # runs PyInstaller with a fixed argv
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SDK_SRC = REPO_ROOT / "plugin-sdk" / "src"
EXAMPLES_DIR = REPO_ROOT / "examples" / "plugins"
MANIFEST_FILENAME = "privacyfence-plugin.yaml"
FLAGS_FILENAME = "build-flags.json"
DEFAULT_OUT = Path("dist") / "plugins"
EXAMPLES = ("today",)


def build(name: str, out: Path, with_crash_tool: bool = False) -> Path:
    """Build example ``name`` into ``out/<name>`` and return that folder."""
    source = EXAMPLES_DIR / name
    executable = f"{name}-plugin"
    target = out.resolve() / name
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pf-plugin-build-") as scratch:
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--onefile",
            "--noconfirm",
            "--clean",
            "--name",
            executable,
            "--paths",
            str(SDK_SRC),
            "--paths",
            str(source),
            "--distpath",
            str(target),
            "--workpath",
            str(Path(scratch) / "work"),
            "--specpath",
            scratch,
            str(source / f"{name}_plugin.py"),
        ]
        subprocess.run(command, check=True)  # nosec B603  # fixed argv, no shell
    shutil.copy(source / MANIFEST_FILENAME, target / MANIFEST_FILENAME)
    (target / FLAGS_FILENAME).write_text(json.dumps({"crash_tool": with_crash_tool}) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an example plugin with PyInstaller for the current OS.")
    parser.add_argument("name", choices=EXAMPLES, help="the example to build")
    parser.add_argument("--with-crash-tool", action="store_true", help="also list the hidden crash tool")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output folder (default {DEFAULT_OUT})")
    args = parser.parse_args(argv)
    folder = build(args.name, args.out, args.with_crash_tool)
    print(folder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
