"""Mirror of firmware/lib/ngs/ngs_protocol.h.

That header is the single source of truth; this module restates it for Python.
host/tests/test_protocol_sync.py parses the header and fails if the two drift,
so change both sides in the same commit.

Payload layouts are declared once, as their C field list. The `struct` format
string is derived from that list rather than hand-written alongside it, so
there is no second place to forget to update -- and the same declaration is
what the drift test compares against the header.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, fields
from enum import IntEnum
from typing import ClassVar

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

PROTO_VERSION = 1
MAX_PAYLOAD = 512

#: OR'd onto a request type to form the response type.
MSG_RESP = 0x80

# From ngs_board.h -- the firmware validates against these before touching
# hardware, and the host checks them too so a typo fails locally instead of
# costing a round trip.
MAX_DIGITAL_PIN = 54
MAX_ADC_CHANNEL = 17


class MsgType(IntEnum):
    """NGS_MSG_* -- request types are 0x01..0x7F, 0xF0.. are device-initiated."""

    PING = 0x01
    GET_INFO = 0x02
    GET_STATUS = 0x03
    RESET = 0x04

    SET_GPIO = 0x10
    GET_GPIO = 0x11
    READ_ADC = 0x12
    WRITE_PWM = 0x13

    SET_STREAM = 0x20

    LOG = 0xF0
    TELEMETRY = 0xF1
    ERROR = 0xFE

    @property
    def response(self) -> int:
        """The type the device answers this request with."""
        return int(self) | MSG_RESP


class ErrCode(IntEnum):
    """NGS_ERR_* -- payload of an NGS_MSG_ERROR frame."""

    NONE = 0x00
    BAD_CRC = 0x01
    BAD_LENGTH = 0x02
    UNKNOWN_TYPE = 0x03
    BAD_PAYLOAD = 0x04
    BAD_ARGUMENT = 0x05
    OVERFLOW = 0x06
    NOT_SUPPORTED = 0x07
    BUSY = 0x08


class PinMode(IntEnum):
    """NGS_PIN_MODE_* -- applied before the read or write in a GPIO request."""

    OUTPUT = 0x00
    INPUT = 0x01
    INPUT_PULLUP = 0x02
    INPUT_PULLDOWN = 0x03


class NgsError(Exception):
    """The device answered a request with NGS_MSG_ERROR."""

    def __init__(self, code: ErrCode | int, seq: int = 0, msg_type: int = 0) -> None:
        try:
            self.code: ErrCode | int = ErrCode(code)
            name = self.code.name
        except ValueError:  # a firmware newer than this host
            self.code = code
            name = f"0x{code:02X}"
        self.seq = seq
        self.msg_type = msg_type
        super().__init__(f"device error {name} (seq={seq}, type=0x{msg_type:02X})")


# --------------------------------------------------------------------------
# Payloads
#
# Every C struct is __attribute__((packed)) and little-endian, which is what
# a "<" format with no alignment gives us. Fields named _pad map to struct's
# "x" so they never become Python attributes -- they exist only to keep the C
# side naturally aligned.
# --------------------------------------------------------------------------

_CTYPE_FMT = {
    "uint8_t": "B",
    "int8_t": "b",
    "uint16_t": "H",
    "int16_t": "h",
    "uint32_t": "I",
    "int32_t": "i",
}

_CTYPE_SIZE = {"uint8_t": 1, "int8_t": 1, "uint16_t": 2, "int16_t": 2, "uint32_t": 4, "int32_t": 4}


def _fmt_for(ctype: str, name: str) -> str:
    """Translate one C field declaration into its struct format fragment."""
    base, _, count = ctype.partition("[")
    n = int(count.rstrip("]")) if count else 1
    width = _CTYPE_SIZE[base] * n

    if name.startswith("_pad"):
        return "x" * width
    if n > 1:
        if base != "uint8_t":
            raise ValueError(f"non-byte array {ctype} {name} needs an explicit format")
        return f"{width}s"
    return _CTYPE_FMT[base]


@dataclass(slots=True)
class Payload:
    """Base for the NgsXxxPayload mirrors. Subclasses declare C_NAME and FIELDS."""

    #: Name of the mirrored struct in ngs_protocol.h.
    C_NAME: ClassVar[str] = ""
    #: The struct's fields, in declaration order, as (c_type, name).
    FIELDS: ClassVar[tuple[tuple[str, str], ...]] = ()

    @classmethod
    def struct(cls) -> struct.Struct:
        cached = cls.__dict__.get("_struct")
        if cached is None:
            fmt = "<" + "".join(_fmt_for(ctype, name) for ctype, name in cls.FIELDS)
            cached = struct.Struct(fmt)
            cls._struct = cached  # type: ignore[attr-defined]
        return cached

    @classmethod
    def size(cls) -> int:
        return cls.struct().size

    @classmethod
    def unpack(cls, data: bytes):
        if len(data) != cls.size():
            raise NgsError(ErrCode.BAD_PAYLOAD)
        return cls(*cls.struct().unpack(data))

    def pack(self) -> bytes:
        return self.struct().pack(*(getattr(self, f.name) for f in fields(self)))


@dataclass(slots=True)
class Pong(Payload):
    C_NAME = "NgsPongPayload"
    FIELDS = (("uint32_t", "uptime_us"),)

    uptime_us: int


@dataclass(slots=True)
class Info(Payload):
    C_NAME = "NgsInfoPayload"
    FIELDS = (
        ("uint8_t", "proto_version"),
        ("uint8_t", "fw_major"),
        ("uint8_t", "fw_minor"),
        ("uint8_t", "fw_patch"),
        ("uint32_t", "cpu_hz"),
        ("uint32_t", "max_payload"),
        ("uint8_t[8]", "mcu_serial"),
    )

    proto_version: int
    fw_major: int
    fw_minor: int
    fw_patch: int
    cpu_hz: int
    max_payload: int
    mcu_serial: bytes

    @property
    def fw_version(self) -> str:
        return f"{self.fw_major}.{self.fw_minor}.{self.fw_patch}"

    @property
    def serial_hex(self) -> str:
        return self.mcu_serial.hex()


@dataclass(slots=True)
class Status(Payload):
    C_NAME = "NgsStatusPayload"
    FIELDS = (
        ("uint32_t", "uptime_us"),
        ("uint32_t", "rx_frames"),
        ("uint32_t", "tx_frames"),
        ("uint32_t", "rx_crc_errors"),
        ("uint32_t", "rx_overflows"),
        ("uint32_t", "loop_max_us"),
        ("int32_t", "temp_mc"),
    )

    uptime_us: int
    rx_frames: int
    tx_frames: int
    rx_crc_errors: int
    rx_overflows: int
    loop_max_us: int
    temp_mc: int

    @property
    def temp_c(self) -> float:
        return self.temp_mc / 1000.0


@dataclass(slots=True)
class GpioSet(Payload):
    C_NAME = "NgsGpioSetPayload"
    FIELDS = (
        ("uint8_t", "pin"),
        ("uint8_t", "value"),
        ("uint8_t", "mode"),
        ("uint8_t", "_pad"),
    )

    pin: int
    value: int
    mode: int = PinMode.OUTPUT


@dataclass(slots=True)
class GpioGet(Payload):
    C_NAME = "NgsGpioGetPayload"
    FIELDS = (
        ("uint8_t", "pin"),
        ("uint8_t", "value"),
        ("uint8_t", "mode"),
        ("uint8_t", "_pad"),
    )

    pin: int
    value: int = 0
    mode: int = PinMode.INPUT


@dataclass(slots=True)
class AdcRead(Payload):
    C_NAME = "NgsAdcReadPayload"
    FIELDS = (
        ("uint8_t", "channel"),
        ("uint8_t", "samples"),
        ("uint16_t", "raw"),
        ("uint8_t", "resolution"),
        ("uint8_t[3]", "_pad"),
    )

    channel: int
    samples: int = 1
    raw: int = 0
    resolution: int = 0

    @property
    def normalized(self) -> float:
        """Reading as 0.0..1.0. Volts are the caller's job -- the reference and
        any divider are bench wiring, not something the firmware can know."""
        if self.resolution == 0:
            return 0.0
        return self.raw / float((1 << self.resolution) - 1)


@dataclass(slots=True)
class PwmWrite(Payload):
    C_NAME = "NgsPwmWritePayload"
    FIELDS = (
        ("uint8_t", "pin"),
        ("uint8_t", "_pad"),
        ("uint16_t", "duty"),
        ("uint32_t", "freq_hz"),
        ("uint8_t", "resolution"),
        ("uint8_t[3]", "_pad2"),
    )

    pin: int
    duty: int
    freq_hz: int = 0
    resolution: int = 0


@dataclass(slots=True)
class StreamCfg(Payload):
    C_NAME = "NgsStreamCfgPayload"
    FIELDS = (
        ("uint8_t", "enable"),
        ("uint8_t[3]", "_pad"),
        ("uint32_t", "period_us"),
        ("uint32_t", "channel_mask"),
    )

    enable: int
    period_us: int = 0
    channel_mask: int = 0


@dataclass(slots=True)
class TelemetryHeader(Payload):
    C_NAME = "NgsTelemetryHeader"
    FIELDS = (
        ("uint32_t", "timestamp_us"),
        ("uint32_t", "seq"),
        ("uint32_t", "channel_mask"),
        ("uint8_t", "count"),
        ("uint8_t", "resolution"),
        ("uint8_t[2]", "_pad"),
    )

    timestamp_us: int
    seq: int
    channel_mask: int
    count: int
    resolution: int


@dataclass(slots=True)
class ErrorPayload(Payload):
    C_NAME = "NgsErrorPayload"
    FIELDS = (
        ("uint8_t", "code"),
        ("uint8_t", "seq"),
        ("uint8_t", "type"),
        ("uint8_t", "_pad"),
    )

    code: int
    seq: int = 0
    type: int = 0


@dataclass(slots=True)
class Telemetry:
    """A decoded NGS_MSG_TELEMETRY frame: header plus its trailing samples.

    `samples` is ordered by channel number, low bit of channel_mask first --
    the same order the firmware walks the mask in.
    """

    timestamp_us: int
    seq: int
    channel_mask: int
    resolution: int
    samples: tuple[int, ...]

    @property
    def channels(self) -> dict[int, int]:
        """Samples keyed by analog channel, for callers that would otherwise
        have to re-derive the mapping from the mask."""
        active = [ch for ch in range(MAX_ADC_CHANNEL + 1) if self.channel_mask & (1 << ch)]
        return dict(zip(active, self.samples, strict=False))

    @classmethod
    def unpack(cls, data: bytes) -> Telemetry:
        hdr = TelemetryHeader.unpack(data[: TelemetryHeader.size()])
        body = data[TelemetryHeader.size() :]
        if len(body) < hdr.count * 2:
            raise NgsError(ErrCode.BAD_PAYLOAD)
        samples = struct.unpack_from(f"<{hdr.count}H", body)
        return cls(
            timestamp_us=hdr.timestamp_us,
            seq=hdr.seq,
            channel_mask=hdr.channel_mask,
            resolution=hdr.resolution,
            samples=samples,
        )

    def pack(self) -> bytes:
        hdr = TelemetryHeader(
            timestamp_us=self.timestamp_us,
            seq=self.seq,
            channel_mask=self.channel_mask,
            count=len(self.samples),
            resolution=self.resolution,
        )
        return hdr.pack() + struct.pack(f"<{len(self.samples)}H", *self.samples)


#: Response payload class for each request type, for callers that dispatch
#: generically. RESET and the ack-only requests answer with an empty payload.
RESPONSE_PAYLOAD: dict[int, type[Payload]] = {
    MsgType.PING: Pong,
    MsgType.GET_INFO: Info,
    MsgType.GET_STATUS: Status,
    MsgType.GET_GPIO: GpioGet,
    MsgType.READ_ADC: AdcRead,
}
