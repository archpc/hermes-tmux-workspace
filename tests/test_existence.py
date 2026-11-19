"""Adversarial tests for tmux target existence and socket hygiene.

Every test here fails against the pre-fix implementation:

* ``pane_exists`` / ``window_exists`` used to test the exit status of
  ``display-message``, which tmux 3.8 returns as 0 even for a missing target.
  They therefore answered True for any non-empty id.
* The suite's teardown probed the socket path *after* killing the server, so the
  probe returned an empty path and the file was never removed.
* ``down`` counted windows with the same broken check and always printed 0.

Why the exit status is the wrong oracle: ``display-message -t <missing>`` in
tmux 3.8 exits 0 and prints nothing. So a status-only probe answers "exists"
for every well-formed id, which is not a cosmetic bug — it silently disables
stale-pane recovery (a dead pane id is never noticed), window accounting in
``down``, and every guard that refuses to address a target it cannot prove.
``hermes_tmux.tmuxio.pane_exists`` therefore compares the *resolved* id
(``format_of(target, "#{pane_id}") == target``), and ``window_exists`` does the
same for ``#{window_id}``. A missing target resolves to the empty string and
fails the comparison.

Harness: ``Sandbox`` from ``tests/test_tmux_integration``. Each box is one
disposable session on a private ``tmux -L hx_it_<pid>`` server with an empty
``-f /dev/null`` config and a private ``HERMES_HOME``; nothing here can read or
write the user's server, config, or state. The probes run *inside* that pane
(``box.run``) so the child inherits the sandbox's exports, rather than being
asked in this process's environment.
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
    # Both tests are differential: a real id that must answer True and an id
    # that must answer False, printed from the same process. The positive half
    # is not decoration — it is the negative control's control. A probe that
    # always returned False would satisfy the MISSING assertion alone, so the
    # pair is what pins "resolves this exact id", not "returned something".
    #
    # The missing ids are chosen to be out of reach rather than merely unused:
    # %9999 and @9999 are far past any id tmux allocates in a fresh sandbox
    # (ids come from a small monotonic counter), so the probe cannot accidentally
    # hit a live pane or window.

    def test_pane_exists_rejects_a_missing_pane(self):
        with Sandbox() as box:
            self.assertEqual(box.cli("up"), 0)
            real = box.pane_id
            # One child process, two answers: avoids a second interpreter start
            # and guarantees both probes saw the same server state.
            code = ("from hermes_tmux import tmuxio; "
                    "print('REAL', tmuxio.pane_exists(%r)); "
                    "print('MISSING', tmuxio.pane_exists('%%9999'))" % real)
            self.assertEqual(box.run("%s -c %s" % (PY, _q(code))), 0)
            out = box.capture()
            self.assertIn("REAL True", out)
            # The failure message names the observable defect so a regression
            # report says what broke, not just that an assert fired.
            self.assertIn("MISSING False", out,
                          "pane_exists answered True for a pane that does not exist")

    def test_window_exists_rejects_a_missing_window(self):
        with Sandbox() as box:
            self.assertEqual(box.cli("up"), 0)
            # `agents` is the workspace's own overview window (watch.OVERVIEW_WINDOW).
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
    # Timing: nothing here asserts a duration, only an eventual state, and every
    # wait is bounded. The coordinator reacts to a vnode event (pane death is
    # seen on the next reconcile), so a kill-pane is followed by a poll rather
    # than a sleep. The 25 s budget on the first wait covers one
    # watch._SESSION_CHECK_S (20 s) idle cadence plus the render throttle; the
    # later 5 s waits are short on purpose — they assert that *nothing* changed,
    # and a longer window would only slow the failure mode.

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
            # One delegation with one task: exactly one worker tab, so "the tab
            # went away" and "the layout changed" cannot be confused.
            self.assertTrue(wait_for(lambda: len(box.layout().get("tabs") or {}) == 1))
            wkey, tab = list(box.layout()["tabs"].items())[0]
            old_pane, old_window = tab["pane"], tab["window"]
            # Killing the pane is the closest observable stand-in for a reader
            # closing the tab; tmux offers no event that distinguishes them.
            _tmux("kill-pane", "-t", old_pane)

            # The invariant is about the *layout*, not about tmux: a dead pane id
            # must stop being addressed. Waiting on the layout rather than on
            # the window list keeps the assertion about the record the rest of
            # the tool reads.
            self.assertTrue(
                wait_for(lambda: wkey not in (box.layout().get("tabs") or {}), timeout=25),
                "the coordinator kept a dead pane id in the layout")
            self.assertIn(wkey, box.layout().get("dismissed") or {},
                          "the closure was not recorded, so the tab can reopen by itself")

            # Negative control for the dismissal: a later worker event drives
            # another reconcile pass, and a rebuild-from-scratch implementation
            # would resurrect the tab here. The short timeout means the test
            # fails fast on the resurrection rather than waiting out the budget.
            box.append_event("deleg_stale001", 0, "note", "still working")
            self.assertTrue(wait_for(
                lambda: wkey not in (box.layout().get("tabs") or {}), timeout=5),
                "the coordinator reopened a tab the reader had closed")
            # Cross-check tmux itself, not only our JSON: a tab could be
            # "dismissed" on disk while its window is still on screen.
            open_windows = _tmux("list-windows", "-t", box.name, "-F", "#{window_id}")[1].split()
            self.assertNotIn(old_window, open_windows)

            # `open` is the documented way to get it back. A new pane id — not
            # reused — is what makes this a reopen rather than a bookkeeping
            # edit of the old record.
            self.assertEqual(box.cli("open", "stale001"), 0)
            tabs = box.layout().get("tabs") or {}
            self.assertIn(wkey, tabs, "`hermes-tmux open` did not reopen the tab")
            new_pane = tabs[wkey]["pane"]
            self.assertNotEqual(new_pane, old_pane)
            self.assertNotIn(wkey, box.layout().get("dismissed") or {})
            # And the reopened record must address something real: an id in
            # layout.json that tmux cannot resolve is the defect this file
            # exists for. Resolving the id back to itself is the same test
            # tmuxio.pane_exists applies.
            self.assertTrue(
                wait_for(lambda: _tmux("display-message", "-p", "-t", new_pane,
                                       "#{pane_id}")[1].strip() == new_pane, timeout=10),
                "the reopened tab addresses a pane that does not exist")

    def test_down_reports_the_windows_it_closed(self):
        # `down` used the broken existence check, so its window counter was
        # always zero and its report lied about work it had actually done.
        # The regex demands a non-zero window count: a message that keeps the
        # old shape but reports 0 fails, which is the regression being pinned.
        with Sandbox() as box:
            box.add_delegation("deleg_stale002", [{"goal": "accounting"}])
            self.assertEqual(box.cli("up"), 0)
            # `logs` guarantees at least one extra permanent window (hx:logs)
            # beside `agents`, so a count of 1 is the floor of the claim.
            self.assertEqual(box.cli("logs"), 0)
            self.assertTrue(wait_for(lambda: box.window("hx:logs") is not None))
            self.assertEqual(box.cli("down"), 0)
            out = box.capture()
            self.assertRegex(out, r"closed \d+ pane\(s\) and [1-9]\d* window\(s\)",
                             "down did not report the windows it actually closed: %r"
                             % out[-300:])


class SocketTeardownTest(unittest.TestCase):
    # The premise is asserted, not assumed: if tmux cleaned up its own socket
    # file on kill-server, the helper would have nothing to do and the test
    # would pass while proving nothing. The explicit "test premise wrong" guard
    # turns that platform difference into a visible failure.
    #
    # The probe deliberately does not reuse the suite's SOCKET: this test kills
    # a server outright, and doing that to the shared one would tear down every
    # other test in the module.

    def test_socket_file_is_removed_once_the_server_is_gone(self):
        name = "hx_probe_%d" % os.getpid()
        # A dedicated socket for this probe: _tmux targets SOCKET, so the probe
        # server is created directly and can be killed without touching the suite.
        subprocess.run([TMUX, "-L", name, "-f", "/dev/null", "new-session", "-d",
                        "-s", "probe", "exec /bin/zsh -f"], check=True,
                       capture_output=True, timeout=15)
        # Ask the server for the path it actually bound rather than deriving it:
        # `-L name` resolves through $TMUX_TMPDIR, which the harness may move.
        path = subprocess.run([TMUX, "-L", name, "display-message", "-p",
                               "#{socket_path}"], capture_output=True, text=True,
                              timeout=15).stdout.strip()
        self.assertTrue(path and Path(path).exists(), "no socket file to test")
        subprocess.run([TMUX, "-L", name, "kill-server"], capture_output=True, timeout=15)
        # kill-server returns before the server has finished exiting, and the
        # unlink is only safe once nothing answers on the socket; the helper
        # polls for that. The file must still be present right here, or the
        # helper's "never unlink a live server's socket" rule is untested.
        self.assertTrue(Path(path).exists(), "tmux removed the file itself; test premise wrong")

        _remove_socket_if_dead(name, path)
        self.assertFalse(Path(path).exists(),
                         "the teardown helper left the dead socket file behind")


if __name__ == "__main__":
    unittest.main()
