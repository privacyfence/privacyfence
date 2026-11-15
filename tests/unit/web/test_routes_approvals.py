"""Tests for web/routes_approvals.py's runtime bridge shim: the organization
shim forwards every key of a card's payload to the decide route. The routes
themselves are exercised in test_routes_org_approvals.py."""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence.principal import Principal
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session, routes_approvals
from privacyfence.web_approval_ui import WebApprovalUI

ISSUER = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")


class TestBatchDecideBody:
    def test_a_body_that_is_not_json_is_a_400(self):
        sessions = org_session.OrgSessionStore()
        routes = routes_approvals.build_routes(
            web_ui=WebApprovalUI(), sessions=sessions, step_up=StepUpConfig(), issuer_url=ISSUER,
        )
        client = TestClient(Starlette(routes=routes), base_url=ISSUER)
        client.cookies.set(org_session.SESSION_COOKIE, sessions.create(ALICE))

        r = client.post("/api/approvals/batch/decide", content=b"not json", headers={"Content-Type": "application/json"})

        assert r.status_code == 400
        assert r.json()["error"] == "invalid JSON body"


# Runs one bridge shim's own <script> (the second of the two it returns; the first is
# PF_WEBAUTHN_JS) in node, with fetch, sessionStorage and location stubbed, posts one payload
# through window.webkit.messageHandlers.pf, and prints what was sent and what the list would toast.
_SHIM_HARNESS_JS = r"""
const vm = require('vm');
const [script, payloadJson, status] = [process.argv[1], process.argv[2], Number(process.argv[3])];
const seen = {bodies: [], toast: null, navigated: null};
const sandbox = {
  document: {body: {innerHTML: ''}},
  sessionStorage: {setItem: (key, value) => { if (key === 'pf_toast') seen.toast = JSON.parse(value).msg; }},
  fetch: (url, init) => {
    seen.bodies.push(JSON.parse(init.body));
    return Promise.resolve({ok: status >= 200 && status < 300, status, json: () => Promise.resolve({})});
  },
};
sandbox.window = sandbox;
sandbox.window.location = {replace: (url) => { seen.navigated = url; }};
vm.createContext(sandbox);
vm.runInContext(script, sandbox);
sandbox.window.webkit.messageHandlers.pf.postMessage(JSON.parse(payloadJson));
setTimeout(() => process.stdout.write(JSON.stringify(seen)), 50);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
class TestBridgeShimsForwardDenyFeedback:
    """The shim sends every key of the card's payload to the decide route unchanged, so the
    card's note and intent reach it without the shim knowing about them (ADR 0083). A deny
    that carried either toasts _DENIED_WITH_NOTE_MESSAGE; a plain one still toasts
    _DENIED_MESSAGE."""

    def _run(self, payload: dict, *, status: int = 200) -> dict:
        shim = routes_approvals._org_decide_shim(
            decide_url="/api/approvals/a1/decide", csrf="csrf-1", stepup_options_url="/x", nonce="n",
        )
        scripts = re.findall(r'<script nonce="n">(.*?)</script>', shim, flags=re.S)
        assert len(scripts) == 2
        out = subprocess.run(
            ["node", "-e", _SHIM_HARNESS_JS, scripts[1], json.dumps(payload), str(status)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return json.loads(out.stdout)

    def test_note_and_intent_are_forwarded_unchanged(self):
        note = 'Only Anna "please" \\ not the team\nthanks'
        seen = self._run({"action": "resolve", "result": "deny", "note": note, "intent": "wrong_target"})
        assert seen["bodies"] == [{
            "action": "resolve", "result": "deny", "note": note, "intent": "wrong_target", "csrf": "csrf-1",
        }]
        assert seen["toast"] == routes_approvals._DENIED_WITH_NOTE_MESSAGE
        assert seen["navigated"] == "/approvals"

    @pytest.mark.parametrize("extra", [{"intent": "stop"}, {"note": "x"}])
    def test_either_part_alone_gets_the_noted_toast(self, extra):
        seen = self._run({"action": "resolve", "result": "deny", **extra})
        assert seen["bodies"][0] == {"action": "resolve", "result": "deny", **extra, "csrf": "csrf-1"}
        assert seen["toast"] == routes_approvals._DENIED_WITH_NOTE_MESSAGE

    def test_a_plain_deny_is_unchanged(self):
        seen = self._run({"action": "resolve", "result": "deny"})
        assert seen["bodies"] == [{"action": "resolve", "result": "deny", "csrf": "csrf-1"}]
        assert seen["toast"] == routes_approvals._DENIED_MESSAGE

    def test_an_allow_never_gets_the_noted_toast(self):
        seen = self._run({"action": "resolve", "result": "accept"})
        assert seen["toast"] == routes_approvals._DECIDED_MESSAGE
