"""Stable event envelopes; notification cause is never inferred from a tweet URL."""

import asyncio
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from .exceptions import EnrichmentError


@dataclass(frozen=True)
class TweetDetails:
    tweet_id: str
    author_id: str | None = None
    username: str | None = None
    text: str | None = field(default=None, repr=False)
    media: tuple[dict[str, Any], ...] = field(default=(), repr=False)
    is_reply: bool | None = None
    is_quote: bool | None = None
    is_repost: bool | None = None


@dataclass(frozen=True, kw_only=True)
class XPushEvent:
    raw: Any = field(repr=False)
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    title: str | None = field(default=None, repr=False)
    body: str | None = field(default=None, repr=False)
    url: str | None = field(default=None, repr=False)
    tweet_id: str | None = None
    tweet: TweetDetails | None = field(default=None, repr=False)
    type: str | None = field(default=None, repr=False)

    def to_dict(self, *, include_raw: bool = True) -> dict[str, Any]:
        value = asdict(self)
        value["received_at"] = self.received_at.isoformat()
        if not include_raw:
            value.pop("raw")
        return value


@dataclass(frozen=True, kw_only=True)
class UnknownNotification(XPushEvent):
    """An unclassified notification; type still preserves X's data.type value."""


class TweetEnrichmentProvider(Protocol):
    async def fetch(self, tweet_id: str) -> TweetDetails:
        """Fetch optional details; implementations own their resources."""
        ...


async def enrich(event: XPushEvent, provider: TweetEnrichmentProvider) -> XPushEvent:
    """Keep the original event if optional enrichment fails."""
    if event.tweet_id is None:
        return event
    try:
        details = await asyncio.wait_for(provider.fetch(event.tweet_id), 10)
        if not isinstance(details, TweetDetails) or details.tweet_id != event.tweet_id:
            raise EnrichmentError("Enrichment returned a different tweet.")
        return replace(event, tweet=details)
    except Exception:
        # Cancellation (BaseException) propagates; third-party errors are not logged.
        return event
