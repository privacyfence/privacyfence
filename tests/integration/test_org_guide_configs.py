"""The org guide's nginx server block is valid, and keeps the lines the daemon needs.

``docs/org-mode-setup-guide.md`` gives operators an nginx reverse-proxy block to copy verbatim.
Nothing else in the suite runs it; ``test_org_ubuntu_release_smoke.py`` starts the daemon as a plain
process and simulates the proxy's headers at the HTTP layer. The block is read from the guide by
heading name, so the guide stays the only copy and a renumbered section does not break the lookup.

Two kinds of check:

- Static assertions in plain Python that the lines the daemon depends on are still there: the
  forwarded headers the Origin check needs, the streaming settings and a body size limit that fits
  an upload slot. These run everywhere.
- The real parser, ``nginx -t``, which catches an unknown directive, a bad value or a syntax error.
  It skips when nginx is absent. It proves the config parses, not that the daemon runs behind it.
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
from tests.integration.guide_sections import fenced_block, section

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE = REPO_ROOT / "docs" / "org-mode-setup-guide.md"

GUIDE_CERT = "/etc/letsencrypt/live/pf.acme.example.com/fullchain.pem"
GUIDE_KEY = "/etc/letsencrypt/live/pf.acme.example.com/privkey.pem"

_NGINX_SIZE = re.compile(r"^(\d+)([kKmMgG]?)$")
_NGINX_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _section(title: str) -> str:
    return section(GUIDE, title)


def _fenced_block(title: str, language: str) -> str:
    return fenced_block(GUIDE, title, language)


def _nginx() -> str:
    return _fenced_block("nginx", "nginx")


def _nginx_directives(config: str, name: str) -> list[str]:
    """The argument strings of every ``name`` directive, anywhere in the config."""
    return [m.group(1).strip() for m in re.finditer(rf"^\s*{name}\s+([^;]*);", config, flags=re.MULTILINE)]


# --- Static: runs everywhere ------------------------------------------------------------------------


def test_section_lookup_is_by_heading_name_and_fails_loudly() -> None:
    assert "server_name" in _section("nginx")
    with pytest.raises(AssertionError, match="no section headed"):
        _section("No such heading")


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
