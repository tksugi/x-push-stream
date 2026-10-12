"""The live observer is testable without contacting X or using credentials."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from examples.capture_notifications import SettingsCheckingRegistrar, capture
from xpushstream import UnknownNotification
from xpushstream.exceptions import RegistrationError
from xpushstream.state import FileStateStore

from .fakes import make_state


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    "fault", [None, "save", "checkin", "missing", "unexpected", "shape"]
)
def test_settings_verifies_saves_and_readback_without_leaking_data(
    resume, fault, capsys
):
    calls = []
    expected = {
        "LikesNonVitSetting": "anyone",
        "MentionsSetting": "anyone",
        "RetweetsSetting": "anyone",
    }

    class Ct0:
        async def acquire(self, token):
            return "synthetic-ct0"

    def respond(request):
        operation = request.url.path.rsplit("/", 1)[-1]
        device = json.loads(request.content)["push_device_info"]
        calls.append((operation, device))
        settings = expected.copy()
        if len(calls) == 1:
            # Initial settings may be off; this must not prevent the later saves.
            settings = {name: "off" for name in expected}
        if fault == "save" and operation == "save.json":
            settings["LikesNonVitSetting"] = "off"
        if len(calls) == 5:
            if fault == "checkin":
                settings["LikesNonVitSetting"] = "off"
            elif fault == "missing":
                settings.pop("LikesNonVitSetting")
            elif fault == "unexpected":
                settings["LikesNonVitSetting"] = "synthetic-private-response"
            elif fault == "shape":
                settings = {"settings": settings}
        return httpx.Response(
            200,
            json={
                "push_settings_template": {"checksum": "synthetic-private-checksum"},
                "push_settings": settings,
                "private": "synthetic-private-response",
            },
        )

    registrar = SettingsCheckingRegistrar(
        ct0_provider=Ct0(), transport=httpx.MockTransport(respond)
    )

    async def run():
        action = registrar.apply_settings if resume else registrar.register
        await action(make_state(), "synthetic-token")

    if fault is None:
        asyncio.run(run())
    else:
        with pytest.raises(RegistrationError, match="could not be verified"):
            asyncio.run(run())
    assert [operation for operation, _ in calls] == [
        "checkin.json" if resume else "login.json",
        "save.json",
        "save.json",
        "save.json",
        "checkin.json",
    ]
    assert "settings" not in calls[-1][1]
    assert "checksum" not in calls[-1][1]
    output = capsys.readouterr().out
    assert "checkin_readback" in output
    assert ("Settings verified" in output) == (fault is None)
    assert "synthetic-private-response" not in output
    assert "synthetic-private-checksum" not in output
    assert "synthetic-token" not in output


def test_capture_preserves_variant_payloads_limits_count_and_withholds_content(
    tmp_path: Path, capsys
) -> None:
    payloads = [{"data": {"type": "synthetic", "nested": [1]}}, ["private"], None]

    async def events():
        for raw in payloads:
            yield UnknownNotification(raw=raw, body="synthetic-private-content")

    async def run():
        async with FileStateStore(tmp_path).claim("capture"):
            assert await capture(events(), tmp_path, 2) == 2

    asyncio.run(run())
    paths = sorted(tmp_path.glob("*.json"))
    assert [json.loads(path.read_text(encoding="utf-8"))["raw"] for path in paths] == (
        payloads[:2]
    )
    assert "synthetic-private-content" not in capsys.readouterr().out
