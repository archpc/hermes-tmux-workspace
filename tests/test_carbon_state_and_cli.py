"""Regression tests for delegation state reconciliation, manifest retention,
stat-cached log/manifest reads, and CLI history/outside-tmux commands.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes_tmux import state

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable

HEADER = (
    "=== Hermes subagent live transcript ===\n"
    "delegation: {did}   task: {idx}\n"
    "goal: {goal}\n"
    "started: {started}\n"
    "========================================\n"
)


class StateAndCliRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hx_c1c4_")
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_deleg(self, did: str, started: str, tasks_spec: list, completed: str = None) -> Path:
        d = self.home / "cache" / "delegation" / "live" / did
        d.mkdir(parents=True, exist_ok=True)
        tasks = []
        for i, spec in enumerate(tasks_spec):
            lp = d / ("task-%d.log" % i)
            lines = [HEADER.format(did=did, idx=i, goal=spec["goal"], started=started)]
            lines.extend(ev + "\n" for ev in spec.get("events", []))
            lp.write_text("".join(lines), encoding="utf-8")
            entry = {"index": i, "goal": spec["goal"], "log": str(lp), "status": spec["status"]}
            if "exit_reason" in spec:
                entry["exit_reason"] = spec["exit_reason"]
            tasks.append(entry)
        manifest = {
            "delegation_id": did,
            "started": started,
            "task_count": len(tasks),
            "model": "deepseek/v4",
            "tasks": tasks,
        }
        if completed:
            manifest["completed"] = completed
        (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return d

    def _init_db(self, rows: list) -> None:
        con = sqlite3.connect(str(self.home / "state.db"))
        con.execute(
            "CREATE TABLE IF NOT EXISTS async_delegations ("
            "delegation_id TEXT PRIMARY KEY, parent_session_id TEXT, state TEXT, "
            "dispatched_at REAL, completed_at REAL, event_json TEXT, result_json TEXT, task_json TEXT)"
        )
        for r in rows:
            con.execute(
                "INSERT OR REPLACE INTO async_delegations VALUES (?,?,?,?,?,?,?,?)",
                (
                    r["delegation_id"], "s1", r["state"], 1.0, 2.0,
                    json.dumps(r.get("event", {})),
                    json.dumps(r.get("result", {})),
                    json.dumps(r.get("task", {})),
                ),
            )
        con.commit()
        con.close()

    def test_multitask_batch_completed_db_row_preserves_failed_and_interrupted_tasks(self) -> None:
        """Hermes sets async_delegations.state='completed' when a multi-task batch
        finishes delivering mixed per-task results. Failed/interrupted tasks must
        stay failed/interrupted rather than being masked as completed.
        """
        self._write_deleg(
            "deleg_mix00001", "2026-11-13 22:00:00",
            [
                {"goal": "Worker A", "status": "interrupted", "exit_reason": "interrupted",
                 "events": ["22:00:05 final     | end status=interrupted exit_reason=interrupted"]},
                {"goal": "Worker B", "status": "completed", "exit_reason": "completed",
                 "events": ["22:00:10 final     | status=completed duration=5.0s summary: ok"]},
                {"goal": "Worker C", "status": "error", "exit_reason": "provider_error",
                 "events": ["22:00:09 final     | end status=error exit_reason=provider_error"]},
            ],
            completed="2026-11-13 22:00:10",
        )
        self._init_db([{
            "delegation_id": "deleg_mix00001",
            "state": "completed",
            "result": {
                "results": [
                    {"task_index": 0, "status": "interrupted", "exit_reason": "interrupted"},
                    {"task_index": 1, "status": "completed", "exit_reason": "completed"},
                    {"task_index": 2, "status": "error", "exit_reason": "provider_error"},
                ]
            },
        }])
        by_idx = {w.task_index: w for w in state.load_workers(self.home)}
        self.assertEqual(by_idx[0].state, "interrupted")
        self.assertEqual(by_idx[1].state, "completed")
        self.assertEqual(by_idx[2].state, "error")

    def test_multitask_batch_completed_without_result_json_still_respects_manifest_failure(self) -> None:
        self._write_deleg(
            "deleg_mix00002", "2026-11-13 22:01:00",
            [
                {"goal": "Worker A", "status": "interrupted", "exit_reason": "interrupted",
                 "events": ["22:01:05 final     | end status=interrupted"]},
                {"goal": "Worker B", "status": "completed", "exit_reason": "completed",
                 "events": ["22:01:10 final     | status=completed duration=5.0s"]},
            ],
            completed="2026-11-13 22:01:10",
        )
        self._init_db([{"delegation_id": "deleg_mix00002", "state": "completed"}])
        by_idx = {w.task_index: w for w in state.load_workers(self.home)}
        self.assertEqual(by_idx[0].state, "interrupted")
        self.assertEqual(by_idx[1].state, "completed")

    def test_independent_completion_suffixed_unit_rows_reconcile_per_task(self) -> None:
        self._write_deleg(
            "deleg_unit0001", "2026-11-13 22:02:00",
            [
                {"goal": "Unit 0", "status": "running", "events": ["22:02:01 user      | kickoff: Unit 0"]},
                {"goal": "Unit 1", "status": "running", "events": ["22:02:01 user      | kickoff: Unit 1"]},
            ],
        )
        self._init_db([
            {"delegation_id": "deleg_unit0001-1", "state": "error",
             "result": {"status": "error", "exit_reason": "timeout"},
             "task": {"task_indexes": [0]}},
            {"delegation_id": "deleg_unit0001-2", "state": "running",
             "task": {"task_indexes": [1]}},
        ])
        by_idx = {w.task_index: w for w in state.load_workers(self.home)}
        self.assertEqual(by_idx[0].state, "error")
        self.assertEqual(by_idx[0].exit_reason, "timeout")
        self.assertEqual(by_idx[1].state, "running")

    def test_oldest_running_worker_survives_more_than_40_newer_completed_delegations(self) -> None:
        self._write_deleg(
            "deleg_morning0", "2026-11-13 08:00:00",
            [{"goal": "Long running morning audit", "status": "running",
              "events": ["08:00:01 tool      | -> terminal(pytest)"]}],
        )
        for i in range(42):
            self._write_deleg(
                "deleg_fast%04d" % i, "2026-11-13 12:%02d:00" % (i % 60),
                [
                    {"goal": "Fast %dA" % i, "status": "completed",
                     "events": ["12:00:02 final     | status=completed duration=1.0s"]},
                    {"goal": "Fast %dB" % i, "status": "completed",
                     "events": ["12:00:02 final     | status=completed duration=1.0s"]},
                ],
                completed="2026-11-13 12:00:02",
            )

        default_workers = state.load_workers(self.home)
        self.assertEqual(len(default_workers), 40)
        self.assertEqual(default_workers[0].key, "deleg_morning0#0")
        self.assertEqual(default_workers[0].state, "running")

        # status --all must return all 85 workers, not cap at 40
        env = dict(os.environ, HERMES_HOME=str(self.home), HERMES_TMUX="1",
                   PYTHONPATH=str(REPO), NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1")
        st_all = subprocess.run(
            [PY, "-B", "-m", "hermes_tmux", "status", "--all", "--json"],
            env=env, capture_output=True, text=True, check=True,
        )
        self.assertEqual(len(json.loads(st_all.stdout)["workers"]), 85)

        # review record must accept a worker older than the 40th
        rev = subprocess.run(
            [PY, "-B", "-m", "hermes_tmux", "review", "record",
             "--delegation", "deleg_fast0000", "--task", "0"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(rev.returncode, 0, rev.stdout + rev.stderr)

    def test_rerender_outside_tmux_does_not_create_nosock_unknown_dir(self) -> None:
        self._write_deleg(
            "deleg_r0000001", "2026-11-13 22:00:00",
            [{"goal": "Rerender check", "status": "completed",
              "events": ["22:00:01 final     | status=completed duration=1.0s"]}],
            completed="2026-11-13 22:00:01",
        )
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env.update(HERMES_HOME=str(self.home), HERMES_TMUX="1",
                   PYTHONPATH=str(REPO), NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1")
        res = subprocess.run(
            [PY, "-B", "-m", "hermes_tmux", "rerender"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertFalse((self.home / "cache" / "hermes-tmux" / "sessions" / "nosock-unknown").exists())

    def test_parse_log_and_read_manifest_stat_cache_avoids_reread_and_invalidates_on_write(self) -> None:
        d = self._write_deleg(
            "deleg_cache001", "2026-11-13 22:00:00",
            [{"goal": "Cache test", "status": "running",
              "events": ["22:00:01 tool      | -> read_file(a)"]}],
        )
        lp = d / "task-0.log"
        mp = d / "manifest.json"

        first_log = state.parse_log(lp)
        self.assertEqual(first_log["tool_count"], 1)
        first_manifest = state.read_manifest(mp)
        self.assertEqual(first_manifest["delegation_id"], "deleg_cache001")

        # Mutating returned dicts must not corrupt cached copies
        first_log["tool_count"] = 999
        first_manifest["delegation_id"] = "mutated"

        with mock.patch.object(Path, "read_text", side_effect=AssertionError("unexpected disk read")):
            cached_log = state.parse_log(lp)
        self.assertEqual(cached_log["tool_count"], 1)
        self.assertEqual(state.read_manifest(mp)["delegation_id"], "deleg_cache001")

        # Appending to the log changes size/mtime and must invalidate cache immediately
        with lp.open("a", encoding="utf-8") as fh:
            fh.write("22:00:05 tool      | -> terminal(ls)\n")
        updated_log = state.parse_log(lp)
        self.assertEqual(updated_log["tool_count"], 2)


if __name__ == "__main__":
    unittest.main()
