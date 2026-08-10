"""Command-language tests, including the examples from the original spec."""

from __future__ import annotations

import pytest

from ngs_host.bench import Bench, BenchConfig, ValveSpec
from ngs_host.commands import execute, execute_line, help_text, split
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


def run(bench: Bench, line: str):
    results = execute_line(bench, line)
    assert results, f"{line!r} produced no result"
    return results


# -- the syntax from the spec ----------------------------------------------


def test_pump_setpoint(bench, board):
    """`P100.0;` -- pump to 100 %."""
    (result,) = run(bench, "P100.0;")
    assert result.ok
    assert board.pwm[33][0] == 4095
    assert bench.pwm_percent("pump") == 100.0


def test_close_valve_1(bench, board):
    """`V1C;` -- close valve 1."""
    bench.set_valve("valve1", True)
    (result,) = run(bench, "V1C;")
    assert result.ok
    assert board.pin_values[32] == 0


def test_open_valve_2(bench, board):
    (result,) = run(bench, "V2O;")
    assert result.ok
    assert board.pin_values[31] == 1


# -- syntax details --------------------------------------------------------


def test_trailing_semicolon_is_optional(bench):
    assert run(bench, "P25")[0].ok


def test_case_and_whitespace_do_not_matter(bench, board):
    run(bench, "  v1o ; p 12.5 ;  ")
    assert board.pin_values[32] == 1
    assert bench.pwm_percent("pump") == 12.5


def test_commands_chain_in_order(bench, board):
    results = run(bench, "V1O;V2O;P60;")
    assert all(r.ok for r in results)
    assert board.pin_values[32] == 1
    assert board.pin_values[31] == 1
    assert board.pwm[33][0] == 2457


def test_a_failure_stops_the_rest_of_the_line(bench, board):
    """`V1O;P150;P10;` must not quietly apply the last command: the operator's
    intent for the line is already broken by then."""
    results = execute_line(bench, "V1O;P150;P10;")
    assert [r.ok for r in results] == [True, False]
    assert bench.pwm_percent("pump") == 0.0


def test_split_ignores_empty_segments():
    assert split(";;V1O;;P50;;") == ["V1O", "P50"]


# -- queries ---------------------------------------------------------------


def test_valve_query(bench):
    bench.set_valve("valve1", True)
    (result,) = run(bench, "V1?")
    assert result.ok
    assert "OPEN" in result.text


def test_pump_query(bench):
    bench.set_pwm("pump", 42.0)
    (result,) = run(bench, "P?")
    assert "42.0 %" in result.text


def test_flow_query_reports_value_and_volts(bench, board):
    board.adc = lambda channel: 2234  # ~1.8 V
    (result,) = run(bench, "F?")
    assert result.ok
    assert "mL/min" in result.text
    assert "V" in result.text


def test_writing_to_an_input_is_refused_with_a_useful_message(bench):
    (result,) = execute_line(bench, "F50")
    assert not result.ok
    assert "input" in result.text


# -- actions ---------------------------------------------------------------


def test_toggle(bench, board):
    run(bench, "V1T")
    assert board.pin_values[32] == 1
    run(bench, "V1T")
    assert board.pin_values[32] == 0


def test_relative_pump_adjustment(bench):
    run(bench, "P40")
    run(bench, "P+15")
    assert bench.pwm_percent("pump") == 55.0
    run(bench, "P-5")
    assert bench.pwm_percent("pump") == 50.0


def test_relative_adjustment_cannot_leave_the_valid_range(bench):
    run(bench, "P95")
    (result,) = execute_line(bench, "P+10")
    assert not result.ok
    assert bench.pwm_percent("pump") == 95.0


def test_stop_puts_everything_safe(bench, board):
    run(bench, "V1O;V2O;P100;")
    (result,) = run(bench, "X")
    assert result.ok
    assert board.pwm[33][0] == 0
    assert board.pin_values[32] == 0
    assert board.pin_values[31] == 0


def test_quit_is_flagged_for_the_ui(bench):
    (result,) = run(bench, "Q")
    assert result.should_quit


def test_status_is_flagged_for_the_ui(bench):
    (result,) = run(bench, "S")
    assert result.show_status


# -- errors ----------------------------------------------------------------


def test_unknown_command(bench):
    (result,) = execute_line(bench, "W1O")
    assert not result.ok
    assert "unknown command" in result.text


def test_unknown_valve_action_lists_the_valid_ones(bench):
    (result,) = execute_line(bench, "V1Z")
    assert not result.ok
    assert "open" in result.text and "close" in result.text


def test_a_non_numeric_pump_setpoint_says_what_was_expected(bench):
    (result,) = execute_line(bench, "Pfast")
    assert not result.ok
    assert "P50" in result.text


def test_out_of_range_setpoint(bench):
    (result,) = execute_line(bench, "P101")
    assert not result.ok
    assert "0-100" in result.text


def test_a_dropped_link_is_reported_not_raised(bench):
    class Dead:
        def read(self, size=1):
            raise OSError("cable yanked")

        def write(self, data):
            raise OSError("cable yanked")

        def close(self):
            pass

    bench.device._t = Dead()
    result = execute(bench, "V1O")
    assert not result.ok
    assert "cable yanked" in result.text


def test_an_empty_command_is_harmless(bench):
    assert execute(bench, "   ").ok


# -- extensibility ---------------------------------------------------------


def test_help_is_generated_from_the_config(bench):
    text = help_text(bench)
    for expected in ("V1O", "V2O", "P<0-100>", "F?", "pin 32", "pin 33", "50 kHz"):
        assert expected in text


def test_a_new_valve_needs_no_change_to_the_parser(board):
    """The extensibility claim, tested: add a spec, get a command."""
    config = BenchConfig(valves=(ValveSpec("drain", "V7", 12, description="Drain valve"),))
    bench = Bench(Device(FakeDevice(board)), config)

    (result,) = execute_line(bench, "V7O")
    assert result.ok
    assert board.pin_values[12] == 1
    assert "V7O" in help_text(bench)


def test_longer_codes_win_so_v1_cannot_shadow_v10(board):
    config = BenchConfig(
        valves=(
            ValveSpec("one", "V1", 2, description="One"),
            ValveSpec("ten", "V10", 3, description="Ten"),
        )
    )
    bench = Bench(Device(FakeDevice(board)), config)

    execute_line(bench, "V10O")
    assert board.pin_values[3] == 1
    assert board.pin_values.get(2, 0) == 0
