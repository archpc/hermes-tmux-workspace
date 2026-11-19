"""Exercise the optional binding through a real client on a private socket."""
from __future__ import annotations

import os
import shlex
import unittest

from tests import test_tabs as T
from tests.test_close_mark import Client


def tearDownModule():
    T.tearDownModule()


@unittest.skipIf(T.SKIP, "tmux not installed")
class InteractiveTabTest(unittest.TestCase):
    def tmux(self, *args):
        rc, out = T._tmux(*args)
        self.assertEqual(rc, 0, (args, out))
        return out

    def test_key_opens_two_independent_user_tabs(self):
        with T.Sandbox() as box:
            fragment = T.REPO / "scripts" / "hermes-tab.conf"
            self.tmux("bind-key", "c", "new-window", "-c", "#{pane_current_path}")
            self.tmux("source-file", str(fragment))
            self.addCleanup(T._tmux, "unbind-key", "C")
            self.addCleanup(T._tmux, "bind-key", "c", "new-window")
            self.tmux("set-option", "-t", box.name, "default-shell", "/bin/sh")
            self.tmux("set-option", "-t", box.name, "update-environment", "")
            self.tmux("set-environment", "-t", box.name, "PATH", f"{box.home}:{os.environ['PATH']}")
            self.tmux("set-environment", "-t", box.name, "HERMES_HOME", str(box.home))
            self.tmux("set-environment", "-t", box.name, "HX_TAB_MARKER", "tmux session")
            launcher = box.home / "hermes"
            launcher.write_text(
                '#!/bin/sh\n'
                'printf "hermes cwd=%s home=%s env=%s argc=%s pane=%s\\n" '
                '"$PWD" "$HERMES_HOME" "$HX_TAB_MARKER" "$#" "$TMUX_PANE"\n'
                'while IFS= read -r line; do\n'
                '  [ "$line" = /exit ] && exit 0\n'
                '  printf "received:%s\\n" "$line"\n'
                'done\n', encoding="utf-8")
            launcher.chmod(0o700)
            box.export("HX_TAB_MARKER", "pane-local")
            self.assertEqual(box.cli("up"), 0, box.capture())
            client = Client(box.name, box.columns, box.rows)
            try:
                self.assertTrue(T.wait_for(lambda: bool(self.tmux("list-clients", "-t", box.name))))
                tabs = []
                start = T._pane_window(box.pane_id)
                for name in ("first project", "second project"):
                    cwd = box.home / name
                    cwd.mkdir()
                    cwd = cwd.resolve()
                    self.tmux("select-window", "-t", start)
                    self.assertEqual(box.run(f"cd {shlex.quote(str(cwd))}"), 0)
                    os.write(client.master, b"\x02C")
                    self.assertTrue(T.wait_for(lambda: T._format(box.name, "#{window_id}") != start))
                    window = T._format(box.name, "#{window_id}")
                    pane = T._format(box.name, "#{pane_id}")
                    self.assertEqual(T._window_name(window), "Hermes")
                    expected = f"hermes cwd={cwd} home={box.home} env=tmux session argc=0 pane={pane}"
                    self.assertTrue(T.wait_for(lambda: expected in self.tmux(
                        "capture-pane", "-pJ", "-t", pane)), box.capture(pane))
                    self.assertEqual(T._option("-p", pane, "@hx"), "")
                    self.assertEqual(T._option("-w", window, "@hx"), "")
                    self.assertNotIn("hxc@", T._option("-w", window, "window-status-current-format"))
                    tabs.append((window, pane))
                self.assertNotEqual(tabs[0], tabs[1])
                box.add_delegation("deleg_tab010001", [{"goal": "Worker control"}])
                self.assertTrue(T.wait_for(lambda: bool(box.tabs())))
                self.assertEqual(set(box.tabs()), {"deleg_tab010001#0"})

                os.write(client.master, b"\x02c")
                self.assertTrue(T.wait_for(lambda: T._format(box.name, "#{window_id}") != tabs[1][0]))
                shell = T._format(box.name, "#{window_id}")
                self.assertTrue(T.wait_for(lambda: T._format(shell, "#{pane_current_path}") == str(cwd)))
                os.write(client.master, b"printf '%s%s\\n' 'shell-' 'ready'\r")
                self.assertTrue(T.wait_for(lambda: "shell-ready" in box.capture(shell)))
                self.assertEqual(box.cli("down"), 0, box.capture())
                self.assertTrue(all(T._window_exists(w) for w, _ in tabs))

                first, second = tabs
                self.tmux("select-window", "-t", first[0])
                os.write(client.master, b"/exit\r")
                self.assertTrue(T.wait_for(lambda: not T._window_exists(first[0])))
                self.assertTrue(T._window_exists(shell))
                self.tmux("select-window", "-t", second[0])
                os.write(client.master, b"still here\r")
                self.assertTrue(T.wait_for(lambda: "received:still here" in box.capture(second[1])))
            finally:
                client.close()
