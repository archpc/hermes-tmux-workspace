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


def pane_env() -> Dict[str, str]:
    from .state import hermes_home
    return {
        "HERMES_HOME": str(hermes_home()),
        "HERMES_TMUX": "1" if gate.enabled() else "0",
        "PYTHONPATH": str(repo_root()),
        "HX_PYTHON": python(),
    }
