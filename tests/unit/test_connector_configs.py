"""Signed connector configs: the format, the pinned key, serials, storage and the legacy import.

The module's own paths are exempt from the autouse isolation fixture, so every test passes an
``org`` directory under ``tmp_path`` explicitly.
"""
from __future__ import annotations

import base64
import json
import logging
import stat

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from privacyfence import connector_configs, org_bundle_signing
from privacyfence.connector_configs import (
    ConfirmationRequired,
    ConnectorConfigError,
    accept,
    fingerprint,
    import_from_legacy_bundle,
    load_all,
    parse,
    remove,
    verify,
)
from privacyfence.org_mode import ConfigurationError
from tests.connector_config_support import (
    FIXTURE_DIR,
    SENTINEL,
    build_expected,
    expected_file,
    fixture_inputs,
    fixture_key,
    public_bytes,
    sign,
    signed_text,
)

KEY_FP = fingerprint(public_bytes(fixture_key()))
BUNDLE_SHA = "a" * 64


@pytest.fixture
def org(tmp_path):
    path = tmp_path / "org"
    path.mkdir()
    return path


def confirmed(org, provider="slack", serial=1, **overrides):
    """Accept a valid file for ``provider``, confirming the fixture key's fingerprint."""
    return accept(org, sign(provider, serial, **overrides), confirmed_fingerprint=KEY_FP)


def replace_line(raw: bytes, prefix: str, new: str | None) -> bytes:
    lines = raw.decode().split("\n")
    index = next(i for i, line in enumerate(lines) if line.startswith(prefix))
    if new is None:
        del lines[index]
    else:
        lines[index] = new
    return "\n".join(lines).encode()


def adopt(org, bundle_pin: bytes | None = None, *, sha=BUNDLE_SHA, **record):
    """A server upgraded from 6.x: an adopted-bundle record, and optionally the bundle's pinned key."""
    (org / "org_bundle_adopted.json").write_text(json.dumps({"from": "org_config.json", "sha256": sha, **record}))
    if bundle_pin is not None:
        (org / org_bundle_signing.PINNED_PUBKEY_FILENAME).write_text(base64.b64encode(bundle_pin).decode() + "\n")


def record_of(org):
    return json.loads((org / "org_bundle_adopted.json").read_text())


# ---------------------------------------------------------------------------- #
# fixtures
# ---------------------------------------------------------------------------- #

class TestFixtures:
    @pytest.mark.parametrize("provider", ["slack", "salesforce", "grist"])
    def test_checked_in_files_are_what_the_helper_builds(self, provider):
        assert expected_file(provider) == build_expected(provider)

    @pytest.mark.parametrize("provider", ["slack", "salesforce", "grist"])
    def test_the_server_verifies_them(self, provider):
        parsed = parse(expected_file(provider))
        assert parsed.provider == provider
        assert parsed.serial == fixture_inputs()[provider]["serial"]
        assert verify(parsed, public_bytes(fixture_key()))

    def test_key_is_pkcs8_pem(self):
        assert (FIXTURE_DIR / "key.pem").read_text().startswith("-----BEGIN PRIVATE KEY-----")


# ---------------------------------------------------------------------------- #
# parse
# ---------------------------------------------------------------------------- #

