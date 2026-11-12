"""End-to-end tests against real, isolated tmux sessions.

Rules enforced by this harness:

* every session it creates is named ``hx_it_<pid>_<n>`` and is killed in a
  ``finally``; the harness refuses to touch any other session, so a user
  session (``main``) can never be listed, renamed, or killed here;
* every test owns a private ``HERMES_HOME`` under a TemporaryDirectory, so no
  real Hermes state is read or written;
* assertions read what tmux actually painted (``capture-pane``) or what the
  process actually wrote (layout/render files), never a claimed value.

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
from pathlib import Path
from typing import Callable, List, Optional, Tuple

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


def _tmux(*args: str) -> Tuple[int, str]:
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
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


_counter = [0]


class Sandbox:
    """One disposable tmux session + its private HERMES_HOME."""

    def __init__(self, columns: int = 160, rows: int = 45):
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
        if self.created:
            _tmux("kill-session", "-t", self.name)
        shutil.rmtree(self.home, ignore_errors=True)
        return False

    # -- driving the pane ---------------------------------------------------
    def pane(self, index: int = 0) -> str:
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

    def cli(self, *args: str, timeout: float = 20.0) -> int:
        return self.run("%s -m hermes_tmux %s" % (PY, " ".join(args)), timeout=timeout)

    def capture(self, target: Optional[str] = None) -> str:
        rc, out = _tmux("capture-pane", "-p", "-t", target or self.pane_id)
        return out

    def windows(self) -> List[dict]:
        rc, out = _tmux("list-windows", "-t", self.name,
                        "-F", "#{window_id}\t#{window_name}\t#{@hx}")
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                rows.append({"id": parts[0], "name": parts[1],
                             "ours": parts[2].strip() == "1"})
        return rows

    def window(self, name: str) -> Optional[dict]:
        for win in self.windows():
            if win["name"] == name or win["name"].startswith(name + " "):
                return win
        return None

    def pane_title(self, pane_id: str) -> str:
        rc, out = _tmux("display-message", "-p", "-t", pane_id, "#{pane_title}")
        return out.strip()

    def panes_of(self, window_id: str) -> List[dict]:
        rc, out = _tmux("list-panes", "-t", window_id,
                        "-F", "#{pane_id}\t#{@hx}\t#{pane_title}\t#{pane_current_command}")
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                rows.append({"id": parts[0], "ours": parts[1].strip() == "1",
                             "title": parts[2], "command": parts[3]})
        return rows

    def layout(self) -> dict:
        root = self.home / "cache" / "hermes-tmux" / "sessions"
        if not root.is_dir():
            return {}
        for child in root.iterdir():
            candidate = child / "layout.json"
            if candidate.exists():
                try:
                    return json.loads(candidate.read_text(encoding="utf-8"))
                except ValueError:
                    return {}
        return {}

    def render_text(self) -> str:
        root = self.home / "cache" / "hermes-tmux" / "sessions"
        if not root.is_dir():
            return ""
        for child in root.iterdir():
            candidate = child / "render.txt"
            if candidate.exists():
                return candidate.read_text(encoding="utf-8")
        return ""

    # -- fixtures ----------------------------------------------------------
    def live(self) -> Path:
        root = self.home / "cache" / "delegation" / "live"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def add_delegation(self, deleg_id: str, tasks: List[dict],
                       model: str = "deepseek/deepseek-v4.1-flash-1017",
                       completed: Optional[str] = None) -> Path:
        d = self.live() / deleg_id
        d.mkdir(parents=True, exist_ok=True)
        manifest_tasks = []
        for i, task in enumerate(tasks):
            log = d / ("task-%d.log" % i)
            lines = task.get("lines") or [
                "=== Hermes subagent live transcript ===",
                "delegation: %s   task: %d" % (deleg_id, i),
                "goal: %s" % task.get("goal", "do the thing"),
                "started: 2026-11-12 09:00:00",
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
            "started": "2026-11-12 09:00:00",
            "task_count": len(tasks),
            "model": model,
            "provider": "nous",
            "tasks": manifest_tasks,
        }
        if completed:
            manifest["completed"] = completed
        (d / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return d

    def append_event(self, deleg_id: str, task: int, role: str, text: str) -> None:
        log = self.live() / deleg_id / ("task-%d.log" % task)
        stamp = time.strftime("%H:%M:%S")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("%s %-9s| %s\n" % (stamp, role, text))

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

    def write_review(self, delegation_id: str, task_index: Optional[int],
                     status: str = "recorded") -> None:
        path = self.home / "cache" / "hermes-tmux" / "reviews.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"reviews": []}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                data = {"reviews": []}
        data.setdefault("reviews", []).append({
            "id": "rev_test01", "delegation_id": delegation_id,
            "task_index": task_index, "reviewer_model": "z-ai/glm-5.3-flash",
            "status": status, "report": None, "at": "2026-11-12 09:05:00",
        })
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


@unittest.skipIf(SKIP, "tmux not installed")
class IntegrationTest(unittest.TestCase):
    maxDiff = None

    # ---- 1. startup ------------------------------------------------------
    def test_up_builds_workers_window_and_overview(self):
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00001", [{"goal": "audit the parser"}])
            self.assertEqual(box.cli("up"), 0)
            win = box.window("hx:workers")
            self.assertIsNotNone(win, "hx:workers was not created")
            self.assertTrue(win["ours"], "window not marked ours")
            self.assertTrue(wait_for(lambda: "audit the parser" in box.render_text()),
                            "overview never rendered the worker")
            self.assertTrue(wait_for(lambda: "audit the parser" in box.capture(win["id"])),
                            "pane did not paint the render")

    def test_up_is_idempotent_and_reuses_panes(self):
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00002", [{"goal": "task one"}])
            self.assertEqual(box.cli("up"), 0)
            first = box.layout()
            self.assertTrue(wait_for(lambda: bool(box.layout().get("workers_window"))))
            follow_pid = _tmux("display-message", "-p", "-t",
                               first["overview_pane"], "#{pane_pid}")[1].strip()
            self.assertEqual(box.cli("up"), 0)
            # A second `up` must not restart the display loop: "reuses panes"
            # means the same process, not just the same pane id.
            self.assertTrue(wait_for(
                lambda: _tmux("display-message", "-p", "-t",
                              box.layout()["overview_pane"], "#{pane_pid}")[1].strip()
                == follow_pid),
                "the overview process was restarted by an idempotent up")
            second = box.layout()
            self.assertEqual(first.get("workers_window"), second.get("workers_window"))
            self.assertEqual(first.get("overview_pane"), second.get("overview_pane"))
            workers = [w for w in box.windows() if w["name"].startswith("hx:workers")]
            self.assertEqual(len(workers), 1, "up created a second workers window")

    # ---- 2. worker panes -------------------------------------------------
    def test_worker_panes_created_per_running_worker(self):
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00003",
                               [{"goal": "w0"}, {"goal": "w1"}, {"goal": "w2"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: len(box.layout().get("tail_panes") or {}) == 3),
                            "expected 3 tail panes, got %r" % (box.layout().get("tail_panes")))
            overview_win = box.window("hx:workers")
            self.assertEqual(len(box.panes_of(overview_win["id"])), 1,
                             "the overview window must hold the overview only")
            worker_wins = sorted(
                (w for w in box.windows() if w["name"].startswith("hx:worker-")),
                key=lambda w: w["name"])
            self.assertEqual(len(worker_wins), 2, "3 workers need 2 windows of at most 2")
            panes = [p for w in worker_wins for p in box.panes_of(w["id"])]
            self.assertEqual(len(panes), 3, "expected one pane per worker across windows")
            self.assertTrue(all(len(box.panes_of(w["id"])) <= 2 for w in worker_wins),
                            "a worker window exceeded two panes")
            titles = " ".join(p["title"] for p in panes)
            self.assertIn("t0", titles)
            self.assertIn("t2", titles)

    def test_pane_title_distinguishes_running_from_completed(self):
        """A pane tailing a finished transcript must say so.

        The tail stream alone cannot distinguish "still executing" from
        "finished, nothing new"; the border label carries the lifecycle state.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_live00001", [{"goal": "still working"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: len(box.layout().get("tail_panes") or {}) == 1))
            pane = list(box.layout()["tail_panes"])[0]

            # A fresh event makes the worker "recently active"; the fixture's
            # header timestamp alone is old enough to be labelled stalled.
            box.append_event("deleg_live00001", 0, "tool", "-> terminal(ls)")
            self.assertTrue(wait_for(lambda: " run " in box.pane_title(pane)),
                            "running worker not labelled run: %r" % box.pane_title(pane))

            # Same worker, now finished: rewrite the manifest status and append
            # the final marker the writer emits.
            box.add_delegation("deleg_live00001",
                               [{"goal": "still working", "status": "completed"}],
                               completed="2026-11-12 09:30:00")
            box.append_event("deleg_live00001", 0, "final",
                             "status=completed duration=3.0s")
            self.assertTrue(wait_for(lambda: " done " in box.pane_title(pane), timeout=20),
                            "completed worker still labelled running: %r"
                            % box.pane_title(pane))

    def test_max_two_panes_per_window_and_titles_carry_identity(self):
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00004",
                               [{"goal": "w%d" % i} for i in range(6)])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: len(box.layout().get("tail_panes") or {}) == 6,
                            timeout=20),
                            "expected six panes, got %r" % (box.layout().get("tail_panes")))
            overview_win = box.window("hx:workers")
            self.assertEqual(len(box.panes_of(overview_win["id"])), 1)
            worker_wins = [w for w in box.windows() if w["name"].startswith("hx:worker-")]
            self.assertEqual(len(worker_wins), 3, "6 workers must fill 3 windows")
            titles = []
            for win in worker_wins:
                panes = box.panes_of(win["id"])
                self.assertLessEqual(len(panes), 2,
                                     "window %s has more than two panes" % win["name"])
                titles.extend(p["title"] for p in panes)
            self.assertIn("deepseek-v4.1-flash", " ".join(titles))

    def test_new_worker_gets_its_own_pane_and_old_pane_is_not_stolen(self):
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00005", [{"goal": "first"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: len(box.layout().get("tail_panes") or {}) == 1))
            panes = box.layout()["tail_panes"]
            first_pane, first_worker = next(iter(panes.items()))
            self.assertIn("aaa00005", first_worker)

            box.add_delegation("deleg_bbb00006", [{"goal": "second"}])
            self.assertTrue(wait_for(
                lambda: any("bbb00006" in (v or "")
                            for v in (box.layout().get("tail_panes") or {}).values()),
                timeout=15),
                "no pane was created for the newly visible worker")

            after = box.layout()["tail_panes"]
            self.assertEqual(len(after), 2)
            # The pane the user may be reading stays bound to its own worker.
            self.assertEqual(after.get(first_pane), first_worker,
                             "an existing pane was repointed at a different worker")

    def test_worker_command_focuses_the_pane_in_stable_slot_order(self):
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00019",
                               [{"goal": "w%d" % i} for i in range(3)])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: len(box.layout().get("tail_panes") or {}) == 3))
            layout = box.layout()
            assign = layout["worker_assign"]
            order = sorted(
                layout["tail_panes"],
                key=lambda p: (int(assign[layout["tail_panes"][p]]),
                               int(p.lstrip("%"))))
            self.assertEqual(box.cli("worker", "2"), 0)
            active = _tmux("display-message", "-p", "-t", box.name, "#{pane_id}")[1].strip()
            self.assertEqual(active, order[1],
                             "worker 2 did not focus the second worker in slot order")
            active_window = _tmux("display-message", "-p", "-t", box.name,
                                  "#{window_id}")[1].strip()
            self.assertEqual(active_window,
                             _tmux("display-message", "-p", "-t", active,
                                   "#{window_id}")[1].strip())

    def test_v1_layout_migrates_worker_panes_out_of_the_overview_window(self):
        """An installed v1 layout keeps worker panes in hx:workers.

        On upgrade the overview window must end up overview-only and the worker
        panes must reappear in hx:worker-* windows, without killing a pane the
        user split into that window.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_v1mig001", [{"goal": "a"}, {"goal": "b"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: len(box.layout().get("tail_panes") or {}) == 2))
            overview = box.window("hx:workers")
            layout = box.layout()
            old_panes = list(layout["tail_panes"])
            # Move the two panes into the overview window, as v1 had them, and
            # rewrite layout.json into the v1 shape.
            for pane_id in old_panes:
                _tmux("join-pane", "-d", "-s", pane_id, "-t", overview["id"])
            self.assertTrue(wait_for(
                lambda: len(box.panes_of(overview["id"])) == 3, timeout=10))
            for win in [w for w in box.windows() if w["name"].startswith("hx:worker-")]:
                _tmux("kill-window", "-t", win["id"])
            _tmux("split-window", "-d", "-t", overview["id"], "-h")
            user_pane = [p["id"] for p in box.panes_of(overview["id"])
                         if p["id"] not in old_panes
                         and p["id"] != layout["overview_pane"]][0]
            self.assertTrue(user_pane)
            sessions = box.home / "cache" / "hermes-tmux" / "sessions"
            path = next(sessions.iterdir()) / "layout.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("worker_windows", None)
            data.pop("worker_assign", None)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")

            box.append_event("deleg_v1mig001", 0, "tool", "-> terminal(ls)")
            self.assertTrue(wait_for(
                lambda: len(box.layout().get("worker_windows") or {}) == 1, timeout=20),
                "worker windows were not rebuilt: %r" % box.layout())
            overview_panes = box.panes_of(overview["id"])
            self.assertEqual(len([p for p in overview_panes if p["id"] in old_panes]), 0,
                             "overview window still holds our worker panes")
            self.assertIn(user_pane, [p["id"] for p in overview_panes],
                          "migration destroyed a user pane in the overview window")
            worker_wins = [w for w in box.windows() if w["name"].startswith("hx:worker-")]
            self.assertEqual(len(worker_wins), 1)
            self.assertEqual(len(box.panes_of(worker_wins[0]["id"])), 2)
            for pane_id in old_panes:
                self.assertFalse(
                    _tmux("display-message", "-p", "-t", pane_id, "#{pane_id}")[1].strip()
                    == pane_id, "v1 pane %s survived the migration" % pane_id)

    def test_worker_window_counts_for_zero_through_eight_workers(self):
        for count in (0, 1, 2, 3, 4, 8):
            with self.subTest(workers=count):
                with Sandbox() as box:
                    if count:
                        box.add_delegation("deleg_cnt%05d" % count,
                                           [{"goal": "w%d" % i} for i in range(count)])
                    self.assertEqual(box.cli("up"), 0)
                    self.assertTrue(wait_for(
                        lambda: len(box.layout().get("tail_panes") or {}) == count,
                        timeout=20))
                    overview = box.window("hx:workers")
                    self.assertEqual(len(box.panes_of(overview["id"])), 1,
                                     "overview window must hold one pane")
                    worker_wins = [w for w in box.windows()
                                   if w["name"].startswith("hx:worker-")]
                    self.assertEqual(len(worker_wins), (count + 1) // 2,
                                     "%d workers must fill ceil(%d/2) windows"
                                     % (count, count))
                    for win in worker_wins:
                        self.assertLessEqual(len(box.panes_of(win["id"])), 2,
                                             "%s has more than two panes" % win["name"])

    # ---- 3. events -------------------------------------------------------
    def test_up_rebuilds_a_lost_overview_without_repurposing_survivors(self):
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00106", [{"goal": "rebuild"}])
            self.assertEqual(box.cli("up"), 0)
            win = box.window("hx:workers")
            old_overview = box.layout()["overview_pane"]
            self.assertTrue(wait_for(
                lambda: len(box.layout().get("tail_panes") or {}) == 1, timeout=15),
                "no worker pane was created for the delegation")

            worker_wins = [w for w in box.windows() if w["name"].startswith("hx:worker-")]
            self.assertTrue(worker_wins, "expected a worker window")
            survivors = {p["id"]: _tmux("display-message", "-p", "-t", p["id"],
                                        "#{pane_pid}")[1].strip()
                         for w in worker_wins for p in box.panes_of(w["id"])}
            self.assertTrue(survivors, "expected at least one surviving worker pane")

            _tmux("kill-pane", "-t", old_overview)
            time.sleep(0.3)
            self.assertEqual(box.cli("up"), 0)

            new_overview = box.layout()["overview_pane"]
            self.assertNotIn(new_overview, survivors,
                             "up repurposed a surviving pane as the overview")
            for pane_id, pid in survivors.items():
                now_pid = _tmux("display-message", "-p", "-t", pane_id, "#{pane_pid}")[1].strip()
                self.assertEqual(now_pid, pid,
                                 "up restarted the surviving pane %s" % pane_id)
            self.assertTrue(wait_for(
                lambda: "hermes subagents" in box.capture(new_overview), timeout=10))

    def test_coordinator_is_recognised_while_it_lives(self):
        """Positive control: the identity check must say yes to the real thing."""
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00104", [{"goal": "identity"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: _watcher_pid(box) is not None))
            key = None
            for child in (box.home / "cache" / "hermes-tmux" / "sessions").iterdir():
                if (child / "watch.pid").exists():
                    key = child.name
            self.assertIsNotNone(key)
            code = ("from hermes_tmux import cli; "
                    "print('ALIVE', cli._watcher_alive(%r))" % key)
            self.assertEqual(box.run("%s -c %s" % (PY, _q(code))), 0)
            self.assertTrue(wait_for(lambda: "ALIVE True" in box.capture()),
                            "the live coordinator was not recognised")

    def test_events_stream_in_order_into_the_worker_pane(self):
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00007", [{"goal": "ordered"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: len(box.layout().get("tail_panes") or {}) == 1))
            pane = list(box.layout()["tail_panes"])[0]
            for i in range(3):
                box.append_event("deleg_aaa00007", 0, "tool", "-> step%d(...)" % i)
                time.sleep(0.05)
            box.append_event("deleg_aaa00007", 0, "final", "status=completed duration=1.0s")
            self.assertTrue(wait_for(lambda: "step2" in box.capture(pane)),
                            "worker pane never showed the appended events")
            painted = box.capture(pane)
            self.assertLess(painted.index("step0"), painted.index("step1"))
            self.assertLess(painted.index("step1"), painted.index("step2"))

    def test_overview_updates_on_transcript_events_without_restart(self):
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00008", [{"goal": "live update"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: "live update" in box.render_text()))
            box.append_event("deleg_aaa00008", 0, "tool", "-> terminal(ls)")
            self.assertTrue(wait_for(lambda: "terminal" in box.render_text()),
                            "render.txt did not follow the transcript event")

    def test_state_db_completion_refreshes_overview_without_a_transcript_write(self):
        """A task can finish only in state.db. That must still repaint.

        Before the database was watched, the row kept its old running state
        until an unrelated transcript append happened to fire the coordinator.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_db00001", [{"goal": "db driven"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: len(box.layout().get("tail_panes") or {}) == 1))
            pane = list(box.layout()["tail_panes"])[0]
            box.append_event("deleg_db00001", 0, "tool", "-> terminal(ls)")
            self.assertTrue(wait_for(lambda: " run " in box.pane_title(pane), timeout=10),
                            "fixture did not start as running: %r" % box.pane_title(pane))

            box.write_db_state("deleg_db00001", "completed")
            self.assertTrue(wait_for(lambda: " done " in box.pane_title(pane), timeout=15),
                            "pane label did not follow the state.db completion")
            self.assertTrue(wait_for(lambda: "done 1" in box.render_text(), timeout=15),
                            "overview did not follow the state.db completion: %r"
                            % box.render_text().splitlines()[:2])

    def test_overview_title_is_not_scrolled_off_in_a_short_pane(self):
        """Writing exactly `height` lines with a trailing newline scrolls one
        line: the title (running/total, age) vanished and the pane looked dead.
        """
        with Sandbox(columns=100, rows=8) as box:
            box.add_delegation("deleg_short0001",
                               [{"goal": "w%d" % i} for i in range(3)])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: len(box.layout().get("tail_panes") or {}) == 3))
            overview = box.layout()["overview_pane"]
            self.assertTrue(wait_for(
                lambda: "hermes subagents" in box.capture(overview), timeout=10),
                "the overview title scrolled out of a short pane: %r"
                % box.capture(overview).splitlines()[:3])

    # ---- 4. failure ------------------------------------------------------
    def test_failed_task_renders_as_failure_not_success(self):
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00009", [{
                "goal": "broken", "status": "failed", "exit_reason": "error",
                "lines": [
                    "=== Hermes subagent live transcript ===",
                    "delegation: deleg_aaa00009   task: 0",
                    "goal: broken", "started: 2026-11-12 09:00:00",
                    "(append-only)", "=" * 40,
                    "09:00:05 result   | terminal ERROR 5.0s: exit 1",
                    "09:00:06 final    | status=failed exit_reason=error",
                ]}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: "fail" in box.render_text()), )
            text = box.render_text()
            self.assertIn("fail", text)
            self.assertNotIn("done", text)

    def test_missing_delegation_directory_is_tolerated(self):
        with Sandbox() as box:
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: "no delegations" in box.render_text().lower()))
            self.assertEqual(box.cli("status"), 0)

    # ---- 5. review lifecycle --------------------------------------------
    def test_review_record_changes_display_and_is_listed(self):
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00010", [{"goal": "reviewable"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: "review" in box.render_text()))
            self.assertIn("unreviewed", box.render_text())
            self.assertEqual(box.cli("review", "record", "--delegation", "deleg_aaa00010",
                                     "--task", "0", "--status", "recorded"), 0)
            self.assertTrue(wait_for(lambda: "1 recorded" in box.render_text()),
                            "review record did not reach the overview: %r"
                            % box.render_text()[:400])
            self.assertEqual(box.cli("review", "list"), 0)
            self.assertIn("deleg_aaa00010", box.capture())

    def test_reviewer_role_never_inferred_from_executor(self):
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00011", [{"goal": "please review this code"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: "please review this code" in box.render_text()))
            # A goal that merely mentions "review" must not make the row a reviewer.
            self.assertNotIn("revw", box.render_text().split("please review")[0])

    # ---- 6. detach / reattach -------------------------------------------
    def test_detach_and_reattach_preserves_panes_and_watcher(self):
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00012", [{"goal": "survivor"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: len(box.layout().get("tail_panes") or {}) == 1))
            before = box.layout()
            pid_before = (box.home / "cache" / "hermes-tmux" / "sessions")
            self.assertTrue(wait_for(lambda: _watcher_pid(box) is not None))
            watcher_before = _watcher_pid(box)

            # A real detach needs a real client: `attach` fails with "not a
            # terminal" in a test process, so run one under a pty via `script`.
            # A real client needs a real tty that stays open. `script` exits as
            # soon as its stdin closes, so the pty is created here and handed to
            # the child directly.
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
            self.assertEqual(before.get("tail_panes"), after.get("tail_panes"))
            self.assertEqual(_watcher_pid(box), watcher_before,
                             "watcher was restarted by an idempotent up")
            win = box.window("hx:workers")
            self.assertIsNotNone(win)
            self.assertEqual(len(box.panes_of(win["id"])), 1,
                             "the overview window must still hold only the overview")
            worker_wins = [w for w in box.windows() if w["name"].startswith("hx:worker-")]
            self.assertTrue(worker_wins, "worker windows did not survive a reattach")

    def test_coordinator_exits_when_its_session_disappears(self):
        """run-shell children outlive the server; the watcher must not.

        Without a liveness check every closed session/user session left one idle
        python process behind (93 were found and killed during development).
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00101", [{"goal": "lifecycle"}])
            self.assertEqual(box.cli("up"), 0)
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
            self.assertEqual(box.cli("up"), 0)
            win = box.window("hx:workers")
            self.assertIsNotNone(win)

            # The pane id comes back from the split itself: selecting by command
            # name races the coordinator's tail-pane creation.
            user_pane = _tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                              "-t", win["id"], "-c", str(REPO))[1].strip()
            self.assertTrue(user_pane)
            user_pid = _tmux("display-message", "-p", "-t", user_pane,
                             "#{pane_pid}")[1].strip()

            self.assertEqual(box.cli("down"), 0)
            time.sleep(0.5)

            self.assertIsNotNone(box.window("hx:workers"),
                                 "down destroyed a window containing a user pane")
            remaining = box.panes_of(win["id"])
            self.assertEqual([p["id"] for p in remaining], [user_pane],
                             "down left or removed the wrong panes: %r" % remaining)
            self.assertTrue(_pid_alive(int(user_pid)), "down killed the user's shell")

    def test_down_closes_only_our_windows_and_keeps_user_work(self):
        with Sandbox() as box:
            _tmux("new-window", "-d", "-t", box.name, "-n", "userwork", "exec /bin/zsh -f")
            box.add_delegation("deleg_aaa00013", [{"goal": "teardown"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertEqual(box.cli("logs"), 0)
            self.assertTrue(wait_for(lambda: box.window("hx:logs") is not None))
            self.assertEqual(box.cli("down"), 0)
            self.assertTrue(wait_for(lambda: box.window("hx:workers") is None))
            self.assertIsNone(box.window("hx:logs"))
            self.assertIsNotNone(box.window("userwork"),
                                 "down destroyed a window it did not create")
            sessions = box.home / "cache" / "hermes-tmux" / "sessions"
            left = list(sessions.iterdir()) if sessions.is_dir() else []
            self.assertEqual(left, [], "down left its state directory behind")

    def test_foreign_window_with_our_name_is_never_adopted(self):
        """A user window called hx:workers must not be taken over."""
        with Sandbox() as box:
            _tmux("new-window", "-d", "-t", box.name, "-n", "hx:workers",
                  "exec /bin/zsh -f")
            foreign = box.window("hx:workers")
            self.assertIsNotNone(foreign)
            box.add_delegation("deleg_aaa00100", [{"goal": "name clash"}])
            self.assertEqual(box.cli("up"), 0)

            # Two windows now: theirs, untouched, and ours under another name.
            windows = box.windows()
            same_name = [w for w in windows if w["name"] == "hx:workers"]
            self.assertEqual(len(same_name), 1, windows)
            self.assertFalse(same_name[0]["ours"],
                             "the tool adopted a window it did not create")
            self.assertEqual(same_name[0]["id"], foreign["id"])
            ours = [w for w in windows if w["ours"]]
            self.assertTrue(ours, "no workspace window was created")

            self.assertEqual(box.cli("down"), 0)
            self.assertIsNotNone(box.window("hx:workers"),
                                 "down closed a foreign window")

    def test_inherited_marker_on_a_user_shell_is_not_killable(self):
        """Pane options are inherited by splits, so @hx alone cannot authorise a kill."""
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00102", [{"goal": "guard"}])
            self.assertEqual(box.cli("up"), 0)
            win = box.window("hx:workers")
            self.assertIsNotNone(win)

            # A user splits their own shell INSIDE our window; the split inherits
            # the @hx marker from the pane it was made from.
            # A plain split (no command) is how a user opens a shell: it inherits
            # the @hx marker and has an empty start command. Take the id from the
            # split itself, never from a command-name search.
            target = _tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                           "-t", win["id"], "-c", str(REPO))[1].strip()
            self.assertTrue(target)

            code = ("import sys; from hermes_tmux import tmuxio; "
                    "ok, why = tmuxio.close_pane(%r); "
                    "print('RESULT', ok, why)" % target)
            self.assertEqual(box.run("%s -c %s" % (PY, _q(code))), 0)
            painted = box.capture()
            self.assertIn("RESULT False", painted,
                          "a user shell inside our window was killable: %r" % painted[-300:])
            # Either refusal is correct; what matters is that it is refused.
            self.assertTrue("no ownership provenance" in painted or "not ours" in painted,
                            painted[-300:])

    def test_up_never_respawns_a_user_pane_that_took_the_first_slot(self):
        """The overview slot is positional; a user split may occupy it."""
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00103", [{"goal": "overview slot"}])
            self.assertEqual(box.cli("up"), 0)
            win = box.window("hx:workers")
            self.assertIsNotNone(win)

            # The user opens a shell in our window and our overview dies.
            user_pane = _tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                              "-t", win["id"], "-c", str(REPO))[1].strip()
            self.assertTrue(user_pane)
            user_pid = _tmux("display-message", "-p", "-t", user_pane,
                             "#{pane_pid}")[1].strip()
            overview = box.layout()["overview_pane"]
            _tmux("kill-pane", "-t", overview)
            time.sleep(0.3)

            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(_pid_alive(int(user_pid)),
                            "up killed the user's shell in the overview slot")
            # And a working overview exists again.
            new_overview = box.layout().get("overview_pane")
            self.assertTrue(new_overview)
            self.assertNotEqual(new_overview, overview)
            self.assertTrue(wait_for(
                lambda: "hermes subagents" in box.capture(new_overview), timeout=10),
                "no overview was rebuilt")

    def test_window_marker_does_not_make_its_panes_ours(self):
        """Ownership is the pane's own option; a marked window is not enough.

        `#{@hx}` falls back pane -> window -> session, so a format read would call
        every pane in our window ours. This pins the `show-options -p` read.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00107", [{"goal": "ownership"}])
            self.assertEqual(box.cli("up"), 0)
            win = box.window("hx:workers")
            self.assertIsNotNone(win)

            new = _tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                        "-t", win["id"], "-c", str(REPO))[1].strip()
            self.assertTrue(new)
            self.assertNotEqual(
                _tmux("display-message", "-p", "-t", new, "#{@hx}")[1].strip(), "",
                "the window marker is expected to leak through the format read")
            code = ("from hermes_tmux import tmuxio; "
                    "print('OWNS', tmuxio.owns_pane(%r))" % new)
            self.assertEqual(box.run("%s -c %s" % (PY, _q(code))), 0)
            self.assertTrue(wait_for(lambda: "OWNS False" in box.capture()),
                            "a pane with no option of its own was reported ours")

    def test_foreign_pane_is_never_killed(self):
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
        with Sandbox() as box:
            box.add_delegation("deleg_aaa00014", [{"goal": "aux"}])
            self.assertEqual(box.cli("up"), 0)
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
        with Sandbox() as box:
            box.run("export HERMES_TMUX=0")
            box.add_delegation("deleg_aaa00015", [{"goal": "gated"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertIn("HERMES_TMUX=0", box.capture())
            self.assertIsNone(box.window("hx:workers"))
            self.assertEqual(box.cli("status"), 0)
            self.assertIn("gated", box.capture())

    def test_outside_tmux_degrades_gracefully(self):
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
    return "'" + text.replace("'", "'\\''") + "'"


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _watcher_pid(box: Sandbox) -> Optional[int]:
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
