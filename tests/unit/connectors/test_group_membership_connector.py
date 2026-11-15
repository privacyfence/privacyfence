"""privacyfence_update_group_membership: the scoped-token-only snapshot tool (plan 3.7)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from privacyfence import group_membership, scoped_tokens, upstream_mcp_runtime
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import group_membership as connector_module
from privacyfence.connectors.group_membership import TOOL_NAME, GroupMembershipConnector
from privacyfence.identity_claim import ClaimSigner
from privacyfence.upstream_claim_keys import KeySet
from privacyfence.upstream_mcp_config import GroupMapStore, parse_upstreams
from tests.helpers import assert_all_tools_leave_an_audit_trail

_FILE = {
    "version": 1,
    "upstreams": [
        {
            "id": "kh", "label": "Knowledge hub", "url": "https://kh.example/mcp", "audience": "kh-aud",
            "tools": {"search": {"gate": "auto", "verb": "search"}},
        },
        {
            "id": "wiki", "label": "Wiki", "url": "https://wiki.example/mcp", "audience": "wiki-aud",
            "tools": {"search": {"gate": "auto", "verb": "search"}},
        },
    ],
}
_ADMIN = {"upstream_mcp": {
    "kh": {"groups": {"everyone@co.example": ["all"], "mgmt@co.example": ["mgmt"]}},
    "wiki": {"groups": {"mgmt@co.example": ["wiki-mgmt"]}},
}}
_SCOPE = SimpleNamespace(
    id="st-1", label="Nightly sync", tools=frozenset({TOOL_NAME}), principal_id="u1", principal_email="a@co.example",
)


@pytest.fixture
def audit_dir(tmp_path):
    directory = tmp_path / "audit"
    directory.mkdir()
    init_audit_logger(str(directory))
    return directory


@pytest.fixture
def store(tmp_path):
    membership = group_membership.MembershipStore(tmp_path / "group_membership.json")
    group_membership.set_store(membership)
    return membership


@pytest.fixture
def configured(tmp_path, store):
    admin = tmp_path / "admin_config.json"
    admin.write_text(json.dumps(_ADMIN), encoding="utf-8")
    keys = KeySet(tmp_path / "keys.json")
    keys.ensure()
    upstream_mcp_runtime.configure(
        upstreams=parse_upstreams(_FILE, source="test"),
        signer=ClaimSigner(issuer="https://pf.example", keys=keys),
        groups=GroupMapStore(admin, frozenset({"kh", "wiki"})),
        resolver=group_membership.SnapshotResolver(store),
        membership_max_additions=3,
    )


def _entries(audit_dir):
    path = audit_dir / f"{current_week()}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _call(groups):
    with scoped_tokens.token_scope(_SCOPE):
        return await GroupMembershipConnector().call(TOOL_NAME, {"groups": groups})


def test_the_tool_spec():
    connector = GroupMembershipConnector(None)
    assert connector.name == "privacyfence"
    (spec,) = connector.tool_specs()
    assert spec.name == TOOL_NAME and not spec.read_only and spec.params == []
    assert len(spec.description) < 1024
    assert spec.input_schema["required"] == ["groups"]


async def test_another_tool_is_refused():
    with pytest.raises(ValueError):
        await GroupMembershipConnector().call("nope", {})


async def test_a_call_without_a_token_scope_is_refused_and_audited(audit_dir, configured):
    with pytest.raises(RuntimeError, match="can only be called with a scoped token"):
        await GroupMembershipConnector().call(TOOL_NAME, {"groups": {"everyone@co.example": ["a@x.example"]}})
    (entry,) = _entries(audit_dir)
    assert entry["decision"] == "group_membership_denied" and entry["tool"] == TOOL_NAME
    assert entry["connector"] == "privacyfence"


async def test_a_token_without_the_tool_is_refused(audit_dir, configured):
    other = SimpleNamespace(**{**_SCOPE.__dict__, "tools": frozenset({"gmail_get_message"})})
    with scoped_tokens.token_scope(other), pytest.raises(RuntimeError, match="scoped token"):
        await GroupMembershipConnector().call(TOOL_NAME, {"groups": {}})
    assert [e["decision"] for e in _entries(audit_dir)] == ["group_membership_denied"]


async def test_an_addition_to_an_all_group_applies(audit_dir, configured, store):
    result = await _call({"everyone@co.example": ["Bob@X.example"]})
    assert result == {
        "status": "applied", "added": 1, "removed": 0, "parked": 0, "ignored_groups": [], "reason": "",
    }
    assert store.members_of("bob@x.example") == {"everyone@co.example"}
    (entry,) = _entries(audit_dir)
    assert entry["decision"] == "group_membership_applied" and entry["sender"] == "Nightly sync"
    assert entry["auto_accept_rule"] == "auto"


async def test_an_addition_to_a_restricted_group_is_parked(audit_dir, configured, store):
    await _call({"everyone@co.example": ["bob@x.example"]})
    result = await _call({"everyone@co.example": ["bob@x.example"], "mgmt@co.example": ["ann@x.example"]})
    assert result["status"] == "applied" and result["added"] == 1
    # mgmt maps to "mgmt" on kh and "wiki-mgmt" on wiki: one person waiting for each.
    assert result["parked"] == 2
    groups = upstream_mcp_runtime.all_group_audiences()
    assert [(p.group, p.audience, p.emails) for p in store.pending(groups)] == [
        ("mgmt@co.example", "mgmt", ("ann@x.example",)),
        ("mgmt@co.example", "wiki-mgmt", ("ann@x.example",)),
    ]


async def test_a_removal_applies_at_once(audit_dir, configured, store):
    await _call({"everyone@co.example": ["a@x.example", "b@x.example"]})
    result = await _call({"everyone@co.example": ["a@x.example"]})
    assert (result["status"], result["added"], result["removed"]) == ("applied", 0, 1)
    assert store.members_of("b@x.example") == frozenset()


async def test_a_snapshot_over_the_cap_is_rejected_and_audited(audit_dir, configured, store, caplog):
    await _call({"everyone@co.example": ["a@x.example"]})  # the bootstrap snapshot is exempt
    emails = [f"p{i}@x.example" for i in range(4)]  # the cap is 3
    with caplog.at_level("WARNING"):
        result = await _call({"everyone@co.example": ["a@x.example", *emails]})
    assert result["status"] == "rejected" and result["reason"] == "too_many_additions"
    assert result["added"] == 0
    assert store.members_of("p0@x.example") == frozenset()
    assert _entries(audit_dir)[-1]["decision"] == "group_membership_rejected"
    assert "Group membership snapshot rejected: too_many_additions" in caplog.text


async def test_an_unmapped_group_is_reported_as_ignored(audit_dir, configured, store):
    result = await _call({"everyone@co.example": ["a@x.example"], "other@co.example": ["z@x.example"]})
    assert result["ignored_groups"] == ["other@co.example"]
    assert store.members_of("z@x.example") == frozenset()


async def test_audit_summaries_hold_no_email_address(audit_dir, configured):
    await _call({"everyone@co.example": ["secret.person@x.example"], "mgmt@co.example": ["m@x.example"]})
    await _call({"everyone@co.example": ["secret.person@x.example", "m2@x.example"] * 3})
    text = (audit_dir / f"{current_week()}.jsonl").read_text(encoding="utf-8")
    assert "@x.example" not in text


async def test_every_tool_leaves_an_audit_trail(tmp_path, configured, monkeypatch):
    connector = GroupMembershipConnector()
    with scoped_tokens.token_scope(_SCOPE):
        await assert_all_tools_leave_an_audit_trail(
            connector, connector_module, monkeypatch, tmp_path / "trail",
            arg_overrides={TOOL_NAME: {"groups": {}}},
        )
