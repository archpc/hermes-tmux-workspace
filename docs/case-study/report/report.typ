= Executive summary
#lead[Hermes delegates work to subagents and writes down everything they do. This is the account of building the tmux workspace that lets a person watch that work, through three layouts in one day, and of why the third one is organised around the worker rather than around the screen.]

// claim: S1
#verdict[Version 3 is deployed and its lifecycle is observed in production: a real worker appeared as a named tab, finished, lingered and retired. An independent reviewer failed the change once on a real lost-update defect and passed it after the fix. The failure path has only been exercised with synthetic workers.]

#key-findings((
  (title: "The unit of display is the worker", detail: [Versions 1 and 2 packed transcripts into panes and named windows by number. Version 3 gives each worker one window, named after the task its parent wrote, with state as a glyph in the tab.]),
  (title: "History was the real clutter", detail: [The cramped panes were a symptom. Under a real day of work, version 2 showed four tabs of finished transcripts up to nine hours old. Version 3 keeps tabs for live work only.]),
  (title: "Rendering found what tests did not", detail: [A tmux notice printed into the user's own pane, an unreadable header colour and a twenty-second blind spot were all found by looking at real renders, then pinned with tests.]),
  (title: "Review earned its place once", detail: [The second model found one defect that mattered, a lost update between the coordinator and a user command, and withdrew one of its own claims when asked to test it.]),
))

#metrics((
  ("1", "worker per tab"),
  ("308", "tests, 109.5 s"),
  ("2", "review rounds"),
))

= The problem: work you cannot see

// claim: P1
Hermes already records delegated work. Every task gets an append-only transcript under `cache/delegation/live`, and a manifest says which tasks belong to which delegation. Nothing is missing from the record. What was missing was a way to look at it while it happens: which workers are running, what each is doing now, which one stopped, and where to read the rest.

The tool, `hermes-tmux`, adds no daemon and changes nothing in Hermes. It is one stdlib Python process per tmux session that reads those files and arranges tmux windows. That constraint shaped everything after it. The display can only say what Hermes recorded, so the design question was never what to compute. It was what to show, where, and for how long.

// claim: P2
Some things Hermes does not record at all. It has no concept of reviewing a delegation, and it records how an agent run stopped, not whether the work was good. Both gaps became display rules rather than features: the workspace shows a review only when one was written down, and it reports a run that ended in an error as exactly that.

= Three versions in one day

#figure-image("/Code/hermes-tmux-workspace-ux/docs/case-study/report/figures/evolution.svg", caption: [The three layouts, drawn schematically. Window tabs are along the bottom of each frame, as tmux draws them.], source: [Source: commit messages `859d3f0`, `4d93f6e`, `ade244b`; drawn for this report, not captured.])

// claim: V1
Version 1 landed at 13:32 with a single window, `hx:workers`, holding the overview and up to four `tail -F` panes. Getting it to work at all was most of the effort. The integration suite had never run to completion, and running it on an isolated socket exposed defects that only real tmux shows.

#data-table(
  caption: [Defects that only appeared against real tmux, version 1],
  source: [Source: `docs/DESIGN.md` defect table, rows D6, D9, D15, D25; wording condensed.],
  head: ("What happened", "Why"),
  rows: (
    ("`down` closed nothing, and the guard against foreign panes passed for the wrong reason", "The ownership marker was read as `{@hx}`, a literal, not `#{@hx}`"),
    ("Worker panes read the wrong profile and failed to import the package", "New tmux windows inherit the server's environment, not the caller's"),
    ("93 idle coordinator processes had accumulated, one per closed session", "A `run-shell -b` child outlives its server and never checked"),
    ("Stale-pane recovery and socket cleanup were silently disabled", "`display-message` exits 0 for a target that does not exist on tmux 3.8"),
  ),
  widths: (1.5fr, 1.3fr),
  aligns: (left, left),
)

// claim: V2
Version 2 followed four hours later and addressed the obvious complaint, which was width. The overview got the `hx:workers` window to itself and transcripts moved to `hx:worker-N` windows of at most two panes, so every pane stayed wide enough to read. Placement was made independent of run state so a finishing worker could not drag its pane elsewhere. The suite grew from 142 to 166 tests.

It passed its tests and was deployed. Then it was rendered against the actual state of the machine.

= What version 2 looked like on a real day

