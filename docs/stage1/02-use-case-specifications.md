# FinReview Co. — Use Case Specifications

Group total: **9 use cases** (within the required 5–10). Each member owns at least two specifications. UC-09 is a short shared policy use case.

**Overview diagram:** [diagrams/use-case-overview.puml](diagrams/use-case-overview.puml)

Traceability: ad hoc requirement → primary use case → that member’s activity, sequence, and state machine.

| UC | Name | Author | Ad hoc | Primary behaviour diagrams? |
|---|---|---|---|---|
| UC-01 | Create Review Engagement | Member A | REQ-A-01 | Sequence, State (`Engagement`) |
| UC-02 | Upload and Parse Statements | Member A | REQ-A-01 | Activity |
| UC-03 | Cross-Statement Consistency Review | Member B | REQ-B-01 | Activity, Sequence |
| UC-04 | Ratio and Trend Analysis | Member B | REQ-B-01 | — (specified; supports B’s sequence) |
| UC-05 | Detect Anomalies | Member C | REQ-C-01 | — (optional feature; feeds Finding) |
| UC-06 | Check Disclosure Completeness | Member C | REQ-C-01 | — (optional) |
| UC-07 | Generate and Approve Review Report | Member C | REQ-C-01 | Activity, Sequence, State (`Finding`) |
| UC-08 | Adaptive Re-Review | Member B | REQ-B-01 | State (`ReviewJob`) |
| UC-09 | Manage Review Policy | Group | — | — |

---

## UC-01 Create Review Engagement

**Author:** Member A  
**Ad hoc requirement:** REQ-A-01  
**Level:** User goal  
**Primary actor:** Practitioner  
**Supporting:** OrchestratorAgent, LLMService  
**Goal:** Open a scoped engagement with an initial work plan and a document request list.

### Preconditions

- Practitioner is authenticated (MAN-10).
- No open draft is required; a new engagement may be created at any time.

### Postconditions (success)

- An `Engagement` exists in state `Scoped`.
- `ReviewPolicy.materiality` is stored.
- A `WorkPlan` lists mandatory tasks (intake, consistency, ratios, report) and enabled optional tasks.
- The practitioner is shown which statement types are still missing.

### Main success scenario

1. Practitioner opens “New engagement” and types a brief (entity, period, emphasis, materiality cue).
2. OrchestratorAgent sends the brief to LLMService with a scoping schema (entity, period, framework, materiality, emphasis areas, required artefacts).
3. LLMService returns a structured scope object.
4. OrchestratorAgent creates `Engagement` (state `Draft` then `Scoped`) and a `WorkPlan`.
5. System displays the interpreted scope and the upload checklist.
6. Practitioner confirms or edits materiality / emphasis; system stores the confirmed policy.

### Extensions

- 2a. LLMService timeout: engagement remains `Draft`; practitioner may retry or fill a short structured form.
- 3a. Scope object missing period or entity: OrchestratorAgent asks a clarifying question (LLM *interaction*); resume at 2.
- 6a. Practitioner rejects the interpretation: return to 1 with the previous brief retained.

### Special requirements

- Scoping is advisory; AGR-01 still applies (no audit opinion language in the plan).
- NFR: interpretability — the UI must show the structured fields derived from the brief.

---

## UC-02 Upload and Parse Statements

**Author:** Member A  
**Ad hoc requirement:** REQ-A-01 (supporting)  
**Level:** User goal  
**Primary actor:** Practitioner  
**Supporting:** ParserAgent, LLMService, DocumentTool  
**Goal:** Persist structured `FinancialStatement` / `LineItem` graphs for the engagement.

### Preconditions

- `Engagement` is `Scoped` (UC-01).
- At least one file is selected (PDF, Excel, or XBRL).

### Postconditions (success)

- Required statement types for the work plan are present as `FinancialStatement` objects with `LineItem` children.
- `Engagement` is `Parsed` if parsing succeeded, else `DocumentsIn` awaiting correction.
- Extraction provenance (file name, page/sheet) is stored on line items where available.

### Main success scenario

1. Practitioner uploads one or more files and tags intended statement types.
2. ParserAgent calls DocumentTool to obtain raw text/tables.
3. ParserAgent calls LLMService to map tables to a canonical chart of accounts / statement schema.
4. ParserAgent writes `FinancialStatement` and `LineItem` instances (composition).
5. System shows a reconciliation preview (totals, unmapped rows).
6. Practitioner accepts the parse; `Engagement` becomes `Parsed` then `ReadyForReview` if the work plan’s intake gate is satisfied.

### Extensions