class TestParse:
    @pytest.mark.parametrize("provider", connector_configs.PROVIDERS)
    def test_every_provider_parses(self, provider):
        parsed = parse(sign(provider, 4))
        assert (parsed.provider, parsed.serial, parsed.signed_at) == (provider, 4, "2026-11-02T10:00:00Z")
        assert parsed.key == public_bytes(fixture_key())

    def test_fields_keep_the_bundle_names(self):
        assert list(parse(sign("salesforce", login_url="https://x.my.salesforce.com")).fields) == [
            "consumer_key", "consumer_secret", "login_url",
        ]
        assert list(parse(sign("salesforce")).fields) == ["consumer_key", "consumer_secret"]
        assert list(parse(sign("grist")).fields) == [
            "client_id", "client_secret", "auth_server_url", "server_url",
        ]

    def test_repr_never_shows_the_secret(self):
        parsed = parse(sign("slack"))
        assert SENTINEL not in repr(parsed)
        loaded = connector_configs.LoadedConfig("slack", "x", 1, "", "", dict(parsed.fields))
        assert SENTINEL not in repr(loaded)

    def test_the_signed_message_is_every_line_before_the_signature(self):
        raw = sign("slack")
        assert raw.startswith(parse(raw).message)
        assert raw[len(parse(raw).message):].startswith(b"signature: ")

    @pytest.mark.parametrize("size", [connector_configs.MAX_FILE_BYTES + 1, 100_000])
    def test_oversize_file(self, size):
        with pytest.raises(ConnectorConfigError, match="at most 8 KB"):
            parse(sign("slack") + b"x" * size)

    def test_exactly_the_limit_is_not_a_size_error(self):
        with pytest.raises(ConnectorConfigError) as excinfo:
            parse(b"x" * connector_configs.MAX_FILE_BYTES)
        assert "KB" not in str(excinfo.value)

    def test_non_ascii(self):
        with pytest.raises(ConnectorConfigError, match="printable ASCII only"):
            parse(sign("slack").replace(b"slack-id", "slack-é".encode()))

    @pytest.mark.parametrize("char", [b"\r", b"\t", b"\x00", b"\x1b", b"\x7f"])
    def test_control_character(self, char):
        raw = sign("slack").replace(b"client_id: slack-id", b"client_id: slack" + char + b"id")
        with pytest.raises(ConnectorConfigError, match="no control character"):
            parse(raw)

    def test_crlf_line_endings(self):
        with pytest.raises(ConnectorConfigError, match="control character"):
            parse(sign("slack").replace(b"\n", b"\r\n"))

    def test_no_final_line_feed(self):
        with pytest.raises(ConnectorConfigError, match="ends with a line feed"):
            parse(sign("slack").rstrip(b"\n"))

    def test_extra_blank_line_at_the_end(self):
        with pytest.raises(ConnectorConfigError, match="Line 9 is not expected"):
            parse(sign("slack") + b"\n")

    def test_extra_line_after_the_signature(self):
        with pytest.raises(ConnectorConfigError, match="not expected here"):
            parse(sign("slack") + b"note: hi\n")

    def test_wrong_header(self):
        with pytest.raises(ConnectorConfigError, match="first line"):
            parse(sign("slack").replace(b"config v1", b"config v2"))

    def test_header_only(self):
        with pytest.raises(ConnectorConfigError, match="Line 2 must be provider"):
            parse(f"{connector_configs.HEADER}\n".encode())

    @pytest.mark.parametrize("line", ["name: slack", "provider: Slack", "provider: telegram", "provider:slack"])
    def test_provider_line(self, line):
        with pytest.raises(ConnectorConfigError, match="Line 2 must be provider"):
            parse(replace_line(sign("slack"), "provider:", line))

    def test_reordered_fields(self):
        lines = sign("slack").decode().split("\n")
        lines[3], lines[4] = lines[4], lines[3]
        with pytest.raises(ConnectorConfigError, match="Line 4 must be client_id"):
            parse("\n".join(lines).encode())

    def test_unknown_field(self):
        with pytest.raises(ConnectorConfigError, match="Line 5 must be client_secret"):
            parse(replace_line(sign("slack"), "client_secret:", "scope: all"))

    def test_extra_field_before_signed_at(self):
        raw = replace_line(sign("slack"), "signed_at:", "extra: 1\nsigned_at: 2026-11-02T10:00:00Z")
        with pytest.raises(ConnectorConfigError, match="Line 6 must be signed_at"):
            parse(raw)

    def test_a_field_of_another_provider(self):
        raw = replace_line(sign("slack"), "signed_at:", "login_url: https://x\nsigned_at: 2026-11-02T10:00:00Z")
        with pytest.raises(ConnectorConfigError, match="Line 6 must be signed_at"):
            parse(raw)

    def test_missing_required_field(self):
        with pytest.raises(ConnectorConfigError, match="Line 5 must be client_secret"):
            parse(replace_line(sign("slack"), "client_secret:", None))

    def test_grist_needs_its_urls(self):
        with pytest.raises(ConnectorConfigError, match="must be auth_server_url"):
            parse(replace_line(sign("grist"), "auth_server_url:", None))

    def test_optional_login_url_must_be_well_formed_when_present(self):
        raw = replace_line(sign("salesforce"), "signed_at:", "login_url:https://x\nsigned_at: 2026-11-02T10:00:00Z")
        with pytest.raises(ConnectorConfigError, match="Line 6 must be signed_at"):
            parse(raw)

    @pytest.mark.parametrize("value", ["", " ", " lead", "trail ", "a" * 513])
    def test_value_rules(self, value):
        raw = replace_line(sign("slack"), "client_id:", f"client_id: {value}")
        with pytest.raises(ConnectorConfigError, match="Line 4 .client_id."):
            parse(raw)

    def test_value_may_hold_inner_spaces_and_the_maximum_length(self):
        parse(sign("slack", client_id="a b" + "c" * 509))

    @pytest.mark.parametrize("value", ["0", "012", "-3", "3.5", "x", "1" * 16])
    def test_serial_rules(self, value):
        with pytest.raises(ConnectorConfigError, match="serial must be"):
            parse(replace_line(sign("slack"), "serial:", f"serial: {value}"))

    @pytest.mark.parametrize(
        "value", ["2026-11-02", "2026-11-02 10:00:00Z", "2026-11-02T10:00:00", "2026-13-02T10:00:00Z", "2026-11-02T25:00:00Z"]
    )
    def test_signed_at_rules(self, value):
        with pytest.raises(ConnectorConfigError, match="signed_at must be"):
            parse(replace_line(sign("slack"), "signed_at:", f"signed_at: {value}"))

    def test_key_must_be_32_bytes_of_base64(self):
        for value in ("!!!", base64.b64encode(b"x" * 31).decode()):
            with pytest.raises(ConnectorConfigError, match="key is not the base64 of 32 bytes"):
                parse(replace_line(sign("slack"), "key:", f"key: {value}"))

    def test_base64_must_be_canonical(self):
        key = base64.b64encode(public_bytes(fixture_key())).decode()
        # The last symbol before the padding carries two unused bits: another spelling of the same key.
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        index = alphabet.index(key[-2])
        variant = key[:-2] + alphabet[index ^ 1] + "="
        assert base64.b64decode(variant, validate=True) == base64.b64decode(key)
        with pytest.raises(ConnectorConfigError, match="key is not the base64"):
            parse(replace_line(sign("slack"), "key:", f"key: {variant}"))

    def test_signature_must_be_64_bytes(self):
        with pytest.raises(ConnectorConfigError, match="signature is not the base64 of 64 bytes"):
            parse(replace_line(sign("slack"), "signature:", f"signature: {base64.b64encode(b'x' * 63).decode()}"))

    def test_messages_never_hold_a_value(self):
        for raw in (
            replace_line(sign("slack"), "client_id:", "client_id: " + "a" * 513),
            replace_line(sign("slack"), "client_secret:", f"scope: {SENTINEL}"),
            replace_line(sign("slack"), "client_secret:", f"client_secret: {SENTINEL} "),
            sign("slack") + SENTINEL.encode() + b"\n",
        ):
            with pytest.raises(ConnectorConfigError) as excinfo:
                parse(raw)
            assert SENTINEL not in str(excinfo.value)


