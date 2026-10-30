"""website/tools/connector-config/index.html: the browser page that signs connector configs.

The page handles a signing key and a client secret, so it must send nothing anywhere: no outside
resource, a Content-Security-Policy whose hashes match its inline script and style, no form. Its
script is also run with node, whose WebCrypto is the same Ed25519 the browsers use, to prove it
signs the shared fixtures to exactly the bytes the Python tool and the server expect.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from privacyfence import connector_configs
from tests.connector_config_support import FIXTURE_DIR, expected_file, fixture_inputs

pytestmark = pytest.mark.unit

PAGE = Path(__file__).resolve().parents[2] / "website" / "tools" / "connector-config" / "index.html"
HTML = PAGE.read_text(encoding="utf-8")


def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'"


class _Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.blocks: dict[str, list[str]] = {"script": [], "style": []}
        self._open: str | None = None

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag in self.blocks:
            self._open = tag
            self.blocks[tag].append("")

    def handle_endtag(self, tag):
        if tag == self._open:
            self._open = None

    def handle_data(self, data):
        if self._open:
            self.blocks[self._open][-1] += data


PARSED = _Elements()
PARSED.feed(HTML)


def _csp() -> dict[str, list[str]]:
    metas = [a for tag, a in PARSED.tags if tag == "meta" and (a.get("http-equiv") or "").lower() == "content-security-policy"]
    assert len(metas) == 1
    directives = {}
    for part in metas[0]["content"].split(";"):
        name, *sources = part.split()
        directives[name] = sources
    return directives


# ---- sends nothing anywhere --------------------------------------------------------------------


def test_the_policy_forbids_every_network_and_form_use():
    csp = _csp()
    assert csp["default-src"] == ["'none'"]
    assert csp["connect-src"] == ["'none'"]
    assert csp["form-action"] == ["'none'"]
    assert csp["base-uri"] == ["'none'"]
    assert set(csp) == {"default-src", "script-src", "style-src", "connect-src", "form-action", "base-uri"}


def test_the_policy_hashes_match_the_inline_script_and_style():
    csp = _csp()
    assert len(PARSED.blocks["script"]) == 1 and len(PARSED.blocks["style"]) == 1
    assert csp["script-src"] == [_hash(PARSED.blocks["script"][0])]
    assert csp["style-src"] == [_hash(PARSED.blocks["style"][0])]
    assert "'unsafe-inline'" not in HTML and "'unsafe-eval'" not in HTML


def test_no_external_resource_is_loaded():
    for tag, attrs in PARSED.tags:
        if tag in {"script", "link", "img", "iframe", "frame", "object", "embed", "audio", "video", "source", "track", "base"}:
            for attr in ("src", "href", "srcset", "data"):
                assert attrs.get(attr) is None, f"<{tag} {attr}={attrs.get(attr)!r}>"
    assert not [tag for tag, _ in PARSED.tags if tag == "link"]
    assert "@import" not in HTML and "url(" not in PARSED.blocks["style"][0]


def test_there_is_no_form_and_nothing_submits():
    tags = {tag for tag, _ in PARSED.tags}
    assert "form" not in tags
    assert not [a for tag, a in PARSED.tags if tag == "button" and a.get("type") != "button"]
    assert not [a for tag, a in PARSED.tags if tag == "input" and a.get("type") == "submit"]


def test_the_script_has_no_network_api_and_no_dynamic_code():
    script = PARSED.blocks["script"][0]
    for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "EventSource", "eval(", "Function(",
                      "importScripts", "localStorage", "sessionStorage", "indexedDB", "document.cookie", "innerHTML",
                      "window.open", "location."):
        assert forbidden not in script, forbidden
    # No inline handler attributes or style attributes either: the policy would block them.
    for tag, attrs in PARSED.tags:
        assert not [a for a in attrs if a.startswith("on") or a == "style"], (tag, attrs)


def test_the_page_is_not_indexed_and_says_what_it_is():
    metas = {(a.get("name") or "").lower(): a.get("content") for tag, a in PARSED.tags if tag == "meta"}
    assert metas["robots"] == "noindex, nofollow"
    assert metas["referrer"] == "no-referrer"
    assert "runs only in your browser" in HTML
    assert "privacyfence-connector-config-tool.html" in HTML
    assert "offline copy" in HTML
    assert "This browser cannot sign Ed25519; use scripts/sign_connector_config.py." in HTML


def test_the_page_is_ascii_safe_to_hash():
    assert HTML.isascii()


# ---- the script signs what the Python tool signs ------------------------------------------------

_HARNESS = """
const fs = require('fs'), vm = require('vm');
if (!globalThis.crypto) globalThis.crypto = require('crypto').webcrypto;
const [script, inputs, keyPath, job] = process.argv.slice(2);
vm.runInThisContext(fs.readFileSync(script, 'utf8'));
const tool = globalThis.pfConnectorTool;
const pem = fs.readFileSync(keyPath, 'utf8');
(async () => {
  const out = {};
  if (job === 'sign') {
    const specs = JSON.parse(fs.readFileSync(inputs, 'utf8'));
    for (const [provider, spec] of Object.entries(specs)) {
      const made = await tool.signConfig({
        provider, serial: String(spec.serial), signedAt: spec.signed_at, values: Object.fromEntries(spec.fields),
      }, pem);
      out[provider] = made;
    }
  } else if (job === 'keygen') {
    out.made = await tool.generateKey();
    out.again = await tool.keyFingerprint(out.made.pem);
    out.fixture = await tool.keyFingerprint(pem);
    out.serial = tool.todaySerial();
  } else {
    const attempts = JSON.parse(fs.readFileSync(inputs, 'utf8'));
    out.errors = [];
    for (const [input, key] of attempts) {
      try { await tool.signConfig(input, key === null ? pem : key); out.errors.push(null); }
      catch (e) { out.errors.push(e.message); }
    }
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI to prove the page signs the same bytes as the Python tool")
        pytest.skip("node is not installed")
    return node


@pytest.fixture(scope="module")
def run_node(tmp_path_factory):
    node = _node()
    work = tmp_path_factory.mktemp("web_tool")
    (work / "script.js").write_text(PARSED.blocks["script"][0], encoding="utf-8")
    (work / "harness.js").write_text(_HARNESS, encoding="utf-8")

    def run(job: str, inputs: Path | None = None) -> dict:
        result = subprocess.run(
            [node, str(work / "harness.js"), str(work / "script.js"), str(inputs or ""),
             str(FIXTURE_DIR / "key.pem"), job],
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    run.work = work  # type: ignore[attr-defined]
    return run


@pytest.mark.parametrize("provider", sorted(fixture_inputs()))
def test_the_script_signs_the_fixtures_byte_for_byte(run_node, provider):
    made = run_node("sign", FIXTURE_DIR / "inputs.json")[provider]
    assert made["text"].encode("ascii") == expected_file(provider)
    parsed = connector_configs.parse(made["text"].encode("ascii"))
    assert connector_configs.verify(parsed, parsed.key)
    assert made["fingerprint"] == connector_configs.fingerprint(parsed.key)


def test_a_generated_key_loads_in_python_and_shows_the_servers_fingerprint(run_node):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_private_key

    out = run_node("keygen")
    key = load_pem_private_key(out["made"]["pem"].encode("ascii"), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert out["made"]["fingerprint"] == connector_configs.fingerprint(public)
    assert out["again"] == out["made"]["fingerprint"]
    fixture_public = load_pem_private_key((FIXTURE_DIR / "key.pem").read_bytes(), password=None).public_key()
    assert out["fixture"] == connector_configs.fingerprint(fixture_public.public_bytes(Encoding.Raw, PublicFormat.Raw))
    assert re.fullmatch(r"\d{8}01", out["serial"])
    assert re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", out["made"]["fingerprint"])


def test_the_script_refuses_what_the_format_forbids(run_node):
    good = {"provider": "slack", "serial": "3", "signedAt": "2026-11-02T10:00:00Z",
            "values": {"client_id": "id", "client_secret": "secret"}}

    def variant(**changes):
        data = json.loads(json.dumps(good))
        for key, value in changes.items():
            if key in good["values"]:
                data["values"][key] = value
            else:
                data[key] = value
        return data

    attempts = [
        [good, None],
        [variant(serial="03"), None],
        [variant(serial="0"), None],
        [variant(serial="1" * 16), None],
        [variant(serial="1e3"), None],
        [variant(provider="nope"), None],
        [variant(client_secret=""), None],
        [variant(client_secret=" padded"), None],
        [variant(client_secret="café"), None],
        [variant(client_secret="line\nbreak"), None],
        [variant(client_secret="x" * 513), None],
        [variant(signedAt="2026-11-02 10:00:00"), None],
        [good, "not a key"],
        [good, "-----BEGIN PRIVATE KEY-----\n!!!\n-----END PRIVATE KEY-----"],
    ]
    path = run_node.work / "attempts.json"
    path.write_text(json.dumps(attempts), encoding="utf-8")
    errors = run_node("refuse", path)["errors"]
    assert errors[0] is None
    assert all(errors[1:]), errors
    # A refusal names the field, never the value it was given.
    assert not [e for e in errors if e and any(leak in e for leak in ("padded", "caf", "line", "xxxx", "!!!"))]
