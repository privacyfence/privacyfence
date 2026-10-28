# Plan: an AI client proposes a scoped token, a person creates it

## Goal

A person who has worked something out with Claude, approving calls by hand or through
auto-accept, often wants to turn it into a routine. The routine reaches `/mcp` with a scoped token
(ADR 0220), and building that token by hand in **Settings → Scoped tokens → New token** is slow
for a long recipe.

With this change Claude can call `privacyfence_propose_scoped_token`:
- Claude sends a bundle (name, days, rules, tools, personal-data flags), or asks PrivacyFence to
  build the bundle from the calls approved in this MCP session.
- The tool returns a review link at once, and that is all the client gets. MCP never carries the
  token, and nothing tells the client whether one was made.

The person then:
1. Opens the link and reviews the proposal in Settings, where they can only narrow it.
2. Confirms in a second dialog that warns them they are delegating access. That dialog works only
   after they type the token's name.
3. Passes passkey step-up, if it is in force, bound to the exact request.

The token is then made by the existing create path and delivered the existing way: to the
clipboard through the companion in local mode, or shown once in organization mode.

The tool is treated as highly sensitive:
- It is never offered to a scoped token.
- It is refused in an unattended session.
- In organization mode it is off until the administrator turns it on.
- On a local install only the computer's owner can use it.

## Current state

**The scoped-token store** (`src/privacyfence/scoped_tokens.py`)
- `ScopedTokenPolicy(enabled, max_days)` is at `:162-165`.
- `policy_from_org_config` (`:180-191`) gives `(True, 365)` locally and reads
  `org_config["scoped_tokens"]` in organization mode.
- `normalise_rule_requests` (`:393-416`) and `_RULE_REQUEST_SHAPES` (`:386-390`) define the rule
  requests `new{group, value: str, verbs}`, `copy{rule_id}` and `duplicate{token_id, rule_id}`.
- `ScopedTokenStore.create` (`:496-595`) works in two stages:
  1. Without the lock (`:518-546`): the policy, label and 50-rule checks, and compiling the `new`
     rules. It reads `own_rows` before taking the lock.
  2. One `with self._lock:` block (`:547-594`): refresh, then the duplicate-label, days,
     no-rules-and-no-tools, tool-name, `copy`/`duplicate` and 50-tokens checks, then minting and
     the write.
- Nothing validates a request without minting it.

**The create routes** (`src/privacyfence/web/routes_settings.py`, local `build_routes`, org
`build_org_routes` at `:1442`)
- `_scoped_create_fingerprint_body` (`:368-388`) normalises the create body.
  `tests/unit/web/test_routes_settings.py:1619-1660` calls it directly.
- Local:
  - Gates are in `_scoped_token_request` (`:1165-1206`). Step-up runs inside it, straight after the
    fingerprint body: `_verify_or_challenge(subject_key=..., fingerprint=...)`.
  - The handler is `scoped_token_create` (`:1208-1240`). It calls `store_create` and then
    `send_secret` (the companion clipboard, `control_channel.send_mcp_token`), revokes the token if
    delivery fails, and returns `{"created": {id, label, expires_at}}`.
- Org:
  - Gates are in `_org_scoped_token_request` (`:1784-1826`).
  - The handler is at `:1828-1863`. It runs `_ensure_principal_settings_loaded()` under
    `principal_scope` (`:1833-1837`) and returns `{"created": ..., "secret": secret}` with
    `Cache-Control: no-store`.
- Every bespoke POST route must be listed in `_BESPOKE_SENSITIVE_ROUTE_PATHS` (`:297`) or
  `_BESPOKE_EXEMPT_ROUTE_PATHS` (`:319`). `TestBespokeRoutesAreClassified`
  (`test_routes_settings.py:2041`) checks this.
- Page routes: local `/settings` and `/settings/connectors` (`:1359-1360`); org `/settings` and
  `/settings/privacy` (`:2127-2128`).
- The settings bridge shim's `scopedRoutes` map (`:583-591`) turns a page `post(action, ...)` into
  a fetch and retries with a passkey on 428. On success it calls `window.location.reload()`, or
  `window.__pfShowScopedSecret(secret, reload)` in org mode (`:628-635`).

**The form and its page data**
- `renderScopedTokens()` in `src/privacyfence/settings_window_html.py` (`:1444-1600`) builds it.
  The state is in `ui.st*` (`:351-352`), and `submitScopedToken` (`:1815-1828`) builds the
  request.
- The page re-renders `#app` with innerHTML on `render()`.
- The only pre-fill is client-side Duplicate (`duplicateScopedToken`, `:1400-1417`).
- `build_html(..., initial_section=..., scoped_tokens=page_data)` (`:2576`) embeds
  `window.__pfScopedTokens` and `window.__pfInitialSection` (`:2648-2655`).
- Page data:
  - Local: `scoped_token_page_data` (`src/privacyfence/web/server.py:348-413`), wired by
    `build_scoped_token_wiring` (`:416-458`).
  - Org: `org_scoped_token_page_data` (`:1391-1400`). `_org_scoped_token_data` returns `None`,
    which hides the section, when scoped tokens are disabled.

**MCP meta-tools**
- Each is a `types.Tool` in `src/privacyfence/web/mcp_tools.py` (`:130-291`), listed in
  `META_TOOLS`/`META_TOOL_NAMES` (`:317-327`).
- Every description is at most 1024 characters, and each needs a `reason` property
  (`tests/unit/web/test_tool_schema_portability.py`, `tests/unit/web/test_mcp_tools.py:192`).
- `handle_list_tools` (`src/privacyfence/web/routes_mcp.py:421-455`) lists every meta-tool. Several
  tests assert the complete list:
  - `tests/unit/web/test_tool_schema_portability.py:88-93`
  - `tests/integration/test_shim_mcp_contract.py:365-366`
  - `tests/unit/web/test_routes_mcp.py:350,391,410,1200-1207`
- Scoped tokens see only `SCOPED_META_TOOLS` (`:103-109`).
- `_dispatch_meta_tool` (`:576-612`) receives `base_url`.
- A meta-tool call runs as `UNKNOWN_AGENT` on purpose (`:479-491`, ADR 0006). Only connector tools
  go through `_resolve_agent` (`:202-242`).
- `McpDispatcher` (`src/privacyfence/web/mcp_dispatch.py:96`):
  - `_unattended_sessions` is at `:138` and is cleared in `end_session` (`:674-682`).
  - `check_policy` (`:355-400`) takes `my_email` from `getattr(connector, "my_email", "")` and drops
    `_CONNECTOR_COMPUTED_ARGS` from the args (`:372`).
- `daemon_main.py` builds the dispatcher at `:898` (local) and `:1233-1238` (org, `org_config` in
  scope). It imports `scoped_tokens` only inside a function (`:2346`).

**Nothing records a session's approved calls**
- In `McpDispatcher.call` (`:229-322`), `connector.call` runs inside
  `with (..., released_request_scope()):` (`:276-279`). Within that block,
  `audit_log.current_released_request_id()` is non-empty exactly when the gate recorded an
  approved-like decision. On exit it resets to `""`.
- An `auto`-gate tool never sets it: the connector's `_auto_audit` writes `request_id=""`.
- `policy/suggest.py`'s `suggested_rules(tool, args, my_email)` (`:69-144`) turns one call into
  candidates `{group, label, value: list|[]|None, value_known, verb, operation, requires_args?}`,
  narrowest first.

**Precedent and helpers**
- `gate.propose_policy_change` (`src/privacyfence/gate.py:1521-1658`) refuses a scoped token,
  validates before any dialog, refuses an unattended session (audits `denied_unattended`), and
  blocks on a sensitive confirm.
- A blocking tool cannot work for this change, because a review that takes minutes outruns MCP
  client timeouts. The user decided the tool returns only a link.
- These helpers exist with these signatures:
  - `policy.describe.row_sentence(row, value_display=None)`
  - `policy.scope_rows.add_rules(rows, rules) -> (rows, touched_ids)`
  - `policy.catalogue.parse_verbs(list)`
  - `policy.catalogue.parse_value_list(text, group)`
  - `policy.catalogue.rules_for_catalogue_entry(group, value, verbs)`
  - `agent_label.label_for(agent)`, which returns an `AgentLabel`. Use `.text`.

**Docs**
- These say "eight `privacyfence_*` tools":
  - `docs/how-it-works.md:116,176`
  - `scripts/generate_tools_reference.py:210`, which regenerates `docs/tools-reference.md`; checked
    by `tests/unit/test_docs_tools_reference.py`
