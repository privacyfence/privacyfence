# pf-pages: a simple HTML page publishing plugin for PrivacyFence

## Goal

A PrivacyFence plugin, `pages`, that lets the AI client (Claude) publish simple HTML pages for its
owner. Through gated tools Claude can store and read one shared stylesheet, store image assets,
publish a page, update it (each update is a new version), read it and delete it.

The pages are browsed in **PrivacyFence's own page browser**, not in a page of the plugin. The
framework plan `plan/plugin-page-browser` in `privacyfence/privacyfence` adds that browser: protocol
1.2's `pages.list`, the SDK's `@plugin.page_index`, and `/plugin-pages`, reached from the top
navigation's Plugins menu. The plugin lists every published page there with its name, version,
created and updated date, and each entry opens that single page in a new tab.

When a page is shown, the plugin inlines into it the one shared stylesheet and the images it
references, as a `<style>` block and `data:` URIs. Every page gets the same stylesheet, and an image
is never a separate request. The plugin-page sandbox (ADR 0124) stays exactly as strict as it is.

The plugin is built only on `privacyfence-plugin-sdk` and lives in its own repository,
`privacyfence/pf-pages`.

## Current state

- `privacyfence/pf-pages` holds only the files GitHub created with it (a README), plus this plan,
  `docs/pf-pages-plan-manual-steps.html` and `.claude/toolkit.yaml` on `plan/pf-pages`.
- The framework, at `privacyfence/privacyfence@567254695aad54efa586c3950eb6d05d43a2f744`
  (`feature/plugin-framework-polish`), defines what a plugin can do:
  - `docs/plugins.md`: installing, reviewing and enabling a plugin; plugins run only on a packaged
    (separated) install, as PrivacyFence's service account.
  - `docs/plugin-protocol.md`: the protocol (version 1.1.0). It covers the manifest, tool
    definitions (parameters must be scalars, `reason` is reserved, at most 64 tools, MCP name
    `<plugin>_<tool>`), `tool.prepare` (the preview is at most 64 KiB and 50 blocks, and the JSON
    of `{"blocks": payload}` at most 100,000 bytes), blocks (`heading`, `fields`, `table`, `text`,
    `code`, `diff`; no image block) and pages (GET only, owner only, sandboxed, an 8 MiB body cap,
    and `text/html` among the allowed content types).
  - `plugin-sdk/`: the `privacyfence-plugin-sdk` package (zero dependencies, Python ≥ 3.11).
    `Plugin`, `Prepared`, `blocks`, `Html`, `Text`, `PROTOCOL_VERSION`, `SourceError`;
    `@plugin.tool(...)`, `@<tool>.execute`, `@plugin.page(path)` (exact path match only, so one
    page route takes its subject from the query), `@plugin.on(event)`, `@plugin.on_purge`;
    `ctx.principal.storage_dir`, `ctx.data_dir`. `privacyfence_plugin_sdk.testing.PluginTestHost`
    runs a plugin in memory: `call_tool(name, args, decide="approve"|"deny")` returns a
    `ToolOutcome` (`card_shown`, `card.preview`, `card.payload`, `card.scopes`, `released`,
    `result`, `error`), and `get(path, query=...)` returns a page response (`status`, `headers`,
    `body`).
  - `examples/plugins/today/today_plugin.py` is the reference plugin: its `build_plugin()` layout,
    its handling of `_home()`, `--self-test` and `on_purge`, and its "preview says why the call
    will not do anything, execute returns the reason" pattern are what this plan copies.
    `scripts/build_example_plugin.py` is the PyInstaller build to copy.
- The SDK is not on PyPI yet. It is installed from git, pinned to **the SDK pin**: the commit of
  `privacyfence/privacyfence` where the framework plan's PR (`feature/plugin-page-browser`, which
  adds `PageEntry` and `@plugin.page_index`) has merged. The planning session writes the real SHA
  into this file at handover (`mb1-ready`), replacing every occurrence of the placeholder, also in
  the documentation links below. A worker whose `pyproject.toml` dependency does not match
  `privacyfence@[0-9a-f]{40}#subdirectory=plugin-sdk` stops with `status=blocked`. The dependency is
  `privacyfence-plugin-sdk @ git+https://github.com/privacyfence/privacyfence@SDK_SHA_TO_FILL#subdirectory=plugin-sdk`.
- At that SHA the SDK also has `PageEntry(path, title, version=None, created_at=None,
  updated_at=None, description=None)` and `@plugin.page_index` (`async def index(ctx) ->
  list[PageEntry]`), and `PluginTestHost.list_pages()` returns the entries as dicts after the
  daemon's checks. `PROTOCOL_VERSION` is `"1.2.0"`.

## Design

### Names

| Thing | Value |
|---|---|
| Repository | `privacyfence/pf-pages` |
| Plugin `name` (manifest, `Plugin(name=…)`, plugins-directory folder, URL) | `pages` |
| `display_name` | `Pages` |
| Python distribution / import package | `pf-pages` / `pf_pages` |
| Executable (`command`) | `pages-plugin` (`pages-plugin.exe` on Windows) |
| First version | `0.1.0`, in `src/pf_pages/__init__.py` as `__version__`, equal to the manifest's `version` |
| Page browser | PrivacyFence's `/plugin-pages/pages` (top navigation → Plugins → Pages), fed by the plugin's `@plugin.page_index` |
| One page | `/plugins/pages/view?p=<name>` (current version) or `…&v=<n>` |
| License | Apache-2.0, the SDK's |

`pages` is not a reserved plugin name (`docs/plugin-protocol.md#manifest`), and the MCP tool names
are `pages_<tool>`.

### Manifest (`privacyfence-plugin.yaml`, at the repo root and copied next to the executable)

```yaml
name: pages
display_name: Pages
version: 0.1.0
protocol: "1"
command: ["pages-plugin"]
tools: dynamic
pages: true
```

No `source_operations`, no `outputs` and no `max_gate_floor` (the default, `review`): every tool is
gated.

### Storage

Everything is per principal, under `ctx.principal.storage_dir / "store"` (call it `root`). The
plugin uses nothing in `ctx.data_dir`.

```
root/site.css                     the stylesheet (UTF-8)
root/site.json                    {"size": n, "sha256": hex, "updated_at": ts}
root/pages/<name>/page.json       {"name", "title", "created_at", "updated_at", "current": n,
                                   "versions": [{"version", "created_at", "size", "sha256"}, …]}
root/pages/<name>/v<n>.html       version n's HTML (UTF-8), n = 1, 2, …
root/assets/<asset name>          the asset's bytes
root/assets.json                  {"<asset name>": {"mime_type", "size", "sha256", "updated_at"}}
```

- Timestamps are UTC, `YYYY-MM-DDTHH:MM:SSZ`, from an injectable clock (`now: Callable[[],
  datetime]`, default `datetime.now(timezone.utc)`).
- `sha256` is the lowercase hex SHA-256 of the stored bytes.
- Every write goes through `_atomic_write(path, data: bytes)`: write `.<filename>.tmp` in the same
  folder, flush and `os.fsync`, then `os.replace`. A metadata file is written after the content file
  it describes.
- Every version is kept until the page is deleted. Deleting a page removes its folder.
- The store's methods are synchronous and never awaited half-way, so two tool executions on the
  plugin's event loop cannot interleave inside one. There is no lock.

### Limits and validation (`src/pf_pages/store.py`)

