# v3 evidence inventory

hermes-tmux v3, repo `/Users/sein/Code/hermes-tmux-workspace` (branch `main`,
commit `ade244b` plus docs commit `ce69f1c`). Each claim cites `file:line` and
quotes it. Read-only: no test run, no tmux, no network.

## (a) Tests — 308 methods

In `tests/`: `grep -c '    def test_' *.py | awk -F: '{s+=$2; print} END {print "TOTAL",s}'`
→ TOTAL 308 over 17 modules.

- **test_concurrency.py** — 3 — two sessions, one server, disjoint. tmux. `tests/test_concurrency.py:1` "Concurrency and isolation, on real sessions of one private tmux server."
- **test_existence.py** — 5 — existence checks, socket hygiene. tmux. `tests/test_existence.py:1` "Adversarial tests for tmux target existence and socket hygiene."
- **test_follow_liveness.py** — 2 — loop exits with its pane. tmux. `tests/test_follow_liveness.py:1` "The pane-side display loop must not outlive the pane it draws into."
- **test_gate.py** — 6 — gate opt-out, default off. pure. `tests/test_gate.py:1` "Gate semantics: opt-out must win, default must be off until validated."
- **test_honesty.py** — 24 — display truthfulness. pure, tmuxio mocked. `tests/test_honesty.py:1` "Unit tests for the honesty rules the display depends on."
- **test_isolation_guards.py** — 3 — coordinator dies with its session. tmux. `tests/test_isolation_guards.py:1` "Two guards that keep the workspace inside its own fences."
- **test_labels.py** — 65 — naming, escaping, clipping. pure. `tests/test_labels.py:3` "Pure functions only: no tmux, no clock. Everything asserted here is what the"
- **test_layout_races.py** — 3 — layout.json transactions. tmux. `tests/test_layout_races.py:1` "Layout.json is shared state; updates must be transactions, not snapshots."
- **test_lifecycle.py** — 1 — manifest write reaches overview. tmux. `tests/test_lifecycle.py:1` "Lifecycle propagation: a manifest write must reach the overview promptly."
- **test_process_guards.py** — 4 — signals only to our watcher. pure. `tests/test_process_guards.py:1` "Unit tests for the safety guards around the workspace's own processes."
- **test_render.py** — 59 — renderer width and trimming. pure. `tests/test_render.py:3` "No tmux, no state module: workers are ``types.SimpleNamespace`` stubs carrying"
- **test_state.py** — 31 — readers, temp homes. pure. `tests/test_state.py:3` "Every fixture is built inside a ``tempfile.TemporaryDirectory``; nothing here"
- **test_state_json.py** — 5 — JSON-column fields. pure. `tests/test_state_json.py:3` "These pin two facts established by reading the installed Hermes source and the"
- **test_tab_merge.py** — 11 — merge keeps `open`'s tab. pure, in-memory fake. `tests/test_tab_merge.py:9` "No tmux here: the tmux layer is replaced by a small in-memory fake so the race"
- **test_tabs.py** — 12 — tab contract, end to end. tmux, private `-L` server. `tests/test_tabs.py:1` "Integration tests for the v3 tab contract (one worker, one tab)."
- **test_tmux_integration.py** — 33 — sessions end to end, harness. tmux. `tests/test_tmux_integration.py:1` "End-to-end tests against real, isolated tmux sessions."
- **test_transcript.py** — 41 — `Formatter`, `run_tail`. pure. `tests/test_transcript.py:3` "Formatter is fed lines and returns display lines; no terminal, no clock, no"

## (b) Independent review, two rounds

### Round 1: FAIL, one blocker

Round summary, `glm/prompt-2.md:1`: "VERDICT: FAIL, one blocker, nine non-blocking". Token `VERDICT: FAIL` at `glm/review-1.txt:1`.

Blocker 1 (inv. 5, defect V7): the pass "reads layout outside the lock, then does tmux work (new_window, wait_for_panes, decorate: watch.py:648-686)", then rewrites the whole `tabs` map (`glm/review-1.txt:5`).

Non-blocking (`review-1.txt`):

1. inv. 1 `_killable` "authorises any pane with an inherited `@hx` marker" (`glm/review-1.txt:9`).
2. inv. 1/a "I could not construct a state where that id names an unmarked window within one server" (`glm/review-1.txt:11`).
3. inv. 6 "a tab the user is actually reading can retire after linger. Resolve the active check through the pane's live window id." (`glm/review-1.txt:13`).
4. inv. 6/c "A worker window that lost its marker is now left behind where `main` still found it by name/prefix/recorded id" (`glm/review-1.txt:15`).
5. inv. 4 "if `lsof` is missing or times out, `_lock_holder`/`_watcher_pid` return None" (`glm/review-1.txt:17`).
6. inv. 7 "when the file is not watched (`sync` hit the 256-file cap or the open failed), every 1 s timeout calls" (`glm/review-1.txt:19`).
7. (e) fd leak: "leaks both pipe fds on `close()`" (`glm/review-1.txt:21`).
8. (h) "so with a pane shorter than header+RUNNING alone, trailing RUNNING rows are cut" (`glm/review-1.txt:23`).
9. (d) "a SIGTERM in the window before the handler is registered leaves a stale pid file" (`glm/review-1.txt:25`).

