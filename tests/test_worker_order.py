"""Pure tests for worker selection and stable ordering.

No tmux, no state reader: these functions decide which worker gets a pane and in
which order, and every layout regression starts with getting that right.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from hermes_tmux.watch import (
    MAX_VISIBLE,
    WORKERS_PER_WINDOW,
    _next_slot,
    select_visible,
    worker_order,
)


def worker(deleg: str, task: int, state: str = "running",
           started: str = "2026-11-12 09:00:00") -> SimpleNamespace:
    return SimpleNamespace(
        delegation_id=deleg,
        task_index=task,
        key="%s#%d" % (deleg, task),
        state=state,
        delegation_started=started,
    )


class WorkerOrderTest(unittest.TestCase):
    def test_tasks_of_one_delegation_stay_together_in_index_order(self):
        workers = [worker("deleg_a", 2), worker("deleg_a", 0), worker("deleg_b", 1)]
        self.assertEqual([w.key for w in worker_order(workers)],
                         ["deleg_b#1", "deleg_a#0", "deleg_a#2"])

    def test_state_does_not_change_position(self):
        """A worker finishing must not jump the queue and drag its pane along."""
        running = worker("deleg_new", 0, state="running")
        done = worker("deleg_old", 0, state="done", started="2026-11-12 08:00:00")
        before = [w.key for w in worker_order([running, done])]
        after = [w.key for w in worker_order([
            worker("deleg_new", 0, state="done"), done])]
        self.assertEqual(before, after,
                         "worker order changed when a worker settled")

    def test_workers_without_a_key_are_ignored(self):
        ghost = SimpleNamespace(delegation_id="x", task_index=0, key="",
                                state="running", delegation_started="")
        self.assertEqual(worker_order([ghost]), [])


class SelectVisibleTest(unittest.TestCase):
    def test_everything_is_visible_below_the_cap(self):
        workers = [worker("deleg_%d" % i, 0) for i in range(MAX_VISIBLE)]
        self.assertEqual(len(select_visible(workers)), MAX_VISIBLE)

    def test_running_workers_outrank_settled_ones_at_the_cap(self):
        old_running = worker("deleg_run", 0, started="2026-11-12 07:00:00")
        settled = [worker("deleg_done%d" % i, 0, state="done")
                   for i in range(MAX_VISIBLE)]
        visible = select_visible([old_running] + settled, limit=2)
        keys = [w.key for w in visible]
        self.assertIn("deleg_run#0", keys)
        self.assertEqual(len(keys), 2)

    def test_assigned_settled_workers_survive_the_cap(self):
        pinned = worker("deleg_pinned", 0, state="done",
                        started="2026-11-12 06:00:00")
        newer = [worker("deleg_new%d" % i, 0, state="done")
                 for i in range(MAX_VISIBLE)]
        visible = select_visible(newer + [pinned], assigned=[pinned.key], limit=2)
        self.assertIn(pinned.key, [w.key for w in visible])

    def test_result_keeps_stable_order(self):
        workers = [worker("deleg_a", 1), worker("deleg_b", 0, state="done",
                                                started="2026-11-12 08:00:00")]
        visible = select_visible(workers)
        self.assertEqual([w.key for w in visible], ["deleg_a#1", "deleg_b#0"])

    def test_capacity_constant_is_two(self):
        self.assertEqual(WORKERS_PER_WINDOW, 2)

    def test_next_slot_fills_the_first_gap_after_the_highest(self):
        self.assertEqual(_next_slot({}), "1")
        self.assertEqual(_next_slot({"1": "@1", "2": "@2"}), "3")
        self.assertEqual(_next_slot({"1": "@1", "3": "@3"}), "4")


if __name__ == "__main__":
    unittest.main()
