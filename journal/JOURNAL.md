# Carbon | Hermes Ecosystem Audit & Repairs — Engineering Journal

Canonical source for the autonomous overnight audit, reproduction, repair, verification, review, push, and live deployment across `hermes-tmux-workspace`, `hermes-agent-tui-ux`, and `~/.hermes/hermes-agent`.

## 2026-11-13 22:54 EST | 01 Pre-Audit Baseline, Topology, and Isolated Worktrees [C41b7ea2a]

Commit at time of entry: `0a5cc97 Product shots, install path, layout and license: a repo someone can read cold`

Isolated repair worktrees created for hermes-tmux-workspace (fix/carbon-audit-2026-11-13 @ 0a5cc97) and hermes-agent-tui-ux (fix/carbon-tui-audit-2026-11-13 @ 7159ad64cd).
Recorded live production baseline and rollback snapshot before any edits: tmux server pid 4695, session main ($0), agents window @16 (%30), watch pid 38808 (25.5 MB RSS, 7m24s CPU over 6h03m, 186 FDs), follow pid 69018 (23.4 MB RSS, 9m34s CPU over 14h27m, 186 FDs), both running on macOS Python 3.9.6.
Baseline test suites verified clean: hermes-tmux-workspace 333/333 pass on Python 3.9.6 (166.3s, 0 leaked processes); hermes-agent-tui-ux footer suite verified via canonical run_tests.sh.
Next: execute controlled reproduction & performance benchmark harnesses in isolated scratch homes and private tmux servers.
---
### Pre-audit topology and rollback state

- `~/Code/hermes-tmux-workspace`: `main` @ `0a5cc97a0ba90c2f673f98d534b04140a607b41e` (`archpc/hermes-tmux-workspace`), clean; symlinked from `~/.local/bin/hermes-tmux`.
- `~/Code/hermes-agent-tui-ux`: `tui/compact-footer` @ `7159ad64cddd82c400655550a55970e62fc4d738` (`archpc/hermes-agent`), clean.
- `~/.hermes/hermes-agent`: detached HEAD @ `3b421578588243f920886e6fe79a6faa87ea026e` (`NousResearch/hermes-agent`), clean.
- Live coordinator/overview observations (read-only):
  - `watch --key 3cbd5142-_0` (PID 38808): Python 3.9.6, 25,456 KB RSS, 7:23.89 CPU time across 06:03:16 elapsed, 15.1% instantaneous CPU during parent turn WAL writes, 186 open file descriptors.
  - `follow --key 3cbd5142-_0` (PID 69018): Python 3.9.6, 23,440 KB RSS, 9:34.00 CPU time across 14:26:54 elapsed, 8.8% instantaneous CPU during parent turn WAL writes, 186 open file descriptors.
  - Orphan directory observed: `~/.hermes/cache/hermes-tmux/sessions/nosock-unknown` (empty directory created when commands compute `session_key()` outside tmux).
- Rollback snapshot saved at `~/.hermes/artifacts/carbon-audit-2026-11-13/rollback/pre-audit-snapshot.json`.

## 2026-11-13 23:05 EST | 02 Reproductions, Falsified Hypotheses, and Performance Baseline [C6eba2a14]

Commit at time of entry: `0a5cc97 Product shots, install path, layout and license: a repo someone can read cold`

Completed isolated Python 3.9.6 reproduction & performance baselines across hermes-tmux-workspace, hermes-agent-tui-ux, and COBOL references.
Confirmed hermes-tmux P1/P2 defects: (1) multi-task batch DB state overwrites failed/interrupted sibling tasks; (2) suffixed independent-completion unit IDs missed; (3) WatchSet.sync keeps dead inode after atomic replace/rotation and drops files unordered >256; (4) limit=40 drops oldest live running workers and breaks status --all, open, and review; (5) close-mark fails across profiles, draws dead mark on open-before-up, and uninstalls server binding on single-profile down; (6) rerender outside tmux leaks nosock-unknown; (7) CJK cell-width overflow wraps fixed-height overview.
Confirmed severe CPU/FD bottleneck: 20 Hz unrelated parent WAL writes burn 48.1% combined CPU (21.4% watch + 26.7% follow) and hold 476 open FDs with zero active subagents.
Confirmed hermes-agent-tui-ux defects: _repeats_goal 24-char prefix collision, unguarded scalar display config in layout/busy readers, and unsanitized escapes + ignored NO_COLOR in reasoning_text_line. Falsified status_bar.fields=[] rollback bug.
---
### Summary of reproduced findings vs falsified hypotheses

