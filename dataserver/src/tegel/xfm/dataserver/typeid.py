"""TypeID (spec v0.3) encoding, decoding, and deterministic derivation.

A TypeID is ``<prefix>_<suffix>``: a lowercase ``[a-z_]`` prefix (at most 63
characters, starting and ending with a letter) and a 26-character suffix that
encodes a 128-bit UUID in Crockford base32 (lowercase, no i/l/o/u). See
https://github.com/jetify-com/typeid.

XFM measurement ids are derived, not minted: the UUID is a version-5 UUID of the
crate's folder name, so the same crate gets the same id at every catalog rebuild
without anything being written into the data.
"""

from __future__ import annotations

import re
import uuid

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_DECODE = {char: index for index, char in enumerate(ALPHABET)}
_PREFIX = re.compile(r"^(?:[a-z]|[a-z][a-z_]{0,61}[a-z])$")
_SUFFIX = re.compile(r"^[0-7][0-9a-hjkmnp-tv-z]{25}$")

PREFIX = "xfm"
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://w3id.org/tegel/xfm/processed")


def encode(prefix: str, value: uuid.UUID) -> str:
    """Return the TypeID for ``prefix`` and a UUID of any version."""
    if prefix and not _PREFIX.match(prefix):
        raise ValueError(f"invalid TypeID prefix: {prefix!r}")
    number = value.int
    chars = [ALPHABET[(number >> (5 * shift)) & 31] for shift in range(25, -1, -1)]
    suffix = "".join(chars)
    return f"{prefix}_{suffix}" if prefix else suffix


def decode(text: str) -> tuple[str, uuid.UUID]:
    """Split a TypeID into its prefix and UUID. Raises ``ValueError`` when invalid."""
    prefix, _, suffix = text.rpartition("_")
    if prefix and not _PREFIX.match(prefix):
        raise ValueError(f"invalid TypeID prefix in {text!r}")
    if not _SUFFIX.match(suffix):
        raise ValueError(f"invalid TypeID suffix in {text!r}")
    number = 0
    for char in suffix:
        number = (number << 5) | _DECODE[char]
    return prefix, uuid.UUID(int=number)


def is_typeid(text: str, prefix: str = PREFIX) -> bool:
    """Whether ``text`` is a valid TypeID with exactly ``prefix``."""
    try:
        found, _ = decode(text)
    except ValueError:
        return False
    return found == prefix


def derive(name: str, prefix: str = PREFIX) -> str:
    """The stable TypeID of a measurement named ``name`` (UUIDv5 in the XFM namespace)."""
    if not name:
        raise ValueError("a TypeID cannot be derived from an empty name")
    return encode(prefix, uuid.uuid5(NAMESPACE, name))
