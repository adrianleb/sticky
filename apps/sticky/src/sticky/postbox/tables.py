"""Readers for Postbox ItemCollections / OrderedItemList / discovery.

Postbox stores everything as key/value pairs in numbered SQLite tables
(`t1`, `t2`, …). The number-to-purpose mapping is stable within a given
TelegramSwift build but is not part of a public API — we discover tables
by their row layout and by inspecting a sample value's `@type` tag.

Key layouts we rely on:

* `ItemCollectionItemTable` rows
    key  = [namespace:i32 BE][collection_id:i64 BE][item_index:i32 BE][item_id:i64 BE]  (24 bytes)
    value = PostboxCoding(StickerPackItem) blob
* `ItemCollectionInfoTable` rows
    key  = [namespace:i32 BE][collection_index:i32 BE][collection_id:i64 BE]  (16 bytes)
    value = PostboxCoding(StickerPackCollectionInfo) blob — @type=2112923154
* `OrderedItemListTable` rows
    key  = [namespace:i32 BE][rank_index:i32 BE or id suffix]  (8–N bytes)
    value = PostboxCoding(OrderedItemListItem) blob — contents contain a
            `TelegramMediaFile` (@type=665733176) when the list holds stickers
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator, Optional

from .coding import PostboxDecoder
from .schema import (
    STICKER_PACK_COLLECTION_INFO_HASH,
    TELEGRAM_MEDIA_FILE_HASH,
)


@dataclass(frozen=True)
class PostboxTable:
    """Metadata for a `t<N>` key/value table."""

    name: str
    rows: int
    key_lengths: tuple[int, ...]  # sampled

    def has_key_length(self, length: int) -> bool:
        return length in self.key_lengths


def list_kv_tables(conn) -> list[PostboxTable]:
    """Return discovered `t<N>` tables and their sampled key layouts.

    `rows` is an O(1) approximation using `max(rowid)` rather than
    `COUNT(*)` — on a 30M-row SQLCipher table, count(*) takes ~2 minutes.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 't%'"
    ).fetchall()
    results: list[PostboxTable] = []
    for (name,) in rows:
        if not name[1:].isdigit():
            continue
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({name})").fetchall()]
        if "key" not in cols or "value" not in cols:
            continue
        # Try max(rowid) first (O(1) on WITH ROWID tables). Postbox declares
        # some tables WITHOUT ROWID — those raise "no such column: rowid"
        # and we leave the count as an unknown sentinel (-1). Callers must
        # not rely on this count being populated for every table.
        try:
            row = conn.execute(f"SELECT max(rowid) FROM {name}").fetchone()
            count = int(row[0]) if row and row[0] is not None else 0
        except Exception:  # noqa: BLE001
            count = -1
        samples = conn.execute(
            f"SELECT length(key) FROM {name} LIMIT 50"
        ).fetchall()
        lengths = tuple(sorted({row[0] for row in samples if row[0] is not None}))
        results.append(PostboxTable(name, count, lengths))
    return results


def _sample_root_type_hashes(conn, table: str, limit: int = 5) -> list[int]:
    """Decode up to `limit` rows from `table` and return any non-zero @type hashes."""
    hashes: list[int] = []
    try:
        cur = conn.execute(f"SELECT value FROM {table} LIMIT {int(limit)}")
    except Exception:  # noqa: BLE001
        return hashes
    for (value,) in cur:
        if not isinstance(value, (bytes, bytearray)):
            continue
        try:
            root = PostboxDecoder(bytes(value)).decode_root_object()
        except Exception:  # noqa: BLE001
            continue
        if isinstance(root, dict) and root.get("@type"):
            hashes.append(int(root["@type"]))
    return hashes


def detect_item_collection_info_table(conn, tables: list[PostboxTable]) -> Optional[str]:
    """Find the single ItemCollectionInfoTable by @type fingerprint."""
    for t in tables:
        if 16 not in t.key_lengths:
            continue
        if len(t.key_lengths) > 1:
            # real t21 has key_len exactly 16, nothing else
            continue
        types = _sample_root_type_hashes(conn, t.name, limit=3)
        if any(h == STICKER_PACK_COLLECTION_INFO_HASH for h in types):
            return t.name
    return None


