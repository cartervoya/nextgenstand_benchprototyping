"""The recorded trace behind the live plots.

Three decisions carry the performance here, and they are all about not doing
work rather than doing it quickly:

  Fixed-size ring    Samples go into preallocated lists with a write cursor.
                     Nothing is allocated per sample and nothing is ever
                     shifted, so recording is O(1) forever and memory is
                     decided once, at startup, rather than by how long the
                     bench has been running.

  Incremental reads  Every sample gets a monotonic sequence number, and the
                     client asks for "everything after N". A browser polling
                     at 4 Hz then transfers the four samples that are new
                     instead of the hundred thousand it already has. This is
                     the difference between a plot that keeps up all afternoon
                     and one that gets slower the longer you watch it.

  Min/max decimation A plot is a thousand pixels wide; a buffer holds a
                     hundred thousand points. Sending them all is wasted on
                     both ends. Reducing each pixel column to its min and max
                     keeps every spike visible -- which averaging would erase,
                     and which is the whole reason you are watching a noisy
                     flow signal in the first place.

Traces are declared, not hardcoded: whatever the recorder is handed is what
gets stored and offered, so a new channel in BENCH_CONFIG shows up as a
plottable trace with no changes here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Samples kept per trace. At 4 Hz this is about two hours; at 50 Hz, ten
#: minutes. Sized so the whole buffer stays comfortably in cache-friendly
#: territory and a full decimation pass is a few milliseconds.
DEFAULT_CAPACITY = 30_000


@dataclass(frozen=True, slots=True)
class TraceSpec:
    """A plottable signal. `key` is what the recorder is handed."""

    key: str
    label: str
    unit: str = ""
    #: Traces sharing an axis are drawn against the same scale. Flow in mL/min
    #: and duty in percent on one axis makes both unreadable.
    axis: str = "left"
    color: str = "#61afef"


@dataclass
class History:
    """A ring buffer of samples, one slot per trace."""

    traces: tuple[TraceSpec, ...]
    capacity: int = DEFAULT_CAPACITY

    _time: list[float] = field(default_factory=list, repr=False)
    _values: dict[str, list[float]] = field(default_factory=dict, repr=False)
    _count: int = 0
    _cursor: int = 0

    def __post_init__(self) -> None:
        # Preallocated: appending would make the first pass through the buffer
        # allocate on every sample, and NaN is the natural "nothing recorded
        # here" for a plot -- it leaves a gap rather than drawing a line to
        # zero.
        self._time = [math.nan] * self.capacity
        self._values = {t.key: [math.nan] * self.capacity for t in self.traces}

    # -- recording ---------------------------------------------------------

    def record(self, timestamp: float, values: dict[str, float | None]) -> int:
        """Store one sample. Returns its sequence number.

        A trace missing from `values`, or explicitly None, records as a gap.
        That matters: a dropped poll should leave a hole in the plot, not a
        straight line pretending the signal held steady.
        """
        slot = self._cursor
        self._time[slot] = timestamp
        for key, column in self._values.items():
            value = values.get(key)
            column[slot] = math.nan if value is None else float(value)

        self._cursor = (self._cursor + 1) % self.capacity
        self._count += 1
        return self._count

    @property
    def count(self) -> int:
        """Samples recorded since the start, ever. Also the newest sequence
        number, which is what a client tracks."""
        return self._count

    @property
    def oldest(self) -> int:
        """Sequence number of the oldest sample still held."""
        return max(1, self._count - self.capacity + 1)

    def _slot(self, seq: int) -> int:
        return (self._cursor - (self._count - seq) - 1) % self.capacity

    # -- reading -----------------------------------------------------------

    def since(self, seq: int, keys: list[str] | None = None) -> dict:
        """Everything newer than sequence `seq`.

        The client passes back the `seq` it last saw, so a steady-state poll
        moves only the handful of samples that are actually new.
        """
        keys = keys or [t.key for t in self.traces]
        start = max(seq, self.oldest - 1)
        n = self._count - start
        if n <= 0:
            return {"seq": self._count, "t": [], "series": {k: [] for k in keys}}

        times: list[float] = []
        series: dict[str, list[float | None]] = {k: [] for k in keys}
        for i in range(n):
            slot = self._slot(start + i + 1)
            times.append(self._time[slot])
            for key in keys:
                value = self._values[key][slot]
                series[key].append(None if math.isnan(value) else value)

        return {"seq": self._count, "t": times, "series": series}

    def decimate(self, keys: list[str] | None = None, buckets: int = 1000) -> dict:
        """The whole buffer reduced to `buckets` columns, min and max per
        column.

        Used when a client attaches, or after a gap too big to backfill
        incrementally: it gets the shape of the entire history in a fixed,
        small payload no matter how much is stored.

        Min *and* max, not an average: on a noisy signal the average is a
        smooth line through data that is not smooth, and the excursions it
        erases are the ones worth seeing.
        """
        keys = keys or [t.key for t in self.traces]
        held = min(self._count, self.capacity)
        if held == 0:
            return {"seq": self._count, "t": [], "series": {k: [] for k in keys}}

        buckets = max(1, min(buckets, held))
        per = held / buckets

        times: list[float] = []
        series: dict[str, list[list[float | None]]] = {k: [] for k in keys}

        for b in range(buckets):
            lo = int(b * per)
            hi = max(int((b + 1) * per), lo + 1)
            first_seq = self._count - held + lo + 1

            times.append(self._time[self._slot(first_seq)])
            for key in keys:
                column = self._values[key]
                low = high = math.nan
                for i in range(lo, hi):
                    value = column[self._slot(self._count - held + i + 1)]
                    if math.isnan(value):
                        continue
                    low = value if math.isnan(low) or value < low else low
                    high = value if math.isnan(high) or value > high else high
                series[key].append(
                    [None, None] if math.isnan(low) else [low, high]
                )

        return {"seq": self._count, "t": times, "series": series, "decimated": True}

    def span(self) -> tuple[float, float]:
        """Timestamps of the oldest and newest samples held."""
        if self._count == 0:
            return 0.0, 0.0
        return self._time[self._slot(self.oldest)], self._time[self._slot(self._count)]

    def clear(self) -> None:
        for column in self._values.values():
            for i in range(self.capacity):
                column[i] = math.nan
        for i in range(self.capacity):
            self._time[i] = math.nan
        self._count = 0
        self._cursor = 0


def traces_for(config) -> tuple[TraceSpec, ...]:
    """The plottable signals implied by a bench config.

    Derived rather than listed, so a channel added to BENCH_CONFIG becomes a
    trace without touching this file -- the same rule the command language and
    the dashboards already follow.
    """
    specs: list[TraceSpec] = []

    for analog in config.analogs:
        specs.append(
            TraceSpec(analog.name, analog.description or analog.name, analog.unit, "left",
                      "#61afef")
        )

    for pwm in config.pwms:
        specs.append(TraceSpec(f"{pwm.name}_output", f"{pwm.description} output", "%", "right",
                               "#e5c07b"))

    for control in config.controls:
        analog = next((a for a in config.analogs if a.name == control.input), None)
        unit = analog.unit if analog else ""
        specs.append(TraceSpec(f"{control.output}_setpoint", "Setpoint", unit, "left", "#a3d977"))
        specs.append(TraceSpec(f"{control.output}_error", "Error", unit, "left", "#e06c75"))
        specs.append(TraceSpec(f"{control.output}_p", "P term", "%", "right", "#c678dd"))
        specs.append(TraceSpec(f"{control.output}_i", "I term", "%", "right", "#56b6c2"))

    for valve in config.valves:
        specs.append(TraceSpec(valve.name, valve.description or valve.name, "open", "right",
                               "#98c379"))

    return tuple(specs)


def sample_from(snapshot, config) -> dict[str, float | None]:
    """Flatten a bench Snapshot into the values `record` expects."""
    values: dict[str, float | None] = {}

    for name, reading in snapshot.analogs.items():
        values[name] = None if reading.faulted else reading.value
    for name, reading in snapshot.pwms.items():
        values[f"{name}_output"] = reading.percent
    for name, reading in snapshot.valves.items():
        values[name] = 1.0 if reading.is_open else 0.0

    control = snapshot.control
    for spec in config.controls:
        if control is None:
            continue
        values[f"{spec.output}_setpoint"] = control.setpoint
        values[f"{spec.output}_error"] = control.error
        values[f"{spec.output}_p"] = control.p_term
        values[f"{spec.output}_i"] = control.i_term

    return values
