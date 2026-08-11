"""The bench driver: one `Device` per Teensy, one method per command.

Synchronous and single-threaded on purpose. A bench script reads a channel,
decides something, drives a pin -- a thread pool buys nothing there and makes
failures much harder to reason about. Telemetry, the one genuinely
asynchronous thing, is exposed as a generator instead.

The transport is anything with read/write/close, so the tests drive the whole
driver against a simulated device (see fake.py) without a board attached.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol

from . import protocol as p
from .link import Decoder, Frame, FramingError, encode_frame

#: Teensy's USB vendor id. Every Teensy enumerates under it, so it identifies
#: the board but not which one -- use the MCU serial from GET_INFO for that.
TEENSY_VID = 0x16C0

DEFAULT_TIMEOUT = 1.0

#: How long `wait_ready` keeps trying. Generous: it also covers a board that
#: is still rebooting after a reset or a fresh flash.
READY_TIMEOUT = 5.0

#: Per-attempt timeout while waiting for the link. Short, because the whole
#: point is to retry rather than sit through one long silence.
_READY_ATTEMPT_TIMEOUT = 0.25

#: How long to wait on the transport in one read. Short enough that a command
#: timeout stays responsive, long enough not to spin the CPU.
_READ_CHUNK_TIMEOUT = 0.02


class Transport(Protocol):
    """The slice of pyserial's Serial that this driver actually uses."""

    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...
    def close(self) -> None: ...


class Timeout(TimeoutError):
    """The device did not answer in time."""


class ProtocolMismatch(Exception):
    """The firmware speaks a protocol version this host does not.

    Reflash the board from this checkout -- the header and this package are
    versioned together precisely so this is always the fix.
    """


@dataclass(frozen=True, slots=True)
class PortInfo:
    device: str
    serial_number: str | None
    description: str


def find_ports() -> list[PortInfo]:
    """Every attached Teensy, as candidate port names."""
    from serial.tools import list_ports

    return [
        PortInfo(pt.device, pt.serial_number, pt.description or "")
        for pt in list_ports.comports()
        if pt.vid == TEENSY_VID
    ]


def _resolve_port(port: str | None) -> str:
    if port is not None:
        return port

    found = find_ports()
    if not found:
        raise RuntimeError(
            "no Teensy found. Check the cable, or pass --port explicitly if the "
            "board enumerates under a different VID."
        )
    if len(found) > 1:
        names = ", ".join(f"{pt.device} (serial {pt.serial_number})" for pt in found)
        raise RuntimeError(f"several Teensys attached, pass --port to choose: {names}")
    return found[0].device


