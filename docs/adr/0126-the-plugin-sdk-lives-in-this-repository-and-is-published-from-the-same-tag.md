# ADR 0126: The plugin SDK lives in this repository and is published from the same tag

## Status

Accepted — 2026-10-07. Implemented: `plugin-sdk/`, `scripts/gen_plugin_sdk_types.py`,
`.github/workflows/publish-pypi.yml`.

## Context

A plugin author needs the protocol handled for them, and a way to test a plugin without a running
PrivacyFence. The SDK and the daemon must agree on every message, every limit and every refusal, or
a plugin that passes its tests fails when it is installed.

## Decision

The SDK, `privacyfence-plugin-sdk`, is a zero-dependency Python package in `plugin-sdk/`, in this
repository. The existing publish workflow builds and publishes it, to TestPyPI and then PyPI with
OIDC trusted publishing ([ADR 0020](0020-pypi-publishing-uses-oidc-trusted-publisher-only.md)), from
the same stable tag as the daemon. Its version comes from that tag. It never reaches the R2 archive.

The SDK does not import `privacyfence`. It reimplements framing, the block rules and the
tool-definition floors, and a test asserts its limits equal the daemon's constants. Its protocol
types are generated from `docs/plugin-protocol/protocol.schema.json`, and a test fails when the
generated file is stale.

The SDK ships a public test host, `privacyfence_plugin_sdk.testing.PluginTestHost`, with a
semver-stable API. It runs the plugin's real runner over an in-memory stream pair and plays the
daemon's side: tool-definition floors, block limits, a simulated gate, scope rules, source
fixtures, pages with the daemon's headers, confirmations, events, purge and shutdown. Its source
samples are hand-written, redacted examples, one per source operation, and a test runs each
through the daemon's adapter shape check, so a drift between a sample and the daemon fails CI. A
conformance test runs the same scenarios against the test host and the real daemon.

The test host and the daemon differ where they must: the test host does not implement
introspection or plugin-sent `tools.changed`, it reports a rule-accepted call's `approval.via` as
`rule` where the daemon reports `card`, and the daemon's audit decision `rejected` is the test
host's `denied`.

## Alternatives considered

- **A separate SDK repository.** Rejected. The schema, the daemon's limits and the SDK would drift
  between releases, and a daemon change would need two coordinated releases.
- **Depend on `privacyfence` from the SDK.** Rejected. A plugin's own build must not pull in the
  daemon and its dependencies.
- **Samples copied from the daemon's live fixtures.** Rejected. Four of the six operations have no
  recorded fixture, and the normalized shapes are produced by the daemon.
- **A schema-validation dependency in the SDK.** Rejected. The SDK has no dependencies, and the
  validators are checked against the schema by tests.

## Consequences

- A protocol change and its SDK change are one pull request and one release.
- The first publish of the SDK needs the pending trusted publisher registered on both indexes, once.
- The test host is a public API: a change that breaks a plugin author's test is a breaking change.

## Verification

`tests/unit/plugin_sdk/` (the runner and the test host), `tests/unit/test_gen_plugin_sdk_types.py`
(the generated types are current), `tests/unit/plugins/test_sdk_samples.py` (the samples against the
daemon) and `tests/integration/test_sdk_testhost_conformance.py` (the test host against the real
daemon).

## Related

- [The tracking issue](https://github.com/privacyfence/privacyfence/issues/846)
- [ADR 0020](0020-pypi-publishing-uses-oidc-trusted-publisher-only.md)
- [ADR 0120](0120-plugins-are-out-of-process-executables-speaking-json-rpc-over-stdio.md)
