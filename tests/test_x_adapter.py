"""Validate observed X request shape and preserve unobserved notification kinds."""

import asyncio
import json
import logging
import traceback
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from xpushstream import TweetDetails, UnknownNotification
from xpushstream._http import private_http
from xpushstream.events import enrich
from xpushstream.exceptions import (
    AuthenticationError,
    NormalizationError,
    RegistrationError,
)
from xpushstream.filters import EventFilter
from xpushstream.webpush import encode64
from xpushstream.x_adapter import XPushRegistrar, normalize

from .fakes import FakeTransactionGenerator, make_state


def test_transaction_id_is_generated_for_each_get_and_post(monkeypatch):
    from xpushstream.auth import ReleaseNotesCt0Provider

    generator = FakeTransactionGenerator()
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            assert request.url.path == "/i/release_notes"
            return httpx.Response(
                200, headers={"set-cookie": "ct0=synthetic-ct0; Path=/"}
            )
        return httpx.Response(
            200, json={"push_settings_template": {"checksum": "fresh-checksum"}}
        )

    class Provider(ReleaseNotesCt0Provider):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("xpushstream.x_adapter.ReleaseNotesCt0Provider", Provider)
    registrar = XPushRegistrar(
        transaction_generator=generator, transport=httpx.MockTransport(respond)
    )

    async def run():
        await registrar.register(make_state(), "synthetic-token")
        await registrar.apply_settings(make_state(), "synthetic-token")
        await registrar.save_settings(make_state(), "synthetic-token", checksum="known")

    asyncio.run(run())
    assert len(requests) == 12
    assert generator.calls == [(r.method, r.url.path) for r in requests]
    ids = [r.headers["x-client-transaction-id"] for r in requests]
    assert ids == [f"synthetic-transaction-{i}" for i in range(1, 13)]
    assert len(set(ids)) == len(ids)
    assert [r.url.path.rsplit("/", 1)[-1] for r in requests] == [
        "release_notes",
        "login.json",
        "save.json",
        "save.json",
        "save.json",
        "release_notes",
        "checkin.json",
        "save.json",
        "save.json",
        "save.json",
        "release_notes",
        "save.json",
    ]


def test_public_transaction_inputs_are_loaded_once_without_credentials(monkeypatch):
    calls = []
    generator = FakeTransactionGenerator()

    async def fetch(client):
        assert not list(client.cookies.jar)
        assert "authorization" not in client.headers
        assert "x-csrf-token" not in client.headers
        assert "x-client-transaction-id" not in client.headers
        calls.append(client)
        await asyncio.sleep(0)
        return generator

    monkeypatch.setattr("xpushstream.x_adapter.TransactionIdGenerator.fetch", fetch)
    registrar = XPushRegistrar(ct0_provider=FakeCt0())

    async def run():
        values = await asyncio.gather(
            *(registrar._get_transaction_generator() for _ in range(3))
        )
        assert all(value is generator for value in values)

    asyncio.run(run())
    assert len(calls) == 1 and calls[0].is_closed


def test_transaction_source_failure_is_private_and_sends_no_session_request(
    monkeypatch,
):
    from x_client_transaction import TransactionSourceError

    async def fetch(client):
        raise TransactionSourceError("synthetic-private-source")

    monkeypatch.setattr("xpushstream.x_adapter.TransactionIdGenerator.fetch", fetch)
    requests = []
    registrar = XPushRegistrar(
        ct0_provider=FakeCt0(),
        transport=httpx.MockTransport(lambda request: requests.append(request)),
    )
    with pytest.raises(RegistrationError) as caught:
        asyncio.run(registrar.register(make_state(), "synthetic-token"))
    assert requests == []
    assert "synthetic-private-source" not in "".join(
        traceback.format_exception(caught.value)
    )


class FakeCt0:
    async def acquire(self, auth_token: str) -> str:
        assert auth_token == "synthetic-token"
        return "synthetic-ct0"