- `docs/testing-policy.md:32-36` names every `browser`-marked module.
- `docs/approvals-and-policy.md:443` says only Settings can set the personal-data box.

## Design

### D1. Policy: `ScopedTokenPolicy.allow_ai_proposals`

In `src/privacyfence/scoped_tokens.py`:

```python
@dataclass(frozen=True)
class ScopedTokenPolicy:
    enabled: bool
    max_days: int
    allow_ai_proposals: bool = False

PROPOSALS_DISABLED_MESSAGE = (
    "Token proposals are not enabled on this install. "
    "Your administrator turns them on in the organization config bundle."
)
```

- **Local mode:** `policy_from_org_config({}, org_mode=False)` returns
  `ScopedTokenPolicy(True, LOCAL_MAX_DAYS, True)`.
- **Org mode:** `raw = section.get("allow_ai_proposals", False)`.
  - If `raw` is not a `bool`, log the warning
    `"scoped_tokens.allow_ai_proposals must be true or false; using false"` and use `False`.
  - Otherwise, `allow_ai_proposals = raw is True and enabled`.
- **`scripts/build_org_bundle.py`**, "Scoped AI client tokens (org mode)" group (`:518-535`):
  - Add a mutually exclusive pair, `--enable-scoped-token-proposals` /
    `--disable-scoped-token-proposals`.
  - Help text: `"Let AI clients propose scoped tokens for a person to review (off by default; needs --enable-scoped-tokens)."`
  - The pair sets `scoped_tokens.allow_ai_proposals` in the merge block (`:776-784`).
  - Add one line to the docstring (`:32-34`).

### D2. Request normalisation and dry-run validation

- Rename `_scoped_create_fingerprint_body` to `normalise_create_request(payload: dict[str, Any]) -> dict[str, Any] | None`
  and move it, body unchanged, into `scoped_tokens.py`. `routes_settings.py` and
  `test_routes_settings.py:1619-1660` call `scoped_tokens.normalise_create_request`; no alias is
  kept.
- Split `ScopedTokenStore.create` without changing any message or its order:

  ```python
  def _prepare(self, *, label: str, rules: Sequence[dict[str, Any]], policy: ScopedTokenPolicy
               ) -> tuple[list[PolicyRule], dict[str, ScopeRow]]:
      """No lock: today's :515-546 (policy, label, 50-rule checks, compiling `new` rules) and the
      own_rows read; returns (compiled new rules in request order, own_rows)."""

  def _check_locked(self, existing: list[ScopedToken], *, label: str, days: int,
                    rules: Sequence[dict[str, Any]], tool_set: frozenset[str],
                    new_rules: list[PolicyRule], own_rows: dict[str, ScopeRow],
                    policy: ScopedTokenPolicy, check_existing: bool = True) -> list[ScopeRow]:
      """Caller holds self._lock: today's :551-574, returning the token's rows. With
      check_existing=False the duplicate-label and 50-tokens checks are skipped."""

  def check_request(self, principal: Principal, *, label: str, days: int,
                    rules: Sequence[dict[str, Any]], tools: Iterable[str],
                    policy: ScopedTokenPolicy, check_existing: bool = True) -> None:
      """_prepare, then `with self._lock:` _refresh + _check_locked. Writes nothing."""
  ```

- `create` calls `_prepare`, then a single `with self._lock:` block that refreshes, reads
  `existing`, calls `_check_locked` and mints. That keeps the duplicate-label and 50-token checks
  in the same lock hold as minting, as today.
- Match the parameter list of `_check_locked` to what `:551-574` actually reads. It must hold
  exactly the values that code uses, no more.

### D3. The proposal module: `src/privacyfence/scoped_token_proposals.py`

The module has no web imports. Import `policy.*` and `auto_accept` inside `bundle_from_calls` and
`review_data`, not at the top (they cycle, like `scoped_tokens.py:91`).

```python
PROPOSAL_TTL_SECONDS = 3600
MAX_PENDING_PER_PRINCIPAL = 5
REVIEW_PATH = "/settings/scoped_tokens/proposals/{proposal_id}"
ID_PREFIX = "stp-"
GONE_MESSAGE = "This token proposal has expired or was already used."
WIDENED_MESSAGE = "A proposed token can only be narrowed here. To add rules or tools, use New token."

@dataclass(frozen=True)
class TokenProposal:
    id: str                    # ID_PREFIX + secrets.token_hex(16)
    principal: Principal
    request: dict[str, Any]    # normalise_create_request output
    source: str                # "bundle" or "session"
    agent: AgentIdentity
    reason: str                # trimmed to 500 characters
    uncovered_tools: tuple[str, ...]
    created_at: float
    expires_at: float

class ProposalError(ValueError): ...

class ProposalRegistry:
    def __init__(self, on_expired: Callable[[TokenProposal], None] | None = None) -> None
    def add(self, proposal: TokenProposal, now: float | None = None) -> None
    def get(self, principal_id: str, proposal_id: str, now: float | None = None) -> TokenProposal | None
    def take(self, principal_id: str, proposal_id: str, now: float | None = None) -> TokenProposal | None
    def restore(self, proposal: TokenProposal, now: float | None = None) -> None
    def pending_for(self, principal_id: str, now: float | None = None) -> list[TokenProposal]

def get_registry() -> ProposalRegistry
def set_registry(registry: ProposalRegistry | None) -> None
def new_proposal(*, principal, request, source, agent, reason, uncovered_tools=(), now=None) -> TokenProposal
def narrowing_error(proposed: dict[str, Any], final: dict[str, Any]) -> str | None
def bundle_from_calls(calls: Sequence[tuple[str, dict[str, Any], str]]) -> tuple[list[dict[str, Any]], list[str], list[str]]
def review_data(proposal: TokenProposal, own_rows: Sequence[ScopeRow]) -> dict[str, Any]
def record(decision: str, proposal: TokenProposal, summary: str) -> None
```

**The registry**
- It lives in memory, behind a `threading.Lock`.
- Every public method first sweeps entries with `expires_at <= now`. It removes them and, after
  releasing the lock, calls `on_expired(p)` for each one.
- `get_registry()` builds one lazily with
  `on_expired=lambda p: record("scoped_token_proposal_expired", p, f"Token proposal '{p.request['label']}' expired")`.
- `add` raises
  `ProposalError("Too many token proposals are waiting. Review or decline them in Settings → Scoped tokens.")`
  when the principal already has `MAX_PENDING_PER_PRINCIPAL` live proposals.
- `get` and `take` return `None` for an unknown id, an expired one, or another principal's.
- `take` removes the entry, so a proposal can be used once.
- `restore` puts a taken proposal back unless it has expired.
- `tests/conftest.py` `_reset()` calls `scoped_token_proposals.set_registry(None)`.

**`narrowing_error`** returns `None` when `final` is no wider than `proposed`, and otherwise
`WIDENED_MESSAGE`. `final` is no wider when all of these hold:
- Every `final` rule matches a `proposed` rule:
  - a `copy` matches the `copy` with the same `rule_id`;
  - a `new` matches the `new` with the same `group` and the same `value` string, where
    `set(final.verbs) <= set(proposed.verbs)` and the verbs are non-empty;
  - a `duplicate` never matches.
- `set(final.tools) <= set(proposed.tools)`.
- `final.days <= proposed.days`.
- Each personal-data flag is true in `final` only if it is true in `proposed`.

The label may differ.

**`bundle_from_calls`** takes `(tool, agent_args, my_email)` triples and returns
`(rules, tools, uncovered_tools)`:
- A tool not in `auto_accept.TOOL_TO_GATE` is skipped. Every other tool goes into `tools`.
- A tool whose gate is `"auto"` needs no rule.
- For any other tool, call `policy.suggest.suggested_rules(tool, agent_args, my_email)` and take
  the first suggestion that has no `requires_args`, has `value_known` true, and whose `value` is a
  list.
  - If there is none, add the tool to `uncovered_tools`.
  - Otherwise, add `suggestion["verb"]` to the verbs under the key
    `(suggestion["group"], ",".join(suggestion["value"]))`.
- `rules` is `[{"kind": "new", "group": g, "value": v, "verbs": sorted(verbs)}]` sorted by
  `(group, value)`.
- `tools` and `uncovered_tools` are sorted and de-duplicated.

**`review_data`** returns the data the review page renders:

```
{"id", "label", "days", "tools": [...], "allow_personal_data", "allow_personal_data_reads",
 "rules": [{"request": <rule request dict>, "sentence": str}],
 "agent_label": agent_label.label_for(proposal.agent).text, "reason", "source",
 "uncovered_tools": [...], "expires_at"}
```

