"""Isolated tmux integration tests for lazy `hx:mail` (`hermes-tmux mail`)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMUX = shutil.which("tmux") or "tmux"
PY = sys.executable or "/usr/bin/python3"
HXMAIL_RELEASE = Path("/Users/sein/.hermes/artifacts/hermes-terminal-env/worktrees/impl/target/release/hxmail")


@unittest.skipUnless(shutil.which("tmux"), "tmux unavailable")
class MailAuxWindowTests(unittest.TestCase):
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

        # Start isolated server and user session `work`
        subprocess.run(
            [TMUX, "-L", self.sock, "-f", "/dev/null", "new-session", "-d", "-s", "work", "-x", "120", "-y", "36"],
            env=self._base_env(),
            check=True,
        )
        sock_info = subprocess.run(
            [TMUX, "-L", self.sock, "display-message", "-p", "-t", "work", "#{socket_path},#{pid},0\t#{pane_id}\t#{window_id}"],
            env=self._base_env(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.tmux_env_val, self.user_pane, self.user_win = sock_info.split("\t")

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
        if HXMAIL_RELEASE.is_file():
            env["HXMAIL_BIN"] = str(HXMAIL_RELEASE)
        return env

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
        out = subprocess.run(
            [TMUX, "-L", self.sock, "list-windows", "-t", "work", "-F", "#{window_id}\t#{window_name}"],
            env=self._base_env(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        rows = []
        for line in out.splitlines():
            wid, wname = line.split("\t", 1)
            rows.append((wid, wname))
        return rows

    def test_mail_window_is_lazy_idempotent_protected_from_close_and_cleaned_by_down(self):
        # 1. `up` creates `agents`, NOT `hx:mail` (lazy!)
        r_up = self._cli("up")
        self.assertEqual(r_up.returncode, 0, r_up.stdout + r_up.stderr)
        names_after_up = [n for _, n in self._windows()]
        self.assertIn("agents", names_after_up)
        self.assertNotIn("hx:mail", names_after_up)

        # 2. `hermes-tmux mail` lazily creates and focuses `hx:mail` without touching user_win or agents
        r_mail1 = self._cli("mail")
        self.assertEqual(r_mail1.returncode, 0, r_mail1.stdout + r_mail1.stderr)
        self.assertIn("opened hx:mail", r_mail1.stdout)
        wins1 = dict((n, wid) for wid, n in self._windows())
        self.assertIn("hx:mail", wins1)
        self.assertIn(self.user_win, [wid for wid, _ in self._windows()])
        mail_wid = wins1["hx:mail"]

        # If real hxmail binary is present, wait briefly and confirm TUI rendered inside tmux pane!
        if HXMAIL_RELEASE.is_file():
            deadline = time.time() + 5.0
            captured = ""
            while time.time() < deadline:
                captured = subprocess.run(
                    [TMUX, "-L", self.sock, "capture-pane", "-p", "-t", mail_wid],
                    env=self._base_env(),
                    capture_output=True,
                    text=True,
                ).stdout
                if "MAIL" in captured and "Inbox" in captured:
                    break
                time.sleep(0.05)
            self.assertIn("MAIL", captured)
            self.assertIn("Inbox", captured)

        # 3. Second `hermes-tmux mail` focuses the SAME window rather than opening a duplicate
        r_mail2 = self._cli("mail")
        self.assertEqual(r_mail2.returncode, 0)
        self.assertIn("focused hx:mail", r_mail2.stdout)
        self.assertEqual([n for _, n in self._windows()].count("hx:mail"), 1)

        # 4. `hermes-tmux close` refuses to close `hx:mail` (it is not a worker tab!)
        r_close = self._cli("close", mail_wid)
        self.assertNotEqual(r_close.returncode, 0)
        self.assertIn("not a worker tab", r_close.stdout)
        self.assertIn("hx:mail", [n for _, n in self._windows()])

        # 5. `hermes-tmux down` closes `agents` and `hx:mail`, leaving the user's window intact!
        r_down = self._cli("down")
        self.assertEqual(r_down.returncode, 0, r_down.stdout + r_down.stderr)
        surviving = self._windows()
        self.assertEqual([wid for wid, _ in surviving], [self.user_win])


if __name__ == "__main__":
    unittest.main(verbosity=2)
