# Case study

`Hermes-tmux-Workspace-Case-Study.pdf` is a 15-page account of this tool from
version 1 to version 3: the problem, the three layouts, the designs that were
rejected, the lifecycle rules, how it was built and reviewed, what was observed
in production, and what went wrong along the way.

| Path | What it is |
|---|---|
| `Hermes-tmux-Workspace-Case-Study.pdf` | the report |
| `*.pdf.build.json`, `*.pdf.evidence.json` | build manifest and evidence sidecar; all three share one generation hash |
| `report/make_report.py` | facts, prose and the two diagrams; regenerates `report.typ`, `report.json`, `report.evidence.json` |
| `report/figures/img/` | screenshot crops used in the report |
| `report/crop.py` | how the crops were cut from the full-window captures (the originals are not in this repository) |
| `evidence/` | snapshot of every record the report's claims point at: design record, review transcripts, deployment and production records, journal entries, test runs |

## Rebuild

Needs Typst and the `research-pdf` skill (design system 1.2.0).

    python3 docs/case-study/report/make_report.py --build

Two builds from the same inputs produce the same PDF bytes.

## Reading the figures

Captions say which captures are real and which are synthetic. "Production" is
the real `main` tmux session after deployment. "Lab, synthetic" is the final
build on a private tmux server replaying real transcripts, one with an injected
provider error. No production worker ended in an error during validation; the
report says so where it matters.
