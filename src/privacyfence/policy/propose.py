"""Rule proposals and the one writer -- P5 of the policy v2 redesign.

P5's job is F2: today the popup's **Always allow** button and Settings' **Write auto-accept**
toggle are described to the user in the same words and mean different things. Clicking the button on
a Sheets write creates a rule for that one operation key; turning on the toggle for the same folder
covers thirteen. Neither surface can say which it is, because they are built out of different
tables -- five of them on the popup side alone (``auto_accept.SUGGESTION_FAMILIES``,
``auto_accept._MULTI_CANDIDATE_FAMILIES``, ``auto_accept.WRITE_RULE_SUGGESTIONS`` with its
``_SANDBOX_WRITE_VALUE_BUILDERS``, ``auto_accept._RULE_SHORT_HINTS`` and
``auto_accept._RULE_DESCRIPTIONS``) against ``resource_grants.GRANT_RESOURCE_TYPES`` on the
Settings side.

This module replaces all of them with one catalogue and one question -- *which scopes contain this
item?* -- answered by P2's selector registry itself:

* ``PROPOSABLE_SCOPES`` declares, once, each scope a surface may offer: its v2 scope type, which
  **verbs** it can govern, and how to derive its value from the call under review. Which *operation
  keys* a (scope, verb) pair reaches is not declared -- it is derived from P1's tool registry, so
  adding a Drive write tool widens the folder scope's "update" verb automatically instead of needing
  an edit to ``DRIVE_SANDBOX_WRITE_TARGETS`` and everything built from it (F10).
* A candidate is only proposed when the scope selector for it **confirms the item it was derived
  from** (``scopes.SCOPE_SELECTORS[...].matches(value, ctx)``). A popup can therefore never propose
  a rule that would not have accepted the very item it was proposed from -- the selectors are the
  oracle, not a second copy of their logic.
* ``rules_for_proposal`` (the popup's intent: one scope, the verb just gated, plus whichever
  widenings the user explicitly took) and ``rules_for_scope_group`` (Settings' intent: one scope, a
  set of verbs) both build their rule set out of the same ``_rules_from_pairs``. Given the same
  intent they produce byte-identical rules because it is the same function -- P5's exit criterion,
  asserted directly in ``tests/unit/policy/test_propose.py``.

**Narrowest first, widening is explicit.** A proposal's own rule covers exactly the one operation
key that was just gated -- the same blast radius ``auto_accept.add_auto_accept_rule`` has today, so
a proposal accepted as-is can never be wider than v1's equivalent. Everything beyond that is a
named ``Widening``: one per verb the same scope can govern, ordered narrowest family first, each
carrying exactly the ``(predicate, operation)`` pairs it adds. Taking every write-family widening on
a ``drive.folder`` proposal reproduces the sandbox-folder grant's thirteen operation keys exactly --
which is the point: the width a grant always had is now something the user is shown and chooses,
rather than something one boolean hides.

**What this phase deliberately does not propose.** F5's three ungovernable operation groups stay
that way here, each for its own reason, and none of them is an oversight:

* ``apps_script.read_content``/``write_content``/``read_execution_log`` do have a v2 scope --
  ``apps_script.project``, which P2 already implements -- but it is not a *v1* predicate, and the v1
  ``auto_accept_rules`` section is still the write target. A rule naming it would be one the v1
  evaluator cannot evaluate, which is the same dead rule from the other direction.
* ``slack.create_group_chat`` has no scope in the redesign proposal's §04 catalogue at all: its
  verb's subject is an audience, and no scope type measures one.
* ``gmail.create_filter``/``update_filter`` could only be scoped by ``gmail.anything``, and an
  ``always_allow`` rule under those keys is precisely P0·3's security hole -- a live, unconditional
  rule that no surface can render or remove, for an operation that can silently archive or forward
  mail indefinitely. It needs P7's write-time validation before it needs a proposal.

All three become proposable once the v2 store is the write target and a rule the UI cannot render
is rejected at write time (P6, P7). Until then this module's writer refuses outright to persist a
predicate the v1 evaluator cannot evaluate (``v1_entries``) rather than quietly creating one.

Separately, a scope is told which operation keys it *cannot* address (``ProposableScope.excludes``)
where deriving over a shared verb would over-reach: ``calendar.out_of_office`` and
``calendar.working_location`` carry no ``calendar_id`` at all, so a ``calendar.calendar`` rule
naming them could never match, and the redesign proposal's §07 is explicit that such a rule is
rejected rather than stored.

``gate.py``'s two "Always allow" call sites (P9) build their button/confirmation-dialog choices from
``proposals_for``/``rules_for_proposal`` directly and write the result through
``auto_accept.add_policy_v2_rules`` -- the same one-shape write path Settings (P6) and the MCP
bridge (P7) already share.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..auto_accept import ReviewContext, _domain_of, _file_from
from . import conditions, registry, scopes, store
from .engine import PolicyRule
from .registry import TOOL_REGISTRY, VERB_FAMILY, Verb, VerbFamily

# Returned by a ``value_of`` builder that cannot derive a value from this call -- distinct from a
# value that is legitimately ``None`` (every value-less scope, e.g. ``i_am_owner``, derives ``None``
# as a *real* value and lets its selector decide). Same job as
# ``auto_accept._NO_MATCH``/``_NO_SUGGESTION``, which is why there is one sentinel here and not two:
# the read and write suggestion paths were only ever separate because their tables were.
NO_VALUE: Any = object()

# An operation key's namespace is its connector, except for the two that ride Drive's own scopes:
# Sheets and Docs tools address a Drive file and are governed by a ``drive.folder`` rule (see
# ``resource_grants.DRIVE_FOLDER_READ_TARGETS``' own comment for why). This is the whole of the
# mapping -- every other namespace is its own connector.
_NAMESPACE_CONNECTOR: dict[str, str] = {"sheets": "drive", "docs": "drive"}


def connector_of_operation(operation: str) -> str:
    """The connector whose scopes can govern ``operation`` -- its namespace, or ``drive`` for the
    Sheets/Docs keys that address a Drive file."""
    namespace = operation.split(".", 1)[0]
    return _NAMESPACE_CONNECTOR.get(namespace, namespace)


ALL_OPERATIONS: frozenset[str] = frozenset(
    entry.operation for entry in TOOL_REGISTRY.values() if entry.operation is not None
)


# ── Value builders ──────────────────────────────────────────────────────────────────────────────
#
# One per scope whose rule carries a value. Each derives the value that would put *this* call's item
# in scope; the scope's own selector then confirms it (see ``_candidate_value``), so a builder never
# has to reproduce the selector's matching rules -- only find the field the selector will read.


def _no_value_needed(_ctx: ReviewContext) -> Any:
    """For a value-less scope (``i_am_owner``, ``dm_with_myself``, a condition scope): there is
    nothing to derive, and whether the scope applies is the selector's own question."""
    return None


