"""Enable/disable gate for the tmux workspace.

`HERMES_TMUX=0` always disables. Before validation the default is off, so an
explicit `HERMES_TMUX=1` is required; after validation DEFAULT_ENABLED flips to
True and the workspace is on unless opted out.
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
    """Human-readable reason the workspace is off, or '' when it is on."""
    env = os.environ if environ is None else environ
    if enabled(env):
        return ""
    if env.get("HERMES_TMUX") is not None:
        return "HERMES_TMUX=%s" % env.get("HERMES_TMUX")
    return "disabled by default until validated (set HERMES_TMUX=1)"
