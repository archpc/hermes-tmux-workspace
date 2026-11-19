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

What "exit" means here, precisely, because the two tests below pin different
mechanisms:

* session loss is detected by ``tmuxio.session_alive(session_id)``, which
  resolves ``#{session_id}`` and requires the resolved id to *equal* the
  recorded ``$N``. A grouped-view target (a name) only proves tmux answered
  something, so the coordinator always passes the id it recorded.
* server replacement is detected by comparing the recorded server pid with the
  one answering now (``tmuxio.server_pid``). Identity is the pid because window
  ids are only unique per server; a new server starts its sequence over.
* either check runs on the coordinator's idle cadence
  (``watch._SESSION_CHECK_S`` = 20 s), so both tests budget tens of seconds and
  assert "eventually gone", never a duration.

Isolation, so this module can never touch the user's server or state:

* most tmux calls go through ``tests.test_tmux_integration._tmux``, which pins
  ``tmux -L hx_it_<pid> -f /dev/null``: a private socket and an empty config, so
  neither the developer's server nor their tmux.conf can influence the outcome.
  The exception is ``MockedCoordinatorTmuxFenceTest``, whose own ``_tmux``
  helper passes ``-S <absolute socket under a TemporaryDirectory>`` and no
  config pin; an absolute ``-S`` path cannot be redirected by ``$TMUX_TMPDIR``,
  so its reads stay inside the disposable server;
* each ``Sandbox`` owns a private ``HERMES_HOME`` under a TemporaryDirectory;
* the coordinator processes under test are started by ``up`` through
  ``tmux run-shell -b`` inside the sandbox, so they inherit the sandbox's
  profile and socket — not this process's;
* ``_tmux`` is addressed by pane/window id, never by index, because indices are
  configurable and get renumbered.

The one process started *directly* (ForeignWindowGuardTest) is given a
hand-built environment pointing at the sandbox socket, with TMUX/TMUX_PANE
stripped first so an ambient value cannot redirect it.

Skipped implicitly: the module needs tmux and a working ``ps``/``lsof``; the
``Sandbox`` raises SkipTest when a session cannot be created.
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
    # Two whole workspaces, so the server outlives the session: `keeper` proves
    # the server still answers after `victim` is killed, which is the exact
    # condition the old exit-status check could not see. If the server went
    # down, the test would pass for the wrong reason, so that is asserted too
    # (`list-sessions` rc 0) before waiting for the coordinator to go.

    def test_coordinator_exits_when_its_session_is_closed(self):
        with Sandbox() as keeper, Sandbox() as victim:
            keeper.add_delegation("deleg_keep0001", [{"goal": "stay"}])
            victim.add_delegation("deleg_vict0001", [{"goal": "go"}])
            self.assertEqual(keeper.cli("up"), 0)
            self.assertEqual(victim.cli("up"), 0)

            # One session directory per workspace; taking the only one is safe
            # because each Sandbox has its own HERMES_HOME.
            key = next((d.name for d in
                        (victim.home / "cache/hermes-tmux/sessions").iterdir()), "")
            self.assertTrue(key, "no session directory was created")
            # watch.pid is written by the coordinator once it holds watch.lock,
            # a moment after `up` returns — hence the poll, not a read.
            pid_file = victim.home / "cache/hermes-tmux/sessions" / key / "watch.pid"
            self.assertTrue(wait_for(pid_file.exists, timeout=10), "no coordinator started")
            pid = int(pid_file.read_text().strip())
            self.assertTrue(_pid_alive(pid))

            _tmux("kill-session", "-t", victim.name)
            # The sandbox must not try to kill a session it no longer owns; the
            # flag makes __exit__ a no-op without asserting anything.
            victim.created = False
            self.assertEqual(_tmux("list-sessions")[0], 0,
                             "the server itself died; this test needs it alive")
            # 30 s covers one _SESSION_CHECK_S (20 s) idle cadence plus the
            # process exit; the assertion is "eventually", not "within 20 s".
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
        # kill -9 is the point of the test: the coordinator survives its server
        # (no SIGHUP, no session-gone answer), so the only thing that can stop
        # it is the pid comparison. A graceful kill-server would test the other
        # path in the test above.
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
            # Snapshot the user's window before the coordinator can act: name and
            # pane list together are what "untouched" means here. The pane list
            # catches a split, the name catches a rename; neither alone covers
            # both mutations.
            win = _tmux("list-windows", "-t", "reborn", "-F", "#{window_id}")[1].split()[0]
            before = _tmux("list-panes", "-t", win, "-F", "#{pane_id}")[1].split()

            # 45 s: the old coordinator may be asleep in its idle wait when the
            # server is replaced, so the check lands on the next tick.
            self.assertTrue(wait_for(lambda: not _pid_alive(pid), timeout=45),
                            "the old coordinator kept serving a replaced server (pid %s)" % pid)

            # Read back after the coordinator is gone: checking earlier would
            # race the very window the test exists to close.
            name = _tmux("display-message", "-p", "-t", win, "#{window_name}")[1]
            after = _tmux("list-panes", "-t", win, "-F", "#{pane_id}")[1].split()
            self.assertEqual(name, "notes", "the crashed server's coordinator renamed a user window")
            self.assertEqual(after, before, "the crashed server's coordinator split into a user window")


