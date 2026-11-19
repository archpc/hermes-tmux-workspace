"""Command line surface for the Hermes tmux workspace.

Every command is safe to run at any time: with the gate off, outside tmux, or
with a half-built workspace, it degrades to a message and exit 0. Nothing here
kills a pane, window, or session it did not create.

The surface, and what each command owns:

==========================  ===================================================
`up`                        build or repair this session's workspace: overview
                            window (`agents`), `hx:mail`, `hx:testbench`, the
                            coordinator, the click binding. Idempotent.
`down`                      close every pane of ours on this session, then the
                            state directory, then the server-wide click binding
                            if no workspace is left anywhere.
`status`                    print the overview. The only command designed to
                            work outside tmux and in a pipe.
`rerender`                  force one render; outside tmux it renders to stdout.
`follow` / `watch`          the two loops (delegated to `watch`).
`logs|tests|debug|rea|mail|testbench`
                            ensure one `hx:<role>` window and focus it.
`tests --run`               run the suite in `hx:tests` and record `tests.log`.
`run`                       `up`, then exec the agent in the current pane.
`worker N` / `open REF`     focus a worker tab; `open` creates it for finished
                            work and marks it pinned.
`close [@N]`                close the *view* of one worker tab (never the worker).
`close-mark on|off|status`  repair/remove the click binding and the marks.
`tail LOG`                  print and follow one transcript.
`review …`                  record or run an independent review.

Two invariants run through the whole file:

1. **Ownership.** A window is ours only if it carries `@hx` as its *own* window
   option, and a pane is closable only through `tmuxio.close_pane`, which
   requires the pane's own `@hx`, our tail/python foreground, or a non-empty
   `pane_start_command`. Names are never an address, and a window id from a
   layout file only ever *nominates* a window (`cmd_down`), never authorises
   touching anything inside it.
2. **The reader's pane is never written to.** Anything started through
   `tmux run-shell` must be silent and exit 0, because tmux prints a job's
   output — or a line about its non-zero exit — into the active pane. Every
   such command here is wrapped `… >/dev/null 2>&1; true`, and the coordinator
   logs to `watch.log` instead of stdout.

State lives in `watch.session_dir(key)`; the layout document is the only shared
mutable state and every write goes through `watch.update_layout` (locked
read-modify-write). `cmd_up` additionally serialises the whole build on a
server-wide lock, because two `up`s that both find no overview would each build
one.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import closemark, gate, tmuxio, watch
from .env import pane_env, python as _env_python, repo_root as _env_repo_root

WORKERS_WINDOW = watch.OVERVIEW_WINDOW
AUX_ROLES = ("logs", "tests", "debug", "rea", "mail", "testbench")


def testbench_tab_enabled() -> bool:
    return os.environ.get("HERMES_TMUX_TESTBENCH", "1").strip().lower() not in (
        "0", "false", "off", "no"
    )


# --------------------------------------------------------------------------- helpers

def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _python() -> str:
    return os.environ.get("HX_PYTHON") or sys.executable or "python3"


def _self_command(*args: str) -> str:
    """Shell command that re-enters this CLI under the same interpreter.

    Environment is embedded explicitly: processes started through
    ``tmux run-shell -b`` inherit the *server's* environment, not the pane's,
    so an inherited HERMES_HOME would silently point at the wrong profile.

    The command is ``cd <repo> && exec env K=V … <python> -m hermes_tmux …``.
    Callers that need the shell to survive the child (so they can append
    ``; true`` or a log redirect) call ``.replace("&& exec env ", "&& env ", 1)``
    — that is why the exact string matters. Changing the prefix silently breaks
    `_start_watcher` and `_close_handler`, which both depend on it.
    """
    root = _repo_root()
    env = pane_env()
    prefix = " ".join("%s=%s" % (k, shlex.quote(v)) for k, v in env.items())
    return "cd %s && exec env %s %s -m hermes_tmux %s" % (
        shlex.quote(str(root)), prefix,
        shlex.quote(_python()), " ".join(shlex.quote(a) for a in args))


def _pane_env() -> Dict[str, str]:
    """Environment every pane we create must carry (see hermes_tmux.env)."""
    from .env import pane_env
    return pane_env()


def _ensure_state_dir(key: str) -> Path:
    sdir = watch.session_dir(key)
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def _lock_holder(key: str) -> Optional[int]:
    """Pid currently holding this session's coordinator lock, if any.

    The coordinator takes ``flock`` on ``watch.lock`` for its whole life, so the
    lock holder IS the coordinator. Looking it up with ``lsof`` is what makes the
    identity check reliable: a command line can be spoofed, an open lock cannot.
    """
    path = watch.lock_path(key)
    if not path.exists():
        return None
    try:
        proc = subprocess.run(["lsof", "-t", str(path)], capture_output=True,
                              text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def _lock_held(key: str) -> bool:
    """True when some process holds this session's coordinator lock right now.

    The probe takes the lock for an instant. A coordinator that starts inside
    that instant sees the lock taken and exits; the cost is a missed start that
    the next `up` repairs, never two coordinators.
    """
    import fcntl
    path = watch.lock_path(key)
    if not path.exists():
        return False
    try:
        with open(path, "a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, IOError):
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def _pid_is_watcher(pid: int, key: str = "") -> bool:
    """True only when ``pid`` is a live process that is really our watcher.

    A pid recorded in a file is not an identity: pids get recycled, and
    signalling a recycled pid can kill an unrelated shell. Three independent
    checks must pass: the pid holds this session's lock, its command line is the
    contiguous ``-m hermes_tmux watch`` sequence, and any known key matches
    exactly. A grep or an editor mentioning the words holds no lock and fails.
    """
    if pid <= 1:
        return False
    if key:
        holder = _lock_holder(key)
        if holder is not None and holder != pid:
            return False
        if holder is None:
            # No lock held at all: whatever this pid is, it is not coordinating.
            return False
    try:
        proc = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    command = (proc.stdout or "").strip()
    if "-m hermes_tmux watch" not in command:
        return False
    if key and (" --key %s" % key) not in command:
        return False
    return True


def _watcher_pid(key: str) -> Optional[int]:
    try:
        pid = int(watch.pid_path(key).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_is_watcher(pid, key) else None


def _watcher_alive(key: str) -> bool:
    return _watcher_pid(key) is not None


def _start_watcher(key: str) -> bool:
    """Start the per-session coordinator, passing the session key explicitly.

    The process is launched with ``tmux run-shell -b``, which runs it under the
    *server's* environment: it has ``TMUX`` but no ``TMUX_PANE``, so it cannot
    derive this process's session key itself. Without ``--key`` it would compute
    a different key, read an empty layout, and silently reconcile nothing.
    """
    if _watcher_alive(key):
        return True
    # tmux shows the output of a `run-shell` job, and a line about any job that
    # ends non-zero or on a signal, in the ACTIVE pane in view mode: that is
    # the reader's own pane. So the coordinator's output goes to a log, and the
    # wrapper shell (no `exec`) always ends 0. Nothing this process does, crash
    # included, may write into a pane we do not own.
    log = watch.session_dir(key) / "watch.log"
    command = _self_command("watch", "--key", key).replace("&& exec env ", "&& env ", 1)
    return tmuxio.run_shell_background(
        "%s >>%s 2>&1; true" % (command, shlex.quote(str(log))))


# --------------------------------------------------------------------------- up

def cmd_up(args) -> int:
    """Build or repair the workspace in the pane's session. Exit 0 unless tmux
    refused to create something.

    Three preconditions, each with its own message rather than an exception: a
    disabled gate, no tmux, or a window that could not be created. Outside tmux
    `up` is a no-op *by design* — `status` is the command that still works, and
    the message says so.

    Serialised on `_server_lock()` before anything is read: the two things `up`
    does at the end (wrap the click binding, create the overview) are both
    check-then-act, and two concurrent `up`s each seeing "no overview" built two.
    """
    if not gate.enabled():
        print("hermes-tmux: %s" % gate.disabled_reason())
        return 0
    if not tmuxio.in_tmux():
        print("hermes-tmux: not inside tmux; nothing to build.")
        print("  status still works outside tmux:  hermes-tmux status")
        return 0

    # One `up` at a time: two that both find no overview would each
    # build one, and the click binding is wrapped by check-then-bind.
    with _server_lock():
        return _up()


@contextlib.contextmanager
def _server_lock():
    """Serialise workspace construction on this server (not just this session).

    Two locks, always in this order, both ``flock``:

    * ``hx-srv-<uid>-<server digest>.lock`` beside the tmux socket (or in the
      temp dir when the socket's directory is not writable): one `up` per
      server, because the click binding and `_any_workspace_on_server` are
      server-wide facts. Taking this first means the per-home lock below is never
      contended by two processes that would then fight over the same window.
    * ``<state_root>/up.lock``: per HERMES_HOME. Two profiles on one server
      still build independently; this one only stops two `up`s of the *same*
      profile from interleaving their layout writes.

    The digest is derived by asking for a key with a dummy session id and taking
    its first component: the same server-pid digest `watch.session_key` uses, so
    a replaced server never shares a lock file with its predecessor.
    """
    digest = tmuxio.session_key(session_id="x").split("-", 1)[0]
    sock_raw = (os.environ.get("TMUX") or "").split(",", 1)[0]
    lock_dir = Path(sock_raw).parent if sock_raw and Path(sock_raw).parent.is_dir() else Path(tempfile.gettempdir())
    srv_lock = lock_dir / ("hx-srv-%d-%s.lock" % (os.getuid(), digest))
    root = watch.state_root()     # beside sessions/, whose entries are all session directories
    root.mkdir(parents=True, exist_ok=True)
    with open(str(srv_lock), "a+") as srv_handle, open(str(root / "up.lock"), "a+") as handle:
        fcntl.flock(srv_handle.fileno(), fcntl.LOCK_EX)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                fcntl.flock(srv_handle.fileno(), fcntl.LOCK_UN)


def _anchor(layout: Dict) -> Tuple[str, str]:
    """(id, name) of the session the workspace's coordinator is tied to.

    Any session of the group can address the shared windows; the anchor only
    decides whose end stops the coordinator. Keep the recorded one while it
    lives, so a second terminal coming and going changes nothing. Otherwise
    take the oldest session of the group, never the one tmux resolved this
    pane to: that is whichever was active last, often a short-lived view.
    """
    recorded = layout.get("session_id") or ""
    if recorded and tmuxio.session_alive(recorded):
        session_id = recorded
    else:
        members = tmuxio.group_sessions(tmuxio.current_session_id() or "")
        session_id = members[0] if members else ""
    return session_id, (tmuxio.format_of(session_id, "#{session_name}") if session_id else "")


def _up() -> int:
    """The body of `up`, already under the server lock.

    Reads once, decides, then writes: the layout is read at entry, mutated in
    memory, and published at the end through `update_layout` (so a concurrent
    `open`/`logs` that added its own tab or aux record is not reverted). The
    fields it owns are `session_id`, `session_name`, `workers_window`,
    `overview_pane`, `created` and `ascii`; it never writes `tabs`, `aux`,
    `dismissed` or `seen`.

    Order is load-bearing: the overview window must exist and be marked before
    `decorate_window` (which derives its format from the *global* option), and
    `created` must be published before the coordinator starts, or the first tab
    pass would treat work that finished a second ago as new.
    """
    key = tmuxio.session_key()
    _ensure_state_dir(key)
    layout = watch.read_layout(key)
    session_id, session_name = _anchor(layout)
    layout["session_id"] = session_id
    layout["session_name"] = session_name

    # Reuse the recorded window only if it is still alive AND still ours. A
    # window id is only unique within one server; a layout that outlived its
    # server (or was hand-edited) must never turn a user window into ours.
    window_id = layout.get("workers_window")
    if not (window_id and tmuxio.window_exists(window_id)
            and tmuxio.owns_window(window_id)):
        window_id = None
    if window_id is None:
        existing = tmuxio.find_window_by_name(session_id, WORKERS_WINDOW)
        window_id = existing["window_id"] if existing else None
    created = False
    if window_id is None:
        window_id = tmuxio.new_window(
            session_id, tmuxio.free_window_name(session_id, WORKERS_WINDOW),
            cwd=str(Path.home()),
            command=_self_command("follow", "--key", key), env=_pane_env())
        if not window_id:
            print("hermes-tmux: could not create %s" % WORKERS_WINDOW)
            return 1
        created = True

    layout["workers_window"] = window_id
    tmuxio.mark_window(window_id)
    # The workspace's identity travels with its own window from here on.
    tmuxio.mark_key(window_id, key)

    panes = tmuxio.wait_for_panes(window_id)
    if not panes:
        print("hermes-tmux: %s has no panes" % WORKERS_WINDOW)
        return 1
    # Find the pane that is already running the overview. Ownership is the
    # pane's OWN option (a marked window does not make its panes ours), and the
    # command must be the follow loop: pane 0 is first only by position, and a
    # user may have split a shell there.
    follow_cmd = _self_command("follow", "--key", key)
    overview = ""
    recorded = layout.get("overview_pane") or ""
    for pane in panes:
        # `created` (the window was made by this call) relaxes the id match: the
        # first pane of a window we just made is ours by construction, and its
        # recorded id cannot exist yet.
        if pane["ours"] and "python" in (pane["command"] or "").lower() \
                and (not recorded or pane["pane_id"] == recorded or created):
            overview = pane["pane_id"]
            break

    if not overview and created:
        # The window was created by THIS call and its first pane is ours by
        # construction: if the loop is not running there, restart it in place.
        if panes and panes[0]["ours"]:
            overview = panes[0]["pane_id"]
            tmuxio.respawn(overview, follow_cmd)

    if not overview:
        # The overview was lost. Build a replacement; never repurpose a pane that
        # survived with someone else's work in it.
        overview = tmuxio.split_window(window_id, follow_cmd,
                                       horizontal=False, env=_pane_env()) or ""
        if not overview:
            print("hermes-tmux: could not rebuild the overview in %s" % WORKERS_WINDOW)
            return 1
        tmuxio.mark_pane(overview)
        print("hermes-tmux: rebuilt the overview pane (%s)" % overview)

    # Window-scoped presentation only: the state badge in this window's tab,
    # and no border line above a pane that already has its own heading.
    from . import labels
    watch.decorate_window(window_id, header=False, ascii_mode=labels.ascii_only())
    if closemark.enabled():
        ok, why = closemark.install(_close_handler())
        if not ok:
            print("hermes-tmux: close mark not installed (%s)" % why)

    def _apply(current):
        """Publish our fields without touching tabs/aux/dismissed."""
        current["session_id"] = session_id
        current["session_name"] = session_name
        current["workers_window"] = window_id
        current["overview_pane"] = overview
        # Work that finished before this moment is history, not a tab.
        # setdefault: a repaired workspace keeps its original birthday, so
        # `up` on a healthy session never reclassifies yesterday's failures as
        # today's attention.
        current.setdefault("created", time.time())
        current["ascii"] = labels.ascii_only()
        return current
    layout = watch.update_layout(key, _apply)
    mail_wid, _mail_state = ensure_aux_window(
        key, session_id, "mail", focus=False, after_window=window_id
    )
    if not mail_wid:
        print("hermes-tmux: warning: could not create hx:mail")
    if testbench_tab_enabled():
        tb_wid, _tb_state = ensure_aux_window(
            key, session_id, "testbench", focus=False, after_window=mail_wid or window_id
        )
        if not tb_wid:
            print("hermes-tmux: warning: could not create hx:testbench")
    _start_watcher(key)
    watch.render_and_publish(key, session_label=session_name)
    print("hermes-tmux: workspace ready  (%s)" % window_id)
    return 0


# --------------------------------------------------------------------------- down

def cmd_down(args) -> int:
    """Close everything of ours on this session; report panes/windows separately.

    Deliberately does **not** check the gate: a reader who disabled the
    workspace must still be able to remove the one an earlier shell built.

    Order, and why each step precedes the next:

    1. Stop the coordinator (identity-checked, `_watcher_pid`). While it lives it
       would reconcile the emptying window set and reopen worker windows between
       our pane closes.
    2. Enumerate targets: every window on this session that carries our marker,
       plus every window id recorded in the layout (the marker can be lost after
       a server restart). Recorded ids only *nominate*; the per-pane proof inside
       `tmuxio.close_pane` decides. Closing is one pane at a time so a reader
       shell split into one of our windows survives with its window.
    3. Refuse to delete the state directory while any process still holds the
       coordinator lock — that lock holder would otherwise be orphaned beside a
       fresh, empty state directory.
    4. Restore the server-wide click binding only when no workspace or owned
       window remains anywhere on the server (re-checked under the lock).

    Windows are counted by diffing the window set from before the closes, since
    tmux destroys a window implicitly with its last pane and would report "gone".
    """
    if not tmuxio.in_tmux():
        print("hermes-tmux: not inside tmux.")
        return 0
    key = tmuxio.session_key()
    layout = watch.read_layout(key)
    # Stop the coordinator first. Closing panes while it is alive lets it
    # reconcile the empty window set and reopen worker windows between our pane
    # closes and the window close, which looked like `down` leaving them behind.
    pid = _watcher_pid(key)
    if pid is not None:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        deadline = time.time() + 2.0
        while time.time() < deadline and _watcher_alive(key):
            time.sleep(0.02)
    closed_panes = 0
    session = tmuxio.current_session_id() or ""
    before_windows = {win["window_id"] for win in tmuxio.list_windows(session)}
    # Every window we created carries our marker; a window's name is the task
    # it shows, so names are not an address. The marker only nominates a
    # window: inside it, each pane is closed on its OWN proof of ownership, one
    # at a time, so a shell the user split into one of our windows survives and
    # keeps its window.
    targets: List[str] = [win["window_id"] for win in tmuxio.list_windows(session)
                          if win["ours"]]
    # Recorded ids are nominated too, for a window whose marker was lost. They
    # only nominate: a stale id that now names a user window has no pane that
    # passes the per-pane proof, so nothing in it is closed.
    recorded = [layout.get("workers_window")] + list((layout.get("aux") or {}).values()) \
        + [tab.get("window") for tab in (layout.get("tabs") or {}).values()
           if isinstance(tab, dict)]
    targets += [wid for wid in recorded if wid and tmuxio.window_exists(wid)]
    for window_id in dict.fromkeys(targets):
        for pane in tmuxio.list_panes(window_id):
            if tmuxio.close_pane(pane["pane_id"])[0]:
                closed_panes += 1
        if tmuxio.window_exists(window_id) and tmuxio.foreign_panes(window_id):
            print("hermes-tmux: leaving %s in place; it holds a pane we do not own"
                  % (tmuxio.format_of(window_id, "#{window_name}") or window_id))
    if _lock_held(key):
        # The coordinator could not be identified or did not stop (no `lsof`,
        # a timeout). Removing its state directory now would let a second
        # coordinator take a fresh lock beside it.
        print("hermes-tmux: a coordinator still holds this session's lock; "
              "state kept in %s" % watch.session_dir(key))
        return 1
    # This session's state directory belongs to a workspace that no longer
    # exists. Removing it here is what keeps `sessions/` from growing one dead
    # directory per server restart (the key includes the server pid).
    try:
        sdir = watch.session_dir(key)
        for child in sdir.iterdir():
            if child.is_file():
                child.unlink()
        sdir.rmdir()
    except OSError:
        pass
    if not _any_workspace_on_server(exclude_key=key):
        # The click binding is per server. It goes only when no workspace or
        # owned worker window remains anywhere on this server (including other
        # HERMES_HOME profiles and tabs opened without `up`).
        with _server_lock():
            if not _any_workspace_on_server(exclude_key=key):
                closemark.restore()
    closed_windows = sum(1 for window_id in before_windows
                         if not tmuxio.window_exists(window_id))
    print("hermes-tmux: closed %d pane(s) and %d window(s)"
          % (closed_panes, closed_windows))
    return 0


# --------------------------------------------------------------------------- status

def cmd_status(args) -> int:
    """Print the overview; the one command that works outside tmux and in a pipe.

    ``--json`` is the machine surface and prints stable field names only (no
    derived labels), so a consumer never has to parse prose. Text mode asks the
    terminal for its width and renders for it, defaulting to 110 columns when
    stdout is not a tty; ``--all`` lifts both the worker limit and the RECENT
    row cap, which is what the "+ N older" line points at.

    Layout-derived context (tab numbers, seen keys, workspace birthday) is read
    only when inside tmux; outside, the same render runs with none of it, which
    is why `status` in a pipe shows no tab numbers rather than wrong ones.
    """
    from . import render as render_mod
    from . import state as state_mod
    from .state import live_root

    show_all = bool(getattr(args, "all", False))
    workers = state_mod.load_workers(limit=-1 if show_all else 40)
    if args.json:
        rows = []
        for w in workers:
            rows.append({k: getattr(w, k, None) for k in (
                "delegation_id", "task_index", "key", "role", "model", "state",
                "exit_reason", "phase", "tool_count", "last_tool", "last_error",
                "started", "duration_s", "review")})
        print(json.dumps({"enabled": gate.enabled(),
                          "live_root": str(live_root()),
                          "workers": rows}, indent=2))
        return 0
    from . import labels
    from .transcript import colour_enabled
    layout = watch.read_layout(tmuxio.session_key()) if tmuxio.in_tmux() else {}
    try:
        width = os.get_terminal_size(sys.stdout.fileno()).columns
    except (OSError, ValueError, AttributeError):
        width = 110
    text = render_mod.render(
        workers, width=width, live_root=str(live_root()), colour=colour_enabled(),
        tabs=watch.tab_indexes(layout), seen=watch.seen_keys(layout),
        since=layout.get("created"), ascii_mode=labels.ascii_only(),
        recent_rows=len(workers) if show_all else render_mod.RECENT_ROWS)
    print(text)
    if not gate.enabled():
        print()
        print("gate: %s" % gate.disabled_reason())
    return 0


def cmd_rerender(args) -> int:
    if not tmuxio.in_tmux():
        from . import labels
        from . import render as render_mod
        from . import state as state_mod
        from .transcript import colour_enabled
        text = render_mod.render(
            state_mod.load_workers(), width=110,
            live_root=str(state_mod.live_root()),
            colour=colour_enabled(), ascii_mode=labels.ascii_only())
        print(text)
        return 0
    key = tmuxio.session_key()
    layout = watch.read_layout(key)
    text = watch.render_and_publish(key, session_label=layout.get("session_name") or "")
    print(text)
    return 0


# --------------------------------------------------------------------------- follow/watch

def cmd_follow(args) -> int:
    return watch.run_follow(key=getattr(args, "key", "") or None)


def cmd_watch(args) -> int:
    return watch.run_watch(key=getattr(args, "key", "") or None)


# --------------------------------------------------------------------------- aux panes

def _tests_log() -> Path:
    from .state import hermes_home
    return hermes_home() / "cache" / watch.STATE_DIR_NAME / "tests.log"


def _aux_command(role: str, shell: str, run: bool = False, *, receipt: Optional[Path] = None) -> Tuple[str, str]:
    """Return (cwd, command) for one auxiliary pane.

    Every pane shows real output or a plain shell. Nothing is synthesised: the
    logs pane tails the real agent log, the tests pane shows or records a real
    run, the debug pane prints the real environment, and the REA pane reports the
    real install if the CLI is present.

    Two constraints shape the strings: `printf` is used instead of zsh's `print`
    (the pane shell is whatever $SHELL names), and no `%`-formatting is applied
    to the shell text (a literal `printf '%s'` would be read as a format spec).
    """
    from .state import hermes_home
    home = Path.home()
    repo = _repo_root()
    q = shlex.quote

    if role == "logs":
        log = hermes_home() / "logs" / "agent.log"
        if log.is_file():
            return str(home), "tail -n 200 -F " + q(str(log)) + "; exec " + shell
        return str(home), "printf '%s\\n' 'no agent log yet: " + q(str(log)) + "'; exec " + shell

    if role == "tests":
        # Never auto-run: this pane is opened from inside the very suite it would
        # run, so running it by default duplicates work and recurses. `--run`
        # opts in and records the output, which is the only writer of tests.log.
        log = _tests_log()
        if run:
            from .suite_run import testbench_binary
            argv = [_python(), "-m", "hermes_tmux.suite_run", str(repo), str(log), testbench_binary()]
            store = _pane_env().get("TESTBENCH_HOME")
            if store:
                argv[:0] = ["/usr/bin/env", f"TESTBENCH_HOME={store}"]
            launch = "trap ':' INT; cd " + q(str(repo)) + " && "
            if receipt is not None:
                argv.append(str(receipt))
                # The Python runner may never start (bad interpreter, cwd or import).
                dest, tmp = q(str(receipt)), q(str(receipt.with_suffix(".shell")))
                fallback = (
                    f"; rc=$?; if [ -d {q(str(receipt.parent))} ] && [ ! -e {dest} ]; then "
                    f"printf '%s\\n' \"$rc\" > {tmp} && /bin/mv -f {tmp} {dest}; fi"
                )
            else:
                fallback = ""
            return str(repo), launch + shlex.join(argv) + fallback + "; trap - INT; exec " + q(shell)
        if log.is_file():
            return str(repo), (
                "printf '%s\\n' '--- last recorded test run ---'; tail -n 60 "
                + q(str(log))
                + "; printf '%s\\n' '--- rerun: hermes-tmux tests --run ---'; exec " + shell)
        return str(repo), (
            "printf '%s\\n' 'hermes-tmux: no recorded test run yet'; "
            "printf '%s\\n' 'run it with: hermes-tmux tests --run'; exec " + shell)

    if role == "debug":
        sid = q(tmuxio.current_session_id() or "?")
        skey = q(tmuxio.session_key())
        return str(home), (
            "printf '%s\\n' '--- hermes-tmux debug ---'; "
            "printf 'HERMES_HOME=%s HERMES_TMUX=%s TMUX_PANE=%s\\n' "
            "\"${HERMES_HOME:-unset}\" \"${HERMES_TMUX:-unset}\" \"${TMUX_PANE:-unset}\"; "
            "printf 'session=%s key=%s\\n' " + sid + " " + skey + "; "
            "printf '%s\\n' '--- workspace windows ---'; "
            + q(_python()) + " -m hermes_tmux debug-report; exec " + shell)

    if role == "mail":
        return str(home), (
            "if [ -x \"$HOME/.local/bin/hxmail\" ]; then exec \"$HOME/.local/bin/hxmail\"; "
            "elif command -v hxmail >/dev/null 2>&1; then exec hxmail; "
            "else printf '%s\\n' 'hxmail binary not installed yet (expected ~/.local/bin/hxmail)'; exec " + shell + "; fi")

    if role == "testbench":
        return str(home), (
            "if [ -x \"$HOME/.local/bin/testbench\" ]; then exec \"$HOME/.local/bin/testbench\" tui; "
            "elif command -v testbench >/dev/null 2>&1; then exec testbench tui; "
            "else printf '%s\\n' 'testbench binary not installed yet (expected ~/.local/bin/testbench)'; exec " + shell + "; fi")

    # role == "rea": show what is actually installed, then a shell.
    return str(home), (
        "printf '%s\\n' '--- REA ---'; "
        "if command -v rea >/dev/null 2>&1; then rea --version 2>&1 | head -3; "
        "else printf '%s\\n' 'rea CLI not on PATH'; fi; "
        "printf '%s\\n' 'REA MCP state is served by Hermes (hermes mcp test rea), not by this pane.'; "
        "exec " + shell)


def ensure_aux_window(
    key: str,
    session_id: str,
    role: str,
    *,
    focus: bool = False,
    run: bool = False,
    after_window: Optional[str] = None,
    home: Optional[Path] = None,
    owner_pid: Optional[str] = None,
    receipt: Optional[Path] = None,
) -> Tuple[Optional[str], str]:
    """Ensure ``hx:<role>`` exists with one owned pane; focus only when asked.

    Shared by ``cmd_aux`` (interactive ``hermes-tmux mail|logs|...``), ``_up``
    (permanent ``hx:mail`` startup), and ``watch`` (detached crash recovery).
    Returns ``(window_id, status)`` where status is ``opened``, ``rebuilt``,
    ``running``, ``existing``, or the two failures ``failed`` / ``stale_server``.

    Resolution order for the window: the recorded id, if it still exists *and*
    is still ours; then a name lookup inside the anchor session. That lookup is
    exact-or-"name + space" (the badge suffix `free_window_name`/`watch` add),
    and it requires our marker, so a user window that happens to be called
    `hx:mail` is never adopted. A `#N` collision suffix is *not* matched: if a
    foreign window already held the name when we created ours, a later pass will
    find nothing by name and create a second window — the recorded id covers that
    case for every caller that writes one.

    ``home`` re-points the pane's `HERMES_HOME` and, for a non-default home,
    its mail/TestBench stores. It is how the tests and `watch`'s recovery build
    a tab for a profile other than the caller's.

    ``owner_pid`` is the server pid the caller believed in. It is re-checked
    before every tmux mutation: a server replaced on the same socket during this
    call would otherwise receive writes aimed at its predecessor, and the window
    created in that window of time is removed again if it turned out to be ours.

    Focusing is a separate step at the end, so a failure between creation and
    focus cannot leave the reader looking at a half-built pane.
    """
    name = "hx:" + role
    shell = os.environ.get("SHELL") or "/bin/zsh"
    layout = watch.read_layout(key, home)
    recorded = ((layout.get("aux") or {}).get(role)) or ""
    wid: Optional[str] = None
    if recorded and tmuxio.window_exists(recorded) and tmuxio.owns_window(recorded):
        wid = recorded
    if wid is None and session_id:
        found = tmuxio.find_window_by_name(session_id, name)
        if found:
            wid = found["window_id"]

    p_env = _pane_env()
    if home is not None:
        p_env["HERMES_HOME"] = str(home)
        try:
            is_def = home.resolve() == (Path.home() / ".hermes").resolve()
        except OSError:
            is_def = False
        if not is_def and "HXMAIL_DB" not in os.environ:
            p_env["HXMAIL_DB"] = str(home / "cache" / "hxmail" / "mail.db")
            p_env["HXMAIL_LOCAL_DIR"] = str(home / "cache" / "hxmail" / "local")
        if not is_def and "TESTBENCH_HOME" not in os.environ:
            p_env["TESTBENCH_HOME"] = str(home / "cache" / "testbench")

    if owner_pid and tmuxio.server_pid() != owner_pid:
        return None, "stale_server"

    status = "existing"
    if wid is not None:
        panes = tmuxio.list_panes(wid)
        ours = [pane for pane in panes if pane["ours"]]
        if run and ours:
            _cwd, run_cmd = _aux_command(role, shell, run=True, receipt=receipt)
            if not tmuxio.respawn(ours[0]["pane_id"], run_cmd):
                return None, "failed"
            status = "running"
        elif not ours:
            if owner_pid and tmuxio.server_pid() != owner_pid:
                return None, "stale_server"
            _cwd, cmd = _aux_command(role, shell, run=run, receipt=receipt)
            rebuilt = tmuxio.split_window(wid, cmd, horizontal=False, env=p_env)
            if not rebuilt:
                return None, "failed"
            tmuxio.mark_pane(rebuilt)
            status = "rebuilt"
    else:
        if not session_id:
            return None, "failed"
        if owner_pid and tmuxio.server_pid() != owner_pid:
            return None, "stale_server"
        cwd, cmd = _aux_command(role, shell, run=run, receipt=receipt)
        wid = tmuxio.new_window(
            session_id,
            tmuxio.free_window_name(session_id, name),
            cwd=cwd,
            command=cmd,
            env=p_env,
            after_window=after_window,
        )
        if not wid:
            return None, "failed"
        if owner_pid and tmuxio.server_pid() != owner_pid:
            # Raced server replacement on the same socket: remove only if ours and leave old layout untouched.
            if tmuxio.owns_window(wid):
                tmuxio.run("kill-window", "-t", wid)
            return None, "stale_server"
        tmuxio.window_option(wid, "pane-border-status", "off")
        status = "opened"

    if owner_pid and tmuxio.server_pid() != owner_pid:
        return None, "stale_server"

    if ((layout.get("aux") or {}).get(role)) != wid:
        def _record(current):
            current.setdefault("aux", {})[role] = wid
            return current
        watch.update_layout(key, _record, home=home)

    if focus and wid:
        tmuxio.select_window(wid)
    return wid, status


def _run_tests_in_aux(key: str, session_id: str, sdir: Path, layout: dict) -> int:
    """Run the suite in `hx:tests`, wait for its receipt, return its status.

    Three guards, each for a failure that was observed:

    * `<state>/tests-run.lock`, non-blocking: a second `tests --run` says so
      instead of starting a second run that would overwrite `tests.log`.
    * if the caller *is* the tests pane, run in place. Respawning the pane the
      command was typed in would kill the shell that is waiting for the answer.
    * a startup grace of 5 s: the pane's shell touches `started` before the
      Python runner begins. Without that evidence, stop after 5 s and report 1 —
      otherwise a runner that never imported (bad interpreter, missing module)
      would look like a 7-minute hang.

    The deadline is 420 s against the manifest's 360 s bound, leaving room for
    startup and cleanup. A `cancel` file appearing (Ctrl-C in the caller, which
    also touches it) is reported as 130, not as a suite failure.
    """
    from .suite_run import run, testbench_binary

    with (sdir / "tests-run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("hermes-tmux: tests are already running in hx:tests", file=sys.stderr)
            return 1
        wid = (layout.get("aux") or {}).get("tests")
        caller = os.environ.get("TMUX_PANE")
        if wid and any(p["ours"] and p["pane_id"] == caller for p in tmuxio.list_panes(wid)):
            # From the test shell itself, don't respawn the caller.
            return run(_repo_root(), _tests_log(), testbench_binary())

        with tempfile.TemporaryDirectory(prefix="hx-tests-", dir=sdir) as td:
            receipt = Path(td) / "exit"
            wid, _status = ensure_aux_window(
                key, session_id, "tests", focus=True, run=True,
                after_window=layout.get("workers_window"), receipt=receipt,
            )
            if not wid:
                print("hermes-tmux: could not start tests in hx:tests", file=sys.stderr)
                return 1
            print("hermes-tmux: running the suite in hx:tests", flush=True)
            # The manifest bounds the suite to 360s; allow time for startup and cleanup.
            startup = time.monotonic() + 5
            deadline = time.monotonic() + 420
            interrupted = False
            while not receipt.exists() and time.monotonic() < deadline:
                try:
                    if time.monotonic() >= startup and not (receipt.parent / "started").exists():
                        break
                    time.sleep(0.1)
                except KeyboardInterrupt:
                    interrupted = True
                    (receipt.parent / "cancel").touch()
            try:
                rc = int(receipt.read_text(encoding="ascii"))
            except (OSError, ValueError):
                print("hermes-tmux: hx:tests did not report an exit status", file=sys.stderr)
                return 1
            if interrupted:
                rc = 130
            if rc:
                print(f"hermes-tmux: TestBench exited {rc}; see hx:tests or {_tests_log()}", file=sys.stderr)
            return rc


def cmd_aux(args) -> int:
    """Ensure one `hx:<role>` window and focus it (or run the suite for `tests`).

    The session the aux window is created in is the *recorded* one while it
    lives, otherwise the current session (focus semantics). That fallback is
    deliberately NOT `up`'s anchor rule (the group's lowest session, DESIGN §7):
    aux is a navigation command, so it follows the terminal you typed in. The
    recorded-session branch still keeps a second terminal from building a second
    `hx:mail` in another session of the same group.

    Exit status: 0 for "opened/focused", 1 for a refused run (`tests --run` with
    a disabled gate, outside tmux, or already running). A plain aux command
    outside tmux stays 0 with a message: it is a navigation command, and a
    script calling it from a cron job should not treat "no workspace here" as an
    error.
    """
    role = args.role
    run_requested = getattr(args, "run", False)
    if not gate.enabled():
        print("hermes-tmux: %s" % gate.disabled_reason(), file=sys.stderr if run_requested else sys.stdout)
        return 1 if run_requested else 0
    if not tmuxio.in_tmux():
        print("hermes-tmux: not inside tmux.", file=sys.stderr if run_requested else sys.stdout)
        return 1 if run_requested else 0
    key = tmuxio.session_key()
    sdir = _ensure_state_dir(key)
    layout = watch.read_layout(key)
    recorded_sid = layout.get("session_id") or ""
    session_id = recorded_sid if (recorded_sid and tmuxio.session_alive(recorded_sid)) else (
        tmuxio.current_session_id() or ""
    )
    if run_requested:
        return _run_tests_in_aux(key, session_id, sdir, layout)
    name = "hx:" + role
    wid, status = ensure_aux_window(
        key, session_id, role, focus=True, after_window=layout.get("workers_window")
    )
    if not wid:
        print("hermes-tmux: could not create %s" % name)
        return 1
    if status == "opened":
        print("hermes-tmux: opened %s" % name)
    else:
        print("hermes-tmux: focused %s" % name)
    return 0


def cmd_debug_report(args) -> int:
    """Print the workspace's own view of itself: windows, panes, ownership."""
    if not tmuxio.in_tmux():
        print("not inside tmux")
        return 0
    key = tmuxio.session_key()
    layout = watch.read_layout(key)
    session = tmuxio.current_session_id() or ""
    print("session %s (%s)  key %s" % (
        session, tmuxio.current_session_name() or "?", key))
    print("layout: %s" % json.dumps(layout, sort_keys=True))
    for win in tmuxio.list_windows(session):
        mark = "*" if win["ours"] else " "
        print("%s %s %-18s index %s" % (mark, win["window_id"], win["name"], win["index"]))
        for pane in tmuxio.list_panes(win["window_id"]):
            print("    %s ours=%s %-10s %s" % (
                pane["pane_id"], pane["ours"], pane["command"], pane["title"]))
    pid = "?"
    try:
        pid = watch.pid_path(key).read_text(encoding="utf-8").strip()
    except OSError:
        pass
    print("watcher pid: %s (alive=%s)" % (pid, _watcher_alive(key)))
    return 0


