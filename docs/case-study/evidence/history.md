# hermes-tmux — v1 and v2 history

Versions by commit: v1 = `859d3f0`, `bdd9fc7`, `e9a66d3`; v2 = `4d93f6e` … `a17522d`
(tag `v2-two-per-window`); v3 (out of scope) starts at `ade244b`.

## 1. Original problem

Hermes already writes one append-only transcript per delegated task, at
`$HERMES_HOME/cache/delegation/live/<deleg_id>/task-<n>.log` — `docs/DESIGN.md:13`
«$HERMES_HOME/cache/delegation/live/<deleg_id>/task-<n>.log».

The tool exists to turn that material into a per-session tmux workspace — `commit 859d3f0`
«hermes-tmux builds a per-session workspace inside tmux (overview, one pane per».

It does so without touching Hermes — `docs/DESIGN.md:41`
«hermes-tmux  (python3, stdlib only, single process per tmux session)».

The hard part was honesty about facts Hermes does not record: review status — `docs/DESIGN.md:23`
«Hermes has no concept of "reviewing a delegation".».

Running workers also have no live cost data — `docs/DESIGN.md:27`
«`result_json` has these only after the task settles.».

And absence of events is not evidence of progress — `docs/DESIGN.md:29`
«**Tool-level progress between transcript flushes.** The transcript is the source of».

## 2. Timeline (main, up to a17522d)

| hash | date (-0500) | subject | meaning |
|---|---|---|---|
| `859d3f0` | 2026-11-12 13:32:54 | tmux workspace: live subagent panes with verified ownership | v1 lands: overview + one pane per subagent + aux windows |
| `bdd9fc7` | 2026-11-12 13:37:33 | Enable the tmux workspace by default | `DEFAULT_ENABLED` flipped to True after validation |
| `e9a66d3` | 2026-11-12 13:51:19 | Close the remaining review notes: server identity, two test pins | coordinator records server pid; two tests pinned |
| `4d93f6e` | 2026-11-12 17:33:18 | Worker windows: overview-only hx:workers, two panes per worker window | v2 layout starts |
| `3ffb719` | 2026-11-12 17:38:27 | Fix overview staleness (db watch, draw scroll), migration test, docs | v2 fixes |
| `acc7a54` | 2026-11-12 17:41:23 | Focus worker command across windows; add delegated layout regressions and 0-8 worker matrix | v2 `worker N` fix, layout tests |
| `c21ae8f` | 2026-11-12 17:44:01 | Cap visible worker panes at eight (four two-pane windows) | v2 cap lowered from 16 to 8 |
| `a17522d` | 2026-11-12 17:47:03 | down: stop the coordinator before closing panes (no reopen race) | v2 final fix |

Subjects are the commit subjects as printed by git log.

## 3. v1: one workers window, four panes

Layout: a single `hx:workers` window holding the overview pane plus up to four
`tail -F` panes — `commit 3ffb719`
«pane1 overview (hermes-tmux follow) + up to 4 x tail -F task-N.log».

Everything else was lazy aux windows (`hx:logs`, `hx:tests`, `hx:debug`,
`hx:rea`) created on first use — `commit 859d3f0`
«logs/tests/debug/rea aux windows) and keeps it honest».

Built as one stdlib Python process per tmux session — `docs/DESIGN.md:41`
«hermes-tmux  (python3, stdlib only, single process per tmux session)».

Defects found while validating v1, all fixed in `859d3f0` — `docs/DESIGN.md:120`
«integration suite had never run to completion. Running it on an isolated socket».
Cause and fix columns paraphrase the row cited in the last column.

| # | defect | cause | fix | source |
|---|---|---|---|---|
| D1 | integration suite hung | harness targeted `session:0`, but the user's `tmux.conf` sets `base-index 1` | harness addresses panes by id and runs with `-f /dev/null` | `docs/DESIGN.md:125` «integration suite hung, then failed every test» |
| D3 | no worker panes ever appeared | `reconcile_panes` read an attribute nothing set | `Worker.log_path` from the manifest | `docs/DESIGN.md:127` «no worker panes ever appeared» |
| D6 | `down` closed nothing; foreign-pane guard passed for the wrong reason | marker read as `{@hx}` (literal) instead of `#{@hx}` | `_SENTINEL_FMT = "#{@hx}"` | `docs/DESIGN.md:130` «ownership marker read as `{@hx}` (literal) instead of `#{@hx}`» |
| D9 | aux/worker panes read the wrong profile and failed to import | new windows inherit the server env, not the caller's | pass env with `new-window -e` | `docs/DESIGN.md:133` «aux/worker panes read the wrong profile and failed to import the package» |
| D15 | one idle coordinator accumulated per closed session (93 found) | `run-shell -b` child outlives the server; loop never checked | loop probes session liveness and exits | `docs/DESIGN.md:139` «one idle coordinator process accumulated per closed session» |
| D25 | stale-pane recovery, window accounting, socket cleanup silently disabled | `display-message` exits 0 for a missing target on tmux 3.8 | existence = resolved id equals the id asked for | `docs/DESIGN.md:149` «stale-pane recovery, window accounting and the suite's socket cleanup were all silently disabled» |
| D27 | overview lagged a lifecycle change by up to 20 s | only the delegation dir and `*.log` were watched | each `manifest.json` watched explicitly | `docs/DESIGN.md:151` «the overview lagged a lifecycle change by up to 20 s» |
| D28 | a `follow` loop outlived its server | pane-side loop never asked whether its pane existed | loop returns when `pane_exists` is false | `docs/DESIGN.md:156` «a `follow` loop survived the death of its server» |

