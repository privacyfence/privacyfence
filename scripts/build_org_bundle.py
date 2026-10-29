#!/usr/bin/env python3
"""Build a PrivacyFence desktop (local mode) config bundle (org_bundle.json).

Run this once per organization after registering each cloud app (see the
"For IT admins" section of docs/google-cloud-setup.md, docs/slack-setup.md,
docs/salesforce-setup.md, and docs/atlassian-setup.md). The output file is
what you distribute to your users -- they install it via "Install/Update
Organization Config…" on the General page of PrivacyFence Settings (the
embedded web page).

A bundle carries the connector app credentials (Google, Slack, Salesforce,
Atlassian, Grist, Miro) and its own bookkeeping (the organization name, and
with --sign-key a serial and a signature). Everything operational -- which
services are offered, file delivery, web push, sign-in restrictions, step-up,
scoped tokens, audit forwarding and unattended sessions -- is set on the Admin
page of PrivacyFence Settings, not here; the options that used to write those
settings are still recognised only to tell you so.

Organization servers take no bundle: their settings come from privacyfence.env
and their own connector apps from signed connector configs (see
docs/org-mode-setup-guide.md). This script refuses --mode org and every
--server-* and --idp-* option, and --merge refuses an organization bundle.

Telegram's api_id/api_hash identify the PrivacyFence app itself (not your
organization) and are baked into the release build -- see docs/telegram-setup.md
and src/privacyfence/app_credentials.py -- so a bundle carries no secret for it.

Stdlib only -- no PrivacyFence install required to run this -- except
--sign-key/--generate-signing-key (bundle signing, below), which need the
`cryptography` package (pip install cryptography) specifically, not a
full PrivacyFence install.

--sign-key also signs a "serial" into the bundle (--serial N, default: the
current Unix time); an install refuses a bundle whose serial is lower than the
highest it has accepted, so a later build always wins.

Example:
    python3 scripts/build_org_bundle.py \\
        --org-name "Acme Corp" \\
        --google-client-secret ~/Downloads/client_secret_....json \\
        --slack-client-id 1234.5678 --slack-client-secret abcdef \\
        --salesforce-consumer-key 3MVG9... --salesforce-consumer-secret abc \\
        --atlassian-client-id abc123 --atlassian-client-secret def456 \\
        -o org_bundle.json
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# The bundle sections that hold connector app credentials; a bundle with none of
# them has nothing worth writing.
_CREDENTIAL_SECTIONS = ("google", "slack", "salesforce", "atlassian", "grist", "miro")

# Mirrors admin_config.LEGACY_BUNDLE_KEYS; this script stays standard-library only.
_LEGACY_BUNDLE_KEYS = (
    "download_delivery", "web_push", "authz", "step_up", "scoped_tokens", "audit_forwarding",
    "unattended_sessions", "rooms", "rooms_synced_at", "telegram", "peek",
)

_ORG_MODE_MESSAGE = (
    "error: {option}: organization servers take their settings from privacyfence.env and their own "
    "apps from signed connector configs (docs/org-mode-setup-guide.md, ADR 0268)."
)
_MOVED_MESSAGE = "error: {option} has moved to the Admin page (ADR 0256); remove it."
_MOVED_WITH_ENV_MESSAGE = (
    "error: {option} has moved to the Admin page and PRIVACYFENCE_PEEK_BASE_URL / "
    "PRIVACYFENCE_GRIST_SERVER_URL / PRIVACYFENCE_GRIST_PROVISIONER_URL on the server (ADR 0256, 0263); "
    "remove it."
)
_BUNDLE_IS_ORG_MESSAGE = (
    "error: {file} is an organization bundle; organization servers no longer take bundles (ADR 0268)."
)

# Options that wrote an operational setting into the bundle. They stay parseable, hidden from --help,
# only so that main() can say where the setting went.
_MOVED_OPTIONS = (
    "--enable-telegram", "--enable-peek", "--enable-grist", "--peek-base-url",
    "--atlassian-assets", "--atlassian-assets-workspace-id",
    "--authz-allowed-domain", "--authz-groups-claim", "--authz-required-group",
    "--step-up-enabled", "--step-up-disabled", "--step-up-scope", "--step-up-rp-id", "--step-up-rp-name",
    "--step-up-require-passkey", "--step-up-no-require-passkey",
    "--enable-unattended-sessions", "--disable-unattended-sessions",
    "--enable-scoped-tokens", "--disable-scoped-tokens", "--scoped-token-max-days",
    "--enable-scoped-token-proposals", "--disable-scoped-token-proposals",
    "--downloads-inline-max-bytes", "--downloads-link-ttl-seconds", "--downloads-disable-staging",
    "--agent-links", "--no-agent-links", "--exports", "--no-exports",
    "--web-push", "--no-web-push",
    "--enable-audit-forwarding", "--disable-audit-forwarding", "--audit-forwarding-kind",
    "--audit-forwarding-syslog-host", "--audit-forwarding-syslog-port", "--audit-forwarding-syslog-protocol",
    "--audit-forwarding-http-url", "--audit-forwarding-http-bearer-token-env",
)
# The auto-provisioning options also have environment variables on the server.
_MOVED_WITH_ENV_OPTIONS = (
    "--peek-auto-provision", "--no-peek-auto-provision", "--peek-provisioning-key-env",
    "--peek-trust-unverified-email", "--grist-auto-provision", "--no-grist-auto-provision",
    "--grist-provisioner-url", "--grist-provisioning-key-env", "--grist-trust-unverified-email",
)
# Organization-mode options, by destination prefix so that a later --idp-* option is refused as well.
_ORG_MODE_DEST_PREFIXES = ("server_", "idp_")
# (option, dest) for the organization-mode options that are not simply the option name with "_" for "-".
_ORG_MODE_OPTIONS = (
    ("--server-issuer-url", "server_issuer_url"), ("--server-bind-host", "server_bind_host"),
    ("--server-port", "server_port"), ("--server-tls-cert", "server_tls_cert"),
    ("--server-tls-key", "server_tls_key"), ("--server-trusted-proxy", "server_trusted_proxies"),
    ("--idp-issuer", "idp_issuer"), ("--idp-client-id", "idp_client_id"),
    ("--idp-client-secret", "idp_client_secret"), ("--idp-admin-group-claim", "idp_admin_group_claim"),
    ("--idp-admin-group-value", "idp_admin_group_values"),
    ("--idp-step-up-acr-value", "idp_step_up_acr_values"),
)


def _grist_url(value: str) -> str:
    """Validate one Grist address and strip its trailing ``/`` (mirrors ``grist_auth.normalize_server_url``;
    this script stays standard-library only)."""
    url = value.strip().rstrip("/")
    parts = urlsplit(url)
    if not parts.hostname or parts.scheme not in ("https", "http") or (
        parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1")
    ):
        raise SystemExit(
            "--grist-server-url and --grist-auth-server-url must be https:// addresses (http:// only for localhost)."
        )
    if (
        parts.username is not None or parts.password is not None or parts.query or parts.fragment
        or "api" in parts.path.split("/")
    ):
        raise SystemExit(
            "--grist-server-url and --grist-auth-server-url must not contain a user name, password, query, "
            "fragment or /api."
        )
    return url


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive whole number.")
    return number


def _canonical_payload_bytes(bundle: dict[str, Any]) -> bytes:
    """Mirrors src/privacyfence/org_bundle_signing.py's own
    ``_canonical_payload_bytes`` exactly -- the two MUST stay byte-for-
    byte identical, or a bundle signed here will fail to verify there.
    Not imported from that module because this script is meant to be
    runnable standalone (see module docstring) without a PrivacyFence
    install; only ``cryptography`` needs to be pip-installed separately
    to use --sign-key/--generate-signing-key at all.
    """
    payload = {k: v for k, v in bundle.items() if k != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _require_cryptography():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
            load_pem_private_key,
        )
    except ImportError as exc:
        raise SystemExit(
            "--sign-key/--generate-signing-key need the `cryptography` package: "
            "pip install cryptography"
        ) from exc
    return ed25519, Encoding, NoEncryption, PrivateFormat, PublicFormat, load_pem_private_key


def _generate_signing_key(path: str) -> int:
    ed25519, Encoding, NoEncryption, PrivateFormat, PublicFormat, _load = _require_cryptography()
    private_key = ed25519.Ed25519PrivateKey.generate()
    out = Path(path)
    out.write_bytes(private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    try:
        out.chmod(0o600)
    except OSError:  # pragma: no cover - best effort on non-POSIX
        pass
    pub_b64 = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    print(f"Wrote Ed25519 signing private key to {out} -- keep this file secret.")
    print(f"Public key (embedded in every bundle you sign with it): {pub_b64}")
    print(
        "Pass this file to every future build_org_bundle.py run (including --merge) via "
        "--sign-key so all your bundles keep verifying against the same key -- the first signed "
        "bundle each install sees pins that key and rejects anything that doesn't verify against "
        "it afterwards, unsigned bundles included. Losing this key means every install that has "
        "already pinned it can't accept an update until an administrator deletes their pinned "
        "org_bundle_signing_pubkey.txt (and, to go back to 6.x, org_config_signing_pubkey.txt) by hand."
    )
    return 0


def _sign_bundle(bundle: dict[str, Any], sign_key_path: str) -> dict[str, Any]:
    ed25519, Encoding, _NoEncryption, _PrivateFormat, PublicFormat, load_pem_private_key = _require_cryptography()
    with open(sign_key_path, "rb") as fh:
        private_key = load_pem_private_key(fh.read(), password=None)
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise SystemExit(
            f"{sign_key_path} is not an Ed25519 private key "
            "(generate one with --generate-signing-key)."
        )
    signed = dict(bundle)
    signed.pop("signature", None)
    signed["signing_public_key"] = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    signature = private_key.sign(_canonical_payload_bytes(signed))
    signed["signature"] = base64.b64encode(signature).decode("ascii")
    return signed


def _load_google_client_secret(path: str) -> dict[str, Any]:
    """Extract the inner "installed"/"web" block from Google's client_secret.json.

    PrivacyFence stores it flat (no wrapper) in the bundle and re-wraps it
    when handing it to google-auth-oauthlib at authorize time.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    inner = data.get("installed") or data.get("web")
    if not inner:
        raise SystemExit(
            f"{path} doesn't look like a Google OAuth client_secret.json "
            '(expected a top-level "installed" or "web" key). Download it from '
            "Google Cloud Console -> APIs & Services -> Credentials, for an "
            "OAuth client of type 'Desktop app' (local mode's loopback flow) or "
            "'Web application' (org mode's server-redirect flow needs an "
            "explicit, registered HTTPS redirect URI -- see docs/org-mode-"
            "setup-guide.md's §4.2)."
        )
    return inner


