#!/usr/bin/env python3
"""Compose the case study from facts + prose, then hand it to research-pdf.

Regenerates report.typ, report.json, report.evidence.json and the two vector
diagrams on every run; nothing is patched in place. Raster figures come from
crop.py, which only reads the original captures.

    python3 make_report.py            # write sources
    python3 make_report.py --build    # write sources, then build + inspect
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = Path.home() / ".hermes/skills/research/research-pdf"
OUT = HERE.parent / "Hermes-tmux-Workspace-Case-Study.pdf"
REPO = str(Path.home() / "Code/hermes-tmux-workspace")

# --- facts: every number in the prose comes from here -----------------------
F = {
    "tests_v1": "142",            # commit bdd9fc7 message
    "tests_v2": "166",            # static count at a17522d (history.md)
    "tests_v3": "308",            # evidence/test-run-main.txt
    "tests_v3_s": "109.5",
    "pure_tests": "231",          # evidence/dev/pure-suites.txt
    "pure_s": "0.14",
    "linger_min": "5",
    "tab_cap": "8",
    "attention_h": "2",
    "stall_min": "10",
    "content_cols": "118",
    "v3_commit": "ade244b",
    "v2_commit": "a17522d",
    "docs_commit": "ce69f1c",
}

META = {
    "title": "Hermes tmux Workspace",
    "subtitle": "Designing an Observable Multi-Agent Development Environment",
    "kind": "Case Study",
    "topic": "Agent tooling · Terminal UX",
    "report_id": "RR-2026-1112-HX",
    "version": "1.0",
    "date": "2026-11-12",
    "classification": "Internal",
    "baseline": "v2 · two transcripts per window · a17522d",
    "target": "v3 · one worker per tab · ade244b",
    "baseline_short": "v2",
    "target_short": "v3",
    "prepared_by": "Sein",
    "source": "Repository history, design record, review transcripts, journal, window captures",
    "short_title": "Hermes tmux Workspace",
    "standfirst": "Three layouts in one day. The third stopped arranging transcripts and started "
                  "showing work: one worker per tab, named for its task, gone when it is no longer live.",
    "section_breaks": False,
}

BODY = r"""
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
  ("«tests_v3»", "tests, «tests_v3_s» s"),
  ("2", "review rounds"),
))

= The problem: work you cannot see

// claim: P1
Hermes already records delegated work. Every task gets an append-only transcript under `cache/delegation/live`, and a manifest says which tasks belong to which delegation. Nothing is missing from the record. What was missing was a way to look at it while it happens: which workers are running, what each is doing now, which one stopped, and where to read the rest.

The tool, `hermes-tmux`, adds no daemon and changes nothing in Hermes. It is one stdlib Python process per tmux session that reads those files and arranges tmux windows. That constraint shaped everything after it. The display can only say what Hermes recorded, so the design question was never what to compute. It was what to show, where, and for how long.

// claim: P2
Some things Hermes does not record at all. It has no concept of reviewing a delegation, and it records how an agent run stopped, not whether the work was good. Both gaps became display rules rather than features: the workspace shows a review only when one was written down, and it reports a run that ended in an error as exactly that.

= Three versions in one day

#figure-image("figures/evolution.svg", caption: [The three layouts, drawn schematically. Window tabs are along the bottom of each frame, as tmux draws them.], source: [Source: commit messages `859d3f0`, `4d93f6e`, `ade244b`; drawn for this report, not captured.])

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
Version 2 followed four hours later and addressed the obvious complaint, which was width. The overview got the `hx:workers` window to itself and transcripts moved to `hx:worker-N` windows of at most two panes, so every pane stayed wide enough to read. Placement was made independent of run state so a finishing worker could not drag its pane elsewhere. The suite grew from «tests_v1» to «tests_v2» tests.

It passed its tests and was deployed. Then it was rendered against the actual state of the machine.

= What version 2 looked like on a real day

#figure-image("figures/img/v2-overview.png", caption: [Version 2 overview, left 96 columns. Version 2 code on a private tmux socket, reading the day's real delegation history at 17:58.], source: [Source: `ux-redesign/baseline/v2-overview.png`, cropped.], full-width: true)

// claim: B1
Nothing was running, and the screen was full. The overview was a seven-column table keyed by delegation id, with forty rows of history and every state reading `done`. The footer counted two failures that no visible row showed. The tab for the window carried a permanent `!`, because two old failures could never be acknowledged.

