# Align the Peek connector with Peek's current API — plan

## Goal

Peek changed who-can-open-a-page under PrivacyFence, and two of the connector's write tools now fail or mislead:

- Page passwords are gone. Any request carrying one gets HTTP 400 `password-protected pages were removed; use restricted visibility and grants`, so `peek_upload_page` and `peek_set_visibility` with `password` visibility always fail.
- `private` was renamed `signed-in`. Peek stores and returns `signed-in`. PrivacyFence then shows a value its own tools refuse as input, and it cannot set the new default, `restricted` (only the owner, admins and people granted access).

After this change:

- The connector speaks Peek's three visibilities (`public`, `signed-in`, `restricted`). New pages default to `restricted`, like Peek's own default.
- Four new tools let the assistant list a page's versions and manage who can open a restricted page: `peek_list_versions`, `peek_list_grants`, `peek_grant_access` and `peek_revoke_access`. Every write gets an approval card.
- Comments carry Peek's `verified` flag, and a comment can target a version.
- Unreachable error handling is removed. Peek's rate-limit and not-HTML answers get their own messages.
- The QA recorder and fixtures follow the new model.

No issue tracks this.

## Current state

**Peek's API today** (andras-tkcs/peek `main`, a173e5d; paths under `internal/`):

- **Visibility.** Values are `public`, `signed-in` and `restricted`. `private` is still accepted as input and mapped to `signed-in`. Anything else gets 400 `visibility must be public, signed-in, or restricted` (`uploads/visibility.go:12-22`; `models/models.go:6-13`). Migrations 14 and 15 rewrote `private` to `signed-in` and password pages to `restricted`.
- **Default.** A new upload without a visibility is `restricted` (`uploads/service.go:109-112`).
- **Upload.** `POST /api/upload` reads the form fields `file`, `visibility`, `slug`, `backend_image` and `backend_port`.
  - A non-empty `password` → 400 (`server/api_uploads.go:72-75`).
  - A name over 200 runes → 400 `name must be 200 characters or fewer` (`uploads/service.go:106-108`).
  - A file that is not HTML → 415 `file does not look like HTML`.
- **Set visibility.** `POST /api/uploads/{slug}/visibility` with `{visibility}`. A non-empty password → 400 (`:339-342`). It answers `{"visibility", "notified"}` (`:363`). Switching to `restricted` emails everyone already granted (`:356-362`).
- **Comments.** `GET /api/uploads/{slug}/comments` items carry `id, selector, element_text, anchor_kind, author, verified, body, created_at, version` (`server/comments.go:23-33`).
  - `verified` is true for a signed-in author or the account API, and false for a name a viewer typed.
  - `POST /api/uploads/{slug}/account-comments` accepts `?v=<n>` (404 `version not found`) (`comments.go:217-221`).
  - Limits are bytes after trimming: body 4000, selector 500, element_text 200, else 413 `field too long` (`comments.go:184`).
  - 429 `too many comments, slow down` (`:203-205`).
- **Versions.** `GET /api/uploads/{slug}/versions` → `[{version, size, created_at, url}]` (`server/api_versions.go:8-35`). Owner or admin.
- **Grants.** All take the JSON `{"kind":"user"|"group","email":"…"}` and are owner or admin only (`server/api_grants.go`).
  - `GET /api/uploads/{slug}/grants` → `[{id, kind, email, created_at}]` (`:88-103`).
  - `POST` → the grant plus `notified` (`:105-125`). The grantee is emailed when the page is restricted.
  - `DELETE` (same body) → `{"removed": true}`, or 404 `grant not found` (`:127-146`).
  - The email is lower-cased, must parse, and is at most 254 characters. `kind` must be `user` or `group`. A group grant needs Peek's Google settings, else 400 (`:57-70`).
- **404s.** Every 404 is `{"error": "not found"}`, except `version not found` and `grant not found`. "page was renamed; use slug …" is written only on the upload-a-new-version path (`api_uploads.go:188-189`).
- **Rate limits.** The upload, comment and login rate limits answer 429.

**PrivacyFence today:**

- **`src/privacyfence/peek_client.py`.**
  - `VISIBILITIES = ("public", "password", "private")` and `PASSWORD_MAX_BYTES = 72` (`:28-29`).
  - The renamed-slug handling (`:36`, `:131-140`) cannot fire. The connector always calls `get_page` (the page list) first, and that answers "No Peek page … among the pages this account can manage".
  - `PeekPage.visibility` is commented "may also be restricted" (`:309`). `PeekComment` has no `verified` (`:315-324`, `_comment` `:375-386`).
  - `_check_visibility` is at `:389-395`. `upload_page(data, name, visibility, password, slug="")` is at `:475-497`. `set_visibility(slug, visibility, password) -> str` is at `:499-507`. `add_comment(slug, body, selector, element_text)` is at `:520-533`.
  - There are no methods for versions or grants.
  - `_send` is at `:112-155`. `PeekClientError(message, *, status=None)` is at `:43-53`.
- **`src/privacyfence/connectors/peek.py`.**
  - `_VISIBILITY_DESC` and `_PASSWORD` are at `:41-52`.
  - Tool specs are at `:74-283`, and the `call` dispatch at `:285-302`.
  - `_upload_page` (`:436-544`) defaults to `"private"` (`:461`) and puts "Password: Set (not shown)" on the card (`:516-517`).
  - `_set_visibility` (`:546-581`) refuses restricted pages (`:550-554`).
  - `_add_comment` (`:583-629`) checks lengths in characters.
  - `_check_visibility` is at `:727-738`.
  - `_auto_audit` (`:700-720`) is the audit for auto tools. `_fetch` (`:690-698`) turns `PeekClientError` into `RuntimeError`.
