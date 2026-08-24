# FinReview Co. — Scope Freeze and Requirements Classification

**Product:** FinReview Co., a one-person AI financial-statement review firm.
**Stage:** ELEC5620 Stage 1 (requirements, architecture, modelling). No prototype code in this stage.
**Domain:** Finance (course application area 2).
**Theme:** One-Person AI Company — a sole practitioner operates specialised agents as virtual staff.

This document freezes product scope, requirement classes, ad hoc requirements, design assumptions, and discarded alternatives. All subsequent models must trace to identifiers defined here.

---

## 1. Problem and positioning

A sole practitioner (CPA or financial analyst) cannot staff a full review team, yet clients still expect structured analytical review of financial statements. FinReview is the practitioner’s virtual firm: the human remains the engagement partner; agents perform intake, analytical review, optional assurance checks, and report drafting.

**In scope (review, not statutory audit):**

- Create a review engagement from a natural-language brief and materiality guidance.
- Upload and parse Balance Sheet, Income Statement, Cash Flow Statement, notes, and optionally a trial balance.
- Cross-statement consistency (accounting equation; articulation among the three statements).
- Ratio and trend analysis.
- Generate a review memorandum with evidence-backed findings.
- Human-in-the-loop confirmation, dismissal, or request for more information.
- Orchestrator replans after failed checks or human dismissal (adapt based on results).

**Out of scope (explicit non-goals):**

- Full bookkeeping from source documents / journals.
- Tax return preparation or lodgement.
- Issuing a statutory audit opinion or any legally binding assurance conclusion.
- Continuous monitoring of live ERP feeds (future work only).

---

## 2. Requirement classification

Project Description requires a clear classification of **agreement**, **mandatory capabilities**, and **optional features**. Identifiers below are stable for the traceability matrix.

### 2.1 Agreement (system contract)

These are non-negotiable operating constraints of the virtual firm. Changing an agreement is a change-control event, not a backlog tweak.

| ID | Statement |
|---|---|
| AGR-01 | The system shall not issue a statutory audit opinion. All outputs are labelled as *analytical review findings* pending practitioner sign-off. |
| AGR-02 | Every finding included in a report shall be traceable to an input line item, note, computed ratio, or cited disclosure page. |
| AGR-03 | Client financial documents shall not be used to train foundation models. Inference-only use of third-party LLMs is assumed, with no retention for training. |

### 2.2 Mandatory capabilities

These must be present in the Stage 2 prototype (most models implemented) and are fully modelled in Stage 1.

| ID | Capability | Primary features | NFR (where applicable) |
|---|---|---|---|
| MAN-01 | Multi-role agent collaboration under an orchestrator | F-Orchestration | auditability of task assignment |
| MAN-02 | Natural-language engagement scoping with materiality | F-Engagement | interpretability of the derived plan |
| MAN-03 | Statement upload and structured extraction | F-Intake | extraction accuracy; latency |
| MAN-04 | Cross-statement consistency review | F-Consistency | numerical correctness of checks |
| MAN-05 | Ratio and trend analysis | F-Ratios | reproducibility of formulae |
| MAN-06 | Review-report generation with human approval | F-Reporting | traceability; completeness |
| MAN-07 | LLM used for perception, decision, and interaction | F-LLM | bounded hallucination via tools |
| MAN-08 | Tool use (parsers, calculators; market data optional) | F-Tools | isolation of tool failures |
| MAN-09 | Adaptive replanning after failed checks or dismissed findings | F-Adapt | termination (no infinite replan) |
| MAN-10 | Practitioner authentication and engagement ownership | F-Identity | confidentiality |

### 2.3 Optional features

Modelled in the feature diagram and architecture so they can be added without rewriting the domain model. Stage 2 may stub or omit them.

| ID | Feature | Notes |
|---|---|---|
| OPT-01 | Anomaly / red-flag detection | Member C secondary use case |
| OPT-02 | AASB/IFRS disclosure completeness via standards RAG | requires vector store |
| OPT-03 | Industry benchmark comparison | external market-data API |
| OPT-04 | Multi-period (YoY / QoQ) comparison | needs prior-period statements |
| OPT-05 | PDF / Word export of the signed review memo | presentation concern |
| OPT-06 | Local / on-prem LLM alternative | alternative in feature diagram |
| OPT-07 | XBRL filing ingest | alternative input format |
| OPT-08 | Voice briefing of key findings | out of Stage 2 prototype |

---

## 3. Individual ad hoc requirements

Each student contributes at least one informal (ad hoc) requirement. Behaviour diagrams **must** grow from the author’s own ad hoc requirement and corresponding use case specification(s).

### 3.1 Member A — Intake (REQ-A-01)

> As a sole practitioner opening a new client file, I want to describe the review in ordinary language (entity, period, what to emphasise, and how large a misstatement would matter) so the system can draft an engagement plan and tell me which statements to upload, instead of filling a long form before I have thought the job through.

**Intent:** LLM *perception* of an unstructured brief and *decision* of an initial work plan.
**Seeds:** UC-01 (primary), UC-02 (supporting).
**Behaviour object:** `Engagement` life cycle.

