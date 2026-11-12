"""Unit tests for the safety guards around the workspace's own processes.

The watcher pid file is an untrusted pointer: pids get recycled, so a stale
file can name a perfectly healthy unrelated process. These tests pin the rule
that a signal is only sent to a process that is provably our watcher.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hermes_tmux import cli, watch


class WatcherPidGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)
        self.key = "testkey"

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home
        self.tmp.cleanup()

    def write_pid(self, pid):
        path = watch.pid_path(self.key, self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(pid), encoding="utf-8")

    def test_live_but_foreign_pid_is_not_our_watcher(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.write_pid(proc.pid)
            self.assertFalse(cli._watcher_alive(self.key),
                             "a live unrelated process must not count as our watcher")
            self.assertIsNone(cli._pid_is_watcher(proc.pid) or None)
        finally:
            proc.kill()
            proc.wait()

    def test_pid_one_and_zero_are_refused(self):
        for pid in (0, 1, -5):
            self.assertFalse(cli._pid_is_watcher(pid))

    def test_missing_and_garbage_pid_files_are_refused(self):
        self.assertFalse(cli._watcher_alive(self.key))          # absent file
        self.write_pid("not-a-pid")
        self.assertFalse(cli._watcher_alive(self.key))          # unparsable
        self.write_pid(str(os.getpid()))
        # This test process is alive but is not a watcher.
        self.assertFalse(cli._watcher_alive(self.key))

    def test_dead_pid_is_refused(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.write_pid(proc.pid)
        self.assertFalse(cli._watcher_alive(self.key))


if __name__ == "__main__":
    unittest.main()
