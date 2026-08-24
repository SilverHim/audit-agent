# Stage 1 deliverables — FinReview Co.

Submission package for ELEC5620 Stage 1 (report 15 + video 5 + interview 10). **No prototype code** in this folder.

## What to upload to Canvas

1. **PDF of the formal report** — start from [FinReview_Stage1_Report.md](FinReview_Stage1_Report.md), paste exported UML PNGs from `diagrams/*.puml`, replace Member A/B/C with names and SIDs.
2. **Video ≤ 8 minutes** — script in [video_script.md](video_script.md). Attach a YouTube link if you have one.
3. Interview is in practicals week 11–12 (not a file). Drill with [interview_prep.md](interview_prep.md).

Assignment 1 submission time controls interview order (earlier A1 → later slot).

## Document map

| File | Role |
|---|---|
| [FinReview_Stage1_Report.md](FinReview_Stage1_Report.md) | Formal report (all Project Description §1 sections) |
| [01-scope-and-requirements.md](01-scope-and-requirements.md) | Frozen AGR/MAN/OPT, ad hoc requirements, discarded choices |
| [02-use-case-specifications.md](02-use-case-specifications.md) | Full UC-01–UC-09 |
| [03-architecture-and-traceability.md](03-architecture-and-traceability.md) | Viewpoints, matrix, contribution % |
| [video_script.md](video_script.md) | 8-minute narration |
| [interview_prep.md](interview_prep.md) | Per-member pitches and likely questions |
| [diagrams/](diagrams/) | PlantUML for every scoring diagram |

## Marking checklist

- [ ] Each member: 1 ad hoc requirement, ≥1 UC spec, 1 activity, 1 interaction, 1 state machine, **same story line**
- [ ] Group: 5–10 use cases (we have 9), 1 feature diagram with NFRs, 1 UC overview
- [ ] Class diagram uses generalisation, composition, aggregation, interfaces, multiplicity
- [ ] Complex structure: object + structured class + collaboration (not a second class diagram)
- [ ] Optional: ad hoc overview, package, deployment, viewpoints — included
- [ ] Contribution table filled with real names
- [ ] Video covers requirements classification, design rationale including discarded choices, model overview, change/acceptance
- [ ] Ten-second rule: any activity node → REQ + UC

## Render diagrams

```text
plantuml docs/stage1/diagrams/*.puml
```

Or paste a `.puml` file into https://www.plantuml.com/plantuml
