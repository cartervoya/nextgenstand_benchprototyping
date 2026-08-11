"""A Python mirror of the firmware's control loop (firmware/lib/ngs/ngs_control.c).

This exists so the simulated device behaves like the real one -- so the UI, the
command language and the tuning workflow can all be exercised with no board.

It is a *mirror*, and duplicated logic drifts. Two things hold it honest:

  - host/tests/test_control_vector.py runs a fixed scenario through this code
    and asserts an exact output sequence. firmware/test/test_ngs/test_control.h
    runs the same numbers through the C. If either implementation changes
    behaviour, one of them fails.
  - The C is authoritative. When they disagree, this file is wrong.

Read ngs_control.h for why the loop is built the way it is; the reasoning is
not repeated here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import protocol as p

MEDIAN_TAPS = 5
AT_MAX_CYCLES = 8


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def _apply_deadzone(cfg: p.ControlCfg, demand: float) -> float:
    """Demand 0-100 mapped onto deadzone..100. See ngs_control.c."""
    if cfg.out_deadzone <= 0.0 or demand <= 0.0:
        return demand
    return cfg.out_deadzone + demand * (100.0 - cfg.out_deadzone) / 100.0


def _remove_deadzone(cfg: p.ControlCfg, duty: float) -> float:
    if cfg.out_deadzone <= 0.0 or duty <= 0.0:
        return duty
    if duty <= cfg.out_deadzone:
        return 0.0
    return (duty - cfg.out_deadzone) * 100.0 / (100.0 - cfg.out_deadzone)


def _slew(from_: float, to: float, max_step: float) -> float:
    """0 means no limit, not "never move" -- same convention as the C."""
    if max_step <= 0.0:
        return to
    delta = to - from_
    if delta > max_step:
        return from_ + max_step
    if delta < -max_step:
        return from_ - max_step
    return to


@dataclass
class _Autotune:
    state: int = p.AutotuneState.IDLE
    fail_reason: int = p.AutotuneFail.NONE
    rule: int = p.TuningRule.TYREUS_LUYBEN
    want_cycles: int = 4
    relay_high: bool = True

    setpoint: float = 0.0
    amplitude: float = 0.0
    hysteresis: float = 0.0
    bias: float = 0.0

    started_us: int = 0
    timeout_us: int = 0
    last_cross_us: int = 0

    periods_us: list[int] = field(default_factory=list)
    peaks: list[float] = field(default_factory=list)
    troughs: list[float] = field(default_factory=list)

    peak_acc: float = 0.0
    trough_acc: float = 0.0

    settle_us: int = 0
    settle_min: float = 0.0
    settle_max: float = 0.0
    noise: float = 0.0

    ku: float = 0.0
    tu: float = 0.0
    measured_amplitude: float = 0.0
    spread: float = 0.0
    kp: float = 0.0
    ki: float = 0.0
    kd: float = 0.0

    @property
    def cycles(self) -> int:
        return len(self.periods_us)


class FakeController:
    """The firmware controller, in Python. See the module docstring."""

    def __init__(self) -> None:
        self.cfg = p.ControlCfg()
        self.mode = p.PumpMode.MANUAL
        self.flags = 0

        self._taps: list[float] = []
        self.measurement = 0.0
        self.measurement_raw = 0.0
        self._primed = False
        self.seed_setpoint = False

        self.setpoint_active = 0.0
        self.demand = 0.0
        self.integral = 0.0
        self.last_measurement = 0.0
        self.output = 0.0
        self.p_term = 0.0
        self.i_term = 0.0
        self.d_term = 0.0

        self.next_us = 0
        self.updates = 0
        self.fault_count = 0

        self.at = _Autotune()

    # -- conversion --------------------------------------------------------

    def convert(self, raw: int) -> float:
        return (raw - self.cfg.cal_offset) * self.cfg.cal_scale

    # -- configuration -----------------------------------------------------

    def configure(self, cfg: p.ControlCfg, current_output: float) -> int:
        """Returns an ErrCode, or 0. Mirrors validate() in the C."""
        if cfg.mode > p.PumpMode.AUTO:
            return p.ErrCode.BAD_ARGUMENT
        if cfg.channel > p.MAX_ADC_CHANNEL:
            return p.ErrCode.BAD_ARGUMENT
        if cfg.out_min < 0.0 or cfg.out_max > 100.0 or cfg.out_min >= cfg.out_max:
            return p.ErrCode.BAD_ARGUMENT
        if not 1000 <= cfg.period_us <= 1_000_000:
            return p.ErrCode.BAD_ARGUMENT
        if any(math.isnan(v) for v in (cfg.kp, cfg.ki, cfg.kd, cfg.setpoint)):
            return p.ErrCode.BAD_ARGUMENT
        if cfg.kp < 0.0 or cfg.ki < 0.0 or cfg.kd < 0.0:
            return p.ErrCode.BAD_ARGUMENT
        if cfg.filter_tau_s < 0.0 or cfg.deadband < 0.0:
            return p.ErrCode.BAD_ARGUMENT
        if not 0.0 <= cfg.out_deadzone < 100.0:
            return p.ErrCode.BAD_ARGUMENT
        if cfg.cal_scale == 0.0:
            return p.ErrCode.BAD_ARGUMENT

        if self.at.state in (p.AutotuneState.SETTLING, p.AutotuneState.RELAY):
            self.at.state = p.AutotuneState.FAILED
            self.at.fail_reason = p.AutotuneFail.ABORTED

        was = self.mode
        self.cfg = cfg
        self.mode = cfg.mode

        if cfg.mode == p.PumpMode.AUTO:
            if was != p.PumpMode.AUTO:
                self.output = current_output
                self.demand = _remove_deadzone(cfg, current_output)
                self.integral = self.demand
                # NOT seeded from self.measurement here -- it is stale until the
                # loop has ticked at least once. See seed_setpoint in
                # ngs_control.h.
                self.seed_setpoint = True
                self.updates = 0
                self.flags = 0
            self.next_us = 0
        else:
            self.output = current_output

        return 0

    def note_manual_output(self, output_pct: float) -> None:
        if self.mode == p.PumpMode.MANUAL:
            self.output = output_pct

    # -- measurement -------------------------------------------------------

    def _filter(self, sample: float, dt: float) -> float:
        self._taps.append(sample)
        if len(self._taps) > MEDIAN_TAPS:
            self._taps.pop(0)

        # NOT statistics.median: on an even-length window that averages the two
        # middle samples, while the C takes sorted[count/2] -- the upper middle.
        # They differ until the window fills, which is exactly when the loop is
        # starting up and the difference matters most.
        med = sorted(self._taps)[len(self._taps) // 2]

        tau = self.cfg.filter_tau_s
        if tau <= 0.0 or not self._primed:
            self.measurement = med
            self._primed = True
            return med

        alpha = dt / (tau + dt)
        self.measurement += (med - self.measurement) * alpha
        return self.measurement

    # -- the loop ----------------------------------------------------------

    def _enter_fault(self) -> float:
        self.fault_count += 1
        self.flags |= p.CtrlFlag.FAULT
        self.mode = p.PumpMode.MANUAL
        self.integral = 0.0
        self.output = self.cfg.out_min
        if self.at.state in (p.AutotuneState.SETTLING, p.AutotuneState.RELAY):
            self.at.state = p.AutotuneState.FAILED
            self.at.fail_reason = p.AutotuneFail.SENSOR
        return self.output

    def _run_pid(self, dt: float) -> float:
        cfg = self.cfg

        self.setpoint_active = _slew(self.setpoint_active, cfg.setpoint, cfg.setpoint_slew * dt)
        if self.setpoint_active != cfg.setpoint:
            self.flags |= p.CtrlFlag.SLEWING
        else:
            self.flags &= ~p.CtrlFlag.SLEWING

        error = self.setpoint_active - self.measurement
        self.p_term = cfg.kp * error

        if cfg.kd > 0.0 and dt > 0.0:
            self.d_term = -cfg.kd * (self.measurement - self.last_measurement) / dt
        else:
            self.d_term = 0.0
        self.last_measurement = self.measurement

        integrate = True
        if cfg.deadband > 0.0 and abs(error) < cfg.deadband:
            integrate = False

        unsaturated = self.p_term + self.integral + self.d_term
        if (unsaturated >= cfg.out_max and error > 0.0) or (
            unsaturated <= cfg.out_min and error < 0.0
        ):
            integrate = False
            self.flags |= p.CtrlFlag.WINDUP
        else:
            self.flags &= ~p.CtrlFlag.WINDUP

        if integrate:
            self.integral += cfg.ki * error * dt
            self.integral = _clamp(self.integral, cfg.out_min, cfg.out_max)
        self.i_term = self.integral

        raw_output = self.p_term + self.i_term + self.d_term
        limited = _clamp(raw_output, cfg.out_min, cfg.out_max)
        if limited != raw_output:
            self.flags |= p.CtrlFlag.SATURATED
        else:
            self.flags &= ~p.CtrlFlag.SATURATED

        self.demand = _slew(self.demand, limited, cfg.output_slew * dt)
        self.demand = _clamp(self.demand, cfg.out_min, cfg.out_max)
        self.output = _apply_deadzone(cfg, self.demand)
        return self.output

    def tick(self, now_us: int, raw: int) -> float | None:
        """Returns the new output, or None when nothing is due."""
        if self.mode == p.PumpMode.MANUAL:
            return None

        if self.next_us == 0:
            self.next_us = now_us + self.cfg.period_us
            self.measurement_raw = self.convert(raw)
            self._filter(self.measurement_raw, self.cfg.period_us / 1e6)
            if self.seed_setpoint:
                self.setpoint_active = self.measurement
                self.last_measurement = self.measurement
                self.seed_setpoint = False
            return None

        if now_us < self.next_us:
            return None

        dt = self.cfg.period_us / 1e6
        self.next_us += self.cfg.period_us
        if now_us >= self.next_us:
            self.next_us = now_us + self.cfg.period_us

        self.measurement_raw = self.convert(raw)
        self._filter(self.measurement_raw, dt)

        if (self.cfg.options & p.CtrlOpt.FAULT_CHECK) and self.measurement < self.cfg.fault_below:
            return self._enter_fault()
        self.flags &= ~p.CtrlFlag.FAULT

        self.updates += 1

        if self.mode == p.PumpMode.AUTOTUNE:
            return self._autotune_tick(now_us)
        return self._run_pid(dt)

    # -- autotune ----------------------------------------------------------

    def start_autotune(self, cmd: p.AutotuneCmd, now_us: int, current_output: float) -> int:
        at = self.at
        if cmd.action == p.AutotuneAction.ABORT:
            if at.state in (p.AutotuneState.SETTLING, p.AutotuneState.RELAY):
                at.state = p.AutotuneState.FAILED
                at.fail_reason = p.AutotuneFail.ABORTED
            self.mode = p.PumpMode.MANUAL
            return 0

        if cmd.action != p.AutotuneAction.START:
            return p.ErrCode.BAD_ARGUMENT
        if not 0.0 < cmd.amplitude <= 50.0:
            return p.ErrCode.BAD_ARGUMENT
        if cmd.hysteresis < 0.0 or math.isnan(cmd.setpoint) or cmd.setpoint < 0.0:
            return p.ErrCode.BAD_ARGUMENT
        if not 2 <= cmd.cycles <= AT_MAX_CYCLES:
            return p.ErrCode.BAD_ARGUMENT
        if cmd.rule > p.TuningRule.PESSEN:
            return p.ErrCode.BAD_ARGUMENT
        if cmd.timeout_ms < 1000:
            return p.ErrCode.BAD_ARGUMENT
        if cmd.settle_ms > cmd.timeout_ms:
            return p.ErrCode.BAD_ARGUMENT

        self.at = _Autotune(
            state=p.AutotuneState.SETTLING,
            rule=cmd.rule,
            want_cycles=cmd.cycles,
            setpoint=cmd.setpoint,
            amplitude=cmd.amplitude,
            hysteresis=cmd.hysteresis,
            started_us=now_us,
            timeout_us=cmd.timeout_ms * 1000,
            settle_us=(cmd.settle_ms or 2000) * 1000,
            settle_min=self.measurement,
            settle_max=self.measurement,
            relay_high=True,
            peak_acc=self.measurement,
            trough_acc=self.measurement,
            bias=_clamp(
                current_output,
                self.cfg.out_min + cmd.amplitude,
                self.cfg.out_max - cmd.amplitude,
            ),
        )
        self.mode = p.PumpMode.AUTOTUNE
        self.next_us = 0
        return 0

    def _autotune_finish(self) -> None:
        at = self.at
        if at.cycles < 2:
            at.state = p.AutotuneState.FAILED
            at.fail_reason = p.AutotuneFail.TIMEOUT
            return

        first = 1 if at.cycles > 2 else 0
        peaks = at.peaks[first:]
        troughs = at.troughs[first:]
        periods = at.periods_us[first:]

        amplitude = sum(pk - tr for pk, tr in zip(peaks, troughs, strict=True)) / len(peaks) * 0.5
        period_us = sum(periods) / len(periods)
        at.spread = max(abs(x - period_us) / period_us for x in periods)

        # See the matching check in ngs_control.c: as the swing approaches the
        # hysteresis band, Ku goes to infinity rather than merely getting
        # uncertain.
        if amplitude <= 1.5 * at.hysteresis:
            at.state = p.AutotuneState.FAILED
            at.fail_reason = p.AutotuneFail.NO_SWING
            return
        if period_us < 10.0 * self.cfg.period_us:
            at.state = p.AutotuneState.FAILED
            at.fail_reason = p.AutotuneFail.INCONSISTENT
            return
        if at.spread > 0.5:
            at.state = p.AutotuneState.FAILED
            at.fail_reason = p.AutotuneFail.INCONSISTENT
            return

        at.measured_amplitude = amplitude * 2.0
        at.tu = period_us / 1e6
        at.ku = (4.0 * at.amplitude) / (
            math.pi * math.sqrt(amplitude * amplitude - at.hysteresis * at.hysteresis)
        )
        at.kp, at.ki, at.kd = p.apply_rule(at.rule, at.ku, at.tu)
        at.state = p.AutotuneState.DONE
        at.fail_reason = p.AutotuneFail.NONE

    def _autotune_settle(self, now_us: int) -> float:
        """Hold the bias, let the process settle, measure the noise.

        The measured wander is what sizes the relay band: guessing it is how
        an autotune ends up switching on noise and confidently reporting the
        period of its own sampling. See ngs_control.c.
        """
        at = self.at
        m = self.measurement
        at.settle_min = min(at.settle_min, m)
        at.settle_max = max(at.settle_max, m)

        self.demand = _clamp(at.bias, self.cfg.out_min, self.cfg.out_max)
        self.output = _apply_deadzone(self.cfg, self.demand)

        if now_us < at.started_us + at.settle_us:
            return self.output

        at.noise = at.settle_max - at.settle_min
        if at.hysteresis <= 0.0:
            at.hysteresis = 1.5 * at.noise or 0.5

        at.relay_high = self.measurement < at.setpoint
        at.peak_acc = m
        at.trough_acc = m
        at.last_cross_us = 0
        at.state = p.AutotuneState.RELAY
        return self.output

    def _autotune_tick(self, now_us: int) -> float:
        at = self.at

        if at.state == p.AutotuneState.SETTLING:
            return self._autotune_settle(now_us)

        if now_us >= at.started_us + at.timeout_us:
            self._autotune_finish()
            if at.state != p.AutotuneState.DONE:
                at.state = p.AutotuneState.FAILED
                at.fail_reason = p.AutotuneFail.TIMEOUT
            self.mode = p.PumpMode.MANUAL
            self.output = at.bias
            return self.output

        m = self.measurement
        # Over the whole cycle, not per relay phase -- see the matching comment
        # in ngs_control.c. The peak arrives after the switch, not before.
        at.peak_acc = max(at.peak_acc, m)
        at.trough_acc = min(at.trough_acc, m)

        switch_now = (at.relay_high and m > at.setpoint + at.hysteresis) or (
            not at.relay_high and m < at.setpoint - at.hysteresis
        )

        if switch_now:
            if not at.relay_high:
                if at.last_cross_us != 0 and at.cycles < AT_MAX_CYCLES:
                    at.periods_us.append(now_us - at.last_cross_us)
                    at.peaks.append(at.peak_acc)
                    at.troughs.append(at.trough_acc)
                at.last_cross_us = now_us
                at.peak_acc = m
                at.trough_acc = m

            at.relay_high = not at.relay_high
            at.state = p.AutotuneState.RELAY

            if at.cycles >= at.want_cycles:
                self._autotune_finish()
                self.mode = p.PumpMode.MANUAL
                self.output = at.bias
                return self.output

        level = at.bias + at.amplitude if at.relay_high else at.bias - at.amplitude
        self.demand = _clamp(level, self.cfg.out_min, self.cfg.out_max)
        self.output = _apply_deadzone(self.cfg, self.demand)
        return self.output

    # -- reporting ---------------------------------------------------------

    def state(self) -> p.ControlState:
        return p.ControlState(
            mode=int(self.mode),
            flags=int(self.flags),
            autotune_state=int(self.at.state),
            stored=0,  # the device layer fills this in

            setpoint=self.setpoint_active,
            setpoint_target=self.cfg.setpoint,
            measurement=self.measurement,
            measurement_raw=self.measurement_raw,
            output=self.output,
            p_term=self.p_term,
            i_term=self.i_term,
            d_term=self.d_term,
            updates=self.updates,
            fault_count=self.fault_count,
        )

    def autotune_state(self) -> p.AutotuneResult:
        at = self.at
        return p.AutotuneResult(
            state=int(at.state),
            fail_reason=int(at.fail_reason),
            cycles_done=at.cycles,
            rule=int(at.rule),
            ku=at.ku,
            tu=at.tu,
            amplitude=at.measured_amplitude,
            kp=at.kp,
            ki=at.ki,
            kd=at.kd,
            spread=at.spread,
            noise=at.noise,
            hysteresis=at.hysteresis,
        )
