"""Synthetic vectors measured from X module 564498 and Chromium CSS."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from x_client_transaction import TransactionIdGenerator, TransactionSourceError
from x_client_transaction._animation import _hex_number, easing, fixed_two, style_key

PATH = "/i/api/1.1/notifications/settings/login.json"
SCRIPT = "(n[41],16)(n[35],16)(n[44],16)(n[9],16)"
OBSERVATIONS = json.loads(
    (Path(__file__).parent / "fixtures/x_tid_observed.json").read_text(encoding="utf-8")
)["vectors"]


def shell(seed: int = 0) -> str:
    key = bytes((i * 7 + seed) % 256 for i in range(48))
    rows = [
        [10 + i, 50 + i, 100 + i, 200 - i, 150 - i, 80 + i, 120 + i, 64, 100, 180, 220]
        for i in range(16)
    ]
    path = "M0 0 0 0 " + "C".join(" ".join(map(str, row)) for row in rows)
    return (
        '<meta name="twitter-site-verification" content="'
        + base64.b64encode(key).decode()
        + '"><script>({12:"ondemand.s"})[e]||e)+"."+({12:"abc"})[e]+"a.js"</script>'
        + "".join(
            f'<svg id="loading-x-anim-{i}"><g>'
            f'<path d="M0"/><path d="{path}"/></g></svg>'
            for i in range(4)
        )
    )


@pytest.mark.parametrize(
    ("seed", "expected"),
    [(item["seed"], item["result"]) for item in OBSERVATIONS],
)
def test_reference_vectors(seed: int, expected: str) -> None:
    generator = TransactionIdGenerator.from_sources(shell(seed), SCRIPT)
    assert (
        generator.generate("POST", PATH, timestamp=1682924400 + 123456, random_byte=42)
        == expected
    )
    assert "_inputs" not in repr(generator)
    observed = next(item for item in OBSERVATIONS if item["seed"] == seed)
    assert (
        generator.generate(
            "POST",
            PATH,
            timestamp=1682924400 + 123456,
            random_byte=42,
        )
        == observed["rtc_result"]
    )


def test_wire_encoding_and_epoch_zero() -> None:
    generator = TransactionIdGenerator.from_sources(shell(), SCRIPT)
    encoded = generator.generate("GET", "/test", timestamp=1682924400, random_byte=0)
    wire = base64.b64decode(encoded + "=" * (-len(encoded) % 4))
    assert wire[:49] == b"\0" + bytes(i * 7 % 256 for i in range(48))
    assert wire[49:53] == b"\0" * 4
    assert (
        wire[53:69]
        == hashlib.sha256(
            f"GET!/test!0obfiowerehiring{generator._style}".encode()
        ).digest()[:16]
    )
    assert wire[-1] == 3
    assert generator.generate("GET", "/test")
    assert (
        generator.generate("POST", "/test", timestamp=1682924400, random_byte=0)
        != encoded
    )


def test_javascript_numeric_compatibility() -> None:
    assert fixed_two(0.125) == 0.13
    assert fixed_two(-0.125) == -0.13
    assert fixed_two(2.675) == 2.67
    assert _hex_number(0.1) == "0.1999999999999a"
    assert easing((0.25, 0.5, 0.75, 0.5), 0) == 0
    assert easing((0.25, 0.5, 0.75, 0.5), 1) == 1
    assert style_key((12, 34, 56), (0.5, -0.25, 0.25, 0.5, 0, 0)) == "c22380804040800"


@pytest.mark.parametrize(
    ("html", "script"),
    [
        ("", SCRIPT),
        (shell().replace('content="', 'content="!'), SCRIPT),
        (shell(), "changed JavaScript"),
        (shell(), "(a[99],16)(a[1],16)"),
        (shell().replace("loading-x-anim", "missing"), SCRIPT),
        (shell().replace("64 100 180 220", "999 100 180 220"), SCRIPT),
        pytest.param("x" * (4 * 1024 * 1024 + 1), SCRIPT, id="oversized-source"),
    ],
)
def test_changed_sources_fail_explicitly(html: str, script: str) -> None:
    with pytest.raises(TransactionSourceError):
        TransactionIdGenerator.from_sources(html, script)


@pytest.mark.parametrize(
    ("method", "path", "timestamp", "random_byte"),
    [
        ("post", PATH, 1682924400, 1),
        ("GET", "https://x.com/test", 1682924400, 1),
        ("GET", "/test?x=1", 1682924400, 1),
        ("GET", "/test\n", 1682924400, 1),
        ("GET", PATH, 0, 1),
        ("GET", PATH, 1682924400 + 2**32, 1),
        ("GET", PATH, 1682924400, 256),
        ("GET", PATH, 1682924400, True),
    ],
)
def test_invalid_request_inputs(
    method: str, path: str, timestamp: int, random_byte: int
) -> None:
    generator = TransactionIdGenerator.from_sources(shell(), SCRIPT)
    with pytest.raises(ValueError):
        generator.generate(method, path, timestamp=timestamp, random_byte=random_byte)


def test_fetch_with_caller_owned_client() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(200, text=shell() if len(requests) == 1 else SCRIPT)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            generator = await TransactionIdGenerator.fetch(client)
            assert not client.is_closed
            assert generator.generate("GET", "/test")

    asyncio.run(run())
    assert [str(request.url) for request in requests] == [
        "https://x.com/i/jf/",
        "https://abs.twimg.com/responsive-web/client-web/ondemand.s.abca.js",
    ]


@pytest.mark.parametrize("status", [302, 403, 429, 500])
def test_fetch_failure_does_not_expose_response(status: int) -> None:
    async def run() -> None:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(
                status,
                text="private response",
                headers={"location": "https://example.com"},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(TransactionSourceError) as error:
                await TransactionIdGenerator.fetch(client)
            assert "private response" not in str(error.value)

    asyncio.run(run())
