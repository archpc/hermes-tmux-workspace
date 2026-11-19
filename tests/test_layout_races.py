"""Layout.json is shared state; updates must be transactions, not snapshots.

The coordinator, `up`, and every aux command rewrite layout.json. Each used to
read at the start of its work and write the whole document back at the end, so a
writer that was mid-flight silently reverted another process's change. Observed:
`up` followed by `logs` dropped the worker tab record and left a second,
untracked transcript pane for the same worker; the coordinator could drop an
``aux`` entry the same way.

The concurrency test fails against the old read-modify-write shape (parallel
processes overwrite each other); the integration test pins the reported sequence.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_tmux_integration import PY, REPO, Sandbox, _q, _tmux, wait_for

WORKERS = 8
ROUNDS = 20


class LayoutTransactionTest(unittest.TestCase):
    """Real processes and the real layout file; tmux is real where the race needs it."""
    def test_parallel_updates_do_not_lose_each_other(self):
        """Eight writers, twenty appends each, all through ``update_layout``.

        Per-writer counts are asserted as well as presence: an update lost near the
        end still leaves the key present, so only the length discriminates. The old
        read-modify-write shape fails here because the last writer's snapshot drops
        every append made after its read.
        """
        key = "race"
        home = Path(tempfile.mkdtemp(prefix="hxrace_"))
        try:
            program = (
                "import sys\n"
                "from pathlib import Path\n"
                "from hermes_tmux import watch\n"
                "name = sys.argv[1]\n"
                "for i in range(%d):\n"
                "    watch.update_layout(%r, lambda lay: lay.setdefault('seen', {})"
                ".setdefault(name, []).append(i), Path(%r))\n" % (ROUNDS, key, str(home))
            )
            script = home / "worker.py"
            script.write_text(program)
            child_env = dict(os.environ, PYTHONPATH=str(REPO))
            procs = [subprocess.Popen([PY, str(script), "w%d" % n], cwd=str(REPO),
                                      env=child_env, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)
                     for n in range(WORKERS)]
            for proc in procs:
                err = proc.communicate(timeout=120)[1]
                self.assertEqual(proc.returncode, 0, (err or b"")[-300:])
            data = json.loads((home / "cache" / "hermes-tmux" / "sessions" / key
                               / "layout.json").read_text())
            seen = data.get("seen") or {}
            missing = [n for n in ("w%d" % i for i in range(WORKERS)) if n not in seen]
            self.assertFalse(missing, "updates lost for %r: %r" % (missing, sorted(seen)))
            short = {n: len(v) for n, v in seen.items() if len(v) != ROUNDS}
            self.assertFalse(short, "updates dropped inside a writer: %r" % short)
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)

    def test_aux_does_not_write_back_a_layout_it_read_before_the_window(self):
        """Deterministic version of the reported race.

        `cmd_aux` reads layout.json, creates the window, then writes. A writer
        that publishes in that gap must not be reverted. The window creation is
        slowed down here so the gap is guaranteed; the old code writes the stale
        document back and loses `tail_panes`.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_race0002", [{"goal": "stale"}])
            self.assertEqual(box.cli("up"), 0)
            key = next(d.name for d in (box.home / "cache/hermes-tmux/sessions").iterdir())

            program = (
                "import sys, time\n"
                "import hermes_tmux.tmuxio as t\n"
                "orig = t.new_window\n"
                "def slow(*a, **k):\n"
                "    time.sleep(0.6)\n"
                "    return orig(*a, **k)\n"
                "t.new_window = slow\n"
                "from hermes_tmux.cli import main\n"
                "sys.exit(main(['logs']))\n"
            )
            script = box.home / "slow_logs.py"
            script.write_text(program)
            info = _tmux("display-message", "-p", "#{socket_path}|#{pid}")[1]
            sock, server = info.split("|")
            env = dict(os.environ, TMUX="%s,%s,0" % (sock, server), TMUX_PANE=box.pane_id,
                       HERMES_HOME=str(box.home), HERMES_TMUX="1", PYTHONPATH=str(REPO))
            aux = subprocess.Popen([PY, str(script)], env=env, cwd=str(REPO),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # Wait until the aux command has read the layout (it sleeps inside
            # new_window next), then publish a change it never saw. Two aux
            # commands can genuinely race, and the coordinator does not own
            # this key, so the coordinator's own reconcile cannot mask the
            # result the way it would for a tab record.
            time.sleep(0.15)
            publish = (
                "from pathlib import Path\n"
                "from hermes_tmux import watch\n"
                "watch.update_layout(%r, lambda lay: lay.setdefault('aux', {})"
                ".__setitem__('debug', '%%7777'), Path(%r))\n" % (key, str(box.home))
            )
            subprocess.run([PY, "-c", publish], env=env, cwd=str(REPO), check=True, timeout=60)
            aux.communicate(timeout=90)
            self.assertEqual(aux.returncode, 0)

            layout = box.layout()
            self.assertIn("debug", (layout.get("aux") or {}),
                          "the aux command reverted a concurrent update: %r" % layout)
            self.assertIn("logs", (layout.get("aux") or {}), layout)

    def test_logs_after_up_keeps_the_worker_tab_record(self):
        """The exact reported damage: ``up`` then ``logs`` must keep the worker tab.

        Two claims: the aux write does not clobber ``tabs`` (and the pane it names is
        still live), and the coordinator's next pass does not drop the ``aux`` entry
        it did not write. The appended event is what forces that pass.
        """
        with Sandbox() as box:
            box.add_delegation("deleg_race0001", [{"goal": "race"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertEqual(box.cli("logs"), 0)
            layout = box.layout()
            tabs = layout.get("tabs") or {}
            self.assertEqual(len(tabs), 1,
                             "the aux write clobbered tabs: %r" % layout)
            pane = list(tabs.values())[0]["pane"]
            self.assertTrue(wait_for(lambda: _tmux("display-message", "-p", "-t", pane,
                                                  "#{pane_id}")[1].strip() == pane),
                            "the recorded worker tab pane does not exist")

            # And the coordinator must not lose the aux entry in its next pass.
            log = box.home / "cache/delegation/live/deleg_race0001/task-0.log"
            with log.open("a", encoding="utf-8") as handle:
                handle.write("09:00:02 note | more\n")
            self.assertTrue(wait_for(
                lambda: "aux" in (box.layout() or {})
                and "logs" in ((box.layout() or {}).get("aux") or {}), timeout=15),
                "the coordinator dropped the aux entry: %r" % box.layout())


def tearDownModule():
    """Kill only this process's private server, and remove its socket file."""
    from tests.test_tmux_integration import tearDownModule as _harness_teardown
    _harness_teardown()


if __name__ == "__main__":
    unittest.main()
