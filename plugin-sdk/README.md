# privacyfence-plugin-sdk

A zero-dependency Python SDK for writing [PrivacyFence](https://github.com/privacyfence/privacyfence)
plugins. A plugin is its own executable. PrivacyFence starts it as a child process and talks
JSON-RPC 2.0 to it over stdin and stdout. A plugin can contribute MCP tools that PrivacyFence gates
like its own connector tools, read from connected services without ever holding a token, ask a human
to confirm something, and serve read-only pages.

The protocol is described by
[protocol.schema.json](https://github.com/privacyfence/privacyfence/blob/main/docs/plugin-protocol/protocol.schema.json).
The design and its discussion live in
[the tracking issue](https://github.com/privacyfence/privacyfence/issues/846).

## Install

```
pip install privacyfence-plugin-sdk
```

Python 3.11 or newer. The package has no dependencies.

## A minimal plugin

```python
from privacyfence_plugin_sdk import Plugin, Prepared, blocks

plugin = Plugin(name="hello", version="1.0.0")
plugin.scope_type("greeting", "The greeting a call returns")


@plugin.tool(
    "greet",
    description="Return a greeting for a name.",
    gate="review",
    read_only=True,
    scopes=["greeting"],
    params={"name": {"type": "string", "description": "Who to greet"}},
    required=["name"],
)
async def greet(ctx, args) -> Prepared:
    message = f"Hello, {args['name']}!"
    return Prepared(
        preview=[blocks.fields({"Greets": args["name"]})],
        payload=[blocks.text(message)],
        scopes={"greeting": ["hello"]},
    )


if __name__ == "__main__":
    plugin.run()
```

A tool is called in two steps. `tool.prepare` runs your function, which returns a `Prepared`: what
the call would release or do. PrivacyFence applies the gate and shows the preview on a card. Only
after approval does it send `tool.execute`. A read-only tool releases the prepared payload itself,
so the AI receives exactly what the human saw. A tool that changes things attaches the function that
does the work with `@greet.execute`, which receives the `Prepared` back, including its `state`.

The SDK checks the arguments' digest on execute and answers `digest_mismatch` or `unknown_call`
itself. Prepared state is kept for 20 minutes, at most 256 at a time; the oldest goes first.

Block builders (`blocks.heading`, `fields`, `table`, `text`, `code`, `diff`) validate with the same
rules PrivacyFence applies, and raise `ValueError` on a mistake. A tool definition PrivacyFence
would refuse raises `ToolDefinitionError` when the tool is registered.

## Reading from connected services

```python
result = await ctx.source.call("calendar.list_events", time_min="...", time_max="...")
file = await ctx.source.download(file_id)   # chunked, restarts once if the file changes
```

A source operation must be listed in the manifest's `source_operations`.

### Paging

```python
async for page in ctx.source.pages("jira.search", jql="project = PF", page_size=50):
    handle(page.data)                      # one SourceResult per page

events = await ctx.source.collect("calendar.list_events", time_min="...", time_max="...")
```

`pages` calls the operation, then calls it again with the `next_cursor` of the previous page, until
there is none. Size pages with `page_size`; do not send `max_results` or `cursor` yourself. `collect`
concatenates the pages into one list and works only for `jira.search` and `calendar.list_events`; any
other operation raises `ValueError`. `download` handles its own cursor.

To read every row of a Salesforce report, use `report_pages`:

```python
async for page in ctx.source.report_pages(
    "00O...", page_by="Account.PF_QA_Number__c", columns=[...], filters=[...]
):
    rows = page.data["factMap"]["T!T"]["rows"]
```

Each page is one report run, and Salesforce counts it against the org's report-run limits.
`page_by` must be unique per row (an auto-number column is best): pages are read in order of that
column, and a value that repeats across pages raises `SourceError` with `reason == "not_unique"`.
`columns` and `filters` are optional and narrow the run as for `salesforce.report_run`.

## The manifest

Next to the executable, a plugin ships `privacyfence-plugin.yaml`:

```yaml
name: hello
display_name: Hello
version: 1.0.0
protocol: "1"
command: ["hello-plugin"]
source_operations: []
tools: dynamic
max_gate_floor: review
pages: false
```

`name` must equal the directory name and the `Plugin` name, and `version` must equal the `Plugin`
version.

## Pages

```python
from privacyfence_plugin_sdk import Html

@plugin.page("/")
async def home(ctx, request) -> Html:
    return Html("<h1>Hello</h1>")
```

Pages are served read-only at `/plugins/<name>/` under a sandbox content security policy. **A page
must be self-contained**: inline its CSS, scripts and images (as `data:` URIs). The page runs in an
opaque origin, so requests for separate files carry no session and are refused. A page is a single
self-contained page that keeps its state in the page itself (script or `#fragment`). Other pages of
the plugin open from the page browser, Settings or a typed URL: links between plugin pages, and links from a
plugin page back into PrivacyFence, do not carry the session and get a 404.

### Page index

To have PrivacyFence's page browser list your pages, register a page index:

```python
from privacyfence_plugin_sdk import PageEntry

@plugin.page_index
async def pages(ctx): return [PageEntry("/", "Home")]
```

`PluginTestHost.list_pages()` returns the entries as the daemon receives them. `types.py` has a
`PageEntry` TypedDict for the wire shape; `privacyfence_plugin_sdk.PageEntry` is the dataclass to
return.

## Confirmations

A confirmation is a card no saved rule can accept. `ctx.confirm.wait(...)` waits until it is approved,
denied or expired:

```python
approval_id = await ctx.confirm.request("publish", "Publish the report", [blocks.text("…")])
outcome = await ctx.confirm.wait(approval_id)   # outcome.status: approved, denied or expired
```

## Approvals

An approval is a human's yes to one specific thing (a template, a mapping, a piece of code) that stays
valid until the thing changes or the human revokes it in Settings.

```python
ticket = await ctx.approvals.request(
    "template", "templates/invoice", content=template_text, title="Approve the invoice template",
    preview=[blocks.code(template_text)], page="/approval",
)
if ticket.status != "approved":
    outcome = await ctx.approvals.wait(ticket.approval_id)   # waits for the human, however long the card is pending

if await ctx.approvals.check("template", "templates/invoice", template_text) == "approved":
    ...   # "approved", "revoked" or "unknown"
```

The SDK computes the digest (`sha256:` over the UTF-8 text or the raw bytes; `ctx.approvals.digest(content)`
gives it to you), so changing the content makes the old approval stop matching. A `page` is shown in a
sandboxed frame on the approval card and receives `pf_approval=<approval id>` in its query. Register the
`approval.revoked` event with `@plugin.on("approval.revoked")` to react when a human revokes one.

## Outputs

A plugin whose manifest sets `outputs: true` (and optionally `output_types`) can publish files that the
user's agent lists and reads through PrivacyFence.

```python
path = ctx.outputs.publish("reports/2026-q3.csv", csv_text)   # str or bytes; returns the relative path
folder = ctx.outputs.dir
```

`publish` writes a hidden `.name.tmp` file, flushes it and renames it, so a reader never sees a partial
file. It refuses an existing file (publish a new version under a new name; `FileExistsError`), a path with
`..`, a leading `/` or a dot-prefixed segment, and an extension the manifest's `output_types` do not allow
(`ValueError`). Without `outputs: true` both calls raise `RuntimeError`.

## Child processes

A plugin may start child processes. They run under the plugin's account with the plugin's environment, and
they hold the same trust as the plugin. PrivacyFence does not supervise, restart or count them; stopping the
plugin kills its process group on POSIX and its process on Windows. Confining them is the plugin's job, for
example with no network, read-only inputs and one scratch folder.

## Building with PyInstaller

PrivacyFence runs one executable, so freeze the plugin into a single file:

```
pyinstaller --onefile --name hello-plugin hello.py
```

Build on each operating system you support. Put the executable and the manifest in a directory
named after the plugin, inside PrivacyFence's administrator-only plugins directory.

## Running and testing

`plugin.run()` serves the protocol on stdin and stdout until PrivacyFence sends `shutdown` or closes
the pipe. Anything the plugin prints to stdout goes to stderr, so it cannot corrupt the protocol
stream; log to stderr.

### Testing a plugin

`privacyfence_plugin_sdk.testing` provides `PluginTestHost`, an in-memory PrivacyFence. It runs the
plugin's real runner and plays the daemon's side of the protocol: it checks the tool definitions,
blocks and limits against the rules the SDK knows, decides each call at the simulated gate, answers
`source.call` from fixtures, and releases only what a person would have seen on the card.

```python
from privacyfence_plugin_sdk.testing import PluginTestHost, samples

async def test_the_ai_gets_what_the_card_showed():
    async with PluginTestHost(plugin, source_operations=("calendar.list_events",)) as host:
        host.source.load(samples.get("calendar.list_events"))
        outcome = await host.call_tool("list_events", {"reason": "plan the day"})
        assert outcome.card_shown
        assert outcome.released == {"blocks": outcome.card.payload}
```

With pytest, add `pytest_plugins = ["privacyfence_plugin_sdk.testing.pytest"]` to `conftest.py` and
use the `plugin_host` fixture: `async with plugin_host(plugin) as host`. The host also drives pages,
confirmations, events, purge and shutdown. The module docstring of `privacyfence_plugin_sdk.testing`
lists them all.

`await PluginTestHost(plugin).introspect()` starts the plugin the way Settings does when you review
it, and returns its tool list.

Where the test host differs from PrivacyFence:

- `PluginTestHost(plugin, max_gate_floor="auto")` is how a test declares the manifest's floor; the
  default is `"review"`, which refuses a tool on the `auto` gate.
- `PluginTestHost(plugin, source_operations=(...), pages=True)` takes the manifest's `source_operations` and
  `pages`; it checks source-call parameters as PrivacyFence does.
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
- PrivacyFence adds `Permissions-Policy` and `Cross-Origin-Opener-Policy` headers to a page
  response; the test host does not.
