"""Ranking queries over the local StickerUsage table."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import StickerUsage

Window = Literal["7d", "30d", "90d", "all"]


def cutoff_ts(window: Window, now: datetime | None = None) -> int | None:
    if window == "all":
        return None
    days = {"7d": 7, "30d": 30, "90d": 90}[window]
    base = now or datetime.now(tz=timezone.utc)
    return int((base - timedelta(days=days)).timestamp())


async def top_by_window(
    session: AsyncSession, window: Window, limit: int
) -> list[StickerUsage]:
    rows = (await session.execute(select(StickerUsage))).scalars().all()
    if window == "all":
        rows_sorted = sorted(rows, key=lambda r: r.total_sends, reverse=True)
        return rows_sorted[:limit]

    cutoff = cutoff_ts(window)
    cutoff_day = (
        datetime.fromtimestamp(cutoff, tz=timezone.utc).date() if cutoff else None
    )

    def windowed_sum(row: StickerUsage) -> int:
        series = (row.daily_sends or {}).get("series") or []
        total = 0
        for pair in series:
            if not isinstance(pair, list | tuple) or len(pair) < 2:
                continue
            day_str, count = pair[0], pair[1]
            try:
                day = date.fromisoformat(str(day_str))
            except ValueError:
                continue
            if cutoff_day is None or day >= cutoff_day:
                total += int(count or 0)
        return total

    scored = [(r, windowed_sum(r)) for r in rows]
    scored = [(r, s) for (r, s) in scored if s > 0]
    scored.sort(key=lambda rs: rs[1], reverse=True)
    return [r for r, _ in scored[:limit]]


async def graveyard(
    session: AsyncSession,
    *,
    min_lifetime_sends: int = 10,
    idle_days: int = 90,
    limit: int = 50,
) -> list[StickerUsage]:
    cutoff = int((datetime.now(tz=timezone.utc) - timedelta(days=idle_days)).timestamp())
    rows = (
        await session.execute(
            select(StickerUsage)
            .where(StickerUsage.total_sends >= min_lifetime_sends)
            .where(
                (StickerUsage.last_sent_at.is_(None))
                | (StickerUsage.last_sent_at < cutoff)
            )
            .order_by(StickerUsage.total_sends.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)