def cmd_run(args) -> int:
    """`up`, then exec the agent in this pane. The workspace never owns this pane.

    `exec` (rather than a subprocess) so the agent replaces this process and no
    wrapper outlives it. The pane is deliberately *not* marked: it is the
    reader's agent window, and `down` must leave it standing.

    127 when the binary cannot be executed, which is the shell's convention for
    "command not found" and lets a caller distinguish it from the agent's own
    exit status.
    """
    cmd_up(args)
    argv = list(args.args or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    binary = args.hermes or "hermes"
    try:
        os.execvp(binary, [binary] + argv)
    except OSError as exc:
        print("hermes-tmux: cannot exec %s: %s" % (binary, exc))
        return 127


def _tabs_in_order(layout: Dict) -> List[Tuple[str, Dict]]:
    """Worker tabs as the status line shows them: by window index.

    A tab with no recorded index sorts last instead of raising or sorting as 0:
    the index is a tmux fact that can be missing after a hand-edit, and a missing
    number is not tab number one.
    """
    indexes = watch.tab_indexes(layout)

    def _index(item) -> int:
        text = str(indexes.get(item[0], ""))
        return int(text) if text.isdigit() else 10 ** 6

    tabs = [(k, v) for k, v in (layout.get("tabs") or {}).items() if isinstance(v, dict)]
    return sorted(tabs, key=_index)


def cmd_worker(args) -> int:
    """Focus the Nth worker tab, numbering from 1 in status-line order.

    The order comes from the layout's recorded window indices (`tab_indexes`),
    not from the dict order: tab order is what the reader sees, and a tab whose
    index is unknown sorts last rather than pretending to be first.
    """
    if not tmuxio.in_tmux():
        print("hermes-tmux: not inside tmux.")
        return 0
    key = tmuxio.session_key()
    tabs = _tabs_in_order(watch.read_layout(key))
    idx = args.index
    if idx < 1 or idx > len(tabs):
        print("hermes-tmux: no worker tab %d (have %d)" % (idx, len(tabs)))
        return 1
    wkey, tab = tabs[idx - 1]
    # `select-pane` alone does not switch the session's current window, so
    # focus is two explicit steps: the window, then the pane inside it.
    tmuxio.select_window(tab.get("window") or "")
    tmuxio.select_pane(tab.get("pane") or "")
    print("hermes-tmux: focused worker tab %d (%s)" % (idx, wkey))
    return 0


def cmd_open(args) -> int:
    """Open (or focus) the transcript tab of any worker, finished ones included.

    A tab opened this way is the reader's: it stays until they close it.

    Reference matching, in order: exact ``<last8>#<task>``, or the bare last 8
    of the delegation id. A bare id that matches several tasks of one delegation
    resolves to task 0 (the head task) rather than to whichever came first in
    the list — an arbitrary sibling would be a coin flip presented as a choice.

    The record this writes is the same shape the coordinator writes, plus
    ``pinned``: the coordinator may retire an unpinned tab when its worker
    leaves, and must never retire one the reader opened on purpose. ``dismissed``
    is cleared so a tab closed by hand can be brought back.
    """
    if not gate.enabled():
        print("hermes-tmux: %s" % gate.disabled_reason())
        return 0
    if not tmuxio.in_tmux():
        print("hermes-tmux: not inside tmux.")
        return 0
    from . import labels
    from .state import load_workers
    ref = args.ref.replace("deleg_", "")
    all_workers = load_workers(limit=-1)
    matches = [w for w in all_workers
               if ref in ("%s#%s" % (w.delegation_id[-8:], w.task_index))
               or ref == w.delegation_id[-8:]]
    if not matches:
        print("hermes-tmux: no worker matches %r (ids are in `hermes-tmux status`)" % args.ref)
        return 1
    if len({w.key for w in matches}) > 1 and "#" not in ref:
        matches = [w for w in matches if w.task_index == 0] or matches[:1]
    worker = matches[0]
    key = tmuxio.session_key()
    _ensure_state_dir(key)
    layout = watch.read_layout(key)
    tab = (layout.get("tabs") or {}).get(worker.key)
    if isinstance(tab, dict) and tmuxio.pane_exists(tab.get("pane") or ""):
        tmuxio.select_window(tab.get("window") or "")
        print("hermes-tmux: focused %s" % worker.key)
        return 0
    if not worker.log_path:
        print("hermes-tmux: %s has no transcript on disk" % worker.key)
        return 1
    session_id = tmuxio.current_session_id() or ""
    ascii_mode = labels.ascii_only()
    window_id = tmuxio.new_window(
        session_id,
        tmuxio.free_window_name(session_id, labels.tab_label(
            worker.goal, [w.goal for w in all_workers
                          if w.delegation_id == worker.delegation_id and w.key != worker.key],
            ascii_mode=ascii_mode)),
        cwd=str(Path.home()), command=watch.tail_command(worker.log_path), env=_pane_env())
    panes = tmuxio.wait_for_panes(window_id) if window_id else []
    if not panes:
        print("hermes-tmux: could not open a tab for %s" % worker.key)
        return 1
    watch.decorate_window(window_id, ascii_mode=ascii_mode)
    if closemark.enabled():
        with _server_lock():
            closemark.install(_close_handler())
    kind = watch._kind(worker)
    record = {"window": window_id, "pane": panes[0]["pane_id"], "opened": time.time(),
              "pinned": True, "seen": time.time(),
              "glyph": watch.tab_glyph(kind, ascii_mode),
              "head": watch.pane_header(worker, kind, ascii_mode)}
    tmuxio.window_option(window_id, "@hx_glyph", record["glyph"])
    tmuxio.pane_option(record["pane"], "@hx_head", record["head"])

    def _apply(current):
        current.setdefault("tabs", {})[worker.key] = record
        (current.get("dismissed") or {}).pop(worker.key, None)
        return current
    watch.update_layout(key, _apply)
    tmuxio.select_window(window_id)
    print("hermes-tmux: opened %s" % worker.key)
    return 0


# --------------------------------------------------------------------------- close mark

def _close_handler() -> str:
    """Shell command the status-line binding runs for a click on a mark.

    Silent and always 0: tmux shows the output of a ``run-shell`` job, and a
    line about one that ends non-zero, in the active pane in view mode, which
    is the reader's own pane.

    ``#{client_name}`` is passed so the result message goes to the client that
    clicked, and ``#{mouse_status_range}`` carries the window id (``hxc@N``) —
    both are expanded by tmux at click time, not here. ``--quiet`` makes
    `cmd_close` print nothing and exit 0 whatever it decided: a refusal (not a
    worker tab) is not an error the reader can act on.
    """
    command = _self_command("close", "--quiet", "--client", "#{client_name}",
                            "#{mouse_status_range}").replace("&& exec env ", "&& env ", 1)
    return "%s >/dev/null 2>&1; true" % command


def _server_keys(home: Optional[Path] = None) -> List[str]:
    """Session keys with state on disk for the tmux server we are talking to."""
    digest = tmuxio.session_key(session_id="x").split("-", 1)[0]
    try:
        return sorted(p.name for p in (watch.state_root(home) / "sessions").iterdir()
                      if p.is_dir() and p.name.startswith(digest + "-"))
    except OSError:
        return []


def _any_workspace_on_server(exclude_key: str = "") -> bool:
    """True when any owned window or live watcher remains on this tmux server.

    Two independent answers, either of which keeps the click binding in place:
    any window on the server carrying our marker (another profile's workspace,
    or worker tabs opened without `up`), or a live coordinator for any *other*
    session key of this server. The second covers a workspace whose windows were
    all closed by hand while its coordinator still runs — restoring the binding
    under it would leave a server-wide list-keys change with no owner.
    """
    rc, all_wids = tmuxio.run("list-windows", "-a", "-F", "#{window_id}")
    if rc == 0:
        for line in all_wids.splitlines():
            wid = line.strip()
            if wid and tmuxio.owns_window(wid):
                return True
    return any(_watcher_alive(other) for other in _server_keys() if other != exclude_key)


def _window_home(window_id: str) -> Optional[Path]:
    """Return the HERMES_HOME stamped on ``window_id``, if any."""
    rc, out = tmuxio.run("show-options", "-wv", "-t", window_id, "@hx_home")
    val = out.strip() if rc == 0 else ""
    return Path(val) if val else None


def _tab_of_window(window_id: str) -> Tuple[Optional[Path], str, str, Dict]:
    """(home, session key, worker key, tab record) of the worker tab in ``window_id``.

    Resolves across HERMES_HOME profiles via ``@hx_home`` stamped on the window
    when it was decorated, falling back to the current ``hermes_home()``.
    """
    from .state import hermes_home
    homes: List[Path] = []
    stamped = _window_home(window_id)
    if stamped is not None:
        homes.append(stamped)
    cur_home = hermes_home()
    if cur_home not in homes:
        homes.append(cur_home)
    for cand_home in homes:
        for key in _server_keys(cand_home):
            for wkey, tab in (watch.read_layout(key, cand_home).get("tabs") or {}).items():
                if isinstance(tab, dict) and tab.get("window") == window_id:
                    return cand_home, key, wkey, tab
    return None, "", "", {}


def cmd_close(args) -> int:
    """Close the view of one worker tab. The worker itself is not touched.

    Two ways in: a click (`{"target": "hxc@12"}`, `--quiet`, `--client`) or a
    command line (`hermes-tmux close @12`, or no target = the window this
    command runs in). The target is validated by `closemark.parse_target` first;
    a string that is not a window id is refused instead of being handed to tmux
    as a target expression.

    Refusal reasons are distinct exit codes when not quiet — 2 bad target, 3 no
    such window, 4 not a worker tab / nothing of ours in it — because a script
    needs to tell "I aimed wrong" from "there was nothing to close". Quiet mode
    always exits 0, so the click binding never produces a tmux error line.

    The message is honest about the two things a reader may not expect: the
    worker is *still running* (this closed the view, not the job), and a pane
    they split into that tab was left in place (the window survives with it).
    """
    quiet = bool(getattr(args, "quiet", False))
    client = getattr(args, "client", "") or ""

    def say(text: str, code: int) -> int:
        if client:
            tmuxio.run("display-message", "-c", client, text)
        if not quiet:
            print("hermes-tmux: %s" % text)
        return 0 if quiet else code

    if not gate.enabled():
        return say(gate.disabled_reason(), 0)
    if not tmuxio.in_tmux():
        return say("not inside tmux.", 0)
    raw = getattr(args, "target", "") or ""
    if raw:
        window_id = closemark.parse_target(raw)
        if window_id is None:
            return say("not a close target: %r" % raw, 2)
    else:
        window_id = tmuxio.format_of(os.environ.get("TMUX_PANE") or "", "#{window_id}").strip()
    if not window_id or not tmuxio.window_exists(window_id):
        return say("no such window: %s" % (window_id or "?"), 3)
    tab_home, key, wkey, tab = _tab_of_window(window_id)
    if not wkey:
        # Not a worker tab: the reader's own windows, the overview, the aux
        # panes. Nothing is closed and nothing is asked of tmux.
        return say("not a worker tab: left alone", 4)
    closed = 0
    for pane in tmuxio.list_panes(window_id):
        if tmuxio.close_pane(pane["pane_id"])[0]:
            closed += 1
    if not closed:
        return say("no pane of ours in that tab: left alone", 4)

    def _apply(current):
        tabs = current.get("tabs") or {}
        if (tabs.get(wkey) or {}).get("window") == window_id:
            tabs.pop(wkey, None)
        current.setdefault("dismissed", {})[wkey] = time.time()
        return current
    watch.update_layout(key, _apply, tab_home)

    ref = wkey.replace("deleg_", "")
    ref = "%s#%s" % (ref.split("#", 1)[0][-8:], ref.split("#", 1)[1]) if "#" in ref else ref[-8:]
    text = "view closed"
    if "settled" not in tab:
        text += "; worker still running"
    text += " (reopen: hermes-tmux open %s)" % ref
    if tmuxio.window_exists(window_id):
        text += "; your pane in that tab was left"
    return say(text, 0)


def cmd_close_mark(args) -> int:
    """Turn the clickable mark on or off for this server without rebuilding anything.

    Candidate windows are the intersection of "owned" (marker as own option) and
    "recorded as a worker tab in some layout of this server" — including layouts
    of other HERMES_HOME profiles, discovered through the `@hx_home` stamp on each
    owned window. Without that breadth, `close-mark on` from one profile would
    silently skip the tabs of another, and `status` would undercount them.

    `on` installs the binding first and then marks windows; a failed install
    still reports what it marked, and the exit status follows the binding, since
    that is the part the reader asked for. `off` restores the binding *and*
    unmarks, so a server can be returned to its pre-tool state in one command.
    """
    if not tmuxio.in_tmux():
        print("hermes-tmux: not inside tmux.")
        return 0
    from . import labels
    rc, all_wids = tmuxio.run("list-windows", "-a", "-F", "#{window_id}")
    cand_wids = [w.strip() for w in all_wids.splitlines() if w.strip()] if rc == 0 else []
    owned_wids = [wid for wid in cand_wids if tmuxio.owns_window(wid)]
    from .state import hermes_home
    homes: List[Path] = [hermes_home()]
    for wid in owned_wids:
        wh = _window_home(wid)
        if wh is not None and wh not in homes:
            homes.append(wh)
    tab_wids = {
        tab.get("window")
        for h in homes
        for key in _server_keys(h)
        for tab in (watch.read_layout(key, h).get("tabs") or {}).values()
        if isinstance(tab, dict) and tab.get("window")
    }
    windows = [wid for wid in owned_wids if wid in tab_wids]
    if args.action == "status":
        marked = sum(1 for wid in windows if closemark.has_mark(closemark._own_format(wid)))
        print("hermes-tmux: close mark binding %s; %d of %d worker tab(s) marked"
              % ("installed" if closemark.installed() else "not installed", marked, len(windows)))
        return 0
    if args.action == "on":
        ok, why = closemark.install(_close_handler())
        changed = sum(1 for wid in windows if closemark.mark_window(wid, labels.ascii_only()))
        print("hermes-tmux: close mark %s; %d tab(s) marked" % (why, changed))
        return 0 if ok else 1
    ok, why = closemark.restore()
    changed = sum(1 for wid in windows if closemark.unmark_window(wid))
    print("hermes-tmux: close mark %s; %d tab(s) unmarked" % (why, changed))
    return 0 if ok else 1


def cmd_tail(args) -> int:
    from .transcript import run_tail
    return run_tail(args.log, follow=not args.no_follow)


# --------------------------------------------------------------------------- review

def _reviews_file() -> Path:
    from .state import hermes_home
    return hermes_home() / "cache" / watch.STATE_DIR_NAME / "reviews.json"


def _load_review_file() -> Dict:
    try:
        return json.loads(_reviews_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"reviews": []}


def _save_review_file(data: Dict) -> bool:
    path = _reviews_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    return watch.atomic_write(path, json.dumps(data, indent=2, sort_keys=True))


def _verdict_of(text: str) -> Optional[str]:
    """Extract the reviewer's own verdict, if it stated one.

    Real review prose states a verdict in many ways, and the failure mode that
    matters is recording PASS for a review that said FAIL. Rules, in order:

    1. An explicit ``VERDICT`` label outranks generic ones (``CONCLUSION``,
       ``RESULT``, ``OVERALL``). A document often says "the result: tests pass"
       and only later "Verdict: FAIL"; the verdict label must win.
    2. Within the winning class, the LAST labelled decision wins, since reviews
       conclude at the end.
    3. A label followed only by an enumeration ("Verdict: PASS or FAIL") states
       nothing.
    4. Negation ("not a pass", "doesn't pass", "never pass") inverts the token.
    5. With no label at all, the first un-negated token wins.

    Nothing is inferred when no verdict is stated.
    """
    if not text:
        return None
    # Typographic apostrophes must become ASCII, not disappear: deleting them
    # turned "can\u2019t" into "CANT", which no negation rule matches and made
    # "can't pass" record PASS.
    upper = text.upper()
    for quote in ("\u2019", "\u2018", "\u02bc"):
        upper = upper.replace(quote, "'")
    for marker in ("*", "_", "`", "#", "\u201c", "\u201d"):
        upper = upper.replace(marker, "")

    tokens = [(m.start(), m.group(1)) for m in re.finditer(r"\b(PASS|FAIL)\b", upper)]
    if not tokens:
        return None

    def _negated(pos: int) -> bool:
        before = upper[max(0, pos - 28):pos]
        # No leading \b on the N'T forms: "DOESN'T" must match, and \bN'T\b cannot.
        return re.search(
            r"(?:\bNOT\b|\bNO\b|\bNEVER\b|\bCANNOT\b|\bCAN'T\b|\bWON'T\b|N'T\b)"
            r"(?:\s+\w+){0,2}\s+$", before) is not None

    def _in_enumeration(pos: int, word: str) -> bool:
        after = upper[pos + len(word):pos + len(word) + 16]
        before = upper[max(0, pos - 12):pos]
        return bool(re.match(r"\s*(OR|/)\s*(PASS|FAIL)\b", after)
                    or re.search(r"(PASS|FAIL)\s*(OR|/)\s*$", before))

    def _decision_after(label_end: int) -> Optional[str]:
        for pos, word in tokens:
            if pos < label_end:
                continue
            if pos - label_end > 40:
                return None
            if _in_enumeration(pos, word):
                continue
            if _negated(pos):
                return "FAIL" if word == "PASS" else "PASS"
            return word
        return None

    # 1/2. Highest-priority label class that yields a decision; last such label.
    for pattern in (r"\bVERDICT\b", r"\bCONCLUSION\b", r"\bRESULT\b", r"\bOVERALL\b"):
        decision = None
        for label in re.finditer(pattern, upper):
            found = _decision_after(label.end())
            if found:
                decision = found
        if decision:
            return decision

    if re.search(r"\b(VERDICT|CONCLUSION|RESULT|OVERALL)\b", upper):
        # A label exists but stated only an enumeration: that is not a verdict.
        return None

    # 5. Unlabelled prose.
    negated_pass = False
    for pos, word in tokens:
        if _in_enumeration(pos, word):
            continue
        if _negated(pos):
            if word == "PASS":
                negated_pass = True
            continue
        return word
    return "FAIL" if negated_pass else None


def cmd_review(args) -> int:
    action = args.action
    if action == "list":
        data = _load_review_file()
        if not data.get("reviews"):
            print("no review records")
            return 0
        print(json.dumps(data, indent=2))
        return 0

    if not args.delegation:
        print("hermes-tmux review: --delegation required")
        return 2

    status = args.status or "recorded"
    report = args.report
    reviewer = args.reviewer

    from .state import load_workers

    def _find_worker():
        for candidate in load_workers(limit=-1):
            if candidate.delegation_id != args.delegation:
                continue
            if args.task < 0 or candidate.task_index == args.task:
                return candidate
        return None

    if action == "run":
        target = _find_worker()
        if target is None:
            print("hermes-tmux review: no such worker %s#%s"
                  % (args.delegation, args.task))
            return 2
        transcript = getattr(target, "log_path", None) or ""
        # Refuse to run a review that cannot read the work. A reviewer prompted
        # with a missing transcript still produces confident prose, and recording
        # that verdict would attribute a judgement to evidence it never saw.
        if not transcript or not Path(transcript).is_file():
            print("hermes-tmux review: no transcript on disk for %s#%s; not reviewing"
                  % (args.delegation, args.task))
            return 2
        prompt = (
            "Independently review the work recorded in this Hermes subagent "
            "transcript and verdict it.\n\nTranscript: %s\nGoal: %s\n"
            "Report: (1) what the worker actually did, (2) defects or "
            "unverified claims, (3) whether the goal was met, (4) a verdict "
            "of PASS or FAIL with one-line reasons. Write your review to the "
            "path given in the goal field if one is present; otherwise reply "
            "inline."
            % (transcript, getattr(target, "goal", "") or args.delegation)
        )
        cmd = [args.hermes or "hermes", "chat", "-q", prompt,
               "--oneshot", "-Q", "-m", reviewer or "z-ai/glm-5.3-flash"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            out = (proc.stdout or "").strip()
            status = "recorded" if proc.returncode == 0 and out else "failed"
        except (OSError, subprocess.SubprocessError) as exc:
            out = "reviewer run failed: %s" % exc
            status = "failed"
        if report:
            watch.atomic_write(Path(report), out + "\n")
        else:
            print(out)
        used_model = reviewer or "z-ai/glm-5.3-flash"
    else:
        # Recording a manual verdict. It must name real work, and it must not
        # invent a reviewer model: nothing ran, so nothing is attributed.
        if _find_worker() is None:
            print("hermes-tmux review: no such worker %s#%s; nothing recorded"
                  % (args.delegation, args.task))
            return 2
        out = ""
        used_model = reviewer or None

    data = _load_review_file()
    entry = {
        "id": "rev_%s" % os.urandom(4).hex(),
        #: the delegation this review is ABOUT (the executed work)
        "delegation_id": args.delegation,
        "task_index": None if args.task < 0 else args.task,
        #: the run that performed the review, when it was a Hermes delegation.
        #: Role labels come from this field only, never from delegation_id.
        "reviewer_delegation": args.reviewer_delegation or None,
        "reviewer_model": used_model,
        "status": status,
        "verdict": _verdict_of(out) if action == "run" else None,
        "report": report,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    data.setdefault("reviews", []).append(entry)
    if not _save_review_file(data):
        print("hermes-tmux review: could not write registry")
        return 1
    print("hermes-tmux review: %s %s (%s)%s" % (
        action, args.delegation, status,
        " -> %s" % report if report else ""))
    return 0 if status == "recorded" else 1


# --------------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-tmux",
        description="Hermes-native tmux workspace: agent, logs, tests, debug, REA, subagents.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("up", help="build or repair the workspace in this tmux session"
                   ).set_defaults(func=cmd_up)
    sub.add_parser("down", help="close workspace windows we created"
                   ).set_defaults(func=cmd_down)
    status = sub.add_parser("status", help="print the subagent overview (works outside tmux)")
    status.add_argument("--json", action="store_true")
    status.add_argument("--all", action="store_true",
                        help="list every recorded worker, not only the recent ones")
    status.set_defaults(func=cmd_status)
    sub.add_parser("rerender", help="force one render of the overview"
                   ).set_defaults(func=cmd_rerender)
    sub.add_parser("debug-report", help="print windows, panes, ownership, watcher"
                   ).set_defaults(func=cmd_debug_report)
    followp = sub.add_parser("follow", help="pane-side display loop")
    followp.add_argument("--key", default="",
                         help="session key written by `up`")
    followp.set_defaults(func=cmd_follow)
    watchp = sub.add_parser("watch", help="event-driven coordinator loop")
    watchp.add_argument("--key", default="",
                        help="session key written by `up`; required when the "
                             "process has no TMUX_PANE (run-shell, daemons)")
    watchp.set_defaults(func=cmd_watch)

    for role in AUX_ROLES:
        aux = sub.add_parser(role, help="open or focus the hx:%s pane" % role)
        if role == "tests":
            aux.add_argument("--run", action="store_true",
                             help="run the suite in this pane and record tests.log")
        aux.set_defaults(func=cmd_aux, role=role)

    runp = sub.add_parser("run", help="build the workspace, then exec the agent")
    runp.add_argument("args", nargs=argparse.REMAINDER)
    runp.add_argument("--hermes", default="")
    runp.set_defaults(func=cmd_run)

    worker = sub.add_parser("worker", help="focus the Nth worker tab")
    worker.add_argument("index", type=int)
    worker.set_defaults(func=cmd_worker)
    openp = sub.add_parser("open", help="open the transcript tab of any worker by id")
    openp.add_argument("ref", help="delegation id or id#task, as shown by `status`")
    openp.set_defaults(func=cmd_open)
    closep = sub.add_parser("close", help="close a worker's tab (the view, never the worker)")
    closep.add_argument("target", nargs="?", default="",
                        help="window id (@N); default: the tab this command runs in")
    closep.add_argument("--client", default="", help="client to show the result to")
    closep.add_argument("--quiet", action="store_true",
                        help="print nothing and exit 0 (how the click binding calls it)")
    closep.set_defaults(func=cmd_close)
    markp = sub.add_parser("close-mark", help="clickable close mark on worker tabs: on, off, status")
    markp.add_argument("action", choices=["on", "off", "status"])
    markp.set_defaults(func=cmd_close_mark)
    tailp = sub.add_parser("tail", help="print a transcript readably and follow it")
    tailp.add_argument("log")
    tailp.add_argument("--no-follow", action="store_true")
    tailp.set_defaults(func=cmd_tail)

    review = sub.add_parser("review", help="record or run an independent review")
    review.add_argument("action", choices=["record", "run", "list"])
    review.add_argument("--delegation", default="")
    review.add_argument("--task", type=int, default=-1,
                        help="task index; -1 records a delegation-wide review")
    review.add_argument("--status", choices=["recorded", "failed"], default="recorded")
    review.add_argument("--report", default="")
    review.add_argument("--reviewer", default="")
    review.add_argument("--reviewer-delegation", default="",
                        help="delegation id that performed the review (provenance)")
    review.add_argument("--hermes", default="")
    review.set_defaults(func=cmd_review)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Dispatch one subcommand; 2 with help when none was given, 130 on Ctrl-C.

    Only `KeyboardInterrupt` is caught. Everything else propagates: a bug in a
    read path must be visible as a traceback in the pane that ran it, not
    converted into an exit status that looks like a policy refusal.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
