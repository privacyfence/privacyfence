# Plan: the four low-severity findings from the 2026-10-14 repository review

## Goal

Fix four findings the whole-repository review at `3766d50` (review document:
[`docs/repo-2026-10-14-review.md` at `0506f23`](https://github.com/privacyfence/privacyfence/blob/0506f2311a3d04af12e8d298f253965be02eaf4d/docs/repo-2026-10-14-review.md))
decided to fix. They are independent of each other; one feature branch and one PR carry all four.

| Finding | Issue | What changes for the user |
|---|---|---|
| SEC-A5 | [#923](https://github.com/privacyfence/privacyfence/issues/923) | The Jira "if I'm reporter / assignee" and Confluence "if I'm the author" auto-accept rules match the signed-in person's Atlassian `accountId`, never the account email inside a display name. Nobody can make issues auto-readable by naming themselves "Mallory (alice@corp.com)", and the Confluence rule, dead until now, works. |
| SEC-A7 | [#924](https://github.com/privacyfence/privacyfence/issues/924) | A signed organization bundle carries a `serial`; an install refuses a bundle whose serial is lower than the highest it has accepted. A deliberate rollback is an administrator deleting one pin file. |
| TST-C14 | [#925](https://github.com/privacyfence/privacyfence/issues/925) | The `platform-windows` CI job gates Windows-only coverage with its own floors; four `_resolve_path` tests that were skipped on Windows run everywhere. |
| WEB-9 | [#926](https://github.com/privacyfence/privacyfence/issues/926) | privacyfence.eu sends HSTS, a CSP, `nosniff`, a Referrer-Policy and frame protection (set by Cloudflare), with the expected headers kept in the repository, tested against the built site in a browser and re-checked after every deploy. |

The feature PR body closes #923, #924, #925 and #926.

## Current state

### SEC-A5 (Atlassian identity)

- `src/privacyfence/policy/scopes.py:322-331` - `_i_am_reporter_matches` / `_i_am_assignee_matches` test
  `ctx.my_email.lower() in reporter.lower()`, where `reporter` is `JiraIssue.reporter`, which
  `JiraClient._parse_issue` (`src/privacyfence/jira_client.py:762-763`) fills from the user-editable
  `displayName`.
- `scopes.py:143-148` - `_i_am_author_matches` does the same against `ConfluencePage.author`, which for the v2
  API is the opaque `authorId` (`confluence_client.py:726-749`), so the rule never matches.
- The signed-in person is known only as `account_email` in the Atlassian token record
  (`atlassian_oauth.py:197`, written by `resolve_resource_and_save`). `daemon_main.py:1554,1577,1601` copy it to
  `connector.my_email`. The same record is re-saved with an explicit field list by `JiraClient._try_refresh`
  (`jira_client.py:278-285`), `ConfluenceClient` (`confluence_client.py:~292-299`) and `AssetsClient`
  (`assets_client.py:~333-340`); any field not in those lists is lost at the next token refresh.
- `daemon_main.py:1534`: `atlassian_config = {**atlassian_org, **atlassian_token}`, so any field in the token
  record reaches the clients' `_config`.
- `JiraClient.check_connection` (`jira_client.py:301-309`) already calls `/myself` through the refresh-and-retry
  wrapper; Jira's `/myself` answers with `accountId`.
- `ReviewContext` (`src/privacyfence/auto_accept.py:~394-405`) carries `my_email`; `gate.gated_call`
  (`gate.py:~988`) takes `my_email` and builds the context; every Jira and Confluence `gated_call` passes
  `my_email=self.my_email` (`connectors/jira.py`: 7 sites; `connectors/confluence.py`: 5 sites).
- Both selectors are `ResolvesFrom.FETCHED` (`scopes.py:430-433, 514-521`), so `preflight` never evaluates them and
  needs no change.

### SEC-A7 (bundle rollback)

- `org_bundle_signing.verify_and_maybe_pin` (`org_bundle_signing.py:203-250`) checks only the signature
  against the pinned key. Nothing compares a version, time or serial with what is installed. ADR 0016 covers
  tampering, key replacement and downgrade to unsigned, not replay of an older validly signed bundle.
- Callers: `daemon_main.load_org_config` (`daemon_main.py:~455-483`, every daemon start) and
  `SettingsController.install_org_config_bytes` (`settings_controller.py:1092-1115`, which writes the bundle
  with `atomic_write_json` after verification).
- `scripts/build_org_bundle.py` has its own copy of the signing code (`_canonical_payload_bytes`,
  `_sign_bundle`, lines 116-188); it signs every field except `signature`, so a new field is signed
  automatically. `tests/unit/test_build_org_bundle.py` cross-checks the two copies.

### TST-C14 (Windows coverage)

- `.github/workflows/tests.yml:261-292` (`platform-windows`) runs `pytest -v --cov=src/privacyfence
  --cov-report=term-missing`: no `--cov-branch`, no JSON, no floor check, no artifact.
- `scripts/check_coverage_floor.py` gates only the Linux run. `MODULE_FLOORS` carries `windows_acl.py` (83.0) and
  `web/control_channel.py` (77.0) with comments saying the Windows halves are "exercised for real by the
  platform-windows job", which measures nothing that gates. `windows_service.py` has no floor.
- `tests/unit/test_daemon_main.py` skips four tests on Windows with a reason that says "not otherwise tracked"
  or "a known gap": `TestResolvePath` (two tests, lines ~123-134), `TestResolveAuthorityPath::
  test_absolute_path_is_returned_unchanged` (~155) and the Telegram `test_built_when_creds_and_session_present`
  (~1213). The cause for the first three is the test, not the code: `"/etc/hosts"` is not absolute on Windows
  (no drive), so `_resolve_path` joins it onto `PROJECT_ROOT`, and the relative tests compare against a
  POSIX-joined string.

### WEB-9 (security headers)

- `curl -D - https://privacyfence.eu/` returns no HSTS, CSP, `X-Content-Type-Options`, `Referrer-Policy` or
  frame protection. GitHub Pages cannot hold headers in the repository; ADR 0053 states "there is no per-path
  header or redirect file" and that Cloudflare proxies the domain.
- Measured on the site as `scripts/build_site.py` builds it (docs extra installed), what a CSP has to allow:
  - Scripts: every hand-written page loads only `/site.js` (and `/stats.js`, `/download/download.js`,
    `/releases/releases.js`) plus a `type="application/ld+json"` block (data, not executed). `site.js` injects
    `https://www.googletagmanager.com/gtag/js` only after consent. **No inline executable script and no `style=`
    attribute on any hand-written page.**
  - `/docs/` (Zensical output) has **three inline executable `<script>` blocks** (the `__md_scope` /
    palette / tabbed-nav bootstrap) and **60 `style="text-align: right;"` attributes**, all on
    `/docs/tools-reference/`. The issue's proposed policy (`script-src 'self' https://www.googletagmanager.com`,
    no `style-src`) would break `/docs/` and that table.
  - Fetches: `https://downloads.privacyfence.eu` (stats, release list) and same-origin `release-stats.json`.
    Images are same-origin. No web fonts, no iframes, no forms.
- `tests/website_site.py` serves the built site on a local socket for the browser tests
  (`test_website_consent.py`, `test_website_layout.py`, ...); `website-build.yml` runs them against `_site-next`
  with `/docs/` included. `pages.yml` has no step after the deploy and its `deploy` job has no checkout.

## Design

### D1. Atlassian account ids (SEC-A5)

1. **Token record.** `atlassian_oauth.py` gets `_fetch_account_id(access_token: str, cloud_id: str) -> str`:
   the same `GET https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/myself` request as
   `_fetch_account_email`, returning `resp.json().get("accountId", "")`, never raising (`requests.RequestException`
   returns `""`, logged at debug). `resolve_resource_and_save` adds `"account_id": _fetch_account_id(access_token, cloud_id)`
   to the token record, next to `account_email`. `_fetch_account_email` is unchanged (existing tests patch it).
2. **Carry it.** `JiraClient`, `ConfluenceClient` and `AssetsClient` each add
   `"account_id"` to the dict they pass to `save_token_file` on a refresh: `self._config.get("account_id", "")`, and when
   that is empty, the `account_id` already in the token file on disk (the refresh code already reads that file; reuse
   how it gets the stored refresh token). Reason: each client copies its config (`self._config = dict(config)`), so
   one client's backfill is invisible to the others, and a refresh in a client that never saw the id must not erase it.
   `JiraClient` and `ConfluenceClient` each get a read-only property
   `account_id -> str` returning `str(self._config.get("account_id", "") or "")`.
3. **Backfill for existing installs.** `JiraClient.check_connection` already has `myself`. After it,
   `account_id = myself.get("accountId", "")`; when non-empty and different from `self._config.get("account_id")`,
   it sets `self._config["account_id"]` and, if `self._token_file` is set, persists it by **read-modify-write**:
   `record = dict(load_token_file(self._token_file)); record["account_id"] = account_id; save_token_file(self._token_file, record)`
   (so a stale in-memory token is never written back), inside `try/except (AtlassianOAuthError, OSError)` that logs a
   warning (`"Could not save the Atlassian account id: %s"`) and carries on: a failed save must not disable the connector.
   `daemon_main.build_connectors` then shares the id: right after `jira_client.check_connection()` it runs
   `if jira_client.account_id: atlassian_config["account_id"] = jira_client.account_id`, before the Confluence and Assets
   clients are built from that dict. An install with Jira enabled therefore gets the id on its first start after upgrading,
   for all three connectors. An install with only Confluence enabled gets it at its next Atlassian sign-in (the Confluence
   rule never matched before, so nothing regresses).
4. **Issue objects.** `JiraIssue` stays a dataclass with the same fields (so `asdict`, what the agent receives,
   is unchanged). `__post_init__` adds two plain attributes, `reporter_account_id: str = ""` and
   `assignee_account_id: str = ""` (not dataclass fields, like `display_description`, so `asdict(issue)`, what the agent
   receives, is unchanged). `JiraConnector._get_issue` builds the rule context's `raw_data` from `asdict`, which drops them, so it
   passes `raw_data={**result, "reporter_account_id": issue.reporter_account_id, "assignee_account_id": issue.assignee_account_id}`
   and keeps `filtered_data=result` (`connectors/jira.py` around lines 809 and 894). `JiraClient._parse_issue`
   sets them from `(f.get("reporter") or {}).get("accountId", "")` and `(f.get("assignee") or {}).get("accountId", "")`.
5. **Context.** `ReviewContext` gains `my_account_id: str = ""` **after** `session_created_ids` (so positional
   construction is unchanged). `gate.gated_call` gains the keyword parameter `my_account_id: str = ""` directly after
   `my_email` and passes it to `ReviewContext`. `JiraConnector` and `ConfluenceConnector` gain
   `self.my_account_id: str = ""` and pass `my_account_id=self.my_account_id` wherever they pass
   `my_email=self.my_email`. `AssetsConnector` is not touched. `daemon_main.build_connectors` sets
   `jira_connector.my_account_id = jira_client.account_id` and
   `confluence_connector.my_account_id = confluence_client.account_id`.
6. **Rules.** In `policy/scopes.py` the three matchers become exact comparisons of account ids and never look at
   `ctx.my_email` or at a display name:
   - `_i_am_reporter_matches`: `reporter_id = raw.get("reporter_account_id") if isinstance(raw, dict) else getattr(raw, "reporter_account_id", "")`;
     return `bool(ctx.my_account_id) and (reporter_id or "") == ctx.my_account_id`.
   - `_i_am_assignee_matches`: the same with `assignee_account_id`.
   - `_i_am_author_matches`: `author = raw.get("author") if isinstance(raw, dict) else getattr(raw, "author", "")`;
     return `bool(ctx.my_account_id) and (author or "") == ctx.my_account_id` (`ConfluencePage.author` is already the v2
     `authorId`).
   Comparison is case-sensitive: account ids are opaque.
7. **No new settings, no preflight change, no change to the rules' stored form or ids.**

Rejected: *comparing `accountId` only inside the Jira client and handing the matcher a boolean* - the matchers are
the single place identity rules live (ADR 0074's content-derived ids, `docs/approvals-and-policy.md`), and a boolean
computed elsewhere cannot be unit-tested against the rule table. *Reading `myself` on every rule evaluation* - one
extra request per read. *Re-using `my_email` for the id* - would make `ctx.my_domain` and the Google/Slack rules
that read `my_email` ambiguous.

### D2. Bundle serial (SEC-A7)

1. **Format.** A signed bundle may carry a top-level `"serial"`: a JSON integer ≥ 1 (a `bool` is not an integer
   here). `scripts/build_org_bundle.py` gets `--serial N` (a positive integer) and, **only when `--sign-key` is
   used**, adds `"serial": N` to the bundle before signing; default `N` is `int(time.time())` (UTC seconds), so a
   later build is always higher without the admin tracking a counter. `--serial` without `--sign-key` is an
   argparse error: "--serial needs --sign-key (only a signed bundle carries a serial)." The signature already covers
   every field except `signature`, so `serial` is signed with no change to the canonicalization.
2. **Pin.** The highest accepted serial is stored as `org_config_serial.txt` in the org dir (constant
   `PINNED_SERIAL_FILENAME`), decimal digits and a newline, written with `secure_files.atomic_write_text` (0600),
   beside `org_config_signing_pubkey.txt`.
3. **API in `org_bundle_signing.py`.**
   - `pinned_serial_path(org_dir: Path) -> Path`.
   - `load_pinned_serial(org_dir: Path) -> int | None`: `None` when the file does not exist; the integer when it
     holds a positive integer; **raises `ValueError`** when the file exists but holds anything else (fail closed).
   - `BundleTrust` gains `serial: int | None = None` and `remedy: str = "key"`: which sentence the two refusal messages
     end with (`"key"` is today's text about deleting the key pin; `"serial"` names the serial pin; `"none"` adds no remedy).
   - `verify_and_maybe_pin` keeps its signature. After the signature has verified (both the pinned-key path and the
     first-pin path) it runs the serial check; it still writes only the key pin:
     - `serial = bundle.get("serial")`. Present but not a positive int (including `bool`):
       `BundleTrust(ok=False, remedy="none", detail="serial is not a positive integer")`.
     - `pinned = load_pinned_serial(org_dir)`; on `ValueError`:
       `BundleTrust(ok=False, remedy="serial", detail="the pinned serial file is unreadable")`.
     - `pinned is not None and serial is None`:
       `BundleTrust(ok=False, remedy="serial", detail=f"this bundle carries no serial, but this install has already accepted serial {pinned}")`.
     - `pinned is not None and serial < pinned`:
       `BundleTrust(ok=False, remedy="serial", detail=f"serial {serial} is older than serial {pinned}, the newest this install has accepted (rollback refused)")`.
     - otherwise the existing `ok=True` result with `serial=serial`. **In the first-pin path the serial check runs before
       `_pin_public_key`**, so a bundle refused for its serial pins no key. Equal is accepted, because every daemon start
       re-verifies the installed bundle.
   - **Unsigned or key-less paths are unchanged** (an unsigned bundle with no key pinned has no serial to check).
   - `record_accepted_serial(trust: BundleTrust, org_dir: Path) -> None`: when `trust.ok and trust.serial is not None`
     and (`load_pinned_serial` is `None` or lower than `trust.serial`), writes the pin. It never raises: an `OSError` or
     `ValueError` is logged at warning level (`"Could not record the accepted organization config serial: %s"`), and the
     caller carries on.
   - `__all__` gains `PINNED_SERIAL_FILENAME`, `load_pinned_serial`, `pinned_serial_path`, `record_accepted_serial`.
4. **Callers record only after success.**
   - `daemon_main.load_org_config`: after the `trust.ok` check and the org-mode-unsigned check, call
     `org_bundle_signing.record_accepted_serial(trust, org_dir())` before `return data`.
   - `SettingsController.install_org_config_bytes`: after `atomic_write_json(dest, data, indent=2)` succeeds, call it.
     (Recording before the write would leave the pin ahead of the installed file and refuse the old file at the next
     start.)
5. **Messages.** By `trust.remedy`:
   - `"key"`: today's text, unchanged.
   - `"serial"`: `load_org_config` raises `ConfigurationError(f"Organization config at {path} was refused ({trust.detail}) -- refusing to start. To deliberately go back to an older bundle, an administrator must delete {org_bundle_signing.pinned_serial_path(org_dir())} first.")`;
     `install_org_config_bytes` sets `self.error = f"Refusing to install: {trust.detail}. To deliberately go back to an older bundle, an administrator must delete {org_bundle_signing.pinned_serial_path(org_dir())} first."`.
   - `"none"`: `ConfigurationError(f"Organization config at {path} was refused ({trust.detail}) -- refusing to start.")` and `self.error = f"Refusing to install: {trust.detail}."`.
6. **`--merge`** in `build_org_bundle.py` drops `signature` and `signing_public_key` before re-signing; it also drops `serial` (`bundle.pop("serial", None)`), so a merged bundle gets a fresh serial.
7. **Key rotation** (delete `org_config_signing_pubkey.txt`) keeps the serial pin; the new bundle's default serial is a
   current timestamp, so it is higher. Deleting both files is the full reset. A signed bundle with **no** serial is
   accepted while no serial has ever been pinned (installs that have not rebuilt their bundle keep working); once any
   serial is pinned, a serial-less bundle is refused.

Rejected: *an `issued_at` timestamp compared with the clock* - a clock is not a trust anchor, and the point is
ordering, not freshness. *Recording the serial inside `verify_and_maybe_pin`* - it is also called for a bundle that is
then not written (see 4). *A pin inside `org_config.json`* - ADR 0016's reasoning for keeping the key pin outside the
bundle applies unchanged. *Refusing serial-less bundles outright* - breaks every existing signed install on upgrade.

### D3. Windows coverage gate (TST-C14)

1. `scripts/check_coverage_floor.py` gains `WINDOWS_MODULE_FLOORS: dict[str, float]` with exactly the keys
   `src/privacyfence/windows_service.py`, `src/privacyfence/windows_acl.py`, `src/privacyfence/web/control_channel.py`
   and a CLI option `--platform {linux,windows}` (default `linux`, today's behaviour, byte-for-byte output).
   With `--platform windows` the script prints `Windows module floors:` and checks **only** `WINDOWS_MODULE_FLOORS`
   (no overall floor, no `MODULE_FLOORS`): same `[ ok ]` / `[FAIL]` / `[MISSING]` lines, the same stale warning, the same
   failure text, and ends with `Coverage ratchet passed.` or exit 1.
2. `_load_totals` normalises every `coverage.json` file key with `path.replace("\\", "/")`, because coverage writes
   `src\privacyfence\...` on Windows.
3. `platform-windows` in `tests.yml`: the pytest step becomes
   `pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json`;
   a new step `Windows coverage floors` runs `python scripts/check_coverage_floor.py --platform windows coverage.json`;
   a new `if: always()` step uploads `coverage.json` as the artifact `platform-windows-coverage` (14 days).
4. **Floor values** are whole numbers equal to `math.floor(measured)` (written with `.0`) where `measured` is the percent
   the `platform-windows` job printed on the feature branch (the file's convention: whole percent, rounded down; a larger
   margin would trip the stale-floor warning, which fires above `floor + 1.0`). They cannot be measured in the Linux container, so the phase first lands the values `0.0`,
   dispatches `tests.yml` on its branch (the workflow exists on `main`, so `workflow_dispatch` with
   `ref=<phase branch>` runs the branch's copy), reads the numbers from the job log, writes the real floors,
   and dispatches again to see `Coverage ratchet passed.`
5. The four skipped tests lose their `skipif` and are rewritten to build their paths from `tmp_path`:
   - `TestResolvePath::test_absolute_path_is_returned_unchanged`: `path = str(tmp_path / "x.json")`,
     `assert daemon_main._resolve_path(path) == path`.
   - `TestResolvePath::test_relative_path_is_joined_with_project_root`: `monkeypatch.setattr(daemon_main, "PROJECT_ROOT", str(tmp_path))`,
     `assert daemon_main._resolve_path("credentials/x.json") == os.path.join(str(tmp_path), "credentials/x.json")`.
   - `TestResolveAuthorityPath::test_absolute_path_is_returned_unchanged`: as the first, with `_resolve_authority_path`.
   - the Telegram `test_built_when_creds_and_session_present`: `_resolve_path` returns `os.path.join(PROJECT_ROOT, "credentials/telegram.session")`
     (`TOKEN_FILES["telegram"]`), which on Windows differs from `str(tmp_path / "credentials" / "telegram.session")` only in
     separators. The expected `session_file` becomes `os.path.join(str(tmp_path), "credentials/telegram.session")`.
   No `not otherwise tracked`, `a known gap` or `wrong-drive` text may remain for these tests.

### D4. Website security headers (WEB-9)

Cloudflare, not the repository, sends the headers (GitHub Pages cannot). The repository holds the **expected**
headers, proves a browser accepts the site under them, and re-checks the live site after every deploy.

1. **`scripts/site_security_headers.py`** (stdlib only; importable by tests and runnable as a script that prints the
   headers) defines:
   - `HSTS = "max-age=31536000; includeSubDomains"` (no `preload`).
   - `NOSNIFF = "nosniff"`, `REFERRER_POLICY = "strict-origin-when-cross-origin"`.
   - `CSP_SITE` (one string, directives in this order, separated by `"; "`):
     `default-src 'self'`; `script-src 'self' https://www.googletagmanager.com`; `style-src 'self'`;
     `img-src 'self' data: https://*.google-analytics.com https://www.googletagmanager.com`;
     `connect-src 'self' https://downloads.privacyfence.eu https://*.google-analytics.com https://*.analytics.google.com https://www.googletagmanager.com`;
     `font-src 'self'`; `object-src 'none'`; `base-uri 'self'`; `form-action 'self'`; `frame-ancestors 'none'`.
   - `CSP_DOCS`: `CSP_SITE` with `script-src` replaced by
     `script-src 'self' 'unsafe-inline' https://www.googletagmanager.com` and `style-src` by `style-src 'self' 'unsafe-inline'`
     (the docs generator's inline bootstrap scripts and table alignment styles; accepted and recorded in the ADR).
   - `def headers_for_path(path: str) -> dict[str, str]`: the four headers
     (`Strict-Transport-Security`, `X-Content-Type-Options`, `Referrer-Policy`, `Content-Security-Policy`),
     with `CSP_DOCS` when `path == "/docs"` or `path.startswith("/docs/")`, else `CSP_SITE`.
   - `main()` printing, for the Cloudflare dashboard, the three rules below.
2. **The Cloudflare rules** (three *Modify Response Header* Transform Rules on the `privacyfence.eu` zone; the
   manual step creates them from the output of `python3 scripts/site_security_headers.py`):
   - `pf-security-headers-all`: when the hostname equals `privacyfence.eu`, set `Strict-Transport-Security`,
     `X-Content-Type-Options`, `Referrer-Policy`.
   - `pf-csp-site`: when hostname equals `privacyfence.eu` and URI Path does not start with `/docs/` and does not equal
     `/docs`, set `Content-Security-Policy` to `CSP_SITE`.
   - `pf-csp-docs`: when hostname equals `privacyfence.eu` and URI Path starts with `/docs/` or equals `/docs`, set
     `Content-Security-Policy` to `CSP_DOCS`.
   `www.privacyfence.eu` is a Cloudflare 301 to the apex (ADR 0053) and needs no headers.
3. **`scripts/check_site_headers.py <base-url>`** (stdlib `urllib`, no redirects followed; exit 0 or 1): requests
   `<base>/`, `<base>/docs/` and `<base>/download/`, compares each of the four headers with `headers_for_path`
   (header names case-insensitive, values exact), prints one `[ ok ]` / `[FAIL] <path> <header>: expected ... got ...`
   line per header, then `Site security headers match.` or `Site security headers differ.`
4. **Tests.**
   - `tests/website_site.py::serve()` sends `headers_for_path(self.path.split("?")[0])` on every response, so every
     existing browser test already runs under the policy.
   - `tests/unit/test_website_security_headers.py` (no browser): the constants have the exact values above;
     `headers_for_path` picks `CSP_DOCS` only under `/docs`; on `built_site()`, every page outside `/docs/` has no inline
     executable `<script>` (only `src=` scripts and `type="application/ld+json"`), no `style=` attribute, no `<style>`
     element and no `on*=` attribute, and every `<script src>` is same-origin or `https://www.googletagmanager.com`.
   - `tests/integration/test_website_csp.py` (browser, same skip posture as `test_website_consent.py`): for every
     `page_paths()` page, with consent accepted and gtag.js answered by an empty script, no `securitypolicyviolation`
     event and no console message containing "Content Security Policy" fires, and the consent banner still works.
   - `tests/unit/test_check_site_headers.py`: against a local `http.server` that sends the right headers, a missing
     header, and a wrong value.
5. **CI.** `website-build.yml` adds `tests/integration/test_website_csp.py` to the guardrail `pytest` command.
   `pages.yml`: the `deploy` job gets, after the deploy step, `actions/checkout` (default depth, same pinned SHA the
   file already uses) and a step `Check the security headers` running
   `python3 scripts/check_site_headers.py https://privacyfence.eu`; the `paths:` filter gains
   `scripts/site_security_headers.py` and `scripts/check_site_headers.py`.

Rejected: *the issue's CSP verbatim* - it blocks `/docs/`' inline scripts and 60 inline styles (measured above), so it
would have broken the docs the day the rule went live. *Hashes for the three inline scripts* - they change whenever
the docs generator is bumped, and nobody would notice until `/docs/` broke. *`'unsafe-inline'` site-wide* - the
hand-written pages need none, so they keep the strict policy. *A `<meta http-equiv>` CSP in the header partial* -
cannot carry `frame-ancestors`, and cannot cover HSTS. *HSTS `preload`* - irreversible for the browsers' lists;
a separate decision.

## ADRs

- **0194** - Jira and Confluence "mine" rules compare Atlassian account ids, never names or emails (relates to 0117,
  0118).
- **0195** - An organization bundle carries a signed serial, and an install refuses a lower one than it has accepted;
  amends 0016 (the old ADR's Status gains one line pointing forward; its index row gains "amended by 0195").
- **0196** - privacyfence.eu's security headers are set by Cloudflare Response Header Transform Rules, with the expected
  set kept in the repository and checked after every deploy; the `/docs/` CSP allows inline scripts and styles;
  amends 0053 (same Status line and index treatment).

TST-C14 makes no ADR-worthy decision (a CI gate and test rewrites).

## Manual steps

Only the user can create the Cloudflare rules (a dashboard action; no Cloudflare token exists in any session) and
confirm the real Google Analytics flow, which the tests stub. Both happen after the last phase and before the PR is
merged, because the rules do not depend on the code and the post-deploy check needs them to exist when the merge
deploys. There are no `manual_before` steps. Instructions with exact links:
[manual steps page](https://claude.ai/artifact/6Rqf3UAVtm9FxiYLxw2bEn).

- **ma1-cloudflare-header-rules** - create the three Transform Rules (about 15 minutes).
- **ma2-live-site-check** - run `python3 scripts/check_site_headers.py https://privacyfence.eu`, then accept
  cookies and click through the home page, `/download/` and `/docs/` with the browser console open (about 10 minutes).

## Risks and open questions

- **CSP text is wrong for the real site** (a third-party origin the stubbed tests never load, such as a different
  Google Analytics region host). The wildcards in the policy are the ones the issue proposed; `ma2` is the real check.
  If `test_website_csp.py` reports a violation from the built site in p5, the worker stops with `status=blocked`
  and names the blocked URL; it does not edit the policy strings, which the manual page repeats verbatim.
- **HSTS `includeSubDomains`** also covers `downloads.privacyfence.eu` (a Cloudflare Worker, HTTPS only) and any other
  subdomain. If the user runs a plain-HTTP subdomain, `ma1` says to drop `includeSubDomains` from the rule; the
  repository constant then differs and the check reports it.
- **p1:** if `JiraClient._token_file` is empty, `check_connection` cannot persist; it must not raise (the id then lives in
  memory for that run only). A worker that finds `JiraIssue` is copied with `dataclasses.replace` or rebuilt anywhere in
  `src/` (grep `JiraIssue(` and `replace(`) stops with `status=blocked`, because the two plain attributes would be lost on copy.
- **p2:** the definition of done owes a live-connector QA report for the changed `*_client.py` and connector files; p2
  dispatches `qa-record-fixture.yml` and reads the fixture diff. A fixture diff that contains a real account id, email or
  tenant URL is a stop (`status=blocked`).
- **p2:** if `ConfluenceClient` has another parse path that fills `ConfluencePage.author` with a display name (grep
  `author=` shows only the v2 `authorId` today), stop with `status=blocked`.
- **p3:** a signed bundle written by an older `build_org_bundle.py` has no serial and is accepted until a serial is
  pinned. If `tests/integration/test_org_ubuntu_release_smoke.py` or `scripts/sync_room_directory.py` break, they build
  bundles without a serial and need no change; if one fails for another reason, stop.
- **p4:** the floors need two `tests.yml` dispatches on the phase branch (the Windows job takes about 25 minutes). This is a
  deliberate exception to the steward skill's "cross-platform pytest is not dispatched": that line assumes a pull request, and
  phase branches are never PR'd.
  `platform-windows` red for a reason outside the four rewritten tests means stop with `status=blocked`; do not skip
  a test to get green. The exact measured percentages are only known once Windows runs.

## Implementation manifest

```yaml
plan_slug: repo-review-low-findings
feature_branch: fix/review-low-findings
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/6Rqf3UAVtm9FxiYLxw2bEn
manual_steps_source: docs/repo-review-low-findings-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-cloudflare-header-rules
    title: Create the three Cloudflare response-header Transform Rules for privacyfence.eu
    why: GitHub Pages cannot send headers, so Cloudflare does; the post-deploy check in pages.yml fails after the merge deploys unless the rules exist.
  - id: ma2-live-site-check
    title: Check the live site's headers and click through it with consent accepted
    why: The browser tests stub Google Analytics, so only a real visit shows whether the policy blocks something the real analytics flow needs.
verify_after_merge:
  - python3 -m pytest tests/unit/policy/test_scopes.py tests/unit/test_atlassian_oauth.py tests/unit/test_jira_client.py tests/unit/test_confluence_client.py tests/unit/test_assets_client.py tests/unit/connectors/test_jira_connector.py tests/unit/connectors/test_confluence_connector.py tests/unit/test_gate_real_evaluator.py -q
  - python3 -m pytest tests/unit/test_org_bundle_signing.py tests/unit/test_build_org_bundle.py tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py -q
  - python3 -m pytest tests/unit/test_check_coverage_floor.py tests/unit/test_website_security_headers.py tests/unit/test_check_site_headers.py -q
final_checks:
  - docs/repo-review-low-findings-plan.md and docs/repo-review-low-findings-plan-manual-steps.html are deleted and nothing links to them
  - docs/adr/0194-*.md, docs/adr/0195-*.md and docs/adr/0196-*.md exist, are Accepted, and are in docs/adr/README.md; the 0016 and 0053 rows say "amended by 0195" and "amended by 0196"
  - CHANGELOG.md has entries under ## [Unreleased] and no version heading
  - the PR body closes #923, #924, #925 and #926 and lists ma1-cloudflare-header-rules and ma2-live-site-check as unchecked items with the manual steps link
phases:
  - id: p1-atlassian-account-id
    title: Record the Atlassian account id at sign-in and carry it through token refreshes
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/atlassian_oauth.py
      - src/privacyfence/jira_client.py
      - src/privacyfence/confluence_client.py
      - src/privacyfence/assets_client.py
      - tests/unit/test_atlassian_oauth.py
      - tests/unit/test_jira_client.py
      - tests/unit/test_confluence_client.py
      - tests/unit/test_assets_client.py
    brief: |
      Implements Design D1 points 1 to 4 (read them in this plan first). Read CLAUDE.md,
      docs/coding-and-testing-guidelines.md and docs/testing-policy.md. Nothing in this phase changes a policy rule.
      1. src/privacyfence/atlassian_oauth.py: add `_fetch_account_id(access_token, cloud_id) -> str`
         directly after `_fetch_account_email`, with the same request, timeout and error handling, returning
         `resp.json().get("accountId", "")` and `""` on `requests.RequestException`. In
         `resolve_resource_and_save` add `"account_id": _fetch_account_id(access_token, cloud_id)` to
         `token_record`, after the `"account_email"` entry. Leave `_fetch_account_email` as it is.
      2. src/privacyfence/jira_client.py: (a) in `_try_refresh`, add `"account_id"` to the dict passed to `save_token_file`
         as Design D1 point 2 says (the in-memory value, else the one in the token file on disk). (b) add the read-only
         property `account_id` returning `str(self._config.get("account_id", "") or "")`. (c) in `check_connection`, after
         `myself` is read, backfill as Design D1 point 3 says (read-modify-write inside
         `try/except (AtlassianOAuthError, OSError)` logging a warning; import `AtlassianOAuthError` and `load_token_file`
         from `.atlassian_oauth` if not already imported). A failed save must never make `check_connection` raise.
         (d) in `JiraIssue.__post_init__` add two plain attributes `self.reporter_account_id: str = ""` and
         `self.assignee_account_id: str = ""` with a comment that, like `display_description`, they are not dataclass
         fields so the agent never receives them. In `_parse_issue`, after the `JiraIssue(...)` is built, set them from
         `(f.get("reporter") or {}).get("accountId", "")` and `(f.get("assignee") or {}).get("accountId", "")`.
      3. src/privacyfence/confluence_client.py: add the same `account_id` property, and the same `"account_id"` entry
         (in-memory, else on disk) in the dict it saves on refresh (the `save_token_file` call next to `"account_email"`).
      4. src/privacyfence/assets_client.py: the same `"account_id"` entry in the dict it saves on refresh. No property.
      5. Tests (extend the existing files; copy the style of the neighbouring tests; no network, ever):
         - test_atlassian_oauth.py: every test that patches `_fetch_account_email` (the `monkeypatch.setattr(atlassian_oauth_module,
           "_fetch_account_email", ...)` lines) must also patch `_fetch_account_id` to a lambda returning `""` (or the id the test
           asserts), otherwise it makes a real HTTP call; update the exact token-dict assertion (the one containing
           `"account_email": "me@x.com", "scopes": []`) to include `"account_id"`. Add `TestFetchAccountId` with
           `test_returns_the_account_id` and `test_returns_empty_when_the_request_fails`.
         - test_jira_client.py: update the exact-dict assertion in `test_successful_refresh_persists_to_shared_token_file` to
           include `"account_id"`; add `test_check_connection_records_the_account_id`,
           `test_check_connection_persists_a_missing_account_id` (the saved token file holds `account_id` and keeps the other
           fields as they were on disk), `test_check_connection_does_not_rewrite_an_unchanged_account_id`,
           `test_check_connection_survives_a_failed_account_id_save`, `test_refresh_keeps_the_account_id_from_disk_when_config_lacks_it`,
           `test_parse_issue_records_reporter_and_assignee_account_ids` (and `dataclasses.asdict(issue)` has no
           `reporter_account_id` key).
         - test_confluence_client.py and test_assets_client.py: update their exact refresh-dict assertions the same way and add
           `test_refresh_keeps_the_account_id_from_disk_when_config_lacks_it`; Confluence also
           `test_account_id_property_reads_the_config`.
      6. Run `grep -rn "JiraIssue(" src` and `grep -rn "replace(" src/privacyfence/jira_client.py src/privacyfence/connectors/jira.py`.
         If any code copies or rebuilds a `JiraIssue` after `_parse_issue`, stop with status=blocked and say where.
      7. Run `ruff check .`, `python3 -m mypy src/privacyfence` and the touched test files. No CHANGELOG or ADR
         edits here; the last phase writes them.
    acceptance:
      - python3 -m pytest tests/unit/test_atlassian_oauth.py -q passes
      - python3 -m pytest tests/unit/test_jira_client.py -q -k "account_id" passes
      - python3 -m pytest tests/unit/test_confluence_client.py tests/unit/test_assets_client.py -q -k "account_id" passes
      - grep -c '"account_id"' src/privacyfence/jira_client.py src/privacyfence/confluence_client.py src/privacyfence/assets_client.py prints at least 1 for each file
      - ruff check . passes
  - id: p2-account-id-rules
    title: Match the Jira reporter/assignee and Confluence author rules on the account id
    depends_on: [p1-atlassian-account-id]
    complexity: M
    touches:
      - src/privacyfence/auto_accept.py
      - src/privacyfence/gate.py
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/connectors/jira.py
      - src/privacyfence/connectors/confluence.py
      - src/privacyfence/daemon_main.py
      - tests/helpers.py
      - tests/fixtures/**
      - tests/unit/policy/_v1_reference.py
      - tests/unit/policy/test_scopes.py
      - tests/unit/test_gate_real_evaluator.py
      - tests/unit/connectors/test_jira_connector.py
      - tests/unit/connectors/test_confluence_connector.py
      - tests/unit/test_daemon_main.py
    brief: |
      Implements Design D1 points 5 and 6; p1 already landed the client side. Read the Design section first.
      1. src/privacyfence/auto_accept.py: add `my_account_id: str = ""` to `ReviewContext` after `session_created_ids`.
      2. src/privacyfence/gate.py: add the keyword parameter `my_account_id: str = ""` to `gated_call` directly
         after `my_email`, and pass `my_account_id=my_account_id` to the `ReviewContext(...)` it builds. Do not touch
         `preflight_auto_accept`.
      3. src/privacyfence/connectors/jira.py and connectors/confluence.py: in `__init__`, next to
         `self.my_email: str = ""`, add `self.my_account_id: str = ""`; at every `my_email=self.my_email,` argument of a
         `gated_call` (jira.py 7 sites, confluence.py 5 sites) add `my_account_id=self.my_account_id,` on the next
         line. `grep -c "my_account_id=self.my_account_id"` must equal the `my_email=self.my_email` count in each file.
         In `JiraConnector._get_issue` (the `jira_get_issue` tool, around lines 809 and 894) the context's `raw_data` is
         `asdict(issue)` plus comments, which drops the two plain attributes: pass
         `raw_data={**result, "reporter_account_id": issue.reporter_account_id, "assignee_account_id": issue.assignee_account_id}`
         and keep `filtered_data=result`. Do not touch connectors/assets.py.
      4. src/privacyfence/daemon_main.py in `build_connectors`: right after `info = jira_client.check_connection()` add
         `if jira_client.account_id: atlassian_config["account_id"] = jira_client.account_id` (so the Confluence and Assets
         clients built after it start with the id); after `jira_connector.my_email = ...` add
         `jira_connector.my_account_id = jira_client.account_id`; after `confluence_connector.my_email = ...` add
         `confluence_connector.my_account_id = confluence_client.account_id`. In tests/unit/test_daemon_main.py,
         `fake_client_class`'s `_FakeClient` gets the class attribute `account_id = ""` (every Jira and Confluence
         `build_connectors` test needs it).
      5. src/privacyfence/policy/scopes.py: replace the three matchers exactly as Design D1 point 6 says.
      6. tests/helpers.py: `make_ctx` defaults gain `my_account_id=""`.
      7. tests/unit/policy/_v1_reference.py is the frozen v1 oracle that `test_scopes.py::test_agrees_on_every_fixture`
         compares every selector against; its `_rule_i_am_reporter`, `_rule_i_am_assignee` and `_rule_i_am_author` still hold
         the substring rules. Rewrite those three to the account-id comparison of Design D1 point 6 and add a comment that this
         is a deliberate divergence from v1 (ADR 0194).
      8. Tests:
         - tests/unit/policy/test_scopes.py: replace the `i_am_reporter`, `i_am_assignee` and `i_am_author` entries of the
           table that holds them (around lines 93, 215, 220) so their matching cases use `my_account_id` and a
           `SimpleNamespace` or dict raw with `reporter_account_id` / `assignee_account_id` / `author`, and add
           these named tests: `test_i_am_reporter_ignores_a_display_name_that_contains_my_email` (raw reporter
           "Mallory (me@example.com)", reporter_account_id "other", my_email "me@example.com", my_account_id "acc-me":
           False), `test_i_am_reporter_matches_only_my_account_id` (equal id True; different id False; empty
           my_account_id False even when the raw id is empty), the same pair for assignee, and
           `test_i_am_author_matches_only_my_account_id`.
         - tests/unit/test_gate_real_evaluator.py: update the existing i_am_reporter / i_am_assignee / i_am_author
           cases (around lines 594-720) to build the context with `my_account_id`, keep the existing
           "name containing my email does not match" case and make it meaningful for reporter and assignee too.
         - tests/unit/connectors/test_jira_connector.py and test_confluence_connector.py: one test each,
           `test_gated_calls_pass_the_account_id`, that a read through the connector reaches `gated_call` with
           `my_account_id` equal to the connector's attribute (copy how the neighbouring tests capture `gated_call`). Jira also
           `test_get_issue_raw_data_carries_the_reporter_and_assignee_account_ids` (present in `raw_data`, absent from
           `filtered_data`). Update the stale module docstring at the top of test_jira_connector.py that says `asdict(issue)`
           already matches what the rules expect.
         - tests/unit/test_daemon_main.py: `test_jira_and_confluence_connectors_get_the_clients_account_id`.
      9. Run `ruff check .`, `python3 -m mypy src/privacyfence`, `python3 scripts/mypy_strict_modules.py`, and
         `python3 -m pytest tests/unit -q -x` (the whole unit suite, because `ReviewContext` and `gated_call` are shared).
         Stop with status=blocked if a test outside the touched files fails and the fix is not an `account_id` default.
      10. Definition of done (`verify.dod_conditional`: the change touches `*_client.py` and `connectors/**`): push the phase
         branch, then for each of `jira`, `confluence` and `assets` that is a key of `CONNECTOR_CHECKS` in
         scripts/qa_fixture_recorder.py, dispatch `qa-record-fixture.yml` on `main` with inputs `connector=<name>` and
         `branch=<this phase branch>` (one at a time: the runs share a concurrency group and a queued run is not a hang),
         wait, `git pull` the fixture commits, and read the fixture diff. If it contains a real account id, email address or
         tenant URL, stop with status=blocked; do not commit it. Note the three results for the PR body.
    acceptance:
      - python3 -m pytest tests/unit/policy/test_scopes.py -q -k "i_am_reporter_ignores_a_display_name_that_contains_my_email or i_am_reporter_matches_only_my_account_id or i_am_author_matches_only_my_account_id" passes
      - python3 -m pytest tests/unit/test_gate_real_evaluator.py tests/unit/connectors/test_jira_connector.py tests/unit/connectors/test_confluence_connector.py -q passes
      - python3 -m pytest tests/unit/test_daemon_main.py -q -k "account_id" passes
      - python3 -m pytest tests/unit/policy -q passes
      - grep -n "my_email.lower() in" src/privacyfence/policy/scopes.py tests/unit/policy/_v1_reference.py shows no line inside the i_am_reporter, i_am_assignee or i_am_author rules
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
  - id: p3-bundle-serial
    title: Sign a serial into organization bundles and refuse an older one
    depends_on: [p2-account-id-rules]
    complexity: M
    touches:
      - src/privacyfence/org_bundle_signing.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/settings_controller.py
      - scripts/build_org_bundle.py
      - tests/unit/test_org_bundle_signing.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_settings_controller.py
      - tests/unit/test_build_org_bundle.py
    brief: |
      Implements Design D2 in full; read it first, and docs/adr/0016-org-config-bundle-hash-log-and-signing.md.
      1. src/privacyfence/org_bundle_signing.py: add `PINNED_SERIAL_FILENAME = "org_config_serial.txt"`,
         `SERIAL_FIELD = "serial"`, `pinned_serial_path`, `load_pinned_serial`, the two new `BundleTrust` fields
         (`serial: int | None = None`, `remedy: str = "key"`), the serial check in `verify_and_maybe_pin` (both the
         pinned-key success path and the first-pin path, where it runs before `_pin_public_key`; the detail strings are in
         Design D2 point 3 and are exact),
         and `record_accepted_serial`. Update `__all__` and the module docstring's last paragraph of the TOFU
         description with two sentences on the serial pin. `would_pin_new_key` is unchanged.
      2. src/privacyfence/daemon_main.py `load_org_config`: choose the message by `trust.remedy` as Design D2
         point 5 says (`"key"` keeps today's text) and call
         `org_bundle_signing.record_accepted_serial(trust, org_dir())` immediately before `return data`.
      3. src/privacyfence/settings_controller.py `install_org_config_bytes`: same two changes (messages from point 5;
         call `record_accepted_serial` after the successful `atomic_write_json`, before `self.error = ""`).
      4. scripts/build_org_bundle.py: add `import time` if missing; argparse `--serial` (type: a positive int, same
         style as `_scoped_token_days`, help "Serial signed into the bundle (default: the current Unix time). Installs refuse a bundle whose serial is lower than the highest they have accepted."); after parsing, when
         `--serial` is given without `--sign-key`, `parser.error("--serial needs --sign-key (only a signed bundle carries a serial).")`;
         when signing, set `bundle["serial"] = args.serial if args.serial is not None else int(time.time())` before
         `_sign_bundle` is called; in the `--merge` branch that pops `signature` and `signing_public_key`, also
         `bundle.pop("serial", None)`. Mention `--serial` in the module docstring's signing paragraph.
      5. Tests (named; copy `_write_signed_bundle` and the neighbouring style):
         - tests/unit/test_org_bundle_signing.py: `test_a_lower_serial_than_the_pinned_one_is_refused_as_a_rollback`,
           `test_the_same_serial_is_accepted_on_reload`, `test_a_bundle_without_a_serial_is_refused_once_a_serial_is_pinned`,
           `test_a_bundle_without_a_serial_is_accepted_while_none_is_pinned`,
           `test_a_boolean_or_non_positive_serial_is_refused_with_no_remedy`,
           `test_an_unreadable_serial_pin_fails_closed_with_the_serial_remedy`, `test_a_rollback_bundle_with_a_new_key_does_not_pin_the_key`,
           `test_record_accepted_serial_only_raises_the_pin`, `test_record_accepted_serial_swallows_a_write_error`,
           `test_deleting_the_serial_pin_allows_an_older_bundle`.
         - tests/unit/test_daemon_main.py, in `TestLoadOrgConfigSigning`:
           `test_load_org_config_rejects_older_signed_bundle` (load serial 200, then replace the file with a serial-100
           bundle signed by the same key: `ConfigurationError` whose text contains `rollback refused` and the serial pin
           path), `test_load_org_config_records_the_highest_serial`.
         - tests/unit/test_settings_controller.py: `test_install_org_config_refuses_an_older_serial_and_keeps_the_installed_file`,
           `test_install_org_config_records_the_serial_only_after_the_write` (patch `atomic_write_json` to raise
           `OSError`: no pin written).
         - tests/unit/test_build_org_bundle.py: `test_signing_adds_a_serial_that_defaults_to_the_current_time`,
           `test_serial_flag_sets_the_serial`, `test_serial_without_sign_key_is_an_error`, `test_merge_does_not_carry_the_old_serial`,
           `test_a_script_signed_bundle_with_a_serial_verifies_in_the_daemon_copy`.
      6. Run `ruff check .`, `bandit -c pyproject.toml -r src`, `python3 -m mypy src/privacyfence`,
         `python3 scripts/mypy_strict_modules.py`, and confirm `org_bundle_signing.py` stays at 100% with
         `python3 -m pytest tests/unit/test_org_bundle_signing.py -q --cov=privacyfence.org_bundle_signing --cov-branch --cov-report=term-missing`
         (its floor in scripts/check_coverage_floor.py is 99.0). Do not edit docs, ADRs or CHANGELOG; the last phase does.
    acceptance:
      - python3 -m pytest tests/unit/test_daemon_main.py -q -k "test_load_org_config_rejects_older_signed_bundle" passes
      - python3 -m pytest tests/unit/test_org_bundle_signing.py tests/unit/test_build_org_bundle.py tests/unit/test_settings_controller.py -q passes
      - python3 -m pytest tests/unit/test_org_bundle_signing.py -q --cov=privacyfence.org_bundle_signing --cov-branch --cov-report=term-missing reports no missing lines
      - grep -n "rollback refused" src/privacyfence/org_bundle_signing.py matches
      - ruff check . and bandit -c pyproject.toml -r src pass
  - id: p4-windows-coverage-gate
    title: Gate Windows-only coverage on platform-windows and un-skip the four path tests
    depends_on: [p3-bundle-serial]
    complexity: M
    touches:
      - scripts/check_coverage_floor.py
      - .github/workflows/tests.yml
      - tests/unit/test_check_coverage_floor.py
      - tests/unit/test_daemon_main.py
    brief: |
      Implements Design D3 in full; read it first, and docs/testing-policy.md. This phase needs two real Windows CI runs;
      the Linux container cannot measure Windows coverage, so the floors come from a dispatched run.
      1. scripts/check_coverage_floor.py: add `WINDOWS_MODULE_FLOORS` (keys exactly as in Design D3 point 1, values `0.0`
         for now, with a comment saying these are the platform-windows job's floors and why they are separate from
         MODULE_FLOORS: the same modules read differently on Windows), `--platform {linux,windows}`, the
         backslash normalisation in `_load_totals`, and move the per-module loop into a helper used by both modes so the
         Linux output stays identical. Extend the module docstring's "fails if" list and Usage with the Windows mode.
      2. tests/unit/test_check_coverage_floor.py: add `test_windows_mode_passes_when_every_windows_module_is_at_its_floor`,
         `test_windows_mode_fails_below_a_windows_floor`, `test_windows_mode_fails_when_a_windows_module_is_missing`,
         `test_windows_mode_ignores_the_linux_floors_and_the_overall_floor`,
         `test_windows_backslash_paths_are_normalised`, `test_default_platform_is_linux`. Reuse `_write_report`'s pattern with a
         Windows-shaped report.
      3. .github/workflows/tests.yml, `platform-windows` job only: change the pytest step as in Design D3 point 3, add the
         `Windows coverage floors` step and the `if: always()` upload of `coverage.json` as `platform-windows-coverage`
         (use the same pinned `actions/upload-artifact` SHA as the neighbouring step). Update the job's leading comment
         (it says nothing gates; make it say what gates now).
      4. tests/unit/test_daemon_main.py: apply Design D3 point 5 to the four skipped tests (`TestResolvePath` x2,
         `TestResolveAuthorityPath::test_absolute_path_is_returned_unchanged`, and the Telegram
         `test_built_when_creds_and_session_present`), removing their `skipif`.
      5. Run `ruff check .`, `python3 -m pytest tests/unit/test_check_coverage_floor.py tests/unit/test_daemon_main.py -q`,
         and `python3 scripts/check_coverage_floor.py coverage.json` after a full `pytest --cov=src/privacyfence --cov-branch
         --cov-report=json:coverage.json` (the Linux gate must still pass unchanged).
      6. Push the phase branch. Dispatch `tests.yml` on it with `mcp__github__actions_run_trigger`
         (`workflow_id: tests.yml`, `ref: <this phase branch>`). This is a deliberate exception to the steward skill's
         "cross-platform pytest is not dispatched": phase branches are never PR'd, so no pull_request run exists. Wait for the run (`actions_get`), then read the
         `platform-windows` job log (`get_job_logs`). With every floor at `0.0` expect a green job whose log prints
         `[ ok ] src/privacyfence/...: NN.NN%` for the three modules. If any step or test failed, read the log, fix the cause in the four rewritten tests, and dispatch again;
         after two failed attempts stop with status=blocked and paste the failing assertion.
      7. Set each Windows floor to `math.floor(measured)` (a whole number written with `.0`, e.g. measured 87.4 -> 87.0), amend
         the comment to say the numbers were measured on the platform-windows job on <today's date>, push, dispatch
         `tests.yml` again, and confirm the `platform-windows` log shows `Coverage ratchet passed.` with no
         `stale coverage floor` warning for the three Windows modules.
      8. Do not edit CHANGELOG or docs; the last phase does.
    acceptance:
      - python3 -m pytest tests/unit/test_check_coverage_floor.py -q passes
      - grep -c "not otherwise tracked" tests/unit/test_daemon_main.py prints 0
      - grep -c "wrong-drive" tests/unit/test_daemon_main.py prints 0
      - the second tests.yml run on the phase branch has a green platform-windows job whose log contains "Coverage ratchet passed."
      - python3 scripts/check_coverage_floor.py coverage.json passes after the full Linux run
      - ruff check . passes
  - id: p5-website-security-headers
    title: Keep the expected site headers in the repo, test the site under them, check them after deploy
    depends_on: []
    complexity: M
    touches:
      - scripts/site_security_headers.py
      - scripts/check_site_headers.py
      - tests/website_site.py
      - tests/unit/test_website_security_headers.py
      - tests/unit/test_check_site_headers.py
      - tests/integration/test_website_csp.py
      - .github/workflows/website-build.yml
      - .github/workflows/pages.yml
    brief: |
      Implements Design D4 in full; read it first, plus docs/adr/0053-the-website-is-hosted-on-github-pages-behind-cloudflare.md,
      docs/adr/0050-website-analytics-is-ga4-behind-consent.md, tests/website_site.py and tests/integration/test_website_consent.py.
      Set the policy strings exactly as Design D4 point 1 gives them; do not edit them to make a test pass.
      1. Create scripts/site_security_headers.py with the constants, `headers_for_path` and `main()` as Design D4 points 1
         and 2 say. `main()` prints, for each of the three rules, its name, its match expression in plain words and its
         header lines.
      2. Create scripts/check_site_headers.py as Design D4 point 3 says (stdlib `urllib.request` with a handler that
         does not follow redirects; a `check(base_url) -> list[str]` function returning the mismatch lines so tests can
         call it; `main(argv)` printing and returning 0 or 1). Import `site_security_headers` after
         `sys.path.insert(0, str(Path(__file__).resolve().parent))`, as scripts/build_site.py does at line ~81.
      3. tests/website_site.py: make `serve()`'s `Handler` send `headers_for_path(self.path.split("?")[0])` by overriding
         `end_headers`; import the function with the `sys.path.insert(0, str(REPO / "scripts"))` the file already does.
      4. Tests, as listed in Design D4 point 4, with these names:
         - tests/unit/test_website_security_headers.py: `test_the_csp_has_the_agreed_directives`,
           `test_the_docs_csp_is_the_site_csp_plus_inline_script_and_style`, `test_headers_for_path_picks_the_docs_policy_only_under_docs`,
           `test_hand_written_pages_need_no_unsafe_inline` (loops over `built_site()` pages outside `docs/`).
         - tests/unit/test_check_site_headers.py: `test_matching_headers_pass`, `test_a_missing_header_is_reported`,
           `test_a_wrong_value_is_reported`, `test_the_docs_path_expects_the_docs_policy` (a local http.server on port 0).
         - tests/integration/test_website_csp.py: `test_no_page_violates_its_csp` (parametrised over `page_paths()`),
           `test_the_consent_flow_loads_google_tag_manager_under_the_csp` (reuse the route stubbing of
           test_website_consent.py; accepting consent requests googletagmanager.com and raises no violation).
           Install the listener with `page.add_init_script` registering `securitypolicyviolation` and collect
           `console` messages containing "Content Security Policy". Same `importorskip`/`pytestmark` posture and
           the same `PRIVACYFENCE_REQUIRE_BROWSER` handling as test_website_consent.py.
      5. .github/workflows/website-build.yml: add `tests/integration/test_website_csp.py \` to the guardrail pytest list.
         .github/workflows/pages.yml: add the two scripts to the `on.push.paths` filter; in the `deploy` job add, after the
         deploy step, an `actions/checkout` step (same pinned SHA as the `build` job, no `fetch-depth`) and a step named
         `Check the security headers` running `python3 scripts/check_site_headers.py https://privacyfence.eu`.
      6. Run `ruff check .`, then build the site the way the CI does and run the tests against it:
         `pip install --require-hashes -r requirements/docs.lock.txt` (into a venv outside the repo),
         `python scripts/build_site.py --out _site --offline --docs-ref worktree` (without `--docs-ref worktree` the docs
         come from the newest stable tag and may be skipped, which would let the CSP test pass without ever seeing the docs'
         inline scripts), confirm `test -f _site/docs/index.html`, then
         `PRIVACYFENCE_SITE_DIR=_site python3 -m pytest tests/unit/test_website_*.py tests/unit/test_check_site_headers.py tests/integration/test_website_csp.py tests/integration/test_website_consent.py tests/integration/test_website_layout.py -q`
         (the steward skill explains `PRIVACYFENCE_TEST_CHROMIUM`; a SKIPPED browser test is not a pass). Keep `_site` until
         the acceptance below has run, then remove it; never commit it.
         If `test_no_page_violates_its_csp` reports a violation, stop with status=blocked naming the page and the blocked URL.
      7. Do not run the new script against the live site (the Cloudflare rules do not exist yet) and do not edit docs, ADRs or
         CHANGELOG; the last phase does.
    acceptance:
      - python3 -m pytest tests/unit/test_website_security_headers.py tests/unit/test_check_site_headers.py -q passes
      - after python scripts/build_site.py --out _site --offline --docs-ref worktree, test -f _site/docs/index.html succeeds and PRIVACYFENCE_SITE_DIR=_site python3 -m pytest tests/integration/test_website_csp.py -q passes with no test skipped
      - grep -n "test_website_csp.py" .github/workflows/website-build.yml matches
      - grep -n "check_site_headers.py https://privacyfence.eu" .github/workflows/pages.yml matches
      - python3 scripts/site_security_headers.py prints the three rule names pf-security-headers-all, pf-csp-site and pf-csp-docs
      - ruff check . passes
  - id: p6-adrs
    title: Write ADRs 0194, 0195 and 0196 and amend the index
    depends_on: [p1-atlassian-account-id, p2-account-id-rules, p3-bundle-serial, p4-windows-coverage-gate, p5-website-security-headers]
    complexity: S
    touches:
      - docs/adr/0194-jira-and-confluence-mine-rules-compare-atlassian-account-ids.md
      - docs/adr/0195-an-organization-bundle-carries-a-signed-serial-and-an-older-one-is-refused.md
      - docs/adr/0196-the-websites-security-headers-are-set-by-cloudflare-and-checked-from-the-repository.md
      - docs/adr/0016-org-config-bundle-hash-log-and-signing.md
      - docs/adr/0053-the-website-is-hosted-on-github-pages-behind-cloudflare.md
      - docs/adr/README.md
    brief: |
      Extract the plan's decisions into ADRs before the plan is deleted. Read CONTRIBUTING.md "Decisions, plans and ADRs",
      docs/adr/README.md (template, rules) and the Design and ADRs sections of docs/repo-review-low-findings-plan.md. All five
      code phases have landed; describe what the code does now (read it), not what the plan intended.
      1. Write the three files named in `touches` in the README template, Status `Accepted - <today's date>. Implemented.`,
         with Context, Decision, Alternatives considered (the "Rejected" items of the plan's D1, D2 and D4), Consequences,
         Verification (the tests and files the phases created) and Related (issues #923, #924, #926; ADR 0195 `Amends 0016`;
         ADR 0196 `Amends 0053`; ADR 0194 relates to 0117 and 0118). ADR 0196's Consequences must say that Cloudflare can drop
         the rules without a commit, that `pages.yml`'s post-deploy `Check the security headers` step is how that is caught, and
         that the `/docs/` policy allows inline scripts and styles because the docs generator emits them. ADR 0195's
         Consequences must say that a signed bundle without a serial is accepted until a serial is pinned. Link to source files
         and issues only, never to the plan.
      2. docs/adr/0016-...md and docs/adr/0053-...md: add one line to each Status section pointing forward
         (`Amended by [0195](0195-...md): ...` / `Amended by [0196](0196-...md): ...`); change nothing else in them.
      3. docs/adr/README.md: add three rows after the 0193 row (`Accepted; amends 0016` / `Accepted; amends 0053` / `Accepted`)
         and change the 0016 and 0053 rows' status to end `; amended by 0195` and `; amended by 0196`.
    acceptance:
      - ls docs/adr/0194-*.md docs/adr/0195-*.md docs/adr/0196-*.md lists three files
      - grep -c "0194\|0195\|0196" docs/adr/README.md prints at least 5
      - grep -n "amended by 0195" docs/adr/README.md matches the 0016 row and grep -n "amended by 0196" docs/adr/README.md matches the 0053 row
      - grep -rn "repo-review-low-findings" docs/adr prints nothing
      - ruff check . passes
  - id: p7-retire-plan
    title: Update the reference docs and the changelog, and delete the plan
    depends_on: [p6-adrs]
    complexity: M
    touches:
      - docs/approvals-and-policy.md
      - docs/connector-qa.md
      - docs/org-mode-setup-guide.md
      - docs/configuration-reference.md
      - docs/testing-policy.md
      - CHANGELOG.md
      - docs/repo-review-low-findings-plan.md
      - docs/repo-review-low-findings-plan-manual-steps.html
    brief: |
      Retire the plan. The ADRs exist (p6). Read CONTRIBUTING.md "Decisions, plans and ADRs" and the Design section of
      docs/repo-review-low-findings-plan.md before you delete it.
      1. Reference docs, describing today's behaviour (state the new behaviour; link the ADR for the why, never retell it):
         - docs/approvals-and-policy.md (~lines 401-403) and docs/connector-qa.md (~lines 304, 345, 721-733): "you are the
           reporter / assignee / author" now means your Atlassian account is the reporter, assignee or author, compared by account
           id, never by a name or email; `grep -rn "i_am_reporter\|i_am_assignee\|i_am_author" docs` and fix every sentence that
           implies an email or name match. Do not hand-edit docs/always-allow-rules-reference.md: it is generated by
           scripts/generate_always_allow_reference.py from labels that did not change.
         - docs/org-mode-setup-guide.md section 6 "Signing key and bundle" (~lines 219-260) and its flag table (~line 303):
           document `--serial`, its default, the pin file `org_config_serial.txt`, that an older serial is refused, and that
           deleting that file (or both pin files, to rotate the key and reset the serial) allows a deliberate rollback. Add a
           troubleshooting row for the rollback refusal where that file lists startup refusals (~line 1149; search for "refusing to
           start"). docs/configuration-reference.md: add the `serial` bundle field wherever `signing_public_key` / `signature` are
           listed (`grep -n "signing_public_key" docs/configuration-reference.md`).
         - docs/testing-policy.md: add `test_website_csp.py` to the browser-marker list (~lines 33-36) and to the website-build
           description (~lines 167-172); update the `platform-windows` row (~line 60) and the ratchet paragraph (~lines 83-86)
           to say the Windows job now gates `WINDOWS_MODULE_FLOORS` through `check_coverage_floor.py --platform windows`.
      2. CHANGELOG.md under `## [Unreleased]` (never a version heading; match the neighbouring entries' style), three lines:
         (a) the Jira "if I'm reporter / assignee" and Confluence "if I'm the author" rules match your Atlassian account id
         instead of your email in a display name, and the Confluence rule now works; installs with Jira pick the id up on their
         next start, installs with only Confluence need to sign in to Atlassian once more. (b) organization bundles signed with
         `build_org_bundle.py --sign-key` carry a `serial`, and an install refuses a bundle with a lower serial than it has accepted
         (delete `org_config_serial.txt` to go back on purpose). (c) privacyfence.eu sends HSTS, a Content-Security-Policy,
         nosniff and a Referrer-Policy. The Windows gate and tests are internal and need no line.
      3. Delete docs/repo-review-low-findings-plan.md and docs/repo-review-low-findings-plan-manual-steps.html. Then
         `grep -rIn "repo-review-low-findings" . --exclude-dir=.git --exclude-dir=.venv` must print nothing.
      4. Run the full definition of done with `/dod` (profile `verify.dod`, including `check_coverage_floor.py`).
    acceptance:
      - grep -n "org_config_serial.txt" CHANGELOG.md docs/org-mode-setup-guide.md prints a line in each file
      - sed -n '/## \[Unreleased\]/,/^## \[[0-9]/p' CHANGELOG.md contains "Atlassian account", "serial" and "Content-Security-Policy"
      - grep -rn "repo-review-low-findings" . -I --exclude-dir=.git --exclude-dir=.venv prints nothing
      - test ! -e docs/repo-review-low-findings-plan.md
      - python3 -m pytest tests/unit -q passes
      - ruff check . passes
```
