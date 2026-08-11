"""Framing tests: CRC, COBS, and the decoder's behaviour on damaged input."""

from __future__ import annotations

import random

import pytest
from cobs import cobs as reference_cobs

from ngs_host.link import (
    Decoder,
    FramingError,
    cobs_decode,
    cobs_encode,
    crc16,
    encode_frame,
)
from ngs_host.protocol import MAX_PAYLOAD, ErrCode, MsgType


def test_crc16_check_value():
    """The check value every CRC-16/CCITT-FALSE implementation agrees on. If
    this passes, the firmware and this host use the same polynomial."""
    assert crc16(b"123456789") == 0x29B1


def test_crc16_detects_single_bit_flips():
    data = bytearray(b"the quick brown fox")
    good = crc16(bytes(data))
    for bit in range(len(data) * 8):
        flipped = bytearray(data)
        flipped[bit // 8] ^= 1 << (bit % 8)
        assert crc16(bytes(flipped)) != good


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x00",
        b"\x00\x00\x00",
        b"hello",
        b"\x01\x00\x02\x00\x03",
        bytes(range(256)),
        b"\x11" * 254,  # exactly one full COBS run
        b"\x11" * 255,  # one past it, forcing a split
        bytes(600),  # all zeros, longer than one run
    ],
)
def test_cobs_round_trip(payload):
    encoded = cobs_encode(payload)
    assert 0 not in encoded, "a COBS body must never contain the delimiter"
    assert cobs_decode(encoded) == payload


def test_cobs_matches_the_reference_implementation():
    """Ours is a hand-written mirror of the C. Prove it is really COBS and not
    just self-consistent."""
    rng = random.Random(1234)
    for _ in range(300):
        n = rng.randint(0, 700)
        # Zero-heavy on purpose: zeros are where COBS gets interesting.
        payload = bytes(rng.choice([0, 0, 0, rng.randint(0, 255)]) for _ in range(n))
        assert cobs_encode(payload) == reference_cobs.encode(payload)
        assert cobs_decode(reference_cobs.encode(payload)) == payload


def test_cobs_rejects_an_embedded_zero():
    """The same vector the on-target C test uses: a code byte claiming two
    data bytes, the second of which is 0x00.

    Note the shape -- an earlier version of this test used a 4-byte input that
    passed for the wrong reason, tripping the run-past-end check before ever
    reaching the zero. The C implementation had the gap this was supposed to
    catch, and only the on-target run exposed it.
    """
    with pytest.raises(FramingError):
        cobs_decode(bytes([0x03, 0x11, 0x00]))


def test_cobs_rejects_a_zero_code_byte():
    with pytest.raises(FramingError):
        cobs_decode(bytes([0x00, 0x11]))


def test_cobs_rejects_a_run_that_overruns_the_input():
    with pytest.raises(FramingError):
        cobs_decode(bytes([0x05, 0x11]))  # claims 4 data bytes, one present


def decode_one(data: bytes) -> list:
    return Decoder().push(data)


def test_frame_round_trip():
    frames = decode_one(encode_frame(MsgType.PING, 7, b"abc"))
    assert len(frames) == 1
    assert frames[0].type == MsgType.PING
    assert frames[0].seq == 7
    assert frames[0].payload == b"abc"


def test_empty_payload_round_trip():
    (frame,) = decode_one(encode_frame(MsgType.GET_INFO, 0, b""))
    assert frame.payload == b""


def test_max_payload_round_trip():
    payload = bytes(range(256)) * 2
    assert len(payload) == MAX_PAYLOAD
    (frame,) = decode_one(encode_frame(MsgType.LOG, 1, payload))
    assert frame.payload == payload


def test_oversized_payload_is_refused_before_it_reaches_the_wire():
    with pytest.raises(FramingError) as exc:
        encode_frame(MsgType.LOG, 1, bytes(MAX_PAYLOAD + 1))
    assert exc.value.code is ErrCode.OVERFLOW


def test_response_flag():
    (frame,) = decode_one(encode_frame(MsgType.PING | 0x80, 3, b""))
    assert frame.is_response


def test_several_frames_in_one_read():
    data = encode_frame(MsgType.PING, 1) + encode_frame(MsgType.PING, 2)
    assert [f.seq for f in decode_one(data)] == [1, 2]


def test_frame_split_across_reads():
    """The realistic case: USB hands us arbitrary chunks."""
    wire = encode_frame(MsgType.READ_ADC, 9, b"\x01\x02\x03")
    decoder = Decoder()
    out = []
    for i in range(0, len(wire), 3):
        out += decoder.push(wire[i : i + 3])
    assert len(out) == 1
    assert out[0].payload == b"\x01\x02\x03"


def test_corrupted_byte_is_reported_and_the_stream_resynchronises():
    good = encode_frame(MsgType.PING, 1, b"data")
    corrupt = bytearray(good)
    corrupt[3] ^= 0xFF

    errors = []
    decoder = Decoder()
    frames = decoder.push(bytes(corrupt) + good, on_error=errors.append)

    assert [e.code for e in errors] == [ErrCode.BAD_CRC]
    assert len(frames) == 1, "the frame after the damaged one must still arrive"
    assert decoder.crc_errors == 1


def test_leading_garbage_and_stray_delimiters_are_ignored():
    """What a mid-frame device reset looks like from here."""
    wire = b"\x00\x00" + encode_frame(MsgType.PING, 4) + b"\x00\x00"
    errors = []
    frames = Decoder().push(wire, on_error=errors.append)
    assert len(frames) == 1
    assert not errors


def test_truncated_frame_is_rejected_not_silently_accepted():
    errors = []
    Decoder().push(b"\x03ab\x00", on_error=errors.append)
    assert errors and errors[0].code in (ErrCode.BAD_CRC, ErrCode.BAD_LENGTH)


def test_length_field_disagreeing_with_the_frame_is_caught():
    """CRC-valid but internally inconsistent -- a sender bug, not corruption.
    Built by hand because encode_frame cannot produce it."""
    body = bytes([MsgType.PING, 1, 99, 0]) + b"xy"  # claims 99 bytes, carries 2
    crc = crc16(body)
    wire = cobs_encode(body + bytes([crc & 0xFF, crc >> 8])) + b"\x00"

    errors = []
    frames = Decoder().push(wire, on_error=errors.append)
    assert not frames
    assert [e.code for e in errors] == [ErrCode.BAD_LENGTH]


def test_oversized_inbound_frame_reports_one_overflow():
    decoder = Decoder()
    errors = []
    # No delimiter for a long time, then one: the decoder should latch a single
    # overflow rather than emitting a burst of garbage.
    decoder.push(b"\x41" * 4096, on_error=errors.append)
    decoder.push(b"\x00", on_error=errors.append)
    assert [e.code for e in errors] == [ErrCode.OVERFLOW]
    assert decoder.overflows == 1

    # And it is usable afterwards.
    assert len(decoder.push(encode_frame(MsgType.PING, 1))) == 1