| ID | Component | Severity | Status | Evidence (`evidence/repro_*.json`, `bench_baseline.json`) |
|---|---|---|---|---|
| C1 / C1b | `hermes_tmux/state.py:489-507,649` | P1 | CONFIRMED | 3-task mixed batch (`interrupted`, `completed`, `error`) with DB `state='completed'` turns all 3 into `completed`, `attention_count=0` (expected 2); suffixed unit IDs `deleg_*-1` missed (`running` instead of `error`). |
| C2a / C2b | `hermes_tmux/watch.py:219-254` | P1 | CONFIRMED | After `os.replace`, `WatchSet.sync` keeps `old_ino=27506987` instead of `new_ino=27506988`; subsequent appends fire `0` kqueue events. At 282 files, unordered `set` truncates arbitrarily at 256. |
| C3 / W2 | `hermes_tmux/watch.py:949-958,1081-1100`, `state.py` | P1 | CONFIRMED | `watcher.wait(remaining)` wakes immediately on `state.db-wal` writes instead of enforcing 1s throttle; `follow` wakes unthrottled on parent WAL writes, re-parsing 150 historical transcripts and spawning tmux subprocesses: **48.11% combined CPU** (21.42% `watch` + 26.69% `follow`) and **235 + 241 = 476 open FDs** when 0 subagents run. |
| C4 | `hermes_tmux/state.py:274,587`, `cli.py:401,646,950` | P1 | CONFIRMED | Oldest still-running worker dropped (`oldest_running_worker_visible=False`) behind 42 fast delegations; `status --all` returns 40 of 85 workers; `open` and `review` fail on >40th worker (`rc=2`). |
| C5a-d | `hermes_tmux/closemark.py`, `cli.py:383,679,700` | P1 | CONFIRMED | `MouseDown1Status` hardcodes first session's `HERMES_HOME` (Profile B click fails: `tab_closed_on_click=False`); `open` before `up` renders dead `×` with no binding; `down` in Profile A uninstalls server binding while Profile B lives; `without_mark` adds trailing space to no-space format. |
| C6 | `hermes_tmux/cli.py:432` | P2 | CONFIRMED | `rerender` outside tmux creates `cache/hermes-tmux/sessions/nosock-unknown` (matches orphan dir found on live Honk). |
| C7 | `hermes_tmux/labels.py`, `render.py`, `transcript.py`, `watch.py` | P2 | CONFIRMED | Escape/OSC52/C1/bidi defusal held (`escapes_completely_defused=True`), but CJK overview at `width=60` rendered `76` display columns, wrapping and scrolling fixed-height `_draw`. |
| T1 | `hermes_cli/cli_modern_layout.py:276-286` | P2 | CONFIRMED | `_repeats_goal` compares only first 24 chars, hiding distinct manual `/title` sharing a 24-char prefix with active goal. |
| T2 | `hermes_cli/cli_status_bar_mixin.py:1012` | - | **FALSIFIED** | Suspected `fields: []` would hide all status bar fields; verified `if isinstance(fields, list) and fields:` treats `[]` as `None` (default). |
| T3 | `hermes_cli/cli_modern_layout.py:116-129` | P1/P2 | CONFIRMED | Raw OSC 52 (`\x1b]52;c;...`) passes straight through `reasoning_text_line()` into terminal scrollback; `NO_COLOR=1` ignored. |
| T4 | `hermes_cli/cli_modern_layout.py:32-40` | P2 | CONFIRMED | Scalar `display:` config raises `AttributeError` in `layout_from_config` and `busy_input_mode_from_config` (unlike `dock_mode_from_config`). |
| COB-1..3 | `bench/tui/cobol/listbench.cob:160,675,779` | P3 (ref) | CONFIRMED | `cobc -debug` traps `offset of 'PATH-TEXT' out of bounds: 301, maximum: 300` on empty argv; `YMD-CALC` maps `TS=-1` to `1970-01-01` instead of `1969-12-31`; `ROW-FLAGS = 0` misses flagged-unread rows (`flags & 1 == 0`). Reference-only (HTE not in live path). |

## 2026-11-14 00:16 EST | 03 TDD Repairs Complete, 145x WAL CPU Reduction, Full Suites Green [Cd42ec2ec]

Commit at time of entry: `b2a1531 docs: align README, DESIGN, and shot notes with runtime contracts and 352-test suite`

Completed all TDD repairs across hermes-tmux-workspace (5 commits on fix/carbon-audit-2026-11-13) and hermes-agent-tui-ux (1 commit on fix/carbon-tui-audit-2026-11-13).
Full hermes-tmux suite passed on macOS Python 3.9.6: 352/352 OK in 198.8s, 0 leaked processes (caught and fixed a late-flush edge case in test_tabs where completed logs needed watching on the 8 most recent delegations).
Identical-workload Python 3.9.6 benchmark (W2 parent WAL 20Hz writes, 0 subagents): combined CPU dropped from 48.11% (21.42% watch + 26.69% follow) to 0.33% (0.33% watch + 0.00% follow) — a 145x reduction; open FDs dropped from 476 to 257; RSS held steady at ~22 MB; worker-to-overview latency improved to 178-282 ms.
hermes-agent-tui-ux footer suite passed 38/38 (0 failures) and py_compile verified on Python 3.9.6.
Next: dispatch independent code review subagent to challenge all commits before push and live rollout.
---
### Repair commits prepared for independent review

