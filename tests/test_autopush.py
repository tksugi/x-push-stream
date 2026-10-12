"""Exercise the real transport against a local protocol server."""

import asyncio
import json
from uuid import uuid4

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from xpushstream.autopush import AckOutcome, AutoPushTransport, Notification, hello
from xpushstream.exceptions import (
    AutoPushConnectionError,
    ChannelConflictError,
    ProtocolError,
)


def test_notification_rejects_ambiguous_encryption_headers() -> None:
    with pytest.raises(ProtocolError, match="Invalid AutoPush notification"):
        Notification.parse(
            {
                "channelID": str(uuid4()),
                "version": "synthetic",
                "headers": {"crypto_key": "private-one", "Crypto-Key": "private-two"},
            }
        )


def test_hello_register_buffered_notification_ack_and_unregister() -> None:
    async def run() -> None:
        uaid, channel = str(uuid4()), str(uuid4())
        received: list[dict[str, object]] = []

        async def handler(socket: ServerConnection) -> None:
            received.append(json.loads(await socket.recv()))
            await socket.send(
                json.dumps(
                    {
                        "messageType": "hello",
                        "status": 200,
                        "uaid": uaid,
                        "use_webpush": True,
                    }
                )
            )
            received.append(json.loads(await socket.recv()))
            await socket.send("{}")
            await socket.send(
                json.dumps(
                    {
                        "messageType": "notification",
                        "channelID": channel,
                        "version": "opaque-version",
                        "data": "synthetic-data",
                        "headers": {"encoding": "aes128gcm"},
                    }
                )
            )
            await socket.send(
                json.dumps(
                    {
                        "messageType": "register",
                        "channelID": channel,
                        "status": 200,
                        "pushEndpoint": "https://push.invalid/opaque",
                    }
                )
            )
            received.append(json.loads(await socket.recv()))
            received.append(json.loads(await socket.recv()))
            await socket.send(
                json.dumps(
                    {"messageType": "unregister", "status": 200, "channelID": channel}
                )
            )

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            transport = AutoPushTransport(endpoint=f"ws://127.0.0.1:{port}", timeout=1)
            try:
                assert await transport.open(None, None) == uaid
                assert (
                    await transport.register(channel, "synthetic-application-key")
                    == "https://push.invalid/opaque"
                )
                message = await transport.receive()
                assert message.version == "opaque-version"
                await transport.ack(message, AckOutcome.DECRYPTION_FAILURE)
                await transport.unregister(channel)
            finally:
                await transport.close()
        assert received == [
            hello(None, None),
            {
                "messageType": "register",
                "channelID": channel,
                "key": "synthetic-application-key",
            },
            {
                "messageType": "ack",
                "updates": [
                    {"channelID": channel, "version": "opaque-version", "code": 101}
                ],
            },
            {"messageType": "unregister", "channelID": channel, "code": 200},
        ]

    asyncio.run(run())


@pytest.mark.parametrize(
    "reply",
    [
        "not-json",
        "[]",
        json.dumps(
            {
                "messageType": "hello",
                "status": 200,
                "uaid": "invalid",
                "use_webpush": True,
            }
        ),
        json.dumps(
            {
                "messageType": "hello",
                "status": 200,
                "uaid": str(uuid4()),
                "use_webpush": False,
            }
        ),
        json.dumps(
            {
                "messageType": "hello",
                "status": "200",
                "uaid": str(uuid4()),
                "use_webpush": True,
            }
        ),
    ],
)
def test_invalid_hello_is_fatal_and_does_not_repeat_on_one_socket(reply: str) -> None:
    async def run() -> None:
        requests = 0

        async def handler(socket: ServerConnection) -> None:
            nonlocal requests
            await socket.recv()
            requests += 1
            await socket.send(reply)
            try:
                await socket.recv()
                requests += 1
            except ConnectionClosed:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            transport = AutoPushTransport(endpoint=f"ws://127.0.0.1:{port}", timeout=1)
            try:
                with pytest.raises(ProtocolError):
                    await transport.open(None, None)
            finally:
                await transport.close()
        assert requests == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,error", [(409, ChannelConflictError), (503, AutoPushConnectionError)]
)
def test_register_conflict_and_server_backoff(
    status: int, error: type[Exception]
) -> None:
    async def run() -> None:
        async def handler(socket: ServerConnection) -> None:
            await socket.recv()
            await socket.send(
                json.dumps(
                    {
                        "messageType": "hello",
                        "status": 200,
                        "uaid": str(uuid4()),
                        "use_webpush": True,
                    }
                )
            )
            request = json.loads(await socket.recv())
            await socket.send(
                json.dumps(
                    {
                        "messageType": "register",
                        "status": status,
                        "channelID": request["channelID"],
                        "retry_after": 7,
                    }
                )
            )

        async with serve(handler, "127.0.0.1", 0) as server:
            transport = AutoPushTransport(
                endpoint=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                timeout=1,
            )
            try:
                await transport.open(None, None)
                with pytest.raises(error) as caught:
                    await transport.register(str(uuid4()), "synthetic")
                if isinstance(caught.value, AutoPushConnectionError):
                    assert caught.value.retry_after == 7
            finally:
                await transport.close()

    asyncio.run(run())


def test_handshake_timeout_closes_cleanly() -> None:
    async def run() -> None:
        async def handler(socket: ServerConnection) -> None:
            await socket.recv()
            try:
                await socket.recv()
            except ConnectionClosed:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            transport = AutoPushTransport(
                endpoint=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                timeout=0.05,
            )
            try:
                with pytest.raises(AutoPushConnectionError):
                    await transport.open(None, None)
            finally:
                await transport.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"channelID": "invalid", "version": "one"},
        {"channelID": str(uuid4()), "version": 1},
        {"channelID": str(uuid4()), "version": "one", "headers": {"encoding": 1}},
    ],
)
def test_notification_parser_rejects_malformed_boundaries(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ProtocolError):
        Notification.parse(payload)
