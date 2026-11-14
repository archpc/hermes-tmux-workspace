"""Tests for the terminal-open hook's workspace bootstrap (``scripts/tmux-attach``).

The hook is a zsh script that ends in ``exec tmux attach-session``, so every test
here runs it on a **real pty** (its guards require a terminal) and intercepts only
the final ``attach-session`` with a shim on PATH: the attach itself is not what is
under test, and a real attach would block.

Isolation rules, so this file can never reach the user's server or state:

* a private ``-L`` socket and a private ``-f /dev/null`` config, passed to the
  script through ``TMUX_ATTACH_SOCKET``/``TMUX_ATTACH_CONFIG``;
* a private ``TMUX_TMPDIR``, so even a bug that dropped the ``-L`` would land in
  the sandbox and not on the default socket;
* a private ``HERMES_HOME``, so no real layout, log, or coordinator is touched;
* the script's PATH points at a shim directory first (``tmux`` forwarding shim,
  whose only interception is ``attach-session``, plus symlinks to the repo's
  launcher and to this interpreter).

Every assertion reads what tmux actually holds (window names, the windows' own
``@hx``/``@hx_key`` options, ``watch.pid``, process liveness), never a log line
the script wrote about itself.
"""

from __future__ import annotations

import os
import pty
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "tmux-attach"
LAUNCHER = REPO / "hermes-tmux"
_TMUX = shutil.which("tmux")
SKIP = _TMUX is None

SOCKET = "hx_ab_%d" % os.getpid()
CONFIG = "/dev/null"
AGENTS = "agents"
MAIL = "hx:mail"
BOOTSTRAP = "hx:bootstrap"


#: The socket directory of the test currently running. `tmux -L name` resolves
#: to ``$TMUX_TMPDIR/tmux-<uid>/name``, so the helper and the hook must agree on
#: it: reading the ambient TMUX_TMPDIR told the helper a different server existed
#: and every readiness poll waited on an empty one.
_SOCKET_DIR = {"dir": None}


def _tmux(*args: str, timeout: float = 20.0):
    env = dict(os.environ)
    if _SOCKET_DIR["dir"]:
        env["TMUX_TMPDIR"] = _SOCKET_DIR["dir"]
    proc = subprocess.run([_TMUX, "-L", SOCKET, "-f", CONFIG, *args],
                          capture_output=True, text=True, timeout=timeout, env=env)
    return proc.returncode, (proc.stdout + proc.stderr).rstrip("\n")


def _wait_for(predicate, timeout: float = 60.0, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def _windows(session: str):
    """[(window_id, name, @hx, @hx_key)] for one exact session."""
    rc, out = _tmux("list-windows", "-t", "=%s" % session,
                    "-F", "#{window_id}|#{window_name}|#{@hx}|#{@hx_key}")
    if rc != 0 or not out.strip():
        return []
    return [tuple(line.split("|")) for line in out.splitlines()]


def _window_ids(session: str):
    return [w[0] for w in _windows(session)]


def _named(session: str, name: str):
    return [w for w in _windows(session) if w[1] == name]


def _current_window(session: str) -> str:
    rc, out = _tmux("display-message", "-p", "-t", "=%s:" % session, "#{window_id}")
    return out.strip() if rc == 0 else ""


def _sessions():
    rc, out = _tmux("list-sessions", "-F", "#{session_name}")
    return [s for s in out.splitlines() if s.strip()] if rc == 0 else []


def _coordinator_pid(key: str, home: Path):
    pidfile = home / "cache" / "hermes-tmux" / "sessions" / key / "watch.pid"
    if not pidfile.is_file():
        return None
    try:
        pid = int(pidfile.read_text().strip() or 0)
    except (OSError, ValueError):
        return None
    return pid or None


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _processes_for(key: str):
    """Pids of this workspace's own `watch`/`follow` processes.

    The `sh -c` wrapper tmux uses for `run-shell -b` carries the same words on
    its command line, so matching is on the exact trailing command instead.
    """
    proc = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True, text=True)
    tails = ("hermes_tmux watch --key %s" % key, "hermes_tmux follow --key %s" % key)
    pids = []
    for line in proc.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if command.rstrip().endswith(tails) and pid.isdigit():
            pids.append(int(pid))
    return pids


