"""COBS framing + CRC-16 -- the Python side of firmware/lib/ngs/ngs_link.c.

Deliberately a line-by-line mirror of the C rather than a call into the `cobs`
package: when a frame is rejected at 3am at the bench, being able to read both
implementations side by side is worth more than the dependency saved. The
tests cross-check this against `cobs` on random data, so "mirror" is enforced
rather than hoped for.

Wire format (see ngs_protocol.h):

    0x00 <COBS( type seq len payload... crc16 )> 0x00
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .protocol import MAX_PAYLOAD, ErrCode

HEADER_SIZE = 4
CRC_SIZE = 2
FRAME_RAW_MAX = HEADER_SIZE + MAX_PAYLOAD + CRC_SIZE


def cobs_max_encoded(n: int) -> int:
    """Worst-case COBS output: one code byte per 254-byte run, plus one to open
    the first run. Matches NGS_COBS_MAX_ENCODED."""
    return n + (n // 254) + 2


class FramingError(Exception):
    """A received frame was malformed. `code` is the NGS_ERR_* the firmware
    would have reported for the same bytes."""

    def __init__(self, code: ErrCode, detail: str) -> None:
        self.code = code
        super().__init__(f"{code.name}: {detail}")


# --------------------------------------------------------------------------
# CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final XOR.
# --------------------------------------------------------------------------


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


# --------------------------------------------------------------------------
# COBS
# --------------------------------------------------------------------------


def cobs_encode(src: bytes) -> bytes:
    out = bytearray(b"\x00")  # slot reserved for the first run's code byte
    code_idx = 0
    code = 1

    for b in src:
        if b != 0:
            out.append(b)
            code += 1
        # A zero ends the run (the code byte implies it), and a full 254-byte
        # run has to be split because the code byte caps at 0xFF.
        if b == 0 or code == 0xFF:
            out[code_idx] = code
            code_idx = len(out)
            out.append(0)
            code = 1

    out[code_idx] = code
    return bytes(out)


def cobs_decode(src: bytes) -> bytes:
    out = bytearray()
    read = 0
    n = len(src)

    while read < n:
        code = src[read]
        read += 1
        if code == 0:
            raise FramingError(ErrCode.BAD_LENGTH, "0x00 inside a COBS body")

        run = code - 1
        if read + run > n:
            raise FramingError(ErrCode.BAD_LENGTH, "COBS run overruns the frame")

        chunk = src[read : read + run]
        # A zero among the data bytes is not valid COBS -- removing zeros is
        # the entire point of the encoding. See the matching check in
        # ngs_link.c; the two must agree or a frame one side rejects is a
        # frame the other side acts on.
        if 0 in chunk:
            raise FramingError(ErrCode.BAD_LENGTH, "0x00 inside a COBS body")

        out += chunk
        read += run

        # A code < 0xFF means the run was terminated by a zero -- unless we just
        # consumed the last byte of input, where that zero was the delimiter.
        if code != 0xFF and read < n:
            out.append(0)

    return bytes(out)


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------


def encode_frame(msg_type: int, seq: int, payload: bytes = b"") -> bytes:
    """Render one complete frame: COBS body plus its trailing delimiter."""
    if len(payload) > MAX_PAYLOAD:
        raise FramingError(ErrCode.OVERFLOW, f"payload {len(payload)} > {MAX_PAYLOAD}")

    body = bytes([msg_type & 0xFF, seq & 0xFF, len(payload) & 0xFF, len(payload) >> 8]) + payload
    crc = crc16(body)
    body += bytes([crc & 0xFF, crc >> 8])
    return cobs_encode(body) + b"\x00"


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Frame:
    type: int
    seq: int
    payload: bytes

    @property
    def is_response(self) -> bool:
        return bool(self.type & 0x80)


class Decoder:
    """Byte-stream frame decoder.

    Fed whatever `serial.read()` returned; yields whole frames as they
    complete. Like the firmware's decoder it treats a lone 0x00 as the only
    frame boundary, so it resynchronises on its own after a dropped byte or a
    device reset mid-frame -- both routine when someone unplugs the bench.

    A malformed frame does not raise: one bad frame in the middle of a buffer
    must not cost the good frames around it, which is exactly the case that
    matters while streaming. Bad frames bump the counters below and are handed
    to `on_error` if the caller supplied one; decoding continues at the next
    delimiter. The counters mirror the ones NGS_MSG_GET_STATUS reports, so
    host- and device-side loss can be compared directly.
    """

    def __init__(self) -> None:
        self._enc = bytearray()
        self._overrun = False
        self.frames = 0
        self.crc_errors = 0
        self.overflows = 0

    def push(
        self, data: bytes, on_error: Callable[[FramingError], None] | None = None
    ) -> list[Frame]:
        """Feed received bytes; return every frame that completed."""
        out: list[Frame] = []
        for byte in data:
            if byte != 0x00:
                if len(self._enc) < cobs_max_encoded(FRAME_RAW_MAX):
                    self._enc.append(byte)
                else:
                    # Latch and keep draining to the delimiter, so one oversized
                    # frame produces one error rather than a burst of garbage.
                    self._overrun = True
                continue

            try:
                frame = self._end_of_frame()
            except FramingError as exc:
                if on_error is not None:
                    on_error(exc)
                continue
            if frame is not None:
                out.append(frame)

        return out

    def _end_of_frame(self) -> Frame | None:
        enc = bytes(self._enc)
        overrun = self._overrun
        self._enc.clear()
        self._overrun = False

        if overrun:
            self.overflows += 1
            raise FramingError(ErrCode.OVERFLOW, f"frame exceeded {FRAME_RAW_MAX} bytes")

        # Runs of delimiters, or a leading one after a reset. Not an error.
        if not enc:
            return None

        try:
            raw = cobs_decode(enc)
        except FramingError:
            self.crc_errors += 1
            raise

        if len(raw) < HEADER_SIZE + CRC_SIZE:
            self.crc_errors += 1
            raise FramingError(ErrCode.BAD_LENGTH, f"frame of {len(raw)} bytes is too short")

        body, crc_bytes = raw[:-CRC_SIZE], raw[-CRC_SIZE:]
        got = crc_bytes[0] | (crc_bytes[1] << 8)
        if got != crc16(body):
            self.crc_errors += 1
            raise FramingError(ErrCode.BAD_CRC, f"got 0x{got:04X}, want 0x{crc16(body):04X}")

        declared = body[2] | (body[3] << 8)
        if declared != len(body) - HEADER_SIZE:
            # CRC passed, so this is a sender bug rather than line corruption.
            raise FramingError(
                ErrCode.BAD_LENGTH, f"len field {declared}, frame carries {len(body) - HEADER_SIZE}"
            )

        self.frames += 1
        return Frame(type=body[0], seq=body[1], payload=body[HEADER_SIZE:])
