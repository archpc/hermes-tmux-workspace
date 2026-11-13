"""Grouped sessions: one workspace, however many sessions look at its windows.

``tmux new-session -t main`` makes a second session that shares every window of
``main``. A pane then belongs to both, and tmux answers "which session is this
pane in" with whichever was active last. The workspace used to take its identity
from that answer, so ``up`` or ``open`` from a shell could build a second
workspace (second overview, second coordinator) beside the first.

Contract under test:

* the workspace is identified by its own overview window, not by the session a
  command happens to resolve to: ``up``, ``open`` and ``down`` from any session
  of the group act on the one workspace;
* the coordinator is anchored to a session of the group that outlives a view
  coming and going, and re-anchors on ``up`` when its session is gone;
* a tab some attached session of the group is looking at is in view;
* concurrent ``up`` calls build one overview and start one coordinator;
* a marker on a window that is not ours is never believed.

Harness: the one in ``tests/test_tabs.py`` (private server, private HERMES_HOME).
"""

from __future__ import annotations

import os
import subprocess
import time
import unittest
from pathlib import Path

from tests import test_tabs as T
from tests.test_close_mark import Client

_tmux = T._tmux
wait_for = T.wait_for
Sandbox = T.Sandbox


def tearDownModule() -> None:
    T.tearDownModule()


def _state_dirs(box) -> list:
    root = box.home / "cache" / "hermes-tmux" / "sessions"
    return sorted(p.name for p in root.iterdir() if (p / "layout.json").exists()) if root.is_dir() else []


def _overviews(box, session=None) -> list:
    rc, out = _tmux("list-windows", "-t", session or box.name, "-F", "#{window_id} #{window_name}")
    return [line.split()[0] for line in out.splitlines() if line.split()[1:2] and "agents" in line.split(None, 1)[1]]


def _coordinators(box) -> list:
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    pids = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if "-m hermes_tmux watch --key" in command and str(box.home) in _environ_of(pid) and not command.lstrip().startswith("sh -c"):
            pids.append(int(pid))
    return pids


