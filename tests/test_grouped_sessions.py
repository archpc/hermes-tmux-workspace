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

Identity, precisely (``tmuxio.session_key`` / ``cli._anchor``):

* the key is ``<server-digest>-<safe session id>`` and is read from the
  ``@hx_key`` option on a window that carries **our own** ``@hx`` marker
  (``tmuxio.marked_key``), matched with a full regex so a key from a previous
  server incarnation is not adopted. Only if no such window exists does the key
  fall back to the group's lowest session id;
* the *anchor* (``layout["session_id"]``) is kept while that session is alive
  and only then replaced by the group's oldest session. It is never the session
  tmux resolved the pane to — that is whichever member was active last, which
  after a ``new-session -t`` is the newest view;
* so "one workspace" means: one state directory under
  ``<HERMES_HOME>/cache/hermes-tmux/sessions/``, one ``agents`` window, one
  coordinator pid. Every test asserts those three, not a log line.

``_coordinators`` filters ``ps`` output rather than trusting ``watch.pid``: the
command line must *contain* ``-m hermes_tmux watch --key`` — a substring test,
not the stricter tail/``endswith`` form used by
``test_attach_bootstrap._processes_for`` — this sandbox's home must appear in
the process environment, and the ``sh -c`` wrapper tmux uses for
``run-shell -b`` is excluded (it carries the same words on its command line).
``_overviews`` matches any window whose name contains ``agents`` because
``tmuxio.free_window_name`` appends ``#N`` when a foreign window already holds
the bare name.

Timing: every wait is a bounded poll, and the fixed sleeps (1.0 s, 1.5 s,
2.5 s) are slack for the coordinator's asynchronous start and for tmux's own
state to settle — they are not the assertion. The assertions are equalities on
(pids, window ids, state directories), so a slow machine makes a test slower,
not wrong, except where noted (``test_concurrent_up_builds_one_workspace``
needs all six ``up`` calls to finish inside their 60 s budget).

Skipped when tmux is missing (``@unittest.skipIf(T.SKIP)``), and the sandbox
raises SkipTest if it cannot create a session.
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
    # Delegate rather than duplicate: T's teardown kills the private server and
    # removes its socket and every temp home this module's sandboxes created.
    T.tearDownModule()


def _state_dirs(box) -> list:
    # The name of every session directory that holds a layout.json. Comparing
    # this list (sorted) is how "a second workspace was keyed" is detected:
    # a duplicate workspace would be a second key, not a second file in one key.
    root = box.home / "cache" / "hermes-tmux" / "sessions"
    return sorted(p.name for p in root.iterdir() if (p / "layout.json").exists()) if root.is_dir() else []


def _overviews(box, session=None) -> list:
    # Window ids of every window whose name contains "agents" — the bare name or
    # "agents#N" after free_window_name appended a suffix. Scanning the session
    # (not the layout) is the point: a duplicate workspace would show up here
    # even if layout.json were rewritten to hide it.
    rc, out = _tmux("list-windows", "-t", session or box.name, "-F", "#{window_id} #{window_name}")
    return [line.split()[0] for line in out.splitlines() if line.split()[1:2] and "agents" in line.split(None, 1)[1]]


def _coordinators(box) -> list:
    # Counted from `ps`, not from watch.pid: the pid file is a hint a restart may
    # not have rewritten yet, and "two coordinators" is exactly the state this
    # file must catch. The environment check (`ps eww`) attributes a process to
    # this sandbox's HERMES_HOME; the `sh -c` exclusion drops tmux's run-shell
    # wrapper, which repeats the same words on its command line.
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    pids = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if "-m hermes_tmux watch --key" in command and str(box.home) in _environ_of(pid) and not command.lstrip().startswith("sh -c"):
            pids.append(int(pid))
    return pids


