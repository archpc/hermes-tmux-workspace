"""Concurrency and isolation, on real sessions of one private tmux server.

This suite reuses the harness in ``test_tmux_integration`` (pid-scoped server
name, disposable HERMES_HOME per sandbox, assertions read what tmux painted and
what the process wrote) instead of copying it, so nothing here can reach the
user's default socket and no real Hermes state is read or written.

What is pinned:

* two sessions on one server keep disjoint state directories, disjoint panes,
  and disjoint coordinators; ``down`` in one is not felt by the other;
* a server restart never adopts the previous incarnation's layout or panes;
* detaching a real client leaves the panes and the coordinator untouched.

Every reading of a layout goes through the session key the product itself
computes (``tmuxio.session_key()``) rather than "the one directory in
``sessions/``": after a restart two layouts coexist under one HERMES_HOME, and
picking whichever ``iterdir()`` yields first would compare a layout with itself.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import struct
import subprocess
import termios
import unittest
from pathlib import Path
from typing import Dict, Optional, Set

from tests.test_tmux_integration import (
    PY,
    REPO,
    SKIP,
    SOCKET,
    TMUX,
    Sandbox,
    _pid_alive,
    _q,
    _tmux,
    wait_for,
)
from tests.test_tmux_integration import tearDownModule as _harness_teardown

#: Delegation ids used here carry this tag so a title from the wrong session is
#: never mistaken for the right one by an accidental prefix match.
TAG_A = "conc00001"
TAG_B = "conc00002"
TAG_C = "conc00003"
TAG_D = "conc00004"

#: ``"<delegation> t<index> <state> <model>"`` is the border label
#: (``watch.pane_label``); only the leading delegation token is identity.
_LABEL = re.compile(r"^([A-Za-z0-9_]+) t[0-9]+ ")

#: Pane ids are handed out monotonically per server but start again at ``%0``
#: after a restart, so a numeric id can recur without any pane being adopted.
#: The restart test burns this many ids first, which puts its recorded ids far
#: above anything a freshly started server allocates in this test.
_BURN = 24


def tearDownModule():
    """Kill only this process's private test server, never the default socket.

    The harness owns the socket name and the cleanup rule (pid-scoped name,
    ``kill-server`` allowed there and nowhere else), so its teardown is reused
    rather than reimplemented; it also removes every temp home it handed out.
    """
    _harness_teardown()


# --------------------------------------------------------------- small readers

def _sessions_dir(box: Sandbox) -> Path:
    return box.home / "cache" / "hermes-tmux" / "sessions"


def _session_key(box: Sandbox) -> str:
    """The key the product computes for this pane's session, not a directory guess."""
    code = "from hermes_tmux import tmuxio; print('KEY', tmuxio.session_key())"
    box.run("%s -c %s" % (PY, _q(code)))
    painted = box.capture()
    found = [line.split("KEY ", 1)[1].strip()
             for line in painted.splitlines() if "KEY " in line]
    if not found:
        raise AssertionError("session_key() printed nothing: %r" % painted[-400:])
    return found[-1]


def _layout_at(box: Sandbox, key: str) -> Dict:
    path = _sessions_dir(box) / key / "layout.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _layout_panes(layout: Dict) -> Set[str]:
    """Pane ids a layout addresses: the overview plus every tail pane."""
    panes = set((layout.get("tail_panes") or {}).keys())
    if layout.get("overview_pane"):
        panes.add(layout["overview_pane"])
    return panes


def _watch_pid_at(box: Sandbox, key: str) -> Optional[int]:
    path = _sessions_dir(box) / key / "watch.pid"
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pane_pid(pane_id: str) -> str:
    """``#{pane_pid}``, or ``''`` when tmux cannot resolve the id to a pane.

    ``display-message -t <missing>`` exits 0 and prints nothing on tmux 3.8, so
    the resolved value is the authority, never the exit status.
    """
    return _tmux("display-message", "-p", "-t", pane_id, "#{pane_pid}")[1].strip()


def _server_pid(pane_id: str) -> str:
    return _tmux("display-message", "-p", "-t", pane_id, "#{pid}")[1].strip()