### Round 2: PASS, no blockers

Token `VERDICT: PASS` (`glm/review-2.txt:10`), `BLOCKERS: none.` (line 12). Blocker closed: "tabs merge never touches K (absent from both `tabs0` and `tabs`), open's record survives" (`glm/review-2.txt:16`).

Non-blocking (`review-2.txt`):

1. `quiet_tty` "never restores termios, and `run_tail` calls it before the follow check" (`glm/review-2.txt:20`).
2. "cannot distinguish a real mismatch from a transient tmux failure" (`glm/review-2.txt:21`).
3. same-key race: "two windows for one worker if `open` and the coordinator's step 5 both create its first tab" (`glm/review-2.txt:22`).
4. `_lock_held` probe: "holds `LOCK_EX` between flock and unlock; a coordinator starting inside that window fails its `LOCK_EX|LOCK_NB` (watch.py:865-867) and exits 0 silently" (`glm/review-2.txt:23`).
5. case (b): "when K was already in `dismissed0`, the pass overwrites open's cleared entry with `now` — inert while the tab lives" (`glm/review-2.txt:24`).
6. stale comment: "the experiment disproves that on tmux 3.8. Harmless belt-and-suspenders, but the comment states a false fact." (`glm/review-2.txt:25`).
7. `test_tmux_integration.py` "carries uncommitted working-tree modifications (docstring/imports) beyond HEAD" (`glm/review-2.txt:26`).

Round-1 item 1 withdrawn: "The split pane has no own `@hx`, so `_has_own_option` is false and `_killable` returns" (`glm/review-2.txt:28`). Runs: "All suite runs green: 19 (tab_merge+tabs), 33 integration, 106 transcript/render/lifecycle/existence, 33 races/concurrency/honesty/isolation" (`glm/review-2.txt:30`).

Prompt 1: eight invariants, questions (a)-(i), 900-word cap ("Does the wrapper shell break that identity check or `down`'s kill?", `glm/prompt-1.md:22`).

## (c) Deployment evidence (`ux-redesign/deploy/`)

Timestamps as printed inside.

- **pre-deploy.txt** — v2: four `hx:worker-N` windows, two panes each, no `agents`; header 19:22:42 (line 1), layout stamp `19:13:54` (line 32). `deploy/pre-deploy.txt:4` "3 @4 hx:worker-1 panes=2 active=0 @hx=1"
- **post-deploy.txt** — v3: window 3 is `agents`, `@hx=1`; header 19:23:03 (line 1), coordinator 28801 with `HERMES_TMUX_LINGER_S=600` (line 7). `deploy/post-deploy.txt:4` "3 @9 agents panes=1 active=0 @hx=1"
- **down-v2.txt** — v2 teardown; no internal timestamp. `deploy/down-v2.txt:1` "hermes-tmux: closed 8 pane(s) and 4 window(s)"
- **up-v3.txt** — v3 bring-up; no internal timestamp. `deploy/up-v3.txt:1` "hermes-tmux: workspace ready  (@9)"
- **live-running.txt** — run at 19:24:22 (line 1): `1 running`, worker tab 4 named from the goal. `deploy/live-running.txt:5` "4 @10 [Check workspace docs…] panes=1 active=0 glyph=#[push-default]#[fg=colour114]●#[default]#[pop-default]"
- **live-completed.txt** — done at 19:28:38 (line 1): tab 4 keeps its name, DONE glyph, `4m07s · 46 tools`, under RECENT. `deploy/live-completed.txt:11` "✓ Check the workspace docs for stale statements                                                        7s ago  tab 4"
- **debug-report-v3.txt** — runtime: session `$0`/`main`, overview `%23`, `workers_window` `@9`, stamp `19:23:57` (line 2). `deploy/debug-report-v3.txt:9` "watcher pid: 30862 (alive=True)"
- **rollback-rehearsal.txt** — drill 19:29:16, socket `hxlab`: v3 up/down, tag `v2-two-per-window` up, back to v3, user pane survives. `deploy/rollback-rehearsal.txt:1` "== rollback rehearsal 19:29:16 (private socket hxlab, session rb)"

## (d) Design record, `docs/DESIGN.md` section 5

Decisions, 14 rows from line 204:

