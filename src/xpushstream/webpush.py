"""Independent RFC 8291 composition and legacy draft-04 aesgcm decoding."""

import base64
import binascii
import os
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .exceptions import DecryptionError

MAX_PAYLOAD = 1_048_576


def encode64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode64(value: str) -> bytes:
    if len(value) > MAX_PAYLOAD * 2 or not re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", value):
        raise ValueError("Invalid base64url data.")
    return base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )


@dataclass(frozen=True, repr=False)
class PushKeys:
    private_key: bytes
    public_key: bytes
    auth_secret: bytes

    def validate(self) -> None:
        if (
            len(self.private_key) != 32
            or len(self.public_key) != 65
            or len(self.auth_secret) != 16
        ):
            raise ValueError("Invalid Web Push key lengths.")
        private = ec.derive_private_key(
            int.from_bytes(self.private_key, "big"), ec.SECP256R1()
        )
        public = private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        if public != self.public_key:
            raise ValueError("Web Push public and private keys do not match.")


def generate_keys() -> PushKeys:
    private = ec.generate_private_key(ec.SECP256R1())
    return PushKeys(
        private.private_numbers().private_value.to_bytes(32, "big"),
        private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        ),
        os.urandom(16),
    )


def _derive(secret: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(
        secret
    )


def _shared(keys: PushKeys, sender: bytes) -> bytes:
    keys.validate()
    if len(sender) != 65 or sender[0] != 4:
        raise ValueError("Invalid sender key.")
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender)
    private = ec.derive_private_key(
        int.from_bytes(keys.private_key, "big"), ec.SECP256R1()
    )
    return private.exchange(ec.ECDH(), public)


def _parameter(header: str, name: str) -> str:
    # aesgcm permits one encryption entry and at most one dh parameter.
    values = re.findall(
        r"(?:^|[;,])\s*" + re.escape(name) + r'\s*=\s*(?:"([^"]*)"|([^;,\s]+))', header
    )
    if len(values) != 1:
        raise ValueError("Missing or ambiguous encryption header parameter.")
    return str(values[0][0] or values[0][1])


def decrypt(data: bytes, headers: Mapping[str, str], keys: PushKeys) -> bytes:
    """Authenticate a single Web Push record; reject truncation and bad padding."""
    try:
        if not data or len(data) > MAX_PAYLOAD:
            raise ValueError("Invalid ciphertext length.")
        fields = {name.lower(): value for name, value in headers.items()}
        encoding = fields.get("encoding", fields.get("content-encoding", ""))
        if encoding == "aes128gcm":
            if len(data) < 21:
                raise ValueError("Truncated encryption header.")
            salt, record_size, key_length = struct.unpack("!16sIB", data[:21])
            if key_length != 65 or len(data) < 21 + key_length + 17:
                raise ValueError("Invalid encryption record.")
            sender, ciphertext = data[21:86], data[86:]
            # RFC 8291 restricts Web Push to one record, including the last delimiter.
            if record_size < 18 or len(ciphertext) >= record_size:
                raise ValueError("Invalid Web Push record size.")
            shared = _shared(keys, sender)
            ikm = _derive(
                shared,
                keys.auth_secret,
                b"WebPush: info\x00" + keys.public_key + sender,
                32,
            )
            cek = _derive(ikm, salt, b"Content-Encoding: aes128gcm\x00", 16)
            nonce = _derive(ikm, salt, b"Content-Encoding: nonce\x00", 12)
            plaintext = AESGCM(cek).decrypt(nonce, ciphertext, None).rstrip(b"\x00")
            if not plaintext or plaintext[-1] != 2:
                raise ValueError("Invalid final record delimiter.")
            return plaintext[:-1]
        if encoding == "aesgcm":
            encryption = fields.get("encryption", "")
            if "," in encryption:
                raise ValueError("Multiple encryption entries are unsupported.")
            salt = decode64(_parameter(encryption, "salt"))
            sender = decode64(_parameter(fields.get("crypto-key", ""), "dh"))
            record_size = (
                int(_parameter(encryption, "rs"))
                if re.search(r"\brs\s*=", encryption)
                else 4096
            )
            if len(salt) != 16 or record_size < 3 or len(data) >= record_size + 16:
                raise ValueError("Invalid legacy record size.")
            ikm = _derive(
                _shared(keys, sender),
                keys.auth_secret,
                b"Content-Encoding: auth\x00",
                32,
            )
            context = (
                b"P-256\x00"
                + struct.pack("!H", 65)
                + keys.public_key
                + struct.pack("!H", 65)
                + sender
            )
            cek = _derive(ikm, salt, b"Content-Encoding: aesgcm\x00" + context, 16)
            nonce = _derive(ikm, salt, b"Content-Encoding: nonce\x00" + context, 12)
            plaintext = AESGCM(cek).decrypt(nonce, data, None)
            if len(plaintext) < 2:
                raise ValueError("Missing legacy padding length.")
            padding = int.from_bytes(plaintext[:2], "big")
            if 2 + padding > len(plaintext) or any(plaintext[2 : 2 + padding]):
                raise ValueError("Invalid legacy padding.")
            return plaintext[2 + padding :]
        raise ValueError("Unsupported content encoding.")
    except (ValueError, InvalidTag, binascii.Error, OverflowError, TypeError):
        raise DecryptionError("Web Push decryption failed.") from None
