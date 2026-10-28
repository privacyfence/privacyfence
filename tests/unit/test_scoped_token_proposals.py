"""scoped_token_proposals: the registry, narrowing, the session bundle, review data and audit."""
from __future__ import annotations

import json

import pytest

from privacyfence import audit_log, scoped_token_proposals as stp
from privacyfence.agent_identity import AgentIdentity, AgentSource
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.principal import Principal, principal_scope

ALICE = Principal(id="alice", email="alice@example.com")
BOB = Principal(id="bob")
AGENT = AgentIdentity(id="claude-code", name="Claude Code", version="1", source=AgentSource.NONE)
NOW = 1000.0


def _request(**over):
    base = {
        "label": "Routine", "days": 30,
        "rules": [{"kind": "new", "group": "drive.folder", "value": "F1", "verbs": ["download", "read"]}],
        "tools": ["drive_download_file", "gmail_list_messages"],
        "allow_personal_data": False, "allow_personal_data_reads": False,
    }
    base.update(over)
    return base


def _proposal(principal=ALICE, now=NOW, **over):
    return stp.new_proposal(
        principal=principal, request=over.pop("request", _request()), source="bundle", agent=AGENT,
        reason="because", now=now, **over,
    )


class TestRegistry:
    def test_get_returns_a_live_proposal(self):
        registry, p = stp.ProposalRegistry(), _proposal()
        registry.add(p, now=NOW)
        assert registry.get("alice", p.id, now=NOW + 1) is p

    def test_get_refuses_another_principal(self):
        registry, p = stp.ProposalRegistry(), _proposal()
        registry.add(p, now=NOW)
        assert registry.get("bob", p.id, now=NOW) is None
        assert registry.take("bob", p.id, now=NOW) is None
        assert registry.get("alice", "stp-unknown", now=NOW) is None

    def test_take_is_single_use(self):
        registry, p = stp.ProposalRegistry(), _proposal()
        registry.add(p, now=NOW)
        assert registry.take("alice", p.id, now=NOW) is p
        assert registry.take("alice", p.id, now=NOW) is None
        assert registry.get("alice", p.id, now=NOW) is None

    def test_restore_puts_it_back(self):
        registry, p = stp.ProposalRegistry(), _proposal()
        registry.add(p, now=NOW)
        registry.take("alice", p.id, now=NOW)
        registry.restore(p, now=NOW + 1)
        assert registry.get("alice", p.id, now=NOW + 2) is p

    def test_restore_skips_an_expired_one(self):
        registry, p = stp.ProposalRegistry(), _proposal()
        registry.restore(p, now=p.expires_at)
        assert registry.get("alice", p.id, now=NOW) is None

    def test_expired_proposals_are_swept_and_reported_once(self):
        seen = []
        registry = stp.ProposalRegistry(on_expired=seen.append)
        p = _proposal()
        registry.add(p, now=NOW)
        assert registry.get("alice", p.id, now=p.expires_at) is None
        assert registry.pending_for("alice", now=p.expires_at + 1) == []
        assert seen == [p]

    def test_expiry_without_a_callback_is_silent(self):
        registry, p = stp.ProposalRegistry(), _proposal()
        registry.add(p, now=NOW)
        assert registry.pending_for("alice", now=p.expires_at) == []

    def test_add_refuses_a_sixth_pending(self):
        registry = stp.ProposalRegistry()
        for _ in range(stp.MAX_PENDING_PER_PRINCIPAL):
            registry.add(_proposal(), now=NOW)
        with pytest.raises(stp.ProposalError, match="Too many token proposals"):
            registry.add(_proposal(), now=NOW)
        registry.add(_proposal(principal=BOB), now=NOW)

    def test_pending_for_lists_only_this_principal(self):
        registry = stp.ProposalRegistry()
        mine, theirs = _proposal(now=NOW), _proposal(principal=BOB, now=NOW)
        later = _proposal(now=NOW + 5)
        for p in (later, theirs, mine):
            registry.add(p, now=NOW + 5)
        assert registry.pending_for("alice", now=NOW + 5) == [mine, later]

    def test_ids_are_prefixed_and_unique(self):
        a, b = _proposal(), _proposal()
        assert a.id.startswith(stp.ID_PREFIX) and a.id != b.id
        assert len(a.id) == len(stp.ID_PREFIX) + 32

    def test_reason_is_trimmed_and_ttl_applied(self):
        p = stp.new_proposal(
            principal=ALICE, request=_request(), source="bundle", agent=AGENT, reason="x" * 900, now=NOW,
        )
        assert len(p.reason) == 500 and p.expires_at == NOW + stp.PROPOSAL_TTL_SECONDS

    def test_the_default_registry_is_lazy_and_settable(self, tmp_path):
        _init_audit(tmp_path, ALICE)
        first = stp.get_registry()
        assert stp.get_registry() is first
        p = _proposal()
        first.add(p, now=NOW)
        first.pending_for("alice", now=p.expires_at)
        entries = _entries(tmp_path)
        assert [e["decision"] for e in entries] == ["scoped_token_proposal_expired"]
        assert "Routine" in entries[0]["summary"]
        stp.set_registry(None)
        assert stp.get_registry() is not first


