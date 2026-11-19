"""The claims the display makes, and the ones it refuses to make.

Each class pins one honesty rule. They are not styling tests: every assertion
is about whether the pane states something the recorded evidence supports.

* **Silence is not failure, and it is not completion.** A running worker with a
  recent event stays RUNNING; the same worker quiet past ``STALL_AFTER_S``
  (600 s) is labelled with how long it has been quiet — a display-only label,
  never a verdict. A timestamp in the future (a transcript time joined to a
  manifest date across midnight) is clamped, not treated as hours stale.
* **An error mark belongs to the latest call.** ``error_pending`` is cleared by
  a new tool call or a successful result, so a worker that recovered stops
  claiming a failure it no longer has; the earlier call is still named.
* **A reader failure is not an empty workspace.** An exception while reading
  Hermes state renders as a failure line plus an explicit "not an empty
  workspace"; only a genuinely empty read says "No delegations yet."
* **A verdict is only what the reviewer said.** ``_verdict_of`` prefers an
  explicit label, takes the last such label, honours negation and typographic
  apostrophes, ignores enumerations ("PASS or FAIL"), and returns None rather
  than guessing. Recording PASS for a review that said FAIL is the failure mode
  that matters.
* **Watcher identity is the lock, not the command line.** A pid is only the
  coordinator if it holds the session's lock; a forged argv is not enough.
* **A review needs the work it judges.** ``review run`` refuses a missing
  worker or a transcript not on disk, and records nothing; a manual record does
  not attribute itself to a reviewer model that never ran.
* **``tests --run`` runs the suite it advertises**, in the pane it names. A
  missing receipt directory is swallowed by ``suite_run.main()``, whose receipt
  write is best-effort: a ``FileNotFoundError`` there is re-raised only when the
  receipt's parent still exists, so the runner's own exit status is returned
  unchanged rather than replaced by a fabricated success. The test pins 127
  because the runner was invoked with an empty TestBench binary.

Runtime notes: no tmux server, but two tests spawn real child processes (to be
seen by ``ps``/``lsof``) and one calls ``cli.cmd_review`` with a stub args
object. Those children sleep for 30 s and are killed in ``finally``. The ``ps``
skip on argv order guards nothing for key ``abc``: no ``watch.lock`` exists, so
the lock gate answers False before any ``ps`` comparison.
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
    """Stall is derived from the latest activity, never from the start time.

    ``now`` is a class constant so every case is deterministic. Glyphs are the
    unicode pair (``●`` run, ``◐`` stall) — the assertions use the character,
    not the word alone, because a row can carry the word and the wrong glyph.
    """

    now = datetime(2026, 11, 12, 12, 0, 0)

    def test_recent_activity_keeps_a_long_run_running(self):
        """Eight hours elapsed, one minute quiet: still RUNNING.

        This is the case where using ``started`` instead of ``last_event``
        would mislabel a healthy long run as stalled. The absence of the
        silence wording is asserted too, so "RUNNING" cannot be satisfied by a
        row that also claims no output.
        """
        w = _Worker(started="2026-11-12 08:00:00",
                    last_event="2026-11-12 11:59:00", state="running")
        text = render.render([w], now=self.now, width=120)
        row = _row(text)
        # RUNNING glyph, and no silence claim: the v3 row has no "run" word.
        self.assertIn("\u25cf", row)
        self.assertNotIn("no output for", text)
        self.assertIn("RUNNING", text)

    def test_silence_past_the_threshold_is_stall(self):
        """One hour quiet is stall; the row says how long, not "failed".

        The wording distinction is the test: "no output for 1h00m" describes
        silence, "run ended" would claim the process stopped. Both the stall
        glyph and the absence of the terminal phrase are asserted, so the test
        fails if the label drifts toward a verdict.
        """
        w = _Worker(started="2026-11-12 08:00:00",
                    last_event="2026-11-12 11:00:00", state="running")
        text = render.render([w], now=self.now, width=120)
        row = _row(text)
        # Silent, not failed: the v3 wording is "no output for <duration>".
        self.assertIn("no output for 1h00m", row + " " + _detail(text))
        self.assertIn("\u25d0", row)
        self.assertNotIn("run ended", text)

    def test_future_timestamp_does_not_stall(self):
        """A midnight-crossing transcript date must not look hours stale.

        The event (00:05) is dated after ``now`` (12:00 on the 12th) because the
        log only carries a time of day; the renderer clamps the activity to
        ``now`` instead of computing a negative quiet period or ~24 h of silence.
        """
        w = _Worker(started="2026-11-12 23:50:00",
                    last_event="2026-11-13 00:05:00", state="running")
        text = render.render([w], now=self.now, width=120)
        row = _row(text)
        self.assertIn("\u25cf", row)
        self.assertNotIn("no output for", text)


class ErrorMarkTest(unittest.TestCase):
    """The failure marker describes the worker *now*, not its worst moment.

    ``_Worker`` defaults ``error_pending=False`` and the two cases differ only
    in that flag while both carry ``last_error``. That is the discrimination:
    the presence of an error string alone must not produce "last call failed".
    """

    def test_only_the_latest_call_can_mark_an_error(self):
        """A recovering worker keeps its last call named, drops the failure claim.

        First half: pending error + error text renders the wording. Second half:
        same error text, flag cleared — the call is still named (the transcript
        is not hidden) but "last call failed" must not appear. Without the
        second half a renderer that always prints the marker would pass.
        """
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
    """Two different empty-looking pages, and the difference is the claim.

    `render.render_empty` takes ``note`` as an explicit failure channel. These
    tests call it directly rather than provoking a real exception, because the
    contract under test is the wording split, not the exception plumbing.
    """

    def test_reader_failure_is_not_rendered_as_empty(self):
        """A failure note is repeated verbatim and never softened to "empty"."""
        text = render.render_empty(width=100, note="state reader failed: boom")
        self.assertIn("state reader failed: boom", text)
        self.assertNotIn("no delegations found", text)
        self.assertIn("reader failure", text)

    def test_empty_workspace_still_says_empty(self):
        """No note means genuinely empty — and must not invent a failure word."""
        text = render.render_empty(width=100)
        # v3 headline for a genuinely empty workspace.
        self.assertIn("No delegations yet.", text)
        self.assertNotIn("reader failure", text)
        self.assertNotIn("failed", text)


class VerdictTest(unittest.TestCase):
    """``cli._verdict_of``: label class first, then last label, then prose.

    The order of the rule loop is what makes "result: tests pass … Verdict:
    FAIL" a FAIL: label *class* priority (VERDICT > CONCLUSION > RESULT >
    OVERALL) is checked before position, so an explicit verdict anywhere beats
    a generic label earlier in the document. Within a class, the last label that
    yields a decision wins, because reviews conclude at the end. Only when no
    label class decides does unlabelled prose get a say.

    A label followed solely by an enumeration ("PASS or FAIL") is not a
    decision, and no verdict at all returns None — never a guess.
    """

    def test_explicit_verdict_label_wins(self):
        """A labelled token is taken from the label, not the first token in text."""
        self.assertEqual(cli._verdict_of("... VERDICT: PASS ... FAIL elsewhere"), "PASS")
        self.assertEqual(cli._verdict_of("verdict=fail"), "FAIL")

    def test_negated_pass_is_not_a_pass(self):
        """Negation inverts the token; "not a pass" is a FAIL, not an absence."""
        self.assertEqual(cli._verdict_of("The work does not pass review."), "FAIL")
        self.assertEqual(cli._verdict_of("no pass possible here"), "FAIL")

    def test_markdown_emphasis_does_not_hide_the_label(self):
        """``*``/``_``/``#``/backtick and curly quotes are stripped before matching."""
        self.assertEqual(cli._verdict_of("**Verdict: FAIL** although pass rates rose"),
                         "FAIL")
        self.assertEqual(cli._verdict_of("## Verdict: **PASS**"), "PASS")

    def test_reviewer_prose_shapes_seen_in_practice(self):
        """The exact strings an earlier parser got wrong, as a table.

        Reading the table as a spec: ``Verdict (PASS or FAIL): FAIL`` must skip
        the enumeration and take the third token; ``Verdict: PASS or FAIL``
        states nothing; a generic "result: … pass" must lose to a later
        ``Verdict: FAIL``; ``pass or fail? pass.`` must take the trailing answer;
        and the three apostrophe forms (ASCII and U+2019) must all negate.
        """
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
        """No token and empty text both return None: silence is not a PASS."""
        self.assertIsNone(cli._verdict_of("some prose with no verdict"))
        self.assertIsNone(cli._verdict_of(""))


