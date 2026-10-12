"""Exercise the user-specified ct0 algorithm without live X credentials."""

import asyncio
import logging
import traceback
from collections.abc import AsyncIterator

import httpx
import pytest

from xpushstream.auth import Ct0Provider, ReleaseNotesCt0Provider
from xpushstream.exceptions import Ct0AcquisitionError


def test_acquire_sends_auth_cookie_and_returns_response_ct0() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://x.com/i/release_notes"
        assert request.headers["cookie"] == "auth_token=synthetic-token"
        assert request.headers["user-agent"] == "test-agent"
        assert request.extensions["timeout"]["read"] == 3.0
        return httpx.Response(
            200,
            headers=[
                ("set-cookie", "other=ignored; Path=/"),
                ("set-cookie", "ct0=synthetic-ct0; Path=/; Secure; HttpOnly"),
            ],
        )

    provider: Ct0Provider = ReleaseNotesCt0Provider(
        headers={"User-Agent": "test-agent"},
        timeout=3.0,
        transport=httpx.MockTransport(respond),
    )
    assert asyncio.run(provider.acquire("synthetic-token")) == "synthetic-ct0"


@pytest.mark.parametrize("status", [201, 204, 302, 401, 403, 429, 500])
def test_rejects_non_200_even_with_ct0_and_does_not_follow_redirects(
    status: int,
) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={
                "set-cookie": "ct0=synthetic-ct0; Path=/",
                "location": "https://other.invalid/",
            },
        )

    provider = ReleaseNotesCt0Provider(transport=httpx.MockTransport(respond))
    with pytest.raises(Ct0AcquisitionError, match=f"received {status}"):
        asyncio.run(provider.acquire("synthetic-token"))
    assert calls == 1


@pytest.mark.parametrize("cookie", [None, "other=value; Path=/", "ct0=; Path=/"])
def test_requires_a_non_empty_ct0_cookie_in_the_response(cookie: str | None) -> None:
    provider = ReleaseNotesCt0Provider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={} if cookie is None else {"set-cookie": cookie}
            )
        )
    )
    with pytest.raises(Ct0AcquisitionError, match="non-empty ct0"):
        asyncio.run(provider.acquire("synthetic-token"))


def test_rejects_ambiguous_ct0_cookies() -> None:
    provider = ReleaseNotesCt0Provider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers=[
                    ("set-cookie", "ct0=first; Path=/"),
                    ("set-cookie", "ct0=second; Path=/i/"),
                ],
            )
        )
    )
    with pytest.raises(Ct0AcquisitionError, match="acquisition failed"):
        asyncio.run(provider.acquire("synthetic-token"))


@pytest.mark.parametrize("token", ["", "a;b", "a\r\nb", "a b", "a,b", "a\\b", "日本語"])
def test_rejects_invalid_cookie_values_before_network(token: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid credentials must not reach the transport.")

    provider = ReleaseNotesCt0Provider(transport=httpx.MockTransport(respond))
    with pytest.raises(Ct0AcquisitionError, match="cookie value"):
        asyncio.run(provider.acquire(token))


@pytest.mark.parametrize("header", ["Cookie", "cookie", "Host"])
def test_custom_headers_cannot_override_authentication_or_destination(
    header: str,
) -> None:
    with pytest.raises(ValueError, match="managed by the provider"):
        ReleaseNotesCt0Provider(headers={header: "synthetic-secret"})


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_secrets_are_absent_from_logs_repr_and_formatted_exceptions(
    error_type: type[httpx.RequestError], caplog: pytest.LogCaptureFixture
) -> None:
    token = "synthetic-token-secret"
    ct0 = "synthetic-ct0-secret"
    proxy = "synthetic-proxy-secret"

    def respond(request: httpx.Request) -> httpx.Response:
        raise error_type(f"Cookie: auth_token={token}; ct0={ct0}; proxy={proxy}")

    provider = ReleaseNotesCt0Provider(transport=httpx.MockTransport(respond))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Ct0AcquisitionError) as caught:
            asyncio.run(provider.acquire(token))

    output = (
        caplog.text + repr(provider) + "".join(traceback.format_exception(caught.value))
    )
    for secret in (token, ct0, proxy):
        assert secret not in output


def test_each_acquisition_has_an_independent_cookie_jar() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        expected = "first-token" if calls == 1 else "second-token"
        assert request.headers["cookie"] == f"auth_token={expected}"
        return httpx.Response(
            200, headers={"set-cookie": "ct0=first-ct0; Path=/"} if calls == 1 else {}
        )

    provider = ReleaseNotesCt0Provider(transport=httpx.MockTransport(respond))

    async def acquire_twice() -> None:
        assert await provider.acquire("first-token") == "first-ct0"
        with pytest.raises(Ct0AcquisitionError, match="non-empty ct0"):
            await provider.acquire("second-token")

    asyncio.run(acquire_twice())


def test_response_body_is_not_read_and_transport_is_closed() -> None:
    class Body(httpx.AsyncByteStream):
        was_read = False
        was_closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            self.was_read = True
            yield b"Private response body must not be downloaded."

        async def aclose(self) -> None:
            self.was_closed = True

    class Transport(httpx.MockTransport):
        was_closed = False

        async def aclose(self) -> None:
            self.was_closed = True

    body = Body()
    transport = Transport(
        lambda request: httpx.Response(
            200, headers={"set-cookie": "ct0=synthetic-ct0; Path=/"}, stream=body
        )
    )
    provider = ReleaseNotesCt0Provider(transport=transport)
    assert asyncio.run(provider.acquire("synthetic-token")) == "synthetic-ct0"
    assert not body.was_read
    assert body.was_closed
    assert transport.was_closed


def test_cancellation_propagates_and_closes_the_transport() -> None:
    class Transport(httpx.MockTransport):
        was_closed = False

        async def aclose(self) -> None:
            self.was_closed = True

    async def respond(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    transport = Transport(respond)
    provider = ReleaseNotesCt0Provider(transport=transport)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider.acquire("synthetic-token"))
    assert transport.was_closed
