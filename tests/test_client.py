"""End-to-end synthetic encrypted delivery and adversarial client lifecycles."""

import asyncio
import logging
from dataclasses import replace

import httpx
import pytest

from xpushstream import UnknownNotification, XPushClient
from xpushstream.autopush import AckOutcome, Notification
from xpushstream.exceptions import (
    AuthenticationError,
    AutoPushConnectionError,
    ChannelConflictError,
    PersistentClientCredentialMismatch,
    PersistentClientInUseError,
    ProtocolError,
    RegistrationError,
)
from xpushstream.state import MemoryStateStore
from xpushstream.x_adapter import XPushRegistrar

from .fakes import FakeRegistrar, FakeTransport, make_state, notification, wait_until


@pytest.mark.parametrize("changed_uaid", [False, True])
def test_configured_resume_and_reconnect_apply_settings_per_endpoint(changed_uaid):
    async def run():
        state = replace(make_state(), registered=True)
        store = MemoryStateStore()
        async with store.claim("settings"):
            await store.save("settings", state)
        requests = []

        class Ct0:
            async def acquire(self, auth_token):
                return "synthetic-ct0"

        def respond(request):
            requests.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(
                200, json={"push_settings_template": {"checksum": "fresh-checksum"}}
            )

        first = FakeTransport(uaid=state.uaid)
        second = FakeTransport(uaid=None if changed_uaid else state.uaid)
        transports = iter([first, second])
        client = XPushClient(
            "synthetic-token",
            client_id="settings",
            state_store=store,
            registrar=XPushRegistrar(
                ct0_provider=Ct0(),
                notify_likes=True,
                transport=httpx.MockTransport(respond),
            ),
            transport_factory=lambda: next(transports),
            reconnect_base_delay=0.001,
            reconnect_max_delay=0.002,
        )
        async with client:
            assert requests == ["checkin.json", "save.json", "save.json", "save.json"]
            await first.messages.put(AutoPushConnectionError("synthetic disconnect"))
            await wait_until(lambda: second.opened and client.status.connected)
            assert requests == (
                [
                    "checkin.json",
                    "save.json",
                    "save.json",
                    "save.json",
                    "login.json",
                    "save.json",
                    "save.json",
                    "save.json",
                ]
                if changed_uaid
                else ["checkin.json", "save.json", "save.json", "save.json"]
            )
        assert first.closed and second.closed

    asyncio.run(run())


def test_sdk_setting_arguments_and_failed_save_keep_registration_pending(monkeypatch):
    async def run():
        requests = []

        class Ct0:
            async def acquire(self, auth_token):
                return "synthetic-ct0"

        def respond(request):
            import json

            device = json.loads(request.content)["push_device_info"]
            requests.append(device)
            if request.url.path.endswith("save.json"):
                return httpx.Response(500, json={})
            return httpx.Response(
                200, json={"push_settings_template": {"checksum": "fresh-checksum"}}
            )

        class TestRegistrar(XPushRegistrar):
            def __init__(self, **kwargs):
                super().__init__(transport=httpx.MockTransport(respond), **kwargs)

        monkeypatch.setattr("xpushstream.client.XPushRegistrar", TestRegistrar)
        store, transport = MemoryStateStore(), FakeTransport()
        client = XPushClient(
            "synthetic-token",
            client_id="settings",
            state_store=store,
            ct0_provider=Ct0(),
            transport_factory=lambda: transport,
            notify_likes=True,
            notify_mentions=True,
            notify_reposts=True,
        )
        with pytest.raises(RegistrationError):
            async with client:
                pytest.fail("Settings failure must prevent readiness")
        assert requests[1]["settings"] == {"LikesNonVitSetting": "anyone"}
        assert len(requests) == 2
        assert not client.status.connected and transport.closed
        async with store.claim("settings"):
            pending = await store.load("settings")
            assert pending is not None and not pending.registered

    asyncio.run(run())


def test_setting_arguments_are_not_silently_ignored_with_custom_registrar():
    with pytest.raises(ValueError, match="custom registrar"):
        XPushClient("synthetic-token", registrar=FakeRegistrar(), notify_mentions=False)


@pytest.mark.parametrize("name", ["notify_likes", "notify_mentions", "notify_reposts"])
@pytest.mark.parametrize("value", ["anyone", 1, None])
def test_notification_preferences_require_real_booleans(name, value):
    options = {name: value}
    with pytest.raises(ValueError, match="True or False"):
        XPushClient("synthetic-token", **options)
    with pytest.raises(ValueError, match="True or False"):
        XPushRegistrar(**options)


