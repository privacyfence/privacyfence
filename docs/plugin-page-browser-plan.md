# Plugin page browser served by PrivacyFence

## Goal

A plugin can have many pages (the `pages` plugin in `privacyfence/pf-pages` has one per published
HTML page). PrivacyFence itself serves a **page browser** that lists each plugin's pages with a
title, version, created and updated date. Each entry opens that single page in a new tab. The
plugin says which pages it has through a new protocol request, `pages.list` (protocol 1.2).
PrivacyFence renders the list in its own UI. The plugin's pages keep the ADR 0124 sandbox
unchanged: no popups, no relaxed cookie, no subresource loading.

The browser is an ordinary PrivacyFence page, same-origin and not sandboxed, with its own nonce
CSP. A link from it to `/plugins/<name>/…` therefore carries the `SameSite=Strict` session cookie,
which is the "open from Settings" path ADR 0124 already allows. This replaces the earlier idea of a
same-origin "bounce" with `allow-popups`. That idea was rejected because it loosened the cookie and
sandbox guarantees for every plugin page.

Tracking issue: https://github.com/privacyfence/privacyfence/issues/846. The baseline is PR #869
(`feature/plugins-nav-menu`, the top navigation's **Plugins** menu), which is based on PR #868
(`feature/plugin-framework-polish`).

## Current state

At `feature/plugins-nav-menu` (539a6c28):

- **Protocol 1.1.0.**
  - `src/privacyfence/plugins/constants.py:13` sets `PROTOCOL_VERSION = "1.1.0"`, and `:56-65`
    holds `TIMEOUT_SECONDS`.
  - `src/privacyfence/plugins/protocol.py` holds the wire dataclasses (`WebResponse` at :603 is the
    pattern for a result type) and the helpers `_obj`, `_req`, `_str`, `_opt_str`, `_bad`.
  - `docs/plugin-protocol/protocol.schema.json` (`x-protocol-version`, `x-limits`, `x-timeouts`) is
    the machine description.
  - `scripts/gen_plugin_sdk_types.py` generates
    `plugin-sdk/src/privacyfence_plugin_sdk/types.py` from it. `--check` fails when the file is
    stale, and `tests/unit/test_gen_plugin_sdk_types.py` runs that check.
- **"1.1.0" is asserted** in:
  - `tests/unit/plugins/test_constants.py:145`, `tests/unit/plugins/test_protocol.py:196`,
    `tests/unit/plugins/test_host.py:324` and `tests/unit/plugin_sdk/test_plugin.py:130`;
  - `plugin-sdk/src/privacyfence_plugin_sdk/plugin.py:34`;
  - `docs/plugin-protocol.md:4,177,195` and `examples/plugins/today/README.md:41`.

  `tests/unit/plugins/test_approvals.py:28` and
  `tests/integration/test_plugin_approval_frame_browser.py:55` use it as a manifest `protocol`
  value, where it is a plugin's own declared version, and keep it.
- **The host.**
  - `src/privacyfence/plugins/host.py:887` `web_request()` is the pattern for a request to a
    running plugin: look up the plugin, its supervisor peer and its manifest, build
    `_request_context`, then `peer.request(...)`.
  - `host.py:841` `page_links()` (PR #869) returns `(display name, "/plugins/<name>/")`.
  - `host.py:872` `rows()` gives each plugin row `"page_url": "/plugins/<name>/"`.
- **The UI.**
  - `src/privacyfence/web_shell.py` (PR #869): `_plugins_html()` and `_STREAM_JS`'s
    `updatePluginsMenu()` build the Plugins menu, with each link `target="_blank"` to the plugin's
    `/`. `wrap(..., plugin_pages=...)` renders it.
  - `src/privacyfence/settings_window_html.py:1231-1233`: the plugin card's
    `<a ... target="_blank">Open page</a>`.
  - `src/privacyfence/web/server.py:1063-1068` mounts `routes_plugins.build_routes` under
    `_owner_only_routes`, with `is_owner_session=lambda request: _is_human_session(request,
    sessions)`. `server.py:1093` passes `plugin_pages=lambda: plugin_host.page_links()`.
  - Every response under `/plugins/` gets the sandbox CSP (`server.py:493`, `_PLUGIN_PAGES_PREFIX` at :417; `"/plugin-pages"` does not start with `"/plugins/"`), so the browser must
    not live under `/plugins/`.
- **The SDK.**
  - `plugin-sdk/src/privacyfence_plugin_sdk/plugin.py`: `_Registry` (:475) and `Plugin.page()`
    (:618).
  - `serve()` (:873) builds the request handler table. An unknown method is answered
    `method_not_found` by `_rpc.py:320`.
  - The test host is `testing/_host.py` (`get`, `request`), with page serving in
    `testing/_pages.py`.
- **Tests to extend:**
  - `tests/unit/plugins/test_protocol.py`, `test_constants.py`, `test_protocol_doc.py` and
    `test_host.py`;
  - `tests/unit/plugin_sdk/test_plugin.py` and `test_testhost_surfaces.py`;
  - `tests/unit/test_web_shell.py`, `tests/unit/test_settings_window_html.py` and
    `tests/unit/web/test_server.py`;
  - `tests/integration/test_plugin_settings_browser.py` and
    `tests/integration/test_sdk_testhost_conformance.py`.

## Design

### Protocol 1.2.0: `pages.list`

- `PROTOCOL_VERSION = "1.2.0"`. Version 1.2 adds one daemon-to-plugin request, `pages.list`, and
  changes no 1.1 message. A plugin that does not implement it answers `method_not_found`, as every
  1.0 and 1.1 plugin does.
- Who is asked: the daemon sends `pages.list` only to a running plugin whose manifest has
  `pages: true`. The timeout is `TIMEOUT_SECONDS["pages.list"] = 10.0`.
- Params: `{"principal": PrincipalContext}`.
- Result: `{"pages": [PageEntry, …]}`, at most `MAX_PAGE_INDEX_ENTRIES = 500` entries, in the order
  the browser shows them.

A `PageEntry`:

| Field | Rule |
|---|---|
| `path` | Required. 1 to `MAX_PAGE_PATH_CHARS` (512) characters matching `PAGE_ENTRY_PATH_RE = re.compile(r"/[\x21-\x7e]*")` (fullmatch): printable ASCII only, so no space, tab, newline or other character a browser strips while parsing a URL. No `#` and no backslash anywhere. It may carry a query after the first `?`. The part before `?` must pass `plugins.pages.normalize_path` unchanged (no NUL or `//`, already decoded, so `normalize_path(p) == p`), and none of its segments may be `.` or `..`. Together these keep `"/plugins/<name>" + path` inside the plugin's own pages once a browser resolves it. |
| `title` | Required, 1 to `MAX_TITLE_CHARS` (120) characters, with no control or bidirectional characters, line breaks or tabs: `blocks.clean_line(title) == title`, the rule `display_name` follows. |
| `version` | Optional, 1 to `MAX_PAGE_VERSION_CHARS = 40` characters, same character rule as `title`. |
| `created_at`, `updated_at` | Optional. An RFC 3339 timestamp with a time zone (`datetime.fromisoformat` accepts it after `Z` is replaced by `+00:00`, and the result is time-zone aware). |
| `description` | Optional, 1 to `MAX_PAGE_DESCRIPTION_CHARS = 200` characters, same character rule. |

Validation is whole-list, like tool definitions: the first bad entry fails the result with
`invalid_params`, `detail` naming the entry index and field, for example `pages[3].path: …`. Unknown
fields are ignored.

The protocol dataclasses go in `protocol.py`: `PageEntry` (frozen, with the fields above and
`from_wire` / `to_wire`) and `PagesListResult(pages: tuple[PageEntry, ...])` with
`from_wire(obj, *, mode="local")`. Both are added to `__all__`.

### Host: `PluginHost.list_pages`

```python
@dataclass(frozen=True)
class PageIndex:
    name: str                       # plugin name
    display_name: str
    entries: tuple[PageEntry, ...]  # empty when error is set
    error: str = ""                 # one of the PAGE_INDEX_* sentences below, or ""

PAGE_INDEX_NO_ANSWER = "The plugin did not answer."
PAGE_INDEX_INVALID = "The plugin returned an invalid page list."

async def list_pages(self, name: str, principal: Principal) -> PageIndex
async def list_all_pages(self, principal: Principal) -> list[PageIndex]   # every running pages plugin, by display name (casefold)
```

- A plugin that is not running, or has `pages: false`, raises `LookupError`, like `web_request`.
- `method_not_found` gives the one-entry index `PageEntry(path="/", title=display_name)`. That is
  the 1.1 behaviour, so the `today` example and every existing plugin keep exactly one page.
- `timeout` and `internal_error` give `error=PAGE_INDEX_NO_ANSWER`. Any other `RpcError`, or a
  result that fails `PagesListResult.from_wire`, gives `error=PAGE_INDEX_INVALID`. The error is also
  logged at WARNING with the plugin name and the `RpcError` code, never the result.
- `list_all_pages` calls `list_pages` for each plugin concurrently (`asyncio.gather`), so one slow
  plugin costs at most the 10-second timeout, not the sum of them. A plugin whose `list_pages`
  raises `LookupError` (it stopped in between) is left out, never failing the whole page.

`page_links()` and `rows()` link to the browser instead of the plugin:

- `page_links()` returns `(display name, "/plugin-pages/<name>")`.
- `rows()` sets `"page_url": "/plugin-pages/<name>"`, under the same conditions as today.

### The browser: `GET /plugin-pages` and `GET /plugin-pages/<name>`

New module `src/privacyfence/web/routes_plugin_browser.py`:

```python
def build_routes(
    plugin_host: PluginHost, *, is_owner_session: Callable[[Request], bool],
    notifications_enabled: bool = True, notifications_detail: str = "minimal",
) -> list[BaseRoute]
```

- `Route("/plugin-pages", all_pages, methods=["GET"], name="plugin_pages")` and
  `Route("/plugin-pages/{name}", one_plugin, methods=["GET"], name="plugin_pages_one")`.
- Both answer `PlainTextResponse("Not Found", status_code=404)` unless `is_owner_session(request)`.
  That is the same answer and the same check as `routes_plugins.py`, because a page list is the
  owner's, never an agent's.
- `one_plugin` checks `PLUGIN_NAME_RE.fullmatch(name)`, then calls `list_pages`. `LookupError`
  gives the same 404.
- Both render `web_shell.wrap(body, title="PrivacyFence — Plugin pages", active="plugin-pages",
  nonce=csp.nonce_for(request), notifications_enabled=notifications_enabled,
  notifications_detail=notifications_detail, plugin_pages=tuple(plugin_host.page_links()))`, as an
  `HTMLResponse` with `Cache-Control: no-store`. `server.py` passes the same
  `notifications_enabled` and `notifications_detail` it passes to the approvals app.
- `server.py` mounts them with `_owner_only_routes(build_plugin_browser_routes(plugin_host,
  is_owner_session=lambda request: _is_human_session(request, sessions)))`, next to the plugin page
  routes, only when `plugin_host is not None`. That is local mode only.

New module `src/privacyfence/plugin_browser_html.py` holds a pure renderer with no Starlette
import:

```python
def render(indexes: list[PageIndex], *, single: bool, nonce: str) -> str
def format_timestamp(value: str | None) -> str   # "YYYY-MM-DD HH:MM UTC", converted to UTC; "" for None
```

- `<h1>` reads `Plugin pages`, or the plugin's display name when `single`. When `single`, a
  `<p><a href="/plugin-pages">All plugin pages</a></p>` link follows it.
- With no plugin at all (`not single` and no indexes) the body is `<p id="no-plugins">No plugin
  with pages is running.</p>`.
- Each index is a `<section class="pf-plugin-pages" data-plugin="<name>">`. It has an `<h2>` with
  the display name (left out when `single`), then one of these:
  - `<p class="pf-plugin-pages-error">` with the error sentence;
  - `<p class="pf-plugin-pages-empty">This plugin lists no pages.</p>`;
  - a `<table>` with the header row Title, Version, Created and Updated.
- Each table row's Title cell is `<a href="/plugins/<name><path>" target="_blank"
  rel="noopener">{title}</a>`, with the description below it in `<div
  class="pf-plugin-pages-desc">` when there is one. Version is `version` or `—`, and Created and
  Updated use `format_timestamp`, or `—`.
- Every value goes through `html.escape(…, quote=True)`, the `href` included. The `href` is the
  literal concatenation `"/plugins/" + name + path`. `path` has already passed the protocol rules
  (printable ASCII, no `.` or `..` segment), so it cannot leave `/plugins/<name>/` once a browser
  resolves it.
- There is no inline script. Styles reuse the shell's tokens (`var(--ink)`, `var(--line)`,
  `var(--surface)`) in a `<style nonce=…>` block carrying the `nonce` argument.

### The Plugins menu and the Settings card (on top of PR #869)

- **The menu.** In `web_shell.py`'s `_plugins_html()` and `_STREAM_JS`'s `updatePluginsMenu()`:
  - The first item is `All plugin pages` → `/plugin-pages`, shown whenever the menu is.
  - Then one item per plugin → `/plugin-pages/<name>`.
  - Every item opens in the same tab: drop `target="_blank"` and `rel="noopener"`, because the
    browser is an app page.
  - The stream script gets `page_url` from the settings rows, which now point at the browser, so it
    keeps working unchanged apart from the target and the extra first item.
- **The card.** In `settings_window_html.py`, the plugin card's link reads `Pages`, has
  `aria-label="Pages of <display name>"` and no `target` or `rel`, and points at `p.page_url`.

### SDK: `@plugin.page_index`

In `plugin-sdk/src/privacyfence_plugin_sdk/`:

- **`PageEntry`.** A frozen dataclass `PageEntry(path: str, title: str, version: str | None = None,
  created_at: str | None = None, updated_at: str | None = None, description: str | None = None)`
  in `responses.py`, exported from `__init__`, with `to_wire()` that leaves out `None` fields.
- **`Plugin.page_index(fn)`.** A decorator registering `async def fn(ctx) -> list[PageEntry]`,
  stored on `_Registry.page_index`.
  - Registering a second one raises `ValueError("page_index is already registered")`.
  - On a plugin with no `@plugin.page(...)` it is allowed, and the test host and daemon decide by
    the manifest's `pages`.
- **The handler.** `serve()` adds `"pages.list": self._pages_list` to its handlers only when a page
  index is registered. Without one the request stays `method_not_found`, the 1.1 behaviour.
  - `_pages_list` validates the returned entries with the SDK's own copy of the `PageEntry` rules,
    in a new function `validate_page_entries(entries) -> list[dict]` in `blocks.py`, next to the
    other validators. The `normalize_path` equivalent lives there too, as
    `_normalized_page_path(path) -> str | None`.
  - A violation raises `RpcError("invalid_params", …)`, so a plugin author sees it in a test.
- **The test host.** `PluginTestHost.list_pages(principal: str | None = None) -> list[dict]` sends
  `pages.list` the way the daemon does and applies the same rules.
  - `method_not_found` gives `[{"path": "/", "title": <plugin name>}]`. The test host has no
    display name, so it uses the plugin name, and `docs/plugins.md`'s differences list says so.
  - An invalid result raises `AssertionError` with the validation detail.
- **The version.** `PROTOCOL_VERSION = "1.2.0"` in `plugin.py`.

### What was rejected (these go into ADR 0132)

- **A same-origin bounce plus `allow-popups allow-popups-to-escape-sandbox` on plugin pages.**
  Rejected:
  - Any site, and any plugin page, could make the owner's browser load any plugin page as the
    owner, which is effectively `SameSite=Lax` for plugin pages.
  - Script in a plugin page could open any URL in an unsandboxed tab.
  - For a plugin like `pages`, which serves HTML an AI wrote, that is an exfiltration and phishing
    surface.
- **`SameSite=Lax` for the session cookie.** Rejected: it weakens every Settings and approvals
  route.
- **The plugin serves its own browser page.** Rejected: links from a sandboxed page carry no
  cookie (ADR 0124), and fixing that is the bounce above.
- **Serving a plugin's images and CSS as separate URLs.** Rejected and unchanged: a sandboxed
  page's subresource requests carry no cookie. A plugin inlines them (`data:` URIs, `<style>`).
- **A page index in the manifest.** Rejected: the manifest is reviewed and hashed at enable time,
  so a plugin whose pages change at runtime (published HTML) would need re-review for every new
  page.

## ADRs

- **ADR 0132**, `docs/adr/0132-plugin-pages-are-listed-by-the-plugin-and-browsed-in-privacyfence.md`
  (or the next free number). Decision: protocol 1.2's `pages.list` lets a plugin list its pages,
  PrivacyFence serves the browser at `/plugin-pages`, and the ADR 0124 sandbox, cookie and
  subresource rules are unchanged. It amends ADR 0124: other pages of a plugin now open from the
  page browser as well as from Settings or a typed URL.

## Manual steps

Step by step: [the manual steps page](https://claude.ai/artifact/EJ7PWQ9pi2aBGMUXzuGetF)
(`docs/plugin-page-browser-plan-manual-steps.html`).

- **Before** (`mb1-baseline-merged`): PR #868 (`feature/plugin-framework-polish`) and PR #869
  (`feature/plugins-nav-menu`) are merged into `main`. This is a review-and-merge decision only the
  maintainer makes.
- **After:** nothing. The new-tab behaviour is checked by the browser integration test in p4.

## Risks and open questions

- **The PR's base.** `/implement` merges `origin/main` into the feature branch and opens its PR to
  `main`. This work builds on PR #868 and PR #869, so it can only start once both have merged into
  `main` (`manual_before` `mb1-baseline-merged`). Until then the final review and the PR would carry
  their whole diff. p1's first step checks the baseline is there (`page_links` in `host.py`).
- **ADR number.** PR #868 or #869 may gain an ADR before this lands. p5 takes the next free number
  and updates every reference.
- **`normalize_path` decodes.** An entry path with `%2F` decodes to `/`, and `normalize_path(p) == p`
  fails. That is intended (paths must be given decoded). If an existing test expects encoded
  paths in entries, the worker stops and reports it.
- **The stream script.** `updatePluginsMenu` rebuilds the menu from settings events. If PR #869
  changes it again before this lands, p4's merge conflicts. The worker resolves it to keep both
  changes, or stops if they contradict.

## Implementation manifest

```yaml
plan_slug: plugin-page-browser
feature_branch: feature/plugin-page-browser
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/EJ7PWQ9pi2aBGMUXzuGetF
manual_steps_source: docs/plugin-page-browser-plan-manual-steps.html
manual_before:
  - id: mb1-baseline-merged
    title: Merge PR #868 and PR #869 into main
    why: /implement merges origin/main into the feature branch and opens the PR to main; p1 needs PluginHost.page_links() from PR #869, and without the merge the review and the PR would carry both PRs' whole diff.
    done_when: "Both PRs show Merged on GitHub, and `git fetch origin main && git show origin/main:src/privacyfence/plugins/host.py | grep -n 'def page_links'` prints a line."
verify_after_merge:
  - python -m pytest -q tests/unit/plugins tests/unit/plugin_sdk tests/unit/test_web_shell.py tests/unit/test_settings_window_html.py tests/unit/web/test_server.py tests/unit/test_gen_plugin_sdk_types.py
  - python -m ruff check src plugin-sdk tests scripts
final_checks:
  - docs/plugin-page-browser-plan.md is deleted and nothing links to it (grep -rn "plugin-page-browser-plan" . --exclude-dir=.git prints nothing)
  - ADR 0132 (or the number p5 took) exists, is Accepted, amends 0124, and is in docs/adr/README.md's index; ADR 0124's Status has the Amended-by line
  - CHANGELOG.md has [Unreleased] entries for this change and no version heading
  - python scripts/gen_plugin_sdk_types.py --check exits 0
  - docs/plugin-page-browser-plan-manual-steps.html is deleted
phases:
  - id: p1-protocol
    title: Protocol 1.2.0 — pages.list, PageEntry, schema, generated SDK types, protocol doc
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/plugins/constants.py
      - src/privacyfence/plugins/protocol.py
      - docs/plugin-protocol/protocol.schema.json
      - plugin-sdk/src/privacyfence_plugin_sdk/types.py
      - docs/plugin-protocol.md
      - examples/plugins/today/README.md
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - tests/unit/plugin_sdk/test_plugin.py
      - tests/unit/plugins/test_host.py
      - tests/unit/plugins/test_constants.py
      - tests/unit/plugins/test_protocol.py
      - tests/unit/plugins/test_protocol_doc.py
    brief: |
      Read docs/plugin-page-browser-plan.md "Protocol 1.2.0: pages.list" and "Current state".
      First check the base: `grep -n "def page_links" src/privacyfence/plugins/host.py` must print a
      line; otherwise stop with status=blocked (the feature branch is not on top of PR #869).
      1. constants.py: PROTOCOL_VERSION = "1.2.0"; MAX_PAGE_INDEX_ENTRIES = 500;
         MAX_PAGE_VERSION_CHARS = 40; MAX_PAGE_DESCRIPTION_CHARS = 200;
         PAGE_ENTRY_PATH_RE = re.compile(r"/[\x21-\x7e]*"); TIMEOUT_SECONDS["pages.list"] = 10.0.
         Also set PROTOCOL_VERSION = "1.2.0" in plugin-sdk/src/privacyfence_plugin_sdk/plugin.py (the
         SDK's TestLimits asserts it equals the daemon's).
      2. protocol.py: PageEntry and PagesListResult as specified, validated with the existing helpers
         (_obj, _req, _str, _opt_str, _bad), the path rule through plugins.pages.normalize_path, the
         path also through PAGE_ENTRY_PATH_RE, no "#" or backslash, and no "." or ".." segment before
         the "?", the character rule through blocks.clean_line (protocol.py already imports
         privacyfence.plugins.blocks), timestamps through datetime.fromisoformat after replacing a trailing "Z" with
         "+00:00" and requiring tzinfo. Errors are RpcError("invalid_params", "pages[<i>].<field>: …").
         Give both a WIRE_KEYS ClassVar as WebResponse has (protocol.py:609), and add both to __all__.
      3. protocol.schema.json: x-protocol-version "1.2.0"; the new limits in x-limits and the timeout
         in x-timeouts; definitions PagesListParams, PageEntry and PagesListResult matching the table.
         Then run `python scripts/gen_plugin_sdk_types.py` to regenerate the SDK's types.py (never edit
         types.py by hand).
      4. docs/plugin-protocol.md: version 1.2.0 everywhere it says 1.1.0; a "Version 1.2 adds"
         paragraph under Versioning; `pages.list` in the Messages table (D to P, request, "List the
         plugin's pages for the page browser"); a "### `pages.list`" section with params, result, the
         PageEntry table, the 500 limit, method_not_found → one entry for "/", and "the page browser
         at /plugin-pages lists the entries; each opens /plugins/<name><path> in a new tab"; the
         limits and timeouts tables gain the new rows.
         examples/plugins/today/README.md: "today ok protocol 1.2.0".
      5. Tests: update every "1.1.0" listed in the plan's Current state except the two manifest
         `protocol=` values; test_protocol.py gains PagesListResult cases: a valid list with every
         field; 501 entries; each rule violated once (path without "/", with "#", with "..", with
         "%2F"; title empty, 121 chars, with "\n", with "‮"; version 41 chars; created_at
         "yesterday" and "2026-10-09T10:00:00" without zone; description 201 chars), each asserting
         the RpcError code and that detail starts with "pages[<i>].<field>"; also paths with "\t", "\n",
         a space, "/.\t./x", "/./x" and "/a/../b"; add PagesListParams, PageEntry and PagesListResult
         to the parametrize list of test_required_keys_are_all_handled (it maps $defs to WIRE_KEYS);
         update "1.1.0" in tests/unit/plugin_sdk/test_plugin.py:130 and tests/unit/plugins/test_host.py:324;
         test_constants.py asserts
         the new constants; test_protocol_doc.py keeps passing (it compares the doc with constants).
      Stop with status=blocked if test_protocol_doc.py or test_gen_plugin_sdk_types.py need a change
      to their own logic (not only to expected values) to pass.
    acceptance:
      - python -m pytest -q tests/unit/plugins/test_protocol.py tests/unit/plugins/test_constants.py tests/unit/plugins/test_protocol_doc.py tests/unit/test_gen_plugin_sdk_types.py passes
      - python scripts/gen_plugin_sdk_types.py --check exits 0
      - grep -rn '"1\.1\.0"' src plugin-sdk/src docs/plugin-protocol tests/unit/plugins/test_constants.py tests/unit/plugins/test_protocol.py tests/unit/plugins/test_host.py tests/unit/plugin_sdk/test_plugin.py prints nothing
      - python -m pytest -q tests/unit/plugin_sdk/test_plugin.py tests/unit/plugins/test_host.py passes
      - python -m ruff check src plugin-sdk tests scripts exits 0

  - id: p2-host
    title: PluginHost.list_pages / list_all_pages; menu and card links point at the browser
    depends_on: [p1-protocol]
    complexity: S
    touches:
      - src/privacyfence/plugins/host.py
      - tests/unit/plugins/test_host.py
    brief: |
      Read docs/plugin-page-browser-plan.md "Host: PluginHost.list_pages".
      1. host.py: the PageIndex dataclass, PAGE_INDEX_NO_ANSWER and PAGE_INDEX_INVALID, and
         list_pages / list_all_pages exactly as specified, modelled on web_request (host.py:887);
         page_links() and rows() use "/plugin-pages/<name>". Export PageIndex and the two constants
         in __all__.
      2. tests/unit/plugins/test_host.py, with the file's existing fake peer: a plugin answering two
         entries → PageIndex with both; method_not_found → one entry ("/", display name); a timeout
         RpcError → PAGE_INDEX_NO_ANSWER; an invalid result (an entry without title) →
         PAGE_INDEX_INVALID and a WARNING log that does not contain the result; a stopped plugin and a
         pages: false plugin → LookupError; list_all_pages orders by display name casefold, skips
         plugins that are not running, and leaves out a plugin whose list_pages raises LookupError
         (monkeypatch it) while still returning the others; page_links() and rows()[...]["page_url"] are
         "/plugin-pages/<name>"; update the existing page_links/page_url assertions to the new URL.
    acceptance:
      - python -m pytest -q tests/unit/plugins/test_host.py passes
      - grep -n '"/plugins/{name}/"\|f"/plugins/{name}/"' src/privacyfence/plugins/host.py prints nothing
      - python -m ruff check src tests exits 0

  - id: p3-sdk
    title: SDK page_index, PageEntry, validation and PluginTestHost.list_pages
    depends_on: [p1-protocol]
    complexity: M
    touches:
      - plugin-sdk/src/privacyfence_plugin_sdk/__init__.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - plugin-sdk/src/privacyfence_plugin_sdk/responses.py
      - plugin-sdk/src/privacyfence_plugin_sdk/blocks.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
      - plugin-sdk/README.md
      - tests/unit/plugin_sdk/test_plugin.py
      - tests/unit/plugin_sdk/test_testhost_surfaces.py
      - tests/unit/plugin_sdk/test_testhost.py
    brief: |
      Read docs/plugin-page-browser-plan.md "SDK: @plugin.page_index". The SDK must not import
      privacyfence (ADR 0126); copy rules, do not import them.
      1. responses.py: PageEntry; __init__.py exports it.
      2. blocks.py: validate_page_entries(entries) and _normalized_page_path(path), mirroring
         protocol.py's PageEntry rules from p1 and plugins/pages.normalize_path, with the same
         "pages[<i>].<field>: …" details, including the printable-ASCII path rule and the "."/".."
         segment rule; the limits as private module constants in the SDK's style (_MAX_PAGE_INDEX_ENTRIES,
         _MAX_PAGE_VERSION_CHARS, _MAX_PAGE_DESCRIPTION_CHARS, _PAGE_ENTRY_PATH_RE).
      3. plugin.py (PROTOCOL_VERSION is already "1.2.0" from p1): _Registry.page_index; Plugin.page_index decorator;
         _pages_list handler registered in serve() only when page_index is set; it builds a Context
         for the principal as _web_request does, awaits the function, converts each PageEntry with
         to_wire(), validates, and returns {"pages": [...]}.
      4. testing/_host.py: list_pages(principal=None) as specified; add "pages.list" to _TIMEOUTS
         (testing/_host.py:51) and pass timeout=_TIMEOUTS["pages.list"].
      5. tests: test_plugin.py — page_index registration twice raises; a plugin with a page_index
         answers pages.list with the wire entries; without one, pages.list is method_not_found; an
         invalid entry raises invalid_params (one case each for "\t", "/./x" and "#"); and extend
         TestLimits.test_block_limits_and_version (test_plugin.py:86-119) so the SDK's new private limits
         and path regex pattern equal privacyfence.plugins.constants'. test_testhost_surfaces.py —
         list_pages returns the entries, the fallback [{"path": "/", "title": <name>}] without an index,
         AssertionError for an invalid entry. tests/unit/plugin_sdk/test_testhost.py's timeout parity
         test (around :337) still passes with the new _TIMEOUTS key.
      6. plugin-sdk/README.md: a short "Page index" section with a three-line example.
    acceptance:
      - python -m pytest -q tests/unit/plugin_sdk passes
      - grep -n "import privacyfence\b\|from privacyfence\b" -r plugin-sdk/src prints nothing
      - python -m ruff check plugin-sdk tests exits 0

  - id: p4-browser
    title: /plugin-pages browser, Plugins menu and Settings card links, conformance and browser tests
    depends_on: [p2-host, p3-sdk]
    complexity: M
    touches:
      - src/privacyfence/web/routes_plugin_browser.py
      - src/privacyfence/plugin_browser_html.py
      - src/privacyfence/web/server.py
      - src/privacyfence/web_shell.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/web/test_routes_plugin_browser.py
      - tests/unit/test_plugin_browser_html.py
      - tests/unit/test_web_shell.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/web/test_server.py
      - tests/integration/test_plugin_settings_browser.py
      - tests/integration/test_sdk_testhost_conformance.py
    brief: |
      Read docs/plugin-page-browser-plan.md "The browser" and "The Plugins menu and the Settings card".
      1. plugin_browser_html.py: render(indexes, *, single, nonce) and format_timestamp as specified.
      2. web/routes_plugin_browser.py: build_routes as specified. server.py: mount it as specified,
         right after the plugin page routes (server.py:1063-1068), passing notifications_enabled and
         notifications_detail as the approvals app gets them.
      3. web_shell.py: _plugins_html and updatePluginsMenu as specified (first item "All plugin pages",
         same-tab links). settings_window_html.py: the card link as specified.
      4. Tests:
         - test_plugin_browser_html.py: escaping of a title "<img src=x>" and of a path with "\"" (a
           protocol-valid printable character);
           href is "/plugins/<name><path>" with target="_blank" rel="noopener"; error and empty
           sections; "—" for missing fields; format_timestamp converts "+02:00" to UTC; no "<script" in
           the output.
         - test_routes_plugin_browser.py, built through build_app(..., plugin_host=host) the way
           tests/unit/web/test_routes_plugins.py:52 builds its app, with a fake host exposing
           list_pages/list_all_pages/page_links: owner session → 200 with each entry; no session, an MCP
           bearer, a non-owner principal and an unattested session (copy test_routes_plugins.py's
           test_unattested_session_not_served) → 404 "Not Found" and the fake host is not called; an invalid name and an unknown plugin → 404;
           the response carries the app's nonce CSP (not the plugin sandbox CSP) and
           Cache-Control: no-store.
         - test_web_shell.py and test_settings_window_html.py: update PR #869's assertions to the new
           hrefs, no target="_blank", the "All plugin pages" first item, the "Pages" label.
         - test_server.py: /plugin-pages is mounted only when a plugin host is given; its CSP has no
           "sandbox".
         - test_plugin_settings_browser.py (real Chromium): sign in, open /plugin-pages/<name> from the
           Plugins menu, click an entry; `with context.expect_page() as info:` gives a new tab whose
           content is the plugin's page (served, not "Not Found") — this proves the Strict cookie
           travels from the browser to the sandboxed page.
         - test_sdk_testhost_conformance.py: one scenario running the same page_index plugin against
           the test host's list_pages and the daemon's list_pages, asserting equal entries, and the
           same for a plugin without an index (give that plugin a display name equal to its name, since
           the test host titles the fallback entry with the name).
      Stop with status=blocked if the new tab in the browser test gets "Not Found" while signed in
      (the link does not carry the cookie): report the request headers; do not change the cookie,
      the CSP or the sandbox.
    acceptance:
      - python -m pytest -q tests/unit/web/test_routes_plugin_browser.py tests/unit/test_plugin_browser_html.py tests/unit/test_web_shell.py tests/unit/test_settings_window_html.py tests/unit/web/test_server.py passes
      - python -m pytest -q tests/integration/test_plugin_settings_browser.py tests/integration/test_sdk_testhost_conformance.py passes with PRIVACYFENCE_TEST_CHROMIUM set
      - grep -n "_blank" src/privacyfence/web_shell.py prints nothing
      - python -m ruff check src tests exits 0

  - id: p5-docs-adr-retire
    title: Reference docs, ADR 0132, changelog; retire the plan
    depends_on: [p4-browser]
    complexity: S
    touches:
      - docs/plugins.md
      - docs/security-and-compliance.md
      - docs/adr/0132-plugin-pages-are-listed-by-the-plugin-and-browsed-in-privacyfence.md
      - docs/adr/0124-plugin-pages-are-get-only-owner-only-and-sandboxed.md
      - docs/adr/README.md
      - CHANGELOG.md
      - docs/plugin-page-browser-plan.md
      - docs/plugin-page-browser-plan-manual-steps.html
    brief: |
      1. docs/plugins.md: in "What a plugin is", the pages bullet says a plugin lists its pages and
         PrivacyFence's page browser (Plugins menu → the plugin, or /plugin-pages) opens each in a new
         tab; a "### Page index" paragraph in "Writing a plugin" with @plugin.page_index and PageEntry;
         the self-contained-pages sentence keeps "inline the CSS, scripts and images" and adds that
         links between a plugin's pages do not carry the session, so a multi-page plugin lists its
         pages with page_index instead of linking them; the "Installing a plugin" step that mentions
         "Open page" now says "Pages"; the test-host differences list gains "list_pages falls back to
         the plugin's name as the title; PrivacyFence uses its display name".
      2. docs/security-and-compliance.md (its plugins section): one sentence — the page browser is a
         PrivacyFence page; plugin pages keep the sandbox, and a plugin's images and styles are inlined,
         never fetched.
      3. ADR: take the next free number (0132 unless taken; then rename the file and every reference)
         from docs/adr/README.md's template: Status "Accepted — <today>." with "Amends 0124" on its own
         line; Context = the plan's Goal; Decision = protocol 1.2 pages.list, the host fallback, the
         browser routes and the unchanged sandbox; Alternatives = the plan's "What was rejected";
         Consequences = an extra request per plugin per browser view (10 s timeout each, concurrent), a
         plugin's page list is not reviewed at enable time, the browser is the only place that links
         into plugin pages; Verification = the tests of p1–p4; Related = issue 846, ADR 0124, ADR 0126.
         ADR 0124: add the line "Amended by 0132" to its Status and nothing else. docs/adr/README.md:
         the index row, and 0124's row status "Accepted; amended by 0132".
      4. CHANGELOG.md ## [Unreleased]: under "### Added", "- A plugin page browser: the Plugins menu
         opens a PrivacyFence page listing each plugin's pages with title, version and dates, and each
         page opens in a new tab. Plugin protocol 1.2 adds pages.list, and the SDK adds
         @plugin.page_index; plugins written for 1.0 and 1.1 keep working and are listed with one
         page." Under "### Changed", amend PR #869's Plugins menu line so it says the
         menu opens the page browser.
      5. Delete docs/plugin-page-browser-plan.md and docs/plugin-page-browser-plan-manual-steps.html.
    acceptance:
      - grep -rn "plugin-page-browser-plan" . --exclude-dir=.git prints nothing
      - grep -n "0132" docs/adr/README.md docs/adr/0124-*.md shows the index row and the Amended-by line (or the number taken)
      - python -m pytest -q tests/unit/plugins/test_protocol_doc.py passes
```
