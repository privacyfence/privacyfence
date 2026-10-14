# Approval core hardening plan

Plan 1 of 4 made from the whole-repository review of 2026-10-14
([`docs/repo-2026-10-14-review.md` at c8416d94](https://github.com/privacyfence/privacyfence/blob/c8416d94981ac6eb776db7be8ee72de4af56f12a/docs/repo-2026-10-14-review.md),
reviewed commit `3766d50f`, which is still `main`'s tip). The other three are
`plan/web-and-org-hardening`, `plan/platform-and-ci-hardening` and
`plan/docs-and-website-accuracy`. Each finding id below is the review's; its evidence is there.

## 1. Goal

An approval a person gives releases exactly the request they saw, and nothing else. Today the
decision ledger and the coalescing key hold only the subset of arguments a connector passes to the
gate as `args`, so one approved Slack message can be redeemed for a different message (SEC-A1,
critical, reproduced end to end) and one approved read can be replayed for a wider page with no PII
confirmation (SEC-A2). This plan binds every approval to the whole request plus a digest of what the
card showed, makes the audit log's hash chain reject spliced legacy lines (SEC-A3), makes the
sender- and recipient-domain rules immune to display-name spoofing (SEC-A4), and adds the tests that
pin all of it (TST-A1 to TST-A9, TST-A12).

Findings in this plan: **SEC-A1, SEC-A2, SEC-A3, SEC-A4, TST-A1, TST-A2, TST-A3, TST-A4, TST-A5,
TST-A6, TST-A7, TST-A8 (the hang fixture; its `timeout-minutes` half is in
`plan/platform-and-ci-hardening`), TST-A9, TST-A12.**

## 2. Current state

- `gate.py:1077` `dedupe_key = canonical_key(connector, tool, args)`, where `args` is the
  connector's policy dict. `approvals.py:196-204` `canonical_key` is
  `f"{connector}:{tool}:{json.dumps(args or {}, sort_keys=True, default=str)}"`. The same key is
  the coalescing key (`register_or_coalesce`, `approvals.py:~560-600`) and the ledger key
  (`consume_ledger`, `approvals.py:774-818`), both scoped by `(principal_id, dedupe_key)`.
- Connectors leave the written content out of `args`: `slack.py:803`
  `args={"channel_id", "thread_ts", "is_self_dm"}` (no `text`), `telegram.py:337`, `gmail.py:1332`
  (no body/cc/bcc), `drive.py:1854`, `calendar.py:1014/1056` (`args={}`). An AST scan found 41 of 93
  popup sites with effect-bearing parameters missing. Reads leave out `limit`/`cursor`
  (`slack.py:538,591`).
- `_resolve_decision` (`gate.py:436-546`) checks `registry.consume_ledger(dedupe_key)` first and
  returns the stored decision without re-checking PII.
- `web/mcp_dispatch.py:203-305` `McpDispatcher.call` pops `reason` from the request args and runs
  `connector.call(tool, args)` inside `unattended_scope`, `reason_scope` and
  `released_request_scope`. It is the only caller of `connector.call` for an MCP tool call
  (`routes_mcp.py:547-553`). `gate.py:826-858` defines `reason_scope`/`current_reason`, the pattern
  this plan copies.
- `canonical_key` is also used directly by tests: `tests/unit/test_approvals.py:20-46`
  (`TestCanonicalKey`) and `tests/unit/connectors/test_grist_connector.py:1063,1471`. It stays.
- `audit_log.py:646-711` `verify_chain`: a line with an empty `entry_hash` sets
  `expected_prev = None` and is counted as checked, anywhere in the file. The script
  `scripts/verify_audit_log.py:75-109` never compares the last hash with
  `.audit_chain_state.json` (written by `_persist_chain_state`, `audit_log.py:628-632`) and never
  checks continuity between week files. `_hash_canonical` (`:614-623`) hashes every field but
  `entry_hash`; only `summary` and `agent` edits are tested (`tests/unit/test_audit_log.py:642`
  `TestHashChain`).
- `policy/scopes.py:223-232` `_trusted_sender_domain_matches` takes the text between the first `<`
  and the first `>`; `:260-263` `_to_is_myself_matches` is a substring test;
  `:266-270` `_approved_recipient_domain_matches` uses `auto_accept._domain_of`
  (`auto_accept.py:398-402`), the same first-`<` parse. `auto_accept._address_of` (`:404-420`) is
  `parseaddr(raw or "")[1].strip().lower()`. `tests/unit/policy/_v1_reference.py:~161`
  `_rule_trusted_sender_domain` repeats the first-`<` parse. `tests/unit/policy/test_scopes.py:267-306`
  holds `_SPOOF_PAYLOADS`, `_SPOOF_IDENTITY_SELECTORS` and `TestIdentitySpoofResistance`.
- `settings_controller.py:1830-1839` `_save_and_reload_policy_v2` calls `_save_and_reload` (which
  fires `notify_rules_changed()`) before `set_policy_v2_store_rules(...)`, so listeners re-evaluate
  pending cards against the old rules (TST-A4).
- `tests/unit/test_approvals.py:803-816` registers the cross-principal ledger test with
  `gate_kind="popup"`, so Alice's single-use consume hides whether Bob is checked (TST-A1).
  `TestLedgerTTLExpiry` (`:448`) always sweeps before `consume_ledger`, hiding the TTL check at
  `approvals.py:799-800` (TST-A9). `tests/unit/test_gate.py` never calls `notify_rules_changed` or
  `_on_rules_changed` (TST-A3). A failing deferred-approval test leaves a `pf-popup` worker
  (`gate.py:336-339`) blocked and the process hangs at exit (TST-A8). `tests/unit/conftest.py`
  does not exist.

## 3. Design

### 3.1 The approval binding key (SEC-A1, SEC-A2)

An approval is keyed by three things: the full request the agent sent, the connector's policy args,
and a digest of everything the card shows. A difference in any of them misses the ledger and does
not coalesce; it registers a fresh card. No other path changes: rule matching
(`ReviewContext.args`) still uses the connector's policy args, and the dispatcher's own retry
dedupe (`mcp_dispatch._dedupe_key`) is untouched by this plan.

**`gate.py`**, next to `reason_scope` (same shape, same docstring style):

```python
_request_args_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "privacyfence_request_args", default=None
)

def current_request_args() -> str | None:
    """The canonical JSON of the agent's whole tool-call arguments, minus "reason", for the
    call in progress; None outside McpDispatcher.call (a direct gated_call in a test or a
    script)."""
    return _request_args_ctx.get()

class request_args_scope:  # noqa: N801 (context-manager-style name, like reason_scope)
    def __init__(self, args: dict[str, Any]) -> None:
        self._json = json.dumps(args, sort_keys=True, default=str)
        self._token: contextvars.Token | None = None
    def __enter__(self) -> "request_args_scope": ...   # sets _request_args_ctx to self._json
    def __exit__(self, *exc_info: object) -> None: ...  # resets it
```

The value is a frozen JSON string, taken before the connector runs, so a connector that mutates
its `args` dict cannot change what the approval is bound to.

**`approvals.py`**, below `canonical_key` (which stays unchanged):

```python
def card_digest(
    *, gate: str, summary: str, sender: str, details_text: str, scan_text: str,
    preview: dict | None, new_info: dict | None, visibility: dict | None,
    preview_tables: list | None, preview_blocks: list | None, table_only: bool,
    pdf_bytes: bytes, preview_bytes: bytes, preview_mime_type: str, delivery: str,
) -> str:
    """SHA-256 over everything the approval card shows and everything the PII scan read.
    Bytes are folded in by their own SHA-256, and only for a write (gate "popup"): a read's
    thumbnail is fetched best-effort and can differ between two identical calls, while a
    read's content is already bound through details_text and scan_text."""
    def _h(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest() if data else ""
    material = {
        "gate": gate, "summary": summary, "sender": sender, "details_text": details_text,
        "scan_text": hashlib.sha256(scan_text.encode("utf-8")).hexdigest(),
        "preview": preview or {}, "new_info": new_info or {}, "visibility": visibility or {},
        "preview_tables": preview_tables or [], "preview_blocks": preview_blocks or [],
        "table_only": table_only, "preview_mime_type": preview_mime_type, "delivery": delivery,
        "pdf_bytes": _h(pdf_bytes) if gate == "popup" else "",
        "preview_bytes": _h(preview_bytes) if gate == "popup" else "",
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def binding_key(
    connector: str, tool: str, *, request_args_json: str | None, policy_args: dict[str, Any] | None,
    card: str,
) -> str:
    """The decision-ledger / coalescing key (ADR 0175): the agent's whole request, the
    connector's policy args and the card digest, hashed. Re-issuing the identical call over
    unchanged content reproduces it; any other request, or changed content, does not."""
    material = json.dumps(
        {"request": request_args_json, "policy": canonical_key(connector, tool, policy_args), "card": card},
        sort_keys=True,
    )
    return f"{connector}:{tool}:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"
```

**`gated_call`** (`gate.py:1072-1077`): replace the `canonical_key` line and its comment with

```python
dedupe_key = binding_key(
    connector, tool, request_args_json=current_request_args(), policy_args=args,
    card=card_digest(
        gate=gate, summary=summary, sender=sender, details_text=details_text,
        scan_text="\x00".join(t or "" for t in (pii_scan_text, upload_pii_scan_text, write_content_scan_text)),
        preview=preview, new_info=new_info, visibility=visibility,
        preview_tables=preview_tables, preview_blocks=preview_blocks, table_only=table_only,
        pdf_bytes=pdf_bytes, preview_bytes=preview_bytes, preview_mime_type=preview_mime_type,
        delivery=delivery,
    ),
)
```

with a comment that names ADR 0175 and says why: the policy args leave out what is written, so the
key must also hold the whole request and what the card showed. It hashes the `details_text`
argument, never the derived `details` (`gate.py:980`), whose fallback `_default_details(raw_data)`
is a `json.dumps(..., default=str)` of an arbitrary object and can differ between two identical
calls. `card_digest` is pure CPU over strings already in memory; it needs no `asyncio.to_thread`.

**`McpDispatcher.call`** (`mcp_dispatch.py:250-253`): add `request_args_scope(args)` to the `with`
that already holds `unattended_scope`, `reason_scope` and `released_request_scope`. `args` there
already has `reason` popped, which keeps the key retry-stable.

Why this satisfies SEC-A2 with no separate PII recheck on a ledger hit: a read's `details_text`
and PII scan text are the content released, and both are in the digest, so changed content gives
a different card digest, a ledger miss and a fresh card that runs the PII gate. The user chose
this strict form ("any change gets a new card") over re-checking PII only on a replay. A wider request (`limit`, `cursor`)
changes the request JSON the same way.

Rejected:

- **Adding the missing parameters to each of the 41 connector `args` dicts** (the review's
  alternative, as ADR 0148/0155 did for Grist and Drive downloads): it needs every future tool to
  get it right, and `args` doubles as the rule-matching context, where large content does not
  belong. The structural key covers every tool at once.
- **Keying on `raw_data`/`filtered_data`:** they are arbitrary objects whose `str()` can carry
  memory addresses, which would make the key unstable between identical calls and break the
  deferred protocol (a re-issue would never find its decision).
- **Recomputing `pii_forces_confirmation` on a ledger hit** (the review's minimum): unnecessary
  once changed content cannot hit the ledger at all.

Consequences to record in the ADR:

- A re-issued read whose content changed while the card was pending (a new message in a busy
  channel, live Peek visit counts, an inbox search) gets a new card instead of collecting the old
  decision. For live content an agent can meet a new card on every re-issue after a deferred
  approval; deciding within the hold window avoids it. The user chose this over re-checking PII
  only.
- An agent that spells out an optional argument it left out before (`"limit": 50`) changes the
  request and gets a new card.
- A Drive upload from a local path whose card shows only name and size is bound to the path, not
  to the file's bytes; swapping the file between approval and re-issue is not detected.

Also update the docstrings that describe the key as `(connector, tool, args)`:
`approvals.register_or_coalesce` (`:548-551`) and `consume_ledger` (`:775-778`).

### 3.2 Systemic invariant for the binding (TST-A2)

`tests/unit/test_systemic_gate_invariants.py` gains `class TestApprovalBindingCoversTheWholeRequest`
(docstring: the decision ledger must never release one approval for a request that differs in any
argument; ADR 0175):

1. `test_every_param_of_every_gated_tool_changes_the_key`, parametrized over every tool whose
   `auto_accept.TOOL_TO_GATE` value is `"review"` or `"popup"`, using `_all_tool_specs()` and
   `tests/helpers.py::build_stub_args`. For each `ToolParam` other than `reason`, vary the stub
   value (`str` → value + `"-x"`, `bool` → `not`, `int`/`float` → `+ 1`, `list` → `[*value, "x"]`,
   `dict` → `{**value, "x": 1}`, `None` → `"x"`) and assert
   `binding_key(..., request_args_json=json.dumps(a, sort_keys=True, default=str), policy_args={}, card="")`
   differs between the stub and the varied args. This one guards against a per-tool exclusion
   being added later; tests 2 and 3 are the ones that fail on today's code.
2. `test_the_dispatcher_binds_the_request`: read `src/privacyfence/web/mcp_dispatch.py` with `ast`,
   find `McpDispatcher.call`, and assert the `With` node that contains the `connector.call(...)`
   await also has a `request_args_scope(...)` item.
3. `test_the_gate_keys_on_the_whole_request`: read `src/privacyfence/gate.py` with `ast`, find
   `gated_call`, and assert the assignment to `dedupe_key` is a call to `binding_key` whose keywords
   include `request_args_json` with a `current_request_args()` call as its value and `card` with a
   `card_digest(...)` call.

The review's form of this invariant (it fails today on 41 cases) assumed connector `args` stay the
key; with 3.1 they no longer are, so the invariant is restated over the new key.

### 3.3 Audit chain (SEC-A3, TST-A5, TST-A6)

- `verify_chain` (`audit_log.py:646-711`): track `seen_chained = False`. A line with an empty
  `entry_hash` is accepted only while `seen_chained` is False (legacy lines that predate chaining
  sit at the top of a file). After the first chained line, a chain-less line returns
  `ok=False, first_break_line=<line>, detail="an entry with no chain fields follows a chained entry -- a line may have been forged or replaced"`.
  Set `seen_chained = True` on the first line that has an `entry_hash`. When one or more
  chain-less lines precede the first chained line, that chained line's `prev_hash` must equal
  `GENESIS_HASH` (`audit_log.py:83`; a chain starts at genesis when no state file exists, which is
  exactly when legacy lines precede it); otherwise return `ok=False` at that line with
  `detail="the first chained entry after unchained lines does not start a new chain -- earlier entries may have been replaced"`.
- `scripts/verify_audit_log.py`, after the per-week loop, two more checks, each printing one line
  and making the exit status 1 on failure:
  - **Continuity:** for each pair of consecutive week files present (sorted by name), the first
    chained line of the later file must have `prev_hash` equal to the last chained line's
    `entry_hash` in the earlier file. Failure line:
    `{later_week}: FAILED -- its first entry does not continue {earlier_week}'s last entry (a week may have been removed or replaced)`.
    A later week whose first chained `prev_hash` is `GENESIS_HASH` is a legitimate restart (the
    chain-state file was missing, `_load_chain_state`, `audit_log.py:1044-1065`): print
    `{later_week}: note -- starts a new chain segment` and do not fail.
  - **Tail:** if `.audit_chain_state.json` exists in the directory, its `last_hash` must equal the
    last chained line's `entry_hash` of the newest week file. Failure line:
    `{newest_week}: FAILED -- the last entry does not match .audit_chain_state.json (entries may have been removed from the end)`.
  - The tail check cannot catch someone who also edits `.audit_chain_state.json` (it is not
    keyed); the docs say so.
  - Both read the JSONL directly with a small helper `_chain_ends(path) -> tuple[str, str] | None`
    returning `(first prev_hash, last entry_hash)` of the chained lines, or None when the file has
    no chained line (then the pair is skipped). With `--week` given, both checks are skipped (a
    single week cannot be checked for continuity).
- Docs: `docs/security-and-compliance.md:316-332` gains one sentence after the exit-status line,
  written in the retirement phase (see p6).

### 3.4 Address parsing (SEC-A4, TST-A7)

- `auto_accept._domain_of(sender)` becomes: `addr = _address_of(sender)`; return
  `addr.rsplit("@", 1)[1]` if `addr.count("@") == 1` else `""` (a Python 3.11 without the
  CVE-2023-27043 fix can return `x@evil.com@trusted.com` from `parseaddr`). An address with no `@` has no domain and
  matches no domain rule (fails closed).
- `scopes._trusted_sender_domain_matches`: replace the hand parse with `domain = _domain_of(raw_sender)`;
  return False when `domain` is empty.
- `scopes._to_is_myself_matches`: `all(_address_of(r) == ctx.my_email.lower() for r in recipients)`.
- `scopes._approved_recipient_domain_matches`: unchanged in shape; it already calls `_domain_of`,
  so it gets the fix. Add `and all(_domain_of(r) for r in recipients)` so an address with no domain
  never matches.
- `tests/unit/policy/_v1_reference.py` is a frozen oracle that must not import from
  `auto_accept` or be "improved" (its own docstring, `:1-14`). It keeps the old parse; none of the
  agreement `FIXTURES` holds a spoof payload, so the agreement test still passes. The new spoof
  test class's docstring records the divergence.

### 3.5 Rules-changed reload order (TST-A4)

`settings_controller._save_and_reload_policy_v2` (`:1830-1839`) becomes: `self._save_config(cfg)`;
then `try: set_policy_v2_store_rules(policy_store.compile_rules_from_config(cfg))`, whose `except`
keeps the existing warning and also calls `set_policy_v2_store_rules([])` (fail closed: a failed
compile must not leave the old, possibly wider, rules live); then `try: notify_rules_changed()`
with the existing `"Rule hot-reload failed: %s"` warning. It no longer calls `_save_and_reload`,
which then has no caller and is deleted. Update its docstring to say the cache is reloaded
before listeners fire, so a pending card is re-checked against the new rules.

### 3.6 Hang guard (TST-A8, fixture half)

New `tests/unit/conftest.py` with module docstring
`"""Unit-test fixtures shared across tests/unit (the repository-wide ones are in tests/conftest.py)."""`
and one autouse fixture:

```python
@pytest.fixture(autouse=True)
def _deny_left_over_cards():
    """A test that fails while a gated call waits on a card leaves a pf-popup worker blocked on
    it, and the process then never exits. Deny whatever is still pending, so a failure stays a
    failure instead of a hung job."""
    yield
    ui = approval_ui._INSTANCE
    registry = getattr(ui, "deferred_registry", None) if ui is not None else None
    if registry is None:
        return
    for card in registry.list_pending():
        ui.resolve(card.id, "deny")
```

It runs its teardown before `tests/conftest.py`'s `_reset_singletons` clears `_INSTANCE`
(fixtures tear down in reverse order of set-up, and the root autouse fixture is set up first).

## 4. ADRs

- **0175** — An approval is bound to the agent's whole request and to a digest of what its card
  showed; any difference registers a new card. Amends 0073. (p6)
- **0176** — A chain-less audit line is accepted only before a file's first chained line, and the
  verifier checks week-to-week continuity and the chain-state tail. Amends 0071. (p6)

Plans 2 and 3 take 0177-0180 and 0181-0185. If another branch takes a number first, renumber per
`docs/adr/README.md` rule 6.

## 5. Manual steps

None before. One after, in the [step-by-step page](https://claude.ai/artifact/GhJ76hcmoTwbCRPMXH6aa6):
`ma1-real-client-deferred-round-trip`, a check with a real AI client against a source run of the
feature branch that the deferred protocol still collects decisions and now refuses a substituted
write. CI drives the gate with fake connectors only.

## 6. Risks and open questions

- **An unstable card.** If some tool's card carries a value that changes between two identical calls
  (a timestamp, a fresh id), its re-issue never finds its decision and the agent loops on new
  cards. p1's tests cover Slack, a Gmail draft and a Grist upload; `ma1` covers real clients.
- **Live reads (the user's choice).** A deferred read of live content (a busy Slack channel, Peek
  stats) can meet a new card on each re-issue. `ma1` checks how it behaves in practice. A worker that
  sees a gate test where an identical re-issue gets a second card must stop with
  `status=blocked` and name the card field that changed, rather than dropping that field from the
  digest.
- **Coordination with plan 2.** `plan/web-and-org-hardening` changes
  `mcp_dispatch._dedupe_key` (SEC-A6) in the same file p1 edits. The edits are in different
  functions; whichever PR merges second takes a trivial merge.
- **Grist/Drive download args** (ADR 0148/0155) already carry a content hash; they keep it. The new
  key adds to them, it does not replace them.

## Implementation manifest

```yaml
plan_slug: approval-core-hardening
feature_branch: fix/approval-core-hardening
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/GhJ76hcmoTwbCRPMXH6aa6
manual_steps_source: docs/approval-core-hardening-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-real-client-deferred-round-trip
    title: With a real AI client, check that a deferred approval is still collected and that a substituted write gets a new card
    why: CI drives the gate with fake connectors; only a real client and a real connector show that identical re-issues still reproduce the binding key (no endless cards) and that changed content is refused
verify_after_merge:
  - python3 -m pytest tests/unit/test_gate.py tests/unit/test_approvals.py tests/unit/web/test_mcp_dispatch.py tests/unit/test_systemic_gate_invariants.py -q
  - python3 -m pytest tests/unit/test_audit_log.py tests/unit/test_verify_audit_log.py tests/unit/policy tests/unit/test_auto_accept.py tests/unit/test_settings_controller.py tests/unit/test_audit_forwarding.py -q
  - python3 -m pytest tests/integration/test_deferred_approval_round_trip.py tests/integration/test_pending_approval_releases_nothing.py tests/integration/test_mcp_daemon_contract.py tests/system/test_local_mode_system.py -q
final_checks:
  - docs/approval-core-hardening-plan.md and docs/approval-core-hardening-plan-manual-steps.html are deleted and nothing links to them
  - ADRs 0175 and 0176 exist, are Accepted, and are listed in docs/adr/README.md; ADR 0073 and ADR 0071 each carry a one-line Status pointer to the ADR that amends them
  - CHANGELOG.md has this plan's entries under "## [Unreleased]" and no new version heading
  - pytest tests/integration -v passed on the feature branch head (dod_conditional row for web/mcp_dispatch.py), and test_mcp_daemon_contract.py is green
phases:
  - id: p1-approval-binding
    title: Bind every approval to the whole request and a digest of its card
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/gate.py
      - src/privacyfence/approvals.py
      - src/privacyfence/web/mcp_dispatch.py
      - tests/unit/test_gate.py
      - tests/unit/test_approvals.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/test_systemic_gate_invariants.py
    brief: |
      Read docs/approval-core-hardening-plan.md sections 2, 3.1 and 3.2 first; they are the spec.
      Findings: SEC-A1, SEC-A2, TST-A2 (review linked in the plan's header).
      1. src/privacyfence/approvals.py: add `import hashlib`; add `card_digest(...)` and
         `binding_key(...)` exactly as in 3.1, directly below `canonical_key`. Leave
         `canonical_key` unchanged. In the module docstring, the two places that say the ledger is
         "keyed by ``(connector, tool, canonical(args))``" now say it is keyed by the whole request,
         the policy args and the card digest (``binding_key``, ADR 0175); the same in the
         `register_or_coalesce` (:548-551) and `consume_ledger` (:775-778) docstrings.
      2. src/privacyfence/gate.py:
         - Add `_request_args_ctx`, `current_request_args()` and `request_args_scope` exactly as
           in 3.1, directly after `reason_scope`. `json` is already importable; add
           `import json` if it is not imported.
         - Import `binding_key` and `card_digest` from `.approvals` (line 158); keep
           `canonical_key` only if something else in gate.py still uses it, otherwise drop it from
           the import.
         - Replace `dedupe_key = canonical_key(connector, tool, args)` (:1077) and the comment
           above it (:1072-1076) with the `binding_key(...)` call from 3.1. The comment names
           ADR 0175 and says why in one or two lines.
         - In the module docstring (:20-22), "keyed by ``(connector, tool, canonical(args))``"
           becomes "keyed by the whole request and what its card showed (approvals.binding_key,
           ADR 0175)"; "replayed with identical arguments" stays true.
      3. src/privacyfence/web/mcp_dispatch.py: import `request_args_scope` from `..gate` next to
         `reason_scope`, and add `request_args_scope(args)` to the `with (...)` at :250-252, after
         `reason_scope(reason)`.
      4. tests/unit/test_approvals.py: add `class TestBindingKey`: identical inputs give the same
         key; a different `request_args_json`, a different policy arg, or a different `card` each
         give a different key; `request_args_json=None` is allowed. Add `class TestCardDigest`:
         identical inputs give the same digest; changing `details_text`, `scan_text`, `sender`,
         one `preview` value, one `visibility` value or one `preview_tables` cell each change it;
         `preview_bytes` changes it for `gate="popup"` and does not for `gate="review"`; key order
         inside `preview` does not.
      5. tests/unit/web/test_mcp_dispatch.py, in `TestCall`: add
         `test_the_connector_sees_the_whole_request_minus_reason` — a small connector whose `call`
         records `gate.current_request_args()`; after `dispatcher.call("s1", ..., {"x": 1, "reason": "r"})`
         it recorded `'{"x": 1}'`; outside the call `current_request_args()` is None.
      6. tests/unit/test_gate.py: add `class TestApprovalBinding` (docstring: one approval never
         releases a request the human did not see; ADR 0175). Build the registry the way
         `TestDeferredApprovalProtocol` does (`PendingApprovalRegistry(hold_window=0.05, ...)`,
         `approval_ui.init_approval_ui(WebApprovalUI(registry=registry))`, the `FakeEvaluator` and
         `proposals_for` monkeypatches, the `audit_dir` fixture), copy
         `TestDeferredApprovalProtocol._decide_first_pending` (:3006) into the new class, and
         drive a real connector through `McpDispatcher(lambda: {"slack": connector})` (as
         tests/unit/web/test_mcp_dispatch.py:119-136 builds dispatchers) so request args flow:
         - Build the Slack connector as `tests/unit/connectors/test_slack_connector.py::make_connector`
           does (copy those lines into a local helper; do not import across test modules).
           `client.send_message.return_value = {"ok": True, "ts": "1", "channel_id": "C1"}`.
         - `test_approved_write_is_not_redeemable_with_different_content`: call
           `slack_send_message` with `{"channel_id": "C1", "text": "a", "reason": "r"}`; it returns
           `approval_pending`; accept that card (use `self`-style helper like
           `_decide_first_pending`); re-issue with `"text": "b"` → the result is `approval_pending`
           again with a new `approval_id`; `client.send_message` was never called with `"b"`.
         - `test_identical_reissue_collects_the_decision`: same first call, accept, re-issue the
           identical args → `client.send_message` called once with `"a"`; audit rows are
           `approval_pending` then `approved` with one shared `request_id`.
         - `test_a_changed_request_arg_not_on_the_card_still_needs_its_own_card`: a direct
           popup `gated_call` under `gate.request_args_scope({"id": "1", "extra": "a"})`, accepted;
           the identical card under `request_args_scope({"id": "1", "extra": "b"})` → pending.
         - `test_identical_gmail_draft_reissue_collects_the_decision` and
           `test_identical_grist_import_reissue_collects_the_decision`: build the Gmail and Grist
           connectors the way tests/unit/connectors/test_gmail_connector.py and
           test_grist_connector.py do (copy the setup; for Grist the CSV pattern at :1063), make
           a `gmail_create_draft` (with a signature, if the file's helpers set one) and a
           `grist_import_csv` call through the dispatcher, accept, re-issue identically → the
           write runs once and no second card appears.
         - `test_a_differing_write_does_not_coalesce_onto_a_pending_card`: with `hold_window=5.0`,
           start the `"a"` call as a task, wait for its card, issue `"b"` with hold window 0 →
           `registry.list_pending()` has two cards.
         - `test_read_replay_requires_identical_released_content`: a review-gated read via
           `gated_call` directly under `gate.request_args_scope({"channel_id": "C1", "limit": 1})`,
           `details_text="one"`; accept; re-issue under the same scope with `details_text="one"` →
           released, no new card; re-issue with `{"limit": 500}` → `approval_pending`; re-issue with
           the original scope but `details_text="one\ntwo IBAN HU42117730161111101800000000"` →
           `approval_pending` and the new card has `pii_forces_confirmation` true.
         - `test_coalescing_with_real_args`: the existing `TestCoalescing` note says
           `base_kwargs()` carries no args; add a twin that passes `args={"id": "1"}` under one
           `request_args_scope` and proves two identical concurrent calls still share one card.
      7. tests/unit/test_systemic_gate_invariants.py: add `TestApprovalBindingCoversTheWholeRequest`
         exactly as 3.2 describes (three tests).
      8. Run: `python3 -m pytest tests/unit/test_gate.py tests/unit/test_approvals.py
         tests/unit/web/test_mcp_dispatch.py tests/unit/test_systemic_gate_invariants.py
         tests/unit/connectors -q`, then `python3 -m pytest tests/integration/test_deferred_approval_round_trip.py
         tests/integration/test_pending_approval_releases_nothing.py tests/integration/test_mcp_daemon_contract.py
         tests/system/test_local_mode_system.py -q`, then `ruff check .` and `python3 -m mypy src/privacyfence`.
      Stop conditions: if any existing test that re-issues an identical call (TestDeferredApprovalProtocol,
      the integration round trip, the system test) now gets a second card, the card is unstable for
      that tool: stop with status=blocked and name the field that differs between the two calls.
      Do not drop fields from card_digest and do not loosen an assertion to get green.
      No CHANGELOG or ADR edits in this phase (p6 writes them).
    acceptance:
      - "SEC-A1/SEC-A2: python3 -m pytest tests/unit/test_gate.py::TestApprovalBinding -q passes with at least 8 tests"
      - "TST-A2: python3 -m pytest tests/unit/test_systemic_gate_invariants.py -k ApprovalBindingCoversTheWholeRequest -q passes"
      - python3 -m pytest tests/unit/test_approvals.py -k "BindingKey or CardDigest" -q passes
      - python3 -m pytest tests/unit/web/test_mcp_dispatch.py -k whole_request_minus_reason -q passes
      - grep -n "dedupe_key = canonical_key" src/privacyfence/gate.py returns nothing
      - python3 -m pytest tests/integration/test_deferred_approval_round_trip.py tests/integration/test_mcp_daemon_contract.py tests/system/test_local_mode_system.py -q passes
      - ruff check . and python3 -m mypy src/privacyfence pass

  - id: p2-ledger-and-rules-tests
    title: Make the ledger, TTL and rules-changed tests able to fail, and stop failed gate tests hanging
    depends_on: [p1-approval-binding]
    complexity: S
    touches:
      - tests/unit/test_gate.py
      - tests/unit/test_approvals.py
      - tests/unit/conftest.py
    brief: |
      Read docs/approval-core-hardening-plan.md sections 2 and 3.6. Findings: TST-A1, TST-A3,
      TST-A9, TST-A8 (fixture half). Tests only; no source change.
      1. tests/unit/test_approvals.py::TestPrincipalDimension::test_ledger_entry_is_not_shared_across_principals
         (:803-816): register with `gate_kind="review"` (a reusable entry); finalize "accept" as
         Alice; then, BEFORE Alice consumes, assert `consume_ledger("same-key") is None` under
         `principal_scope(Principal(id="bob"))`; then assert Alice still gets the LedgerHit. Update
         the docstring/comment to say why the review gate is used (a single-use write hides the
         principal check).
      2. tests/unit/test_gate.py: add `class TestPrincipalScopedLedger` with
         `test_another_principals_identical_read_gets_its_own_card`: registry as in
         TestDeferredApprovalProtocol; under `principal_scope(Principal(id="alice"))` a review
         `gated_call_or_pending(**base_kwargs())` returns pending; accept it; re-issue as Alice →
         FILTERED; the same call under `principal_scope(Principal(id="bob"))` → `approval_pending`.
      3. tests/unit/test_approvals.py::TestLedgerTTLExpiry: add
         `test_consume_ledger_refuses_an_entry_past_its_ledger_ttl_without_a_sweep`: register a
         review approval, finalize "accept", set `approval.ledger_expires_at = time.time() - 1`,
         and assert `registry.consume_ledger("k1") is None` with no `pop_*` call first. Docstring:
         consume_ledger refuses an expired entry with no external sweep.
      4. tests/unit/test_gate.py: add `class TestRulesChangedReevaluation`:
         - `test_rules_change_resolves_only_cards_the_new_rules_cover`: a pending review card
           (hold_window=0.05); swap `gate._evaluate_auto_accept` for a FakeEvaluator that does not
           match, call `auto_accept.notify_rules_changed()` → still pending; swap in one that
           matches → the card is finalized `auto_accepted`; the identical re-issue returns FILTERED
           and is audited `auto_accepted`.
         - `test_rules_change_resolves_a_pending_popup_card`: the same for `gate="popup"`
           (covers gate.py's popup auto-accept branch after a rule change).
         - `test_a_rule_change_by_one_principal_leaves_another_principals_card_pending`: register
           Alice's and Bob's cards each under its own `principal_scope`, use a FakeEvaluator that
           matches only when `current_principal().id == "alice"`, call `notify_rules_changed()`
           under `principal_scope(Principal(id="alice"))`; Alice's card resolves, Bob's stays
           pending.
         Use the `_should_auto_accept` path (`gate._should_auto_accept` reads
         `_evaluate_auto_accept`), not a hand-made predicate into `reevaluate_all`.
      5. Create tests/unit/conftest.py with exactly the fixture in 3.6.
      6. Prove the new tests can fail: in a scratch copy only (never commit it), change
         approvals.py's `consume_ledger` to look the key up under any principal, and gate.py's
         `_on_rules_changed` to pass `lambda op, ctx: (True, "any")`; the step 1/2 and step 4
         tests must fail; delete both TTL checks (`approvals.py:799-800` and the ledger `elif` at
         `:1022-1023`); the step 3 test must fail; then restore. Also, with the identical-write
         guard in `register_or_coalesce` removed in a scratch copy, `timeout 60 python3 -m pytest
         tests/unit/test_gate.py -q` must exit non-zero before the timeout (rc != 124).
         Report each result in your final message.
      7. Run python3 -m pytest tests/unit/test_gate.py tests/unit/test_approvals.py -q and ruff check .
      Stop condition: if step 6's hang check still hits the timeout, report it with the stuck
      thread (py-spy is not available; use `faulthandler.dump_traceback_later` in the scratch run)
      and stop with status=blocked.
    acceptance:
      - python3 -m pytest tests/unit/test_gate.py -k "RulesChangedReevaluation or PrincipalScopedLedger" -q passes with at least 4 tests
      - python3 -m pytest tests/unit/test_approvals.py -k "not_shared_across_principals or without_a_sweep" -q passes
      - test -f tests/unit/conftest.py && grep -n "_deny_left_over_cards" tests/unit/conftest.py matches
      - "TST-A1/TST-A3/TST-A9/TST-A8: the final report lists the four mutation checks of step 6, each failing as expected, and the hang check exiting in under 60 s"

  - id: p3-rules-reload-order
    title: Reload the rule cache before notifying listeners, and test the fail-closed config paths
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/settings_controller.py
      - tests/unit/test_settings_controller.py
      - tests/unit/test_audit_forwarding.py
    brief: |
      Read docs/approval-core-hardening-plan.md sections 2 and 3.5. Findings: TST-A4, TST-A12.
      1. tests/unit/test_settings_controller.py::TestAddPolicyRule: add
         `test_rules_changed_listeners_see_the_new_rule`, parametrized over the controller methods
         that write v2 rules (`add_policy_rule`, `remove_policy_rule`, `set_policy_rule_verbs`, and
         `update_policy_rule`; settings_controller.py:1737, 1765, 1781, 1800). Register a listener with `auto_accept.add_rules_changed_listener`          that records
         `list(get_policy_v2_store_rules())`; call the method; assert the recorded list equals
         `list(get_policy_v2_store_rules())` after the call (`PolicyRule` compares by value, so a
         verbs change is caught too). Run it: it must fail today for every parameter.
      2. src/privacyfence/settings_controller.py: rewrite `_save_and_reload_policy_v2` as in 3.5
         (reload first, fail closed, then notify) and delete `_save_and_reload`. The test from
         step 1 passes.
      3. Add `test_a_pending_card_is_auto_accepted_when_settings_adds_a_covering_rule` to the
         same file: a registry-backed WebApprovalUI (as tests/unit/test_gate.py does), a pending
         review `gated_call` with `connector="drive", tool="drive_get_file_content",
         raw_data=SimpleNamespace(parent_ids=["FOLDER1"])` (the shape the `approved_folder`
         fixture in tests/unit/policy/test_scopes.py:113 uses) and `hold_window=0.05`, then
         `controller.add_policy_rule("drive.folder", "FOLDER1", ["read"])` (as
         TestAddPolicyRule:1903 calls it) → the card is finalized `auto_accepted`. Do not stub
         `_evaluate_auto_accept` here: the point is the real rule cache.
      4. TST-A12, same file: `test_set_category_policy_rejects_an_unknown_policy`
         (`set_category_policy("gmail", "body", "allow_everything")` leaves the saved config
         unchanged); `test_a_failed_policy_reload_after_a_remove_is_logged_and_leaves_no_wider_rule`:
         monkeypatch `policy_store.compile_rules_from_config` to raise after the remove's save,
         call `remove_policy_rule`, assert the warning "Policy v2 store hot-reload failed" is
         logged (caplog) and that `get_policy_v2_store_rules() == []` afterwards (fail closed, 3.5);
         and `test_a_failed_privacy_filter_reload_is_logged` (`init_privacy_filter` raising →
         warning "Privacy filter hot-reload failed").
      5. tests/unit/test_audit_forwarding.py: `test_build_sender_rejects_an_unknown_kind`
         (`kind="carrier-pigeon"` raises `ValueError` matching "Unknown audit_forwarding kind").
      6. Run python3 -m pytest tests/unit/test_settings_controller.py tests/unit/test_audit_forwarding.py
         tests/unit/test_gate.py -q, then ruff check . and python3 -m mypy src/privacyfence.
      Stop condition: if the reorder makes any existing test in test_settings_controller.py fail
      because it expected the old order, stop with status=blocked and quote the assertion.
    acceptance:
      - "TST-A4: python3 -m pytest tests/unit/test_settings_controller.py -k \"listeners_see_the_new_rule or covering_rule\" -q passes"
      - "TST-A12: python3 -m pytest tests/unit/test_settings_controller.py tests/unit/test_audit_forwarding.py -k \"unknown_policy or failed_policy_reload or failed_privacy_filter_reload or unknown_kind\" -q passes"

  - id: p4-audit-chain
    title: Reject chain-less lines after a chained entry, and check continuity and the tail
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/audit_log.py
      - scripts/verify_audit_log.py
      - tests/unit/test_audit_log.py
      - tests/unit/test_verify_audit_log.py
      - tests/system/test_local_mode_system.py
    brief: |
      Read docs/approval-core-hardening-plan.md sections 2 and 3.3. Findings: SEC-A3, TST-A5, TST-A6.
      1. tests/unit/test_audit_log.py::TestHashChain: add
         `test_verify_chain_rejects_chainless_line_after_chained_entries` (3 recorded entries;
         rewrite line 2 with `entry_hash`/`prev_hash` removed and `decision` changed to "approved";
         assert `ok is False` and `first_break_line == 2`) and
         `test_verify_chain_rejects_a_junk_line_replacing_a_chained_one` (line 1 replaced by a
         chain-less dict; `ok is False` and `first_break_line == 2`, because line 2's `prev_hash`
         is not the genesis hash). Both fail today. Change `test_verify_chain_skips_legacy_entries_with_no_hash`
         so its file has two legacy lines followed by chained lines (written by a real
         AuditLogger after the legacy lines are on disk) and still asserts ok.
      2. src/privacyfence/audit_log.py: implement 3.3's `verify_chain` change, including the
         genesis rule. Update the method
         docstring: legacy lines are accepted only before the first chained line.
      3. TST-A5, same class: `test_editing_any_recorded_field_breaks_the_chain`, parametrized over
         `[f.name for f in dataclasses.fields(AuditEntry) if f.name not in ("entry_hash", "prev_hash")]`:
         record 2 entries, change that field of line 1 to a different value of the same JSON type
         (str → + "x", bool → not, int/float → + 1, list → + ["x"], dict → {"x": 1}, None → "x"),
         keep its `entry_hash`, assert `verify_chain(...).ok is False`.
      4. scripts/verify_audit_log.py: add `_chain_ends` and the continuity and tail checks of 3.3,
         with exactly those lines. tests/unit/test_verify_audit_log.py: add tests for a removed
         middle week (three consecutive weeks recorded with the file's `_record_entries(log_dir,
         week, n)` helper at :21, delete the middle file → exit 1 and the continuity line), a
         truncated tail (drop the last line of the newest file → exit 1 and the tail line), a week
         that restarts at genesis (delete `.audit_chain_state.json` between weeks → exit 0 and the
         "note" line), and an intact set → exit 0. Follow the file's existing way of invoking
         main(). Stop condition: if an existing test in this file fails because of the new checks,
         stop with status=blocked and quote it.
      5. tests/system/test_local_mode_system.py, step 8 (~:405-421): after the decisions assertion,
         build `AuditLogger(str(audit_dir))` (import from privacyfence.audit_log) and, for each
         `*.jsonl` stem in that directory, assert `verify_chain(stem).ok` and that the sum of
         `entries_checked` is at least 4.
      6. Run python3 -m pytest tests/unit/test_audit_log.py tests/unit/test_verify_audit_log.py
         tests/system/test_local_mode_system.py -q, ruff check ., python3 -m mypy src/privacyfence.
      Stop condition: if a real install's chain could legitimately contain a chain-less line after
      a chained one (search audit_log.py for any writer that records without hashing), stop with
      status=blocked and name it.
    acceptance:
      - "SEC-A3/TST-A6: python3 -m pytest tests/unit/test_audit_log.py -k \"chainless_line_after_chained_entries or junk_line_replacing or legacy_entries\" -q passes"
      - "TST-A5: python3 -m pytest tests/unit/test_audit_log.py -k editing_any_recorded_field -q passes"
      - python3 -m pytest tests/unit/test_verify_audit_log.py -q passes and includes the removed-week and truncated-tail cases
      - python3 -m pytest tests/system/test_local_mode_system.py -q passes and the file contains verify_chain

  - id: p5-address-parsing
    title: Parse the real address in the domain and self rules
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/scopes.py
      - tests/unit/policy/test_scopes.py
      - tests/unit/test_auto_accept.py
    brief: |
      Read docs/approval-core-hardening-plan.md sections 2 and 3.4. Findings: SEC-A4, TST-A7.
      1. tests/unit/policy/test_scopes.py: after `TestIdentitySpoofResistance` add
         `_SPOOF_DOMAIN_SELECTORS` and `class TestDomainAndSelfSpoofResistance` with three tests,
         each parametrized over the payloads
         `'"CEO <ceo@trusted.com>" <attacker@evil.com>'`, `'attacker@evil.com (<x@trusted.com>)'`
         `"x@evil.com@trusted.com"`, `'alice@trusted.com)<bob@evil.com>'` and, for
         to_is_myself, `'me@example.com <attacker@evil.com>'`. The class docstring says the v1
         reference oracle keeps the old parse as a documented divergence (3.4):
         - `test_trusted_sender_domain_ignores_display_name_address`:
           `SCOPE_SELECTORS["trusted_sender_domain"].matches(["trusted.com"], make_ctx(raw_data=SimpleNamespace(sender=p)))`
           is False.
         - `test_approved_recipient_domain_ignores_display_name_address`: the selector with
           `["trusted.com"]` and `make_ctx(args={"to": p})` is False.
         - `test_to_is_myself_ignores_display_name_address`: `make_ctx(args={"to": p}, my_email="me@example.com")`
           is False.
         Also add positive controls: `"Alice <alice@trusted.com>"` and `"alice@sub.trusted.com"`
         still match trusted_sender_domain; `"me@example.com"` and `"Me <me@example.com>"` still
         match to_is_myself. Run them: the spoof cases fail today.
      2. src/privacyfence/auto_accept.py `_domain_of` and src/privacyfence/policy/scopes.py
         `_trusted_sender_domain_matches`, `_to_is_myself_matches`,
         `_approved_recipient_domain_matches`: change exactly as 3.4 says.
      3. Do not edit tests/unit/policy/_v1_reference.py (3.4).
      4. tests/unit/test_auto_accept.py: add `_domain_of` cases: `"Bob <bob@x.com>"` → `x.com`;
         `'"CEO <ceo@trusted.com>" <attacker@evil.com>'` → `evil.com`; `"no-at-sign"` → `""`;
         `"x@evil.com@trusted.com"` → `""`.
      5. grep -rn "_domain_of(" src/privacyfence and check every other caller still gets a correct
         result with the parseaddr version; list them in your final report.
      6. Run python3 -m pytest tests/unit/policy tests/unit/test_auto_accept.py tests/unit/test_gate.py -q,
         then ruff check . and python3 -m mypy src/privacyfence.
      Stop condition: if TestEverySelectorAgreesWithItsCounterpart fails on a fixture after
      step 2, stop with status=blocked and quote the fixture; do not edit FIXTURES or the oracle.
    acceptance:
      - "SEC-A4/TST-A7: python3 -m pytest tests/unit/policy/test_scopes.py -k DomainAndSelfSpoofResistance -q passes"
      - python3 -m pytest tests/unit/policy tests/unit/test_auto_accept.py -q passes
      - grep -n 'raw_sender.index("<")' src/privacyfence/policy/scopes.py returns nothing

  - id: p6-retire
    title: ADRs 0175 and 0176, reference docs, changelog, and delete the plan
    depends_on: [p1-approval-binding, p2-ledger-and-rules-tests, p3-rules-reload-order, p4-audit-chain, p5-address-parsing]
    complexity: S
    touches:
      - docs/adr/0175-*.md
      - docs/adr/0176-*.md
      - docs/adr/0073-an-approved-write-is-single-use-and-an-approved-read-replays.md
      - docs/adr/0071-audit-log-integrity-is-a-keyed-hash-chain-plus-off-host-forwarding.md
      - docs/adr/README.md
      - docs/approvals-and-policy.md
      - docs/how-it-works.md
      - docs/security-and-compliance.md
      - CHANGELOG.md
      - docs/approval-core-hardening-plan.md
      - docs/approval-core-hardening-plan-manual-steps.html
    brief: |
      Read docs/approval-core-hardening-plan.md (whole) and docs/adr/README.md (template, rules).
      1. docs/adr/0175-an-approval-is-bound-to-the-whole-request-and-what-its-card-showed.md, title
         "ADR 0175: An approval is bound to the whole request and to what its card showed",
         Status "Accepted — <today>. Amends 0073." Context: the ledger key held only the
         connector's policy args; one approved Slack message could be redeemed for another, an
         approved read replayed for a wider page with no PII check. Decision: 3.1's key (request
         JSON minus reason, policy args, card digest; any difference registers a new card; rule
         matching still uses policy args). Alternatives: 3.1's three rejected items, and
         re-checking PII only on a read replay (the user chose a new card on any change).
         Consequences: 3.1's consequences list, plus "a new tool is covered with no per-tool work". Verification:
         approvals.binding_key/card_digest, gate.gated_call, mcp_dispatch request_args_scope,
         tests/unit/test_gate.py::TestApprovalBinding,
         tests/unit/test_systemic_gate_invariants.py::TestApprovalBindingCoversTheWholeRequest.
      2. docs/adr/0176-a-chainless-audit-line-is-accepted-only-before-the-first-chained-line.md,
         "Amends 0071": 3.3's decision; Alternatives: treating every chain-less line as a break
         (rejected: logs written before chaining existed would fail forever); it also records the
         genesis rule and the "new chain segment" note from 3.3; Verification:
         audit_log.verify_chain, scripts/verify_audit_log.py, the p4 tests.
      3. ADR 0073 and ADR 0071: add one Status line each, "Amended by 0175." / "Amended by 0176."
         (rule 2 allows only Status edits). docs/adr/README.md: add both index rows and change the
         0073 and 0071 rows' status cells to "Accepted; amended by 0175" / "...0176" (keep any
         existing amended-by text).
      4. docs/approvals-and-policy.md, after the paragraph ending "through the original call."
         (:~50), add: "Your decision applies to exactly what you saw: if any argument of the
         repeated call differs, or the content the card would show has changed, the repeat gets
         a new card ([ADR 0175](adr/0175-an-approval-is-bound-to-the-whole-request-and-what-its-card-showed.md))." docs/how-it-works.md:160 "repeats the original
         call with the same arguments" gets the same rule in one sentence.
         docs/security-and-compliance.md, after the "Exit status" line (:~327): "The script also
         checks that each week's first entry continues the previous week's last one, and that the
         newest entry matches `.audit_chain_state.json`, so a removed week or a cut-off tail is
         reported, unless the `.audit_chain_state.json` file was edited too (it is not keyed). Lines written before chaining existed are accepted only at the top of a file
         ([ADR 0176](adr/0176-a-chainless-audit-line-is-accepted-only-before-the-first-chained-line.md))."
      5. CHANGELOG.md under "## [Unreleased]" (create the subsection headings the file already
         uses for Security/Fixed if missing; never a version heading):
         - Security: "An approval now covers exactly the request and content the card showed. A
           repeated call with any different argument, or whose content changed, gets a new card,
           so an approved message can no longer be swapped for another one."
         - Security: "`trusted_sender_domain`, `approved_recipient_domain` and `to_is_myself`
           rules read the real email address, so a display name such as
           `"CEO <ceo@trusted.com>" <attacker@evil.com>` no longer matches."
         - Security: "`verify_audit_log.py` now reports a forged line placed after chained entries,
           a removed week, and entries cut from the end of the log (unless the chain-state file
           was edited too)."
         - Fixed: "A rule added in Settings now resolves the pending approvals it covers right
           away."
      6. git rm docs/approval-core-hardening-plan.md docs/approval-core-hardening-plan-manual-steps.html;
         grep -rn "approval-core-hardening-plan" . --include=*.md --include=*.html --include=*.py
         --include=*.yml returns nothing.
      7. Run python3 -m pytest tests/unit/test_code_no_history.py tests/unit/test_docs_no_history.py
         tests/unit/test_docs_links.py -q and ruff check .
    acceptance:
      - ls docs/adr/0175-*.md docs/adr/0176-*.md lists both files and each contains "Accepted"
      - grep -c "0175\|0176" docs/adr/README.md is at least 2
      - grep -n "Amended by 0175" docs/adr/0073-*.md and grep -n "Amended by 0176" docs/adr/0071-*.md match
      - test ! -e docs/approval-core-hardening-plan.md && test ! -e docs/approval-core-hardening-plan-manual-steps.html
      - python3 -m pytest tests/unit/test_code_no_history.py tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py -q passes
```
