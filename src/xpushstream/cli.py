"""Thin SDK consumers; credentials come only from the environment."""

import argparse
import asyncio
import json
import os
import sys

from .client import XPushClient
from .exceptions import XPushError
from .state import FileStateStore
from .webhook import Route, WebhookDestination, WebhookRouter


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xpushstream")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("listen", "webhook"):
        child = commands.add_parser(command)
        child.add_argument("--client-id")
        child.add_argument("--contains")
        child.add_argument(
            "--event-type",
            action="append",
            help="Select an exact X type; repeat for OR.",
        )
        if command == "webhook":
            child.add_argument("--url", required=True)
    for command in ("status", "reset"):
        child = commands.add_parser(command)
        child.add_argument("--client-id", required=True)
    return parser


async def _run(args: argparse.Namespace) -> None:
    if args.command in {"status", "reset"}:
        store = FileStateStore()
        async with store.claim(args.client_id):
            if args.command == "reset":
                await store.delete(args.client_id)
                print("Persistent state reset.")
            else:
                state = await store.load(args.client_id)
                print(
                    json.dumps(
                        {
                            "exists": state is not None,
                            "registered": state.registered if state else False,
                        }
                    )
                )
        return
    token = os.environ.get("XPUSHSTREAM_AUTH_TOKEN", "")
    if not token:
        raise ValueError("Set XPUSHSTREAM_AUTH_TOKEN in the environment.")
    router = (
        WebhookRouter([Route(lambda event: True, WebhookDestination(args.url))])
        if args.command == "webhook"
        else None
    )
    async with XPushClient(auth_token=token, client_id=args.client_id) as client:
        stream = client.stream()
        if args.event_type:
            stream = stream.event_types(args.event_type)
        if args.contains:
            stream = stream.contains(args.contains)
        if router is not None:
            await router.run(stream)
        else:
            async for event in stream:
                print(
                    json.dumps(event.to_dict(include_raw=False), ensure_ascii=False),
                    flush=True,
                )


def main() -> None:
    args = _parser().parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        return
    except (XPushError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
