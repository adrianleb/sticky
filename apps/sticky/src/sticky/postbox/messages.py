"""Read outgoing sticker messages from Postbox `MessageHistoryTable`.

Postbox stores every message as a key/value row where:

    key   = MessageIndex = [peer_id: i64 BE][namespace: i32 BE]
                          [timestamp: i32 BE][message_id: i32 BE]   (20 bytes)
    value = intermediate message payload (Postbox-specific framing,
            NOT a PostboxCoding root object — see `read_intermediate_message`).

The payload has a leading message-type byte; type==0 is a regular message.
The sticker information lives in the `embedded_media` array — each entry is
a length-prefixed PostboxCoding blob that deserializes to a TelegramMediaFile
(type hash = mmh3 of "TelegramMediaFile"). Sticker files carry a
`DocumentAttributeSticker` (type=2) attribute with `stickerSet` info.

Adapted from telegram-message-exporter (MIT) with the sticker-specific
extraction added.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from enum import IntFlag
from typing import Iterator, Optional

from .coding import PostboxDecoder, ValueType
from .hashing import murmur_hash
from .schema import (
    DocumentAttributeType,
    TELEGRAM_MEDIA_FILE_ATTRIBUTE_HASH,
    TELEGRAM_MEDIA_FILE_HASH,
)


class MessageFlags(IntFlag):
    UNSENT = 1
    FAILED = 2
    INCOMING = 4
    TOP_INDEXABLE = 16
    SENDING = 32
    CAN_BE_GROUPED_INTO_FEED = 64
    WAS_SCHEDULED = 128
    COUNTED_AS_INCOMING = 256


class MessageDataFlags(IntFlag):
    GLOBALLY_UNIQUE_ID = 1 << 0
    GLOBAL_TAGS = 1 << 1
    GROUPING_KEY = 1 << 2
    GROUP_INFO = 1 << 3
    LOCAL_TAGS = 1 << 4
    THREAD_ID = 1 << 5


class FwdInfoFlags(IntFlag):
    SOURCE_ID = 1 << 1
    SOURCE_MESSAGE = 1 << 2
    SIGNATURE = 1 << 3
    PSA_TYPE = 1 << 4
    FLAGS = 1 << 5


@dataclass(frozen=True)
class MessageIndex:
    """Postbox message index — the SQLite row key."""

    peer_id: int
    namespace: int
    timestamp: int
    message_id: int

    @classmethod
    def parse(cls, key: bytes) -> Optional["MessageIndex"]:
        if len(key) != 20:
            return None
        peer, ns, ts, mid = struct.unpack(">qiii", key)
        return cls(peer, ns, ts, mid)


class _Reader:
    __slots__ = ("buf",)

    def __init__(self, data: bytes) -> None:
        self.buf = io.BytesIO(data)

    def _read(self, fmt: str):
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, self.buf.read(size))[0]

    def u8(self) -> int:
        return self.buf.read(1)[0]

    def i8(self) -> int:
        return self._read("<b")

    def i32(self) -> int:
        return self._read("<i")

    def u32(self) -> int:
        return self._read("<I")

    def i64(self) -> int:
        return self._read("<q")

    def bytes(self) -> bytes:
        length = self.i32()
        return self.buf.read(length)

    def string(self) -> str:
        return self.bytes().decode("utf-8")


@dataclass(frozen=True)
class StickerReference:
    """Reference to a sticker document extracted from a message."""

    file_id: int
    access_hash: Optional[int]
    sticker_set_id: Optional[int]
    sticker_set_access_hash: Optional[int]
    emoji: Optional[str] = None
    mime_type: Optional[str] = None
    datacenter_id: Optional[int] = None
    size_bytes: Optional[int] = None


@dataclass(frozen=True)
class ReferencedMediaId:
    """A 12-byte MediaId (namespace + id) pulled from a message's referenced-media list."""

    namespace: int
    id: int

    @classmethod
    def parse(cls, raw: bytes) -> Optional["ReferencedMediaId"]:
        if len(raw) != 12:
            return None
        ns, mid = struct.unpack("<iq", raw)
        return cls(ns, mid)