#figure-image("/Code/hermes-tmux-workspace-ux/docs/case-study/report/figures/img/v2-overview.png", caption: [Version 2 overview, left 96 columns. Version 2 code on a private tmux socket, reading the day's real delegation history at 17:58.], source: [Source: `ux-redesign/baseline/v2-overview.png`, cropped.], full-width: true)

// claim: B1
Nothing was running, and the screen was full. The overview was a seven-column table keyed by delegation id, with forty rows of history and every state reading `done`. The footer counted two failures that no visible row showed. The tab for the window carried a permanent `!`, because two old failures could never be acknowledged.

#figure-image("/Code/hermes-tmux-workspace-ux/docs/case-study/report/figures/img/v2-tabs.png", caption: [Version 2 status line. Four numbered worker windows, all holding finished work.], source: [Source: `ux-redesign/baseline/v2-overview.png`, bottom row.], full-width: true)

// claim: B2
The four worker tabs held eight finished transcripts, the oldest nine hours old. Each pane was a raw `tail -F`: no colour, every message repeated by the writer, and a kickoff line that filled the pane before any work appeared.

#figure-image("/Code/hermes-tmux-workspace-ux/docs/case-study/report/figures/img/v2-worker.png", caption: [Version 2 worker window, left 96 columns, top 30 rows. Two stacked panes; identity is the border label.], source: [Source: `ux-redesign/baseline/v2-worker-1.png`, cropped.], full-width: true)

The brief for the redesign named the cramped panes. The render said something else. Panes two-up were tolerable. What made the workspace useless at a glance was that it could not tell live work from history, and that nothing on screen was named for what it was doing.

= Rejected architectures

The redesign started from mockups that grouped workers into tabs by role: Protocol, Implementation, Review. Three structures were considered and two were dropped before any code.

#data-table(
  caption: [Structures considered for version 3],
  source: [Source: `docs/DESIGN.md` section 5 decision table; journal entry 02.],
  head: ("Structure", "Outcome", "Reason"),
  rows: (
    ("Tabs by role", "Rejected", "Hermes records no role for a task. Role tabs would need a classifier reading prose, and a wrong label is worse than none."),
    ("One window per delegation", "Rejected", "Three siblings either break the two-pane ceiling or get split anyway, and the tab's state becomes an aggregate."),
    ("One worker per window", "Chosen", "A tab's state is exactly one worker's state, and the transcript gets the whole window. Slot packing and tiled reflow disappear."),
  ),
  widths: (1fr, 0.6fr, 2.6fr),
  aligns: (left, left, left),
)

// claim: R1
The role tabs are the instructive rejection. They were the most attractive option in a mockup and the least honest in practice: the grouping would have been invented by the display. The same reasoning removed a smaller idea later, renaming windows as their state changed. A name that changes is a tab that moves under the cursor.

Two further ideas were rejected by the user during the work, and both rejections improved the result. Re-rendering the transcript on resize would have kept a hanging indent at the cost of the reader's scroll position. Emulator-specific click handling was unnecessary because tmux mouse mode already makes status-line tabs clickable in every terminal.

= The design: a tab is a worker

#figure-image("/Code/hermes-tmux-workspace-ux/docs/case-study/report/figures/img/prod-overview.png", caption: [Version 3 overview with two real workers running. Production session, 19:32, columns 35 to 152 of 187.], source: [Source: `case-study/shots/prod-01-overview-running.heic`, cropped. The workers are the two investigations dispatched to gather sources for this report.], full-width: true)

// claim: D1
The overview is now three sections: what is running, what needs attention, and what finished recently. Each entry leads with the task as the parent wrote it. Model, elapsed time, the current tool call and the identifier are secondary and dimmer. History only ever takes the rows the live sections leave free.

#figure-image("/Code/hermes-tmux-workspace-ux/docs/case-study/report/figures/img/prod-tabs.png", caption: [Version 3 status line in production, left 100 columns. State is the glyph; the name is the task.], source: [Source: `case-study/shots/prod-02-worker-running.heic`, bottom row. `hxview` is a second view of the production session, used so the capture did not change the user's active window.], full-width: true)

A tab's name is the first clause of the goal, set once and never changed. Articles are dropped, paths reduce to a basename, and words shared by every sibling in a delegation are dropped from the front so narrow tabs still differ. Truncation happens in the status line, per client, from that client's width: the same session shows full names on a wide terminal and bare glyphs on a narrow one, with no renames.

== Lifecycle

#figure-image("/Code/hermes-tmux-workspace-ux/docs/case-study/report/figures/lifecycle.svg", caption: [Life of a worker tab. Times are the defaults.], source: [Source: `hermes_tmux/watch.py` and `render.py` constants; drawn for this report.])

