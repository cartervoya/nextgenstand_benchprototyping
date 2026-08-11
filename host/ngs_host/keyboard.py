"""Non-blocking keystroke reads, so one thread can poll hardware *and* accept
typed commands.

The alternative -- a reader thread around `input()` -- fights the live display
for the terminal and leaves a blocked thread behind at exit. Reading raw keys
means the dashboard draws its own input line and the whole UI stays in one
loop, which is much easier to reason about when something hangs.

Windows uses msvcrt; POSIX puts the tty in cbreak mode for the duration.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

#: What `read()` returns for the keys the UI cares about.
ENTER = "\n"
BACKSPACE = "\b"
CTRL_C = "\x03"
CTRL_D = "\x04"
#: Emergency stop. A chord rather than a plain letter so it cannot be
#: triggered by typing, and it needs no Enter -- an E-stop you have to
#: finish typing is not an E-stop.
CTRL_E = "\x05"


def stdin_is_interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, AttributeError):  # closed or replaced stdin
        return False


if sys.platform == "win32":

    @contextmanager
    def raw_mode() -> Iterator[None]:
        # The Windows console needs no mode change: msvcrt reads the input
        # buffer directly and does not echo.
        yield

    def read_keys() -> str:
        import msvcrt

        out: list[str] = []
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                msvcrt.getwch()  # discard the second half of a function key
                continue
            if ch == "\r":
                ch = ENTER
            out.append(ch)
        return "".join(out)

else:

    @contextmanager
    def raw_mode() -> Iterator[None]:
        import termios
        import tty

        if not stdin_is_interactive():
            yield
            return

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            # cbreak, not raw: signals keep working, so Ctrl-C still aborts
            # even if the UI loop is wedged.
            tty.setcbreak(fd)
            yield
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def read_keys() -> str:
        import select

        out: list[str] = []
        while select.select([sys.stdin], [], [], 0)[0]:
            ch = sys.stdin.read(1)
            if not ch:
                break
            if ch == "\r":
                ch = ENTER
            if ch == "\x7f":
                ch = BACKSPACE
            out.append(ch)
        return "".join(out)


class EmergencyStop(Exception):  # noqa: N818 -- not an error, a signal
    """Ctrl-E was pressed. Raised out of the editor so the caller acts on it
    immediately, without waiting for a line to be completed."""


class LineEditor:
    """A one-line input buffer fed by `read_keys()`.

    Keeps a history so arrow-free recall (Ctrl-P style is overkill here) is at
    least possible from the caller, and returns completed lines.
    """

    def __init__(self) -> None:
        self.buffer = ""
        self.history: list[str] = []

    def feed(self, keys: str) -> list[str]:
        """Consume keystrokes; return every line completed by an Enter.

        Raises KeyboardInterrupt on Ctrl-C so the caller's normal exit path
        handles it -- the UI has hardware to put back in a safe state, and
        that must not be skipped. Ctrl-E raises EmergencyStop, which the
        caller acts on at once rather than at the end of a line.
        """
        lines: list[str] = []
        for ch in keys:
            if ch == CTRL_E:
                raise EmergencyStop
            if ch in (CTRL_C, CTRL_D):
                raise KeyboardInterrupt
            if ch == ENTER:
                line = self.buffer.strip()
                self.buffer = ""
                if line:
                    self.history.append(line)
                    lines.append(line)
            elif ch in (BACKSPACE, "\x7f"):
                self.buffer = self.buffer[:-1]
            elif ch == "\x1b":  # a bare escape clears the line
                self.buffer = ""
            elif ch.isprintable():
                self.buffer += ch
        return lines
