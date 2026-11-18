"""Unit tests for the honesty rules the display depends on.

Each test here pins a claim the pane makes (or refuses to make): the error mark
belongs to the *latest* call, `stall` follows the last recorded activity rather
than the start time, a reader failure is not rendered as "no delegations", and a
negated verdict is not a PASS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from hermes_tmux import cli, render, state, watch


class _Worker:
    """Minimal object exposing the attributes the renderer reads."""

    def __init__(self, **kw):
        self.delegation_id = kw.get("delegation_id", "deleg_aaa11111")
        self.task_index = kw.get("task_index", 0)
        self.key = "%s#%d" % (self.delegation_id, self.task_index)
        self.role = kw.get("role", "executor")
        self.model = kw.get("model", "deepseek/deepseek-v4.1-flash-1017")
        self.goal = kw.get("goal", "audit")
        self.state = kw.get("state", "running")
        self.exit_reason = None
        self.phase = "tool"
        self.tool_count = 3
        self.last_tool = kw.get("last_tool", "terminal")
        self.last_error = kw.get("last_error")
        self.summary = None
        self.started = kw.get("started")
        self.duration_s = kw.get("duration_s")
        self.review = kw.get("review", {})
        self.last_event = kw.get("last_event")
        self.error_pending = kw.get("error_pending", False)


def _row(text, needle="audit"):
    """The worker's title line (glyph + task); the key sits on the line below."""
    return [line for line in text.splitlines() if needle in line][0]


def _detail(text, needle="audit"):
    """The dim second line of a worker's row (facts, silence, failure)."""
    lines = text.splitlines()
    at = next(i for i, line in enumerate(lines) if needle in line)
    return lines[at + 1] if at + 1 < len(lines) else ""


class StallRuleTest(unittest.TestCase):
    now = datetime(2026, 11, 12, 12, 0, 0)

    def test_recent_activity_keeps_a_long_run_running(self):
        w = _Worker(started="2026-11-12 08:00:00",
                    last_event="2026-11-12 11:59:00", state="running")
        text = render.render([w], now=self.now, width=120)
        row = _row(text)
        # RUNNING glyph, and no silence claim: the v3 row has no "run" word.
        self.assertIn("\u25cf", row)
        self.assertNotIn("no output for", text)
        self.assertIn("RUNNING", text)

    def test_silence_past_the_threshold_is_stall(self):
        w = _Worker(started="2026-11-12 08:00:00",
                    last_event="2026-11-12 11:00:00", state="running")
        text = render.render([w], now=self.now, width=120)
        row = _row(text)
        # Silent, not failed: the v3 wording is "no output for <duration>".
        self.assertIn("no output for 1h00m", row + " " + _detail(text))
        self.assertIn("\u25d0", row)
        self.assertNotIn("run ended", text)

    def test_future_timestamp_does_not_stall(self):
        """A midnight-crossing transcript date must not look hours stale."""
        w = _Worker(started="2026-11-12 23:50:00",
                    last_event="2026-11-13 00:05:00", state="running")
        text = render.render([w], now=self.now, width=120)
        row = _row(text)
        self.assertIn("\u25cf", row)
        self.assertNotIn("no output for", text)


class ErrorMarkTest(unittest.TestCase):
    def test_only_the_latest_call_can_mark_an_error(self):
        pending = _Worker(error_pending=True, last_error="terminal ERROR exit 1")
        text = render.render([pending], width=120)
        # v3 states the failure in words on the row's detail line.
        self.assertIn("last call failed: terminal", text)

        recovered = _Worker(error_pending=False, last_error="terminal ERROR exit 1")
        text = render.render([recovered], width=120)
        self.assertIn("terminal", text)   # the call is still named
        self.assertNotIn("last call failed", text,
                         "an error from an earlier call was still claimed as current")


class ReaderFailureTest(unittest.TestCase):
    def test_reader_failure_is_not_rendered_as_empty(self):
        text = render.render_empty(width=100, note="state reader failed: boom")
        self.assertIn("state reader failed: boom", text)
        self.assertNotIn("no delegations found", text)
        self.assertIn("reader failure", text)

    def test_empty_workspace_still_says_empty(self):
        text = render.render_empty(width=100)
        # v3 headline for a genuinely empty workspace.
        self.assertIn("No delegations yet.", text)
        self.assertNotIn("reader failure", text)
        self.assertNotIn("failed", text)


