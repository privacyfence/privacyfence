# Miro connector plan

## 1. Goal

Add a **Miro** connector. An AI client can then list a person's Miro boards and tags straight
away. It can read a board's items and connector lines after the person reviews them. With approval
on a card, it can add sticky notes, cards, texts, shapes, frames and connector lines, and change
existing items (text, color, position, size, moving into a frame) and a board's name or
description. Nothing the connector does deletes anything.

People connect only by OAuth, with a Miro app an administrator registers once and puts in the
organization config bundle. Sign-in works on a desktop install (**Authenticate…** in Settings, or
`--miro-oauth`) and on an organization server (**Connect** on `/connect`), the same way as
Salesforce.

The maintainer confirmed the scope while planning:
- read, create and update, never delete;
- OAuth only, with `boards:read` and `boards:write`;
- no Enterprise APIs (organizations, teams, projects, audit logs, board export or eDiscovery,
  legal holds, content logs, data classification).

Board comments are left out: Miro's public REST API has no endpoint that reads them (§3.1). There is
no tracking issue; this plan is the whole specification.

## 2. Current state

Line numbers are on `main` at `6787aefb` (v6.8.1). The plan was first written against `33757024`
and re-verified against `6787aefb`.

- **Connectors** are `Connector` subclasses (`src/privacyfence/connector.py:78-96`).
  - `ToolParam` (`:16-24`) takes the annotations `"str"`, `"int"`, `"bool"` and `"float"`.
  - `ToolSpec` (`:27-75`) has `read_only` and `destructive`, which are truthful annotations (ADR 0089).
  - Connectors are discovered automatically by `connector_catalog.connector_classes()`
    (`src/privacyfence/connector_catalog.py:20-29`).
- **The template connector** is `connectors/grist.py`:
  - `_fetch` (`:1540-1545`) wraps the client in `asyncio.to_thread` and re-raises `GristClientError`
    as `RuntimeError`.
  - `_auto_audit` (`:1547-1566`) audits ungated tools.
  - The review-gated read is `_get_records` (`:767-843`).
  - The popup write with an old→new table is `_update_records` (`:1065-1127`). It validates, fetches
    the current values, calls `gated_call(gate="popup", …)` (`:1092`), and only then writes.
  - Grist's `tool_specs()` and `call()` now go through `export_files.visible_specs` and
    `export_files.refuse_if_exports_off`. That is only for export tools; Miro has none and does
    not copy it.
- **OAuth templates.**
  - `grist_auth.py` covers the bundle section, the token file, refresh and
    `authorize_interactive`: `bundle_settings` `:122-144`, `save_token_file` `:157-159`,
    `load_token_file` `:169-188`, `exchange_code` `:397-424`, `refresh` `:427-447`,
    `GristTokenProvider` `:450-470`, `authorize_interactive` `:473-490`.
  - `oauth_loopback.run_browser_oauth(build_authorize_url, exchange, port, path="/callback",
    timeout=180.0, open_browser=None, redirect_host="127.0.0.1")` is at `oauth_loopback.py:148-156`.
    Its two callbacks take `(redirect_uri, state, code_challenge)` and
    `(code, redirect_uri, code_verifier)`.
  - Ports in use are 53682 (Slack), 53683 (Salesforce), 53684 (Atlassian) and 53685 (Grist).
    **53686 is free** (nothing in the repository names it).
- **Which connectors an organization offers** is one table, `org_mode.CONNECTOR_ORG_SECTION`
  (`src/privacyfence/org_mode.py:520-529`, ADR 0196), read by `connector_enabled_by_org`
  (`:532-534`). `settings_controller.ORG_CONFIG_SERVICE` (`:129`) is an alias of it, and
  `ORG_BUNDLE_SERVICES` (`:130`) lists the bundle sections. `connector_enabled_by_org` indexes the
  table for every connector in `ALL_CONNECTORS`, so a connector missing from it raises `KeyError`
  in `_connectors_state`, `_org_allows` and `web/routes_connect.py`.
- **Daemon wiring.**
  - `daemon_main.TOKEN_FILES` (`:194-207`) and `_resolve_path` (`:322-340`).
  - `build_connectors()` (`:1342-1718`).
    - The Salesforce block (`:1533-1552`) raises `"… organization config not installed"` when the
      bundle lacks its section.
    - The Grist block (`:1699-1716`), like Telegram's (`:1653`) and Peek's (`:1687`), opens with
      `if not org_mode.connector_enabled_by_org(org_config, "grist"): raise …("Grist organization config not installed")`.
    - The Jira Assets block (`:1622-1644`) uses its own `assets_enabled()`; it is not a template.
  - `_classify_connector_failure` (`:1293-1325`) maps `"Use Authenticate…"` to `not_authenticated`
    (`:1321`) and `"organization config not installed"` to `no_org_config` (`:1323`).
  - `run_grist_oauth` (`:1892-1904`), the `--grist-oauth` flag (`:2334`), the `oauth_flag` chain
    (`:2461-2467`) and the dispatch (`:2497-2498`).
  - `tests/unit/test_systemic_gate_invariants.py` `TOKEN_WRITE_SITES` (`:182-195`, alphabetical) lists
    12 writers, pinned by `test_twelve_token_write_sites_are_listed` (`:255-260`).
- **Settings (local mode).** In `settings_controller.py`:
  - `ALL_CONNECTORS` (`:104-107`, now with `"assets"` after `"confluence"`), `authenticate_connector`
    (`:1284-1305`), `_authenticate_salesforce` (`:1370-1396`) and `_connectors_state`
    (`:2155-2194`, `has_org` generic through `connector_enabled_by_org`).
  - `authenticate_connector` first calls `_org_allows` (`:1307+`). Without the connector's bundle
    section it sets `"<Label> isn't enabled in your organization config."` and returns before any
    per-connector branch.
  - `connector_label("miro")` already gives `"Miro"` (`:123-124`).
  - The Authenticate… link is generic, so an OAuth-only connector needs no HTML.
  - `tests/unit/test_settings_controller.py` `ORG_CONFIG_ENABLING_EVERY_CONNECTOR` (`:64-68`) must
    enable every connector, or `test_connectors_cover_all_connectors` (`:2879-2881`) fails.
- **Org mode.**
  - `web/routes_connect.py`: `OAUTH_SERVICES` (`:95`, a frozenset union), `_GRANT_KEY` (`:103-104`),
    `SERVICE_LABELS` (`:106-111`), `_ORG_CONFIG_SECTION` (`:114-115`), `_is_connected` (`:160-172`,
    default: the token file exists), `_is_configured` (`:175-189`, `connector_enabled_by_org` first,
    then default `bool(section.get("client_id"))`), `_build_authorize_url` (`:333-389`),
    `_exchange_and_save` (`:392-436`), the start and callback routes (`:456-520`), and the generic
    rows tuple in `_render_connect_page` (`:1026-1028`, `("slack", "salesforce", "jira",
    "confluence") if offered(s)`, then the Assets row).
  - `tests/unit/web/test_routes_connect.py` `_ORG_CONFIG` (`:31-37`); `:222-224` pins 11 service rows
    ("Ten OAuth services and Telegram …; Peek and Grist have no section in this config").
  - `scripts/build_org_bundle.py`: option groups (`:245-317`, Salesforce `:262-268`, Grist `:283`),
    section writing (`:648-711`, Salesforce `:659-666`), the `services` list (`:898`, order `google,
    slack, salesforce, atlassian, grist, peek, telegram`) and `_CONNECTOR_CALLBACKS` (`:66-72`, used
    by the org-mode summary at `:948-963`).