# ---------------------------------------------------------------------------- #
# verify and fingerprint
# ---------------------------------------------------------------------------- #

class TestVerify:
    def test_good_file(self):
        assert verify(parse(sign("slack")), public_bytes(fixture_key()))

    @pytest.mark.parametrize("old, new", [
        (b"slack-id", b"slack-ie"), (b"serial: 1", b"serial: 2"), (b"2026-11-02T10", b"2026-11-02T11"),
        (SENTINEL.encode(), SENTINEL.encode()[:-1] + b"!"), (b"provider: slack", b"provider: miro"),
    ])
    def test_a_changed_byte_does_not_verify(self, old, new):
        raw = sign("slack").replace(old, new)
        assert raw != sign("slack")
        assert not verify(parse(raw), public_bytes(fixture_key()))

    def test_a_changed_signature_does_not_verify(self):
        lines = sign("slack").decode().split("\n")
        signature = bytearray(base64.b64decode(lines[-2].removeprefix("signature: ")))
        signature[0] ^= 1
        lines[-2] = "signature: " + base64.b64encode(bytes(signature)).decode()
        assert not verify(parse("\n".join(lines).encode()), public_bytes(fixture_key()))

    def test_another_key_does_not_verify(self):
        other = public_bytes(Ed25519PrivateKey.generate())
        assert not verify(parse(sign("slack")), other)

    def test_a_key_of_the_wrong_size_does_not_verify(self):
        assert not verify(parse(sign("slack")), b"short")

    def test_fingerprint_is_openssh_style(self):
        text = fingerprint(public_bytes(fixture_key()))
        assert text.startswith("SHA256:") and "=" not in text and len(text) == 7 + 43


# ---------------------------------------------------------------------------- #
# accept
# ---------------------------------------------------------------------------- #

