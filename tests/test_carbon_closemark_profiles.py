"""Regression tests for close-mark ownership across profiles (audit HX-5).

The damage these pin down, all from `docs/AUDIT-AND-REPAIR-REPORT.md`:

* ``hermes-tmux open`` used to skip ``closemark.install``, so a tab opened
  before ``up`` drew a cross no click could reach;
* two ``HERMES_HOME`` profiles share one tmux server, hence one
  ``MouseDown1Status`` binding. ``down`` in the first profile used to restore
  the previous binding while the second profile still owned marked windows,
  leaving its crosses dead;
* ``without_mark`` appended a space unconditionally, so a format that never had
  a trailing space gained one and every tab after it shifted by a column.

Two independent halves are exercised: the pure format round-trip (no tmux), and
the click path through a real attached client. The click cannot be simulated
with a direct ``bind-key`` call — tmux routes a status click by the range under
the pointer (``#{mouse_status_range}``), and only a real SGR mouse report at the
right cell proves the range the mark wrote is the range the binding matches.

Isolation is per test: its own server (``-L <sock>``, private ``TMUX_TMPDIR``,
``-f /dev/null``), so ``list-keys`` describes this server alone. Each CLI
invocation is a fresh ``python -m hermes_tmux`` with ``HERMES_TMUX=1``,
``PYTHONPATH`` at this repo, and a private ``HERMES_HOME`` — never the
developer's profile.

Timing: a click is asynchronous once tmux has the bytes, so every assertion
about a window disappearing polls (`_wait_until`, 3 s for the click, 8 s
default for setup) rather than sleeping a fixed interval.

One quirk is recorded rather than repaired (this pass is doc-only): the class
skip guard is effectively inert. ``TMUX`` is ``shutil.which("tmux") or "tmux"``,
a truthy string even when tmux is absent, so nothing is ever skipped. ``setUp``
itself makes no tmux call: the absence surfaces as a ``FileNotFoundError`` from
the first ``_tmux``/``subprocess`` call in the test body, and again from
``tearDown``'s ``kill-server``.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path

from hermes_tmux import closemark

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
TMUX = shutil.which("tmux") or "tmux"


def _wait_until(pred, timeout: float = 8.0, interval: float = 0.05) -> bool:
    """Poll ``pred`` until true or ``timeout``; False means the wait expired.

    Timeout is monotonic, so a clock adjustment cannot extend or cut the wait.
    Callers assert on the boolean, which keeps "the event never arrived" a test
    failure rather than a silent pass.
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


class _PtyClient:
    """A real tmux client on a pty, able to write SGR mouse reports.

    ``tmux attach-session`` needs a terminal: it reads the window size from the
    pty and only then draws a status line the clicks can address. The size is
    set with ``TIOCSWINSZ`` before exec, so ``rows`` is both the pane height and
    the status-line row used by `click_status`.

    ``TMUX``/``TMUX_PANE`` are stripped from the child environment: inheriting
    them would make tmux treat this as a nested invocation instead of a client
    connecting to the named server. Output is drained on a daemon thread because
    an unread pty fills its buffer and blocks the server's client.
    """

    def __init__(self, base_env: dict, sock: str, session: str, cols: int = 160, rows: int = 45) -> None:
        self.rows = rows
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        env = {k: v for k, v in base_env.items() if k not in ("TMUX", "TMUX_PANE")}
        env["TERM"] = "xterm-256color"
        self.proc = subprocess.Popen(
            [TMUX, "-L", sock, "-f", "/dev/null", "attach-session", "-t", session],
            stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True,
        )
        os.close(slave)
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        try:
            while os.read(self.master, 65536):
                pass
        except OSError:
            pass

    def click_status(self, col_0based: int) -> None:
        """Send a left press and release on the status line at ``col_0based``.

        SGR encodes the column 1-based and the row as-is, so ``rows`` is the
        status line. Both final bytes are written: tmux binds the action on
        ``M`` (press) while some builds deliver the release; sending both costs
        one ignored event and makes the test independent of which one the
        server acts on.
        """
        for final in ("M", "m"):
            os.write(self.master, ("\x1b[<0;%d;%d%s" % (col_0based + 1, self.rows, final)).encode())

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        try:
            os.close(self.master)
        except OSError:
            pass