def _folder_ids(ctx: ReviewContext) -> Any:
    """The folder(s) a Drive/Sheets/Docs item currently lives in -- what ``approved_folder``/
    ``approved_sandbox_folder``/``move_within_approved_folders`` all read (F1: one selector, three
    v1 names). For a move, ``raw_data``'s file is the file *before* the move, i.e. its source
    folder."""
    parents = list(getattr(_file_from(ctx.raw_data), "parent_ids", []) or [])
    return parents or NO_VALUE


def _args_values(name: str) -> Callable[[ReviewContext], Any]:
    """A single-entry list built from one call argument -- the shape every identity scope's value
    takes (a rule allows a *set* of resources; a proposal derived from one call names one of them)."""

    def build(ctx: ReviewContext) -> Any:
        value = ctx.args.get(name)
        return [value] if value else NO_VALUE

    return build


def _sender_domain(ctx: ReviewContext) -> Any:
    domain = _domain_of(getattr(ctx.raw_data, "sender", "") or "")
    return [domain] if domain else NO_VALUE


def _slack_channel(ctx: ReviewContext) -> Any:
    channel_id = ctx.args.get("channel_id", "") or ctx.args.get("channel", "") or ""
    return [channel_id] if channel_id else NO_VALUE


def _slack_result_channels(ctx: ReviewContext) -> Any:
    """Every channel present across a search's results -- ``approved_channel_all_results``' value.
    A search has no single ``channel_id`` argument to scope to, which is the whole reason that twin
    predicate exists (F7); in v2 it is the ``search`` verb of the same ``slack.channel`` scope."""
    items = ctx.raw_data if isinstance(ctx.raw_data, list) else [ctx.raw_data]
    channel_ids = sorted({cid for item in items if (cid := getattr(item, "channel_id", None))})
    return channel_ids or NO_VALUE