class Device:
    """A connected NGS device.

    Usually built with `Device.open()`; pass a transport directly to drive a
    simulated one.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._t = transport
        self.timeout = timeout
        self._decoder = Decoder()
        self._seq = 0
        # Unsolicited frames that arrived while we were waiting for a response.
        # Bounded: a device left streaming into an idle host must not grow the
        # process without limit.
        self.logs: deque[str] = deque(maxlen=256)
        self._telemetry: deque[p.Telemetry] = deque(maxlen=4096)
        self._framing_errors: deque[FramingError] = deque(maxlen=64)
        self._on_log = on_log

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(
        cls,
        port: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        on_log: Callable[[str], None] | None = None,
        wait_ready: bool = True,
    ) -> Device:
        """Open a Teensy by port name, or the only attached one if omitted.

        Waits for the link to actually work before returning -- see
        `wait_ready()`. Pass `wait_ready=False` to skip that when you are
        deliberately talking to something that may not answer.
        """
        import serial

        name = _resolve_port(port)
        # Baud is ignored by Teensy USB CDC (main.cpp says so too); pyserial
        # still requires a value.
        transport = serial.Serial(
            name,
            baudrate=115200,
            timeout=_READ_CHUNK_TIMEOUT,
            # Without this a write to a port whose device vanished (unplugged
            # mid-run) blocks forever instead of raising.
            write_timeout=timeout,
        )
        # The board has been running and talking since it was powered, so the
        # OS buffer can hold a half-finished frame or a boot log from minutes
        # ago. The decoder would resynchronise on its own, but starting from a
        # clean buffer means the first command's response is the first thing we
        # see rather than the tail of something else.
        transport.reset_input_buffer()
        device = cls(transport, timeout=timeout, on_log=on_log)
        if wait_ready:
            device.wait_ready()
        return device

    def wait_ready(self, timeout: float = READY_TIMEOUT) -> None:
        """Ping until the device answers, or give up after `timeout`.

        Opening the port is not the same as having a working link. Until
        Windows finishes the CDC handshake and the firmware sees DTR, its
        `ngs_board_write` finds `!Serial` and *drops* whatever it was about to
        send -- so the first command after open reliably gets a request
        through, is processed, and never hears back. The same gap appears
        after a reset, while the board reboots.

        Retrying with a short per-attempt timeout costs a few milliseconds on
        a healthy link and turns "mystery timeout on the first command" into
        "connected".
        """
        deadline = time.monotonic() + timeout
        attempts = 0
        while True:
            attempts += 1
            try:
                self._transact(p.MsgType.PING, timeout=_READY_ATTEMPT_TIMEOUT)
                return
            except (Timeout, p.NgsError):
                if time.monotonic() >= deadline:
                    raise Timeout(
                        f"device did not respond within {timeout:g}s ({attempts} attempts). "
                        "The port exists, so the board is powered -- is it running this "
                        "firmware? Try: pio run -t upload"
                    ) from None

    @property
    def port(self) -> str | None:
        """The port name, when the transport has one. `None` for a simulated
        device -- which is exactly the distinction a UI wants to display."""
        return getattr(self._t, "port", None)

    def close(self) -> None:
        self._t.close()

    def __enter__(self) -> Device:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- framing plumbing --------------------------------------------------

    @property
    def framing_errors(self) -> tuple[FramingError, ...]:
        """Frames this host rejected, newest last. Compare with the device's
        own rx_crc_errors from `status()` to tell which direction is lossy."""
        return tuple(self._framing_errors)

    def _next_seq(self) -> int:
        # 0 is reserved for unsolicited frames, so an error attributed to seq 0
        # is unambiguously "not caused by a request of ours".
        self._seq = (self._seq % 255) + 1
        return self._seq

    def _read_available(self) -> bytes:
        """Read what is there, without waiting for bytes that may never come.

        pyserial's `read(n)` blocks until it has *n* bytes or the port timeout
        expires -- so asking for a big buffer to "get whatever is available"
        actually pays the full timeout on every single response. That alone
        put 20 ms on every command.

        `in_waiting` gives the real count; the `read(1)` fallback blocks only
        until the first byte and returns as soon as it lands.
        """
        waiting = getattr(self._t, "in_waiting", 0)
        if waiting:
            return self._t.read(waiting)
        return self._t.read(1)

    def _pump(self) -> list[Frame]:
        """Read whatever is available and file the unsolicited frames."""
        data = self._read_available()
        if not data:
            return []

        out: list[Frame] = []
        for frame in self._decoder.push(data, on_error=self._framing_errors.append):
            if frame.type == p.MsgType.LOG:
                text = frame.payload.decode("utf-8", errors="replace")
                self.logs.append(text)
                if self._on_log is not None:
                    self._on_log(text)
            elif frame.type == p.MsgType.TELEMETRY:
                self._telemetry.append(p.Telemetry.unpack(frame.payload))
            else:
                out.append(frame)
        return out

    def _transact(
        self, msg_type: p.MsgType, payload: bytes = b"", *, timeout: float | None = None
    ) -> bytes:
        """Send one request and return the matching response payload."""
        seq = self._next_seq()
        self._t.write(encode_frame(msg_type, seq, payload))

        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            for frame in self._pump():
                if frame.seq != seq:
                    # A late response to a request that already timed out.
                    # Dropping it resynchronises us rather than answering this
                    # request with the previous one's data.
                    continue
                if frame.type == p.MsgType.ERROR:
                    err = p.ErrorPayload.unpack(frame.payload)
                    raise p.NgsError(err.code, err.seq, err.type)
                if frame.type == msg_type | p.MSG_RESP:
                    return frame.payload
                raise p.NgsError(p.ErrCode.UNKNOWN_TYPE, seq, frame.type)

            if time.monotonic() >= deadline:
                waited = self.timeout if timeout is None else timeout
                raise Timeout(f"no response to {msg_type.name} (seq={seq}) in {waited:g}s")

    def transact(self, msg_type: p.MsgType, payload: bytes = b"") -> bytes:
        """Send an arbitrary request and return its response payload.

        The escape hatch for firmware that is newer than this package: a new
        message can be exercised from the CLI before it has a typed wrapper
        here. Prefer the named methods below for anything permanent.
        """
        return self._transact(msg_type, payload)

    # -- commands ----------------------------------------------------------

    def ping(self) -> p.Pong:
        """Round-trip check. The returned uptime is the device's own clock."""
        return p.Pong.unpack(self._transact(p.MsgType.PING))

    def info(self) -> p.Info:
        """Firmware identity. Raises if it speaks a protocol we do not."""
        info = p.Info.unpack(self._transact(p.MsgType.GET_INFO))
        if info.proto_version != p.PROTO_VERSION:
            raise ProtocolMismatch(
                f"device speaks protocol v{info.proto_version} "
                f"(firmware {info.fw_version}), this host speaks v{p.PROTO_VERSION}"
            )
        return info

    def status(self) -> p.Status:
        """Health counters. Note loop_max_us is read-and-clear on the device."""
        return p.Status.unpack(self._transact(p.MsgType.GET_STATUS))

    def reset(self) -> None:
        """Reboot the board. The USB endpoint drops immediately after the ack,
        so this Device is dead afterwards -- reopen to talk again."""
        self._transact(p.MsgType.RESET)

    def set_gpio(self, pin: int, value: bool | int, mode: p.PinMode = p.PinMode.OUTPUT) -> None:
        self._check_pin(pin)
        self._transact(p.MsgType.SET_GPIO, p.GpioSet(pin, int(bool(value)), mode).pack())

    def get_gpio(self, pin: int, mode: p.PinMode = p.PinMode.INPUT) -> bool:
        self._check_pin(pin)
        resp = self._transact(p.MsgType.GET_GPIO, p.GpioGet(pin, mode=mode).pack())
        return bool(p.GpioGet.unpack(resp).value)

    def read_adc(self, channel: int, samples: int = 1) -> p.AdcRead:
        """Averaged analog read. Averaging happens on the device, so `samples`
        costs one round trip regardless of its value."""
        if not 0 <= channel <= p.MAX_ADC_CHANNEL:
            raise ValueError(f"channel {channel} outside A0..A{p.MAX_ADC_CHANNEL}")
        if not 1 <= samples <= 255:
            raise ValueError(f"samples must be 1..255, got {samples}")
        resp = self._transact(p.MsgType.READ_ADC, p.AdcRead(channel, samples).pack())
        return p.AdcRead.unpack(resp)

    def write_pwm(self, pin: int, duty: int, freq_hz: int = 0, resolution: int = 0) -> None:
        """`freq_hz` 0 keeps the current frequency, `resolution` 0 the current
        resolution. Both are board-wide on Teensy, not per-pin."""
        self._check_pin(pin)
        self._transact(p.MsgType.WRITE_PWM, p.PwmWrite(pin, duty, freq_hz, resolution).pack())

    def set_stream(self, enable: bool, period_us: int = 0, channels: int | list[int] = 0) -> None:
        """Turn periodic telemetry on or off.

        `channels` is a bitmask, or a list of channel numbers for readability.
        """
        mask = channels if isinstance(channels, int) else sum(1 << ch for ch in channels)
        if enable:
            if period_us <= 0:
                raise ValueError("streaming needs a positive period_us")
            if mask == 0:
                raise ValueError("streaming needs at least one channel")
            if mask >> (p.MAX_ADC_CHANNEL + 1):
                raise ValueError(f"channel mask 0x{mask:X} exceeds A{p.MAX_ADC_CHANNEL}")
        self._transact(p.MsgType.SET_STREAM, p.StreamCfg(int(enable), period_us, mask).pack())

    # -- closed-loop control -----------------------------------------------

    def set_control(self, cfg: p.ControlCfg) -> None:
        """Push a whole controller configuration.

        Whole, not incremental: a partial update would need a field mask on the
        wire, and re-sending eleven floats costs less than a millisecond.
        """
        self._transact(p.MsgType.SET_CONTROL, cfg.pack())

    def control(self) -> p.ControlState:
        """What the loop is doing right now, including the split P/I/D terms."""
        return p.ControlState.unpack(self._transact(p.MsgType.GET_CONTROL))

    def autotune(self, cmd: p.AutotuneCmd) -> None:
        """Start or abort a relay autotune. Returns as soon as the device has
        accepted it -- poll `autotune_result()` for progress."""
        self._transact(p.MsgType.AUTOTUNE, cmd.pack())

    def autotune_result(self) -> p.AutotuneResult:
        return p.AutotuneResult.unpack(self._transact(p.MsgType.GET_AUTOTUNE))

    # -- telemetry ---------------------------------------------------------

    def stream(
        self,
        channels: int | list[int],
        period_us: int,
        *,
        duration: float | None = None,
        count: int | None = None,
    ) -> Iterator[p.Telemetry]:
        """Enable streaming and yield records until `duration` or `count`.

        Streaming is turned off again even if the caller breaks out early --
        otherwise the next command would have to wade through a backlog of
        telemetry, and the board would keep filling the USB buffer.
        """
        self.set_stream(True, period_us, channels)
        deadline = None if duration is None else time.monotonic() + duration
        seen = 0
        try:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    return
                if count is not None and seen >= count:
                    return
                records = self._drain_telemetry()
                if not records:
                    # A transport whose read() does not block (a simulated one)
                    # would otherwise spin a core flat between records.
                    time.sleep(0.001)
                    continue
                for record in records:
                    yield record
                    seen += 1
                    if count is not None and seen >= count:
                        return
        finally:
            self._quiesce_stream()

    def _drain_telemetry(self) -> list[p.Telemetry]:
        self._pump()
        out = list(self._telemetry)
        self._telemetry.clear()
        return out

    def _quiesce_stream(self) -> None:
        """Stop streaming and swallow whatever was already in flight, so the
        next command's response is not hiding behind a queue of telemetry."""
        try:
            self.set_stream(False)
        except (Timeout, p.NgsError):
            # Best effort: if the link is already broken, the caller will find
            # out from their next command with a clearer error than this one.
            return
        self._telemetry.clear()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _check_pin(pin: int) -> None:
        if not 0 <= pin <= p.MAX_DIGITAL_PIN:
            raise ValueError(f"pin {pin} outside 0..{p.MAX_DIGITAL_PIN}")
