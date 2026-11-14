# The original tmux server's disappearance is unproven

Status: open question, deliberately not resolved by the `scripts/tmux-attach`
bootstrap fix. Recorded separately from that change so a repair is never read as
an explanation.

## What was observed

- A `main` session created 2026-11-14 17:34 EST existed with one shell pane and
  a window named `recovery`, while the state directory of the previous server
  (`~/.hermes/cache/hermes-tmux/sessions/3cbd5142-_0`, last written 17:33) held a
  `layout.json` and a `watch.lock` but no `watch.pid`: the coordinator of that
  server was gone, not merely idle.
- No `watch`/`follow` process survived from the earlier server; the workspace in
  the new server was built from scratch under a new key (`761472c0-_0`), which
  includes the new server pid.
- tmux session ids restart at `$0` when a server restarts, so "the session id
  changed back to 0" is consistent with a restart and proves nothing about why.

## What is not known

- Whether the earlier server exited on its own, was killed by a signal, crashed,
  or was replaced by a second server starting on the same socket.
- Whether anything other than the two processes above was affected.
- Whether a reboot, logout, or terminal-level event was involved.

Nothing in this repository's evidence distinguishes those cases. The one tmux
fact that is established is that a server each session's processes belong to is
recorded in the workspace key and in `watch.log`; a key with no live pid is a
dead server, not a diagnosis.

## What this means for the bootstrap fix

The fix makes a recreated or half-built session converge to a complete workspace
on the next terminal open. It does not make the restart harmless if the cause was
a crash loop or an external killer: that would need evidence of a second restart,
which has not been observed. If the server disappears again, capture
`tmux display -p '#{pid}'`, `sysctl kern.boottime`, the tail of
`~/.hermes/cache/hermes-tmux/sessions/*/watch.log`, and any panic report before
starting a new server over the socket.
