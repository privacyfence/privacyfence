"""The org guide's systemd unit and nginx server block are valid, and keep the lines the daemon needs.

``docs/org-mode-setup-guide.md`` gives operators two configs to copy verbatim: the hardened
``privacyfence-org.service`` unit and the nginx reverse-proxy block. Nothing else in the suite runs
either one; ``test_org_ubuntu_release_smoke.py`` starts the daemon as a plain process and simulates
the proxy's headers at the HTTP layer. Both configs are read from the guide by heading name, so the
guide stays the only copy and a renumbered section does not break the lookup.

Two kinds of check:

- Static assertions in plain Python that the lines the daemon depends on are still there: the
  hardening settings, the one writable path, the forwarded headers the Origin check needs, and a
  body size limit that fits an upload slot. These run everywhere.
- The real parsers, ``systemd-analyze verify`` and ``nginx -t``, which catch an unknown directive,
  a bad value or a syntax error. Each skips when its binary is absent. They prove the configs
  parse, not that the daemon runs under them.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from privacyfence.local_files import DEFAULT_CAPABILITY_UPLOAD_MAX_BYTES
from privacyfence.web.server import DEFAULT_PORT

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE = REPO_ROOT / "docs" / "org-mode-setup-guide.md"

UNIT_NAME = "privacyfence-org.service"
GUIDE_EXEC_START = "/opt/privacyfence/venv/bin/privacyfence-app"
GUIDE_CERT = "/etc/letsencrypt/live/pf.acme.example.com/fullchain.pem"
GUIDE_KEY = "/etc/letsencrypt/live/pf.acme.example.com/privkey.pem"
DATA_HOME = "/var/lib/privacyfence-org"

_HEADING = re.compile(r"^(#{1,6}) +(?:\d+\. +)?(.+?)\s*$")
_NGINX_SIZE = re.compile(r"^(\d+)([kKmMgG]?)$")
_NGINX_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _section(title: str) -> str:
    """The body of the guide section headed ``title``, up to the next heading at its level or above."""
    lines = GUIDE.read_text(encoding="utf-8").splitlines()
    start: int | None = None
    level = 0
    in_fence = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
        # A "# comment" inside a fenced config is not a heading.
        match = None if in_fence else _HEADING.match(line)
        if match is None:
            continue
        if start is None:
            if match.group(2) == title:
                start, level = index + 1, len(match.group(1))
        elif len(match.group(1)) <= level:
            return "\n".join(lines[start:index])
    assert start is not None, f"no section headed {title!r} in {GUIDE.name}"
    return "\n".join(lines[start:])


def _fenced_block(title: str, language: str) -> str:
    blocks: list[str] = re.findall(rf"^```{language}\n(.*?)^```$", _section(title), flags=re.MULTILINE | re.DOTALL)
    assert len(blocks) == 1, f"section {title!r} has {len(blocks)} ```{language} blocks, expected one"
    return blocks[0]


def _unit() -> str:
    return _fenced_block("Hardened systemd unit", "ini")


def _nginx() -> str:
    return _fenced_block("nginx", "nginx")


def _service_settings(unit: str) -> dict[str, list[str]]:
    settings: dict[str, list[str]] = {}
    section = None
    for raw in unit.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line
            continue
        if section == "[Service]":
            key, _, value = line.partition("=")
            settings.setdefault(key.strip(), []).append(value.strip())
    return settings


def _nginx_directives(config: str, name: str) -> list[str]:
    """The argument strings of every ``name`` directive, anywhere in the config."""
    return [m.group(1).strip() for m in re.finditer(rf"^\s*{name}\s+([^;]*);", config, flags=re.MULTILINE)]


# --- Static: runs everywhere ------------------------------------------------------------------------


def test_section_lookup_is_by_heading_name_and_fails_loudly() -> None:
    assert "[Service]" in _section("Hardened systemd unit")
    with pytest.raises(AssertionError, match="no section headed"):
        _section("No such heading")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("User", "privacyfence-org"),
        ("Group", "privacyfence-org"),
        ("ExecStart", GUIDE_EXEC_START),
        ("UMask", "0077"),
        ("ProtectSystem", "strict"),
        ("ReadWritePaths", DATA_HOME),
        ("ProtectHome", "true"),
        ("PrivateTmp", "true"),
        ("PrivateDevices", "true"),
        ("NoNewPrivileges", "true"),
        ("CapabilityBoundingSet", ""),
        ("AmbientCapabilities", ""),
        ("RestrictNamespaces", "true"),
        ("SystemCallArchitectures", "native"),
        ("RestrictAddressFamilies", "AF_UNIX AF_INET AF_INET6"),
    ],
)
def test_unit_keeps_hardening_setting(key: str, value: str) -> None:
    assert _service_settings(_unit()).get(key) == [value]


def test_unit_writable_path_is_the_service_accounts_home() -> None:
    # The data directory is ~/.privacyfence inside the account's home, so the one writable path and
    # the home directory the install section creates must be the same directory.
    install = _section("Service account and install")
    assert f"--home-dir {DATA_HOME} " in install


def test_nginx_body_limit_fits_an_upload_slot() -> None:
    (size,) = _nginx_directives(_nginx(), "client_max_body_size")
    match = _NGINX_SIZE.match(size)
    assert match is not None, f"unparseable client_max_body_size {size!r}"
    limit = int(match.group(1)) * _NGINX_UNITS[match.group(2).lower()]
    assert limit >= DEFAULT_CAPABILITY_UPLOAD_MAX_BYTES


@pytest.mark.parametrize(
    "header",
    [
        "Host              $host",
        "X-Forwarded-For   $proxy_add_x_forwarded_for",
        "X-Forwarded-Proto $scheme",
    ],
)
def test_nginx_forwards_header(header: str) -> None:
    normalized = {" ".join(arg.split()) for arg in _nginx_directives(_nginx(), "proxy_set_header")}
    assert " ".join(header.split()) in normalized


def test_nginx_streams_and_proxies_to_the_daemon() -> None:
    config = _nginx()
    assert _nginx_directives(config, "proxy_buffering") == ["off"]
    assert set(_nginx_directives(config, "proxy_pass")) == {f"http://127.0.0.1:{DEFAULT_PORT}"}
    assert _nginx_directives(config, "ssl_certificate") == [GUIDE_CERT]
    assert _nginx_directives(config, "ssl_certificate_key") == [GUIDE_KEY]


# --- Real parsers: skip where the binary is absent --------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        pytest.skip(f"{binary} is not installed")
    return path


def test_systemd_analyze_verifies_the_unit(tmp_path: Path) -> None:
    systemd_analyze = _require("systemd-analyze")
    # verify refuses a unit whose ExecStart binary does not exist; a stub stands in for the venv.
    stub = tmp_path / "privacyfence-app"
    stub.write_text("#!/bin/sh\n", encoding="utf-8")
    stub.chmod(0o755)
    unit_file = tmp_path / UNIT_NAME
    unit_file.write_text(_unit().replace(GUIDE_EXEC_START, str(stub)), encoding="utf-8")

    result = subprocess.run(
        [systemd_analyze, "verify", "--man=no", str(unit_file)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    # An unknown key or unparseable value is only logged ("..., ignoring") with exit 0, so any line
    # naming the unit counts as a failure; lines about other units on the host do not.
    output = result.stdout + result.stderr
    problems = [line for line in output.splitlines() if UNIT_NAME in line]
    assert result.returncode == 0, output
    assert problems == []


def test_nginx_accepts_the_server_block(tmp_path: Path) -> None:
    nginx = _require("nginx")
    openssl = _require("openssl")
    cert, key = tmp_path / "fullchain.pem", tmp_path / "privkey.pem"
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=pf.acme.example.com", "-keyout", str(key), "-out", str(cert),
        ],
        capture_output=True,
        timeout=60,
        check=True,
    )  # fmt: skip
    block = _nginx().replace(GUIDE_CERT, str(cert)).replace(GUIDE_KEY, str(key))
    # nginx -t binds every listen address, which needs root for 80 and 443; free loopback ports stand
    # in for them and every other listen parameter (ssl, http2) is kept as the guide wrote it.
    block, listens = re.subn(
        r"^(\s*listen\s+)(\d+)\b", lambda m: f"{m.group(1)}127.0.0.1:{_free_port()}", block, flags=re.MULTILINE
    )
    assert listens == 2, "expected the guide's two listen directives (80 and 443)"
    # Every path nginx would otherwise take from its compiled-in defaults under /var is redirected
    # into tmp_path, so the check needs no root and leaves nothing behind.
    temp_paths = "\n".join(
        f"    {kind}_temp_path {tmp_path / kind};" for kind in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi")
    )
    config = tmp_path / "nginx.conf"
    config.write_text(
        f"pid {tmp_path / 'nginx.pid'};\n"
        f"error_log {tmp_path / 'error.log'};\n"
        "events {}\n"
        "http {\n"
        f"    access_log off;\n{temp_paths}\n"
        f"{block}\n"
        "}\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [nginx, "-t", "-p", str(tmp_path), "-e", str(tmp_path / "error.log"), "-c", str(config)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    # nginx 1.25.1+ deprecates "listen ... http2", which the guide keeps for Ubuntu 24.04's 1.24.
    warnings = [
        line
        for line in output.splitlines()
        if "[warn]" in line and '"listen ... http2" directive is deprecated' not in line
    ]
    assert warnings == [], output
