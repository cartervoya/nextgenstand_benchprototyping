"""Dashboard tests.

The rendering is a pure function of a snapshot and the loop is separable from
the terminal, so both can be checked headlessly. What is *not* covered here is
the raw-keyboard path -- that needs a real tty.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from ngs_host.bench import Bench
from ngs_host.device import Device
from ngs_host.fake import FakeBoard, FakeDevice
from ngs_host.keyboard import BACKSPACE, CTRL_C, ENTER, LineEditor
from ngs_host.ui import POLL_INTERVAL, Dashboard, render


@pytest.fixture
def board() -> FakeBoard:
    return FakeBoard()


@pytest.fixture
def dash(board: FakeBoard) -> Dashboard:
    bench = Bench(Device(FakeDevice(board)))
    bench.initialize()
    dash = Dashboard(bench, port="sim", console=Console(file=io.StringIO(), width=100))
    dash.poll()
    return dash


def to_text(dash: Dashboard) -> str:
    console = Console(file=io.StringIO(), width=100)
    console.print(
        render(
            dash.snapshot,
            list(dash.log),
            dash.editor.buffer,
            port="sim",
            fw="0.1.0",
            poll_hz=2.0,
        )
    )
    return console.file.getvalue()


def test_polls_at_2_hz():
    assert POLL_INTERVAL == 0.5


def test_every_channel_appears_on_screen(dash):
    text = to_text(dash)
    for expected in ("Pump speed", "Valve 1", "Valve 2", "Flow meter", "CLOSED"):
        assert expected in text


def test_readings_are_shown_with_units(dash, board):
    board.adc = lambda channel: 2234
    dash.poll()
    assert "mL/min" in to_text(dash)


def test_the_typed_command_is_echoed(dash):
    dash.editor.buffer = "V1O"
    assert "V1O" in to_text(dash)


def test_a_command_updates_the_display_immediately(dash):
    """Without this the operator waits up to half a second to see a valve
    move, which reads as an unresponsive UI."""
    assert dash.handle("V1O")
    assert dash.snapshot.valves["valve1"].is_open
    assert "OPEN" in to_text(dash)


def test_quit_stops_the_loop(dash):
    assert dash.handle("Q") is False


def test_a_failed_command_is_shown_but_does_not_stop_the_dashboard(dash):
    assert dash.handle("P900") is True
    assert any(not line.ok for line in dash.log)
    assert "0-100" in to_text(dash)


def test_a_broken_link_is_visible_rather_than_silent(dash):
    class Dead:
        def read(self, size=1):
            raise OSError("device disconnected")

        def write(self, data):
            raise OSError("device disconnected")

        def close(self):
            pass

    dash.bench.device._t = Dead()
    dash.poll()
    assert "LINK ERROR" in to_text(dash)


def test_an_output_mismatch_takes_over_the_header(dash, board):
    """If the board reset and dropped the valves, that has to be the first
    thing on screen -- not a number buried in a table."""
    dash.handle("V1O")
    board.pin_values.clear()
    dash.poll()

    text = to_text(dash)
    assert "OUTPUT MISMATCH" in text
    assert "V1" in text


def test_status_command_renders_the_counters(dash):
    dash.handle("S")
    assert any("uptime" in line.text for line in dash.log)


def test_measured_poll_rate_is_reported_not_assumed(dash):
    for _ in range(3):
        dash.poll()
    assert dash.poll_hz > 0


# -- line editing ----------------------------------------------------------


def test_line_editor_returns_completed_lines():
    editor = LineEditor()
    assert editor.feed("V1O") == []
    assert editor.feed(ENTER) == ["V1O"]
    assert editor.buffer == ""


def test_backspace():
    editor = LineEditor()
    editor.feed("P50" + BACKSPACE)
    assert editor.buffer == "P5"


def test_escape_clears_the_line():
    editor = LineEditor()
    editor.feed("P50\x1b")
    assert editor.buffer == ""


def test_several_commands_in_one_burst():
    editor = LineEditor()
    assert editor.feed(f"V1O{ENTER}P50{ENTER}") == ["V1O", "P50"]


def test_ctrl_c_raises_so_the_caller_can_shut_down_safely():
    """It must not be swallowed -- the exit path is what stops the pump."""
    editor = LineEditor()
    with pytest.raises(KeyboardInterrupt):
        editor.feed(CTRL_C)


def test_history_is_kept():
    editor = LineEditor()
    editor.feed(f"V1O{ENTER}")
    assert editor.history == ["V1O"]