class AttachBootstrap(unittest.TestCase):

    # ---------------- sandbox ----------------

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hx_attach_"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.tmux_tmpdir = self.tmp / "tmux"
        self.tmux_tmpdir.mkdir()
        _SOCKET_DIR["dir"] = str(self.tmux_tmpdir)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        shim = self.bin / "tmux"
        shim.write_text(
            "#!/bin/sh\n"
            "for a in \"$@\"; do\n"
            "  if [ \"$a\" = attach-session ]; then echo 'attach intercepted'; exit 0; fi\n"
            "done\n"
            "exec %s \"$@\"\n" % _TMUX)
        shim.chmod(0o755)
        for name, target in (("hermes-tmux", LAUNCHER), ("python3", sys.executable)):
            link = self.bin / name
            if not link.exists():
                link.symlink_to(target)
        self._killed = False

    def tearDown(self):
        _tmux("kill-server")
        _wait_for(lambda: not _sessions(), 10)
        shutil.rmtree(self.tmp, ignore_errors=True)
        _SOCKET_DIR["dir"] = None

    # ---------------- running the hook ----------------

    def env(self, **extra):
        env = {
            "HOME": str(self.tmp),
            "PATH": "%s:/usr/bin:/bin:/usr/sbin:/sbin" % self.bin,
            "LANG": "en_US.UTF-8",
            "TERM": "xterm-256color",
            "HERMES_HOME": str(self.home),
            "TMUX_TMPDIR": str(self.tmux_tmpdir),
            "TMUX_ATTACH_SOCKET": SOCKET,
            "TMUX_ATTACH_CONFIG": CONFIG,
        }
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run_hook(self, args=(), reply="", timeout=90.0, **extra):
        """Run the hook on a pty; return (rc, output)."""
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(["/bin/zsh", str(SCRIPT), *args],
                                    stdin=slave, stdout=slave, stderr=slave,
                                    env=self.env(**extra), close_fds=True,
                                    start_new_session=True)
            os.close(slave)
            slave = -1
            if reply:
                os.write(master, reply.encode())
            chunks = []
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
                if proc.poll() is not None:
                    # Drain what is left, then stop.
                    for _ in range(50):
                        try:
                            more = os.read(master, 65536)
                        except OSError:
                            more = b""
                        if not more:
                            break
                        chunks.append(more)
                    break
            try:
                rc = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                rc = proc.wait(timeout=10)
            return rc, b"".join(chunks).decode("utf-8", "replace")
        finally:
            if slave != -1:
                os.close(slave)
            os.close(master)

    # ---------------- assertions ----------------

    def build_ready(self, session="main", home=None):
        """Wait until the bootstrap produced the whole workspace."""
        def ready():
            agents = _named(session, AGENTS)
            mail = _named(session, MAIL)
            if not agents or not mail:
                return False
            if agents[0][2] != "1" or not agents[0][3]:
                return False
            if mail[0][2] != "1":
                return False
            return _alive(_coordinator_pid(agents[0][3], home or self.home))
        return _wait_for(ready)

    def assert_no_bootstrap_window(self, session):
        # The pane closes when `up` returns, which can be a moment after the
        # workspace is complete; a window that outlives that is a leak, so the
        # assertion waits for the exit instead of racing it.
        self.assertTrue(_wait_for(lambda: not _named(session, BOOTSTRAP), 15),
                        "temporary bootstrap window left behind: %r" % _named(session, BOOTSTRAP))

    def bootstrap_logs(self, session):
        return list((self.home / "cache" / "hermes-tmux").glob("bootstrap-*.log"))

    def assert_clean_workspace(self, session, expect_shell=True):
        names = [w[1] for w in _windows(session)]
        self.assertEqual(1, names.count(AGENTS), "duplicate agents window")
        self.assertEqual(1, names.count(MAIL), "duplicate hx:mail window")
        self.assert_no_bootstrap_window(session)
        self.assertEqual(expect_shell, "zsh" in names or any(
            w[1] not in (AGENTS, MAIL) for w in _windows(session)))

    # ---------------- tests ----------------

    def test_01_fresh_session_creation_builds_workspace(self):
        rc, out = self.run_hook()
        self.assertEqual(0, rc, out)
        self.assertIn("attach intercepted", out)
        self.assertIn("main", _sessions())
        self.assertTrue(self.build_ready(), "workspace incomplete after fresh create")
        self.assert_clean_workspace("main")
        # The shell pane the new session was created with survives.
        self.assertEqual(3, len(_windows("main")))

    def test_02_reattach_healthy_is_a_no_op(self):
        self.run_hook()
        self.assertTrue(self.build_ready())
        key = _named("main", AGENTS)[0][3]
        pid = _coordinator_pid(key, self.home)
        windows = _windows("main")
        logs_before = [p.stat().st_mtime for p in self.bootstrap_logs("main")]
        rc, out = self.run_hook()
        self.assertEqual(0, rc, out)
        time.sleep(1.0)     # a bootstrap, if wrongly started, would have run by now
        self.assertEqual(windows, _windows("main"), "windows changed on a healthy attach")
        self.assertEqual(pid, _coordinator_pid(key, self.home), "coordinator restarted")
        self.assertEqual(logs_before, [p.stat().st_mtime for p in self.bootstrap_logs("main")],
                         "workspace re-initialized although it was healthy")

    def test_03_missing_agents_is_rebuilt(self):
        self.run_hook()
        self.assertTrue(self.build_ready())
        key = _named("main", AGENTS)[0][3]
        _tmux("kill-window", "-t", _named("main", AGENTS)[0][0])
        self.assertEqual([], _named("main", AGENTS))
        rc, out = self.run_hook()
        self.assertEqual(0, rc, out)
        self.assertTrue(self.build_ready(), "agents not restored")
        self.assert_clean_workspace("main")
        self.assertEqual(1, len(_named("main", MAIL)), "mail duplicated while repairing agents")

    def test_04_missing_mail_is_rebuilt_without_touching_agents(self):
        self.run_hook()
        self.assertTrue(self.build_ready())
        agents = _named("main", AGENTS)[0]
        key = agents[3]
        pid = _coordinator_pid(key, self.home)
        _tmux("kill-window", "-t", _named("main", MAIL)[0][0])
        rc, out = self.run_hook()
        self.assertEqual(0, rc, out)
        self.assertTrue(self.build_ready(), "mail not restored")
        self.assertEqual(agents, _named("main", AGENTS)[0], "agents window replaced")
        self.assertEqual(pid, _coordinator_pid(key, self.home),
                         "coordinator restarted for a missing mail window")
        self.assert_clean_workspace("main")

    def test_05_partial_workspace_with_dead_coordinator(self):
        self.run_hook()
        self.assertTrue(self.build_ready())
        agents = _named("main", AGENTS)[0]
        old_pid = _coordinator_pid(agents[3], self.home)
        os.kill(old_pid, signal.SIGTERM)
        _tmux("kill-window", "-t", _named("main", MAIL)[0][0])
        self.assertTrue(_wait_for(lambda: not _alive(old_pid), 10),
                        "coordinator did not stop")
        rc, out = self.run_hook()
        self.assertEqual(0, rc, out)
        self.assertTrue(self.build_ready(), "workspace not repaired")
        new_pid = _coordinator_pid(_named("main", AGENTS)[0][3], self.home)
        self.assertNotEqual(old_pid, new_pid, "dead coordinator not replaced")
        self.assert_clean_workspace("main")

    def test_06_repeated_attaches_are_stable(self):
        self.run_hook()
        self.assertTrue(self.build_ready())
        key = _named("main", AGENTS)[0][3]
        pid = _coordinator_pid(key, self.home)
        windows = _windows("main")
        for _ in range(3):
            rc, out = self.run_hook()
            self.assertEqual(0, rc, out)
        time.sleep(1.0)
        self.assertEqual(windows, _windows("main"))
        self.assertEqual(pid, _coordinator_pid(key, self.home))
        procs = _processes_for(key)
        self.assertEqual(2, len(procs), "expected exactly one watch + one follow: %r" % procs)

    def test_07_only_the_selected_session_is_bootstrapped(self):
        self.run_hook()
        self.assertTrue(self.build_ready())
        _tmux("new-session", "-d", "-s", "main-2", "-c", str(self.tmp))
        before = _windows("main")
        rc, out = self.run_hook(reply="2\n")
        self.assertEqual(0, rc, out)
        self.assertTrue(self.build_ready("main-2"), "second session not bootstrapped")
        self.assertEqual([], _named("main", BOOTSTRAP))
        # main keeps every window it had: same ids, same key, same coordinator.
        self.assertEqual(before, _windows("main"), "unrelated session was modified")

    def test_08_explicit_opt_out_builds_nothing(self):
        rc, out = self.run_hook(HERMES_TMUX="0")
        self.assertEqual(0, rc, out)
        self.assertIn("main", _sessions())
        time.sleep(1.5)
        names = [w[1] for w in _windows("main")]
        self.assertNotIn(AGENTS, names)
        self.assertNotIn(MAIL, names)
        self.assertEqual([], self.bootstrap_logs("main"))

    def test_09_server_restart_rebuilds_from_scratch(self):
        self.run_hook()
        self.assertTrue(self.build_ready())
        old_key = _named("main", AGENTS)[0][3]
        stale = self.home / "cache" / "hermes-tmux" / "sessions" / old_key
        self.assertTrue(stale.is_dir())
        _tmux("kill-server")
        self.assertTrue(_wait_for(lambda: not _sessions(), 10), "server did not stop")
        # The coordinator checks its server on its own idle cadence (up to
        # watch._SESSION_CHECK_S = 20 s), so a few ticks are expected, not a hang.
        self.assertTrue(_wait_for(lambda: not _alive(_coordinator_pid(old_key, self.home)), 60),
                        "coordinator outlived its server")
        rc, out = self.run_hook()
        self.assertEqual(0, rc, out)
        self.assertTrue(self.build_ready(), "workspace not rebuilt after restart")
        new_key = _named("main", AGENTS)[0][3]
        self.assertNotEqual(old_key, new_key, "new server reused the dead server's key")
        self.assert_clean_workspace("main")

    def test_10_no_focus_or_current_window_change(self):
        _tmux("new-session", "-d", "-s", "main", "-c", str(self.tmp))
        _tmux("new-window", "-t", "=main:", "-c", str(self.tmp))
        before_current = _current_window("main")
        self.assertTrue(before_current)
        rc, out = self.run_hook()
        self.assertEqual(0, rc, out)
        self.assertTrue(self.build_ready())
        self.assertEqual(before_current, _current_window("main"),
                         "the bootstrap moved the user's window selection")
        rc, out = _tmux("display-message", "-p", "-t", "=%s:" % "main", "#{window_active}")
        self.assertEqual("1", out.strip())

    def test_11_bootstrap_never_targets_the_default_socket(self):
        # TMUX_TMPDIR is private, so a dropped -L would land here as `default`;
        # the only session that may exist is the one on the named socket.
        self.run_hook()
        self.assertTrue(self.build_ready())
        proc = subprocess.run(
            [_TMUX, "-f", CONFIG, "list-sessions"],
            capture_output=True, text=True,
            env={"TMUX_TMPDIR": str(self.tmux_tmpdir), "PATH": "/usr/bin:/bin"})
        self.assertNotEqual(0, proc.returncode,
                            "something ran on the default socket besides the named one")

    def test_12_dry_run_reports_and_does_nothing(self):
        rc, out = self.run_hook(args=("--dry-run",))
        self.assertEqual(0, rc, out)
        self.assertIn("new session: main", out)
        self.assertEqual([], _sessions())
        self.assertEqual([], self.bootstrap_logs("main"))


    def test_13_profile_stamped_on_the_window_wins_over_the_shell(self):
        # A server started from a shell with HERMES_HOME=B, then a terminal with
        # no HERMES_HOME at all: the key and the state directory live under B, and
        # the overview window says so in `@hx_home`. Probing $HOME/.hermes instead
        # would rebuild on every attach.
        profile_b = self.tmp / "profileB"
        profile_b.mkdir()
        self.run_hook(HERMES_HOME=str(profile_b))
        self.assertTrue(self.build_ready(home=profile_b))
        key = _named("main", AGENTS)[0][3]
        self.assertTrue((profile_b / "cache" / "hermes-tmux" / "sessions" / key).is_dir(),
                        "workspace not built under the profile the pane was given")
        windows = _windows("main")
        pid = _coordinator_pid(key, profile_b)
        rc, out = self.run_hook(HERMES_HOME=None)      # shell default: $HOME/.hermes
        self.assertEqual(0, rc, out)
        time.sleep(1.0)
        self.assertEqual(windows, _windows("main"), "re-bootstrapped under the wrong profile")
        self.assertEqual(pid, _coordinator_pid(key, profile_b))
        self.assertFalse((self.tmp / ".hermes" / "cache" / "hermes-tmux").exists(),
                         "the hook wrote state under a profile the workspace is not in")

    def test_14_renamed_permanent_tabs_still_count_as_healthy(self):
        # `free_window_name` appends `#N` when a foreign window already holds the
        # name. Such a workspace is healthy; a hook that insisted on the bare name
        # would rebuild it on every attach.
        self.run_hook()
        self.assertTrue(self.build_ready())
        mail = _named("main", MAIL)[0][0]
        agents = _named("main", AGENTS)[0][0]
        _tmux("rename-window", "-t", mail, "hx:mail#2")
        _tmux("rename-window", "-t", agents, "agents#2")
        key = _named("main", "agents#2")[0][3]
        pid = _coordinator_pid(key, self.home)
        windows = _windows("main")
        logs_before = [p.stat().st_mtime for p in self.bootstrap_logs("main")]
        rc, out = self.run_hook()
        self.assertEqual(0, rc, out)
        time.sleep(1.0)
        self.assertEqual(windows, _windows("main"), "renamed tabs treated as missing")
        self.assertEqual(pid, _coordinator_pid(key, self.home))
        self.assertEqual(logs_before, [p.stat().st_mtime for p in self.bootstrap_logs("main")],
                         "re-bootstrapped although both renamed tabs were present")

    def test_15_falsey_opt_out_spellings(self):
        # The tool's gate accepts 0/false/no/off/disable/disabled; the hook must
        # agree, or it builds a workspace the user asked not to have.
        for spelling in ("false", "off", "disabled"):
            with self.subTest(spelling=spelling):
                _tmux("kill-server")
                _wait_for(lambda: not _sessions(), 10)
                rc, out = self.run_hook(HERMES_TMUX=spelling)
                self.assertEqual(0, rc, out)
                self.assertIn("main", _sessions())
                time.sleep(1.0)
                names = [w[1] for w in _windows("main")]
                self.assertNotIn(AGENTS, names, "workspace built despite HERMES_TMUX=%s" % spelling)
                self.assertEqual([], self.bootstrap_logs("main"))

    def test_16_dead_bootstrap_window_does_not_block_repair(self):
        # `remain-on-exit on` turns a finished bootstrap into a dead window. It must
        # be cleared and the repair must still happen, not be skipped forever.
        _tmux("new-session", "-d", "-s", "main", "-c", str(self.tmp))
        _tmux("set-option", "-g", "remain-on-exit", "on")
        try:
            _tmux("new-window", "-d", "-t", "=main:", "-n", BOOTSTRAP, "--",
                  "/bin/sh", "-c", "exit 0")
            dead = _wait_for(lambda: any(
                line.endswith("|1") for line in
                _tmux("list-panes", "-s", "-t", "=main",
                      "-F", "#{window_name}|#{pane_dead}")[1].splitlines()
                if line.startswith(BOOTSTRAP)), 10)
            self.assertTrue(dead, "fixture: no dead bootstrap pane")
            rc, out = self.run_hook()
            self.assertEqual(0, rc, out)
            self.assertTrue(self.build_ready(), "a dead bootstrap window blocked repair")
            self.assert_no_bootstrap_window("main")
        finally:
            _tmux("set-option", "-g", "remain-on-exit", "off")

    def test_17_hung_bootstrap_pane_does_not_double_start(self):
        # A live bootstrap pane means one is in flight: a second terminal must not
        # create a second window or a second `up`.
        _tmux("new-session", "-d", "-s", "main", "-c", str(self.tmp))
        _tmux("new-window", "-d", "-t", "=main:", "-n", BOOTSTRAP, "--",
              "/bin/sh", "-c", "sleep 30")
        before = _windows("main")
        rc, out = self.run_hook()
        self.assertEqual(0, rc, out)
        self.assertEqual(before, _windows("main"), "a second bootstrap window was created")

def tearDownModule():
    if SKIP:
        return
    _tmux("kill-server")
    _SOCKET_DIR["dir"] = None


if SKIP:  # pragma: no cover - tmux is a hard requirement of this file
    raise unittest.SkipTest("tmux not available")
