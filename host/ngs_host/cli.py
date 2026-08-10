"""`ngs` -- the command line entry point.

Two ways in, sharing one code path:

    ngs bench                 the live dashboard (2 Hz poll + command line)
    ngs send "V1O;P50;"       the same command language, one shot
    ngs pump 50 / ngs flow    conveniences for scripts and muscle memory

Every command takes --sim, which swaps the serial port for the simulator so
the whole thing runs with no board attached.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .bench import BENCH_CONFIG, Bench
from .commands import execute_line
from .device import Device, find_ports
from .protocol import MsgType
from .sim import make_sim_device
from .ui import Dashboard

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Host tools for the NextGen Stand Teensy 4.1 bench.",
)
console = Console()

PortOpt = Annotated[str | None, typer.Option("--port", "-p", help="Serial port, e.g. COM7.")]
SimOpt = Annotated[bool, typer.Option("--sim", help="Use the simulator instead of hardware.")]
TimeoutOpt = Annotated[float, typer.Option("--timeout", help="Per-command timeout, seconds.")]


def _open(port: str | None, sim: bool, timeout: float = 1.0) -> tuple[Bench, str]:
    """Open the bench and return it with a label for the display."""
    if sim:
        return Bench(Device(make_sim_device(), timeout=timeout)), "sim"
    try:
        device = Device.open(port, timeout=timeout)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from None
    return Bench(device), port or "auto"


def _run(bench: Bench, line: str) -> None:
    """Execute a command line and print each result, exiting non-zero on the
    first failure so shell scripts can rely on it."""
    failed = False
    for result in execute_line(bench, line):
        if result.show_status:
            _print_status(bench)
        elif result.text:
            console.print(result.text, style="" if result.ok else "red")
        failed |= not result.ok
    if failed:
        raise typer.Exit(1)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@app.command()
def ports() -> None:
    """List attached Teensy boards."""
    found = find_ports()
    if not found:
        console.print("no Teensy found", style="yellow")
        raise typer.Exit(1)

    table = Table("port", "serial", "description", box=None)
    for pt in found:
        table.add_row(pt.device, pt.serial_number or "-", pt.description)
    console.print(table)


@app.command()
def info(port: PortOpt = None, sim: SimOpt = False) -> None:
    """Firmware identity and protocol version."""
    bench, _ = _open(port, sim)
    with bench.device:
        data = bench.device.info()
        table = Table(box=None, show_header=False)
        table.add_row("protocol", str(data.proto_version))
        table.add_row("firmware", data.fw_version)
        table.add_row("cpu", f"{data.cpu_hz / 1e6:.0f} MHz")
        table.add_row("max payload", f"{data.max_payload} B")
        table.add_row("mcu serial", data.serial_hex)
        console.print(table)


@app.command()
def status(port: PortOpt = None, sim: SimOpt = False) -> None:
    """Link and health counters."""
    bench, _ = _open(port, sim)
    with bench.device:
        _print_status(bench)


def _print_status(bench: Bench) -> None:
    st = bench.device.status()
    table = Table(box=None, show_header=False)
    table.add_row("uptime", f"{st.uptime_us / 1e6:.1f} s")
    table.add_row("frames rx / tx", f"{st.rx_frames} / {st.tx_frames}")
    table.add_row("crc errors", str(st.rx_crc_errors))
    table.add_row("overflows", str(st.rx_overflows))
    table.add_row("loop max", f"{st.loop_max_us} us")
    table.add_row("die temp", f"{st.temp_c:.1f} C")
    host_errors = bench.device.framing_errors
    table.add_row("host-side rejects", str(len(host_errors)))
    console.print(table)


@app.command()
def ping(
    count: Annotated[int, typer.Option("--count", "-n", help="How many pings.")] = 5,
    port: PortOpt = None,
    sim: SimOpt = False,
) -> None:
    """Measure round-trip time."""
    import time

    bench, _ = _open(port, sim)
    with bench.device:
        rtts = []
        for _ in range(count):
            t0 = time.perf_counter()
            bench.device.ping()
            rtts.append((time.perf_counter() - t0) * 1000)
            console.print(f"seq ok  {rtts[-1]:.3f} ms")
        console.print(
            f"\nmin {min(rtts):.3f}  mean {sum(rtts) / len(rtts):.3f}  max {max(rtts):.3f} ms",
            style="bold",
        )


# --------------------------------------------------------------------------
# Bench control
# --------------------------------------------------------------------------


@app.command()
def bench(
    port: PortOpt = None,
    sim: SimOpt = False,
    timeout: TimeoutOpt = 1.0,
    no_init: Annotated[
        bool, typer.Option("--no-init", help="Do not force a safe state on start.")
    ] = False,
) -> None:
    """Live dashboard: polls at 2 Hz and takes commands (V1O; P50; ...)."""
    obj, label = _open(port, sim, timeout)
    with obj.device:
        if not no_init:
            obj.initialize()
        try:
            Dashboard(obj, port=label, console=console).run()
        finally:
            # Whatever happened -- crash, quit, Ctrl-C -- the pump does not
            # get left running.
            obj.stop()


@app.command()
def check(
    outputs: Annotated[
        bool, typer.Option("--outputs", help="Also exercise the valves and the pump.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
    port: PortOpt = None,
    sim: SimOpt = False,
) -> None:
    """Bring-up check for a freshly flashed board.

    Read-only by default: it identifies the firmware, measures the link, and
    reads the flow meter without driving anything. `--outputs` additionally
    cycles the valves and ramps the pump, which moves real hardware.
    """
    import time

    obj, label = _open(port, sim)
    ok = True

    with obj.device:
        console.print(f"[bold]port[/bold]          {label}")

        data = obj.device.info()
        console.print(
            f"[bold]firmware[/bold]      {data.fw_version}, protocol v{data.proto_version}"
        )
        console.print(f"[bold]mcu[/bold]           {data.serial_hex} @ {data.cpu_hz / 1e6:.0f} MHz")

        rtts = []
        for _ in range(20):
            t0 = time.perf_counter()
            obj.device.ping()
            rtts.append((time.perf_counter() - t0) * 1000)
        console.print(
            f"[bold]round trip[/bold]    min {min(rtts):.2f} / mean "
            f"{sum(rtts) / len(rtts):.2f} / max {max(rtts):.2f} ms"
        )

        st = obj.device.status()
        console.print(f"[bold]die temp[/bold]      {st.temp_c:.1f} C")
        console.print(f"[bold]loop max[/bold]      {st.loop_max_us} us")
        if st.rx_crc_errors or st.rx_overflows:
            console.print(
                f"[red]link errors: {st.rx_crc_errors} crc, {st.rx_overflows} overflow[/red]"
            )
            ok = False

        for spec in BENCH_CONFIG.analogs:
            reading = obj.read_analog(spec.name)
            console.print(
                f"[bold]{spec.name}[/bold]          {reading.text}"
                f"   ({reading.volts:.3f} V, raw {reading.raw})"
            )
            if reading.faulted:
                console.print(
                    f"[red]  {spec.name}: under {spec.v_min} V -- "
                    "sensor unpowered, loop open, or sense resistor wrong[/red]"
                )
                ok = False

        if outputs:
            if not yes and not typer.confirm("\nThis will move valves and run the pump. Continue?"):
                raise typer.Abort
            ok &= _exercise_outputs(obj)
        else:
            console.print(
                "\n[dim]outputs not touched. Re-run with --outputs to cycle "
                "the valves and pump.[/dim]"
            )

    console.print("\n[green]all checks passed[/green]" if ok else "\n[red]problems above[/red]")
    if not ok:
        raise typer.Exit(1)


def _exercise_outputs(obj: Bench) -> bool:
    """Cycle each valve and ramp the pump, verifying the board agrees."""
    import time

    ok = True
    obj.initialize()

    for spec in BENCH_CONFIG.valves:
        for want_open in (True, False):
            obj.set_valve(spec.name, want_open)
            time.sleep(0.2)  # let a solenoid actually move before believing the pin
            reading = obj.read_valve(spec.name)
            state = "open" if want_open else "close"
            if reading.mismatch:
                console.print(f"[red]{spec.code} {state}: pin reads {reading.text}[/red]")
                ok = False
            else:
                console.print(f"{spec.code} {state}: [green]ok[/green]")

    # Open the flow path before running the pump. Ramping into closed valves
    # dead-heads it, which is the one thing this check must not do.
    console.print("\n[dim]opening valves for the pump ramp[/dim]")
    for spec in BENCH_CONFIG.valves:
        obj.set_valve(spec.name, True)
    time.sleep(0.3)

    try:
        for spec in BENCH_CONFIG.pwms:
            for percent in (25.0, 50.0, 0.0):
                obj.set_pwm(spec.name, percent)
                time.sleep(1.0)  # let the flow settle before reading it
                flows = [obj.read_analog(a.name).text for a in BENCH_CONFIG.analogs]
                console.print(f"{spec.code} at {percent:5.1f} %   " + "  ".join(flows))
    finally:
        # stop() drops the pump first, then closes -- see Bench.stop().
        obj.stop()

    return ok


@app.command()
def send(
    line: Annotated[str, typer.Argument(help='Commands, e.g. "V1O;P50;"')],
    port: PortOpt = None,
    sim: SimOpt = False,
) -> None:
    """Run bench commands without the dashboard."""
    obj, _ = _open(port, sim)
    with obj.device:
        _run(obj, line)


@app.command()
def pump(
    percent: Annotated[float, typer.Argument(help="Duty cycle, 0-100.")],
    port: PortOpt = None,
    sim: SimOpt = False,
) -> None:
    """Set the pump setpoint."""
    obj, _ = _open(port, sim)
    with obj.device:
        _run(obj, f"P{percent}")


@app.command()
def valve(
    which: Annotated[str, typer.Argument(help="Valve code or name, e.g. V1 or valve1.")],
    action: Annotated[str, typer.Argument(help="open | close | toggle | read")],
    port: PortOpt = None,
    sim: SimOpt = False,
) -> None:
    """Drive or read a valve."""
    codes = {v.code.upper(): v for v in BENCH_CONFIG.valves}
    names = {v.name.lower(): v for v in BENCH_CONFIG.valves}
    spec = codes.get(which.upper()) or names.get(which.lower())
    if spec is None:
        known = ", ".join(f"{v.code}/{v.name}" for v in BENCH_CONFIG.valves)
        raise typer.BadParameter(f"unknown valve {which!r}. Known: {known}")

    letter = {"open": "O", "close": "C", "toggle": "T", "read": "?"}.get(action.lower())
    if letter is None:
        raise typer.BadParameter("action must be open, close, toggle or read")

    obj, _ = _open(port, sim)
    with obj.device:
        _run(obj, f"{spec.code}{letter}")


@app.command()
def flow(
    watch: Annotated[
        bool, typer.Option("--watch", "-w", help="Poll at 2 Hz until Ctrl-C.")
    ] = False,
    port: PortOpt = None,
    sim: SimOpt = False,
) -> None:
    """Read the flow meter."""
    import time

    obj, _ = _open(port, sim)
    with obj.device:
        while True:
            reading = obj.read_analog("flow")
            console.print(
                f"{reading.value:8.1f} {reading.spec.unit}   "
                f"{reading.volts:.3f} V   raw {reading.raw}",
                style="red" if reading.faulted else "",
            )
            if not watch:
                return
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                return


@app.command()
def reset(port: PortOpt = None, sim: SimOpt = False) -> None:
    """Reboot the board."""
    obj, _ = _open(port, sim)
    with obj.device:
        obj.device.reset()
    console.print("reset sent; the port will re-enumerate in a moment")


@app.command()
def raw(
    msg_type: Annotated[str, typer.Argument(help="Message name, e.g. PING or GET_STATUS.")],
    payload_hex: Annotated[str, typer.Argument(help="Payload as hex.")] = "",
    port: PortOpt = None,
    sim: SimOpt = False,
) -> None:
    """Send one protocol message by name. For poking at new firmware."""
    try:
        msg = MsgType[msg_type.upper()]
    except KeyError:
        known = ", ".join(m.name for m in MsgType)
        raise typer.BadParameter(f"unknown message {msg_type!r}. Known: {known}") from None

    obj, _ = _open(port, sim)
    with obj.device:
        resp = obj.device.transact(msg, bytes.fromhex(payload_hex))
        console.print(f"{len(resp)} B: {resp.hex(' ') or '(empty)'}")


if __name__ == "__main__":
    app()
