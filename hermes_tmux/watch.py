"""Event-driven watcher and pane display for the tmux workspace.

Two long-lived processes, both children of the tmux server (so they survive
detach/reattach):

``watch``
    One per tmux session. Holds a kqueue over the delegation live root,
    re-renders the overview on filesystem events, keeps the per-worker
    ``tail -F`` panes pointed at the right task logs, and badges the window
    name. Never reads a file unless the kernel reported a change.

``follow``
    One per overview pane. Composes the block itself (at the pane's own width,
    from the same records) and redraws on live-root changes, with a bounded wait
    so a dead coordinator degrades to periodic re-reads rather than a frozen
    pane. ``render.txt`` is still written per session for ``status`` and the
    watcher, but the pane no longer depends on it.

Neither process polls for data. The only timed wake is one scheduled wake per
minute while any worker is running, used solely to advance the display-only
"stalled" label in ``render.render()`` from already-loaded records; it performs
no filesystem or database reads.
"""

from __future__ import annotations

import errno
import fcntl
from contextlib import contextmanager
import json
import os
import select
import shlex
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import tmuxio
from .gate import enabled

STATE_DIR_NAME = "hermes-tmux"
#: At most two live worker transcripts in one window. One window per pair keeps
#: every pane wide enough to be readable and bounds visual churn: a window is
#: created only when both slots of every existing worker window are taken.
WORKERS_PER_WINDOW = 2
#: Hard cap on worker panes across all windows. Delegations are read up to 40
#: records; without a cap a long session accumulates windows without limit.
MAX_VISIBLE = 16
WORKER_WINDOW_PREFIX = "hx:worker-"
_DEBOUNCE_S = 0.12
#: Floor between event-driven passes. state.db-wal is written on every Hermes
#: event, so an unthrottled vnode loop would re-read 40 transcripts many times a
#: second. One second still reads as live and keeps the coordinator cheap.
_MIN_RENDER_INTERVAL_S = 1.0
_TIMER_WAKE_S = 60.0
#: Upper bound on how long the pane may go without re-reading state. Only used so
#: the pane degrades (stale but truthful) instead of freezing when nothing fires.
_FOLLOW_WAKE_S = 60.0
#: How often the coordinator checks that its server still exists. `run-shell -b`
#: children are detached from the server's lifetime, so without this the watcher
#: would outlive the session it serves and accumulate one process per session.
_SESSION_CHECK_S = 20.0
_MAX_WATCH_DIRS = 64
_MAX_WATCH_FILES = 256

_VNODE_FLAGS = (
    select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND
    | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME
)


# --------------------------------------------------------------------------- paths

def state_root(home: Optional[Path] = None) -> Path:
    from .state import hermes_home
    base = hermes_home(home)
    return base / "cache" / STATE_DIR_NAME


def session_dir(key: str, home: Optional[Path] = None) -> Path:
    return state_root(home) / "sessions" / key


def layout_path(key: str, home: Optional[Path] = None) -> Path:
    return session_dir(key, home) / "layout.json"


def render_path(key: str, home: Optional[Path] = None) -> Path:
    return session_dir(key, home) / "render.txt"


def pid_path(key: str, home: Optional[Path] = None) -> Path:
    return session_dir(key, home) / "watch.pid"


def lock_path(key: str, home: Optional[Path] = None) -> Path:
    return session_dir(key, home) / "watch.lock"


