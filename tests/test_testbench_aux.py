"""Isolated tmux integration tests for permanent first-class `hx:testbench` tab."""

from __future__ import annotations

import json
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


def _installed_testbench() -> Path | None:
    cand = Path.home() / ".local/bin/testbench"
    if cand.is_file() and os.access(str(cand), os.X_OK):
        return cand
    found = shutil.which("testbench")
    return Path(found) if found else None


@unittest.skipUnless(shutil.which("tmux"), "tmux unavailable")
class PermanentTestBenchTabTests(unittest.TestCase):
    def setUp(self):
        import pwd
        scratch = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".hermes/cache/scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=str(scratch), prefix="hxtb-")
        self.root = Path(self.tmp.name)
        self.tmux_dir = self.root / "t"
        self.tmux_dir.mkdir(mode=0o700)
        self.home_dir = self.root / "home"
        self.home_dir.mkdir(mode=0o700)
        self.tb_home = self.root / "tb_home"
        self.sock = "tb%d" % os.getpid()

        # Start isolated server with TWO user windows: `1:hermes` (active) and `2:shell`
        self._tmux("new-session", "-d", "-s", "work", "-x", "132", "-y", "38", "-n", "hermes")
        self._tmux("new-window", "-d", "-t", "work", "-n", "shell")
        sock_info = self._tmux(
            "display-message", "-p", "-t", "work:hermes",
            "#{socket_path},#{pid},0\t#{pane_id}\t#{window_id}\t#{window_index}",
        ).stdout.strip()
        self.tmux_env_val, self.user_pane, self.user_win, self.user_idx = sock_info.split("\t")
        shell_info = self._tmux(
            "display-message", "-p", "-t", "work:shell",
            "#{window_id}\t#{window_index}",
        ).stdout.strip()
        self.shell_win, self.shell_idx = shell_info.split("\t")

    def tearDown(self):
        subprocess.run(
            [TMUX, "-L", self.sock, "kill-server"],
            env=self._base_env(),
            capture_output=True,
        )
        self.tmp.cleanup()

    def _base_env(self, extra: dict[str, str] | None = None):
        env = dict(os.environ)
        env["TMUX_TMPDIR"] = str(self.tmux_dir)
        env["HERMES_HOME"] = str(self.home_dir)
        env["HERMES_TMUX"] = "1"
        env["PYTHONPATH"] = str(REPO)
        env["HXMAIL_DB"] = str(self.root / "mail.db")
        env["HXMAIL_LOCAL_DIR"] = str(self.root / "local")
        env["TESTBENCH_HOME"] = str(self.tb_home)
        if extra:
            env.update(extra)
        return env

    def _tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [TMUX, "-L", self.sock, "-f", "/dev/null", *args],
            env=self._base_env(),
            capture_output=True,
            text=True,
            check=check,
        )

    def _cli(self, *args: str, pane: str = "", extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = self._base_env(extra_env)
        env["TMUX"] = self.tmux_env_val
        env["TMUX_PANE"] = pane or self.user_pane
        return subprocess.run(
            [PY, "-m", "hermes_tmux", *args],
            cwd=str(REPO),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def _windows(self):
        out = self._tmux(
            "list-windows", "-t", "work",
            "-F", "#{window_index}\t#{window_id}\t#{window_name}\t#{window_active}",
        ).stdout.strip()
        rows = []
        for line in out.splitlines():
            idx, wid, wname, active = line.split("\t")
            rows.append({"index": int(idx), "id": wid, "name": wname, "active": active == "1"})
        return rows

    def _active_window_id(self) -> str:
        return self._tmux("display-message", "-p", "-t", "work", "#{window_id}").stdout.strip()

    def _pane_pid_of(self, target: str) -> int:
        val = self._tmux("display-message", "-p", "-t", target, "#{pane_pid}").stdout.strip()
        return int(val)

    def _seed_running_worker(self, did: str, goal: str) -> None:
        live = self.home_dir / "cache/delegation/live" / did
        live.mkdir(parents=True, exist_ok=True)
        log = live / "task-0.log"
        log.write_text("06:00:00 tool     | cargo test\n", encoding="utf-8")
        manifest = {
            "delegation_id": did,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task_count": 1,
            "model": "deepseek/deepseek-v4.1-flash-1017",
            "provider": "nous",
            "tasks": [{
                "index": 0,
                "goal": goal,
                "log": str(log),
                "status": "running",
                "exit_reason": None,
                "model": "deepseek/deepseek-v4.1-flash-1017",
            }],
        }
        (live / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def _wait_testbench_rendered(self, tb_wid: str) -> None:
        if _installed_testbench() is None:
            return
        deadline = time.time() + 6.0
        captured = ""
        while time.time() < deadline:
            captured = self._tmux("capture-pane", "-p", "-t", tb_wid, check=False).stdout
            if "TESTBENCH" in captured:
                return
            time.sleep(0.06)
        self.fail("testbench TUI did not render TESTBENCH header in %s: %r" % (tb_wid, captured[:400]))

    def test_permanent_testbench_tab_lifecycle_coexistence_with_mail_and_teardown(self):
        self._seed_running_worker("deleg_tb000001", "Verify adapter parser")

        # 1. `hermes-tmux up` creates `agents`, `hx:mail`, AND `hx:testbench` detached without focus steal
        r_up = self._cli("up")
        self.assertEqual(r_up.returncode, 0, r_up.stdout + r_up.stderr)
        self.assertEqual(self._active_window_id(), self.user_win, "up stole focus from user window!")

        deadline = time.time() + 10.0
        wins = []
        while time.time() < deadline:
            wins = self._windows()
            if any("Verify adapter" in w["name"] for w in wins):
                break
            time.sleep(0.1)

        by_name = {w["name"]: w for w in wins}
        for expected in ("hermes", "shell", "agents", "hx:mail", "hx:testbench"):
            self.assertIn(expected, by_name, wins)
        worker_wins = [w for w in wins if "Verify adapter" in w["name"]]
        self.assertEqual(len(worker_wins), 1, wins)

        # Window order: user tabs preserved, then agents < hx:mail < hx:testbench < dynamic worker tab
        self.assertEqual(by_name["hermes"]["index"], int(self.user_idx))
        self.assertEqual(by_name["shell"]["index"], int(self.shell_idx))
        self.assertLess(by_name["agents"]["index"], by_name["hx:mail"]["index"])
        self.assertLess(by_name["hx:mail"]["index"], by_name["hx:testbench"]["index"])
        self.assertLess(by_name["hx:testbench"]["index"], worker_wins[0]["index"])
        self.assertEqual(self._active_window_id(), self.user_win)

        tb_wid = by_name["hx:testbench"]["id"]
        self._wait_testbench_rendered(tb_wid)
        tb_pid = self._pane_pid_of(tb_wid)

        # Must NOT auto-run any suites on startup!
        if (self.tb_home / "runs").is_dir():
            self.assertEqual(list((self.tb_home / "runs").iterdir()), [])

        # 2. Repeated `up` calls are idempotent: single `hx:testbench`, same PID, no focus steal
        for _ in range(2):
            r_up2 = self._cli("up")
            self.assertEqual(r_up2.returncode, 0)
            self.assertEqual([w["name"] for w in self._windows()].count("hx:testbench"), 1)
            self.assertEqual(self._pane_pid_of(tb_wid), tb_pid, "repeated up restarted testbench TUI!")
            self.assertEqual(self._active_window_id(), self.user_win)

        # 3. `hermes-tmux testbench` focuses `hx:testbench` explicitly without restarting TUI
        r_tb = self._cli("testbench")
        self.assertEqual(r_tb.returncode, 0)
        self.assertIn("focused hx:testbench", r_tb.stdout)
        self.assertEqual(self._active_window_id(), tb_wid)
        self.assertEqual(self._pane_pid_of(tb_wid), tb_pid)
        self._tmux("select-window", "-t", self.user_win)
        self.assertEqual(self._active_window_id(), self.user_win)

        # 4. `close` refuses `hx:testbench` (permanent aux, not a worker tab)
        r_close_tb = self._cli("close", tb_wid)
        self.assertNotEqual(r_close_tb.returncode, 0)
        self.assertIn("not a worker tab", r_close_tb.stdout)

        # 5. Crash recovery: kill TUI process -> background coordinator or `up` restores `hx:testbench` detached!
        os.kill(tb_pid, signal.SIGKILL)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if "hx:testbench" not in [w["name"] for w in self._windows()]:
                break
            time.sleep(0.05)
        r_restore = self._cli("up")
        self.assertEqual(r_restore.returncode, 0, r_restore.stdout + r_restore.stderr)
        self.assertEqual(self._active_window_id(), self.user_win, "restoring hx:testbench stole focus!")
        restored_wins = {w["name"]: w for w in self._windows()}
        self.assertIn("hx:testbench", restored_wins)
        restored_wid = restored_wins["hx:testbench"]["id"]
        self._wait_testbench_rendered(restored_wid)

        # 6. Foreign user split inside `hx:testbench` survives crash + rebuild AND `down`
        foreign_pane = self._tmux(
            "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", restored_wid, "sleep 300"
        ).stdout.strip()
        for line in self._tmux("list-panes", "-t", restored_wid, "-F", "#{pane_id}\t#{pane_pid}").stdout.splitlines():
            pid_id, pid_num = line.split("\t")
            if pid_id != foreign_pane:
                os.kill(int(pid_num), signal.SIGKILL)
        time.sleep(0.25)
        self.assertEqual(self._cli("up").returncode, 0)
        panes_after = self._tmux("list-panes", "-t", restored_wid, "-F", "#{pane_id}").stdout.splitlines()
        self.assertIn(foreign_pane, panes_after)
        self._tmux("kill-pane", "-t", foreign_pane)

        # 7. Safe teardown via `hermes-tmux down` removes `agents`, `hx:mail`, `hx:testbench` and leaves user windows
        r_down = self._cli("down")
        self.assertEqual(r_down.returncode, 0, r_down.stdout + r_down.stderr)
        remaining = {w["name"]: w for w in self._windows()}
        self.assertEqual(set(remaining), {"hermes", "shell"})
        self.assertEqual(remaining["hermes"]["index"], int(self.user_idx))
        self.assertEqual(remaining["shell"]["index"], int(self.shell_idx))

    def test_optional_opt_out_skips_auto_creation_while_explicit_command_still_works(self):
        r_up = self._cli("up", extra_env={"HERMES_TMUX_TESTBENCH": "0"})
        self.assertEqual(r_up.returncode, 0, r_up.stdout + r_up.stderr)
        names = [w["name"] for w in self._windows()]
        self.assertIn("agents", names)
        self.assertIn("hx:mail", names)
        self.assertNotIn("hx:testbench", names)

        # Explicit `hermes-tmux testbench` still opens and focuses `hx:testbench` on demand!
        r_tb = self._cli("testbench", extra_env={"HERMES_TMUX_TESTBENCH": "0"})
        self.assertEqual(r_tb.returncode, 0, r_tb.stdout + r_tb.stderr)
        self.assertIn("hx:testbench", [w["name"] for w in self._windows()])
        self._cli("down")


if __name__ == "__main__":
    unittest.main()
