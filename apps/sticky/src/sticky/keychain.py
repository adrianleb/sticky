"""Store the long-lived pairing JWT in the macOS Keychain."""

from __future__ import annotations

from typing import Optional

import keyring

SERVICE = "app.sticky.agent"
KEY = "pairing-jwt"


def save_jwt(token: str) -> None:
    keyring.set_password(SERVICE, KEY, token)


def load_jwt() -> Optional[str]:
    return keyring.get_password(SERVICE, KEY)


def clear_jwt() -> None:
    try:
        keyring.delete_password(SERVICE, KEY)
    except keyring.errors.PasswordDeleteError:
        pass
