"""Event-driven coordinator and overview display for the tmux workspace.

Two long-lived processes, both children of the tmux server (so they survive
detach/reattach):

``watch``
    One per workspace (keyed by ``@hx_key`` on the overview window). Holds a
    kqueue over ``state.db``/``state.db-wal``, ``reviews.json``, ``live/``
    directories, manifests, and active transcripts; reconciles worker tabs,
    badges the overview tab, and publishes ``render.txt``. Bursts are coalesced
    and throttled to ``_MIN_RENDER_INTERVAL_S`` (1 Hz), while unchanged
    manifests and historical transcripts are stat-cached in memory.

``follow``
    One per ``agents`` overview pane. Redraws immediately on ``SIGWINCH`` or
    when ``render.txt`` / ``layout.json`` / active delegation files change.
    While the coordinator holds ``watch.lock``, ``follow`` relies on the
    coordinator to observe ``state.db-wal`` so unrelated parent chat writes do
    not wake the pane; if the coordinator is absent, ``follow`` watches
    ``state.db`` directly and degrades gracefully on bounded timer wakes
    (``_FOLLOW_RUNNING_WAKE_S`` while work is running, ``_FOLLOW_WAKE_S`` idle).

Division of labour. As between these two processes, only the coordinator mutates
tmux state and ``layout.json``; ``follow`` reads state and draws. (The CLI
commands do the same mutations on demand, under the same locks.) Everything the
coordinator does is idempotent per pass, because a pass can die half-way (tmux
timeout, SIGTERM, a killed server) and the next one must converge from whatever
is on disk.

What this module is not. It does not decide what a worker's state *is* — that
is ``state.load_workers``. It does not decide whether work is good, and it
never invents an event: every redraw is caused by a vnode that actually fired
or by a timer whose only job is to let a display-only label age. Silence in a
transcript is not progress, and it is not failure either.

Concurrency in one paragraph. ``layout.json`` is shared by the coordinator,
``up``, ``open``, every aux command and ``down``. Reads go through
``read_layout``; writes go through ``update_layout`` (flock on
``.layout.lock``, read-modify-write). A tab *pass* is not atomic — it reads the
layout, spends seconds in tmux, then writes — so its write is a three-way merge
(``_merge``) of "what I saw" against "what is there now" that only applies this
pass's own deltas. Writing the whole document back is what once reverted a tab
``open`` had just published and produced a duplicate window for the same
worker. The coordinator's own liveness is a separate flock on ``watch.lock``:
whoever holds it *is* the coordinator, which is the identity ``down`` and
``follow`` test (a command line can be forged, an open lock cannot).
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
#: Worker tabs open at once. One worker per tab keeps every transcript at the
#: full window size and makes a tab's state exactly one worker's state. Eight
#: labelled tabs still fit a 188-column status line beside the user's windows.
MAX_TABS = 8
#: How long a finished worker keeps its tab after it settles or was last
#: looked at. Long enough to read the result Hermes just reported, short enough
#: that the tab bar shows current work instead of history.
LINGER_S = 300.0


def linger_s() -> float:
    """``LINGER_S``, or ``HERMES_TMUX_LINGER_S`` when set to a number."""
    try:
        return max(0.0, float(os.environ["HERMES_TMUX_LINGER_S"]))
    except (KeyError, ValueError):
        return LINGER_S


OVERVIEW_WINDOW = "agents"
#: Idle wake while a tab is waiting to retire or be acknowledged; otherwise the
#: coordinator sleeps for the whole session-check interval. A settled tab must
#: age out on time even when nothing on disk changes, which is why this is a
#: timer and not an event.
_TICK_S = 5.0
_DEBOUNCE_S = 0.12
#: Floor between event-driven passes. state.db-wal is written on every Hermes
#: event, so an unthrottled vnode loop would re-read 40 transcripts many times a
#: second. One second still reads as live and keeps the coordinator cheap.
_MIN_RENDER_INTERVAL_S = 1.0
#: Unused today: nothing reads it. The loop's waits come from ``_TICK_S`` and
#: ``_SESSION_CHECK_S``; the constant remains only so the numbers in this block
#: stay together.
_TIMER_WAKE_S = 60.0
#: Upper bound on how long the pane may go without re-reading state. Only used so
#: the pane degrades (stale but truthful) instead of freezing when nothing fires.
_FOLLOW_WAKE_S = 60.0
_FOLLOW_RUNNING_WAKE_S = 2.0
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
    """``<HERMES_HOME>/cache/hermes-tmux``: one directory per install, not per server."""
    from .state import hermes_home
    base = hermes_home(home)
    return base / "cache" / STATE_DIR_NAME


def session_dir(key: str, home: Optional[Path] = None) -> Path:
    """Per-workspace directory: layout, render.txt, lock, pid, watch.log.

    Named by the workspace key, which already contains the server digest, so two
    servers never share one. Everything here is disposable: ``down`` removes it
    once no coordinator holds the lock.
    """
    return state_root(home) / "sessions" / key


def layout_path(key: str, home: Optional[Path] = None) -> Path:
    """``layout.json``: the only durable record of what this workspace built."""
    return session_dir(key, home) / "layout.json"


def render_path(key: str, home: Optional[Path] = None) -> Path:
    """``render.txt``: the plain overview text, doubled as the redraw signal."""
    return session_dir(key, home) / "render.txt"


def pid_path(key: str, home: Optional[Path] = None) -> Path:
    """``watch.pid``: a hint only. Identity is the lock holder, not this file."""
    return session_dir(key, home) / "watch.pid"


def lock_path(key: str, home: Optional[Path] = None) -> Path:
    """``watch.lock``: the coordinator's flock for its whole life."""
    return session_dir(key, home) / "watch.lock"


