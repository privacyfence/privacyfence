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
  - The fixtures `tests/fixtures/live/peek/list_pages.json` (says `"private"`) and `get_comments.json` (no `verified`) predate the change. The recorder's unit tests are in `tests/unit/test_qa_fixture_recorder.py` (`:4092-4131`).
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

  `"Who can open the page: public (anyone with the link), signed-in (anyone signed in to Peek) or restricted (only you, Peek admins and the people or groups you grant access with peek_grant_access)."`

- **New pages default to restricted.** `peek_upload_page` without a slug and without a visibility publishes `restricted`.
  - Its description says: `A new page is restricted unless you choose otherwise: only you, Peek admins and the people you grant access can open it.`
  - The `visibility` parameter description is `_VISIBILITY_DESC + " Empty means restricted for a new page; leave it empty with slug."`
- **Passwords leave the tool surface.** The `password` parameter is removed from both `ToolSpec`s and from `_PASSWORD`. The handlers keep a hidden keyword `password: str = ""`, so a client that still sends it gets a clear refusal instead of a `TypeError`. A non-empty value → `ValueError` with the `normalize_visibility` password message. The "Password: Set (not shown)" card lines go.
- **Restricted pages can be changed from PrivacyFence.** The refusal at `connectors/peek.py:550-554` goes. In `_set_visibility`:
  - The card always shows `Visibility: <old> → <new>`.
  - When `new == "restricted"`, `details_text` adds: `Only you, Peek admins and the people granted access can open it. Peek emails the link to everyone already granted access.`
  - When `old == "restricted"` and `new != "restricted"`, it adds: `The page is no longer limited to the people granted access. Their grants stay and apply again if the page is restricted later.`
  - The result is `{"slug", "visibility", "notified"}`.
  - The `peek_set_visibility` description is: `Change who can open a Peek page: anyone with the link, anyone signed in to Peek, or only the people granted access (restricted). Returns {slug, visibility, notified}, where notified says whether Peek emailed the people granted access. Use peek_grant_access to choose who can open a restricted page. Requires user approval.`
- **Upload name.** The resolved `name` must be 1–200 characters, else `ValueError("name must be 1 to 200 characters.")`. This is checked before the gate.

### Comments

- `PeekComment` gains `verified: bool`, parsed with `_bool`, which is true only for JSON `true`. `peek_get_comments` returns it per comment.
  - The card table gets a column `Signed in`, `yes` or `no`, after Author.
  - The tool description lists `verified` and says: `verified is true when a signed-in Peek account wrote the comment and false when a viewer typed the author name.`
- `add_comment(slug, body, selector, element_text, version: int = 0)` sends `params={"v": str(version)}` when `version > 0`. A 404 with a version maps to the same message as `get_comments`: `Peek has no version {n} of this page (HTTP 404). Retention may have removed it.`
- `peek_add_comment` gains `version` (int, default 0). Its description: `Version number to comment on, from peek_list_versions; 0, the default, means the latest.`
  - The card's `Version` row shows the chosen version, or the latest.
  - The result's `version` is the one posted on.
  - The tool description drops "It goes on the page's latest version".
- Limits are checked in UTF-8 bytes after `strip()`, as Peek does:
  - `body` 1–4000 bytes → `body must be 1 to 4000 bytes (UTF-8).`
  - `selector` ≤500 bytes → `selector must be 500 bytes or fewer (UTF-8).`
  - `element_text` ≤200 bytes → `element_text must be 200 bytes or fewer (UTF-8).`
  - Parameter descriptions say "bytes (UTF-8)".

### Client error handling (`peek_client.py`)

- `PeekClientError.__init__(message, *, status=None, detail="")`. `_send` passes `detail=_detail(response)` on every raise that follows an HTTP answer. Existing messages stay byte-identical. If `detail` already exists (from the auto-provisioning plan), keep it as it is.
- Delete `_RENAMED_PREFIX` and the renamed branch of the 404 mapping (`:36`, `:131-140`).
- 429 → `Peek is limiting how fast this account can do that (HTTP 429). Wait a minute, then try again.`
- 415 → `Peek refused the page: it does not look like HTML (HTTP 415).`
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
    # email: strip + lower; exactly one "@" with non-empty parts and no whitespace; at most 254 characters;
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
- `add_grant` maps a 400 whose detail starts with `group grants need` to `PeekClientError("This Peek server cannot check Google group membership, so it does not accept group grants. Grant access to people one by one, or ask the Peek admin to set up group lookup.", status=400, detail=...)`.
- Log lines carry the host and counts only. Never an email.

