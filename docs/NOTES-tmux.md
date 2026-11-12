# tmux mechanics this workspace depends on (verified on tmux 3.8, macOS 27.2.1)

Every statement below was produced by running the command shown. Where a probe
was inconclusive it says so instead of guessing.

## 1. Ownership markers must be read as `#{@opt}`

`set-option -p -t %7 @hx 1` stores a user option on that pane. Reading it
requires the `#`:

```sh
tmux display-message -p -t %7 '#{@hx}'   # -> 1
tmux display-message -p -t %7 '{@hx}'    # -> {@hx}   (literal, never "1")
```

The literal form is why an earlier implementation's destructive guard answered
"not ours" for every pane and made `down` a silent no-op. `list-panes -F
'#{@hx}'` and `list-windows -F '#{@hx}'` are the batch equivalents.

## 2. Window and pane ids are stable; indices are not

`#{window_id}` (`@N`) and `#{pane_id}` (`%N`) survive `renumber-windows`,
`base-index`, and detach/reattach. Indices (`:0`, `.1`) do not: with
`base-index 1` the first window is `:1`, so a test or script that hardcodes
`:0` works on a default config and fails on this machine.

## 3. New windows inherit the SERVER environment, not the caller's

```sh
tmux new-window -d 'printenv HERMES_HOME'      # server's value (or empty)
tmux new-window -d -e HERMES_HOME=/x 'printenv HERMES_HOME'   # -> /x
```

A pane that must read a particular profile has to be created with `-e`, or
carry the values in the command itself. `split-window` takes `-e` the same way.

## 4. `run-shell -b` children have `TMUX` but no `TMUX_PANE`

Observed: a `run-shell -b` child reports `TMUX=/<socket>,<server_pid>,<idx>`
with `TMUX_PANE` unset, and its parent is the tmux server. Consequences:

- anything that derives its identity from `TMUX_PANE` must be given that
  identity explicitly (`hermes-tmux watch --key …`);
- the child survives detach (verified: a run-shell child kept running after the
  only client detached);
- the child inherits the server's environment, so `HERMES_HOME` has to be
  embedded in the command.

## 5. A leftover socket file is normal, not evidence of a crash

Both a clean exit (`kill-session` of the last session) and `kill -9` of the
server leave the socket *file* behind; `tmux ls` then reports
`no server running on <path>` and a new client reuses the path without
complaint. Stale socket files on their own prove nothing about how a server
ended.

## 6. `[exited]` is a client-side message about the session, not about tmux

A client prints `[exited]` when the session it is attached to is destroyed. It
is reproduced deterministically by attaching to a session whose only pane exits
at once (`tmux new-session 'true'`) and by a client whose stdin reaches EOF
immediately. tmux itself starting cleanly in the same environment does not
produce it.

## 7. kqueue on APFS

`NOTE_WRITE` on the *directory* fires for file creation, mkdir, rename and
delete inside it; `NOTE_EXTEND` on the *file* fires on append. A zero-timeout
`kqueue.control` returns `[]` when idle. One append can raise two events, so
the watcher coalesces with a short drain.

## 8. `base-index` / `pane-base-index` are per-server options

A script that types into a session created by someone else cannot assume either
value. Address panes by id.

## 9. `send-keys` needs a resolvable target

Without `-t`, `send-keys` resolves through the "current client"; on a server
with no attached client it fails with `no current client`. Automation that
drives a detached session must always pass `-t <pane_id>`.

## 10. `terminal-features` / `terminal-overrides` append

`set -as` appends on every `source-file`, so `prefix r` reloads grow the tables
without bound (three reloads left three copies of `*:RGB`). Assign the complete
value with `set -g` to keep reloads idempotent. Winning an idempotent reload is
worth more than inheriting future defaults automatically.
