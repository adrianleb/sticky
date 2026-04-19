"""Murmur3 hashing helpers used by Postbox key derivation and type dispatch."""

from __future__ import annotations

from typing import Final

import mmh3

TEMPKEY_MURMUR_SEED: Final[int] = 0xF7CA7FD2


def murmur_hash(data: bytes, seed: int = TEMPKEY_MURMUR_SEED) -> int:
    """Signed 32-bit Murmur3 hash."""
    return mmh3.hash(data, seed=seed, signed=True)


def murmur_hash_bytes(data: bytes, seed: int) -> bytes:
    """Raw Murmur3 hash bytes."""
    return mmh3.hash_bytes(data, seed=seed)
