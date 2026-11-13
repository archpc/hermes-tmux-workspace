You are an independent technical reviewer. Review a substantive change to a small Python tool, "hermes-tmux", for CORRECTNESS and SAFETY. You did not write it. Be adversarial and concrete. Read-only: do not modify any file in the repository; do not run tmux against the default socket; do not start or stop anything. You may read files and run read-only commands (git diff, git show, grep, python -c for pure-function checks).

Repository (git worktree, branch feat/ux-v3): /Users/sein/Code/hermes-tmux-workspace-ux
Base: branch `main`. Implementation diff: /Users/sein/.hermes/artifacts/hermes-tmux-workspace/ux-redesign/glm/v3-impl.diff (git diff main..HEAD -- hermes_tmux). Design record: docs/DESIGN.md (section 5 describes this change). Files: hermes_tmux/watch.py, cli.py, tmuxio.py, render.py, labels.py, transcript.py, state.py, env.py.

What the tool does: a side-car that shows Hermes subagents inside the user's tmux session. Version 3 gives each worker its own tmux window ("tab"), an overview window named "agents", and a styled transcript follower. A coordinator process per tmux session (started with `tmux run-shell -b`) reconciles tabs from files on disk.

Safety invariants that MUST hold (these are the review's focus):
1. Never kill, respawn, split, rename, mark, or set options on a pane or window the tool did not create. Ownership is the target's OWN `@hx` option plus launch provenance (tmuxio._killable), never the window's marker alone, never a name. A user may split their own shell into one of the tool's windows; that pane and its window must survive tab retirement and `down`.
2. Stale state must not turn a user window into ours: window/pane ids are only unique per tmux server; layout.json can outlive a server.
3. No surprise focus changes: nothing may select a window or pane except commands the user ran (`worker N`, `open`, aux commands).
4. The coordinator must not leak (it must exit when its session or server is gone), must not write into panes it does not own (including via tmux run-shell output), and two coordinators must not run for one session.
5. layout.json is updated only through watch.update_layout (locked read-modify-write); look for lost-update paths, especially cli.cmd_open versus reconcile_tabs running concurrently.
6. A tab is never retired while it is the window in view; running workers' tabs are never closed to make room; a tab the user closed is not recreated; `open` restores it.
7. Transcript follower is presentation only: no event may be dropped except the documented exact duplicates; malformed lines are shown; truncation of the file is handled; it must exit when its pane is gone.
8. Text from goals/models/exit reasons is placed into tmux format strings (window option @hx_glyph, pane option @hx_head, window names shown through #W, window-status-format). Check for tmux format/style injection (`#[`, `#{`, `#(` command substitution, `,` and `}` inside conditionals) and for shell injection in commands built with string formatting.

Specific questions to answer with evidence (file:line, and a minimal reproduction or a reasoned trace):
a. watch.reconcile_tabs step 1 drops a tab when its pane is gone or not ours and records it as dismissed. Can any path afterwards close or decorate something that is not ours? What if tab["window"] names a user window but tab["pane"] is ours, or the reverse? Step 6 writes @hx_glyph to t["window"] and @hx_head to t["pane"] without re-checking ownership of the window: is that a write to a foreign window in any reachable state?
b. watch.decorate_window sets window-status-format / pane-border-* on a window id returned by tmuxio.new_window. Any way that id is not ours?
c. cli.cmd_down now closes "every pane we own in every window that carries our marker". Compare with the previous implementation (git show main:hermes_tmux/cli.py). Is anything now closed that was not before, or left behind (aux windows, windows whose marker was lost)?
d. cli._start_watcher wraps the coordinator as `cd X && env ... python -m hermes_tmux watch --key K >>LOG 2>&1; true`. cli._pid_is_watcher/_lock_holder identify the coordinator by lock holder + command line. Does the wrapper shell break that identity check or `down`'s kill? Does SIGTERM handling (raise KeyboardInterrupt in a signal handler) leave the lock/pid file in a bad state?
e. watch.WatchSet.wake_on_signal uses signal.set_wakeup_fd + a pipe in the kqueue. Any fd leak, blocking write, or interaction with subprocess calls?
f. transcript.Formatter: find an input sequence where an event is silently lost or duplicated (think/assistant collapse, header skipping, 'start' suppression, final/end lines). transcript.run_tail: partial last line, file replaced by a shorter one, file that does not exist yet, invalid UTF-8 split across reads.
g. labels.tmux_escape and its call sites; labels.tab_label output used as a window name; watch.pane_header building a tmux conditional. Construct a goal string that breaks out (e.g. contains `#(touch /tmp/x)`, `#{pane_id}`, `}`, `,`, `#[fg=red]`). Is `#(...)` ever evaluated by tmux for a window NAME displayed via #W or via `#{=/N/…:window_name}`? State what you verified versus what you are inferring.
h. render.py: any path where a line exceeds the given width, or where `height` trimming removes a RUNNING or NEEDS ATTENTION row in favour of history.
i. Anything else that violates the invariants.

Output format (plain text, no preamble):
VERDICT: PASS or FAIL (FAIL if any invariant can be violated in a reachable state)
BLOCKERS: numbered, each with invariant number, file:line, the reachable scenario, and the smallest fix.
NON-BLOCKING: numbered, same detail.
VERIFIED: what you actually executed or traced, versus INFERRED.
Keep it under 900 words. Do not pad with praise. If you cannot verify something, say so.