def _environ_of(pid: str) -> str:
    return subprocess.run(["ps", "eww", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout


def _session_id(name: str) -> str:
    return T._format(name, "#{session_id}")


def _direct_env(box) -> dict:
    info = _tmux("display-message", "-p", "#{socket_path},#{pid},0")[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_TMUX") and k != "NO_COLOR"}
    env.update(TMUX=info, TMUX_PANE=box.pane_id, HERMES_HOME=str(box.home), HERMES_TMUX="1",
               PYTHONPATH=str(T.REPO))
    return env


@unittest.skipIf(T.SKIP, "tmux not installed")
class GroupedSessionTest(unittest.TestCase):
    maxDiff = None

    def _up(self, box) -> int:
        """Run `up` and return the coordinator's pid (it starts asynchronously)."""
        rc = box.cli("up")
        self.assertEqual(rc, 0, "up exited %s: %r" % (rc, box.capture()[-600:]))
        self.assertTrue(wait_for(lambda: bool(_coordinators(box)) and box.watcher_pid() is not None, timeout=10),
                        "no coordinator came up")
        return box.watcher_pid() or 0

    def _view(self, box, name=None) -> str:
        """A second session sharing the box's windows. tmux now prefers it for the pane."""
        name = name or (box.name + "_view")
        rc, out = _tmux("new-session", "-d", "-t", box.name, "-s", name)
        self.assertEqual(rc, 0, out)
        self.addCleanup(_tmux, "kill-session", "-t", name)
        self.assertEqual(T._format(box.pane_id, "#{session_name}"), name,
                         "precondition: tmux should resolve the pane to the newer session")
        return name

    # -- identity ----------------------------------------------------------
    def test_up_from_a_grouped_view_is_the_same_workspace(self):
        with Sandbox() as box:
            pid = self._up(box)
            key, overview = box.key(), _overviews(box)
            self.assertEqual(len(overview), 1)
            self._view(box)
            self._up(box)
            time.sleep(1.0)
            self.assertEqual(_state_dirs(box), [key], "a second workspace was keyed")
            self.assertEqual(_overviews(box), overview, "a second overview window was built")
            self.assertEqual(_coordinators(box), [pid], "the coordinator was replaced or doubled")
            self.assertEqual(box.layout().get("session_id"), _session_id(box.name),
                             "the workspace was re-anchored to the view")

    def test_open_and_down_from_a_grouped_view_act_on_the_one_workspace(self):
        with Sandbox() as box:
            box.add_delegation("deleg_grp00001", [{"goal": "grouped open"}])
            self._up(box)
            self.assertTrue(wait_for(lambda: len(box.tabs()) == 1, timeout=20))
            key = box.key()
            wkey = "deleg_grp00001#0"
            self.assertEqual(box.cli("close", box.tabs()[wkey]["window"]), 0)
            self.assertTrue(wait_for(lambda: wkey not in box.tabs(), timeout=10))
            view = self._view(box)
            self.assertEqual(box.cli("open", "grp00001#0"), 0)
            self.assertEqual(_state_dirs(box), [key], "open keyed a second workspace")
            self.assertTrue(wait_for(lambda: (box.tabs().get(wkey) or {}).get("pinned") is True, timeout=15),
                            "open did not record the tab in the one layout: %r" % box.layout())
            user_window = T._pane_window(box.pane_id)
            self.assertEqual(box.cli("down"), 0)
            self.assertTrue(wait_for(lambda: not _coordinators(box), timeout=10), "a coordinator survived down")
            rc, out = _tmux("list-windows", "-t", view, "-F", "#{window_id}")
            self.assertEqual(out.split(), [user_window], "down left windows of ours, or took the reader's")

    # -- lifecycle ---------------------------------------------------------
    def test_a_view_coming_and_going_does_not_stop_the_coordinator(self):
        with Sandbox() as box:
            view = self._view(box)
            pid = self._up(box)                 # built while tmux prefers the view
            key = box.key()
            self.assertEqual(box.layout().get("session_id"), _session_id(box.name),
                             "the workspace is anchored to the view, which is about to go")
            _tmux("kill-session", "-t", view)
            time.sleep(2.5)                     # past a liveness check
            self.assertTrue(T._pid_alive(pid), "the coordinator stopped when a view was closed")
            self._up(box)
            self.assertEqual((_state_dirs(box), box.watcher_pid()), ([key], pid))

    def test_reconnects_do_not_change_the_workspace(self):
        with Sandbox() as box:
            pid = self._up(box)
            key, overview = box.key(), _overviews(box)
            view = self._view(box)
            for target in (box.name, view, box.name, view):
                client = Client(target, box.columns, box.rows)
                try:
                    self.assertTrue(wait_for(lambda: T._format(target, "#{session_attached}") == "1", timeout=10))
                    self._up(box)
                finally:
                    client.close()
                self.assertTrue(wait_for(lambda: T._format(target, "#{session_attached}") == "0", timeout=10))
                self.assertEqual((_state_dirs(box), _overviews(box), box.watcher_pid()), ([key], overview, pid),
                                 "a client on %s changed the workspace" % target)

    def test_the_first_session_can_go_and_the_workspace_carries_on(self):
        with Sandbox() as box:
            box.add_delegation("deleg_grp00002", [{"goal": "survive the first session"}])
            self._up(box)
            self.assertTrue(wait_for(lambda: len(box.tabs()) == 1, timeout=20))
            key, overview, pid = box.key(), _overviews(box), box.watcher_pid() or 0
            view = self._view(box)
            _tmux("kill-session", "-t", box.name)
            self.assertTrue(wait_for(lambda: not T._pid_alive(pid), timeout=15),
                            "the coordinator outlived the session it was anchored to")
            self._up(box)
            self.assertEqual(_state_dirs(box), [key], "the surviving session got a second workspace")
            self.assertEqual(_overviews(box, view), overview)
            self.assertEqual(box.layout().get("session_id"), _session_id(view), "not re-anchored")
            self.assertTrue(wait_for(lambda: len(_coordinators(box)) == 1, timeout=10))
            box.append_event("deleg_grp00002", 0, "tool", "-> AFTER-REANCHOR(x)")
            self.assertTrue(wait_for(lambda: "AFTER-REANCHOR" in box.render_text(), timeout=15),
                            "the overview is not following the workspace any more")
            self.assertEqual(len(box.tabs()), 1, "the open tab was lost: %r" % box.tabs())

    # -- in view -----------------------------------------------------------
    def test_a_tab_watched_from_another_session_of_the_group_is_in_view(self):
        with Sandbox() as box:
            box.export("HERMES_TMUX_LINGER_S", "1")
            box.add_delegation("deleg_grp00003", [{"goal": "watched from the view"}])
            self._up(box)
            self.assertTrue(wait_for(lambda: len(box.tabs()) == 1, timeout=20))
            wkey = "deleg_grp00003#0"
            tab = box.tabs()[wkey]
            key = box.key()
            box.stop_coordinator()
            box.finish("deleg_grp00003", 0)
            view = self._view(box)
            _tmux("select-window", "-t", "%s:%s" % (view, T._window_index(tab["window"])))
            client = Client(view, box.columns, box.rows)
            try:
                self.assertTrue(wait_for(lambda: T._format(view, "#{session_attached}") == "1", timeout=10))
                self.assertNotEqual(T._format(box.name, "#{window_id}"), tab["window"],
                                    "precondition: the first session is not on the tab")
                with T.inproc(box, linger="1"):
                    from hermes_tmux import state, watch
                    home = Path(box.home)
                    workers = state.load_workers(home)
                    t0 = time.time()
                    watch.reconcile_tabs(key, workers, home, now=t0)
                    watch.reconcile_tabs(key, workers, home, now=t0 + 60.0)
                    self.assertIn(wkey, watch.read_layout(key, home).get("tabs") or {},
                                  "a tab on screen in the view was retired")
                    self.assertTrue(T._pane_exists(tab["pane"]))
                    _tmux("select-window", "-t", "%s:%s" % (view, T._window_index(T._pane_window(box.pane_id))))
                    watch.reconcile_tabs(key, workers, home, now=t0 + 180.0)
                    self.assertNotIn(wkey, watch.read_layout(key, home).get("tabs") or {},
                                     "control: out of view everywhere, the tab should retire")
            finally:
                client.close()

    # -- concurrency -------------------------------------------------------
    def test_concurrent_up_builds_one_workspace(self):
        with Sandbox() as box:
            self._view(box)
            env = _direct_env(box)
            procs = [subprocess.Popen([T.PY, "-m", "hermes_tmux", "up"], env=env, cwd=str(T.REPO),
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                     for _ in range(6)]
            outs = [p.communicate(timeout=60)[0] for p in procs]
            self.assertEqual([p.returncode for p in procs], [0] * 6, outs)
            time.sleep(1.5)
            self.assertEqual(len(_state_dirs(box)), 1, _state_dirs(box))
            self.assertEqual(len(_overviews(box)), 1, "overview windows: %r\n%s" % (_overviews(box), "\n".join(outs)))
            self.assertEqual(len(_coordinators(box)), 1, "coordinators: %r" % _coordinators(box))
            rc, out = _tmux("list-keys", "-T", "root", "MouseDown1Status")
            self.assertEqual(out.count("hxc@*"), 1, "the click binding was wrapped more than once: %r" % out)
            self.assertEqual(box.cli("down"), 0)

    # -- adversarial -------------------------------------------------------
    def test_a_marker_on_a_window_that_is_not_ours_is_not_believed(self):
        with Sandbox() as box:
            self._up(box)
            key = box.key()
            self.assertEqual(box.cli("down"), 0)
            # the reader's own window claims to be a workspace, with a well-formed key
            forged = key.rsplit("-", 1)[0] + "-_999"
            _tmux("set-option", "-w", "-t", T._pane_window(box.pane_id), "@hx_key", forged)
            self._up(box)
            self.assertNotIn(forged, _state_dirs(box), "a marker on the reader's window chose the key")
            self.assertEqual(box.key(), key)
            self.assertEqual(box.cli("down"), 0)
            self.assertTrue(T._pane_exists(box.pane_id))


    def test_an_overview_linked_into_another_session_does_not_move_the_anchor(self):
        with Sandbox() as box:
            pid = self._up(box)
            key, anchor = box.key(), _session_id(box.name)
            other = box.name + "_other"
            rc, pane = _tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", other, "exec /bin/zsh -f")
            self.assertEqual(rc, 0, pane)
            self.addCleanup(_tmux, "kill-session", "-t", other)
            _tmux("link-window", "-d", "-s", _overviews(box)[0], "-t", other + ":")
            env = _direct_env(box)
            env["TMUX_PANE"] = pane.strip()
            done = subprocess.run([T.PY, "-m", "hermes_tmux", "up"], env=env, cwd=str(T.REPO),
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertEqual(_state_dirs(box), [key])
            self.assertEqual(box.layout().get("session_id"), anchor, "the anchor moved to the other session")
            self.assertEqual(box.watcher_pid(), pid)
            self.assertEqual(box.cli("down"), 0)


if __name__ == "__main__":
    unittest.main()
