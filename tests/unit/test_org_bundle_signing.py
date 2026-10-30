"""Tests for org_bundle_signing.py -- the org-config bundle's Ed25519
signature and trust-on-first-use verification core (ADR 0016), shared by daemon_main.load_org_config and
settings_controller.install_org_config_bytes. See that module's own
docstring for the model these tests exercise directly (test_daemon_main.py
and test_settings_controller.py cover the two call sites' own wiring)."""
from __future__ import annotations

import base64

import sys

import pytest

from privacyfence import org_bundle_signing as obs


def _signed(bundle: dict, private_key=None):
    if private_key is None:
        private_key, _ = obs.generate_keypair()
    return obs.sign_bundle(bundle, private_key), private_key


class TestShaHex:
    def test_matches_hashlib(self):
        import hashlib

        assert obs.sha256_hex(b"hello") == hashlib.sha256(b"hello").hexdigest()


class TestSignAndVerifyRoundTrip:
    def test_freshly_signed_bundle_verifies_against_its_own_key(self, tmp_path):
        signed, _ = _signed({"mode": "org", "org_name": "Acme"})
        trust = obs.verify_and_maybe_pin(signed, tmp_path)
        assert trust.ok
        assert trust.signed
        assert trust.newly_pinned

    def test_signing_is_deterministic_over_field_order(self):
        private_key, _ = obs.generate_keypair()
        a, _ = _signed({"a": 1, "b": 2}, private_key)
        b, _ = _signed({"b": 2, "a": 1}, private_key)
        assert a["signature"] == b["signature"]

    def test_re_signing_replaces_any_prior_signature(self):
        private_key, _ = obs.generate_keypair()
        once, _ = _signed({"org_name": "Acme"}, private_key)
        twice = obs.sign_bundle(once, private_key)
        assert twice["signature"] == once["signature"]  # same payload -> same signature
        assert "signature" in twice


