"""The bench command language.

    V1O;        open valve 1              P100;   pump to 100 %
    V1C;        close valve 1             P37.5;  pump to 37.5 %
    V1T;        toggle valve 1            P+5;    pump 5 % faster
    V1?;        query valve 1             P?;     query the pump setpoint
    VO; VC;     all valves open/closed
    F?;         read the flow meter       X;      stop: everything safe
    S;          device status             Z;      re-initialise the bench
    ?;          help                      Q;      quit

Commands chain: `V1O;V2C;P50;` runs three in order. Whitespace is ignored,
case does not matter, and the trailing `;` is optional on the last one.

Nothing here enumerates the hardware. Codes come from the bench config, so a
new valve in BENCH_CONFIG is immediately typeable, appears in `?` help, and
needs no edit to this file. Adding a *kind* of hardware means adding one
handler below.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import protocol as p
from .bench import AnalogInputSpec, Bench, PwmOutputSpec, ValveSpec
from .device import Timeout
from .protocol import NgsError

#: Commands that act on the bench as a whole rather than one channel.
GLOBAL_ALIASES: dict[str, str] = {
    # Every valve at once. Exact matches, so they cannot shadow V1O/V2C --
    # those are resolved later, by channel code.
    "VO": "valves_open",
    "VOPEN": "valves_open",
    "VC": "valves_close",
    "VCLOSE": "valves_close",
    "!": "estop",
    "E": "estop",
    "ESTOP": "estop",
    "EC": "estop_clear",
    "CLEAR": "estop_clear",
    "S": "status",
    "STATUS": "status",
    "X": "stop",
    "STOP": "stop",
    "Z": "init",
    "INIT": "init",
    "?": "help",
    "H": "help",
    "HELP": "help",
    "Q": "quit",
    "QUIT": "quit",
    "EXIT": "quit",
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    ok: bool
    text: str
    #: Set by `quit` so a UI knows to exit; every other command leaves it False.
    should_quit: bool = False
    #: Set by `status`, which the UI renders itself rather than as a line.
    show_status: bool = False


class CommandError(Exception):
    """The input did not parse, or asked for something outside a valid range."""


def split(text: str) -> list[str]:
    """Split a line into individual commands, dropping empties."""
    return [part.strip() for part in text.split(";") if part.strip()]


def execute_line(bench: Bench, line: str) -> list[CommandResult]:
    """Run every command in `line`, stopping at the first failure.

    Stopping matters: in `V1O;P100;` the pump command almost certainly assumed
    the valve opened. Running it anyway after a failure is how a bench ends up
    dead-heading a pump into a closed valve.
    """
    results: list[CommandResult] = []
    for part in split(line):
        result = execute(bench, part)
        results.append(result)
        if not result.ok:
            break
    return results


def execute(bench: Bench, command: str) -> CommandResult:
    """Run a single command. Never raises for ordinary failures -- a bad
    command or a dropped link comes back as `ok=False` so a UI can print it
    and carry on."""
    text = command.strip()
    if not text:
        return CommandResult(True, "")

    try:
        return _dispatch(bench, text)
    except CommandError as exc:
        return CommandResult(False, str(exc))
    except (NgsError, Timeout, OSError) as exc:
        return CommandResult(False, f"device: {exc}")
    except (KeyError, ValueError) as exc:
        return CommandResult(False, str(exc))


def _dispatch(bench: Bench, text: str) -> CommandResult:
    upper = text.upper()

    # `S` and `S?` mean the same thing; so do `X` and `X?`.
    action = GLOBAL_ALIASES.get(upper) or GLOBAL_ALIASES.get(upper.rstrip("?"))
    if action is not None:
        return _GLOBALS[action](bench)

    # Tuning and autotune are loop-wide rather than per-channel, so they get
    # their own prefixes instead of hanging off a channel code.
    for prefix in sorted(_LOOP_COMMANDS, key=len, reverse=True):
        if upper.startswith(prefix):
            return _LOOP_COMMANDS[prefix](bench, upper[len(prefix) :].strip())

    code, arg = _split_code(bench, upper)
    spec = bench.config.by_code()[code]

    if isinstance(spec, ValveSpec):
        return _valve_command(bench, spec, arg)
    if isinstance(spec, PwmOutputSpec):
        return _pwm_command(bench, spec, arg)
    if isinstance(spec, AnalogInputSpec):
        return _analog_command(bench, spec, arg)
    raise CommandError(f"{code}: no handler for {type(spec).__name__}")


def _split_code(bench: Bench, upper: str) -> tuple[str, str]:
    """Split `V1O` into ("V1", "O").

    Longest code first, so a future "V10" cannot be shadowed by "V1".
    """
    codes = sorted(bench.config.by_code(), key=len, reverse=True)
    for code in codes:
        if upper.startswith(code):
            return code, upper[len(code) :].strip()
    raise CommandError(f"unknown command {upper!r}. Type ? for the list.")


# --------------------------------------------------------------------------
# Per-kind handlers
# --------------------------------------------------------------------------

_VALVE_ACTIONS = {"O": True, "OPEN": True, "1": True, "C": False, "CLOSE": False, "0": False}


def _valve_command(bench: Bench, spec: ValveSpec, arg: str) -> CommandResult:
    if arg in ("", "?"):
        reading = bench.read_valve(spec.name)
        return CommandResult(True, f"{spec.code} {spec.description}: {reading.text}")

    if arg in ("T", "TOGGLE"):
        return _valve_result(spec, bench.toggle_valve(spec.name))

    if (want_open := _VALVE_ACTIONS.get(arg)) is None:
        raise CommandError(
            f"{spec.code}: expected O (open), C (close), T (toggle) or ?, got {arg!r}"
        )

    bench.set_valve(spec.name, want_open)
    return _valve_result(spec, want_open)


def _valve_result(spec: ValveSpec, is_open: bool) -> CommandResult:
    return CommandResult(
        True, f"{spec.code} {spec.description}: {'OPEN' if is_open else 'CLOSED'}"
    )


def _has_loop(bench: Bench, spec: PwmOutputSpec) -> bool:
    return any(c.output == spec.name for c in bench.config.controls)


def _pwm_command(bench: Bench, spec: PwmOutputSpec, arg: str) -> CommandResult:
    if arg in ("", "?"):
        if _has_loop(bench, spec):
            state = bench.control_state()
            if state.mode != p.PumpMode.MANUAL:
                flags = ", ".join(state.flag_names())
                return CommandResult(
                    True,
                    f"{spec.code} {spec.description}: AUTO, setpoint {state.setpoint_target:.1f}"
                    f" (now {state.setpoint:.1f}), flow {state.measurement:.1f},"
                    f" output {state.output:.1f} %" + (f"  [{flags}]" if flags else ""),
                )
        return CommandResult(
            True, f"{spec.code} {spec.description}: MANUAL, {bench.pwm_percent(spec.name):.1f} %"
        )

    if arg in ("M", "MAN", "MANUAL"):
        if not _has_loop(bench, spec):
            raise CommandError(f"{spec.code} has no control loop configured")
        bench.set_pump_mode(False, output=spec.name)
        return CommandResult(
            True,
            f"{spec.code} {spec.description}: MANUAL, holding "
            f"{bench.pwm_percent(spec.name):.1f} %",
        )

    if arg.startswith("A"):
        if not _has_loop(bench, spec):
            raise CommandError(f"{spec.code} has no control loop configured")
        rest = arg[1:].strip()
        if rest in ("", "?"):
            state = bench.control_state()
            return CommandResult(
                True,
                f"{spec.code} {spec.description}: {state.mode_name}, "
                f"setpoint {state.setpoint_target:.1f}",
            )
        try:
            setpoint = float(rest)
        except ValueError:
            raise CommandError(
                f"{spec.code}A: expected a setpoint like {spec.code}A250, got {rest!r}"
            ) from None
        bench.set_pump_mode(True, setpoint, output=spec.name)
        return CommandResult(
            True, f"{spec.code} {spec.description}: AUTO, setpoint {setpoint:.1f}"
        )

    # A bare number means manual duty. Refuse it while the loop owns the output
    # rather than letting it apply and then be silently overwritten on the next
    # control tick.
    if _has_loop(bench, spec) and bench.control_state().mode != p.PumpMode.MANUAL:
        raise CommandError(
            f"{spec.code} is in AUTO. Use {spec.code}M for manual, "
            f"or {spec.code}A<setpoint> to change the setpoint."
        )

    relative = arg[0] in "+-"
    try:
        number = float(arg)
    except ValueError:
        raise CommandError(
            f"{spec.code}: expected a percentage like {spec.code}50 or {spec.code}+5, got {arg!r}"
        ) from None

    percent = bench.pwm_percent(spec.name) + number if relative else number
    if not 0.0 <= percent <= 100.0:
        raise CommandError(f"{spec.code}: {percent:g} % is outside 0-100 %")

    bench.set_pwm(spec.name, percent)
    return CommandResult(True, f"{spec.code} {spec.description}: {percent:.1f} %")


def _analog_command(bench: Bench, spec: AnalogInputSpec, arg: str) -> CommandResult:
    if arg not in ("", "?"):
        raise CommandError(f"{spec.code} is an input; {spec.code}? reads it")
    reading = bench.read_analog(spec.name)
    return CommandResult(
        True,
        f"{spec.code} {spec.description}: {reading.text}  "
        f"({reading.volts:.3f} V, raw {reading.raw})",
    )


# --------------------------------------------------------------------------
# Tuning and autotune
# --------------------------------------------------------------------------


def _gain_command(field: str):
    """Build a handler for one gain. `K?` shows them all."""

    def handler(bench: Bench, arg: str) -> CommandResult:
        if arg in ("", "?"):
            return _show_tuning(bench)
        try:
            value = float(arg)
        except ValueError:
            raise CommandError(f"K{field.upper()}: expected a number, got {arg!r}") from None
        cfg = bench.set_gains(**{field: value})
        return CommandResult(True, _tuning_text(cfg))

    return handler


def _tuning_text(cfg) -> str:
    return (
        f"gains: kp {cfg.kp:g}  ki {cfg.ki:g}  kd {cfg.kd:g}   "
        f"filter {cfg.filter_tau_s:g} s  deadband {cfg.deadband:g}"
    )


def _show_tuning(bench: Bench, _arg: str = "") -> CommandResult:
    if not bench.config.controls:
        raise CommandError("no control loop is configured")
    return CommandResult(True, _tuning_text(bench.control_cfg()))


def _filter_command(bench: Bench, arg: str) -> CommandResult:
    if arg in ("", "?"):
        return _show_tuning(bench)
    try:
        tau = float(arg)
    except ValueError:
        raise CommandError(f"KF: expected a time constant in seconds, got {arg!r}") from None
    if tau < 0.0:
        raise CommandError("KF: the filter time constant cannot be negative")
    from dataclasses import replace

    bench.set_control_cfg(replace(bench.control_cfg(), filter_tau_s=tau))
    return CommandResult(True, _tuning_text(bench.control_cfg()))


def _deadband_command(bench: Bench, arg: str) -> CommandResult:
    if arg in ("", "?"):
        return _show_tuning(bench)
    try:
        band = float(arg)
    except ValueError:
        raise CommandError(f"KB: expected a deadband in flow units, got {arg!r}") from None
    if band < 0.0:
        raise CommandError("KB: the deadband cannot be negative")
    from dataclasses import replace

    bench.set_control_cfg(replace(bench.control_cfg(), deadband=band))
    return CommandResult(True, _tuning_text(bench.control_cfg()))


def _autotune_command(bench: Bench, arg: str) -> CommandResult:
    """`T<setpoint>` starts, `T?` reports, `TX` aborts, `TA` adopts."""
    if not bench.config.controls:
        raise CommandError("no control loop is configured")

    if arg in ("", "?"):
        return CommandResult(True, _autotune_text(bench.autotune_result()))

    if arg in ("X", "STOP", "ABORT"):
        bench.abort_autotune()
        return CommandResult(True, "autotune aborted; pump back to manual")

    if arg in ("A", "ADOPT"):
        result = bench.adopt_autotune()
        return CommandResult(
            True,
            f"adopted kp {result.kp:g}  ki {result.ki:g}  kd {result.kd:g}"
            + ("" if result.trustworthy else "   (note: cycle spread was high)"),
        )

    try:
        setpoint = float(arg)
    except ValueError:
        raise CommandError(
            f"T: expected a setpoint to tune around like T250, got {arg!r}. "
            "T? reports, TX aborts, TA adopts the result."
        ) from None

    bench.start_autotune(setpoint)
    return CommandResult(
        True,
        f"autotune started around {setpoint:g} -- the pump will oscillate on purpose. "
        "T? for progress, TX to stop.",
    )


def _autotune_text(result) -> str:
    if result.state == p.AutotuneState.IDLE:
        return "autotune: never run"
    if result.running:
        return f"autotune: {result.state_name.lower()}, {result.cycles_done} cycles so far"
    if result.state == p.AutotuneState.FAILED:
        return f"autotune: FAILED ({result.fail_name.lower()})"

    trust = "" if result.trustworthy else f"   (period spread {result.spread * 100:.0f} %, "\
        "treat with suspicion)"
    return (
        f"autotune: done in {result.cycles_done} cycles. "
        f"Ku {result.ku:.4g}, Tu {result.tu:.3g} s, swing {result.amplitude:.1f} -> "
        f"kp {result.kp:g}  ki {result.ki:g}  kd {result.kd:g}. TA to adopt.{trust}"
    )


#: Prefix -> handler. Longest prefix wins, so KP beats K.
_LOOP_COMMANDS: dict[str, Callable[[Bench, str], CommandResult]] = {
    "KP": _gain_command("kp"),
    "KI": _gain_command("ki"),
    "KD": _gain_command("kd"),
    "KF": _filter_command,
    "KB": _deadband_command,
    "K": _show_tuning,
    "T": _autotune_command,
}


# --------------------------------------------------------------------------
# Global commands
# --------------------------------------------------------------------------


def _cmd_status(bench: Bench) -> CommandResult:
    return CommandResult(True, "", show_status=True)


def _cmd_valves_open(bench: Bench) -> CommandResult:
    if not bench.config.valves:
        raise CommandError("no valves are configured")
    names = bench.set_all_valves(True)
    return CommandResult(True, f"all valves OPEN ({len(names)})")


def _cmd_valves_close(bench: Bench) -> CommandResult:
    if not bench.config.valves:
        raise CommandError("no valves are configured")

    # Checked before closing, not after: if the answer changes the operator's
    # mind, the warning is only useful while the line is still open.
    running = bench.running_outputs()
    names = bench.set_all_valves(False)

    text = f"all valves CLOSED ({len(names)})"
    if running:
        text += f"  -- WARNING: {', '.join(running)} still running into a closed line"
    return CommandResult(True, text)


def _cmd_estop(bench: Bench) -> CommandResult:
    bench.estop()
    return CommandResult(
        True,
        "*** EMERGENCY STOP ENGAGED *** outputs safe, latched. EC to clear.",
    )


def _cmd_estop_clear(bench: Bench) -> CommandResult:
    bench.clear_estop()
    return CommandResult(
        True, "emergency stop cleared. Nothing moved -- outputs are still at their safe values."
    )


def _cmd_stop(bench: Bench) -> CommandResult:
    bench.stop()
    return CommandResult(True, "STOP: pump at default, valves closed")


def _cmd_init(bench: Bench) -> CommandResult:
    bench.initialize()
    return CommandResult(True, "bench re-initialised")


def _cmd_quit(bench: Bench) -> CommandResult:
    return CommandResult(True, "bye", should_quit=True)


def _cmd_help(bench: Bench) -> CommandResult:
    return CommandResult(True, help_text(bench))


_GLOBALS: dict[str, Callable[[Bench], CommandResult]] = {
    "valves_open": _cmd_valves_open,
    "valves_close": _cmd_valves_close,
    "estop": _cmd_estop,
    "estop_clear": _cmd_estop_clear,
    "status": _cmd_status,
    "stop": _cmd_stop,
    "init": _cmd_init,
    "quit": _cmd_quit,
    "help": _cmd_help,
}


def help_text(bench: Bench) -> str:
    """Help built from the live config, so it cannot drift from the hardware.

    Two columns, aligned to the widest syntax rather than to hand-counted
    spaces -- a longer code in a future config would otherwise ragged the
    whole block.
    """
    rows: list[tuple[str, str]] = []

    for valve in bench.config.valves:
        rows.append((
            f"{valve.code}O / {valve.code}C / {valve.code}T / {valve.code}?",
            f"open / close / toggle / query {valve.description} (pin {valve.pin})",
        ))
    if len(bench.config.valves) > 1:
        rows.append(("VO / VC", "open / close every valve at once"))

    for pwm in bench.config.pwms:
        rows.append((
            f"{pwm.code}<0-100> / {pwm.code}+<n> / {pwm.code}?",
            f"set / adjust / query {pwm.description} "
            f"(pin {pwm.pin}, {pwm.freq_hz / 1000:g} kHz)",
        ))
        if any(c.output == pwm.name for c in bench.config.controls):
            unit = next(
                (
                    a.unit
                    for c in bench.config.controls
                    if c.output == pwm.name
                    for a in bench.config.analogs
                    if a.name == c.input
                ),
                "units",
            )
            rows.append((
                f"{pwm.code}A<setpoint> / {pwm.code}M",
                f"closed-loop on the flow setpoint ({unit}) / back to manual",
            ))
    for analog in bench.config.analogs:
        rows.append((
            f"{analog.code}?",
            f"read {analog.description} (pin {analog.pin}, {analog.unit})",
        ))

    if bench.config.controls:
        rows += [
            ("K? / KP<n> / KI<n> / KD<n>", "show or set the loop gains"),
            ("KF<seconds> / KB<units>", "measurement filter / integration deadband"),
            ("T<setpoint>", "autotune around a setpoint (the pump will oscillate)"),
            ("T? / TA / TX", "autotune progress / adopt the gains / abort"),
        ]

    rows += [
        ("! or E", "EMERGENCY STOP -- everything safe, latched (Ctrl-E too)"),
        ("EC", "clear the emergency stop"),
        ("S", "device status"),
        ("X", "stop: pump to default, valves closed"),
        ("Z", "re-initialise the bench"),
        ("Q", "quit"),
    ]

    width = max(len(syntax) for syntax, _ in rows)
    return "\n".join(
        ["commands (chain with ';', e.g. V1O;P50;)"]
        + [f"  {syntax.ljust(width)}   {description}" for syntax, description in rows]
    )
