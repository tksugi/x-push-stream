"""X wire contracts observed in its public web client on 2026-10-05.

These are X's public application constants, not user credentials.
Registration has been checked with a real session; multi-endpoint delivery
behavior remains unverified.
"""

import asyncio
import json
import re
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from x_client_transaction import TransactionIdGenerator, TransactionSourceError

from ._http import private_http
from .auth import Ct0Provider, ReleaseNotesCt0Provider, is_cookie_value
from .events import UnknownNotification, XPushEvent
from .exceptions import AuthenticationError, NormalizationError, RegistrationError
from .state import PushSubscriptionState
from .webpush import MAX_PAYLOAD, encode64

PUBLIC_APPLICATION_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%"
    "3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
# Browser values supplied from the observed X registration on 2026-10-07.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
SEC_CH_UA_PLATFORM = '"Windows"'
SEC_CH_UA = '"Chromium";v="154", "Google Chrome";v="154", "Not A(Brand";v="99"'
SEC_CH_UA_MOBILE = "?0"
DNT = "1"
APPLICATION_SERVER_KEY = encode64(
    bytes(
        [
            4,
            94,
            104,
            18,
            141,
            49,
            13,
            74,
            96,
            202,
            82,
            131,
            78,
            91,
            29,
            242,
            150,
            102,
            197,
            0,
            53,
            149,
            230,
            8,
            54,
            38,
            62,
            173,
            43,
            28,
            89,
            130,
            191,
            222,
            213,
            128,
            147,
            62,
            21,
            49,
            187,
            95,
            212,
            194,
            196,
            253,
            140,
            157,
            234,
            34,
            8,
            234,
            143,
            158,
            221,
            15,
            83,
            8,
            222,
            111,
            100,
            204,
            213,
            48,
            75,
        ]
    )
)


class PushRegistrar(Protocol):
    application_server_key: str

    async def register(self, state: PushSubscriptionState, auth_token: str) -> None: ...


def _valid_checksum(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value.isascii()
        and value.isprintable()
    )


def _notification_settings(
    likes: bool, mentions: bool, retweets: bool
) -> dict[str, str]:
    values = (
        ("LikesNonVitSetting", likes),
        ("MentionsSetting", mentions),
        ("RetweetsSetting", retweets),
    )
    if any(type(value) is not bool for _, value in values):
        raise ValueError("Notification preferences must be True or False.")
    return {name: "anyone" if value else "off" for name, value in values}