#figure-image("figures/img/v2-tabs.png", caption: [Version 2 status line. Four numbered worker windows, all holding finished work.], source: [Source: `ux-redesign/baseline/v2-overview.png`, bottom row.], full-width: true)

// claim: B2
The four worker tabs held eight finished transcripts, the oldest nine hours old. Each pane was a raw `tail -F`: no colour, every message repeated by the writer, and a kickoff line that filled the pane before any work appeared.

#figure-image("figures/img/v2-worker.png", caption: [Version 2 worker window, left 96 columns, top 30 rows. Two stacked panes; identity is the border label.], source: [Source: `ux-redesign/baseline/v2-worker-1.png`, cropped.], full-width: true)

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

#figure-image("figures/img/prod-overview.png", caption: [Version 3 overview with two real workers running. Production session, 19:32, columns 35 to 152 of 187.], source: [Source: `case-study/shots/prod-01-overview-running.heic`, cropped. The workers are the two investigations dispatched to gather sources for this report.], full-width: true)

// claim: D1
The overview is now three sections: what is running, what needs attention, and what finished recently. Each entry leads with the task as the parent wrote it. Model, elapsed time, the current tool call and the identifier are secondary and dimmer. History only ever takes the rows the live sections leave free.

#figure-image("figures/img/prod-tabs.png", caption: [Version 3 status line in production, left 100 columns. State is the glyph; the name is the task.], source: [Source: `case-study/shots/prod-02-worker-running.heic`, bottom row. `hxview` is a second view of the production session, used so the capture did not change the user's active window.], full-width: true)

A tab's name is the first clause of the goal, set once and never changed. Articles are dropped, paths reduce to a basename, and words shared by every sibling in a delegation are dropped from the front so narrow tabs still differ. Truncation happens in the status line, per client, from that client's width: the same session shows full names on a wide terminal and bare glyphs on a narrow one, with no renames.

== Lifecycle

#figure-image("figures/lifecycle.svg", caption: [Life of a worker tab. Times are the defaults.], source: [Source: `hermes_tmux/watch.py` and `render.py` constants; drawn for this report.])

// claim: L1
The lifecycle is the part that fixed the real problem. A tab exists for live work, which includes work already running when the workspace starts. A finished tab lingers «linger_min» minutes after it settled or was last looked at, and never closes while it is in view. A run that ended in an error keeps its tab until someone has looked at it; looking is the acknowledgement. At most «tab_cap» tabs exist, and anything past that is listed in the overview.

#data-table(
  caption: [Lifecycle rules and their constants],
  source: [Source: `hermes_tmux/watch.py`, `hermes_tmux/render.py` at `«docs_commit»`.],
  head: ("Rule", "Value", "Where"),
  rows: (
    ("Finished tab lingers", "300 s", "`LINGER_S`"),
    ("Tabs open at once", "«tab_cap»", "`MAX_TABS`"),
    ("Unseen failure stops asking for attention", "«attention_h» h", "`ATTENTION_TTL_S`"),
    ("Worker shown as silent, never as failed", "«stall_min» min without an event", "`STALL_AFTER_S`"),
    ("Overview text column", "«content_cols» columns, centred", "`MAX_CONTENT_WIDTH`"),
  ),
  widths: (2.2fr, 1.5fr, 1.2fr),
  aligns: (left, left, left),
)

A tab the reader closes stays closed. Version 2 recreated it, which was the tool overriding the reader. The worker stays listed in the overview, and `hermes-tmux open` with its identifier brings the transcript back as a pinned tab.

== A run that ended is not a task that failed

#figure-image("figures/img/lab-attention.png", caption: [A run that ended with a provider error. Synthetic: lab socket, a real transcript replayed with an injected 529.], source: [Source: `ux-redesign/final/01-full-overview.png`, cropped.], full-width: true)

// claim: D2
In this capture the worker had finished its work and reported eight passing tests when the provider failed. A red mark that said "failed" would have been false. The overview says how the run ended, quotes the error, and then quotes the worker's own last report beside it. The display does not decide which one matters.

= Reading a transcript

#figure-image("figures/img/prod-worker.png", caption: [Version 3 worker tab in production, left 96 columns, top 15 rows. A real worker, 19:33.], source: [Source: `case-study/shots/prod-02-worker-running.heic`, cropped.], full-width: true)