class TestNarrowing:
    def test_identical_request_is_allowed(self):
        assert stp.narrowing_error(_request(), _request()) is None

    def test_dropping_a_rule_tool_or_days_is_allowed(self):
        final = _request(rules=[], tools=["drive_download_file"], days=7)
        assert stp.narrowing_error(_request(), final) is None

    def test_dropping_a_verb_is_allowed(self):
        final = _request(rules=[{"kind": "new", "group": "drive.folder", "value": "F1", "verbs": ["read"]}])
        assert stp.narrowing_error(_request(), final) is None

    def test_adding_a_rule_is_refused(self):
        extra = {"kind": "copy", "rule_id": "r1"}
        final = _request(rules=[*_request()["rules"], extra])
        assert stp.narrowing_error(_request(), final) == stp.WIDENED_MESSAGE

    def test_adding_a_verb_is_refused(self):
        final = _request(rules=[{"kind": "new", "group": "drive.folder", "value": "F1", "verbs": ["read", "write"]}])
        assert stp.narrowing_error(_request(), final) == stp.WIDENED_MESSAGE

    def test_adding_a_tool_is_refused(self):
        final = _request(tools=["drive_download_file", "gmail_list_messages", "gmail_get_message"])
        assert stp.narrowing_error(_request(), final) == stp.WIDENED_MESSAGE

    def test_more_days_is_refused(self):
        assert stp.narrowing_error(_request(), _request(days=31)) == stp.WIDENED_MESSAGE

    def test_turning_on_a_personal_data_flag_is_refused(self):
        for flag in ("allow_personal_data", "allow_personal_data_reads"):
            assert stp.narrowing_error(_request(), _request(**{flag: True})) == stp.WIDENED_MESSAGE
        both = _request(allow_personal_data=True, allow_personal_data_reads=True)
        assert stp.narrowing_error(both, _request()) is None

    def test_a_new_rule_with_another_value_is_refused(self):
        final = _request(rules=[{"kind": "new", "group": "drive.folder", "value": "F2", "verbs": ["read"]}])
        assert stp.narrowing_error(_request(), final) == stp.WIDENED_MESSAGE

    def test_a_new_rule_with_no_verbs_is_refused(self):
        final = _request(rules=[{"kind": "new", "group": "drive.folder", "value": "F1", "verbs": []}])
        assert stp.narrowing_error(_request(), final) == stp.WIDENED_MESSAGE

    def test_a_copy_rule_matches_only_its_own_id(self):
        proposed = _request(rules=[{"kind": "copy", "rule_id": "r1"}])
        assert stp.narrowing_error(proposed, _request(rules=[{"kind": "copy", "rule_id": "r1"}])) is None
        assert stp.narrowing_error(proposed, _request(rules=[{"kind": "copy", "rule_id": "r2"}])) == stp.WIDENED_MESSAGE

    def test_a_duplicate_rule_is_refused(self):
        dup = {"kind": "duplicate", "token_id": "t1", "rule_id": "r1"}
        assert stp.narrowing_error(_request(rules=[dup]), _request(rules=[dup])) == stp.WIDENED_MESSAGE

    def test_the_label_may_change(self):
        assert stp.narrowing_error(_request(), _request(label="Renamed")) is None