@pytest.mark.parametrize("status", [200, 403])
def test_public_source_wire_cookies_are_redacted(monkeypatch, caplog, status):
    from x_client_transaction import TransactionIdGenerator, _sources

    from .test_client_transaction import SCRIPT, shell

    monkeypatch.setattr(
        "xpushstream.x_adapter.TransactionIdGenerator", TransactionIdGenerator
    )

    async def run():
        async def respond(reader, writer):
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                assert b"cookie:" not in request.lower()
                body = (
                    shell() if request.startswith(b"GET /shell ") else SCRIPT
                ).encode()
                writer.write(
                    f"HTTP/1.1 {status} Test\r\n".encode()
                    + b"Set-Cookie: session=synthetic-source-cookie; Secure\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode()
                    + b"Connection: close\r\n\r\n"
                    + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
            root = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
            monkeypatch.setattr(_sources, "SHELL_URL", root + "shell")
            monkeypatch.setattr(_sources, "ASSET_ROOT", root)
            registrar = XPushRegistrar()
            if status == 200:
                await registrar._get_transaction_generator()
            else:
                with pytest.raises(RegistrationError):
                    await registrar._get_transaction_generator()
            assert not private_http.get()
            logging.getLogger("httpcore.http11").debug("outside source fetch")

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert "synthetic-source-cookie" not in caplog.text
    assert "details withheld" in caplog.text
    assert "outside source fetch" in caplog.text


@pytest.mark.parametrize("source", [False, True])
@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "gzip, identity"])
def test_compressed_responses_are_rejected_before_reading(source, encoding):
    from x_client_transaction._sources import TransactionSourceError, _text

    class UnreadBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            pytest.fail("Compressed body must not be read or decompressed")
            yield b""

        async def aclose(self):
            self.closed = True

    body = UnreadBody()

    def respond(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"content-encoding": encoding}, stream=body)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(
                TransactionSourceError if source else RegistrationError,
                match="Compressed",
            ):
                if source:
                    await _text(client, "https://example.invalid/")
                else:
                    await XPushRegistrar()._post(client, "login", {})
        assert body.closed

    asyncio.run(run())


@pytest.mark.parametrize("source", [False, True])
def test_uncompressed_stream_limit_is_enforced(source):
    from x_client_transaction._sources import (
        SOURCE_LIMIT,
        TransactionSourceError,
        _text,
    )
    from xpushstream.webpush import MAX_PAYLOAD

    class OversizedBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b" " * (SOURCE_LIMIT if source else MAX_PAYLOAD)
            yield b" "
            pytest.fail("Reading must stop when the size limit is exceeded")

        async def aclose(self):
            self.closed = True

    body = OversizedBody()

    async def run():
        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(
                TransactionSourceError if source else RegistrationError,
                match="size limit",
            ):
                if source:
                    await _text(client, "https://example.invalid/")
                else:
                    await XPushRegistrar()._post(client, "login", {})
        assert body.closed

    asyncio.run(run())


@pytest.mark.parametrize("resume", [False, True])
def test_template_checksum_is_automatically_used_for_individual_saves(resume, caplog):
    state = make_state()
    requests = []

    class CountingCt0(FakeCt0):
        calls = 0

        async def acquire(self, auth_token):
            self.calls += 1
            return await super().acquire(auth_token)

    provider = CountingCt0()

    def respond(request):
        device = json.loads(request.content)["push_device_info"]
        requests.append((request.url.path, device))
        assert request.headers["x-csrf-token"] == "synthetic-ct0"
        assert device["token"] == state.endpoint
        assert device["encryption_key1"] == encode64(state.keys.public_key)
        logging.getLogger("httpcore.http11").debug("wire %s", request.content)
        if not request.url.path.endswith("save.json"):
            assert "checksum" not in device
            assert "settings" not in device
            return httpx.Response(
                200, json={"push_settings_template": {"checksum": "fresh-checksum"}}
            )
        assert device["checksum"] == "fresh-checksum"
        assert len(device["settings"]) == 1
        return httpx.Response(
            200, json={"push_settings_template": {"checksum": "fresh-checksum"}}
        )

    registrar = XPushRegistrar(
        ct0_provider=provider,
        transport=httpx.MockTransport(respond),
        notify_likes=True,
        notify_mentions=False,
        notify_reposts=True,
    )
    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            (registrar.apply_settings if resume else registrar.register)(
                state, "synthetic-token"
            )
        )
    assert requests[0][0].endswith("checkin.json" if resume else "login.json")
    assert [device["settings"] for _, device in requests[1:]] == [
        {"LikesNonVitSetting": "anyone"},
        {"MentionsSetting": "off"},
        {"RetweetsSetting": "anyone"},
    ]
    assert provider.calls == 1
    assert "fresh-checksum" not in caplog.text
    assert state.endpoint not in caplog.text
    assert not private_http.get()