def detect_item_collection_item_table(conn, tables: list[PostboxTable]) -> Optional[str]:
    """Find the ItemCollectionItemTable by key prefix + value content.

    Keys are exactly 24 bytes with the first 4 bytes carrying a low-valued
    ItemCollectionNamespace (0, 1, or 8). Values are PostboxCoding blobs
    that, once decoded, contain a `TelegramMediaFile` somewhere in the tree.
    """
    for t in tables:
        if t.key_lengths != (24,):
            continue
        try:
            cur = conn.execute(
                f"SELECT key, value FROM {t.name} LIMIT 8"
            )
        except Exception:  # noqa: BLE001
            continue
        hits = 0
        for key, value in cur:
            if not isinstance(key, (bytes, bytearray)) or len(key) != 24:
                continue
            ns = struct.unpack(">i", bytes(key[:4]))[0]
            if ns not in (0, 1, 8):
                continue
            if not isinstance(value, (bytes, bytearray)):
                continue
            if _blob_contains_media_file(bytes(value)):
                hits += 1
        if hits >= 2:
            return t.name
    return None


def detect_ordered_item_list_tables(
    conn, tables: list[PostboxTable]
) -> list[str]:
    """Find OrderedItemList tables whose values wrap a TelegramMediaFile.

    OrderedItemList entries for stickers/saved-stickers embed a
    `TelegramMediaFile` (@type=665733176). Keys are short (8–12 bytes):
    `[namespace:4][id:variable]`. We exclude tables whose key layout also
    includes 16- or 24-byte rows (those are ItemCollectionInfo/Item tables
    which also contain TelegramMediaFile payloads but aren't OrderedItemLists).
    """
    matches: list[str] = []
    for t in tables:
        if 8 not in t.key_lengths:
            continue
        if any(k >= 16 for k in t.key_lengths):
            continue
        try:
            cur = conn.execute(f"SELECT value FROM {t.name} LIMIT 8")
        except Exception:  # noqa: BLE001
            continue
        hits = 0
        for (value,) in cur:
            if not isinstance(value, (bytes, bytearray)):
                continue
            if _blob_contains_media_file(bytes(value)):
                hits += 1
        if hits > 0:
            matches.append(t.name)
    return matches


def iter_pack_stickers(
    conn, item_table: str, namespace: Optional[int] = None
) -> Iterator[tuple[int, int, Optional[int], Optional[str]]]:
    """Yield (document_id, pack_id, access_hash, emoji) for every sticker in the ICIT.

    Used to resolve `referenced_media_ids` from message-history into real
    sticker file_ids + pack membership. When `namespace` is None, yields
    stickers across every namespace present in the table (0, 1, 2, 7, 8) —
    necessary because messages can reference stickers from archived or
    featured packs too, not only the three install-state namespaces.
    """
    prefix = struct.pack(">i", namespace) if namespace is not None else None
    for key, value in conn.execute(f"SELECT key, value FROM {item_table}"):
        if not isinstance(key, (bytes, bytearray)):
            continue
        if prefix is not None and not key.startswith(prefix):
            continue
        parsed_key = ItemCollectionItemKey.parse(bytes(key))
        if parsed_key is None:
            continue
        if namespace is not None and parsed_key.namespace != namespace:
            continue
        try:
            root = PostboxDecoder(bytes(value)).decode_root_object()
        except Exception:  # noqa: BLE001
            continue
        for fid, ah, emoji in _iter_sticker_files(root):
            yield fid, parsed_key.collection_id, ah, emoji


