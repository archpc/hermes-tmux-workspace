"""Layout.json is shared state; updates must be transactions, not snapshots.

The coordinator, `up`, and every aux command rewrite layout.json. Each used to
read at the start of its work and write the whole document back at the end, so a
writer that was mid-flight silently reverted another process's change. Observed:
`up` followed by `logs` dropped ``tail_panes`` and left a second, untracked tail
pane for the same worker; the coordinator could drop an ``aux`` entry the same
way.

The concurrency test fails against the old read-modify-write shape (parallel
processes overwrite each other); the integration test pins the reported sequence.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_tmux_integration import PY, REPO, Sandbox, _q, _tmux, wait_for

WORKERS = 8
ROUNDS = 20


class LayoutTransactionTest(unittest.TestCase):
    def test_parallel_updates_do_not_lose_each_other(self):
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

    def test_logs_after_up_keeps_the_tail_pane_record(self):
        with Sandbox() as box:
            box.add_delegation("deleg_race0001", [{"goal": "race"}])
            self.assertEqual(box.cli("up"), 0)
            self.assertEqual(box.cli("logs"), 0)
            layout = box.layout()
            tail = layout.get("tail_panes") or {}
            self.assertEqual(len(tail), 1,
                             "the aux write clobbered tail_panes: %r" % layout)
            pane = list(tail)[0]
            self.assertTrue(wait_for(lambda: _tmux("display-message", "-p", "-t", pane,
                                                  "#{pane_id}")[1].strip() == pane),
                            "the recorded tail pane does not exist")

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
