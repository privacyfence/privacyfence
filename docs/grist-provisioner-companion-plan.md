# Grist provisioner — companion plan

**Where this belongs.** This plan is for a new repository, `privacyfence/grist-provisioner` (the user chose a new repository). It was written on PrivacyFence's `plan/grist-auto-provisioning` branch because that repository does not exist yet. Once it exists and has its profile (`manual_before` below), copy this file to `docs/grist-provisioner-plan.md` on a `plan/grist-provisioner` branch there, and run `/implement` on that URL. PrivacyFence's own plan, `docs/grist-auto-provisioning-plan.md`, defines the wire contract; this plan implements it unchanged.

## Goal

A small service that runs next to a self-hosted, free grist-core, in Grist's network zone, and gives PrivacyFence each person's Grist API key, behind a scoped provisioning key. grist-core has no API that creates a key for another user, so the provisioner reads and writes `users.api_key` in Grist's home database (SQLite) directly.

- **A person with no key** gets a new *managed* key. It expires 90 days after it was last issued, and a release deletes it, but only while it is still that person's current key.
- **A person who already has a key** has it *adopted*: handed over as it is, and never deleted by the provisioner.
- **The install admin** is never provisioned.
- **People without a Grist account** are not created; they get 404 until Grist creates the account itself.

It only answers; it never connects to PrivacyFence.

## Current state

There is no code yet. The facts it builds on, checked at gristlabs/grist-core `23043a18` (version 1.7.21):

- **`users` table** (`app/gen-server/entity/User.ts`).
  - Columns `id` (integer primary key), `name`, `api_key` (nullable text, plaintext, one per user, `:25-28`), `disabled_at` (nullable datetime), and `type` (`"login"` or `"service"`, `:87`).
  - Grist makes a key as `crypto.randomBytes(20).toString("hex")`, 40 hex characters (`app/gen-server/lib/homedb/UsersManager.ts:37-39`).
  - `createApiKey` sets a key only when there is none, unless asked to force a new one (`:1018-1031`).
- **`logins` table** (`app/gen-server/entity/Login.ts`). Columns `id`, `email` (lower-case, indexed, `app/common/emails.ts:40-43`), `display_email` and `user_id`.
- **API key checks.** Every API request looks the key up in the database, with no cache (`app/server/lib/Authorizer.ts:187-217`). A written key works at once and a cleared one fails at once. Disabled users get 403.
- **Install admin.** `GRIST_ADMIN_EMAIL`, else `GRIST_DEFAULT_EMAIL` (`app/server/lib/InstallAdmin.ts:102-116`). Special users: `anon@getgrist.com`, `everyone@getgrist.com`, `thumbnail@getgrist.com` (`app/common/UserAPI.ts:47-53`) and `GRIST_SUPPORT_EMAIL` (default `support@getgrist.com`).
- **Docker defaults.** The home database is `/persist/home.sqlite3` (`Dockerfile:192`), and Grist listens on 8484.
- **API used by the real-Grist test.** `GRIST_BOOT_KEY` lets an `x-boot-key` header act as the install admin (`Authorizer.ts`, the boot-key branch). The test uses `POST /api/orgs`, `PATCH /api/orgs/:oid/access` (sharing creates the user), `POST /api/users/:userId/disable`, `GET /api/profile/user` and `GET /status`.

## Design

### Language, packaging, layout

- Python 3.12, packaged as `grist-provisioner` (module `grist_provisioner`), Apache-2.0.
- Dependencies: `starlette` and `uvicorn` only; SQLite through the standard library.
- Shipped as a Docker image, `ghcr.io/privacyfence/grist-provisioner:<version>`, built by GitHub Actions on a `v*` tag.

```
src/grist_provisioner/{__init__,config,griststore,state,service,app,cli}.py
tests/unit/...      tests/real_grist/test_real_grist.py   tests/fixtures/grist_home_schema.sql
.github/workflows/{tests,real-grist,release}.yml   Dockerfile   README.md   CHANGELOG.md
```

