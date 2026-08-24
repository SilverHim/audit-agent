# FinReview Co. — Interview preparation (Weeks 11–12)

Interview is 10 marks. Every member will be asked questions. Prepare your own 90-second chain first, then the shared architecture answers.

Time slots are assigned in **reverse order of Assignment 1 submission** (earlier A1 ⇒ later interview).

---

## 1. Personal 90-second pitch (memorise)

### Member A

My ad hoc requirement is that a practitioner can open a job in ordinary language. That is REQ-A-01. It becomes UC-01, create review engagement, and UC-02, parse statements. My activity diagram is the parse pipeline, including unreadable files and low-confidence rows. My sequence diagram shows the orchestrator calling the LLM with a scoping schema, then the parser. My state machine is Engagement: Draft, Scoped, DocumentsIn, Parsed, ReadyForReview, Closed or Failed. The LLM’s role for me is perception of the brief and of tables, plus a decision of the initial work plan. Amounts are never invented.

### Member B

My ad hoc requirement is that we do not restart the whole review after a failed tick-and-tie or a dismissed finding. REQ-B-01. Primary use case UC-03 consistency, plus UC-04 ratios and UC-08 adaptive re-review. Activity: CalculatorTool does arithmetic; LLM only explains. Sequence: orchestrator runs consistency and ratio in parallel, then may patch the plan. State machine: ReviewJob with Replanning and a max-replan cap so we terminate. The LLM decides a patch; the orchestrator validates it against a dependency graph.

### Member C

My ad hoc requirement is that every red flag carries clickable evidence and the human can confirm, dismiss, or send it back. REQ-C-01. Primary UC-07 generate and approve report; UC-05 and UC-06 are optional producers of the same Finding type. Activity: HITL loop and AGR-01 banner. Sequence: specialists create findings, writer drafts, practitioner gates Confirm. State machine: Finding — Confirm is guarded on evidence locators. The LLM writes narrative from evidence fields; it cannot move the state machine.

---

## 2. Likely shared questions

**Why not one LLM call on the whole annual report?**  
Roles, isolation, examinable specialisation, and REQ-B-01 (partial replan). Discarded choice D1.

**Why not fully automatic pass/fail?**  
AGR-01, professional responsibility, and we need Finding states. D2.

**Pipeline vs blackboard vs orchestrator?**  
Pipeline cannot skip/rerun well. Blackboard is heavier. Orchestrator plus graph-cut replan is the compromise. D3.

**Where does the LLM perceive, decide, interact?**  
Perceive: brief and tables (A), notes vs checklist (C optional). Decide: initial plan (A), replan patch (B). Interact: clarifications, explanations, memo prose, wording edits (A/B/C). Arithmetic is `CalculatorTool`.

**How do you stop hallucination?**  
Schema-constrained scoping; tools for numbers; drop narrative if it contradicts numbers; Confirm disabled without locator; AGR-03 no training on client files.

**How does the design support future work?**  
Subclass `Agent`; bind new ports; optional flags on `ReviewPolicy`; `ILLMClient` for local vs cloud.

**What is acceptance of a requirement change?**  
Update the matrix; if a `*` artefact moves, the owning student redraws from the same ad hoc; Stage 2 tests the UC postcondition.

**What did you personally contribute on group diagrams?**  
Quote the percentage in `03-architecture-and-traceability.md` section 7 and name the packages/classes you drew.

**Review vs audit?**  
We produce an analytical review memorandum with a banner. We do not issue a statutory opinion.

**What if the LLM proposes restarting everything on replan?**  
Orchestrator rejects the patch and applies the minimal dependency cut (UC-08 extension 2a).

**NFR examples?**  
Parsing accuracy and latency; report traceability and completeness; replan termination; confidentiality of identity; citation integrity for RAG.

---

## 3. Trap questions (marking notes)

- “Show me which ad hoc requirement this activity is from.” If you hesitate, you lose the individual-behaviour rule (note 4).
- “Point to multiplicity / composition / an interface on the class diagram.”
- “This object diagram is just another class diagram.” Reply: named instances, FY2025 snapshot, states on objects.
- “Where is human-in-the-loop?” Finding SM + UC-07 + `humanGate` port.

---

## 4. Rapid drill (pair up)

1. Other member picks a random activity node → author must name REQ + UC in 10 seconds.
2. Name three associations on the class diagram with multiplicity.
3. Walk cash-articulation failure from CalculatorTool to UC-08 to a new Finding.
4. Recite AGR-01, AGR-02, AGR-03 without notes.
