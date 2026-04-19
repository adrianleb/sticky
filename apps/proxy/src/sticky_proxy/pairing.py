from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass


@dataclass
class PairEntry:
    telegram_user_id: int
    expires_at: float


class PairStore:
    """In-memory pairing-code store with TTL. Single-process — one replica only."""

    def __init__(self, ttl_sec: int = 300) -> None:
        self._ttl_sec = ttl_sec
        self._codes: dict[str, PairEntry] = {}
        self._lock = asyncio.Lock()

    async def create(self, telegram_user_id: int) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        async with self._lock:
            self._prune()
            self._codes[code] = PairEntry(
                telegram_user_id=telegram_user_id,
                expires_at=time.time() + self._ttl_sec,
            )
        return code

    async def consume(self, code: str) -> int | None:
        async with self._lock:
            self._prune()
            entry = self._codes.pop(code, None)
        if entry is None:
            return None
        return entry.telegram_user_id

    def _prune(self) -> None:
        now = time.time()
        expired = [code for code, entry in self._codes.items() if entry.expires_at < now]
        for code in expired:
            self._codes.pop(code, None)
