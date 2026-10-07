"""Reading a password with one `*` per character, instead of nothing at all.

`getpass` echoes nothing, which is the right default for a login prompt someone types every
day. It is the wrong default here: this prompt is met once, by an operator pasting a
database password into an unfamiliar tool, and a terminal that shows no reaction at all is
indistinguishable from one that has stopped listening. The first thing people do is type it
twice.

So the characters are counted on screen but not shown. That does reveal the length of the
password to anyone looking at the screen, which `getpass` does not — a deliberate trade, and
the smaller risk of the two when the alternative is an operator who cannot tell whether
their keystrokes arrived.

The loop is separated from the platform so it can be tested without a terminal: `read_masked`
takes a function that returns one character and a function that writes one, and every
platform difference lives in `_reader_for_this_terminal`.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager

MASK = "*"

# What a terminal sends for Enter, an interrupt, and the two backspaces.
ENTER = ("\r", "\n")
INTERRUPT = "\x03"
BACKSPACES = ("\b", "\x7f")
END_OF_TRANSMISSION = "\x04"

# Windows sends these as the first half of an arrow or function key; the second half is the
# key itself and means nothing here.
WINDOWS_KEY_PREFIXES = ("\x00", "\xe0")


def read_masked(
    prompt: str,
    read_char: Callable[[], str],
    write: Callable[[str], None],
) -> str:
    """Reads characters until Enter, echoing a mask for each one.

    Raises KeyboardInterrupt on Ctrl-C and EOFError on Ctrl-D at an empty prompt, so that a
    caller can treat cancellation exactly as it would from `input()` or `getpass`.
    """
    write(prompt)
    typed: list[str] = []

    while True:
        char = read_char()

        if char == "" or (char == END_OF_TRANSMISSION and not typed):
            write("\n")
            raise EOFError
        if char in ENTER:
            write("\n")
            return "".join(typed)
        if char == INTERRUPT:
            write("\n")
            raise KeyboardInterrupt
        if char in BACKSPACES:
            if typed:
                typed.pop()
                # Move back over the mask, overwrite it with a space, move back again.
                write("\b \b")
            continue
        if char in WINDOWS_KEY_PREFIXES:
            read_char()  # discard the key code that follows
            continue
        if char < " ":
            continue  # every other control character: ignored rather than masked

        typed.append(char)
        write(MASK)


def prompt_for_secret(prompt: str) -> str:
    """Asks for a secret at the terminal, falling back to `getpass` when masking cannot work.

    The fallback matters more than the feature: a terminal this cannot drive character by
    character must still be able to take a password, so anything unexpected here goes back
    to the standard, echo-free prompt rather than failing.
    """
    try:
        reader = _reader_for_this_terminal()
    except Exception:  # noqa: BLE001 - any terminal trouble means fall back, never fail
        return getpass.getpass(prompt)

    if reader is None:
        return getpass.getpass(prompt)

    try:
        with reader as read_char:
            return read_masked(prompt, read_char, _write)
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:  # noqa: BLE001 - as above
        return getpass.getpass(prompt)


def _write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _reader_for_this_terminal():  # noqa: ANN202 - a context manager or None
    """Picks the way to read one character here, or None when there is no way."""
    if not (sys.stdin is not None and sys.stdin.isatty()):
        return None

    try:
        import msvcrt
    except ImportError:
        pass
    else:
        return _windows_reader(msvcrt)

    try:
        import termios
    except ImportError:  # pragma: no cover - neither Windows nor POSIX
        return None

    return _posix_reader(termios)


@contextmanager
def _windows_reader(msvcrt) -> Iterator[Callable[[], str]]:  # noqa: ANN001
    # The console is already unbuffered and unechoed for getwch; nothing to set up or undo.
    yield msvcrt.getwch


@contextmanager
def _posix_reader(termios) -> Iterator[Callable[[], str]]:  # noqa: ANN001
    descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(descriptor)

    changed = list(original)
    # Off: canonical mode, so a character arrives without waiting for Enter; and echo, so
    # the terminal does not print it before we decide what to show instead.
    changed[3] &= ~(termios.ICANON | termios.ECHO)

    termios.tcsetattr(descriptor, termios.TCSANOW, changed)
    try:
        yield lambda: sys.stdin.read(1)
    finally:
        # TCSADRAIN, not TCSANOW: let the mask characters reach the screen before the
        # terminal goes back to echoing, or the next prompt lands in the middle of them.
        termios.tcsetattr(descriptor, termios.TCSADRAIN, original)
