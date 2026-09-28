# Speaker script — 8 minute introduction

ELEC5620 Group 16. English deck: `Group16-audit-support-8min.pptx`.

Three speakers. Spoken time is about 7 minutes 45 seconds, including the three figure flashes. Page turns bring it to about 8 minutes.

| Speaker | Slides | Subject | Time |
|---|---|---|---|
| Member C | 1–3 | Positioning and the five phases | about 2:20 |
| Member A | 4–7 | Agents, Figure 1b, design models, Figure 3a | about 2:50 |
| Member B | 8–11 | Use cases, Figure 6, one audit line, close | about 2:30 |

Slides 5, 7, and 9 are figure flashes. Each is one sentence. Skip a flash if the talk is running long. The appendix after slide 12 is not spoken.

A7 independent review and A8 workpapers are in the specification. This prototype has not wired them in. A10 dialogue is not in this prototype either. Presentation goes through the gate and review screens. Report drafting is already on A10.

The diagram PNGs already in `docs/diagrams/out` are embedded. Each figure slide also links to the tracked `.puml` on the university GitHub. That repository is private to the group. The labels on a fitted diagram are small; open the source to read every box.

## 1. Title

**Member C · about 20 seconds · cumulative 0:20**

Hello, we are Group 16. This system helps one Australian registered company auditor complete a statutory audit. I will cover positioning and functions. The next speaker covers the agents and the design models. The last speaker walks one full case, from a cutoff error to the opinion.

## 2. Positioning

**Member C · about 55 seconds · cumulative 1:15**

The user is a sole-practitioner registered company auditor. The setting is Australia: the auditing standards ASA, the accounting standards AASB, and the Corporations Act. This is audit support. The opinion and the signature have to be the auditor's own. Agents check registrations, run procedures, read evidence, and produce a preliminary judgment. Four statutory decisions stop and wait. Every judgment has to trace back to the original evidence.

## 3. Five phases, four gates

**Member C · about 70 seconds · cumulative 2:25**

The work runs in five phases. Acceptance checks the ABN and ASIC, and the auditor decides whether to take the engagement. Planning sets materiality, confirms the risk list, and assembles the procedure pack. Execution runs substantive procedures in parallel. In this prototype the two main lines are revenue cutoff and bank confirmation. Completion adds up uncorrected misstatements, and the auditor confirms the opinion type. Reporting drafts the report. The auditor can send it back with comments, or sign and archive it. A state machine drives the flow, and it can resume from a checkpoint. The specification has 161 procedures. This prototype runs 17. That is my part.

## 4. Ten agents

**Member A · about 70 seconds · cumulative 3:35**

The ten agents are split by what they produce, not by account area. A1 is the orchestrator: the state machine, the gates, and the schedule. It does not call a language model. A2 checks background and independence. A3 turns contracts and replies into structured data. A4 rates risk and materiality. A5 collects external evidence, such as a confirmation. A6 makes the substantive judgments, and it can run as several instances. A7 re-reads the evidence with a different model, so a model does not review itself. A8 writes the workpapers. A9 does rule-based quality control, including the misstatement roll-up. A10 is the only door. It presents the item and passes the decision on. It does not decide. Agents do not call each other. Results go into the engagement state, and A1 merges them. A7 is the one exception: it reviews before that merge. The dashed roles, A7 and A8, are in the specification. This prototype has not wired them in. Presentation goes through the gate and review screens. Report drafting is already on A10.

## 5. Figure 1b — Class diagram — agents, services, and contracts

**Member A · about 15 seconds · cumulative 3:50**

This is Figure 1b, the agent and service model. We will not read every box. The full source is linked on the slide.

**If over time:** Skip this page.

## 6. Three constraints on autonomy

**Member A · about 70 seconds · cumulative 5:00**

There are three constraints. First, bounded autonomy. The macro flow is a state machine. A gate does not move until the auditor makes an explicit decision. In execution, procedures run in dependency layers, in parallel within a layer. After the merge the flow continues, waits for a person, or enters completion. Second, four autonomy levels: the person decides, the person reviews each item, the person sees only exceptions, or the person samples afterwards. A significant risk, an amount above performance materiality, or low confidence moves the level down. Thirty-two statutory procedures stay at level one and never move up. Third, a judgment is a conclusion, a reasoning chain, a counter-argument, a confidence score, and evidence pointers. Four checks: syntax, structure, references, and semantics. A fact has to point at a document. A standard has to point at a fragment in the knowledge base. That is my part.

The autonomy levels follow Sheridan and Verplank, 1978, compressed to the four review habits used in audit.

## 7. Figure 3a — State machine — engagement lifecycle

