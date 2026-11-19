"""Thin, non-destructive wrapper around the tmux CLI.

Invariants enforced here, not at call sites:

* every command is an argv list (never a shell string), with a timeout;
* nothing raises: failures return ``(rc, stdout)`` and callers degrade;
* kills are gated on two independent facts, not one. ``kill-pane`` and
  ``kill-window`` require the target's *own* ``@hx`` option (read with
  ``show-options -p/-w``, never through a format) **and** provenance that
  matches how this tool launches panes (``tail``/``python`` in the foreground,
  or a non-empty ``pane_start_command``). ``respawn-pane -k`` asks only for the
  pane's own option; see ``respawn`` for why that is the weaker proof.

Why the format read is banned for ownership: ``#{@hx}`` resolves pane -> window
-> session, so every pane inside one of our windows reads back ``1`` and a
user's split would inherit the right to be killed. ``show-options -p`` answers
only for the pane itself (``invalid option`` when it has none).

Socket selection is tmux's own: bare ``tmux`` uses ``$TMUX`` when set, else
``$TMUX_TMPDIR/tmux-<uid>/default``. There is no ``-S``/``-L`` here, so a test
or a stray process inside a pane talks to the live server unless the caller
severs ``$TMUX`` (see ``tests/test_carbon_watch_and_perf.no_live_tmux``).

Pane/window ids (``%N``/``@N``) are the only addressing used, because they are
stable under renumber-windows and detach/reattach; indices are not.

Existence is a resolved-id comparison, never an exit status: on tmux 3.8
``display-message -t <missing>`` exits 0 and prints nothing, so status alone
says every id exists. A read that returns nothing is "unknown", not "gone" —
``format_of`` returns ``""`` for both a hiccup and a missing target, and the
callers that must tell them apart (``pane_exists``, ``window_exists``) compare
ids instead.
"""

from __future__ import annotations

import hashlib
import os
import re
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
    """Path of the tmux binary, or None. Resolved on every call, not cached."""
    return shutil.which("tmux")


def run(*args: str, timeout: float = 5.0) -> Tuple[int, str]:
    """Run one tmux subcommand. Returns (rc, stdout+stderr). Never raises.

    On success only stdout is returned, so a warning on stderr cannot be
    mistaken for data by the tab-separated parsers below. On failure the two
    streams are concatenated, because tmux puts the diagnosis there and callers
    surface it verbatim. A timeout or a missing binary becomes an ordinary
    non-zero result; some callers (``close_pane``) legitimately cannot tell a
    slow answer from a refusal, which is why destructive paths re-check
    ownership instead of trusting one call.
    """
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

    A ``$N`` target must resolve back to the same id. Anything else (a name, a
    grouped-view target) only proves tmux answered *something*, which is why the
    coordinator always passes the id it recorded, never a name.
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
    """True when this process is a child of a tmux client.

    Checks the environment only (``$TMUX`` and a tmux binary on PATH); it does
    not prove a server answers. Used to refuse building a workspace from a
    plain shell, where no pane exists to derive identity from.
    """
    env = os.environ if environ is None else environ
    return bool(env.get("TMUX")) and tmux_bin() is not None


def socket_path() -> str:
    """Path of the running tmux server's socket ('' when unknown).

    Read from tmux, not from ``$TMUX``: a ``run-shell`` child inherits
    ``$TMUX`` but the authoritative path is what the server reports. Empty means
    the query failed, which also means the server is gone or unreachable.
    """
    rc, out = run("display-message", "-p", "#{socket_path}")
    return out if rc == 0 else ""


def current_session_id() -> Optional[str]:
    """`$N` id of the session this process runs inside.

    Only an attached-client child has ``TMUX_PANE``; a ``run-shell`` child has
    none and gets ``None``, which is why identity is passed as ``--key`` to the
    coordinator rather than derived there.

    With grouped sessions this answers "the session tmux resolved the pane to",
    i.e. the most recently active member of the group, not the workspace's
    anchor. That is right for focus and wrong for identity; see
    ``session_key``.
    """
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    rc, out = run("display-message", "-p", "-t", pane, "#{session_id}")
    return out.strip() if rc == 0 and out.strip() else None


