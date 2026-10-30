"""scripts/sign_connector_config.py: it reproduces the checked-in fixtures byte for byte, keeps the
client secret off the command line, and never overwrites or exposes what it writes."""

from __future__ import annotations

import base64
import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

from privacyfence import connector_configs
from tests.connector_config_support import FIXTURE_DIR, SENTINEL, expected_file, fixture_inputs

pytestmark = pytest.mark.unit

_SPEC = importlib.util.spec_from_file_location(
    "sign_connector_config", Path(__file__).resolve().parents[2] / "scripts" / "sign_connector_config.py"
)
tool = importlib.util.module_from_spec(_SPEC)
sys.modules["sign_connector_config"] = tool
_SPEC.loader.exec_module(tool)

KEY = FIXTURE_DIR / "key.pem"
OPTIONS = {
    "login_url": "--login-url",
    "auth_server_url": "--auth-server-url",
    "server_url": "--server-url",
}


def _sign_args(provider: str, secret_file: Path, output: Path, key: Path = KEY) -> list[str]:
    spec = fixture_inputs()[provider]
    fields = dict(spec["fields"])
    ident, secret = tool.field_names(provider)[:2]
    args = [
        "sign", "--provider", provider, "--serial", str(spec["serial"]), "--client-id", fields[ident],
        "--client-secret-file", str(secret_file), "--key", str(key), "--signed-at", spec["signed_at"],
        "--output", str(output),
    ]
    for name, option in OPTIONS.items():
        if name in fields:
            args += [option, fields[name]]
    return args


@pytest.fixture
def secret_file(tmp_path: Path):
    def make(provider: str) -> Path:
        path = tmp_path / f"{provider}.secret"
        path.write_text(dict(fixture_inputs()[provider]["fields"])[tool.field_names(provider)[1]] + "\n")
        return path

    return make


@pytest.mark.parametrize("provider", sorted(fixture_inputs()))
def test_reproduces_the_fixture_byte_for_byte(provider, secret_file, tmp_path, capsys):
    out = tmp_path / f"{provider}.pfconnector"
    assert tool.main(_sign_args(provider, secret_file(provider), out)) == 0
    assert out.read_bytes() == expected_file(provider)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    shown = capsys.readouterr().out
    assert "SHA256:" in shown
    assert dict(fixture_inputs()[provider]["fields"])[tool.field_names(provider)[1]] not in shown


@pytest.mark.parametrize("provider", sorted(fixture_inputs()))
def test_what_it_makes_passes_the_servers_parser_and_verifier(provider, secret_file, tmp_path):
    out = tmp_path / "out.pfconnector"
    assert tool.main(_sign_args(provider, secret_file(provider), out)) == 0
    parsed = connector_configs.parse(out.read_bytes())
    assert connector_configs.verify(parsed, parsed.key)
    assert parsed.key == tool._public_bytes(tool.load_private_key(KEY))
    assert parsed.provider == provider


def test_the_fingerprint_format_matches_the_server(secret_file):
    key = tool.load_private_key(KEY)
    public = tool._public_bytes(key)
    assert tool.fingerprint(public) == connector_configs.fingerprint(public)


# ---- the secret never rides on the command line ----------------------------------------------


@pytest.mark.parametrize(
    "form",
    [["--client-secret", SENTINEL], [f"--client-secret={SENTINEL}"]],
)
def test_client_secret_on_the_command_line_is_refused(form, secret_file, tmp_path, capsys):
    out = tmp_path / "never.pfconnector"
    args = _sign_args("slack", secret_file("slack"), out)
    args = args[: args.index("--client-secret-file")] + form + args[args.index("--key"):]
    assert tool.main(args) == 1
    captured = capsys.readouterr()
    assert "--client-secret-file" in captured.err
    assert SENTINEL not in captured.out + captured.err
    assert not out.exists()


def test_client_secret_is_refused_even_without_the_other_options(capsys):
    with pytest.raises(SystemExit) as stopped:
        tool.main(["sign", "--client-secret", SENTINEL])
    assert stopped.value.code != 0
    assert SENTINEL not in capsys.readouterr().err