- **The tool lists every Peek tool appears in.**
  - `auto_accept.py`: `TOOL_TO_OPERATION` (Peek block `:150-156`; auto tools such as `peek_list_pages` have no entry) and `TOOL_TO_GATE` (`:378-386`).
  - `policy/registry.py`: `TOOL_TO_VERB` (`:213-219`), with the test matrix in `tests/unit/policy/test_registry.py` (Peek block `:281-287`; `:120` and `:311` require exact key sets).
  - `write_effects.py`: `EFFECT_BY_TOOL` (`:182-195`, enforced by `tests/unit/test_write_effects.py`).
  - `gate.py`: `_TOOL_LAYOUT` (`:275-276`).
  - `tests/unit/connectors/test_peek_connector.py`: `PEEK_SIBLINGS` (`:80-89`) and `arg_overrides` in `TestEveryToolIsAudited` (`:797-804`).
  - `tests/unit/test_connector_tool_annotations.py`: `DESTRUCTIVE_TOOLS` (`:29-31`).
- **Rules.** The `peek.page` scope has verbs `(READ, ADD)` (`policy/catalogue.py:80-84`). It matches on the call's `slug` argument (`policy/scopes.py:596-602`, `_peek_page_matches`). UPDATE and DELETE calls are never coverable, so they always get a card.
- **Generated docs.**
  - `docs/tools-reference.md` comes from `scripts/generate_tools_reference.py`. `tests/unit/test_docs_tools_reference.py:28-34` fails when it is stale.
  - `docs/always-allow-rules-reference.md` comes from `scripts/generate_always_allow_reference.py`.
  - Both are regenerated in the same phase that changes their sources.
- **Argument handling.** Unknown arguments are not stripped: `McpDispatcher.call` passes `args` to `connector.call(tool, args)` (`web/mcp_dispatch.py:229-281`), which does `**args`. Removing a parameter from a handler therefore turns a client that still sends it into a `TypeError`.
- **QA.**
  - `scripts/qa_fixture_recorder.py` `lifecycle_peek` (`:3015-3071`) calls the client directly with the old signatures and expects `'private'` back. `check_peek` is at `:950-998`.
  - The fixtures `tests/fixtures/live/peek/list_pages.json` (says `"private"`) and `get_comments.json` (no `verified`) predate the change.
  - The recorder's unit tests are in `tests/unit/test_qa_fixture_recorder.py`. `TestCheckPeek`'s fake is at `:4092-4131`. The lifecycle fake `_FakePeekClient` is at `:4185-4239`, with `upload_page(…, password, …)` at `:4197` and `set_visibility(…, password) -> str` at `:4204`. The recorder compares the `set_visibility` result to `"public"` at `:3032`.
  - The live-fixture parse tests in `tests/unit/test_peek_client.py` are at `:709-716` (list_pages) and `:719-727` (get_comments).
- **Rule catalogue test.** `tests/unit/policy/test_catalogue.py:164-168` asserts that a `peek.page` *read* compiles to exactly `peek.read_comments` and `peek.read_stats`. `tests/unit/policy/test_registry.py:310` asserts `len(_VERB_MATRIX) == 109`.
- **Website counts.** The Peek card in `website/connectors/index.html:173-175` carries `data-tools="8" data-auto="1" data-review="2" data-popup="5"`, checked against the code by `tests/unit/test_website_connectors_page.py`. The total `167 tools` is in `website/connectors/index.html:29` and `website/how-it-works/index.html:54`.
  - `--lifecycle` runs only in `connector-live-check.yml` (weekly, on `main`).
  - The QA server runs `ghcr.io/andras-tkcs/peek:main` (`docs/connector-qa.md:39`).
