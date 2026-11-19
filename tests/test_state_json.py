"""Unit tests for the state fields that come from Hermes' JSON columns.

These pin two facts established by reading the installed Hermes source and the
live ``state.db`` schema: the per-task transcript map lives in
``async_delegations.task_json`` (there is no per-task column), and the live
states include ``stalling`` and ``finalizing``, which are not verdicts.

Why these two facts need a test of their own: both are easy to "fix" by
guessing. Rebuilding a transcript path from the ``task-<n>.log`` naming
convention works on most delegations and silently mislabels the rest; treating
``stalling`` or ``finalizing`` as terminal freezes a worker's row at a state it
has not reached. So each behaviour is asserted against a fixture that carries
the *other* possibility as well — a ``transcript-<n>.txt`` file that only the
map knows about, and a real terminal state that must still win.

The database here has a ``task_json`` column, which ``tests/test_state.py``'s
simpler schema omits: this module's subject is exactly that column.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hermes_tmux import state

HEADER = """=== Hermes subagent live transcript ===
delegation: {did}   task: {index}
goal: {goal}
started: 2026-11-12 09:00:00
(append-only; streams while the subagent runs - tail -f me)
========================================
"""


class TaskJsonTest(unittest.TestCase):
    """``task_json.task_transcripts`` as the authoritative index -> path map.

    The fixtures deliberately name the log ``transcript-<n>.txt``, which the
    ``task-<n>.log`` convention would never find. That makes "the map wins" and
    "the convention is only a fallback" two separable, observable facts.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.live = self.home / "cache" / "delegation" / "live" / "deleg_json0001"
        self.live.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _manifest(self, tasks):
        (self.live / "manifest.json").write_text(json.dumps({
            "delegation_id": "deleg_json0001",
            "started": "2026-11-12 09:00:00",
            "task_count": len(tasks),
            "model": "deepseek/deepseek-v4.1-flash-1017",
            "tasks": tasks,
        }), encoding="utf-8")

    def _db(self, state_value, task_json=None, result_json=None):
        """One ``async_delegations`` row, with the ``task_json`` column present.

        ``DROP TABLE IF EXISTS`` lets a test rewrite the row's state between
        loads without a second fixture; ``state.load_workers`` is called fresh
        after each write.
        """
        connection = sqlite3.connect(str(self.home / "state.db"))
        try:
            connection.execute("DROP TABLE IF EXISTS async_delegations")
            connection.execute(
                "CREATE TABLE async_delegations (delegation_id TEXT PRIMARY KEY,"
                " parent_session_id TEXT, state TEXT, dispatched_at REAL,"
                " completed_at REAL, event_json TEXT, result_json TEXT,"
                " task_json TEXT)")
            connection.execute(
                "INSERT INTO async_delegations VALUES (?,?,?,?,?,?,?,?)",
                ("deleg_json0001", "local-x", state_value, 1.0, None, None,
                 result_json, task_json))
            connection.commit()
        finally:
            connection.close()

    def _write_log(self, index, extra=""):
        # NOT task-<n>.log: only the task_json map can point here.
        path = self.live / ("transcript-%d.txt" % index)   # NOT task-<n>.log
        path.write_text(HEADER.format(did="deleg_json0001", index=index, goal="g")
                        + extra, encoding="utf-8")
        return path

    def test_transcript_map_wins_over_the_naming_convention(self):
        # The manifest entry has no ``log`` key, so the only way to reach this
        # file is the map. A convention-based guess would find nothing and the
        # worker would look like it had no transcript at all.
        log = self._write_log(0, "09:00:01 tool      | -> terminal(ls)\n")
        self._manifest([{"index": 0, "goal": "g", "status": "running"}])
        self._db("running", task_json=json.dumps(
            {"task_transcripts": {"0": str(log)}}))

        workers = state.load_workers(self.home)
        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0].log_path, str(log))

    def test_unknown_transcript_map_falls_back_to_task_n_log(self):
        # An empty map is "the map does not know", not "there is no log": the
        # sibling convention still has to be tried.
        fallback = self.live / "task-0.log"
        fallback.write_text(HEADER.format(did="deleg_json0001", index=0, goal="g"))
        self._manifest([{"index": 0, "goal": "g", "status": "running"}])
        self._db("running", task_json=json.dumps({"task_transcripts": {}}))

        workers = state.load_workers(self.home)
        self.assertEqual(workers[0].log_path, str(fallback))

    def test_stalling_and_finalizing_are_not_terminal(self):
        # Both are in Hermes' own live-state set. Read as verdicts, either one
        # would freeze the row at a state the task has not reached, and the
        # manifest's "running" (plus the display stall label) would never show.
        log = self._write_log(0, "09:00:01 tool      | -> terminal(ls)\n")
        self._manifest([{"index": 0, "goal": "g", "status": "running"}])
        for db_state in ("stalling", "finalizing"):
            self._db(db_state, task_json=json.dumps(
                {"task_transcripts": {"0": str(log)}}))
            workers = state.load_workers(self.home)
            self.assertEqual(
                workers[0].state, "running",
                "%r must not be shown as a terminal verdict" % db_state)

    def test_real_terminal_state_still_wins(self):
        # The negative control for the test above: the non-terminal rule must
        # not have been implemented by ignoring the DB column altogether.
        log = self._write_log(0, "09:00:01 tool      | -> terminal(ls)\n")
        self._manifest([{"index": 0, "goal": "g", "status": "running"}])
        self._db("error", task_json=json.dumps({"task_transcripts": {"0": str(log)}}))
        workers = state.load_workers(self.home)
        self.assertEqual(workers[0].state, "error")

    def test_garbage_task_json_is_tolerated(self):
        # ``task_json`` is a JSON *string* in the column; a truncated one must
        # discard that row's map, not the whole read. With the map unusable, the
        # convention path is what remains.
        self._manifest([{"index": 0, "goal": "g", "status": "running"}])
        (self.live / "task-0.log").write_text(
            HEADER.format(did="deleg_json0001", index=0, goal="g"))
        self._db("running", task_json="{not json")
        workers = state.load_workers(self.home)
        self.assertEqual(len(workers), 1)
        self.assertTrue(workers[0].log_path.endswith("task-0.log"))


if __name__ == "__main__":
    unittest.main()
