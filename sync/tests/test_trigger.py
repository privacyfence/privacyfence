from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from privacyfence_sync import trigger as trigger_mod
from privacyfence_sync.client import SignInError, SyncError
from privacyfence_sync.trigger import fire

TOKEN = "sk-ant-secret-token"
URL = "https://api.anthropic.com/v1/claude_code/routines/trig_abc/fire"


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx2.Request]:
    real = httpx2.AsyncClient
    seen: list[httpx2.Request] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)  # type: ignore[no-any-return]

    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        return real(transport=httpx2.MockTransport(recording), **kwargs)

    monkeypatch.setattr(trigger_mod.httpx2, "AsyncClient", factory)
    return seen


async def start(text: str | None = "hello") -> str | None:
    return await fire("start", "trig_abc", "PRIVACYFENCE_TRIGGER_X", TOKEN, text)


async def test_posts_the_text_and_returns_the_session_url(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"type": "routine_fire", "claude_code_session_url": "https://claude.ai/code/session_1"}
    seen = _mock_http(monkeypatch, lambda request: httpx2.Response(200, json=body))
    assert await start() == "https://claude.ai/code/session_1"
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST" and str(request.url) == URL
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert json.loads(request.content) == {"text": "hello"}


async def test_sends_no_body_without_text(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_http(monkeypatch, lambda request: httpx2.Response(200, json={}))
    assert await start(None) is None
    assert seen[0].content == b""
    assert "content-type" not in seen[0].headers


async def test_non_json_success_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_http(monkeypatch, lambda request: httpx2.Response(200, content=b"not json"))
    assert await start() is None


@pytest.mark.parametrize("body", [{"type": "routine_fire"}, {"claude_code_session_url": 5}, ["x"]])
async def test_success_without_a_url_returns_none(monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    _mock_http(monkeypatch, lambda request: httpx2.Response(200, json=body))
    assert await start() is None


@pytest.mark.parametrize("code", [401, 403])
async def test_a_refused_token_is_a_sign_in_error(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    seen = _mock_http(monkeypatch, lambda request: httpx2.Response(code, text=f"bad {TOKEN}"))
    with pytest.raises(SignInError) as info:
        await start()
    assert str(info.value) == (
        f"step start: Claude refused the token in $PRIVACYFENCE_TRIGGER_X for scheduled task trig_abc (HTTP {code}); "
        "generate a new token for the task and store it again"
    )
    assert len(seen) == 1


async def test_rate_limit_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_http(monkeypatch, lambda request: httpx2.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(SyncError) as info:
        await start()
    assert not isinstance(info.value, SignInError)
    assert str(info.value) == (
        "step start: Claude is limiting how often scheduled task trig_abc starts (HTTP 429, retry after 120); "
        "the next scheduled run tries again"
    )
    assert len(seen) == 1


async def test_rate_limit_without_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_http(monkeypatch, lambda request: httpx2.Response(429))
    with pytest.raises(SyncError, match=r"starts \(HTTP 429\); the next scheduled run"):
        await start()


async def test_other_statuses_name_the_code(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_http(monkeypatch, lambda request: httpx2.Response(500, text=f"oops {TOKEN}"))
    with pytest.raises(SyncError) as info:
        await start()
    assert str(info.value) == "step start: Claude answered HTTP 500 when starting scheduled task trig_abc"
    assert TOKEN not in str(info.value) and len(seen) == 1


async def test_a_redirect_is_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_http(monkeypatch, lambda request: httpx2.Response(302, headers={"Location": "https://evil.example/"}))
    with pytest.raises(SyncError, match="HTTP 302"):
        await start()
    assert len(seen) == 1


async def test_a_transport_error_names_only_its_class(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"boom {TOKEN}")

    seen = _mock_http(monkeypatch, handler)
    with pytest.raises(SyncError) as info:
        await start()
    assert str(info.value) == "step start: cannot reach api.anthropic.com (ConnectError)"
    assert TOKEN not in str(info.value) and len(seen) == 1
