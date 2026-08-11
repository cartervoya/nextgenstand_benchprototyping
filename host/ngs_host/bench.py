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
from contextlib import suppress
from dataclasses import dataclass, field, replace

from . import protocol as p
from .device import Device
from .store import TUNED_FIELDS, TuningRecord, TuningStore


class EstopLatched(Exception):
    """The bench cannot be brought up because the emergency stop is engaged.

    Its own type so a UI can say what to do about it instead of showing a
    traceback from whichever output it happened to try to drive first.
    """

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
class ControlSpec:
    """How a PWM output is closed around an analog input.

    The device runs the loop, but everything here is host knowledge: which
    sensor feeds which pump, what the gains mean, where the sensor is
    considered dead. The device is handed the two calibration numbers it needs
    and works in mL/min from there.
    """

    output: str  # PwmOutputSpec.name
    input: str  # AnalogInputSpec.name

    kp: float = 0.05
    ki: float = 0.02
    kd: float = 0.0

    #: Measurement low-pass. A second is generous, and this loop is allowed to
    #: be slow -- stability matters far more than speed here.
    filter_tau_s: float = 1.0
    #: No integration while the error is under this. Sized to the sensor noise,
    #: so the loop stops hunting once it is as close as the sensor can tell.
    deadband: float = 2.0
    #: Setpoint ramp rate. Full scale in ten seconds by default.
    setpoint_slew: float = 60.0
    output_slew: float = 25.0
    out_min: float = 0.0
    out_max: float = 100.0
    period_us: int = 20_000

    #: Margin below zero flow that means the sensor is dead rather than idle.
    #: Negative because on a 4-20 mA loop, under 4 mA reads below zero.
    fault_below: float = -25.0
    fault_check: bool = True


