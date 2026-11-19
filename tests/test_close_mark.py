"""The clickable close mark on worker tabs (``hermes_tmux/closemark.py``).

Contract under test:

* ``up`` wraps the server's ``MouseDown1Status`` binding and keeps the previous
  binding as the else-branch; ``close-mark off`` and the last ``down`` put the
  previous binding back exactly;
* only a worker tab carries the mark, only in its current-window format, and
  the mark names that window; the reader's windows and the overview never do;
* a click on the mark, through a real client, closes that tab; a click on a
  tab's name still selects it;
* closing is closing the VIEW: the worker's transcript and manifest are not
  touched, the coordinator does not reopen the tab, ``open`` brings it back;
* a close aimed at anything that is not a worker tab is refused and closes
  nothing; a pane the reader split into a worker tab survives;
* ``HERMES_TMUX_CLOSE_MARK=0`` leaves the binding and the tabs alone.

The harness (private tmux server, private HERMES_HOME, fixtures) is the one in
``tests/test_tabs.py``. It is imported, and this module calls that module's
``tearDownModule`` itself, because an imported module's teardown does not run.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import struct
import subprocess
import termios
import threading
import time
import unittest

from hermes_tmux import closemark
from tests import test_tabs as T

_tmux = T._tmux
wait_for = T.wait_for
Sandbox = T.Sandbox


def tearDownModule() -> None:
    T.tearDownModule()


class FormatTest(unittest.TestCase):
    """The pure half: what the mark looks like and which strings it accepts.

    No tmux here. ``with_mark``/``without_mark`` are checked for round-trip
    identity, because the mark is written into a window option that the reader
    later reads back and unmarks; any asymmetry would accumulate across an
    ``on``/``off``/``on`` cycle.
    """

    FMT = " #I:#{E:@hx_glyph} #W#{?window_flags,#F,} "

    def test_mark_is_appended_once_and_names_the_window(self):
        # Three claims: the mark names the window id in its range argument, the
        # window's own range is restored right after it (or the padding cell
        # stops selecting the tab), and applying it twice is a no-op.
        marked = closemark.with_mark(self.FMT, "@12")
        self.assertIn("#[range=user|hxc@12 ", marked)
        self.assertTrue(marked.endswith("#[range=window|#{window_index}] "),
                        "the window's own range is not restored after the mark: %r" % marked)
        self.assertEqual(closemark.with_mark(marked, "@12"), marked)
        self.assertEqual(closemark.without_mark(marked), self.FMT)

    def test_range_argument_fits_tmux_limit(self):
        # tmux caps a range argument at 15 bytes. "hxc@" + 8 digits is 12, so
        # the widest window id still fits; the test asserts the margin rather
        # than trusting the arithmetic.
        marked = closemark.with_mark(self.FMT, "@99999999")
        found = re.search(r"range=user\|(\S+)", marked)
        arg = found.group(1) if found else ""
        self.assertLessEqual(len(arg.encode()), 15, "tmux allows 15 bytes: %r" % arg)

    def test_ascii_mode_uses_a_plain_letter(self):
        # "×" is one cell everywhere, but a non-UTF-8 locale renders it as
        # mojibake; the fallback is a plain "x".
        self.assertIn("]x#[", closemark.with_mark(self.FMT, "@1", ascii_mode=True))

    def test_only_window_ids_are_targets(self):
        # The targeted strings are exactly the two forms a click can produce.
        # Everything else — a bare index, a pane id, a session, a window id
        # with a trailing character, and a command injection — is refused, and
        # a refused target leaves the format untouched.
        self.assertEqual(closemark.parse_target("hxc@7"), "@7")
        self.assertEqual(closemark.parse_target("@7"), "@7")
        for bad in ("", "7", "%7", "hxc@7x", "@7; kill-server", "hxc", "main:1"):
            self.assertIsNone(closemark.parse_target(bad), bad)
        self.assertEqual(closemark.with_mark(self.FMT, "main:1"), self.FMT)

    def test_opt_out(self):
        # Only the five false-ish spellings ('0', 'false', 'no', 'off', 'OFF')
        # disable it; anything else, including empty, means on.
        self.assertTrue(closemark.enabled({}))
        for off in ("0", "false", "no", "off", "OFF"):
            self.assertFalse(closemark.enabled({"HERMES_TMUX_CLOSE_MARK": off}))


def _binding() -> str:
    """The server's current root ``MouseDown1Status`` binding, as one line."""
    return _tmux("list-keys", "-T", "root", closemark.KEY)[1].strip()


