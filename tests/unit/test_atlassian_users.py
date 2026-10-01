"""Tests for privacyfence.atlassian_users: user parsing, the bulk/search HTTP helpers, the
``@[Name](accountId)`` mention markup helpers, and AtlassianUserDirectory.

Invariant: a read never costs more than one bulk call per 100 unseen ids, and an unresolvable id
is not retried within an hour.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

from privacyfence import atlassian_users as au
from privacyfence.atlassian_users import (
    AtlassianUser,
    AtlassianUserDirectory,
    AtlassianUsersError,
)

pytestmark = pytest.mark.unit

ID_A = "5b10ac8d82e05b22cc7d4ef5"
ID_B = "712020:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLOUD = "cloud-1"


def _response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    return resp


def _session(*payloads):
    session = MagicMock()
    session.get.side_effect = [_response(p) for p in payloads]
    return session


def _user(account_id: str, name: str = "Jane Doe") -> AtlassianUser:
    return AtlassianUser(account_id=account_id, display_name=name)


def _ids(n: int) -> list[str]:
    return [f"acct-{i:08d}" for i in range(n)]


class TestParseUser:
    def test_full_record(self):
        user = au.parse_user({"accountId": ID_A, "displayName": "Jane", "active": False,
                              "accountType": "app"})
        assert user == AtlassianUser(ID_A, "Jane", False, "app")

    def test_defaults(self):
        user = au.parse_user({"accountId": ID_A})
        assert user == AtlassianUser(ID_A, "", True, "")

    def test_missing_account_id(self):
        assert au.parse_user({"displayName": "Jane"}) is None

    def test_email_not_kept(self):
        user = au.parse_user({"accountId": ID_A, "displayName": "J", "emailAddress": "j@x.com"})
        assert not hasattr(user, "email_address")
        assert "j@x.com" not in repr(user)


CUSTOMER_RAW = {"accountId": ID_A, "displayName": "jo.doe+x@Example.co.uk", "accountType": "customer"}
MASKED = f"Customer account {ID_A[-4:]}"


class TestEmailMasking:
    def test_parse_user_masks_customer_email(self):
        assert au.parse_user(CUSTOMER_RAW).display_name == MASKED

    def test_empty_id_falls_back_to_unknown_user(self):
        assert au.mask_emails("jo@example.com", "") == "unknown user"

    def test_masks_substring(self):
        user = au.parse_user({"accountId": ID_A, "displayName": "Jo (o'neil@x.com) Doe"})
        assert user.display_name == f"Jo ({MASKED}) Doe"

    @pytest.mark.parametrize("name", ["Jane Doe", "Jane O'Neil", "Jane @ Acme", "@jane", "a@b"])
    def test_ordinary_names_untouched(self, name):
        assert au.parse_user({"accountId": ID_A, "displayName": name}).display_name == name

    def test_search_users_masks(self):
        users = au.search_users(_session([CUSTOMER_RAW]), CLOUD, "jo", 5)
        assert [u.display_name for u in users] == [MASKED]

    def test_fetch_users_bulk_masks(self):
        users = au.fetch_users_bulk(_session({"values": [CUSTOMER_RAW], "isLast": True}), CLOUD, [ID_A])
        assert [u.display_name for u in users] == [MASKED]

    def test_cache_file_has_no_address(self, tmp_path: Path):
        path = tmp_path / "users.json"
        names = AtlassianUserDirectory(str(path), CLOUD).resolve(
            [ID_A], MagicMock(return_value=[au.parse_user(CUSTOMER_RAW)]))
        assert names == {ID_A: MASKED}
        assert "@" not in path.read_text()

    def test_stale_cache_masked_on_load(self, tmp_path: Path):
        path = tmp_path / "users.json"
        path.write_text(json.dumps({"cloud_id": CLOUD, "users": {ID_A: {
            "account_id": ID_A, "display_name": "jo@example.com", "active": True,
            "account_type": "customer", "fetched_at": "2026-01-01T00:00:00+00:00"}}}))
        with freeze_time("2026-01-02 00:00:00"):
            names = AtlassianUserDirectory(str(path), CLOUD).resolve([ID_A], MagicMock())
        assert names == {ID_A: MASKED}


class TestFetchUsersBulk:
    def test_params_and_parse(self):
        session = _session({"values": [{"accountId": ID_A, "displayName": "A"}], "isLast": True})
        users = au.fetch_users_bulk(session, CLOUD, [ID_A, ID_B])
        assert [u.account_id for u in users] == [ID_A]
        args, kwargs = session.get.call_args
        assert args[0] == "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/user/bulk"
        assert kwargs["params"] == [("accountId", ID_A), ("accountId", ID_B),
                                    ("maxResults", 100), ("startAt", 0)]
        assert kwargs["timeout"] == 30

    def test_follows_pagination(self):
        session = _session(
            {"values": [{"accountId": ID_A}], "isLast": False},
            {"values": [{"accountId": ID_B}], "isLast": True},
        )
        users = au.fetch_users_bulk(session, CLOUD, [ID_A, ID_B])
        assert [u.account_id for u in users] == [ID_A, ID_B]
        assert session.get.call_args_list[1].kwargs["params"][-1] == ("startAt", 1)

    def test_stops_after_page_budget(self):
        pages = [{"values": [{"accountId": f"acct-{i:08d}"}], "isLast": False} for i in range(10)]
        session = _session(*pages)
        users = au.fetch_users_bulk(session, CLOUD, [ID_A])
        assert session.get.call_count == au._BULK_PAGE_BUDGET
        assert len(users) == au._BULK_PAGE_BUDGET

    def test_stops_on_empty_page(self):
        session = _session({"values": [], "isLast": False})
        assert au.fetch_users_bulk(session, CLOUD, [ID_A]) == []
        assert session.get.call_count == 1

    def test_http_error_propagates(self):
        session = _session({"values": []})
        session.get.side_effect = None
        resp = _response({})
        resp.raise_for_status.side_effect = RuntimeError("boom")
        session.get.return_value = resp
        with pytest.raises(RuntimeError, match="boom"):
            au.fetch_users_bulk(session, CLOUD, [ID_A])


class TestSearchUsers:
    def test_params_and_parse(self):
        session = _session([{"accountId": ID_A, "displayName": "A"}, {"displayName": "no id"}])
        users = au.search_users(session, CLOUD, "jane", 7)
        assert users == [AtlassianUser(ID_A, "A", True, "")]
        args, kwargs = session.get.call_args
        assert args[0].endswith("/rest/api/3/user/search")
        assert kwargs["params"] == {"query": "jane", "maxResults": 7}
        assert kwargs["timeout"] == 30

    def test_http_error_propagates(self):
        session = MagicMock()
        resp = _response([])
        resp.raise_for_status.side_effect = RuntimeError("nope")
        session.get.return_value = resp
        with pytest.raises(RuntimeError, match="nope"):
            au.search_users(session, CLOUD, "q", 5)


class TestMentionMarkup:
    def test_strips_brackets_and_newlines(self):
        assert au.mention_markup("Ja[n]e\r\n Doe ", ID_A) == f"@[Jane Doe]({ID_A})"

    def test_empty_falls_back(self):
        assert au.mention_markup("[]\n", ID_A) == f"@[unknown user]({ID_A})"

    def test_markup_mention_ids_order_and_dedup(self):
        text = f"hi @[A]({ID_B}) and @[B]({ID_A}) and @[A again]({ID_B})"
        assert au.markup_mention_ids(text) == [ID_B, ID_A]

    def test_display_markup_without_names_uses_label(self):
        assert au.display_markup(f"hi @[Jane]({ID_A})") == "hi @Jane"
        assert au.display_markup(f"hi @[Jane]({ID_A})", {}) == "hi @Jane"

    def test_display_markup_names_win(self):
        assert au.display_markup(f"hi @[Old]({ID_A}) @[Other]({ID_B})", {ID_A: "New"}) == \
            "hi @New @Other"

    def test_quote_in_id_does_not_match(self):
        assert au.MENTION_MARKUP_RE.search('@[x](abc"defghijklmn)') is None

    def test_markup_to_storage_exact(self):
        assert au.markup_to_storage(f"a @[Jane]({ID_A}) b") == \
            f'a <ac:link><ri:user ri:account-id="{ID_A}" /></ac:link> b'

    def test_storage_mention_ids(self):
        html = (f'<ac:link><ri:user ri:account-id="{ID_A}" /></ac:link>'
                f'<ri:user ri:account-id="{ID_B}"></ri:user>'
                f'<ac:link><ri:user ri:account-id="{ID_A}" /></ac:link>')
        assert au.storage_mention_ids(html) == [ID_A, ID_B]

    def test_storage_mentions_to_text_escapes_name(self):
        html = f'x <ac:link><ri:user ri:account-id="{ID_A}" /></ac:link> y'
        assert au.storage_mentions_to_text(html, {ID_A: "<b>x</b>"}) == "x @&lt;b&gt;x&lt;/b&gt; y"

    def test_storage_mentions_to_text_unknown_and_body_variants(self):
        html = (
            f'<ac:link><ri:user ri:account-id="{ID_A}" /><ac:link-body>Jane</ac:link-body></ac:link>'
            f'<ac:link><ri:user ri:account-id="{ID_B}" />'
            f'<ac:plain-text-link-body><![CDATA[Bob]]></ac:plain-text-link-body></ac:link>'
        )
        assert au.storage_mentions_to_text(html, {ID_A: "Jane Doe"}) == "@Jane Doe@unknown user"

    def test_storage_mentions_to_text_non_self_closing(self):
        html = f'<ac:link><ri:user ri:account-id="{ID_A}"></ri:user></ac:link>'
        assert au.storage_mentions_to_text(html, {ID_A: "Jane"}) == "@Jane"

    @pytest.mark.parametrize("attr", [
        f'ri:account-id="{ID_A}"',
        f"ri:account-id='{ID_A}'",
        f'ri:account-id = "{ID_A}"',
        f"ri:account-id\n=\t'{ID_A}'",
    ])
    @pytest.mark.parametrize("shape", [
        '<ac:link><ri:user {a} /></ac:link>',
        '<ac:link><ri:user {a}></ri:user></ac:link>',
        '<ac:link><ri:user ri:x="1" {a} ri:y="2" /></ac:link>',
        '<ac:link><ri:user {a} /><ac:link-body>Jane</ac:link-body></ac:link>',
        '<ac:link><ri:user {a} /><ac:plain-text-link-body><![CDATA[Jane]]></ac:plain-text-link-body></ac:link>',
    ])
    def test_quote_and_whitespace_forms(self, attr, shape):
        html = f"x {shape.format(a=attr)} y"
        assert au.storage_mention_ids(html) == [ID_A]
        assert au.storage_mentions_to_text(html, {ID_A: "Jane Doe"}) == "x @Jane Doe y"


    REAL_ID = "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077"

    def test_gt_inside_earlier_quoted_attribute(self):
        html = (f'<ac:link><ri:user ri:local-id="a>b" ri:account-id="{self.REAL_ID}" />'
                '<ac:plain-text-link-body><![CDATA[Bob]]></ac:plain-text-link-body></ac:link>')
        assert au.storage_mention_ids(html) == [self.REAL_ID]
        assert au.storage_mentions_to_text(html, {self.REAL_ID: "Bob Real"}) == "@Bob Real"

    def test_entity_encoded_account_id_is_decoded(self):
        html = '<ac:link><ri:user ri:account-id="557058&#58;f58131cb-b67d-43c7-b30d-6b58d40bd077" /></ac:link>'
        assert au.storage_mention_ids(html) == [self.REAL_ID]
        assert au.storage_mentions_to_text(html, {self.REAL_ID: "Bob Real"}) == "@Bob Real"

    @pytest.mark.parametrize("attr", ['ri:userkey="8a7f"', "ri:username='bob'", ""])
    def test_legacy_or_idless_user_is_unrecognised(self, attr):
        html = f"x <ac:link><ri:user {attr} /></ac:link> y"
        assert au.storage_mention_ids(html) == []
        assert au.storage_unrecognised_user_mentions(html) == 1
        assert au.storage_mentions_to_text(html, {}) == "x @unknown user y"

    def test_unrecognised_count_ignores_recognised(self):
        html = f'<ri:user ri:account-id="{ID_A}"/><ri:user ri:userkey="k"/><ri:user ri:username="u"/>'
        assert au.storage_unrecognised_user_mentions(html) == 2

    def test_link_body_cannot_swallow_content_after_the_link(self):
        html = (f'<ac:link><ri:user ri:account-id="{ID_A}"/><ac:link-body>x</ac:link-body>JUNK</ac:link>'
                '<p>hidden</p><ac:link><ri:page ri:content-title="t"/><ac:link-body>y</ac:link-body></ac:link>')
        assert au.storage_mentions_to_text(html, {ID_A: "Jane"}) == (
            '@Jane<p>hidden</p><ac:link><ri:page ri:content-title="t"/>'
            '<ac:link-body>y</ac:link-body></ac:link>')

    def test_script_element_does_not_hide_later_mentions(self):
        html = f'<script>x</script><ac:link><ri:user ri:account-id="{ID_A}"/></ac:link>'
        assert au.storage_mention_ids(html) == [ID_A]

    @pytest.mark.parametrize("tag", [
        "plaintext", "script", "style", "textarea", "title", "xmp", "iframe", "noscript",
        "noembed", "noframes",
    ])
    def test_raw_text_elements_do_not_hide_later_mentions(self, tag):
        html = f'<{tag}><ac:link><ri:user ri:account-id="{ID_A}"/></ac:link>'
        assert au.storage_mention_ids(html) == [ID_A]
        assert au.storage_mentions_to_text(html, {ID_A: "Jane"}) == f"<{tag}>@Jane"

    def test_duplicate_account_id_attributes_list_every_value(self):
        html = f'<ac:link><ri:user ri:account-id="{ID_A}" ri:account-id="{ID_B}"/></ac:link>'
        assert au.storage_mention_ids(html) == [ID_A, ID_B]
        assert au.storage_unrecognised_user_mentions(html) == 0
        assert au.storage_mentions_to_text(html, {ID_A: "Jane", ID_B: "Bob"}) == "@unknown user"

    def test_duplicate_account_id_with_blank_value_keeps_the_other(self):
        html = f'<ac:link><ri:user ri:account-id=" " ri:account-id="{ID_A}"/></ac:link>'
        assert au.storage_mention_ids(html) == [ID_A]
        assert au.storage_mentions_to_text(html, {ID_A: "Jane"}) == "@Jane"

    def test_multiline_offsets_and_unclosed_link(self):
        html = f'a\n<ac:link>\n<ri:user ri:account-id="{ID_A}"/>\n</ac:link>\nb <ac:link><ri:user ri:account-id="{ID_B}"/>'
        assert au.storage_mentions_to_text(html, {ID_A: "Jane"}).startswith("a\n@Jane\nb <ac:link>")
        assert au.storage_mention_ids(html) == [ID_A, ID_B]


class TestDirectoryConcurrency:
    def test_blocked_fetch_does_not_block_cached_resolve(self):
        directory = AtlassianUserDirectory()
        directory.remember([_user(ID_A, "Cached")])
        entered = threading.Event()
        release = threading.Event()

        def slow_fetch(ids):
            entered.set()
            release.wait(5)
            return [_user(ID_B, "Slow")]

        worker = threading.Thread(target=directory.resolve, args=([ID_B], slow_fetch))
        worker.start()
        assert entered.wait(5)
        result: dict = {}
        reader = threading.Thread(
            target=lambda: result.update(directory.resolve([ID_A], MagicMock()))
        )
        reader.start()
        reader.join(5)
        assert not reader.is_alive()
        assert result == {ID_A: "Cached"}
        release.set()
        worker.join(5)

    def test_second_refresh_in_progress_raises(self):
        directory = AtlassianUserDirectory()
        directory.remember([_user(ID_A)])
        entered = threading.Event()
        release = threading.Event()

        def slow_fetch(ids):
            entered.set()
            release.wait(5)
            return [_user(ID_A)]

        worker = threading.Thread(target=directory.refresh, args=(slow_fetch,))
        worker.start()
        assert entered.wait(5)
        with pytest.raises(AtlassianUsersError, match="refresh already in progress"):
            directory.refresh(slow_fetch)
        release.set()
        worker.join(5)
        assert directory.refresh(lambda ids: [_user(ID_A)]) == 1


class TestDirectoryResolve:
    def test_first_resolve_fetches_valid_deduped_ids_once(self):
        directory = AtlassianUserDirectory()
        fetch = MagicMock(return_value=[_user(ID_A, "A")])
        result = directory.resolve([ID_A, ID_A, "", "a b", "x" * 200, ID_A + "\n"], fetch)
        assert result == {ID_A: "A"}
        fetch.assert_called_once_with([ID_A])

    def test_cached_within_ttl_then_refetched_after(self):
        directory = AtlassianUserDirectory()
        fetch = MagicMock(return_value=[_user(ID_A, "A")])
        with freeze_time("2026-01-01 00:00:00") as clock:
            directory.resolve([ID_A], fetch)
            clock.tick(timedelta(days=6))
            directory.resolve([ID_A], fetch)
            assert fetch.call_count == 1
            clock.tick(timedelta(days=2))
            directory.resolve([ID_A], fetch)
            assert fetch.call_count == 2

    def test_chunks_of_100(self):
        directory = AtlassianUserDirectory()
        ids = _ids(250)
        fetch = MagicMock(side_effect=lambda chunk: [_user(i) for i in chunk])
        result = directory.resolve(ids, fetch)
        assert [len(c.args[0]) for c in fetch.call_args_list] == [100, 100, 50]
        assert len(result) == 250

    def test_missing_id_is_negative_cached_for_an_hour(self):
        directory = AtlassianUserDirectory()
        fetch = MagicMock(return_value=[])
        with freeze_time("2026-01-01 00:00:00") as clock:
            assert directory.resolve([ID_A], fetch) == {}
            clock.tick(timedelta(minutes=30))
            directory.resolve([ID_A], fetch)
            assert fetch.call_count == 1
            clock.tick(timedelta(minutes=31))
            directory.resolve([ID_A], fetch)
            assert fetch.call_count == 2

    def test_failure_returns_empty_logs_and_cools_down(self, caplog):
        directory = AtlassianUserDirectory()
        fetch = MagicMock(side_effect=RuntimeError("down"))
        with freeze_time("2026-01-01 00:00:00") as clock, caplog.at_level(logging.WARNING):
            assert directory.resolve([ID_A], fetch) == {}
            assert "non-fatal" in caplog.text
            clock.tick(timedelta(minutes=4))
            directory.resolve([ID_A], fetch)
            assert fetch.call_count == 1
            clock.tick(timedelta(minutes=2))
            directory.resolve([ID_A], fetch)
            assert fetch.call_count == 2

    def test_stale_name_returned_when_refetch_fails(self):
        directory = AtlassianUserDirectory()
        with freeze_time("2026-01-01 00:00:00") as clock:
            directory.resolve([ID_A], MagicMock(return_value=[_user(ID_A, "Old")]))
            clock.tick(timedelta(days=8))
            result = directory.resolve([ID_A], MagicMock(side_effect=RuntimeError("down")))
        assert result == {ID_A: "Old"}

    def test_failure_stops_remaining_chunks(self):
        directory = AtlassianUserDirectory()
        fetch = MagicMock(side_effect=RuntimeError("down"))
        directory.resolve(_ids(150), fetch)
        assert fetch.call_count == 1

    def test_no_ids_needing_fetch_makes_no_call(self):
        directory = AtlassianUserDirectory()
        fetch = MagicMock()
        assert directory.resolve([], fetch) == {}
        fetch.assert_not_called()


class TestBlankNames:
    @pytest.mark.parametrize("name", ["", " ", " \t\n "])
    def test_blank_name_is_unresolved_and_negative_cached(self, name: str):
        directory = AtlassianUserDirectory()
        fetch = MagicMock(return_value=[_user(ID_A, name)])
        assert directory.resolve([ID_A], fetch) == {}
        assert directory.resolve([ID_A], fetch) == {}
        fetch.assert_called_once_with([ID_A])

    def test_blank_name_not_persisted(self, tmp_path: Path):
        path = tmp_path / "users.json"
        directory = AtlassianUserDirectory(str(path), CLOUD)
        directory.resolve([ID_A, ID_B], MagicMock(return_value=[_user(ID_A, " "), _user(ID_B, "Bo")]))
        assert list(json.loads(path.read_text())["users"]) == [ID_B]

    def test_blank_name_in_cache_file_is_ignored(self, tmp_path: Path):
        path = tmp_path / "users.json"
        stamp = "2026-01-01T00:00:00+00:00"
        path.write_text(json.dumps({"cloud_id": CLOUD, "users": {
            ID_A: {"account_id": ID_A, "display_name": "  ", "fetched_at": stamp}}}))
        fetch = MagicMock(return_value=[])
        with freeze_time("2026-01-02"):
            assert AtlassianUserDirectory(str(path), CLOUD).resolve([ID_A], fetch) == {}
        fetch.assert_called_once_with([ID_A])

    def test_remember_skips_blank_names(self):
        directory = AtlassianUserDirectory()
        directory.remember([_user(ID_A, ""), _user(ID_B, "Bo")])
        assert directory.resolve([ID_A, ID_B], MagicMock(return_value=[])) == {ID_B: "Bo"}

    def test_refresh_drops_blank_names(self):
        directory = AtlassianUserDirectory()
        directory.remember([_user(ID_A, "Al"), _user(ID_B, "Bo")])
        assert directory.refresh(MagicMock(return_value=[_user(ID_A, " "), _user(ID_B, "Bo")])) == 1
        assert directory.resolve([ID_A], MagicMock(return_value=[])) == {}

    def test_redact_query_hides_search_text_in_all_encodings(self):
        exc = RuntimeError("400 for url: x?query=jane%40example.com&q=jane@example.com+jane+doe")
        for query in ("jane@example.com", "jane doe"):
            assert query not in au.redact_query(exc, query)
        assert "jane%40example.com" not in au.redact_query(exc, "jane@example.com")


class TestMalformedCacheFile:
    STAMP = "2026-01-01T00:00:00+00:00"

    def _resolve(self, tmp_path: Path, payload) -> dict:
        path = tmp_path / "users.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        fetch = MagicMock(return_value=[_user(ID_A, "Fresh")])
        with freeze_time("2026-01-01 01:00:00"):
            return AtlassianUserDirectory(str(path), CLOUD).resolve([ID_A], fetch)

    @pytest.mark.parametrize("users", [["x"], "x", 7, True])
    def test_non_dict_users(self, tmp_path: Path, users):
        assert self._resolve(tmp_path, {"cloud_id": CLOUD, "users": users}) == {ID_A: "Fresh"}

    @pytest.mark.parametrize("payload", [["x"], "{", 3, None])
    def test_non_dict_document(self, tmp_path: Path, payload):
        assert self._resolve(tmp_path, payload) == {ID_A: "Fresh"}

    @pytest.mark.parametrize("entry", [
        "x", None, 5, ["a"], {},
        {"account_id": ID_A, "display_name": "Old"},
        {"account_id": ID_A, "display_name": "Old", "fetched_at": 12},
        {"account_id": ID_A, "display_name": "Old", "fetched_at": "garbage"},
        {"account_id": ID_A, "display_name": 5, "fetched_at": STAMP},
        {"account_id": ["l"], "display_name": "Old", "fetched_at": STAMP},
        {"account_id": ID_A, "display_name": "Old", "fetched_at": STAMP, "active": {}, "account_type": []},
    ])
    def test_bad_entries(self, tmp_path: Path, entry):
        # Entries that load give "Old"; the rest are skipped and re-fetched. None may raise.
        result = self._resolve(tmp_path, {"cloud_id": CLOUD, "users": {ID_A: entry}})
        assert result in ({ID_A: "Fresh"}, {ID_A: "Old"})

    def test_non_str_key_skipped(self, tmp_path: Path, monkeypatch):
        path = tmp_path / "users.json"
        path.write_text("{}")  # JSON keys are always str, so feed the loader a decoded document
        monkeypatch.setattr(au.json, "load", lambda fh: {
            "cloud_id": CLOUD,
            "users": {5: {"account_id": ID_A, "display_name": "Old", "fetched_at": self.STAMP}},
        })
        fetch = MagicMock(return_value=[_user(ID_A, "Fresh")])
        with freeze_time("2026-01-01 01:00:00"):
            assert AtlassianUserDirectory(str(path), CLOUD).resolve([ID_A], fetch) == {ID_A: "Fresh"}


class TestDirectoryPersistence:
    def test_writes_file_in_documented_format(self, tmp_path: Path):
        path = tmp_path / "users.json"
        directory = AtlassianUserDirectory(str(path), CLOUD)
        with freeze_time("2026-01-01 00:00:00"):
            directory.resolve([ID_A], MagicMock(return_value=[
                AtlassianUser(ID_A, "Jane", False, "app")]))
        data = json.loads(path.read_text())
        assert data["cloud_id"] == CLOUD
        assert data["users"][ID_A] == {
            "account_id": ID_A, "display_name": "Jane", "active": False,
            "account_type": "app", "fetched_at": "2026-01-01T00:00:00+00:00",
        }

    def test_new_directory_resolves_from_disk(self, tmp_path: Path):
        path = tmp_path / "users.json"
        AtlassianUserDirectory(str(path), CLOUD).resolve(
            [ID_A], MagicMock(return_value=[_user(ID_A, "Jane")]))
        fetch = MagicMock()
        assert AtlassianUserDirectory(str(path), CLOUD).resolve([ID_A], fetch) == {ID_A: "Jane"}
        fetch.assert_not_called()

    def test_different_cloud_id_ignores_file(self, tmp_path: Path):
        path = tmp_path / "users.json"
        AtlassianUserDirectory(str(path), CLOUD).resolve(
            [ID_A], MagicMock(return_value=[_user(ID_A, "Jane")]))
        fetch = MagicMock(return_value=[_user(ID_A, "Other")])
        assert AtlassianUserDirectory(str(path), "cloud-2").resolve([ID_A], fetch) == \
            {ID_A: "Other"}
        fetch.assert_called_once()

    @pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
    def test_corrupt_or_non_dict_ignored(self, tmp_path: Path, content: str):
        path = tmp_path / "users.json"
        path.write_text(content)
        fetch = MagicMock(return_value=[_user(ID_A, "Jane")])
        assert AtlassianUserDirectory(str(path), CLOUD).resolve([ID_A], fetch) == {ID_A: "Jane"}

    def test_bad_entries_skipped_and_naive_timestamp_accepted(self, tmp_path: Path):
        path = tmp_path / "users.json"
        good = {"account_id": ID_A, "display_name": "Jane", "fetched_at": "2026-01-01T00:00:00"}
        path.write_text(json.dumps({"cloud_id": CLOUD, "users": {
            ID_A: good, ID_B: {"account_id": ID_B}, "bad": "not-a-dict"}}))
        fetch = MagicMock(return_value=[_user(ID_B, "Bob")])
        with freeze_time("2026-01-02 00:00:00"):
            result = AtlassianUserDirectory(str(path), CLOUD).resolve([ID_A, ID_B], fetch)
        assert result == {ID_A: "Jane", ID_B: "Bob"}
        fetch.assert_called_once_with([ID_B])

    def test_empty_users_key_loads_nothing(self, tmp_path: Path):
        path = tmp_path / "users.json"
        path.write_text(json.dumps({"cloud_id": CLOUD, "users": None}))
        assert AtlassianUserDirectory(str(path), CLOUD).refresh(MagicMock()) == 0

    def test_no_cache_file_writes_nothing(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        directory = AtlassianUserDirectory("", CLOUD)
        directory.resolve([ID_A], MagicMock(return_value=[_user(ID_A)]))
        assert list(tmp_path.iterdir()) == []

    def test_save_error_is_logged_not_raised(self, tmp_path: Path, monkeypatch, caplog):
        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(au, "atomic_write_json", boom)
        directory = AtlassianUserDirectory(str(tmp_path / "u.json"), CLOUD)
        with caplog.at_level(logging.WARNING):
            assert directory.resolve([ID_A], MagicMock(return_value=[_user(ID_A, "J")])) == \
                {ID_A: "J"}
        assert "Could not save" in caplog.text


class TestDirectoryRememberAndRefresh:
    def test_remember_makes_resolve_fetch_free_and_clears_negative(self):
        directory = AtlassianUserDirectory()
        with freeze_time("2026-01-01 00:00:00"):
            directory.resolve([ID_A], MagicMock(return_value=[]))
            directory.remember([_user(ID_A, "Jane")])
            fetch = MagicMock()
            assert directory.resolve([ID_A], fetch) == {ID_A: "Jane"}
        fetch.assert_not_called()

    def test_refresh_refetches_all_and_drops_missing(self):
        directory = AtlassianUserDirectory()
        directory.remember([_user(ID_A, "A"), _user(ID_B, "B")])
        fetch = MagicMock(return_value=[_user(ID_A, "A2")])
        assert directory.refresh(fetch) == 1
        fetch.assert_called_once()
        assert sorted(fetch.call_args.args[0]) == sorted([ID_A, ID_B])
        assert directory.resolve([ID_A, ID_B], MagicMock()) == {ID_A: "A2"}

    def test_refresh_chunks(self):
        directory = AtlassianUserDirectory()
        directory.remember([_user(i) for i in _ids(150)])
        fetch = MagicMock(side_effect=lambda chunk: [_user(i) for i in chunk])
        assert directory.refresh(fetch) == 150
        assert fetch.call_count == 2

    def test_refresh_empty_cache_returns_zero_without_fetch(self):
        fetch = MagicMock()
        assert AtlassianUserDirectory().refresh(fetch) == 0
        fetch.assert_not_called()

    def test_refresh_failure_raises_and_leaves_cache(self):
        directory = AtlassianUserDirectory()
        directory.remember([_user(ID_A, "A")])
        with pytest.raises(AtlassianUsersError, match="refresh failed: down"):
            directory.refresh(MagicMock(side_effect=RuntimeError("down")))
        fetch = MagicMock()
        assert directory.resolve([ID_A], fetch) == {ID_A: "A"}
        fetch.assert_not_called()
        assert directory.refresh(lambda ids: [_user(ID_A, "A")]) == 1