- **Docs.**
  - `docs/peek-setup.md`: what you need (`:8-16`, which calls the comment endpoint the fork's), the tool table (`:57-66`), the restricted rule (`:82-84`), passwords (`:94`), the renamed message (`:154-155`) and the restricted refusal (`:164-165`).
  - `docs/connector-qa.md`: `:236`, `:242`, `:388`, `:753-762`.
  - `README.md:80`.
- **ADRs.** No ADR records the visibility model or the dashboard-only rule for restricted pages. Related ADRs: 0153 (comments only through the account endpoint), 0154 (stats never carry IP or user agent), and 0158 (Peek rules name pages; *read* covers comments and stats, *add* covers new versions).
- **Related plan.** `docs/peek-auto-provisioning-plan.md` on `plan/peek-auto-provisioning` also changes `peek_client.py` (`PeekClientError.detail`, `save_token_file`). This plan adds `detail` the same way, so whichever lands second finds it present and keeps it.

## Design

### Visibility

- `peek_client.VISIBILITIES = ("public", "signed-in", "restricted")`. `PASSWORD_MAX_BYTES` is deleted.
- New `peek_client.normalize_visibility(value: str) -> str`:
  - strips the value;
  - maps `private` to `signed-in`;
  - `password` → `PeekClientError("Peek no longer has page passwords. Use restricted, then grant access to people with peek_grant_access.")`;
  - any value not in `VISIBILITIES` → `PeekClientError("visibility must be public, signed-in or restricted.")`.
- The connector's `_check_visibility` is replaced by a call to `normalize_visibility`, mapping `PeekClientError` to `ValueError` as `_check_slug` does.
- `_VISIBILITY_DESC` becomes exactly:

  `"Who can open the page: public (anyone with the link), signed-in (anyone signed in to Peek) or restricted (only you, Peek admins and the people or groups granted access)."`

  p3 appends ` Grant access with peek_grant_access.` once that tool exists.

- **New pages default to restricted.** `peek_upload_page` without a slug and without a visibility publishes `restricted`. Its tool description becomes exactly:

  `Publish an HTML page on Peek, or a new version of one of your pages, and get its share link. Give the page as html, local_path or upload_id. Give slug to replace what an existing page's link shows with this new version; its visibility stays as it is. A new page is restricted unless you choose otherwise: only you, Peek admins and the people granted access can open it. Returns {slug, url, visibility, version, version_url}. Use peek_set_visibility to change who can open a page later. Requires user approval.`

  The `visibility` parameter description is `_VISIBILITY_DESC + " Empty means restricted for a new page; leave it empty with slug."`.
- **Passwords leave the tool surface.** The `password` parameter is removed from both `ToolSpec`s and from `_PASSWORD`. The handlers keep a hidden keyword `password: str = ""`, so a client that still sends it gets a clear refusal instead of a `TypeError`. A non-empty value → `ValueError` with the `normalize_visibility` password message. The "Password: Set (not shown)" card lines go.
- **Restricted pages can be changed from PrivacyFence.** The refusal at `connectors/peek.py:550-554` goes. In `_set_visibility`:
  - The card always shows `Visibility: <old> → <new>`.
  - When `new == "restricted"` and `old != "restricted"`, `details_text` adds: `Only you, Peek admins and the people granted access can open it. Peek emails the link to everyone already granted access.` (Peek emails only on that change, `api_uploads.go:356`.)
  - When `old == "restricted"` and `new != "restricted"`, it adds: `The page is no longer limited to the people granted access. Their grants stay and apply again if the page is restricted later.`
  - The result is `{"slug", "visibility", "notified"}`.
  - The `peek_set_visibility` description is exactly: `Change who can open a Peek page: anyone with the link, anyone signed in to Peek, or only the people granted access (restricted). Returns {slug, visibility, notified}, where notified says whether Peek emailed the people granted access. Use peek_upload_page to publish a new version. Requires user approval.` p3 inserts ` Use peek_grant_access to choose who can open a restricted page.` before ` Requires user approval.`.
  - `write_effects.py` `peek_set_visibility` becomes: `Who can open the page changes. Its content and comments do not change. Making it restricted emails the link to everyone already granted access.`
- **Upload name.** The resolved `name` must be 1–200 characters, else `ValueError("name must be 1 to 200 characters.")`. This is checked before the gate.

### Comments

- `PeekComment` gains `verified: bool`, parsed with `_bool`, which is true only for JSON `true`. `peek_get_comments` returns it per comment. The card table gets a column `Signed in`, `yes` or `no`, after Author. The `peek_get_comments` description becomes exactly:

  `Read the reviewers' comments on one Peek page, with what each comment is anchored to. Comments belong to one version of the page: the latest unless you give version. Returns {slug, name, version, comments: [{id, author, verified, body, anchor_kind, selector, element_text, created_at, version}]}, where version is the one read and verified is true when a signed-in Peek account wrote the comment and false when a viewer typed the author name. Use peek_get_stats to see how often the page was opened and peek_add_comment to reply. Requires user approval.`

- `add_comment(slug, body, selector, element_text, version: int = 0)` sends `params={"v": str(version)}` when `version > 0`. Its 404/405 mapping, in this order:
  1. When `version > 0`, status is 404 and `detail == "version not found"` → `Peek has no version {version} of this page (HTTP 404). Retention may have removed it.`
  2. Otherwise the existing "no account comment endpoint" mapping (ADR 0153).
- `peek_add_comment` gains `version` (int, default 0), with the parameter description `Version number to comment on, from peek_list_versions; 0, the default, means the latest.`
  - `version < 0` → `ValueError("version must be 0 (the latest) or a version number.")`.
  - `version > page.version` → `ValueError(f"This page's latest version is {page.version}.")`, before the gate.
  - The card's `Version` row shows the chosen version, or the latest. The result's `version` is the one posted on, and the result's `comment` dict gains `verified`.
  - Its tool description becomes exactly (p2 text; p3 adds ` Use peek_list_versions to find a version.` before ` Requires user approval.`):

    `Post a comment on a Peek page under your own Peek account name, on the latest version or the one you give. Copy selector and element_text from a comment returned by peek_get_comments to reply at the same spot. Returns {slug, version, comment: {id, author, verified, body, anchor_kind, selector, element_text, created_at}}. Use peek_get_comments to read the existing comments. Requires user approval.`
- Limits are checked in UTF-8 bytes after `strip()`, as Peek does:
  - `body` 1–4000 bytes → `body must be 1 to 4000 bytes (UTF-8).`
  - `selector` ≤500 bytes → `selector must be 500 bytes or fewer (UTF-8).`
  - `element_text` ≤200 bytes → `element_text must be 200 bytes or fewer (UTF-8).`
  - Parameter descriptions say "bytes (UTF-8)".

### Client error handling (`peek_client.py`)

- `PeekClientError.__init__(message, *, status=None, detail="")`. `_send` passes `detail=_detail(response)` on every raise that follows an HTTP answer. Existing messages stay byte-identical. If `detail` already exists (from the auto-provisioning plan), keep it as it is.
- Delete `_RENAMED_PREFIX` and the renamed branch of the 404 mapping (`:36`, `:131-140`).
- 429 → `Peek is limiting how fast these requests can be made (HTTP 429). Wait a minute, then try again.`
- 415 → `f"Peek refused the file (HTTP 415): {detail}"` (Peek says `file does not look like HTML` or `unsupported content type`).
- Both go before the generic non-2xx branch.

### New client methods and results

```python
@dataclass(frozen=True)
class PeekVersion:
    version: int
    size_bytes: int
    created_at: str     # ISO, via _iso
    url: str

@dataclass(frozen=True)
class PeekGrant:
    kind: str           # "user" or "group"
    email: str
    created_at: str     # ISO, via _iso

@dataclass(frozen=True)
class PeekVisibilityResult:
    visibility: str
    notified: bool

GRANT_KINDS = ("user", "group")

def validate_grant(kind: str, email: str) -> tuple[str, str]
    # kind in GRANT_KINDS, else PeekClientError("kind must be user or group.")
    # email: strip + lower; must fullmatch r'[^@\s,;<>()\[\]"]+@[^@\s,;<>()\[\]"]+' and be at most 254 characters,
    # and email.utils.parseaddr(email) must return ("", email) (Peek checks mail.ParseAddress the same way);
    # else PeekClientError("Enter one email address, such as alice@example.com.")

class PeekClient:
    def upload_page(self, data: bytes, name: str, visibility: str, slug: str = "") -> PeekUploadResult
    def set_visibility(self, slug: str, visibility: str) -> PeekVisibilityResult
    def add_comment(self, slug: str, body: str, selector: str, element_text: str, version: int = 0) -> PeekComment
    def list_versions(self, slug: str) -> list[PeekVersion]                       # sorted by version, newest first
    def list_grants(self, slug: str) -> list[PeekGrant]                           # sorted by (kind, email)
    def add_grant(self, slug: str, kind: str, email: str) -> bool                 # returns notified
    def remove_grant(self, slug: str, kind: str, email: str) -> None
```

- `upload_page` without a slug sends `visibility` after `normalize_visibility`. With a slug, a non-empty visibility → `PeekClientError("A new version keeps the page's visibility; change it with set_visibility.")`. It never sends `password`.
- `set_visibility` sends `{"visibility": v}` only.
- `remove_grant` maps a 404 whose `detail == "grant not found"` to `PeekClientError("Peek has no such grant on this page (HTTP 404). Call peek_list_grants to see who has access.", status=404, detail=...)`.
- `add_grant` and `remove_grant` both map a 400 whose detail starts with `group grants need` (both routes check it, `api_grants.go:79`, `:132`) to `PeekClientError("This Peek server cannot check Google group membership, so it does not accept group grants. Grant access to people one by one, or ask the Peek admin to set up group lookup.", status=400, detail=...)`.
- Log lines carry the host and counts only. Never an email.

### New tools

| Tool | Gate | Verb | Operation | Effect text (`write_effects.py`) |
|---|---|---|---|---|
| `peek_list_versions` | `auto` | (none) | (none) | (read) |
| `peek_list_grants` | `review` | `READ` | `peek.read_grants` | (read) |
| `peek_grant_access` | `popup` | `UPDATE` | `peek.grant_access` | `The person or group can open the page whenever it is restricted. Peek emails them the link the first time they are granted access to a restricted page.` |
| `peek_revoke_access` | `popup` | `UPDATE` | `peek.revoke_access` | `The person or group can no longer open the page while it is restricted.` |

**No rule covers any of the grant tools.** `peek.page` in `policy/catalogue.py:80-84` gets `excludes=frozenset({"peek.read_grants", "peek.grant_access", "peek.revoke_access"})`, so a **Peek — page** rule keeps compiling to exactly `peek.read_comments` and `peek.read_stats` for *read*. `peek_list_grants` therefore always gets its review card, and grants and revokes always get a popup. The exclusion also holds if `peek.page` ever gains an UPDATE verb. Neither write is `destructive`.

The grantee's email appears in the approval card and in the audit entry's summary, as recipients do for Gmail writes. It never appears in a logger line.

**Tool specs** (descriptions exact):

- **`peek_list_versions(slug, reason)`**
  - Description: `List the versions of one Peek page you manage, newest first, with each version's size, time and link. Returns {slug, name, versions: [{version, size_bytes, created_at, url}]}. Use a version number with peek_get_comments or peek_add_comment. Auto-approved.`
  - Behaviour: `read_only=True`. It runs `get_page`, then `list_versions`. Audit with `_auto_audit("peek_list_versions", "List Peek Page Versions", f"Versions of {page.name}", f"{n} version(s)", t0)`.
- **`peek_list_grants(slug, reason)`**
  - Description: `List who can open one restricted Peek page: the people and Google groups granted access. Returns {slug, name, visibility, grants: [{kind, email, created_at}]}. Grants apply only while the page is restricted. Use peek_grant_access and peek_revoke_access to change them. Requires user approval.`
  - Behaviour: `read_only=True`, `gated_call(gate="review")`.
    - `tool_name="Read Peek Page Access"`, `summary=f"{n} grant(s) on {page.name}"`.
    - preview `{Server, Page, Link, Visibility}`.
    - `new_info={"Grants": str(n), "Email addresses": "The address of each person or group granted access"}`.
    - The table has headers `Kind`, `Email`, `Added`. `details_text` has one line per grant.
    - `pii_scan_text` is the emails joined, `table_only=True`, `args={"slug": slug}`.
- **`peek_grant_access(slug, email, kind="user", reason)`**
  - Description: `Let a person or a Google group open a restricted Peek page. Peek emails them the link when the page is restricted and they did not have access yet. Returns {slug, kind, email, notified}. Use peek_set_visibility to restrict the page, and peek_list_grants to see who has access. Requires user approval.`
  - `kind` parameter description: `user (one person's email, the default) or group (a Google group's email; needs group lookup on the Peek server).`
  - Behaviour: `read_only=False`. `validate_grant` runs first (`ValueError` on failure), then `get_page`, then `gated_call(gate="popup")`.
    - `tool_name="Grant Access to Peek Page"`, `summary=f"Let {email} open {page.name}"`.
    - preview `{Server, Page, Link, Visibility, "Grant to": f"{email} ({kind})"}`.
    - `details_text`: when `page.visibility == "restricted"`, `f"{email} can open {page.url}. Peek emails them the link unless they already had access."`; otherwise `f"{email} can open {page.url} once it is restricted. It is {page.visibility} now, so the grant changes nothing until then and no email is sent."`.
    - `args={"slug": slug}`.
    - After the gate it calls `add_grant`.
- **`peek_revoke_access(slug, email, kind="user", reason)`**
  - Description: `Remove a person's or a Google group's access to a restricted Peek page. Returns {slug, kind, email, removed}. Use peek_list_grants to see who has access. Requires user approval.`
  - Behaviour: as for grant, with `tool_name="Revoke Access to Peek Page"`, `summary=f"Remove {email} from {page.name}"`, preview `"Revoke from"` and `details_text=f"{email} can no longer open {page.url} while it is restricted."`.
  - Returns `removed: True` after `remove_grant`.

**Layout and siblings.**

- `gate.py` `_TOOL_LAYOUT`: `peek_list_grants` is `WIDE`.
- `PEEK_SIBLINGS` additions:
  - `peek_list_versions → (peek_get_comments,)`
  - `peek_list_grants → (peek_grant_access, peek_revoke_access)`
  - `peek_grant_access → (peek_set_visibility, peek_list_grants)`
  - `peek_revoke_access → (peek_list_grants,)`
  - `peek_set_visibility → (peek_upload_page, peek_grant_access)`
  - `peek_add_comment → (peek_get_comments, peek_list_versions)`
- The existing descriptions get the sibling names through the exact insertions named in "Visibility" and "Comments" (`_VISIBILITY_DESC`, `peek_set_visibility`, `peek_add_comment`).
- **Counts on the website** (checked by `tests/unit/test_website_connectors_page.py` and the tools reference):
  - In `website/connectors/index.html`, the Peek card becomes `data-tools="12" data-auto="2" data-review="3" data-popup="7"`, with the line `12 tools: 2 without a card · 3 reviewed · 7 need approval`.
  - The `<h1>` total (`167 tools`) rises by 4, and so does the total in `website/how-it-works/index.html:54`. Use the total that `docs/tools-reference.md` shows after regeneration.

### QA recorder

- `lifecycle_peek` uploads as `signed-in` and expects `signed-in` back. Then it:
  1. checks `set_visibility(slug, "public").visibility == "public"`;
  2. checks `list_versions(slug)` has exactly version 1;
  3. grants `qa-grantee@example.invalid` (kind user) while the page is public, so Peek sends no email, and checks `list_grants` shows it;
  4. revokes it and checks `list_grants` is empty;
  5. continues with rename and comment as today. The comment must come back with `verified is True`.
- The note becomes `upload, set_visibility, versions, grants, rename, add_comment, get_comments all verified`.
- `check_peek` needs no new recordings.
- The fixtures are re-recorded from the current QA server, so `list_pages.json` shows today's visibility values and `get_comments.json` carries `verified`.

### Rejected alternatives (become ADR text)

- **Keep `private` and `password` as PrivacyFence's own words and translate.** Rejected: the assistant would see a value in `peek_list_pages` that it must not send back. Passwords no longer exist to translate to.
- **Default new pages to `signed-in`, what `private` meant before.** Rejected: Peek's own default is `restricted`, and an assistant publishing a page should not open it to every Peek account unless asked.
- **Keep restricted pages dashboard-only.** Rejected: with `restricted` as the default, the assistant could create pages that it could then neither open up nor share. Grants are ordinary owner actions in Peek's API, and every grant and revoke gets a card no rule can skip.
- **Let a Peek page rule cover grants or the grant list.** Rejected: a grant sends an email to a new person and widens who can read the page. The grant list is email addresses, which the personal-data check would hold back anyway.
- **Drop the `password` argument silently.** Rejected: a caller that still sends a password would publish a page it believes is protected.

## ADRs

The next free numbers in `docs/adr` when the retirement phase runs: 0239 and 0240 if the auto-provisioning plan has not landed, 0242 and 0243 if it has.

- **A.** *The Peek connector uses Peek's own visibilities, and new pages are restricted.* It covers the three values, `private` accepted as an alias, passwords refused with a message, and the restricted default.
- **B.** *PrivacyFence manages who can open a restricted Peek page, always behind a card.* It covers the four tools, and the `peek.page` exclusion that keeps every grant tool out of rules. A Peek page rule still covers exactly what ADR 0158 says.

## Manual steps

The phases run against mocks. The QA fixture dispatch exercises the live list and comment reads, and `connector-live-check.yml` runs the new lifecycle weekly after merge. One check needs a second real person, so it is `manual_after`. See the step-by-step page linked in the manifest.

- **ma1:** a restricted page, a grant and a revoke, checked by a colleague's own browser and inbox.

## Risks and open questions

- **Live coverage.** `qa-record-fixture.yml` records only `--record` fixtures. The new versions and grants steps of `--lifecycle` first run live in the weekly `connector-live-check.yml` on `main` after merge, and ma1 covers grants by hand.
- **The `peek_client.py` merge with the auto-provisioning plan.** If `PeekClientError` already has `detail` when p1 starts, keep it and skip that step. If `_send` has changed shape in another way, stop with `status=blocked`.
- **Peek's error texts differ** from those quoted in "Current state" (for example `grant not found` or `group grants need`). Use the text Peek's `main` has, and say so in the phase result.
- **A test elsewhere sends `password`** to a Peek tool (grep `tests/` for `"password"` near `peek_`). It must now expect the refusal. Do not delete the test.
- **QA dispatch refused or the runner offline.** Stop p4 with `status=blocked` and say which. Do not mark the QA row done.

## Implementation manifest

```yaml
plan_slug: peek-api-alignment
feature_branch: feature/peek-api-alignment
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/Av9D5CRoDbcX5EfN3WxSHf
manual_steps_source: docs/peek-api-alignment-plan-manual-steps.html
manual_after:
  - id: ma1-restricted-share
    title: Restrict a page, grant a colleague, revoke them; they check their inbox and browser
    why: Only a second real person can confirm Peek's email and the access check end to end.
verify_after_merge:
  - python3 -m pytest tests/unit/test_peek_client.py tests/unit/connectors/test_peek_connector.py tests/unit/test_qa_fixture_recorder.py -q
  - python3 -m pytest tests/unit/test_docs_tools_reference.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_connector_tool_annotations.py tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_website_connectors_page.py -q
final_checks:
  - docs/peek-api-alignment-plan.md and docs/peek-api-alignment-plan-manual-steps.html are deleted and nothing links to them
  - both ADRs from the ADRs section exist, are Accepted, and are rows in docs/adr/README.md
  - CHANGELOG.md has the entries under "## [Unreleased]" and no new version heading
  - the PR description carries the qa-record-fixture.yml (connector=peek) result for this branch
  - grep -n 'PASSWORD_MAX_BYTES\|"password"' src/privacyfence/connectors/peek.py src/privacyfence/peek_client.py prints nothing
phases:
  - id: p1-client-additions
    title: Client additions only - error detail, 429/415, verified, versions and grants methods
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/peek_client.py
      - tests/unit/test_peek_client.py
    brief: |
      Must-read docs: CLAUDE.md, CONTRIBUTING.md, docs/coding-and-testing-guidelines.md and docs/testing-policy.md. Then read this plan's Design sections "Client error handling" and "New client methods and results" (only PeekVersion, PeekGrant, GRANT_KINDS, validate_grant, list_versions, list_grants, add_grant, remove_grant), and "Comments" (only PeekComment.verified), plus "Risks".
      This phase only adds. It does not change VISIBILITIES, PASSWORD_MAX_BYTES, _check_visibility, or the upload_page, set_visibility and add_comment signatures: connectors/peek.py still imports and calls them, and p2 changes both together.
      1. In src/privacyfence/peek_client.py, add:
         - PeekClientError.detail, and pass it from _send
         - the 429 and 415 messages
         - _bool and PeekComment.verified
         - PeekVersion, PeekGrant, GRANT_KINDS, validate_grant, list_versions, list_grants, add_grant and remove_grant, exactly as in Design, including the grant-not-found and group-lookup mappings on both grant routes
         Also delete _RENAMED_PREFIX and the renamed 404 branch, and update the module docstring to add "an email address of a grant".
      2. tests/unit/test_peek_client.py:
         - Remove the renamed-slug tests (:191-198) and add test_404_is_page_not_found_even_with_renamed_text.
         - Add tests for:
           - detail on 403, 404, 409 and 500
           - the 429 and 415 messages
           - verified parsed (true, false, absent → False)
           - list_versions sorted newest first with ISO times
           - list_grants sorted
           - add_grant body and notified
           - remove_grant body, and the grant-not-found message
           - the group-lookup message on add and on remove
           - validate_grant: "Alice@Example.com " → "alice@example.com"; refused: "a@b@c", "nobody", "a b@c.d", "a,b@c.d", "<a@b.c>" and a 255-character address; kind "team" refused
           - no log line contains an email (caplog)
      3. Run ruff check ., python3 -m mypy src/privacyfence and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/test_peek_client.py -q --cov=privacyfence.peek_client --cov-branch --cov-report=term-missing passes and reports 100% for peek_client.py
      - python3 -m pytest tests/unit -q passes
      - grep -n '_RENAMED_PREFIX' src/privacyfence/peek_client.py prints nothing
      - ruff check . passes

  - id: p2-visibility-and-comments
    title: Client and connector switch to Peek's visibilities together; restricted default; comment version
    depends_on: [p1-client-additions]
    complexity: M
    touches:
      - src/privacyfence/peek_client.py
      - src/privacyfence/connectors/peek.py
      - src/privacyfence/write_effects.py
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_peek_client.py
      - tests/unit/connectors/test_peek_connector.py
      - tests/unit/test_qa_fixture_recorder.py
      - docs/tools-reference.md
    brief: |
      Read this plan's Design sections "Visibility" and "Comments" in full, and the upload_page, set_visibility and add_comment signatures in "New client methods and results".
      1. src/privacyfence/peek_client.py:
         - VISIBILITIES; delete PASSWORD_MAX_BYTES; add normalize_visibility; delete _check_visibility.
         - The new upload_page, set_visibility (returns PeekVisibilityResult) and add_comment (version, with the 404 mapping order in Design) exactly as in Design.
         - Drop "a page password" from the docstring.
      2. src/privacyfence/connectors/peek.py, every item in Design "Visibility" and "Comments" with its exact text:
         - _VISIBILITY_DESC without the p3 sentence; delete _PASSWORD and the password ToolParams; the hidden password keyword refusal
         - the restricted default and its upload description
         - the name check
         - delete the restricted refusal; add the details_text additions and notified, and the set_visibility description without the p3 sentence
         - normalize_visibility in place of _check_visibility
         - verified, the "Signed in" column and the get_comments description
         - version (with both checks), the byte limits, verified in the result, and the add_comment description without the p3 sentence
         Drop "a page password" from the docstring.
      3. src/privacyfence/write_effects.py: the peek_set_visibility text from Design.
      4. scripts/qa_fixture_recorder.py lifecycle_peek: call upload_page with "signed-in" and no password, expect "signed-in", and call set_visibility(slug, "public").visibility (:3026-3032). The versions and grants steps are p4's.
         In tests/unit/test_qa_fixture_recorder.py, change _FakePeekClient (:4185-4239) to the new signatures.
      5. tests/unit/test_peek_client.py: rewrite the password and visibility tests (:539-607):
         - public, signed-in and restricted pass
         - private becomes signed-in
         - password and other values give the exact messages
         - upload_page never sends password
         - set_visibility returns notified
         - add_comment sends ?v only when version > 0
         - the version-not-found 404 vs the no-endpoint 404/405
      6. tests/unit/connectors/test_peek_connector.py:
         - Replace "private" fixture values with "signed-in".
         - Rewrite the password and visibility tests (:336-346, :368-371, :452-460, :524-580).
         - Add:
           - test_upload_defaults_to_restricted
           - test_upload_password_keyword_is_refused
           - test_set_visibility_password_keyword_is_refused
           - test_upload_private_is_published_as_signed_in
           - test_upload_name_over_200_characters_is_refused
           - test_set_visibility_changes_a_restricted_page
           - test_set_visibility_to_restricted_explains_emails (and the line is absent when already restricted)
           - test_set_visibility_from_restricted_keeps_grants_text
           - test_set_visibility_returns_notified
           - test_get_comments_returns_verified_and_signed_in_column
           - test_add_comment_on_a_version
           - test_add_comment_version_above_latest_is_refused_before_the_gate
           - test_add_comment_result_has_verified
           - test_add_comment_limits_are_bytes (1500 × "é" = 3000 bytes passes; 2001 × "é" = 4002 bytes is refused)
         - PEEK_SIBLINGS stays as it is in this phase.
      7. Regenerate docs/tools-reference.md (python3 scripts/generate_tools_reference.py).
      8. Run ruff check ., python3 -m mypy src/privacyfence and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_peek_connector.py -q --cov=privacyfence.connectors.peek --cov-branch --cov-report=term-missing passes and reports 100% for connectors/peek.py
      - python3 -m pytest tests/unit/test_peek_client.py -q --cov=privacyfence.peek_client --cov-branch --cov-report=term-missing passes and reports 100% for peek_client.py
      - python3 -m pytest tests/unit -q passes
      - grep -n "This page is restricted to people granted access in Peek" src/privacyfence/connectors/peek.py prints nothing
      - grep -n 'PASSWORD_MAX_BYTES\|"password"' src/privacyfence/connectors/peek.py src/privacyfence/peek_client.py prints nothing

  - id: p3-new-tools
    title: peek_list_versions, peek_list_grants, peek_grant_access, peek_revoke_access
    depends_on: [p2-visibility-and-comments]
    complexity: M
    touches:
      - src/privacyfence/connectors/peek.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - tests/unit/connectors/test_peek_connector.py
      - tests/unit/policy/test_registry.py
      - tests/unit/policy/test_catalogue.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
    brief: |
      Read this plan's Design section "New tools" (the table, the exclusion, the specs, layout, siblings and website counts), and the p3 insertions named in "Visibility" and "Comments".
      1. src/privacyfence/connectors/peek.py:
         - Add the four ToolSpecs after peek_rename_page, the call() branches, and the handlers _list_versions, _list_grants, _grant_access and _revoke_access, exactly as Design says.
         - Apply the three p3 sentence insertions (_VISIBILITY_DESC, peek_set_visibility, peek_add_comment).
      2. Register the tools:
         - auto_accept.py: TOOL_TO_OPERATION (:150-156: peek_list_grants, peek_grant_access and peek_revoke_access with their operations) and TOOL_TO_GATE (:378-386: all four).
         - policy/registry.py: TOOL_TO_VERB (:213-219).
         - policy/catalogue.py: the excludes on peek.page (:80-84).
         - write_effects.py: EFFECT_BY_TOOL, grant and revoke with the exact texts.
         - gate.py: _TOOL_LAYOUT, peek_list_grants WIDE.
      3. Tests:
         - tests/unit/policy/test_registry.py: the Peek block of the verb matrix (:281-287), and len(_VERB_MATRIX) at :310 rises by 3.
         - tests/unit/policy/test_catalogue.py: keep test_peek_page_read_compiles_comments_and_stats (:164-168) unchanged, since it must still pass. Add test_peek_page_rules_never_include_grant_operations, covering every verb.
         - tests/unit/connectors/test_peek_connector.py:
           - Update PEEK_SIBLINGS to Design's list, and add arg_overrides entries in TestEveryToolIsAudited.
           - One test per handler covering: the result shape; the card preview keys; grant details_text on a restricted and on a public page; validate_grant errors raised before the gate; list_versions audited as auto.
           - test_revoke_unknown_grant_message.
      4. Regenerate docs/tools-reference.md (python3 scripts/generate_tools_reference.py) and docs/always-allow-rules-reference.md (python3 scripts/generate_always_allow_reference.py).
      5. Update the website counts as Design says, taking the new total from the regenerated docs/tools-reference.md Total row.
      6. Run ruff check ., python3 -m mypy src/privacyfence, python3 scripts/mypy_strict_modules.py and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_peek_connector.py -q --cov=privacyfence.connectors.peek --cov-branch --cov-report=term-missing passes and reports 100% for connectors/peek.py
      - python3 -m pytest tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_connector_tool_annotations.py tests/unit/test_website_connectors_page.py -q passes
      - python3 -m pytest tests/unit/test_docs_tools_reference.py tests/unit/test_generate_always_allow_reference.py tests/unit/connectors/test_readme_manifest_alignment.py -q passes
      - grep -n '"peek_grant_access"' src/privacyfence/auto_accept.py src/privacyfence/policy/registry.py src/privacyfence/write_effects.py matches in all three
      - python3 -m pytest tests/unit -q --cov=privacyfence --cov-branch --cov-report=json:coverage.json && python3 scripts/check_coverage_floor.py coverage.json passes

  - id: p4-qa
    title: QA lifecycle for versions and grants; re-record the Peek fixtures
    depends_on: [p3-new-tools]
    complexity: S
    touches:
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/unit/test_peek_client.py
      - tests/fixtures/live/peek/*.json
      - docs/connector-qa.md
    brief: |
      Read this plan's Design section "QA recorder" and docs/connector-qa.md ("Reviewing recorded fixtures").
      1. scripts/qa_fixture_recorder.py lifecycle_peek: add the steps and the note exactly as Design says.
      2. tests/unit/test_qa_fixture_recorder.py: extend _FakePeekClient with list_versions, list_grants, add_grant and remove_grant, and add:
         - test_lifecycle_peek_checks_versions_and_grants (the calls in order)
         - test_lifecycle_peek_fails_when_grant_not_listed
         - test_lifecycle_peek_fails_when_comment_not_verified
      3. docs/connector-qa.md:
         - :39: drop "The fork's"; the image is Peek's own.
         - :236: "upload a signed-in page".
         - :242 and :388: the lifecycle list adds versions and grants.
         - :753-762 Peek checks:
           - the popup list adds peek_grant_access and peek_revoke_access
           - the review list adds peek_list_grants
           - peek_list_versions is auto
           - replace the "stock Peek server without the fork's endpoint" sentence with "A Peek server without the account comment endpoint answers 404 or 405, and the tool says so."
      4. Push this phase's branch, then dispatch qa-record-fixture.yml on main with connector=peek and branch=<this phase's branch>.
         - Wait for it, pull its commit, and review the fixture diff per docs/connector-qa.md: no real account identifier, URL, token or private content.
         - Extend the get_comments live-fixture test in tests/unit/test_peek_client.py (:719-727) to assert verified is a bool.
         - Put the run URL in the phase result. If the dispatch is refused, see Risks.
    acceptance:
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py -k peek -q passes
      - python3 -m pytest tests/unit/test_peek_client.py -k live -q passes
      - grep -c '"private"' tests/fixtures/live/peek/list_pages.json prints 0
      - the qa-record-fixture.yml run for connector=peek on this branch concludes success

  - id: p5-docs
    title: Peek setup guide, README and website wording
    depends_on: [p3-new-tools]
    complexity: S
    touches:
      - docs/peek-setup.md
      - README.md
      - website/connectors/peek/index.html
      - website/connectors/index.html
    brief: |
      Read this plan's Design in full; the docs describe the result. Describe current behaviour only (guidelines §5): no "since", "no longer", "older servers" or "removed".
      1. docs/peek-setup.md:
         - "What you need" (:8-16): the account comment endpoint is part of Peek; a Peek server without it answers 404 or 405, and peek_add_comment says so.
         - Tool table (:57-66): add the four tools with their gates.
         - :81: peek_add_comment posts on the latest version or the one you give.
         - Rewrite the notes at :82-84. Cover:
           - the three visibilities, and that new pages are restricted
           - grants apply only while a page is restricted
           - Peek emails a grantee when the page is restricted and they did not have access yet, and emails everyone granted when a page becomes restricted
           - group grants need Peek's Google group lookup
           - grant and revoke always ask, and peek_list_grants always shows a review card: no Peek page rule covers them
         - Privacy (:94): replace the password line with "Grant email addresses are shown on the review card and the approval cards, and are personal data."
         - Troubleshooting:
           - delete the renamed-slug entry (:154-155) and the restricted refusal (:164-165)
           - add entries for: the password message, the visibility message, the 429 and 415 messages, the comment version 404, "This page's latest version is N.", the grant-not-found message, the group-lookup message, the validate_grant messages, and "name must be 1 to 200 characters."
      2. README.md:80, the Peek row: "List your shared HTML pages and their versions; read comments, visit counts and who has access after review; publish pages and new versions, choose who can open them, rename, comment on and delete them".
      3. website/connectors/index.html, Peek card dd lines (:176-178):
         - "The list of your shared pages, and a page's versions."
         - "A page's comments, its visit counts, and who has access."
         - "Publishing a page or a new version of one, changing who can open a page and granting or removing access, renaming it, commenting on it and deleting it."
      4. website/connectors/peek/index.html (:42-50), the same three sentences in its three cards:
         - "Your page list and a page's versions: names, links, sizes and who can open it. The call is still audited."
         - "The comments reviewers left on a page, how often it was opened, and who has access. Visit counts never include IP addresses."
         - "Publishing a page or a new version, changing who can open a page, granting or removing access, commenting on a page, renaming it and deleting it."
      5. Run python3 -m pytest tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_website_claims.py -q.
    acceptance:
      - grep -n "peek_grant_access" docs/peek-setup.md matches
      - grep -n "dashboard (Access)\|refuses to change a page into or out of" docs/peek-setup.md prints nothing
      - grep -n "who has access" README.md matches
      - python3 -m pytest tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_website_claims.py -q passes

  - id: p6-retire
    title: ADRs, changelog, delete the plan
    depends_on: [p4-qa, p5-docs]
    complexity: S
    touches:
      - docs/adr/*.md
      - docs/adr/README.md
      - CHANGELOG.md
      - docs/peek-api-alignment-plan.md
      - docs/peek-api-alignment-plan-manual-steps.html
    brief: |
      Read docs/adr/README.md (template and rules; one decision per ADR).
      1. Write ADRs A and B from this plan's ADRs section, numbered as it says (the next free numbers in docs/adr), Status Accepted.
         - Context: Peek's migrations 14 and 15, and its restricted default.
         - Include the matching rejected alternatives from Design.
         - B notes that ADR 0158's rule coverage is unchanged.
         - Link source files and https://github.com/andras-tkcs/peek/blob/main/docs/src/content/docs/auth-access.md, never this plan.
         - File names: `<n>-the-peek-connector-uses-peeks-visibilities-and-new-pages-are-restricted.md` and `<n>-privacyfence-manages-who-can-open-a-restricted-peek-page-behind-a-card.md`.
      2. docs/adr/README.md: add both rows.
      3. CHANGELOG.md under "## [Unreleased]", in the file's bold-lead style, adding the ### headings if absent:
         - ### Fixed: "- **Peek pages publish again on current Peek servers.** peek_upload_page and peek_set_visibility use Peek's public, signed-in and restricted, and a new page is restricted unless you choose otherwise; a page password is refused with a message that says to restrict the page and grant access."
         - ### Added: "- **Choose who can open a restricted Peek page.** peek_grant_access and peek_revoke_access (each with an approval card), peek_list_grants and peek_list_versions; comments say whether a signed-in account wrote them, and peek_add_comment can target a version."
      4. git rm docs/peek-api-alignment-plan.md docs/peek-api-alignment-plan-manual-steps.html, and grep that nothing links to them.
      5. Run ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - test ! -e docs/peek-api-alignment-plan.md && test ! -e docs/peek-api-alignment-plan-manual-steps.html
      - grep -rn "peek-api-alignment-plan" docs src README.md CHANGELOG.md prints nothing
      - grep -n "uses-peeks-visibilities" docs/adr/README.md matches
      - python3 -m pytest tests/unit -q passes
```
