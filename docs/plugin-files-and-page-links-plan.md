# Plan: files for plugin tools, links from plugin pages, and the plugin-page CSP stated

## Goal

The pages plugin ([privacyfence/pf-pages](https://github.com/privacyfence/pf-pages), plan
`pf-pages-plan.md` in the `docs` directory on its `main`) publishes AI-built HTML dashboards and shows them as plugin
pages. The owner's two reference pages are self-contained dashboards of 600 to 670 KB: inline
`<script>` and `<style>`, JSON in `<script type="application/json">`, `data:` fonts and images, no
network requests. Their Jira, Salesforce and report links use `target="_blank"`. Three framework
changes make such pages work:

1. **Links open in new tabs, for a plugin that asks.** A plugin whose manifest sets
   `page_new_tabs: true` has its pages served with `allow-popups allow-popups-to-escape-sandbox`,
   so a `target="_blank"` link opens a normal new tab. The owner sees this on the "Review and
   enable" dialog. Every other plugin page keeps the ADR 0124 sandbox exactly. Same-tab links
   already work and are now recorded and tested.
2. **The plugin-page CSP is written down and tested.** `docs/plugin-protocol.md` states the exact
   `Content-Security-Policy` of a plugin page, and a real-browser test proves a page can run inline
   script, use inline `<style>` and `style` attributes, and load `data:` images and `data:` fonts.
   Nothing in it changes: it already allows all of these.
3. **Plugin tools take files without the AI writing their bytes (protocol 1.3).** A tool parameter
   can be a file parameter. The AI client passes a reference (a local path, or `upload:<upload_id>`
   from `privacyfence_create_upload_slot`), PrivacyFence reads the file, shows its name, size,
   declared and detected type and SHA-256 on the approval card, and hands the bytes to the plugin.
   The SDK and `PluginTestHost` support it.

Which pf-pages tools take a file (page HTML, `asset_put`, `data_put`) is the pf-pages plan's
follow-up, not this plan's.

Tracking issue: [privacyfence/privacyfence#846](https://github.com/privacyfence/privacyfence/issues/846).

## Current state

### The stack this builds on

- `main` (203ce182) is at protocol **1.1.0** (`src/privacyfence/plugins/constants.py:13`).
- The plugin page browser was first planned on `plan/plugin-page-browser`. That plan was folded into
  `plan/plugin-framework-hardening` and built on **`feature/plugin-framework-hardening`**, now open as
  [privacyfence/privacyfence#873](https://github.com/privacyfence/privacyfence/pull/873) (head 690e3c8b,
  all phases and `origin/main` merged, final review approved, its plan document deleted). It has
  **protocol 1.2.0** (`pages.list`, `PageEntry`, the `/plugin-pages` browser, `WRITE_RESULT_MAX_BYTES`,
  `SEND_TIMEOUT_SECONDS`) and ADRs 0136 to 0139 (0139 is the page browser, amending ADR 0124). The PR
  was still blocked on CI and review when this plan was last checked.
- This plan therefore starts from PR 873 (`manual_before` `mb1-hardening-finished`). Its p0 merges
  `origin/main` if #873 has merged, and otherwise the branch first; in that case this plan's PR
  carries #873's diff until #873 merges.
- **ADR numbers.** `docs/adr/` ends at 0139 once #873 is in, so this plan's two ADRs are expected to take
  0140 and 0141; p8 takes the next two free numbers whatever they are.

Paths and line numbers below were taken at 2bd334d9 on `feature/plugin-framework-hardening` and the
symbols re-checked at #873's head 690e3c8b; line numbers may be a few lines off. This branch merged 45e955ac.

### Plugin pages (ADR 0124)

- `src/privacyfence/plugins/pages.py:22-31`: the sandbox headers.

  ```python
  CSP = (
      "sandbox allow-scripts; default-src 'self' data: 'unsafe-inline'; "
      "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
  )
  CSP_EMBEDDED = ("sandbox allow-scripts; default-src 'self' data: 'unsafe-inline'; form-action 'none'; "
                  "base-uri 'none'; frame-ancestors 'self'")
  CACHE_CONTROL = "private, no-store"
  ```

- `src/privacyfence/web/server.py:417` `_PLUGIN_PAGES_PREFIX = "/plugins/"`, and
  `_SecurityHeadersMiddleware` (`server.py:420-505`) sets, for every response under that prefix:
  `Content-Security-Policy` (`CSP`, or `CSP_EMBEDDED` when `csp.plugin_embed_for(scope)`),
  `X-Frame-Options: DENY` (`SAMEORIGIN` when embedded), `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: private, no-store`, `Permissions-Policy`
  (`_PERMISSIONS_POLICY`, `server.py:173-178`) and `Cross-Origin-Opener-Policy: same-origin`.
- `src/privacyfence/web/csp.py:53,110-120`: the per-response flag pattern (`_PLUGIN_EMBED_KEY`,
  `set_plugin_embed(request)`, `plugin_embed_for(scope_or_request)`, via `_state_flag`).
- `src/privacyfence/web/routes_plugins.py:71-80` `_embed_allowed` asks the host with
  `getattr(host, "approval_embed_allowed", None)`, and `plugin_page` (`:89-106`) calls
  `csp.set_plugin_embed(request)` before `render_plugin_page`.
- `src/privacyfence/plugins/host.py:948` `web_request()`; `:780-800` builds `plugin.review`, the dict
  the "Review and enable" dialog renders.
- `src/privacyfence/plugins/manifest.py`: `_KEYS` (`:29`), the frozen `Manifest` dataclass
  (`:40-52`) and `load_manifest`; an unknown key is an error.
- `src/privacyfence/settings_window_html.py:1270-1300`: the enable dialog's JS, which prints one
  `<div class="pf-plugin-facts">` per manifest fact (`Serves its own pages: yes.` and so on).
- `plugin-sdk/src/privacyfence_plugin_sdk/testing/_pages.py:16-22`: the SDK's copy of `CSP`.
- `tests/integration/test_plugin_pages_browser.py` loads a plugin page in real Chromium.
  `TestLinksBetweenPages` already proves that a **same-tab navigation from a sandboxed top-level
  page works**: clicking `<a href="/plugins/demo/two">` navigates, and the target gets the
  owner-only 404 because the opaque-origin initiator is cross-site and the `SameSite=Strict`
  session cookie (`web/session_auth.py:330`) is not sent.

What the current CSP allows, by directive fallback (CSP3: `script-src`, `style-src`,
`style-src-attr`, `img-src`, `font-src`, `connect-src` and `frame-src` all fall back to
`default-src`):

| Need of a dashboard | Allowed by | Result |
|---|---|---|
| Inline `<script>` | `'unsafe-inline'` | allowed |
| `<script type="application/json">` | not executed, so no directive applies | allowed |
| Inline `<style>` and `style="…"` | `'unsafe-inline'` (style-src and style-src-attr) | allowed |
| `data:` images | `data:` | allowed |
| `data:` fonts (`@font-face src: url(data:…)`) | `data:` (font-src) | allowed |
| `eval`, `new Function` | no `'unsafe-eval'` | blocked |
| `blob:` URLs, external hosts | not listed | blocked |
| File downloads (`<a download>`) | sandbox without `allow-downloads` | blocked |

So item 2 needs no CSP change. `eval` and downloads stay blocked; they are not in this plan's
requirements and are listed under Risks.

### Tool calls (ADR 0122)

- `src/privacyfence/plugins/connector.py` (`PluginConnector`): `call()` (`:218-240`) computes
  `key = canonical_key(self._plugin, tool, args)`, then `_reuse_or_prepare` (`:242-276`),
  `_prepare` (`:278-312`, sends `tool.prepare` with `call_id`, `principal`, `tool`, `args`,
  `reason`), `_gate` (`:314-355`, `gated_call(..., preview_blocks=to_card_blocks(prepared.preview +
  payload), args=args, dedupe_extra=prepared.call_id)`) and `_execute` (`:362-395`, sends
  `tool.execute` with `args` and `args_digest(args)`). `PreparedCall` is at `:76-84`, and
  `_tool_spec` (`:174-195`) maps each scalar property to a `ToolParam`.
- `src/privacyfence/plugins/tools.py:64-80` `_check_parameters` accepts a property whose `type` is
  scalar and that has no `enum`; other keys in a property are ignored. `tool_signature` (`:35`) is
  `(name, gate, read_only, destructive, scopes)` and is persisted as a five-element list
  (`plugins/state.py:61,91`).
- `src/privacyfence/plugins/protocol.py:208` `ToolDef` keeps `parameters` as a raw dict.

### Files today (ADR 0007, ADR 0028, ADR 0102)

- `src/privacyfence/local_files.py`: `require_local_files(paths, *, max_total_bytes,
  download_mode)` (`:286`) resolves each path in this order: already read this call; an
  `upload:<id>` reference (`UPLOAD_REF_PREFIX`, `:96`) peeked from `UploadStagingStore` for
  `current_principal()` with a hold of `UPLOAD_HOLD_SECONDS` (20 minutes); the shim's bridge upload
  map; a direct read when `can_access_user_files(download_mode)` (unseparated local install);
  `LocalFilesNeeded` for the shim handshake; otherwise `LocalFileAccessError(NO_BRIDGE_UPLOAD_MESSAGE)`.
  `read_local_file` (`:387`) returns the bytes; `commit_uploads()` (`:266`) claims every slot the
  call read and must run after the gate, before the write (ADR 0102).
- `build_upload_slot` (`:338`) mints a capability slot: `DEFAULT_CAPABILITY_UPLOAD_MAX_BYTES =
  50_000_000`, 10-minute TTL, fill once, bound to the creating principal.
  `src/privacyfence/upload_staging.py:84-100` `_PendingSlot` keeps `declared_path` (the slot's
  `filename`); `peek` (`:300`) and `claim` (`:326`) return bytes only.
- `src/privacyfence/web/routes_mcp.py:435-450` enters `local_files.call_context` around every tool
  call and turns `LocalFilesNeeded` into the shim's `need_uploads` response, for any connector.
- `src/privacyfence/web/mcp_tools.py:292` `CREATE_UPLOAD_SLOT_TOOL` and its description.
- `src/privacyfence/connectors/drive.py:1485-1530,1623` is the pattern: build `upload:<id>` from an
  `upload_id`, `require_local_files`, `read_local_file`, gate, `commit_uploads()`, write.

### The SDK and the test host

- `plugin-sdk/src/privacyfence_plugin_sdk/plugin.py`: `PROTOCOL_VERSION = "1.2.0"` (`:34`),
  `Context` (`:447`), `_validate_parameters` (`:510`), `Plugin.tool` / `_check_tool` (`:569-640`),
  `tool_definitions` (`:672`), `_initialize` (`:731`), `_prepare` (`:754`), `_execute` (`:818`),
  `_PreparedEntry` (`:490`). An execute function is `async def fn(ctx, prepared, approval)`.
- `plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py`: `PluginTestHost.__init__` (`:227`),
  `_check_tool_defs` (`:102`, the daemon's floors), `call_tool` (`:432`), `_run_call` (`:469`).
- The SDK never imports `privacyfence` (ADR 0126); it copies rules and a test checks the copies.
- `tests/fixtures/plugins/echo/` is the plugin both the conformance test
  (`tests/integration/test_sdk_testhost_conformance.py`) and the harness (`harness.py`: `Stack`,
  `mcp_session`, `web_session`, `Popups`, `CardCapture`) use.

## Design

### 1. Same-tab navigation (no change)

A sandboxed top-level document may navigate itself: the sandbox's navigation flags restrict only
navigating *other* browsing contexts (an ancestor or another tab), and a plugin page is top-level.
`TestLinksBetweenPages` already shows it. A same-tab navigation to PrivacyFence carries no session
(the initiator is an opaque origin, so cross-site, and the cookie is `SameSite=Strict`). A same-tab
navigation to Jira is an ordinary cross-site top-level navigation: Jira's own `SameSite=Lax` or
`None` cookies are sent. p2 adds a test that a same-tab link to an external URL loads.

### 2. `page_new_tabs`: links that open new tabs

**Manifest.** A new optional key, `page_new_tabs`, boolean, default `false`. It is an error without
`pages: true`: `ManifestError("page_new_tabs needs pages: true")`. `Manifest` gains the field
`page_new_tabs: bool = False`. Like every manifest change it changes the manifest hash, so a plugin
that adds it is "executable or manifest changed, enable again" and goes back through review.

**Headers.** `plugins/pages.py` gains

```python
CSP_NEW_TABS = (
    "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; default-src 'self' data: 'unsafe-inline'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)
```

and adds it to `__all__`. `CSP` and `CSP_EMBEDDED` do not change. The framed page of an approval
card never gets popups: `CSP_EMBEDDED` wins over `CSP_NEW_TABS`.

**Flag.** `web/csp.py` gains `_PLUGIN_NEW_TABS_KEY = "csp_plugin_new_tabs"`,
`set_plugin_new_tabs(request) -> None` and `plugin_new_tabs_for(scope_or_request) -> bool`, built
exactly like `set_plugin_embed` / `plugin_embed_for`, both in `__all__`.

**Route.** `routes_plugins.plugin_page`, after the owner check and the name check and before
`render_plugin_page`: `if _new_tabs_allowed(plugin_host, name): csp.set_plugin_new_tabs(request)`,
with

```python
def _new_tabs_allowed(host: PageHost, name: str) -> bool:
    """Whether ``name``'s manifest lets its pages open new tabs. A host without ``page_new_tabs`` answers no."""
    check = getattr(host, "page_new_tabs", None)
    return check is not None and check(name) is True
```

`plugin_root` (the 307 redirect) never sets it.

**Host.** `PluginHost.page_new_tabs(self, name: str) -> bool`: true only when the plugin exists, its
manifest has `pages: true` and `page_new_tabs: true`, and its state is `running`; otherwise false
(never raises). `plugin.review` gains `"page_new_tabs": manifest.page_new_tabs`, next to `"pages"`.

**Middleware.** In `_SecurityHeadersMiddleware`, the `/plugins/` branch becomes: embedded →
`CSP_EMBEDDED` and `X-Frame-Options: SAMEORIGIN`; else `plugin_new_tabs_for(scope)` →
`CSP_NEW_TABS`; else `CSP`. Everything else (COOP `same-origin`, `Referrer-Policy: no-referrer`,
`Cache-Control`) is unchanged.

**Enable dialog.** In `settings_window_html.py`, right after the "Serves its own pages" fact:

```js
if (r.page_new_tabs) html += '<div class="pf-plugin-facts"><strong>Its pages can open links in new tabs.</strong> A tab opened that way is outside PrivacyFence\'s sandbox.</div>';
```

**The SDK test host** (p6): `PluginTestHost(..., page_new_tabs: bool = False)`; `ValueError("page_new_tabs needs pages=True")`
when set without `pages`; `testing/_pages.py` gains its own `CSP_NEW_TABS` (same string) and uses it
for every page response when the host was built with `page_new_tabs=True`.

**Why per plugin and not for every page.** The constraint is that the sandbox loosens only as far
as links need. Most plugin pages (status pages, the `today` example) have no outbound links, and a
plugin that shows AI-written HTML is exactly where an owner should decide. The manifest key puts
the decision on the review dialog and under the manifest hash.

**The risks, weighed (these go into the ADR):**

- **An escaped popup is an unsandboxed top-level tab.** It can be any URL, PrivacyFence's own
  included. Its navigation is initiated by the opaque-origin plugin page, so it is cross-site, and
  the `SameSite=Strict` session cookie is not sent: a popup to `/settings`, `/approvals/<id>` or
  another plugin page arrives without a session and gets the owner-only 404 or the sign-in page.
  Every GET route either needs that cookie or is a capability URL (`/downloads/<token>`,
  `/mcp-files/fetch/<token>`) whose token the page cannot know, and no GET route changes state
  (state changes are POST with a CSRF check). A same-tab navigation could already reach every one
  of these URLs the same way, so popups add no new GET-CSRF reach. p2's browser test opens a popup
  to `/plugins/demo/two` and asserts the 404 and that the plugin was never asked.
- **`window.opener`.** The plugin page is served with `Cross-Origin-Opener-Policy: same-origin`, and
  its origin is opaque, so every page a popup lands on is cross-origin to it: the browser puts the
  popup in a new browsing-context group, the popup's `window.opener` is `null`, and the plugin
  page's handle to the popup is closed. The popup cannot navigate the plugin tab (no reverse
  tabnabbing), and the plugin page cannot script the popup. `Referrer-Policy: no-referrer` keeps the
  plugin page's URL out of the new tab's `document.referrer`. p2 asserts all three in Chromium;
  `manual_after` checks Firefox and Safari.
- **Popups without a user gesture.** `allow-popups` does not require a click; the browser's popup
  blocker does, and it blocks `window.open` without transient user activation. A page can still open
  one tab per click, as any web page can. PrivacyFence adds nothing here. Playwright launches
  Chromium with `--disable-popup-blocking`, so this is a `manual_after` check, not a CI test.
- **Phishing look-alikes.** A page could open a look-alike sign-in page in a new tab. It could
  already navigate its own tab there today. The new tab shows its real URL in the address bar, and
  the review dialog names the capability. Accepted.
- **Exfiltration.** A plugin page can already send what it shows to any host by navigating its own
  tab (navigation is not governed by CSP). Popups do not widen that. It is recorded so nobody reads
  the sandbox as a data-loss boundary for page content.

### 3. Stating the CSP

`docs/plugin-protocol.md`'s `### Pages` section (last phase) gets: the three exact
`Content-Security-Policy` values (`CSP`, `CSP_NEW_TABS`, `CSP_EMBEDDED`) and when each applies; the
other headers with their exact values, `Permissions-Policy` written out from `_PERMISSIONS_POLICY`;
the "What the current CSP allows" table from Current state; and the same-tab and new-tab rules.
No CSP change, so no ADR note beyond the new-tabs ADR's Context, which records that the policy was
checked against the reference pages' needs.

### 4. The preview script for the manual browser checks

New `scripts/plugin_page_preview.py`, so the owner can open any HTML file, or a built-in check page,
as a plugin page in their own browser without a plugin:

```
python3 scripts/plugin_page_preview.py [--html PATH | --check-page] [--new-tabs] [--port N]
```

- Exactly one of `--html` and `--check-page` (argparse mutually exclusive group, required).
  `--port` defaults to 8765.
- It sets `privacyfence.paths.data_dir` to a `tempfile.TemporaryDirectory()` (as
  `tests/integration/test_plugin_pages_browser.py`'s `server` fixture does with `monkeypatch`),
  starts `WebServer(WebApprovalUI(), host="localhost", port=port, plugin_host=PreviewHost(body,
  new_tabs))`, and prints two lines:
  `Open this first: http://localhost:<port>/approvals?bootstrap=<srv.bootstrap.mint(provenance=PROVENANCE_HUMAN)>`
  and `Then the page: http://localhost:<port>/plugins/preview/`. It runs until Ctrl+C, then calls
  `srv.stop()`.
- `class PreviewHost` has `async def web_request(self, name, path, query, principal) -> dict`
  returning `{"status": 200, "headers": {"content-type": "text/html; charset=utf-8"}, "body": body}`
  for `name == "preview"` and raising `LookupError` otherwise, and `def page_new_tabs(self, name) ->
  bool` returning `new_tabs and name == "preview"`.
- `CHECK_PAGE` (a module constant) is one HTML page with: an inline `<style>` giving `#css` the color
  `rgb(0, 128, 0)`; `<span id="attr" style="color: rgb(0, 0, 255)">`; an `@font-face` named
  `PfCheck` with `src: url(data:font/woff2;base64,d09GMgABAAAAAA==)`, used by `#font`; `<img id="img"
  src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=">`;
  `<script type="application/json" id="data">{"ok": true}</script>`; and an inline script that
  collects `securitypolicyviolation` events (listener added first thing) and, on `load`, writes
  into `<pre id="out">` a JSON object with `script: true`, `css` (computed color of `#css`), `attr`
  (computed color of `#attr`), `json` (`JSON.parse(#data).ok`), `img` (`#img.naturalWidth`) and
  `violations` (each event's `violatedDirective` and `blockedURI`), after awaiting
  `document.fonts.load('16px PfCheck').catch(() => null)`. Below that: `<a id="newtab"
  href="https://example.com/" target="_blank">example.com in a new tab</a>`, `<a id="sametab"
  href="https://example.com/">example.com in this tab</a>`, `<a id="own" href="/settings"
  target="_blank">PrivacyFence Settings in a new tab</a>`, and `<button id="nogesture">` whose text
  says the page tried `window.open` on load (the script calls `window.open("https://example.com/")`
  once on `load`, without a gesture, and writes whether it returned a value into `#nogesture`).
- A failing `--html` read prints `Cannot read <path>: <error>` to stderr and exits 2.

The script is dev tooling, never packaged. `tests/unit/test_plugin_page_preview.py` covers
`PreviewHost` and the argument parsing (without starting a server).

### 5. Protocol 1.3.0: file parameters

`PROTOCOL_VERSION = "1.3.0"` in `plugins/constants.py` and the SDK's `plugin.py`. Version 1.3 adds,
without changing any 1.2 message: the manifest key `page_new_tabs`, file parameters in tool
definitions, and the `files` member of `tool.prepare` and `tool.execute`. A 1.0 to 1.2 plugin never
declares a file parameter, so it is never sent `files`, and its pages keep `CSP`.

**New constants** (`plugins/constants.py`, all in the schema's `x-limits`):

```python
FILE_PARAM_KEY = "x-privacyfence-file"
MAX_FILE_BYTES = 8 * 1024 * 1024        # per file; base64 of it plus the envelope fits MAX_LINE_BYTES
MAX_FILE_PARAMS_PER_TOOL = 1
FILE_MEDIA_TYPES = (
    "text/html", "text/plain", "application/json", "application/pdf",
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "font/woff", "font/woff2", "font/ttf", "font/otf",
    "application/octet-stream",
)
UPLOAD_ID_RE = re.compile(r"[A-Za-z0-9_-]{43}")   # a 32-byte slot token, base64url without padding
```

**A file parameter** is a property whose schema carries `FILE_PARAM_KEY`:

```json
"html": {"type": "string", "description": "The page.",
         "x-privacyfence-file": {"max_bytes": 1048576, "media_types": ["text/html"]}}
```

Rules, checked by `tools._check_parameters` (daemon) and `_validate_parameters` / `_check_tool`
(SDK), in this order, each a `ToolDefError` / `ToolDefinitionError` with exactly this detail
(`<p>` the parameter, `<t>` the tool):

1. `type` is `"string"`: `parameter <p> of <t>: a file parameter must have type string`.
2. The value of `x-privacyfence-file` is an object with exactly the keys `max_bytes` and
   `media_types`: `parameter <p> of <t>: x-privacyfence-file takes max_bytes and media_types only`.
3. `max_bytes` is an integer (not a bool) from 1 to `MAX_FILE_BYTES`:
   `parameter <p> of <t>: max_bytes must be 1 to 8388608`.
4. `media_types` is a non-empty list of distinct strings, each in `FILE_MEDIA_TYPES`:
   `parameter <p> of <t>: media_types must be a non-empty list of distinct supported types`.
5. At most `MAX_FILE_PARAMS_PER_TOOL` file parameters per tool: `tool <t> may take at most 1 file parameter`.
6. A tool with a file parameter is not read-only: `tool <t> takes a file and cannot be read-only`.
7. A tool with a file parameter is not on the `auto` gate: `tool <t> takes a file and must use the review or popup gate`.

Rules 6 and 7 mean a file's bytes reach a plugin only in `tool.execute`, after a card (or a saved rule
the owner made) accepted the call, and no file content can come back to the AI through a read's
payload. Before that the plugin sees the metadata only.

`tool_signature` does not change (parameters were never part of it; the per-call card still gates
every file). A tool list that adds a file parameter after enable is accepted like any parameter
change.

**Daemon-side parsing.** `plugins/protocol.py` gains

```python
@dataclass(frozen=True)
class FileParamSpec:
    param: str
    max_bytes: int
    media_types: tuple[str, ...]

def file_params(defn: ToolDef) -> dict[str, FileParamSpec]
```

`file_params` reads the already validated `defn.parameters` and returns the file parameters by
name (empty for most tools). Both go in `__all__`.

**On the wire.** `tool.prepare` and `tool.execute` params gain `files`, sent only when the tool has
a file parameter and the call gave one: an object from parameter name to a `ToolFile`.
**`tool.prepare` carries the metadata only; the bytes (`content_base64`) are in `tool.execute` only**,
after the gate has passed:

| Field | Rule |
|---|---|
| `name` | 1 to 120 characters, `blocks.clean_line(name) == name`; the file's name, never a path |
| `size` | integer, the decoded byte count, at most the parameter's `max_bytes` |
| `media_type` | the declared type: from the name's extension (below) |
| `sniffed_type` | the detected type: from the bytes (below), always one of the parameter's `media_types` |
| `sha256` | 64 lowercase hex digits of the bytes |
| `content_base64` | `tool.execute` only, absent from `tool.prepare`: the bytes, standard base64 with padding |

The file parameter itself is **removed from `args`** before `tool.prepare` and `tool.execute`, like
`reason`, so the plugin never sees the path or the upload token, and `args_digest` covers the other
arguments only. The bytes are bound by `sha256`: the card showed the SHA-256 of the bytes the daemon sends
to execute, and the SDK refuses an execute whose files' SHA-256 differ from the prepared
ones with `digest_mismatch`.

Schema (`docs/plugin-protocol/protocol.schema.json`): `x-protocol-version` `"1.3.0"`; `x-limits`
gains `MAX_FILE_BYTES` (8388608) and `MAX_FILE_PARAMS_PER_TOOL` (1) (integers only: the list of types
is not an `x-limits` entry); `$defs` gains `FileParamSpec` (`max_bytes`, `media_types` whose items are an
enum of `FILE_MEDIA_TYPES`, both required, no other properties) and `ToolFile` (the table; `content_base64`
is the only optional member); `$defs.Manifest` gains `page_new_tabs` (boolean); the existing `$defs` for the `tool.prepare` and
`tool.execute` params gain an optional `files` (`additionalProperties: {"$ref": "#/$defs/ToolFile"}`).
Then `python3 scripts/gen_plugin_sdk_types.py` regenerates `plugin-sdk/src/privacyfence_plugin_sdk/types.py`.

### 6. The daemon resolves, shows and passes a file

New module `src/privacyfence/plugins/files.py` (no Starlette import):

```python
FILE_PARAM_HINT = (
    "A file, not its content: a local file path (absolute or starting with ~/), or upload:<upload_id> "
    "after you PUT the file to the upload_url privacyfence_create_upload_slot returned. "
    "At most {max_bytes:,} bytes; accepted types: {types}."
)
UNNAMED_FILE = "(unnamed file)"

@dataclass(frozen=True)
class IncomingFile:
    param: str
    name: str
    size: int
    media_type: str
    sniffed_type: str
    sha256: str
    source: str                     # "Upload slot", or the path as the AI gave it (clean_line, 200 characters)
    data: bytes = field(repr=False)

    def to_wire(self, *, with_content: bool) -> dict   # the ToolFile object; content_base64 only when with_content

def declared_media_type(name: str) -> str
def sniff_media_type(data: bytes) -> str
def file_reference(value: str) -> str
def resolve_file(spec: FileParamSpec, value: str, *, tool_title: str) -> IncomingFile
def card_block(file: IncomingFile) -> dict
def param_description(description: str, spec: FileParamSpec) -> str
```

- **`declared_media_type`**: a fixed map on the lowercased extension, so Windows' registry plays
  no part: `.html`/`.htm` → `text/html`, `.txt`/`.csv`/`.md` → `text/plain`, `.json` →
  `application/json`, `.pdf` → `application/pdf`, `.png` → `image/png`, `.jpg`/`.jpeg` →
  `image/jpeg`, `.gif` → `image/gif`, `.webp` → `image/webp`, `.woff` → `font/woff`, `.woff2` →
  `font/woff2`, `.ttf` → `font/ttf`, `.otf` → `font/otf`; anything else `application/octet-stream`.
- **`sniff_media_type`**, first match wins:
  1. `\x89PNG\r\n\x1a\n` → `image/png`; `\xff\xd8\xff` → `image/jpeg`; `GIF87a` or `GIF89a` →
     `image/gif`; `RIFF` then any 4 bytes then `WEBP` → `image/webp`; `wOFF` → `font/woff`;
     `wOF2` → `font/woff2`; `\x00\x01\x00\x00` → `font/ttf`; `%PDF-` → `application/pdf`. (OpenType
     `OTTO` and TrueType `true` are not matched: they are plain ASCII and would misread text files.)
  2. Otherwise, if the bytes decode as UTF-8 (a leading BOM `\xef\xbb\xbf` dropped) and contain no
     NUL: let `head` be the first 1024 characters with leading whitespace stripped, lowercased. If
     `head` contains `<!doctype html` or `<html` → `text/html`. Else if `json.loads` of the whole
     text succeeds → `application/json`. Else → `text/plain`.
  3. Otherwise `application/octet-stream`.

  An empty file is `text/plain`.
- **`file_reference`**: `value.strip()`; a value starting with `upload:` is returned as is; a value
  that fullmatches `UPLOAD_ID_RE` becomes `"upload:" + value`; anything else is a local path and is
  returned as is.
- **`resolve_file`**, called with `local_files.call_context` already entered by `routes_mcp`:
  1. A `value` that is not a `str` raises `LocalFileAccessError(f"{tool_title} needs a file path or
     upload id in {spec.param}.")`. `ref = file_reference(value)`; an empty `ref` raises
     `LocalFileAccessError(f"{tool_title} needs a file in {spec.param}.")`.
  2. `local_files.require_local_files([ref], max_total_bytes=spec.max_bytes, download_mode="local")`
     (plugins run in local mode only: `daemon_main._build_plugin_host` is the local-mode `PluginHost`, so a future
     org-mode plugin host must revisit this argument). It may raise `LocalFilesNeeded` (the shim fetches the file and
     re-sends the call) or `LocalFileAccessError`; both propagate unchanged.
  3. `data = local_files.read_local_file(ref, download_mode="local")`.
  4. `len(data) > spec.max_bytes` → `LocalFileAccessError(f"The file is {len(data):,} bytes, over
     the {spec.max_bytes:,}-byte limit of {tool_title}.")`.
  5. `name = local_files.resolved_name(ref)` (below), then `blocks.clean_line(name)[:120]`, and
     `UNNAMED_FILE` when that is empty.
  6. `sniffed = sniff_media_type(data)`; not in `spec.media_types` →
     `LocalFileAccessError(f"The file's content is {sniffed}, and {tool_title} accepts only
     {', '.join(spec.media_types)}.")`.
  7. Return `IncomingFile(spec.param, name, len(data), declared_media_type(name), sniffed,
     hashlib.sha256(data).hexdigest(), "Upload slot" if ref.startswith("upload:") else
     blocks.clean_line(value.strip())[:200], data)`.
- **`card_block`**: `{"type": "fields", "items": [{"label": "File", "value": name}, {"label":
  "Source", "value": source}, {"label":
  "Size", "value": f"{size:,} bytes"}, {"label": "Declared type", "value": media_type}, {"label":
  "Detected type", "value": sniffed_type}, {"label": "SHA-256", "value": sha256}]}`.
- **`param_description`**: `description` (stripped), a space when it is non-empty, then
  `FILE_PARAM_HINT` formatted with `max_bytes=spec.max_bytes` and `types=", ".join(spec.media_types)`.

**`local_files.resolved_name(path: str) -> str`**: for an `upload:<id>` reference, the basename of
the slot's `declared_path` (new `UploadStagingStore.declared_path(token: bytes, principal_id: str)
-> str | None`, `peek`'s lookup, filled check and principal check, no expiry change; `""` when it returns
`None` or the token does not decode); for a bridge path or a direct path, `os.path.basename` of the
path with both `/` and `\` treated as separators. Added to `__all__`.

**`PluginConnector`** (`plugins/connector.py`):

- `_tool_spec`: a file parameter's `ToolParam` keeps annotation `"str"` and gets
  `description=files.param_description(schema.get("description", ""), spec)`.
- `PreparedCall` gains `files: dict[str, str] = field(default_factory=dict)` (parameter → SHA-256).
- `call()`:
  1. `specs = file_params(defn)`. For each spec: `value = args.get(p) or ""`. A non-empty string
     gives `incoming[p] = files.resolve_file(spec, value, tool_title=title)`. An empty one is
     `LocalFileAccessError(f"{title} needs a file in {p}.")` when `p` is in the tool's `required`,
     and means no file otherwise.
  2. `plugin_args = {k: v for k, v in args.items() if k not in specs}`.
  3. `key = canonical_key(self._plugin, tool, args)`, plus, when `incoming` is non-empty,
     `"|files:" + ",".join(f"{p}={f.sha256}" for p, f in sorted(incoming.items()))`. A file whose bytes
     changed (a local path edited between the first call and the re-issued one) therefore gets a
     fresh prepare and its own card, never the old approval.
  4. `_reuse_or_prepare(key, peer, tool, defn, plugin_args, incoming)`; `_prepare` adds
     `"files": {p: f.to_wire(with_content=False) for p, f in incoming.items()}` to the `tool.prepare` params only when
     `incoming` is non-empty, and stores `files={p: f.sha256 ...}` on the `PreparedCall`.
  5. `_gate(...)` is called with `args` (the AI's arguments, as today, so the ledger key is stable)
     and `file_blocks=[files.card_block(f) for f in incoming.values()]`, and passes
     `preview_blocks=to_card_blocks(file_blocks + prepared.preview + payload)`.
  6. After the gate returns and before `_execute`: `local_files.commit_uploads()` (ADR 0102), then
     one audit entry per file (below).
  7. `_execute(defn, prepared, plugin_args, approval, incoming)` adds `"files": {p: f.to_wire(with_content=True)
     ...}` to `tool.execute`, and `args_digest(plugin_args)`.
- **Audit.** Modelled on `_auto_audit` (same try/except and `logger.warning`): `AuditEntry(timestamp=datetime.now(timezone.utc).isoformat(),
  week=current_week(), latency_seconds=time.time() - prepared.created_at, claude_reason=current_reason(), connector=f"plugin:{self._plugin}", tool=tool,
  tool_name=title, decision="plugin_file", auto_accept_rule="", sender="", request_id="",
  summary=f"{p}: {name}; bytes={size}; sha256={sha256}; type={sniffed_type}")`. Written only after
  the gate passed and the slot was committed, so a denied call has none. Never the content.

**The MCP dispatcher's 30-second cache.** `McpDispatcher.call` (`web/mcp_dispatch.py:77,219-235`) answers an
identical completed call from its cache for 30 seconds, keyed on the arguments without `reason`, before the
connector runs. So within 30 seconds a repeated call with the same `upload:` reference or path returns the
first result and does not read the file again; the slot rules below are enforced by the connector and are
tested on it directly, not through MCP.

**What is held, and for how long.** The daemon keeps no file bytes across calls: `PreparedCall`
holds only the SHA-256, and a re-issued call reads the file again (a slot is held for 20 minutes by
`peek`, ADR 0102). `commit_uploads()` makes a slot back exactly one approved call; the second
call that read it fails before execute with the existing "The uploaded file expired or was already
used by another call." message. A slot is only usable by the principal that created it
(`peek`/`claim` check `current_principal()`); in local mode that is the `local` principal of every
MCP session. What the plugin stores is the plugin's data, deleted by "Delete this plugin's data"
(`storage.purge`) as before.

**`privacyfence_create_upload_slot`**: its description's sentence "Then pass upload_id to the tool
that needs the file (its upload_id parameter, e.g. drive_upload_file, or an 'upload:<upload_id>'
attachments entry, e.g. gmail_*_with_attachments)." becomes "Then pass upload_id to the tool that
needs the file (its upload_id parameter, e.g. drive_upload_file; an 'upload:<upload_id>'
attachments entry, e.g. gmail_*_with_attachments; or 'upload:<upload_id>' as a plugin tool's file
parameter)."

**Enable dialog.** Each `plugin.review["tools"]` entry with a file parameter gains `"file":
{"param": p, "max_bytes": n, "media_types": [...]}`. The dialog prints, under that tool's
description: `<div class="pf-plugin-meta">Takes a file in <param>: <types>, up to <n> bytes.</div>`,
with `<n>` as `Number(n).toLocaleString('en-US')` and every value through `esc()`.

### 7. The SDK (protocol 1.3.0)

In `plugin-sdk/src/privacyfence_plugin_sdk/`:

- **New `_files.py`** (private): the SDK's copies of `FILE_PARAM_KEY`, `MAX_FILE_BYTES`,
  `MAX_FILE_PARAMS_PER_TOOL`, `FILE_MEDIA_TYPES`, `declared_media_type` and `sniff_media_type`
  (same rules as section 6, copied, not imported), plus

  ```python
  @dataclass(frozen=True)
  class IncomingFile:
      name: str
      size: int
      media_type: str
      sniffed_type: str
      sha256: str
      data: bytes | None = field(default=None, repr=False)

      @property
      def content(self) -> bytes   # raises RuntimeError("the file's bytes are available in the execute function only") when data is None

  def file_param(description: str = "", *, max_bytes: int, media_types: Sequence[str]) -> dict
  def parse_files(raw: Any, specs: Mapping[str, Mapping], *, with_content: bool) -> dict[str, IncomingFile]
  ```

  `file_param` returns `{"type": "string", "description": description, FILE_PARAM_KEY:
  {"max_bytes": max_bytes, "media_types": list(media_types)}}` (no `description` key when it is
  empty). `parse_files` turns the wire `files` into `IncomingFile`s (with `with_content=True`, `content_base64` is required, decoded, its length must equal `size` and its SHA-256 must equal `sha256`; with `False` it is ignored and `data` stays `None`) and raises
  `RpcError("invalid_params", "files.<p>: <what>")` when a name is not a declared file parameter,
  a field is missing or of the wrong type, base64 does not decode, the size exceeds that parameter's
  `max_bytes`, or `sniffed_type` is not in its `media_types`, and, with content, the length or SHA-256 differ. `None` or a missing `files` gives `{}`.
- **`__init__.py`** exports `IncomingFile` and `file_param`.
- **`plugin.py`**:
  - `_validate_parameters` and `_check_tool` apply rules 1 to 7 of section 5 with the same detail
    texts (as `ToolDefinitionError`).
  - `Context` gains `files: Mapping[str, IncomingFile]`, an empty `MappingProxyType` by default.
  - `_prepare`: `files = parse_files(params.get("files"), <the tool's file specs>, with_content=False)`, set on the
    context before the tool function runs; `_PreparedEntry` gains `files: dict[str, str]`
    (parameter → SHA-256).
  - `_execute`: parses `files` with `with_content=True`; when `{p: f.sha256}` differs from the entry's,
    `RpcError("digest_mismatch", "the files differ from the prepared call")`; otherwise the
    execute function's `ctx.files` holds them.
  - **Older daemons.** `_initialize` records the daemon's minor version
    (`int(protocol_version.split(".")[1])`, `0` when it does not parse). When it is below 3, the
    `tools` list it returns, and every later `tools_changed`, leaves out each tool with a file
    parameter and logs `"tool %s takes a file, which needs PrivacyFence with plugin protocol 1.3; it
    is not offered"` at WARNING once per tool. Introduce `_offered_definitions()` for both.
  - `PROTOCOL_VERSION` is already `"1.3.0"` from p3.
- **`README.md`**: a "File parameters" section with a short example (`params={"html":
  file_param("The page.", max_bytes=1_048_576, media_types=["text/html"])}` and
  `ctx.files["html"].content` in the execute function; the tool function sees name, size, types and
  SHA-256 only), the rules (one per tool, not read-only, not
  auto, the plugin never sees the path, `ctx.files` is empty for a call without a file), and that a
  daemon older than 1.3 is not offered such a tool.

### 8. The test host

`plugin-sdk/src/privacyfence_plugin_sdk/testing/`:

- `PluginTestHost.__init__` gains `page_new_tabs: bool = False` (section 2).
- `_check_tool_defs` applies rules 1 to 7 of section 5, as the daemon does.
- `call_tool(name, args=None, decide="approve", principal=None, files=None)`:
  `files: Mapping[str, bytes | tuple[str, bytes]] | None` maps a file parameter to its bytes, or to
  `(name, bytes)`; plain bytes get the name `"file"` plus the extension of the parameter's first media
  type from a fixed map (`text/html` → `.html`, `text/plain` → `.txt`, `application/json` →
  `.json`, `application/pdf` → `.pdf`, `image/png` → `.png`, `image/jpeg` → `.jpg`, `image/gif` →
  `.gif`, `image/webp` → `.webp`, `font/woff` → `.woff`, `font/woff2` → `.woff2`, `font/ttf` →
  `.ttf`, `font/otf` → `.otf`, `application/octet-stream` → no extension).
  The host applies the daemon's rules and returns the daemon's messages in
  `ToolOutcome.error = {"code": "invalid_params", "detail": <message>}` without calling the plugin, checked in
  this order and with file parameters left out of `call_tool`'s existing `missing <r>` check:
  a key that is not a file parameter (`"<p> is not a file parameter of <tool>"`), a file parameter
  given in `args` instead of `files` (`"pass <p> in files=, not in args"`), a required file
  parameter without a file (`"missing <p>"`), over `max_bytes`, and a sniffed type not accepted
  (the two `resolve_file` sentences of section 6, with the tool's title). Otherwise it sends
  `files` as the daemon does (metadata in `tool.prepare`, bytes added in `tool.execute`), puts the `card_block` fields
  block first on the card it shows `decide`, records one `plugin_file` audit row with the
  daemon's summary, and shows `Source` as `Test host`.
- `ToolOutcome` and `Card` keep their shapes; the card's blocks simply start with the file block.

### 9. Versions and what a plugin pins

| Feature | Daemon | SDK (`privacyfence-plugin-sdk`) |
|---|---|---|
| Same-tab links | any | any |
| `page_new_tabs` | protocol 1.3.0 | any for the plugin; 1.3.0 for `PluginTestHost(page_new_tabs=True)` |
| File parameters | protocol 1.3.0 | 1.3.0 (`file_param`, `ctx.files`, `call_tool(files=…)`) |

The SDK is built from this repository (ADR 0126), so a plugin pins a commit until a release
carries 1.3.0. Both features land together: the commit to pin is **the merge commit of this
plan's PR on `main`** (before it merges, the head of `feature/plugin-files-and-page-links`). The
last phase writes this table into `docs/plugins.md` with "the first commit on `main` where
`plugin-sdk/src/privacyfence_plugin_sdk/plugin.py` has `PROTOCOL_VERSION = "1.3.0"`", which is
mechanical to look up after the merge. pf-pages takes it in its own follow-up.

### What was rejected (this text goes into the two ADRs)

**Links**

- **`allow-popups` and `allow-popups-to-escape-sandbox` for every plugin page.** It loosens the
  sandbox for plugins that never link out. Rejected for the per-plugin key.
- **`allow-popups` without `allow-popups-to-escape-sandbox`.** The new tab would inherit the
  sandbox: Jira and Salesforce would run in an opaque origin without their own cookies or storage,
  and a target that sends `Cross-Origin-Opener-Policy` would fail to load in a sandboxed popup.
  Useless for the links in question.
- **`allow-top-navigation` or `allow-same-origin`.** Not needed (a top-level page navigates itself)
  or back in the app's origin (ADR 0124).
- **Rewriting the page's links in the daemon** (adding `rel="noopener"`, or turning `_blank` into
  `_top`). The daemon does not parse or rewrite plugin HTML, and COOP already severs the opener.
- **A PrivacyFence "leaving" interstitial.** The new tab carries no PrivacyFence session anyway,
  and the review dialog already states the capability. Not worth a route.

**Files**

- **Base64 in a string argument (today).** Every byte passes through the AI's output: slow, costly,
  and over a few hundred KB impossible.
- **A read-only path for the plugin.** The plugin would hold a path into a daemon-written file, and
  every plugin would need a cleanup rule for it. Inline bytes in the message fit the 16 MiB line at
  8 MiB per file and need no new storage.
- **Bytes in `tool.prepare` too.** The plugin could then check the file before the card. Rejected:
  ADR 0121 trusts a plugin with what its account can read, and on a privilege-separated install the
  user's own files are not that, so sending the bytes before the card would disclose a file the
  owner has not approved. The plugin gets the name, size, both types and the SHA-256 at prepare,
  which is enough to describe the call, and the bytes at execute.
- **File parameters on read-only or `auto` tools.** A read could return a local file's content to
  the AI through its payload, and an `auto` tool would hand a file to a plugin with no card. Both
  refused.
- **A new slot kind.** `privacyfence_create_upload_slot`'s slots already are single-use,
  principal-bound, expiring and consumed after the gate (ADR 0028, ADR 0102); the per-tool limit is
  checked when the slot is read.
- **The file's SHA-256 in the reviewed tool signature.** Parameters were never in the signature;
  changing its persisted five-element shape would disable every enabled plugin. The per-call card
  gates every file.
- **More than one file per tool.** Two 8 MiB files would not fit one 16 MiB line. A later minor
  version may raise `MAX_FILE_PARAMS_PER_TOOL` with a total cap.

## ADRs

Numbers are the next two free in `docs/adr/` when the last phase runs (expected 0140 and 0141,
after the hardening branch's four).

- **ADR 0140** (expected), `docs/adr/<n>-a-plugin-page-opens-links-in-new-tabs-only-when-its-manifest-says-so.md`:
  a plugin page is served with `allow-popups allow-popups-to-escape-sandbox` only when the
  plugin's manifest sets `page_new_tabs: true`, shown on the review dialog; the framed approval page
  never is; same-tab navigation is recorded as already working; the CSP is recorded as allowing
  everything a self-contained dashboard needs (inline script and style, `data:` images and fonts).
  Amends ADR 0124.
- **ADR 0141** (expected), `docs/adr/<n+1>-plugin-tools-take-files-by-reference-and-privacyfence-passes-the-bytes.md`:
  protocol 1.3 file parameters; the AI passes a path or an upload slot reference, PrivacyFence reads
  the file through the local file bridge, shows a daemon-made block on the card, and sends the bytes
  with `tool.prepare` and `tool.execute`; only gated, non-read-only tools; one file of at most 8 MiB.
  Amends ADR 0122.

## Manual steps

Step by step: [the manual steps page](https://claude.ai/artifact/BztwskChbss3zbZosuTnzL)
(`docs/plugin-files-and-page-links-plan-manual-steps.html`).

- **Before** (`mb1-hardening-finished`): [#873](https://github.com/privacyfence/privacyfence/pull/873)
  (plugin framework hardening and the page browser, protocol 1.2.0) is merged into `main`. Its branch is
  complete, so if you start before it merges, p0 merges the branch and this plan's PR carries #873's diff.
- **After** (`ma1-reference-dashboards`): open the owner's two reference dashboards through
  `scripts/plugin_page_preview.py --new-tabs` in Chrome: they render fully with no CSP violation in
  the console, and a Jira link opens a new tab with `window.opener === null`.
- **After** (`ma2-browser-matrix`): the built-in check page in Chrome, Firefox and Safari (Edge on
  Windows if available): CSP checks pass, the new-tab link opens with no opener, the no-gesture
  `window.open` is blocked, Settings in a new tab has no session, and without `--new-tabs` the
  `_blank` link does nothing.
- **After** (`ma3-claude-code-file`): from Claude Code against a dev daemon with the echo fixture
  plugin, hand a local file to `echo_file_put` through an upload slot, and check the card's file
  block and the result's SHA-256.

## Risks and open questions

- **The hardening branch is not finished.** p0 stops with `status=blocked` when
  `git ls-tree -r --name-only HEAD | grep -q plugin-framework-hardening-plan.md` succeeds after its merge, or when
  `grep -n 'PROTOCOL_VERSION = "1.2.0"' src/privacyfence/plugins/constants.py` prints nothing.
- **Line numbers.** They are at 2bd334d9. A brief names symbols; when a symbol it names is missing,
  the worker stops with `status=blocked` instead of guessing.
- **COOP and the sandbox in Chromium.** The design relies on `Cross-Origin-Opener-Policy:
  same-origin` severing the opener of a popup from an opaque-origin page. If p2's test finds
  `window.opener` not `null` in the popup, p2 stops with `status=blocked` and reports it; it does
  not add headers, rewrite HTML or relax the assertion.
- **A popup that does not open in headless Chromium.** If clicking the `_blank` link with
  `page_new_tabs` on opens no page within 5 seconds, p2 stops with `status=blocked` and reports the
  response's CSP header.
- **`eval`.** A dashboard library that needs `eval` or `new Function` (Vega, some template engines)
  is blocked by the CSP. The reference pages are believed not to need it; `ma1` checks the console.
  If one does, that is a separate decision, not this plan's.
- **Downloads.** An "export CSV" button (`<a download>` with a `data:` or `blob:` URL) is blocked by
  the sandbox (no `allow-downloads`). Not in this plan's requirements; `ma1` notes whether the
  reference pages have one.
- **Existing tests that list the echo plugin's tools.** p7 adds a tool to the echo fixture. A test
  outside p7's `touches` that asserts the exact echo tool list means `status=blocked`.
- **`LocalFilesNeeded` through the plugin connector.** It must propagate from
  `PluginConnector.call` to `routes_mcp` unchanged. If `mcp_dispatch` wraps or swallows it for
  plugin tools (p4's bridge test fails with another error), p4 stops with `status=blocked`.
- **Windows separators in names.** `resolved_name` splits on `/` and `\`; a slot filename such as
  `C:\x\page.html` shows as `page.html`. Tested in p4.

## Implementation manifest

Every code phase's brief ends with the same two rules: no `CHANGELOG.md` line (p8 writes them), and
no plan item IDs (`p4`, `ma1` and the like) in code, comments or test names, because
`tests/unit/test_code_no_history.py` refuses them. Phases that run at the same time share no path
in `touches`.

```yaml
plan_slug: plugin-files-and-page-links
feature_branch: feature/plugin-files-and-page-links
tracking_issue: 846
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/BztwskChbss3zbZosuTnzL
manual_steps_source: docs/plugin-files-and-page-links-plan-manual-steps.html
manual_before:
  - id: mb1-hardening-finished
    title: Merge PR 873 (plugin framework hardening) into main, or accept that this PR carries its diff
    why: p0 merges that branch; every phase builds on its protocol 1.2.0, the page browser and its host and SDK changes. Without it p3's version bump and p6's test host edits conflict with the remaining hardening phases.
    done_when: "PR 873 shows Merged (preferred), or you have decided to start with it open. Either way `git fetch origin && git ls-tree -r --name-only origin/feature/plugin-framework-hardening | grep plugin-framework-hardening-plan.md` prints nothing (it already does at head 690e3c8b); and on that branch merged with main, `ls docs/adr | cut -c1-4 | sort | uniq -d` prints nothing (no two ADRs share a number; true at head 690e3c8b, where its ADRs are 0136 to 0139)."
manual_after:
  - id: ma1-reference-dashboards
    title: Open the two reference dashboards as plugin pages in Chrome and follow a Jira link
    why: Only the owner has the real pages. CI proves the CSP allows each feature on a synthetic page; this proves the real 600-670 KB pages render with no CSP violation and that their target=_blank links open a working, unconnected tab.
  - id: ma2-browser-matrix
    title: Run the built-in check page in Chrome, Firefox and Safari
    why: CI runs headless Chromium with the popup blocker off. Firefox and Safari implement COOP, sandbox popups and the popup blocker on their own; this checks the no-opener result, the blocked no-gesture popup and the cookie-less PrivacyFence tab in each.
  - id: ma3-claude-code-file
    title: Hand a local file to a plugin tool from Claude Code through an upload slot
    why: The integration test drives MCP directly. This checks that a real AI client follows the file parameter's description (create a slot, PUT the file, pass upload:<id>) and that the card shows the file block.
verify_after_merge:
  - python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk tests/unit/web/test_routes_plugins.py tests/unit/web/test_server.py tests/unit/web/test_csp.py tests/unit/web/test_mcp_tools.py tests/unit/test_local_files.py tests/unit/test_upload_staging.py tests/unit/test_settings_window_html.py tests/unit/test_gen_plugin_sdk_types.py tests/unit/test_code_no_history.py -q
  - python3 scripts/gen_plugin_sdk_types.py --check
final_checks:
  - docs/plugin-files-and-page-links-plan.md and docs/plugin-files-and-page-links-plan-manual-steps.html are deleted and nothing links to them (grep -rn "plugin-files-and-page-links-plan" docs scripts src plugin-sdk README.md prints nothing)
  - The two new ADRs exist with Status Accepted, each with its "Amends" line, and are in docs/adr/README.md's index; ADR 0124 and ADR 0122 each have the "Amended by" Status line and index cell
  - CHANGELOG.md has the [Unreleased] lines from p8 and no new version heading
  - python3 scripts/gen_plugin_sdk_types.py --check exits 0
  - The full /dod passes, including python3 scripts/check_coverage_floor.py coverage.json and python3 -m pytest tests/integration -v (with PRIVACYFENCE_TEST_CHROMIUM set, and no browser test SKIPPED)
  - The PR's platform-windows and platform-macos jobs are green (resolved_name and the declared-type map run there)
  - python3 -m build plugin-sdk succeeds (delete plugin-sdk/dist and any build/ or *.egg-info afterwards)
  - "connector-live-check.yml: no phase changes a *_client.py file. If git diff --name-only origin/main...HEAD | grep -E 'src/privacyfence/[a-z_]+_client\\.py$' prints anything outside what the hardening branch already brought, dispatch connector-live-check.yml against the feature branch and link it; otherwise the PR says it was not needed"
phases:
  - id: p0-sync
    title: Merge the finished hardening branch (or main, if it already carries it) into this branch
    depends_on: []
    complexity: S
    touches:
      - docs/plugin-files-and-page-links-plan.md
    brief: |
      Read docs/plugin-files-and-page-links-plan.md "Current state / The stack this builds on".
      1. git fetch origin main feature/plugin-framework-hardening.
      2. If `git merge-base --is-ancestor origin/feature/plugin-framework-hardening origin/main` exits 0,
         run `git merge --no-ff origin/main`. Otherwise run `git merge --no-ff origin/feature/plugin-framework-hardening`
         and then `git merge --no-ff origin/main`. Resolve conflicts only in files the merged branches
         changed, keeping both sides; never edit code beyond conflict markers.
      3. Check: `git ls-tree -r --name-only HEAD | grep -q plugin-framework-hardening-plan.md` must fail, and
         `grep -n 'PROTOCOL_VERSION = "1.2.0"' src/privacyfence/plugins/constants.py` must print a line, and
         `grep -n "def list_pages" src/privacyfence/plugins/host.py` must print a line, and
         `ls docs/adr | cut -c1-4 | sort | uniq -d` must print nothing. If any check fails, stop with
         status=blocked: the hardening branch is not finished (manual_before mb1-hardening-finished).
      4. In docs/plugin-files-and-page-links-plan.md "Current state", at the end of the sentence that begins "Paths and
         line numbers below were taken at", append " This branch merged <short SHA of the merged head>." Change
         nothing else in the plan.
      5. Run `ruff check .` and `python3 -m pytest tests/unit -q`. A failure here is the merged branches' own: stop
         with status=blocked and report it; do not fix it in this phase.
      No CHANGELOG line; no plan item IDs in code.
    acceptance:
      - git ls-tree -r --name-only HEAD | grep -q plugin-framework-hardening-plan.md exits non-zero
      - grep -n 'PROTOCOL_VERSION = "1.2.0"' src/privacyfence/plugins/constants.py prints one line
      - python3 -m pytest tests/unit -q passes
      - ruff check . exits 0

  - id: p1-new-tabs
    title: page_new_tabs manifest key, CSP_NEW_TABS, the route flag and the enable dialog line
    depends_on: [p0-sync]
    complexity: M
    touches:
      - src/privacyfence/plugins/manifest.py
      - src/privacyfence/plugins/pages.py
      - src/privacyfence/web/csp.py
      - src/privacyfence/web/routes_plugins.py
      - src/privacyfence/web/server.py
      - src/privacyfence/plugins/host.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/plugins/test_manifest.py
      - tests/unit/plugins/test_pages.py
      - tests/unit/web/test_csp.py
      - tests/unit/web/test_routes_plugins.py
      - tests/unit/web/test_server.py
      - tests/unit/plugins/test_host.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Read docs/plugin-files-and-page-links-plan.md Design section 2 ("page_new_tabs") first; it is the spec.
      1. manifest.py: add "page_new_tabs" to _KEYS; `page_new_tabs = _bool(data, "page_new_tabs")`; raise
         ManifestError("page_new_tabs needs pages: true") when it is true and pages is false; add
         `page_new_tabs: bool = False` to Manifest (after output_types) and pass it.
      2. plugins/pages.py: CSP_NEW_TABS exactly as in the Design, with a one-line comment that it is CSP plus
         popups that escape the sandbox, for a plugin whose manifest sets page_new_tabs (ADR 0124's amendment);
         add it to __all__. Do not change CSP or CSP_EMBEDDED.
      3. web/csp.py: _PLUGIN_NEW_TABS_KEY, set_plugin_new_tabs, plugin_new_tabs_for, copied from the embed pair;
         both in __all__.
      4. web/routes_plugins.py: _new_tabs_allowed as in the Design, called in plugin_page after the name check and
         before render_plugin_page; extend the module docstring's exception paragraph with one sentence on it.
      5. web/server.py: in _SecurityHeadersMiddleware's /plugins/ branch, embedded wins, then
         csp.plugin_new_tabs_for(scope) -> plugin_pages.CSP_NEW_TABS, else plugin_pages.CSP. Import
         plugin_new_tabs_for the way plugin_embed_for is imported.
      6. plugins/host.py: PluginHost.page_new_tabs(name) -> bool as in the Design (running, pages and
         page_new_tabs all true; never raises); plugin.review gains "page_new_tabs": manifest.page_new_tabs right
         after "pages".
      7. settings_window_html.py: the enable dialog line from the Design, right after the "Serves its own pages"
         fact.
      8. Tests:
         - test_manifest.py: page_new_tabs defaults to False; true with pages: true loads; true without pages raises
           with the exact message; a non-bool raises "page_new_tabs must be true or false".
         - test_pages.py: CSP_NEW_TABS equals CSP with " allow-popups allow-popups-to-escape-sandbox" inserted after
           "sandbox allow-scripts" (assert by string replace), and CSP is unchanged (assert its literal).
         - test_csp.py: the new flag pair, as the embed pair is tested.
         - test_routes_plugins.py, with the file's existing fake host: a host whose page_new_tabs("demo") is True
           -> the response's CSP is CSP_NEW_TABS; False -> CSP; a host without the method -> CSP; with a valid
           pf_approval embed -> CSP_EMBEDDED (embed wins); a non-owner request -> 404 and page_new_tabs not called.
         - test_server.py: the middleware picks CSP_NEW_TABS when the flag is set on the scope, and keeps
           Cross-Origin-Opener-Policy: same-origin and Referrer-Policy: no-referrer on it.
         - test_host.py: page_new_tabs is True only for a running pages plugin whose manifest sets it; False for a
           stopped one, an unknown name and pages: false; review carries page_new_tabs.
         - test_settings_window_html.py: the dialog JS contains "Its pages can open links in new tabs." and it is
           gated on r.page_new_tabs (assert the `if (r.page_new_tabs)` text).
      Stop with status=blocked if _SecurityHeadersMiddleware no longer has a /plugins/ branch with plugin_embed_for.
      No CHANGELOG line; no plan item IDs in code, comments or test names.
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_manifest.py tests/unit/plugins/test_pages.py tests/unit/web/test_csp.py tests/unit/web/test_routes_plugins.py tests/unit/web/test_server.py tests/unit/plugins/test_host.py tests/unit/test_settings_window_html.py -q passes
      - grep -n "allow-popups-to-escape-sandbox" src/privacyfence/plugins/pages.py prints exactly one line
      - grep -c "allow-popups" src/privacyfence/web/server.py prints 0
      - ruff check . exits 0

  - id: p2-browser-checks
    title: Real-browser tests for the CSP, same-tab and new-tab links, and the preview script for the manual checks
    depends_on: [p1-new-tabs]
    complexity: M
    touches:
      - tests/integration/test_plugin_pages_browser.py
      - scripts/plugin_page_preview.py
      - tests/unit/test_plugin_page_preview.py
    brief: |
      Read docs/plugin-files-and-page-links-plan.md Design sections 1, 2 ("The risks, weighed") and 4 first.
      1. scripts/plugin_page_preview.py exactly as Design section 4 specifies (PreviewHost, CHECK_PAGE, main()
         with argparse, `if __name__ == "__main__": raise SystemExit(main())`). Use only privacyfence imports that
         tests/integration/test_plugin_pages_browser.py already uses.
      2. tests/unit/test_plugin_page_preview.py (load the script with importlib.util.spec_from_file_location, as
         other tests load scripts): PreviewHost serves the body for "preview" and raises LookupError otherwise;
         page_new_tabs is True only with new_tabs and name "preview"; the parser refuses neither and both of
         --html/--check-page; CHECK_PAGE contains id="newtab", id="sametab", id="own", id="nogesture" and
         "securitypolicyviolation".
      3. tests/integration/test_plugin_pages_browser.py: give _PageHost a `new_tabs: set[str]` (empty by default)
         and `def page_new_tabs(self, name): return name in self.new_tabs`; serve the script's CHECK_PAGE at
         "/check" (import it from the script as in step 2) and these pages:
         "/blank" = `<a id="go" href="https://jira.example.test/browse/PF-1" target="_blank">`,
         "/same" = `<a id="go" href="https://jira.example.test/browse/PF-1">`,
         "/own" = `<a id="go" href="/plugins/demo/two" target="_blank">`.
         In every new test, first `context.route("https://jira.example.test/**", lambda route: route.fulfill(
         status=200, content_type="text/html", body='<p id="jira">jira</p><script>document.title = JSON.stringify(
         {opener: window.opener === null, referrer: document.referrer})</script>'))`, so nothing leaves the machine.
         New tests (sign in the way the existing tests do):
         - TestCspAllowsSelfContainedPages.test_inline_script_style_data_images_and_fonts: open /plugins/demo/check
           (new_tabs empty); wait for #out; assert script is true, css == "rgb(0, 128, 0)", attr == "rgb(0, 0, 255)",
           json is true, img == 1, and violations has no entry whose directive starts with "script-src",
           "style-src", "img-src", "font-src" or "default-src".
         - TestCspAllowsSelfContainedPages.test_a_blocked_font_host_is_reported: a page (served at "/font") with
           `@font-face{font-family:X;src:url(https://fonts.example.test/x.woff2)}` used by text, a
           `securitypolicyviolation` listener added first, and a script that awaits `document.fonts.load('16px X')
           .catch(() => null)` before writing the collected violations to `<pre id="out">`; assert one violation whose
           directive starts with "font-src", which shows the collector works.
         - TestNewTabs.test_same_tab_link_to_an_external_site_loads: /plugins/demo/same; click #go under
           page.expect_navigation(); page.locator("#jira") is visible.
         - TestNewTabs.test_blank_link_does_nothing_without_page_new_tabs: /plugins/demo/blank; click #go; wait 1 s;
           assert len(context.pages) == 1; assert the response's CSP equals plugin_pages.CSP.
         - TestNewTabs.test_blank_link_opens_an_unconnected_tab_with_page_new_tabs: host.new_tabs = {"demo"};
           /plugins/demo/blank; assert the CSP equals plugin_pages.CSP_NEW_TABS; `with context.expect_page(timeout=5000)
           as info: page.locator("#go").click()`; on info.value wait for "#jira"; json.loads(its title) ==
           {"opener": True, "referrer": ""}; then the plugin page still shows #go (the opener tab was not navigated).
         - TestNewTabs.test_new_tab_into_privacyfence_carries_no_session: host.new_tabs = {"demo"}; /plugins/demo/own;
           expect_page as above; the new page's response status is 404 and host.calls has no ("demo", "/two").
      Stop with status=blocked, reporting the response headers, if the new tab's opener check is False, or if no
      page opens within 5 s with CSP_NEW_TABS: do not add headers, rewrite HTML, or relax an assertion.
      No CHANGELOG line; no plan item IDs in code, comments or test names.
    acceptance:
      - PRIVACYFENCE_TEST_CHROMIUM set; python3 -m pytest tests/integration/test_plugin_pages_browser.py -v passes with no test SKIPPED
      - python3 -m pytest tests/unit/test_plugin_page_preview.py -q passes
      - python3 scripts/plugin_page_preview.py --help exits 0
      - ruff check . exits 0

  - id: p3-protocol-1-3
    title: Protocol 1.3.0 - file parameter rules, FileParamSpec, ToolFile, schema and regenerated SDK types
    depends_on: [p1-new-tabs]
    complexity: M
    touches:
      - src/privacyfence/plugins/constants.py
      - src/privacyfence/plugins/protocol.py
      - src/privacyfence/plugins/tools.py
      - docs/plugin-protocol/protocol.schema.json
      - plugin-sdk/src/privacyfence_plugin_sdk/types.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - docs/plugin-protocol.md
      - examples/plugins/today/README.md
      - tests/unit/plugins/test_constants.py
      - tests/unit/plugins/test_protocol.py
      - tests/unit/plugins/test_tools.py
      - tests/unit/plugins/test_protocol_doc.py
      - tests/unit/plugins/test_host.py
      - tests/unit/plugin_sdk/test_plugin.py
    brief: |
      Read docs/plugin-files-and-page-links-plan.md Design section 5 first; it is the spec. This is the only phase
      that changes PROTOCOL_VERSION.
      1. constants.py: PROTOCOL_VERSION = "1.3.0"; FILE_PARAM_KEY, MAX_FILE_BYTES, MAX_FILE_PARAMS_PER_TOOL,
         FILE_MEDIA_TYPES and UPLOAD_ID_RE exactly as in the Design. SDK plugin.py: PROTOCOL_VERSION = "1.3.0"
         (nothing else in that file).
      2. protocol.py: FileParamSpec and file_params(defn) as in the Design; both in __all__.
      3. tools.py _check_parameters: rules 1 to 7 of Design section 5, in order, with the exact detail texts (rules
         6 and 7 need defn.read_only and defn.gate, which _check_parameters already receives through defn).
         tool_signature does not change.
      4. protocol.schema.json: x-protocol-version "1.3.0"; x-limits gains MAX_FILE_BYTES and MAX_FILE_PARAMS_PER_TOOL
         (never FILE_MEDIA_TYPES: test_x_limits_match_the_constants compares each x-limits value with the
         constant); $defs FileParamSpec (media_types items enum) and ToolFile (content_base64 optional);
         $defs.Manifest gains "page_new_tabs": {"type": "boolean"} and tests/unit/plugins/test_protocol.py's literal
         manifest key set (test_manifest_properties) gains "page_new_tabs"; the tool.prepare and tool.execute params
         $defs gain optional "files". Then run `python3 scripts/gen_plugin_sdk_types.py` (never edit types.py by hand).
      5. Version strings: change "1.2.0" to "1.3.0" only where it is the protocol version: the initialize
         protocol_version assertions in tests/unit/plugins/test_constants.py, test_protocol.py, test_host.py and
         tests/unit/plugin_sdk/test_plugin.py; in docs/plugin-protocol.md the "This is protocol version" line and
         the "protocol_version" of both initialize examples; examples/plugins/today/README.md's protocol line.
         Leave every manifest or plugin `version: 1.2.0` (a plugin's own version, for example in
         test_manifest.py, test_state.py, test_trust.py and the manifest example) unchanged. In
         docs/plugin-protocol.md also add the two new rows to the limits table (File parameter size: 8 MiB;
         File parameters per tool: 1) so test_protocol_doc.py passes; the prose comes in the last phase.
      6. Tests: test_constants.py asserts the new constants, and that
         len(base64.b64encode(bytes(MAX_FILE_BYTES))) + 65536 < MAX_LINE_BYTES; test_tools.py has one case per rule
         1 to 7 asserting the exact detail, plus a valid file parameter accepted on a review tool and a popup tool;
         test_protocol.py: file_params returns the spec for a file parameter and {} for a tool without one, and
         FileParamSpec/ToolFile join the $defs-to-WIRE_KEYS parametrize list only if that test requires every $def
         (follow what the test does for PageEntry).
      Stop with status=blocked if test_protocol_doc.py or test_gen_plugin_sdk_types.py need a change to their own
      logic (not only expected values) to pass.
      No CHANGELOG line; no plan item IDs in code, comments or test names.
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_constants.py tests/unit/plugins/test_protocol.py tests/unit/plugins/test_tools.py tests/unit/plugins/test_protocol_doc.py tests/unit/plugins/test_host.py tests/unit/plugin_sdk/test_plugin.py tests/unit/test_gen_plugin_sdk_types.py -q passes
      - python3 scripts/gen_plugin_sdk_types.py --check exits 0
      - grep -n 'PROTOCOL_VERSION = "1.3.0"' src/privacyfence/plugins/constants.py plugin-sdk/src/privacyfence_plugin_sdk/plugin.py prints two lines
      - ruff check . exits 0

  - id: p4-daemon-files
    title: The daemon resolves a file parameter, shows it on the card, audits it and sends the bytes to the plugin
    depends_on: [p3-protocol-1-3]
    complexity: M
    touches:
      - src/privacyfence/plugins/files.py
      - src/privacyfence/plugins/connector.py
      - src/privacyfence/local_files.py
      - src/privacyfence/upload_staging.py
      - src/privacyfence/web/mcp_tools.py
      - src/privacyfence/audit_log.py
      - src/privacyfence/plugins/host.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/plugins/test_files.py
      - tests/unit/plugins/test_connector.py
      - tests/unit/test_local_files.py
      - tests/unit/test_upload_staging.py
      - tests/unit/web/test_mcp_tools.py
      - tests/unit/plugins/test_host.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Read docs/plugin-files-and-page-links-plan.md Design section 6 first; it is the spec, including every message.
      Copy connectors/drive.py:1485-1530 and :1623 for how a connector reads an upload and commits it after the gate.
      1. upload_staging.py: UploadStagingStore.declared_path(token, principal_id) -> str | None (peek's lookup, filled
         check and principal check, no expiry change). audit_log.py: add "plugin_file" to the comment that lists the
         decision values (next to plugin_output).
      2. local_files.py: resolved_name(path) as specified; add to __all__.
      3. New plugins/files.py with everything in Design section 6's code block, exactly as specified.
      4. plugins/connector.py: PreparedCall.files; _tool_spec's file parameter description; call() steps 1 to 7;
         _reuse_or_prepare/_prepare/_gate/_execute gain the parameters they need; the plugin_file audit entry
         with every AuditEntry field Design section 6 names (copy _auto_audit's try/except and logger.warning). Keep every existing invariant in the module docstring
         true and add one paragraph to it on file parameters (bytes go to prepare and execute, the card's file block
         is the daemon's, the slot is committed after the gate).
      5. web/mcp_tools.py: the CREATE_UPLOAD_SLOT_TOOL sentence exactly as in the Design.
      6. plugins/host.py: each review tool entry gains "file" when file_params(d) is non-empty.
         settings_window_html.py: the "Takes a file in …" line under that tool, as in the Design.
      7. Tests:
         - test_files.py: declared_media_type for every extension in the map and an unknown one, case-insensitive;
           sniff_media_type for each magic (and the texts "true" and "OTTO" are text/plain), for HTML with a BOM and leading whitespace, for "<!DOCTYPE html>" in
           capitals, for JSON, plain text, empty bytes, a NUL in text, invalid UTF-8; a non-string value; file_reference for
           "upload:abc", a 43-character id, "~/x.html" and "/tmp/x.html"; resolve_file for an upload slot (use
           local_files.call_context and the real UploadStagingStore as test_local_files.py does), for a direct path
           (tmp_path; force_bridge_for_tests stays False), over max_bytes, a refused sniffed type, an empty value,
           and a slot whose filename is "C:\\x\\page.html" (name "page.html"); card_block (six labels; Source is "Upload slot" for a slot and the path as given for a path); param_description.
         - test_connector.py, with the file's existing fake peer: a popup tool with a file parameter sends "files" in tool.prepare
           without content_base64 and in tool.execute with it, and "args" without the parameter; args_digest
           excludes it; the card's first block is the file block (read it as a label to value dict: File, Source, Size, Declared type,
           Detected type, SHA-256); the slot is consumed after approval (a second call
           with the same upload: reference fails with the existing "expired or was already used" message); a denied
           call leaves the slot and writes no plugin_file audit row; an approved call writes exactly one with the
           summary format; a changed local file between two calls gets a new prepare (two tool.prepare requests);
           a tool without a file parameter sends no "files" key (unchanged behaviour); LocalFilesNeeded raised by
           require_local_files propagates out of call() (force_bridge_for_tests(True) and a bridge-available
           call_context).
         - test_local_files.py: resolved_name for upload:, a bridge path and a direct path.
         - test_upload_staging.py: declared_path for the right principal, a wrong principal (None) and an unknown token.
         - test_mcp_tools.py: the slot tool's description contains "a plugin tool's file parameter".
         - test_host.py and test_settings_window_html.py: the review "file" entry and the dialog line.
      Stop with status=blocked if LocalFilesNeeded does not reach routes_mcp from a plugin tool (another exception
      type arrives in the test) or if gated_call cannot take the extra file block through preview_blocks.
      No CHANGELOG line; no plan item IDs in code, comments or test names.
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_files.py tests/unit/plugins/test_connector.py tests/unit/test_local_files.py tests/unit/test_upload_staging.py tests/unit/web/test_mcp_tools.py tests/unit/plugins/test_host.py tests/unit/test_settings_window_html.py -q passes
      - python3 -m pytest tests/unit/plugins/test_files.py -q --cov=privacyfence.plugins.files --cov-branch --cov-fail-under=100 passes
      - grep -n "commit_uploads" src/privacyfence/plugins/connector.py prints one line
      - ruff check . exits 0

  - id: p5-sdk-files
    title: SDK file parameters - file_param, IncomingFile, ctx.files, digest check and the older-daemon filter
    depends_on: [p3-protocol-1-3]
    complexity: M
    touches:
      - plugin-sdk/src/privacyfence_plugin_sdk/_files.py
      - plugin-sdk/src/privacyfence_plugin_sdk/__init__.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - plugin-sdk/README.md
      - tests/unit/plugin_sdk/test_plugin.py
      - tests/unit/plugin_sdk/test_sdk_files.py
    brief: |
      Read docs/plugin-files-and-page-links-plan.md Design sections 5 and 7 first; they are the spec. The SDK must
      not import privacyfence (ADR 0126): copy the rules, do not import them.
      1. New _files.py as specified (constants, declared_media_type, sniff_media_type, IncomingFile, file_param,
         parse_files with "files.<p>: …" details).
      2. __init__.py: export IncomingFile and file_param (and add them to its __all__).
      3. plugin.py: rules 1 to 7 in _validate_parameters/_check_tool with the daemon's exact texts; Context.files;
         _prepare and _execute parse files, the entry's SHA-256 map, digest_mismatch on a difference;
         _offered_definitions() and the minor-version filter in _initialize and tools_changed, with the WARNING text.
      4. README.md: the "File parameters" section as in the Design.
      5. Tests:
         - test_sdk_files.py: file_param's dict; sniff and declared type cases mirroring Design section 6's list;
           parse_files: valid, unknown parameter, bad base64, size mismatch, over max_bytes, refused sniffed type,
           SHA-256 mismatch, None -> {}.
         - test_plugin.py: each of rules 1 to 7 raises ToolDefinitionError with the text; tool.prepare with files
           (no content_base64) gives the tool function ctx.files[p] with name, size and sha256, and its .content raises
           RuntimeError; tool.execute with the same files plus content_base64 runs and the execute
           function reads ctx.files[p].content; execute with a different SHA-256 answers digest_mismatch; a tool without a file
           parameter sees ctx.files == {}; initialize with protocol_version "1.2.0" leaves the file tool out of
           "tools" and logs the WARNING (caplog), "1.3.0" keeps it; tools_changed after a 1.2.0 initialize leaves it
           out too; add a test next to TestLimits comparing the SDK's constants in privacyfence_plugin_sdk._files
           (FILE_PARAM_KEY, MAX_FILE_BYTES, MAX_FILE_PARAMS_PER_TOOL, FILE_MEDIA_TYPES) with privacyfence.plugins.constants'
           (the existing parametrize reads underscore names from plugin.py, so it does not fit).
      No CHANGELOG line; no plan item IDs in code, comments or test names.
    acceptance:
      - python3 -m pytest tests/unit/plugin_sdk -q passes
      - grep -rn "import privacyfence\b\|from privacyfence\b" plugin-sdk/src prints nothing
      - ruff check . exits 0

  - id: p6-testhost-files
    title: PluginTestHost - file parameters in call_tool, the daemon's floors, page_new_tabs, and the sniffing parity test
    depends_on: [p4-daemon-files, p5-sdk-files]
    complexity: M
    touches:
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_pages.py
      - plugin-sdk/README.md
      - tests/unit/plugin_sdk/test_testhost.py
      - tests/unit/plugin_sdk/test_testhost_surfaces.py
      - tests/unit/plugin_sdk/test_sdk_files_parity.py
    brief: |
      Read docs/plugin-files-and-page-links-plan.md Design sections 2 (the SDK test host paragraph), 6 and 8 first.
      1. testing/_pages.py: CSP_NEW_TABS (same string as the daemon's) used for every page response when the host
         has page_new_tabs. testing/_host.py: PluginTestHost(page_new_tabs=False) with the ValueError; the file
         parameter floors in _check_tool_defs; call_tool(..., files=None) exactly as Design section 8 says, using
         _files.py's sniff and declared type and the daemon's messages; the card's file block first: in _host.py's _validate_prepared (where cards are built), prepend one
         {"type": "fields", "items": [...]} entry to Card.preview; the plugin_file audit row.
      2. README.md: in the test host section, call_tool's files= argument with a one-line example, and
         page_new_tabs=.
      3. Tests:
         - test_testhost_surfaces.py: call_tool with files= gives prepare the metadata only and execute the bytes; plain bytes get
           the name "file.html" for a text/html parameter; the card's first block is the file block with the six
           labels (File, Source, Size, Declared type, Detected type, SHA-256); every refusal in Design section 8 returns invalid_params with its exact detail and does not call
           the plugin; a denied card writes no plugin_file row; page_new_tabs=True gives CSP_NEW_TABS on a page and
           False gives the old CSP; page_new_tabs without pages raises ValueError.
         - test_testhost.py: _check_tool_defs refuses each of rules 6 and 7 for a plugin built without the SDK's own
           check (use the file's existing raw-initialize pattern).
         - New test_sdk_files_parity.py: for a list of sample byte strings (one per sniff rule) the SDK's and the
           daemon's sniff_media_type agree, and so do declared_media_type for every extension; the SDK's CSP_NEW_TABS
           equals privacyfence.plugins.pages.CSP_NEW_TABS; the test host's refusal sentences equal the daemon's
           resolve_file messages for the same input (call both).
      No CHANGELOG line; no plan item IDs in code, comments or test names.
    acceptance:
      - python3 -m pytest tests/unit/plugin_sdk -q passes
      - python3 -m pytest tests/unit/plugin_sdk/test_sdk_files_parity.py -q passes
      - grep -rn "import privacyfence\b\|from privacyfence\b" plugin-sdk/src prints nothing
      - ruff check . exits 0

  - id: p7-file-integration
    title: Echo fixture file tool, an end-to-end MCP upload test and the conformance scenario
    depends_on: [p6-testhost-files]
    complexity: M
    touches:
      - tests/fixtures/plugins/echo/echo_plugin.py
      - tests/fixtures/plugins/echo/harness.py
      - tests/integration/test_plugin_files.py
      - tests/integration/test_sdk_testhost_conformance.py
      - tests/integration/test_plugin_framework.py
      - tests/integration/test_plugin_harness.py
    brief: |
      Read docs/plugin-files-and-page-links-plan.md Design sections 6 and 8 first.
      1. echo_plugin.py: a tool "file_put", gate "popup", title "File put", description "Store a file and return its
         size and SHA-256.", params {"file": file_param("The file to store.", max_bytes=65536,
         media_types=["text/plain", "application/json", "text/html"])}, required ["file"], effect "Stores a file in
         the Echo plugin."; the tool function returns Prepared(preview=[blocks.text("file put")]); execute returns
         {"name": f.name, "size": f.size, "sha256": f.sha256} for f = ctx.files["file"].
         Then run `grep -rn "popup_write" tests/integration tests/unit` and update every assertion of the exact echo
         tool list that the new tool breaks, if it is in this phase's touches; one outside them means stop with
         status=blocked.
      2. New tests/integration/test_plugin_files.py, on the harness's Stack and mcp_session (copy the setup of
         tests/integration/test_plugin_outputs.py): call privacyfence_create_upload_slot (filename "note.txt",
         size_bytes 5), PUT b"hello" to upload_url with httpx, call echo_file_put with file="upload:<upload_id>" and a
         reason, approve the card (read it from stack.popups.write[-1][1]["preview_blocks"], where the daemon has turned the file block into field entries; compare label to value pairs); the result is {"name": "note.txt", "size": 5,
         "sha256": sha256(b"hello")}; the captured card lists File note.txt, Source Upload slot, Size 5 bytes, Declared
         type text/plain, Detected type text/plain and the SHA-256; the audit log has one plugin_file row for
         plugin:echo with that summary; a 70,000-byte
         slot fails with the over-the-limit message and no card; a PNG slot fails with the refused-type message; a
         direct local path (tmp_path file, unseparated test daemon) works and its card names the file's basename.
      3. tests/integration/test_sdk_testhost_conformance.py: one scenario running echo file_put with the same bytes on
         the test host (call_tool(files={"file": ("note.txt", b"hello")})) and on the daemon (upload slot as above),
         comparing the result and the card's file block as label to value pairs, leaving out Source (the test host says "Test host").
      No CHANGELOG line; no plan item IDs in code, comments or test names.
    acceptance:
      - python3 -m pytest tests/integration/test_plugin_files.py tests/integration/test_sdk_testhost_conformance.py tests/integration/test_plugin_framework.py tests/integration/test_plugin_harness.py -v passes
      - python3 -m pytest tests/integration -q passes
      - ruff check . exits 0

  - id: p8-docs-adrs-retire
    title: Reference docs, the two ADRs, changelog; retire the plan
    depends_on: [p2-browser-checks, p7-file-integration]
    complexity: M
    touches:
      - docs/plugin-protocol.md
      - docs/plugins.md
      - docs/security-and-compliance.md
      - docs/adr/README.md
      - docs/adr/0122-plugin-tools-are-gated-in-two-steps-and-a-read-releases-the-prepared-payload.md
      - docs/adr/0124-plugin-pages-are-get-only-owner-only-and-sandboxed.md
      - docs/adr/*-a-plugin-page-opens-links-in-new-tabs-only-when-its-manifest-says-so.md
      - docs/adr/*-plugin-tools-take-files-by-reference-and-privacyfence-passes-the-bytes.md
      - CHANGELOG.md
      - docs/plugin-files-and-page-links-plan.md
      - docs/plugin-files-and-page-links-plan-manual-steps.html
    brief: |
      Read the whole plan first. Edit docs by section heading; a named section that is missing means status=blocked.
      1. ADR numbers: n = 1 + the highest four-digit prefix in docs/adr/; the new-tabs ADR takes n and the files ADR
         n+1. Use docs/adr/README.md's template. New-tabs ADR: Status "Accepted — <today>." and "Amends [ADR 0124](…)."
         on its own line; Context = Goal items 1 and 2 and the CSP table from Current state; Decision = Design
         sections 1 and 2; Alternatives = "What was rejected / Links"; Consequences = the five risks of Design
         section 2, the per-plugin review, eval and downloads still blocked; Verification = test_plugin_pages_browser.py's
         new classes, test_routes_plugins.py, test_server.py, and the preview script for the manual checks; Related =
         issue 846, ADR 0124, ADR 0139 (the page browser), ADR 0127. Files ADR: Status and
         "Amends [ADR 0122](…)."; Context = Goal item 3; Decision = Design sections 5 to 8; Alternatives = "What was
         rejected / Files"; Consequences = one file per tool of at most 8 MiB, every file passes a card or a saved
         rule, no bytes held across calls, the plugin never sees a path or token, 1.2 plugins unchanged; Verification
         = the p3 to p7 tests by file; Related = ADR 0007, 0028, 0102, 0121, 0122, 0126.
         ADR 0124 and ADR 0122 each get one Status line in the format of ADR 0121's "Amended by [ADR 0131](…): …".
         docs/adr/README.md: two index rows, and the Status cells of 0122 and 0124 gain the amendment.
      2. docs/plugin-protocol.md: "Version 1.3 adds" paragraph under Versioning (Design section 5's first paragraph);
         the manifest example and bullets gain page_new_tabs; Tool definitions gain the file parameter rules 1 to 7
         and the JSON example; tool.prepare and tool.execute gain "files" with the ToolFile table and the
         args-without-the-file rule; ### Pages gets Design section 3 (all three CSP values verbatim from
         plugins/pages.py, the other headers, Permissions-Policy written out from web/server.py's
         _PERMISSIONS_POLICY, the "what the CSP allows" table, same-tab and new-tab links); Audit entries gain the
         plugin_file row; the limits table rows from p3 stay.
      3. docs/plugins.md: in the pages part, that links open in the same tab, and in a new tab with page_new_tabs
         (and what the owner sees on the review dialog); a "### File parameters" part under writing a plugin
         (file_param, ctx.files, the rules) and, for AI-client users, how a file is handed over (a local path, or
         privacyfence_create_upload_slot + PUT + upload:<id>); the Design section 9 table under a "### Protocol and
         SDK versions" heading; the test-host differences list gains nothing new unless p6 recorded one.
      4. docs/security-and-compliance.md (its plugins section): two sentences: a plugin page opens new tabs only when
         its manifest says so and those tabs carry no PrivacyFence session; a file reaches a plugin only through an
         approval card showing its name, size, types and SHA-256, and the plugin never gets a path.
      5. CHANGELOG.md ## [Unreleased] ### Added: "- Plugin pages can open links in new tabs when the plugin's manifest
         sets page_new_tabs; the Review and enable dialog says so, and other plugin pages keep the full sandbox." and
         "- Plugin tools can take a file (plugin protocol 1.3): the AI passes a local path or an upload slot, and the
         approval card shows the file's name, size, declared and detected type and SHA-256. The plugin SDK adds
         file_param, ctx.files and PluginTestHost.call_tool(files=…); plugins written for 1.0 to 1.2 keep working."
      6. Delete docs/plugin-files-and-page-links-plan.md and docs/plugin-files-and-page-links-plan-manual-steps.html.
    acceptance:
      - grep -rn "plugin-files-and-page-links-plan" docs scripts src plugin-sdk README.md prints nothing
      - python3 -m pytest tests/unit/plugins/test_protocol_doc.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_website_docs_allowlist.py -q passes
      - grep -n "allow-popups-to-escape-sandbox" docs/plugin-protocol.md prints at least one line
      - grep -n "Amended by" docs/adr/0122-*.md docs/adr/0124-*.md prints a line for each file
```
