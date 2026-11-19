"""Lifecycle propagation: a manifest write must reach the overview promptly.

The coordinator watches the delegation directory and its ``*.log`` files. A
directory vnode fires for create/delete but NOT for a write to a child, and the
manifest is rewritten in place when a task changes state. Before ``manifest.json``
was watched explicitly, the overview lagged a lifecycle change by the whole
session-check interval (20 s). This test fails if that registration is removed.

Assertions use the renderer's summary line ("total 1: done 1"), not a substring
of the whole pane: the header contains the word "running" and pane scrollback can
hold text from an earlier test that reused the same pane id.
"""

from __future__ import annotations

import json
import time
import unittest

from tests.test_tmux_integration import Sandbox, _tmux, wait_for

DEADLINE_S = 6.0


class LifecyclePropagationTest(unittest.TestCase):
    """One sandbox, one flip: a manifest rewrite must repaint before the slow poll."""
    def _manifest(self, box: Sandbox, deleg: str, status: str) -> None:
        """Rewrite every task's status in place, the way the writer does.

        Same path, whole document, rewritten in place (no rename): a content
        write to a child of a watched directory, which no directory vnode
        reports. The test can only pass if ``manifest.json`` itself is
        registered.
        """
        path = box.home / "cache/delegation/live" / deleg / "manifest.json"
        data = json.loads(path.read_text())
        for task in data["tasks"]:
            task["status"] = status
        path.write_text(json.dumps(data))

    def _overview_text(self, box: Sandbox) -> str:
        """The visible screen only.

        Scrollback can hold "RUNNING" from an earlier pass or an earlier test that
        reused the pane id, so capturing with history would make the assertion
        satisfiable by stale text.
        """
        pane = box.layout().get("overview_pane")
        self.assertTrue(pane, box.layout())
        # Visible screen only: scrollback can hold an earlier test's content.
        return _tmux("capture-pane", "-p", "-t", pane)[1]

    def test_completion_reaches_the_overview_without_waiting_for_the_poll(self):
        """A manifest write reaches the overview well inside one session-check interval.

        The 2.5 s sleep first drains the event burst raised while the workspace was
        built: a flip landing inside that burst would be rendered by its wake-up, and
        the test would pass with the manifest watch removed. ``DEADLINE_S`` is 6 s
        against a 20 s poll, so only the explicit watch can explain a pass.
        """
        with Sandbox() as box:
            deleg = "deleg_life0001"
            box.add_delegation(deleg, [{"goal": "lifecycle"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertTrue(wait_for(lambda: "RUNNING" in self._overview_text(box), timeout=15),
                            "the worker never rendered in a live state: %r"
                            % self._overview_text(box)[-200:])
            # Let the events raised while the workspace was built drain first:
            # a flip that lands inside that burst is rendered by the wake-up it
            # causes, which would make this test pass even without the watch.
            time.sleep(2.5)
            self._manifest(box, deleg, "done")
            self.assertTrue(
                wait_for(lambda: "Nothing running." in self._overview_text(box)
                         and "RUNNING" not in self._overview_text(box),
                         timeout=DEADLINE_S),
                "the overview did not show the completed state within %.0fs; "
                "manifest.json is probably no longer watched: %r"
                % (DEADLINE_S, self._overview_text(box)[-300:]))


if __name__ == "__main__":
    unittest.main()
