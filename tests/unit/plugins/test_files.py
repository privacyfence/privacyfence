"""Unit tests for privacyfence.plugins.files: a plugin tool's file parameter, resolved by the daemon."""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from privacyfence import local_files, paths
from privacyfence.local_files import LocalFileAccessError
from privacyfence.plugins import files
from privacyfence.plugins.constants import FILE_MEDIA_TYPES
from privacyfence.plugins.files import IncomingFile
from privacyfence.plugins.protocol import FileParamSpec
from privacyfence.principal import LOCAL_PRINCIPAL, principal_scope
from privacyfence.upload_staging import get_upload_staging_store

HTML_ONLY = FileParamSpec("html", 1000, ("text/html",))
ANY_TEXT = FileParamSpec("doc", 1000, ("text/html", "text/plain", "application/json"))


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


def _slot(filename: str, data: bytes) -> str:
    store = get_upload_staging_store()
    token = store.create_slot(LOCAL_PRINCIPAL, filename, max_bytes=100_000)
    store.fill(token, LOCAL_PRINCIPAL.id, [data])
    return local_files._encode_token(token)


class TestDeclaredMediaType:
    @pytest.mark.parametrize("name, expected", [
        ("a.html", "text/html"), ("a.htm", "text/html"),
        ("a.txt", "text/plain"), ("a.csv", "text/plain"), ("a.md", "text/plain"),
        ("a.json", "application/json"), ("a.pdf", "application/pdf"),
        ("a.png", "image/png"), ("a.jpg", "image/jpeg"), ("a.jpeg", "image/jpeg"),
        ("a.gif", "image/gif"), ("a.webp", "image/webp"),
        ("a.woff", "font/woff"), ("a.woff2", "font/woff2"), ("a.ttf", "font/ttf"), ("a.otf", "font/otf"),
        ("a.exe", "application/octet-stream"), ("noextension", "application/octet-stream"),
    ])
    def test_map(self, name, expected):
        assert files.declared_media_type(name) == expected

    def test_case_insensitive(self):
        assert files.declared_media_type("PAGE.HTML") == "text/html"
        assert files.declared_media_type("Photo.JpG") == "image/jpeg"

    def test_every_mapped_type_is_a_supported_type(self):
        assert set(files._BY_EXTENSION.values()) <= set(FILE_MEDIA_TYPES)


