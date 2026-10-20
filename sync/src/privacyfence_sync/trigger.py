"""Trigger steps: start a Claude Code scheduled task through its API trigger (ADR 0207)."""

from __future__ import annotations

import httpx2

from .client import SignInError, SyncError

FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/{trigger}/fire"
ANTHROPIC_VERSION = "2023-06-01"
_TIMEOUT_SECONDS = 30.0


async def fire(step_id: str, trigger: str, token_env: str, token: str, text: str | None) -> str | None:
    """Start the scheduled task once; return its session URL when Claude reports one."""
    headers = {"Authorization": f"Bearer {token}", "anthropic-version": ANTHROPIC_VERSION}
    async with httpx2.AsyncClient(timeout=_TIMEOUT_SECONDS) as http:
        try:
            if text is None:
                response = await http.post(FIRE_URL.format(trigger=trigger), headers=headers)
            else:
                response = await http.post(FIRE_URL.format(trigger=trigger), headers=headers, json={"text": text})
        except httpx2.HTTPError as exc:
            raise SyncError(f"step {step_id}: cannot reach api.anthropic.com ({type(exc).__name__})") from None
    code = response.status_code
    if code in (401, 403):
        raise SignInError(
            f"step {step_id}: Claude refused the token in ${token_env} for scheduled task {trigger} "
            f"(HTTP {code}); generate a new token for the task and store it again"
        )
    if code == 429:
        retry_after = response.headers.get("Retry-After")
        after = f", retry after {retry_after}" if retry_after is not None else ""
        raise SyncError(
            f"step {step_id}: Claude is limiting how often scheduled task {trigger} starts "
            f"(HTTP 429{after}); the next scheduled run tries again"
        )
    if not 200 <= code < 300:
        raise SyncError(f"step {step_id}: Claude answered HTTP {code} when starting scheduled task {trigger}")
    try:
        body = response.json()
    except ValueError:
        return None
    url = body.get("claude_code_session_url") if isinstance(body, dict) else None
    return url if isinstance(url, str) else None