- 2a. Encrypted or unreadable PDF: status `Failed` on that artefact; practitioner uploads a replacement.
- 3a. Low-confidence mapping: rows flagged; practitioner maps them manually; ParserAgent does not invent amounts (ASM-03 analogue for intake).
- 5a. Totals do not match the source: remain in `DocumentsIn`; block UC-03 until accepted or overridden with a comment.

### Behaviour diagrams (Member A)

- Activity diagram models this scenario (upload → parse → correct → accept).
- `Engagement` state machine includes `DocumentsIn`, `Parsed`, `ReadyForReview`, `Failed`.

---

## UC-03 Cross-Statement Consistency Review

**Author:** Member B  
**Ad hoc requirement:** REQ-B-01  
**Level:** User goal  
**Primary actor:** OrchestratorAgent (triggered by Practitioner or by work plan)  
**Supporting:** ConsistencyAgent, CalculatorTool, LLMService  
**Goal:** Produce consistency findings (tick-and-tie) without the LLM inventing arithmetic.

### Preconditions

- `Engagement` is `ReadyForReview`.
- Balance sheet, income statement, and cash-flow statement are parsed (or practitioner has waived a missing statement in policy — then this UC is skipped or partial).

### Postconditions (success)

- A `Task` of type Consistency is `Completed`.
- Zero or more `Finding` objects exist with evidence (accounts, amounts, rule id).
- If material inconsistencies exist, OrchestratorAgent marks the `WorkPlan` step failed and may enter UC-08.

### Main success scenario

1. OrchestratorAgent creates a Consistency `Task` and assigns ConsistencyAgent.
2. ConsistencyAgent loads line items and invokes CalculatorTool for: accounting equation; net income vs retained earnings movement; cash-flow net change vs cash on the balance sheet.
3. CalculatorTool returns pass/fail per rule with numeric evidence.
4. For each failed rule, ConsistencyAgent asks LLMService for a natural-language explanation **grounded in the numeric evidence only**.
5. Findings are stored (`Proposed` + evidence).
6. OrchestratorAgent records the step result and continues the plan (or UC-08).

### Extensions

- 2a. Missing statement: task `Blocked`; OrchestratorAgent requests UC-02 or waives per policy.
- 4a. LLM explanation conflicts with numbers: explanation discarded; finding kept with numeric evidence only (MAN-07 bounded hallucination).

---

## UC-04 Ratio and Trend Analysis

**Author:** Member B  
**Ad hoc requirement:** REQ-B-01  
**Level:** Subfunction / user goal  
**Primary actor:** OrchestratorAgent  
**Supporting:** RatioAgent, CalculatorTool, LLMService, MarketDataAPI (optional)

### Preconditions

- `Engagement` is `ReadyForReview`.
- Parsed statements provide the accounts required by the selected ratio set.

### Postconditions (success)

- `RatioResult` objects stored with formula identifiers and inputs.
- Commentary is stored separately from values (reproducibility NFR).

### Main success scenario

1. OrchestratorAgent assigns RatioAgent with the policy’s ratio pack (liquidity, leverage, profitability, coverage).
2. RatioAgent calls CalculatorTool; values persisted as `RatioResult`.
3. If OPT-03 enabled, RatioAgent queries MarketDataAPI for a peer band.
4. LLMService writes commentary comparing results to policy thresholds / peers.
5. Material outliers become `Finding` objects with formula evidence.

### Extensions

- 2a. Division by zero / missing denominator: ratio omitted with reason; not fabricated.
- 3a. Market API failure: commentary proceeds without peers; optional finding “benchmark unavailable”.

---

## UC-05 Detect Anomalies

**Author:** Member C  
**Ad hoc requirement:** REQ-C-01  
**Feature:** OPT-01 (optional)  
**Level:** User goal  
**Primary actor:** OrchestratorAgent  
**Supporting:** AnomalyAgent, CalculatorTool, LLMService

### Preconditions

- Policy enables `F-Anomaly`.
- `Engagement` is `ReadyForReview`.

### Postconditions (success)

- Anomaly `Finding` objects each carry evidence slots required by REQ-C-01 (account, period, calculation or heuristic id, source locator).

### Main success scenario

1. OrchestratorAgent assigns AnomalyAgent.
2. AnomalyAgent runs configured heuristics (unusual period-on-period swings, negative unexpected signs, concentration of round numbers — illustrative, not a claim of Benford legal use).
3. Each alert is wrapped as `Finding` with evidence; LLMService phrases the narrative from evidence fields only.
4. Findings remain `Proposed` until UC-07.

### Extensions

- 0a. Feature disabled: UC skipped; work plan shows “not in scope”.
- 3a. Heuristic without a source locator: finding cannot leave `Proposed` into a report (AGR-02).

---

## UC-06 Check Disclosure Completeness

