#!/usr/bin/env python3
"""Fail a ``.deb`` build whose bundled binaries need a newer glibc than ``debian/control`` declares.

The ``.deb`` ships a PyInstaller bundle: the Python interpreter and every extension module are
inside it, so the one system library the package really depends on is glibc, at whatever version
the build host's toolchain linked against. That version is not chosen anywhere -- it moves when
``build.yml``'s ``ubuntu-latest`` runner image does -- so ``debian/control`` states it as
``libc6 (>= X)`` and this script, run by ``scripts/build_deb.sh`` on the staged package tree,
refuses a bundle that references a ``GLIBC_`` symbol version newer than ``X``. Without it, a runner
image bump would silently raise the real floor while the package still claimed the old one, and
``dpkg`` would install an app that cannot start instead of refusing it.

Usage: ``check_deb_glibc_floor.py <staged package root> <DEBIAN/control>``
"""
from __future__ import annotations

import re
import subprocess  # nosec B404  # runs objdump on files this build just produced
import sys
from pathlib import Path

_ELF_MAGIC = b"\x7fELF"
_GLIBC_VERSION_RE = re.compile(r"\bGLIBC_(\d+(?:\.\d+)+)\b")
_LIBC6_FLOOR_RE = re.compile(r"(?:^|,)\s*libc6\s*\(\s*>=\s*(\d+(?:\.\d+)+)\s*\)")


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def max_glibc_version(objdump_output: str) -> str | None:
    """Highest ``GLIBC_x.y`` symbol version named in ``objdump -T`` output, or None."""
    versions = set(_GLIBC_VERSION_RE.findall(objdump_output))
    return max(versions, key=_version_key) if versions else None


def declared_glibc_floor(control_text: str) -> str | None:
    """The ``X`` in ``Depends: ... libc6 (>= X) ...``, or None if the field doesn't declare one."""
    for line in control_text.splitlines():
        if line.startswith("Depends:"):
            match = _LIBC6_FLOOR_RE.search(line.removeprefix("Depends:"))
            return match.group(1) if match else None
    return None


def _is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(4) == _ELF_MAGIC
    except OSError:
        return False


def required_glibc(root: Path) -> tuple[str | None, Path | None]:
    """The highest glibc symbol version any ELF file under ``root`` references, and one file
    that references it (for the error message)."""
    best: str | None = None
    best_path: Path | None = None
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or not _is_elf(path):
            continue
        result = subprocess.run(  # nosec B603 B607  # fixed argv, objdump from PATH like the rest of build_deb.sh's tools
            ["objdump", "-T", str(path)], capture_output=True, text=True, check=False
        )
        found = max_glibc_version(result.stdout)
        if found and (best is None or _version_key(found) > _version_key(best)):
            best, best_path = found, path
    return best, best_path


def check(root: Path, control_text: str) -> tuple[bool, str]:
    declared = declared_glibc_floor(control_text)
    if declared is None:
        return False, "DEBIAN/control's Depends: declares no `libc6 (>= X)` floor"
    required, where = required_glibc(root)
    if required is None:
        return False, f"no GLIBC_ symbol versions found under {root} -- is objdump installed and the bundle staged?"
    if _version_key(required) > _version_key(declared):
        return False, (
            f"the bundle needs glibc {required} ({where} references GLIBC_{required}) but debian/control "
            f"declares libc6 (>= {declared}). The build host's glibc moved: raise the floor in "
            "debian/control and the support matrix in docs/platform-support.md together."
        )
    note = "" if required == declared else f" (declared floor {declared} is higher than needed)"
    return True, f"glibc floor OK: bundle needs {required}, debian/control declares libc6 (>= {declared}){note}"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    ok, message = check(Path(argv[0]), Path(argv[1]).read_text())
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
