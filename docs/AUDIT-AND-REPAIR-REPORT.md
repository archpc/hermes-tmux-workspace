# Hermes Ecosystem Adversarial Audit & Repair Report (2026-11-13 / 2026-11-14)

## 1. Executive Verdict

`hermes-tmux-workspace` v3 and `hermes-agent` (`tui/compact-footer`) have a sound architectural boundary: read-only artifact observation (`manifest.json`, `task-<n>.log`, `state.db`, `reviews.json`), pane/window ownership proofs (`@hx=1` + process command guard), and strict terminal escape defusal on subagent transcripts.

Under adversarial testing and production inspection on macOS 27.2.1 (Apple M5 Pro, tmux 3.8, Xcode Python 3.9.6), however, **seven confirmed correctness/performance defects** in `hermes-tmux-workspace` and **three confirmed defects** in `hermes-agent-tui-ux` were reproduced, root-caused, repaired with failing-first regression tests, benchmarked before/after on identical workloads, and verified across full suites:

1. **Severe CPU & FD waste during normal Hermes chat (`P1`, fixed)**: `watch` and `follow` burned **48.11% combined CPU** (`21.42%` `watch` + `26.69%` `follow`) and held **476 open file descriptors** during ordinary 20 Hz parent SQLite WAL writes (`messages`/`sessions`) with **zero subagents running**. Repaired combined CPU on the identical Python 3.9.6 workload is **0.33%** (**145x reduction**; `follow` **0.00%**), and open descriptors dropped to **257** (**-46%**).
2. **Multi-task & independent-unit delegation state corruption (`P1`, fixed)**: Hermes sets `async_delegations.state = 'completed'` when a multi-task batch finishes delivering mixed per-task outcomes (`interrupted`, `completed`, `error`). `_resolve_state` unconditionally overwrote failed/interrupted sibling tasks to `completed`, suppressing `NEEDS ATTENTION` and retiring failed tabs unacknowledged. Suffixed `independent_completions` unit rows (`<delegation_id>-<k>`) were also missed.
3. **Long-running worker eviction & broken `--all` / `open` / `review` past 40 workers (`P1`, fixed)**: `load_manifests(limit=40)` sliced by `started` timestamp before checking running state, dropping live running workers older than 40 subsequent short delegations; `status --all`, `open`, and `review` also capped at 40 workers instead of inspecting all recorded delegations.
4. **kqueue `WatchSet.sync` stale inode leak after atomic replace & unordered 256-FD truncation (`P1`, fixed)**: `WatchSet.sync` keyed `self.files` by path string without checking `(st_dev, st_ino)`. After `os.replace(tmp, path)` (or log rotation), kqueue continued watching the unlinked inode and fired **0 events** on subsequent writes. Converting `files` into an unordered `set` also randomly evicted high-priority files (`state.db`, `state.db-wal`) past `_MAX_WATCH_FILES = 256`.
5. **Close-mark (`×`) multi-profile failure, dead mark on `open`-before-`up`, and premature server-wide `down` unbind (`P1`, fixed)**: Server-wide `MouseDown1Status` hardcoded the first session's `HERMES_HOME`, breaking clicks on other profiles sharing the tmux server; `open` before `up` rendered a dead `×` with no key binding; `down` in one profile uninstalled the server binding while other profiles or `open`-only tabs were still live; `without_mark` mutated formats lacking a trailing space.
6. **Display-column width overflow on CJK/wide text (`P2`, fixed)** and **outside-tmux `rerender` orphan directory (`P2`, fixed)**: `len()` code-point width allowed a 60-column CJK overview to emit 76 terminal columns, soft-wrapping lines inside `watch._draw` and scrolling the fixed-height `AGENTS` header off the top of the pane. Running `hermes-tmux rerender` outside tmux created an orphan `sessions/nosock-unknown/` directory (confirmed present on live Honk).
7. **`hermes-agent-tui-ux` escape injection in `reasoning_text_line`, `NO_COLOR` bypass, `/title` prefix collision, and scalar `display` crash (`P1/P2`, fixed)**: Raw OSC 52 / CSI / bidi sequences in streamed reasoning lines reached terminal scrollback unsanitized; `NO_COLOR` was ignored; `_repeats_goal` hid distinct manual `/title`s sharing a 24-character prefix with the active goal; scalar `display:` config raised `AttributeError` in `layout_from_config` and `busy_input_mode_from_config`.

