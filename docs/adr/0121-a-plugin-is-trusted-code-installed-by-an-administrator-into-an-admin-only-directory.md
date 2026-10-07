# ADR 0121: A plugin is trusted code, installed by an administrator into an admin-only directory

## Status

Accepted — 2026-10-07. Implemented: `src/privacyfence/plugins/trust.py`,
`src/privacyfence/plugins/state.py`, `src/privacyfence/plugins/host.py`,
`src/privacyfence/plugins/tools.py`, `src/privacyfence/privilege_separation.py`
(`admin_only_write_problem`, `admin_only_ancestor_write_problem`), `src/privacyfence/web/routes_settings.py`.

## Context

A plugin runs as the service account, the same account that holds every connector credential, and
its source API ([ADR 0123](0123-the-plugin-source-api-is-ungated-but-audited-without-content.md))
reads connected services with no approval card. Anyone who can replace a plugin's executable, or
any directory on the way to it, can therefore run code as the service account and read the user's
data. That person must not be the user, and so must not be the AI client acting as the user: it is
the same boundary that [ADR 0058](0058-nothing-runs-elevated-unless-only-an-administrator-can-rewrite-it.md)
draws around anything that runs elevated.

## Decision

**Where plugins live.** A plugin is a directory holding its executable and its manifest, inside an
administrator-only plugins directory that is outside the installed app (which upgrades replace) and
outside the service account's data root (which the service account can write): `/usr/local/lib/privacyfence/plugins`
on Linux, `/Library/PrivacyFence/plugins` on macOS and `%ProgramFiles%\PrivacyFence Plugins` on
Windows. The directory is not configurable and no environment variable overrides it, because a
configurable path could point somewhere the user can write.

**What is checked, on every start.** The executable, every directory between it and the plugin
directory, the plugin directory and every ancestor of it up to the filesystem root must be writable
by administrators only. A symlink on the way is followed, so the directories checked are the ones
the operating system walks. On POSIX the rule is the one the elevation scripts already use: owned
by root, with no group or other write bit. On Windows it is the DACL rule: no write-granting entry
for a trustee outside the trusted set.

For directory ancestors only, the Windows rule ignores inherit-only entries and the right to create
a subdirectory (add-subdirectory), because neither lets a non-administrator rename, replace or
delete a directory that already exists, and a default `C:\` grants both to every signed-in user.
The executable and the plugin directory keep the strict rule, and POSIX has the same rule for all
of them. A consequence on POSIX: some older Debian installs make `/usr/local` and `/usr/local/lib`
group-writable by `staff` (mode 2775), and plugins are refused there until the directory is made
administrator-only.

**What runs.** Plugins start only when privilege separation is on, which every packaged install
has. `plugins.enabled: false` stops them all. A plugin is never started until a human has enabled
it: Settings shows a "Review and enable" card listing the executable's and manifest's sha256
hashes, the gate floor, the source operations, whether it serves pages and every tool with its
gate. Enabling is sensitive (it needs a human session and, where required, a passkey), as
[ADR 0070](0070-enabling-a-connector-is-sensitive-and-disabling-is-not.md) decides for connectors.
Introspection, which starts the plugin only to read its tool list and refuses source reads and
confirmations, is not sensitive: it can neither read data nor change state, and the binary is
already administrator-installed. The daemon refuses to enable unless the files still hash to what
was reviewed.

**What changes after enable.** The hashes are recorded at enable, and any difference at a later
start disables the plugin with "executable or manifest changed, enable again". The signatures of
the reviewed tools (name, gate, read-only, destructive, scopes) are recorded too. A plugin may drop
tools afterwards but never add or re-gate one: a tool list at start or in `tools.changed` that
holds an unreviewed tool or signature is refused, and at start it disables the plugin with the same
reason, so a plugin cannot widen what it exposes by restarting.

**The floor.** A tool on the `auto` gate, a read as much as a write, needs the manifest's
`max_gate_floor: auto`, which the owner approves at enable. A plugin reads connected services
ungated, so an `auto` read tool would hand connector data to the AI with no card. A destructive
tool must use the `popup` gate.

**Uninstall.** A plugin that has a state record and no directory is uninstalled: its data
directories, its auto-accept rules (every predicate starting `plugin:<name>:`) and its state record
are deleted, so a different plugin installed later under the same name inherits nothing. This runs
only after a successful listing of the plugins directory. If the directory is missing or
unreadable, nothing is deleted and every known plugin shows "plugins directory unreadable".

## Alternatives considered

- **A configurable plugins directory.** Rejected. The path could point somewhere the user can write.
- **A plugins directory under the data root or the app.** Rejected. The service account can write
  the data root, and upgrades replace the app.
- **Check the executable only.** Rejected. A user who can rename a parent directory can swap the
  chain to a harmless-looking executable.
- **Hold Windows ancestors to the strict rule.** Rejected. No plugin on a standard Windows install
  could pass: `C:\` grants every signed-in user the right to create folders.
- **Run plugins as their own OS account, or in a sandbox.** Rejected for protocol 1. It needs an
  account and a sandbox per OS and is not what the first consumer needs. The residual risk is
  recorded below.
- **Gate `auto` only for writes.** Rejected. See "The floor".

## Consequences

- Installing a plugin takes an administrator. That is the point.
- A plugin is trusted with everything the service account can read. In particular it can read the
  connectors' credential files; nothing in protocol 1 prevents that, and the owner's approval at
  enable is the control. This residual risk is stated in `docs/security-and-compliance.md`.
- A rebuilt plugin must be reviewed and enabled again, and an upgrade that adds a tool does too.
- A host whose `/usr/local` is group-writable refuses plugins until that is fixed.

## Verification

`tests/unit/plugins/test_trust.py` (locations per OS, the ancestor walk, discovery, hashes),
`tests/unit/plugins/test_state.py` (hash drift disables), `tests/unit/plugins/test_tools.py` (the
floors and the reviewed set), `tests/unit/plugins/test_host.py` (enable refuses changed files,
uninstall deletes data and rules only after a successful listing),
`tests/unit/test_privilege_separation.py`, `tests/platform/test_plugin_dir_permissions.py` and
`tests/integration/test_plugin_refusals.py` (a user-writable plugin, a changed executable and an
unreviewed tool, each against a real process).

## Related

- [The tracking issue](https://github.com/privacyfence/privacyfence/issues/846)
- [ADR 0058](0058-nothing-runs-elevated-unless-only-an-administrator-can-rewrite-it.md)
- [ADR 0070](0070-enabling-a-connector-is-sensitive-and-disabling-is-not.md)
- [ADR 0120](0120-plugins-are-out-of-process-executables-speaking-json-rpc-over-stdio.md)
- [ADR 0122](0122-plugin-tools-are-gated-in-two-steps-and-a-read-releases-the-prepared-payload.md)
- [ADR 0123](0123-the-plugin-source-api-is-ungated-but-audited-without-content.md)
