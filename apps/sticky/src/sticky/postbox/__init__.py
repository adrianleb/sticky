"""Postbox decoder primitives — SQLCipher key, binary coding, table readers."""

from .coding import PostboxDecoder, ValueType
from .hashing import TEMPKEY_MURMUR_SEED, murmur_hash
from .keyderiv import PasscodeRequired, TempKey, derive_tempkey
from .messages import (
    MessageIndex,
    ReferencedMediaId,
    StickerMessage,
    StickerReference,
    detect_message_table,
    iter_outgoing_sticker_messages,
)
from .schema import (
    DocumentAttributeType,
    ItemCollectionNamespace,
    MediaNamespace,
    MessageNamespace,
    OrderedItemListNamespace,
)
from .sqlcipher import SQLCipherOpenError, open_postbox
from .tables import (
    ItemCollectionInfoKey,
    ItemCollectionItemKey,
    OrderedItemListKey,
    PostboxTable,
    detect_item_collection_info_table,
    detect_item_collection_item_table,
    detect_media_reference_table,
    detect_ordered_item_list_tables,
    iter_item_collection_infos,
    iter_item_collection_items,
    iter_media_table_stickers,
    iter_ordered_item_list,
    iter_pack_stickers,
    list_kv_tables,
)

__all__ = [
    "DocumentAttributeType",
    "ItemCollectionInfoKey",
    "ItemCollectionItemKey",
    "ItemCollectionNamespace",
    "MediaNamespace",
    "MessageIndex",
    "MessageNamespace",
    "OrderedItemListKey",
    "OrderedItemListNamespace",
    "PasscodeRequired",
    "PostboxDecoder",
    "PostboxTable",
    "ReferencedMediaId",
    "SQLCipherOpenError",
    "StickerMessage",
    "StickerReference",
    "TEMPKEY_MURMUR_SEED",
    "TempKey",
    "ValueType",
    "derive_tempkey",
    "detect_item_collection_info_table",
    "detect_item_collection_item_table",
    "detect_media_reference_table",
    "detect_message_table",
    "detect_ordered_item_list_tables",
    "iter_item_collection_infos",
    "iter_item_collection_items",
    "iter_media_table_stickers",
    "iter_ordered_item_list",
    "iter_outgoing_sticker_messages",
    "iter_pack_stickers",
    "list_kv_tables",
    "murmur_hash",
    "open_postbox",
]