- **Policy tables** a new tool must appear in:
  - `auto_accept.TOOL_TO_OPERATION` (`:86-194`) and `TOOL_TO_GATE` (`:208-379`).
  - `policy/registry.TOOL_TO_VERB` (`:125-230`), with the nine verbs at `:37-48` (ADR 0171). It is
    mirrored by `_VERB_MATRIX` in `tests/unit/policy/test_registry.py:189+`, whose size is pinned at
    `:296` (`len(_VERB_MATRIX) == 102`).
  - `write_effects.EFFECT_BY_TOOL` (`:42`, Peek's header last at `:182`) and `gate._TOOL_LAYOUT`
    (`:262+`, default NARROW).
  - The per-document Grist rule and the per-page Peek rule are the precedent for a per-board rule:
    - `policy/scopes.NEW_SCOPE_SELECTORS` (`:683-738`, `grist.document` at `:692-695`; matchers
      `_grist_document_matches` `:589-594` and `_peek_page_matches` `:596+`, which is `False` for an
      empty argument); module docstring list `:31-41`;
    - `policy/catalogue.EXTRA_SCOPES` (`:69-121`, Grist `:75-79`, docstring `:12-21`).
      `PolicyExtraScope` gained `verb_predicates` and `value_optional` (ADR 0228), both defaulted;
    - `policy/value_format.VALUE_FORMATS` (`:72+`): every value-taking extra predicate needs a format
      (`tests/unit/policy/test_value_format.py:168`). It imports the clients' own id regexes
      (`grist_client.DOC_ID_RE`, `peek_client._SLUG_RE`, `assets_client._ID_RE`) and refuses a URL
      instead of rewriting it;
    - `policy/suggest._EXTRA_ARGUMENT` (`:27-31`) names the call argument that carries each extra
      scope's value, so `privacyfence_check_policy` can suggest a rule;
    - no `policy/propose.PROPOSABLE_SCOPES` entry (ADR 0144, ADR 0077); `propose.py` docstring
      bullets `:30-45`;
    - `tests/unit/policy/test_registry.py` `_GRANT_MANIFEST_UNREACHABLE_OPERATIONS` (`:32-46`, comment
      at `:28-31` says "thirteen");
    - `tests/unit/policy/test_propose.py` `test_the_extra_scope_operations_stay_unproposed`
      (`:336-355`).
  - `scope_rows.py`, `describe.py` and `outdated.py` are generic over `EXTRA_SCOPES` and need no
    Miro entry.
- **Bookkeeping enforced by tests for every connector:**
  - `tests/unit/connectors/test_readme_manifest_alignment.py` `CONNECTOR_CLASSES` (`:39-43`).
  - `scripts/generate_tools_reference.py` `CONNECTOR_TITLES` (`:44-59`) and `CONNECTOR_SHORT`
    (`:62-77`), and `docs/tools-reference.md`, which is generated (today `| **Total** | **156** |
    **54** | **35** | **67** |`, `:106`).
  - `docs/always-allow-rules-reference.md`, which is generated.
  - `scripts/pyinstaller_common.py` (the list at `:75-88`, Peek last).
  - `tests/unit/test_website_connector_pages.py` `CONNECTORS` (`:28-43`).
  - `scripts/build_site.py`: `PAGES` (`:97-125`, Peek at `:108`) and `CONNECTOR_GUIDES` (`:195-197`).
  - `website/_partials/other-connectors.html` (header comment "the other six").
  - `website/connectors/index.html:29` (`Fourteen connectors, 156 tools, each with a fixed gate.`).
  - `website/how-it-works/index.html:54` (`156 connector tools`).
  - `website/compare/mcp-gateways/index.html:62` (`fourteen connectors`).
  - `tests/unit/test_website_connectors_page.py` (`:42-60`, `len(REFERENCE) == 14` and
    `"Fourteen connectors"` at `:60`).
  - `tests/unit/test_website_pages.py:200-201` (`test_there_are_fourteen_second_level_pages`).
  - `tests/unit/test_website_docs_allowlist.py` (every `docs/*.md` is in `docs/README.md`'s published
    half or in `CONTRIBUTOR_DOCS`).
  - `tests/unit/test_connector_tool_annotations.py` (`DESTRUCTIVE_TOOLS`).
  - `tests/unit/test_systemic_gate_invariants.py`. Every gated tool has a required `reason`
    (`TestReasonParamOnEveryGatedTool`, `:209-227`), every review `gated_call` passes
    `pii_scan_text` (`TestPiiScanTextOnEveryReviewGatedTool`, `:230-251`), and every parameter
    changes the approval binding key (`:394-409`, ADR 0175). All three cover a new tool once it is
    in `TOOL_TO_GATE`.
  - `tests/unit/test_docs_references_exist.py` (no doc path named before it exists).
  - Provider-name lists on other website pages (`website/index.html:189`,
    `compare/claude-connectors/index.html:72,78`, `gdpr/index.html:70,106`, `privacy/index.html:50`,
    `faq/index.html:58,100`, `security/index.html:41`, `enterprise/index.html:85`) are not tested.
    Jira Assets was not added to them either; this plan leaves them alone (§3.7).
- **QA.**
  - `scripts/qa_fixture_recorder.py`: `RawCapture` (`:664-699`), `_build_grist_client`
    (`:1277-1283`, now returning `(GristClient, bool)`), `check_grist` (`:1300-1365`),
    `CONNECTOR_CHECKS` (`:2250-2265`) and `EXPECTED_FIXTURES` (`:2279-2294`), with equal keys asserted
    at `:2301-2304`.
  - `lifecycle_grist` (`:2725-2775`) is the add-and-update-without-delete precedent;
    `LIFECYCLE_CHECKS` is at `:2994-3002`, with its connector list in the module docstring (`:32-33`).
  - `tests/unit/test_qa_fixture_recorder.py`:
    - `TestFixturePresence.test_every_expected_fixture_file_exists_and_is_nonempty_valid_json`
      (`:2504-2510`);
    - `test_every_expected_fixture_is_replayed_by_a_unit_test` (`:2512-2528`), which needs a
      `_load("<file>")` for every `EXPECTED_FIXTURES` file somewhere under `tests/unit`;
    - `test_committed_fixtures_carry_no_unredacted_identifiers` (`:2530`, no UUIDs, no e-mail domain
      but example.com) and `test_no_undocumented_fixture_files_are_silently_ignored` (`:2560`).
  - `scripts/qa_authenticate_connectors.py` `STEPS` (`:74-84`, Salesforce last).
  - The runner copies `~/privacyfence/{credentials,org,tests/fixtures/qa_environment.yaml}` in and
    copies `credentials/` back out (`docs/connector-qa.md` "Persistent QA state", `:464`).
  - **Which workflow can check a branch.** The self-hosted runner admits only workflows as they
    stand on `main` (`.claude/toolkit.yaml` `ci.dispatchable`, `qa-record-fixture.yml` header).
    - `qa-record-fixture.yml` is dispatched **on `main`** with inputs `connector` and `branch`. It
      runs `--record <connector>` (which runs that connector's check) against the branch's code and
      commits the fixture to that branch. It is the definition-of-done QA row for a branch.
    - `connector-live-check.yml` checks `main`'s own code (`--check` and `--lifecycle`, weekly). It
      cannot check a branch, so **no CI run can execute `lifecycle_miro` before the feature merges**.
- **The next free ADR number is 0230.** There are 14 connectors and 156 tools.

## 3. Design

### 3.0 Rules for every phase

- **No project history** in code, comments, docstrings, user-visible strings or standing docs. That
  means no phase ids, plan names, "Phase N", bare `#123` issue numbers or "as of" phrasing
  (`tests/unit/test_code_no_history.py`, `tests/unit/test_docs_no_history.py`, ADR 0056). Cite an ADR
  number for a reason instead.
- **Literal tool and gate names.** Every `gated_call(...)` in `connectors/miro.py` passes its tool name
  as a string literal (`tool="miro_create_items"`) and its gate as a literal (`gate="popup"`), inline.
  `test_readme_manifest_alignment.py`, `test_write_effects.py` and
  `test_systemic_gate_invariants.py` parse the source.
- **Don't name the setup guide's path before it exists.** The setup guide is `miro-setup.md` in
  `docs/`. Until the phase that creates it, never write its path as one token in a committed file:
  `tests/unit/test_docs_references_exist.py` fails on it.
- **The plan is never listed.** `tests/unit/test_website_docs_allowlist.py` skips `docs/*-plan.md`,
  so this plan is in neither `scripts/build_site.py` `CONTRIBUTOR_DOCS` nor `docs/README.md`. No
  phase adds it there.
- **Exit check.** Every phase ends with `ruff check .` and `python3 -m pytest tests/unit -q` passing
  in full.
- **Logging and errors.** No log line, error message, preview dict or audit entry carries a token, the
  client secret, an authorization code or item content. Logs carry the board id and counts only.
- **Test ids look real.** Item ids in tests are 19-digit strings such as `"3458764549483493025"`, never
  short numbers: `test_code_no_history.py` reads `#42`-shaped text as an issue number, and the
  connector formats items as `f"{type} #{id}"`.
- **The live-check row of `/dod` is owed by the QA phases.** Every phase that touches
  `src/privacyfence/*_client.py` or `src/privacyfence/connectors/**` before `p11-qa-live`: dispatch
  nothing, and report that row as "deferred to p11-qa-live and p12-docs-adrs-retire". Miro has no
  live check until `p10-qa-recorder` registers one. The row is met only by `qa-record-fixture.yml`
  dispatched on `main` with `connector=miro` and `branch=<the phase branch>`; never by
  `connector-live-check.yml`, which checks `main`'s code (§2 "QA").

### 3.1 Miro API facts this plan relies on

Sources:
- **Confirmed** in Miro's official OpenAPI spec and SDKs: `github.com/miroapp/api-clients`
  (`packages/generator/spec.json`, `packages/miro-api-python/miro_api/miro_api_wrapper.py`),
  re-checked against the spec at commit `f7a2747` (2026-10-21, spec `v2.0`). Every endpoint,
  limit and value list below still matches it. Don't copy the Python SDK's token handling: its expiry
  check reads `payload.get("payload")`, so it never records an expiry.
- **Reported** in Miro's developer docs as quoted by search results. Marked *(docs)*.

**OAuth**
- Authorization URL `https://miro.com/oauth/authorize`, params `response_type=code`, `client_id`,
  `redirect_uri` and `state`. There is no `scope` parameter: the app's permissions are set in its
  settings in Miro. The SDK also sends an optional `team_id` that pre-selects the team on the
  install screen; this plan never sends it, so the person picks the team.
- No PKCE: neither the SDKs nor the docs mention one *(docs)*.
- Token URL `POST https://api.miro.com/v1/oauth/token` *(docs; the spec has no token path)*, with
  `grant_type`, `client_id`, `client_secret` and then `code` and `redirect_uri`, or
  `refresh_token`. The SDKs send these as query parameters. This plan sends them as a form body
  (§3.2.1); manual step `mb2` confirms Miro accepts that before any phase starts.
- Token response *(SDK types and docs)*: `access_token`, `token_type`, `scope` (space-separated),
  `team_id` and `user_id`.
  `refresh_token` and `expires_in` come only when the app was created with **"Expire user
  authorization token"** on. That choice is made when the app is created and cannot be changed
  afterwards *(docs)*.
- An access token lasts 1 hour and a refresh token 60 days. A refresh returns a new refresh token
  and voids the old pair *(docs)*.
- Redirect URIs match exactly. An app can list several, and an `http://` loopback URI works
  *(docs)*.
- A token belongs to **one user in one team**: the team chosen on Miro's install screen. It cannot
  reach other teams' boards *(docs)*.
- An app made in a Developer team is installed by other people through its **Installation URL**
  ("Share app" on the app page) *(docs)*.
- `GET https://api.miro.com/v1/oauth-token` returns `{type, organization: {type, id, name},
  team: {type, id, name}, createdBy: {type, id, name}, user: {type, id, name}, scopes: [...]}`. The
  client reads only `team.name`.

**REST v2**
- Base `https://api.miro.com`, bearer token.
- Errors are JSON `{status, code, message, type: "error", context}`; a 429 has
  `code: "tooManyRequests"`. The client reads only `message`.
- HTTP 429 when the rate limit is hit (a credit budget per minute, weighted per method). The
  `X-RateLimit-Reset` header carries the epoch second *(docs)*.

| Client method | HTTP | Notes |
|---|---|---|
| `token_context` | `GET /v1/oauth-token` | team and user names |
| `list_boards` | `GET /v2/boards?query&limit&offset` | offset paging, `limit` ≤ 50; response `{data, total, offset, limit}` |
| `get_board` | `GET /v2/boards/{board_id}` | `{id, name, description, team{name}, owner{name}, createdAt, modifiedAt, viewLink}` |
| `update_board` | `PATCH /v2/boards/{board_id}` body `{name?, description?}` | name ≤ 60, description ≤ 300; never send `policy`, `teamId` or `projectId` |
| `list_tags` | `GET /v2/boards/{board_id}/tags?limit&offset` | offset paging |
| `list_items` | `GET /v2/boards/{board_id}/items?type&parent_item_id&limit&cursor` | cursor paging, `limit` 10–50 |
| `get_item` | `GET /v2/boards/{board_id}/items/{item_id}`, then for the five editable types `GET /v2/boards/{board_id}/{endpoint}/{item_id}` | the generic `GenericItem` has **no `style`** (`{id, type, data, position{x,y}, geometry{width,height}, parent{id}, modifiedAt}`); the per-type answer adds `style` |
| `list_connectors` | `GET /v2/boards/{board_id}/connectors?limit&cursor` | `{id, startItem{id}, endItem{id}, shape, captions[{content}]}` |
| `create_items` | `POST /v2/boards/{board_id}/items/bulk` body `[ItemCreate, …]` | 1–20 items, **transactional** (all or none); response `{data: [{id, type, …}]}`. `ItemDataCreate` has no frame data, so **frames are not created here** |
| `create_frame` | `POST /v2/boards/{board_id}/frames` body `{data{title, format, type}, style{fillColor}, position, geometry}` | one per call |
| `create_connector` | `POST /v2/boards/{board_id}/connectors` | one per call; frames cannot be ends; `startItem.id` must differ from `endItem.id` |
| `update_item` | `PATCH /v2/boards/{board_id}/{sticky_notes\|cards\|texts\|shapes\|frames}/{item_id}` | partial body `{data?, style?, position?, geometry?}` |
| `set_parent` | `PATCH /v2/boards/{board_id}/items/{item_id}` body `{"parent": {"id": frame_id}}` | the generic item update accepts only `parent` and `position` |

- **An item cannot be taken out of a frame through the API.** The spec's `Parent` is `{id}`, not
  nullable, and manual step `mb3` showed it live on 2026-10-25: `PATCH /v2/boards/{id}/items/{item}`
  with `{"parent": null}` answers **HTTP 200 and leaves the parent unchanged**. Because Miro reports
  success for a change it did not make, the connector refuses the request before the card (§3.3), and
  `set_parent` reads the item back to confirm a move into a frame (§3.2.2).
- **Fill colors of shapes and frames** are documented as one of 16 hex values (`FILL_COLORS` in
  §3.3); the connector accepts only those. Card border (`cardTheme`) and text color take any
  `#RRGGBB`.
- **Search.** `GET /v2/boards?query=` matches a board's name or description.
- **Never called:**
  - any `DELETE`;
  - `POST /v2/boards` (create board) and `PUT /v2/boards?copy_from` (copy board);
  - anything under `/members`;
  - `POST` or `PATCH` on tags, and tag attach;
  - images, documents and embeds;
  - groups;
  - every `/v2/orgs/…` path (Enterprise).

### 3.2 Miro modules

#### 3.2.1 `src/privacyfence/miro_auth.py`

**Module docstring.** How PrivacyFence signs in to Miro:
- authorization code, confidential client, no PKCE (Miro offers none), `state` checked by the
  loopback;
- expiring tokens only, refreshed and rotated;
- one token per person, for the one team chosen at sign-in;
- redirects are never followed, and nothing logs a token, a code or the client secret.

**Errors**
- `class MiroClientError(Exception)`. The REST client imports and re-exports it.

**Constants**

| Name | Value |
|---|---|
| `MIRO_OAUTH_PORT` | `53686` |
| `MIRO_REDIRECT_PATH` | `"/callback"` (local redirect `http://localhost:53686/callback`) |
| `AUTHORIZE_URL` | `"https://miro.com/oauth/authorize"` |
| `TOKEN_URL` | `"https://api.miro.com/v1/oauth/token"` |
| `REQUIRED_SCOPES` | `("boards:read", "boards:write")` |
| `NOT_AUTHENTICATED` | `"Miro is not authenticated. Use Authenticate… in PrivacyFence Settings."` |

**Bundle section `miro`:** `{"client_id": str, "client_secret": str}`
- `@dataclass(frozen=True) class MiroOAuthConfig(client_id: str, client_secret: str)`, with a
  `__repr__` that hides the secret.
- `bundle_settings(section: dict[str, Any]) -> MiroOAuthConfig`:
  - Both keys empty or missing → `MiroClientError("Miro organization config not installed")`.
  - Exactly one of them →
    `MiroClientError("Miro organization config is incomplete: client_id and client_secret go together.")`.

**Credential file `credentials/miro_token.json`**
- Its content is `{"access_token": str, "refresh_token": str, "expires_at": float, "team_id": str,
  "user_id": str, "scope": str}`.
  - `user_id` and `scope` are optional on load and default to `""`.
  - `expires_at` may be `0`, which forces a refresh on first use. Manual step `mb2` writes the
    runner's file that way.
- `save_token_file(path: str, record: dict[str, Any]) -> None` → `secure_files.atomic_write_json`.
  It is the single token-write site.
- `load_token_file(path: str) -> dict[str, Any]`:
  - A missing file, or a missing or empty `access_token`, `refresh_token` or `team_id`, or an
    `expires_at` that is not an int or float → `MiroClientError(NOT_AUTHENTICATED)`.
  - Not valid JSON, or not an object →
    `MiroClientError("Miro's saved sign-in could not be read. Use Authenticate… in PrivacyFence Settings to connect again.")`.
  - Returns the record with the optional keys filled in.

**Token requests** (`exchange_code` and `refresh` both use them)
- A private `_token_request(form: dict[str, str]) -> tuple[int, dict[str, Any]]`:
  - `requests.post(TOKEN_URL, data=form, timeout=30, allow_redirects=False)`. The credentials go
    in the form body, never in the URL, so no logger that prints a request line can see them.
  - `requests.RequestException` →
    `MiroClientError(f"Could not reach Miro's sign-in server: {type(exc).__name__}")`.
  - 3xx → `MiroClientError(f"Miro's sign-in server answered with a redirect (HTTP {status}).")`.
  - 5xx → `MiroClientError(f"Miro's sign-in server answered with an error (HTTP {status}).")`.
  - Any status whose body is not a JSON object →
    `MiroClientError(f"Miro's sign-in server answered with something other than JSON (HTTP {status}).")`.
  - Returns `(status, body)` for the callers to interpret 4xx.
  - `message` below is the body's `message`, else its `error_description`, else its `error`, cut to
    200 characters, else `"no detail"`.
- `build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str`: `AUTHORIZE_URL` plus
  `response_type=code`, `client_id`, `redirect_uri` and `state` (`urllib.parse.urlencode`).
- `exchange_code(config, code, redirect_uri) -> dict[str, Any]`:
  - Form `grant_type=authorization_code`, `client_id`, `client_secret`, `code`, `redirect_uri`.
  - 4xx → `MiroClientError(f"Miro sign-in failed: {message}")`.
  - No `refresh_token` or `expires_in` in a 2xx answer →
    `MiroClientError("Miro returned a token that never expires. Create the PrivacyFence app in Miro with \"Expire user authorization token\" turned on; see the Miro setup guide.")`.
  - When the answer carries `scope` and either required scope is missing from it →
    `MiroClientError(f"The Miro app is missing the permission(s) {', '.join(missing)}. Turn on boards:read and boards:write in the app's settings in Miro, then sign in again.")`.
    Extra scopes are not refused; the connector calls nothing beyond §3.1's table.
  - A 2xx answer without `access_token` or `team_id`, or whose `expires_in` is not a positive
    number, → `MiroClientError(f"Miro returned an incomplete sign-in answer (no {key}).")`, where
    `key` is the first of `access_token`, `team_id`, `expires_in` that fails. These checks run
    before the expiring-token and scope checks, in this order: access_token, team_id,
    refresh_token/expires_in presence (the non-expiring message), expires_in value, scope.
  - Returns the record: `expires_at = time.time() + float(expires_in)`; `team_id` and `user_id` as
    `str`; `scope` as given or `""`.
- `refresh(config, record) -> dict[str, Any]`:
  - Form `grant_type=refresh_token`, `client_id`, `client_secret`, `refresh_token`.
  - 400 or 401 →
    `MiroClientError("Your Miro sign-in has expired or was revoked. Use Authenticate… in PrivacyFence Settings to sign in again.")`.
  - Other 4xx → `MiroClientError(f"Miro sign-in refresh failed: {message}")`.
  - A 2xx answer without `access_token`, or whose `expires_in` is missing or not a positive number,
    → the "incomplete sign-in answer" message with that key.
  - Returns `{**record, "access_token": …, "refresh_token": <new one, or the old one if absent>, "expires_at": …}`.

**Token provider**
- `class MiroTokenProvider(config: MiroOAuthConfig, token_file: str, record: dict[str, Any])`.
  - `team_id` property.
  - `access_token(*, force_refresh: bool = False) -> str` takes a `threading.Lock`. When it needs a
    refresh (`force_refresh`, or `record["expires_at"] - 60 <= time.time()`), it first re-reads the
    file with `load_token_file`. When that read succeeds and the file's `refresh_token` differs from
    the one in memory, another instance has already refreshed, and Miro voided the old pair. The
    provider then adopts the file's record. If that record is still valid
    (`expires_at - 60 > time.time()`) and `force_refresh` is false, it returns that access token
    without refreshing. Otherwise it refreshes from the newest record, saves with
    `save_token_file` and returns the token. A failed re-read (`MiroClientError`) is ignored, and
    it refreshes from memory.
    This matters because a refresh voids the old refresh token. Two providers can be alive at once:
    a Settings rebuild while the old connector still runs, an org registry rebuild, or the recorder
    on the runner. Without the re-read the second one would sign the person out.
  - `__repr__` hides every token.

**Interactive sign-in**
- `authorize_interactive(config, token_file) -> dict[str, Any]`:
  - Calls `oauth_loopback.run_browser_oauth(_build, _exchange, port=MIRO_OAUTH_PORT,
    path=MIRO_REDIRECT_PATH, redirect_host="localhost")`.
  - `_build(redirect_uri, state, _challenge)` returns
    `build_authorize_url(config.client_id, redirect_uri, state)`.
  - `_exchange(code, redirect_uri, _verifier)` returns `exchange_code(config, code, redirect_uri)`.
  - Maps `OAuthLoopbackError` to `MiroClientError(f"Miro sign-in failed: {exc}")`, saves the record
    and returns it.

#### 3.2.2 `src/privacyfence/miro_client.py`

**Module docstring.** The Miro REST v2 client:
- the token comes from `miro_auth`;
- never follows a redirect and never retries a write (ADR 0105's rule). The one repeat is after an
  HTTP 401, with a refreshed token: Miro ran nothing on a 401, so it is not a retried write;
- logs board ids and counts only, never a token or item content;
- calls only the endpoints in §3.1's table.

**Imports and HTTP**
- `from .miro_auth import MiroClientError` (re-exported in `__all__`), plus
  `class MiroNotFound(MiroClientError)` for HTTP 404 and `class MiroAccessDenied(MiroClientError)`
  for HTTP 403.
- `API_BASE = "https://api.miro.com"`, `MAX_PAGES = 10` (ADR 0116), `MAX_ITEMS = 500`.
- `requests` with one `requests.Session`. Every call is
  `session.request(method, url, params=…, json=…, headers=…, timeout=30, allow_redirects=False)`,
  with TLS verification on.

**Ids**
- `BOARD_ID_RE = re.compile(r"[A-Za-z0-9_\-=+/]{4,64}")`, public: `policy/value_format.py`
  imports it for the `miro.board` rule value (§3.4), as it imports `grist_client.DOC_ID_RE`.
- `ITEM_ID_RE = re.compile(r"[0-9]{1,20}")`.
- `validate_board_id(value: str) -> str`:
  - Strips whitespace.
  - Requires `BOARD_ID_RE.fullmatch`, else
    `MiroClientError(f"Not a Miro board id: {value!r}. Use the id from miro_list_boards, or the part of the board's address after /app/board/.")`.
  - A board link is **not** accepted. Rule values are matched verbatim and `policy/value_format.py`
    refuses a URL rather than rewriting it, so a tool that took links while its rules could not
    would answer `privacyfence_check_policy` with suggestions that cannot be saved.
- `validate_item_id(value: str) -> str` → `MiroClientError(f"Not a Miro item id: {value!r}")`.
- Board ids go into paths through `urllib.parse.quote(board_id, safe="")`.

**Dataclasses** (all fields typed)
- `MiroBoard(id: str, name: str, description: str, team: str, created_at: str, modified_at: str,
  view_link: str)`. No owner: the connector shows no person's name it does not need.
- `MiroTag(id: str, title: str, color: str)`.
- `MiroItem(id: str, type: str, content: str, title: str, description: str, due_date: str,
  shape: str, color: str, x: float | None, y: float | None, width: float | None,
  height: float | None, frame_id: str, modified_at: str)`.
  - `content`, `title`, `description` and `shape` come from `data`; `due_date` is `data.dueDate[:10]`.
  - `color` is `style.fillColor` for `sticky_note`, `shape`, `frame` and every other type,
    `style.cardTheme` for `card`, and `style.color` for `text`. Only the per-type answer carries
    `style`, so `color` is `""` for items from `list_items` and filled in by `get_item`.
  - `frame_id` is `parent.id`; missing keys give `""` or `None`.
- `MiroConnectorLine(id: str, start_item_id: str, end_item_id: str, shape: str, captions: list[str])`.

**`class MiroClient(provider: MiroTokenProvider)`**
- `team_name` attribute, `""` until `check_connection` sets it. `__repr__` without the token.
- `_request(self, method: str, path: str, *, params: dict[str, str] | None = None, json_body: Any = None) -> Any`
  is the single choke point; `RawCapture` wraps it, so keep `method` first.
  - Adds `Authorization: Bearer <provider.access_token()>`.
  - On 401, calls `access_token(force_refresh=True)` and retries once.
  - Returns parsed JSON, or `None` for an empty body.
  - Errors (exact):

    | Condition | Raised |
    |---|---|
    | `requests.RequestException` | `f"Could not reach Miro: {type(exc).__name__}"` |
    | 3xx | `f"Miro answered with a redirect (HTTP {status})."` |
    | 401 after the retry | `"Miro refused the sign-in (HTTP 401). Use Authenticate… in PrivacyFence Settings to sign in again."` |
    | 403 | `MiroAccessDenied("Miro refused the request (HTTP 403). Your Miro account, or the team PrivacyFence was signed in to, has no access to it.")` |
    | 404 | `MiroNotFound("Miro found no such board or item (HTTP 404).")` |
    | 429 | `f"Miro's rate limit was reached. Try again in {n} seconds."`, where `n = max(1, int(X-RateLimit-Reset) - int(time.time()))`; without a usable header, `"Miro's rate limit was reached. Try again in a minute."` |
    | other non-2xx | `f"Miro API error (HTTP {status}): {detail}"`, with `detail` the JSON `message` cut to 200 characters, else `"no detail"` |
    | a 2xx whose non-empty body is not JSON | `f"Miro answered with something other than JSON (HTTP {status})."` |

- `check_connection() -> str`:
  - `GET /v1/oauth-token` sets `team_name` to `team.name`.
  - On `MiroAccessDenied` it instead does `GET /v2/boards` with `limit=1` and leaves `team_name` at
    `""`.
  - Returns `team_name`.
- Methods:
  - `list_boards(query: str, max_results: int) -> list[MiroBoard]` pages by offset (`limit` 50) up
    to `max_results` or `MAX_PAGES`.
  - `get_board(board_id) -> MiroBoard`.
  - `update_board(board_id, *, name: str | None, description: str | None) -> MiroBoard` sends only
    the given keys.
  - `list_tags(board_id) -> list[MiroTag]` offset-pages, at most 200.
  - `list_items(board_id, *, item_type: str, frame_id: str, max_results: int) -> tuple[list[MiroItem], bool]`:
    - cursor paging with `limit` 50 and `MAX_PAGES`;
    - sends `type` only when non-empty, and `parent_item_id` only when `frame_id` is non-empty;
    - returns `(items, truncated)`, where `truncated` is `True` when a next cursor remained after
      `max_results` items were collected.
  - `get_item(board_id, item_id) -> MiroItem`: the generic GET. When its `type` is a key of
    `_TYPE_ENDPOINT`, it then does the per-type GET and parses that answer instead, which carries
    `style`.
  - `list_connectors(board_id, *, max_results: int) -> tuple[list[MiroConnectorLine], bool]`.
  - `create_items(board_id, bodies: list[dict[str, Any]]) -> list[MiroItem]`, one bulk POST. It
    never contains a frame.
  - `create_frame(board_id, body: dict[str, Any]) -> MiroItem`, one `POST /v2/boards/{id}/frames`.
  - `create_connector(board_id, body: dict[str, Any]) -> str` returns the new id.
  - `update_item(board_id, item_type: str, item_id: str, body: dict[str, Any]) -> None` uses
    `_TYPE_ENDPOINT = {"sticky_note": "sticky_notes", "card": "cards", "text": "texts",
    "shape": "shapes", "frame": "frames"}`. Any other type →
    `MiroClientError(f"Miro items of type {item_type} cannot be changed here.")`.
  - `set_parent(board_id, item_id, frame_id: str) -> None` sends `{"parent": {"id": frame_id}}`,
    for a non-empty `frame_id`; `""` raises
    `MiroClientError("Miro's API cannot take an item out of a frame. Move it out in Miro.")` before any
    request. After the PATCH it calls `get_item` and, when the item's `frame_id` is not `frame_id`,
    raises `MiroClientError(f"Miro accepted moving item {item_id} into frame {frame_id} but did not move it.")`
    (Miro answers 200 to a parent change it ignores, §3.1).
- Every method validates its ids before any request.

### 3.3 `src/privacyfence/connectors/miro.py`

**Class shape**
- `class MiroConnector(Connector)`, with `name == "miro"`, `__init__(self, client: MiroClient)` and a
  `client` property.
- `_fetch` and `_auto_audit` are the Grist ones (`connectors/grist.py:1536-1562`). `_fetch`
  re-raises `MiroClientError` as `RuntimeError(str(exc)) from exc`.
- Argument validation raises `ValueError` before anything is fetched or gated. A `MiroClientError`
  from `validate_board_id` or `validate_item_id` becomes `ValueError(str(exc))`.
- `_board_info(board_id) -> MiroBoard` calls `client.get_board` through `_fetch`.
- Every `preview` dict below starts with
  `"Board": board.name, "Team": self._client.team_name or "(not shown by Miro)"`.
- Every `gated_call` passes `connector=self.name`, `my_email=""` and `args={"board_id": board_id}`,
  where `board_id` is the validated, stripped id. The `miro.board` rule matches on it.

**Tool parameters**
- Every tool ends with the required
  `ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")`.
- `read_only` is `True` for the six reads and `False` for the four writes. None is `destructive`.
- JSON-string parameters are parsed with `json.loads`. A parse failure or wrong shape raises the
  `ValueError` given below.

**Tools**

| Tool | Gate | Params (besides `reason`) | Returns |
|---|---|---|---|
| `miro_list_boards` | auto | `query` (str, default `""`), `max_results` (int, default 50, 1–200) | bare list of `{"id","name","team","modified_at","view_link"}` |
| `miro_get_board` | auto | `board_id` | `{"id","name","team","created_at","modified_at","view_link"}`; no description (free text is content, read only behind a card) and no owner |
| `miro_list_tags` | auto | `board_id` | bare list of `{"id","title","color"}` |
| `miro_list_items` | review | `board_id`, `type` (str, default `""`), `frame_id` (str, default `""`), `max_results` (int, default 100, 1–500) | bare list of item dicts |
| `miro_get_items` | review | `board_id`, `item_ids` (str, ids separated by commas, 1–50) | bare list of item dicts |
| `miro_list_connectors` | review | `board_id`, `max_results` (int, default 100, 1–500) | bare list of `{"id","start_item_id","end_item_id","shape","captions"}` |
| `miro_create_items` | popup | `board_id`, `items` (str, JSON) | `{"board_id", "created": [{"id","type"}]}` |
| `miro_create_connectors` | popup | `board_id`, `connectors` (str, JSON) | `{"board_id", "created_ids": [...]}` |
| `miro_update_items` | popup | `board_id`, `changes` (str, JSON) | `{"board_id", "updated_ids": [...]}` |
| `miro_update_board` | popup | `board_id`, `name` (str, default `""`), `description` (str, default `""`) | `{"board_id","name","description"}` |

An **item dict** is `dataclasses.asdict(MiroItem)`.

**Descriptions** follow ADR 0115 (`assert_tool_definitions_complete`): a one-sentence summary, a
`Returns` sentence, each sibling by name, and `"Auto-approved."` or `"Requires user approval."`.
First sentences:
- `miro_list_boards`: "List the Miro boards this account can open in the connected Miro team, optionally only those matching a search text."
- `miro_get_board`: "Get one Miro board's name, team, dates and link."
- `miro_list_tags`: "List the tags defined on one Miro board."
- `miro_list_items`: "Read the items on one Miro board, optionally only one type of item or only the items inside one frame."
- `miro_get_items`: "Read specific items of one Miro board by their ids."
- `miro_list_connectors`: "Read the connector lines on one Miro board and the ids of the items each one joins."
- `miro_create_items`: "Add sticky notes, cards, texts, shapes or frames to one Miro board, up to 20 in one request."
- `miro_create_connectors`: "Draw connector lines between existing items on one Miro board, up to 20 in one request."
- `miro_update_items`: "Change existing items on one Miro board (text, color, position, size, or which frame they are in), up to 20 in one request."
- `miro_update_board`: "Rename a Miro board or change its description."

Two descriptions carry extra sentences:
- `miro_list_boards` adds: "Only boards in the Miro team chosen when PrivacyFence was signed in are visible."
- `miro_update_items` adds: "Nothing can be deleted, and an item cannot be taken out of a frame; that is done in Miro."

**Parameter descriptions**
- `board_id`: "The board id from miro_list_boards (such as uXjVOD6LSME=), the part of the board's address after /app/board/. Not the full link."
- `type` (list_items): "Only items of this type: sticky_note, card, text, shape, frame, image, document, embed or app_card. Empty means every type; use frame to list the board's frames."
- `frame_id`: "Only the items inside this frame (an item id of type frame from miro_list_items). Empty means the whole board."
- `item_ids`: "Item ids from miro_list_items, separated by commas, 1 to 50."
- `max_results`: "Most results to return. The result is cut to this many."
- `query`: "Text to search for in board names and descriptions. Empty lists every board."
- `name`: "New board name, at most 60 characters. Empty keeps the current name."
- `description`: "New board description, at most 300 characters. Empty keeps the current description."
- `items`: "A JSON array of 1 to 20 items, each {\"type\": sticky_note|card|text|shape|frame, ...}. Keys by type: sticky_note content, shape (square or rectangle), width or height, frame_id; card title, description, due_date (YYYY-MM-DD), width, height, frame_id; text content (required), width, frame_id; shape content, shape (rectangle, round_rectangle, circle, triangle, rhombus, star, cloud, ...), width, height, frame_id; frame title, width, height. Every type takes x, y and color: a sticky_note color is a name such as yellow, light_blue or green; a shape or frame color is one of Miro's 16 fill colors such as #fff9b1; a card or text color is any #RRGGBB. Inside a frame x and y are measured from the frame's top-left corner. Content may use simple HTML such as <p> and <strong>. Example: [{\"type\": \"sticky_note\", \"content\": \"Idea\", \"color\": \"yellow\", \"x\": 0, \"y\": 0}]."
- `connectors`: "A JSON array of 1 to 20 lines, each {\"start_item_id\": ..., \"end_item_id\": ...} with optional shape (straight, elbowed or curved), caption (at most 200 characters) and color (#RRGGBB). The two ends are different existing items that are not frames. Example: [{\"start_item_id\": \"3458764549483493025\", \"end_item_id\": \"3458764549483493026\", \"caption\": \"leads to\"}]."
- `changes`: "A JSON array of 1 to 20 changes, each {\"id\": <item id>, <key>: <new value>, ...} using the keys miro_create_items allows for that item's type (not type). Set frame_id to a frame's id to move the item into it; an item cannot be taken out of a frame here. A change that sets frame_id sets no x or y. Example: [{\"id\": \"3458764549483493025\", \"content\": \"Done\", \"color\": \"green\"}]."

  These three strings are written as Python string literals in the connector, with the quotes
  escaped as shown; the shape list in `items` is abbreviated with "..." on purpose, and the full
  list is in the validation message.

**`items` format** (create): a JSON array of 1–20 objects, each with `type` and only the keys its
type allows:

| `type` | Allowed keys besides `type`, `x`, `y`, `color` |
|---|---|
| `sticky_note` | `content`, `shape` (`square` or `rectangle`), `width` **or** `height`, `frame_id` |
| `card` | `title`, `description`, `due_date` (`YYYY-MM-DD`), `width`, `height`, `frame_id` |
| `text` | `content` (required, non-empty), `width`, `frame_id` |
| `shape` | `content`, `shape` (one of `MIRO_SHAPES`), `width`, `height`, `frame_id` |
| `frame` | `title`, `width`, `height` |

**Value rules**
- `x` and `y` are numbers, default 0. Inside a frame they are measured from the frame's top-left
  corner; the descriptions say so.
- `width` and `height` are numbers from 1 to 100000.
- `content` is a string of at most 6000 characters, passed to Miro as is; Miro accepts simple HTML
  such as `<p>`, `<strong>`, `<em>` and `<a>`.
- `title` is at most 500 characters and `description` at most 6000.
- `color`:
  - for `sticky_note`, one of `STICKY_COLORS = ("gray", "light_yellow", "yellow", "orange",
    "light_green", "green", "dark_green", "cyan", "light_pink", "pink", "violet", "red",
    "light_blue", "blue", "dark_blue", "black")`;
  - for `shape` and `frame`, one of
    `FILL_COLORS = ("#f5f6f8", "#d5f692", "#d0e17a", "#93d275", "#67c6c0", "#23bfe7", "#a6ccf5",
    "#7b92ff", "#fff9b1", "#f5d128", "#ff9d48", "#f16c7f", "#ea94bb", "#ffcee0", "#b384bb",
    "#000000")` (Miro's documented values; compared lower-cased);
  - for `card` and `text`, `#RRGGBB` (`re.fullmatch(r"#[0-9A-Fa-f]{6}")`).
- A `bool` is never accepted where a number is expected (`x`, `y`, `width`, `height`), even though
  Python's `bool` is an `int`.
- `MIRO_SHAPES` is the 21 values of Miro's `ShapeData.shape` enum: `rectangle`, `round_rectangle`,
  `circle`, `triangle`, `rhombus`, `parallelogram`, `trapezoid`, `pentagon`, `hexagon`, `octagon`,
  `wedge_round_rectangle_callout`, `star`, `flow_chart_predefined_process`, `cloud`, `cross`, `can`,
  `right_arrow`, `left_arrow`, `left_right_arrow`, `left_brace`, `right_brace`.

**Body built per item** (`_item_body(spec) -> dict`). For every type but `frame` this is the bulk
`ItemCreate`. A frame's body is the same dict without `"type"` and `"parent"`, sent to
`create_frame`.
- `{"type": t, "data": {...}, "style": {...}, "position": {"x": x, "y": y}, "geometry": {...},
  "parent": {"id": frame_id}}`, leaving out empty `data`, `style`, `geometry` and `parent`.
- `data` per type:

  | Type | `data` |
  |---|---|
  | `sticky_note` | `content`, `shape` |
  | `card` | `title`, `description`, `dueDate` = `f"{due_date}T12:00:00.000Z"` |
  | `text` | `content` |
  | `shape` | `content`, `shape` |
  | `frame` | `title`, `"format": "custom"`, `"type": "freeform"` |

- `color` goes to `style.fillColor` (sticky_note, shape, frame), `style.cardTheme` (card) or
  `style.color` (text).

**Order of a create.** Frames in `items` are created first, one `create_frame` each, in the order
given. Then every other item goes in one `create_items` bulk call, which Miro runs all or none. A
`frame_id` in `items` must name a frame already on the board; a frame created in the same request
cannot be a parent. When the request holds no other item, there is no bulk call.

**`connectors` format**: a JSON array of 1–20
`{"start_item_id", "end_item_id", "shape"?: straight|elbowed|curved, "caption"?: str ≤ 200, "color"?: "#RRGGBB"}`,
and no other keys.
- The body is `{"startItem": {"id"}, "endItem": {"id"}, "shape"?, "captions"?: [{"content": caption}], "style"?: {"strokeColor": color}}`.
- Both ends must be existing items. A missing one is a `MiroNotFound` from `get_item`.
- A frame cannot be an end, and the two ends differ.

**`changes` format**: a JSON array of 1–20 `{"id": "<item id>", <field>: <new value>, …}` with at
least one field.
- The fields are the create keys of the item's current type, minus `type`.
- `frame_id` must name a frame on the board (checked with `get_item` before the gate, like a create's
  `frame_id`). `""` raises `ValueError("Miro's API cannot take an item out of a frame. Move it out in Miro.")`
  before the gate (§3.1). Frames take no `frame_id`.
- A change that carries `frame_id` carries no `x` or `y`: after a move, Miro measures the position
  from the new parent, so the two are separate changes.
- A frame's change sends only `title` in `data`, never `format` or `type` again.
- Ids are distinct.
- Each change becomes, in order:
  1. one `update_item` with the changed `data`, `style`, `position` and `geometry` groups, when any
     is changed;
  2. one `set_parent`, when `frame_id` is given.

**Validation messages** (exact, raised before anything is gated)
- `items`:
  `ValueError('items must be a JSON array of 1 to 20 objects, each with a type of sticky_note, card, text, shape or frame.')`
- An unknown key for a type:
  `ValueError(f"A {t} takes only these keys: {', '.join(sorted(allowed))}.")`
- A sticky note with both width and height:
  `ValueError("A sticky_note takes width or height, not both.")`
- A text with no content: `ValueError("A text needs non-empty content.")`
- A bad color for a sticky note:
  `ValueError(f"A sticky_note color must be one of {', '.join(STICKY_COLORS)}.")`
- A bad color for a shape or frame:
  `ValueError(f"A {t} color must be one of {', '.join(FILL_COLORS)}.")`
- A bad color for a card or text:
  `ValueError(f"A {t} color must be a hex color such as #2D9BF0.")`
- A bad shape:
  `ValueError(f"Unsupported shape {s!r}. Use one of {', '.join(MIRO_SHAPES)}.")` (sticky notes:
  `"A sticky_note shape must be square or rectangle."`)
- A number out of range, or a `bool`: `ValueError(f"{key} must be a number from 1 to 100000.")`;
  for x and y, `ValueError("x and y must be numbers.")`
- A bad `due_date`: `ValueError("due_date must be a date written YYYY-MM-DD.")`
- Text too long: `ValueError(f"{key} is longer than {limit} characters.")`
- A bad `frame_id`: `ValueError(f"Not a Miro item id: {value!r}")`
- A `frame_id` that is not a frame on this board (checked with `get_item` before the gate; a
  `MiroNotFound` counts too): `ValueError(f"Item {frame_id} is not a frame on this board.")`
- `connectors`:
  `ValueError('connectors must be a JSON array of 1 to 20 {"start_item_id": ..., "end_item_id": ...} objects.')`
- An unknown end:
  `ValueError(f"Item(s) not found on this board: {', '.join(sorted(missing))}.")`
- A frame as an end: `ValueError("A connector cannot start or end at a frame.")`
- The same item at both ends: `ValueError("A connector must join two different items.")`
- An unknown key in a connector:
  `ValueError("A connector takes only these keys: caption, color, end_item_id, shape, start_item_id.")`
- A bad shape: `ValueError("A connector shape must be straight, elbowed or curved.")`
- A bad color: `ValueError("A connector color must be a hex color such as #2D9BF0.")`
- A caption over 200 characters: the "longer than" message with key `caption`.
- `changes`:
  `ValueError('changes must be a JSON array of 1 to 20 {"id": ..., <field>: <new value>} objects with distinct ids and at least one field each.')`
- An item id not on the board: the same "not found" message.
- `frame_id` together with `x` or `y`:
  `ValueError(f"Item {id}: move it into a frame in one change and set its x and y in another.")`
- A type that cannot be changed:
  `ValueError(f"Item {id} is a {type}; only sticky notes, cards, texts, shapes and frames can be changed.")`
- `item_ids`: `ValueError("item_ids must be 1 to 50 Miro item ids separated by commas.")`
- `type` (list):
  `ValueError("type must be empty or one of sticky_note, card, text, shape, frame, image, document, embed, app_card.")`
- `max_results` outside its range:
  `ValueError(f"max_results must be between 1 and {upper}.")`
- `miro_update_board` with both empty:
  `ValueError("Give a new name, a new description, or both.")`; over-long values give the
  "longer than" message (60 and 300).

**Display helpers**
- `_plain(html_text: str) -> str` replaces each `<…>` tag with a space (`re.sub(r"<[^>]+>", " ", s)`),
  applies `html.unescape`, collapses whitespace and strips.
- `_cell(value) -> str`: `None` → `""`; a number → `f"{value:g}"`; else `_plain(str(value))`; cut to
  200 characters with a trailing `…`.
- `_item_text(item: MiroItem) -> str` is `_plain(item.content or item.title)`.

**`gated_call` arguments**
- `miro_list_items`: fetch `_board_info` and `list_items`, then gate.
  - `tool_name="Read Miro Items"`, `summary=f"Read {n} item(s) from {board.name}"`,
    `sender=board.name`, `raw_data=<the list of dicts>`, `filtered_data=<the same list>`,
    `gate="review"`.
  - `preview={…, "Type": type or "(all)", "Frame": frame_id or "(whole board)"}`.
  - `new_info={"Items": str(n), "Item content": "the text of every listed item"}`.
  - `details_text` is one line per item, `f"{item.type} #{item.id}: {_item_text(item)}"`, or
    `"(no items)"`. When truncated it is prefixed with
    `f"Showing the first {max_results} items; the board has more.\n\n"`.
  - `pii_scan_text=details_text`.
  - `preview_tables=[{"headers": ["id", "type", "text", "frame"], "rows": [...]}]`, omitted when
    empty; `table_only=True`.
  - Returns the list.
- `miro_get_items`: as above, with `tool_name="Read Miro Items by Id"` and
  `summary=f"Read {n} item(s) from {board.name}"`. Ids that are not found raise the "not found"
  `ValueError` before the gate.
- `miro_list_connectors`:
  - `tool_name="Read Miro Connectors"`, `summary=f"Read {n} connector(s) from {board.name}"`.
  - `new_info={"Connectors": str(n), "Captions": "the caption text of every listed connector"}`.
  - `details_text` is one line per connector,
    `f"#{c.id}: {c.start_item_id} → {c.end_item_id} {' / '.join(_plain(x) for x in c.captions)}"`,
    or `"(no connectors)"`. When truncated it is prefixed with
    `f"Showing the first {max_results} connectors; the board has more.\n\n"`.
  - Table headers `["id", "from", "to", "captions"]`, `table_only=True`.
- `miro_create_items`: validate, then `_board_info`, then `get_item` for each distinct `frame_id`
  (the "not a frame" check), then gate, then the frames and the bulk call in the order above. On a
  `MiroClientError` after k frames were created, it raises
  `RuntimeError(f"{exc} Frames created before the error: {', '.join(ids) or 'none'}.")`.
  - `tool_name="Add Miro Items"`, `summary=f"Add {n} item(s) to {board.name}"`,
    `raw_data={"board_id": board_id, "items": bodies}`, `filtered_data=None`, `gate="popup"`.
  - `preview={…, "Items": str(n)}`.
  - `preview_tables=[{"headers": ["Type", "Text", "Position", "Size", "Frame", "Color"], "rows": [...]}]`,
    where Position is `f"{x:g}, {y:g}"`, Size is `f"{w:g} × {h:g}"` with `-` for an unset side,
    Frame is the parent frame's title (`_cell(_plain(frame.title))`, from the `get_item` check),
    and empty cells are `""`.
  - `details_text=json.dumps(bodies, indent=2, ensure_ascii=False)`.
  - Returns `{"board_id": board_id, "created": [{"id": i.id, "type": i.type} for i in created]}`.
- `miro_create_connectors`: validate, `_board_info`, `get_item` for each distinct end (raising the
  "not found" and "frame" messages), gate, then `create_connector` for each, in order.
  - `tool_name="Add Miro Connectors"`, `summary=f"Draw {n} connector(s) on {board.name}"`,
    `preview={…, "Connectors": str(n)}`.
  - Table `["From", "To", "Shape", "Caption"]`, where an end reads
    `f"{item.type} {_cell(_item_text(item))[:60]}"`.
  - `details_text` is the bodies as indented JSON.
  - On a `MiroClientError` after k creations, raises
    `RuntimeError(f"{exc} Connectors created before the error: {', '.join(ids) or 'none'}.")`.
- `miro_update_items`: validate, `_board_info`, `get_item` for each id (raising "not found" and the
  type message), gate, then the requests for each change, in order.
  - `tool_name="Update Miro Items"`, `summary=f"Update {n} item(s) on {board.name}"`.
  - `preview={…, "Items": str(n), "Fields changed": str(m)}`.
  - Table `["Item", "Field", "Current", "New"]`, one row per given field, with Item
    `f"{item.type} #{item.id}"`. Current comes from the `MiroItem` field of the same name (`x`,
    `y`, `width`, `height`, `color`, `content`, `title`, `description`, `due_date`, `shape`,
    `frame_id`). An item in no frame reads `"(none)"` in the Current column.
  - The table is the summary (its cells strip tags and stop at 200 characters).
    `details_text` is exactly what will be sent:
    `json.dumps([{"id": …, "type": …, "update": <update_item body or None>, "frame_id": <given value, or omitted>} …], indent=2, ensure_ascii=False)`,
    so a link target or text past 200 characters is still visible to the approver.
  - On a `MiroClientError` after k items, raises
    `RuntimeError(f"{exc} Items changed before the error: {', '.join(ids) or 'none'}.")`.
- `miro_update_board`: `tool_name="Update Miro Board"`, `summary=f"Update board {board.name}"`,
  `preview={…}`, and table `["Field", "Current", "New"]` with only the given fields. Then
  `update_board`.
- **Auto tools** audit with:
  - `_auto_audit("miro_list_boards", "List Miro Boards", "List boards", f"{n} board(s)", t0)`;
  - `_auto_audit("miro_get_board", "Get Miro Board", f"Get board {board_id}", board.name, t0)`;
  - `_auto_audit("miro_list_tags", "List Miro Tags", f"List tags of {board_id}", f"{n} tag(s)", t0)`.

**Siblings** (for `TestToolDefinitions`):

| Tool | Siblings |
|---|---|
| `miro_list_boards` | `miro_get_board` |
| `miro_get_board` | `miro_list_boards`, `miro_list_items` |
| `miro_list_tags` | `miro_list_items` |
| `miro_list_items` | `miro_get_items`, `miro_create_items` |
| `miro_get_items` | `miro_list_items`, `miro_update_items` |
| `miro_list_connectors` | `miro_create_connectors` |
| `miro_create_items` | `miro_update_items` |
| `miro_create_connectors` | `miro_list_connectors` |
| `miro_update_items` | `miro_create_items` |
| `miro_update_board` | `miro_get_board` |

Each description names its siblings.

### 3.4 Policy tables

| Tool | `TOOL_TO_GATE` | `TOOL_TO_OPERATION` | `TOOL_TO_VERB` | `EFFECT_BY_TOOL` | `_TOOL_LAYOUT` |
|---|---|---|---|---|---|
| `miro_list_boards` | `auto` | — | — | — | — |
| `miro_get_board` | `auto` | — | — | — | — |
| `miro_list_tags` | `auto` | — | — | — | — |
| `miro_list_items` | `review` | `miro.list_items` | `READ` | — | `WIDE` |
| `miro_get_items` | `review` | `miro.get_items` | `READ` | — | `WIDE` |
| `miro_list_connectors` | `review` | `miro.list_connectors` | `READ` | — | `WIDE` |
| `miro_create_items` | `popup` | `miro.create_items` | `ADD` | `"New items are added to the board. Nothing already on it changes."` | `WIDE` |
| `miro_create_connectors` | `popup` | `miro.create_connectors` | `ADD` | `"New connector lines are drawn between the items shown. Nothing already on the board changes."` | `WIDE` |
| `miro_update_items` | `popup` | `miro.update_items` | `UPDATE` | `"The fields shown change to their new values. Other fields and items are not touched."` | `WIDE` |
| `miro_update_board` | `popup` | `miro.update_board` | `UPDATE` | `"The board's name or description changes as shown. Its content and sharing do not change."` | — (NARROW) |

**Rule scope: `miro.board`**, a new identity scope on `board_id`, resolved from the call's args.
- **`policy/scopes.NEW_SCOPE_SELECTORS`**:
  `"miro.board": ScopeSelector(predicate="miro.board", scope_type="miro.board", kind=ScopeKind.IDENTITY, resolves_from=ResolvesFrom.ARGS, matches=_miro_board_matches)`.
  `_miro_board_matches` has the `_peek_page_matches` shape (`policy/scopes.py:596`): `False` for an
  empty rule value or an empty `ctx.args.get("board_id", "")`, else membership in
  `_values_of(value)`. It compares verbatim; it never rewrites a link.
- **`policy/catalogue.EXTRA_SCOPES`**:
  `"miro.board": PolicyExtraScope(predicate="miro.board", connector="miro", verbs=(Verb.READ, Verb.ADD, Verb.UPDATE), label="Miro — board", needs_value=True, value_hint="uXjVOD6LSME=…board-id")`.
  It needs no `verb_predicates` (ADR 0228): every Miro tool has its own operation key, and that
  split is only for an operation key two verbs share.
- **`policy/value_format.VALUE_FORMATS`** (required by
  `test_every_value_needing_predicate_has_a_format_or_is_listed_as_unvalidated`):
  `"miro.board": _fmt(_MIRO_BOARD_ID_RE, "a Miro board id", "the id from the board's address (the part after /app/board/), not its name or the full link", "uXjVOD6LSME=")`,
  with `from ..miro_client import BOARD_ID_RE as _MIRO_BOARD_ID_RE` next to the Grist, Peek and
  Assets imports. A board link pasted on Settings or sent through
  `privacyfence_propose_policy_change` is refused with that message.
- **`policy/suggest._EXTRA_ARGUMENT`**: `"miro.board": "board_id"`, so `privacyfence_check_policy`
  suggests a `miro.board` rule for an uncovered Miro call, as it does for `grist.document`
  (`doc_id`) and `peek.page` (`slug`).
- **No `PROPOSABLE_SCOPES` entry.** The approval card never offers "Always allow" for a Miro
  operation. Rules are made on **Settings > Auto-accept**, through
  `privacyfence_propose_policy_change`, or from a `privacyfence_check_policy` suggestion (ADR 0077,
  ADR 0144 precedent; Grist, Peek and Apps Script work the same way).
- **The rule value is the board id**, and so is every tool's `board_id` (§3.2.2): one shape on both
  sides, compared verbatim.

### 3.5 Local mode: daemon, Settings and CLI

**`daemon_main.py`**
- `TOKEN_FILES["miro"] = "credentials/miro_token.json"`.
- In `build_connectors()`, after the Grist block:
  ```python
  if enabled("miro"):
      try:
          if not org_mode.connector_enabled_by_org(org_config, "miro"):
              raise MiroClientError("Miro organization config not installed")
          miro_config = miro_bundle_settings(org_config.get("miro") or {})
          token_path = _resolve_path(TOKEN_FILES["miro"])
          provider = MiroTokenProvider(miro_config, token_path, load_miro_token(token_path))
          client = MiroClient(provider)
          client.check_connection()
          connectors.append(MiroConnector(client))
      except MiroClientError as exc:
          logger.warning("Miro connector disabled: %s", exc)
          failures["miro"] = _classify_connector_failure(exc)
  ```
  `miro_bundle_settings`, `load_miro_token` and `miro_authorize_interactive` are
  `miro_auth.bundle_settings`, `load_token_file` and `authorize_interactive`, imported under those
  names (Grist's are imported the same way). The opening guard is the one the Grist, Telegram and
  Peek blocks have.
- `run_miro_oauth(org_config) -> int` and a `--miro-oauth` flag, in the shape of `run_grist_oauth`
  and its flag, `oauth_flag` entry and dispatch:

  | Outcome | Prints | Returns |
  |---|---|---|
  | No usable `miro` section | `"Miro OAuth setup failed: Miro organization config not installed"` (or the "incomplete" message) to stderr | 1 |
  | Success | `f"Miro OAuth complete. Signed in to the Miro team {team_id}."` | 0 |
  | `MiroClientError` | `f"Miro OAuth setup failed: {exc}"` to stderr | 1 |

**`org_mode.py`**
- `CONNECTOR_ORG_SECTION` gains `"miro": "miro"` (ADR 0196). `settings_controller.ORG_CONFIG_SERVICE`
  *is* this table, and `connector_enabled_by_org` indexes it for every connector in
  `ALL_CONNECTORS` (`_connectors_state`, `_org_allows`, `web/routes_connect.py`), so without the
  entry those raise `KeyError`.

**`settings_controller.py`**
- `"miro"` is appended to `ALL_CONNECTORS` and to `ORG_BUNDLE_SERVICES`. The row's `has_org` then
  follows the bundle through `connector_enabled_by_org`; no per-connector code.
- `authenticate_connector` gains `elif connector == "miro": self._authenticate_miro(org_config)`.
  Its existing `_org_allows` check runs first, so with no `miro` section the person sees
  `"Miro isn't enabled in your organization config."` and `_authenticate_miro` is not called.
- `_authenticate_miro` is `_authenticate_salesforce` (`:1370-1396`) with
  `miro_auth.bundle_settings` and `miro_auth.authorize_interactive`:
  - when `org_config.get("miro")` is empty or missing (reachable only by a direct call), it sets
    `self.error = "Miro organization config isn't installed yet."` and returns;
  - otherwise it calls `bundle_settings`, and a `MiroClientError` sets
    `self.error = f"Miro organization config is not usable: {exc}"`;
  - a sign-in failure sets `f"Miro authentication failed: {result}"`.
- `scripts/qa_authenticate_connectors.py`: add
  `OAuthStep("miro", "miro", "--miro-oauth", "Miro")` to `STEPS` after Salesforce, and name
  `miro` among the groups in its docstring.

### 3.6 Org mode

**`scripts/build_org_bundle.py`**
- A "Miro" argument group with `--miro-client-id` and `--miro-client-secret`, both or neither:
  `SystemExit("--miro-client-id and --miro-client-secret must be given together.")`.
- With both, it writes `bundle["miro"] = {"client_id": …, "client_secret": …}`.
- `"miro"` joins the `services` list after `"grist"`, and `_CONNECTOR_CALLBACKS["miro"] = ("miro",)`.

**`web/routes_connect.py`**
- `"miro"` goes into `OAUTH_SERVICES`, `_GRANT_KEY`, `SERVICE_LABELS` (`"Miro"`) and
  `_ORG_CONFIG_SECTION`, and is appended to the `other_rows` tuple (`:976`) after `"confluence"`.
- `_is_configured("miro")` is the default `client_id` check.
- `_build_authorize_url` gets a `miro` branch: `cfg = miro_auth.bundle_settings(section)` (a
  `MiroClientError` raises `_NotConfigured`), and
  `url = miro_auth.build_authorize_url(cfg.client_id, redirect_uri, state)`. It returns
  `(url, "")`, as the Slack branch does (no PKCE).
- `_exchange_and_save` gets a `miro` branch:
  `record = miro_auth.exchange_code(cfg, code, redirect_uri)`, then
  `miro_auth.save_token_file(token_file, record)`. A `MiroClientError` follows the existing
  sign-in-failed path (the way the Grist branch's errors do).
- The org redirect URI is `https://<server>/oauth/callback/miro`. One Miro app can carry it and the
  local `http://localhost:53686/callback`.
- `_is_connected("miro")` keeps the default (the token file exists).

### 3.7 What is deliberately not built

- **No delete** of boards, items, frames, connectors or tags. No board create or copy, no sharing
  or member changes, and no board `policy`, `teamId` or `projectId`.
- **No tag writes** or tag attachment.
- **No images, documents, embeds or app cards**, and no groups.
- **No comments:** Miro's public REST API cannot read them.
- **No Enterprise APIs.** Nothing under `/v2/orgs`, no Teams, Projects, audit logs, board export,
  legal holds, content logs or data classification.
- **No other sign-in.** No non-expiring tokens, no personal tokens from Miro's "Install app and get
  OAuth token" button, and no PKCE-less public client.
- **One Miro team per person.** Connecting a second team replaces the first.
- **No board links.** Tools and rules take the board id only (§3.2.2, §3.4).
- **No board-name resolver on Settings > Auto-accept.** A `miro.board` rule shows the board id, as
  Peek's rules show page ids. Grist's resolver (`settings_controller.py:213-215`,
  `policy/resource_registry.py:348-355`) is not copied.
- **No other website page changes.** The untested provider-name lists on the home, FAQ, GDPR,
  privacy, security, enterprise and Claude-connectors comparison pages (§2) do not list Jira Assets
  either; they stay as they are.
- **No connector icon.** Only real brand assets go in `resources/connector_icons/`, and the website
  shows no logos (ADR 0101).
- Adding any of these later is a new decision.

### 3.8 Setup guide `miro-setup.md` (in `docs/`)

Modelled on the Grist and Salesforce guides. `p3-connector-listing` writes a skeleton: every
heading below with one sentence under it. `p12-docs-adrs-retire` writes the full content and checks
it against the code. Sections:
- `# Miro setup`: what the assistant can do, and that nothing is deleted.
- `## Register the PrivacyFence app` (for administrators):
  1. Create a Developer team in Miro if there is none (Miro's "Create a Developer team" page).
  2. **Create new app**, named `PrivacyFence`, with **Expire user authorization token** turned on.
     Say plainly that it can only be chosen when the app is created; an app without it is refused
     at sign-in.
  3. **Permissions** `boards:read` and `boards:write` only.
  4. **Redirect URI for OAuth2.0**: `http://localhost:53686/callback`, and for an organization
     server `https://<server>/oauth/callback/miro`.
  5. Copy the Client ID and Client secret.
  6. Under **Share app**, copy the Installation URL for people outside the Developer team; signing
     in from PrivacyFence also works without it.
- `## Values`: a table of client id and client secret with their `build_org_bundle.py` options.
- `## Build and distribute the bundle`.
- `## Connect`: Settings → Connectors → Miro → **Authenticate…** (desktop) or **Connect** on the
  connections page (organization server). Miro's page asks which team to install the app in; one
  team per connection; connecting again with another team replaces it.
- `## What the assistant can do`: the ten tools with their gates, linking the tools reference
  `#miro`. Cover:
  - batches of up to 20 items under one approval;
  - positions inside a frame are measured from the frame's corner;
  - an item can be moved into a frame but not taken out of one (do that in Miro);
  - content may be simple HTML;
  - comments cannot be read.
- `## Auto-accept rules`: `miro.board`, set on Settings > Auto-accept with the board id (the part of
  the board's address after `/app/board/`; a full link is refused), or from a suggestion of
  `privacyfence_check_policy`; no Always allow on the card.
- `## Troubleshooting`: the exact error texts from §3.2.1 and §3.2.2 that a person can meet, with
  what to do. In particular: the non-expiring-token message, the missing-permission message, 403
  (the board is in another team, so connect again and choose that team), and 429.

## 4. ADRs

- **0230**: Miro connects only by OAuth.
  - The connection uses an organization-registered app whose tokens expire: authorization code,
    confidential client, `state`, and no PKCE (Miro documents none).
  - The app's permissions are `boards:read` and `boards:write`, set on the app because Miro takes no
    `scope` parameter. Sign-in refuses a token without either one, and refuses a token that never
    expires.
  - Tokens and the client secret go in the form body, never the URL.
  - One per-principal file, `credentials/miro_token.json`, written only by
    `miro_auth.save_token_file` (`atomic_write_json`, 0600). A refresh rotates the refresh token.
  - A connection is one Miro team.
  - Rejected:
    - non-expiring tokens (a leaked token works until the app is uninstalled);
    - personal tokens from Miro's app page;
    - query-string credentials as Miro's SDKs send them (request-line logging could capture them);
    - Miro's own MCP server (`mcp.miro.com`, live since 2026) instead of a connector: it has no
      PrivacyFence approval card, policy or audit, runs with its own OAuth 2.1 grant outside the
      organization bundle, and is enabled per Enterprise organization.
  - A refresh re-reads the token file first, so two live instances cannot void each other's
    refresh token.
- **0231**: The Miro connector reads, adds and changes, and never deletes.
  - It leaves out every Enterprise API, sharing and member changes, board create and copy, and tag
    writes.
  - It cannot read comments, because Miro's public REST API has no endpoint for them.
  - It cannot take an item out of a frame: Miro's API answers 200 to `{"parent": null}` and keeps the
    parent, so the connector refuses that change before the card instead of reporting a success
    that did not happen, and confirms every move into a frame by reading the item back.
  - Rejected:
    - delete tools (a delete of a board or frame takes everything inside it, and the card could show
      only ids);
    - board `policy` changes (they change who can see the board);
    - Enterprise export and audit APIs (organization-wide compliance access, not one person's
      boards).
- **0232**: Miro auto-accept rules are per board (`miro.board`), made on Settings, through
  `privacyfence_propose_policy_change` or from a `privacyfence_check_policy` suggestion, never from
  the approval card. A rule value must be a board id; a board link is refused when the rule is saved
  (`policy/value_format.py`, reusing `miro_client.BOARD_ID_RE`), and the tools take ids only, so a
  suggestion's value is always one a rule can hold.
  - Rejected: accepting board links in the tools and rewriting them to ids (the rule side refuses
    URLs, so `check_policy` would suggest rules that cannot be saved).
  - Rejected: adding the scope to `SCOPE_SELECTORS`, the frozen v1 set; ADR 0077 and ADR 0144 are
    the precedent.
- **0233**: A Miro write covers a batch of up to 20 items or connectors under one approval card.
  - Creating sticky notes, cards, texts and shapes is Miro's transactional bulk call: all or none.
    Frames are not part of Miro's bulk call, so they are created first, one request each.
  - Updates and connectors go one request at a time after the approval. They stop at the first
    error, report what was already done, and never retry.
  - Rejected:
    - one card per item (a diagram of twenty stickies would be twenty approvals);
    - batches beyond 20 (Miro's bulk limit, and a card that cannot be read).

## 5. Manual steps

The step-by-step page is `manual_steps_artifact` in the manifest.

**Before implementation**
- **`mb1-miro-qa-app`**: a Miro QA account with a Developer team, the QA app (expiring tokens on,
  `boards:read` and `boards:write`, redirect `http://localhost:53686/callback`), a seed board and a
  lifecycle board.
- **`mb2-runner-qa-state`**: on the self-hosted runner:
  - a `miro` section in the QA bundle;
  - a token file obtained by hand with the form-body exchange of §3.2.1;
  - the board ids in `qa_environment.yaml`.

  `p11-qa-live` dispatches `qa-record-fixture.yml`, which fails without them. `mb2` also
  confirms that Miro accepts the form-body token request (§6).
- **`mb3-miro-write-probe`**: within the hour after `mb2`'s exchange, on the runner, run the probe
  script from the manual-steps page with the access token from `mb2`'s answer. It never refreshes,
  so the token file stays valid. On the lifecycle board only it:
  1. creates a frame through `POST /v2/boards/{id}/frames`;
  2. creates, in one `POST …/items/bulk`, a sticky note with `parent` that frame and a shape with
     `style.fillColor` `#fff9b1`;
  3. sends `PATCH …/items/{sticky id}` with `{"parent": null}`;
  4. reads the sticky note back and prints whether it still has a parent.

  No CI run can test these writes before the feature merges (§2 "QA"), so this is where they are
  proven. The items it creates stay on the lifecycle board, are tagged `[QATEST]`, and are cleaned
  up by hand like the lifecycle's.

**After implementation**
- **`ma1-local-mode-check`**: sign in from Settings and drive every Miro tool from a real AI client
  in local mode.
- **`ma2-org-mode-check`**: if an org-mode test deployment exists, sign in on `/connect`.
- **`ma3-lifecycle-on-main`**: after the merge, dispatch `connector-live-check.yml` (or wait for
  Monday's run) and check that the Miro lifecycle row passes. It is the first run of
  `lifecycle_miro` against Miro.

## 6. Risks and open questions

- **Form-body token requests.**
  - Risk: Miro's SDKs send the token request as query parameters; this plan sends a form body.
  - Check: `mb2` performs exactly that request before any phase starts.
  - Its done-when tells the user not to confirm `mb2` if only the query-parameter request worked.
    The plan is then revised (§3.2.1 and ADR 0230) before `/implement` starts. No phase checks this
    itself.
- **Taking an item out of a frame: settled, not supported.** `mb3` sent `{"parent": null}` on
  2026-10-25; Miro answered HTTP 200 and kept the parent. The plan applies the fallback throughout:
  - `connectors/miro.py` refuses a change with `frame_id == ""` before the gate, and
    `MiroClient.set_parent` refuses `""` the same way (§3.2.2, §3.3);
  - the `miro_update_items` description, the setup guide (§3.8) and ADR 0231 say an item cannot be
    taken out of a frame;
  - because Miro answers 200 to a parent change it ignores, `set_parent` reads the item back and
    raises when a move into a frame did not happen;
  - `lifecycle_miro` moves an item into a frame and checks it, and never tries a move out.
  - If Miro later supports it, adding it back is a new decision.
- **Token context endpoint.**
  - Risk: `GET /v1/oauth-token` may refuse a `boards:*` token.
  - That is handled: `check_connection` falls back to `GET /v2/boards`, and cards show
    "(not shown by Miro)".
  - If `/v2/boards` is refused too, stop with `status=blocked`.
- **Response shapes.**
  - The parsers tolerate missing optional keys (`""` or `None`).
  - `p11-qa-live` records real responses. A parser a fixture contradicts is fixed in that phase.
  - If a recorded shape makes a §3.3 behaviour impossible, stop with `status=blocked`.
- **Frames and colors on the live board.**
  - Risk: `create_frame` and the 16 `FILL_COLORS` come from the spec.
  - Check: `mb3` creates one frame and one shape with fill color `#fff9b1` by hand.
  - If Miro refuses either, `mb3` is not confirmed and the plan is revised before `/implement`.
- **The seed board stays small.** `RawCapture` keeps the last page of a paged call. The seed board
  holds fewer than 50 items, so `list_items.json` is one whole page.
- **`miro.board` and the popup.** If, after `p7-policy-scope`, an approval card offers "Always
  allow" for a Miro tool, stop with `status=blocked`.
- **Tests that enumerate connectors.**
  - `tests/unit/test_daemon_main.py`, `test_settings_controller.py`, `web/test_routes_connect.py`
    and `test_qa_authenticate_connectors.py` pin connector lists or counts. Update them to include
    `miro`.
  - If one asserts something this plan does not account for, stop with `status=blocked` and name
    the test.
- **The website goes live on merge.** `pages.yml` deploys `website/` from `main`, so
  `/connectors/miro/` is public once the feature PR merges, before a release carries the connector.
  Its "Set it up" button links the guide on GitHub's `main`, as Grist's did.
- **Counts on the website.**
  - Each tool phase changes the totals. Always take them from the regenerated
    `docs/tools-reference.md` summary table.
  - The expected values:

    | After phase | Miro row | Total row |
    |---|---|---|
    | `p3-connector-listing` | `3 · 3 · 0 · 0` | `159 · 57 · 35 · 67` |
    | `p4-reads` | `6 · 3 · 3 · 0` | `162 · 57 · 38 · 67` |
    | `p5-create` | `8 · 3 · 3 · 2` | `164 · 57 · 38 · 69` |
    | `p6-update` | `10 · 3 · 3 · 4` | `166 · 57 · 38 · 71` |

  - If the starting total is not 156 (another branch merged first), add Miro's counts to whatever
    it is.
- **The runner credential.**
  - If the dispatched `qa-record-fixture.yml` fails at its "copy QA state" step, or with "Miro is
    not authenticated" or "Your Miro sign-in has expired or was revoked", `mb2` is not done (or the
    hand-written token's 60-day refresh window passed). It is refreshed by the run and rotated by
    hand with `scripts/qa_authenticate_connectors.py --only miro` once the feature has merged.
  - Stop with `status=blocked` and say so.

## Implementation manifest

```yaml
plan_slug: miro-connector
feature_branch: feature/miro-connector
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/WvzgB68aY5xe5byJcDSVfs
manual_steps_source: docs/miro-connector-plan-manual-steps.html
manual_before:
  - id: mb1-miro-qa-app
    title: Create a Miro QA account, Developer team, QA app (expiring tokens, boards:read and boards:write), a seed board and a lifecycle board
    why: p11-qa-live records live fixtures from the seed board through this app, mb3 probes the writes on the lifecycle board, and lifecycle_miro runs there weekly once merged; without them the recording has nothing to read.
    done_when: The QA app exists with "Expire user authorization token" on, permissions boards:read and boards:write only, and redirect URI http://localhost:53686/callback; its client id and secret are in your password manager; the board "PrivacyFence QA [QATEST]" holds a frame "QaFrame [QATEST]" with two [QATEST] sticky notes inside it, one [QATEST] sticky note outside it, one connector line between two of the sticky notes and a tag QATEST; and the board "PrivacyFence QA lifecycle [QATEST]" exists with a frame "QaLifecycleFrame [QATEST]".
  - id: mb2-runner-qa-state
    title: Put the Miro app in the runner's QA bundle, a hand-obtained Miro token file on the runner, and the board ids in qa_environment.yaml
    why: p11-qa-live dispatches qa-record-fixture.yml, which reads ~/privacyfence/org/org_config.json (miro section), ~/privacyfence/credentials/miro_token.json and the miro section of ~/privacyfence/tests/fixtures/qa_environment.yaml on the runner; without them the run fails. The token request also proves that Miro accepts a form-body token request (plan §6).
    done_when: On the runner, org/org_config.json has "miro" {"client_id", "client_secret"} and still loads (re-signed if it is signed); credentials/miro_token.json has mode 600 and holds access_token, refresh_token, "expires_at" 0 and team_id; qa_environment.yaml has a miro section with board_id and lifecycle_board_id. The form-body token request worked. If only the query-parameter one did, do not confirm this step; tell the planner session instead, because the plan must be revised before /implement starts (plan §6).
  - id: mb3-miro-write-probe
    title: Within the hour after mb2's token exchange, run the Miro write probe on the runner against the lifecycle board
    why: No CI run can execute Miro writes against a branch (connector-live-check.yml checks main's code only), so the three undocumented or spec-only behaviours the plan depends on (frame create, a fill color from the documented list, and taking an item out of a frame with a null parent) are proven here before any phase is built on them (plan §6).
    done_when: The probe printed "frame created", "bulk created 2 items" and "fill color accepted". Its move-out line is expected to say "move-out NOT supported" (run on 2026-10-25, plan §6); if it says "move-out works" instead, tell the planner session, because the plan refuses move-outs. Any other failure (frame, bulk create or fill color refused) means the plan must be revised before /implement starts.
manual_after:
  - id: ma1-local-mode-check
    title: Sign in to Miro from Settings and drive every Miro tool from an AI client in local mode
    why: Proves the real browser sign-in (the live check uses a hand-made token), what the approval cards show (board, team, old→new tables) and that real writes land, which unit tests and the recorder cannot show.
  - id: ma2-org-mode-check
    title: (If you run an org-mode test deployment) connect Miro on /connect
    why: Proves the bundle section, the org redirect URI and the server-side code exchange; no CI job runs an org deployment against Miro.
  - id: ma3-lifecycle-on-main
    title: After the merge, dispatch connector-live-check.yml on main (or wait for Monday's run) and check the Miro lifecycle row
    why: lifecycle_miro can only run on main's code; this is its first run against Miro, and it repeats mb3's writes through the real client.
verify_after_merge:
  - python3 -m pytest tests/unit/test_miro_auth.py tests/unit/test_miro_client.py tests/unit/test_systemic_gate_invariants.py -q
  - python3 -m pytest tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_website_docs_allowlist.py tests/unit/test_connector_tool_annotations.py tests/unit/test_docs_references_exist.py -q
  - python3 -m pytest tests/unit/connectors -q -k miro
  - python3 -m pytest tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_generate_always_allow_reference.py -q
  - python3 -m pytest tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py tests/unit/web/test_routes_connect.py tests/unit/test_build_org_bundle.py tests/unit/test_qa_fixture_recorder.py tests/unit/test_qa_authenticate_connectors.py -q
final_checks:
  - docs/miro-connector-plan.md and docs/miro-connector-plan-manual-steps.html are deleted and nothing links to them (grep -rn "miro-connector-plan" . --exclude-dir=.git finds nothing)
  - ADRs 0230, 0231, 0232 and 0233 exist in docs/adr/, are Accepted, and are in the docs/adr/README.md index (renumbered if main took those numbers first)
  - CHANGELOG.md has the Miro entry under "## [Unreleased]" and no new version heading
  - After python3 scripts/generate_tools_reference.py and python3 scripts/generate_always_allow_reference.py, git diff --exit-code docs/tools-reference.md docs/always-allow-rules-reference.md exits 0
  - The PR description links the two qa-record-fixture.yml runs (dispatched on main with branch= the p11-qa-live and the p12-docs-adrs-retire phase branch); the p12 one is the definition-of-done QA row. It notes that the Miro lifecycle row is first checked on main after the merge (ma3)
phases:
  - id: p1-auth
    title: Miro auth module (bundle section, token file, sign-in, refresh) and its tests
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/miro_auth.py
      - tests/unit/test_miro_auth.py
      - tests/unit/test_systemic_gate_invariants.py
    brief: |
      Read first: the must-read docs (CLAUDE.md, CONTRIBUTING.md, docs/releasing.md, docs/coding-and-testing-guidelines.md,
      docs/testing-policy.md, docs/adr/README.md), plan §3.0, §3.1 and §3.2.1 (the spec), and the Grist functions this copies in
      shape: src/privacyfence/grist_auth.py:122-188 and 397-490, and oauth_loopback.run_browser_oauth (oauth_loopback.py:148-156).
      1. Create src/privacyfence/miro_auth.py exactly as plan §3.2.1: module docstring, MiroClientError, the constants,
         MiroOAuthConfig (repr hides the secret) and bundle_settings, save_token_file, load_token_file, _token_request,
         build_authorize_url, exchange_code, refresh, MiroTokenProvider and authorize_interactive. All HTTP through requests
         with data= (form body), timeout=30 and allow_redirects=False. Exact error strings from §3.2.1. Never log a token, a
         code or the client secret.
      2. tests/unit/test_miro_auth.py (pytestmark = pytest.mark.unit; module docstring naming the invariant "a Miro token is
         only kept when it expires and carries boards:read and boards:write"). Fake requests.post with monkeypatch (no network).
         Classes: TestBundleSettings (empty → "not installed"; one key → "incomplete"; both → config; repr hides the secret),
         TestTokenFile (round trip; mode 0o600 on POSIX; missing file, each missing required key, non-number expires_at → the
         not-authenticated message; invalid JSON and non-object → the "could not be read" message; user_id and scope default
         to ""), TestBuildAuthorizeUrl (exactly response_type, client_id, redirect_uri, state; no scope, no code_challenge),
         TestTokenRequest (credentials in data=, never in the URL or params; allow_redirects False; network error, 302, 500,
         non-JSON body → their messages; returns (status, body)), TestExchangeCode (record shape and expires_at; 400 with
         message → "Miro sign-in failed: …"; no access_token, no team_id, expires_in "soon" or 0 → the "incomplete sign-in
         answer" message naming that key; no refresh_token → the non-expiring message; scope "boards:read" only → the
         missing-permission message naming boards:write; extra scopes accepted; no scope key accepted), TestRefresh (new
         refresh_token replaces the old; absent keeps the old; no access_token → the incomplete message; 400 and 401 → the
         expired message; 403 with JSON → "Miro sign-in refresh failed: …"), TestMiroTokenProvider (no refresh and no file
         read while valid; refresh 60 s before expiry, at expires_at 0 and on force_refresh; the refreshed record is saved;
         when the file on disk holds a different refresh_token with a valid expires_at, it is adopted with no HTTP request;
         when it holds a different but expired one, the refresh uses the file's refresh_token; an unreadable file is ignored
         and the refresh uses memory), TestAuthorizeInteractive (run_browser_oauth monkeypatched: port
         53686, path /callback, redirect_host localhost; the build callback ignores the challenge; OAuthLoopbackError →
         "Miro sign-in failed: …"; the record is saved). Assert no token, code or secret appears in any raised message or repr.
      3. tests/unit/test_systemic_gate_invariants.py: add ("miro_auth", None, "save_token_file") to TOKEN_WRITE_SITES (after the
         grist_auth entry: the tuple is alphabetical) and rename
         test_twelve_token_write_sites_are_listed to test_thirteen_token_write_sites_are_listed asserting len == 13.
      4. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: if oauth_loopback.run_browser_oauth's signature differs from plan §2, stop with status=blocked.
    acceptance:
      - python3 -m pytest tests/unit/test_miro_auth.py tests/unit/test_systemic_gate_invariants.py -q passes
      - python3 -m pytest tests/unit/test_miro_auth.py -q --cov=privacyfence.miro_auth --cov-branch --cov-report=term-missing reports 100% for src/privacyfence/miro_auth.py
      - 'grep -n "MIRO_OAUTH_PORT = 53686" src/privacyfence/miro_auth.py matches'
      - grep -n "verify=False" src/privacyfence/miro_auth.py finds nothing
      - grep -n "params=" src/privacyfence/miro_auth.py finds nothing
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p2-client
    title: Miro REST client and its tests
    depends_on: [p1-auth]
    complexity: M
    touches:
      - src/privacyfence/miro_client.py
      - tests/unit/test_miro_client.py
    brief: |
      Read first: plan §3.0, §3.1 and §3.2.2 (the spec), src/privacyfence/miro_auth.py from the previous phase, and
      src/privacyfence/grist_client.py as the style template.
      1. Create src/privacyfence/miro_client.py exactly as plan §3.2.2: re-exported MiroClientError, MiroNotFound,
         MiroAccessDenied, the constants, the id regexes (BOARD_ID_RE public: policy/value_format.py imports it in p7) with
         validate_board_id and validate_item_id, the four dataclasses
         and their parsers, MiroClient with _request (the single choke point; one force_refresh retry on 401; the error
         table), check_connection (with the 403 fallback) and every method. Exact strings from §3.2.2.
      2. tests/unit/test_miro_client.py (pytestmark unit; module docstring). Build a client on a fake provider
         (access_token(*, force_refresh=False) returns "at-1", then "at-2" after a forced refresh) and monkeypatch the
         client's requests.Session.request (no network). Classes: TestIds (a board id with "="; a board link https://miro.com/app/board/uXjVOD6LSME=/ rejected with
         the message naming /app/board/; "../x", "a b", "" rejected; path quoting turns "/" into %2F; item id digits only),
         TestRequestErrors (every row of the error table with its exact message; 429 with and without X-RateLimit-Reset;
         401 then success after one forced refresh; 401 twice; allow_redirects=False and the Authorization header asserted;
         no token in any message), TestCheckConnection (team name from /v1/oauth-token; 403 falls back to GET /v2/boards
         limit=1 and team_name ""), and one class per method asserting method, path, params/body and parsing:
         TestListBoards (offset paging stops at max_results and at MAX_PAGES), TestGetBoard (missing keys give ""; no owner field),
         TestUpdateBoard (only given keys sent; never policy/teamId/projectId), TestListTags, TestListItems (cursor paging;
         type and parent_item_id sent only when non-empty; truncated flag; MiroItem field mapping for each type, color ""
         from the generic answer, due_date cut to 10 chars), TestGetItem (generic GET then the per-type GET for each of the
         five editable types, color taken from the per-type style per type; an image makes only the generic GET; 404 raises
         MiroNotFound), TestListConnectors, TestCreateItems (one POST to /items/bulk with the list), TestCreateFrame (one POST
         to /frames), TestCreateConnector, TestUpdateItem (endpoint per type;
         an image type raises the "cannot be changed here" message before any request), TestSetParent (body with id; body
         "" rejected with the move-out message before any request; after the PATCH, get_item is called and a frame_id
         other than the one asked for raises the "did not move it" message). Each id-taking method rejects a bad id
         before any request.
      3. ruff check . and python3 -m pytest tests/unit -q.
      Definition of done: the /dod live-check row is deferred to p11-qa-live and p12-docs-adrs-retire; dispatch no
      workflow in this phase (plan §3.0).
    acceptance:
      - python3 -m pytest tests/unit/test_miro_client.py -q passes
      - python3 -m pytest tests/unit/test_miro_client.py -q --cov=privacyfence.miro_client --cov-branch --cov-report=term-missing reports 100% for src/privacyfence/miro_client.py
      - grep -n "allow_redirects=False" src/privacyfence/miro_client.py matches
      - grep -n "verify=False" src/privacyfence/miro_client.py finds nothing
      - grep -En '"DELETE"|/orgs|/members|copy_from' src/privacyfence/miro_client.py finds nothing
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p3-connector-listing
    title: MiroConnector with the three auto tools, and every per-connector table, page and guide a new connector module requires
    depends_on: [p2-client]
    complexity: M
    touches:
      - src/privacyfence/connectors/miro.py
      - tests/unit/connectors/test_miro_connector.py
      - src/privacyfence/auto_accept.py
      - scripts/pyinstaller_common.py
      - scripts/generate_tools_reference.py
      - scripts/build_site.py
      - docs/tools-reference.md
      - docs/miro-setup*.md
      - docs/README.md
      - README.md
      - website/connectors/miro/index.html
      - website/_partials/other-connectors.html
      - website/connectors/index.html
      - website/how-it-works/index.html
      - website/compare/mcp-gateways/index.html
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_website_connector_pages.py
      - tests/unit/test_website_connectors_page.py
      - tests/unit/test_website_pages.py
    brief: |
      Read first: the must-read docs, plan §3.3 (connector spec), §3.7, §3.8 (setup guide), and src/privacyfence/connectors/grist.py
      (template for _fetch at l.1540-1545, _auto_audit at l.1547-1566, tool_specs style; ignore the export_files
      wrapping, which is only for export tools) with tests/unit/connectors/test_grist_connector.py (gated_call_spy at
      :41-50, TestToolDefinitions, TestEveryToolIsAudited).
      1. Create src/privacyfence/connectors/miro.py: module docstring, MiroConnector(Connector) with name "miro",
         __init__(client: MiroClient), client property, _fetch, _auto_audit and _board_info as plan §3.3, tool_specs()
         returning ONLY miro_list_boards, miro_get_board and miro_list_tags with the §3.3 descriptions, params and siblings
         (write the sibling sentences so later tools can be added; a description may name a tool that a later phase adds),
         and call() dispatching them (unknown tool → ValueError(f"Unknown Miro tool: {tool!r}")). Return shapes per §3.3.
      2. src/privacyfence/auto_accept.py TOOL_TO_GATE: the three tools "auto", under a "# Miro" comment line after Peek's.
      3. Bookkeeping: "privacyfence.connectors.miro" to scripts/pyinstaller_common.py's list (after the peek entry, l.88); "miro": "Miro" to both
         CONNECTOR_TITLES and CONNECTOR_SHORT in scripts/generate_tools_reference.py (after "peek"); MiroConnector to
         CONNECTOR_CLASSES (and its import) in tests/unit/connectors/test_readme_manifest_alignment.py.
      4. python3 scripts/generate_tools_reference.py regenerates docs/tools-reference.md (Miro row 3 · 3 · 0 · 0; plan §6 counts).
      5. Website and docs, required by tests/unit/test_website_connector_pages.py and test_website_connectors_page.py:
         a. The setup guide miro-setup.md in docs/ as a skeleton: every heading in plan §3.8, each with one plain sentence
            saying what the section covers (p12-docs-adrs-retire writes the full content).
         b. README.md "## Connectors" table: row "| Miro | List boards and tags; read items and connector lines after review; add and change sticky notes, cards, texts, shapes, frames and connector lines with approval (nothing is deleted) |" after Peek.
         c. website/connectors/miro/index.html, modelled on website/connectors/grist/index.html (same head, meta
            pf-content-group connector, canonical/og URLs for /connectors/miro/, the other-connectors include with
            current="miro"). Copy: boards and tags straight away, items read after review, adding and changing only with
            approval, batches of up to 20 under one card, nothing deleted, sign-in with your organization's Miro app, one
            Miro team per connection. No logo (ADR 0101). Its "Set it up" button is exactly
            <a class="button primary" href="GUIDE_URL">Set it up</a> where GUIDE_URL is
            https://github.com/privacyfence/privacyfence/blob/main/ followed by docs/ and miro-setup.md, with no space.
         d. scripts/build_site.py: "/connectors/miro/": "connectors/miro/index.html" in PAGES (after peek) and "miro-setup" in
            CONNECTOR_GUIDES.
         e. tests/unit/test_website_connector_pages.py CONNECTORS: "miro": ("/connectors/miro/", "miro-setup", "Miro").
         f. website/connectors/index.html: a Miro card at the position of Miro's row in the summary table (last), copying the
            Grist card's markup with id="miro", data-connector="Miro", data-tools="3" data-auto="3" data-review="0"
            data-popup="0", the line "3 tools: 3 without a card · 0 reviewed · 0 need approval", links to the tools
            reference #miro and /connectors/miro/; heading "Fifteen connectors, 159 tools, each with a fixed gate."
         g. website/how-it-works/index.html: "156 connector tools" → "159 connector tools".
         h. website/compare/mcp-gateways/index.html: "fourteen connectors" → "fifteen connectors" (keep the surrounding words).
         i. tests/unit/test_website_connectors_page.py: len(REFERENCE) == 15 and "Fifteen connectors" (line 60).
         j. website/_partials/other-connectors.html: add <li data-connector="miro"><a href="/connectors/miro/">Miro</a></li>
            after the Peek line, and change the header comment's "the other six" to "the other seven".
         k. docs/README.md: add "- [`miro-setup.md`](miro-setup.md)" after the peek-setup.md line under "### Connector setup".
         l. tests/unit/test_website_pages.py: rename test_there_are_fourteen_second_level_pages to
            test_there_are_fifteen_second_level_pages and assert len(SECOND_LEVEL) == 15 (/connectors/miro/ is the new one).
      6. Create tests/unit/connectors/test_miro_connector.py (module docstring; pytestmark unit) with a gated_call_spy fixture
         as Grist's: TestDispatch (unknown tool → ValueError); TestListBoards, TestGetBoard, TestListTags (never call
         gated_call — spy stays empty; each writes an auto_accepted audit entry with the tool name; a MiroClientError becomes
         RuntimeError; a bad board_id raises ValueError before the client is called; a board link is rejected with the validate_board_id message; max_results
         range message); TestToolDefinitions with MIRO_SIBLINGS restricted to the registered tools, calling
         assert_tool_definitions_complete; TestEveryToolIsAudited calling assert_all_tools_leave_an_audit_trail.
      7. ruff check . and python3 -m pytest tests/unit -q (many cross-connector tests discover the new module), and
         python3 scripts/build_site.py --out /tmp/pf-site-check --no-docs --offline.
      Stop condition: if a unit test outside touches fails because of the new connector module (another hand-maintained
      connector list this plan does not name), stop with status=blocked and name the test.
      Definition of done: the /dod live-check row is deferred to p11-qa-live and p12-docs-adrs-retire; dispatch no
      workflow in this phase (plan §3.0).
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_miro_connector.py -q passes
      - python3 -m pytest tests/unit/test_website_pages.py tests/unit/test_website_docs_allowlist.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connector_pages.py tests/unit/test_website_connectors_page.py tests/unit/test_pyinstaller_hidden_imports.py tests/unit/web/test_tool_schema_portability.py tests/unit/test_connector_tool_annotations.py tests/unit/test_systemic_gate_invariants.py tests/unit/test_docs_references_exist.py -q passes
      - grep -n "| \[Miro\](#miro) | 3 | 3 | 0 | 0 |" docs/tools-reference.md matches
      - python3 scripts/build_site.py --out /tmp/pf-site-check --no-docs --offline exits 0
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p4-reads
    title: miro_list_items, miro_get_items and miro_list_connectors, the review-gated reads
    depends_on: [p3-connector-listing]
    complexity: M
    touches:
      - src/privacyfence/connectors/miro.py
      - tests/unit/connectors/test_miro_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - tests/unit/policy/test_registry.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
    brief: |
      Read first: plan §3.3 (the three tools, their validation, _plain/_cell/_item_text and gated_call arguments) and §3.4;
      src/privacyfence/connectors/grist.py _get_records (l.767-843) as the pattern.
      1. connectors/miro.py: add the three tools to tool_specs() and call(), the display helpers, and the three handlers with
         exactly the §3.3 gated_call arguments (pii_scan_text=details_text on each). Extend MIRO_SIBLINGS in the test.
      2. Tables (§3.4): auto_accept TOOL_TO_GATE review ×3 and TOOL_TO_OPERATION; policy/registry.py TOOL_TO_VERB Verb.READ ×3
         and the same three rows in _VERB_MATRIX of tests/unit/policy/test_registry.py, with its size assertion (l.296)
         102 → 105; gate._TOOL_LAYOUT WIDE ×3.
      3. python3 scripts/generate_tools_reference.py and python3 scripts/generate_always_allow_reference.py; website card →
         data-tools="6" data-auto="3" data-review="3" data-popup="0", "6 tools: 3 without a card · 3 reviewed · 0 need
         approval", heading total 162; how-it-works "162 connector tools" (plan §6 counts).
      4. Tests: TestListItems — preview has exactly Board, Team, Type, Frame (Board first) and no item text; details_text and
         the table carry the text with HTML tags stripped and entities unescaped; pii_scan_text == details_text; args ==
         {"board_id": <the stripped id>}; type and frame_id passed to the client; truncated prefix; empty result gives
         "(no items)" and no table; each validation message (spy stays empty); MiroClientError → RuntimeError; team_name ""
         gives "(not shown by Miro)". TestGetItems — ids parsed (spaces allowed), more than 50 or a non-digit id → its
         message, a MiroNotFound for one id → the "not found" ValueError naming it and no gate. TestListConnectors — table and
         details lines. TestFieldCompleteness — a real MiroClient with a faked _request returning a fully populated board and
         items response, run through miro_list_items, checked with assert_no_placeholder_fields(gated_call_spy[0]["preview"]).
         TestListConnectors also covers its truncated prefix and "(no connectors)". TestEveryToolIsAudited: give the MagicMock
         client team_name "QA team", get_board → MiroBoard("uXjVOD6LSME=", "QA board", "", "QA team", "", "", ""),
         list_items → ([STICKY], False), get_item → STICKY, list_connectors → ([MiroConnectorLine("3458764549483493027",
         "3458764549483493025", "3458764549483493026", "curved", ["[QATEST]"])], False), where STICKY is
         MiroItem("3458764549483493025", "sticky_note", "<p>[QATEST]</p>", "", "", "", "square", "yellow", 0.0, 0.0, 199.0,
         228.0, "", ""); and arg_overrides {"miro_list_items": {"board_id": "uXjVOD6LSME="}, "miro_get_items":
         {"board_id": "uXjVOD6LSME=", "item_ids": "3458764549483493025"}, "miro_list_connectors": {"board_id":
         "uXjVOD6LSME="}} plus board_id for the three auto tools.
      5. ruff check . and python3 -m pytest tests/unit -q.
      Definition of done: the /dod live-check row is deferred to p11-qa-live and p12-docs-adrs-retire; dispatch no
      workflow in this phase (plan §3.0).
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_miro_connector.py -q passes
      - python3 -m pytest tests/unit/test_systemic_gate_invariants.py tests/unit/policy/test_registry.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n "| \[Miro\](#miro) | 6 | 3 | 3 | 0 |" docs/tools-reference.md matches
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p5-create
    title: miro_create_items and miro_create_connectors, popup-gated
    depends_on: [p4-reads]
    complexity: M
    touches:
      - src/privacyfence/connectors/miro.py
      - tests/unit/connectors/test_miro_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - tests/unit/policy/test_registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
    brief: |
      Read first: plan §3.3 (items and connectors formats, value rules, _item_body, validation messages, the two tools'
      gated_call arguments) and §3.4; src/privacyfence/connectors/grist.py _add_records (l.1022-1063) as the pattern.
      1. connectors/miro.py: STICKY_COLORS, FILL_COLORS, MIRO_SHAPES, the items and connectors parsers, _item_body, and the
         two tools (specs, call(), handlers). Order inside each: parse and validate (ValueError) → _board_info, then
         get_item for every distinct frame_id in items (the "not a frame" check) or every distinct connector end →
         gated_call(gate="popup", …) → the client write(s) through _fetch → return the §3.3 dict. Nothing is written if
         gated_call raises. miro_create_items creates the frames first (create_frame, one each, in order), then every other
         item in one create_items call, skipped when there is none (§3.3 "Order of a create"). A MiroClientError after k
         frames, or after k connectors, raises the §3.3 RuntimeError naming the ids already created.
      2. Tables (§3.4): TOOL_TO_GATE popup ×2, TOOL_TO_OPERATION, TOOL_TO_VERB Verb.ADD ×2 (and _VERB_MATRIX, size 105 → 107),
         write_effects.EFFECT_BY_TOOL with the two exact strings under a "# ── Miro ──" header, gate._TOOL_LAYOUT WIDE ×2.
      3. Regenerate docs/tools-reference.md and docs/always-allow-rules-reference.md; website card → 8 tools: 3 auto, 3 review,
         2 popup ("8 tools: 3 without a card · 3 reviewed · 2 need approval"), heading 164; how-it-works 164.
      4. Tests: TestCreateItems — one parametrized case per type building the exact body (color to the right style key,
         dueDate with T12:00:00.000Z, frame format/type and no type/parent keys in a frame's body, parent only with frame_id,
         empty groups left out); a mixed request calls create_frame for each frame first and then one create_items without
         the frames; a frames-only request makes no create_items call; a failure on the second frame raises naming the
         first and makes no bulk call; the card table rows (Position, Size with "-", Frame showing the frame's title,
         Color); a frame_id that is a sticky note or not found gives the "not a frame" message with no gate; a bool width,
         a shape color outside FILL_COLORS and a card color "#12345" give their messages; preview keys exactly Board, Team, Items; gate == "popup";
         filtered_data is None; every §3.3 validation message for items (spy stays empty for each); the client write runs only
         after the spy returns and not at all when gated_call raises (monkeypatch it to raise RuntimeError); the return
         dict. TestCreateConnectors — the body; From/To cells from the fetched items; a missing end, a frame end, the same item at
         both ends, an unknown key, a bad shape, a bad color and a 201-character caption give their messages with no gate; a failure on the second of three raises the RuntimeError naming the first id; the third is
         never attempted. Extend MIRO_SIBLINGS. TestEveryToolIsAudited: give the MagicMock client get_board →
         MiroBoard("uXjVOD6LSME=", "QA board", "", "QA team", "", "", ""), get_item → a sticky_note MiroItem with id
         "3458764549483493025", create_items → [that item], create_connector → "3458764549483493027", and arg_overrides
         {"miro_create_items": {"board_id": "uXjVOD6LSME=", "items": '[{"type": "sticky_note", "content": "a"}]'},
         "miro_create_connectors": {"board_id": "uXjVOD6LSME=", "connectors": '[{"start_item_id": "3458764549483493025",
         "end_item_id": "3458764549483493026"}]'}}; for the connector case get_item returns a sticky note with the id asked for.
      5. ruff check . and python3 -m pytest tests/unit -q.
      Definition of done: the /dod live-check row is deferred to p11-qa-live and p12-docs-adrs-retire; dispatch no
      workflow in this phase (plan §3.0).
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_miro_connector.py -q passes
      - python3 -m pytest tests/unit/test_write_effects.py tests/unit/policy/test_registry.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_connector_tool_annotations.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n "| \[Miro\](#miro) | 8 | 3 | 3 | 2 |" docs/tools-reference.md matches
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p6-update
    title: miro_update_items and miro_update_board, popup-gated
    depends_on: [p5-create]
    complexity: M
    touches:
      - src/privacyfence/connectors/miro.py
      - tests/unit/connectors/test_miro_connector.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - tests/unit/policy/test_registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
    brief: |
      Read first: plan §3.3 (changes format, update ordering, the two tools' gated_call arguments), §3.4 and §3.7;
      src/privacyfence/connectors/grist.py _update_records (l.1065-1127) as the pattern.
      1. connectors/miro.py: the changes parser (reusing the per-type key and value rules from p5, with frame_id "" refused
         with the §3.3 move-out message, frame_id checked to be a frame with get_item as in p5, and no frame_id for
         frames), and the two tools. miro_update_items: validate → _board_info → get_item per id (not
         found and non-changeable type messages) → build the Current/New rows → gated_call(gate="popup") → per change, in
         order, update_item with only the changed groups (data/style/position/geometry; a position change sends both x and y,
         taking the unchanged one from the current item; a geometry change for a sticky note sends only the one side given)
         then set_parent when frame_id is given → return. A MiroClientError after k items raises the §3.3 RuntimeError. A
         frame's change sends only title in data. details_text is the §3.3 JSON of what will be sent; the table is the
         summary.
         miro_update_board: validate (both empty, lengths) → _board_info → gate → update_board → return the new name and
         description.
      2. Tables (§3.4): TOOL_TO_GATE popup ×2, TOOL_TO_OPERATION, TOOL_TO_VERB Verb.UPDATE ×2 (and _VERB_MATRIX, size 107 → 109),
         EFFECT_BY_TOOL
         with the two exact strings, gate._TOOL_LAYOUT WIDE for miro_update_items only.
      3. Regenerate docs/tools-reference.md and docs/always-allow-rules-reference.md; website card → 10 tools: 3 auto, 3 review,
         4 popup ("10 tools: 3 without a card · 3 reviewed · 4 need approval"), heading 166; how-it-works 166.
      4. Tests: TestUpdateItems — Current/New rows for each field (frame "" shows "(none)"); details_text is the JSON of the
         exact bodies (a 300-character content with an <a href> appears in it whole); preview
         keys exactly Board, Team, Items, Fields changed; the request sequence per change (update_item body groups; set_parent
         with a frame id; a change of only frame_id makes no update_item call); validation messages (unknown key for
         the item's type, frame_id on a frame, frame_id with x, frame_id "" with the move-out message, a frame_id that
         is not a frame, duplicate ids, empty change, an image item) with the spy empty; a set_parent MiroClientError
         after the gate (Miro ignored the move) raises the §3.3 RuntimeError naming the items already changed; a frame's title change sends {"data": {"title": ...}} only; no write when
         gated_call raises; a failure on the second item raises naming the first. TestUpdateBoard — only given fields in the
         table and the client call; both empty and over-long messages. Extend MIRO_SIBLINGS and TestEveryToolIsAudited
         (update_board returns the MiroBoard; arg_overrides for both tools).
      5. Confirm no Miro tool is destructive (tests/unit/test_connector_tool_annotations.py unchanged and passing).
      6. ruff check . and python3 -m pytest tests/unit -q.
      Definition of done: the /dod live-check row is deferred to p11-qa-live and p12-docs-adrs-retire; dispatch no
      workflow in this phase (plan §3.0).
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_miro_connector.py -q passes
      - python3 -m pytest tests/unit/test_write_effects.py tests/unit/policy/test_registry.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_connector_tool_annotations.py tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -n "| \[Miro\](#miro) | 10 | 3 | 3 | 4 |" docs/tools-reference.md matches
      - python3 -m pytest tests/unit/connectors/test_miro_connector.py -q --cov=privacyfence.connectors.miro --cov-branch --cov-report=term-missing reports 100% for src/privacyfence/connectors/miro.py
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p7-policy-scope
    title: The miro.board auto-accept scope, offered on Settings, the bridge and check_policy suggestions but never by the popup
    depends_on: [p6-update]
    complexity: S
    touches:
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/value_format.py
      - src/privacyfence/policy/suggest.py
      - src/privacyfence/policy/propose.py
      - src/privacyfence/policy/registry.py
      - docs/always-allow-rules-reference.md
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_value_format.py
      - tests/unit/policy/test_suggest.py
      - tests/unit/policy/test_propose.py
      - tests/unit/policy/test_registry.py
      - tests/unit/policy/test_scope_rows.py
      - tests/unit/test_gate.py
    brief: |
      Read first: plan §3.4 "Rule scope", and how peek.page and grist.document are wired: policy/scopes.py:589-600 and
      683-738 (docstring list l.31-41), policy/catalogue.py:12-21 and 69-121, policy/value_format.py (whole; VALUE_FORMATS
      at l.72), policy/suggest.py:27-31 and 146-156, the policy/propose.py docstring (l.30-45),
      tests/unit/policy/test_catalogue.py (l.32-36, 97-105, 270-278), tests/unit/policy/test_scope_rows.py:227-262,
      tests/unit/policy/test_scopes.py:475, tests/unit/policy/test_value_format.py (the VALID/INVALID rows and l.168),
      tests/unit/policy/test_suggest.py:33-45, tests/unit/policy/test_propose.py:336-355,
      tests/unit/policy/test_registry.py:28-46, and the grist.document case in tests/unit/test_gate.py (l.1069-1083).
      1. scopes.py: _miro_board_matches in the _peek_page_matches shape (False for an empty rule value or an empty
         board_id; verbatim membership, no link rewriting) and the "miro.board" NEW_SCOPE_SELECTORS entry exactly as §3.4;
         name miro.board in the module docstring's list.
      2. catalogue.py: the "miro.board" PolicyExtraScope exactly as §3.4 (no verb_predicates); name it in the docstring's
         list of extra scopes.
      3. value_format.py: import miro_client.BOARD_ID_RE as _MIRO_BOARD_ID_RE next to the Grist, Peek and Assets imports,
         and add the "miro.board" VALUE_FORMATS entry exactly as §3.4.
      4. suggest.py: "miro.board": "board_id" in _EXTRA_ARGUMENT.
      5. propose.py docstring: a bullet that miro.list_items, get_items, list_connectors, create_items, create_connectors,
         update_items and update_board are governed by miro.board, a scope with no entry here. registry.py docstring, where it
         lists what policy/catalogue.py offers: add miro.board. No PROPOSABLE_SCOPES entry.
      6. python3 scripts/generate_always_allow_reference.py.
      7. Tests:
         - test_scopes.py: miro.board matches the board_id from args and not another one; an empty value and an empty
           board_id never match; a rule value written as https://miro.com/app/board/uXjVOD6LSME=/ does not match board_id
           "uXjVOD6LSME=".
         - test_catalogue.py: the entry exists with verbs read, add, update; compiling ["uXjVOD6LSME="] with
           [Verb.READ, Verb.ADD] gives the three miro read operations and the two add operations with predicate miro.board;
           an empty value is rejected like grist.document's; a board link is rejected with the value_format message.
         - test_value_format.py: VALID "uXjVOD6LSME=" and "o9J_kzZb3Xg=" for miro.board; INVALID the board link, a name with a
           space, and "../x". test_client_regexes_are_reused (l.109) also asserts VALUE_FORMATS["miro.board"].pattern is
           miro_client.BOARD_ID_RE.
         - test_suggest.py: test_miro_board_is_named_by_its_board_id next to the Grist and Peek cases (an uncovered
           miro_list_items call suggests a miro.board rule with value "uXjVOD6LSME=").
         - test_scope_rows.py: a miro.board row next to the grist.document cases (same shape).
         - test_gate.py: next to the grist.document case, a stored miro.board rule auto-accepts miro_list_items for that board
           and not for another.
         - test_propose.py: add the seven gated Miro tools with make_ctx(connector="miro", args={"board_id": "uXjVOD6LSME="})
           to test_the_extra_scope_operations_stay_unproposed.
         - test_registry.py: add the seven miro.* operation keys to _GRANT_MANIFEST_UNREACHABLE_OPERATIONS and update its
           comment's count ("thirteen" → "twenty") and list of extra scopes.
      8. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: if propose.proposals_for returns a proposal for a Miro tool without a PROPOSABLE_SCOPES entry, stop with
      status=blocked (plan §6).
    acceptance:
      - python3 -m pytest tests/unit/policy -q passes
      - python3 -m pytest tests/unit/test_gate.py tests/unit/test_generate_always_allow_reference.py tests/unit/test_daemon_main.py tests/unit/web/test_mcp_dispatch.py -q passes
      - grep -n '"miro.board"' src/privacyfence/policy/scopes.py src/privacyfence/policy/catalogue.py src/privacyfence/policy/value_format.py src/privacyfence/policy/suggest.py matches in all four files
      - grep -n '_scope("miro' src/privacyfence/policy/propose.py finds nothing
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p8-local-daemon-oauth
    title: Build the connector in the daemon, and the OAuth sign-in from Settings, the CLI and the QA authenticate script
    depends_on: [p3-connector-listing]
    complexity: M
    touches:
      - src/privacyfence/daemon_main.py
      - src/privacyfence/org_mode.py
      - src/privacyfence/settings_controller.py
      - scripts/qa_authenticate_connectors.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_settings_controller.py
      - tests/unit/test_qa_authenticate_connectors.py
    brief: |
      Read first: plan §3.5 and §3.2.1; the wiring this copies: daemon_main.py TOKEN_FILES (l.194-207), the Salesforce and
      Grist blocks of build_connectors (l.1533-1552, 1699-1716), run_grist_oauth (l.1892-1904), the --grist-oauth flag
      (l.2334), the oauth_flag chain (l.2461-2467) and dispatch (l.2497-2498); org_mode.py CONNECTOR_ORG_SECTION and
      connector_enabled_by_org (l.515-534, ADR 0196); settings_controller.py ALL_CONNECTORS (l.104-107),
      ORG_CONFIG_SERVICE and ORG_BUNDLE_SERVICES (l.126-130), authenticate_connector and _org_allows (l.1284-1316),
      _authenticate_salesforce (l.1370-1396), _connectors_state (l.2155-2194); tests/unit/test_settings_controller.py
      ORG_CONFIG_ENABLING_EVERY_CONNECTOR (l.64-68); scripts/qa_authenticate_connectors.py STEPS (l.74-84).
      1. daemon_main.py: TOKEN_FILES["miro"]; imports (MiroClient, MiroClientError, MiroConnector, MiroTokenProvider and
         miro_auth's bundle_settings, load_token_file and authorize_interactive under the §3.5 names); the build_connectors
         block exactly as §3.5; run_miro_oauth and --miro-oauth with the exact messages and exit codes.
      2. org_mode.py: "miro": "miro" in CONNECTOR_ORG_SECTION (and Miro in the comment above it, as a connector whose
         section carries the app's client id and secret). settings_controller.py: "miro" in ALL_CONNECTORS and
         ORG_BUNDLE_SERVICES, the authenticate_connector branch and _authenticate_miro with the §3.5 messages.
      3. scripts/qa_authenticate_connectors.py: the Miro OAuthStep after Salesforce and the docstring's group list.
      4. Tests: test_daemon_main.py — with a miro bundle section and a valid token file the connector is built
         (MiroClient.check_connection monkeypatched); no section → failures["miro"] == "no_org_config"; no token file →
         "not_authenticated"; an incomplete section → its message; disabled → no entry; run_miro_oauth success, missing section
         and failure (authorize_interactive monkeypatched) with exit codes and messages; --miro-oauth dispatches to it.
         test_settings_controller.py — add "miro": {"client_id": "m", "client_secret": "s"} to
         ORG_CONFIG_ENABLING_EVERY_CONNECTOR; authenticate_connector("miro") without a section sets "Miro isn't enabled in
         your organization config." and never calls authorize_interactive; _authenticate_miro called directly without a
         section sets "Miro organization config isn't installed yet."; with one it runs miro_auth.authorize_interactive (monkeypatched; drive _run_async synchronously the
         way the Grist tests do) against data_dir()/credentials/miro_token.json and refreshes connectors; a failure sets
         "Miro authentication failed: …"; the miro row's has_org follows the bundle; an incomplete section sets "Miro
         organization config is not usable: …". test_qa_authenticate_connectors.py — the miro step runs --miro-oauth and --only miro selects it; update any
         pinned step list or count.
      5. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: plan §6 "Tests that enumerate connectors".
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py tests/unit/test_qa_authenticate_connectors.py -q passes
      - 'grep -n "\"miro\": \"credentials/miro_token.json\"" src/privacyfence/daemon_main.py matches'
      - grep -n "\-\-miro-oauth" src/privacyfence/daemon_main.py scripts/qa_authenticate_connectors.py matches in both files
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p9-org-oauth
    title: Org bundle miro section and the org-mode OAuth sign-in on /connect
    depends_on: [p8-local-daemon-oauth]
    complexity: M
    touches:
      - scripts/build_org_bundle.py
      - src/privacyfence/web/routes_connect.py
      - docs/configuration-reference.md
      - tests/unit/test_build_org_bundle.py
      - tests/unit/web/test_routes_connect.py
    brief: |
      Read first: plan §3.6 and §3.2.1; web/routes_connect.py (the Salesforce and Grist branches at l.95-189, 333-436,
      456-520, 1015-1030); scripts/build_org_bundle.py's Salesforce option group (l.262-268) and section writing
      (l.659-666), the services list (l.898), _CONNECTOR_CALLBACKS (l.66-72) and the org-mode summary (l.948-963);
      tests/unit/web/test_routes_connect.py _ORG_CONFIG (l.31-37) and the row count (l.222-224).
      1. scripts/build_org_bundle.py: the Miro argument group, validation, section writing, the services list and
         _CONNECTOR_CALLBACKS["miro"] = ("miro",) exactly as §3.6.
      2. docs/configuration-reference.md build options table, after the Grist rows:
         "| `--miro-client-id`, `--miro-client-secret` | none | `miro.client_id`, `miro.client_secret` | The Miro app people sign in with. Give both or neither. |"
         (tests/unit/test_docs_configuration_reference.py requires every option documented).
      3. web/routes_connect.py: every bullet of §3.6 for routes_connect.
      4. Tests: test_build_org_bundle.py — both options write {"miro": {"client_id", "client_secret"}}; one without the other
         exits with the exact message; a Miro-only bundle is written; an org-mode build including Miro prints
         {issuer}/oauth/callback/miro. web/test_routes_connect.py — the Miro row is not configured without a section and shows
         Connect with one; /oauth/start/miro redirects to https://miro.com/oauth/authorize with client_id, redirect_uri
         https://<server>/oauth/callback/miro, state and no scope; /oauth/callback/miro exchanges the code
         (miro_auth.exchange_code monkeypatched) and saves user_dir(principal)/credentials/miro_token.json; an exchange
         MiroClientError takes the sign-in-failed path. Add "miro": {"client_id": "m", "client_secret": "s"} to _ORG_CONFIG
         (l.31-37); the service-row count assertion (l.224) then becomes 12 and its comment "Eleven OAuth services and
         Telegram …". Any other test in the file that counts rows from _ORG_CONFIG changes the same way.
      5. ruff check . and python3 -m pytest tests/unit -q.
    acceptance:
      - python3 -m pytest tests/unit/test_build_org_bundle.py tests/unit/web/test_routes_connect.py tests/unit/test_docs_configuration_reference.py tests/unit/web/test_server.py -q passes
      - grep -n '"miro"' src/privacyfence/web/routes_connect.py scripts/build_org_bundle.py matches in both files
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p10-qa-recorder
    title: The Miro live check, fixture replay tests and lifecycle in the QA recorder, tested against a fake client, and the QA docs
    depends_on: [p6-update, p8-local-daemon-oauth]
    complexity: M
    touches:
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/unit/test_miro_client.py
      - tests/fixtures/qa_environment.yaml.example
      - docs/connector-qa.md
      - docs/testing-policy.md
    brief: |
      Read first: docs/connector-qa.md (whole), docs/testing-policy.md "Layer 5", scripts/qa_fixture_recorder.py
      _build_grist_client (l.1277-1283), check_grist (l.1300-1365), lifecycle_grist (l.2725-2775), RawCapture (l.664-699),
      CONNECTOR_CHECKS/EXPECTED_FIXTURES (l.2250-2304), LIFECYCLE_CHECKS (l.2994-3002) and the module docstring's
      lifecycle list (l.32-33); tests/unit/test_qa_fixture_recorder.py l.2504-2580 (fixture presence, replay, identifier and
      undocumented-file tests); tests/unit/test_grist_client.py TestLiveFixtureParsing (l.655+); plan §5 mb3 and §6 "Taking
      an item out of a frame". This phase dispatches nothing; p11-qa-live records on the runner.
      1. scripts/qa_fixture_recorder.py:
         - _build_miro_client(): config = miro_auth.bundle_settings(daemon_main.load_org_config().get("miro") or {}); path =
           daemon_main._resolve_path(daemon_main.TOKEN_FILES["miro"]); provider = MiroTokenProvider(config, path,
           miro_auth.load_token_file(path)); client = MiroClient(provider); client.check_connection(); return client.
         - check_miro(record, manifest): cfg = manifest.get("miro") or {}; board_id required (missing → a failed CheckResult
           "miro.board_id missing from qa_environment.yaml"). Four CheckResults recorded through RawCapture:
           get_board ("get_board.json": ok when the name contains [QATEST]); list_items ("list_items.json": ok only when
           every sticky_note's content contains [QATEST] and at least one frame is listed; it refuses to record otherwise);
           list_connectors ("list_connectors.json": ok when at least one is listed); list_tags ("list_tags.json": ok when a
           tag titled QATEST exists). Before writing each fixture, pass it through the recorder's existing redact and
           deidentify_structural_fields, and replace every createdBy/modifiedBy/owner object's name and id with the
           recorder's placeholders, so test_committed_fixtures_carry_no_unredacted_identifiers passes on what is recorded.
         - lifecycle_miro(manifest): on cfg["lifecycle_board_id"] only, never board_id. Find the frame titled
           "QaLifecycleFrame [QATEST]" via list_items(item_type="frame"). Then create_items one sticky note with content
           f"{LIFECYCLE_TAG} miro sticky {suffix}" and parent that frame, plus one shape with content
           f"{LIFECYCLE_TAG} miro shape {suffix}" and style fillColor "#fff9b1"; create_frame one frame titled
           f"{LIFECYCLE_TAG} miro frame {suffix}"; get_item the sticky note; update_item its content to
           f"{LIFECYCLE_TAG} miro sticky {suffix} updated"; get_item again and check the content; set_parent(board,
           shape id, the new frame's id) to move the shape into the new frame, which set_parent itself confirms by
           reading it back. Never a move out of a frame (plan §6). No delete: the client has none (plan
           §3.7). The docstring says the items accumulate and are cleaned by hand. Return LifecycleResult("miro", ok, note,
           cleanup_ok=None); ok covers every step, and note names the first step that failed. Manual step mb3 proved the
           frame create, the bulk create with a parent and the fill color by hand; the move into a frame through
           set_parent is first checked here and in mb3's follow-up probe.
         - Register "miro" in CONNECTOR_CHECKS, EXPECTED_FIXTURES ("get_board.json", "list_items.json",
           "list_connectors.json", "list_tags.json") and LIFECYCLE_CHECKS, and name Miro in the module docstring's list of
           lifecycle connectors.
      2. tests/unit/test_miro_client.py: TestLiveFixtureParsing, replaying the four fixtures through the real parsers via a
         faked _request with a _load(name) helper that skips with the record hint when a file is missing, exactly like the
         Grist one. It skips until p11-qa-live records the files, and it satisfies
         test_every_expected_fixture_is_replayed_by_a_unit_test now.
      3. tests/fixtures/qa_environment.yaml.example: a miro section (board_id: "", lifecycle_board_id: "") with comments in
         the file's style.
      4. docs/connector-qa.md:
         - a Miro row in the QA accounts table: a free Miro account holding nothing real, with a Developer team and the QA
           app; the runner's token file was obtained by hand once with the form-body exchange, is refreshed by the runner,
           and is rotated with qa_authenticate_connectors.py --only miro;
         - miro in the "--only" options list (l.388);
         - "### Seed: Miro" next to "### Seed: Jira Assets" (l.308): the two boards from manual step mb1, kept under 50
           items, and the optional miro.board rule row;
         - the Manifest reference row (l.349);
         - Miro in the --lifecycle sentence (l.416), noting that the lifecycle runs on main's code only;
         - "### Miro checks" in the exploratory section: sign-in from Settings, the review cards for the three reads, the
           popup cards for the four writes with their tables, a batch of 20, a rule from Settings auto-accepting a read, a
           board link refused as a board_id and as a rule value, and a board in another team answering 403.
         docs/testing-policy.md: add Miro where it lists the connectors --lifecycle covers (l.233-234).
      5. tests/unit/test_qa_fixture_recorder.py, against a fake client following the existing per-connector tests there:
         check_miro (the four results; the missing board_id; the non-[QATEST] refusal; the owner and createdBy scrub), and
         lifecycle_miro (all steps ok; a set_parent MiroClientError makes ok False and names the step; it never
         touches board_id and never calls set_parent with ""). TestFixturePresence.test_every_expected_fixture_file_exists_and_is_nonempty_valid_json
         (l.2504-2510) fails for miro until the files are recorded. Change its parametrize list to
         [pytest.param(c, marks=pytest.mark.xfail(strict=True, reason="Miro fixtures are recorded on the QA runner"))
         if c == "miro" else c for c in sorted(recorder.EXPECTED_FIXTURES)]; p11-qa-live puts the plain list back. No
         phase id goes in the reason (tests/unit/test_code_no_history.py).
      6. ruff check . and python3 -m pytest tests/unit -q.
      Stop condition: if adding "miro" to EXPECTED_FIXTURES makes a test other than TestFixturePresence fail for want of
      files, stop with status=blocked and name it.
    acceptance:
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py tests/unit/test_miro_client.py -q passes
      - python3 -c "import sys; sys.path.insert(0,'scripts'); import qa_fixture_recorder as q; assert 'miro' in q.CONNECTOR_CHECKS and 'miro' in q.LIFECYCLE_CHECKS and 'miro' in q.EXPECTED_FIXTURES" exits 0
      - grep -n "### Seed: Miro" docs/connector-qa.md matches
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p11-qa-live
    title: Record the Miro fixtures on the QA runner, then apply what they show
    depends_on: [p10-qa-recorder]
    complexity: M
    touches:
      - tests/fixtures/live/miro/**
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/unit/test_miro_client.py
      - src/privacyfence/miro_client.py
      - tests/unit/connectors/test_miro_connector.py
    brief: |
      Read first: the .claude/skills/steward/SKILL.md notes on qa-record-fixture.yml, the ci.dispatchable entries in
      .claude/toolkit.yaml, .github/workflows/qa-record-fixture.yml's header, docs/connector-qa.md "Recording one connector"
      and "Reviewing recorded fixtures", plan §2 "QA" and §6 ("Response shapes", "The runner credential").
      1. Push this phase branch. Dispatch .github/workflows/qa-record-fixture.yml with the GitHub MCP actions_run_trigger,
         ref main (never the phase branch: the runner admits only main's workflow), inputs connector=miro and branch=<this
         phase branch>. A queued run is waiting on the connector-live-check concurrency group: wait, and never re-dispatch.
         When it succeeds, its commit-fixture job has pushed to this branch: git pull.
      2. Review every file under tests/fixtures/live/miro/. None may hold a real name or e-mail, a team name other than the
         QA one, a token, a UUID, or content without [QATEST]. If one does, extend the scrub in check_miro (and its unit
         test), push, and dispatch again the same way.
      3. tests/unit/test_miro_client.py: TestLiveFixtureParsing (added in p10-qa-recorder) now runs. If a fixture shows a
         shape a parser mishandles, fix miro_client.py and its unit test (plan §6). Remove the xfail marker p10-qa-recorder
         put on TestFixturePresence (restore the plain sorted(recorder.EXPECTED_FIXTURES) parametrize).
      4. ruff check . and python3 -m pytest tests/unit -q.
      Do not dispatch connector-live-check.yml: it checks main's code, not this branch's (lifecycle_miro first runs after
      the merge, manual step ma3).
      Stop condition: plan §6 "The runner credential" and "Response shapes".
    acceptance:
      - ls tests/fixtures/live/miro/ lists get_board.json, list_items.json, list_connectors.json and list_tags.json
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py tests/unit/test_miro_client.py tests/unit/connectors/test_miro_connector.py -q passes, with TestLiveFixtureParsing not skipped
      - grep -n "Miro fixtures are recorded on the QA runner" tests/unit/test_qa_fixture_recorder.py finds nothing
      - The qa-record-fixture.yml run (ref main, connector=miro, branch=this phase branch) concluded success (URL in the final report)
      - python3 -m pytest tests/unit/connectors/test_miro_connector.py tests/unit/test_miro_client.py -q --cov=privacyfence.connectors.miro --cov=privacyfence.miro_client --cov-branch --cov-report=term-missing reports 100% for both files
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p12-docs-adrs-retire
    title: The full setup guide, reference docs, changelog, the four ADRs, the live check, and retiring the plan
    depends_on: [p7-policy-scope, p9-org-oauth, p11-qa-live]
    complexity: M
    touches:
      - docs/miro-setup*.md
      - docs/configuration-reference.md
      - docs/README.md
      - docs/approvals-and-policy.md
      - docs/connecting-a-service.md
      - docs/org-mode-setup-guide.md
      - CHANGELOG.md
      - docs/adr/0230-miro-connects-only-by-oauth-with-an-expiring-token-app-in-the-bundle.md
      - docs/adr/0231-the-miro-connector-reads-adds-and-changes-and-never-deletes.md
      - docs/adr/0232-miro-rules-are-per-board-and-set-from-settings-not-the-card.md
      - docs/adr/0233-a-miro-write-covers-a-batch-of-up-to-twenty-under-one-card.md
      - docs/adr/README.md
      - docs/miro-connector-plan.md
      - docs/miro-connector-plan-manual-steps.html
    brief: |
      Read first: docs/adr/README.md (template and rules), plan §3 and §4, docs/configuration-reference.md (l.125-135,
      248-300, 345-366), docs/connecting-a-service.md (l.1-10, 72-78, 135-141, 177-191), docs/org-mode-setup-guide.md
      (l.35-50, 221-240, 325-340), docs/approvals-and-policy.md (l.336-342, 395-421), CHANGELOG.md's "## [Unreleased]"
      section, .claude/toolkit.yaml ci.dispatchable.
      0. If main already holds ADRs numbered 0230-0233, take the next free numbers and use them everywhere below.
      1. The setup guide miro-setup.md in docs/: replace the skeleton with the full content of every plan §3.8 section,
         taking each statement from the code as it now is: the error texts from miro_auth.py and miro_client.py, the option
         names in build_org_bundle.py, the port and callback paths, the ten tools and their gates from
         docs/tools-reference.md, and that an item cannot be taken out of a frame (Miro answers 200 and ignores it;
         plan §3.1 and §6).
      2. docs/configuration-reference.md: miro in the connectors.<name>.enabled list (l.131); "[Miro setup](miro-setup.md)" in
         the guide sentence (l.252-256); --miro-oauth in the command-line options table after --grist-oauth (l.363); the
         redirect-URI sentence (l.348) gains Miro. docs/approvals-and-policy.md: after the "Grist document" row of the scope
         table (l.419) add "| Miro board | identity | board ids | `miro.board` | the board is one of these |", and name Miro
         boards (scope "Miro — board") in the "only from Settings or a proposal" paragraph (l.337-342), with the reason: the
         card proposes rules only from a fixed set of scopes. docs/connecting-a-service.md: Miro in the connector list
         (l.3-4), the row "| Miro | 53686 |" after Grist's in the loopback-port table (l.78), Miro in the "Organization config
         missing" row's provider list (l.140), and "| Miro | [miro-setup.md](miro-setup.md) |" in the Provider guides table
         (after l.189). docs/org-mode-setup-guide.md: Miro in the connector apps row (l.39) and the /connect list (l.47); the
         row "| Miro | `https://pf.acme.example.com/oauth/callback/miro` | `--miro-client-id`, `--miro-client-secret` | [Miro setup](miro-setup.md) |"
         after Grist's in the per-connector table (l.238); the two options after the Grist rows of its build-options table
         (l.336-338), matching configuration-reference.md's row.
      3. CHANGELOG.md under "## [Unreleased]" (never a version heading), in its Added section (create "### Added" under
         Unreleased if missing, in the order the file's other releases use): "Miro connector: list boards and tags, read a
         board's items and connector lines after review, and add or change sticky notes, cards, texts, shapes, frames and
         connector lines with approval, up to 20 at a time. Sign in with Authenticate… in Settings or on the connections page,
         with the Miro app your organization registered. Nothing is deleted."
      4. Write ADRs 0230–0233 from plan §4 at the paths in touches, with the docs/adr/README.md template (Status "Accepted —
         <today's date>. Implemented.", Context, Decision, Alternatives considered, Consequences, Verification naming the
         tests that enforce each, Related). Link source files and ADRs 0019, 0077, 0089, 0101, 0105, 0115, 0116, 0142, 0144,
         0145, 0196, 0204 and 0228 where relevant; never link the plan. Add the four rows to the index in docs/adr/README.md.
      5. git rm docs/miro-connector-plan.md docs/miro-connector-plan-manual-steps.html;
         grep -rn "miro-connector-plan" . --exclude-dir=.git must find nothing.
      6. python3 scripts/generate_tools_reference.py and python3 scripts/generate_always_allow_reference.py leave no diff;
         ruff check . and python3 -m pytest tests/unit -q pass.
      7. Push this phase branch and dispatch .github/workflows/qa-record-fixture.yml with the GitHub MCP actions_run_trigger,
         ref main, inputs connector=miro and branch=<this phase branch>: it runs check_miro against code that carries every
         phase, so it is the definition-of-done live check for src/privacyfence/*_client.py and connectors/**. Never
         dispatch connector-live-check.yml for this: it checks main's code. A queued run waits on the connector-live-check
         concurrency group: wait, never re-dispatch. When it succeeds, git pull; review the re-recorded fixtures as
         p11-qa-live did, and keep them only if the unit tests still pass. Put the run URL in your final report.
    acceptance:
      - ls docs/adr/0230-*.md docs/adr/0231-*.md docs/adr/0232-*.md docs/adr/0233-*.md lists four files, each containing "Accepted" (or the renumbered ones)
      - grep -ci "02[3-9][0-9]-.*miro" docs/adr/README.md prints at least 4
      - test ! -e docs/miro-connector-plan.md && test ! -e docs/miro-connector-plan-manual-steps.html
      - grep -rn "miro-connector-plan" . --exclude-dir=.git finds nothing
      - grep -n "Miro connector" CHANGELOG.md matches a line below "## [Unreleased]" and above the next "## [" heading
      - The qa-record-fixture.yml run (ref main, connector=miro, branch=this phase branch) concluded success (URL in the final report)
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
```
