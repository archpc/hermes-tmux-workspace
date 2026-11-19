"""Integration tests for the v3 tab contract (one worker, one tab).

Contract under test, as implemented by ``hermes_tmux/watch.py:reconcile_tabs``
and the CLI:

* ``up`` owns exactly one non-worker window, ``agents``, holding one pane;
* every running worker gets one window with one pane, named from its goal,
  marked ``@hx`` on both window and pane, running the styled follower (or
  ``tail`` when ``HERMES_TMUX_TRANSCRIPT=raw``);
* finished work does not get a tab; work that was running when ``up`` ran does;
* a tab is never renamed or moved: finishing changes only its glyph/header;
* a finished tab lingers (``HERMES_TMUX_LINGER_S``), is never retired while it
  is the current window of an attached client, and a failure keeps its tab
  until somebody looks at it;
* a tab the reader closed stays closed until ``hermes-tmux open`` re-creates it
  pinned; a pinned tab is not retired;
* only panes proven to be ours are closed: a user split inside a worker window
  survives retirement and ``down``, and neither a foreign window nor a foreign
  pane is ever adopted, renamed, or closed;
* ``down`` closes everything we own, stops the coordinator, removes the session
  state directory, and leaves the reader's own pane untouched and out of
  copy-mode.

Isolation (copied, not imported, from tests/test_worker_layouts.py):

* a private tmux server (``-L hx_tab_<pid>_<rand> -f /dev/null``), never the
  default socket and never the user's sessions;
* a private ``HERMES_HOME`` per sandbox under ``tempfile.mkdtemp``;
* every assertion reads real tmux state (list-windows / list-panes /
  show-options) and the real ``layout.json``.

The harness is copied rather than imported because an imported module's
``tearDownModule`` does not run, which would leak a server and temp homes.

Timing, honestly: most waits are ``wait_for`` polls against real tmux and a live
coordinator, and their timeouts (12 s default, 20-40 s where a pass must run)
are the only slack. One case is a known load-sensitive race and is not papered
over: ``cmd_down`` SIGTERMs the coordinator and polls ``_watcher_alive`` (pid
file, lock holder, cmdline) for 2.0 s; ``_lock_held`` is a separate check that
decides the exit code. On a loaded machine the coordinator can still be exiting
when that window closes, so ``test_down_...`` can fail on timing alone. It
passes on CI; nothing here retries it.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from hermes_tmux import labels, watch

REPO = Path(__file__).resolve().parent.parent
_TMUX_PATH = shutil.which("tmux")
TMUX = _TMUX_PATH or "tmux"
PY = sys.executable or "python3"
SKIP = _TMUX_PATH is None

#: Private server, pid- and run-scoped: it can never name the user's server or
#: another concurrent run of this file.
SOCKET = "hx_tab_%d_%s" % (os.getpid(), os.urandom(2).hex())
CONFIG = "/dev/null"

MAX_TABS = watch.MAX_TABS


def _tmux(*args: str, timeout: float = 15.0) -> Tuple[int, str]:
    """Run one tmux command on this module's private server.

    ``stdout + stderr`` is merged because tmux reports refusals on stderr while the
    tests want one text blob plus a status; a caller that needs a value (a pane id)
    takes the last line of it.
    """
    proc = subprocess.run([TMUX, "-L", SOCKET, "-f", CONFIG, *args],
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr).rstrip("\n")


_HOMES: List[Path] = []


def _socket_file(name: str) -> Path:
    """Where tmux keeps this server's socket, computed instead of asked.

    Asking tmux after the server is gone is not a trustworthy path: on tmux 3.8
    it answers "no server running on <socket path>" and exits 1. Computing the
    path is why a teardown that used to probe for it left one socket file behind
    per run.
    """
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return Path(base) / ("tmux-%d" % os.getuid()) / name


def _remove_socket_if_dead(name: str, wait: float = 3.0) -> None:
    """Unlink the socket file only after the server stops answering.

    ``kill-server`` returns before the server has exited, so a probe run straight
    afterwards still succeeds and would skip the unlink. If the name keeps
    answering past ``wait`` it is not ours to remove and the file is left alone.
    """
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
    """Poll ``predicate`` until it is true or ``timeout`` expires.

    Exceptions count as "not yet": most predicates here read tmux or a file that
    can vanish between two calls, and a read that fails once is not a verdict.
    The 12 s default covers a single tmux read; callers that wait on a coordinator
    pass pass 20-40 s, and some retry the whole condition, so a missed wake-up
    costs time rather than the test.
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


# --------------------------------------------------------------------------- tmux readers

def _format(target: str, fmt: str) -> str:
    """One ``display-message -p`` expansion, stripped; '' when no answer.

    '' does not mean "gone": tmux exits 0 and prints nothing for a missing target,
    so existence is decided by comparing the resolved id (``_pane_exists``).
    """
    return _tmux("display-message", "-p", "-t", target, fmt)[1].strip()


def _pane_exists(pane_id: str) -> bool:
    """True only when ``pane_id`` still resolves to itself.

    The resolved id is the authority, not the exit status, because tmux answers 0
    for an id that no longer names a pane.
    """
    if not pane_id:
        return False
    return _format(pane_id, "#{pane_id}") == pane_id


def _window_exists(window_id: str) -> bool:
    """True only when ``window_id`` still resolves to itself (see ``_pane_exists``)."""
    if not window_id:
        return False
    return _format(window_id, "#{window_id}") == window_id