class VerdictTest(unittest.TestCase):
    def test_explicit_verdict_label_wins(self):
        self.assertEqual(cli._verdict_of("... VERDICT: PASS ... FAIL elsewhere"), "PASS")
        self.assertEqual(cli._verdict_of("verdict=fail"), "FAIL")

    def test_negated_pass_is_not_a_pass(self):
        self.assertEqual(cli._verdict_of("The work does not pass review."), "FAIL")
        self.assertEqual(cli._verdict_of("no pass possible here"), "FAIL")

    def test_markdown_emphasis_does_not_hide_the_label(self):
        self.assertEqual(cli._verdict_of("**Verdict: FAIL** although pass rates rose"),
                         "FAIL")
        self.assertEqual(cli._verdict_of("## Verdict: **PASS**"), "PASS")

    def test_reviewer_prose_shapes_seen_in_practice(self):
        """These are the exact strings an earlier parser got wrong."""
        cases = {
            "Verdict (PASS or FAIL): FAIL. The goal was not met.": "FAIL",
            "On the second pass through the transcript I found fabricated "
            "output. Overall: FAIL.": "FAIL",
            "This is not a pass.": "FAIL",
            "## Verdict\n**FAIL**": "FAIL",
            "The work does not pass review.": "FAIL",
            "verdict: pass or fail? pass.": "PASS",
            "no failures found; result: pass": "PASS",
            "Verdict: PASS or FAIL": None,
            "The work doesn't pass review.": "FAIL",
            # A generic label early must not beat an explicit verdict line later.
            "(1) reported the result: all 127 tests pass. (4) Verdict: FAIL": "FAIL",
            "Overall: PASS. Verdict: FAIL": "FAIL",
            # Typographic apostrophes must negate exactly like ASCII ones.
            "The work can\u2019t pass review.": "FAIL",
            "It won\u2019t pass.": "FAIL",
            "It isn\u2019t a pass.": "FAIL",
        }
        for text, want in cases.items():
            self.assertEqual(cli._verdict_of(text), want, repr(text))

    def test_absent_verdict_stays_none(self):
        self.assertIsNone(cli._verdict_of("some prose with no verdict"))
        self.assertIsNone(cli._verdict_of(""))


class WatcherIdentityTest(unittest.TestCase):
    def test_a_process_mentioning_the_words_is_not_the_watcher(self):
        """A spoofed command line is not enough: the watcher must hold the lock."""
        tmp = tempfile.TemporaryDirectory()
        try:
            os.environ["HERMES_HOME"] = tmp.name
            key = "guardkey"
            lock = watch.lock_path(key)
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text("", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, "-c",
                                     "import time; time.sleep(30)",
                                     "-m hermes_tmux watch --key " + key])
            try:
                # Same words on the command line, but it never flock()ed the lock.
                self.assertFalse(cli._pid_is_watcher(proc.pid, key))
            finally:
                proc.kill()
                proc.wait()
        finally:
            os.environ.pop("HERMES_HOME", None)
            tmp.cleanup()

    def test_key_must_match_exactly(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)",
             "-m", "hermes_tmux", "watch", "--key", "other"])
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", str(proc.pid)],
                                 capture_output=True, text=True).stdout
            if "-m hermes_tmux watch" not in cmd:
                self.skipTest("argv order not preserved by the interpreter")
            self.assertFalse(cli._pid_is_watcher(proc.pid, "abc"))
        finally:
            proc.kill()
            proc.wait()


class MidnightRolloverTest(unittest.TestCase):
    """A run that crosses midnight must not look a day stale."""

    def test_event_time_before_start_time_rolls_to_the_next_day(self):
        from hermes_tmux.state import _combine_started
        self.assertEqual(_combine_started("2026-11-12 23:50:00", "00:05:00"),
                         "2026-11-13 00:05:00")

    def test_same_day_event_is_unchanged(self):
        from hermes_tmux.state import _combine_started
        self.assertEqual(_combine_started("2026-11-12 09:00:00", "09:20:00"),
                         "2026-11-12 09:20:00")

    def test_rollover_keeps_a_worker_running_in_the_renderer(self):
        w = _Worker(started="2026-11-12 23:50:00", last_event="2026-11-13 00:05:00",
                    state="running")
        text = render.render([w], now=datetime(2026, 11, 13, 0, 6, 0), width=120)
        row = _row(text)
        self.assertIn("\u25cf", row)
        self.assertNotIn("no output for", text)


class ErrorMarkWidthTest(unittest.TestCase):
    def test_marker_never_pushes_a_line_past_the_width(self):
        w = _Worker(last_tool="execute_code", error_pending=True)
        for width in (40, 60, 80, 110):
            text = render.render([w], width=width)
            for line in text.splitlines():
                self.assertLessEqual(len(line), width, "%r exceeds %d" % (line, width))
            # The current failure stays legible at every width the row is shown.
            self.assertIn("last call failed: execute_code", text,
                          "failure marker clipped away at width %d" % width)


