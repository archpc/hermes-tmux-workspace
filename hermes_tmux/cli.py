"""Command line surface for the Hermes tmux workspace.

Every command is safe to run at any time: with the gate off, outside tmux, or
with a half-built workspace, it degrades to a message and exit 0. Nothing here
kills a pane, window, or session it did not create.
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
AUX_ROLES = ("logs", "tests", "debug", "rea", "mail")


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
        current["session_id"] = session_id
        current["session_name"] = session_name
        current["workers_window"] = window_id
        current["overview_pane"] = overview
        # Work that finished before this moment is history, not a tab.
        current.setdefault("created", time.time())
        current["ascii"] = labels.ascii_only()
        return current
    layout = watch.update_layout(key, _apply)
    _start_watcher(key)
    watch.render_and_publish(key, session_label=session_name)
    print("hermes-tmux: workspace ready  (%s)" % window_id)
    return 0


# --------------------------------------------------------------------------- down

def cmd_down(args) -> int:
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

def _aux_command(role: str, shell: str, run: bool = False) -> Tuple[str, str]:
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
        log = hermes_home() / "cache" / watch.STATE_DIR_NAME / "tests.log"
        cmd = q(_python()) + " -m unittest discover -s tests -t ."
        if run:
            return str(repo), "( " + cmd + " ) 2>&1 | tee " + q(str(log)) + "; exec " + shell
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

    # role == "rea": show what is actually installed, then a shell.
    return str(home), (
        "printf '%s\\n' '--- REA ---'; "
        "if command -v rea >/dev/null 2>&1; then rea --version 2>&1 | head -3; "
        "else printf '%s\\n' 'rea CLI not on PATH'; fi; "
        "printf '%s\\n' 'REA MCP state is served by Hermes (hermes mcp test rea), not by this pane.'; "
        "exec " + shell)


def cmd_aux(args) -> int:
    role = args.role
    if not gate.enabled():
        print("hermes-tmux: %s" % gate.disabled_reason())
        return 0
    if not tmuxio.in_tmux():
        print("hermes-tmux: not inside tmux.")
        return 0
    key = tmuxio.session_key()
    _ensure_state_dir(key)
    layout = watch.read_layout(key)
    session_id = tmuxio.current_session_id() or ""
    name = "hx:" + role

    run_now = bool(getattr(args, "run", False))
    win = tmuxio.find_window_by_name(session_id, name)
    if win:
        panes = tmuxio.list_panes(win["window_id"])
        ours = [pane for pane in panes if pane["ours"]]
        if run_now and ours:
            # `--run` has to run. Focusing the pane that only prints the hint
            # would claim a run that never happened.
            _cwd, run_cmd = _aux_command(role, os.environ.get("SHELL") or "/bin/zsh",
                                         run=True)
            tmuxio.respawn(ours[0]["pane_id"], run_cmd)
            tmuxio.select_window(win["window_id"])
            print("hermes-tmux: running the suite in %s" % name)
            return 0
        tmuxio.select_window(win["window_id"])
        print("hermes-tmux: focused %s" % name)
        return 0

    shell = os.environ.get("SHELL") or "/bin/zsh"
    cwd, command = _aux_command(role, shell, run=getattr(args, "run", False))

    window_id = tmuxio.new_window(session_id, tmuxio.free_window_name(session_id, name),
                                  cwd=cwd, command=command, env=_pane_env())
    if not window_id:
        print("hermes-tmux: could not create %s" % name)
        return 1
    def _apply(current):
        current.setdefault("aux", {})[role] = window_id
        return current
    watch.update_layout(key, _apply)
    tmuxio.select_window(window_id)
    print("hermes-tmux: opened %s" % name)
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
    """`up`, then exec the agent in this pane. The workspace never owns this pane."""
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
    """Worker tabs as the status line shows them: by window index."""
    indexes = watch.tab_indexes(layout)

    def _index(item) -> int:
        text = str(indexes.get(item[0], ""))
        return int(text) if text.isdigit() else 10 ** 6

    tabs = [(k, v) for k, v in (layout.get("tabs") or {}).items() if isinstance(v, dict)]
    return sorted(tabs, key=_index)


def cmd_worker(args) -> int:
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
    """True when any owned window or live watcher remains on this tmux server."""
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
    """Close the view of one worker tab. The worker itself is not touched."""
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
    """Turn the clickable mark on or off for this server without rebuilding anything."""
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
