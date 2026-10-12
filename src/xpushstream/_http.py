"""Redact third-party wire logging only within this SDK's HTTP operations."""

import logging
import math
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

private_http: ContextVar[bool] = ContextVar("xpushstream_private_http", default=False)


def retry_after(value: str | None) -> float:
    """Honor finite Retry-After seconds or HTTP dates; cancellation remains possible."""
    if value is None:
        return 0
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (
                parsedate_to_datetime(value) - datetime.now(timezone.utc)
            ).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0


class _WireFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if private_http.get():
            record.msg = "HTTP transport activity (details withheld)."
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


_filter = _WireFilter()
for _name in (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
):
    logging.getLogger(_name).addFilter(_filter)
