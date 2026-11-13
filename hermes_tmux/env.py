"""Environment every pane this tool creates must carry.

Windows and splits are forked from the tmux *server*, not from the pane that
asked for them, so values exported in the caller's shell do not reach them.
Without this, a pane silently reads a different Hermes profile or fails to
import this package.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict

from . import gate


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def python() -> str:
    return os.environ.get("HX_PYTHON") or sys.executable or "python3"


#: Reader preferences that must reach every process this tool starts. They are
#: read from the shell that ran the command; the tmux server's environment is
#: older and does not have them.
_PASSTHROUGH = ("HERMES_TMUX_TRANSCRIPT", "HERMES_TMUX_LINGER_S", "HERMES_TMUX_CLOSE_MARK",
                "NO_COLOR")


def pane_env() -> Dict[str, str]:
    from . import labels
    from .state import hermes_home
    env = {
        "HERMES_HOME": str(hermes_home()),
        "HERMES_TMUX": "1" if gate.enabled() else "0",
        "PYTHONPATH": str(repo_root()),
        "HX_PYTHON": python(),
        # Decided once, where the locale is known, so the tab, the header and
        # the transcript agree on glyphs.
        "HERMES_TMUX_ASCII": "1" if labels.ascii_only() else "0",
    }
    for name in _PASSTHROUGH:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env
