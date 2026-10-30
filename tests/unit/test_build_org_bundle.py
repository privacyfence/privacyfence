"""Tests for scripts/build_org_bundle.py: the desktop bundle it builds, its signing support, and
the options it refuses now that operational settings live on the Admin page and organization
servers take no bundle.

This script deliberately doesn't import the ``privacyfence`` package (see
its own module docstring -- it's meant to be runnable standalone, without
a PrivacyFence install) and instead carries its own copy of the
canonicalization/signing logic that mirrors src/privacyfence/
org_bundle_signing.py's. The most important thing these tests establish
is that the two copies actually stay in sync: a bundle signed by the
script must verify against org_bundle_signing.verify_and_maybe_pin(), the
real function the daemon and settings_controller.py use -- if the two
canonicalizations ever drift, every bundle built with this script would
fail to install/start, silently.

Imported by file path (importlib) rather than as a package, since
scripts/ isn't part of the installed ``privacyfence`` distribution.
"""
from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest


from privacyfence import org_bundle_signing

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_org_bundle.py"
_spec = importlib.util.spec_from_file_location("build_org_bundle", _SCRIPT_PATH)
build_org_bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_org_bundle)


class TestCanonicalPayloadBytes:
    def test_excludes_signature_field(self):
        with_sig = build_org_bundle._canonical_payload_bytes({"a": 1, "signature": "xyz"})
        without_sig = build_org_bundle._canonical_payload_bytes({"a": 1})
        assert with_sig == without_sig

    def test_key_order_does_not_affect_output(self):
        assert (
            build_org_bundle._canonical_payload_bytes({"b": 2, "a": 1})
            == build_org_bundle._canonical_payload_bytes({"a": 1, "b": 2})
        )

    def test_matches_org_bundle_signing_module_exactly(self):
        """The whole reason both copies exist -- see module docstring.
        This pins them to identical output on the same input so any
        future edit to either that breaks the other fails loudly here."""
        bundle = {"mode": "org", "server": {"issuer_url": "https://x"}, "signature": "stale"}
        assert (
            build_org_bundle._canonical_payload_bytes(bundle)
            == org_bundle_signing._canonical_payload_bytes(bundle)
        )


class TestGenerateSigningKey:
    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_writes_a_private_key_file_with_restrictive_permissions(self, tmp_path, capsys):
        key_path = tmp_path / "signing_key.pem"

        rc = build_org_bundle._generate_signing_key(str(key_path))

        assert rc == 0
        assert key_path.exists()
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert b"PRIVATE KEY" in key_path.read_bytes()

    def test_prints_the_public_key(self, tmp_path, capsys):
        build_org_bundle._generate_signing_key(str(tmp_path / "key.pem"))
        out = capsys.readouterr().out
        assert "Public key" in out