def _telegram_chat(ctx: ReviewContext) -> Any:
    # ``!= ""`` rather than truthiness, matching ``_approved_chats_matches``' own read: a chat id is
    # stringified, and ``0`` is falsy but not absent.
    chat_id = ctx.args.get("chat_id", "")
    return [str(chat_id)] if chat_id != "" else NO_VALUE


def _telegram_result_chats(ctx: ReviewContext) -> Any:
    items = ctx.raw_data if isinstance(ctx.raw_data, list) else [ctx.raw_data]
    chat_ids = sorted({str(cid) for item in items if (cid := getattr(item, "chat_id", None)) is not None})
    return chat_ids or NO_VALUE


def _jira_project_keys(ctx: ReviewContext) -> Any:
    """``jira_create_issue`` carries ``project_key`` directly; every other Jira tool carries only
    ``issue_key``, whose ``PROJ-123`` prefix is the project -- the same two-step read
    ``_approved_project_keys_matches`` performs."""
    project_key = ctx.args.get("project_key", "") or ""
    if not project_key:
        issue_key = ctx.args.get("issue_key", "") or ""
        project_key = issue_key.split("-")[0] if "-" in issue_key else ""
    return [project_key] if project_key else NO_VALUE


def _confluence_space_keys(ctx: ReviewContext) -> Any:
    raw = ctx.raw_data
    space_key = ctx.args.get("space_key") or (raw.get("space_key") if isinstance(raw, dict) else "") or ""
    return [space_key] if space_key else NO_VALUE


def _salesforce_object_types(ctx: ReviewContext) -> Any:
    """``salesforce_search`` takes a comma-separated ``object_types``; ``salesforce_get_record`` a
    single ``object_type``. An unscoped search reaches Salesforce's whole globally-searchable set,
    which is too broad to derive a rule from -- no value, so nothing is proposed."""
    if "object_types" in ctx.args:
        requested = [t.strip() for t in (ctx.args.get("object_types") or "").split(",") if t.strip()]
        return requested or NO_VALUE
    object_type = ctx.args.get("object_type", "") or ""
    return [object_type] if object_type else NO_VALUE


def _task_list_ids(ctx: ReviewContext) -> Any:
    """``tasks_move_task`` carries a source and a destination list instead of one ``task_list_id``,
    and a rule for a move has to cover both ends -- one scoped to only one of them would let a task
    move into (or out of) a list the user never approved."""
    if "task_list_id" in ctx.args:
        task_list_id = ctx.args.get("task_list_id") or ""
        return [task_list_id] if task_list_id else NO_VALUE
    source = ctx.args.get("source_list_id", "") or ""
    destination = ctx.args.get("destination_list_id", "") or ""
    return sorted({source, destination}) if source and destination else NO_VALUE


# ── The scope catalogue ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProposableScope:
    """One scope a surface may offer, and everything either surface needs to know about it.

    ``verbs`` is what makes a width explicit: a scope declares the verbs it can govern, and the
    operation keys follow from P1's registry (``operations_for``). ``excludes`` names the operation
    keys that derivation over-reaches -- an operation whose arguments cannot carry this scope's
    identity at all, so a rule naming it could never match and must not be written (the "rejected at
    write time, not silently stored as a rule that can never match" invariant of the redesign
    proposal's §07).

    ``condition`` is set only for the two *condition* scopes v1's calendar read family offered as
    plain rules ("any event with no external attendees"). They have no resource identity to scope
    to, so their v2 shape is the honestly-unconditional ``<connector>.anything`` scope carrying one
    ``when:``. ``v1_rule`` is the v1 rule name such a scope persists under, which for a
    condition scope is the condition's own v1 name rather than ``always_allow``.
    """

    id: str
    predicate: str
    scope_type: str
    connector: str
    verbs: frozenset[Verb]
    value_of: Callable[[ReviewContext], Any]
    hint: str
    widening_group: str
    excludes: frozenset[str] = frozenset()
    condition: tuple[str, Any] | None = None
    widenable: bool = True


