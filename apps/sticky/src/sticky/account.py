"""Locate Telegram-macOS account directories and their Postbox DBs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

TELEGRAM_CONTAINER = Path.home() / (
    "Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/stable"
)

TEMPKEY_NAME = ".tempkeyEncrypted"
POSTBOX_SUBPATH = Path("postbox/db/db_sqlite")


@dataclass(frozen=True)
class TelegramAccount:
    """One Telegram-macOS signed-in account."""

    account_dir: Path

    @property
    def tempkey_path(self) -> Path:
        return self.account_dir.parent / TEMPKEY_NAME

    @property
    def db_path(self) -> Path:
        return self.account_dir / POSTBOX_SUBPATH

    @property
    def media_cache_dir(self) -> Path:
        return self.account_dir / "postbox/media/cache"

    @property
    def display_id(self) -> str:
        """The numeric suffix used to distinguish multi-account installs."""
        name = self.account_dir.name
        return name.removeprefix("account-")


def discover_accounts(container: Path = TELEGRAM_CONTAINER) -> list[TelegramAccount]:
    """Return every Telegram-macOS account directory under the container."""
    if not container.exists():
        return []
    accounts: list[TelegramAccount] = []
    for entry in sorted(container.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("account-"):
            continue
        if not (entry / POSTBOX_SUBPATH).exists():
            continue
        accounts.append(TelegramAccount(entry))
    return accounts


def resolve_account(account_dir: Optional[str]) -> TelegramAccount:
    """Resolve a user-supplied path, a display_id, or auto-detect a single account."""
    if account_dir is not None:
        path = Path(account_dir).expanduser()
        if path.exists():
            return TelegramAccount(path)
        for candidate in discover_accounts():
            if candidate.display_id == account_dir:
                return candidate
        raise FileNotFoundError(f"Account directory does not exist: {account_dir}")

    accounts = discover_accounts()
    if not accounts:
        raise FileNotFoundError(
            "No Telegram-macOS accounts found under "
            f"{TELEGRAM_CONTAINER} — is Telegram-macOS installed and signed in?"
        )
    if len(accounts) == 1:
        return accounts[0]
    options = "\n  ".join(f"- {a.account_dir}" for a in accounts)
    raise RuntimeError(
        "Multiple Telegram accounts detected; re-run with `--account <dir>`:\n  "
        + options
    )
