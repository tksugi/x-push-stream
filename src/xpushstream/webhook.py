"""Normalized webhook routing with bounded retries; no transport dependencies."""

import asyncio
import math
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from ._http import private_http, retry_after
from .events import XPushEvent
from .exceptions import WebhookDeliveryError


class WebhookDestination:
    """Own each HTTP operation; propagate permanent failures to the application.

    Delivery is in-process. A process restart does not replay completed SDK ACKs.
    The caller chooses how to handle exhausted or permanent delivery failures.
    """

    def __init__(
        self,
        url: str,
        *,
        retries: int = 3,
        timeout: float = 10,
        include_raw: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError
            _ = parsed.port
        except (ValueError, TypeError):
            raise ValueError(
                "Webhook URL must use HTTPS without URL credentials or fragments."
            ) from None
        if (
            type(retries) is not int
            or not 0 <= retries <= 10
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("Invalid webhook retry or timeout configuration.")
        self._url, self._retries, self._timeout = url, retries, timeout
        self._include_raw, self._transport = include_raw, transport

    async def deliver(self, event: XPushEvent) -> None:
        token = private_http.set(True)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                for attempt in range(self._retries + 1):
                    server_delay = 0.0
                    try:
                        async with client.stream(
                            "POST",
                            self._url,
                            json=event.to_dict(include_raw=self._include_raw),
                        ) as response:
                            if 200 <= response.status_code < 300:
                                return
                            if (
                                response.status_code
                                not in {
                                    408,
                                    425,
                                    429,
                                }
                                and not 500 <= response.status_code < 600
                            ):
                                raise WebhookDeliveryError(
                                    "Webhook rejected the event "
                                    f"(HTTP {response.status_code})."
                                )
                            server_delay = retry_after(
                                response.headers.get("Retry-After")
                            )
                    except httpx.HTTPError:
                        pass
                    if attempt == self._retries:
                        break
                    await asyncio.sleep(max(server_delay, min(30, 2**attempt)))
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError):
            raise WebhookDeliveryError("Webhook delivery failed.") from None
        finally:
            private_http.reset(token)
        raise WebhookDeliveryError("Webhook delivery exhausted its retries.")


@dataclass(frozen=True)
class Route:
    predicate: Callable[[XPushEvent], bool]
    destination: WebhookDestination


class WebhookRouter:
    def __init__(self, routes: list[Route]) -> None:
        self._routes = tuple(routes)

    async def dispatch(self, event: XPushEvent) -> None:
        # Dispatch every matching route, even if another destination fails.
        deliveries = [
            route.destination.deliver(event)
            for route in self._routes
            if route.predicate(event)
        ]
        results = await asyncio.gather(*deliveries, return_exceptions=True)
        if any(isinstance(result, asyncio.CancelledError) for result in results):
            raise asyncio.CancelledError
        if any(isinstance(result, Exception) for result in results):
            raise WebhookDeliveryError("One or more webhook routes failed.")

    async def run(self, events: AsyncIterable[XPushEvent]) -> None:
        async for event in events:
            await self.dispatch(event)
