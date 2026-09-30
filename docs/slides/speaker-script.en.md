# Speaker script — 8 minute introduction

ELEC5620 Group 16. English deck: `Group16-audit-support.pptx`.

Three speakers. Spoken time is about 9 minutes if the A6 workflow and the prototype walk-through are both included. Skip those two pages to stay near 8 minutes.

| Speaker | Slides | Subject | Time |
|---|---|---|---|
| Member C | 1–3 | Positioning and the five phases | about 2:20 |
| Member A | 4–9 | Agents, one fieldwork workflow, collaboration, design models, the state machine | about 3:45 |
| Member B | 10–14 | Use cases, the prototype run, one audit line, close | about 2:55 |

Slide 6 is one agent's workflow. Slides 7, 9, and 11 are simplified diagrams. Slide 12 is the prototype execution path. Skip slide 6 or slide 12 if the talk is running long. The full PlantUML figures stay in the appendix, from slide 15, and are not spoken.

A7 independent review and A8 workpapers are in the specification. This prototype has not wired them in. A10 dialogue is not in this prototype either. Presentation goes through the gate and review screens. Report drafting is already on A10.

Procedure counts on the agent slide are the specification of 161. This prototype runs 17 of them. The appendix still embeds the diagram PNGs from `docs/diagrams/out`, with a link to each tracked `.puml`. That repository is private to the group.

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

**Member A · about 40 seconds · cumulative 3:05**

The ten agents are split by what they produce, not by account area, so the procedure library can grow without adding roles. A1 is the orchestrator: the state machine, the gates, and the schedule. It does not call a language model. Acceptance and planning are A2, A3, and A4. Execution is A5 and A6, and A6 can run as several instances. The dashed cards, A7 and A8, are in the specification and are not wired into this prototype. A9 does the rule-based roll-up. A10 is the only door. The next slide says what each one produces.

## 5. What each agent produces

**Member A · about 50 seconds · cumulative 3:55**

The map named the ten roles. This is what each one produces. A1 does not run procedures. It only advances the state machine. The other nine share the 161 procedures in the specification. A2, A3, and A4 cover acceptance and planning. A5 sends confirmations and does not call a model. A6 does the substantive work. Most of the procedures sit there, and it can run as several instances. A7 reads the evidence again on a different model, so a model does not review itself. A8 writes the workpapers. Both are specification only. A9 is rules: the roll-up and the completeness checks. A10 is the only door. It presents the item and drafts the report. It records the auditor's decision as given. It does not decide.

## 6. A6, one procedure: judge the cutoff

**Member A · about 40 seconds · cumulative 4:35**

Take one agent, and one procedure it actually owns. A6 is Fieldwork. REV-02 is the revenue cutoff judgment. A1 does not start it until the three-way match and the contract judgment are already in the state. A rule then selects the invoices: inside the cutoff window, and large enough, or already flagged as a cutoff exception. The model judges one invoice at a time. It sees that invoice, the delivery note, the contract, and two paragraphs of AASB 15. It does not see the whole ledger. What comes back is a judgment: a conclusion, a reasoning chain, a counter-argument, and pointers to the page. Because this procedure is level 2, the item waits. The auditor confirms it, overrides it, or records the misstatement. On the synthetic file, invoice SI-2026-0612 was posted on 28 June and signed on 3 July. A6 proposes a misstatement of 142,000. It does not record that misstatement itself.

**If over time:** Skip this page. The later case still walks the same invoice.

## 7. How the agents collaborate

**Member A · about 20 seconds · cumulative 4:55**

This is the collaboration model, simplified from Figure 1b. Agents do not call each other. A1 hands each one the engagement state and merges the patch that comes back, after four checks. The dashed cards are specification only. The one exception is A7: A6 calls it before the merge, on a different model, and only with the evidence. The auditor comes in through A10.

**If over time:** Skip this page.

## 8. Three constraints on autonomy

**Member A · about 55 seconds · cumulative 5:50**

There are three constraints. First, bounded autonomy. The macro flow is a state machine. A gate does not move until the auditor makes an explicit decision. In execution, procedures run in dependency layers, in parallel within a layer. After the merge the flow continues, waits for a person, or enters completion. Second, four autonomy levels: the person decides, the person reviews each item, the person sees only exceptions, or the person samples afterwards. A significant risk, an amount above performance materiality, or low confidence moves the level down. Thirty-two statutory procedures stay at level one and never move up. Third, a judgment is a conclusion, a reasoning chain, a counter-argument, a confidence score, and evidence pointers. Four checks: syntax, structure, references, and semantics. A fact has to point at a document. A standard has to point at a fragment in the knowledge base. That is my part.

The autonomy levels follow Sheridan and Verplank, 1978, compressed to the four review habits used in audit.

## 9. Five phases, gold gates

**Member A · about 20 seconds · cumulative 6:10**

This is the engagement state machine, simplified from Figure 3a. Five phases run in order. The gold bars are the human gates: accept or decline, approve the plan, confirm the opinion, and sign or send the report back. Execution has no gate of its own. When the pack settles, completion starts. A risk flag can send execution back to planning. Decline and withdrawal are the two ways out. Hand over to the next speaker.

**If over time:** Skip this page. Hand over to Member B either way.

## 10. Nine use cases

**Member B · about 40 seconds · cumulative 6:50**

