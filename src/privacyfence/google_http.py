"""The HTTP transport every Google API client is built with.

googleapiclient reuses a pooled keep-alive TLS connection between calls. When Google or a network
device has already closed that connection, the next request on it fails with an error such as
``EOF occurred in violation of protocol`` even though a fresh connection would succeed. This module
retries such a read once, on a new connection.

The retry sits in the transport, below the approval gate, so it is invisible to approvals and the
audit log: no second approval and no second audit row.

Only ``GET`` is retried. A write may already have been applied by the time its connection dropped,
so PrivacyFence never retries one (a read-only ``POST`` stays unretried too). Timeouts, TLS
certificate failures, refused connections, DNS failures and HTTP error responses are not retried
either.

googleapiclient's ``execute(num_retries=...)`` is deliberately not used: see
docs/adr/0105-a-google-read-is-retried-once-when-its-connection-drops.md.
"""

from __future__ import annotations

import http.client
import logging
import ssl
import urllib.parse
from typing import Any

import google_auth_httplib2
import googleapiclient.http
import httplib2
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

RETRYABLE_METHODS: frozenset[str] = frozenset({"GET"})

RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (
    ssl.SSLEOFError,
    http.client.RemoteDisconnected,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)


class RetryOnceAuthorizedHttp(google_auth_httplib2.AuthorizedHttp):
    """An ``AuthorizedHttp`` that retries a ``GET`` once when its connection drops."""

    def request(
        self,
        uri: str,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> tuple[httplib2.Response, bytes]:
        # The 401-refresh re-entry from AuthorizedHttp.request carries
        # _credential_refresh_attempt; only the outermost call may retry.
        if method.upper() not in RETRYABLE_METHODS or "_credential_refresh_attempt" in kwargs:
            return super().request(uri, method, body=body, headers=headers, **kwargs)
        try:
            return super().request(uri, method, body=body, headers=headers, **kwargs)
        except RETRYABLE_ERRORS as exc:
            logger.info(
                "Google API %s to %s lost its connection (%s); retrying once on a new connection",
                method.upper(), urllib.parse.urlsplit(uri).hostname, type(exc).__name__,
            )
            self.close()
            return super().request(uri, method, body=body, headers=headers, **kwargs)


def authorized_http(creds: Credentials, http: httplib2.Http | None = None) -> RetryOnceAuthorizedHttp:
    """The transport every Google client builds its services with."""
    return RetryOnceAuthorizedHttp(creds, http=http if http is not None else googleapiclient.http.build_http())