// claim: L1
The lifecycle is the part that fixed the real problem. A tab exists for live work, which includes work already running when the workspace starts. A finished tab lingers 5 minutes after it settled or was last looked at, and never closes while it is in view. A run that ended in an error keeps its tab until someone has looked at it; looking is the acknowledgement. At most 8 tabs exist, and anything past that is listed in the overview.

#data-table(
  caption: [Lifecycle rules and their constants],
  source: [Source: `hermes_tmux/watch.py`, `hermes_tmux/render.py` at `ce69f1c`.],
  head: ("Rule", "Value", "Where"),
  rows: (
    ("Finished tab lingers", "300 s", "`LINGER_S`"),
    ("Tabs open at once", "8", "`MAX_TABS`"),
    ("Unseen failure stops asking for attention", "2 h", "`ATTENTION_TTL_S`"),
    ("Worker shown as silent, never as failed", "10 min without an event", "`STALL_AFTER_S`"),
    ("Overview text column", "118 columns, centred", "`MAX_CONTENT_WIDTH`"),
  ),
  widths: (2.2fr, 1.5fr, 1.2fr),
  aligns: (left, left, left),
)

A tab the reader closes stays closed. Version 2 recreated it, which was the tool overriding the reader. The worker stays listed in the overview, and `hermes-tmux open` with its identifier brings the transcript back as a pinned tab.

== A run that ended is not a task that failed

#figure-image("/Code/hermes-tmux-workspace-ux/docs/case-study/report/figures/img/lab-attention.png", caption: [A run that ended with a provider error. Synthetic: lab socket, a real transcript replayed with an injected 529.], source: [Source: `ux-redesign/final/01-full-overview.png`, cropped.], full-width: true)

// claim: D2
In this capture the worker had finished its work and reported eight passing tests when the provider failed. A red mark that said "failed" would have been false. The overview says how the run ended, quotes the error, and then quotes the worker's own last report beside it. The display does not decide which one matters.

= Reading a transcript

#figure-image("/Code/hermes-tmux-workspace-ux/docs/case-study/report/figures/img/prod-worker.png", caption: [Version 3 worker tab in production, left 96 columns, top 15 rows. A real worker, 19:33.], source: [Source: `case-study/shots/prod-02-worker-running.heic`, cropped.], full-width: true)

// claim: T1
Worker panes run `hermes-tmux tail` instead of `tail -F`. It is presentation only: the file path is printed first, lines it does not recognise are shown verbatim, and `HERMES_TMUX_TRANSCRIPT=raw` restores plain `tail`. Tool calls and their results take one line each, the writer's duplicate of every message is collapsed, and colour separates narration from tool traffic.

The wrapping decision is the one visible compromise. Each event is written as one terminal line and the terminal wraps it. Continuation lines therefore start at the left edge rather than under the text. In exchange, a resize re-wraps natively, the follower never redraws, and scrolling back while output arrives leaves the view where the reader put it.

= One implementation, three terminals

#figure-image("/Code/hermes-tmux-workspace-ux/docs/case-study/report/figures/img/lab-narrow-overview.png", caption: [Overview at 61 by 24. Synthetic lab workers. Tabs have collapsed to number and glyph.], source: [Source: `ux-redesign/final/08-narrow-overview.png`.], width: 80%)

// claim: X1
The workspace uses tmux features only: window options, format conditionals in the status line and pane border, and mouse mode. It was rendered in iTerm2 and Ghostty at four sizes from 187 by 51 down to 61 by 24, and smoke-tested in Terminal.app, where a real click on a status-line tab switched windows. There is no emulator-specific code.

Responsiveness is mostly tmux's own. The pane header is four variants inside one conditional on the pane's width, and tmux picks. The overview reflows in the renderer: below 90 columns it drops the tool count and identifier, below 60 the model. Primary text uses the terminal's own foreground so a light theme stays legible.

= How it was built

#data-table(
  caption: [Who did what],
  source: [Source: journal entries 02 to 05; delegation ids from Hermes state.],
  head: ("Role", "Model", "Work"),
  rows: (
    ("Design authority, integration, acceptance", "Fable 5.2", "Chose the design, wrote the layout, lifecycle and transcript code, judged every render, merged and deployed"),
    ("Delegated implementation and tests", "DeepSeek V4.1 Flash", "Rewrote and ported the test suites in parallel, one owner per file; later, source gathering for this report"),
    ("Independent review", "GLM 5.3 Flash", "Two read-only rounds against eight safety invariants"),
  ),
  widths: (1.3fr, 0.9fr, 2.3fr),
  aligns: (left, left, left),
)

