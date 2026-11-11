"""Length-prefixed JSON frames: 4-byte little-endian length, then UTF-8 JSON."""

from __future__ import annotations

import json
import struct
from collections.abc import Iterator
from typing import Any

HEADER_SIZE = 4
HANDSHAKE_MAX_FRAME_SIZE = 4 * 1024
MAX_FRAME_SIZE = 16 * 1024 * 1024

_HEADER = struct.Struct("<I")


class FrameError(Exception):
    """The byte stream cannot be decoded; the connection must be closed."""


class FrameTooLarge(FrameError):
    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"frame of {size} bytes exceeds the {limit} byte limit")
        self.size = size
        self.limit = limit


class InvalidFrame(FrameError):
    pass


def encode_frame(message: Any, max_size: int = MAX_FRAME_SIZE) -> bytes:
    text = json.dumps(
        message, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    payload = text.encode("utf-8")
    if len(payload) > max_size:
        raise FrameTooLarge(len(payload), max_size)
    return _HEADER.pack(len(payload)) + payload


class FrameDecoder:
    """Incremental decoder: feed bytes, get decoded JSON messages back."""

    def __init__(self, max_size: int = MAX_FRAME_SIZE) -> None:
        self.max_size = max_size
        self._buffer = bytearray()
        self._failed = False

    def feed(self, data: bytes) -> list[Any]:
        """Buffer `data` and decode every complete frame; raises FrameError."""
        self.append(data)
        return list(self.messages())

    def append(self, data: bytes) -> None:
        if self._failed:
            raise InvalidFrame("decoder already failed")
        self._buffer += data

    def messages(self) -> Iterator[Any]:
        """Decode buffered frames one by one, so frames before a bad one survive."""
        while not self._failed and len(self._buffer) >= HEADER_SIZE:
            (size,) = _HEADER.unpack_from(self._buffer)
            if size > self.max_size:
                self._failed = True
                raise FrameTooLarge(size, self.max_size)
            end = HEADER_SIZE + size
            if len(self._buffer) < end:
                return
            payload = bytes(self._buffer[HEADER_SIZE:end])
            del self._buffer[:end]
            try:
                message = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as err:
                self._failed = True
                raise InvalidFrame(f"frame is not UTF-8 JSON: {err}") from err
            yield message

    @property
    def buffered(self) -> int:
        return len(self._buffer)
