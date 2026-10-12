"""Atomic subscription storage and OS-backed ownership for each logical client."""

import hashlib
import json
import os
import stat
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from platformdirs import user_data_path, user_state_path

from .exceptions import PersistentClientInUseError, StateError
from .webpush import PushKeys, decode64, encode64


@dataclass(frozen=True, repr=False)
class PushSubscriptionState:
    uaid: str
    channel_id: str
    endpoint: str
    keys: PushKeys
    credential_fingerprint: str
    device_id: str
    registered: bool = False
    schema_version: int = 1

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported subscription schema.")
        UUID(self.uaid)
        UUID(self.channel_id)
        try:
            endpoint = urlsplit(self.endpoint)
            _ = endpoint.port
        except ValueError:
            raise ValueError("Invalid push endpoint.") from None
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.fragment
        ):
            raise ValueError("Invalid push endpoint.")
        if len(self.credential_fingerprint) != 64 or any(
            c not in "0123456789abcdef" for c in self.credential_fingerprint
        ):
            raise ValueError("Invalid credential fingerprint.")
        if (
            not self.device_id
            or len(self.device_id) > 256
            or not isinstance(self.registered, bool)
        ):
            raise ValueError("Invalid subscription metadata.")
        self.keys.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "uaid": self.uaid,
            "channel_id": self.channel_id,
            "endpoint": self.endpoint,
            "private_key": encode64(self.keys.private_key),
            "public_key": encode64(self.keys.public_key),
            "auth_secret": encode64(self.keys.auth_secret),
            "credential_fingerprint": self.credential_fingerprint,
            "device_id": self.device_id,
            "registered": self.registered,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "PushSubscriptionState":
        if not isinstance(value, dict):
            raise ValueError("State must be an object.")
        names = {
            "uaid",
            "channel_id",
            "endpoint",
            "private_key",
            "public_key",
            "auth_secret",
            "credential_fingerprint",
            "device_id",
            "registered",
            "schema_version",
        }
        if set(value) != names:
            raise ValueError("Invalid state fields.")
        if not all(
            isinstance(value[name], str)
            for name in names - {"registered", "schema_version"}
        ):
            raise ValueError("Invalid state types.")
        if (
            type(value["schema_version"]) is not int
            or type(value["registered"]) is not bool
        ):
            raise ValueError("Invalid state metadata.")
        state = cls(
            uaid=value["uaid"],
            channel_id=value["channel_id"],
            endpoint=value["endpoint"],
            keys=PushKeys(
                decode64(value["private_key"]),
                decode64(value["public_key"]),
                decode64(value["auth_secret"]),
            ),
            credential_fingerprint=value["credential_fingerprint"],
            device_id=value["device_id"],
            registered=value["registered"],
            schema_version=value["schema_version"],
        )
        state.validate()
        return state


class StateStore(Protocol):
    def claim(self, client_id: str) -> AbstractAsyncContextManager[None]: ...
    async def load(self, client_id: str) -> PushSubscriptionState | None: ...
    async def save(self, client_id: str, state: PushSubscriptionState) -> None: ...
    async def delete(self, client_id: str) -> None: ...


def credential_fingerprint(auth_token: str) -> str:
    return hashlib.sha256(auth_token.encode("utf-8")).hexdigest()


def _client_key(client_id: str) -> str:
    if not client_id or len(client_id) > 1024:
        raise StateError("client_id must contain between 1 and 1024 characters.")
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()


def _private_access(path: Path, *, directory: bool = False) -> None:
    if sys.platform != "win32":
        path.chmod(0o700 if directory else 0o600)
        return
    import ctypes
    from ctypes import wintypes

    # Protected owner-only DACL, inherited by children of the state directory.
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    convert.restype = wintypes.BOOL
    apply = advapi.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    sddl = "D:P(A;OICI;FA;;;OW)" if directory else "D:P(A;;FA;;;OW)"
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise StateError("Could not construct private state access controls.")
    try:
        if not apply(str(path), 0x80000004, descriptor):
            raise StateError("Could not protect state access controls.")
    finally:
        kernel.LocalFree(descriptor)