def current_session_name() -> Optional[str]:
    """Name of the session this process runs inside (None without a pane)."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    rc, out = run("display-message", "-p", "-t", pane, "#{session_name}")
    return out.strip() if rc == 0 and out.strip() else None


def server_pid() -> str:
    """Pid of the tmux server this client is talking to ('' when unknown).

    The coordinator's owner identity. tmux restarts session ids at ``$0`` when a
    server is replaced, so this pid — not the socket path — decides whether the
    server answering now is the one the coordinator was started for.
    """
    rc, out = run("display-message", "-p", "#{pid}")
    return out.strip() if rc == 0 else ""


#: Window option on the overview window: the key of the workspace it belongs to.
#: Windows are what a session group shares, so this is where identity can live
#: without depending on which session a command happens to resolve to.
KEY_OPTION = "@hx_key"


def _server_digest() -> str:
    """8 hex chars of sha1("socket_path|server_pid"), or ``nosock``.

    Both halves matter: the socket path alone is inherited by a replacement
    server, so it would make a new incarnation collide with the dead one's state
    directory and pane ids. ``nosock`` is the "cannot tell" value — a key built
    from it (``nosock-unknown``) is a bug symptom, not a workspace.
    """
    sock = socket_path()
    material = "%s|%s" % (sock, server_pid())
    return hashlib.sha1(material.encode("utf-8", "replace")).hexdigest()[:8] if sock else "nosock"


def group_sessions(session_id: str) -> List[str]:
    """Every session that shares its windows with ``session_id``, itself included, lowest id first.

    ``new-session -t <session>`` puts both sessions in one group with one set of
    windows. A session outside any group is its own list.

    Returned ascending by id, because "the lowest id of the group" is the
    workspace anchor fallback: it is the original session, and for an ungrouped
    session it is the session itself. An empty/unknown answer degrades to just
    the session asked about, so a tmux hiccup can never widen the group.
    """
    if not session_id:
        return []
    rc, out = run("list-sessions", "-F", "#{session_id}\t#{session_group}")
    rows = [line.split("\t") for line in out.splitlines()] if rc == 0 else []
    group = next((row[1] for row in rows if len(row) > 1 and row[0] == session_id), "")
    members = [row[0] for row in rows if group and len(row) > 1 and row[1] == group]
    if session_id not in members:
        members.append(session_id)

    def _number(sid: str) -> int:
        try:
            return int(sid.lstrip("$"))
        except ValueError:
            return 1 << 30
    return sorted(set(members), key=_number)


def marked_key(session_id: str) -> Optional[str]:
    """Key recorded on a window of ours among this session's windows, if any.

    Believed only on a window that carries our own marker, read as that window's
    own option (a format read would fall back to a session or global value), and
    only when it names this server instance.

    The digest prefix is checked with a regex, not a substring test, so a key
    left by a previous server (same socket, different pid) is not adopted even
    though it looks like ours. First match wins; two workspaces cannot normally
    share a session group, and if they do, one of them is already wrong.
    """
    if not session_id:
        return None
    wanted = re.compile(r"^%s-[A-Za-z0-9_]+$" % re.escape(_server_digest()))
    for win in list_windows(session_id):
        if not win["ours"]:
            continue
        rc, out = run("show-options", "-wv", "-t", win["window_id"], KEY_OPTION)
        value = out.strip() if rc == 0 else ""
        if wanted.match(value):
            return value
    return None


def mark_key(window_id: str, key: str) -> bool:
    """Stamp the workspace key on our overview window (identity travels with it)."""
    return window_option(window_id, KEY_OPTION, key)


def session_key(environ=None, session_id: Optional[str] = None) -> str:
    """Filesystem-safe isolation key of the workspace this process belongs to.

    Identity belongs to the workspace, not to a session or a client. With
    grouped sessions a pane is in several sessions at once and tmux resolves it
    to whichever was active last, so "the session of this pane" is not stable.
    Resolution, in order:

    1. the key recorded on the workspace's own overview window (``KEY_OPTION``),
       which every session of the group sees;
    2. otherwise, for a workspace that does not exist yet (or was built before
       the marker existed), the lowest session id of the group. That is the
       original session, and for an ungrouped session it is the session itself,
       so keys written before this rule are still found.

    The server pid is part of the key on purpose: tmux session ids (``$0``,
    ``$1``) start over when a server restarts, so a key built from the socket
    path and the id alone collides with the previous incarnation and makes a
    new session adopt a dead session's layout, including its pane ids.

    When neither the marker nor a group exists (no pane, no server, or every
    query failed) the id is ``unknown``: the key is still well-formed but names
    nothing. Callers that would create state from it must check ``in_tmux``
    first; the historical ``sessions/nosock-unknown/`` directory came from
    exactly that mistake.
    """
    digest = _server_digest()
    if session_id is None:
        current = current_session_id()
        marked = marked_key(current) if current else None
        if marked:
            return marked
        members = group_sessions(current) if current else []
        session_id = members[0] if members else None
    sid = session_id or "unknown"
    safe = "".join(ch if ch.isalnum() else "_" for ch in sid)
    return "%s-%s" % (digest, safe)


def format_of(target: str, fmt: str) -> str:
    """One ``display-message -p`` format expansion, '' on any failure.

    '' is the only failure signal, and it means "no answer", not "gone": tmux
    3.8 exits 0 for a missing target. Callers that need to distinguish the two
    compare the resolved id (``pane_exists``/``window_exists``); callers that
    only decorate treat '' as "skip this pass".
    """
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
    """Windows of a session as rows: id, name, index, ours, session_id.

    ``#{@hx}`` is still in the format string but its *value* is discarded — the
    format falls back to the session's option, so a bare session-wide marker
    would mark every window. ``ours`` comes from ``_has_own_option`` instead.
    """
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
    """Panes of a window (or all panes of a session) as rows.

    Fields: ``pane_id``, ``window_id``, ``index``, ``pid``, ``ours``,
    ``command`` (foreground process), ``title``. As in ``list_windows`` the
    ``@hx`` value from the format is not trusted; ownership is the pane's own
    option, so a pane the reader split into one of our windows is not "ours".
    """
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


def windows_by_prefix(session: str, prefix: str) -> List[str]:
    """Ids of our windows whose name starts with ``prefix`` (never foreign).

    Name prefixes are only an enumeration device (tabs are renamed to the task),
    so every candidate is still gated on the window's own marker.
    """
    return [win["window_id"] for win in list_windows(session)
            if win["ours"] and win["name"].startswith(prefix)]


def free_window_name(session: str, base: str) -> str:
    """A window name that no foreign window in this session is using.

    Only foreign names are avoided. Two windows of ours may end up sharing a
    name (two workers whose goals label identically); ids are the address, so
    that is cosmetic. The suffix is ``#N`` rather than tmux's usual ``-N``
    because it is also the character labels use in the tab bar.
    """
    taken = {win["name"] for win in list_windows(session) if not win["ours"]}
    if base not in taken:
        return base
    suffix = 2
    while "%s#%d" % (base, suffix) in taken:
        suffix += 1
    return "%s#%d" % (base, suffix)


def new_window(session: str, name: str, command: Optional[str] = None,
               cwd: Optional[str] = None,
               env: Optional[Dict[str, str]] = None,
               after_window: Optional[str] = None) -> Optional[str]:
    """Create a detached window we own. Returns its window_id.

    ``env`` is passed with ``-e``: a new window is forked from the *server*, so
    it does not see variables exported in the pane that asked for it. Without
    this, HERMES_HOME/PYTHONPATH silently fall back to the server's values and
    the pane reads a different Hermes profile than the caller.

    When ``after_window`` is given and the index immediately after it is free,
    insert there (``-a -t <after_window>``) so permanent tabs stay beside
    ``agents`` without ever renumbering or shifting occupied tabs.

    The window is created detached (``-d``) and never steals focus; ``-P -F``
    returns its id in the same call. Both the window and its first pane are
    marked here, because a window option does not make its panes ours.
    """
    target = session
    extra: List[str] = []
    if after_window:
        wins = list_windows(session)
        by_id = {w["window_id"]: w for w in wins}
        if after_window in by_id:
            try:
                want_idx = int(by_id[after_window]["index"]) + 1
                taken = {int(w["index"]) for w in wins}
                if want_idx not in taken:
                    extra = ["-a"]
                    target = after_window
            except (ValueError, KeyError):
                pass
    args = ["new-window", "-d"] + extra + ["-P", "-F", "#{window_id}", "-t", target, "-n", name]
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
    """Create a detached pane in our window and mark it. Returns its pane id.

    Used only for rebuilding the overview and for aux panes; a split inside one
    of our windows by the *reader* gets no ``@hx`` of its own, which is the
    property the ownership guard depends on.
    """
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
    """Set ``@hx=1`` on the window itself (never inherited by its panes)."""
    rc, _ = run("set-option", "-w", "-t", window_id, _SENTINEL, "1")
    return rc == 0


def mark_pane(pane_id: str) -> bool:
    """Set ``@hx=1`` on the pane itself; this is the only marker ownership reads."""
    rc, _ = run("set-option", "-p", "-t", pane_id, _SENTINEL, "1")
    return rc == 0


def owns_window(window_id: str) -> bool:
    """True only when this window carries the marker as its own option."""
    return bool(window_id) and _has_own_option("window", window_id, _SENTINEL)


def owns_pane(pane_id: str) -> bool:
    """True only when this pane carries the marker as its own option.

    Necessary, not sufficient, for a kill: see ``_killable``.
    """
    return bool(pane_id) and _has_own_option("pane", pane_id, _SENTINEL)


#: Foreground processes this tool itself starts in a pane. A split made inside
#: one of our windows does NOT get the pane option (measured on tmux 3.8:
#: `show-options -p` on the new pane says "invalid option"); only a FORMAT read
#: of `#{@hx}` falls back to the window's value, which is why ownership is read
#: with show-options. The command check is a second, independent proof kept in
#: case a pane ever does carry a marker it should not.
_KILLABLE_COMMANDS = ("tail", "python", "python3", "python3.9", "python3.10",
                      "python3.11", "python3.12", "python3.13", "python3.14",
                      "python3.15", "hxmail", "testbench")


def _killable(pane_id: str) -> Tuple[bool, str]:
    """Is this pane something we started, not merely something marked?

    The pane's own marker is the first proof (a user's split does not have
    one). It is not trusted alone. Two independent properties must hold:
    our marker, and provenance that matches how we launch panes — either the
    foreground process is one we run (tail / python), or the pane was started
    with an explicit command, which our panes always are and an interactive user
    split never is.

    Failure is always "leave it alone": an unreadable ``pane_current_command``
    (pane just died) or an unexpected foreground both answer no. The cost of a
    false negative is a tab that stays until the next pass; the cost of a false
    positive is the reader's work.
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
    """Kill a pane only if we created it. Returns (closed, reason).

    ``(False, "gone")`` means the pane was already absent; any other failure is
    a refusal and the caller must not treat it as a close (``reconcile_tabs``
    re-checks existence before forgetting a tab).
    """
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
    """Kill a window only if every pane in it is ours.

    Not on the live coordinator path: ``down`` closes panes one at a time so a
    window that still holds a reader pane survives, and a window disappears by
    itself when its last pane goes.
    """
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
    """Replace the foreground process of a pane we own.

    Asks only for the pane's own marker, not the ``_killable`` provenance
    check: a respawn is how a marked pane is *given* the command in the first
    place (rebuilding a dead overview/aux pane), so requiring an
    already-running ``python`` would make recovery impossible. The blast radius
    is the same pane the marker already authorises; nothing else is touched.
    """
    if not owns_pane(pane_id):
        return False
    rc, _ = run("respawn-pane", "-k", "-t", pane_id, command)
    return rc == 0