def test_full_encrypted_event_pipeline_and_ephemeral_cleanup() -> None:
    async def run() -> None:
        transport, registrar = FakeTransport(), FakeRegistrar()
        client = XPushClient(
            "synthetic-token", registrar=registrar, transport_factory=lambda: transport
        )
        async with client:
            state = registrar.registrations[0]
            raw = {
                "title": "synthetic",
                "body": "hello",
                "url": "/name/status/123",
                "extra": {"retained": True},
            }
            message = notification(state, "one", raw)
            await transport.messages.put(message)
            event = await asyncio.wait_for(client.__anext__(), 2)
            assert isinstance(event, UnknownNotification)
            assert event.raw == raw
            assert event.tweet_id == "123"
            assert event.type is None
            assert transport.acks == [(message, AckOutcome.DELIVERED)]
            assert client.status.delivered == 1
        assert transport.unregistered == [state.channel_id]
        assert transport.closed
        assert not client.status.connected
        with pytest.raises(StopAsyncIteration):
            await client.__anext__()

    asyncio.run(run())


def test_persistent_resume_skips_x_registration_and_ct0() -> None:
    async def run() -> None:
        store = MemoryStateStore()
        first, registrar = FakeTransport(), FakeRegistrar()
        async with XPushClient(
            "synthetic-token",
            client_id="bot",
            state_store=store,
            registrar=registrar,
            transport_factory=lambda: first,
        ):
            pass
        state = registrar.registrations[0]
        assert first.unregistered == []
        second, unused = (
            FakeTransport(uaid=state.uaid),
            FakeRegistrar(AuthenticationError("must not be called")),
        )
        async with XPushClient(
            "synthetic-token",
            client_id="bot",
            state_store=store,
            registrar=unused,
            transport_factory=lambda: second,
        ):
            assert second.opened == [(state.uaid, state.channel_id)]
            assert second.channels == []
        assert unused.registrations == []
        async with store.claim("bot"):
            assert (await store.load("bot")).registered

    asyncio.run(run())


def test_changed_uaid_replaces_subscription_keys_and_registers_x_again() -> None:
    async def run() -> None:
        store, registrar = MemoryStateStore(), FakeRegistrar()
        first = FakeTransport()
        async with XPushClient(
            "synthetic-token",
            client_id="bot",
            state_store=store,
            registrar=registrar,
            transport_factory=lambda: first,
        ):
            pass
        old = registrar.registrations[0]
        second = FakeTransport()
        async with XPushClient(
            "synthetic-token",
            client_id="bot",
            state_store=store,
            registrar=registrar,
            transport_factory=lambda: second,
        ):
            new = registrar.registrations[1]
            assert new.uaid != old.uaid
            assert new.channel_id != old.channel_id
            assert new.endpoint != old.endpoint
            assert new.keys != old.keys
            assert new.device_id != old.device_id

    asyncio.run(run())


def test_credential_mismatch_precedes_network_and_preserves_state() -> None:
    async def run() -> None:
        store = MemoryStateStore()
        state = replace(make_state(), registered=True)
        async with store.claim("bot"):
            await store.save("bot", state)
        transport = FakeTransport()
        client = XPushClient(
            "different-synthetic-token",
            client_id="bot",
            state_store=store,
            registrar=FakeRegistrar(),
            transport_factory=lambda: transport,
        )
        with pytest.raises(PersistentClientCredentialMismatch):
            async with client:
                pass
        assert transport.opened == []
        async with store.claim("bot"):
            assert await store.load("bot") == state

    asyncio.run(run())


def test_failed_x_registration_keeps_pending_state_and_releases_claim() -> None:
    async def run() -> None:
        store, failing = (
            MemoryStateStore(),
            FakeRegistrar(AuthenticationError("synthetic failure")),
        )
        transport = FakeTransport()
        with pytest.raises(AuthenticationError):
            async with XPushClient(
                "synthetic-token",
                client_id="bot",
                state_store=store,
                registrar=failing,
                transport_factory=lambda: transport,
            ):
                pass
        async with store.claim("bot"):
            pending = await store.load("bot")
            assert pending is not None and not pending.registered
        resumed, good = FakeTransport(uaid=pending.uaid), FakeRegistrar()
        async with XPushClient(
            "synthetic-token",
            client_id="bot",
            state_store=store,
            registrar=good,
            transport_factory=lambda: resumed,
        ):
            assert good.registrations[0].endpoint == pending.endpoint
            assert resumed.channels == []
        assert transport.closed

    asyncio.run(run())


