"""TypeID (spec v0.3) minting for scan crates: ``xfm_`` + base32 UUIDv7."""

from __future__ import annotations

import os
import time
import uuid

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
PREFIX = "xfm"


def uuid7(timestamp_ms: int | None = None) -> uuid.UUID:
    """A version-7 UUID (RFC 9562): 48-bit Unix milliseconds, then random bits."""
    ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return uuid.UUID(int=value)


def encode(prefix: str, value: uuid.UUID) -> str:
    number = value.int
    suffix = "".join(ALPHABET[(number >> (5 * shift)) & 31] for shift in range(25, -1, -1))
    return f"{prefix}_{suffix}"


def mint(prefix: str = PREFIX) -> str:
    """A new, time-ordered TypeID."""
    return encode(prefix, uuid7())
