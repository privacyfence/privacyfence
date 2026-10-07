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
itself. Prepared state is kept for 15 minutes.

Block builders (`blocks.heading`, `fields`, `table`, `text`, `code`, `diff`) validate with the same
rules PrivacyFence applies, and raise `ValueError` on a mistake. A tool definition PrivacyFence
would refuse raises `ToolDefinitionError` when the tool is registered.

## Reading from connected services

```python
result = await ctx.source.call("calendar.list_events", time_min="...", time_max="...")
file = await ctx.source.download(file_id)   # chunked, restarts once if the file changes
```

A source operation must be listed in the manifest's `source_operations`.

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
opaque origin, so requests for separate files carry no session and are refused. Links between pages
work.

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

An in-memory test host for plugin tests ships in a later release.
