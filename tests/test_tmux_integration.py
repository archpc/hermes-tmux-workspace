"""End-to-end tests against real, isolated tmux sessions.

Rules enforced by this harness:

* every session it creates is named ``hx_it_<pid>_<n>`` and is killed in a
  ``finally``; the harness refuses to touch any other session, so a user
  session (``main``) can never be listed, renamed, or killed here;
* every test owns a private ``HERMES_HOME`` under a TemporaryDirectory, so no
  real Hermes state is read or written;
* assertions read what tmux actually painted (``capture-pane``), what a pane's
  own options say (``show-options -p``), or what the process actually wrote
  (layout/render files), never a claimed value.

The contract these tests pin is the v3 tab model (one worker, one window, one
pane) as implemented by ``hermes_tmux/watch.py:reconcile_tabs``:

* ``up`` owns two static workspace windows, ``agents`` (``cli.WORKERS_WINDOW``)
  and ``hx:mail`` (permanent local mail reader),
  holding one pane: the overview;
* a RUNNING worker gets one window of its own, named from its goal, one pane
  running the styled follower (``python -m hermes_tmux tail <log>``), and a
  record in ``layout["tabs"]``;
* work that had already finished before ``up`` gets no tab; a tab that finishes
  afterwards keeps its window and pane and only changes glyph/header;
* identity and state are not in ``#{pane_title}``: the pane's own
  ``@hx_head`` option carries state word and task, the window's ``@hx_glyph``
  option carries the state glyph;
* ownership is the pane's/window's OWN ``@hx`` option, never a name and never a
  format read that falls back through session/window;
* ``down`` closes only panes it can prove it started, leaves any window that
  still holds a user pane standing, and removes its state directory.

Skipped entirely when tmux is unavailable.
"""

from __future__ import annotations

import json
import os
import pty
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
_TMUX_PATH = shutil.which("tmux")
TMUX = _TMUX_PATH or "tmux"
PY = sys.executable or "python3"
SKIP = _TMUX_PATH is None

#: Every call in this suite targets a private server, never the user's default
#: socket. The name is derived from the test process id so concurrent runs of
#: this file cannot collide with each other either.
SOCKET = "hx_it_%d" % os.getpid()

#: The suite runs with an empty config on purpose: it exercises our code, so the
#: user's ~/.config/tmux/tmux.conf (base-index 1, per-window options, hooks) must
#: not be able to change the outcome. Addressing is by pane/window id, never index.
CONFIG = "/dev/null"

#: The one window the workspace owns that is not a worker tab.
AGENTS = "agents"


def _tmux(*args: str) -> Tuple[int, str]:
    """One tmux command on the private server, stdout and stderr merged.

    tmux reports refusals on stderr, and the tests want a single blob plus a
    status rather than two streams.
    """
    proc = subprocess.run([TMUX, "-L", SOCKET, "-f", CONFIG, *args],
                          capture_output=True, text=True, timeout=15)
    return proc.returncode, (proc.stdout + proc.stderr).rstrip("\n")


#: Every temp home this module created, so tearDownModule can remove one a test
#: forgot (a sandbox built without its context manager).
_HOMES: List[Path] = []


def _socket_file(name: str) -> Path:
    """Path tmux will use for ``-L name``, without asking a server.

    Asking is what broke the old teardown: by then the server is usually gone,
    ``display-message`` prints nothing, and the file is never removed.
    """
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return Path(base) / ("tmux-%d" % os.getuid()) / name


def _remove_socket_if_dead(name: str, socket_path, wait: float = 3.0) -> None:
    """Unlink a dead server's socket file, never a live server's.

    ``kill-server`` returns before the server has finished exiting, so a single
    probe straight afterwards still sees a live server and skips the unlink, and
    the file is left behind. Wait for the server to stop answering first.
    """
    path = Path(socket_path) if socket_path else _socket_file(name)
    if not path.exists():
        return
    deadline = time.time() + wait
    while True:
        rc = subprocess.run([TMUX, "-L", name, "ls"], capture_output=True, timeout=10).returncode
        if rc != 0:
            break
        if time.time() >= deadline:
            return  # still alive: it is not ours to remove
        time.sleep(0.05)
    try:
        path.unlink()
    except OSError:
        pass


def tearDownModule():
    """Tear down the private server this module created (never the default one).

    ``kill-server`` is safe here and only here: the socket name is this module's
    own pid-scoped server, so nothing the user owns can live on it.
    """
    if TMUX:
        socket_path = _socket_file(SOCKET)
        subprocess.run([TMUX, "-L", SOCKET, "kill-server"],
                       capture_output=True, text=True, timeout=15)
        # kill-server (or the last session closing) leaves the socket file
        # behind; remove it so a run leaves no litter in the socket directory.
        _remove_socket_if_dead(SOCKET, socket_path)
    for home in _HOMES:
        shutil.rmtree(home, ignore_errors=True)


def wait_for(predicate: Callable[[], bool], timeout: float = 12.0,
             interval: float = 0.05) -> bool:
    """Poll ``predicate`` until true or ``timeout`` expires; exceptions mean "not yet".

    Predicates here read tmux or files that can disappear between calls, so a
    single failed read is not a verdict. Tests that wait on the coordinator pass
    20 s or more; the 12 s default covers one tmux read.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- readers


def _format(target: str, fmt: str) -> str:
    """A ``display-message -p`` expansion, '' when tmux did not answer."""
    rc, out = _tmux("display-message", "-p", "-t", target, fmt)
    return out.strip() if rc == 0 else ""


def _pane_exists(pane_id: Optional[str]) -> bool:
    """The resolved id is the authority, not the exit status (tmux 3.8 prints
    nothing and exits 0 for a missing target)."""
    return bool(pane_id) and _format(pane_id, "#{pane_id}") == pane_id


def _window_exists(window_id: Optional[str]) -> bool:
    """True only when the id resolves to itself (tmux exits 0 for a missing target)."""
    return bool(window_id) and _format(window_id, "#{window_id}") == window_id


def _pane_pid(pane_id: str) -> Optional[int]:
    """``#{pane_pid}`` as an int, or None when the pane does not resolve."""
    text = _format(pane_id, "#{pane_pid}")
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _pane_command(pane_id: str) -> str:
    """``pane_current_command``, used to tell the follower from a shell."""
    return _format(pane_id, "#{pane_current_command}")


def _window_index(window_id: str) -> int:
    """``#{window_index}`` as an int, -1 when unknown.

    -1 sorts before every real tab rather than looking like tab 0.
    """
    try:
        return int(_format(window_id, "#{window_index}"))
    except (TypeError, ValueError):
        return -1


_counter = [0]