@dataclass(frozen=True, slots=True)
class BenchConfig:
    valves: tuple[ValveSpec, ...] = ()
    analogs: tuple[AnalogInputSpec, ...] = ()
    pwms: tuple[PwmOutputSpec, ...] = ()
    controls: tuple[ControlSpec, ...] = ()

    #: Host-silence watchdog, milliseconds. 0 disables it.
    #:
    #: Off by default, and switched on by the dashboards while they are
    #: supervising. A watchdog that fires whenever no host is connected would
    #: also undo a valve you deliberately set from a one-shot command and then
    #: walked away from -- which is ordinary bench use, not an emergency.
    watchdog_ms: int = 0

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
    controls=(
        # The pump chases the flow meter. Gains are deliberately timid
        # starting values -- run `ngs tune` on the real rig to replace them.
        ControlSpec(output="pump", input="flow"),
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
    #: None when no loop is configured, or when the poll failed.
    control: p.ControlState | None = None
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

    def __init__(
        self,
        device: Device,
        config: BenchConfig = BENCH_CONFIG,
        store: TuningStore | None = None,
    ) -> None:
        self.device = device
        self.config = config
        #: Where tuning is persisted. Pass a store to relocate it; the default
        #: is the repo's tuning.json.
        self.store = store if store is not None else TuningStore()
        self._serial: str | None = None
        self._loaded_tuning: TuningRecord | None = None
        self._pwm_percent: dict[str, float] = {s.name: s.default_percent for s in config.pwms}
        # What we last commanded, so a readback that disagrees can be flagged
        # rather than silently believed.
        self._valve_commanded: dict[str, bool] = {}
        # Live controller settings, seeded from the config and then edited by
        # the operator or replaced wholesale by an autotune. Held here because
        # the device has no notion of "the gains I was configured with before
        # someone switched to manual".
        self._control_cfg: dict[str, p.ControlCfg] = {
            spec.output: self._initial_control_cfg(spec) for spec in config.controls
        }

    # -- persisted tuning ---------------------------------------------------

    def board_serial(self) -> str:
        """The MCU serial, cached. Identifies the rig a tuning belongs to."""
        if self._serial is None:
            self._serial = self.device.info().serial_hex
        return self._serial

    def load_tuning(self) -> TuningRecord | None:
        """Adopt the board's own tuning.

        The device is the authority: its configuration lives in its NVM, so a
        fresh checkout, a different laptop, or no host config at all still
        drives the rig with the gains it was actually set up with. Gains only
        mean anything against a particular pump, line and flow meter, and the
        board is the thing bolted to those.

        The calibration is *not* taken from the device. That describes the
        wiring, it lives in BENCH_CONFIG, and letting a stale stored copy
        override it would silently rescale every reading.

        Idempotent, and called from every entry point -- a tuning that only
        loads down one code path is one you cannot trust to be in effect.
        """
        if not self.config.controls:
            return None

        try:
            on_board = self.device.control_cfg()
            state = self.device.control()
        except (p.NgsError, TimeoutError, OSError):
            return None

        if not state.stored:
            # Nothing was ever saved, so what the board reports is the
            # firmware's generic defaults. BENCH_CONFIG's are specific to this
            # rig and strictly better; adopting the generic ones would quietly
            # undo, say, the deadband sized to this flow meter's noise.
            self._loaded_tuning = TuningRecord(values={}, source="firmware defaults")
            return self._loaded_tuning

        values = {field: getattr(on_board, field) for field in TUNED_FIELDS}
        for name, cfg in self._control_cfg.items():
            self._control_cfg[name] = replace(cfg, **values)

        self._loaded_tuning = TuningRecord(values=values, source="board")
        return self._loaded_tuning

    def save_to_board(self, output: str = "pump") -> None:
        """Push the configuration and write it to the board's NVM.

        The board is the authority, so "save" means "save there". The host file
        is written too, but only as a record.
        """
        cfg = self.control_cfg(output)
        self.device.set_control(replace(cfg, mode=p.PumpMode.MANUAL))
        self.device.store_control()
        # Reapply whatever mode was actually in force; the line above deliberately
        # sent MANUAL so a save can never be what starts a pump.
        if cfg.mode == p.PumpMode.AUTO:
            self.device.set_control(cfg)

    def erase_board_tuning(self) -> None:
        """Forget the board's stored tuning. It falls back to firmware defaults
        on its next boot."""
        self.device.erase_control()

    def save_tuning(
        self,
        output: str = "pump",
        *,
        source: str = "manual",
        result: p.AutotuneResult | None = None,
    ) -> None:
        """Write the tuning to the board, and record it in the host file.

        The board copy is what gets read back; the file is a reviewable record
        so "kp changed on the 12th, from an autotune with Ku 0.52" stays a
        question git can answer. It is never read back as configuration -- one
        authority, or you eventually run gains you did not choose.
        """
        self.save_to_board(output)
        self.store.save(
            self.board_serial(),
            self.control_cfg(output),
            source=source,
            ku=None if result is None else result.ku,
            tu=None if result is None else result.tu,
            spread=None if result is None else result.spread,
        )

    @property
    def loaded_tuning(self) -> TuningRecord | None:
        """The record applied by `load_tuning`, for a UI to show provenance."""
        return self._loaded_tuning

    def forget_tuning(self) -> bool:
        """Discard the tuning: erased from the board, dropped from the record,
        and back to the configured defaults here."""
        with suppress(p.NgsError, TimeoutError, OSError):
            self.erase_board_tuning()
        dropped = self.store.forget(self.board_serial())
        self._loaded_tuning = None
        for spec in self.config.controls:
            self._control_cfg[spec.output] = self._initial_control_cfg(spec)
        return dropped

    # -- control configuration ---------------------------------------------

    def _control_spec(self, output: str) -> ControlSpec:
        for spec in self.config.controls:
            if spec.output == output:
                return spec
        known = ", ".join(s.output for s in self.config.controls) or "none configured"
        raise KeyError(f"no control loop drives {output!r}. Known: {known}")

    def calibration(self, analog: AnalogInputSpec, adc_bits: int = 12) -> tuple[float, float]:
        """The sensor's linear calibration as (units per count, count at zero).

        The device needs the loop's measurement in engineering units so the
        setpoint and gains mean something, but it has no business knowing what
        a flow meter is. These two numbers are the whole of what it is told;
        the calibration itself still lives here, in version control.
        """
        full_scale = float((1 << adc_bits) - 1)
        volts_per_count = ADC_FULL_SCALE_V / full_scale
        units_per_volt = (analog.value_max - analog.value_min) / (analog.v_max - analog.v_min)

        scale = volts_per_count * units_per_volt
        # value(counts) = scale * counts + intercept, so the zero crossing is
        # at -intercept / scale.
        intercept = analog.value_min - analog.v_min * units_per_volt
        return scale, -intercept / scale

    def _initial_control_cfg(self, spec: ControlSpec) -> p.ControlCfg:
        analog = self._analog(spec.input)
        scale, offset = self.calibration(analog)
        return p.ControlCfg(
            mode=p.PumpMode.MANUAL,
            channel=analog.channel,
            options=p.CtrlOpt.FAULT_CHECK if spec.fault_check else 0,
            setpoint=0.0,
            kp=spec.kp,
            ki=spec.ki,
            kd=spec.kd,
            out_min=spec.out_min,
            out_max=spec.out_max,
            filter_tau_s=spec.filter_tau_s,
            deadband=spec.deadband,
            setpoint_slew=spec.setpoint_slew,
            output_slew=spec.output_slew,
            cal_scale=scale,
            cal_offset=offset,
            fault_below=spec.fault_below,
            period_us=spec.period_us,
        )

    def control_cfg(self, output: str = "pump") -> p.ControlCfg:
        """The settings that would be pushed on the next mode change."""
        return self._control_cfg[self._control_spec(output).output]

    def set_control_cfg(self, cfg: p.ControlCfg, output: str = "pump") -> None:
        """Replace the settings and, if the loop is running, apply them now.

        Refuses while an autotune is in progress. The alternatives are both
        worse: pushing the config would cancel the experiment as a side effect
        of typing a gain, and doing nothing would silently discard the change
        until the next mode switch.
        """
        spec = self._control_spec(output)
        if self.autotune_result().running:
            raise ValueError("an autotune is running -- TX to abort it before changing gains")

        self._control_cfg[spec.output] = cfg
        if cfg.mode == p.PumpMode.AUTO:
            self.device.set_control(cfg)
        self.save_tuning(spec.output)

    def set_gains(
        self,
        kp: float | None = None,
        ki: float | None = None,
        kd: float | None = None,
        output: str = "pump",
    ) -> p.ControlCfg:
        """Update gains, keeping everything else. Applied live in auto."""
        cfg = replace(
            self.control_cfg(output),
            kp=self.control_cfg(output).kp if kp is None else kp,
            ki=self.control_cfg(output).ki if ki is None else ki,
            kd=self.control_cfg(output).kd if kd is None else kd,
        )
        for value, name in ((cfg.kp, "kp"), (cfg.ki, "ki"), (cfg.kd, "kd")):
            if value < 0.0:
                raise ValueError(f"{name} must not be negative -- that is positive feedback")
        self.set_control_cfg(cfg, output)
        return cfg

    def set_pump_mode(
        self, auto: bool, setpoint: float | None = None, output: str = "pump"
    ) -> None:
        """Switch the pump between manual and closed-loop control.

        Going to auto is bumpless -- the device seeds its integrator from the
        duty already applied, so the pump does not step when the mode changes.
        Coming back to manual leaves the output where the loop left it, which
        is why this does not force it to zero: an operator taking manual
        control of a running pump wants it to keep running.
        """
        spec = self._control_spec(output)
        analog = self._analog(spec.input)

        if auto and setpoint is not None:
            limit = analog.value_max
            if not 0.0 <= setpoint <= limit:
                raise ValueError(
                    f"setpoint {setpoint:g} outside the sensor's 0-{limit:g} {analog.unit}"
                )

        cfg = replace(
            self.control_cfg(output),
            mode=p.PumpMode.AUTO if auto else p.PumpMode.MANUAL,
            setpoint=self.control_cfg(output).setpoint if setpoint is None else setpoint,
        )
        self._control_cfg[spec.output] = cfg
        self.device.set_control(cfg)

        if not auto:
            # Keep the cached manual percentage in step with whatever the loop
            # left behind, so the display and a later `P?` agree with reality.
            self._pwm_percent[spec.output] = self.device.control().output

    def set_setpoint(self, value: float, output: str = "pump") -> None:
        self.set_pump_mode(True, value, output)

    def control_state(self) -> p.ControlState:
        return self.device.control()

    # -- autotune ----------------------------------------------------------

    def start_autotune(
        self,
        setpoint: float,
        *,
        amplitude: float = 10.0,
        hysteresis: float | None = None,
        cycles: int = 4,
        rule: int = p.TuningRule.TYREUS_LUYBEN,
        timeout_s: float = 180.0,
        output: str = "pump",
    ) -> None:
        """Kick off a relay autotune around `setpoint`.

        The default hysteresis is derived from the configured deadband rather
        than left at zero: a relay with no hysteresis on a noisy signal
        switches on the noise instead of on the process, and returns a
        confidently wrong ultimate period.
        """
        spec = self._control_spec(output)
        if hysteresis is None:
            hysteresis = max(spec.deadband, 1.0)

        self.device.autotune(
            p.AutotuneCmd(
                action=p.AutotuneAction.START,
                cycles=cycles,
                rule=rule,
                setpoint=setpoint,
                amplitude=amplitude,
                hysteresis=hysteresis,
                timeout_ms=int(timeout_s * 1000),
            )
        )

    def abort_autotune(self) -> None:
        self.device.autotune(p.AutotuneCmd(action=p.AutotuneAction.ABORT))

    def autotune_result(self) -> p.AutotuneResult:
        return self.device.autotune_result()

    def adopt_autotune(self, output: str = "pump") -> p.AutotuneResult:
        """Take the gains a finished autotune suggested. Refuses anything the
        device did not finish cleanly, so a timed-out run cannot be adopted by
        accident."""
        result = self.autotune_result()
        if result.state != p.AutotuneState.DONE:
            raise ValueError(f"autotune did not finish cleanly ({result.state_name})")
        self.set_gains(result.kp, result.ki, result.kd, output=output)
        # Re-saved with the experiment's own numbers attached, so a suspicious
        # set of gains can be traced back to the run that produced them.
        self.save_tuning(output, source="autotune", result=result)
        return result

    # -- setup -------------------------------------------------------------

    def initialize(self, watchdog_ms: int | None = None, *, clear_estop: bool = False) -> None:
        """Put the bench in a known safe state: valves closed, PWM at its
        default, PWM frequency and resolution configured.

        Safe to call on an already-running bench -- it is also how you recover
        after the board resets underneath you.
        """
        # A latched board refuses every write below. Say so in one line rather
        # than failing at whichever output happens to be configured first --
        # and do not clear it silently: an emergency stop that any startup can
        # undo is not one.
        status = self.device.status()
        if status.estopped:
            if not clear_estop:
                raise EstopLatched(
                    f"the emergency stop is latched ({status.estop_source_name.lower()}). "
                    "Nothing can be driven until it is cleared: run `ngs estop --clear`, "
                    "or send EC."
                )
            self.clear_estop()

        # Saved tuning first: everything below configures the device, and it
        # should be configured with the gains this rig was actually tuned to.
        self.load_tuning()

        # Register where every output belongs in an emergency before driving
        # anything, so the device can get there without us from this point on.
        self.register_safe_state(watchdog_ms)

        # Same reason as stop(): a running loop would refuse the PWM writes.
        for spec in self.config.controls:
            self.set_pump_mode(False, output=spec.output)
        for valve in self.config.valves:
            self.set_valve(valve.name, False)
        for pwm in self.config.pwms:
            self.set_pwm(pwm.name, pwm.default_percent)

    def stop(self) -> None:
        """Everything to its safe default. Wired to the `X` command, and worth
        calling from a `finally` in any script that drives the pump.

        Drops any closed loop to manual *first*. The device refuses a manual
        PWM write while the loop owns the output -- correctly, since the loop
        would overwrite it on the next tick -- so without this the emergency
        stop fails with BUSY at exactly the moment it is needed.
        """
        for spec in self.config.controls:
            # Best effort: a loop we cannot switch off must not stop us from
            # zeroing the outputs below.
            with suppress(p.NgsError, KeyError):
                self.set_pump_mode(False, output=spec.output)
        for pwm in self.config.pwms:
            self.set_pwm(pwm.name, pwm.default_percent)
        for valve in self.config.valves:
            self.set_valve(valve.name, False)

    # -- emergency stop ----------------------------------------------------

    def register_safe_state(self, watchdog_ms: int | None = None) -> None:
        """Tell the device where every output belongs in an emergency.

        Sent up front, so the device can reach a safe state on its own -- when
        the host has crashed, or the cable is out. That is the difference
        between an emergency stop and a convenience command.
        """
        timeout = self.config.watchdog_ms if watchdog_ms is None else watchdog_ms
        self.device.clear_safe_table(watchdog_ms=timeout)

        index = 0
        for valve in self.config.valves:
            self.device.set_safe_entry(
                p.SafeEntry(
                    index=index,
                    kind=p.SafeKind.GPIO,
                    pin=valve.pin,
                    value=valve.level_for(False),
                    watchdog_ms=timeout,
                )
            )
            index += 1

        for pwm in self.config.pwms:
            self.device.set_safe_entry(
                p.SafeEntry(
                    index=index,
                    kind=p.SafeKind.PWM,
                    pin=pwm.pin,
                    value=pwm.to_counts(pwm.default_percent),
                    resolution=pwm.resolution,
                    watchdog_ms=timeout,
                )
            )
            index += 1

    def estop(self) -> None:
        """Latch the emergency stop.

        One message. The device drives every output to its safe value itself,
        so this cannot half-succeed the way a sequence of individual commands
        can -- and it stays latched until explicitly cleared.

        Registers the safe-state table first if the device does not have one.
        Without that check, an emergency stop issued from a process that never
        called `initialize()` -- `ngs estop` against a freshly booted board,
        say -- would latch correctly and drive nothing at all, which is the
        worst possible way to fail. The extra status read costs a fraction of
        a millisecond against the 0.3 ms the stop itself takes.
        """
        if self.device.status().safe_entries == 0:
            self.register_safe_state()

        self.device.estop()
        for pwm in self.config.pwms:
            self._pwm_percent[pwm.name] = pwm.default_percent
        for valve in self.config.valves:
            self._valve_commanded[valve.name] = False

    def clear_estop(self) -> None:
        """Release the latch. Nothing moves -- outputs stay safe until
        commanded."""
        self.device.clear_estop()

    def is_estopped(self) -> bool:
        return self.device.status().estopped

    # -- outputs -----------------------------------------------------------

    def set_valve(self, name: str, is_open: bool) -> None:
        spec = self._valve(name)
        self.device.set_gpio(spec.pin, spec.level_for(is_open), p.PinMode.OUTPUT)
        self._valve_commanded[spec.name] = is_open

    def set_all_valves(self, is_open: bool) -> list[str]:
        """Drive every valve the same way. Returns the names, in order.

        Not atomic on the device -- that is what the emergency stop is for.
        This is the ordinary convenience of not typing `V1O;V2O;`, and it fails
        the same way any other command does if one of them is refused.
        """
        for spec in self.config.valves:
            self.set_valve(spec.name, is_open)
        return [spec.name for spec in self.config.valves]

    def running_outputs(self) -> list[str]:
        """PWM outputs that are not at zero right now, by description.

        Used to warn before closing every valve: a pump driving into a closed
        line is dead-heading, which is the one ordering mistake this codebase
        keeps taking care to avoid.
        """
        running = []
        for spec in self.config.pwms:
            if any(c.output == spec.name for c in self.config.controls):
                state = self.control_state()
                if state.mode != p.PumpMode.MANUAL or state.output > 0.0:
                    running.append(spec.description or spec.name)
                    continue
            elif self._pwm_percent.get(spec.name, 0.0) > 0.0:
                running.append(spec.description or spec.name)
        return running

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
            control = self.device.control() if self.config.controls else None
            pwms = {
                s.name: PwmReading(
                    s,
                    # In auto the loop owns the duty; the cached manual value
                    # would be stale and quietly wrong on screen.
                    control.output
                    if control is not None
                    and control.mode != p.PumpMode.MANUAL
                    and any(c.output == s.name for c in self.config.controls)
                    else self._pwm_percent[s.name],
                )
                for s in self.config.pwms
            }
            status = self.device.status() if include_status else None
            # Deliberately not re-read here: the loop runs at 50 Hz, so a second
            # query would return a slightly different output and the snapshot
            # would show a duty that disagrees with its own control state.
        except (p.NgsError, OSError, TimeoutError) as exc:
            return Snapshot(monotonic=time.monotonic(), error=f"{type(exc).__name__}: {exc}")

        return Snapshot(
            monotonic=time.monotonic(),
            valves=valves,
            analogs=analogs,
            pwms=pwms,
            status=status,
            control=control,
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
