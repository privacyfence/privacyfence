# Plan: an AI client proposes a scoped token, a person creates it

## Goal

A person who has worked something out with Claude, approving calls by hand or through
auto-accept, often wants to turn it into a routine. The routine reaches `/mcp` with a scoped token
(ADR 0220), and building that token by hand in **Settings → Scoped tokens → New token** is slow
for a long recipe.

With this change Claude can call `privacyfence_propose_scoped_token`. It sends a bundle (name,
days, rules, tools, personal-data flags), or it asks PrivacyFence to build the bundle from the
calls approved in this MCP session. The tool returns a review link at once and that is all Claude
gets: never the token, and never whether one was made.

The person opens the link and reviews the proposal in Settings. There they can only narrow it.
They then confirm in a second dialog that warns them they are delegating access, and that dialog
works only after they type the token's name. If passkey step-up is in force it follows, bound to
the exact request. The token is then made by the existing create path and delivered the existing
way: to the clipboard through the companion in local mode, shown once in organization mode.

The tool is treated as highly sensitive:
- It is never offered to a scoped token.
- It is refused in an unattended session.
- In organization mode it stays off until the administrator turns it on.

## Current state

**Scoped tokens**
- The store is `src/privacyfence/scoped_tokens.py`.
- `ScopedTokenPolicy(enabled, max_days)` is at `:162-165`. `policy_from_org_config` (`:180-191`)
  always gives `(True, 365)` locally and reads `org_config["scoped_tokens"]` in organization mode.
- `normalise_rule_requests` (`:393-416`) and `_RULE_REQUEST_SHAPES` (`:386-390`) define the rule
  requests `new{group, value: str, verbs}`, `copy{rule_id}` and `duplicate{token_id, rule_id}`.
- `ScopedTokenStore.create` (`:496-595`) validates and mints in one step. Its errors, in order:
  - disabled;
  - a bad label (`LABEL_RE`, `:44`);
  - more than 50 rules;
  - a bad `new` rule;
  - a duplicate label;
  - days outside `1..policy.max_days`;
  - no rules and no tools;
  - a tool not in `auto_accept.TOOL_TO_GATE`;
  - a missing `copy` or `duplicate` source;
  - 50 tokens already.

  There is no way to validate a request without minting it.

**Create routes**
- Both modes live in `src/privacyfence/web/routes_settings.py`: local in `build_routes`, org in
  `build_org_routes` (`:1442`).
- `_scoped_create_fingerprint_body` (`:368-388`) normalises the create body. Step-up is bound to
  that body: `_verify_or_challenge(subject_key="create_scoped_token", fingerprint=...)`.
- Local:
  - Gates are in `_scoped_token_request` (`:1165-1206`).
  - The handler is `scoped_token_create` (`:1208-1240`). It calls `store_create` and then
    `send_secret` (the companion clipboard, `control_channel.send_mcp_token`), revokes the token if
    delivery fails, and returns `{"created": {id, label, expires_at}}` with no secret.
- Org:
  - Gates are in `_org_scoped_token_request` (`:1784-1826`).
  - The handler (`:1828-1863`) returns `{"created": ..., "secret": secret}` with
    `Cache-Control: no-store`.
- Every bespoke POST route must be listed in `_BESPOKE_SENSITIVE_ROUTE_PATHS` (`:297`) or
  `_BESPOKE_EXEMPT_ROUTE_PATHS` (`:319`). `TestBespokeRoutesAreClassified`
  (`tests/unit/web/test_routes_settings.py:2041`) enforces it.
- The settings bridge shim's `scopedRoutes` map (`:583-591`) turns a page `post(action, ...)` into
  a fetch, retries with a passkey on 428, and shows the org secret through
  `window.__pfShowScopedSecret`.

**Form and page data**
- The New token form is `renderScopedTokens()` in `src/privacyfence/settings_window_html.py`
  (`:1444-1600`).
- Its state is the `ui.st*` fields (`:351-352`). `submitScopedToken` (`:1815-1828`) builds the
  request.
- The only pre-fill today is client-side Duplicate (`duplicateScopedToken`, `:1400-1417`).
- `build_html(..., initial_section=..., scoped_tokens=page_data)` (`:2576`) embeds
  `window.__pfScopedTokens` and `window.__pfInitialSection` (`:2648-2655`).
- Page data comes from `scoped_token_page_data` (`src/privacyfence/web/server.py:348-413`), wired
  by `build_scoped_token_wiring` locally (`:416-458`) and `org_scoped_token_page_data` in org mode
  (`:1391-1400`).

**MCP meta-tools**
- Each is a `types.Tool` in `src/privacyfence/web/mcp_tools.py` (`:130-291`), with
  `META_TOOLS`/`META_TOOL_NAMES` at `:317-327`.
- Every description is at most 1024 characters (`tests/unit/web/test_tool_schema_portability.py`),
  and every meta-tool needs a `reason` property (`tests/unit/web/test_mcp_tools.py:192`).
- Listing is `handle_list_tools` (`src/privacyfence/web/routes_mcp.py:421-455`). Scoped tokens see
  only `SCOPED_META_TOOLS` (`:103-109`).
- Dispatch is `_dispatch_meta_tool` (`:576-612`), an if-chain receiving `base_url`.
- A meta-tool call runs as `UNKNOWN_AGENT` on purpose (`:479-491`, ADR 0006); only connector tools
  go through `_resolve_agent` (`:202-242`).
- Handlers are on `McpDispatcher` (`src/privacyfence/web/mcp_dispatch.py:96`). The unattended set
  is `_unattended_sessions` (`:138`), cleared in `end_session` (`:674-682`).
- The dispatcher is built in `src/privacyfence/daemon_main.py:898` (local) and `:1233-1238` (org,
  where `org_config` is in scope).
- It never sees the scoped-token policy.

**Nothing records a session's approved calls**
- `McpDispatcher.call` (`:229-322`) knows `session_key`, connector, tool and the agent's args.
- After `connector.call` returns, `audit_log.current_released_request_id()` (`audit_log.py:110-126`)
  is non-empty exactly when the gate recorded an approved-like decision (`APPROVED_LIKE_DECISIONS`,
  `audit_log.py:95-97`).
- `policy/suggest.py`'s `suggested_rules(tool, args, my_email)` (`:69-144`) turns one call into
  candidate rules. Each candidate is
  `{group, label, value: list|[]|None, value_known, verb, operation, requires_args?}`, narrowest
  first.
- `McpDispatcher.check_policy` (`:355-400`) already calls it and shows how `my_email` and
  `agent_args` are found.

**Precedent**
- `gate.propose_policy_change` (`src/privacyfence/gate.py:1521-1658`):
  - It refuses a scoped token, validates before any dialog, refuses an unattended session (audits
    `denied_unattended`), and blocks on a sensitive confirm.
  - It audits through `gate._audit` (`:1804`).
