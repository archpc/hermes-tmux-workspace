# hermes-tmux — design decision record

Status: implemented and deployed. Sections 1 to 4 record the earlier versions; section 5 is the current design.
Date: 2026-11-12. Author: parent session (Sein), reviewed independently.

## 1. What was actually observable (evidence, not assumption)

Verified by reading the install at `~/.hermes/hermes-agent` @ `3b42157858`
(Hermes v0.23.1) and by inspecting live artifacts on disk.

| # | Surface | Path / API | Verified content |
|---|---|---|---|
| S1 | Per-child live transcript | `$HERMES_HOME/cache/delegation/live/<deleg_id>/task-<n>.log` | append-only, pre-headered at dispatch; lines `HH:MM:SS role | text`; roles `user,start,think,assistant,tool,result,final`; real tool names + arg previews + durations + ERROR status |
| S2 | Delegation manifest | `.../live/<deleg_id>/manifest.json` | `delegation_id, started, task_count, model, provider, completed`, `tasks[{index, goal, log, status, exit_reason, model}]`; status starts `running` |
| S3 | Delegation DB rows | `state.db` table `async_delegations` | `delegation_id PK, parent_session_id, state, dispatched_at, completed_at, event_json, result_json, task_json`; result_json carries per-task `model, status, exit_reason, tokens, cost_usd, tool_trace, live_transcript, summary` |
| S4 | TUI JSON-RPC | `subagent.list / subagent.tail / subagent.interrupt` (`tui_gateway/methods_subagents.py`) | tail reads the same S1 file; scoped to a live renderer transport, not callable from a third process |
| S5 | Hook bus | `hooks:` config → `invoke_hook(name)`; events incl. `on_session_open`, `pre/post_tool_call` | requires config.yaml edits + first-use interactive consent; no subagent-lifecycle event |
| S6 | tmux mechanics | probed live on tmux 3.8 | `#{session_id}` `$N`, `#{window_id}` `@N`, `#{pane_id}` `%N` all stable; window/pane user options `@x` readable via `#{@x}`; `run-shell -b` works; `select-layout tiled` OK |
| S7 | kqueue | `select.kqueue` on macOS APFS | dir vnode `NOTE_WRITE` fires on append-inside, file create, mkdir; file vnode `NOTE_EXTEND` fires on append; 0-timeout read returns `[]` when idle |

### Not observable — must not be invented

- **Review status.** Hermes has no concept of "reviewing a delegation". No file, row, or
  event expresses it. Therefore review status is only ever read from an explicit
  registry written by a reviewer run that actually executed. Absent a record, the
  display says `unreviewed` — it never infers review from a worker's own summary.
- **Live token/cost per worker.** `result_json` has these only after the task settles.
  Running workers show `-`.
- **Tool-level progress between transcript flushes.** The transcript is the source of
  truth; silence is not progress.

## 2. Chosen architecture

A **read-only side-car**. Hermes core is not modified, patched, or configured.

```
Hermes (unchanged)
  └─ writes S1 logs + S2 manifests + S3 rows    [append-only, already on]
        │
        ▼
hermes-tmux  (python3, stdlib only, single process per tmux session)
  ├─ state.py       S1+S2+S3+review registry → normalized Worker records
  ├─ labels.py      task titles, tab names, state glyphs (one vocabulary)
  ├─ render.py      Worker records → the agents overview (pure)
  ├─ transcript.py  one transcript → readable live view (presentation only)
  ├─ watch.py       kqueue over live root → tabs and redraw on change
  └─ tmuxio.py      thin tmux CLI wrapper; refuses to touch panes it did not create
        │
        ▼
tmux session (user's existing config untouched)
  agent window   (user's own; never renamed, never touched)
  agents         overview only (hermes-tmux follow)
  <task name>    one window per live worker: hermes-tmux tail task-N.log
  hx:logs        lazy, on demand
  hx:tests       lazy, on demand
  hx:debug       lazy, on demand
  hx:rea         lazy, on demand
```

Why this and not the alternatives:

- **Not a hook/plugin.** `hooks:` needs config.yaml edits and an interactive consent
  prompt, has no subagent-lifecycle event, and would put our failure modes inside the
  agent loop. S1–S3 already carry strictly more information than any hook would.
- **Not the TUI gateway API.** S4 is bound to a renderer transport; using it would mean
  faking a client and coupling to an internal protocol version.