class TestBundleFromCalls:
    @pytest.fixture
    def suggestions(self, monkeypatch):
        table = {}
        monkeypatch.setattr(
            "privacyfence.policy.suggest.suggested_rules",
            lambda tool, args, my_email="": table.get((tool, my_email, args.get("k")), []),
        )
        return table

    @staticmethod
    def _s(group, value, verb, **extra):
        return {"group": group, "label": group, "value": value, "value_known": True, "verb": verb,
                "operation": "x", **extra}

    def test_auto_tools_need_no_rule(self, suggestions):
        rules, tools, uncovered = stp.bundle_from_calls([("gmail_list_messages", {}, "")])
        assert (rules, tools, uncovered) == ([], ["gmail_list_messages"], [])

    def test_unknown_tools_are_skipped(self, suggestions):
        assert stp.bundle_from_calls([("not_a_tool", {}, "")]) == ([], [], [])

    def test_calls_on_one_resource_merge_their_verbs(self, suggestions):
        suggestions[("drive_download_file", "", 1)] = [self._s("drive.folder", ["F1"], "download")]
        suggestions[("drive_get_file_content", "", 1)] = [self._s("drive.folder", ["F1"], "read")]
        rules, tools, uncovered = stp.bundle_from_calls([
            ("drive_download_file", {"k": 1}, ""), ("drive_get_file_content", {"k": 1}, ""),
        ])
        assert rules == [{"kind": "new", "group": "drive.folder", "value": "F1", "verbs": ["download", "read"]}]
        assert tools == ["drive_download_file", "drive_get_file_content"] and uncovered == []

    def test_each_call_uses_its_own_my_email(self, suggestions):
        suggestions[("gmail_get_message", "a@x.com", 1)] = [self._s("gmail.sender_domain", ["a.com"], "read")]
        suggestions[("gmail_get_message", "b@x.com", 1)] = [self._s("gmail.sender_domain", ["b.com"], "read")]
        rules, _, _ = stp.bundle_from_calls([
            ("gmail_get_message", {"k": 1}, "a@x.com"), ("gmail_get_message", {"k": 1}, "b@x.com"),
        ])
        assert [r["value"] for r in rules] == ["a.com", "b.com"]

    @pytest.mark.parametrize("suggestion", [
        {"requires_args": {"x": 1}},
        {"value_known": False},
        {"value": None},
    ])
    def test_a_call_without_a_usable_suggestion_is_uncovered(self, suggestions, suggestion):
        suggestions[("gmail_get_message", "", 1)] = [
            {**self._s("gmail.sender_domain", ["a.com"], "read"), **suggestion},
        ]
        assert stp.bundle_from_calls([("gmail_get_message", {"k": 1}, "")]) == ([], ["gmail_get_message"], ["gmail_get_message"])

    def test_a_later_usable_suggestion_is_taken(self, suggestions):
        suggestions[("gmail_get_message", "", 1)] = [
            self._s("gmail.query", None, "read", value_known=False),
            self._s("gmail.sender_domain", ["a.com"], "read"),
        ]
        rules, _, uncovered = stp.bundle_from_calls([("gmail_get_message", {"k": 1}, "")])
        assert rules[0]["group"] == "gmail.sender_domain" and uncovered == []

    def test_output_is_sorted(self, suggestions):
        suggestions[("gmail_get_message", "", 1)] = [self._s("gmail.sender_domain", ["z.com"], "read")]
        suggestions[("drive_download_file", "", 1)] = [self._s("drive.folder", ["F1"], "download")]
        suggestions[("gmail_get_thread", "", 1)] = [self._s("gmail.sender_domain", ["a.com"], "read")]
        rules, tools, _ = stp.bundle_from_calls([
            ("gmail_get_message", {"k": 1}, ""), ("drive_download_file", {"k": 1}, ""),
            ("gmail_get_thread", {"k": 1}, ""), ("gmail_get_thread", {"k": 1}, ""),
        ])
        assert [(r["group"], r["value"]) for r in rules] == [
            ("drive.folder", "F1"), ("gmail.sender_domain", "a.com"), ("gmail.sender_domain", "z.com"),
        ]
        assert tools == sorted(set(tools)) and len(tools) == 3


