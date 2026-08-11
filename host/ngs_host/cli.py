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
from .keyboard import stdin_is_interactive
from .protocol import AutotuneFail, AutotuneState, MsgType, TuningRule
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
    # The resolved port, not "auto": which board you are actually talking to is
    # the whole point of the label once a second one shows up on the bench.
    return Bench(device), device.port or "?"


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
    watchdog: Annotated[
        int,
        typer.Option(
            "--watchdog",
            help="Latch the E-stop if the host goes quiet for this many ms. 0 disables.",
        ),
    ] = 0,
) -> None:
    """Live dashboard: polls at 2 Hz and takes commands (V1O; P50; ...)."""
    # Checked before opening the port: if we cannot run, there is no reason to
    # have driven the outputs to their safe state on the way to failing.
    if not stdin_is_interactive():
        console.print(
            "[red]the dashboard needs an interactive terminal.[/red]\n"
            "Run it directly in a console, or use the one-shot commands "
            "(ngs send / ngs pump / ngs valve / ngs flow) when piping or scripting.",
        )
        raise typer.Exit(2)

    obj, label = _open(port, sim, timeout)
    with obj.device:
        if not no_init:
            obj.initialize(watchdog_ms=watchdog)
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
def selftest(
    pings: Annotated[int, typer.Option(help="Round trips in the link soak.")] = 500,
    port: PortOpt = None,
    sim: SimOpt = False,
) -> None:
    """Verify the board itself, before it is wired to anything.

    Drives the valve pins and the pump pin to check the silicon does what the
    firmware claims. Safe with nothing connected; once the board is wired,
    these same writes move real hardware.
    """
    from .selftest import run

    obj, label = _open(port, sim)
    console.print(f"[dim]running against {label} -- this takes a few seconds[/dim]\n")

    with obj.device:
        report = run(obj, pings=pings)

    width = max(len(c.name) for c in report.checks)
    for check in report.checks:
        if not check.ok:
            mark, style = "FAIL", "bold red"
        elif check.caveat:
            mark, style = "note", "yellow"
        else:
            mark, style = " ok ", "green"
        console.print(f"[{style}]{mark}[/{style}]  {check.name.ljust(width)}  {check.detail}")

    if report.ok:
        console.print("\n[green]the board is behaving correctly[/green]")
    else:
        console.print("\n[red]something is wrong -- see the failures above[/red]")
        raise typer.Exit(1)


@app.command()
def web(
    http_port: Annotated[int, typer.Option("--http-port", help="Port to serve on.")] = 8765,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Do not open a browser.")
    ] = False,
    port: PortOpt = None,
    sim: SimOpt = False,
    timeout: TimeoutOpt = 1.0,
    watchdog: Annotated[
        int,
        typer.Option(
            "--watchdog",
            help="Latch the E-stop if the host goes quiet for this many ms. 0 disables.",
        ),
    ] = 0,
) -> None:
    """Serve the dashboard as a local web page, for a separate window.

    Same bench and same commands as `ngs bench`, in a browser instead of a
    terminal. Binds to localhost only -- this drives hardware.
    """
    import webbrowser

    from .web import WebBench, serve

    obj, label = _open(port, sim, timeout)
    url = f"http://127.0.0.1:{http_port}/"

    with obj.device:
        obj.initialize(watchdog_ms=watchdog)
        state = WebBench(obj, label)
        try:
            state.fw = obj.device.info().fw_version
        except Exception as exc:  # noqa: BLE001 -- the page just shows "?"
            console.print(f"[yellow]could not read device info: {exc}[/yellow]")

        try:
            server = serve(state, http_port)
        except OSError as exc:
            raise typer.BadParameter(
                f"cannot listen on {url} ({exc}). Another instance running? "
                "Pass --http-port to pick a different one."
            ) from None

        console.print(f"dashboard at [bold]{url}[/bold]   (Ctrl-C to stop)")
        if not no_browser:
            webbrowser.open(url)

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            console.print("\nstopping")
        finally:
            server.server_close()
            # Never leave the pump running because a browser tab was closed.
            obj.stop()


@app.command()
def estop(
    clear: Annotated[
        bool, typer.Option("--clear", help="Release the latch instead of engaging it.")
    ] = False,
    port: PortOpt = None,
    sim: SimOpt = False,
) -> None:
    """Emergency stop: everything to its safe state, latched.

    The device does the work and stays latched until cleared, so this cannot
    half-succeed and cannot be undone by accident.
    """
    obj, _ = _open(port, sim)
    with obj.device:
        if clear:
            obj.clear_estop()
            console.print(
                "emergency stop cleared. Nothing moved -- outputs are still safe."
            )
            return

        obj.estop()
        status = obj.device.status()
        console.print("[bold white on red] EMERGENCY STOP ENGAGED [/bold white on red]")
        console.print(
            f"{status.safe_entries} outputs driven to their safe state and latched. "
            "Run `ngs estop --clear` to release."
        )


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
def auto(
    setpoint: Annotated[
        float | None, typer.Argument(help="Flow setpoint in mL/min. Omit to query.")
    ] = None,
    off: Annotated[bool, typer.Option("--off", help="Return the pump to manual.")] = False,
    port: PortOpt = None,
    sim: SimOpt = False,
) -> None:
    """Put the pump under closed-loop flow control."""
    obj, _ = _open(port, sim)
    with obj.device:
        if off:
            _run(obj, "PM")
        elif setpoint is None:
            _run(obj, "P?")
        else:
            _run(obj, f"PA{setpoint}")


