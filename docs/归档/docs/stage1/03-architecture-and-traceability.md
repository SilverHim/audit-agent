# Architecture analysis, life cycle, and traceability

## 1. Viewpoints (optional architecture analysis)

| Viewpoint | Question answered | Primary models |
|---|---|---|
| Logical | What concepts and contracts exist? | Class diagram, interfaces `ILLMClient` / `ITool` |
| Process / collaboration | How do agents work a job? | Sequence, activity, collaboration, `ReviewJob` SM |
| Development | How will Stage 2 be packaged? | Package diagram |
| Physical | Where do processes run? | Deployment diagram |
| Life cycle | How does an engagement live and die? | `Engagement` SM + this section |

The orchestrator-and-specialists style is a **process view** choice: control is centralised in `OrchestratorAgent`, domain state lives in `Engagement` / `Finding`, and LLM I/O is behind `ILLMClient` so providers can change (feature alternative `F-LLMProvider`).

## 2. System life cycle (engagement)

```text
Brief (UC-01) → Scope + policy (UC-09 include)
  → Intake (UC-02)
  → Analytical procedures (UC-03, UC-04, optional UC-05/06)
  → Human gate (UC-07)
  → Adaptive patch if needed (UC-08)
  → Issued memo or cancelled
```

Support components (not principal agents) and their life-cycle role:

| Component | Role | Failure behaviour |
|---|---|---|
| DocumentTool | Bytes → tables | Engagement stays `DocumentsIn` |
| CalculatorTool | Deterministic arithmetic | Task fails closed; no LLM numbers |
| LLMService | Perception, decision, interaction | Draft stays; job queues (ASM-05) |
| Object store | Source artefacts | Immutable blobs; parse is derived |
| StandardsStore | Optional RAG | UC-06 fails closed |
| MarketDataAPI | Optional peers | Ratios continue without bands |

## 3. LLM roles (perception, decision, interaction)

| Role | Where | Guardrail |
|---|---|---|
| Perception | UC-01 brief; UC-02 table mapping; UC-06 notes vs checklist | Schema-constrained outputs; human accept of parse |
| Decision | UC-01 work plan; UC-08 replan patch | Dependency graph validates patches |
| Interaction | Clarifying questions; explanations; memo prose; HITL wording | Numeric evidence from tools; Confirm disabled without locator |

## 4. Relationships that support future work

- New specialist **subclasses `Agent`** and binds to orchestrator **ports** (`planOut`, `findingIn`) — see structured class diagram. Domain types `Finding` and `Evidence` do not change.
- Optional features are **policy flags** on `ReviewPolicy`, not forks of `Engagement`.
- `ILLMClient` / `ITool` keep cloud vs local LLM and new tools off the domain model (D1/D3 leftovers).

## 5. Change assessment and acceptance

**Change types**

1. Agreement change (AGR-*) — requires group re-approval; likely new UC extensions (e.g. allowing auto-issue would break HITL and Member C’s state machine).
2. Mandatory feature change — impact via feature diagram; Class/TaskKind may extend; prototype scope changes.
3. Optional feature toggle — `ReviewPolicy` + new Agent subclass; acceptance = UC postcondition of the optional UC.
4. Editorial / NFR tightening — usually diagram notes and test oracles in Stage 2.

**Acceptance determination:** a change is accepted when (a) the traceability matrix row is updated, (b) the owning member’s ad hoc story still holds if the change touches a `*` artefact, and (c) UC postconditions remain the oracle for Stage 2 tests.

**Impact heuristic:** walk the feature diagram from the changed leaf to the root; any mandatory sibling that shares a class (e.g. `Finding`) needs a regression of AGR-02.

## 6. Traceability matrix

| Requirement | Use cases | Structure | Activity | Interaction | State | Owner |
|---|---|---|---|---|---|---|
| REQ-A-01 | UC-01, UC-02 | `Engagement`, `WorkPlan`, `ParserAgent` | member-a-activity (UC-02) | member-a-sequence (UC-01→02) | member-a-state `Engagement` | A |
| REQ-B-01 | UC-03, UC-04, UC-08 | `ReviewJob`, `WorkPlan.patch`, `ConsistencyAgent`, `RatioAgent` | member-b-activity (UC-03) | member-b-sequence | member-b-state `ReviewJob` | B |
| REQ-C-01 | UC-05, UC-06, UC-07 | `Finding`, `Evidence`, `ReviewReport` | member-c-activity (UC-07) | member-c-sequence | member-c-state `Finding` | C |
| AGR-01 | UC-07 | `ReviewReport.banner` | member-c-activity | member-c-sequence | — | Group |
| AGR-02 | UC-07 | `Finding * Evidence` 1..* | member-c-activity | member-c-sequence | Confirm guard | C / Group |
| AGR-03 | — | `llm` package; deployment note | — | — | — | Group |
| MAN-01 | all | `OrchestratorAgent` → `Agent` | — | all sequences | — | Group |
| MAN-07 | UC-01,03,07,08 | `ILLMClient` | all activities | all sequences | notes on SM | Group |
| MAN-09 | UC-08 | `ReviewJob.replanCount` | — | member-b-sequence | `Replanning` | B |
| OPT-01 | UC-05 | `AnomalyAgent` | — | member-c-sequence (opt) | Finding SM | C |
| OPT-02 | UC-06 | `ComplianceAgent` | — | member-c-sequence (opt) | Finding SM | C |
| UC-09 | UC-09 | `ReviewPolicy` | — | — | Engagement `Scoped` | Group |

## 7. Contribution matrix (group tasks)

Replace Member A/B/C with legal names before Canvas upload. Individual (`*`) artefacts are 100% the named author.

| Artefact | Type | A | B | C |
|---|---|---|---|---|
| Ad hoc requirement | * | REQ-A-01 | REQ-B-01 | REQ-C-01 |
| Use case specs | * | UC-01, UC-02 | UC-03, UC-04, UC-08 | UC-05, UC-06, UC-07 |
| Activity diagram | * | UC-02 | UC-03 | UC-07 |
| Interaction diagram | * | UC-01 | UC-03/04 | UC-07 |
| State machine | * | Engagement | ReviewJob | Finding |
| Feature diagram | Group | 35% (intake/identity/LLM alt.) | 35% (analysis/adapt) | 30% (reporting/optional assurance) |
| Use case overview | Group | 34% | 33% | 33% |
| Class diagram | Group | 30% (intake types) | 35% (job/plan/tools) | 35% (finding/report) |
| Object diagram | Group | 30% | 40% | 30% |
| Structured class | Group | 20% | 50% | 30% |
| Collaboration | Group | 20% | 40% | 40% |
| Package / deployment | Group | 30% | 40% | 30% |
| Requirements classification | Group | 34% | 33% | 33% |
| Report prose / video | Group | Intro + intake models | Architecture + analysis models | HITL + change/acceptance |
| UC-09 spec | Group | equal | equal | equal |
