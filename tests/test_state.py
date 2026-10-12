"""Check storage integrity, crash-safe replacement, and real process ownership."""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from xpushstream.exceptions import PersistentClientInUseError, StateError
from xpushstream.state import FileStateStore, MemoryStateStore, PushSubscriptionState

from .fakes import make_state


def test_state_round_trip_and_private_repr() -> None:
    state = make_state()
    assert PushSubscriptionState.from_dict(state.to_dict()) == state
    assert state.endpoint not in repr(state)
    assert state.credential_fingerprint not in repr(state)
    assert state.keys.private_key.hex() not in repr(state.keys)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("uaid", "invalid"),
        ("endpoint", "http://push.invalid"),
        ("private_key", "invalid"),
        ("registered", "yes"),
        ("public_key", "bad"),
    ],
)
def test_corrupt_state_does_not_silently_reset(
    field: str, value: object, tmp_path: Path
) -> None:
    async def run() -> None:
        store = FileStateStore(tmp_path / "state")
        async with store.claim("bot"):
            state = make_state().to_dict()
            state[field] = value
            path = store._path("bot")
            path.write_text(json.dumps(state), encoding="utf-8")
            with pytest.raises(StateError):
                await store.load("bot")
            assert path.exists()

    asyncio.run(run())


def test_file_resume_delete_and_private_access(tmp_path: Path) -> None:
    async def run() -> None:
        directory = tmp_path / "state"
        first = FileStateStore(directory)
        state = make_state()
        async with first.claim("../bot"):
            assert await first.load("../bot") is None
            await first.save("../bot", state)
        second = FileStateStore(directory)
        async with second.claim("../bot"):
            assert await second.load("../bot") == state
            path = second._path("../bot")
            assert path.parent == directory
            assert "synthetic-token" not in path.read_text()
            if os.name != "nt":
                assert path.stat().st_mode & 0o777 == 0o600
                assert directory.stat().st_mode & 0o777 == 0o700
            await second.delete("../bot")
            assert await second.load("../bot") is None
        assert not list(directory.glob(".state-*"))

    asyncio.run(run())


@pytest.mark.parametrize("in_memory", [False, True])
def test_same_id_is_exclusive_and_other_id_can_run(
    tmp_path: Path, in_memory: bool
) -> None:
    async def run() -> None:
        store = MemoryStateStore() if in_memory else FileStateStore(tmp_path / "state")
        async with store.claim("one"):
            with pytest.raises(PersistentClientInUseError):
                async with store.claim("one"):
                    pass
            async with store.claim("two"):
                await store.save("two", make_state())
        async with store.claim("one"):
            assert await store.load("one") is None

    asyncio.run(run())


def test_os_lock_blocks_another_process_only_for_the_same_id(tmp_path: Path) -> None:
    code = """
import asyncio, sys
from xpushstream.state import FileStateStore
from xpushstream.exceptions import PersistentClientInUseError
async def run():
    try:
        async with FileStateStore(sys.argv[1]).claim(sys.argv[2]):
            pass
    except PersistentClientInUseError:
        raise SystemExit(3)
asyncio.run(run())
"""

    async def run() -> None:
        directory = tmp_path / "state"
        async with FileStateStore(directory).claim("one"):
            for name, expected in (("one", 3), ("two", 0)):
                result = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-c", code, str(directory), name],
                    capture_output=True,
                    timeout=10,
                )
                assert result.returncode == expected, result.stderr.decode()

    asyncio.run(run())


def test_atomic_write_failure_preserves_original_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        store = FileStateStore(tmp_path / "state")
        state = make_state()
        async with store.claim("bot"):
            await store.save("bot", state)

            def fail_replace(source: object, target: object) -> None:
                raise OSError("synthetic write failure")

            monkeypatch.setattr(os, "replace", fail_replace)
            with pytest.raises(StateError):
                await store.save("bot", replace(state, registered=True))
            assert await store.load("bot") == state
            assert not list(store.directory.glob(".state-*"))

    asyncio.run(run())


def test_operations_require_claim(tmp_path: Path) -> None:
    with pytest.raises(StateError, match="claim"):
        asyncio.run(FileStateStore(tmp_path).load("bot"))


def test_oversized_state_is_rejected_before_replacing_existing_file(tmp_path):
    async def run():
        store, state = FileStateStore(tmp_path / "state"), make_state()
        async with store.claim("bot"):
            await store.save("bot", state)
            oversized = replace(state, endpoint="https://push.invalid/" + "x" * 65536)
            with pytest.raises(StateError, match="size limit"):
                await store.save("bot", oversized)
            assert await store.load("bot") == state
            assert not list(store.directory.glob(".state-*"))

    asyncio.run(run())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DACL verification")
def test_windows_state_has_a_protected_owner_only_dacl(tmp_path: Path):
    import ctypes
    from ctypes import wintypes

    def dacl(path: Path) -> str:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        get = advapi.GetFileSecurityW
        get.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get.restype = wintypes.BOOL
        needed = wintypes.DWORD()
        get(str(path), 4, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        assert get(str(path), 4, buffer, needed, ctypes.byref(needed))
        convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
        convert.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.c_void_p,
        ]
        convert.restype = wintypes.BOOL
        result = wintypes.LPWSTR()
        assert convert(buffer, 1, 4, ctypes.byref(result), None)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        try:
            return result.value
        finally:
            kernel.LocalFree(result)

    async def run():
        store = FileStateStore(tmp_path / "state")
        async with store.claim("bot"):
            await store.save("bot", make_state())
            assert dacl(store._path("bot")) == "D:P(A;;FA;;;OW)"
            assert dacl(store.directory) == "D:P(A;OICI;FA;;;OW)"

    asyncio.run(run())
