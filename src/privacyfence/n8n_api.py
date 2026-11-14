"""Client for the n8n public API, used to put a bearer credential into n8n and take it out again.

It creates and deletes ``httpBearerAuth`` credentials and checks that the API key works.
Redirects are never followed, TLS is always verified, and nothing logs the API key or the
token; an error carries a fixed text with the host and at most an HTTP status, never a
response body, a header or the request (ADR 0278-0282).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 1_000_000
_CREDENTIAL_TYPE = "httpBearerAuth"
_OWN_ID_RE = re.compile(r"[0-9a-f]{16}")
_ANY_ID_RE = re.compile(r"[A-Za-z0-9]{1,16}")


class N8nApiClientError(Exception):
    """A fixed text naming the host and at most an HTTP status; never a body, header or request."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class N8nApi:
    def __init__(self, base_url: str, key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._headers = {"X-N8N-API-KEY": key, "Accept": "application/json"}

    @property
    def host(self) -> str:
        return urlsplit(self._base_url).netloc

    def __repr__(self) -> str:
        return f"N8nApi({self.host})"

    def _not_created(self) -> N8nApiClientError:
        return N8nApiClientError(f"n8n at {self.host} did not create the credential as asked.")

    def _send(
        self,
        method: str,
        path: str,
        *,
        ok: tuple[int, ...] = (200, 201, 204),
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> tuple[int, bytes]:
        """One request and the error mapping. Returns the status and the (capped) body."""
        unreachable = False
        status = 0
        body = b""
        response: requests.Response | None = None
        try:
            response = self._session.request(
                method,
                self._base_url + path,
                headers={**self._headers, **(headers or {})},
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
                stream=True,
                **kwargs,
            )
            status = response.status_code
            if status in ok:
                body = b"".join(self._chunks(response))
        except requests.RequestException:
            unreachable = True  # raised below, so the exception chain never holds the original
        finally:
            # A streamed response holds its connection until it is read or closed.
            if response is not None:
                response.close()
        if unreachable:
            raise N8nApiClientError(f"n8n at {self.host} could not be reached.")
        if status in ok:
            if len(body) > MAX_RESPONSE_BYTES:
                raise self._not_created()
            return status, body
        if 300 <= status < 400:
            raise N8nApiClientError(
                f"n8n at {self.host} redirected the request; set PRIVACYFENCE_N8N_URL to the address it redirects to.",
                status=status,
            )
        raise N8nApiClientError(f"n8n at {self.host} answered HTTP {status}.", status=status)

    @staticmethod
    def _json_object(body: bytes) -> dict[str, Any] | None:
        try:
            answer = json.loads(body)
        except ValueError:
            return None
        return answer if isinstance(answer, dict) else None

    @staticmethod
    def _chunks(response: requests.Response) -> Any:
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            total += len(chunk)
            yield chunk
            if total > MAX_RESPONSE_BYTES:
                return

    def create_bearer_credential(self, *, credential_id: str, name: str, token: str, project_id: str) -> None:
        payload: dict[str, Any] = {
            "id": credential_id,
            "name": name,
            "type": _CREDENTIAL_TYPE,
            "data": {"token": token},
        }
        if project_id:
            payload["projectId"] = project_id
        logger.info("n8n credential create at %s: %s", self.host, credential_id)
        _, body = self._send(
            "POST",
            "/api/v1/credentials",
            ok=(200, 201),
            headers={"Idempotency-Key": credential_id},
            json=payload,
        )
        answer = self._json_object(body)
        if answer is None:
            raise self._not_created()
        answered_id = answer.get("id")
        if answered_id == credential_id and answer.get("type") == _CREDENTIAL_TYPE:
            return
        if isinstance(answered_id, str) and _ANY_ID_RE.fullmatch(answered_id):
            try:
                self._delete(answered_id)
            except N8nApiClientError:
                logger.warning("n8n at %s: could not remove a credential it created under another id", self.host)
        raise self._not_created()

    def delete_credential(self, credential_id: str) -> bool:
        """True when deleted, False when n8n no longer has it (404)."""
        if not _OWN_ID_RE.fullmatch(credential_id):
            raise ValueError("Not a PrivacyFence credential id.")
        return self._delete(credential_id)

    def _delete(self, credential_id: str) -> bool:
        logger.info("n8n credential delete at %s: %s", self.host, credential_id)
        try:
            self._send("DELETE", f"/api/v1/credentials/{credential_id}", ok=(200, 204))
        except N8nApiClientError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def check(self) -> None:
        logger.info("n8n check at %s", self.host)
        self._send("GET", f"/api/v1/credentials/schema/{_CREDENTIAL_TYPE}", ok=(200,))