def _pane_window(pane_id: str) -> str:
    """Window id this pane currently lives in.

    Not assumed to be the tab's recorded window: a ``join-pane`` by the reader
    moves the pane and is exactly the case retirement has to notice.
    """
    return _format(pane_id, "#{window_id}")


def _pane_command(pane_id: str) -> str:
    """``pane_current_command``: the process tmux sees in the foreground.

    Used to tell the styled follower (``python``) from raw ``tail`` and from a
    reader's shell, so it is read from tmux rather than from the layout.
    """
    return _format(pane_id, "#{pane_current_command}")


def _window_name(window_id: str) -> str:
    """The window name as drawn.

    Renaming is forbidden by the contract, so a change here is a defect, not a
    refresh.
    """
    return _format(window_id, "#{window_name}")


def _window_index(window_id: str) -> int:
    """Window index as an int, -1 when tmux has no number for it.

    -1 keeps a missing index sorting before every real tab instead of looking like
    tab number zero.
    """
    text = _format(window_id, "#{window_index}")
    try:
        return int(text)
    except ValueError:
        return -1


def _option(scope: str, target: str, option: str) -> str:
    """The value of ``option`` set ON ``target`` itself (not inherited).

    ``show-options -p`` reports the pane's own value; a format read would fall
    back through pane -> window -> session and call every pane in a marked
    window "ours".
    """
    rc, out = _tmux("show-options", scope, "-t", target, option)
    if rc != 0:
        return ""
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] == option:
            return parts[1].strip()
    return ""


_counter = [0]


class Sandbox:
    """One disposable tmux session, its private HERMES_HOME, and fixtures."""

    def __init__(self, columns: int = 160, rows: int = 45) -> None:
        """Allocate a name and a home, but touch neither yet.

        The session name is unique per process on the private server; the home is a
        fresh ``mkdtemp`` registered for module teardown so a sandbox built without
        its context manager still loses its temp home.
        """
        _counter[0] += 1
        self.name = "hx_tab_%d_%d" % (os.getpid(), _counter[0])
        self.home = Path(tempfile.mkdtemp(prefix="hxtabs_home_"))
        _HOMES.append(self.home)
        self.columns, self.rows = columns, rows
        self.created = False
        self.marker_seq = 0

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "Sandbox":
        """Create the session and export the three variables every command needs.

        ``zsh -f`` skips rc files so the reader's shell setup cannot change what the
        tests observe. The exports happen INSIDE the pane: ``box.cli`` types into that
        shell, so it inherits HERMES_HOME, the gate and PYTHONPATH from there. A tmux
        refusal (session limit, no server) is a skip, not a failure of the code.
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
        """Kill the session by name and remove the home.

        The server itself belongs to ``tearDownModule``; leaving it up keeps the next
        test in this module from paying for a new server.
        """
        if self.created:
            _tmux("kill-session", "-t", self.name)
        shutil.rmtree(self.home, ignore_errors=True)
        return False

    # -- driving the pane ---------------------------------------------------
    def run(self, command: str, timeout: float = 40.0) -> int:
        """Type a command into the sandbox's own pane and return its exit status.

        The status travels through a file because pane text is a rendering, not a
        record: scrollback wraps, styles intervene, and the echoed command can be
        mistaken for its output. ``-l`` sends the line literally. A marker that never
        appears means the command hung (assertion failure); an unreadable marker
        answers -1 rather than a success-shaped 0.
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
        """``export NAME=VALUE`` in the pane shell; raises when zsh refused it."""
        rc = self.run("export %s=%s" % (name, value))
        if rc != 0:
            raise AssertionError("could not export %s" % name)

    def cli(self, *args: str, timeout: float = 40.0) -> int:
        """Run ``python -m hermes_tmux ...`` in the pane, inheriting its exports."""
        return self.run("%s -m hermes_tmux %s" % (PY, " ".join(args)), timeout=timeout)

    def capture(self, target: Optional[str] = None) -> str:
        """A pane's screen plus scrollback, as text (a rendering, not evidence)."""
        return _tmux("capture-pane", "-p", "-t", target or self.pane_id)[1]

    # -- observation --------------------------------------------------------
    def windows(self) -> List[dict]:
        """Every window of the session with its own ``@hx`` marker.

        ``ours`` is read with ``show-options -w`` — the window's OWN option.
        ``#{@hx}`` would fall back to the session and call a foreign window ours.
        """
        rc, out = _tmux("list-windows", "-t", self.name,
                        "-F", "#{window_id}\t#{window_name}\t#{window_index}")
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                rows.append({"id": parts[0], "name": parts[1],
                             "index": parts[2],
                             "ours": _option("-w", parts[0], "@hx") == "1"})
        return rows

    def window(self, name: str) -> Optional[dict]:
        """First window with this exact name, or None.

        Names are not addresses; this exists for the static ``agents``/``hx:*`` windows
        whose names the product fixes.
        """
        for win in self.windows():
            if win["name"] == name:
                return win
        return None

    def panes_of(self, window_id: str) -> List[dict]:
        """Panes of a window as ``{"id", "command"}`` dicts from tmux."""
        rc, out = _tmux("list-panes", "-t", window_id,
                        "-F", "#{pane_id}\t#{pane_current_command}")
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                rows.append({"id": parts[0], "command": parts[1]})
        return rows

    def layout(self) -> dict:
        """The session's ``layout.json``, or {} before any state exists.

        One workspace per sandbox, so the single state directory cannot be ambiguous
        here; ``test_concurrency`` reads by key because there it can be.
        """
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

    def tab_dir(self) -> Optional[Path]:
        """The state directory that holds layout.json, or None before ``up``."""
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

    def render_text(self) -> str:
        """The last overview text the coordinator published, or ''."""
        child = self.tab_dir()
        if child is None:
            return ""
        try:
            return (child / "render.txt").read_text(encoding="utf-8")
        except OSError:
            return ""

    def tabs(self) -> Dict[str, dict]:
        """Tab records keyed ``<delegation_id>#<task_index>``."""
        return dict((self.layout().get("tabs") or {}))

    def dismissed(self) -> Dict[str, float]:
        """Keys of tabs the reader closed or that retired, with the time it happened."""
        return dict((self.layout().get("dismissed") or {}))

    # -- fixtures -----------------------------------------------------------
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
        """Write a manifest plus one ``task-N.log``, as the real writer does.

        ``status`` defaults to running and ``started`` to today because a task that
        finished before ``up`` is history and gets no tab — a fixture that wants a tab
        must look live. A log with no event line still classifies as run/stall (both
        count as live work), which is why tab tests need no appended event.
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
        """Rewrite one task's status atomically; raises for an unknown index.

        Without the raise, a mistyped fixture index would let a test pass while
        asserting nothing.
        """
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
        """Append one parseable line in the writer's event format.

        This is a write to a child of a watched directory, i.e. exactly the event the
        coordinator's kqueue reports, so it is also how a test forces a pass.
        """
        log = self.live() / deleg_id / ("task-%d.log" % task)
        stamp = time.strftime("%H:%M:%S")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("%s %-9s| %s\n" % (stamp, role, text))

    def finish(self, deleg_id: str, index: int, status: str = "completed") -> None:
        """Record a terminal state in both places a reader can learn it from.

        Manifest status and the final event line each suffice for classification;
        writing both mirrors the real writer, so a test using this is not accidentally
        testing only one of the two paths.
        """
        self.set_task_status(deleg_id, index, status)
        self.append_event(deleg_id, index, "final", "status=%s duration=4.0s" % status)

    # -- the coordinator ----------------------------------------------------
    def watcher_pid(self) -> Optional[int]:
        """The coordinator pid from ``watch.pid``, or None. Liveness is not implied."""
        child = self.tab_dir()
        if child is None:
            return None
        try:
            return int((child / "watch.pid").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def stop_coordinator(self) -> int:
        """SIGTERM the live coordinator so a test can drive the decision itself.

        Returns only after both signals agree it is gone: the pid file is
        removed by the coordinator's own exit path, while the ``kill(pid, 0)``
        check catches a process that unlinked it and is still running. A
        coordinator that will not stop fails here instead of being reconciled
        beside in-process.
        """
        pid = self.watcher_pid()
        if pid is None:
            raise AssertionError("no coordinator pid was written")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        child = self.tab_dir()
        ok = child is not None and wait_for(
            lambda: not (child / "watch.pid").exists()
            and not _pid_alive(pid), timeout=10)
        if not ok:
            raise AssertionError("coordinator %d did not stop" % pid)
        return pid


def _pid_alive(pid: int) -> bool:
    """``kill(pid, 0)`` probe.

    Any OSError answers dead. EPERM cannot happen: the coordinator runs as the
    same user as the tests — it is a tmux ``run-shell`` child, not a child of
    this process.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextlib.contextmanager
