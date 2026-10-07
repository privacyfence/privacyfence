# Plugins

A plugin is a separate program that adds tools to PrivacyFence. An administrator installs it, you
review what it can do and enable it, and from then on its tools appear to your AI client next to the
connectors' tools. PrivacyFence gates a plugin's tools exactly as it gates a connector's: a card
shows what a call would release or do, and nothing happens until you approve it.

This page is for administrators who install plugins and for authors who write them. The message
formats are in [`plugin-protocol.md`](plugin-protocol.md); the reasons behind the design are in
[ADR 0120](adr/0120-plugins-are-out-of-process-executables-speaking-json-rpc-over-stdio.md) to
[ADR 0126](adr/0126-the-plugin-sdk-lives-in-this-repository-and-is-published-from-the-same-tag.md).

## What a plugin is

A plugin is its own executable with a manifest, `privacyfence-plugin.yaml`, next to it. PrivacyFence
starts it as a child process, under the service account, and talks to it over its standard input and
output. A plugin can:

- **Add MCP tools**, each on one of three gates: `auto` (no card), `review` (a card shows what a
  read would release) and `popup` (a card asks before a write). The AI client receives exactly
  what the card showed.
- **Read from your connected services** (Calendar, Drive and Sheets, Jira, Confluence and
  Salesforce reports) without ever holding a token. These reads show no card, so the list of
  operations a plugin may use is part of what you approve when you enable it, and every read is
  written to the audit log (the target and the size, never the content).
- **Ask you to confirm something** on a card that no "Always allow" rule can accept.
- **Serve read-only pages** at `/plugins/<name>/`, in a sandbox, to you only.
- **Keep its own files** in folders only the service account can read.

Plugins run only on a packaged install, where PrivacyFence runs as its own service account. A `pip`
or source install shows every plugin as "plugins need PrivacyFence's background service".
Organization deployments do not run plugins.

## Trust

A plugin runs as the service account, the account that holds your connector credentials. It is
trusted code. That is why:

- Only an administrator can install one: the plugins directory, the plugin's folder and every
  folder above it must be writable by administrators only.
- You enable each plugin yourself, with your passkey where your install asks for one, after seeing
  its tools, gates and reads.
- A plugin that changes after you enabled it stops until you review it again.

PrivacyFence does not sandbox a plugin and gives it no account of its own. An enabled plugin can read
anything the service account can read, including connector credential files, and an AI client cannot
change that. Install only plugins you trust as you would trust any program an administrator runs.
[Security and compliance](security-and-compliance.md#plugins) states the model and its residual risk.

## The plugins directory

| OS | Plugins directory |
|---|---|
| Linux | `/usr/local/lib/privacyfence/plugins` |
| macOS | `/Library/PrivacyFence/plugins` |
| Windows | `%ProgramFiles%\PrivacyFence Plugins` (usually `C:\Program Files\PrivacyFence Plugins`) |

The directory is not configurable and no environment variable changes it. It must be writable by
administrators only, and so must every folder above it, up to the root of the drive. Create it as an
administrator (`sudo` on Linux and macOS, an elevated prompt on Windows). The directory is outside
the installed app, so upgrades keep your plugins.

On POSIX a folder is administrator-only when it is owned by root and has no group or other write
permission. Some older Debian installs make `/usr/local` and `/usr/local/lib` group-writable by
`staff` (mode 2775); plugins are refused there until you make those directories administrator-only
(`sudo chmod g-w /usr/local /usr/local/lib`). On Windows, a folder is administrator-only when no
account but administrators, SYSTEM and TrustedInstaller can write to it. For the folders above the
plugin's own, PrivacyFence ignores two grants a default drive root gives every signed-in user:
inherited-only entries and the right to create subfolders, because neither lets anyone swap an
existing folder. The executable and the plugin's own folder get the strict rule.

## Installing a plugin

1. Get the plugin's folder: its executable (for example `today-plugin`, `today-plugin.exe` on
   Windows) and `privacyfence-plugin.yaml`. The folder is named after the plugin's `name`.
2. As an administrator, copy the folder into the plugins directory. Make sure the copy is owned by
   an administrator and not writable by anyone else.