---

## 2. Architecture Map, Trust Boundaries, and State Ownership

| Boundary / Component | Owner / Writer | Reader(s) | Trust Level & Invariants |
|---|---|---|---|
| `<HERMES_HOME>/cache/delegation/live/<did>/manifest.json` | Hermes parent (`delegation_live_log.py`) | `hermes_tmux.state`, `watch` | Structured local JSON; rewritten in-place or via temp replace; partial reads tolerated (`{}`). |
| `<HERMES_HOME>/cache/delegation/live/<did>/task-<n>.log` | Hermes child callback (`delegation_live_log.py`) | `hermes_tmux.state`, `transcript.run_tail` | **Untrusted content** (tool outputs, scraped web pages, model prose); must pass `labels.defuse()` before any terminal write. |
| `<HERMES_HOME>/state.db` (`async_delegations` + `-wal`) | Hermes gateway / CLI (`async_delegation.py`) | `hermes_tmux.state.db_delegations` | Opened strictly `?mode=ro` (`timeout=0.5s`); WAL fires on **all** parent tables (`messages`, `sessions`, `async_delegations`). |
| `<HERMES_HOME>/cache/hermes-tmux/reviews.json` | `hermes-tmux review` (`cli.py`) | `hermes_tmux.state` | Advisory verdict registry; role `reviewer` comes strictly from `reviewer_delegation`, never goal text. |
| `<HERMES_HOME>/cache/hermes-tmux/sessions/<key>/` | `watch`, `up`, `open`, `close`, aux (`_layout_lock`) | `watch`, `follow`, `status`, `close` | Per-workspace `layout.json` (guarded by `.layout.lock`), `render.txt`, `watch.lock` (`flock` identity proof), `watch.pid`. |
| tmux windows & panes (`@hx=1`, `@hx_key`, `@hx_home`) | `hermes_tmux.tmuxio`, `watch`, `cli` | `tmuxio`, `cli`, `watch` | `close_pane` requires `@hx=1` on the pane itself plus allowed command (`python`/`tail`/shell), never touching user-split panes. |
| tmux root `MouseDown1Status` & `@hx_prev_mouse1` | `closemark.install` / `restore` (`_server_lock`) | tmux server on status click | Single server-scoped binding; delegates non-`hxc@*` clicks verbatim to `@hx_prev_mouse1`; restored when last `@hx=1` window leaves server. |
| Hermes REPL scrollback & prompt_toolkit chrome | `hermes_cli/cli_modern_layout.py`, `cli_subagent_monitor.py`, `cli_session_dock.py` | User terminal emulator | Streamed model/reasoning text is untrusted for raw ESC/OSC/bidi sequences; live bottom chrome redraws via `prompt_toolkit`. |

---

## 3. Prioritized Verified Findings, Root Causes, and Repairs

### `hermes-tmux-workspace`

