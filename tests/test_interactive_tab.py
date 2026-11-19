"""Exercise the optional key binding through a real client on a private socket.

No mocks: a tmux client is attached on a pty, ``scripts/hermes-tab.conf`` is
sourced into the sandbox's private server, and the bytes ``C`` are typed at the
client. The binding is ``C`` -> ``new-window -c "#{pane_current_path}" -n Hermes
"exec hermes"``: uppercase, so lowercase ``c`` keeps the reader's own meaning
(the test rebinds ``c`` explicitly rather than assuming a default).

A fake ``hermes`` on PATH is the probe, not a stand-in for the product: it
prints the cwd, HERMES_HOME, a marker variable, argc and TMUX_PANE, which is
how the test can tell a tab that inherited the SESSION environment from one
that merely started. ``update-environment`` is cleared and the marker is set
with ``set-environment`` on the server, so ``env=tmux session`` proves the new
window forked from the server, not from the pane that pressed the key.

What is pinned: each press opens an independent tab in the directory the reader
was in, that tab is NOT ours (no ``@hx`` on window or pane, no close mark, so
``down`` must leave it standing), and closing one tab with ``/exit`` does not
touch the other.
"""
from __future__ import annotations

import os
import shlex
import unittest

from tests import test_tabs as T
from tests.test_close_mark import Client


def tearDownModule():
    """The private server and temp homes belong to ``test_tabs``.

    Reusing its teardown keeps one cleanup rule for both modules: only the
    pid-scoped socket is killed, and every temp home is removed.
    """
    T.tearDownModule()


@unittest.skipIf(T.SKIP, "tmux not installed")
class InteractiveTabTest(unittest.TestCase):
    """One real tmux client on a pty, driving the uppercase ``C`` binding."""
    def tmux(self, *args):
        """Run a tmux command and require success.

        A binding that failed to load would otherwise surface much later as an
        assertion about a window that never appeared.
        """
        rc, out = T._tmux(*args)
        self.assertEqual(rc, 0, (args, out))
        return out

    def test_key_opens_two_independent_user_tabs(self):
        """``C`` opens a tab that belongs to the reader, in the directory they were in.

        The binding comes from ``scripts/hermes-tab.conf`` (``C`` -> ``new-window -c
        #{pane_current_path} -n Hermes "exec hermes"``); lower-case ``c`` is rebound in
        the test so it stays an ordinary new-window. The launcher on PATH prints the
        cwd, HERMES_HOME, a marker variable, argc and TMUX_PANE, which is what makes
        the environment claim checkable: the tab must see the session's
        ``HX_TAB_MARKER``
        ("tmux session") rather than the pane-local export, because
        ``update-environment`` is cleared. Each press must land in the directory the
        reader had cd'd to.

        Ownership is the negative control: neither the window nor the pane may carry
        ``@hx`` and the tab must not get the close mark, so ``down`` later must leave
        both user tabs standing while it removes the workspace. Finally ``/exit`` in
        the first tab closes only that tab: the second still runs and still receives
        keystrokes, proving they were independent shells and not two views of one.
        """
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