class ReviewRefusalTest(unittest.TestCase):
    """A review must not run, or be recorded, without the work it judges."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self.old
        self.tmp.cleanup()

    def test_missing_worker_is_refused(self):
        args = type("A", (), {"action": "run", "delegation": "deleg_nope",
                              "task": 0, "status": "", "report": "",
                              "reviewer": "z-ai/glm-5.3-flash", "hermes": "",
                              "reviewer_delegation": ""})()
        self.assertEqual(cli.cmd_review(args), 2)
        self.assertFalse((self.home / "cache" / "hermes-tmux" / "reviews.json").exists(),
                         "a review was recorded for a worker that was never read")

    def test_worker_without_transcript_is_refused(self):
        live = self.home / "cache" / "delegation" / "live" / "deleg_nolog01"
        live.mkdir(parents=True)
        (live / "manifest.json").write_text(
            '{"delegation_id": "deleg_nolog01", "started": "2026-11-12 09:00:00",'
            ' "task_count": 1, "tasks": [{"index": 0, "goal": "x", "status": "completed"}]}')
        args = type("A", (), {"action": "run", "delegation": "deleg_nolog01",
                              "task": 0, "status": "", "report": "",
                              "reviewer": "z-ai/glm-5.3-flash", "hermes": "",
                              "reviewer_delegation": ""})()
        self.assertEqual(cli.cmd_review(args), 2)
        self.assertFalse((self.home / "cache" / "hermes-tmux" / "reviews.json").exists())


class ReviewRecordValidationTest(unittest.TestCase):
    """Recording a verdict must name real work and invent no reviewer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)
        self.registry = self.home / "cache" / "hermes-tmux" / "reviews.json"

    def tearDown(self):
        if self.old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self.old
        self.tmp.cleanup()

    def _args(self, **kw):
        base = dict(action="record", delegation="deleg_ghost", task=0, status="recorded",
                    report="", reviewer="", hermes="", reviewer_delegation="")
        base.update(kw)
        return type("A", (), base)()

    def test_unknown_delegation_is_refused(self):
        self.assertEqual(cli.cmd_review(self._args()), 2)
        self.assertFalse(self.registry.exists())

    def test_recorded_verdict_does_not_invent_a_reviewer_model(self):
        live = self.home / "cache" / "delegation" / "live" / "deleg_real0001"
        live.mkdir(parents=True)
        log = live / "task-0.log"
        log.write_text("=== Hermes subagent live transcript ===\n"
                       "09:00:01 final     | status=completed duration=1.0s\n")
        (live / "manifest.json").write_text(json.dumps({
            "delegation_id": "deleg_real0001", "started": "2026-11-12 09:00:00",
            "task_count": 1, "model": "deepseek/x",
            "tasks": [{"index": 0, "goal": "g", "log": str(log), "status": "completed"}]}))
        self.assertEqual(cli.cmd_review(self._args(delegation="deleg_real0001")), 0)
        entry = json.loads(self.registry.read_text())["reviews"][0]
        self.assertIsNone(entry["reviewer_model"],
                          "a manual record attributed itself to a model that never ran")


class TestsRunBranchTest(unittest.TestCase):
    """`tests --run` on an existing pane must respawn it, not just focus it."""

    def test_run_on_existing_pane_respawns_with_the_recording_command(self):
        from unittest import mock
        from hermes_tmux import tmuxio

        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        os.environ["HERMES_HOME"] = str(home)
        os.environ["HERMES_TMUX"] = "1"
        try:
            with mock.patch.object(tmuxio, "in_tmux", return_value=True), \
                 mock.patch.object(tmuxio, "current_session_id", return_value="$0"), \
                 mock.patch.object(tmuxio, "session_key", return_value="k"), \
                 mock.patch.object(tmuxio, "find_window_by_name",
                                   return_value={"window_id": "@1", "name": "hx:tests"}), \
                 mock.patch.object(tmuxio, "list_panes",
                                   return_value=[{"pane_id": "%2", "ours": True}]), \
                 mock.patch.object(tmuxio, "respawn") as respawn, \
                 mock.patch.object(tmuxio, "select_window"):
                wid, status = cli.ensure_aux_window("k", "$0", "tests", run=True)
            self.assertEqual((wid, status), ("@1", "running"))
            self.assertTrue(respawn.called,
                            "tests --run focused the pane instead of running in it")
            self.assertNotIn("unittest", respawn.call_args[0][1])
            self.assertIn("tests.log", respawn.call_args[0][1])
        finally:
            os.environ.pop("HERMES_HOME", None)
            os.environ.pop("HERMES_TMUX", None)
            tmp.cleanup()


class AuxCommandTest(unittest.TestCase):
    def test_explicit_run_fails_when_disabled_or_outside_tmux(self):
        from unittest.mock import patch
        from hermes_tmux import tmuxio
        args = type("Args", (), {"role": "tests", "run": True})()
        with patch.object(tmuxio, "in_tmux", return_value=False):
            for enabled in ("0", "1"):
                with patch.dict(os.environ, {"HERMES_TMUX": enabled}):
                    self.assertNotEqual(cli.cmd_aux(args), 0)

    def test_missing_receipt_dir_does_not_replace_runner_status(self):
        from unittest.mock import patch
        from hermes_tmux import suite_run
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("sys.argv", ["suite_run", td, str(root / "tests.log"), "", str(root / "gone/exit")]):
                self.assertEqual(suite_run.main(), 127)

    def test_tests_pane_does_not_run_by_default(self):
        _cwd, cmd = cli._aux_command("tests", "/bin/zsh")
        self.assertNotIn("tee", cmd)
        # It advertises the opt-in command instead of executing the suite.
        self.assertIn("tests --run", cmd)

    def test_tests_run_writes_the_log_it_advertises(self):
        _cwd, cmd = cli._aux_command("tests", "/bin/zsh", run=True)
        self.assertNotIn("unittest", cmd)
        self.assertIn("tests.log", cmd)


if __name__ == "__main__":
    unittest.main()
