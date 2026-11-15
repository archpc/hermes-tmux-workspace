"""Regression tests for WatchSet inode replacement, ordered FD priority,
active-only log watching, and run_watch throttle enforcement.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hermes_tmux import cli, state, watch


def _watch_fd_capacity() -> int:
    """Descriptors this process can still open, used to size the watch cap."""
    import resource
    soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    if soft in (resource.RLIM_INFINITY, -1):
        return watch._MAX_WATCH_FILES
    try:
        # The listing holds one descriptor of its own for the duration of the
        # call, so the observed count is one higher than the steady state.
        used = len(os.listdir("/dev/fd")) - 1
    except OSError:
        used = 0
    return max(0, soft - used)


@contextlib.contextmanager
def no_live_tmux(tmux_tmpdir: Path):
    """Point bare ``tmux`` calls at a private, empty socket directory.

    ``tmuxio`` shells out to ``tmux`` with no ``-S``/``-L``, so the target is
    whatever $TMUX names, else $TMUX_TMPDIR/tmux-<uid>/default. A fixture that
    lets the real ``reconcile_tabs`` run must sever both, or it operates on the
    developer's live workspace.
    """
    keys = ("TMUX", "TMUX_PANE", "TMUX_TMPDIR")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    Path(tmux_tmpdir).mkdir(parents=True, exist_ok=True)
    os.environ["TMUX_TMPDIR"] = str(tmux_tmpdir)
    try:
        yield
    finally:
        for k, value in saved.items():
            if value is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = value


def run_mocked_coordinator(
    home: Path,
    key: str,
    *,
    session_alive,
    counting_reconcile,
    tmux_tmpdir: Path,
):
    """Run one coordinator loop with tmux mocked, fenced off any live server.

    ``reconcile_tabs`` keeps its real implementation: the tests measure how
    often it runs. That means the only thing standing between the test and the
    developer's tmux server is ``no_live_tmux`` and the two patches below.
    """
    with mock.patch.dict(os.environ, {"HERMES_TMUX": "1"}), \
         no_live_tmux(tmux_tmpdir), \
         mock.patch.object(watch.tmuxio, "server_pid", return_value="123"), \
         mock.patch.object(watch.tmuxio, "session_alive", side_effect=session_alive), \
         mock.patch.object(watch, "reconcile_tabs", side_effect=counting_reconcile), \
         mock.patch.object(cli, "ensure_aux_window", return_value=(None, "")), \
         mock.patch.object(cli, "testbench_tab_enabled", return_value=False):
        return watch.run_watch(home=home, key=key)


class WatchSetAndPerfRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hx_c2c3_")
        self.base = Path(self._tmp.name)
        self.tmux_tmp = self.base / "tmux"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_watchset_sync_reopens_atomically_replaced_inode(self) -> None:
        """After os.replace(tmp, path), WatchSet.sync must close the stale FD on
        the unlinked inode and watch the new inode so subsequent in-place writes
        still fire kqueue events immediately.
        """
        p = self.base / "manifest.json"
        p.write_text('{"v": 1}\n', encoding="utf-8")
        ws = watch.WatchSet()
        try:
            ws.sync([], [p])
            old_ino = os.fstat(ws.files[str(p)]).st_ino
            tmp = self.base / "manifest.json.tmp"
            tmp.write_text('{"v": 2}\n', encoding="utf-8")
            os.replace(tmp, p)
            new_ino = p.stat().st_ino
            self.assertNotEqual(old_ino, new_ino)
            ws.drain(quiet_for=0.03)

            ws.sync([], [p])
            watched_ino = os.fstat(ws.files[str(p)]).st_ino
            self.assertEqual(watched_ino, new_ino)

            with p.open("a", encoding="utf-8") as fh:
                fh.write("// in-place update\n")
            fired = ws.wait(0.3)
            self.assertGreater(fired, 0, "expected kqueue event on new inode write")
        finally:
            ws.close()

    def test_watchset_sync_preserves_caller_priority_order_at_cap(self) -> None:
        """Critical singleton files passed first (state.db, state.db-wal,
        reviews.json) must never be evicted by unordered iteration when more
        than _MAX_WATCH_FILES candidates are passed.

        The cap is a ceiling, not a promise: when the process cannot hold
        _MAX_WATCH_FILES descriptors (macOS soft RLIMIT_NOFILE is often 256,
        which the kqueue FD and stdio already eat into), sync must keep the
        caller-priority prefix and drop the tail, never an arbitrary subset.
        """
        files = []
        for idx in range(watch._MAX_WATCH_FILES + 30):
            fp = self.base / ("f_%04d.txt" % idx)
            fp.write_text("x", encoding="utf-8")
            files.append(fp)

        ws = watch.WatchSet()
        try:
            # Measured after WatchSet() so the kqueue descriptor is counted.
            expected = min(watch._MAX_WATCH_FILES, _watch_fd_capacity())
            if expected < 32:
                self.skipTest("only %d descriptors available; cap cannot be exercised" % expected)
            ws.sync([], files)
            self.assertEqual(len(ws.files), expected)
            # Everything watched is exactly the caller-priority prefix
            self.assertEqual(list(ws.files), [str(fp) for fp in files[:expected]])
            # Every one of the first 20 highest-priority files must be watched
            for fp in files[:20]:
                self.assertIn(str(fp), ws.files)
            # Overflow tail files must be the ones left out
            for fp in files[expected:]:
                self.assertNotIn(str(fp), ws.files)
        finally:
            ws.close()

    def test_collect_paths_skips_completed_delegation_logs_and_prioritizes_db(self) -> None:
        """Completed delegations never append to task-*.log again; opening kqueue
        FDs on 150+ historical logs wastes descriptors in both watch and follow.
        """
        home = self.base / "home"
        live = home / "cache" / "delegation" / "live"
        # 10 completed delegations x 3 logs = 30 historical logs
        for idx in range(10):
            dd = live / ("deleg_done%04d" % idx)
            dd.mkdir(parents=True)
            tasks = []
            for t in range(3):
                lp = dd / ("task-%d.log" % t)
                lp.write_text("done\n", encoding="utf-8")
                tasks.append({"index": t, "goal": "g", "log": str(lp), "status": "completed"})
            (dd / "manifest.json").write_text(json.dumps({
                "delegation_id": "deleg_done%04d" % idx,
                "started": "2026-11-13 10:00:00",
                "completed": "2026-11-13 10:01:00",
                "tasks": tasks,
            }), encoding="utf-8")

        # 1 running delegation x 2 logs = 2 active logs
        rd = live / "deleg_run00001"
        rd.mkdir(parents=True)
        rtasks = []
        for t in range(2):
            lp = rd / ("task-%d.log" % t)
            lp.write_text("running\n", encoding="utf-8")
            rtasks.append({"index": t, "goal": "r", "log": str(lp), "status": "running"})
        (rd / "manifest.json").write_text(json.dumps({
            "delegation_id": "deleg_run00001",
            "started": "2026-11-13 11:00:00",
            "tasks": rtasks,
        }), encoding="utf-8")

        (home / "state.db").write_text("", encoding="utf-8")
        (home / "state.db-wal").write_text("", encoding="utf-8")

        _dirs, files = watch.collect_paths(home)
        file_strs = [str(f) for f in files]
        # Priority head: state.db and state.db-wal come first
        self.assertEqual(file_strs[:2], [str(home / "state.db"), str(home / "state.db-wal")])
        # Running logs ARE watched
        self.assertIn(str(rd / "task-0.log"), file_strs)
        self.assertIn(str(rd / "task-1.log"), file_strs)
        # Completed logs are NOT watched
        self.assertNotIn(str(live / "deleg_done0000" / "task-0.log"), file_strs)

    def test_run_watch_enforces_min_render_interval_under_continuous_live_writes(self) -> None:
        """Rapid live transcript/manifest writes (every 40ms) must not defeat
        _MIN_RENDER_INTERVAL_S by waking watcher.wait(remaining) early.
        """
        home = self.base / "whome"
        dd = home / "cache" / "delegation" / "live" / "deleg_spam0001"
        dd.mkdir(parents=True)
        lp = dd / "task-0.log"
        lp.write_text("08:00:01 tool      | -> terminal(ls)\n", encoding="utf-8")
        (dd / "manifest.json").write_text(json.dumps({
            "delegation_id": "deleg_spam0001",
            "started": "2026-11-13 08:00:00",
            "tasks": [{"index": 0, "goal": "spam", "log": str(lp), "status": "running"}],
        }), encoding="utf-8")
        key = "test-throttle"
        watch.write_layout(key, {"session_id": "$1", "session_name": "s"}, home)

        passes = []
        stop_at = time.monotonic() + 1.6

        import threading
        def spam_log() -> None:
            i = 0
            while time.monotonic() < stop_at:
                with lp.open("a", encoding="utf-8") as fh:
                    fh.write("08:00:02 tool      | -> read_file(%d)\n" % i)
                i += 1
                time.sleep(0.04)

        def fake_session_alive(_sid: str) -> bool:
            return time.monotonic() < stop_at

        orig_reconcile = watch.reconcile_tabs

        def counting_reconcile(k: str, workers: list, h=None):
            passes.append(time.monotonic())
            return orig_reconcile(k, workers, h)

        writer = threading.Thread(target=spam_log, daemon=True)
        writer.start()
        run_mocked_coordinator(home, key, session_alive=fake_session_alive,
                               counting_reconcile=counting_reconcile,
                               tmux_tmpdir=self.tmux_tmp)
        writer.join(timeout=2)

        self.assertGreaterEqual(len(passes), 1, "coordinator never reconciled")
        self.assertLessEqual(len(passes), 3, "expected <=3 passes in 1.6s, got %d" % len(passes))
        if len(passes) >= 2:
            self.assertGreaterEqual(passes[1] - passes[0], 0.85)

    def test_unrelated_wal_writes_skip_reconcile_until_delegations_change(self) -> None:
        """Writes to unrelated tables in state.db-wal must not trigger full
        reconcile_tabs passes when async_delegations is unchanged, but an actual
        async_delegations update must still reconcile promptly.
        """
        import sqlite3
        home = self.base / "walhome"
        (home / "cache" / "delegation" / "live").mkdir(parents=True)
        con = sqlite3.connect(str(home / "state.db"), check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        con.execute(
            "CREATE TABLE async_delegations ("
            "delegation_id TEXT PRIMARY KEY, state TEXT)"
        )
        con.commit()

        key = "test-wal-skip"
        watch.write_layout(key, {"session_id": "$1", "session_name": "s"}, home)
        passes = []
        done = []
        deadline = time.monotonic() + 5.0

        import threading
        def drive_db() -> None:
            time.sleep(0.15)
            # 15 unrelated WAL commits over 0.6s -> must trigger 0 extra passes
            for i in range(15):
                con.execute("INSERT INTO messages (body) VALUES (?)", ("msg %d" % i,))
                con.commit()
                time.sleep(0.04)
            passes_after_unrelated = len(passes)
            # Now mutate async_delegations -> MUST trigger a pass!
            con.execute("INSERT INTO async_delegations VALUES ('deleg_db01', 'completed')")
            con.commit()
            done.append(passes_after_unrelated)

        orig_reconcile = watch.reconcile_tabs

        def counting_reconcile(k: str, workers: list, h=None):
            passes.append(time.monotonic())
            return orig_reconcile(k, workers, h)

        def fake_session_alive(_sid: str) -> bool:
            if time.monotonic() > deadline:
                return False
            return not (done and len(passes) >= 2)

        t = threading.Thread(target=drive_db, daemon=True)
        t.start()
        try:
            # HERMES_TMUX is forced on: the gate is orthogonal to WAL
            # reconciliation, and TestBench's isolation sets HERMES_TMUX=0,
            # which would make run_watch return before the first pass.
            run_mocked_coordinator(home, key, session_alive=fake_session_alive,
                                   counting_reconcile=counting_reconcile,
                                   tmux_tmpdir=self.tmux_tmp)
        finally:
            t.join(timeout=2)
            con.close()

        self.assertEqual(done[0], 1, "unrelated WAL commits caused extra reconcile passes: %d" % done[0])
        self.assertGreaterEqual(len(passes), 2, "real async_delegations change did not trigger reconcile")


if __name__ == "__main__":
    unittest.main()