**Author:** Member C  
**Ad hoc requirement:** REQ-C-01  
**Feature:** OPT-02 (optional)  
**Supporting:** ComplianceAgent, StandardsStore, LLMService

### Preconditions

- Policy enables `F-ComplianceRAG`.
- Notes / disclosure PDF is parsed or attached.

### Postconditions (success)

- Completeness `Finding` objects cite the standard chunk id and the note page (or “not found”).

### Main success scenario

1. ComplianceAgent retrieves applicable disclosure checklist from StandardsStore (RAG).
2. LLMService matches notes text to checklist items (perception).
3. Missing or weak items become findings with standard citations.
4. Practitioner later treats them in UC-07.

### Extensions

- 1a. StandardsStore empty: task fails closed; no invented standards citations.

---

## UC-07 Generate and Approve Review Report

**Author:** Member C  
**Ad hoc requirement:** REQ-C-01  
**Level:** User goal  
**Primary actor:** Practitioner  
**Supporting:** ReportWriterAgent, OrchestratorAgent, LLMService  
**Goal:** Issue a review memorandum containing only approved, evidence-backed findings.

### Preconditions

- Planned analytical tasks have completed, blocked with waiver, or been skipped per policy.
- Zero or more `Finding` objects exist.

### Postconditions (success)

- `ReviewReport` in state Issued (or equivalent) contains only `Confirmed` findings.
- `Dismissed` findings are listed in an appendix or omitted per policy, never mixed into the opinion-like summary.
- `Engagement` may move to `Closed`.
- AGR-01 banner is present on the report.

### Main success scenario

1. OrchestratorAgent asks ReportWriterAgent to assemble findings with evidence.
2. ReportWriterAgent groups findings and calls LLMService to draft sections (scope, procedures, findings, limitations).
3. Practitioner reviews each finding: Confirm / Dismiss / Needs more info.
4. Needs-more-info findings return to the producing agent (may trigger UC-08).
5. Practitioner accepts the draft; ReportWriterAgent freezes the report snapshot.
6. System marks the report issued for the engagement (still not an audit opinion).

### Extensions

- 3a. Finding lacks evidence: UI forbids Confirm (AGR-02); only Dismiss or Needs more info.
- 5a. Practitioner requests wording changes: LLMService regenerates the affected section; numbers and evidence tables are not regenerated from prose.

---

## UC-08 Adaptive Re-Review

**Author:** Member B  
**Ad hoc requirement:** REQ-B-01  
**Level:** User goal  
**Primary actor:** OrchestratorAgent  
**Supporting:** specialist agents, LLMService  
**Goal:** Replan and rerun **only affected tasks** after a failed check, new documents, or dismissed/needs-info findings.

### Preconditions

- A `ReviewJob` exists and is not `Cancelled`.
- A trigger has occurred: consistency failure, UC-02 replacement file, or UC-07 “needs more info” / dismissal that invalidates a downstream section.

### Postconditions (success)

- A new `WorkPlan` version exists with unchanged completed tasks reused.
- Affected tasks rerun; `ReviewJob` returns to `Running` then `AwaitingHuman` or `Completed`.
- Replan count is incremented; if it exceeds policy max, job `Cancelled` or `AwaitingHuman` with an error (termination NFR).

### Main success scenario

1. OrchestratorAgent collects the trigger (failed task ids, dismissed finding ids, new artefact ids).
2. LLMService proposes a patch to the `WorkPlan` (decision).
3. OrchestratorAgent validates the patch against a deterministic dependency graph (intake → consistency/ratios/anomaly → report).
4. Invalidated downstream outputs are marked stale; upstream successful tasks are kept.
5. Specialists rerun; Practitioner is notified of a diff (“what changed”).
6. Job resumes UC-07 if reporting was stale.

### Extensions

- 2a. LLM proposes restarting everything: OrchestratorAgent rejects the patch and applies the minimal graph cut (protect REQ-B-01).
- 3a. Max replan exceeded: stop; Practitioner must change policy or close the engagement.

---

## UC-09 Manage Review Policy

**Author:** Group (shared, short specification)  
**Level:** Subfunction  
**Primary actor:** Practitioner

### Preconditions

- Practitioner owns the engagement (or is creating one).

### Postconditions (success)

- `ReviewPolicy` updated: materiality, enabled optional agents, max replan, ratio pack.

### Main success scenario

1. Practitioner opens policy for the engagement.
2. Practitioner sets materiality, toggles OPT-01/02/03, sets max replan.
3. System stores `ReviewPolicy` and invalidates only tasks that depend on the changed flags (may enqueue UC-08).

### Extensions

- 2a. Materiality empty: save rejected.
- 2b. Disabling a mandatory feature is not offered (feature diagram constraints).
