"""Mozilla AutoPush protocol, independently implemented from public contracts."""

import asyncio
import json
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Protocol
from uuid import UUID

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidProxy,
    InvalidStatus,
    InvalidURI,
)

from ._http import retry_after
from .exceptions import AutoPushConnectionError, ChannelConflictError, ProtocolError
from .webpush import MAX_PAYLOAD


class AckOutcome(IntEnum):
    DELIVERED = 100
    DECRYPTION_FAILURE = 101
    PROCESSING_FAILURE = 102


@dataclass(frozen=True, repr=False)
class Notification:
    channel_id: str
    version: str
    data: str | None = field(default=None, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, message: dict[str, Any]) -> "Notification":
        try:
            channel = str(UUID(message["channelID"]))
            version = message["version"]
            data = message.get("data")
            headers = message.get("headers", {})
            if not isinstance(version, str) or not version or len(version) > 4096:
                raise ValueError
            if data is not None and not isinstance(data, str):
                raise ValueError
            if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
            ):
                raise ValueError
            # AutoPush carries HTTP Crypto-Key as crypto_key in its JSON map.
            fields = {
                name.lower().replace("_", "-"): value for name, value in headers.items()
            }
            if len(fields) != len(headers):
                raise ValueError
            return cls(channel, version, data, fields)
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ProtocolError("Invalid AutoPush notification.") from None


class PushTransport(Protocol):
    async def open(self, uaid: str | None, channel_id: str | None) -> str: ...
    async def register(self, channel_id: str, application_server_key: str) -> str: ...
    async def receive(self) -> Notification: ...
    async def ack(self, notification: Notification, outcome: AckOutcome) -> None: ...
    async def unregister(self, channel_id: str) -> None: ...
    async def close(self) -> None: ...


def hello(uaid: str | None, channel_id: str | None) -> dict[str, Any]:
    return {
        "messageType": "hello",
        "uaid": uaid or "",
        "channelIDs": [channel_id] if channel_id else [],
        "use_webpush": True,
    }