def coordinator_alive(key: str, home: Optional[Path] = None) -> bool:
    """True when a coordinator currently holds the advisory flock on ``watch.lock``.

    Probes by *taking* the lock non-blocking and releasing it: an EACCES/EAGAIN
    answer means someone holds it, any other error (including "no file") means
    nobody does. The probe is a momentary hold, which is why ``run_watch``
    retries its own lock a few times at startup instead of exiting on the first
    refusal.

    This is used by ``follow`` to decide whether it needs to watch
    ``state.db-wal`` itself. A false "alive" costs a missed redraw until the
    next timer wake; a false "dead" costs a redundant watch, not a wrong screen.
    """
    lp = lock_path(key, home)
    if not lp.is_file():
        return False
    fd = -1
    try:
        fd = os.open(str(lp), os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError) as exc:
        return exc.errno in (errno.EACCES, errno.EAGAIN)
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        return False
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def atomic_write(path: Path, text: str) -> bool:
    """Replace ``path`` with ``text`` via a sibling temp file + ``os.replace``.

    Readers see either the old file or the new one: this is what makes a
    half-written ``layout.json`` impossible and what lets ``follow`` treat the
    session directory as a change signal. Returns False instead of raising.

    Not a transaction by itself: the temp name is fixed, so concurrent writers
    must hold the layout lock (``update_layout`` does) or they can clobber each
    other's temp file. No fsync: a crash can lose the last write, but can never
    leave a truncated document.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def read_layout(key: str, home: Optional[Path] = None) -> Dict:
    """The layout document, or ``{}`` when it is missing or unreadable.

    ``{}`` is "nothing recorded", not "nothing exists": callers must treat every
    key as optional (they do — every read is ``.get``). Callers that intend to
    write use ``update_layout`` so this read is not the one that gets written
    back minutes later.
    """
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

    Degraded path: if the lock file cannot be opened the body still runs,
    unlocked. That keeps a read-only or full-disk home working for commands
    that only read; it also means last-writer-wins is back for that one update.
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
    """Read, apply ``mutate``, write, all under the layout lock.

    ``mutate`` receives the current document and may change it in place, return
    a replacement dict, or return anything else (``None``) to mean "I mutated
    it". The write happens unconditionally, so it stamps ``updated`` even when
    nothing changed; callers that would rewrite the same document every pass
    compare before/after themselves (``reconcile_tabs`` does).

    Returns the document as written, which is what the caller should continue
    to reason about: it may contain changes made by another process while this
    one was working.
    """
    with _layout_lock(key, home):
        layout = read_layout(key, home)
        result = mutate(layout)
        if not isinstance(result, dict):
            result = layout
        write_layout(key, result, home)
        return result


def write_layout(key: str, layout: Dict, home: Optional[Path] = None) -> bool:
    """Serialise and atomically replace the document, stamping ``updated``.

    Low-level escape hatch for tests and the initial write; live code goes
    through ``update_layout`` so it cannot revert a concurrent change.
    """
    layout["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return atomic_write(layout_path(key, home),
                        json.dumps(layout, indent=2, sort_keys=True))


# --------------------------------------------------------------------------- kqueue

class WatchSet:
    """kqueue wrapper over vnodes. `sync(paths)` reconciles the watch set.

    One descriptor per watched path, held for as long as it is wanted, plus an
    optional wake pipe for a signal. Three properties matter:

    * **identity is the inode**, not the path. A file replaced while watched
      (``atomic_write``, log rotation) keeps its old descriptor on an unlinked
      inode and would fire nothing ever again; ``sync`` re-opens on a
      ``(st_dev, st_ino)`` change.
    * **the set is capped** (``_MAX_WATCH_DIRS``/``_MAX_WATCH_FILES``) because
      the process must stay under its ``RLIMIT_NOFILE`` soft limit (often 256
      under launchd on macOS, higher from a login shell — environment-dependent)
      and leave room for the descriptors it is not watching. The cap keeps
      the *caller-priority prefix*, never an arbitrary subset.
    * **events are not queued per path**: ``wait`` returns a count and records
      the ids that fired in ``fired_fds``; ``sync`` re-arms everything with
      ``KQ_EV_CLEAR``. A pass therefore knows which files changed, not in what
      order, which is all the throttle needs.

    ``close()`` releases every descriptor including the wake pipe. It does not
    unregister the wake fd with the interpreter (``signal.set_wakeup_fd``);
    that is fine only for a process that closes the set on its way out, which
    ``follow`` does.
    """

    def __init__(self) -> None:
        self.kq = select.kqueue()
        self.dirs: Dict[str, int] = {}
        self.files: Dict[str, int] = {}
        self._dir_inos: Dict[str, Tuple[int, int]] = {}
        self._file_inos: Dict[str, Tuple[int, int]] = {}
        self.signal_woke: bool = False
        self.fired_fds: set = set()

    def close(self) -> None:
        for fd in list(self.dirs.values()) + list(self.files.values()):
            try:
                os.close(fd)
            except OSError:
                pass
        self.dirs.clear()
        self.files.clear()
        self._dir_inos.clear()
        self._file_inos.clear()
        for fd in getattr(self, "_wake_fds", ()):
            try:
                os.close(fd)
            except OSError:
                pass
        self._wake_fds = ()
        self._wake_fd = None
        try:
            self.kq.close()
        except OSError:
            pass

    def _add(self, fd: int) -> None:
        """Arm a vnode filter, edge-triggered with auto-clear.

        ``KQ_EV_CLEAR`` resets the state after delivery, so one append yields
        one wake rather than a permanently-fired descriptor. A failure is
        ignored: a path that cannot be watched degrades to the timer wake.
        """
        try:
            self.kq.control(
                [select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                               flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                               fflags=_VNODE_FLAGS)],
                0, 0,
            )
        except OSError:
            pass

    @staticmethod
    def _path_ino(path: str) -> Optional[Tuple[int, int]]:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    def sync(self, dirs: Sequence[Path], files: Sequence[Path]) -> None:
        """Make the watched set exactly ``dirs`` + ``files``, in priority order.

        Order is preserved (``dict.fromkeys``) and load-bearing: the caps below
        are applied by skipping the tail, so the caller puts ``state.db``,
        ``state.db-wal`` and ``reviews.json`` at the head of ``files`` and they
        are the ones that survive a constrained process.

        Removal happens first and for two reasons: the path is no longer wanted,
        or its inode changed (stat failure counts as changed). Then additions
        fill up to the caps. A directory that cannot be opened (deleted between
        listing and sync) is skipped silently; the next listing brings its
        replacement.
        """
        # Preserve caller priority order (dict.fromkeys) so critical singleton
        # files at the head of `files` (state.db, state.db-wal, reviews.json)
        # are never dropped when len(files) exceeds _MAX_WATCH_FILES.
        want_dirs = list(dict.fromkeys(str(p) for p in dirs))
        want_files = list(dict.fromkeys(str(p) for p in files))
        want_dir_set = set(want_dirs)
        want_file_set = set(want_files)

        for path in list(self.dirs):
            cur_ino = self._path_ino(path) if path in want_dir_set else None
            if path not in want_dir_set or cur_ino is None or cur_ino != self._dir_inos.get(path):
                self._dir_inos.pop(path, None)
                try:
                    os.close(self.dirs.pop(path))
                except OSError:
                    pass
        for path in list(self.files):
            cur_ino = self._path_ino(path) if path in want_file_set else None
            if path not in want_file_set or cur_ino is None or cur_ino != self._file_inos.get(path):
                self._file_inos.pop(path, None)
                try:
                    os.close(self.files.pop(path))
                except OSError:
                    pass

        for path in want_dirs:
            if path in self.dirs or len(self.dirs) >= _MAX_WATCH_DIRS:
                continue
            try:
                fd = os.open(path, os.O_RDONLY)
                st = os.fstat(fd)
            except OSError:
                continue
            self.dirs[path] = fd
            self._dir_inos[path] = (st.st_dev, st.st_ino)
            self._add(fd)
        for path in want_files:
            if path in self.files or len(self.files) >= _MAX_WATCH_FILES:
                continue
            try:
                fd = os.open(path, os.O_RDONLY)
                st = os.fstat(fd)
            except OSError:
                continue
            self.files[path] = fd
            self._file_inos[path] = (st.st_dev, st.st_ino)
            self._add(fd)

    def wake_on_signal(self, signum: int) -> None:
        """Make ``wait`` return when ``signum`` arrives (a pane resize).

        The handler only writes a byte to a pipe that the kqueue watches, so
        nothing is raised into whatever the process happens to be doing.

        Any failure leaves ``_wake_fd`` None and the loop falls back to timer
        wakes; a resize then arrives at the next pass instead of instantly.
        """
        import signal
        try:
            read_fd, write_fd = os.pipe()
            os.set_blocking(read_fd, False)
            os.set_blocking(write_fd, False)
            signal.set_wakeup_fd(write_fd, warn_on_full_buffer=False)
            signal.signal(signum, lambda *_: None)
            self.kq.control([select.kevent(read_fd, filter=select.KQ_FILTER_READ,
                                           flags=select.KQ_EV_ADD)], 0, 0)
            self._wake_fd = read_fd
            self._wake_fds = (read_fd, write_fd)
        except (OSError, ValueError, AttributeError):
            self._wake_fd = None

    def _drain_wake(self) -> bool:
        fd = getattr(self, "_wake_fd", None)
        if fd is None:
            return False
        got = False
        try:
            while os.read(fd, 512):
                got = True
        except OSError:
            pass
        return got

    def clear_fired(self) -> None:
        self.fired_fds.clear()

    def wait(self, timeout: Optional[float]) -> int:
        """Wait up to ``timeout`` for vnode/signal events; return how many fired.

        Records each fired descriptor in ``fired_fds`` (the coordinator uses it
        to tell "only the database changed" from "a delegation changed").
        ``signal_woke`` says a resize arrived, which the follower treats as
        "discard the last frame and redraw". ``EINTR`` is a normal return of 0,
        not an error: a signal that is not a resize must not kill the loop.
        """
        self.signal_woke = False
        try:
            events = self.kq.control(None, 64, timeout)
        except OSError as exc:
            if exc.errno == errno.EINTR:
                self.signal_woke = self._drain_wake()
                return 0
            return 0
        for ev in events:
            self.fired_fds.add(ev.ident)
        self.signal_woke = self._drain_wake()
        return len(events)

    def drain(self, quiet_for: float = _DEBOUNCE_S, max_wait: float = 0.35) -> None:
        """Coalesce a short burst of events into one render.

        One append can raise two events (``NOTE_EXTEND`` on the file, plus the
        directory's ``NOTE_WRITE`` if the file was created), and a task start
        writes a header and then its first event. Waiting for a quiet window
        turns those into one pass without delaying by more than ``max_wait`` —
        a busy producer must not be able to defer the redraw forever.
        """
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            if not self.wait(quiet_for):
                return


def collect_paths(
    home: Optional[Path] = None, *, include_db: bool = True
) -> Tuple[List[Path], List[Path]]:
    """Directories and files whose changes mean "state changed".

    Returned ``files`` are ordered by priority so ``WatchSet._MAX_WATCH_FILES``
    always retains ``state.db``/``state.db-wal``, ``reviews.json``, manifests,
    and active transcripts. Transcripts of delegations that settled more than 8
    delegations ago are not opened in kqueue: they are immutable once settled,
    and opening 150+ historical log files per process wastes descriptors.

    Composition, in the order it is returned:

    * ``dirs``: the Hermes home (only for its own file creation, when watching
      the DB), the hermes-tmux state dir (``reviews.json`` appears there), the
      nearest existing ancestors of ``live/`` (a brand-new profile has no live
      root yet), the live root, every live delegation dir, and a 40-dir
      name-order sample of the rest (the whole set capped at ``_MAX_WATCH_DIRS``);
    * ``files``: ``state.db``, ``state.db-wal``, ``reviews.json``, then every
      manifest of the selected dirs, then the logs of live delegations, then the
      logs of up to 8 completed delegations from that same sample.

    The last group is not redundant: Hermes can flush a final line *after* the
    manifest flips, and that late write is what makes a finished worker's last
    output appear without waiting for the slow wake. Eight is margin over the
    overview's six recent rows; older transcripts really are immutable. Because
    the sample is ordered by name, those 8 are not "the most recent" in time —
    they are the first 8 settled delegations the sample happens to contain.

    Delegation dirs are chosen by "live, or one of the 40 lexicographically
    greatest names". Directory names are random ids, so that is a stable sample,
    not the newest 40 — it bounds descriptor use, and anything live is kept
    regardless, so the cost of the imprecision is a late event on a finished
    delegation, never a missed worker.
    """
    from .state import _manifest_has_live_task, hermes_home, live_root, read_manifest
    base = hermes_home(home)
    root = live_root(home)
    dirs: List[Path] = []
    files: List[Path] = []

    if include_db:
        if base.is_dir():
            dirs.append(base)
        db = base / "state.db"
        for suffix in ("", "-wal"):
            candidate = Path(str(db) + suffix)
            if candidate.exists():
                files.append(candidate)

    reviews = base / "cache" / STATE_DIR_NAME
    if reviews.is_dir():
        dirs.append(reviews)
        candidate = reviews / "reviews.json"
        if candidate.exists():
            files.append(candidate)

    for ancestor in (root.parent.parent, root.parent):
        if ancestor.is_dir() and not root.is_dir():
            dirs.append(ancestor)

    manifests: List[Path] = []
    active_logs: List[Path] = []
    recent_logs: List[Path] = []
    if root.is_dir():
        dirs.append(root)
        try:
            all_children = [c for c in root.iterdir() if c.is_dir()]
        except OSError:
            all_children = []
        # Keep any live delegation dir even if >40 newer completed dirs exist,
        # plus 40 by descending name. Names are random ids, so this is a stable
        # sample rather than a recency order; liveness is what is actually
        # promised here.
        all_children.sort(key=lambda p: p.name, reverse=True)
        selected: List[Path] = []
        for child in all_children:
            mp = child / "manifest.json"
            is_live = (not mp.is_file()) or _manifest_has_live_task(read_manifest(mp))
            if is_live or len(selected) < 40:
                selected.append(child)
        completed_with_logs = 0
        for child in selected:
            dirs.append(child)
            mp = child / "manifest.json"
            is_live = True
            if mp.is_file():
                manifests.append(mp)
                is_live = _manifest_has_live_task(read_manifest(mp))
            # Watch logs of all live delegations plus up to 8 completed ones from
            # this sample, so a late buffered flush after completion still
            # triggers an immediate coordinator pass, without holding FDs open on
            # dozens of older historical delegations.
            watch_logs = is_live or (include_db and completed_with_logs < 8)
            if not is_live and watch_logs:
                completed_with_logs += 1
            if watch_logs:
                try:
                    for entry in child.iterdir():
                        if entry.suffix == ".log":
                            (active_logs if is_live else recent_logs).append(entry)
                except OSError:
                    continue
    files.extend(manifests)
    files.extend(active_logs)
    files.extend(recent_logs)
    return dirs, files


# --------------------------------------------------------------------------- tabs

def _task_index(worker) -> int:
    """``task_index`` as an int, 0 when absent or unparsable (sort key only)."""
    try:
        return int(getattr(worker, "task_index", 0) or 0)
    except (TypeError, ValueError):
        return 0


def worker_order(workers: Sequence) -> List:
    """Chronological: oldest delegation first, task index ascending within one.

    Tabs are appended in this order, so the tab bar reads left to right in the
    order work was dispatched and a later worker never lands before an earlier
    one. Deliberately independent of run state: a worker that finishes keeps
    its place.
    """
    ordered = [w for w in workers if getattr(w, "key", "")]
    ordered.sort(key=lambda w: (str(getattr(w, "delegation_started", "") or ""),
                                str(getattr(w, "delegation_id", "") or ""),
                                _task_index(w)))
    return ordered


def _log_path(worker) -> Optional[str]:
    """The transcript a pane may tail, if the reader published one.

    ``log_path`` is the public field. ``_log_path`` is a legacy private spelling
    still honoured because some callers build ad-hoc worker objects by hand; a
    real ``Worker`` always has ``log_path`` and never the underscore.
    """
    return getattr(worker, "log_path", None) or getattr(worker, "_log_path", None)


def _pane_env() -> Dict[str, str]:
    """Env for panes this process creates (forked from the server, not from us).

    Indirection so tests can replace it wholesale; the values come from
    ``hermes_tmux.env.pane_env`` and must carry ``HERMES_HOME``/``PYTHONPATH``,
    or the new pane reads a different profile.
    """
    from .env import pane_env
    return pane_env()


def tail_command(log_path: str) -> str:
    """Pane command for one transcript.

    The styled follower by default; plain ``tail -F`` when the reader asked for
    raw output with ``HERMES_TMUX_TRANSCRIPT=raw``. There is no third fallback:
    if the follower cannot be located the pane shows its import error, because a
    silent downgrade to a different renderer is worse than a visible failure
    (the reader would not know which one produced the text). Either way the pane
    shows the file Hermes wrote and nothing else.

    ``exec`` makes the follower the pane's foreground process, which is one of
    the provenance proofs ``_killable`` accepts; ``cd`` puts the package on the
    import path without relying on the pane's inherited ``PYTHONPATH``.
    """
    if (os.environ.get("HERMES_TMUX_TRANSCRIPT") or "").strip().lower() == "raw":
        return "exec tail -n +1 -F %s" % shlex.quote(log_path)
    from .env import python, repo_root
    return "cd %s && exec %s -m hermes_tmux tail %s" % (
        shlex.quote(str(repo_root())), shlex.quote(python()), shlex.quote(log_path))


def _kind(worker, now=None) -> str:
    """run / stall / done / fail / other for one worker, at ``now``.

    ``stall`` is derived here from transcript age and nowhere else; it is a
    display label, not a state the reader ever stored (see
    ``render.STALL_AFTER_S``).
    """
    from datetime import datetime
    from . import labels
    from .render import _state_label
    return labels.state_kind(_state_label(worker, now or datetime.now()))


def tab_glyph(kind: str, ascii_mode: bool = False) -> str:
    """Coloured state glyph for the status line, as a tmux style string.

    ``push-default`` makes the tab's own style (the user's current-window
    highlight) the one ``default`` returns to, so colouring one glyph does not
    flatten the rest of the tab.
    """
    from . import labels
    return "#[push-default]#[fg=colour%d]%s#[default]#[pop-default]" % (
        labels.colour(kind), labels.glyph(kind, ascii_mode))


def pane_header(worker, kind: str, ascii_mode: bool = False) -> str:
    """Pane border text: state, the task in the parent's words, then details.

    A pane following a finished transcript looks exactly like one following a
    running transcript, so the header is what says whether the worker is still
    executing. It carries no clock: a header that ticks redraws the border
    every second for information the overview already shows.

    The text is a tmux conditional on the pane's own width with three
    variants, so a half-screen or quarter-screen client gets a shorter header
    from tmux itself and nothing here has to notice a resize.
    """
    from . import labels
    from .render import _ran_for
    sep = " | " if ascii_mode else " \u00b7 "
    ellipsis = "..." if ascii_mode else "\u2026"
    model = labels.model_short(getattr(worker, "model", None))
    timing = ""
    started = str(getattr(worker, "started", "") or "")
    reason = str(getattr(worker, "exit_reason", "") or "")
    if kind in ("run", "stall"):
        if len(started) >= 16:
            timing = "started %s" % started[11:16]
    else:
        ran = _ran_for(worker)
        timing = labels.duration(ran) if ran is not None else ""
        if reason and reason not in ("completed", "done", "success"):
            # How the run ended, in Hermes' own word. It says nothing about
            # whether the work is good; the transcript below does.
            timing = (timing + sep if timing else "") + reason
    tools = getattr(worker, "tool_count", 0) or 0
    key = "%s#%s" % (str(getattr(worker, "delegation_id", "?"))[-8:],
                     getattr(worker, "task_index", "?"))
    title = labels.title(getattr(worker, "goal", ""))

    def variant(word: bool, width: int, parts):
        """(tmux string, visible width) for one header variant."""
        meta = sep.join(part for part in parts if part)
        shown = labels.clip(title, width, ellipsis)
        state_text = labels.glyph(kind, ascii_mode) + ((" " + labels.word(kind)) if word else "")
        visible = (1 + labels.cell_width(state_text) + 2 + labels.cell_width(shown)
                   + ((2 + labels.cell_width(meta)) if meta else 0) + 1)
        # Explicit styles throughout: inside a border, `default` is the border's
        # own style, which is deliberately dim. `terminal` is the terminal's
        # foreground, so the title reads on light and dark themes alike;
        # details are a mid grey that works on both.
        text = " #[fg=colour%d#,bold]%s  #[fg=terminal#,bold]%s%s#[default] " % (
            labels.colour(kind), state_text, labels.tmux_escape(shown),
            ("  #[fg=colour246#,nobold]" + labels.tmux_escape(meta)) if meta else "")
        return text, visible

    variants = [
        variant(True, 96, [model, timing,
                           "%d tool%s" % (tools, "" if tools == 1 else "s") if tools else "",
                           key]),
        variant(True, 60, [model, timing]),
        variant(True, 46, [timing]),
    ]
    # Each variant is used only where it fits whole (its own width plus the
    # border's corner cells); below the last one, state glyph and title alone.
    out = variant(False, 30, [])[0]
    for text, visible in reversed(variants):
        out = "#{?#{e|>=:#{pane_width},%d},%s,%s}" % (visible + 4, text, out)
    return out


#: Room one worker tab can have on this client's status line: the client's
#: width less the session badge and clock (22) and two short windows that are
#: not worker tabs (the agent's own window and the overview, 26), shared
#: between the rest. With two windows or fewer the tab has the line to itself.
_TAB_ROOM = ("#{?#{e|>:#{session_windows},2},"
             "#{e|/:#{e|-:#{client_width},48},#{e|-:#{session_windows},2}},"
             "#{e|-:#{client_width},22}}")


def _fitted_name(ascii_mode: bool = False) -> str:
    """``#W`` truncated to what the status line of THIS client can afford.

    The window keeps one stable name; how much of it shows is decided by tmux
    per client when it draws the status line, so two clients of different
    widths on one session each get a tab bar that fits, and resizing a
    terminal renames nothing. The ladder is coarse on purpose: names change
    length at a few widths, not with every column.
    """
    mark = "..." if ascii_mode else "\u2026"

    def cut(width: int) -> str:
        return "#{=/%d/%s:window_name}" % (width, mark)

    # Below the last rung there is no room for a name at all: the tab is its
    # number and state glyph, which is still a click target and still says
    # whether the worker is running.
    out = ""
    for share, name in ((11, cut(5)), (14, cut(8)), (18, cut(12)), (24, cut(18)), (34, "#W")):
        out = "#{?#{e|>=:%s,%d},%s,%s}" % (_TAB_ROOM, share, name, out)
    return out


def _tab_format(option: str, ascii_mode: bool = False, worker: bool = True) -> str:
    """The user's own tab format with our state glyph placed before the name.

    Derived from the global option instead of replaced, so our tabs follow the
    user's theme (index, flags, current-window highlight). A format with no
    ``#W`` is left alone: there is no name to decorate.

    Returns '' when the user's format has no name slot or already carries the
    glyph, which the caller reads as "set no window option" — inheriting the
    user's format verbatim is the correct result in both cases. The value is a
    snapshot taken when the window is decorated; later theme edits by the user
    reach new tabs, not existing ones.
    """
    rc, out = tmuxio.run("show-options", "-gwv", option)
    fmt = out.rstrip("\n") if rc == 0 else ""
    if "#W" not in fmt or "@hx_glyph" in fmt:
        return ""
    # The overview is one short name, so it only yields on a very narrow line.
    name = _fitted_name(ascii_mode) if worker else "#{?#{e|>=:#{client_width},72},#W,}"
    return fmt.replace("#W", "#{E:@hx_glyph} " + name, 1)


def decorate_window(window_id: str, header: bool = True,
                    ascii_mode: bool = False,
                    home: Optional[Path] = None) -> None:
    """Window-scoped presentation for a window we created (never global).

    ``header=True`` is a worker tab: per-pane border header, and the close mark
    on the in-view format only. ``header=False`` is the overview: badge in the
    tab, no border line above a pane that draws its own heading.

    Also stamps ``@hx_home`` so a click handler running under a different
    ``HERMES_HOME`` can find the layout that owns this window. Every option is
    written with ``-w``/``-p`` on this window or pane; a failure is ignored (the
    tab simply keeps the user's default look).
    """
    from . import closemark
    from .state import hermes_home
    tmuxio.window_option(window_id, "@hx_home", str(hermes_home(home)))
    for option in ("window-status-format", "window-status-current-format"):
        fmt = _tab_format(option, ascii_mode, worker=header)
        # Worker tabs only, and only the tab in view: one quiet mark, and a
        # click aimed at another tab's name can never close it instead.
        if fmt and header and option == "window-status-current-format" and closemark.enabled():
            fmt = closemark.with_mark(fmt, window_id, ascii_mode)
        if fmt:
            tmuxio.window_option(window_id, option, fmt)
    if header:
        tmuxio.window_option(window_id, "pane-border-status", "top")
        tmuxio.window_option(window_id, "pane-border-format", "#{E:@hx_head}")
        tmuxio.window_option(window_id, "pane-active-border-style", "fg=colour238")
        tmuxio.window_option(window_id, "pane-border-style", "fg=colour238")
    else:
        tmuxio.window_option(window_id, "pane-border-status", "off")


def _set_if_changed(record: Dict, field: str, value: str, apply) -> bool:
    """Write ``value`` through ``apply`` only when it differs from ``record``.

    Two jobs: an idle workspace issues no tmux subprocess for decoration, and a
    failed write is retried next pass instead of being memoized as done (the
    record only advances when ``apply`` returns true). The cached value lives in
    the tab record so the next pass can compare without asking tmux what it
    currently shows.
    """
    if record.get(field) == value:
        return False
    if apply(value):
        record[field] = value
        return True
    return False


def _active_window(session_id: str) -> str:
    """Window the reader is looking at, '' when nobody is attached.

    ``#{session_attached}`` is a client count; 0 means the session has no
    viewer, so nothing is "in view" and no tab is protected from retiring. A
    malformed answer is treated the same way: the conservative mistake here is
    to let a tab go, not to keep eight tabs forever.
    """
    out = tmuxio.format_of(session_id, "#{session_attached} #{window_id}")
    parts = out.split()
    if len(parts) != 2 or parts[0] in ("", "0"):
        return ""
    return parts[1]


def _windows_in_view(session_id: str) -> set:
    """Windows some reader is looking at: one per attached session of the group.

    Sessions of one group share the windows but each has its own current
    window, so a tab can be on screen in a second terminal while the session
    the workspace is anchored to shows something else.

    Empty ``session_id`` yields the empty set (``group_sessions('')`` is ``[]``
    and the fallback list is ``['']``, whose active window is ''). A layout with
    no recorded session therefore treats nothing as in view — every finished tab
    becomes retirable, and a failure's attention clock starts at once.
    """
    views = {_active_window(sid) for sid in (tmuxio.group_sessions(session_id) or [session_id])}
    views.discard("")
    return views


def _settled_at(worker) -> Optional[float]:
    """Epoch seconds of the worker's last known activity, or None.

    Used only to decide whether a finished worker is recent enough to deserve a
    tab. None means "no timestamp I can trust" (a log time with no date), which
    the caller treats as history rather than opening a tab for it.
    """
    from .render import _activity_at
    activity = _activity_at(worker)
    return activity.timestamp() if activity is not None else None


def reconcile_tabs(key: str, workers: Sequence, home: Optional[Path] = None,
                   now: Optional[float] = None) -> Dict:
    """Give live work a tab each, and let finished work leave.

    Rules, all of them about not surprising the reader:

    * one worker, one window, one pane, named after the task. A tab is created
      once, appended at the end, and never renamed or moved; only its state
      glyph and pane header change;
    * a worker gets a tab while it runs. Work that had already finished when
      the workspace came up is history and gets no tab;
    * a finished worker keeps its tab for ``LINGER_S`` after it settled or was
      last looked at, and is never retired while it is the window in view. A
      failure keeps its tab until someone has looked at it (or it goes stale);
    * a tab the reader closed stays closed. ``hermes-tmux open`` brings one back;
    * at ``MAX_TABS`` a running worker takes the place of the oldest finished
      tab; running tabs are never closed to make room;
    * only panes proven to be ours are closed. A tab the reader split keeps
      their pane and is forgotten by the layout instead of being torn down.

    Mechanics. The pass reads ``layout.json`` once, snapshots ``tabs``/
    ``dismissed`` (``tabs0``/``dismissed0``), then spends as long as tmux takes
    creating or closing windows. The write is therefore a merge of this pass's
    *delta* onto whatever is on disk at write time (``_merge``), not a rewrite
    of the snapshot. ``before`` is the serialised snapshot used to skip the write
    entirely when the pass changed nothing.

    Two pieces of bookkeeping are easy to miss:

    * ``dismissed`` is permanent while the worker is still listed. It is how a
      closed tab, a retired tab, and a pane the reader moved elsewhere stay
      closed; ``open`` removes an entry, and a pass drops one only once the
      worker is no longer in the reader's list.
    * ``unknown`` marks tabs whose window read returned nothing. tmux not
      answering is not evidence that a pane moved, so those tabs are left
      exactly as they are for the pass — not decorated, not retired, not
      forgotten.

    Cost: roughly seven tmux calls per existing tab per pass (existence and
    ownership, each asked twice, plus the window read and the window ownership
    check), plus one ``new-window`` per tab created. The caller throttles passes
    to 1 Hz, which is what keeps that affordable.
    """
    from . import labels
    from .render import ATTENTION_TTL_S

    now = time.time() if now is None else now
    linger = linger_s()
    layout = read_layout(key, home)
    session_id = layout.get("session_id") or tmuxio.current_session_id() or ""
    created = float(layout.get("created") or now)
    ascii_mode = bool(layout.get("ascii"))
    # Deep-enough copies: the snapshot is what the merge compares against, so it
    # must not alias the working maps.
    tabs: Dict[str, Dict] = {str(k): dict(v) for k, v in (layout.get("tabs") or {}).items()
                             if isinstance(v, dict)}
    dismissed: Dict[str, float] = dict(layout.get("dismissed") or {})
    before = json.dumps([tabs, dismissed], sort_keys=True)
    tabs0 = json.loads(json.dumps(tabs))
    dismissed0 = dict(dismissed)
    by_key = {str(getattr(w, "key", "")): w for w in workers if getattr(w, "key", "")}

    # 1. A tab whose pane is gone was closed by the reader (or died). Either
    #    way it is not reopened behind their back. Pane ids are unique per
    #    server only, so ownership is checked, not assumed.
    #    A pane the reader moved into another window (join-pane) is theirs to
    #    place: the tab is forgotten, the pane is left running, and nothing is
    #    ever written to the window it now lives in.
    unknown = set()
    for wkey in list(tabs):
        pane_id = tabs[wkey].get("pane") or ""
        window_id = tabs[wkey].get("window") or ""
        # A "no" forgets a tab for good, so it is asked twice: one failed tmux
        # call must not hide live work.
        if not (_twice(tmuxio.pane_exists, pane_id) and _twice(tmuxio.owns_pane, pane_id)):
            tabs.pop(wkey)
            dismissed[wkey] = now
            continue
        live = tmuxio.format_of(pane_id, "#{window_id}")
        if not live:
            # tmux did not answer. That is not evidence the pane moved: leave
            # the tab exactly as it is for this pass and touch nothing.
            unknown.add(wkey)
        elif live != window_id or not _twice(tmuxio.owns_window, window_id):
            tabs.pop(wkey)
            dismissed[wkey] = now

    # 2. Looking at a tab acknowledges it and restarts its linger clock.
    active = _windows_in_view(session_id) if tabs else set()
    for wkey, tab in tabs.items():
        if tab.get("window") in active:
            tab["seen"] = now

    # 3. Note when each tab's worker settled (coordinator clock, so linger is
    #    immune to transcript timestamps that carry no date).
    kinds: Dict[str, str] = {}
    for wkey, tab in tabs.items():
        worker = by_key.get(wkey)
        # A worker this pass does not know about — dropped by the reader's row
        # limit, or the reader returned nothing at all — has no kind, and
        # "other" is not run/stall, so the tab is treated as settled and starts
        # ageing out. A transient empty list therefore starts every live tab's
        # linger clock; the tab is only closed once that clock runs out.
        kind = _kind(worker) if worker is not None else "other"
        kinds[wkey] = kind
        if kind in ("run", "stall"):
            tab.pop("settled", None)
        elif "settled" not in tab:
            tab["settled"] = now

    def _retire(wkey: str) -> bool:
        tab = tabs[wkey]
        closed, _why = tmuxio.close_pane(tab.get("pane") or "")
        if closed or not tmuxio.pane_exists(tab.get("pane") or ""):
            tabs.pop(wkey)
            dismissed[wkey] = now
            return True
        return False

    def _retirable(wkey: str) -> bool:
        tab = tabs[wkey]
        return ("settled" in tab and not tab.get("pinned")
                and tab.get("window") not in active and wkey not in unknown)

    # 4. Let finished work leave.
    for wkey in list(tabs):
        if not _retirable(wkey):
            continue
        tab = tabs[wkey]
        # A failure nobody looked at is an alert, and an alert that never clears
        # is noise. It keeps its tab until the attention TTL expires, then goes
        # the same way as any other finished work.
        if kinds.get(wkey) == "fail" and "seen" not in tab:
            if now - tab["settled"] > ATTENTION_TTL_S:
                _retire(wkey)
            continue
        if now - max(tab["settled"], tab.get("seen", 0.0)) > linger:
            _retire(wkey)

    # 5. Open tabs for new work, oldest first.
    #    `dismissed` is consulted here and nowhere else: it is the whole rule
    #    that a tab the reader closed does not come back.
    for worker in worker_order(workers):
        wkey = str(getattr(worker, "key", ""))
        log_path = _log_path(worker)
        if not log_path or wkey in tabs or wkey in dismissed:
            continue
        kind = _kind(worker)
        running = kind in ("run", "stall")
        settled = None
        if not running:
            settled = _settled_at(worker)
            # Finished before this workspace existed, or long enough ago that
            # its linger would already be over: history, not a tab.
            if settled is None or settled < created or now - settled > linger:
                continue
        if len(tabs) >= MAX_TABS:
            if not running:
                continue
            victims = sorted((k for k in tabs if _retirable(k)),
                             key=lambda k: tabs[k]["settled"])
            if not victims or not _retire(victims[0]):
                continue              # every tab is live work: nothing to evict
        siblings = [getattr(w, "goal", "") for w in workers
                    if getattr(w, "delegation_id", None) == getattr(worker, "delegation_id", "")
                    and getattr(w, "key", "") != wkey]
        name = tmuxio.free_window_name(
            session_id, labels.tab_label(getattr(worker, "goal", ""), siblings,
                                         ascii_mode=ascii_mode))
        window_id = tmuxio.new_window(session_id, name, cwd=str(Path.home()),
                                      command=tail_command(log_path), env=_pane_env())
        if not window_id:
            continue
        panes = tmuxio.wait_for_panes(window_id)
        if not panes:
            continue
        decorate_window(window_id, ascii_mode=ascii_mode, home=home)
        tabs[wkey] = {"window": window_id, "pane": panes[0]["pane_id"], "opened": now}
        if settled is not None:
            tabs[wkey]["settled"] = now
        kinds[wkey] = kind

    # 6. State glyph and header. Written only when they change, so an idle
    #    workspace issues no tmux commands for decoration.
    for wkey, tab in tabs.items():
        worker = by_key.get(wkey)
        if worker is None or wkey in unknown:
            continue
        kind = kinds.get(wkey) or _kind(worker)
        _set_if_changed(tab, "glyph", tab_glyph(kind, ascii_mode),
                        lambda value, t=tab: tmuxio.window_option(t["window"], "@hx_glyph", value))
        _set_if_changed(tab, "head", pane_header(worker, kind, ascii_mode),
                        lambda value, t=tab: tmuxio.pane_option(t["pane"], "@hx_head", value))

    # A dismissal only matters while the worker can still be listed.
    for wkey in list(dismissed):
        if wkey not in by_key:
            dismissed.pop(wkey)

    if json.dumps([tabs, dismissed], sort_keys=True) != before:
        # Only this pass's delta is written. `duplicated` is how a first-tab
        # collision with `open` comes back so the loser can clean up its pane.
        def _apply(current: Dict) -> Dict:
            # This pass read the layout before doing seconds of tmux work.
            # `open` may have published a tab in the meantime, so only what
            # THIS pass changed is written; replacing both maps wholesale
            # would revert that record, and the next pass would then open a
            # second window for the same worker.
            duplicated.extend(_merge(current.setdefault("tabs", {}), tabs0, tabs))
            _merge(current.setdefault("dismissed", {}), dismissed0, dismissed,
                   blocked=current["tabs"])
            return current
        duplicated: List[str] = []
        layout = update_layout(key, _apply, home)
        # `open` created the same worker's first tab while this pass was
        # creating one too. The reader's tab is the one recorded; the pane this
        # pass made is closed so the worker is not shown twice.
        for wkey in duplicated:
            tmuxio.close_pane(tabs[wkey].get("pane") or "")
    return layout


def _twice(check, target: str) -> bool:
    """Ask a negative-answerable question twice; true if either answer is true.

    A "no" here can forget a tab permanently (the worker is written into
    ``dismissed``), so one failed tmux call must not be allowed to hide live
    work. Two immediate calls turn a lost subprocess into a retry, not a
    decision.

    It is a mitigation, not a guarantee: two failures in a row still answer
    "no". The remaining exposure is a tmux that is down for the duration of both
    calls, and that condition also fails the session liveness check, which ends
    the coordinator instead of letting it act on the answer.
    """
    return bool(check(target)) or bool(check(target))


def _merge(current: Dict, before: Dict, after: Dict,
           blocked: Optional[Dict] = None) -> List[str]:
    """Apply the difference ``before -> after`` to ``current`` in place.

    A key this pass added or changed is written; a key it removed is removed
    only if nobody else touched it since ``before``; a key someone else added
    is left alone. ``blocked`` names keys that must not be added (a worker
    that has a live tab again is not dismissed).

    Returns the keys this pass added that someone else had already added: the
    other record stands, and the caller owns cleaning up what it built.

    The four cases, explicitly:

    * unchanged (``before[k] == after[k]``) — nothing to do, even if ``current``
      has moved on; someone else's newer value wins.
    * changed by this pass — write it unconditionally. Other writers add or
      remove whole records rather than editing a tab's ``seen``/``settled``/
      glyph, so the overwrite is safe in practice. The interleaving this does
      not protect is a ``close`` landing between the read and the write: the
      record is restored for one pass, then step 1 of the next pass sees the
      dead pane and dismisses it again (the dismissal ``close`` wrote stands).
    * removed by this pass — remove only when ``current`` still holds the value
      this pass saw. If another process replaced it, that replacement stands
      (``open`` re-recording a pinned tab while the pass was retiring it).
    * added by this pass — write it unless someone else added the same key
      first, in which case the other record wins and the key is reported.
    """
    lost: List[str] = []
    for name, value in after.items():
        if before.get(name) == value:
            continue
        if name not in before and name in current and current[name] != value:
            lost.append(name)
            continue
        if not (blocked and name in blocked and name not in before):
            current[name] = value
    for name, value in before.items():
        if name not in after and current.get(name) == value:
            current.pop(name, None)
    return lost


def seen_keys(layout: Dict) -> List[str]:
    """Workers whose tab has been looked at, or closed by the reader.

    Both facts suppress an alert: an acknowledged failure and a dismissed tab
    are "handled" from the overview's point of view. This is the only bridge
    from layout state into the renderer's attention classification, which is why
    it takes a bare layout rather than reading one itself.
    """
    keys = [k for k, tab in (layout.get("tabs") or {}).items()
            if isinstance(tab, dict) and "seen" in tab]
    return keys + list((layout.get("dismissed") or {}).keys())


def tab_indexes(layout: Dict) -> Dict[str, str]:
    """worker key -> tmux window index of its tab (what the status line shows).

    Queried live rather than recorded, because a window's index changes when
    earlier windows are closed and a stale index in the overview would point at
    somebody else's window. Missing windows are simply absent: the overview then
    shows the worker without a tab number.
    """
    tabs = layout.get("tabs") or {}
    if not tabs:
        return {}
    index_of = {win["window_id"]: win["index"]
                for win in tmuxio.list_windows(layout.get("session_id") or "")}
    return {wkey: index_of[tab.get("window")] for wkey, tab in tabs.items()
            if isinstance(tab, dict) and tab.get("window") in index_of}


def overview_glyph(workers: Sequence, layout: Dict, ascii_mode: bool = False) -> str:
    """Badge on the overview tab: running count, and what needs a look.

    Composed from the same classification the overview body uses, so the tab and
    the pane can never disagree about how many workers need attention. A quiet
    workspace gets a dim ``-``/hollow circle rather than an empty string: a tab
    with no glyph at all looks broken.
    """
    from . import labels
    from . import render as render_mod
    counts = render_mod.summary(render_mod.classify(
        workers, seen=seen_keys(layout), since=layout.get("created")))
    parts = []
    if counts["running"]:
        parts.append("#[fg=colour%d]%s%d" % (labels.colour("run"),
                                             labels.glyph("run", ascii_mode), counts["running"]))
    if counts["attention"]:
        parts.append("#[fg=colour%d]%s%d" % (labels.colour("fail"),
                                             labels.glyph("fail", ascii_mode), counts["attention"]))
    if not parts:
        parts.append("#[fg=colour244]%s" % ("-" if ascii_mode else "\u25cb"))
    return "#[push-default]" + " ".join(parts) + "#[default]#[pop-default]"


def load_view(key: str, home: Optional[Path] = None) -> Tuple[List, str, Dict]:
    """(workers, failure note, layout) for one render.

    The note is non-empty only when the reader itself failed. That distinction
    matters: "state reader failed: ..." is a different, true statement, while an
    empty list alone would be rendered as "no delegations found", which is a
    claim this function cannot make.
    """
    from . import state as state_mod
    note = ""
    try:
        workers = state_mod.load_workers(home)
    except Exception as exc:                      # pragma: no cover - defensive
        workers = []
        # Never present a reader crash as "no delegations": that is a different,
        # wrong claim. Say what failed.
        note = "state reader failed: %s" % (exc,)
    return workers, note, read_layout(key, home)


def compose(key: str, home: Optional[Path] = None, session_label: str = "",
            width: int = 110, height: Optional[int] = None,
            colour: bool = False, *,
            preloaded: Optional[Tuple[Sequence, str, Dict]] = None) -> str:
    """The overview text for this session at the given size.

    ``preloaded`` lets a pass that has just read state share it with the render
    instead of re-reading the manifests (the coordinator does this every pass).
    A renderer exception falls back to an empty-but-labelled overview: showing
    nothing is acceptable, showing a traceback in the reader's pane is not.
    """
    from . import render as render_mod
    from .state import live_root
    workers, note, layout = preloaded if preloaded is not None else load_view(key, home)
    try:
        return render_mod.render(
            workers, width=width, height=height, session_label=session_label,
            live_root=str(live_root(home)), note=note, colour=colour,
            tabs=tab_indexes(layout), seen=seen_keys(layout),
            since=layout.get("created"), ascii_mode=bool(layout.get("ascii")))
    except Exception:
        return render_mod.render_empty(width=width, live_root=str(live_root(home)),
                                       note=note or "overview renderer failed",
                                       colour=colour)


def render_and_publish(key: str, home: Optional[Path] = None,
                       session_label: str = "", *,
                       preloaded: Optional[Tuple[Sequence, str, Dict]] = None) -> str:
    """Write the plain overview to render.txt; the pane redraws when it changes.

    Plain, uncoloured, default width: this file is the *signal*, not the
    picture. Each follower composes its own coloured frame at its own pane size;
    writing the same bytes twice is skipped so the vnode does not wake the pane
    for nothing.
    """
    text = compose(key, home, session_label, preloaded=preloaded)
    current = ""
    try:
        current = render_path(key, home).read_text(encoding="utf-8")
    except OSError:
        pass
    if text != current:
        atomic_write(render_path(key, home), text)
    return text


def run_watch(home: Optional[Path] = None, key: Optional[str] = None) -> int:
    """Main loop of the coordinator. Returns an exit code.

    Startup, in order, each step load-bearing:

    1. **gate** — off means exit 0 before touching anything.
    2. **key** — passed by the caller that built the layout; a ``run-shell``
       child has no ``TMUX_PANE`` and would derive a different key, read an empty
       layout, and reconcile nothing.
    3. **state dir** — create it, or give up (1).
    4. **``flock`` on ``watch.lock``** — this is the single-coordinator
       guarantee, and the lock is held for the process's whole life. A short
       retry absorbs the momentary hold a ``coordinator_alive``/``_lock_held``
       probe takes; after that, refusing the lock means a real coordinator owns
       the workspace and this process exits 0 (success: the job is already being
       done).
    5. **pid file** — written for ``down``, a hint that is only believed after
       ``_pid_is_watcher`` corroborates it.
    6. **SIGTERM → KeyboardInterrupt** — so ``down``'s signal leaves through the
       normal exit path: lock released, pid file removed, exit 0. The
       ``run-shell`` wrapper's exit-0 discipline depends on this.
    7. **owner server pid** — captured once. A tmux server that dies without
       killing its children (crash, SIGKILL) leaves this process alive while a
       new server answers on the same socket; window ids on that server come
       from a different sequence, so continuing would rename or split into
       windows this workspace never created. Identity is the pid, not the path.

    Each pass: reload state (if due) → ``reconcile_tabs`` → restore missing
    permanent aux windows → publish ``render.txt`` → refresh the overview badge
    → wait for events. The pass runs *before* the first wait, because ``up`` has
    just written the layout and any delegation that already existed raised no
    vnode event; waiting first would leave tabs unbuilt for a whole interval.

    Waiting and coalescing: ``watcher.wait`` is given ``_TICK_S`` whenever a tab
    is settled or a worker is running (both need a clock), else
    ``_SESSION_CHECK_S``. A real event is followed by a throttle: sleep out the
    remainder of ``_MIN_RENDER_INTERVAL_S`` in slices, then ``wait(0)`` to absorb
    anything that arrived meanwhile; if the floor has already passed, ``drain``
    coalesces the burst instead.

    The database fast path skips a full reload when *only* the home/state.db/
    state.db-wal descriptors fired, the live root is a real directory, the
    previous full pass is recent enough, and ``async_delegations`` is
    byte-identical. That is what keeps a chatty parent's WAL writes from
    re-reading every transcript. It is bounded by ``last_full_pass_mono`` so a
    continuous stream of unrelated writes cannot starve a real pass forever.

    Exit conditions, all returning 0: another server pid answers the socket, or
    the recorded session no longer resolves (its own id must match — a missing
    target exits 0 on tmux 3.8 and must not read as "alive"). ``KeyboardInterrupt``
    from SIGTERM exits 0 too. The ``finally`` block closes the kqueue, deletes
    the pid file only if it still names this process, and releases the lock.
    """
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
        locked = False
        # Retry briefly so a microsecond `coordinator_alive` / `_lock_held`
        # probe cannot cause a newly launched coordinator to exit at startup.
        for attempt in range(5):
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except (OSError, IOError):
                if attempt < 4:
                    time.sleep(0.02)
        if not locked:
            lock_file.close()
            return 0  # another coordinator genuinely owns this session
    except (OSError, IOError):
        return 0

    pid_path(key, home).write_text(str(os.getpid()), encoding="utf-8")
    # `down` stops this process with SIGTERM. Leaving through the normal exit
    # path releases the lock and pid file, and ends 0.
    import signal

    def _stop(*_args):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _stop)
    except (OSError, ValueError):
        pass
    # The server that owns us. A tmux server that dies without killing its jobs
    # (crash, SIGKILL) leaves this process running while a NEW server answers on
    # the same socket; that server's window ids come from a different sequence,
    # so continuing to reconcile would rename or split into windows we never
    # created. Identity is the server pid, not the socket path.
    owner_pid = tmuxio.server_pid()
    layout = read_layout(key, home)
    session_label = layout.get("session_name") or tmuxio.current_session_name() or ""

    from .state import db_delegations, hermes_home, live_root
    base = hermes_home(home)
    watcher = WatchSet()
    # Pass bookkeeping. `last_pass_mono` is the throttle clock, `last_full_pass_mono`
    # bounds the database fast path, `last_reload_mono` paces a full re-read when
    # nothing fires. `workers`/`note`/`last_db_rows` are reused on a timer wake:
    # the timer exists to age tabs and notice what is in view, not to re-read.
    last_pass_mono = 0.0
    last_full_pass_mono = 0.0
    last_liveness_mono = time.monotonic()
    workers: List = []
    last_db_rows: Optional[Dict] = None
    note = ""
    reload_state = True
    last_reload_mono = time.monotonic()
    dirs: List[Path] = []
    files: List[Path] = []
    badge = ""
    try:
        while True:
            last_pass_mono = time.monotonic()
            last_full_pass_mono = last_pass_mono
            # Reconcile BEFORE blocking. The first pass must run immediately:
            # `up` starts this process after writing the layout, and a delegation
            # that already existed raised no vnode event, so waiting first would
            # leave worker tabs unbuilt for a whole wake interval.
            if reload_state:
                dirs, files = collect_paths(home)
                watcher.sync(dirs, files)
                note = ""
                try:
                    from . import state as state_mod
                    last_db_rows = db_delegations(home)
                    workers = state_mod.load_workers(home)
                except Exception as exc:
                    workers = []
                    note = "state reader failed: %s" % (exc,)
            # A timer wake reuses the loaded records: it exists to notice which
            # tab is in view and to retire finished tabs, not to re-read state.
            layout = reconcile_tabs(key, workers, home)
            # Keep permanent `hx:mail` and (when enabled) `hx:testbench` alive across crashes without stealing focus.
            # Re-verify server ownership before mutating layout/windows so a coordinator racing
            # `kill-server` + server restart never creates windows on the new server or rewrites old layout.json.
            # The assignment into the local document after a successful rebuild is display-only:
            # ensure_aux_window already published the id through update_layout, and writing the
            # whole document here would be the lost-update bug again.
            mail_wid = ((layout.get("aux") or {}).get("mail")) or ""
            mail_sid = layout.get("session_id") or ""
            if mail_sid and (
                not mail_wid
                or not tmuxio.window_exists(mail_wid)
                or not any(p["ours"] for p in tmuxio.list_panes(mail_wid))
            ):
                live_srv = tmuxio.server_pid()
                if owner_pid and live_srv != owner_pid:
                    return 0
                if not tmuxio.session_alive(mail_sid):
                    return 0
                from . import cli as cli_mod
                restored_wid, _ = cli_mod.ensure_aux_window(
                    key, mail_sid, "mail", focus=False,
                    after_window=layout.get("workers_window"), home=home,
                    owner_pid=owner_pid,
                )
                if restored_wid:
                    layout.setdefault("aux", {})["mail"] = restored_wid
                    mail_wid = restored_wid
            from . import cli as cli_mod_tb
            tb_wid = ((layout.get("aux") or {}).get("testbench")) or ""
            if mail_sid and cli_mod_tb.testbench_tab_enabled() and (
                not tb_wid
                or not tmuxio.window_exists(tb_wid)
                or not any(p["ours"] for p in tmuxio.list_panes(tb_wid))
            ):
                live_srv = tmuxio.server_pid()
                if owner_pid and live_srv != owner_pid:
                    return 0
                if not tmuxio.session_alive(mail_sid):
                    return 0
                restored_tb, _ = cli_mod_tb.ensure_aux_window(
                    key, mail_sid, "testbench", focus=False,
                    after_window=mail_wid or layout.get("workers_window"), home=home,
                    owner_pid=owner_pid,
                )
                if restored_tb:
                    layout.setdefault("aux", {})["testbench"] = restored_tb
            render_and_publish(key, home, session_label, preloaded=(workers, note, layout))
            window_id = layout.get("workers_window")
            # Decorating is a write to someone else's window if that id names a
            # foreign window on a new server; check `want != badge` first so an
            # unchanged overview issues zero tmux subprocesses.
            if window_id:
                want = overview_glyph(workers, layout, bool(layout.get("ascii")))
                if want != badge and tmuxio.owns_window(window_id):
                    if tmuxio.window_option(window_id, "@hx_glyph", want):
                        badge = want

            while True:
                watcher.clear_fired()
                pending = any("settled" in tab for tab in (layout.get("tabs") or {}).values()
                              if isinstance(tab, dict))
                running = any(getattr(w, "state", "") == "running" for w in workers)
                fired = watcher.wait(_TICK_S if (pending or running) else _SESSION_CHECK_S)
                if fired:
                    # Enforce the floor rather than trusting the wait: a WAL write
                    # wakes the kqueue immediately, so the sleep is the throttle.
                    rem = _MIN_RENDER_INTERVAL_S - (time.monotonic() - last_pass_mono)
                    if rem > 0:
                        while True:
                            rem = _MIN_RENDER_INTERVAL_S - (time.monotonic() - last_pass_mono)
                            if rem <= 0:
                                break
                            # Short slices, not one long sleep: a SIGTERM must
                            # land promptly, and the remaining time can shrink.
                            time.sleep(min(rem, 0.25))
                        watcher.wait(0)
                    else:
                        watcher.drain()
                    last_pass_mono = time.monotonic()

                    # Fast-path: parent Hermes writes to `messages`/`sessions` in
                    # state.db-wal many times per second while chatting. When ONLY
                    # home/state.db/state.db-wal fds fired (and live_root already
                    # exists as its own watched dir) and `async_delegations` rows
                    # are unchanged, skip full worker reload and tmux subprocesses.
                    db_only_fds = set()
                    for p_str in (str(base), str(base / "state.db"), str(base / "state.db-wal")):
                        if p_str in watcher.dirs:
                            db_only_fds.add(watcher.dirs[p_str])
                        if p_str in watcher.files:
                            db_only_fds.add(watcher.files[p_str])
                    max_skip = _TICK_S if (pending or running) else _SESSION_CHECK_S
                    if (
                        last_db_rows is not None
                        and live_root(home).is_dir()
                        and (time.monotonic() - last_full_pass_mono) < max_skip
                        and watcher.fired_fds
                        and watcher.fired_fds.issubset(db_only_fds)
                        and db_delegations(home) == last_db_rows
                    ):
                        if time.monotonic() - last_liveness_mono >= _TICK_S:
                            last_liveness_mono = time.monotonic()
                            live_pid = tmuxio.server_pid()
                            if owner_pid and live_pid and live_pid != owner_pid:
                                return 0
                            if not tmuxio.session_alive(layout.get("session_id") or ""):
                                return 0
                        # Nothing this workspace owns changed: stay in the wait
                        # loop. `reload_state` is left exactly as it was — only
                        # the paths that actually decide about a pass set it.
                        continue
                    reload_state = True
                else:
                    # Nothing changed on disk. Re-read on the slow cadence only, so
                    # the display-only "no output" label can still advance.
                    reload_state = not (pending or running) or \
                        time.monotonic() - last_reload_mono >= _SESSION_CHECK_S
                last_liveness_mono = time.monotonic()
                live_pid = tmuxio.server_pid()
                if owner_pid and live_pid and live_pid != owner_pid:
                    return 0          # a different server now answers this socket
                if not tmuxio.session_alive(layout.get("session_id") or ""):
                    return 0          # the session this coordinator serves is gone
                if reload_state:
                    last_reload_mono = time.monotonic()
                break
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


# --------------------------------------------------------------------------- follow

def _draw(text: str, pane_id: str, size: Optional[Tuple[int, int]] = None) -> None:
    """Overwrite the pane in place: home, per line erase, no scroll.

    The frame is written from the top with the cursor hidden, so a redraw is a
    replacement rather than an append and the reader's scroll position is never
    touched. A write failure (pane gone, EIO) is swallowed: the loop's next
    pane-existence check is what ends it.
    """
    width, height = size if size is not None else tmuxio.pane_size(pane_id)
    lines = text.splitlines()
    if len(lines) > height:
        lines = lines[:height]
    buf = ["\033[?25l\033[H"]
    for index, line in enumerate(lines):
        buf.append(line)                     # already fitted by the renderer
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
                     pane_id: str = "", key: str = "", *,
                     size: Optional[Tuple[int, int]] = None) -> str:
    """Render the overview at this pane's size, in colour when it can show it.

    The pane is the only consumer that knows its own size; ``render.txt`` stays
    the shared plain copy and doubles as the coordinator's redraw signal.
    """
    from .transcript import colour_enabled
    width, height = (size if size is not None
                     else (tmuxio.pane_size(pane_id) if pane_id else (110, None)))
    return compose(key, home, session_label, width=width, height=height,
                   colour=bool(pane_id) and colour_enabled())


def run_follow(home: Optional[Path] = None, key: Optional[str] = None) -> int:
    """Pane-side display loop: redraw when the live root or layout changes.

    Structure of one iteration, in this order and for these reasons:

    1. **pane exists?** Asked first, every time. tmux does not reliably deliver
       SIGHUP when a server is killed outright, so this check is the only thing
       that stops a follower from outliving its pane (an orphan was observed
       7.5 minutes after its server went).
    2. compose at the pane's current size, and draw only when the text or the
       size changed — a no-op redraw would still cost a full escape sequence.
    3. decide whether to watch the database: not while the coordinator holds
       ``watch.lock``, because it already watches ``state.db-wal`` and rewrites
       ``render.txt``; watching it here too is what made the follower burn CPU on
       every parent chat write.
    4. wait: 2 s while the text says RUNNING, 60 s otherwise. The bound exists so
       a pane degrades (stale but truthful) instead of freezing when nothing
       fires.

    ``SIGWINCH`` clears the last frame so the next draw happens at the new size
    immediately. Exit is 0 on pane disappearance and on ``KeyboardInterrupt``; the
    ``finally`` closes the kqueue and restores the tty (no-echo off). This loop
    installs no SIGTERM handler — only ``run_watch`` converts SIGTERM to
    ``KeyboardInterrupt`` — so a SIGTERM here takes the default disposition (143)
    and skips the tty restore.
    """
    if not enabled():
        print("hermes-tmux: disabled (%s)" % _reason())
        return 0
    pane_id = os.environ.get("TMUX_PANE", "")
    key = key or tmuxio.session_key()
    layout = read_layout(key, home)
    session_label = layout.get("session_name") or tmuxio.current_session_name() or ""

    target = render_path(key, home)
    watcher = WatchSet()
    import signal
    restore = lambda: None
    if pane_id:
        from .transcript import quiet_tty
        restore = quiet_tty()
    # A resized pane is redrawn at its new size at once, not on the next wake.
    watcher.wake_on_signal(signal.SIGWINCH)
    last_drawn: Optional[Tuple[str, int, int]] = None
    last_pass_mono = 0.0
    try:
        while True:
            if pane_id and not tmuxio.pane_exists(pane_id):
                return 0
            last_pass_mono = time.monotonic()
            size = tmuxio.pane_size(pane_id) if pane_id else None
            text = compose_for_pane(home, session_label, pane_id, key, size=size)
            if pane_id and size is not None:
                frame_sig = (text, size[0], size[1])
                if frame_sig != last_drawn:
                    _draw(text, pane_id, size=size)
                    last_drawn = frame_sig
            # While the coordinator holds watch.lock, it already watches
            # state.db-wal and rewrites render.txt whenever DB state changes;
            # skipping direct DB watch in follow avoids waking on every parent
            # chat write. If the coordinator dies, follow watches state.db itself.
            coord_up = coordinator_alive(key, home)
            dirs, files = collect_paths(home, include_db=not coord_up)
            watcher.sync([target.parent] + dirs, [target] + files)
            fired = watcher.wait(_FOLLOW_RUNNING_WAKE_S if "RUNNING" in text else _FOLLOW_WAKE_S)
            if watcher.signal_woke:
                last_drawn = None  # SIGWINCH: force immediate redraw at new size
                continue
            if fired:
                if not coord_up:
                    rem = _MIN_RENDER_INTERVAL_S - (time.monotonic() - last_pass_mono)
                    if rem > 0:
                        time.sleep(rem)
                        watcher.wait(0)
                        continue
                watcher.drain(quiet_for=0.05, max_wait=0.25)
    except KeyboardInterrupt:
        return 0
    finally:
        watcher.close()
        restore()


def _reason() -> str:
    from .gate import disabled_reason
    return disabled_reason() or "off"
