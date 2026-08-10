"""A simulated NGS device: everything ngs_app.c does, in Python.

This is what lets the whole host stack -- framing, dispatch, error handling,
telemetry -- be tested with no board on the desk, and it doubles as a target
for bench scripts while the hardware is in use elsewhere.

It mirrors ngs_app.c's *observable behaviour*, not its structure: same
responses, same error codes for the same bad input, same read-and-clear on
loop_max_us. Where the firmware would touch hardware, FakeBoard stands in.

It is not a substitute for the on-target tests (firmware/test/) -- those check
the C itself. This checks that the host agrees with what the C is specified to
do.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import protocol as p
from .link import Decoder, Frame, encode_frame

#: The firmware's compiled-in resolutions (main.cpp).
ADC_BITS = 12
PWM_BITS = 12


@dataclass
class FakeBoard:
    """Simulated hardware state, inspectable by tests.

    `adc` is a callable so a test can hand in a ramp, a noise source, or a
    fixed value without this class growing a mode flag for each.
    """

    cpu_hz: int = 600_000_000
    serial_number: bytes = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    temp_mc: int = 42_000
    fw_version: tuple[int, int, int] = (0, 1, 0)

    pin_modes: dict[int, int] = field(default_factory=dict)
    pin_values: dict[int, int] = field(default_factory=dict)
    pwm: dict[int, tuple[int, int, int]] = field(default_factory=dict)
    pwm_bits: int = PWM_BITS

    adc: Callable[[int], int] = lambda channel: 2048 + channel

    def micros(self) -> int:
        return int(time.perf_counter() * 1e6) & 0xFFFFFFFF


class FakeDevice:
    """A `Transport` that answers like the firmware.

    Wire it straight into a Device:

        dev = Device(FakeDevice())
        dev.ping()
    """

    def __init__(self, board: FakeBoard | None = None) -> None:
        self.board = board or FakeBoard()
        self._decoder = Decoder()
        self._tx = bytearray()
        self.closed = False

        self.tx_frames = 0
        self.loop_max_us = 0
        self._t0 = self.board.micros()

        self.stream_enabled = False
        self.stream_period_us = 0
        self.stream_channel_mask = 0
        self.stream_seq = 0
        self._stream_next_us = 0

        # main.cpp logs this from setup(); a host that connects mid-run would
        # not see it, but one that opens the fake from scratch should.
        self._send(p.MsgType.LOG, 0, b"ngs firmware ready")

    # -- Transport ---------------------------------------------------------

    def write(self, data: bytes) -> int:
        for frame in self._decoder.push(data, on_error=self._on_framing_error):
            self._dispatch(frame)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        self._poll_stream()
        out = bytes(self._tx[:size])
        del self._tx[: len(out)]
        return out

    @property
    def in_waiting(self) -> int:
        """Bytes ready to read, as pyserial reports it. Present so the driver
        takes the same code path here as against a real port."""
        self._poll_stream()
        return len(self._tx)

    def close(self) -> None:
        self.closed = True

    # -- plumbing ----------------------------------------------------------

    def uptime_us(self) -> int:
        return (self.board.micros() - self._t0) & 0xFFFFFFFF

    def _send(self, msg_type: int, seq: int, payload: bytes = b"") -> None:
        self._tx += encode_frame(msg_type, seq, payload)
        self.tx_frames += 1

    def _send_error(self, code: p.ErrCode, seq: int = 0, msg_type: int = 0) -> None:
        self._send(p.MsgType.ERROR, seq, p.ErrorPayload(int(code), seq, msg_type).pack())

    def _on_framing_error(self, exc: Exception) -> None:
        # The firmware reports these with seq/type 0: the frame never decoded,
        # so it has no idea what they were.
        code = getattr(exc, "code", p.ErrCode.BAD_LENGTH)
        self._send_error(code, 0, 0)

    def log(self, text: str) -> None:
        self._send(p.MsgType.LOG, 0, text.encode()[: p.MAX_PAYLOAD])

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self, frame: Frame) -> None:
        handler = {
            p.MsgType.PING: self._ping,
            p.MsgType.GET_INFO: self._get_info,
            p.MsgType.GET_STATUS: self._get_status,
            p.MsgType.RESET: self._reset,
            p.MsgType.SET_GPIO: self._set_gpio,
            p.MsgType.GET_GPIO: self._get_gpio,
            p.MsgType.READ_ADC: self._read_adc,
            p.MsgType.WRITE_PWM: self._write_pwm,
            p.MsgType.SET_STREAM: self._set_stream,
        }.get(frame.type)

        if handler is None:
            self._send_error(p.ErrCode.UNKNOWN_TYPE, frame.seq, frame.type)
            return

        err = handler(frame)
        if err:
            self._send_error(err, frame.seq, frame.type)

    def _respond(self, frame: Frame, payload: bytes = b"") -> None:
        self._send(frame.type | p.MSG_RESP, frame.seq, payload)

    @staticmethod
    def _unpack(cls_: type[p.Payload], frame: Frame):
        """Payload or None -- None means answer NGS_ERR_BAD_PAYLOAD, which is
        what the firmware's `req->len != sizeof(...)` check produces."""
        if len(frame.payload) != cls_.size():
            return None
        return cls_.unpack(frame.payload)

    def _ping(self, frame: Frame) -> p.ErrCode | None:
        self._respond(frame, p.Pong(self.uptime_us()).pack())
        return None

    def _get_info(self, frame: Frame) -> p.ErrCode | None:
        major, minor, patch = self.board.fw_version
        info = p.Info(
            proto_version=p.PROTO_VERSION,
            fw_major=major,
            fw_minor=minor,
            fw_patch=patch,
            cpu_hz=self.board.cpu_hz,
            max_payload=p.MAX_PAYLOAD,
            mcu_serial=self.board.serial_number,
        )
        self._respond(frame, info.pack())
        return None

    def _get_status(self, frame: Frame) -> p.ErrCode | None:
        status = p.Status(
            uptime_us=self.uptime_us(),
            rx_frames=self._decoder.frames,
            tx_frames=self.tx_frames,
            rx_crc_errors=self._decoder.crc_errors,
            rx_overflows=self._decoder.overflows,
            loop_max_us=self.loop_max_us,
            temp_mc=self.board.temp_mc,
        )
        self._respond(frame, status.pack())
        self.loop_max_us = 0  # read-and-clear, as in handle_get_status()
        return None

    def _reset(self, frame: Frame) -> p.ErrCode | None:
        self._respond(frame)  # ack first, then the endpoint drops
        self.__init__(self.board)  # noqa: PLC2801 -- reboot is exactly re-init
        return None

    def _set_gpio(self, frame: Frame) -> p.ErrCode | None:
        req = self._unpack(p.GpioSet, frame)
        if req is None:
            return p.ErrCode.BAD_PAYLOAD
        if (err := self._pin_mode(req.pin, req.mode)) is not None:
            return err
        self.board.pin_values[req.pin] = 1 if req.value else 0
        self._respond(frame)
        return None

    def _get_gpio(self, frame: Frame) -> p.ErrCode | None:
        req = self._unpack(p.GpioGet, frame)
        if req is None:
            return p.ErrCode.BAD_PAYLOAD
        if (err := self._pin_mode(req.pin, req.mode)) is not None:
            return err
        req.value = self.board.pin_values.get(req.pin, 0)
        self._respond(frame, req.pack())
        return None

    def _pin_mode(self, pin: int, mode: int) -> p.ErrCode | None:
        if pin > p.MAX_DIGITAL_PIN:
            return p.ErrCode.BAD_ARGUMENT
        if mode not in tuple(p.PinMode):
            return p.ErrCode.BAD_ARGUMENT
        self.board.pin_modes[pin] = mode
        # An input pin reads the wire, not whatever was last written to it.
        if mode == p.PinMode.INPUT_PULLUP:
            self.board.pin_values.setdefault(pin, 1)
        elif mode != p.PinMode.OUTPUT:
            self.board.pin_values.setdefault(pin, 0)
        return None

    def _read_adc(self, frame: Frame) -> p.ErrCode | None:
        req = self._unpack(p.AdcRead, frame)
        if req is None:
            return p.ErrCode.BAD_PAYLOAD
        if req.channel > p.MAX_ADC_CHANNEL:
            return p.ErrCode.BAD_ARGUMENT

        samples = req.samples or 1  # the protocol documents 0 as one sample
        acc = sum(self.board.adc(req.channel) for _ in range(samples))
        req.raw = acc // samples
        req.resolution = ADC_BITS
        self._respond(frame, req.pack())
        return None

    def _write_pwm(self, frame: Frame) -> p.ErrCode | None:
        req = self._unpack(p.PwmWrite, frame)
        if req is None:
            return p.ErrCode.BAD_PAYLOAD
        if req.pin > p.MAX_DIGITAL_PIN or req.resolution > 16:
            return p.ErrCode.BAD_ARGUMENT

        if req.resolution:
            self.board.pwm_bits = req.resolution
        if self.board.pwm_bits < 16 and req.duty >= (1 << self.board.pwm_bits):
            return p.ErrCode.BAD_ARGUMENT

        self.board.pwm[req.pin] = (req.duty, req.freq_hz, self.board.pwm_bits)
        self._respond(frame)
        return None

    def _set_stream(self, frame: Frame) -> p.ErrCode | None:
        req = self._unpack(p.StreamCfg, frame)
        if req is None:
            return p.ErrCode.BAD_PAYLOAD

        if req.enable:
            if req.period_us == 0 or req.channel_mask == 0:
                return p.ErrCode.BAD_ARGUMENT
            if req.channel_mask >> (p.MAX_ADC_CHANNEL + 1):
                return p.ErrCode.BAD_ARGUMENT
            self.stream_channel_mask = req.channel_mask
            self.stream_period_us = req.period_us
            self._stream_next_us = self.board.micros() + req.period_us
            self.stream_seq = 0
            self.stream_enabled = True
        else:
            self.stream_enabled = False

        self._respond(frame)
        return None

    # -- telemetry ---------------------------------------------------------

    def _poll_stream(self) -> None:
        """Emit whatever records are due. Called from read(), which is the
        fake's equivalent of the firmware's loop()."""
        if not self.stream_enabled:
            return

        now = self.board.micros()
        # Bounded so a long gap between reads cannot generate a huge backlog --
        # the firmware resyncs instead of catching up, and so do we.
        for _ in range(64):
            if (now - self._stream_next_us) & 0x80000000:
                break
            self._emit_telemetry(now)
            self._stream_next_us += self.stream_period_us
        else:
            self._stream_next_us = now + self.stream_period_us

    def _emit_telemetry(self, now: int) -> None:
        channels = [
            ch for ch in range(p.MAX_ADC_CHANNEL + 1) if self.stream_channel_mask & (1 << ch)
        ]
        record = p.Telemetry(
            timestamp_us=now,
            seq=self.stream_seq,
            channel_mask=self.stream_channel_mask,
            resolution=ADC_BITS,
            samples=tuple(self.board.adc(ch) for ch in channels),
        )
        self.stream_seq += 1
        self._send(p.MsgType.TELEMETRY, 0, record.pack())