3. Open **Settings → Plugins** and choose **Rescan**. The plugin shows as **Not enabled**, or as
   **Rejected** with the reason (see [Why a plugin does not start](#why-a-plugin-does-not-start)).
4. Choose **Review and enable**. PrivacyFence starts the plugin once to read its tool list, with
   reads and confirmations switched off, then shows a card listing every tool with its gate
   (**Runs without asking**, **Review** or **Popup**), whether it reads or writes and whether it is
   destructive, the connector reads it may make, whether it serves pages, and whether any tool runs
   without asking. Check each one.
5. Choose **Enable**. Enabling needs a human session and, where your install requires it, your
   passkey. PrivacyFence checks again that the files are the ones you reviewed, records their
   hashes and the tools you saw, and starts the plugin.

A running plugin shows **Running**, and its tools appear to the AI client, which is told that the
tool list changed. A plugin with pages shows an **Open page** link.

A plugin can later drop tools, but it cannot add a tool or change a tool's gate without another
review: a rebuilt plugin whose tool list differs is disabled with "executable or manifest changed,
enable again".

## Using a plugin's tools

A plugin's tool is named `<plugin>_<tool>` (for example `today_list_events`) and takes a required
`reason` like any gated tool. A gated call opens an approval card. The card shows the plugin's name
and tool, and the plugin's own description of the call, as text, tables and diffs that PrivacyFence
renders itself. An approved read returns exactly what the card showed.

"Always allow" works as for connectors, with limits: a rule for a plugin tool names the plugin, the
tool and the values the call returned for each of the tool's scopes (for example one calendar), and
it matches only when every returned value is in the rule. A tool with no scopes gets a rule for that
one tool. A destructive tool never offers "Always allow". When you enable a plugin again, PrivacyFence
deletes the rules saved for any tool whose gate, read or write, destructive flag or scopes differ from
your previous review, or that is gone, and every rule for a destructive tool. A plugin's
confirmation cards cannot be auto-accepted. See [Approvals and policy](approvals-and-policy.md#plugin-tools).

## Logs

Each plugin's standard error goes to `logs/plugins/<name>.log` in PrivacyFence's data directory (see
[Platform support](platform-support.md#data-locations) for where that is on each OS), readable with
`sudo` or from an elevated prompt. The file is limited to 5 MiB, with three older copies kept
(`<name>.log.1` to `.3`). PrivacyFence's own record of a plugin's start, stop and crashes is in the
daemon log, and its enabling, disabling, tool changes and reads are in the audit log (decisions
`plugin_lifecycle`, `plugin_source` and `plugin_confirm`).

## Disabling, deleting data, removing

- **Disable** (the **Disable** button) stops the plugin and keeps it from starting again, with the
  reason "disabled by you". It needs no passkey. **Review again** and **Enable** turns it on again.
- **Delete this plugin's data** asks the plugin to let go of its files, waits up to 30 seconds, then
  deletes them whether or not it answered, and restarts the plugin if it was running. Deleting needs
  a human session and your passkey where your install asks for one, and cannot be undone. The data
  is the plugin's install-wide folder and its per-user folders (`plugin-data/<name>/` in the data
  directory).
- **Remove** a plugin by deleting its folder from the plugins directory as an administrator, then
  choosing **Rescan**. PrivacyFence stops the plugin and deletes its data, its "Always allow" rules
  and its record, so a different plugin later installed under the same name inherits nothing. If the
  plugins directory is missing or cannot be read (an upgrade in progress, a permissions change),
  nothing is deleted and every known plugin shows "plugins directory unreadable" until a rescan
  succeeds.

## Turning plugins off

`plugins.enabled: false` in `settings.yaml` stops every plugin from running; each shows "plugins are
turned off in settings". It defaults to `true`, applies to local mode only, and takes effect at the
next start. See the [configuration reference](configuration-reference.md).

## Why a plugin does not start

Settings shows a plugin's state and, unless it is running, the reason.

| State | Reason | What it means |
|---|---|---|
| Rejected | `manifest invalid: <detail>` | The manifest is missing, unreadable or breaks a rule; the detail names it. |
| Rejected | `executable is writable by non-administrators` | The executable, the plugin's folder or a folder above it can be changed by someone other than an administrator. The details are in the daemon log. |
| Rejected or disabled | `protocol major mismatch` | The plugin speaks a different protocol major version than PrivacyFence (the manifest's `protocol` is not `"1"`, or the plugin reported another). |
| Disabled | `executable or manifest changed, enable again` | A file differs from what you reviewed, or the plugin reported a tool you did not review. Review it again. |
| Disabled | `crashed 5 times in 10 minutes` | The plugin exited five times within ten minutes. The log shows why. |
| Disabled | `disabled by you` | You disabled it. |
| Disabled | `manifest invalid: name or version differs from the plugin's own` | The plugin's name or version differs from the manifest's. |
| Disabled | `plugins need PrivacyFence's background service` | The install is not separated (`pip` or source). |
| Disabled | `plugins are turned off in settings` | `plugins.enabled` is `false`. |
| Disabled | `could not start` | The process could not be started, or the checks before a start could not run; the daemon log has the error. |
| Disabled | `plugin is no longer installed` | The plugin's folder was gone when it was about to restart after a crash. |
| Missing | `plugins directory unreadable` | The plugins directory could not be listed. Nothing is deleted; **Rescan** once it is back. |

A **Restarting** plugin crashed and is waiting to start again, after 1, 2, 4, 8, 16 and then 30
seconds. Each restart checks the files again as a first start does, so a plugin that changed while
it ran is disabled with the matching reason above instead. Under a plugin, "last tools change rejected: …" means the plugin sent a tool list
PrivacyFence refused (for example a tool you have not reviewed); the previous list stays in force.

## Writing a plugin

A plugin can be written in any language that reads and writes lines on standard input and output,
following [`plugin-protocol.md`](plugin-protocol.md). For Python, `privacyfence-plugin-sdk` does the
protocol for you:

```
pip install privacyfence-plugin-sdk
```

It has no dependencies and needs Python 3.11 or newer. A minimal plugin:

```python
from privacyfence_plugin_sdk import Plugin, Prepared, blocks

plugin = Plugin(name="hello", version="1.0.0")
plugin.scope_type("greeting", "The greeting a call returns")


@plugin.tool("greet", description="Return a greeting for a name.", gate="review", read_only=True,
             scopes=["greeting"], params={"name": {"type": "string", "description": "Who to greet"}},
             required=["name"])
async def greet(ctx, args) -> Prepared:
    return Prepared(
        preview=[blocks.fields({"Greets": args["name"]})],
        payload=[blocks.text(f"Hello, {args['name']}!")],
        scopes={"greeting": ["hello"]},
    )


if __name__ == "__main__":
    plugin.run()
```

Each tool call runs in two steps: `prepare` returns the preview (and, for a read, the payload), you
approve it on the card, and only then does `execute` run. A tool that is not read-only attaches the
function that does the work with `@greet.execute`. Reads from connected services go through
`ctx.source.call(...)` and `ctx.source.download(...)`, a confirmation through `ctx.confirm`, and a
page is `@plugin.page("/")`. Pages must be self-contained: inline the CSS, scripts and images, since
a page's requests for separate files carry no session and are refused.

The manifest names the plugin, its version (equal to the `Plugin`'s), `protocol: "1"`, its
`command`, the source operations it uses, whether it serves pages and its `max_gate_floor`. A tool on
the `auto` gate, read or write, needs `max_gate_floor: auto`, which you see when enabling.

Package the plugin as one executable (`pyinstaller --onefile`) for each OS you support. The plugin
runs with a small, fixed environment (`PATH`, the temporary-directory and locale variables, `TZ`, and
`PRIVACYFENCE_PLUGIN=1`), so a packaged plugin must not depend on `PYTHONPATH` or other variables of
yours. The [`today` example](../examples/plugins/today/README.md) is a complete plugin that uses
every part of the protocol, with a build script and a smoke test.

### Testing a plugin

`privacyfence_plugin_sdk.testing.PluginTestHost` runs your plugin in memory and plays PrivacyFence's
side of the protocol, with the same checks: tool-definition floors, block limits, a simulated gate
(no card, a review card, a popup card, decided by you), scope rules, source fixtures, pages with
PrivacyFence's headers, confirmations, events, purge and shutdown.

```python
from privacyfence_plugin_sdk.testing import PluginTestHost, samples

async def test_the_ai_gets_what_the_card_showed():
    async with PluginTestHost(plugin) as host:
        host.source.load(samples.get("calendar.list_events"))
        outcome = await host.call_tool("list_events", {"reason": "plan the day"})
        assert outcome.card_shown
        assert outcome.released == {"blocks": outcome.card.payload}
```

With pytest, `pytest_plugins = ["privacyfence_plugin_sdk.testing.pytest"]` provides a `plugin_host`
fixture. Where the test host differs from PrivacyFence:

- `PluginTestHost(plugin, max_gate_floor="auto")` is how a test declares the manifest's floor; the
  default is `"review"`, which refuses a tool on the `auto` gate.
- It does not implement `host.introspect()` (what Settings does when you review a plugin) or a
  `tools.changed` sent by the plugin.
- A `source.call` that no fixture answers raises `SourceFixtureMissing`.
- A call a saved rule accepted reports `approval.via` as `rule`; PrivacyFence reports `card`.
- An audit decision PrivacyFence records as `rejected` is `denied` in the test host.
