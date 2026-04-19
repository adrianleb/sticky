"""Dynamic pack composition + Bot API orchestration.

Pipeline: pick top-N stickers → upload each PNG to Bot API (cached after first
upload) → createNewStickerSet → DM install link.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .botapi import BotClient
from .config import Config
from .db import DynamicPack, DynamicPackSticker, StickerUsage
from .rank import top_by_window

logger = logging.getLogger(__name__)

DEFAULT_EMOJI = "⭐"
EDIT_SPACING_SEC = 2.0
SHORT_NAME_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class PackCreateResult:
    short_name: str
    title: str
    install_url: str
    added: int
    skipped_no_png: list[int]


class PackError(RuntimeError):
    pass


# ─── helpers ────────────────────────────────────────────────────────────────


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = SHORT_NAME_RE.sub("_", normalized.lower()).strip("_")
    return slug or "pack"


def build_short_name(title: str, bot_username: str) -> str:
    slug = slugify(title)
    suffix = f"_by_{bot_username}"
    max_slug = 64 - len(suffix)
    if max_slug < 1:
        raise PackError("bot_username too long for sticker set short_name")
    return f"{slug[:max_slug]}{suffix}"


def install_url(short_name: str) -> str:
    return f"https://t.me/addstickers/{short_name}"


def resolve_png_path(usage: StickerUsage, cache_dir: Path) -> Path | None:
    """Return the best-available PNG for `uploadStickerFile`.

    Telegram requires at least one side = 512px. The full-resolution render
    (no `size-` prefix, no `-m:` marker) satisfies this; size-prefixed and
    120x120 variants are smaller thumbnails we only fall back to as last resort.
    """
    if usage.cache_png_path and Path(usage.cache_png_path).exists():
        return Path(usage.cache_png_path)
    patterns = [
        # Full-resolution render — one side = 512.
        f"telegram-cloud-document-*-{usage.file_id}:sticker-v3-png",
        f"telegram-cloud-document-*-{usage.file_id}:sticker-v1-png",
    ]
    for pattern in patterns:
        for path in cache_dir.glob(pattern):
            # Exclude `size-…` files from the broad glob.
            if path.is_file() and "-size-" not in path.name:
                return path
    return None


async def _ensure_bot_file_id(
    session: AsyncSession,
    bot: BotClient,
    usage: StickerUsage,
    cache_dir: Path,
) -> tuple[str, str] | None:
    """Upload (or reuse) the Bot API file for this sticker.

    Returns (bot_file_id, bot_file_unique_id) or None if no cache PNG exists.
    file_id rotates per use; file_unique_id is stable and is the correct
    key for diffing pack contents across refreshes.
    """
    if usage.bot_file_id and usage.bot_file_unique_id:
        return usage.bot_file_id, usage.bot_file_unique_id
    png = resolve_png_path(usage, cache_dir)
    if png is None:
        return None
    uploaded = await bot.upload_sticker_file(png, sticker_format="static")
    usage.bot_file_id = uploaded.file_id
    usage.bot_file_unique_id = uploaded.file_unique_id
    usage.cache_png_path = str(png)
    await session.flush()
    return uploaded.file_id, uploaded.file_unique_id


# ─── public API ─────────────────────────────────────────────────────────────


async def create_pack(
    session: AsyncSession,
    bot: BotClient,
    cfg: Config,
    *,
    title: str,
    source: str = "top-all",
    count: int = 30,
    cache_dir: Path,
) -> PackCreateResult:
    """Create a static PNG sticker set of the user's top-N stickers.

    Emits one `uploadStickerFile` per missing cached upload, then a single
    `createNewStickerSet`. DMs the install link on success.
    """
    existing = await session.execute(
        select(DynamicPack).where(DynamicPack.title == title)
    )
    if existing.scalar_one_or_none() is not None:
        raise PackError(f"a dynamic pack titled {title!r} already exists")

    window = "all" if source == "top-all" else source.removeprefix("top-")
    if window not in ("7d", "30d", "90d", "all"):
        raise PackError(f"unknown source: {source}")
    top = await top_by_window(session, window=window, limit=count)  # type: ignore[arg-type]
    if not top:
        raise PackError("no sticker usage data yet — run `sticky sync` first")

    short_name = build_short_name(title, cfg.bot_username)
    stickers: list[dict] = []
    picked: list[StickerUsage] = []
    skipped: list[int] = []
    for usage in top:
        uploaded = await _ensure_bot_file_id(session, bot, usage, cache_dir)
        if uploaded is None:
            skipped.append(usage.file_id)
            continue
        bot_file_id, _unique = uploaded
        stickers.append(
            {"sticker": bot_file_id, "emoji_list": [DEFAULT_EMOJI], "format": "static"}
        )
        picked.append(usage)
        if len(stickers) >= count:
            break

    if not stickers:
        raise PackError(
            "no cached sticker PNGs found for top stickers — sync first, or check "
            "that your Telegram-macOS cache dir is reachable"
        )

    await bot.create_new_sticker_set(name=short_name, title=title, stickers=stickers)

    pack = DynamicPack(
        short_name=short_name,
        title=title,
        source=source,
        count=count,
        rule={"window": window, "count": count},
        last_refreshed_at=datetime.now(tz=timezone.utc),
    )
    session.add(pack)
    await session.flush()
    for pos, usage in enumerate(picked):
        session.add(
            DynamicPackSticker(
                pack_id=pack.id,
                file_id=usage.file_id,
                position=pos,
                emoji=DEFAULT_EMOJI,
                bot_file_id=usage.bot_file_id,
            )
        )

    url = install_url(short_name)
    try:
        await bot.send_message_to_self(
            f"🎉 Your pack \"{title}\" is ready:\n{url}",
        )
    except Exception as exc:  # non-fatal
        logger.warning("failed to DM install link: %s", exc)

    return PackCreateResult(
        short_name=short_name,
        title=title,
        install_url=url,
        added=len(stickers),
        skipped_no_png=skipped,
    )


async def refresh_pack(
    session: AsyncSession,
    bot: BotClient,
    cfg: Config,
    *,
    short_name: str,
    cache_dir: Path,
) -> dict:
    """Rebuild pack contents to match the current top-N for its configured source.

    Diffs current server-side set contents vs target; applies adds/removes with
    ~2s spacing to stay well under Bot API rate limits.
    """
    pack = (
        await session.execute(
            select(DynamicPack).where(DynamicPack.short_name == short_name)
        )
    ).scalar_one_or_none()
    if pack is None:
        raise PackError(f"no dynamic pack named {short_name!r}")
    window = pack.rule.get("window", "all")
    count = pack.rule.get("count", pack.count)

    target_rows = await top_by_window(session, window=window, limit=count)

    @dataclass
    class _Target:
        file_id: int
        bot_file_id: str
        unique_id: str

    targets: list[_Target] = []
    for usage in target_rows:
        uploaded = await _ensure_bot_file_id(session, bot, usage, cache_dir)
        if uploaded is None:
            continue
        bot_file_id, unique_id = uploaded
        targets.append(_Target(usage.file_id, bot_file_id, unique_id))
        if len(targets) >= count:
            break

    current = await bot.get_sticker_set(short_name)
    # Map current set's stable unique_id -> (rotating) file_id to use for removal.
    current_by_unique: dict[str, str] = {
        s["file_unique_id"]: s["file_id"] for s in current.get("stickers", [])
    }
    target_unique_ids = {t.unique_id for t in targets}

    to_remove = [
        (uid, fid) for uid, fid in current_by_unique.items()
        if uid not in target_unique_ids
    ]
    to_add = [t for t in targets if t.unique_id not in current_by_unique]

    removed = 0
    for _uid, fid in to_remove:
        await bot.delete_sticker_from_set(fid)
        removed += 1
        await asyncio.sleep(EDIT_SPACING_SEC)

    added = 0
    for t in to_add:
        await bot.add_sticker_to_set(
            name=short_name,
            sticker={"sticker": t.bot_file_id, "emoji_list": [DEFAULT_EMOJI], "format": "static"},
        )
        added += 1
        await asyncio.sleep(EDIT_SPACING_SEC)

    await session.execute(delete(DynamicPackSticker).where(DynamicPackSticker.pack_id == pack.id))
    for pos, t in enumerate(targets):
        session.add(
            DynamicPackSticker(
                pack_id=pack.id,
                file_id=t.file_id,
                position=pos,
                emoji=DEFAULT_EMOJI,
                bot_file_id=t.bot_file_id,
            )
        )
    pack.last_refreshed_at = datetime.now(tz=timezone.utc)

    return {
        "short_name": short_name,
        "added": added,
        "removed": removed,
        "total": len(targets),
    }


async def list_packs(session: AsyncSession) -> list[DynamicPack]:
    rows = (await session.execute(select(DynamicPack))).scalars().all()
    return list(rows)


async def delete_pack_record(session: AsyncSession, short_name: str) -> bool:
    pack = (
        await session.execute(
            select(DynamicPack).where(DynamicPack.short_name == short_name)
        )
    ).scalar_one_or_none()
    if pack is None:
        return False
    await session.delete(pack)
    return True