- **Grouping** → one worker per window, one pane. `docs/DESIGN.md:204` "A tab's state is exactly one worker's state; the transcript gets the whole window;"
- **Role tabs** → rejected. `docs/DESIGN.md:205` "Role tabs would need a classifier reading prose, and a wrong label is worse than none."
- **One window per delegation** → rejected. `docs/DESIGN.md:206` "Three siblings either break the ceiling or get split anyway;"
- **Tab name** → first goal clause, articles dropped. `docs/DESIGN.md:207` "First clause of the goal the parent wrote; articles dropped, paths to basename"
- **Narrow clients** → fixed name, per-client truncation. `docs/DESIGN.md:208` "The name never changes; `window-status-format` truncates it per client from"
- **State in the tab** → glyph in `@hx_glyph`. `docs/DESIGN.md:209` "Glyph in window option `@hx_glyph`, placed before the name in a format derived from the user's own"
- **History** → live only; finished tabs linger. `docs/DESIGN.md:210` "Tabs only for live work; finished tabs linger, then leave; never while in view"
- **User closes a tab** → stays closed; `open <id>` restores it. `docs/DESIGN.md:211` "It stays closed; `open <id>` brings it back pinned"
- **Attention** → unseen failed run, <2 h. `docs/DESIGN.md:212` "A failed run nobody has looked at, ended since the workspace came up, less than two hours old"
- **Run vs task outcome** → run-ended reason + report. `docs/DESIGN.md:213` "Hermes records how the run stopped. It does not record whether the work is good, so the display does not say."
- **Overview** → RUNNING / NEEDS ATTENTION / RECENT. `docs/DESIGN.md:214` "Sections RUNNING / NEEDS ATTENTION / RECENT, task title first, details dimmer"
- **Transcript** → one terminal line per event. `docs/DESIGN.md:215` "each event ONE terminal line, soft-wrapped by the terminal"
- **Pane header** → three variants, one conditional. `docs/DESIGN.md:216` "Three variants inside one tmux conditional on `pane_width`"
- **Colour** → terminal fg, greys, 256-colour. `docs/DESIGN.md:217` "Legible on light and dark themes; no truecolor dependency"

Defects V1..V10:

- **V1** — tmux message in the reader's pane. `docs/DESIGN.md:232` "output goes to `watch.log`, the wrapper shell always ends 0"
- **V2** — delegation unnoticed up to 20 s. `docs/DESIGN.md:233` "any vnode event reloads; the signature is gone"
- **V3** — one-character orphan lines. `docs/DESIGN.md:234` "fitted lines keep a two-column margin"
- **V4** — preview plus the message. `docs/DESIGN.md:235` "flushed only after a quiet wait"
- **V5** — pre-workspace attention. `docs/DESIGN.md:236` "runs that ended before `created` are history"
- **V6** — header title unreadable. `docs/DESIGN.md:237` "inside a border, the bare `default` style is the border colour"
- **V7** — pass reverted `open`'s tab. `docs/DESIGN.md:238` "a first tab created by both sides is kept once"
- **V8** — `--no-follow` left no echo. `docs/DESIGN.md:239` "set only while following, restored on exit"
- **V9** — narrow pane cut the marker. `docs/DESIGN.md:240` "the marker is whole or a bare ellipsis"
- **V10** — fresh-profile delegation on slow wake. `docs/DESIGN.md:241` "its nearest existing ancestors are watched"

## (e) Constants

- **Linger** — 300 s. `hermes_tmux/watch.py:47` "#: How long a finished worker keeps its tab after it settles or was last"; `LINGER_S = 300.0` (`hermes_tmux/watch.py:50`), env override read at `hermes_tmux/watch.py:54`.
- **Tab cap** — 8 tabs. `hermes_tmux/watch.py:44` "#: full window size and makes a tab's state exactly one worker's state. Eight"; `MAX_TABS = 8` at `hermes_tmux/watch.py:46`.
- **Attention TTL** — 7200 s. `hermes_tmux/render.py:27` "#: A failure nobody looked at stops asking for attention after this long. An"; `ATTENTION_TTL_S = 2 * 3600` at `hermes_tmux/render.py:30`.
- **Stall threshold** — 600 s, display-only; `hermes_tmux/render.py:26`: "STALL_AFTER_S = 600  # 10 minutes; display-only, never a failure claim".
- **Max text width** — 118 columns. `hermes_tmux/render.py:33` "#: Text is easier to scan in a column than across a 188-column pane."; `MAX_CONTENT_WIDTH = 118` at `hermes_tmux/render.py:34`.
- **Tab-truncation rungs** — five, shares 11/14/18/24/34 → 5/8/12/18 columns then the full name `(34, "#W")`; below that, index plus glyph. `hermes_tmux/watch.py:518` "for share, name in ((11, cut(5)), (14, cut(8)), (18, cut(12)), (24, cut(18))"; `hermes_tmux/watch.py:514` "    # Below the last rung there is no room for a name at all: the tab is its".

## Not in sources

- `glm/review-2.txt:1-9` is diff/probe output before the verdict; provenance not stated in sources.
- `down-v2.txt`, `up-v3.txt` carry no internal timestamp; their wall-clock times are not stated in sources (filesystem mtimes 19:23 only).
- Counts are static (`def test_`, 4-space indent); pass/fail under a live run is not stated in sources.
