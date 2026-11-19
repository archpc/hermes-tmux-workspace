"""The coordinator must not revert a tab record published while it was working.

``reconcile_tabs`` reads the layout, spends up to seconds creating windows, and
then writes. ``hermes-tmux open`` can publish a pinned tab in that window of
time. Writing the whole ``tabs`` map back would drop that record: the window
would exist with nothing pointing at it, and the next pass would open a second
window for the same worker. (Found by independent review.)

No tmux here: the tmux layer is replaced by a small in-memory fake so the race
can be placed exactly, inside ``new_window``.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_tmux import watch


class MergeRule(unittest.TestCase):
    """``_merge``'s rules, one call each: add, change, remove, unchanged, plus
    the dismissal-blocked and both-sides-added cases."""
    def test_key_added_by_someone_else_survives(self):
        """A key this pass never saw is not its business: it stays."""
        current = {"a": 1, "theirs": 9}
        watch._merge(current, {"a": 1}, {"a": 1, "mine": 2})
        self.assertEqual(current, {"a": 1, "theirs": 9, "mine": 2})

    def test_changed_key_is_written(self):
        """A value this pass changed is written; other writers move whole records."""
        current = {"a": {"v": 1}}
        watch._merge(current, {"a": {"v": 1}}, {"a": {"v": 2}})
        self.assertEqual(current, {"a": {"v": 2}})

    def test_removed_key_is_removed_only_if_untouched_since(self):
        """A removal lands only when ``current`` still holds the value the pass saw.

        Second case is the control: ``open`` re-recorded the tab, so the pass's removal
        must lose to the newer record.
        """
        current = {"a": 1, "b": 1}
        watch._merge(current, {"a": 1, "b": 1}, {})
        self.assertEqual(current, {})
        current = {"a": "reopened"}
        watch._merge(current, {"a": 1}, {})
        self.assertEqual(current, {"a": "reopened"},
                         "a record replaced since the snapshot must not be deleted")

    def test_unchanged_key_removed_by_someone_else_stays_removed(self):
        """A key the pass did not touch is not resurrected from its snapshot."""
        current = {}
        watch._merge(current, {"a": 1}, {"a": 1})
        self.assertEqual(current, {})

    def test_dismissal_is_not_added_for_a_worker_with_a_live_tab(self):
        """``blocked`` keys are not added: a worker with a tab again is not dismissed.
        """
        current = {}
        watch._merge(current, {}, {"w": 5.0}, blocked={"w": {"pane": "%9"}})
        self.assertEqual(current, {})

    def test_a_key_both_sides_added_is_reported_and_the_other_record_stands(self):
        """A first-tab collision keeps the other record and names the loser.

        The loser is returned so the caller can close the pane it built; dropping it
        silently is what would leave two windows for one worker.
        """
        current = {"w": {"pane": "%7", "pinned": True}}
        lost = watch._merge(current, {}, {"w": {"pane": "%8"}})
        self.assertEqual(lost, ["w"])
        self.assertEqual(current, {"w": {"pane": "%7", "pinned": True}})


class FakeTmux:
    """Just enough of ``tmuxio`` for ``reconcile_tabs``; windows are dict rows."""

    def __init__(self):
        """The fake's whole world: window id -> pane id, plus an id counter.

        ``on_new_window`` is a one-shot hook, consumed by the first ``new_window``
        call, which is how a test places an event inside the pass.
        """
        self.windows = {}        # window id -> pane id
        self.count = 0
        self.on_new_window = None

    def current_session_id(self):
        """One session, ``$0``; this fake never models groups."""
        return "$0"

    def group_sessions(self, session_id):
        """A session is its own group: no grouped sessions here."""
        return [session_id]      # no grouped sessions in this model

    def pane_exists(self, pane_id):
        """Ownership and existence are the same question in this fake."""
        return pane_id in self.windows.values()

    def owns_pane(self, pane_id):
        """A pane is ours exactly when a window holds it."""
        return pane_id in self.windows.values()

    def owns_window(self, window_id):
        """A window is ours exactly when it is in the map."""
        return window_id in self.windows

    def window_exists(self, window_id):
        """A window is ours exactly when it is in the map."""
        return window_id in self.windows

    def format_of(self, target, fmt):
        """Only ``#{window_id}`` is modelled.

        Anything else answers '', which is how "nobody attached" is spelled, so no
        tab is ever in view in this fake.
        """
        if fmt == "#{window_id}":
            return next((w for w, p in self.windows.items() if p == target), "")
        return ""                # nobody attached: no window is in view

    def close_pane(self, pane_id):
        """Delete the window holding the pane, so the window dies with its last pane."""
        for window_id, pane in list(self.windows.items()):
            if pane == pane_id:
                del self.windows[window_id]
                return True, ""
        return False, "gone"

    def free_window_name(self, session, base):
        """No naming collisions are modelled; the caller's suffix rule is not under test.
        """
        return base

    def new_window(self, session, name, command=None, cwd=None, env=None):
        """Mint increasing ids and fire the one-shot hook.

        The hook is where a concurrent ``open`` is placed: inside the pass, between
        its read of the layout and its write.
        """
        self.count += 1
        window_id, pane_id = "@%d" % self.count, "%%%d" % self.count
        self.windows[window_id] = pane_id
        if self.on_new_window:
            hook, self.on_new_window = self.on_new_window, None
            hook()
        return window_id

    def wait_for_panes(self, window_id, timeout=2.0):
        """The pane of a window that exists, ``[]`` otherwise, as tmux would answer."""
        return [{"pane_id": self.windows[window_id]}] if window_id in self.windows else []

    def window_option(self, window_id, option, value):
        """Decoration is accepted and not recorded.

        Option writes are only asserted where a test wraps this method.
        """
        return True

    def pane_option(self, pane_id, option, value):
        """Accepted and not recorded, for the same reason as ``window_option``."""
        return True

    def list_windows(self, session):
        """All windows are ours: this fake never models a foreign window."""
        return [{"window_id": w, "index": str(i), "name": "", "ours": True}
                for i, w in enumerate(self.windows, 1)]


def _worker(key, goal):
    """A SimpleNamespace, not a real Worker: only these attributes are read."""
    deleg, index = key.split("#")
    return SimpleNamespace(
        key=key, delegation_id=deleg, task_index=int(index), goal=goal,
        model="m/x", state="running", exit_reason=None,
        started=time.strftime("%Y-%m-%d %H:%M:%S"),
        last_event=time.strftime("%Y-%m-%d %H:%M:%S"),
        delegation_started=time.strftime("%Y-%m-%d %H:%M:%S"),
        duration_s=None, tool_count=0, log_path="/dev/null")


class OpenDuringReconcile(unittest.TestCase):
    """Reconcile against the fake with a real layout file, so the merge is genuine."""
    def setUp(self):
        """Replace the tmux module on ``watch`` only.

        ``watch`` reaches tmux through that module global, so the fake is what the pass
        sees; ``decorate_window`` and ``_pane_env`` are stubbed because they would
        otherwise shell out.
        """
        self.home = Path(tempfile.mkdtemp(prefix="hx_merge_"))
        self.fake = FakeTmux()
        self._saved = (watch.tmuxio, watch.decorate_window, watch._pane_env)
        watch.tmuxio = self.fake
        watch.decorate_window = lambda *a, **k: None
        watch._pane_env = lambda: {}

    def tearDown(self):
        """Restore all three globals.

        A leaked fake would make later tests in this module reconcile against
        nothing, and the failure would point at the wrong test.
        """
        watch.tmuxio, watch.decorate_window, watch._pane_env = self._saved
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    def test_a_tab_published_mid_pass_is_kept_and_not_duplicated(self):
        """A tab published mid-pass survives, and is not doubled.

        The hook fires once, inside the pass's first ``new_window``: exactly the window
        between its read and its write. The second pass is the control that matters —
        if the record had been reverted, it would open a second window for the same
        worker, which the window-set comparison catches.
        """
        key = "racekey"
        watch.update_layout(key, lambda cur: dict(cur, session_id="$0", created=time.time()),
                            self.home)
        running = _worker("deleg_run00001#0", "Build the running thing now")
        old = _worker("deleg_old00001#0", "Read an old transcript again")
        old.state = "completed"
        old.last_event = "2020-01-01 00:00:00"      # long before the workspace

        def publish_like_open():
            # What cli.cmd_open does: its own window, then a locked update.
            """What ``cli.cmd_open`` does: build a window, publish it under the lock."""
            window_id = self.fake.new_window("$0", "old")
            record = {"window": window_id, "pane": self.fake.windows[window_id],
                      "opened": time.time(), "pinned": True, "seen": time.time()}
            watch.update_layout(
                key, lambda cur: (cur.setdefault("tabs", {}).__setitem__(old.key, record), cur)[1],
                self.home)

        self.fake.on_new_window = publish_like_open
        layout = watch.reconcile_tabs(key, [running, old], self.home)

        self.assertIn(running.key, layout["tabs"])
        self.assertIn(old.key, layout["tabs"],
                      "the coordinator's write reverted a tab published during its pass")
        self.assertTrue(layout["tabs"][old.key].get("pinned"))
        windows_after_first = dict(self.fake.windows)

        layout = watch.reconcile_tabs(key, [running, old], self.home)
        self.assertEqual(self.fake.windows, windows_after_first,
                         "a second window was opened for a worker that already had one")
        self.assertEqual(set(layout["tabs"]), {running.key, old.key})

    def test_open_and_the_pass_creating_the_same_first_tab_leaves_one_window(self):
        """Both sides create the worker's FIRST tab; the reader's record wins.

        ``_merge`` reports the collision, and the pass must then close the pane it
        made. The assertion is on the window set, not the count: one window, holding
        exactly the recorded pane.
        """
        key = "samekey"
        watch.update_layout(key, lambda cur: dict(cur, session_id="$0", created=time.time()),
                            self.home)
        running = _worker("deleg_sam00001#0", "Show one worker exactly once")

        def publish_like_open():
            """``cmd_open``'s shape for the same worker key the pass is about to create.
            """
            window_id = self.fake.new_window("$0", "same")
            record = {"window": window_id, "pane": self.fake.windows[window_id],
                      "opened": time.time(), "pinned": True, "seen": time.time()}
            watch.update_layout(
                key, lambda cur: (cur.setdefault("tabs", {}).__setitem__(running.key, record),
                                  cur)[1], self.home)

        self.fake.on_new_window = publish_like_open
        layout = watch.reconcile_tabs(key, [running], self.home)
        self.assertTrue(layout["tabs"][running.key].get("pinned"),
                        "the reader's tab must be the one that is recorded")
        self.assertEqual(list(self.fake.windows.values()),
                         [layout["tabs"][running.key]["pane"]],
                         "the pass left its own duplicate window standing")

    def test_one_failed_tmux_answer_does_not_forget_a_tab(self):
        """One bad tmux answer must not dismiss live work.

        ``owns_pane`` answers False once and truthfully after; ``_twice`` turns that
        into a retry rather than a decision. The second half replaces ``format_of``
        with '' — "no answer" — which must leave the tab and its window untouched
        instead of being read as a move.
        """
        key = "flakykey"
        watch.update_layout(key, lambda cur: dict(cur, session_id="$0", created=time.time()),
                            self.home)
        running = _worker("deleg_flk00001#0", "Keep a healthy tab through a hiccup")
        layout = watch.reconcile_tabs(key, [running], self.home)
        tab = dict(layout["tabs"][running.key])
        real = self.fake.owns_pane
        answers = [False]                      # first call fails, the retry is true
        self.fake.owns_pane = lambda pane: answers.pop(0) if answers else real(pane)
        layout = watch.reconcile_tabs(key, [running], self.home)
        self.assertEqual(layout["tabs"][running.key]["pane"], tab["pane"])
        self.assertNotIn(running.key, layout.get("dismissed", {}))
        # tmux not answering the window read at all: tab untouched, nothing closed.
        self.fake.format_of = lambda target, fmt: ""
        layout = watch.reconcile_tabs(key, [running], self.home)
        self.assertEqual(layout["tabs"][running.key]["pane"], tab["pane"])
        self.assertIn(tab["window"], self.fake.windows)

    def test_a_pane_moved_out_of_its_window_is_forgotten_not_decorated(self):
        """A pane the reader moved belongs to them: forget the tab, write nothing.

        The window option writer is wrapped to record calls; the assertion that no
        option was written to ``@user`` is the point, since decorating someone else's
        window is the failure this path exists to avoid.
        """
        key = "movekey"
        watch.update_layout(key, lambda cur: dict(cur, session_id="$0", created=time.time()),
                            self.home)
        running = _worker("deleg_mov00001#0", "Follow a worker the reader rearranged")
        layout = watch.reconcile_tabs(key, [running], self.home)
        tab = layout["tabs"][running.key]
        # The reader joins our pane into a window of their own.
        del self.fake.windows[tab["window"]]
        self.fake.windows["@user"] = tab["pane"]
        writes = []
        self.fake.window_option = lambda w, o, v: writes.append((w, o)) or True
        self.fake.owns_window = lambda w: w != "@user" and w in self.fake.windows

        layout = watch.reconcile_tabs(key, [running], self.home)
        self.assertNotIn(running.key, layout.get("tabs", {}))
        self.assertIn(running.key, layout.get("dismissed", {}))
        self.assertIn("@user", self.fake.windows, "the moved pane was closed")
        self.assertEqual([w for w in writes if w[0] == "@user"], [],
                         "an option was written to a window that is not ours")


class FreshProfileIsWatched(unittest.TestCase):
    """The first delegation on a profile creates the live root; it must be seen."""

    def test_nearest_existing_ancestor_of_a_missing_live_root_is_watched(self):
        """Watch the nearest existing ancestor, then ``live/`` once it exists.

        Each step creates one more level so the expected directory moves outward to
        inward; a fixed ``live/`` path would never be registered on a fresh profile
        and the first delegation would be invisible until the slow poll.
        """
        home = Path(tempfile.mkdtemp(prefix="hx_fresh_"))
        try:
            (home / "cache").mkdir()
            dirs, _files = watch.collect_paths(home)
            self.assertIn(home / "cache", dirs)
            (home / "cache" / "delegation").mkdir()
            dirs, _files = watch.collect_paths(home)
            self.assertIn(home / "cache" / "delegation", dirs)
            live = home / "cache" / "delegation" / "live"
            live.mkdir()
            dirs, _files = watch.collect_paths(home)
            self.assertIn(live, dirs)
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
