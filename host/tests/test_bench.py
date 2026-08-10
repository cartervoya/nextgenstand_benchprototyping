"""Bench-layer tests: the wiring, the scaling, and the safe states.

These are the numbers that turn into real current through a real solenoid, so
they get checked against the specification rather than against themselves.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from ngs_host.bench import (
    BENCH_CONFIG,
    TEENSY41_ANALOG_PINS,
    Bench,
    BenchConfig,
    ValveSpec,
)
from ngs_host.device import Device
from ngs_host.fake import FakeBoard, FakeDevice
from ngs_host.sim import make_sim_device


@pytest.fixture
def board() -> FakeBoard:
    return FakeBoard()


@pytest.fixture
def bench(board: FakeBoard) -> Bench:
    return Bench(Device(FakeDevice(board)))


# -- config ----------------------------------------------------------------


def test_the_shipped_config_is_valid_for_this_board():
    BENCH_CONFIG.validate()


def test_config_matches_the_documented_wiring():
    """The bench as specified: valve 1 on 32, valve 2 on 31, flow on 27,
    pump on 33. If someone rewires, this test is the place it gets recorded."""
    valve1, valve2 = BENCH_CONFIG.valves
    assert (valve1.name, valve1.pin) == ("valve1", 32)
    assert (valve2.name, valve2.pin) == ("valve2", 31)

    (flow,) = BENCH_CONFIG.analogs
    assert flow.pin == 27
    assert flow.channel == 13, "pin 27 is A13 on a Teensy 4.1"
    assert (flow.v_min, flow.v_max) == (0.6, 3.0)
    assert (flow.value_min, flow.value_max) == (0.0, 600.0)

    (pump,) = BENCH_CONFIG.pwms
    assert pump.pin == 33
    assert pump.freq_hz == 50_000
    assert pump.resolution == 12


def test_pwm_frequency_clears_the_rc_filter():
    """The filter corner is 1-2 kHz; the carrier has to be far above it or the
    pump sees ripple instead of DC."""
    (pump,) = BENCH_CONFIG.pwms
    assert pump.freq_hz >= 10 * 2_000


def test_pwm_frequency_is_achievable_at_this_resolution():
    (pump,) = BENCH_CONFIG.pwms
    assert pump.freq_hz <= 600_000_000 / (1 << pump.resolution)


def test_duplicate_command_codes_are_rejected():
    config = BenchConfig(
        valves=(ValveSpec("a", "V1", 2), ValveSpec("b", "V1", 3)),
    )
    with pytest.raises(ValueError, match="duplicate"):
        config.validate()


def test_a_non_analog_pin_is_rejected():
    assert 32 not in TEENSY41_ANALOG_PINS
    (flow,) = BENCH_CONFIG.analogs
    with pytest.raises(ValueError, match="not an analog input"):
        BenchConfig(analogs=(replace(flow, pin=32),)).validate()


# -- flow scaling ----------------------------------------------------------


def counts_for(volts: float, bits: int = 12) -> int:
    return round(volts / 3.3 * ((1 << bits) - 1))


@pytest.mark.parametrize(
    ("volts", "expected_ml_min"),
    [
        (0.6, 0.0),  # 4 mA
        (1.8, 300.0),  # 12 mA, mid-scale
        (3.0, 600.0),  # 20 mA
    ],
)
def test_flow_scaling_endpoints(volts, expected_ml_min):
    (flow,) = BENCH_CONFIG.analogs
    assert flow.to_value(volts) == pytest.approx(expected_ml_min)


def test_flow_reading_from_raw_counts(bench, board):
    (flow,) = BENCH_CONFIG.analogs
    board.adc = lambda channel: counts_for(1.8)

    reading = bench.read_analog("flow")
    assert reading.volts == pytest.approx(1.8, abs=1e-3)
    assert reading.value == pytest.approx(300.0, abs=0.5)
    assert not reading.faulted


def test_a_dead_current_loop_reads_as_a_fault_not_as_zero_flow(bench, board):
    """Under 4 mA means the loop is broken or the transmitter is unpowered.
    Reporting that as 0 mL/min would be indistinguishable from a stopped pump."""
    board.adc = lambda channel: counts_for(0.2)

    reading = bench.read_analog("flow")
    assert reading.faulted
    assert "FAULT" in reading.text


def test_a_slightly_low_reading_is_not_a_fault(bench, board):
    board.adc = lambda channel: counts_for(0.58)
    assert not bench.read_analog("flow").faulted


def test_over_range_is_reported_rather_than_clamped(bench, board):
    board.adc = lambda channel: counts_for(3.2)
    assert bench.read_analog("flow").value > 600.0


# -- pump ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("percent", "counts"), [(0.0, 0), (50.0, 2048), (100.0, 4095), (37.5, 1536)]
)
def test_pump_percent_to_counts(percent, counts):
    (pump,) = BENCH_CONFIG.pwms
    assert pump.to_counts(percent) == counts


def test_full_scale_is_expressible(bench, board):
    """4096 would be rejected by the firmware as out of range for 12 bits."""
    bench.set_pwm("pump", 100.0)
    duty, _freq, bits = board.pwm[33]
    assert duty == (1 << bits) - 1


def test_pump_write_carries_frequency_and_resolution_every_time(bench, board):
    bench.set_pwm("pump", 25.0)
    assert board.pwm[33] == (1024, 50_000, 12)

    # 75 % of 4095 is 3071.25 -- full scale is 4095, not 4096.
    bench.set_pwm("pump", 75.0)
    assert board.pwm[33] == (3071, 50_000, 12)


def test_pump_setpoint_outside_0_100_is_refused(bench):
    with pytest.raises(ValueError, match="0-100"):
        bench.set_pwm("pump", 101.0)
    with pytest.raises(ValueError, match="0-100"):
        bench.set_pwm("pump", -1.0)


# -- valves ----------------------------------------------------------------


def test_valve_drives_the_right_pin(bench, board):
    bench.set_valve("valve1", True)
    assert board.pin_values[32] == 1
    assert board.pin_values.get(31, 0) == 0

    bench.set_valve("valve2", True)
    assert board.pin_values[31] == 1


def test_valve_state_is_read_back_from_the_board(bench):
    bench.set_valve("valve1", True)
    assert bench.read_valve("valve1").is_open
    bench.set_valve("valve1", False)
    assert not bench.read_valve("valve1").is_open


def test_toggle(bench):
    assert bench.toggle_valve("valve2") is True
    assert bench.toggle_valve("valve2") is False


def test_a_valve_that_did_not_follow_the_command_is_flagged(bench, board):
    """The board rebooting mid-run -- a brownout when a solenoid kicks in is
    the classic cause -- leaves the pin at its power-on default while this host
    still believes the valve is open."""
    bench.set_valve("valve1", True)
    assert not bench.read_valve("valve1").mismatch

    board.pin_values.clear()  # what a reset looks like from here

    reading = bench.read_valve("valve1")
    assert reading.mismatch
    assert not reading.is_open
    assert "commanded OPEN" in reading.text


def test_a_valve_we_have_not_driven_is_not_a_mismatch(bench):
    """Nothing has been commanded yet, so the pin cannot disagree with us."""
    reading = bench.read_valve("valve1")
    assert reading.commanded is None
    assert not reading.mismatch


def test_the_snapshot_surfaces_mismatched_valves(bench, board):
    bench.set_valve("valve1", True)
    bench.set_valve("valve2", True)
    board.pin_values.clear()

    snapshot = bench.poll()
    assert {r.spec.name for r in snapshot.mismatched_valves} == {"valve1", "valve2"}


def test_re_initialising_clears_a_mismatch(bench, board):
    bench.set_valve("valve1", True)
    board.pin_values.clear()
    assert bench.poll().mismatched_valves

    bench.initialize()
    assert not bench.poll().mismatched_valves


def test_an_inverted_valve_needs_no_special_cases_elsewhere(board):
    """A normally-open valve or an inverting driver is one field, not a pile of
    negations at the call sites."""
    config = BenchConfig(valves=(ValveSpec("nc", "V9", 20, open_level=0),))
    bench = Bench(Device(FakeDevice(board)), config)

    bench.set_valve("nc", True)
    assert board.pin_values[20] == 0
    assert bench.read_valve("nc").is_open


def test_unknown_channel_names_say_what_is_available(bench):
    with pytest.raises(KeyError, match="valve1, valve2"):
        bench.set_valve("valve3", True)


# -- lifecycle -------------------------------------------------------------


def test_initialize_puts_the_bench_in_a_safe_state(bench, board):
    bench.set_valve("valve1", True)
    bench.set_pwm("pump", 80.0)

    bench.initialize()

    assert board.pin_values[32] == 0
    assert board.pin_values[31] == 0
    assert board.pwm[33][0] == 0
    assert bench.pwm_percent("pump") == 0.0


def test_stop_kills_the_pump_before_closing_the_valves(bench, board):
    """Order matters: closing a valve while the pump is at full tilt
    dead-heads it."""
    calls: list[str] = []
    bench.set_pwm("pump", 100.0)
    bench.set_valve("valve1", True)

    original_pwm, original_valve = bench.set_pwm, bench.set_valve
    bench.set_pwm = lambda *a, **k: (calls.append("pump"), original_pwm(*a, **k))[1]
    bench.set_valve = lambda *a, **k: (calls.append("valve"), original_valve(*a, **k))[1]

    bench.stop()
    assert calls[0] == "pump"
    assert board.pwm[33][0] == 0


# -- polling ---------------------------------------------------------------


def test_poll_returns_every_channel(bench):
    snapshot = bench.poll()
    assert snapshot.error is None
    assert set(snapshot.valves) == {"valve1", "valve2"}
    assert set(snapshot.analogs) == {"flow"}
    assert set(snapshot.pwms) == {"pump"}
    assert snapshot.status is not None


def test_poll_reports_a_broken_link_instead_of_raising(bench):
    class Dead:
        def read(self, size=1):
            raise OSError("device disconnected")

        def write(self, data):
            raise OSError("device disconnected")

        def close(self):
            pass

    bench.device._t = Dead()
    snapshot = bench.poll()
    assert snapshot.error is not None
    assert "disconnected" in snapshot.error


# -- simulator -------------------------------------------------------------


def test_the_simulator_responds_to_the_pump_and_valve():
    """The sim exists so the UI can be driven with no board attached; if the
    flow stopped tracking the pump it would stop being useful for that.

    The time constant is compressed to microseconds so the test settles within
    a few hundred reads instead of the real ~second.
    """
    bench = Bench(Device(make_sim_device(tau=0.0005, noise_ml=0.0)))
    bench.initialize()

    bench.set_valve("valve1", True)
    bench.set_pwm("pump", 100.0)
    for _ in range(200):
        reading = bench.read_analog("flow")
    assert reading.value > 300, "flow should follow the pump when valve 1 is open"

    bench.set_valve("valve1", False)
    for _ in range(200):
        reading = bench.read_analog("flow")
    assert reading.value < 100, "a closed valve 1 should stop the flow"
