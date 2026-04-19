"""Open a Telegram-macOS Postbox SQLCipher database read-only."""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .keyderiv import TempKey

try:
    _sqlcipher = importlib.import_module("sqlcipher3")
except ImportError:
    try:
        _sqlcipher = importlib.import_module("pysqlcipher3.dbapi2")
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: sqlcipher3-binary (macOS) or pysqlcipher3."
        ) from exc


@dataclass(frozen=True)
class CipherProfile:
    """A SQLCipher configuration profile to try in order."""

    name: str
    compat: Optional[int]
    pragmas: dict[str, str | int]
    compat_before_key: bool = False


def default_profiles() -> list[CipherProfile]:
    """Profiles ordered by how likely they match Telegram-macOS Postbox."""
    return [
        CipherProfile(
            "telegram-macos-rawkey",
            4,
            {
                "kdf_iter": 1,
                "cipher_hmac_algorithm": "HMAC_SHA512",
                "cipher_kdf_algorithm": "PBKDF2_HMAC_SHA512",
                "cipher_plaintext_header_size": 32,
                "cipher_default_plaintext_header_size": 32,
            },
            False,
        ),
        CipherProfile(
            "sqlcipher4-rawkey-hmac",
            4,
            {
                "kdf_iter": 1,
                "cipher_hmac_algorithm": "HMAC_SHA512",
                "cipher_kdf_algorithm": "PBKDF2_HMAC_SHA512",
            },
            False,
        ),
        CipherProfile("sqlcipher4-default", 4, {}, False),
        CipherProfile(
            "sqlcipher4-legacy",
            4,
            {"cipher_page_size": 4096},
            True,
        ),
        CipherProfile("sqlcipher3-default", 3, {}, False),
    ]


def _apply_pragmas(conn, pragmas: dict[str, str | int]) -> None:
    for name, value in pragmas.items():
        conn.execute(f"PRAGMA {name} = {value}")


def _try_open(db_path: Path, key_hex: str, profile: CipherProfile):
    conn = _sqlcipher.connect(str(db_path))
    if profile.compat is not None and profile.compat_before_key:
        conn.execute(f"PRAGMA cipher_compatibility = {profile.compat}")
    if profile.pragmas:
        _apply_pragmas(conn, profile.pragmas)
    conn.execute(f"PRAGMA key=\"x'{key_hex}'\"")
    if profile.compat is not None and not profile.compat_before_key:
        conn.execute(f"PRAGMA cipher_compatibility = {profile.compat}")
    db_error = getattr(_sqlcipher, "DatabaseError", Exception)
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn, profile
    except db_error:
        conn.close()
        return None


@contextmanager
def open_postbox(
    db_path: Path,
    tempkey: TempKey,
    profiles: Optional[list[CipherProfile]] = None,
) -> Iterator[tuple[object, CipherProfile]]:
    """Open the Postbox DB with one of the candidate SQLCipher profiles."""
    if profiles is None:
        profiles = default_profiles()

    last_error: Optional[Exception] = None
    for profile in profiles:
        try:
            result = _try_open(db_path, tempkey.sqlcipher_key_hex, profile)
        except Exception as exc:  # noqa: BLE001 — keep trying next profile
            last_error = exc
            continue
        if result is not None:
            conn, matched = result
            try:
                yield conn, matched
            finally:
                conn.close()
            return

    msg = f"Failed to open {db_path}: no SQLCipher profile matched."
    if last_error is not None:
        msg += f" Last error: {last_error!r}"
    raise SQLCipherOpenError(msg)


class SQLCipherOpenError(RuntimeError):
    """Raised when no configured profile successfully opens the DB."""