class TestSignBundle:
    def test_signed_bundle_verifies_against_org_bundle_signing(self, tmp_path):
        """The critical cross-module check: a bundle this script signs
        must be acceptable to the real verification path the daemon and
        settings_controller.py actually run."""
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))

        signed = build_org_bundle._sign_bundle({"mode": "org", "org_name": "Acme"}, str(key_path))

        assert "signature" in signed
        assert "signing_public_key" in signed
        trust = org_bundle_signing.verify_and_maybe_pin(signed, tmp_path / "orgdir")
        assert trust.ok
        assert trust.signed

    def test_tampering_the_signed_bundle_fails_verification(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        signed = build_org_bundle._sign_bundle({"org_name": "Acme"}, str(key_path))

        signed["org_name"] = "Evil Corp"

        trust = org_bundle_signing.verify_and_maybe_pin(signed, tmp_path / "orgdir")
        assert not trust.ok

    def test_non_ed25519_key_file_is_rejected(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_path = tmp_path / "rsa_key.pem"
        key_path.write_bytes(rsa_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))

        with pytest.raises(SystemExit, match="not an Ed25519"):
            build_org_bundle._sign_bundle({"org_name": "Acme"}, str(key_path))


# Every option that wrote an operational setting, with a sample value list that makes the command
# line otherwise valid (the refusal must be what fails). The second column is the message kind.
_MOVED = "moved"
_MOVED_ENV = "moved-env"
_MOVED_OPTIONS: list[tuple[str, list[str], str]] = [
    ("--enable-telegram", [], _MOVED),
    ("--enable-peek", [], _MOVED),
    ("--enable-grist", [], _MOVED),
    ("--peek-base-url", ["https://peek.example.com"], _MOVED),
    ("--atlassian-assets", [], _MOVED),
    ("--atlassian-assets-workspace-id", ["6d8c7ed1-92a5-4693-b107-226f466d78a3"], _MOVED),
    ("--authz-allowed-domain", ["acme.com"], _MOVED),
    ("--authz-groups-claim", ["groups"], _MOVED),
    ("--authz-required-group", ["pf-users"], _MOVED),
    ("--step-up-enabled", [], _MOVED),
    ("--step-up-disabled", [], _MOVED),
    ("--step-up-scope", ["writes"], _MOVED),
    ("--step-up-rp-id", ["pf.example.com"], _MOVED),
    ("--step-up-rp-name", ["PrivacyFence"], _MOVED),
    ("--step-up-require-passkey", [], _MOVED),
    ("--step-up-no-require-passkey", [], _MOVED),
    ("--enable-unattended-sessions", [], _MOVED),
    ("--disable-unattended-sessions", [], _MOVED),
    ("--enable-scoped-tokens", [], _MOVED),
    ("--disable-scoped-tokens", [], _MOVED),
    ("--scoped-token-max-days", ["30"], _MOVED),
    ("--enable-scoped-token-proposals", [], _MOVED),
    ("--disable-scoped-token-proposals", [], _MOVED),
    ("--downloads-inline-max-bytes", ["1000"], _MOVED),
    ("--downloads-link-ttl-seconds", ["60"], _MOVED),
    ("--downloads-disable-staging", [], _MOVED),
    ("--agent-links", [], _MOVED),
    ("--no-agent-links", [], _MOVED),
    ("--exports", [], _MOVED),
    ("--no-exports", [], _MOVED),
    ("--web-push", [], _MOVED),
    ("--no-web-push", [], _MOVED),
    ("--enable-audit-forwarding", [], _MOVED),
    ("--disable-audit-forwarding", [], _MOVED),
    ("--audit-forwarding-kind", ["syslog"], _MOVED),
    ("--audit-forwarding-syslog-host", ["siem.example.com"], _MOVED),
    ("--audit-forwarding-syslog-port", ["601"], _MOVED),
    ("--audit-forwarding-syslog-protocol", ["udp"], _MOVED),
    ("--audit-forwarding-http-url", ["https://siem.example.com/ingest"], _MOVED),
    ("--audit-forwarding-http-bearer-token-env", ["SIEM_TOKEN"], _MOVED),
    ("--peek-auto-provision", [], _MOVED_ENV),
    ("--no-peek-auto-provision", [], _MOVED_ENV),
    ("--peek-provisioning-key-env", ["MY_KEY"], _MOVED_ENV),
    ("--peek-trust-unverified-email", [], _MOVED_ENV),
    ("--grist-auto-provision", [], _MOVED_ENV),
    ("--no-grist-auto-provision", [], _MOVED_ENV),
    ("--grist-provisioner-url", ["https://prov.example.com"], _MOVED_ENV),
    ("--grist-provisioning-key-env", ["MY_KEY"], _MOVED_ENV),
    ("--grist-trust-unverified-email", [], _MOVED_ENV),
    # Pins an API-key server, which is an Admin-page setting; with a Grist OAuth app it stays valid.
    ("--grist-server-url", ["https://grist.example.com"], _MOVED),
]

_ORG_MODE_OPTIONS: list[tuple[str, list[str]]] = [
    ("--server-issuer-url", ["https://pf.example.com"]),
    ("--server-bind-host", ["0.0.0.0"]),
    ("--server-port", ["8765"]),
    ("--server-tls-cert", ["/etc/pf/cert.pem"]),
    ("--server-tls-key", ["/etc/pf/key.pem"]),
    ("--server-trusted-proxy", ["10.0.0.5"]),
    ("--idp-issuer", ["https://idp.example.com"]),
    ("--idp-client-id", ["cid"]),
    ("--idp-client-secret", ["csecret"]),
    ("--idp-admin-group-claim", ["groups"]),
    ("--idp-admin-group-value", ["pf-admins"]),
    ("--idp-step-up-acr-value", ["mfa"]),
]

_MOVED_TEXT = "error: {option} has moved to the Admin page (ADR 0257); remove it."
_MOVED_ENV_TEXT = (
    "error: {option} has moved to the Admin page and PRIVACYFENCE_PEEK_BASE_URL / "
    "PRIVACYFENCE_GRIST_SERVER_URL / PRIVACYFENCE_GRIST_PROVISIONER_URL on the server (ADR 0257, 0264); "
    "remove it."
)
_ORG_TEXT = (
    "error: {option}: organization servers take their settings from privacyfence.env and their own "
    "apps from signed connector configs (docs/org-mode-setup-guide.md, ADR 0269)."
)


def _desktop_args(tmp_path: Path, *extra: str) -> list[str]:
    """A valid minimal desktop command line: a Slack app, written to tmp_path/org_bundle.json."""
    return [
        "-o", str(tmp_path / "org_bundle.json"),
        "--slack-client-id", "id1", "--slack-client-secret", "secret1", *extra,
    ]


def _run_expecting_exit(argv: list[str]) -> int | str | None:
    with pytest.raises(SystemExit) as excinfo:
        build_org_bundle.main(argv)
    return excinfo.value.code


class TestMainSigningIntegration:
    def test_merge_without_sign_key_strips_a_stale_signature(self, tmp_path):
        """Re-running without --sign-key after a bundle was previously
        signed must not leave a now-invalid signature/key pair sitting in
        the output -- see main()'s own comment on why these are always
        stripped and only re-added by an explicit (re-)sign this run."""
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_bundle.json"
        build_org_bundle.main([
            "-o", str(out_path), "--org-name", "Acme",
            "--slack-client-id", "id1", "--slack-client-secret", "secret1",
            "--sign-key", str(key_path),
        ])
        assert "signature" in json.loads(out_path.read_text())

        build_org_bundle.main([
            "-o", str(out_path), "--merge",
            "--salesforce-consumer-key", "ckey", "--salesforce-consumer-secret", "csecret",
        ])

        bundle = json.loads(out_path.read_text())
        assert "signature" not in bundle
        assert "signing_public_key" not in bundle
        assert bundle["slack"]["client_id"] == "id1"  # the earlier merge survives
        assert bundle["salesforce"]["consumer_key"] == "ckey"

    def test_a_credentials_only_desktop_bundle_builds_and_verifies(self, tmp_path, capsys):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_bundle.json"

        rc = build_org_bundle.main([
            "-o", str(out_path), "--org-name", "Acme", "--slack-client-id", "id1",
            "--slack-client-secret", "secret1", "--sign-key", str(key_path),
        ])

        assert rc == 0
        bundle = json.loads(out_path.read_text())
        assert bundle["slack"] == {"client_id": "id1", "client_secret": "secret1"}
        assert not {"mode", "server", "idp"} & set(bundle)
        trust = org_bundle_signing.verify_and_maybe_pin(bundle, tmp_path / "orgdir")
        assert trust.ok and trust.signed
        out = capsys.readouterr().out
        assert "Install configuration file" in out
        assert "<data>/org/org_bundle.json" not in out

    def test_mode_local_is_still_accepted_and_written(self, tmp_path):
        assert build_org_bundle.main(_desktop_args(tmp_path, "--mode", "local")) == 0
        assert json.loads((tmp_path / "org_bundle.json").read_text())["mode"] == "local"

    def test_signing_adds_a_serial_that_defaults_to_the_current_time(self, tmp_path, monkeypatch):
        monkeypatch.setattr(build_org_bundle.time, "time", lambda: 1_700_000_000.9)
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_bundle.json"

        build_org_bundle.main([
            "-o", str(out_path), "--org-name", "Acme",
            "--slack-client-id", "id1", "--slack-client-secret", "secret1",
            "--sign-key", str(key_path),
        ])

        assert json.loads(out_path.read_text())["serial"] == 1_700_000_000

    def test_serial_flag_sets_the_serial(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_bundle.json"

        build_org_bundle.main([
            "-o", str(out_path), "--org-name", "Acme",
            "--slack-client-id", "id1", "--slack-client-secret", "secret1",
            "--sign-key", str(key_path), "--serial", "42",
        ])

        assert json.loads(out_path.read_text())["serial"] == 42

    def test_serial_without_sign_key_is_an_error(self, tmp_path, capsys):
        out_path = tmp_path / "org_bundle.json"

        with pytest.raises(SystemExit):
            build_org_bundle.main([
                "-o", str(out_path), "--org-name", "Acme",
                "--slack-client-id", "id1", "--slack-client-secret", "secret1",
                "--serial", "42",
            ])

        assert "--serial needs --sign-key" in capsys.readouterr().err
        assert not out_path.exists()

    @pytest.mark.parametrize("value", ["0", "-1", "abc"])
    def test_serial_must_be_a_positive_integer(self, tmp_path, value):
        with pytest.raises(SystemExit):
            build_org_bundle.main([
                "-o", str(tmp_path / "o.json"), "--org-name", "Acme",
                "--sign-key", str(tmp_path / "key.pem"), "--serial", value,
            ])

    def test_merge_does_not_carry_the_old_serial(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_bundle.json"
        build_org_bundle.main([
            "-o", str(out_path), "--org-name", "Acme",
            "--slack-client-id", "id1", "--slack-client-secret", "secret1",
            "--sign-key", str(key_path), "--serial", "42",
        ])

        build_org_bundle.main([
            "-o", str(out_path), "--merge",
            "--salesforce-consumer-key", "ckey", "--salesforce-consumer-secret", "csecret",
        ])
        assert "serial" not in json.loads(out_path.read_text())

        build_org_bundle.main([
            "-o", str(out_path), "--merge",
            "--salesforce-consumer-key", "ckey", "--salesforce-consumer-secret", "csecret",
            "--sign-key", str(key_path),
        ])
        assert json.loads(out_path.read_text())["serial"] != 42

    def test_a_script_signed_bundle_with_a_serial_verifies_in_the_daemon_copy(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_bundle.json"
        build_org_bundle.main([
            "-o", str(out_path), "--org-name", "Acme",
            "--slack-client-id", "id1", "--slack-client-secret", "secret1",
            "--sign-key", str(key_path), "--serial", "42",
        ])
        bundle = json.loads(out_path.read_text())

        trust = org_bundle_signing.verify_and_maybe_pin(bundle, tmp_path / "orgdir")

        assert trust.ok and trust.signed
        assert trust.serial == 42

    def test_sign_key_needs_cryptography_gives_a_clear_error(self, tmp_path, monkeypatch):
        # A plain sys.modules["cryptography"] = None wouldn't reliably
        # force ImportError here -- once a submodule has been imported
        # once, its parent package object already carries it as a real
        # attribute, so `from cryptography.hazmat... import ed25519` can
        # resolve via attribute access without re-checking sys.modules.
        # Evict every cached cryptography.* module first so the import
        # is actually re-attempted (and short-circuits on the None
        # sentinel) rather than served from the attribute cache.
        for name in list(sys.modules):
            if name == "cryptography" or name.startswith("cryptography."):
                monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setitem(sys.modules, "cryptography", None)

        with pytest.raises(SystemExit, match="pip install cryptography"):
            build_org_bundle._generate_signing_key(str(tmp_path / "key.pem"))


class TestRefusedOptions:
    """Operational and organization-mode options stay parseable, hidden from --help, only to be
    refused with a message that says where the setting went. Nothing is written."""

    @pytest.mark.parametrize(
        ("option", "values", "kind"), _MOVED_OPTIONS, ids=[row[0] for row in _MOVED_OPTIONS],
    )
    def test_moved_flag_is_refused(self, tmp_path, capsys, option, values, kind):
        out_path = tmp_path / "org_bundle.json"

        code = _run_expecting_exit(_desktop_args(tmp_path, option, *values))

        template = _MOVED_ENV_TEXT if kind == _MOVED_ENV else _MOVED_TEXT
        assert code == 2
        assert capsys.readouterr().err == template.format(option=option) + "\n"
        assert not out_path.exists()

    def test_org_mode_option_list_matches_the_parsers_server_and_idp_destinations(self):
        parser = build_org_bundle.build_parser()
        from_parser = sorted(
            option
            for action in parser._actions if action.dest.startswith(("server_", "idp_"))
            for option in action.option_strings
        )
        assert from_parser == sorted(option for option, _ in _ORG_MODE_OPTIONS)

    @pytest.mark.parametrize(("option", "values"), _ORG_MODE_OPTIONS, ids=[row[0] for row in _ORG_MODE_OPTIONS])
    def test_org_mode_option_is_refused(self, tmp_path, capsys, option, values):
        out_path = tmp_path / "org_bundle.json"

        code = _run_expecting_exit(_desktop_args(tmp_path, option, *values))

        assert code == 2
        assert capsys.readouterr().err == _ORG_TEXT.format(option=option) + "\n"
        assert not out_path.exists()

    @pytest.mark.parametrize("spelling", [["--mode", "org"], ["--mode=org"]])
    def test_org_mode_option_is_refused_for_mode_org(self, tmp_path, capsys, spelling):
        code = _run_expecting_exit(_desktop_args(tmp_path, *spelling))

        assert code == 2
        assert capsys.readouterr().err == _ORG_TEXT.format(option="--mode org") + "\n"
        assert not (tmp_path / "org_bundle.json").exists()

    def test_refused_options_are_hidden_from_help(self, capsys):
        with pytest.raises(SystemExit):
            build_org_bundle.main(["--help"])
        out = capsys.readouterr().out
        for option in [row[0] for row in _MOVED_OPTIONS if row[0] != "--grist-server-url"]:
            assert option not in out
        for option, _ in _ORG_MODE_OPTIONS:
            assert option not in out
        parser = build_org_bundle.build_parser()
        hidden = {o for a in parser._actions if a.help is build_org_bundle.argparse.SUPPRESS for o in a.option_strings}
        assert {row[0] for row in _MOVED_OPTIONS} - {"--grist-server-url"} <= hidden

    def test_help_points_organization_servers_to_the_env_file_and_connector_configs(self, capsys):
        with pytest.raises(SystemExit):
            build_org_bundle.main(["--help"])
        out = " ".join(capsys.readouterr().out.split())
        assert "privacyfence.env" in out
        assert "connector configs" in out
        assert "Admin page" in out

    def test_every_refused_option_is_reported_once_in_the_order_given(self, tmp_path, capsys):
        code = _run_expecting_exit(_desktop_args(
            tmp_path, "--enable-telegram", "--idp-issuer", "https://idp.example.com", "--mode", "org",
            "--no-web-push", "--enable-telegram", "--peek-auto-provision",
        ))

        assert code == 2
        assert capsys.readouterr().err.splitlines() == [
            _MOVED_TEXT.format(option="--enable-telegram"),
            _ORG_TEXT.format(option="--idp-issuer"),
            _ORG_TEXT.format(option="--mode org"),
            _MOVED_TEXT.format(option="--no-web-push"),
            _MOVED_ENV_TEXT.format(option="--peek-auto-provision"),
        ]

    def test_an_option_given_with_an_equals_sign_is_refused(self, tmp_path, capsys):
        code = _run_expecting_exit(_desktop_args(tmp_path, "--step-up-scope=writes"))

        assert code == 2
        assert capsys.readouterr().err == _MOVED_TEXT.format(option="--step-up-scope") + "\n"

    def test_a_grist_oauth_app_keeps_its_server_address(self, tmp_path):
        out_path = tmp_path / "org_bundle.json"
        assert build_org_bundle.main([
            "-o", str(out_path), "--grist-server-url", "https://docs.example.com",
            "--grist-client-id", "gid", "--grist-client-secret", "gsecret",
        ]) == 0
        assert json.loads(out_path.read_text())["grist"]["server_url"] == "https://docs.example.com"

    def test_refusal_does_not_touch_an_existing_output(self, tmp_path):
        out_path = tmp_path / "org_bundle.json"
        out_path.write_text("keep me")

        assert _run_expecting_exit(_desktop_args(tmp_path, "--merge", "--enable-telegram")) == 2

        assert out_path.read_text() == "keep me"


class TestMerge:
    def _write(self, tmp_path, bundle):
        out_path = tmp_path / "org_bundle.json"
        out_path.write_text(json.dumps(bundle))
        return out_path

    def test_merge_drops_legacy_operational_keys_and_says_so(self, tmp_path, capsys):
        out_path = self._write(tmp_path, {
            "version": 1,
            "slack": {"client_id": "id", "client_secret": "secret"},
            "authz": {"allowed_domains": ["acme.com"]},
            "peek": {"base_url": "https://peek.example.com", "auto_provision": {"enabled": True}},
            "telegram": {"enabled": True},
            "download_delivery": {"agent_links": False},
        })

        assert build_org_bundle.main(["-o", str(out_path), "--merge"]) == 0

        bundle = json.loads(out_path.read_text())
        assert bundle["slack"] == {"client_id": "id", "client_secret": "secret"}
        assert not {"authz", "peek", "telegram", "download_delivery"} & set(bundle)
        out = capsys.readouterr().out
        assert "note: dropped download_delivery, authz, telegram, peek; they are managed on the Admin page now." in out

    def test_merge_drops_the_operational_sub_keys_and_keeps_the_oauth_apps(self, tmp_path, capsys):
        out_path = self._write(tmp_path, {
            "version": 1,
            "atlassian": {
                "client_id": "a", "client_secret": "as", "assets": True,
                "assets_workspace_id": "6d8c7ed1-92a5-4693-b107-226f466d78a3",
            },
            "grist": {"server_url": "https://docs.example.com", "client_id": "g", "client_secret": "gs"},
            "miro": {"client_id": "m", "client_secret": "ms"},
        })

        assert build_org_bundle.main(["-o", str(out_path), "--merge"]) == 0

        bundle = json.loads(out_path.read_text())
        assert bundle["atlassian"] == {"client_id": "a", "client_secret": "as"}
        assert bundle["grist"]["server_url"] == "https://docs.example.com"
        assert "note: dropped atlassian.assets, atlassian.assets_workspace_id;" in capsys.readouterr().out

    def test_merge_drops_a_grist_api_key_server_and_its_auto_provisioning(self, tmp_path, capsys):
        out_path = self._write(tmp_path, {
            "version": 1,
            "slack": {"client_id": "id", "client_secret": "secret"},
            "grist": {"server_url": "https://grist.example.com", "auto_provision": {"enabled": True}},
        })

        assert build_org_bundle.main(["-o", str(out_path), "--merge"]) == 0

        bundle = json.loads(out_path.read_text())
        assert "grist" not in bundle
        assert "note: dropped grist.server_url, grist.auto_provision;" in capsys.readouterr().out

    def test_merge_of_a_credentials_only_bundle_prints_no_note(self, tmp_path, capsys):
        out_path = self._write(tmp_path, {"version": 1, "slack": {"client_id": "id", "client_secret": "secret"}})

        assert build_org_bundle.main(["-o", str(out_path), "--merge", "--org-name", "Acme"]) == 0

        assert "note:" not in capsys.readouterr().out

    @pytest.mark.parametrize("org_keys", [
        {"mode": "org"},
        {"idp": {"issuer": "https://idp.example.com", "client_id": "c", "client_secret": "s"}},
        {"server": {"issuer_url": "https://pf.example.com"}},
    ], ids=["mode-org", "idp", "server"])
    def test_merge_refuses_an_organization_bundle(self, tmp_path, capsys, org_keys):
        out_path = self._write(tmp_path, {
            "version": 1, "slack": {"client_id": "id", "client_secret": "secret"}, **org_keys,
        })
        before = out_path.read_bytes()

        code = _run_expecting_exit(["-o", str(out_path), "--merge", "--miro-client-id", "m", "--miro-client-secret", "ms"])

        assert code == 2
        assert capsys.readouterr().err == (
            f"error: {out_path} is an organization bundle; organization servers no longer take bundles (ADR 0269).\n"
        )
        assert out_path.read_bytes() == before

    def test_merge_keeps_a_local_mode_marker(self, tmp_path):
        out_path = self._write(tmp_path, {
            "version": 1, "mode": "local", "slack": {"client_id": "id", "client_secret": "secret"},
        })

        assert build_org_bundle.main(["-o", str(out_path), "--merge", "--org-name", "Acme"]) == 0

        assert json.loads(out_path.read_text())["mode"] == "local"

    def test_nothing_to_write_names_only_app_credentials(self, tmp_path):
        out_path = tmp_path / "org_bundle.json"

        with pytest.raises(SystemExit) as excinfo:
            build_org_bundle.main(["-o", str(out_path), "--org-name", "Acme"])

        message = str(excinfo.value)
        assert "app credentials" in message and "nothing to write" in message
        assert "unattended" not in message and "scoped" not in message
        assert not out_path.exists()

    def test_a_merge_left_with_no_app_credentials_writes_nothing(self, tmp_path):
        out_path = self._write(tmp_path, {"version": 1, "telegram": {"enabled": True}})

        with pytest.raises(SystemExit, match="nothing to write"):
            build_org_bundle.main(["-o", str(out_path), "--merge"])

        assert json.loads(out_path.read_text()) == {"version": 1, "telegram": {"enabled": True}}


class TestRemovedToolAnnotations:
    """ADR 0089: --tool-annotations is gone, and --merge drops the mcp.tool_annotations key a
    5.0.0a1 bundle may still carry -- the daemon refuses to start with it."""

    def _out(self, tmp_path):
        return tmp_path / "org_bundle.json"

    def test_the_flag_is_no_longer_accepted(self, tmp_path):
        with pytest.raises(SystemExit):
            build_org_bundle.main(["-o", str(self._out(tmp_path)), "--tool-annotations", "truthful"])

    def test_a_fresh_bundle_carries_no_mcp_section(self, tmp_path):
        from privacyfence import org_mode

        assert build_org_bundle.main([
            "-o", str(self._out(tmp_path)), "--slack-client-id", "id", "--slack-client-secret", "secret",
        ]) == 0
        bundle = json.loads(self._out(tmp_path).read_text())
        assert "mcp" not in bundle
        org_mode.reject_removed_tool_annotations(bundle)

    @pytest.mark.parametrize("value", ["truthful", "all_read_only"])
    def test_merge_drops_the_removed_key_and_keeps_the_rest(self, tmp_path, capsys, value):
        from privacyfence import org_mode

        self._out(tmp_path).write_text(json.dumps({
            "version": 1, "slack": {"client_id": "id", "client_secret": "secret"},
            "mcp": {"tool_annotations": value},
        }))
        assert build_org_bundle.main(["-o", str(self._out(tmp_path)), "--merge"]) == 0
        bundle = json.loads(self._out(tmp_path).read_text())
        assert "mcp" not in bundle
        assert bundle["slack"] == {"client_id": "id", "client_secret": "secret"}
        assert "mcp.tool_annotations" in capsys.readouterr().out
        org_mode.reject_removed_tool_annotations(bundle)

    def test_merge_keeps_any_other_mcp_key(self, tmp_path):
        self._out(tmp_path).write_text(json.dumps({
            "version": 1, "slack": {"client_id": "id", "client_secret": "secret"},
            "mcp": {"tool_annotations": "all_read_only", "other": 1},
        }))
        assert build_org_bundle.main(["-o", str(self._out(tmp_path)), "--merge"]) == 0
        assert json.loads(self._out(tmp_path).read_text())["mcp"] == {"other": 1}


class TestGristFlags:
    def _build(self, tmp_path, *args):
        out_path = tmp_path / "org_bundle.json"
        rc = build_org_bundle.main(["-o", str(out_path), *args])
        assert rc == 0
        return json.loads(out_path.read_text())

    def _exit_message(self, tmp_path, *args):
        with pytest.raises(SystemExit) as exc:
            build_org_bundle.main(["-o", str(tmp_path / "org_bundle.json"), *args])
        return str(exc.value)

    def test_client_pair_and_auth_server_write_all_four_keys(self, tmp_path):
        bundle = self._build(
            tmp_path, "--grist-server-url", "https://docs.example.com/", "--grist-client-id", "gid",
            "--grist-client-secret", "gsecret", "--grist-auth-server-url", "https://login.example.com/",
        )
        assert bundle["grist"] == {
            "server_url": "https://docs.example.com", "client_id": "gid", "client_secret": "gsecret",
            "auth_server_url": "https://login.example.com",
        }

    def test_id_without_secret_exits(self, tmp_path):
        message = self._exit_message(
            tmp_path, "--grist-server-url", "https://grist.example.com", "--grist-client-id", "gid",
        )
        assert message == "--grist-client-id and --grist-client-secret must be given together."

    def test_client_pair_without_server_url_exits(self, tmp_path):
        message = self._exit_message(tmp_path, "--grist-client-id", "gid", "--grist-client-secret", "gsecret")
        assert message == "--grist-client-id, --grist-client-secret and --grist-auth-server-url need --grist-server-url."

    def test_http_url_exits(self, tmp_path):
        message = self._exit_message(
            tmp_path, "--grist-server-url", "http://grist.example.com",
            "--grist-client-id", "gid", "--grist-client-secret", "gsecret",
        )
        assert message == (
            "--grist-server-url and --grist-auth-server-url must be https:// addresses (http:// only for localhost)."
        )

    def test_http_localhost_is_allowed(self, tmp_path):
        bundle = self._build(
            tmp_path, "--grist-server-url", "http://localhost:8484",
            "--grist-client-id", "gid", "--grist-client-secret", "gsecret",
        )
        assert bundle["grist"]["server_url"] == "http://localhost:8484"

    @pytest.mark.parametrize("url", [
        "https://grist.example.com/api", "https://grist.example.com/o/acme/api", "https://grist.example.com?x=1",
        "https://grist.example.com/#top", "https://user:pw@grist.example.com",
    ])
    def test_api_path_query_fragment_and_userinfo_exit(self, tmp_path, url):
        message = self._exit_message(
            tmp_path, "--grist-server-url", url, "--grist-client-id", "gid", "--grist-client-secret", "gsecret",
        )
        assert message == (
            "--grist-server-url and --grist-auth-server-url must not contain a user name, password, query, "
            "fragment or /api."
        )

    def test_auth_server_url_alone_exits(self, tmp_path):
        message = self._exit_message(tmp_path, "--grist-auth-server-url", "https://login.example.com")
        assert message == "--grist-client-id, --grist-client-secret and --grist-auth-server-url need --grist-server-url."


class TestMiroFlags:
    def _build(self, tmp_path, *args):
        out_path = tmp_path / "org_bundle.json"
        assert build_org_bundle.main(["-o", str(out_path), *args]) == 0
        return json.loads(out_path.read_text())

    def _exit_message(self, tmp_path, *args):
        with pytest.raises(SystemExit) as exc:
            build_org_bundle.main(["-o", str(tmp_path / "org_bundle.json"), *args])
        return str(exc.value)

    def test_the_pair_is_written(self, tmp_path):
        bundle = self._build(tmp_path, "--miro-client-id", "mid", "--miro-client-secret", "msecret")
        assert bundle["miro"] == {"client_id": "mid", "client_secret": "msecret"}

    def test_a_miro_only_bundle_is_written(self, tmp_path):
        bundle = self._build(tmp_path, "--miro-client-id", "mid", "--miro-client-secret", "msecret")
        assert "miro" in bundle and "google" not in bundle

    @pytest.mark.parametrize("args", [["--miro-client-id", "mid"], ["--miro-client-secret", "msecret"]])
    def test_one_without_the_other_exits(self, tmp_path, args):
        assert self._exit_message(tmp_path, *args) == "--miro-client-id and --miro-client-secret must be given together."


class TestAtlassianFlags:
    def test_the_credentials_are_written_without_operational_keys(self, tmp_path):
        out_path = tmp_path / "org_bundle.json"
        build_org_bundle.main([
            "-o", str(out_path), "--atlassian-client-id", "a", "--atlassian-client-secret", "as",
        ])
        assert json.loads(out_path.read_text())["atlassian"] == {"client_id": "a", "client_secret": "as"}

    def test_one_without_the_other_exits(self, tmp_path):
        with pytest.raises(SystemExit) as excinfo:
            build_org_bundle.main(["-o", str(tmp_path / "org_bundle.json"), "--atlassian-client-id", "a"])
        assert str(excinfo.value) == "--atlassian-client-id and --atlassian-client-secret must be given together."


class TestInstructionsPrintedAfterWriting:
    def test_the_bundle_is_installed_from_settings(self, tmp_path, capsys):
        build_org_bundle.main(_desktop_args(tmp_path))

        out = capsys.readouterr().out
        assert "Install configuration file" in out
        assert "<data>/org/org_bundle.json" not in out
        assert "/oauth/" not in out

    def test_the_summary_names_the_services(self, tmp_path, capsys):
        build_org_bundle.main(_desktop_args(tmp_path))

        assert f"Wrote {tmp_path / 'org_bundle.json'} with: slack, signed=False" in capsys.readouterr().out