def _titled_workers(box: Sandbox, window_id: str) -> Set[str]:
    """Delegation tokens carried by the border titles in one window."""
    out = set()
    for row in box.panes_of(window_id):
        match = _LABEL.match(row["title"].strip())
        if match:
            out.add(match.group(1))
    return out


def _client_ttys() -> str:
    return _tmux("list-clients", "-F", "#{client_tty}")[1].strip()


def _client_sessions() -> str:
    return _tmux("list-clients", "-F", "#{client_session}")[1].strip()


# --------------------------------------------------------------- sandbox extras

def _spawn(home: Path, tag: str = "") -> Sandbox:
    """A second session on the private server, using ``home`` as HERMES_HOME.

    Built on the harness's Sandbox rather than by hand so the env exports, the
    pane id and the cleanup all match every other test here.
    """
    box = Sandbox()
    box.home = home
    box._tmp = home
    name = box.name + tag
    rc, out = _tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name,
                    "-x", str(box.columns), "-y", str(box.rows),
                    "-c", str(REPO), "exec /bin/zsh -f")
    if rc != 0:
        raise AssertionError("cannot create a second tmux session: %s" % out)
    box.name = name
    box.pane_id = out.strip().splitlines()[-1].strip()
    box.created = True
    # Two sandboxes sharing one HERMES_HOME would also share the marker names
    # Sandbox.run() writes (rc_1, rc_2, ...), and the second one would read the
    # first one's rc file. Offset this session's counter out of that range.
    box.marker_seq = 1000
    box.run("export HERMES_HOME=%s" % home)
    box.run("export HERMES_TMUX=1")
    box.run("export PYTHONPATH=%s" % REPO)
    return box


def _burn_pane_ids(box: Sandbox, count: int) -> None:
    """Consume ``count`` pane ids in this server, then give the panes back.

    Ids are not recycled, so the panes created afterwards are numbered well past
    anything a fresh server can produce.
    """
    for index in range(count):
        rc, out = _tmux("new-window", "-d", "-P", "-F", "#{pane_id}",
                        "-t", box.name, "-n", "burn%d" % index, "exec /bin/zsh -f")
        if rc != 0:
            raise AssertionError("could not burn a pane id: %s" % out)
        _tmux("kill-pane", "-t", out.strip().splitlines()[-1].strip())


