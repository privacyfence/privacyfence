"""scripts/check_site_headers.py against a local server, without a browser."""

from __future__ import annotations

import http.server
import socketserver
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from check_site_headers import check, main  # noqa: E402 -- scripts/ is not a package
from site_security_headers import headers_for_path  # noqa: E402


class _Server(http.server.ThreadingHTTPServer):
    def server_bind(self):
        # The stock bind does a reverse DNS lookup (socket.getfqdn) that hangs on macOS CI runners.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


@contextmanager
def _server(mutate=lambda path, headers: headers):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 -- http.server API
            self.send_response(200)
            for name, value in mutate(self.path, dict(headers_for_path(self.path))).items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):  # noqa: A003
            pass

    server = _Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_matching_headers_pass(capsys):
    with _server() as url:
        lines = check(url)
        assert main([url]) == 0
    assert len(lines) == 12 and all(line.startswith("[ ok ]") for line in lines)
    assert "Site security headers match." in capsys.readouterr().out


def test_a_missing_header_is_reported(capsys):
    def drop(path, headers):
        headers.pop("Referrer-Policy")
        return headers

    with _server(drop) as url:
        failures = [line for line in check(url) if line.startswith("[FAIL]")]
        assert main([url]) == 1
    assert len(failures) == 3 and all("Referrer-Policy" in line and "got None" in line for line in failures)
    assert "Site security headers differ." in capsys.readouterr().out


def test_a_wrong_value_is_reported():
    def weaken(path, headers):
        headers["Strict-Transport-Security"] = "max-age=1"
        return headers

    with _server(weaken) as url:
        failures = [line for line in check(url) if line.startswith("[FAIL]")]
    assert len(failures) == 3
    assert all("Strict-Transport-Security" in line and "max-age=1" in line for line in failures)


def test_the_docs_path_expects_the_docs_policy():
    def site_policy_everywhere(path, headers):
        headers["Content-Security-Policy"] = headers_for_path("/")["Content-Security-Policy"]
        return headers

    with _server(site_policy_everywhere) as url:
        failures = [line for line in check(url) if line.startswith("[FAIL]")]
    assert len(failures) == 1
    assert failures[0].startswith("[FAIL] /docs/ Content-Security-Policy")
