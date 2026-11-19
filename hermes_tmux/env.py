"""Environment every pane this tool creates must carry.

Windows and splits are forked from the tmux *server*, not from the pane that
asked for them, so values exported in the caller's shell do not reach them
(`docs/NOTES-tmux.md` §3: `new-window` prints the server's value; only `-e`
overrides it). Without this module a pane silently reads a different Hermes
profile, fails to import this package, or cannot find the interpreter that
owns the dependencies.

Two classes of value, and the difference matters when adding one:

* **Derived here, always set.** `HERMES_HOME` (this process's resolved root),
  `HERMES_TMUX` (the *resolved* gate answer, so a pane cannot re-decide from a
  different environment and disagree with the window that spawned it),
  `PYTHONPATH` (the checkout, because panes run `python -m hermes_tmux` from an
  arbitrary cwd), `HX_PYTHON` (the interpreter that can import that checkout),
  and `HERMES_TMUX_ASCII` (glyph mode decided once, where the locale is known,
  so the tab, the pane header and the transcript cannot disagree).
* **Passed through from the caller.** `_PASSTHROUGH`: reader preferences that
  only the calling shell knows. The tmux server's environment is older than the
  shell that ran `hermes-tmux`, so anything the user exported after the server
  started has to be forwarded by hand or it is lost.

Non-default profiles also get `HXMAIL_DB`, `HXMAIL_LOCAL_DIR` and
`TESTBENCH_HOME` under this home, unless the caller set them explicitly
(`setdefault`, never override). That is what keeps a sandboxed profile from
reading the live mail database or the live TestBench run store.
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
    """Interpreter used to re-enter this package from a pane.

    `HX_PYTHON` first: a pane created under the tmux server cannot rely on
    `sys.executable` meaning anything, but a pane that has already been handed
    `HX_PYTHON` (by `pane_env`) can. `sys.executable` is right for a pane's own
    process; `python3` is the last resort when both are empty (a frozen or
    embedded interpreter), and it will usually fail to import the package —
    which is why `pane_env` always pins a real path.
    """
    return os.environ.get("HX_PYTHON") or sys.executable or "python3"


#: Reader preferences that must reach every process this tool starts. They are
#: read from the shell that ran the command; the tmux server's environment is
#: older and does not have them.
_PASSTHROUGH = ("HERMES_TMUX_TRANSCRIPT", "HERMES_TMUX_LINGER_S", "HERMES_TMUX_CLOSE_MARK",
                "HERMES_TMUX_TESTBENCH", "NO_COLOR", "HXMAIL_DB", "HXMAIL_LOCAL_DIR",
                "TESTBENCH_HOME")


def pane_env() -> Dict[str, str]:
    """The `-e` set for every window and split this tool creates.

    Never mutate the result expecting it to reach existing panes: it is a
    snapshot taken at creation time. Callers that need a different home for one
    aux pane (`cli.ensure_aux_window(home=...)`) copy this dict and override
    `HERMES_HOME` rather than editing the module.
    """
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
