"""Acquire ct0 using the release-notes algorithm supplied by the user."""

import math
from collections.abc import Mapping
from typing import Protocol

import httpx

from ._http import private_http
from .exceptions import Ct0AcquisitionError


def is_cookie_value(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and all(0x21 <= ord(char) <= 0x7E and char not in '";,\\' for char in value)
    )


class Ct0Provider(Protocol):
    """Acquire ct0 for a supplied X credential without exposing either in logs.

    Implementations must not persist the credential, log sensitive response
    bodies, or include secrets in exceptions.
    """

    async def acquire(self, auth_token: str) -> str:
        """Return the session's non-empty ct0 value or raise an acquisition error."""
        ...


class ReleaseNotesCt0Provider:
    """Fetch ct0 from an HTTP 200 response to X's release-notes endpoint.

    Each acquisition uses a fresh cookie jar and closes its HTTP resources.
    A supplied transport is also closed; it must be dedicated to this operation.
    Proxy settings are explicit; environment proxy settings are not inherited.
    Redirects are reported as failures rather than followed with session cookies.
    """

    def __init__(
        self,
        *,
        headers: Mapping[str, str] | None = None,
        proxy: str | None = None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("ct0 timeout must be positive and finite.")
        self._headers = dict(headers or {})
        if any(name.lower() in {"cookie", "host"} for name in self._headers):
            raise ValueError("Cookie and Host headers are managed by the provider.")
        self._proxy = proxy
        self._timeout = timeout
        self._transport = transport

    async def acquire(self, auth_token: str) -> str:
        """Return only the new ct0 cookie; never persist or log session secrets."""
        if not is_cookie_value(auth_token):
            raise Ct0AcquisitionError("auth_token must be a non-empty cookie value.")

        token = private_http.set(True)
        try:
            async with httpx.AsyncClient(
                headers=self._headers,
                cookies={"auth_token": auth_token},
                proxy=self._proxy,
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                # Only response headers are needed; do not download the HTML body.
                async with client.stream(
                    "GET", "https://x.com/i/release_notes"
                ) as response:
                    if response.status_code != 200:
                        raise Ct0AcquisitionError(
                            "ct0 acquisition requires an HTTP 200 response "
                            f"(received {response.status_code})."
                        )
                    ct0 = response.cookies.get("ct0")
                    if not ct0:
                        raise Ct0AcquisitionError(
                            "The HTTP 200 response has no non-empty ct0 cookie."
                        )
                    return ct0
        except (httpx.HTTPError, httpx.InvalidURL, httpx.CookieConflict, ValueError):
            # HTTP/proxy errors can contain credentials or sensitive response data.
            raise Ct0AcquisitionError("ct0 acquisition failed.") from None
        finally:
            private_http.reset(token)
