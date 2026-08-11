"""Measuring what the pump actually does, so the loop can be told.

Two things about a real pump break a controller tuned as if it were ideal:

  Deadzone      Nothing happens at all until the drive clears a threshold. To
                the loop that is a region of zero gain, and the only way out of
                it is to wind the integral up until something moves -- which
                then overshoots, because by the time flow appears the
                integrator is carrying far more than the operating point needs.

  Non-linearity Gain varies with operating point, so gains that are right at
                300 mL/min are wrong at 50. Nothing here fixes that; it
                measures it, so you know how far a tuning travels and can tune
                where you intend to run.

Both are properties of the hardware, not of the software, so both have to be
measured rather than assumed. This steps the pump across its range, waits at
each step, and reports what came back.

It moves real hardware: the pump runs, and the flow path must be open.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .bench import Bench


@dataclass(frozen=True, slots=True)
class CurvePoint:
    percent: float
    flow: float
    #: Spread of the samples taken at this step -- the noise floor at this
    #: operating point, which is not always the same at every flow.
    spread: float
    samples: int


@dataclass
class PumpCurve:
    points: list[CurvePoint] = field(default_factory=list)
    unit: str = "mL/min"
    #: Sample spread at zero drive: the sensor's own noise, with the pump off.
    noise: float = 0.0
    aborted: str = ""

    # -- what the curve tells you ------------------------------------------

    @property
    def deadzone(self) -> float:
        """Highest drive that still produced nothing.

        "Nothing" means within the measured noise floor rather than exactly
        zero -- on a noisy 4-20 mA signal the reading is never exactly zero,
        and a threshold defined as "> 0" would come out as the first step
        every time.
        """
        threshold = max(self.noise, 1.0)
        last_dead = 0.0
        for point in self.points:
            if point.flow <= threshold:
                last_dead = point.percent
            else:
                break
        return last_dead

    @property
    def max_flow(self) -> float:
        return max((point.flow for point in self.points), default=0.0)

    def active(self) -> list[CurvePoint]:
        """The points above the deadzone -- the part worth fitting."""
        return [pt for pt in self.points if pt.percent > self.deadzone]

    def fit(self) -> tuple[float, float]:
        """Least-squares slope and intercept over the active region."""
        pts = self.active()
        if len(pts) < 2:
            return 0.0, 0.0

        n = len(pts)
        mean_x = sum(pt.percent for pt in pts) / n
        mean_y = sum(pt.flow for pt in pts) / n
        sxx = sum((pt.percent - mean_x) ** 2 for pt in pts)
        sxy = sum((pt.percent - mean_x) * (pt.flow - mean_y) for pt in pts)
        slope = sxy / sxx if sxx else 0.0
        return slope, mean_y - slope * mean_x

    @property
    def linearity(self) -> float:
        """Worst deviation from the straight-line fit, as a fraction of full
        scale. 0 is a perfect line.

        Reported rather than corrected: a number you can look at tells you how
        far a tuning done at one flow should be trusted at another, which is a
        judgement call, not something to paper over silently.
        """
        pts = self.active()
        if len(pts) < 3 or self.max_flow <= 0:
            return 0.0
        slope, intercept = self.fit()
        worst = max(abs(pt.flow - (slope * pt.percent + intercept)) for pt in pts)
        return worst / self.max_flow

    @property
    def gain_low(self) -> float:
        """Local slope over the bottom third of the active range, units per %."""
        return self._local_gain(0.0, 1.0 / 3.0)

    @property
    def gain_high(self) -> float:
        """Local slope over the top third."""
        return self._local_gain(2.0 / 3.0, 1.0)

    def _local_gain(self, lo: float, hi: float) -> float:
        pts = self.active()
        if len(pts) < 4:
            return 0.0
        start, end = int(len(pts) * lo), max(int(len(pts) * hi), 2)
        window = pts[start:end]
        if len(window) < 2:
            return 0.0
        span = window[-1].percent - window[0].percent
        return (window[-1].flow - window[0].flow) / span if span else 0.0

    def describe(self) -> list[str]:
        lines = [
            f"deadzone      {self.deadzone:.0f} % "
            f"(nothing moves below this)",
            f"full scale    {self.max_flow:.0f} {self.unit} at 100 %",
            f"sensor noise  {self.noise:.1f} {self.unit} peak-to-peak, pump off",
        ]
        slope, _ = self.fit()
        if slope:
            lines.append(f"average gain  {slope:.1f} {self.unit} per %")
        if self.linearity:
            lines.append(
                f"linearity     {self.linearity * 100:.0f} % worst deviation from a straight line"
            )
        low, high = self.gain_low, self.gain_high
        if low and high:
            ratio = high / low if low else 0.0
            lines.append(
                f"gain spread   {low:.1f} at the bottom vs {high:.1f} at the top "
                f"({ratio:.1f}x) -- tune where you intend to run"
            )
        return lines


def measure(
    bench: Bench,
    *,
    output: str = "pump",
    analog: str = "flow",
    steps: int = 10,
    dwell_s: float = 4.0,
    samples: int = 8,
    settle_s: float = 2.0,
    on_point: object = None,
) -> PumpCurve:
    """Step the pump across its range and record the flow at each step.

    Deliberately stepping up only. A pump with any check valve or compliance in
    the line reads differently on the way down, and averaging the two would
    hide exactly the hysteresis worth knowing about.

    Returns whatever was collected even if it stops early, so a run aborted by
    a sensor fault still tells you where the fault appeared.
    """
    sensor = bench.config.by_name(analog)
    curve = PumpCurve(unit=sensor.unit)

    # The loop cannot own the output while we are stepping it by hand.
    has_loop = any(c.output == output for c in bench.config.controls)
    if has_loop and bench.control_state().mode != 0:
        bench.set_pump_mode(False, output=output)

    def sample() -> tuple[float, float]:
        readings = []
        for _ in range(samples):
            readings.append(bench.read_analog(analog).value)
            time.sleep(0.05)
        return sum(readings) / len(readings), max(readings) - min(readings)

    try:
        # Zero first: the sensor's own noise floor, with nothing running.
        bench.set_pwm(output, 0.0)
        time.sleep(settle_s)
        flow, spread = sample()
        curve.noise = spread
        curve.points.append(CurvePoint(0.0, flow, spread, samples))
        if on_point is not None:
            on_point(curve.points[-1])

        for i in range(1, steps + 1):
            percent = 100.0 * i / steps
            bench.set_pwm(output, percent)
            time.sleep(dwell_s)

            reading = bench.read_analog(analog)
            if reading.faulted:
                curve.aborted = f"sensor fault at {percent:.0f} %"
                break

            flow, spread = sample()
            curve.points.append(CurvePoint(percent, flow, spread, samples))
            if on_point is not None:
                on_point(curve.points[-1])
    finally:
        # However this ends, the pump comes back down.
        bench.set_pwm(output, 0.0)

    return curve
