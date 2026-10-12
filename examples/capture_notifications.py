"""Opt-in live observation. Private payloads are never printed or packaged."""

import argparse
import asyncio
import json
import os
from collections.abc import AsyncIterable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from xpushstream import XPushClient, XPushEvent
from xpushstream.exceptions import RegistrationError, XPushError
from xpushstream.state import FileStateStore
from xpushstream.x_adapter import XPushRegistrar

ROOT = Path(__file__).resolve().parents[1]


def report_settings(
    stage: str, response: dict[str, object], expected: dict[str, str]
) -> bool:
    """Only display allowlisted settings; missing/unknown schema is unconfirmed."""
    settings = response.get("push_settings")
    matched = True
    for name, wanted in expected.items():
        actual = settings.get(name) if isinstance(settings, dict) else None
        confirmed = isinstance(actual, str) and actual == wanted
        matched = matched and confirmed
        print(
            json.dumps(
                {
                    "stage": stage,
                    "setting": name,
                    "expected": wanted,
                    "actual": actual
                    if isinstance(actual, str) and actual in {"anyone", "off"}
                    else "missing_or_unrecognized",
                    "result": "match" if confirmed else "unconfirmed_or_mismatch",
                }
            ),
            flush=True,
        )
    return matched


class SettingsCheckingRegistrar(XPushRegistrar):
    """Test-only registrar: inspect each save and read back without re-saving."""

    async def _post(
        self, client: httpx.AsyncClient, operation: str, device: dict[str, object]
    ) -> dict[str, object]:
        value = await super()._post(client, operation, device)
        if operation in {"login", "checkin"}:
            self._saved: set[str] = set()
            self._save_ok = True
            report_settings(f"initial_{operation}", value, self._settings)
        if operation == "save":
            requested = device.get("settings")
            if not isinstance(requested, dict):
                raise RegistrationError(
                    "Settings verification has no requested values."
                )
            expected = {
                name: wanted
                for name, wanted in self._settings.items()
                if name in requested
            }
            self._save_ok = report_settings("save", value, expected) and self._save_ok
            self._saved.update(expected)
            if self._saved == set(self._settings):
                # Call the base POST directly: checkin must not trigger another save.
                readback = await super()._post(
                    client,
                    "checkin",
                    {
                        k: v
                        for k, v in device.items()
                        if k not in {"settings", "checksum"}
                    },
                )
                readback_ok = report_settings(
                    "checkin_readback", readback, self._settings
                )
                if not (self._save_ok and readback_ok):
                    raise RegistrationError(
                        "Settings could not be verified from save/checkin responses. "
                        "See the allowlisted setting results above."
                    )
                print("Settings verified by save and checkin.", flush=True)
        return value


async def capture(
    events: AsyncIterable[XPushEvent], directory: Path, limit: int
) -> int:
    count = 0
    async for event in events:
        count += 1
        path = directory / f"{count:04d}.json"
        # The owner-only parent DACL is inherited on Windows; POSIX uses 0600.
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(event.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        print(f"Captured notification {count:04d}.", flush=True)
        if count >= limit:
            break
    return count


async def monitor(client: XPushClient) -> None:
    while True:
        await asyncio.sleep(30)
        status = client.status
        print(
            f"Status: connected={status.connected}, delivered={status.delivered}, "
            f"decrypt_failed={status.decryption_failures}, "
            f"processing_failed={status.processing_failures}, "
            f"reconnects={status.reconnects}.",
            flush=True,
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--client-id", default="live-observation")
    parser.add_argument(
        "--verify-settings",
        action="store_true",
        help="Verify save responses and read settings back with checkin before Ready.",
    )
    args = parser.parse_args()
    if args.seconds <= 0 or not 1 <= args.limit <= 1000:
        raise ValueError("Use positive seconds and a limit between 1 and 1000.")
    token = os.environ.get("XPUSHSTREAM_AUTH_TOKEN", "")
    if not token:
        raise ValueError("Set XPUSHSTREAM_AUTH_TOKEN in the environment.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = ROOT / "private" / "captures" / f"{stamp}-{uuid4().hex[:8]}"
    # Reuse the SDK's OS access controls rather than relying on Windows chmod.
    async with FileStateStore(directory).claim("capture"):
        print("Connecting to AutoPush and registering with X...", flush=True)
        async with XPushClient(
            auth_token=token,
            client_id=args.client_id,
            max_reconnect_attempts=5,
            registrar=SettingsCheckingRegistrar() if args.verify_settings else None,
        ) as client:
            print("Ready. Send test notifications now.", flush=True)
            print(f"Private capture directory: {directory}", flush=True)
            task = asyncio.create_task(monitor(client))
            try:
                await asyncio.wait_for(
                    capture(client, directory, args.limit), args.seconds
                )
            except asyncio.TimeoutError:
                print("Observation time limit reached.", flush=True)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Observation stopped.")
    except (XPushError, ValueError) as error:
        print(f"Observation failed: {error}", flush=True)
        raise SystemExit(1) from None
    except Exception:
        print("Observation failed; private details withheld.", flush=True)
        raise SystemExit(1) from None
