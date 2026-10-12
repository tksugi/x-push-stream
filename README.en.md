<h1 align="center">XPushStream</h1>

<p align="center">
  <strong>Receive new-post, like, and follow notifications from X (formerly Twitter) in real time with Python.</strong><br>
  Python 3.10+ ｜ asyncio / async for ｜ CLI &amp; Webhooks
</p>

<p align="center">
  <a href="#installation"><img src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-22863A?style=flat-square" alt="MIT License"></a>
  <a href="#verification-and-delivery-limits"><img src="https://img.shields.io/badge/status-alpha-D97706?style=flat-square" alt="Alpha"></a>
</p>

<p align="center">
  <a href="README.md">日本語</a> · <strong>English</strong>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#usage">Usage</a> ·
  <a href="https://github.com/tksugi/x-push-stream/blob/HEAD/docs/usage.md">Usage guide (Japanese)</a> ·
  <a href="CONTRIBUTING.md">Contributing (Japanese)</a>
</p>

---

## Overview

XPushStream is a Python SDK for receiving real-time notifications from X (formerly Twitter), including new posts from accounts you have enabled notifications for, likes on your posts, and new followers.
It receives and decrypts X Web Push notifications and delivers them as a Python async event stream.
Read notifications with `async for`, process them in your application, or forward them to a webhook.
Authentication requires only the `auth_token` session cookie from X. The SDK handles connection setup, key generation, registration, decryption, and reconnection.

## Features

- **Async event stream**: Process incoming notifications with `async with` and `async for`.
- **Event filters**: Select notifications by type, text, regular expression, author, or a custom predicate.
- **Reusable subscriptions**: Set `client_id` to save subscription state and keys across restarts.
- **CLI and webhooks**: Output events as JSON or forward them to an HTTPS destination.
- **Original payloads**: Receive unknown notification types and access the original JSON through `event.raw`.

---

## Quick start

<a id="installation"></a>

### 1. Install

Requires **Python 3.10 or later**. Install from PyPI:

```console
python -m pip install xpushstream
```

<a id="receive-events-in-python"></a>

### 2. Receive events in Python

Replace `YOUR_AUTH_TOKEN` with the `auth_token` session cookie from X and save the following code as `receive.py`.

> [!IMPORTANT]
> By default, startup saves the like, mention, and repost notification settings on X to receive notifications from all accounts.
> Disable individual categories with `notify_likes=False`, `notify_mentions=False`, or `notify_reposts=False`. Previous settings are not restored on shutdown.

```python
import asyncio

from xpushstream import XPushClient


async def main():
    async with XPushClient(auth_token="YOUR_AUTH_TOKEN") as client:
        async for event in client:
            print(event.to_dict(include_raw=False))


asyncio.run(main())
```

Run it in the same terminal:

```console
python receive.py
```

Entering `async with` completes connection setup and notification settings updates. Events are then yielded as they arrive.
This example prints notification content without the original JSON. The output still includes potentially private content, such as notification text.

---

## Usage

### Filter incoming notifications

Replace the receive loop above to select replies and mentions containing `incident`.
Values in the event type list are combined with OR; chained conditions are combined with AND.

```python
async for event in (
    client.stream().event_types(["reply", "mention"]).contains("incident")
):
    print(event.to_dict(include_raw=False))
```

