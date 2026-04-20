"""Fetch sticker bodies from Bot API for file_ids not present on disk.

Telegram-macOS only downloads a sticker's full body when it's actually
rendered — power users who send from the picker for years may have 100s
of top stickers whose animated TGS/WebM never touched disk. We can
retrieve them via Bot API (`getStickerSet` + `getFile`) because sticker
packs are public: any bot knows how to reach them given the pack's short
name.

Strategy: group missing file_ids by their pack, call `getStickerSet` once
per pack, zip the pack's sticker list against Postbox's
ItemCollectionItemTable ordering, download each missing sticker's bytes,
save to `~/.sticky/media/` using the MTProto file id as the filename.
The report picks those files up alongside Postbox's own `media/` root.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .account import TelegramAccount
from .botapi import BotApiError, BotClient
from .db import Pack, StickerUsage
from .postbox import (
    ItemCollectionNamespace,
    derive_tempkey,
    iter_item_collection_items,
    list_kv_tables,
    open_postbox,
)
from .postbox.tables import detect_item_collection_item_table


def fetch_dir(base: Path | None = None) -> Path:
    root = base or (Path.home() / ".sticky" / "media")
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass(frozen=True)
class FetchResult:
    fetched: int
    skipped: int
    failed: int
    total_bytes: int
    failures: list[tuple[int, str]]


# ─── on-disk presence check ─────────────────────────────────────────────────


def _collect_on_disk_file_ids(postbox_media: Path, fetch_base: Path) -> set[int]:
    """File ids whose full body already exists locally (any namespace).

    Picks up both Telegram's own `media/telegram-cloud-document-<ns>-<fid>`
    flat files and our fetched `<fetch_base>/<fid>` blobs.
    """
    present: set[int] = set()
    for directory in (postbox_media, fetch_base):
        if not directory.exists():
            continue
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            if name.startswith("telegram-cloud-document-"):
                if "-size-" in name or "_partial" in name or name.endswith(".meta"):
                    continue
                for part in reversed(name.split("-")):
                    if part.isdigit() and len(part) >= 10:
                        present.add(int(part))
                        break
                continue
            # Our own naming: plain integer filenames in fetch_base.
            if name.isdigit():
                present.add(int(name))
    return present


# ─── pack → ordered file-id map from Postbox ────────────────────────────────


def _pack_position_maps(account: TelegramAccount) -> dict[int, list[int]]:
    """Return {collection_id: [file_id, file_id, ...]} in pack display order.

    Pack order is whatever Telegram-macOS stores in ItemCollectionItemTable,
    sorted by `item_index`. This ordering is stable across clients and
    matches Bot API `getStickerSet.stickers[]` ordering.
    """
    tempkey = derive_tempkey(account.tempkey_path)
    ordered: dict[int, list[tuple[int, int]]] = {}
    with open_postbox(account.db_path, tempkey) as (conn, _profile):
        tables = list_kv_tables(conn)
        item_table = detect_item_collection_item_table(conn, tables)
        if item_table is None:
            return {}
        for key, decoded in iter_item_collection_items(
            conn, item_table, ItemCollectionNamespace.CLOUD_STICKER_PACKS
        ):
            fid = _extract_file_id(decoded)
            if fid is None:
                continue
            ordered.setdefault(key.collection_id, []).append((key.item_index, fid))
    return {
        coll_id: [fid for _idx, fid in sorted(items)]
        for coll_id, items in ordered.items()
    }


def _extract_file_id(decoded: object) -> Optional[int]:
    """Pull the MTProto document id out of a decoded StickerPackItem payload.

    TelegramSwift's StickerPackItem carries a `TelegramMediaFile` whose id
    lives at `.file.id` (or `.f.i` depending on schema version). Fall back
    to a recursive search for the largest-looking integer labelled `id`.
    """
    if not isinstance(decoded, dict):
        return None
    for path in (("file", "id"), ("f", "i"), ("file", "i"), ("doc", "id")):
        cur: object = decoded
        ok = True
        for seg in path:
            if not isinstance(cur, dict) or seg not in cur:
                ok = False
                break
            cur = cur[seg]
        if ok and isinstance(cur, int):
            return cur
    return _scan_int_field(decoded, {"id", "i"})


def _scan_int_field(node: object, keys: set[str]) -> Optional[int]:
    if isinstance(node, dict):
        for k in keys:
            v = node.get(k)
            if isinstance(v, int) and v.bit_length() > 32:
                return v
        for v in node.values():
            found = _scan_int_field(v, keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _scan_int_field(v, keys)
            if found is not None:
                return found
    return None


# ─── file format sniffing ───────────────────────────────────────────────────


def sniff_format(body: bytes) -> str:
    """Return 'webm', 'tgs', 'webp', or 'other' based on magic bytes."""
    if body.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if body.startswith(b"\x1f\x8b"):
        return "tgs"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "webp"
    return "other"


# ─── main orchestration ────────────────────────────────────────────────────


async def fetch_missing(
    session: AsyncSession,
    bot: BotClient,
    account: TelegramAccount,
    *,
    limit: Optional[int] = None,
    on_progress=None,
    fetch_base: Optional[Path] = None,
) -> FetchResult:
    """Download stickers that are used but have no full body on disk.

    `on_progress(done, total, label)` is called before each fetch; the caller
    can render a progress bar. `limit` caps the number of fetches per call so
    users can test incrementally.
    """
    base = fetch_base or fetch_dir()
    postbox_media = account.account_dir / "postbox" / "media"
    present = _collect_on_disk_file_ids(postbox_media, base)

    # Candidate stickers: every used sticker whose body isn't local and whose
    # pack we know a shortName for.
    rows = (
        await session.execute(
            select(
                StickerUsage.file_id,
                StickerUsage.sticker_set_id,
                Pack.short_name,
            )
            .join(Pack, StickerUsage.sticker_set_id == Pack.collection_id)
            .where(StickerUsage.sticker_set_id.is_not(None))
            .where(Pack.short_name.is_not(None))
        )
    ).all()

    missing_by_pack: dict[str, list[int]] = {}
    for fid, set_id, short_name in rows:
        if fid in present:
            continue
        missing_by_pack.setdefault(short_name, []).append(fid)

    if not missing_by_pack:
        return FetchResult(0, 0, 0, 0, [])

    # Postbox's per-pack ordering lets us match by position against Bot API's
    # `getStickerSet.stickers[]`, which returns stickers in the same order.
    ordered_by_coll = _pack_position_maps(account)
    # Build short_name -> collection_id from the Pack rows we already have.
    name_to_coll: dict[str, int] = {}
    pack_rows = (await session.execute(select(Pack))).scalars().all()
    for p in pack_rows:
        if p.short_name and p.collection_id:
            name_to_coll[p.short_name] = p.collection_id

    fetched = 0
    skipped = 0
    failed = 0
    total_bytes = 0
    failures: list[tuple[int, str]] = []

    # Counted against the optional cap; helps short test runs.
    todo_total = sum(len(v) for v in missing_by_pack.values())
    if limit is not None:
        todo_total = min(todo_total, limit)
    done = 0

    for short_name, missing_fids in missing_by_pack.items():
        if limit is not None and done >= limit:
            break
        coll_id = name_to_coll.get(short_name)
        if coll_id is None:
            skipped += len(missing_fids)
            continue
        pack_order = ordered_by_coll.get(coll_id)
        if not pack_order:
            skipped += len(missing_fids)
            continue
        try:
            set_info = await bot.get_sticker_set(short_name)
        except BotApiError as exc:
            failed += len(missing_fids)
            failures.append((coll_id, f"getStickerSet({short_name}): {exc.body}"))
            continue
        api_stickers = set_info.get("stickers") or []
        if len(api_stickers) != len(pack_order):
            # Mismatch: pack has been edited since last sync, positional match
            # is unreliable. Skip rather than risk grabbing the wrong body.
            skipped += len(missing_fids)
            failures.append(
                (
                    coll_id,
                    f"pack '{short_name}' size mismatch "
                    f"(postbox={len(pack_order)} vs bot_api={len(api_stickers)})",
                )
            )
            continue

        fid_to_pos = {fid: i for i, fid in enumerate(pack_order)}
        for fid in missing_fids:
            if limit is not None and done >= limit:
                break
            pos = fid_to_pos.get(fid)
            if pos is None:
                skipped += 1
                done += 1
                continue
            api_sticker = api_stickers[pos]
            bot_file_id = api_sticker.get("file_id")
            if not bot_file_id:
                skipped += 1
                done += 1
                continue
            if on_progress is not None:
                on_progress(done, todo_total, f"{short_name} #{pos+1}")
            try:
                info = await bot.get_file(bot_file_id)
                body = await bot.download_file_bytes(info["file_path"])
            except BotApiError as exc:
                failed += 1
                done += 1
                failures.append((fid, f"get_file/download: {exc.body}"))
                continue
            out = base / str(fid)
            out.write_bytes(body)
            total_bytes += len(body)
            fetched += 1
            done += 1

    return FetchResult(fetched, skipped, failed, total_bytes, failures)


async def run_with_progress(
    session: AsyncSession,
    bot: BotClient,
    account: TelegramAccount,
    *,
    limit: Optional[int] = None,
    progress_cb=None,
) -> FetchResult:
    """Thin wrapper so callers can pass an async progress callback."""

    loop = asyncio.get_running_loop()

    def _cb(done: int, total: int, label: str) -> None:
        if progress_cb is not None:
            progress_cb(done, total, label)

    return await fetch_missing(
        session, bot, account, limit=limit, on_progress=_cb, fetch_base=None
    )
