"""The minimum OS each installer enforces matches ``docs/platform-support.md``'s support matrix.

Every installer declares its floor in its own format -- ``PrivacyFenceApp.spec``'s
``LSMinimumSystemVersion`` (which ``scripts/build_pkg.sh`` copies into the ``.pkg``'s
``allowed-os-versions``), ``installer/privacyfence.iss``'s ``MinVersion``, ``debian/control``'s
``libc6``/``systemd`` dependency versions -- and none of them can see the others or the doc.
These tests read all of them and the matrix, so one can't move without the rest. What a built
artifact actually carries is checked where the artifact exists: ``test_macos_pkg_smoke.py`` for
the ``.pkg`` and ``scripts/check_deb_glibc_floor.py`` (run by ``scripts/build_deb.sh``) for the
``.deb``'s glibc floor against what the bundle was linked with.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_glibc_checker():
    spec = importlib.util.spec_from_file_location(
        "check_deb_glibc_floor", REPO_ROOT / "scripts" / "check_deb_glibc_floor.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


glibc_checker = _load_glibc_checker()


def _matrix_floor(platform_label: str) -> str:
    text = (REPO_ROOT / "docs" / "platform-support.md").read_text(encoding="utf-8")
    lines = text.split("## Support matrix", 1)[1].split("\n\n", 2)[1].splitlines()
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    column = header.index("Minimum OS")
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] == platform_label:
            return cells[column]
    raise AssertionError(f"no {platform_label!r} row in platform-support.md's support matrix")


def test_macos_app_floor_matches_matrix():
    spec = (REPO_ROOT / "PrivacyFenceApp.spec").read_text(encoding="utf-8")
    declared = re.findall(r'"LSMinimumSystemVersion":\s*"([\d.]+)"', spec)
    assert len(declared) == 1, declared
    assert _matrix_floor("macOS") == f"macOS {declared[0].removesuffix('.0')}, Apple silicon"


def test_macos_pkg_ships_for_apple_silicon_only():
    script = (REPO_ROOT / "scripts" / "build_pkg.sh").read_text(encoding="utf-8")
    assert re.search(r'^HOST_ARCH="arm64"$', script, flags=re.MULTILINE)
    assert 'hostArchitectures="${HOST_ARCH}"' in script
    # ...and refuses to package a bundle built for anything else.
    assert re.search(r'\[ "\$BUNDLE_ARCHS" = "\$HOST_ARCH" \] \|\| \{', script)


def test_macos_pkg_takes_its_floor_from_the_app_bundle():
    script = (REPO_ROOT / "scripts" / "build_pkg.sh").read_text(encoding="utf-8")
    assert re.search(r'MIN_MACOS=\$\(plutil -extract LSMinimumSystemVersion raw "\$\{BUNDLE\}/Contents/Info\.plist"\)', script)
    assert re.search(r'<allowed-os-versions>\s*<os-version min="\$\{MIN_MACOS\}"/>\s*</allowed-os-versions>', script)


def test_windows_installer_floor_matches_matrix():
    iss = (REPO_ROOT / "installer" / "privacyfence.iss").read_text(encoding="utf-8")
    setup = iss.split("[Setup]", 1)[1].split("\n[", 1)[0]
    declared = re.findall(r"^MinVersion=(\S+)$", setup, flags=re.MULTILINE)
    assert declared == ["10.0"]
    assert _matrix_floor("Windows") == "Windows 10 / Windows Server 2016 (x64)"
    # The matrix's "(x64)" is enforced only by ArchitecturesAllowed; the 64-bit
    # install mode on its own refuses nothing.
    allowed = re.findall(r"^ArchitecturesAllowed=(\S+)$", setup, flags=re.MULTILINE)
    assert allowed == ["x64os"], allowed
    mode = re.findall(r"^ArchitecturesInstallIn64BitMode=(\S+)$", setup, flags=re.MULTILINE)
    assert mode == ["x64os"], mode


def _deb_depends() -> str:
    control = (REPO_ROOT / "debian" / "control").read_text(encoding="utf-8")
    package_stanza = control.strip().split("\n\n")[-1]
    return next(line for line in package_stanza.splitlines() if line.startswith("Depends:"))


def test_deb_floor_matches_matrix():
    depends = _deb_depends()
    glibc = glibc_checker.declared_glibc_floor(depends)
    systemd = re.search(r"\bsystemd \(>= (\d+)\)", depends)
    assert glibc and systemd, depends
    assert _matrix_floor("Debian/Ubuntu local mode").startswith(
        f"glibc {glibc} and systemd {systemd.group(1)} "
    )


def test_deb_architectures_match_matrix():
    # debian/control may only claim what the matrix (and so CI) ships -- #679.
    control = (REPO_ROOT / "debian" / "control").read_text(encoding="utf-8")
    package_stanza = control.strip().split("\n\n")[-1]
    declared = re.findall(r"^Architecture: (.+)$", package_stanza, flags=re.MULTILINE)
    assert declared == ["amd64"], declared
    assert _matrix_floor("Debian/Ubuntu local mode").endswith(f", {declared[0]})")


def test_deb_build_refuses_an_undeclared_host_architecture():
    script = (REPO_ROOT / "scripts" / "build_deb.sh").read_text(encoding="utf-8")
    assert 'declared = line.partition(":")[2].split()' in script
    assert re.search(r"^\s+if arch not in declared:\n\s+sys\.exit\(", script, flags=re.MULTILINE)


def test_deb_systemd_floor_covers_every_unit_directive():
    # RestrictSUIDSGID= is the newest directive the daemon's unit uses (systemd 242).
    unit = (REPO_ROOT / "installer" / "linux" / "privacyfence-daemon.service.tmpl").read_text(encoding="utf-8")
    assert "RestrictSUIDSGID=" in unit
    assert "systemd (>= 242)" in _deb_depends()


def test_python_install_floor_matches_requires_python():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    floor = pyproject["project"]["requires-python"].removeprefix(">=")
    assert _matrix_floor("Linux Python install") == f"Python {floor}"
    assert _matrix_floor("Linux org mode") == f"Python {floor}"


# ── scripts/check_deb_glibc_floor.py ──────────────────────────────────────────

_OBJDUMP_SAMPLE = """
0000000000000000      DF *UND*  0000000000000000 (GLIBC_2.2.5) free
0000000000000000      DF *UND*  0000000000000000 (GLIBC_2.38) __isoc23_strtol
0000000000000000      DF *UND*  0000000000000000 (GLIBC_2.9)  pipe2
0000000000000000  w   D  *UND*  0000000000000000  Base        __gmon_start__
"""


def test_max_glibc_version_compares_numerically():
    assert glibc_checker.max_glibc_version(_OBJDUMP_SAMPLE) == "2.38"
    assert glibc_checker.max_glibc_version("no versioned symbols") is None


@pytest.mark.parametrize(
    ("depends", "expected"),
    [
        ("Depends: libc6 (>= 2.39), systemd (>= 242)", "2.39"),
        ("Depends: systemd (>= 242), libc6 (>=2.36)", "2.36"),
        ("Depends: libc6-dev (>= 2.39)", None),
        ("Depends: systemd", None),
        ("Package: privacyfence", None),
    ],
)
def test_declared_glibc_floor(depends, expected):
    assert glibc_checker.declared_glibc_floor(depends) == expected


def _fake_objdump(monkeypatch, output_by_name: dict[str, str]) -> None:
    def fake_run(argv, **kwargs):
        assert argv[:2] == ["objdump", "-T"]
        return subprocess.CompletedProcess(argv, 0, stdout=output_by_name[Path(argv[2]).name], stderr="")

    monkeypatch.setattr(glibc_checker.subprocess, "run", fake_run)


def _stage(tmp_path: Path) -> Path:
    root = tmp_path / "opt"
    (root / "_internal").mkdir(parents=True)
    (root / "PrivacyFenceApp").write_bytes(b"\x7fELF" + b"\0" * 12)
    (root / "_internal" / "libpython.so").write_bytes(b"\x7fELF" + b"\0" * 12)
    (root / "_internal" / "base_library.zip").write_bytes(b"PK\x03\x04")
    (root / "_internal" / "libpython-link.so").symlink_to("libpython.so")
    return root


def test_check_passes_when_bundle_is_within_the_floor(tmp_path, monkeypatch):
    root = _stage(tmp_path)
    _fake_objdump(monkeypatch, {"PrivacyFenceApp": "(GLIBC_2.17)", "libpython.so": _OBJDUMP_SAMPLE})
    ok, message = glibc_checker.check(root, "Depends: libc6 (>= 2.38)")
    assert ok, message
    assert "needs 2.38" in message and "higher than needed" not in message


def test_check_notes_a_floor_higher_than_needed(tmp_path, monkeypatch):
    root = _stage(tmp_path)
    _fake_objdump(monkeypatch, {"PrivacyFenceApp": "(GLIBC_2.17)", "libpython.so": _OBJDUMP_SAMPLE})
    ok, message = glibc_checker.check(root, "Depends: libc6 (>= 2.39)")
    assert ok
    assert "higher than needed" in message


def test_check_fails_when_bundle_needs_a_newer_glibc(tmp_path, monkeypatch):
    root = _stage(tmp_path)
    _fake_objdump(monkeypatch, {"PrivacyFenceApp": "(GLIBC_2.17)", "libpython.so": _OBJDUMP_SAMPLE})
    ok, message = glibc_checker.check(root, "Depends: libc6 (>= 2.36)")
    assert not ok
    assert "needs glibc 2.38" in message and "libpython.so" in message and "(>= 2.36)" in message


def test_check_fails_without_a_declared_floor(tmp_path):
    ok, message = glibc_checker.check(tmp_path, "Depends: systemd (>= 242)")
    assert not ok and "declares no" in message


def test_check_fails_when_no_versioned_symbols_are_found(tmp_path, monkeypatch):
    root = _stage(tmp_path)
    _fake_objdump(monkeypatch, {"PrivacyFenceApp": "", "libpython.so": ""})
    ok, message = glibc_checker.check(root, "Depends: libc6 (>= 2.39)")
    assert not ok and "no GLIBC_ symbol versions" in message


@pytest.mark.skipif(sys.platform != "linux", reason="needs a real glibc-linked ELF and objdump")
def test_required_glibc_reads_a_real_binary(tmp_path):
    import shutil

    if shutil.which("objdump") is None:
        pytest.skip("objdump not on PATH")
    real = Path(shutil.which("ls") or "/bin/ls").resolve()
    (tmp_path / real.name).write_bytes(real.read_bytes())
    required, where = glibc_checker.required_glibc(tmp_path)
    assert required is not None and required.startswith("2.")
    assert where == tmp_path / real.name


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    root = _stage(tmp_path)
    _fake_objdump(monkeypatch, {"PrivacyFenceApp": "(GLIBC_2.17)", "libpython.so": _OBJDUMP_SAMPLE})
    control = tmp_path / "control"
    control.write_text("Package: privacyfence\nDepends: libc6 (>= 2.38), systemd (>= 242)\n")
    assert glibc_checker.main([str(root), str(control)]) == 0
    control.write_text("Package: privacyfence\nDepends: libc6 (>= 2.35)\n")
    assert glibc_checker.main([str(root), str(control)]) == 1
    assert glibc_checker.main([]) == 2
