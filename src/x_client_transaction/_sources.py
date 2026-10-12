"""Acquire bounded public X sources and extract observed algorithm inputs."""

import base64
import binascii
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

import httpx

SOURCE_LIMIT = 4 * 1024 * 1024
SHELL_URL = "https://x.com/i/jf/"
ASSET_ROOT = "https://abs.twimg.com/responsive-web/client-web/"


class TransactionSourceError(ValueError):
    """Public X client data is unavailable or uses an unsupported layout."""


@dataclass(frozen=True)
class SourceInputs:
    verification: bytes = field(repr=False)
    coordinates: tuple[int, ...]
    milliseconds: int


class _Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.verification = ""
        self.paths: list[list[str]] = []
        self._inside_frame = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "twitter-site-verification":
            self.verification = values.get("content") or ""
        if tag == "svg":
            self._inside_frame = (values.get("id") or "").startswith("loading-x-anim")
            if self._inside_frame:
                self.paths.append([])
        if tag == "path" and self._inside_frame:
            self.paths[-1].append(values.get("d") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "svg":
            self._inside_frame = False


def chunk_url(html: str) -> str:
    name = re.search(r"""\b([0-9]+)\s*:\s*["']ondemand[.]s["']""", html)
    if name:
        identifier = re.escape(name[1])
        tail = html[name.end() :]
        content_hash = re.search(
            rf"""\b{identifier}\s*:\s*["']([a-zA-Z0-9_-]+)["']""", tail
        )
        if content_hash:
            return ASSET_ROOT + "ondemand.s." + content_hash[1] + "a.js"
    raise TransactionSourceError("Unsupported X chunk map")


def parse_sources(html: str, javascript: str) -> SourceInputs:
    for source in (html, javascript):
        if len(source.encode("utf-8")) > SOURCE_LIMIT:
            raise TransactionSourceError("Public source exceeds size limit")
    document = _Document()
    document.feed(html)
    try:
        verification = base64.b64decode(document.verification, validate=True)
    except (ValueError, binascii.Error):
        raise TransactionSourceError("Invalid verification data") from None
    indices = tuple(
        int(match[1])
        for match in re.finditer(r"\[[ \t]*([0-9]{1,2})\],\s*16\)", javascript)
    )
    if not 6 <= len(verification) <= 256 or not 2 <= len(indices) <= 16:
        raise TransactionSourceError("Unsupported X key indices")
    if max(indices) >= len(verification):
        raise TransactionSourceError("X key index is out of bounds")
    try:
        path = document.paths[verification[5] % 4][1]
        segment = path[9:].split("C")[verification[indices[0]] % 16]
    except IndexError:
        raise TransactionSourceError("Missing X animation coordinates") from None
    numbers = re.findall(r"[0-9]+", segment)
    if len(numbers) != 11 or any(
        len(number) > 3 or int(number) > 255 for number in numbers
    ):
        raise TransactionSourceError("Unsupported X animation coordinates")
    product = 1
    for index in indices[1:]:
        product *= verification[index] % 16
    milliseconds = ((product + 5) // 10) * 10
    if milliseconds > 4096:
        raise TransactionSourceError("X animation time exceeds active interval")
    return SourceInputs(verification, tuple(map(int, numbers)), milliseconds)


async def _text(client: httpx.AsyncClient, url: str) -> str:
    try:
        async with client.stream(
            "GET", url, headers={"accept-encoding": "identity"}, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise TransactionSourceError("Public source returned non-200 status")
            if (
                response.headers.get("content-encoding", "identity").strip().lower()
                != "identity"
            ):
                raise TransactionSourceError(
                    "Compressed public sources are unsupported"
                )
            buffer = bytearray()
            async for block in response.aiter_bytes():
                if len(buffer) + len(block) > SOURCE_LIMIT:
                    raise TransactionSourceError("Public source exceeds size limit")
                buffer.extend(block)
        return buffer.decode("utf-8")
    except (httpx.HTTPError, UnicodeError):
        raise TransactionSourceError("Public source download failed") from None


async def fetch_sources(client: httpx.AsyncClient) -> SourceInputs:
    html = await _text(client, SHELL_URL)
    script = await _text(client, chunk_url(html))
    return parse_sources(html, script)
