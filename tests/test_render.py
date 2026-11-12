"""Unit tests for hermes_tmux.render — pure, deterministic fixed-width block."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from hermes_tmux.render import LEGEND, render, render_empty

NOW = datetime(2026, 11, 12, 12, 0, 0)


class Worker(object):
    """Tiny local stub; render() must not import hermes_tmux.state."""

    def __init__(self, **overrides):
        self.delegation_id = "deleg_6cfc92ad"
        self.task_index = 0
        self.key = "6cfc92ad#0"
        self.role = "executor"
        self.model = "deepseek/deepseek-v4.1-flash-1017"
        self.goal = "Implement the renderer and unit tests."
        self.state = "running"
        self.exit_reason = None
        self.phase = "tool"
        self.tool_count = 3
        self.last_tool = "read_file"
        self.last_error = None
        self.summary = None
        self.started = "2026-11-12 11:59:18"
        self.duration_s = None
        self.review = {}
        self.delegation_started = "2026-11-12 11:59:00"
        for key, value in overrides.items():
            setattr(self, key, value)


def rows_of(text):
    """Rows live between the header line and the closing rule/summary block."""
    return text.split("\n")[3:-4]


class TestEmpty(unittest.TestCase):
    def test_empty_explains_and_shows_root(self):
        out = render_empty(width=110, live_root="/tmp/hermes/live")
        self.assertIn("no delegations found", out)
        self.assertIn("/tmp/hermes/live", out)
        self.assertIn(LEGEND, out)

    def test_render_with_no_workers_delegates_to_empty(self):
        out = render([], width=110, live_root="/tmp/hermes/live")
        self.assertIn("no delegations found", out)
        self.assertIn(LEGEND, out)

    def test_empty_unknown_root_does_not_raise(self):
        out = render_empty(width=80)
        self.assertIn("(unknown)", out)


class TestSingleRunningWorker(unittest.TestCase):
    def test_title_counts_and_row(self):
        out = render([Worker()], now=NOW)
        lines = out.split("\n")
        self.assertTrue(lines[0].startswith("hermes subagents"))
        self.assertIn("1 running / 1 total", lines[0])
        self.assertEqual(lines[1], "-" * 110)
        self.assertIn("KEY", lines[2])
        self.assertIn("GOAL", lines[2])
        self.assertEqual(len(rows_of(out)), 1)
        row = rows_of(out)[0]
        self.assertIn("6cfc92ad#0", row)
        self.assertIn("exec", row)
        self.assertIn("run", row)
        self.assertIn("42s", row)

    def test_title_carries_session_label(self):
        out = render([Worker()], now=NOW, session_label="hx:workers")
        self.assertIn("hermes subagents - hx:workers", out.split("\n")[0])

    def test_footer_counts_and_legend(self):
        out = render([Worker()], now=NOW)
        lines = out.split("\n")
        self.assertIn("total 1", lines[-3])
        self.assertIn("review: 0 recorded, 1 unreviewed", lines[-2])
        self.assertTrue(lines[-1].endswith(LEGEND))
        self.assertIn("legend: STATE from Hermes state", out)


class TestRoleDistinctness(unittest.TestCase):
    def test_reviewer_row_differs_from_executor(self):
        executor = Worker(task_index=0, role="executor")
        reviewer = Worker(
            task_index=1,
            role="reviewer",
            model="z-ai/glm-5.3-flash",
            goal="Review task 0.",
            review={"status": "recorded", "reviewer_model": "z-ai/glm-5.3-flash", "at": "2026-11-12 11:59:50"},
        )
        out = render([executor, reviewer], now=NOW)
        rows = rows_of(out)
        self.assertEqual(len(rows), 2)
        exec_row = [r for r in rows if "#0" in r][0]
        rev_row = [r for r in rows if "#1" in r][0]
        self.assertNotEqual(exec_row, rev_row)
        self.assertIn("exec", exec_row)
        self.assertIn("*revw", rev_row)
        self.assertIn("review: recorded", rev_row)
        self.assertNotIn("review: recorded", exec_row)

    def test_reviewer_marker_survives_narrow_width(self):
        reviewer = Worker(role="reviewer", goal="Review.")
        out = render([reviewer], width=48, now=NOW)
        row = rows_of(out)[0]
        self.assertIn("*rev", row)


class TestStallDerivation(unittest.TestCase):
    def test_eleven_minutes_is_stall(self):
        started = (NOW - timedelta(minutes=11)).strftime("%Y-%m-%d %H:%M:%S")
        out = render([Worker(started=started, state="running")], now=NOW)
        self.assertIn("stall", rows_of(out)[0])
        self.assertIn("stall 1", out)

    def test_nine_minutes_is_running(self):
        started = (NOW - timedelta(minutes=9)).strftime("%Y-%m-%d %H:%M:%S")
        out = render([Worker(started=started, state="running")], now=NOW)
        row = rows_of(out)[0]
        self.assertIn("run", row)
        self.assertNotIn("stall", row)

    def test_finished_worker_never_goes_stall(self):
        started = (NOW - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        out = render([Worker(started=started, state="done")], now=NOW)
        self.assertIn("done", rows_of(out)[0])
        self.assertNotIn("stall", out)

    def test_iso_timestamps_parse(self):
        started = (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = render([Worker(started=started, state="running")], now=NOW)
        self.assertIn("30m", rows_of(out)[0])


class TestWidthGuarantee(unittest.TestCase):
    def _assert_fits(self, text, width):
        for line in text.split("\n"):
            self.assertLessEqual(len(line), width, "line too long: %r" % line)
            self.assertEqual(line, line.rstrip(), "trailing whitespace: %r" % line)
            self.assertTrue(all(ord(ch) < 128 for ch in line), "non-ascii: %r" % line)
            self.assertNotIn("\x1b", line)

    def _workers(self):
        return [
            Worker(delegation_id="deleg_aaaa1111", task_index=0, role="executor",
                   goal="A very long goal " * 8, state="running"),
            Worker(delegation_id="deleg_bbbb2222", task_index=1, role="reviewer",
                   model="z-ai/glm-5.3-flash", state="done", duration_s=120,
                   review={"status": "recorded"}, last_error="boom"),
            Worker(delegation_id="deleg_cccc3333", task_index=2, state="failed",
                   last_tool="execute_code", model=None),
        ]

    def test_every_line_fits(self):
        for width in (60, 80, 110, 30):
            out = render(self._workers(), width=width, now=NOW, session_label="hx")
            self._assert_fits(out, width)
            self._assert_fits(render_empty(width=width, live_root="/a/very/long/live/root/path"), width)

    def test_pathological_workers_still_fit(self):
        wide = Worker(
            delegation_id="x" * 200,
            task_index=99,
            goal="\t\n\x01wild\x7f goal   with\tcontrol\nchars",
            model="/" * 20,
            last_tool="t" * 100,
            last_error="e" * 100,
            state="some-unknown-state",
            started="not-a-date",
        )
        out = render([wide], width=30, now=NOW)
        self._assert_fits(out, 30)
        # Unknown states are shown as the literal state, truncated to 5 — never
        # invented as one of the known labels.
        self.assertIn("some-", out)
        self.assertNotIn("fail", out)


class TestMaxRows(unittest.TestCase):
    def test_truncates_rows(self):
        workers = [Worker(task_index=i, delegation_id="deleg_%08d" % i) for i in range(5)]
        out = render(workers, now=NOW, max_rows=2)
        self.assertEqual(len(rows_of(out)), 2)
        self.assertIn("total 5", out)

    def test_all_rows_when_within_limit(self):
        workers = [Worker(task_index=i) for i in range(3)]
        out = render(workers, now=NOW, max_rows=12)
        self.assertEqual(len(rows_of(out)), 3)

    def test_zero_rows(self):
        out = render([Worker()], now=NOW, max_rows=0)
        self.assertEqual(len(rows_of(out)), 0)

    def test_running_rows_come_first(self):
        workers = [
            Worker(task_index=0, delegation_id="deleg_00000000", state="done"),
            Worker(task_index=1, delegation_id="deleg_11111111", state="running"),
        ]
        rows = rows_of(render(workers, now=NOW))
        self.assertIn("11111111", rows[0])
        self.assertIn("00000000", rows[1])


class TestColumns(unittest.TestCase):
    def test_model_prefix_stripped(self):
        row = rows_of(render([Worker(model="deepseek/deepseek-v4.1-flash-1017")], now=NOW))[0]
        self.assertNotIn("/", row)
        self.assertIn("deepseek-v4.1-fl", row)

    def test_model_without_slash_renders_as_is(self):
        row = rows_of(render([Worker(model="glm-5")], now=NOW))[0]
        self.assertIn("glm-5", row)

    def test_none_model_shows_dash(self):
        row = rows_of(render([Worker(model=None)], now=NOW))[0]
        self.assertIn("-", row)
        self.assertNotIn("None", row)

    def test_error_marker(self):
        row = rows_of(render([Worker(last_tool="execute_code", last_error="boom")], now=NOW))[0]
        # One column is reserved for the marker so it survives the width cap.
        self.assertIn("execute_co!", row)
        self.assertLessEqual(len(row), 132)

    def test_no_error_has_no_bang(self):
        row = rows_of(render([Worker(last_tool="execute_code")], now=NOW))[0]
        self.assertIn("execute_cod", row)
        self.assertNotIn("!", row)

    def test_missing_tool_is_dash(self):
        row = rows_of(render([Worker(last_tool=None, last_error=None)], now=NOW))[0]
        self.assertIn("-", row)

    def test_goal_whitespace_collapsed(self):
        goal = "Fix\tstate\nreader   and\tunit\n\ntests"
        row = rows_of(render([Worker(goal=goal, state="done")], now=NOW))[0]
        self.assertIn("Fix state reader and unit tests", row)
        self.assertNotIn("\t", row)
        self.assertNotIn("\n", row)

    def test_unparseable_started_gives_question_mark(self):
        row = rows_of(render([Worker(started="whenever", state="done")], now=NOW))[0]
        self.assertIn("?", row)

    def test_missing_attributes_do_not_raise(self):
        class Bare(object):
            pass

        out = render([Bare()], now=NOW)
        self.assertIn("other", out)
        self.assertIn("unreviewed", out)


class TestDeterminism(unittest.TestCase):
    def test_two_calls_are_byte_identical(self):
        workers = [Worker(task_index=0), Worker(task_index=1, state="done", duration_s=90)]
        first = render(workers, now=NOW, session_label="hx", live_root="/tmp/live")
        second = render(workers, now=NOW, session_label="hx", live_root="/tmp/live")
        self.assertEqual(first.encode("utf-8"), second.encode("utf-8"))

    def test_render_defaults_now_without_crashing(self):
        self.assertIsInstance(render([Worker()]), str)


if __name__ == "__main__":
    unittest.main()
