# ADR 0120: Plugins are out-of-process executables speaking newline-delimited JSON-RPC 2.0 over stdio

## Status

Accepted — 2026-10-07. Implemented: `src/privacyfence/plugins/rpc.py`,
`src/privacyfence/plugins/supervisor.py`, `src/privacyfence/plugins/protocol.py`,
`docs/plugin-protocol/protocol.schema.json`.
Amended by [ADR 0132](0132-a-plugin-that-stops-reading-its-input-for-10-seconds-is-treated-as-crashed.md): a plugin that stops reading its input is a crash.

## Context

PrivacyFence needs a versioned interface through which a separately built and separately released
program extends it
([the tracking issue](https://github.com/privacyfence/privacyfence/issues/846)). The first consumer
is a data-lake plugin in its own repository, and PrivacyFence must learn no data-lake concept from
it. The daemon is a frozen PyInstaller app, so it cannot import third-party Python packages at
run time, and the plugin's author may not use Python at all. Whatever the channel is, the
plugin's output is untrusted input to the daemon.

## Decision

A plugin is its own executable. The daemon starts it as a child process and speaks JSON-RPC 2.0 to
it over the child's stdin and stdout, one JSON object per line, UTF-8, at most 16 MiB per line
(`MAX_LINE_BYTES`). Stdout carries protocol messages only; stderr goes to a private rotating log.
Each side numbers its own requests, a batch is refused, an unknown notification is ignored and an
unknown request gets `method_not_found`.

The protocol version is semver. The manifest names the major, and the daemon and the plugin must
share it, or the plugin is not started. Unknown fields are ignored, so a minor version can add
fields.

The daemon is the only party that decides anything. It validates every message a plugin sends with
hand-written validators (no new dependency), closes a peer after three consecutive invalid lines,
caps in-flight requests at 16 in each direction, and gives each method a timeout. A plugin that
crashes is restarted with backoff, and disabled after five crashes in ten minutes. The child
environment is built from an allow-list, never copied from the daemon's.

`docs/plugin-protocol/protocol.schema.json` is the published description of every message, and the
SDK's types are generated from it ([ADR 0126](0126-the-plugin-sdk-lives-in-this-repository-and-is-published-from-the-same-tag.md)).

## Alternatives considered

- **Import plugin packages into the daemon.** Rejected. The daemon is a frozen app with no
  plugin import path, a plugin could not be built or released separately, and a plugin would run
  inside the daemon's process with its tokens and memory.
- **A local socket or port.** Rejected. It is another surface to secure: any local process could
  connect to it, and the daemon would have to authenticate the peer. A child's stdio is reachable
  only by its parent.
- **`Content-Length` framing (as in the Language Server Protocol).** Rejected. It is harder to write
  in other languages and gives nothing at these sizes: a line is capped at 16 MiB, and the largest
  tool result is 100,000 bytes ([ADR 0092](0092-the-inline-download-limit-caps-the-tool-result-at-100000-bytes.md)).
- **JSON Schema validation with a library.** Rejected for the daemon. The validators are short,
  fail closed and add no dependency; a test checks that they handle exactly the fields the
  published schema lists.

## Consequences

- A plugin can be written in any language that reads and writes lines on stdio.
- A plugin that prints to stdout outside the protocol is disconnected after three such lines.
- The daemon keeps a process per enabled plugin, restarted on a crash and stopped on shutdown.
- The protocol can grow by minor versions without breaking an installed plugin; a major version is
  a different contract and needs a new decision.

## Verification

`tests/unit/plugins/test_rpc.py`, `tests/unit/plugins/test_supervisor.py` (a real child process,
the crash limit, the backoff, the shutdown sequence and the environment allow-list),
`tests/unit/plugins/test_protocol.py` (the validators against the schema) and
`tests/integration/test_plugin_framework.py` (a real plugin process through the daemon).

## Related

- [The tracking issue](https://github.com/privacyfence/privacyfence/issues/846)
- [ADR 0121](0121-a-plugin-is-trusted-code-installed-by-an-administrator-into-an-admin-only-directory.md)
- [ADR 0122](0122-plugin-tools-are-gated-in-two-steps-and-a-read-releases-the-prepared-payload.md)
- [ADR 0126](0126-the-plugin-sdk-lives-in-this-repository-and-is-published-from-the-same-tag.md)
