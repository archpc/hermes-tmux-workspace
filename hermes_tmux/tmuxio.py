"""Thin, non-destructive wrapper around the tmux CLI.

Invariants enforced here, not at call sites:

* every command is an argv list (never a shell string), with a timeout;
* nothing raises: failures return ``(rc, stdout)`` and callers degrade;
* destructive calls (``kill-pane``, ``respawn-pane -k``, ``kill-window``) go
  through one guard that refuses any target whose pane/window option ``@hx``
  is not ``1``, so panes the workspace did not create are untouchable.

Pane/window ids (``%N``/``@N``) are the only addressing used, because they are
stable under renumber-windows and detach/reattach; indices are not.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Tuple

TMUX_OPTION = "@hx"
_SENTINEL = "@hx"  # user option carrying "1" on every pane/window we own

#: tmux format fragment that reads that option. The leading ``#`` is required:
#: ``{@hx}`` is literal text, so reading it without ``#`` compares against the
#: marker's own spelling and every ownership check silently answers "not ours".
_SENTINEL_FMT = "#{%s}" % _SENTINEL


def tmux_bin() -> Optional[str]:
    return shutil.which("tmux")


def run(*args: str, timeout: float = 5.0) -> Tuple[int, str]:
    """Run one tmux subcommand. Returns (rc, stdout+stderr). Never raises."""
    binary = tmux_bin()
    if binary is None:
        return 127, "tmux not found"
    try:
        proc = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        # stdout only on success: a warning on stderr must not be mistaken for
        # data by the tab-separated parsers above.
        out = (proc.stdout or "") if proc.returncode == 0 else \
            (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out.rstrip("\n")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "tmux error: %s" % exc


def session_alive(session_id: str = "") -> bool:
    """True while the session (and therefore its server) still answers.

    ``run-shell -b`` detaches the child from the server's lifetime, so a
    coordinator started that way outlives the server unless it checks. This is a
    liveness probe, not a data poll: nothing is read but whether tmux answers.
    """
    target = session_id or os.environ.get("TMUX_SESSION_HINT", "")
    if target:
        # Same rule as pane_exists: `display-message -t <missing>` exits 0 and
        # prints nothing, so the status alone said every session was alive and a
        # coordinator outlived the session it served until the whole server died.
        resolved = format_of(target, "#{session_id}")
        if not resolved:
            return False
        if target.startswith("$"):
            return resolved == target
        return True
    rc, out = run("list-sessions", "-F", "#{session_id}")
    return rc == 0 and bool(out.strip())


def in_tmux(environ=None) -> bool:
    """True when this process is a child of a tmux client."""
    env = os.environ if environ is None else environ
    return bool(env.get("TMUX")) and tmux_bin() is not None


def socket_path() -> str:
    """Path of the running tmux server's socket ('' when unknown)."""
    rc, out = run("display-message", "-p", "#{socket_path}")
    return out if rc == 0 else ""


