"""Two guards that keep the workspace inside its own fences.

1. A coordinator must exit when the session it serves is closed, even while the
   tmux server keeps running for other sessions. `session_alive` used to trust
   the exit status of `display-message`, which tmux 3.8 returns as 0 for a
   missing session, so a closed session left a coordinator (kqueue, a full
   re-render per event) running until the whole server went.

2. A recorded window id is only meaningful on the server that issued it. After a
   server dies without killing its jobs, the new server can hand that id to a
   window the user owns. Reconciliation must refuse to split into or rename a
   window it does not own; before the guard it adopted the user's window.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hermes_tmux import watch
from tests.test_carbon_watch_and_perf import run_mocked_coordinator
from tests.test_tmux_integration import (
    PY, REPO, TMUX, Sandbox, _pid_alive, _q, _tmux, wait_for,
)


class SessionLivenessTest(unittest.TestCase):
    def test_coordinator_exits_when_its_session_is_closed(self):
        with Sandbox() as keeper, Sandbox() as victim:
            keeper.add_delegation("deleg_keep0001", [{"goal": "stay"}])
            victim.add_delegation("deleg_vict0001", [{"goal": "go"}])
            self.assertEqual(keeper.cli("up"), 0)
            self.assertEqual(victim.cli("up"), 0)

            key = next((d.name for d in
                        (victim.home / "cache/hermes-tmux/sessions").iterdir()), "")
            self.assertTrue(key, "no session directory was created")
            pid_file = victim.home / "cache/hermes-tmux/sessions" / key / "watch.pid"
            self.assertTrue(wait_for(pid_file.exists, timeout=10), "no coordinator started")
            pid = int(pid_file.read_text().strip())
            self.assertTrue(_pid_alive(pid))

            _tmux("kill-session", "-t", victim.name)
            victim.created = False
            self.assertEqual(_tmux("list-sessions")[0], 0,
                             "the server itself died; this test needs it alive")
            self.assertTrue(
                wait_for(lambda: not _pid_alive(pid), timeout=30),
                "the coordinator outlived the session it served (pid %s)" % pid)


class ServerIdentityTest(unittest.TestCase):
    """A coordinator from a crashed server must not serve the next one.

    tmux servers that die without killing their jobs (SIGKILL, crash) leave the
    coordinator running while a new server answers on the same socket. Window ids
    on the new server come from a fresh sequence, so a coordinator that keeps
    reconciling renames or splits into windows it never created, and duplicates
    its own tail panes. Identity is the server pid.
    """

    def test_coordinator_exits_after_its_server_is_replaced(self):
        with Sandbox() as box:
            box.add_delegation("deleg_crash001", [{"goal": "crash"}])
            self.assertEqual(box.cli("up"), 0)
            key = next(d.name for d in (box.home / "cache/hermes-tmux/sessions").iterdir())
            pid_file = box.home / "cache/hermes-tmux/sessions" / key / "watch.pid"
            self.assertTrue(wait_for(pid_file.exists, timeout=10))
            pid = int(pid_file.read_text().strip())
            self.assertTrue(_pid_alive(pid))

            server = int(_tmux("display-message", "-p", "#{pid}")[1].strip())
            subprocess.run(["kill", "-9", str(server)], check=False)
            time.sleep(1.0)

            # A new server takes over the socket, with a window the user owns.
            _tmux("new-session", "-d", "-s", "reborn", "-n", "notes", "exec /bin/zsh -f")
            self.assertEqual(_tmux("list-sessions")[0], 0, "the replacement server did not start")
            win = _tmux("list-windows", "-t", "reborn", "-F", "#{window_id}")[1].split()[0]
            before = _tmux("list-panes", "-t", win, "-F", "#{pane_id}")[1].split()

            self.assertTrue(wait_for(lambda: not _pid_alive(pid), timeout=45),
                            "the old coordinator kept serving a replaced server (pid %s)" % pid)

            name = _tmux("display-message", "-p", "-t", win, "#{window_name}")[1]
            after = _tmux("list-panes", "-t", win, "-F", "#{pane_id}")[1].split()
            self.assertEqual(name, "notes", "the crashed server's coordinator renamed a user window")
            self.assertEqual(after, before, "the crashed server's coordinator split into a user window")


class ForeignWindowGuardTest(unittest.TestCase):
    def test_reconcile_never_adopts_a_foreign_window(self):
        with Sandbox() as box:
            box.add_delegation("deleg_guard001", [{"goal": "guard"}])
            self.assertEqual(box.cli("up"), 0)
            key = next(d.name for d in (box.home / "cache/hermes-tmux/sessions").iterdir())
            layout_path = box.home / "cache/hermes-tmux/sessions" / key / "layout.json"
            saved = layout_path.read_text()
            self.assertEqual(box.cli("down"), 0)  # also removes the state dir

            # A window the user owns: no marker, an interactive shell.
            user_win = _tmux("new-window", "-d", "-P", "-F", "#{window_id}",
                             "-t", box.name, "-n", "notes", "exec /bin/zsh -f")[1].strip()
            self.assertTrue(user_win)
            before_panes = _tmux("list-panes", "-t", user_win, "-F", "#{pane_id}")[1].split()

            # A layout from a dead server, pointing at an id that now names the
            # user's window.
            layout = json.loads(saved)
            # Both stale references name the user's window: the overview id and
            # a worker tab record (window and pane) left by the dead server.
            layout.update({"workers_window": user_win, "overview_pane": "",
                           "session_id": "$0", "dismissed": {},
                           "tabs": {"deleg_guard001#0": {
                               "window": user_win, "pane": before_panes[0],
                               "opened": 1.0, "settled": 1.0}}})
            layout_path.parent.mkdir(parents=True, exist_ok=True)
            layout_path.write_text(json.dumps(layout))

            code = ("from hermes_tmux import watch, state\n"
                    "watch.reconcile_tabs(%r, state.load_workers(), None)\n" % key)
            info = _tmux("display-message", "-p", "#{socket_path}|#{pid}")[1]
            sock, server = info.split("|")
            import os as _os, subprocess as _sp
            env = {k: v for k, v in _os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
            env.update(TMUX="%s,%s,0" % (sock, server), TMUX_PANE=box.pane_id,
                       HERMES_HOME=str(box.home), HERMES_TMUX="1", PYTHONPATH=str(REPO))
            done = _sp.run([PY, "-c", code], env=env, cwd=str(REPO),
                           capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr[-400:])

            name = _tmux("display-message", "-p", "-t", user_win, "#{window_name}")[1]
            self.assertEqual(name, "notes", "the coordinator renamed a user window: %r" % name)
            after_panes = _tmux("list-panes", "-t", user_win, "-F", "#{pane_id}")[1].split()
            self.assertEqual(after_panes, before_panes,
                             "the coordinator split into a user window: %r -> %r"
                             % (before_panes, after_panes))
            marker = _tmux("show-options", "-w", "-t", user_win, "-v", "@hx")[1].strip()
            self.assertIn(marker, ("", "invalid option: @hx"),
                          "the coordinator marked a user window as its own: %r" % marker)


def tearDownModule():
    """Kill only this process's private server, and remove its socket file."""
    from tests.test_tmux_integration import tearDownModule as _harness_teardown
    _harness_teardown()
