"""Orchestrate a single end-to-end Postbox sync.

Opens the Postbox SQLCipher DB, locates the message-history and
ItemCollection tables, runs the outgoing-sticker scan, and builds
the upload payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .account import TelegramAccount
from .aggregate import StickerUsage, aggregate
from .postbox import (
    ItemCollectionNamespace,
    OrderedItemListNamespace,
    StickerReference,
    derive_tempkey,
    detect_message_table,
    iter_item_collection_infos,
    iter_item_collection_items,
    iter_media_table_stickers,
    iter_ordered_item_list,
    iter_outgoing_sticker_messages,
    iter_pack_stickers,
    list_kv_tables,
    open_postbox,
)
from .postbox.tables import (
    detect_item_collection_info_table,
    detect_item_collection_item_table,
    detect_media_reference_table,
    detect_ordered_item_list_tables,
)


@dataclass(frozen=True)
class ScanResult:
    snapshot_at: int
    usage: dict[int, StickerUsage]
    unresolved: dict[int, StickerUsage]
    packs: list[dict]
    recent_stickers: list[dict]
    faved_stickers: list[dict]
    last_timestamp: Optional[int]


def run_scan(
    account: TelegramAccount,
    *,
    peer_salt: bytes,
    since_ts: Optional[int],
) -> ScanResult:
    """Do one full sync and return results ready for upload."""
    tempkey = derive_tempkey(account.tempkey_path)
    with open_postbox(account.db_path, tempkey) as (conn, _profile):
        messages_table = detect_message_table(conn)
        if messages_table is None:
            raise RuntimeError(
                "Could not locate Postbox message-history table. "
                "Run `sticky diagnose` to inspect the DB schema."
            )

        tables = list_kv_tables(conn)
        info_table = detect_item_collection_info_table(conn, tables)
        item_table = detect_item_collection_item_table(conn, tables)
        ordered_names = detect_ordered_item_list_tables(conn, tables)
        media_table = detect_media_reference_table(conn, tables)

        pack_lookup: dict[int, StickerReference] = {}
        if item_table is not None:
            for fid, pack_id, access_hash, _emoji in iter_pack_stickers(
                conn, item_table
            ):
                pack_lookup.setdefault(
                    fid,
                    StickerReference(
                        file_id=fid,
                        access_hash=access_hash,
                        sticker_set_id=pack_id,
                        sticker_set_access_hash=None,
                    ),
                )
        # Messages reference media by document id; MessageHistoryMediaTable
        # stores every sticker the user has ever encountered (including
        # uninstalled or never-installed packs). ICIT wins on conflict since
        # it's the source of truth for currently-installed packs; the media
        # table fills gaps for everything else.
        if media_table is not None:
            for fid, set_id, _set_ah, access_hash, _emoji in iter_media_table_stickers(
                conn, media_table
            ):
                pack_lookup.setdefault(
                    fid,
                    StickerReference(
                        file_id=fid,
                        access_hash=access_hash,
                        sticker_set_id=set_id,
                        sticker_set_access_hash=None,
                    ),
                )

        sticker_messages = iter_outgoing_sticker_messages(
            conn, messages_table, since_ts=since_ts
        )
        usage, unresolved = aggregate(
            sticker_messages, peer_salt=peer_salt, pack_lookup=pack_lookup
        )

        packs = _collect_packs(conn, info_table, item_table)
        recent = _collect_ordered(
            conn, ordered_names, OrderedItemListNamespace.CLOUD_RECENT_STICKERS
        )
        faved = _collect_ordered(
            conn, ordered_names, OrderedItemListNamespace.CLOUD_SAVED_STICKERS
        )

    last_ts = max(
        (
            ts
            for ts in (
                max((u.last_sent_at for u in usage.values() if u.last_sent_at), default=None),
                max((u.last_sent_at for u in unresolved.values() if u.last_sent_at), default=None),
            )
            if ts is not None
        ),
        default=None,
    )
    return ScanResult(
        snapshot_at=int(datetime.now(tz=timezone.utc).timestamp()),
        usage=usage,
        unresolved=unresolved,
        packs=packs,
        recent_stickers=recent,
        faved_stickers=faved,
        last_timestamp=last_ts,
    )


def _collect_packs(
    conn, info_table: Optional[str], item_table: Optional[str]
) -> list[dict]:
    if info_table is None:
        return []
    items_by_coll: dict[int, list[dict]] = {}
    if item_table is not None:
        for item_key, item in iter_item_collection_items(
            conn, item_table, ItemCollectionNamespace.CLOUD_STICKER_PACKS
        ):
            items_by_coll.setdefault(item_key.collection_id, []).append(
                {
                    "item_id": item_key.item_id,
                    "item_index": item_key.item_index,
                    "data": _simplify(item),
                }
            )

    packs: list[dict] = []
    for key, info in iter_item_collection_infos(
        conn, info_table, ItemCollectionNamespace.CLOUD_STICKER_PACKS
    ):
        packs.append(
            {
                "collection_id": key.collection_id,
                "collection_index": key.collection_index,
                "info": _simplify(info),
                "items": items_by_coll.get(key.collection_id, []),
            }
        )
    return packs


def _collect_ordered(conn, table_names: list[str], namespace: int) -> list[dict]:
    entries: list[dict] = []
    for table_name in table_names:
        for key, payload in iter_ordered_item_list(conn, table_name, namespace):
            entries.append(
                {
                    "namespace": key.namespace,
                    "tail": key.tail.hex(),
                    "data": _simplify(payload),
                }
            )
    return entries


def _simplify(payload: object) -> object:
    """Best-effort conversion of decoded Postbox payloads to JSON-safe types."""
    if isinstance(payload, dict):
        return {str(k): _simplify(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_simplify(v) for v in payload]
    if isinstance(payload, tuple):
        return [_simplify(v) for v in payload]
    if isinstance(payload, bytes):
        return payload.hex()
    if isinstance(payload, (int, float, str, bool)) or payload is None:
        return payload
    return repr(payload)
