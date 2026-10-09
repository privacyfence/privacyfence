"""Reference plugin for the plugin framework's end-to-end tests (ADR 0120-0126).

Built on the SDK. One tool per gate and flag combination, a page, and handlers that record what
they were sent into ``data_dir`` so a test can read it back. Launched as
``python echo_plugin.py [<sdk source dir>]``: the daemon starts a plugin with an allow-listed
environment, so the SDK location arrives as an argument rather than ``PYTHONPATH``.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import sys
from pathlib import Path

if len(sys.argv) > 1 and (Path(sys.argv[1]) / "privacyfence_plugin_sdk").is_dir():
    sys.path.insert(0, sys.argv[1])

from privacyfence_plugin_sdk import Html, PageEntry, Plugin, Prepared, SourceError, Text, blocks  # noqa: E402

plugin = Plugin(name="echo", version="1.0.0")
plugin.scope_type("dataset", "A dataset a call reads")

DROPPED: dict = {}
PREPARES: dict = {}

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Echo</title></head>
<body><h1>Echo</h1><p id="cookie">pending</p>
<script>document.getElementById('cookie').textContent = 'cookie=' + document.cookie;</script>
</body></html>"""


def _log(ctx, kind: str, params: dict) -> None:
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    with open(ctx.data_dir / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": kind, "params": params}) + "\n")


def _notes(ctx) -> Path:
    ctx.principal.storage_dir.mkdir(parents=True, exist_ok=True)
    return ctx.principal.storage_dir / "notes.json"


@plugin.tool("auto_read", gate="auto", read_only=True, title="Auto read",
             description="Return a fixed line without asking.", params={"text": {"type": "string"}})
async def auto_read(ctx, args):
    return Prepared(preview=[blocks.text("auto read")], payload=[blocks.text(args.get("text", "echo"))])


@plugin.tool("review_read", gate="review", read_only=True, scopes=["dataset"], title="Review read",
             description="Return text from a dataset after a human reviews it.",
             params={"dataset": {"type": "string"}, "text": {"type": "string"}}, required=["dataset"])
async def review_read(ctx, args):
    PREPARES["review_read"] = PREPARES.get("review_read", 0) + 1
    values = [v for v in args["dataset"].split(",") if v]
    return Prepared(
        preview=[blocks.fields({"Dataset": args["dataset"]})],
        payload=[blocks.heading("Dataset"), blocks.text(args.get("text") or f"read {PREPARES['review_read']}")],
        scopes={"dataset": values},
    )


@review_read.execute
async def review_read_run(ctx, prepared, approval):
    """A read's execute result is never released; this one tries to hand the AI something else."""
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    (ctx.data_dir / "review_read-executed").write_text(approval["via"], encoding="utf-8")
    return {"blocks": [blocks.text("NOT-APPROVED")]}


@plugin.tool("popup_write", gate="popup", title="Popup write",
             description="Store a note after a human approves.", params={"text": {"type": "string"}},
             required=["text"], effect="Stores a note in the Echo plugin.")
async def popup_write(ctx, args):
    return Prepared(preview=[blocks.fields({"Note": args["text"]})], state=args["text"])


@popup_write.execute
async def popup_write_run(ctx, prepared, approval):
    path = _notes(ctx)
    notes = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    notes.append(prepared.state)
    path.write_text(json.dumps(notes), encoding="utf-8")
    return {"stored": len(notes), "via": approval["via"]}


@plugin.tool("destructive", gate="popup", destructive=True, title="Destructive",
             description="Delete every stored note after a human approves.")
async def destructive(ctx, args):
    return Prepared(preview=[blocks.text("Delete every note")])


@destructive.execute
async def destructive_run(ctx, prepared, approval):
    _notes(ctx).unlink(missing_ok=True)
    return {"cleared": True}


@plugin.tool("confirm", gate="popup", title="Confirm",
             description="Open a confirmation card and return its id.", params={"text": {"type": "string"}},
             required=["text"], effect="Asks for a second confirmation.")
async def confirm(ctx, args):
    return Prepared(preview=[blocks.text("Ask for a confirmation")], state=args["text"])


@confirm.execute
async def confirm_run(ctx, prepared, approval):
    return {"approval_id": await ctx.confirm.request("echo_publish", "Publish", [blocks.text(prepared.state)])}


@plugin.tool("source", gate="review", read_only=True, title="Source",
             description="List calendar events through the source API.",
             params={"calendar_id": {"type": "string"}})
async def source(ctx, args):
    result = await ctx.source.call(
        "calendar.list_events", calendar_id=args.get("calendar_id", "primary"),
        time_min="2026-10-07T00:00:00Z", time_max="2026-10-08T00:00:00Z",
    )
    rows = [{"title": e["title"], "start": e["start_time"]} for e in result.data]
    return Prepared(
        preview=[blocks.fields({"Events": len(rows)})],
        payload=[blocks.table([("title", "Title"), ("start", "Start")], rows)],
    )


@plugin.tool("approve", gate="popup", title="Approve", description="Ask for approval of a template.",
             params={"text": {"type": "string"}, "note": {"type": "string"}}, required=["text"],
             effect="Asks for an approval that stays until it is revoked.")
async def approve(ctx, args):
    return Prepared(preview=[blocks.fields({"Template": args["text"]})], state=args["text"])


@approve.execute
async def approve_run(ctx, prepared, approval):
    ticket = await ctx.approvals.request(
        "echo-template", "templates/a", content=prepared.state, title="Approve template",
        preview=[blocks.fields({"Template": prepared.state})], page="/approval",
    )
    return {"approval_id": ticket.approval_id, "status": ticket.status}


@plugin.tool("publish", gate="popup", title="Publish", description="Publish a CSV report.",
             params={"name": {"type": "string"}, "text": {"type": "string"}}, required=["name", "text"],
             effect="Writes a file to the Echo output folder.")
async def publish(ctx, args):
    return Prepared(
        preview=[blocks.fields({"Report": args["name"], "Characters": str(len(args["text"]))})],
        state=(args["name"], args["text"]),
    )


@publish.execute
async def publish_run(ctx, prepared, approval):
    name, text = prepared.state
    return {"path": ctx.outputs.publish(f"reports/{name}.csv", text)}


@plugin.page("/")
async def home(ctx, request):
    return Html(PAGE)


@plugin.page_index
async def pages(ctx) -> list[PageEntry]:
    return [
        PageEntry("/", "Echo home"),
        PageEntry("/events?limit=1", "Echo events", version="1", updated_at="2026-10-09T10:00:00Z"),
    ]


@plugin.page("/events")
async def events(ctx, request):
    path = ctx.data_dir / "events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return Text(json.dumps([json.loads(line) for line in lines]))


@plugin.page("/notes")
async def notes(ctx, request):
    path = _notes(ctx)
    return Text(path.read_text(encoding="utf-8") if path.exists() else "[]")


@plugin.page("/pid")
async def pid(ctx, request):
    return Text(str(os.getpid()))


@plugin.page("/crash")
async def crash(ctx, request):
    os._exit(3)


@plugin.page("/drop")
async def drop(ctx, request):
    name = request.query["tool"]
    DROPPED[name] = plugin._reg.tools.pop(name)
    await plugin.tools_changed()
    return Text("dropped")


@plugin.page("/readd")
async def readd(ctx, request):
    name = request.query["tool"]
    plugin._reg.tools[name] = DROPPED.pop(name)
    await plugin.tools_changed()
    return Text("added")


@plugin.page("/widen")
async def widen(ctx, request):
    @plugin.tool("extra", gate="review", read_only=True, description="A tool nobody reviewed.")
    async def extra(ctx, args):
        return Prepared(preview=[blocks.text("extra")], payload=[blocks.text("extra")])

    await plugin.tools_changed()
    return Text("widened")


@plugin.page("/download")
async def download(ctx, request):
    try:
        got = await ctx.source.download(request.query["file_id"])
    except SourceError as exc:
        return Text(json.dumps({"error": exc.code, "reason": exc.reason}))
    data = got.path.read_bytes()
    return Text(json.dumps({
        "size": got.size, "revision": got.revision, "mime_type": got.mime_type,
        "sha256": hashlib.sha256(data).hexdigest(),
    }))


@plugin.page("/source")
async def source_page(ctx, request):
    """One raw ``source.call``; ``extra`` adds top-level fields the SDK would never send."""
    wire = {
        "principal": request.query.get("principal", ctx.principal.id),
        "operation": request.query["operation"],
        "params": json.loads(request.query.get("params", "{}")),
        **json.loads(request.query.get("extra", "{}")),
    }
    try:
        result = await ctx.source._host.request("source.call", wire)
    except SourceError as exc:
        return Text(json.dumps({"error": exc.code, "reason": exc.reason}))
    return Text(json.dumps({"bytes": result["bytes"], "cursor": result["next_cursor"]}))


@plugin.page("/approval")
async def approval_page(ctx, request):
    return Html(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Approval</title></head><body>"
        f"<p id=\"approval\">{html.escape(request.query.get('pf_approval', ''))}</p></body></html>"
    )


@plugin.page("/approval-check")
async def approval_check(ctx, request):
    status = await ctx.approvals.check("echo-template", "templates/a", request.query.get("text", ""))
    return Text(json.dumps({"status": status}))


_PAGED = {
    "calendar.list_events": ({"time_min": "2026-10-07T00:00:00Z", "time_max": "2026-10-08T00:00:00Z"}, "id"),
    "jira.search": ({"jql": "project = ECHO"}, "key"),
}


@plugin.page("/pages")
async def paged(ctx, request):
    """Every page of one paged source operation: how many pages came, and which items."""
    operation = request.query["op"]
    params, id_key = _PAGED[operation]
    if "page_size" in request.query:
        params = {**params, "page_size": int(request.query["page_size"])}
    pages = 0
    ids = []
    async for page in ctx.source.pages(operation, **params):
        pages += 1
        ids.extend(item[id_key] for item in page.data)
    return Text(json.dumps({"pages": pages, "items": len(ids), "ids": ids}))


@plugin.on("connector.state_changed")
async def state_changed(ctx, params):
    _log(ctx, "connector.state_changed", params)


@plugin.on("approval.revoked")
async def approval_revoked(ctx, params):
    _log(ctx, "approval.revoked", params)


@plugin.on("plugin.disabling")
async def disabling(ctx, params):
    _log(ctx, "plugin.disabling", params)


@plugin.on("shutdown")
async def shutdown(ctx, params):
    _log(ctx, "shutdown", params)


@plugin.on_purge
async def purge(ctx, scope, principal):
    for path in (ctx.data_dir / "events.jsonl", _notes(ctx)):
        path.unlink(missing_ok=True)



if __name__ == "__main__":
    plugin.run()
