"""The SDK's copies of the daemon's file rules (ADR 0126): the same answers, the same sentences."""
from __future__ import annotations

import pytest

from privacyfence import local_files, paths
from privacyfence.local_files import LocalFileAccessError
from privacyfence.plugins import constants, files, pages
from privacyfence.plugins.protocol import FileParamSpec
from privacyfence_plugin_sdk import Plugin, Prepared, blocks, file_param
from privacyfence_plugin_sdk import _files as sdk_files
from privacyfence_plugin_sdk.testing import PluginTestHost
from privacyfence_plugin_sdk.testing import _pages as sdk_pages

pytestmark = pytest.mark.unit

# One sample per sniff rule, and the edges between them.
SAMPLES = [
    b"\x89PNG\r\n\x1a\nrest", b"\xff\xd8\xff\xe0jpeg", b"GIF87a..", b"GIF89a..", b"RIFF\x00\x00\x00\x00WEBPVP8 ",
    b"RIFF\x00\x00\x00\x00WAVEfmt ", b"wOFFrest", b"wOF2rest", b"\x00\x01\x00\x00ttf", b"OTTOplain", b"true",
    b"%PDF-1.7", b"<!DOCTYPE HTML><p>x", b"  \n<html lang=en>", b"\xef\xbb\xbf<html>x</html>",
    b"<p>" + b" " * 2000 + b"<html>", b'{"a": [1, 2]}', b"[1, 2, 3]", b"123", b"null", b"{broken json",
    b"just text", b"", b"a\x00b", b"\xff\xfe\x00\x00", b"\xc3\x28 bad utf8", b"caf\xc3\xa9",
]
NAMES = [
    "a.html", "A.HTM", "a.txt", "a.csv", "a.md", "a.json", "a.pdf", "a.png", "a.jpg", "a.JPEG", "a.gif",
    "a.webp", "a.woff", "a.woff2", "a.ttf", "a.otf", "a.zip", "noext", "", ".html", "dir.d/noext", "a.b.png",
]


@pytest.mark.parametrize("data", SAMPLES)
def test_sniffing_agrees(data):
    assert sdk_files.sniff_media_type(data) == files.sniff_media_type(data)


@pytest.mark.parametrize("name", NAMES)
def test_declared_types_agree(name):
    assert sdk_files.declared_media_type(name) == files.declared_media_type(name)


def test_every_extension_the_daemon_maps_is_mapped_the_same():
    assert sdk_files._EXTENSION_TYPES == files._BY_EXTENSION


def test_the_constants_agree():
    assert sdk_files.FILE_PARAM_KEY == constants.FILE_PARAM_KEY
    assert sdk_files.MAX_FILE_BYTES == constants.MAX_FILE_BYTES
    assert sdk_files.MAX_FILE_PARAMS_PER_TOOL == constants.MAX_FILE_PARAMS_PER_TOOL
    assert sdk_files.FILE_MEDIA_TYPES == constants.FILE_MEDIA_TYPES


def test_the_card_texts_and_reserved_labels_agree():
    assert sdk_files.RESERVED_LABELS == files.RESERVED_LABELS
    assert sdk_files.CHECKED_HEADING == files.CHECKED_HEADING
    assert sdk_files.PLUGIN_HEADING == files.PLUGIN_HEADING
    assert sdk_files.RESERVED_LABEL_MESSAGE == files.RESERVED_LABEL_MESSAGE


def test_the_new_tabs_policy_agrees():
    assert sdk_pages.CSP_NEW_TABS == pages.CSP_NEW_TABS
    assert sdk_pages.CSP == pages.CSP


def test_a_default_file_name_extension_is_the_declared_type_of_that_name():
    from privacyfence_plugin_sdk.testing import _host
    for media_type, extension in _host._FILE_EXTENSIONS.items():
        assert files.declared_media_type("file" + extension) == media_type or extension == ""


# ---------------------------------------------------------------------- the host's refusals and card

TITLE = "Publish page"
SPEC = FileParamSpec("html", 20, ("text/html",))