| ID | Sev | Location | Observed vs Expected Behavior | Root Cause | Repair & Regression Test |
|---|---|---|---|---|---|
| **HX-1** | **P1** | `hermes_tmux/watch.py:949-1100`, `hermes_tmux/state.py:174-271` | **Observed**: 20 Hz parent SQLite WAL writes (`messages` table, 0 subagents) burned **48.11% CPU** (`21.42%` `watch` + `26.69%` `follow`) and held **476 FDs** (live Honk: 17m CPU, 186+186 FDs). **Expected**: <1% CPU when `async_delegations` is unchanged. | (1) `watcher.wait(remaining)` woke immediately on `state.db-wal` instead of enforcing `_MIN_RENDER_INTERVAL_S=1.0s`; (2) `follow` watched `state.db-wal` unthrottled alongside `watch`; (3) `parse_log`/`read_manifest` had no stat cache and `run_watch` called `load_workers` twice per pass; (4) `collect_paths` opened kqueue FDs on all historical `.log` files. | Monotonic 1 Hz sleep floor + `db_only_fds` `async_delegations` equality fast-path in `run_watch`; skip DB watch in `follow` while `coordinator_alive`; stat-keyed `_LOG_CACHE`/`_MANIFEST_CACHE`; limit completed log FDs to 8 newest (`RECENT_ROWS`). Tested in `tests/test_carbon_watch_and_perf.py` & `tests/test_carbon_state_and_cli.py`. |
| **HX-2** | **P1** | `hermes_tmux/state.py:489-507,649,682` | **Observed**: In a 3-task batch (`interrupted`, `completed`, `error`) with DB `state='completed'`, all 3 workers showed `completed` (`attention_count=0` instead of `2`); suffixed unit rows (`deleg_*-1`) stayed `running` instead of `error`. **Expected**: Per-task verdicts preserved. | `_resolve_state` applied batch `db_row['state']` unconditionally across all tasks in a multi-task manifest, ignored `result_json.results`, and looked up only exact `db_rows.get(delegation_id)`. | Added `_db_row_for_task` (matching suffixed unit rows via `task_json.task_indexes`) and `_db_task_verdict` (`result_json.results`), and prevented multi-task batch `completed` from masking per-task terminal non-success in `manifest.json`. Tested in `tests/test_carbon_state_and_cli.py`. |
| **HX-3** | **P1** | `hermes_tmux/state.py:274,587`, `hermes_tmux/cli.py:401,646,950` | **Observed**: A still-running morning worker disappeared (`oldest_running_worker_visible=False`) once 42 fast delegations ran; `status --all` returned 40 of 85 workers; `open` and `review` failed (`rc=2`) on >40th worker. **Expected**: Live workers never dropped; `--all`/`open`/`review` see full history. | `load_manifests(limit=40)` sliced `found[:40]` by `started` timestamp before sorting live tasks first; `cmd_status --all`, `cmd_open`, and `cmd_review` omitted `limit=-1`. | Sort manifests with `_manifest_has_live_task` ahead of completed manifests before slicing; pass `limit=-1` in `cmd_status(--all)`, `cmd_open`, and `cmd_review`. Tested in `tests/test_carbon_state_and_cli.py`. |
| **HX-4** | **P1** | `hermes_tmux/watch.py:219-254` | **Observed**: After `os.replace(tmp, path)`, `WatchSet.sync` kept `old_ino=27506987` instead of `new_ino=27506988` (`0` kqueue events on subsequent appends); at 282 files, unordered `set` dropped `f_0001.txt`. **Expected**: Replaced inodes reopened; priority head preserved. | `WatchSet.sync` checked `if path in self.files: continue` without comparing `(st_dev, st_ino)`, and converted `files` into an unordered `set`. | Track `_dir_inos` and `_file_inos` in `WatchSet`, reopen when `(st_dev, st_ino)` changes, preserve order via `dict.fromkeys`, and place `state.db`/`-wal` + `reviews.json` first in `collect_paths`. Tested in `tests/test_carbon_watch_and_perf.py`. |
| **HX-5** | **P1** | `hermes_tmux/closemark.py:62-80`, `hermes_tmux/cli.py:198,383,679,725-793`, `hermes_tmux/watch.py:618` | **Observed**: Clicking `×` on Profile B (`HERMES_HOME=homeB`) failed (`tab_closed_on_click=False`); `open` before `up` drew a dead `×`; `down` in Profile A removed server binding while Profile B lived; `without_mark` added `" "` to no-space format. **Expected**: Cross-profile click & teardown isolation; lossless format round-trip. | `_tab_of_window` searched only current `$HERMES_HOME`; `cmd_open` skipped `closemark.install`; `cmd_down` checked only current-profile watchers; `without_mark` unconditionally appended `" "`. | Stamp `@hx_home` on decorated windows and resolve it in `_tab_of_window`; install binding in `cmd_open`; check server-wide `@hx=1` windows in `_any_workspace_on_server` under socket-keyed `_server_lock`; preserve trailing-space presence in `with_mark`/`without_mark`. Tested in `tests/test_carbon_closemark_profiles.py`. |
| **HX-6** | **P2** | `hermes_tmux/labels.py:129`, `render.py:314-355`, `transcript.py:176`, `watch.py:545` | **Observed**: `render(width=60)` with CJK goal/call emitted `76` terminal columns, wrapping lines in `watch._draw` and scrolling the `AGENTS` title row off screen. **Expected**: Every line `<= width` terminal cells. | Widths were counted with `len()` (code points) rather than East Asian / combining cell widths. | Added `labels.cell_width` and `labels.take_cells` (`isascii()` fast-path + `unicodedata`) across `labels.clip`, `render._Pen`, `transcript.Formatter._fit`, and `watch.pane_header`. Tested in `tests/test_carbon_unicode_and_escapes.py`. |
| **HX-7** | **P2** | `hermes_tmux/cli.py:432-437` | **Observed**: `hermes-tmux rerender` outside tmux created `cache/hermes-tmux/sessions/nosock-unknown/render.txt` (also observed on live Honk). **Expected**: Render to stdout without creating bogus state dir. | `cmd_rerender` called `render_and_publish(session_key())` without checking `tmuxio.in_tmux()`. | Guarded `cmd_rerender` when `not tmuxio.in_tmux()` to render directly to stdout. Tested in `tests/test_carbon_state_and_cli.py`. |

