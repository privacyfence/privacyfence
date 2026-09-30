# Web pages: publish a single-file HTML page and get its link

## Goal

People make single-file HTML pages with Claude (claude.ai artifacts, reports Claude Code writes)
and want to share them inside the organization as a plain link. With this change, an AI client
connected to PrivacyFence can publish such a page to a web server the organization (or, on a
desktop install, the person) runs, and PrivacyFence answers with the page's URL. The page is copied
over SFTP, after the same approval card every write gets. Anyone with the link can open the page. A
person can later replace a page at the same link, list the pages they published, and take one
down.

It works in both install modes: an organization server reads the SFTP target from its signed
bundle, a desktop install from **Settings → Connectors → Web pages**. The web server can be the
PrivacyFence host itself or any other Ubuntu machine; PrivacyFence never serves the pages itself.

There is no GitHub issue; this plan came from the maintainer's request in a `/make-plan` session.

## Current state

- **Connectors.** A connector is a `Connector` subclass (`src/privacyfence/connector.py:78`, 96
  lines) with `name`, `tool_specs()` and `async call()`. `connector_catalog.catalog_tools()`
  builds every class as `cls(None).tool_specs()` (`src/privacyfence/connector_catalog.py:36-39`),
  so a constructor takes one positional client and `tool_specs()` never touches it. The smallest
  write-capable connector, and the template for one outside the OAuth world, is
  `src/privacyfence/connectors/telegram.py` (364 lines): its write `_send_message`
  (`telegram.py:319-339`) builds a metadata-only `preview`, calls
  `gated_call(gate="popup", ...)` and only then writes; `_auto_audit` (`telegram.py:345`) audits the
  auto-approved tools.
- **Gate.** `gate.gated_call` (`src/privacyfence/gate.py:799`) runs the real PII scan with a forced
  second confirmation on a popup write only when `upload_pii_scan_text` is set, which today only
  `drive_upload_file` does (`gate.py:109-139` module docstring, `:876-882`, `:955-960`). A pending
  approval raises `approvals.ApprovalPending`, so code after `await gated_call(...)` runs only once
  approved, on the re-issued identical call (ADR 0093); the decision ledger keys on the `args=`
  passed in, so every content-determining argument goes there (`drive.py:1612-1621`).
- **Uploads.** `drive_upload_file` (`src/privacyfence/connectors/drive.py:1457-1642`) is the
  pattern for `upload_id`: `local_files.require_local_files(["upload:" + upload_id], ...)` before
  the gate, `local_files.read_local_file(...)` for the bytes, `local_files.commit_uploads()` right
  after `gated_call` returns (ADR 0102). Slots come from `privacyfence_create_upload_slot`
  (ADR 0028), up to 50,000,000 bytes.
- **HTML text.** `text_extraction.extract_text(data, "text/html")` already turns HTML into
  Markdown-ish text for the PII scan and the card (`src/privacyfence/text_extraction.py:124`), and
  `text_extraction.preview_blocks_for(details, extracted)` (`:158`) builds the WIDE card body.
- **Errors reaching the agent.** `safe_errors.public_message` (`src/privacyfence/safe_errors.py`)
  replaces a bare `RuntimeError`'s text with a generic message and passes a named `RuntimeError`
  subclass through (e.g. `google_errors.GoogleResourceUnavailableError`).
- **Config.** Local mode reads each principal's `settings.yaml` as a plain dict
  (`daemon_main.load_config`, `src/privacyfence/daemon_main.py:360`); org mode reads the signed
  `org_config.json` (`daemon_main.load_org_config`, `:374`, ADR 0016), whose sections have
  dataclasses with `from_org_config` in `src/privacyfence/org_mode.py` (e.g.
  `DownloadDeliveryConfig`, `:201-349`) that raise `org_mode.ConfigurationError`, and whose flags
  come from the stdlib-only `scripts/build_org_bundle.py` (`--downloads-*` group at `:350-379`,
  merged at `:607-623`).
- **Building connectors.** `daemon_main.build_connectors` (`daemon_main.py:1248-1538`) has one
  `if enabled(name): try ... except` block per connector; Telegram's (`:1503-1536`) is the
  non-OAuth template. Failures are classified by `_classify_connector_failure` (`:1213-1245`):
  "Use Authenticate…" → `not_authenticated`, "organization config not installed" →
  `no_org_config`. Org mode builds connectors per principal inside `principal_scope`
  (`connector_registry.py`, `daemon_main._start_org_web_server` `:1104-1120`), so `user_dir()` and
  `_resolve_path()` resolve per person.
- **Server-held keys.** Org mode already generates and keeps a server key beside the bundle:
  `web_push.load_or_create_vapid_key(org_dir() / VAPID_KEY_FILE_NAME)`
  (`src/privacyfence/web_push.py:128-143`, called from `daemon_main._start_web_push`).
- **Settings (local).** Connector rows come from `settings_controller.ALL_CONNECTORS`
  (`src/privacyfence/settings_controller.py:89-92`) through `_connectors_state()` (`:1734-1767`,
  whose `has_org` line at `:1742-1745` raises `KeyError` for a name it does not know), rendered by
  `settings_window_html.renderConnectors()` (`src/privacyfence/settings_window_html.py:706-746`);
  Telegram's row opens a client-side modal (`data-telegram-auth`, `renderTelegramModal` ~`:1185`).
  A settings write is a controller method dispatched through `POST /api/settings/{action}`
  (ADR 0032), registered in `web/org_settings_scope.py`'s `ACTION_SCOPES` (`:65-151`) and in
  exactly one of `web/routes_settings.py`'s `_SENSITIVE_ACTIONS`/`_NON_SENSITIVE_ACTIONS`
  (`:223-263`). `toggle_gmail_signature` (`settings_controller.py:1585-1591`) is the
  load → change → `_save_config` → `refresh_connectors` → `snapshot` shape to copy. Org mode shows
  no connector section at all (`settings_window_html._capabilities_for`, `:1516-1565`).
- **No SSH anywhere.** Nothing in `src/`, `scripts/` or the lock files uses an SSH library or runs
  `scp`/`sftp`.
- **Numbers.** 11 connectors, 114 connector tools in `docs/tools-reference.md`'s summary table;
  `website/connectors/index.html` has one card per connector and the text "Eleven connectors"
  (`tests/unit/test_website_connectors_page.py:56-60`). `TOKEN_WRITE_SITES` has 10 entries
  (`tests/unit/test_systemic_gate_invariants.py:177-188`, asserted at `:253`). The destructive-tool
  set is pinned to two tools (`tests/unit/test_connector_tool_annotations.py:29`). The next free
  ADR number is 0116.

## Design

### D1. Shape

A new connector named **`pages`**, label **"Web pages"**, with three tools:

| Tool | Gate | `read_only` | `destructive` | Operation key | Verb |
|---|---|---|---|---|---|
| `pages_publish_html` | `popup` | False | False | `pages.publish_page` | `Verb.SHARE` |
| `pages_list_pages` | `auto` | True | False | none | none |
| `pages_unpublish_page` | `popup` | False | **True** | `pages.unpublish_page` | `Verb.DELETE` |

`pages_unpublish_page` deletes something that cannot be restored, so it joins the pinned
destructive set (ADR 0086's rule for a new deleting tool). Republishing with a `page_id` replaces a
page but keeps it, so `pages_publish_html` is not destructive.

New modules:

- `src/privacyfence/pages_client.py`: configuration, key handling and the SFTP client (D2-D5).
- `src/privacyfence/pages_index.py`: each person's record of the pages they published (D6).
- `src/privacyfence/connectors/pages.py`: `PagesConnector` (D7).

New runtime dependency: `asyncssh>=2.24,<3.0` in `pyproject.toml`'s `dependencies`, with a comment
in the file's existing style saying why (SFTP for the Web pages connector, ADR 0117; pure Python on
top of `cryptography`, which is already a dependency; licensed EPL-2.0 or GPL-2.0-or-later).
`requirements/*.lock.txt` are regenerated with `scripts/update_dependency_locks.sh`, and `"asyncssh"`
is added to `scripts/pyinstaller_common.py`'s `HIDDEN_IMPORTS` beside `"telethon"`, with the comment
`# asyncssh (Web pages connector's SFTP client)`.

### D2. Configuration: `PagesConfig`

The same keys in both places: org mode reads `org_config.json`'s `"pages"` object, local mode reads
`settings.yaml`'s `pages:` mapping. In `pages_client.py`:

```python
class PagesConfigError(ValueError): ...

@dataclass(frozen=True)
class PagesConfig:
    sftp_host: str
    sftp_user: str
    sftp_host_key: str          # one OpenSSH public key line, as given
    remote_dir: str             # posixpath.normpath'd
    public_base_url: str        # no trailing slash
    sftp_port: int = 22
    max_bytes: int = DEFAULT_MAX_BYTES   # 16_000_000

    @staticmethod
    def from_mapping(raw: object, where: str) -> "PagesConfig": ...
```

`from_mapping` checks, in this order, and raises `PagesConfigError(f"{where}: {message}")` with
these messages (`where` is `org_config.json's "pages" section` or `settings.yaml's pages section`):

