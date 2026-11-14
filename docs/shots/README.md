# Product shots

Real captures of the workspace running in tmux, used by the README. No mockups,
no re-typed output: each file is a screenshot, cropped or scaled only.

| File | Shows | Source |
|---|---|---|
| `01-agents-overview.png` | The `agents` window at full width: RUNNING, NEEDS ATTENTION, RECENT, with tab numbers and worker ids | `ux-redesign/final/01-full-overview.png`, scaled to 1500 px |
| `02-worker-running.png` | A worker tab mid-run: border (state, task, model), transcript following the file | `ux-redesign/final/02-full-worker-running.png`, scaled to 1500 px |
| `03-worker-run-ended-with-error.png` | A worker whose run ended `provider_error`; the transcript ends with the quoted last report | `ux-redesign/final/10-full-worker-run-ended-with-error.png`, scaled to 1500 px |
| `04-narrow-overview.png` | The overview in a narrow client (996 px window) | `ux-redesign/final/08-narrow-overview.png`, scaled to 1500 px |
| `05-tab-bar.png` | The status line alone: session, worker tabs with state glyphs, `agents` badge | cropped from `01-agents-overview.png` |

Capture conditions, so a reader can tell what a shot proves and what it does not:

- All five come from a lab tmux server on a private socket with a synthetic
  `HERMES_HOME`, not from the author's live session. The layout, renderer and
  data sources are the shipped ones; the workers and tasks in the frames are lab
  fixtures.
- The session is named `lab`; a real session carries whatever name you gave it.
- Terminal: the macOS window with the `⌥⌘2` label is Ghostty; captures 01-04
  were taken at a `1800x~870` client, 04 at 996 px wide.
- Timestamps, durations and worker ids are from the capture moment and are not
  reproducible.
- Text that looks truncated (`…`) is truncated by the renderer or by tmux on
  purpose; it is not cropping in the screenshot.
- Captures `01`–`05` were recorded during the v3 redesign prior to commit
  `3f29699` (`closemark.py`), so worker tabs in `02` and `03` do not yet show
  the trailing `×` close mark, and `05` has `agents*` active in view.

The case study in `docs/case-study/` uses a larger set from the same sessions,
including the version-2 layout for comparison:
`docs/case-study/report/figures/img/`.