class TestAccept:
    def test_first_upload_asks_for_confirmation_and_stores_nothing(self, org):
        with pytest.raises(ConfirmationRequired) as excinfo:
            accept(org, sign("slack"))
        assert excinfo.value.fingerprint == KEY_FP
        assert KEY_FP in str(excinfo.value)
        assert list(org.iterdir()) == []

    @pytest.mark.parametrize("answer", ["", "SHA256:other", KEY_FP.lower(), KEY_FP + " "])
    def test_a_wrong_confirmation_pins_nothing(self, org, answer):
        with pytest.raises(ConfirmationRequired):
            accept(org, sign("slack"), confirmed_fingerprint=answer)
        assert list(org.iterdir()) == []

    def test_confirmed_upload_pins_stores_unchanged_and_records_the_serial(self, org):
        raw = sign("slack", 5)
        result = accept(org, raw, confirmed_fingerprint=KEY_FP)
        assert (result.provider, result.serial, result.fingerprint, result.newly_pinned) == ("slack", 5, KEY_FP, True)
        stored = connector_configs.stored_path(org, "slack")
        assert stored.read_bytes() == raw
        assert stat.S_IMODE(stored.stat().st_mode) == 0o600
        assert connector_configs.load_pin(org) == public_bytes(fixture_key())
        assert stat.S_IMODE(connector_configs.pinned_key_path(org).stat().st_mode) == 0o600
        assert json.loads(connector_configs.serials_path(org).read_text()) == {"slack": 5}

    def test_the_pinned_key_needs_no_confirmation_again(self, org):
        confirmed(org, "slack")
        result = accept(org, sign("google"))
        assert result.newly_pinned is False
        assert json.loads(connector_configs.serials_path(org).read_text()) == {"slack": 1, "google": 1}

    def test_a_confirmation_is_ignored_once_a_key_is_pinned(self, org):
        confirmed(org)
        assert accept(org, sign("miro"), confirmed_fingerprint="SHA256:whatever").newly_pinned is False

    def test_another_key_is_refused_and_nothing_changes(self, org):
        confirmed(org)
        other = Ed25519PrivateKey.generate()
        before = sorted(p.name for p in org.rglob("*"))
        with pytest.raises(ConnectorConfigError, match="another key") as excinfo:
            accept(org, sign("google", key=other), confirmed_fingerprint=fingerprint(public_bytes(other)))
        assert str(connector_configs.pinned_key_path(org)) in str(excinfo.value)
        assert sorted(p.name for p in org.rglob("*")) == before
        assert connector_configs.load_pin(org) == public_bytes(fixture_key())

    def test_a_tampered_file_under_the_pin_is_refused(self, org):
        confirmed(org)
        with pytest.raises(ConnectorConfigError, match="does not verify against the key this server trusts"):
            accept(org, sign("google", 2).replace(b"google-id", b"evil-id"))

    def test_a_tampered_first_file_is_refused_before_asking_for_confirmation(self, org):
        with pytest.raises(ConnectorConfigError, match="own key") as excinfo:
            accept(org, sign("slack").replace(b"slack-id", b"evil-id"), confirmed_fingerprint=KEY_FP)
        assert not isinstance(excinfo.value, ConfirmationRequired)
        assert list(org.iterdir()) == []

    def test_a_file_signed_by_someone_else_cannot_pin_without_matching_its_own_key(self, org):
        other = Ed25519PrivateKey.generate()
        raw = sign("slack", key=other)
        # Swap in the fixture key's public key: the signature then fails against it.
        forged = replace_line(raw, "key:", "key: " + base64.b64encode(public_bytes(fixture_key())).decode())
        with pytest.raises(ConnectorConfigError, match="own key"):
            accept(org, forged, confirmed_fingerprint=KEY_FP)

    @pytest.mark.parametrize("serial", [5, 4, 1])
    def test_an_equal_or_lower_serial_is_refused(self, org, serial):
        confirmed(org, "slack", 5)
        with pytest.raises(ConnectorConfigError, match=f"serial {serial} is not higher than 5"):
            accept(org, sign("slack", serial))
        assert parse(connector_configs.stored_path(org, "slack").read_bytes()).serial == 5

    def test_a_higher_serial_replaces_the_file(self, org):
        confirmed(org, "slack", 5)
        accept(org, sign("slack", 6))
        assert parse(connector_configs.stored_path(org, "slack").read_bytes()).serial == 6

    def test_serials_are_per_provider(self, org):
        confirmed(org, "slack", 9)
        accept(org, sign("google", 1))

    @pytest.mark.parametrize("serial", [5, 3])
    def test_serial_protection_survives_removal(self, org, serial):
        confirmed(org, "slack", 5)
        remove(org, "slack")
        assert not connector_configs.stored_path(org, "slack").exists()
        with pytest.raises(ConnectorConfigError, match="not higher than 5"):
            accept(org, sign("slack", serial))
        accept(org, sign("slack", 6))

    def test_the_serial_is_checked_before_a_first_key_is_confirmed(self, org):
        connector_configs.serials_path(org).write_text(json.dumps({"slack": 8}))
        with pytest.raises(ConnectorConfigError, match="not higher than 8") as excinfo:
            accept(org, sign("slack", 2))
        assert not isinstance(excinfo.value, ConfirmationRequired)

    def test_an_unreadable_serials_file_refuses(self, org):
        confirmed(org)
        for content in ("not json", "[1]", '{"slack": 0}', '{"slack": true}', '{"slack": "3"}', '{"mars": 3}'):
            connector_configs.serials_path(org).write_text(content)
            with pytest.raises(ConnectorConfigError, match="serials .* cannot be read"):
                accept(org, sign("slack", 9))

    def test_an_unreadable_pin_refuses_instead_of_trusting_a_new_key(self, org):
        confirmed(org)
        other = Ed25519PrivateKey.generate()
        for content in ("", "!!!", base64.b64encode(b"short").decode()):
            connector_configs.pinned_key_path(org).write_text(content)
            with pytest.raises(ConnectorConfigError, match="cannot be read"):
                accept(org, sign("google", key=other), confirmed_fingerprint=fingerprint(public_bytes(other)))

    def test_a_later_upload_replaces_an_imported_config(self, org):
        adopt(org, public_bytes(fixture_key()))
        import_from_legacy_bundle(org, {"slack": {"client_id": "old", "client_secret": "old-secret"}}, BUNDLE_SHA)
        assert connector_configs.legacy_path(org, "slack").exists()
        accept(org, sign("slack"))
        assert not connector_configs.legacy_path(org, "slack").exists()
        assert load_all(org)["slack"].source == connector_configs.UPLOADED_SOURCE

    def test_nothing_secret_reaches_a_log_or_a_message(self, org, caplog):
        caplog.set_level(logging.DEBUG)
        other = Ed25519PrivateKey.generate()
        messages = []
        for action in (
            lambda: accept(org, sign("slack")),
            lambda: accept(org, sign("slack"), confirmed_fingerprint=KEY_FP),
            lambda: accept(org, sign("slack", 1)),
            lambda: accept(org, sign("google", key=other)),
            lambda: accept(org, sign("google").replace(b"google-id", b"x-id")),
        ):
            try:
                result = action()
            except ConnectorConfigError as exc:
                messages.append(str(exc))
            else:
                messages.append(repr(result))
        assert messages and all(SENTINEL not in message for message in messages)
        assert SENTINEL not in caplog.text