- `hermes-tmux-workspace` (`fix/carbon-audit-2026-11-13` from `0a5cc97`):
  1. `ff30264`: `fix(state,cli): reconcile per-task DB verdicts, preserve old live workers, stat-cache reads`
  2. `440ef08`: `fix(watch,perf): track replaced inodes, enforce 1Hz floor, skip unrelated WAL churn`
  3. `a4ba899`: `fix(closemark,cli): resolve @hx_home per window, install on open, guard server-wide down`
  4. `c00ef0b`: `fix(labels,render,transcript): account for wide CJK and combining cell widths`
  5. `2b44f93`: `fix(watch): keep kqueue logs on 8 most recent settled delegations for late flushes`
  6. Documentation update commit aligning `README.md`, `docs/DESIGN.md`, and `docs/shots/README.md`.
- `hermes-agent-tui-ux` (`fix/carbon-tui-audit-2026-11-13` from `7159ad64cd`):
  1. `c15b1bbffb`: `fix(tui): defuse escapes in reasoning lines, preserve distinct /title, guard scalar display`

### Before / After Benchmark Summary (Python 3.9.6, identical synthetic 50-delegation WAL home)

| Metric | Baseline (`0a5cc97`) | Repaired (`fix/carbon-audit-2026-11-13`) | Change |
|---|---:|---:|---|
| `W1` Idle `watch` CPU % | 1.31% | 0.81% | -38% |
| `W1` Idle `follow` CPU % | 0.00% | 0.00% | unchanged |
| `W2` Parent WAL chat `watch` CPU % | **21.42%** | **0.33%** | **-98.5% (65x lower)** |
| `W2` Parent WAL chat `follow` CPU % | **26.69%** | **0.00%** | **-100% (eliminated)** |
| `W2` Combined `watch` + `follow` CPU % | **48.11%** | **0.33%** | **-99.3% (145x lower)** |
| Open FDs (`watch` + `follow`) | 476 (235 + 241) | 257 (140 + 117) | **-46% (-219 FDs)** |
| Memory RSS (`watch` / `follow`) | 22.1 MB / 22.1 MB | 22.4 MB / 21.9 MB | flat (~0 delta) |
| `W3` New worker -> overview (ms) | 230, 274, 280 ms | 283, 178, 179 ms | ~25% faster median |
| `W3` New worker -> tab (ms) | 675, 501, 529 ms | 846, 468, 433 ms | ~13% faster median |

## 2026-11-14 00:33 EST | 04 Independent Review Reconciled, Pushed to GitHub, Deployed Live [Cfb7e2a6b]

Commit at time of entry: `5673131 docs(audit): add consolidated adversarial audit and repair report`

Independent review subagent (deleg_38d0f6a2) returned VERDICT: PASS; reconciled all valid follow-ups (watch.lock startup retry B1, db_only_fds max_skip bound C2, socket-dir server lock B2, O(sessions) close-mark scan E1, full NO_COLOR reasoning block G1).
Re-ran complete suites post-reconciliation: hermes-tmux-workspace 352/352 OK on Python 3.9.6 (0 leaks); hermes-agent-tui-ux 15-file status/subagent/dock/layout suite OK (rc=0).
Pushed and fast-forwarded archpc/hermes-tmux-workspace main (b6f2a74+) and archpc/hermes-agent tui/compact-footer (21750d0b0f), preserving rollback tags pre-carbon-2026-11-13 (0a5cc97) and pre-carbon-tui-2026-11-13 (7159ad64cd).
Deployed hermes-tmux live on Honk session main ($0) with active focus preserved on @0: live CPU dropped from 23.9% to ~0.0% (0.00s over 5s), open FDs dropped from 372 to 251, status --all now lists all 88 workers, and mixed batch deleg_f0b03414 reports exact per-task interrupted/completed states.
---
### Final verification & rollout summary

- Independent review report: `~/.hermes/artifacts/carbon-audit-2026-11-13/evidence/independent_review.md` (`Verdict: PASS`).
- Live Honk processes after rollout:
  - `watch --key 3cbd5142-_0`: PID `14066`, Python 3.9.6, `134` FDs (down from `186`), `0.00s` CPU across 5s active WAL turn.
  - `follow --key 3cbd5142-_0`: window `@34` (`agents`, `@hx=1`, `@hx_key=3cbd5142-_0`, `@hx_home=/Users/sein/.hermes`), pane `%48`, PID `14044`, Python 3.9.6, `117` FDs (down from `186`), `0.00s` CPU across 5s.
  - User windows `@0` (`%0`, active Hermes `PID 4696`) and `@2` (`%6`, user `zsh` `PID 5425`) preserved untouched.
- `~/.hermes/hermes-agent` (`3b421578`) left untouched per safeguard (`tui/compact-footer` is an unmerged feature branch on `archpc/hermes-agent` and `PID 4696` is active).