def _scope(
    predicate: str,
    scope_type: str,
    connector: str,
    verbs: Iterable[Verb],
    value_of: Callable[[ReviewContext], Any],
    hint: str,
    *,
    entry_id: str | None = None,
    group: str | None = None,
    excludes: Iterable[str] = (),
    condition: tuple[str, Any] | None = None,
    widenable: bool = True,
) -> ProposableScope:
    """``id`` and ``widening_group`` default to the two things that are almost always right: the
    predicate identifies the entry, and every entry of one scope type widens into the others of that
    type. Both are overridden where v1's names force it -- see ``PROPOSABLE_SCOPES`` -- and a
    condition scope always gets its own id and group, since every one of them shares the predicate
    ``always_allow`` and differs only in the condition it carries."""
    resolved_id = entry_id or (predicate if condition is None else f"{scope_type}/{condition[0]}")
    return ProposableScope(
        id=resolved_id, predicate=predicate, scope_type=scope_type, connector=connector,
        verbs=frozenset(verbs), value_of=value_of, hint=hint,
        widening_group=group or (scope_type if condition is None else resolved_id),
        excludes=frozenset(excludes), condition=condition, widenable=widenable,
    )


# Declaration order is proposal order within a connector, and it is deliberately **identity scopes
# before attribute scopes before condition scopes** -- narrowest first, per P5's brief. That is a
# change from v1's hand-declared ``SUGGESTION_FAMILIES`` order (which put ``i_am_owner`` ahead of
# ``approved_folder``, i.e. "every file you own" ahead of "this one folder"); the set of proposals
# is unchanged or larger, only which one the popup offers first differs.
PROPOSABLE_SCOPES: tuple[ProposableScope, ...] = (
    # ── Drive (with Sheets/Docs, which address a Drive file) ───────────────────────────────────
    #
    # One scope type, four v1 predicate names, because v1 named the same folder check once per verb
    # that needed it (F1). The three that read the item's own parents share a value; the upload's
    # own name reads the destination out of args, since the file does not exist yet.
    _scope("approved_folder", "drive.folder", "drive", (Verb.READ, Verb.DOWNLOAD), _folder_ids, "this folder"),
    _scope(
        "approved_sandbox_folder", "drive.folder", "drive",
        (Verb.UPDATE, Verb.FORMAT, Verb.RESTRUCTURE, Verb.COMMENT, Verb.DELETE),
        _folder_ids, "this folder",
    ),
    _scope("parent_folder_allowlist", "drive.folder", "drive", (Verb.CREATE,), _args_values("parent_folder_id"), "this folder"),
    _scope("move_within_approved_folders", "drive.folder", "drive", (Verb.MOVE,), _folder_ids, "this folder"),
    _scope("i_am_owner", "drive.owned_by_me", "drive", (Verb.READ, Verb.DOWNLOAD), _no_value_needed, "if I own it"),
    # ── Gmail ─────────────────────────────────────────────────────────────────────────────────
    _scope("i_am_sender", "gmail.sender", "gmail", (Verb.READ, Verb.DOWNLOAD, Verb.ARCHIVE), _no_value_needed, "if I'm sender"),
    _scope(
        "trusted_sender_domain", "gmail.sender_domain", "gmail",
        (Verb.READ, Verb.DOWNLOAD, Verb.ARCHIVE), _sender_domain, "this sender domain",
    ),
    _scope("label_name_allowlist", "gmail.label", "gmail", (Verb.LABEL,), _args_values("label_name"), "this label"),
    # D4: Gmail drafting is unconditional today (``always_allow`` under ``gmail.create_draft``), and
    # modelling it as an explicit "anything in Gmail" scope is the point -- an unconditional grant
    # that reads as unconditional. It is never widenable: a scope with no resource identity must not
    # be offered a one-click width increase, and ``draft`` is a send-family verb.
    _scope(
        "always_allow", "gmail.anything", "gmail", (Verb.DRAFT,), _no_value_needed, "",
        group="gmail.anything", widenable=False,
    ),
    # ── Calendar ──────────────────────────────────────────────────────────────────────────────
    #
    # ``calendar.out_of_office`` and ``calendar.working_location`` always act on your own primary
    # calendar and carry no ``calendar_id`` argument at all, so ``personal_calendar`` has nothing to
    # check for them -- derivation over their ``create``/``update`` verbs has to be told so.
    _scope(
        "personal_calendar", "calendar.calendar", "calendar",
        (Verb.READ, Verb.CREATE, Verb.UPDATE, Verb.SHARE, Verb.DELETE), _args_values("calendar_id"),
        "this calendar", excludes=("calendar.out_of_office", "calendar.working_location"),
    ),
    _scope("i_am_organizer", "calendar.organized_by_me", "calendar", (Verb.READ,), _no_value_needed, "if I organize it"),
    # ── Slack ─────────────────────────────────────────────────────────────────────────────────
    #
    # ``read`` and ``search`` are two verbs of one scope, not two allowlists (F7): they share the
    # ``slack.read_messages`` operation key, and each carries the predicate whose value that verb
    # can actually be measured from -- args for a single channel, every result for a search.
    _scope("approved_channel", "slack.channel", "slack", (Verb.READ,), _slack_channel, "this channel"),
    _scope("approved_channel_all_results", "slack.channel", "slack", (Verb.SEARCH,), _slack_result_channels, "this channel"),
    _scope("approved_recipient", "slack.channel", "slack", (Verb.SEND,), _slack_channel, "this channel"),
    # The channel-kind attributes are mutually exclusive descriptions of one channel, not degrees of
    # the same scope, so each gets its own widening group: "my own DM" must never offer "…and every
    # group DM" as a widening of itself.
    _scope("dm_with_myself", "slack.channel_kind", "slack", (Verb.READ,), _no_value_needed, "my own DM", group="slack.channel_kind/self_dm"),
    _scope("group_dm", "slack.channel_kind", "slack", (Verb.READ,), _no_value_needed, "this group DM", group="slack.channel_kind/group_dm"),
    # ── Telegram ──────────────────────────────────────────────────────────────────────────────
    _scope("approved_chats", "telegram.chat", "telegram", (Verb.READ, Verb.SEND), _telegram_chat, "this chat"),
    _scope("approved_chats_all_results", "telegram.chat", "telegram", (Verb.SEARCH,), _telegram_result_chats, "this chat"),
    # ── Jira ──────────────────────────────────────────────────────────────────────────────────
    _scope(
        "approved_project_keys", "jira.project", "jira",
        (Verb.READ, Verb.CREATE, Verb.COMMENT, Verb.UPDATE, Verb.TRANSITION), _jira_project_keys, "this project",
    ),
    _scope("i_am_reporter", "jira.my_issues", "jira", (Verb.READ,), _no_value_needed, "if I'm reporter", group="jira.my_issues/reporter"),
    _scope("i_am_assignee", "jira.my_issues", "jira", (Verb.READ,), _no_value_needed, "if I'm assignee", group="jira.my_issues/assignee"),
    # ── Confluence ────────────────────────────────────────────────────────────────────────────
    _scope(
        "approved_space_keys", "confluence.space", "confluence",
        (Verb.READ, Verb.DOWNLOAD, Verb.CREATE, Verb.UPDATE), _confluence_space_keys, "this space",
    ),
    _scope(
        "i_am_author", "confluence.authored_by_me", "confluence",
        # ``download`` as well as ``read``: ``confluence_download_attachment`` fetches the page for
        # its own preview before gating and passes that same page as ``raw_data``, so authorship is
        # exactly as checkable there as on a page read.
        (Verb.READ, Verb.DOWNLOAD), _no_value_needed, "if I'm author",
    ),
    # ── Salesforce ────────────────────────────────────────────────────────────────────────────
    #
    # Both Salesforce read tools share the ``read`` verb, and each has its own scope: a record read
    # is scoped by object type, a report run by report id. Neither scope can measure the other's
    # operation key.
    _scope(
        "approved_object_types", "salesforce.object_type", "salesforce",
        (Verb.READ, Verb.SEARCH), _salesforce_object_types, "this object type",
        excludes=("salesforce.run_report",),
    ),
    _scope(
        "approved_report_ids", "salesforce.report", "salesforce", (Verb.READ,), _args_values("report_id"),
        "this report", excludes=("salesforce.read_record",),
    ),
    # ── Tasks ─────────────────────────────────────────────────────────────────────────────────
    _scope(
        "approved_task_list", "tasks.list", "tasks",
        (Verb.CREATE, Verb.UPDATE, Verb.COMPLETE, Verb.MOVE), _task_list_ids, "this list",
    ),
    # ── Contacts ──────────────────────────────────────────────────────────────────────────────
    # ``label_name_allowlist`` is the one predicate that genuinely serves two scope types (see
    # ``scopes.SCOPE_SELECTORS``' own tuple for it), so this entry cannot be identified by its
    # predicate alone the way every other one is.
    _scope(
        "label_name_allowlist", "contacts.label", "contacts", (Verb.LABEL,), _args_values("label_name"),
        "this label", entry_id="contacts.label",
    ),
    # ── Condition scopes (no resource identity -- see ProposableScope.condition) ───────────────
    _scope(
        "always_allow", "calendar.anything", "calendar", (Verb.READ,), _no_value_needed,
        "no external attendees", condition=("no_external_attendees", None), widenable=False,
    ),
    _scope(
        "always_allow", "calendar.anything", "calendar", (Verb.READ,), _no_value_needed,
        "non-private events", condition=("not_private", None), widenable=False,
    ),
)