### New tools

| Tool | Gate | Verb | Operation | Effect text (`write_effects.py`) |
|---|---|---|---|---|
| `peek_list_versions` | `auto` | (none) | (none) | (read) |
| `peek_list_grants` | `review` | `READ` | `peek.read_grants` | (read) |
| `peek_grant_access` | `popup` | `UPDATE` | `peek.grant_access` | `The person or group can open the page whenever it is restricted. Peek emails them the link when it is restricted now.` |
| `peek_revoke_access` | `popup` | `UPDATE` | `peek.revoke_access` | `The person or group can no longer open the page while it is restricted. Links they already have stop working for them.` |

A **Peek — page** *read* rule names slugs, and it now also covers `peek_list_grants` for those pages, because the rule matches on `slug`. No rule ever covers a grant or a revoke, since `peek.page` has no UPDATE verb. Neither is `destructive`.

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
  - Description: `Let a person or a Google group open a restricted Peek page. Peek emails them the link when the page is restricted now. Returns {slug, kind, email, notified}. Use peek_set_visibility to restrict the page, and peek_list_grants to see who has access. Requires user approval.`
  - `kind` parameter description: `user (one person's email, the default) or group (a Google group's email; needs group lookup on the Peek server).`
  - Behaviour: `read_only=False`. `validate_grant` runs first (`ValueError` on failure), then `get_page`, then `gated_call(gate="popup")`.
    - `tool_name="Grant Access to Peek Page"`, `summary=f"Let {email} open {page.name}"`.
    - preview `{Server, Page, Link, Visibility, "Grant to": f"{email} ({kind})"}`.
    - `details_text`: when `page.visibility == "restricted"`, `f"{email} can open {page.url}. Peek emails them the link."`; otherwise `f"{email} can open {page.url} once it is restricted. It is {page.visibility} now, so the grant changes nothing until then and no email is sent."`.
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
- The existing descriptions get the sibling names they now reference.

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
- **Let a Peek page rule cover grants.** Rejected: a grant sends an email to a new person and widens who can read the page.
- **Drop the `password` argument silently.** Rejected: a caller that still sends a password would publish a page it believes is protected.

## ADRs

The next free numbers in `docs/adr` when the retirement phase runs: 0239 and 0240 if the auto-provisioning plan has not landed, 0242 and 0243 if it has.

- **A.** *The Peek connector uses Peek's own visibilities, and new pages are restricted.* It covers the three values, `private` accepted as an alias, passwords refused with a message, and the restricted default.
- **B.** *PrivacyFence manages who can open a restricted Peek page, always behind a card.* It covers the four tools, `peek_list_grants` as a *read* a Peek page rule covers (it extends ADR 0158), and grants and revokes never being coverable.

## Manual steps

The phases run against mocks. The QA fixture dispatch exercises the live list and comment reads, and `connector-live-check.yml` runs the new lifecycle weekly after merge. One check needs a second real person, so it is `manual_after`. See the step-by-step page linked in the manifest.

- **ma1:** a restricted page, a grant and a revoke, checked by a colleague's own browser and inbox.

## Risks and open questions

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
  - python3 -m pytest tests/unit/test_docs_tools_reference.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_connector_tool_annotations.py tests/unit/policy tests/unit/test_write_effects.py -q
final_checks:
  - docs/peek-api-alignment-plan.md and docs/peek-api-alignment-plan-manual-steps.html are deleted and nothing links to them
  - both ADRs from the ADRs section exist, are Accepted, and are rows in docs/adr/README.md
  - CHANGELOG.md has the entries under "## [Unreleased]" and no new version heading
  - the PR description carries the qa-record-fixture.yml (connector=peek) result for this branch
  - grep -rn -i "password" src/privacyfence/connectors/peek.py src/privacyfence/peek_client.py docs/peek-setup.md shows only the refusal message and the hidden keyword