@app.command()
def gains(
    kp: Annotated[float | None, typer.Option(help="Proportional gain, % per mL/min.")] = None,
    ki: Annotated[float | None, typer.Option(help="Integral gain, % per mL/min-second.")] = None,
    kd: Annotated[float | None, typer.Option(help="Derivative gain. 0 on a noisy signal.")] = None,
    port: PortOpt = None,
    sim: SimOpt = False,
) -> None:
    """Show or set the controller gains."""
    obj, _ = _open(port, sim)
    with obj.device:
        if kp is None and ki is None and kd is None:
            _run(obj, "K?")
            return
        cfg = obj.set_gains(kp, ki, kd)
        console.print(f"kp {cfg.kp:g}  ki {cfg.ki:g}  kd {cfg.kd:g}")


@app.command()
def tune(
    setpoint: Annotated[float, typer.Argument(help="Flow to oscillate around, mL/min.")],
    amplitude: Annotated[float, typer.Option(help="Relay step, % output.")] = 10.0,
    hysteresis: Annotated[
        float | None, typer.Option(help="Switching band, mL/min. Defaults to the deadband.")
    ] = None,
    cycles: Annotated[int, typer.Option(help="Limit cycles to average.")] = 4,
    rule: Annotated[
        str, typer.Option(help="tyreus-luyben (default), ziegler-nichols, or pessen.")
    ] = "tyreus-luyben",
    timeout: Annotated[float, typer.Option(help="Give up after this many seconds.")] = 180.0,
    adopt: Annotated[bool, typer.Option("--adopt", help="Apply the gains if it succeeds.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
    port: PortOpt = None,
    sim: SimOpt = False,
) -> None:
    """Autotune the flow loop by relay feedback.

    The pump is driven up and down around `setpoint` on purpose until the flow
    settles into a limit cycle; its amplitude and period give the ultimate gain
    and period, and the tuning rule turns those into gains.
    """
    import time

    rules = {
        "tyreus-luyben": TuningRule.TYREUS_LUYBEN,
        "ziegler-nichols": TuningRule.ZIEGLER_NICHOLS,
        "pessen": TuningRule.PESSEN,
    }
    if rule not in rules:
        raise typer.BadParameter(f"rule must be one of: {', '.join(rules)}")

    obj, _ = _open(port, sim)
    with obj.device:
        if not yes and not typer.confirm(
            f"\nThis oscillates the pump around {setpoint:g} mL/min for up to "
            f"{timeout:g}s. Flow path open and ready?"
        ):
            raise typer.Abort

        obj.start_autotune(
            setpoint,
            amplitude=amplitude,
            hysteresis=hysteresis,
            cycles=cycles,
            rule=rules[rule],
            timeout_s=timeout,
        )

        deadline = time.monotonic() + timeout + 10
        try:
            with console.status("running the relay experiment...") as status:
                while time.monotonic() < deadline:
                    time.sleep(0.5)
                    result = obj.autotune_result()
                    state = obj.control_state()
                    status.update(
                        f"{result.state_name.lower()}, {result.cycles_done} cycles, "
                        f"flow {state.measurement:.0f}, output {state.output:.0f} %"
                    )
                    if not result.running:
                        break
        finally:
            # However we leave -- Ctrl-C, timeout, an exception -- the board
            # must not be left driving the pump up and down on its own.
            if obj.autotune_result().running:
                obj.abort_autotune()
                obj.stop()
                console.print("[yellow]autotune still running on exit -- aborted[/yellow]")

        result = obj.autotune_result()
        if result.state != AutotuneState.DONE:
            console.print(f"[red]autotune {result.state_name}: {result.fail_name.lower()}[/red]")
            if result.state in (AutotuneState.SETTLING, AutotuneState.RELAY):
                console.print(
                    "[dim]It never completed a limit cycle. The flow has to actually swing "
                    "past the setpoint: check the valves are open and the pump is running, "
                    "and give it a longer --timeout.[/dim]"
                )
            if result.fail_reason == AutotuneFail.NO_SWING:
                console.print(
                    "[dim]The flow barely moved. Raise --amplitude, lower --hysteresis, "
                    "or check the valves are actually open.[/dim]"
                )
            raise typer.Exit(1)

        table = Table(box=None, show_header=False)
        table.add_row("cycles", str(result.cycles_done))
        table.add_row("ultimate gain Ku", f"{result.ku:.4g}")
        table.add_row("ultimate period Tu", f"{result.tu:.3g} s")
        table.add_row("process swing", f"{result.amplitude:.1f} mL/min")
        table.add_row("period spread", f"{result.spread * 100:.0f} %")
        table.add_row("", "")
        table.add_row("kp", f"{result.kp:.4g}")
        table.add_row("ki", f"{result.ki:.4g}")
        table.add_row("kd", f"{result.kd:.4g}")
        console.print(table)

        if not result.trustworthy:
            console.print(
                "[yellow]The cycles were not consistent -- treat these numbers with "
                "suspicion and re-run with more --cycles.[/yellow]"
            )

        if adopt:
            obj.adopt_autotune()
            console.print("\n[green]gains applied[/green]")
        else:
            console.print("\n[dim]Not applied. Re-run with --adopt, or send TA.[/dim]")


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
