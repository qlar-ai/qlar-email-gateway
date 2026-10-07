"""Typing a password and seeing something happen.

`getpass` echoes nothing, and an operator meeting this prompt once — pasting a database
password into a tool they installed five minutes ago — cannot tell a silent prompt from a
hung one. They type it twice, or they Ctrl-C. Both were reported.

The loop is tested here without a terminal: it is handed a function that returns the next
character and one that writes, which is also how the platform differences stay in one place.
"""

from __future__ import annotations

import pytest

from qlar_email_gateway.masked_input import read_masked


class Keyboard:
    """A scripted terminal: characters go in, what the screen shows comes out."""

    def __init__(self, *keys: str) -> None:
        self.keys = list("".join(keys))
        self.screen: list[str] = []

    def read(self) -> str:
        return self.keys.pop(0) if self.keys else ""

    def write(self, text: str) -> None:
        self.screen.append(text)

    @property
    def shown(self) -> str:
        """What is left on screen, as a terminal would render it.

        A cursor, not a stack: `\\b` moves left without erasing, and the space in `"\\b \\b"`
        is what actually overwrites the character. Modelling backspace as "delete" instead
        makes the sequence erase twice, which is a bug in the test rather than the code —
        it cost me one, so it is written out here.
        """
        line: list[str] = []
        cursor = 0
        for character in "".join(self.screen):
            if character == "\b":
                cursor = max(0, cursor - 1)
            elif character == "\n":
                line.append("\n")
                cursor = len(line)
            elif cursor < len(line):
                line[cursor] = character
                cursor += 1
            else:
                line.append(character)
                cursor += 1
        return "".join(line).rstrip(" ")


class TestTyping:
    def test_the_password_comes_back_and_the_screen_shows_stars(self):
        keyboard = Keyboard("s3cret\r")

        typed = read_masked("Password: ", keyboard.read, keyboard.write)

        assert typed == "s3cret"
        assert keyboard.shown == "Password: ******\n"

    def test_spaces_and_punctuation_are_passed_through(self):
        keyboard = Keyboard("@UFp57 nW1q2%Hbzc\n")

        assert read_masked("", keyboard.read, keyboard.write) == "@UFp57 nW1q2%Hbzc"

    def test_an_empty_password_is_allowed_through_to_the_caller(self):
        # The wizard decides whether empty means "keep the old one" or "ask again"; the
        # reader's job is only to report what was typed.
        keyboard = Keyboard("\r")

        assert read_masked("", keyboard.read, keyboard.write) == ""

    def test_non_ascii_is_not_mangled(self):
        keyboard = Keyboard("café\r")

        assert read_masked("", keyboard.read, keyboard.write) == "café"


class TestCorrections:
    @pytest.mark.parametrize("backspace", ["\b", "\x7f"])
    def test_backspace_removes_a_character_and_its_mask(self, backspace):
        keyboard = Keyboard(f"abc{backspace}d\r")

        typed = read_masked("", keyboard.read, keyboard.write)

        assert typed == "abd"
        assert keyboard.shown == "***\n"

    def test_backspace_on_an_empty_prompt_does_nothing(self):
        keyboard = Keyboard("\b\b\bx\r")

        assert read_masked("", keyboard.read, keyboard.write) == "x"
        assert keyboard.shown == "*\n"


class TestKeysThatAreNotCharacters:
    def test_an_arrow_key_on_windows_is_swallowed_whole(self):
        # Windows sends \xe0 then the key code; masking the second half would put a star on
        # screen for a keypress that typed nothing, and add it to the password.
        keyboard = Keyboard("ab\xe0Hc\r")

        typed = read_masked("", keyboard.read, keyboard.write)

        assert typed == "abc"
        assert keyboard.shown == "***\n"

    def test_other_control_characters_are_ignored(self):
        keyboard = Keyboard("a\tb\x1bc\r")

        assert read_masked("", keyboard.read, keyboard.write) == "abc"


class TestCancelling:
    def test_ctrl_c_raises_the_same_thing_input_would(self):
        keyboard = Keyboard("half\x03")

        with pytest.raises(KeyboardInterrupt):
            read_masked("", keyboard.read, keyboard.write)

    def test_ctrl_d_at_an_empty_prompt_is_end_of_input(self):
        keyboard = Keyboard("\x04")

        with pytest.raises(EOFError):
            read_masked("", keyboard.read, keyboard.write)

    def test_ctrl_d_after_typing_is_not(self):
        # Mid-password it is just another control character; ending input there would
        # discard what was typed with no way to tell that is what happened.
        keyboard = Keyboard("abc\x04d\r")

        assert read_masked("", keyboard.read, keyboard.write) == "abcd"

    def test_a_closed_terminal_is_end_of_input(self):
        keyboard = Keyboard("")

        with pytest.raises(EOFError):
            read_masked("", keyboard.read, keyboard.write)