### Configuration (`config.py`)

`Settings.from_env(environ) -> Settings`, a frozen dataclass:

| Variable | Default | Meaning |
|---|---|---|
| `GRIST_HOME_DB` | `/persist/home.sqlite3` | Grist's home database |
| `PROVISIONER_STATE_DB` | `/data/provisioner.sqlite3` | The provisioner's own database |
| `PROVISIONER_HOST` / `PROVISIONER_PORT` | `0.0.0.0` / `8485` | Listen address |
| `GRIST_ADMIN_EMAIL`, `GRIST_DEFAULT_EMAIL` | empty | The same values as Grist's. Both addresses are refused. |
| `GRIST_SUPPORT_EMAIL` | `support@getgrist.com` | Refused, like the three special users |
| `PROVISIONER_ALLOWED_DOMAINS` | empty (any) | Comma-separated email domains, compared lower-case |

A bad port raises `ConfigError("PROVISIONER_PORT must be a number from 1 to 65535")`. When both `GRIST_ADMIN_EMAIL` and `GRIST_DEFAULT_EMAIL` are empty it raises `ConfigError("Set GRIST_ADMIN_EMAIL or GRIST_DEFAULT_EMAIL to the same value as Grist's, so the install admin is never provisioned.")`; `serve` and `check` print it and exit 2. Constants: `KEY_DAYS = 90`, `EXPIRE_INTERVAL_SECONDS = 3600`.

### Grist's home database (`griststore.py`)

```python
@dataclass(frozen=True)
class GristUser:
    id: int
    email: str
    disabled: bool
    api_key: str | None = field(repr=False)
    type: str

class SchemaError(Exception): ...

class GristStore:
    def __init__(self, path: str) -> None        # sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    def check_schema(self) -> None
    def find_by_email(self, email: str) -> GristUser | None
    def find_by_id(self, user_id: int) -> GristUser | None
    def set_key_if_empty(self, user_id: int, key: str) -> bool
    def clear_key_if(self, user_id: int, key: str) -> bool
```

- **`check_schema`** reads `PRAGMA table_info` for both tables. When `users` lacks `id`, `api_key`, `disabled_at` or `type`, or `logins` lacks `email` or `user_id`, it raises `SchemaError("Grist's home database does not have the users and logins columns this provisioner expects (it supports grist-core 1.7). Check GRIST_HOME_DB, or update the provisioner.")`.
- **`find_by_email`** lower-cases the address and runs `SELECT u.id, l.email, u.disabled_at, u.api_key, u.type FROM logins l JOIN users u ON u.id = l.user_id WHERE l.email = ?`. `find_by_id` is the same query on `u.id` with `LIMIT 1`.
- **`set_key_if_empty`** runs `UPDATE users SET api_key = ? WHERE id = ? AND api_key IS NULL` inside `BEGIN IMMEDIATE … COMMIT`, and returns whether one row changed. It treats an empty-string key as no key: `api_key IS NULL OR api_key = ''`.
- **`clear_key_if`** runs `UPDATE users SET api_key = NULL WHERE id = ? AND api_key = ?` the same way, and returns whether one row changed.
- The store never writes any other column or table, and never logs a key.

### The provisioner's own database (`state.py`)

`StateStore(path)` opens `sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)` and creates these tables when missing (`schema_version` = 1):

```sql
CREATE TABLE provisioning_keys (id INTEGER PRIMARY KEY, name TEXT NOT NULL, key_sha256 TEXT NOT NULL UNIQUE,
                                created_at INTEGER NOT NULL, revoked_at INTEGER);
CREATE TABLE links (external_id TEXT PRIMARY KEY, grist_user_id INTEGER NOT NULL UNIQUE, created_at INTEGER NOT NULL);
CREATE TABLE managed_keys (grist_user_id INTEGER PRIMARY KEY, key_sha256 TEXT NOT NULL, expires_at INTEGER NOT NULL);
```

