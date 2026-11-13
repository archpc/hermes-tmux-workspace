"""Unit tests for hermes_tmux.transcript — the readable live view of one log.

Formatter is fed lines and returns display lines; no terminal, no clock, no
tmux. ``run_tail(..., follow=False)`` is exercised against a temporary file.
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
SCRATCH = "/Users/sein/.hermes/cache/scratch/hxlab"

HEADER_LINES = [
    "=== Hermes subagent live transcript ===",
    "delegation: deleg_fixt0001   task: 0",
    "goal: Implement the tmux state reader and unit tests.",
    "started: 2026-11-12 09:00:00",
    "(append-only; streams while the subagent runs \u2014 tail -f me)",
    "========================================",
]


def visible(text):
    return ANSI_RE.sub("", text)


def feed(stream, *lines):
    """Flush every displayed line of ``stream`` into one list."""
    out = []
    for line in lines:
        out.extend(stream.feed(line))
    return out


class TestHeader(unittest.TestCase):
    def test_header_lines_are_skipped_before_the_first_event(self):
        fmt = Formatter(width=100)
        for line in HEADER_LINES:
            self.assertEqual(fmt.feed(line), [], line)

    def test_first_event_renders_after_the_header(self):
        fmt = Formatter(width=100)
        for line in HEADER_LINES:
            fmt.feed(line)
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
    def test_malformed_line_after_events_is_never_dropped(self):
        fmt = Formatter(width=100)
        fmt.feed("09:00:01  assistant  | hello")
        out = fmt.feed("THIS IS NOT AN EVENT")
        self.assertEqual(len(out), 1)
        self.assertIn("THIS IS NOT AN EVENT", out[0])

    def test_malformed_line_before_events_is_shown_too(self):
        fmt = Formatter(width=100)
        out = fmt.feed("WARNING: log was rotated")
        self.assertIn("WARNING: log was rotated", "\n".join(out))

    def test_blank_lines_between_events_are_not_lines(self):
        fmt = Formatter(width=100)
        fmt.feed("09:00:01  assistant  | hello")
        self.assertEqual(fmt.feed("   "), [])
        self.assertEqual(fmt.feed(""), [])

    def test_unknown_role_keeps_its_name(self):
        fmt = Formatter(width=100)
        out = fmt.feed("09:00:09  warning  | careful")
        self.assertEqual(len(out), 1)
        self.assertIn("warning", out[0])
        self.assertIn("careful", out[0])


class TestThinkCollapse(unittest.TestCase):
    def test_think_that_previews_the_assistant_line_is_collapsed(self):
        fmt = Formatter(width=100)
        self.assertEqual(fmt.feed("09:00:02  think  | I'll read the layout first."), [])
        out = fmt.feed("09:00:03  assistant  | I'll read the layout first. Then the tests.")
        self.assertEqual(out, ["", "09:00:03  I'll read the layout first. Then the tests.", ""])
        self.assertEqual(fmt.flush(), [])

    def test_think_followed_by_something_else_is_emitted_first(self):
        fmt = Formatter(width=100)
        fmt.feed("09:00:02  think  | I should look at the tests")
        out = fmt.feed("09:00:03  tool  | -> read_file(tests/test_render.py)")
        self.assertEqual(len(out), 2)
        self.assertIn("I should look at the tests", out[0])
        self.assertIn("read_file", out[1])

    def test_think_at_end_of_file_is_flushed(self):
        fmt = Formatter(width=100)
        self.assertEqual(fmt.feed("09:00:02  think  | still thinking"), [])
        out = fmt.flush()
        self.assertEqual(len(out), 1)
        self.assertIn("still thinking", out[0])
        self.assertEqual(fmt.flush(), [])

    def test_think_followed_by_a_shorter_assistant_text_is_kept(self):
        fmt = Formatter(width=100)
        fmt.feed("09:00:02  think  | a completely different opening")
        out = fmt.feed("09:00:03  assistant  | something else entirely")
        # The held think, then the assistant block (blank, text, blank).
        self.assertEqual(len(out), 4)
        self.assertIn("a completely different opening", out[0])
        self.assertIn("something else entirely", out[2])


class TestKickoff(unittest.TestCase):
    def test_kickoff_renders_task_and_context(self):
        fmt = Formatter(width=100)
        out = fmt.feed("09:00:00  user  | kickoff: Build the thing | context: branch feat/x")
        self.assertEqual(len(out), 3)
        self.assertIn("TASK", out[0])
        self.assertIn("Build the thing", out[0])
        self.assertIn("context: branch feat/x", out[1])
        self.assertEqual(out[2], "")

    def test_start_line_after_kickoff_is_suppressed(self):
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
    def test_tool_call_is_one_line_with_arrow_stripped(self):
        fmt = Formatter(width=100)
        out = fmt.feed("09:00:04  tool  | -> read_file(path=a.py)")
        self.assertEqual(len(out), 1)
        self.assertIn("read_file(path=a.py)", out[0])
        self.assertNotIn("->", out[0])

    def test_tool_call_is_fitted_to_the_width(self):
        for width in (60, 80, 100):
            fmt = Formatter(width=width)
            line = fmt.feed("09:00:04  tool  | -> execute_code(" + "x" * 300 + ")")[0]
            self.assertLessEqual(len(visible(line)), width - 2,
                                 "width=%d line=%d" % (width, len(visible(line))))
            self.assertIn("(+", line)
            self.assertIn("chars)", line)

    def test_tool_call_clipping_counts_what_is_hidden(self):
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
        fmt = Formatter(width=100)
        out = fmt.feed('09:00:21  result  | terminal ERROR 5.0s: '
                       '{"output": "FAILED (errors=1)", "exit_code": 1}')
        self.assertEqual(len(out), 1)
        self.assertIn("ERROR", out[0])
        self.assertIn("FAILED (errors=1)", out[0])

    def test_named_error_field_is_what_is_shown(self):
        fmt = Formatter(width=100)
        out = fmt.feed('09:00:21  result  | tool ERROR 0.4s: '
                       '{"output": "", "exit_code": 1, "error": "permission denied"}')
        self.assertIn("permission denied", out[0])
        self.assertNotIn('"error"', out[0])

    def test_error_result_is_not_fitted_to_one_line(self):
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
    def test_final_line_shows_status_and_duration(self):
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
        fmt = Formatter(width=100)
        out = fmt.feed("09:02:14  final  | end status=failed exit_reason=timeout")
        self.assertEqual(len(out), 1)
        self.assertIn("exit: timeout", out[0])


class TestStyle(unittest.TestCase):
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
        # than the pane; the terminal wraps those.
        for width in (40, 46, 61, 93):
            fmt = Formatter(width=width, colour=False)
            for line in ("12:00:00  tool  | -> execute_code(" + "x" * 50 + ")",
                         "12:00:01  result  | execute_code ok 0.1s: " + "y" * 80):
                for out in fmt.feed(line):
                    self.assertLessEqual(len(out), width, (width, out))

    def test_clip_marker_is_whole_or_absent(self):
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
        for line in ("09:00:00  tool  | -> read(\x1b]52;c;x\x07)",
                     "noise \x1b]52;c;x\x07",
                     '09:00:00  result  | t ok 0.1s: {"output": "\x1b[2J"}'):
            out = self.displayed(line)
            self.assertNotIn("\x1b", out, line)

    def test_tab_is_kept(self):
        out = self.displayed("09:00:00  assistant  | a\tb")
        self.assertIn("a\tb", out)


class TestRotation(unittest.TestCase):
    """Replacing the file is not truncating it: a larger replacement used to be
    read from the old offset, showing half a line and skipping the start."""

    def setUp(self):
        os.makedirs(SCRATCH, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=SCRATCH)
        self.path = os.path.join(self.dir, "task-0.log")

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def test_replacement_from_the_beginning(self):
        import subprocess
        import sys
        import time

        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("09:00:00  assistant  | first\n")
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
