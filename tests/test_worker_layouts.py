"""Independent regression tests for the worker-window layout contract.

The contract under test (implemented in ``hermes_tmux/watch.py`` / ``cli.py``):

* ``hx:workers`` holds exactly one pane, the overview;
* worker transcripts live in ``hx:worker-1``, ``hx:worker-2``, ... each with at
  most two panes;
* ``layout.json`` records ``workers_window``, ``overview_pane``,
  ``worker_windows`` (slot -> window id), ``worker_assign`` (worker key -> slot)
  and ``tail_panes`` (pane id -> worker key);
* running and already-assigned workers never move when another worker starts or
  finishes; a new worker fills the lowest free slot in an existing window before
  a new window is created; empty worker windows are retired;
* a killed worker window is recreated by the coordinator; a killed overview is
  rebuilt by ``up`` without moving surviving worker panes.

Isolation rules, same as ``tests/test_tmux_integration.py``:

* private tmux server (``-L hx_wl_<pid>_<rand> -f /dev/null``), never the default
  socket, never the user's session;
* private ``HERMES_HOME`` per sandbox under a TemporaryDirectory;
* assertions read real tmux output (list-windows / list-panes / pane titles) and
  the real ``layout.json``, never a claim made by the code under test.

The harness is copied, not imported: an imported module's ``tearDownModule``
does not run, which would leak a server and temp homes.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
_TMUX_PATH = shutil.which("tmux")
TMUX = _TMUX_PATH or "tmux"
PY = sys.executable or "python3"
SKIP = _TMUX_PATH is None

#: Private server, pid- and run-scoped: it can never name the user's server or
#: another concurrent run of this file.
SOCKET = "hx_wl_%d_%s" % (os.getpid(), os.urandom(2).hex())

#: Empty config on purpose: no user tmux.conf may change the outcome. All
#: addressing below is by pane/window id, never by index.
CONFIG = "/dev/null"


def _tmux(*args: str, timeout: float = 15.0) -> Tuple[int, str]:
    proc = subprocess.run([TMUX, "-L", SOCKET, "-f", CONFIG, *args],
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr).rstrip("\n")


_HOMES: List[Path] = []


def _socket_file(name: str) -> Path:
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return Path(base) / ("tmux-%d" % os.getuid()) / name


def _remove_socket_if_dead(name: str, wait: float = 3.0) -> None:
    path = _socket_file(name)
    if not path.exists():
        return
    deadline = time.time() + wait
    while True:
        rc = subprocess.run([TMUX, "-L", name, "ls"],
                            capture_output=True, timeout=10).returncode
        if rc != 0:
            break
        if time.time() >= deadline:
            return  # still answering: not ours to remove
        time.sleep(0.05)
    try:
        path.unlink()
    except OSError:
        pass


def tearDownModule() -> None:
    """Kill only this module's private server and remove its temp homes."""
    if TMUX:
        subprocess.run([TMUX, "-L", SOCKET, "kill-server"],
                       capture_output=True, text=True, timeout=15)
        _remove_socket_if_dead(SOCKET)
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


def _pane_exists(pane_id: str) -> bool:
    if not pane_id:
        return False
    return _tmux("display-message", "-p", "-t", pane_id, "#{pane_id}")[1].strip() == pane_id


def _pane_window(pane_id: str) -> str:
    return _tmux("display-message", "-p", "-t", pane_id, "#{window_id}")[1].strip()


_counter = [0]


