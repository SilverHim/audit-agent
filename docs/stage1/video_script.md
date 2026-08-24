# FinReview Co. — 8-minute video script (≤ 8:00)

**Purpose:** Introduce the project; overview agent roles, functions, use cases, and design models. Upload to Canvas; optional YouTube link.

**Visuals:** Feature diagram, use case overview, class diagram, Member B sequence, Member C finding state machine, traceability slide.

---

### 0:00–0:45 — Problem and one-person company

**Narration:**
We are building FinReview Co., a one-person AI accounting-review firm. A sole practitioner cannot staff a full analytical-review team, but clients still need structured review of financial statements — not a statutory audit. The human is the engagement partner. Specialised agents are the virtual staff. The product reviews statements; it never issues an audit opinion.

**On screen:** Title; “Review ≠ Audit”; AGR-01 banner.

---

### 0:45–2:00 — Requirements classification and features

**Narration:**
We classified requirements three ways. Agreements are the firm’s contract: no audit opinion, every finding is traceable, client files are not used to train models. Mandatory capabilities are orchestration, intake, consistency, ratios, reporting, tool use, and adaptive replan. Optional features — anomaly detection, standards RAG, benchmarks — sit on the feature diagram so we can add them without rewriting the domain.

**On screen:** Table AGR / MAN / OPT; feature diagram with NFRs (traceability, termination, confidentiality).

---

### 2:00–3:30 — Agent roles and key use cases

**Narration:**
Nine use cases. The practitioner creates an engagement from a natural-language brief, uploads statements, then agents run consistency and ratio analysis. Optional anomaly and disclosure checks feed the same Finding type. The practitioner approves the memo. If a check fails or a finding is sent back, the orchestrator replans only the affected steps.

Three individual stories: Member A owns intake and engagement scoping. Member B owns consistency, ratios, and adaptive re-review. Member C owns evidence-backed findings and report approval.

**On screen:** Use case overview; three swimlanes of agents.

---

### 3:30–5:00 — Design rationale and discarded choices

**Narration:**
We discarded a single mega-agent: it hides roles and cannot be modelled or failed in isolation. We discarded full automation: finance requires a human gate, and that gate gives us real state machines. We discarded both a rigid pipeline and a full blackboard: the pipeline cannot replan; the blackboard is heavier than a course prototype. We chose an orchestrator with specialist agents, ports for future specialists, and deterministic tools for arithmetic so the LLM cannot invent numbers.

**On screen:** Three-column “discarded vs chosen”.

---

### 5:00–7:00 — Model overview

**Narration:**
Design-time structure: Engagement composes policy and work plans, aggregates statements, statements compose line items, reports compose findings, findings compose evidence. Agents specialise an abstract Agent and depend on ILLMClient and ITool.

Runtime: an object diagram of a FY2025 job; composite structure with planOut, findingIn, humanGate ports.

Behaviour: Member A’s engagement states from Draft to Closed. Member B’s review job, including Replanning with a max-replan cap. Member C’s finding states — Confirm is guarded on evidence. Sequence diagrams show LLM perception, decision, and interaction explicitly.

**On screen:** Class diagram 40s; object or ports 20s; one sequence 30s; Finding SM 30s.

---

### 7:00–7:45 — Change assessment and acceptance

**Narration:**
Every requirement has an ID. Changes walk the feature diagram and the traceability matrix. Agreement changes need group re-approval. Optional flags are policy, not schema forks. Acceptance for Stage 2 is the use-case postcondition: for example, a report cannot contain a finding without evidence. If you cannot point from an activity diagram to an ad hoc requirement in ten seconds, the model is wrong.

**On screen:** Traceability matrix excerpt.

---

### 7:45–8:00 — Stage 2 preview

**Narration:**
Stage 2 will implement the mandatory path with an agent runtime, a web workbench, parsers, and an LLM API. Optional agents plug into the same ports. Thank you.

**On screen:** Deployment sketch; “prototype = most models, Agile”.

---

## Recording notes

- One narrator or three voices (A/B/C) matching the individual stories.
- Hard cut at 8:00; prefer 7:50.
- Do not show unimplemented UI as if it were a prototype.