@unittest.skipIf(SKIP, "tmux not installed")
class ConcurrencyIsolationTest(unittest.TestCase):
    maxDiff = None

    # ---- 1. two sessions, one server ------------------------------------
    def test_two_sessions_same_server_are_isolated(self):
        with Sandbox() as a, Sandbox() as b:
            self.assertEqual(_server_pid(a.pane_id), _server_pid(b.pane_id),
                             "the two sandboxes are not on one server")
            self.assertNotEqual(a.name, b.name)

            a.add_delegation(TAG_A, [{"goal": "alpha work"}])
            b.add_delegation(TAG_B, [{"goal": "beta work"}])
            self.assertEqual(a.cli("up"), 0)
            self.assertEqual(b.cli("up"), 0)

            self.assertTrue(wait_for(lambda: "alpha work" in a.render_text(), timeout=15),
                            "session a never rendered its own worker")
            self.assertTrue(wait_for(lambda: "beta work" in b.render_text(), timeout=15),
                            "session b never rendered its own worker")
            self.assertNotIn("beta work", a.render_text(),
                             "session a rendered session b's worker")
            self.assertNotIn("alpha work", b.render_text(),
                             "session b rendered session a's worker")

            # Two state directories, not one shared one. The key's first half is
            # the socket+server-pid digest, so equal halves prove both sessions
            # are talking to the same server instance and the isolation comes
            # from the session, not from two accidental servers.
            key_a, key_b = _session_key(a), _session_key(b)
            self.assertNotEqual(key_a, key_b,
                                "two sessions on one server share a state directory")
            self.assertEqual(key_a.split("-")[0], key_b.split("-")[0],
                             "the two sessions are not keyed on one server instance")
            self.assertTrue((_sessions_dir(a) / key_a / "layout.json").exists(),
                            "session a's layout is not under its own key")
            self.assertTrue((_sessions_dir(b) / key_b / "layout.json").exists(),
                            "session b's layout is not under its own key")
            self.assertFalse((_sessions_dir(a) / key_b).exists(),
                             "session a's home holds session b's directory")

            win_a, win_b = a.window("hx:workers"), b.window("hx:workers")
            self.assertIsNotNone(win_a, "session a has no workers window")
            self.assertIsNotNone(win_b, "session b has no workers window")
            self.assertNotEqual(win_a["id"], win_b["id"])

            self.assertTrue(wait_for(lambda: _titled_workers(a, win_a["id"]) == {TAG_A},
                                     timeout=15),
                            "session a's window is not titled for its own worker: %r"
                            % _titled_workers(a, win_a["id"]))
            self.assertTrue(wait_for(lambda: _titled_workers(b, win_b["id"]) == {TAG_B},
                                     timeout=15),
                            "session b's window is not titled for its own worker: %r"
                            % _titled_workers(b, win_b["id"]))
            layout_a, layout_b = _layout_at(a, key_a), _layout_at(b, key_b)
            self.assertNotEqual(layout_a.get("overview_pane"), layout_b.get("overview_pane"),
                                "both sessions address the same overview pane")
            self.assertEqual(_layout_panes(layout_a) & _layout_panes(layout_b), set(),
                             "the two layouts address a shared pane")

            # down in a must be a private act.
            panes_before = {row["id"]: _pane_pid(row["id"])
                            for row in b.panes_of(win_b["id"])}
            self.assertTrue(panes_before, "session b has no panes to preserve")
            self.assertTrue(wait_for(lambda: _watch_pid_at(b, key_b) is not None, timeout=10))
            coordinator_b = _watch_pid_at(b, key_b)
            self.assertTrue(_pid_alive(coordinator_b))

            self.assertEqual(a.cli("down"), 0)
            self.assertTrue(wait_for(lambda: a.window("hx:workers") is None),
                            "down left session a's window standing")
            self.assertIsNotNone(b.window("hx:workers"),
                                 "down in one session closed the other session's window")
            for pane_id, pid in panes_before.items():
                self.assertEqual(_pane_pid(pane_id), pid,
                                 "down in one session disturbed pane %s of the other"
                                 % pane_id)
            self.assertTrue(_pid_alive(coordinator_b),
                            "down in one session killed the other session's coordinator")
            self.assertEqual(_watch_pid_at(b, key_b), coordinator_b)

    # ---- 2. restart ------------------------------------------------------
    def test_restart_does_not_adopt_old_panes(self):
        box = Sandbox()
        with box:
            _burn_pane_ids(box, _BURN)
            box.add_delegation(TAG_C, [{"goal": "restart me"}])
            self.assertEqual(box.cli("up"), 0)

            key_before = _session_key(box)
            self.assertTrue(wait_for(
                lambda: len(_layout_at(box, key_before).get("tail_panes") or {}) == 1,
                timeout=15), "no tail pane was built before the restart")
            old_layout = _layout_at(box, key_before)
            old_panes = _layout_panes(old_layout)
            self.assertTrue(old_panes)
            # The burn must have taken effect, or the disjointness asserted at
            # the end could not tell adoption from a reused number.
            self.assertGreaterEqual(min(int(p[1:]) for p in old_panes), _BURN + 1,
                                    "the pane-id burn did not take effect: %r" % old_panes)
            old_pids = {pane: _pane_pid(pane) for pane in old_panes}

            # Only this suite's pid-scoped private server is ever killed here.
            killed = subprocess.run([TMUX, "-L", SOCKET, "kill-server"],
                                    capture_output=True, text=True, timeout=15)
            self.assertIn(killed.returncode, (0, 1), killed.stderr)
            box.created = False  # the session died with its server

            fresh = _spawn(box.home, "_r")  # same HERMES_HOME, new server instance
            try:
                self.assertEqual(fresh.cli("up"), 0)
                key_after = _session_key(fresh)
                self.assertTrue(wait_for(
                    lambda: len(_layout_at(fresh, key_after).get("tail_panes") or {}) == 1,
                    timeout=15), "no tail pane was built after the restart")
                new_layout = _layout_at(fresh, key_after)
                new_panes = _layout_panes(new_layout)

                self.assertNotEqual(key_after, key_before,
                                    "a new server instance reused the old session key")
                # The old layout is still on disk, untouched...
                self.assertTrue((_sessions_dir(fresh) / key_before / "layout.json").exists(),
                                "the pre-restart layout was removed rather than ignored")
                self.assertEqual(_layout_at(fresh, key_before), old_layout,
                                 "the pre-restart layout was rewritten by the new session")
                # ...and none of its panes was adopted into the new one.
                self.assertTrue(new_panes, "the new layout addresses no pane")
                self.assertEqual(new_panes & old_panes, set(),
                                 "the new session adopted panes from the old layout: %r"
                                 % (new_panes & old_panes))
                for pane_id, pid in old_pids.items():
                    self.assertNotEqual(_pane_pid(pane_id), pid,
                                        "pane %s survived the restart" % pane_id)
                for pane_id in new_panes:
                    self.assertTrue(_pane_pid(pane_id),
                                    "the new layout addresses dead pane %s" % pane_id)
            finally:
                _tmux("kill-session", "-t", fresh.name)
                fresh.created = False

    # ---- 3. detach / reattach -------------------------------------------
    def test_detach_reattach_keeps_panes_and_coordinator(self):
        with Sandbox() as box:
            box.add_delegation(TAG_D, [{"goal": "keep me"}])
            self.assertEqual(box.cli("up"), 0)
            key = _session_key(box)
            self.assertTrue(wait_for(
                lambda: len(_layout_at(box, key).get("tail_panes") or {}) == 1,
                timeout=15), "no tail pane was built")
            before = _layout_at(box, key)
            before_panes = _layout_panes(before)
            self.assertTrue(before_panes)
            pids_before = {pane: _pane_pid(pane) for pane in before_panes}
            self.assertTrue(all(pids_before.values()), pids_before)

            self.assertTrue(wait_for(lambda: _watch_pid_at(box, key) is not None, timeout=10))
            coordinator = _watch_pid_at(box, key)
            self.assertTrue(_pid_alive(coordinator))

            # A real attach needs a real tty and TERM=xterm-256color; the pty is
            # sized to the session so attaching cannot resize the layout.
            child_env = {k: v for k, v in os.environ.items()
                         if k not in ("TMUX", "TMUX_PANE")}
            child_env["TERM"] = "xterm-256color"
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ,
                        struct.pack("HHHH", box.rows, box.columns, 0, 0))
            client = subprocess.Popen(
                [TMUX, "-L", SOCKET, "attach", "-t", box.name],
                stdin=slave, stdout=slave, stderr=slave, env=child_env)
            os.close(slave)
            try:
                self.assertTrue(wait_for(lambda: _client_ttys() != "", timeout=10),
                                "could not attach a test client")
                self.assertIn(box.name, _client_sessions(),
                              "the client attached to another session")
                _tmux("detach-client", "-s", box.name)
                self.assertTrue(wait_for(lambda: _client_ttys() == "", timeout=10),
                                "detach did not remove the client")
            finally:
                client.terminate()
                try:
                    client.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    client.kill()
                os.close(master)

            after = _layout_at(box, key)
            self.assertEqual(after.get("workers_window"), before.get("workers_window"))
            self.assertEqual(after.get("overview_pane"), before.get("overview_pane"))
            self.assertEqual(after.get("tail_panes"), before.get("tail_panes"))
            for pane_id, pid in pids_before.items():
                self.assertEqual(_pane_pid(pane_id), pid,
                                 "detach replaced pane %s" % pane_id)
            self.assertTrue(_pid_alive(coordinator),
                            "the coordinator died when the client detached")
            self.assertEqual(_watch_pid_at(box, key), coordinator,
                             "detach started a second coordinator")
            self.assertTrue(wait_for(lambda: "keep me" in box.render_text(), timeout=10))


if __name__ == "__main__":
    unittest.main(verbosity=2)