def _by_group(entries: tuple[ProposableScope, ...]) -> dict[str, tuple[ProposableScope, ...]]:
    grouped: dict[str, list[ProposableScope]] = {}
    for entry in entries:
        grouped.setdefault(entry.widening_group, []).append(entry)
    return {group: tuple(members) for group, members in grouped.items()}


_SCOPES_BY_GROUP: dict[str, tuple[ProposableScope, ...]] = _by_group(PROPOSABLE_SCOPES)

# Public alias -- P6's Settings page builds its "add a rule" scope picker directly off this (one
# entry per widening group, the same grouping `rules_for_scope_group` itself keys on), rather than
# re-deriving the grouping from `PROPOSABLE_SCOPES` a second time.
SCOPES_BY_GROUP: dict[str, tuple[ProposableScope, ...]] = _SCOPES_BY_GROUP

# Risk order, so a widening list reads narrowest-first and a destructive or send widening is never
# the one a hurried click lands on.
_FAMILY_ORDER: tuple[VerbFamily, ...] = (
    VerbFamily.READ, VerbFamily.WRITE, VerbFamily.SEND, VerbFamily.DESTRUCTIVE,
)
_VERB_ORDER: tuple[Verb, ...] = tuple(Verb)


def verb_sort_key(verb: Verb) -> tuple[int, int]:
    """Risk order for a verb: read family first, destructive last, stable within a family."""
    return _FAMILY_ORDER.index(VERB_FAMILY[verb]), _VERB_ORDER.index(verb)


