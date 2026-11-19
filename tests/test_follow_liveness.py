"""The pane-side display loop must not outlive the pane it draws into.

tmux does not reliably deliver SIGHUP to pane processes when the server is
killed outright (a crash, `kill-server`, a reboot-time cleanup), so a loop that
only redraws and waits survives forever, holding a temp HERMES_HOME and a
kqueue. This was observed in practice: a `follow` process was still running 7.5
minutes after the server it belonged to had gone.

Both tests fail against the loop without its liveness check: the first hangs
until the timeout, the second would pass for the wrong reason only if the check
were inverted (it exits a live pane immediately).

What ``watch.run_follow`` actually guarantees: ``tmuxio.pane_exists(pane_id)``
is asked at the top of every iteration, and a False answer returns 0 before any
compose or draw. So the exit is a *poll*, not a signal: worst case one wake
interval (``_FOLLOW_WAKE_S`` = 60 s idle, ``_FOLLOW_RUNNING_WAKE_S`` = 2 s while
the text says RUNNING) plus one ``display-message`` round trip. The first test
therefore budgets 15 s and uses a pane id that never existed, where the check is
the *first* thing reached — the TTL bound in the real incident is the idle wake,
not a race here.

The pair is deliberately symmetric: existence is only meaningful if the probe
can say False, and a probe that always says False would kill a healthy follower.
Neither test alone distinguishes "check works" from "check constant".

Harness: joined via ``tests.test_tmux_integration`` for the private
``tmux -L hx_it_<pid>`` server, its empty config, and ``Sandbox``. ``follow`` is
run as a child of *this* process, not typed into a pane, so the test controls
its exit status and its stderr.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_tmux_integration import PY, REPO, SOCKET, TMUX, Sandbox, _tmux, wait_for


def _control_env(home: Path, pane_id: str) -> dict:
    # A hand-built environment, because the thing under test is a process that
    # is *not* started by tmux: it needs the three facts tmux would have given
    # it — which socket to talk to (the comma form of $TMUX), which pane it
    # draws into ($TMUX_PANE), and which profile to read (HERMES_HOME).
    #
    # The socket path and server pid are read from the sandbox server itself, so
    # the child can only ever reach the private server, never the ambient one.
    # TMUX/TMUX_PANE are dropped from the inherited environment first: a stray
    # ambient value would point the follower at the developer's own server.
    info = _tmux("display-message", "-p", "#{socket_path}|#{pid}")[1]
    socket_path, server_pid = info.split("|")
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    # "%s,%s,0" is tmux's $TMUX shape: socket path, server pid, session index.
    # The index is a legacy field and is not what identifies the session here.
    env.update(TMUX="%s,%s,0" % (socket_path, server_pid), TMUX_PANE=pane_id,
               HERMES_HOME=str(home), HERMES_TMUX="1", PYTHONPATH=str(REPO))
    return env


class FollowLivenessTest(unittest.TestCase):
    def test_follow_exits_when_its_pane_is_gone(self):
        # Positive control for the exit path: a pane id that cannot exist, so
        # the very first liveness check answers False. The assertion is on the
        # *return code* as well as the timeout, because a follower that died of
        # a traceback would also stop quickly; exit 0 is the documented
        # "pane disappeared" result.
        with Sandbox() as box:
            self.assertEqual(box.cli("up"), 0)
            env = _control_env(box.home, "%9999")
            started = time.time()
            try:
                done = subprocess.run([PY, "-m", "hermes_tmux", "follow", "--key", "liveness"],
                                      env=env, cwd=str(REPO), capture_output=True,
                                      text=True, timeout=15)
            except subprocess.TimeoutExpired:
                # A TimeoutExpired here *is* the regression: the loop kept
                # waiting on a pane that does not exist. subprocess.run kills
                # the child on timeout, so no leak is left behind.
                self.fail("follow kept running for a pane that does not exist")
            self.assertEqual(done.returncode, 0, done.stderr[-300:])
            # Redundant with the timeout, kept as a stated budget: a follower
            # that took 14 s would still mean the check is not being reached
            # promptly, and the number belongs in the test, not in the comment.
            self.assertLess(time.time() - started, 15)

    def test_follow_keeps_running_for_a_live_pane(self):
        # The negative control. Without it, a `follow` that exited unconditionally
        # (inverted check, or an exception on the first draw) would pass the test
        # above. A live overview pane must keep it alive past several wake
        # intervals; 3 s is enough to cross the RUNNING wake (2 s) and to fail an
        # immediate exit, without paying for the 60 s idle wake.
        with Sandbox() as box:
            self.assertEqual(box.cli("up"), 0)
            layout = box.layout()
            pane = layout.get("overview_pane")
            self.assertTrue(pane, layout)
            env = _control_env(box.home, pane)
            # Popen rather than run(): this one must be observed while running,
            # and killed by the test rather than by a timeout.
            proc = subprocess.Popen([PY, "-m", "hermes_tmux", "follow", "--key", "liveness"],
                                    env=env, cwd=str(REPO), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            try:
                exited = wait_for(lambda: proc.poll() is not None, timeout=3.0)
                self.assertFalse(exited, "follow exited while its pane was alive")
            finally:
                # terminate() sends SIGTERM, which run_follow does not handle:
                # the default disposition kills the process at once (status
                # 143), so its `finally` — kqueue close, tty restore — is
                # skipped. That is why the exit status is never asserted here.
                # kill() is only a SIGKILL backstop if the process is still up
                # after 5 s.
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    unittest.main()
