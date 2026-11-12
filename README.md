# hermes-tmux

A Hermes-native tmux workspace: your agent window plus lazy, reusable
`logs` / `tests` / `debug` / `REA` panes and a live subagent window that shows
real worker output.

It is a **read-only side-car**. Hermes core is not patched, configured, or
restarted. The workspace reads the delegation artifacts Hermes already writes
and renders them into tmux. Nothing is fabricated: if a fact is not in a Hermes
artifact or a review registry entry, the display says so.

## Quick start

```sh
# inside an existing tmux session, from the pane you run `hermes` in
hermes-tmux up          # build or repair the workspace (idempotent)

hermes-tmux status      # one-shot overview; works outside tmux too
hermes-tmux logs        # open/focus hx:logs   (also: tests debug rea)
hermes-tmux worker 2    # focus the 2nd visible worker pane
hermes-tmux tests       # open/focus hx:tests (also: logs debug rea)
hermes-tmux debug-report  # windows, panes, ownership flags, watcher pid
hermes-tmux down        # close only panes this tool created; a window that
                        # also holds one of your panes is left standing
```

While you are still validating it, the workspace is **off by default**:

```sh
HERMES_TMUX=1 hermes-tmux up     # explicit opt-in while DEFAULT_ENABLED is False
HERMES_TMUX=0 hermes-tmux up     # always off
```

`HERMES_TMUX=0` is checked first and always wins.

## What the subagent window shows

```
hermes subagents - main        2 running / 5 total
------------------------------------------------------------------------------
KEY         ROLE  STATE MODEL            TOOL        AGE   GOAL
6cfc92ad#0  exec  run   deepseek-v4.1-fl execute_cod 3m    Analyse the sender domains
6cfc92ad#1  exec  run   deepseek-v4.1-fl write_file  3m    Quantify the corpus
8207551c#0  *revw done  glm-5.3-flash    -           12m   Independent review | review: recorded
------------------------------------------------------------------------------
states: 2 run, 3 done   review: 1 recorded, 4 unreviewed
legend: STATE from Hermes state; review only when an entry was recorded.
```

Per-worker panes each run `tail -F` on that worker's real append-only
transcript, so they show the actual tool calls and results as they happen.
Pane titles carry worker id, task index, and model.

Rules that follow from "never fabricate progress":

- a pane is bound to one worker key for as long as that worker is visible; it is
  never repointed at a different worker mid-run;
- at most four worker panes; the overview always lists every worker;
- workers with no transcript on disk get no pane rather than an empty one.

## Auxiliary panes

| Pane | Shows | Never |
|---|---|---|
| `hx:logs` | `tail -F` of the real `~/.hermes/logs/agent.log` | no placeholder text when the log is missing |
| `hx:tests` | the last recorded run, or the opt-in command; `tests --run` records it | does not execute the suite by default (that would recurse into a running suite) |
| `hx:debug` | live env (`HERMES_HOME`, `HERMES_TMUX`, `TMUX_PANE`), session key, window/pane inventory with ownership flags | no invented health status |
| `hx:rea` | `rea --version` when the CLI exists, plus a note that REA MCP state is served by Hermes | no simulated REA output |

All four are created lazily on first use and reused afterwards.

## Where the data comes from

| Display | Source |
|---|---|
| worker id, task goal, model | `<HERMES_HOME>/cache/delegation/live/<deleg_id>/manifest.json` |
| tool activity, thinking, assistant text, final summary | `.../task-<n>.log` (append-only, written live by Hermes) |
| terminal state, exit reason, tokens/cost | `state.db` table `async_delegations` |
| review status | `cache/hermes-tmux/reviews.json` — written by `review run` or `review record` |

Review status is never inferred. A worker whose goal mentions "review" is still
an executor; only a registry entry makes a row a reviewer.

See `docs/DESIGN.md` for the architecture decision record and the evidence table,
and `docs/NOTES-tmux.md` for the tmux/macOS mechanics the layout depends on.

## Tests

```sh
TMUX_TMPDIR=$(mktemp -d) python3 -m unittest discover -s tests -t .   # 142 tests
```

The suite creates a private tmux server (`-L hx_it_<pid>`) and tears it down;
the socket *file* it leaves behind is removed by `tearDownModule` only when the
server is gone, so a normal run leaves no live process and `sessions/` empty.

The suite needs no environment: it creates a **private tmux server** named
`hx_it_<pid>` and tears it down afterwards, so it never touches the default
socket or the user's sessions.

Unit tests cover the gate, the state reader, and the renderer. The integration
suite drives **real, isolated tmux sessions** named `hx_it_<pid>_<n>`, each with
a private `HERMES_HOME`, and asserts against what tmux actually painted. It
never lists, renames, or kills any other session.
