"""The bench: what is actually wired to the board, and what its numbers mean.

Everything above this line (protocol, link, device) is generic -- it knows
about pins and ADC counts and nothing else. Everything the *bench* knows lives
here: which pin a valve is on, what 0.6 V means on the flow sensor, what duty
cycle the pump wants. That split is deliberate, and it is why adding hardware
does not mean touching the firmware.

To add a channel, add a spec to BENCH_CONFIG. The dashboard, the command
language and the CLI all read the config, so a new entry shows up in all three
with no further edits.

Firmware changes are only needed for genuinely new *capabilities* (a new bus, a
new measurement mode) -- not for new instances of what already exists.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from . import protocol as p
from .device import Device

# --------------------------------------------------------------------------
# Board facts
# --------------------------------------------------------------------------

#: Teensy 4.1 analog pins, digital pin number -> ADC channel index. The wire
#: protocol speaks channels (A0 == 0) but a schematic speaks pin numbers, so
#: the config below is written in pin numbers and translated here.
TEENSY41_ANALOG_PINS: dict[int, int] = {
    14: 0, 15: 1, 16: 2, 17: 3, 18: 4, 19: 5, 20: 6, 21: 7, 22: 8,
    23: 9, 24: 10, 25: 11, 26: 12, 27: 13, 38: 14, 39: 15, 40: 16, 41: 17,
}  # fmt: skip

#: Teensy 4.1 pins with a FlexPWM or QuadTimer output behind them. Every other
#: pin silently does nothing when written with analogWrite, which is a
#: miserable thing to debug with a meter, so the config is checked against this.
TEENSY41_PWM_PINS = frozenset(
    {*range(0, 16), 18, 19, 22, 23, 24, 25, 28, 29, 33, 36, 37, *range(42, 48), *range(51, 55)}
)

#: Teensy 4.x ADC full scale. The reference is the 3.3 V rail -- there is no
#: internal reference option on i.MXRT, so a sensor that swings above this
#: needs a divider, and one that stays well below it wastes resolution.
ADC_FULL_SCALE_V = 3.3

#: Nominal core clock. FlexPWM divides it, so it sets the frequency ceiling
#: for a given resolution: F_CPU / 2^bits.
F_CPU_HZ = 600_000_000

#: The firmware's digital pin ceiling (ngs_board.h). Restated rather than
#: imported so this module stays readable as the bench's own description.
MAX_DIGITAL_PIN = p.MAX_DIGITAL_PIN


# --------------------------------------------------------------------------
# Channel specs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValveSpec:
    """A solenoid valve on a digital output.

    `open_level` is the logic level that *opens* the valve, so a normally-open
    valve or an inverting driver is a one-line change here rather than a pile
    of `not` operators at every call site.
    """

    name: str
    code: str  # what you type in the command line, e.g. "V1"
    pin: int
    open_level: int = 1
    description: str = ""

    def level_for(self, is_open: bool) -> int:
        return self.open_level if is_open else 1 - self.open_level

    def is_open(self, level: int) -> bool:
        return level == self.open_level


@dataclass(frozen=True, slots=True)
class AnalogInputSpec:
    """A linear sensor on an analog input.

    Two voltages and the engineering values they correspond to. For a 4-20 mA
    loop across a sense resistor, `v_min`/`v_max` are the 4 mA and 20 mA
    voltages -- which also makes "below v_min" meaningful: a healthy loop never
    goes under 4 mA, so it means a broken wire or an unpowered transmitter, not
    a low reading.
    """

    name: str
    code: str
    pin: int
    unit: str
    v_min: float
    v_max: float
    value_min: float
    value_max: float
    #: Reading this far below v_min is reported as a fault rather than a value.
    #: Sized to clear ADC noise and transmitter tolerance, not to be clever.
    fault_margin_v: float = 0.1
    #: Averaged on the device, so this costs no extra round trips.
    samples: int = 8
    description: str = ""

    @property
    def channel(self) -> int:
        try:
            return TEENSY41_ANALOG_PINS[self.pin]
        except KeyError:
            raise ValueError(f"pin {self.pin} is not an analog input on Teensy 4.1") from None

    def voltage(self, raw: int, resolution: int) -> float:
        if resolution <= 0:
            return 0.0
        return raw / float((1 << resolution) - 1) * ADC_FULL_SCALE_V

    def to_value(self, volts: float) -> float:
        """Volts to engineering units. Extrapolates rather than clamping: a
        reading slightly over range is real information, and clamping it would
        hide a miscalibrated span."""
        span_v = self.v_max - self.v_min
        span_value = self.value_max - self.value_min
        return self.value_min + (volts - self.v_min) / span_v * span_value

    def is_faulted(self, volts: float) -> bool:
        return volts < self.v_min - self.fault_margin_v


@dataclass(frozen=True, slots=True)
class PwmOutputSpec:
    """A PWM output driven as a 0-100 % setpoint.

    `freq_hz` matters when the output is filtered to DC: it has to sit far
    enough above the filter's corner that the ripple is gone, while staying
    low enough that the resolution below is still achievable. On a 600 MHz
    Teensy 4.1 the FlexPWM ceiling is F_CPU / 2^bits, i.e. ~146 kHz at 12 bits,
    so 50 kHz has plenty of headroom.
    """

    name: str
    code: str
    pin: int
    freq_hz: int = 50_000
    resolution: int = 12
    #: Safe value applied at startup and by the stop command.
    default_percent: float = 0.0
    description: str = ""

    @property
    def max_counts(self) -> int:
        """Full scale in counts. 12-bit full scale is 4095, not 4096 -- the
        firmware rejects a duty the resolution cannot express."""
        return (1 << self.resolution) - 1

    def to_counts(self, percent: float) -> int:
        if not 0.0 <= percent <= 100.0:
            raise ValueError(f"{self.name}: {percent}% outside 0-100%")
        return round(percent / 100.0 * self.max_counts)

    def to_percent(self, counts: int) -> float:
        return counts / self.max_counts * 100.0


@dataclass(frozen=True, slots=True)
class BenchConfig:
    valves: tuple[ValveSpec, ...] = ()
    analogs: tuple[AnalogInputSpec, ...] = ()
    pwms: tuple[PwmOutputSpec, ...] = ()

    def by_code(self) -> dict[str, ValveSpec | AnalogInputSpec | PwmOutputSpec]:
        """Command code -> spec, for the parser and the CLI. Codes are matched
        case-insensitively, so they are stored uppercase."""
        out: dict[str, ValveSpec | AnalogInputSpec | PwmOutputSpec] = {}
        for spec in (*self.valves, *self.analogs, *self.pwms):
            code = spec.code.upper()
            if code in out:
                raise ValueError(f"duplicate command code {code!r} in the bench config")
            out[code] = spec
        return out

    def by_name(self, name: str) -> ValveSpec | AnalogInputSpec | PwmOutputSpec:
        for spec in (*self.valves, *self.analogs, *self.pwms):
            if spec.name == name:
                return spec
        raise KeyError(name)

    def validate(self) -> None:
        """Check the config against what the board can actually do.

        Called from the tests rather than at import: a bad edit here should
        fail a test run, not stop the CLI from starting and take the
        diagnostic commands down with it.
        """
        self.by_code()  # raises on duplicate codes

        for valve in self.valves:
            if not 0 <= valve.pin <= MAX_DIGITAL_PIN:
                raise ValueError(f"{valve.name}: pin {valve.pin} outside 0..{MAX_DIGITAL_PIN}")

        for analog in self.analogs:
            analog.channel  # noqa: B018 -- raises if the pin has no ADC
            if analog.v_max <= analog.v_min:
                raise ValueError(f"{analog.name}: v_max must exceed v_min")
            if analog.v_max > ADC_FULL_SCALE_V:
                raise ValueError(
                    f"{analog.name}: {analog.v_max} V exceeds the {ADC_FULL_SCALE_V} V "
                    "reference -- the signal needs a divider"
                )

        for pwm in self.pwms:
            if pwm.pin not in TEENSY41_PWM_PINS:
                raise ValueError(f"{pwm.name}: pin {pwm.pin} has no PWM peripheral")
            if not 1 <= pwm.resolution <= 16:
                raise ValueError(f"{pwm.name}: resolution {pwm.resolution} outside 1..16")
            ceiling = F_CPU_HZ / (1 << pwm.resolution)
            if pwm.freq_hz > ceiling:
                raise ValueError(
                    f"{pwm.name}: {pwm.freq_hz} Hz at {pwm.resolution}-bit exceeds the "
                    f"{ceiling:.0f} Hz ceiling -- lower the frequency or the resolution"
                )


# --------------------------------------------------------------------------
# The bench as currently wired
#
# This block is the wiring diagram in executable form. Keep it in sync with
# the physical bench and let everything else derive from it.
# --------------------------------------------------------------------------

BENCH_CONFIG = BenchConfig(
    valves=(
        ValveSpec(name="valve1", code="V1", pin=32, description="Valve 1"),
        ValveSpec(name="valve2", code="V2", pin=31, description="Valve 2"),
    ),
    analogs=(
        AnalogInputSpec(
            name="flow",
            code="F",
            pin=27,  # A13
            unit="mL/min",
            # 4-20 mA across a 150 R sense resistor: 4 mA -> 0.6 V, 20 mA -> 3.0 V.
            v_min=0.6,
            v_max=3.0,
            value_min=0.0,
            value_max=600.0,
            description="Flow meter (4-20 mA)",
        ),
    ),
    pwms=(
        PwmOutputSpec(
            name="pump",
            code="P",
            pin=33,
            # ~50 kHz: >25x the 1-2 kHz RC corner, so the ripple is attenuated
            # to nothing, and well under the 146 kHz ceiling for 12-bit.
            freq_hz=50_000,
            resolution=12,
            description="Pump speed",
        ),
    ),
)


# --------------------------------------------------------------------------
# Readings
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValveReading:
    spec: ValveSpec
    #: Read back from the pin, not remembered from the last command.
    is_open: bool
    #: What this host last commanded, or None if it has not driven the valve
    #: since it connected.
    commanded: bool | None = None

    @property
    def mismatch(self) -> bool:
        """The pin disagrees with the last command we sent.

        Almost always means the board rebooted -- a brownout when a solenoid
        kicks in is the classic cause -- and came back with its outputs at the
        power-on default. Worth shouting about: the bench is not in the state
        the operator thinks it is.
        """
        return self.commanded is not None and self.commanded != self.is_open

    @property
    def text(self) -> str:
        state = "OPEN" if self.is_open else "CLOSED"
        if self.mismatch:
            return f"{state} (commanded {'OPEN' if self.commanded else 'CLOSED'}!)"
        return state


@dataclass(frozen=True, slots=True)
class AnalogReading:
    spec: AnalogInputSpec
    raw: int
    resolution: int
    volts: float
    value: float
    faulted: bool

    @property
    def text(self) -> str:
        if self.faulted:
            return f"FAULT ({self.volts:.3f} V, loop under 4 mA?)"
        return f"{self.value:.1f} {self.spec.unit}"


@dataclass(frozen=True, slots=True)
class PwmReading:
    spec: PwmOutputSpec
    percent: float

    @property
    def text(self) -> str:
        return f"{self.percent:.1f} %"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One poll of the whole bench."""

    monotonic: float
    valves: dict[str, ValveReading] = field(default_factory=dict)
    analogs: dict[str, AnalogReading] = field(default_factory=dict)
    pwms: dict[str, PwmReading] = field(default_factory=dict)
    status: p.Status | None = None
    #: Set when the poll itself failed; the readings above are then stale.
    error: str | None = None

    @property
    def mismatched_valves(self) -> list[ValveReading]:
        """Valves whose pin disagrees with what we commanded -- see
        `ValveReading.mismatch`. Non-empty means the bench is not in the state
        the operator believes it is in."""
        return [reading for reading in self.valves.values() if reading.mismatch]


