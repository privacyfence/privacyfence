"""Stand-in plugin for supervisor tests: stdlib only, speaks the line-framed JSON-RPC protocol.

Usage: ``stub_plugin.py <mode>`` with a mode of ``ok``, ``crash-on-start``, ``crash-after-init``,
``bad-version``, ``junk-stdout``, ``slow-shutdown``, ``echo-env``, ``wrong-name``,
``stop-reading`` (answers initialize, then never reads stdin again) or
``spawn-child`` (starts a sleeping child process and writes ``CHILD:<pid>`` to stderr) or
``spam-stderr`` (writes about 30 KB to stderr right after the initialize result, then ``SPAM_DONE``) or
``pages-invalid`` (answers ``pages.list`` with an entry that has no title) or
``pages-slow`` (never answers ``pages.list``).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "ok"


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def initialize_result(params: dict) -> dict:
    plugin = params.get("plugin", {})
    return {
        "protocol_version": "2.0.0" if MODE == "bad-version" else "1.0.0",
        "plugin": {
            "name": "other" if MODE == "wrong-name" else plugin.get("name", "stub"),
            "version": plugin.get("manifest_version", "0.0.0"),
        },
        "scope_types": [],
        "tools": [],
    }


def main() -> int:
    sys.stdout.reconfigure(newline="\n")  # type: ignore[union-attr]
    if MODE == "crash-on-start":
        print("stub: crashing on start", file=sys.stderr, flush=True)
        return 1
    if MODE == "spawn-child":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        )
        print(f"CHILD:{child.pid}", file=sys.stderr, flush=True)
    if MODE == "echo-env":
        print("ENV:" + ",".join(sorted(os.environ)), file=sys.stderr, flush=True)
    if MODE == "slow-shutdown" and hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    deadline = time.monotonic() + 60  # never outlive a broken test
    for line in sys.stdin:
        if time.monotonic() > deadline:
            return 2
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get("method")
        if method == "initialize":
            if message.get("params", {}).get("purpose") == "introspect":
                # Introspection must not reach the host's source or confirmation services.
                send({"jsonrpc": "2.0", "id": "s1", "method": "source.call", "params": {}})
                reply = json.loads(sys.stdin.readline())
                print("SOURCE_CALL:" + reply.get("error", {}).get("data", {}).get("code", "?"),
                      file=sys.stderr, flush=True)
            send({"jsonrpc": "2.0", "id": message["id"], "result": initialize_result(message.get("params", {}))})
            if MODE == "spam-stderr":
                for _ in range(300):
                    print("x" * 99, file=sys.stderr)
                print("SPAM_DONE", file=sys.stderr, flush=True)
            if MODE == "crash-after-init":
                return 3
            if MODE == "stop-reading":
                while time.monotonic() < deadline:
                    time.sleep(0.1)
                return 2
            if MODE == "junk-stdout":
                for _ in range(3):
                    sys.stdout.write("this is not json-rpc\n")
                sys.stdout.flush()
        elif method == "shutdown":
            if MODE != "slow-shutdown":
                return 0
        elif method == "pages.list" and MODE == "pages-invalid":
            send({"jsonrpc": "2.0", "id": message["id"], "result": {"pages": [{"path": "/"}]}})
        elif method == "pages.list" and MODE == "pages-slow":
            continue
        elif "id" in message and method is not None:
            send({
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {
                    "code": -32601,
                    "message": "method_not_found",
                    "data": {"code": "method_not_found", "detail": method, "retryable": False},
                },
            })
    while MODE == "slow-shutdown" and time.monotonic() < deadline:
        time.sleep(0.1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