```python
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")                                       # fullmatch; a page's name
ASSET_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,57}\.(png|jpg|jpeg)")  # fullmatch; at most 63 chars
WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul",
                              *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})
TITLE_FORBIDDEN_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
ASSET_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}
MAX_TITLE_CHARS = 120
MAX_HTML_BYTES = 1_048_576      # UTF-8, after line-ending normalization
MAX_CSS_BYTES = 262_144         # UTF-8, after line-ending normalization
MAX_ASSET_BYTES = 1_048_576     # decoded
MAX_PAGES = 500
MAX_ASSETS = 200
```

- A page name in `WINDOWS_RESERVED` is `bad_name`, and so is an asset name whose part before the
  extension is in it (`con.png`). This holds on every OS, so a store copies between machines.
- A title is valid when it has 1 to `MAX_TITLE_CHARS` characters, at least one of them not a space,
  and no match of `TITLE_FORBIDDEN_RE` (C0 and C1 controls, line and paragraph separators, and
  bidirectional controls). That is exactly what the SDK's `blocks.clean_line` strips, so a stored
  title always passes the page index's `clean_line(title) == title` rule.
- `check_html` and `check_css` normalize line endings before checking the size: `\r\n` and a lone
  `\r` become `\n`. They return the normalized text, encoded, and that is what is stored. Reads
  return text exactly as stored, except that the SDK's blocks strip control characters other than
  newline and tab, and bidirectional controls. The README says so.

An asset's bytes must match its extension: PNG starts `\x89PNG\r\n\x1a\n`, and JPEG (`.jpg`, `.jpeg`)
starts `\xff\xd8\xff`. Only these two formats are supported: both are plain raster data that
carry no script, no links and no references to other files.

### Images, securely

The plugin-page sandbox stays as strict as it is (ADR 0124). A sandboxed page's own requests carry
no cookie, so an image can never be a separate URL. The design keeps every image inside the one page
response:

1. **In through a gated write only.** An image enters the store only through `pages_asset_put`, a
   popup card that shows its name, type, size and SHA-256. The plugin reads nothing from the user's
   disk or the network.
2. **Checked on the way in.**
   - The name must match `ASSET_NAME_RE`, so it can never be a path.
   - Only PNG and JPEG are accepted, and the bytes must match the extension (the magic numbers
     above), with a 1 MiB cap. Neither format can carry script or links, so there is no markup to
     check or sanitize.
3. **Out only inlined.** When `/view` is served, `inline` replaces each `assets/<name>` reference
   with a `data:<mime>;base64,…` URI of the stored bytes. The MIME type comes from the extension,
   never from the content or from the AI.
   - The browser makes no extra request.
   - The page's CSP (`default-src 'self' data:`) allows exactly those `data:` images.
   - There is no URL from which an image could be fetched on its own.
4. **Rendered in the sandbox.** Even an image that slipped through runs inside the page's opaque
   origin, with no cookie, no same-origin access and no popups.

`StoreError(ValueError)` carries one of these messages exactly (`{…}` filled in):

| Code (attribute `code`) | Message |
|---|---|
| `bad_name` | `A page name is 1 to 63 characters: lowercase letters, digits and hyphens, starting with a letter or digit, and not a reserved device name such as con or nul.` |
| `bad_title` | `A title is 1 to 120 characters on one line, with no control characters.` |
| `empty_html` | `The HTML is empty.` |
| `html_too_large` | `The HTML is larger than 1 MiB (1,048,576 bytes).` |
| `css_too_large` | `The stylesheet is larger than 256 KiB (262,144 bytes).` |
| `page_exists` | `A page named '{name}' already exists. Use pages_update to publish a new version.` |
| `no_page` | `There is no page named '{name}'.` |
| `no_version` | `Page '{name}' has no version {n}; its versions are 1 to {current}.` |
| `page_changed` | `Page '{name}' changed since this call was prepared (it is now at version {current}). Call the tool again.` |
| `too_many_pages` | `There are already 500 pages. Delete one first.` |
| `bad_asset_name` | `An asset name is up to 63 lowercase letters, digits, hyphens and underscores, ending in .png, .jpg or .jpeg, and not a reserved device name such as con.png.` |
| `bad_base64` | `The content is not valid base64.` |
| `asset_too_large` | `The asset is larger than 1 MiB (1,048,576 bytes).` |
| `asset_mismatch` | `The content is not a {mime_type} image.` |
| `no_asset` | `There is no asset named '{name}'.` |
| `too_many_assets` | `There are already 200 assets. Delete one first.` |
| `bad_offset` | `offset must be between 0 and {length}.` |

### Store API (`src/pf_pages/store.py`)

```python
@dataclass(frozen=True)
class VersionInfo:
    version: int; created_at: str; size: int; sha256: str

@dataclass(frozen=True)
class PageInfo:
    name: str; title: str; created_at: str; updated_at: str; current: int
    versions: tuple[VersionInfo, ...]

@dataclass(frozen=True)
class AssetInfo:
    name: str; mime_type: str; size: int; sha256: str; updated_at: str

@dataclass(frozen=True)
class StylesheetInfo:
    size: int; sha256: str; updated_at: str

class Store:
    def __init__(self, root: Path, now: Callable[[], datetime] = utc_now) -> None
    # validation, usable before any write
    def check_name(self, name: str) -> None
    def check_title(self, title: str) -> None
    def check_html(self, html: str) -> bytes        # normalized line endings, then empty / size checks
    def check_css(self, css: str) -> bytes
    def check_offset(self, value: object, length: int) -> int   # None -> 0; int 0..length -> it; else bad_offset
    def decode_asset(self, name: str, content: str) -> bytes   # name, base64, size, magic
    # pages
    def list_pages(self) -> list[PageInfo]            # updated_at descending, then name
    def get_page(self, name: str) -> PageInfo | None
    def read_html(self, name: str, version: int | None = None) -> tuple[PageInfo, int, str]
    def publish(self, name: str, title: str, html: str) -> PageInfo
    def update(self, name: str, html: str, title: str | None, expected_current: int) -> PageInfo
    def delete(self, name: str, expected_current: int) -> int          # versions deleted
    def pages_referencing(self, asset_name: str) -> list[str]         # names whose current HTML references the asset
                                                                      # (store.ASSET_REF_RE, the regex inline also uses);
                                                                      # plus "stylesheet" last when site.css contains it
    # stylesheet
    def read_css(self) -> tuple[StylesheetInfo, str] | None
    def write_css(self, css: str) -> StylesheetInfo
    # assets
    def list_assets(self) -> list[AssetInfo]          # by name
    def get_asset(self, name: str) -> tuple[AssetInfo, bytes] | None
    def put_asset(self, name: str, data: bytes) -> AssetInfo
    def delete_asset(self, name: str) -> AssetInfo
    # everything
    def purge(self) -> None                            # removes root and everything under it
```

`get_asset` and `read_html` never follow a symbolic link. They open with `os.open(…, O_RDONLY |
O_NOFOLLOW)` where the OS has `O_NOFOLLOW`, and otherwise check `Path.is_symlink()` first, and treat
a link as missing. A name is always validated by `NAME_RE` or `ASSET_NAME_RE` before it is joined
to a path, so no request reaches a path outside `root`.

### Fitting text into a card (`src/pf_pages/fit.py`)