@pytest.mark.parametrize(
    "template",
    [
        None,
        [],
        {},
        {"checksum": None},
        {"checksum": 1},
        {"checksum": ""},
        {"checksum": "private\nchecksum"},
        {"checksum": "x" * 129},
    ],
)
def test_invalid_template_prevents_settings_save(template):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"push_settings_template": template})

    registrar = XPushRegistrar(
        ct0_provider=FakeCt0(),
        notify_likes=True,
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(RegistrationError) as caught:
        asyncio.run(registrar.register(make_state(), "synthetic-token"))
    assert len(requests) == 1
    assert "private" not in str(caught.value)
    assert not private_http.get()


def test_default_preferences_enable_all_three_types_on_resume():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content)["push_device_info"])
        return httpx.Response(
            200, json={"push_settings_template": {"checksum": "fresh-checksum"}}
        )

    registrar = XPushRegistrar(
        ct0_provider=FakeCt0(), transport=httpx.MockTransport(respond)
    )
    asyncio.run(registrar.apply_settings(make_state(), "synthetic-token"))
    assert [device["settings"] for device in requests[1:]] == [
        {"LikesNonVitSetting": "anyone"},
        {"MentionsSetting": "anyone"},
        {"RetweetsSetting": "anyone"},
    ]


def test_settings_save_failure_stops_remaining_updates():
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path.endswith("login.json"):
            return httpx.Response(
                200, json={"push_settings_template": {"checksum": "fresh-checksum"}}
            )
        return httpx.Response(500, json={"errors": [{"message": "private"}]})

    registrar = XPushRegistrar(
        ct0_provider=FakeCt0(),
        notify_likes=True,
        notify_mentions=True,
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(RegistrationError):
        asyncio.run(registrar.register(make_state(), "synthetic-token"))
    assert len(requests) == 2


@pytest.mark.parametrize(
    "options,expected",
    [
        ({"notify_likes": False}, {"LikesNonVitSetting": "off"}),
        ({"notify_mentions": False}, {"MentionsSetting": "off"}),
        ({"notify_reposts": False}, {"RetweetsSetting": "off"}),
        (
            dict.fromkeys(
                ["notify_likes", "notify_mentions", "notify_reposts"],
                True,
            ),
            dict.fromkeys(
                ["LikesNonVitSetting", "MentionsSetting", "RetweetsSetting"],
                "anyone",
            ),
        ),
    ],
)
def test_save_settings_uses_own_subscription_and_selected_fields(
    options, expected, caplog
) -> None:
    state = make_state()

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == (
            "https://x.com/i/api/1.1/notifications/settings/save.json"
        )
        assert request.headers["user-agent"].endswith("Safari/537.36")
        assert request.headers["x-twitter-client-language"] == "ja"
        assert request.headers["x-csrf-token"] == "synthetic-ct0"
        assert "auth_token=synthetic-token" in request.headers["cookie"]
        assert request.headers["x-client-transaction-id"].startswith(
            "synthetic-transaction-"
        )
        assert json.loads(request.content) == {
            "push_device_info": {
                "os_version": state.device_id,
                "udid": state.device_id,
                "checksum": "synthetic-checksum",
                "env": 3,
                "locale": "ja",
                "protocol_version": 1,
                "token": state.endpoint,
                "encryption_key1": encode64(state.keys.public_key),
                "encryption_key2": encode64(state.keys.auth_secret),
                "settings": {
                    **dict.fromkeys(
                        ["LikesNonVitSetting", "MentionsSetting", "RetweetsSetting"],
                        "anyone",
                    ),
                    **expected,
                },
            }
        }
        logging.getLogger("httpcore.http11").debug("wire %s", request.content)
        return httpx.Response(200, json={})

    registrar = XPushRegistrar(
        ct0_provider=FakeCt0(), locale="ja", transport=httpx.MockTransport(respond)
    )
    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            registrar.save_settings(
                state, "synthetic-token", checksum="synthetic-checksum", **options
            )
        )
    assert "synthetic-checksum" not in caplog.text
    assert state.endpoint not in caplog.text
    assert not private_http.get()