def test_same_client_id_conflicts_but_different_clients_run() -> None:
    async def run() -> None:
        store = MemoryStateStore()

        def new(name: str) -> XPushClient:
            return XPushClient(
                "synthetic-token",
                client_id=name,
                state_store=store,
                registrar=FakeRegistrar(),
                transport_factory=FakeTransport,
            )

        async with new("one"):
            with pytest.raises(PersistentClientInUseError):
                async with new("one"):
                    pass
            async with new("two"):
                pass

    asyncio.run(run())


def test_reconnect_resumes_and_duplicate_does_not_emit_twice() -> None:
    async def run() -> None:
        first, registrar = FakeTransport(), FakeRegistrar()
        second = FakeTransport(uaid=first.uaid)
        transports = iter([first, second])
        async with XPushClient(
            "synthetic-token",
            registrar=registrar,
            transport_factory=lambda: next(transports),
            reconnect_base_delay=0.001,
            reconnect_max_delay=0.001,
        ) as client:
            state = registrar.registrations[0]
            message = notification(state, "one", {"body": "first"})
            await first.messages.put(message)
            assert (await client.__anext__()).body == "first"
            await first.messages.put(AutoPushConnectionError())
            await wait_until(lambda: bool(second.opened))
            await second.messages.put(message)
            await second.messages.put(notification(state, "two", {"body": "second"}))
            assert (await asyncio.wait_for(client.__anext__(), 2)).body == "second"
            assert client.status.delivered == 2
            assert client.status.reconnects == 1
            assert len(registrar.registrations) == 1
            assert second.opened == [(state.uaid, state.channel_id)]
            assert len(second.acks) == 2
        assert first.closed and second.closed

    asyncio.run(run())


def test_queue_backpressure_does_not_ack_an_unqueued_event() -> None:
    async def run() -> None:
        transport, registrar = FakeTransport(), FakeRegistrar()
        async with XPushClient(
            "synthetic-token",
            registrar=registrar,
            transport_factory=lambda: transport,
            queue_size=1,
        ) as client:
            state = registrar.registrations[0]
            for version in ("one", "two"):
                await transport.messages.put(
                    notification(state, version, {"body": version})
                )
            await wait_until(lambda: transport.received == 2)
            assert len(transport.acks) == 1
            assert (await client.__anext__()).body == "one"
            await wait_until(lambda: len(transport.acks) == 2)
            assert (await client.__anext__()).body == "two"

    asyncio.run(run())


def test_failed_notification_ack_codes_and_safe_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        transport, registrar = FakeTransport(), FakeRegistrar()
        with caplog.at_level(logging.DEBUG):
            async with XPushClient(
                "synthetic-token",
                registrar=registrar,
                transport_factory=lambda: transport,
                normalizer=lambda data: (_ for _ in ()).throw(
                    ValueError("synthetic-secret-body")
                ),
            ) as client:
                state = registrar.registrations[0]
                await transport.messages.put(
                    Notification(
                        state.channel_id,
                        "bad",
                        "bad-secret-data",
                        {"encoding": "aes128gcm"},
                    )
                )
                await transport.messages.put(
                    notification(state, "json", {"private": "synthetic-secret-body"})
                )
                await wait_until(lambda: len(transport.acks) == 2)
                assert [code for _, code in transport.acks] == [
                    AckOutcome.DECRYPTION_FAILURE,
                    AckOutcome.PROCESSING_FAILURE,
                ]
                assert (
                    client.status.decryption_failures
                    == client.status.processing_failures
                    == 1
                )
                assert client.status.delivered == 0
                for secret in (
                    "synthetic-token",
                    "synthetic-secret-body",
                    "bad-secret-data",
                    state.endpoint,
                    state.keys.private_key.hex(),
                ):
                    assert secret not in caplog.text + repr(client)

    asyncio.run(run())


def test_fatal_transport_error_wakes_iterator_and_closes_socket() -> None:
    async def run() -> None:
        transport = FakeTransport()
        async with XPushClient(
            "synthetic-token",
            registrar=FakeRegistrar(),
            transport_factory=lambda: transport,
        ) as client:
            await transport.messages.put(ProtocolError("synthetic protocol failure"))
            with pytest.raises(ProtocolError):
                await asyncio.wait_for(client.__anext__(), 2)
            assert transport.closed

    asyncio.run(run())


def test_close_wakes_waiting_consumer_and_releases_all_tasks() -> None:
    async def run() -> None:
        client = XPushClient(
            "synthetic-token",
            registrar=FakeRegistrar(),
            transport_factory=FakeTransport,
        )
        async with client:
            reading = asyncio.create_task(client.__anext__())
            await wait_until(lambda: client._reading)
            await client.aclose()
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(reading, 2)
            assert not [
                task
                for task in asyncio.all_tasks()
                if task.get_name().startswith("xpushstream-")
            ]

    asyncio.run(run())


