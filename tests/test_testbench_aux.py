"""Isolated tmux integration tests for the permanent first-class `hx:testbench` tab.

What is under test
------------------
`cli._up` creates `hx:testbench` after `hx:mail`, guarded by
`cli.testbench_tab_enabled()` (the `HERMES_TMUX_TESTBENCH` toggle), and both
windows come from `cli.ensure_aux_window`. `watch.run_watch` restores the same
window on every pass under the same toggle, so the tab is permanent in both
senses: `up` re-creates it after a crash, and the coordinator does too when
nobody runs `up` at all. This module pins that lifecycle plus its position in
the window order; `tests/test_mail_aux.py` covers the shared mechanism, so a
failure here that also fails there is an `ensure_aux_window` bug, not a
TestBench one.

Invariants these two tests protect
----------------------------------
1. **Coexistence.** `agents` < `hx:mail` < `hx:testbench` < first dynamic worker
   tab. The chain is produced by `_up` passing `after_window` down the line
   (`mail` after `agents`, `testbench` after `mail`), and `tmuxio.new_window`
   honours it only when the index immediately after the anchor is free — which
   is why the server here starts with exactly two contiguous user windows.
   `reconcile_tabs` appends worker windows, so they can only land last.
2. **Identity.** A repeated `up` must not restart the TUI: the pane pid is
   compared before and after, and a change means `respawn` ran on a healthy
   window.
3. **Nav vs build.** `hermes-tmux testbench` focuses the window (moving focus is
   the command's purpose); `up` and recovery must never move it.
4. **Ownership.** `close` refuses the aux window ("not a worker tab") because it
   has no `tabs` entry, so `_tab_of_window` yields no worker key.
5. **Opt-out is narrow.** `HERMES_TMUX_TESTBENCH=0` suppresses auto-creation
   (`up`, and the coordinator's restore via `testbench_tab_enabled`) but must
   not remove the command: `hermes-tmux testbench` still opens the tab on
   demand. `cmd_aux` never consults the toggle — that asymmetry is the contract.

Fixtures and negative controls
------------------------------
Private server per test (`-L tb<pid>`, `TMUX_TMPDIR` in a temp tree,
`-f /dev/null`), distinct socket prefix from `test_mail_aux.py` so both modules
can run in one process. `HERMES_HOME`/`HXMAIL_DB`/`HXMAIL_LOCAL_DIR` isolate the
mail half of `up`; `TESTBENCH_HOME` points at its own empty directory so the
"no suite ran on startup" control has something to look at. `TESTBENCH_SCRATCH_DIR`
is honoured when set, otherwise the same `~/.hermes/cache/scratch` is used.

`TMUX` is rebuilt by hand (`#{socket_path},#{pid},0`) because the CLI is not a
tmux child; `tmuxio.in_tmux()` reads only that variable, while addressing comes
from the live server the socket names.

What these tests do not prove
-----------------------------
The TUI. `_wait_testbench_rendered` returns immediately when no `testbench`
binary exists and otherwise waits up to 6 s for the word "TESTBENCH" — a
header, not a working session. The ordering, ownership and toggle assertions
hold without the binary; the rendering claim does not.

Teardown here is checked by window *names* (`{"hermes", "shell"}`), where
`test_mail_aux.py` compares exact `(index, id)` pairs; a renumbering of the
user's windows would pass this module and fail that one. The foreign-pane step
likewise asserts only that the reader's pane survived, not that ours was
rebuilt beside it.

Timing: the coordinator wakes on `watch._TICK_S` (5 s) while a worker runs, so
the 10 s waits tolerate one missed pass. `_cli` uses `timeout=15`, which raises
`TimeoutExpired` rather than failing an assertion.
"""

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