- A blocking tool cannot work here: a review that takes minutes outruns MCP client timeouts. The
  user decided the tool returns only a link.

**Docs**
- `docs/how-it-works.md:116,176` say "eight `privacyfence_*` tools". So does
  `scripts/generate_tools_reference.py:210`, which regenerates `docs/tools-reference.md`;
  `tests/unit/test_docs_tools_reference.py` fails when that file is stale.
- `docs/approvals-and-policy.md:443` says the personal-data box exists only in Settings, which stays
  true: a proposal can ask for it, but only the person can keep it.

## Design

### D1. Policy: `ScopedTokenPolicy.allow_ai_proposals`

In `src/privacyfence/scoped_tokens.py`:

```python
@dataclass(frozen=True)
class ScopedTokenPolicy:
    enabled: bool
    max_days: int
    allow_ai_proposals: bool = False
```

- Local mode: `policy_from_org_config({}, org_mode=False)` returns
  `ScopedTokenPolicy(True, LOCAL_MAX_DAYS, True)`. There is no administrator, as for scoped tokens
  themselves.
- Org mode: `allow_ai_proposals = section.get("allow_ai_proposals") is True and enabled`. It is off
  unless both are true, and a non-bool value logs
  `"scoped_tokens.allow_ai_proposals must be true or false; using false"`.

New helper:

```python
def proposals_enabled_message() -> str: ...
```

It returns the text `PROPOSALS_DISABLED_MESSAGE`:
`"Token proposals are not enabled on this install. Your administrator turns them on in the organization config bundle."`

`scripts/build_org_bundle.py` gets a mutually exclusive pair in the "Scoped AI client tokens (org
mode)" argument group (`:518-535`):
- `--enable-scoped-token-proposals` / `--disable-scoped-token-proposals`, writing
  `scoped_tokens.allow_ai_proposals` in the same merge block (`:776-784`), so `--merge` keeps other
  keys.
- Help text: `"Let AI clients propose scoped tokens for a person to review (off by default; needs --enable-scoped-tokens)."`
- The docstring at `:32-34` gains one line.

### D2. Request normalisation and dry-run validation

- Move `_scoped_create_fingerprint_body` from `routes_settings.py` to `scoped_tokens.py` as
  `normalise_create_request(payload: dict[str, Any]) -> dict[str, Any] | None`. The body is
  unchanged, the same keys come out, and it is the same function, renamed.
- `routes_settings.py` imports it and calls it at every place the old name was used. No alias is
  kept.
- Split `ScopedTokenStore.create` into:
  - `_validate(self, principal, *, label, days, rules, tools, policy) -> tuple[ScopedTokenRow..., frozenset[str]]`.
    It is private, holds every check `create` makes today in the same order with the same messages,
    and returns the built rows and the tool set. The 50-tokens check stays in it.
  - `check_request(self, principal, *, label, days, rules, tools, policy, allow_personal_data=False, allow_personal_data_reads=False) -> None`.
    It is public, calls `_validate` under the same lock order `create` uses, and writes nothing.
  - `create(...)`, which calls `_validate` and then mints exactly as now.
- Use whatever row type `create` builds today (`ScopeRow`); the signature above names the idea, not
  a new type.

### D3. The proposal module: `src/privacyfence/scoped_token_proposals.py`

It has no web imports.

```python
PROPOSAL_TTL_SECONDS = 3600
MAX_PENDING_PER_PRINCIPAL = 5
REVIEW_PATH = "/settings/scoped_tokens/proposals/{proposal_id}"
ID_PREFIX = "stp-"

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

class ProposalError(ValueError):
    """Shown verbatim."""

class ProposalRegistry:
    def __init__(self, on_expired: Callable[[TokenProposal], None] | None = None) -> None: ...
    def add(self, proposal: TokenProposal, now: float | None = None) -> None
    def get(self, principal_id: str, proposal_id: str, now: float | None = None) -> TokenProposal | None
    def take(self, principal_id: str, proposal_id: str, now: float | None = None) -> TokenProposal | None
    def restore(self, proposal: TokenProposal) -> None
    def pending_for(self, principal_id: str, now: float | None = None) -> list[TokenProposal]

def get_registry() -> ProposalRegistry
def set_registry(registry: ProposalRegistry | None) -> None
def new_proposal(*, principal, request, source, agent, reason, uncovered_tools=(), now=None) -> TokenProposal
def narrowing_error(proposed: dict[str, Any], final: dict[str, Any]) -> str | None
def bundle_from_calls(calls: Sequence[tuple[str, dict[str, Any]]], my_email: str) -> tuple[list[dict[str, Any]], list[str], list[str]]
def review_data(proposal: TokenProposal, own_rows: Sequence[ScopeRow]) -> dict[str, Any]
def record(decision: str, proposal: TokenProposal, summary: str) -> None
```

**The registry**
- It is in memory, behind a `threading.Lock`.
- Every public method first sweeps entries with `expires_at <= now`. It removes them and, after
  releasing the lock, calls `on_expired(p)` for each.
- `get_registry()` lazily builds one with `on_expired=lambda p: record("scoped_token_proposal_expired", p, f"Token proposal '{label}' expired")`.
- `add` raises `ProposalError("Too many token proposals are waiting. Review or decline them in Settings → Scoped tokens.")`
  when the principal already has `MAX_PENDING_PER_PRINCIPAL` live proposals.
- `get`/`take` return `None` for an unknown id, an expired one, or another principal's.
- `take` removes the entry (single use). `restore` puts a taken proposal back if it has not expired.
- `tests/conftest.py`'s `_reset()` calls `scoped_token_proposals.set_registry(None)`.

**`narrowing_error`** returns `None` when `final` is no wider than `proposed`, else
`"A proposed token can only be narrowed here. To add rules or tools, use New token."`.
- Every `final` rule must match a `proposed` rule:
  - a `copy` must have the same `rule_id`;
  - a `new` must have the same `group` and the same `value` string, with `set(final.verbs) <= set(proposed.verbs)`
    and non-empty verbs;
  - a `duplicate` never matches.
- `set(final.tools) <= set(proposed.tools)`.
- `final.days <= proposed.days`.
- Each personal-data flag may be true in `final` only if it is true in `proposed`.
- The label may differ.

**`bundle_from_calls`** takes `(tool, args)` pairs and returns `(rules, tools, uncovered_tools)`:
- A tool not in `auto_accept.TOOL_TO_GATE` is skipped.
- Every other tool goes into `tools`.
- A tool whose gate is `"auto"` needs no rule.
- For the rest, it calls `policy.suggest.suggested_rules(tool, args, my_email)` and takes the first
  suggestion that has no `requires_args`, `value_known` true and `value is not None`.
  - None found: the tool goes into `uncovered_tools`.
  - Found: it adds `suggestion["verb"]` to the verbs for the key
    `(suggestion["group"], ",".join(suggestion["value"]))`.
