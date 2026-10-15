"""The consent page is the only step that tells an org member which client is asking for access
as them (ADR 0177), so what it shows, and what it refuses to show raw, is pinned here."""
from __future__ import annotations

from html.parser import HTMLParser

from privacyfence import paths
from privacyfence.principal import Principal
from privacyfence.web import oauth_consent as oc


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _tags(html: str) -> list[tuple[str, dict]]:
    parser = _Tags()
    parser.feed(html)
    return parser.tags


def _view(**overrides) -> oc.ConsentView:
    values = {"client_name": "Claude", "redirect_host": "claude.ai", "principal_label": "alice@acme.com", "csrf": "tok"}
    return oc.ConsentView(**{**values, **overrides})


class TestConsentStore:
    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        store = oc.ConsentStore()
        alice = Principal(id="alice")
        assert not store.has(alice, "c1", "https://a.example/cb")

        store.record(alice, "c1", "https://a.example/cb")

        assert store.has(alice, "c1", "https://a.example/cb")
        assert not store.has(alice, "c1", "https://b.example/cb")
        assert not store.has(alice, "c2", "https://a.example/cb")
        assert not store.has(Principal(id="bob"), "c1", "https://a.example/cb")
        assert oc.ConsentStore().has(alice, "c1", "https://a.example/cb")  # on disk, not just in memory

    def test_recording_a_second_redirect_keeps_the_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        store = oc.ConsentStore()
        alice = Principal(id="alice")
        store.record(alice, "c1", "https://a.example/cb")
        store.record(alice, "c1", "https://b.example/cb")
        assert store.has(alice, "c1", "https://a.example/cb")
        assert store.has(alice, "c1", "https://b.example/cb")

    def test_malformed_file_reads_as_no_consent(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        alice = Principal(id="alice")
        for content in ("{not json", "[]", '{"clients": []}', '{"clients": {"c1": "x"}}'):
            (paths.user_dir(alice) / oc.CONSENTS_FILE_NAME).write_text(content, encoding="utf-8")
            assert not oc.ConsentStore().has(alice, "c1", "https://a.example/cb"), content

        assert "treating it as no consent given" in caplog.text

    def test_recording_over_a_malformed_file_replaces_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        alice = Principal(id="alice")
        (paths.user_dir(alice) / oc.CONSENTS_FILE_NAME).write_text("{not json", encoding="utf-8")
        store = oc.ConsentStore()
        store.record(alice, "c1", "https://a.example/cb")
        assert store.has(alice, "c1", "https://a.example/cb")


class TestConsentPage:
    def test_shows_the_client_the_person_and_where_they_return_to(self):
        html = oc.render_consent_page(_view(), consent_id="id", nonce="n")
        assert "Allow Claude to use PrivacyFence as alice@acme.com?" in html
        assert "You'll return to claude.ai." in html
        assert "<title>Allow access?</title>" in html
        form = [attrs for tag, attrs in _tags(html) if tag == "form"]
        assert form == [{"method": "post"}]
        buttons = [attrs for tag, attrs in _tags(html) if tag == "button"]
        assert [(b["name"], b["value"]) for b in buttons] == [("decision", "allow"), ("decision", "deny")]
        assert {"type": "hidden", "name": "csrf", "value": "tok"} in [a for t, a in _tags(html) if t == "input"]

    def test_a_hostile_client_name_is_escaped(self):
        html = oc.render_consent_page(
            _view(client_name="</title><script>x</script>"), consent_id="id", nonce="n",
        )
        assert "<script>x</script>" not in html
        assert "&lt;/title&gt;&lt;script&gt;x&lt;/script&gt;" in html
        assert [t for t, _ in _tags(html) if t == "script"] == []

    def test_every_interpolated_value_is_escaped(self):
        html = oc.render_consent_page(
            _view(redirect_host='a"><b>', principal_label="<i>", csrf='"><u>'), consent_id="id", nonce="n",
        )
        assert not any(t in ("b", "i", "u") for t, _ in _tags(html))

    def test_the_unnamed_label_is_the_providers_to_choose(self):
        assert oc.UNNAMED_CLIENT_LABEL == "An unnamed AI client"
        html = oc.render_consent_page(_view(client_name=oc.UNNAMED_CLIENT_LABEL), consent_id="id", nonce="n")
        assert "Allow An unnamed AI client to use PrivacyFence" in html


class TestContinuePage:
    def test_links_and_refreshes_to_the_same_escaped_url(self):
        url = "https://claude.ai/cb?code=a&state=b\"><script>x</script>"
        html = oc.render_continue_page(url, redirect_host="claude.ai", nonce="n")
        tags = _tags(html)
        meta = [a for t, a in tags if t == "meta" and a.get("http-equiv") == "refresh"]
        link = [a for t, a in tags if t == "a"]
        assert meta == [{"http-equiv": "refresh", "content": f"0;url={url}"}]
        assert link == [{"href": url}]
        assert "Returning to claude.ai…" in html
        assert "<title>Returning to your AI client</title>" in html
        assert [t for t, _ in tags if t == "script"] == []
