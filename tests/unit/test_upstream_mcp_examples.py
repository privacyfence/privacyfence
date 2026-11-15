"""The knowledge-hub example files in contrib/examples validate and give the documented audiences."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from privacyfence import upstream_mcp_config
from privacyfence.group_membership import StaticFileResolver
from privacyfence.upstream_mcp_config import GroupMapStore

EXAMPLES = Path(__file__).parents[2] / "contrib" / "examples" / "knowledge-hub"


def test_operator_file_validates():
    data = json.loads((EXAMPLES / "upstream-mcp.json").read_text(encoding="utf-8"))
    (server,) = upstream_mcp_config.parse_upstreams(data, source="example")
    claim, membership = upstream_mcp_config.parse_settings(data, source="example")
    assert server.id == "knowledgehub"
    assert server.audience == "knowledge-hub"
    assert server.identity_claim == "required"
    assert server.claim_lifetime_seconds == 60
    assert server.allowed_principal_types == ("claude_client", "n8n_workflow")
    assert {tool.name for tool in server.tools} == {"search", "get_doc"}
    assert claim.key_overlap_hours == 24
    assert membership.source == "snapshot"
    assert membership.max_additions == 20


def test_groups_and_static_membership_give_the_documented_audiences(tmp_path):
    admin = tmp_path / "admin_config.json"
    shutil.copy(EXAMPLES / "admin_config.upstream_mcp.json", admin)
    groups = GroupMapStore(admin, frozenset({"knowledgehub"})).group_audiences("knowledgehub")
    assert set(groups) == {"everyone@company", "mgmt@company", "hr@company", "contracts@company"}

    static = tmp_path / "group_membership_static.json"
    shutil.copy(EXAMPLES / "group_membership_static.json", static)
    resolver = StaticFileResolver(static)
    assert resolver.audiences_for("alice@example.org", groups) == {"all", "mgmt"}
    assert resolver.audiences_for("bob@example.org", groups) == {"all"}
    assert resolver.audiences_for("", groups) == frozenset()
