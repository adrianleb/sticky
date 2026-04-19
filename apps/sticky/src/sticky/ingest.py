"""Write a ScanResult into the local SQLite DB.

Upserts per-sticker usage, installed packs, and recomputes pack heat_score
(sum of sticker total_sends grouped by sticker_set_id).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Pack, StickerUsage, SyncState, UnresolvedSend
from .scan import ScanResult


async def apply(session: AsyncSession, result: ScanResult) -> dict:
    """Merge a scan result into local state. Returns counts for the CLI."""
    sticker_count = await _upsert_usage(session, result)
    pack_count = await _upsert_packs(session, result)
    unresolved_count = await _upsert_unresolved(session, result)
    await _recompute_heat_scores(session)
    await _update_sync_state(session, result)
    return {
        "stickers": sticker_count,
        "packs": pack_count,
        "unresolved": unresolved_count,
        "last_timestamp": result.last_timestamp,
    }


async def _upsert_usage(session: AsyncSession, result: ScanResult) -> int:
    """Upsert per-sticker usage rows, additively merging with existing data."""
    count = 0
    for usage in result.usage.values():
        payload = usage.to_payload()
        histogram = {"buckets": payload["peer_count_histogram"]}
        daily = {"series": payload["daily_sends"]}

        existing = (
            await session.execute(
                select(StickerUsage).where(StickerUsage.file_id == usage.file_id)
            )
        ).scalar_one_or_none()

        if existing is None:
            session.add(
                StickerUsage(
                    file_id=usage.file_id,
                    access_hash=usage.access_hash,
                    sticker_set_id=usage.sticker_set_id,
                    total_sends=usage.total_sends,
                    first_sent_at=usage.first_sent_at,
                    last_sent_at=usage.last_sent_at,
                    unique_peers_count=len(usage.peer_hashes),
                    peer_count_histogram=histogram,
                    daily_sends=daily,
                )
            )
        else:
            existing.total_sends += usage.total_sends
            if existing.first_sent_at is None or (
                usage.first_sent_at is not None
                and usage.first_sent_at < existing.first_sent_at
            ):
                existing.first_sent_at = usage.first_sent_at
            if usage.last_sent_at and (
                existing.last_sent_at is None
                or usage.last_sent_at > existing.last_sent_at
            ):
                existing.last_sent_at = usage.last_sent_at
            if existing.access_hash is None:
                existing.access_hash = usage.access_hash
            if existing.sticker_set_id is None:
                existing.sticker_set_id = usage.sticker_set_id
            # Histograms and daily series are replaced rather than merged — the
            # scan already aggregates across the message window it covered.
            existing.peer_count_histogram = _merge_histogram(
                existing.peer_count_histogram or {}, histogram
            )
            existing.daily_sends = _merge_daily(existing.daily_sends or {}, daily)
            existing.unique_peers_count = max(
                existing.unique_peers_count, len(usage.peer_hashes)
            )
        count += 1
    await session.flush()
    return count


def _merge_histogram(existing: dict, new: dict) -> dict:
    """Sum per-bucket sends across scans."""
    by_bucket: dict[str, int] = {}
    for entry in existing.get("buckets", []):
        by_bucket[entry["bucket"]] = by_bucket.get(entry["bucket"], 0) + entry["sends"]
    for entry in new.get("buckets", []):
        by_bucket[entry["bucket"]] = by_bucket.get(entry["bucket"], 0) + entry["sends"]
    return {"buckets": [{"bucket": b, "sends": s} for b, s in by_bucket.items()]}


def _merge_daily(existing: dict, new: dict) -> dict:
    """Sum per-day counts across scans."""
    merged: dict[str, int] = {}
    for day, count in existing.get("series", []):
        merged[day] = merged.get(day, 0) + count
    for day, count in new.get("series", []):
        merged[day] = merged.get(day, 0) + count
    return {"series": sorted(merged.items())}


async def _upsert_unresolved(session: AsyncSession, result: ScanResult) -> int:
    """Merge unresolved sends into the UnresolvedSend table.

    Stickers whose document id appears as `MediaId` on an outgoing message but
    isn't in the installed-pack lookup. Most common cause: the pack was
    uninstalled after the send. Counts are additive across syncs.
    """
    count = 0
    for usage in result.unresolved.values():
        existing = (
            await session.execute(
                select(UnresolvedSend).where(UnresolvedSend.file_id == usage.file_id)
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                UnresolvedSend(
                    file_id=usage.file_id,
                    total_sends=usage.total_sends,
                    first_sent_at=usage.first_sent_at,
                    last_sent_at=usage.last_sent_at,
                )
            )
        else:
            existing.total_sends += usage.total_sends
            if usage.first_sent_at is not None and (
                existing.first_sent_at is None
                or usage.first_sent_at < existing.first_sent_at
            ):
                existing.first_sent_at = usage.first_sent_at
            if usage.last_sent_at is not None and (
                existing.last_sent_at is None
                or usage.last_sent_at > existing.last_sent_at
            ):
                existing.last_sent_at = usage.last_sent_at
        count += 1
    await session.flush()
    return count


async def _upsert_packs(session: AsyncSession, result: ScanResult) -> int:
    count = 0
    for pack in result.packs:
        info = pack.get("info") or {}
        inner = info.get("_") if isinstance(info, dict) else None
        if not isinstance(inner, dict):
            inner = info if isinstance(info, dict) else {}
        title = inner.get("title") or inner.get("t")
        short_name = inner.get("shortName") or inner.get("s")
        sticker_count = int(
            inner.get("count") or inner.get("n") or len(pack.get("items") or [])
        )
        flags = inner.get("flags") if "flags" in inner else inner.get("f")
        is_archived = bool(
            isinstance(flags, int) and (flags & 0x1)  # Postbox archived bit
        )
        stmt = sqlite_insert(Pack).values(
            collection_id=pack["collection_id"],
            title=title,
            short_name=short_name,
            sticker_count=sticker_count,
            is_archived=is_archived,
            raw_info=info if isinstance(info, dict) else {},
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Pack.collection_id],
            set_={
                "title": stmt.excluded.title,
                "short_name": stmt.excluded.short_name,
                "sticker_count": stmt.excluded.sticker_count,
                "is_archived": stmt.excluded.is_archived,
                "raw_info": stmt.excluded.raw_info,
                "updated_at": datetime.now(tz=timezone.utc),
            },
        )
        await session.execute(stmt)
        count += 1
    return count


async def _recompute_heat_scores(session: AsyncSession) -> None:
    """Sum total_sends per sticker_set_id into Pack.heat_score."""
    heat_rows = await session.execute(
        select(
            StickerUsage.sticker_set_id,
            func.sum(StickerUsage.total_sends).label("heat"),
        )
        .where(StickerUsage.sticker_set_id.is_not(None))
        .group_by(StickerUsage.sticker_set_id)
    )
    await session.execute(update(Pack).values(heat_score=0.0))
    for set_id, heat in heat_rows.all():
        await session.execute(
            update(Pack)
            .where(Pack.collection_id == set_id)
            .values(heat_score=float(heat or 0.0))
        )


async def _update_sync_state(session: AsyncSession, result: ScanResult) -> None:
    state = (
        await session.execute(select(SyncState).where(SyncState.id == 1))
    ).scalar_one_or_none()
    now = datetime.now(tz=timezone.utc)
    if state is None:
        session.add(
            SyncState(
                id=1,
                last_sync_at=now,
                last_message_timestamp=result.last_timestamp,
            )
        )
    else:
        state.last_sync_at = now
        if result.last_timestamp and (
            state.last_message_timestamp is None
            or result.last_timestamp > state.last_message_timestamp
        ):
            state.last_message_timestamp = result.last_timestamp
