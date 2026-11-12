"""Adversarial tests for tmux target existence and socket hygiene.

Every test here fails against the pre-fix implementation:

* ``pane_exists`` / ``window_exists`` used to test the exit status of
  ``display-message``, which tmux 3.8 returns as 0 even for a missing target.
  They therefore answered True for any non-empty id.
* The suite's teardown probed the socket path *after* killing the server, so the
  probe returned an empty path and the file was never removed.
* ``down`` counted windows with the same broken check and always printed 0.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from tests.test_tmux_integration import (
    PY, REPO, SOCKET, TMUX, Sandbox, _q, _remove_socket_if_dead, _tmux,
    wait_for,
)


class ExistenceTest(unittest.TestCase):
    def test_pane_exists_rejects_a_missing_pane(self):
        with Sandbox() as box:
            self.assertEqual(box.cli("up"), 0)
            real = box.pane_id
            code = ("from hermes_tmux import tmuxio; "
                    "print('REAL', tmuxio.pane_exists(%r)); "
                    "print('MISSING', tmuxio.pane_exists('%%9999'))" % real)
            self.assertEqual(box.run("%s -c %s" % (PY, _q(code))), 0)
            out = box.capture()
            self.assertIn("REAL True", out)
            self.assertIn("MISSING False", out,
                          "pane_exists answered True for a pane that does not exist")

    def test_window_exists_rejects_a_missing_window(self):
        with Sandbox() as box:
            self.assertEqual(box.cli("up"), 0)
            win = box.window("hx:workers")
            code = ("from hermes_tmux import tmuxio; "
                    "print('REAL', tmuxio.window_exists(%r)); "
                    "print('MISSING', tmuxio.window_exists('@9999'))" % win["id"])
            self.assertEqual(box.run("%s -c %s" % (PY, _q(code))), 0)
            out = box.capture()
            self.assertIn("REAL True", out)
            self.assertIn("MISSING False", out,
                          "window_exists answered True for a window that does not exist")


class StalePaneRecoveryTest(unittest.TestCase):
    def test_a_closed_worker_pane_is_rebuilt(self):
        """A killed tail pane must come back while its worker is still visible."""
        with Sandbox() as box:
            box.add_delegation("deleg_stale001", [{"goal": "recover"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: len(box.layout().get("tail_panes") or {}) == 1))
            old = list(box.layout()["tail_panes"])[0]
            _tmux("kill-pane", "-t", old)

            self.assertTrue(
                wait_for(lambda: any(p != old for p in (box.layout().get("tail_panes") or {})),
                         timeout=25),
                "the coordinator kept a dead pane id and never rebuilt the tail pane")

    def test_down_reports_the_windows_it_closed(self):
        with Sandbox() as box:
            box.add_delegation("deleg_stale002", [{"goal": "accounting"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertEqual(box.cli("logs"), 0)
            self.assertTrue(wait_for(lambda: box.window("hx:logs") is not None))
            self.assertEqual(box.cli("down"), 0)
            out = box.capture()
            self.assertRegex(out, r"closed \d+ pane\(s\) and [1-9]\d* window\(s\)",
                             "down did not report the windows it actually closed: %r"
                             % out[-300:])


class SocketTeardownTest(unittest.TestCase):
    def test_socket_file_is_removed_once_the_server_is_gone(self):
        name = "hx_probe_%d" % os.getpid()
        # A dedicated socket for this probe: _tmux targets SOCKET, so the probe
        # server is created directly and can be killed without touching the suite.
        subprocess.run([TMUX, "-L", name, "-f", "/dev/null", "new-session", "-d",
                        "-s", "probe", "exec /bin/zsh -f"], check=True,
                       capture_output=True, timeout=15)
        path = subprocess.run([TMUX, "-L", name, "display-message", "-p",
                               "#{socket_path}"], capture_output=True, text=True,
                              timeout=15).stdout.strip()
        self.assertTrue(path and Path(path).exists(), "no socket file to test")
        subprocess.run([TMUX, "-L", name, "kill-server"], capture_output=True, timeout=15)
        self.assertTrue(Path(path).exists(), "tmux removed the file itself; test premise wrong")

        _remove_socket_if_dead(name, path)
        self.assertFalse(Path(path).exists(),
                         "the teardown helper left the dead socket file behind")


if __name__ == "__main__":
    unittest.main()
