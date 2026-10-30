#!/usr/bin/env python3
"""Copy the QA principal's tokens between the QA server's data directory and a checkout.

The QA organization server (docs/connector-qa.md, "The QA server") writes the tokens when the
maintainer connects an account in a browser. The live-check workflows run ``pull`` before the
recorder and ``push`` after it (``if: always()``), so the recorder works on a copy and a token the
run refreshed goes back.

``pull --data-dir DIR [--checkout DIR] [--principal-id ID]``
    Find the QA principal's folder under ``<data-dir>/users/``: the folder named by
    ``--principal-id`` (default: the PF_QA_PRINCIPAL_ID environment variable) when it is set,
    otherwise the single folder whose ``credentials/`` holds a ``daemon_main.TOKEN_FILES`` file.
    With none, or several, it fails and names the candidate ids (not secrets). It copies that
    ``credentials/`` to ``<checkout>/users/qa-live/credentials/`` (files mode 0600) and records
    each file's SHA-256 in ``<checkout>/.qa-pull.json``.

``push --data-dir DIR [--checkout DIR]``
    Write back only the files the run changed, each atomically (a temporary file in the same
    directory, then a rename). A file whose server copy changed since ``pull`` -- for example one
    the maintainer reconnected during the run -- is skipped with a warning.

A symlink anywhere on either side is refused. Nothing here prints a token or a file's content,
only file names and principal ids. Stdlib only, like scripts/builtin_apps.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

QA_PRINCIPAL = "qa-live"
RECORD = ".qa-pull.json"
ENV_PRINCIPAL = "PF_QA_PRINCIPAL_ID"
REPO_ROOT = Path(__file__).resolve().parent.parent


class TokenError(Exception):
    """A refusal whose message is safe to print: it never carries file content."""


def _token_files() -> set[str]:
    """The credentials-relative names of the token files, from the daemon's own table."""
    from privacyfence import daemon_main

    return {value.split("/", 1)[1] for value in daemon_main.TOKEN_FILES.values()}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(root: Path) -> dict[str, Path]:
    """Every regular file under ``root`` by relative name; a symlink is refused."""
    found: dict[str, Path] = {}
    if root.is_symlink():
        raise TokenError(f"{root} is a symlink")
    if not root.is_dir():
        return found
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in [*dirs, *names]:
            if (Path(current) / name).is_symlink():
                raise TokenError(f"{Path(current, name)} is a symlink")
        for name in names:
            path = Path(current) / name
            found[path.relative_to(root).as_posix()] = path
    return found


def _principal_dir(users: Path, principal_id: str, token_files: set[str]) -> Path:
    if users.is_symlink() or not users.is_dir():
        raise TokenError(f"{users} is not a directory")
    if principal_id:
        if "/" in principal_id or principal_id in {".", ".."}:
            raise TokenError(f"{ENV_PRINCIPAL} is not a folder name")
        folder = users / principal_id
        if folder.is_symlink() or not folder.is_dir():
            raise TokenError(f"no folder for principal {principal_id!r} under {users}")
        return folder
    candidates = sorted(
        folder for folder in users.iterdir()
        if folder.is_dir() and not folder.is_symlink()
        and any((folder / "credentials" / name).is_file() for name in token_files)
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise TokenError(f"no principal under {users} holds a token file")
    ids = ", ".join(folder.name for folder in candidates)
    raise TokenError(f"several principals hold token files ({ids}); set {ENV_PRINCIPAL} to one of them")


def _credentials_dir(checkout: Path) -> Path:
    return checkout / "users" / QA_PRINCIPAL / "credentials"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def pull(data_dir: Path, checkout: Path, principal_id: str) -> int:
    folder = _principal_dir(data_dir / "users", principal_id, _token_files())
    source = _files(folder / "credentials")
    if not source:
        raise TokenError(f"principal {folder.name!r} has no credentials")
    destination = _credentials_dir(checkout)
    digests: dict[str, str] = {}
    for name, path in sorted(source.items()):
        data = path.read_bytes()
        _write_atomic(destination / name, data)
        digests[name] = hashlib.sha256(data).hexdigest()
    (checkout / RECORD).write_text(json.dumps({"principal": folder.name, "files": digests}, indent=2))
    print(f"pulled {len(digests)} credential file(s) for principal {folder.name}")
    return 0


def push(data_dir: Path, checkout: Path) -> int:
    record_path = checkout / RECORD
    if not record_path.is_file():
        raise TokenError(f"{record_path} is missing: pull did not run")
    record = json.loads(record_path.read_text())
    principal, recorded = str(record["principal"]), dict(record["files"])
    target = data_dir / "users" / principal / "credentials"
    local = _files(_credentials_dir(checkout))
    server = _files(target)
    pushed = 0
    for name, path in sorted(local.items()):
        digest = _digest(path)
        if recorded.get(name) == digest:
            continue
        if name in server and _digest(server[name]) != recorded.get(name):
            print(f"warning: {name} changed on the server since pull; not overwritten", file=sys.stderr)
            continue
        if name not in recorded and name in server:
            print(f"warning: {name} appeared on the server since pull; not overwritten", file=sys.stderr)
            continue
        _write_atomic(target / name, path.read_bytes())
        print(f"pushed {name}")
        pushed += 1
    print(f"pushed {pushed} credential file(s) for principal {principal}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("pull", "push"):
        command = sub.add_parser(name)
        command.add_argument("--data-dir", type=Path, required=True)
        command.add_argument("--checkout", type=Path, default=REPO_ROOT)
        if name == "pull":
            command.add_argument("--principal-id", default=os.environ.get(ENV_PRINCIPAL, ""))
    args = parser.parse_args(argv)
    try:
        if args.command == "pull":
            return pull(args.data_dir, args.checkout, args.principal_id.strip())
        return push(args.data_dir, args.checkout)
    except TokenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