def scope_needs_value(scope: ProposableScope) -> bool:
    """Whether ``scope`` has a resource identity to type in, or is a value-less attribute/condition
    scope ("if I own it", "no external attendees") that only ever needs a verb selection. P6's
    Settings page uses this to decide whether its "add a rule" form shows a value field for a given
    scope group -- a group is homogeneous on this (every member of one widening group shares a
    value shape), so checking the group's first entry is enough."""
    return scope.value_of is not _no_value_needed


def operations_for(scope: ProposableScope, verb: Verb) -> frozenset[str]:
    """Every operation key ``scope`` can govern for ``verb`` -- derived from P1's registry, not
    declared. ``registry.operation_verbs`` is what makes the three double-verb keys
    (``slack.read_messages``, ``telegram.read_chat_messages``, ``calendar.create_modify_event``)
    resolve correctly: each appears under both of its verbs, so a rule for one verb of such a key
    never silently claims the other."""
    if verb not in scope.verbs:
        return frozenset()
    return frozenset(
        operation
        for operation in ALL_OPERATIONS
        if connector_of_operation(operation) == scope.connector
        and verb in registry.operation_verbs(operation)
        and operation not in scope.excludes
    )


# ── Proposals ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Widening:
    """One named, opt-in step beyond a proposal's own single operation key: "also allow format".

    ``pairs`` is the exact set of ``(predicate, operation_key)`` this widening adds and nothing
    more, so a rule set is a plain union of the narrow proposal's pair and whichever widenings the
    user took -- there is no expansion step that could round up to a verb family. A widening can add
    a *predicate* for an operation key the proposal already covers (a Slack read widened to search
    is the same key under its all-results predicate), which is why the unit here is a pair rather
    than an operation key.
    """

    verb: Verb
    pairs: frozenset[tuple[str, str]]

    @property
    def operations(self) -> frozenset[str]:
        return frozenset(operation for _predicate, operation in self.pairs)