def current_session_id() -> Optional[str]:
    """`$N` id of the session this process runs inside."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    rc, out = run("display-message", "-p", "-t", pane, "#{session_id}")
    return out.strip() if rc == 0 and out.strip() else None


def current_session_name() -> Optional[str]:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    rc, out = run("display-message", "-p", "-t", pane, "#{session_name}")
    return out.strip() if rc == 0 and out.strip() else None


def server_pid() -> str:
    """Pid of the tmux server this client is talking to ('' when unknown)."""
    rc, out = run("display-message", "-p", "#{pid}")
    return out.strip() if rc == 0 else ""


def session_key(environ=None) -> str:
    """Filesystem-safe, per-server-instance, per-session isolation key.

    The server pid is part of the key on purpose: tmux session ids (``$0``,
    ``$1``) start over when a server restarts, so a key built from the socket
    path and the id alone collides with the previous incarnation and makes a
    new session adopt a dead session's layout, including its pane ids.
    """
    sock = socket_path()
    material = "%s|%s" % (sock, server_pid())
    digest = hashlib.sha1(material.encode("utf-8", "replace")).hexdigest()[:8] if sock else "nosock"
    sid = current_session_id() or "unknown"
    safe = "".join(ch if ch.isalnum() else "_" for ch in sid)
    return "%s-%s" % (digest, safe)


def format_of(target: str, fmt: str) -> str:
    rc, out = run("display-message", "-p", "-t", target, fmt)
    return out.strip() if rc == 0 else ""


def pane_exists(pane_id: str) -> bool:
    """True only when tmux resolves this id to that pane.

    The exit status is useless here: on tmux 3.8 ``display-message -t <missing>``
    exits 0 and prints nothing. Testing the status made every id "exist", which
    silently disabled stale-pane recovery and window accounting. The resolved id
    is the authority.
    """
    if not pane_id:
        return False
    return format_of(pane_id, "#{pane_id}") == pane_id


def window_exists(window_id: str) -> bool:
    """True only when tmux resolves this id to that window (see pane_exists)."""
    if not window_id:
        return False
    return format_of(window_id, "#{window_id}") == window_id


def list_windows(session: str) -> List[dict]:
    fmt = "\t".join([
        "#{window_id}", "#{window_name}", "#{window_index}",
        _SENTINEL_FMT, "#{session_id}",
    ])
    rc, out = run("list-windows", "-t", session, "-F", fmt)
    if rc != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        rows.append({
            "window_id": parts[0],
            "name": parts[1],
            "index": parts[2],
            # The format value falls back to the session's option, so ownership
            # is resolved per window instead of read out of the format.
            "ours": _has_own_option("window", parts[0], _SENTINEL),
            "session_id": parts[4],
        })
    return rows


def wait_for_panes(target: str, timeout: float = 2.0) -> List[dict]:
    """List panes, waiting briefly for tmux to finish creating the first one.

    ``new-window`` returns the window id before its pane exists in the pane
    table, so an immediate ``list-panes`` can legitimately come back empty and
    make a caller think the window is broken.
    """
    deadline = time.time() + timeout
    panes = list_panes(target)
    while not panes and time.time() < deadline:
        time.sleep(0.02)
        panes = list_panes(target)
    return panes


def list_panes(target: str) -> List[dict]:
    fmt = "\t".join([
        "#{pane_id}", "#{window_id}", "#{pane_index}", "#{pane_pid}",
        _SENTINEL_FMT, "#{pane_current_command}", "#{pane_title}",
    ])
    rc, out = run("list-panes", "-t", target, "-F", fmt)
    if rc != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        rows.append({
            "pane_id": parts[0],
            "window_id": parts[1],
            "index": parts[2],
            "pid": parts[3],
            "ours": _has_own_option("pane", parts[0], _SENTINEL),
            "command": parts[5],
            "title": parts[6],
        })
    return rows


def find_window_by_name(session: str, name: str) -> Optional[dict]:
    """Find our window by name, tolerating the live badge suffix.

    The watcher renames ``hx:workers`` to ``hx:workers 2`` / ``hx:workers !`` to
    carry a running/failed count, so an exact match alone would lose the window
    and make ``down`` a no-op.

    Both branches require our marker: a user window that happens to be called
    ``hx:workers`` must never be adopted, renamed, or closed by this tool.
    """
    windows = list_windows(session)
    for win in windows:
        if win["name"] == name and win["ours"]:
            return win
    for win in windows:
        if win["name"].startswith(name + " ") and win["ours"]:
            return win
    return None


def free_window_name(session: str, base: str) -> str:
    """A window name that no foreign window in this session is using."""
    taken = {win["name"] for win in list_windows(session) if not win["ours"]}
    if base not in taken:
        return base
    suffix = 2
    while "%s#%d" % (base, suffix) in taken:
        suffix += 1
    return "%s#%d" % (base, suffix)


def new_window(session: str, name: str, command: Optional[str] = None,
               cwd: Optional[str] = None,
               env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Create a detached window we own. Returns its window_id.

    ``env`` is passed with ``-e``: a new window is forked from the *server*, so
    it does not see variables exported in the pane that asked for it. Without
    this, HERMES_HOME/PYTHONPATH silently fall back to the server's values and
    the pane reads a different Hermes profile than the caller.
    """
    args = ["new-window", "-d", "-P", "-F", "#{window_id}", "-t", session, "-n", name]
    for key, value in (env or {}).items():
        args.extend(["-e", "%s=%s" % (key, value)])
    if cwd:
        args.extend(["-c", cwd])
    if command:
        args.append(command)
    rc, out = run(*args)
    if rc != 0:
        return None
    window_id = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not window_id:
        return None
    mark_window(window_id)
    # The initial pane is ours too, but the window option does not make it so:
    # ownership is read from the pane's own option. Mark it here so aux windows
    # (which never split) are recognised as ours.
    panes = wait_for_panes(window_id)
    if panes:
        mark_pane(panes[0]["pane_id"])
    return window_id


def split_window(window_id: str, command: Optional[str] = None,
                 horizontal: bool = True,
                 env: Optional[Dict[str, str]] = None) -> Optional[str]:
    args = ["split-window", "-d", "-P", "-F", "#{pane_id}", "-t", window_id]
    args.append("-h" if horizontal else "-v")
    for key, value in (env or {}).items():
        args.extend(["-e", "%s=%s" % (key, value)])
    if command:
        args.append(command)
    rc, out = run(*args)
    if rc != 0:
        return None
    pane_id = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not pane_id:
        return None
    mark_pane(pane_id)
    return pane_id