There are nine use cases, and they sit on those five phases. The first seven follow the phases: acceptance, planning, execution, exception review, risk re-assessment, completion and the report, then archiving. The last two can cut in at any phase: asking where the work is and where a figure came from, and statutory notification or withdrawal. Besides the auditor, the actors are the client, external data sources, and the engagement quality reviewer before signature. Risk re-assessment can pull an engagement that has already started execution back into planning.

## 11. Who joins each use case

**Member B · about 20 seconds · cumulative 7:10**

This is Figure 6, redrawn so the room can read it. The registered company auditor joins every use case. The client supplies the letter, the file, and the replies. External data covers the registers, the accounting system, and confirmations. The engagement quality reviewer comes in before signature, and afterwards can only read the archive. Risk re-assessment can pull execution back to planning. Statutory notice or withdrawal can cut in at any phase.

**If over time:** Skip this page.

## 12. How this prototype runs one engagement

**Member B · about 35 seconds · cumulative 7:45**

This is the path this prototype actually runs, not the full specification. A1 schedules execution in three layers. In the first layer, A6 matches the invoice to the delivery note and reads the contract, while A5 sends the bank confirmation and A6 compares the balances. A1 merges those results. In the next layer, A6 makes the cutoff judgment: the invoice was recorded on 28 June and signed for on 3 July, so it proposes a misstatement of 142,000. A3 reads the bank reply. In the last layer, A6 explains the 50,000 gap as a deposit in transit. The auditor confirms both items on the review queue. A9 rolls the cutoff above materiality, and A10 drafts the report. The next page is the same case, told as three scenes.

**If over time:** Skip this page. The next page still tells the same case.

## 13. Cutoff error to a qualified opinion

**Member B · about 60 seconds · cumulative 8:45**

One line through the synthetic data. The company is fictional. Year-end 30 June 2026, revenue about 12 million Australian dollars. An invoice for 142,000 is recorded on 28 June, and the goods are signed for on 3 July. Control transfers on signing, so three-way matching flags the cutoff. The judgment proposes a misstatement, and the auditor confirms it. The bank reply is 50,000 below the book. That difference is a deposit in transit, recorded on 30 June and received on 2 July, so the system proposes that it is not a misstatement. The auditor confirms that too. The cutoff amount sits above the top of the overall-materiality range, 60 to 120 thousand, so completion proposes a qualified opinion. The signature waits until the auditor confirms the opinion type. The invoice, the delivery note, and the reply open from the judgment.

## 14. Close

**Member B · about 20 seconds · cumulative 9:05 if every added page is spoken**

Three sentences. The judgment stays with the person. Execution, traceability, and the block stay with the system. Ten agents collaborate by what they produce, and the orchestrator is the only scheduler. The higher the risk, the lower the autonomy. Thank you. Questions welcome. Do not present the appendix unless someone asks for a figure.

**If over time:** Skip the A6 workflow and the prototype walk-through to land near 7:50. Skipping the three diagram slides as well lands near 6:50.

## 15. Appendix divider

**Not spoken**

Stop unless a question asks for a specific figure.

## 16. Figure 1a — Class diagram — domain model

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/class_domain.puml

## 17. Figure 1b — Class diagram — agents, services, and contracts

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/class_agents.puml

## 18. Figure 2 — Package diagram — modules and contracts

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/package.puml

## 19. Figure 3a — State machine — engagement lifecycle

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/state_main.puml

## 20. Figure 3b — Object state machines

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/state_objects.puml

## 21. Figure 3b-1 — Object states — risk, procedure, run

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/state_objects_member_b.puml

## 22. Figure 3b-2 — Object states — misstatement, statutory report, document

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/state_objects_member_c.puml

## 23. Figure 4a — Activity — pack assembly and fieldwork

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/activity_fieldwork.puml

## 24. Figure 4a-1 — Activity — pack assembly and plan approval

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/activity_planning.puml

## 25. Figure 4a-2 — Activity — fieldwork execution and feedback loops

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/activity_execution.puml

## 26. Figure 4b — Activity — one procedure inside an A6 instance

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/activity_procedure.puml

## 27. Figure 5a — Sequence — assertion-level risk reasoning

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/sequence_risk_assessment.puml

## 28. Figure 5b — Sequence — failure paths of a journal-exception attribution

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/sequence_failure_paths.puml

## 29. Figure 6 — Use cases — actors and the nine use cases

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/usecase.puml

## 30. Figure 6A — Use case specification — UC-01 to UC-03

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/usecase_spec_member_a.puml

## 31. Figure 6B — Use case specification — UC-04 to UC-06

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/usecase_spec_member_b.puml

## 32. Figure 6C — Use case specification — UC-07 to UC-09

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/usecase_spec_member_c.puml

## 33. Figure 7 — Requirements — mandatory and optional

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/requirements.puml

## 34. Figure 7A — Requirements — execution and evidence

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/requirements_member_a.puml

## 35. Figure 7B — Requirements — human judgement and interaction

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/requirements_member_b.puml

## 36. Figure 7C — Requirements — statutory obligations

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/requirements_member_c.puml

## 37. Figure 8 — Feature diagram — audit support system

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/feature.puml

## 38. Figure 9 — Structured class — A1 Orchestrator

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/structured_class_a1.puml

## 39. Figure 10 — Collaboration — one exception review

**Not spoken**

Not spoken.

https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo/blob/main/docs/diagrams/collaboration_review.puml
