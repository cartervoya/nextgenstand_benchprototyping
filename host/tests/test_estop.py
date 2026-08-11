"""Emergency stop.

The point of these is the difference between a stop and a *convenience*: it
has to be atomic on the device, it has to latch, and it has to work when the
host is not there. The device-side behaviour is also covered on the board in
firmware/test/test_ngs/test_estop.h.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from ngs_host import protocol as p
from ngs_host.bench import BENCH_CONFIG, Bench
from ngs_host.commands import execute, execute_line, help_text
from ngs_host.device import Device
from ngs_host.fake import FakeBoard, FakeDevice
from ngs_host.keyboard import CTRL_E, EmergencyStop, LineEditor
from ngs_host.ui import Dashboard, render


@pytest.fixture
def board() -> FakeBoard:
    return FakeBoard()


@pytest.fixture
def fake(board: FakeBoard) -> FakeDevice:
    return FakeDevice(board)


@pytest.fixture
def bench(fake: FakeDevice) -> Bench:
    bench = Bench(Device(fake))
    bench.initialize()
    return bench


# -- the safe-state table --------------------------------------------------


def test_initialize_registers_every_output(bench, fake):
    """The device is told where everything belongs *before* anything is
    driven, so from that moment it can get there without us."""
    entries = fake.safe_entries
    assert len(entries) == len(BENCH_CONFIG.valves) + len(BENCH_CONFIG.pwms)

    by_pin = {e.pin: e for e in entries.values()}
    assert by_pin[32].kind == p.SafeKind.GPIO and by_pin[32].value == 0  # valve 1 closed
    assert by_pin[31].kind == p.SafeKind.GPIO and by_pin[31].value == 0  # valve 2 closed
    assert by_pin[33].kind == p.SafeKind.PWM and by_pin[33].value == 0  # pump off


def test_re_registering_does_not_leave_stale_entries(bench, fake):
    """A reconnecting host must not leave a safe-state entry for an output
    that is no longer wired."""
    before = len(fake.safe_entries)
    bench.register_safe_state()
    assert len(fake.safe_entries) == before


def test_the_status_reports_how_many_are_registered(bench):
    assert bench.device.status().safe_entries == 3


# -- engaging --------------------------------------------------------------


def test_estop_drives_everything_safe(bench, board):
    bench.set_valve("valve1", True)
    bench.set_valve("valve2", True)
    bench.set_pwm("pump", 80.0)

    bench.estop()

    assert board.pin_values[32] == 0
    assert board.pin_values[31] == 0
    assert board.pwm[33][0] == 0


def test_estop_takes_the_pump_back_from_the_loop(bench, board):
    """Leaving the controller in AUTO would let it drive the pump straight
    back up on its next tick, milliseconds later."""
    bench.set_pump_mode(True, 300.0)
    bench.estop()

    assert bench.control_state().mode == p.PumpMode.MANUAL
    assert board.pwm[33][0] == 0


def test_it_latches(bench):
    bench.estop()
    status = bench.device.status()
    assert status.estopped
    assert status.estop_source == p.EstopSource.COMMAND


def test_outputs_are_refused_while_latched(bench):
    bench.estop()

    for call in (
        lambda: bench.set_valve("valve1", True),
        lambda: bench.set_pwm("pump", 50.0),
        lambda: bench.set_pump_mode(True, 200.0),
        lambda: bench.start_autotune(200.0),
    ):
        with pytest.raises(p.NgsError) as exc:
            call()
        assert exc.value.code is p.ErrCode.ESTOP


def test_reads_still_work_while_latched(bench):
    """A latched bench is exactly when you most want to see what it is
    doing."""
    bench.estop()
    assert bench.read_analog("flow") is not None
    assert bench.read_valve("valve1") is not None
    assert bench.poll().error is None


def test_returning_the_loop_to_manual_is_allowed_while_latched(bench):
    """Manual is a way *out* of driving something, so it is never refused."""
    bench.estop()
    bench.set_pump_mode(False)
    assert bench.control_state().mode == p.PumpMode.MANUAL


# -- clearing --------------------------------------------------------------


def test_clearing_moves_nothing(bench, board):
    """Releasing the latch must not restore what was running -- outputs stay
    where they are until something is explicitly commanded."""
    bench.set_valve("valve1", True)
    bench.estop()
    bench.clear_estop()

    assert not bench.device.status().estopped
    assert board.pin_values[32] == 0


def test_outputs_work_again_after_clearing(bench, board):
    bench.estop()
    bench.clear_estop()
    bench.set_valve("valve1", True)
    assert board.pin_values[32] == 1


def test_it_does_not_clear_itself(bench):
    """A stop that lapses on its own is not a stop. Poll a while and check."""
    bench.estop()
    for _ in range(20):
        bench.poll()
    assert bench.device.status().estopped


# -- the watchdog ----------------------------------------------------------


def test_the_watchdog_latches_when_the_host_goes_quiet(bench, fake, board):
    """The case a host-side stop cannot cover: the process dies, or the cable
    comes out, while the pump is running."""
    bench.initialize(watchdog_ms=50)
    bench.set_pwm("pump", 70.0)
    assert board.pwm[33][0] > 0

    # Nothing sent for longer than the timeout -- the device is on its own.
    fake.last_rx_us -= 200_000
    fake._poll_watchdog()

    assert fake.estop
    assert fake.estop_source == p.EstopSource.WATCHDOG
    assert board.pwm[33][0] == 0


def test_the_watchdog_is_off_unless_asked_for(bench, fake):
    """Otherwise it would undo a valve deliberately set from a one-shot
    command -- ordinary bench use, not an emergency."""
    assert BENCH_CONFIG.watchdog_ms == 0
    assert fake.watchdog_ms == 0


def test_a_talking_host_keeps_the_watchdog_happy(bench, fake, board):
    bench.initialize(watchdog_ms=1000)
    bench.set_pwm("pump", 70.0)
    for _ in range(10):
        bench.poll()
    assert not fake.estop
    assert board.pwm[33][0] > 0


# -- command language ------------------------------------------------------


@pytest.mark.parametrize("word", ["!", "E", "estop", "ESTOP"])
def test_the_estop_commands(bench, word, board):
    bench.set_valve("valve1", True)
    (result,) = execute_line(bench, word)
    assert result.ok
    assert "EMERGENCY STOP" in result.text
    assert board.pin_values[32] == 0


def test_the_clear_command(bench):
    execute_line(bench, "!")
    (result,) = execute_line(bench, "EC")
    assert result.ok
    assert not bench.device.status().estopped


def test_a_refused_command_says_the_estop_is_why(bench):
    execute_line(bench, "!")
    (result,) = execute_line(bench, "V1O")
    assert not result.ok
    assert "ESTOP" in result.text


def test_help_leads_with_the_estop(bench):
    text = help_text(bench)
    assert "EMERGENCY STOP" in text
    assert "Ctrl-E" in text


def test_estop_does_not_need_a_control_loop(board):
    from ngs_host.bench import BenchConfig, ValveSpec

    config = BenchConfig(valves=(ValveSpec("v", "V1", 2, description="V"),))
    plain = Bench(Device(FakeDevice(board)), config)
    plain.initialize()

    assert execute(plain, "!").ok
    assert board.pin_values[2] == 0


# -- the dashboards --------------------------------------------------------


def test_ctrl_e_is_raised_immediately_not_at_end_of_line():
    """An E-stop you have to finish typing is not an E-stop."""
    editor = LineEditor()
    editor.feed("V1O")  # mid-command
    with pytest.raises(EmergencyStop):
        editor.feed(CTRL_E)


def test_the_dashboard_engages_on_ctrl_e(fake, board):
    bench = Bench(Device(fake))
    bench.initialize()
    bench.set_valve("valve1", True)

    dash = Dashboard(bench, port="sim", console=Console(file=io.StringIO(), width=100))
    dash.emergency_stop()

    assert board.pin_values[32] == 0
    assert fake.estop


def test_a_latched_estop_takes_over_the_header(fake):
    bench = Bench(Device(fake))
    bench.initialize()
    dash = Dashboard(bench, port="sim", console=Console(file=io.StringIO(), width=100))
    dash.bench.estop()
    dash.poll()

    console = Console(file=io.StringIO(), width=100)
    console.print(
        render(dash.snapshot, list(dash.log), "", port="sim", fw="0.1.0", poll_hz=2.0)
    )
    text = console.file.getvalue()
    assert "EMERGENCY STOP LATCHED" in text
    assert "command" in text  # the source


def test_the_web_page_reports_the_latch(fake):
    from ngs_host.web import WebBench

    bench = Bench(Device(fake))
    bench.initialize()
    web = WebBench(bench, port="sim", fw="0.1.0")

    assert web.state()["estop"]["latched"] is False
    bench.estop()
    state = web.state()
    assert state["estop"]["latched"] is True
    assert state["estop"]["source"] == "command"


def test_estop_registers_the_table_if_the_device_has_none(fake, board):
    """`ngs estop` against a freshly booted board would otherwise latch
    correctly and drive nothing -- the worst possible way to fail."""
    bench = Bench(Device(fake))  # note: no initialize()
    assert fake.safe_entries == {}

    board.pin_values[32] = 1  # something is live
    bench.estop()

    assert fake.safe_entries, "no safe state registered before latching"
    assert board.pin_values[32] == 0
