from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from privacyfence_sync import auth
from privacyfence_sync.client import SignInError, SyncError

SERVER = "https://pf.example/mcp"


def test_storage_round_trips_tokens_and_client_info(tmp_path: Path) -> None:
    storage = auth.FileTokenStorage(tmp_path / "sub" / "t.json")
    assert asyncio.run(storage.get_tokens()) is None
    assert asyncio.run(storage.get_client_info()) is None
    assert not storage.has_tokens()
    info = OAuthClientInformationFull(
        client_id="abc", redirect_uris=["http://127.0.0.1:33418/callback"], token_endpoint_auth_method="none"
    )
    tokens = OAuthToken(access_token="at", token_type="Bearer", refresh_token="rt", expires_in=60)
    asyncio.run(storage.set_client_info(info))
    asyncio.run(storage.set_tokens(tokens))
    assert storage.has_tokens()
    assert asyncio.run(storage.get_tokens()) == tokens
    assert asyncio.run(storage.get_client_info()) == info


def test_storage_ignores_a_file_that_is_not_an_object(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text("[1]", encoding="utf-8")
    assert asyncio.run(auth.FileTokenStorage(path).get_tokens()) is None


def test_storage_cleans_up_when_the_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(
            auth.FileTokenStorage(tmp_path / "t.json").set_tokens(OAuthToken(access_token="a", token_type="Bearer"))
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes do not apply on Windows")
def test_storage_file_is_mode_600(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    asyncio.run(auth.FileTokenStorage(path).set_tokens(OAuthToken(access_token="a", token_type="Bearer")))
    assert path.stat().st_mode & 0o777 == 0o600


def test_storage_path_uses_xdg_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    digest = hashlib.sha256(SERVER.encode()).hexdigest()[:16]
    assert auth.storage_path(SERVER) == tmp_path / "privacyfence-sync" / f"{digest}.json"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert auth.storage_path(SERVER) == tmp_path / "home" / ".config" / "privacyfence-sync" / f"{digest}.json"


def _metadata(provider: object) -> object:
    return provider.context.client_metadata  # type: ignore[attr-defined]


def test_non_interactive_handlers_raise_the_login_hint(tmp_path: Path) -> None:
    provider = auth.oauth_provider(
        SERVER, auth.FileTokenStorage(tmp_path / "t.json"), interactive=False, callback_port=33418
    )
    hint = f"Not signed in to {SERVER}, or the sign-in expired. Run: privacyfence-sync login --server {SERVER}"
    for handler, args in (
        (provider.context.redirect_handler, ("http://as/authorize",)),
        (provider.context.callback_handler, ()),
    ):
        assert handler is not None
        with pytest.raises(SignInError) as caught:
            asyncio.run(handler(*args))  # type: ignore[arg-type]
        assert str(caught.value) == hint


def test_client_metadata_is_a_public_loopback_client(tmp_path: Path) -> None:
    for interactive in (False, True):
        provider = auth.oauth_provider(
            SERVER, auth.FileTokenStorage(tmp_path / "t.json"), interactive=interactive, callback_port=4321
        )
        meta = provider.context.client_metadata
        assert meta.client_name == "privacyfence-sync"
        assert [str(u) for u in meta.redirect_uris or []] == ["http://127.0.0.1:4321/callback"]
        assert meta.grant_types == ["authorization_code", "refresh_token"]
        assert meta.response_types == ["code"]
        assert meta.token_endpoint_auth_method == "none"


def _get(port: int, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:  # noqa: S310
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_callback_server_returns_code_and_state() -> None:
    server = auth._bind_callback_server(0)
    port = server.server_address[1]
    replies: list[tuple[int, str]] = []
    thread = threading.Thread(
        target=lambda: replies.extend(
            [_get(port, "/other"), _get(port, "/callback?code=c1&state=s1&iss=https%3A%2F%2Fas")]
        )
    )
    thread.start()
    result = auth._wait_for_callback(server, 5)
    thread.join()
    assert (result.code, result.state, result.iss) == ("c1", "s1", "https://as")
    assert replies[0][0] == 404
    assert replies[1] == (200, "Signed in to PrivacyFence. You can close this tab.")


def test_callback_without_a_code_fails_the_sign_in() -> None:
    server = auth._bind_callback_server(0)
    port = server.server_address[1]
    replies: list[tuple[int, str]] = []
    thread = threading.Thread(target=lambda: replies.append(_get(port, "/callback?error=access_denied")))
    thread.start()
    with pytest.raises(SyncError, match="Sign-in failed: access_denied"):
        auth._wait_for_callback(server, 5)
    thread.join()
    assert replies[0][0] == 400


def test_callback_gives_up_after_the_timeout() -> None:
    server = auth._bind_callback_server(0)
    started = time.monotonic()
    with pytest.raises(SyncError, match="No sign-in arrived within 5 minutes."):
        auth._wait_for_callback(server, 0.05)
    assert time.monotonic() - started < 5


def test_binding_a_busy_port_names_the_flag() -> None:
    first = auth._bind_callback_server(0)
    try:
        with pytest.raises(SyncError, match="pass --callback-port"):
            auth._bind_callback_server(first.server_address[1])
    finally:
        first.server_close()


def test_interactive_sign_in_prints_the_link_and_opens_the_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(auth.webbrowser, "open", lambda url: opened.append(url))
    flow = auth._InteractiveSignIn(0, timeout=5)

    async def drive() -> object:
        await flow.redirect_handler("http://as/authorize?x=1")
        assert flow._server is not None
        port = flow._server.server_address[1]
        getter = asyncio.get_running_loop().run_in_executor(None, _get, port, "/callback?code=z")
        result = await flow.callback_handler()
        await getter
        return result

    result = asyncio.run(drive())
    assert result.code == "z"  # type: ignore[attr-defined]
    assert opened == ["http://as/authorize?x=1"]
    assert capsys.readouterr().out == "Open this page to sign in: http://as/authorize?x=1\n"


def test_callback_before_the_sign_in_page_is_an_error() -> None:
    with pytest.raises(SyncError, match="before the sign-in page"):
        asyncio.run(auth._InteractiveSignIn(0).callback_handler())


def test_state_file_is_json_with_both_sections(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    storage = auth.FileTokenStorage(path)
    asyncio.run(storage.set_tokens(OAuthToken(access_token="a", token_type="Bearer")))
    assert set(json.loads(path.read_text())) == {"tokens"}


def test_connect_oauth_wires_the_provider_into_the_client(tmp_path: Path) -> None:
    from privacyfence_sync.client import SyncClient, connect_oauth

    storage = auth.FileTokenStorage(tmp_path / "t.json")
    client = connect_oauth(SERVER, storage)
    assert isinstance(client, SyncClient)
    assert client._headers == {}
    provider = client._auth
    assert [str(u) for u in provider.context.client_metadata.redirect_uris or []] == [  # type: ignore[union-attr]
        f"http://127.0.0.1:{auth.DEFAULT_CALLBACK_PORT}/callback"
    ]
    other = connect_oauth(SERVER, storage, interactive=True, callback_port=4000)
    assert [str(u) for u in other._auth.context.client_metadata.redirect_uris or []] == [  # type: ignore[union-attr]
        "http://127.0.0.1:4000/callback"
    ]
