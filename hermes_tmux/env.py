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
                "HERMES_TMUX_TESTBENCH", "NO_COLOR", "HXMAIL_DB", "HXMAIL_LOCAL_DIR",
                "TESTBENCH_HOME")


def pane_env() -> Dict[str, str]:
    from . import labels
    from .state import hermes_home
    hh = hermes_home()
    env = {
        "HERMES_HOME": str(hh),
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
    try:
        is_default_home = hh.resolve() == (Path.home() / ".hermes").resolve()
    except OSError:
        is_default_home = False
    if not is_default_home:
        env.setdefault("HXMAIL_DB", str(hh / "cache" / "hxmail" / "mail.db"))
        env.setdefault("HXMAIL_LOCAL_DIR", str(hh / "cache" / "hxmail" / "local"))
        env.setdefault("TESTBENCH_HOME", str(hh / "cache" / "testbench"))
    return env