def _iter_sticker_files(
    obj: object,
) -> Iterator[tuple[int, Optional[int], Optional[str]]]:
    """Walk a decoded Postbox tree, yielding (doc_id, access_hash, emoji) for every sticker file."""
    if isinstance(obj, dict):
        if obj.get("@type") == TELEGRAM_MEDIA_FILE_HASH:
            is_sticker = False
            emoji: Optional[str] = None
            for attr in obj.get("at") or []:
                if not isinstance(attr, dict):
                    continue
                if attr.get("@type") != 1922378215:  # DocumentAttribute wrapper
                    continue
                if attr.get("t") != 1:  # DocumentAttributeType.STICKER
                    continue
                is_sticker = True
                dt = attr.get("dt")
                if isinstance(dt, str) and dt:
                    emoji = dt
            if is_sticker:
                resource = obj.get("r") or {}
                fid = resource.get("f") if isinstance(resource, dict) else None
                ah = resource.get("a") if isinstance(resource, dict) else None
                if isinstance(fid, int):
                    yield int(fid), (int(ah) if isinstance(ah, int) else None), emoji
        for v in obj.values():
            yield from _iter_sticker_files(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_sticker_files(v)


def _blob_contains_media_file(blob: bytes) -> bool:
    """Check if a PostboxCoding blob references @type=TELEGRAM_MEDIA_FILE_HASH."""
    try:
        dec = PostboxDecoder(blob)
        for _, _vt, val in dec.iter_kv():
            if _contains_type(val, TELEGRAM_MEDIA_FILE_HASH):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _contains_type(val: object, target: int, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(val, dict):
        if val.get("@type") == target:
            return True
        for v in val.values():
            if _contains_type(v, target, depth + 1):
                return True
    elif isinstance(val, list):
        for v in val:
            if _contains_type(v, target, depth + 1):
                return True
    return False


def find_table_by_key_length(
    tables: list[PostboxTable], length: int, min_rows: int = 1
) -> list[PostboxTable]:
    return [t for t in tables if t.has_key_length(length) and t.rows >= min_rows]


@dataclass(frozen=True)
class ItemCollectionItemKey:
    namespace: int
    collection_id: int
    item_index: int
    item_id: int

    @classmethod
    def parse(cls, key: bytes) -> Optional["ItemCollectionItemKey"]:
        if len(key) != 24:
            return None
        ns, coll, idx, item = struct.unpack(">iqiq", key)
        return cls(ns, coll, idx, item)


@dataclass(frozen=True)
class ItemCollectionInfoKey:
    namespace: int
    collection_index: int
    collection_id: int

    @classmethod
    def parse(cls, key: bytes) -> Optional["ItemCollectionInfoKey"]:
        if len(key) != 16:
            return None
        ns, idx, coll = struct.unpack(">iiq", key)
        return cls(ns, idx, coll)


@dataclass(frozen=True)
class OrderedItemListKey:
    namespace: int
    tail: bytes

    @classmethod
    def parse(cls, key: bytes) -> Optional["OrderedItemListKey"]:
        if len(key) < 4:
            return None
        ns = struct.unpack(">i", key[:4])[0]
        return cls(ns, key[4:])


def iter_item_collection_items(
    conn, table: str, namespace: int
) -> Iterator[tuple[ItemCollectionItemKey, dict]]:
    """Yield sticker-pack items for the given collection namespace."""
    rows = conn.execute(f"SELECT key, value FROM {table}")
    prefix = struct.pack(">i", namespace)
    for key, value in rows:
        if not isinstance(key, (bytes, bytearray)):
            continue
        if not key.startswith(prefix):
            continue
        parsed = ItemCollectionItemKey.parse(bytes(key))
        if parsed is None or parsed.namespace != namespace:
            continue
        try:
            decoded = PostboxDecoder(bytes(value)).as_dict()
        except Exception:  # noqa: BLE001
            continue
        yield parsed, decoded


def iter_item_collection_infos(
    conn, table: str, namespace: int
) -> Iterator[tuple[ItemCollectionInfoKey, dict]]:
    """Yield sticker-pack collection-info rows for the given namespace."""
    rows = conn.execute(f"SELECT key, value FROM {table}")
    prefix = struct.pack(">i", namespace)
    for key, value in rows:
        if not isinstance(key, (bytes, bytearray)):
            continue
        if not key.startswith(prefix):
            continue
        parsed = ItemCollectionInfoKey.parse(bytes(key))
        if parsed is None or parsed.namespace != namespace:
            continue
        try:
            decoded = PostboxDecoder(bytes(value)).as_dict()
        except Exception:  # noqa: BLE001
            continue
        yield parsed, decoded


def iter_ordered_item_list(
    conn, table: str, namespace: int
) -> Iterator[tuple[OrderedItemListKey, dict]]:
    """Yield rows from an OrderedItemList table for the given namespace."""
    rows = conn.execute(f"SELECT key, value FROM {table}")
    prefix = struct.pack(">i", namespace)
    for key, value in rows:
        if not isinstance(key, (bytes, bytearray)):
            continue
        if not key.startswith(prefix):
            continue
        parsed = OrderedItemListKey.parse(bytes(key))
        if parsed is None or parsed.namespace != namespace:
            continue
        try:
            decoded = PostboxDecoder(bytes(value)).as_dict()
        except Exception:  # noqa: BLE001
            continue
        yield parsed, decoded
