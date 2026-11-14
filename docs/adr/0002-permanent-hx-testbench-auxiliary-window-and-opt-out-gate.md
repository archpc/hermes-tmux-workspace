# ADR-0002: Permanent hx:testbench auxiliary window and opt-out gate

- Status: accepted
- Date: 2026-11-14
- Deciders: Sein, Carbon (lead architect)
- Supersedes: none
- Superseded by: none

## Context and problem

Hermes TestBench (`~/Code/hermes-testbench`) provides a standalone verification engine, SQLite WAL state store, and terminal-native TUI (`~/.local/bin/testbench tui`). Operators working inside the `hermes-tmux` workspace need live test execution visibility, historical failure inspection, and explicit rerun/cancel controls alongside `agents` and `hx:mail`, without introducing a second tmux lifecycle daemon or violating ADR-0001's side-car rules.

## Constraints and requirements

- Must reuse `ensure_aux_window` (`hermes_tmux/cli.py`), per-pane `@hx` ownership plus `_KILLABLE_COMMANDS` provenance (`hermes_tmux/tmuxio.py`), and locked `layout["aux"]` updates (`hermes_tmux/watch.py`).
- Must never steal focus during `hermes-tmux up` or background crash recovery (`focus=False`), and must focus explicitly when the user invokes `hermes-tmux testbench` (`focus=True`).
- Must order static windows deterministically (`agents`, `hx:mail`, `hx:testbench`, then dynamic worker tabs) without hard-coded window indices or renumbering existing user windows.
- Must keep the tab optional (`HERMES_TMUX_TESTBENCH=0` skips automatic creation and recovery while keeping `hermes-tmux testbench` available on demand) and degrade to an interactive shell notice if `~/.local/bin/testbench` is absent.
- Non-default `HERMES_HOME` profiles and test sandboxes must isolate `TESTBENCH_HOME` under `$HERMES_HOME/cache/testbench` unless explicitly set.

## Alternatives considered

| Alternative | Why it was weighed | Why it lost |
|---|---|---|
| Separate TestBench tmux daemon managing `hx:testbench` | Would keep `hermes-tmux` untouched | Two coordinators racing on the same tmux server violate ADR-0001 and risk duplicate windows, focus races, and `layout.json` drift |
| Repurpose existing `hx:tests` shell-runner role | `tests` already exists in `AUX_ROLES` | `hermes-tmux tests --run` has existing callers and tests (`tests.log` capture); replacing it silently breaks compatibility |
| Unconditional permanent window with no opt-out | Matches `hx:mail` literally | Violates the requirement that `hx:testbench` remain optional and lightweight for sessions that do not want it open |

## Decision

Add `"testbench"` to `AUX_ROLES` (`hx:testbench`), launching `~/.local/bin/testbench tui` (with `PATH` and shell fallback), created detached after `hx:mail` during `_up` and restored detached by `watch.py` when `testbench_tab_enabled()` (`HERMES_TMUX_TESTBENCH != 0`). Add `"testbench"` to `tmuxio._KILLABLE_COMMANDS` and isolate `TESTBENCH_HOME` for non-default `HERMES_HOME`.

## Rationale

`ensure_aux_window` already implements every required invariant for `hx:mail` (commit `a74dd24`): detached creation via `after_window`, idempotent reuse on repeated `up`, per-pane `@hx` provenance, foreign-split preservation on rebuild and `down`, and refusal by `cmd_close` ("not a worker tab"). Reusing that exact path adds 34 lines across 4 files instead of a parallel lifecycle manager.

## Tradeoffs and consequences

- Static workspace windows on default `hermes-tmux up` increase from two (`agents`, `hx:mail`) to three (`agents`, `hx:mail`, `hx:testbench`), updating static window count assertions in `tests/test_tabs.py`, `tests/test_close_mark.py`, and `tests/test_tmux_integration.py`.
- Sessions that prefer fewer tabs must export `HERMES_TMUX_TESTBENCH=0` before `hermes-tmux up`.

## Supporting evidence

- `hermes_tmux/cli.py` (`testbench_tab_enabled`, `_up`, `_aux_command`, `ensure_aux_window`)
- `hermes_tmux/watch.py` (detached `hx:testbench` crash recovery loop)
- `hermes_tmux/tmuxio.py` (`_KILLABLE_COMMANDS`)
- `hermes_tmux/env.py` (`_PASSTHROUGH`, `TESTBENCH_HOME` isolation)
- `tests/test_testbench_aux.py` (`PermanentTestBenchTabTests`)
- Opus 5.5 consultation receipt `90eba6086c27b546687b288d90123d27a21da877160df0b46da5f4147c6ab655` (recommended reusing `ensure_aux_window` and isolating TUI viewing from TestBench execution ownership)

## Verification status

verified (`TMUX_TMPDIR=$(mktemp -d) python3.14 -W default -m unittest tests.test_testbench_aux tests.test_mail_aux tests.test_tabs tests.test_close_mark -v` — 27/27 passed, 2026-11-14)

## Related decisions

- ADR-0001 (`docs/adr/0001-read-only-side-car-over-hermes-artifacts-not-agent-hooks.md`)