def _own(window_id: str, option: str) -> str:
    """A window's *own* option value; '' when it only inherits the global one.

    ``-w`` is what makes this an ownership test: a user window and the overview
    inherit their format and must read as empty; ``-v`` only prints the value.
    """
    rc, out = _tmux("show-options", "-wv", "-t", window_id, option)
    return out if rc == 0 else ""


def _plain(text: str) -> str:
    """Strip tmux style runs so a drawn status line can be searched by text."""
    return re.sub(r"#\[[^\]]*\]", "", text)


class Client:
    """A real tmux client on a pty, so a mouse click takes the real path.

    Mouse events only reach tmux through a terminal, so the test allocates a
    pty, sets its size, attaches a client to the sandbox session and writes
    SGR mouse reports into the master side. The ``TMUX``/``TMUX_PANE``
    variables are removed from the child's environment: the client must attach
    to the named session, not to whatever server the test runner is inside.

    Output is drained on a thread; nobody asserts on it, but a full pty buffer
    would block the client.
    """

    def __init__(self, session: str, columns: int, rows: int) -> None:
        self.rows = rows
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env["TERM"] = "xterm-256color"
        self.proc = subprocess.Popen(
            [T.TMUX, "-L", T.SOCKET, "-f", T.CONFIG, "attach-session", "-t", session],
            stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        try:
            while os.read(self.master, 65536):
                pass
        except OSError:
            pass

    def click(self, column: int) -> None:
        """Left press and release on the status line at a 0-based column.

        tmux acts on the press; the release is sent too so the client does not
        leave a button held. SGR coordinates are 1-based.
        """
        for final in ("M", "m"):
            os.write(self.master, ("\x1b[<0;%d;%d%s" % (column + 1, self.rows, final)).encode())

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        try:
            os.close(self.master)
        except OSError:
            pass


def _status_text(box) -> str:
    """The status line as drawn, left to right, without style runs.

    Rebuilt rather than screenshotted: the left segment is truncated to
    ``status-left-length`` (as tmux draws it) and each window's text is expanded
    through the format it would currently use — current format for the window in
    view, ordinary format otherwise. That is what makes ``text.index("click me")``
    a column the click can be aimed at.
    """
    rc, out = _tmux("display-message", "-p", "-t", box.name, "#{E:status-left}")
    rc, limit = _tmux("show-options", "-gv", "status-left-length")
    text = _plain(out)[:int(limit or 10)]
    rc, sep = _tmux("show-options", "-gwv", "window-status-separator")
    current = T._format(box.name, "#{window_id}")
    parts = []
    for win in box.windows():
        option = "window-status-current-format" if win["id"] == current else "window-status-format"
        parts.append(_plain(T._format(win["id"], "#{E:%s}" % option)))
    return text + (sep or " ").join(parts)


@unittest.skipIf(T.SKIP, "tmux not installed")
class CloseMarkTest(unittest.TestCase):
    """The mark and the click, against a private tmux server.

    Every test builds its own ``Sandbox`` (private socket, private HERMES_HOME,
    fixtures) and asserts both directions: that the click/mark happened where
    it should, and that everything else — the reader's pane, the overview, the
    other windows, the worker's own files — was left alone.

    Timing: tab creation and window disappearance are awaited with
    ``wait_for`` rather than slept on; the one bounded poll is the click walk
    below (0.6 s per column) because a click that misses its target is not an
    event at all.
    """

    maxDiff = None

    def setUp(self):
        # The previous binding these tests expect to get back. ``mouse off`` is
        # restored by cleanup, so a failing test does not leave the server with
        # mouse reporting enabled.
        _tmux("start-server")
        self.addCleanup(_tmux, "set-option", "-g", "mouse", "off")

    def _up(self, box):
        rc = box.cli("up")
        self.assertEqual(rc, 0, "up exited %s: %r" % (rc, box.capture()[-600:]))

    def _one_tab(self, box, deleg="deleg_mark0001", goal="mark me"):
        """Create one delegation, run ``up``, and wait for its tab and pane."""
        box.add_delegation(deleg, [{"goal": goal}])
        self._up(box)
        ok = wait_for(lambda: len(box.tabs()) == 1
                      and T._pane_exists(list(box.tabs().values())[0].get("pane") or ""), timeout=20)
        self.assertTrue(ok, "no worker tab appeared: %r" % box.tabs())
        wkey = "%s#0" % deleg
        return wkey, box.tabs()[wkey]

    def test_up_marks_worker_tabs_only_and_keeps_the_previous_binding(self):
        # The binding is a wrapper: ours wins for ``hxc@*`` ranges and the
        # previous command is kept as the else-branch. The previous command is
        # captured before ``up`` (which may be empty on a fresh server, hence
        # the conditional assertion).
        with Sandbox() as box:
            before = _binding()
            wkey, tab = self._one_tab(box)
            after = _binding()
            self.assertIn("hxc@*", after, "the click binding was not wrapped: %r" % after)
            previous = before.split(closemark.KEY, 1)[1].strip() if before else ""
            if previous:
                self.assertTrue(after.rstrip().endswith(previous) or previous in after,
                                "the previous binding is not the else-branch: %r" % after)
            current = _own(tab["window"], "window-status-current-format")
            self.assertIn("range=user|hxc%s " % tab["window"], current)
            self.assertNotIn("range=user", _own(tab["window"], "window-status-format"),
                             "a tab that is not in view must not carry the mark")
            overview = box.window("agents")["id"]
            for option in ("window-status-format", "window-status-current-format"):
                self.assertNotIn("range=user", _own(overview, option), "the overview carries a mark")
                self.assertEqual(_own(T._pane_window(box.pane_id), option), "",
                                 "the reader's window was given a format")
            box.cli("down")
            self.assertEqual(_binding(), before, "down did not restore the previous binding")

    def test_click_on_the_mark_closes_the_view_and_the_name_still_selects(self):
        # Two clicks, in order: on the name (selects), then a walk rightward
        # from the name until one closes the tab. The walk exists because the
        # mark's exact column is tmux's layout, which this test must not
        # hardcode — only its position relative to the name is ours.
        with Sandbox() as box:
            _tmux("set-option", "-g", "mouse", "on")
            wkey, tab = self._one_tab(box, "deleg_mark0002", "click me")
            log = box.live() / "deleg_mark0002" / "task-0.log"
            client = Client(box.name, box.columns, box.rows)
            try:
                self.assertTrue(wait_for(
                    lambda: T._format(box.name, "#{session_attached}") == "1", timeout=10))
                # 1. a click on the tab's name selects it
                text = _status_text(box)
                self.assertNotIn(closemark.MARK, text, "a tab not in view shows a mark: %r" % text)
                client.click(text.index("click me"))
                self.assertTrue(wait_for(
                    lambda: T._format(box.name, "#{window_id}") == tab["window"], timeout=10),
                    "a click on the tab name did not select it (status: %r)" % text)
                # 2. now it is in view and carries the mark. Walk right from the
                #    name: a cell of the tab in view selects it again (nothing
                #    changes), the mark's cell closes it. The exact column is
                #    tmux's business; where the mark sits relative to the name
                #    is ours.
                text = _status_text(box)
                self.assertEqual(text.count(closemark.MARK), 1, "status: %r" % text)
                windows_before_close = len(box.windows())
                start = text.index("click me")
                for column in range(start, start + len("click me") + 8):
                    client.click(column)
                    if wait_for(lambda: not T._window_exists(tab["window"]), timeout=0.6):
                        break
                self.assertFalse(T._window_exists(tab["window"]),
                                 "no click on or after the name closed the tab (status: %r)" % text)
                self.assertEqual(len(box.windows()), windows_before_close - 1,
                                 "a click closed more than the one tab: %r" % box.windows())
            finally:
                client.close()
            # the view is gone, the worker is not
            self.assertTrue(wait_for(lambda: wkey in box.dismissed() and wkey not in box.tabs(),
                                     timeout=10), "layout: %r" % box.layout())
            manifest = (box.live() / "deleg_mark0002" / "manifest.json").read_text()
            self.assertIn('"status": "running"', manifest, "closing the view changed the worker")
            size = log.stat().st_size
            box.append_event("deleg_mark0002", 0, "tool", "-> STILL-WORKING(x)")
            self.assertGreater(log.stat().st_size, size)
            self.assertTrue(wait_for(lambda: "STILL-WORKING" in box.render_text(), timeout=15),
                            "the overview stopped following the worker")
            self.assertNotIn(wkey, box.tabs(), "the coordinator reopened a closed tab")
            # the reader's window was never in danger
            self.assertTrue(T._pane_exists(box.pane_id))
            self.assertEqual(T._format(box.pane_id, "#{pane_in_mode}"), "0",
                             "the handler wrote into the reader's pane")
            # and it comes back on request, marked again
            self.assertEqual(box.cli("open", "mark0002#0"), 0)
            self.assertTrue(wait_for(lambda: (box.tabs().get(wkey) or {}).get("pinned") is True,
                                     timeout=15))
            again = box.tabs()[wkey]["window"]
            self.assertIn("range=user|hxc%s " % again, _own(again, "window-status-current-format"))

    def test_close_refuses_everything_that_is_not_a_worker_tab(self):
        # The refusal is the security property: a target that is not a worker
        # tab — the reader's own window, the overview, an unknown id, a worker
        # window id with our prefix, and a shell-injection attempt — exits
        # non-zero, and the window set is unchanged afterwards.
        #
        # ``--quiet`` is the exception: exit 0 so tmux prints nothing into the
        # active pane, which is the whole reason the flag exists.
        with Sandbox() as box:
            wkey, tab = self._one_tab(box, "deleg_mark0003", "guard me")
            user_window = T._pane_window(box.pane_id)
            overview = box.window("agents")["id"]
            windows = sorted(w["id"] for w in box.windows())
            for target in (user_window, overview, "@99999", "hxc" + user_window):
                rc = box.cli("close", target)
                self.assertNotEqual(rc, 0, "close %s was not refused" % target)
            self.assertNotEqual(box.cli("close", "'; kill-server'"), 0)
            self.assertEqual(sorted(w["id"] for w in box.windows()), windows,
                             "a refused close changed the window set")
            self.assertEqual(box.cli("close", "--quiet", user_window), 0,
                             "--quiet must exit 0 so tmux prints nothing into the active pane")
            self.assertTrue(T._window_exists(user_window))
            self.assertIn(wkey, box.tabs())

    def test_a_pane_the_reader_split_into_the_tab_survives(self):
        # Closing must remove only the pane this tool created. A pane the user
        # split into the same window — here a real shell — is not ours and must
        # outlive the close.
        with Sandbox() as box:
            wkey, tab = self._one_tab(box, "deleg_mark0004", "split me")
            rc, mine = _tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", tab["window"],
                             "exec /bin/zsh -f")
            self.assertEqual(rc, 0, mine)
            self.assertEqual(box.cli("close", tab["window"]), 0)
            self.assertTrue(T._pane_exists(mine.strip()), "the reader's pane was closed")
            self.assertFalse(T._pane_exists(tab["pane"]), "our pane was left")
            self.assertTrue(wait_for(lambda: wkey not in box.tabs(), timeout=10))

    def test_off_restores_the_binding_and_on_brings_it_back(self):
        # Toggling must be lossless and idempotent: ``off`` restores the exact
        # previous binding, ``on`` is safe to run twice (the binding is wrapped
        # once, not twice), and ``down`` restores again.
        with Sandbox() as box:
            before = _binding()
            wkey, tab = self._one_tab(box, "deleg_mark0005", "toggle me")
            self.assertEqual(box.cli("close-mark", "off"), 0)
            self.assertEqual(_binding(), before, "off did not restore the previous binding")
            self.assertNotIn("range=user", _own(tab["window"], "window-status-current-format"))
            self.assertEqual(box.cli("close-mark", "on"), 0)
            self.assertIn("hxc@*", _binding())
            self.assertIn("range=user|hxc%s " % tab["window"],
                          _own(tab["window"], "window-status-current-format"))
            self.assertEqual(box.cli("close-mark", "on"), 0, "on is not idempotent")
            self.assertEqual(_binding().count("hxc@*"), 1, "the binding was wrapped twice")
            box.cli("down")
            self.assertEqual(_binding(), before)

    def test_opt_out_changes_nothing(self):
        # Opt-out is checked before anything is written: no binding change, no
        # mark on the window, nothing to clean up.
        with Sandbox() as box:
            before = _binding()
            box.export("HERMES_TMUX_CLOSE_MARK", "0")
            wkey, tab = self._one_tab(box, "deleg_mark0006", "leave me")
            self.assertEqual(_binding(), before, "the binding was changed despite the opt-out")
            self.assertNotIn("range=user", _own(tab["window"], "window-status-current-format"))


if __name__ == "__main__":
    unittest.main()
