"""A clickable close mark on the current worker tab.

What the mark does, and what it never does:

* It closes the VIEW: the pane that follows one worker's transcript. The worker
  is a Hermes subagent in another process; it keeps running, stays listed in
  the overview, and ``hermes-tmux open <id>`` brings its tab back. This is the
  existing "a tab the reader closed stays closed" rule, reachable by mouse.
* It does not cancel the job (this tool has no way to) and it does not kill a
  window: only panes that pass the ownership proof in ``tmuxio.close_pane``.

tmux mechanics (measured on 3.8):

* a status format can mark cells ``#[range=user|X]`` (X at most 15 bytes); a
  click there fires the ordinary ``MouseDown1Status`` key with
  ``#{mouse_status_range}`` = X;
* the window's own range must be restored by hand after the mark, or the
  padding cell after it stops selecting the tab;
* for a user range the mouse target ``=`` is NOT the clicked window, so the
  window id travels in the range argument;
* a click only gets a behaviour through a key binding, and bindings are per
  server. That one binding is the only thing this tool sets outside the
  windows it created: the previous binding is kept verbatim as the else-branch
  and restored by ``uninstall``.
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import Optional, Tuple

from . import tmuxio

#: U+00D7. One cell in every terminal (no emoji presentation), thinner than the
#: failed-state glyph U+2717 it must not be mistaken for.
MARK = "\u00d7"
MARK_ASCII = "x"
RANGE = "hxc"
KEY = "MouseDown1Status"
#: Server option holding the binding that was there before ours (the whole
#: ``list-keys`` line, which tmux can parse back), "" when there was none.
PREV_OPTION = "@hx_prev_mouse1"
_COLOUR = "colour247"

_MARK_RE = re.compile(r" ?#\[range=user\|%s@\d+ [^\]]*\][^#]*#\[range=window\|#\{window_index\}\] ?$" % RANGE)
_TARGET_RE = re.compile(r"^(?:%s)?(@\d+)$" % RANGE)
_BIND_RE = re.compile(r"^bind-key\s+(?:-r\s+)?-T\s+root\s+%s\s+(.*)$" % KEY)


def enabled(environ=None) -> bool:
    """On unless ``HERMES_TMUX_CLOSE_MARK`` says otherwise."""
    env = os.environ if environ is None else environ
    return (env.get("HERMES_TMUX_CLOSE_MARK") or "").strip().lower() not in ("0", "false", "no", "off")


def has_mark(fmt: str) -> bool:
    return ("range=user|%s@" % RANGE) in fmt


def with_mark(fmt: str, window_id: str, ascii_mode: bool = False) -> str:
    """``fmt`` with the mark appended; unchanged if it already has one.

    Only the foreground and weight change for the mark, so the tab's own
    background carries through it and through the padding cell after it.
    Preserves whether ``fmt`` originally ended with a trailing space.
    """
    if not fmt or has_mark(fmt) or not _TARGET_RE.match(window_id or ""):
        return fmt
    had_space = fmt.endswith(" ")
    body = fmt[:-1] if had_space else fmt
    tail = " " if had_space else ""
    return "%s #[range=user|%s%s fg=%s nobold]%s#[range=window|#{window_index}]%s" % (
        body, RANGE, window_id, _COLOUR, MARK_ASCII if ascii_mode else MARK, tail)


def without_mark(fmt: str) -> str:
    """Inverse of ``with_mark`` (restores trailing space iff one was present)."""
    if not has_mark(fmt):
        return fmt
    had_space = fmt.endswith(" ")
    return _MARK_RE.sub("", fmt) + (" " if had_space else "")


def parse_target(arg: str) -> Optional[str]:
    """``hxc@12`` (from a click) or ``@12`` -> ``@12``; anything else -> None."""
    match = _TARGET_RE.match((arg or "").strip())
    return match.group(1) if match else None


# --------------------------------------------------------------------------- binding

def current_binding() -> Tuple[str, str]:
    """(whole list-keys line, command part) of the root ``MouseDown1Status`` binding."""
    rc, out = tmuxio.run("list-keys", "-T", "root", KEY)
    line = out.splitlines()[0].strip() if rc == 0 and out.strip() else ""
    match = _BIND_RE.match(line)
    return (line, match.group(1)) if match else ("", "")


def installed() -> bool:
    return ("%s@*" % RANGE) in current_binding()[1]


def _tmux_quote(text: str) -> str:
    """Double-quote for tmux's command parser (which expands ``$`` in them)."""
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def install(handler: str) -> Tuple[bool, str]:
    """Wrap the status-line click so a click on a mark runs ``handler``.

    ``handler`` is a shell command; ``#{mouse_status_range}`` and
    ``#{client_name}`` in it are expanded by tmux at click time. Every other
    click runs the binding that was there before, unchanged.
    """
    line, command = current_binding()
    if ("%s@*" % RANGE) in command:
        return True, "already installed"
    if tmuxio.run("set-option", "-s", PREV_OPTION, line)[0] != 0:
        return False, "could not record the previous binding"
    condition = "#{m:%s@*,#{mouse_status_range}}" % RANGE
    ours = "run-shell -b %s" % _tmux_quote(handler)
    args = ["bind-key", "-T", "root", KEY, "if-shell", "-F", condition, ours]
    if command:
        args.append(command)
    rc, out = tmuxio.run(*args)
    if rc != 0 or not installed():
        restore()
        return False, "bind-key failed: %s" % out
    return True, "installed (previous binding kept: %r)" % (command or "none")


def restore() -> Tuple[bool, str]:
    """Put back exactly the binding ``install`` found. Safe to call twice."""
    rc, line = tmuxio.run("show-options", "-sv", PREV_OPTION)
    if rc != 0:
        return (not installed()), "nothing recorded"
    line = line.strip()
    if line:
        # Let tmux parse its own list-keys line: quoting survives that way.
        fd, path = tempfile.mkstemp(prefix="hx-bind-", suffix=".conf")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(line + "\n")
            rc, out = tmuxio.run("source-file", path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if rc != 0:
            return False, "could not restore the previous binding: %s" % out
    else:
        tmuxio.run("unbind-key", "-T", "root", KEY)
    tmuxio.run("set-option", "-su", PREV_OPTION)
    return (not installed()), "restored: %r" % (line or "no binding")


# --------------------------------------------------------------------------- windows

_OPTION = "window-status-current-format"


def _own_format(window_id: str) -> str:
    """The format set ON this window ('' when it only inherits the global one)."""
    rc, out = tmuxio.run("show-options", "-wv", "-t", window_id, _OPTION)
    return out if rc == 0 else ""


def mark_window(window_id: str, ascii_mode: bool = False) -> bool:
    """Add the mark to a window that already carries its own tab format."""
    fmt = _own_format(window_id)
    if not fmt or has_mark(fmt):
        return False
    return tmuxio.window_option(window_id, _OPTION, with_mark(fmt, window_id, ascii_mode))


def unmark_window(window_id: str) -> bool:
    fmt = _own_format(window_id)
    if not has_mark(fmt):
        return False
    return tmuxio.window_option(window_id, _OPTION, without_mark(fmt))
