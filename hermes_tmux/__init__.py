"""hermes-tmux: a tmux view over Hermes' async subagent delegations.

The package is organised by surface, and the split is deliberate — the display
half is pure and testable without a terminal:

* ``state`` reads Hermes' own artifacts (live transcripts, ``manifest.json``,
  ``state.db``) into ``Worker`` records. Read-only, total: it never writes and
  never raises.
* ``labels`` decides names, glyphs, colours and width arithmetic, so every
  surface agrees.
* ``render`` turns workers into the overview block, given a width, a height
  budget and an explicit ``now``.
* ``transcript`` follows one worker's log and prints it readably.
* ``closemark`` and ``tmuxio`` own the tmux mechanics (options, bindings, pane
  ownership checks).
* ``cli`` (with ``__main__``) is the entry point: ``hermes-tmux up``/``down``,
  ``status``, ``rerender``, ``debug-report``, ``follow``/``watch``, ``run``,
  ``worker``, ``open``/``close``, ``close-mark``, ``tail``, ``review``, and the
  aux panes (``logs``, ``tests``, ``debug``, ``rea``, ``mail``, ``testbench``).

No module here starts or contacts a tmux server at import time: tmux is only
invoked through ``subprocess`` when a command actually runs, so importing the
package (and rendering ``status`` output) works outside tmux.
"""
