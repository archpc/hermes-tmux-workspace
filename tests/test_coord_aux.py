"""Isolated tmux integration tests for the permanent `hx:coord` tab (G3 view).

The pane renders `hermes coord` on a refresh loop; tests use a PATH shim so the
render is deterministic and no real Hermes install is required.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMUX = shutil.which("tmux") or "tmux"
PY = sys.executable or "/usr/bin/python3"

_SHIM = """#!/bin/sh
if [ "$1" = "coord" ]; then
  if [ -f "{marker}" ]; then exit 1; fi
  printf 'COORD-FIXTURE coordination: enabled\\n'
  exit 0
fi
exit 1
"""


@unittest.skipUnless(shutil.which("tmux"), "tmux unavailable")
class PermanentCoordTabTests(unittest.TestCase):
    def setUp(self):
        import pwd
        # Honor TestBench's writable scratch override; under Seatbelt confinement
        # the real ~/.hermes/cache/scratch is not writable.
        tb_sc = os.environ.get("TESTBENCH_SCRATCH_DIR", "").strip()
        scratch = Path(tb_sc) if tb_sc else (Path(pwd.getpwuid(os.getuid()).pw_dir) / ".hermes/cache/scratch")
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=str(scratch), prefix="hxcoord-")
        self.root = Path(self.tmp.name)
        self.tmux_dir = self.root / "t"
        self.tmux_dir.mkdir(mode=0o700)
        self.home_dir = self.root / "home"
        self.home_dir.mkdir(mode=0o700)
        self.shim_dir = self.root / "bin"
        self.shim_dir.mkdir(mode=0o700)
        self.fail_marker = self.root / "fail-marker"
        shim = self.shim_dir / "hermes"
        shim.write_text(_SHIM.format(marker=self.fail_marker), encoding="utf-8")
        shim.chmod(0o755)
        self.sock = "coord%d" % os.getpid()

        self._tmux("new-session", "-d", "-s", "work", "-x", "132", "-y", "38", "-n", "hermes")
        self._tmux("new-window", "-d", "-t", "work", "-n", "shell")
        sock_info = self._tmux(
            "display-message", "-p", "-t", "work:hermes",
            "#{socket_path},#{pid},0\t#{pane_id}\t#{window_id}\t#{window_index}",
        ).stdout.strip()
        self.tmux_env_val, self.user_pane, self.user_win, self.user_idx = sock_info.split("\t")

    def tearDown(self):
        subprocess.run([TMUX, "-L", self.sock, "kill-server"],
                       env=self._base_env(), capture_output=True)
        self.tmp.cleanup()

    def _base_env(self, extra: dict[str, str] | None = None):
        env = dict(os.environ)
        env["TMUX_TMPDIR"] = str(self.tmux_dir)
        env["HERMES_HOME"] = str(self.home_dir)
        env["HERMES_TMUX"] = "1"
        env["PYTHONPATH"] = str(REPO)
        env["HXMAIL_DB"] = str(self.root / "mail.db")
        env["HXMAIL_LOCAL_DIR"] = str(self.root / "local")
        env["TESTBENCH_HOME"] = str(self.root / "tb_home")
        env["HX_HERMES_BIN"] = str(self.shim_dir / "hermes")
        env["PATH"] = str(self.shim_dir) + os.pathsep + env.get("PATH", "")
        if extra:
            env.update(extra)
        return env

    def _tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args],
                              env=self._base_env(), capture_output=True, text=True, check=check)

    def _cli(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = self._base_env(extra_env)
        env["TMUX"] = self.tmux_env_val
        env["TMUX_PANE"] = self.user_pane
        return subprocess.run([PY, "-m", "hermes_tmux", *args], cwd=str(REPO),
                              env=env, capture_output=True, text=True, timeout=20)

    def _windows(self):
        out = self._tmux("list-windows", "-t", "work",
                         "-F", "#{window_index}\t#{window_id}\t#{window_name}").stdout.strip()
        rows = []
        for line in out.splitlines():
            idx, wid, wname = line.split("\t")
            rows.append({"index": int(idx), "id": wid, "name": wname})
        return rows

    def _active_window_id(self) -> str:
        return self._tmux("display-message", "-p", "-t", "work", "#{window_id}").stdout.strip()

    def _pane_pid_of(self, target: str) -> int:
        return int(self._tmux("display-message", "-p", "-t", target, "#{pane_pid}").stdout.strip())

    def _wait_rendered(self, wid: str, needle: str) -> None:
        deadline = time.time() + 8.0
        captured = ""
        while time.time() < deadline:
            captured = self._tmux("capture-pane", "-p", "-t", wid, check=False).stdout
            if needle in captured:
                return
            time.sleep(0.1)
        self.fail("hx:coord pane did not render %r: %r" % (needle, captured[:400]))

    def test_coord_tab_lifecycle(self):
        r_up = self._cli("up")
        self.assertEqual(r_up.returncode, 0, r_up.stdout + r_up.stderr)
        self.assertEqual(self._active_window_id(), self.user_win, "up stole focus!")

        by_name = {w["name"]: w for w in self._windows()}
        for expected in ("agents", "hx:mail", "hx:testbench", "hx:coord"):
            self.assertIn(expected, by_name)
        self.assertLess(by_name["hx:testbench"]["index"], by_name["hx:coord"]["index"])
        self.assertEqual(self._active_window_id(), self.user_win)

        coord_wid = by_name["hx:coord"]["id"]
        self._wait_rendered(coord_wid, "COORD-FIXTURE")
        coord_pid = self._pane_pid_of(coord_wid)

        # idempotent up: same window, same pane process, no focus steal
        for _ in range(2):
            self.assertEqual(self._cli("up").returncode, 0)
            self.assertEqual([w["name"] for w in self._windows()].count("hx:coord"), 1)
            self.assertEqual(self._pane_pid_of(coord_wid), coord_pid)
            self.assertEqual(self._active_window_id(), self.user_win)

        # explicit focus command
        r = self._cli("coord")
        self.assertEqual(r.returncode, 0)
        self.assertIn("focused hx:coord", r.stdout)
        self.assertEqual(self._active_window_id(), coord_wid)
        self._tmux("select-window", "-t", self.user_win)

        # close refuses a permanent aux window
        r_close = self._cli("close", coord_wid)
        self.assertNotEqual(r_close.returncode, 0)
        self.assertIn("not a worker tab", r_close.stdout)

        # crash recovery: kill the pane process, `up` restores detached
        os.kill(coord_pid, signal.SIGKILL)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if "hx:coord" not in [w["name"] for w in self._windows()]:
                break
            time.sleep(0.05)
        r_restore = self._cli("up")
        self.assertEqual(r_restore.returncode, 0, r_restore.stdout + r_restore.stderr)
        self.assertEqual(self._active_window_id(), self.user_win, "restore stole focus!")
        restored = {w["name"]: w for w in self._windows()}
        self.assertIn("hx:coord", restored)
        self._wait_rendered(restored["hx:coord"]["id"], "COORD-FIXTURE")

        # teardown removes aux windows, keeps user windows
        r_down = self._cli("down")
        self.assertEqual(r_down.returncode, 0, r_down.stdout + r_down.stderr)
        remaining = {w["name"] for w in self._windows()}
        self.assertEqual(remaining, {"hermes", "shell"})

    def test_opt_out_skips_auto_creation_but_explicit_command_works(self):
        r_up = self._cli("up", extra_env={"HERMES_TMUX_COORD": "0"})
        self.assertEqual(r_up.returncode, 0, r_up.stdout + r_up.stderr)
        names = [w["name"] for w in self._windows()]
        self.assertIn("hx:mail", names)
        self.assertNotIn("hx:coord", names)
        r = self._cli("coord", extra_env={"HERMES_TMUX_COORD": "0"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("hx:coord", [w["name"] for w in self._windows()])
        self._cli("down")

    def test_unavailable_coord_cli_shows_fallback(self):
        self.fail_marker.write_text("fail", encoding="utf-8")
        r_up = self._cli("up", extra_env={"HERMES_TMUX_TESTBENCH": "0"})
        self.assertEqual(r_up.returncode, 0, r_up.stdout + r_up.stderr)
        by_name = {w["name"]: w for w in self._windows()}
        self.assertIn("hx:coord", by_name)
        self._wait_rendered(by_name["hx:coord"]["id"], "hermes coord unavailable")
        self._cli("down")


if __name__ == "__main__":
    unittest.main()