```python
PAYLOAD_BUDGET = 95_000      # bytes; the daemon's limit for {"blocks": payload} is 100,000
PREVIEW_BUDGET = 60_000      # bytes; the daemon's preview limit is 64 KiB
READ_CHUNK_CHARS = 40_000
PREVIEW_CHARS = 24_000

def json_size(blocks: list[dict]) -> int
    # max(len(json.dumps({"blocks": blocks}).encode()),
    #     len(json.dumps({"blocks": blocks}, ensure_ascii=False).encode()))
def fit(text: str, start: int, max_chars: int, budget: int,
        make_blocks: Callable[[str], list[dict]]) -> int
    # the largest end, start < end <= min(len(text), start + max_chars), such that
    # json_size(make_blocks(text[start:end])) <= budget; found by halving (end - start) until it
    # fits, then growing by bisection. Returns start when even one character does not fit.
```

`make_blocks` always builds the **whole** block list that will be sent, not only the code block:

- For a read, it is the payload: the fields block and the chunk's code block.
- For a write, it is the whole preview: the fields block, headings, the code or diff block holding
  the cut text, and the truncation `text` block.

A read returns `text[offset:end]` with `next_offset = end` while `end < len(text)`, else none.

A write card shows `text[0:end]` with `end = fit(text, 0, PREVIEW_CHARS, PREVIEW_BUDGET, …)`. When
that is not the whole text, it adds a `text` block: `Showing the first {end:,} of {len:,}
characters. The SHA-256 above is of the whole file.`

### Tools (`src/pf_pages/tools_pages.py`, `src/pf_pages/tools_assets.py`)

`src/pf_pages/plugin.py` keeps `build_plugin(now=utc_now)`. It declares the two scope types,
defines `store_for(ctx) -> Store` (`Store(ctx.principal.storage_dir / "store", now)`, recording the
storage dir in the `used_dirs` set), and calls `register_page_tools(plugin, store_for)` (from
`tools_pages.py`: `publish`, `update`, `delete`, `read`, `list`),
`register_asset_tools(plugin, store_for)` (from `tools_assets.py`: `css_write`, `css_read`,
`asset_put`, `asset_list`, `asset_delete`), the purge handler, and `register_pages(plugin,
store_for)` (from `views.py`, see Pages). Each `register_*` function is added by the
phase that writes its module.

Two scope types: `page` (`The page a call reads or changes, by its name`) and `asset` (`The asset
a call changes, by its name`). A scope value is the call's `name` argument cut to 200 characters,
or `-` when it is empty.

| Tool | Gate | read_only | destructive | scopes | Parameters (required in **bold**) | Title |
|---|---|---|---|---|---|---|
| `css_write` | popup | no | no | — | **`css`** string | Store the stylesheet |
| `css_read` | review | yes | no | — | `offset` integer | Read the stylesheet |
| `publish` | popup | no | no | `page` | **`name`**, **`title`**, **`html`** strings | Publish a page |
| `update` | popup | no | no | `page` | **`name`**, **`html`**, `title` strings | Publish a new version |
| `delete` | popup | no | yes | `page` | **`name`** string | Delete a page |
| `read` | review | yes | no | `page` | **`name`** string, `version` integer, `offset` integer | Read a page |
| `list` | review | yes | no | — | `offset` integer | List pages |
| `asset_put` | popup | no | no | `asset` | **`name`**, **`content`** strings | Store an image |
| `asset_list` | review | yes | no | — | — | List assets |
| `asset_delete` | popup | no | yes | `asset` | **`name`** string | Delete an asset |

Descriptions and `effect` lines, verbatim:

- `css_write`: "Store the one stylesheet every page uses, replacing the stored one. It is inlined into
  each page's `<head>` before the page's own styles, so a page can override it. The previous
  stylesheet is not kept. At most 256 KiB."
  Parameter `css`: "The whole stylesheet as CSS text. Reference an asset as url(assets/<asset
  name>)." Effect: "Replaces the stylesheet of every published page."
- `css_read`: "Return the stored stylesheet, about 40,000 characters per call. When the result shows
  a next offset, call again with that offset." `offset`: "Character offset to start at; default 0."
- `publish`: "Publish a new HTML page as version 1. The page appears in the owner's page browser in
  PrivacyFence (Plugins → Pages) and opens at /plugins/pages/view?p=<name>. Reference an image as
  src=\"assets/<asset name>\" (store it first with pages_asset_put); the stylesheet and images are
  inlined when the page is shown. A page runs sandboxed: scripts work, but it cannot load anything
  from the network. To change an existing page use pages_update." `name`: "Lowercase letters,
  digits and hyphens, 1 to 63 characters; it is the page's address and cannot change." `title`:
  "The page's name in the page browser, 1 to 120 characters." `html`: "The whole HTML document, at
  most 1 MiB." Effect: "Publishes a new page."
- `update`: "Publish a new version of an existing page. The previous versions are kept and can be
  read with pages_read and its version parameter. The page browser shows the newest version."
  `name`: "The page's name." `html`: "The whole new HTML document, at most 1 MiB." `title`: "A new
  title; leave out to keep the current one." Effect: "Publishes a new version of a page."
- `delete`: "Delete a page and every one of its versions. This cannot be undone." `name`: "The
  page's name." Effect: "Deletes a page and all its versions."
- `read`: "Return a page's HTML (the newest version, or the version asked for), about 40,000
  characters per call, with its title, version and dates. When the result shows a next offset, call
  again with that offset." `name`: "The page's name." `version`: "Version number; default the
  newest." `offset`: "Character offset to start at; default 0."
- `list`: "List the published pages with their title, newest version, created and updated dates and
  size, 100 per call, most recently updated first." `offset`: "Number of pages to skip; default 0."
- `asset_put`: "Store a PNG or JPEG image the pages can use as assets/<name>, replacing one with the
  same name; the replaced image is not kept. Send the file's bytes as base64. At most 1 MiB." `name`:
  "File name ending in .png, .jpg or .jpeg: lowercase letters, digits, hyphens and underscores."
  `content`: "The file's bytes as base64." Effect: "Stores an image for the published pages."
- `asset_list`: "List the stored assets with their type, size and date."
- `asset_delete`: "Delete an asset. Pages that reference it show a broken image." `name`: "The
  asset's file name." Effect: "Deletes an image the published pages may use."

`version` and `offset` are declared `{"type": "integer", …}`.

- `offset` goes through `store.check_offset(value, length)`, where `length` is the text's length
  (or, for `list`, the page count).
- A `version` that is missing is the newest. One that is not an integer of at least 1, or is past the
  newest, raises `no_version`.

**When a call cannot succeed** (any `StoreError` raised while preparing), copy `today`'s pattern:

- a write prepares `Prepared(preview=[blocks.heading("This call will not change anything"),
  blocks.text(message)], state={"error": message}, scopes=…)`, and its execute returns
  `{"ok": False, "error": message}`;
- a read prepares `Prepared(preview=[blocks.text("Report why this read cannot be done")],
  payload=[blocks.text(message)], scopes=…)`.

**Previews and payloads** (block helpers from `privacyfence_plugin_sdk.blocks`; sizes as
`"{n:,} bytes"`; dates as stored):

- `css_write`: `fields` {Stylesheet: `site.css`, Size, SHA-256, Replaces: `nothing (first
  stylesheet)` or `"{old:,} bytes"`}; then, if a stylesheet exists, `heading("Changes")` and
  `diff(unified diff old→new, fromfile "stored", tofile "new", n=3, lineterm="")` (or
  `text("The stylesheet is unchanged.")` when equal); else `heading("CSS")` and `code(head,
  "css")`; plus the truncation `text` when cut. State: the CSS. Execute: `store.write_css(css)` →
  `{"ok": True, "stylesheet": "site.css", "bytes": n, "sha256": hex}`.