def _reject_link(path: Path) -> None:
    if path.is_symlink() or (
        path.exists()
        and getattr(path.lstat(), "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ):
        raise StateError("State paths must not be symbolic links or reparse points.")


class FileStateStore:
    """Private JSON files; locks survive until claim exits and release on process exit.

    All operations require a claim. Lock files remain on disk, but their existence
    does not mean they are held. Only the OS lock determines ownership.
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        default = (
            user_state_path("xpushstream", appauthor=False)
            if sys.platform.startswith("linux")
            else user_data_path("XPushStream", appauthor=False, roaming=False)
        )
        self.directory = Path(directory) if directory is not None else default
        self._held: set[str] = set()

    def _path(self, client_id: str) -> Path:
        key = _client_key(client_id)
        if key not in self._held:
            raise StateError("Acquire the client state claim before accessing state.")
        path = self.directory / f"{key}.json"
        _reject_link(path)
        return path

    @asynccontextmanager
    async def claim(self, client_id: str) -> AsyncIterator[None]:
        key = _client_key(client_id)
        if key in self._held:
            raise PersistentClientInUseError("Persistent client is already in use.")
        descriptor: int | None = None
        locked = False
        try:
            _reject_link(self.directory)
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _private_access(self.directory, directory=True)
            path = self.directory / f"{key}.lock"
            _reject_link(path)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            _private_access(path)
            try:
                if sys.platform == "win32":
                    import msvcrt

                    if os.fstat(descriptor).st_size == 0:
                        os.write(descriptor, b"\x00")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise PersistentClientInUseError(
                    "Persistent client is already in use."
                ) from None
            locked = True
            self._held.add(key)
            yield
        except OSError:
            if locked:
                raise
            raise StateError("Could not access private client state.") from None
        finally:
            if descriptor is not None:
                try:
                    if locked:
                        self._held.discard(key)
                        if sys.platform == "win32":
                            import msvcrt

                            os.lseek(descriptor, 0, os.SEEK_SET)
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    async def load(self, client_id: str) -> PushSubscriptionState | None:
        path = self._path(client_id)
        try:
            if not path.exists():
                return None
            _private_access(path)
            if path.stat().st_size > 65536:
                raise StateError("State file exceeds its size limit.")
            return PushSubscriptionState.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError, RecursionError):
            raise StateError(
                "Persistent state is unreadable, corrupt, or unsupported."
            ) from None

    async def save(self, client_id: str, state: PushSubscriptionState) -> None:
        path = self._path(client_id)
        temporary: Path | None = None
        try:
            payload = json.dumps(state.to_dict(), separators=(",", ":"))
            if len(payload.encode("utf-8")) > 65536:
                raise StateError("State exceeds its size limit.")
            descriptor, name = tempfile.mkstemp(
                dir=self.directory, prefix=".state-", suffix=".tmp"
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                _private_access(temporary)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except (OSError, ValueError, TypeError):
            raise StateError("Could not save private subscription state.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def delete(self, client_id: str) -> None:
        try:
            self._path(client_id).unlink(missing_ok=True)
        except OSError:
            raise StateError("Could not reset persistent state.") from None


class MemoryStateStore:
    """An in-process store for embedding and tests; no cross-process persistence."""

    def __init__(self) -> None:
        self._states: dict[str, PushSubscriptionState] = {}
        self._held: set[str] = set()

    @asynccontextmanager
    async def claim(self, client_id: str) -> AsyncIterator[None]:
        key = _client_key(client_id)
        if key in self._held:
            raise PersistentClientInUseError("Persistent client is already in use.")
        self._held.add(key)
        try:
            yield
        finally:
            self._held.remove(key)

    def _key(self, client_id: str) -> str:
        key = _client_key(client_id)
        if key not in self._held:
            raise StateError("Acquire the client state claim before accessing state.")
        return key

    async def load(self, client_id: str) -> PushSubscriptionState | None:
        return self._states.get(self._key(client_id))

    async def save(self, client_id: str, state: PushSubscriptionState) -> None:
        try:
            state.validate()
            self._states[self._key(client_id)] = replace(state)
        except ValueError:
            raise StateError("Invalid subscription state.") from None

    async def delete(self, client_id: str) -> None:
        self._states.pop(self._key(client_id), None)
