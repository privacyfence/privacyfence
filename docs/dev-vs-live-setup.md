# Dev vs. live setup

How to run PrivacyFence from a source checkout without colliding with a packaged install, and what
`PRIVACYFENCE_DEV_ALLOW_UNSEPARATED` does and does not change.

## Source runs and packaged installs do not share a machine

Every packaged install (the macOS `.pkg`, the Windows installer, the `.deb`) is privilege-separated:
the daemon runs as a dedicated service account and keeps its data under a system root, described by
a marker file, `privilege-separation.json`, that the installer writes there
([ADR 0003](adr/0003-separated-installs-only.md)).

| Platform | System root (marker lives here) | Service account |
|---|---|---|
| macOS | `/Library/Application Support/PrivacyFence` | `_privacyfence` |
| Linux | `/var/lib/privacyfence` | `privacyfence` |
| Windows | `%ProgramData%\PrivacyFence` | `NT SERVICE\PrivacyFence` |

Every PrivacyFence process reads that marker at startup, including one started from a source
checkout. `privilege_separation.check_runtime_identity()`, called from `daemon_main.main()` before
the config is loaded, refuses to start when:

- the marker exists and the current process is not the service account it names, or
- the marker exists but this build cannot parse it (an unknown `version` or a foreign `platform`).

So `./scripts/dev_start.sh` or `privacyfence-app` from a checkout, on a machine that has a packaged
install, exits with `Configuration error: This install runs the daemon under the dedicated
'<account>' account …, but this process is running as '<you>'. Refusing to start`. This happens
whatever `web.port` is set to, under any OS user account (the marker is machine-wide), and with
`PRIVACYFENCE_DEV_ALLOW_UNSEPARATED` set. `PRIVACYFENCE_SYSTEM_ROOT`, the test-only override that
moves the system root, is ignored while a real marker exists at the platform's default root.

You have two options:

1. **Develop on a separate machine or VM** that has never had a packaged install. This is the
   recommended setup. Test packaged builds on another machine or VM.
2. **Remove the packaged install first.** Each platform's separation tool has
   `uninstall [--purge]` ([ADR 0042](adr/0042-uninstall-replaces-disable.md)):
   - `uninstall` stops and unregisters the service and the companion autostart. It **keeps** the
     data, the marker and the service account. Because the marker stays, a source run still
     refuses to start.
   - `uninstall --purge` (`-Purge` on Windows) also deletes the system root (connector tokens,
     rules, passkeys and audit log, together with the marker) and the service account and group.
     This is the only option that lets a source run start on that machine. It cannot be undone.

   | Platform | Keep data | Delete data and marker |
   |---|---|---|
   | macOS | `sudo "/Applications/PrivacyFence.app/Contents/Resources/scripts/macos_privilege_separation.sh" uninstall` | same, with `--purge` |
   | Linux | `sudo apt remove privacyfence` | `sudo apt purge privacyfence` |
   | Windows | Uninstall PrivacyFence from Settings → Apps | the same, with **Delete PrivacyFence data** ticked |

   See [`platform-support.md`](platform-support.md) for exactly what each platform's uninstall
   removes.

You cannot leave the packaged install stopped but in place, or give the source run a different
port, and expect the source run to start.

## `PRIVACYFENCE_DEV_ALLOW_UNSEPARATED`

This variable is for local development on a machine with no packaged install. It is never for a real
deployment. It counts as set when its value is anything other than empty, `0`, `false` or `False`
(`privilege_separation.dev_allows_unseparated()`).

**When it is honoured:** only on a non-packaged build (`paths.is_bundled()` is false), meaning a
source checkout, an editable install or a `pip`/`pipx` install. On a packaged build the variable has
no effect: `enforce_separation()` has no override, and a packaged build that is not separated
refuses to serve.

**What it allows** on an unseparated, non-packaged install:

- **An explicit `step_up.require_passkey: true` in `config/settings.yaml`.** Without the variable,
  `step_up_config.from_local_config()` rejects that setting at startup. The daemon would otherwise
  check a passkey against a credential store that the agent's own account can write.
- **Enrolling the first passkey without the companion confirming it.** A checkout starts no
  companion. `web/server.py` logs a warning and skips the confirmation.

**What it does not do:**

- It does not protect anything. Approvals on an unseparated install are not protected against the
  AI client they govern. With the variable set, `privilege_separation.dev_unseparated_notice()`
  says so in the startup log and on the `/security` page.
- It does not get past `check_runtime_identity()`. A machine with a packaged install still refuses
  a source run, as described above.
- It does not change any path, port or discovery file.

## State locations

`src/privacyfence/paths.py` is authoritative.

| How PrivacyFence is run | Data directory (`paths.data_dir()`) |
|---|---|
| Source checkout or editable install (`pip install -e .`) | the checkout root (`config/`, `credentials/`, `logs/`, discovery files) |
| Non-editable `pip`/`pipx` install | `~/.privacyfence` (`%LOCALAPPDATA%\PrivacyFence` on Windows) |
| Packaged install (privilege-separated) | the system root above. Files the user's own session reads (`mcp_url`, the control-channel sockets) live in its `handoff/` subdirectory |

The MCPB shim (`mcpb/shim/src/protocol.ts`) looks for `mcp_url` and the control channel under
`handoff/` in the system root when a valid marker exists. Otherwise it looks in
`PRIVACYFENCE_DEV_DATA_DIR` when that is set to an absolute path, and in `~/.privacyfence`
(`%LOCALAPPDATA%\PrivacyFence` on Windows) when it is not. `dev_start.sh` sets the variable to the
checkout's data directory in the entry it registers; a shim registered any other way does not find
a source daemon. The variable is ignored on a privilege-separated install.

## Local web port

The embedded HTTP server (approvals, `/settings`, `/mcp`) listens on `web.port`, which defaults to
`8765`, and binds to localhost only. Only one process can bind a given port. A second daemon started
from the same data directory also stops on the instance lock (`privacyfence.lock`) before it reaches
the port.

## Development/source run

From the repository root:

```bash
./scripts/dev_start.sh
```

The script:

1. creates `.venv` with an editable install if none exists;
2. copies `settings.yaml.example` to `config/settings.yaml` if that file is missing;
3. builds the shim (`mcpb/shim/dist/shim.js`);
4. registers it as the `privacyfence` MCP server, with `PRIVACYFENCE_DEV_DATA_DIR` set to the
   checkout's data directory, through `claude mcp add` if the Claude Code CLI is on `PATH`,
   otherwise in Claude Desktop's `claude_desktop_config.json` on macOS;
5. runs `privacyfence-app` in the foreground.

Ctrl-C stops the daemon and removes the registration.

Use dedicated QA accounts for connector work, never personal or production ones. See
[`connector-qa.md`](connector-qa.md).

## Packaged/release run

Use the artifact from the build path in [`platform-support.md`](platform-support.md), on a machine
or VM with no source run. Treat it as an end-user install: configure connectors and the Claude
integration through the packaged app, and do not rely on the checkout or its venv.

Test packaged behavior against the real artifact, never against a source process. See
[`release-testing.md`](release-testing.md) for the checks only a person can do and
[`testing-policy.md`](testing-policy.md) for automated coverage, including the
`pytest.mark.packaged` smoke tests that `build.yml` runs against each built installer.