- `rules` is `[{"kind": "new", "group": g, "value": v, "verbs": sorted(verbs)} ...]`, sorted by
  `(group, value)`.
- `tools` and `uncovered_tools` are sorted and de-duplicated.

**`review_data`** is what the review page renders:

```
{"id", "label", "days", "tools": [...], "allow_personal_data", "allow_personal_data_reads",
 "rules": [{"request": <the rule request dict>, "sentence": str}],
 "agent_label": agent_label.label_for(proposal.agent), "reason", "source",
 "uncovered_tools": [...], "expires_at"}
```

- Each `sentence` is built from the rows the request would compile to:
  - `new`: `policy_describe.row_sentence(row)` over
    `policy_scope_rows.add_rules([], policy_catalogue.rules_for_catalogue_entry(group, policy_catalogue.parse_value_list(value, group), policy_catalogue.parse_verbs(verbs)))[0]`,
    joined with `"; "`;
  - `copy`: the sentence of the matching own row, or `"A rule that no longer exists"` if it is gone.

**`record`** writes one `AuditEntry` through `audit_log.get_audit_logger()`:
- It runs under `principal_scope(proposal.principal)` and `agent_scope(proposal.agent)`, so the
  agent fields are filled by `_stamp_entry`.
- Fields: `connector="policy"`, `tool="privacyfence_propose_scoped_token"`, `decision=decision`,
  `summary=summary`, `claude_reason=proposal.reason`, `request_id=proposal.id`,
  `sender=proposal.principal.email or proposal.principal.id`.
- Copy the remaining required fields' values from `gate._audit` (`gate.py:1816-1840`).

The four new decision strings are:
- `scoped_token_proposed`
- `scoped_token_proposal_accepted`
- `scoped_token_proposal_declined`
- `scoped_token_proposal_expired`

None of them goes into `APPROVED_LIKE_DECISIONS`. Add them to the `AuditEntry.decision` comment
list (`audit_log.py:139-155`).

### D4. The MCP tool `privacyfence_propose_scoped_token`

**Declaration**
- In `mcp_tools.py` add `PROPOSE_SCOPED_TOKEN_TOOL`, appended to `META_TOOLS`.
- Annotations: `read_only_hint=False`, `destructive_hint=False`, `idempotent_hint=False`.
- Description, exactly (under 1024 characters):

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

- `input_schema` properties:
  - `reason` (string)
  - `label` (string)
  - `days` (integer)
  - `from_session` (boolean)
  - `rules` (array of objects)
  - `tools` (array of strings)
  - `allow_personal_data` (boolean)
  - `allow_personal_data_reads` (boolean)

  `required: ["reason", "label"]`.

**Listing and calling** (`routes_mcp.py`)
- `handle_list_tools` leaves the tool out when `dispatcher.scoped_token_proposals_enabled()` is
  false.
- It stays out of `SCOPED_META_TOOLS`, so a scoped token neither sees it nor may call it (the
  existing `_refuse_scoped_call` path).
- `_dispatch_meta_tool` gets a branch calling
  `await dispatcher.propose_scoped_token(session_key, arguments, base_url)`.
- Attribution: at `:489`, when `name == mcp_tools.PROPOSE_SCOPED_TOKEN_TOOL.name` and `scope is None`,
  resolve the agent with `_resolve_agent(...)` as for connector tools. Every other meta-tool keeps
  `UNKNOWN_AGENT`. The comment above that line is updated to say so and cite ADR 0251.

**`McpDispatcher` (`mcp_dispatch.py`)**
- New constructor keyword `scoped_token_policy: ScopedTokenPolicy | None = None`.
  - `daemon_main.py:898` passes `policy_from_org_config({}, org_mode=False)`.
  - `:1233` passes `policy_from_org_config(org_config, org_mode=True)`.
- `scoped_token_proposals_enabled(self) -> bool` returns `policy is not None and policy.enabled and policy.allow_ai_proposals`.
- **Session ledger:**
  - `self._session_calls: dict[Hashable, list[tuple[str, dict[str, Any]]]] = {}` and
    `MAX_SESSION_CALLS = 200`.
  - In `call()`, after `connector.call` returns normally (not `ApprovalPending`, not an exception),
    while `current_token_scope() is None` and `audit_log.current_released_request_id()` is
    non-empty (read inside `released_request_scope()`), append `(tool, agent_args)`. Keep the
    newest 200.
  - `end_session` drops the key. Nothing is written to disk.
- **`async def propose_scoped_token(self, session_key, params, base_url) -> dict`**, in order:
  1. `current_token_scope()` not None: raise
     `GateDeniedError("Request denied: a scoped token cannot propose tokens.")`. This is defence in
     depth; the route already refuses.
  2. Not `scoped_token_proposals_enabled()`: raise `ValueError(PROPOSALS_DISABLED_MESSAGE)`.
  3. `session_key in self._unattended_sessions`: record a `denied_unattended` audit entry (`connector="policy"`, the same
     fields as `gate.propose_policy_change`'s at `gate.py:1602-1606`, through `gate._audit`), then
     raise
     `GateDeniedError("Request denied: this connection is in an unattended session, so nobody is there to review a token proposal.")`.
  4. Build the request:
     - With `from_session` true:
       - `rules` or `tools` also given: raise `ValueError("Pass either from_session or rules and tools, not both.")`.
       - Get `(rules, tools, uncovered) = bundle_from_calls(self._session_calls.get(session_key, []), my_email)`,
         with `my_email` found exactly as `check_policy` finds it.
       - Empty `rules` and `tools`: raise
         `ValueError("This session has no approved calls to build a token from.")`.
     - Otherwise:
       - A rule whose `kind` is not `new` or `copy`: raise
         `ValueError("A proposed rule is {kind:'new', ...} or {kind:'copy', rule_id}.")`.
     - `days` defaults to `scoped_tokens.DEFAULT_DAYS`, then becomes `min(days, policy.max_days)`.
     - Normalise with `normalise_create_request`. `None`: raise
       `ValueError("The proposal is not a valid token request.")`.
  5. `_ = self.connectors`, as in `propose_policy_change`. Then run
     `scoped_tokens.get_store().check_request(principal, ..., policy=policy)` in `asyncio.to_thread`.
     A `ScopedTokenError` propagates and its message reaches Claude.
  6. `proposal = new_proposal(principal=current_principal(), request=..., source="bundle"|"session", agent=current_agent(), reason=params["reason"], uncovered_tools=...)`,
     then `get_registry().add(proposal)` (a `ProposalError` propagates).
  7. `record("scoped_token_proposed", proposal, f"Token '{label}' proposed: {n} rule(s), {t} tool(s), {days} days")`.
  8. Return:

     ```python
     {"proposal_id": proposal.id,
      "review_url": base_url.rstrip("/") + REVIEW_PATH.format(proposal_id=proposal.id),
      "expires_at": <ISO 8601 UTC of expires_at>,
      "label": ..., "days": ..., "tools": [...],
      "rules": [<review_data sentences>],
      "uncovered_tools": [...],
      "message": "Give the person this link. They review the token in PrivacyFence Settings, can narrow it, and confirm it themselves; you will not receive the token or learn whether it was created. Tools in uncovered_tools have no rule, so the token's calls to them will be denied."}
     ```

### D5. Settings routes (both modes, `routes_settings.py`)

**`GET /settings/scoped_tokens/proposals/{proposal_id}`**
- Local: behind the same auth as `/settings`. Org: behind the same sign-in as `/settings`.
- It renders the settings page with `initial_section="scoped_tokens"` and a new `build_html` keyword
  `scoped_proposal`:
  - `review_data(p, auto_accept.get_policy_rows())` for a live proposal of this principal;
  - otherwise `{"missing": True}`.
- In org mode, when scoped tokens or proposals are disabled, it renders the page with
  `{"missing": True}`.

**`POST /api/settings/scoped_tokens/create_from_proposal`**
- Listed in `_BESPOKE_SENSITIVE_ROUTE_PATHS`.
- Body: the create fields plus `proposal_id` (str), `typed_label` (str), `confirmed` (true) and, on
  retry, `webauthn_assertion`.
- Gates, in this order:
  1. The same gates as that mode's create route (auth, CSRF/origin, `confirmed is True`, local
     human session, org `token_policy.enabled`).
  2. Org: `token_policy.allow_ai_proposals`, else 403 `{error: PROPOSALS_DISABLED_MESSAGE}`.
  3. `body = normalise_create_request(payload)`, else 400 "invalid request".
  4. `typed_label == body["label"]`, else 400 `{error: "Type the token's name exactly to confirm."}`.
  5. `p = registry.get(principal.id, proposal_id)`, else 410
     `{error: "This token proposal has expired or was already used."}`.
  6. `narrowing_error(p.request, body)`, else 400 with that text.
  7. Step-up when in force: `_verify_or_challenge(subject_key="create_proposed_scoped_token", fingerprint={**body, "proposal_id": proposal_id})`.
     The 428/403 responses are the same as create's.
  8. `registry.take(...)`. `None` gives the 410 above.
