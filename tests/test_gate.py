"""Gate semantics: opt-out must win, default must be off until validated."""

from __future__ import annotations

import unittest

from hermes_tmux import gate


class GateTest(unittest.TestCase):
    def test_unset_follows_default(self):
        self.assertEqual(gate.enabled({}), gate.DEFAULT_ENABLED)

    def test_zero_disables(self):
        self.assertFalse(gate.enabled({"HERMES_TMUX": "0"}))
        self.assertFalse(gate.enabled({"HERMES_TMUX": "off"}))
        self.assertFalse(gate.enabled({"HERMES_TMUX": "FALSE"}))

    def test_one_enables_regardless_of_default(self):
        self.assertTrue(gate.enabled({"HERMES_TMUX": "1"}))
        self.assertTrue(gate.enabled({"HERMES_TMUX": "yes"}))

    def test_garbage_falls_back_to_default(self):
        self.assertEqual(gate.enabled({"HERMES_TMUX": "maybe"}), gate.DEFAULT_ENABLED)

    def test_opt_out_wins_when_default_on(self):
        original = gate.DEFAULT_ENABLED
        try:
            gate.DEFAULT_ENABLED = True
            self.assertTrue(gate.enabled({}))
            self.assertFalse(gate.enabled({"HERMES_TMUX": "0"}))
        finally:
            gate.DEFAULT_ENABLED = original

    def test_disabled_reason_names_the_cause(self):
        self.assertIn("HERMES_TMUX=0", gate.disabled_reason({"HERMES_TMUX": "0"}))
        self.assertEqual(gate.disabled_reason({"HERMES_TMUX": "1"}), "")


if __name__ == "__main__":
    unittest.main()