- `css_read`: preview `[text("Release the stored stylesheet.")]`; payload `[fields {Stylesheet:
  site.css, Size, Updated, Characters, Offset, Next offset: n or "none"}, code(chunk, "css")]`, or
  `[text("No stylesheet is stored.")]`.
- `publish`: `fields` {Page, Title, Size, SHA-256}, `heading("HTML")`, `code(head, "html")`, plus the
  truncation `text`. Prepare fails early with `page_exists` and `too_many_pages`. State: name, title,
  HTML. Execute: `store.publish(…)` (which re-checks existence and raises `page_exists`) →
  `{"ok": True, "name", "title", "version": 1, "url": "/plugins/pages/view?p=<name>"}`.
- `update`: the card starts with `fields` {Page, Title (the new one, or `"<current> (unchanged)"`),
  Version (`"{n} → {n+1}"`), Size, SHA-256}. What follows depends on the change:
  - **Identical HTML**: `text("The HTML is the same as version {n}.")`.
  - **Too big to diff**: when the old plus the new HTML is over 400,000 characters, or any line of
    either is over 2,000 characters (minified HTML), the card shows `heading("New HTML")`,
    `code(head, "html")` and `text("The change is too large to show as a diff; this is the start of
    the new version.")`. This rule keeps `difflib` fast and keeps the new HTML visible.
  - **Otherwise**: `heading("Changes")` and `diff(unified diff of version n → new, fromfile f"v{n}",
    tofile f"v{n+1}", n=3, lineterm="")`, cut by `fit` to `PREVIEW_CHARS`/`PREVIEW_BUDGET`.
  - The truncation `text` is added whenever something is cut. State: name, HTML, title, `expected_current = n`.
  Execute: `store.update(…)` → `{"ok": True, "name", "version": n+1, "previous_version": n, "url"}`;
  `page_changed` and `no_page` → `{"ok": False, "error": message}`.
- `delete`: `fields` {Page, Title, Versions, Created, Updated}, `text("Deletes every version of this
  page. This cannot be undone.")`. State: name, `expected_current`. Execute → `{"ok": True, "name",
  "versions_deleted": k}`.
- `read`: preview `[text(f"Release the HTML of page '{name}', version {v}.")]`; payload `[fields
  {Page, Title, Version, Versions: count, Created: the page's, Updated: the page's, Characters, Offset,
  Next offset}, code(chunk, "html")]`, the chunk cut by `fit(html, offset, READ_CHUNK_CHARS,
  PAYLOAD_BUDGET, …)` with the fields block included in `make_blocks`.
- `list`: preview `[text("Release the list of published pages.")]`; payload `[fields {Pages: total,
  Showing: "{a}–{b}", Next offset: n or "none"}, table(name "Name", title "Title", version
  "Version", created "Created", updated "Updated", size "Size (bytes)")]`, 100 rows from
  `offset`; or `[text("No pages are published.")]`.
- `asset_put`: `fields` {Asset, Type, Size, SHA-256, Replaces: `no` or `"yes ({old:,} bytes)"`,
  Reference: `assets/<name>`}. Prepare
  fails early with every `decode_asset` error and with `too_many_assets` (only for a new name). State:
  name, bytes. Execute → `{"ok": True, "name", "mime_type", "bytes": n, "sha256", "reference":
  "assets/<name>"}`.
- `asset_list`: preview `[text("Release the list of stored assets.")]`; payload `[table(name "Name",
  mime_type "Type", size "Size (bytes)", updated "Updated")]` or `[text("No assets are stored.")]`.
- `asset_delete`: `fields` {Asset, Type, Size, Used by: `", ".join(store.pages_referencing(name))` or
  `nothing`}. Execute → `{"ok": True, "name"}`; `no_asset` → `{"ok": False, "error": message}`.

**Purge** (`@plugin.on_purge(ctx, scope, principal)`, in `plugin.py`, written in p3a):

- Scope `principal`: remove only `ctx.principal.storage_dir / "store"`.
- Scope `all`: remove `<dir> / "store"` for every storage dir in `used_dirs` and for
  `ctx.principal.storage_dir`.
- Scope `install`: nothing to remove.

### Pages (`src/pf_pages/render.py`, pure; `src/pf_pages/views.py`, `register_pages(plugin, store_for)`)

`views.py` registers the page index and one page route. The plugin serves no `/` page and no
browser of its own.

**`@plugin.page_index`**: one `PageEntry` per page, in `store.list_pages()` order (most recently
updated first):

```python
PageEntry(path=f"/view?p={info.name}", title=info.title, version=str(info.current),
          created_at=info.created_at, updated_at=info.updated_at,
          description=f"{len(info.versions)} version" + ("" if len(info.versions) == 1 else "s"))
```

The plugin returns at most 500 entries (`MAX_PAGES` is 500, so every page fits). A page's name is
`[a-z0-9-]`, so `path` needs no encoding. PrivacyFence's browser opens each entry at
`/plugins/pages/view?p=<name>` in a new tab.

