"""Local SQLite schema.

Single-user; every table is scoped to the sticky-config'd owner. No `users`
row, no cross-user separation, no auth — this runs on your own Mac.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Base(DeclarativeBase):
    pass


class SyncState(Base):
    """Single-row state used by the incremental Postbox scanner."""

    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_message_timestamp: Mapped[Optional[int]] = mapped_column(BigInteger)
    account_id: Mapped[Optional[str]] = mapped_column(String(64))


class StickerUsage(Base):
    __tablename__ = "sticker_usage"
    __table_args__ = (
        UniqueConstraint("file_id", name="uq_sticker_usage_file"),
        Index("ix_sticker_usage_last", "last_sent_at"),
        Index("ix_sticker_usage_total", "total_sends"),
    )

    file_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    access_hash: Mapped[Optional[int]] = mapped_column(BigInteger)
    sticker_set_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)

    total_sends: Mapped[int] = mapped_column(Integer, default=0)
    first_sent_at: Mapped[Optional[int]] = mapped_column(BigInteger)
    last_sent_at: Mapped[Optional[int]] = mapped_column(BigInteger)
    unique_peers_count: Mapped[int] = mapped_column(Integer, default=0)

    peer_count_histogram: Mapped[dict] = mapped_column(JSON, default=dict)
    daily_sends: Mapped[dict] = mapped_column(JSON, default=dict)

    # Cached Bot API file_id after a successful uploadStickerFile call.
    # Once set, we can include this sticker in dynamic packs without re-uploading.
    bot_file_id: Mapped[Optional[str]] = mapped_column(String(255))
    # Stable-across-uploads identity. file_id rotates per use; file_unique_id
    # is the only reliable key when diffing a set's contents.
    bot_file_unique_id: Mapped[Optional[str]] = mapped_column(String(128))

    # Absolute path to the Postbox-cache PNG used when uploading.
    cache_png_path: Mapped[Optional[str]] = mapped_column(String(1024))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Pack(Base):
    """Installed sticker pack, read from Postbox ItemCollectionsTable."""

    __tablename__ = "packs"

    collection_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[Optional[str]] = mapped_column(String(255))
    short_name: Mapped[Optional[str]] = mapped_column(String(255))
    sticker_count: Mapped[int] = mapped_column(Integer, default=0)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    heat_score: Mapped[float] = mapped_column(Float, default=0.0)
    raw_info: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class DynamicPack(Base):
    """A pack this user created via sticky (owned by their Telegram account)."""

    __tablename__ = "dynamic_packs"

    id: Mapped[int] = mapped_column(primary_key=True)
    short_name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(32), default="top-all")
    count: Mapped[int] = mapped_column(Integer, default=30)
    rule: Mapped[dict] = mapped_column(JSON, default=dict)
    last_refreshed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class DynamicPackSticker(Base):
    __tablename__ = "dynamic_pack_stickers"
    __table_args__ = (
        UniqueConstraint("pack_id", "file_id", name="uq_dyn_pack_file"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pack_id: Mapped[int] = mapped_column(
        ForeignKey("dynamic_packs.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[int] = mapped_column(BigInteger)
    position: Mapped[int] = mapped_column(Integer, default=0)
    emoji: Mapped[Optional[str]] = mapped_column(String(32))
    bot_file_id: Mapped[Optional[str]] = mapped_column(String(255))


# ─── Engine / session helpers ──────────────────────────────────────────────


_default_path = Path.home() / ".sticky" / "sticky.db"


def db_url(path: Path | None = None) -> str:
    resolved = path or _default_path
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{resolved}"


def make_engine(path: Path | None = None):
    return create_async_engine(db_url(path), echo=False, future=True)


async def init_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_runtime_migrations(conn)


async def _apply_runtime_migrations(conn) -> None:
    """SQLite-friendly ADD COLUMNs for schema fields added after rollout."""
    from sqlalchemy import text

    def _cols(sync_conn, table: str) -> set[str]:
        rows = sync_conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
        return {row[1] for row in rows}

    cols = await conn.run_sync(_cols, "sticker_usage")
    if "bot_file_unique_id" not in cols:
        await conn.execute(
            text("ALTER TABLE sticker_usage ADD COLUMN bot_file_unique_id VARCHAR(128)")
        )


@asynccontextmanager
async def session_scope(engine) -> AsyncIterator[AsyncSession]:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
