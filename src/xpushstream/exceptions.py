"""Secret-safe errors exposed by the SDK."""

import math


class XPushError(Exception):
    """Base class for SDK failures."""


class AuthenticationError(XPushError):
    """An X session could not be established."""


class Ct0AcquisitionError(AuthenticationError):
    """The release-notes request did not yield a usable ct0 cookie."""


class AutoPushError(XPushError):
    """An AutoPush operation failed."""


class AutoPushConnectionError(AutoPushError):
    """A recoverable connection failure, optionally with server backoff."""

    def __init__(
        self, message: str = "AutoPush connection failed.", *, retry_after: float = 0
    ) -> None:
        super().__init__(message)
        self.retry_after = max(0.0, retry_after) if math.isfinite(retry_after) else 0.0


class ProtocolError(AutoPushError):
    """A protocol boundary contained invalid data."""


class ChannelConflictError(AutoPushError):
    """AutoPush rejected an already-used channel ID."""


class RegistrationError(XPushError):
    """X did not accept the subscription."""


class DecryptionError(XPushError):
    """Web Push authentication or decoding failed."""


class NormalizationError(XPushError):
    """The decrypted notification was not valid JSON."""


class StateError(XPushError):
    """Persistent subscription state could not be used safely."""


class PersistentClientCredentialMismatch(StateError):
    """A logical client was previously used with different credentials."""


class PersistentClientInUseError(StateError):
    """The particular persistent client is already owned by another consumer."""


class EnrichmentError(XPushError):
    """An optional enrichment provider failed."""


class WebhookDeliveryError(XPushError):
    """Webhook delivery exhausted its retries."""