def _add_refused_options(parser: argparse.ArgumentParser) -> None:
    """Register every option that no longer builds anything, hidden from --help.

    They take any number of values so that a leftover command line parses; main() then prints where
    each one went and exits without writing.
    """
    for option in (*_MOVED_OPTIONS, *_MOVED_WITH_ENV_OPTIONS):
        parser.add_argument(option, nargs="*", help=argparse.SUPPRESS)
    for option, dest in _ORG_MODE_OPTIONS:
        parser.add_argument(option, dest=dest, nargs="*", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a PrivacyFence desktop config bundle (org_bundle.json).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
        # Refused options are matched by their exact spelling in main(); a prefix must not slip past.
        allow_abbrev=False,
    )
    parser.add_argument("--org-name", default="", help="Shown to users after they install the bundle.")
    parser.add_argument("-o", "--output", default="org_bundle.json", help="Output path (default: org_bundle.json).")
    parser.add_argument(
        "--merge", action="store_true",
        help="Merge into an existing desktop bundle at the output path instead of overwriting it "
             "(useful for adding one more service to an already-distributed bundle). Operational keys an "
             "older bundle still carries are dropped; they are managed on the Admin page now.",
    )
    parser.add_argument(
        "--mode", choices=["local", "org"], default=None,
        help="\"local\" is the only mode this script builds, and the default: leaving it out is the same. "
             "Organization servers take no bundle.",
    )

    google = parser.add_argument_group("Google (Gmail, Drive, Calendar, Contacts, Tasks, Apps Script)")
    google.add_argument(
        "--google-client-secret", metavar="PATH",
        help="Path to the client_secret.json downloaded from Google Cloud Console "
             "(an OAuth client of type 'Desktop app').",
    )

    slack = parser.add_argument_group("Slack")
    slack.add_argument("--slack-client-id")
    slack.add_argument("--slack-client-secret")
    slack.add_argument(
        "--slack-scopes", nargs="+", metavar="SCOPE",
        help="Override the default Slack user-token scopes (advanced; usually leave unset).",
    )

    salesforce = parser.add_argument_group("Salesforce")
    salesforce.add_argument("--salesforce-consumer-key")
    salesforce.add_argument("--salesforce-consumer-secret")
    salesforce.add_argument(
        "--salesforce-login-url", default="https://login.salesforce.com",
        help="Default: https://login.salesforce.com (use https://test.salesforce.com for sandboxes).",
    )

    atlassian = parser.add_argument_group("Atlassian (Jira + Confluence)")
    atlassian.add_argument("--atlassian-client-id")
    atlassian.add_argument("--atlassian-client-secret")

    grist = parser.add_argument_group("Grist (an OAuth app)")
    grist.add_argument(
        "--grist-server-url", metavar="URL",
        help="The Grist server the OAuth app belongs to (for example https://docs.getgrist.com); "
             "needed with --grist-client-id and --grist-client-secret.",
    )
    grist.add_argument("--grist-client-id", help="A Grist OAuth app; give the client id and secret together.")
    grist.add_argument("--grist-client-secret")
    grist.add_argument(
        "--grist-auth-server-url", metavar="URL",
        help="Where Grist's sign-in discovery document is served, when the server itself does not serve it "
             "(only with the client id and secret). Default: the server URL.",
    )

    miro = parser.add_argument_group("Miro")
    miro.add_argument("--miro-client-id", help="The Miro app people sign in with; give the client id and secret together.")
    miro.add_argument("--miro-client-secret")

    _add_refused_options(parser)

    signing = parser.add_argument_group(
        "Bundle signing (src/privacyfence/org_bundle_signing.py)",
    )
    signing.add_argument(
        "--generate-signing-key", metavar="PATH",
        help="Generate a new Ed25519 signing keypair, write the private key (PEM, PKCS8) to "
             "PATH with 0600 permissions, print its public key, and exit without building a "
             "bundle. Run this once per organization and keep PATH secret -- pass it to "
             "--sign-key on every future run (including --merge) so all your bundles keep "
             "verifying against the same key. Needs the `cryptography` package.",
    )
    signing.add_argument(
        "--sign-key", metavar="PATH",
        help="Sign the bundle with the Ed25519 private key at PATH (from "
             "--generate-signing-key). Optional but recommended: an install that has pinned a "
             "signing key refuses an unsigned bundle. Needs the `cryptography` package.",
    )
    signing.add_argument(
        "--serial", type=_positive_int, metavar="N", default=None,
        help="Serial signed into the bundle (default: the current Unix time). Installs refuse a "
             "bundle whose serial is lower than the highest they have accepted.",
    )

    return parser


