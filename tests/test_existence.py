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
            win = box.window("agents")
            self.assertIsNotNone(win, "up did not create the overview window")
            code = ("from hermes_tmux import tmuxio; "
                    "print('REAL', tmuxio.window_exists(%r)); "
                    "print('MISSING', tmuxio.window_exists('@9999'))" % win["id"])
            self.assertEqual(box.run("%s -c %s" % (PY, _q(code))), 0)
            out = box.capture()
            self.assertIn("REAL True", out)
            self.assertIn("MISSING False", out,
                          "window_exists answered True for a window that does not exist")


class StalePaneRecoveryTest(unittest.TestCase):
    def test_a_closed_tab_stays_closed_until_it_is_opened(self):
        """v3: closing a tab is the reader's decision, and `open` undoes it.

        A pane that disappears is indistinguishable from a tab the reader
        closed, so the coordinator records the dismissal instead of silently
        rebuilding a pane behind them. The old "rebuild the killed tail pane"
        rule is exactly the behaviour v3 removed; the property that survives is
        that the layout never keeps addressing a dead pane, and that the
        reader has one documented way to get the tab back.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_stale001", [{"goal": "recover"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: len(box.layout().get("tabs") or {}) == 1))
            wkey, tab = list(box.layout()["tabs"].items())[0]
            old_pane, old_window = tab["pane"], tab["window"]
            _tmux("kill-pane", "-t", old_pane)

            self.assertTrue(
                wait_for(lambda: wkey not in (box.layout().get("tabs") or {}), timeout=25),
                "the coordinator kept a dead pane id in the layout")
            self.assertIn(wkey, box.layout().get("dismissed") or {},
                          "the closure was not recorded, so the tab can reopen by itself")

            # A later worker event drives another reconcile pass; it must not
            # resurrect the tab the reader closed.
            box.append_event("deleg_stale001", 0, "note", "still working")
            self.assertTrue(wait_for(
                lambda: wkey not in (box.layout().get("tabs") or {}), timeout=5),
                "the coordinator reopened a tab the reader had closed")
            open_windows = _tmux("list-windows", "-t", box.name, "-F", "#{window_id}")[1].split()
            self.assertNotIn(old_window, open_windows)

            # `open` is the documented way to get it back.
            self.assertEqual(box.cli("open", "stale001"), 0)
            tabs = box.layout().get("tabs") or {}
            self.assertIn(wkey, tabs, "`hermes-tmux open` did not reopen the tab")
            new_pane = tabs[wkey]["pane"]
            self.assertNotEqual(new_pane, old_pane)
            self.assertNotIn(wkey, box.layout().get("dismissed") or {})
            self.assertTrue(
                wait_for(lambda: _tmux("display-message", "-p", "-t", new_pane,
                                       "#{pane_id}")[1].strip() == new_pane, timeout=10),
                "the reopened tab addresses a pane that does not exist")

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