# ---------------------------------------------------------------------------- #
# load_all
# ---------------------------------------------------------------------------- #

class TestLoadAll:
    def test_nothing_stored(self, org):
        assert load_all(org) == {}

    def test_empty_store_directory(self, org):
        connector_configs.store_dir(org).mkdir()
        assert load_all(org) == {}

    def test_loads_each_uploaded_config(self, org):
        confirmed(org, "slack", 3)
        accept(org, sign("salesforce", 4, login_url="https://acme.my.salesforce.com"))
        loaded = load_all(org)
        assert sorted(loaded) == ["salesforce", "slack"]
        slack = loaded["slack"]
        assert (slack.source, slack.serial, slack.signed_at, slack.fingerprint) == (
            "connector-config", 3, "2026-11-02T10:00:00Z", KEY_FP,
        )
        assert slack.client_id == "slack-id"
        assert slack.section() == {"client_id": "slack-id", "client_secret": SENTINEL}
        assert loaded["salesforce"].client_id == "salesforce-id"
        assert loaded["salesforce"].section()["login_url"] == "https://acme.my.salesforce.com"

    def test_the_section_is_a_copy(self, org):
        confirmed(org)
        loaded = load_all(org)["slack"]
        loaded.section()["client_id"] = "changed"
        assert loaded.section()["client_id"] == "slack-id"

    def test_a_changed_byte_refuses_the_start_naming_the_provider(self, org):
        confirmed(org, "slack")
        accept(org, sign("google"))
        path = connector_configs.stored_path(org, "google")
        path.write_bytes(path.read_bytes().replace(b"google-id", b"google-ie"))
        with pytest.raises(ConfigurationError, match="stored google connector config no longer verifies"):
            load_all(org)

    def test_a_file_signed_with_another_key_refuses(self, org):
        confirmed(org)
        other = Ed25519PrivateKey.generate()
        connector_configs.stored_path(org, "slack").write_bytes(sign("slack", 9, key=other))
        with pytest.raises(ConnectorConfigError, match="stored slack connector config no longer verifies"):
            load_all(org)

    def test_another_providers_file_under_this_name_refuses(self, org):
        confirmed(org, "slack")
        accept(org, sign("google"))
        connector_configs.stored_path(org, "google").write_bytes(sign("slack", 9))
        with pytest.raises(ConnectorConfigError, match="stored google connector config is a slack config"):
            load_all(org)

    def test_an_older_valid_file_swapped_in_is_a_rollback(self, org):
        confirmed(org, "slack", 1)
        old = connector_configs.stored_path(org, "slack").read_bytes()
        accept(org, sign("slack", 2))
        connector_configs.stored_path(org, "slack").write_bytes(old)
        with pytest.raises(ConnectorConfigError, match="stored slack connector config is older than the newest"):
            load_all(org)

    def test_a_malformed_stored_file_refuses_without_echoing_it(self, org):
        confirmed(org)
        connector_configs.stored_path(org, "slack").write_bytes(b"client_secret: " + SENTINEL.encode() + b"\n")
        with pytest.raises(ConnectorConfigError, match="stored slack connector config is malformed") as excinfo:
            load_all(org)
        assert SENTINEL not in str(excinfo.value)

    def test_an_unreadable_stored_file_refuses(self, org):
        confirmed(org)
        connector_configs.stored_path(org, "slack").unlink()
        connector_configs.stored_path(org, "slack").mkdir()
        with pytest.raises(ConnectorConfigError, match="stored slack connector config cannot be read"):
            load_all(org)

    def test_a_stored_file_with_no_pin_refuses(self, org):
        confirmed(org)
        connector_configs.pinned_key_path(org).unlink()
        with pytest.raises(ConnectorConfigError, match="no trusted signing key"):
            load_all(org)

    def test_an_unreadable_pin_refuses(self, org):
        confirmed(org)
        connector_configs.pinned_key_path(org).write_text("garbage")
        with pytest.raises(ConnectorConfigError, match="pinned connector config signing key"):
            load_all(org)

    def test_an_unreadable_serials_file_refuses(self, org):
        confirmed(org)
        connector_configs.serials_path(org).write_text("{")
        with pytest.raises(ConnectorConfigError, match="serials"):
            load_all(org)

    def test_a_stored_file_never_leaks_through_its_error(self, org):
        confirmed(org)
        path = connector_configs.stored_path(org, "slack")
        path.write_bytes(path.read_bytes().replace(b"slack-id", b"slack-ie"))
        with pytest.raises(ConnectorConfigError) as excinfo:
            load_all(org)
        assert SENTINEL not in str(excinfo.value)


