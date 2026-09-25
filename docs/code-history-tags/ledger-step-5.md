# Ledger: step 5

Slice: `tests/unit/code_history_pending/step-5.txt` (76 files, 295 guard hits), plus `website/` and
`CLAUDE.md`. `website/` and `CLAUDE.md` already passed both guards and needed no change.

## ADR candidates

1. **Apps Script gets no run/execute tool.** `connectors/apps_script.py` and `apps_script_client.py`
   say running a script is out of scope, and that one must not be added without a fresh threat-model
   discussion: Apps Script's runtime is opaque to PrivacyFence once a script starts, so a "run this
   script" approval could only be a blank cheque. The reasoning used to point at a closed issue's
   "Non-goals" section; it is now only in the module docstring. Meets the bar: a trust boundary, and a
   rejected alternative (a popup-gated run tool) for a non-obvious reason.
2. **Every MCP tool is advertised read-only/non-destructive.** `web/mcp_tools.py`
   (`_UNIFORM_READ_ONLY_ANNOTATIONS`), `tests/unit/web/test_mcp_tools.py`,
   `tests/unit/web/test_routes_mcp.py`. Annotations are UI hints, gate.py is the real boundary, and a
   truthful write annotation makes the client add a redundant confirmation in front of the real one.
   The only written record is `5deef1d8:docs/TECHNICAL_REFERENCE.md` (a file at an old commit) and a
   deleted refactor plan's section. Meets the bar: non-obvious, and the rejected alternative (honest
   annotations) looks like the correct choice at first sight. Note the open issue
   https://github.com/privacyfence/privacyfence/issues/46 ("All MCP tools advertised as read-only is a
   workaround, not a permanent posture"): the ADR should record the current posture and link it as
   the open follow-up. No code in this slice cites #46 today.
3. **The `/mcp` bearer token and the browser session cookie are audience-separated.**
   `web/routes_mcp.py` (`build_mcp_asgi_app`), `web/routes_file_bridge.py` (module docstring),
   `tests/unit/web/test_routes_mcp.py` (auth section). Neither credential is accepted on the other
   surface, which is why `/mcp` and the file bridge sit in their own nested Starlette app with their
   own auth stack. Previously cited as a finding ID and a deleted plan's section. Check whether ADR
   0008 or ADR 0010 already covers it before writing a new one; it is a trust boundary.

## Changed user-visible strings

All are `--help` output of developer/operator scripts (module docstrings passed to argparse):

- `scripts/release_stats.py` description: `Compute the website's public release-stats.json
  (release-publishing plan Phase 4).` → `Compute the website's public release-stats.json.`
- `scripts/sync_room_directory.py` epilog: `SEC-05 full signing (org_bundle_signing.py): merging ...`
  → `Bundle signing (org_bundle_signing.py, ADR 0016): merging ...`
- `scripts/qa_fixture_recorder.py` description: `(see docs/testing-policy.md §2.1)` → `(see
  docs/testing-policy.md's live-connector layer)`. `testing-policy.md` has no §2.1 any more, so the
  old pointer was dead.

No test asserts on any of them (`git grep` of the old strings finds nothing under `tests/`).

## Cross-slice edits needed

- `scripts/build_org_bundle.py` (step 4) still says `SEC-05 full signing` in its docstring and a
  section title. Step 4 owns the rewrite; if it names the feature, `Bundle signing` would match the
  wording `sync_room_directory.py` now uses for the same thing.

## Bugs noticed

None in behaviour. Leftovers outside the guard's shapes, noted for step 7 rather than fixed here:

- `tests/unit/test_website_download_cta.py`'s docstring says "It also holds guardrail 7": a plan item
  reference the guard does not catch.
- `src/privacyfence/local_files.py` cited `ADR 0007 SS1.2`, a mangled `§` reference that ADR 0007
  has no section for. It was in the same sentence as a phase tag and was rewritten with it.
- Several comments cite `5deef1d8:docs/<file>.md` (a document at an old commit). They pass the guard
  as named-document citations and were left alone.

## Open issues kept as URLs

None. Every issue the slice referenced was checked with the GitHub MCP (and against the list of
open issues) and is closed: #112, #113,
#154, #250, #365, #370, #373, #377, #396, #400, #415, #428, #643. All references were removed.
