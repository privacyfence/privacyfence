"""``today``: an example PrivacyFence plugin that shows today's calendar events on a page.

It is built only on ``privacyfence-plugin-sdk`` and uses every part of the framework once: a source
read, plugin storage, one tool per gate, a confirmation, a page and the four events. Read it top to
bottom as a worked example; the README next to it walks through trying it on a packaged install.

Run it as ``today-plugin`` (the PyInstaller build) or ``python today_plugin.py``. ``--self-test``
checks that the plugin builds and exits, without touching stdio.
"""

from __future__ import annotations

import asyncio
import csv
import difflib
import html
import io
import json
import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

from privacyfence_plugin_sdk import PROTOCOL_VERSION, Html, PageEntry, Plugin, Prepared, SourceError, blocks

NAME = "today"
VERSION = "1.0.0"
MANIFEST_FILENAME = "privacyfence-plugin.yaml"
FLAGS_FILENAME = "build-flags.json"

_DAY = "day.json"
_NOTES = "notes.json"
_COUNTER = "counter.json"
_REVOKED = "layout-revoked.json"
_LAYOUT_KIND = "page-layout"
_LAYOUT_SUBJECT = "today/layout"
_LAYOUT = """Today page layout
- Header: the word Today
- Section 1: the published day, one row per event with its attendees
- Section 2: notes, one line each
- Footer: the number of fetches so far
"""
_OPERATION = "calendar.list_events"