class TestLoadImported:
    def write(self, org, provider="slack", **replace):
        data = {"source": "legacy-bundle", "provider": provider, "sha256": BUNDLE_SHA,
                "fields": {"client_id": "old", "client_secret": SENTINEL}}
        data.update(replace)
        path = connector_configs.legacy_path(org, provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_loads_without_any_signature_or_pin(self, org):
        self.write(org)
        loaded = load_all(org)["slack"]
        assert (loaded.source, loaded.serial, loaded.fingerprint, loaded.client_id) == ("legacy-bundle", None, "", "old")
        assert loaded.section() == {"client_id": "old", "client_secret": SENTINEL}

    def test_a_signed_file_wins(self, org):
        confirmed(org, "slack")
        self.write(org)
        assert load_all(org)["slack"].source == "connector-config"

    @pytest.mark.parametrize("change", [
        {"source": "upload"}, {"fields": "x"}, {"fields": {"client_id": "old"}},
        {"fields": {"client_id": "old", "client_secret": ""}}, {"fields": {"client_id": 3, "client_secret": "s"}},
    ])
    def test_a_broken_imported_file_refuses(self, org, change):
        self.write(org, **change)
        with pytest.raises(ConnectorConfigError, match="imported slack connector config is unreadable") as excinfo:
            load_all(org)
        assert SENTINEL not in str(excinfo.value)

    @pytest.mark.parametrize("content", ["not json", "[]", ""])
    def test_a_file_that_is_not_an_object_refuses(self, org, content):
        connector_configs.store_dir(org).mkdir()
        connector_configs.legacy_path(org, "slack").write_text(content)
        with pytest.raises(ConnectorConfigError, match="imported slack connector config is unreadable"):
            load_all(org)


# ---------------------------------------------------------------------------- #
# remove
# ---------------------------------------------------------------------------- #

class TestRemove:
    def test_removes_the_file_and_keeps_the_serials(self, org):
        confirmed(org, "slack", 4)
        assert remove(org, "slack") is True
        assert load_all(org) == {}
        assert json.loads(connector_configs.serials_path(org).read_text()) == {"slack": 4}
        assert connector_configs.pinned_key_path(org).exists()

    def test_removes_an_imported_config(self, org):
        adopt(org)
        import_from_legacy_bundle(org, {"miro": {"client_id": "i", "client_secret": "s"}}, BUNDLE_SHA)
        assert remove(org, "miro") is True
        assert load_all(org) == {}

    def test_nothing_to_remove(self, org):
        assert remove(org, "slack") is False

    def test_unknown_provider(self, org):
        with pytest.raises(ConnectorConfigError, match="not a provider"):
            remove(org, "../org_bundle")


# ---------------------------------------------------------------------------- #
# the legacy bundle
# ---------------------------------------------------------------------------- #

LEGACY_BUNDLE = {
    "mode": "org",
    "google": {"client_id": "g-id", "client_secret": "g-secret", "auth_uri": "https://a", "token_uri": "https://t"},
    "slack": {"client_id": "s-id", "client_secret": SENTINEL, "user_scopes": ["search:read"], "relay": True},
    "salesforce": {"consumer_key": "sf-key", "consumer_secret": "sf-secret", "login_url": "https://x.my.salesforce.com"},
    "atlassian": {"client_id": "a-id", "client_secret": "a-secret", "assets": True},
    "miro": {"client_id": "m-id", "client_secret": "m-secret"},
    "grist": {"client_id": "gr-id", "client_secret": "gr-secret", "auth_server_url": "https://a", "server_url": "https://g"},
}


class TestImportFromLegacyBundle:
    def test_each_credential_section_is_stored_once_with_the_bundle_hash(self, org):
        adopt(org)
        assert import_from_legacy_bundle(org, LEGACY_BUNDLE, BUNDLE_SHA) == list(connector_configs.PROVIDERS)
        slack = json.loads(connector_configs.legacy_path(org, "slack").read_text())
        assert slack == {
            "source": "legacy-bundle", "provider": "slack", "sha256": BUNDLE_SHA,
            "fields": {"client_id": "s-id", "client_secret": SENTINEL, "user_scopes": ["search:read"]},
        }
        assert stat.S_IMODE(connector_configs.legacy_path(org, "slack").stat().st_mode) == 0o600
        loaded = load_all(org)
        assert sorted(loaded) == sorted(connector_configs.PROVIDERS)
        assert loaded["google"].section()["token_uri"] == "https://t"
        assert loaded["atlassian"].section() == {"client_id": "a-id", "client_secret": "a-secret"}
        assert loaded["salesforce"].section()["login_url"] == "https://x.my.salesforce.com"
        assert record_of(org)["connector_import_done"] is True

    def test_it_is_not_repeated(self, org):
        adopt(org)
        import_from_legacy_bundle(org, LEGACY_BUNDLE, BUNDLE_SHA)
        assert import_from_legacy_bundle(org, LEGACY_BUNDLE, BUNDLE_SHA) == []

    def test_a_removed_config_does_not_come_back(self, org):
        adopt(org)
        import_from_legacy_bundle(org, LEGACY_BUNDLE, BUNDLE_SHA)
        remove(org, "slack")
        import_from_legacy_bundle(org, LEGACY_BUNDLE, BUNDLE_SHA)
        assert "slack" not in load_all(org)

    def test_a_bundle_that_is_not_the_adopted_one_imports_nothing(self, org):
        adopt(org)
        assert import_from_legacy_bundle(org, LEGACY_BUNDLE, "b" * 64) == []
        assert not connector_configs.store_dir(org).exists()
        assert "connector_import_done" not in record_of(org)

    @pytest.mark.parametrize("sha", ["", None])
    def test_no_hash_imports_nothing(self, org, sha):
        adopt(org, sha=sha or "")
        assert import_from_legacy_bundle(org, LEGACY_BUNDLE, sha or "") == []

    def test_a_server_that_adopted_nothing_imports_nothing(self, org):
        assert import_from_legacy_bundle(org, LEGACY_BUNDLE, BUNDLE_SHA) == []

    def test_sections_that_would_not_load_are_skipped(self, org):
        adopt(org)
        bundle = {
            "google": "x",
            "slack": {"client_id": "s-id"},
            "miro": {"client_id": "m-id", "client_secret": ""},
            "grist": {"server_url": "https://g"},
            "atlassian": {"client_id": 7, "client_secret": "s"},
            "salesforce": {"consumer_key": "k", "consumer_secret": "s"},
        }
        assert import_from_legacy_bundle(org, bundle, BUNDLE_SHA) == ["salesforce"]
        assert list(load_all(org)) == ["salesforce"]

    def test_an_existing_config_is_kept(self, org):
        adopt(org, public_bytes(fixture_key()))
        accept(org, sign("slack", 4))
        connector_configs.legacy_path(org, "google").parent.mkdir(exist_ok=True)
        connector_configs.legacy_path(org, "google").write_text(json.dumps(
            {"source": "legacy-bundle", "fields": {"client_id": "kept", "client_secret": "kept"}}
        ))
        assert import_from_legacy_bundle(org, LEGACY_BUNDLE, BUNDLE_SHA) == [
            "salesforce", "atlassian", "miro", "grist",
        ]
        loaded = load_all(org)
        assert loaded["slack"].source == "connector-config"
        assert loaded["google"].client_id == "kept"

    def test_nothing_is_logged_with_a_secret(self, org, caplog):
        caplog.set_level(logging.DEBUG)
        adopt(org)
        import_from_legacy_bundle(org, LEGACY_BUNDLE, BUNDLE_SHA)
        assert "slack" in caplog.text and SENTINEL not in caplog.text

    def test_nothing_to_import_logs_nothing(self, org, caplog):
        caplog.set_level(logging.DEBUG)
        adopt(org)
        assert import_from_legacy_bundle(org, {"mode": "org"}, BUNDLE_SHA) == []
        assert caplog.text == ""
        assert record_of(org)["connector_import_done"] is True

    def test_a_failed_record_write_is_an_error(self, org, monkeypatch):
        adopt(org)

        def fail(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(connector_configs, "atomic_write_json", fail)
        with pytest.raises(ConnectorConfigError, match="Could not write"):
            import_from_legacy_bundle(org, {"mode": "org"}, BUNDLE_SHA)


class TestPinFromTheBundle:
    def test_the_bundles_pinned_key_becomes_the_pin_when_the_credentials_are_imported(self, org):
        adopt(org, public_bytes(fixture_key()))
        import_from_legacy_bundle(org, {"mode": "org"}, BUNDLE_SHA)
        assert connector_configs.load_pin(org) == public_bytes(fixture_key())
        assert record_of(org)["connector_pin_seeded"] is True

    def test_the_bundles_key_is_trusted_before_any_import(self, org):
        adopt(org, public_bytes(fixture_key()))
        result = accept(org, sign("slack"))
        assert result.newly_pinned is False
        assert connector_configs.load_pin(org) == public_bytes(fixture_key())

    def test_another_key_is_refused_on_an_upgraded_server(self, org):
        adopt(org, public_bytes(fixture_key()))
        other = Ed25519PrivateKey.generate()
        with pytest.raises(ConnectorConfigError, match="another key"):
            accept(org, sign("slack", key=other), confirmed_fingerprint=fingerprint(public_bytes(other)))
        assert not connector_configs.stored_path(org, "slack").exists()

    def test_stored_files_verify_against_the_seeded_pin(self, org):
        adopt(org, public_bytes(fixture_key()))
        connector_configs.stored_path(org, "slack").parent.mkdir()
        connector_configs.stored_path(org, "slack").write_bytes(sign("slack"))
        assert load_all(org)["slack"].source == "connector-config"

    def test_deleting_the_pin_rotates_the_key_and_the_bundle_key_is_not_brought_back(self, org):
        adopt(org, public_bytes(fixture_key()))
        accept(org, sign("slack"))
        connector_configs.pinned_key_path(org).unlink()
        other = Ed25519PrivateKey.generate()
        with pytest.raises(ConfirmationRequired):
            accept(org, sign("google", key=other))
        result = accept(org, sign("google", key=other), confirmed_fingerprint=fingerprint(public_bytes(other)))
        assert result.newly_pinned is True

    @pytest.mark.parametrize("bundle_key", [None, b"short"])
    def test_a_bundle_without_a_usable_pin_seeds_nothing(self, org, bundle_key):
        adopt(org, bundle_key)
        with pytest.raises(ConfirmationRequired):
            accept(org, sign("slack"))
        assert "connector_pin_seeded" not in record_of(org)

    def test_a_server_that_adopted_no_bundle_seeds_nothing(self, org):
        (org / org_bundle_signing.PINNED_PUBKEY_FILENAME).write_text(
            base64.b64encode(public_bytes(fixture_key())).decode() + "\n"
        )
        with pytest.raises(ConfirmationRequired):
            accept(org, sign("slack"))
        assert not (org / "org_bundle_adopted.json").exists()


class TestDefaultOrgDir:
    def test_it_is_the_organization_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr(connector_configs, "org_dir", lambda: tmp_path / "org")
        assert connector_configs.default_org_dir() == tmp_path / "org"


def test_signed_text_matches_the_modules_own_layout():
    raw = signed_text(
        "salesforce", 3, [("consumer_key", "k"), ("consumer_secret", "s")], "2026-11-02T10:00:00Z", fixture_key()
    )
    assert parse(raw).fields == {"consumer_key": "k", "consumer_secret": "s"}