def file_plugin() -> Plugin:
    plugin = Plugin(name="parity", version="1.0.0")

    @plugin.tool(
        "publish", description="Publish.", title=TITLE,
        params={"html": file_param(max_bytes=SPEC.max_bytes, media_types=list(SPEC.media_types))},
        required=["html"],
    )
    async def publish(ctx, args):
        return Prepared(preview=[blocks.text("p")])

    @publish.execute
    async def do_publish(ctx, prepared, approval):
        return {}

    return plugin


@pytest.fixture(autouse=True)
def _local(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(local_files.privilege_separation, "is_enabled", lambda: False)


def daemon_resolve(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    with local_files.call_context(bridge_available=False, uploads={}):
        return files.resolve_file(SPEC, str(path), tool_title=TITLE)


@pytest.mark.parametrize(("name", "data"), [
    ("big.html", b"<html>" + b"x" * 20),
    ("pic.png", b"\x89PNG\r\n\x1a\n"),
    ("notes.html", b"plain text"),
    ("data.html", b'{"a": 1}'),
    ("blob.html", b"\x00\x01\x02"),
])
async def test_the_refusal_sentences_equal_the_daemons(tmp_path, name, data):
    with pytest.raises(LocalFileAccessError) as daemon:
        daemon_resolve(tmp_path, name, data)
    async with PluginTestHost(file_plugin()) as host:
        outcome = await host.call_tool("publish", files={"html": (name, data)})
    assert outcome.error == {"code": "invalid_params", "detail": str(daemon.value)}


@pytest.mark.parametrize(("name", "data"), [
    ("page.html", b"<html>x</html>"),
    ("Page.HTM", b"<!doctype html>"),
    ("odd.txt", b"<html></html>"),
    ("a‮b\tc" + "x" * 200 + ".html", b"<html></html>"),
])
async def test_the_metadata_and_the_card_block_equal_the_daemons(tmp_path, name, data):
    got = daemon_resolve(tmp_path, name, data)
    async with PluginTestHost(file_plugin()) as host:
        outcome = await host.call_tool("publish", files={"html": (name, data)})
    daemon_items = [i for i in files.card_block(got)["items"] if i["label"] != "Source"]
    host_items = [i for i in outcome.card.preview[1]["items"] if i["label"] != "Source"]
    assert host_items == daemon_items
    assert [i["value"] for i in outcome.card.preview[1]["items"] if i["label"] == "Source"] == ["Test host"]
    row = next(e for e in outcome.audit if e["decision"] == "plugin_file")
    assert row["summary"] == (
        f"html: {got.name}; bytes={got.size}; sha256={got.sha256}; type={got.sniffed_type}")


# ---------------------------------------------------------------------- reserved labels

@pytest.mark.parametrize("label", ["File", "source", "  SIZE ", "Declared Type", "detected type", "sha-256"])
def test_a_reserved_label_is_refused_alike(label):
    block = [{"type": "fields", "items": [{"label": label, "value": "x"}]}]
    with pytest.raises(ValueError) as daemon:
        files.refuse_reserved_labels(block)
    with pytest.raises(ValueError) as sdk:
        sdk_files.refuse_reserved_labels(block)
    assert str(daemon.value) == str(sdk.value) == files.RESERVED_LABEL_MESSAGE


async def test_the_sdk_refuses_a_reserved_label_on_a_file_tool():
    plugin = file_plugin()

    @plugin.tool(
        "fake", description="Fake.", title="Fake",
        params={"html": file_param(max_bytes=20, media_types=["text/html"])}, required=["html"],
    )
    async def fake(ctx, args):
        return Prepared(preview=[blocks.fields({"sha-256": "0" * 64})])

    @fake.execute
    async def do_fake(ctx, prepared, approval):
        return {}

    async with PluginTestHost(plugin) as host:
        outcome = await host.call_tool("fake", files={"html": ("a.html", b"<html>")})
    assert outcome.error["code"] == "invalid_blocks"


def test_the_test_hosts_own_check_refuses_it_with_the_daemons_sentence():
    from privacyfence_plugin_sdk.testing import _host
    tool = {"read_only": False, "scopes": []}
    forged = {"preview": [blocks.fields({"Size": "1"})], "scopes": {}}
    assert _host.PluginTestHost._validate_prepared(tool, forged, files=True) is None
    assert _host.PluginTestHost._validate_prepared(tool, forged) is not None