| Field | Rule | Message |
|---|---|---|
| (whole) | a `dict` | `must be a mapping of settings` |
| `sftp_host` | non-empty `str`, no whitespace, no `@`, `/` or `:` | `sftp_host must be the server's host name or IP address, for example pages.example.com` |
| `sftp_port` | `int` (not `bool`), 1-65535; absent → 22 | `sftp_port must be a whole number from 1 to 65535` |
| `sftp_user` | matches `^[A-Za-z0-9_.][A-Za-z0-9_.-]{0,31}$` | `sftp_user must be the account name PrivacyFence signs in as on the server, for example pf-pages` |
| `sftp_host_key` | `asyncssh.import_public_key(value.strip())` succeeds | `sftp_host_key must be one line of the server's public host key, for example the contents of /etc/ssh/ssh_host_ed25519_key.pub on the server` |
| `remote_dir` | non-empty `str`, no NUL, no `..` path segment, normalizes to something other than `/` and `.` | `remote_dir must be the folder on the server the web server publishes, for example /srv/pages` |
| `public_base_url` | `urllib.parse.urlsplit`: scheme `https`, a host, no query or fragment | `public_base_url must be the https:// address at which the web server publishes remote_dir, for example https://pages.example.com` |
| `max_bytes` | `int` (not `bool`), 1-50,000,000; absent → 16,000,000 | `max_bytes must be a whole number of bytes from 1 to 50000000` |

