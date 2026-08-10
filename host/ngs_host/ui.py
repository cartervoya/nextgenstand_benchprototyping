"""The bench dashboard: polls at 2 Hz, takes commands on the same line.

One loop does everything -- no threads. It ticks fast enough that typing feels
immediate, and polls the board on its own slower schedule:

    every ~40 ms   drain the keyboard, redraw
    every 500 ms   poll the bench (2 Hz)

Rendering is split out from the loop so the layout can be tested without a
terminal, a board, or a running clock.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .bench import Bench, Snapshot, ValveReading
from .commands import CommandResult, execute_line, help_text
from .keyboard import LineEditor, raw_mode, read_keys, stdin_is_interactive

#: 2 Hz, as specified. Each poll is 4-5 round trips; USB CDC round trips are
#: well under a millisecond, so this costs nothing and could go much faster.
POLL_INTERVAL = 0.5

#: UI tick. Fast enough that keystrokes echo instantly, slow enough to idle.
TICK = 0.04

LOG_LINES = 8


@dataclass
class LogLine:
    text: str
    ok: bool = True


def render_channels(snapshot: Snapshot) -> Table:
    """The live value table. Pure function of a snapshot, so it renders the
    same in a test as on the bench."""
    table = Table(box=None, pad_edge=False, expand=True)
    table.add_column("", style="dim", width=4)
    table.add_column("channel", style="bold", min_width=14)
    table.add_column("value", justify="right", min_width=18)
    table.add_column("detail", style="dim")

    for reading in snapshot.pwms.values():
        spec = reading.spec
        table.add_row(
            spec.code,
            spec.description,
            Text(reading.text, style="cyan"),
            f"pin {spec.pin}, {spec.freq_hz / 1000:g} kHz, {spec.resolution}-bit",
        )

    for reading in snapshot.valves.values():
        spec = reading.spec
        table.add_row(
            spec.code,
            spec.description,
            Text(reading.text, style=_valve_style(reading)),
            f"pin {spec.pin}",
        )

    for reading in snapshot.analogs.values():
        spec = reading.spec
        table.add_row(
            spec.code,
            spec.description,
            Text(reading.text, style="red" if reading.faulted else "cyan"),
            f"pin {spec.pin}, {reading.volts:.3f} V, raw {reading.raw}",
        )

    return table


def _valve_style(reading: ValveReading) -> str:
    if reading.mismatch:
        return "bold red"
    return "green" if reading.is_open else "yellow"


def render_status(snapshot: Snapshot, *, port: str, fw: str, poll_hz: float) -> Text:
    """The one-line header: is the link healthy, and is the board the one we
    think it is."""
    if snapshot.error:
        return Text(f"{port}  LINK ERROR  {snapshot.error}", style="bold red")

    if snapshot.mismatched_valves:
        names = ", ".join(r.spec.code for r in snapshot.mismatched_valves)
        return Text(
            f"{port}  OUTPUT MISMATCH on {names} -- did the board reset? "
            f"Press Z to re-apply the safe state.",
            style="bold red",
        )

    parts = [f"{port}", f"fw {fw}", f"{poll_hz:.1f} Hz"]
    if snapshot.status is not None:
        st = snapshot.status
        parts += [
            f"up {st.uptime_us / 1e6:.1f}s",
            f"rx {st.rx_frames}",
            f"tx {st.tx_frames}",
            f"crc-err {st.rx_crc_errors}",
            f"loop-max {st.loop_max_us} us",
            f"{st.temp_c:.1f} C",
        ]
    style = "bold red" if snapshot.status and snapshot.status.rx_crc_errors else "green"
    return Text("  ".join(parts), style=style)


def render(
    snapshot: Snapshot,
    log: list[LogLine],
    prompt: str,
    *,
    port: str,
    fw: str,
    poll_hz: float,
) -> RenderableType:
    body = Group(
        render_status(snapshot, port=port, fw=fw, poll_hz=poll_hz),
        Text(),
        render_channels(snapshot),
        Text(),
        *[Text(line.text, style="" if line.ok else "red") for line in log[-LOG_LINES:]],
        Text(),
        Text.assemble(("> ", "bold cyan"), (prompt, "bold"), ("_", "dim")),
    )
    return Panel(body, title="NextGen Stand bench", subtitle="? for help, Q to quit")


class Dashboard:
    """The interactive dashboard. `run()` blocks until the user quits."""

    def __init__(self, bench: Bench, *, port: str = "-", console: Console | None = None) -> None:
        self.bench = bench
        self.port = port
        self.console = console or Console()
        self.log: deque[LogLine] = deque(maxlen=200)
        self.editor = LineEditor()
        self.snapshot = Snapshot(monotonic=time.monotonic())
        self.fw = "?"
        self._poll_times: deque[float] = deque(maxlen=10)

    # -- data --------------------------------------------------------------

    def poll(self) -> None:
        now = time.monotonic()
        self.snapshot = self.bench.poll()
        self._poll_times.append(now)

    @property
    def poll_hz(self) -> float:
        """Measured, not assumed -- if the link slows down, the header says so
        instead of claiming 2 Hz while showing stale numbers."""
        if len(self._poll_times) < 2:
            return 0.0
        span = self._poll_times[-1] - self._poll_times[0]
        return (len(self._poll_times) - 1) / span if span > 0 else 0.0

    def handle(self, line: str) -> bool:
        """Run one command line. Returns False when the user asked to quit."""
        self.log.append(LogLine(f"> {line}", ok=True))
        for result in execute_line(self.bench, line):
            self._log_result(result)
            if result.should_quit:
                return False
        # Reflect the change immediately rather than waiting up to 500 ms.
        self.poll()
        return True

    def _log_result(self, result: CommandResult) -> None:
        if result.show_status:
            status = self.snapshot.status
            text = "no status yet" if status is None else (
                f"uptime {status.uptime_us / 1e6:.1f} s, rx {status.rx_frames}, "
                f"tx {status.tx_frames}, crc-err {status.rx_crc_errors}, "
                f"overflow {status.rx_overflows}, loop-max {status.loop_max_us} us, "
                f"{status.temp_c:.1f} C"
            )
            self.log.append(LogLine(text))
            return
        for line in result.text.splitlines():
            if line:
                self.log.append(LogLine(line, ok=result.ok))

    # -- loop --------------------------------------------------------------

    def run(self) -> None:
        if not stdin_is_interactive():
            raise RuntimeError(
                "the dashboard needs an interactive terminal. "
                "Use the one-shot commands (ngs pump / ngs valve / ngs flow) when piping."
            )

        try:
            self.fw = self.bench.device.info().fw_version
        except Exception as exc:  # noqa: BLE001 -- the header just shows "?"
            self.log.append(LogLine(f"could not read device info: {exc}", ok=False))

        self.log.append(LogLine(help_text(self.bench)))
        self.poll()

        next_poll = time.monotonic() + POLL_INTERVAL
        live = Live(self._frame(), console=self.console, screen=True, auto_refresh=False)
        with raw_mode(), live:
            while True:
                try:
                    for line in self.editor.feed(read_keys()):
                        if not self.handle(line):
                            return
                except KeyboardInterrupt:
                    return

                now = time.monotonic()
                if now >= next_poll:
                    self.poll()
                    # Fixed cadence rather than sleep-driven drift; if a poll
                    # overruns, skip ahead instead of trying to catch up.
                    next_poll += POLL_INTERVAL
                    if now >= next_poll:
                        next_poll = now + POLL_INTERVAL

                live.update(self._frame(), refresh=True)
                time.sleep(TICK)

    def _frame(self) -> RenderableType:
        return render(
            self.snapshot,
            list(self.log),
            self.editor.buffer,
            port=self.port,
            fw=self.fw,
            poll_hz=self.poll_hz,
        )