def _environ_of(pid: str) -> str:
    # `eww` prints the environment after the command line; it is the only way to
    # tell two coordinators on the same private server apart by profile. Errors
    # (a pid that just exited) come back as empty output, i.e. no match.
    return subprocess.run(["ps", "eww", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout


def _session_id(name: str) -> str:
    return T._format(name, "#{session_id}")


def _direct_env(box) -> dict:
    # Environment for `up` run as a child of this process (concurrency and
    # link-window tests) instead of typed into the pane. Snapshot fields come
    # from the sandbox server, and every HERMES_TMUX* variable plus NO_COLOR is
    # dropped from the inherited environment — a stale HERMES_TMUX_* value from
    # the developer's shell would otherwise change the child's behaviour.
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
        # `up` returns before the coordinator has written watch.pid, so the rc
        # is followed by a poll for both "some coordinator process exists" and
        # "the pid file is readable". Reading the pid file alone would race the
        # coordinator's startup; reading ps alone would not know the pid.
        rc = box.cli("up")
        self.assertEqual(rc, 0, "up exited %s: %r" % (rc, box.capture()[-600:]))
        self.assertTrue(wait_for(lambda: bool(_coordinators(box)) and box.watcher_pid() is not None, timeout=10),
                        "no coordinator came up")
        return box.watcher_pid() or 0

    def _view(self, box, name=None) -> str:
        """A second session sharing the box's windows. tmux now prefers it for the pane."""
        # `new-session -t <name>` links every window of the original, so a pane
        # belongs to both sessions and tmux answers "which session" with the
        # most recently active one. The precondition assertion is the fixture
        # checking that this tmux version still behaves that way: if it did not,
        # the test would no longer be exercising the ambiguity it is about.
        name = name or (box.name + "_view")
        rc, out = _tmux("new-session", "-d", "-t", box.name, "-s", name)
        self.assertEqual(rc, 0, out)
        self.addCleanup(_tmux, "kill-session", "-t", name)
        self.assertEqual(T._format(box.pane_id, "#{session_name}"), name,
                         "precondition: tmux should resolve the pane to the newer session")
        return name

    # -- identity ----------------------------------------------------------
    def test_up_from_a_grouped_view_is_the_same_workspace(self):
        # The defect: identity came from the pane's resolved session, so `up`
        # from a view keyed a second workspace with its own overview and its own
        # coordinator. The three equalities below are the contract, and the
        # session_id assertion pins that the anchor did *not* follow the view.
        with Sandbox() as box:
            pid = self._up(box)
            key, overview = box.key(), _overviews(box)
            self.assertEqual(len(overview), 1)
            self._view(box)
            self._up(box)
            # Slack for a wrongly-started second coordinator to appear; a leak
            # that shows up later would still be caught by the pid comparison.
            time.sleep(1.0)
            self.assertEqual(_state_dirs(box), [key], "a second workspace was keyed")
            self.assertEqual(_overviews(box), overview, "a second overview window was built")
            self.assertEqual(_coordinators(box), [pid], "the coordinator was replaced or doubled")
            self.assertEqual(box.layout().get("session_id"), _session_id(box.name),
                             "the workspace was re-anchored to the view")

    def test_open_and_down_from_a_grouped_view_act_on_the_one_workspace(self):
        # The second half of "one workspace": the mutating subcommands. `open`
        # must land in the same layout (same key), and `down` must empty the
        # workspace from *either* session while leaving the reader's own window —
        # the shell the sandbox was created with — standing.
        with Sandbox() as box:
            box.add_delegation("deleg_grp00001", [{"goal": "grouped open"}])
            self._up(box)
            self.assertTrue(wait_for(lambda: len(box.tabs()) == 1, timeout=20))
            key = box.key()
            wkey = "deleg_grp00001#0"
            # Close the tab first so `open` has something to reopen; the tab must
            # leave the layout before `open` can be shown to add it back.
            self.assertEqual(box.cli("close", box.tabs()[wkey]["window"]), 0)
            self.assertTrue(wait_for(lambda: wkey not in box.tabs(), timeout=10))
            view = self._view(box)
            self.assertEqual(box.cli("open", "grp00001#0"), 0)
            self.assertEqual(_state_dirs(box), [key], "open keyed a second workspace")
            # `open` records the tab as pinned; pinned is what keeps a later
            # reconcile from retiring it, so this is the durable part of "reopened".
            self.assertTrue(wait_for(lambda: (box.tabs().get(wkey) or {}).get("pinned") is True, timeout=15),
                            "open did not record the tab in the one layout: %r" % box.layout())
            # The reader's own window is the one the sandbox's pane lives in.
            user_window = T._pane_window(box.pane_id)
            self.assertEqual(box.cli("down"), 0)
            self.assertTrue(wait_for(lambda: not _coordinators(box), timeout=10), "a coordinator survived down")
            # `down` from the view: exactly the user's window must remain. A
            # leftover workspace window means ownership counting is wrong; a
            # missing user window means `down` closed something it did not own.
            rc, out = _tmux("list-windows", "-t", view, "-F", "#{window_id}")
            self.assertEqual(out.split(), [user_window], "down left windows of ours, or took the reader's")

    # -- lifecycle ---------------------------------------------------------
    def test_a_view_coming_and_going_does_not_stop_the_coordinator(self):
        # Anchoring to the newest session would tie the coordinator's life to a
        # session that a reader opens and closes freely. Here the workspace is
        # built while tmux prefers the view, then the view is killed; the
        # coordinator must survive and keep the same key and pid.
        with Sandbox() as box:
            view = self._view(box)
            pid = self._up(box)                 # built while tmux prefers the view
            key = box.key()
            self.assertEqual(box.layout().get("session_id"), _session_id(box.name),
                             "the workspace is anchored to the view, which is about to go")
            _tmux("kill-session", "-t", view)
            # 2.5 s is slack past the point where a wrong anchor would be noticed
            # on a tick; the decisive evidence is the next two assertions, which
            # show the same coordinator still serving the same key.
            time.sleep(2.5)                     # past a liveness check
            self.assertTrue(T._pid_alive(pid), "the coordinator stopped when a view was closed")
            self._up(box)
            self.assertEqual((_state_dirs(box), box.watcher_pid()), ([key], pid))

    def test_reconnects_do_not_change_the_workspace(self):
        # A client attaching and detaching changes which session tmux considers
        # active — `#{session_attached}` and the resolved session move — and that
        # must be invisible to the workspace. Four alternations, in both
        # directions, so a rule that only handled one direction would fail.
        with Sandbox() as box:
            pid = self._up(box)
            key, overview = box.key(), _overviews(box)
            view = self._view(box)
            for target in (box.name, view, box.name, view):
                # Client is a real attach over a pty, not a fake: only a real
                # client makes tmux change its notion of the active session.
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
        # The anchor's own death: the coordinator is tied to the anchor session
        # (`session_alive` on the recorded $N), so it must exit when that session
        # is killed — and `up` from the surviving view must then re-anchor to it
        # while keeping the same key, the same overview and the same open tab.
        # Killing the anchor is also the case where a key derived from the
        # *resolved* session would silently move to a fresh state directory.
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
            # Same window id: the overview was linked, not rebuilt.
            self.assertEqual(_overviews(box, view), overview)
            # `_anchor` had no live recorded session, so it took the group's
            # oldest member — the view is now the only one.
            self.assertEqual(box.layout().get("session_id"), _session_id(view), "not re-anchored")
            self.assertTrue(wait_for(lambda: len(_coordinators(box)) == 1, timeout=10))
            # The re-anchored coordinator must actually be *serving*: a new event
            # has to reach the overview. Without this, "re-anchored" could mean a
            # coordinator pointing at the right session and rendering nothing.
            box.append_event("deleg_grp00002", 0, "tool", "-> AFTER-REANCHOR(x)")
            self.assertTrue(wait_for(lambda: "AFTER-REANCHOR" in box.render_text(), timeout=15),
                            "the overview is not following the workspace any more")
            self.assertEqual(len(box.tabs()), 1, "the open tab was lost: %r" % box.tabs())

    # -- in view -----------------------------------------------------------
    def test_a_tab_watched_from_another_session_of_the_group_is_in_view(self):
        # "In view" is `watch._windows_in_view(session_id)`: the current window
        # of any *attached* client of any session of the group. The test drives
        # reconcile in-process with explicit `now` values, so the linger clock is
        # arithmetic instead of a sleep, and the only real-time element is the
        # attached client.
        #
        # Sequence: the tab is on screen in the view at t0 and at t0+60, where it
        # must survive; then the view is moved to the reader's own shell window
        # (``T._pane_window(box.pane_id)``, not the agents overview), so at
        # t0+180 the tab is out of view everywhere and must retire. Same tab,
        # same pass, only the clock and the visible window change — which is what
        # makes the second assertion a control for the first.
        with Sandbox() as box:
            box.export("HERMES_TMUX_LINGER_S", "1")
            box.add_delegation("deleg_grp00003", [{"goal": "watched from the view"}])
            self._up(box)
            self.assertTrue(wait_for(lambda: len(box.tabs()) == 1, timeout=20))
            wkey = "deleg_grp00003#0"
            tab = box.tabs()[wkey]
            key = box.key()
            # The live coordinator would also run passes and could retire the tab
            # on its own clock; stopping it makes the in-process `now` values the
            # only thing that decides.
            box.stop_coordinator()
            box.finish("deleg_grp00003", 0)
            view = self._view(box)
            # Select the tab's window inside the view by *index*: the window is
            # shared, so the index is the address the session-level target needs.
            _tmux("select-window", "-t", "%s:%s" % (view, T._window_index(tab["window"])))
            client = Client(view, box.columns, box.rows)
            try:
                self.assertTrue(wait_for(lambda: T._format(view, "#{session_attached}") == "1", timeout=10))
                # Precondition: the *first* session is not on the tab, so being
                # in view can only come from the view. Otherwise the test would
                # pass for the wrong reason.
                self.assertNotEqual(T._format(box.name, "#{window_id}"), tab["window"],
                                    "precondition: the first session is not on the tab")
                with T.inproc(box, linger="1"):
                    from hermes_tmux import state, watch
                    home = Path(box.home)
                    workers = state.load_workers(home)
                    t0 = time.time()
                    watch.reconcile_tabs(key, workers, home, now=t0)
                    # 60 s later, still in view: linger (1 s) has passed, so only
                    # the in-view rule can keep it. This is the positive case.
                    watch.reconcile_tabs(key, workers, home, now=t0 + 60.0)
                    self.assertIn(wkey, watch.read_layout(key, home).get("tabs") or {},
                                  "a tab on screen in the view was retired")
                    self.assertTrue(T._pane_exists(tab["pane"]))
                    # Move the view off the tab, then 120 s more: out of view
                    # everywhere and long past linger, it must retire. The control.
                    _tmux("select-window", "-t", "%s:%s" % (view, T._window_index(T._pane_window(box.pane_id))))
                    watch.reconcile_tabs(key, workers, home, now=t0 + 180.0)
                    self.assertNotIn(wkey, watch.read_layout(key, home).get("tabs") or {},
                                     "control: out of view everywhere, the tab should retire")
            finally:
                client.close()

    # -- concurrency -------------------------------------------------------
    def test_concurrent_up_builds_one_workspace(self):
        # Six real `up` processes at once, from a grouped view so the identity
        # path is the ambiguous one too. The serialisation is `cli._server_lock`:
        # a lock beside the socket (one `up` per server, because the click
        # binding and the overview are server-wide check-then-act facts) taken
        # before a per-HERMES_HOME `up.lock`. Without it every process can see
        # "no overview" and build its own.
        with Sandbox() as box:
            self._view(box)
            env = _direct_env(box)
            procs = [subprocess.Popen([T.PY, "-m", "hermes_tmux", "up"], env=env, cwd=str(T.REPO),
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                     for _ in range(6)]
            # 60 s each, and all six must exit 0: a lock timeout would be a
            # different failure from a race, and the outputs are included in the
            # message so the two can be told apart from a CI log.
            outs = [p.communicate(timeout=60)[0] for p in procs]
            self.assertEqual([p.returncode for p in procs], [0] * 6, outs)
            # Slack for a late loser to finish creating something before counting.
            time.sleep(1.5)
            self.assertEqual(len(_state_dirs(box)), 1, _state_dirs(box))
            self.assertEqual(len(_overviews(box)), 1, "overview windows: %r\n%s" % (_overviews(box), "\n".join(outs)))
            self.assertEqual(len(_coordinators(box)), 1, "coordinators: %r" % _coordinators(box))
            # The status-bar click binding is a server-wide `bind-key`; it must be
            # installed once, not once per concurrent `up`. Counting the marker is
            # how "wrapped more than once" is observed without running a click.
            rc, out = _tmux("list-keys", "-T", "root", "MouseDown1Status")
            self.assertEqual(out.count("hxc@*"), 1, "the click binding was wrapped more than once: %r" % out)
            self.assertEqual(box.cli("down"), 0)

    # -- adversarial -------------------------------------------------------
    def test_a_marker_on_a_window_that_is_not_ours_is_not_believed(self):
        # `@hx_key` is only read from windows that carry our own `@hx` marker
        # (`tmuxio.marked_key` filters on `win["ours"]` first). The forgery here
        # is a well-formed key — correct digest prefix, plausible id — on the
        # reader's own shell window, i.e. an attacker who knows the format. The
        # guard that stops it is ownership, not format; the digest regex is a
        # second line of defence against a key from a previous server.
        with Sandbox() as box:
            self._up(box)
            key = box.key()
            self.assertEqual(box.cli("down"), 0)
            # the reader's own window claims to be a workspace, with a well-formed key
            forged = key.rsplit("-", 1)[0] + "-_999"
            _tmux("set-option", "-w", "-t", T._pane_window(box.pane_id), "@hx_key", forged)
            self._up(box)
            self.assertNotIn(forged, _state_dirs(box), "a marker on the reader's window chose the key")
            # And the genuine key is recovered by the documented fallback, not by
            # the forgery: one session group, so the lowest session id is the key.
            self.assertEqual(box.key(), key)
            self.assertEqual(box.cli("down"), 0)
            # The reader's window — the one carrying the forged option — is still
            # there. A `down` that "cleaned up" the forged workspace would have
            # taken it with it.
            self.assertTrue(T._pane_exists(box.pane_id))


    def test_an_overview_linked_into_another_session_does_not_move_the_anchor(self):
        # `link-window` makes the overview visible from an unrelated session
        # (which then shares that one window, so it joins the "group" for
        # addressing purposes). Identity still comes from the marked window, and
        # `_anchor` keeps the recorded session while it lives. Without that rule
        # the anchor — and the coordinator's lifetime — would follow whichever
        # session the operator happened to be in.
        with Sandbox() as box:
            pid = self._up(box)
            key, anchor = box.key(), _session_id(box.name)
            other = box.name + "_other"
            rc, pane = _tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", other, "exec /bin/zsh -f")
            self.assertEqual(rc, 0, pane)
            self.addCleanup(_tmux, "kill-session", "-t", other)
            _tmux("link-window", "-d", "-s", _overviews(box)[0], "-t", other + ":")
            env = _direct_env(box)
            # `up` as if run from a shell inside `other`: its pane is the new
            # session's pane, which is what makes tmux resolve the identity
            # question towards `other`.
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
