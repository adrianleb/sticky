"""PostboxCoding binary decoder.

Postbox values are serialized with a custom format:

    (KeyLen:u8, Key:UTF-8, ValueType:u8, Value:variable)*

ValueType is an int in 0..13 identifying one of 14 encodings (see
`ValueType`). Object values carry a type hash (mmh3 of the Swift type name)
and a length-prefixed payload of nested (key, value-type, value) records.

This is a Python port of the Postbox Swift encoder in
TelegramMessenger/Telegram-iOS under `submodules/Postbox/Sources/Coding.swift`.
Adapted from the MIT-licensed telegram-message-exporter.
"""

from __future__ import annotations

import enum
import io
import struct
from typing import Any, Callable, Iterator, Optional


class ValueType(enum.Enum):
    """Postbox value encoding tag."""

    INT32 = 0
    INT64 = 1
    BOOL = 2
    DOUBLE = 3
    STRING = 4
    OBJECT = 5
    INT32_ARRAY = 6
    INT64_ARRAY = 7
    OBJECT_ARRAY = 8
    OBJECT_DICTIONARY = 9
    BYTES = 10
    NIL = 11
    STRING_ARRAY = 12
    BYTES_ARRAY = 13


class ByteReader:
    """Tiny struct.unpack reader with pluggable endianness."""

    __slots__ = ("buf", "endian", "_size")

    def __init__(self, data: bytes, endian: str = "<") -> None:
        self.buf = io.BytesIO(data)
        self.endian = endian
        self._size = len(data)

    def tell(self) -> int:
        return self.buf.tell()

    def remaining(self) -> int:
        return self._size - self.buf.tell()

    def read_fmt(self, fmt: str) -> Any:
        full = self.endian + fmt
        raw = self.buf.read(struct.calcsize(full))
        return struct.unpack(full, raw)[0]

    def read_u8(self) -> int:
        return self.buf.read(1)[0]

    def read_i8(self) -> int:
        return self.read_fmt("b")

    def read_i32(self) -> int:
        return self.read_fmt("i")

    def read_u32(self) -> int:
        return self.read_fmt("I")

    def read_i64(self) -> int:
        return self.read_fmt("q")

    def read_u64(self) -> int:
        return self.read_fmt("Q")

    def read_double(self) -> float:
        return self.read_fmt("d")

    def read_bytes(self) -> bytes:
        length = self.read_i32()
        return self.buf.read(length)

    def read_str(self) -> str:
        return self.read_bytes().decode("utf-8")

    def read_short_bytes(self) -> bytes:
        length = self.read_u8()
        return self.buf.read(length)

    def read_short_str(self) -> str:
        return self.read_short_bytes().decode("utf-8")


class PostboxDecoder:
    """Decoder for PostboxCoding payloads."""

    registry: dict[int, type] = {}

    def __init__(self, data: bytes) -> None:
        self.data = bytes(data)
        self.reader = ByteReader(self.data, endian="<")

    @classmethod
    def register(cls, name_hash: int, target: type) -> type:
        cls.registry[name_hash] = target
        return target

    def decode_root_object(self) -> Optional[Any]:
        _, value = self.get(ValueType.OBJECT, "_")
        return value

    def get(
        self, value_type: Optional[ValueType], key: str
    ) -> tuple[Optional[ValueType], Any]:
        for entry_key, entry_type, entry_value in self.iter_kv():
            if entry_key != key:
                continue
            if value_type is None or entry_type == value_type:
                return entry_type, entry_value
            if entry_type == ValueType.NIL:
                return entry_type, None
        return None, None

    def as_dict(self) -> dict[str, Any]:
        """Collect all top-level key/value pairs into a dict."""
        return {k: v for k, _, v in self.iter_kv()}

    def iter_kv(self) -> Iterator[tuple[str, ValueType, Any]]:
        self.reader.buf.seek(0, io.SEEK_SET)
        while self.reader.remaining() > 0:
            key = self.reader.read_short_str()
            value_type, value = self._read_value()
            yield key, value_type, value

    def _read_value(self) -> tuple[ValueType, Any]:
        tag = self.reader.read_u8()
        value_type = ValueType(tag)
        return value_type, self._HANDLERS[value_type](self)

    def _read_array(self, read_item: Callable[[], Any]) -> list[Any]:
        length = self.reader.read_i32()
        return [read_item() for _ in range(length)]

    def _read_object(self) -> Any:
        type_hash = self.reader.read_i32()
        data_len = self.reader.read_i32()
        payload = self.reader.buf.read(data_len)
        target = self.registry.get(type_hash)
        if target is not None:
            return target(PostboxDecoder(payload))
        nested = PostboxDecoder(payload).as_dict()
        nested["@type"] = type_hash
        return nested

    def _read_object_dict(self) -> list[tuple[Any, Any]]:
        length = self.reader.read_i32()
        return [(self._read_object(), self._read_object()) for _ in range(length)]

    _HANDLERS: dict[ValueType, Callable[["PostboxDecoder"], Any]] = {
        ValueType.INT32: lambda d: d.reader.read_i32(),
        ValueType.INT64: lambda d: d.reader.read_i64(),
        ValueType.BOOL: lambda d: d.reader.read_u8() != 0,
        ValueType.DOUBLE: lambda d: d.reader.read_double(),
        ValueType.STRING: lambda d: d.reader.read_str(),
        ValueType.OBJECT: lambda d: d._read_object(),
        ValueType.INT32_ARRAY: lambda d: d._read_array(d.reader.read_i32),
        ValueType.INT64_ARRAY: lambda d: d._read_array(d.reader.read_i64),
        ValueType.OBJECT_ARRAY: lambda d: d._read_array(d._read_object),
        ValueType.OBJECT_DICTIONARY: lambda d: d._read_object_dict(),
        ValueType.BYTES: lambda d: d.reader.read_bytes(),
        ValueType.NIL: lambda d: None,
        ValueType.STRING_ARRAY: lambda d: d._read_array(d.reader.read_str),
        ValueType.BYTES_ARRAY: lambda d: d._read_array(d.reader.read_bytes),
    }