// claim: T1
Worker panes run `hermes-tmux tail` instead of `tail -F`. It is presentation only: the file path is printed first, lines it does not recognise are shown verbatim, and `HERMES_TMUX_TRANSCRIPT=raw` restores plain `tail`. Tool calls and their results take one line each, the writer's duplicate of every message is collapsed, and colour separates narration from tool traffic.

The wrapping decision is the one visible compromise. Each event is written as one terminal line and the terminal wraps it. Continuation lines therefore start at the left edge rather than under the text. In exchange, a resize re-wraps natively, the follower never redraws, and scrolling back while output arrives leaves the view where the reader put it.

= One implementation, three terminals

#figure-image("figures/img/lab-narrow-overview.png", caption: [Overview at 61 by 24. Synthetic lab workers. Tabs have collapsed to number and glyph.], source: [Source: `ux-redesign/final/08-narrow-overview.png`.], width: 80%)

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
The inner loop was build, render, look. A small generator replays real transcripts into a private state directory as if they were live, so any mix of running, finished and failed workers can be on screen in seconds without launching an agent. The logic that decides what to show is tested without tmux at all: «pure_tests» tests in «pure_s» seconds.

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

#figure-image("figures/img/prod-overview-done.png", caption: [Production overview after both workers finished, 19:39. The earlier worker has lost its tab and remains in the list.], source: [Source: `case-study/shots/prod-04-overview-completed.heic`, cropped.], full-width: true)