- **Not `tmux capture-pane` scraping.** Fragile against reflow, colors, and user input.
  S1 is structured.
- **The transcript file as the only per-worker source.** A worker pane follows the
  real file (kqueue on the vnode with a 2 s bounded safety wait for rotation/pane
  liveness) and prints each event once. The styled
  follower changes presentation only (section 5); `HERMES_TMUX_TRANSCRIPT=raw` puts
  plain `tail -F` back.
- **kqueue for the overview.** stdlib, event-driven, no daemon dependency, no `fswatch`.

### Event coverage actually achieved

| Event | Source | Latency | Mechanism |
|---|---|---|---|
| delegation starts | dir create under `live/` | immediate | dir `NOTE_WRITE` |
| task starts (header written) | file create | immediate | dir `NOTE_WRITE` |
| tool call / tool result / thinking / assistant text | appended line | immediate | file `NOTE_EXTEND` → redraw; the pane's follower for the transcript |
| task finishes | `final` marker in log; manifest status flip | immediate | file `NOTE_EXTEND` |
| delegation completes | `manifest.completed` | immediate | dir `NOTE_WRITE` |
| DB terminal state | `async_delegations.state` | immediate (1 s floor) | `state.db`/`-wal` `NOTE_WRITE` → redraw |
| review recorded | registry file write | immediate | dir `NOTE_WRITE` |
| Hermes crashes mid-delegation | no further events | n/a | UI keeps last phase, marks the worker silent after 10 minutes without a transcript event (`STALL_AFTER_S`) |

`stalled` is a display-only derived label with the rule stated in the pane footer. It is
not a claim that the task failed.

### Isolation, stability, reconnect

- State root: `$HERMES_HOME/cache/hermes-tmux/sessions/<sockid>-<sanitised_session_id>/`
  where `sockid` is the first 8 hex of sha1 of `socket_path|server_pid` and the
  session id's non-alphanumerics become `_` (`$0` → `_0`). The **server pid** is part
  of the digest because tmux restarts session ids at `$0`: without it, a new server
  inherits the previous incarnation's layout (and its dead pane ids), which is exactly
  the stale-state failure described in section 4. Two servers, or two sessions on one
  server, never share a layout file or a watcher.
- Every pane we create gets pane option `@hx=1` and its `%id` recorded in `layout.json`.
  We only ever act on ids carrying `@hx=1`; anything else is invisible to us.
- `up` is idempotent: re-reads `layout.json`, drops dead pane ids, recreates what is
  missing, restarts `follow` only if its recorded pid is dead.
- `watch` holds an exclusive `flock` on the session lock, so a double `up` cannot
  start two coordinators. `follow` does not take the lock: there is one per overview
  pane and it owns no shared state.
- Detach/reattach: panes, their followers, and the watcher are children of the tmux server and
  survive detach. `up` after reattach is a no-op.

### Gate

`HERMES_TMUX=0` disables everything (all commands become no-ops or read-only status).
The default is **on** (it was off until the suite passed end to end); `HERMES_TMUX=0` opts
one shell out. The default lives in
`hermes_tmux/gate.py:DEFAULT_ENABLED`, one boolean, and the flip is covered by a test.

## 3. Defects found while validating (all fixed, each with a test)

The recovered implementation looked complete and its unit tests passed, but the
integration suite had never run to completion. Running it on an isolated socket
exposed the following, in the order they blocked progress:

| # | Symptom | Cause | Fix |
|---|---|---|---|
| D1 | integration suite hung, then failed every test | harness targeted `session:0`, but the user's `tmux.conf` sets `base-index 1` | harness addresses panes by id and runs with `-f /dev/null` |
| D2 | every `up` exited with a traceback | `watch.state_root()` called `hermes_home(home)` while `state.hermes_home()` took no argument | `hermes_home(home=None)` |
| D3 | no worker panes ever appeared | `reconcile_panes` read `worker._log_path`, an attribute nothing set | `Worker.log_path`, populated from the manifest's log path |
| D4 | the coordinator reconciled nothing | started via `run-shell`, which has no `TMUX_PANE`, so it derived a different session key | `up` passes `--key` explicitly |
| D5 | worker panes unbuilt for up to 60 s | the loop waited for a vnode event before its first pass | reconcile before the first `kqueue` wait |
| D6 | `down` never closed anything; foreign-pane guard "passed" for the wrong reason | ownership marker read as `{@hx}` (literal) instead of `#{@hx}` | `_SENTINEL_FMT = "#{@hx}"` |
| D7 | `down` was a no-op after the badge renamed the window | name lookup was exact-match only | suffix-tolerant lookup restricted to windows we own |
| D8 | a pane could be repointed at a different worker mid-run | `reconcile_panes` assigned panes by slot order | one pane per worker key; existing panes are never repointed |
| D9 | aux/worker panes read the wrong profile and failed to import the package | new windows are forked from the *server* env, not the caller's pane | pass `HERMES_HOME`/`HERMES_TMUX`/`PYTHONPATH`/`HX_PYTHON` with `new-window -e` |
| D10 | a new session adopted a dead session's layout | state key = socket + session id, and ids restart at `$0` per server | server pid included in the key digest |
| D11 | `up` intermittently reported "hx:workers has no panes" | `new-window` returns before the pane exists in the pane table | `wait_for_panes()` polls briefly |
| D12 | a live pane was signalled by `down` | the watcher pid file was trusted blindly; pids get recycled | verify the target's command line (`hermes_tmux … watch`) before any `kill` |
| D13 | running workers displayed as `stall` | stall used the start timestamp only; no last-event time was exposed | `Worker.last_event` from the transcript's last line |
| D14 | a worker that recovered still showed an error marker | any historical `ERROR` line set the marker | `error_pending` reflects only the latest completed call |
| D15 | one idle coordinator process accumulated per closed session (93 were found running) | `run-shell -b` detaches the child from the server's lifetime and the loop never checked | the loop probes session liveness every `_SESSION_CHECK_S` and exits |
| D16 | `up` could claim and respawn the first pane of a reused window without proving it was ours | the window id from the layout was trusted, and the first pane was marked unconditionally | window ownership is verified (`owns_window`) before any pane is marked or respawned |
| D17 | `review run` prompted the reviewer with `(unknown)` as the transcript and still recorded a verdict | it read `_log_path`, an attribute removed when the public `log_path` field landed | reads `log_path` and refuses to run, or record anything, when no transcript exists |
| D18 | a pane option can be inherited by a user split inside our window, so `@hx` alone could authorise a kill | the guard trusted the marker only | a kill also requires provenance: our tail/python foreground, or a non-empty `pane_start_command` (our panes always have one; an interactive split never does) |
| D19 | every pane in a marked window read as ours, so `up` could respawn a user's shell that happened to sit in the first slot | ownership was read with the `#{@hx}` format, which falls back pane → window → session | ownership is read with `show-options -p` / `-w`: only the pane's or window's *own* option counts |
| D20 | `down` could destroy a window that still contained a user pane | it closed the window as a unit once its own panes were gone | panes are closed one at a time; a window that still holds any pane we do not own is left standing and reported |
| D21 | a lost overview pane let `up` repurpose the first surviving pane | the replacement search accepted any pane we owned | the overview is rebuilt as a NEW pane unless the window was created by this same call |
| D22 | verdict parsing recorded PASS for reviews that said FAIL ("Verdict (PASS or FAIL): FAIL", "Overall: FAIL", "this is not a pass", a `## Verdict` heading) | only `VERDICT: X` was recognised, then the earliest token won | labelled verdicts take the nearest non-enumerated token; a label with only an enumeration states nothing |
| D23 | the ownership guard could regress to the inherited marker read without any test noticing | no test asserted that a marked window does not make its panes ours | `test_window_marker_does_not_make_its_panes_ours` pins the pane-local read |
| D24 | `down` reported "closed 0 window(s)" after closing them | windows are destroyed when their last pane goes, so the window close reported "gone" | it reports panes and windows separately |
| D25 | stale-pane recovery, window accounting and the suite's socket cleanup were all silently disabled | `pane_exists`/`window_exists` tested the exit status of `display-message`, which tmux 3.8 returns as 0 for a missing target, so every id "existed" | existence means the resolved `#{pane_id}`/`#{window_id}` equals the id asked for; the socket file is computed from `TMUX_TMPDIR` instead of being probed from a dead server |
| D26 | `can't pass` recorded PASS | typographic apostrophes were deleted before negation matching, turning `CAN'T` into `CANT` | `'` variants are normalised to ASCII, not dropped |
| D27 | the overview lagged a lifecycle change by up to 20 s | only the delegation directory and its `*.log` files were watched; a kqueue vnode on a directory fires for create/delete, not for a write to a child, and the manifest is rewritten in place | each `manifest.json` is watched explicitly; measured latency dropped from 16 s to 1 s |
| D29 | `logs` right after `up` lost the `tail_panes` record and produced a second, untracked tail pane | every writer read layout.json at entry and wrote the whole document back, so a mid-flight writer reverted another's update | layout updates go through `update_layout`, a locked read-modify-write; `up`, every aux command and reconciliation use it (proved by an 8-process lost-update test) |
| D30 | a coordinator outlived the session it served | `session_alive` used the exit status of `display-message -t <missing>`, which is 0 on tmux 3.8 | it requires the resolved `#{session_id}` to match, so a closed session ends its coordinator while the server keeps running |
| D32 | a coordinator kept serving a *replaced* server after a crash | identity was the socket path, which the new server inherits | the coordinator records the server pid at start and exits when a different pid answers the socket |
| D31 | reconciliation could adopt a user's window after a server restart | `reconcile_panes` checked `window_exists`, never `owns_window`, and a window id is only unique per server | reconciliation and the badge rename require `owns_window` |
| D28 | a `follow` loop survived the death of its server | the pane-side loop redrew and waited forever and never asked whether its pane still existed; tmux does not reliably deliver SIGHUP when the server is killed outright | the loop returns as soon as `pane_exists(pane_id)` is false (observed orphan: 7.5 min after its server went) |