@dataclass(frozen=True)
class RuleProposal:
    """One rule a surface may offer for the call under review, at its narrowest.

    ``operations`` is always exactly the one operation key that was gated -- the same width
    ``auto_accept.add_auto_accept_rule`` writes today, which is what makes accepting a proposal
    as-is provably no wider than v1's own "Always allow". ``widenings`` is everything beyond that,
    each named and chosen separately.
    """

    scope: ProposableScope
    value: Any
    verb: Verb
    operation: str
    widenings: tuple[Widening, ...] = ()

    @property
    def pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset({(self.scope.predicate, self.operation)})

    @property
    def operations(self) -> frozenset[str]:
        return frozenset({self.operation})


def _candidate_value(scope: ProposableScope, ctx: ReviewContext) -> Any:
    """``scope``'s value for this call, or ``NO_VALUE`` if it is not a candidate.

    The value builder only finds the field; whether the scope actually contains this item is the
    **selector's** own answer (``scopes.SCOPE_SELECTORS``, or ``conditions.CONDITION_SELECTORS`` for
    a condition scope). That is what collapses the five v1 suggestion tables into P2's registry:
    a value-less attribute scope has nothing to derive and is decided entirely by its selector, and
    a valued scope is only proposed when the value derived from the item would have accepted that
    same item. A selector that raises is a non-match, never a crash in a popup -- the same
    fail-closed posture ``policy.engine.evaluate`` takes.
    """
    value = scope.value_of(ctx)
    if value is NO_VALUE:
        return NO_VALUE
    try:
        if scope.condition is not None:
            selector = conditions.CONDITION_SELECTORS.get(scope.condition[0])
            if selector is None or not selector.holds(scope.condition[1], ctx):
                return NO_VALUE
            return value
        scope_selector = scopes.SCOPE_SELECTORS.get(scope.predicate)
        if scope_selector is None or not scope_selector.matches(value, ctx):
            return NO_VALUE
    except Exception:
        return NO_VALUE
    return value


def _widenings_for(scope: ProposableScope, verb: Verb, operation: str) -> tuple[Widening, ...]:
    """Every verb the proposal's scope group can govern beyond what the proposal itself covers, as
    one ``Widening`` each. An unwidenable scope (``<connector>.anything``) gets none."""
    if not scope.widenable:
        return ()
    narrow = frozenset({(scope.predicate, operation)})
    group = _SCOPES_BY_GROUP[scope.widening_group]
    widenings: list[Widening] = []
    for candidate_verb in sorted({v for entry in group for v in entry.verbs}, key=verb_sort_key):
        pairs = frozenset(
            (entry.predicate, op)
            for entry in group
            for op in operations_for(entry, candidate_verb)
        ) - narrow
        if pairs:
            widenings.append(Widening(verb=candidate_verb, pairs=pairs))
    return tuple(widenings)