Filters select events after receipt and do not change settings on X.
See the [filter guide (Japanese)](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/usage.md#フィルター) for regular expressions, author filters, and custom predicates.

### Reuse a subscription across restarts

Pass a stable ID such as `client_id="notification-bot"` to `XPushClient` to save subscription state and keys in your OS user directory and reuse them at the next startup.
Without `client_id`, each client creates an independent subscription, writes no state to disk, and attempts to unsubscribe on shutdown.

Concurrent use of the same ID is rejected. Reusing a saved ID with a different `auth_token` raises `PersistentClientCredentialMismatch`.
Neither `auth_token` nor ct0 is persisted in plaintext. See [persistence and deletion (Japanese)](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/usage.md#永続化と削除) for storage locations and removal steps.
Each client supports one active consumer at a time.

## Receive and forward with the CLI

To use the CLI, set the `auth_token` session cookie from X in the `XPUSHSTREAM_AUTH_TOKEN` environment variable.
The CLI does not automatically load `.env` files.
`listen` writes one event per line as JSON, excluding raw data. `webhook` sends events to an HTTPS URL with POST.
Replace the example webhook URL with your own destination.

```console
xpushstream listen --client-id notification-bot --event-type reply --event-type mention --contains incident
xpushstream webhook --client-id relay --url https://example.com/events
```

Help and local state management do not require authentication.
Stop the corresponding client before running `status` or `reset`.

```console
xpushstream --help
xpushstream status --client-id notification-bot
xpushstream reset --client-id notification-bot
```

`reset` deletes local subscription state. It does not remove a device on X or unsubscribe from AutoPush.
See the [webhook guide (Japanese)](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/usage.md#webhookへ転送する) for Python routing, conditional destinations, and retry behavior.

## Events preserve the original notification

The default event class is `UnknownNotification`. X's `data.type` is preserved in `event.type`; the SDK does not infer its own notification categories.
Text, URLs, and post IDs are populated when available. Access the original JSON through `event.raw`.

| Observed action or notification | `event.type` |
|---|---|
| New post, quote, or post with an image | `tweet` |
| Reply / mention | `reply` / `mention` |
| Like / repost | `favorite` / `retweet` |
| Follow | `follower` |

Unknown types are preserved too. The type alone does not distinguish quotes or indicate whether a post has images.
No API for fetching full posts or author information is bundled. Connect your own fetch logic through `TweetEnrichmentProvider` to attach post details. If enrichment fails, the original event is returned.

## Verification and delivery limits

The SDK acknowledges a notification once it enters the in-memory queue.
**It does not guarantee durable storage until processing completes or exactly-once delivery.** A later process crash or webhook delivery failure can lose events.

Registration or delivery may fail when X changes its internal APIs or notification settings.
Complete post coverage is not guaranteed. Catch-up after downtime, delivery across multiple subscriptions for the same X session, and behavior for every notification type remain unverified.
The API may change during Alpha development.

<details>
<summary>Local tests and live-service observations</summary>

Local simulated services cover receiving, decryption, reconnection, persistence, filters, enrichment failure handling, webhooks, and the CLI.

On October 7, 2026, live checks on Windows confirmed registration, saving and reading back notification settings, persistent resume in a separate process, automatic reconnection, and recovery from injected faults.
Receiving and decrypting notifications has been observed for new posts, replies, quotes, posts with images, mentions, likes, reposts, and follows. The latest control-path checks did not receive new notifications.

On October 13, 2026, public-resource fetching and X registration/settings APIs were changed to reject compressed responses.
This change has been verified locally but has not been rechecked against live X traffic. Docker execution also remains unverified.

</details>

## Documentation and contributing

The detailed guides are currently in Japanese.

| Document | Contents |
|---|---|
| [Usage guide](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/usage.md) | Notification settings, persistence, filters, enrichment, webhooks, and errors |
| [Client transaction ID generation](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/client-transaction.md) | The bundled independent package |
| [Changelog](CHANGELOG.md) | Features and migration from development versions |
| [Contributing](CONTRIBUTING.md) | Development setup, quality checks, independent implementation rules, and bug reports |

Report bugs or suggest improvements through [Issues](https://github.com/tksugi/x-push-stream/issues).
Include your OS, Python and SDK versions, and reproduction steps. Do not attach credentials, keys, subscription identifiers, or unsanitized notifications.

<details>
<summary>Uninstalling</summary>

```console
python -m pip uninstall xpushstream
```

Uninstalling does not remove saved subscription state or notification settings on X.
To remove local state as well, stop the client and run `xpushstream reset --client-id <ID>` before uninstalling.

</details>

## License

[MIT License](LICENSE). Independently implemented from RFCs, Mozilla AutoPush documentation, and observations of X's public client and traffic.
Runtime dependencies are HTTPX, websockets, cryptography, and platformdirs, each under its own license.
This project is not affiliated with X Corp.
