"""Tests for scripts/build_org_bundle.py's bundle signing support.

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

pytest.importorskip("cryptography")

from privacyfence import org_bundle_signing
from privacyfence.step_up_config import STEP_UP_SCOPES, StepUpConfig

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


class TestMainSigningIntegration:
    def test_mode_org_without_sign_key_is_rejected(self, tmp_path):
        out_path = tmp_path / "org_config.json"
        with pytest.raises(SystemExit):
            build_org_bundle.main([
                "-o", str(out_path), "--mode", "org",
                "--server-issuer-url", "https://pf.example.com",
                "--idp-issuer", "https://idp.example.com",
                "--idp-client-id", "cid", "--idp-client-secret", "csecret",
            ])
        assert not out_path.exists()

    def test_mode_org_with_sign_key_writes_a_signed_bundle(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_config.json"

        rc = build_org_bundle.main([
            "-o", str(out_path), "--mode", "org",
            "--server-issuer-url", "https://pf.example.com",
            "--idp-issuer", "https://idp.example.com",
            "--idp-client-id", "cid", "--idp-client-secret", "csecret",
            "--sign-key", str(key_path),
        ])

        assert rc == 0
        bundle = json.loads(out_path.read_text())
        assert bundle["mode"] == "org"
        assert "signature" in bundle
        trust = org_bundle_signing.verify_and_maybe_pin(bundle, tmp_path / "orgdir")
        assert trust.ok and trust.signed

    def test_merge_without_sign_key_strips_a_stale_signature(self, tmp_path):
        """Re-running without --sign-key after a bundle was previously
        signed must not leave a now-invalid signature/key pair sitting in
        the output -- see main()'s own comment on why these are always
        stripped and only re-added by an explicit (re-)sign this run."""
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_config.json"
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

    def test_authz_flags_write_the_authz_section(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_config.json"

        rc = build_org_bundle.main([
            "-o", str(out_path), "--mode", "org",
            "--server-issuer-url", "https://pf.example.com",
            "--idp-issuer", "https://idp.example.com",
            "--idp-client-id", "cid", "--idp-client-secret", "csecret",
            "--authz-allowed-domain", "acme.com", "--authz-allowed-domain", "acme.co.uk",
            "--authz-groups-claim", "groups", "--authz-required-group", "privacyfence-users",
            "--sign-key", str(key_path),
        ])

        assert rc == 0
        bundle = json.loads(out_path.read_text())
        assert bundle["authz"] == {
            "allowed_domains": ["acme.com", "acme.co.uk"],
            "groups_claim": "groups",
            "required_groups": ["privacyfence-users"],
        }

    def test_authz_required_group_without_groups_claim_is_rejected(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_config.json"

        with pytest.raises(SystemExit, match="authz-groups-claim"):
            build_org_bundle.main([
                "-o", str(out_path), "--mode", "org",
                "--server-issuer-url", "https://pf.example.com",
                "--idp-issuer", "https://idp.example.com",
                "--idp-client-id", "cid", "--idp-client-secret", "csecret",
                "--authz-required-group", "privacyfence-users",
                "--sign-key", str(key_path),
            ])

    def test_authz_flags_require_mode_org(self, tmp_path):
        out_path = tmp_path / "org_config.json"
        with pytest.raises(SystemExit, match="--mode org"):
            build_org_bundle.main([
                "-o", str(out_path), "--authz-allowed-domain", "acme.com",
            ])

    def test_mode_local_clears_a_previously_written_authz_section(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_config.json"
        build_org_bundle.main([
            "-o", str(out_path), "--mode", "org",
            "--server-issuer-url", "https://pf.example.com",
            "--idp-issuer", "https://idp.example.com",
            "--idp-client-id", "cid", "--idp-client-secret", "csecret",
            "--authz-allowed-domain", "acme.com",
            "--sign-key", str(key_path),
        ])

        build_org_bundle.main(["-o", str(out_path), "--merge", "--mode", "local"])

        assert "authz" not in json.loads(out_path.read_text())

    def test_step_up_require_passkey_flag_writes_the_section(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_config.json"

        rc = build_org_bundle.main([
            "-o", str(out_path), "--mode", "org",
            "--server-issuer-url", "https://pf.example.com",
            "--idp-issuer", "https://idp.example.com",
            "--idp-client-id", "cid", "--idp-client-secret", "csecret",
            "--step-up-enabled", "--step-up-require-passkey",
            "--sign-key", str(key_path),
        ])

        assert rc == 0
        bundle = json.loads(out_path.read_text())
        assert bundle["step_up"]["enabled"] is True
        assert bundle["step_up"]["require_passkey"] is True

    def test_step_up_no_require_passkey_turns_it_back_off_on_merge(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_config.json"
        build_org_bundle.main([
            "-o", str(out_path), "--mode", "org",
            "--server-issuer-url", "https://pf.example.com",
            "--idp-issuer", "https://idp.example.com",
            "--idp-client-id", "cid", "--idp-client-secret", "csecret",
            "--step-up-enabled", "--step-up-require-passkey",
            "--sign-key", str(key_path),
        ])

        build_org_bundle.main([
            "-o", str(out_path), "--merge", "--step-up-no-require-passkey", "--sign-key", str(key_path),
        ])

        bundle = json.loads(out_path.read_text())
        assert bundle["step_up"]["require_passkey"] is False
        assert bundle["step_up"]["enabled"] is True  # untouched by this merge

    def test_step_up_scope_writes_the_widest_value_through(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_config.json"

        rc = build_org_bundle.main([
            "-o", str(out_path), "--mode", "org",
            "--server-issuer-url", "https://pf.example.com",
            "--idp-issuer", "https://idp.example.com",
            "--idp-client-id", "cid", "--idp-client-secret", "csecret",
            "--step-up-enabled", "--step-up-scope", "writes_and_reads",
            "--sign-key", str(key_path),
        ])

        assert rc == 0
        bundle = json.loads(out_path.read_text())
        assert bundle["step_up"]["scope"] == "writes_and_reads"
        # The bundle this script writes has to survive the daemon's own
        # parser, which is the half that actually enforces the value.
        assert StepUpConfig.from_org_config(bundle).scope == "writes_and_reads"

    def test_step_up_scope_choices_match_the_daemons_own_accepted_scopes(self):
        """build_org_bundle.py is stdlib-only by design, so it repeats
        step_up_config.STEP_UP_SCOPES instead of importing it -- this is
        what catches the two drifting apart."""
        action = next(
            a for a in build_org_bundle.build_parser()._actions if a.dest == "step_up_scope"
        )
        assert tuple(action.choices) == STEP_UP_SCOPES

    def test_step_up_require_passkey_requires_mode_org(self, tmp_path):
        out_path = tmp_path / "org_config.json"
        with pytest.raises(SystemExit, match="--mode org"):
            build_org_bundle.main([
                "-o", str(out_path), "--step-up-require-passkey",
            ])

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


class TestAuditForwardingFlags:
    """org_config.json's "audit_forwarding" section, built from
    --audit-forwarding-* flags."""

    def _sign_key(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        return key_path

    def _org_mode_args(self, tmp_path, *extra):
        return [
            "-o", str(tmp_path / "org_config.json"), "--mode", "org",
            "--server-issuer-url", "https://pf.example.com",
            "--idp-issuer", "https://idp.example.com",
            "--idp-client-id", "cid", "--idp-client-secret", "csecret",
            "--sign-key", str(self._sign_key(tmp_path)),
            *extra,
        ]

    def test_requires_org_mode(self, tmp_path):
        with pytest.raises(SystemExit, match="require --mode org"):
            build_org_bundle.main([
                "-o", str(tmp_path / "org_config.json"),
                "--enable-audit-forwarding", "--audit-forwarding-syslog-host", "siem.example.com",
            ])

    def test_syslog_kind_writes_full_section(self, tmp_path):
        rc = build_org_bundle.main(self._org_mode_args(
            tmp_path,
            "--enable-audit-forwarding", "--audit-forwarding-kind", "syslog",
            "--audit-forwarding-syslog-host", "siem.example.com",
            "--audit-forwarding-syslog-port", "601",
            "--audit-forwarding-syslog-protocol", "udp",
        ))
        assert rc == 0
        bundle = json.loads((tmp_path / "org_config.json").read_text())
        assert bundle["audit_forwarding"] == {
            "enabled": True, "kind": "syslog",
            "syslog": {"host": "siem.example.com", "port": 601, "protocol": "udp"},
        }

    def test_syslog_kind_without_host_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit, match="audit-forwarding-syslog-host"):
            build_org_bundle.main(self._org_mode_args(tmp_path, "--enable-audit-forwarding"))

    def test_http_kind_writes_full_section(self, tmp_path):
        rc = build_org_bundle.main(self._org_mode_args(
            tmp_path,
            "--enable-audit-forwarding", "--audit-forwarding-kind", "http",
            "--audit-forwarding-http-url", "https://siem.example.com/ingest",
            "--audit-forwarding-http-bearer-token-env", "SIEM_TOKEN",
        ))
        assert rc == 0
        bundle = json.loads((tmp_path / "org_config.json").read_text())
        assert bundle["audit_forwarding"] == {
            "enabled": True, "kind": "http",
            "http": {"url": "https://siem.example.com/ingest", "bearer_token_env": "SIEM_TOKEN"},
        }

    def test_http_kind_without_url_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit, match="audit-forwarding-http-url"):
            build_org_bundle.main(self._org_mode_args(
                tmp_path, "--enable-audit-forwarding", "--audit-forwarding-kind", "http",
            ))

    def test_disable_flag_turns_it_back_off_on_merge(self, tmp_path):
        args = self._org_mode_args(
            tmp_path,
            "--enable-audit-forwarding", "--audit-forwarding-syslog-host", "siem.example.com",
        )
        build_org_bundle.main(args)
        key_path = tmp_path / "key.pem"  # written by _org_mode_args' own _sign_key() above

        rc = build_org_bundle.main([
            "-o", str(tmp_path / "org_config.json"), "--merge", "--disable-audit-forwarding",
            "--sign-key", str(key_path),
        ])
        assert rc == 0
        bundle = json.loads((tmp_path / "org_config.json").read_text())
        assert bundle["audit_forwarding"]["enabled"] is False
        # The rest of the section (host, etc.) survives -- only "enabled" flips.
        assert bundle["audit_forwarding"]["syslog"]["host"] == "siem.example.com"

    def test_local_mode_strips_any_existing_section(self, tmp_path):
        args = self._org_mode_args(
            tmp_path,
            "--enable-audit-forwarding", "--audit-forwarding-syslog-host", "siem.example.com",
        )
        build_org_bundle.main(args)

        rc = build_org_bundle.main([
            "-o", str(tmp_path / "org_config.json"), "--merge", "--mode", "local",
        ])
        assert rc == 0
        bundle = json.loads((tmp_path / "org_config.json").read_text())
        assert "audit_forwarding" not in bundle

    def test_summary_line_reports_enabled_state(self, tmp_path, capsys):
        build_org_bundle.main(self._org_mode_args(
            tmp_path, "--enable-audit-forwarding", "--audit-forwarding-syslog-host", "siem.example.com",
        ))
        assert "audit_forwarding.enabled=True" in capsys.readouterr().out


class TestServerBindHost:
    """The org guide says never to expose the listener directly, so a bundle
    built with no --server-bind-host binds loopback for a same-host proxy."""

    def _build(self, tmp_path, *extra):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_config.json"
        rc = build_org_bundle.main([
            "-o", str(out_path), "--mode", "org",
            "--server-issuer-url", "https://pf.example.com",
            "--idp-issuer", "https://idp.example.com",
            "--idp-client-id", "cid", "--idp-client-secret", "csecret",
            "--sign-key", str(key_path), *extra,
        ])
        assert rc == 0
        return json.loads(out_path.read_text())

    def test_defaults_to_loopback(self, tmp_path):
        assert self._build(tmp_path)["server"]["bind_host"] == "127.0.0.1"

    def test_explicit_flag_still_binds_elsewhere(self, tmp_path):
        assert self._build(tmp_path, "--server-bind-host", "0.0.0.0")["server"]["bind_host"] == "0.0.0.0"

    def test_default_matches_the_daemons_default_for_a_bundle_without_bind_host(self):
        from privacyfence import org_mode

        assert build_org_bundle.build_parser().get_default("server_bind_host") == org_mode.DEFAULT_BIND_HOST


class TestAgentLinksFlag:
    def _build(self, tmp_path, *extra, merge=False):
        key_path = tmp_path / "key.pem"
        if not key_path.exists():
            build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_config.json"
        argv = ["-o", str(out_path), "--sign-key", str(key_path)]
        if merge:
            argv.append("--merge")
        else:
            argv += [
                "--mode", "org", "--server-issuer-url", "https://pf.example.com",
                "--idp-issuer", "https://idp.example.com",
                "--idp-client-id", "cid", "--idp-client-secret", "csecret",
            ]
        assert build_org_bundle.main([*argv, *extra]) == 0
        return json.loads(out_path.read_text())

    def test_absent_leaves_the_key_out_so_the_daemon_default_applies(self, tmp_path):
        bundle = self._build(tmp_path)
        assert "agent_links" not in bundle.get("download_delivery", {})

    def test_no_agent_links_writes_false(self, tmp_path):
        assert self._build(tmp_path, "--no-agent-links")["download_delivery"]["agent_links"] is False

    def test_agent_links_writes_true(self, tmp_path):
        assert self._build(tmp_path, "--agent-links")["download_delivery"]["agent_links"] is True

    def test_agent_links_turns_it_back_on_on_merge_and_keeps_other_download_keys(self, tmp_path):
        self._build(tmp_path, "--no-agent-links", "--downloads-disable-staging")

        bundle = self._build(tmp_path, "--agent-links", merge=True)

        assert bundle["download_delivery"] == {"agent_links": True, "allow_disk_staging": False}

    def test_written_value_is_what_the_daemon_reads(self, tmp_path):
        from privacyfence import org_mode

        bundle = self._build(tmp_path, "--no-agent-links")

        assert org_mode.DownloadDeliveryConfig.from_org_config(bundle).agent_links is False

    def test_requires_org_mode(self, tmp_path):
        with pytest.raises(SystemExit, match="--agent-links"):
            build_org_bundle.main([
                "-o", str(tmp_path / "org_config.json"),
                "--slack-client-id", "id", "--slack-client-secret", "secret", "--no-agent-links",
            ])

    def test_summary_line_reports_agent_links(self, tmp_path, capsys):
        self._build(tmp_path, "--no-agent-links")
        assert "download_delivery.agent_links=False" in capsys.readouterr().out


class TestInstructionsPrintedAfterWriting:
    """The closing instructions depend on the bundle's mode: an org-mode
    bundle is installed on the server by hand (Settings has no install
    button in org mode), a local-mode bundle through Settings."""

    def _org_args(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        return [
            "-o", str(tmp_path / "org_config.json"), "--mode", "org",
            "--server-issuer-url", "https://pf.example.com/",
            "--idp-issuer", "https://idp.example.com",
            "--idp-client-id", "cid", "--idp-client-secret", "csecret",
            "--sign-key", str(key_path),
        ]

    def test_org_bundle_says_to_copy_it_to_the_server_and_restart(self, tmp_path, capsys):
        build_org_bundle.main(self._org_args(tmp_path))

        out = capsys.readouterr().out
        assert "<data>/org/org_config.json" in out
        assert "restart" in out
        assert "Install/Update" not in out

    def test_local_bundle_says_to_install_it_from_settings(self, tmp_path, capsys):
        build_org_bundle.main([
            "-o", str(tmp_path / "org_config.json"), "--slack-client-id", "id", "--slack-client-secret", "s",
        ])

        out = capsys.readouterr().out
        assert "Install/Update Organization Config" in out
        assert "<data>/org/org_config.json" not in out

    def test_org_bundle_lists_all_three_idp_redirect_uris(self, tmp_path, capsys):
        build_org_bundle.main(self._org_args(tmp_path))

        out = capsys.readouterr().out
        for path in ("/oauth/idp/callback", "/oauth/idp/login-callback", "/oauth/stepup/callback"):
            assert f"https://pf.example.com{path}" in out
        assert "'cid'" in out
        assert "/oauth/callback/" not in out  # no connector configured, nothing to list

    def test_org_bundle_lists_each_configured_connectors_callback(self, tmp_path, capsys):
        secret = tmp_path / "client_secret.json"
        secret.write_text(json.dumps({"web": {"client_id": "g", "client_secret": "gs"}}))

        build_org_bundle.main([
            *self._org_args(tmp_path),
            "--google-client-secret", str(secret),
            "--atlassian-client-id", "a", "--atlassian-client-secret", "as",
        ])

        out = capsys.readouterr().out
        for name in ("gmail", "drive", "calendar", "contacts", "tasks", "apps_script", "atlassian"):
            assert f"https://pf.example.com/oauth/callback/{name}" in out
        assert "/oauth/callback/slack" not in out

    def test_merge_without_mode_flags_reads_the_issuer_from_the_existing_bundle(self, tmp_path, capsys):
        build_org_bundle.main(self._org_args(tmp_path))
        capsys.readouterr()

        build_org_bundle.main([
            "-o", str(tmp_path / "org_config.json"), "--merge",
            "--slack-client-id", "id", "--slack-client-secret", "s",
            "--sign-key", str(tmp_path / "key.pem"),
        ])

        out = capsys.readouterr().out
        assert "https://pf.example.com/oauth/stepup/callback" in out
        assert "https://pf.example.com/oauth/callback/slack" in out
        assert "None/" not in out

    def test_help_names_the_step_up_callback_and_apps_script(self, capsys):
        with pytest.raises(SystemExit):
            build_org_bundle.main(["--help"])

        out = " ".join(capsys.readouterr().out.split())
        assert out.count("/oauth/stepup/callback") == 2
        assert "Contacts, Tasks, Apps Script)" in out
