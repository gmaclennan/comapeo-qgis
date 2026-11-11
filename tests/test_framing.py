import json
import struct

import pytest

from comapeo_qgis.core.framing import (
    MAX_FRAME_SIZE,
    FrameDecoder,
    FrameTooLarge,
    InvalidFrame,
    encode_frame,
)

MESSAGES = [
    {"id": 1, "method": "hello", "params": {"secret": "s"}},
    {"method": "layer.changed", "params": {"ids": ["a", "b"]}},
    {"id": 2, "result": {"name": "Escuela ñandú 🌳"}},
]


def raw_frame(payload: bytes) -> bytes:
    return struct.pack("<I", len(payload)) + payload


def test_frame_is_little_endian_length_then_utf8_json():
    data = encode_frame({"name": "ñ"})
    (size,) = struct.unpack("<I", data[:4])
    assert size == len(data) - 4
    assert json.loads(data[4:].decode("utf-8")) == {"name": "ñ"}


def test_decodes_several_frames_in_one_chunk():
    stream = b"".join(encode_frame(m) for m in MESSAGES)
    assert FrameDecoder().feed(stream) == MESSAGES


def test_split_at_every_byte_boundary():
    stream = b"".join(encode_frame(m) for m in MESSAGES)
    for cut in range(len(stream) + 1):
        decoder = FrameDecoder()
        out = decoder.feed(stream[:cut]) + decoder.feed(stream[cut:])
        assert out == MESSAGES, f"cut at {cut}"
        assert decoder.buffered == 0


def test_one_byte_at_a_time():
    stream = b"".join(encode_frame(m) for m in MESSAGES)
    decoder = FrameDecoder()
    out = []
    for i in range(len(stream)):
        out += decoder.feed(stream[i : i + 1])
    assert out == MESSAGES


def test_oversize_length_is_rejected_from_the_header_alone():
    decoder = FrameDecoder(max_size=10)
    with pytest.raises(FrameTooLarge) as info:
        decoder.feed(struct.pack("<I", 11))
    assert info.value.size == 11
    with pytest.raises(InvalidFrame):
        decoder.feed(b"{}")


def test_frame_at_exact_limit_is_accepted():
    payload = json.dumps("x" * 8).encode()
    assert FrameDecoder(max_size=len(payload)).feed(raw_frame(payload)) == ["x" * 8]


def test_default_limit_is_16_mib():
    decoder = FrameDecoder()
    with pytest.raises(FrameTooLarge):
        decoder.feed(struct.pack("<I", MAX_FRAME_SIZE + 1))


def test_invalid_utf8_and_invalid_json_are_errors():
    with pytest.raises(InvalidFrame):
        FrameDecoder().feed(raw_frame(b"\xff\xfe"))
    with pytest.raises(InvalidFrame):
        FrameDecoder().feed(raw_frame(b"{not json"))


def test_encoding_refuses_oversize_messages():
    with pytest.raises(FrameTooLarge):
        encode_frame({"secret": "x" * 5000}, max_size=4096)


def test_deeply_nested_json_is_refused_or_decoded_never_raised_raw():
    # Python 3.14 parses this nesting; earlier versions hit RecursionError.
    try:
        [message] = FrameDecoder().feed(
            raw_frame(("[" * 100_000 + "]" * 100_000).encode())
        )
    except InvalidFrame:
        return
    assert isinstance(message, list)


def test_encoding_refuses_values_json_cannot_carry():
    with pytest.raises(ValueError):
        encode_frame({"n": float("nan")})