# Locate testbench the way `cli._aux_command` does: `~/.local/bin` first, then
# PATH. Same precedence as the pane command, so a hit here means the pane will
# exec the TUI rather than print the "not installed yet" line and drop to a
# shell. None is what makes the render wait a no-op instead of a failure.
def _installed_testbench() -> Path | None:
    cand = Path.home() / ".local/bin/testbench"
    if cand.is_file() and os.access(str(cand), os.X_OK):
        return cand
    found = shutil.which("testbench")
    return Path(found) if found else None


# Skipped, not failed, without tmux: these tests drive a real server.
@unittest.skipUnless(shutil.which("tmux"), "tmux unavailable")
class PermanentTestBenchTabTests(unittest.TestCase):
    def setUp(self):
        # `pwd.getpwuid` instead of `$HOME`: the temp tree must sit under the
        # real home even when the invoking shell exports a different HOME (the
        # CLI would otherwise inherit it and `_aux_command`'s
        # `$HOME/.local/bin/testbench` probe would miss).
        import pwd
        tb_sc = os.environ.get("TESTBENCH_SCRATCH_DIR", "").strip()
        scratch = Path(tb_sc) if tb_sc else (Path(pwd.getpwuid(os.getuid()).pw_dir) / ".hermes/cache/scratch")
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=str(scratch), prefix="hxtb-")
        self.root = Path(self.tmp.name)
        # 0700: tmux refuses a socket directory other users can enter.
        self.tmux_dir = self.root / "t"
        self.tmux_dir.mkdir(mode=0o700)
        self.home_dir = self.root / "home"
        self.home_dir.mkdir(mode=0o700)
        # TestBench's own store, separate from HERMES_HOME, so the
        # "no suite ran on startup" check reads a directory nothing else writes.
        self.tb_home = self.root / "tb_home"
        # Prefix `tb` (mail uses `m`) so both modules can share one test process.
        self.sock = "tb%d" % os.getpid()

        # Start isolated server with TWO user windows: `1:hermes` (active) and `2:shell`
        # Size is pinned so captures have deterministic dimensions; detached
        # windows inherit the session's default size rather than the client's.
        self._tmux("new-session", "-d", "-s", "work", "-x", "132", "-y", "38", "-n", "hermes")
        self._tmux("new-window", "-d", "-t", "work", "-n", "shell")
        # Read identity back from the live server instead of assuming index 0 or
        # a particular id; these are what every later comparison uses.
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
        # Unconditional: a failed assertion can leave the coordinator alive, and
        # it holds the socket directory this cleanup is about to delete.
        subprocess.run(
            [TMUX, "-L", self.sock, "kill-server"],
            env=self._base_env(),
            capture_output=True,
        )
        self.tmp.cleanup()

    # Environment every tmux client and every CLI child shares.
    #
    # `HERMES_TMUX=1` because the gate defaults to on but an inherited 0 would
    # turn each assertion into a no-op. `HOME` is deliberately not overridden:
    # `_aux_command` hard-codes `$HOME/.local/bin/testbench`, so the pane needs
    # the real home; isolation is `HERMES_HOME` plus the store variables.
    # `extra` is the per-call override used by the opt-out test, and it is
    # applied last so a caller can always win.
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
        # `-f /dev/null` is load-bearing: a user config could bind keys, enable
        # `renumber-windows`, or change the status format read by the
        # assertions.
        return subprocess.run(
            [TMUX, "-L", self.sock, "-f", "/dev/null", *args],
            env=self._base_env(),
            capture_output=True,
            text=True,
            check=check,
        )

    # Run `python -m hermes_tmux <args>` as a pane of the test server.
    #
    # `TMUX_PANE` defaults to the user's pane but can be overridden so a test can
    # act as an aux pane. `extra_env` rides on `_base_env`, which is how the
    # opt-out test changes `HERMES_TMUX_TESTBENCH` for exactly one invocation —
    # the `up` process's own environment, which `env.pane_env()` then copies
    # into the windows it creates.
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

    # Snapshot of `work`'s windows as dicts: index, id, name, active.
    #
    # Names identify a role or a task; ids are what the file uses whenever it
    # touches a window, because a name is not an address.
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
        # "What the reader is looking at" — the only meaningful form of the
        # no-focus-steal claim.
        return self._tmux("display-message", "-p", "-t", "work", "#{window_id}").stdout.strip()

    def _pane_pid_of(self, target: str) -> int:
        # A window target resolves to its active pane; aux windows are
        # single-pane until a reader splits one, and this is only read while
        # they are. The pid is the durable "same TUI instance" identity.
        val = self._tmux("display-message", "-p", "-t", target, "#{pane_pid}").stdout.strip()
        return int(val)

    # Write a live delegation manifest where `state.load_workers` looks.
    #
    # Fields are the minimum that module reads to yield state `running`; naming
    # the status keeps the result off the DB-verdict path rather than relying on
    # the "no status but not completed counts as live" inference. The tab name
    # is `labels.tab_label(goal)`, clipped to 28 columns, so the assertions match
    # a prefix of the goal.
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

    # Best-effort wait for the TUI header, then fail with the last capture.
    #
    # No-op when testbench is not installed: the pane would show the "not
    # installed yet" line and a shell, and asserting on that would test the
    # fallback. The header word alone is a weak signal by design — this test
    # owns the lifecycle, not the TUI's contents.
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
        # Seeded before `up` on purpose: the coordinator's first pass runs inside
        # `_up`, after both aux windows, and `reconcile_tabs` appends, so a
        # pre-existing worker can only land after them. Seeding later would let
        # the order assertions pass for a reason the code does not guarantee.
        self._seed_running_worker("deleg_tb000001", "Verify adapter parser")

        # 1. `hermes-tmux up` creates `agents`, `hx:mail`, AND `hx:testbench` detached without focus steal
        r_up = self._cli("up")
        self.assertEqual(r_up.returncode, 0, r_up.stdout + r_up.stderr)
        # Checked before any polling: a later `select-window` made by a test
        # helper would mask a steal performed by `up` itself.
        self.assertEqual(self._active_window_id(), self.user_win, "up stole focus from user window!")

        # Wait for the worker tab, otherwise the "testbench precedes it"
        # assertion would race an empty window list.
        deadline = time.time() + 10.0
        wins = []
        while time.time() < deadline:
            wins = self._windows()
            if any("Verify adapter" in w["name"] for w in wins):
                break
            time.sleep(0.1)

        by_name = {w["name"]: w for w in wins}
        # All five in one loop: the point of this test is coexistence, so a
        # missing mail tab must fail here rather than be silently tolerated.
        for expected in ("hermes", "shell", "agents", "hx:mail", "hx:testbench"):
            self.assertIn(expected, by_name, wins)
        worker_wins = [w for w in wins if "Verify adapter" in w["name"]]
        # Exactly one tab per worker: two would mean a pass built a window whose
        # layout entry it then failed to record.
        self.assertEqual(len(worker_wins), 1, wins)

        # Window order: user tabs preserved, then agents < hx:mail < hx:testbench < dynamic worker tab
        # Compared against the indices captured before `up` ran, so equality is
        # a real no-renumbering claim rather than a plausible ordering.
        self.assertEqual(by_name["hermes"]["index"], int(self.user_idx))
        self.assertEqual(by_name["shell"]["index"], int(self.shell_idx))
        # Direction matters as much as the gap: `testbench` is inserted after
        # `mail` (which `_up` passes as its anchor), so `mail < testbench` also
        # proves `_up` used the freshly returned mail id and not the overview.
        self.assertLess(by_name["agents"]["index"], by_name["hx:mail"]["index"])
        self.assertLess(by_name["hx:mail"]["index"], by_name["hx:testbench"]["index"])
        self.assertLess(by_name["hx:testbench"]["index"], worker_wins[0]["index"])
        self.assertEqual(self._active_window_id(), self.user_win)

        tb_wid = by_name["hx:testbench"]["id"]
        self._wait_testbench_rendered(tb_wid)
        tb_pid = self._pane_pid_of(tb_wid)

        # Must NOT auto-run any suites on startup!
        # Conditional as written: if the TUI never created `runs/` there is
        # nothing to inspect, so this control only fires after a run store
        # exists. It catches "TUI launched and immediately started a suite",
        # not "TUI never launched" — the render wait above covers that half.
        if (self.tb_home / "runs").is_dir():
            self.assertEqual(list((self.tb_home / "runs").iterdir()), [])

        # 2. Repeated `up` calls are idempotent: single `hx:testbench`, same PID, no focus steal
        # Two iterations: the first covers "recorded id still ours", the second
        # the same decision with a pid that has already been stable once.
        for _ in range(2):
            r_up2 = self._cli("up")
            self.assertEqual(r_up2.returncode, 0)
            self.assertEqual([w["name"] for w in self._windows()].count("hx:testbench"), 1)
            # Same pid, not just same window: a respawn would keep the id.
            self.assertEqual(self._pane_pid_of(tb_wid), tb_pid, "repeated up restarted testbench TUI!")
            self.assertEqual(self._active_window_id(), self.user_win)

        # 3. `hermes-tmux testbench` focuses `hx:testbench` explicitly without restarting TUI
        r_tb = self._cli("testbench")
        self.assertEqual(r_tb.returncode, 0)
        # "focused", not "opened": the window already existed, so this also
        # proves the navigation path called `ensure_aux_window` with focus and
        # got `existing` back instead of recreating.
        self.assertIn("focused hx:testbench", r_tb.stdout)
        # Navigation is the one path allowed to move focus, and it must move it
        # to *this* window, not merely somewhere.
        self.assertEqual(self._active_window_id(), tb_wid)
        self.assertEqual(self._pane_pid_of(tb_wid), tb_pid)
        # Bookkeeping so the recovery step starts from the pre-command state.
        self._tmux("select-window", "-t", self.user_win)
        self.assertEqual(self._active_window_id(), self.user_win)

        # 4. `close` refuses `hx:testbench` (permanent aux, not a worker tab)
        r_close_tb = self._cli("close", tb_wid)
        # Exit 4 from `cmd_close`, distinct from "closed" and from "no pane of
        # ours in that tab": the aux window has no `tabs` entry at all.
        self.assertNotEqual(r_close_tb.returncode, 0)
        self.assertIn("not a worker tab", r_close_tb.stdout)

        # 5. Crash recovery: kill TUI process -> background coordinator or `up` restores `hx:testbench` detached!
        # SIGKILL: a process that died without cleanup is the case a reader
        # actually hits, and the pane ran `exec testbench tui`, so killing it
        # destroys the lone pane and with it the window.
        os.kill(tb_pid, signal.SIGKILL)
        # Wait for the window to disappear; continuing early would measure the
        # dead window and make "restored" meaningless.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if "hx:testbench" not in [w["name"] for w in self._windows()]:
                break
            time.sleep(0.05)
        r_restore = self._cli("up")
        self.assertEqual(r_restore.returncode, 0, r_restore.stdout + r_restore.stderr)
        # Either the coordinator or this `up` may win the race; both converge on
        # one window, and neither may leave the reader elsewhere.
        self.assertEqual(self._active_window_id(), self.user_win, "restoring hx:testbench stole focus!")
        restored_wins = {w["name"]: w for w in self._windows()}
        self.assertIn("hx:testbench", restored_wins)
        restored_wid = restored_wins["hx:testbench"]["id"]
        self._wait_testbench_rendered(restored_wid)

        # 6. Foreign user split inside `hx:testbench` survives crash + rebuild AND `down`
        # `-d` so the split is not the active pane; the kill loop below then has
        # to identify our pane by pid list rather than by position.
        foreign_pane = self._tmux(
            "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", restored_wid, "sleep 300"
        ).stdout.strip()
        # Kill every pane but the foreign one. The window survives because that
        # pane still holds it, which is the state `ensure_aux_window` rebuilds
        # into: empty owned-pane list, existing window, so it splits.
        for line in self._tmux("list-panes", "-t", restored_wid, "-F", "#{pane_id}\t#{pane_pid}").stdout.splitlines():
            pid_id, pid_num = line.split("\t")
            if pid_id != foreign_pane:
                os.kill(int(pid_num), signal.SIGKILL)
        # Fixed sleep, no poll: the window cannot vanish (the foreign pane holds
        # it), so the only thing to wait for is the kernel reaping our pane.
        time.sleep(0.25)
        self.assertEqual(self._cli("up").returncode, 0)
        panes_after = self._tmux("list-panes", "-t", restored_wid, "-F", "#{pane_id}").stdout.splitlines()
        # The discriminating assertion for the ownership rule: a rebuild that
        # replaced the window (rather than adding a pane) loses the reader's
        # split, which is exactly what `down` is forbidden to do as well.
        self.assertIn(foreign_pane, panes_after)
        # Not asserted here (test_mail_aux.py does assert the count): with only
        # this check, a rebuild that also failed to add our pane would pass, as
        # long as the foreign pane stayed. Step 7's teardown is what would then
        # notice a leftover window.
        self._tmux("kill-pane", "-t", foreign_pane)

        # 7. Safe teardown via `hermes-tmux down` removes `agents`, `hx:mail`, `hx:testbench` and leaves user windows
        r_down = self._cli("down")
        self.assertEqual(r_down.returncode, 0, r_down.stdout + r_down.stderr)
        remaining = {w["name"]: w for w in self._windows()}
        # Set equality on names: any surviving aux/worker window fails, but a
        # renumbering of the two user windows would not (mail's test checks exact
        # (index, id) pairs for that).
        self.assertEqual(set(remaining), {"hermes", "shell"})
        self.assertEqual(remaining["hermes"]["index"], int(self.user_idx))
        self.assertEqual(remaining["shell"]["index"], int(self.shell_idx))

    # Opt-out is about *auto*-creation, not about the command existing.
    #
    # `testbench_tab_enabled()` is read by `_up` (skip creation) and by
    # `watch.run_watch` (skip restore); `cmd_aux` never reads it. So one env var
    # decides whether a tab appears unbidden without taking away the way to ask
    # for it. `env.pane_env()` copies the variable into every window it makes,
    # which is what makes the coordinator inherit the same answer.
    def test_optional_opt_out_skips_auto_creation_while_explicit_command_still_works(self):
        r_up = self._cli("up", extra_env={"HERMES_TMUX_TESTBENCH": "0"})
        self.assertEqual(r_up.returncode, 0, r_up.stdout + r_up.stderr)
        names = [w["name"] for w in self._windows()]
        # `agents` and `hx:mail` still built: this is a TestBench-specific
        # toggle, not a general aux opt-out. Asserting them together is what
        # keeps a future "if disabled, skip all aux" bug from passing.
        self.assertIn("agents", names)
        self.assertIn("hx:mail", names)
        self.assertNotIn("hx:testbench", names)

        # Explicit `hermes-tmux testbench` still opens and focuses `hx:testbench` on demand!
        # No toggle in this call's environment either — the assertion is that the
        # command does not consult it, not that the environment was reset.
        r_tb = self._cli("testbench", extra_env={"HERMES_TMUX_TESTBENCH": "0"})
        self.assertEqual(r_tb.returncode, 0, r_tb.stdout + r_tb.stderr)
        # The coordinator started by the first `up` carries the toggle through
        # `pane_env` and will not restore this window, so what exists here was
        # made by `cmd_aux` and stays made.
        self.assertIn("hx:testbench", [w["name"] for w in self._windows()])
        # No exit-status assertion: `cmd_down` returns 1 only when a coordinator
        # still holds the lock, and this test does not wait for it to stop.
        self._cli("down")


if __name__ == "__main__":
    unittest.main()