### `hermes-agent-tui-ux` (`tui/compact-footer`)

| ID | Sev | Location | Observed vs Expected Behavior | Root Cause | Repair & Regression Test |
|---|---|---|---|---|---|
| **TUI-1** | **P1/P2** | `hermes_cli/cli_modern_layout.py:116-129` | **Observed**: Raw OSC 52 (`\x1b]52;c;...`), CSI (`\x1b[2J`), and bidi overrides (`\u202e`) passed straight through `reasoning_text_line()` into terminal scrollback; `NO_COLOR=1` still emitted `\x1b[38;2;...m`. **Expected**: Escapes/controls defused; `NO_COLOR`/`TERM=dumb` plain. | `reasoning_text_line` wrapped raw `line` in SGR without control/escape filtering or `NO_COLOR` check. | Added `_defuse_terminal_text` and `_plain_output()` (`NO_COLOR` / `TERM=dumb`) to `reasoning_text_line` and `thought_line`. Tested in `tests/hermes_cli/test_cli_modern_layout.py`. |
| **TUI-2** | **P2** | `hermes_cli/cli_modern_layout.py:276-286` | **Observed**: Explicit `/title Audit hermes-tmux-workspace Phase 2` was hidden whenever active goal was `Audit hermes-tmux-workspace performance and watchers`. **Expected**: Distinct manual titles remain visible; clipped auto-titles collapse. | `_repeats_goal` truncated comparison to `head = 24` chars (`title[:24] == goal_text[:24]`) instead of checking true prefix equivalence after stripping trailing ellipses. | Strip trailing `.… ` and check `goal_text.startswith(title) or title.startswith(goal_text)` with `min_len >= 12`. Tested in `tests/hermes_cli/test_cli_modern_layout.py`. |
| **TUI-3** | **P2** | `hermes_cli/cli_modern_layout.py:32-40` | **Observed**: Scalar `display: "compact"` raised `AttributeError: 'str' object has no attribute 'get'` in `layout_from_config` and `busy_input_mode_from_config`. **Expected**: Safe fallback to defaults (matching `dock_mode_from_config`). | `(display or {}).get(...)` assumed truthy `display` was a `dict`. | Guarded both with `display if isinstance(display, dict) else {}`. Tested in `tests/hermes_cli/test_cli_modern_layout.py`. |

---

## 4. Falsified Hypotheses & Reference-Only Findings

