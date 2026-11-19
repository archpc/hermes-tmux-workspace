"""Unit tests for hermes_tmux.transcript — the readable live view of one log.

Formatter is fed lines and returns display lines; no terminal, no clock, no
tmux. ``run_tail(..., follow=False)`` is exercised against a temporary file.

The invariant every case here serves is *one event, one terminal line*: nothing
is dropped except the fixed file header and three duplicates the writer itself
produces (a ``think`` that previews the ``assistant`` line after it, the
``start`` line that restates a kickoff goal, and the ``end`` line whose exit
reason merely repeats the status already printed). Tests that assert a line is
*absent* are the ones defending that rule; tests that assert a malformed line
*survives* defend the other half of it — a foreign line is evidence.

Two groups, with different isolation:

* ``Formatter`` tests are pure. They feed writer-format strings and inspect the
  returned display lines; width, colour and ASCII mode are constructor
  arguments, so nothing depends on the environment.
* ``run_tail`` tests write a real file under ``SCRATCH`` and call it with
  ``follow=False``; two tests spawn ``python -m hermes_tmux tail`` as a
  subprocess because the rotation and busy-loop defects only appear in the
  follower loop, and both are timing-sensitive (sleeps of 1.3 s / 1.5 s / 2.0 s
  against a 1 s poll). They are slower than the rest of the file and the CPU
  bound in ``test_unreadable_file_does_not_busy_loop`` is a 0.5 core average,
  not a precise measurement.

``SCRATCH`` is hardcoded to ``/Users/sein/.hermes/cache/scratch/hxlab`` on this
branch (the ``TMPDIR``-portable version lives on another branch). Both ``setUp``
methods ``makedirs`` that root and put every fixture inside it, so the run
ignores ``TMPDIR`` and fails with ``PermissionError`` on any account that cannot
create ``/Users/sein``.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import tempfile
import unittest

from hermes_tmux import transcript
from hermes_tmux.transcript import Formatter, run_tail

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
#: Fixed fixture root on this branch; see the module docstring. Every temporary
#: transcript directory is created inside it.
SCRATCH = "/Users/sein/.hermes/cache/scratch/hxlab"

# The six lines the writer puts at the top of every transcript, verbatim. The
# formatter suppresses them because the pane header and the kickoff event
# already state the same facts; the tests feed them through the real matcher
# rather than a shortened stand-in.
HEADER_LINES = [
    "=== Hermes subagent live transcript ===",
    "delegation: deleg_fixt0001   task: 0",
    "goal: Implement the tmux state reader and unit tests.",
    "started: 2026-11-12 09:00:00",
    "(append-only; streams while the subagent runs \u2014 tail -f me)",
    "========================================",
]


def visible(text):
    """The characters a pane shows: SGR sequences removed."""
    return ANSI_RE.sub("", text)


def feed(stream, *lines):
    """Flush every displayed line of ``stream`` into one list.

    Blank lines are part of the output contract (they separate what the worker
    said), so the helper keeps them rather than filtering.
    """
    out = []
    for line in lines:
        out.extend(stream.feed(line))
    return out


class TestHeader(unittest.TestCase):
    """Header suppression, and the gate that stops it applying too late.

    The header is skipped only until the first real event. After that, a line
    that looks like a header is a quote inside the transcript or a second file
    concatenated onto this one, and both must be shown.
    """

    def test_header_lines_are_skipped_before_the_first_event(self):
        fmt = Formatter(width=100)
        for line in HEADER_LINES:
            self.assertEqual(fmt.feed(line), [], line)

    def test_first_event_renders_after_the_header(self):
        fmt = Formatter(width=100)
        for line in HEADER_LINES:
            fmt.feed(line)
        # A ``start`` line becomes the TASK row plus the blank that follows it.
        self.assertEqual(fmt.feed("09:00:00  start  | Do the thing"),
                         ["09:00:00  TASK  Do the thing", ""])

    def test_header_looking_line_after_an_event_is_shown_verbatim(self):
        fmt = Formatter(width=100)
        fmt.feed("09:00:01  assistant  | hello")
        self.assertEqual(fmt.feed("=== Hermes subagent live transcript ==="),
                         ["=== Hermes subagent live transcript ==="])
        self.assertEqual(fmt.feed("delegation: deleg_x   task: 1"),
                         ["delegation: deleg_x   task: 1"])


class TestForeignLines(unittest.TestCase):
    """Lines that are not events, or not roles we know.

    These are the omission tests: a truncated write, a rotation warning or a
    role added after this code was written must reach the reader, dimmed and
    intact. Dropping one would make the transcript disagree with the file.
    """

    def test_malformed_line_after_events_is_never_dropped(self):
        fmt = Formatter(width=100)
        fmt.feed("09:00:01  assistant  | hello")
        out = fmt.feed("THIS IS NOT AN EVENT")
        self.assertEqual(len(out), 1)
        self.assertIn("THIS IS NOT AN EVENT", out[0])

    def test_malformed_line_before_events_is_shown_too(self):
        # Not the header, not an event: shown before any event has been seen.
        fmt = Formatter(width=100)
        out = fmt.feed("WARNING: log was rotated")
        self.assertIn("WARNING: log was rotated", "\n".join(out))

    def test_blank_lines_between_events_are_not_lines(self):
        # Blank input produces nothing: the formatter adds its own separators,
        # so echoing the file's blank lines would double the spacing.
        fmt = Formatter(width=100)
        fmt.feed("09:00:01  assistant  | hello")
        self.assertEqual(fmt.feed("   "), [])
        self.assertEqual(fmt.feed(""), [])

    def test_unknown_role_keeps_its_name(self):
        # The parse anchors on the timestamp, so a role this module has never
        # seen still renders under its own name instead of being swallowed.
        fmt = Formatter(width=100)
        out = fmt.feed("09:00:09  warning  | careful")
        self.assertEqual(len(out), 1)
        self.assertIn("warning", out[0])
        self.assertIn("careful", out[0])


class TestThinkCollapse(unittest.TestCase):
    """The ``think``/``assistant`` duplicate, and the line of lookahead it needs.

    A ``think`` is held until the next line shows whether it was a preview of
    the assistant text that followed. Held state is why these are the only
    formatter tests where input order and ``flush()`` matter.
    """

    def test_think_that_previews_the_assistant_line_is_collapsed(self):
        # The held think is dropped, so the output starts at the assistant
        # block: blank, text, blank. Nothing is left to flush.
        fmt = Formatter(width=100)
        self.assertEqual(fmt.feed("09:00:02  think  | I'll read the layout first."), [])
        out = fmt.feed("09:00:03  assistant  | I'll read the layout first. Then the tests.")
        self.assertEqual(out, ["", "09:00:03  I'll read the layout first. Then the tests.", ""])
        self.assertEqual(fmt.flush(), [])

    def test_think_followed_by_something_else_is_emitted_first(self):
        # Not a preview, so the held line is released *before* the new one, in
        # the order the writer produced them.
        fmt = Formatter(width=100)
        fmt.feed("09:00:02  think  | I should look at the tests")
        out = fmt.feed("09:00:03  tool  | -> read_file(tests/test_render.py)")
        self.assertEqual(len(out), 2)
        self.assertIn("I should look at the tests", out[0])
        self.assertIn("read_file", out[1])

    def test_think_at_end_of_file_is_flushed(self):
        # A held line with nothing after it must still be shown; ``flush`` is
        # idempotent because the follower calls it on every quiet poll.
        fmt = Formatter(width=100)
        self.assertEqual(fmt.feed("09:00:02  think  | still thinking"), [])
        out = fmt.flush()
        self.assertEqual(len(out), 1)
        self.assertIn("still thinking", out[0])
        self.assertEqual(fmt.flush(), [])

    def test_think_followed_by_a_shorter_assistant_text_is_kept(self):
        # Only a *prefix* relationship suppresses the think. Unrelated text
        # keeps both lines (held think, then the assistant block's three).
        fmt = Formatter(width=100)
        fmt.feed("09:00:02  think  | a completely different opening")
        out = fmt.feed("09:00:03  assistant  | something else entirely")
        # The held think, then the assistant block (blank, text, blank).
        self.assertEqual(len(out), 4)
        self.assertIn("a completely different opening", out[0])
        self.assertIn("something else entirely", out[2])


class TestKickoff(unittest.TestCase):
    """The kickoff event: TASK line, optional context line, blank separator."""

    def test_kickoff_renders_task_and_context(self):
        # Three lines: the goal is already the TASK row, so the second line
        # carries only ``context:`` rather than repeating it.
        fmt = Formatter(width=100)
        out = fmt.feed("09:00:00  user  | kickoff: Build the thing | context: branch feat/x")
        self.assertEqual(len(out), 3)
        self.assertIn("TASK", out[0])
        self.assertIn("Build the thing", out[0])
        self.assertIn("context: branch feat/x", out[1])
        self.assertEqual(out[2], "")

    def test_start_line_after_kickoff_is_suppressed(self):
        # ``_goal_shown`` is set by the kickoff and never reset within one
        # file: the writer's ``start`` line is the same goal, verbatim.
        fmt = Formatter(width=100)
        fmt.feed("09:00:00  user  | kickoff: Build the thing")
        self.assertEqual(fmt.feed("09:00:00  start  | Build the thing"), [])

    def test_kickoff_without_context_has_two_lines(self):
        fmt = Formatter(width=100)
        out = fmt.feed("09:00:00  user  | kickoff: Build the thing")
        self.assertEqual(len(out), 2)
        self.assertIn("Build the thing", out[0])
        self.assertEqual(out[1], "")


class TestToolAndResult(unittest.TestCase):
    """Tool calls and results: one fitted line each, clipped visibly.

    The clip marker counts *characters* hidden, not columns: it is a "how much
    text you are not seeing" budget, and the column fitting happens separately
    in ``_fit``. Two columns are held back from the pane width (``_MARGIN``) so
    a pane created wider than the client that draws it cannot wrap one
    character onto its own line.
    """

    def test_tool_call_is_one_line_with_arrow_stripped(self):
        fmt = Formatter(width=100)
        out = fmt.feed("09:00:04  tool  | -> read_file(path=a.py)")
        self.assertEqual(len(out), 1)
        self.assertIn("read_file(path=a.py)", out[0])
        self.assertNotIn("->", out[0])

    def test_tool_call_is_fitted_to_the_width(self):
        # The pane width minus the two-column margin is the hard bound here.
        for width in (60, 80, 100):
            fmt = Formatter(width=width)
            line = fmt.feed("09:00:04  tool  | -> execute_code(" + "x" * 300 + ")")[0]
            self.assertLessEqual(len(visible(line)), width - 2,
                                 "width=%d line=%d" % (width, len(visible(line))))
            self.assertIn("(+", line)
            self.assertIn("chars)", line)

    def test_tool_call_clipping_counts_what_is_hidden(self):
        # The marker must end the line and carry a positive count: a marker of
        # "0 chars" would claim a clip that did not happen.
        fmt = Formatter(width=60)
        args = "x" * 300
        line = visible(fmt.feed("09:00:04  tool  | -> execute_code(" + args + ")")[0])
        match = re.search(r"\(\+(\d+) chars\)$", line)
        self.assertIsNotNone(match, line)
        if match is not None:  # narrowing for readers of the assertion above
            self.assertGreater(int(match.group(1)), 0)

    def test_short_tool_call_is_not_marked(self):
        fmt = Formatter(width=100)
        line = fmt.feed("09:00:04  tool  | -> terminal(ls)")[0]
        self.assertNotIn("chars)", line)

    def test_ok_result_is_one_fitted_line(self):
        for width in (60, 100):
            fmt = Formatter(width=width)
            out = fmt.feed("09:00:05  result  | read_file ok 0.0s: {\"content\": \"\", \"total_lines\": 0}")
            self.assertEqual(len(out), 1, out)
            self.assertLessEqual(len(visible(out[0])), width - 2)
            self.assertIn("ok", out[0])

    def test_long_ok_result_is_clipped_with_a_marker(self):
        fmt = Formatter(width=60)
        out = fmt.feed("09:00:05  result  | terminal ok 0.4s: " + "y" * 400)
        self.assertEqual(len(out), 1)
        self.assertIn("(+", out[0])
        self.assertLessEqual(len(visible(out[0])), 58)

    def test_error_result_shows_the_message(self):
        # The payload's ``output`` field is the message when no named ``error``
        # exists; the JSON framing around it is dropped.
        fmt = Formatter(width=100)
        out = fmt.feed('09:00:21  result  | terminal ERROR 5.0s: '
                       '{"output": "FAILED (errors=1)", "exit_code": 1}')
        self.assertEqual(len(out), 1)
        self.assertIn("ERROR", out[0])
        self.assertIn("FAILED (errors=1)", out[0])

    def test_named_error_field_is_what_is_shown(self):
        # ``error`` wins over ``output``; the JSON key itself must not leak.
        fmt = Formatter(width=100)
        out = fmt.feed('09:00:21  result  | tool ERROR 0.4s: '
                       '{"output": "", "exit_code": 1, "error": "permission denied"}')
        self.assertIn("permission denied", out[0])
        self.assertNotIn('"error"', out[0])

    def test_error_result_is_not_fitted_to_one_line(self):
        # Deliberate asymmetry: an ERROR is printed in full and left to the
        # terminal to wrap, so the message is never truncated and never grows a
        # chars marker. The test asserts both halves.
        fmt = Formatter(width=40)
        out = fmt.feed('09:00:21  result  | tool ERROR 0.4s: '
                       '{"output": "", "error": "' + "e" * 300 + '"}')
        self.assertEqual(len(out), 1)
        self.assertGreater(len(visible(out[0])), 40)
        self.assertNotIn("chars)", out[0])

    def test_unparsable_result_still_renders_one_line(self):
        fmt = Formatter(width=100)
        out = fmt.feed("09:00:04  result  | weird thing happened here")
        self.assertEqual(len(out), 1)
        self.assertIn("weird thing happened here", out[0])


class TestFinal(unittest.TestCase):
    """The run's last lines: ``final status=… duration=…`` and ``end … exit_reason=…``.

    ``end`` repeats the status ``final`` already printed, so it renders only
    when the reason is news. Both forms can appear alone: a follower started
    mid-file sees only the ``end`` line.
    """

    def test_final_line_shows_status_and_duration(self):
        # A clean finish is separated from the transcript by one blank line and
        # reports the duration in the humanised form, not raw seconds.
        fmt = Formatter(width=100)
        out = fmt.feed("09:02:14  final  | status=completed duration=134s summary: all good")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], "")
        self.assertIn("completed", out[1])
        self.assertIn("2m14s", out[1])

    def test_failed_final_line_is_a_failure(self):
        fmt = Formatter(width=100)
        out = fmt.feed("09:00:09  final  | status=failed duration=9s")
        self.assertIn("failed", out[1])

    def test_end_line_repeats_nothing_when_the_reason_matches(self):
        fmt = Formatter(width=100)
        fmt.feed("09:02:14  final  | status=completed duration=134s")
        # reason == status, so the closing line adds nothing.
        self.assertEqual(fmt.feed("09:02:14  final  | end status=completed exit_reason=completed"),
                         [])

    def test_end_line_shows_a_different_exit_reason(self):
        fmt = Formatter(width=100)
        fmt.feed("09:02:14  final  | status=completed duration=134s")
        out = fmt.feed("09:02:14  final  | end status=completed "
                       "exit_reason=max_iterations duration=134s")
        self.assertEqual(len(out), 1)
        self.assertIn("exit: max_iterations", out[0])

    def test_end_line_alone_shows_the_reason(self):
        # The comparison is against the status in the same line, not against a
        # remembered one, so this works without a preceding ``final``.
        fmt = Formatter(width=100)
        out = fmt.feed("09:02:14  final  | end status=failed exit_reason=timeout")
        self.assertEqual(len(out), 1)
        self.assertIn("exit: timeout", out[0])


class TestStyle(unittest.TestCase):
    """Colour and ASCII: same characters, different presentation.

    Colour on must be the plain text with SGR inserted, never a different
    layout — the pane and a pipe must agree. ASCII mode replaces every glyph
    and every ellipsis, because a non-UTF-8 locale turns them into mojibake.
    """

    def test_colour_off_has_no_escape_byte(self):
        fmt = Formatter(width=100, colour=False)
        out = feed(fmt, "09:00:00  user  | kickoff: Build it",
                   "09:00:01  think  | hmm",
                   "09:00:02  assistant  | text",
                   "09:00:03  tool  | -> read_file(a)",
                   "09:00:04  result  | read_file ok 0.1s: x",
                   "09:00:05  result  | read_file ERROR 0.1s: boom",
                   "09:00:06  final  | status=completed duration=9s")
        for line in out:
            self.assertNotIn("\x1b", line)

    def test_colour_is_the_same_text_with_sgr(self):
        lines = ["09:00:00  user  | kickoff: Build it",
                 "09:00:02  assistant  | text",
                 "09:00:03  tool  | -> read_file(a)",
                 "09:00:04  result  | read_file ok 0.1s: x",
                 "09:00:05  result  | read_file ERROR 0.1s: boom",
                 "09:00:06  final  | status=completed duration=9s"]
        plain = feed(Formatter(width=100, colour=False), *lines)
        coloured = feed(Formatter(width=100, colour=True), *lines)
        self.assertTrue(any("\x1b" in line for line in coloured))
        self.assertEqual([visible(line) for line in coloured], plain)

    def test_ascii_mode_uses_ascii_markers(self):
        # Index-based assertions only where the marker is the whole claim: the
        # bullet becomes ">", the error mark becomes "x".
        fmt = Formatter(width=100, colour=False, ascii_mode=True)
        out = feed(fmt, "09:00:03  tool  | -> read_file(a)",
                   "09:00:04  result  | read_file ok 0.1s: x",
                   "09:00:05  result  | read_file ERROR 0.1s: boom",
                   "09:00:06  final  | status=completed duration=9s")
        for line in out:
            for ch in line:
                self.assertLess(ord(ch), 128, "non-ascii %r in %r" % (ch, line))
        self.assertIn("> read_file(a)", out[0])
        self.assertIn("x ERROR", out[2])

    def test_ascii_mode_has_no_unicode_ellipsis_when_clipping(self):
        fmt = Formatter(width=50, ascii_mode=True)
        line = fmt.feed("09:00:04  tool  | -> execute_code(" + "x" * 200 + ")")[0]
        self.assertIn("...", line)
        self.assertNotIn("\u2026", line)


class TestRunTail(unittest.TestCase):
    """``run_tail(follow=False)`` against a real file: a one-shot read.

    The file path is printed first so a reader always knows where the full text
    is, and the exit code is 0 for every outcome including a missing file — the
    pane must not die because a transcript has not been created yet.

    The fixture directory lives under ``SCRATCH``; ``setUp`` creates the root if
    it is absent and ``tearDown`` removes only the files it created.
    """

    def setUp(self):
        os.makedirs(SCRATCH, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=SCRATCH)
        self.path = os.path.join(self.dir, "task-0.log")

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def run_once(self):
        # stdout is captured instead of redirected to a pty: ``colour_enabled``
        # then says "not a tty" and the output is plain, which is what the
        # assertions below expect.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run_tail(self.path, follow=False)
        return code, buf.getvalue()

    def test_prints_the_transcript_path_first_and_exits_zero(self):
        self.write("09:00:00 user     | kickoff: Do the thing\n")
        code, out = self.run_once()
        self.assertEqual(code, 0)
        self.assertEqual(out.split("\n")[0], "transcript: %s" % self.path)

    def test_renders_the_file_readably(self):
        self.write("\n".join(HEADER_LINES) + "\n"
                   "09:00:00 user     | kickoff: Do the thing | context: none\n"
                   "09:00:02 think    | thinking about it\n")
        code, out = self.run_once()
        self.assertEqual(code, 0)
        self.assertIn("TASK  Do the thing", out)
        self.assertIn("context: none", out)
        # A held think with nothing after it is emitted before the reader stops.
        self.assertIn("thinking about it", out)

    def test_missing_file_does_not_write_events(self):
        # An empty transcript is one line of path plus the blank separator;
        # nothing is invented for a file with no events.
        self.write("")
        code, out = self.run_once()
        self.assertEqual(code, 0)
        self.assertEqual(out, "transcript: %s\n\n" % self.path)

    def test_one_shot_read_shows_an_unterminated_last_line(self):
        # Following waits for the newline; a one-shot read must not hide what
        # is already on disk.
        self.write("09:00:00  assistant  | half a line without a newline")
        code, out = self.run_once()
        self.assertEqual(code, 0)
        self.assertIn("half a line", out)

    def test_whole_file_is_rendered_in_order(self):
        self.write("\n".join([
            "09:00:00  start  | Build the thing",
            "09:00:01  assistant  | first",
            "09:00:02  assistant  | second",
            "",
        ]))
        code, out = self.run_once()
        self.assertEqual(code, 0)
        self.assertLess(out.index("first"), out.index("second"))


class TestNarrowAndMidFile(unittest.TestCase):
    """Found by review of the first test pass; each was a real defect."""

    def test_fitted_lines_never_exceed_a_narrow_pane(self):
        # Below about 40 columns the timestamp and tool name alone are wider
        # than the pane; the terminal wraps those. The test asserts the fitted
        # part never *adds* to the problem: no line this module produces may
        # exceed the pane width.
        for width in (40, 46, 61, 93):
            fmt = Formatter(width=width, colour=False)
            for line in ("12:00:00  tool  | -> execute_code(" + "x" * 50 + ")",
                         "12:00:01  result  | execute_code ok 0.1s: " + "y" * 80):
                for out in fmt.feed(line):
                    self.assertLessEqual(len(out), width, (width, out))

    def test_clip_marker_is_whole_or_absent(self):
        # A 20-column pane is narrower than the marker itself; the regex is the
        # negative control for a half-written "(+12 c" — either the whole marker
        # or a bare ellipsis, never a fragment.
        out = Formatter(width=20, colour=False).feed(
            "12:00:00  tool  | -> execute_code(" + "x" * 50 + ")")[0]
        self.assertNotRegex(out, r"\(\+\d+ c(?!hars\))")
        self.assertTrue(out.endswith("\u2026") or out.endswith("chars)"), out)

    def test_end_line_alone_does_not_announce_an_ordinary_exit(self):
        # The follower can start mid-file (after a truncation) and see only
        # the closing line.
        fmt = Formatter(width=70, colour=False)
        self.assertEqual(
            fmt.feed("12:00:00  final  | end status=completed exit_reason=completed"), [])
        self.assertEqual(
            fmt.feed("12:00:01  final  | end status=completed exit_reason=interrupted"),
            ["12:00:01  exit: interrupted"])


class TestUntrustedText(unittest.TestCase):
    """A transcript is untrusted input: nothing in it may drive the reader's
    terminal. OSC 52 sets a clipboard, OSC 8 forges a link, CSI clears a pane,
    and a bare NUL/CR edits the line that was just drawn."""

    #: Each entry is a complete attack, not a fragment: clipboard write, forged
    #: hyperlink, clear+home, window title, NUL, carriage return, 8-bit CSI and
    #: a bidi override. The last one reorders what is displayed while the bytes
    #: stay identical.
    HOSTILE = [
        "\x1b]52;c;cHduZWQ=\x07",
        "\x1b]8;;https://evil.example\x1b\\x\x1b]8;;\x1b\\",
        "\x1b[2J\x1b[H",
        "\x1b]0;title\x07",
        "\x00",
        "\r",
        "\x9b31m",
        "\u202eRTL\u202c",
    ]

    def displayed(self, line):
        # Assembled with "\n" so one assertion can look for a byte anywhere in
        # the rendered block rather than per line.
        out = Formatter(width=90, colour=False).feed(line)
        return "\n".join(out)

    def test_assistant_text_carries_no_escape(self):
        for hostile in self.HOSTILE:
            with self.subTest(hostile=repr(hostile)):
                out = self.displayed("09:00:00  assistant  | a" + hostile + "b")
                self.assertNotIn("\x1b", out)
                self.assertNotIn("\x00", out)
                self.assertNotIn("\r", out)
                self.assertNotIn("\x9b", out)
                self.assertNotIn("\u202e", out)
                # The visible words around it survive; the line is not dropped.
                self.assertIn("a", out)
                self.assertIn("b", out)

    def test_tool_and_foreign_lines_are_defused_too(self):
        # Defusing happens before the event parse, so it covers the tool and
        # "not an event" branches without either of them remembering to ask.
        for line in ("09:00:00  tool  | -> read(\x1b]52;c;x\x07)",
                     "noise \x1b]52;c;x\x07",
                     '09:00:00  result  | t ok 0.1s: {"output": "\x1b[2J"}'):
            out = self.displayed(line)
            self.assertNotIn("\x1b", out, line)

    def test_tab_is_kept(self):
        # Tabs are the one control character kept: they are layout the writer
        # may have used, and the terminal expands them harmlessly.
        out = self.displayed("09:00:00  assistant  | a\tb")
        self.assertIn("a\tb", out)


class TestRotation(unittest.TestCase):
    """Replacing the file is not truncating it: a larger replacement used to be
    read from the old offset, showing half a line and skipping the start.

    Both tests drive ``python -m hermes_tmux tail`` as a subprocess, because the
    follower loop is the thing under test. They are timing-sensitive: the first
    waits 1.3 s before the replacement so the follower has taken its initial
    read and is parked on a 1 s poll; the second measures CPU over 2 s, which is
    a coarse bound (a spin burns ~2 s of CPU, a correct wait ~0).
    """

    def setUp(self):
        os.makedirs(SCRATCH, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=SCRATCH)
        self.path = os.path.join(self.dir, "task-0.log")

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def test_replacement_from_the_beginning(self):
        # ``os.replace`` keeps the path and changes the inode. The follower
        # keys on (st_dev, st_ino), so it must restart at offset 0 and show the
        # whole new file. "MENT-HEAD" is the negative control: it is the tail of
        # "REPLACEMENT-HEAD" and can only appear if the reader started mid-line.
        import subprocess
        import sys
        import time

        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("09:00:00  assistant  | first\n")
        # NO_COLOR/TERM=dumb keep the child's output plain, so the assertions
        # match text rather than escape-laden lines.
        env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
        proc = subprocess.Popen(
            [sys.executable, "-m", "hermes_tmux", "tail", self.path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        try:
            time.sleep(1.3)
            replacement = self.path + ".new"
            with open(replacement, "w", encoding="utf-8") as handle:
                handle.write("09:00:01  assistant  | REPLACEMENT-HEAD\n"
                             "09:00:02  assistant  | second\n")
            os.replace(replacement, self.path)
            time.sleep(1.5)
        finally:
            proc.terminate()
            try:
                out = proc.communicate(timeout=5)[0]
            except subprocess.TimeoutExpired:
                proc.kill()
                out = proc.communicate()[0]
        self.assertIn("REPLACEMENT-HEAD", out)
        self.assertNotIn("MENT-HEAD", out.replace("REPLACEMENT-HEAD", ""))
        self.assertIn("first", out)

    def test_unreadable_file_does_not_busy_loop(self):
        # stat() succeeds and open() fails: the old loop treated the empty read
        # as "no new data" and retried with no wait, burning a core.
        #
        # This relies on chmod 000 actually denying the read, i.e. on not
        # running as root; as root the file stays readable and the test proves
        # nothing about the spin path (it still passes, vacuously).
        import resource
        import subprocess
        import sys
        import time

        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("09:00:00  assistant  | secret\n")
        os.chmod(self.path, 0o000)
        env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
        before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
        proc = subprocess.Popen(
            [sys.executable, "-m", "hermes_tmux", "tail", self.path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            time.sleep(2.0)
        finally:
            # Restore permissions first: tearDown has to unlink the file, and
            # the directory removal fails on a file it cannot read.
            os.chmod(self.path, 0o600)
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
        used = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime - before
        self.assertLess(used, 1.0, "follower burned %.2fs CPU in 2s" % used)


if __name__ == "__main__":
    unittest.main()
