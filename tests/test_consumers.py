"""Filters, webhook delivery and CLI operate on the public event stream."""

import argparse
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from xpushstream import TweetDetails, UnknownNotification, cli
from xpushstream._http import private_http, retry_after
from xpushstream.exceptions import WebhookDeliveryError
from xpushstream.filters import EventFilter, EventStream
from xpushstream.state import MemoryStateStore
from xpushstream.webhook import Route, WebhookDestination, WebhookRouter
from xpushstream.x_adapter import normalize

from .fakes import make_state


async def source(events):
    for event in events:
        yield event


def test_filter_composition_is_lazy_and_preserves_original_events() -> None:
    events = [
        UnknownNotification(raw={}, body="障害 RECOVERED", tweet_id="1"),
        UnknownNotification(
            raw={"unchanged": True},
            body="障害 active",
            tweet_id="2",
            type="favorite",
            tweet=TweetDetails("2", author_id="123", username="Alice"),
        ),
        UnknownNotification(raw={}, body="elsewhere", tweet_id="3"),
    ]
    selected = EventFilter.contains("障害") & ~EventFilter.contains("RECOVERED")
    assert selected(events[1]) and not selected(events[0])
    assert (selected | EventFilter.contains("elsewhere"))(events[2])

    async def run() -> None:
        stream = (
            EventStream(source(events))
            .users(["123"])
            .usernames(["@aLiCe"])
            .event_types(["favorite"])
            .contains("障害")
            .exclude("RECOVERED")
            .regex(r"ACTIVE", flags=2)
        )
        assert [event async for event in stream] == [events[1]]
        assert [
            event async for event in EventStream(source(events)).where(selected)
        ] == [events[1]]

    asyncio.run(run())


@pytest.mark.parametrize(
    "selected", ["favorite", "future-type", "unknown", "", "Favorite"]
)
def test_event_types_matches_x_type_exactly(selected: str) -> None:
    events = [
        normalize(json.dumps({"data": {"type": value}}).encode())
        for value in ("favorite", "future-type", "unknown", "", None, 123)
    ] + [normalize(b"{}")]
    expected = [event for event in events if event.type == selected]
    condition = EventFilter.event_types(types=iter([selected]))
    assert [event for event in events if condition(event)] == expected

    async def run() -> None:
        stream = EventStream(source(events)).event_types(types=iter([selected]))
        assert [event async for event in stream] == expected
        assert [
            event async for event in EventStream(source(events)).event_types([])
        ] == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "method,flag",
    [
        ("exclude_replies", "is_reply"),
        ("exclude_quotes", "is_quote"),
        ("exclude_reposts", "is_repost"),
    ],
)
def test_exclusion_retains_unknown_flags(method, flag) -> None:
    events = [UnknownNotification(raw={})] + [
        UnknownNotification(raw={}, tweet=TweetDetails("1", **{flag: value}))
        for value in (None, False, True)
    ]

    async def run() -> None:
        stream = getattr(EventStream(source(events)), method)()
        assert [event async for event in stream] == events[:3]

    asyncio.run(run())


def test_media_and_enriched_text_filters() -> None:
    events = [
        UnknownNotification(raw={}),
        UnknownNotification(
            raw={},
            tweet=TweetDetails("1", text="full text", media=({"type": "photo"},)),
        ),
    ]

    async def run() -> None:
        stream = EventStream(source(events)).has_media().contains("full text")
        assert [event async for event in stream] == [events[1]]

    asyncio.run(run())


def test_observed_types_filter_without_inventing_post_details() -> None:
    fixtures = Path(__file__).parent / "fixtures"
    samples = {
        name: normalize((fixtures / f"x_notification_{name}.json").read_bytes())
        for name in (
            "tweet",
            "quote",
            "image",
            "reply",
            "mention",
            "favorite",
            "retweet",
            "follower",
            "follow_request_accepted",
            "mr_infrequent_poster",
        )
    }
    unknown = normalize(b'{"data":{"type":"future-type"}}')
    missing = normalize(b"{}")
    events = [*samples.values(), unknown, missing]

    async def run() -> None:
        assert [
            event async for event in EventStream(source(events)).event_types(["tweet"])
        ] == [samples[name] for name in ("tweet", "quote", "image")]
        assert [
            event
            async for event in EventStream(source(events)).event_types(
                ["reply", "mention", "favorite", "retweet"]
            )
        ] == [samples[name] for name in ("reply", "mention", "favorite", "retweet")]
        for method, excluded in (
            ("exclude_replies", "reply"),
            ("exclude_reposts", "retweet"),
        ):
            assert [
                event async for event in getattr(EventStream(source(events)), method)()
            ] == [event for event in events if event.type != excluded]
        assert [
            event async for event in EventStream(source(events)).exclude_quotes()
        ] == events
        assert [event async for event in EventStream(source(events)).has_media()] == []
        assert [
            event
            async for event in EventStream(source(events)).event_types(["future-type"])
        ] == [unknown]

    asyncio.run(run())
    assert all(event.tweet is None for event in events)
    assert samples["retweet"].raw["data"]["scribe_target"] == "favorite"