class AutoPushTransport:
    def __init__(
        self,
        *,
        endpoint: str = "wss://push.services.mozilla.com/",
        timeout: float = 15,
        proxy: str | None = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("AutoPush timeout must be positive and finite.")
        self._endpoint = endpoint
        self._timeout = timeout
        self._proxy = proxy
        self._socket: ClientConnection | None = None
        self._pending: deque[Notification] = deque()
        # websockets DEBUG includes frame payloads, so use a private, silent logger.
        self._wire_logger = logging.Logger(
            "xpushstream.autopush.wire", logging.CRITICAL + 1
        )

    async def open(self, uaid: str | None = None, channel_id: str | None = None) -> str:
        if self._socket is not None:
            raise ProtocolError("AutoPush connection is already open.")
        try:
            self._socket = await connect(
                self._endpoint,
                proxy=self._proxy,
                open_timeout=self._timeout,
                close_timeout=3,
                ping_interval=300,
                ping_timeout=20,
                max_size=MAX_PAYLOAD,
                max_queue=16,
                logger=self._wire_logger,
            )
        except InvalidStatus as error:
            if error.response.status_code in {429, 500, 502, 503, 504}:
                raise AutoPushConnectionError(
                    retry_after=retry_after(error.response.headers.get("Retry-After"))
                ) from None
            raise ProtocolError("AutoPush refused the WebSocket handshake.") from None
        except (OSError, TimeoutError, asyncio.TimeoutError, InvalidHandshake):
            raise AutoPushConnectionError() from None
        except (InvalidURI, InvalidProxy, ValueError):
            raise ProtocolError("Invalid AutoPush connection configuration.") from None
        try:
            response = await self._exchange(hello(uaid, channel_id), "hello")
            if response.get("use_webpush") is not True:
                raise ProtocolError("AutoPush did not enable Web Push data delivery.")
            return str(UUID(response["uaid"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ProtocolError("AutoPush hello returned an invalid UAID.") from None

    async def _send(self, message: dict[str, Any]) -> None:
        if self._socket is None:
            raise AutoPushConnectionError("AutoPush is not connected.")
        try:
            await asyncio.wait_for(
                self._socket.send(json.dumps(message, separators=(",", ":"))),
                self._timeout,
            )
        except (ConnectionClosed, OSError, TimeoutError, asyncio.TimeoutError):
            raise AutoPushConnectionError() from None

    async def _message(self) -> dict[str, Any]:
        if self._socket is None:
            raise AutoPushConnectionError("AutoPush is not connected.")
        try:
            data = await self._socket.recv()
        except (ConnectionClosed, OSError):
            raise AutoPushConnectionError() from None
        try:
            if len(data) > MAX_PAYLOAD:
                raise ValueError
            message = json.loads(data)
            if not isinstance(message, dict):
                raise ValueError
            return message
        except (ValueError, TypeError, RecursionError):
            raise ProtocolError("Invalid AutoPush JSON message.") from None

    async def _wait_reply(
        self, expected: str, channel_id: str | None
    ) -> dict[str, Any]:
        while True:
            message = await self._message()
            kind = message.get("messageType")
            if not message or kind in {"broadcast", "broadcasts"}:
                continue
            if kind == "notification":
                if len(self._pending) >= 256:
                    raise AutoPushConnectionError(
                        "Handshake notification buffer is full."
                    )
                self._pending.append(Notification.parse(message))
                continue
            if kind != expected:
                raise ProtocolError("Unexpected AutoPush response type.")
            if channel_id is not None and message.get("channelID") != channel_id:
                raise ProtocolError(
                    "AutoPush response channel does not match its request."
                )
            status = message.get("status")
            if type(status) is not int:
                raise ProtocolError("AutoPush response is missing its status.")
            if status == 409:
                raise ChannelConflictError("AutoPush channel ID conflict.")
            if status == 429 or status >= 500:
                retry = message.get("retry_after", 0)
                raise AutoPushConnectionError(retry_after=retry_after(str(retry)))
            if status != 200:
                raise ProtocolError("AutoPush request was rejected.")
            return message

    async def _exchange(self, request: dict[str, Any], expected: str) -> dict[str, Any]:
        await self._send(request)
        try:
            return await asyncio.wait_for(
                self._wait_reply(expected, request.get("channelID")), self._timeout
            )
        except (TimeoutError, asyncio.TimeoutError):
            raise AutoPushConnectionError("AutoPush response timed out.") from None

    async def register(self, channel_id: str, application_server_key: str) -> str:
        response = await self._exchange(
            {
                "messageType": "register",
                "channelID": channel_id,
                "key": application_server_key,
            },
            "register",
        )
        endpoint = response.get("pushEndpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise ProtocolError("AutoPush registration returned no endpoint.")
        return endpoint

    async def receive(self) -> Notification:
        if self._pending:
            return self._pending.popleft()
        while True:
            message = await self._message()
            kind = message.get("messageType")
            if not message or kind in {"broadcast", "broadcasts"}:
                continue
            if kind != "notification":
                raise ProtocolError("Unexpected AutoPush stream message.")
            return Notification.parse(message)

    async def ack(self, notification: Notification, outcome: AckOutcome) -> None:
        await self._send(
            {
                "messageType": "ack",
                "updates": [
                    {
                        "channelID": notification.channel_id,
                        "version": notification.version,
                        "code": int(outcome),
                    }
                ],
            }
        )

    async def unregister(self, channel_id: str) -> None:
        await self._exchange(
            {"messageType": "unregister", "channelID": channel_id, "code": 200},
            "unregister",
        )

    async def close(self) -> None:
        socket, self._socket = self._socket, None
        self._pending.clear()
        if socket is not None:
            try:
                await asyncio.wait_for(socket.close(), 4)
            except (ConnectionClosed, OSError, TimeoutError, asyncio.TimeoutError):
                pass