- Then create and deliver exactly as that mode's create handler does. Extract the shared part of
  each mode's `scoped_token_create` into a local helper, `_create_and_deliver(...)`, so both routes
  share it.
- If creating raises `ScopedTokenError`, or local delivery fails, call `registry.restore(p)` and
  return the same error the create route returns.
- On success:
  - `record("scoped_token_proposal_accepted", p, f"Token proposal '{p.request['label']}' accepted as '{label}' ({token_id})")`;
  - the existing "Created scoped token ..." settings audit entry, with `" from a proposal"` added
    before the principal suffix;
  - the same response as create.

**`POST /api/settings/scoped_tokens/decline_proposal`**
- Listed in `_BESPOKE_EXEMPT_ROUTE_PATHS` with reason
  `"declining only discards a proposal; it creates and widens nothing"`.
- Body `{proposal_id, csrf}` with the mode's normal auth and CSRF/origin checks.
- `take`, then `record("scoped_token_proposal_declined", p, f"Token proposal '{label}' declined")`,
  then `{"declined": true}`. An unknown id gives 410 with the text above.

**Page data**
- `scoped_token_page_data` (`server.py:348`) gains
  `"proposals": [{"id", "label", "agent_label", "expires_at"} ...]`, from
  `get_registry().pending_for(principal.id)`.
- Also `"proposals_enabled": bool`: local always true; org `policy.allow_ai_proposals`.
- `org_scoped_token_page_data` passes the same.

**Bridge shim** (`_settings_bridge_shim`)
- `scopedRoutes` gains
  `create_proposed_scoped_token: ['create_from_proposal', null]` and
  `decline_scoped_token_proposal: ['decline_proposal', 'Decline this token proposal?']`.
- `null` means no `window.confirm`: the page's own dialog is the confirmation.
- The existing 428 retry and secret dialog apply unchanged.

### D6. The review UI (`settings_window_html.py`)

- `build_html(..., scoped_proposal: dict | None = None)` embeds `window.__pfScopedProposal`, using
  the same JSON-in-script escaping as `__pfScopedTokens`.
- In `renderScopedTokens()`:
  - When `SCOPED.proposals` is non-empty, a "Waiting for review" list goes above the New token
    form. Each row reads `{label} — proposed by {agent_label}` with a link "Review" to
    `/settings/scoped_tokens/proposals/{id}`.
  - When `window.__pfScopedProposal` is set, a "Review proposed token" panel replaces the New token
    form.
- All agent-supplied text (label, reason, agent_label, sentences, tool names) is set with
  `textContent` or the page's existing escape helper, never as HTML.

**Missing proposal:** the panel shows only
`"This token proposal has expired, was declined or was already used."`.

**Live proposal**, in order:
- Heading `Review proposed token`.
- Intro: `"An AI client proposed this token. Untick anything the job does not need. You can only narrow it here; to add more, use New token."`
- `Proposed by: {agent_label}. The AI client reports this name itself; PrivacyFence does not verify it.`
- `Reason given: {reason}`.
- Name input (pre-filled) and days input (`max` = proposal days).
- One checkbox per rule (its sentence, ticked).
- For a `new` rule with more than one verb, one checkbox per verb (ticked).
- One checkbox per tool (ticked).
- The two personal-data checkboxes, shown only for a flag the proposal set, ticked, with the
  existing labels.
- When `uncovered_tools` is non-empty:
  `"No rule covers these tools, so the token's calls to them will be denied: {list}."`
- Buttons `Decline` (posts `decline_scoped_token_proposal`, then reloads `/settings`) and
  `Continue`.

**`Continue`** opens a modal over the page:
- Title `Delegate access to this token?`.
- Text:
  `"You are delegating access. Anything that holds this token can act as you within these rules, for {days} days, with no approval card and no prompt. PrivacyFence cannot tell who is using it. Revoke it in Settings → Scoped tokens if it leaks or the job is retired."`
- If any personal-data flag is still ticked:
  `"This token may also return or upload personal data."`
- The final rule sentences and tool names, as a list.
- Label `Type the token name to confirm`, with an empty text input.
- `Cancel` (focused when the modal opens; Escape also cancels) and `Delegate access`.
  `Delegate access` is disabled until `input.value === label` exactly.
- `Delegate access` posts `create_proposed_scoped_token` with
  `{proposal_id, label, days, rules, tools, allow_personal_data, allow_personal_data_reads, typed_label}`:
  - `rules` holds only the ticked rules, a `new` rule with only its ticked verbs. A `new` rule with
    no verb ticked is left out.
  - `tools` holds the ticked tools.
- After success the existing path runs: the secret dialog in org mode, a reload in local mode. The
  page then goes to `/settings` with the scoped tokens section open.

### D7. What was rejected (for the ADRs)