def _home() -> Path:
    """The folder holding the executable (a frozen build) or this file (a source checkout)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _flags() -> dict:
    try:
        data = json.loads((_home() / FLAGS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _today_bounds() -> tuple[str, str, str]:
    """Local midnight to the next local midnight as RFC 3339 strings, and the local date."""
    now = datetime.now().astimezone()
    start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    return start.isoformat(), (start + timedelta(days=1)).isoformat(), now.date().isoformat()


def _clock(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%H:%M")
    except ValueError:
        return value


def _event_line(event: dict) -> str:
    when = (
        "all day"
        if event.get("all_day")
        else f"{_clock(event.get('start_time', ''))}-{_clock(event.get('end_time', ''))}"
    )
    return f"{when} {event.get('title', '')}"


def _published_lines(snapshot: dict | None) -> list[str]:
    if not snapshot:
        return []
    lines = [_event_line(e) for e in snapshot.get("events", [])]
    lines += [f"note on {n['event_id']}: {n['text']}" for n in snapshot.get("notes", [])]
    return lines


def _layout_note(status: str, revoked_at: str | None) -> str:
    if status == "approved":
        return '<p id="layout" class="layout-approved">The layout is <b>approved</b>.</p>'
    detail = f" (revoked {html.escape(revoked_at)})" if status == "revoked" and revoked_at else ""
    return f'<p id="layout" class="layout-not-approved">The layout is <b>not approved</b>{detail}.</p>'


def _approval_page_html(approval_id: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Approve the layout</title><style>'
        "body{font-family:sans-serif;margin:1rem;color:rgb(30,30,30);background:white}"
        "pre{background:whitesmoke;padding:1rem;overflow:auto}"
        "</style></head><body><h1>Page layout</h1>"
        f'<p>Approval <code id="approval-id">{html.escape(approval_id)}</code></p>'
        f'<pre id="template">{html.escape(_LAYOUT)}</pre></body></html>'
    )


def _csv_text(events: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["start", "end", "title", "attendees"])
    for e in events:
        writer.writerow([e.get("start_time", ""), e.get("end_time", ""), e.get("title", ""), _attendee_names(e)])
    return out.getvalue()


def _attendee_names(event: dict) -> str:
    return ", ".join(a.get("display_name") or a.get("email", "") for a in event.get("attendees", []))


def _page_html(day: dict, notes: list[dict], fetches: int, manifest_text: str, layout_note: str) -> str:
    esc = html.escape
    published = day.get("published")
    parts = [
        '<!doctype html><html><head><meta charset="utf-8"><title>Today</title><style>',
        "body{font-family:sans-serif;margin:2rem;color:rgb(30,30,30);background:white}",
        "table{border-collapse:collapse}td,th{border:1px solid silver;padding:.3rem .6rem;text-align:left}",
        ".stale{background:gold;border:1px solid darkorange;padding:.5rem;margin-bottom:1rem}",
        "pre{background:whitesmoke;padding:1rem;overflow:auto}",
        ".layout-approved{color:darkgreen}.layout-not-approved{color:firebrick}",
        "</style></head><body><h1>Today</h1>",
        layout_note,
    ]
    if day.get("stale"):
        parts.append(
            '<div class="stale" id="stale">Calendar changed since this day was fetched. '
            "Run today_refresh to update it.</div>"
        )
    if published:
        parts.append(f"<h2>Published day</h2><p>Published at {esc(published['at'])}.</p>")
        rows = "".join(
            f"<tr><td>{esc(_event_line(e))}</td><td>{esc(_attendee_names(e))}</td></tr>" for e in published["events"]
        )
        parts.append(f'<table id="events"><tr><th>Event</th><th>Attendees</th></tr>{rows}</table>')
    else:
        parts.append('<p id="unpublished">Nothing is published yet. Run today_publish to publish the fetched day.</p>')
    parts.append("<h2>Notes</h2>")
    if notes:
        parts.append(
            '<ul id="notes">' + "".join(f"<li>{esc(n['event_id'])}: {esc(n['text'])}</li>" for n in notes) + "</ul>"
        )
    else:
        parts.append('<p id="no-notes">No notes.</p>')
    parts.append(f"<p>Fetches so far: {fetches}.</p>")
    parts.append('<h2>Manifest</h2><pre><code id="manifest">' + esc(manifest_text) + "</code></pre>")
    parts.append(
        '<h2>Sandbox check</h2><p id="cookie-check">checking</p><script>'
        "var el = document.getElementById('cookie-check');"
        "try { var value = document.cookie; "
        "el.textContent = 'document.cookie is readable' + (value ? ' and not empty' : ' (and empty)'); }"
        "catch (error) { el.textContent = 'document.cookie is unreadable: the sandbox blocks it'; }"
        "</script></body></html>"
    )
    return "".join(parts)


def build_plugin(crash_tool: bool = False) -> Plugin:
    """Build the plugin. ``crash_tool`` adds the hidden ``crash`` tool a tester uses to watch restarts."""
    plugin = Plugin(name=NAME, version=VERSION)
    plugin.scope_type("calendar", "Calendar id a call reads")
    pending = {"fetches": 0}  # fetches counted since the last flush to counter.json
    used_dirs: set[Path] = set()  # principal directories this process wrote to, for a purge of everything

    def _principal_dir(ctx) -> Path:
        ctx.principal.storage_dir.mkdir(parents=True, exist_ok=True)
        used_dirs.add(ctx.principal.storage_dir)
        return ctx.principal.storage_dir

    def _load_day(ctx) -> dict | None:
        day = _read_json(_principal_dir(ctx) / _DAY, None)
        return day if isinstance(day, dict) else None

    def _load_notes(ctx) -> list[dict]:
        saved = _read_json(_principal_dir(ctx) / _NOTES, {})
        if not isinstance(saved, dict) or saved.get("date") != _today_bounds()[2]:
            return []
        return saved.get("notes", [])

    def _flush_counter(ctx) -> None:
        if not pending["fetches"]:
            return
        ctx.data_dir.mkdir(parents=True, exist_ok=True)
        saved = _read_json(ctx.data_dir / _COUNTER, {})
        total = (saved.get("fetches", 0) if isinstance(saved, dict) else 0) + pending["fetches"]
        _write_json(ctx.data_dir / _COUNTER, {"fetches": total})
        pending["fetches"] = 0

    def _total_fetches(ctx) -> int:
        saved = _read_json(ctx.data_dir / _COUNTER, {})
        return (saved.get("fetches", 0) if isinstance(saved, dict) else 0) + pending["fetches"]

    async def _fetch(ctx, calendar_id: str) -> tuple[list[dict], str]:
        start, end, date = _today_bounds()
        result = await ctx.source.call(_OPERATION, calendar_id=calendar_id, time_min=start, time_max=end)
        return (result.data if isinstance(result.data, list) else []), date

    # ------------------------------------------------------------------ tools

    @plugin.tool(
        "status",
        gate="auto",
        read_only=True,
        title="Today status",
        description="Say whether today's events were fetched, when, how many, and whether they are stale. "
        "Returns no event content.",
    )
    async def status(ctx, args):
        day = _load_day(ctx)
        info = (
            {"Fetched": "no"}
            if day is None
            else {
                "Fetched": "yes",
                "Fetched at": day["fetched_at"],
                "Events": len(day["events"]),
                "Stale": "yes" if day.get("stale") else "no",
            }
        )
        return Prepared(preview=[blocks.text("Report the state of today's cache")], payload=[blocks.fields(info)])

    @plugin.tool(
        "refresh",
        gate="auto",
        title="Refresh today",
        description="Fetch today's events again and store them in the plugin.",
        params={"calendar_id": {"type": "string", "description": "Calendar to read; default primary"}},
        effect="Replaces the plugin's stored copy of today's events.",
    )
    async def refresh(ctx, args):
        return Prepared(
            preview=[blocks.fields({"Calendar": args.get("calendar_id") or "primary"})],
            state=args.get("calendar_id") or "primary",
        )

    @refresh.execute
    async def refresh_run(ctx, prepared, approval):
        events, date = await _fetch(ctx, prepared.state)
        previous = _load_day(ctx) or {}
        day = {
            "date": date,
            "calendar_id": prepared.state,
            "fetched_at": datetime.now().astimezone().isoformat(),
            "events": events,
            "stale": False,
            "published": previous.get("published") if previous.get("date") == date else None,
        }
        _write_json(_principal_dir(ctx) / _DAY, day)
        pending["fetches"] += 1
        return {"events": len(events), "fetched_at": day["fetched_at"]}

    @plugin.tool(
        "list_events",
        gate="review",
        read_only=True,
        scopes=["calendar"],
        title="List today's events",
        description="List today's titles, times and attendees on one calendar.",
        params={"calendar_id": {"type": "string", "description": "Calendar to read; default primary"}},
    )
    async def list_events(ctx, args):
        calendar_id = args.get("calendar_id") or "primary"
        events, _ = await _fetch(ctx, calendar_id)
        rows = [
            {
                "time": "all day"
                if e.get("all_day")
                else f"{_clock(e.get('start_time', ''))}-{_clock(e.get('end_time', ''))}",
                "title": e.get("title", ""),
                "attendees": _attendee_names(e),
            }
            for e in events
        ]
        table = blocks.table([("time", "Time"), ("title", "Title"), ("attendees", "Attendees")], rows)
        return Prepared(
            preview=[blocks.fields({"Calendar": calendar_id, "Events": len(rows)}), table],
            payload=[table],
            scopes={"calendar": [calendar_id]},
        )

    @plugin.tool(
        "add_note",
        gate="popup",
        title="Add a note",
        description="Attach a note to one of today's events. The note is stored by the plugin only; "
        "the calendar is never written.",
        params={
            "event_id": {"type": "string", "description": "Id of the event"},
            "text": {"type": "string", "description": "The note"},
        },
        required=["event_id", "text"],
        effect="Stores a note in the Today plugin.",
    )
    async def add_note(ctx, args):
        return Prepared(
            preview=[blocks.fields({"Event": args["event_id"]}), blocks.text(args["text"])],
            state={"event_id": args["event_id"], "text": args["text"]},
        )

    @add_note.execute
    async def add_note_run(ctx, prepared, approval):
        notes = _load_notes(ctx) + [prepared.state]
        _write_json(_principal_dir(ctx) / _NOTES, {"date": _today_bounds()[2], "notes": notes})
        return {"notes": len(notes)}

    @plugin.tool(
        "clear_notes",
        gate="popup",
        destructive=True,
        title="Clear today's notes",
        description="Delete all of today's notes.",
        effect="Deletes every note the plugin holds for today.",
    )
    async def clear_notes(ctx, args):
        return Prepared(preview=[blocks.fields({"Notes to delete": len(_load_notes(ctx))})])

    @clear_notes.execute
    async def clear_notes_run(ctx, prepared, approval):
        (_principal_dir(ctx) / _NOTES).unlink(missing_ok=True)
        return {"cleared": True}

    def _snapshot(ctx) -> dict | None:
        day = _load_day(ctx)
        if day is None:
            return None
        return {"events": day["events"], "notes": _load_notes(ctx)}

    @plugin.tool(
        "publish",
        gate="popup",
        title="Publish the day",
        description="Ask a human to confirm publishing the fetched day and notes on the plugin's page. "
        "Returns an approval id to wait on with privacyfence_await_approval.",
        effect="Publishes the stored day on the Today page once a human confirms.",
    )
    async def publish(ctx, args):
        snapshot = _snapshot(ctx)
        if snapshot is None:
            return Prepared(preview=[blocks.text("Nothing to publish: run today_refresh first.")])
        return Prepared(
            preview=[blocks.fields({"Events": len(snapshot["events"]), "Notes": len(snapshot["notes"])})],
            state=snapshot,
        )

    background: set[asyncio.Task] = set()

    async def _publish_when_confirmed(ctx, approval_id: str, snapshot: dict) -> None:
        try:
            verdict = await ctx.confirm.wait(approval_id)
            day = _load_day(ctx)
            if verdict.status == "approved" and day is not None:
                day["published"] = {"at": verdict.decided_at or datetime.now().astimezone().isoformat(), **snapshot}
                _write_json(_principal_dir(ctx) / _DAY, day)
        except SourceError:
            pass  # the plugin or PrivacyFence went away; nothing is published

    @publish.execute
    async def publish_run(ctx, prepared, approval):
        snapshot = prepared.state
        if snapshot is None:
            return {"published": False, "reason": "Nothing was fetched; run today_refresh first."}
        day = _load_day(ctx) or {}
        old = "\n".join(_published_lines(day.get("published")))
        new = "\n".join(_published_lines(snapshot))
        diff = (
            "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "published", "new", lineterm=""))
            or "no change"
        )
        approval_id = await ctx.confirm.request(
            "today_publish",
            "Publish today's page",
            [blocks.heading("What changes on the Today page"), blocks.diff(diff)],
        )
        task = asyncio.ensure_future(_publish_when_confirmed(ctx, approval_id, snapshot))
        background.add(task)
        task.add_done_callback(background.discard)
        return {"approval_id": approval_id}

    @plugin.tool(
        "export",
        gate="popup",
        title="Export today",
        description="Write today's fetched events to exports/<date>.csv, a file the AI client can list and read "
        "with plugin_outputs_list and plugin_outputs_read.",
        effect="Publishes a CSV file of today's events.",
    )
    async def export(ctx, args):
        day = _load_day(ctx)
        if day is None:
            return Prepared(preview=[blocks.text("Nothing to export: run today_refresh first.")])
        return Prepared(
            preview=[blocks.fields({"Events": len(day["events"]), "File": f"exports/{day['date']}.csv"})],
            state={"date": day["date"], "events": day["events"]},
        )

    @export.execute
    async def export_run(ctx, prepared, approval):
        if prepared.state is None:
            return {"exported": False, "reason": "Nothing was fetched; run today_refresh first."}
        path = f"exports/{prepared.state['date']}.csv"
        try:
            ctx.outputs.publish(path, _csv_text(prepared.state["events"]))
        except FileExistsError:
            return {"exported": False, "path": path, "reason": "Today was already exported."}
        return {"exported": True, "path": path}

    @plugin.tool(
        "approve_layout",
        gate="popup",
        title="Approve the page layout",
        description="Ask a human to approve the layout template of the Today page. The approval stays valid "
        "until the template changes or the human revokes it in Settings.",
        effect="Records a human's approval of the page layout.",
    )
    async def approve_layout(ctx, args):
        return Prepared(preview=[blocks.text("Ask for approval of the Today page layout")])

    @approve_layout.execute
    async def approve_layout_run(ctx, prepared, approval):
        ticket = await ctx.approvals.request(
            _LAYOUT_KIND,
            _LAYOUT_SUBJECT,
            content=_LAYOUT,
            title="Approve the Today page layout",
            preview=[blocks.code(_LAYOUT)],
            page="/approval",
        )
        return {"approval_id": ticket.approval_id, "status": ticket.status}

    if crash_tool:

        @plugin.tool(
            "crash",
            gate="auto",
            title="Crash the plugin",
            description="Exit the plugin process, so a tester can watch it restart.",
            effect="Stops the plugin process.",
        )
        async def crash(ctx, args):
            return Prepared(preview=[blocks.text("Exit the plugin process")])

        @crash.execute
        async def crash_run(ctx, prepared, approval):
            os._exit(3)

    # ------------------------------------------------------------------ page

    @plugin.page("/")
    async def home(ctx, request):
        try:
            manifest_text = (_home() / MANIFEST_FILENAME).read_text(encoding="utf-8")
        except OSError:
            manifest_text = "The manifest is not next to the executable."
        day = _load_day(ctx) or {}
        try:
            status = await ctx.approvals.check(_LAYOUT_KIND, _LAYOUT_SUBJECT, _LAYOUT)
        except SourceError:
            status = "unknown"
        revoked = _read_json(ctx.data_dir / _REVOKED, {})
        revoked_at = revoked.get("at") if isinstance(revoked, dict) else None
        return Html(
            _page_html(day, _load_notes(ctx), _total_fetches(ctx), manifest_text, _layout_note(status, revoked_at))
        )

    @plugin.page_index
    async def pages(ctx) -> list[PageEntry]:
        return [PageEntry("/", "Today")]  # /approval is framed by the approval card, not browsed

    @plugin.page("/approval")
    async def approval_page(ctx, request):
        return Html(_approval_page_html(request.query.get("pf_approval", "")))

    # ------------------------------------------------------------------ events and purge

    @plugin.on("connector.state_changed")
    async def connector_changed(ctx, params):
        if params.get("connector") != "calendar":
            return
        day = _load_day(ctx)
        if day is not None:
            day["stale"] = True
            _write_json(_principal_dir(ctx) / _DAY, day)

    @plugin.on("approval.revoked")
    async def approval_revoked(ctx, params):
        if params.get("kind") == _LAYOUT_KIND and params.get("subject_id") == _LAYOUT_SUBJECT:
            _write_json(ctx.data_dir / _REVOKED, {"at": datetime.now().astimezone().isoformat()})

    @plugin.on("plugin.disabling")
    async def disabling(ctx, params):
        _flush_counter(ctx)

    @plugin.on("shutdown")
    async def shutdown(ctx, params):
        _flush_counter(ctx)

    @plugin.on_purge
    async def purge(ctx, scope, principal):
        # PrivacyFence deletes the directories afterwards; this drops the files and any count still
        # held in memory. A purge of everything names no principal, so it covers the directories
        # this process has used.
        directories = {ctx.principal.storage_dir, *used_dirs} if principal else set(used_dirs)
        if scope in ("all", "install"):
            pending["fetches"] = 0
            (ctx.data_dir / _COUNTER).unlink(missing_ok=True)
            (ctx.data_dir / _REVOKED).unlink(missing_ok=True)
        if scope in ("all", "principal"):
            for directory in directories:
                for name in (_DAY, _NOTES):
                    (directory / name).unlink(missing_ok=True)

    return plugin


plugin = build_plugin(crash_tool=_flags().get("crash_tool") is True)


def self_test() -> str:
    """Build every part of the plugin without starting it; the line the build prints on success."""
    tools = plugin.tool_definitions()
    names = {t["name"] for t in tools}
    expected = {"status", "refresh", "list_events", "add_note", "clear_notes", "publish", "export", "approve_layout"}
    if not expected <= names:
        raise SystemExit(f"today: missing tools {sorted(expected - names)}")
    return f"{NAME} ok protocol {PROTOCOL_VERSION}"


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if "--self-test" in args:
        print(self_test())
        return
    plugin.run()


if __name__ == "__main__":
    main()