def test_an_abbreviation_does_not_reach_the_secret_file_option(secret_file, tmp_path):
    args = _sign_args("slack", secret_file("slack"), tmp_path / "x")
    args[args.index("--client-secret-file")] = "--client-secret-fi"
    with pytest.raises(SystemExit):
        tool.main(args)


def test_without_a_secret_file_the_secret_is_prompted_for(tmp_path, monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(tool.getpass, "getpass", lambda prompt: asked.append(prompt) or "prompted-secret")
    out = tmp_path / "out.pfconnector"
    args = _sign_args("slack", tmp_path / "unused", out)
    del args[args.index("--client-secret-file"): args.index("--client-secret-file") + 2]
    assert tool.main(args) == 0
    assert asked == ["Client secret: "]
    assert "client_secret: prompted-secret\n" in out.read_text()


def test_a_salesforce_prompt_names_the_consumer_secret(tmp_path, monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(tool.getpass, "getpass", lambda prompt: asked.append(prompt) or "s")
    args = _sign_args("salesforce", tmp_path / "unused", tmp_path / "out")
    del args[args.index("--client-secret-file"): args.index("--client-secret-file") + 2]
    assert tool.main(args) == 0
    assert asked == ["Consumer secret: "]


def test_a_secret_file_loses_one_trailing_line_ending_only(tmp_path):
    path = tmp_path / "s"
    for raw, expected in [("abc\n", "abc"), ("abc\r\n", "abc"), ("abc", "abc"), ("abc\n\n", "abc\n"), ("a b ", "a b ")]:
        path.write_text(raw)
        assert tool.read_secret(path, "x") == expected


# ---- output files ----------------------------------------------------------------------------


def test_it_never_overwrites_the_output(secret_file, tmp_path, capsys):
    out = tmp_path / "taken.pfconnector"
    out.write_text("keep me")
    assert tool.main(_sign_args("slack", secret_file("slack"), out)) == 1
    assert out.read_text() == "keep me"
    assert "already exists" in capsys.readouterr().err


def test_it_does_not_follow_a_symlink_at_the_output(secret_file, tmp_path):
    target = tmp_path / "target"
    target.write_text("keep me")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert tool.main(_sign_args("slack", secret_file("slack"), link)) == 1
    assert target.read_text() == "keep me"


def test_the_output_is_0600_whatever_the_umask(secret_file, tmp_path):
    previous = os.umask(0)
    try:
        out = tmp_path / "out"
        assert tool.main(_sign_args("slack", secret_file("slack"), out)) == 0
    finally:
        os.umask(previous)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_keygen_writes_a_parseable_key_and_prints_only_public_facts(tmp_path, capsys):
    out = tmp_path / "key.pem"
    assert tool.main(["keygen", "--out", str(out)]) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert out.read_text().startswith("-----BEGIN PRIVATE KEY-----")
    key = tool.load_private_key(out)
    public = tool._public_bytes(key)
    shown = capsys.readouterr().out
    assert base64.b64encode(public).decode() in shown
    assert connector_configs.fingerprint(public) in shown
    body = "".join(out.read_text().splitlines()[1:-1])
    assert body not in shown


def test_keygen_refuses_to_overwrite(tmp_path, capsys):
    out = tmp_path / "key.pem"
    out.write_text("precious")
    assert tool.main(["keygen", "--out", str(out)]) == 1
    assert out.read_text() == "precious"
    assert "already exists" in capsys.readouterr().err


def test_a_key_made_by_keygen_signs_files_the_server_accepts(secret_file, tmp_path):
    key_path = tmp_path / "key.pem"
    assert tool.main(["keygen", "--out", str(key_path)]) == 0
    out = tmp_path / "out"
    assert tool.main(_sign_args("slack", secret_file("slack"), out, key_path)) == 0
    parsed = connector_configs.parse(out.read_bytes())
    assert connector_configs.verify(parsed, parsed.key)


# ---- refusals --------------------------------------------------------------------------------


def _run(args: list[str], capsys) -> str:
    assert tool.main(args) == 1
    return capsys.readouterr().err


def test_bad_inputs_are_refused_without_writing(secret_file, tmp_path, capsys):
    secret = secret_file("slack")
    out = tmp_path / "out"
    base = _sign_args("slack", secret, out)

    def with_(option: str, value: str) -> list[str]:
        args = list(base)
        args[args.index(option) + 1] = value
        return args

    assert "serial" in _run(with_("--serial", "0123"), capsys)
    assert "serial" in _run(with_("--serial", "-5"), capsys)
    assert "serial" in _run(with_("--serial", "1" * 16), capsys)
    assert "client_id" in _run(with_("--client-id", " padded"), capsys)
    assert "client_id" in _run(with_("--client-id", "café"), capsys)
    assert "client_id" in _run(with_("--client-id", "x" * 513), capsys)
    assert "client_id" in _run(with_("--client-id", "tab\there"), capsys)
    assert "signed_at" in _run(with_("--signed-at", "2026-11-02 10:00"), capsys)
    assert "signed_at" in _run(with_("--signed-at", "2026-11-2T1:00:00Z"), capsys)
    assert "Cannot read the key file" in _run(with_("--key", str(tmp_path / "missing.pem")), capsys)
    assert not out.exists()


def test_a_bad_secret_is_refused_without_echoing_it(tmp_path, capsys):
    secret = tmp_path / "s"
    secret.write_text("café-secret\n", encoding="utf-8")
    out = tmp_path / "out"
    err = _run(_sign_args("slack", secret, out), capsys)
    assert "client_secret" in err
    assert "caf" not in err
    assert not out.exists()


def test_a_non_ed25519_or_encrypted_key_is_refused(secret_file, tmp_path, capsys):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    rsa_key = tmp_path / "rsa.pem"
    rsa_key.write_bytes(
        rsa.generate_private_key(65537, 2048).private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    assert "not an Ed25519 key" in _run(_sign_args("slack", secret_file("slack"), tmp_path / "o", rsa_key), capsys)
    junk = tmp_path / "junk.pem"
    junk.write_text("not a key")
    assert "PKCS#8 PEM" in _run(_sign_args("slack", secret_file("slack"), tmp_path / "o", junk), capsys)


def test_provider_specific_options_are_checked(secret_file, tmp_path, capsys):
    base = _sign_args("slack", secret_file("slack"), tmp_path / "o")
    assert "does not apply" in _run(base + ["--login-url", "https://x.my.salesforce.com"], capsys)
    assert "does not apply" in _run(base + ["--server-url", "https://x.example"], capsys)
    grist = _sign_args("grist", secret_file("grist"), tmp_path / "g")
    i = grist.index("--server-url")
    assert "needs --auth-server-url and --server-url" in _run(grist[:i] + grist[i + 2:], capsys)


def test_salesforce_without_a_login_url_omits_the_line(secret_file, tmp_path):
    args = _sign_args("salesforce", secret_file("salesforce"), tmp_path / "o")
    i = args.index("--login-url")
    assert tool.main(args[:i] + args[i + 2:]) == 0
    text = (tmp_path / "o").read_text()
    assert "login_url" not in text
    parsed = connector_configs.parse(text.encode())
    assert connector_configs.verify(parsed, parsed.key)


def test_the_largest_legal_file_fits_the_size_limit():
    key = tool.load_private_key(KEY)
    fields = {"client_id": "i", "client_secret": "s"}
    assert len(tool.build_signed_text("slack", "1", fields, "2026-11-02T10:00:00Z", key)) < tool.MAX_FILE_BYTES
    long_fields = {"client_id": "i" * 512, "client_secret": "s" * 512, "auth_server_url": "a" * 512, "server_url": "b" * 512}
    assert len(tool.build_signed_text("grist", "1", long_fields, "2026-11-02T10:00:00Z", key)) < tool.MAX_FILE_BYTES


def test_an_unknown_provider_is_refused():
    key = tool.load_private_key(KEY)
    with pytest.raises(tool.ToolError):
        tool.build_signed_text("nope", "1", {}, "2026-11-02T10:00:00Z", key)


def test_verify_catches_a_tampered_file():
    key = tool.load_private_key(KEY)
    good = tool.build_signed_text("slack", "1", {"client_id": "i", "client_secret": "s"}, "2026-11-02T10:00:00Z", key)
    tool.verify_signed_text(good)
    with pytest.raises(tool.ToolError):
        tool.verify_signed_text(good.replace(b"client_id: i", b"client_id: j"))
