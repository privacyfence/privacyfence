# Peek connector plan

## 1. Goal

Add a **Peek** connector. [Peek](https://github.com/andras-tkcs/peek) is a self-hosted server for
sharing HTML pages inside a company and collecting comments on them; the maintainer runs a fork of
it. Through PrivacyFence an AI client can:

- list the Peek pages the connected account manages, straight away;
- read a page's comments and its visit counts after review;
- publish an HTML page, change who can open a page, post a comment, and delete a page, each after
  approval on a card.

People connect with **Peek's own device sign-in** (the flow `peek login` uses): PrivacyFence asks
the Peek server for a sign-in code, the person approves it in Peek in a browser, and PrivacyFence
stores the token Peek issues. In local mode this happens from **Settings > Connectors**; in
organization mode from **/connect**. An organization bundle can pin the Peek server
(`--peek-base-url`); organization mode offers Peek only when it does.

Scope confirmed with the maintainer while planning: read **and** write tools; comments are read and
posted (posting needs one new endpoint in the fork, `manual_before` item `mb1`); Peek's upcoming
view restrictions are **not** part of this plan (a follow-up once the fork's API for them exists);
local and organization mode.

## 2. Current state

- **Peek's API** (fork `andras-tkcs/peek`, `internal/server/routes.go:9-29`). Every token call sends
  `Authorization: Bearer <token>`; errors are JSON `{"error": "<text>"}`
  (`internal/server/api_response.go`). A non-admin token sees and changes only its own pages; an
  admin token sees all (`api_uploads.go:143-157`).

  | Call | Request | Answer |
  |---|---|---|
  | list pages | `GET /api/uploads` | `[{"slug","name","owner","size","visibility","url","created_at"}]` (`created_at` Unix seconds) |
  | upload | `POST /api/upload`, multipart: `file` (with filename), `visibility`, `password` | `{"slug","url","visibility"}`; 413 `file too large` above the server's `max_upload` (2 MiB default) |
  | set visibility | `POST /api/uploads/{slug}/visibility`, JSON `{"visibility","password"}` | `{"visibility"}`; values `public`, `password`, `private`; password ≤ 72 bytes |
  | delete | `DELETE /api/uploads/{slug}` | `{"deleted": slug}` |
  | stats | `GET /api/uploads/{slug}/stats` | `{"slug","name","total_visits","unique_visitors","recent":[{"name","ip","user_agent","visited_at"}]}` |
  | comments | `GET /api/uploads/{slug}/comments` (owner or admin with a token) | `[{"id","selector","element_text","anchor_kind","author","body","created_at"}]` |
  | device sign-in start | `POST /api/cli/login/start` (no auth) | `{"device_code","user_code","verification_url","interval","expires_in"}` (`cli_login.go:17-48`; 15 minutes, interval 2 s) |
  | device sign-in poll | `POST /api/cli/login/poll`, JSON `{"device_code"}` | `{"status": "pending"\|"approved"\|"denied"\|"expired"\|"consumed", "token"?}`; the token comes once, then `consumed` |

  Posting a comment today (`POST /api/uploads/{slug}/comments`, `comments.go:76-150`) has **no token
  path**: it checks only the browser's page access, takes the author name from the body, and on a
  public page accepts anyone as `"anonymous"`. A token sent there is ignored. Slugs are
  `[A-Za-z0-9_-]+` (`internal/objectstore/slug.go`). The raw page HTML is served only to browsers
  holding a view token (`pages.go:84-125`), so the API cannot read a page's content back.
- **Connectors** are `Connector` subclasses (`src/privacyfence/connector.py:78-96`), discovered by
  `connector_catalog.connector_classes()`. The shape template is `connectors/salesforce.py`
  (`_fetch` at `:628-632`, `_auto_audit` at `:634-653`); the review-gated read with tables is
  `connectors/telegram.py` `_get_messages` (`:189-252`); the popup write is
  `connectors/telegram.py` `_send_message` (`:303-325`). The upload template is `connectors/drive.py`
  `_upload_file` (`:1457-1640`): exactly one of `local_path`/`content_base64`/`upload_id`, ADR 0007
  (file bridge), ADR 0028 (upload slots), and `local_files.commit_uploads()` only after the gate
  (ADR 0102, `:1622-1623`).
- **Typed sign-in in Settings**: only Telegram's (`settings_controller.py:1345-1473`,
  `settings_window_html.py:1185-1251` and `:1320-1377`, actions in `web/org_settings_scope.py:126-129`
  and `web/routes_settings.py:257-258`; the org-mode settings snapshot stubs `"telegram_auth"` at
  `web/routes_settings.py:1332`). `_connectors_state` (`settings_controller.py:1734-1767`) reads
  `ORG_CONFIG_SERVICE[cname]` for every connector but Telegram, so a connector outside the bundle
  needs its own `has_org` branch. `ORG_BUNDLE_SERVICES` (`:122`) is not read anywhere.
- **Org mode**: `web/routes_connect.py`: `SERVICE_LABELS` (`:111-115`, also read by
  `web/server.py:346-349`), `_is_connected`/`_is_configured` (`:131-148`), Telegram's per-principal
  state store (`:196-230`) and form routes (`:421-541`), `_telegram_box_html` (`:614-668`),
  `_render_connect_page` (`:671-695`). `tests/unit/web/test_routes_connect.py:170` counts
  `<li class="service card cluster">` rows (11 today).
- **Daemon**: `daemon_main.TOKEN_FILES` (`:169-180`); `build_connectors()` (`:1263-1566`) computes
  `download_mode = org_mode.resolve_mode(org_config)` at `:1294`; `_classify_connector_failure`
  (`:1210-1246`) maps `"Use Authenticate…"` to `not_authenticated` and
  `"organization config not installed"` to `no_org_config`. CLI sign-ins: `run_telegram_setup`
  (`:1797-1811`), flags at `:2091-2100`, dispatch at `:2207-2243`. Token writers are listed in
  `TOKEN_WRITE_SITES` (`tests/unit/test_systemic_gate_invariants.py:177-188`), pinned at 10 by
  `test_ten_token_write_sites_are_listed` (`:248`).
- **Bundle**: `scripts/build_org_bundle.py` (standard library only): one argument group and section
  per service (Salesforce `:188-193`, `:505-511`), the `services` tuple at `:692`. Every option is
  documented in `docs/configuration-reference.md` "Build options" (`:236-254`), enforced by
  `tests/unit/test_docs_configuration_reference.py`.
- **Policy tables**: `auto_accept.TOOL_TO_OPERATION` (`:83`), `TOOL_TO_GATE` (`:176`),
  `policy/registry.TOOL_TO_VERB` (`:141`), `write_effects.EFFECT_BY_TOOL` (`:42`), optional
  `gate._TOOL_LAYOUT` (`:254-296`). `tests/unit/test_connector_tool_annotations.py:29` pins
  `DESTRUCTIVE_TOOLS`.
- **Per-connector bookkeeping enforced by tests**: `CONNECTOR_CLASSES` in
  `tests/unit/connectors/test_readme_manifest_alignment.py:36-40`; `CONNECTOR_TITLES`/`CONNECTOR_SHORT`
  in `scripts/generate_tools_reference.py:44-71` and the generated `docs/tools-reference.md`; the
  generated `docs/always-allow-rules-reference.md`; `scripts/pyinstaller_common.py` hidden imports;
  `tests/unit/test_website_connector_pages.py:27-39` `CONNECTORS` (README row, setup guide in
  `scripts/build_site.py` `CONNECTOR_GUIDES` `:193`, page in `PAGES` `:97-107`);
  `website/_partials/other-connectors.html`; `tests/unit/test_website_connectors_page.py:59-60`
  (tool totals, `len(REFERENCE) == 11`, "Eleven connectors"); `tests/unit/test_website_docs_allowlist.py`
  (every `docs/*.md` listed in `docs/README.md` or `CONTRIBUTOR_DOCS`).
- **QA**: `scripts/qa_fixture_recorder.py` `CONNECTOR_CHECKS` (`:1739`), `EXPECTED_FIXTURES`
  (`:1765`, equal keys asserted at import), `LIFECYCLE_CHECKS`; live credentials exist only on the
  self-hosted runner under `~/privacyfence/` (ADR 0019, `docs/connector-qa.md` "Persistent QA
  state").
- **A parallel plan**: `plan/grist-connector` (not merged) adds a connector of the same shape and
  touches the same files and website counts, reserves ADRs 0142-0145, and renames
  `_check_telegram_post` to `_check_form_post` in `web/routes_connect.py`. This plan takes ADRs
  **0146-0149** and is written so it works whichever of the two merges first (§6).

## 3. Design

### 3.0 Rules for every phase

- No project history in code, comments, docstrings, user-visible strings or standing docs: no phase
  ids, plan names, "Phase N", bare issue numbers or "as of" phrasing
  (`tests/unit/test_code_no_history.py`, `tests/unit/test_docs_no_history.py`, ADR 0056). Cite an ADR
  number for a reason instead.
- Every `gated_call(...)` in `connectors/peek.py` passes its tool name and gate as string literals
  inline at the call site (`tool="peek_get_comments"`, `gate="review"`):
  `test_readme_manifest_alignment.py`, `test_write_effects.py` and `test_systemic_gate_invariants.py`
  read the source.
- This plan (`peek-connector-plan.md`) is listed in `scripts/build_site.py` `CONTRIBUTOR_DOCS` and in
  `docs/README.md`'s contributor half while the work is open (the plan branch's own commit did that);
  the last phase removes both entries.
- Nothing logs, audits, puts in a snapshot, an error message or a preview: a Peek token, a device
  code, a page password, page content or a comment body. Log the host and counts only.
- Every phase ends with `ruff check .` and `python3 -m pytest tests/unit -q` passing in full.

### 3.1 The fork change PrivacyFence needs (done by the maintainer, `mb1`)

One new route in `andras-tkcs/peek`, `internal/server/routes.go`:

```go
mux.HandleFunc("POST /api/uploads/{slug}/account-comments", s.authToken(s.handleAddAccountComment))
```

`handleAddAccountComment`, in the same file as `handleAddComment` (`comments.go`):

- the upload must exist (404 `not found`); the token's account must own it or be an admin
  (403 `not owner`), the same rule as the token path of `handleListComments`;
- body JSON `{"body", "selector", "element_text", "anchor_kind"}` (any `name` is ignored), with the
  same trimming, `normalizeCommentAnchorKind`, required body and length limits as `handleAddComment`;
- the author is the token's account name (`owner.Name`); the visitor hash is computed from the
  account id instead of a visitor cookie;
- the same comment rate limiter;
- an audit line `comment.create` with the slug, like the other token routes;
- answer 200 with the **new comment only**, as `commentOut`
  (`{"id","selector","element_text","anchor_kind","author","body","created_at"}`).

The browser route `POST /api/uploads/{slug}/comments` is not changed. A separate route, not a token
branch inside the old one, is deliberate: on a Peek server without it, PrivacyFence's call fails
(404 or 405) instead of the old route silently posting the comment as `"anonymous"` on a public
page (ADR 0148).

### 3.2 `src/privacyfence/peek_client.py`

Module docstring: the Peek REST client and Peek's device sign-in; a token is only ever sent to the
server it was issued for (ADR 0147); redirects are never followed; nothing logs a token, a device
code, a password, page content or a comment body.

- `class PeekClientError(Exception)`.
- Constants: `REQUEST_TIMEOUT_SECONDS = 30`; `VISIBILITIES = ("public", "password", "private")`;
  `PASSWORD_MAX_BYTES = 72`; `UPLOAD_MAX_BYTES = 10 * 1024 * 1024` (PrivacyFence's own ceiling on a
  page it reads; the server's own `max_upload`, 2 MiB by default, still applies);
  `_SLUG_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")`.
- `normalize_base_url(raw: str) -> str`: strip whitespace and trailing `/`; `urllib.parse.urlsplit`.
  Every rejection raises `PeekClientError`. Messages (exact): empty or no host →
  `"Enter the Peek server address, such as https://peek.example.com."`; a scheme other than `https`,
  except `http` with host `localhost`/`127.0.0.1`/`::1` →
  `"The Peek server address must start with https:// (http:// is allowed only for localhost)."`;
  userinfo, query or fragment →
  `"The Peek server address must not contain a user name, password, query or fragment."`. A path
  prefix is kept. Returns `scheme://netloc[/path]` with scheme and host lower-cased.
- `validate_slug(slug: str) -> str`: `_SLUG_RE.fullmatch`, else
  `PeekClientError(f"Not a Peek page slug: {slug!r}")`. Every method that puts a slug in a path calls
  it first.
- `bundle_base_url(section: dict[str, Any], *, org_mode: bool) -> str`: `section.get("base_url")`
  normalized, or `""` when absent or empty; a value `normalize_base_url` rejects →
  `PeekClientError(f"Peek organization config is not usable: {exc}")`; `org_mode` and `""` →
  `PeekClientError("Peek organization config not installed")`.

**HTTP.** `requests` (already a dependency), every call with `timeout=REQUEST_TIMEOUT_SECONDS`,
`allow_redirects=False`, TLS verification on (never `verify=False`). One module function
`_send(session: requests.Session, method: str, url: str, host: str, **kwargs: Any) -> requests.Response`
does the request and the error mapping for every call (sign-in and client alike). Errors (exact):

- `requests.RequestException` → `f"Could not reach Peek at {host}: {type(exc).__name__}"`
- 3xx → `f"Peek answered with a redirect (HTTP {status}). Check the Peek server address."`
- 401 → `"Peek refused the saved sign-in (HTTP 401). Use Authenticate… in PrivacyFence Settings to connect again."`
- 403 → `f"Peek refused the request (HTTP 403): {detail}"`
- 404 → `"Peek found no such page (HTTP 404). Call peek_list_pages to see the pages you can manage."`
- other non-2xx → `f"Peek API error (HTTP {status}): {detail}"`
- a 2xx whose body is not JSON → `f"Peek answered with something other than JSON (HTTP {status}). Check the Peek server address."`

`detail` is the JSON `error` string cut to 200 characters, else `"no detail"`. `host` is the URL's
`netloc`.

**Device sign-in.**

- `@dataclass(frozen=True) class PeekDeviceLogin`: `device_code: str = field(repr=False)`,
  `user_code: str`, `verification_url: str`, `interval: int`, `expires_at: float`
  (`time.time() + expires_in`).
- `start_device_login(base_url: str) -> PeekDeviceLogin`: `POST {base_url}/api/cli/login/start` with
  `json={}`. A missing key →
  `PeekClientError("Peek answered the sign-in request without a sign-in code.")`. A
  `verification_url` that does not start with `base_url + "/"` →
  `PeekClientError("Peek answered with a sign-in address on another server. Check the Peek server address.")`.
  `interval` below 1 becomes 2.
- `@dataclass(frozen=True) class PeekLoginPoll`: `status: str`, `token: str = field(default="", repr=False)`.
- `poll_device_login(base_url: str, device_code: str) -> PeekLoginPoll`:
  `POST {base_url}/api/cli/login/poll` with `json={"device_code": device_code}`. A status outside
  `pending, approved, denied, expired, consumed`, or `approved` without a non-empty `token` →
  `PeekClientError("Peek answered the sign-in check with something unexpected.")`.

**Credential file** `credentials/peek_token.json`, one JSON object `{"base_url": <normalized>, "token": <str>}`.

- `save_token_file(path: str, base_url: str, token: str) -> None` → `secure_files.atomic_write_json`
  (the single token-write site).
- `load_token_file(path: str) -> dict[str, str]`: missing file →
  `PeekClientError("Peek is not authenticated. Use Authenticate… in PrivacyFence Settings.")`;
  not JSON, not an object, a missing or empty `base_url`/`token`, or a `base_url` that
  `normalize_base_url` rejects →
  `PeekClientError("Peek's saved sign-in could not be read. Use Authenticate… in PrivacyFence Settings to connect again.")`.
  Returns `{"base_url": <normalized>, "token": ...}`.
- `resolve_server(pinned_base_url: str, record: dict[str, str]) -> str`: when `pinned_base_url` is
  non-empty and differs from `record["base_url"]` →
  `PeekClientError("Peek was connected to a different server than your organization uses. Use Authenticate… in PrivacyFence Settings to connect again.")`;
  otherwise returns `record["base_url"]`.

**Dataclasses** (all fields typed; times are ISO 8601 UTC strings, `"2026-10-10T11:07:00Z"`, from
Peek's Unix seconds):

- `PeekPage(slug, name, owner, size_bytes: int, visibility, url, created_at)`.
- `PeekComment(id: int, author, body, anchor_kind, selector, element_text, created_at)`.
- `PeekVisit(visitor_name, visited_at)`. Docstring: Peek also returns each visit's IP address and
  user agent; they are deliberately never carried (ADR 0149).
- `PeekStats(slug, name, total_visits: int, unique_visitors: int, recent: list[PeekVisit] = field(default_factory=list))`.
- `PeekUploadResult(slug, url, visibility)`.

Missing optional string keys parse as `""` and missing numbers as `0`.

**`class PeekClient(base_url: str, token: str)`**: keeps one `requests.Session`; `base_url` and `host`
properties; `__repr__` shows the host only. `_request(self, method: str, path: str, **kwargs: Any) -> Any`
is the single choke point: adds `Authorization: Bearer <token>`, calls `_send`, returns parsed JSON.
Methods:

| Method | HTTP | Notes |
|---|---|---|
| `check_connection() -> str` | `GET /api/uploads` | returns `host` |
| `list_pages() -> list[PeekPage]` | `GET /api/uploads` | sorted newest `created_at` first |
| `get_page(slug) -> PeekPage` | via `list_pages` | not listed → `PeekClientError(f"No Peek page {slug!r} among the pages this account can manage. Call peek_list_pages to see them.")` |
| `get_comments(slug) -> list[PeekComment]` | `GET /api/uploads/{slug}/comments` | sorted by `(created_at, id)` |
| `get_stats(slug) -> PeekStats` | `GET /api/uploads/{slug}/stats` | drops `ip` and `user_agent` |
| `upload_page(data: bytes, name: str, visibility: str, password: str) -> PeekUploadResult` | `POST /api/upload` multipart: `files={"file": (name, data, "text/html")}`, `data={"visibility": visibility, "password": password}` (`password` only for `password` visibility) | |
| `set_visibility(slug, visibility, password) -> str` | `POST /api/uploads/{slug}/visibility` JSON | returns the new visibility |
| `delete_page(slug) -> None` | `DELETE /api/uploads/{slug}` | |
| `add_comment(slug, body, selector, element_text) -> PeekComment` | `POST /api/uploads/{slug}/account-comments` JSON `{"body","selector","element_text"}` | a 404 or 405 → `PeekClientError("This Peek server does not accept comments from PrivacyFence: it has no account comment endpoint. See the Peek setup guide.")` |

`upload_page` and `set_visibility` check, before any request: visibility in `VISIBILITIES`, else
`PeekClientError("visibility must be public, password or private.")`; a password for `password`
visibility and none otherwise, and at most 72 UTF-8 bytes, else
`PeekClientError("A password of 1 to 72 bytes goes with password visibility, and only with it.")`.

### 3.3 `src/privacyfence/connectors/peek.py`

`class PeekConnector(Connector)`, `name == "peek"`, `__init__(self, client: PeekClient, download_mode: str = "local")`,
a `client` property. `_auto_audit` copied from `connectors/salesforce.py:634-653`. `_fetch` widened to
keyword arguments: `async def _fetch(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any`
runs `await asyncio.to_thread(func, *args, **kwargs)` and re-raises `PeekClientError` as
`RuntimeError(str(exc)) from exc` after `logger.warning`. Argument validation raises `ValueError`
before anything is fetched or gated. `call()` raises `ValueError(f"Unknown Peek tool: {tool!r}")`
for an unknown name.

Tools (each ends with `ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")`):

| Tool | Gate | `read_only` | `destructive` | Params (besides `reason`) | Returns |
|---|---|---|---|---|---|
| `peek_list_pages` | auto | True | False | `max_results` (int, default 50) | `[{"slug","name","owner","size_bytes","visibility","url","created_at"}]` |
| `peek_get_comments` | review | True | False | `slug` | `{"slug","name","comments":[{"id","author","body","anchor_kind","selector","element_text","created_at"}]}` |
| `peek_get_stats` | review | True | False | `slug` | `{"slug","name","total_visits","unique_visitors","recent":[{"visitor_name","visited_at"}]}` |
| `peek_upload_page` | popup | False | False | `html`, `local_path`, `upload_id`, `name` (str, all default `""`), `visibility` (default `"private"`), `password` (default `""`) | `{"slug","url","visibility"}` |
| `peek_set_visibility` | popup | False | False | `slug`, `visibility`, `password` (default `""`) | `{"slug","visibility"}` |
| `peek_add_comment` | popup | False | False | `slug`, `body`, `selector` (default `""`), `element_text` (default `""`) | `{"slug","comment":{"id","author","body","anchor_kind","selector","element_text","created_at"}}` |
| `peek_delete_page` | popup | False | True | `slug` | `{"deleted": slug}` |

Descriptions follow ADR 0115 (`assert_tool_definitions_complete`): this first sentence, then a
`Returns` sentence for the shape above, the sibling tools named, and `"Auto-approved."` or
`"Requires user approval."` at the end:

- `peek_list_pages`: "List the Peek pages this account can manage, newest first, with each page's link and who can open it." (an admin account sees every page)
- `peek_get_comments`: "Read the reviewers' comments on one Peek page, with what each comment is anchored to."
- `peek_get_stats`: "Read how often one Peek page was opened, and the names visitors gave." (add: "IP addresses and browsers are never returned.")
- `peek_upload_page`: "Publish an HTML page on Peek and get its share link." (add: "Give the page as html, local_path or upload_id; a page is private unless you choose otherwise.")
- `peek_set_visibility`: "Change who can open a Peek page: anyone with the link, anyone with the link and a password, or signed-in Peek accounts only."
- `peek_add_comment`: "Post a comment on a Peek page under your own Peek account name." (add: "Copy selector and element_text from a comment returned by peek_get_comments to reply at the same spot.")
- `peek_delete_page`: "Delete a Peek page, with its comments and visit history."

Parameter descriptions (exact):

- `max_results`: "Most pages to return, newest first. Default 50, 1 to 200."
- `slug`: "The page's slug: the slug field from peek_list_pages, or the last part of its link (…/p/<slug>)."
- `html`: "The page's full HTML source. Give exactly one of html, local_path and upload_id; empty otherwise."
- `local_path`: "Path of an HTML file on the user's computer, absolute or starting with ~/. Not available on an organization-managed install. Give exactly one of html, local_path and upload_id; empty otherwise."
- `upload_id`: "Id returned by privacyfence_create_upload_slot after you PUT the file's bytes to its upload_url. Give exactly one of html, local_path and upload_id; empty otherwise."
- `name`: "File name Peek shows for the page and builds its link from, such as report.html. Empty uses the local file's name, or page.html."
- `visibility` (both tools): "Who can open the page: public (anyone with the link), password (anyone with the link and the password) or private (signed-in Peek accounts only)." plus " Default private." on `peek_upload_page`.
- `password`: "The page password, 1 to 72 bytes. Required with password visibility and empty otherwise. It goes to Peek and is never shown on the approval card."
- `body`: "The comment text, 1 to 4000 characters."
- `selector`: "CSS selector of the element to pin the comment to, copied from a comment's selector in peek_get_comments. Empty comments on the whole page."
- `element_text`: "Quoted text to anchor the comment to, up to 200 characters, copied from a comment's element_text. Needs selector. Empty anchors to the element, or the page."

Siblings map `PEEK_SIBLINGS` for `TestToolDefinitions` (each description names these):
`peek_list_pages`→(`peek_get_comments`, `peek_get_stats`); `peek_get_comments`→(`peek_add_comment`,);
`peek_get_stats`→(`peek_get_comments`,); `peek_upload_page`→(`peek_set_visibility`,);
`peek_set_visibility`→(`peek_upload_page`,); `peek_add_comment`→(`peek_get_comments`,);
`peek_delete_page`→(`peek_list_pages`,).

Validation (`ValueError`, exact, before any fetch or gate):

- `max_results` outside 1-200 → `"max_results must be between 1 and 200."`
- a bad slug → `f"Not a Peek page slug: {slug!r}"` (call `peek_client.validate_slug`, turn its error into `ValueError`).
- `peek_upload_page`: not exactly one non-empty source → `"peek_upload_page: give exactly one of html, local_path and upload_id."`;
  `local_path` when `self.download_mode == "org"` → `"local_path is not available on an organization-managed install. Pass html or upload_id instead."`;
  content bytes empty → `"peek_upload_page: the page is empty."`; not valid UTF-8 →
  `"peek_upload_page: the page is not UTF-8 text. Peek publishes HTML pages only."`.
- visibility (both tools) → `"visibility must be public, password or private."`; password missing
  with `password` → `"A password is required when visibility is password."`; given otherwise →
  `"password must be empty unless visibility is password."`; over 72 UTF-8 bytes →
  `"password must be 72 bytes or fewer."`.
- `peek_add_comment`: stripped `body` empty or over 4000 characters → `"body must be 1 to 4000 characters."`;
  `selector` over 500 → `"selector must be 500 characters or fewer."`; `element_text` over 200 →
  `"element_text must be 200 characters or fewer."`; `element_text` without `selector` →
  `"element_text needs a selector."`.

**Upload source handling** copies `connectors/drive.py` `_upload_file` (`:1457-1640`) for
`local_path`/`upload_id`: `local_files.require_local_files([path], max_total_bytes=UPLOAD_MAX_BYTES, download_mode=self.download_mode)`,
then `local_files.read_local_file(...)`; `upload_id` becomes `f"{local_files.UPLOAD_REF_PREFIX}{upload_id.strip()}"`.
`html` is `html.encode("utf-8")`, over `UPLOAD_MAX_BYTES` → `"peek_upload_page: the page is larger than 10 MB."`.
`name` = `os.path.basename(name.strip())`, else `os.path.basename(local_path)` when given, else
`"page.html"`. After `gated_call` returns: `local_files.commit_uploads()` (ADR 0102), then the upload.

Anchor text helper `_anchor_text(anchor_kind, selector, element_text) -> str`: `"page"` →
`"Whole page"`; `"element"` → `f"Element {selector}"`; `"text"` → `f"Text “{element_text}”"`.

`gated_call` arguments (keyword, `connector=self.name`). Every `preview` starts with
`"Server": self._client.host`, so the card names the server (ADR 0147); the dicts are listed without it.
Each tool first fetches what it shows (through `_fetch`), then gates, then (for writes) writes.

- `peek_get_comments` (fetch `get_page`, `get_comments`): `tool_name="Read Peek Comments"`,
  `summary=f"{n} comment(s) on {page.name}"`, `sender=page.name`, `raw_data=comments`,
  `filtered_data=<the return dict>`, `gate="review"`,
  `preview={"Page": page.name, "Link": page.url}`,
  `new_info={"Comments": str(n), "Comment text": "Author, text and anchor of every comment"}`,
  `details_text` one line per comment `f"[{c.created_at}] {c.author} ({_anchor_text(...)}): {c.body}"` or `"(no comments)"`,
  `pii_scan_text` = every body and element_text joined by newlines,
  `preview_tables=[{"headers": ["Author", "Date", "Anchored to", "Comment"], "rows": [...]}]` (omitted when there are none; dates through `preview_dates.format_preview_datetime`),
  `table_only=True`, `args={"slug": slug}`.
- `peek_get_stats` (fetch `get_page`, `get_stats`): `tool_name="Read Peek Visits"`,
  `summary=f"{stats.total_visits} visit(s) to {page.name}"`, `sender=page.name`, `raw_data=stats`,
  `filtered_data=<the return dict>`, `gate="review"`, `preview={"Page": page.name, "Link": page.url}`,
  `new_info={"Visits": str(total), "Unique visitors": str(unique), "Recent visitors": "Name each visitor gave, and when (no IP address or browser)"}`,
  `details_text` one line per visit `f"{v.visited_at} {v.visitor_name or '(no name given)'}"` or `"(no visits)"`,
  `pii_scan_text` = visitor names joined by newlines,
  `preview_tables=[{"headers": ["Visitor", "Time"], "rows": [...]}]` (omitted when empty), `table_only=True`,
  `args={"slug": slug}`.
- `peek_upload_page`: `tool_name="Publish Peek Page"`, `summary=f"Publish {name} ({visibility})"`,
  `sender=name`, `raw_data={"name": name, "visibility": visibility, "size_bytes": len(data)}`,
  `filtered_data=None`, `gate="popup"`,
  `preview={"Name": name, "Visibility": visibility, "Size": f"{len(data):,} bytes"}` plus
  `"Password": "Set (not shown)"` for password visibility,
  `details_text=html_to_text.html_to_text(text) or "(the page has no visible text)"`,
  `args={"name": name, "visibility": visibility}`. Never the password or the HTML in `args` or `raw_data`.
- `peek_set_visibility` (fetch `get_page`): `tool_name="Change Peek Page Visibility"`,
  `summary=f"{page.name}: {page.visibility} → {visibility}"`, `sender=page.name`,
  `raw_data={"slug": slug, "visibility": visibility}`, `filtered_data=None`, `gate="popup"`,
  `preview={"Page": page.name, "Link": page.url, "Visibility": f"{page.visibility} → {visibility}"}`
  plus `"Password": "Set (not shown)"` for password visibility,
  `details_text=f"Who can open {page.url} changes from {page.visibility} to {visibility}."`,
  `args={"slug": slug, "visibility": visibility}`.
- `peek_add_comment` (fetch `get_page`): `tool_name="Comment on Peek Page"`,
  `summary=f"Comment on {page.name}: {body[:80]}{'…' if len(body) > 80 else ''}"`, `sender=page.name`,
  `raw_data={"slug": slug, "body": body}`, `filtered_data=None`, `gate="popup"`,
  `preview={"Page": page.name, "Link": page.url, "Anchored to": _anchor_text(kind, selector, element_text)}`
  (kind: `"text"` with element_text, `"element"` with only a selector, else `"page"`),
  `details_text=body`, `args={"slug": slug}`.
- `peek_delete_page` (fetch `get_page`): `tool_name="Delete Peek Page"`, `summary=f"Delete {page.name}"`,
  `sender=page.name`, `raw_data={"slug": slug}`, `filtered_data=None`, `gate="popup"`,
  `preview={"Page": page.name, "Link": page.url, "Visibility": page.visibility, "Created": page.created_at}`,
  `details_text=f"{page.name} ({page.url}) is deleted with its comments and visit history."`,
  `args={"slug": slug}`.

`peek_list_pages` audits with
`_auto_audit("peek_list_pages", "List Peek Pages", f"List pages (max {max_results})", f"{n} page(s)", t0)`.

### 3.4 Policy tables

| Tool | `TOOL_TO_GATE` | `TOOL_TO_OPERATION` | `TOOL_TO_VERB` | `EFFECT_BY_TOOL` | `_TOOL_LAYOUT` |
|---|---|---|---|---|---|
| `peek_list_pages` | `auto` | — | — | — | — |
| `peek_get_comments` | `review` | `peek.read_comments` | `READ` | — | `WIDE` |
| `peek_get_stats` | `review` | `peek.read_stats` | `READ` | — | `WIDE` |
| `peek_upload_page` | `popup` | `peek.upload_page` | `CREATE` | `"The page is published at a new link. Who can open it follows the visibility shown."` | `WIDE` |
| `peek_set_visibility` | `popup` | `peek.set_visibility` | `SHARE` | `"Who can open the page changes. Its content and comments do not change."` | — |
| `peek_add_comment` | `popup` | `peek.add_comment` | `COMMENT` | `"The comment is posted on the page under your Peek account name. PrivacyFence cannot delete it."` | `WIDE` |
| `peek_delete_page` | `popup` | `peek.delete_page` | `DELETE` | `"The page, its comments and its visit history are deleted and cannot be restored."` | — |

No Peek-specific rule scope: Peek gets no entry in `policy/scopes.py`, `policy/propose.py`,
`policy/catalogue.py` or `policy/resource_registry.py`; rules use only what every connector already
has. A per-page scope is a follow-up (§3.7).

### 3.5 Local mode: daemon, CLI and Settings

- `daemon_main.TOKEN_FILES["peek"] = "credentials/peek_token.json"`. Imports:
  `from .connectors.peek import PeekConnector` and from `.peek_client`: `PeekClient`,
  `PeekClientError`, `bundle_base_url as peek_bundle_base_url`, `load_token_file as load_peek_token`,
  `resolve_server as peek_resolve_server`, `normalize_base_url as peek_normalize_base_url`,
  `start_device_login as peek_start_device_login`, `poll_device_login as peek_poll_device_login`,
  `save_token_file as save_peek_token`.
- `build_connectors()`, after the Telegram block, reusing the `download_mode` it computes at `:1294`:
  ```python
  if enabled("peek"):
      try:
          pinned = peek_bundle_base_url(org_config.get("peek") or {}, org_mode=download_mode == "org")
          record = load_peek_token(_resolve_path(TOKEN_FILES["peek"]))
          client = PeekClient(peek_resolve_server(pinned, record), record["token"])
          client.check_connection()
          logger.info("Peek connector ready for %s", client.host)
          connectors.append(PeekConnector(client, download_mode=download_mode))
      except PeekClientError as exc:
          logger.warning("Peek connector disabled: %s", exc)
          failures["peek"] = _classify_connector_failure(exc)
  ```
- `run_peek_login(org_config: dict[str, Any]) -> int` and a `--peek-login` flag (help: "Sign in to
  Peek with Peek's device sign-in and save the token."), dispatched like `--telegram-setup`
  (`:2207-2243`, it takes `org_config`). Steps, every message exact:
  1. `pinned = peek_bundle_base_url(org_config.get("peek") or {}, org_mode=False)`; a
     `PeekClientError` → print `str(exc)` to stderr, return 1.
  2. `base = pinned or peek_normalize_base_url(input("Peek server address: "))`.
  3. `login = peek_start_device_login(base)`; print
     `f"Open {login.verification_url} and approve the code {login.user_code}."`.
  4. Loop: `time.sleep(login.interval)`; when `time.time() > login.expires_at` print
     `"The Peek sign-in code expired. Run --peek-login again."` to stderr, return 1;
     `poll = peek_poll_device_login(base, login.device_code)`: `approved` →
     `save_peek_token(_resolve_path(TOKEN_FILES["peek"]), base, poll.token)`, print
     `f"Peek sign-in complete. Connected to {urlsplit(base).netloc}."`, return 0; `denied` → print
     `"The Peek sign-in was denied."` to stderr, return 1; `expired`/`consumed` → the expired message,
     return 1; `pending` → continue.
  5. Any `PeekClientError` in 2-4 → print `f"Peek sign-in failed: {exc}"` to stderr, return 1.
  Add `--peek-login` to the "is any auth flag set" check (`:2208-2211`) and to
  `test_oauth_and_telegram_flags_do_not_trigger_the_separation_gate`'s parameters.
- `settings_controller.py`: `"peek"` appended to `ALL_CONNECTORS`; `_connectors_state` sets
  `has_org = True` for `"peek"` (no bundle needed in local mode). Peek is not added to
  `ORG_CONFIG_SERVICE` or `ORG_BUNDLE_SERVICES`; `connector_label("peek")` is already `"Peek"`;
  `authenticate_connector` is unchanged (the row never calls it, as Telegram's does not).
- `settings_controller.py`, sign-in actions. `self._peek_auth: dict[str, Any] | None = None` next to
  `_telegram_auth`. Helper `_peek_pinned(self) -> tuple[str, str]` returns
  `(peek_bundle_base_url(self._org_config_or_empty().get("peek") or {}, org_mode=False), "")`, or
  `("", str(exc))` on `PeekClientError`. `snapshot()` gains
  `"peek_auth": {"step": None | "approve", "error": str, "user_code": str, "verification_url": str, "pinned_base_url": str}`
  (`pinned_base_url` from the helper; `error` is the pending error, else the helper's error, else
  `""`). The device code and the token are never in the snapshot or on `self` outside `_peek_auth`'s
  `device_code`. Actions:
  - `peek_start_login(self, base_url: str = "") -> dict[str, Any]`: helper error →
    `_peek_auth = {"step": None, "error": <it>}`, return the snapshot. `url = pinned or peek_normalize_base_url(base_url)`;
    a `PeekClientError` → `_peek_auth = {"step": None, "error": str(exc)}`. Otherwise mark `"peek"`
    busy and `_run_async` a worker that runs `peek_start_device_login(url)` and then
    `webbrowser.open(login.verification_url)`, returning the login. Done: success →
    `_peek_auth = {"step": "approve", "error": "", "base_url": url, "device_code": ..., "user_code": ..., "verification_url": ..., "expires_at": ...}`;
    failure → `{"step": None, "error": str(result)}`; un-busy; `_push_snapshot()`.
  - `peek_finish_login(self) -> dict[str, Any]`: no `approve` step → return the snapshot. Past
    `expires_at` → `_peek_auth = {"step": None, "error": "The sign-in code expired. Start again."}`.
    Otherwise busy, and a worker that polls once and, on `approved`, calls
    `save_peek_token(str(data_dir() / TOKEN_FILES["peek"]), base_url, poll.token)`, returning the
    status. Done: `approved` → `_peek_auth = None`, `self.error = ""`, `refresh_connectors()`;
    `pending` → keep the step, error
    `"Peek has not recorded your approval yet. Approve the code in Peek, then choose Done again."`;
    `denied` → `{"step": None, "error": "The sign-in was denied in Peek."}`; `expired`/`consumed` →
    `{"step": None, "error": "The sign-in code expired. Start again."}`; an exception → keep the
    step, error `str(result)`.
  - `peek_cancel_login(self) -> dict[str, Any]`: `_peek_auth = None`, return the snapshot.
- `web/org_settings_scope.py`: the three actions as `ActionScope(modes=frozenset({LOCAL_MODE}))` next
  to the Telegram ones. `web/routes_settings.py`: all three in `_NON_SENSITIVE_ACTIONS` (connector
  sign-in stays ungated, as the module docstring says; enabling a connector stays sensitive, ADR
  0070), and the org-mode snapshot stub (`:1332`) gains
  `"peek_auth": {"step": None, "error": "", "user_code": "", "verification_url": "", "pinned_base_url": ""}`.
- `settings_window_html.py`: the Peek row gets `data-peek-auth="1"` (as Telegram's
  `data-telegram-auth`), opening a modal that reuses Telegram's modal classes. It reads
  `state.peek_auth || {step: null, error: ''}`.
  - Step `null`: title "Connect Peek". With `pinned_base_url`: the line
    "Your organization uses <pinned_base_url>." and no input. Without: the line "The address of your
    Peek server." and `<input type="url" class="field pf-modal-input" data-peek-field="base_url" placeholder="https://peek.example.com">`.
    Buttons **Cancel** (`data-peek-cancel`, posts `peek_cancel_login`) and **Continue**
    (`data-peek-submit="peek_start_login"`, posts `{base_url: <input value or "">}`).
  - Step `approve`: title "Approve in Peek"; the line "A browser window opened on the machine running
    PrivacyFence. Sign in to Peek there and approve this code:"; the code in
    `<div class="pf-modal-code">`; the line `"Or open " + verification_url` (text, not a link);
    buttons **Cancel** and **Done** (`data-peek-submit="peek_finish_login"`, posts `{}`).
  - While the row is busy the primary button reads "Working…" and is `aria-disabled`. `peek_auth.error`
    shows in `pf-modal-error`. The modal closes itself like Telegram's (`settings_window_html.py:1240-1251`,
    with its own `ui.peekModalOpen`/`ui.peekAuthWasActive`) when the step returns to `null` with no
    error after a submit. `.pf-modal-code` is one new rule in the page's CSS using existing tokens
    (monospace, `--step-1` size, letter spacing); no colour literal (§1.10 of the coding guidelines).

### 3.6 Organization mode

- **Bundle section** `peek` = `{"base_url": str}`, written by `scripts/build_org_bundle.py`: a "Peek"
  argument group with `--peek-base-url URL`. Standard library only: strip a trailing `/`; reject
  `SystemExit("--peek-base-url must be an https:// address (http:// only for localhost).")` for an
  empty host or a scheme other than `https` (except `http` with host `localhost`, `127.0.0.1` or
  `::1`), and `SystemExit("--peek-base-url must not contain a user name, password, query or fragment.")`.
  `"peek"` joins the `services` tuple (`:692`). No `_CONNECTOR_CALLBACKS` entry (no OAuth callback).
  `docs/configuration-reference.md` "Build options" row (after the Atlassian rows):
  `| \`--peek-base-url URL\` | none | \`peek.base_url\` | The Peek server people connect to. Pins it in local mode; organization mode offers Peek only with it. |`.
- **`web/routes_connect.py`**:
  - `SERVICE_LABELS["peek"] = "Peek"`. Peek is not in `OAUTH_SERVICES`, `_GRANT_KEY` or
    `_ORG_CONFIG_SECTION`.
  - `_is_configured(org_config, "peek")`: `bundle_base_url(org_config.get("peek") or {}, org_mode=True)`
    succeeds. `_is_connected(principal, "peek")` (it gains an `org_config` parameter only if needed;
    otherwise a separate `_peek_connected(principal, org_config) -> bool`): load
    `paths.user_dir(principal) / TOKEN_FILES["peek"]` with `load_token_file` and `resolve_server`
    against the bundle's base URL; any `PeekClientError` means not connected.
  - `_PeekState` dataclass (`step: str | None = None` (`None` or `"approve"`), `base_url`,
    `device_code`, `user_code`, `verification_url`, `expires_at: float = 0.0`, `error`,
    `created_at`) and `_PeekAuthStore`, a copy of `_TelegramAuthStore` with a 15-minute TTL;
    `peek_states` created where `telegram_states` is.
  - CSRF and origin checks through the Telegram helper renamed from `_check_telegram_post` to
    `_check_form_post` (if `_check_form_post` already exists when this phase starts, use it).
  - `POST /connect/peek/start`: signed out → `_signed_out_redirect()`; not configured → state error
    `"Peek is not set up by your organization."`; otherwise
    `await asyncio.to_thread(start_device_login, base_url)` → state `approve` with the login's
    fields; `PeekClientError` → state error `str(exc)`.
  - `POST /connect/peek/finish`: no `approve` step → redirect; past `expires_at` → reset with
    `"The sign-in code expired. Start again."`; otherwise `await asyncio.to_thread(poll_device_login, ...)`:
    `approved` → `save_token_file(str(paths.user_dir(principal) / TOKEN_FILES["peek"]), base_url, token)`,
    clear the state, `connector_registry.evict(principal.id)`; `pending` → error
    `"Peek has not recorded your approval yet. Approve the code in Peek, then choose Done."`;
    `denied` → reset with `"The sign-in was denied in Peek."`; `expired`/`consumed` → reset with the
    expired message; `PeekClientError` → error `str(exc)`, step kept.
  - `POST /connect/peek/cancel`: clear the state.
  - Every handler ends with `RedirectResponse("/connect", status_code=303, headers={"Cache-Control": "no-store"})`.
    Routes added after the Telegram ones.
  - `_peek_box_html(principal, org_config, peek_state, csrf) -> str`, rendered after the Telegram box
    in `_render_connect_page`: not configured → the generic row
    (`<li class="service card cluster">` with "Not set up by your organization", so the
    `test_routes_connect.py:170` count goes up by one). Configured →
    `<li class="service peek card stack">` with the head chip "Peek" and the Connected / Not connected
    badge, the error (`card card-danger`, `role="alert"`), and: step `None` → "Your organization uses
    <base_url>." and a form `POST /connect/peek/start` with the CSRF field and **Connect Peek**
    (**Reconnect Peek** when connected); step `approve` → "Open Peek, sign in and approve this code:",
    the code in `<strong class="pf-peek-code">`, a link
    `<a class="button secondary" href="<verification_url>" target="_blank" rel="noopener noreferrer">Open Peek</a>`,
    a form `POST /connect/peek/finish` with **Done**, and a Cancel form (the Telegram box's `form=`
    pattern). All values through `_esc`. `.pf-peek-code` goes in the module's `_STYLE` with tokens only.
- Per-person token files sit under `paths.user_dir(principal)` with the other per-person
  third-party credentials.

### 3.7 What is deliberately not built

- View restrictions (the fork will add them; a follow-up plan adds them to `peek_set_visibility`).
- No "delete all my pages" and no export tool; no tool reads a page's HTML back (Peek's API has none).
- No visitor IP address or user agent reaches the AI client (ADR 0149).
- No pasted token and no CLI-config import as a sign-in path (ADR 0146).
- No Peek-specific auto-accept scope (a per-page `peek.page` scope offered from Settings, the Grist
  `grist.document` shape, is a follow-up).
- No connector icon (only real brand assets go in `resources/connector_icons/`).

### 3.8 Setup guide `peek-setup.md` (in `docs/`)

Modelled on `docs/telegram-setup.md`'s template. Sections: `# Peek setup`; `## What you need` (a Peek
server you can sign in to; for `peek_add_comment`, a server with the account comment endpoint
(§3.1 contract in prose: route, who may post, author name); every other tool works with any Peek
server); `## Users connect` (local: **Settings > Connectors > Peek > Authenticate…**, the server
address (or the organization's), approve the code in the browser that opens, **Done**; the CLI
alternative `privacyfence-app --peek-login`; organization mode: the connections page, **Connect
Peek**, **Open Peek**, approve, **Done**); `## Values` (table: the Peek server address, its
`build_org_bundle.py` option); `## Build and distribute the bundle` (`--peek-base-url` pins the server
in local mode; organization mode needs it); `## What the assistant can do` (the seven tools and their
gates; link the tools reference `#peek`); `## Privacy` (stats carry visitor names and times only,
never IP addresses or browsers; a page password is sent to Peek and never shown on the card or kept
by PrivacyFence; a token is only sent to the server it was issued for); `## Troubleshooting` (the
exact error texts from §3.2 and §3.5, each with what to do).

## 4. ADRs

- **0146** — PrivacyFence signs in to Peek with Peek's own device sign-in (`/api/cli/login/start` and
  `/poll`), from Settings, `/connect` and `--peek-login`, and keeps the token per principal in
  `credentials/peek_token.json` (`atomic_write_json`, 0600), never in `settings.yaml`, the settings
  snapshot, a log line or the audit log. Rejected: pasting a token (on Peek servers that use SSO only
  an admin can mint one, and a typed token is easy to leak); reading the Peek CLI's own config file
  (another program's credential store, and absent in organization mode).
- **0147** — Which Peek server: in local mode without a bundle section, the person's choice; a bundle
  `base_url` pins it; organization mode offers Peek only with one. A token is used only with the
  server it was issued for (a mismatch asks to connect again); only `https` (or `http` to loopback);
  redirects are never followed; the sign-in address Peek answers with must be on the same server;
  every Peek approval card names the server. Sign-in stays a non-sensitive Settings action, like
  every connector's. Rejected: a free server address in organization mode (a person could send
  organization data to any server).
- **0148** — Peek comments are posted only through a token-authenticated account comment endpoint,
  under the account's own name; on a Peek server without it, `peek_add_comment` fails with a message
  saying so. Rejected: sending the token to the browser comment endpoint (it ignores the token and,
  on a public page, posts as "anonymous", so a comment the person approved would appear unsigned);
  posting with a made-up author name.
- **0149** — Peek visit stats reach the AI client as counts, visitor names and times only; Peek's IP
  address and user agent for each visit are dropped in the client and never carried. Rejected:
  returning them after review (personal data the assistant has no use for, and the card could not
  make that visible enough).

## 5. Manual steps

Step-by-step page: see `manual_steps_artifact` in the manifest.

- **Before implementation**: add the account comment endpoint (§3.1) to the fork and deploy it
  (`mb1`); a QA Peek server running it, reachable from the self-hosted runner, with a QA account and a
  seed page with one comment (`mb2`); on the runner, the Peek token file and the seed slug in
  `qa_environment.yaml` (`mb3`). `p9-qa-recorder` dispatches `qa-record-fixture.yml`, which fails
  without them.
- **After implementation**: connect Peek in local mode and drive every tool from a real AI client
  (`ma1`); if an organization-mode test deployment exists, the bundle option and `/connect` (`ma2`).

## 6. Risks and open questions

- **Grist merging first, or not.** Both plans add a connector to the same lists and website counts.
  Website counts are never hard-coded in a brief: each phase reads the current number and adds to it.
  `_check_telegram_post` may already be `_check_form_post`. Test counts that grow by one
  (`test_routes_connect.py:170`, `TOKEN_WRITE_SITES`, `len(REFERENCE)`) are "one more than when the
  phase starts". ADR numbers: if 0146-0149 are taken when the last phase runs, take the next free
  numbers and say so in the final report (ADR rule 6).
- **Peek response shapes.** The parsers come from reading the fork's Go source, not a live server.
  `p9-qa-recorder` records real responses; a parser a fixture contradicts is fixed in that phase. If a
  recorded shape makes a §3.3 behaviour impossible, stop with `status=blocked`.
- **The fork endpoint.** If `p9`'s live check or lifecycle gets 404/405 from `account-comments`,
  `mb1` is not done: stop with `status=blocked` and say so.
- **Tests that enumerate connectors.** `tests/unit/test_daemon_main.py`,
  `test_settings_controller.py` (`TestSnapshotStructure` pins the snapshot keys,
  `test_connectors_cover_all_connectors`), `test_settings_window_html.py`, `web/test_routes_settings.py`,
  `web/test_routes_connect.py` and `web/test_server.py` (`SERVICE_LABELS` in the status payload) may pin
  a list or count. Update those to include `peek`; if one asserts something this plan does not account
  for (for example that every connector has an `ORG_CONFIG_SERVICE` entry, or that every operation key
  has a rule scope), stop with `status=blocked` and name it.
- **No Peek rule scope.** If a policy test requires a scope or grant for every operation key, stop with
  `status=blocked`: adding one is a design change.
- **Website goes live on merge.** `pages.yml` deploys `website/` from `main`, so `/connectors/peek/`
  is public once the feature PR merges, before a release carries the connector, and it describes a
  connector whose comment posting needs the fork's endpoint. Holding the page back would be a change
  to `p2-connector-listing` only (the page test requires the page today).
- **The runner credential.** If the dispatched `qa-record-fixture.yml` fails at its "copy QA state"
  step or with "Peek is not authenticated", `mb3` is not done: stop with `status=blocked`.

## Implementation manifest

```yaml
plan_slug: peek-connector
feature_branch: feature/peek-connector
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/EuE4FosPLnDACgsotL89h7
manual_steps_source: docs/peek-connector-plan-manual-steps.html
manual_before:
  - id: mb1-fork-comment-endpoint
    title: Add the account comment endpoint (POST /api/uploads/{slug}/account-comments) to the Peek fork, through /devflow:make-plan with the prompt on the manual-steps page, and deploy it
    why: p9-qa-recorder's live check and lifecycle post a comment through it; without it peek_add_comment can only report that the server lacks it.
    done_when: A POST to https://<your Peek>/api/uploads/<a page you own>/account-comments with your token and {"body":"[QATEST] probe"} answers 200 with JSON whose "author" is your account name, and the same request without the Authorization header answers 401.
  - id: mb2-peek-qa-server
    title: A QA Peek server running the fork, a QA account on it, and a seed page with one comment
    why: p9-qa-recorder records live fixtures from this page; without it the recording has nothing to read.
    done_when: The QA server is reachable over https from the self-hosted runner, the QA account owns a private page named "PrivacyFence QA [QATEST].html" with one comment whose text contains [QATEST], and you have its slug.
  - id: mb3-runner-qa-state
    title: Put the QA account's Peek token and the seed slug on the self-hosted QA runner
    why: p9-qa-recorder dispatches qa-record-fixture.yml, which reads ~/privacyfence/credentials/peek_token.json and the peek section of ~/privacyfence/tests/fixtures/qa_environment.yaml on the runner; without them the run fails.
    done_when: On the runner, ~/privacyfence/credentials/peek_token.json exists with mode 600 and holds {"base_url", "token"}, ~/privacyfence/tests/fixtures/qa_environment.yaml has "peek:" with "page_slug:", and the runner's ~/privacyfence/org/org_config.json has no peek section or one whose base_url equals the token file's.
manual_after:
  - id: ma1-local-mode-check
    title: Connect Peek from Settings with the device sign-in and drive every Peek tool from an AI client
    why: Proves the sign-in modal against a real Peek, the approval cards (server named, password never shown, no IP address), a real publish, visibility change, comment under the account name and delete, which unit tests and the recorder cannot show.
  - id: ma2-org-mode-check
    title: (If you run an org-mode test deployment) a bundle with --peek-base-url, and connecting Peek on /connect
    why: Proves the bundle option and the /connect device sign-in; no CI job runs an organization deployment against Peek.
verify_after_merge:
  - python3 -m pytest tests/unit/test_peek_client.py tests/unit/test_systemic_gate_invariants.py -q
  - python3 -m pytest tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_website_docs_allowlist.py tests/unit/test_connector_tool_annotations.py -q
  - python3 -m pytest tests/unit/connectors -q -k peek
  - python3 -m pytest tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_generate_always_allow_reference.py -q
  - python3 -m pytest tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py tests/unit/web/test_routes_settings.py tests/unit/test_settings_window_html.py tests/unit/web/test_routes_connect.py tests/unit/test_build_org_bundle.py tests/unit/test_qa_fixture_recorder.py -q
final_checks:
  - docs/peek-connector-plan.md and docs/peek-connector-plan-manual-steps.html are deleted and nothing links to them (grep -rn "peek-connector-plan" . --exclude-dir=.git finds nothing)
  - ADRs 0146, 0147, 0148 and 0149 (or the numbers p10 took instead, named in its report) exist in docs/adr/, are Accepted, and are in the docs/adr/README.md index
  - CHANGELOG.md has the Peek entry under "## [Unreleased]" and no new version heading
  - After python3 scripts/generate_tools_reference.py and python3 scripts/generate_always_allow_reference.py, git diff --exit-code docs/tools-reference.md docs/always-allow-rules-reference.md exits 0
  - The PR description links the qa-record-fixture.yml run (p9-qa-recorder) and the connector-live-check.yml run (p10-docs-adrs-retire), which is the definition-of-done QA row
phases:
  - id: p1-client
    title: Peek client, device sign-in and credential file, with tests
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/peek_client.py
      - tests/unit/test_peek_client.py
      - tests/unit/test_systemic_gate_invariants.py
    brief: |
      Read first: the must-read docs (CLAUDE.md, CONTRIBUTING.md, docs/releasing.md, docs/coding-and-testing-guidelines.md,
      docs/testing-policy.md, docs/adr/README.md), plan §2 (the Peek API table), §3.0 and §3.2 (the spec), and
      src/privacyfence/salesforce_client.py:415-530 (token file helpers this copies in shape).
      1. Create src/privacyfence/peek_client.py exactly as plan §3.2: module docstring, PeekClientError, the
         constants, normalize_base_url, validate_slug, bundle_base_url, _send with the error mapping, PeekDeviceLogin,
         start_device_login, PeekLoginPoll, poll_device_login, save_token_file (secure_files.atomic_write_json),
         load_token_file, resolve_server, the five dataclasses and PeekClient with _request and every method in the
         §3.2 table. Exact error strings from §3.2. timeout=30 and allow_redirects=False on every request. Never log
         a token, device code, password, page content or comment body; log the host and counts.
      2. tests/unit/test_peek_client.py (pytestmark = pytest.mark.unit; module docstring naming the invariant "a Peek
         token is only ever sent to the server it was issued for"). Fake requests at the boundary with monkeypatch on
         requests.Session.request (no network). Classes: TestNormalizeBaseUrl (every rule and message; path prefix
         kept; scheme and host lower-cased; http://localhost, http://127.0.0.1 and http://[::1] accepted),
         TestValidateSlug ("PhiUs-lMbZE_Sw" accepted; "../x", "a/b", "a?b", "" and 65 characters rejected),
         TestBundleBaseUrl (absent → ""; org_mode and absent → "Peek organization config not installed"; a bad URL →
         the "not usable" message), TestSendErrors (connection error, 302, 401, 403 with {"error": "not owner"}, 404,
         500 with a 300-character error cut to 200, 500 without JSON, 200 with an HTML body; exact messages; assert
         allow_redirects=False and timeout=30), TestStartDeviceLogin (parses; expires_at from expires_in; a missing
         key; a verification_url on another host or another path prefix rejected; interval 0 becomes 2; repr hides
         the device code), TestPollDeviceLogin (each of the five statuses; approved without token and an unknown
         status rejected; repr hides the token), TestTokenFile (round trip; mode 0o600 on POSIX; missing file →
         "not authenticated"; invalid JSON, a list, a missing token and a non-loopback http base_url → "could not be
         read"; trailing slash normalized on load), TestResolveServer (no pin → the record's URL; equal pin → it;
         different pin → the "different server" message), and one class per PeekClient method asserting method,
         path, Authorization header, body or multipart fields and parsing: TestListPages (newest first, created_at
         as ISO 8601 UTC, missing keys give "" and 0), TestGetPage (found; missing → its message, no extra request),
         TestGetComments (sorted by created_at then id), TestGetStats (ip and user_agent dropped: the PeekVisit has
         only visitor_name and visited_at), TestUploadPage (multipart file name and "text/html"; password sent only
         for password visibility; the visibility and password checks raise before any request), TestSetVisibility,
         TestDeletePage, TestAddComment (path account-comments; 404 and 405 → the "no account comment endpoint"
         message). Every slug-taking method rejects a bad slug before any request. Assert no token, device code or
         password appears in any raised message or repr.
      3. tests/unit/test_systemic_gate_invariants.py: add ("peek_client", None, "save_token_file") to TOKEN_WRITE_SITES
         and bump the count test by one (rename test_ten_token_write_sites_are_listed / test_eleven_… to the new
         number word and assert the new length; if the Grist plan already made it eleven, Peek makes it twelve).
      4. ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/test_peek_client.py tests/unit/test_systemic_gate_invariants.py -q passes
      - python3 -m pytest tests/unit/test_peek_client.py -q --cov=privacyfence.peek_client --cov-branch --cov-report=term-missing reports 100% for src/privacyfence/peek_client.py
      - grep -n "allow_redirects=False" src/privacyfence/peek_client.py matches
      - grep -n "verify=False" src/privacyfence/peek_client.py finds nothing
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p2-connector-listing
    title: PeekConnector with peek_list_pages, and every per-connector table, page and guide a new connector module requires
    depends_on: [p1-client]
    complexity: M
    touches:
      - src/privacyfence/connectors/peek.py
      - tests/unit/connectors/test_peek_connector.py
      - src/privacyfence/auto_accept.py
      - scripts/pyinstaller_common.py
      - scripts/generate_tools_reference.py
      - scripts/build_site.py
      - docs/tools-reference.md
      - docs/peek-setup*.md
      - docs/README.md
      - README.md
      - website/connectors/peek/index.html
      - website/_partials/other-connectors.html
      - website/connectors/index.html
      - website/how-it-works/index.html
      - website/compare/mcp-gateways/index.html
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_website_connector_pages.py
      - tests/unit/test_website_connectors_page.py
    brief: |
      Read first: the must-read docs, plan §3.0, §3.3 (connector spec) and §3.8 (setup guide), and
      src/privacyfence/connectors/salesforce.py (template for _fetch, _auto_audit and tool_specs style).
      1. Create src/privacyfence/connectors/peek.py: module docstring, PeekConnector(Connector) with name "peek",
         __init__(client: PeekClient, download_mode: str = "local"), client property, _fetch and _auto_audit as
         plan §3.3, tool_specs() returning ONLY peek_list_pages (description and params from §3.3), and call()
         dispatching it (unknown tool → ValueError(f"Unknown Peek tool: {tool!r}")). max_results validation from §3.3;
         return the list shape from the §3.3 table, truncated to max_results. Do not add the other six tools now.
      2. src/privacyfence/auto_accept.py TOOL_TO_GATE: "peek_list_pages": "auto" under a "# Peek" comment line like
         the other connectors' groups.
      3. Bookkeeping: "privacyfence.connectors.peek" in scripts/pyinstaller_common.py's connectors list; "peek": "Peek"
         as the last entry of both CONNECTOR_TITLES and CONNECTOR_SHORT in scripts/generate_tools_reference.py;
         PeekConnector in CONNECTOR_CLASSES (and its import) in tests/unit/connectors/test_readme_manifest_alignment.py.
      4. python3 scripts/generate_tools_reference.py to regenerate docs/tools-reference.md (Peek row: 1 tool, 1 auto,
         0 review, 0 popup).
      5. Website and docs, required by tests/unit/test_website_connector_pages.py and test_website_connectors_page.py.
         Read the current numbers first; never assume "eleven" or "120":
         a. The setup guide peek-setup.md in docs/, with the sections in plan §3.8 (describe all seven tools and both
            modes now; the feature ships as a whole).
         b. README.md "## Connectors" table: the last row "| Peek | List your shared HTML pages; read comments and
            visit counts after review; publish pages, change who can open them, comment on and delete them |".
         c. website/connectors/peek/index.html, modelled on website/connectors/telegram/index.html (same head, meta
            pf-content-group connector, canonical/og URLs for /connectors/peek/, the other-connectors include with
            current="peek"). Copy: Peek is a self-hosted server for sharing HTML pages and collecting comments;
            the assistant lists pages straight away, reads comments and visit counts after review, and publishes,
            changes visibility, comments and deletes only with your approval; visit counts never include IP
            addresses; you connect with Peek's own sign-in. Its "Set it up" button is exactly
            <a class="button primary" href="GUIDE_URL">Set it up</a>
            where GUIDE_URL is https://github.com/privacyfence/privacyfence/blob/main/ followed by docs/ and the guide's
            file name peek-setup.md, with no space (written split here because test_docs_references_exist.py rejects
            a plan naming a doc path that does not exist yet).
         d. scripts/build_site.py: "/connectors/peek/": "connectors/peek/index.html" in PAGES (after telegram) and
            "peek-setup" in CONNECTOR_GUIDES.
         e. tests/unit/test_website_connector_pages.py CONNECTORS: "peek": ("/connectors/peek/", "peek-setup", "Peek").
         f. website/connectors/index.html: a Peek card in the same position as Peek's row in the tools-reference
            summary table (last), copying the Telegram card's markup with id="peek", data-connector="Peek",
            data-tools="1" data-auto="1" data-review="0" data-popup="0", the printed line
            "1 tools: 1 without a card · 0 reviewed · 0 need approval" (exactly that: test_website_connectors_page.py
            builds the expected line as f"{n} tools: …" with no singular form), links to
            /docs/tools-reference/#peek and /connectors/peek/; in the h1 add one to the connector count word and 1 to
            the tool total (the total must equal the regenerated tools-reference summary's total).
         g. website/how-it-works/index.html: "<N> connector tools" → the new total.
         h. website/compare/mcp-gateways/index.html: "Its own <word> connectors" → one more.
         i. tests/unit/test_website_connectors_page.py: len(REFERENCE) and the connector count word, one more each.
         j. website/_partials/other-connectors.html: <li data-connector="peek"><a href="/connectors/peek/">Peek</a></li>
            as the last connector line (before "All connectors"), and update the partial's header comment count.
         k. docs/README.md: "- [`peek-setup.md`](peek-setup.md)" as the last line of the "### Connector setup" list in
            the user-and-operator half, so test_website_docs_allowlist.py passes.
      6. Create tests/unit/connectors/test_peek_connector.py (module docstring; pytestmark unit): TestDispatch (unknown
         tool → ValueError); TestListPages (never calls gated_call — use the gated_call_spy fixture pattern from
         tests/unit/connectors/test_salesforce_connector.py:75-84 and assert it stays empty; writes an auto_accepted
         audit entry with the tool name; truncates to max_results; 0 and 201 raise the §3.3 message; a PeekClientError
         becomes RuntimeError); TestToolDefinitions with PEEK_SIBLINGS = {"peek_list_pages": ()} for now, calling
         assert_tool_definitions_complete; TestEveryToolIsAudited calling assert_all_tools_leave_an_audit_trail.
      7. ruff check ., python3 -m pytest tests/unit -q, and python3 scripts/build_site.py --out /tmp/pf-site-check
         --no-docs --offline (the site builds and its link check passes).
      Stop condition: if a unit test outside the files in touches fails because of the new connector module
      (another hand-maintained connector list this plan does not name), stop with status=blocked and name the test.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_peek_connector.py -q passes
      - python3 -m pytest tests/unit/test_website_docs_allowlist.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_pyinstaller_hidden_imports.py tests/unit/web/test_tool_schema_portability.py tests/unit/test_connector_tool_annotations.py tests/unit/test_systemic_gate_invariants.py -q passes
      - grep -n "| \[Peek\](#peek) | 1 | 1 | 0 | 0 |" docs/tools-reference.md matches
      - python3 scripts/build_site.py --out /tmp/pf-site-check --no-docs --offline exits 0
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p3-reads
    title: peek_get_comments and peek_get_stats, the review-gated reads
    depends_on: [p2-connector-listing]
    complexity: M
    touches:
      - src/privacyfence/connectors/peek.py
      - tests/unit/connectors/test_peek_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
    brief: |
      Read first: plan §3.3 (the two tools' rows, validation, _anchor_text and gated_call arguments) and §3.4;
      src/privacyfence/connectors/telegram.py _get_messages (l.189-252) as the pattern for preview, new_info,
      preview_tables, table_only and pii_scan_text.
      1. connectors/peek.py: add peek_get_comments and peek_get_stats to tool_specs() (descriptions, params, siblings
         from §3.3) and call(); _anchor_text; each fetches get_page then its data through _fetch, then calls
         gated_call with exactly the §3.3 arguments, and returns the §3.3 dict. Update peek_list_pages' description to
         name both new siblings.
      2. auto_accept.py: TOOL_TO_GATE review ×2; TOOL_TO_OPERATION "peek_get_comments": "peek.read_comments",
         "peek_get_stats": "peek.read_stats". policy/registry.py TOOL_TO_VERB Verb.READ ×2. gate.py _TOOL_LAYOUT WIDE ×2.
      3. python3 scripts/generate_tools_reference.py and python3 scripts/generate_always_allow_reference.py; then
         website/connectors/index.html Peek card → data-tools="3" data-auto="1" data-review="2" data-popup="0", printed
         line "3 tools: 1 without a card · 2 reviewed · 0 need approval", h1 total +2; website/how-it-works/index.html
         total +2 (both equal to the regenerated summary's total).
      4. Tests in tests/unit/connectors/test_peek_connector.py: TestGetComments — preview has exactly the keys Server
         (first, equal to client.host), Page, Link, and no comment text (data minimization); details_text carries
         each body with its author and anchor; pii_scan_text holds bodies and element_text; the table's headers; args
         == {"slug": slug}; no comments → "(no comments)" and no table; each anchor kind's text. TestGetStats — same
         preview rule; new_info's three keys; the returned dict's recent entries have exactly visitor_name and
         visited_at; "(no visits)"; a visitor with no name shows "(no name given)". For both: a bad slug raises
         ValueError with gated_call_spy empty; a PeekClientError becomes RuntimeError. TestFieldCompleteness — a real
         PeekClient with requests.Session.request faked to return a fully populated /api/uploads list and comments
         answer, run through peek_get_comments, checked with assert_no_placeholder_fields(gated_call_spy[0]["preview"])
         (pattern: tests/unit/connectors/test_confluence_connector.py TestFieldCompleteness). Extend PEEK_SIBLINGS and
         TestEveryToolIsAudited (a MagicMock client whose get_page returns PeekPage("slug1", "QA page", "qa", 10,
         "private", "https://peek.example.com/p/slug1", "2026-10-10T11:07:00Z"), with host "peek.example.com";
         arg_overrides {"slug": "slug1"} for both tools).
      5. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: plan §6 "No Peek rule scope".
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_peek_connector.py -q passes
      - python3 -m pytest tests/unit/test_systemic_gate_invariants.py tests/unit/policy tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n "| \[Peek\](#peek) | 3 | 1 | 2 | 0 |" docs/tools-reference.md matches
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p4-upload
    title: peek_upload_page, popup-gated, with html, local_path and upload_id sources
    depends_on: [p3-reads]
    complexity: M
    touches:
      - src/privacyfence/connectors/peek.py
      - tests/unit/connectors/test_peek_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
    brief: |
      Read first: plan §3.3 ("Upload source handling", the peek_upload_page row, validation and gated_call
      arguments) and §3.4; src/privacyfence/connectors/drive.py _upload_file (l.1457-1640) for local_files use and
      the ADR 0102 commit_uploads call; docs/adr/0007-local-file-bridge.md, 0028 and 0102.
      1. connectors/peek.py: add peek_upload_page (spec and params from §3.3; its description already names
         peek_set_visibility, which arrives in p5; PEEK_SIBLINGS gets "peek_upload_page": () now and its sibling in p5).
         Order: validate args (ValueError, §3.3) → resolve the bytes (html, or local_files for local_path/upload_id
         exactly as §3.3, refusing local_path in org download_mode) → size and UTF-8 checks → gated_call(gate="popup",
         §3.3 arguments) → local_files.commit_uploads() → client.upload_page through _fetch → return the §3.3 dict.
         The upload must not run, and commit_uploads must not be called, if gated_call raises.
      2. Tables: TOOL_TO_GATE popup; TOOL_TO_OPERATION "peek.upload_page"; TOOL_TO_VERB Verb.CREATE;
         write_effects.EFFECT_BY_TOOL with the exact §3.4 string (under a "# ── Peek ──" header like the others);
         gate._TOOL_LAYOUT WIDE.
      3. Regenerate docs/tools-reference.md and docs/always-allow-rules-reference.md (the two generator scripts);
         website card → "4 tools: 1 without a card · 2 reviewed · 1 need approval" with data-tools="4" data-auto="1"
         data-review="2" data-popup="1"; h1 total and how-it-works total +1.
      4. Tests: TestUploadPage — html source: preview keys exactly Server, Name, Visibility, Size (plus Password "Set
         (not shown)" for password visibility), no HTML and no password in preview, args or raw_data; details_text is
         the page's visible text (a <script> body does not appear); default visibility private; name defaults
         (page.html; local file's basename; a name with a directory keeps only its basename); local_path with
         local_files monkeypatched (require_local_files, read_local_file) reads the bytes; upload_id becomes the
         upload: reference; local_path in org mode → its message and nothing read; commit_uploads is called after the
         gate and not at all when the gate raises (monkeypatch gated_call to raise RuntimeError); the client upload is
         called only after the gate; every §3.3 validation message (no source, two sources, empty, non-UTF-8, over 10
         MB, visibility, each password rule) with gated_call_spy empty. Extend TestEveryToolIsAudited
         (upload_page returns PeekUploadResult("new1", "https://peek.example.com/p/new1", "private"); arg_overrides
         {"html": "<p>hi</p>"}).
      5. ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_peek_connector.py -q passes
      - python3 -m pytest tests/unit/test_write_effects.py tests/unit/policy/test_registry.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n "| \[Peek\](#peek) | 4 | 1 | 2 | 1 |" docs/tools-reference.md matches
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p5-page-writes
    title: peek_set_visibility, peek_add_comment and peek_delete_page, popup-gated
    depends_on: [p4-upload]
    complexity: M
    touches:
      - src/privacyfence/connectors/peek.py
      - tests/unit/connectors/test_peek_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - tests/unit/test_connector_tool_annotations.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
    brief: |
      Read first: plan §3.3 (the three tools' rows, validation and gated_call arguments) and §3.4;
      src/privacyfence/connectors/telegram.py _send_message (l.303-325) as the popup pattern.
      1. connectors/peek.py: add peek_set_visibility, peek_add_comment and peek_delete_page (specs, params, siblings
         from §3.3; peek_delete_page has destructive=True). Order inside each: validate (ValueError) → _fetch get_page →
         gated_call(gate="popup", §3.3 arguments) → the client write through _fetch → return the §3.3 dict. The write
         must not run if gated_call raises. Update peek_get_comments' description to name peek_add_comment.
      2. Tables: TOOL_TO_GATE popup ×3; TOOL_TO_OPERATION peek.set_visibility / peek.add_comment / peek.delete_page;
         TOOL_TO_VERB SHARE / COMMENT / DELETE; write_effects.EFFECT_BY_TOOL with the three exact §3.4 strings;
         gate._TOOL_LAYOUT WIDE for peek_add_comment only. tests/unit/test_connector_tool_annotations.py:
         add "peek_delete_page" to DESTRUCTIVE_TOOLS.
      3. Regenerate docs/tools-reference.md and docs/always-allow-rules-reference.md; website card → "7 tools: 1 without
         a card · 2 reviewed · 4 need approval" with data-tools="7" data-auto="1" data-review="2" data-popup="4"; h1
         total and how-it-works total +3.
      4. Tests: TestSetVisibility (preview Server, Page, Link, Visibility "old → new", Password "Set (not shown)" only
         for password visibility; no password anywhere in the gate kwargs; validation messages), TestAddComment (the
         three anchor texts; details_text is the body; body/selector/element_text messages; element_text without a
         selector; returns {"slug", "comment"}), TestDeletePage (preview keys; destructive spec; returns {"deleted"}).
         For each: the client write is called only after the spy returns and not at all when gated_call raises; a bad
         slug and a get_page PeekClientError stop before the gate. Complete PEEK_SIBLINGS from §3.3. Extend
         TestEveryToolIsAudited (set_visibility returns "public"; add_comment returns a PeekComment; arg_overrides
         {"slug": "slug1", "visibility": "public"} and {"slug": "slug1", "body": "hi"}).
      5. ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_peek_connector.py -q passes
      - python3 -m pytest tests/unit/test_write_effects.py tests/unit/policy tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_connector_tool_annotations.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n "| \[Peek\](#peek) | 7 | 1 | 2 | 4 |" docs/tools-reference.md matches
      - python3 -m pytest tests/unit/connectors/test_peek_connector.py -q --cov=privacyfence.connectors.peek --cov-branch --cov-report=term-missing reports 100% for src/privacyfence/connectors/peek.py
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p6-local-daemon
    title: Build the Peek connector in the daemon, the --peek-login CLI sign-in, and Peek in the Settings connector list
    depends_on: [p2-connector-listing]
    complexity: M
    touches:
      - src/privacyfence/daemon_main.py
      - src/privacyfence/settings_controller.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_settings_controller.py
    brief: |
      Read first: plan §3.5 (daemon, CLI and the first settings_controller bullet) and §3.2; daemon_main.py TOKEN_FILES
      (l.169-180), the Telegram block of build_connectors (l.1531-1564), run_telegram_setup (l.1797-1811), the flags and
      dispatch (l.2091-2100, 2207-2243); settings_controller.py ALL_CONNECTORS (l.89-92) and _connectors_state
      (l.1734-1767).
      1. daemon_main.py: TOKEN_FILES["peek"]; the imports under the names in §3.5; the build_connectors block exactly as
         §3.5; run_peek_login and --peek-login with the exact messages, added to the auth-flag check.
      2. settings_controller.py: "peek" appended to ALL_CONNECTORS and has_org True for "peek" in _connectors_state.
         Nothing else here; the sign-in actions are p7.
      3. Tests: test_daemon_main.py — with no bundle section a valid token file builds the connector
         (PeekClient.check_connection monkeypatched) with download_mode "local"; a pinned bundle URL equal to the
         file's builds it; a different one → failures["peek"] == "not_authenticated"; org mode with no peek section →
         "no_org_config"; no file → "not_authenticated"; a check_connection 401 → "not_authenticated"; disabled → no
         entry. run_peek_login: success after one "pending" poll (time.sleep, input, start_device_login,
         poll_device_login monkeypatched) writes the token file and returns 0 with its message; a pinned URL skips the
         input() prompt; denied, expired (time past expires_at), consumed and a PeekClientError each return 1 with
         their messages; --peek-login dispatches to it; add "--peek-login" to
         test_oauth_and_telegram_flags_do_not_trigger_the_separation_gate's parameters.
         test_settings_controller.py — the peek row exists with has_org True and no bundle; update any assertion
         that pins the exact connector list or count.
      4. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: plan §6 "Tests that enumerate connectors".
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py -q passes
      - 'grep -n "\"peek\": \"credentials/peek_token.json\"" src/privacyfence/daemon_main.py matches'
      - grep -n "\-\-peek-login" src/privacyfence/daemon_main.py matches
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p7-local-settings-login
    title: The Peek device sign-in on the local Settings page
    depends_on: [p6-local-daemon]
    complexity: M
    touches:
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/org_settings_scope.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_settings_controller.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Read first: plan §3.5 (the sign-in actions, org_settings_scope/routes_settings and settings_window_html bullets)
      and §3.2 (start_device_login, poll_device_login, save_token_file); the Telegram flow this copies:
      settings_controller.py:1345-1473, settings_window_html.py:1185-1251 and 1320-1377,
      web/org_settings_scope.py:126-129, web/routes_settings.py:251-263 and 1320-1339.
      1. settings_controller.py: self._peek_auth next to _telegram_auth; _peek_pinned; the "peek_auth" snapshot key;
         peek_start_login (base_url defaulting to ""), peek_finish_login and peek_cancel_login exactly as §3.5. The
         device code and the token never reach the snapshot or a log line.
      2. web/org_settings_scope.py and web/routes_settings.py: the three actions as §3.5 (LOCAL_MODE, non-sensitive), and
         the "peek_auth" stub in the org-mode settings snapshot.
      3. settings_window_html.py: the Peek row and the two-step modal as §3.5, reusing the Telegram modal's classes,
         helpers and auto-close logic; the .pf-modal-code rule with tokens only.
      4. Tests: test_settings_controller.py — peek_start_login: a bad URL and a malformed bundle section set their errors
         without starting anything; a pinned URL wins over the submitted one and a request without base_url works;
         success (start_device_login and webbrowser.open monkeypatched, _run_async driven synchronously the way the
         Telegram tests do) gives step "approve" with user_code and verification_url in the snapshot and no
         device_code anywhere in json.dumps(snapshot()); a failure returns to step None with the error.
         peek_finish_login: approved writes data_dir()/credentials/peek_token.json as {"base_url", "token"}, clears
         _peek_auth and refreshes connectors; pending keeps the step with its message; denied, expired (clock past
         expires_at, no poll made) and consumed reset with their messages; an exception keeps the step; called with no
         sign-in in progress it changes nothing. peek_cancel_login clears it. TestSnapshotStructure gains "peek_auth".
         web/test_routes_settings.py — the three actions allowed and classified (TestSensitiveActionsCoverAllAllowedActions
         passes); a POST to peek_start_login reaches the controller; the org snapshot carries the stub.
         test_settings_window_html.py — the script carries data-peek-auth, data-peek-field="base_url", the
         peek_start_login and peek_finish_login actions and "Approve in Peek".
      5. ruff check . and python3 -m pytest tests/unit -q (test_design_system.py must pass: no colour literal, no width
         media query).
      Stop condition: plan §6 "Tests that enumerate connectors".
    acceptance:
      - python3 -m pytest tests/unit/test_settings_controller.py tests/unit/web/test_routes_settings.py tests/unit/test_settings_window_html.py tests/unit/test_design_system.py -q passes
      - grep -n '"peek_start_login"' src/privacyfence/web/org_settings_scope.py src/privacyfence/web/routes_settings.py matches in both files
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p8-org-connect
    title: The org bundle peek section and the device sign-in on /connect
    depends_on: [p6-local-daemon]
    complexity: M
    touches:
      - scripts/build_org_bundle.py
      - src/privacyfence/web/routes_connect.py
      - docs/configuration-reference.md
      - tests/unit/test_build_org_bundle.py
      - tests/unit/web/test_routes_connect.py
      - tests/unit/web/test_server.py
    brief: |
      Read first: plan §3.6 (whole) and §3.2; web/routes_connect.py (whole module; the Telegram parts at l.111-148,
      196-230, 415-541 and 595-695); scripts/build_org_bundle.py's Salesforce option group (l.188-193, 505-511) and the
      services tuple (l.692); docs/configuration-reference.md "Build options" (l.236-254).
      1. scripts/build_org_bundle.py: the Peek argument group, validation and section, and "peek" in the services
         tuple, exactly as §3.6.
      2. docs/configuration-reference.md: the --peek-base-url row from §3.6 after the Atlassian rows
         (tests/unit/test_docs_configuration_reference.py requires every option documented).
      3. web/routes_connect.py: everything in §3.6's routes_connect bullet: SERVICE_LABELS, configured/connected checks,
         _PeekState and _PeekAuthStore, the _check_form_post rename (unless it exists), the three handlers and routes,
         _peek_box_html and its place in _render_connect_page, .pf-peek-code in _STYLE with tokens only. Device sign-in
         calls go through asyncio.to_thread.
      4. Tests: test_build_org_bundle.py — --peek-base-url writes {"peek": {"base_url": ...}} (trailing slash stripped);
         a Peek-only bundle is written; http:// on a non-loopback host and a URL with a query each exit with the exact
         message; http://localhost is accepted. web/test_routes_connect.py — the box for: no section ("Not set up by
         your organization", a cluster row), a section with no token, a section with a token for that server
         ("Connected"), a token for another server (not connected); POST /connect/peek/start without CSRF → 401,
         cross-origin → 403, signed out → the signed-out redirect, not configured → its error, success
         (start_device_login monkeypatched) → the next GET shows the code and the Open Peek link to the
         verification_url; /connect/peek/finish approved (poll monkeypatched) writes
         user_dir(principal)/credentials/peek_token.json with the bundle's base_url, evicts the principal and shows
         Connected; pending, denied and expired show their messages; cancel clears; no token or device code appears
         in any response body; the row-count assertion (l.170) goes up by one. web/test_server.py — update the status
         payload assertion (around l.1487) if it pins SERVICE_LABELS.
      5. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: plan §6 "Tests that enumerate connectors".
    acceptance:
      - python3 -m pytest tests/unit/test_build_org_bundle.py tests/unit/web/test_routes_connect.py tests/unit/web/test_server.py tests/unit/test_docs_configuration_reference.py -q passes
      - grep -n '"/connect/peek/start"' src/privacyfence/web/routes_connect.py matches
      - grep -n "peek-base-url" scripts/build_org_bundle.py docs/configuration-reference.md matches in both files
      - grep -n "_check_telegram_post" src/privacyfence/web/routes_connect.py finds nothing
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p9-qa-recorder
    title: Live check, lifecycle and recorded fixtures for Peek
    depends_on: [p5-page-writes, p6-local-daemon]
    complexity: M
    touches:
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/fixtures/qa_environment.yaml.example
      - tests/fixtures/live/peek/**
      - tests/unit/test_peek_client.py
      - src/privacyfence/peek_client.py
      - docs/connector-qa.md
    brief: |
      Read first: docs/connector-qa.md (whole), docs/testing-policy.md "Layer 5", scripts/qa_fixture_recorder.py
      _build_salesforce_client (l.948-956), check_confluence (l.811-865), the lifecycle checks and LIFECYCLE_CHECKS,
      RawCapture (l.595-628), redact() and deidentify_structural_fields(), and .claude/skills/steward/SKILL.md's notes on
      qa-record-fixture.yml.
      1. scripts/qa_fixture_recorder.py:
         - _build_peek_client(): pinned = peek_client.bundle_base_url(daemon_main.load_org_config().get("peek") or {},
           org_mode=False); record = peek_client.load_token_file(daemon_main._resolve_path(daemon_main.TOKEN_FILES["peek"]));
           return PeekClient(peek_client.resolve_server(pinned, record), record["token"]).
         - check_peek(record, manifest): cfg = manifest.get("peek") or {}; page_slug required (missing → a failed
           CheckResult "peek.page_slug missing from qa_environment.yaml"). Two CheckResults recorded through RawCapture:
           list_pages ("list_pages.json": only the seed page's entry, then deidentify_structural_fields(redact(...)) so the
           owner, host and URL are placeholders; ok when the slug is listed and its name contains [QATEST]) and
           get_comments ("get_comments.json": the raw comments answer with authors redacted; ok only when there is at
           least one comment and every body contains [QATEST], refusing to record otherwise). Stats are never recorded
           (they carry IP addresses).
         - lifecycle_peek(manifest): upload a private page "[QATEST] lifecycle <suffix>.html" with a [QATEST] body, set it
           to public, add_comment "[QATEST] lifecycle comment", get_comments and confirm it with the QA account's name as
           author, delete_page, then list_pages and confirm it is gone. LifecycleResult("peek", ok, note, cleanup_ok=<the
           delete was confirmed>); a failure after the upload still tries the delete.
         - Register "peek" in CONNECTOR_CHECKS, EXPECTED_FIXTURES ("list_pages.json", "get_comments.json") and
           LIFECYCLE_CHECKS; update the comment above LIFECYCLE_CHECKS to name Peek.
      2. tests/fixtures/qa_environment.yaml.example: a peek section (page_slug: "") with comments in the file's style.
      3. docs/connector-qa.md: a Peek row in the QA accounts table (a QA account on a QA Peek server running the fork
         with the account comment endpoint; the runner's token in credentials/peek_token.json written by hand as
         {"base_url", "token"} with mode 600, the token from `peek login` on any machine); "### Seed: Peek" (a private
         page "PrivacyFence QA [QATEST].html" with one [QATEST] comment; set peek.page_slug); the Manifest reference row;
         the sentence that lists which connectors --lifecycle covers gains Peek; a sentence in "Authenticating
         connectors" that Peek has no step there because its QA credential is a token file written by hand;
         "### Peek checks" in the exploratory section (device sign-in in Settings and on /connect, the review cards for
         comments and stats with no IP address, the four popup cards naming the server, the password never shown, a
         comment posted under the account name, the "no account comment endpoint" message against a stock Peek).
      4. Commit and push this phase branch, then dispatch .github/workflows/qa-record-fixture.yml against it with input
         connector=peek (GitHub MCP actions_run_trigger, ref = this phase branch). A queued run is waiting on the
         connector-live-check concurrency group: wait, never re-dispatch. When it succeeds, git pull. Review every file
         under tests/fixtures/live/peek/ before continuing: no real e-mail, account name other than the QA placeholders,
         host name, token or non-[QATEST] content. If something leaks, extend the redaction in check_peek, push and
         dispatch again. Put the run URL in your final report.
      5. tests/unit/test_peek_client.py: TestLiveFixtureParsing replaying both fixtures through the real parsers
         (list_pages and get_comments via a faked requests.Session.request), skipping with the record hint when a file is
         missing, exactly like tests/unit/test_salesforce_client.py:1316-1360. If a fixture shows a shape the parser
         mishandles, fix peek_client.py and its unit test (plan §6).
      6. tests/unit/test_qa_fixture_recorder.py: check_peek (missing slug, a non-[QATEST] comment refused, success) and
         lifecycle_peek (success; a failing add_comment still deletes) against a fake client, following the existing
         per-connector tests there; TestFixturePresence passes.
      7. ruff check . and python3 -m pytest tests/unit -q. New text follows plan §3.0.
      Stop condition: plan §6 "The runner credential" and "The fork endpoint".
    acceptance:
      - ls tests/fixtures/live/peek/ lists list_pages.json and get_comments.json
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py tests/unit/test_peek_client.py -q passes, with TestLiveFixtureParsing not skipped
      - python3 -c "import sys; sys.path.insert(0,'scripts'); import qa_fixture_recorder as q; assert 'peek' in q.CONNECTOR_CHECKS and 'peek' in q.LIFECYCLE_CHECKS" exits 0
      - The qa-record-fixture.yml run for connector=peek on this phase branch concluded success (URL in the final report)
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p10-docs-adrs-retire
    title: Reference docs, changelog, the four ADRs, the live check, and retiring the plan
    depends_on: [p7-local-settings-login, p8-org-connect, p9-qa-recorder]
    complexity: S
    touches:
      - docs/peek-setup*.md
      - docs/configuration-reference.md
      - docs/README.md
      - docs/connecting-a-service.md
      - docs/org-mode-setup-guide.md
      - CHANGELOG.md
      - docs/adr/0146-*.md
      - docs/adr/0147-*.md
      - docs/adr/0148-*.md
      - docs/adr/0149-*.md
      - docs/adr/README.md
      - docs/peek-connector-plan.md
      - docs/peek-connector-plan-manual-steps.html
      - scripts/build_site.py
    brief: |
      Read first: docs/adr/README.md (template and rules), plan §3 and §4, docs/configuration-reference.md (l.95-110 and
      300-325), docs/connecting-a-service.md, docs/org-mode-setup-guide.md's per-connector table, CHANGELOG.md's
      "## [Unreleased]" section.
      1. The setup guide peek-setup.md in docs/: check every statement against the code as it now is (error texts in
         peek_client.py, settings_controller.py, routes_connect.py and daemon_main.py, the option name in
         build_org_bundle.py, the fork endpoint contract in plan §3.1) and correct it.
      2. docs/configuration-reference.md: add peek to the connectors.<name>.enabled list (l.103), Peek to the sentence
         listing per-service guides as "[Peek setup](peek-setup.md)", and `--peek-login` to the CLI flag table next to
         `--telegram-setup`. docs/connecting-a-service.md: a Peek bullet in "### Provider differences" (device sign-in:
         no OAuth app; the organization pins the server) and a Peek row in the "## Provider guides" table.
         docs/org-mode-setup-guide.md: a Peek row in the per-connector table (no callback URL; option --peek-base-url;
         guide "[Peek setup](peek-setup.md)") and the option in its build-options table, matching
         configuration-reference.md.
      3. CHANGELOG.md under "## [Unreleased]" (never a version heading): one Added line — "Peek connector: list your
         Peek pages, read their comments and visit counts after review, and publish pages, change who can open them,
         comment on them and delete them with approval. Connect with Peek's own sign-in from Settings, the connections
         page in organization mode, or `privacyfence-app --peek-login`; an organization bundle can pin the Peek server
         with `--peek-base-url`. Posting comments needs a Peek server with the account comment endpoint."
      4. Write ADRs from plan §4 with the next free numbers (0146-0149 unless taken; if taken, the next free ones, named
         in your report), titles stating the decision, with the docs/adr/README.md template (Status "Accepted — <today's
         date>. Implemented.", Context, Decision, Alternatives considered, Consequences, Verification naming the tests
         that enforce each, Related). Link source files and ADRs 0019, 0070, 0072, 0102 where relevant; never link the
         plan. Add the four rows to the index in docs/adr/README.md.
      5. git rm docs/peek-connector-plan.md docs/peek-connector-plan-manual-steps.html; remove "peek-connector-plan.md"
         from scripts/build_site.py CONTRIBUTOR_DOCS and its line from docs/README.md's contributor half;
         grep -rn "peek-connector-plan" . --exclude-dir=.git must find nothing.
      6. python3 scripts/generate_tools_reference.py and python3 scripts/generate_always_allow_reference.py leave no diff;
         ruff check . and python3 -m pytest tests/unit -q pass.
      7. Push this phase branch and dispatch .github/workflows/connector-live-check.yml (no inputs) against it with the
         GitHub MCP actions_run_trigger: it carries every phase, so it is the definition-of-done live check for
         src/privacyfence/*_client.py and connectors/**. A queued run is waiting on the connector-live-check concurrency
         group: wait, never re-dispatch. Put the run URL in your final report. If it opens or updates the
         chore/connector-live-fixture-drift PR for another connector, leave that PR alone (steward skill) and mention it.
    acceptance:
      - ls docs/adr/ lists four new Peek ADR files, each containing "Accepted", and docs/adr/README.md has a row for each
      - test ! -e docs/peek-connector-plan.md && test ! -e docs/peek-connector-plan-manual-steps.html
      - grep -rn "peek-connector-plan" . --exclude-dir=.git finds nothing
      - grep -n "Peek connector" CHANGELOG.md matches a line below "## [Unreleased]" and above the next "## [" heading
      - The connector-live-check.yml run on this phase branch concluded success, or failed only in another connector's row (URL in the final report)
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
```
