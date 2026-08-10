"""The bench command language.

    V1O;        open valve 1              P100;   pump to 100 %
    V1C;        close valve 1             P37.5;  pump to 37.5 %
    V1T;        toggle valve 1            P+5;    pump 5 % faster
    V1?;        query valve 1             P?;     query the pump setpoint
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

from .bench import AnalogInputSpec, Bench, PwmOutputSpec, ValveSpec
from .device import Timeout
from .protocol import NgsError

#: Commands that act on the bench as a whole rather than one channel.
GLOBAL_ALIASES: dict[str, str] = {
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


def _pwm_command(bench: Bench, spec: PwmOutputSpec, arg: str) -> CommandResult:
    if arg in ("", "?"):
        return CommandResult(
            True, f"{spec.code} {spec.description}: {bench.pwm_percent(spec.name):.1f} %"
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
# Global commands
# --------------------------------------------------------------------------


def _cmd_status(bench: Bench) -> CommandResult:
    return CommandResult(True, "", show_status=True)


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
    for pwm in bench.config.pwms:
        rows.append((
            f"{pwm.code}<0-100> / {pwm.code}+<n> / {pwm.code}?",
            f"set / adjust / query {pwm.description} "
            f"(pin {pwm.pin}, {pwm.freq_hz / 1000:g} kHz)",
        ))
    for analog in bench.config.analogs:
        rows.append((
            f"{analog.code}?",
            f"read {analog.description} (pin {analog.pin}, {analog.unit})",
        ))

    rows += [
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