class Sandbox:
    """One disposable tmux session, its private HERMES_HOME, and fixtures."""

    def __init__(self, columns: int = 160, rows: int = 45) -> None:
        _counter[0] += 1
        self.name = "hx_wl_%d_%d" % (os.getpid(), _counter[0])
        self.home = Path(tempfile.mkdtemp(prefix="hxwl_home_"))
        _HOMES.append(self.home)
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
    def run(self, command: str, timeout: float = 30.0) -> int:
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

    def cli(self, *args: str, timeout: float = 40.0) -> int:
        return self.run("%s -m hermes_tmux %s" % (PY, " ".join(args)), timeout=timeout)

    def capture(self, target: Optional[str] = None) -> str:
        return _tmux("capture-pane", "-p", "-t", target or self.pane_id)[1]

    # -- observation --------------------------------------------------------
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

    def worker_windows(self) -> List[dict]:
        return sorted((w for w in self.windows() if w["name"].startswith("hx:worker-")),
                      key=lambda w: w["name"])

    def pane_title(self, pane_id: str) -> str:
        return _tmux("display-message", "-p", "-t", pane_id, "#{pane_title}")[1].strip()

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
                try:
                    return candidate.read_text(encoding="utf-8")
                except OSError:
                    return ""
        return ""

    def snapshot(self) -> Dict[str, dict]:
        """worker key -> {pane, slot, window} as recorded in layout.json."""
        layout = self.layout()
        tail = layout.get("tail_panes") or {}
        assign = layout.get("worker_assign") or {}
        windows = layout.get("worker_windows") or {}
        out: Dict[str, dict] = {}
        for pane_id, wkey in tail.items():
            slot = assign.get(wkey)
            out[str(wkey)] = {"pane": pane_id, "slot": slot,
                              "window": windows.get(slot)}
        return out

    def wait_for_panes(self, count: int, timeout: float = 25.0) -> dict:
        ok = wait_for(lambda: len(self.layout().get("tail_panes") or {}) == count
                      and all(_pane_exists(p) for p in (self.layout().get("tail_panes") or {})),
                      timeout=timeout)
        if not ok:
            raise AssertionError("expected %d live worker panes, layout tail_panes=%r"
                                 % (count, self.layout().get("tail_panes")))
        return self.layout()

    # -- fixtures ----------------------------------------------------------
    def live(self) -> Path:
        root = self.home / "cache" / "delegation" / "live"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _atomic_json(self, path: Path, data: dict) -> None:
        """Write via rename: a reader must never see a half-written manifest.

        A truncated manifest reads as ``{}`` and would momentarily hide every
        worker of the delegation, which could retire live panes for an unrelated
        reason and make a stability assertion lie.
        """
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def add_delegation(self, deleg_id: str, tasks: List[dict],
                       model: str = "deepseek/deepseek-v4.1-flash-1017",
                       started: str = "2026-11-12 09:00:00") -> Path:
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

    def set_task_status(self, deleg_id: str, index: int, status: str) -> None:
        """Rewrite one task's manifest status, preserving everything else."""
        path = self.live() / deleg_id / "manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        found = False
        for task in data.get("tasks", []):
            if int(task.get("index", -1)) == index:
                task["status"] = status
                found = True
        if not found:
            raise AssertionError("no task %d in %s" % (index, deleg_id))
        self._atomic_json(path, data)

    def append_event(self, deleg_id: str, task: int, role: str, text: str) -> None:
        log = self.live() / deleg_id / ("task-%d.log" % task)
        stamp = time.strftime("%H:%M:%S")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("%s %-9s| %s\n" % (stamp, role, text))