- **Falsified (`T2` — `display.status_bar.fields: []` rollback claim)**: Initial inspection of `cli_modern_layout.py:376` (`if field_set is None`) suggested setting `display.status_bar.fields` to `[]` might return `frozenset()` and hide all status-bar fields. Runtime verification and inspection of `cli_status_bar_mixin.py:1012` (`if isinstance(fields, list) and fields:`) proved `[]` returns `None` ("not customized"), so the documented rollback command works as stated.
- **Confirmed Robust (`C7` transcript escape & partial UTF-8 handling)**: Adversarial testing of `hermes_tmux/transcript.py` and `labels.defuse()` with OSC 52, OSC 8, DCS, APC, CSI, NUL, CR, DEL, C1 controls, bidi overrides, and multi-byte UTF-8 sequences split across writes confirmed zero escape leakage and clean UTF-8 reassembly (`pending.split(b"\n")`).
- **Secondary Reference-Only COBOL Findings (`~/Code/hermes-terminal-env/bench/tui/cobol/listbench.cob`)** *(HTE is completed research and not part of the running Hermes runtime; left untouched per scope)*:
  1. `listbench.cob:160`: `ACCEPT PATH-TEXT FROM ARGUMENT-VALUE` with no arguments leaves `TRIM(PATH-TEXT)` length 0 (`P-LEN = 301`), trapping under `cobc -debug` (`libcob: offset of 'PATH-TEXT' out of bounds: 301, maximum: 300`).
  2. `listbench.cob:779`: `FUNCTION INTEGER-PART(ROW-TS / 86400)` truncates toward zero instead of floor division (`div_euclid`), mapping `ROW-TS = -1` to `1970-01-01` instead of `1969-12-31`.
  3. `listbench.cob:675`: `IF ROW-FLAGS = 0` checks equality with zero rather than unread bitmask `(flags & 1) == 0`, undercounting flagged-unread messages relative to the Rust/Go/Python benchmarks.

---

## 5. Performance Benchmark Results (Python 3.9.6, Identical Workloads)

Harness: `bench_performance.py` running in an isolated tmux 3.8 server (`-f /dev/null`) with a synthetic 50-delegation / 150-transcript `HERMES_HOME` and WAL-mode `state.db` (`2,000` seeded messages).

| Workload / Metric | Baseline (`0a5cc97`) | Repaired (`2b44f93`) | Delta |
|---|---:|---:|---|
| **`W1` Idle (6.1s, 0 writes)** — `watch` CPU % | 1.31% | 0.81% | -38% |
| **`W1` Idle (6.1s, 0 writes)** — `follow` CPU % | 0.00% | 0.00% | 0% |
| **`W2` Parent Chat WAL Writes (~20 Hz, 0 subagents)** — `watch` CPU % | **21.42%** (1.30s / 6.07s) | **0.33%** (0.02s / 6.08s) | **-98.5% (65x lower)** |
| **`W2` Parent Chat WAL Writes (~20 Hz, 0 subagents)** — `follow` CPU % | **26.69%** (1.62s / 6.07s) | **0.00%** (0.00s / 6.08s) | **-100% (eliminated)** |
| **`W2` Combined CPU (`watch` + `follow`)** | **48.11%** | **0.33%** | **-99.3% (145x lower)** |
| **Open File Descriptors (`watch` / `follow` / total)** | 235 / 241 (**476**) | 140 / 117 (**257**) | **-219 FDs (-46%)** |
| **Resident Memory RSS (`watch` / `follow`)** | 22,112 KB / 22,128 KB | 22,352 KB / 21,920 KB | Flat (<0.3 MB) |
| **`W3` New Worker -> Overview Visible Latency (3 trials)** | 230.0, 274.4, 280.2 ms | 282.5, 177.7, 179.2 ms | Median **179.2 ms** (-35%) |
| **`W3` New Worker -> Worker Tab Created Latency (3 trials)** | 674.5, 500.9, 529.0 ms | 846.0, 468.2, 433.0 ms | Median **468.2 ms** (-11%) |

---

## 6. Runtime Test Matrix & Coverage Gaps