# --------------------------------------------------------------------------
# Bench
# --------------------------------------------------------------------------


class Bench:
    """Named bench hardware on top of a `Device`.

    PWM setpoints are held here rather than read back, because the board has no
    way to report a duty cycle -- the protocol writes PWM, it does not read it.
    Valve states *are* read back every poll: an output pin reads its own drive
    level, so a device reset shows up as valves reporting closed while this
    host still thinks they are open, which is exactly the failure worth seeing.
    """

    def __init__(self, device: Device, config: BenchConfig = BENCH_CONFIG) -> None:
        self.device = device
        self.config = config
        self._pwm_percent: dict[str, float] = {s.name: s.default_percent for s in config.pwms}
        # What we last commanded, so a readback that disagrees can be flagged
        # rather than silently believed.
        self._valve_commanded: dict[str, bool] = {}

    # -- setup -------------------------------------------------------------

    def initialize(self) -> None:
        """Put the bench in a known safe state: valves closed, PWM at its
        default, PWM frequency and resolution configured.

        Safe to call on an already-running bench -- it is also how you recover
        after the board resets underneath you.
        """
        for valve in self.config.valves:
            self.set_valve(valve.name, False)
        for pwm in self.config.pwms:
            self.set_pwm(pwm.name, pwm.default_percent)

    def stop(self) -> None:
        """Everything to its safe default. Wired to the `X` command, and worth
        calling from a `finally` in any script that drives the pump."""
        for pwm in self.config.pwms:
            self.set_pwm(pwm.name, pwm.default_percent)
        for valve in self.config.valves:
            self.set_valve(valve.name, False)

    # -- outputs -----------------------------------------------------------

    def set_valve(self, name: str, is_open: bool) -> None:
        spec = self._valve(name)
        self.device.set_gpio(spec.pin, spec.level_for(is_open), p.PinMode.OUTPUT)
        self._valve_commanded[spec.name] = is_open

    def toggle_valve(self, name: str) -> bool:
        """Flip the valve and return its new state."""
        spec = self._valve(name)
        now_open = self.read_valve(spec.name).is_open
        self.set_valve(spec.name, not now_open)
        return not now_open

    def set_pwm(self, name: str, percent: float) -> None:
        """Set a PWM output as a percentage.

        The frequency and resolution ride along with every write. That is one
        extra field on a message we are already sending, and it means a board
        that reset mid-run comes back at the right frequency instead of the
        Teensy default -- which, under an RC filter, would show up as a pump
        that quietly stops responding properly rather than as an error.
        """
        spec = self._pwm(name)
        self.device.write_pwm(
            spec.pin,
            duty=spec.to_counts(percent),
            freq_hz=spec.freq_hz,
            resolution=spec.resolution,
        )
        self._pwm_percent[spec.name] = percent

    def pwm_percent(self, name: str) -> float:
        return self._pwm_percent[self._pwm(name).name]

    # -- inputs ------------------------------------------------------------

    def read_valve(self, name: str) -> ValveReading:
        spec = self._valve(name)
        level = int(self.device.get_gpio(spec.pin, p.PinMode.OUTPUT))
        return ValveReading(spec, spec.is_open(level), self._valve_commanded.get(spec.name))

    def read_analog(self, name: str) -> AnalogReading:
        spec = self._analog(name)
        resp = self.device.read_adc(spec.channel, spec.samples)
        volts = spec.voltage(resp.raw, resp.resolution)
        return AnalogReading(
            spec=spec,
            raw=resp.raw,
            resolution=resp.resolution,
            volts=volts,
            value=spec.to_value(volts),
            faulted=spec.is_faulted(volts),
        )

    def poll(self, *, include_status: bool = True) -> Snapshot:
        """Read the whole bench once.

        A link failure is returned as `Snapshot.error` rather than raised: the
        dashboard polls forever and must survive a cable being nudged, and a
        caller that wants the exception can check the field.
        """
        try:
            valves = {v.name: self.read_valve(v.name) for v in self.config.valves}
            analogs = {a.name: self.read_analog(a.name) for a in self.config.analogs}
            pwms = {
                s.name: PwmReading(s, self._pwm_percent[s.name]) for s in self.config.pwms
            }
            status = self.device.status() if include_status else None
        except (p.NgsError, OSError, TimeoutError) as exc:
            return Snapshot(monotonic=time.monotonic(), error=f"{type(exc).__name__}: {exc}")

        return Snapshot(
            monotonic=time.monotonic(),
            valves=valves,
            analogs=analogs,
            pwms=pwms,
            status=status,
        )

    # -- lookup ------------------------------------------------------------

    def _valve(self, name: str) -> ValveSpec:
        return self._lookup(name, self.config.valves, "valve")

    def _pwm(self, name: str) -> PwmOutputSpec:
        return self._lookup(name, self.config.pwms, "PWM output")

    def _analog(self, name: str) -> AnalogInputSpec:
        return self._lookup(name, self.config.analogs, "analog input")

    @staticmethod
    def _lookup(name: str, specs: Sequence, kind: str):
        for spec in specs:
            if spec.name == name:
                return spec
        known = ", ".join(s.name for s in specs) or "none configured"
        raise KeyError(f"no {kind} named {name!r}. Known: {known}")
