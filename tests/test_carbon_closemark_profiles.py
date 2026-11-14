"""Regression tests for close-mark multi-profile HERMES_HOME handling,
open-before-up binding installation, cross-profile down safety, and format
round-tripping without trailing spaces.
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
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


class _PtyClient:
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
        fmt_space = " #I:#{E:@hx_glyph} #W#{?window_flags,#F,} "
        fmt_plain = "#I:#{E:@hx_glyph} #W#{?window_flags,#F,}"
        self.assertEqual(closemark.without_mark(closemark.with_mark(fmt_space, "@3")), fmt_space)
        self.assertEqual(closemark.without_mark(closemark.with_mark(fmt_plain, "@3")), fmt_plain)

    def test_open_before_up_installs_binding_and_click_closes_tab(self) -> None:
        home = self.base / "home"
        self._make_deleg(home, "deleg_open1st0", "Open before up job")
        _, pane0 = self._tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "s1", "-x", "160", "-y", "45", "exec /bin/zsh -f")
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
        homeA = self.base / "homeA"
        homeB = self.base / "homeB"
        self._make_deleg(homeA, "deleg_profa001", "Alpha profile task")
        self._make_deleg(homeB, "deleg_profb001", "Beta profile task")

        _, paneA = self._tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "sessA", "-x", "160", "-y", "45", "exec /bin/zsh -f")
        self._tmux("set-option", "-g", "mouse", "on")
        orig_bind = self._tmux("list-keys", "-T", "root", "MouseDown1Status")[1]
        info = self._tmux("display-message", "-p", "#{socket_path},#{pid},0")[1]
        envA = dict(self.base_env, TMUX=info, TMUX_PANE=paneA, HERMES_HOME=str(homeA))

        _, paneB = self._tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "sessB", "-x", "160", "-y", "45", "exec /bin/zsh -f")
        envB = dict(self.base_env, TMUX=info, TMUX_PANE=paneB, HERMES_HOME=str(homeB))

        self.assertEqual(subprocess.run([PY, "-B", "-m", "hermes_tmux", "up"], env=envA, capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run([PY, "-B", "-m", "hermes_tmux", "up"], env=envB, capture_output=True).returncode, 0)

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