class WatcherIdentityTest(unittest.TestCase):
    """A pid file is a hint; the coordinator is whoever holds ``watch.lock``.

    Read the fixtures carefully, because the mechanism is narrower than the test
    names suggest: ``_pid_is_watcher`` asks `_lock_holder` (an ``lsof -t`` on the
    lock path) *before* it looks at the command line, and returns False when no
    process holds the lock. Neither fixture ever holds the lock, so both
    assertions are decided at that gate. They pin "no lock ⇒ not the
    coordinator" — the property ``down`` and ``follow`` rely on — not the argv
    comparison that runs when a holder exists.
    """

    def test_a_process_mentioning_the_words_is_not_the_watcher(self):
        """A forged command line with the right words is still not the watcher.

        The child is a real Python sleeping 30 s whose *extra* argv spells
        ``-m hermes_tmux watch --key guardkey``; Python ignores everything after
        ``-c``, so ``ps`` shows the words while nothing has opened the lock.
        The lock file is created but left unheld, so ``lsof`` names no pid and
        the check fails — which is exactly the case a recycled pid would
        present.
        """
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
        """A process under a different key is not this key's watcher.

        The child is a forged ``python -c "import time; time.sleep(30)"`` whose
        *extra* argv spells ``-m hermes_tmux watch --key other``; Python ignores
        everything after ``-c``. The assertion asks about key ``abc``, for which
        no ``watch.lock`` exists, so ``cli._pid_is_watcher`` returns False at the
        lock gate before it ever runs ``ps``: neither the key match nor the
        command-line comparison is exercised. The argv-order skip guards nothing
        in this case.
        """
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