Methods:

| Method | Notes |
|---|---|
| `add_key(name, now) -> tuple[int, str]` | The key is `"gristprov_" + secrets.token_urlsafe(32)`; only its SHA-256 is stored. |
| `list_keys() -> list[dict]` | |
| `revoke_key(id, now) -> bool` | |
| `key_valid(presented: str) -> bool` | Looks up the SHA-256 among keys whose `revoked_at` is null. |
| `link_for(external_id)` and `link_of_user(user_id)` | |
| `add_link(external_id, user_id, now)` and `drop_link(external_id)` | |
| `managed(user_id)`, `set_managed(user_id, sha, expires_at)`, `drop_managed(user_id)` | |
| `expired_managed(now) -> list[tuple[int, str]]` | |
| `count_links() -> int` | |

No email and no Grist key is ever stored; `sha256(x)` is `hashlib.sha256(x.encode()).hexdigest()`.

### The service (`service.py`)

`ProvisionError(status: int, message: str)`. `Provisioner(store, state, settings, clock=time.time)`, with `key_id(key) = sha256(key)[:12]`. The provisioner holds one `threading.Lock`; `upsert`, `issue_key`, `release` and `expire_once` each run entirely under it, so the HTTP handlers and the expiry thread never interleave (an expiry can never clear a key that `issue_key` just renewed). The stores are shared between threads only through these methods. The first five steps below are shared by `upsert` and `issue_key`:

**`upsert(external_id, email) -> dict`**

1. When `external_id` does not match `^[A-Za-z0-9._@-]{1,200}$` → 400 `bad external id`. When `email` is not a string of 3-320 characters with exactly one `@` → 400 `bad email`.
2. When `PROVISIONER_ALLOWED_DOMAINS` is set and the domain is not in it → 403 `account email domain is not allowed`.
3. `user = store.find_by_email(email)`. When it is `None`, or `user.type != "login"` → 404 `no Grist account for this email`.
4. When `user.email` is in the refused set (admin, default, support and the three special addresses, lower-case) → 409 `account is a Grist admin; connect it by hand`.
5. When `external_id` is linked to a different user, or `user.id` is linked to a different `external_id` → 409 `account is linked to another external id or email`. Otherwise add the link if it is missing.
6. `managed = state.managed(user.id)` exists and its sha equals `sha256(user.api_key)`.
7. Return `{"external_id", "user_id", "disabled", "has_key": bool(user.api_key), "managed", "key_id": key_id(user.api_key) if user.api_key else "", "expires_at": the managed row's expires_at when `managed`, else 0}`.

**`issue_key(external_id) -> dict`**

1. No link → 404 `not found`.
2. `user = store.find_by_id(...)`. When it is `None`, drop the link → 404 `not found`.
3. Admin → 409, as in `upsert` step 4. `user.disabled` → 403 `account disabled`.
4. When `user.api_key` is empty: create `secrets.token_hex(20)` and call `set_key_if_empty`. If that returns False (Grist set a key at the same moment), read the user again and go to step 6. Otherwise `set_managed(sha, now + KEY_DAYS*86400)` and return `{"key", "managed": true, "key_id", "expires_at"}`.
5. When a managed row matches the current key: push its `expires_at` to `now + KEY_DAYS*86400` and return the same key with `managed: true`.
6. Otherwise adopt: `drop_managed(user.id)` and return the key with `managed: false` and `expires_at: 0`.

**`release(external_id) -> dict`**

1. No link → 404 `not found`.
2. `user = store.find_by_id(...)`. `revoked = False`. When `user` is not `None` and a managed row matches the current key, `revoked = store.clear_key_if(user.id, user.api_key)`.
3. `drop_managed` and `drop_link`, then return `{"released": True, "revoked": revoked}`.