**Member A · about 15 seconds · cumulative 5:15**

This is Figure 3a, the engagement state machine. The yellow states are the human gates. We will not trace every arrow.

**If over time:** Skip this page. Hand over to Member B either way.

## 8. Nine use cases

**Member B · about 50 seconds · cumulative 6:05**

There are nine use cases, and they sit on those five phases. The first seven follow the phases: acceptance, planning, execution, exception review, risk re-assessment, completion and the report, then archiving. The last two can cut in at any phase: asking where the work is and where a figure came from, and statutory notification or withdrawal. Besides the auditor, the actors are the client, external data sources, and the engagement quality reviewer before signature. Risk re-assessment can pull an engagement that has already started execution back into planning.

## 9. Figure 6 — Use cases — actors and the nine use cases

**Member B · about 15 seconds · cumulative 6:20**

This is Figure 6, the nine use cases and who supplies what. We will not read the legend.

**If over time:** Skip this page.

## 10. Cutoff error to a qualified opinion

**Member B · about 65 seconds · cumulative 7:25**

One line through the synthetic data. The company is fictional. Year-end 30 June 2026, revenue about 12 million Australian dollars. An invoice for 142,000 is recorded on 28 June, and the goods are signed for on 3 July. Control transfers on signing, so three-way matching flags the cutoff. The judgment proposes a misstatement, and the auditor confirms it. The bank reply is 50,000 below the book. That difference is a deposit in transit, recorded on 30 June and received on 2 July, so the system proposes that it is not a misstatement. The auditor confirms that too. The cutoff amount sits above the top of the overall-materiality range, 60 to 120 thousand, so completion proposes a qualified opinion. The signature waits until the auditor confirms the opinion type. The invoice, the delivery note, and the reply open from the judgment.

## 11. Close

**Member B · about 20 seconds · cumulative 7:45 including the three flashes**

Three sentences. The judgment stays with the person. Execution, traceability, and the block stay with the system. Ten agents collaborate by what they produce, and the orchestrator is the only scheduler. The higher the risk, the lower the autonomy. Thank you. Questions welcome. Do not present the appendix unless someone asks for a figure.

**If over time:** If the three figure flashes were skipped, this close lands near 7:00. Page turns still bring a full reading to about 8:00.

## 12. Appendix divider

**Not spoken**

Stop unless a question asks for a specific figure.

## 13. Figure 1a — Class diagram — domain model

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/class_domain.puml

## 14. Figure 1b — Class diagram — agents, services, and contracts

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/class_agents.puml

## 15. Figure 2 — Package diagram — modules and contracts

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/package.puml

## 16. Figure 3a — State machine — engagement lifecycle

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/state_main.puml

## 17. Figure 3b — Object state machines

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/state_objects.puml

## 18. Figure 3b-1 — Object states — risk, procedure, run

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/state_objects_member_b.puml

## 19. Figure 3b-2 — Object states — misstatement, statutory report, document

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/state_objects_member_c.puml

## 20. Figure 4a — Activity — pack assembly and fieldwork

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/activity_fieldwork.puml

## 21. Figure 4a-1 — Activity — pack assembly and plan approval

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/activity_planning.puml

## 22. Figure 4a-2 — Activity — fieldwork execution and feedback loops

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/activity_execution.puml

## 23. Figure 4b — Activity — one procedure inside an A6 instance

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/activity_procedure.puml

## 24. Figure 5a — Sequence — assertion-level risk reasoning

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/sequence_risk_assessment.puml

## 25. Figure 5b — Sequence — failure paths of a journal-exception attribution

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/sequence_failure_paths.puml

## 26. Figure 6 — Use cases — actors and the nine use cases

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/usecase.puml

## 27. Figure 6A — Use case specification — UC-01 to UC-03

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/usecase_spec_member_a.puml

## 28. Figure 6B — Use case specification — UC-04 to UC-06

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/usecase_spec_member_b.puml

## 29. Figure 6C — Use case specification — UC-07 to UC-09

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/usecase_spec_member_c.puml

## 30. Figure 7 — Requirements — mandatory and optional

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/requirements.puml

## 31. Figure 7A — Requirements — execution and evidence

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/requirements_member_a.puml

## 32. Figure 7B — Requirements — human judgement and interaction

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/requirements_member_b.puml

## 33. Figure 7C — Requirements — statutory obligations

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/requirements_member_c.puml

## 34. Figure 8 — Feature diagram — audit support system

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/feature.puml

## 35. Figure 9 — Structured class — A1 Orchestrator

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/structured_class_a1.puml

## 36. Figure 10 — Collaboration — one exception review

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/collaboration_review.puml
