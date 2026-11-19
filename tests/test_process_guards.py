"""Unit tests for the safety guards around the workspace's own processes.

The watcher pid file is an untrusted pointer: pids get recycled, so a stale
file can name a perfectly healthy unrelated process. These tests pin the rule
that a signal is only sent to a process that is provably our watcher.

The rule, as ``cli._pid_is_watcher`` implements it (all three must hold):

* ``pid > 1``. 0 is "my whole process group" to ``kill(2)``, 1 is init/launchd,
  and a negative pid is a process *group* — none of those can be a coordinator,
  and signalling one is the worst case of the bug this file defends against.
  This check runs first, before any subprocess, so a hostile pid file cannot
  even cost a probe.
* when a key is given, the pid is the current holder of that session's
  ``watch.lock`` (``lsof -t``). The coordinator flocks the file for its whole
  life, so the holder *is* the coordinator; a command line can be spoofed or
  merely mention the words, an open lock cannot.
* the command line from ``ps`` contains the contiguous ``-m hermes_tmux watch``
  sequence and, when a key is given, `` --key <key>``. Substring matching on the
  exact launcher shape is what separates the coordinator from a grep, an editor,
  or the ``sh -c`` wrapper tmux uses for ``run-shell -b``.

``_watcher_alive`` is ``_watcher_pid(key) is not None``, i.e. "the pid file
parses, and what it names passes the three checks". A missing or unparsable
file answers False without raising.

None of these tests need tmux: the guards are pure process/lock inspection, and
the pid file is written into a private ``HERMES_HOME`` per test. Every fixture
here is a negative control, so the positive one lives elsewhere: in
``test_tmux_integration``, ``test_coordinator_is_recognised_while_it_lives``
runs ``cli._watcher_alive`` against a live coordinator and waits for ``True``.
(``test_isolation_guards`` only calls ``_pid_alive`` on a raw pid, which
bypasses these helpers entirely.)
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
    # HERMES_HOME is set in this process because `watch.pid_path` resolves the
    # session directory from the environment when no explicit home is passed.
    # The original value is restored in tearDown, including the "was unset" case,
    # so this class cannot leak a profile into another module.
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
        # The same path the tool reads (`<home>/cache/hermes-tmux/sessions/
        # <key>/watch.pid`), built by the production helper so a layout change
        # cannot leave the test writing to a place nobody reads.
        path = watch.pid_path(self.key, self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(pid), encoding="utf-8")

    def test_live_but_foreign_pid_is_not_our_watcher(self):
        # The recycled-pid case, and the reason the guard exists: the process is
        # genuinely alive, so `kill -0` (and any pid-only liveness check) says
        # yes. Nothing but the lock and the command line can tell it apart.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.write_pid(proc.pid)
            # No watch.lock exists in this home, so the lock half alone rejects
            # it: _lock_holder answers None and _pid_is_watcher returns False
            # before the ps probe.
            self.assertFalse(cli._watcher_alive(self.key),
                             "a live unrelated process must not count as our watcher")
            # Called without a key, the lock check is skipped entirely (the
            # default key="" cannot identify a session). The remaining
            # discriminator is then the command line, which for `python -c
            # sleep` does not contain "-m hermes_tmux watch". `or None` collapses
            # the False into None so assertIsNone states the contract plainly.
            self.assertIsNone(cli._pid_is_watcher(proc.pid) or None)
        finally:
            proc.kill()
            proc.wait()

    def test_pid_one_and_zero_are_refused(self):
        # The numeric floor, isolated from any probe: these must be refused even
        # if a lock file and a matching command line existed. -5 is the
        # process-group form of kill(2); refusing it is the difference between
        # "stop the coordinator" and "signal whatever group that number names".
        for pid in (0, 1, -5):
            self.assertFalse(cli._pid_is_watcher(pid))

    def test_missing_and_garbage_pid_files_are_refused(self):
        # Three shapes of "the pointer is useless", each answering False rather
        # than raising: absent, unparsable, and parseable-but-not-a-watcher.
        self.assertFalse(cli._watcher_alive(self.key))          # absent file
        self.write_pid("not-a-pid")
        self.assertFalse(cli._watcher_alive(self.key))          # unparsable
        # This test process is alive and its pid parsed cleanly. The lock
        # lookup refuses it before the command line is even consulted (there is
        # no watch.lock in this home); the command line would refuse it too.
        # Both halves of the guard therefore contribute, in order.
        self.write_pid(str(os.getpid()))
        self.assertFalse(cli._watcher_alive(self.key))

    def test_dead_pid_is_refused(self):
        # A pid that existed and is gone. The lock half decides it: this home
        # has no watch.lock, so _pid_is_watcher returns False at that gate and
        # ps is never invoked. Together with the test above this pins the lock
        # gate only; the command-line half is not reached by either.
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.write_pid(proc.pid)
        self.assertFalse(cli._watcher_alive(self.key))


if __name__ == "__main__":
    unittest.main()