@pytest.mark.parametrize(
    "options",
    [
        {"notify_likes": None},
        {"notify_likes": ""},
        {"notify_mentions": "synthetic-private\nvalue"},
        {"notify_reposts": 1},
        {"checksum": "", "notify_likes": True},
    ],
)
def test_invalid_push_settings_rejected_before_authentication(options) -> None:
    registrar = XPushRegistrar(ct0_provider=FakeCt0())
    arguments = {"checksum": "synthetic-checksum", **options}
    with pytest.raises(ValueError) as caught:
        asyncio.run(registrar.save_settings(make_state(), "invalid", **arguments))
    assert "synthetic-private" not in str(caught.value)


@pytest.mark.parametrize(
    "status,body,error",
    [
        (403, {}, AuthenticationError),
        (500, {}, RegistrationError),
        (200, {"errors": [{"message": "synthetic-private"}]}, RegistrationError),
        (200, [], RegistrationError),
    ],
)
def test_save_settings_rejects_failed_response_without_private_details(
    status, body, error
) -> None:
    registrar = XPushRegistrar(
        ct0_provider=FakeCt0(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=body)
        ),
    )
    with pytest.raises(error) as caught:
        asyncio.run(
            registrar.save_settings(
                make_state(),
                "synthetic-token",
                checksum="synthetic-checksum",
                notify_likes=True,
            )
        )
    text = "".join(traceback.format_exception(caught.value))
    assert "synthetic-private" not in text
    assert "synthetic-token" not in text
    assert "synthetic-checksum" not in text


@pytest.mark.parametrize(
    "fixture_name,notification_type,body",
    [
        ("tweet", "tweet", "synthetic new post"),
        ("reply", "reply", "synthetic reply"),
        ("quote", "tweet", "synthetic quote post"),
        ("image", "tweet", "synthetic image post"),
        ("mention", "mention", "synthetic mention"),
        (
            "mr_infrequent_poster",
            "mr_infrequent_poster",
            "synthetic unclassified post notification",
        ),
    ],
)
def test_sanitized_live_structure_and_type_filter(
    fixture_name, notification_type, body
) -> None:
    # Observed 2026-10-07 after the user sent labeled test notifications.
    # Substitute private strings, retaining observed type and matching scribe_target.
    path = Path(__file__).parent / "fixtures" / f"x_notification_{fixture_name}.json"
    payload = path.read_bytes()
    event = normalize(payload)
    assert isinstance(event, UnknownNotification)
    assert event.raw == json.loads(payload)
    assert event.type == notification_type
    assert event.title == "Synthetic author"
    assert event.body == body
    assert event.url == "https://x.com/synthetic/status/123"
    assert event.tweet_id == "123"
    # Observed quotes/images are type=tweet; quote/media flags cannot be inferred.
    assert event.tweet is None
    assert EventFilter.event_types([notification_type])(event)
    assert not EventFilter.event_types(["favorite"])(event)
    assert not EventFilter.event_types(["quote"])(event)


def test_sanitized_live_follower_preserves_group_fields_without_a_tweet() -> None:
    # Observed 2026-10-07; preserve type, matching tag, and empty multi_body.
    path = Path(__file__).parent / "fixtures" / "x_notification_follower.json"
    payload = path.read_bytes()
    event = normalize(payload)
    assert event.raw == json.loads(payload)
    assert event.type == "follower"
    assert event.title == "Synthetic follower"
    assert event.body == "Synthetic account followed you"
    assert event.url == "https://x.com/synthetic_follower"
    assert event.tweet_id is None
    assert event.tweet is None
    assert event.raw["data"]["multi_uri"] == "/synthetic_group"
    assert event.raw["data"]["multi_body"] == ""
    assert event.raw["data"]["multi_title"] == "Synthetic follow group title"
    assert EventFilter.event_types(["follower"])(event)
    assert not EventFilter.event_types(["tweet"])(event)