def _refused_option_lines(parser: argparse.ArgumentParser, args: argparse.Namespace, argv: list[str]) -> list[str]:
    """One ``error:`` line per refused option the command line contains, in the order given."""
    message_for: dict[str, str] = {}
    for option in _MOVED_OPTIONS:
        message_for[option] = _MOVED_MESSAGE
    for option in _MOVED_WITH_ENV_OPTIONS:
        message_for[option] = _MOVED_WITH_ENV_MESSAGE
    for action in parser._actions:
        if action.dest.startswith(_ORG_MODE_DEST_PREFIXES):
            for option in action.option_strings:
                message_for[option] = _ORG_MODE_MESSAGE
    lines: list[str] = []
    seen: set[str] = set()
    for token in argv:
        option = token.split("=", 1)[0] if token.startswith("--") else token
        if option in seen:
            continue
        message = message_for.get(option)
        label = option
        if option == "--mode" and args.mode == "org":
            message, label = _ORG_MODE_MESSAGE, "--mode org"
        elif option == "--grist-server-url" and not args.grist_client_id:
            # The address of a Grist OAuth app stays; on its own it only pinned an API-key server.
            message = _MOVED_MESSAGE
        if message is not None:
            seen.add(option)
            lines.append(message.format(option=label))
    return lines