@dataclass(frozen=True)
class StickerMessage:
    """Outgoing sticker message found during scan.

    `stickers` holds TelegramMediaFile blobs that were embedded directly in the
    message payload. `referenced_ids` holds MediaId references — the underlying
    media lives in another table (most commonly ItemCollectionItemTable, i.e.
    stickers from installed packs). Callers resolve these with a lookup table.
    """

    index: MessageIndex
    stickers: tuple[StickerReference, ...]
    referenced_ids: tuple[ReferencedMediaId, ...] = ()


_TELEGRAM_MEDIA_FILE_HASH = TELEGRAM_MEDIA_FILE_HASH
_ATTRIBUTE_WRAPPER_HASH = TELEGRAM_MEDIA_FILE_ATTRIBUTE_HASH


def _skip_fwd_info(reader: _Reader) -> None:
    info_flags = FwdInfoFlags(reader.i8())
    if info_flags == 0:
        return
    reader.i64()  # author
    reader.i32()  # date
    if FwdInfoFlags.SOURCE_ID in info_flags:
        reader.i64()
    if FwdInfoFlags.SOURCE_MESSAGE in info_flags:
        reader.i64()
        reader.i32()
        reader.i32()
    if FwdInfoFlags.SIGNATURE in info_flags:
        reader.string()
    if FwdInfoFlags.PSA_TYPE in info_flags:
        reader.string()
    if FwdInfoFlags.FLAGS in info_flags:
        reader.i32()


def _parse_intermediate_message(payload: bytes) -> Optional[dict]:
    reader = _Reader(payload)
    if reader.u8() != 0:
        return None
    reader.u32()  # stableId
    reader.u32()  # stableVer

    data_flags = MessageDataFlags(reader.u8())
    if MessageDataFlags.GLOBALLY_UNIQUE_ID in data_flags:
        reader.i64()
    if MessageDataFlags.GLOBAL_TAGS in data_flags:
        reader.u32()
    if MessageDataFlags.GROUPING_KEY in data_flags:
        reader.i64()
    if MessageDataFlags.GROUP_INFO in data_flags:
        reader.u32()
    if MessageDataFlags.LOCAL_TAGS in data_flags:
        reader.u32()
    if MessageDataFlags.THREAD_ID in data_flags:
        reader.i64()

    flags = MessageFlags(reader.u32())
    reader.u32()  # tags

    _skip_fwd_info(reader)

    if reader.i8() == 1:
        reader.i64()  # author_id

    reader.string()  # text

    attributes_count = reader.i32()
    attribute_blobs: list[bytes] = []
    for _ in range(attributes_count):
        attribute_blobs.append(reader.bytes())

    embedded_media_count = reader.i32()
    embedded_blobs: list[bytes] = []
    for _ in range(embedded_media_count):
        embedded_blobs.append(reader.bytes())

    referenced_ids: list[ReferencedMediaId] = []
    try:
        referenced_count = reader.i32()
    except Exception:  # noqa: BLE001
        referenced_count = 0
    for _ in range(max(0, referenced_count)):
        raw = reader.buf.read(12)
        if len(raw) != 12:
            break
        parsed_id = ReferencedMediaId.parse(raw)
        if parsed_id is not None:
            referenced_ids.append(parsed_id)

    return {
        "flags": flags,
        "attribute_blobs": attribute_blobs,
        "embedded_media_blobs": embedded_blobs,
        "referenced_media_ids": referenced_ids,
    }