phases:
  - id: p1-client
    title: Client follows Peek's visibilities, comments, errors; versions and grants methods
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/peek_client.py
      - tests/unit/test_peek_client.py
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
    brief: |
      Must-read docs: CLAUDE.md, CONTRIBUTING.md, docs/coding-and-testing-guidelines.md and docs/testing-policy.md. Then read this plan's Design sections "Visibility" (the client part), "Comments" (the client part), "Client error handling" and "New client methods and results", and "Risks".
      1. src/privacyfence/peek_client.py:
         - VISIBILITIES; delete PASSWORD_MAX_BYTES; add normalize_visibility; delete _check_visibility.
         - PeekClientError.detail, and pass it from _send.
         - Delete the renamed branch and _RENAMED_PREFIX.
         - Add the 429 and 415 messages.
         - Add PeekComment.verified and _bool.
         - Change the upload_page, set_visibility and add_comment signatures.
         - Add PeekVersion, PeekGrant, PeekVisibilityResult, GRANT_KINDS, validate_grant, list_versions, list_grants, add_grant and remove_grant.
         All of these exactly as in Design. Update the module docstring: drop "a page password", add "an email address of a grant".
      2. scripts/qa_fixture_recorder.py lifecycle_peek (:3015-3071): change only the calls to the new signatures, uploading "signed-in" and expecting "signed-in" (the lifecycle steps for versions and grants are p4's). Update tests/unit/test_qa_fixture_recorder.py where it builds the Peek fakes (:4092-4131), so the suite passes.
      3. tests/unit/test_peek_client.py:
         - Rewrite the password and visibility tests (:539-607) for the new rules: public, signed-in and restricted pass; private becomes signed-in; password gives the exact message; anything else gives the exact message.
         - Remove the renamed-slug tests (:191-198), which assert behaviour that no longer exists, and add test_404_is_page_not_found_even_with_renamed_text.
         - Add tests for:
           - detail on 403, 404, 409 and 500
           - 429 and 415 messages
           - verified parsed (true, false, absent → False)
           - add_comment sends ?v only when version > 0
           - add_comment 404 with a version
           - upload_page never sends password and sends the normalised visibility
           - set_visibility returns notified
           - list_versions sorted newest first, with ISO times
           - list_grants sorted
           - add_grant body and notified
           - remove_grant body, and the "grant not found" message
           - the group-lookup message
           - validate_grant (cases: "Alice@Example.com " → "alice@example.com"; "a@b@c", "nobody", "a b@c.d" and a 255-character address refused; kind "team" refused)
           - no log line contains an email (caplog)
      4. Run ruff check ., python3 -m mypy src/privacyfence and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/test_peek_client.py -q --cov=privacyfence.peek_client --cov-branch --cov-report=term-missing passes and reports 100% for peek_client.py
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py -q passes
      - grep -n '_RENAMED_PREFIX\|PASSWORD_MAX_BYTES' src/privacyfence/peek_client.py prints nothing
      - ruff check . passes

  - id: p2-connector-visibility-comments
    title: Connector uses the new visibilities, restricted default, comment version and verified
    depends_on: [p1-client]
    complexity: M
    touches:
      - src/privacyfence/connectors/peek.py
      - tests/unit/connectors/test_peek_connector.py
      - docs/tools-reference.md
    brief: |
      Read this plan's Design sections "Visibility" and "Comments" (the connector parts).
      1. src/privacyfence/connectors/peek.py:
         - _VISIBILITY_DESC exact; delete _PASSWORD; remove the password ToolParams.
         - New descriptions for peek_upload_page, peek_set_visibility, peek_get_comments and peek_add_comment, exact where Design gives the text.
         - The hidden password keyword refusal in _upload_page and _set_visibility.
         - The restricted default.
         - The name length check.
         - Delete the restricted refusal; add the details_text additions and the notified result in _set_visibility.
         - Replace _check_visibility with normalize_visibility.
         - The verified field and the "Signed in" column in _get_comments.
         - The version parameter, version validation (version < 0 → ValueError("version must be 0 (the latest) or a version number.")) and the byte limits in _add_comment.
         Update the module docstring: drop "a page password".
      2. Regenerate docs/tools-reference.md with python3 scripts/generate_tools_reference.py.
      3. tests/unit/connectors/test_peek_connector.py:
         - Replace every "private" fixture value with "signed-in".
         - Rewrite the password and visibility tests (:336-346, :368-371, :452-460, :524-580) for the new rules, including:
           - test_upload_defaults_to_restricted
           - test_upload_password_keyword_is_refused
           - test_upload_private_is_published_as_signed_in
           - test_upload_name_over_200_characters_is_refused
           - test_set_visibility_changes_a_restricted_page
           - test_set_visibility_to_restricted_explains_emails
           - test_set_visibility_returns_notified
           - test_get_comments_returns_verified_and_signed_in_column
           - test_add_comment_on_a_version
           - test_add_comment_limits_are_bytes (a 1500-character body of "é", 3000 bytes, passes; one of 2001 "é", 4002 bytes, is refused)
         - Keep every test that is not about passwords or visibility values unchanged.
      4. Run ruff check ., python3 -m mypy src/privacyfence and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_peek_connector.py -q passes
      - python3 -m pytest tests/unit/test_docs_tools_reference.py tests/unit/connectors/test_readme_manifest_alignment.py -q passes
      - grep -n "This page is restricted to people granted access in Peek" src/privacyfence/connectors/peek.py prints nothing
      - ruff check . passes

  - id: p3-new-tools
    title: peek_list_versions, peek_list_grants, peek_grant_access, peek_revoke_access
    depends_on: [p2-connector-visibility-comments]
    complexity: M
    touches:
      - src/privacyfence/connectors/peek.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - tests/unit/connectors/test_peek_connector.py
      - tests/unit/policy/test_registry.py
      - tests/unit/policy/test_scopes.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
    brief: |
      Read this plan's Design section "New tools" (the table, the specs, the layout and siblings).
      1. src/privacyfence/connectors/peek.py: add the four ToolSpecs after peek_rename_page, the call() branches, and the handlers _list_versions, _list_grants, _grant_access and _revoke_access exactly as Design says. Add the sibling references to the existing descriptions.
      2. Register the tools:
         - auto_accept.py TOOL_TO_OPERATION (:150-156: the three non-auto tools with their operations) and TOOL_TO_GATE (:378-386: all four).
         - policy/registry.py TOOL_TO_VERB (:213-219).
         - write_effects.py EFFECT_BY_TOOL (:182-195: grant and revoke, exact texts).
         - gate.py _TOOL_LAYOUT (:275-276: peek_list_grants WIDE).
      3. Tests:
         - tests/unit/policy/test_registry.py: add the tools to the Peek block of the verb matrix (:281-287).
         - tests/unit/policy/test_scopes.py: test_peek_page_read_rule_covers_list_grants and test_peek_page_rule_never_covers_grant_or_revoke.
         - tests/unit/connectors/test_peek_connector.py:
           - PEEK_SIBLINGS and arg_overrides in TestEveryToolIsAudited
           - one test per handler: the result shape; the card preview keys; details_text for a restricted and for a public page on grant; validate_grant errors raise before the gate; list_versions is audited as auto
           - test_revoke_unknown_grant_message
      4. Regenerate docs/tools-reference.md (python3 scripts/generate_tools_reference.py) and docs/always-allow-rules-reference.md (python3 scripts/generate_always_allow_reference.py).
      5. Run ruff check ., python3 -m mypy src/privacyfence, python3 scripts/mypy_strict_modules.py and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_peek_connector.py tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_connector_tool_annotations.py -q passes
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
      1. scripts/qa_fixture_recorder.py lifecycle_peek: add the steps and note exactly as Design says.
      2. tests/unit/test_qa_fixture_recorder.py: extend the Peek lifecycle tests with these cases:
         - test_lifecycle_peek_checks_versions_and_grants (a fake client records the calls in order)
         - test_lifecycle_peek_fails_when_grant_not_listed
         - test_lifecycle_peek_fails_when_comment_not_verified
      3. docs/connector-qa.md:
         - :236: "upload a signed-in page".
         - :242 and :388: the lifecycle list adds versions and grants.
         - :753-762 Peek checks:
           - the popup list adds peek_grant_access and peek_revoke_access
           - the review list adds peek_list_grants
           - peek_list_versions is auto
           - drop "fork's"/"stock Peek server without the fork's endpoint", since the account comment endpoint is in Peek itself
      4. Push this phase's branch, then dispatch qa-record-fixture.yml on main with connector=peek and branch=<this phase's branch>.
         - Wait for it, pull its commit, and review the fixture diff per docs/connector-qa.md: no real account identifier, URL, token or private content.
         - The new list_pages.json shows a current visibility value, and get_comments.json carries verified.
         - The live-fixture parse test in tests/unit/test_peek_client.py (:709-716) must pass on them; extend it to assert verified is a bool.
         - Put the run URL into the phase result. If the dispatch is refused, see Risks.
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
      Read this plan's Design in full; the docs describe what it built.
      1. docs/peek-setup.md:
         - "What you need" (:8-16): the account comment endpoint is part of Peek (keep the 404/405 note for older servers); the new tools need a Peek with versions and grants (any Peek release since page passwords were removed).
         - Tool table (:57-66): add the four tools with their gates.
         - Rewrite the notes at :82-84. Cover:
           - the three visibilities, with new pages restricted
           - grants apply only while restricted
           - Peek emails a grantee when the page is restricted
           - group grants need Peek's Google group lookup
           - grant and revoke always ask, even with a rule
           - a Peek — page read rule also covers peek_list_grants
         - Privacy (:94): replace the password line with "Grant email addresses are shown on the review card and are personal data."
         - Troubleshooting:
           - delete the renamed-slug entry (:154-155) and the restricted refusal (:164-165)
           - add the password message, the visibility message, the 429 and 415 messages, the version 404 for comments, the grant-not-found message, the group-lookup message, the validate_grant messages, and "name must be 1 to 200 characters."
      2. README.md:80: mention managing who can open a page.
      3. website/connectors/peek/index.html (:42-50) and website/connectors/index.html (:179): list grants and versions in the capability line. Run python3 -m pytest tests/unit/test_website_connector_pages.py -q.
    acceptance:
      - grep -n "peek_grant_access" docs/peek-setup.md README.md matches in both
      - grep -n "Change who can open it in Peek's dashboard" docs/peek-setup.md prints nothing
      - python3 -m pytest tests/unit/test_website_connector_pages.py -q passes

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
         - Context: Peek's change (migrations 14 and 15, its restricted default).
         - Include the matching rejected alternatives from Design.
         - B says it extends ADR 0158 (a page read rule now also covers peek_list_grants).
         - Link source files, and https://github.com/andras-tkcs/peek/blob/main/docs/src/content/docs/auth-access.md, never this plan.
         - File names: `<n>-the-peek-connector-uses-peeks-visibilities-and-new-pages-are-restricted.md` and `<n>-privacyfence-manages-who-can-open-a-restricted-peek-page-behind-a-card.md`.
      2. docs/adr/README.md: add both rows.
      3. CHANGELOG.md under "## [Unreleased]", in the file's bold-lead style (add the ### headings if absent):
         - ### Fixed: "- **Peek pages publish again on current Peek servers.** Peek removed page passwords and renamed private to signed-in; peek_upload_page and peek_set_visibility now use public, signed-in and restricted, and new pages are restricted, as in Peek."
         - ### Added: "- **Choose who can open a restricted Peek page.** peek_grant_access and peek_revoke_access (each with an approval card), peek_list_grants and peek_list_versions; comments now say whether a signed-in account wrote them, and peek_add_comment can target a version."
      4. git rm docs/peek-api-alignment-plan.md docs/peek-api-alignment-plan-manual-steps.html, and grep that nothing links to them.
      5. Run ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - test ! -e docs/peek-api-alignment-plan.md && test ! -e docs/peek-api-alignment-plan-manual-steps.html
      - grep -rn "peek-api-alignment-plan" docs src README.md CHANGELOG.md prints nothing
      - grep -n "uses-peeks-visibilities" docs/adr/README.md matches
      - python3 -m pytest tests/unit -q passes
```
