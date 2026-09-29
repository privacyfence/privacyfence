#!/usr/bin/env python3
"""Render the official MCP registry's `server.json` for one stable release.

`.github/workflows/publish-mcp-registry.yml` runs this after `build.yml` has attached
`PrivacyFence.mcpb` to the release's GitHub Release, then hands the output to `mcp-publisher
publish`. The committed file is a template, `mcpb/server.json.tmpl`, for the same reason
`mcpb/manifest.json.tmpl` is one: there is no version string in the source tree, and
setuptools_scm stays the only version source (docs/releasing.md). The workflow passes in the
version setuptools_scm resolved and the `.mcpb` it downloaded back from the release, and this
fills in the three things that change per release -- `version`, the package `identifier` (a
release-asset URL, the only host the registry accepts for an MCPB package besides GitLab) and its
`fileSha256`.

The hash is of the file the registry's clients will actually download, which is why the workflow
fetches it from the public release URL rather than hashing the build job's copy: the registry does
not check `fileSha256` itself, but every client that installs from it does, so a hash of anything
else would publish an entry nobody can install (ADR 0112).

Only a stable version renders. A pre-release's GitHub Release carries no files, so its
`identifier` would point at nothing.

Stdlib only -- no PrivacyFence install required, matching scripts/changelog_section.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "mcpb" / "server.json.tmpl"

_STABLE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*?__")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render(template: str, version: str, sha256: str) -> str:
    """Fill the template and return it as pretty-printed JSON.

    Raises ValueError for a non-stable version, a malformed hash, or a placeholder the
    template has that this function does not fill.
    """
    if not _STABLE_VERSION.match(version):
        raise ValueError(
            f"{version!r} is not a stable X.Y.Z version; only a stable release's GitHub Release "
            "carries PrivacyFence.mcpb"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError(f"{sha256!r} is not a lowercase hex SHA-256")
    text = template.replace("__VERSION__", version).replace("__SHA256__", sha256)
    leftover = _PLACEHOLDER.findall(text)
    if leftover:
        raise ValueError(f"unfilled placeholder(s) in the template: {sorted(set(leftover))}")
    return json.dumps(json.loads(text), indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True, help="the stable version, e.g. 5.2.1")
    parser.add_argument("--mcpb", required=True, type=Path, help="the PrivacyFence.mcpb downloaded from the release")
    parser.add_argument("--out", type=Path, default=Path("server.json"))
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    args = parser.parse_args(argv)

    try:
        output = render(args.template.read_text(encoding="utf-8"), args.version, sha256_of(args.mcpb))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(output, encoding="utf-8")
    print(f"wrote {args.out} for {args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
