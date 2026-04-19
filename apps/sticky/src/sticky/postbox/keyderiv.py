"""Derive the SQLCipher key for Telegram-macOS's Postbox DB.

Telegram-macOS stores a `.tempkeyEncrypted` file in the account directory.
When the user has not set a local passcode, the file is AES-CBC encrypted
with a key derived from the literal passcode "no-matter-key". After
decryption it contains:

    [db_key: 32 bytes][db_salt: 16 bytes][murmur_verify: int32 LE][padding]

where murmur_verify = mmh3(db_key + db_salt, seed=0xF7CA7FD2, signed=True).

See stek29's gist and telegram-message-exporter for the reference
implementation this module is adapted from.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from Cryptodome.Cipher import AES

from .hashing import TEMPKEY_MURMUR_SEED, murmur_hash

DEFAULT_PASSCODE = b"no-matter-key"


@dataclass(frozen=True)
class TempKey:
    """Decrypted tempkey material."""

    db_key: bytes  # 32 bytes
    db_salt: bytes  # 16 bytes

    @property
    def sqlcipher_key_hex(self) -> str:
        """SQLCipher raw-key hex representation (db_key || db_salt)."""
        return (self.db_key + self.db_salt).hex()


def _tempkey_kdf(passcode: bytes) -> tuple[bytes, bytes]:
    digest = hashlib.sha512(passcode).digest()
    return digest[:32], digest[-16:]


def _decrypt_tempkey(encrypted: bytes, passcode: bytes) -> Optional[TempKey]:
    if len(encrypted) != 64 or len(encrypted) % 16 != 0:
        return None

    aes_key, aes_iv = _tempkey_kdf(passcode)
    data = AES.new(aes_key, AES.MODE_CBC, aes_iv).decrypt(encrypted)
    if len(data) < 52:
        return None

    db_key = data[:32]
    db_salt = data[32:48]
    expected_hash = int.from_bytes(data[48:52], "little", signed=True)

    if murmur_hash(db_key + db_salt, TEMPKEY_MURMUR_SEED) != expected_hash:
        return None

    return TempKey(db_key=db_key, db_salt=db_salt)


def derive_tempkey(
    tempkey_path: Path,
    passcodes: Iterable[bytes] = (DEFAULT_PASSCODE,),
) -> TempKey:
    """Read and decrypt the `.tempkeyEncrypted` file.

    Raises `PasscodeRequired` if the file can't be decrypted with any of the
    supplied passcodes — this is the signal that the user has enabled a
    Telegram-macOS local passcode and we cannot proceed.
    """
    encrypted = tempkey_path.read_bytes()
    for passcode in passcodes:
        tempkey = _decrypt_tempkey(encrypted, passcode)
        if tempkey is not None:
            return tempkey
    raise PasscodeRequired(
        f"Could not decrypt {tempkey_path}. A Telegram-macOS local passcode "
        "is likely set — disable it to use the sticky agent, or use the "
        "forward-stickers flow instead."
    )


class PasscodeRequired(RuntimeError):
    """Raised when `.tempkeyEncrypted` cannot be decrypted without a passcode."""