**`@plugin.page("/view")`**: query `p` (the page's name) and optional `v` (its version). The answers:

| Request | Answer |
|---|---|
| `p` missing or empty | `Html(message_html("Page not found", "No page was named in the address."), status=404)` |
| `p` invalid or unknown | `Html(message_html("Page not found", "There is no page named '<p>'."), status=404)` |
| `v` present but not a decimal integer ≥ 1, or past the newest | 404 with `message_html("Page not found", <the no_version message>)`; for a non-integer `v`, `{n}` is the raw `v` |
| over the size cap | `Html(message_html("Page too large", "This page is larger than 8 MiB with its stylesheet and images inlined, so PrivacyFence cannot show it. Use smaller images."), status=500)` when `inline` raises `RenderTooLarge` |
| otherwise | `Html(inline(html, css, lookup))` |

`inline(html: str, css: str | None, lookup: Callable[[str], tuple[str, bytes] | None]) -> str`:

1. Replace every match of `ASSET_REF_RE` in the CSS, and then in the HTML, with
   `lead + "data:" + mime + ";base64," + b64`. An unknown asset is left as it is.
   - Each asset's data URI is built once and cached in a dict for the call.
   - The replacement keeps a running total of the output's UTF-8 size. As soon as the total passes
     `render.MAX_RENDERED_BYTES` (8,388,608), it raises `RenderTooLarge`. It never builds a string
     larger than the cap plus one data URI.
   - The check reads `render.MAX_RENDERED_BYTES` at call time, so a test can monkeypatch
     `pf_pages.render.MAX_RENDERED_BYTES`.
2. If there is CSS, build `<style id="pf-pages-site-css">` + the CSS with every `</` replaced by
   `<\/` + `</style>`.
   - Insert it right after the first match of `re.compile(r"<head\b[^>]*>", re.I)`. With no
     `<head>`, put it at the very start of the document.
   - Check the total size again after inserting it.

```python
# in store.py (so pages_referencing and inline agree); render.py imports it from there
ASSET_REF_RE = re.compile(
    r"""(?P<lead>["'(]\s*)assets/(?P<name>[a-z0-9][a-z0-9_-]{0,57}\.(?:png|jpg|jpeg))(?=\s*["')])"""
)
# in render.py
class RenderTooLarge(Exception): ...
```

`message_html(title: str, text: str) -> str` is a minimal self-contained document with `<h1>` and
`<p>`.

### Repository layout

```
pyproject.toml                      setuptools; [project.scripts] pages-plugin = "pf_pages.__main__:main"
privacyfence-plugin.yaml
src/pf_pages/__init__.py            __version__ = "0.1.0"; NAME = "pages"; PLUGIN_TITLE = "Pages"
src/pf_pages/__main__.py            main(argv): --self-test prints "pages ok protocol <PROTOCOL_VERSION>"; else plugin.run()
src/pf_pages/store.py
src/pf_pages/fit.py
src/pf_pages/tools_pages.py         register_page_tools(plugin, store_for)
src/pf_pages/tools_assets.py        register_asset_tools(plugin, store_for)
src/pf_pages/render.py              inline, message_html (no SDK import)
src/pf_pages/views.py               register_pages(plugin, store_for): page_index and /view
src/pf_pages/plugin.py              build_plugin(now=utc_now) -> Plugin; plugin = build_plugin()
scripts/pages_plugin_entry.py       PyInstaller entry: from pf_pages.__main__ import main; main()
scripts/build.py                    python scripts/build.py --out dist  ->  dist/pages/{pages-plugin[.exe], privacyfence-plugin.yaml}
tests/conftest.py                   pytest_plugins = ["privacyfence_plugin_sdk.testing.pytest"]
tests/test_scaffold.py, test_store.py, test_fit.py, test_tools_pages.py, test_tools_assets.py, test_render.py, test_pages.py
.github/workflows/ci.yml
README.md, CHANGELOG.md, LICENSE, CLAUDE.md, .gitignore, .claude/toolkit.yaml
```

`pyproject.toml`: `requires-python = ">=3.11"`; dependency
`privacyfence-plugin-sdk @ git+https://github.com/privacyfence/privacyfence@SDK_SHA_TO_FILL#subdirectory=plugin-sdk`;
extra `dev = ["pytest>=8", "pytest-asyncio>=0.24", "ruff>=0.6", "pyinstaller>=6.0", "pyyaml>=6"]`;
`[tool.pytest.ini_options] asyncio_mode = "auto"`, `testpaths = ["tests"]`; `[tool.ruff] line-length
= 120`, `target-version = "py311"`, `[tool.ruff.lint] select = ["E", "F", "W", "I", "B", "UP"]`.

### What was rejected

- **A Finder or File Explorer folder for assets.** A plugin's folders are private to the service
  account, the daemon has no desktop session, and ADR 0007 keeps the daemon off the user's disk.
  A shared folder would need ACL work on three OSes, a companion-app request and a new ADR in
  PrivacyFence. The owner chose assets that Claude uploads through a gated tool instead.
- **Serving assets and CSS as separate URLs.** A sandboxed page's subresource requests carry no
  cookie and get the owner-only 404 (ADR 0124), so the plugin inlines them as `data:` URIs and a
  `<style>` block when it serves the page.
- **Auto gates for writes.** The owner chose a popup card for every write and the review gate for
  every read, so the manifest keeps the default `max_gate_floor: review`.
- **Several stylesheets.** One shared `site.css` is what was asked for. A page that needs its own
  styles puts them in its own `<style>`, which comes after `site.css` and overrides it.
- **A version number the AI chooses.** Versions are 1, 2, 3, … assigned by the store. An update
  binds to the version it was prepared against (`expected_current`), so two overlapping updates
  cannot silently overwrite each other.
- **SVG, GIF and WebP images.** SVG is markup that can carry script, links and references to other
  files, so accepting it safely needs a sanitizer. GIF and WebP add nothing a page needs that PNG and
  JPEG lack. Only PNG and JPEG are accepted, so there is nothing to sanitize.
- **A page browser served by the plugin.** Links from a sandboxed plugin page carry no session
  cookie (ADR 0124), and making them work would loosen the sandbox for every plugin. PrivacyFence
  serves the browser instead, from the plugin's page index.
- **One page route per page (`/p/<name>`).** The SDK matches page paths exactly, so the page comes
  from the query (`/view?p=<name>`).

## ADRs

None in this repository: it keeps no ADR directory. The decisions above are recorded in README.md's
"Design" section by the retirement phase. The framework change has its own ADR in
`privacyfence/privacyfence`, written by its own session.

## Manual steps

Step by step, with links: [the manual steps page](https://claude.ai/artifact/S3iKvi1HvwAdMEDmwsq2xz)
(`docs/pf-pages-plan-manual-steps.html`).

- **Before** (`mb1-ready`):
  - The framework plan's PR (`feature/plugin-page-browser`) has merged into its base.
  - `privacyfence/pf-pages` exists with a README, so `main` exists, and the Claude GitHub app has
    access to it.
  - The planning session has written the merge SHA in place of the SDK pin placeholder and pushed
    this plan branch there.
- **After** (`ma1-smoke-test`): on a packaged PrivacyFence built from the branch that holds the
  framework change, install the built `pages` plugin, enable it, and run the README's smoke test:
  - store CSS, a PNG and a JPEG;
  - publish and update a page;
  - open it from the Plugins menu's page browser in a new tab;
  - read it, then delete it.

## Risks and open questions

- **The SDK pin.** If `pip install -e ".[dev]"` cannot resolve the git dependency (network policy,
  a moved SHA), p1 stops with `status=blocked` and reports the pip error. It does not switch to
  another source or vendor the SDK.
- **SDK API drift.** The briefs name `Plugin`, `Prepared`, `blocks.*`, `Html`, `PluginTestHost.call_tool`,
  `host.get`, `ToolOutcome.card/released/result/error` as they are at the pinned SHA. If an import or
  attribute in a brief does not exist there, the worker stops with `status=blocked` and quotes the
  error. It does not guess a replacement.
- **Card size.** A 1 MiB page cannot be shown whole on a card. The card shows the first 24,000
  characters, or a cut diff, and the SHA-256 of the whole file. That is a deliberate trade-off of the
  popup gate, and it is said in the README.
- **Base64 through the AI.** Every asset byte passes through the AI's output as base64 (about 1.37
  characters per byte), so large images are slow and token-expensive. The 1 MiB cap keeps the worst
  case bounded. The README says to prefer small, compressed PNG and JPEG images.
- **The framework must land first.** `@plugin.page_index` and `PageEntry` exist only from the
  framework plan's merge commit on, which is why the pin is filled at handover. On an install
  without that change there is no page browser: the Plugins menu and the Settings link go to
  `/plugins/pages/`, which this plugin does not serve (404). The README says which PrivacyFence the
  plugin needs.
- **`text/html` inlining is string-level.** `inline` rewrites only `"assets/…"`, `'assets/…'` and
  `(assets/…)` references. A page that builds an asset URL in script does not get it inlined. That
  is documented, not handled.

## Implementation manifest

```yaml
plan_slug: pf-pages
feature_branch: feature/pages-plugin
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/S3iKvi1HvwAdMEDmwsq2xz
manual_steps_source: docs/pf-pages-plan-manual-steps.html
manual_before:
  - id: mb1-ready
    title: Framework page-browser PR merged; privacyfence/pf-pages created with Claude's access; plan pushed there with the SDK pin filled
    why: Every phase runs in a session cloned from privacyfence/pf-pages; p1 installs the SDK at the pinned commit, and p4 needs @plugin.page_index, which exists only after the framework PR merges.
    done_when: "The orchestrator checks: `git show FETCH_HEAD:docs/pf-pages-plan.md | grep -cE 'privacyfence@[0-9a-f]{40}#subdirectory=plugin-sdk'` prints 2 or more, and the same grep for the placeholder word (SDK_SHA_ followed by TO_FILL) prints 0."
manual_after:
  - id: ma1-smoke-test
    title: Run the README smoke test on a packaged PrivacyFence install
    why: Only a real install shows the review cards, PrivacyFence's page browser and the sandboxed page with its inlined stylesheet and images opening in a new tab; the test host models no browser.
verify_after_merge:
  - python -m ruff check .
  - python -m ruff format --check .
  - python -m pytest -q
final_checks:
  - docs/pf-pages-plan.md and docs/pf-pages-plan-manual-steps.html are deleted and nothing links to them (grep -rn "pf-pages-plan" . --exclude-dir=.git --exclude-dir=dist --exclude-dir=build prints nothing)
  - .claude/toolkit.yaml's docs.must_read no longer lists docs/pf-pages-plan.md
  - CHANGELOG.md has an [Unreleased] section with entries and no version heading
  - privacyfence-plugin.yaml's version equals src/pf_pages/__init__.py's __version__ (tests/test_scaffold.py)
phases:
  - id: p1-scaffold
    title: Package skeleton, manifest, self-test, CI and contributor files
    depends_on: []
    complexity: S
    touches:
      - pyproject.toml
      - privacyfence-plugin.yaml
      - src/pf_pages/__init__.py
      - src/pf_pages/__main__.py
      - src/pf_pages/plugin.py
      - tests/conftest.py
      - tests/test_scaffold.py
      - .github/workflows/ci.yml
      - README.md
      - CHANGELOG.md
      - LICENSE
      - CLAUDE.md
      - .gitignore
    brief: |
      Read docs/pf-pages-plan.md ("Names", "Manifest", "Repository layout") first.
      1. pyproject.toml exactly as "Repository layout" says (setuptools>=77 build backend, project name
         pf-pages, version read from pf_pages.__version__ via [tool.setuptools.dynamic], the git
         dependency on the SDK, the dev extra, pytest and ruff settings, package discovery under src/).
      2. privacyfence-plugin.yaml exactly as in "Manifest".
      3. src/pf_pages/__init__.py: __version__ = "0.1.0", NAME = "pages", PLUGIN_TITLE = "Pages".
      4. src/pf_pages/plugin.py: build_plugin() -> Plugin returning Plugin(name=NAME, version=__version__)
         with the two scope types from "Tools" (page, asset; descriptions verbatim) and no tools yet;
         module-level plugin = build_plugin().
      5. src/pf_pages/__main__.py: main(argv=None). With "--self-test" print
         f"pages ok protocol {PROTOCOL_VERSION}" (PROTOCOL_VERSION from privacyfence_plugin_sdk) and
         return; otherwise plugin.run(). `if __name__ == "__main__": main()`.
      6. tests/conftest.py: pytest_plugins = ["privacyfence_plugin_sdk.testing.pytest"].
         tests/test_scaffold.py: (a) the manifest (yaml.safe_load) has exactly the seven keys of
         "Manifest" with those values and its version equals __version__; (b) main(["--self-test"])
         prints a line starting "pages ok protocol 1."; (c) async: `async with PluginTestHost(plugin)
         as host:` starts, and host.tools is a list.
      7. .github/workflows/ci.yml: on push to main and pull_request. Job "test": matrix os
         [ubuntu-latest, macos-latest, windows-latest] x python ["3.11", "3.13"]; steps checkout,
         setup-python, pip install -e ".[dev]", ruff check ., ruff format --check ., pytest -q,
         python -m pf_pages --self-test. Use actions/checkout@v4 and actions/setup-python@v5.
      8. README.md: title "pf-pages", one paragraph from the plan's Goal, "Development" (pip install -e
         ".[dev]", pytest, ruff), and the headings "Tools", "Pages", "Limits", "Design", "Build",
         "Install", "Smoke test", each with "To be written." (p5 fills them).
         CHANGELOG.md: Keep a Changelog header and "## [Unreleased]" with "### Added" and
         "- The pages plugin skeleton: manifest, self-test and CI.".
         LICENSE: the Apache License 2.0 text. .gitignore: Python defaults plus build/, dist/, *.spec.
         CLAUDE.md: five lines — what the repo is; that the plugin framework docs are
         https://github.com/privacyfence/privacyfence/blob/SDK_SHA_TO_FILL/docs/plugins.md
         and https://github.com/privacyfence/privacyfence/blob/SDK_SHA_TO_FILL/docs/plugin-protocol.md;
         that /devflow:dod is the definition of done; and the branch naming <type>/<kebab-case>.
      9. Run pip install -e ".[dev]", ruff format ., ruff check ., pytest -q.
      Stop with status=blocked if the dependency you wrote in pyproject.toml does not match
      `grep -E 'privacyfence@[0-9a-f]{40}#subdirectory=plugin-sdk' pyproject.toml` (the pin was never
      filled in at handover), if pip
      cannot install the SDK from the git URL (quote the error), or if
      `from privacyfence_plugin_sdk import PROTOCOL_VERSION, Plugin, PageEntry` fails.
    acceptance:
      - python -m pytest -q tests/test_scaffold.py passes
      - python -m pf_pages --self-test prints a line starting "pages ok protocol 1."
      - python -m ruff check . and python -m ruff format --check . exit 0
      - python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))" exits 0

  - id: p2a-fit
    title: Card-fitting helpers
    depends_on: [p1-scaffold]
    complexity: S
    touches:
      - src/pf_pages/fit.py
      - tests/test_fit.py
    brief: |
      Read docs/pf-pages-plan.md "Fitting text into a card". fit.py imports nothing from the package or
      the SDK.
      1. src/pf_pages/fit.py: PAYLOAD_BUDGET, PREVIEW_BUDGET, READ_CHUNK_CHARS, PREVIEW_CHARS,
         json_size(), fit() as specified.
      2. tests/test_fit.py: json_size counts both encodings (1,000 "😀" is larger with ensure_ascii); fit
         never exceeds the budget for ASCII, '"'-heavy and emoji text; fit returns len(text) when it all
         fits; fit returns start when one character does not fit.
    acceptance:
      - python -m pytest -q tests/test_fit.py passes
      - python -m ruff check . and python -m ruff format --check . exit 0

  - id: p2b-store
    title: Storage, validation and versions
    depends_on: [p2a-fit]
    complexity: M
    touches:
      - src/pf_pages/store.py
      - tests/test_store.py
    brief: |
      Read docs/pf-pages-plan.md "Storage", "Limits and validation", "Images, securely" and "Store
      API". This phase imports nothing from the SDK.
      1. src/pf_pages/store.py: the constants of the code block in "Limits and validation" (copy the
         regexes from that code block, not from any table) and ASSET_REF_RE from "Pages",
         StoreError(ValueError) with attribute `code` and the exact messages of the table, the four
         dataclasses, utc_now() and a module function `timestamp(dt) -> str` that formats with
         `dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")` (every stored timestamp goes
         through it), _atomic_write(), and Store with exactly the methods and signatures of "Store
         API", storing files as "Storage" lays out. publish raises page_exists / too_many_pages; update
         raises no_page, and page_changed when page.current != expected_current; delete raises no_page
         and page_changed the same way; read_html raises no_page and no_version; put_asset raises
         too_many_assets only for a new name; delete_asset raises no_asset; check_offset raises
         bad_offset; decode_asset decodes base64 (bad_base64), checks the size and the PNG/JPEG magic
         bytes (asset_mismatch); pages_referencing uses ASSET_REF_RE. Line endings
         are normalized in check_html and check_css as specified. Symlinks: as "Store API" says.
         list_pages skips a page whose page.json cannot be read or parsed, and logs a WARNING with its
         name only.
      2. tests/test_store.py, with tmp_path and a fake clock that advances one second per call:
         - publish → version 1, created_at == updated_at, both matching r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ";
           update twice → current 3, three versions, each vN.html readable via read_html(name, N);
           update with a stale expected_current → page_changed; list_pages order (updated_at
           descending); a corrupt page.json is skipped by list_pages; delete returns the version count
           and removes the folder;
         - every StoreError code in the table is raised by at least one test, each message compared
           verbatim (bad_offset through check_offset);
         - "con", "nul", "com1" are bad_name and "con.png" is bad_asset_name; a 64-character asset name
           is bad_asset_name and a 63-character one is accepted; titles with "\u202e", "\t", "\u2028"
           and "\x85" are bad_title;
         - check_html("a\r\nb\rc") stores "a\nb\nc";
         - write_css/read_css round trip and site.json fields;
         - put_asset for .png, .jpg and .jpeg with minimal valid bytes; "logo.svg", "a.gif" and
           "a.webp" are bad_asset_name; PNG bytes named .jpg and JPEG bytes named .png are asset_mismatch;
           1 MiB + 1 bytes; the 201st asset;
         - pages_referencing finds src="assets/x.png", does not match "myassets/x.png", and lists
           "stylesheet" last when site.css has url(assets/x.png);
         - a symlinked v1.html and a symlinked asset read as missing (pytest.skip when os.symlink raises
           OSError);
         - purge removes root; no ".tmp" file is left after any write.
    acceptance:
      - python -m pytest -q tests/test_store.py passes
      - grep -c "class StoreError" src/pf_pages/store.py prints 1
      - python -m ruff check . and python -m ruff format --check . exit 0

  - id: p3a-page-tools
    title: The five page tools (publish, update, delete, read, list) and purge
    depends_on: [p2b-store]
    complexity: M
    touches:
      - src/pf_pages/tools_pages.py
      - src/pf_pages/plugin.py
      - tests/test_tools_pages.py
    brief: |
      Read docs/pf-pages-plan.md "Tools" fully. For the SDK patterns (Prepared with state,
      @tool.execute, on_purge) read examples/plugins/today/today_plugin.py of privacyfence at commit
      the SDK pin: `git clone --filter=blob:none https://github.com/privacyfence/privacyfence /tmp/pf-src && git -C /tmp/pf-src checkout SDK_SHA_TO_FILL`.
      Stop with status=blocked if that clone fails.
      1. src/pf_pages/plugin.py: build_plugin(now: Callable[[], datetime] = utc_now) gains used_dirs,
         store_for(ctx) and the purge handler exactly as "Tools" and "Purge" say, and calls
         register_page_tools(plugin, store_for).
      2. src/pf_pages/tools_pages.py: register_page_tools registers publish, update, delete, read and
         list with exactly the gate, read_only, destructive, scopes, params, required, title,
         description and effect of the table and list (description and effect verbatim), and
         implements prepare/execute per "Previews and payloads" and "When a call cannot succeed",
         including update's three card cases. Use fit() for every truncation and chunk, with
         make_blocks building the whole block list. Writes carry everything execute needs in
         Prepared.state; execute never re-reads the arguments.
      3. tests/test_tools_pages.py with PluginTestHost(build_plugin(now=fake_clock)):
         - the five tools' gate, read_only, destructive and scopes as in the table (parametrized);
         - publish → card_shown, the preview's fields block holds the SHA-256 of the HTML, result ok and
           version 1; publish again → result {"ok": False, "error": <page_exists message>};
         - update → preview has a diff block, result version 2; deny the card → read returns version 1;
           update with one 3,000-character line → preview has heading "New HTML" and the "too large to
           show as a diff" text;
         - read of a 100,000-character LF-only ASCII page returns a chunk with a next offset; following
           next offsets reassembles the exact HTML; json.dumps({"blocks": outcome.released["blocks"]})
           stays under 100,000 bytes; read version=1 after an update returns version 1; version=0 and
           offset=-1 give the no_version and bad_offset messages in the payload;
         - a 300,000-character publish: the preview's code block is cut, the truncation text is present,
           and json_size(outcome.card.preview) <= PREVIEW_BUDGET;
         - delete → versions_deleted; list then says "No pages are published.";
         - list with 120 pages: first call 100 rows and Next offset 100, second call 20 rows;
         - card.scopes is {"page": [name]} for publish, update, delete and read, and {} for list;
         - host.purge("all") leaves no store folder.
      Stop with status=blocked if PluginTestHost rejects a tool definition from the table (quote the
      error): the table is wrong, not the host.
    acceptance:
      - python -m pytest -q tests/test_tools_pages.py passes
      - python -m pytest -q passes
      - python -m ruff check . and python -m ruff format --check . exit 0

  - id: p3b-asset-tools
    title: The stylesheet and asset tools, and the full self-test
    depends_on: [p3a-page-tools]
    complexity: M
    touches:
      - src/pf_pages/tools_assets.py
      - src/pf_pages/plugin.py
      - src/pf_pages/__main__.py
      - tests/test_tools_assets.py
      - tests/test_scaffold.py
    brief: |
      Read docs/pf-pages-plan.md "Tools", and src/pf_pages/tools_pages.py from p3a for the patterns
      to follow.
      1. src/pf_pages/tools_assets.py: register_asset_tools(plugin, store_for) registers css_write,
         css_read, asset_put, asset_list and asset_delete exactly as the table and list say, with
         previews and payloads per "Previews and payloads".
      2. src/pf_pages/plugin.py: call register_asset_tools(plugin, store_for) after register_page_tools.
      3. src/pf_pages/__main__.py: --self-test also checks that the ten tool names of the table are in
         {d["name"] for d in plugin.tool_definitions()} and raises SystemExit("pages: missing tools
         [...]") otherwise. tests/test_scaffold.py: assert the self-test passes and that a plugin
         missing a tool makes it raise SystemExit (monkeypatch tool_definitions).
      4. tests/test_tools_assets.py with PluginTestHost:
         - the five tools' gate, read_only, destructive and scopes (parametrized);
         - css_write then css_read returns the CSS; a second css_write shows a diff block; css_read
           with nothing stored says "No stylesheet is stored.";
         - asset_put of a base64 PNG and of a base64 JPEG succeed with the reference "assets/<name>"; bad
           base64 → {"ok": False, "error": <bad_base64 message>}; "logo.svg" → bad_asset_name;
           tool_definitions() shows asset_put with exactly the parameters name and content;
         - asset_list shows both; asset_delete's preview "Used by" names a page published with
           src="assets/<name>" (publish it through call_tool("publish", ...));
         - card.scopes is {"asset": [name]} for asset_put and asset_delete and {} for the css tools and
           asset_list.
    acceptance:
      - python -m pytest -q tests/test_tools_assets.py tests/test_scaffold.py passes
      - python -m pf_pages --self-test exits 0
      - python -m pytest -q passes
      - python -m ruff check . and python -m ruff format --check . exit 0

  - id: p4-pages
    title: Page index and page view with the inlined stylesheet and images
    depends_on: [p3b-asset-tools]
    complexity: M
    touches:
      - src/pf_pages/render.py
      - src/pf_pages/views.py
      - src/pf_pages/plugin.py
      - tests/test_render.py
      - tests/test_pages.py
    brief: |
      Read docs/pf-pages-plan.md "Pages" and "Images, securely".
      1. src/pf_pages/render.py (no SDK import; ASSET_REF_RE imported from store): MAX_RENDERED_BYTES = 8_388_608,
         RenderTooLarge, inline() and message_html(), exactly as specified, every interpolated value
         in message_html through html.escape(..., quote=True).
      2. src/pf_pages/views.py: register_pages(plugin, store_for) registers @plugin.page_index (the
         PageEntry per page as specified) and @plugin.page("/view") with the answers of the table in
         "Pages", using the store's list_pages, read_css, read_html and get_asset (via a lookup closure
         returning (mime_type, bytes)). src/pf_pages/plugin.py: call register_pages(plugin, store_for).
      3. tests/test_render.py (pure): inline replaces "assets/x.png", 'assets/x.png', url(assets/x.png)
         and url("assets/x.png") in HTML and CSS; leaves an unknown asset alone; does not touch
         "myassets/x.png"; the mime type in the data URI comes from the extension; the style block goes
         right after <head lang="en"> and at the start with no head; CSS containing "</style>" is
         emitted as "<\/style>"; with MAX_RENDERED_BYTES monkeypatched (pf_pages.render) to 10,000 and
         one 4,000-byte asset referenced 1,000 times, inline raises RenderTooLarge.
      4. tests/test_pages.py with PluginTestHost:
         - after publishing pages "a" then "b" via call_tool, `await host.list_pages()` returns two
           entries, "b" first, with path "/view?p=b", the title, version "1", both timestamps and
           description "1 version"; after an update of "a", "a" comes first with version "2" and
           "2 versions"; with no pages it returns [];
         - after publish + update + css_write + asset_put, host.get("/view", query={"p": name}) is 200,
           headers["content-type"].startswith("text/html"), and contains the pf-pages-site-css style
           block, a "data:image/png;base64," src and the version 2 HTML; query v="1" returns version 1;
         - v="abc", v="0" and v="9" are 404 with the no_version message; no p is 404 with "No page was
           named in the address."; an unknown and a bad name are 404 with "There is no page named";
         - with pf_pages.render.MAX_RENDERED_BYTES monkeypatched to 1,000 a page is 500 with
           "Page too large";
         - host.get("/") is 404 (the plugin has no page of its own there).
      Stop with status=blocked if PluginTestHost has no list_pages or the SDK has no page_index /
      PageEntry (the pin is not at the framework merge), or if host.get does not pass the query to the
      page handler as request.query (quote what it passes).
    acceptance:
      - python -m pytest -q tests/test_render.py tests/test_pages.py passes
      - python -m pytest -q passes
      - python -m ruff check . and python -m ruff format --check . exit 0

  - id: p5-build-docs-retire
    title: PyInstaller build, CI build job, README, changelog; retire the plan
    depends_on: [p4-pages]
    complexity: S
    touches:
      - scripts/build.py
      - scripts/pages_plugin_entry.py
      - .github/workflows/ci.yml
      - README.md
      - CHANGELOG.md
      - .claude/toolkit.yaml
      - docs/pf-pages-plan.md
      - docs/pf-pages-plan-manual-steps.html
    brief: |
      1. scripts/pages_plugin_entry.py: `from pf_pages.__main__ import main` and `main()`.
         scripts/build.py, modelled on privacyfence's scripts/build_example_plugin.py at commit
         the SDK pin (clone as p3a's brief says): argparse --out (default
         "dist"); run `python -m PyInstaller --onefile --name pages-plugin --distpath <tmp> --workpath
         <tmp> --specpath <tmp> scripts/pages_plugin_entry.py`; create <out>/pages/ holding the
         executable and a copy of privacyfence-plugin.yaml; run "<out>/pages/pages-plugin[.exe]
         --self-test" and exit non-zero unless it prints a line starting "pages ok protocol ". Print the
         folder path at the end.
      2. .github/workflows/ci.yml: add job "build" (needs: test), matrix [ubuntu-latest, macos-latest,
         windows-latest], python 3.12: pip install -e ".[dev]", python scripts/build.py --out dist,
         actions/upload-artifact@v4 named pages-plugin-${{ matrix.os }} with path dist/pages/.
      3. README.md: fill every "To be written." section. "Tools": the plan's tool table with each tool's
         description. "Pages": PrivacyFence's page browser (Plugins menu → Pages) lists every page through
         the plugin's page index and opens each at /plugins/pages/view?p=<name> in a new tab; the
         stylesheet and images are inlined into each page ("Images, securely", reworded); this needs a
         PrivacyFence whose plugin protocol is 1.2 or later (the SDK pin's commit). "Limits": the limits from "Limits and validation", the line-ending normalization
         and control-character note, that only PNG and JPEG images are accepted, and the card-size and base64 notes from the plan's Risks. "Design":
         the plan's "What was rejected" list, reworded as decisions. "Build": scripts/build.py usage and
         the CI artifacts. "Install": point to
         https://github.com/privacyfence/privacyfence/blob/SDK_SHA_TO_FILL/docs/plugins.md#installing-a-plugin
         and name the folder dist/pages. "Smoke test": these numbered steps —
         (1) install and "Review and enable": the card lists 10 tools, 6 popup (2 destructive) and 4
         review, and pages; (2) ask Claude to store a stylesheet: popup card with the CSS; (3) store a
         small PNG logo and a JPEG photo: popup cards with type, size and SHA-256; (4) publish a page that
         uses both: popup card with the HTML; (5) top navigation → Plugins → Pages: PrivacyFence's page
         browser lists the page as version 1 with dates; (6) click its title: a new tab shows it styled,
         with both images; (7) ask for an update: a diff card; reload the browser: version 2 and a later
         Updated; (8) ask to store logo.svg: the call fails with the bad_asset_name message and
         stores nothing; (9) pages_read version 1: a review card, and Claude gets version 1's HTML; (10) delete the page:
         a destructive popup with no "Always allow"; the browser says "This plugin lists no pages."; (11)
         "Delete this plugin's data" in Settings, then pages_asset_list says "No assets are stored.".
      4. CHANGELOG.md [Unreleased] → "### Added": one line each for the ten tools, the page index for
         PrivacyFence's page browser, the page view with the inlined stylesheet and images, PNG and JPEG
         images only, and the build script and CI artifacts.
      5. Retire the plan: delete docs/pf-pages-plan.md and docs/pf-pages-plan-manual-steps.html, and
         remove "docs/pf-pages-plan.md" from .claude/toolkit.yaml docs.must_read (keep README.md).
      Stop with status=blocked if `pip install -e ".[dev]"` cannot install PyInstaller or
      scripts/build.py fails in the session (quote the error); do not mark the build as checked by CI
      instead.
    acceptance:
      - python scripts/build.py --out dist exits 0 and dist/pages/privacyfence-plugin.yaml exists
      - grep -rn "To be written" README.md prints nothing
      - grep -c "PNG and JPEG\|1\.2" README.md prints 2 or more, and README.md's Smoke test has steps (1) to (11)
      - grep -rn "pf-pages-plan" . --exclude-dir=.git --exclude-dir=dist --exclude-dir=build prints nothing
      - python -m pytest -q passes; python -m ruff check . and python -m ruff format --check . exit 0
```