class XPushRegistrar:
    """Register our subscription using the observed browser header values."""

    application_server_key = APPLICATION_SERVER_KEY

    def __init__(
        self,
        *,
        ct0_provider: Ct0Provider | None = None,
        locale: str = "en",
        notify_likes: bool = True,
        notify_mentions: bool = True,
        notify_reposts: bool = True,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        transaction_generator: TransactionIdGenerator | None = None,
    ) -> None:
        if not isinstance(locale, str) or not re.fullmatch(
            r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", locale
        ):
            raise ValueError("Invalid X registration locale.")
        self._ct0_provider = ct0_provider
        self._transaction_generator = transaction_generator
        self._transaction_lock = asyncio.Lock()
        self._locale = locale
        self._proxy = proxy
        self._transport = transport
        self._settings = _notification_settings(
            notify_likes, notify_mentions, notify_reposts
        )

    async def _get_transaction_generator(self) -> TransactionIdGenerator:
        async with self._transaction_lock:
            if self._transaction_generator is None:
                token = private_http.set(True)
                try:
                    async with httpx.AsyncClient(
                        timeout=20,
                        proxy=self._proxy,
                        trust_env=False,
                        follow_redirects=False,
                    ) as client:
                        self._transaction_generator = (
                            await TransactionIdGenerator.fetch(client)
                        )
                except TransactionSourceError:
                    raise RegistrationError(
                        "X transaction source preparation failed."
                    ) from None
                finally:
                    private_http.reset(token)
            return self._transaction_generator

    async def register(self, state: PushSubscriptionState, auth_token: str) -> None:
        await self._request(state, auth_token, "login", settings=self._settings)

    async def apply_settings(
        self, state: PushSubscriptionState, auth_token: str
    ) -> None:
        """Fetch a fresh template and apply configured settings on resume."""
        await self._request(state, auth_token, "checkin", settings=self._settings)

    async def save_settings(
        self,
        state: PushSubscriptionState,
        auth_token: str,
        *,
        checksum: str,
        notify_likes: bool = True,
        notify_mentions: bool = True,
        notify_reposts: bool = True,
    ) -> None:
        """Save selected settings with an explicitly acquired template checksum."""
        settings = _notification_settings(notify_likes, notify_mentions, notify_reposts)
        if not _valid_checksum(checksum):
            raise ValueError("Invalid X push notification settings or checksum.")
        await self._request(
            state, auth_token, "save", checksum=checksum, settings=settings
        )

    async def _request(
        self,
        state: PushSubscriptionState,
        auth_token: str,
        operation: str,
        *,
        checksum: str | None = None,
        settings: dict[str, str],
    ) -> None:
        if not is_cookie_value(auth_token):
            raise AuthenticationError("auth_token is not a valid cookie value.")
        state.validate()
        generator = await self._get_transaction_generator()
        provider = self._ct0_provider
        if provider is None:
            provider = ReleaseNotesCt0Provider(
                proxy=self._proxy,
                headers={
                    "user-agent": USER_AGENT,
                    "x-client-transaction-id": generator.generate(
                        "GET", "/i/release_notes"
                    ),
                },
            )
        ct0 = await provider.acquire(auth_token)
        if not is_cookie_value(ct0):
            raise AuthenticationError(
                "The ct0 provider returned an invalid cookie value."
            )
        device = {
            "os_version": state.device_id,
            "udid": state.device_id,
            "env": 3,
            "locale": self._locale,
            "protocol_version": 1,
            "token": state.endpoint,
            "encryption_key1": encode64(state.keys.public_key),
            "encryption_key2": encode64(state.keys.auth_secret),
        }
        headers = {
            "authorization": PUBLIC_APPLICATION_BEARER,
            "x-csrf-token": ct0,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": self._locale,
            "origin": "https://x.com",
            "referer": "https://x.com/settings/push_notifications",
            "user-agent": USER_AGENT,
            "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
            "dnt": DNT,
        }
        token = private_http.set(True)
        try:
            async with httpx.AsyncClient(
                headers=headers,
                cookies={"auth_token": auth_token, "ct0": ct0},
                timeout=20,
                proxy=self._proxy,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                if operation != "save":
                    value = await self._post(client, operation, device)
                    template = value.get("push_settings_template")
                    checksum = (
                        template.get("checksum") if isinstance(template, dict) else None
                    )
                    if not _valid_checksum(checksum):
                        raise RegistrationError(
                            "X push settings response has no valid template checksum."
                        )
                    # Observed browser saves one setting per request.
                    for name, setting in settings.items():
                        await self._post(
                            client,
                            "save",
                            {
                                **device,
                                "checksum": checksum,
                                "settings": {name: setting},
                            },
                        )
                else:
                    await self._post(
                        client,
                        "save",
                        {**device, "checksum": checksum, "settings": settings},
                    )
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            ValueError,
            TypeError,
            RecursionError,
        ):
            raise RegistrationError("X push request failed.") from None
        finally:
            private_http.reset(token)

    async def _post(
        self, client: httpx.AsyncClient, operation: str, device: dict[str, object]
    ) -> dict[str, object]:
        path = f"/i/api/1.1/notifications/settings/{operation}.json"
        generator = await self._get_transaction_generator()
        async with client.stream(
            "POST",
            f"https://x.com{path}",
            headers={
                "x-client-transaction-id": generator.generate("POST", path),
                "accept-encoding": "identity",
            },
            json={"push_device_info": device},
        ) as response:
            if response.status_code in {401, 403}:
                raise AuthenticationError("X rejected the authentication session.")
            if response.status_code != 200:
                raise RegistrationError(
                    f"X push {operation} failed (HTTP {response.status_code})."
                )
            if (
                response.headers.get("content-encoding", "identity").strip().lower()
                != "identity"
            ):
                raise RegistrationError("Compressed X push responses are unsupported.")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_PAYLOAD:
                    raise RegistrationError("X push response exceeds its size limit.")
                body.extend(chunk)
            value = json.loads(body)
            if not isinstance(value, dict) or value.get("errors"):
                raise RegistrationError("X push request returned an error response.")
            return value


def normalize(payload: bytes) -> XPushEvent:
    try:
        raw = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise NormalizationError(
            "The decrypted notification is not valid JSON."
        ) from None
    title = body = url = tweet_id = notification_type = None
    if isinstance(raw, dict):
        title = raw.get("title") if isinstance(raw.get("title"), str) else None
        body = raw.get("body") if isinstance(raw.get("body"), str) else None
        data = raw.get("data", {})
        if isinstance(data, dict) and isinstance(data.get("type"), str):
            notification_type = data["type"]
        candidate = raw.get("url") or (
            (data.get("url") or data.get("uri")) if isinstance(data, dict) else None
        )
        if isinstance(candidate, str):
            try:
                resolved = urljoin("https://x.com/", candidate)
                parsed = urlsplit(resolved)
                if (
                    parsed.scheme == "https"
                    and parsed.hostname
                    in {
                        "x.com",
                        "www.x.com",
                        "twitter.com",
                        "www.twitter.com",
                        "mobile.twitter.com",
                    }
                    and not parsed.username
                    and not parsed.password
                ):
                    url = resolved
                    match = re.fullmatch(
                        r"/[^/]+/status(?:es)?/([0-9]+)/?", parsed.path
                    )
                    if match:
                        tweet_id = match[1]
            except ValueError:
                pass
    # A link to a tweet does not identify reply/mention/repost/creation semantics.
    return UnknownNotification(
        raw=raw,
        title=title,
        body=body,
        url=url,
        tweet_id=tweet_id,
        type=notification_type,
    )
