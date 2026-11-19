"""Gate semantics: opt-out wins, and an unrecognised value changes nothing.

``gate.enabled`` resolves in one order, and each step has a reason:

1. no ``HERMES_TMUX`` → ``DEFAULT_ENABLED``;
2. a falsey spelling (``0``/``off``/``false``/...) → off, always;
3. a truthy spelling (``1``/``yes``/``on``/...) → on, always;
4. anything else — a typo, a stray space, an empty string, ``maybe`` → the
   default, *not* "off".

Step 4 is the safety choice for a display tool: a mistyped value must not make
the workspace vanish from a shell that wanted it. The tests therefore compare
against ``gate.DEFAULT_ENABLED`` rather than hardcoding ``True``, so flipping
the default back for a rollback does not rewrite these expectations.

``disabled_reason`` has one branch that is unreachable while the default is on
(no variable + disabled). It is kept for the rollback case; these tests only
pin what is reachable, which is why nothing here asserts the "disabled by
default" string.

``DEFAULT_ENABLED`` is read at call time, not captured: the opt-out test
monkey-patches the module attribute and restores it in ``finally`` so a failure
cannot leak the change into another test.
"""

from __future__ import annotations

import unittest

from hermes_tmux import gate


class GateTest(unittest.TestCase):
    """Pure function tests: every case passes its own ``environ`` mapping.

    Nothing here reads ``os.environ`` — ``enabled(environ)`` takes a mapping
    precisely so these run identically on a machine where the developer has
    exported ``HERMES_TMUX``.
    """

    def test_unset_follows_default(self):
        """No variable means the compiled default, whatever it currently is."""
        self.assertEqual(gate.enabled({}), gate.DEFAULT_ENABLED)

    def test_zero_disables(self):
        """Falsey spellings are matched case-insensitively and strip-safe."""
        self.assertFalse(gate.enabled({"HERMES_TMUX": "0"}))
        self.assertFalse(gate.enabled({"HERMES_TMUX": "off"}))
        self.assertFalse(gate.enabled({"HERMES_TMUX": "FALSE"}))

    def test_one_enables_regardless_of_default(self):
        """A truthy value overrides a default that might be off."""
        self.assertTrue(gate.enabled({"HERMES_TMUX": "1"}))
        self.assertTrue(gate.enabled({"HERMES_TMUX": "yes"}))

    def test_garbage_falls_back_to_default(self):
        """A typo is not an opt-out: it resolves exactly like no value at all."""
        self.assertEqual(gate.enabled({"HERMES_TMUX": "maybe"}), gate.DEFAULT_ENABLED)

    def test_opt_out_wins_when_default_on(self):
        """With the default forced on, ``0`` still disables: opt-out is absolute.

        The attribute is patched rather than the environment because that is
        the only way to state "even when on by default" without relying on what
        the shipped default happens to be. `finally` restores it.
        """
        original = gate.DEFAULT_ENABLED
        try:
            gate.DEFAULT_ENABLED = True
            self.assertTrue(gate.enabled({}))
            self.assertFalse(gate.enabled({"HERMES_TMUX": "0"}))
        finally:
            gate.DEFAULT_ENABLED = original

    def test_disabled_reason_names_the_cause(self):
        """A refusal quotes the variable the reader actually set.

        ``HERMES_TMUX=1`` must yield ``""`` — a caller printing the reason
        unconditionally must print nothing when the workspace is on.
        """
        self.assertIn("HERMES_TMUX=0", gate.disabled_reason({"HERMES_TMUX": "0"}))
        self.assertEqual(gate.disabled_reason({"HERMES_TMUX": "1"}), "")


if __name__ == "__main__":
    unittest.main()