@unittest.skipUnless(TMUX, "tmux not installed")
class CloseMarkProfileRegressions(unittest.TestCase):
    """One isolated tmux server per test; two profiles where the test needs two.

    ``base_env`` is the shared substrate: private ``TMUX_TMPDIR`` keeps the
    socket out of the user's directory, ``HX_PYTHON`` pins the interpreter the
    CLI re-enters, ``-B``/``PYTHONDONTWRITEBYTECODE`` keep the worktree clean.
    Tear-down kills the server by its socket, so a leaked coordinator cannot
    outlive the test.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hx_c5_")
        self.base = Path(self._tmp.name)
        self.tmux_tmp = self.base / "t"
        self.tmux_tmp.mkdir()
        self.sock = "hx_c5_%d_%x" % (os.getpid(), id(self) & 0xFFFF)
        self.base_env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.base_env.update(
            TMUX_TMPDIR=str(self.tmux_tmp),
            HERMES_TMUX="1",
            PYTHONPATH=str(REPO),
            HX_PYTHON=PY,
            PYTHONDONTWRITEBYTECODE="1",
        )

    def tearDown(self) -> None:
        subprocess.run([TMUX, "-L", self.sock, "kill-server"],
                       env=self.base_env, capture_output=True)
        self._tmp.cleanup()

    def _tmux(self, *args: str) -> tuple:
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args],
                           env=self.base_env, capture_output=True, text=True, timeout=15)
        return r.returncode, (r.stdout + r.stderr).rstrip("\n")

    def _make_deleg(self, home: Path, did: str, goal: str) -> None:
        """Write the minimum live delegation the CLI will turn into a tab.

        A manifest plus one ``task-0.log``. ``status: running`` and a log with
        no final line keep the worker live, so the coordinator does not retire
        the tab while the test is clicking it. The goal becomes the window
        name, which is how the tests find the tab afterwards.
        """
        dd = home / "cache" / "delegation" / "live" / did
        dd.mkdir(parents=True, exist_ok=True)
        lp = dd / "task-0.log"
        lp.write_text(
            "=== Hermes subagent live transcript ===\n"
            "22:00:01 user      | kickoff: %s\n" % goal,
            encoding="utf-8",
        )
        (dd / "manifest.json").write_text(json.dumps({
            "delegation_id": did,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task_count": 1,
            "model": "deepseek/v4",
            "tasks": [{"index": 0, "goal": goal, "log": str(lp), "status": "running"}],
        }), encoding="utf-8")

    def _mark_column(self, session: str, active_wid: str) -> int:
        """Visible column of ``closemark.MARK`` in the reconstructed status bar.

        tmux draws ``status-left`` then each window label in index order, using
        ``window-status-current-format`` for the active window and
        ``window-status-format`` for the rest. Both are read with ``#{E:...}``
        (format-expanded) and stripped of ``#[...]`` styling, then concatenated
        exactly as tmux lays them out. ``status-left`` is cut to its first 10
        visible characters: that is longer than the plugin's badge, and the cut
        is what keeps the arithmetic from depending on the trailing clock.

        This is a fixture that mirrors tmux's layout, not the product: if the
        badge or separator changes, the column (not the feature) is what breaks.
        """
        left = re.sub(r"#\[[^\]]*\]", "",
                      self._tmux("display-message", "-p", "-t", session, "#{E:status-left}")[1])[:10]
        parts = []
        for wid in self._tmux("list-windows", "-t", session, "-F", "#{window_id}")[1].splitlines():
            wid = wid.strip()
            opt = "window-status-current-format" if wid == active_wid else "window-status-format"
            parts.append(re.sub(r"#\[[^\]]*\]", "",
                                self._tmux("display-message", "-p", "-t", wid, "#{E:%s}" % opt)[1]))
        bar = left + " ".join(parts)
        return bar.index(closemark.MARK)

    def test_without_mark_preserves_trailing_space_exactness(self) -> None:
        """Round-trip is byte-for-byte, with and without a trailing space.

        Two formats, because the bug was asymmetric: the space-terminated one
        can lose its separator, the other can gain one. Comparing the whole
        string (rather than just "contains the mark") is what makes either
        regression fail.
        """
        fmt_space = " #I:#{E:@hx_glyph} #W#{?window_flags,#F,} "
        fmt_plain = "#I:#{E:@hx_glyph} #W#{?window_flags,#F,}"
        self.assertEqual(closemark.without_mark(closemark.with_mark(fmt_space, "@3")), fmt_space)
        self.assertEqual(closemark.without_mark(closemark.with_mark(fmt_plain, "@3")), fmt_plain)

    def test_open_before_up_installs_binding_and_click_closes_tab(self) -> None:
        """``open`` alone must install the click binding, not only decorate.

        ``open`` runs before any ``up``, so this is the one-call path that used
        to leave a dead cross. ``hxc@*`` in ``list-keys`` is the same substring
        ``closemark.installed()`` tests, so it is a direct statement of "the
        binding names our range" rather than a proxy. The click then proves it
        routes: the window id must stop resolving within 3 s.
        """
        home = self.base / "home"
        self._make_deleg(home, "deleg_open1st0", "Open before up job")
        _, pane0 = self._tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "s1", "-x", "160", "-y", "45", "exec /bin/zsh -f")
        # tmux only reports status clicks at all with mouse mode on; this is
        # deliberately not part of what `up` configures.
        self._tmux("set-option", "-g", "mouse", "on")
        info = self._tmux("display-message", "-p", "#{socket_path},#{pid},0")[1]
        env = dict(self.base_env, TMUX=info, TMUX_PANE=pane0, HERMES_HOME=str(home))

        r = subprocess.run([PY, "-B", "-m", "hermes_tmux", "open", "open1st0#0"],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("hxc@*", self._tmux("list-keys", "-T", "root", "MouseDown1Status")[1])

        wins = self._tmux("list-windows", "-t", "s1", "-F", "#{window_id}\t#{window_name}")[1].splitlines()
        wid = [w.split("\t")[0] for w in wins if "Open" in w][0]
        self._tmux("select-window", "-t", wid)

        client = _PtyClient(self.base_env, self.sock, "s1")
        try:
            self.assertTrue(_wait_until(lambda: self._tmux("display-message", "-p", "-t", "s1", "#{session_attached}")[1] == "1"))
            col = self._mark_column("s1", wid)
            client.click_status(col)
            self.assertTrue(
                _wait_until(lambda: self._tmux("display-message", "-p", "-t", wid, "#{window_id}")[1] != wid, timeout=3.0),
                "clicking close mark on tab opened before `up` did not close the tab",
            )
        finally:
            client.close()

    def test_multi_profile_same_server_click_and_down_isolation(self) -> None:
        """Two profiles, one server, one binding: teardown order must not matter.

        ``MouseDown1Status`` is server scope, so profile A's ``down`` cannot
        restore the previous binding while B still owns marked windows — that
        was HX-5's cross-profile failure. The assertions run in the order that
        exposes it: A down, binding still ours, B's click still works, B's
        layout records the dismissal (so the coordinator will not reopen the
        tab), then B down restores the pre-existing binding *verbatim* (string
        equality against the line captured before any profile ran). Verbatim
        matters because ``restore`` re-sources tmux's own ``list-keys`` line;
        a reconstruction could reorder quoting.
        """
        homeA = self.base / "homeA"
        homeB = self.base / "homeB"
        self._make_deleg(homeA, "deleg_profa001", "Alpha profile task")
        self._make_deleg(homeB, "deleg_profb001", "Beta profile task")

        # Two sessions on ONE server: sessions differ, the binding does not.
        _, paneA = self._tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "sessA", "-x", "160", "-y", "45", "exec /bin/zsh -f")
        self._tmux("set-option", "-g", "mouse", "on")
        orig_bind = self._tmux("list-keys", "-T", "root", "MouseDown1Status")[1]
        info = self._tmux("display-message", "-p", "#{socket_path},#{pid},0")[1]
        envA = dict(self.base_env, TMUX=info, TMUX_PANE=paneA, HERMES_HOME=str(homeA))

        _, paneB = self._tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "sessB", "-x", "160", "-y", "45", "exec /bin/zsh -f")
        envB = dict(self.base_env, TMUX=info, TMUX_PANE=paneB, HERMES_HOME=str(homeB))

        self.assertEqual(subprocess.run([PY, "-B", "-m", "hermes_tmux", "up"], env=envA, capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run([PY, "-B", "-m", "hermes_tmux", "up"], env=envB, capture_output=True).returncode, 0)

        # Tabs appear on the coordinator's first pass, not inside `up`; poll
        # rather than assume the window exists the moment up returns.
        self.assertTrue(_wait_until(lambda: any("Beta" in l for l in self._tmux("list-windows", "-t", "sessB", "-F", "#{window_name}")[1].splitlines())))

        # Tear down Profile A FIRST: server binding MUST stay installed for Profile B!
        self.assertEqual(subprocess.run([PY, "-B", "-m", "hermes_tmux", "down"], env=envA, capture_output=True).returncode, 0)
        self.assertIn("hxc@*", self._tmux("list-keys", "-T", "root", "MouseDown1Status")[1])

        # Now click the close mark on Profile B's worker tab!
        winsB = self._tmux("list-windows", "-t", "sessB", "-F", "#{window_id}\t#{window_name}")[1].splitlines()
        widB = [w.split("\t")[0] for w in winsB if "Beta" in w][0]
        self._tmux("select-window", "-t", widB)

        clientB = _PtyClient(self.base_env, self.sock, "sessB")
        try:
            self.assertTrue(_wait_until(lambda: self._tmux("display-message", "-p", "-t", "sessB", "#{session_attached}")[1] == "1"))
            col = self._mark_column("sessB", widB)
            clientB.click_status(col)
            self.assertTrue(
                _wait_until(lambda: self._tmux("display-message", "-p", "-t", widB, "#{window_id}")[1] != widB, timeout=3.0),
                "clicking close mark on second HERMES_HOME profile failed to close its tab",
            )
        finally:
            clientB.close()

        # Verify Profile B's layout recorded the dismissal (so watch does not reopen it)
        sdirB = next((homeB / "cache" / "hermes-tmux" / "sessions").iterdir())
        layB = json.loads((sdirB / "layout.json").read_text(encoding="utf-8"))
        self.assertIn("deleg_profb001#0", layB.get("dismissed") or {})

        # Tear down Profile B (the LAST workspace on the server): original binding restored!
        self.assertEqual(subprocess.run([PY, "-B", "-m", "hermes_tmux", "down"], env=envB, capture_output=True).returncode, 0)
        self.assertEqual(self._tmux("list-keys", "-T", "root", "MouseDown1Status")[1], orig_bind)


if __name__ == "__main__":
    unittest.main()