// claim: H1
The division held because ownership was by file. Three DeepSeek workers rewrote unit tests, wrote tab-lifecycle tests on a private socket and repaired four older suites at the same time, without touching each other's files or the implementation. They were told to report defects rather than bend assertions, and they did: a clipped line that could cut its own truncation marker in half, a closing line that announced an ordinary exit, and a first delegation on a fresh profile that went unnoticed until a slow wake.

The same workers are also the test subjects. A workspace for watching subagents is best exercised by subagents doing real work, so each delegation above doubled as a live trial of the display.

== Iterating on what is rendered

// claim: I1
The inner loop was build, render, look. A small generator replays real transcripts into a private state directory as if they were live, so any mix of running, finished and failed workers can be on screen in seconds without launching an agent. The logic that decides what to show is tested without tmux at all: 231 tests in 0.14 seconds.

The renders found things the tests had no reason to look for. Stopping the workspace left "terminated by signal 15" in the user's own pane, in view mode, because tmux reports a signalled background job in whichever pane is active; version 2 had done this since it shipped. The pane header's title was dim to the point of unreadable in one terminal, because inside a border the default colour means the border's colour. A new delegation went unnoticed for up to twenty seconds after a change intended to skip redundant reloads.

= Safety, review and production

The design was negotiable. Six things were not: never close or repurpose a pane the tool did not create, never change focus unasked, never let a coordinator outlive or cross a tmux server, never lose a concurrent layout write, never alter a transcript, and always leave a way back.

// claim: G1
The first review round failed the change, correctly. The coordinator read the layout, spent seconds creating windows, then wrote the whole tab map back. A user running `hermes-tmux open` in that gap had their tab's record erased, and the next pass opened a second window for the same worker. The pass now remembers what it read and writes only its own changes, and a test places the race exactly.

// claim: G2
The second round passed with no blockers. The reviewer had also claimed that a pane the user splits inside one of the tool's windows inherits the ownership marker and would be closed by `down`. Asked to settle it by experiment on a private socket, it reported that the new pane has no marker of its own and withdrew the claim. A comment in the code had asserted the same wrong thing and was corrected to what was measured.

#data-table(
  caption: [Production validation, session `main`, 12 November],
  source: [Source: `ux-redesign/deploy/` records and `case-study/shots/` captures. All rows are real production behaviour.],
  head: ("Time", "Observed"),
  rows: (
    ("19:22", "Version 2 coordinator stopped with an interrupt; nothing printed into the Hermes pane"),
    ("19:23", "Old `down` closed 8 panes and 4 windows; checkout fast-forwarded; `up` built `agents`. Active window unchanged"),
    ("19:24", "Real delegation `bcaf50c1` appeared as tab 4, named from its goal, running glyph, listed under RUNNING"),
    ("19:28", "Same worker completed after 4m07s: done glyph, moved to RECENT with its own summary"),
    ("19:33", "Tab retired 300 s after it settled; still listed, still reopenable"),
    ("19:32 to 19:39", "Delegation `98adce95`: two workers, two tabs, both completed"),
  ),
  widths: (0.6fr, 3.6fr),
  aligns: (left, left),
)

#figure-image("/Code/hermes-tmux-workspace-ux/docs/case-study/report/figures/img/prod-overview-done.png", caption: [Production overview after both workers finished, 19:39. The earlier worker has lost its tab and remains in the list.], source: [Source: `case-study/shots/prod-04-overview-completed.heic`, cropped.], full-width: true)

// claim: G3
The full suite, 308 tests, passes in 109.5 seconds on private sockets and leaves no tmux server behind. Rollback was rehearsed on a private socket rather than assumed: version 3 up and down, the version 2 tag up and down, version 3 again, with a user shell surviving every step.

// claim: G4
#note(kind: "Not observed", tone: "muted")[No production worker ended in an error during validation. The attention state, the failed-run tab that waits to be seen, and the failure wording are shown here from synthetic lab workers and covered by tests. They have not been watched happening to a real worker in the production session.]

= Mistakes and limits

