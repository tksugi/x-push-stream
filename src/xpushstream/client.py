"""Async event-stream lifecycle and resumable subscription orchestration."""

import asyncio
import logging
import math
import random
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from types import TracebackType
from typing import TYPE_CHECKING
from uuid import uuid4

from .auth import Ct0Provider, is_cookie_value
from .autopush import AckOutcome, AutoPushTransport, Notification, PushTransport
from .events import TweetEnrichmentProvider, XPushEvent, enrich
from .exceptions import (
    AutoPushConnectionError,
    ChannelConflictError,
    DecryptionError,
    NormalizationError,
    PersistentClientCredentialMismatch,
    ProtocolError,
    StateError,
    XPushError,
)
from .state import (
    FileStateStore,
    PushSubscriptionState,
    StateStore,
    credential_fingerprint,
)
from .webpush import decode64, decrypt, generate_keys
from .x_adapter import PushRegistrar, XPushRegistrar, normalize

if TYPE_CHECKING:
    from .filters import EventStream

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClientStatus:
    connected: bool
    persistent: bool
    queued_events: int
    delivered: int
    decryption_failures: int
    processing_failures: int
    reconnects: int


class XPushClient(AsyncIterator[XPushEvent]):
    """Receive X Web Push events with one async consumer per client instance.

    No client_id means ephemeral. A stable client_id opts into private state and
    per-client ownership. Injected stores and enrichment providers remain owned
    by the caller; each transport_factory result is owned by this client.
    """

    def __init__(
        self,
        auth_token: str,
        *,
        client_id: str | None = None,
        state_store: StateStore | None = None,
        ct0_provider: Ct0Provider | None = None,
        registrar: PushRegistrar | None = None,
        transport_factory: Callable[[], PushTransport] | None = None,
        normalizer: Callable[[bytes], XPushEvent] = normalize,
        enrich_tweets: bool = False,
        enrichment_provider: TweetEnrichmentProvider | None = None,
        queue_size: int = 256,
        reconnect_base_delay: float = 1,
        reconnect_max_delay: float = 60,
        max_reconnect_attempts: int | None = None,
        proxy: str | None = None,
        notify_likes: bool = True,
        notify_mentions: bool = True,
        notify_reposts: bool = True,
    ) -> None:
        if not is_cookie_value(auth_token):
            raise ValueError("auth_token must be a non-empty cookie value.")
        if client_id is not None and (not client_id or len(client_id) > 1024):
            raise ValueError("client_id must contain between 1 and 1024 characters.")
        if state_store is not None and client_id is None:
            raise ValueError("state_store requires a persistent client_id.")
        if (
            type(queue_size) is not int
            or queue_size <= 0
            or not math.isfinite(reconnect_base_delay)
            or not math.isfinite(reconnect_max_delay)
            or not 0 < reconnect_base_delay <= reconnect_max_delay
        ):
            raise ValueError("Queue size and reconnect delays must be positive.")
        if max_reconnect_attempts is not None and (
            type(max_reconnect_attempts) is not int or max_reconnect_attempts < 0
        ):
            raise ValueError("max_reconnect_attempts must be non-negative.")
        if enrich_tweets and enrichment_provider is None:
            raise ValueError("enrich_tweets requires an enrichment_provider.")
        preferences = (notify_likes, notify_mentions, notify_reposts)
        if any(type(value) is not bool for value in preferences):
            raise ValueError("Notification preferences must be True or False.")
        if registrar is not None and not all(preferences):
            raise ValueError("Configure notification settings on the custom registrar.")
        self._auth_token = auth_token
        self._client_id = client_id
        self._store = state_store or (
            FileStateStore() if client_id is not None else None
        )
        self._registrar = registrar or XPushRegistrar(
            ct0_provider=ct0_provider,
            proxy=proxy,
            notify_likes=notify_likes,
            notify_mentions=notify_mentions,
            notify_reposts=notify_reposts,
        )
        self._transport_factory = transport_factory or (
            lambda: AutoPushTransport(proxy=proxy)
        )
        self._normalizer = normalizer
        self._enrichment = enrichment_provider
        self._queue: asyncio.Queue[XPushEvent] = asyncio.Queue(queue_size)
        self._base_delay = reconnect_base_delay
        self._max_delay = reconnect_max_delay
        self._max_attempts = max_reconnect_attempts
        self._attempts = 0
        self._state: PushSubscriptionState | None = None
        self._settings_applied_endpoint: str | None = None
        self._transport: PushTransport | None = None
        self._task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._returned: deque[XPushEvent] = deque()
        self._ready = False
        self._stack = AsyncExitStack()
        self._entered = self._closed = self._reading = False
        self._failure: XPushError | None = None
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._connected_at = 0.0
        self._delivered = self._decryption_failures = self._processing_failures = (
            self._reconnects
        ) = 0

    @property
    def status(self) -> ClientStatus:
        return ClientStatus(
            self._ready and not self._closed,
            self._client_id is not None,
            self._queue.qsize() + len(self._returned),
            self._delivered,
            self._decryption_failures,
            self._processing_failures,
            self._reconnects,
        )

    async def __aenter__(self) -> "XPushClient":
        if self._entered or self._closed:
            raise RuntimeError("A client instance can be entered only once.")
        self._entered = True
        try:
            if self._store is not None and self._client_id is not None:
                await self._stack.enter_async_context(
                    self._store.claim(self._client_id)
                )
                self._state = await self._store.load(self._client_id)
                if self._state is not None:
                    try:
                        self._state.validate()
                    except (ValueError, TypeError):
                        raise StateError(
                            "Persistent subscription state is invalid."
                        ) from None
                    if self._state.credential_fingerprint != credential_fingerprint(
                        self._auth_token
                    ):
                        raise PersistentClientCredentialMismatch(
                            "Persistent client was created with different credentials. "
                            "Use another client_id or reset its state."
                        )
            await self._connect_with_retries()
            self._task = asyncio.create_task(self._run(), name="xpushstream-receiver")
            return self
        except BaseException:
            await self.aclose()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _save(self) -> None:
        if (
            self._store is not None
            and self._client_id is not None
            and self._state is not None
        ):
            await self._store.save(self._client_id, self._state)

    async def _connect(self) -> None:
        transport = self._transport_factory()
        self._transport = transport
        previous = self._state
        uaid = await transport.open(
            previous.uaid if previous else None,
            previous.channel_id if previous else None,
        )
        if previous is None or previous.uaid != uaid:
            keys = generate_keys()
            for attempt in range(3):
                channel_id = str(uuid4())
                try:
                    endpoint = await transport.register(
                        channel_id, self._registrar.application_server_key
                    )
                    break
                except ChannelConflictError:
                    if attempt == 2:
                        raise
            self._state = PushSubscriptionState(
                uaid,
                channel_id,
                endpoint,
                keys,
                credential_fingerprint(self._auth_token),
                f"XPushStream/{uuid4()}",
            )
            try:
                self._state.validate()
            except (ValueError, TypeError):
                raise ProtocolError(
                    "AutoPush returned an invalid subscription."
                ) from None
            await self._save()
        if self._state is None:
            raise XPushError("Subscription initialization failed.")
        if not self._state.registered:
            await self._registrar.register(self._state, self._auth_token)
            self._state = replace(self._state, registered=True)
            await self._save()
            self._settings_applied_endpoint = self._state.endpoint
        elif self._settings_applied_endpoint != self._state.endpoint and isinstance(
            self._registrar, XPushRegistrar
        ):
            await self._registrar.apply_settings(self._state, self._auth_token)
            self._settings_applied_endpoint = self._state.endpoint
        self._connected_at = time.monotonic()
        self._ready = True

    async def _backoff(self, error: AutoPushConnectionError) -> None:
        if self._max_attempts is not None and self._attempts >= self._max_attempts:
            raise AutoPushConnectionError("Reconnect attempts exhausted.")
        ceiling = min(self._max_delay, self._base_delay * 2 ** min(self._attempts, 30))
        self._attempts += 1
        self._reconnects += 1
        delay = max(error.retry_after, random.uniform(ceiling / 2, ceiling))
        logger.info("AutoPush connection interrupted; retrying after backoff.")
        await asyncio.sleep(delay)

    async def _disconnect(self) -> None:
        self._ready = False
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                await asyncio.wait_for(transport.close(), 5)
            except Exception:
                pass

    async def _connect_with_retries(self) -> None:
        while True:
            try:
                await self._connect()
                return
            except AutoPushConnectionError as error:
                await self._disconnect()
                await self._backoff(error)

    async def _process(self, notification: Notification) -> None:
        state, transport = self._state, self._transport
        if state is None or transport is None:
            raise XPushError("The subscription is not ready.")
        if notification.channel_id != state.channel_id:
            await transport.ack(notification, AckOutcome.PROCESSING_FAILURE)
            return
        identity = (notification.channel_id, notification.version)
        if identity in self._seen:
            await transport.ack(notification, AckOutcome.DELIVERED)
            return
        try:
            if notification.data is None:
                plaintext = b"{}"
            else:
                try:
                    data = decode64(notification.data)
                except ValueError:
                    raise DecryptionError(
                        "Invalid Web Push base64url payload."
                    ) from None
                plaintext = decrypt(data, notification.headers, state.keys)
        except DecryptionError:
            self._decryption_failures += 1
            await transport.ack(notification, AckOutcome.DECRYPTION_FAILURE)
            logger.warning("A Web Push notification could not be decrypted.")
            return
        try:
            event = self._normalizer(plaintext)
            if not isinstance(event, XPushEvent):
                raise NormalizationError("The normalizer did not return an event.")
        except Exception:
            self._processing_failures += 1
            await transport.ack(notification, AckOutcome.PROCESSING_FAILURE)
            logger.warning("A Web Push notification could not be normalized.")
            return
        if self._enrichment is not None:
            event = await enrich(event, self._enrichment)
        await self._queue.put(event)
        self._seen[identity] = None
        if len(self._seen) > 4096:
            self._seen.popitem(last=False)
        self._delivered += 1
        await transport.ack(notification, AckOutcome.DELIVERED)

    async def _run(self) -> None:
        try:
            while True:
                try:
                    if self._transport is None:
                        raise AutoPushConnectionError()
                    notification = await self._transport.receive()
                    await self._process(notification)
                    self._attempts = 0
                except AutoPushConnectionError as error:
                    if time.monotonic() - self._connected_at > 30:
                        self._attempts = 0
                    await self._disconnect()
                    await self._backoff(error)
                    await self._connect_with_retries()
        except XPushError as error:
            self._failure = error
        except Exception:
            self._failure = XPushError("The event stream failed.")
        finally:
            if not self._closed:
                await self._disconnect()

    def __aiter__(self) -> "XPushClient":
        return self

    async def __anext__(self) -> XPushEvent:
        if self._closed:
            raise StopAsyncIteration
        if self._task is None:
            raise RuntimeError("Enter the client context before receiving events.")
        if self._reading:
            raise RuntimeError("Only one consumer may read a client at a time.")
        self._reading = True
        pending: asyncio.Task[XPushEvent] | None = None
        try:
            if self._returned:
                return self._returned.popleft()
            if not self._queue.empty():
                return self._queue.get_nowait()
            pending = asyncio.create_task(self._queue.get())
            await asyncio.wait(
                {pending, self._task}, return_when=asyncio.FIRST_COMPLETED
            )
            if pending.done():
                return pending.result()
            if self._failure is not None:
                raise self._failure
            raise StopAsyncIteration
        except asyncio.CancelledError:
            if pending is not None and pending.done() and not pending.cancelled():
                self._returned.appendleft(pending.result())
            raise
        finally:
            self._reading = False
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    def stream(self) -> "EventStream":
        from .filters import EventStream

        return EventStream(self)

    async def _cleanup(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        try:
            if (
                self._client_id is None
                and self._transport is not None
                and self._state is not None
            ):
                try:
                    await asyncio.wait_for(
                        self._transport.unregister(self._state.channel_id), 3
                    )
                except Exception:
                    pass
        finally:
            try:
                await self._disconnect()
                await self._stack.aclose()
            finally:
                self._auth_token = ""
                self._state = None
                self._seen.clear()
                self._returned.clear()
                while not self._queue.empty():
                    self._queue.get_nowait()

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(
                self._cleanup(), name="xpushstream-cleanup"
            )
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await self._close_task
            raise