def atomic_write(path: Path, text: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def read_layout(key: str, home: Optional[Path] = None) -> Dict:
    try:
        return json.loads(layout_path(key, home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@contextmanager
def _layout_lock(key: str, home: Optional[Path] = None):
    """Serialise layout read-modify-write across processes.

    The coordinator, `up`, and every aux command all rewrite layout.json, and
    each one used to write back what it read at the start of its work. A writer
    that was mid-flight while another process published a change silently
    reverted it: `up` then `logs` dropped tail_panes, and the coordinator could
    drop an aux entry. The lock makes the update a transaction.
    """
    path = session_dir(key, home) / ".layout.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
    except OSError:
        yield
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def update_layout(key: str, mutate, home: Optional[Path] = None) -> Dict:
    """Read, apply ``mutate``, write, all under the layout lock."""
    with _layout_lock(key, home):
        layout = read_layout(key, home)
        result = mutate(layout)
        if not isinstance(result, dict):
            result = layout
        write_layout(key, result, home)
        return result


def write_layout(key: str, layout: Dict, home: Optional[Path] = None) -> bool:
    layout["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return atomic_write(layout_path(key, home),
                        json.dumps(layout, indent=2, sort_keys=True))


# --------------------------------------------------------------------------- kqueue

class WatchSet:
    """kqueue wrapper over vnodes. `sync(paths)` reconciles the watch set."""

    def __init__(self) -> None:
        self.kq = select.kqueue()
        self.dirs: Dict[str, int] = {}
        self.files: Dict[str, int] = {}

    def close(self) -> None:
        for fd in list(self.dirs.values()) + list(self.files.values()):
            try:
                os.close(fd)
            except OSError:
                pass
        self.dirs.clear()
        self.files.clear()
        try:
            self.kq.close()
        except OSError:
            pass

    def _add(self, fd: int) -> None:
        try:
            self.kq.control(
                [select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                               flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                               fflags=_VNODE_FLAGS)],
                0, 0,
            )
        except OSError:
            pass

    def sync(self, dirs: Sequence[Path], files: Sequence[Path]) -> None:
        want_dirs = {str(p) for p in dirs}
        want_files = {str(p) for p in files}

        for path in list(self.dirs):
            if path not in want_dirs:
                try:
                    os.close(self.dirs.pop(path))
                except OSError:
                    pass
        for path in list(self.files):
            if path not in want_files:
                try:
                    os.close(self.files.pop(path))
                except OSError:
                    pass

        for path in want_dirs:
            if path in self.dirs or len(self.dirs) >= _MAX_WATCH_DIRS:
                continue
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError:
                continue
            self.dirs[path] = fd
            self._add(fd)
        for path in want_files:
            if path in self.files or len(self.files) >= _MAX_WATCH_FILES:
                continue
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError:
                continue
            self.files[path] = fd
            self._add(fd)

    def wait(self, timeout: Optional[float]) -> int:
        try:
            events = self.kq.control(None, 64, timeout)
        except OSError as exc:
            if exc.errno == errno.EINTR:
                return 0
            return 0
        return len(events)

    def drain(self, quiet_for: float = _DEBOUNCE_S) -> None:
        """Coalesce a burst of events (one append can raise two) into one render."""
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not self.wait(quiet_for):
                return


def collect_paths(home: Optional[Path] = None) -> Tuple[List[Path], List[Path]]:
    """Directories and files whose changes mean "state changed"."""
    from .state import hermes_home, live_root
    root = live_root(home)
    dirs: List[Path] = []
    files: List[Path] = []
    if root.is_dir():
        dirs.append(root)
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name, reverse=True)[:40]
        except OSError:
            children = []
        for child in children:
            if not child.is_dir():
                continue
            dirs.append(child)
            try:
                for entry in child.iterdir():
                    # The manifest is rewritten in place when a task changes
                    # state. Watching the directory alone does not see that: a
                    # kqueue vnode on a directory fires for create/delete, not
                    # for a write to a child, so the overview used to lag a
                    # lifecycle change by the whole session-check interval.
                    if entry.suffix == ".log" or entry.name == "manifest.json":
                        files.append(entry)
            except OSError:
                continue
    reviews = hermes_home(home) / "cache" / STATE_DIR_NAME
    if reviews.is_dir():
        dirs.append(reviews)
        candidate = reviews / "reviews.json"
        if candidate.exists():
            files.append(candidate)
    # Terminal state and token/cost data live in state.db, not in the manifest.
    # Without a watch on the database a task that finishes only there kept its
    # old "running" row until an unrelated transcript event happened to fire.
    # The WAL is where SQLite writes first; both are heap files, so a vnode
    # watch is enough and no polling is needed.
    db = hermes_home(home) / "state.db"
    for suffix in ("", "-wal"):
        candidate = Path(str(db) + suffix)
        if candidate.exists():
            files.append(candidate)
    return dirs, files


# --------------------------------------------------------------------------- pick

def _task_index(worker) -> int:
    try:
        return int(getattr(worker, "task_index", 0) or 0)
    except (TypeError, ValueError):
        return 0


def worker_order(workers: Sequence) -> List:
    """Stable order: newest delegation first, task index ascending within one.

    Deliberately independent of run state. Ordering by state made a worker that
    finished jump position and drag its pane (and its window neighbours) with
    it, which is exactly the reshuffle this layout must avoid.
    """
    ordered = [w for w in workers if getattr(w, "key", "")]
    try:
        ordered.sort(
            key=lambda w: (str(getattr(w, "delegation_started", "") or ""),
                           str(getattr(w, "delegation_id", "") or "")),
            reverse=True,
        )
        # Rank delegations by that order, then sort tasks inside each one. A
        # plain second sort on task index alone would interleave delegations.
        rank = {}
        for w in ordered:
            pair = (str(getattr(w, "delegation_started", "") or ""),
                    str(getattr(w, "delegation_id", "") or ""))
            rank.setdefault(pair, len(rank))
        ordered.sort(key=lambda w: (
            rank[(str(getattr(w, "delegation_started", "") or ""),
                  str(getattr(w, "delegation_id", "") or ""))],
            _task_index(w),
        ))
    except Exception:                             # pragma: no cover - defensive
        return ordered
    return ordered


def select_visible(workers: Sequence, assigned: Iterable[str] = (),
                   limit: int = MAX_VISIBLE) -> List:
    """Workers that get a pane: running ones first, then newest, stable order.

    ``assigned`` names workers that already own a pane. They are preferred over
    unseen settled workers when the cap bites, so a display limit never closes
    a pane the reader is already using while an older finished worker waits.
    The result keeps ``worker_order`` so window grouping is predictable.
    """
    ordered = worker_order(workers)
    if len(ordered) <= limit:
        return ordered
    running = [w for w in ordered if getattr(w, "state", "") == "running"]
    keep_running = running[:limit]
    room = limit - len(keep_running)
    pinned = set(assigned)
    settled = [w for w in ordered if getattr(w, "state", "") != "running"]
    chosen = keep_running + sorted(
        settled, key=lambda w: 0 if getattr(w, "key", "") in pinned else 1
    )[:room]
    chosen_keys = {getattr(w, "key", "") for w in chosen}
    return [w for w in ordered if getattr(w, "key", "") in chosen_keys]


def _slot_sort(slot: str) -> int:
    text = str(slot).lstrip("0")
    return int(text) if text.isdigit() else 0


def _next_slot(windows: Dict[str, str]) -> str:
    used = [_slot_sort(slot) for slot in windows] or [0]
    return str(max(used) + 1)


def _log_path(worker) -> Optional[str]:
    return getattr(worker, "log_path", None) or getattr(worker, "_log_path", None)


def configure_worker_window(window_id: str) -> None:
    """Border label + clean tiling for a worker window we own."""
    tmuxio.window_option(window_id, "pane-border-status", "top")
    tmuxio.window_option(window_id, "pane-border-format", " #{pane_title} ")


def trim_overview_window(layout: Dict) -> bool:
    """Close our panes in the overview window that are not the overview.

    Version 1 put worker tail panes in the overview window. The overview window
    is now overview-only, so those panes are retired here. Only panes proven to
    be ours are closed; a user's split in the same window is left alone, and the
    function then reports no change for it.
    """
    window_id = layout.get("workers_window")
    keep = layout.get("overview_pane")
    if not window_id or not keep or not tmuxio.window_exists(window_id) \
            or not tmuxio.owns_window(window_id):
        return False
    closed = False
    for pane in tmuxio.list_panes(window_id):
        if pane["pane_id"] == keep:
            continue
        if pane["ours"] and tmuxio.close_pane(pane["pane_id"])[0]:
            closed = True
    return closed


# --------------------------------------------------------------------------- watch

def _pane_env() -> Dict[str, str]:
    """Env for panes this process creates (forked from the server, not from us)."""
    from .env import pane_env
    return pane_env()


def tail_command(log_path: str) -> str:
    return "exec tail -F %s" % shlex.quote(log_path)


def pane_label(worker) -> str:
    """Border label: worker id, task index, LIVE STATE, model.

    The state is the point: a pane tailing a finished transcript looks exactly
    like a pane tailing a running one, so the border is what tells a reader
    whether the worker is still executing. The label is the only per-pane
    lifecycle claim, and it comes from the same records the overview uses.
    """
    from .render import _state_label  # same mapping the overview uses
    from datetime import datetime
    model = getattr(worker, "model", None) or "?"
    model = model.split("/")[-1]
    state = _state_label(worker, datetime.now())
    return "%s t%s %s %s" % (getattr(worker, "delegation_id", "?").replace("deleg_", ""),
                             getattr(worker, "task_index", "?"), state, model)


# --------------------------------------------------------------------------- watch

def reconcile_panes(key: str, workers: Sequence, home: Optional[Path] = None) -> Dict:
    """Keep one overview-only window plus worker windows of at most two panes.

    Identity and stability rules:

    * the overview window holds the overview pane and nothing else of ours;
    * a worker keeps its pane and its window for as long as it is visible. A
      worker finishing, or another worker starting, never moves an existing
      pane: new work fills the lowest free slot, then opens a new window;
    * a worker that leaves the visible set gives up its pane; a window with no
      worker panes left is retired only when every remaining pane is ours;
    * a window that holds any pane we did not create is never split, renamed,
      closed, or repurposed. It is simply forgotten by the layout.
    """
    layout = read_layout(key, home)
    changed = trim_overview_window(layout)

    session_id = layout.get("session_id") or tmuxio.current_session_id() or ""
    windows: Dict[str, str] = {
        str(slot): str(wid)
        for slot, wid in (layout.get("worker_windows") or {}).items()
    }
    assign: Dict[str, str] = {
        str(wkey): str(slot)
        for wkey, slot in (layout.get("worker_assign") or {}).items()
    }
    tail_panes: Dict[str, str] = {
        str(pane_id): str(wkey)
        for pane_id, wkey in (layout.get("tail_panes") or {}).items()
    }

    # 1. Validate what we recorded. A window id is unique only within one
    #    server, and a stale id can name a user window on a new server.
    for slot in list(windows):
        wid = windows[slot]
        if not (tmuxio.window_exists(wid) and tmuxio.owns_window(wid)):
            windows.pop(slot, None)
    for wkey, slot in list(assign.items()):
        if slot not in windows:
            assign.pop(wkey, None)
    for pane_id in list(tail_panes):
        if not tmuxio.pane_exists(pane_id):
            tail_panes.pop(pane_id, None)

    desired: Dict[str, Tuple[str, str]] = {}
    for worker in select_visible(workers, assigned=assign.keys()):
        wkey = str(getattr(worker, "key", ""))
        log_path = _log_path(worker)
        if wkey and log_path:
            desired[wkey] = (log_path, pane_label(worker))

    # 2. Exactly one pane per worker. Duplicates are a bookkeeping artefact
    #    (a pane that survived a crash); keep the first and retire the rest.
    by_worker: Dict[str, str] = {}
    for pane_id, wkey in list(tail_panes.items()):
        if wkey not in desired or wkey in by_worker:
            if tmuxio.close_pane(pane_id)[0] or wkey not in desired:
                tail_panes.pop(pane_id, None)
                changed = True
            continue
        by_worker[wkey] = pane_id

    # 3. Panes that exist but whose window binding is gone are closed, not
    #    repointed: a repointed pane loses the scrollback the reader had.
    slot_panes: Dict[str, List[str]] = {
        slot: [p["pane_id"] for p in tmuxio.list_panes(wid) if p["ours"]]
        for slot, wid in windows.items()
    }
    placed: Dict[str, str] = {}
    for wkey, pane_id in list(by_worker.items()):
        slot = assign.get(wkey)
        if slot in windows and pane_id in slot_panes.get(slot, []):
            placed[wkey] = pane_id
            continue
        assign.pop(wkey, None)
        if tmuxio.close_pane(pane_id)[0]:
            tail_panes.pop(pane_id, None)
            by_worker.pop(wkey, None)
            changed = True

    # 4. Place every desired worker that has no pane yet: lowest free slot in
    #    an existing window, else a new window. Order is the stable worker
    #    order, so the first pair of a delegation lands in the first window.
    for wkey, (log_path, label) in desired.items():
        if wkey in placed:
            continue
        target = ""
        for slot in sorted(slot_panes, key=_slot_sort):
            if slot not in windows:
                continue
            if len(slot_panes[slot]) < WORKERS_PER_WINDOW \
                    and not tmuxio.foreign_panes(windows[slot]):
                target = slot
                break
        if target:
            pane_id = tmuxio.split_window(
                windows[target], tail_command(log_path), horizontal=True,
                env=_pane_env()) or ""
            slot = target
        else:
            slot = _next_slot(windows)
            name = tmuxio.free_window_name(
                session_id, "%s%d" % (WORKER_WINDOW_PREFIX, int(slot)))
            window_id = tmuxio.new_window(
                session_id, name, cwd=str(Path.home()),
                command=tail_command(log_path), env=_pane_env())
            if not window_id:
                continue
            windows[slot] = window_id
            slot_panes[slot] = []
            configure_worker_window(window_id)
            panes = tmuxio.wait_for_panes(window_id)
            pane_id = panes[0]["pane_id"] if panes else ""
            changed = True
        if not pane_id:
            continue
        assign[wkey] = slot
        tail_panes[pane_id] = wkey
        placed[wkey] = pane_id
        slot_panes.setdefault(slot, []).append(pane_id)
        tmuxio.set_title(pane_id, label)
        changed = True

    # 5. Retire worker windows that no longer hold any worker pane. The guard in
    #    close_window refuses a window with even one foreign pane, and such a
    #    window is dropped from our map instead of being retried forever.
    for slot in list(windows):
        window_id = windows[slot]
        if any(p["ours"] for p in tmuxio.list_panes(window_id)):
            continue
        if tmuxio.close_window(window_id)[0]:
            changed = True
        windows.pop(slot, None)

    # 6. Refresh border labels even when nothing moved: the state in a label is
    #    a lifecycle claim that changes while the worker runs (run -> done).
    retitled = False
    for slot, window_id in windows.items():
        titles = {p["pane_id"]: p["title"] for p in tmuxio.list_panes(window_id)}
        for pane_id, wkey in tail_panes.items():
            want = desired.get(wkey, (None, ""))[1]
            if assign.get(wkey) != slot or not want:
                continue
            if titles.get(pane_id, "") != want and tmuxio.set_title(pane_id, want):
                retitled = True
        if changed:
            tmuxio.run("select-layout", "-t", window_id, "tiled")

    if changed or retitled:
        def _apply(current: Dict) -> Dict:
            current["tail_panes"] = tail_panes
            current["worker_windows"] = windows
            current["worker_assign"] = assign
            return current
        layout = update_layout(key, _apply, home)
    return layout


def badge_name(workers: Sequence, base: str = "hx:workers") -> str:
    running = sum(1 for w in workers if getattr(w, "state", "") == "running")
    failed = sum(1 for w in workers
                 if getattr(w, "state", "") in ("failed", "error", "interrupted"))
    suffix = ""
    if running:
        suffix += " %d" % running
    if failed:
        suffix += "!"
    return base + suffix


def render_and_publish(key: str, home: Optional[Path] = None,
                       session_label: str = "") -> str:
    from . import render as render_mod
    from . import state as state_mod
    from .state import live_root

    note = ""
    try:
        workers = state_mod.load_workers(home)
    except Exception as exc:                      # pragma: no cover - defensive
        workers = []
        # Never present a reader crash as "no delegations": that is a different,
        # wrong claim. Say what failed.
        note = "state reader failed: %s" % (exc,)
    text = render_mod.render(
        workers,
        session_label=session_label,
        live_root=str(live_root(home)),
        note=note,
    )
    current = ""
    try:
        current = render_path(key, home).read_text(encoding="utf-8")
    except OSError:
        pass
    if text != current:
        atomic_write(render_path(key, home), text)
    return text


def run_watch(home: Optional[Path] = None, key: Optional[str] = None) -> int:
    """Main loop of the coordinator. Returns an exit code."""
    if not enabled():
        return 0
    # `key` comes from the caller that built the layout: a run-shell child has
    # no TMUX_PANE and would otherwise derive a key nothing else uses.
    key = key or tmuxio.session_key()
    sdir = session_dir(key, home)
    try:
        sdir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 1

    lock_file = None
    try:
        lock_file = open(lock_path(key, home), "a+")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        return 0  # another watcher already owns this session

    pid_path(key, home).write_text(str(os.getpid()), encoding="utf-8")
    # The server that owns us. A tmux server that dies without killing its jobs
    # (crash, SIGKILL) leaves this process running while a NEW server answers on
    # the same socket; that server's window ids come from a different sequence,
    # so continuing to reconcile would rename or split into windows we never
    # created. Identity is the server pid, not the socket path.
    owner_pid = tmuxio.server_pid()
    layout = read_layout(key, home)
    session_label = layout.get("session_name") or tmuxio.current_session_name() or ""

    watcher = WatchSet()
    last_sig = None
    last_pass = 0.0
    try:
        while True:
            # state.db-wal fires on every Hermes write. Cap the pass rate so a
            # busy session cannot turn the coordinator into a re-read loop; the
            # first pass is never delayed.
            if last_pass:
                remaining = _MIN_RENDER_INTERVAL_S - (time.time() - last_pass)
                if remaining > 0:
                    watcher.wait(min(remaining, _SESSION_CHECK_S))
                    live_pid = tmuxio.server_pid()
                    if owner_pid and live_pid and live_pid != owner_pid:
                        return 0
                    if not tmuxio.session_alive(layout.get("session_id") or ""):
                        return 0
            last_pass = time.time()
            # Reconcile BEFORE blocking. The first pass must run immediately:
            # `up` starts this process after writing the layout, and a delegation
            # that already existed raised no vnode event, so waiting first would
            # leave worker panes unbuilt for up to _TIMER_WAKE_S.
            dirs, files = collect_paths(home)
            watcher.sync(dirs, files)
            last_sig = _signature(dirs, files)
            render_and_publish(key, home, session_label)
            try:
                from . import state as state_mod
                workers = state_mod.load_workers(home)
            except Exception:
                workers = []
            layout = reconcile_panes(key, workers, home)
            window_id = layout.get("workers_window")
            # Renaming is a write to someone else's window if that id names a
            # foreign window on a new server.
            if window_id and tmuxio.owns_window(window_id):
                tmuxio.rename_window(window_id, badge_name(workers))

            fired = watcher.wait(_SESSION_CHECK_S)
            live_pid = tmuxio.server_pid()
            if owner_pid and live_pid and live_pid != owner_pid:
                return 0          # a different server now answers this socket
            if not tmuxio.session_alive(layout.get("session_id") or ""):
                return 0          # the session this coordinator serves is gone
            if fired:
                watcher.drain()
                sig = _signature(dirs, files)
                if sig == last_sig:
                    continue
                last_sig = sig
    except KeyboardInterrupt:
        return 0
    finally:
        watcher.close()
        try:
            # Only retire the pid file if it still names this process: a restarted
            # watcher may already have written its own pid here.
            pid_file = pid_path(key, home)
            if pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass
        if lock_file is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            except (OSError, IOError):
                pass


def _signature(dirs: Sequence[Path], files: Sequence[Path]) -> Tuple:
    """Cheap change signature: (path, size, mtime) for every watched file."""
    sig = []
    for path in files:
        try:
            st = os.stat(path)
            sig.append((str(path), st.st_size, int(st.st_mtime)))
        except OSError:
            sig.append((str(path), -1, -1))
    return tuple(sorted(sig))


# --------------------------------------------------------------------------- follow

def _draw(text: str, pane_id: str) -> None:
    width, height = tmuxio.pane_size(pane_id)
    lines = text.splitlines()
    if len(lines) > height:
        lines = lines[:height]
    buf = ["\033[H"]
    for index, line in enumerate(lines):
        buf.append(line[:width])
        buf.append("\033[K")
        # No newline after the last line: with one, writing exactly `height`
        # lines scrolls the pane by one row and the title (running/total, age)
        # silently disappears, which reads as a frozen overview.
        if index < len(lines) - 1:
            buf.append("\r\n")
    buf.append("\033[J")
    try:
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def compose_for_pane(home: Optional[Path] = None, session_label: str = "",
                     pane_id: str = "") -> str:
    """Render the overview at this pane's width.

    The pane is the only consumer that knows its own width. Rendering at a fixed
    110 columns and clipping afterwards drops the GOAL text on narrow panes, so
    the pane composes its own text; ``render.txt`` stays the shared, width
    independent copy used by ``status`` and the watcher.
    """
    from . import render as render_mod
    from . import state as state_mod
    from .state import live_root

    width = tmuxio.pane_size(pane_id)[0] if pane_id else 110
    note = ""
    try:
        workers = state_mod.load_workers(home)
    except Exception as exc:
        workers = []
        # Same rule as render_and_publish: a crash is not an empty workspace.
        note = "state reader failed: %s" % (exc,)
    try:
        return render_mod.render(workers, width=width, session_label=session_label,
                                 live_root=str(live_root(home)), note=note)
    except Exception:
        return render_mod.render_empty(width=width, live_root=str(live_root(home)),
                                       note=note)


def run_follow(home: Optional[Path] = None, key: Optional[str] = None) -> int:
    """Pane-side display loop: redraw when the live root or layout changes."""
    if not enabled():
        print("hermes-tmux: disabled (%s)" % _reason())
        return 0
    pane_id = os.environ.get("TMUX_PANE", "")
    key = key or tmuxio.session_key()
    layout = read_layout(key, home)
    session_label = layout.get("session_name") or tmuxio.current_session_name() or ""

    target = render_path(key, home)
    watcher = WatchSet()
    try:
        while True:
            # A pane-side loop outlives its server when the server is killed
            # outright (no `down` to close panes): tmux does not always deliver
            # SIGHUP, so without this the process keeps redrawing into a dead
            # pane forever. One cheap check per wake, and the loop is cheap.
            if pane_id and not tmuxio.pane_exists(pane_id):
                return 0
            text = compose_for_pane(home, session_label, pane_id)
            if pane_id:
                _draw(text, pane_id)
            dirs, files = collect_paths(home)
            watcher.sync([target.parent] + dirs, [target] + files)
            # A bounded wait, not None: if the coordinator dies, no vnode event
            # will ever arrive, and a blocking wait would freeze this pane at the
            # last state it saw. One recompose a minute keeps the display honest
            # (it re-reads the real artifacts, it does not poll for progress).
            watcher.wait(_FOLLOW_WAKE_S)
            watcher.drain(quiet_for=0.05)
    except KeyboardInterrupt:
        return 0
    finally:
        watcher.close()


def _reason() -> str:
    from .gate import disabled_reason
    return disabled_reason() or "off"