def inproc(box: Sandbox, linger: str = "1"):
    """Point this process at the sandbox's private server for ``reconcile_tabs``.

    ``tmuxio`` resolves the socket from ``TMUX``/``TMUX_PANE`` on every call —
    there is no cached connection — so setting them is enough for an in-process
    pass to act on the sandbox. The whole environment is saved and restored by
    clear+update, because a partial restore would leak HERMES_HOME into the next
    test. ``linger`` is passed through so a test can pin the retirement clock.
    """
    info = _tmux("display-message", "-p", "#{socket_path}|#{pid}")[1]
    sock, server = info.split("|")
    saved = os.environ.copy()
    os.environ.update(TMUX="%s,%s,0" % (sock, server), TMUX_PANE=box.pane_id,
                      HERMES_HOME=str(box.home), HERMES_TMUX="1",
                      HERMES_TMUX_LINGER_S=linger, PYTHONPATH=str(REPO))
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@unittest.skipIf(SKIP, "tmux not installed")
class TabsTest(unittest.TestCase):
    """One Sandbox per test; nothing is shared between tests but the private server."""
    maxDiff = None

    # -- shared helpers -----------------------------------------------------
    def _up(self, box: Sandbox) -> dict:
        """``up`` plus the ``agents`` invariant: exit 0, one window, marked ours.

        The failure message carries the pane tail because "up exited 1" alone says
        nothing about which step refused.
        """
        rc = box.cli("up")
        self.assertEqual(rc, 0, "up exited %s; pane output: %r"
                         % (rc, box.capture()[-700:]))
        win = box.window("agents")
        self.assertIsNotNone(win, "up did not create the 'agents' window: %r"
                             % [w["name"] for w in box.windows()])
        self.assertTrue(win["ours"], "'agents' is not marked ours")
        return win

    def _wait_tabs(self, box: Sandbox, count: int, timeout: float = 20.0) -> dict:
        """Wait for ``count`` tabs whose recorded panes all exist.

        Both writers publish a tab record only after ``tmuxio.wait_for_panes``
        returned a pane (``watch.py``'s reconcile, ``cli.py`` open), so a record
        cannot name a pane tmux has not reported yet. The extra
        ``_pane_exists`` poll is harmless, not a race guard.
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

    def _assert_one_pane_per_tab(self, box: Sandbox, layout: dict) -> None:
        """The v3 shape: one pane in the window, and it is the pane the tab records."""
        for wkey, tab in (layout.get("tabs") or {}).items():
            panes = box.panes_of(tab["window"])
            self.assertEqual(len(panes), 1,
                             "%s: window %s holds %d panes, expected exactly 1: %r"
                             % (wkey, tab["window"], len(panes), panes))
            self.assertEqual(panes[0]["id"], tab["pane"],
                             "%s: the tab's recorded pane is not the window's pane" % wkey)

    # -- 1 ----------------------------------------------------------------
    def test_up_with_no_workers_owns_only_the_agents_window(self):
        """With no live work, ``up`` owns the static windows and nothing else.

        An empty ``tabs`` is the assertion that matters: a tab here would mean ``up``
        invented work. The static set is named explicitly because a new permanent
        window is a visible change to the reader's tab bar, not an internal detail.
        """
        with Sandbox() as box:
            win = self._up(box)
            ours = [w for w in box.windows() if w["ours"]]
            self.assertEqual([w["name"] for w in ours], ["agents", "hx:mail", "hx:testbench"],
                             "up owns unexpected static windows: %r"
                             % [w["name"] for w in ours])
            panes = box.panes_of(win["id"])
            self.assertEqual(len(panes), 1,
                             "'agents' holds %d panes, expected 1: %r"
                             % (len(panes), panes))
            layout = box.layout()
            self.assertEqual(layout.get("workers_window"), win["id"])
            self.assertEqual(layout.get("overview_pane"), panes[0]["id"],
                             "layout overview_pane does not name the pane in 'agents'")
            self.assertEqual(layout.get("tabs") or {}, {},
                             "tabs exist with zero workers: %r" % (layout.get("tabs"),))

    # -- 2 ----------------------------------------------------------------
    def test_running_workers_get_one_tab_each_with_the_following_pane(self):
        """One window per running worker, named from its goal, running the follower.

        Keys are ``<delegation>#<task>`` so a tab cannot be attributed to the wrong
        task, and the name is recomputed with ``labels.tab_label(goal, siblings)``
        instead of compared to a literal, because sibling de-duplication is part of
        the contract. ``@hx`` is read on window and pane separately: a window option
        does not make its panes ours. The ``raw`` subtest is the negative control for
        ``tail_command`` — a pane that is ``python`` in both modes fails there.
        """
        names = ["alpha", "bravo", "charlie", "delta",
                 "echo", "foxtrot", "golf", "hotel"]
        for n in (1, 2, 3, 4, 8):
            with self.subTest(workers=n, transcript="styled"):
                with Sandbox() as box:
                    deleg = "deleg_tab%02d0001" % n
                    goals = ["audit %s" % names[i] for i in range(n)]
                    box.add_delegation(deleg, [{"goal": g} for g in goals])
                    self._up(box)
                    layout = self._wait_tabs(box, n)
                    tabs = layout["tabs"]
                    self.assertEqual(set(tabs),
                                     {"%s#%d" % (deleg, i) for i in range(n)},
                                     "n=%d: tab keys are %r" % (n, sorted(tabs)))
                    self.assertEqual(len({t["window"] for t in tabs.values()}), n,
                                     "n=%d: two workers share a window: %r"
                                     % (n, [t["window"] for t in tabs.values()]))
                    windows = {w["id"]: w for w in box.windows()}
                    for i, goal in enumerate(goals):
                        wkey = "%s#%d" % (deleg, i)
                        tab = tabs[wkey]
                        win = windows.get(tab["window"])
                        self.assertIsNotNone(win, "%s: window %s vanished"
                                             % (wkey, tab["window"]))
                        siblings = [g for j, g in enumerate(goals) if j != i]
                        self.assertEqual(win["name"],
                                         labels.tab_label(goal, siblings),
                                         "%s: window name is %r, expected the tab "
                                         "label of its goal" % (wkey, win["name"]))
                        self.assertTrue(win["ours"], "%s: window is not marked @hx" % wkey)
                        self.assertEqual(_option("-w", tab["window"], "@hx"), "1")
                        self.assertEqual(_option("-p", tab["pane"], "@hx"), "1",
                                         "%s: pane %s has no @hx of its own"
                                         % (wkey, tab["pane"]))
                        self.assertTrue(
                            wait_for(lambda p=tab["pane"]:
                                     _pane_command(p).lower().startswith("python"),
                                     timeout=8),
                            "%s: pane runs %r, expected the styled follower (python)"
                            % (wkey, _pane_command(tab["pane"])))
                    self._assert_one_pane_per_tab(box, layout)

            with self.subTest(workers=n, transcript="raw"):
                with Sandbox() as box:
                    box.export("HERMES_TMUX_TRANSCRIPT", "raw")
                    deleg = "deleg_raw%02d0001" % n
                    box.add_delegation(deleg, [{"goal": "audit %s" % names[i]}
                                               for i in range(n)])
                    self._up(box)
                    layout = self._wait_tabs(box, n)
                    for wkey, tab in layout["tabs"].items():
                        self.assertTrue(
                            wait_for(lambda p=tab["pane"]:
                                     _pane_command(p).lower() == "tail", timeout=8),
                            "%s: pane runs %r with HERMES_TMUX_TRANSCRIPT=raw, "
                            "expected tail" % (wkey, _pane_command(tab["pane"])))

    # -- 3 ----------------------------------------------------------------
    def test_nine_running_workers_fill_the_cap_with_running_tabs_only(self):
        """The cap admits no ninth live tab and evicts no live tab to make room.

        Nine workers run, eight tabs exist: exactly one running key is missing, and
        every open tab belongs to a running worker. Asserting "one missing" rather
        than "the ninth is missing" keeps the test about the cap, not arrival order.
        """
        with Sandbox() as box:
            deleg = "deleg_cap00001"
            goals = ["audit w%d" % i for i in range(9)]
            box.add_delegation(deleg, [{"goal": g} for g in goals])
            self._up(box)
            ok = wait_for(lambda: len(box.tabs()) >= MAX_TABS, timeout=25)
            self.assertTrue(ok, "only %d tabs opened for 9 running workers: %r"
                            % (len(box.tabs()), sorted(box.tabs())))
            layout = box.layout()
            tabs = layout["tabs"]
            self.assertLessEqual(len(tabs), MAX_TABS,
                                 "%d tabs open, MAX_TABS is %d" % (len(tabs), MAX_TABS))
            self.assertEqual(len(tabs), MAX_TABS,
                             "expected the cap to be reached, got %d" % len(tabs))
            running = {"%s#%d" % (deleg, i) for i in range(9)}
            self.assertTrue(set(tabs) <= running,
                             "tabs exist for workers that are not running: %r"
                             % sorted(set(tabs) - running))
            missing = running - set(tabs)
            self.assertEqual(len(missing), 1,
                             "open tabs do not cover the running set: missing %r"
                             % sorted(missing))
            self._assert_one_pane_per_tab(box, layout)

    # -- 4 ----------------------------------------------------------------
    def test_work_finished_before_up_gets_no_tab_but_running_work_does(self):
        """History gets no tab; work still running when ``up`` ran does.

        Both tasks share one manifest, one directory and one mtime, so only per-task
        state and start time can separate them — the fixture shape is the control.
        """
        with Sandbox() as box:
            deleg = "deleg_hist0001"
            past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 09:00:00")
            box.add_delegation(deleg, [
                {"goal": "finished before up", "status": "completed",
                 "lines": ["09:00:00 start     | task 0",
                           "09:04:00 tool      | -> read(x)",
                           "09:05:00 final     | status=completed duration=300s"]},
                {"goal": "still running", "status": "running",
                 "lines": ["09:00:00 start     | task 1",
                           "09:06:00 tool      | -> write(y)"]},
            ], started=past)
            self._up(box)
            layout = self._wait_tabs(box, 1)
            self.assertEqual(set(layout["tabs"]), {"%s#1" % deleg},
                             "tabs are %r; the historical worker must not get one "
                             "and the running one must" % sorted(layout["tabs"]))
            self.assertNotIn("%s#0" % deleg, layout["tabs"])

    # -- 5 ----------------------------------------------------------------
    def test_finished_tab_keeps_its_window_and_pane_then_lingers_away(self):
        """Finishing changes labels only, then the tab retires after the linger.

        The done glyph is asserted absent before the transition; otherwise "the done
        glyph is present" would already be true and prove nothing. After retirement a
        fresh event is appended, so a reappearing tab would be a coordinator
        recreation rather than a stale assertion.
        """
        with Sandbox() as box:
            box.export("HERMES_TMUX_LINGER_S", "1")
            deleg = "deleg_ling0001"
            box.add_delegation(deleg, [{"goal": "finish me later"}])
            self._up(box)
            layout = self._wait_tabs(box, 1)
            wkey = "%s#0" % deleg
            tab = layout["tabs"][wkey]
            window, pane = tab["window"], tab["pane"]
            name_before = _window_name(window)
            glyph_before = _option("-w", window, "@hx_glyph")
            ascii_mode = bool(layout.get("ascii"))
            done_glyph = labels.glyph("done", ascii_mode)

            box.finish(deleg, 0)
            ok = wait_for(
                lambda: done_glyph in (_option("-w", window, "@hx_glyph") or "")
                and "settled" in ((box.tabs().get(wkey)) or {}), timeout=20)
            self.assertTrue(ok, "tab %s never showed the done glyph: glyph=%r tabs=%r"
                            % (wkey, _option("-w", window, "@hx_glyph"), box.tabs()))
            self.assertNotIn(done_glyph, glyph_before,
                             "the running tab already carried the done glyph")

            layout = box.layout()
            tab = layout["tabs"][wkey]
            self.assertEqual(tab["window"], window,
                             "the tab moved to window %s on finishing" % tab["window"])
            self.assertEqual(tab["pane"], pane,
                             "the tab moved to pane %s on finishing" % tab["pane"])
            self.assertEqual(_window_name(window), name_before,
                             "the tab was renamed on finishing: %r -> %r"
                             % (name_before, _window_name(window)))
            self.assertTrue(_pane_exists(pane), "the tab's pane was closed on finishing")

            ok = wait_for(lambda: wkey not in box.tabs() and wkey in box.dismissed(),
                          timeout=25)
            self.assertTrue(ok, "the tab never retired after the linger: tabs=%r "
                                "dismissed=%r" % (box.tabs(), box.dismissed()))
            self.assertFalse(_pane_exists(pane), "the retired tab's pane still exists")

            # Forcing a coordinator pass must not bring it back.
            box.append_event(deleg, 0, "assistant", "LATE-AFTER-RETIRE")
            self.assertTrue(wait_for(lambda: "LATE-AFTER-RETIRE" in box.render_text(),
                                     timeout=15),
                            "the coordinator never ran a pass after the retirement")
            self.assertNotIn(wkey, box.tabs(),
                             "the retired tab was recreated: %r" % box.tabs())

    # -- 6 ----------------------------------------------------------------
    def test_tab_in_view_is_not_retired_and_is_marked_seen(self):
        """A tab on screen is acknowledged and never retired, even long past the linger.

        ``_active_window`` is replaced rather than attaching a client: attaching would
        resize the session and this test is about the retirement decision, not about
        clients. Both clocks are passed in, so "60 s later" costs nothing and the
        linger is tested rather than waited out. The final call is the control: the
        same settled tab with nothing in view does retire.
        """
        with Sandbox() as box:
            box.export("HERMES_TMUX_LINGER_S", "1")
            deleg = "deleg_view0001"
            box.add_delegation(deleg, [{"goal": "watch me"}])
            self._up(box)
            layout = self._wait_tabs(box, 1)
            wkey = "%s#0" % deleg
            window, pane = layout["tabs"][wkey]["window"], layout["tabs"][wkey]["pane"]
            key = box.key()
            box.stop_coordinator()
            box.finish(deleg, 0)

            with inproc(box, linger="1"):
                from hermes_tmux import state, watch
                home = Path(box.home)
                workers = state.load_workers(home)
                self.assertTrue([w for w in workers if w.key == wkey],
                                "fixture worker is not readable: %r"
                                % [w.key for w in workers])
                original = watch._active_window
                watch._active_window = lambda session_id: window
                try:
                    t0 = time.time()
                    watch.reconcile_tabs(key, workers, home, now=t0)
                    tab = (watch.read_layout(key, home).get("tabs") or {}).get(wkey) or {}
                    self.assertIn("settled", tab,
                                  "a finished tab was not marked settled: %r" % tab)
                    self.assertIn("seen", tab,
                                  "the current window's tab was not marked seen: %r" % tab)
                    self.assertTrue(_pane_exists(pane))

                    later = t0 + 60.0  # far past the 1 s linger
                    watch.reconcile_tabs(key, workers, home, now=later)
                    tab = (watch.read_layout(key, home).get("tabs") or {}).get(wkey) or {}
                    self.assertTrue(tab,
                                    "the tab was retired while it was the session's "
                                    "current window (layout=%r)"
                                    % watch.read_layout(key, home))
                    self.assertTrue(_pane_exists(pane),
                                    "the current window's pane was closed past the linger")
                    self.assertGreaterEqual(tab.get("seen", 0.0), later,
                                            "looking at the tab did not refresh its "
                                            "linger clock: %r" % tab)
                finally:
                    watch._active_window = original

                # Control: out of view, the same settled tab does retire.
                watch.reconcile_tabs(key, workers, home, now=t0 + 120.0)
                self.assertNotIn(wkey, (watch.read_layout(key, home).get("tabs") or {}),
                                 "an unviewed settled tab outlived the linger")

    # -- 7 ----------------------------------------------------------------
    def test_failed_tab_survives_the_linger_until_it_is_seen(self):
        """An unseen failure keeps its tab past the linger; seeing it starts the clock.

        Three steps, all needed: unseen failure stays, being looked at records ``seen``
        without retiring in that same pass, and the next pass past the linger retires
        it. An empty active window makes "unattached" explicit instead of relying on
        the sandbox happening to have no client.
        """
        with Sandbox() as box:
            box.export("HERMES_TMUX_LINGER_S", "1")
            deleg = "deleg_fail0001"
            box.add_delegation(deleg, [{"goal": "break things"}])
            self._up(box)
            layout = self._wait_tabs(box, 1)
            wkey = "%s#0" % deleg
            window, pane = layout["tabs"][wkey]["window"], layout["tabs"][wkey]["pane"]
            key = box.key()
            box.stop_coordinator()
            box.set_task_status(deleg, 0, "error")
            box.append_event(deleg, 0, "final", "status=error duration=2.0s")

            with inproc(box, linger="1"):
                from hermes_tmux import state, watch
                home = Path(box.home)
                workers = state.load_workers(home)
                original = watch._active_window
                try:
                    t0 = time.time()
                    watch._active_window = lambda session_id: ""
                    watch.reconcile_tabs(key, workers, home, now=t0)
                    tab = (watch.read_layout(key, home).get("tabs") or {}).get(wkey) or {}
                    self.assertTrue(tab, "the failed worker lost its tab")
                    self.assertIn("settled", tab)
                    self.assertNotIn("seen", tab,
                                     "an unattached session marked the tab seen: %r" % tab)

                    watch.reconcile_tabs(key, workers, home, now=t0 + 60.0)
                    tab = (watch.read_layout(key, home).get("tabs") or {}).get(wkey) or {}
                    self.assertTrue(tab,
                                    "a failure nobody looked at was retired after the "
                                    "linger (layout=%r)"
                                    % watch.read_layout(key, home))
                    self.assertTrue(_pane_exists(pane))

                    watch._active_window = lambda session_id: window
                    seen_at = t0 + 70.0
                    watch.reconcile_tabs(key, workers, home, now=seen_at)
                    tab = (watch.read_layout(key, home).get("tabs") or {}).get(wkey) or {}
                    self.assertIn("seen", tab,
                                  "looking at a failed tab did not acknowledge it: %r"
                                  % tab)

                    watch._active_window = lambda session_id: ""
                    watch.reconcile_tabs(key, workers, home, now=seen_at + 20.0)
                    self.assertNotIn(wkey, (watch.read_layout(key, home).get("tabs") or {}),
                                     "a seen failure was not retired after the linger")
                    self.assertFalse(_pane_exists(pane))
                finally:
                    watch._active_window = original

    # -- 8 ----------------------------------------------------------------
    def test_closed_tab_stays_closed_until_open_pins_it(self):
        """A tab the reader closed stays closed; ``open`` brings it back pinned.

        ``open`` is addressed by the last 8 characters of the delegation id, the
        product's own reference form. The pin is proved by reconciling an hour later
        with nothing in view: an unpinned tab would be gone. The reopened window must
        differ from the one the reader closed, or "reopened" is indistinguishable from
        "never closed".
        """
        with Sandbox() as box:
            box.export("HERMES_TMUX_LINGER_S", "1")
            deleg = "deleg_cls00001"
            box.add_delegation(deleg, [{"goal": "close me"}])
            self._up(box)
            layout = self._wait_tabs(box, 1)
            wkey = "%s#0" % deleg
            window = layout["tabs"][wkey]["window"]

            rc, out = _tmux("kill-window", "-t", window)
            self.assertEqual(rc, 0, "could not close the tab: %s" % out)
            box.append_event(deleg, 0, "tool", "-> nudge(x)")
            ok = wait_for(lambda: wkey not in box.tabs() and wkey in box.dismissed(),
                          timeout=20)
            self.assertTrue(ok, "a closed tab was not dismissed: tabs=%r dismissed=%r"
                            % (box.tabs(), box.dismissed()))

            box.append_event(deleg, 0, "tool", "-> NUDGE-AGAIN(x)")
            self.assertTrue(wait_for(lambda: "NUDGE-AGAIN" in box.render_text(),
                                     timeout=15),
                            "the coordinator never ran a pass after the dismissal")
            self.assertNotIn(wkey, box.tabs(),
                             "the coordinator reopened a tab the reader closed: %r"
                             % box.tabs())

            ref = "%s#0" % deleg[-8:]
            rc = box.cli("open", ref)
            self.assertEqual(rc, 0, "open %s exited %s: %r"
                             % (ref, rc, box.capture()[-500:]))
            ok = wait_for(lambda: (box.tabs().get(wkey) or {}).get("pinned") is True,
                          timeout=15)
            self.assertTrue(ok, "open did not create a pinned tab: %r" % box.tabs())
            layout = box.layout()
            tab = layout["tabs"][wkey]
            self.assertNotIn(wkey, layout.get("dismissed") or {},
                             "open left the worker in dismissed")
            self.assertNotEqual(tab["window"], window,
                                "open reused the window the reader closed")
            self.assertTrue(_pane_exists(tab["pane"]), "the reopened tab has no pane")

            box.finish(deleg, 0)
            self.assertTrue(wait_for(
                lambda: "settled" in ((box.tabs().get(wkey)) or {}), timeout=20),
                "the reopened tab never settled: %r" % box.tabs())
            key = box.key()
            with inproc(box, linger="1"):
                from hermes_tmux import state, watch
                home = Path(box.home)
                workers = state.load_workers(home)
                original = watch._active_window
                watch._active_window = lambda session_id: ""
                try:
                    watch.reconcile_tabs(key, workers, home, now=time.time() + 3600.0)
                finally:
                    watch._active_window = original
            layout = box.layout()
            self.assertIn(wkey, layout.get("tabs") or {},
                          "a pinned tab was retired by the linger: %r" % layout)
            self.assertTrue(_pane_exists(tab["pane"]),
                            "a pinned tab's pane was closed by the linger")

    # -- 9 ----------------------------------------------------------------
    def test_user_pane_and_foreign_window_survive_retirement_and_down(self):
        """Retirement and ``down`` close only panes that carry proof of ownership.

        Three independent controls: a foreign window already using the label (never
        adopted, never renamed, and our tab must step to ``#2`` because the suffix
        starts at 2), a reader's split inside a worker window (our pane closes, their
        pane and the window stay), and a foreign window's pane (survives ``down``).
        Retiring the split window would also be a failure: the test asserts the
        window still exists under its original name.
        """
        with Sandbox() as box:
            box.export("HERMES_TMUX_LINGER_S", "1")
            deleg = "deleg_safe0001"
            goals = ["audit alpha", "audit bravo"]
            box.add_delegation(deleg, [{"goal": g} for g in goals])
            label0 = labels.tab_label(goals[0], [goals[1]])

            rc, out = _tmux("new-window", "-d", "-P", "-F", "#{window_id}",
                            "-n", label0, "-t", box.name,
                            "-c", str(box.home), "exec sleep 600")
            self.assertEqual(rc, 0, "could not make the foreign window: %s" % out)
            foreign = out.strip().splitlines()[-1].strip()
            foreign_pane = _tmux("list-panes", "-t", foreign, "-F", "#{pane_id}")[1].strip()

            self._up(box)
            layout = self._wait_tabs(box, 2)
            tabs = layout["tabs"]
            win0 = tabs["%s#0" % deleg]["window"]
            self.assertNotEqual(win0, foreign,
                                "a foreign window with the worker's label was adopted")
            self.assertEqual(_window_name(win0), label0 + "#2",
                             "the new tab did not step around the foreign name")
            self.assertEqual(_window_name(foreign), label0,
                             "the foreign window was renamed")

            # The reader splits a shell into a worker tab.
            tab1 = tabs["%s#1" % deleg]
            win1, our1 = tab1["window"], tab1["pane"]
            name1 = _window_name(win1)
            rc, out = _tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                            "-t", win1, "-c", str(box.home), "exec sleep 600")
            self.assertEqual(rc, 0, "could not split the tab: %s" % out)
            user_pane = out.strip().splitlines()[-1].strip()
            self.assertNotEqual(user_pane, our1)

            box.finish(deleg, 0)
            box.finish(deleg, 1)
            ok = wait_for(lambda: not _pane_exists(our1) and _pane_exists(user_pane),
                          timeout=30)
            self.assertTrue(ok, "retirement did not close only our pane: our=%s(%s) "
                                "user=%s(%s)" % (our1, _pane_exists(our1),
                                                 user_pane, _pane_exists(user_pane)))
            self.assertFalse(_pane_exists(tabs["%s#0" % deleg]["pane"]),
                             "the other tab's pane survived retirement")
            self.assertTrue(_window_exists(win1),
                            "retirement tore down a window holding a user pane")
            self.assertEqual(_window_name(win1), name1,
                             "the window holding a user pane was renamed")
            self.assertTrue(_window_exists(foreign), "the foreign window was closed")

            rc = box.cli("down")
            self.assertEqual(rc, 0, "down exited %s: %r" % (rc, box.capture()[-500:]))
            self.assertTrue(_pane_exists(user_pane),
                            "down closed a pane the reader created")
            self.assertTrue(_pane_exists(foreign_pane),
                            "down closed a foreign window's pane")
            self.assertTrue(_window_exists(win1),
                            "down closed the window that held a user pane")
            self.assertTrue(_window_exists(foreign), "down closed the foreign window")
            self.assertIsNone(box.window("agents"), "down left 'agents' standing")

    # -- 10 ---------------------------------------------------------------
    def test_down_closes_our_windows_stops_the_coordinator_and_keeps_the_user_pane(self):
        """``down`` closes ours, stops the coordinator, and removes the state directory.

        The reader's pane is identified before ``down`` and must keep both its id and
        its window. The coordinator is proved stopped by pid rather than by its pid
        file, which can be gone while the process is still exiting.

        Known sensitivity: ``cmd_down`` SIGTERMs the coordinator and waits 2.0 s, then
        exits 1 if the flock is still held. On a loaded machine the coordinator can
        still be inside its exit path when that window closes, so this test can fail
        on timing alone. There is no retry and no masking; it passes on CI, and the
        race is real.
        """
        with Sandbox() as box:
            deleg = "deleg_down0001"
            box.add_delegation(deleg, [{"goal": "audit one"}, {"goal": "audit two"}])
            self._up(box)
            self._wait_tabs(box, 2)
            user_pane = box.pane_id
            user_window = _pane_window(user_pane)
            sdir = box.tab_dir()
            self.assertIsNotNone(sdir)
            pid = box.watcher_pid()
            self.assertIsNotNone(pid, "no coordinator was started")

            rc = box.cli("down")
            self.assertEqual(rc, 0, "down exited %s: %r" % (rc, box.capture()[-500:]))

            ours = [w for w in box.windows() if w["ours"]]
            self.assertEqual(ours, [], "down left windows we own: %r"
                             % [(w["name"], w["id"]) for w in ours])
            self.assertFalse(sdir.exists(),
                             "down left the session state directory: %s" % sdir)
            self.assertTrue(wait_for(lambda: not _pid_alive(pid), timeout=10),
                            "down left the coordinator (%d) running" % pid)
            self.assertTrue(_pane_exists(user_pane), "down closed the reader's pane")
            self.assertEqual(_pane_window(user_pane), user_window,
                             "the reader's pane changed window")

    # -- 11 ---------------------------------------------------------------
    def test_worker_n_focuses_the_nth_tab_in_window_index_order(self):
        """``worker N`` focuses the Nth tab in status-line order, window and pane.

        The expected tab comes from sorting the layout's own windows by index, so the
        test does not assume creation order equals display order. Selecting the window
        alone would leave the pane unfocused, hence both assertions.
        """
        with Sandbox() as box:
            deleg = "deleg_ord00001"
            box.add_delegation(deleg, [{"goal": "audit %s" % n}
                                       for n in ("alpha", "bravo", "charlie")])
            self._up(box)
            layout = self._wait_tabs(box, 3)
            ordered = sorted(layout["tabs"].items(),
                             key=lambda item: _window_index(item[1]["window"]))
            self.assertEqual(len(ordered), 3)
            target_key, target = ordered[1]

            rc = box.cli("worker", "2")
            self.assertEqual(rc, 0, "worker 2 exited %s: %r" % (rc, box.capture()[-500:]))
            current = _format(box.name, "#{window_id}")
            self.assertEqual(current, target["window"],
                             "worker 2 selected %s, expected the second tab %s (%s)"
                             % (current, target["window"], target_key))
            active = _format(target["window"], "#{pane_id}")
            self.assertEqual(active, target["pane"],
                             "worker 2 did not focus the tab's pane")

    # -- 12 ---------------------------------------------------------------
    def test_down_leaves_the_user_pane_out_of_copy_mode(self):
        """``down`` must not put the reader's pane into copy-mode.

        The pre-condition is asserted too: a pane already in a mode before ``down``
        would let the post-condition pass for the wrong reason.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_mode0001", [{"goal": "audit mode"}])
            self._up(box)
            self._wait_tabs(box, 1)
            self.assertEqual(
                _format(box.pane_id, "#{pane_in_mode}"), "0",
                "the reader's pane was in a mode while the workspace ran")
            rc = box.cli("down")
            self.assertEqual(rc, 0, "down exited %s: %r" % (rc, box.capture()[-500:]))
            self.assertTrue(_pane_exists(box.pane_id), "down closed the reader's pane")
            self.assertEqual(
                _format(box.pane_id, "#{pane_in_mode}"), "0",
                "down put the reader's pane into copy-mode: %r"
                % box.capture(box.pane_id)[-400:])
            self.assertTrue(_window_exists(_pane_window(box.pane_id)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
