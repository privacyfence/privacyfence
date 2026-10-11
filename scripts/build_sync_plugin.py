#!/usr/bin/env python3
"""Build the privacyfence-sync Claude plugin zip that a stable release attaches (ADR 0163).

Copies plugins/privacyfence-sync/ into a temporary folder, stamps the release version into the
copy's plugin.json (the repository's plugin.json carries no version), and zips the copy so the
archive's root holds `.claude-plugin/` and `skills/` -- the layout of a plugin zip a user uploads.

Refuses with exit 1 when the bundled references/README.md differs from sync/README.md, so a zip
never ships a stale copy of the recipe format.

Stdlib only; `.github/workflows/build.yml`'s `finalize-release` job calls it before the stable
release assets are attached.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

PLUGIN_DIR = Path("plugins/privacyfence-sync")
README_COPY = PLUGIN_DIR / "skills/privacyfence-sync-recipes/references/README.md"
README_SOURCE = Path("sync/README.md")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True, help="version stamped into plugin.json")
    parser.add_argument("--out", required=True, type=Path, help="path of the zip to write")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: this script's repository)",
    )
    args = parser.parse_args(argv)

    root: Path = args.root
    if (root / README_COPY).read_bytes() != (root / README_SOURCE).read_bytes():
        print(
            f"build_sync_plugin: {README_COPY} differs from {README_SOURCE}; "
            f"copy it again: cp {README_SOURCE} {README_COPY}",
            file=sys.stderr,
        )
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "plugin"
        shutil.copytree(root / PLUGIN_DIR, staged)
        manifest_path = staged / ".claude-plugin" / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = args.version
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        args.out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staged.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staged).as_posix())
    print(f"build_sync_plugin: wrote {args.out} (version {args.version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
