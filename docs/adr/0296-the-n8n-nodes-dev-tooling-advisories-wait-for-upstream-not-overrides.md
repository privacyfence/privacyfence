# ADR 0296: The n8n node's dev-tooling advisories wait for upstream, not overrides

## Status

Accepted — 2026-11-14. Implemented.
Relates to [ADR 0289](0289-the-n8n-node-ships-from-this-repository-and-is-published-with-provenance.md).

## Context

Five Dependabot security-update runs fail on `main` for `/n8n`: zod, uuid, qs, minimatch and stream-json. Each log
says the package "can't be automatically updated to a non-vulnerable version", because n8n's own packages pin the
vulnerable versions exactly. Upstream n8n has no fixed release (checked 2026-11-14): `@n8n/config@2.45.1` and
`@n8n/backend-network@1.21.4` still pin `zod@3.25.76` and `qs@6.16.0`, `@n8n/backend-common@1.47.4` pins
`stream-json@1.9.1`, and every zod 3.x is in the advisory range `<4.6.6`.

Every vulnerable copy is build, lint or test tooling, or the `n8n-workflow` peer that the n8n host provides at
runtime. The published package ships only `dist/` and has no runtime `dependencies` (ADR 0289). But the lock file
installed `n8n-workflow@3.2.0` as a non-dev package, because it is a peer, so `npm audit --omit=dev` counted its
tree as runtime and reported three high findings. The `npm-audit` job's n8n step was made informational for that
reason, which also hid any real runtime finding.

The repository's usual remedy for an unpatched transitive dependency is `overrides`. It does not work here.

## Decision

- `n8n-workflow` is also a `devDependency`, pinned to the exact version the lock file already resolves (`3.2.0`).
  The `peerDependencies` entry (`"*"`) is unchanged, and the package still has no `dependencies`. The lock file then
  marks the `n8n-workflow` tree `"dev": true`.
- The `npm-audit` job's n8n step runs `npm audit --omit=dev --audit-level=high` and blocks. It guards against a
  runtime dependency being added later. The whole tree, dev included, is reported informationally.
- Findings in the dev tree that come from n8n's own pins stay until upstream moves. Their Dependabot alerts are
  dismissed as tolerable risk, citing this ADR.

## Alternatives considered

- **`overrides`** — `n8n-node lint` fails with `@n8n/community-nodes/no-overrides-field`: the field is not allowed in
  community node packages. Overriding `zod` to 4.x also breaks `n8n-workflow` at load time
  (`zod_1.z.string(...).ip is not a function`).
- **Leave the runs red** — five permanently failing runs hide a real future failure.
- **Drop the peer install** — the build, the typecheck and the tests import `n8n-workflow`, so it has to be installed.

## Consequences

- The dev tooling (`@n8n/node-cli`, eslint, prettier, tsx, typescript and n8n's pins under them) runs in the publish
  job of `publish-n8n-node.yml`, next to `id-token: write` and `NPM_TOKEN`. All five advisories are denial-of-service
  or bounds-check bugs that need attacker-shaped input, and this tooling reads only this repository's own files, so
  the risk is accepted.
- A new advisory in n8n's pins opens a new Dependabot alert and one more failing run. It is dismissed the same way.
- `@n8n/node-cli` and `n8n-workflow` are bumped together by the existing `n8n-node-dependencies` Dependabot group.

## Verification

- The blocking n8n step of the `npm-audit` job in `.github/workflows/dependency-audit.yml`.
- The `"dev": true` flags on `node_modules/zod` and `node_modules/n8n-workflow` in `n8n/package-lock.json`.

## Related

- Issue https://github.com/privacyfence/privacyfence/issues/1116
- Failing run https://github.com/privacyfence/privacyfence/actions/runs/43634322241
- [ADR 0289](0289-the-n8n-node-ships-from-this-repository-and-is-published-with-provenance.md)
- [ADR 0292](0292-the-n8n-node-carries-its-own-handshake-only-mcp-client.md)
