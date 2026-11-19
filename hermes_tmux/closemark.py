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
  windows it created: the previous binding is kept verbatim as the
  else-branch and restored by ``restore``.

The two halves of the feature are independent and fail separately:

1. **The mark** — text appended to a window's *own* ``window-status-current-format``
   (`mark_window`). Only that option, only for a window that already carries its
   own format: the global option is never touched, so a user window or the
   overview never grows a cross. Because it is a per-window option, a window that
   has been closed and recreated has no mark until something marks it again.
   `decorate_window` does that at creation; `close-mark on` (in `cli`) walks the
   recorded worker tabs for a server-side repair.

2. **The click** — the server-wide ``MouseDown1Status`` binding (`install`). It
   is a wrapper, not a replacement: `if-shell -F '#{m:hxc@*,#{mouse_status_range}}'`
   selects our handler when the clicked range starts with ``hxc@``, and falls
   through to the previous binding otherwise. The previous binding is captured by
   `list-keys` as a whole line, stored in the server option
   `@hx_prev_mouse1`, and restored by feeding that line back through
   `source-file` (tmux's own parser, so quoting survives). An empty previous
   binding is restored as `unbind-key`.

Ordering rules that make this safe to repeat: `install` is idempotent (it
returns "already installed" if the range is in the current command), and a
failed `bind-key` calls `restore()` before reporting, so a half-installed
binding cannot survive. `restore` is also idempotent and clears the option.

Server scope is the reason `cli.cmd_down` checks for *any* remaining workspace
before restoring: two profiles on one server share the binding, and the first
`down` must not remove the other's.

Injection: everything that reaches a format is either ours or validated.
`with_mark` refuses anything that is not a bare window id (`_TARGET_RE`), and
`parse_target` accepts only `hxc@12`/`@12` — the range string comes from tmux,
which is not a trust boundary to relax, it is a place where a hostile string
would arrive as a shell argument.
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
#: `list-keys` prints the binding as ``bind-key [-r] -T root MouseDown1Status <cmd>``.
#: The command part is everything after the key name and is what gets stored and
#: re-sourced; matching it (rather than splitting on whitespace) is what keeps a
#: quoted multi-word previous binding intact.
_BIND_RE = re.compile(r"^bind-key\s+(?:-r\s+)?-T\s+root\s+%s\s+(.*)$" % KEY)


def enabled(environ=None) -> bool:
    """On unless ``HERMES_TMUX_CLOSE_MARK`` says otherwise.

    Separate from the workspace gate on purpose: a reader can want the
    workspace without the mouse behaviour (or a terminal that sends clicks
    somewhere else). Off leaves both the binding and every window format
    untouched — there is no half-installed state to clean up.
    """
    env = os.environ if environ is None else environ
    return (env.get("HERMES_TMUX_CLOSE_MARK") or "").strip().lower() not in ("0", "false", "no", "off")


def has_mark(fmt: str) -> bool:
    return ("range=user|%s@" % RANGE) in fmt


def with_mark(fmt: str, window_id: str, ascii_mode: bool = False) -> str:
    """``fmt`` with the mark appended; unchanged if it already has one.

    Shape of the appended slice::

        #[range=user|hxc@12 fg=colour247 nobold]×#[range=window|#{window_index}]

    Three details, each of which was a visible defect when wrong:

    * ``range=user|…`` and not ``range=window|…``: only a user range delivers
      its own string in ``#{mouse_status_range}``, and that string is how the
      window id reaches the handler (tmux's mouse target ``=`` is the current
      window, not the clicked one).
    * the window's own range is restored immediately after the mark, or the
      cells following the cross stop selecting the tab.
    * only foreground and weight change, so the tab's own background (the
      current-window highlight) shows through the cross and the padding cell.

    Preserves whether ``fmt`` originally ended with a trailing space; the range
    argument is at most `hxc@` + 8 digits (11 bytes), well inside tmux's 15-byte
    limit, and anything that is not a bare ``@N`` window id is refused outright.
    """
    if not fmt or has_mark(fmt) or not _TARGET_RE.match(window_id or ""):
        return fmt
    had_space = fmt.endswith(" ")
    body = fmt[:-1] if had_space else fmt
    tail = " " if had_space else ""
    return "%s #[range=user|%s%s fg=%s nobold]%s#[range=window|#{window_index}]%s" % (
        body, RANGE, window_id, _COLOUR, MARK_ASCII if ascii_mode else MARK, tail)


def without_mark(fmt: str) -> str:
    """Inverse of ``with_mark`` (restores trailing space iff one was present).

    The regex nibbles the leading space that `with_mark` inserted, which is why
    the trailing space has to be re-added when it was there originally: the
    status line's separator is drawn by tmux between windows, and losing the
    space silently changes the spacing of every tab after this one.
    """
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
    """(whole list-keys line, command part) of the root ``MouseDown1Status`` binding.

    Only the first line is used: one key has one binding per table. Both empty
    when there is no binding, which `install` treats as "record nothing" and
    `install`'s else-branch omits — an unbound key must stay unbound, not gain an
    empty else-branch that tmux would try to parse as a command.
    """
    rc, out = tmuxio.run("list-keys", "-T", "root", KEY)
    line = out.splitlines()[0].strip() if rc == 0 and out.strip() else ""
    match = _BIND_RE.match(line)
    return (line, match.group(1)) if match else ("", "")


def installed() -> bool:
    """True when the current binding is ours (its condition names our range)."""
    return ("%s@*" % RANGE) in current_binding()[1]


def _tmux_quote(text: str) -> str:
    """Double-quote for tmux's command parser (which expands ``$`` in them).

    Backslash and ``"`` are escaped for tmux, and ``$`` too: tmux treats a
    ``$name`` inside a double-quoted command as a *format/variable* reference and
    would replace it before the shell ever sees it, so an unrelated ``$HOME`` in
    the handler would silently become something else. Single quotes would avoid
    that but cannot contain the ``'`` that shell quoting of our own paths
    produces.
    """
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def install(handler: str) -> Tuple[bool, str]:
    """Wrap the status-line click so a click on a mark runs ``handler``.

    ``handler`` is a shell command; ``#{mouse_status_range}`` and
    ``#{client_name}`` in it are expanded by tmux at click time. Every other
    click runs the binding that was there before, unchanged.

    ``handler`` arrives already shell-quoted for tmux by `_tmux_quote` (which
    escapes ``$`` because tmux, not the shell, expands formats inside a
    double-quoted command). ``run-shell -b`` keeps the click from blocking the
    status line, and the caller is responsible for the command being silent and
    exit-0: tmux prints a run-shell job's output, or a line about a non-zero
    exit, into the reader's own pane.

    Failure handling is deliberate: the previous binding is recorded *before*
    ``bind-key``, and any failure re-runs `restore()` so the server is left with
    exactly what it had.
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
    """Put back exactly the binding ``install`` found. Safe to call twice.

    The recorded line is a `list-keys` line, i.e. a complete tmux command. It is
    fed back through ``source-file`` rather than rebuilt with ``bind-key``
    arguments: the command may contain quotes and formats, and re-parsing our
    own reconstruction would be a second chance to get quoting wrong. The
    tempfile is unlinked either way.

    An empty record means "there was no binding": that case unbinds, so a server
    that had none does not keep ours. The option is unset last, so an
    interrupted restore can be re-run instead of silently reporting success.
    """
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
    """Add the mark to a window that already carries its own tab format.

    The "own format" precondition is the guard that keeps this off user windows
    and the overview: `_own_format` reads with ``show-options -wv`` (the
    window's own value only), so a window that merely inherits the global format
    returns '' and is left alone. Adding a mark to an inherited format would mean
    writing one, i.e. taking over a window we do not own.
    """
    fmt = _own_format(window_id)
    if not fmt or has_mark(fmt):
        return False
    return tmuxio.window_option(window_id, _OPTION, with_mark(fmt, window_id, ascii_mode))


def unmark_window(window_id: str) -> bool:
    fmt = _own_format(window_id)
    if not has_mark(fmt):
        return False
    return tmuxio.window_option(window_id, _OPTION, without_mark(fmt))
