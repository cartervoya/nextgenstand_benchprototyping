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

PROTO_VERSION = 2
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

    SET_CONTROL = 0x30
    GET_CONTROL = 0x31
    AUTOTUNE = 0x32
    GET_AUTOTUNE = 0x33

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
    # IEEE-754 single, little-endian -- the same bytes the Cortex-M7 FPU uses.
    "float": "f",
}

_CTYPE_SIZE = {
    "uint8_t": 1,
    "int8_t": 1,
    "uint16_t": 2,
    "int16_t": 2,
    "uint32_t": 4,
    "int32_t": 4,
    "float": 4,
}


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


# --------------------------------------------------------------------------
# Closed-loop pump control
# --------------------------------------------------------------------------


class PumpMode(IntEnum):
    """NGS_PUMP_MODE_* -- who owns the pump output."""

    MANUAL = 0x00
    AUTO = 0x01
    AUTOTUNE = 0x02


class CtrlFlag(IntEnum):
    """NGS_CTRL_FLAG_* -- what the loop is doing, as a bitmask."""

    SATURATED = 0x01
    WINDUP = 0x02
    FAULT = 0x04
    SLEWING = 0x08


class CtrlOpt(IntEnum):
    """NGS_CTRL_OPT_* -- optional behaviour, as a bitmask."""

    FAULT_CHECK = 0x01


class AutotuneAction(IntEnum):
    ABORT = 0x00
    START = 0x01


class TuningRule(IntEnum):
    """NGS_AT_RULE_* -- how measured Ku/Tu become gains."""

    TYREUS_LUYBEN = 0x00
    ZIEGLER_NICHOLS = 0x01
    PESSEN = 0x02


class AutotuneState(IntEnum):
    IDLE = 0x00
    SETTLING = 0x01
    RELAY = 0x02
    DONE = 0x03
    FAILED = 0x04


class AutotuneFail(IntEnum):
    NONE = 0x00
    TIMEOUT = 0x01
    NO_SWING = 0x02
    SENSOR = 0x03
    ABORTED = 0x04
    INCONSISTENT = 0x05


@dataclass(slots=True)
class ControlCfg(Payload):
    C_NAME = "NgsControlCfgPayload"
    FIELDS = (
        ("uint8_t", "mode"),
        ("uint8_t", "channel"),
        ("uint8_t", "options"),
        ("uint8_t", "_pad"),
        ("float", "setpoint"),
        ("float", "kp"),
        ("float", "ki"),
        ("float", "kd"),
        ("float", "out_min"),
        ("float", "out_max"),
        ("float", "filter_tau_s"),
        ("float", "deadband"),
        ("float", "setpoint_slew"),
        ("float", "output_slew"),
        ("float", "cal_scale"),
        ("float", "cal_offset"),
        ("float", "fault_below"),
        ("uint32_t", "period_us"),
    )

    mode: int = PumpMode.MANUAL
    channel: int = 0
    options: int = 0
    setpoint: float = 0.0
    kp: float = 0.05
    ki: float = 0.02
    kd: float = 0.0
    out_min: float = 0.0
    out_max: float = 100.0
    filter_tau_s: float = 1.0
    deadband: float = 0.0
    setpoint_slew: float = 60.0
    output_slew: float = 25.0
    cal_scale: float = 1.0
    cal_offset: float = 0.0
    fault_below: float = 0.0
    period_us: int = 20_000


