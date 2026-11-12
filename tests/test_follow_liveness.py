"""The pane-side display loop must not outlive the pane it draws into.

tmux does not reliably deliver SIGHUP to pane processes when the server is
killed outright (a crash, `kill-server`, a reboot-time cleanup), so a loop that
only redraws and waits survives forever, holding a temp HERMES_HOME and a
kqueue. This was observed in practice: a `follow` process was still running 7.5
minutes after the server it belonged to had gone.

Both tests fail against the loop without its liveness check: the first hangs
until the timeout, the second would pass for the wrong reason only if the check
were inverted (it exits a live pane immediately).
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
    info = _tmux("display-message", "-p", "#{socket_path}|#{pid}")[1]
    socket_path, server_pid = info.split("|")
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env.update(TMUX="%s,%s,0" % (socket_path, server_pid), TMUX_PANE=pane_id,
               HERMES_HOME=str(home), HERMES_TMUX="1", PYTHONPATH=str(REPO))
    return env


class FollowLivenessTest(unittest.TestCase):
    def test_follow_exits_when_its_pane_is_gone(self):
        with Sandbox() as box:
            self.assertEqual(box.cli("up"), 0)
            env = _control_env(box.home, "%9999")
            started = time.time()
            try:
                done = subprocess.run([PY, "-m", "hermes_tmux", "follow", "--key", "liveness"],
                                      env=env, cwd=str(REPO), capture_output=True,
                                      text=True, timeout=15)
            except subprocess.TimeoutExpired:
                self.fail("follow kept running for a pane that does not exist")
            self.assertEqual(done.returncode, 0, done.stderr[-300:])
            self.assertLess(time.time() - started, 15)

    def test_follow_keeps_running_for_a_live_pane(self):
        with Sandbox() as box:
            self.assertEqual(box.cli("up"), 0)
            layout = box.layout()
            pane = layout.get("overview_pane")
            self.assertTrue(pane, layout)
            env = _control_env(box.home, pane)
            proc = subprocess.Popen([PY, "-m", "hermes_tmux", "follow", "--key", "liveness"],
                                    env=env, cwd=str(REPO), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            try:
                exited = wait_for(lambda: proc.poll() is not None, timeout=3.0)
                self.assertFalse(exited, "follow exited while its pane was alive")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    unittest.main()