Unknown keys are ignored. `public_base_url` is stored with trailing `/` removed; `remote_dir` with
`posixpath.normpath` applied. Constants: `DEFAULT_MAX_BYTES = 16_000_000` (the size limit of a
claude.ai artifact), `MAX_MAX_BYTES = 50_000_000` (an upload slot's size).

### D3. PrivacyFence's SSH key

PrivacyFence generates its own Ed25519 key; nobody hands it one. In `pages_client.py`:

```python
KEY_COMMENT = "privacyfence-pages"
ORG_KEY_FILE_NAME = "pages_ssh_key"                    # in org_dir()
LOCAL_KEY_RELATIVE_PATH = "credentials/pages_ssh_key"  # through daemon_main._resolve_path

def load_or_create_ssh_key(path: Path) -> asyncssh.SSHKey: ...
def public_key_line(key: asyncssh.SSHKey) -> str: ...
```

`load_or_create_ssh_key` copies `web_push.load_or_create_vapid_key`: if `path` exists, import it
with `asyncssh.import_private_key(path.read_text(encoding="utf-8"))` and raise
`ValueError(f"{path} is not an Ed25519 private key; move it aside to generate a new one")` unless
its `get_algorithm()` is `"ssh-ed25519"`; otherwise
`asyncssh.generate_private_key("ssh-ed25519", comment=KEY_COMMENT)`, write
`key.export_private_key().decode("ascii")` with `secure_files.atomic_write_text(path, text)`
(default 0600), log `logger.info("Generated the Web pages SSH key at %s", path)`, return it.
`public_key_line` returns `key.export_public_key().decode("ascii").strip()`, which is
`ssh-ed25519 AAAA… privacyfence-pages`.

- **Org mode:** one key per server, at `org_dir() / ORG_KEY_FILE_NAME`, beside the VAPID key. The
  administrator adds it once to the web server account; people do not have keys of their own.
- **Local mode:** one key per principal, at `daemon_main._resolve_path(LOCAL_KEY_RELATIVE_PATH)`.

**Where the public key is shown (trusted surfaces only).** The public key is not a secret, but the
administrator acts on it by granting access, so it must come from PrivacyFence and never through
the AI client, which a prompt injection could make show a different key. It is shown:

1. org mode: in the daemon's log at startup (D8) and by `privacyfence-app --pages-public-key` (D8);
2. local mode: in **Settings → Connectors → Web pages** (D9).

No tool result, tool error, or `privacyfence_status` output ever contains the public key.

### D4. The server's host key is pinned

`sftp_host_key` is required. PrivacyFence connects with
`known_hosts=([asyncssh.import_public_key(config.sftp_host_key)], [], [])` and nothing else: no
`~/.ssh/known_hosts`, no trust on first use. A mismatch fails before anything is sent. The first
connection happens inside an AI tool call, with nobody watching, so trust on first use would trust
whatever answered first.

### D5. SFTP client: `PagesSftpClient`

```python
class PagesClientError(Exception): ...           # every message is written in this module
class PagesUnavailableError(RuntimeError): ...   # the connector's re-raise; its text reaches the agent

class PagesSftpClient:
    def __init__(self, config: PagesConfig, key: asyncssh.SSHKey) -> None: ...
    async def publish(self, page_id: str, slug: str, data: bytes) -> None: ...
    async def unpublish(self, page_id: str) -> bool: ...   # True if the page's folder existed

def new_page_id() -> str: ...    # base64.b32encode(secrets.token_bytes(15)).decode("ascii").lower(): 24 chars of [a-z2-7]
def slugify(title: str) -> str: ...
def page_url(config: PagesConfig, page_id: str, slug: str) -> str: ...  # f"{config.public_base_url}/{page_id}/{slug}.html"
```

`slugify`: `unicodedata.normalize("NFKD", title)`, encode ASCII with `errors="ignore"`, lowercase,
replace every run of characters outside `[a-z0-9]` with `-`, strip `-` from both ends, cut to 60
characters, strip `-` again; `"page"` if empty. `slugify("Q3 report: Ünits & Co.")` is
`q3-report-units-co`.

Connection (one per call, closed at the end; nothing is pooled):

```python
asyncssh.connect(
    config.sftp_host, port=config.sftp_port, username=config.sftp_user,
    client_keys=[key], known_hosts=([host_key], [], []),
    agent_path=None, config=None, preferred_auth="publickey",
    connect_timeout=15, login_timeout=15,
)
```

then `conn.start_sftp_client()`. Both methods first check `page_id` against `^[a-z2-7]{24}$` and
`slug` against `^[a-z0-9][a-z0-9-]{0,59}$`, raising `ValueError` otherwise, before any network.

`publish(page_id, slug, data)`:

1. `folder = posixpath.join(remote_dir, page_id)`; `await sftp.makedirs(folder, exist_ok=True)`;
   `await sftp.chmod(folder, 0o755)`.
2. `tmp = posixpath.join(folder, f".{slug}.{secrets.token_hex(4)}.tmp")`; open it `"wb"`, write
   `data`, close; `await sftp.chmod(tmp, 0o644)`.
3. `await sftp.posix_rename(tmp, final)` where `final = posixpath.join(folder, f"{slug}.html")`; if
   that raises `asyncssh.SFTPOpUnsupported`, `await sftp.remove(final)` (ignoring
   `asyncssh.SFTPNoSuchFile`) and then `await sftp.rename(tmp, final)`.
4. If step 2 or 3 fails, try `await sftp.remove(tmp)` once, ignoring any error, then raise.

`unpublish(page_id)`: `folder` as above; `await sftp.exists(folder)` false → return `False`;
otherwise `await sftp.rmtree(folder)` and return `True`.

Every `asyncssh.Error` and `OSError` (including `asyncio.TimeoutError`) raised inside either method
is logged with `logger.warning("Web pages SFTP %s failed: %s", <"publish"|"unpublish">, exc)` and
re-raised as `PagesClientError(<message>)` with exactly one of these messages, chosen by type
(`{host}` is `config.sftp_host`, `{port}` `config.sftp_port`, `{user}` `config.sftp_user`,
`{dir}` `config.remote_dir`):

| Caught | Message |
|---|---|
| `asyncssh.HostKeyNotVerifiable` | `The web page server {host}:{port} presented an SSH host key that does not match the one PrivacyFence is set up with, so nothing was sent. Ask whoever set up Web pages to check the server's host key.` |
| `asyncssh.PermissionDenied` | `The web page server {host}:{port} refused PrivacyFence's SSH key for the account {user}. Ask whoever set up Web pages to add PrivacyFence's public key to that account's authorized_keys file.` |
| `asyncssh.SFTPError` (any subclass) | `The web page server {host}:{port} did not accept the page in {dir}. Ask whoever set up Web pages to check that the account {user} can write to that folder.` |
| anything else (`asyncssh.Error`, `OSError`, `asyncio.TimeoutError`) | `PrivacyFence could not connect to the web page server {host}:{port}. Try again later, or ask whoever set up Web pages to check that the server is reachable from PrivacyFence.` |

The raw exception text never enters a `PagesClientError` message.

### D6. Each person's page list: `PageIndex`

A JSON file per principal, `daemon_main._resolve_path("pages_index.json")` (so `user_dir()` in org
mode, the local principal's root in local mode). In `pages_index.py`:

```python
class PageIndexError(ValueError): ...

@dataclass(frozen=True)
class PageRecord:
    page_id: str
    title: str
    slug: str
    url: str
    bytes: int
    sha256: str
    created_at: str   # ISO 8601, UTC
    updated_at: str

class PageIndex:
    def __init__(self, path: Path) -> None: ...
    def list(self) -> list[PageRecord]: ...          # newest updated_at first
    def get(self, page_id: str) -> PageRecord | None: ...
    def upsert(self, record: PageRecord) -> None: ...
    def remove(self, page_id: str) -> bool: ...
```

File shape: `{"version": 1, "pages": [<PageRecord as dict>, ...]}`, written with
`secure_files.atomic_write_json`. A missing file is an empty list. A file that is not valid JSON,
not that shape, or has a different `version` raises
`PageIndexError("Your list of published pages could not be read. Ask whoever runs PrivacyFence to check its log.")`
after `logger.error("Unreadable Web pages index at %s", path)`. Every read-modify-write
(`upsert`, `remove`) holds a module-level `threading.Lock` named `_LOCK`. A `threading.Lock` needs
no reset in `tests/conftest.py`.

The index is what makes a page "yours": `pages_list_pages` reads only it, and `pages_publish_html`
with a `page_id` and `pages_unpublish_page` refuse a `page_id` that is not in it. The web server's
folder is shared by everyone in the organization, and nobody can list it through PrivacyFence.

### D7. The connector: `PagesConnector`

```python
class PagesConnector(Connector):
    def __init__(
        self, client: PagesSftpClient | None, *, config: PagesConfig | None = None,
        index: PageIndex | None = None, download_mode: str = "local",
    ) -> None: ...
    name -> "pages"
```

`download_mode` is passed to `local_files.require_local_files` / `read_local_file`, exactly as
`DriveConnector.download_mode` is. `call()` dispatches the three tools and raises
`ValueError(f"Unknown Web pages tool: {tool!r}")` otherwise.

**Tool specs** (exact text; every tool also has
`ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")`
as its last parameter):

`pages_publish_html`, description:

> Publish a single-file HTML page on your organization's web page server and get its link. Pass
> the whole HTML document (CSS and scripts inline, or scripts from public CDNs) as html, or, for a
> page over about 100 KB, send it with privacyfence_create_upload_slot first and pass upload_id.
> Returns {page_id, url, title, bytes, created, updated_at}; give url to the user, since anyone
> with that link can open the page. To change a page you published, call this again with its
> page_id: the page is replaced and keeps its url. Use pages_list_pages to find a page_id and
> pages_unpublish_page to take a page down. Requires user approval.

Parameters, in this order:

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `title` | str | yes | | `Page title for the approval card and pages_list_pages; also names the file in the link (for example "Q3 report" gives q3-report.html). 1 to 200 characters. A republish with page_id keeps the link's original name.` |
| `html` | str | no | `""` | `The complete HTML document as text. Leave empty when passing upload_id instead; give exactly one of html and upload_id.` |
| `upload_id` | str | no | `""` | `The upload_id privacyfence_create_upload_slot returned, after you sent the HTML file to its upload_url with PUT. Leave empty when passing html instead.` |
| `page_id` | str | no | `""` | `The page_id of a page you published earlier, from pages_list_pages or an earlier result, to replace that page at the same url. Leave empty to publish a new page with a new link.` |

`pages_list_pages`, description:

> List the web pages you published through PrivacyFence, most recently changed first. Returns
> {pages: [{page_id, title, url, bytes, created_at, updated_at}], count}, up to limit pages
> (default 50, capped at 200); times are ISO 8601, and only your own pages are listed. Pass a
> page_id to pages_publish_html to replace that page, or to pages_unpublish_page to take it down.
> Auto-approved.

Parameter `limit` (int, optional, default 50): `Maximum pages to return, most recently changed first. Default 50, capped at 200.`

`pages_unpublish_page`, description:

> Take down a web page you published, deleting it from the web page server. Returns {page_id, url,
> removed}; the link stops working and the page cannot be restored, so publishing it again with
> pages_publish_html gives a new link. Get page_id from pages_list_pages. Requires user approval.

Parameter `page_id` (str, required): `The page_id of a page you published, from pages_list_pages or a pages_publish_html result. Only your own pages can be taken down.`

Sibling map for `assert_tool_definitions_complete`:
`{"pages_publish_html": ("pages_list_pages", "pages_unpublish_page"), "pages_list_pages": ("pages_publish_html", "pages_unpublish_page"), "pages_unpublish_page": ("pages_list_pages", "pages_publish_html")}`.

**`pages_publish_html(title, html="", upload_id="", page_id="")`**, in this order:

1. `title = title.strip()`; empty or over 200 characters →
   `ValueError("title must be 1 to 200 characters.")`.
2. Exactly one of `html`, `upload_id` non-empty, else
   `ValueError("Give exactly one of html and upload_id.")`.
3. If `page_id`: `existing = index.get(page_id)`; `None` →
   `ValueError(f"No page with page_id {page_id!r} was published by you. Call pages_list_pages to see your pages.")`.
   The slug is `existing.slug`. Otherwise the slug is `slugify(title)`.
4. Bytes: `html.encode("utf-8")`, or for an upload
   `local_files.require_local_files([local_files.UPLOAD_REF_PREFIX + upload_id], max_total_bytes=config.max_bytes, download_mode=self._download_mode)`
   then `local_files.read_local_file(ref, download_mode=self._download_mode)`.
5. Over `config.max_bytes` →
   `ValueError(f"The page is {n:,} bytes, over the {config.max_bytes:,}-byte limit for Web pages.")`.
   For an upload, `data.decode("utf-8")` failing →
   `ValueError("The uploaded file is not UTF-8 text, so it is not an HTML page.")`.
6. `text = extract_text(data, "text/html")`; `sha256 = hashlib.sha256(data).hexdigest()`;
   `address = page_url(config, existing.page_id, existing.slug)` for a replacement, or
   `f"{config.public_base_url}/<new id>/{slug}.html"` for a new page.
7. `await gated_call(...)` with:
   - `connector="pages"`, `tool="pages_publish_html"`, `tool_name="Publish Web Page"`,
   - `summary=f'Publish "{title}"'` (new) or `f'Replace "{title}"'` (replacement),
   - `sender=urlsplit(config.public_base_url).hostname`,
   - `raw_data={"title": title, "page_id": page_id, "bytes": len(data), "sha256": sha256}`,
     `filtered_data=None`, `gate="popup"`,
   - `preview={"Page": title, "Address": address, "Action": "Publish a new page" | f"Replace the page first published {existing.created_at[:10]}", "Size": f"{len(data):,} bytes", "Scripts": "Yes" | "No", "Who can open it": "Anyone with the link"}`
     (`Scripts` is `"Yes"` when `re.search(r"<script\b", data.decode("utf-8", "replace"), re.I)`),
   - `details_text="The page above will be published at the address shown. Anyone with the link can open it."`,
   - `preview_blocks=preview_blocks_for(details_text, text)`,
   - `upload_pii_scan_text=text` (D10),
   - `args={"title": title, "html": html, "upload_id": upload_id, "page_id": page_id}`.
8. If `upload_id`: `local_files.commit_uploads()` (ADR 0102).
9. New page: `page_id = new_page_id()`. Then `await client.publish(page_id, slug, data)`; a
   `PagesClientError` becomes `raise PagesUnavailableError(str(exc)) from exc`.
10. `now = datetime.now(timezone.utc).isoformat()`; `index.upsert(PageRecord(page_id, title, slug, page_url(config, page_id, slug), len(data), sha256, existing.created_at if existing else now, now))`.
11. Return `{"page_id": page_id, "url": <url>, "title": title, "bytes": len(data), "created": existing is None, "updated_at": now}`.

**`pages_list_pages(limit=50)`**: `limit` clamped to 1-200; records from `index.list()[:limit]`;
return `{"pages": [{"page_id", "title", "url", "bytes", "created_at", "updated_at"} per record], "count": len(pages)}`;
then `_auto_audit("pages_list_pages", "List Web Pages", f"{n} page(s)", "", t0)` copied from
`telegram.py:345`. It never contacts the server.

**`pages_unpublish_page(page_id)`**:

1. `record = index.get(page_id)`; `None` → the same `ValueError` text as publish step 3.
2. `await gated_call(connector="pages", tool="pages_unpublish_page", tool_name="Take Down Web Page", summary=f'Take down "{record.title}"', sender=<hostname>, raw_data={"page_id": page_id}, filtered_data=None, gate="popup", preview={"Page": record.title, "Address": record.url, "Published": record.created_at[:10]}, details_text="The page is deleted from the web server. Its link stops working, and the page cannot be restored.", args={"page_id": page_id})`.
3. `await client.unpublish(page_id)` (a `PagesClientError` → `PagesUnavailableError`, and the
   index is left unchanged); then `index.remove(page_id)`.
4. Return `{"page_id": page_id, "url": record.url, "removed": True}` (also when the folder was
   already gone on the server).

**Wiring tables** (all in the connector phase):

- `auto_accept.TOOL_TO_OPERATION`: `"pages_publish_html": "pages.publish_page"`,
  `"pages_unpublish_page": "pages.unpublish_page"`.
- `auto_accept.TOOL_TO_GATE`: `"pages_list_pages": "auto"`, `"pages_publish_html": "popup"`,
  `"pages_unpublish_page": "popup"`.
- `policy/registry.py` `TOOL_TO_VERB`: `"pages_publish_html": Verb.SHARE`,
  `"pages_unpublish_page": Verb.DELETE`.
- `write_effects.EFFECT_BY_TOOL`:
  `"pages_publish_html": "The page goes live at its link, where anyone with the link can open it. It can be replaced or taken down later, but not from copies people already saved."`,
  `"pages_unpublish_page": "The page is deleted from the web server and its link stops working. It cannot be restored."`.
- `gate._TOOL_LAYOUT`: `"pages_publish_html": WIDE`.
- No entry in `policy/scopes.py`, `policy/catalogue.py`, `policy/propose.py` or
  `policy/resource_registry.py`: the approval card offers no "Always allow" for Web pages (D10).
- `tests/unit/test_connector_tool_annotations.py` `DESTRUCTIVE_TOOLS` gains
  `"pages_unpublish_page"`, with its comment updated to "a calendar event, rows or columns of a
  spreadsheet, and a published web page".
- `CONNECTOR_CLASSES` in `tests/unit/connectors/test_readme_manifest_alignment.py` and
  `tests/unit/test_systemic_gate_invariants.py` gain `PagesConnector`.
- `scripts/generate_tools_reference.py`: `CONNECTOR_TITLES["pages"] = "Web pages"` and
  `CONNECTOR_SHORT["pages"] = "Web pages"`, placed last; then regenerate `docs/tools-reference.md`
  and `docs/always-allow-rules-reference.md` with their scripts.
- `scripts/pyinstaller_common.py`: `"privacyfence.connectors.pages"` after
  `"privacyfence.connectors.apps_script"`.
- `website/connectors/index.html`: a card after the last one, in the same markup as the Telegram
  card (`website/connectors/index.html:112-121`) but with `id="web-pages"`,
  `data-connector="Web pages"`, the counts the regenerated reference prints, a one-sentence body
  "Publish a single-file HTML page on a web server your organization runs, and get back a link
  anyone can open.", and a single link
  `<a href="https://github.com/privacyfence/privacyfence/blob/main/docs/tools-reference.md#web-pages">Every Web pages tool</a>`
  (a `/docs/` link would fail the website build's link check until a stable release carries the
  new section, ADR 0052). "Eleven connectors" becomes "Twelve connectors", and the tool totals
  on `/connectors/` and `/how-it-works/` become the new total the tests compute.

### D8. Daemon wiring

In `daemon_main.py`:

```python
def pages_config(config: dict[str, Any], org_config: dict[str, Any]) -> PagesConfig | None:
    """Org mode: org_config.json's "pages" section; local mode: settings.yaml's pages section.
    None when the section is absent."""

def pages_key_path(org_config: dict[str, Any]) -> Path:
    """org_dir() / ORG_KEY_FILE_NAME in org mode, else Path(_resolve_path(LOCAL_KEY_RELATIVE_PATH))."""
```

"Org mode" is `org_mode.resolve_mode(org_config) == "org"` in both.

`build_connectors` gains, after the Telegram block:

```python
    # Web pages: an SFTP target, not an account -- the organization's bundle names it in org
    # mode, the person's own settings.yaml in local mode (ADR 0116).
    if enabled("pages"):
        try:
            cfg = pages_config(config, org_config)
            if cfg is None:
                if download_mode == "org":
                    raise PagesClientError("Web pages organization config not installed")
                raise PagesClientError("Web pages are not set up. Use Authenticate… in PrivacyFence Settings.")
            key = load_or_create_ssh_key(pages_key_path(org_config))
            connectors.append(PagesConnector(
                PagesSftpClient(cfg, key), config=cfg,
                index=PageIndex(Path(_resolve_path("pages_index.json"))), download_mode=download_mode,
            ))
        except (PagesClientError, PagesConfigError, ValueError, OSError) as exc:
            logger.warning("Web pages connector disabled: %s", exc)
            failures["pages"] = _classify_connector_failure(exc)
```

(`download_mode` is the variable `build_connectors` already computes. The ellipsis in "Use
Authenticate…" is U+2026, which `_classify_connector_failure` matches.)

`TOKEN_FILES` is not changed: the key is not an OAuth token and org mode keeps it outside
`user_dir()`.

**Org startup.** A new function `_log_pages_setup(org_config)` is called in
`_start_org_web_server` just before `server = WebServer(...)`. When `"pages"` is in `org_config`:
`pages_config(...)` with a `PagesConfigError` re-raised as `org_mode.ConfigurationError(str(exc))`
(the daemon refuses to start, like any other invalid bundle section), then
`load_or_create_ssh_key(pages_key_path(org_config))`, then:

```python
logger.info(
    "Web pages: publishing over SFTP as %s@%s:%d into %s, served at %s. "
    "PrivacyFence's SSH public key for that account: %s",
    cfg.sftp_user, cfg.sftp_host, cfg.sftp_port, cfg.remote_dir, cfg.public_base_url,
    public_key_line(key),
)
```

**CLI.** `parse_args` gains `--pages-public-key` (`action="store_true"`, help: `Print the SSH
public key this organization server publishes web pages with, generating it on first use, and exit.
Run it as the service account.`). In `main()`, after `setup_logging(config)`, inside the existing
`try:` and before the OAuth-flag branch:
`if args.pages_public_key: return run_print_pages_public_key(load_org_config())`.

```python
def run_print_pages_public_key(org_config: dict[str, Any]) -> int:
```

Not org mode → print to stderr `This is a desktop install. Its Web pages public key is in
PrivacyFence Settings, under Connectors → Web pages.` and return 1. Org mode → print
`public_key_line(load_or_create_ssh_key(pages_key_path(org_config)))` to stdout and return 0.

**`src/privacyfence/resources/settings.yaml.example`** gains, after the `connectors:` section, a
commented example:

```yaml
# Web pages (desktop installs; an organization server takes these from its bundle instead).
# PrivacyFence Settings > Connectors > Web pages writes this section.
# pages:
#   sftp_host: pages.example.com
#   sftp_port: 22
#   sftp_user: pf-pages
#   sftp_host_key: "ssh-ed25519 AAAA... root@pages"
#   remote_dir: /srv/pages
#   public_base_url: https://pages.example.com
```

### D9. Settings (desktop installs)

In `settings_controller.py`:

- `ALL_CONNECTORS` gains `"pages"` last; `_CONNECTOR_LABEL_OVERRIDES["pages"] = "Web pages"`.
- `_connectors_state()`: for `"pages"`, `has_org` is whether
  `daemon_main.pages_config(self._load_config(), {})` returns a config without raising
  `PagesConfigError` (a raise counts as `False`). The pages row also carries
  `"auth_label": "Change…" if has_org else "Set up…"` and a `"pages"` dict:
  `{"public_key": public_key_line(load_or_create_ssh_key(daemon_main.pages_key_path({}))), "sftp_host": ..., "sftp_port": ..., "sftp_user": ..., "sftp_host_key": ..., "remote_dir": ..., "public_base_url": ...}`,
  the six values read from the raw `pages:` mapping as strings (`""` when absent, the port as a
  string). No other row gets a `"pages"` key.
- A new action, copying `toggle_gmail_signature`'s shape:

```python
def pages_configure(
    self, sftp_host: str, sftp_port: int, sftp_user: str, sftp_host_key: str,
    remote_dir: str, public_base_url: str,
) -> dict[str, Any]:
```

It builds the mapping, validates it with
`PagesConfig.from_mapping(mapping, "Web pages settings")`; on `PagesConfigError` sets
`self.error = str(exc)` and returns `self.snapshot()` without saving; otherwise sets
`cfg["pages"] = mapping` (keys exactly the six fields, values as validated: host key stripped,
`public_base_url` without trailing `/`), `_save_config(cfg)`, `self.refresh_connectors()`, returns
`self.snapshot()`. How `self.error` is shown follows `export_audit_log_path`'s existing use.

- `web/org_settings_scope.py` `ACTION_SCOPES`:
  `"pages_configure": ActionScope(modes=frozenset({LOCAL_MODE}), admin_only=True)`, next to
  `toggle_gmail_signature`.
- `web/routes_settings.py`: `"pages_configure"` goes into `_SENSITIVE_ACTIONS` (it decides where
  published content goes and which server key is trusted; like `enable_connector`, ADR 0070), so it
  needs a human session and step-up (ADR 0034).

In `settings_window_html.py` (JS inside the Python string):

- `connectorStatus()`: for `c.key === 'pages'` and `!c.has_org` the badge is `Not configured`
  (checked before the generic "Organization config missing" branch).
- `renderConnectors()`: the pages row's link carries `data-pages-setup` instead of
  `authenticate_connector`, like Telegram's `data-telegram-auth`, and its text is `c.auth_label`.
- A modal `renderPagesModal()` built like `renderTelegramModal`, open while `ui.pagesSetup` is
  true, its six inputs held in `ui.pages.*` (so a snapshot re-render keeps what was typed,
  following the `onInput` pattern) and pre-filled from the row's `pages` dict when opened.
  Exact strings:
  - heading `Web pages`;
  - intro `PrivacyFence publishes pages over SFTP to a web server you run. Anyone with a page's link can open it.`;
  - labels and placeholders: `Server` / `pages.example.com`; `Port` / `22`; `Account` /
    `pf-pages`; `Server's host key` / `ssh-ed25519 AAAA… (from /etc/ssh/ssh_host_ed25519_key.pub on the server)`;
    `Folder on the server` / `/srv/pages`; `Public address of that folder` / `https://pages.example.com`;
  - the public key in a read-only `<textarea>` (selectable, no copy button) under the heading
    `PrivacyFence's public key` and the line `Add this line to the account's ~/.ssh/authorized_keys file on the server.`;
  - buttons `Save` (posts `pages_configure` with the six values, the port as a number) and
    `Cancel` (closes, discards `ui.pages`).
- The modal closes after a save that leaves `snapshot.error` empty.

Org mode gets no settings surface: the connectors section stays hidden there, and
`pages_configure` is local-only through `ACTION_SCOPES`.

### D10. Approval: the PII scan applies and "Always allow" is not offered

Publishing puts content on the open web behind a link that can be forwarded, and an uploaded page
may be a file Claude never read. So `pages_publish_html` passes `upload_pii_scan_text` (the
page's extracted text), which makes a PII match force the same second confirmation
`drive_upload_file` gets. The gate's docstrings that say only `drive_upload_file` sets it
(`gate.py:109-139` and the `upload_pii_scan_text` parameter comment at `:876-882`, and the comment
at `:955-960`) are updated to name both tools and point to ADR 0120. No scope proposals are added,
so the card has no "Always allow"; a rule written by hand for `pages.publish_page` still works like
any other operation-level rule.

### D11. The web server (documentation only)

PrivacyFence does not serve pages. `docs/web-pages-setup.md` (new, in the last phase) tells the
administrator, for Ubuntu 24.04, whether the web server is the PrivacyFence host or another one:

1. A dedicated account that can only use SFTP:
   `sudo useradd --system --create-home --home-dir /var/lib/pf-pages --shell /usr/sbin/nologin pf-pages`,
   and `/etc/ssh/sshd_config.d/pf-pages.conf` containing
   `Match User pf-pages` / `ForceCommand internal-sftp` / `AllowTcpForwarding no` /
   `X11Forwarding no` / `PermitTTY no`, then `sudo systemctl reload ssh`.
2. The folder: `sudo install -d -o pf-pages -g pf-pages -m 755 /srv/pages`.
3. PrivacyFence's public key into `/var/lib/pf-pages/.ssh/authorized_keys` (directory 700, file
   600, owned by `pf-pages`), prefixed with `restrict `.
4. The server's host key for `sftp_host_key`: `cat /etc/ssh/ssh_host_ed25519_key.pub`.
5. A **separate hostname** (for example `pages.acme.example.com`), never PrivacyFence's own: a
   published page runs its own scripts, and on PrivacyFence's origin it could act with a signed-in
   person's session. Caddy:

   ```
   pages.acme.example.com {
       root * /srv/pages
       file_server
       header {
           Content-Security-Policy "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms allow-modals allow-downloads"
           X-Content-Type-Options nosniff
           Referrer-Policy no-referrer
           X-Robots-Tag "noindex, nofollow"
           Cache-Control no-cache
       }
   }
   ```

   and the nginx equivalent (`root /srv/pages; autoindex off; location / { try_files $uri =404; }`
   and the same five headers with `add_header ... always;`). No directory listing: a page's
   random folder name is what keeps other pages from being found.
6. Then either the bundle flags (org) or **Settings → Connectors → Web pages** (desktop), and a
   test publish.

It also says: the `sandbox` header gives every page an opaque origin, so a page's
`localStorage`/cookies do not persist (claude.ai artifacts already tolerate this); pages that use
claude.ai-only artifact features (shared storage, asking Claude) do not work once published
elsewhere; and an organization using claude.ai needs nothing extra for inline `html`, but an
`upload_id` publish from claude.ai needs the PrivacyFence host on claude.ai's domain allowlist
like any upload (ADR 0097).

### D12. Bundle flags

`scripts/build_org_bundle.py` (stdlib-only; it cannot import `privacyfence`) gains an argument
group `Web pages (org mode, docs/web-pages-setup.md)` with `--pages-sftp-host HOST`,
`--pages-sftp-port PORT` (int), `--pages-sftp-user USER`, `--pages-sftp-host-key LINE`,
`--pages-remote-dir PATH`, `--pages-public-base-url URL`, `--pages-max-bytes BYTES` (int), all
`default=None`, and `--no-pages` (`action="store_true"`, removes the section). Merge logic copies
the `--downloads-*` block (`:607-623`): the flags require `--mode org` (or `--merge` against an
org bundle), else `SystemExit("--pages-* flags require --mode org (or --merge against an existing org-mode bundle).")`;
given values are merged into `bundle["pages"]` (flag names map to the D2 keys without the `pages-`
prefix and with `-` → `_`); after merging, when `bundle["pages"]` exists it must contain
`sftp_host`, `sftp_user`, `sftp_host_key`, `remote_dir` and `public_base_url`, and
`public_base_url` must start with `https://`, else
`SystemExit("--pages-* needs --pages-sftp-host, --pages-sftp-user, --pages-sftp-host-key, --pages-remote-dir and an https:// --pages-public-base-url.")`.
The daemon repeats the full D2 validation at startup. `--mode local` pops `"pages"` like it pops
`"download_delivery"` (`:557`). The printed summary lists the section like the others.

## ADRs

Written in the last phase, each `Accepted — <date of that phase>` and added to
`docs/adr/README.md`'s index:

- **0116** — Web pages are published over SFTP to a web server the organization runs, never served
  by PrivacyFence itself. Rejected: PrivacyFence serving `/pages/…` on its own origin (a page's
  scripts would share the origin of the approvals UI and session cookie, and the app's CSP would
  need a per-path hole); WebDAV or an HTTP `PUT` receiver (needs a server module and a credential
  over HTTP); object storage (not "minimal config on an Ubuntu server"); per-mode different
  transports.
- **0117** — PrivacyFence speaks SFTP with the `asyncssh` library (a new runtime dependency,
  EPL-2.0 or GPL-2.0-or-later). Rejected: the system OpenSSH `sftp` binary (key-file ACL checks
  under the Windows virtual service account, `known_hosts` handling, subprocess plumbing per OS);
  paramiko (synchronous, pulls in bcrypt and PyNaCl). Departs from "standard library first" the
  way ADR 0009's SDK choice does.
- **0118** — PrivacyFence generates its own SFTP key, pins the server's host key from its
  configuration, and shows the public key only on trusted surfaces (startup log, CLI, local
  Settings), never to the AI client. Rejected: trust on first use (the first connection is an
  unattended tool call); an administrator-supplied private key in the bundle or settings (a secret
  that has to travel); per-person keys in org mode; telling the agent the public key (a prompt
  injection could substitute its own key for the administrator to authorize).
- **0119** — A page's link is a random 120-bit id plus a readable name, anyone with the link can
  open it, and each person lists, replaces and takes down only the pages they published, from a
  per-person index PrivacyFence keeps. Rejected: readable-only URLs (guessable); pages behind the
  identity provider (needs PrivacyFence to serve them, ADR 0116); listing the server's folder as
  the index (shared by everyone, and it would hand every page's link to every person).
- **0120** — Publishing a web page runs the real PII scan with a forced confirmation, the second
  write to do so after `drive_upload_file`, and the approval card offers no "Always allow" for it.
  Amends the gate's "only `drive_upload_file`" rule.

## Manual steps

`manual_before`: none. Every phase builds and tests against an in-process `asyncssh` SFTP server.

`manual_after` (step-by-step in the artifact, `manual_steps_artifact` below):

1. **ma1-org-end-to-end**: set up a web page server on Ubuntu (the PrivacyFence host or another),
   add the bundle flags to a test organization deployment, and publish, replace, list and take
   down a page from claude.ai and from Claude Code; check the approval cards, the PII confirmation,
   the headers, and a wrong host key.
2. **ma2-desktop-end-to-end**: on a desktop install built from the feature branch, set up Web pages
   in Settings and publish from Claude Desktop.

## Risks and open questions

- **asyncssh on Windows and macOS CI.** The client tests run a real in-process `asyncssh` server
  with `SFTPServer(chan, chroot=...)`. If the `platform-windows` or `platform-macos` job fails in
  that fixture on path handling, the fix is in the fixture (the root path passed as
  `str(tmp_path).encode()`, the remote paths relative), never a skip. If that does not fix it, the
  phase stops with `status=blocked`.
- **PyInstaller.** `connectors/pages.py` imports `pages_client`, which imports `asyncssh` at the top,
  and `daemon_main` imports the connector, so a bundle missing asyncssh fails `build.yml`'s smoke
  start. The last phase dispatches `build.yml`; a failure there naming `asyncssh` means the hidden
  import needs `collect_submodules`, which that phase adds in `scripts/pyinstaller_common.py`.
- **License.** asyncssh is EPL-2.0 OR GPL-2.0-or-later. The maintainer accepted the dependency when
  choosing asyncssh in the `/make-plan` session; ADR 0117 records it.
- **Policy tables.** If a policy test (`tests/unit/policy/`) fails because `pages.publish_page` or
  `pages.unpublish_page` has no scope selector or catalogue entry, the brief is wrong about
  "no entry needed": stop with `status=blocked` rather than inventing a scope.
- **Snapshot keys.** `test_snapshot_has_one_key_per_page` pins the settings snapshot's top-level
  keys; D9 adds nothing at the top level (the pages data rides on its connector row). If that test
  fails, the change added a key it should not have.
- **ADR numbers.** 0116-0120 are free on `main` today; if a parallel branch takes one first, the
  last phase renumbers (ADR README rule 6).
- **Website counts.** `test_the_totals_in_the_copy_match` asserts `len(REFERENCE) == 11` and
  "Eleven connectors"; the connector phase changes both to 12/"Twelve". If other copy on the
  website counts connectors in words and a website test fails on it, update that copy the same way.

## Implementation manifest

```yaml
plan_slug: web-pages
feature_branch: feature/web-pages
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/WTJJ3qAB4c9XEsH6j8nirk
manual_steps_source: docs/web-pages-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-org-end-to-end
    title: Set up a web page server and publish from claude.ai and Claude Code on an organization deployment
    why: Only a real Ubuntu sshd, a real web server and the real AI clients show the SFTP setup, headers, links and approval cards work together; CI uses an in-process SFTP server.
  - id: ma2-desktop-end-to-end
    title: Set up Web pages in a desktop install's Settings and publish from Claude Desktop
    why: The Settings modal, step-up on save and a packaged build's asyncssh are only exercised by a real desktop install.
verify_after_merge:
  - python3 -m pytest tests/unit -q
  - ruff check .
  - python3 scripts/mypy_strict_modules.py
final_checks:
  - docs/web-pages-plan.md and docs/web-pages-plan-manual-steps.html are deleted and nothing links to them (git grep -n "web-pages-plan" returns nothing)
  - docs/adr/0116-*.md through docs/adr/0120-*.md exist, each says Accepted, and each is in docs/adr/README.md's index
  - CHANGELOG.md has an [Unreleased] entry for Web pages and no new version heading
  - build.yml dispatched against feature/web-pages is green, and its run is linked in the PR
phases:
  - id: p1-sftp-client
    title: asyncssh dependency, PagesConfig, SSH key, SFTP client and the per-person page index
    depends_on: []
    complexity: M
    touches:
      - pyproject.toml
      - requirements/runtime.lock.txt
      - requirements/dev.lock.txt
      - requirements/docs.lock.txt
      - src/privacyfence/pages_client.py
      - src/privacyfence/pages_index.py
      - scripts/pyinstaller_common.py
      - tests/unit/test_pages_client.py
      - tests/unit/test_pages_index.py
      - tests/unit/test_systemic_gate_invariants.py
    brief: |
      Read docs/web-pages-plan.md sections D1-D6 first; they hold every name, message and rule.
      1. pyproject.toml: add "asyncssh>=2.24,<3.0" to [project] dependencies after
         "portalocker>=2.8", with a comment in the file's style: SFTP client for the Web pages
         connector (ADR 0117); pure Python on cryptography, already a dependency; licensed
         EPL-2.0 or GPL-2.0-or-later. Run scripts/update_dependency_locks.sh (uv is on PATH) and
         commit whichever requirements/*.lock.txt it changes. pip install -e ".[test]" again.
      2. scripts/pyinstaller_common.py: add "asyncssh" to HIDDEN_IMPORTS after "telethon", with
         the comment "# asyncssh (Web pages connector's SFTP client)".
      3. Create src/privacyfence/pages_client.py (module docstring explaining: the Web pages
         SFTP client, why the host key is pinned (ADR 0118), why errors carry only text written
         here). Contents exactly per D2 (PagesConfigError, PagesConfig with from_mapping and its
         messages table, DEFAULT_MAX_BYTES, MAX_MAX_BYTES), D3 (KEY_COMMENT, ORG_KEY_FILE_NAME,
         LOCAL_KEY_RELATIVE_PATH, load_or_create_ssh_key copying
         web_push.load_or_create_vapid_key at src/privacyfence/web_push.py:128, public_key_line),
         D5 (PagesClientError, PagesUnavailableError(RuntimeError), PagesSftpClient.publish and
         .unpublish with the exact connect() arguments, the page_id/slug checks, the four-row
         error table, new_page_id, slugify, page_url). Use `from __future__ import annotations`
         and `X | None`; no Optional.
      4. Create src/privacyfence/pages_index.py per D6 (PageIndexError, PageRecord, PageIndex,
         module-level _LOCK = threading.Lock()), writing with secure_files.atomic_write_json.
      5. tests/unit/test_systemic_gate_invariants.py: add ("pages_client", None,
         "load_or_create_ssh_key") to TOKEN_WRITE_SITES (alphabetical position) and change
         `assert len(TOKEN_WRITE_SITES) == 10` to 11, renaming the test to
         test_eleven_token_write_sites_are_listed and updating its comment's count.
      6. tests/unit/test_pages_client.py (marker unit; module docstring naming the invariant:
         nothing is sent to a server whose host key does not match). A fixture starts a real
         in-process server: asyncssh.listen("127.0.0.1", 0, server_host_keys=[host_key],
         server_factory=<SSHServer subclass accepting only the client key via
         validate_public_key>, sftp_factory=lambda chan: asyncssh.SFTPServer(chan,
         chroot=str(root).encode()), allow_scp=False), yielding (port, root, host_key, client_key);
         config remote_dir is "site" (relative, inside the chroot). Classes:
         TestPagesConfig (every row of D2's table: one accepted value, one rejected value with its
         exact message; defaults; trailing-slash and normpath normalization; bool rejected for
         ints), TestSlugify (the D5 example, empty → "page", 60-char cap, no leading/trailing
         "-"), TestNewPageId (24 chars of [a-z2-7], two calls differ), TestSshKey (creates 0600
         file on POSIX, reloads the same key, refuses a non-Ed25519 key with the D3 message,
         public_key_line ends with " privacyfence-pages"), TestPublish (file lands at
         root/site/<id>/<slug>.html with the bytes, mode 0644, folder 0755 on POSIX; replacing
         overwrites; no *.tmp left), TestUnpublish (removes the folder, returns True; missing →
         False), TestFailures (wrong pinned host key → PagesClientError with the exact
         HostKeyNotVerifiable message and no file written; wrong client key → PermissionDenied
         message; closed port → connect message; remote_dir pointing at a file → SFTPError
         message; bad page_id/slug → ValueError before connecting). Every async test finishes in
         well under the 30 s timeout: connect_timeout stays 15 but tests use 127.0.0.1.
      7. tests/unit/test_pages_index.py: empty when missing, upsert/get/list order by
         updated_at desc, replace keeps one record, remove True/False, malformed JSON / wrong
         version → PageIndexError with the D6 message, file written 0600 on POSIX.
      8. Run ruff check ., bandit -c pyproject.toml -r src, python3 -m pytest
         tests/unit/test_pages_client.py tests/unit/test_pages_index.py
         tests/unit/test_systemic_gate_invariants.py tests/unit/test_pyinstaller_hidden_imports.py -q.
      No CHANGELOG line in this phase (nothing is user-visible yet).
      Stop with status=blocked if: asyncssh>=2.24 cannot be resolved by the lock script; the
      in-process server cannot be made to work with chroot on Linux; or
      test_pyinstaller_hidden_imports.py rejects a non-connector entry in HIDDEN_IMPORTS.
    acceptance:
      - python3 -m pytest tests/unit/test_pages_client.py tests/unit/test_pages_index.py -q passes
      - python3 -m pytest tests/unit/test_systemic_gate_invariants.py tests/unit/test_pyinstaller_hidden_imports.py -q passes
      - grep -n '"asyncssh>=2.24,<3.0"' pyproject.toml prints one line, and grep -n '^asyncssh==' requirements/runtime.lock.txt prints one line
      - ruff check . and bandit -c pyproject.toml -r src pass

  - id: p2-bundle-flags
    title: build_org_bundle.py --pages-* flags and their configuration-reference rows
    depends_on: []
    complexity: S
    touches:
      - scripts/build_org_bundle.py
      - tests/unit/test_build_org_bundle.py
      - docs/configuration-reference.md
    brief: |
      Read docs/web-pages-plan.md sections D2 and D12 first.
      1. scripts/build_org_bundle.py: add the argument group, flags, merge block, required-key
         check, --no-pages, the --mode local pop and the summary line exactly as D12 says,
         copying the --downloads-* code (arg group near line 350, merge near line 607, pop near
         line 557). The script stays standard-library only; do not import privacyfence.
      2. tests/unit/test_build_org_bundle.py: extend it with a TestPagesFlags class, following the
         file's existing tests of --downloads-*: all flags → bundle["pages"] has the D2 keys with
         the right types (sftp_port and max_bytes ints); --merge adds/changes one key and keeps the
         rest; --no-pages removes the section; missing required key → the exact SystemExit text;
         http:// URL → the same SystemExit; --pages-* without --mode org → its SystemExit;
         --mode local drops a pages section.
      3. docs/configuration-reference.md: in the "Build options" table add one row per new flag
         in the style of the download_delivery rows, pointing to docs/web-pages-setup.md (that
         file is written by a later phase; keep the link). Add the pages.* keys to the bundle-key
         list if the doc lists bundle keys separately.
      4. Run python3 -m pytest tests/unit/test_build_org_bundle.py -q and ruff check .
      No CHANGELOG line in this phase.
      Stop with status=blocked if the existing --downloads-* merge code does not exist in the
      shape D12 describes (lines 350-379 and 607-623).
    acceptance:
      - python3 -m pytest tests/unit/test_build_org_bundle.py -q passes, including TestPagesFlags
      - python3 scripts/build_org_bundle.py --help | grep -c -- '--pages-' prints 8
      - ruff check . passes

  - id: p3-connector
    title: PagesConnector, its gate/policy tables, generated references and the website card
    depends_on: [p1-sftp-client]
    complexity: M
    touches:
      - src/privacyfence/connectors/pages.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/write_effects.py
      - src/privacyfence/gate.py
      - scripts/pyinstaller_common.py
      - scripts/generate_tools_reference.py
      - docs/tools-reference.md
      - docs/always-allow-rules-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
      - tests/unit/connectors/test_pages_connector.py
      - tests/unit/connectors/test_readme_manifest_alignment.py
      - tests/unit/test_systemic_gate_invariants.py
      - tests/unit/test_connector_tool_annotations.py
      - tests/unit/test_website_connectors_page.py
      - tests/unit/test_gate.py
    brief: |
      Read docs/web-pages-plan.md sections D1, D5-D7 and D10 first; tool texts, card fields and
      messages are given there verbatim. Template: src/privacyfence/connectors/telegram.py.
      1. Create src/privacyfence/connectors/pages.py with PagesConnector exactly per D7 (module
         docstring "Web pages connector." plus one sentence on why the index, not the server,
         decides whose page it is, ADR 0119). Group methods under the telegram.py banner comments
         ("Auto", "Popup gate (writes)", "Helpers"). tool_specs() must not touch the client
         (catalog builds cls(None)). A PagesClientError is re-raised as
         PagesUnavailableError(str(exc)) from exc. Use asyncio-native calls only (asyncssh is
         async; no to_thread needed); PageIndex file I/O is small and stays synchronous.
      2. Wiring tables exactly per D7's "Wiring tables" list: auto_accept.TOOL_TO_OPERATION and
         TOOL_TO_GATE, policy/registry.py TOOL_TO_VERB, write_effects.EFFECT_BY_TOOL,
         gate._TOOL_LAYOUT, scripts/pyinstaller_common.py's connector list,
         scripts/generate_tools_reference.py's two dicts.
      3. gate.py: update the three places D10 names so they say upload_pii_scan_text is set by
         drive_upload_file and pages_publish_html, with the reason for the second one in one
         sentence (content published behind a public link) and "ADR 0120". Change no logic.
      4. Regenerate docs/tools-reference.md with python3 scripts/generate_tools_reference.py and
         docs/always-allow-rules-reference.md with python3
         scripts/generate_always_allow_reference.py; commit both outputs unedited.
      5. Website per D7's last bullet: the card in website/connectors/index.html, "Twelve
         connectors", and the tool totals on /connectors/ and website/how-it-works/index.html.
         In tests/unit/test_website_connectors_page.py change `len(REFERENCE) == 11 and
         "Eleven connectors"` to 12 and "Twelve connectors".
      6. tests/unit/connectors/test_readme_manifest_alignment.py and
         tests/unit/test_systemic_gate_invariants.py: add PagesConnector to CONNECTOR_CLASSES.
         tests/unit/test_connector_tool_annotations.py: DESTRUCTIVE_TOOLS per D7.
      7. tests/unit/connectors/test_pages_connector.py (marker unit), following
         docs/coding-and-testing-guidelines.md §2.6 and the gated_call_spy pattern of
         tests/unit/connectors/test_telegram_connector.py; the client is an AsyncMock with
         spec=PagesSftpClient, the index a real PageIndex under tmp_path. Classes: TestDispatch
         (unknown tool → ValueError); TestListPages (auto, never calls gated_call, writes its own
         audit entry, order, limit clamp 0→1 and 500→200, no client call); TestPublishNew (card
         kwargs: preview has exactly the six D7 keys and no HTML body, Address uses "<new id>",
         upload_pii_scan_text is the extracted text, args carry title/html/upload_id/page_id,
         WIDE layout; client.publish called with a 24-char id and the slug; result shape; index
         record); TestPublishReplace (unknown page_id → the exact ValueError, no gate; known →
         same page_id and slug, created_at kept, "Replace ..." summary and Action text);
         TestPublishValidation (both/neither of html and upload_id, empty and 201-char title,
         over max_bytes, non-UTF-8 upload – each the exact message, gate never reached);
         TestPublishUpload (upload_id path: require_local_files/read_local_file used and
         commit_uploads called after the gate – copy the Drive upload_id tests' setup in
         tests/unit/connectors/test_drive_connector.py); TestUnpublish (unknown → ValueError;
         known → gated card, client.unpublish, record removed, result; client failure → 
         PagesUnavailableError with the client's message and the record kept); TestErrors
         (PagesClientError on publish → PagesUnavailableError, index unchanged; and
         safe_errors.public_message on it returns its text, not the generic message);
         assert_all_tools_leave_an_audit_trail with arg_overrides for publish (html="<p>x</p>")
         and unpublish (a page_id present in the index); TestToolDefinitions calling
         assert_tool_definitions_complete with D7's sibling map.
      8. tests/unit/test_gate.py: one test in the existing upload_pii_scan_text coverage
         proving a popup call with tool="pages_publish_html" and a PII-bearing
         upload_pii_scan_text forces the confirmation (copy the drive_upload_file case).
      9. Run python3 -m pytest tests/unit -q, ruff check ., bandit -c pyproject.toml -r src,
         python3 scripts/mypy_strict_modules.py.
      No CHANGELOG line yet (the last phase writes it).
      Stop with status=blocked if: a tests/unit/policy test demands a scope selector or
      catalogue entry for pages.* (see the plan's Risks); test_docs_tools_reference or the
      website tests still fail after regenerating and updating the counts; or
      assert_tool_definitions_complete rejects a D7 text as written (report which rule).
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_pages_connector.py -q passes
      - python3 -m pytest tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_systemic_gate_invariants.py tests/unit/test_connector_tool_annotations.py tests/unit/test_write_effects.py tests/unit/test_docs_tools_reference.py tests/unit/test_website_connectors_page.py tests/unit/test_generate_always_allow_reference.py tests/unit/test_pyinstaller_hidden_imports.py tests/unit/web/test_tool_schema_portability.py -q passes
      - python3 -m pytest tests/unit -q passes
      - grep -n '^## Web pages' docs/tools-reference.md prints one line
      - grep -c 'pages_publish_html' src/privacyfence/gate.py prints at least 2

  - id: p4-daemon-wiring
    title: Build the connector in both modes, org startup log and --pages-public-key
    depends_on: [p3-connector]
    complexity: S
    touches:
      - src/privacyfence/daemon_main.py
      - src/privacyfence/resources/settings.yaml.example
      - tests/unit/test_daemon_main.py
    brief: |
      Read docs/web-pages-plan.md section D8 first; it gives the code.
      1. src/privacyfence/daemon_main.py: add the imports, pages_config(), pages_key_path(), the
         build_connectors block after the Telegram block (daemon_main.py:1503-1536), and
         _log_pages_setup() called in _start_org_web_server just before `server = WebServer(`,
         the --pages-public-key flag in parse_args, its branch in main() and
         run_print_pages_public_key(), all exactly as D8 says.
      2. src/privacyfence/resources/settings.yaml.example: the commented pages block from D8,
         after the connectors: section.
      3. tests/unit/test_daemon_main.py: a TestPagesConnectorBuild class copying how the file
         already tests settings.yaml plumbing into build_connectors (around lines 840-847) and
         failure classification (around 1216-1238): local config present → a PagesConnector
         with download_mode "local", its key file created under the sandboxed PROJECT_ROOT's
         credentials/; local config absent → failures["pages"] == "not_authenticated"; invalid
         local config → failures["pages"] is the redacted public message (not a crash);
         org bundle with pages → key at org_dir()/pages_ssh_key (monkeypatch org_dir to tmp_path);
         org bundle without pages → failures["pages"] == "no_org_config";
         connectors.pages.enabled false → no connector and no failure entry.
         TestLogPagesSetup: valid section → one INFO record containing "Web pages: publishing over
         SFTP as" and "ssh-ed25519 "; invalid section → org_mode.ConfigurationError; no section →
         nothing logged. TestPrintPagesPublicKey: local → exit 1 and the exact stderr text;
         org → exit 0 and stdout is the key line, stable across two runs.
      4. Run python3 -m pytest tests/unit/test_daemon_main.py -q, then python3 -m pytest
         tests/unit -q, ruff check ., bandit -c pyproject.toml -r src.
      No CHANGELOG line yet.
      Stop with status=blocked if build_connectors has no `download_mode` variable holding
      org_mode.resolve_mode(org_config), or if _start_org_web_server no longer builds the
      WebServer in one place.
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py -q passes, including TestPagesConnectorBuild, TestLogPagesSetup and TestPrintPagesPublicKey
      - python3 -m privacyfence.daemon_main --help 2>/dev/null | grep -c -- '--pages-public-key' prints 1 (or the same via the privacyfence-app console script)
      - python3 -m pytest tests/unit -q passes

  - id: p5-settings
    title: Desktop Settings row, setup modal and the pages_configure action
    depends_on: [p4-daemon-wiring]
    complexity: M
    touches:
      - src/privacyfence/settings_controller.py
      - src/privacyfence/settings_window_html.py
      - src/privacyfence/web/org_settings_scope.py
      - src/privacyfence/web/routes_settings.py
      - tests/unit/test_settings_controller.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_org_settings_scope.py
      - tests/integration/test_browser_smoke.py
    brief: |
      Read docs/web-pages-plan.md section D9 first; it gives names and every UI string.
      1. settings_controller.py: ALL_CONNECTORS, _CONNECTOR_LABEL_OVERRIDES, the pages branch of
         _connectors_state() (has_org, auth_label, the "pages" dict), and pages_configure(),
         copying toggle_gmail_signature (settings_controller.py:1585-1591).
      2. web/org_settings_scope.py ACTION_SCOPES and web/routes_settings.py _SENSITIVE_ACTIONS,
         as D9 says.
      3. settings_window_html.py: connectorStatus() "Not configured", the data-pages-setup link,
         renderPagesModal() with ui.pagesSetup / ui.pages.* state and its click/input/submit
         handlers, copying the Telegram modal's structure (renderTelegramModal ~line 1185, its
         handlers ~1327-1377, the ui fields ~299-324 and the onInput pattern ~1423-1443). Use the
         existing design-system classes only; no colour literals and no width @media queries
         (tests/unit/test_design_system.py).
      4. Tests:
         tests/unit/test_settings_controller.py – the connectors row set still equals
         ALL_CONNECTORS; the pages row: has_org False and auth_label "Set up…" with no pages:
         section, True and "Change…" with a valid one; its "pages" dict has public_key starting
         "ssh-ed25519 " and the six fields; pages_configure saves the six keys and calls
         refresh_connectors; an invalid host key sets error to the PagesConfigError text and
         leaves settings.yaml unchanged; test_snapshot_has_one_key_per_page still passes
         unchanged.
         tests/unit/web/test_routes_settings.py – pages_configure is in _SENSITIVE_ACTIONS and
         requires step-up like enable_connector (copy that test).
         tests/unit/web/test_org_settings_scope.py – pages_configure is local-only and never
         permitted in org (the parametrized local-only test covers it once it is in the table;
         add it explicitly if the test lists names).
         tests/unit/test_settings_window_html.py – the rendered page carries data-pages-setup,
         the "Not configured" branch, and every D9 string.
         tests/integration/test_browser_smoke.py – one case: open Settings → Connectors, click
         the Web pages row, the modal shows the public key textarea and six inputs, typing
         survives a snapshot push (copy the setup of TestSettingsPageRendering and its
         local_server_with_settings fixture, tests/integration/test_browser_smoke.py ~line 2501).
      5. Run python3 -m pytest tests/unit -q, python3 -m pytest
         tests/integration/test_browser_smoke.py -q -k "pages or Pages" (Chromium via
         PRIVACYFENCE_TEST_CHROMIUM; a SKIPPED result is not a pass), ruff check .
      No CHANGELOG line yet.
      Stop with status=blocked if: _connectors_state has no single has_org expression to extend;
      a test pins the org-only action set in a way pages_configure breaks; or the browser test
      cannot run at all in this container (report the hook's Chromium line).
    acceptance:
      - python3 -m pytest tests/unit/test_settings_controller.py tests/unit/test_settings_window_html.py tests/unit/web/test_routes_settings.py tests/unit/web/test_org_settings_scope.py -q passes
      - python3 -m pytest tests/integration/test_browser_smoke.py -q -k "pages or Pages" reports at least one passed and no skipped
      - python3 -m pytest tests/unit -q passes
      - grep -n '"pages_configure"' src/privacyfence/web/org_settings_scope.py src/privacyfence/web/routes_settings.py prints two lines

  - id: p6-docs-adrs-retire
    title: Setup guide, reference docs, ADRs 0116-0120, CHANGELOG, build.yml check, plan deletion
    depends_on: [p2-bundle-flags, p5-settings]
    complexity: M
    touches:
      - docs/web-pages-setup.md
      - docs/org-mode-setup-guide.md
      - docs/configuration-reference.md
      - docs/connecting-a-service.md
      - docs/README.md
      - README.md
      - scripts/build_site.py
      - scripts/pyinstaller_common.py
      - docs/adr/0116-web-pages-are-published-over-sftp-never-served-by-privacyfence.md
      - docs/adr/0117-privacyfence-speaks-sftp-with-asyncssh.md
      - docs/adr/0118-privacyfence-generates-its-sftp-key-and-pins-the-host-key.md
      - docs/adr/0119-a-page-link-is-a-random-id-and-each-person-manages-their-own-pages.md
      - docs/adr/0120-publishing-a-web-page-runs-the-real-pii-scan.md
      - docs/adr/README.md
      - CHANGELOG.md
      - docs/web-pages-plan.md
      - docs/web-pages-plan-manual-steps.html
    brief: |
      Read the whole of docs/web-pages-plan.md first, D11 and the ADRs section in particular.
      1. docs/web-pages-setup.md (new): for an administrator (org) and a person (desktop), in
         the style of docs/telegram-setup.md: what Web pages does; the Ubuntu 24.04 server steps
         of D11 (commands verbatim, Caddy and nginx configs); how to get PrivacyFence's public key
         (org: the startup log line and `sudo -u privacyfence-org -H
         /opt/privacyfence/venv/bin/privacyfence-app --pages-public-key`; desktop: Settings →
         Connectors → Web pages); the bundle flags (org) or the Settings modal fields (desktop);
         a test publish; limits (max_bytes default 16,000,000, anyone with the link, no listing,
         sandboxed origin, claude.ai-only artifact features); troubleshooting, one row per D5
         error message with its fix. Link ADRs 0116-0120 for the why.
      2. docs/org-mode-setup-guide.md: a Web pages row in section 5's connector table (redirect
         URI "none", flags "--pages-*", guide docs/web-pages-setup.md); the --pages-* rows in
         section 6's options table (same text as p2 put in configuration-reference.md).
         docs/configuration-reference.md: the settings.yaml `pages:` keys (D2 table, local mode).
         docs/connecting-a-service.md and README.md: add Web pages wherever connectors are
         listed, one line each. docs/README.md: list web-pages-setup.md next to
         telegram-setup.md. scripts/build_site.py: add "web-pages-setup" to CONNECTOR_GUIDES.
      3. ADRs 0116-0120 from docs/adr/README.md's template, one decision each, with the
         decisions and rejected alternatives the plan's ADRs section and D3, D4, D6, D10, D11
         give; Status "Accepted — <today's date>"; Verification names the files and tests that
         enforce each (pages_client.py and test_pages_client.py for 0117/0118, pages_index.py
         and test_pages_connector.py for 0119, gate.py and test_gate.py for 0120,
         docs/web-pages-setup.md for 0116); Related links ADR 0028, 0086, 0093, 0097, 0102 and
         0009 where relevant, never the plan. Add five rows to the README index. If a parallel
         branch has taken any of 0116-0120 on main, renumber and fix every reference.
      4. CHANGELOG.md, under [Unreleased], a new "### Added" subsection above "### Changed" with
         one bullet in the file's style: **Web pages.** An AI client can publish a single-file
         HTML page to a web server you run and get back a link anyone can open
         (`pages_publish_html`, `pages_list_pages`, `pages_unpublish_page`); every publish is
         approved on the usual card. PrivacyFence copies the page over SFTP with its own key;
         see [Web pages setup](docs/web-pages-setup.md). Desktop installs set it up under
         Settings → Connectors, organization servers with the `--pages-*` bundle options.
      5. Dispatch build.yml against feature/web-pages (GitHub MCP actions_run_trigger) and wait
         for it. If a platform job fails importing asyncssh in the bundle, change the
         "asyncssh" entry in scripts/pyinstaller_common.py to the package's collected
         submodules (PyInstaller.utils.hooks.collect_submodules("asyncssh"), spread into
         HIDDEN_IMPORTS), push, and dispatch again. Put the green run's URL in your final report.
      6. Delete docs/web-pages-plan.md and docs/web-pages-plan-manual-steps.html. git grep -n
         "web-pages-plan" must print nothing.
      7. Run python3 -m pytest tests/unit -q, ruff check ., and the website tests
         python3 -m pytest tests/unit/test_build_site.py tests/unit/test_website_connectors_page.py -q.
      Stop with status=blocked if build.yml fails for a reason other than a missing asyncssh
      module in the bundle, or fails again after the collect_submodules change.
    acceptance:
      - ls docs/adr/0116-*.md docs/adr/0117-*.md docs/adr/0118-*.md docs/adr/0119-*.md docs/adr/0120-*.md lists five files, and grep -c 'Accepted' on each prints at least 1
      - grep -c '011[6-9]\|0120' docs/adr/README.md prints at least 5
      - git grep -n "web-pages-plan" prints nothing
      - grep -n 'Web pages' CHANGELOG.md prints a line above the first '## [5.' heading
      - python3 -m pytest tests/unit -q passes
      - the dispatched build.yml run on feature/web-pages is green
```
