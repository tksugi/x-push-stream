"""Independent in-memory service doubles and a sender for synthetic test messages."""

import asyncio
import hashlib
import hmac
import json
import os
import struct
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from xpushstream.autopush import AckOutcome, Notification
from xpushstream.state import PushSubscriptionState, credential_fingerprint
from xpushstream.webpush import PushKeys, encode64, generate_keys


def make_state(token: str = "synthetic-token") -> PushSubscriptionState:
    return PushSubscriptionState(
        str(uuid4()),
        str(uuid4()),
        "https://push.invalid/opaque",
        generate_keys(),
        credential_fingerprint(token),
        "synthetic-device",
    )


def encrypted(keys: PushKeys, plaintext: bytes, *, delimiter: int = 2) -> bytes:
    sender = ec.generate_private_key(ec.SECP256R1())
    public = sender.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = sender.exchange(
        ec.ECDH(),
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), keys.public_key),
    )
    # Sender-side HMAC expansion is independent of the receiver's HKDF wrapper.
    combined = hmac.digest(keys.auth_secret, shared, "sha256")
    ikm = hmac.digest(
        combined, b"WebPush: info\0" + keys.public_key + public + b"\1", "sha256"
    )
    salt = os.urandom(16)
    prk = hmac.digest(salt, ikm, "sha256")
    cek = hmac.digest(prk, b"Content-Encoding: aes128gcm\0\1", "sha256")[:16]
    nonce = hmac.digest(prk, b"Content-Encoding: nonce\0\1", "sha256")[:12]
    content = AESGCM(cek).encrypt(nonce, plaintext + bytes([delimiter]), None)
    return (
        struct.pack("!16sIB", salt, max(4096, len(content) + 1), 65) + public + content
    )


def notification(
    state: PushSubscriptionState, version: str, raw: object
) -> Notification:
    return Notification(
        state.channel_id,
        version,
        encode64(encrypted(state.keys, json.dumps(raw).encode())),
        {"encoding": "aes128gcm"},
    )


class FakeTransport:
    def __init__(self, *, uaid: str | None = None) -> None:
        self.uaid = uaid or str(uuid4())
        self.opened: list[tuple[str | None, str | None]] = []
        self.channels: list[str] = []
        self.acks: list[tuple[Notification, AckOutcome]] = []
        self.unregistered: list[str] = []
        self.closed = False
        self.received = 0
        self.messages: asyncio.Queue[Notification | BaseException] = asyncio.Queue()

    async def open(self, uaid: str | None, channel_id: str | None) -> str:
        self.opened.append((uaid, channel_id))
        return self.uaid

    async def register(self, channel_id: str, application_server_key: str) -> str:
        self.channels.append(channel_id)
        return "https://push.invalid/" + channel_id

    async def receive(self) -> Notification:
        value = await self.messages.get()
        self.received += 1
        if isinstance(value, BaseException):
            raise value
        return value

    async def ack(self, notification: Notification, outcome: AckOutcome) -> None:
        self.acks.append((notification, outcome))

    async def unregister(self, channel_id: str) -> None:
        self.unregistered.append(channel_id)

    async def close(self) -> None:
        self.closed = True


class FakeRegistrar:
    application_server_key = "synthetic-public-application-key"

    def __init__(self, error: Exception | None = None) -> None:
        self.registrations: list[PushSubscriptionState] = []
        self.error = error

    async def register(self, state: PushSubscriptionState, auth_token: str) -> None:
        self.registrations.append(state)
        assert (
            state.credential_fingerprint
            == hashlib.sha256(auth_token.encode()).hexdigest()
        )
        if self.error:
            raise self.error


class FakeTransactionGenerator:
    """Offline prepared inputs with a new observable ID per generate call."""

    def __init__(self):
        self.calls = []

    @classmethod
    async def fetch(cls, client=None):
        return cls()

    def generate(self, method, path):
        self.calls.append((method, path))
        return f"synthetic-transaction-{len(self.calls)}"


async def wait_until(predicate: object) -> None:
    from collections.abc import Callable

    assert isinstance(predicate, Callable)

    async def spin() -> None:
        while not predicate():  # noqa: ASYNC110 - bounded test-double observation
            await asyncio.sleep(0)

    await asyncio.wait_for(spin(), 2)