| Area | Exercised How | Outcome |
|---|---|---|
| Full `hermes-tmux-workspace` suite on macOS Python 3.9.6 | `TMUX_TMPDIR=... /usr/bin/python3 -B -m unittest discover -s tests -t .` | **352 / 352 PASS** (`198.8s`, 0 leaked servers/processes) |
| Real user `~/.config/tmux/tmux.conf` (`base-index 1`, `renumber-windows on`, custom status format) | Isolated `-L` server loaded with `-f ~/.config/tmux/tmux.conf`: `up` -> `worker 1` -> `down` | **PASS** (window indices `1, 2, 3`, clean teardown) |
| Multi-profile `HERMES_HOME`, `open`-before-`up`, grouped sessions, PTY SGR mouse clicks on `×` | Real PTY `attach-session` sending `\x1b[<0;col;rowM/m` across `sessA`/`sessB` | **PASS** (`tests/test_carbon_closemark_profiles.py`, `test_close_mark.py`, `test_grouped_sessions.py`) |
| `WatchSet` atomic inode replacement, >256 file priority cap, 25 Hz WAL & transcript floods | Isolated kqueue + SQLite WAL + thread stress tests | **PASS** (`tests/test_carbon_watch_and_perf.py`) |
| Hostile OSC 52, OSC 8, DCS, APC, CSI, C1, bidi, partial UTF-8, CJK double-width & combining marks | `Formatter` + `render` + `pane_header` + `cli_modern_layout` | **PASS** (`tests/test_carbon_unicode_and_escapes.py`, `test_cli_modern_layout.py`) |
| `hermes-agent-tui-ux` compact footer & layout suite | `scripts/run_tests.sh` on 4 target files + Python 3.9.6 `py_compile` | **38 / 38 PASS** (`1.4s`) |
| **Unresolved / Accepted Coverage Gaps** | (1) Multi-codepoint ZWJ emoji grapheme clusters are measured per code point rather than terminal-specific HarfBuzz shaping; (2) `tui/compact-footer` is an unmerged feature branch (`archpc/hermes-agent`) not yet merged into upstream `~/.hermes/hermes-agent` (`3b421578`), so its fix is pushed to `fork` (`archpc/hermes-agent`) rather than hot-patched onto detached upstream `main`. | Documented |

---

## 7. Documentation & Comment Audit Fixes

1. **`hermes_tmux/watch.py:1-23` module docstring**: Replaced stale v1/v2 claims ("keeps per-worker `tail -F` panes pointed...", "Neither process polls for data. The only timed wake is one scheduled wake per minute... performs no filesystem or database reads") with accurate v3 coordinator/follower, 1 Hz throttle, stat-cache, and bounded fallback wake semantics.
2. **`README.md:24-28` & `docs/shots/README.md:27-29`**: Clarified that `×` appears when a *worker* tab is in view and noted that screenshots `01`–`05` were captured prior to commit `3f29699` (`closemark.py`) with `agents*` active in `05-tab-bar.png`.
3. **`README.md:203`**: Corrected false claim that `tokens/cost` are read from `state.db` `async_delegations` (actual fields used: terminal `state`, `result_json` exit reason/status, and `task_json` transcript path map).
4. **`README.md:242`**: Updated test count from `333` to `352`.
5. **`docs/DESIGN.md:70,260`**: Updated `tail` vnode + 2s safety wait description and replaced obsolete "widths are counted in characters" limitation with the new East Asian / combining cell-width behavior.

---

## 8. Rollback Procedures

Snapshot recorded prior to any change: `/Users/sein/.hermes/artifacts/carbon-audit-2026-11-13/rollback/pre-audit-snapshot.json`.

### Roll back `hermes-tmux-workspace` (live symlink `~/.local/bin/hermes-tmux`)
```sh
git -C ~/Code/hermes-tmux-workspace checkout main
git -C ~/Code/hermes-tmux-workspace reset --hard 0a5cc97a0ba90c2f673f98d534b04140a607b41e
# Inside tmux session `main`, restart coordinator & overview pane non-destructively:
hermes-tmux up
```

### Roll back `hermes-agent-tui-ux`
```sh
git -C ~/Code/hermes-agent-tui-ux reset --hard 7159ad64cddd82c400655550a55970e62fc4d738
```
