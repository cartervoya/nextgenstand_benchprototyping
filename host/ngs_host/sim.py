"""A simulated bench: the fake device, wired to a crude process model.

`fake.py` simulates the *firmware*. This simulates the *plant* -- what the
sensors read given what the outputs are doing. With it, `ngs bench --sim`
brings up the full dashboard with no board attached, which is how the UI and
the command language get exercised when the hardware is on someone else's
desk.

The model is deliberately shallow: a first-order lag from pump duty to flow,
gated on valve 1, plus noise. It is enough to make the display move
believably. It is not a hydraulic model and should not be trusted as one.
"""

from __future__ import annotations

import math
import random
import time
from collections import deque

from .bench import BENCH_CONFIG, BenchConfig
from .fake import FakeBoard, FakeDevice


class FlowModel:
    """Pump duty in, flow out, with a time constant.

    Valve 1 gates the line: closed means the pump dead-heads and the meter
    reads ~0 regardless of duty. Valve 2 is not in the flow path in this
    model -- it is here to be switched, not simulated.
    """

    def __init__(
        self,
        config: BenchConfig = BENCH_CONFIG,
        *,
        tau: float = 0.8,
        noise_ml: float = 1.5,
        delay_s: float = 0.4,
        seed: int = 0,
    ) -> None:
        self.config = config
        self.tau = tau
        self.noise_ml = noise_ml
        # Transport delay: fluid leaving the pump takes time to reach the
        # meter. Not a detail -- a first-order lag with no dead time has no
        # ultimate gain at all (its phase never reaches -180 degrees), so a
        # relay autotune run against a delay-free model measures its own
        # sampling and returns confident nonsense. Any real flow line has it.
        self.delay_s = delay_s
        self.rng = random.Random(seed)
        self.flow_ml_min = 0.0
        self._last = time.monotonic()
        self._sim_last: float | None = None
        self._history: deque[tuple[float, float]] = deque()

    def step(self, board: FakeBoard) -> float:
        pump = self.config.by_name("pump")
        valve1 = self.config.by_name("valve1")

        duty, _freq, bits = board.pwm.get(pump.pin, (0, 0, pump.resolution))
        demand_pct = duty / float((1 << bits) - 1) * 100.0

        gate_open = valve1.is_open(board.pin_values.get(valve1.pin, 0))
        full_scale = self.config.by_name("flow").value_max
        target = (demand_pct / 100.0) * full_scale if gate_open else 0.0

        # Advance on the device's clock when the control loop is driving us,
        # and on wall time otherwise (a bare `read_analog` from a script).
        # See FakeBoard.sim_now_us for why this matters.
        if board.sim_now_us is not None:
            now = board.sim_now_us / 1e6
            if self._sim_last is None:
                self._sim_last = now
            dt = min(max(now - self._sim_last, 0.0), 1.0)
            self._sim_last = now
        else:
            now = time.monotonic()
            dt = min(now - self._last, 1.0)  # a long pause must not jump state
            self._last = now

        alpha = 1.0 - math.exp(-dt / self.tau)
        self.flow_ml_min += (target - self.flow_ml_min) * alpha

        # What the meter sees is what the pump did `delay_s` ago.
        self._history.append((now, self.flow_ml_min))
        while len(self._history) > 1 and self._history[0][0] < now - self.delay_s:
            self._history.popleft()
        measured = self._history[0][1]

        return max(0.0, measured + self.rng.gauss(0.0, self.noise_ml))


def make_sim_device(config: BenchConfig = BENCH_CONFIG, **model_kwargs: float) -> FakeDevice:
    """A FakeDevice whose flow channel responds to the pump and valve 1."""
    board = FakeBoard()
    model = FlowModel(config, **model_kwargs)  # type: ignore[arg-type]
    flow = config.by_name("flow")

    def adc(channel: int) -> int:
        if channel != flow.channel:
            # Unconfigured channels read as a floating input would: near zero
            # with a bit of noise, not a suspiciously clean constant.
            return model.rng.randint(0, 60)

        volts = flow.v_min + (model.step(board) / flow.value_max) * (flow.v_max - flow.v_min)
        counts = round(volts / 3.3 * 4095)
        return max(0, min(4095, counts))

    board.adc = adc
    return FakeDevice(board)
