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
from .control import FakeController
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

    #: Set by the control loop to the timestamp of the tick being served, and
    #: cleared afterwards. A simulated plant behind `adc` should advance on
    #: this when it is set: the loop replays a burst of ticks on its own clock
    #: whenever the host does I/O, and a plant advancing on wall time would sit
    #: frozen through the burst -- which turns any tuning experiment run
    #: against the simulator into a measurement of the host's poll rate.
    sim_now_us: int | None = None

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

        # Emergency stop, mirroring ngs_app.c.
        self.estop = False
        self.estop_source = p.EstopSource.NONE
        self.safe_entries: dict[int, p.SafeEntry] = {}
        self.watchdog_ms = 0
        self.last_rx_us = self.board.micros()

        self.control = FakeController()
        self.control_pin = 0
        self.control_bits = 0

        # main.cpp logs this from setup(); a host that connects mid-run would
        # not see it, but one that opens the fake from scratch should.
        self._send(p.MsgType.LOG, 0, b"ngs firmware ready")

    # -- Transport ---------------------------------------------------------

    def write(self, data: bytes) -> int:
        for frame in self._decoder.push(data, on_error=self._on_framing_error):
            self._dispatch(frame)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        self._poll_watchdog()
        self._poll_control()
        self._poll_stream()
        out = bytes(self._tx[:size])
        del self._tx[: len(out)]
        return out

    @property
    def in_waiting(self) -> int:
        """Bytes ready to read, as pyserial reports it. Present so the driver
        takes the same code path here as against a real port."""
        self._poll_watchdog()
        self._poll_control()
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

    # -- emergency stop ----------------------------------------------------

    def _apply_safe_state(self) -> None:
        """Every registered output to its safe value, and the pump back from
        the controller. Mirrors app_apply_safe_state()."""
        from dataclasses import replace

        self.control.configure(replace(self.control.cfg, mode=p.PumpMode.MANUAL), 0.0)
        self.control.output = 0.0

        for entry in self.safe_entries.values():
            if entry.kind == p.SafeKind.GPIO:
                self.board.pin_modes[entry.pin] = p.PinMode.OUTPUT
                self.board.pin_values[entry.pin] = entry.value
            else:
                freq = self.board.pwm.get(entry.pin, (0, 0, 0))[1]
                self.board.pwm[entry.pin] = (entry.value, freq, entry.resolution)

    def engage_estop(self, source: int = p.EstopSource.COMMAND) -> None:
        if not self.estop:
            self.estop = True
            self.estop_source = source
        self._apply_safe_state()

    def _estop(self, frame: Frame) -> p.ErrCode | None:
        req = self._unpack(p.EstopCmd, frame)
        if req is None:
            return p.ErrCode.BAD_PAYLOAD
        if req.action == p.EstopAction.ENGAGE:
            self.engage_estop(p.EstopSource.COMMAND)
        elif req.action == p.EstopAction.CLEAR:
            self.estop = False
            self.estop_source = p.EstopSource.NONE
        else:
            return p.ErrCode.BAD_ARGUMENT
        self._respond(frame)
        return None

    def _set_safe_entry(self, frame: Frame) -> p.ErrCode | None:
        req = self._unpack(p.SafeEntry, frame)
        if req is None:
            return p.ErrCode.BAD_PAYLOAD

        self.watchdog_ms = req.watchdog_ms

        if req.index == p.SAFE_INDEX_CLEAR:
            self.safe_entries.clear()
            self._respond(frame)
            return None
        if req.index >= p.SAFE_MAX_ENTRIES:
            return p.ErrCode.BAD_ARGUMENT
        if req.kind > p.SafeKind.PWM or req.pin > p.MAX_DIGITAL_PIN:
            return p.ErrCode.BAD_ARGUMENT
        if req.kind == p.SafeKind.GPIO and req.value > 1:
            return p.ErrCode.BAD_ARGUMENT
        if req.kind == p.SafeKind.PWM and not 1 <= req.resolution <= 16:
            return p.ErrCode.BAD_ARGUMENT

        self.safe_entries[req.index] = req
        self._respond(frame)
        return None

    def _poll_watchdog(self) -> None:
        """The host has gone quiet -- see the comment in ngs_app.c."""
        silent_us = self.board.micros() - self.last_rx_us
        if self.watchdog_ms and not self.estop and silent_us > self.watchdog_ms * 1000:
            self.engage_estop(p.EstopSource.WATCHDOG)

    def _dispatch(self, frame: Frame) -> None:
        self.last_rx_us = self.board.micros()
        handler = {
            p.MsgType.PING: self._ping,
            p.MsgType.GET_INFO: self._get_info,
            p.MsgType.GET_STATUS: self._get_status,
            p.MsgType.RESET: self._reset,
            p.MsgType.SET_GPIO: self._set_gpio,
            p.MsgType.GET_GPIO: self._get_gpio,
            p.MsgType.READ_ADC: self._read_adc,
            p.MsgType.WRITE_PWM: self._write_pwm,
            p.MsgType.ESTOP: self._estop,
            p.MsgType.SET_SAFE_ENTRY: self._set_safe_entry,
            p.MsgType.SET_STREAM: self._set_stream,
            p.MsgType.SET_CONTROL: self._set_control,
            p.MsgType.GET_CONTROL: self._get_control,
            p.MsgType.AUTOTUNE: self._autotune,
            p.MsgType.GET_AUTOTUNE: self._get_autotune,
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
            estop=1 if self.estop else 0,
            estop_source=int(self.estop_source),
            safe_entries=(max(self.safe_entries) + 1) if self.safe_entries else 0,
        )
        self._respond(frame, status.pack())
        self.loop_max_us = 0  # read-and-clear, as in handle_get_status()
        return None

    def _reset(self, frame: Frame) -> p.ErrCode | None:
        self._respond(frame)  # ack first, then the endpoint drops
        self.__init__(self.board)  # noqa: PLC2801 -- reboot is exactly re-init
        return None

    def _set_gpio(self, frame: Frame) -> p.ErrCode | None:
        if self.estop:
            return p.ErrCode.ESTOP
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
        if self.estop:
            return p.ErrCode.ESTOP
        req = self._unpack(p.PwmWrite, frame)
        if req is None:
            return p.ErrCode.BAD_PAYLOAD
        if req.pin > p.MAX_DIGITAL_PIN or req.resolution > 16:
            return p.ErrCode.BAD_ARGUMENT

        # The loop owns the output in auto; a manual write would be overwritten
        # on the next tick. Same refusal the firmware makes.
        if self.control.mode != p.PumpMode.MANUAL and req.pin == self.control_pin:
            return p.ErrCode.BUSY

        if req.resolution:
            self.board.pwm_bits = req.resolution
        if self.board.pwm_bits < 16 and req.duty >= (1 << self.board.pwm_bits):
            return p.ErrCode.BAD_ARGUMENT

        self.board.pwm[req.pin] = (req.duty, req.freq_hz, self.board.pwm_bits)
        self.control_pin = req.pin
        if req.resolution:
            self.control_bits = req.resolution
        if self.control_bits:
            full_scale = (1 << self.control_bits) - 1
            self.control.note_manual_output(req.duty * 100.0 / full_scale)
        self._respond(frame)
        return None

    # -- closed-loop control ----------------------------------------------

    def _set_control(self, frame: Frame) -> p.ErrCode | None:
        req = self._unpack(p.ControlCfg, frame)
        if req is None:
            return p.ErrCode.BAD_PAYLOAD
        # Manual is always allowed -- it is a way *out* of driving something.
        if self.estop and req.mode != p.PumpMode.MANUAL:
            return p.ErrCode.ESTOP
        err = self.control.configure(req, self.control.output)
        if err:
            return p.ErrCode(err)
        self._respond(frame)
        return None

    def _get_control(self, frame: Frame) -> p.ErrCode | None:
        self._respond(frame, self.control.state().pack())
        return None

    def _autotune(self, frame: Frame) -> p.ErrCode | None:
        req = self._unpack(p.AutotuneCmd, frame)
        if req is None:
            return p.ErrCode.BAD_PAYLOAD
        if self.estop and req.action != p.AutotuneAction.ABORT:
            return p.ErrCode.ESTOP
        err = self.control.start_autotune(req, self.board.micros(), self.control.output)
        if err:
            return p.ErrCode(err)
        self._respond(frame)
        return None

    def _get_autotune(self, frame: Frame) -> p.ErrCode | None:
        self._respond(frame, self.control.autotune_state().pack())
        return None

    def _poll_control(self) -> None:
        """The fake's equivalent of app_run_control(), driven from read().

        The firmware polls continuously and ticks the loop every period. This
        only gets called when the host does I/O -- perhaps twice a second --
        so it replays the ticks that would have happened in between, feeding
        each one its scheduled timestamp rather than "now". Without that, the
        simulated loop would run hundreds of times slower than the real one and
        every tuning experiment done against it would be meaningless.
        """
        if self.control.mode == p.PumpMode.MANUAL or not self.control_bits:
            return

        now = self.board.micros()
        full_scale = (1 << self.control_bits) - 1

        for _ in range(500):  # bounded: a long stall must not hang the caller
            due = self.control.next_us
            when = now if due == 0 else due
            if when > now:
                break

            self.board.sim_now_us = when
            try:
                raw = int(self.board.adc(self.control.cfg.channel))
            finally:
                self.board.sim_now_us = None

            output = self.control.tick(when, raw)
            if output is None:
                if due == 0:
                    continue  # the priming pass
                break

            duty = int(output / 100.0 * full_scale + 0.5)
            freq = self.board.pwm.get(self.control_pin, (0, 0, 0))[1]
            self.board.pwm[self.control_pin] = (duty, freq, self.control_bits)

            if self.control.mode == p.PumpMode.MANUAL:
                break  # a fault or a finished autotune handed the pump back

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