class TestSniffMediaType:
    @pytest.mark.parametrize("data, expected", [
        (b"\x89PNG\r\n\x1a\nrest", "image/png"),
        (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        (b"GIF87a...", "image/gif"),
        (b"GIF89a...", "image/gif"),
        (b"RIFF\x01\x02\x03\x04WEBPVP8 ", "image/webp"),
        (b"wOFF\x00\x01", "font/woff"),
        (b"wOF2\x00\x01", "font/woff2"),
        (b"\x00\x01\x00\x00\x00\x0a", "font/ttf"),
        (b"%PDF-1.7\n", "application/pdf"),
    ])
    def test_magic(self, data, expected):
        assert files.sniff_media_type(data) == expected

    def test_riff_that_is_not_webp_is_binary(self):
        assert files.sniff_media_type(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "application/octet-stream"

    @pytest.mark.parametrize("data", [b"true", b"OTTO", b"true story"])
    def test_font_names_in_ascii_are_not_magic(self, data):
        assert files.sniff_media_type(data) != "font/ttf"
        assert files.sniff_media_type(data) != "font/otf"

    def test_otto_is_text(self):
        assert files.sniff_media_type(b"OTTO") == "text/plain"

    def test_ascii_true_is_a_json_value(self):
        # Not TrueType magic; as text it is JSON, like any other JSON scalar.
        assert files.sniff_media_type(b"true") == "application/json"

    def test_html_with_bom_and_leading_whitespace(self):
        assert files.sniff_media_type(b"\xef\xbb\xbf \n\t<!doctype html><p>x") == "text/html"

    def test_doctype_in_capitals(self):
        assert files.sniff_media_type(b"<!DOCTYPE HTML>\n<body>") == "text/html"

    def test_html_tag_without_doctype(self):
        assert files.sniff_media_type(b"<HTML><body>x</body></HTML>") == "text/html"

    def test_json(self):
        assert files.sniff_media_type(json.dumps({"a": [1, 2]}).encode()) == "application/json"

    def test_plain_text(self):
        assert files.sniff_media_type(b"hello, world\n") == "text/plain"

    def test_empty_is_plain_text(self):
        assert files.sniff_media_type(b"") == "text/plain"

    def test_nul_in_text_is_binary(self):
        assert files.sniff_media_type(b"abc\x00def") == "application/octet-stream"

    def test_invalid_utf8_is_binary(self):
        assert files.sniff_media_type(b"\xff\xfe\xfa abc") == "application/octet-stream"

    def test_html_marker_beyond_the_first_1024_characters_is_not_html(self):
        assert files.sniff_media_type(b"x" * 1100 + b"<html>") == "text/plain"


class TestFileReference:
    def test_upload_prefix_is_kept(self):
        assert files.file_reference("upload:abc") == "upload:abc"

    def test_bare_upload_id_gets_the_prefix(self):
        slot = "A" * 43
        assert files.file_reference(slot) == "upload:" + slot

    def test_paths_are_returned_as_they_are(self):
        assert files.file_reference("~/x.html") == "~/x.html"
        assert files.file_reference("/tmp/x.html") == "/tmp/x.html"

    def test_surrounding_whitespace_is_stripped(self):
        assert files.file_reference("  /tmp/x.html \n") == "/tmp/x.html"


class TestResolveFile:
    def test_upload_slot(self):
        html = b"<!doctype html><p>hi</p>"
        slot = _slot("page.html", html)
        with principal_scope(LOCAL_PRINCIPAL), local_files.call_context(bridge_available=False, uploads={}):
            got = files.resolve_file(HTML_ONLY, "upload:" + slot, tool_title="Publish")
        assert got == IncomingFile(
            "html", "page.html", len(html), "text/html", "text/html",
            hashlib.sha256(html).hexdigest(), "Upload slot", html,
        )

    def test_bare_slot_id_is_an_upload(self):
        slot = _slot("page.html", b"<html></html>")
        with principal_scope(LOCAL_PRINCIPAL), local_files.call_context(bridge_available=False, uploads={}):
            got = files.resolve_file(HTML_ONLY, slot, tool_title="Publish")
        assert got.source == "Upload slot"

    def test_slot_named_with_a_windows_path(self):
        slot = _slot("C:\\x\\page.html", b"<html></html>")
        with principal_scope(LOCAL_PRINCIPAL), local_files.call_context(bridge_available=False, uploads={}):
            got = files.resolve_file(HTML_ONLY, "upload:" + slot, tool_title="Publish")
        assert got.name == "page.html"

    def test_direct_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_files.privilege_separation, "is_enabled", lambda: False)
        f = tmp_path / "Page.HTM"
        f.write_bytes(b"<html>x</html>")
        value = f"  {f} "
        with local_files.call_context(bridge_available=False, uploads={}):
            got = files.resolve_file(HTML_ONLY, value, tool_title="Publish")
        assert (got.name, got.source, got.media_type, got.sniffed_type) == (
            "Page.HTM", str(f), "text/html", "text/html",
        )
        assert got.size == 14

    def test_name_and_source_are_cleaned_and_capped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_files.privilege_separation, "is_enabled", lambda: False)
        slot = _slot("a\u202eb\tc" + "x" * 200 + ".html", b"<html></html>")
        with principal_scope(LOCAL_PRINCIPAL), local_files.call_context(bridge_available=False, uploads={}):
            got = files.resolve_file(HTML_ONLY, "upload:" + slot, tool_title="Publish")
        assert "\u202e" not in got.name and "\t" not in got.name
        assert len(got.name) == 120
        long_path = tmp_path / ("p" * 100) / ("q" * 100) / ("r" * 100)
        long_path.mkdir(parents=True)
        (long_path / "f.html").write_bytes(b"<html></html>")
        with local_files.call_context(bridge_available=False, uploads={}):
            got = files.resolve_file(HTML_ONLY, str(long_path / "f.html"), tool_title="Publish")
        assert len(got.source) == 200

    def test_unnamed_slot(self):
        slot = _slot("dir/", b"<html></html>")
        with principal_scope(LOCAL_PRINCIPAL), local_files.call_context(bridge_available=False, uploads={}):
            got = files.resolve_file(HTML_ONLY, "upload:" + slot, tool_title="Publish")
        assert got.name == files.UNNAMED_FILE

    def test_over_max_bytes(self):
        slot = _slot("big.html", b"<html>" + b"x" * 2000)
        spec = FileParamSpec("html", 1000, ("text/html",))
        with principal_scope(LOCAL_PRINCIPAL), local_files.call_context(bridge_available=False, uploads={}):
            with pytest.raises(LocalFileAccessError, match=r"The file is 2,006 bytes, over the 1,000-byte limit of Publish\."):
                files.resolve_file(spec, "upload:" + slot, tool_title="Publish")

    def test_refused_sniffed_type(self):
        slot = _slot("notes.html", b"just text")
        with principal_scope(LOCAL_PRINCIPAL), local_files.call_context(bridge_available=False, uploads={}):
            with pytest.raises(
                LocalFileAccessError,
                match=r"The file's content is text/plain, and Publish accepts only text/html\.",
            ):
                files.resolve_file(HTML_ONLY, "upload:" + slot, tool_title="Publish")

    def test_empty_value(self):
        with pytest.raises(LocalFileAccessError, match=r"Publish needs a file in html\."):
            files.resolve_file(HTML_ONLY, "   ", tool_title="Publish")

    def test_non_string_value(self):
        with pytest.raises(LocalFileAccessError, match=r"Publish needs a file path or upload id in html\."):
            files.resolve_file(HTML_ONLY, 5, tool_title="Publish")  # type: ignore[arg-type]

    def test_needs_the_bridge_for_a_path_when_the_daemon_cannot_read_it(self):
        local_files.force_bridge_for_tests(True)
        with local_files.call_context(bridge_available=True, uploads={}):
            with pytest.raises(local_files.LocalFilesNeeded) as caught:
                files.resolve_file(HTML_ONLY, "~/x.html", tool_title="Publish")
        assert caught.value.paths == ["~/x.html"]
        assert caught.value.max_bytes == 1000


