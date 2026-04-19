"""Enumerate Telegram-macOS's on-disk sticker cache.

Postbox's `media/cache` directory contains decrypted sticker bodies keyed by
their document id. We index {document_id → local_path} so a future desktop
companion can render instantly without a network fetch. This index is
**kept on-device** and is never uploaded — the plan's privacy promise.

Document IDs land in the cache filename one of several ways depending on
app version; we probe the two layouts we've seen: `<id>` and `<id>.webp`.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Optional

from .account import TelegramAccount

CACHE_DIRNAME = "media/cache"
_FILENAME_ID = re.compile(r"^(?P<id>-?\d+)(?P<ext>\..+)?$")


@dataclass(frozen=True)
class CacheEntry:
    document_id: int
    path: str
    size: int


def cache_dir(account: TelegramAccount) -> Path:
    return account.account_dir / "postbox" / CACHE_DIRNAME


def iter_cache(account: TelegramAccount) -> Iterator[CacheEntry]:
    root = cache_dir(account)
    if not root.exists():
        return
    for entry in root.rglob("*"):
        if not entry.is_file():
            continue
        m = _FILENAME_ID.match(entry.name)
        if not m:
            continue
        try:
            doc_id = int(m.group("id"))
        except ValueError:
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        yield CacheEntry(document_id=doc_id, path=str(entry), size=size)


def build_index(account: TelegramAccount) -> dict[int, str]:
    """Return {document_id → local path}. Last-writer-wins on collisions."""
    index: dict[int, str] = {}
    for entry in iter_cache(account):
        index[entry.document_id] = entry.path
    return index


def save_index(account: TelegramAccount, destination: Path) -> int:
    """Write the index as JSON to `destination` and return the entry count."""
    index = build_index(account)
    serial = [asdict(CacheEntry(doc_id, path, 0)) for doc_id, path in index.items()]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(serial, indent=2))
    return len(serial)


def find(account: TelegramAccount, document_id: int) -> Optional[Path]:
    for entry in iter_cache(account):
        if entry.document_id == document_id:
            return Path(entry.path)
    return None
