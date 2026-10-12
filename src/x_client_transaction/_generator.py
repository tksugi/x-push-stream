"""Request-bound ID envelope derived from X module 564498 observations."""

import base64
import hashlib
import re
import secrets
import struct
import time
from dataclasses import dataclass, field

import httpx

from ._animation import animation_key
from ._sources import SourceInputs, fetch_sources, parse_sources

EPOCH_SECONDS = 1682924400


@dataclass(frozen=True, init=False)
class TransactionIdGenerator:
    """Prepared public inputs; generation is synchronous and performs no I/O."""

    _inputs: SourceInputs = field(repr=False)
    _style: str = field(repr=False)

    def __init__(self, *, html: str, javascript: str) -> None:
        self._initialize(parse_sources(html, javascript))

    def _initialize(self, inputs: SourceInputs) -> None:
        object.__setattr__(self, "_inputs", inputs)
        object.__setattr__(
            self, "_style", animation_key(inputs.coordinates, inputs.milliseconds)
        )

    @classmethod
    def from_sources(cls, html: str, javascript: str) -> "TransactionIdGenerator":
        """Prepare from paired app-shell and ondemand source text."""
        return cls(html=html, javascript=javascript)

    @classmethod
    async def fetch(
        cls, client: httpx.AsyncClient | None = None
    ) -> "TransactionIdGenerator":
        """Fetch public inputs with no credentials; supplied client is not closed."""
        if client is None:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as owned:
                return await cls.fetch(owned)
        instance = object.__new__(cls)
        instance._initialize(await fetch_sources(client))
        return instance

    def generate(
        self,
        method: str,
        path: str,
        *,
        timestamp: int | None = None,
        random_byte: int | None = None,
    ) -> str:
        """Use the exact post-rewrite request path passed to X's header filter.

        timestamp uses Unix seconds. Fixed timestamp/random_byte support tests.
        X's observed WebRTC values are truncated before forming this envelope.
        """
        if re.fullmatch("[A-Z]+", method) is None:
            raise ValueError("Expected an uppercase HTTP method")
        if (
            not path.startswith("/")
            or path.startswith("//")
            or any(char in "?!#" or not 33 <= ord(char) <= 126 for char in path)
        ):
            raise ValueError("Expected an ASCII request path without query or fragment")
        seconds = int(time.time()) if timestamp is None else timestamp
        if (
            type(seconds) is not int
            or not EPOCH_SECONDS <= seconds < EPOCH_SECONDS + 2**32
        ):
            raise ValueError("Timestamp is outside the wire epoch")
        mask = secrets.randbits(8) if random_byte is None else random_byte
        if type(mask) is not int or not 0 <= mask < 256:
            raise ValueError("Random byte is outside 0..255")
        elapsed = seconds - EPOCH_SECONDS
        hash_input = (
            "!".join((method, path, str(elapsed))) + "obfiowerehiring" + self._style
        )
        fingerprint = hashlib.sha256(hash_input.encode()).digest()[:16]
        body = b"".join(
            (
                self._inputs.verification,
                struct.pack("<I", elapsed),
                fingerprint,
                bytes([3]),
            )
        )
        wire = bytes([mask]) + bytes(value ^ mask for value in body)
        return base64.b64encode(wire).decode("ascii").rstrip("=")