- **Blocking until the person finishes**, like `propose_policy_change`: a review with a typed name
  and a passkey outruns MCP client tool-call timeouts. Claude could not set up the routine's
  environment with the token anyway.
- **An await/status tool:** Claude has no use for the outcome, and reporting it would tell a client
  whether its proposal was accepted.
- **Returning the secret to the client:** it would sit in the chat transcript (ADR 0104, ADR 0164).
- **Editing that can widen the proposal:** the review would then be a second New token form that
  an AI client fills in. Widening stays on the ordinary form.
- **Relying on step-up alone:** step-up is optional per install. The typed name makes the
  confirmation deliberate everywhere and stops a reflex click or a browser driven by an agent.
- **Putting the review in the approvals list as a confirm dialog:** the decide route carries only
  `result`/`choice`/`note`. Its step-up fingerprint covers none of the form, and confirms are never
  swept (`gate._pop_registry_expirations` runs only on a gated call).
- **On by default in organization mode:** this is a new way to mint credentials, so the
  administrator opts in.
- **Leaving the proposing call unattributed (ADR 0006):** the person needs to see which client asked
  for the delegation.
- **Deriving the bundle from the audit log:** audit entries carry no arguments, so no rule value
  could be recovered. The in-memory session ledger holds them only for the session's life.
- **`duplicate` rules in a proposal:** a client never sees the person's token ids.

## ADRs

- **0250** — An AI client can propose a scoped token, and only the person creates it, after a
  narrowing-only review and a typed-name delegation confirmation. Relates to 0164, 0165, 0220,
  0236, 0195, 0034, 0104.
- **0251** — A token proposal is attributed to the client that made it (amends 0006: meta-tools
  were all unattributed).
- **0252** — Each MCP session keeps its approved calls in memory so that a token can be proposed
  from them.

If a parallel PR has taken any of these numbers by the time the last phase runs, use the next free
ones and update every reference.

## Manual steps

None before implementation.

After implementation, the person tries the whole flow once on a real local install, with Claude
Code and the companion, and once on an organization test install with a passkey. CI cannot drive
the companion clipboard, a real MCP client or a real authenticator. The step-by-step page is
at https://claude.ai/artifact/DY6WS8EiCnMpQmvWYDu1BQ (`manual_steps_artifact`).

## Risks and open questions

**`ScopedTokenStore.create`'s validation**
- If it is not one contiguous block before minting (for example, a check that needs the store lock
  held across minting), the `_validate` split in D2 may change behaviour.
- The p1 worker stops with `status=blocked` if any existing `tests/unit/test_scoped_tokens.py` test
  changes outcome after the split.

**`suggested_rules` values**
- If any `value` is not a `list[str]` when `value_known` is true, `",".join` fails.
- p2 stops with `status=blocked` and reports the tool.

**`current_released_request_id()`**
- If it is not readable in `McpDispatcher.call` after `connector.call` returns (for example,
  because `released_request_scope()` resets it on exit), p3 stops with `status=blocked` rather
  than inventing another signal.

**Agent attribution in local mode**
- Every agent shows as "Undetected" (`agent_label.set_local_mode`). The review line then reads
  "Proposed by: Undetected", which is accurate; nothing to fix.

**Rendering `review_data`**
- If `policy_describe.row_sentence` or `policy_scope_rows.add_rules` have other signatures than
  D3 assumes, p2 adapts the call (they are internal) but keeps `review_data`'s output shape
  exactly.

**ADR numbers**
- They may be taken by a parallel PR first; see the ADRs section.

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
  - python3 -m pytest tests/unit/test_scoped_token_proposals.py -q
  - python3 -m pytest tests/unit/web/test_mcp_dispatch.py tests/unit/web/test_routes_mcp.py tests/unit/web/test_mcp_tools.py tests/unit/web/test_tool_schema_portability.py -q
  - python3 -m pytest tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py tests/unit/web/test_server.py tests/unit/test_settings_window_html.py -q
  - python3 -m pytest tests/unit/test_docs_tools_reference.py -q
