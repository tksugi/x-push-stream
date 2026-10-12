"""Published RFC/draft vectors plus independently produced authenticated records."""

from dataclasses import replace

import pytest

from xpushstream.autopush import Notification
from xpushstream.exceptions import DecryptionError
from xpushstream.webpush import PushKeys, decode64, decrypt, generate_keys

from .fakes import encrypted


def test_rfc8291_section5_and_appendix_a() -> None:
    keys = PushKeys(
        decode64("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"),
        decode64(
            "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcx"
            "aOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
        ),
        decode64("BTBZMqHH6r4Tts7J_aSIgg"),
    )
    body = decode64(
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
        "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
    ) + decode64(
        "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ"
    )
    assert decrypt(body, {"encoding": "aes128gcm"}, keys) == (
        b"When I grow up, I want to be a watermelon"
    )


@pytest.mark.parametrize("header_name", ["crypto-key", "crypto_key"])
def test_legacy_webpush_encryption_draft04_section5(header_name: str) -> None:
    keys = PushKeys(
        decode64("9FWl15_QUQAWDaD3k3l50ZBZQJ4au27F1V4F0uLSD_M"),
        decode64(
            "BCEkBjzL8Z3C-oi2Q7oE5t2Np-p7osjGLg93qUP0wvqR"
            "T21EEWyf0cQDQcakQMqz4hQKYOQ3il2nNZct4HgAUQU"
        ),
        decode64("R29vIGdvbyBnJyBqb29iIQ"),
    )
    assert (
        decrypt(
            decode64("6nqAQUME8hNqw5J3kl8cpVVJylXKYqZOeseZG8UueKpA"),
            Notification.parse(
                {
                    "channelID": "431b4391-c78f-429a-a134-f890b5adc0bb",
                    "version": "synthetic",
                    "headers": {
                        "encoding": "aesgcm",
                        "encryption": 'salt="lngarbyKfMoi9Z75xYXmkg"',
                        header_name: (
                            'dh="BNoRDbb84JGm8g5Z5CFxurSqsXWJ11ItfXEWYVLE85Y7'
                            'CYkDjXsIEc4aqxYaQ1G8BqkXCJ6DPpDrWtdWj_mugHU"'
                        ),
                    },
                }
            ).headers,
            keys,
        )
        == b"I am the walrus"
    )


@pytest.mark.parametrize(
    "content", [b"", b'{"title":"synthetic"}', "日本語".encode(), b"tail\0"]
)
def test_independent_sender_round_trip(content: bytes) -> None:
    keys = generate_keys()
    assert decrypt(encrypted(keys, content), {"encoding": "aes128gcm"}, keys) == content


@pytest.mark.parametrize(
    "fault", ["tag", "truncate", "padding", "auth", "sender", "record"]
)
def test_rejects_authenticated_record_failures(fault: str) -> None:
    keys = generate_keys()
    body = encrypted(keys, b"synthetic", delimiter=1 if fault == "padding" else 2)
    if fault == "tag":
        body = body[:-1] + bytes([body[-1] ^ 1])
    elif fault == "truncate":
        body = body[:80]
    elif fault == "auth":
        keys = replace(keys, auth_secret=b"\0" * 16)
    elif fault == "sender":
        body = body[:21] + b"\0" * 65 + body[86:]
    elif fault == "record":
        body = body[:16] + (len(body) - 86).to_bytes(4, "big") + body[20:]
    with pytest.raises(DecryptionError, match="decryption failed"):
        decrypt(body, {"encoding": "aes128gcm"}, keys)


@pytest.mark.parametrize("value", ["!", "x x", "é", "a", "abc==="])
def test_base64url_is_validated(value: str) -> None:
    with pytest.raises(ValueError):
        decode64(value)
