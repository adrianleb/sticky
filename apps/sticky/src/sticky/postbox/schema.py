"""Postbox/TelegramCore namespace + table constants.

Source of truth: TelegramMessenger/Telegram-iOS under
`submodules/TelegramCore/Sources/SyncCore/SyncCore_Namespaces.swift`
and the Postbox ItemCollections/OrderedItemList/MessageHistory tables.
"""

from __future__ import annotations

from typing import Final


class ItemCollectionNamespace:
    """Namespaces registered under `Namespaces.ItemCollection`."""

    CLOUD_STICKER_PACKS: Final[int] = 0
    CLOUD_MASK_PACKS: Final[int] = 1
    CLOUD_EMOJI_PACKS: Final[int] = 8


class OrderedItemListNamespace:
    """Namespaces registered under `Namespaces.OrderedItemList`."""

    CLOUD_RECENT_STICKERS: Final[int] = 0
    CLOUD_RECENT_INLINE_BOTS: Final[int] = 2
    CLOUD_SAVED_STICKERS: Final[int] = 7
    CLOUD_RECENT_GIFS: Final[int] = 8


class MessageNamespace:
    """Namespaces registered under `Namespaces.Message`."""

    CLOUD: Final[int] = 0
    LOCAL: Final[int] = 1
    SCHEDULED_CLOUD: Final[int] = 2


class MediaNamespace:
    """Namespaces registered under `Namespaces.Media`."""

    CLOUD_IMAGE: Final[int] = 0
    CLOUD_FILE: Final[int] = 1
    CLOUD_SECRET_FILE: Final[int] = 2
    LOCAL_IMAGE: Final[int] = 3
    LOCAL_FILE: Final[int] = 4


class DocumentAttributeType:
    """Discriminator values for `TelegramMediaFileAttribute` (`t` field).

    Empirically verified against Telegram-macOS Postbox: the `at` array on a
    `TelegramMediaFile` object contains `TelegramMediaFileAttribute` objects
    (type-hash 1922378215), each with a single-byte `t` discriminator.
    """

    FILE_NAME: Final[int] = 0       # fields: fn
    STICKER: Final[int] = 1          # fields: dt (displayText), pr (packReference), mc (maskCoords)
    IMAGE_SIZE: Final[int] = 2       # fields: w, h
    ANIMATED: Final[int] = 3         # no fields
    VIDEO: Final[int] = 4            # fields: dur/du, w, h, f, prs, ct, vc
    AUDIO: Final[int] = 5            # fields: du, iv, wf, ti, pe
    HAS_LINKED_STICKERS: Final[int] = 6
    CUSTOM_EMOJI: Final[int] = 7     # (not yet observed)
    NO_PREMIUM_STICKERS: Final[int] = 8


TELEGRAM_MEDIA_FILE_HASH: Final[int] = 665733176
TELEGRAM_MEDIA_FILE_ATTRIBUTE_HASH: Final[int] = 1922378215
STICKER_PACK_REFERENCE_HASH: Final[int] = -1320282174
STICKER_PACK_COLLECTION_INFO_HASH: Final[int] = 2112923154
