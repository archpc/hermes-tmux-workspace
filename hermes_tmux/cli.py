"""Command line surface for the Hermes tmux workspace.

Every command is safe to run at any time: with the gate off, outside tmux, or
with a half-built workspace, it degrades to a message and exit 0. Nothing here
kills a pane, window, or session it did not create.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import gate, tmuxio, watch
from .env import pane_env, python as _env_python, repo_root as _env_repo_root

WORKERS_WINDOW = "hx:workers"
AUX_ROLES = ("logs", "tests", "debug", "rea")


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
    from .state import hermes_home
    root = _repo_root()
    env = {
        "HERMES_HOME": str(hermes_home()),
        "HERMES_TMUX": "1" if gate.enabled() else "0",
        "HX_PYTHON": _python(),
        "PYTHONPATH": str(root),
    }
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
    return tmuxio.run_shell_background(_self_command("watch", "--key", key))


# --------------------------------------------------------------------------- up

def cmd_up(args) -> int:
    if not gate.enabled():
        print("hermes-tmux: %s" % gate.disabled_reason())
        return 0
    if not tmuxio.in_tmux():
        print("hermes-tmux: not inside tmux; nothing to build.")
        print("  status still works outside tmux:  hermes-tmux status")
        return 0

    key = tmuxio.session_key()
    _ensure_state_dir(key)
    layout = watch.read_layout(key)
    session_id = tmuxio.current_session_id() or ""
    session_name = tmuxio.current_session_name() or ""
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
    for pane in panes:
        if pane["ours"] and "python" in (pane["command"] or "").lower():
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
        # survived with someone else's work in it (a user shell, or a tail pane
        # already bound to a worker key in the layout).
        overview = tmuxio.split_window(window_id, follow_cmd,
                                       horizontal=False, env=_pane_env()) or ""
        if not overview:
            print("hermes-tmux: could not rebuild the overview in %s" % WORKERS_WINDOW)
            return 1
        tmuxio.mark_pane(overview)
        print("hermes-tmux: rebuilt the overview pane (%s)" % overview)
    layout["overview_pane"] = overview

    # The overview window is overview-only in this layout. Version 1 kept worker
    # tail panes here; retire ours so the window holds exactly the overview.
    # Never touches a pane the user split into the window.
    watch.trim_overview_window(dict(layout, workers_window=window_id,
                                    overview_pane=overview))

    # Pane borders carry worker identity: id, task, model.
    tmuxio.window_option(window_id, "pane-border-status", "top")
    tmuxio.window_option(window_id, "pane-border-format",
                         " #{pane_title} ")

    def _apply(current):
        current["session_id"] = session_id
        current["session_name"] = session_name
        current["workers_window"] = window_id
        current["overview_pane"] = overview
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
    closed_panes = 0
    before_windows = {win["window_id"] for win in
                      tmuxio.list_windows(tmuxio.current_session_id() or "")}
    # Close our panes first, one by one. A window that also holds a user pane is
    # not closed as a whole (that would kill their shell with it), but our own
    # panes in it still go.
    targets: List[str] = []
    session = tmuxio.current_session_id() or ""
    for name in [WORKERS_WINDOW] + ["hx:" + r for r in AUX_ROLES]:
        win = tmuxio.find_window_by_name(session, name)
        if win:
            targets.append(win["window_id"])
    # Worker windows are recorded by slot, but a layout that lost an entry still
    # has a name: close those too, only when the marker says they are ours.
    targets.extend(tmuxio.windows_by_prefix(session, watch.WORKER_WINDOW_PREFIX))
    for window_id in [layout.get("workers_window")] \
            + list((layout.get("worker_windows") or {}).values()) \
            + list((layout.get("aux") or {}).values()):
        if window_id and tmuxio.window_exists(window_id):
            targets.append(window_id)
    for window_id in dict.fromkeys(targets):
        # Remove only panes proven to be ours, one at a time.
        for pane in tmuxio.list_panes(window_id):
            if tmuxio.close_pane(pane["pane_id"])[0]:
                closed_panes += 1

    for name in [WORKERS_WINDOW] + ["hx:" + r for r in AUX_ROLES]:
        win = tmuxio.find_window_by_name(session, name)
        if not win:
            continue
        # A window that still holds any pane we did not create is left standing:
        # closing it would take the user's shell with it. Our panes are already
        # gone, so what remains is theirs.
        if tmuxio.foreign_panes(win["window_id"]):
            print("hermes-tmux: leaving %s in place; it holds a pane we do not own"
                  % win["name"])
            continue
        tmuxio.close_window(win["window_id"])
    # Also close by recorded id: a window may have been renamed since, and the
    # id is the address that survives renumbering.
    for window_id in [layout.get("workers_window")] \
            + list((layout.get("worker_windows") or {}).values()) \
            + list((layout.get("aux") or {}).values()):
        if window_id:
            tmuxio.close_window(window_id)
    pid = _watcher_pid(key)
    if pid is not None:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
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

    workers = state_mod.load_workers()
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
    text = render_mod.render(workers, live_root=str(live_root()))
    print(text)
    if not gate.enabled():
        print()
        print("gate: %s" % gate.disabled_reason())
    return 0


def cmd_rerender(args) -> int:
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


def cmd_worker(args) -> int:
    if not tmuxio.in_tmux():
        print("hermes-tmux: not inside tmux.")
        return 0
    key = tmuxio.session_key()
    layout = watch.read_layout(key)

    def _order(pane_id: str) -> int:
        digits = pane_id.lstrip("%")
        return int(digits) if digits.isdigit() else 0

    assign = layout.get("worker_assign") or {}
    tail_panes = layout.get("tail_panes") or {}

    def _slot(pane_id: str) -> int:
        slot = assign.get(tail_panes.get(pane_id, ""), "")
        text = str(slot).lstrip("0")
        return int(text) if text.isdigit() else 0

    # Worker order is the layout's own: window slot first, then pane order, so
    # "worker 2" names the same transcript across a detach or a lifecycle change.
    panes = sorted(tail_panes.keys(), key=lambda pane_id: (_slot(pane_id), _order(pane_id)))
    idx = args.index
    if idx < 1 or idx > len(panes):
        print("hermes-tmux: no worker pane %d (have %d)" % (idx, len(panes)))
        return 1
    pane_id = panes[idx - 1]
    # A worker lives in its own window now. `select-pane` alone does not switch
    # the session's current window when the target is elsewhere, so focus is two
    # explicit steps: the window, then the pane inside it.
    window_id = tmuxio.format_of(pane_id, "#{window_id}")
    if window_id:
        tmuxio.select_window(window_id)
    tmuxio.select_pane(pane_id)
    print("hermes-tmux: focused worker pane %d (%s)" % (idx, pane_id))
    return 0


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
        for candidate in load_workers():
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

    worker = sub.add_parser("worker", help="focus the Nth visible worker pane")
    worker.add_argument("index", type=int)
    worker.set_defaults(func=cmd_worker)

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