def test_cancel_startup_releases_persistent_claim() -> None:
    class WaitingTransport(FakeTransport):
        async def open(self, uaid: str | None, channel_id: str | None) -> str:
            self.opened.append((uaid, channel_id))
            await asyncio.Event().wait()
            return self.uaid

    async def run() -> None:
        store, transport = MemoryStateStore(), WaitingTransport()
        client = XPushClient(
            "synthetic-token",
            client_id="bot",
            state_store=store,
            registrar=FakeRegistrar(),
            transport_factory=lambda: transport,
        )
        starting = asyncio.create_task(client.__aenter__())
        await wait_until(lambda: bool(transport.opened))
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert transport.closed
        async with store.claim("bot"):
            pass

    asyncio.run(run())


def test_single_consumer_and_context_required() -> None:
    async def run() -> None:
        client = XPushClient(
            "synthetic-token",
            registrar=FakeRegistrar(),
            transport_factory=FakeTransport,
        )
        with pytest.raises(RuntimeError, match="context"):
            await client.__anext__()
        async with client:
            first = asyncio.create_task(client.__anext__())
            await wait_until(lambda: client._reading)
            with pytest.raises(RuntimeError, match="one consumer"):
                await client.__anext__()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

    asyncio.run(run())


def test_startup_connection_retries_obey_server_delay_and_budget(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    class FailingTransport(FakeTransport):
        async def open(self, uaid, channel_id):
            raise AutoPushConnectionError(retry_after=7)

    transports = []

    def factory():
        transport = FailingTransport()
        transports.append(transport)
        return transport

    monkeypatch.setattr("xpushstream.client.asyncio.sleep", sleep)

    async def run():
        with pytest.raises(AutoPushConnectionError, match="exhausted"):
            async with XPushClient(
                "synthetic-token",
                registrar=FakeRegistrar(),
                transport_factory=factory,
                max_reconnect_attempts=2,
            ):
                pass
        assert delays == [7, 7]
        assert len(transports) == 3 and all(t.closed for t in transports)

    asyncio.run(run())


def test_register_channel_conflict_uses_another_uuid():
    class ConflictingTransport(FakeTransport):
        async def register(self, channel_id, application_server_key):
            endpoint = await super().register(channel_id, application_server_key)
            if len(self.channels) == 1:
                raise ChannelConflictError("synthetic conflict")
            return endpoint

    async def run():
        transport = ConflictingTransport()
        async with XPushClient(
            "synthetic-token",
            registrar=FakeRegistrar(),
            transport_factory=lambda: transport,
        ):
            assert len(set(transport.channels)) == 2

    asyncio.run(run())


def test_cancelled_read_can_be_replaced_and_concurrent_close_is_idempotent():
    async def run():
        transport, registrar = FakeTransport(), FakeRegistrar()
        async with XPushClient(
            "synthetic-token", registrar=registrar, transport_factory=lambda: transport
        ) as client:
            reader = asyncio.create_task(client.__anext__())
            await wait_until(lambda: client._reading)
            reader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reader
            message = notification(
                registrar.registrations[0], "one", {"body": "retained"}
            )
            await transport.messages.put(message)
            assert (await client.__anext__()).body == "retained"
            await asyncio.gather(client.aclose(), client.aclose(), client.aclose())
        assert len(transport.unregistered) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"queue_size": 0},
        {"queue_size": 0.5},
        {"reconnect_max_delay": float("inf")},
        {"reconnect_base_delay": float("nan")},
        {"max_reconnect_attempts": -1},
        {"state_store": MemoryStateStore()},
        {"enrich_tweets": True},
    ],
)
def test_invalid_client_configuration_fails_before_network(kwargs):
    with pytest.raises(ValueError):
        XPushClient("synthetic-token", **kwargs)


def test_invalid_endpoint_from_server_does_not_leak_into_errors():
    class InvalidTransport(FakeTransport):
        async def register(self, channel_id, application_server_key):
            return "https://push.invalid:synthetic-secret-endpoint/"

    async def run():
        transport = InvalidTransport()
        with pytest.raises(ProtocolError) as caught:
            async with XPushClient(
                "synthetic-token",
                registrar=FakeRegistrar(),
                transport_factory=lambda: transport,
            ):
                pass
        assert "synthetic-secret-endpoint" not in str(caught.value)
        assert caught.value.__suppress_context__
        assert transport.closed

    asyncio.run(run())
