"""Composable predicates and lazy async filtering above the event stream."""

import re
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable
from dataclasses import dataclass

from .events import XPushEvent


def _text(event: XPushEvent) -> str:
    return "\n".join(
        value
        for value in (
            event.title,
            event.body,
            event.tweet.text if event.tweet else None,
        )
        if value
    )


@dataclass(frozen=True)
class EventFilter:
    predicate: Callable[[XPushEvent], bool]

    def __call__(self, event: XPushEvent) -> bool:
        return self.predicate(event)

    def __and__(self, other: "EventFilter") -> "EventFilter":
        return EventFilter(lambda event: self(event) and other(event))

    def __or__(self, other: "EventFilter") -> "EventFilter":
        return EventFilter(lambda event: self(event) or other(event))

    def __invert__(self) -> "EventFilter":
        return EventFilter(lambda event: not self(event))

    @classmethod
    def contains(cls, keyword: str) -> "EventFilter":
        return cls(lambda event: keyword in _text(event))

    @classmethod
    def users(cls, user_ids: Iterable[str]) -> "EventFilter":
        selected = frozenset(user_ids)
        return cls(
            lambda event: event.tweet is not None and event.tweet.author_id in selected
        )

    @classmethod
    def usernames(cls, usernames: Iterable[str]) -> "EventFilter":
        selected = {name.removeprefix("@").casefold() for name in usernames}
        return cls(
            lambda event: (
                event.tweet is not None
                and event.tweet.username is not None
                and event.tweet.username.casefold() in selected
            )
        )

    @classmethod
    def event_types(cls, types: Iterable[str]) -> "EventFilter":
        """Match X's notification type exactly, including unknown string values."""
        selected = frozenset(types)
        return cls(lambda event: event.type in selected)

    @classmethod
    def regex(cls, pattern: str, flags: int = 0) -> "EventFilter":
        expression = re.compile(pattern, flags)
        return cls(lambda event: expression.search(_text(event)) is not None)


class EventStream(AsyncIterable[XPushEvent]):
    def __init__(
        self,
        source: AsyncIterable[XPushEvent],
        predicate: Callable[[XPushEvent], bool] | None = None,
    ) -> None:
        self._source = source
        self._predicate = predicate or (lambda event: True)

    async def __aiter__(self) -> AsyncIterator[XPushEvent]:
        async for event in self._source:
            if self._predicate(event):
                yield event

    def where(self, predicate: Callable[[XPushEvent], bool]) -> "EventStream":
        return EventStream(
            self._source, lambda event: self._predicate(event) and predicate(event)
        )

    def users(self, user_ids: Iterable[str]) -> "EventStream":
        return self.where(EventFilter.users(user_ids))

    def usernames(self, usernames: Iterable[str]) -> "EventStream":
        return self.where(EventFilter.usernames(usernames))

    def event_types(self, types: Iterable[str]) -> "EventStream":
        return self.where(EventFilter.event_types(types))

    def contains(self, keyword: str) -> "EventStream":
        return self.where(EventFilter.contains(keyword))

    def exclude(self, keyword: str) -> "EventStream":
        return self.where(~EventFilter.contains(keyword))

    def regex(self, pattern: str, flags: int = 0) -> "EventStream":
        return self.where(EventFilter.regex(pattern, flags))

    def exclude_replies(self) -> "EventStream":
        return self.where(
            lambda event: (
                event.type != "reply"
                and (event.tweet is None or event.tweet.is_reply is not True)
            )
        )

    def exclude_quotes(self) -> "EventStream":
        return self.where(
            lambda event: event.tweet is None or event.tweet.is_quote is not True
        )

    def exclude_reposts(self) -> "EventStream":
        return self.where(
            lambda event: (
                event.type != "retweet"
                and (event.tweet is None or event.tweet.is_repost is not True)
            )
        )

    def has_media(self) -> "EventStream":
        return self.where(
            lambda event: event.tweet is not None and bool(event.tweet.media)
        )
