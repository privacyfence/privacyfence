"""Builds signed connector configs for tests, independently of ``privacyfence.connector_configs``.

The text format is written out here rather than imported so that a mistake in the module under
test cannot also be a mistake in the files it is tested against. ``tests/fixtures/connector_config/``
holds a key, the inputs and the expected signed files; the signing tools must reproduce them byte
for byte. Run ``python -m tests.connector_config_support`` to write them again.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "connector_config"
HEADER = "privacyfence-connector-config v1"
# Never a real credential; tests that check for leaks use their own sentinel.
SENTINEL = "SENTINEL-client-secret-do-not-leak"


# key.pem is a throwaway test key that signs nothing real; it is checked in on purpose (the
# repository ignores *.pem elsewhere) so that every checkout reproduces the expected files.
def fixture_key() -> Ed25519PrivateKey:
    key = load_pem_private_key((FIXTURE_DIR / "key.pem").read_bytes(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return key


def public_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def signed_text(
    provider: str, serial: int, fields: list[tuple[str, str]], signed_at: str, key: Ed25519PrivateKey
) -> bytes:
    lines = [HEADER, f"provider: {provider}", f"serial: {serial}"]
    lines += [f"{name}: {value}" for name, value in fields]
    lines += [f"signed_at: {signed_at}", f"key: {base64.b64encode(public_bytes(key)).decode()}"]
    message = ("\n".join(lines) + "\n").encode("ascii")
    signature = base64.b64encode(key.sign(message)).decode()
    return message + f"signature: {signature}\n".encode("ascii")


def sign(provider: str, serial: int = 1, key: Ed25519PrivateKey | None = None, **overrides: str) -> bytes:
    """A valid file for ``provider`` with the sentinel secret; ``overrides`` replace field values."""
    secret = "consumer_secret" if provider == "salesforce" else "client_secret"
    ident = "consumer_key" if provider == "salesforce" else "client_id"
    fields = [(ident, f"{provider}-id"), (secret, SENTINEL)]
    if provider == "grist":
        fields += [("auth_server_url", "https://auth.grist.example"), ("server_url", "https://grist.example")]
    fields = [(name, overrides.get(name, value)) for name, value in fields]
    if provider == "salesforce" and "login_url" in overrides:
        fields.append(("login_url", overrides["login_url"]))
    return signed_text(provider, serial, fields, "2026-11-02T10:00:00Z", key or fixture_key())


def fixture_inputs() -> dict[str, dict]:
    return json.loads((FIXTURE_DIR / "inputs.json").read_text(encoding="utf-8"))


def expected_file(provider: str) -> bytes:
    return (FIXTURE_DIR / f"{provider}.pfconnector").read_bytes()


def build_expected(provider: str) -> bytes:
    spec = fixture_inputs()[provider]
    return signed_text(provider, spec["serial"], [tuple(pair) for pair in spec["fields"]], spec["signed_at"], fixture_key())


INPUTS = {
    "slack": {
        "serial": 3,
        "signed_at": "2026-11-02T10:00:00Z",
        "fields": [["client_id", "1234.5678"], ["client_secret", "fixture-slack-secret-0001"]],
    },
    "salesforce": {
        "serial": 2026110201,
        "signed_at": "2026-11-02T10:05:00Z",
        "fields": [
            ["consumer_key", "3MVG9fixtureConsumerKey"],
            ["consumer_secret", "fixture-salesforce-secret-0002"],
            ["login_url", "https://acme.my.salesforce.com"],
        ],
    },
    "grist": {
        "serial": 7,
        "signed_at": "2026-11-02T10:10:00Z",
        "fields": [
            ["client_id", "grist-fixture-client"],
            ["client_secret", "fixture-grist-secret-0003"],
            ["auth_server_url", "https://auth.grist.example"],
            ["server_url", "https://grist.example"],
        ],
    },
}


def regenerate() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    key_path = FIXTURE_DIR / "key.pem"
    if not key_path.exists():
        key = Ed25519PrivateKey.generate()
        key_path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    (FIXTURE_DIR / "inputs.json").write_text(json.dumps(INPUTS, indent=2) + "\n", encoding="utf-8")
    for provider in INPUTS:
        (FIXTURE_DIR / f"{provider}.pfconnector").write_bytes(build_expected(provider))


if __name__ == "__main__":
    regenerate()