@dataclass(slots=True)
class ControlState(Payload):
    C_NAME = "NgsControlStatePayload"
    FIELDS = (
        ("uint8_t", "mode"),
        ("uint8_t", "flags"),
        ("uint8_t", "autotune_state"),
        ("uint8_t", "_pad"),
        ("float", "setpoint"),
        ("float", "setpoint_target"),
        ("float", "measurement"),
        ("float", "measurement_raw"),
        ("float", "output"),
        ("float", "p_term"),
        ("float", "i_term"),
        ("float", "d_term"),
        ("uint32_t", "updates"),
        ("uint32_t", "fault_count"),
    )

    mode: int
    flags: int
    autotune_state: int
    setpoint: float
    setpoint_target: float
    measurement: float
    measurement_raw: float
    output: float
    p_term: float
    i_term: float
    d_term: float
    updates: int
    fault_count: int

    @property
    def mode_name(self) -> str:
        try:
            return PumpMode(self.mode).name
        except ValueError:
            return f"0x{self.mode:02X}"

    @property
    def saturated(self) -> bool:
        return bool(self.flags & CtrlFlag.SATURATED)

    @property
    def winding_up(self) -> bool:
        return bool(self.flags & CtrlFlag.WINDUP)

    @property
    def faulted(self) -> bool:
        return bool(self.flags & CtrlFlag.FAULT)

    @property
    def slewing(self) -> bool:
        return bool(self.flags & CtrlFlag.SLEWING)

    @property
    def error(self) -> float:
        return self.setpoint - self.measurement

    def flag_names(self) -> list[str]:
        return [flag.name for flag in CtrlFlag if self.flags & flag]


@dataclass(slots=True)
class AutotuneCmd(Payload):
    C_NAME = "NgsAutotuneCmdPayload"
    FIELDS = (
        ("uint8_t", "action"),
        ("uint8_t", "cycles"),
        ("uint8_t", "rule"),
        ("uint8_t", "_pad"),
        ("float", "setpoint"),
        ("float", "amplitude"),
        ("float", "hysteresis"),
        ("uint32_t", "timeout_ms"),
    )

    action: int = AutotuneAction.START
    cycles: int = 4
    rule: int = TuningRule.TYREUS_LUYBEN
    setpoint: float = 0.0
    amplitude: float = 10.0
    hysteresis: float = 0.0
    timeout_ms: int = 120_000


@dataclass(slots=True)
class AutotuneResult(Payload):
    C_NAME = "NgsAutotuneResultPayload"
    FIELDS = (
        ("uint8_t", "state"),
        ("uint8_t", "fail_reason"),
        ("uint8_t", "cycles_done"),
        ("uint8_t", "rule"),
        ("float", "ku"),
        ("float", "tu"),
        ("float", "amplitude"),
        ("float", "kp"),
        ("float", "ki"),
        ("float", "kd"),
        ("float", "spread"),
    )

    state: int
    fail_reason: int
    cycles_done: int
    rule: int
    ku: float
    tu: float
    amplitude: float
    kp: float
    ki: float
    kd: float
    spread: float

    @property
    def state_name(self) -> str:
        try:
            return AutotuneState(self.state).name
        except ValueError:
            return f"0x{self.state:02X}"

    @property
    def fail_name(self) -> str:
        try:
            return AutotuneFail(self.fail_reason).name
        except ValueError:
            return f"0x{self.fail_reason:02X}"

    @property
    def running(self) -> bool:
        return self.state in (AutotuneState.SETTLING, AutotuneState.RELAY)

    @property
    def trustworthy(self) -> bool:
        """Cycle-to-cycle period scatter under 20 %. Above that the limit cycle
        never really settled and the gains describe noise."""
        return self.state == AutotuneState.DONE and self.spread < 0.20


def apply_rule(rule: int, ku: float, tu: float) -> tuple[float, float, float]:
    """Mirror of ngs_control_apply_rule. Used to preview what a tuning rule
    would give without asking the device to re-run the experiment."""
    if rule == TuningRule.ZIEGLER_NICHOLS:
        kp, ti, td = 0.45 * ku, tu / 1.2, 0.0
    elif rule == TuningRule.PESSEN:
        kp, ti, td = 0.7 * ku, 0.4 * tu, 0.15 * tu
    else:  # Tyreus-Luyben, the conservative default
        kp, ti, td = ku / 3.2, 2.2 * tu, 0.0
    return kp, (kp / ti if ti > 0 else 0.0), kp * td


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
    MsgType.GET_CONTROL: ControlState,
    MsgType.GET_AUTOTUNE: AutotuneResult,
}
