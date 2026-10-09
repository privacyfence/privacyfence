#!/usr/bin/env python3
"""Open any HTML file, or a built-in check page, as a plugin page in your own browser.

    python3 scripts/plugin_page_preview.py [--html PATH | --check-page] [--new-tabs] [--port N]

Starts the real web server with a stand-in plugin host named ``preview`` and prints two URLs: open
the first to sign in, then the second to see the page under the same headers a plugin page gets.
Dev tooling for the manual browser checks; never packaged.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

CHECK_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Plugin page check</title>
<style>
  #css { color: rgb(0, 128, 0); }
  @font-face { font-family: PfCheck; src: url(data:font/woff2;base64,d09GMgABAAAAAA==); }
  #font { font-family: PfCheck, sans-serif; }
</style>
<script>
  var violations = [];
  document.addEventListener("securitypolicyviolation", function (e) {
    violations.push({directive: e.violatedDirective, blocked: e.blockedURI});
  });
</script>
</head>
<body>
<p id="css">inline style</p>
<span id="attr" style="color: rgb(0, 0, 255)">style attribute</span>
<p id="font">data: font</p>
<img id="img" alt="" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=">
<script type="application/json" id="data">{"ok": true}</script>
<pre id="out">pending</pre>
<p><a id="newtab" href="https://example.com/" target="_blank">example.com in a new tab</a></p>
<p><a id="sametab" href="https://example.com/">example.com in this tab</a></p>
<p><a id="own" href="/settings" target="_blank">PrivacyFence Settings in a new tab</a></p>
<p><button id="nogesture">The page tried window.open on load (no click)</button></p>
<script>
window.addEventListener("load", async function () {
  var opened = window.open("https://example.com/");
  document.getElementById("nogesture").textContent =
    "window.open on load without a click returned " + (opened ? "a window" : "nothing");
  await document.fonts.load("16px PfCheck").catch(function () { return null; });
  document.getElementById("out").textContent = JSON.stringify({
    script: true,
    css: getComputedStyle(document.getElementById("css")).color,
    attr: getComputedStyle(document.getElementById("attr")).color,
    json: JSON.parse(document.getElementById("data").textContent).ok,
    img: document.getElementById("img").naturalWidth,
    violations: violations.map(function (v) {
      return {violatedDirective: v.directive, blockedURI: v.blocked};
    })
  });
});
</script>
</body></html>"""


class PreviewHost:
    """Serves one fixed page as the plugin ``preview``."""

    def __init__(self, body: str, new_tabs: bool = False) -> None:
        self.body = body
        self.new_tabs = new_tabs

    async def web_request(self, name, path, query, principal) -> dict:
        if name != "preview":
            raise LookupError(name)
        return {"status": 200, "headers": {"content-type": "text/html; charset=utf-8"}, "body": self.body}

    def page_new_tabs(self, name) -> bool:
        return self.new_tabs and name == "preview"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve an HTML file or the check page as a plugin page.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--html", metavar="PATH", help="an HTML file to serve")
    source.add_argument("--check-page", action="store_true", help="serve the built-in check page")
    parser.add_argument("--new-tabs", action="store_true", help="serve the page as if page_new_tabs were set")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check_page:
        body = CHECK_PAGE
    else:
        try:
            body = Path(args.html).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"Cannot read {args.html}: {exc}", file=sys.stderr)
            return 2

    from privacyfence import paths as paths_module
    from privacyfence.web.server import WebServer
    from privacyfence.web.session_auth import PROVENANCE_HUMAN
    from privacyfence.web_approval_ui import WebApprovalUI

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / ".privacyfence"
        home.mkdir()
        paths_module.data_dir = lambda: home  # type: ignore[assignment]
        srv = WebServer(WebApprovalUI(), host="localhost", port=args.port, plugin_host=PreviewHost(body, args.new_tabs))
        srv.start()
        try:
            token = srv.bootstrap.mint(provenance=PROVENANCE_HUMAN)
            print(f"Open this first: http://localhost:{args.port}/approvals?bootstrap={token}")
            print(f"Then the page: http://localhost:{args.port}/plugins/preview/")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            srv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