- A `new` rule's sentence is the sentences of the rows it compiles to, joined with `"; "`. Each
  row's sentence is `policy.describe.row_sentence(row)`, over the rows of:

  ```python
  policy.scope_rows.add_rules([], policy.catalogue.rules_for_catalogue_entry(
      group,
      policy.catalogue.parse_value_list(value, group),
      policy.catalogue.parse_verbs(verbs),
  ))[0]
  ```

- A `copy` rule's sentence is the matching row's sentence from `own_rows`, or
  `"A rule that no longer exists"`.

**`record`** writes one `AuditEntry` through `audit_log.get_audit_logger().record(...)`:
- It runs inside `principal_scope(proposal.principal)` and `agent_scope(proposal.agent)`, so that
  `_stamp_entry` fills in the agent fields.
- Fields:
  - `connector="policy"`
  - `tool="privacyfence_propose_scoped_token"`
  - `decision=decision`
  - `summary=summary`
  - `claude_reason=proposal.reason`
  - `request_id=proposal.id`
  - `sender=proposal.principal.email or proposal.principal.id`
- Fill the remaining fields the way `gate._audit` does (`gate.py:1816-1840`), and catch and log a
  logging failure the way it does.

The decision strings are `scoped_token_proposed`, `scoped_token_proposal_accepted`,
`scoped_token_proposal_declined` and `scoped_token_proposal_expired`. None of them goes into
`APPROVED_LIKE_DECISIONS`; all four are added to the `AuditEntry.decision` comment list
(`audit_log.py:139-155`).

### D4. The MCP tool `privacyfence_propose_scoped_token`

**Declaration** (`mcp_tools.py`)

`PROPOSE_SCOPED_TOKEN_TOOL` is appended to `META_TOOLS`, with annotations
`read_only_hint=False, destructive_hint=False, idempotent_hint=False`. Its description is exactly:

> Propose a scoped token (a named, expiring bearer credential limited to some rules and tools,
> for a routine or scheduled job) for the person to review in PrivacyFence Settings. Returns at
> once with review_url: give it to the person. Nothing is created until they review it, type its
> name and confirm; they may narrow it. You never receive the token, nor whether it was created.
> Set from_session=true to build its rules and tools from the calls approved in this session, or
> pass rules (each {kind:'new', group, value, verbs} with group and verbs from
> privacyfence_list_policy's scope_groups and value a comma-separated id list, or {kind:'copy',
> rule_id}) and tools. days defaults to 90. Ask for allow_personal_data or
> allow_personal_data_reads only if the job needs them. Refused in an unattended session.
> reason: one sentence on what the token is for (logged and shown to the person).

`input_schema` properties are `reason` (string), `label` (string), `days` (integer),
`from_session` (boolean), `rules` (array of objects), `tools` (array of strings),
`allow_personal_data` (boolean) and `allow_personal_data_reads` (boolean). `required` is
`["reason", "label"]`.

**Listing and calling** (`routes_mcp.py`)
- `handle_list_tools` leaves the tool out when `dispatcher.scoped_token_proposals_enabled()` is
  false.
- The tool is not in `SCOPED_META_TOOLS`, so a scoped token neither sees it nor may call it.
- `_dispatch_meta_tool` gets a branch:
  `return await dispatcher.propose_scoped_token(session_key, arguments, base_url)`.
- Attribution (around `:489`): when `name == mcp_tools.PROPOSE_SCOPED_TOKEN_TOOL.name` and
  `scope is None`, resolve the agent with `_resolve_agent(...)`, as for connector tools. Every
  other meta-tool keeps `UNKNOWN_AGENT`. The comment above that line now says so and cites ADR
  0251.

**`McpDispatcher` (`mcp_dispatch.py`)**
- New constructor keyword `scoped_token_policy: ScopedTokenPolicy | None = None`.
  - `daemon_main.py:898` passes `scoped_tokens.policy_from_org_config({}, org_mode=False)`.
  - `:1233` passes `scoped_tokens.policy_from_org_config(org_config, org_mode=True)`.
  - Add `from . import scoped_tokens` at the top of `daemon_main.py`.
- `scoped_token_proposals_enabled(self) -> bool` returns
  `self._scoped_token_policy is not None and self._scoped_token_policy.enabled and self._scoped_token_policy.allow_ai_proposals`.
- Session ledger:
  - `MAX_SESSION_CALLS = 200`, and
    `self._session_calls: dict[Hashable, list[tuple[str, dict[str, Any], str]]] = {}`.
  - In `call()`, on the line right after `connector.call` returns normally (inside the existing
    `with (...)` block at `:276-279`), append
    `(tool, {k: v for k, v in args.items() if k not in _CONNECTOR_COMPUTED_ARGS}, getattr(connector, "my_email", "") or "")`.
    Append only when both hold:
    - `current_token_scope() is None`;
    - `audit_log.current_released_request_id()` is non-empty, or
      `auto_accept.TOOL_TO_GATE.get(tool) == "auto"`.
  - Keep the newest 200 entries.
  - `end_session` drops the key. Nothing is written to disk.

**`async def propose_scoped_token(self, session_key, params, base_url) -> dict`**, steps in order:

1. **Scoped token.** If `current_token_scope()` is not `None`, raise
   `GateDeniedError("Request denied: a scoped token cannot propose tokens.")`.
2. **Local non-owner.** If `self._mode == "local"` and
   `current_principal().id != LOCAL_PRINCIPAL.id`, raise
   `ValueError("Token proposals are reviewed by this computer's owner; this account has no Scoped tokens page.")`.
3. **Disabled.** If not `scoped_token_proposals_enabled()`, raise
   `ValueError(scoped_tokens.PROPOSALS_DISABLED_MESSAGE)`.
4. **Unattended session.** If `session_key in self._unattended_sessions`:
   - record an `AuditEntry` through `audit_log.get_audit_logger()` with `connector="policy"`,
     `tool="privacyfence_propose_scoped_token"`, `decision="denied_unattended"`,
     `summary="Token proposal refused in an unattended session"` and
     `claude_reason=params.get("reason", "")`, filling the other fields as D3's `record` does;
   - then raise
     `GateDeniedError("Request denied: this connection is in an unattended session, so nobody is there to review a token proposal.")`.
5. **Build the request.** Read `rules = params.get("rules", [])` and
   `tools = params.get("tools", [])`.
   - If `from_session` is true:
     - If `rules` or `tools` is non-empty, raise
       `ValueError("Pass either from_session or rules and tools, not both.")`.
     - `(rules, tools, uncovered) = bundle_from_calls(self._session_calls.get(session_key, []))`.
     - If both lists are empty, raise
       `ValueError("This session has no approved calls to build a token from.")`.
   - Otherwise, any rule whose `kind` is not `"new"` or `"copy"` raises
     `ValueError("A proposed rule is {kind:'new', group, value, verbs} or {kind:'copy', rule_id}.")`.
   - `days = min(params.get("days", scoped_tokens.DEFAULT_DAYS), policy.max_days)`.
   - Normalise `{label, days, rules, tools, allow_personal_data, allow_personal_data_reads}` with
     `normalise_create_request`. If the result is `None`, raise
     `ValueError("The proposal is not a valid token request.")`.
6. **Validate.** Run `_ = self.connectors`. Then run
   `scoped_tokens.get_store().check_request(principal, ..., policy=policy, check_existing=False)` in
   `asyncio.to_thread`, so that a re-proposed label never reveals that a token exists. A
   `ScopedTokenError` propagates.
7. **Register.** Call
   `new_proposal(principal=current_principal(), request=body, source="session" if from_session else "bundle", agent=current_agent(), reason=params["reason"], uncovered_tools=uncovered)`,
   then `get_registry().add(proposal)`. A `ProposalError` propagates.
8. **Audit.** Call
   `record("scoped_token_proposed", proposal, f"Token '{label}' proposed: {n} rule(s), {t} tool(s), {days} days")`.
9. **Return:**

   ```python
   {"proposal_id": proposal.id,
    "review_url": base_url.rstrip("/") + REVIEW_PATH.format(proposal_id=proposal.id),
    "expires_at": <ISO 8601 UTC of proposal.expires_at>,
    "label": ..., "days": ..., "tools": [...],
    "rules": [<the review_data sentences>],
    "uncovered_tools": [...],
    "message": "Give the person this link. They review the token in PrivacyFence Settings, can narrow it, and confirm it themselves; you will not receive the token or learn whether it was created. Tools in uncovered_tools have no rule, so the token's calls to them will be denied."}
   ```

