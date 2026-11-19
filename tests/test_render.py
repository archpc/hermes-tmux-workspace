"""Unit tests for hermes_tmux.render — the pure, deterministic agents overview.

No tmux, no state module: workers are ``types.SimpleNamespace`` stubs carrying
exactly the attributes render.py documents. ``now`` is always passed
explicitly so nothing here depends on the wall clock, and colour is checked by
stripping SGR sequences rather than by matching escape bytes, so a theme change
cannot break a test that is not about colour.

What this module pins, in the order the renderer decides it:

* ``classify`` — which section a worker lands in. A failure is "attention" only
  while all conditions hold at once: key not in ``seen``, last activity within
  ``ATTENTION_TTL_S`` (a failure with no timestamp counts as fresh), and
  activity not older than ``since``. Each exists because alone it produced a
  badge that could never clear.
* geometry — every line is at most ``width`` display cells and free of trailing
  whitespace, at five widths down to a 40-column pane, with and without colour,
  in ASCII mode, and inside a ``height`` budget that history may not spend.
* rows — what a row states and what it refuses to state: an unknown state is
  never dressed up as FAILED or RUNNING, an error is quoted as its message
  rather than its JSON, and ``review:`` appears only for a recorded review.
* the empty / reader-failure split — a note means the reader failed and must
  never render as "no delegations".

There are no golden-file snapshots here. The assertions are structural
(substrings, widths, ordering), so they catch broken invariants but not an
unintended cosmetic change.
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from hermes_tmux import render as render_mod
from hermes_tmux.render import ATTENTION_TTL_S, STALL_AFTER_S, classify, render, render_empty, summary

NOW = datetime(2026, 11, 12, 12, 0, 0)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible(text):
    """The characters a pane shows: SGR sequences removed."""
    return ANSI_RE.sub("", text)


def stamp(dt):
    """The timestamp format render._parse_dt accepts (no timezone, no ``T``)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def worker(**overrides):
    """A consistent stub: attributes as render.py documents them.

    The field set is the ``Worker`` dataclass plus the optional aliases the
    renderer falls back to (``last_call``, ``last_message``, ``error_pending``);
    all are present so a row can be built with ``getattr`` defaults untouched.
    ``overrides`` is how each named fixture below states one fact only.
    """
    fields = dict(
        key="deleg_6cfc92ad#0",
        delegation_id="deleg_6cfc92ad",
        task_index=0,
        goal="Implement the renderer and unit tests.",
        role="executor",
        model="deepseek/deepseek-v4.1-flash-1017",
        state="running",
        exit_reason=None,
        started=stamp(NOW - timedelta(seconds=42)),
        last_event=stamp(NOW - timedelta(seconds=30)),
        duration_s=None,
        tool_count=3,
        last_tool="read_file",
        last_call=None,
        last_message=None,
        last_error=None,
        error_pending=None,
        summary=None,
        review={},
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def running(**kw):
    """The baseline stub, unchanged: a live run 30 s since its last event."""
    return worker(**kw)


def failed(**kw):
    """A finished run that stopped by iteration limit, with a last report.

    ``failed()`` is fresh (last event 8 min before ``NOW``), so it clears the
    attention TTL; the TTL tests rebuild this fixture with an older
    ``last_event`` rather than inventing a second failure shape.
    """
    base = dict(key="deleg_aaaabbbb#1", delegation_id="deleg_aaaabbbb", task_index=1,
                state="failed", exit_reason="max_iterations", goal="Fix the flaky test.",
                started=stamp(NOW - timedelta(minutes=10)),
                last_event=stamp(NOW - timedelta(minutes=8)), duration_s=120,
                last_message="I could not finish: permissions.")
    base.update(kw)
    return worker(**base)


def done(**kw):
    """A clean finish: ``exit_reason=completed`` plus the worker's own summary."""
    base = dict(key="deleg_ccccdddd#2", delegation_id="deleg_ccccdddd", task_index=2,
                state="done", exit_reason="completed", goal="Write the state reader tests.",
                started=stamp(NOW - timedelta(hours=1)),
                last_event=stamp(NOW - timedelta(minutes=55)), duration_s=300,
                summary="All eight tests pass.")
    base.update(kw)
    return worker(**base)


def stalled(**kw):
    """A run whose last event is 19 min old — past ``STALL_AFTER_S`` (600 s).

    State stays ``running``: stall is derived by the renderer, never read from
    Hermes. This fixture is what proves that derivation.
    """
    base = dict(key="deleg_eeeeffff#3", delegation_id="deleg_eeeeffff", task_index=3,
                goal="Wait for the build to finish.",
                started=stamp(NOW - timedelta(minutes=20)),
                last_event=stamp(NOW - timedelta(minutes=19)))
    base.update(kw)
    return worker(**base)


def lines_of(text):
    return text.split("\n")


# --------------------------------------------------------------------------- #
# classify()
# --------------------------------------------------------------------------- #


class TestClassify(unittest.TestCase):
    """Section assignment, the attention window's edges, and the sort order.

    The negative controls here matter as much as the positives: a stalled run
    must not become "attention", a seen failure must fall to history, and a
    failure that ended before the workspace came up must not reopen a badge for
    work the reader already outlived.
    """

    def test_running_worker_is_running(self):
        groups = classify([running()], now=NOW)
        self.assertEqual([w.key for w in groups["running"]], [running().key])
        self.assertEqual(groups["attention"], [])
        self.assertEqual(groups["recent"], [])

    def test_stalled_worker_stays_under_running(self):
        groups = classify([stalled()], now=NOW)
        self.assertEqual(len(groups["running"]), 1)
        self.assertEqual(groups["attention"], [])

    def test_fresh_failure_is_attention(self):
        groups = classify([failed()], now=NOW)
        self.assertEqual([w.key for w in groups["attention"]], [failed().key])
        self.assertEqual(groups["recent"], [])

    def test_failure_states_that_mean_failed_are_attention(self):
        for state in ("error", "failed", "interrupted"):
            groups = classify([failed(state=state, key="k-" + state)], now=NOW)
            self.assertEqual(len(groups["attention"]), 1, state)

    def test_seen_failure_is_recent(self):
        w = failed()
        groups = classify([w], now=NOW, seen={w.key})
        self.assertEqual(groups["attention"], [])
        self.assertEqual([x.key for x in groups["recent"]], [w.key])

    def test_failure_older_than_attention_ttl_is_recent(self):
        old = failed(started=stamp(NOW - timedelta(hours=9)),
                     last_event=stamp(NOW - timedelta(hours=8)))
        groups = classify([old], now=NOW)
        self.assertEqual(groups["attention"], [])
        self.assertEqual(len(groups["recent"]), 1)

    def test_failure_exactly_at_attention_ttl_is_still_attention(self):
        # The comparison is ``<= ATTENTION_TTL_S``, so the edge belongs to
        # attention. This pins the boundary direction: with ``<`` a run that
        # ended exactly at the TTL would land in recent instead.
        edge = failed(started=stamp(NOW - timedelta(hours=9)),
                      last_event=stamp(NOW - timedelta(seconds=ATTENTION_TTL_S)))
        groups = classify([edge], now=NOW)
        self.assertEqual(len(groups["attention"]), 1)

    def test_failure_one_second_past_ttl_is_recent(self):
        edge = failed(started=stamp(NOW - timedelta(hours=9)),
                      last_event=stamp(NOW - timedelta(seconds=ATTENTION_TTL_S + 1)))
        groups = classify([edge], now=NOW)
        self.assertEqual(groups["attention"], [])

    def test_failure_that_ended_before_since_is_recent(self):
        # ``since`` is the workspace's start; ``classify`` drops a failure whose
        # activity is strictly older, i.e. one that ended before this pane
        # existed and already had (or was never given) a chance to be seen.
        w = failed(started=stamp(NOW - timedelta(hours=2)),
                   last_event=stamp(NOW - timedelta(hours=1)))
        since = (NOW - timedelta(minutes=30)).timestamp()
        groups = classify([w], now=NOW, since=since)
        self.assertEqual(groups["attention"], [])
        self.assertEqual(len(groups["recent"]), 1)

    def test_failure_that_ended_after_since_is_attention(self):
        w = failed(started=stamp(NOW - timedelta(minutes=20)),
                   last_event=stamp(NOW - timedelta(minutes=10)))
        since = (NOW - timedelta(minutes=30)).timestamp()
        groups = classify([w], now=NOW, since=since)
        self.assertEqual(len(groups["attention"]), 1)

    def test_unparsable_since_is_ignored(self):
        # ``since`` comes from a workspace record; a corrupt value must widen
        # the window to "no floor", not empty the section.
        groups = classify([failed()], now=NOW, since="not-a-number")
        self.assertEqual(len(groups["attention"]), 1)

    def test_done_worker_is_recent(self):
        groups = classify([done()], now=NOW)
        self.assertEqual(groups["recent"], [done()])
        self.assertEqual(groups["running"], [])

    def test_none_entries_are_skipped(self):
        # ``load_workers`` may hand back a list built from partial reads; a
        # None hole must not be classified as "unknown" and rendered.
        groups = classify([None], now=NOW)
        self.assertEqual(groups, {"running": [], "attention": [], "recent": []})

    def test_running_sorted_oldest_first(self):
        # Oldest first so a row does not jump when a younger sibling finishes:
        # appending to a list sorted by start time leaves existing rows in place.
        newer = running(key="new", started=stamp(NOW - timedelta(minutes=1)),
                        last_event=stamp(NOW))
        older = running(key="old", started=stamp(NOW - timedelta(minutes=30)),
                        last_event=stamp(NOW))
        groups = classify([newer, older], now=NOW)
        self.assertEqual([w.key for w in groups["running"]], ["old", "new"])

    def test_attention_and_recent_newest_first(self):
        # The opposite rule for the two history-ish sections: the freshest
        # failure (or the freshest finish) is the one a reader is looking for.
        older = failed(key="old", started=stamp(NOW - timedelta(hours=2)),
                       last_event=stamp(NOW - timedelta(minutes=40)))
        newer = failed(key="new", started=stamp(NOW - timedelta(minutes=20)),
                       last_event=stamp(NOW - timedelta(minutes=5)))
        groups = classify([older, newer], now=NOW)
        self.assertEqual([w.key for w in groups["attention"]], ["new", "old"])


class TestSummary(unittest.TestCase):
    """The one place counts are computed, for the header and the tab badge."""

    def test_empty(self):
        self.assertEqual(summary({"running": [], "attention": [], "recent": []}, NOW),
                         {"running": 0, "stalled": 0, "attention": 0})

    def test_running_count_excludes_stalled(self):
        groups = {"running": [running(), stalled()], "attention": [], "recent": []}
        counts = summary(groups, NOW)
        self.assertEqual(counts["running"], 1)
        self.assertEqual(counts["stalled"], 1)

    def test_stalled_is_counted_as_attention(self):
        groups = {"running": [stalled()], "attention": [failed()], "recent": [done()]}
        counts = summary(groups, NOW)
        self.assertEqual(counts["attention"], 2)
        self.assertEqual(counts["recent"] if "recent" in counts else 0, 0)


# --------------------------------------------------------------------------- #
# geometry: width, whitespace, colour, ascii, height
# --------------------------------------------------------------------------- #


def crowded_workers():
    """A maximal, hostile worker list for the geometry tests.

    Every width-sensitive shape appears at once: an over-long goal, an error
    payload that is JSON, markdown in the last report, a ``None`` model, a
    200-character delegation id, tab/control characters, a 1000-character
    error, an unrecognised state and an unparsable date. If a line can grow
    past its column, one of these six does it.
    """
    return [
        running(key="k0", goal="A very long goal " * 8),
        failed(last_error='{"output": "", "error": "permission denied"}',
               last_message="*could not* finish `the thing`"),
        done(),
        stalled(),
        worker(key="k4", state="done", goal="x" * 300, model=None,
               started=stamp(NOW - timedelta(hours=3)),
               last_event=stamp(NOW - timedelta(hours=3)), exit_reason="timeout",
               duration_s=60),
        worker(key="k5", delegation_id="x" * 200, task_index=99,
               goal="\t\n\x01wild\x7f goal   with\tcontrol\nchars", model="/" * 20,
               last_tool="t" * 100, last_error="e" * 1000, state="some-unknown-state",
               started="not-a-date"),
    ]


# Keys of the first four crowded workers; ``k5`` is unmapped on purpose so the
# "tab N" segment is exercised present and absent in the same render.
TABS = {"k0": 3, "k1": 4, "k2": 5, "k4": 12}


class TestGeometry(unittest.TestCase):
    """Width, colour, ASCII and height — the invariants that break silently.

    ``assert_fits`` is the negative control for every geometry test: a line one
    column too wide is a wrapped row in a real pane, and trailing whitespace
    makes the pane repaint blank cells (and puts padding in a pipe).
    """

    def assert_fits(self, text, width, label=""):
        for line in lines_of(visible(text)):
            self.assertLessEqual(len(line), width,
                                 "%s line %d columns in %d: %r" % (label, len(line), width, line))
            self.assertEqual(line, line.rstrip(), "%s trailing whitespace: %r" % (label, line))

    def test_every_line_fits_at_each_width(self):
        # Five widths, because the renderer has three separate width ladders
        # (60/72/90 columns). 187 exercises the 118-column content cap plus
        # centring; 40 exercises a pane narrower than the fixed prefixes.
        for width in (187, 118, 93, 61, 40):
            out = render(crowded_workers(), now=NOW, width=width,
                         session_label="hx:workers", tabs=TABS, colour=True)
            self.assert_fits(out, width, "width=%d" % width)

    def test_width_fits_without_colour(self):
        for width in (187, 118, 93, 61, 40):
            out = render(crowded_workers(), now=NOW, width=width, tabs=TABS)
            self.assert_fits(out, width, "plain width=%d" % width)

    def test_empty_block_fits(self):
        for width in (187, 118, 93, 61, 40):
            self.assert_fits(render_empty(width=width, live_root="/a/very/long/live/root/path"),
                             width, "empty width=%d" % width)
            self.assert_fits(render_empty(width=width, live_root="x", note="reader failed"),
                             width, "note width=%d" % width)

    def test_colour_output_is_the_plain_text_with_sgr(self):
        # The contract is "same characters, plus SGR": a pipe and a pane must
        # agree. Comparing stripped output (not a byte count) is what shows the
        # style pass never inserts or drops text at a clip boundary.
        for width in (187, 118, 61):
            coloured = render(crowded_workers(), now=NOW, width=width, tabs=TABS, colour=True)
            plain = render(crowded_workers(), now=NOW, width=width, tabs=TABS, colour=False)
            self.assertIn("\x1b", coloured)
            self.assertEqual(visible(coloured), plain)

    def test_colour_off_has_no_escape_byte(self):
        out = render(crowded_workers(), now=NOW, width=110, colour=False)
        self.assertNotIn("\x1b", out)

    def test_ascii_mode_is_pure_ascii(self):
        # Checked per character, not per line: one stray glyph in a 40-column
        # row is exactly what a non-UTF-8 locale turns into mojibake.
        for width in (187, 118, 93, 61, 40):
            out = render(crowded_workers(), now=NOW, width=width, ascii_mode=True)
            for line in lines_of(out):
                for ch in line:
                    self.assertLess(ord(ch), 128, "non-ascii %r in %r" % (ch, line))
            self.assertNotIn("\u2026", out)

    def test_height_bounds_the_block(self):
        # Two live rows plus six finished ones: enough history that a naive
        # clipper would drop the live section to fit. The budget is a ceiling,
        # not an equality — a short block may legitimately be shorter.
        workers = [running(key="r%d" % i, delegation_id="deleg_r%07d" % i,
                           goal="running %d" % i, started=stamp(NOW - timedelta(minutes=20 - i)))
                   for i in range(2)]
        workers += [done(key="d%d" % i, delegation_id="deleg_d%07d" % i, task_index=i,
                         goal="done %d" % i) for i in range(6)]
        for height in (4, 6, 8, 10, 12, 16, 20, 30):
            out = render(workers, now=NOW, width=100, height=height)
            self.assertLessEqual(len(lines_of(out)), height, "height=%d" % height)

    def test_height_never_drops_live_rows_for_history(self):
        # The rule under test: history takes the rows left over, live sections
        # are never trimmed. Both running rows and the RUNNING header must
        # survive even at height=8, where history has no room at all.
        workers = [running(key="r%d" % i, delegation_id="deleg_r%07d" % i,
                           goal="running %d" % i, started=stamp(NOW - timedelta(minutes=20 - i)))
                   for i in range(2)]
        workers += [done(key="d%d" % i, delegation_id="deleg_d%07d" % i, task_index=i,
                         goal="done %d" % i) for i in range(6)]
        for height in (8, 10, 12, 16):
            out = render(workers, now=NOW, width=100, height=height)
            self.assertIn("running 0", out, "height=%d" % height)
            self.assertIn("running 1", out, "height=%d" % height)
            self.assertIn("RUNNING", out, "height=%d" % height)

    def test_height_with_only_history_keeps_the_bound(self):
        # No live section to protect: heights below the header alone (1, 2)
        # exercise the final hard truncation, which is the only place rows are
        # cut without the budget arithmetic having room for them.
        workers = [done(key="d%d" % i, delegation_id="deleg_d%07d" % i, goal="done %d" % i)
                   for i in range(6)]
        for height in (1, 2, 3, 5, 9):
            self.assertLessEqual(len(lines_of(render(workers, now=NOW, width=90, height=height))),
                                 height)

    def test_output_is_deterministic(self):
        # No clock, no set iteration, no dict order in the output: two calls
        # with the same inputs must be byte-identical, which is what lets a
        # caller skip a redraw when nothing changed.
        first = render(crowded_workers(), now=NOW, width=100, tabs=TABS, session_label="hx")
        second = render(crowded_workers(), now=NOW, width=100, tabs=TABS, session_label="hx")
        self.assertEqual(first, second)

    def test_workers_without_timestamps_still_render(self):
        # ``?`` is the honest duration for a worker with no clock evidence; the
        # row must still render rather than vanish.
        w = worker(started=None, last_event=None, state="done", duration_s=None)
        out = render([w], now=NOW, width=90)
        self.assertIn("RECENT", out)
        self.assertIn("?", out)


# --------------------------------------------------------------------------- #
# sections and rows
# --------------------------------------------------------------------------- #


class TestSections(unittest.TestCase):
    """What each row says, and the claims it must not make.

    The negative assertions (``assertNotIn``) are half of every test here: a
    section that appears when it has nothing to show is as wrong as a missing
    one, because it teaches the reader to ignore the header.
    """

    def test_running_section_shows_task_and_meta(self):
        # A running row is two lines: the goal (the worker's name), then model,
        # elapsed time, tool count. "3 tools" also pins the plural rule.
        out = render([running()], now=NOW, width=110)
        self.assertIn("RUNNING", out)
        self.assertIn("Implement the renderer and unit tests", out)
        self.assertIn("42s", out)
        self.assertIn("deepseek-v4.1-flash", out)
        self.assertIn("3 tools", out)
        self.assertNotIn("NEEDS ATTENTION", out)
        self.assertNotIn("RECENT", out)

    def test_nothing_running_line(self):
        # An empty live section is stated, not omitted: silence would read as
        # "the reader is broken".
        out = render([done()], now=NOW, width=110)
        self.assertIn("Nothing running.", out)
        self.assertNotIn("RUNNING", out)
        self.assertIn("RECENT", out)

    def test_attention_section_only_when_present(self):
        self.assertNotIn("NEEDS ATTENTION", render([running(), done()], now=NOW, width=110))
        self.assertIn("NEEDS ATTENTION", render([running(), failed()], now=NOW, width=110))

    def test_recent_section_absent_when_no_history(self):
        self.assertNotIn("RECENT", render([running(), failed()], now=NOW, width=110))

    def test_attention_row_states_how_the_run_ended(self):
        # "run ended", not "task failed": the renderer knows how the agent run
        # stopped and nothing about whether the work is any good.
        out = render([failed()], now=NOW, width=110)
        self.assertIn("run ended: max_iterations", out)

    def test_attention_row_quotes_last_report(self):
        out = render([failed(last_message="I could not finish: permissions.")], now=NOW, width=110)
        self.assertIn("last report: I could not finish: permissions.", out)

    def test_attention_row_shows_error_message_not_json(self):
        # ``labels.error_text`` extracts the named field; the JSON framing is
        # in the transcript file and would only widen the row.
        out = render([failed(last_error='{"output": "", "error": "permission denied"}')],
                     now=NOW, width=110)
        self.assertIn("permission denied", out)
        self.assertNotIn('"error"', out)

    def test_running_row_shows_tab_when_mapped(self):
        # ``tabs`` is keyed by worker key, not delegation id: siblings of one
        # delegation have separate tabs.
        out = render([running(), done()], now=NOW, width=110, tabs={"deleg_6cfc92ad#0": 3,
                                                                  "deleg_ccccdddd#2": 4})
        self.assertIn("tab 3", out)
        self.assertIn("tab 4", out)

    def test_no_tab_label_without_mapping(self):
        # No mapping means no claim; "tab ?" would assert a tab that may not
        # exist. The trailing space keeps this from matching "tab 3" prose.
        out = render([running(), done()], now=NOW, width=110)
        self.assertNotIn("tab ", out)

    def test_recent_row_shows_summary_and_age(self):
        out = render([done()], now=NOW, width=110)
        self.assertIn("Write the state reader tests", out)
        self.assertIn("All eight tests pass.", out)
        self.assertIn("55m ago", out)

    def test_stalled_row_says_nothing_came_out(self):
        # The second line of a stalled row reports the silence instead of the
        # last call: the call is old news by definition.
        out = render([stalled()], now=NOW, width=110)
        self.assertIn("no output for", out)

    def test_unknown_state_is_not_invented_as_a_known_badge(self):
        w = worker(key="odd", state="some-unknown-state", last_event=None, started=None)
        out = render([w], now=NOW, width=110)
        # It is not a failure and not running: it lands in history, with no
        # FAILED/RUNNING claim this module cannot support.
        self.assertIn("RECENT", out)
        self.assertNotIn("FAILED", out)
        self.assertNotIn("RUNNING", out)
        # The label keeps the first five characters so an unexpected long
        # state cannot widen a row; the glyph/word table still says UNKNOWN.
        self.assertEqual(render_mod._state_label(w, NOW)[:5], "some-")

    def test_review_note_only_for_recorded_review(self):
        # Only ``status == "recorded"`` produces a note. A failed or
        # unreviewed entry, or no entry at all, prints nothing: the absence of
        # a review is not a verdict and must not read as one.
        recorded = done(key="rev-manual", review={"status": "recorded"})
        self.assertIn("review: recorded (manual)", render([recorded], now=NOW, width=110))

        modelled = done(key="rev-model",
                        review={"status": "recorded", "reviewer_model": "z-ai/glm-5.3-flash"})
        self.assertIn("review: recorded (glm-5.3-flash)", render([modelled], now=NOW, width=110))

        for review in ({}, {"status": "failed"}, {"status": "unreviewed"}, None):
            out = render([done(key="rev-none", review=review)], now=NOW, width=110)
            self.assertNotIn("review:", out, repr(review))

    def test_review_note_not_claimed_when_unreviewed(self):
        out = render([running(review={})], now=NOW, width=110)
        self.assertNotIn("recorded", out)

    def test_goal_whitespace_is_collapsed(self):
        # A goal is untrusted text from the parent; control characters must not
        # reach the pane as layout.
        w = running(goal="Fix\tstate\nreader   and\ttests")
        out = render([w], now=NOW, width=110)
        self.assertIn("Fix state reader and tests", out)
        self.assertNotIn("\t", out)

    def test_object_with_no_attributes_does_not_raise(self):
        # Every attribute read goes through ``getattr`` with a default, so a
        # stub with nothing at all is a valid (if uninformative) worker.
        out = render([SimpleNamespace()], now=NOW, width=90)
        self.assertIn("HERMES AGENTS", out)


# --------------------------------------------------------------------------- #
# empty state, note, and the reader-failure path
# --------------------------------------------------------------------------- #


class TestEmpty(unittest.TestCase):
    """The "nothing to show" block, and the failure path that looks like it.

    The distinction is the whole point: an empty list from a successful read
    means "no delegations yet"; a note means the read itself failed. Rendering
    the second as the first would report the absence of evidence as evidence of
    absence.
    """

    def test_default_empty_explains_and_shows_root(self):
        out = render_empty(width=110, live_root="/tmp/hermes/live")
        self.assertIn("No delegations yet.", out)
        self.assertIn("/tmp/hermes/live", out)

    def test_empty_root_unknown(self):
        # No root is printed as "(unknown)" rather than a guess at a path.
        self.assertIn("(unknown)", render_empty(width=90))

    def test_render_without_workers_shows_empty(self):
        self.assertIn("No delegations yet.", render([], width=110, live_root="/tmp/live"))

    def test_note_is_a_reader_failure_not_an_empty_workspace(self):
        out = render_empty(width=110, live_root="/tmp/live", note="reader exploded")
        self.assertIn("reader exploded", out)
        self.assertIn("reader failure, not an empty workspace", out)
        self.assertNotIn("No delegations yet.", out)

    def test_note_overrides_a_worker_list(self):
        # ``render`` checks the note before the worker list: when the reader
        # raised, the list it produced is not trustworthy either.
        out = render([running()], now=NOW, width=110, note="reader exploded")
        self.assertIn("reader failure, not an empty workspace", out)
        self.assertNotIn("No delegations yet.", out)
        self.assertNotIn("RUNNING", out)

    def test_note_never_says_no_delegations_even_when_clipped(self):
        # 40 columns is narrower than either headline; the failure headline is
        # bold and comes first, so the "No delegations yet." branch cannot be
        # reached by wrapping or clipping.
        for width in (40, 61, 110):
            out = render([], width=width, note="boom")
            self.assertNotIn("No delegations yet.", out, "width=%d" % width)


# --------------------------------------------------------------------------- #
# _state_label
# --------------------------------------------------------------------------- #


class TestStateLabel(unittest.TestCase):
    """The raw-state -> display-state map, including the stall derivation.

    ``_state_label`` is private but is the only place a stall is decided, so its
    edges are pinned here directly; ``classify`` and ``summary`` both depend on
    the answer.
    """

    def label(self, **kw):
        return render_mod._state_label(worker(**kw), NOW)

    def test_running_maps_to_run(self):
        self.assertEqual(self.label(state="running"), "run")
        self.assertEqual(self.label(state="active"), "run")

    def test_stall_after_threshold(self):
        # Strictly greater than STALL_AFTER_S: one second past the threshold is
        # silent, exactly at it is not (next test).
        edge = NOW - timedelta(seconds=STALL_AFTER_S + 1)
        self.assertEqual(self.label(state="running", started=None, last_event=stamp(edge)), "stall")

    def test_not_stall_at_threshold(self):
        edge = NOW - timedelta(seconds=STALL_AFTER_S)
        self.assertEqual(self.label(state="running", started=None, last_event=stamp(edge)), "run")

    def test_future_timestamp_is_clamped_not_stalled(self):
        # A transcript carries only a time of day, joined to the manifest date;
        # across midnight that lands in the future. The label clamps to `now`
        # instead of reporting a negative silence, which would read as healthy
        # forever — and instead of reporting hours of silence, which is a lie.
        future = NOW + timedelta(hours=2)
        self.assertEqual(self.label(state="running", started=None, last_event=stamp(future)), "run")

    def test_done_and_fail_mappings(self):
        self.assertEqual(self.label(state="done", last_event=None, started=None), "done")
        self.assertEqual(self.label(state="completed", last_event=None, started=None), "done")
        for state in ("failed", "error", "interrupted", "crashed"):
            self.assertEqual(self.label(state=state, last_event=None, started=None), "fail", state)

    def test_explicit_stalled_state(self):
        # Hermes has no "stalled" state, but a manifest or an older row might;
        # it is honoured explicitly as well as derived.
        self.assertEqual(self.label(state="stalled", started=None, last_event=None), "stall")

    def test_missing_state_is_unknown(self):
        self.assertEqual(self.label(state=""), "other")

    def test_finished_worker_never_stalls(self):
        # Stall is reachable only from the running branch: a finished run with
        # an old last event is done, not silent.
        self.assertEqual(self.label(state="done", started=None,
                                    last_event=stamp(NOW - timedelta(hours=5))), "done")


if __name__ == "__main__":
    unittest.main()