def test_type_filter_skips_later_predicates_for_unselected_notifications() -> None:
    events = [
        UnknownNotification(raw={}, type=value)
        for value in ("tweet", "mention", "retweet")
    ]
    inspected = []

    def inspect(event):
        inspected.append(event)
        return True

    async def run() -> None:
        selected = EventStream(source(events)).event_types(["mention"]).where(inspect)
        assert [event async for event in selected] == [events[1]]

    asyncio.run(run())
    assert inspected == [events[1]]


@pytest.mark.parametrize("value", [None, "oops", "nan", "inf", "-5"])
def test_retry_after_invalid_values(value) -> None:
    assert retry_after(value) == 0


def test_retry_after_seconds_and_http_date() -> None:
    assert retry_after("17") == 17
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert 58 <= retry_after(format_datetime(future, usegmt=True)) <= 60


def test_webhook_retries_and_sends_normalized_payload(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts, sleeps = [], []
    event = UnknownNotification(
        raw={"private": "synthetic-private-payload"}, body="body", type="favorite"
    )

    async def sleep(delay):
        sleeps.append(delay)

    def respond(request):
        attempts.append(request)
        assert "raw" not in json.loads(request.content)
        assert "kind" not in json.loads(request.content)
        assert json.loads(request.content)["type"] == "favorite"
        logging.getLogger("httpx").info("private synthetic-webhook-token")
        return (
            httpx.Response(429, headers={"Retry-After": "7"})
            if len(attempts) == 1
            else httpx.Response(204)
        )

    monkeypatch.setattr("xpushstream.webhook.asyncio.sleep", sleep)
    destination = WebhookDestination(
        "https://example.invalid/synthetic-webhook-token",
        transport=httpx.MockTransport(respond),
    )
    with caplog.at_level(logging.DEBUG):
        asyncio.run(destination.deliver(event))
    assert len(attempts) == 2 and sleeps == [7]
    assert "synthetic-webhook-token" not in caplog.text
    assert "synthetic-private-payload" not in caplog.text
    assert not private_http.get()


@pytest.mark.parametrize(
    "status,expected_calls", [(400, 1), (302, 1), (503, 3), (501, 3)]
)
def test_webhook_permanent_errors_and_retry_exhaustion(
    monkeypatch, status, expected_calls
):
    calls = []

    async def sleep(delay):
        pass

    def respond(request):
        calls.append(request)
        return httpx.Response(status, text="synthetic-private-error")

    monkeypatch.setattr("xpushstream.webhook.asyncio.sleep", sleep)
    destination = WebhookDestination(
        "https://example.invalid/private",
        retries=2,
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(WebhookDeliveryError) as caught:
        asyncio.run(destination.deliver(UnknownNotification(raw={})))
    assert len(calls) == expected_calls
    assert "synthetic-private-error" not in str(caught.value)
    assert not private_http.get()


def test_webhook_transport_errors_retry_and_raw_is_explicit(monkeypatch):
    calls, sleeps = [], []

    async def sleep(delay):
        sleeps.append(delay)

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("synthetic-secret")
        assert json.loads(request.content)["raw"] == {"retained": True}
        return httpx.Response(200)

    monkeypatch.setattr("xpushstream.webhook.asyncio.sleep", sleep)
    destination = WebhookDestination(
        "https://example.invalid/private",
        include_raw=True,
        transport=httpx.MockTransport(respond),
    )
    asyncio.run(destination.deliver(UnknownNotification(raw={"retained": True})))
    assert len(calls) == 2 and sleeps == [1]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid",
        "https://secret@example.invalid",
        "https://example.invalid/#secret",
        "https://[secret",
        "https://example.invalid:secret",
        "/relative",
    ],
)
def test_webhook_invalid_url_is_rejected_safely(url):
    with pytest.raises(ValueError) as caught:
        WebhookDestination(url)
    assert "secret" not in str(caught.value)