### D5. Settings routes

The same routes exist in both modes, in `routes_settings.py`.

**Gate helpers.** Add a keyword
`precheck: Callable[[dict[str, Any], dict[str, Any]], Response | None] | None = None` to
`_scoped_token_request` and `_org_scoped_token_request`.
- It is called with `(payload, bound)` after `bound` (the fingerprint body) is computed and before
  step-up.
- A non-`None` return is returned as the response.
- The existing callers pass nothing.

**`GET /settings/scoped_tokens`** (both modes) renders the settings page with
`initial_section="scoped_tokens"`. It needs no classification, because the check covers POST
routes only. Stop with status=blocked if `TestBespokeRoutesAreClassified` says otherwise.

**`GET /settings/scoped_tokens/proposals/{proposal_id}`**
- It uses the same authentication as `/settings` in that mode.
- Org, when scoped tokens or proposals are disabled: `303` redirect to `/settings`.
- Otherwise it reads the rows inside `principal_scope(principal)`: `LOCAL_PRINCIPAL` locally; in
  org mode the signed-in principal, after `_ensure_principal_settings_loaded()`, as at
  `:1833-1837`.
- It renders the page with `initial_section="scoped_tokens"` and `scoped_proposal`:
  - `review_data(p, auto_accept.get_policy_rows())` for a live proposal of this principal;
  - `{"missing": True}` otherwise.

**`POST /api/settings/scoped_tokens/create_from_proposal`**
- It is in `_BESPOKE_SENSITIVE_ROUTE_PATHS`, with action and `subject_key`
  `create_proposed_scoped_token`.
- Body: the create fields, plus `proposal_id` (str), `typed_label` (str), `confirmed` (true) and,
  on retry, `webauthn_assertion`.
- `fingerprint_body` is `{**normalise_create_request(p), "proposal_id": p["proposal_id"]}`. It is
  `None` when either part is invalid or `proposal_id` is not a str.
- The gates run in this order, through the mode's helper:
  1. Everything create's gates already check: authentication, CSRF and origin,
     `confirmed is True`, the local human-session check, and org `token_policy.enabled`.
  2. `fingerprint_body`. If it is `None`: 400 `invalid request`.
  3. `precheck`, which in turn checks:
     - (org) `token_policy.allow_ai_proposals`, else 403
       `{error: PROPOSALS_DISABLED_MESSAGE}`;
     - `payload["typed_label"] == bound["label"]`, else 400
       `{error: "Type the token's name exactly to confirm."}`;
     - `registry.get(principal.id, proposal_id)`, else 410 `{error: GONE_MESSAGE}`;
     - `narrowing_error(p.request, body)`, else 400 `{error: WIDENED_MESSAGE}`.
  4. Step-up, when in force, bound to `fingerprint_body`.
- Then the handler:
  1. `registry.take(...)`. If it returns `None`: 410.
  2. Create and deliver through `_create_and_deliver`. This is a per-mode helper extracted from
     that mode's `scoped_token_create` body, and both routes call it.
  3. If creating raises `ScopedTokenError`, or local delivery fails: `registry.restore(p)` and the
     same error response create gives.
  4. On success:
     - call `record("scoped_token_proposal_accepted", p, f"Token proposal '{p.request['label']}' accepted as '{label}' ({token_id})")`;
     - write the existing `"Created scoped token ..."` settings audit entry with
       `" from a proposal"` added (appended at the end locally, before ` (principal=...)` in org);
     - give the same response as create.

**`POST /api/settings/scoped_tokens/decline_proposal`**
- It is in `_BESPOKE_EXEMPT_ROUTE_PATHS` with the reason
  `"declining only discards a proposal; it creates and widens nothing"`.
- Body: `{proposal_id, csrf}`. The mode's normal authentication and CSRF/origin checks apply.
- `take`, then `record("scoped_token_proposal_declined", p, f"Token proposal '{p.request['label']}' declined")`,
  then `{"declined": true}`.
- An unknown id gives 410 `{error: GONE_MESSAGE}`.

**Page data.** `scoped_token_page_data` and `org_scoped_token_page_data` (`server.py`) gain:
- `"proposals": [{"id", "label", "agent_label", "expires_at"}]`, from
  `get_registry().pending_for(principal.id)`, with `agent_label` taken as
  `label_for(p.agent).text`;
- `"proposals_enabled"`: always true locally, `policy.allow_ai_proposals` in org mode.

**Bridge shim**
- `scopedRoutes` gains:

  ```js
  create_proposed_scoped_token: ['create_from_proposal', null]
  decline_scoped_token_proposal: ['decline_proposal', 'Decline this token proposal?']
  ```

  A `null` message skips `window.confirm`, because the page's own dialog is the confirmation.
