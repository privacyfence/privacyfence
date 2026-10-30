#!/usr/bin/env python3
"""Sign a connector config: an organization's own OAuth app for one provider.

An organization server takes its own app for Google, Slack, Salesforce, Atlassian, Miro or Grist
as a small signed text file, which an administrator uploads on Admin -> Services. Only the holder
of the signing key can make one, so a hijacked admin session cannot swap in someone else's app.
The same file can be made in a browser with ``website/tools/connector-config/index.html``; both
produce the same bytes for the same input.

Two steps, once per organization and then once per app:

    python3 scripts/sign_connector_config.py keygen --out connector-key.pem

    python3 scripts/sign_connector_config.py sign \\
        --provider slack --serial 1 --client-id 1234.5678 \\
        --key connector-key.pem --client-secret-file slack-secret.txt \\
        --output slack.pfconnector

Keep the key file private and backed up: it cannot be recovered, and a server accepts only the
key it saw first. ``keygen`` prints the key's fingerprint; compare it with the one the Admin page
shows before you confirm the first upload.

The client secret is read from ``--client-secret-file`` or typed at a hidden prompt. It is never
taken from the command line, where ``ps`` and the shell history would keep it. Output files are
created with mode 0600 and an existing file is never overwritten.

Needs the ``cryptography`` package (``pip install cryptography``); nothing else.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

HEADER = "privacyfence-connector-config v1"
PROVIDERS = ("google", "slack", "salesforce", "atlassian", "miro", "grist")
MAX_FILE_BYTES = 8 * 1024
MAX_VALUE_CHARS = 512
MAX_SERIAL_DIGITS = 15

# Field names after the serial, in file order, per provider. Salesforce keeps the names of its
# bundle section; ``login_url`` is optional there.
_FIELDS: dict[str, tuple[str, ...]] = {
    "salesforce": ("consumer_key", "consumer_secret", "login_url"),
    "grist": ("client_id", "client_secret", "auth_server_url", "server_url"),
}
_DEFAULT_FIELDS = ("client_id", "client_secret")


class ToolError(Exception):
    """A problem the administrator can fix; the message is safe to print."""


def _fail(message: str) -> NoReturn:
    raise ToolError(message)


def _crypto():
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    except ImportError:
        _fail("This tool needs the cryptography package: pip install cryptography")
    return InvalidSignature, serialization, Ed25519PrivateKey, Ed25519PublicKey


def fingerprint(public_key: bytes) -> str:
    """``SHA256:`` and the unpadded base64 of the key's SHA-256, as OpenSSH shows a key."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(public_key).digest()).decode("ascii").rstrip("=")


def _public_bytes(private_key) -> bytes:
    _, serialization, _, _ = _crypto()
    return private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _write_new_file(path: Path, data: bytes) -> None:
    """Creates ``path`` with mode 0600, failing if anything is already there (a symlink included)."""
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        _fail(f"{path} already exists; this tool does not overwrite files. Choose another name.")
    except OSError as error:
        _fail(f"Cannot create {path}: {error.strerror or error}")
    try:
        # The creation mode is subject to the umask, which can only remove bits, but say it anyway.
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def keygen(out: Path) -> tuple[str, str]:
    """Writes a new PKCS#8 PEM private key to ``out``; returns (base64 public key, fingerprint)."""
    _, serialization, Ed25519PrivateKey, _ = _crypto()
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    _write_new_file(out, pem)
    public = _public_bytes(private_key)
    return base64.b64encode(public).decode("ascii"), fingerprint(public)


def load_private_key(path: Path):
    _, serialization, _, _ = _crypto()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        data = path.read_bytes()
    except OSError as error:
        _fail(f"Cannot read the key file {path}: {error.strerror or error}")
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError):
        _fail(f"{path} is not an unencrypted PKCS#8 PEM private key. Make one with: keygen --out FILE")
    if not isinstance(key, Ed25519PrivateKey):
        _fail(f"{path} is not an Ed25519 key.")
    return key


def _check_value(name: str, value: str) -> str:
    if not value or len(value) > MAX_VALUE_CHARS:
        _fail(f"{name} must be 1 to {MAX_VALUE_CHARS} characters.")
    if value != value.strip(" "):
        _fail(f"{name} must not start or end with a space.")
    if any(ord(char) < 0x20 or ord(char) > 0x7E for char in value):
        _fail(f"{name} must be printable ASCII.")
    return value


def field_names(provider: str) -> tuple[str, ...]:
    return _FIELDS.get(provider, _DEFAULT_FIELDS)


