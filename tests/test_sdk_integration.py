"""Real SDK + WebSocket + HTTP adapters, with only external services simulated."""

import asyncio
import json
from uuid import uuid4

import httpx
from websockets.asyncio.server import ServerConnection, serve

from xpushstream import XPushClient
from xpushstream.auth import ReleaseNotesCt0Provider
from xpushstream.autopush import AutoPushTransport
from xpushstream.x_adapter import XPushRegistrar

from .fakes import notification


def test_sdk_runs_standard_auth_registration_crypto_and_ack_together():
    async def run():
        registered = asyncio.Event()
        acknowledged = asyncio.Event()
        requests, wire, states = [], [], []
        uaid = str(uuid4())

        class Registrar(XPushRegistrar):
            async def register(self, state, auth_token):
                states.append(state)
                await super().register(state, auth_token)
                registered.set()

        def http(request):
            requests.append(request)
            if request.method == "GET":
                assert request.url.path == "/i/release_notes"
                return httpx.Response(
                    200,
                    headers={
                        "Set-Cookie": "ct0=synthetic-ct0; Secure; Path=/; Domain=x.com"
                    },
                )
            assert request.url.path in {
                "/i/api/1.1/notifications/settings/login.json",
                "/i/api/1.1/notifications/settings/save.json",
            }
            assert "auth_token=synthetic-token" in request.headers["cookie"]
            assert "ct0=synthetic-ct0" in request.headers["cookie"]
            return httpx.Response(
                200, json={"push_settings_template": {"checksum": "fresh-checksum"}}
            )

        async def handler(socket: ServerConnection):
            wire.append(json.loads(await socket.recv()))
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
            registration = json.loads(await socket.recv())
            wire.append(registration)
            channel = registration["channelID"]
            await socket.send(
                json.dumps(
                    {
                        "messageType": "register",
                        "status": 200,
                        "channelID": channel,
                        "pushEndpoint": "https://push.invalid/synthetic",
                    }
                )
            )
            await registered.wait()
            message = notification(
                states[0],
                "one",
                {
                    "title": "sample",
                    "body": "synthetic delivery",
                    "url": "/name/status/123",
                    "data": {"type": "tweet"},
                },
            )
            await socket.send(
                json.dumps(
                    {
                        "messageType": "notification",
                        "channelID": channel,
                        "version": message.version,
                        "data": message.data,
                        "headers": message.headers,
                    }
                )
            )
            wire.append(json.loads(await socket.recv()))
            acknowledged.set()
            wire.append(json.loads(await socket.recv()))
            await socket.send(
                json.dumps(
                    {"messageType": "unregister", "status": 200, "channelID": channel}
                )
            )

        registrar = Registrar(
            ct0_provider=ReleaseNotesCt0Provider(transport=httpx.MockTransport(http)),
            transport=httpx.MockTransport(http),
        )
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with XPushClient(
                "synthetic-token",
                registrar=registrar,
                transport_factory=lambda: AutoPushTransport(
                    endpoint=f"ws://127.0.0.1:{port}"
                ),
            ) as client:
                stream = client.stream().event_types(["tweet"]).__aiter__()
                event = await asyncio.wait_for(stream.__anext__(), 2)
                await stream.aclose()
                await asyncio.wait_for(acknowledged.wait(), 2)
                assert event.type == "tweet" and event.tweet_id == "123"
                assert event.body == "synthetic delivery"
                assert client.status.connected
        assert [request.method for request in requests] == ["GET"] + ["POST"] * 4
        assert [
            json.loads(request.content)["push_device_info"]["settings"]
            for request in requests[2:]
        ] == [
            {"LikesNonVitSetting": "anyone"},
            {"MentionsSetting": "anyone"},
            {"RetweetsSetting": "anyone"},
        ]
        assert [message["messageType"] for message in wire] == [
            "hello",
            "register",
            "ack",
            "unregister",
        ]
        assert wire[2]["updates"][0]["code"] == 100

    asyncio.run(run())
