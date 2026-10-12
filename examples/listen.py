"""Read a token from the environment; never place session credentials in source."""

import asyncio
import os

from xpushstream import XPushClient


async def main() -> None:
    async with XPushClient(
        auth_token=os.environ["XPUSHSTREAM_AUTH_TOKEN"],
        client_id="notification-bot",
    ) as client:
        async for event in client:
            print(event.to_dict(include_raw=False))


if __name__ == "__main__":
    asyncio.run(main())
