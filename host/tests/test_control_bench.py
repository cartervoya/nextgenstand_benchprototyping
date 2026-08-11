"""Closed-loop control through the bench and command layers.

The controller itself is tested on the board (firmware/test/test_ngs) and
pinned by the shared vector. This covers the layer above it: unit conversion,
mode switching, the command syntax, and the safety behaviour that only exists
on the host side.
"""

from __future__ import annotations

import pytest

from ngs_host import protocol as p
from ngs_host.bench import BENCH_CONFIG, Bench
from ngs_host.commands import execute, execute_line, help_text
from ngs_host.device import Device
from ngs_host.fake import FakeBoard, FakeDevice


@pytest.fixture
def board() -> FakeBoard:
    return FakeBoard()


@pytest.fixture
def bench(board: FakeBoard) -> Bench:
    bench = Bench(Device(FakeDevice(board)))
    bench.initialize()
    return bench


def at_flow(bench: Bench, ml_min: float) -> None:
    """Pin the simulated sensor at a flow, in counts the firmware would see."""
    cfg = bench.control_cfg()
    counts = int(ml_min / cfg.cal_scale + cfg.cal_offset)
    bench.device._t.board.adc = lambda channel: counts


# -- calibration -----------------------------------------------------------


def test_the_device_is_given_the_sensor_calibration():
    """The loop works in mL/min, so it needs the two numbers that get it there
    -- while the calibration itself stays in host config."""
    bench = Bench(Device(FakeDevice()))
    cfg = bench.control_cfg()

    assert cfg.channel == 13  # pin 27
    # 744 counts is 0.6 V is 0 mL/min; 3722 counts is 3.0 V is 600.
    assert (744 - cfg.cal_offset) * cfg.cal_scale == pytest.approx(0.0, abs=0.5)
    assert (3722 - cfg.cal_offset) * cfg.cal_scale == pytest.approx(600.0, abs=1.0)


def test_the_fault_threshold_is_below_zero_flow():
    """Under 4 mA reads below zero, which is the whole point -- a threshold of
    0 could never distinguish a dead loop from a stopped pump."""
    bench = Bench(Device(FakeDevice()))
    cfg = bench.control_cfg()
    assert cfg.fault_below < 0.0
    assert cfg.options & p.CtrlOpt.FAULT_CHECK


# -- mode switching --------------------------------------------------------


def test_auto_mode_round_trip(bench):
    bench.set_pump_mode(True, 250.0)
    state = bench.control_state()
    assert state.mode == p.PumpMode.AUTO
    assert state.setpoint_target == pytest.approx(250.0)

    bench.set_pump_mode(False)
    assert bench.control_state().mode == p.PumpMode.MANUAL


def test_a_setpoint_beyond_the_sensor_range_is_refused(bench):
    with pytest.raises(ValueError, match="0-600"):
        bench.set_pump_mode(True, 900.0)


def test_manual_pwm_is_refused_while_the_loop_owns_the_output(bench):
    """Better a clear refusal than a duty that applies and is silently
    overwritten on the next control tick."""
    bench.set_pump_mode(True, 200.0)
    with pytest.raises(p.NgsError) as exc:
        bench.set_pwm("pump", 50.0)
    assert exc.value.code is p.ErrCode.BUSY


def test_stop_works_while_the_loop_is_running(bench, board):
    """The emergency stop has to drop the loop first, or it fails with BUSY at
    exactly the moment it is needed."""
    bench.set_pump_mode(True, 300.0)
    bench.stop()

    assert bench.control_state().mode == p.PumpMode.MANUAL
    assert board.pwm[33][0] == 0


def test_initialize_also_drops_a_running_loop(bench):
    bench.set_pump_mode(True, 300.0)
    bench.initialize()
    assert bench.control_state().mode == p.PumpMode.MANUAL


# -- gains -----------------------------------------------------------------


def test_gains_persist_across_a_mode_change(bench):
    bench.set_gains(kp=0.2, ki=0.05)
    bench.set_pump_mode(True, 100.0)
    bench.set_pump_mode(False)

    cfg = bench.control_cfg()
    assert cfg.kp == pytest.approx(0.2)
    assert cfg.ki == pytest.approx(0.05)


def test_negative_gains_are_refused(bench):
    """A negative gain is positive feedback, not a slow loop."""
    with pytest.raises(ValueError, match="positive feedback"):
        bench.set_gains(kp=-1.0)


# -- command language ------------------------------------------------------


def test_pa_sets_auto_with_a_setpoint(bench):
    (result,) = execute_line(bench, "PA250")
    assert result.ok
    assert bench.control_state().mode == p.PumpMode.AUTO
    assert bench.control_state().setpoint_target == pytest.approx(250.0)


def test_pm_returns_to_manual(bench):
    execute_line(bench, "PA250")
    (result,) = execute_line(bench, "PM")
    assert result.ok
    assert bench.control_state().mode == p.PumpMode.MANUAL


def test_a_bare_duty_is_refused_in_auto_with_a_useful_message(bench):
    execute_line(bench, "PA250")
    (result,) = execute_line(bench, "P50")
    assert not result.ok
    assert "AUTO" in result.text and "PM" in result.text