- For these two actions, the success path (the reload callback included, for the org secret
  dialog's close) calls `window.location.assign('/settings/scoped_tokens')` instead of
  `window.location.reload()`.

### D6. The review UI (`settings_window_html.py`)

**Embedding.** `build_html(..., scoped_proposal: dict | None = None)` embeds it as
`window.__pfScopedProposal`, escaped the same way as `__pfScopedTokens`. Every string that comes
from the agent (label, reason, sentences, tool names, agent_label) is inserted through the page's
existing escape helper or `textContent`, never as raw HTML.

**`renderScopedTokens()`**
- When `SCOPED.proposals` is non-empty, it shows a "Waiting for review" list above the New token
  form. Each row reads `{label} — proposed by {agent_label}`, with a link `Review` to
  `/settings/scoped_tokens/proposals/{id}`.
- When `window.__pfScopedProposal` is set, a "Review proposed token" panel replaces the New token
  form. Its state lives in new `ui.sp*` fields.
- **Missing proposal:** the panel shows only
  `"This token proposal has expired, was declined or was already used."`

**Live proposal**, in order:
- Heading `Review proposed token`.
- Intro: `"An AI client proposed this token. Untick anything the job does not need. You can only narrow it here; to add more, use New token."`
- `Proposed by: {agent_label}. The AI client reports this name itself; PrivacyFence does not verify it.`
- `Reason given: {reason}`.
- A name input, pre-filled, and a days input with `max` set to the proposal's days.
- One ticked checkbox per rule, labelled with its sentence. A `new` rule with more than one verb
  also gets one ticked checkbox per verb.
- One ticked checkbox per tool.
- For each personal-data flag the proposal set: its existing label, **unticked**, with the hint
  `"The AI client asked for this."`. A flag the proposal did not set is not shown.
- If `uncovered_tools` is non-empty:
  `"No rule covers these tools, so the token's calls to them will be denied: {list}."`
- Buttons:
  - `Decline` posts `decline_scoped_token_proposal` with `{proposal_id}`.
  - `Continue` opens the modal below.

**The modal**
- Title: `Delegate access to this token?`
- Text: `"You are delegating access. Anything that holds this token can act as you within these rules, for {days} days, with no approval card and no prompt. PrivacyFence cannot tell who is using it. Revoke it in Settings → Scoped tokens if it leaks or the job is retired."`
- If a personal-data box is ticked, it adds: `"This token may also return or upload personal data."`
- The final rule sentences and tool names, as a list.
- A label `Type the token name to confirm`, with an empty input.
- Buttons `Cancel` and `Delegate access`.
- Cancel is focused when the modal opens, and Escape cancels.
- The typed-name input updates `ui.spTyped`, and sets the button's `disabled` directly, without a
  full `render()`, so focus is not lost. `Delegate access` stays disabled until
  `input.value === label` exactly.
- `Delegate access` posts `create_proposed_scoped_token` with
  `{proposal_id, label, days, rules, tools, allow_personal_data, allow_personal_data_reads, typed_label}`:
  - `rules` holds only the ticked rules, and each `new` rule only its ticked verbs. A `new` rule
    with no verb ticked is left out.
  - `tools` holds only the ticked tools.

### D7. What was rejected (for the ADRs)

**Blocking until the person finishes, like `propose_policy_change`** (0250)
- A review with a typed name and a passkey outruns MCP client tool-call timeouts.
- Claude could not set up the routine's environment with the token anyway.

**A status or await tool** (0250)
- Claude has no use for the outcome, and reporting it would tell a client whether its delegation
  was accepted.
- For the same reason, the proposal-time dry run skips the duplicate-label and 50-token checks.

**Returning the secret to the client** (0250)
- It would sit in the chat transcript (ADR 0104, ADR 0164).
- "The client never receives the token" is a property of the protocol. A local agent with a shell
  can still read the clipboard, the exposure ADR 0104 already accepts.

**Editing that can widen the proposal** (0250)
- The review would turn into a second New token form filled in by an AI client.
- Widening stays on the ordinary form.

**Pre-ticking the personal-data boxes** (0250)
- That would make the AI's request the default.
- Only the person can turn them on (`docs/approvals-and-policy.md:443`).

**Step-up alone** (0250)
- Step-up is optional per install.
- The typed name makes the confirmation deliberate on every install and stops a reflex click.
- An agent driving a browser session is bounded only by `require_human_session` and step-up, as
  for New token.

**A confirm dialog in the approvals list** (0250)
- The decide route carries only `result`, `choice` and `note`, and its step-up fingerprint covers
  none of the form.
- Confirms are swept only on a gated call (`gate._pop_registry_expirations`).

**On by default in organization mode** (0253)
- This is a new way to mint credentials, so the administrator opts in.

**Leaving the proposing call unattributed under ADR 0006** (0251)
- The person needs to see which client asked for the delegation.
- The name is still a claim, and the page says so.

**Deriving the bundle from the audit log** (0252)
- Audit entries carry no arguments, so no rule value could be recovered.
- The in-memory ledger keeps up to 200 calls' arguments per session, which may include message
  text, for the session's life only.

**`duplicate` rules in a proposal** (0250)
- A client never sees the person's token ids.

**ADR 0013** (no MCP tool mints a sign-in credential) still holds:
- the tool mints nothing;
- only a signed-in person in Settings creates the token.

## ADRs

Each ADR, its decision, and the ADRs it relates to:

| ADR | Decision | Relates to |
|---|---|---|
| 0250 | An AI client can propose a scoped token; only the person creates it, after a narrowing-only review and a typed-name delegation confirmation, and the client learns nothing of the outcome. | 0013, 0034, 0104, 0164, 0165, 0195, 0220, 0236 |
| 0251 | A token proposal is attributed to the client that made it. | Amends 0006 (meta-tools were all unattributed) |
| 0252 | Each MCP session keeps its approved calls in memory, at most 200, so a token can be proposed from them. | — |
| 0253 | AI token proposals are off in organization mode until the administrator turns them on. Locally they are on for the owner. | 0165 |

If a parallel PR has taken any of these numbers by the time the last phase runs, use the next free
ones and update every reference.

## Manual steps

There is nothing to do before implementation.

After implementation, the person tries the whole flow twice:
- on a real local install, with Claude Code and the companion;
- on an organization test install with a passkey and the bundle flag.

CI cannot drive the companion clipboard, a real MCP client or a real authenticator, and none of
the `ci.dispatchable` workflows can either. The step-by-step page is
https://claude.ai/artifact/DY6WS8EiCnMpQmvWYDu1BQ (`manual_steps_artifact`).

## Risks and open questions

- **Splitting the store's validation could change its behaviour.** If an existing
  `tests/unit/test_scoped_tokens.py` test changes outcome after the D2 split, the split moved a
  check. p1 stops with `status=blocked`.
- **Suggestion values may not be lists.** `bundle_from_calls` only uses suggestions whose `value`
  is a list. If a tool's every usable suggestion has a non-list value, that tool lands in
  `uncovered_tools`, which is correct and needs no change.
- **The ledger read may come back empty.** Read `current_released_request_id()` on the line after
  `connector.call` returns, inside the `with` block. If
  `test_from_session_builds_from_approved_calls_only` still fails with it read there, p3 stops with
  `status=blocked`.
- **Local mode shows every agent as "Undetected"** (`agent_label.set_local_mode`). The review line
  then reads "Proposed by: Undetected", which is accurate.
- **ADR numbers may be taken by a parallel PR.** See the ADRs section.

## Implementation manifest

```yaml
plan_slug: ai-proposed-scoped-tokens
feature_branch: feature/ai-proposed-scoped-tokens
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/DY6WS8EiCnMpQmvWYDu1BQ
manual_steps_source: docs/ai-proposed-scoped-tokens-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-local-end-to-end
    title: Propose, review and use a token on a real local install with Claude Code and the companion
    why: CI cannot drive a real MCP client, the companion clipboard or the system browser together
  - id: ma2-org-end-to-end
    title: Propose and accept a token on an organization test install with a real passkey, and check the bundle flag
    why: The org secret dialog, passkey step-up bound to the proposal and the admin opt-in need a real authenticator and a real bundle
verify_after_merge:
  - python3 -m pytest tests/unit/test_scoped_tokens.py tests/unit/test_build_org_bundle.py -q
  - python3 -m pytest tests/unit/web/test_mcp_dispatch.py tests/unit/web/test_routes_mcp.py tests/unit/web/test_mcp_tools.py tests/unit/web/test_tool_schema_portability.py -q
  - python3 -m pytest tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py tests/unit/web/test_server.py tests/unit/test_settings_window_html.py -q
  - python3 -m pytest tests/unit/test_docs_tools_reference.py -q
final_checks:
  - docs/ai-proposed-scoped-tokens-plan.md and docs/ai-proposed-scoped-tokens-plan-manual-steps.html are deleted and nothing links to them
  - ADRs 0250, 0251, 0252 and 0253 (or their renumbered successors) exist, are Accepted, and are in docs/adr/README.md's index; the 0006 and 0164 index rows name them
  - CHANGELOG.md has the entry under "## [Unreleased]" and no new version heading
  - python3 -m pytest tests/integration/test_settings_scoped_token_proposal_browser.py -v reports passed, not skipped (PRIVACYFENCE_TEST_CHROMIUM set)
phases:
  - id: p1-policy-and-validation
    title: allow_ai_proposals policy, bundle flags, normalise_create_request and check_request
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/web/routes_settings.py
      - scripts/build_org_bundle.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_build_org_bundle.py
      - tests/unit/web/test_routes_settings.py
    brief: |
      Read the plan's Design sections D1 and D2 first; they hold every name, signature and string.
      1. src/privacyfence/scoped_tokens.py: add `allow_ai_proposals: bool = False` to
         `ScopedTokenPolicy`, the constant `PROPOSALS_DISABLED_MESSAGE`, and the parsing in
         `policy_from_org_config`, all exactly as D1 says.
      2. Move `_scoped_create_fingerprint_body` (src/privacyfence/web/routes_settings.py:368-388)
         into scoped_tokens.py as `normalise_create_request`, body unchanged. Replace every use in
         routes_settings.py with `scoped_tokens.normalise_create_request` (add it to the existing
         scoped_tokens import at :156). In tests/unit/web/test_routes_settings.py:1619-1660 replace
         `rs._scoped_create_fingerprint_body` with `scoped_tokens.normalise_create_request`
         (import `scoped_tokens` from privacyfence there); change nothing else in those tests.
      3. Split `ScopedTokenStore.create` (:496) into `_prepare`, `_check_locked`, `check_request`
         and `create` exactly as D2 says: the duplicate-label and 50-tokens checks stay in the
         same `with self._lock:` hold as minting inside `create`. Every existing message and its
         order stay the same. Stop with status=blocked if any existing test in
         tests/unit/test_scoped_tokens.py changes outcome.
      4. scripts/build_org_bundle.py: add the `--enable-scoped-token-proposals` /
         `--disable-scoped-token-proposals` mutually exclusive pair with D1's help text in the
         "Scoped AI client tokens (org mode)" group (:518-535), write
         `scoped_tokens.allow_ai_proposals` in the merge block (:776-784), and add one docstring
         line near :32-34.
      5. Tests (write them first and watch them fail):
         - tests/unit/test_scoped_tokens.py `TestPolicy`: test_local_policy_allows_ai_proposals,
           test_org_policy_needs_allow_ai_proposals_true,
           test_org_policy_ignores_allow_ai_proposals_without_enabled,
           test_org_policy_warns_on_non_bool_allow_ai_proposals.
         - tests/unit/test_scoped_tokens.py new class `TestCheckRequest`:
           test_check_request_writes_nothing,
           test_check_request_raises_the_same_errors_as_create (parametrised: bad label, days over
           the cap, unknown tool, blank value for a value-needing group),
           test_check_request_refuses_when_disabled,
           test_check_existing_false_skips_the_duplicate_label_check,
           test_check_existing_false_skips_the_token_limit,
           test_create_still_refuses_a_duplicate_label.
         - tests/unit/test_scoped_tokens.py:
           test_normalise_create_request_sorts_tools_and_rules.
         - tests/unit/test_build_org_bundle.py `TestScopedTokenFlags`:
           test_enable_and_disable_scoped_token_proposals_flags, test_merge_keeps_allow_ai_proposals
           (copy test_merge_keeps_the_other_scoped_token_key).
      6. Run `ruff check .`, `python3 -m mypy src/privacyfence` and the four test files below.
    acceptance:
      - python3 -m pytest tests/unit/test_scoped_tokens.py -q passes, including TestCheckRequest and the four new TestPolicy tests
      - python3 -m pytest tests/unit/test_build_org_bundle.py::TestScopedTokenFlags -q passes
      - python3 -m pytest tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py -q passes
      - grep -rn "_scoped_create_fingerprint_body" src tests prints nothing
      - grep -n "def normalise_create_request" src/privacyfence/scoped_tokens.py matches
      - python3 -m mypy src/privacyfence passes and ruff check . passes

  - id: p2-proposal-module
    title: scoped_token_proposals module (registry, narrowing, session bundle, review data, audit)
    depends_on: [p1-policy-and-validation]
    complexity: M
    touches:
      - src/privacyfence/scoped_token_proposals.py
      - src/privacyfence/audit_log.py
      - tests/unit/test_scoped_token_proposals.py
      - tests/conftest.py
    brief: |
      Read the plan's Design section D3 first; it holds every signature, constant and string.
      1. Create src/privacyfence/scoped_token_proposals.py with exactly the names in D3. No web
         imports; import `policy.*` and `auto_accept` function-locally inside
         `bundle_from_calls` and `review_data`. `new_proposal` makes the id as
         `ID_PREFIX + secrets.token_hex(16)` and trims `reason` to 500 characters.
      2. `record` builds an `audit_log.AuditEntry` with D3's fields, filling the other required
         fields the way `gate._audit` does (src/privacyfence/gate.py:1816-1840), inside
         `principal_scope(proposal.principal)` and `agent_scope(proposal.agent)`, and calls
         `audit_log.get_audit_logger().record(...)`; catch and log a failure as `gate._audit` does.
      3. src/privacyfence/audit_log.py: add the four decision strings from D3 to the
         `AuditEntry.decision` comment list (:139-155). Do not add them to
         `APPROVED_LIKE_DECISIONS`.
      4. tests/conftest.py `_reset()`: add `scoped_token_proposals.set_registry(None)` next to
         `scoped_tokens.set_store(None)` (:150), importing the module with the others (:43).
      5. tests/unit/test_scoped_token_proposals.py: plain pytest, `now=` passed explicitly (never
         sleep). Classes and tests:
         - TestRegistry: test_get_returns_a_live_proposal, test_get_refuses_another_principal,
           test_take_is_single_use, test_restore_puts_it_back, test_restore_skips_an_expired_one,
           test_expired_proposals_are_swept_and_reported_once, test_add_refuses_a_sixth_pending,
           test_pending_for_lists_only_this_principal, test_ids_are_prefixed_and_unique.
         - TestNarrowing: test_identical_request_is_allowed,
           test_dropping_a_rule_tool_or_days_is_allowed, test_dropping_a_verb_is_allowed,
           test_adding_a_rule_is_refused, test_adding_a_verb_is_refused,
           test_adding_a_tool_is_refused, test_more_days_is_refused,
           test_turning_on_a_personal_data_flag_is_refused,
           test_a_new_rule_with_another_value_is_refused, test_a_duplicate_rule_is_refused,
           test_the_label_may_change.
         - TestBundleFromCalls (monkeypatch `privacyfence.policy.suggest.suggested_rules` and pick
           real tool names from `auto_accept.TOOL_TO_GATE`): test_auto_tools_need_no_rule,
           test_unknown_tools_are_skipped, test_calls_on_one_resource_merge_their_verbs,
           test_each_call_uses_its_own_my_email,
           test_a_call_without_a_usable_suggestion_is_uncovered (requires_args set, value_known
           false, value None — one case each), test_output_is_sorted.
         - TestReviewData: test_new_rule_sentence, test_copy_rule_sentence,
           test_missing_copy_rule_says_so, test_agent_label_is_the_label_text.
         - TestRecord: test_record_names_the_agent_and_principal (read the entry back the way
           tests/unit/test_gate.py's audit assertions do),
           test_record_is_not_an_approved_like_decision.
      6. Run `ruff check .`, `python3 -m mypy src/privacyfence` and the coverage command below.
    acceptance:
      - python3 -m pytest tests/unit/test_scoped_token_proposals.py -q passes
      - python3 -m pytest tests/unit/test_scoped_token_proposals.py -q --cov=privacyfence.scoped_token_proposals --cov-branch --cov-report=term-missing reports 100%
      - grep -n "scoped_token_proposals.set_registry(None)" tests/conftest.py matches
      - grep -o "scoped_token_propos[a-z_]*" src/privacyfence/audit_log.py | sort -u | wc -l prints 4
      - python3 -m mypy src/privacyfence passes and ruff check . passes

  - id: p3-mcp-tool
    title: privacyfence_propose_scoped_token meta-tool, session ledger and attribution
    depends_on: [p2-proposal-module]
    complexity: M
    touches:
      - src/privacyfence/web/mcp_tools.py
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/web/routes_mcp.py
      - src/privacyfence/daemon_main.py
      - tests/unit/web/test_mcp_tools.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/web/test_routes_mcp.py
      - tests/unit/web/test_tool_schema_portability.py
      - tests/integration/test_shim_mcp_contract.py
      - tests/unit/test_daemon_main.py
    brief: |
      Read the plan's Design section D4 first; it holds the exact description, schema, step
      order, error texts and return value.
      1. src/privacyfence/web/mcp_tools.py: add `PROPOSE_SCOPED_TOKEN_TOOL` (D4, description
         verbatim) and append it to `META_TOOLS`.
      2. src/privacyfence/web/mcp_dispatch.py: the `scoped_token_policy` constructor keyword,
         `scoped_token_proposals_enabled()`, the session ledger (read
         `audit_log.current_released_request_id()` on the line right after
         `result = await connector.call(tool, args)`, inside the existing `with (...)` block at
         :276-279; the append condition and entry shape are in D4), the drop in `end_session`,
         and `async def propose_scoped_token(self, session_key, params, base_url)` with D4's nine
         steps in order (`LOCAL_PRINCIPAL` comes from `privacyfence.principal`).
      3. src/privacyfence/web/routes_mcp.py: hide the tool in `handle_list_tools` when
         `dispatcher.scoped_token_proposals_enabled()` is false; add the `_dispatch_meta_tool`
         branch; around :489 resolve the agent with `_resolve_agent(...)` for this one tool when
         `scope is None`, and update the comment above it to cite ADR 0251. Do not add the tool to
         `SCOPED_META_TOOLS`.
      4. src/privacyfence/daemon_main.py: add `from . import scoped_tokens` at the top, and pass
         `scoped_token_policy=scoped_tokens.policy_from_org_config({}, org_mode=False)` at :898 and
         `scoped_token_policy=scoped_tokens.policy_from_org_config(org_config, org_mode=True)` at
         :1233.
      5. Keep the existing complete-listing tests as they are by giving their dispatchers the
         local policy: in tests/unit/web/test_tool_schema_portability.py (around :88),
         tests/integration/test_shim_mcp_contract.py (the `McpDispatcher(...)` constructions near
         :224, :239, :261) and tests/unit/web/test_routes_mcp.py's dispatcher helper, pass
         `scoped_token_policy=scoped_tokens.policy_from_org_config({}, org_mode=False)`. Do not
         change their assertions.
      6. New tests (follow each file's existing patterns):
         - tests/unit/web/test_mcp_tools.py `TestMetaToolManifest`:
           test_propose_scoped_token_requires_reason_and_label,
           test_propose_scoped_token_is_not_read_only.
         - tests/unit/web/test_mcp_dispatch.py new class `TestProposeScopedToken`:
           test_bundle_proposal_returns_a_review_url_and_no_secret (no "pfst_" anywhere in the
           result; the store holds no token), test_proposal_is_registered_and_audited,
           test_missing_tools_or_rules_default_to_empty, test_refused_when_proposals_disabled,
           test_refused_for_a_local_non_owner, test_refused_in_an_unattended_session_and_audited,
           test_refused_under_a_scoped_token, test_duplicate_rule_kind_is_refused,
           test_from_session_and_rules_together_are_refused,
           test_days_are_capped_at_the_policy_max,
           test_invalid_request_errors_before_registering (bad label: registry stays empty),
           test_reproposing_an_existing_label_is_not_refused,
           test_from_session_builds_from_approved_calls_only (a denied call and an
           approval_pending call are not recorded), test_from_session_includes_auto_tools,
           test_from_session_with_no_calls_is_refused, test_ledger_drops_computed_args,
           test_session_ledger_is_dropped_on_end_session,
           test_session_ledger_keeps_the_newest_200, test_scoped_token_calls_are_not_recorded.
         - tests/unit/web/test_routes_mcp.py: add "privacyfence_propose_scoped_token" to the
           parametrize at :781-783; add test_propose_scoped_token_is_listed_only_when_enabled,
           test_propose_scoped_token_is_attributed_to_the_client (its audit entry's agent is the
           resolved client, not UNKNOWN_AGENT), test_other_meta_tools_stay_unattributed.
         - tests/unit/test_daemon_main.py: both dispatcher constructions receive a
           `scoped_token_policy` (local allow_ai_proposals True; org from org_config).
      7. Stop with status=blocked only if test_from_session_builds_from_approved_calls_only still
         fails with the read placed as step 2 says.
      8. Run `ruff check .`, `python3 -m mypy src/privacyfence`, the unit files in acceptance,
         and `python3 -m pytest tests/integration/test_mcp_daemon_contract.py tests/integration/test_shim_mcp_contract.py -v`.
    acceptance:
      - python3 -m pytest tests/unit/web/test_mcp_dispatch.py::TestProposeScopedToken -q passes
      - python3 -m pytest tests/unit/web/test_routes_mcp.py tests/unit/web/test_mcp_tools.py tests/unit/web/test_tool_schema_portability.py tests/unit/test_connector_catalog.py tests/unit/test_daemon_main.py -q passes
      - python3 -m pytest tests/integration/test_mcp_daemon_contract.py tests/integration/test_shim_mcp_contract.py -v passes
      - python3 -c "from privacyfence.web import routes_mcp as r; assert 'privacyfence_propose_scoped_token' not in r.SCOPED_META_TOOLS" exits 0
      - python3 -m mypy src/privacyfence passes and ruff check . passes

  - id: p4a-local-settings-routes
    title: Local review page, create_from_proposal and decline routes, page data, shim, build_html keyword
    depends_on: [p2-proposal-module]
    complexity: M
    touches:
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/server.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_server.py
    brief: |
      Read the plan's Design section D5 first; it holds the routes, gate order, status codes,
      error texts and audit summaries. This phase does local mode (`build_routes`), the shared
      shim and page data; org mode is p4b. From D6, only the `build_html` keyword belongs here.
      1. src/privacyfence/settings_window_html.py: add `scoped_proposal: dict | None = None` to
         `build_html` (:2576) and embed it as `window.__pfScopedProposal` next to
         `__pfScopedTokens` (:2648-2652), same escaping. Nothing else in this file.
      2. src/privacyfence/web/routes_settings.py, local `build_routes`:
         - add the `precheck` keyword to `_scoped_token_request` (:1165-1206) as D5 says (called
           after the fingerprint body, before step-up);
         - extract the body of `scoped_token_create` (:1208-1240) into `_create_and_deliver`;
           `scoped_token_create` calls it and its tests pass unchanged;
         - add `GET /settings/scoped_tokens`, `GET /settings/scoped_tokens/proposals/{proposal_id}`
           (rows read under `principal_scope(LOCAL_PRINCIPAL)`),
           `POST /api/settings/scoped_tokens/create_from_proposal` and
           `POST /api/settings/scoped_tokens/decline_proposal`, exactly as D5;
         - classify create_from_proposal in `_BESPOKE_SENSITIVE_ROUTE_PATHS` and decline_proposal
           in `_BESPOKE_EXEMPT_ROUTE_PATHS` with D5's reason; the GET routes need none (stop with
           status=blocked if TestBespokeRoutesAreClassified says otherwise);
         - `_settings_bridge_shim` (:490-640): the two `scopedRoutes` entries and the
           `window.location.assign('/settings/scoped_tokens')` success path from D5, for both
           local and org (the shim is shared).
      3. src/privacyfence/web/server.py: `scoped_token_page_data` (:348) and
         `org_scoped_token_page_data` add `proposals` and `proposals_enabled` as D5 says
         (`agent_label.label_for(p.agent).text`).
      4. Tests:
         - tests/unit/web/test_routes_settings.py new class `TestScopedTokenProposalRoutes`
           (reuse `_scoped_wiring`; register proposals with
           `scoped_token_proposals.get_registry().add(scoped_token_proposals.new_proposal(...))`):
           test_scoped_tokens_page_opens_the_section, test_review_page_embeds_the_proposal,
           test_review_page_for_unknown_id_says_missing,
           test_create_from_proposal_creates_and_delivers_through_the_companion,
           test_typed_label_must_match, test_widening_is_refused, test_expired_proposal_is_gone,
           test_proposal_is_single_use, test_create_failure_restores_the_proposal,
           test_companion_failure_restores_the_proposal,
           test_passkey_is_bound_to_the_proposal (an assertion made for proposal A, or for the
           un-narrowed request, is refused for the narrowed request),
           test_checks_run_before_step_up (a wrong typed_label gets 400, not 428),
           test_confirmation_and_human_session_required, test_accept_is_audited_with_the_agent,
           test_decline_takes_and_audits, test_decline_unknown_is_410,
           test_shim_maps_the_two_actions_and_goes_to_the_section.
         - tests/unit/web/test_server.py `TestScopedTokenWiring`:
           test_page_data_lists_pending_proposals,
           test_page_data_proposals_enabled_follows_the_policy.
      5. Run `ruff check .`, `python3 -m mypy src/privacyfence` and the files in acceptance.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_settings.py tests/unit/web/test_server.py tests/unit/test_settings_window_html.py -q passes
      - python3 -m pytest tests/unit/web/test_routes_settings.py::TestBespokeRoutesAreClassified tests/unit/web/test_routes_settings.py::TestScopedTokenProposalRoutes -q passes
      - python3 -c "from privacyfence.web import routes_settings as r; assert '/api/settings/scoped_tokens/create_from_proposal' in r._BESPOKE_SENSITIVE_ROUTE_PATHS and '/api/settings/scoped_tokens/decline_proposal' in r._BESPOKE_EXEMPT_ROUTE_PATHS" exits 0
      - python3 -m mypy src/privacyfence passes and ruff check . passes

  - id: p4b-org-settings-routes
    title: Organization-mode review page, create_from_proposal and decline routes
    depends_on: [p4a-local-settings-routes]
    complexity: M
    touches:
      - src/privacyfence/web/routes_settings.py
      - tests/unit/web/test_routes_org_settings.py
    brief: |
      Read the plan's Design section D5. p4a did local mode, the shim and page data; this phase
      mirrors the routes in `build_org_routes` (src/privacyfence/web/routes_settings.py:1442).
      Copy p4a's local implementation in the same file as the pattern.
      1. Add the `precheck` keyword to `_org_scoped_token_request` (:1784-1826).
      2. Extract the org `scoped_token_create` body (:1828-1863) into the org
         `_create_and_deliver`; the org create tests pass unchanged.
      3. Add the four routes from D5 in `build_org_routes`: the GETs (303 to `/settings` when
         scoped tokens or proposals are disabled; rows read under the signed-in principal's
         `principal_scope` after `_ensure_principal_settings_loaded()` as at :1833-1837), and the
         two POSTs with the org gates (`token_policy.enabled`, then `allow_ai_proposals` in the
         precheck) and the org response (`{"created": ..., "secret": ...}`, `Cache-Control:
         no-store`). The paths are already classified by p4a.
      4. tests/unit/web/test_routes_org_settings.py new class `TestScopedTokenProposalRoutes`:
         test_refused_when_proposals_disabled_in_the_bundle, test_secret_shown_once_with_no_store,
         test_org_step_up_bound_to_the_proposal, test_typed_label_must_match,
         test_widening_is_refused, test_another_principals_proposal_is_gone,
         test_review_page_redirects_when_disabled, test_review_page_embeds_the_proposal,
         test_decline_takes_and_audits, test_accept_audit_names_principal_and_proposal.
      5. Run `ruff check .`, `python3 -m mypy src/privacyfence` and the files in acceptance.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_org_settings.py -q passes, including TestScopedTokenProposalRoutes
      - python3 -m pytest tests/unit/web/test_routes_settings.py -q passes
      - python3 -m mypy src/privacyfence passes and ruff check . passes

  - id: p5-review-ui
    title: Review panel, waiting list and the typed-name delegation dialog in the settings page
    depends_on: [p4a-local-settings-routes]
    complexity: M
    touches:
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_settings_window_html.py
      - tests/integration/test_settings_scoped_token_proposal_browser.py
    brief: |
      Read the plan's Design section D6 first; every string, control and request field is there.
      Copy the patterns of `renderScopedTokens`, `duplicateScopedToken` and `submitScopedToken`
      (src/privacyfence/settings_window_html.py:1400-1600, 1815-1828).
      1. In `renderScopedTokens()`: the "Waiting for review" list from `SCOPED.proposals`, and
         the "Review proposed token" panel from `window.__pfScopedProposal` in place of the New
         token form (the missing text when `missing` is true). Panel state lives in new `ui.sp*`
         fields initialised next to `ui.st*` (:351-352).
      2. The personal-data boxes: shown only for flags the proposal set, unticked, with D6's hint.
      3. The Continue modal with D6's title, text, list, typed-name input (updates `ui.spTyped`
         and the button's `disabled` without a full `render()`), Cancel focused on open, Escape
         cancels, `Delegate access` disabled until the input equals the label exactly. Delegate
         posts `create_proposed_scoped_token` with D6's fields; Decline posts
         `decline_scoped_token_proposal` with `{proposal_id}`.
      4. Every agent-supplied string goes through `textContent` or the page's existing escape
         helper; add no unescaped `innerHTML` of proposal text.
      5. tests/unit/test_settings_window_html.py new class `TestScopedTokenProposal`:
         test_build_html_embeds_the_proposal_escaped (a label or reason containing
         `</script><img src=x onerror=alert(1)>` does not break out of the script tag),
         test_review_strings_present, test_delegate_button_text,
         test_waiting_list_renders_when_proposals_exist.
      6. tests/integration/test_settings_scoped_token_proposal_browser.py, marked
         `pytest.mark.browser`, built like tests/integration/test_settings_scoped_token_form_browser.py
         (set_content of build_html with `initial_section="scoped_tokens"`, a hand-written
         page-data dict and `scoped_proposal=`, posts captured by stubbing
         `window.webkit.messageHandlers.pf`):
         test_review_panel_replaces_the_new_token_form,
         test_delegate_is_disabled_until_the_name_is_typed_exactly (wrong case and partial keep it
         disabled), test_typing_keeps_focus_in_the_input, test_cancel_is_focused_and_escape_cancels,
         test_personal_data_boxes_start_unticked,
         test_unticking_a_rule_a_verb_and_a_tool_narrows_the_request,
         test_delegate_posts_the_proposal_id_and_typed_label, test_decline_posts_decline,
         test_missing_proposal_shows_the_notice, test_untrusted_reason_is_rendered_as_text.
      7. Run the browser test with `PRIVACYFENCE_TEST_CHROMIUM` set as the session-start hook
         exports it; SKIPPED is not a pass. If the hook warned that no Chromium launches, stop
         with status=blocked and say so.
      8. Run `ruff check .` and tests/unit/test_settings_window_html.py.
    acceptance:
      - python3 -m pytest tests/unit/test_settings_window_html.py -q passes
      - python3 -m pytest tests/integration/test_settings_scoped_token_proposal_browser.py -v reports every test passed and none skipped
      - python3 -m pytest tests/integration/test_settings_scoped_token_form_browser.py -v passes with none skipped
      - ruff check . passes

  - id: p6-docs-adrs-retire
    title: ADRs 0250-0253, reference docs, tools reference, changelog, delete the plan
    depends_on: [p3-mcp-tool, p4b-org-settings-routes, p5-review-ui]
    complexity: M
    touches:
      - docs/adr/02[5-9][0-9]-*.md
      - docs/adr/README.md
      - docs/adr/0006-attributing-a-request-to-the-ai-system-that-made-it.md
      - docs/adr/0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md
      - docs/how-it-works.md
      - docs/security-and-compliance.md
      - docs/approvals-and-policy.md
      - docs/connect-claude-code.md
      - docs/configuration-reference.md
      - docs/org-mode-setup-guide.md
      - docs/testing-policy.md
      - docs/tools-reference.md
      - scripts/generate_tools_reference.py
      - CHANGELOG.md
      - docs/ai-proposed-scoped-tokens-plan.md
      - docs/ai-proposed-scoped-tokens-plan-manual-steps.html
    brief: |
      1. `ls docs/adr` for the next free number. If 0250-0253 are taken, use the next free ones
         everywhere in this phase.
      2. Write the four ADRs in the plan's ADRs table with the template in docs/adr/README.md
         ("## Template"), Status "Accepted — <today>. Implemented.". Context from "Current
         state"; Decision from D1 (0253), D3-D6 (0250), D4's attribution paragraph (0251), D4's
         session ledger (0252); Alternatives from D7, each bullet into the ADR named in its
         parentheses. 0250's Related lists ADR 0013 with D7's sentence on why it still holds, and
         its Consequences say "the client never receives the token" is a protocol property (D7).
         0252 states the memory cost: up to 200 calls' raw arguments, possibly message text, per
         live session, never on disk. Verification names the test classes added in p1-p5. Link
         only source files, tests and ADRs, never this plan.
      3. docs/adr/README.md: four new index rows; append ", 0251" to the "amended by" list of
         0006's row (:118) and "; related: 0250" to 0164's row (:258). Add one Status line to ADR
         0006 ("Amended by ADR 0251: a token proposal is attributed to its client.") and to ADR
         0164 ("Related: ADR 0250 — an AI client can propose a token for a person to create.").
         Change nothing else in either ADR.
      4. Reference docs (today's behaviour, linking the ADRs):
         - docs/how-it-works.md: "eight" → "nine" at :116 and :176, adding "(the ninth,
           privacyfence_propose_scoped_token, only where token proposals are on: always on a local
           install, by the administrator's choice in organization mode)"; a row in the table at
           :180-191; one sentence in "### Scoped tokens" (:100).
         - scripts/generate_tools_reference.py:210: the same "nine" wording; regenerate
           docs/tools-reference.md with that script.
         - docs/connect-claude-code.md, "### Scoped tokens" under local (:70) and org (:100): a
           subsection "Let Claude propose a token": the review link, narrowing only, the
           personal-data boxes start unticked, the typed-name confirmation, and that Claude never
           receives the token or the outcome.
         - docs/security-and-compliance.md "## Scoped tokens" (:224-262): the proposal flow's
           guarantees (not offered to scoped tokens, refused in unattended sessions, owner only
           locally, single use, one hour, narrowing only, typed name, step-up bound to the
           proposal, the session ledger in memory only) and the key
           `scoped_tokens.allow_ai_proposals` beside :258-259.
         - docs/approvals-and-policy.md:443: keep "The box exists only in Settings, behind your
           passkey", and add "A token proposal can ask for it, but the box starts unticked and
           only you can tick it."
         - docs/configuration-reference.md (:299-300) and docs/org-mode-setup-guide.md (:358-359):
           rows for `--enable-scoped-token-proposals` / `--disable-scoped-token-proposals` and the
           key.
         - docs/testing-policy.md (:32-36): add `test_settings_scoped_token_proposal_browser.py` to
           the `browser` list.
      5. CHANGELOG.md under "## [Unreleased]", in the section's existing style, this line:
         "Claude can now propose a scoped token for a routine with
         `privacyfence_propose_scoped_token`, built from what you approved in the session or from
         rules it names. You review and narrow it in **Settings → Scoped tokens**, confirm by
         typing its name, and copy the token yourself; Claude never receives it. Organization
         installs turn this on with `--enable-scoped-token-proposals`." No version heading.
      6. Delete docs/ai-proposed-scoped-tokens-plan.md and
         docs/ai-proposed-scoped-tokens-plan-manual-steps.html.
      7. Run `python3 -m pytest tests/unit -q` and `ruff check .`.
    acceptance:
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes
      - grep -rn "ai-proposed-scoped-tokens-plan" --exclude-dir=.git . prints nothing
      - ls docs/adr/0250-*.md docs/adr/0251-*.md docs/adr/0252-*.md docs/adr/0253-*.md lists four files (or the renumbered ones)
      - grep -n "privacyfence_propose_scoped_token" docs/how-it-works.md docs/tools-reference.md matches in both
      - grep -n "allow_ai_proposals" docs/configuration-reference.md docs/security-and-compliance.md matches in both
      - grep -n "test_settings_scoped_token_proposal_browser" docs/testing-policy.md matches
      - python3 -m pytest tests/unit -q passes and ruff check . passes
```