@pytest.mark.parametrize(
    "notification_type,url,tweet_id,multi_fields",
    [
        (
            "favorite",
            "https://x.com/synthetic/status/123",
            "123",
            {"multi_title"},
        ),
        (
            "retweet",
            "https://x.com/synthetic/status/123",
            "123",
            {"multi_title"},
        ),
        (
            "follow_request_accepted",
            "https://x.com/synthetic",
            None,
            {"multi_uri", "multi_body", "multi_title"},
        ),
    ],
)
def test_additional_live_types_preserve_variant_fields(
    notification_type, url, tweet_id, multi_fields
) -> None:
    path = (
        Path(__file__).parent / "fixtures" / f"x_notification_{notification_type}.json"
    )
    payload = path.read_bytes()
    event = normalize(payload)
    assert event.type == notification_type
    assert event.raw == json.loads(payload)
    assert event.url == url
    assert event.tweet_id == tweet_id
    assert {
        key for key in event.raw["data"] if key.startswith("multi_")
    } == multi_fields
    assert EventFilter.event_types([notification_type])(event)
    assert not EventFilter.event_types(["tweet"])(event)


def test_observed_registration_request_and_redacted_wire_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = make_state()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("save.json"):
            device = json.loads(request.content)["push_device_info"]
            assert device["checksum"] == "fresh-checksum"
            assert list(device["settings"].values()) == ["anyone"]
            return httpx.Response(200, json={})
        assert (
            str(request.url)
            == "https://x.com/i/api/1.1/notifications/settings/login.json"
        )
        assert request.method == "POST"
        assert request.headers["x-csrf-token"] == "synthetic-ct0"
        assert request.headers["x-twitter-auth-type"] == "OAuth2Session"
        assert "auth_token=synthetic-token" in request.headers["cookie"]
        assert "ct0=synthetic-ct0" in request.headers["cookie"]
        assert json.loads(request.content) == {
            "push_device_info": {
                "os_version": state.device_id,
                "udid": state.device_id,
                "env": 3,
                "locale": "en",
                "protocol_version": 1,
                "token": state.endpoint,
                "encryption_key1": encode64(state.keys.public_key),
                "encryption_key2": encode64(state.keys.auth_secret),
            }
        }
        logging.getLogger("httpcore.http11").debug("wire %s", request.headers["cookie"])
        return httpx.Response(
            200, json={"push_settings_template": {"checksum": "fresh-checksum"}}
        )

    registrar = XPushRegistrar(
        ct0_provider=FakeCt0(), transport=httpx.MockTransport(respond)
    )
    with caplog.at_level(logging.DEBUG):
        asyncio.run(registrar.register(state, "synthetic-token"))
    assert "HTTP transport activity" in caplog.text
    assert "synthetic-token" not in caplog.text
    assert "synthetic-ct0" not in caplog.text
    assert not private_http.get()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpcore.http11").debug("outside SDK operation")
    assert "outside SDK operation" in caplog.text