class TestToWire:
    def test_without_content(self):
        f = IncomingFile("doc", "a.txt", 3, "text/plain", "text/plain", "ab" * 32, "Upload slot", b"abc")
        wire = f.to_wire(with_content=False)
        assert wire == {"name": "a.txt", "size": 3, "media_type": "text/plain",
                        "sniffed_type": "text/plain", "sha256": "ab" * 32}

    def test_with_content(self):
        f = IncomingFile("doc", "a.txt", 3, "text/plain", "text/plain", "ab" * 32, "Upload slot", b"abc")
        assert base64.b64decode(f.to_wire(with_content=True)["content_base64"]) == b"abc"

    def test_repr_hides_the_bytes(self):
        f = IncomingFile("doc", "a.txt", 3, "text/plain", "text/plain", "ab" * 32, "Upload slot", b"secret")
        assert "secret" not in repr(f)


class TestCardBlock:
    def test_six_labelled_fields(self):
        f = IncomingFile("doc", "a.txt", 1234, "text/plain", "application/json", "cd" * 32, "~/a.txt", b"x")
        block = files.card_block(f)
        assert block["type"] == "fields"
        assert {i["label"]: i["value"] for i in block["items"]} == {
            "File": "a.txt", "Source": "~/a.txt", "Size": "1,234 bytes", "Declared type": "text/plain",
            "Detected type": "application/json", "SHA-256": "cd" * 32,
        }
        assert [i["label"] for i in block["items"]] == [
            "File", "Source", "Size", "Declared type", "Detected type", "SHA-256",
        ]


class TestParamDescription:
    def test_appends_the_hint(self):
        text = files.param_description("  The page. ", ANY_TEXT)
        assert text.startswith("The page. A file, not its content:")
        assert "At most 1,000 bytes; accepted types: text/html, text/plain, application/json." in text

    def test_empty_description_is_the_hint_alone(self):
        assert files.param_description("", HTML_ONLY).startswith("A file, not its content:")
