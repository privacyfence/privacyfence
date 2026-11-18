# Plan: Let PrivacyFence read audiences from the upstream MCP server (issue 1153)

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** The operator adds `audiences_tool: <tool name>` (and optionally `audiences_refresh_seconds`) to an upstream in the operator file and restarts. PrivacyFence then calls that tool itself at startup and every five minutes, with a service claim, and the upstream answers which addresses hold which audiences. A person's audiences on that upstream come from that table; the `admin_config.json` group map and the group-membership approvals are no longer used for it. **Admin → Upstream MCP servers** shows the source, its state, the last refresh and counts, and `privacyfence_status` shows the state.

**Scope:** One opt-in per upstream; upstreams without it behave exactly as today. New: two operator-file keys, a pure module `upstream_audiences.py`, a refresh task per opted-in upstream, two audit decisions, an Admin card, a `list_audiences` tool on the reference upstream, the contract text and two ADRs. Not changed: the tool list (still fetched once), the claim format (`claim_version` 1), scoped-token narrowing, the knowledge hub itself.

**Security:** The upstream becomes the authority for who holds its own audiences, with no per-person approval and no addition cap (the owner's decision). It cannot reach another upstream's audiences: every table is keyed by upstream id (3.4). The call is refused at start without a signed claim (3.1). A service claim gets data only on a `tools/call` of the declared audiences tool (3.6). A snapshot older than three refresh intervals gives nobody any audience (3.3). No address reaches the audit log, the Admin page or `privacyfence_status`.

**Review focus:** the upstream being free to grant any audience, `all` included, with no approval and no cap (3.2, ADR 0325); existing scoped tokens' ticked audiences being reinterpreted by name when an upstream switches source (3.4); the stale rule and the revocation lag it allows (3.3); keeping an opted-in upstream out of `all_group_audiences`, so the sync tool and the approvals never act on it (3.4); the contract change for a service claim (3.6, ADR 0326); dropping unusable addresses instead of rejecting the whole answer (3.2).

## 1. Goal

An operator whose upstream already knows who may see what (a knowledge hub with its own teams) no longer has to copy that into `upstream_mcp.<id>.groups` in `admin_config.json` and keep it in step by hand. PrivacyFence asks the upstream for its audiences and their members, on a schedule, and uses the answer exactly where it uses the group map today. PrivacyFence stays generic: the upstream only has to implement one tool with the result shape of 3.2. Issue: https://github.com/privacyfence/privacyfence/issues/1153.

The issue text proposed a catalogue with approvals. The owner then decided, on the board (ask `a-442495c9584310d9`, answered 2026-11-18): **model `member`** (the upstream returns each audience's members and is the authority for them), **approvals `none`** (no administrator approval per person or per mapping for an audience from this source) and **refresh `interval`** (at startup, then on an interval the operator sets, default five minutes). This plan follows those answers.

## 2. Current state

- An upstream is declared in the operator file and parsed by `upstream_mcp_config._parse_one` (`src/privacyfence/upstream_mcp_config.py:282`); the allowed keys are `_UPSTREAM_KEYS` (`:59`), the dataclass is `UpstreamServer` (`:85`). `email_for_visibility` shows the refusal pattern with `identity_claim: off` (`:335`). `declaration_digest` (`:97`) pins what an ADR 0300 approval depends on.
- A person's audiences: `upstream_mcp_runtime.audiences_for_email` (`src/privacyfence/upstream_mcp_runtime.py:108`) = `lookup_email`, verified check, then `resolver.audiences_for(address, GroupMapStore.group_audiences(upstream_id))`. It is called by `caller_for` (`:153`, both for a person and for a scoped token's creator) and `held_audiences` (`:194`, used by the New token form in `web/routes_settings.py:908,995` and `web/server.py:335`).
- `all_group_audiences` (`:117`) merges every upstream's group map; the sync tool (`connectors/group_membership.py:76`) stores only groups it names, and the Admin page's pending approvals (`web/routes_admin.py:341,356`) are derived from it.
- `lifespan` (`:286`) starts one `_fetch_tool_list` task per upstream (backoff 5 s doubling to 300 s, `_sleep` patchable, `:35-38`). There is no periodic task.
- `UpstreamMcpClient.call_tool` (`src/privacyfence/upstream_mcp_client.py:110`) opens one session per call, signs every request with `IdentityClaimAuth` (binds `req` on a `tools/call` whatever the caller), caps the text at `MAX_RESULT_CHARS` (1 000 000) but not `structured_content`, and raises `UpstreamMcpClientError(kind)` with `unreachable`, `too_large`, `tool_error`, `claim_failed`. `list_tools` already uses `IdentityCaller(sub="privacyfence", principal_type="service")` (`:107`).
- `status()` (`:204`) and `admin_rows()` (`:228`) feed `privacyfence_status` (`web/mcp_dispatch.py:705`) and the Admin page (`web/routes_admin.py:386`, rendered by `upstreamCardHtml` in `src/privacyfence/settings_window_html.py:2548`).
- Contract: a service claim "gets no data" (`docs/identity-claim.md:66`, `docs/upstream-mcp.md:269-270`); the reference upstream returns nothing to a service claim (`scripts/upstream_mcp_reference_server.py:80`).
- ADR 0298 (audiences from group membership), ADR 0299 (the sync tool and its approvals) and ADR 0323 (which keeps 0298/0299 "for scoped tokens only") are the decisions this plan amends.
- Test patterns: `tests/unit/test_upstream_mcp_runtime.py` (`configured` fixture, `_FlakyClient`, `TestLifespan` with a fake `_sleep`), `tests/unit/test_upstream_mcp_config.py`, `tests/unit/test_upstream_mcp_reference_server.py` (`_mint`, `_post`, `_call`), `tests/unit/test_settings_window_html.py::TestOrgAdminSections` (`test_upstreams_ready_lists_tools_and_groups`, `_render_org`, `_org_state`), `tests/integration/test_upstream_mcp_org.py` (`stack` fixture).

## 3. Design

### 3.1 Operator file keys

Two new optional keys per upstream, added to `_UPSTREAM_KEYS`:

| Key | Rule | Error text (`<source>: upstreams[<i>].<key>: …`) |
|---|---|---|
| `audiences_tool` | a string matching `_TOOL_RE`; not a key of the same upstream's `tools`; not allowed with `identity_claim: off` | not a string or no match: `must be 1-48 letters, digits, underscores or hyphens`; in `tools`: `is also declared under tools; the audiences tool is called by PrivacyFence only`; with claim off: `needs identity_claim required` |
| `audiences_refresh_seconds` | a whole number (`type(...) is int`) from 30 to 3600, default 300; only together with `audiences_tool` | out of range or not an int: `must be a whole number from 30 to 3600`; without `audiences_tool`: `is only allowed with audiences_tool` |

`UpstreamServer` gains, after `email_for_visibility`: `audiences_tool: str | None = None` and `audiences_refresh_seconds: int = 300`. `declaration_digest` does not change: the source decides who holds an audience, not what a tool does, so no ADR 0300 approval depends on it (recorded in ADR 0325).

### 3.2 The result the upstream returns

PrivacyFence calls `tools/call` with name `audiences_tool` and arguments `{}`. The answer is a JSON object, as `structured_content` or, when there is none, as the text content:

```json
{"version": 1, "audiences": {"mgmt": ["ann@example.com", "bob@example.com"], "finance": ["bob@example.com"]}}
```

New module `src/privacyfence/upstream_audiences.py` (pure, no I/O, no audit, no module-level state):

- Constants: `RESULT_VERSION = 1`, `MAX_AUDIENCES = 200`, `MAX_ENTRIES = group_membership.MAX_ENTRIES` (20000), `MAX_ANSWER_CHARS = 1_000_000` (defined here, equal to `upstream_mcp_client.MAX_RESULT_CHARS`; the module must not import `upstream_mcp_client`, which pulls in `mcp` and `httpx2`), `STALE_FACTOR = 3`. Its only package imports are `group_membership`.
- `class AudiencesResultError(Exception)` with `reason: str` (one of the fixed strings below; never quotes the answer).
- `@dataclass(frozen=True) class AudienceSnapshot`: `by_email: Mapping[str, frozenset[str]]`, `audience_count: int`, `people_count: int`, `dropped: int`, `digest: str` (hex SHA-256 of the canonical JSON of the sorted list of `[audience, address]` pairs; used only to see that the table changed, never logged).
- `parse_result(structured: dict | None, text: str) -> AudienceSnapshot`:
  1. If `structured` is a dict, use it; its `len(json.dumps(structured))` must be ≤ `MAX_ANSWER_CHARS`, else `AudiencesResultError("the answer is larger than 1 MB")`. Otherwise `json.loads(text, object_pairs_hook=<a local copy of the repeated-key hook of upstream_mcp_config._json_pairs>)`; a decode error or a repeated key is `"the answer is not a JSON object"` / `"the answer repeats a key"`. Never merge the two.
  2. The value must be a dict whose keys are exactly `{"version", "audiences"}`, else `"the answer must hold exactly version and audiences"`. `version` must be the int 1 (`type(...) is int`), else `"version must be 1"`.
  3. `audiences` must be a dict with at most `MAX_AUDIENCES` keys (`"at most 200 audiences"`); each key must satisfy `group_membership.AUDIENCE_RE.fullmatch` (`"an audience name must be 1-63 characters: lowercase letters, digits, underscores or hyphens, starting with a letter or digit"`); each value a list of strings (`"each audience must list addresses as strings"`); the total list length at most `MAX_ENTRIES` (`"at most 20000 entries"`).
  4. Each address goes through `group_membership.lookup_email`; `None` is dropped and counted in `dropped`, never raised. Duplicates collapse. `by_email[address]` is the union of the audiences that list it. An audience with no usable address still counts in `audience_count`. The name `all` is an ordinary name here: the upstream may list anyone under it, which fits `approvals: none` (ADR 0325 says so). `people_count = len(by_email)`.
- `class UpstreamAudienceSource(refresh_seconds: int, *, clock=time.monotonic, wall=time.time)`, guarded by a `threading.Lock` (the refresh task runs on the event loop, readers run in request handlers and worker threads):
  - `begin() -> int`: the next fetch sequence number (1, 2, …), taken before the call is sent.
  - `apply(snapshot: AudienceSnapshot, seq: int) -> bool`: ignored (returns `False`) when `seq` is lower than the last applied or failed `seq`, so an older fetch that finishes last never overwrites a newer one; otherwise stores it with `clock()` and `wall()`, clears `last_error` and returns `True`. The lock is never held across an `await` (the methods are plain functions).
  - `record_failure(kind: str, seq: int) -> bool`: same `seq` rule; keeps the snapshot and sets `last_error = kind` (`unreachable`, `too_large`, `tool_error`, `claim_failed` or `invalid`).
  - `state() -> str`: `"waiting"` before the first `apply`; `"stale"` when `clock() - fetched_monotonic > STALE_FACTOR * refresh_seconds`; else `"ready"`.
  - `audiences_for(address: str) -> frozenset[str]`: `frozenset()` unless `state() == "ready"`, else `by_email.get(address, frozenset())`.
  - `summary() -> dict[str, object]`: `{"state", "fetched_at" (wall epoch float or None), "audience_count", "people_count", "dropped", "last_error"}`; counts are 0 before the first snapshot.

### 3.3 Refresh, staleness and fail-closed

- `upstream_mcp_runtime._Entry` gains `audiences: UpstreamAudienceSource | None = None`; `configure` sets it for each upstream with `audiences_tool`.
- New `async def refresh_audiences(upstream_id: str) -> bool` in `upstream_mcp_runtime`: one attempt, public so a test can force a refresh. It takes `seq = source.begin()`, calls `entry.client.call_tool(upstream.audiences_tool, {}, IdentityCaller(sub="privacyfence", principal_type="service", tools=(upstream.audiences_tool,)))`, then `upstream_audiences.parse_result(result.structured, result.text)`, then `source.apply(snapshot, seq)`. On `UpstreamMcpClientError` it calls `source.record_failure(error.kind, seq)`; on `AudiencesResultError` `record_failure("invalid", seq)`; a last `except Exception` also calls `record_failure("invalid", seq)` and logs with `exc_info`, so the loop never dies silently and logs `Upstream %s answered its audiences tool with an unusable result: %s` (the fixed reason). It returns whether it succeeded; when `apply`/`record_failure` ignore an older `seq` it returns `False`, writes no audit entry and leaves `audiences_failing` and `audiences_digest` unchanged. On success it logs at info `Upstream %s audiences refreshed: %d audiences, %d people, %d dropped (jti %s)`.
- New `async def _refresh_audiences_loop(entry: _Entry) -> None`: forever: `ok = await refresh_audiences(entry.upstream.id)`; on success `await _audiences_sleep(refresh_seconds)` and reset the backoff to `BACKOFF_START_SECONDS`; on failure `await _audiences_sleep(min(backoff, refresh_seconds))` and double the backoff up to `BACKOFF_MAX_SECONDS`. `_audiences_sleep = asyncio.sleep` is a module-level name of its own next to `_sleep`, so tests of the tool-list backoff and of this loop patch them separately. It does not wait for the tool list (`entry.listed`). The upstream should list the audiences tool in `tools/list`: `mcp`'s `ClientSession.call_tool` re-reads the tool list to validate a result and logs a warning for an unlisted tool.
- `lifespan` starts one such task per entry with a source, next to the tool-list tasks, and cancels them the same way.
- **Stale rule:** a snapshot older than 3 × `audiences_refresh_seconds` gives nobody any audience until the next success (fail closed). Before the first success nobody holds an audience. Worst-case revocation lag: one interval normally; up to 3 × the interval plus the 60 s call timeout while the upstream is down (3 h 1 min at 3600 s). Documented in `docs/upstream-mcp.md`.
- **Audit** (`AuditEntry` from `audit_log`, through `get_audit_logger()`; the task runs outside any request, so the entries land in the server's own log, as `org_config_startup` does; `connector` = upstream id, `tool` = the audiences tool, `tool_name` = `Read audiences`, `sender` = `privacyfence`, `request_id` = `""`, `claim_jti` = the claim's `jti` (the `upstream_forwarded` convention, `audit_log.py:439`) or `""`, `auto_accept_rule` = `""`, `latency_seconds`, never an address). `refresh_audiences` writes:
  - `upstream_audiences_applied`, summary `Audiences from <id> applied: <a> audience(s), <p> people, <d> address(es) dropped`, on the first success, on a success whose `digest` differs from the last applied one (so a swap of one person for another counts), and on the first success after a failure;
  - `upstream_audiences_failed`, summary `Audiences from <id> not refreshed: <kind>`, on a failure when the previous attempt did not fail (the very first attempt included).
  An audit write failure is logged and never changes the outcome (the `try/except` of `connectors/group_membership.py:101-118`). Add both decisions to the decision comment in `src/privacyfence/audit_log.py:164` with one sentence: they record the audiences tool refresh, ADR 0325, and neither counts as an approval.

### 3.4 Where the source is used

- `audiences_for_email(upstream_id, email, verified)`: unchanged guard (`lookup_email`, `verified`); then, when the entry has a source, `return source.audiences_for(address)` and never consult the resolver or `GroupMapStore`. Everything that calls it (`caller_for` for a person and for a scoped token's creator, `held_audiences`) follows unchanged, so a token still holds `creator's audiences now ∩ ticked`.
- `all_group_audiences()` skips every entry with a source. So the sync tool stores no member of a group mapped only for such an upstream, and **Admin → Group membership** shows no pending row for it. Upstreams without a source are untouched.
- `configure()` logs once per opted-in upstream whose `groups.group_audiences(id)` is non-empty: `admin_config.json: upstream_mcp.<id>.groups is ignored: <id> takes its audiences from its audiences tool`.
- Isolation: each source belongs to one entry, keyed by upstream id; no code path reads another upstream's source.

### 3.5 Status and the Admin page

- `status()` rows gain `"audiences_source"`: `"upstream"` or `"groups"`, and, only for `"upstream"`, `"audiences_state"`: the source's `state()`.
- `admin_rows()` rows gain `"audiences_source"`, `"audiences_tool"` (the name or `None`), `"audiences_refresh_seconds"` (int or `None`), `"audiences"` (the source's `summary()`, or `None` for `"groups"`) and `"groups_ignored"` (the number of mapped groups ignored, 0 for `"groups"`); `"groups"` is `[]` for an upstream source. Never an address.
- `upstreamCardHtml` (`settings_window_html.py`): for `audiences_source === 'upstream'` the third card is titled `<label>: audiences` instead of `<label>: groups and audiences`, with rows:
  - `Source`: `From the server's <tool> tool, every <n> seconds. No approvals: the server decides who holds each audience.`
  - `State`: `Waiting for the first answer. Nobody holds an audience on this server yet.` / `Ready: <a> audience(s), <p> people.` (plus ` <d> address(es) were unusable and ignored.` when `dropped > 0`) / `Stale: no answer for over three refresh intervals. Nobody holds an audience on this server until it answers.`
  - `Last refresh`: the UTC time in the format of `membershipTimeText` (move nothing; call it), or `Never`.
  - `Last error`: shown only when `last_error` is non-empty, with the text of this map: `unreachable` → `The server could not be reached.`, `too_large` → `The answer was larger than 1 MB.`, `tool_error` → `The server answered with an error.`, `claim_failed` → `The claim could not be signed.`, `invalid` → `The answer did not have the expected shape.`
  - The page intro in `renderUpstreams` (`'. A change to the declaration needs a restart; a change to the groups in admin_config.json applies without one.'`) becomes `'. A change to the declaration needs a restart; a change to the groups in admin_config.json applies without one, except for a server that takes its audiences from its own tool.'`
  When `groups_ignored > 0`: `Groups in admin_config.json`: `<n> group(s) mapped for this server are ignored.`
  Everything goes through `esc`. Upstreams without a source render exactly as today.

### 3.6 The contract (docs/identity-claim.md, ADR 0326)

- The `service` principal row: `PrivacyFence's own requests: the startup tool listing and, when the operator declared audiences_tool, the call of that tool.` with `sub` `privacyfence`, `audiences: []`.
- New section `## The audiences tool (opt-in)`: when the operator declared `audiences_tool: <name>`, PrivacyFence calls `tools/call` of `<name>` with `{}` and a service claim whose `req` is bound to that call (its `tools` is `[<name>]`, for information only, as everywhere: never decide on it). The upstream answers with the shape of 3.2 **only** when the claim verified, `principal_type` is `service`, `sub` is `privacyfence`, `req` matched and `params.name` is its own audiences tool. Every other request with a service claim still gets no data. The answer holds audience names and addresses only, never documents. Addresses should be the lower-cased form of the identity provider's verified address; PrivacyFence applies `lookup_email` (ASCII only) and ignores the rest. The limits and the fail-closed rules of 3.2 and 3.3 are stated there.
- The `## Audiences` section (`docs/identity-claim.md:72`) gains: `For an upstream that declared audiences_tool, a person's audiences come from that tool's answer instead of the groups.`
- Versioning: the opt-in keeps `claim_version` 1 (an operator opt-in per upstream, like `email_for_visibility`); a later change to the result shape bumps the result's own `version`.
- `docs/upstream-mcp.md` "For upstream implementers": replace the service bullet with `A service claim gets no data, except on a tools/call of the audiences tool the operator declared (see the contract).`

### 3.7 The reference upstream

- `scripts/upstream_mcp_reference_server.py`: `build_app(..., audience_members: dict[str, list[str]] | None = None)`; `_build_server(documents, proposals=None, audience_members=None)` lists a fifth tool `list_audiences` (description `PrivacyFence only: who holds each audience.`, schema `{"type": "object", "properties": {}, "required": []}`) and answers it with `structured_content={"version": 1, "audiences": audience_members or {}}` and the same JSON as text, **only** when the claim's `principal_type` is `service` and its `sub` is `privacyfence`; any other caller gets `is_error=True` with text `list_audiences answers only PrivacyFence's service claim`. `build_app` passes the `audience_members` dict through by reference, never a copy (the documents are copied; this is not), and it is read on every call, so a test can change it between refreshes. The module docstring and `docs/upstream-mcp.md` "Reference upstream" say so.

### 3.8 Rejected alternatives (for ADR 0325 and 0326)

- **Push** (the upstream calls a PrivacyFence scoped-token tool, as the sync tool does, ADR 0299): the upstream would hold a PrivacyFence credential, a stolen one rewrites that upstream's audiences, and staleness could only be guessed from silence.
- **Delegate** (send only `email`, let the upstream decide): a scoped token's claim carries no email (ADR 0310), so tokens could not be resolved or narrowed.
- **Catalogue with approvals** (the issue's first proposal: the upstream lists audience names, an administrator maps groups and approves people): the owner chose `member` and `approvals: none`.
- **Reject the whole answer on one bad address**: one non-ASCII address would stop every refresh and, three intervals later, empty everyone's audiences.
- **Keep the last snapshot forever when the upstream is down**: a removal at the upstream would never reach PrivacyFence during an outage; the 3 × interval rule bounds the lag.
- **Pin the source in `declaration_digest`**: it changes who holds an audience, not what a tool does; an ADR 0300 approval stays valid.
- **Use both sources (union with the group map)**: two answers to one question, the one that needs hand edits would silently widen.

## 4. ADRs

- **0325 — An upstream can be the source of its own audiences.** Status Accepted; amends 0298, 0299, 0319 and 0323; relates to 0285, 0299, 0300, 0310. Decision: 3.1, 3.3, 3.4 (the opt-in, the member table, no approvals and no `max_additions` cap or parking, refresh and the 3 × stale rule, the group map and approvals not used for that upstream, not in the declaration digest, the tool list still fetched once). Consequences: the revocation lag; an existing scoped token's `upstream_audiences` for that upstream are reinterpreted by name against the new source; a compromised upstream can widen access only to its own data.
- **0326 — A service claim may call the declared audiences tool.** Status Accepted; amends 0297; relates to 0286, 0310, 0325. Decision: 3.6 (when an upstream may answer a service claim with data: `principal_type`, `sub`, `req` and the tool name, never `tools`; what it returns; `claim_version` stays 1).
- One Status line each in 0297, 0298, 0299, 0319 and 0323: `Amended by [ADR 0325](…)` / `Amended by [ADR 0326](…)`.

## 5. Manual steps

### Before implementation

None.

### After implementation

1. **ma1-reference-upstream-by-hand: run the reference upstream with an audiences tool against a real organization server** (about 20 minutes).
   1. On a development organization server (see [docs/dev-vs-live-setup.md](https://github.com/privacyfence/privacyfence/blob/feature/upstream-audiences/docs/dev-vs-live-setup.md)), start the reference upstream as [docs/upstream-mcp.md → Reference upstream](https://github.com/privacyfence/privacyfence/blob/feature/upstream-audiences/docs/upstream-mcp.md#reference-upstream) says.
   2. In the operator file add `"audiences_tool": "list_audiences", "audiences_refresh_seconds": 30` to the `ref` upstream and restart PrivacyFence.
   3. Open **Admin → Upstream MCP servers**: the third card is `Reference upstream: audiences`, state `Ready`, with counts and a last refresh time, and no address anywhere on the page.
   4. Stop the reference upstream for more than 90 seconds and reload the page: the state reads `Stale` and `Last error` reads `The server could not be reached.`; restart it and within 30 seconds it reads `Ready` again.
   5. Report back one line for the pull request: `ma1: pass` or `ma1: fail - <what you saw>`.

## 6. Risks and open questions

- **`call_tool` changes shape.** If `UpstreamMcpClient.call_tool` no longer returns `CallResult` with `text`, `structured` and `jti`, or `IdentityClaimAuth` stops binding `req` for a service caller, the brief is wrong: stop with `status=blocked`.
- **The reference server cannot list or answer a fifth tool** under the installed `mcp` lowlevel server (an input-schema validation error for `{}`): stop with `status=blocked` and quote the error; do not change the client.
- **Existing tests assert exact `status()` or `admin_rows()` dicts** (`tests/unit/test_upstream_mcp_runtime.py::TestStatus`, `TestAdminRows`). Add the new keys to their expectations; never drop an assertion.
- **`audit_log` validates decisions** somewhere other than the comment: if a test fails on an unknown decision, add the two names where that test says; if that place is a protected path, report `protected-change:`.
- **A parallel branch takes ADR 0325 or 0326** before this merges: renumber in the retirement phase and say so in `deviations.md`.
- The architect review's remaining non-blocking point: an operator who switches an upstream from groups to the upstream source reinterprets existing tokens' ticked audiences by name; only documented (ADR 0325), no migration.

## Implementation manifest

```yaml
plan_slug: upstream-audiences
feature_branch: feature/upstream-audiences
tracking_issue: 1153
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-reference-upstream-by-hand
    title: Run the reference upstream with list_audiences against a development organization server and watch Ready, Stale and recovery on the Admin page
    why: CI does not render the Admin page against a live upstream that stops and starts, or show what an administrator reads.
verify_after_merge:
  - python3 -m pytest tests/unit/test_upstream_audiences.py tests/unit/test_upstream_mcp_config.py tests/unit/test_upstream_mcp_runtime.py tests/unit/test_upstream_mcp_reference_server.py -q
final_checks:
  - "`tld plan deviations check --file docs/1153/deviations.md --plan docs/1153/plan.md --all` exits 0"
  - ADRs 0325 and 0326 exist, are Accepted, and are in docs/adr/README.md
  - CHANGELOG.md has an entry under `## [Unreleased]` and no new version heading
phases:
  - id: p1-config-and-parser
    title: The two operator-file keys and the pure upstream_audiences module
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_config.py
      - src/privacyfence/upstream_audiences.py
      - tests/unit/test_upstream_mcp_config.py
      - tests/unit/test_upstream_audiences.py
    brief: |
      Implement design sections 3.1 and 3.2 of docs/1153/plan.md exactly (names, limits and error texts are fixed there). The pattern for the new key and its tests is commit cec64275 (email_for_visibility).
      1. src/privacyfence/upstream_mcp_config.py: add "audiences_tool" and "audiences_refresh_seconds" to _UPSTREAM_KEYS; add the two UpstreamServer fields after email_for_visibility; in _parse_one, after the email_for_visibility checks, validate both keys with the rules and messages of the 3.1 table (the `tools` check compares against the names in the parsed `tools` tuple). Pass them to UpstreamServer. Leave declaration_digest unchanged. Update the module docstring's key list in one sentence.
      2. Create src/privacyfence/upstream_audiences.py with the constants, AudiencesResultError, AudienceSnapshot, parse_result and UpstreamAudienceSource of 3.2: module docstring (what it is, ADR 0325), type hints, no I/O, no logging, no module-level mutable state. The duplicate-key hook is a local copy of upstream_mcp_config._json_pairs (do not import the private one); do not import upstream_mcp_client.
      3. Tests: extend tests/unit/test_upstream_mcp_config.py next to the email_for_visibility tests with test_audiences_tool_defaults_to_none, test_audiences_tool_is_read_with_the_default_interval, test_audiences_tool_refuses_a_bad_name, test_audiences_tool_refuses_a_declared_tool_name, test_audiences_tool_needs_the_claim_on, test_audiences_refresh_seconds_bounds (29 and 3601 and True refused, 30 and 3600 read), test_audiences_refresh_seconds_needs_the_tool, and in TestDeclarationDigest test_declaration_digest_ignores_the_audiences_source. Create tests/unit/test_upstream_audiences.py covering every branch of parse_result (structured preferred over text, text fallback, repeated key, not an object, extra key, version 2 and "1" and True, audience name bad, 201 audiences, 20001 entries, non-list, non-string, oversized structured, a non-ASCII and a malformed address dropped and counted, duplicates collapsed, union across audiences, an audience with no usable address counted) and of UpstreamAudienceSource with a fake clock (waiting, ready, stale at exactly 3x+1, a failure keeps the snapshot and sets last_error, apply clears it, an older seq is ignored by apply and record_failure, the digest changes on a swap of one address for another with equal counts, audiences_for empty unless ready, summary before and after the first snapshot).
      4. Do not touch upstream_mcp_runtime.py or any other module.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_audiences.py tests/unit/test_upstream_mcp_config.py -q passes
      - python3 -m pytest tests/unit/test_upstream_audiences.py -q --cov=privacyfence.upstream_audiences --cov-branch --cov-report=term-missing reports 100%
      - ruff check . and python3 -m mypy src/privacyfence pass
      - grep -n "is also declared under tools; the audiences tool is called by PrivacyFence only" src/privacyfence/upstream_mcp_config.py matches
  - id: p2-reference-upstream
    title: list_audiences on the reference upstream
    depends_on: []
    complexity: S
    touches:
      - scripts/upstream_mcp_reference_server.py
      - tests/unit/test_upstream_mcp_reference_server.py
    brief: |
      Implement design section 3.7 of docs/1153/plan.md exactly.
      1. scripts/upstream_mcp_reference_server.py: add the audience_members parameter to build_app and _build_server, list the list_audiences tool, and answer it as 3.7 says (structured_content and the same JSON as text for a service claim; is_error with the fixed text for any other principal). Keep the existing four tools unchanged. In tests/unit/test_upstream_mcp_reference_server.py:145 add "list_audiences" to the expected tool-name set (do not drop the assertion). Update the module docstring (lines 8-9): a service claim lists the tools and may call list_audiences, and gets no documents.
      2. Tests in tests/unit/test_upstream_mcp_reference_server.py, following test_a_service_claim_lists_tools_and_gets_no_documents and its _mint/_post/_call helpers: test_list_audiences_answers_a_service_claim_with_the_table, test_list_audiences_refuses_a_person, test_list_audiences_refuses_a_scoped_token, test_list_audiences_reads_the_table_on_every_call (mutate the dict between two calls), test_a_service_claim_still_gets_no_documents_from_search.
      3. Stop condition: if the installed mcp lowlevel server refuses the call because of the empty schema or an unlisted tool, stop with status=blocked and quote the error.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_reference_server.py -q passes
      - ruff check . passes
      - grep -n "list_audiences answers only PrivacyFence's service claim" scripts/upstream_mcp_reference_server.py matches
  - id: p3-runtime
    title: Refresh task, audit entries and the source inside the runtime
    depends_on: [p1-config-and-parser]
    complexity: M
    touches:
      - src/privacyfence/upstream_mcp_runtime.py
      - src/privacyfence/audit_log.py
      - tests/unit/test_upstream_mcp_runtime.py
    brief: |
      Implement design sections 3.3, 3.4 and the status()/admin_rows() part of 3.5 of docs/1153/plan.md exactly.
      1. src/privacyfence/upstream_mcp_runtime.py: _Entry.audiences; configure builds an UpstreamAudienceSource(upstream.audiences_refresh_seconds) for each upstream with audiences_tool and logs the ignored-map warning of 3.4 once per such upstream; audiences_for_email, all_group_audiences, status and admin_rows as 3.4 and 3.5 say; refresh_audiences (public, async, one attempt, the audit entries of 3.3 written through a private _audit_audiences helper with the try/except of connectors/group_membership.py:101-118) and _refresh_audiences_loop using a new module-level _audiences_sleep = asyncio.sleep, BACKOFF_START_SECONDS and BACKOFF_MAX_SECONDS; lifespan starts one loop task per entry with a source and cancels them with the others. Keep the per-call "last attempt failed" and "last applied counts" on the _Entry (fields audiences_failing: bool = False, audiences_digest: str | None = None). Rewrite the module docstring's "There is no periodic refresh" sentence: the tool list is never refreshed; an upstream with audiences_tool has its audiences refreshed on an interval (ADR 0325).
      2. src/privacyfence/audit_log.py: add upstream_audiences_applied and upstream_audiences_failed to the decision comment at lines 162-166 with the sentence of 3.3. Change nothing else there.
      3. Tests in tests/unit/test_upstream_mcp_runtime.py, using a new fixture configured_with_source next to configured (upstream kh declared with "audiences_tool": "list_audiences" and wiki on groups; leave configured and TestLifespan untouched, they iterate every entry) and a fake client class next to _FlakyClient whose call_tool returns upstream_mcp_client.CallResult or raises UpstreamMcpClientError: TestAudiencesFromUpstream with test_a_person_holds_the_audiences_the_upstream_lists, test_nobody_holds_an_audience_before_the_first_answer, test_a_stale_snapshot_gives_no_audience (replace entry.audiences with UpstreamAudienceSource(300, clock=fake_clock) and advance fake_clock past 900), test_a_failed_refresh_keeps_the_last_snapshot, test_the_group_map_is_not_used_for_an_upstream_source (a group map entry that would grant an audience grants nothing), test_all_group_audiences_skips_an_upstream_source, test_an_upstream_table_never_reaches_another_upstream (A's table, B on groups: B's caller_for and held_audiences unchanged), test_a_scoped_token_holds_the_intersection_with_the_upstream_source, test_the_service_claim_names_only_the_audiences_tool (record the tool, arguments and caller the fake client receives: list_audiences, {}, sub privacyfence, principal_type service, tools (list_audiences,)), test_an_invalid_answer_is_recorded_as_invalid, test_audit_entries_on_first_success_change_failure_and_recovery (a swap with equal counts writes applied; an unchanged answer writes nothing; assert the decisions in order and that no entry's summary contains "@"), test_the_ignored_group_map_is_logged_once (caplog). In TestLifespan: test_the_audiences_loop_sleeps_the_interval_after_success_and_backs_off_after_failure (fake _audiences_sleep records delays; with refresh 300, a client that fails twice then succeeds, and a task cancelled after the fourth sleep, the delays are exactly [5.0, 10.0, 300, 300]: the two failure sleeps are min(backoff, 300) with backoff 5 then 10, the success sleep is 300, and the next success sleep is 300 again) and test_the_audiences_loop_does_not_wait_for_the_tool_list. Extend TestStatus and TestAdminRows expectations with the new keys for both sources.
      4. Do not touch upstream_mcp_client.py, connectors/group_membership.py, web/routes_admin.py or web/server.py: they follow through all_group_audiences, status and admin_rows.
    acceptance:
      - python3 -m pytest tests/unit/test_upstream_mcp_runtime.py tests/unit/test_group_membership.py tests/unit/connectors/test_group_membership_connector.py -q passes
      - python3 -m pytest tests/unit/test_upstream_mcp_runtime.py -q --cov=privacyfence.upstream_mcp_runtime --cov-branch --cov-report=term-missing lists none of the lines of refresh_audiences, _refresh_audiences_loop, _audit_audiences, and the new branches of audiences_for_email, all_group_audiences, status, admin_rows and configure under Missing
      - ruff check . and python3 -m mypy src/privacyfence and python3 scripts/mypy_strict_modules.py pass
      - grep -n "upstream_audiences_applied" src/privacyfence/audit_log.py matches
  - id: p4-admin-page-and-integration
    title: The audiences card on the Admin page and the end-to-end test against the reference upstream
    depends_on: [p2-reference-upstream, p3-runtime]
    complexity: M
    touches:
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_settings_window_html.py
      - tests/integration/test_upstream_mcp_org.py
    brief: |
      Implement the upstreamCardHtml part and the intro sentence of design section 3.5 of docs/1153/plan.md exactly (titles, row labels and texts are fixed there).
      1. src/privacyfence/settings_window_html.py: in upstreamCardHtml, branch on u.audiences_source === 'upstream' before the groups card is built (its groups is [], which would otherwise fall through to "No group is mapped."); add a small upstreamAudiencesCardHtml(u) helper next to upstreamToolRow; reuse membershipTimeText (it is defined later in the same script; it is a function declaration, so it is hoisted). Change the intro sentence in renderUpstreams as 3.5 says and update its test. Update the comment above UPSTREAM_GATE_TEXT: the audiences come from the group map or from the server's audiences tool. Upstreams with audiences_source 'groups' render byte-for-byte as before.
      2. tests/unit/test_settings_window_html.py, next to test_upstreams_ready_lists_tools_and_groups: test_upstreams_audiences_from_the_server_ready, test_upstreams_audiences_waiting, test_upstreams_audiences_stale_with_error, test_upstreams_audiences_dropped_and_ignored_groups, test_upstreams_audiences_text_is_escaped; each asserts the fixed texts of 3.5 and that no "@" from the state reaches the page (the state carries no address, so put none in it).
      3. tests/integration/test_upstream_mcp_org.py: a second fixture audiences_stack next to stack (copy its setup; the ref upstream declared with "audiences_tool": "list_audiences" and "audiences_refresh_seconds": 3600 (so no background refresh posts during a test; leave the existing stack fixture unchanged, its tests count POSTs); reference.build_app(..., audience_members=members) with members = {"general": ["ann@example.com"]}; the admin_config.json group map left empty; pass StaticFileResolver(tmp_path / "members.json") over a file holding {"version": 1, "groups": {}}). Wait until upstream_mcp_runtime.status()[0]["audiences_state"] == "ready" as stack waits for "ready". Tests: test_audiences_from_the_upstream_let_a_person_read_that_audience (ref_search returns the general documents), test_dropping_a_person_at_the_upstream_removes_access_after_a_refresh (set members["general"] = [], then `assert await upstream_mcp_runtime.refresh_audiences("ref")` inside the async test, then ref_search returns nothing), test_the_audiences_refresh_writes_an_applied_audit_entry (read the audit rows as _forwarded_rows does; the decision upstream_audiences_applied is there and no row's summary contains "ann@").
      4. Do not change src/privacyfence/web/server.py, web/routes_admin.py or the runtime.
    acceptance:
      - python3 -m pytest tests/unit/test_settings_window_html.py -q passes
      - python3 -m pytest tests/integration/test_upstream_mcp_org.py -v passes
      - ruff check . passes
  - id: p5-retire
    title: Reference docs, contract, ADRs 0325 and 0326, index and changelog
    depends_on: [p4-admin-page-and-integration]
    complexity: M
    touches:
      - docs/upstream-mcp.md
      - docs/identity-claim.md
      - docs/configuration-reference.md
      - docs/adr/0325-an-upstream-can-be-the-source-of-its-own-audiences.md
      - docs/adr/0326-a-service-claim-may-call-the-declared-audiences-tool.md
      - docs/adr/0297-the-upstream-claim-is-an-eddsa-identity-claim-in-its-own-header-bound-to-one-call.md
      - docs/adr/0298-audiences-come-from-group-membership-and-tokens-only-narrow-them.md
      - docs/adr/0299-group-membership-is-updated-through-a-scoped-token-tool.md
      - docs/adr/0319-the-admin-page-shows-upstream-mcp-servers-read-only.md
      - docs/adr/0323-the-oidc-provider-sends-no-group-membership.md
      - docs/adr/README.md
      - CHANGELOG.md
    brief: |
      Write the docs and ADRs of docs/1153/plan.md sections 3.6, 3.8 and 4. Read docs/adr/README.md (template and rules) and an existing ADR such as docs/adr/0310-email-is-an-opt-in-visibility-input-of-the-identity-claim.md for the shape first.
      1. docs/upstream-mcp.md: add audiences_tool and audiences_refresh_seconds to the Keys row and two rows to the rules table (3.1 texts); a new subsection "### Audiences from the upstream" after "Groups and audiences" (what the operator sets, the result shape of 3.2 in one example, no approvals, the refresh and stale rule with the worst-case lag of 3.3, the group map and Admin → Group membership not used for that upstream); in "Group membership" one bullet that an upstream with audiences_tool is skipped; the privacyfence_status example and text with audiences_source and audiences_state; the Admin page paragraph with the audiences card; the Audit section with the two new decisions and the sentence that the refresh writes no upstream_forwarded row (its jti is logged at info); the implementer bullet of 3.6; the Reference upstream paragraph naming list_audiences. Change the "There is no periodic refresh"-style sentences to say the tool list is never refreshed.
      2. docs/identity-claim.md: the service row, the Audiences sentence and the new section of 3.6, and the versioning paragraph's sentence of 3.6.
      3. docs/configuration-reference.md: add audiences_tool and audiences_refresh_seconds to the PRIVACYFENCE_UPSTREAM_MCP_FILE row, and to the upstream_mcp.<id>.groups row the sentence "Ignored for an upstream that declares audiences_tool."
      4. ADR 0325 and ADR 0326 as section 4 says, Status "Accepted — <today's date>", Context from sections 1 and 2, Decision from 3.x, Alternatives from 3.8, Consequences, Verification naming the tests p1-p4 added, Related. State each decision in full; cite docs/1153/plan.md only as the record.
      5. Status lines: 0297 "Amended by ADR 0326", 0298, 0299, 0319 and 0323 "Amended by ADR 0325" (one line each, nothing else in those files). Two rows in docs/adr/README.md's index after 0324, and the status column of 0297, 0298, 0299, 0319 and 0323 gains "amended by 0325"/"amended by 0326".
      6. CHANGELOG.md under "## [Unreleased]", in its existing "### Added" subsection (never a version heading): one line: "An upstream MCP server can now be the source of its own audiences: declare `audiences_tool` and PrivacyFence reads who holds each audience from that tool at startup and every five minutes (`audiences_refresh_seconds`), with no group map and no approvals for that upstream (#1153)."
      7. If 0325 or 0326 is taken on main when you start, use the next free numbers everywhere and record it in docs/1153/deviations.md.
    acceptance:
      - test -f docs/adr/0325-an-upstream-can-be-the-source-of-its-own-audiences.md and test -f docs/adr/0326-a-service-claim-may-call-the-declared-audiences-tool.md succeed
      - grep -n "0325" docs/adr/README.md and grep -n "0326" docs/adr/README.md match
      - grep -n "audiences_tool" docs/upstream-mcp.md docs/identity-claim.md docs/configuration-reference.md matches in all three files
      - grep -n "#1153" CHANGELOG.md matches a line above the first "## [" version heading after "## [Unreleased]"
      - python3 -m pytest tests/unit -q passes
```