def set_title(pane_id: str, title: str) -> bool:
    """Set the pane title (informational; not used for ownership)."""
    rc, _ = run("select-pane", "-t", pane_id, "-T", title)
    return rc == 0


def rename_window(window_id: str, name: str) -> bool:
    """Rename a window that still exists. Ownership is the caller's business."""
    if not window_exists(window_id):
        return False
    rc, _ = run("rename-window", "-t", window_id, name)
    return rc == 0


def select_window(window_id: str) -> bool:
    """Make a window current in whatever session the target resolves to."""
    rc, _ = run("select-window", "-t", window_id)
    return rc == 0


def select_pane(pane_id: str) -> bool:
    """Make a pane active inside its window."""
    rc, _ = run("select-pane", "-t", pane_id)
    return rc == 0


def window_option(window_id: str, option: str, value: str) -> bool:
    """Set a window-scoped option. Never global, never ``-s``."""
    rc, _ = run("set-option", "-w", "-t", window_id, option, value)
    return rc == 0


def pane_option(pane_id: str, option: str, value: str) -> bool:
    """Set a pane-scoped option (``@hx_head``, ``@hx``)."""
    rc, _ = run("set-option", "-p", "-t", pane_id, option, value)
    return rc == 0


def pane_size(pane_id: str) -> Tuple[int, int]:
    """(width, height) of a pane, (80, 24) when unknown.

    The default is a lie the renderer can survive: composing for 80x24 clips,
    while composing for 0 wide would raise inside the renderer. The follow loop
    is the only caller that cares, and it redraws on the next tick.
    """
    rc, out = run("display-message", "-p", "-t", pane_id, "#{pane_width}x#{pane_height}")
    if rc != 0 or "x" not in out:
        return 80, 24
    try:
        w, h = out.strip().split("x", 1)
        return int(w), int(h)
    except ValueError:
        return 80, 24


def run_shell_background(command: str) -> bool:
    """Start a detached process owned by the tmux server (survives detach).

    The child inherits the server's environment and has ``$TMUX`` but no
    ``$TMUX_PANE``. tmux reports the job's output (and any non-zero exit) in the
    active pane in view mode, so callers wrap the command to log to a file and
    end 0 — see ``cli._start_watcher``.
    """
    rc, _ = run("run-shell", "-b", command)
    return rc == 0
