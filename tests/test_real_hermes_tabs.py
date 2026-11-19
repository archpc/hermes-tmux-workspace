"""Real-Hermes regression for the optional Ctrl-b Shift-C interactive tab.

Complements tests/test_interactive_tab.py (fake launcher, full worker/tab
contract): this module drives the REAL ``hermes`` CLI found on PATH through
the same binding on a private tmux server, and proves the two properties a
fake launcher cannot:

* each Shift-C tab starts an independent conversation (distinct Session ids);
* each tab's lifecycle is its own: ``/exit`` closes only that window, the
  other Hermes tab and the invoking pane survive.

Offline by construction: the isolated HERMES_HOME config pins a dead
endpoint (127.0.0.1:9) and disables telemetry, no prompt is ever sent, and
the gateway runs with HERMES_GATEWAY_NO_SUPERVISE=1. No billable inference.

The tab launcher execs the installed runtime's interpreter directly into
``hermes_cli.main`` (paths resolved from the ``hermes`` launcher on PATH,
never hardcoded), and HERMES_DISABLE_LAZY_INSTALLS=1 skips the CLI's
per-data-root dependency sync: a disposable HERMES_HOME has no committed
dependency environment and building one needs the network. The venv
interpreter already carries the dependencies, so the CLI boots directly.
The CLI code under test is the real one; only the bootstrap is bypassed.

Isolation is the shared tests/test_tabs.py sandbox: private tmux socket,
disposable HERMES_HOME, never the user's server or state. Hermes tabs are
user-owned (no ``@hx``), which is the mechanism that keeps ``hermes-tmux
down`` and ``close`` away from them; the down/close behavior itself is
covered by test_interactive_tab.py.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from tests import test_tabs as T
from tests.test_close_mark import Client


def _resolve_runtime():
    """(venv python, repo root) of the installed Hermes, or None.

    Reads the absolute paths embedded in the ``hermes`` launcher script and
    keeps the one that is a Hermes checkout with a usable venv.

    The launcher script is the only discovery point: nothing here hardcodes an
    install location, so the test works wherever the CLI was installed and
    skips where it was not. The check is deliberately strict (``hermes_cli``
    package *and* a ``.venv/bin/python``), because the test execs that
    interpreter directly and depends on it already carrying the dependencies.
    """
    launcher = shutil.which("hermes")
    if not launcher:
        return None
    try:
        text = Path(launcher).read_text(encoding="utf-8")
    except OSError:
        return None
    for candidate in dict.fromkeys(re.findall(r"'(/[^']+)'", text)):
        root = Path(candidate)
        python = root / ".venv" / "bin" / "python"
        if (root / "hermes_cli" / "main.py").is_file() and python.exists():
            return python, root
    return None


# Resolved once at import: no tmux at all skips on the sandbox, a missing
# runtime skips this module. Both skips are reported separately, because they
# are different facts about the machine.
RUNTIME = None if T.SKIP else _resolve_runtime()
SKIP_REASON = "tmux not installed" if T.SKIP else (
    "no usable Hermes runtime resolved from PATH" if RUNTIME is None else None)


def tearDownModule():
    # Imported modules do not get their teardown run by the test runner, so the
    # sandbox module's cleanup is called explicitly here.
    T.tearDownModule()


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class RealHermesTabsTest(unittest.TestCase):
    """Drives the real CLI; skips (rather than fails) without a runtime.

    What this adds over the fake-launcher module is only what a fake cannot
    show: two Shift-C tabs really are two Hermes sessions, and ``/exit`` in one
    really closes one window. The workspace's own bookkeeping is checked only as
    a negative — this module never invokes ``down``/``close``, it asserts these
    tabs carry no ``@hx`` marker — and is covered positively elsewhere.
    """

    def test_shift_c_tabs_are_independent_real_sessions(self):
        # The assertion is for the type checker's benefit; the class skip has
        # already established it at runtime.
        assert RUNTIME is not None  # narrowed by the class skip
        python, root = RUNTIME
        with T.Sandbox() as box:
            # A launcher that execs the resolved interpreter straight into
            # ``hermes_cli.main``. -B and PYTHONDONTWRITEBYTECODE keep the
            # disposable home free of bytecode.
            launcher = box.home / "hermes"
            launcher.write_text(
                "#!/bin/sh\n"
                f"exec {python} -B -c "
                f"'import sys; sys.path.insert(0, \"{root}\"); "
                "from hermes_cli.main import main; sys.exit(main())' \"$@\"\n",
                encoding="utf-8")
            launcher.chmod(0o700)
            # Offline by construction: a dead endpoint (127.0.0.1:9, where
            # nothing listens), no telemetry, and a CLI interface. No model
            # prompt is ever sent, so no request should leave the machine — and
            # the pin means an accidental one cannot reach a provider.
            (box.home / "config.yaml").write_text(
                'model:\n  default: offline-smoke\n  provider: custom\n'
                '  base_url: http://127.0.0.1:9/v1\n'
                'display:\n  interface: cli\n'
                'telemetry:\n  shared_metrics:\n    enabled: false\n'
                '    send: false\n    offer_version: 2\n', encoding="utf-8")
            # Server-side environment for every pane: the key binding fragment,
            # the disposable home, no lazy installs, and PATH with the stub
            # launcher first so ``hermes`` inside the pane is that script.
            for args in (
                ("source-file", str(T.REPO / "scripts" / "hermes-tab.conf")),
                ("set-option", "-gw", "remain-on-exit", "failed"),
                ("set-option", "-t", box.name, "default-shell", "/bin/sh"),
                ("set-option", "-t", box.name, "update-environment", ""),
                ("set-environment", "-t", box.name, "HERMES_HOME", str(box.home)),
                ("set-environment", "-t", box.name,
                 "HERMES_DISABLE_LAZY_INSTALLS", "1"),
                ("set-environment", "-t", box.name, "PATH",
                 str(box.home) + ":" + os.environ["PATH"]),
            ):
                rc, out = T._tmux(*args)
                self.assertEqual(rc, 0, out)
            client = Client(box.name, box.columns, box.rows)
            env = dict(os.environ, HERMES_HOME=str(box.home),
                       HERMES_GATEWAY_NO_SUPERVISE="1",
                       HERMES_DISABLE_LAZY_INSTALLS="1",
                       PYTHONDONTWRITEBYTECODE="1")
            # The gateway runs as an unsupervised child process, logging to a
            # file the assertions can quote when a wait times out.
            log = box.home / "smoke-gateway.log"
            with log.open("w") as output:
                gateway = subprocess.Popen(
                    [str(launcher), "gateway", "run"],
                    stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                    env=env)
            try:
                # Await the socket so client auto-start cannot race the
                # foreground gateway.
                self.assertTrue(T.wait_for(
                    lambda: (box.home / "gateway.sock").is_socket()
                    or (box.home / "gateway.sock.path").exists(), timeout=22),
                    log.read_text())
                self.assertTrue(T.wait_for(
                    lambda: bool(T._tmux("list-clients", "-t", box.name)[1])))
                windows, sessions = [], []
                for _ in range(2):
                    previous = T._format(box.name, "#{window_id}")
                    os.write(client.master, b"\x02C")
                    self.assertTrue(T.wait_for(
                        lambda: T._format(box.name, "#{window_id}") != previous))
                    window = T._format(box.name, "#{window_id}")
                    self.assertEqual(T._window_name(window), "Hermes")
                    pane = T._format(box.name, "#{pane_id}")
                    # "Session: <id>" is printed by the real CLI; waiting for
                    # it rather than sleeping is what makes the timing robust
                    # on a slow machine.
                    pattern = r"Session:\s+([\w-]+)"
                    self.assertTrue(T.wait_for(
                        lambda: bool(re.search(pattern, box.capture(pane))),
                        timeout=25), (box.capture(pane), log.read_text()[-4000:]))
                    sessions.append(re.findall(pattern, box.capture(pane))[0])
                    windows.append(window)
                    # User-owned: no workspace marker on pane or window, so
                    # down/close can never claim these tabs.
                    self.assertEqual(T._option("-p", pane, "@hx"), "")
                    self.assertEqual(T._option("-w", window, "@hx"), "")
                self.assertNotEqual(*sessions)

                # Lowercase c keeps its stock shell binding (the fragment
                # only binds C). Press it from the original shell pane.
                rc, out = T._tmux("select-window", "-t", T._pane_window(box.pane_id))
                self.assertEqual(rc, 0, out)
                origin = T._format(box.name, "#{window_id}")
                os.write(client.master, b"\x02c")
                self.assertTrue(T.wait_for(
                    lambda: T._format(box.name, "#{window_id}") != origin))
                shell = T._format(box.name, "#{window_id}")
                self.assertNotEqual(T._window_name(shell), "Hermes")
                self.assertTrue(T.wait_for(
                    lambda: T._format(shell, "#{pane_current_command}")
                    in ("sh", "bash", "zsh")),
                    T._format(shell, "#{pane_current_command}"))

                # /exit closes the window it was typed in and nothing else: the
                # sibling Hermes tab, the new shell window and the invoking pane
                # all survive.
                for index in (0, 1):
                    rc, out = T._tmux("select-window", "-t", windows[index])
                    self.assertEqual(rc, 0, out)
                    os.write(client.master, b"/exit\r")
                    self.assertTrue(T.wait_for(
                        lambda: not T._window_exists(windows[index])),
                        box.capture(windows[index]))
                    if index == 0:
                        self.assertTrue(T._window_exists(windows[1]))
                self.assertTrue(T._window_exists(shell))
                self.assertTrue(T._pane_exists(box.pane_id))
            finally:
                # The gateway is a real child process: terminate, then escalate
                # to kill so a stuck process cannot outlive the sandbox.
                client.close()
                gateway.terminate()
                try:
                    gateway.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    gateway.kill()
                    gateway.wait(timeout=5)