def proposals_for(tool: str, ctx: ReviewContext) -> list[RuleProposal]:
    """Every rule a surface may offer for a gated call on ``tool``, narrowest scope first.

    Replaces ``auto_accept.suggest_rule``/``suggest_rule_choices``/``suggest_write_rule`` and the
    five tables behind them with one pass over ``PROPOSABLE_SCOPES``: the candidates are the scopes
    of this tool's own connector that can govern the verb this tool performs, and the survivors are
    the ones whose selector confirms the item. An ungoverned tool (no operation key), an unknown
    tool, or one whose verb no scope of its connector governs yields nothing -- the popup's "Always
    allow" button then simply doesn't render, exactly as it doesn't today when ``suggest_rule``
    returns ``None``.
    """
    entry = TOOL_REGISTRY.get(tool)
    if entry is None or entry.operation is None or entry.verb is None:
        return []
    connector = connector_of_operation(entry.operation)
    proposals: list[RuleProposal] = []
    for scope in PROPOSABLE_SCOPES:
        if scope.connector != connector or entry.verb not in scope.verbs:
            continue
        if entry.operation in scope.excludes:
            continue
        value = _candidate_value(scope, ctx)
        if value is NO_VALUE:
            continue
        proposals.append(
            RuleProposal(
                scope=scope, value=value, verb=entry.verb, operation=entry.operation,
                widenings=_widenings_for(scope, entry.verb, entry.operation),
            )
        )
    return proposals


# ── The one writer ──────────────────────────────────────────────────────────────────────────────


def _rules_from_pairs(
    pairs: Iterable[tuple[str, str]], value: Any, conditions_of: dict[str, tuple[tuple[str, Any], ...]],
) -> list[PolicyRule]:
    """Turn ``(predicate, operation_key)`` pairs into merged ``PolicyRule``s with stable ids.

    The single point every surface's write goes through. ``store.merge_rules`` does the rest: rules
    that agree on ``(predicate, value, conditions)`` become one row spanning every operation key
    they covered, under the content-derived id that row will always have. Iteration is sorted, so
    the same intent produces the same rule list -- and therefore the same YAML -- regardless of the
    order a caller happened to discover its pairs in.
    """
    by_predicate: dict[str, set[str]] = {}
    for predicate, operation in sorted(pairs):
        by_predicate.setdefault(predicate, set()).add(operation)
    return store.merge_rules([
        PolicyRule(
            id=predicate, predicate=predicate, value=value,
            operations=frozenset(operations), conditions=conditions_of.get(predicate, ()),
        )
        for predicate, operations in sorted(by_predicate.items())
    ])


def rules_for_proposal(proposal: RuleProposal, widenings: Iterable[Widening] = ()) -> list[PolicyRule]:
    """The popup's intent: this proposal, plus whichever widenings the user explicitly took.

    With no widenings the result is one rule covering one operation key. ``widenings`` need not come
    from this proposal's own list -- a caller passing an unrelated one gets exactly the pairs it
    names, since nothing here expands beyond a pair.
    """
    pairs = set(proposal.pairs)
    for widening in widenings:
        pairs |= widening.pairs
    conditions_of = {
        entry.predicate: (entry.condition,)
        for entry in _SCOPES_BY_GROUP[proposal.scope.widening_group]
        if entry.condition is not None
    }
    return _rules_from_pairs(pairs, proposal.value, conditions_of)


def rules_for_scope_group(group: str, value: Any, verbs: Iterable[Verb]) -> list[PolicyRule]:
    """Settings' intent: one scope, one value, a set of verbs -- the shape a
    ``resource_grants.GrantCapability`` toggle has always had, now expressed in the same vocabulary
    the popup uses.

    ``group`` is a widening group, which is a scope type except where v1's value-less attribute
    predicates are mutually exclusive variants of one type (``slack.channel_kind``, ``jira.
    my_issues``) -- see ``ProposableScope.widening_group``. An unknown group yields no rules rather
    than raising: fail closed, the same way an unrecognised predicate does everywhere else here.
    """
    entries = _SCOPES_BY_GROUP.get(group, ())
    wanted = frozenset(verbs)
    pairs = {
        (entry.predicate, operation)
        for entry in entries
        for verb in wanted
        for operation in operations_for(entry, verb)
    }
    conditions_of = {entry.predicate: (entry.condition,) for entry in entries if entry.condition is not None}
    return _rules_from_pairs(pairs, value, conditions_of)


__all__ = [
    "ALL_OPERATIONS",
    "NO_VALUE",
    "PROPOSABLE_SCOPES",
    "SCOPES_BY_GROUP",
    "ProposableScope",
    "RuleProposal",
    "Widening",
    "connector_of_operation",
    "verb_sort_key",
    "operations_for",
    "proposals_for",
    "rules_for_proposal",
    "rules_for_scope_group",
    "scope_needs_value",
]