def test_observed_browser_headers_keep_sdk_credentials_and_subscription(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = make_state()
    headers = {
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua": '"Chromium";v="154", "Google Chrome";v="154", '
        '"Not A(Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36",
        "DNT": "1",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        for name, value in headers.items():
            assert request.headers[name] == value
        assert request.headers["x-client-transaction-id"].startswith(
            "synthetic-transaction-"
        )
        assert request.headers["content-type"] == "application/json"
        assert request.headers["x-twitter-client-language"] == "ja"
        assert request.headers["x-csrf-token"] == "synthetic-ct0"
        assert "auth_token=synthetic-token" in request.headers["cookie"]
        device = json.loads(request.content)["push_device_info"]
        assert device["locale"] == "ja"
        assert device["udid"] == device["os_version"] == state.device_id
        assert device["token"] == state.endpoint
        assert device["encryption_key1"] == encode64(state.keys.public_key)
        assert device["encryption_key2"] == encode64(state.keys.auth_secret)
        logging.getLogger("httpcore.http11").debug("wire %s", request.headers)
        return httpx.Response(
            200, json={"push_settings_template": {"checksum": "fresh-checksum"}}
        )

    registrar = XPushRegistrar(
        ct0_provider=FakeCt0(),
        locale="ja",
        transport=httpx.MockTransport(respond),
    )
    with caplog.at_level(logging.DEBUG):
        asyncio.run(registrar.register(state, "synthetic-token"))
    assert "synthetic-token" not in caplog.text
    assert "synthetic-ct0" not in caplog.text


@pytest.mark.parametrize("locale", ["", "ja\r\nsynthetic-secret", "invalid_locale"])
def test_registration_locale_is_header_safe(locale) -> None:
    with pytest.raises(ValueError) as caught:
        XPushRegistrar(locale=locale)
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
def test_failed_registration_does_not_expose_response_or_cookies(status: int) -> None:
    registrar = XPushRegistrar(
        ct0_provider=FakeCt0(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, json={"errors": [{"message": "synthetic-private-body"}]}
            )
        ),
    )
    expected = AuthenticationError if status in {401, 403} else RegistrationError
    token = "synthetic-token"
    with pytest.raises(expected) as caught:
        asyncio.run(registrar.register(make_state(), token))
    text = "".join(traceback.format_exception(caught.value))
    assert "synthetic-private-body" not in text
    assert "synthetic-token" not in text
    assert "synthetic-ct0" not in text


@pytest.mark.parametrize("raw", [{"future": ["retained"]}, ["unrecognized"], None, 1])
def test_unknown_json_is_preserved(raw: object) -> None:
    event = normalize(json.dumps(raw).encode())
    assert isinstance(event, UnknownNotification)
    assert event.raw == raw
    assert event.type is None


@pytest.mark.parametrize(
    "url,expected",
    [
        ("/alice/status/123", "123"),
        ("https://twitter.com/alice/status/123?x=1", "123"),
        ("https://x.com.evil.invalid/alice/status/123", None),
        ("javascript:alert(1)", None),
        ("https://x.com/alice/status/not-a-number", None),
        ("https://x.com/u@evil.invalid/alice/status/123", None),
    ],
)
def test_tweet_reference_is_extracted_without_assuming_notification_cause(
    url: str, expected: str | None
) -> None:
    event = normalize(json.dumps({"data": {"url": url}}).encode())
    assert event.tweet_id == expected
    assert isinstance(event, UnknownNotification)
    assert event.type is None


@pytest.mark.parametrize("body", [b"not-json", b"\xff"])
def test_malformed_plaintext_raises_safe_normalization_error(body: bytes) -> None:
    with pytest.raises(NormalizationError):
        normalize(body)


def test_enrichment_success_failure_and_cancellation() -> None:
    event = normalize(b'{"url":"/alice/status/123"}')

    class Success:
        async def fetch(self, tweet_id: str) -> TweetDetails:
            return TweetDetails(tweet_id, text="synthetic full text")

    class Failure:
        async def fetch(self, tweet_id: str) -> TweetDetails:
            raise RuntimeError("synthetic-private-error")

    class Mismatch:
        async def fetch(self, tweet_id: str) -> TweetDetails:
            return TweetDetails("different")

    class Cancellation:
        async def fetch(self, tweet_id: str) -> TweetDetails:
            raise asyncio.CancelledError

    async def run() -> None:
        result = await enrich(event, Success())
        assert result.tweet.text == "synthetic full text"
        assert result.raw == event.raw
        assert await enrich(event, Failure()) is event
        assert await enrich(event, Mismatch()) is event
        assert await enrich(replace(event, tweet_id=None), Failure()) == replace(
            event, tweet_id=None
        )
        with pytest.raises(asyncio.CancelledError):
            await enrich(event, Cancellation())

    asyncio.run(run())


@pytest.mark.parametrize(
    "uri", ["/synthetic/status/123", "https://x.com/synthetic/status/123"]
)
def test_observed_nested_uri_extracts_tweet_reference(uri: str) -> None:
    raw = {
        "title": "synthetic",
        "body": "synthetic message",
        "data": {"type": "tweet", "uri": uri, "tag": "tweet-123"},
    }
    event = normalize(json.dumps(raw).encode())
    assert event.url == "https://x.com/synthetic/status/123"
    assert event.tweet_id == "123"
    assert event.raw == raw
    assert isinstance(event, UnknownNotification)
    assert event.type == "tweet"
    assert event.tweet is None


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"type": "favorite"}, "favorite"),
        ({"type": "future-type"}, "future-type"),
        ({"type": ""}, ""),
        ({}, None),
        ({"type": None}, None),
        ({"type": 123}, None),
        ([], None),
        (None, None),
    ],
)
def test_notification_type_is_preserved(data: object, expected: str | None) -> None:
    raw = {"data": data}
    event = normalize(json.dumps(raw).encode())
    assert event.type == expected
    assert event.to_dict(include_raw=False)["type"] == expected
    assert event.raw == raw
    assert isinstance(event, UnknownNotification)
    assert not hasattr(event, "kind")
    assert "kind" not in event.to_dict()
    assert "kind" not in event.to_dict(include_raw=False)
    assert "favorite" not in repr(event)
