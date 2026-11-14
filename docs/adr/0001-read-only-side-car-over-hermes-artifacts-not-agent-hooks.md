# ADR-0001: Read-only side-car over Hermes artifacts, not agent hooks

- Status: accepted
- Date: 2026-11-12
- Deciders: Sein (owner)
- Supersedes: none
- Superseded by: none
- Evidence quality: contemporaneous design record

## Context and problem

A person running several Hermes delegations in one terminal needs to see which workers are alive,
what each is doing, and who needs attention, without switching to a web dashboard. Hermes already
writes that information to disk while a delegation runs, and it already runs every worker in its own
process. The question is where the tmux layer attaches: inside the agent process, inside the TUI
transport, or beside both as a reader.

## Constraints and requirements

- Hermes core must not be modified, patched, or reconfigured by the workspace tool.
- No web dashboard; terminal only.
- The user's own tmux windows and panes are never renamed, repointed, or closed.
- Information shown must be something Hermes actually recorded. Review status, task quality, and live
  per-worker cost are not observable and must not be inferred.
- Must survive detach/reattach and not leak processes or state across sessions or servers.
- Python standard library only, no daemon outside the tmux server.

## Alternatives considered

| Alternative | Why it was weighed | Why it lost |
|---|---|---|
| Hermes hooks (`hooks:` + `invoke_hook`) | Official extension surface | Requires config edits and first-use consent, puts failure modes inside the agent loop, and has no subagent-lifecycle event |
| TUI JSON-RPC (`subagent.list` / `tail` / `interrupt`) | Purpose-built worker view | Bound to a live renderer transport; using it means faking a client and pinning to an internal protocol |
| `tmux capture-pane` scraping | No integration work | Fragile against reflow, colour, and typed input; the structured transcript already exists |
| In-agent plugin patching Hermes | Most direct | Violates the no-modification constraint and would drift from upstream |

## Decision

Build a read-only side-car that reads Hermes's existing per-delegation artifacts and renders them
into tmux windows it owns:

- Transcripts: `$HERMES_HOME/cache/delegation/live/<delegation>/task-<n>.log`.
- Manifests: `manifest.json` beside each transcript (status, model, completion).
- Terminal state: `state.db` table `async_delegations`.
- Presentation: `state.py` normalizes, `render.py` builds the overview, `transcript.py` renders one
  transcript, `watch.py` watches with kqueue, `tmuxio.py` refuses panes it did not create.

Anything the sources do not record is displayed as unknown (review status, live cost, quality).

## Rationale

The artifacts already carry strictly more information than any hook would deliver, are append-only
and structured, and require no cooperation from Hermes. A reader cannot break the thing it reads.
Ownership markers (`@hx=1` pane option) plus window provenance keep the tool from touching panes the
user made, which was the dominant failure mode in early versions.

## Tradeoffs and consequences

- Coupled to internal file layouts. If Hermes moves `live/` or renames fields, the side-car breaks
  loudly rather than silently showing wrong data; the reader degrades to "no delegations found" with
  the reason stated.
- No writes to Hermes state, so the tool cannot launch, cancel, or label work. Every action stays in
  the agent.
- A watcher per session plus one follower per live pane; the coordinator exits when its session or
  server dies, and layout writes go through a locked read-modify-write to survive concurrent `up`.
- Staleness is a derived display label with a stated rule, not a claim about the task.
- Escape hatch: the transcript pane falls back to `tail -F` (`HERMES_TMUX_TRANSCRIPT=raw`), and
  `HERMES_TMUX=0` disables the whole layer.

## Supporting evidence

- `docs/DESIGN.md` — observability table (S1–S7), defect log D1–D32, out-of-scope list, v3 decisions.
- Pane ownership: `hermes_tmux/tmuxio.py`, tests `test_window_marker_does_not_make_its_panes_ours`,
  `test_foreign_window_with_our_name_is_never_adopted`.
- State key includes server pid: `docs/DESIGN.md` D10/D32.
- Independent GLM reviews: two FAIL rounds and one PASS, recorded in `docs/DESIGN.md` sections 3 and 5.

## Verification status

verified: `TMUX_TMPDIR=<scratch> python3 -m unittest discover -s tests -t .` → `Ran 354 tests`,
`OK`, 2026-11-14. Deployed and in daily use on this host.

## Related decisions

- ADR-0001 in the `agent-messaging` repository: native agent messaging, which also chose an existing
  Hermes mechanism over a custom transport.