final_checks:
  - docs/ai-proposed-scoped-tokens-plan.md and docs/ai-proposed-scoped-tokens-plan-manual-steps.html are deleted and nothing links to them
  - ADRs 0250, 0251 and 0252 (or their renumbered successors) exist, are Accepted, and are in docs/adr/README.md's index
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
      Read the plan's Design sections D1 and D2 first; they hold every name and string.
      1. src/privacyfence/scoped_tokens.py: add `allow_ai_proposals: bool = False` to
         `ScopedTokenPolicy` and parse it in `policy_from_org_config` exactly as D1 says (local
         mode returns `ScopedTokenPolicy(True, LOCAL_MAX_DAYS, True)`; org: `is True` and
         `enabled`, warning text from D1 for a non-bool). Add the module constant
         `PROPOSALS_DISABLED_MESSAGE` with D1's text.
      2. Move `_scoped_create_fingerprint_body` from src/privacyfence/web/routes_settings.py
         (:368-388) into scoped_tokens.py as `normalise_create_request`, body unchanged. Replace
         every use in routes_settings.py with `scoped_tokens.normalise_create_request` (import it
         with the module's other scoped_tokens imports at :156). Keep no alias.
      3. Split `ScopedTokenStore.create` (:496) into `_validate` + `check_request` + `create` as
         D2 describes. Every existing error message and its order stay exactly the same. Stop
         with status=blocked if any existing test in tests/unit/test_scoped_tokens.py changes
         outcome.
      4. scripts/build_org_bundle.py: add `--enable-scoped-token-proposals` /
         `--disable-scoped-token-proposals` as a mutually exclusive pair in the "Scoped AI client
         tokens (org mode)" group (:518-535) with D1's help text, write
         `scoped_tokens.allow_ai_proposals` in the merge block (:776-784), and add one docstring
         line near :32-34.
      5. Tests (write them first and watch them fail):
         - tests/unit/test_scoped_tokens.py: in `TestPolicy` add
           `test_local_policy_allows_ai_proposals`, `test_org_policy_needs_allow_ai_proposals_true`,
           `test_org_policy_ignores_allow_ai_proposals_without_enabled`,
           `test_org_policy_warns_on_non_bool_allow_ai_proposals`. Add class
           `TestCheckRequest` with `test_check_request_writes_nothing`,
           `test_check_request_raises_the_same_errors_as_create` (parametrised over a bad label, days
           over the cap, an unknown tool, a blank value for a value-needing group),
           `test_check_request_refuses_when_disabled`. Add
           `test_normalise_create_request_matches_the_old_fingerprint_body` (same output as the
           sorted dict the old helper built for one rich payload).
         - tests/unit/test_build_org_bundle.py: in `TestScopedTokenFlags` add
           `test_enable_and_disable_scoped_token_proposals_flags` and
           `test_merge_keeps_allow_ai_proposals` (copy test_merge_keeps_the_other_scoped_token_key).
         - tests/unit/web/test_routes_settings.py: existing `TestScopedTokenRoutes` must pass
           unchanged.
      6. Run `ruff check .` and the three test files.
    acceptance:
      - python3 -m pytest tests/unit/test_scoped_tokens.py -q passes, including TestCheckRequest and the four new TestPolicy tests
      - python3 -m pytest tests/unit/test_build_org_bundle.py::TestScopedTokenFlags -q passes
      - python3 -m pytest tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py -q passes
      - grep -n "_scoped_create_fingerprint_body" -r src tests returns nothing
      - grep -n "def normalise_create_request" src/privacyfence/scoped_tokens.py matches
      - ruff check . passes

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
      1. Create src/privacyfence/scoped_token_proposals.py with exactly the names in D3:
         `PROPOSAL_TTL_SECONDS`, `MAX_PENDING_PER_PRINCIPAL`, `REVIEW_PATH`, `ID_PREFIX`,
         `TokenProposal`, `ProposalError`, `ProposalRegistry` (add/get/take/restore/pending_for,
         sweep on every call, `on_expired` called outside the lock), `get_registry`/`set_registry`,
         `new_proposal` (id = ID_PREFIX + secrets.token_hex(16); reason trimmed to 500 chars),
         `narrowing_error`, `bundle_from_calls`, `review_data`, `record`. No web imports. Use
         function-local imports for `policy.*` if a top-level import cycles (scoped_tokens.py does
         the same at :91).
      2. `record` builds an `audit_log.AuditEntry` with the fields D3 lists, filling the other
         required fields the way `gate._audit` does (src/privacyfence/gate.py:1816-1840), inside
         `principal_scope(proposal.principal)` and `agent_scope(proposal.agent)`, and calls
         `audit_log.get_audit_logger().record(...)`. Logging failures are caught and logged as
         `gate._audit` does.
      3. src/privacyfence/audit_log.py: add the four decision strings from D3 to the
         `AuditEntry.decision` comment list (:139-155). Do not add them to
         `APPROVED_LIKE_DECISIONS`.
      4. tests/conftest.py `_reset()`: add `scoped_token_proposals.set_registry(None)` next to
         `scoped_tokens.set_store(None)` (:150), and import the module there.
      5. If a `suggested_rules` value with `value_known` true is not a list of strings, stop with
         status=blocked naming the tool.
      6. tests/unit/test_scoped_token_proposals.py, plain pytest, `now=` passed explicitly (no
         sleeping), with classes and tests:
         - TestRegistry: test_get_returns_a_live_proposal, test_get_refuses_another_principal,
           test_take_is_single_use, test_restore_puts_it_back, test_restore_skips_an_expired_one,
           test_expired_proposals_are_swept_and_reported_once, test_add_refuses_a_sixth_pending,
           test_pending_for_lists_only_this_principal.
         - TestNarrowing: test_identical_request_is_allowed, test_dropping_a_rule_tool_or_days_is_allowed,
           test_dropping_a_verb_is_allowed, test_adding_a_rule_is_refused, test_adding_a_verb_is_refused,
           test_adding_a_tool_is_refused, test_more_days_is_refused,
           test_turning_on_a_personal_data_flag_is_refused, test_a_new_rule_with_another_value_is_refused,
           test_a_duplicate_rule_is_refused, test_the_label_may_change.
         - TestBundleFromCalls: test_auto_tools_need_no_rule, test_unknown_tools_are_skipped,
           test_calls_on_one_resource_merge_their_verbs (monkeypatch `suggested_rules`),
           test_a_call_without_a_usable_suggestion_is_uncovered (requires_args, value_known false,
           value None), test_output_is_sorted.
         - TestReviewData: test_new_rule_sentence, test_copy_rule_sentence,
           test_missing_copy_rule_says_so, test_agent_label_comes_from_label_for.
         - TestRecord: test_record_names_the_agent_and_principal (read back via the test audit
           logger as tests/unit/test_gate.py's audit assertions do),
           test_record_is_not_an_approved_like_decision.
      7. Run ruff and `python3 -m pytest tests/unit/test_scoped_token_proposals.py --cov=privacyfence.scoped_token_proposals --cov-branch --cov-report=term-missing -q`.
    acceptance:
      - python3 -m pytest tests/unit/test_scoped_token_proposals.py -q passes
      - python3 -m pytest tests/unit/test_scoped_token_proposals.py -q --cov=privacyfence.scoped_token_proposals --cov-branch --cov-report=term-missing reports 100%
      - grep -n "scoped_token_proposals.set_registry(None)" tests/conftest.py matches
      - grep -c "scoped_token_proposal_" src/privacyfence/audit_log.py prints at least 3
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
      - tests/unit/test_daemon_main.py
    brief: |
      Read the plan's Design section D4 first; it holds the exact tool description, schema,
      error texts, steps and return value.
      1. src/privacyfence/web/mcp_tools.py: add `PROPOSE_SCOPED_TOKEN_TOOL` with D4's name,
         description (verbatim), input_schema and annotations, and append it to `META_TOOLS`.
      2. src/privacyfence/web/mcp_dispatch.py: add the `scoped_token_policy` constructor keyword,
         `scoped_token_proposals_enabled()`, the session ledger (`_session_calls`,
         `MAX_SESSION_CALLS = 200`, recorded in `call()` as D4 says, dropped in `end_session`),
         and `async def propose_scoped_token(self, session_key, params, base_url)` following D4's
         eight steps in order. If `audit_log.current_released_request_id()` cannot be read after
         `connector.call` returns (it is reset before you can read it), stop with status=blocked.
      3. src/privacyfence/web/routes_mcp.py: leave the tool out of `handle_list_tools` when
         `dispatcher.scoped_token_proposals_enabled()` is false; add the `_dispatch_meta_tool`
         branch; at :489 resolve the agent with `_resolve_agent` for this one tool when
         `scope is None`, and update the comment above it to cite ADR 0251. Do not add the tool to
         `SCOPED_META_TOOLS`.
      4. src/privacyfence/daemon_main.py: pass
         `scoped_token_policy=scoped_tokens.policy_from_org_config({}, org_mode=False)` at :898
         and `scoped_token_policy=scoped_tokens.policy_from_org_config(org_config, org_mode=True)`
         at :1233.
      5. Tests (follow the existing patterns in each file):
         - tests/unit/web/test_mcp_tools.py `TestMetaToolManifest`: add
           test_propose_scoped_token_requires_reason_and_label and
           test_propose_scoped_token_is_not_read_only.
         - tests/unit/web/test_mcp_dispatch.py: new class `TestProposeScopedToken` with
           test_bundle_proposal_returns_a_review_url_and_no_secret (assert no "pfst_" substring
           anywhere in the result and the store has no token), test_proposal_is_registered_and_audited,
           test_refused_when_proposals_disabled, test_refused_in_an_unattended_session_and_audited,
           test_refused_under_a_scoped_token, test_duplicate_rule_kind_is_refused,
           test_from_session_and_rules_together_are_refused, test_days_are_capped_at_the_policy_max,
           test_invalid_request_errors_before_registering (bad label: registry stays empty),
           test_from_session_builds_from_approved_calls_only (a denied call and an
           approval_pending call are not recorded), test_from_session_with_no_calls_is_refused,
           test_session_ledger_is_dropped_on_end_session, test_session_ledger_keeps_the_newest_200,
           test_scoped_token_calls_are_not_recorded.
         - tests/unit/web/test_routes_mcp.py: add "privacyfence_propose_scoped_token" to the
           parametrize at :781-783 (not offered and refused for a scoped token); add
           test_propose_scoped_token_is_listed_only_when_enabled and
           test_propose_scoped_token_is_attributed_to_the_client (the recorded audit entry's agent
           is the resolved client, not UNKNOWN_AGENT), and
           test_other_meta_tools_stay_unattributed.
         - tests/unit/test_daemon_main.py: assert both dispatcher constructions receive a
           `scoped_token_policy` (local: allow_ai_proposals True; org from org_config).
      6. Run ruff, mypy src/privacyfence, and the four test files plus
         tests/unit/web/test_tool_schema_portability.py and tests/unit/test_connector_catalog.py.
         Then run `python3 -m pytest tests/integration/test_mcp_daemon_contract.py tests/integration/test_shim_mcp_contract.py -v`
         (the profile's conditional DoD row for routes_mcp.py/mcp_dispatch.py).
    acceptance:
      - python3 -m pytest tests/unit/web/test_mcp_dispatch.py::TestProposeScopedToken -q passes
      - python3 -m pytest tests/unit/web/test_routes_mcp.py tests/unit/web/test_mcp_tools.py tests/unit/web/test_tool_schema_portability.py tests/unit/test_connector_catalog.py tests/unit/test_daemon_main.py -q passes
      - python3 -m pytest tests/integration/test_mcp_daemon_contract.py tests/integration/test_shim_mcp_contract.py -v passes
      - grep -n "privacyfence_propose_scoped_token" src/privacyfence/web/routes_mcp.py does not match inside the SCOPED_META_TOOLS frozenset
      - python3 -m mypy src/privacyfence passes and ruff check . passes

  - id: p4-settings-routes
    title: Review page route, create_from_proposal and decline routes, page data, shim mapping
    depends_on: [p2-proposal-module]
    complexity: M
    touches:
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/server.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/unit/web/test_server.py
    brief: |
      Read the plan's Design section D5 first; it holds the routes, gate order, status codes,
      error texts and audit summaries. Only the `build_html` keyword from D6 belongs in this
      phase; the UI itself is p5.
      1. src/privacyfence/settings_window_html.py: add the `scoped_proposal: dict | None = None`
         keyword to `build_html` (:2576) and embed it as `window.__pfScopedProposal` next to
         `__pfScopedTokens` (:2648-2652) with the same escaping. Nothing else in this file.
      2. src/privacyfence/web/routes_settings.py, local `build_routes` and org `build_org_routes`:
         - Extract each mode's create-and-deliver body from `scoped_token_create` (:1208-1240
           local, :1828-1863 org) into a helper both that route and the new route call. The
           existing create tests must pass unchanged.
         - Add `GET /settings/scoped_tokens/proposals/{proposal_id}` rendering the mode's settings
           page with `initial_section="scoped_tokens"` and `scoped_proposal` as D5 says.
         - Add `POST /api/settings/scoped_tokens/create_from_proposal` with D5's eight gates in
           order, `registry.restore` on a create/delivery failure, and the two audit entries.
         - Add `POST /api/settings/scoped_tokens/decline_proposal`.
         - Classify: create_from_proposal in `_BESPOKE_SENSITIVE_ROUTE_PATHS`, decline_proposal in
           `_BESPOKE_EXEMPT_ROUTE_PATHS` with D5's reason.
         - Bridge shim `scopedRoutes` (:583-591): add the two entries from D5; a `null` message
           skips `window.confirm` (the existing code already checks `scopedRoutes[action][1] &&`).
      3. src/privacyfence/web/server.py: `scoped_token_page_data` (:348) and the org page data get
         `proposals` and `proposals_enabled` as D5 says.
      4. Tests:
         - tests/unit/web/test_routes_settings.py: new class `TestScopedTokenProposalRoutes`
           (reuse `_scoped_wiring`, register proposals through
           `scoped_token_proposals.get_registry().add(new_proposal(...))`):
           test_review_page_embeds_the_proposal, test_review_page_for_unknown_id_says_missing,
           test_review_page_hides_another_principals_proposal,
           test_create_from_proposal_creates_and_delivers_through_the_companion,
           test_typed_label_must_match, test_widening_is_refused, test_expired_proposal_is_gone,
           test_proposal_is_single_use, test_create_failure_restores_the_proposal,
           test_companion_failure_restores_the_proposal, test_passkey_is_bound_to_the_proposal
           (an assertion for proposal A or for the un-narrowed request is refused for the narrowed
           one), test_confirmation_and_human_session_required, test_accept_is_audited_with_the_agent,
           test_decline_takes_and_audits, test_decline_unknown_is_410,
           test_shim_maps_the_two_actions. `TestBespokeRoutesAreClassified` must pass.
         - tests/unit/web/test_routes_org_settings.py: new class `TestScopedTokenProposalRoutes`
           with test_refused_when_proposals_disabled_in_the_bundle, test_secret_shown_once,
           test_org_step_up_bound_to_the_proposal, test_typed_label_must_match,
           test_review_page_missing_when_disabled.
         - tests/unit/web/test_server.py `TestScopedTokenWiring`: test_page_data_lists_pending_proposals,
           test_page_data_proposals_enabled_follows_the_policy.
      5. Run ruff, mypy src/privacyfence and the three test files.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py tests/unit/web/test_server.py -q passes
      - python3 -m pytest tests/unit/web/test_routes_settings.py::TestBespokeRoutesAreClassified -q passes
      - grep -n "create_from_proposal" src/privacyfence/web/routes_settings.py matches inside _BESPOKE_SENSITIVE_ROUTE_PATHS
      - python3 -m mypy src/privacyfence passes and ruff check . passes

  - id: p5-review-ui
    title: Review panel, waiting list and the typed-name delegation dialog in the settings page
    depends_on: [p4-settings-routes]
    complexity: M
    touches:
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_settings_window_html.py
      - tests/integration/test_settings_scoped_token_proposal_browser.py
    brief: |
      Read the plan's Design section D6 first; every string, control and request field is
      there. Copy the patterns of `renderScopedTokens`, `duplicateScopedToken` and
      `submitScopedToken` (src/privacyfence/settings_window_html.py:1400-1600, 1815-1828).
      1. In `renderScopedTokens()`: the "Waiting for review" list from `SCOPED.proposals`, and the
         "Review proposed token" panel from `window.__pfScopedProposal` replacing the New token
         form, with the missing-proposal text when `missing` is true. Keep the panel's state in
         new `ui.sp*` fields (initialised next to the `ui.st*` ones at :351-352).
      2. The Continue modal with D6's title, text, list, typed-name input, Cancel focused on open,
         Escape cancels, and `Delegate access` disabled until the input equals the label exactly.
         Delegate posts `create_proposed_scoped_token` with D6's fields; Decline posts
         `decline_scoped_token_proposal`.
      3. Every agent-supplied string goes through `textContent` or the page's existing escape
         helper. Add no `innerHTML` with unescaped proposal text.
      4. tests/unit/test_settings_window_html.py, new class `TestScopedTokenProposal`:
         test_build_html_embeds_the_proposal_escaped (a label/reason containing `</script><img onerror>`
         does not break out), test_review_strings_present, test_delegate_button_text,
         test_waiting_list_renders_when_proposals_exist.
      5. tests/integration/test_settings_scoped_token_proposal_browser.py, marked
         `pytest.mark.browser`, built exactly like
         tests/integration/test_settings_scoped_token_form_browser.py (set_content of build_html
         with `initial_section="scoped_tokens"`, a hand-written page-data dict plus
         `scoped_proposal=`, posts captured by stubbing `window.webkit.messageHandlers.pf`):
         test_review_panel_replaces_the_new_token_form,
         test_delegate_is_disabled_until_the_name_is_typed_exactly (a wrong-case or partial name
         keeps it disabled), test_cancel_is_focused_and_escape_cancels,
         test_unticking_a_rule_a_verb_and_a_tool_narrows_the_request,
         test_delegate_posts_the_proposal_id_and_typed_label, test_decline_posts_decline,
         test_missing_proposal_shows_the_notice, test_untrusted_reason_is_rendered_as_text.
      6. Run the browser test with `PRIVACYFENCE_TEST_CHROMIUM` set as the session-start hook
         exports it; a SKIPPED result is not a pass. If the hook warned that no Chromium launches,
         stop with status=blocked and say so.
      7. Run ruff and tests/unit/test_settings_window_html.py.
    acceptance:
      - python3 -m pytest tests/unit/test_settings_window_html.py -q passes
      - python3 -m pytest tests/integration/test_settings_scoped_token_proposal_browser.py -v reports every test passed and none skipped
      - python3 -m pytest tests/integration/test_settings_scoped_token_form_browser.py -v still passes
      - ruff check . passes

  - id: p6-docs-adrs-retire
    title: ADRs 0250-0252, reference docs, tools reference, changelog, delete the plan
    depends_on: [p3-mcp-tool, p5-review-ui]
    complexity: M
    touches:
      - docs/adr/0250-*.md
      - docs/adr/0251-*.md
      - docs/adr/0252-*.md
      - docs/adr/README.md
      - docs/adr/0006-attributing-a-request-to-the-ai-system-that-made-it.md
      - docs/adr/0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md
      - docs/how-it-works.md
      - docs/security-and-compliance.md
      - docs/approvals-and-policy.md
      - docs/connect-claude-code.md
      - docs/configuration-reference.md
      - docs/org-mode-setup-guide.md
      - docs/tools-reference.md
      - scripts/generate_tools_reference.py
      - CHANGELOG.md
      - docs/ai-proposed-scoped-tokens-plan.md
      - docs/ai-proposed-scoped-tokens-plan-manual-steps.html
    brief: |
      1. Check `ls docs/adr` for the next free number. If 0250-0252 are taken, use the next free
         ones throughout this phase.
      2. Write the three ADRs from the plan's ADRs section using the template in
         docs/adr/README.md ("## Template"), Status "Accepted — <today>. Implemented.". Take the
         Context from "Current state", the Decision from D3-D6, and the Alternatives from D7
         (each bullet in D7 belongs to the ADR it concerns: attribution → 0251, audit-log
         derivation → 0252, the rest → 0250). Verification names the test classes added in
         p1-p5. Link only source files, tests and ADRs, never this plan.
      3. Add three rows to the index in docs/adr/README.md. Add one Status line to ADR 0006
         ("Amended by ADR 0251: a token proposal is attributed to its client.") and to ADR 0164
         ("Related: ADR 0250 — an AI client can propose a token for a person to create."). Change
         nothing else in either body.
      4. Reference docs (describe today's behaviour, link the ADRs):
         - docs/how-it-works.md: "eight" → "nine" at :116 and :176, a row for the new tool in the
           table at :180-191, and one sentence in "### Scoped tokens" (:100).
         - scripts/generate_tools_reference.py:210 "eight" → "nine", then regenerate
           docs/tools-reference.md with that script and confirm tests/unit/test_docs_tools_reference.py passes.
         - docs/connect-claude-code.md "### Scoped tokens" (local :70, org :100): a subsection
           "Let Claude propose a token" describing the review link, narrowing, the typed-name
           confirmation, and that Claude never sees the token.
         - docs/security-and-compliance.md "## Scoped tokens" (:224-262): the proposal flow's
           guarantees (not for scoped tokens or unattended sessions, single use, one hour, typed
           name, step-up bound to the proposal, the session ledger is in memory only) and the
           org key `scoped_tokens.allow_ai_proposals` next to :258-259.
         - docs/approvals-and-policy.md:443: keep that only Settings can grant the personal-data
           box, and add that a proposal can ask for it but only the person keeps it ticked.
         - docs/configuration-reference.md (:299-300) and docs/org-mode-setup-guide.md (:358-359):
           rows for `--enable-scoped-token-proposals` / `--disable-scoped-token-proposals` and the
           key.
      5. CHANGELOG.md under "## [Unreleased]": one user-facing entry, for example "Claude can
         propose a scoped token for a routine with `privacyfence_propose_scoped_token`; you review
         and narrow it in Settings, confirm by typing its name, and copy the token yourself.
         Organization installs turn it on with `--enable-scoped-token-proposals`." Never add a
         version heading.
      6. Delete docs/ai-proposed-scoped-tokens-plan.md and
         docs/ai-proposed-scoped-tokens-plan-manual-steps.html, then
         `grep -rn "ai-proposed-scoped-tokens-plan" .` (excluding .git) must print nothing.
      7. Run `python3 -m pytest tests/unit -q` and `ruff check .`.
    acceptance:
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes
      - grep -rn "ai-proposed-scoped-tokens-plan" --exclude-dir=.git . prints nothing
      - grep -n "0250\|0251\|0252" docs/adr/README.md prints three index rows (or the renumbered ones)
      - grep -n "privacyfence_propose_scoped_token" docs/how-it-works.md docs/tools-reference.md matches in both
      - grep -n "allow_ai_proposals" docs/configuration-reference.md docs/security-and-compliance.md matches in both
      - python3 -m pytest tests/unit -q passes and ruff check . passes
```