class ForeignWindowGuardTest(unittest.TestCase):
    # A stale layout is the hostile input, not an accident: both stale
    # references (`workers_window` and a tab record) point at the user's window,
    # which is what a recycled id looks like. The user's window is deliberately
    # an interactive shell with no marker, so nothing about it says "ours".

    def test_reconcile_never_adopts_a_foreign_window(self):
        with Sandbox() as box:
            box.add_delegation("deleg_guard001", [{"goal": "guard"}])
            self.assertEqual(box.cli("up"), 0)
            key = next(d.name for d in (box.home / "cache/hermes-tmux/sessions").iterdir())
            layout_path = box.home / "cache/hermes-tmux/sessions" / key / "layout.json"
            saved = layout_path.read_text()
            # `down` tears the workspace down *and* removes the state dir, so the
            # layout is captured first and re-planted afterwards. That is exactly
            # the "layout from a dead server" situation, with a live server.
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

            # Called in a separate process, like the coordinator: reconcile_tabs
            # writes layout.json and calls tmux, and the test wants the same code
            # path the coordinator runs, not an in-process variant that could
            # share already-read state.
            code = ("from hermes_tmux import watch, state\n"
                    "watch.reconcile_tabs(%r, state.load_workers(), None)\n" % key)
            info = _tmux("display-message", "-p", "#{socket_path}|#{pid}")[1]
            sock, server = info.split("|")
            import os as _os, subprocess as _sp
            # Same shape as the harness's env: point the child at the sandbox
            # socket, with the ambient TMUX/TMUX_PANE removed so an inherited
            # value cannot redirect it to the developer's server.
            env = {k: v for k, v in _os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
            env.update(TMUX="%s,%s,0" % (sock, server), TMUX_PANE=box.pane_id,
                       HERMES_HOME=str(box.home), HERMES_TMUX="1", PYTHONPATH=str(REPO))
            done = _sp.run([PY, "-c", code], env=env, cwd=str(REPO),
                           capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr[-400:])

            # Three independent observables of "did not touch it": the name (a
            # rename), the pane list (a split), and the absence of our marker
            # (an adoption). `show-options` on a window with no @hx prints
            # "invalid option: @hx" on some tmux versions and an empty line on
            # others, so both spellings are accepted — anything else means we
            # stamped it.
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
    # Defined mid-file, after the last tmux-using class rather than at the
    # bottom: unittest only requires the name to exist on the module, so its
    # position here is legal and keeps it next to the tests it tears down.
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

    # The disposable server is addressed with `-S <path>` (an absolute socket
    # path in a TemporaryDirectory), not `-L name`: an absolute path cannot be
    # redirected by $TMUX_TMPDIR, so the assertion's own reads are unaffected by
    # whatever the fence does to the environment.
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hx_fence_")
        self.root = Path(self._tmp.name)
        self.sock = self.root / "sock"

    def tearDown(self) -> None:
        # kill-server on the disposable socket; nothing here may touch the
        # harness's server, which other tests still need.
        self._tmux("-S", self.sock, "kill-server")
        self._tmp.cleanup()

    def _tmux(self, *args: str):
        proc = subprocess.run([TMUX, *args], capture_output=True, text=True, timeout=15)
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    def _windows(self):
        # An empty list on failure reads as "no windows", which is the direction
        # that would make an assertion pass for the wrong reason — so the test
        # asserts `before` is non-empty first, proving the read works.
        rc, out = self._tmux("-S", str(self.sock), "list-windows", "-a",
                             "-F", "#{session_name}:#{window_index} #{window_name}")
        return out.splitlines() if rc == 0 else []

    def test_mocked_coordinator_creates_no_windows_on_the_ambient_server(self) -> None:
        # Skip rather than fail when the disposable server cannot start: the test
        # is about our fence, not about this machine's ability to run tmux, and a
        # hard failure here would mask real regressions. Every other assertion
        # in the method is fatal.
        rc, out = self._tmux("-S", str(self.sock), "new-session", "-d", "-s", "live",
                             "-x", "120", "-y", "36", "exec /bin/zsh -f")
        if rc != 0:
            raise unittest.SkipTest("cannot create a disposable tmux server: %s" % out)
        pane = self._tmux("-S", str(self.sock), "list-panes", "-t", "live",
                          "-F", "#{pane_id}")[1].split()[0]
        server_pid = self._tmux("-S", str(self.sock), "display-message", "-p", "#{pid}")[1]
        before = self._windows()
        self.assertTrue(before, "disposable server has no windows to protect")

        # One running delegation is enough: reconciliation creates one worker
        # window per running task, so a leaked pass would add exactly one window
        # and the comparison below would see it.
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
            # Counts passes while leaving the real implementation in place: the
            # three-argument wrapper also pins the call signature the coordinator
            # uses (key, workers, home).
            passes.append(k)
            return original_reconcile(k, workers, h)

        def session_alive(_sid):
            # One pass, then exit. `len(passes)` is read *after* the first
            # reconcile returns, so the coordinator's next liveness check fails
            # and run_watch returns — which is what keeps the test finite
            # without killing a process.
            return not passes

        # Set the ambient environment the way a developer's terminal would look,
        # pointing at the disposable ("live") server. This is the hostile input:
        # run_mocked_coordinator's fence must strip it. Restoration is explicit
        # and covers the "was unset" case so the module cannot leak TMUX into
        # another test's process.
        saved = {k: os.environ.get(k) for k in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR")}
        os.environ["TMUX"] = "%s,%s,0" % (self.sock, server_pid)
        os.environ["TMUX_PANE"] = pane
        try:
            # 0.05 s tick and no render floor: the loop must reach its first
            # reconcile quickly, and the assertions below are about windows, not
            # about cadence. Both are restored by mock.patch on exit.
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

        # Exactly one pass is part of the fixture's contract: zero would mean the
        # loop never reconciled (so the window comparison proves nothing), two
        # would mean the exit path is not exercised. Then the real assertion.
        self.assertEqual(len(passes), 1, "coordinator did not reconcile exactly once")
        self.assertEqual(self._windows(), before,
                         "mocked coordinator created windows on the ambient server")


if __name__ == "__main__":
    unittest.main()
