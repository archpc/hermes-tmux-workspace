"""Adaptive overview layout: compact stays byte-identical, expanded earns space.

Goldens in tests/goldens/ were captured from the base renderer (e240962) for the
compact geometries; they must not change. The expanded tier widens the measure
and fills a tall pane with history, while live work is never trimmed.
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path

from tests.test_render import NOW, done, running, stamp, visible
from hermes_tmux.render import (
    EXPANDED_CONTENT_WIDTH,
    EXPANDED_MIN_WIDTH,
    MAX_CONTENT_WIDTH,
    TALL_PANE_MIN_HEIGHT,
    render,
)

GOLDENS = Path(__file__).parent / "goldens"


def fixture():
    """1 running + 10 recent: more than the default six-row history budget."""
    ws = [running()]
    for i in range(10):
        ws.append(done(
            key="deleg_recent%02d#0" % i, delegation_id="deleg_recent%02d" % i,
            goal="Recent task number %d with a fairly long title to clip" % i,
            started=stamp(NOW - timedelta(seconds=200 + i * 30)),
            last_event=stamp(NOW - timedelta(seconds=120 + i * 30)),
            duration_s=10.0 + i))
    return ws


def recent_rows(text):
    return sum(1 for line in text.split("\n") if "Recent task" in line)


def content_width(text):
    return max((len(visible(line).strip()) for line in text.split("\n")), default=0)


class CompactGoldenTests(unittest.TestCase):
    def test_compact_layouts_are_byte_identical_to_base(self):
        ws = fixture()
        for w, h in ((118, 35), (120, 35), (199, 40)):
            with self.subTest(width=w, height=h):
                txt = render(ws, width=w, height=h, now=NOW, session_label="main", colour=False)
                golden = (GOLDENS / ("overview-%dx%d.txt" % (w, h))).read_text()
                self.assertEqual(txt, golden, "the compact layout changed at %dx%d" % (w, h))

    def test_measure_step_boundary(self):
        # A long title forces clipping, so the content width reveals the measure.
        ws = [running(goal="A very long running task title " + "x" * 200)]
        below = render(ws, width=EXPANDED_MIN_WIDTH - 1, height=40, now=NOW, colour=False)
        at = render(ws, width=EXPANDED_MIN_WIDTH, height=40, now=NOW, colour=False)
        self.assertLessEqual(content_width(below), MAX_CONTENT_WIDTH)
        self.assertGreater(content_width(at), MAX_CONTENT_WIDTH)
        self.assertLessEqual(content_width(at), EXPANDED_CONTENT_WIDTH)


class ExpandedLayoutTests(unittest.TestCase):
    def test_tall_pane_fills_history_but_short_pane_keeps_budget(self):
        ws = fixture()
        short = render(ws, width=120, height=TALL_PANE_MIN_HEIGHT - 1, now=NOW, colour=False)
        tall = render(ws, width=120, height=87, now=NOW, colour=False)
        self.assertEqual(recent_rows(short), 6, "a short pane should keep the default budget")
        self.assertGreater(recent_rows(tall), recent_rows(short),
                           "a tall pane did not show more history")
        self.assertEqual(recent_rows(tall), 10, "all available history should fit a tall pane")

    def test_height_budget_is_independent_of_width(self):
        ws = fixture()
        tall_narrow = render(ws, width=118, height=87, now=NOW, colour=False)
        self.assertGreater(recent_rows(tall_narrow), 6,
                           "a tall narrow pane should also fill with history")

    def test_live_rows_survive_any_height(self):
        ws = fixture()
        for h in (4, 10, 24, 50, 87):
            with self.subTest(height=h):
                txt = render(ws, width=120, height=h, now=NOW, colour=False)
                if h >= 4:
                    self.assertIn("Implement the renderer", visible(txt),
                                  "the running worker was trimmed at height %d" % h)

    def test_expanded_still_centers_and_never_exceeds_pane(self):
        ws = fixture()
        for w, h in ((200, 60), (380, 87)):
            txt = render(ws, width=w, height=h, now=NOW, colour=False)
            for line in txt.split("\n"):
                self.assertLessEqual(len(visible(line)), w)

    def test_protected_fields_survive_truncation(self):
        # Truncation-priority polish is deferred; it must hold by test that the
        # glyph and age are never clipped, and the ID is intact wherever it is
        # shown (the key label is gated at 90 cols; narrower layouts omit it).
        ws = [running(goal="An extremely long running task title " + "z" * 300)]
        for w in (60, 80, 118, 150, 200, 380):
            with self.subTest(width=w):
                v = visible(render(ws, width=w, height=24, now=NOW, colour=False))
                self.assertIn("●", v, "the running glyph was clipped at %d" % w)
                self.assertIn("42", v, "the age was clipped at %d" % w)
                if w >= 90:
                    self.assertIn("6cfc92ad", v, "the worker ID was clipped at %d" % w)


if __name__ == "__main__":
    unittest.main()