class MockedCoordinatorTmuxFenceTest(unittest.TestCase):
    """A fixture that runs the real ``reconcile_tabs`` with a mocked tmux client
    must still not mutate whichever tmux server the process can reach.

    ``run_mocked_coordinator`` keeps reconciliation real on purpose (the tests
    count its passes), so the only protection is severing $TMUX/$TMUX_PANE and
    pointing $TMUX_TMPDIR at a private empty directory. Without that, a run
    inside any tmux pane creates one worker window per running delegation in the
    developer's own session. A disposable server here plays that live server: if
    the fence regresses, windows appear on it and this test fails.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hx_fence_")
        self.root = Path(self._tmp.name)
        self.sock = self.root / "sock"

    def tearDown(self) -> None:
        self._tmux("-S", self.sock, "kill-server")
        self._tmp.cleanup()

    def _tmux(self, *args: str):
        proc = subprocess.run([TMUX, *args], capture_output=True, text=True, timeout=15)
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    def _windows(self):
        rc, out = self._tmux("-S", str(self.sock), "list-windows", "-a",
                             "-F", "#{session_name}:#{window_index} #{window_name}")
        return out.splitlines() if rc == 0 else []

    def test_mocked_coordinator_creates_no_windows_on_the_ambient_server(self) -> None:
        rc, out = self._tmux("-S", str(self.sock), "new-session", "-d", "-s", "live",
                             "-x", "120", "-y", "36", "exec /bin/zsh -f")
        if rc != 0:
            raise unittest.SkipTest("cannot create a disposable tmux server: %s" % out)
        pane = self._tmux("-S", str(self.sock), "list-panes", "-t", "live",
                          "-F", "#{pane_id}")[1].split()[0]
        server_pid = self._tmux("-S", str(self.sock), "display-message", "-p", "#{pid}")[1]
        before = self._windows()
        self.assertTrue(before, "disposable server has no windows to protect")

        home = self.root / "home"
        dd = home / "cache" / "delegation" / "live" / "deleg_spam0001"
        dd.mkdir(parents=True)
        log = dd / "task-0.log"
        log.write_text("08:00:01 tool      | -> terminal(ls)\n", encoding="utf-8")
        (dd / "manifest.json").write_text(json.dumps({
            "delegation_id": "deleg_spam0001",
            "started": "2026-11-13 08:00:00",
            "tasks": [{"index": 0, "goal": "fence-probe", "log": str(log), "status": "running"}],
        }), encoding="utf-8")

        passes = []
        original_reconcile = watch.reconcile_tabs

        def counting_reconcile(k, workers, h=None):
            passes.append(k)
            return original_reconcile(k, workers, h)

        def session_alive(_sid):
            return not passes          # one pass, then exit

        saved = {k: os.environ.get(k) for k in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR")}
        os.environ["TMUX"] = "%s,%s,0" % (self.sock, server_pid)
        os.environ["TMUX_PANE"] = pane
        try:
            with mock.patch.object(watch, "_TICK_S", 0.05), \
                 mock.patch.object(watch, "_MIN_RENDER_INTERVAL_S", 0.0):
                run_mocked_coordinator(home, "fence-probe", session_alive=session_alive,
                                       counting_reconcile=counting_reconcile,
                                       tmux_tmpdir=self.root / "private-tmux")
        finally:
            for k, value in saved.items():
                if value is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = value

        self.assertEqual(len(passes), 1, "coordinator did not reconcile exactly once")
        self.assertEqual(self._windows(), before,
                         "mocked coordinator created windows on the ambient server")


if __name__ == "__main__":
    unittest.main()