// claim: G3
The full suite, «tests_v3» tests, passes in «tests_v3_s» seconds on private sockets and leaves no tmux server behind. Rollback was rehearsed on a private socket rather than assumed: version 3 up and down, the version 2 tag up and down, version 3 again, with a user shell surviving every step.

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
  source: [Source: `docs/DESIGN.md` section 5 at `«docs_commit»`; wording condensed.],
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
  [Repository `archpc/hermes-tmux-workspace` (private). Version 2 at `«v2_commit»`, tag `v2-two-per-window`; version 3 at `«v3_commit»`, pull request 1; documentation corrections at `«docs_commit»`.],
  [`docs/DESIGN.md`, sections 1 to 5, at `«docs_commit»`. Snapshot in `evidence/DESIGN.md`.],
  [Independent review transcripts, rounds 1 and 2. `evidence/glm/`.],
  [Deployment and production records. `evidence/deploy/`.],
  [Working journal, entries 01 to 05, as written to the Apple Note "Hermes tmux Workspace -- UX Redesign". `evidence/journal/`.],
  [Test run at `«docs_commit»`. `evidence/test-run-main.txt`, `evidence/dev/pure-suites.txt`.],
))
"""

# --- evidence ----------------------------------------------------------------
def rec(path, quote):
    return {"type": "record", "path": path, "quote": quote}


CLAIMS = [
    ("S1", "observed", "v3 deployed; real worker appeared, finished, lingered, retired; review FAIL then PASS", [
        rec("deploy/live-running.txt", "4 @10 [Check workspace docs…] panes=1 active=0"),
        rec("deploy/live-retired.txt", "19:33:39 layout tabs: deleg_bcaf50c1#0 absent"),
        rec("glm/review-1.txt", "lost update between `reconcile_tabs` and `cmd_open`"),
        rec("glm/review-2.txt", "The split pane has no own `@hx`"),
    ]),
    ("P1", "observed", "Hermes writes one append-only transcript per delegated task", [
        rec("DESIGN.md", "$HERMES_HOME/cache/delegation/live/<deleg_id>/task-<n>.log"),
        rec("DESIGN.md", "hermes-tmux  (python3, stdlib only, single process per tmux session)"),
    ]),
    ("P2", "observed", "Hermes records neither review status nor quality of work", [
        rec("DESIGN.md", "Hermes has no concept of \"reviewing a delegation\"."),
        rec("DESIGN.md", "Hermes records how the run stopped. It does not record whether the work is good, so the display does not say."),
    ]),
    ("V1", "observed", "v1: one window with overview plus up to four tail panes; defects found on real tmux", [
        {"type": "commit", "path": "git-log-main.txt", "repo": REPO, "rev": "859d3f0"},
        rec("history.md", "pane1 overview (hermes-tmux follow) + up to 4 x tail -F task-N.log"),
        rec("DESIGN.md", "one idle coordinator process accumulated per closed session"),
        rec("DESIGN.md", "ownership marker read as `{@hx}` (literal) instead of `#{@hx}`"),
    ]),
    ("V2", "observed", "v2: overview-only window, two transcripts per worker window; 142 then 166 tests", [
        {"type": "commit", "path": "git-log-main.txt", "repo": REPO, "rev": "4d93f6e"},
        rec("DESIGN.md", "Version 2 put the overview in `hx:workers` and two transcripts in each"),
        rec("history.md", "142 tests on a private socket (gate default included)"),
        rec("history.md", "counting `def test_` in `git show a17522d:tests` gives 166"),
    ]),
    ("B1", "observed", "v2 overview under real history: 7-column table, 40 rows, permanent attention badge", [
        rec("journal/entry-01.html", "Overview is a 7-column table keyed by opaque ids, monochrome, 40 history rows"),
        rec("DESIGN.md", "An alert that cannot clear is not an alert."),
    ]),
    ("B2", "observed", "v2 worker tabs held eight finished transcripts up to nine hours old", [
        rec("journal/entry-01.html", "hold 8 finished transcripts up to 9 h old; nothing running"),
        rec("DESIGN.md", "transcripts up to nine hours old, numeric tab names, a seven-column table keyed"),
    ]),
    ("R1", "observed", "role tabs and per-delegation windows rejected; names never change", [
        rec("DESIGN.md", "Role tabs would need a classifier reading prose, and a wrong label is worse than none."),
        rec("DESIGN.md", "Three siblings either break the ceiling or get split anyway;"),
        rec("DESIGN.md", "The name never changes; `window-status-format` truncates it per client from"),
    ]),
    ("D1", "observed", "overview is three sections, task title first", [
        rec("DESIGN.md", "Sections RUNNING / NEEDS ATTENTION / RECENT, task title first, details dimmer"),
        rec("shots/prod-01-overview-running.txt", "Reconstruct the v1 and v2 history of the Hermes tmux workspace from its repository"),
    ]),
    ("L1", "observed", "lifecycle: live work only, 5-minute linger, never while in view, cap of eight", [
        rec("DESIGN.md", "Tabs only for live work; finished tabs linger, then leave; never while in view"),
        rec("code/watch.py", "#: How long a finished worker keeps its tab after it settles or was last"),
        rec("code/watch.py", "#: full window size and makes a tab's state exactly one worker's state. Eight"),
        rec("code/render.py", "STALL_AFTER_S = 600  # 10 minutes; display-only, never a failure claim"),
    ]),
    ("D2", "observed", "a run that ended in an error is reported as such, with the worker's last report", [
        rec("DESIGN.md", "Hermes records how the run stopped. It does not record whether the work is good, so the display does not say."),
        rec("journal/entry-03.html", "the failed worker in the lab screenshots is synthetic"),
    ]),
    ("T1", "observed", "styled follower is presentation only; one terminal line per event", [
        rec("DESIGN.md", "each event ONE terminal line, soft-wrapped by the terminal"),
        rec("journal/entry-03.html", "You rejected the replay (loses scrol"),
    ]),
    ("X1", "observed", "tmux-native; rendered in iTerm2 and Ghostty, smoke-tested in Terminal.app", [
        rec("journal/entry-04.html", "a real mouse click on a status-line tab switched wi"),
        rec("DESIGN.md", "Legible on light and dark themes; no truecolor dependency"),
    ]),
    ("H1", "observed", "parallel DeepSeek workers on disjoint files reported real defects", [
        rec("journal/entry-04.html", "three DeepSeek workers in parallel on disjoint files"),
        rec("journal/entry-05.html", "a clipped transcript line could overflow a narrow pane and cut its own"),
    ]),
    ("I1", "observed", "synthetic replay lab; pure suites run in well under a second; renders found defects", [
        rec("dev/pure-suites.txt", "Ran 231 tests in 0.142s"),
        rec("journal/entry-04.html", "tmux reports run-shell jobs that"),
        rec("journal/entry-03.html", "coordinator ignored new delegations for up to 20 s"),
    ]),
    ("G1", "observed", "round 1 failed on a lost update between reconcile and open", [
        rec("glm/review-1.txt", "lost update between `reconcile_tabs` and `cmd_open`"),
        rec("DESIGN.md", "the pass snapshots what it read and merges only its own changes"),
    ]),
    ("G2", "observed", "round 2 passed; reviewer withdrew the inherited-marker claim by experiment", [
        rec("glm/review-2.txt", "NB 1 (round 1): refuted by experiment."),
        rec("glm/review-2.txt", "The split pane has no own `@hx`"),
    ]),
    ("G3", "observed", "308 tests pass; rollback rehearsed with a user shell surviving", [
        rec("test-run-main.txt", "Ran 308 tests in 109.503s"),
        rec("test-run-main.txt", "leftover private tmux servers: 0"),
        rec("deploy/rollback-rehearsal.txt", "-- checkout tag v2-two-per-window, up"),
        rec("deploy/rollback-rehearsal.txt", "user pane alive: %104 zsh"),
    ]),
    ("M1", "observed", "three mistakes: deleted screenshots, leaked linger setting, misread timing", [
        rec("journal/entry-05.html", "I deleted the Desktop product screenshots before the replacements existed."),
        rec("journal/entry-05.html", "A lab setting leaked into the first production start."),
        rec("dev/tests-baseline-v3.txt", "test_tmux_integration 279s :: Ran 33 tests in 278.593s  FAILED (failures=24, errors=1)"),
    ]),
]

OPEN = [
    ("G4", "No production worker ended in an error during validation; the failure path is shown from synthetic lab workers.",
     "No real delegation failed in the production session during the validation window, and none was induced."),
]


def evolution_svg():
    ink, muted, rule, surface, accent, add = "#1D1D1F", "#6E6E73", "#C7C7CC", "#F5F5F7", "#0071E3", "#1B7F4B"
    W, H = 836, 372
    o = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
         'font-family="SF NS, Helvetica Neue, Helvetica, sans-serif">' % (W, H, W, H)]

    def frame(x, title, sub):
        o.append('<text x="%d" y="22" font-size="20" font-weight="600" fill="%s">%s</text>' % (x, ink, title))
        o.append('<text x="%d" y="46" font-size="15.5" fill="%s">%s</text>' % (x, muted, sub))
        o.append('<rect x="%d" y="60" width="256" height="216" rx="5" fill="%s" stroke="%s"/>' % (x, surface, rule))
        o.append('<rect x="%d" y="248" width="256" height="28" fill="#E5E5EA"/>' % x)

    def pane(x, y, w, h, label, stroke=rule, colour=muted):
        o.append('<rect x="%d" y="%d" width="%d" height="%d" rx="2" fill="#FFFFFF" stroke="%s"/>' % (x, y, w, h, stroke))
        o.append('<text x="%d" y="%d" font-size="14.5" fill="%s">%s</text>' % (x + 7, y + 20, colour, label))

    def tabs(x, items):
        cx = x + 6
        for text, on in items:
            w = int(7.6 * len(text)) + 12
            if on:
                o.append('<rect x="%d" y="252" width="%d" height="20" rx="2" fill="#FFFFFF" stroke="%s"/>' % (cx, w, rule))
            pad = 6 + 5 * (len(text) - len(text.lstrip()))
            o.append('<text x="%d" y="267" font-size="13.5" fill="%s">%s</text>' % (cx + pad, ink if on else muted, text.lstrip()))
            cx += w + 3

    def note(x, lines):
        for i, line in enumerate(lines):
            o.append('<text x="%d" y="%d" font-size="15.5" fill="%s">%s</text>' % (x, 302 + i * 21, ink, line))

    x = 0
    frame(x, "Version 1", "13:32 · one window")
    pane(x + 8, 68, 92, 174, "overview")
    for i in range(4):
        pane(x + 106, 68 + i * 44, 142, 40, "tail -F worker %d" % (i + 1))
    tabs(x, [("hermes", False), ("hx:workers", True)])
    note(x, ["Panes narrow as workers", "are added. Identity is", "a border label."])

    x = 290
    frame(x, "Version 2", "17:33 · two per window")
    pane(x + 8, 68, 240, 84, "tail -F worker 1")
    pane(x + 8, 158, 240, 84, "tail -F worker 2")
    tabs(x, [("hx:workers", False), ("hx:worker-1", True), ("hx:wor…", False)])
    note(x, ["Readable width, numbered", "tabs. Finished work keeps", "its window."])

    x = 580
    frame(x, "Version 3", "19:22 · one worker per tab")
    pane(x + 8, 68, 240, 174, "RUNNING  Port the test suite", stroke=accent, colour=ink)
    for i in range(7):
        o.append('<rect x="%d" y="%d" width="%d" height="5" rx="2.5" fill="%s"/>' % (x + 18, 104 + i * 19, (204, 172, 216, 122, 190, 152, 96)[i], rule))
    tabs(x, [("agents", False), ("   Port test s…", True), ("Review…", False)])
    o.append('<circle cx="%d" cy="262" r="4" fill="%s"/>' % (x + 77, add))
    note(x, ["Tab named for the task,", "state as a glyph. Tabs exist", "only for live work."])
    o.append("</svg>")
    return "\n".join(o)


def lifecycle_svg():
    ink, muted, rule, accent, add, dele = "#1D1D1F", "#6E6E73", "#C7C7CC", "#0071E3", "#1B7F4B", "#B3261E"
    W, H = 836, 268
    o = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
         'font-family="SF NS, Helvetica Neue, Helvetica, sans-serif">' % (W, H, W, H)]
    o.append('<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
             '<path d="M0,0 L10,5 L0,10 z" fill="%s"/></marker></defs>' % muted)

    def box(x, y, w, title, sub, colour):
        o.append('<rect x="%d" y="%d" width="%d" height="60" rx="4" fill="#FFFFFF" stroke="%s" stroke-width="1.6"/>' % (x, y, w, colour))
        o.append('<text x="%d" y="%d" font-size="17" font-weight="600" fill="%s">%s</text>' % (x + 12, y + 25, ink, title))
        o.append('<text x="%d" y="%d" font-size="14.5" fill="%s">%s</text>' % (x + 12, y + 46, muted, sub))

    def arrow(x1, y1, x2, y2):
        o.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="1.4" marker-end="url(#a)"/>' % (x1, y1, x2, y2, muted))

    def label(x, y, text, colour=muted):
        o.append('<text x="%d" y="%d" font-size="14" fill="%s">%s</text>' % (x, y, colour, text))

    # top row: the ordinary life
    box(0, 28, 176, "Dispatched", "tab opens, detached", accent)
    box(220, 28, 176, "Running", "live transcript", add)
    box(440, 28, 176, "Done", "lingers 5 minutes", muted)
    box(660, 28, 176, "Retired", "listed in overview", rule)
    arrow(176, 58, 218, 58)
    arrow(396, 58, 438, 58)
    arrow(616, 58, 658, 58)
    label(566, 18, "once out of view")
    # bottom row: the two exceptions
    box(220, 150, 176, "Closed by reader", "stays closed", rule)
    box(440, 150, 176, "Run ended in error", "waits to be seen", dele)
    arrow(280, 88, 280, 148)
    arrow(360, 88, 470, 148)
    label(420, 124, "error")
    arrow(616, 180, 730, 90)
    label(690, 150, "seen, then 5 min")
    label(0, 250, "Any retired or closed worker: hermes-tmux open brings the transcript back as a pinned tab.", accent)
    o.append("</svg>")
    return "\n".join(o)


def main():
    body = BODY
    for key, value in F.items():
        body = body.replace("«%s»" % key, value)
    assert "«" not in body, "unfilled fact"
    # Typst resolves image paths from the project root (the home directory).
    body = body.replace('figure-image("figures/',
                        'figure-image("/%s/figures/' % HERE.relative_to(Path.home()).as_posix())
    (HERE / "report.typ").write_text(body.lstrip("\n"))
    (HERE / "report.json").write_text(json.dumps(META, indent=2, ensure_ascii=False) + "\n")
    claims = [{"id": i, "kind": k, "statement": s, "refs": r} for i, k, s, r in CLAIMS]
    claims += [{"id": i, "kind": "open", "statement": s, "open_reason": why, "refs": []} for i, s, why in OPEN]
    manifest = {"schema": "research-evidence/1", "report_id": META["report_id"], "root": "../evidence", "claims": claims}
    (HERE / "report.evidence.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    (HERE / "figures").mkdir(exist_ok=True)
    (HERE / "figures" / "evolution.svg").write_text(evolution_svg())
    (HERE / "figures" / "lifecycle.svg").write_text(lifecycle_svg())
    print("wrote report.typ, report.json, report.evidence.json, 2 diagrams; %d claims" % len(claims))
    if "--build" in sys.argv:
        extra = [a for a in sys.argv[1:] if a != "--build"]
        cmd = [sys.executable, str(SKILL / "scripts/build.py"), "--content", str(HERE / "report.typ"),
               "--meta", str(HERE / "report.json"), "--out", str(OUT), "--inspect"] + extra
        sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
