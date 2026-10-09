"""Stand-in for a plugin that reports tool definitions the daemon must refuse (ADR 0121).

The SDK refuses these at registration, so a plugin built on it cannot reproduce them. Usage:
``variant_plugin.py <tools.json>``: it answers ``initialize`` with the tools in that file and
exits on ``shutdown``. Stdlib only.
"""
from __future__ import annotations

import json
import sys


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as handle:
        tools = json.load(handle)
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get("method")
        if method == "initialize":
            plugin = message["params"]["plugin"]
            send({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocol_version": "1.0.0",
                "plugin": {"name": plugin["name"], "version": plugin["manifest_version"]},
                "scope_types": [],
                "tools": tools,
            }})
        elif method == "shutdown":
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