def test_p_query_reports_the_mode(bench):
    execute_line(bench, "PA250")
    (result,) = execute_line(bench, "P?")
    assert "AUTO" in result.text
    assert "250" in result.text


def test_gain_commands(bench):
    (result,) = execute_line(bench, "KP0.15")
    assert result.ok
    assert bench.control_cfg().kp == pytest.approx(0.15)

    (result,) = execute_line(bench, "KI0.03")
    assert bench.control_cfg().ki == pytest.approx(0.03)

    (result,) = execute_line(bench, "K?")
    assert "kp 0.15" in result.text


def test_filter_and_deadband_commands(bench):
    execute_line(bench, "KF2.5")
    assert bench.control_cfg().filter_tau_s == pytest.approx(2.5)

    execute_line(bench, "KB5")
    assert bench.control_cfg().deadband == pytest.approx(5.0)


def test_a_bad_gain_says_what_was_expected(bench):
    (result,) = execute_line(bench, "KPfast")
    assert not result.ok
    assert "number" in result.text


def test_longest_prefix_wins_so_kp_is_not_read_as_k(bench):
    execute_line(bench, "KP0.42")
    assert bench.control_cfg().kp == pytest.approx(0.42)


def test_help_lists_the_control_commands(bench):
    text = help_text(bench)
    for expected in ("PA<setpoint>", "KP<n>", "T<setpoint>", "TA", "TX"):
        assert expected in text


# -- autotune --------------------------------------------------------------


def test_autotune_starts_and_aborts(bench):
    (result,) = execute_line(bench, "T240")
    assert result.ok
    assert bench.control_state().mode == p.PumpMode.AUTOTUNE

    (result,) = execute_line(bench, "TX")
    assert result.ok
    assert bench.control_state().mode == p.PumpMode.MANUAL
    assert bench.autotune_result().fail_reason == p.AutotuneFail.ABORTED


def test_autotune_status_before_it_has_ever_run(bench):
    (result,) = execute_line(bench, "T?")
    assert "never run" in result.text


def test_adopting_an_unfinished_autotune_is_refused(bench):
    """A timed-out run must not be adopted by accident -- its numbers describe
    noise, and they would go straight into the pump."""
    execute_line(bench, "T240")
    execute_line(bench, "TX")
    with pytest.raises(ValueError, match="did not finish"):
        bench.adopt_autotune()


def test_a_started_autotune_survives_a_status_poll(bench):
    execute_line(bench, "T240")
    bench.poll()
    assert bench.autotune_result().running


def test_changing_gains_mid_autotune_is_refused(bench):
    """Not silently ignored, which is what happens if the change is only
    cached, and not silently destructive either -- pushing it would cancel the
    experiment as a side effect of typing a gain."""
    execute_line(bench, "T240")

    (result,) = execute_line(bench, "KP0.3")
    assert not result.ok
    assert "autotune is running" in result.text
    assert bench.autotune_result().running


def test_the_firmware_cancels_an_autotune_if_a_config_does_arrive(bench):
    """The host refuses first, but the device must not be left running a
    half-finished experiment if one gets through another way."""
    execute_line(bench, "T240")
    bench.set_pump_mode(False)
    assert bench.autotune_result().state == p.AutotuneState.FAILED
    assert bench.autotune_result().fail_reason == p.AutotuneFail.ABORTED


def test_tuning_rules_are_ordered_by_aggressiveness():
    tl = p.apply_rule(p.TuningRule.TYREUS_LUYBEN, 2.0, 4.0)
    zn = p.apply_rule(p.TuningRule.ZIEGLER_NICHOLS, 2.0, 4.0)
    assert tl[0] < zn[0]  # kp
    assert tl[1] < zn[1]  # ki
    assert tl[2] == 0.0  # no derivative on a noisy flow signal


# -- reporting -------------------------------------------------------------


def test_the_snapshot_carries_the_loop_state(bench):
    bench.set_pump_mode(True, 150.0)
    snapshot = bench.poll()
    assert snapshot.control is not None
    assert snapshot.control.mode == p.PumpMode.AUTO


def test_the_displayed_duty_follows_the_loop_not_the_cached_manual_value(bench, board):
    """In auto the device owns the duty; showing the last manual figure would
    be quietly wrong on screen."""
    bench.set_pwm("pump", 10.0)
    at_flow(bench, 0.0)
    bench.set_pump_mode(True, 400.0)

    for _ in range(40):
        bench.poll()

    snapshot = bench.poll()
    assert snapshot.pwms["pump"].percent == pytest.approx(snapshot.control.output, abs=0.01)


def test_a_loopless_bench_still_works(board):
    """Nothing in the command layer may assume a control loop exists."""
    from ngs_host.bench import BenchConfig, ValveSpec

    config = BenchConfig(valves=(ValveSpec("v", "V1", 2, description="V"),))
    plain = Bench(Device(FakeDevice(board)), config)

    assert execute(plain, "V1O").ok
    assert not execute(plain, "K?").ok
    assert not execute(plain, "T200").ok


def test_the_shipped_config_closes_the_pump_around_the_flow_meter():
    (control,) = BENCH_CONFIG.controls
    assert control.output == "pump"
    assert control.input == "flow"
    assert control.kd == 0.0, "derivative on a noisy flow signal amplifies noise"