@unittest.skipIf(SKIP, "tmux not installed")
class WorkerLayoutTest(unittest.TestCase):
    maxDiff = None

    # -- shared assertions -------------------------------------------------
    def _up(self, box: Sandbox) -> dict:
        rc = box.cli("up")
        self.assertEqual(rc, 0, "up exited %s; pane output: %r"
                         % (rc, box.capture()[-700:]))
        win = box.window("hx:workers")
        self.assertIsNotNone(win, "up did not create hx:workers")
        self.assertTrue(win["ours"], "hx:workers is not marked ours")
        return win

    def _assert_overview_alone(self, box: Sandbox, win: dict, context: str = "") -> None:
        panes = box.panes_of(win["id"])
        self.assertEqual(len(panes), 1,
                         "%s: hx:workers holds %d panes, expected 1: %r"
                         % (context, len(panes), panes))
        self.assertEqual(box.layout().get("overview_pane"), panes[0]["id"],
                         "%s: layout overview_pane does not name the pane in hx:workers"
                         % context)

    # -- (a) zero workers ---------------------------------------------------
    def test_zero_workers_single_overview_pane_and_no_worker_windows(self):
        with Sandbox() as box:
            win = self._up(box)
            self._assert_overview_alone(box, win, "n=0")
            self.assertEqual(box.worker_windows(), [],
                             "worker windows exist with zero workers: %r"
                             % [w["name"] for w in box.worker_windows()])
            layout = box.layout()
            self.assertEqual(layout.get("workers_window"), win["id"])
            for key in ("worker_windows", "worker_assign", "tail_panes"):
                self.assertFalse(layout.get(key),
                                 "%s should be empty with 0 workers, got %r"
                                 % (key, layout.get(key)))
            self.assertTrue(wait_for(lambda: "no delegations" in box.capture(win["id"]),
                                     timeout=10),
                            "overview pane does not paint the empty overview: %r"
                            % box.capture(win["id"])[:400])

    # -- (b) window/pane budget --------------------------------------------
    def test_worker_window_count_and_pane_budget(self):
        for n in (1, 2, 3, 4, 8):
            with self.subTest(workers=n):
                with Sandbox() as box:
                    deleg = "deleg_ln%02d0001" % n
                    box.add_delegation(deleg, [{"goal": "task %d" % i} for i in range(n)])
                    win = self._up(box)
                    layout = box.wait_for_panes(n)

                    expected_windows = int(math.ceil(n / 2.0))
                    wins = box.worker_windows()
                    self.assertEqual(
                        [w["name"] for w in wins],
                        ["hx:worker-%d" % i for i in range(1, expected_windows + 1)],
                        "n=%d: expected %d windows hx:worker-1..%d, got %r"
                        % (n, expected_windows, expected_windows,
                           [w["name"] for w in wins]))

                    counts = {}
                    for w in wins:
                        panes = box.panes_of(w["id"])
                        counts[w["name"]] = len(panes)
                        self.assertGreaterEqual(len(panes), 1,
                                                "n=%d: %s is an empty worker window"
                                                % (n, w["name"]))
                        self.assertLessEqual(len(panes), 2,
                                             "n=%d: %s holds %d panes (max 2)"
                                             % (n, w["name"], len(panes)))
                    self.assertEqual(sum(counts.values()), n,
                                     "n=%d: %d worker panes across windows, expected %d (%r)"
                                     % (n, sum(counts.values()), n, counts))
                    # Lowest-free-slot fill: every window but the last is full.
                    for name in list(counts)[:-1]:
                        self.assertEqual(counts[name], 2,
                                         "n=%d: %s has %d panes while a later window exists"
                                         % (n, name, counts[name]))
                    self._assert_overview_alone(box, win, "n=%d" % n)

                    slots = layout.get("worker_windows") or {}
                    self.assertEqual(sorted(int(s) for s in slots),
                                     list(range(1, expected_windows + 1)),
                                     "n=%d: worker_windows slots are %r"
                                     % (n, sorted(slots)))
                    self.assertEqual(sorted(slots.values()),
                                     sorted(w["id"] for w in wins),
                                     "n=%d: worker_windows does not name the real windows"
                                     % n)

                    tail = layout.get("tail_panes") or {}
                    assign = layout.get("worker_assign") or {}
                    self.assertEqual(len(tail), n, "n=%d: tail_panes=%r" % (n, tail))
                    self.assertEqual(set(tail.values()), set(assign),
                                     "n=%d: tail_panes and worker_assign disagree on workers"
                                     % n)
                    self.assertEqual(sorted(tail.values()),
                                     sorted("%s#%d" % (deleg, i) for i in range(n)),
                                     "n=%d: tail_panes worker keys are %r"
                                     % (n, sorted(tail.values())))
                    per_slot = {}
                    for pane_id, wkey in tail.items():
                        slot = assign[wkey]
                        per_slot[slot] = per_slot.get(slot, 0) + 1
                        self.assertEqual(_pane_window(pane_id), slots[slot],
                                         "n=%d: pane %s of %s is not in window of slot %s"
                                         % (n, pane_id, wkey, slot))
                        self.assertIn("ln%02d0001" % n, box.pane_title(pane_id),
                                      "n=%d: pane %s title lost worker identity: %r"
                                      % (n, pane_id, box.pane_title(pane_id)))
                    self.assertEqual(per_slot,
                                     {s: counts["hx:worker-%d" % int(s)] for s in slots},
                                     "n=%d: worker_assign does not match pane placement"
                                     % n)

    # -- (c) stability ------------------------------------------------------
    def test_completed_worker_never_moves_any_pane(self):
        with Sandbox() as box:
            deleg = "deleg_st000001"
            box.add_delegation(deleg, [{"goal": "w%d" % i} for i in range(4)])
            win = self._up(box)
            box.wait_for_panes(4)
            for i in range(4):
                box.append_event(deleg, i, "tool", "-> ping%d(x)" % i)
            self.assertTrue(
                wait_for(lambda: len(box.layout().get("tail_panes") or {}) == 4
                         and all(" run " in box.pane_title(p)
                                 for p in box.layout()["tail_panes"]), timeout=20),
                "workers were not labelled run before the transition: %r"
                % {p: box.pane_title(p) for p in (box.layout().get("tail_panes") or {})})

            before = box.snapshot()
            target_key = "%s#1" % deleg
            target_pane = before[target_key]["pane"]
            self.assertIn(" run ", box.pane_title(target_pane))

            box.set_task_status(deleg, 1, "completed")
            box.append_event(deleg, 1, "final", "status=completed duration=4.0s")
            self.assertTrue(
                wait_for(lambda: " done " in box.pane_title(target_pane), timeout=20),
                "completed worker %s still labelled %r"
                % (target_key, box.pane_title(target_pane)))

            after = box.snapshot()
            self.assertEqual(set(after), set(before),
                             "the visible worker set changed: %r -> %r"
                             % (sorted(before), sorted(after)))
            for key in sorted(before):
                self.assertEqual(after[key], before[key],
                                 "%s moved after another worker completed: %r -> %r"
                                 % (key, before[key], after[key]))
                self.assertTrue(_pane_exists(before[key]["pane"]),
                                "%s lost its pane %s" % (key, before[key]["pane"]))
            self.assertEqual(box.layout().get("worker_windows"),
                             {s: before[k]["window"] for k, s in
                              ((k, before[k]["slot"]) for k in before)},
                             "worker_windows changed while a worker completed")
            for key in before:
                if key == target_key:
                    continue
                self.assertIn(" run ", box.pane_title(before[key]["pane"]),
                              "%s was relabelled after an unrelated worker completed: %r"
                              % (key, box.pane_title(before[key]["pane"])))
            self._assert_overview_alone(box, win, "stability")

    # -- (d) fill a free slot ----------------------------------------------
    def test_new_delegation_fills_free_slot_before_new_window(self):
        with Sandbox() as box:
            first = "deleg_fillaaa1"
            second = "deleg_fillbbb2"
            box.add_delegation(first, [{"goal": "a%d" % i} for i in range(3)])
            win = self._up(box)
            box.wait_for_panes(3)

            before = box.snapshot()
            slots_before = dict(box.layout().get("worker_windows") or {})
            self.assertEqual(len(slots_before), 2,
                             "3 workers must occupy exactly 2 windows, got %r" % slots_before)
            counts_before = {slot: len(box.panes_of(wid))
                             for slot, wid in slots_before.items()}
            free_slot = min(counts_before, key=counts_before.get)
            self.assertEqual(counts_before[free_slot], 1,
                             "expected one half-full window, got %r" % counts_before)

            box.add_delegation(second, [{"goal": "new work"}],
                               started="2026-11-12 11:30:00")
            box.wait_for_panes(4)

            after = box.snapshot()
            slots_after = box.layout().get("worker_windows") or {}
            self.assertEqual(slots_after, slots_before,
                             "a new delegation created/renumbered a window: %r -> %r"
                             % (slots_before, slots_after))
            self.assertEqual(len(box.worker_windows()), 2,
                             "a new delegation opened a third window while a slot was free: %r"
                             % [w["name"] for w in box.worker_windows()])

            new_key = "%s#0" % second
            self.assertIn(new_key, after, "no pane was created for the new delegation")
            self.assertEqual(after[new_key]["slot"], free_slot,
                             "new worker landed in slot %r, expected free slot %r"
                             % (after[new_key]["slot"], free_slot))
            self.assertEqual(after[new_key]["window"], slots_before[free_slot],
                             "new worker landed outside the half-full window")
            self.assertEqual(len(box.panes_of(slots_before[free_slot])), 2,
                             "the free slot was not used; window now holds %r"
                             % box.panes_of(slots_before[free_slot]))
            for key, rec in before.items():
                self.assertEqual(after[key], rec,
                                 "existing worker %s moved when a new one started" % key)
            self._assert_overview_alone(box, win, "fill free slot")

    # -- (e) retention ------------------------------------------------------
    def test_completed_worker_keeps_its_pane_and_stays_tailed(self):
        with Sandbox() as box:
            deleg = "deleg_keep0001"
            box.add_delegation(deleg, [{"goal": "k0"}, {"goal": "k1"}])
            win = self._up(box)
            box.wait_for_panes(2)
            for i in range(2):
                box.append_event(deleg, i, "tool", "-> work%d(x)" % i)
            self.assertTrue(wait_for(
                lambda: all(" run " in box.pane_title(p)
                            for p in box.layout()["tail_panes"]), timeout=20))

            pane = box.snapshot()["%s#0" % deleg]["pane"]
            box.set_task_status(deleg, 0, "completed")
            box.append_event(deleg, 0, "final", "status=completed duration=2.0s")
            self.assertTrue(wait_for(lambda: " done " in box.pane_title(pane), timeout=20),
                            "completed worker title is %r" % box.pane_title(pane))

            layout = box.layout()
            self.assertEqual((layout.get("tail_panes") or {}).get(pane), "%s#0" % deleg,
                             "completed worker's pane was dropped from tail_panes")
            wins = box.worker_windows()
            self.assertEqual(len(wins), 1, "a window was retired while capacity remained: %r"
                             % [w["name"] for w in wins])
            self.assertEqual(len(box.panes_of(wins[0]["id"])), 2,
                             "completed worker's pane was closed while capacity remained: %r"
                             % box.panes_of(wins[0]["id"]))
            self.assertEqual(_pane_window(pane), wins[0]["id"],
                             "completed worker's pane changed window")

            marker = "LATE-EVENT-AFTER-COMPLETION"
            box.append_event(deleg, 0, "assistant", marker)
            self.assertTrue(wait_for(lambda: marker in box.capture(pane), timeout=15),
                            "completed worker's transcript is no longer tailed; pane shows %r"
                            % box.capture(pane)[-400:])
            self._assert_overview_alone(box, win, "retention")

    # -- (f1) worker window recovery ---------------------------------------
    def test_coordinator_recreates_a_killed_worker_window(self):
        with Sandbox() as box:
            deleg = "deleg_rec00001"
            box.add_delegation(deleg, [{"goal": "r0"}, {"goal": "r1"}])
            win = self._up(box)
            box.wait_for_panes(2)
            before = box.snapshot()
            old_windows = {rec["window"] for rec in before.values()}
            self.assertEqual(len(old_windows), 1, "fixture expected one worker window: %r"
                             % sorted(old_windows))
            killed = old_windows.pop()

            rc, out = _tmux("kill-window", "-t", killed)
            self.assertEqual(rc, 0, "could not kill %s: %s" % (killed, out))
            self.assertFalse(_pane_exists(before["%s#0" % deleg]["pane"]),
                             "kill-window left the worker pane alive")

            # No vnode event is raised by a kill; a transcript append must be
            # what drives the coordinator's next pass.
            box.append_event(deleg, 0, "tool", "-> afterkill(x)")
            old_panes = {rec["pane"] for rec in before.values()}
            started = time.time()
            ok = wait_for(lambda: (
                len(box.layout().get("tail_panes") or {}) == 2
                and all(_pane_exists(p) for p in box.layout()["tail_panes"])
                and not (set(box.layout()["tail_panes"]) & old_panes)
            ), timeout=15)
            elapsed = time.time() - started
            self.assertTrue(ok, "coordinator did not recreate the worker window within 15s "
                                "(%.1fs); tail_panes=%r, windows=%r"
                                % (elapsed, box.layout().get("tail_panes"),
                                   [(w["name"], w["id"]) for w in box.worker_windows()]))

            after = box.snapshot()
            self.assertEqual(set(after), set(before),
                             "worker keys changed during recovery: %r -> %r"
                             % (sorted(before), sorted(after)))
            for key, rec in after.items():
                self.assertTrue(_pane_exists(rec["pane"]),
                                "%s has no live pane after recovery" % key)
                self.assertNotEqual(rec["pane"], before[key]["pane"],
                                    "%s kept its dead pane id" % key)
                self.assertNotEqual(rec["window"], killed,
                                    "%s was recorded in the killed window" % key)
                self.assertIn(rec["window"],
                              {w["id"] for w in box.worker_windows()},
                              "%s was recorded in a window that is not hx:worker-*: %r"
                              % (key, rec))
            self.assertNotIn(killed, (box.layout().get("worker_windows") or {}).values(),
                             "the killed window id is still in worker_windows")
            for w in box.worker_windows():
                self.assertLessEqual(len(box.panes_of(w["id"])), 2,
                                     "%s holds %d panes after recovery"
                                     % (w["name"], len(box.panes_of(w["id"]))))
            self._assert_overview_alone(box, win, "window recovery")

    # -- (f2) overview recovery --------------------------------------------
    def test_up_rebuilds_the_overview_without_moving_worker_panes(self):
        with Sandbox() as box:
            deleg = "deleg_rec00002"
            box.add_delegation(deleg, [{"goal": "o0"}, {"goal": "o1"}])
            self._up(box)
            box.wait_for_panes(2)
            before = box.snapshot()
            old_overview = box.layout().get("overview_pane")
            self.assertTrue(_pane_exists(old_overview))

            rc, out = _tmux("kill-pane", "-t", old_overview)
            self.assertEqual(rc, 0, "could not kill overview pane: %s" % out)
            time.sleep(0.3)
            self.assertFalse(_pane_exists(old_overview))

            rc = box.cli("up")
            self.assertEqual(rc, 0, "up failed to rebuild the overview (rc=%s): %r"
                             % (rc, box.capture()[-700:]))
            layout = box.layout()
            new_overview = layout.get("overview_pane")
            self.assertTrue(new_overview)
            self.assertNotEqual(new_overview, old_overview,
                                "layout still names the killed overview pane")
            self.assertTrue(_pane_exists(new_overview),
                            "rebuilt overview pane %r does not exist" % new_overview)
            win = box.window("hx:workers")
            self.assertIsNotNone(win, "hx:workers is missing after overview recovery")
            self._assert_overview_alone(box, win, "overview recovery")
            self.assertIn(new_overview, [p["id"] for p in box.panes_of(win["id"])])
            self.assertTrue(
                wait_for(lambda: "hermes subagents" in box.capture(new_overview),
                         timeout=15),
                "rebuilt overview pane paints no overview: %r"
                % box.capture(new_overview)[:400])

            after = box.snapshot()
            self.assertEqual(set(after), set(before),
                             "worker set changed across overview recovery: %r -> %r"
                             % (sorted(before), sorted(after)))
            for key, rec in before.items():
                self.assertEqual(after[key], rec,
                                 "surviving worker %s moved during overview recovery: %r -> %r"
                                 % (key, rec, after[key]))
                self.assertTrue(_pane_exists(rec["pane"]),
                                "surviving worker pane %s was closed by up" % rec["pane"])

    # -- (g) concurrent events ---------------------------------------------
    def test_concurrent_transcript_events_reach_the_overview(self):
        with Sandbox() as box:
            deleg = "deleg_conc0001"
            box.add_delegation(deleg, [{"goal": "c%d" % i} for i in range(4)])
            win = self._up(box)
            box.wait_for_panes(4)
            self.assertTrue(wait_for(lambda: "c0" in box.render_text(), timeout=15),
                            "overview never rendered the fixture workers")

            tokens = ["evtA111", "evtB222", "evtC333", "evtD444"]
            for i, token in enumerate(tokens):
                box.append_event(deleg, i, "tool", "-> %s(x)" % token)

            ok = wait_for(
                lambda: all(t in box.render_text() for t in tokens), timeout=10)
            missing = [t for t in tokens if t not in box.render_text()]
            self.assertTrue(ok, "overview render.txt missed concurrent events after 10s: %r "
                                "(missing %r)" % (box.render_text()[-600:], missing))

            overview = box.layout().get("overview_pane")
            self.assertTrue(
                wait_for(lambda: all(t in box.capture(overview) for t in tokens),
                         timeout=10),
                "overview pane did not paint every concurrent event: %r"
                % box.capture(overview)[-600:])
            self._assert_overview_alone(box, win, "concurrent events")


if __name__ == "__main__":
    unittest.main(verbosity=2)