def _extract_sticker_from_media_blob(blob: bytes) -> Optional[StickerReference]:
    """Try to decode a TelegramMediaFile blob and return sticker info if present."""
    if len(blob) < 8:
        return None
    try:
        decoder = PostboxDecoder(blob)
        root = decoder.decode_root_object()
    except Exception:  # noqa: BLE001
        return None

    if not isinstance(root, dict):
        return None
    if root.get("@type") != _TELEGRAM_MEDIA_FILE_HASH:
        return None

    attributes = root.get("at") or []
    sticker_set_id = None
    sticker_set_access_hash = None
    emoji: Optional[str] = None
    is_sticker = False

    for attr in attributes:
        if not isinstance(attr, dict):
            continue
        if attr.get("@type") != _ATTRIBUTE_WRAPPER_HASH:
            continue
        if attr.get("t") != DocumentAttributeType.STICKER:
            continue
        is_sticker = True
        set_ref = attr.get("pr")
        if isinstance(set_ref, dict):
            sticker_set_id = set_ref.get("i")
            sticker_set_access_hash = set_ref.get("h")
        dt = attr.get("dt")
        if isinstance(dt, str) and dt:
            emoji = dt

    if not is_sticker:
        return None

    # The real file identifier lives inside the CloudDocumentMediaResource at `r`.
    resource = root.get("r")
    file_id: Optional[int] = None
    access_hash: Optional[int] = None
    datacenter_id: Optional[int] = None
    if isinstance(resource, dict):
        file_id = resource.get("f")
        access_hash = resource.get("a")
        datacenter_id = resource.get("d")

    if file_id is None:
        return None

    mime_type = root.get("mt") if isinstance(root.get("mt"), str) else None
    size_bytes = root.get("s64")

    return StickerReference(
        file_id=int(file_id),
        access_hash=int(access_hash) if access_hash is not None else None,
        sticker_set_id=int(sticker_set_id) if sticker_set_id is not None else None,
        sticker_set_access_hash=(
            int(sticker_set_access_hash)
            if sticker_set_access_hash is not None
            else None
        ),
        emoji=emoji,
        mime_type=mime_type,
        datacenter_id=int(datacenter_id) if datacenter_id is not None else None,
        size_bytes=int(size_bytes) if isinstance(size_bytes, int) else None,
    )


def iter_outgoing_sticker_messages(
    conn,
    table: str,
    *,
    since_ts: Optional[int] = None,
) -> Iterator[StickerMessage]:
    """Yield every outgoing sticker message in the message-history table.

    `since_ts` enables incremental sync: pass the last successful sync
    timestamp; only messages with timestamp > since_ts will be yielded.
    """
    query = f"SELECT key, value FROM {table}"
    for key, value in conn.execute(query):
        if not isinstance(key, (bytes, bytearray)):
            continue
        if not isinstance(value, (bytes, bytearray)):
            continue
        idx = MessageIndex.parse(bytes(key))
        if idx is None:
            continue
        if since_ts is not None and idx.timestamp <= since_ts:
            continue

        parsed = _parse_intermediate_message(bytes(value))
        if parsed is None:
            continue
        if MessageFlags.INCOMING in parsed["flags"]:
            continue

        stickers: list[StickerReference] = []
        for blob in parsed["embedded_media_blobs"]:
            ref = _extract_sticker_from_media_blob(blob)
            if ref is not None:
                stickers.append(ref)

        referenced = tuple(parsed["referenced_media_ids"])
        if not stickers and not referenced:
            continue
        yield StickerMessage(
            index=idx, stickers=tuple(stickers), referenced_ids=referenced
        )


def detect_message_table(conn) -> Optional[str]:
    """Heuristically locate the Postbox message-history table.

    The table has rows whose `key` column is exactly 20 bytes long (a
    MessageIndex) and whose `value` column is a non-empty blob.
    """
    from .tables import list_kv_tables

    candidates: list[tuple[str, int]] = []
    for table in list_kv_tables(conn):
        if 20 not in table.key_lengths:
            continue
        # require a real message: first byte is 0x00 (message_type == 0)
        try:
            sample = conn.execute(
                f"SELECT value FROM {table.name} LIMIT 1"
            ).fetchone()
        except Exception:  # noqa: BLE001
            continue
        if sample is None or not sample[0]:
            continue
        if sample[0][0] != 0:
            continue
        candidates.append((table.name, table.rows))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][0]


# Silence unused-import warnings — ValueType exported for downstream debuggers.
_ = ValueType