### Reviewed fixes (independent GLM pass)

A GLM review of the first diff returned FAIL. One finding (B1: adopting a
user window named `hx:workers`) was already fixed before the review ran and is
covered by `test_foreign_window_with_our_name_is_never_adopted`; the rest were
real and are fixed here:

- watcher identity was two loose substrings; a spoofed command line could be
  signalled. It now requires the pid that holds the session's `flock` (via
  `lsof`) *and* the exact `-m hermes_tmux watch --key <key>` sequence.
- `tests.log` had no writer. `hermes-tmux tests --run` is the writer; the pane
  never executes the suite by itself.
- `follow` blocked forever and froze if the coordinator died. The wait is
  bounded and the pane degrades to periodic re-reads.
- reconcile's pane-reuse branch was unreachable; orphan panes are now reused
  before being closed.
- verdict extraction took the earliest token, so "does not pass" recorded PASS.
- a reader exception rendered as "no delegations found"; it now says what broke.
- aux panes used zsh-only `print`; they use `printf`. Pane ordering was string
  based (`%10` before `%2`); it is numeric. A midnight-crossing timestamp no
  longer marks a healthy worker stalled.

## 4. Out of scope (deliberate)

- No writes to Hermes state, logs, or config.
- No launching of subagents from the tmux layer.
- This tool never edits `tmux.conf` and sets no global tmux options: the only options
  it writes are on windows and panes it created (`@hx`, `@hx_glyph`, `@hx_head`,
  `pane-border-*`, and a `window-status-format` derived from the user's own). One
  exception since the close mark (section 6): the server's `MouseDown1Status` binding
  is wrapped, with the previous binding kept as its else-branch and recorded in the
  server option `@hx_prev_mouse1` so it can be put back exactly. (The
  user's `tmux.conf` was hardened separately during the shell-integration audit; that
  work is described in `docs/NOTES-tmux.md`, not done by the CLI.)
- No killing of pre-existing sessions, windows, or panes.
- No daemon outside the tmux server; nothing survives the server's death.

## 5. Version 3: one worker per tab (UX redesign)

Version 2 put the overview in `hx:workers` and two transcripts in each
`hx:worker-N`. Rendered against a real day of work it showed four tabs of
transcripts up to nine hours old, numeric tab names, a seven-column table keyed
by delegation id, and a failure badge that could never clear. The redesign
started from those captures, not from the layout.

### Decisions

| Question | Decision | Why |
|---|---|---|
| Grouping | One worker per window, one pane | A tab's state is exactly one worker's state; the transcript gets the whole window; slot packing, tiled reflow and "which pane" disappear. One pane per window, so the earlier two-per-window ceiling no longer applies. |
| Role tabs (Protocol / Implementation / Review) | Rejected | Hermes records no role for a task. Role tabs would need a classifier reading prose, and a wrong label is worse than none. |
| One window per delegation | Rejected | Three siblings either break the ceiling or get split anyway; the tab becomes an aggregate. |
| Tab name | First clause of the goal the parent wrote; articles dropped, paths to basename, words shared by all siblings dropped | The parent already named the task. Stable: decided once, the sibling set is fixed at dispatch. |
| Narrow clients | The name never changes; `window-status-format` truncates it per client from `client_width` and `session_windows` | Two clients of different width each get a bar that fits; resizing renames nothing and costs the coordinator nothing. |
| State in the tab | Glyph in window option `@hx_glyph`, placed before the name in a format derived from the user's own | No rename churn; follows the user's theme. |
| History | Tabs only for live work; finished tabs linger, then leave; never while in view | The tab bar shows what is happening now. The overview keeps the record. |
| User closes a tab | It stays closed; `open <id>` brings it back pinned | Version 2 recreated it, which is the tool overriding the reader. |
| Attention | A failed run nobody has looked at, ended since the workspace came up, less than two hours old | An alert that cannot clear is not an alert. Looking at the tab is the acknowledgement; no command. |
| Run outcome vs task outcome | "run ended: <reason>" plus the worker's own last report, quoted | Hermes records how the run stopped. It does not record whether the work is good, so the display does not say. |
| Overview | Sections RUNNING / NEEDS ATTENTION / RECENT, task title first, details dimmer | Answers "what is happening" before "what happened". |
| Transcript | Styled follower; each event ONE terminal line, soft-wrapped by the terminal | tmux re-wraps soft-wrapped lines on resize, so the reader's scroll position is never touched and nothing is replayed. A hanging indent was given up for that. |
| Pane header | Three variants inside one tmux conditional on `pane_width` | tmux picks; nothing reacts to a resize. |
| Colour | Terminal foreground for primary text, mid greys for secondary, 256-colour accents | Legible on light and dark themes; no truecolor dependency. |

### Presentation-only guarantee for transcripts

The follower reads the file Hermes wrote and nothing else. It prints the path
first. It never drops an event: the two collapses are exact duplicates the
writer itself produces (the `think` preview of the `assistant` line that
follows it; the `start` copy of the kickoff goal), and the fixed six-line file
header that the pane header repeats. Anything that is not an event is printed
as written. Clipping is marked `(+N chars)`.

### Defects found while building it

| # | Symptom | Cause | Fix |
|---|---|---|---|
| V1 | `down` left a tmux message in the reader's own pane (view mode) | `run-shell -b` reports a job that ends on a signal in the active pane; the coordinator was killed with SIGTERM | output goes to `watch.log`, the wrapper shell always ends 0, SIGTERM exits cleanly |
| V2 | a new delegation was not noticed for up to 20 s | a "nothing changed" signature covered only files already known, and a new directory is not one | any vnode event reloads; the signature is gone |
| V3 | one-character orphan lines in transcripts | a pane is created a column wider than the client that later shows it | fitted lines keep a two-column margin |
| V4 | previews duplicated the message that followed | the held `think` line was flushed before waiting for the next line | flushed only after a quiet wait |
| V5 | failures from before the workspace started asked for attention with no tab to acknowledge them | attention ignored when the workspace came up | runs that ended before `created` are history |
| V6 | pane header title unreadable | inside a border, the bare `default` style is the border colour | explicit styles |
| V7 | the coordinator reverted a tab record that `open` published during its pass, then opened a second window for that worker | it wrote the whole `tabs` map back after seconds of tmux work | the pass snapshots what it read and merges only its own changes (`watch._merge`); a first tab created by both sides is kept once |
| V8 | `tail --no-follow` run by hand left the reader's shell without echo | the view pane's no-echo setting was never restored | set only while following, restored on exit |
| V9 | a fitted transcript line could overflow a narrow pane and cut its own `(+N chars)` marker | a minimum room larger than the pane | clip never exceeds the room; the marker is whole or a bare ellipsis |
| V10 | the first delegation on a fresh profile was noticed only on the slow wake | the live root did not exist yet, so nothing watched it | its nearest existing ancestors are watched |

### Independent review (GLM, two rounds)

Round 1 returned FAIL on one blocker, V7. Round 2 returned PASS with no
blockers after tracing the merge through four interleavings, and withdrew a
round-1 claim by experiment on a private socket: a pane a user splits inside
one of our windows does not carry `@hx` of its own (`show-options -p` says
`invalid option`), so `_killable` refuses it before any command check. Its
remaining notes were taken: ownership "no" answers are asked twice and an
unanswered read leaves the tab alone for that pass, so one failed tmux call
cannot hide live work; a pane the reader moved into another window with
`join-pane` is forgotten and nothing is written to that window; `down` also
nominates recorded window ids and keeps the state directory while a
coordinator still holds the lock.

Known limits, accepted: emoji variation-selector clusters are measured by East
Asian / combining code-point width rather than full grapheme-cluster shaping; if
the kernel watch on a transcript cannot be set up, a `think` preview is shown as
well as the message it previews; with several clients of different sizes
attached, tmux sizes each window for the client that used it last.

## 6. Close mark on the tab in view

A dim `×` at the end of the worker tab in view. Clicking it closes the view.

| Question | Decision | Why |
|---|---|---|
| What it closes | The pane that follows the transcript, by the same ownership proof as everything else (`tmuxio.close_pane`) | The worker is a Hermes subagent in another process. It keeps running, stays in the overview, and `open <id>` brings the tab back. This is the existing "a tab the reader closed stays closed" rule, reachable by mouse. |
| Cancel | Not offered | This tool has no cancel API (S4 is bound to the TUI transport). A mark that looked like "stop" would lie. The message after a close says "worker still running" when it is. |
| Which tabs | Worker tabs only, and only the one in view (`window-status-current-format`) | Compared with a mark on every tab: four more crosses beside the failed-state glyph, and a slip while aiming at a tab name would close instead of select. You can only close what you are looking at. Cost: closing a background tab is two clicks. |
| Which window a click names | The window id travels in the range argument (`range=user\|hxc@N`) | For a user range tmux's mouse target `=` is the current window, not the clicked one (measured). |
| What a close may touch | A window recorded as a worker tab in a layout of this server, then each pane on its own proof | A forged or stale target cannot close the reader's windows, the overview, or an aux pane. A pane the reader split into a tab stays. |
| The binding | `MouseDown1Status` wrapped in `if -F '#{m:hxc@*,#{mouse_status_range}}'`; previous binding verbatim as the else-branch; restored by `close-mark off` and by the last `down` on the server | tmux gives a click a behaviour only through a key binding, and bindings are per server. Every click that is not on a mark behaves exactly as before. |
| How the handler runs | `run-shell -b`, silent, always exit 0 | tmux prints a run-shell job's output, or a line about a non-zero exit, into the active pane in view mode: the reader's own pane. |
| Opt-out | `HERMES_TMUX_CLOSE_MARK=0` | Leaves the binding and the formats alone. |

## 7. Workspace identity is not session identity

Found while deploying the close mark: with a grouped session present (`tmux new-session -t main`),
`up` or `open` from a shell could build a second workspace beside the first: second state
directory, second overview, second coordinator.

Root cause. The key was `server digest + session id`, and the session id came from
`display-message -t $TMUX_PANE '#{session_id}'`. Sessions of one group share every window, so the
pane is in all of them, and tmux resolves it to the session with the latest activity. The answer
changes when a second terminal is used, or merely when a grouped session is created. That answer
is right for focus (it is where the reader is) and wrong for identity.

| Question | Decision | Why |
|---|---|---|
| What identifies a workspace | Its own overview window carries the key (`@hx_key`), believed only on a window with our marker, read as the window's own option, and only for this server instance | Windows are exactly what a group shares. Every session of the group resolves the same key, and the key survives any one session ending. |
| Before a workspace exists, or one built before the marker | Lowest session id of the group | That is the original session. For an ungrouped session it is the session itself, so existing keys are found unchanged and nothing needs migrating. |
| Which session the coordinator is tied to | The recorded one while it lives; otherwise the oldest of the group. Never the session tmux resolved the pane to | A view coming and going must not stop the coordinator. If the anchor session itself ends, the coordinator stops as before, and the next `up` from a surviving session re-anchors the same workspace. |
| In view | The current window of every attached session of the group | A tab on screen in a second terminal is being looked at, whatever the anchor session shows. |
| Focus (`open`, `worker N`, aux) | Unchanged: tmux picks the most recently active session | That is the terminal the command was typed in. |
| Concurrent `up` | One at a time under a lock file | Independent of groups: six concurrent `up` calls built six overview windows. Found by the same test run. |

Not changed: ownership proofs, pane closing, `down`, coordinator exit rules, key format.

Known limits. If the overview window is closed by hand and the original session is also gone, the
marker is lost and a later `up` keys a new workspace; the old tabs are still ours and `down`
closes them. Linking the overview window into an unrelated session (`link-window`) makes that
session resolve to this workspace; its anchor is left where it was.
