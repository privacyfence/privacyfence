"""File parameters: the schema helper, type detection and the wire parser."""
from __future__ import annotations

import base64
import hashlib

import pytest

from privacyfence_plugin_sdk import IncomingFile, file_param
from privacyfence_plugin_sdk._files import declared_media_type, parse_files, sniff_media_type
from privacyfence_plugin_sdk._rpc import RpcError

SPECS = {"html": {"max_bytes": 100, "media_types": ["text/html", "text/plain"]}}
DATA = b"<html>x</html>"


def item(data=DATA, *, content=True, **overrides):
    out = {"name": "a.html", "size": len(data), "media_type": "text/html", "sniffed_type": "text/html",
           "sha256": hashlib.sha256(data).hexdigest()}
    if content:
        out["content_base64"] = base64.b64encode(data).decode()
    out.update(overrides)
    return out


class TestFileParam:
    def test_the_schema_dict(self):
        assert file_param("The page.", max_bytes=10, media_types=("text/html",)) == {
            "type": "string", "description": "The page.",
            "x-privacyfence-file": {"max_bytes": 10, "media_types": ["text/html"]}}

    def test_no_description_key_when_empty(self):
        assert "description" not in file_param(max_bytes=10, media_types=["text/html"])


class TestDeclaredType:
    @pytest.mark.parametrize("name, expected", [
        ("a.html", "text/html"), ("A.HTM", "text/html"), ("a.txt", "text/plain"), ("a.csv", "text/plain"),
        ("a.md", "text/plain"), ("a.json", "application/json"), ("a.pdf", "application/pdf"),
        ("a.png", "image/png"), ("a.jpg", "image/jpeg"), ("a.JPEG", "image/jpeg"), ("a.gif", "image/gif"),
        ("a.webp", "image/webp"), ("a.woff", "font/woff"), ("a.woff2", "font/woff2"),
        ("a.ttf", "font/ttf"), ("a.otf", "font/otf"), ("a.zip", "application/octet-stream"),
        ("noext", "application/octet-stream"), ("", "application/octet-stream"),
    ])
    def test_by_extension(self, name, expected):
        assert declared_media_type(name) == expected


class TestSniff:
    @pytest.mark.parametrize("data, expected", [
        (b"\x89PNG\r\n\x1a\n....", "image/png"), (b"\xff\xd8\xff\xe0", "image/jpeg"),
        (b"GIF87a..", "image/gif"), (b"GIF89a..", "image/gif"),
        (b"RIFF\x01\x02\x03\x04WEBPVP8 ", "image/webp"), (b"RIFF\x01\x02\x03\x04WAVEfmt ", "text/plain"),
        (b"wOFF\x00", "font/woff"), (b"wOF2\x00", "font/woff2"), (b"\x00\x01\x00\x00\x00", "font/ttf"),
        (b"%PDF-1.7", "application/pdf"),
        (b"OTTO", "text/plain"), (b"true", "application/json"),
        (b"<!DOCTYPE HTML><p>", "text/html"), (b"  \n<html lang=en>", "text/html"),
        (b"\xef\xbb\xbf<html>", "text/html"), (b'{"a": 1}', "application/json"), (b"[1, 2]", "application/json"),
        (b"just text", "text/plain"), (b"", "text/plain"), (b"{not json", "text/plain"),
        (b"a\x00b", "application/octet-stream"), (b"\xff\xfe\x00bad", "application/octet-stream"),
    ])
    def test_cases(self, data, expected):
        assert sniff_media_type(data) == expected

    def test_html_marker_beyond_the_first_1024_characters_is_text(self):
        assert sniff_media_type(b" " * 10 + b"x" * 1100 + b"<html>") == "text/plain"


class TestIncomingFile:
    def test_content_needs_the_bytes(self):
        meta = IncomingFile("a", 1, "text/plain", "text/plain", "0" * 64)
        with pytest.raises(RuntimeError, match="available in the execute function only"):
            _ = meta.content
        assert IncomingFile("a", 1, "t", "t", "0", data=b"x").content == b"x"
        assert "data" not in repr(meta)


class TestParseFiles:
    def test_valid_with_content(self):
        files = parse_files({"html": item()}, SPECS, with_content=True)
        assert files["html"].content == DATA and files["html"].name == "a.html"

    def test_content_is_ignored_without_with_content(self):
        files = parse_files({"html": item(content_base64="!!!")}, SPECS, with_content=False)
        assert files["html"].data is None

    def test_none_and_missing_give_nothing(self):
        assert parse_files(None, SPECS, with_content=True) == {}

    def test_not_an_object(self):
        with pytest.raises(RpcError, match="files must be an object"):
            parse_files([], SPECS, with_content=False)

    def test_unknown_parameter(self):
        with pytest.raises(RpcError) as info:
            parse_files({"other": item()}, SPECS, with_content=False)
        assert info.value.code == "invalid_params" and info.value.detail.startswith("files.other:")

    @pytest.mark.parametrize("overrides", [
        {"name": 5}, {"size": "1"}, {"size": True}, {"sha256": None}, {"media_type": 1}, {"sniffed_type": None},
    ])
    def test_wrong_field_types(self, overrides):
        with pytest.raises(RpcError, match="files.html:"):
            parse_files({"html": item(**overrides)}, SPECS, with_content=False)

    def test_missing_field_and_non_object_item(self):
        bad = item()
        del bad["name"]
        with pytest.raises(RpcError, match="files.html: name"):
            parse_files({"html": bad}, SPECS, with_content=False)
        with pytest.raises(RpcError, match="files.html: must be an object"):
            parse_files({"html": "x"}, SPECS, with_content=False)

    def test_content_required_with_content(self):
        with pytest.raises(RpcError, match="files.html: content_base64"):
            parse_files({"html": item(content=False)}, SPECS, with_content=True)

    def test_bad_base64(self):
        with pytest.raises(RpcError, match="files.html: content_base64 is not valid base64"):
            parse_files({"html": item(content_base64="***")}, SPECS, with_content=True)

    def test_size_mismatch(self):
        with pytest.raises(RpcError, match="files.html: content length differs from size"):
            parse_files({"html": item(size=len(DATA) + 1)}, SPECS, with_content=True)

    def test_over_max_bytes(self):
        big = b"<html>" + b"x" * 200
        with pytest.raises(RpcError, match="files.html: size exceeds 100 bytes"):
            parse_files({"html": item(big)}, SPECS, with_content=False)

    def test_refused_sniffed_type(self):
        with pytest.raises(RpcError, match="files.html: sniffed_type image/png is not accepted"):
            parse_files({"html": item(sniffed_type="image/png")}, SPECS, with_content=False)

    def test_sha256_mismatch(self):
        with pytest.raises(RpcError, match="files.html: content differs from sha256"):
            parse_files({"html": item(sha256="0" * 64)}, SPECS, with_content=True)
