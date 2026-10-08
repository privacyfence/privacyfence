"""The SDK ships its own copy of the licence files; they must match the repo root."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("name", ["LICENSE", "NOTICE"])
def test_sdk_copy_is_byte_identical_to_root(name: str) -> None:
    assert (ROOT / "plugin-sdk" / name).read_bytes() == (ROOT / name).read_bytes()