**`expire_once() -> int`.** For each `expired_managed(now)` row: when `find_by_id` returns a user whose current key still matches, call `clear_key_if`; then `drop_managed` (also when the user is gone). Return the number of keys it cleared.

**Audit.** Every `upsert` that adds a link, and every `issue_key`, `release` and expiry, logs one JSON line to stdout: `{"ts", "event": "link" | "issue" | "adopt" | "release" | "expire", "external_id", "grist_user_id", "revoked"?}`. Never an email or a key.

### HTTP (`app.py`)

`create_app(provisioner, state) -> Starlette`, with exactly the routes of the contract in PrivacyFence's plan ("The provisioner's wire contract"):

| Route | Answer |
|---|---|
| `GET /api/provisioning/self` | `{"name": "grist-provisioner", "accounts": state.count_links()}` |
| `PUT /api/provisioning/accounts/{external_id}` | Body `{"email"}`; 200 |
| `POST /api/provisioning/accounts/{external_id}/key` | 201 |
| `DELETE /api/provisioning/accounts/{external_id}` | 200 |
| `GET /healthz` | 200 `ok`, with no authentication |

- Every `/api/provisioning/*` request needs `Authorization: Bearer <key>` with `state.key_valid` true, else 401 `{"error": "unauthorized"}`.
- A body that is not a JSON object → 400 `{"error": "bad request"}`. A `ProvisionError` becomes `{"error": message}` with its status.
- An unknown route answers Starlette's default plain-text 404; PrivacyFence relies on it.
- Handlers are plain `def` functions (Starlette runs them in its thread pool); serialisation is the `Provisioner`'s lock, not the app's.

### CLI (`cli.py`, console script `grist-provisioner`)

| Command | What it does |
|---|---|
| `serve` | Runs `check_schema` (exit 2 with the `SchemaError` message), then starts the expiry thread running `_expiry_loop(provisioner, stop_event)`: `expire_once()` guarded by `except Exception` → stderr `expiry pass failed (will retry): {exc}`, then `stop_event.wait(EXPIRE_INTERVAL_SECONDS)`, until the event is set (it is set when `uvicorn.run` returns) and `uvicorn.run(app, host, port)`. |
| `create-key --name NAME` | Prints the key once, on its own line, and to stderr `Store this key in PrivacyFence's environment; it is not shown again.` |
| `list-keys` | Lists id, name, created and revoked. |
| `revoke-key ID` | Revokes the key. |
| `check` | Checks the schema and prints `ok: N Grist users, M linked` or the `SchemaError`, exit 2. |

### Deployment (README)

The provisioner runs as one more service in Grist's compose file:

- It mounts Grist's `/persist` volume read-write and its own `/data` volume.
- It runs as the same uid and gid as Grist's `grist` user, which owns `/persist` (Grist's entrypoint chowns it, `sandbox/docker_entrypoint.sh`). The README shows `user: "<uid>:<gid>"` in compose, with the values from `docker compose exec grist id grist`. Without it the provisioner cannot write `home.sqlite3` or its journal.
- It runs on the same host as Grist, never on a network file system: SQLite locking across containers needs a local volume.
- Its port is reachable only from the PrivacyFence server, behind the organization's TLS reverse proxy. PrivacyFence requires https.
- Back up `/data` with `/persist`. Losing `/data` loses which keys are managed; existing keys are then treated as adopted and never expire.

### Rejected alternatives

- **TypeScript or a grist-core fork.** grist-core has no server plug-in hook for routes, and a fork must follow every Grist release.
- **Writing through Grist's own API with forward auth.** It replaces Grist's Google sign-in.
- **Creating users through SCIM.** The user declined it.
- **Rotating or replacing a key a person already has.** Grist has one key per person, and replacing it would break their scripts; it is adopted instead.
- **Keeping Grist keys or emails in `/data`.** Only SHA-256 hashes are kept.

## Manual steps

On the shared step-by-step page linked from PrivacyFence's plan:

- **`manual_before`:** create the repository, with a README, `CHANGELOG.md` and the profile below, and enable the devflow plugins for it.
- **`manual_after`:** deploy next to the real Grist, create a key, and check it from PrivacyFence (PrivacyFence plan `ma1`-`ma5`).

The profile to commit as `.claude/toolkit.yaml` (it validates with devflow's `validate_profile.py`):

```yaml
project:
  name: Grist provisioner
docs:
  must_read:
    - README.md
  plan_dir: docs
  changelog:
    file: CHANGELOG.md
    section: "## [Unreleased]"
git:
  main_branch: main
  branch_pattern: "<type>/<kebab-case>"
  branch_types: [feature, fix, chore]
  merge_style: merge-commit
verify:
  fast:
    - ruff check .
    - python3 -m pytest tests -q -m "not real_grist"
  dod:
    - python3 -m pytest tests -q -m "not real_grist" --cov=grist_provisioner --cov-branch --cov-report=term-missing --cov-fail-under=100
    - ruff check .
    - python3 -m mypy src
    - bandit -q -r src
ci:
  dispatchable:
    - workflow: real-grist.yml
      use_when: The provisioner against a real grist-core container (the cloud session has no Docker)
      notes: It runs by itself on every push to feature/** and fix/** (read that run). workflow_dispatch with input `grist_image` works only once the workflow is on main.
stacks:
  - python
```

## Risks and open questions

- **The real Grist schema differs from `tests/fixtures/grist_home_schema.sql`.** `real-grist.yml` fails at `check_schema` or in a query. Fix the fixture to match the real schema; if the columns this plan relies on are missing, stop with `status=blocked`.
- **A Grist API call in the real-Grist setup fails** (org creation, sharing, disable). Stop with `status=blocked` and quote the response; do not replace the setup with direct database writes, except for the one "own key" case the brief names.
- **SQLite busy errors under load.** The `timeout=10` connection and short `BEGIN IMMEDIATE` transactions should cover it. If `real-grist.yml` shows `database is locked`, stop with `status=blocked`.

## Implementation manifest

```yaml
plan_slug: grist-provisioner
feature_branch: feature/grist-provisioner
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/5RVxf9Zhi94bU37gLqBcLS
manual_before:
  - id: mb1-repo
    title: Create privacyfence/grist-provisioner with README.md, CHANGELOG.md and .claude/toolkit.yaml from this plan, and enable the devflow plugins
    why: /implement reads the profile and must-read docs before phase cp1; without them it stops.
    done_when: .claude/toolkit.yaml on main passes devflow's validate_profile.py, and a Claude Code session on the repo lists /implement
manual_after:
  - id: ma1-deploy
    title: Deploy the image next to the organization's Grist and create a provisioning key (PrivacyFence plan ma1)
    why: Only the real deployment shows the volume, network and TLS set-up works.
verify_after_merge:
  - python3 -m pytest tests -q -m "not real_grist"
final_checks:
  - docs/grist-provisioner-plan.md is deleted and nothing links to it
  - CHANGELOG.md has entries under "## [Unreleased]" and no version heading
  - the real-grist.yml run for the feature branch concluded success, and its URL is in the PR description
phases:
  - id: cp1-scaffold
    title: Package skeleton, CI and Dockerfile
    depends_on: []
    complexity: S
    touches:
      - pyproject.toml
      - src/grist_provisioner/__init__.py
      - tests/unit/test_package.py
      - .github/workflows/tests.yml
      - Dockerfile
      - .dockerignore
      - CHANGELOG.md
    brief: |
      Read README.md and this plan's Design section "Language, packaging, layout".
      1. pyproject.toml:
         - Project name grist-provisioner, requires-python >=3.12, Apache-2.0, dependencies starlette and uvicorn, console script grist-provisioner = grist_provisioner.cli:main.
         - Extras test = [pytest, pytest-cov, httpx] and dev = [ruff, mypy, bandit].
         - setuptools_scm for the version, with fallback_version = "0.0.0.dev0" so a build without .git (the Docker build) still works.
         - [tool.pytest.ini_options] declares the marker real_grist.
         - ruff line-length 120; mypy strict for src.
      2. src/grist_provisioner/__init__.py with __version__ from importlib.metadata.version("grist-provisioner"), falling back to "0.0.0.dev0" on PackageNotFoundError. tests/unit/test_package.py: test_version_is_a_string, and test_version_falls_back_when_not_installed (monkeypatch importlib.metadata.version to raise PackageNotFoundError, then importlib.reload the package).
      3. .github/workflows/tests.yml on push and pull_request: Python 3.12; pip install -e ".[test,dev]"; then ruff check ., mypy src, bandit -q -r src, and pytest tests -q -m "not real_grist" --cov=grist_provisioner --cov-branch --cov-fail-under=100. Pin actions by full commit SHA with a version comment.
      4. Dockerfile: python:3.12-slim; ARG SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0.dev0 passed into the pip install; a user `provisioner` with no fixed uid, and USER provisioner (deployments override it with compose `user:` to match Grist's `grist` user, see Deployment); pip install .; VOLUME /data; EXPOSE 8485; ENTRYPOINT ["grist-provisioner"]; CMD ["serve"]. A .dockerignore leaving out .git, tests and .venv.
      5. CHANGELOG.md: add "### Added" under "## [Unreleased]" with "- **Grist provisioner.** Gives PrivacyFence each person's Grist API key on a self-hosted grist-core, behind a scoped provisioning key."
    acceptance:
      - python3 -m pytest tests -q -m "not real_grist" passes
      - ruff check . passes
      - grep -n "real_grist" pyproject.toml matches

  - id: cp2-stores
    title: Settings, Grist home database store and the provisioner's state store
    depends_on: [cp1-scaffold]
    complexity: M
    touches:
      - src/grist_provisioner/config.py
      - src/grist_provisioner/griststore.py
      - src/grist_provisioner/state.py
      - tests/fixtures/grist_home_schema.sql
      - tests/unit/test_config.py
      - tests/unit/test_griststore.py
      - tests/unit/test_state.py
    brief: |
      Read this plan's "Current state" and the Design sections "Configuration", "Grist's home database" and "The provisioner's own database".
      1. Write config.py, griststore.py and state.py exactly as Design says: signatures, SQL, messages.
      2. Write tests/fixtures/grist_home_schema.sql:
         - CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, api_key TEXT, disabled_at DATETIME, type TEXT NOT NULL DEFAULT 'login');
         - CREATE TABLE logins (id INTEGER PRIMARY KEY, email TEXT NOT NULL, display_email TEXT NOT NULL, user_id INTEGER NOT NULL);
         - an index on logins(email).
      3. Tests. Build each database in tmp_path from the fixture.
         - test_config.py: defaults; every variable read; domains split, trimmed and lower-cased; a bad port.
         - test_griststore.py:
           - check_schema passes, and raises SchemaError when a column is missing (one case per column);
           - find_by_email is case-insensitive and returns None when missing;
           - find_by_id;
           - set_key_if_empty is True on NULL, True on '', and False when a key exists;
           - clear_key_if is True on match and False on a different key;
           - repr(GristUser) has no key.
         - test_state.py:
           - add_key returns a gristprov_ key, and only its SHA-256 is in the database (read the table raw);
           - key_valid is true for a valid key and false for revoked and unknown keys;
           - links are unique both ways (an IntegrityError on a second link of the same user);
           - the managed rows' round trip;
           - expired_managed;
           - count_links.
      4. Run ruff check ., python3 -m mypy src and the acceptance commands.
    acceptance:
      - python3 -m pytest tests/unit/test_config.py tests/unit/test_griststore.py tests/unit/test_state.py -q --cov=grist_provisioner.config --cov=grist_provisioner.griststore --cov=grist_provisioner.state --cov-branch --cov-report=term-missing passes and reports 100% for the three modules
      - python3 -m mypy src passes
      - ruff check . passes

  - id: cp3-service-api-cli
    title: Provisioner service, HTTP API, CLI and expiry
    depends_on: [cp2-stores]
    complexity: M
    touches:
      - src/grist_provisioner/service.py
      - src/grist_provisioner/app.py
      - src/grist_provisioner/cli.py
      - tests/unit/test_service.py
      - tests/unit/test_app.py
      - tests/unit/test_cli.py
    brief: |
      Read this plan's Design sections "The service", "HTTP" and "CLI", and the contract table in PrivacyFence's plan ("The provisioner's wire contract"). Field names, statuses and error strings are verbatim from there.
      1. Write service.py, app.py and cli.py exactly as Design says.
      2. Tests (tmp_path databases from the fixture; a fixed clock):
         - test_service.py, one test per numbered step and outcome:
           - upsert: bad id; bad email; domain refused; no account; a service user; admin, default and support addresses; linked to another id; linked to another user; a new link; managed/key_id/has_key reporting.
           - issue_key: no link; a missing user drops the link; admin; disabled.
           - issue_key outcomes: a new managed key (40 hex characters, stored only as a hash); a managed key renewed with the same key and a new expiry; an own key adopted (managed false); a lost race leading to adopt.
           - release: no link; revokes a matching managed key; keeps an adopted key (revoked false); keeps a key the person replaced.
           - expire_once: clears only expired, matching keys.
           - audit lines carry no email or key (capsys).
         - test_app.py with starlette.testclient.TestClient:
           - 401 without, with a bad and with a revoked key;
           - each route's status and JSON shape;
           - a non-object body gives 400;
           - an unknown route gives a plain-text 404;
           - /healthz needs no key.
         - test_cli.py:
           - create-key prints the key once, plus the stderr line;
           - list-keys;
           - revoke-key;
           - check prints ok, and exits 2 on a bad schema;
           - serve exits 2 on a bad schema (monkeypatch uvicorn.run);
           - serve starts the expiry thread and calls uvicorn.run with host and port;
           - _expiry_loop runs expire_once, logs a raised exception and keeps going, and returns once the stop event is set (drive it with an Event set from the fake);
           - serve and check exit 2 with the ConfigError message when both admin emails are empty.
      3. Run ruff check ., python3 -m mypy src, bandit -q -r src and the acceptance commands.
    acceptance:
      - python3 -m pytest tests -q -m "not real_grist" --cov=grist_provisioner --cov-branch --cov-report=term-missing --cov-fail-under=100 passes
      - python3 -m mypy src passes
      - bandit -q -r src passes

  - id: cp4-real-grist
    title: End-to-end test against a real grist-core container
    depends_on: [cp3-service-api-cli]
    complexity: M
    touches:
      - tests/real_grist/test_real_grist.py
      - .github/workflows/real-grist.yml
      - Dockerfile
    brief: |
      Read this plan's "Current state" (the API used by the real-Grist test) and "Risks". The cloud session has no Docker: write the test and the workflow, push, and read the run that your push starts (GitHub accepts workflow_dispatch only for workflows already on main, so the first runs come from the push trigger).
      1. .github/workflows/real-grist.yml:
         - Triggers: push to branches feature/** and fix/**; pull_request; workflow_dispatch with input grist_image (default gristlabs/grist:1.7.21); and a weekly schedule that runs with gristlabs/grist:latest. Every trigger but the schedule and a dispatch with another input uses gristlabs/grist:1.7.21.
         - Job: start the image with docker run, with these settings:
           - a host directory mounted at /persist;
           - GRIST_DEFAULT_EMAIL=admin@example.com;
           - GRIST_BOOT_KEY=a random value from the job;
           - GRIST_SINGLE_ORG unset;
           - port 8484.
         - Wait for GET /status. Then pip install -e ".[test]" and run pytest tests/real_grist -q -m real_grist, with env GRIST_URL, GRIST_BOOT_KEY and GRIST_HOME_DB=<host dir>/home.sqlite3.
         - Start the Grist container with `--user "$(id -u):$(id -g)"`, so the database belongs to the runner and the test can open it.
         - Also build the provisioner image (docker build with --build-arg SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0.dev0) and, after the tests, run `docker run --rm --user "$(id -u):$(id -g)" -v <host dir>:/persist -e GRIST_DEFAULT_EMAIL=admin@example.com <image> check`, expecting exit 0. That proves the released image works before any release.
      2. tests/real_grist/test_real_grist.py, marked real_grist and skipped unless GRIST_URL is set:
         a. With x-boot-key, POST /api/orgs {"name": "QA", "domain": "qa"}, then PATCH /api/orgs/<id>/access with {"delta": {"users": {"alice@example.com": "editors", "bob@example.com": "editors", "carol@example.com": "editors"}}}.
         b. Build GristStore and StateStore and a Provisioner. check_schema passes.
         c. alice: upsert, then issue_key gives a managed key. GET /api/profile/user with that Bearer key returns alice@example.com. release says revoked true, and the same call then answers 401.
         d. bob: write an own key with store.set_key_if_empty (the one allowed direct write, standing in for bob's own key). issue_key returns that key with managed false. release says revoked false, and the key still works.
         e. carol: issue_key, then POST /api/users/<carol id>/disable with the boot key. issue_key now raises 403 "account disabled".
         f. admin@example.com: upsert raises 409.
         g. nobody@example.com: upsert raises 404.
         h. alice again: upsert, then issue_key, then set its managed row's expires_at into the past with the state store. expire_once() returns 1, and the key answers 401.
      3. Push, wait for the real-grist.yml run that the push starts, and put the run URL in the phase result. If it fails in Grist setup calls, see Risks.
    acceptance:
      - python3 -m pytest tests -q -m "not real_grist" passes (the real-Grist test is skipped without GRIST_URL)
      - the real-grist.yml run on this branch with gristlabs/grist:1.7.21 concludes success
      - grep -n "schedule" .github/workflows/real-grist.yml matches

  - id: cp5-docs-release
    title: README, release workflow, retire the plan
    depends_on: [cp4-real-grist]
    complexity: S
    touches:
      - README.md
      - .github/workflows/release.yml
      - CHANGELOG.md
      - docs/grist-provisioner-plan.md
    brief: |
      Read this plan's Design sections "Deployment" and "Rejected alternatives".
      1. README.md: what it is, and that it never calls PrivacyFence; requirements (grist-core 1.7 with SQLite, same host); a compose example next to Grist (the persist volume, the /data volume, GRIST_ADMIN_EMAIL and GRIST_DEFAULT_EMAIL copied from Grist, the port behind TLS); create-key and where the key goes (PrivacyFence's PRIVACYFENCE_GRIST_PROVISIONING_KEY, linking PrivacyFence's docs/grist-setup.md); managed and adopted keys; expiry; backup; every error message and CLI message with its fix; "Design decisions" with the rejected alternatives.
      2. .github/workflows/release.yml: on push of tags v*, build and push ghcr.io/privacyfence/grist-provisioner:<tag without v> and :latest with docker/build-push-action, permissions packages: write, actions pinned by SHA.
      3. CHANGELOG.md: keep the entry under "## [Unreleased]"; no version heading.
      4. git rm docs/grist-provisioner-plan.md, and grep that nothing links to it.
    acceptance:
      - test ! -e docs/grist-provisioner-plan.md
      - grep -n "PRIVACYFENCE_GRIST_PROVISIONING_KEY" README.md matches
      - grep -n "packages: write" .github/workflows/release.yml matches
      - python3 -m pytest tests -q -m "not real_grist" passes
```