class Sandbox:
    """One disposable tmux session + its private HERMES_HOME."""

    def __init__(self, columns: int = 160, rows: int = 45):
        """Allocate the name and a private home; create nothing yet.

        ``_tmp`` is unused: ``test_concurrency._spawn`` assigns ``box._tmp``
        itself, and nothing reads the value stored here.
        """
        _counter[0] += 1
        self.name = "hx_it_%d_%d" % (os.getpid(), _counter[0])
        self.home = Path(tempfile.mkdtemp(prefix="hxtmux_home_"))
        _HOMES.append(self.home)
        self._tmp = self.home
        self.columns, self.rows = columns, rows
        self.created = False
        self.marker_seq = 0

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "Sandbox":
        """Create a detached session running ``zsh -f``, then export into it.

        HERMES_HOME, HERMES_TMUX and PYTHONPATH are set in the pane, not in this
        process: ``cli`` types into that shell, so every command sees this
        sandbox's profile and the private socket.
        """
        rc, out = _tmux("new-session", "-d", "-P", "-F", "#{pane_id}",
                        "-s", self.name,
                        "-x", str(self.columns), "-y", str(self.rows),
                        "-c", str(REPO), "exec /bin/zsh -f")
        if rc != 0:
            raise unittest.SkipTest("cannot create tmux session: %s" % out)
        self.created = True
        self.pane_id = out.strip().splitlines()[-1].strip()
        self.run("export HERMES_HOME=%s" % self.home)
        self.run("export HERMES_TMUX=1")
        self.run("export PYTHONPATH=%s" % REPO)
        return self

    def __exit__(self, *exc):
        """Kill the session by name and remove the home; the server belongs to module
        teardown.
        """
        if self.created:
            _tmux("kill-session", "-t", self.name)
        shutil.rmtree(self.home, ignore_errors=True)
        return False

    # -- driving the pane ---------------------------------------------------
    def pane(self, index: int = 0) -> str:
        """The nth pane id of the session's first window, '' when there is no such pane.
        """
        rc, out = _tmux("list-panes", "-t", self.pane_id, "-F", "#{pane_id}")
        panes = out.split()
        return panes[index] if len(panes) > index else ""

    def run(self, command: str, timeout: float = 20.0) -> int:
        """Type a command into the session's first pane and wait for its rc.

        Addressed by pane id: window indices are configurable (base-index) and
        can be renumbered, so an index target is not a stable address.
        """
        self.marker_seq += 1
        marker = self.home / ("rc_%d" % self.marker_seq)
        line = "%s; print -r -- $? > %s" % (command, marker)
        _tmux("send-keys", "-t", self.pane_id, "-l", line)
        _tmux("send-keys", "-t", self.pane_id, "Enter")
        if not wait_for(lambda: marker.exists(), timeout=timeout):
            raise AssertionError("command timed out: %s" % command)
        try:
            return int(marker.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            return -1

    def export(self, name: str, value: str) -> None:
        """Export in the pane shell; raises when zsh refused it."""
        rc = self.run("export %s=%s" % (name, value))
        if rc != 0:
            raise AssertionError("could not export %s" % name)

    def cli(self, *args: str, timeout: float = 20.0) -> int:
        """Run ``python -m hermes_tmux ...`` in the pane, inheriting its exports."""
        return self.run("%s -m hermes_tmux %s" % (PY, " ".join(args)), timeout=timeout)

    def capture(self, target: Optional[str] = None) -> str:
        """Visible screen plus scrollback of a pane, as text."""
        rc, out = _tmux("capture-pane", "-p", "-t", target or self.pane_id)
        return out

    # -- observation --------------------------------------------------------
    def windows(self) -> List[dict]:
        """Windows with their own ``@hx`` marker, read through the format ``#{@hx}``.

        Unlike ``test_tabs`` this harness uses the format read. That is safe only
        because a window-format read falls back to the session, and no session here is
        marked; ownership decisions that matter are asserted through pane options and
        through the product's own ``show-options`` reads.
        """
        rc, out = _tmux("list-windows", "-t", self.name,
                        "-F", "#{window_id}\t#{window_name}\t#{window_index}\t#{@hx}")
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                rows.append({"id": parts[0], "name": parts[1],
                             "index": parts[2], "ours": parts[3].strip() == "1"})
        return rows

    def window(self, name: str) -> Optional[dict]:
        """A window by name: exact first, then our ``name#N`` disambiguation."""
        windows = self.windows()
        for win in windows:
            if win["name"] == name:
                return win
        for win in windows:
            if win["name"].startswith(name + " ") or win["name"].startswith(name + "#"):
                return win
        return None

    def windows_named(self, name: str) -> List[dict]:
        """Windows whose name is exactly this, in list order."""
        return [win for win in self.windows() if win["name"] == name]

    def own_windows(self) -> List[dict]:
        """Windows carrying our marker, i.e. the workspace's own."""
        return [win for win in self.windows() if win["ours"]]

    def pane_title(self, pane_id: str) -> str:
        """``#{pane_title}``. Informational only: v3 does not put identity there."""
        return _format(pane_id, "#{pane_title}")

    def panes_of(self, window_id: str) -> List[dict]:
        """Panes of a window with the format-read marker, title and command.

        ``ours`` here is the inherited value — it is used for reporting, never as the
        proof that a pane may be killed.
        """
        rc, out = _tmux("list-panes", "-t", window_id,
                        "-F", "#{pane_id}\t#{@hx}\t#{pane_title}\t#{pane_current_command}")
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                rows.append({"id": parts[0], "ours": parts[1].strip() == "1",
                             "title": parts[2], "command": parts[3]})
        return rows

    def window_option(self, window_id: str, option: str) -> str:
        """The value set ON this window (never inherited from the session)."""
        rc, out = _tmux("show-options", "-w", "-t", window_id, option)
        return _option_value(rc, out, option)

    def pane_option(self, pane_id: str, option: str) -> str:
        """The value set ON this pane (never inherited from its window)."""
        rc, out = _tmux("show-options", "-p", "-t", pane_id, option)
        return _option_value(rc, out, option)

    def layout(self) -> dict:
        """The session's layout document, or {} before one exists."""
        child = self.tab_dir()
        if child is None:
            return {}
        try:
            return json.loads((child / "layout.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def tab_dir(self) -> Optional[Path]:
        """The state directory holding layout.json, or None."""
        root = self.home / "cache" / "hermes-tmux" / "sessions"
        if not root.is_dir():
            return None
        for child in root.iterdir():
            if (child / "layout.json").exists():
                return child
        return None

    def key(self) -> str:
        """The workspace key, i.e. the state directory's name."""
        child = self.tab_dir()
        if child is None:
            raise AssertionError("no session state directory was written")
        return child.name

    def tabs(self) -> Dict[str, dict]:
        """Tab records keyed ``<delegation_id>#<task_index>``."""
        return dict(self.layout().get("tabs") or {})

    def dismissed(self) -> Dict[str, float]:
        """Keys of closed/retired tabs with the time of the decision."""
        return dict(self.layout().get("dismissed") or {})

    def render_text(self) -> str:
        """The last published overview text, or ''."""
        child = self.tab_dir()
        if child is None:
            return ""
        try:
            return (child / "render.txt").read_text(encoding="utf-8")
        except OSError:
            return ""

    # -- fixtures ----------------------------------------------------------
    def live(self) -> Path:
        """Create and return the fixtures' ``cache/delegation/live`` root."""
        root = self.home / "cache" / "delegation" / "live"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _atomic_json(self, path: Path, data: dict) -> None:
        """Write via rename: a reader must never see a half-written manifest."""
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def add_delegation(self, deleg_id: str, tasks: List[dict],
                       model: str = "deepseek/deepseek-v4.1-flash-1017",
                       started: Optional[str] = None) -> Path:
        """Write a manifest plus one ``task-N.log`` per task, as the real writer does.

        ``status`` defaults to running and ``started`` to today: a task finished before
        ``up`` is history and gets no tab, so a fixture meant to be live must look live.
        ``lines`` replaces the default header when a test needs exact transcript text.
        """
        started = started or datetime.now().strftime("%Y-%m-%d 09:00:00")
        d = self.live() / deleg_id
        d.mkdir(parents=True, exist_ok=True)
        manifest_tasks = []
        for i, task in enumerate(tasks):
            log = d / ("task-%d.log" % i)
            lines = task.get("lines") or [
                "=== Hermes subagent live transcript ===",
                "delegation: %s   task: %d" % (deleg_id, i),
                "goal: %s" % task.get("goal", "do the thing"),
                "started: %s" % started,
                "(append-only; streams while the subagent runs - tail -f me)",
                "=" * 40,
            ]
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            entry = {
                "index": i,
                "goal": task.get("goal", "do the thing"),
                "log": str(log),
                "status": task.get("status", "running"),
            }
            if task.get("model"):
                entry["model"] = task["model"]
            if task.get("exit_reason"):
                entry["exit_reason"] = task["exit_reason"]
            manifest_tasks.append(entry)
        manifest = {
            "delegation_id": deleg_id,
            "started": started,
            "task_count": len(tasks),
            "model": model,
            "provider": "nous",
            "tasks": manifest_tasks,
        }
        self._atomic_json(d / "manifest.json", manifest)
        return d

    def set_task_status(self, deleg_id: str, index: int, status: str,
                        exit_reason: Optional[str] = None) -> None:
        """Rewrite one task's status (and optional exit_reason) atomically.

        Raises for an unknown index so a mistyped fixture cannot pass while asserting
        nothing.
        """
        path = self.live() / deleg_id / "manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        found = False
        for task in data.get("tasks", []):
            if int(task.get("index", -1)) == index:
                task["status"] = status
                if exit_reason:
                    task["exit_reason"] = exit_reason
                found = True
        if not found:
            raise AssertionError("no task %d in %s" % (index, deleg_id))
        self._atomic_json(path, data)

    def append_event(self, deleg_id: str, task: int, role: str, text: str) -> None:
        """Append one line in the writer's event format.

        A write to a child of a watched directory is exactly the event the
        coordinator's kqueue reports, so this is also how a test forces a pass.
        """
        log = self.live() / deleg_id / ("task-%d.log" % task)
        stamp = time.strftime("%H:%M:%S")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("%s %-9s| %s\n" % (stamp, role, text))

    def finish(self, deleg_id: str, index: int, status: str = "completed") -> None:
        """Terminal state the way the writer records it: manifest + final line."""
        self.set_task_status(deleg_id, index, status)
        self.append_event(deleg_id, index, "final",
                          "status=%s duration=4.0s" % status)

    def write_db_state(self, delegation_id: str, state: str) -> None:
        """Write a terminal row into state.db the way Hermes does.

        This is the lifecycle path that never touches the manifest or a
        transcript, and it is what a DB-only completion looks like.
        """
        path = self.home / "state.db"
        connection = sqlite3.connect(str(path), timeout=5)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS async_delegations "
                "(delegation_id TEXT PRIMARY KEY, state TEXT)")
            connection.execute(
                "INSERT OR REPLACE INTO async_delegations (delegation_id, state) "
                "VALUES (?, ?)", (delegation_id, state))
            connection.commit()
        finally:
            connection.close()



def _option_value(rc: int, out: str, option: str) -> str:
    """Parse ``option value`` out of ``show-options`` output; '' on a failed call."""
    if rc != 0:
        return ""
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] == option:
            return parts[1].strip()
    return ""


@unittest.skipIf(SKIP, "tmux not installed")
class IntegrationTest(unittest.TestCase):
    """End-to-end behaviour against real tmux, one Sandbox per test."""
    maxDiff = None

    # -- shared helpers ----------------------------------------------------
    def up(self, box: Sandbox) -> dict:
        """``up``, asserting the one window it owns exists and is ours."""
        rc = box.cli("up")
        self.assertEqual(rc, 0, "up exited %s: %r" % (rc, box.capture()[-500:]))
        win = box.window(AGENTS)
        self.assertIsNotNone(win, "up did not create the 'agents' window: %r"
                             % [w["name"] for w in box.windows()])
        self.assertTrue(win["ours"], "'agents' is not marked ours")
        panes = box.panes_of(win["id"])
        self.assertEqual(len(panes), 1,
                         "the overview window holds %d panes, expected 1: %r"
                         % (len(panes), panes))
        return win

    def wait_tabs(self, box: Sandbox, count: int, timeout: float = 20.0) -> dict:
        """Wait for ``count`` tabs whose recorded panes all exist.

        The pane check matters because a record is published after its window is made;
        the layout can briefly name a pane tmux has not listed yet.
        """
        ok = wait_for(
            lambda: len(box.tabs()) == count
            and all(_pane_exists(tab.get("pane") or "") for tab in box.tabs().values()),
            timeout=timeout)
        if not ok:
            raise AssertionError(
                "expected %d live worker tabs, layout tabs=%r (windows=%r)"
                % (count, box.tabs(), [(w["name"], w["id"]) for w in box.windows()]))
        return box.layout()

    def assert_one_pane_per_tab(self, box: Sandbox, layout: dict, name: str = "") -> None:
        """One pane in the window, and it is the tab's pane, with ``@hx`` on both.

        The pane marker is read with ``show-options -p`` so inheritance cannot fake it.
        """
        for wkey, tab in (layout.get("tabs") or {}).items():
            panes = box.panes_of(tab["window"])
            self.assertEqual(len(panes), 1,
                             "%s: window %s holds %d panes, expected exactly 1: %r"
                             % (wkey, tab["window"], len(panes), panes))
            self.assertEqual(panes[0]["id"], tab["pane"],
                             "%s: the tab's recorded pane is not its window's pane" % wkey)
            self.assertEqual(box.pane_option(tab["pane"], "@hx"), "1",
                             "%s: pane %s has no @hx of its own" % (wkey, tab["pane"]))
            self.assertEqual(box.window_option(tab["window"], "@hx"), "1",
                             "%s: window %s has no @hx of its own" % (wkey, tab["window"]))

    def glyph(self, box: Sandbox, kind: str) -> str:
        """The state glyph the product would draw for ``kind``, with this layout's ASCII
        mode.
        """
        from hermes_tmux import labels
        return labels.glyph(kind, bool(box.layout().get("ascii")))

    # ---- 1. startup ------------------------------------------------------
    def test_up_builds_workers_window_and_overview(self):
        """``up`` builds the overview window and the running worker's tab beside it.

        The worker tab is required to be a DIFFERENT window from ``agents``: packing it
        into the overview was the v2 shape and is the regression this pins. The pane
        must also print the transcript path it follows, so a pane that merely exists
        (and runs nothing) fails.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00001", [{"goal": "audit the parser"}])
            win = self.up(box)
            layout = box.layout()
            self.assertEqual(layout.get("workers_window"), win["id"])
            self.assertEqual(layout.get("overview_pane"),
                             box.panes_of(win["id"])[0]["id"])
            self.assertTrue(wait_for(lambda: "HERMES AGENTS" in box.capture(win["id"])),
                            "the overview pane never painted its header: %r"
                            % box.capture(win["id"])[:300])
            self.assertTrue(wait_for(lambda: "audit the parser" in box.render_text()),
                            "the overview never rendered the running worker")

            # A running worker is a tab of its own, not a pane of the overview.
            layout = self.wait_tabs(box, 1)
            tab = layout["tabs"]["deleg_aaa00001#0"]
            self.assertNotEqual(tab["window"], win["id"],
                                "the worker tab was packed into the overview window")
            self.assert_one_pane_per_tab(box, layout)
            self.assertTrue(wait_for(lambda: "task-0.log" in box.capture(tab["pane"])),
                            "the worker pane did not print the transcript it follows: %r"
                            % box.capture(tab["pane"])[:300])

    def test_up_is_idempotent_and_reuses_panes(self):
        """A second ``up`` changes no process and no record.

        Reuse is asserted by pid, not by pane id: a restarted display loop keeps the id
        and changes the pid. The tab record is compared whole, so a repointed ``pane``
        or ``window`` is caught, and the static window count catches a duplicate
        ``agents``.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00002", [{"goal": "task one"}])
            self.up(box)
            first = self.wait_tabs(box, 1)
            follow_pid = _pane_pid(first["overview_pane"])
            self.assertTrue(follow_pid)
            tab_before = dict(first["tabs"]["deleg_aaa00002#0"])

            self.assertEqual(box.cli("up"), 0)
            second = box.layout()
            # "Reuses panes" means the same process, not just the same pane id:
            # a restarted display loop would show a different pid.
            self.assertEqual(_pane_pid(second["overview_pane"]), follow_pid,
                             "the overview process was restarted by an idempotent up")
            self.assertEqual(first.get("workers_window"), second.get("workers_window"))
            self.assertEqual(first.get("overview_pane"), second.get("overview_pane"))
            self.assertEqual(second.get("tabs"), first.get("tabs"),
                             "a second up reopened or repointed a worker tab")
            self.assertEqual(dict(second["tabs"]["deleg_aaa00002#0"]), tab_before)
            agents = [w for w in box.windows()
                      if w["name"] == AGENTS or w["name"].startswith(AGENTS + "#")]
            self.assertEqual(len(agents), 1, "up created a second overview window")

    # ---- 2. worker tabs --------------------------------------------------
    def test_worker_panes_created_per_running_worker(self):
        """Three running workers give three windows, each named for its own goal.

        Three distinct windows rule out the v2 packing; the name is recomputed with
        ``labels.tab_label`` because sibling de-duplication is part of the naming
        contract. The overview window must hold exactly one pane.
        """
        from hermes_tmux import labels
        goals = ["w0", "w1", "w2"]
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00003", [{"goal": g} for g in goals])
            self.up(box)
            layout = self.wait_tabs(box, 3)
            tabs = layout["tabs"]
            self.assertEqual(set(tabs), {"deleg_aaa00003#%d" % i for i in range(3)},
                             "tabs are %r" % sorted(tabs))
            self.assertEqual(len({tab["window"] for tab in tabs.values()}), 3,
                             "two workers share a window: %r"
                             % [tab["window"] for tab in tabs.values()])
            windows = {win["id"]: win for win in box.windows()}
            for i, goal in enumerate(goals):
                wkey = "deleg_aaa00003#%d" % i
                tab = tabs[wkey]
                win = windows.get(tab["window"])
                self.assertIsNotNone(win, "%s: window %s vanished" % (wkey, tab["window"]))
                siblings = [g for j, g in enumerate(goals) if j != i]
                self.assertEqual(win["name"], labels.tab_label(goal, siblings),
                                 "%s: window name is %r, expected the tab label of its goal"
                                 % (wkey, win["name"]))
                self.assertTrue(wait_for(
                    lambda pane=tab["pane"]: _pane_command(pane).lower().startswith("python"),
                    timeout=8),
                    "%s: pane runs %r, expected the styled follower (python)"
                    % (wkey, _pane_command(tab["pane"])))
            self.assert_one_pane_per_tab(box, layout)
            overview = box.window(AGENTS)
            self.assertEqual(len(box.panes_of(overview["id"])), 1,
                             "the overview window must hold the overview only")

    def test_pane_title_distinguishes_running_from_completed(self):
        """A tab following a finished transcript must say so.

        v3 moved the label off ``#{pane_title}``: the state word lives in the
        pane's own ``@hx_head`` option and the glyph in the window's
        ``@hx_glyph``. The follower's output alone cannot distinguish "still
        executing" from "finished, nothing new", and this test still exists to
        pin that the border/tab says which it is.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_live00001", [{"goal": "still working"}])
            self.up(box)
            layout = self.wait_tabs(box, 1)
            tab = layout["tabs"]["deleg_live00001#0"]
            pane, window = tab["pane"], tab["window"]

            box.append_event("deleg_live00001", 0, "tool", "-> terminal(ls)")
            self.assertTrue(
                wait_for(lambda: "RUNNING" in box.pane_option(pane, "@hx_head")),
                "running worker not labelled RUNNING: %r"
                % box.pane_option(pane, "@hx_head"))
            self.assertIn(self.glyph(box, "run"),
                          box.window_option(window, "@hx_glyph"),
                          "the running tab's glyph does not say running")

            # Same worker, now finished: manifest status plus the final marker
            # the writer emits. It keeps its window and pane; only the labels
            # change.
            box.finish("deleg_live00001", 0, "completed")
            self.assertTrue(
                wait_for(lambda: "DONE" in box.pane_option(pane, "@hx_head"), timeout=20),
                "completed worker still labelled running: %r"
                % box.pane_option(pane, "@hx_head"))
            self.assertNotIn("RUNNING", box.pane_option(pane, "@hx_head"),
                             "a completed tab still claims to be RUNNING")
            self.assertIn(self.glyph(box, "done"),
                          box.window_option(window, "@hx_glyph"),
                          "the finished tab's glyph does not say done")
            self.assertEqual(box.tabs()["deleg_live00001#0"]["window"], window,
                             "the tab moved house on finishing")

    def test_every_worker_tab_holds_one_pane_and_its_header_carries_identity(self):
        """Replaces the v1/v2 "at most two panes per window" rule.

        v3 gives every worker a window of its own, so the old packing invariant
        is gone. What survives is the property behind it: a tab is exactly one
        pane, and that pane's header and its window's glyph identify the worker
        and its state (task, model, short key) rather than a bare ``tail``.
        """
        from hermes_tmux import labels
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00004",
                               [{"goal": "w%d" % i} for i in range(6)])
            # A fixture's manifest start time is hours old; a fresh event is what
            # makes the worker "RUNNING" rather than "SILENT" (the stall label).
            for i in range(6):
                box.append_event("deleg_aaa00004", i, "tool", "-> terminal(ls)")
            self.up(box)
            layout = self.wait_tabs(box, 6)
            self.assert_one_pane_per_tab(box, layout)
            self.assertEqual(len({tab["window"] for tab in layout["tabs"].values()}), 6,
                             "six workers must have six windows, got %r"
                             % [tab["window"] for tab in layout["tabs"].values()])
            for wkey, tab in layout["tabs"].items():
                head = box.pane_option(tab["pane"], "@hx_head")
                self.assertIn("RUNNING", head, "%s: header does not carry state: %r"
                              % (wkey, head))
                self.assertIn("w" + wkey.rsplit("#", 1)[1], head,
                              "%s: header does not name the task: %r" % (wkey, head))
                self.assertIn(labels.model_short("deepseek/deepseek-v4.1-flash-1017"),
                              head, "%s: header does not name the model: %r" % (wkey, head))
                self.assertIn(wkey.split("#")[0][-8:], head,
                              "%s: header does not carry the worker's id: %r" % (wkey, head))
                self.assertIn(self.glyph(box, "run"),
                              box.window_option(tab["window"], "@hx_glyph"),
                              "%s: tab glyph does not carry state" % wkey)

    def test_new_worker_gets_its_own_pane_and_old_pane_is_not_stolen(self):
        """A worker that appears later gets new ids; the existing tab is left alone.

        The control is the first worker's record and pane: "a new pane was created" is
        only good if the old one was neither repointed nor destroyed.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00005", [{"goal": "first"}])
            self.up(box)
            layout = self.wait_tabs(box, 1)
            first = layout["tabs"]["deleg_aaa00005#0"]
            first_pane, first_window = first["pane"], first["window"]

            box.add_delegation("deleg_bbb00006", [{"goal": "second"}])
            self.assertTrue(wait_for(
                lambda: len(box.tabs()) == 2
                and "deleg_bbb00006#0" in box.tabs(), timeout=15),
                "no tab was created for the newly visible worker: %r" % box.tabs())

            after = box.tabs()
            second = after["deleg_bbb00006#0"]
            self.assertNotEqual(second["window"], first_window,
                                "the new worker landed in an occupied window")
            self.assertNotEqual(second["pane"], first_pane,
                                "the new worker took an existing pane")
            # The pane the user may be reading stays bound to its own worker.
            self.assertEqual(after["deleg_aaa00005#0"]["pane"], first_pane,
                             "an existing tab was repointed at a different worker")
            self.assertEqual(after["deleg_aaa00005#0"]["window"], first_window)
            self.assertTrue(_pane_exists(first_pane),
                            "the first worker's pane was destroyed for the new one")

    def test_worker_command_focuses_the_pane_in_stable_slot_order(self):
        """``worker 2`` is the second tab by window index, and it focuses that pane.

        The target is derived by sorting the layout's windows, so the test does not
        assume creation order equals display order.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00019",
                               [{"goal": "w%d" % i} for i in range(3)])
            self.up(box)
            layout = self.wait_tabs(box, 3)
            ordered = sorted(layout["tabs"].items(),
                             key=lambda item: _window_index(item[1]["window"]))
            self.assertEqual(len(ordered), 3)
            target_key, target = ordered[1]

            self.assertEqual(box.cli("worker", "2"), 0)
            current = _format(box.name, "#{window_id}")
            self.assertEqual(current, target["window"],
                             "worker 2 selected %s, expected the second tab %s (%s)"
                             % (current, target["window"], target_key))
            self.assertEqual(_format(target["window"], "#{pane_id}"), target["pane"],
                             "worker 2 did not focus the tab's pane")

    def test_stale_v1_layout_naming_user_panes_causes_no_damage(self):
        """Replaces the v1->v2 pane migration test.

        The ``trim_overview_window`` migration no longer exists, and v1's
        ``worker_windows`` / ``worker_assign`` / ``tail_panes`` keys are ignored
        by v3. The property the old test protected still matters: a layout file
        written by an older version must never authorise a kill, split, rename
        or ownership marker on a pane or window the user owns, even when those
        stale keys name them.
        """
        with Sandbox() as box:
            # The user's own window, with a pane they split themselves.
            user_win = _tmux("new-window", "-d", "-P", "-F", "#{window_id}",
                             "-t", box.name, "-n", "notes",
                             "-c", str(REPO), "exec /bin/zsh -f")[1].strip()
            self.assertTrue(_window_exists(user_win))
            split = _tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                          "-t", user_win, "-c", str(REPO), "exec sleep 600")[1].strip()
            self.assertTrue(_pane_exists(split))
            user_panes = sorted(p["id"] for p in box.panes_of(user_win))
            user_pids = {p: _pane_pid(p) for p in user_panes}

            box.add_delegation("deleg_v1stale1", [{"goal": "upgrade"}])
            self.up(box)
            layout = self.wait_tabs(box, 1)
            our_tab = dict(layout["tabs"]["deleg_v1stale1#0"])

            # Rewrite layout.json into the v1 shape, its stale references naming
            # the user's own window and panes.
            path = box.tab_dir() / "layout.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["worker_windows"] = {"deleg_v1stale1#0": user_win}
            data["worker_assign"] = {"deleg_v1stale1#0": "0"}
            data["tail_panes"] = {user_panes[-1]: "deleg_v1stale1#0"}
            data["overview_panes"] = list(user_panes)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)

            # Force a full reconcile pass, which reads exactly that layout.
            box.append_event("deleg_v1stale1", 0, "tool", "-> PROOF-OF-PASS(x)")
            self.assertTrue(wait_for(lambda: "PROOF-OF-PASS" in box.render_text(),
                                     timeout=15),
                            "the coordinator never ran a pass over the stale layout")

            after = box.layout()
            self.assertEqual(after["tabs"]["deleg_v1stale1#0"]["window"],
                             our_tab["window"],
                             "a stale tail_panes/worker_windows entry moved the tab "
                             "into the user's window")
            self.assertEqual(_format(user_win, "#{window_name}"), "notes",
                             "the user's window was renamed")
            self.assertEqual(sorted(p["id"] for p in box.panes_of(user_win)), user_panes,
                             "the user's panes were killed or split")
            for pane_id, pid in user_pids.items():
                self.assertEqual(_pane_pid(pane_id), pid,
                                 "the user's pane %s was respawned" % pane_id)
            self.assertEqual(box.window_option(user_win, "@hx"), "",
                             "a stale layout made us mark the user's window as ours")
            for pane_id in user_panes:
                self.assertEqual(box.pane_option(pane_id, "@hx"), "",
                                 "a stale layout made us mark the user's pane as ours")

    def test_worker_window_counts_for_zero_through_eight_workers(self):
        """Window count is exactly workers + static windows, for 0..8.

        The static set is computed from ``cli.testbench_tab_enabled()`` instead of
        hard-coded, because whether ``hx:testbench`` exists is a configuration fact.
        Every worker tab must still hold one pane at each count.
        """
        for count in (0, 1, 2, 3, 4, 8):
            with self.subTest(workers=count):
                with Sandbox() as box:
                    if count:
                        box.add_delegation("deleg_cnt%05d" % count,
                                           [{"goal": "w%d" % i} for i in range(count)])
                    self.up(box)
                    layout = self.wait_tabs(box, count)
                    self.assertEqual(len(layout.get("tabs") or {}), count,
                                     "%d running workers must have %d tabs, got %r"
                                     % (count, count, sorted(layout.get("tabs") or {})))
                    for tab in (layout.get("tabs") or {}).values():
                        self.assertEqual(len(box.panes_of(tab["window"])), 1,
                                         "a worker tab holds more than one pane")
                    # One window per running worker, plus the configured permanent workspace windows.
                    from hermes_tmux import cli as _cli
                    static_tabs = [AGENTS, "hx:mail"] + (
                        ["hx:testbench"] if _cli.testbench_tab_enabled() else []
                    )
                    expected_owned = count + len(static_tabs)
                    self.assertEqual(len(box.own_windows()), expected_owned,
                                     "%d workers must own %d windows, got %r"
                                     % (count, expected_owned,
                                        [w["name"] for w in box.own_windows()]))
                    overview = box.window(AGENTS)
                    self.assertIsNotNone(overview)
                    self.assertEqual(len(box.panes_of(overview["id"])), 1,
                                     "overview window must hold one pane")

    # ---- 3. events -------------------------------------------------------
    def test_up_rebuilds_a_lost_overview_without_repurposing_survivors(self):
        """A new overview is built; no surviving worker pane is repurposed as one.

        Reuse is asserted by pid: the tab's pane must still run the same process, and
        the new overview must be neither the dead id nor the worker's pane.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00106", [{"goal": "rebuild"}])
            self.up(box)
            layout = self.wait_tabs(box, 1)
            old_overview = layout["overview_pane"]
            tab = layout["tabs"]["deleg_aaa00106#0"]
            survivor_pid = _pane_pid(tab["pane"])
            self.assertTrue(survivor_pid)

            _tmux("kill-pane", "-t", old_overview)
            self.assertTrue(wait_for(lambda: not _pane_exists(old_overview), timeout=5))
            self.assertEqual(box.cli("up"), 0)

            rebuilt = box.layout()
            new_overview = rebuilt.get("overview_pane")
            self.assertTrue(new_overview)
            self.assertNotEqual(new_overview, old_overview)
            self.assertNotEqual(new_overview, tab["pane"],
                                "up repurposed a worker pane as the overview")
            self.assertEqual(_pane_pid(tab["pane"]), survivor_pid,
                             "up restarted the surviving worker pane")
            self.assertTrue(wait_for(
                lambda: "HERMES AGENTS" in box.capture(new_overview), timeout=10),
                "no overview was rebuilt")
            self.assertIn("deleg_aaa00106#0", box.tabs(),
                          "up dropped the worker tab while rebuilding the overview")

    def test_coordinator_is_recognised_while_it_lives(self):
        """Positive control: the identity check must say yes to the real thing."""
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00104", [{"goal": "identity"}])
            self.up(box)
            self.assertTrue(wait_for(lambda: _watcher_pid(box) is not None))
            key = box.key()
            code = ("from hermes_tmux import cli; "
                    "print('ALIVE', cli._watcher_alive(%r))" % key)
            self.assertEqual(box.run("%s -c %s" % (PY, _q(code))), 0)
            self.assertTrue(wait_for(lambda: "ALIVE True" in box.capture()),
                            "the live coordinator was not recognised")

    def test_events_stream_in_order_into_the_worker_pane(self):
        """Appended events reach the worker pane, in order.

        Order is asserted by text position in the capture: a follower that re-reads
        the file could show them out of order and still contain each string.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00007", [{"goal": "ordered"}])
            self.up(box)
            layout = self.wait_tabs(box, 1)
            pane = layout["tabs"]["deleg_aaa00007#0"]["pane"]
            for i in range(3):
                box.append_event("deleg_aaa00007", 0, "tool", "-> step%d(...)" % i)
            box.append_event("deleg_aaa00007", 0, "final",
                             "status=completed duration=1.0s")
            self.assertTrue(wait_for(lambda: "step2" in box.capture(pane)),
                            "worker pane never showed the appended events: %r"
                            % box.capture(pane)[-400:])
            painted = box.capture(pane)
            self.assertLess(painted.index("step0"), painted.index("step1"))
            self.assertLess(painted.index("step1"), painted.index("step2"))

    def test_overview_updates_on_transcript_events_without_restart(self):
        """A transcript append repaints the overview without a restart.

        The command text is asserted, not just the goal, so a repaint of the existing
        record cannot satisfy it.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00008", [{"goal": "live update"}])
            self.up(box)
            self.assertTrue(wait_for(lambda: "live update" in box.render_text()))
            box.append_event("deleg_aaa00008", 0, "tool", "-> terminal(ls)")
            self.assertTrue(wait_for(lambda: "terminal(ls)" in box.render_text()),
                            "render.txt did not follow the transcript event: %r"
                            % box.render_text()[:400])

    def test_state_db_completion_refreshes_overview_without_a_transcript_write(self):
        """A task can finish only in state.db. That must still repaint.

        Before the database was watched, the row kept its old running state
        until an unrelated transcript append happened to fire the coordinator.
        v3: the pane header says DONE and the overview stops calling the worker
        running.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_db00001", [{"goal": "db driven"}])
            self.up(box)
            layout = self.wait_tabs(box, 1)
            pane = layout["tabs"]["deleg_db00001#0"]["pane"]
            box.append_event("deleg_db00001", 0, "tool", "-> terminal(ls)")
            self.assertTrue(
                wait_for(lambda: "RUNNING" in box.pane_option(pane, "@hx_head"), timeout=10),
                "fixture did not start as running: %r"
                % box.pane_option(pane, "@hx_head"))

            box.write_db_state("deleg_db00001", "completed")
            self.assertTrue(
                wait_for(lambda: "DONE" in box.pane_option(pane, "@hx_head"), timeout=15),
                "the pane header did not follow the state.db completion: %r"
                % box.pane_option(pane, "@hx_head"))
            self.assertTrue(
                wait_for(lambda: "Nothing running." in box.render_text(), timeout=15),
                "the overview still claims the worker is running: %r"
                % box.render_text()[:400])
            self.assertIn("RECENT", box.render_text(),
                          "the completed worker left the overview entirely: %r"
                          % box.render_text()[:400])

    def test_overview_title_is_not_scrolled_off_in_a_short_pane(self):
        """Writing exactly `height` lines with a trailing newline scrolls one
        line: the title vanished and the pane looked dead.
        """
        with Sandbox(columns=100, rows=8) as box:
            box.add_delegation("deleg_short0001",
                               [{"goal": "w%d" % i} for i in range(3)])
            self.up(box)
            self.wait_tabs(box, 3)
            overview = box.layout()["overview_pane"]
            self.assertTrue(wait_for(
                lambda: "HERMES AGENTS" in box.capture(overview), timeout=10),
                "the overview title scrolled out of a short pane: %r"
                % box.capture(overview).splitlines()[:3])

    # ---- 4. failure ------------------------------------------------------
    def test_failed_task_renders_as_failure_not_success(self):
        """A failed run is never painted as a completed one.

        v3 splits it by time: a run that had already ended before `up` is
        history (RECENT, no tab); one that ends afterwards is a live problem
        (NEEDS ATTENTION). Neither is ever shown with the success glyph.
        """
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 09:00:00")
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00009", [{
                "goal": "broken", "status": "failed", "exit_reason": "error",
                "lines": [
                    "=== Hermes subagent live transcript ===",
                    "delegation: deleg_aaa00009   task: 0",
                    "goal: broken", "started: %s" % past,
                    "(append-only)", "=" * 40,
                    "09:00:05 result   | terminal ERROR 5.0s: exit 1",
                    "09:00:06 final    | status=failed exit_reason=error",
                ]}], started=past)
            self.up(box)
            self.assertTrue(wait_for(lambda: "broken" in box.render_text()))
            text = box.render_text()
            fail_glyph = self.glyph(box, "fail")
            self.assertIn(fail_glyph, text, "the failed run has no failure glyph: %r" % text)
            self.assertNotIn(self.glyph(box, "done"), text,
                             "a failed run was painted as a success: %r" % text)
            self.assertIn("RECENT", text,
                          "a run that ended before `up` must be history, not attention: %r"
                          % text)
            self.assertNotIn("NEEDS ATTENTION", text)
            self.assertEqual(box.tabs(), {},
                             "a worker that had already failed before `up` got a tab")

            # Same failure, but after `up`: it needs a look.
            box.add_delegation("deleg_aaa00018", [{"goal": "breaks later"}])
            self.assertTrue(wait_for(lambda: "breaks later" in box.render_text(), timeout=15))
            box.finish("deleg_aaa00018", 0, "error")
            box.set_task_status("deleg_aaa00018", 0, "error", exit_reason="error")
            self.assertTrue(
                wait_for(lambda: "NEEDS ATTENTION" in box.render_text(), timeout=20),
                "a failure that happened after `up` never asked for attention: %r"
                % box.render_text()[:600])
            self.assertIn("run ended: error", box.render_text())

    def test_missing_delegation_directory_is_tolerated(self):
        """An empty profile is described truthfully instead of erroring.

        Both surfaces are checked: the pane render and the ``status`` text.
        """
        with Sandbox() as box:
            self.up(box)
            self.assertTrue(wait_for(
                lambda: "No delegations yet." in box.render_text(), timeout=10),
                "empty workspace not described truthfully: %r" % box.render_text())
            self.assertIn("HERMES AGENTS", box.render_text())
            self.assertEqual(box.cli("status"), 0)
            self.assertIn("HERMES AGENTS", box.capture())

    # ---- 5. review lifecycle --------------------------------------------
    def test_review_record_changes_display_and_is_listed(self):
        """A recorded review appears in the row and in ``review list``.

        The absence before recording is asserted first; without it, the string could
        be present for another reason and the test would pin nothing.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00010", [{"goal": "reviewable"}])
            self.up(box)
            self.assertTrue(wait_for(lambda: "reviewable" in box.render_text()))
            self.assertNotIn("review: recorded", box.render_text(),
                             "the row claimed a review before one was recorded")
            self.assertEqual(box.cli("review", "record", "--delegation", "deleg_aaa00010",
                                     "--task", "0", "--status", "recorded"), 0)
            self.assertTrue(
                wait_for(lambda: "review: recorded (manual)" in box.render_text()),
                "review record did not reach the overview: %r"
                % box.render_text()[:500])
            self.assertEqual(box.cli("review", "list"), 0)
            self.assertIn("deleg_aaa00010", box.capture())

    def test_reviewer_role_never_inferred_from_executor(self):
        """A goal that mentions review does not make the executor a reviewer.

        The role set is read from ``state.load_workers``, the authority; the render is
        checked only for the RUNNING label and for the absence of the word, because
        prose is not where role is decided.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00011", [{"goal": "please review this code"}])
            self.up(box)
            self.assertTrue(wait_for(lambda: "please review this code" in box.render_text()))
            self.assertIn("RUNNING", box.render_text(),
                          "a goal that merely mentions review stopped being a running "
                          "executor: %r" % box.render_text()[:400])
            self.assertNotIn("reviewer", box.render_text().lower())
            # The role the registry implies is the authority; the goal text is not.
            code = ("from hermes_tmux.state import load_workers; "
                    "roles = {w.role for w in load_workers()}; "
                    "print('ROLE-CHECK executor-only' if roles == {'executor'} "
                    "else 'ROLE-CHECK ' + repr(sorted(roles)))")
            self.assertEqual(box.run("%s -c %s" % (PY, _q(code))), 0)
            self.assertIn("ROLE-CHECK executor-only", box.capture(),
                          "a goal mentioning review made the executor a reviewer")

    # ---- 6. detach / reattach -------------------------------------------
    def test_detach_and_reattach_preserves_panes_and_watcher(self):
        """Detach and reattach change no tab identity and start no second coordinator.

        A real client on a pty is used because ``attach`` refuses a non-terminal. The
        comparison is (window, pane) pairs, since the coordinator may legitimately add
        bookkeeping; the watcher pid must be unchanged, not merely alive.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00012", [{"goal": "survivor"}])
            self.up(box)
            before = self.wait_tabs(box, 1)
            self.assertTrue(wait_for(lambda: _watcher_pid(box) is not None))
            watcher_before = _watcher_pid(box)

            # A real detach needs a real client: `attach` fails with "not a
            # terminal" in a test process, so run one under a pty.
            child_env = {k: v for k, v in os.environ.items()
                         if k not in ("TMUX", "TMUX_PANE")}
            # tmux refuses to attach on TERM=dumb, which is what a tool-driven
            # test process normally has.
            child_env["TERM"] = "xterm-256color"
            master, slave = pty.openpty()
            client = subprocess.Popen(
                [TMUX, "-L", SOCKET, "attach", "-t", box.name],
                stdin=slave, stdout=slave, stderr=slave, env=child_env)
            os.close(slave)
            try:
                self.assertTrue(wait_for(
                    lambda: _tmux("list-clients", "-F", "#{client_tty}")[1].strip() != "",
                    timeout=10), "could not attach a test client")
                _tmux("detach-client", "-s", box.name)
                self.assertTrue(wait_for(
                    lambda: _tmux("list-clients", "-F", "#{client_tty}")[1].strip() == "",
                    timeout=10), "detach did not remove the client")
            finally:
                client.terminate()
                try:
                    client.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    client.kill()
                os.close(master)
            self.assertEqual(box.cli("up"), 0)

            after = box.layout()
            self.assertEqual(before.get("workers_window"), after.get("workers_window"))
            self.assertEqual(
                {k: (v.get("window"), v.get("pane")) for k, v in before["tabs"].items()},
                {k: (v.get("window"), v.get("pane")) for k, v in after["tabs"].items()},
                "a reattach changed a worker tab's window or pane")
            for tab in after["tabs"].values():
                self.assertTrue(_pane_exists(tab["pane"]),
                                "a worker pane did not survive detach/reattach")
            self.assertEqual(_watcher_pid(box), watcher_before,
                             "watcher was restarted by an idempotent up")
            win = box.window(AGENTS)
            self.assertIsNotNone(win)
            self.assertEqual(len(box.panes_of(win["id"])), 1,
                             "the overview window must still hold only the overview")

    def test_coordinator_exits_when_its_session_disappears(self):
        """run-shell children outlive the server; the watcher must not.

        Without a liveness check every closed session/user session left one idle
        python process behind (93 were found and killed during development).
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00101", [{"goal": "lifecycle"}])
            self.up(box)
            self.assertTrue(wait_for(lambda: _watcher_pid(box) is not None))
            pid = _watcher_pid(box)

            # Keep the server alive with a second session: killing the only
            # session kills the server, and a server death proves nothing about
            # session liveness (tmux then takes the run-shell child with it).
            _tmux("new-session", "-d", "-s", box.name + "_keep", "exec /bin/zsh -f")
            _tmux("kill-session", "-t", box.name)
            box.created = False
            self.assertEqual(_tmux("list-sessions")[0], 0,
                             "the server died; this test needs it alive")

            self.assertTrue(
                wait_for(lambda: not _pid_alive(pid), timeout=40),
                "coordinator %s survived its session" % pid)

    # ---- 7. cleanup ------------------------------------------------------
    def test_down_never_destroys_a_window_holding_a_user_pane(self):
        """Remove our panes; leave the window standing if a user pane lives in it."""
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00105", [{"goal": "mixed window"}])
            self.up(box)
            win = box.window(AGENTS)
            self.assertIsNotNone(win)

            # The pane id comes back from the split itself: selecting by command
            # name races the coordinator's tab creation.
            user_pane = _tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                              "-t", win["id"], "-c", str(REPO))[1].strip()
            self.assertTrue(user_pane)
            user_pid = _pane_pid(user_pane)
            self.assertTrue(user_pid)
            overview = box.layout().get("overview_pane")
            self.assertTrue(overview)

            self.assertEqual(box.cli("down"), 0)
            self.assertTrue(
                wait_for(lambda: [p["id"] for p in box.panes_of(win["id"])] == [user_pane],
                         timeout=5),
                "down left or removed the wrong panes: %r" % box.panes_of(win["id"]))
            self.assertIsNotNone(box.window(AGENTS),
                                 "down destroyed a window containing a user pane")
            self.assertTrue(_pid_alive(user_pid), "down killed the user's shell")
            self.assertFalse(_pane_exists(overview),
                             "the overview pane survived down")

    def test_down_closes_only_our_windows_and_keeps_user_work(self):
        """``down`` removes the workspace and its state directory, nothing else.

        A reader's ``userwork`` window is the control; the state directory being empty
        is the assertion that keeps ``sessions/`` from growing one dead directory per
        run.
        """
        with Sandbox() as box:
            _tmux("new-window", "-d", "-t", box.name, "-n", "userwork", "exec /bin/zsh -f")
            box.add_delegation("deleg_aaa00013", [{"goal": "teardown"}])
            self.up(box)
            self.wait_tabs(box, 1)
            self.assertEqual(box.cli("logs"), 0)
            self.assertTrue(wait_for(lambda: box.window("hx:logs") is not None))
            self.assertEqual(box.cli("down"), 0)
            self.assertTrue(wait_for(lambda: box.window(AGENTS) is None),
                            "down left the overview window standing")
            self.assertIsNone(box.window("hx:logs"))
            self.assertIsNotNone(box.window("userwork"),
                                 "down destroyed a window it did not create")
            sessions = box.home / "cache" / "hermes-tmux" / "sessions"
            left = list(sessions.iterdir()) if sessions.is_dir() else []
            self.assertEqual(left, [], "down left its state directory behind")

    def test_foreign_window_with_our_name_is_never_adopted(self):
        """A user window called 'agents' must not be taken over."""
        with Sandbox() as box:
            _tmux("new-window", "-d", "-t", box.name, "-n", AGENTS,
                  "exec /bin/zsh -f")
            foreign = box.window(AGENTS)
            self.assertIsNotNone(foreign)
            box.add_delegation("deleg_aaa00100", [{"goal": "name clash"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertIsNotNone(box.window(AGENTS + "#2"),
                                 "up built no workspace window beside the foreign one: %r"
                                 % [w["name"] for w in box.windows()])

            # Their window is untouched; ours exists under a disambiguated name.
            same_name = box.windows_named(AGENTS)
            self.assertEqual(len(same_name), 1, [w["name"] for w in box.windows()])
            self.assertFalse(same_name[0]["ours"],
                             "the tool adopted a window it did not create")
            self.assertEqual(same_name[0]["id"], foreign["id"])
            ours = box.own_windows()
            self.assertTrue(ours, "no workspace window was created")
            names = [w["name"] for w in ours]
            self.assertIn(AGENTS + "#2", names,
                          "the workspace did not step around the foreign name: %r" % names)

            self.assertEqual(box.cli("down"), 0)
            self.assertIsNotNone(box.window(AGENTS),
                                 "down closed a foreign window")

    def test_inherited_marker_on_a_user_shell_is_not_killable(self):
        """Pane options are inherited by splits, so @hx alone cannot authorise a kill."""
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00102", [{"goal": "guard"}])
            self.up(box)
            win = box.window(AGENTS)
            self.assertIsNotNone(win)

            # A user splits their own shell INSIDE our window; the split inherits
            # the marker from the pane it was made from but was started without
            # a command, so ownership provenance must still refuse the kill.
            target = _tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                           "-t", win["id"], "-c", str(REPO))[1].strip()
            self.assertTrue(target)

            code = ("from hermes_tmux import tmuxio; "
                    "ok, why = tmuxio.close_pane(%r); "
                    "print('RESULT', ok, why)" % target)
            self.assertEqual(box.run("%s -c %s" % (PY, _q(code))), 0)
            painted = box.capture()
            self.assertIn("RESULT False", painted,
                          "a user shell inside our window was killable: %r"
                          % painted[-300:])
            # Either refusal is correct; what matters is that it is refused.
            self.assertTrue("no ownership provenance" in painted or "not ours" in painted,
                            painted[-300:])
            self.assertTrue(_pane_exists(target), "the refused kill still removed the pane")

    def test_up_never_respawns_a_user_pane_that_took_the_first_slot(self):
        """The overview slot is positional; a user split may occupy it."""
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00103", [{"goal": "overview slot"}])
            self.up(box)
            win = box.window(AGENTS)
            self.assertIsNotNone(win)

            # The user opens a shell in our window and our overview dies.
            user_pane = _tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                              "-t", win["id"], "-c", str(REPO))[1].strip()
            self.assertTrue(user_pane)
            user_pid = _pane_pid(user_pane)
            overview = box.layout()["overview_pane"]
            _tmux("kill-pane", "-t", overview)
            self.assertTrue(wait_for(lambda: not _pane_exists(overview), timeout=5))

            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(_pid_alive(user_pid),
                            "up killed the user's shell in the overview slot")
            new_overview = box.layout().get("overview_pane")
            self.assertTrue(new_overview)
            self.assertNotEqual(new_overview, overview)
            self.assertNotEqual(new_overview, user_pane,
                                "up respawned the user's own shell as the overview")
            self.assertTrue(wait_for(
                lambda: "HERMES AGENTS" in box.capture(new_overview), timeout=10),
                "no overview was rebuilt")

    def test_window_marker_does_not_make_its_panes_ours(self):
        """Ownership is the pane's own option; a marked window is not enough.

        `#{@hx}` falls back pane -> window -> session, so a format read would call
        every pane in our window ours. This pins the `show-options -p` read.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00107", [{"goal": "ownership"}])
            self.up(box)
            win = box.window(AGENTS)
            self.assertIsNotNone(win)

            new = _tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                        "-t", win["id"], "-c", str(REPO))[1].strip()
            self.assertTrue(new)
            self.assertNotEqual(_format(new, "#{@hx}"), "",
                                "the window marker is expected to leak through the "
                                "format read")
            code = ("from hermes_tmux import tmuxio; "
                    "print('OWNS', tmuxio.owns_pane(%r))" % new)
            self.assertEqual(box.run("%s -c %s" % (PY, _q(code))), 0)
            self.assertTrue(wait_for(lambda: "OWNS False" in box.capture()),
                            "a pane with no option of its own was reported ours")

    def test_foreign_pane_is_never_killed(self):
        """``close_pane`` refuses a pane we did not create (``not ours``).

        The test exits non-zero from the child when the guard allows the kill, so a
        regression cannot be mistaken for a passing probe.
        """
        with Sandbox() as box:
            _tmux("new-window", "-d", "-t", box.name, "-n", "userwork", "exec /bin/zsh -f")
            user_win = box.window("userwork")
            pane = box.panes_of(user_win["id"])[0]["id"]
            code = ("import sys; from hermes_tmux import tmuxio; "
                    "ok, why = tmuxio.close_pane(%r); "
                    "print('RESULT', ok, why); sys.exit(0 if (ok is False) else 1)" % pane)
            self.assertEqual(box.run("%s -c %s" % (PY, _q(code))), 0,
                             "guard allowed killing a foreign pane")
            self.assertTrue(wait_for(lambda: "RESULT False not ours" in box.capture()))

    # ---- 8. aux panes ----------------------------------------------------
    def test_aux_panes_are_lazy_and_reused(self):
        """Aux windows are created on first use and reused afterwards, by window id.

        The id, not the name, is compared: a recreate could reuse the name and still
        have built a second window.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00014", [{"goal": "aux"}])
            self.up(box)
            self.assertIsNone(box.window("hx:tests"), "tests pane must be lazy")
            self.assertEqual(box.cli("tests"), 0)
            self.assertIsNotNone(box.window("hx:tests"))
            first = box.window("hx:tests")["id"]
            self.assertEqual(box.cli("tests"), 0)
            self.assertEqual(box.window("hx:tests")["id"], first, "aux pane was recreated")
            for role in ("logs", "debug", "rea"):
                self.assertEqual(box.cli(role), 0)
                self.assertTrue(wait_for(lambda r=role: box.window("hx:" + r) is not None),
                                "hx:%s not created" % role)

    def test_aux_panes_show_real_output_not_placeholders(self):
        """Each aux pane must paint evidence from the real system."""
        with Sandbox() as box:
            # A real agent.log in this sandbox's HERMES_HOME.
            logdir = box.home / "logs"
            logdir.mkdir(parents=True, exist_ok=True)
            (logdir / "agent.log").write_text("SENTINEL-AGENT-LOG-LINE\n", encoding="utf-8")

            for role in ("logs", "tests", "debug", "rea"):
                rc = box.cli(role)
                self.assertEqual(rc, 0, "opening hx:%s failed (rc=%s): %r"
                                 % (role, rc, box.capture()[-600:]))
                win = box.window("hx:" + role)
                self.assertIsNotNone(win, "hx:%s not created" % role)
                pane = box.panes_of(win["id"])[0]["id"]

                if role == "logs":
                    self.assertTrue(wait_for(lambda: "SENTINEL-AGENT-LOG-LINE" in box.capture(pane)),
                                    "logs pane did not tail the real agent.log")
                elif role == "tests":
                    # The tests pane must not execute the suite by default (that
                    # would recurse into this very run). It shows either the last
                    # recorded run or the exact opt-in command.
                    self.assertTrue(wait_for(
                        lambda: "tests --run" in box.capture(pane)
                        or "last recorded test run" in box.capture(pane), timeout=10),
                        "tests pane showed no real guidance: %r" % box.capture(pane)[:400])
                elif role == "debug":
                    self.assertTrue(wait_for(lambda: "HERMES_HOME=" in box.capture(pane)),
                                    "debug pane printed no environment")
                    self.assertIn(str(box.home), box.capture(pane))
                else:  # rea
                    text = box.capture(pane)
                    self.assertTrue("rea" in text.lower() or "REA" in text,
                                    "rea pane printed nothing about REA: %r" % text[:300])
                    self.assertNotIn("fake", text.lower())

    def test_tests_run_routes_and_preserves_the_pane_and_status(self):
        """``tests --run`` routes through TestBench, keeping window, pane and shell.

        A fake ``testbench`` on PATH records its argv, so the routing (profile,
        suite, ``--stream``) and the propagated exit status are both observable. The
        pane must be the same pane afterwards, running a shell, and the reader's own
        pane pid must be unchanged: a test runner that hijacks the caller's pane is
        the failure.
        """
        with Sandbox() as box:
            primary = _pane_pid(box.pane_id)
            bin_dir = box.home / ".local/bin"
            bin_dir.mkdir(parents=True)
            self.assertEqual(_tmux("set-environment", "-t", box.name, "HOME", str(box.home))[0], 0)
            (bin_dir / "tmux").symlink_to(TMUX)
            box.export("HOME", str(box.home))
            box.export("PATH", f"{bin_dir}:/usr/bin:/bin")
            binary = bin_dir / "testbench"
            test_log = box.home / "cache/hermes-tmux/tests.log"

            self.assertEqual(box.cli("tests"), 0)
            try:
                missing_rc = box.cli("tests", "--run", timeout=5)
            except AssertionError as exc:
                window = box.window("hx:tests")
                assert window is not None
                pane = box.panes_of(window["id"])[0]["id"]
                self.fail(f"{exc}; hx:tests output: {box.capture(pane)!r}")
            self.assertEqual(missing_rc, 127)
            tests_window = box.window("hx:tests")
            assert tests_window is not None
            win = tests_window["id"]
            pane = box.panes_of(win)[0]["id"]
            self.assertIn("TestBench unavailable; no tests ran", box.capture(pane))
            self.assertIn("exit status: 127", test_log.read_text())

            for rc in (0, 1):
                binary.write_text(f'#!/bin/sh\nprintf "TestBench args: %s\\n" "$*"\nexit {rc}\n')
                binary.chmod(0o700)
                self.assertEqual(box.cli("tests", "--run"), rc)
                self.assertEqual(box.window("hx:tests"), tests_window)
                self.assertEqual(box.panes_of(win)[0]["id"], pane)
                self.assertEqual(_format(box.name, "#{window_id}"), win)
                text = test_log.read_text()
                self.assertIn(f"TestBench args: run {REPO} --profile regression --suite full-tmux-regression --stream", text)
                self.assertIn(f"exit status: {rc}", text)
                self.assertTrue(wait_for(lambda: _pane_command(pane) in ("zsh", "bash", "sh")))
            self.assertEqual(_pane_pid(box.pane_id), primary)

    def test_tests_run_reports_interpreter_start_failure(self):
        """A missing interpreter is reported in the pane with 127, not silently."""
        with Sandbox() as box:
            box.export("HX_PYTHON", str(box.home / "missing-python"))
            self.assertEqual(box.cli("tests", "--run", timeout=8), 127)
            tests_window = box.window("hx:tests")
            assert tests_window is not None
            pane = box.panes_of(tests_window["id"])[0]["id"]
            self.assertIn("missing-python", box.capture(pane))

    def test_tests_run_interrupt_reaps_testbench_and_keeps_test_shell(self):
        """Interrupting reaps TestBench and leaves the pane usable.

        Both SIGINT paths are exercised: Ctrl-C typed into the tests pane and SIGINT to
        the invoking process. Exit 130, a shell left in the pane, and a TestBench pid
        that no longer exists are the three claims; the last is what separates "the
        command returned" from "the child was reaped".
        """
        for target in ("test pane", "invoking process"):
            with self.subTest(target=target):
                self._assert_tests_run_interrupt(target)

    def _assert_tests_run_interrupt(self, target):
        """Drive ``tests --run`` in a real process against a TestBench that blocks.

        The blocking child prints its pid, which is how "reaped" is checked after the
        interrupt; the pane must be back to a shell, so the next run is not wedged.
        """
        with Sandbox() as box:
            bin_dir = box.home / ".local/bin"
            bin_dir.mkdir(parents=True)
            self.assertEqual(_tmux("set-environment", "-t", box.name, "HOME", str(box.home))[0], 0)
            binary = bin_dir / "testbench"
            binary.write_text(f"#!{PY}\nimport os, signal\nprint('ready', os.getpid(), flush=True)\nsignal.pause()\n")
            binary.chmod(0o700)
            env = dict(os.environ, HOME=str(box.home), HERMES_HOME=str(box.home), HERMES_TMUX="1", TMUX_PANE=box.pane_id,
                       TMUX=_format(box.pane_id, "#{socket_path},#{pid},0"))
            log = box.home / "cache/hermes-tmux/tests.log"
            with subprocess.Popen([PY, "-m", "hermes_tmux", "tests", "--run"], env=env, cwd=REPO) as command:
                try:
                    self.assertTrue(wait_for(lambda: log.is_file() and "ready " in log.read_text()))
                    pid = int(log.read_text().split("ready ")[1].splitlines()[0])
                    tests_window = box.window("hx:tests")
                    assert tests_window is not None
                    pane = box.panes_of(tests_window["id"])[0]["id"]
                    if target == "test pane":
                        self.assertEqual(_tmux("send-keys", "-t", pane, "C-c")[0], 0)
                    else:
                        command.send_signal(2)  # SIGINT as if typed in the invoking pane
                    self.assertEqual(command.wait(timeout=10), 130)
                    self.assertTrue(wait_for(lambda: _pane_command(pane) in ("zsh", "bash", "sh")))
                    self.assertIn("exit status: 130", log.read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                finally:
                    if command.poll() is None:
                        command.kill()

    def test_two_sessions_never_share_workspace_state(self):
        """Concurrency isolation: one server, two sessions, disjoint layouts."""
        with Sandbox() as first:
            second_name = first.name + "_b"
            rc, _out = _tmux("new-session", "-d", "-s", second_name,
                             "-c", str(REPO), "exec /bin/zsh -f")
            self.assertEqual(rc, 0)
            try:
                second = Sandbox()
                second.name = second_name
                second.created = True
                second.pane_id = _tmux("list-panes", "-t", second_name,
                                       "-F", "#{pane_id}")[1].split()[0]
                second.run("export HERMES_HOME=%s" % second.home)
                second.run("export HERMES_TMUX=1")
                second.run("export PYTHONPATH=%s" % REPO)

                first.add_delegation("deleg_iso00001", [{"goal": "session one"}])
                second.add_delegation("deleg_iso00002", [{"goal": "session two"}])
                self.assertEqual(first.cli("up"), 0)
                self.assertEqual(second.cli("up"), 0)

                self.assertTrue(wait_for(
                    lambda: "session one" in first.render_text(), timeout=15))
                self.assertTrue(wait_for(
                    lambda: "session two" in second.render_text(), timeout=15))
                self.assertNotIn("session two", first.render_text())
                self.assertNotIn("session one", second.render_text())

                first_keys = {child.name for child in
                              (first.home / "cache" / "hermes-tmux" / "sessions").iterdir()}
                second_keys = {child.name for child in
                               (second.home / "cache" / "hermes-tmux" / "sessions").iterdir()}
                self.assertEqual(len(first_keys), 1)
                self.assertEqual(len(second_keys), 1)
                self.assertNotEqual(first_keys, second_keys)
            finally:
                _tmux("kill-session", "-t", second_name)
                shutil.rmtree(second.home, ignore_errors=True)

    # ---- 9. gate / fallback ---------------------------------------------
    def test_gate_off_builds_nothing(self):
        """``HERMES_TMUX=0`` builds no window and says why; ``status`` still works."""
        with Sandbox() as box:
            box.run("export HERMES_TMUX=0")
            box.add_delegation("deleg_aaa00015", [{"goal": "gated"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertIn("HERMES_TMUX=0", box.capture())
            self.assertIsNone(box.window(AGENTS))
            self.assertEqual(box.cli("status"), 0)
            self.assertIn("gated", box.capture())

    def test_outside_tmux_degrades_gracefully(self):
        """``up`` outside tmux exits 0 with a message, and ``status`` still prints.

        No TMUX/TMUX_PANE is inherited, so this is the real "not in tmux" path rather
        than a mock of it.
        """
        home = Path(tempfile.mkdtemp(prefix="hxtmux_home_"))
        try:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("TMUX", "TMUX_PANE")}
            env.update({"HERMES_HOME": str(home), "HERMES_TMUX": "1",
                        "PYTHONPATH": str(REPO)})
            proc = subprocess.run([PY, "-m", "hermes_tmux", "up"],
                                  capture_output=True, text=True, env=env,
                                  cwd=str(REPO), timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("not inside tmux", proc.stdout)
            status = subprocess.run([PY, "-m", "hermes_tmux", "status"],
                                    capture_output=True, text=True, env=env,
                                    cwd=str(REPO), timeout=30)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertTrue(status.stdout.strip())
        finally:
            shutil.rmtree(home, ignore_errors=True)


def _q(text: str) -> str:
    """Single-quote a Python snippet for the pane shell."""
    return "'" + text.replace("'", "'\\''") + "'"


def _pid_alive(pid: Optional[int]) -> bool:
    """``kill(pid, 0)`` probe for a pid this suite started."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _watcher_pid(box: Sandbox) -> Optional[int]:
    """The live coordinator pid recorded under this home, or None.

    A recorded pid is only returned when the process still answers ``kill(pid, 0)``;
    liveness of the workspace itself is asserted separately by callers.
    """
    root = box.home / "cache" / "hermes-tmux" / "sessions"
    if not root.is_dir():
        return None
    for child in root.iterdir():
        pid_file = child / "watch.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                return None
            try:
                os.kill(pid, 0)
            except OSError:
                return None
            return pid
    return None


if __name__ == "__main__":
    unittest.main(verbosity=2)