class TestVerifyAndMaybePin:
    def test_unsigned_bundle_passes_when_nothing_pinned_yet(self, tmp_path):
        trust = obs.verify_and_maybe_pin({"slack": {"client_id": "x"}}, tmp_path)
        assert trust.ok
        assert not trust.signed
        assert not trust.newly_pinned
        assert obs.load_pinned_public_key(tmp_path) is None

    def test_first_signed_bundle_pins_its_key(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(signed, tmp_path)
        assert obs.load_pinned_public_key(tmp_path) is not None

    def test_pinned_key_matches_the_signing_public_key_field(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(signed, tmp_path)
        pinned = obs.load_pinned_public_key(tmp_path)
        assert base64.b64encode(pinned).decode("ascii") == signed["signing_public_key"]

    def test_subsequent_identical_bundle_verifies_without_re_pinning(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        first = obs.verify_and_maybe_pin(signed, tmp_path)
        second = obs.verify_and_maybe_pin(signed, tmp_path)
        assert first.newly_pinned
        assert second.ok and not second.newly_pinned

    def test_tampered_payload_after_pin_fails(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(signed, tmp_path)

        tampered = dict(signed)
        tampered["org_name"] = "Evil Corp"
        trust = obs.verify_and_maybe_pin(tampered, tmp_path)

        assert not trust.ok

    def test_bundle_signed_by_a_different_key_after_pin_fails(self, tmp_path):
        first, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(first, tmp_path)

        second, _ = _signed({"org_name": "Acme"})  # fresh (different) keypair
        trust = obs.verify_and_maybe_pin(second, tmp_path)

        assert not trust.ok

    def test_downgrade_to_unsigned_after_pin_fails(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(signed, tmp_path)

        trust = obs.verify_and_maybe_pin({"org_name": "Acme"}, tmp_path)

        assert not trust.ok

    def test_signature_without_matching_embedded_key_is_rejected_pre_pin(self, tmp_path):
        trust = obs.verify_and_maybe_pin({"signature": base64.b64encode(b"x" * 64).decode()}, tmp_path)
        assert not trust.ok

    def test_garbage_signing_public_key_is_rejected_pre_pin(self, tmp_path):
        trust = obs.verify_and_maybe_pin(
            {"signature": base64.b64encode(b"x" * 64).decode(), "signing_public_key": "not-base64!!"},
            tmp_path,
        )
        assert not trust.ok

    def test_self_signed_with_invalid_signature_is_rejected_pre_pin(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        signed["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
        trust = obs.verify_and_maybe_pin(signed, tmp_path)
        assert not trust.ok
        assert obs.load_pinned_public_key(tmp_path) is None  # never pinned on a failed self-check


class TestWouldPinNewKey:
    """A pure duplicate of verify_and_maybe_pin's own "first signed bundle"
    branch condition, with no disk
    side effect -- see that function's own docstring for why it's kept
    deliberately independent rather than sharing a helper."""

    def test_true_for_a_fresh_signed_bundle(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        assert obs.would_pin_new_key(signed, tmp_path) is True
        # Read-only -- checking must not itself pin anything.
        assert obs.load_pinned_public_key(tmp_path) is None

    def test_false_for_an_unsigned_bundle(self, tmp_path):
        assert obs.would_pin_new_key({"org_name": "Acme"}, tmp_path) is False

    def test_false_once_a_key_is_already_pinned(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(signed, tmp_path)

        assert obs.would_pin_new_key(signed, tmp_path) is False

    def test_false_for_a_self_signed_bundle_with_an_invalid_signature(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        signed["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
        assert obs.would_pin_new_key(signed, tmp_path) is False

    def test_false_for_garbage_signing_public_key(self, tmp_path):
        bundle = {"signature": base64.b64encode(b"x" * 64).decode(), "signing_public_key": "not-base64!!"}
        assert obs.would_pin_new_key(bundle, tmp_path) is False

    def test_false_for_a_signature_with_no_embedded_key_at_all(self, tmp_path):
        bundle = {"signature": base64.b64encode(b"x" * 64).decode()}
        assert obs.would_pin_new_key(bundle, tmp_path) is False

    def test_matches_verify_and_maybe_pins_own_newly_pinned_flag(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        predicted = obs.would_pin_new_key(signed, tmp_path)
        trust = obs.verify_and_maybe_pin(signed, tmp_path)
        assert predicted == trust.newly_pinned


class TestPinnedPublicKeyFile:
    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_pin_file_is_written_with_restrictive_permissions(self, tmp_path):
        import stat

        signed, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(signed, tmp_path)

        path = obs.pinned_public_key_path(tmp_path)
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_corrupted_pin_file_is_treated_as_unpinned(self, tmp_path):
        # load_pinned_public_key's own fail-closed-to-"nothing pinned"
        # path -- a truncated write, a hand-edit, or disk corruption
        # leaves this file present but not valid base64; it must not
        # raise, and must not be mistaken for a real pinned key.
        obs.pinned_public_key_path(tmp_path).write_text("not valid base64!!", encoding="utf-8")
        assert obs.load_pinned_public_key(tmp_path) is None

    def test_deleting_the_pin_file_allows_a_new_key_to_be_trusted(self, tmp_path):
        first, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(first, tmp_path)

        obs.pinned_public_key_path(tmp_path).unlink()

        second, _ = _signed({"org_name": "Acme"})  # different keypair
        trust = obs.verify_and_maybe_pin(second, tmp_path)
        assert trust.ok
        assert trust.newly_pinned


class TestBundleSerial:
    """The serial pin (SEC-A7): a validly signed bundle older than one this
    install has already accepted is a rollback and is refused."""

    @staticmethod
    def _accept(bundle_key_pair, serial, tmp_path):
        signed, key = _signed({"org_name": "Acme", "serial": serial}, bundle_key_pair)
        trust = obs.verify_and_maybe_pin(signed, tmp_path)
        obs.record_accepted_serial(trust, tmp_path)
        return trust, key

    def test_a_lower_serial_than_the_pinned_one_is_refused_as_a_rollback(self, tmp_path):
        key, _ = obs.generate_keypair()
        self._accept(key, 200, tmp_path)

        older, _ = _signed({"org_name": "Acme", "serial": 100}, key)
        trust = obs.verify_and_maybe_pin(older, tmp_path)

        assert not trust.ok
        assert trust.remedy == "serial"
        assert "rollback refused" in trust.detail
        assert "serial 100 is older than serial 200" in trust.detail

    def test_the_same_serial_is_accepted_on_reload(self, tmp_path):
        key, _ = obs.generate_keypair()
        self._accept(key, 200, tmp_path)

        again, _ = _signed({"org_name": "Acme", "serial": 200}, key)
        trust = obs.verify_and_maybe_pin(again, tmp_path)

        assert trust.ok
        assert trust.serial == 200

    def test_a_bundle_without_a_serial_is_refused_once_a_serial_is_pinned(self, tmp_path):
        key, _ = obs.generate_keypair()
        self._accept(key, 200, tmp_path)

        serial_less, _ = _signed({"org_name": "Acme"}, key)
        trust = obs.verify_and_maybe_pin(serial_less, tmp_path)

        assert not trust.ok
        assert trust.remedy == "serial"
        assert trust.detail == "this bundle carries no serial, but this install has already accepted serial 200"

    def test_a_bundle_without_a_serial_is_accepted_while_none_is_pinned(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})

        trust = obs.verify_and_maybe_pin(signed, tmp_path)

        assert trust.ok
        assert trust.serial is None
        obs.record_accepted_serial(trust, tmp_path)
        assert not obs.pinned_serial_path(tmp_path).exists()

    @pytest.mark.parametrize("bad", [True, False, 0, -5, "7", 1.5])
    @pytest.mark.parametrize("pinned_first", [False, True])
    def test_a_boolean_or_non_positive_serial_is_refused_with_no_remedy(self, tmp_path, bad, pinned_first):
        key, _ = obs.generate_keypair()
        if pinned_first:
            self._accept(key, 5, tmp_path)

        signed, _ = _signed({"org_name": "Acme", "serial": bad}, key)
        trust = obs.verify_and_maybe_pin(signed, tmp_path)

        assert not trust.ok
        assert trust.remedy == "none"
        assert trust.detail == "serial is not a positive integer"

    @pytest.mark.parametrize("content", ["abc\n", "0\n", "-3\n", "", "٣\n"])
    def test_an_unreadable_serial_pin_fails_closed_with_the_serial_remedy(self, tmp_path, content):
        key, _ = obs.generate_keypair()
        self._accept(key, 5, tmp_path)
        obs.pinned_serial_path(tmp_path).write_text(content, encoding="utf-8")

        with pytest.raises(ValueError):
            obs.load_pinned_serial(tmp_path)
        signed, _ = _signed({"org_name": "Acme", "serial": 9}, key)
        trust = obs.verify_and_maybe_pin(signed, tmp_path)

        assert not trust.ok
        assert trust.remedy == "serial"
        assert trust.detail == "the pinned serial file is unreadable"

    def test_a_pin_path_that_cannot_be_read_fails_closed(self, tmp_path):
        obs.pinned_serial_path(tmp_path).mkdir()

        with pytest.raises(ValueError, match="cannot read"):
            obs.load_pinned_serial(tmp_path)

    def test_a_rollback_bundle_with_a_new_key_does_not_pin_the_key(self, tmp_path):
        key, _ = obs.generate_keypair()
        self._accept(key, 200, tmp_path)
        obs.pinned_public_key_path(tmp_path).unlink()  # key rotation, serial pin kept

        older, _ = _signed({"org_name": "Acme", "serial": 100})  # new key, older serial
        trust = obs.verify_and_maybe_pin(older, tmp_path)

        assert not trust.ok
        assert "rollback refused" in trust.detail
        assert obs.load_pinned_public_key(tmp_path) is None

    def test_a_first_signed_bundle_with_a_serial_pins_the_key_and_reports_the_serial(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme", "serial": 7})

        trust = obs.verify_and_maybe_pin(signed, tmp_path)

        assert trust.ok and trust.newly_pinned
        assert trust.serial == 7
        assert not obs.pinned_serial_path(tmp_path).exists()  # recording is the caller's job

    def test_record_accepted_serial_only_raises_the_pin(self, tmp_path):
        record = obs.record_accepted_serial
        record(obs.BundleTrust(ok=True, detail="", serial=10), tmp_path)
        assert obs.load_pinned_serial(tmp_path) == 10
        record(obs.BundleTrust(ok=True, detail="", serial=5), tmp_path)
        assert obs.load_pinned_serial(tmp_path) == 10
        record(obs.BundleTrust(ok=True, detail="", serial=20), tmp_path)
        assert obs.load_pinned_serial(tmp_path) == 20
        assert obs.pinned_serial_path(tmp_path).read_text(encoding="utf-8") == "20\n"
        record(obs.BundleTrust(ok=False, detail="", serial=99), tmp_path)
        record(obs.BundleTrust(ok=True, detail="", serial=None), tmp_path)
        assert obs.load_pinned_serial(tmp_path) == 20

    def test_record_accepted_serial_swallows_a_write_error(self, tmp_path, monkeypatch, caplog):
        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(obs, "atomic_write_text", boom)

        obs.record_accepted_serial(obs.BundleTrust(ok=True, detail="", serial=3), tmp_path)

        assert "Could not record the accepted organization config serial" in caplog.text
        assert obs.load_pinned_serial(tmp_path) is None

    def test_record_accepted_serial_swallows_an_unreadable_pin(self, tmp_path, caplog):
        obs.pinned_serial_path(tmp_path).write_text("junk", encoding="utf-8")

        obs.record_accepted_serial(obs.BundleTrust(ok=True, detail="", serial=3), tmp_path)

        assert "Could not record the accepted organization config serial" in caplog.text

    def test_deleting_the_serial_pin_allows_an_older_bundle(self, tmp_path):
        key, _ = obs.generate_keypair()
        self._accept(key, 200, tmp_path)
        obs.pinned_serial_path(tmp_path).unlink()

        older, _ = _signed({"org_name": "Acme", "serial": 100}, key)
        trust = obs.verify_and_maybe_pin(older, tmp_path)

        assert trust.ok
        assert trust.serial == 100


def _legacy_install(org_dir, bundle: dict | None = None, *, serial: int | None = None, with_key: bool = True):
    """A 6.x data directory: org_config.json plus its two pin files."""
    import json

    org_dir.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(bundle if bundle is not None else {"mode": "local"}).encode("utf-8")
    (org_dir / "org_config.json").write_bytes(raw)
    if with_key:
        (org_dir / obs.LEGACY_PINNED_PUBKEY_FILENAME).write_text("a2V5\n")
    if serial is not None:
        (org_dir / obs.LEGACY_PINNED_SERIAL_FILENAME).write_text(f"{serial}\n")
    return raw


def _legacy_snapshot(org_dir):
    return {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in org_dir.iterdir()
        if p.name.startswith("org_config")
    }


class TestAdoptLegacyFiles:
    def test_new_names_are_the_7x_names(self):
        assert obs.PINNED_PUBKEY_FILENAME == "org_bundle_signing_pubkey.txt"
        assert obs.PINNED_SERIAL_FILENAME == "org_bundle_serial.txt"
        assert obs.LEGACY_PINNED_PUBKEY_FILENAME == "org_config_signing_pubkey.txt"
        assert obs.LEGACY_PINNED_SERIAL_FILENAME == "org_config_serial.txt"

    def test_adoption_copies_bundle_and_pins_and_writes_the_record(self, tmp_path, caplog):
        import json

        raw = _legacy_install(tmp_path, serial=3)
        before = _legacy_snapshot(tmp_path)

        with caplog.at_level("WARNING"):
            assert obs.adopt_legacy_files(tmp_path) is True

        assert (tmp_path / "org_bundle.json").read_bytes() == raw
        assert (tmp_path / obs.PINNED_PUBKEY_FILENAME).read_text() == "a2V5\n"
        assert (tmp_path / obs.PINNED_SERIAL_FILENAME).read_text() == "3\n"
        record = json.loads((tmp_path / "org_bundle_adopted.json").read_text())
        assert record["from"] == "org_config.json"
        assert record["sha256"] == obs.sha256_hex(raw)
        assert record["pins_adopted"] is True
        assert record["at"]
        assert "Copied org/org_config.json to org/org_bundle.json" in caplog.text
        # 7.x never touches the 6.x files, so a downgrade finds them as they were.
        assert _legacy_snapshot(tmp_path) == before

    def test_no_adoption_when_the_bundle_already_exists(self, tmp_path):
        _legacy_install(tmp_path)
        (tmp_path / "org_bundle.json").write_text('{"mode": "org"}')

        assert obs.adopt_legacy_files(tmp_path) is False
        assert (tmp_path / "org_bundle.json").read_text() == '{"mode": "org"}'
        # Only the pins were adopted: the record has no bundle part.
        record = obs.load_adopted_record(tmp_path)
        assert record["from"] == "" and record["sha256"] == ""

    def test_a_retired_bundle_is_not_adopted_again(self, tmp_path):
        import json

        raw = _legacy_install(tmp_path)
        assert obs.adopt_legacy_files(tmp_path) is True
        record = obs.load_adopted_record(tmp_path)
        record["retired"] = True
        (tmp_path / "org_bundle_adopted.json").write_text(json.dumps(record))
        (tmp_path / "org_bundle.json").unlink()

        assert obs.adopt_legacy_files(tmp_path) is False
        assert not (tmp_path / "org_bundle.json").exists()
        assert (tmp_path / "org_config.json").read_bytes() == raw

    def test_nothing_to_adopt_returns_false_and_writes_nothing(self, tmp_path):
        assert obs.adopt_legacy_files(tmp_path) is False
        assert list(tmp_path.iterdir()) == []

    def test_pins_are_copied_without_a_bundle_and_record_it(self, tmp_path):
        import json

        _legacy_install(tmp_path, serial=7)
        (tmp_path / "org_config.json").unlink()

        assert obs.adopt_legacy_files(tmp_path) is False

        assert (tmp_path / obs.PINNED_SERIAL_FILENAME).read_text() == "7\n"
        record = json.loads((tmp_path / "org_bundle_adopted.json").read_text())
        assert record["pins_adopted"] is True
        assert record["sha256"] == ""

    def test_failed_copy_raises_configuration_error_naming_the_file(self, tmp_path, monkeypatch):
        from privacyfence.org_mode import ConfigurationError

        _legacy_install(tmp_path)

        def boom(path, data, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(obs, "atomic_write_bytes", boom)
        with pytest.raises(ConfigurationError, match="org_config"):
            obs.adopt_legacy_files(tmp_path)
        assert (tmp_path / "org_config.json").exists()

    def test_pins_are_read_and_written_only_under_the_new_names_after_adoption(self, tmp_path):
        _legacy_install(tmp_path, serial=2)
        obs.adopt_legacy_files(tmp_path)
        legacy_before = _legacy_snapshot(tmp_path)

        assert obs.pinned_serial_path(tmp_path).name == "org_bundle_serial.txt"
        assert obs.pinned_public_key_path(tmp_path).name == "org_bundle_signing_pubkey.txt"
        obs.record_accepted_serial(obs.BundleTrust(ok=True, detail="", serial=9), tmp_path)

        assert obs.load_pinned_serial(tmp_path) == 9
        assert _legacy_snapshot(tmp_path) == legacy_before
        assert (tmp_path / obs.LEGACY_PINNED_SERIAL_FILENAME).read_text() == "2\n"

    def test_key_rotation_after_adoption_never_brings_the_old_key_back(self, tmp_path):
        old_key, _ = obs.generate_keypair()
        new_key, _ = obs.generate_keypair()
        old_signed = obs.sign_bundle({"mode": "local"}, old_key)
        (tmp_path / "org_config.json").write_text(__import__("json").dumps(old_signed))
        (tmp_path / obs.LEGACY_PINNED_PUBKEY_FILENAME).write_text(obs.public_key_b64(old_key.public_key()) + "\n")
        obs.adopt_legacy_files(tmp_path)

        # Documented rotation: the administrator deletes the new-name pin.
        (tmp_path / obs.PINNED_PUBKEY_FILENAME).unlink()
        assert obs.adopt_legacy_files(tmp_path) is False
        assert not (tmp_path / obs.PINNED_PUBKEY_FILENAME).exists()

        rotated = obs.sign_bundle({"mode": "local", "org_name": "Acme"}, new_key)
        trust = obs.verify_and_maybe_pin(rotated, tmp_path)
        assert trust.ok and trust.newly_pinned
        assert obs.load_pinned_public_key(tmp_path) == base64.b64decode(obs.public_key_b64(new_key.public_key()))
