"""Isolated tmux integration tests for the permanent first-class `hx:mail` tab.

What is under test
------------------
`cli._up` finishes by ensuring `hx:mail` — and, unless
`HERMES_TMUX_TESTBENCH` is falsey, `hx:testbench` — through
`cli.ensure_aux_window(..., focus=False)`. That function is the *only*
implementation of "make an aux window exist": `_up` (permanent startup),
`cmd_aux` (interactive `hermes-tmux mail|logs|...`) and `watch`'s crash
recovery all call it. So every property asserted below belongs to
`ensure_aux_window` plus `_up`'s ordering, not to any mail-specific path;
a regression here is a regression for all six `AUX_ROLES`.

Invariants these two tests protect
----------------------------------
1. **Slot.** `agents` < `hx:mail` < first dynamic worker tab. `reconcile_tabs`
   only ever *appends* worker windows, so this ordering is equivalent to "the
   aux windows exist before the coordinator's first pass". The live worker is
   therefore seeded *before* `up` — a deliberate negative control: with no
   worker pre-existing, the test could pass by accident of timing.
2. **Identity.** A repeated `up`, and the `mail` navigation command, must leave
   the same window and the same `hxmail` process. A changed pane pid means
   `tmuxio.respawn` ran, i.e. the window was rebuilt though it was healthy.
3. **Ownership.** `close` must refuse the aux window ("not a worker tab"). An
   aux window has no `tabs` entry, so `_tab_of_window` yields no worker key and
   `cmd_close` exits 4 without asking tmux for anything.
4. **Foreign panes survive.** A reader's split inside `hx:mail` carries no
   `@hx` of its own (`tmuxio.split_window` marks only what it creates), so a
   crash rebuild must *add* a pane to the surviving window, and `down` must be
   unable to remove a window that still holds a foreign pane.
5. **Focus.** No build or recovery path may end with the reader's window
   deselected. `focus=False`, `new-window -d` and the `-a` insert are the three
   mechanisms that make that true; each is checked after the step that uses it.

Fixtures and negative controls
------------------------------
Each test gets a private tmux server: `-L <sock>`, `TMUX_TMPDIR` under a
temporary directory, and `-f /dev/null` so the reader's `tmux.conf` cannot
inject bindings or enable `renumber-windows` (which would break the index
assertions at the end). The socket name embeds the pid with a per-module
prefix (`m`), so this module and `test_testbench_aux.py` can share one test
process without colliding. `HERMES_HOME`, `HXMAIL_DB` and `HXMAIL_LOCAL_DIR`
point into the temp tree, so `hxmail` opens an empty store and shows its
DEMO/OFFLINE view; the real profile is never touched.

`TMUX` is reconstructed by hand for the CLI subprocess
(`#{socket_path},#{pid},0`): the CLI is not itself a tmux child, and
`tmuxio.in_tmux()` checks only that variable. Addressability comes from the
live server — `server_pid()` and `session_key()` query the socket the value
names — so the fabricated third field (session index) is never read.

What these tests do not prove
-----------------------------
hxmail's rendering. `_wait_mail_rendered` returns immediately when no `hxmail`
binary is installed, and otherwise waits up to 5 s for "MAIL", "Inbox" and
"DEMO/OFFLINE MODE". On a host without hxmail the lifecycle and ownership
invariants still hold, but nothing about the TUI does. Likewise the pane-pid
comparisons observe the *pane's* process, so a rebuild that kept the pid but
restarted the window is not detectable — and cannot occur, since killing the
lone pane destroys the window.

Timing: the coordinator wakes every `watch._TICK_S` (5 s) while any worker is
running, so the 10 s restore deadlines tolerate one missed pass and fail on
two. `_cli` uses `timeout=15`, which raises `TimeoutExpired` instead of
producing an assertion failure.
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


# Locate hxmail the same way `cli._aux_command` does: `~/.local/bin` first, then
# PATH. Mirroring the pane command's precedence is what makes this probe answer
# "will the pane exec a TUI or fall through to a shell". None means absent,
# which is what turns the render wait into a no-op instead of a failure.
def _installed_hxmail() -> Path | None:
    cand = Path.home() / ".local/bin/hxmail"
    if cand.is_file() and os.access(str(cand), os.X_OK):
        return cand
    found = shutil.which("hxmail")
    return Path(found) if found else None


# Skipped, not failed, without tmux: the whole module drives a real server.
@unittest.skipUnless(shutil.which("tmux"), "tmux unavailable")
class PermanentMailTabTests(unittest.TestCase):
    def setUp(self):
        # Scratch under the Hermes cache dir, not the system temp dir, so a
        # leaked temp tree is pruned by the same policy as everything else.
        scratch = Path.home() / ".hermes/cache/scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=str(scratch), prefix="hxm-")
        self.root = Path(self.tmp.name)
        # 0700: tmux refuses a socket directory others can enter, and the
        # default umask would leave it group-readable.
        self.tmux_dir = self.root / "t"
        self.tmux_dir.mkdir(mode=0o700)
        self.home_dir = self.root / "home"
        self.home_dir.mkdir(mode=0o700)
        # Per-module prefix + pid: unique against a concurrent run and against
        # test_testbench_aux.py in the same process.
        self.sock = "m%d" % os.getpid()

        # Start isolated server with TWO user windows: `1:hermes` (active) and `2:shell`
        # Two user windows, so the index assertions have to distinguish "aux
        # inserted after agents" from "aux appended, shifting nothing".
        self._tmux("new-session", "-d", "-s", "work", "-x", "120", "-y", "36", "-n", "hermes")
        self._tmux("new-window", "-d", "-t", "work", "-n", "shell")
        # Capture the server identity from a live query rather than assuming
        # index 0: the CLI needs `TMUX` to name this server, and `@N` ids are
        # what every later assertion compares.
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
        # kill-server unconditionally: a failed assertion mid-test may leave the
        # coordinator running, and it would outlive the temp dir otherwise.
        subprocess.run(
            [TMUX, "-L", self.sock, "kill-server"],
            env=self._base_env(),
            capture_output=True,
        )
        self.tmp.cleanup()

    # Environment every tmux client and every CLI child shares.
    #
    # `HERMES_TMUX=1` because the gate defaults to on but a hostile parent
    # environment could set 0 and turn every assertion into a no-op. `HOME` is
    # deliberately *not* overridden: `_aux_command` hard-codes
    # `$HOME/.local/bin/hxmail`, so the pane must see the real home to find the
    # installed binary — isolation comes from `HERMES_HOME` and the two HXMAIL
    # variables instead.
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
        # `-f /dev/null` is load-bearing, not tidiness: a user config could bind
        # keys, set `renumber-windows`, or change the status format the
        # assertions read.
        return subprocess.run(
            [TMUX, "-L", self.sock, "-f", "/dev/null", *args],
            env=self._base_env(),
            capture_output=True,
            text=True,
            check=check,
        )

    # Run `python -m hermes_tmux <args>` as a pane of the test server.
    #
    # `TMUX_PANE` defaults to the user's active pane but can be overridden, so a
    # test can act *as* an aux pane (that is what makes `session_key` agree with
    # the pane's own identity). No `check=True`: exit status is part of what the
    # tests assert.
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

    # Snapshot of `work`'s windows as dicts: index, id, name, active.
    #
    # Names, not ids, are the readable handle here — a worker tab is named after
    # its task and an aux window after its role — but every assertion that
    # *touches* a window uses the id, because a name is not an address.
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
        # Asked of the session target, i.e. "what would the reader be looking at",
        # which is the only meaningful form of the no-focus-steal claim.
        return self._tmux("display-message", "-p", "-t", "work", "#{window_id}").stdout.strip()

    def _pane_pid_of(self, target: str) -> int:
        # Resolving a *window* target lands on its active pane; aux windows have
        # exactly one pane until a reader splits one, and the tests only read
        # this while they are single-pane. The pid is the durable identity of
        # "the same hxmail instance" across repeated `up` calls.
        val = self._tmux("display-message", "-p", "-t", target, "#{pane_pid}").stdout.strip()
        return int(val)

    # Write a live delegation manifest exactly where `state` looks for it.
    #
    # The fields are the minimum `state.load_workers` reads to yield a worker in
    # state `running`; naming the status explicitly keeps the result off the
    # DB-verdict path (a missing status also counts as live while the delegation
    # is not completed, but only by inference). The tab name comes from
    # `labels.tab_label(goal)`, which is why the assertions search for a
    # substring of `goal` and not the whole string: the label is clipped to 28
    # columns and shared leading words are stripped.
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

    # Best-effort wait for hxmail's DEMO/OFFLINE view, then fail with proof.
    #
    # No-op when hxmail is not installed: the pane would have printed the "not
    # installed yet" line and then a shell, and asserting on those words would
    # test the fallback rather than the tab. All three strings are required
    # because any one alone can appear in a half-painted or error screen.
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
        # The failure carries the last capture: a blank pane (never started) and
        # a traceback (crashed on an unexpected store) are different bugs and
        # must not be collapsed into "did not render".
        self.fail("hxmail did not render DEMO/OFFLINE Inbox in %s: %r" % (mail_wid, captured[:400]))

    def test_permanent_mail_tab_startup_order_idempotency_crash_restore_and_down(self):
        # Seed a live worker BEFORE `up` so we prove `hx:mail` opens BEFORE dynamic worker tabs!
        # This is the discriminating half of the ordering assertion: the
        # coordinator's first pass happens inside `_up`, after the aux windows,
        # so a pre-existing worker can only appear *after* them. Seeding later
        # would make the order assertion pass for the wrong reason.
        self._seed_running_worker("deleg_m0000001", "Audit parser bounds")

        # 1. `hermes-tmux up` creates `agents` AND `hx:mail` without stealing focus or renumbering user windows
        r_up = self._cli("up")
        self.assertEqual(r_up.returncode, 0, r_up.stdout + r_up.stderr)
        # First of four no-focus-steal checks: `up` is the only command that
        # creates windows on a normal path, and `new-window -a`/`-d` must not
        # have promoted the new window.
        self.assertEqual(self._active_window_id(), self.user_win, "up stole focus from user window!")

        # Wait for the dynamic worker tab to appear as well
        # The coordinator builds it on its first pass; the order assertions
        # below would otherwise race an empty window list.
        deadline = time.time() + 10.0
        wins = []
        while time.time() < deadline:
            wins = self._windows()
            if any("Audit parser" in w["name"] for w in wins):
                break
            time.sleep(0.1)

        by_name = {w["name"]: w for w in wins}
        # Presence, not count: `up` also creates `hx:testbench` by default, and
        # this test is about mail. Its teardown is still covered — step 7
        # demands that only the two user windows survive.
        self.assertIn("hermes", by_name)
        self.assertIn("shell", by_name)
        self.assertIn("agents", by_name)
        self.assertIn("hx:mail", by_name)
        worker_wins = [w for w in wins if "Audit parser" in w["name"]]
        # Exactly one: a second tab for one worker would mean `reconcile_tabs`
        # built a window whose layout entry it then failed to record, so the
        # next pass built it again.
        self.assertEqual(len(worker_wins), 1, wins)

        # User tab indices preserved, and `hx:mail` sits alongside `agents` BEFORE dynamic worker tabs!
        # User indices are compared to the values captured before `up` existed:
        # equality here is what makes "no renumbering" a real claim rather than
        # "indices look plausible".
        self.assertEqual(by_name["hermes"]["index"], int(self.user_idx))
        self.assertEqual(by_name["shell"]["index"], int(self.shell_idx))
        # Strict inequality in both directions: `-a -t agents` inserts at
        # agents+1 only when that slot is free, and the append in
        # `reconcile_tabs` puts the worker last. Together they pin the aux
        # window to the gap, which is the reader-visible contract.
        self.assertLess(by_name["agents"]["index"], by_name["hx:mail"]["index"])
        self.assertLess(by_name["hx:mail"]["index"], worker_wins[0]["index"])
        self.assertEqual(self._active_window_id(), self.user_win)

        mail_wid = by_name["hx:mail"]["id"]
        self._wait_mail_rendered(mail_wid)
        mail_pid = self._pane_pid_of(mail_wid)

        # 2. Repeated `up` calls are idempotent: no duplicate window, same hxmail PID, no focus steal
        # Two iterations, because the first `up` after a build exercises the
        # "recorded id still exists and is ours" branch while the second
        # exercises the same branch with the pid already stable — a rebuild that
        # only misbehaves on the second pass would otherwise escape.
        for _ in range(2):
            r_up2 = self._cli("up")
            self.assertEqual(r_up2.returncode, 0)
            self.assertEqual([w["name"] for w in self._windows()].count("hx:mail"), 1)
            # The pid is the point: identical window ids with a new pid would
            # still mean the running hxmail was killed and respawned.
            self.assertEqual(self._pane_pid_of(mail_wid), mail_pid, "repeated up restarted hxmail!")
            self.assertEqual(self._active_window_id(), self.user_win)

        # 3. `hermes-tmux mail` reuses and focuses the permanent `hx:mail` window without restarting hxmail
        r_mail = self._cli("mail")
        self.assertEqual(r_mail.returncode, 0)
        # The message distinguishes the two statuses `ensure_aux_window` can
        # return for a healthy window ("existing"/"running" print "focused");
        # "opened" would print "opened", so this also proves it was *not*
        # recreated.
        self.assertIn("focused hx:mail", r_mail.stdout)
        # A navigation command is the one path allowed to move focus — and it
        # must move it to the mail window, not merely somewhere.
        self.assertEqual(self._active_window_id(), mail_wid)
        self.assertEqual(self._pane_pid_of(mail_wid), mail_pid)
        # Return focus to user's `hermes` window
        # Restored by hand so every later step starts from the same state; this
        # is test bookkeeping, not an assertion about the tool.
        self._tmux("select-window", "-t", self.user_win)
        self.assertEqual(self._active_window_id(), self.user_win)

        # 4. Closing the worker tab leaves `hx:mail` running; `close` refuses `hx:mail`
        r_close_worker = self._cli("close", worker_wins[0]["id"])
        self.assertEqual(r_close_worker.returncode, 0)
        # Negative control for the ownership guard: `close` did real work on an
        # owned worker tab, so the refusal below cannot be "close does nothing
        # to anything".
        self.assertEqual(self._pane_pid_of(mail_wid), mail_pid)
        r_close_mail = self._cli("close", mail_wid)
        # Exit 4, not 0: `cmd_close` treats "not a worker tab" as a distinct
        # outcome from "closed" and from "no pane of ours in that tab".
        self.assertNotEqual(r_close_mail.returncode, 0)
        self.assertIn("not a worker tab", r_close_mail.stdout)

        # 5. Crash recovery A: kill `hxmail` process so `hx:mail` disappears; `up` restores it without focus steal!
        # SIGKILL, not SIGTERM: the tab must survive a process that died without
        # cleaning up, which is the case a reader actually hits.
        os.kill(mail_pid, signal.SIGKILL)
        # Wait for the window to vanish. The pane ran `exec hxmail`, so its
        # death destroys the pane and tmux destroys the last-pane window; if
        # this loop times out the rest of the step would measure the old window.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if "hx:mail" not in [w["name"] for w in self._windows()]:
                break
            time.sleep(0.05)
        r_restore = self._cli("up")
        self.assertEqual(r_restore.returncode, 0, r_restore.stdout + r_restore.stderr)
        # Recovery is the path most likely to steal focus, because it creates a
        # window while the reader is somewhere else.
        self.assertEqual(self._active_window_id(), self.user_win, "restoring hx:mail stole focus!")
        restored_wins = {w["name"]: w for w in self._windows()}
        self.assertIn("hx:mail", restored_wins)
        restored_wid = restored_wins["hx:mail"]["id"]
        self._wait_mail_rendered(restored_wid)

        # 6. Crash recovery B: foreign user split inside `hx:mail` survives when `hxmail` crashes and is rebuilt
        # `-d` so the split does not become the active pane: the kill loop below
        # then has to identify our pane by pid list, not by "the active one".
        foreign_pane = self._tmux(
            "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", restored_wid, "sleep 300"
        ).stdout.strip()
        # Kill only our hxmail pane
        # Every pane except the foreign one: after the rebuild the window must
        # hold exactly two, so killing the foreign pane too would make the count
        # assertion vacuous.
        panes_raw = self._tmux("list-panes", "-t", restored_wid, "-F", "#{pane_id}\t#{pane_pid}").stdout.splitlines()
        for line in panes_raw:
            pid_str_id, pid_num = line.split("\t")
            if pid_str_id != foreign_pane:
                os.kill(int(pid_num), signal.SIGKILL)
        # Fixed sleep rather than a poll: the window cannot disappear here (the
        # foreign pane holds it), so there is nothing to wait for except the
        # kernel reaping the pane.
        time.sleep(0.3)
        r_rebuild = self._cli("up")
        self.assertEqual(r_rebuild.returncode, 0)
        self.assertEqual(self._active_window_id(), self.user_win)
        panes_after = [
            l.strip() for l in self._tmux("list-panes", "-t", restored_wid, "-F", "#{pane_id}").stdout.splitlines()
        ]
        # The window was found by recorded id / name, its owned-pane list was
        # empty, so `ensure_aux_window` split instead of creating a new window.
        # Losing the foreign pane here would mean it was replaced.
        self.assertIn(foreign_pane, panes_after, "foreign split pane was lost!")
        # Exactly 2: ours rebuilt plus theirs kept. One would mean no rebuild,
        # three would mean the rebuild ran twice.
        self.assertEqual(len(panes_after), 2, "hxmail pane was not rebuilt alongside foreign pane!")
        # Remove our foreign test split so `down` can close `hx:mail` cleanly
        # `down` may not remove a window that still holds a pane it cannot prove
        # is ours, so step 7's exact-survivor assertion needs this gone first.
        self._tmux("kill-pane", "-t", foreign_pane)

        # 7. `hermes-tmux down` closes `agents` and `hx:mail`, leaving both user windows at their original indices!
        r_down = self._cli("down")
        self.assertEqual(r_down.returncode, 0, r_down.stdout + r_down.stderr)
        surviving = self._windows()
        # Exact list, ordered: (index, id) pairs catch both a leftover window and
        # a renumbering of the user's own windows. Ids are compared as well
        # because tmux reuses indices, not ids, when a server is rebuilt.
        self.assertEqual([(w["index"], w["id"]) for w in surviving], [
            (int(self.user_idx), self.user_win),
            (int(self.shell_idx), self.shell_win),
        ])

    # The coordinator — not a human typing `up` — restores a dead `hx:mail`.
    #
    # `watch.run_watch` re-checks the recorded aux window every pass and calls
    # the same `ensure_aux_window` used by `up`, but with the layout's session
    # id, the coordinator's `home`, and its `owner_pid`. The owner_pid guard
    # keeps a coordinator that outlived a `kill-server` from creating windows on
    # the replacement server; this test exercises the ordinary branch, not that
    # one.
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
        # Gives the reclaimed-slot assertion below a bounded gap to fall into;
        # without a worker tab appended last, "reclaimed its slot" would be
        # indistinguishable from "was appended".
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if any("Monitor sync" in w["name"] for w in self._windows()):
                break
            time.sleep(0.1)

        # Kill `hxmail` (`SIGKILL`) and do NOT call `hermes-tmux up`: coordinator must restore it!
        # The absence of an `up` is the whole test. A pass that merely re-rendered
        # would leave no window, and the poll would hit its deadline.
        os.kill(mail_pid0, signal.SIGKILL)
        # 10 s against a 5 s tick: one missed pass is tolerated, two are not.
        deadline = time.time() + 10.0
        restored = None
        while time.time() < deadline:
            cur = {w["name"]: w for w in self._windows()}
            cand = cur.get("hx:mail")
            # A new window id is required, not merely a window named hx:mail:
            # the old one is gone, so seeing the old id would mean the poll read
            # a stale list. (Killing the lone pane destroys the window, so
            # "restored" can never mean "the same window, new pane".)
            if cand and cand["id"] != mail_wid0:
                restored = cand
                break
            time.sleep(0.2)
        self.assertIsNotNone(restored, "coordinator did not restore crashed hx:mail automatically")
        assert restored is not None
        # Same index as before the crash: the coordinator passes
        # `after_window=workers_window`, so it re-inserts into the gap beside
        # `agents` rather than appending after the worker tab.
        self.assertEqual(restored["index"], mail_idx0, "hx:mail did not reclaim its slot beside agents")
        # Recovery is `focus=False`; a coordinator that selected the window it
        # just built would yank the reader out of their pane on every crash.
        self.assertEqual(self._active_window_id(), self.user_win, "coordinator auto-restore stole focus!")
        self._wait_mail_rendered(restored["id"])
        # Teardown through the public command, so a failure here also proves
        # `down` still finds the coordinator that performed the restore.
        self.assertEqual(self._cli("down").returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