// claim: M1
Three mistakes were mine, and each is ordinary. I deleted the first set of product screenshots before their replacements existed; the screen had locked, window capture fails while locked, and the originals were gone. The set was re-shot from the final build once the screen unlocked. I exported a ten-minute linger for the lab and then started the production coordinator from the same shell, which inherited it; the process list showed it and the coordinator was restarted clean. And I described a 280-second test file becoming a 43-second one as a speed-up, when the 280 seconds were that file failing against the new layout and waiting out its timeouts.

What remains imperfect is known. Wrapped transcript lines have no hanging indent. Widths are counted in characters, so double-width text in a goal can overrun a line. With clients of different sizes attached, tmux sizes each window for whichever used it last. Tab numbers shift when an earlier tab retires, because the user's tmux renumbers windows. A ninth concurrent worker gets a row in the overview and no tab.

= Lessons for agent tooling

Show the unit the reader thinks in. The reader thinks in tasks. Panes, slots and delegation ids are the tool's bookkeeping, and every version that led with them needed a legend.

Decide what leaves the screen. Both early versions were designed around how to fit more in. The improvement came from a rule for when something goes away, and from making that rule safe: never while in view, never without a way back.

Say only what was recorded. The most useful constraint in this project was refusing to display anything Hermes did not write down: no roles, no review status, no verdict on the work. Each refusal removed a feature and a way to be wrong.

Render before believing. A layout that passes its tests has been shown to be consistent with its author's expectations. The defects that mattered here were visible in one glance at a real terminal and invisible to a suite that had no reason to check for them.

Let the second model disagree, and make it test its claims. One review round found a real defect. The same round asserted something false with equal confidence. Asking for an experiment separated the two in a few minutes.

#appendix-heading("A", "Provenance of the captures")

#data-table(
  caption: [What each kind of capture is],
  source: [Source: capture file names and the journal; see the evidence directory beside this report.],
  head: ("Label in captions", "What it is", "Real or synthetic"),
  rows: (
    ("Version 2, private socket", "Version 2 code on a private tmux server reading the machine's real delegation history", "Real history, lab server"),
    ("Production", "The user's `main` tmux session after deployment, viewed through a second grouped session `hxview` so the user's active window never changed", "Real"),
    ("Lab, synthetic", "Final build on a private tmux server; workers are real transcripts replayed with current timestamps, one with an injected provider error", "Synthetic"),
  ),
  widths: (1.1fr, 2.6fr, 1fr),
  aligns: (left, left, left),
)

Figures are crops of full-window captures. The untouched originals are in `ux-redesign/baseline`, `ux-redesign/final` and `case-study/shots`. Crop rectangles are in `report/crop.py`.

#appendix-heading("B", "Defects found and fixed before release")

#data-table(
  caption: [Defects V1 to V10],
  source: [Source: `docs/DESIGN.md` section 5 at `ce69f1c`; wording condensed.],
  head: ("", "Symptom", "Found by"),
  rows: (
    ("V1", "tmux printed a job notice into the user's pane on `down`", "Rendering"),
    ("V2", "A new delegation went unnoticed for up to 20 s", "Rendering"),
    ("V3", "One-character orphan lines in transcripts", "Rendering"),
    ("V4", "A preview shown as well as the message it previews", "Rendering"),
    ("V5", "Failures from before the workspace started asked for attention", "Rendering"),
    ("V6", "Pane header title unreadable in one terminal", "Rendering"),
    ("V7", "Coordinator reverted a tab published by `open`", "Independent review"),
    ("V8", "`tail --no-follow` left the shell without echo", "Independent review"),
    ("V9", "A clipped line could cut its own truncation marker", "Delegated test writing"),
    ("V10", "First delegation on a fresh profile noticed late", "Delegated test writing"),
  ),
  widths: (0.35fr, 3fr, 1.3fr),
  aligns: (left, left, left),
)

#references((
  [Repository `archpc/hermes-tmux-workspace` (private). Version 2 at `a17522d`, tag `v2-two-per-window`; version 3 at `ade244b`, pull request 1; documentation corrections at `ce69f1c`.],
  [`docs/DESIGN.md`, sections 1 to 5, at `ce69f1c`. Snapshot in `evidence/DESIGN.md`.],
  [Independent review transcripts, rounds 1 and 2. `evidence/glm/`.],
  [Deployment and production records. `evidence/deploy/`.],
  [Working journal, entries 01 to 05, as written to the Apple Note "Hermes tmux Workspace -- UX Redesign". `evidence/journal/`.],
  [Test run at `ce69f1c`. `evidence/test-run-main.txt`, `evidence/dev/pure-suites.txt`.],
))
