"""How the gateway talks to the person standing in front of it.

Everything here exists because of one screenshot: enrolment had succeeded, and the operator
could not tell what to do next. The fingerprint they were meant to compare was ninety-five
unbroken characters on a wrapped line, the instruction was the tail of a sentence, and an
`httpx` log line had landed in the middle of it all.

None of that is decoration. An operator who cannot find the next step does not take it, and
a fingerprint too tedious to compare is a fingerprint nobody compares — which is the one
check standing between a stolen enrolment code and someone else's database.

Rules: ASCII only, because this runs on consoles whose code page we do not choose. Colour
only when there is a terminal that asked for it. And no dependency, in a process a
customer's security team has to accept.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

WIDTH = 70

# Bold, dim, green, yellow. Nothing exotic: these four survive every terminal that supports
# colour at all, including a PowerShell window with a non-default palette.
_CODES = {
    "strong": "\x1b[1m",
    "dim": "\x1b[2m",
    "ok": "\x1b[32m",
    "warn": "\x1b[33m",
    "bad": "\x1b[31m",
}
_RESET = "\x1b[0m"

_colour: bool | None = None


def make_output_safe() -> None:
    """Stops an unencodable character from killing the process.

    A Windows console runs on whichever code page it inherited: cp1252 has no arrow, cp437
    has neither arrow nor em dash, and Python's default is to raise UnicodeEncodeError
    rather than approximate. A gateway that dies while printing a success message is a bad
    joke, and it happened here - the enrolment-code prompt had an arrow in it.

    Everything printed is ASCII for that reason, and a test keeps it that way. This is the
    net under the character that gets in anyway, on a code page nobody anticipated.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):  # pragma: no cover - exotic streams
            pass


def colour_enabled() -> bool:
    """Whether to emit escape codes, decided once and remembered.

    Three ways to say no, all of them honoured: `NO_COLOR` set to anything (the convention),
    `TERM=dumb`, or output that is not a terminal — a log file full of escape codes is worse
    than one without colour.
    """
    global _colour
    if _colour is None:
        _colour = _decide_colour()
    return _colour


def paint(text: str, style: str) -> str:
    if not colour_enabled():
        return text
    return f"{_CODES[style]}{text}{_RESET}"


def banner(title: str) -> None:
    print()
    print("=" * WIDTH)
    print(f" {paint(title, 'strong')}")
    print("=" * WIDTH)
    print()


def separator(title: str, *, file: TextIO | None = None) -> None:
    """A ruled heading for the one thing on the screen that must not be skimmed past."""
    stream = file or sys.stdout
    print(file=stream)
    print("-" * WIDTH, file=stream)
    print(f" {paint(title, 'strong')}", file=stream)
    print("-" * WIDTH, file=stream)
    print(file=stream)


def badge(text: str, style: str, first_line: str, *rest: str, file: TextIO | None = None) -> None:
    """A verdict in brackets, with its explanation hanging off it.

    `[ OK ]` and `[FAIL]` are the same width on purpose: the eye finds the result before it
    reads a word of the line, which is the whole point of putting it there.
    """
    stream = file or sys.stdout
    print(f"  {paint(f'[{text}]', style)}  {first_line}", file=stream)
    for line in rest:
        print(f"          {line}", file=stream)


def heading(text: str, *, file: TextIO | None = None) -> None:
    print(f"  {paint(text, 'strong')}", file=file or sys.stdout)


def field(label: str, value: object, *, width: int = 12, file: TextIO | None = None) -> None:
    """A label and a value, in columns. Labels dimmed so the values are what the eye lands on."""
    print(f"  {paint(label.ljust(width), 'dim')} {value}", file=file or sys.stdout)


def field_wrapped(label: str, text: str, *, width: int = 12, file: TextIO | None = None) -> None:
    """A field whose value is a sentence: wrapped, with continuations under the value.

    A long explanation in a single column runs past the edge of the terminal and wraps
    back to column zero, where it reads as a new line of output rather than the rest of
    this one.
    """
    import textwrap

    room = max(WIDTH - width - 3, 30)
    lines = textwrap.wrap(text, room) or [""]
    field(label, lines[0], width=width, file=file)
    for continuation in lines[1:]:
        detail(continuation, width=width, file=file)


def detail(text: str, *, width: int = 12, file: TextIO | None = None) -> None:
    """A continuation line, aligned under the values rather than under the labels."""
    print(f"  {' ' * width} {text}", file=file or sys.stdout)


def step(number: int, text: str) -> None:
    print(f"  {paint(str(number), 'strong')}  {text}")


def success(label: str, value: str, *, width: int = 12) -> None:
    print(f"  {paint(label.ljust(width), 'dim')} {paint(value, 'ok')}")


def warning(title: str, *lines: str, file: TextIO | None = None) -> None:
    """A warning that reads as one, rather than as more text.

    Indented under its own marked heading, with a blank line either side, so that an
    advisory about an over-privileged database account cannot be mistaken for part of the
    success it follows or the next thing it precedes.
    """
    stream = file or sys.stdout
    print(file=stream)
    print(f"  {paint('! ' + title, 'warn')}", file=stream)
    for line in lines:
        # An empty line stays empty: four spaces of indentation on a blank line is
        # invisible until it lands in a diff or a log, and then it is noise.
        print(f"    {line}" if line else "", file=stream)
    print(file=stream)


def fingerprint_lines(fingerprint: str, *, per_group: int = 8, groups_per_line: int = 2) -> list[str]:
    """Breaks a fingerprint into blocks a human can actually compare.

    Thirty-two colon-separated bytes on one line is 95 characters, which wraps on a normal
    terminal and has to be checked character by character against a web page. Four blocks of
    eight, two to a line, is the same information in the shape a card number is printed in,
    and for the same reason.
    """
    parts = [part for part in fingerprint.split(":") if part]
    groups = [":".join(parts[index : index + per_group]) for index in range(0, len(parts), per_group)]
    return [
        "   ".join(groups[index : index + groups_per_line])
        for index in range(0, len(groups), groups_per_line)
    ]


def fingerprint_block(fingerprint: str) -> None:
    for line in fingerprint_lines(fingerprint):
        print(f"    {paint(line, 'strong')}")


def _decide_colour() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False

    try:
        if not sys.stdout.isatty():
            return False
    except (AttributeError, ValueError):  # pragma: no cover - detached stdout
        return False

    if os.name == "nt":
        return _enable_windows_vt()
    return True


def _enable_windows_vt() -> bool:
    """Asks the Windows console to interpret escape codes, and reports whether it will.

    Windows Terminal has this on already; a classic conhost window does not, and writing
    escape codes to one prints them as text. Turning it on is a two-call dance through
    kernel32, and anything unexpected in it means "no colour" rather than an exception in a
    program whose actual job is elsewhere.
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False

        virtual_terminal = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if mode.value & virtual_terminal:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | virtual_terminal))
    except Exception:  # noqa: BLE001 - any trouble here means plain text, never a crash
        return False