def _drop_legacy_keys(bundle: dict[str, Any]) -> list[str]:
    """Remove the operational settings an older bundle carries; return their dotted names.

    Mirrors admin_config.legacy_keys_in, which names the same keys when the daemon imports a bundle.
    """
    dropped = [key for key in _LEGACY_BUNDLE_KEYS if bundle.pop(key, None) is not None]
    atlassian = bundle.get("atlassian")
    if isinstance(atlassian, dict):
        for key in ("assets", "assets_workspace_id"):
            if atlassian.pop(key, None) is not None:
                dropped.append(f"atlassian.{key}")
    grist = bundle.get("grist")
    if isinstance(grist, dict):
        if not grist.get("client_id") and grist.pop("server_url", None) is not None:
            dropped.append("grist.server_url")
        if grist.pop("auto_provision", None) is not None:
            dropped.append("grist.auto_provision")
        if not grist:
            del bundle["grist"]
    return dropped


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    refused = _refused_option_lines(parser, args, argv)
    if refused:
        for line in refused:
            print(line, file=sys.stderr)
        raise SystemExit(2)
    if args.serial is not None and not args.sign_key:
        parser.error("--serial needs --sign-key (only a signed bundle carries a serial).")

    if args.generate_signing_key:
        return _generate_signing_key(args.generate_signing_key)

    out_path = Path(args.output)
    bundle: dict[str, Any] = {}
    if args.merge and out_path.exists():
        with open(out_path, encoding="utf-8") as fh:
            bundle = json.load(fh)
        if bundle.get("mode") == "org" or "server" in bundle or "idp" in bundle:
            print(_BUNDLE_IS_ORG_MESSAGE.format(file=out_path), file=sys.stderr)
            raise SystemExit(2)
    # Any existing signature was over the bundle as it stood at that
    # earlier signing -- it's stale the moment anything below changes it,
    # and even when nothing changes, whether to re-affirm it is exactly
    # what --sign-key (re-)signing below decides. Never carry a
    # signature/key forward implicitly.
    bundle.pop("signature", None)
    bundle.pop("signing_public_key", None)
    bundle.pop("serial", None)
    dropped = _drop_legacy_keys(bundle)
    if dropped:
        print(f"note: dropped {', '.join(dropped)}; they are managed on the Admin page now.")
    # A 5.0.0a1 bundle may carry mcp.tool_annotations, which the daemon now
    # refuses to start with (ADR 0089). --merge is the way to rebuild one, so
    # drop the key here rather than carry forward a bundle that cannot load.
    mcp_section = bundle.get("mcp")
    if isinstance(mcp_section, dict) and "tool_annotations" in mcp_section:
        del mcp_section["tool_annotations"]
        if not mcp_section:
            del bundle["mcp"]
        print("Dropped the removed mcp.tool_annotations key from the merged bundle (ADR 0089).")

    bundle["version"] = 1
    if args.org_name:
        bundle["org_name"] = args.org_name
    bundle["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if args.google_client_secret:
        bundle["google"] = _load_google_client_secret(args.google_client_secret)

    if args.slack_client_id or args.slack_client_secret:
        if not (args.slack_client_id and args.slack_client_secret):
            raise SystemExit("--slack-client-id and --slack-client-secret must be given together.")
        slack: dict[str, Any] = {"client_id": args.slack_client_id, "client_secret": args.slack_client_secret}
        if args.slack_scopes:
            slack["user_scopes"] = args.slack_scopes
        bundle["slack"] = slack

    if args.salesforce_consumer_key or args.salesforce_consumer_secret:
        if not (args.salesforce_consumer_key and args.salesforce_consumer_secret):
            raise SystemExit("--salesforce-consumer-key and --salesforce-consumer-secret must be given together.")
        bundle["salesforce"] = {
            "consumer_key": args.salesforce_consumer_key,
            "consumer_secret": args.salesforce_consumer_secret,
            "login_url": args.salesforce_login_url,
        }

    if args.atlassian_client_id or args.atlassian_client_secret:
        if not (args.atlassian_client_id and args.atlassian_client_secret):
            raise SystemExit("--atlassian-client-id and --atlassian-client-secret must be given together.")
        bundle["atlassian"] = {"client_id": args.atlassian_client_id, "client_secret": args.atlassian_client_secret}

    if args.grist_server_url or args.grist_client_id or args.grist_client_secret or args.grist_auth_server_url:
        if bool(args.grist_client_id) != bool(args.grist_client_secret):
            raise SystemExit("--grist-client-id and --grist-client-secret must be given together.")
        if not args.grist_server_url:
            raise SystemExit(
                "--grist-client-id, --grist-client-secret and --grist-auth-server-url need --grist-server-url."
            )
        grist_section: dict[str, Any] = {
            "server_url": _grist_url(args.grist_server_url),
            "client_id": args.grist_client_id,
            "client_secret": args.grist_client_secret,
        }
        if args.grist_auth_server_url:
            grist_section["auth_server_url"] = _grist_url(args.grist_auth_server_url)
        bundle["grist"] = grist_section

    if args.miro_client_id or args.miro_client_secret:
        if not (args.miro_client_id and args.miro_client_secret):
            raise SystemExit("--miro-client-id and --miro-client-secret must be given together.")
        bundle["miro"] = {"client_id": args.miro_client_id, "client_secret": args.miro_client_secret}

    if args.mode == "local":
        bundle["mode"] = "local"

    services = [k for k in _CREDENTIAL_SECTIONS if k in bundle]
    if not services:
        raise SystemExit(
            "No app credentials given (Google, Slack, Salesforce, Atlassian, Grist or Miro) -- nothing to write."
        )

    if args.sign_key:
        bundle["serial"] = args.serial if args.serial is not None else int(time.time())
        bundle = _sign_bundle(bundle, args.sign_key)

    out_path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    try:
        out_path.chmod(0o600)
    except OSError:  # pragma: no cover - best effort on non-POSIX
        pass
    print(f"Wrote {out_path} with: {', '.join(services)}, signed={'signature' in bundle}")
    if "signature" in bundle:
        print(
            "This bundle is signed. The first install that reads it will trust and pin its "
            "signing key (trust-on-first-use) -- every bundle installed on that machine "
            "afterwards, including any future unsigned one, must verify against that same key."
        )
    print(
        'Distribute this file to your users. They install it via "Install/Update '
        'Organization Config…" on the General page of PrivacyFence Settings.'
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