def test_router_attempts_every_matching_route_and_propagates_failure():
    async def run():
        calls = []

        class Destination:
            def __init__(self, label, failure=None):
                self.label, self.failure = label, failure

            async def deliver(self, event):
                calls.append(self.label)
                if self.failure:
                    raise self.failure

        routes = [
            Route(
                lambda event: True, Destination("fails", ValueError("synthetic-secret"))
            ),
            Route(lambda event: True, Destination("succeeds")),
            Route(lambda event: False, Destination("unmatched")),
        ]
        with pytest.raises(WebhookDeliveryError, match="routes failed"):
            await WebhookRouter(routes).dispatch(UnknownNotification(raw={}))
        assert calls == ["fails", "succeeds"]
        routes[0] = Route(
            lambda event: True, Destination("cancel", asyncio.CancelledError())
        )
        with pytest.raises(asyncio.CancelledError):
            await WebhookRouter(routes).run(source([UnknownNotification(raw={})]))

    asyncio.run(run())


def test_cancel_webhook_during_retry_closes_http_transport():
    async def run():
        received = asyncio.Event()

        class Transport(httpx.AsyncBaseTransport):
            closed = False

            async def handle_async_request(self, request):
                received.set()
                return httpx.Response(429, headers={"Retry-After": "3600"})

            async def aclose(self):
                self.closed = True

        transport = Transport()
        task = asyncio.create_task(
            WebhookDestination(
                "https://example.invalid/private", transport=transport
            ).deliver(UnknownNotification(raw={}))
        )
        await received.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.closed
        assert not private_http.get()

    asyncio.run(run())


def test_cli_status_and_reset_do_not_require_credentials(monkeypatch, capsys):
    store = MemoryStateStore()
    monkeypatch.setattr(cli, "FileStateStore", lambda: store)
    monkeypatch.delenv("XPUSHSTREAM_AUTH_TOKEN", raising=False)

    async def run():
        async with store.claim("bot"):
            await store.save("bot", make_state())
        await cli._run(argparse.Namespace(command="status", client_id="bot"))
        await cli._run(argparse.Namespace(command="reset", client_id="bot"))
        async with store.claim("bot"):
            assert await store.load("bot") is None

    asyncio.run(run())
    assert capsys.readouterr().out.splitlines() == [
        '{"exists": true, "registered": false}',
        "Persistent state reset.",
    ]


def test_cli_missing_credentials_and_invalid_webhook_precede_network(monkeypatch):
    monkeypatch.delenv("XPUSHSTREAM_AUTH_TOKEN", raising=False)
    args = cli._parser().parse_args(["listen"])
    with pytest.raises(ValueError, match="XPUSHSTREAM_AUTH_TOKEN"):
        asyncio.run(cli._run(args))
    monkeypatch.setenv("XPUSHSTREAM_AUTH_TOKEN", "synthetic-token")
    monkeypatch.setattr(
        cli, "XPushClient", lambda **kwargs: pytest.fail("network started")
    )
    args = cli._parser().parse_args(["webhook", "--url", "http://example.invalid"])
    with pytest.raises(ValueError, match="HTTPS"):
        asyncio.run(cli._run(args))


def test_cli_listen_uses_sdk_filter_and_omits_raw(monkeypatch, capsys):
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"auth_token": "synthetic-token", "client_id": "bot"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def stream(self):
            return EventStream(
                source(
                    [
                        UnknownNotification(
                            raw={"private": True}, body="select", type="favorite"
                        ),
                        UnknownNotification(raw={}, body="drop"),
                        UnknownNotification(raw={}, body="select", type="tweet"),
                    ]
                )
            )

    monkeypatch.setattr(cli, "XPushClient", Client)
    monkeypatch.setenv("XPUSHSTREAM_AUTH_TOKEN", "synthetic-token")
    args = cli._parser().parse_args(
        [
            "listen",
            "--client-id",
            "bot",
            "--event-type",
            "favorite",
            "--event-type",
            "mention",
            "--contains",
            "select",
        ]
    )
    asyncio.run(cli._run(args))
    result = json.loads(capsys.readouterr().out)
    assert result["body"] == "select" and "raw" not in result
    assert result["type"] == "favorite" and "kind" not in result


def test_cli_error_exit_is_concise(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["xpushstream", "listen"])
    monkeypatch.delenv("XPUSHSTREAM_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    assert "XPUSHSTREAM_AUTH_TOKEN" in capsys.readouterr().err