class TestReviewData:
    @staticmethod
    def _copy_row():
        from privacyfence.policy import catalogue, scope_rows
        rules = catalogue.rules_for_catalogue_entry("drive.folder", ["F9"], catalogue.parse_verbs(["read"]))
        return scope_rows.add_rules([], rules)[0][0]

    def test_new_rule_sentence(self):
        data = stp.review_data(_proposal(), [])
        sentence = data["rules"][0]["sentence"]
        assert "F1" in sentence and "download" in sentence
        assert data["rules"][0]["request"] == _request()["rules"][0]
        assert data["id"].startswith(stp.ID_PREFIX) and data["label"] == "Routine" and data["days"] == 30
        assert data["tools"] == _request()["tools"] and data["reason"] == "because"
        assert data["source"] == "bundle" and data["uncovered_tools"] == []
        assert data["allow_personal_data"] is False and data["allow_personal_data_reads"] is False
        assert data["expires_at"] == NOW + stp.PROPOSAL_TTL_SECONDS

    def test_copy_rule_sentence(self):
        row = self._copy_row()
        p = _proposal(request=_request(rules=[{"kind": "copy", "rule_id": row.id}]), uncovered_tools=("t",))
        data = stp.review_data(p, [row])
        assert "F9" in data["rules"][0]["sentence"] and data["uncovered_tools"] == ["t"]

    def test_missing_copy_rule_says_so(self):
        p = _proposal(request=_request(rules=[{"kind": "copy", "rule_id": "gone"}]))
        assert stp.review_data(p, [])["rules"][0]["sentence"] == "A rule that no longer exists"

    def test_agent_label_is_the_label_text(self):
        from privacyfence import agent_label
        assert stp.review_data(_proposal(), [])["agent_label"] == agent_label.label_for(AGENT).text


def _init_audit(directory, principal):
    with principal_scope(principal):
        init_audit_logger(str(directory))


def _entries(directory):
    path = directory / f"{current_week()}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class TestRecord:
    def test_record_names_the_agent_and_principal(self, tmp_path):
        _init_audit(tmp_path, ALICE)
        p = _proposal()
        stp.record("scoped_token_proposed", p, "Token proposal 'Routine' created")
        (entry,) = _entries(tmp_path)
        assert entry["decision"] == "scoped_token_proposed"
        assert entry["connector"] == "policy" and entry["tool"] == "privacyfence_propose_scoped_token"
        assert entry["request_id"] == p.id and entry["claude_reason"] == "because"
        assert entry["sender"] == "alice@example.com"
        assert entry["agent_id"] == "claude-code" and entry["agent_name"] == "Claude Code"
        assert entry["summary"] == "Token proposal 'Routine' created"

    def test_sender_falls_back_to_the_principal_id(self, tmp_path):
        _init_audit(tmp_path, BOB)
        stp.record("scoped_token_proposal_declined", _proposal(principal=BOB), "declined")
        assert _entries(tmp_path)[0]["sender"] == "bob"

    def test_record_is_not_an_approved_like_decision(self):
        for decision in ("scoped_token_proposed", "scoped_token_proposal_accepted",
                         "scoped_token_proposal_declined", "scoped_token_proposal_expired"):
            assert decision not in audit_log.APPROVED_LIKE_DECISIONS

    def test_a_logging_failure_is_swallowed(self, monkeypatch, caplog):
        def boom():
            raise RuntimeError("disk full")
        monkeypatch.setattr(audit_log, "get_audit_logger", boom)
        stp.record("scoped_token_proposed", _proposal(), "x")
        assert "Audit log write failed" in caplog.text
