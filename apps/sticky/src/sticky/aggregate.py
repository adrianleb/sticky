"""Aggregate sticker-message scans into the upload payload.

Combines message-history sends, pack install state, recent/faved lists,
and hashes peer IDs before they ever leave this module.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from .postbox.messages import StickerMessage, StickerReference


PEER_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("1", 1, 1),
    ("2-5", 2, 5),
    ("6-20", 6, 20),
    ("21+", 21, 10**18),
)


def _bucket_label(count: int) -> str:
    for label, lo, hi in PEER_BUCKETS:
        if lo <= count <= hi:
            return label
    return "21+"


def hash_peer(peer_id: int, salt: bytes) -> str:
    """One-way hash for a peer — used only to *count* distinct peers."""
    h = hashlib.blake2b(digest_size=16, key=salt)
    h.update(peer_id.to_bytes(8, "little", signed=True))
    return h.hexdigest()


@dataclass
class StickerUsage:
    """Per-sticker rollup built during a scan."""

    file_id: int
    access_hash: Optional[int] = None
    sticker_set_id: Optional[int] = None
    total_sends: int = 0
    first_sent_at: Optional[int] = None
    last_sent_at: Optional[int] = None
    peer_hashes: set[str] = field(default_factory=set)
    per_peer_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    daily_sends: dict[date, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, ts: int, peer_hash: str, ref: StickerReference) -> None:
        self.total_sends += 1
        if self.first_sent_at is None or ts < self.first_sent_at:
            self.first_sent_at = ts
        if self.last_sent_at is None or ts > self.last_sent_at:
            self.last_sent_at = ts
        self.peer_hashes.add(peer_hash)
        self.per_peer_counts[peer_hash] += 1
        self.daily_sends[_timestamp_to_date(ts)] += 1
        if self.access_hash is None and ref.access_hash is not None:
            self.access_hash = ref.access_hash
        if self.sticker_set_id is None and ref.sticker_set_id is not None:
            self.sticker_set_id = ref.sticker_set_id

    def peer_count_histogram(self) -> list[dict[str, int]]:
        """Bucketed histogram of how concentrated this sticker's usage is."""
        counts: dict[str, int] = defaultdict(int)
        for sends in self.per_peer_counts.values():
            counts[_bucket_label(sends)] += sends
        return [{"bucket": label, "sends": counts.get(label, 0)} for label, *_ in PEER_BUCKETS]

    def daily_sends_list(self) -> list[tuple[str, int]]:
        return [(day.isoformat(), count) for day, count in sorted(self.daily_sends.items())]

    def to_payload(self) -> dict:
        return {
            "file_id": self.file_id,
            "access_hash": self.access_hash,
            "sticker_set_id": self.sticker_set_id,
            "total_sends": self.total_sends,
            "first_sent_at": self.first_sent_at,
            "last_sent_at": self.last_sent_at,
            "unique_peers_count": len(self.peer_hashes),
            "peer_count_histogram": self.peer_count_histogram(),
            "daily_sends": self.daily_sends_list(),
        }


def _timestamp_to_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def aggregate(
    scans: Iterable[StickerMessage],
    *,
    peer_salt: bytes,
    pack_lookup: Optional[dict[int, StickerReference]] = None,
) -> dict[int, StickerUsage]:
    """Roll sticker-send events into per-file_id usage stats.

    `pack_lookup` maps a sticker document id to a StickerReference describing
    the pack it belongs to. It's used to resolve referenced-media IDs on
    outgoing messages — most sends from an installed pack show up as a bare
    MediaId on the message, with the actual TelegramMediaFile living in the
    ItemCollectionItemTable.
    """
    rollup: dict[int, StickerUsage] = {}
    for scan in scans:
        peer_hash = hash_peer(scan.index.peer_id, peer_salt)
        for ref in scan.stickers:
            usage = rollup.get(ref.file_id)
            if usage is None:
                usage = StickerUsage(
                    file_id=ref.file_id,
                    access_hash=ref.access_hash,
                    sticker_set_id=ref.sticker_set_id,
                )
                rollup[ref.file_id] = usage
            usage.add(scan.index.timestamp, peer_hash, ref)
        if not pack_lookup:
            continue
        for ref_id in scan.referenced_ids:
            resolved = pack_lookup.get(ref_id.id)
            if resolved is None:
                continue
            usage = rollup.get(resolved.file_id)
            if usage is None:
                usage = StickerUsage(
                    file_id=resolved.file_id,
                    access_hash=resolved.access_hash,
                    sticker_set_id=resolved.sticker_set_id,
                )
                rollup[resolved.file_id] = usage
            usage.add(scan.index.timestamp, peer_hash, resolved)
    return rollup


def merge_usage(
    prior: dict[int, StickerUsage],
    new: dict[int, StickerUsage],
) -> dict[int, StickerUsage]:
    """Merge an incremental scan's rollup into prior state."""
    combined = {fid: usage for fid, usage in prior.items()}
    for fid, incoming in new.items():
        if fid not in combined:
            combined[fid] = incoming
            continue
        base = combined[fid]
        base.total_sends += incoming.total_sends
        base.first_sent_at = _min_optional(base.first_sent_at, incoming.first_sent_at)
        base.last_sent_at = _max_optional(base.last_sent_at, incoming.last_sent_at)
        base.peer_hashes |= incoming.peer_hashes
        for peer_hash, count in incoming.per_peer_counts.items():
            base.per_peer_counts[peer_hash] += count
        for day, count in incoming.daily_sends.items():
            base.daily_sends[day] += count
        if base.access_hash is None:
            base.access_hash = incoming.access_hash
        if base.sticker_set_id is None:
            base.sticker_set_id = incoming.sticker_set_id
    return combined


def _min_optional(a: Optional[int], b: Optional[int]) -> Optional[int]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max_optional(a: Optional[int], b: Optional[int]) -> Optional[int]:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)