def build_signed_text(
    provider: str, serial: str, fields: dict[str, str], signed_at: str, private_key
) -> bytes:
    """The signed file's bytes. ``fields`` maps field names to values; an empty or missing
    optional ``login_url`` is left out."""
    if provider not in PROVIDERS:
        _fail(f"provider must be one of {', '.join(PROVIDERS)}.")
    if not serial.isdigit() or serial.startswith("0") or len(serial) > MAX_SERIAL_DIGITS:
        _fail("serial must be a positive whole number without leading zeros, at most 15 digits.")
    lines = [HEADER, f"provider: {provider}", f"serial: {serial}"]
    for name in field_names(provider):
        value = fields.get(name, "")
        if name == "login_url" and not value:
            continue
        lines.append(f"{name}: {_check_value(name, value)}")
    try:
        # strptime alone accepts "2026-1-2T1:0:0Z", which the server then rejects.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", signed_at):
            raise ValueError(signed_at)
        datetime.strptime(signed_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _fail("signed_at must look like 2026-11-02T10:00:00Z.")
    public = _public_bytes(private_key)
    lines += [f"signed_at: {signed_at}", f"key: {base64.b64encode(public).decode('ascii')}"]
    message = ("\n".join(lines) + "\n").encode("ascii")
    signature = base64.b64encode(private_key.sign(message)).decode("ascii")
    text = message + f"signature: {signature}\n".encode("ascii")
    if len(text) > MAX_FILE_BYTES:
        _fail(f"A connector config is at most {MAX_FILE_BYTES // 1024} KB.")
    return text


def verify_signed_text(text: bytes) -> None:
    """Checks the signature of ``text`` against the key it carries; raises ToolError otherwise."""
    InvalidSignature, _, _, Ed25519PublicKey = _crypto()
    head, _, tail = text.rpartition(b"signature: ")
    lines = text.decode("ascii").split("\n")
    key_line = next((line for line in lines if line.startswith("key: ")), "")
    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(key_line[len("key: "):], validate=True))
        public.verify(base64.b64decode(tail.strip(), validate=True), head)
    except (ValueError, InvalidSignature):
        _fail("The file just made does not verify against its own key; nothing was written.")


def read_secret(secret_file: Path | None, label: str) -> str:
    """The client secret from a file (one trailing line ending removed) or a hidden prompt."""
    if secret_file is not None:
        try:
            text = secret_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            _fail(f"Cannot read the secret file {secret_file}: {getattr(error, 'strerror', None) or 'not UTF-8'}")
        for ending in ("\r\n", "\n"):
            if text.endswith(ending):
                return text[: -len(ending)]
        return text
    return getpass.getpass(f"{label}: ")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sign_connector_config.py",
        description="Make the signing key for connector configs, and sign one provider's app credentials.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    gen = commands.add_parser("keygen", help="write a new private signing key", allow_abbrev=False)
    gen.add_argument("--out", type=Path, required=True, help="where to write the PKCS#8 PEM key (mode 0600, never overwritten)")

    sign = commands.add_parser("sign", help="sign one provider's credentials", allow_abbrev=False)
    sign.add_argument("--provider", required=True, choices=PROVIDERS)
    sign.add_argument("--serial", required=True, help="a whole number higher than any serial used before for this provider, e.g. 2026110201")
    sign.add_argument("--client-id", required=True, help="the client ID (Salesforce: the consumer key)")
    sign.add_argument("--client-secret-file", type=Path, help="a file holding the client secret; omit to type it at a hidden prompt")
    # Registered only to refuse it with a clear message: a secret on the command line is kept by
    # ``ps`` and the shell history. argparse would otherwise report it as an unknown option, and
    # --client-secret-file must never be reachable by abbreviating it.
    sign.add_argument("--client-secret", help=argparse.SUPPRESS)
    sign.add_argument("--key", type=Path, required=True, help="the private key from keygen")
    sign.add_argument("--login-url", help="Salesforce only: your org's https://<name>.my.salesforce.com address")
    sign.add_argument("--auth-server-url", help="Grist only")
    sign.add_argument("--server-url", help="Grist only")
    sign.add_argument("--signed-at", help="default: now, UTC, like 2026-11-02T10:00:00Z")
    sign.add_argument("--output", type=Path, required=True, help="the file to write (mode 0600, never overwritten)")
    return parser


def _run_sign(args: argparse.Namespace) -> int:
    if args.client_secret is not None:
        _fail(
            "--client-secret is refused: a secret on the command line stays in the process list and the "
            "shell history. Use --client-secret-file FILE, or leave it out to be asked."
        )
    provider = args.provider
    allowed = {
        "login_url": provider == "salesforce",
        "auth_server_url": provider == "grist",
        "server_url": provider == "grist",
    }
    for option, ok in allowed.items():
        if getattr(args, option) is not None and not ok:
            _fail(f"--{option.replace('_', '-')} does not apply to {provider}.")
    if provider == "grist" and not (args.auth_server_url and args.server_url):
        _fail("grist needs --auth-server-url and --server-url.")

    names = field_names(provider)
    secret_label = "Consumer secret" if provider == "salesforce" else "Client secret"
    private_key = load_private_key(args.key)
    # Refuse an existing output before asking for the secret, so a typo costs nothing.
    if os.path.lexists(args.output):
        _fail(f"{args.output} already exists; this tool does not overwrite files. Choose another name.")
    values = {names[0]: args.client_id, names[1]: read_secret(args.client_secret_file, secret_label)}
    values["login_url"] = args.login_url or ""
    values["auth_server_url"] = args.auth_server_url or ""
    values["server_url"] = args.server_url or ""
    signed_at = args.signed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    text = build_signed_text(provider, args.serial, values, signed_at, private_key)
    verify_signed_text(text)
    _write_new_file(args.output, text)
    public = _public_bytes(private_key)
    print(f"Wrote {args.output} (mode 0600): {provider} connector config, serial {args.serial}.")
    print(f"Signing key fingerprint: {fingerprint(public)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "keygen":
            public, print_fingerprint = keygen(args.out)
            print(f"Wrote the private key to {args.out} (mode 0600). Keep it private and back it up.")
            print(f"Public key:  {public}")
            print(f"Fingerprint: {print_fingerprint}")
            return 0
        return _run_sign(args)
    except ToolError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
