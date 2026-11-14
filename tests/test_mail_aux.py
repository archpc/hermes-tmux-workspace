"""Isolated tmux integration tests for permanent first-class `hx:mail` tab."""

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


def _installed_hxmail() -> Path | None:
    cand = Path.home() / ".local/bin/hxmail"
    if cand.is_file() and os.access(str(cand), os.X_OK):
        return cand
    found = shutil.which("hxmail")
    return Path(found) if found else None


@unittest.skipUnless(shutil.which("tmux"), "tmux unavailable")
class PermanentMailTabTests(unittest.TestCase):
    def setUp(self):
        scratch = Path.home() / ".hermes/cache/scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=str(scratch), prefix="hxm-")
        self.root = Path(self.tmp.name)
        self.tmux_dir = self.root / "t"
        self.tmux_dir.mkdir(mode=0o700)
        self.home_dir = self.root / "home"
        self.home_dir.mkdir(mode=0o700)
        self.sock = "m%d" % os.getpid()

        # Start isolated server with TWO user windows: `1:hermes` (active) and `2:shell`
        self._tmux("new-session", "-d", "-s", "work", "-x", "120", "-y", "36", "-n", "hermes")
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

    def _base_env(self):
        env = dict(os.environ)
        env["TMUX_TMPDIR"] = str(self.tmux_dir)
        env["HERMES_HOME"] = str(self.home_dir)
        env["HERMES_TMUX"] = "1"
        env["PYTHONPATH"] = str(REPO)
        env["HXMAIL_DB"] = str(self.root / "mail.db")
        env["HXMAIL_LOCAL_DIR"] = str(self.root / "local")
        return env

    def _tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [TMUX, "-L", self.sock, "-f", "/dev/null", *args],
            env=self._base_env(),
            capture_output=True,
            text=True,
            check=check,
        )

    def _cli(self, *args: str, pane: str = "") -> subprocess.CompletedProcess:
        env = self._base_env()
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

    def _wait_mail_rendered(self, mail_wid: str) -> None:
        if _installed_hxmail() is None:
            return
        deadline = time.time() + 5.0
        captured = ""
        while time.time() < deadline:
            captured = self._tmux("capture-pane", "-p", "-t", mail_wid, check=False).stdout
            if "MAIL" in captured and "Inbox" in captured and "DEMO/OFFLINE MODE" in captured:
                return
            time.sleep(0.05)
        self.fail("hxmail did not render DEMO/OFFLINE Inbox in %s: %r" % (mail_wid, captured[:400]))

    def test_permanent_mail_tab_startup_order_idempotency_crash_restore_and_down(self):
        # Seed a live worker BEFORE `up` so we prove `hx:mail` opens BEFORE dynamic worker tabs!
        self._seed_running_worker("deleg_m0000001", "Audit parser bounds")

        # 1. `hermes-tmux up` creates `agents` AND `hx:mail` without stealing focus or renumbering user windows
        r_up = self._cli("up")
        self.assertEqual(r_up.returncode, 0, r_up.stdout + r_up.stderr)
        self.assertEqual(self._active_window_id(), self.user_win, "up stole focus from user window!")

        # Wait for the dynamic worker tab to appear as well
        deadline = time.time() + 10.0
        wins = []
        while time.time() < deadline:
            wins = self._windows()
            if any("Audit parser" in w["name"] for w in wins):
                break
            time.sleep(0.1)

        by_name = {w["name"]: w for w in wins}
        self.assertIn("hermes", by_name)
        self.assertIn("shell", by_name)
        self.assertIn("agents", by_name)
        self.assertIn("hx:mail", by_name)
        worker_wins = [w for w in wins if "Audit parser" in w["name"]]
        self.assertEqual(len(worker_wins), 1, wins)

        # User tab indices preserved, and `hx:mail` sits alongside `agents` BEFORE dynamic worker tabs!
        self.assertEqual(by_name["hermes"]["index"], int(self.user_idx))
        self.assertEqual(by_name["shell"]["index"], int(self.shell_idx))
        self.assertLess(by_name["agents"]["index"], by_name["hx:mail"]["index"])
        self.assertLess(by_name["hx:mail"]["index"], worker_wins[0]["index"])
        self.assertEqual(self._active_window_id(), self.user_win)

        mail_wid = by_name["hx:mail"]["id"]
        self._wait_mail_rendered(mail_wid)
        mail_pid = self._pane_pid_of(mail_wid)

        # 2. Repeated `up` calls are idempotent: no duplicate window, same hxmail PID, no focus steal
        for _ in range(2):
            r_up2 = self._cli("up")
            self.assertEqual(r_up2.returncode, 0)
            self.assertEqual([w["name"] for w in self._windows()].count("hx:mail"), 1)
            self.assertEqual(self._pane_pid_of(mail_wid), mail_pid, "repeated up restarted hxmail!")
            self.assertEqual(self._active_window_id(), self.user_win)

        # 3. `hermes-tmux mail` reuses and focuses the permanent `hx:mail` window without restarting hxmail
        r_mail = self._cli("mail")
        self.assertEqual(r_mail.returncode, 0)
        self.assertIn("focused hx:mail", r_mail.stdout)
        self.assertEqual(self._active_window_id(), mail_wid)
        self.assertEqual(self._pane_pid_of(mail_wid), mail_pid)
        # Return focus to user's `hermes` window
        self._tmux("select-window", "-t", self.user_win)
        self.assertEqual(self._active_window_id(), self.user_win)

        # 4. Closing the worker tab leaves `hx:mail` running; `close` refuses `hx:mail`
        r_close_worker = self._cli("close", worker_wins[0]["id"])
        self.assertEqual(r_close_worker.returncode, 0)
        self.assertEqual(self._pane_pid_of(mail_wid), mail_pid)
        r_close_mail = self._cli("close", mail_wid)
        self.assertNotEqual(r_close_mail.returncode, 0)
        self.assertIn("not a worker tab", r_close_mail.stdout)

        # 5. Crash recovery A: kill `hxmail` process so `hx:mail` disappears; `up` restores it without focus steal!
        os.kill(mail_pid, signal.SIGKILL)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if "hx:mail" not in [w["name"] for w in self._windows()]:
                break
            time.sleep(0.05)
        r_restore = self._cli("up")
        self.assertEqual(r_restore.returncode, 0, r_restore.stdout + r_restore.stderr)
        self.assertEqual(self._active_window_id(), self.user_win, "restoring hx:mail stole focus!")
        restored_wins = {w["name"]: w for w in self._windows()}
        self.assertIn("hx:mail", restored_wins)
        restored_wid = restored_wins["hx:mail"]["id"]
        self._wait_mail_rendered(restored_wid)

        # 6. Crash recovery B: foreign user split inside `hx:mail` survives when `hxmail` crashes and is rebuilt
        foreign_pane = self._tmux(
            "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", restored_wid, "sleep 300"
        ).stdout.strip()
        # Kill only our hxmail pane
        panes_raw = self._tmux("list-panes", "-t", restored_wid, "-F", "#{pane_id}\t#{pane_pid}").stdout.splitlines()
        for line in panes_raw:
            pid_str_id, pid_num = line.split("\t")
            if pid_str_id != foreign_pane:
                os.kill(int(pid_num), signal.SIGKILL)
        time.sleep(0.3)
        r_rebuild = self._cli("up")
        self.assertEqual(r_rebuild.returncode, 0)
        self.assertEqual(self._active_window_id(), self.user_win)
        panes_after = [
            l.strip() for l in self._tmux("list-panes", "-t", restored_wid, "-F", "#{pane_id}").stdout.splitlines()
        ]
        self.assertIn(foreign_pane, panes_after, "foreign split pane was lost!")
        self.assertEqual(len(panes_after), 2, "hxmail pane was not rebuilt alongside foreign pane!")
        # Remove our foreign test split so `down` can close `hx:mail` cleanly
        self._tmux("kill-pane", "-t", foreign_pane)

        # 7. `hermes-tmux down` closes `agents` and `hx:mail`, leaving both user windows at their original indices!
        r_down = self._cli("down")
        self.assertEqual(r_down.returncode, 0, r_down.stdout + r_down.stderr)
        surviving = self._windows()
        self.assertEqual([(w["index"], w["id"]) for w in surviving], [
            (int(self.user_idx), self.user_win),
            (int(self.shell_idx), self.shell_win),
        ])

    def test_coordinator_auto_restores_crashed_mail_tab_without_manual_up(self):
        self._seed_running_worker("deleg_m0000002", "Monitor sync state")
        self.assertEqual(self._cli("up").returncode, 0)
        wins0 = {w["name"]: w for w in self._windows()}
        self.assertIn("hx:mail", wins0)
        mail_wid0 = wins0["hx:mail"]["id"]
        mail_idx0 = wins0["hx:mail"]["index"]
        self._wait_mail_rendered(mail_wid0)
        mail_pid0 = self._pane_pid_of(mail_wid0)

        # Wait until dynamic worker tab opens so `hx:mail` sits between `agents` and the worker
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if any("Monitor sync" in w["name"] for w in self._windows()):
                break
            time.sleep(0.1)

        # Kill `hxmail` (`SIGKILL`) and do NOT call `hermes-tmux up`: coordinator must restore it!
        os.kill(mail_pid0, signal.SIGKILL)
        deadline = time.time() + 10.0
        restored = None
        while time.time() < deadline:
            cur = {w["name"]: w for w in self._windows()}
            cand = cur.get("hx:mail")
            if cand and cand["id"] != mail_wid0:
                restored = cand
                break
            time.sleep(0.2)
        self.assertIsNotNone(restored, "coordinator did not restore crashed hx:mail automatically")
        assert restored is not None
        self.assertEqual(restored["index"], mail_idx0, "hx:mail did not reclaim its slot beside agents")
        self.assertEqual(self._active_window_id(), self.user_win, "coordinator auto-restore stole focus!")
        self._wait_mail_rendered(restored["id"])
        self.assertEqual(self._cli("down").returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
