"""The shared control vector: the same numbers through the C and the Python.

host/ngs_host/control.py mirrors firmware/lib/ngs/ngs_control.c so the
simulated device behaves like the real one. Duplicated logic drifts, so this
pins both to one scenario:

  - the scenario and the expected outputs below
  - test_control_vector() in firmware/test/test_ngs/test_control.h, running the
    identical numbers on the board

Change the controller's behaviour and one of them fails. If they disagree, the
C is right and control.py is wrong -- the C is what actually drives the pump.

The scenario is chosen to exercise the parts that are easy to get subtly
wrong: bumpless entry seeding the integrator, the median prefilter eating a
full-scale spike, and the sign of the error either side of the setpoint.
"""

from __future__ import annotations

import pytest

from ngs_host import protocol as p
from ngs_host.control import FakeController

#: 744 counts is 0 mL/min and 2233 is ~300, the setpoint. The 4095 is a
#: full-scale spike the median prefilter has to blunt.
RAW_SEQUENCE = [744, 1000, 1500, 2000, 2233, 2233, 2500, 3000, 2233, 4095, 2233, 2233]

#: Outputs after each tick, first (priming) tick excluded. Generated from this
#: implementation and verified against the C on hardware.
EXPECTED_OUTPUT = [
    32.518876,
    32.618232,
    27.637268,
    27.696305,
    22.675021,
    20.326308,
    20.326235,
    20.326162,
    17.613198,
    17.591594,
    20.282881,
]

#: Deliberately awkward values, so a config that silently fails to apply shows
#: up as a mismatch rather than as a coincidentally identical result.
def scenario_config() -> p.ControlCfg:
    return p.ControlCfg(
        mode=p.PumpMode.AUTO,
        channel=13,
        options=0,
        setpoint=300.0,
        kp=0.05,
        ki=0.02,
        kd=0.0,
        out_min=0.0,
        out_max=100.0,
        filter_tau_s=0.0,  # median only, so the vector stays exactly reproducible
        deadband=0.0,
        setpoint_slew=0.0,
        output_slew=0.0,
        cal_scale=0.2016,
        cal_offset=744.0,
        fault_below=0.0,
        period_us=20_000,
    )


def run_scenario() -> list[float]:
    controller = FakeController()
    assert controller.configure(scenario_config(), 20.0) == 0

    now = 1_000_000
    outputs = []
    for raw in RAW_SEQUENCE:
        now += 20_000
        out = controller.tick(now, raw)
        if out is not None:
            outputs.append(out)
    return outputs


def test_the_mirror_reproduces_the_shared_vector():
    outputs = run_scenario()
    assert len(outputs) == len(EXPECTED_OUTPUT)
    for i, (got, want) in enumerate(zip(outputs, EXPECTED_OUTPUT, strict=True)):
        assert got == pytest.approx(want, abs=1e-4), f"tick {i}: {got} != {want}"


def test_the_first_tick_only_primes():
    """It reads and filters but must not drive: acting on a one-sample filter
    is how a loop starts with a kick."""
    controller = FakeController()
    controller.configure(scenario_config(), 20.0)
    assert controller.tick(1_020_000, 744) is None


def test_entry_is_seeded_from_the_output_already_applied():
    """Bumpless transfer: the integrator starts at the manual duty, so the
    first real tick continues from it instead of jumping to P alone."""
    controller = FakeController()
    controller.configure(scenario_config(), 20.0)
    assert controller.integral == pytest.approx(20.0)


def test_the_median_filter_attenuates_the_spike():
    """Tick 10 feeds 4095 counts -- about 675 mL/min, wildly over range.

    Measured against the same run with that one sample replaced by a normal
    reading, so this isolates the spike's effect rather than the ordinary
    movement around it. Note "attenuates", not "rejects": a median of five
    only removes an outlier outright when the other four are clustered, and
    here they are not. The comparison is against what the spike would have
    done unfiltered.
    """
    controller = FakeController()
    controller.configure(scenario_config(), 20.0)
    clean = list(RAW_SEQUENCE)
    clean[RAW_SEQUENCE.index(4095)] = 2233

    now = 1_000_000
    without_spike = []
    for raw in clean:
        now += 20_000
        out = controller.tick(now, raw)
        if out is not None:
            without_spike.append(out)

    spike_index = RAW_SEQUENCE.index(4095) - 1  # the priming tick has no output
    shifted = abs(run_scenario()[spike_index] - without_spike[spike_index])

    cfg = scenario_config()
    unfiltered = cfg.kp * abs((4095 - 2233) * cfg.cal_scale)
    assert shifted < unfiltered / 4, (
        f"spike moved the output {shifted:.2f} %, barely better than the "
        f"{unfiltered:.2f} % it would move unfiltered"
    )