def _has_own_option(kind: str, target: str, option: str) -> bool:
    """True when the target itself carries the option.

    ``-p``/``-w`` scope the lookup to the pane/window; a format read would fall
    back through pane -> window -> session, which makes every pane in a marked
    window look marked even when it has no option of its own.
    """
    flag = "-p" if kind == "pane" else "-w"
    rc, out = run("show-options", flag, "-t", target, option)
    if rc != 0 or "invalid option" in out.lower():
        return False
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] == option and parts[1].strip() == "1":
            return True
    return False


def mark_window(window_id: str) -> bool:
    rc, _ = run("set-option", "-w", "-t", window_id, _SENTINEL, "1")
    return rc == 0


def mark_pane(pane_id: str) -> bool:
    rc, _ = run("set-option", "-p", "-t", pane_id, _SENTINEL, "1")
    return rc == 0


def owns_window(window_id: str) -> bool:
    return bool(window_id) and _has_own_option("window", window_id, _SENTINEL)


def owns_pane(pane_id: str) -> bool:
    return bool(pane_id) and _has_own_option("pane", pane_id, _SENTINEL)


#: Foreground processes this tool itself starts in a pane. A pane option is
#: inherited by panes a user splits inside our window, so the marker alone cannot
#: authorise a kill; the command must also be one of these.
_KILLABLE_COMMANDS = ("tail", "python", "python3", "python3.9", "python3.10",
                      "python3.11", "python3.12", "python3.13", "python3.14",
                      "python3.15")


def _killable(pane_id: str) -> Tuple[bool, str]:
    """Is this pane something we started, not merely something marked?

    Pane options are inherited by a split made inside our window, so the marker
    alone can be forged by a user's split. Two independent properties must hold:
    our marker, and provenance that matches how we launch panes — either the
    foreground process is one we run (tail / python), or the pane was started
    with an explicit command, which our panes always are and an interactive user
    split never is.
    """
    if not owns_pane(pane_id):
        return False, "not ours"
    command = format_of(pane_id, "#{pane_current_command}").strip().lower()
    base = command.rsplit("/", 1)[-1]
    if base in _KILLABLE_COMMANDS or base.startswith("python"):
        return True, ""
    if format_of(pane_id, "#{pane_start_command}").strip():
        return True, ""
    return False, "no ownership provenance (%s)" % (command or "?")


def close_pane(pane_id: str) -> Tuple[bool, str]:
    """Kill a pane only if we created it. Returns (closed, reason)."""
    if not pane_exists(pane_id):
        return False, "gone"
    ok, why = _killable(pane_id)
    if not ok:
        return False, why
    rc, out = run("kill-pane", "-t", pane_id)
    return rc == 0, out


def foreign_panes(window_id: str) -> List[str]:
    """Pane ids in this window that this tool must not touch."""
    return [pane["pane_id"] for pane in list_panes(window_id)
            if not _killable(pane["pane_id"])[0]]


def close_window(window_id: str) -> Tuple[bool, str]:
    """Kill a window only if every pane in it is ours."""
    if not window_exists(window_id):
        return False, "gone"
    panes = list_panes(window_id)
    if not panes:
        return False, "no panes"
    for pane in panes:
        ok, why = _killable(pane["pane_id"])
        if not ok:
            return False, why
    rc, out = run("kill-window", "-t", window_id)
    return rc == 0, out


def respawn(pane_id: str, command: str) -> bool:
    """Replace the foreground process of a pane we own."""
    if not owns_pane(pane_id):
        return False
    rc, _ = run("respawn-pane", "-k", "-t", pane_id, command)
    return rc == 0


def set_title(pane_id: str, title: str) -> bool:
    rc, _ = run("select-pane", "-t", pane_id, "-T", title)
    return rc == 0


def rename_window(window_id: str, name: str) -> bool:
    if not window_exists(window_id):
        return False
    rc, _ = run("rename-window", "-t", window_id, name)
    return rc == 0


def select_window(window_id: str) -> bool:
    rc, _ = run("select-window", "-t", window_id)
    return rc == 0


def select_pane(pane_id: str) -> bool:
    rc, _ = run("select-pane", "-t", pane_id)
    return rc == 0


def window_option(window_id: str, option: str, value: str) -> bool:
    rc, _ = run("set-option", "-w", "-t", window_id, option, value)
    return rc == 0


def pane_size(pane_id: str) -> Tuple[int, int]:
    """(width, height) of a pane, (80, 24) when unknown."""
    rc, out = run("display-message", "-p", "-t", pane_id, "#{pane_width}x#{pane_height}")
    if rc != 0 or "x" not in out:
        return 80, 24
    try:
        w, h = out.strip().split("x", 1)
        return int(w), int(h)
    except ValueError:
        return 80, 24


def run_shell_background(command: str) -> bool:
    """Start a detached process owned by the tmux server (survives detach)."""
    rc, _ = run("run-shell", "-b", command)
    return rc == 0