### 3.2 Member B — Analysis (REQ-B-01)

> If a consistency check fails, or I dismiss a finding because I already know the story behind it, I do not want the whole review restarted. The orchestrator should change only the affected steps, rerun them, and show me what changed.

**Intent:** LLM *decision* to replan; agents *adapt based on results*.
**Seeds:** UC-03 (primary), UC-04 and UC-08 (supporting).
**Behaviour object:** `ReviewJob` life cycle.

### 3.3 Member C — Assurance (REQ-C-01)

> Every red flag in the memo must carry evidence I can click: account, period, calculation or quote, and source page. I must be able to confirm it, dismiss it, or send it back for more information before anything is treated as issued.

**Intent:** LLM *interaction* with the practitioner; evidence-backed findings.
**Seeds:** UC-07 (primary), UC-05 and UC-06 (supporting).
**Behaviour object:** `Finding` life cycle.

---

## 4. Discarded design choices (required for presentation)

These alternatives were considered and rejected. The rationale must appear in the report and the 8-minute video.

| # | Discarded | Chosen | Why |
|---|---|---|---|
| D1 | One mega-agent that reads the entire annual report in a single prompt | Orchestrator + specialised agents | Roles map to a real firm; use cases and class specialisation are examinable; failures are isolatable |
| D2 | Fully automatic “pass/fail” conclusion with no human gate | Human-in-the-loop approval of findings | AGR-01; finance compliance; rich state machines for `Finding` and `ReviewJob` |
| D3 | Strict sequential pipeline, or a full blackboard architecture | Orchestrator with selective replan | Pipeline cannot skip/rerun well (fails REQ-B-01); blackboard is harder to implement and to draw as structured classes |

Secondary discarded options (mentioned briefly, not the three primary ones): fine-tuning a private model on AASB text (cost, data rights) vs RAG for OPT-02; extracting only from XBRL (D1-style rigidity) vs PDF/Excel + LLM perception.

---

## 5. Assumptions

| ID | Assumption |
|---|---|
| ASM-01 | The practitioner is the only human actor in the prototype; “Client” is a secondary actor who may supply files but does not use the product directly in Stage 2. |
| ASM-02 | Input statements may be imperfect PDFs or spreadsheets; extraction can fail and requires a human correction path (UC-02 extensions). |
| ASM-03 | Numerical consistency checks are executed by deterministic tools; the LLM explains and plans, it does not add numbers. |
| ASM-04 | Materiality is a practitioner-supplied threshold (absolute or % of a base), not inferred as a legal standard. |
| ASM-05 | Third-party LLM APIs are available at inference time; outages degrade to queued jobs, not silent omission of checks. |
| ASM-06 | Accounting framework in the modelled system is IFRS/AASB-oriented; US GAAP mapping is future work. |
| ASM-07 | One engagement concerns one reporting entity and one primary reporting period (comparatives are OPT-04). |
| ASM-08 | Stage 2 will use an agent framework (e.g. LangGraph or CrewAI), a web UI, and an LLM API; Stage 1 does not implement these. |

---

## 6. Actors (frozen)

| Actor | Type | Role |
|---|---|---|
| Practitioner | Primary human | Owns engagements, approves findings, signs the memo |
| Client | Secondary human | Supplies source files (may be the same person in a demo) |
| OrchestratorAgent | System agent | Plans, assigns, replans, synthesises |
| ParserAgent | System agent | Perception of documents via LLM + parse tools |
| ConsistencyAgent | System agent | Deterministic articulation checks + LLM explanation |
| RatioAgent | System agent | Formulae + LLM commentary |
| AnomalyAgent | System agent (optional) | Red-flag heuristics |
| ComplianceAgent | System agent (optional) | Disclosure completeness / RAG |
| ReportWriterAgent | System agent | Drafts the memorandum |
| LLMService | External | Foundation-model inference |
| DocumentTool | External | PDF/Excel/XBRL extraction |
| CalculatorTool | External | Ratios and tick/tie arithmetic |
| MarketDataAPI | External (optional) | Industry benchmarks |
| StandardsStore | External (optional) | AASB/IFRS chunks for RAG |

---

## 7. Feature set (IDs for the feature diagram)

Mandatory features: `F-Identity`, `F-Engagement`, `F-Intake`, `F-Orchestration`, `F-Consistency`, `F-Ratios`, `F-Reporting`, `F-LLM`, `F-Tools`, `F-Adapt`, `F-HITL`.

Optional features: `F-Anomaly`, `F-ComplianceRAG`, `F-Benchmark`, `F-MultiPeriod`, `F-Export`, `F-Voice`.

Alternatives:

- `F-LLMProvider`: OpenAI-compatible cloud **xor** local model (`OPT-06`).
- `F-InputFormat`: PDF, Excel, and/or XBRL (`OPT-07`).

NFR labels used on the diagram: *accuracy*, *latency*, *traceability*, *completeness*, *confidentiality*, *auditability*, *reproducibility*, *termination*.
