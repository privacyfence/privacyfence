# today

A small but real PrivacyFence plugin: it shows today's Google Calendar events on a page. It is built
only on `privacyfence-plugin-sdk`, it uses every part of the plugin framework at least once, and it
is the readable example for plugin authors. The framework itself is described in
https://github.com/privacyfence/privacyfence/issues/846.

Nothing here ships in PrivacyFence's installers. You build it yourself and copy it into the plugins
directory. Where that directory is, how to review and enable a plugin, and why one does not start are
in [the plugins guide](../../../docs/plugins.md).

## What it does

| Framework part | What `today` does |
|---|---|
| `initialize`, version negotiation | Declares protocol `1`, its tools and its `calendar` scope type. |
| Source API | `source.call calendar.list_events` for today, local midnight to midnight, on the primary calendar or another one the person picks. |
| Storage | Caches the last fetched day and the notes in its per-principal directory (`day.json`, `notes.json`), and keeps a fetch counter in the install-wide directory (`counter.json`). |
| Tool, `auto`, read-only | `today_status`: whether today has been fetched, when, how many events, and whether the data is stale. It returns no event content. |
| Tool, `auto`, not read-only | `today_refresh`: fetches today again. The manifest declares `max_gate_floor: auto`, so the enable card shows that floor. |
| Tool, `review`, with a scope | `today_list_events`: titles, times and attendees as a table. The scope is `plugin:today:calendar=<calendar id>`, so a rule can auto-accept the primary calendar but not others. |
| Tool, `popup` | `today_add_note`: attaches a note to one event, stored by the plugin only. The calendar is never written. |
| Tool, `popup`, destructive | `today_clear_notes`: deletes all of today's notes. |
| Confirmation | `today_publish`: asks a human to confirm, with a heading and a diff of what changes on the page, then publishes. It returns the `approval_id`, so the AI client waits with `privacyfence_await_approval`. |
| Approval | `today_approve_layout`: asks a human to approve the page's layout template (kind `page-layout`, subject `today/layout`). The card embeds the plugin's `/approval` page, which shows the template. The approval stays valid until the template changes or it is revoked in Settings. |
| Outputs | `today_export`: publishes today's events as `exports/<date>.csv`. The manifest declares `outputs: true` and `output_types: [text/csv]`; the AI client lists and reads the file with `plugin_outputs_list` and `plugin_outputs_read`. |
| Page | `/plugins/today/` shows the published day, the notes, a stale banner when needed, a code block with the raw manifest, and a script that reports whether `document.cookie` is readable, and a note saying whether the layout is approved. The page is self-contained: the sandbox gives it no session cookie, so it cannot load anything else from PrivacyFence. |
| Events | `connector.state_changed` for Calendar marks the cached day stale. `approval.revoked` records when the layout approval was revoked, for the page note. `storage.purge` deletes its files. `plugin.disabling` and `shutdown` flush the counter. |
| Supervision | The hidden tool `today_crash` exits the process. It is listed only in a build made with `--with-crash-tool`. |

## Build

You need a checkout of this repository, Python 3.11 or newer and PyInstaller (`pip install -e ".[dev]"`
installs it). The build is for the OS you run it on.

```
python scripts/build_example_plugin.py today --with-crash-tool --out dist/plugins
dist/plugins/today/today-plugin --self-test
```

On Windows the executable is `today-plugin.exe`. The self-test prints `today ok protocol 1.2.0` and
exits. The plugin lists its page for the page browser. The folder `dist/plugins/today/` holds the
executable, `privacyfence-plugin.yaml` and `build-flags.json`. Leave out `--with-crash-tool` for a build without the hidden tool.

## Smoke test

Run it on a packaged PrivacyFence install (macOS, Windows or Linux) with Google Calendar connected
and at least two events today. The plugins directory is `/Library/PrivacyFence/plugins` on macOS,
`C:\Program Files\PrivacyFence Plugins` on Windows and `/usr/local/lib/privacyfence/plugins` on
Linux; only an administrator can write there.

1. Build `today` with `--with-crash-tool` and copy the `today` folder into the plugins directory as
   an administrator. Then try it from a user-writable copy and confirm PrivacyFence refuses to start
   it.
2. In Settings, open Plugins and choose "Review and enable" on `today`. Check that the card lists the
   eight tools (nine with the crash tool) with their gates, the `auto` floor, `calendar.list_events`
   and the page, and that it asks for the passkey.
3. Ask the AI client for `today_status`, then `today_refresh`. The audit log shows a
   `calendar.list_events` entry attributed to `today`, with no content.
4. Ask for `today_list_events`. A review card shows the events table. Approve it, and the AI client
   gets exactly that table. Add the suggested auto-accept rule for the primary calendar and ask
   again: no card. Ask for another calendar: a card.
5. Ask for `today_add_note`: a popup card. Approve it. Repeat the identical call and check you get a
   new card (single-use).
6. Ask for `today_publish`: a confirmation card with a diff and a passkey prompt. Approve it, then
   open `/plugins/today/` and check the events and notes are shown and the cookie check says it is
   unreadable.
7. Sign Calendar out in Settings. The page shows the stale banner. Sign back in and run
   `today_refresh`.
8. Run `today_crash` five times and check that Settings shows `today` disabled with the reason.
   Re-enable it.
9. Replace the executable with a rebuilt one and check `today` is disabled until you re-enable it.
10. Choose "Delete this plugin's data" in Settings, then remove the plugin folder. Its data folders
    are gone and its tools disappear from the AI client.

Steps 13 to 16 try approvals and outputs; run them before step 10 on an install where `today` is
still enabled.

13. Click "Review and enable" on `today` and check that the dialog lists `today_export` and
    `today_approve_layout` and shows "Publishes output files: CSV". Enable it. Then ask the AI client
    to call `today_approve_layout` and approve the popup card. An approval card follows. At the top,
    outside the framed area, it shows Plugin Today, Kind `page-layout`, Subject `today/layout` and a
    full `sha256:` digest. Below them, the framed plugin page shows the layout template. Approve it
    with your passkey.
14. Open the `today` page (Settings → Plugins → Open page), then expand Approvals under `today` in
    Settings. The page says the layout is approved, and Approvals lists one entry with kind, subject,
    a shortened digest and today's date. Ask for `today_approve_layout` again: there is no second
    approval card, because the same digest is already approved.
15. Click Revoke next to the approval, then reload the `today` page. The entry shows "Revoked
    <date>" and the page says the layout is not approved. Ask for `today_approve_layout` again: a new
    card appears. Deny it.
16. Ask for `today_export` and approve the popup. Then ask the AI client to call
    `plugin_outputs_list` for plugin `today`: no card, and it shows `exports/<today>.csv` with its
    size. Ask it to call `plugin_outputs_read` for that file: a review card shows the CSV text in its
    expandable pane. Approve it, and the AI client gets the same text. Ask again and pick Always
    allow → "this folder" (`exports/`). A third read shows no card. Finally open Export Audit Log…
    in Settings: it has `plugin_approval` rows ("page-layout; requested / approved / revoked /
    denied") and `plugin_output` rows ("read exports/…; offset=0; bytes=…"), and no row contains the
    CSV content or the digest.

## Tests

`tests/unit/examples/test_today_plugin.py` runs the plugin against a recorded Calendar fixture with
the SDK's `PluginTestHost`, one test class per row of the table above.
