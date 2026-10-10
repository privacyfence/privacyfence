# Plugins

A plugin is a separate program that adds tools to PrivacyFence. An administrator installs it, you
review what it can do and enable it, and from then on its tools appear to your AI client next to the
connectors' tools. PrivacyFence gates a plugin's tools exactly as it gates a connector's: a card
shows what a call would release or do, and nothing happens until you approve it.

This page is for administrators who install plugins and for authors who write them. The message
formats are in [`plugin-protocol.md`](plugin-protocol.md); the reasons behind the design are in
[ADR 0120](adr/0120-plugins-are-out-of-process-executables-speaking-json-rpc-over-stdio.md) to
[ADR 0126](adr/0126-the-plugin-sdk-lives-in-this-repository-and-is-published-from-the-same-tag.md),
and [ADR 0127](adr/0127-a-plugin-approval-binds-to-its-content-digest-and-persists-until-revoked.md) to
[ADR 0131](adr/0131-a-plugins-child-processes-run-under-its-account-unsupervised.md).

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
- **Ask you to approve a thing once**, such as a script or a template, and have the approval stay
  until you revoke it ([Approvals](#approvals)).
- **Publish files** you or your AI client can read through PrivacyFence ([Outputs](#outputs)).
- **Serve read-only pages** at `/plugins/<name>/`, in a sandbox, to you only. A plugin can list its
  pages, and PrivacyFence's page browser (the Plugins menu, or `/plugin-pages`) opens each in a new tab.
  A link inside a plugin page opens in the same tab, or in a new tab if the plugin's manifest sets
  `page_new_tabs` (see [Links from a plugin page](#links-from-a-plugin-page)).
- **Take a file** from your AI client in a tool call, without the AI writing the file's bytes
  ([File parameters](#file-parameters)).
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
account but administrators, SYSTEM and TrustedInstaller can write to it and one of them owns it. The executable gets that
strict rule. The plugin's own folder, any folder inside it on the way to the executable, and every
folder above it ignore inherit-only entries, which grant nothing on the folder that carries them
(every folder under `%ProgramFiles%` has one for `CREATOR OWNER`); what such an entry grants a file
or subfolder is checked there. The folders above the plugin's own also ignore the right to create
subfolders, which a default drive root gives every signed-in user, because it does not let anyone
swap an existing folder. Only the executable and the folders on its path are checked. Other files in
the plugin's folder, such as libraries the executable loads, are not; a default install under
`%ProgramFiles%` gives them the folder's administrator-only permissions. Checking every file is
tracked in [issue 860](https://github.com/privacyfence/privacyfence/issues/860).

A plugin's folder must be a real folder inside the plugins directory, not a symbolic link, and its
`command` must name a file inside it.

## Installing a plugin

1. Get the plugin's folder: its executable (for example `today-plugin`, `today-plugin.exe` on
   Windows) and `privacyfence-plugin.yaml`. The folder is named after the plugin's `name`.
2. As an administrator, copy the folder into the plugins directory. Make sure the copy is owned by
   an administrator and not writable by anyone else. On Windows, no account but administrators,
   SYSTEM and TrustedInstaller can write to it and one of them owns it.
3. Open **Settings → Plugins** and choose **Rescan**. The plugin shows as **Not enabled**, or as
   **Rejected** with the reason (see [Why a plugin does not start](#why-a-plugin-does-not-start)).
4. Choose **Review and enable**. PrivacyFence starts the plugin once to read its tool list, with
   reads and confirmations switched off, then shows a card listing every tool with its gate
   (**Runs without asking**, **Review** or **Popup**), whether it reads or writes and whether it is
   destructive, the connector reads it may make, whether it serves pages, whether those pages can
   open links in new tabs, whether a tool takes a file, and whether any tool runs without asking.
   Check each one.
5. Choose **Enable**. Enabling needs a human session and, where your install requires it, your
   passkey. PrivacyFence checks again that the files are the ones you reviewed, records their
   hashes and the tools you saw, and starts the plugin.

A running plugin shows **Running**, and its tools appear to the AI client, which is told that the
tool list changed. A plugin with pages shows a **Pages** link.

A plugin can later drop tools, but it cannot add a tool or change a tool's gate without another
review: a rebuilt plugin whose tool list differs is disabled with "executable or manifest changed,
enable again".

## Using a plugin's tools

A plugin's tool is named `<plugin>_<tool>` (for example `today_list_events`) and takes a required
`reason` like any gated tool. A gated call opens an approval card. The card shows the plugin's name
and tool, and the plugin's own description of the call, as text, tables and diffs that PrivacyFence
renders itself. An approved read returns exactly what the card showed. Asking for the same read
again within five minutes returns the same result without a new card; anything the plugin prepares
afresh, for example after it restarts, gets its own card.

"Always allow" works as for connectors, with limits: a rule for a plugin tool names the plugin, the
tool and the values the call returned for each of the tool's scopes (for example one calendar), and
it matches only when every returned value is in the rule. A tool with no scopes gets a rule for that
one tool. A destructive tool never offers "Always allow". When you enable a plugin again, PrivacyFence
deletes the rules saved for any tool whose gate, read or write, destructive flag or scopes differ from
your previous review, or that is gone, and every rule for a destructive tool. A plugin's
confirmation cards cannot be auto-accepted. See [Approvals and policy](approvals-and-policy.md#plugin-tools).

A tool that takes a file says so in its description: you ask your AI client to use a file, and it
passes a reference, not the file's content. See [Handing a file to a plugin](#handing-a-file-to-a-plugin).

Your AI client learns about a newly enabled plugin's tools from PrivacyFence's tool-list change
notice, but some clients show them only in a new conversation, or after you quit and restart the
client. The enable dialog says so too.

## Large reads and downloads

A plugin's reads from connected services never stop at a size limit. Each operation returns all of
its data, or a page with a cursor to continue, and the plugin follows the cursor until none is left.
Drive files are read in 8 MiB pieces with HTTP Range requests, with no limit on the file's size, so a
large binary file starts arriving at once. Google Docs, Sheets and Slides cannot be ranged: they are
exported whole, as text or CSV, and served from a private temporary file that is deleted after ten
idle minutes. A Salesforce report past 2,000 rows is read with `page_by`, a column that is unique per row,
through `ctx.source.report_pages`, one report run per page. The
cursor rules are in [`plugin-protocol.md`](plugin-protocol.md#paging). A Sheets range is read once and later pages come from a private copy held for ten idle minutes; if
the copy is gone the cursor is refused with `cursor_expired` and the plugin reads the range again. A
Confluence page that changes between pages is refused with `revision_changed` instead of being joined
from two versions. A result that is too large for
the AI goes out as an [output file](#outputs), not through a tool result.

## Approvals

A plugin can ask you to approve a *thing*: a piece of code, a template or a mapping. The card shows
PrivacyFence's own fields (the plugin, by its display name and its installed name, the kind of
thing, the subject and the full `sha256:` digest of its content), then the plugin's preview and,
if the plugin has one, the plugin's own page in a sandboxed frame. Check the fields: PrivacyFence
cannot tell whether the preview or the page shows the content the digest was computed from, and the
digest is what the approval binds to.

- **It stays valid** until you revoke it, across restarts. If the content changes, its digest
  changes and the plugin must ask again. Asking for something already approved opens no card.
- **No rule can accept it.** It asks for your passkey wherever the install requires one for
  sensitive actions (a plugin may turn that off for a request), and it is refused while any AI
  session is [unattended](security-and-compliance.md#human-and-unattested-sessions). A denial or an
  expiry is not stored.
- **Revoke it in Settings.** Under **Settings → Plugins**, a plugin with approvals has an
  **Approvals** list: kind, subject, the first 12 hex digits of the digest, the date and a
  **Revoke** button. Revoking needs no passkey, because it only takes trust away. A running plugin
  is told, and its next check answers revoked. Deleting a plugin's data or removing the plugin deletes
  its approvals too (`approvals deleted: <n>` in the audit log).
- Pending approvals, like confirmations, are limited to 64 in all and 8 per plugin, counted apart
  from confirmations.

## Outputs

A plugin whose manifest sets `outputs: true` gets an **output folder** for each user, which only the
service account can write, and publishes result files into it, for example an export too large for a
tool result. Settings' review card shows "Publishes output files" with the file types.

- **What is published.** A regular file (never a symbolic link) at most eight folders deep, with no
  folder or file name starting with a dot, and with the extension of a type in the manifest's
  `output_types`: `application/json` (`.json`), `text/csv` (`.csv`), `text/html` (`.html`, `.htm`),
  `text/plain` (`.txt`) or `text/markdown` (`.md`). The default is JSON and CSV. A plugin writes
  `.name.tmp` and renames it, so a half-written file is never published.
- **Two tools.** `plugin_outputs_list` runs without asking and lists paths, sizes, times and types,
  200 per page, with a cursor only when more files follow. `plugin_outputs_read` shows an approval
  card with the text and a PII check, and returns about 90 KB per call with `next_offset` to
  continue. A negative offset, or one past the end of the file, is an error. Text that is not valid
  UTF-8 is decoded with replacement characters. The tools exist while any enabled plugin has
  `outputs: true`, running or not.
- **Folder rules.** The card's "Always allow" proposes a rule for the file's folder, which covers
  that folder and everything under it, or for the file itself at the top level. The rule is
  `plugin:<name>:output` ([Approvals and policy](approvals-and-policy.md#plugin-tools)).
- **Paths are checked.** A path with a backslash, a colon, an empty part, a dot-prefixed part or a
  symbolic link on the way is "No such output file", so no spelling reaches a rule it does not belong
  to.
- **Audit.** A read is audited like any gated read, and a `plugin_output` entry for the plugin
  records the path, offset and length. That entry is written after the gate returns, so a denied
  read leaves none.
- **Delete and remove** delete the folder with the rest of the plugin's data.
- **A changed file.** A read of a file that changed after you approved it shows a new card.

## Child processes

A plugin may start child processes.

- They run under the service account, with the plugin's environment, and hold the same trust as the
  plugin. Enabling a plugin approves what it can start.
- PrivacyFence does not supervise them: it does not restart them, count their crashes or limit what
  they do.
- Stopping the plugin kills its process group on Linux and macOS. On Windows only the plugin's own
  process is terminated, so a child can outlive it.
- The plugin is responsible for confining its children, for example with no network, read-only
  inputs, one scratch folder and a time limit, and for stopping them.

See [ADR 0131](adr/0131-a-plugins-child-processes-run-under-its-account-unsupervised.md).

## Logs

Each plugin's standard error goes to `logs/plugins/<name>.log` in PrivacyFence's data directory (see
[Platform support](platform-support.md#data-locations) for where that is on each OS), readable with
`sudo` or from an elevated prompt. The file is limited to 5 MiB while the plugin runs, with three older copies kept
(`<name>.log.1` to `.3`). PrivacyFence's own record of a plugin's start, stop and crashes is in the
daemon log, and its enabling, disabling, tool changes and reads are in the audit log (decisions
`plugin_lifecycle`, `plugin_source`, `plugin_confirm`, `plugin_approval` and `plugin_output`).

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
  and its record, so a different plugin later installed under the same name inherits nothing. On
  Windows, **Disable** the plugin first: Windows does not let a running plugin's folder be deleted.
  If the plugins directory is missing or cannot be read (an upgrade in progress, a permissions
  change), nothing is deleted and every known plugin shows "plugins directory unreadable" until a
  rescan succeeds.

Any of the plugin's approval or confirmation cards still waiting for you expire when you disable it,
delete its data or remove it, and when PrivacyFence disables it after repeated crashes. Actions on
plugins run one at a time: an action you start while another runs waits for it.

## Turning plugins off

`plugins.enabled: false` in `settings.yaml` stops every plugin from running; each shows "plugins are
turned off in settings". It defaults to `true`, applies to local mode only, and takes effect at the
next start. See the [configuration reference](configuration-reference.md).

## Why a plugin does not start

Settings shows a plugin's state and, unless it is running, the reason.

| State | Reason | What it means |
|---|---|---|
| Rejected | `manifest invalid: <detail>` | The manifest is missing, unreadable or breaks a rule; the detail names it. |
| Rejected | `executable is writable by non-administrators` | The executable, the plugin's folder or a folder above it can be changed by someone other than an administrator. The details are in the daemon log. On Windows, it is also refused when a file or folder is owned by an account other than SYSTEM, Administrators or TrustedInstaller. |
| Rejected | `plugin directory is a symbolic link` | The plugin's folder in the plugins directory is a symbolic link. Install the plugin as a real folder. |
| Rejected | `executable is outside the plugins directory` | The executable resolves to a path outside the plugins directory, for example through a junction. Install the plugin as a real folder. |
| Rejected or disabled | `protocol major mismatch` | The plugin speaks a different protocol major version than PrivacyFence (the manifest's `protocol` is not `"1"`, or the plugin reported another). |
| Disabled | `executable or manifest changed, enable again` | A file differs from what you reviewed, or the plugin reported a tool you did not review. Review it again. |
| Disabled | `crashed 5 times in 10 minutes` | The plugin exited five times within ten minutes. The log shows why. |
| Restarting | `crashed 1 time`, `crashed <n> times` | The plugin exited and starts again after a pause (see below). The log shows why. |
| Disabled | `disabled by you` | You disabled it. |
| Disabled | `manifest invalid: name or version differs from the plugin's own` | The plugin's name or version differs from the manifest's. |
| Disabled | `plugins need PrivacyFence's background service` | The install is not separated (`pip` or source). |
| Disabled | `plugins are turned off in settings` | `plugins.enabled` is `false`. |
| Disabled | `could not start` | The process could not be started, or the checks before a start could not run; the daemon log has the error. |
| Disabled | `plugin is no longer installed` | The plugin's folder was gone when it was about to restart after a crash. |
| Missing | `plugins directory unreadable` | The plugins directory could not be listed. Nothing is deleted; **Rescan** once it is back. |

A plugin that stops reading what PrivacyFence sends it for 10 seconds is restarted like a crash.

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
function that does the work with `@greet.execute`. What that function returns
reaches the AI only when it is at most 2,048 bytes and the PII check finds nothing; otherwise
the AI is told the action ran and its result was withheld. Reads from connected services go through
`ctx.source.call(...)`, `ctx.source.pages(...)` (every page of an operation, following the cursor),
`ctx.source.collect(...)` (the whole list for `jira.search` and `calendar.list_events`),
`ctx.source.report_pages(...)` (every page of a Salesforce report, read by a unique column) and
`ctx.source.download(...)`, a confirmation through `ctx.confirm`, an approval through
`ctx.approvals` (`request`, `check`, `await_`, `wait`), `ctx.confirm.wait()` (waits until the card is approved, denied or expired), a published file through `ctx.outputs.publish`, and a
page is `@plugin.page("/")`. A page shown inside an approval card receives `pf_approval` in its
query. Pages must be self-contained: inline the CSS, scripts and images, since
a page's requests for separate files carry no session and are refused. Links between a plugin's
pages do not carry your session either, so a multi-page plugin lists its pages with `page_index`
instead of linking them. A single page can take a `?query`; page paths match exactly.

#### Links from a plugin page

A link without a target opens in the page's own tab and works as in any browser. A link to
another site loads there. A link into PrivacyFence, or to another page of the plugin, carries no
session and gets the owner-only 404.

A `target="_blank"` link or `window.open` does nothing, unless the manifest sets `page_new_tabs: true`
(it needs `pages: true`). Then the plugin's pages open links in a normal new tab. That tab is outside
the sandbox, has no `window.opener`, and carries no PrivacyFence session. The enable dialog says "Its
pages can open links in new tabs." for such a plugin, and a plugin that adds the key later must be
enabled again. The page an approval card shows never opens new tabs. Plugin pages cannot download
files (`<a download>`) or use `eval`; the exact content security policy and what it allows are in
[`plugin-protocol.md`](plugin-protocol.md#pages). To try a page, or the built-in check page, in your own browser
without a plugin, run `python3 scripts/plugin_page_preview.py --check-page --new-tabs` from a checkout.

### File parameters

A tool can take a file instead of a string the AI has to write out. Declare the parameter with
`file_param`:

```python
from privacyfence_plugin_sdk import file_param

@plugin.tool("publish", description="Publish a page.", gate="review",
             params={"html": file_param("The page.", max_bytes=1_048_576, media_types=["text/html"])},
             required=["html"])
async def publish(ctx, args) -> Prepared:
    page = ctx.files["html"]        # name, size, media_type, sniffed_type, sha256
    return Prepared(preview=[blocks.fields({"Page": page.name})])


@publish.execute
async def do_publish(ctx, prepared, approval):
    data = ctx.files["html"].content    # the bytes, in the execute function only
    ...
```

The rules:

- One file parameter per tool, at most 8 MiB, each accepted type one of `text/html`, `text/plain`,
  `application/json`, `application/pdf`, `image/png`, `image/jpeg`, `image/gif`, `image/webp`,
  `font/woff`, `font/woff2`, `font/ttf`, `font/otf` and `application/octet-stream`.
- The tool is not read-only and not on the `auto` gate, so every file goes through a card or a rule
  you saved.
- The tool function gets the file's name, size, declared and detected type and SHA-256. The bytes
  arrive in the execute function only, after the call was approved. The plugin never sees a path or
  an upload token, and PrivacyFence keeps no file between calls.
- `ctx.files` is empty for a call without a file. `file_param` is the SDK's; any other language
  follows [Tool definitions](plugin-protocol.md#file-parameters) and
  [`tool.prepare`](plugin-protocol.md#toolprepare).
- A PrivacyFence older than plugin protocol 1.3 cannot send files; the SDK leaves such a tool out
  of the tool list it offers. See [Protocol and SDK versions](#protocol-and-sdk-versions).

The file's content is checked against the accepted types by its bytes, not its name. The approval
card starts with a block showing the file's name, where it came from, its size, declared and detected
type and SHA-256, under a heading saying PrivacyFence read and checked them; your preview follows under
a "From the plugin" heading, and a `fields` row labelled File, Source, Size, Declared type, Detected
type or SHA-256 (case and surrounding spaces ignored) makes the preview invalid.

#### Handing a file to a plugin

Your AI client passes the file as the parameter's value: a local path (absolute, or starting with
`~/`), which PrivacyFence reads the way it reads a file for any other tool ([How it
works](how-it-works.md#files)). A client without the `.mcpb` extension calls `privacyfence_create_upload_slot`, sends the file to the `upload_url` it
returns with an HTTP `PUT`, and passes `upload:<upload_id>` as the parameter. An upload slot is used
once, by the session that created it, and expires. PrivacyFence names the choice in the parameter's
description, so the client does not need to be told.

### Protocol and SDK versions

| Feature | PrivacyFence | `privacyfence-plugin-sdk` |
|---|---|---|
| Same-tab links | any | any |
| `page_new_tabs` | protocol 1.3.0 | any for the plugin; 1.3.0 for `PluginTestHost(page_new_tabs=True)` |
| File parameters | protocol 1.3.0 | 1.3.0 (`file_param`, `ctx.files`, `call_tool(files=…)`) |

The SDK is built from this repository and is not published to PyPI before a release carries
1.3.0, so a plugin that needs it pins a commit: the first commit on `main` where
`plugin-sdk/src/privacyfence_plugin_sdk/plugin.py` has `PROTOCOL_VERSION = "1.3.0"`.

### Page index

`@plugin.page_index` registers `async def pages(ctx) -> list[PageEntry]`, which returns the pages
PrivacyFence's page browser lists, each a `PageEntry(path, title)` with an optional `version`,
`created_at`, `updated_at` and `description`:

```python
from privacyfence_plugin_sdk import PageEntry

@plugin.page_index
async def pages(ctx):
    return [PageEntry("/", "Home")]
```

A plugin without a page index is listed with one page, `/`.

The manifest names the plugin, its version (equal to the `Plugin`'s), `protocol: "1"`, its
`command`, the source operations it uses, whether it serves pages and whether those can open links in new tabs
(`pages`, `page_new_tabs`), whether it publishes outputs
(`outputs`, `output_types`) and its `max_gate_floor`. A tool on
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
PrivacyFence's headers, confirmations, approvals (`host.approvals`, `decide_approval`, `revoke_approval`), paged source
fixtures (`returns_pages`), an output folder (`outputs=True`, `host.list_outputs`), events, purge and
shutdown.

```python
from privacyfence_plugin_sdk.testing import PluginTestHost, samples

async def test_the_ai_gets_what_the_card_showed():
    async with PluginTestHost(plugin, source_operations=("calendar.list_events",)) as host:
        host.source.load(samples.get("calendar.list_events"))
        outcome = await host.call_tool("list_events", {"reason": "plan the day"})
        assert outcome.card_shown
        assert outcome.released == {"blocks": outcome.card.payload}
```

With pytest, `pytest_plugins = ["privacyfence_plugin_sdk.testing.pytest"]` provides a `plugin_host`
fixture.

`await PluginTestHost(plugin).introspect()` starts the plugin the way Settings does when you review
it, and returns its tool list.

Where the test host differs from PrivacyFence:

- `PluginTestHost(plugin, max_gate_floor="auto")` is how a test declares the manifest's floor; the
  default is `"review"`, which refuses a tool on the `auto` gate.
- `PluginTestHost(plugin, source_operations=(...), pages=True)` takes the manifest's `source_operations` and
  `pages`; it checks source-call parameters as PrivacyFence does.
- `PluginTestHost(plugin, pages=True, page_new_tabs=True)` serves pages with the policy of a plugin that
  sets `page_new_tabs`. `call_tool(name, files={...})` hands a file to a tool, applies PrivacyFence's
  size and detected-type checks, and shows the card's source as `Test host`.
- `list_pages()` titles the fallback entry with the plugin's name; PrivacyFence uses its display name.
- `pii=` takes a function that models the PII check, which overrides an "Always allow" rule.
- It withholds a write result over 2,048 bytes but does not run PrivacyFence's PII detector on it.
- It ignores a `tools.changed` the plugin sends.
- It refuses a reserved plugin name, as PrivacyFence does, which also keeps every tool's MCP name
  clear of PrivacyFence's built-in tools.
- A `source.call` that no fixture answers raises `SourceFixtureMissing`.
- A call a saved rule accepted reports `approval.via` as `rule`; PrivacyFence reports `card`.
- An audit decision PrivacyFence records as `rejected` is `denied` in the test host.
- The body of a 405 is "Method not allowed." in the test host and "Method Not Allowed" in
  PrivacyFence.
- Revoking an approval through the test host writes no audit row; PrivacyFence writes
  `<kind>; revoked`.
- PrivacyFence adds `Permissions-Policy` and `Cross-Origin-Opener-Policy` headers to a page
  response; the test host does not.