Post-deployment v1 fix: a coordinator now records the tmux server pid it belongs
to and exits when a different server answers the same socket — `commit e9a66d3`
«A coordinator now records the tmux server pid it belongs to and exits when a».

## 4. v2: overview-only window, two panes per worker window

What changed: the overview got its own window and transcripts moved out —
`docs/DESIGN.md:194`
«Version 2 put the overview in `hx:workers` and two transcripts in each».

At most two panes per worker window, new windows named `hx:worker-N`. The reason
given is pane readability and bounded churn — `commit 4d93f6e`
«every pane wide enough to be readable and bounds visual churn: a window is».

Ordering became independent of run state, so a finishing worker cannot drag its
pane to another slot — `commit 4d93f6e`
«Deliberately independent of run state. Ordering by state made a worker that».

The visible cap was first 16, then eight — `commit c21ae8f`
«up to 8 worker panes are shown, running workers first, then the most recent».

Defects found in v2 (cause and fix paraphrase the cited row/line):

| # | defect | cause | fix | source |
|---|---|---|---|---|
| v2-1 | overview kept a stale `running` row after a DB-only completion | `state.db` / `-wal` were not watched | both files watched (1 s floor) | `hermes_tmux/watch.py:343` «Without a watch on the database a task that finishes only there kept its» |
| v2-2 | first delegation on a fresh home raised no event | `state.db` did not exist yet, so nothing watched it | the home directory is watched too | `hermes_tmux/watch.py:347` «too so creating state.db (first run, first delegation) raises an event.» |
| v2-3 | a closed pane kept a slot open, misplacing later panes | slot map was not recounted after closes | slot panes recounted after removal | `commit 3ffb719` «Recount: a pane closed above must not hold a slot open.» |
| v2-4 | overview title scrolled out of a short pane; pane looked dead | writing exactly `height` lines with a trailing newline scrolls one | draw bounded to fit the pane | `tests/test_tmux_integration.py:901` «Writing exactly `height` lines with a trailing newline scrolls one» |
| v2-5 | `worker N` selected the pane but stayed in the wrong window | `select-pane` alone does not switch the session's current window | select window, then pane | `hermes_tmux/cli.py:576` «`select-pane` alone does not switch the session's current window, so» |
| v2-6 | `down` appeared to leave worker windows behind | the live coordinator reopened them mid-teardown | coordinator stopped before any pane closes | `hermes_tmux/cli.py:286` «reconcile the empty window set and reopen worker windows between our pane» |
| v2-7 | stale v1 panes in the overview window had to be retired | v1 put tail panes in `hx:workers` | `trim_overview_window` migration | `tests/test_tmux_integration.py:714` «The ``trim_overview_window`` migration no longer exists, and v1's» |

Test count at `a17522d`: stated as 142 — `README.md@a17522d:98` «# 142 tests».
That number is stale: counting `def test_` in `git show a17522d:tests` gives 166
across 15 test files, the README number having carried over from v1 —
`commit bdd9fc7` «142 tests on a private socket (gate default included)».
The 166 is computed here, not run.

## 5. What was wrong with v2, per DESIGN.md section 5

Four tabs of stale transcripts, numeric tab names, a table keyed by delegation
id, and a failure badge that never cleared — `docs/DESIGN.md:195`
«`hx:worker-N`. Rendered against a real day of work it showed four tabs of».

`docs/DESIGN.md:196` «transcripts up to nine hours old, numeric tab names, a seven-column table keyed».

The badge problem: attention could never be acknowledged — `docs/DESIGN.md:212`
«An alert that cannot clear is not an alert.».

Version 2 also recreated a tab the reader had closed — `docs/DESIGN.md:211`
«Version 2 recreated it, which is the tool overriding the reader.».
