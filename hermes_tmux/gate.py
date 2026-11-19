"""Enable/disable gate for the tmux workspace.

One boolean decides whether any command may build, drive or decorate tmux
panes. `HERMES_TMUX=0` (and the other spellings in `_FALSEY`) disables;
`HERMES_TMUX=1` (and `_TRUTHY`) enables; **any other value, including a typo,
falls back to `DEFAULT_ENABLED`** rather than being read as "off". A typo
therefore changes nothing, which is the safer failure for a display tool.

The default is ON (`DEFAULT_ENABLED`). It was off until the end-to-end suite
and the installed launcher passed; the flip is a one-line change and is
covered by `tests/test_gate.py`.

Who reads the gate, and who deliberately does not:

* `cmd_up`, `cmd_open`, `cmd_aux`, `cmd_close` refuse and say why.
* `cmd_down` and `cmd_close_mark` deliberately do **not** consult it: a reader
  who exports `HERMES_TMUX=0` must still be able to tear down the workspace and
  the server-wide click binding that an earlier, enabled shell created.
  Skipping the check there is the difference between "off" and "stuck".
* `env.pane_env()` writes the resolved answer (`0`/`1`) into every pane it
  creates, so a child never re-decides from a different environment.
"""

from __future__ import annotations

import os

#: Flipped to True after the end-to-end tmux validation suite and the installed
#: launcher passed on this machine. `HERMES_TMUX=0` still opts out per shell.
DEFAULT_ENABLED = True

_TRUTHY = {"1", "true", "yes", "on", "enable", "enabled"}
_FALSEY = {"0", "false", "no", "off", "disable", "disabled"}


def enabled(environ=None) -> bool:
    """True when the workspace may build or drive tmux panes."""
    env = os.environ if environ is None else environ
    raw = env.get("HERMES_TMUX")
    if raw is None:
        return DEFAULT_ENABLED
    value = str(raw).strip().lower()
    if value in _FALSEY:
        return False
    if value in _TRUTHY:
        return True
    return DEFAULT_ENABLED


def disabled_reason(environ=None) -> str:
    """Human-readable reason the workspace is off, or '' when it is on.

    The last branch is a leftover from the pre-validation default (off) and is
    unreachable while `DEFAULT_ENABLED` is True: with no `HERMES_TMUX` in the
    environment `enabled()` already returned True. It is kept so the message
    stays correct if the default is ever flipped back for a rollback. Stale
    text, live behaviour — do not "simplify" it into silence without also
    flipping the default.
    """
    env = os.environ if environ is None else environ
    if enabled(env):
        return ""
    if env.get("HERMES_TMUX") is not None:
        return "HERMES_TMUX=%s" % env.get("HERMES_TMUX")
    return "disabled by default until validated (set HERMES_TMUX=1)"