class TestChoosingHowToRead:
    """The glue: which reader is used, and what happens when none can be.

    The character loop above is platform-independent and tested directly. What cannot be
    exercised without a real console is `msvcrt.getwch` itself, so the wiring around it is
    tested instead: that a reader is used when there is one, and that anything at all going
    wrong falls back to an echo-free prompt rather than leaving the operator unable to type
    a password.
    """

    def test_it_uses_the_reader_it_was_given(self, monkeypatch):
        from contextlib import contextmanager

        from qlar_email_gateway import masked_input

        keys = list("hunter2\r")

        @contextmanager
        def fake_reader():
            yield lambda: keys.pop(0)

        monkeypatch.setattr(masked_input, "_reader_for_this_terminal", fake_reader)

        assert masked_input.prompt_for_secret("Password: ") == "hunter2"

    def test_no_reader_means_the_ordinary_echo_free_prompt(self, monkeypatch):
        from qlar_email_gateway import masked_input

        monkeypatch.setattr(masked_input, "_reader_for_this_terminal", lambda: None)
        monkeypatch.setattr(masked_input.getpass, "getpass", lambda prompt: "from getpass")

        assert masked_input.prompt_for_secret("Password: ") == "from getpass"

    def test_a_terminal_that_cannot_be_driven_falls_back_rather_than_failing(self, monkeypatch):
        from qlar_email_gateway import masked_input

        def explode():
            raise OSError("this terminal does not do that")

        monkeypatch.setattr(masked_input, "_reader_for_this_terminal", explode)
        monkeypatch.setattr(masked_input.getpass, "getpass", lambda prompt: "from getpass")

        assert masked_input.prompt_for_secret("Password: ") == "from getpass"

    def test_cancelling_is_never_swallowed_by_the_fallback(self, monkeypatch):
        from contextlib import contextmanager

        from qlar_email_gateway import masked_input

        @contextmanager
        def interrupting_reader():
            def read():
                raise KeyboardInterrupt

            yield read

        monkeypatch.setattr(masked_input, "_reader_for_this_terminal", interrupting_reader)
        monkeypatch.setattr(masked_input.getpass, "getpass", lambda prompt: "must not be reached")

        with pytest.raises(KeyboardInterrupt):
            masked_input.prompt_for_secret("Password: ")


class TestEverythingPrintedIsAscii:
    """A console picks its own code page, and we do not get a say.

    cp1252 has no arrow; cp437 has no em dash either. Python's answer to a character it
    cannot encode is to raise, so a decorative dash in a success message is a crash on
    someone else's machine - which is exactly how `enroll` died on a Windows console while
    printing the line telling the operator where to find their enrolment code.

    So: message strings are ASCII. This test is the thing that keeps them that way, because
    the character that breaks it will arrive in a commit that looks harmless.
    """

    def test_no_message_string_needs_more_than_ascii(self):
        import ast
        from pathlib import Path

        import qlar_email_gateway

        package = Path(qlar_email_gateway.__file__).parent
        offenders: list[str] = []

        for source in sorted(package.rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            docstrings = {
                doc
                for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and (doc := ast.get_docstring(node, clean=False))
            }
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                # Docstrings are never encoded to a terminal, and a single character is a
                # key code or a delimiter rather than something anyone reads.
                if node.value in docstrings or len(node.value) < 2:
                    continue
                if not node.value.isascii():
                    beyond = [hex(ord(c)) for c in node.value if not c.isascii()]
                    offenders.append(f"{source.name}:{node.lineno} {beyond} {node.value[:50]!r}")

        assert not offenders, "non-ASCII in strings that may be printed:\n" + "\n".join(offenders)

    def test_the_guard_survives_a_stream_that_cannot_be_reconfigured(self, monkeypatch):
        import io

        from qlar_email_gateway import console

        monkeypatch.setattr(console.sys, "stdout", io.StringIO())
        console.make_output_safe()  # a StringIO has no reconfigure; this must not raise
