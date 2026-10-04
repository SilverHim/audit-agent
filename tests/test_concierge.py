"""A10 RPT-03 报告首稿: 模型写意见段与基础段, 其余段落取 report.yaml 固定文本 (合成数据集设计第 7 节第 15, 16 行)."""

from dataclasses import replace
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langgraph.types import Command

from backend.agents.base import RunContext
from backend.agents.concierge import ConciergeAgent
from backend.contracts.llm_outputs import ReportDraft
from backend.contracts.models import (
    AgentId,
    Assertion,
    AuditReportVersion,
    Decision,
    Direction,
    GateDecision,
    GateId,
    GateVerdict,
    Judgment,
    JudgmentRef,
    Misstatement,
    MisstatementType,
    OpinionType,
    Procedure,
    ProcedureStatus,
    ProposedAction,
    ReportSection,
    ReportStatus,
    ReviewStatus,
    ReviewTask,
    RunStatus,
    RunTrigger,
    TargetType,
    TaskKind,
    TaskStatus,
    TrailKind,
    UncorrectedItem,
    UncorrectedSchedule,
    User,
)
from backend.core.config import get_settings
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_AUDITOR,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeRegistry,
    FakeTrail,
    FakeUserStore,
    ScriptedLLM,
    make_ctx,
    make_deps,
    numbat_all_p2_agents,
    preset_rerun,
    wait_idle,
)
from tests.test_fin_gate import REASON, fin_decision
from tests.test_flow import EID, approve_materiality, review_all, start_numbat
from tests.test_nodes import single_node

CUTOFF = Judgment(  # 合成数据集设计第 7 节第 9 行, 审计师决定 misstatement 后 v2 confirmed
    id="J-REV-02-1-SI-2026-0612",
    version=2,
    procedure_id="REV-02",
    run_id="RUN-REV-02-1",
    subject_id="SI-2026-0612",
    conclusion="Control passed on 3 July 2026; revenue of $142,000 on SI-2026-0612 belongs to the next year.",
    confidence=0.875,
    confidence_parts={"evidence": 0.7333, "rule": 1.0, "llm": 0.9},
    reasoning_chain=[],
    evidence=[],
    counter_argument="none",
    proposed_action=ProposedAction.misstatement,
    autonomy_level="L2",
    review_status=ReviewStatus.confirmed,
    performed_by="U-1",
    created_at=NOW,
)
MISSTATEMENT = Misstatement(
    id="M-D-REV02",
    judgment_id=CUTOFF.id,
    exception_id=None,
    review_record_id="D-REV02",
    type=MisstatementType.factual,
    account="Revenue",
    assertion=Assertion.Cutoff,
    amount_cents=14200000,
    direction=Direction.overstated,
)
SCHEDULE = UncorrectedSchedule(
    id="SCHEDULE",
    items=[UncorrectedItem(misstatement_id=MISSTATEMENT.id, amount_cents=14200000, direction=Direction.overstated)],
    excluded_trivial_ids=[],
    total_cents=14200000,
    materiality_cents=12000000,
    candidate_opinion=OpinionType.qualified,
)


def fin01(opinion: OpinionType) -> GateDecision:
    return GateDecision(
        id="D-FIN",
        gate=GateId.FIN_01,
        target_id="SCHEDULE",
        target_version=1,
        decision=GateVerdict.approve,
        disposition=opinion,
        reason=REASON,
        decided_by="U-1",
        decided_at=NOW,
    )


def setup(
    opinion: OpinionType = OpinionType.qualified, llm: Any = None
) -> tuple[ConciergeAgent, RunContext, FakeTrail]:
    """A10 与 RPT-03 第 1 次执行: 状态在 FIN-01 通过之后 (第 7 节第 15 行). llm 缺省为 mock 模式 (读 RPT-03/r1.json)."""
    trail = FakeTrail()
    deps = make_deps(FakeRegistry(), trail, llm=llm)
    ctx = make_ctx(deps, "RPT-03")
    state = ctx.state | {
        "opinion_route": opinion,
        "gate_decisions": [fin01(opinion)],
        "uncorrected_schedule": SCHEDULE,
        "misstatements": [MISSTATEMENT],
        "judgments": [CUTOFF],
    }
    params = deps.library.resolve_params(ctx.template, ctx.engagement, ctx.state, None)
    run = ctx.run.model_copy(update={"params_snapshot": params})
    return ConciergeAgent(deps), RunContext(ctx.engagement, ctx.procedure, ctx.template, run, state), trail  # type: ignore[arg-type]


def fixed(opinion: OpinionType) -> dict[ReportSection, str]:
    """report.yaml 中该意见类型的固定段落, 代入 Numbat 的值."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    values = {
        "entity_name": "Numbat Electronic Components Pty Ltd",
        "period_end": "30 June 2026",
        "auditor_name": NUMBAT_AUDITOR.full_name,
    }
    return {s: t.format(**values) for s, t in deps.library.report_sections(opinion).items()}


# ── 首稿 ─────────────────────────────────────────


async def test_first_revision_report() -> None:
    agent, ctx, _ = setup()
    patch = await agent.run(ctx)
    [report] = patch["report_versions"]
    assert (report.id, report.version, report.revision, report.opinion_type, report.status) == (
        "REPORT-1",
        1,
        1,
        OpinionType.qualified,
        ReportStatus.draft,
    )
    assert report.addresses_decision_id is None
    assert report.judgment_refs == [JudgmentRef(id=CUTOFF.id, version=2)]
    assert patch["review_tasks"] == []
    [run] = patch["procedure_runs"]
    assert run.id == "RUN-RPT-03-1" and run.status is RunStatus.succeeded


async def test_sections_combine_model_text_and_fixed_template() -> None:
    agent, ctx, _ = setup()
    [report] = (await agent.run(ctx))["report_versions"]
    draft = ReportDraft.model_validate_json((get_settings().LLM_FIXTURES_ROOT / "RPT-03" / "r1.json").read_text())
    assert list(report.sections) == list(ReportSection)
    assert report.sections[ReportSection.opinion] == draft.opinion
    assert report.sections[ReportSection.basis_for_opinion] == draft.basis_for_opinion
    expected = fixed(OpinionType.qualified)
    for section in (
        ReportSection.directors_responsibilities,
        ReportSection.auditor_responsibilities,
        ReportSection.other_legal_and_regulatory,
        ReportSection.signature,
    ):
        assert report.sections[section] == expected[section]
    assert report.sections[ReportSection.signature].startswith("Jordan Ellis\nTessellate Audit Partners")
    assert all("{" not in text for text in report.sections.values())


async def test_r1_basis_states_the_misstatement() -> None:
    agent, ctx, _ = setup()
    [report] = (await agent.run(ctx))["report_versions"]
    basis = report.sections[ReportSection.basis_for_opinion]
    assert basis.startswith("Basis for Qualified Opinion")
    for fact in ("$142,000", "SI-2026-0612", "28 June 2026", "3 July 2026", "overstated"):
        assert fact in basis, fact
    assert report.sections[ReportSection.opinion].startswith("Qualified Opinion")


async def test_request_and_trail() -> None:
    agent, ctx, trail = setup()
    await agent.run(ctx)
    [entry] = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    assert (entry.actor, entry.procedure_id, entry.run_id) == (AgentId.A10, "RPT-03", "RUN-RPT-03-1")
    assert entry.payload["fixture_key"] == "r1"
    prompt = entry.payload["messages"][0]["content"]
    assert "Opinion type: **qualified**" in prompt
    assert f"{CUTOFF.id} (version 2" in prompt and REASON in prompt
    assert "Total uncorrected misstatements: 14200000 cents." in prompt
    assert "- ASA705-P21 (ASA 705 para 21): If there is a material misstatement" in prompt
    assert "## Revision" not in prompt


async def test_unqualified_uses_unqualified_template_and_no_misstatements() -> None:
    draft = ReportDraft(opinion="Opinion\n\nclean.", basis_for_opinion="Basis for Opinion\n\nok.", judgment_refs=[])
    llm = ScriptedLLM({"r1": [draft.model_dump_json()]})
    agent, ctx, _ = setup(OpinionType.unqualified, llm)
    [report] = (await agent.run(ctx))["report_versions"]
    assert report.opinion_type is OpinionType.unqualified
    expected = fixed(OpinionType.unqualified)
    assert report.sections[ReportSection.auditor_responsibilities] == expected[ReportSection.auditor_responsibilities]
    prompt = llm.requests[0].messages[0].content
    assert "Uncorrected misstatements:" not in prompt and CUTOFF.id not in prompt


async def test_same_state_same_report() -> None:
    first_agent, first_ctx, _ = setup()
    second_agent, second_ctx, _ = setup()
    first = (await first_agent.run(first_ctx))["report_versions"]
    second = (await second_agent.run(second_ctx))["report_versions"]
    assert first == second


async def test_revision_is_max_plus_one() -> None:
    agent, ctx, _ = setup()
    earlier = AuditReportVersion(
        id="REPORT-3", revision=3, opinion_type=OpinionType.qualified, sections={}, judgment_refs=[]
    )
    state = ctx.state | {"report_versions": [earlier]}
    with pytest.raises(NotImplementedError, match="批注修订"):  # 第 4 稿要按批注修订, 尚未接入
        await agent.run(RunContext(ctx.engagement, ctx.procedure, ctx.template, ctx.run, state))  # type: ignore[arg-type]


def r1_raw(**changes: Any) -> str:
    raw = (get_settings().LLM_FIXTURES_ROOT / "RPT-03" / "r1.json").read_text()
    return ReportDraft.model_validate_json(raw).model_copy(update=changes).model_dump_json()


async def test_retry_after_parse_error() -> None:
    agent, ctx, trail = setup(llm=ScriptedLLM({"r1": ["not json", r1_raw()]}))
    [report] = (await agent.run(ctx))["report_versions"]
    assert report.id == "REPORT-1"
    assert [e.payload["attempt"] for e in trail.entries if e.kind is TrailKind.llm_call] == [1, 2]


async def handed_to_human(agent: ConciergeAgent, ctx: RunContext) -> dict[str, Any]:
    """经 A1 的 run_agent: 校验失败时整次执行失败, 建以执行为目标的 failed 任务 (无对象, 只能 rerun)."""
    patch, writer = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
    assert writer is AgentId.A1 and "report_versions" not in patch
    [run] = patch["procedure_runs"]
    assert run.id == "RUN-RPT-03-1" and run.status is RunStatus.failed
    [task] = patch["review_tasks"]
    assert (task.id, task.kind, task.subject_id) == ("T-RUN-RPT-03-1-v1", TaskKind.failed, None)
    assert agent.deps.queue.allowed(task) == frozenset({Decision.rerun})
    return {"failure_reason": run.failure_reason}


async def test_three_syntax_failures_fail_the_run() -> None:
    agent, ctx, trail = setup(llm=ScriptedLLM({"r1": ["x", "{}", "still bad"]}))
    result = await handed_to_human(agent, ctx)
    assert "未通过 syntax 校验" in result["failure_reason"] and "Output is not valid JSON" in result["failure_reason"]
    assert len([e for e in trail.entries if e.kind is TrailKind.llm_call]) == 3


@pytest.mark.parametrize(
    ("changes", "level"),
    [
        ({"judgment_refs": [JudgmentRef(id=CUTOFF.id, version=1)]}, "reference"),
        ({"opinion": "Opinion\n\nclean"}, "semantic"),
    ],
)
async def test_reference_or_semantic_failure_fails_after_one_call(changes: dict[str, Any], level: str) -> None:
    llm = ScriptedLLM({"r1": [r1_raw(**changes), r1_raw()]})
    agent, ctx, _ = setup(llm=llm)
    result = await handed_to_human(agent, ctx)
    assert f"未通过 {level} 校验" in result["failure_reason"]
    assert len(llm.requests) == 1


@pytest.mark.parametrize(
    "bad_replies",
    [
        ["{}", "{}", "{}"],
        [r1_raw(opinion="## Qualified Opinion\n\nbody")],
        [r1_raw(judgment_refs=[JudgmentRef(id=CUTOFF.id, version=1)])],
    ],
    ids=["structure", "semantic", "reference"],
)
async def test_report_node_failure_waits_then_rerun_drafts_report(bad_replies: list[str]) -> None:
    """(Codex R52 探针) report_draft 节点中校验失败: 写失败的 Run, 任务与 awaiting_human, 节点内等待, 不出报告;
    重放不重调模型; 审计师 rerun 后第 2 次执行出 REPORT-1."""
    llm = ScriptedLLM({"r1": [*bad_replies, r1_raw()]})
    agent, ctx, _ = setup(llm=llm)
    store = agent.deps.output_store
    await store.put(ENGAGEMENT.id, [ctx.procedure], actor=AgentId.A1)
    nodes = GraphNodes(agent.deps, parallel_cap=2)
    nodes.agents["RPT-03"] = agent
    app = single_node(nodes, "RPT-03")
    config = GraphRunner.config(ENGAGEMENT.id)

    result = await app.ainvoke(ctx.state, config)

    assert result["__interrupt__"][0].value["run_id"] == "RUN-RPT-03-1"
    [task] = await store.latest(ENGAGEMENT.id, ReviewTask, status=TaskStatus.pending)
    assert (task.id, task.target_type, task.target_id, task.subject_id) == (
        "T-RUN-RPT-03-1-v1",
        TargetType.ProcedureRun,
        "RUN-RPT-03-1",
        None,
    )
    procedure = await store.get(ENGAGEMENT.id, Procedure, "RPT-03")
    assert procedure is not None and procedure.status is ProcedureStatus.awaiting_human
    assert not result["report_versions"]
    await app.ainvoke(None, config)
    assert len(llm.requests) == len(bad_replies)

    await preset_rerun(store, task, "D-RPT-RERUN")
    result = await app.ainvoke(Command(resume="D-RPT-RERUN"), config)

    assert [(r.id, r.status, r.trigger) for r in result["procedure_runs"]] == [
        ("RUN-RPT-03-1", RunStatus.failed, RunTrigger.initial),
        ("RUN-RPT-03-2", RunStatus.succeeded, RunTrigger.rerun),
    ]
    assert [r.id for r in result["report_versions"]] == ["REPORT-1"]
    assert len(llm.requests) == len(bad_replies) + 1


# ── 接入 report_draft 节点 ─────────────────────────────────────────


async def test_real_a10_in_report_draft_reaches_rpt_gate() -> None:
    s = await start_numbat(lambda deps: {**numbat_all_p2_agents(deps), "RPT-03": ConciergeAgent(deps)})
    await approve_materiality(s)
    await wait_idle(s.runner, EID)
    await review_all(s)
    await s.submit.submit(EID, fin_decision(GateVerdict.approve))
    await wait_idle(s.runner, EID)
    assert s.runner.run_error(EID) is None
    assert await s.next_nodes() == ("gate_rpt01",)
    snapshot = await s.runner.app.aget_state(GraphRunner.config(EID))
    [report] = snapshot.values["report_versions"]
    assert report.id == "REPORT-1" and report.opinion_type is OpinionType.qualified
    assert report.sections[ReportSection.signature].startswith(NUMBAT_AUDITOR.full_name)
    cited = next(j for j in snapshot.values["judgments"] if j.id == CUTOFF.id)
    assert report.judgment_refs == [JudgmentRef(id=cited.id, version=cited.version)]


def test_engagement_owner_is_numbat_auditor() -> None:
    assert ENGAGEMENT.owner_id == NUMBAT_AUDITOR.id and NUMBAT_AUDITOR.full_name == "Jordan Ellis"


# ── Codex R51 探针: 节点重放与上下文来源 ─────────────────────────────────────────


async def test_node_replay_returns_stored_report_without_llm_or_user_lookup() -> None:
    """report_draft 节点重放 (Run 已是终态): 片段取 Store 中的 REPORT-1 与 RUN-RPT-03-1,
    不再调 A10, 不查用户, 不写入."""
    agent, ctx, trail = setup()
    store = agent.deps.output_store
    await store.put(ENGAGEMENT.id, [ctx.procedure], actor=AgentId.A1)
    nodes = GraphNodes(agent.deps, parallel_cap=2)
    nodes.agents["RPT-03"] = agent
    first = await nodes.report_draft(ctx.state)
    rows = list(store.rows)

    agent.execute = AsyncMock(side_effect=AssertionError("重放不得调 A10"))  # type: ignore[method-assign]
    agent.deps.users.get = AsyncMock(side_effect=AssertionError("重放不得查用户"))  # type: ignore[method-assign]
    second = await nodes.report_draft(ctx.state)

    assert second == first
    assert [r.id for r in second["report_versions"]] == ["REPORT-1"]
    assert [r.id for r in second["procedure_runs"]] == ["RUN-RPT-03-1"]
    assert store.rows == rows
    assert len([e for e in trail.entries if e.kind is TrailKind.llm_call]) == 1


def test_misstatement_inputs_follow_schedule_order_and_confirmed_sources() -> None:
    """错报按汇总表顺序; 来源判断只取已确认且在状态中的 (各取当前版本), 字段只留提示词要的六个."""
    agent, ctx, _ = setup()
    second_j = CUTOFF.model_copy(update={"id": "J-SECOND", "version": 7, "conclusion": "second"})
    pending_j = CUTOFF.model_copy(update={"id": "J-PENDING", "review_status": ReviewStatus.pending})
    second_m = MISSTATEMENT.model_copy(update={"id": "M-SECOND", "judgment_id": second_j.id})
    pending_m = MISSTATEMENT.model_copy(update={"id": "M-PENDING", "judgment_id": pending_j.id})
    missing_m = MISSTATEMENT.model_copy(update={"id": "M-MISSING", "judgment_id": "J-NO-SUCH"})
    no_j_m = MISSTATEMENT.model_copy(update={"id": "M-NO-J", "judgment_id": None})
    ordered = [second_m, pending_m, missing_m, MISSTATEMENT, no_j_m]
    items = [UncorrectedItem(misstatement_id=m.id, amount_cents=m.amount_cents, direction=m.direction) for m in ordered]
    state = ctx.state | {
        "uncorrected_schedule": SCHEDULE.model_copy(update={"items": items}),
        "misstatements": [MISSTATEMENT, no_j_m, second_m, missing_m, pending_m],
        "judgments": [pending_j, CUTOFF, second_j],
    }

    values = agent.misstatement_inputs(replace(ctx, state=state))

    assert [m["id"] for m in values["misstatements"]] == [m.id for m in ordered]
    assert [(j["id"], j["version"]) for j in values["judgments"]] == [(second_j.id, 7), (CUTOFF.id, 2)]
    fields = {"id", "version", "procedure_id", "subject_id", "conclusion", "evidence"}
    assert all(set(j) == fields for j in values["judgments"])


def test_schedule_misstatement_missing_from_state_raises() -> None:
    agent, ctx, _ = setup()
    with pytest.raises(KeyError, match=MISSTATEMENT.id):
        agent.misstatement_inputs(replace(ctx, state=ctx.state | {"misstatements": []}))


def test_fin01_reason_is_last_approval() -> None:
    """FIN-01 理由取最后一条 FIN-01 通过决定的, 不取驳回或其他闸门的."""
    agent, ctx, _ = setup()
    early = fin01(OpinionType.qualified).model_copy(update={"id": "D-EARLY", "reason": "first approval"})
    last = early.model_copy(update={"id": "D-LAST", "reason": "last approval"})
    reject = early.model_copy(update={"id": "D-REJECT", "reason": "reject", "decision": GateVerdict.reject})
    other = early.model_copy(update={"id": "D-OTHER", "gate": GateId.ACC_01, "reason": "different gate"})
    state = ctx.state | {"gate_decisions": [early, last, reject, other]}
    assert agent.fin01_reason(replace(ctx, state=state)) == "last approval"


async def test_profile_version_owner_and_params_snapshot_sources() -> None:
    """客户名称取状态 client_profile_version 那一版画像 (不取最新), 签名取 owner_id 的用户, 期末日取 Run 参数快照."""
    agent, ctx, trail = setup()
    chosen = NUMBAT_PROFILE.model_copy(update={"version": 2, "legal_name": "Selected v2 Pty Ltd"})
    latest = NUMBAT_PROFILE.model_copy(update={"version": 3, "legal_name": "Latest v3 Pty Ltd"})
    users = FakeUserStore(
        [
            User(id="U-1", email="owner@example.test", full_name="Actual Owner"),
            User(id="U-OTHER", email="other@example.test", full_name="Other User"),
        ]
    )
    engagements = FakeEngagementStore([NUMBAT_PROFILE, chosen, latest], [ENGAGEMENT])
    agent = ConciergeAgent(replace(agent.deps, engagement_store=engagements, users=users))
    run = ctx.run.model_copy(update={"params_snapshot": {"period_end": date(2025, 12, 31)}})

    patch = await agent.run(replace(ctx, run=run, state=ctx.state | {"client_profile_version": 2}))

    [report] = patch["report_versions"]
    assert report.sections[ReportSection.signature].startswith("Actual Owner\n")
    prompt = next(e for e in trail.entries if e.kind is TrailKind.llm_call).payload["messages"][0]["content"]
    assert "Selected v2 Pty Ltd" in prompt and "Latest v3 Pty Ltd" not in prompt
    assert "2025-12-31" in prompt and "2026-06-30" not in prompt


async def test_owner_missing_raises_before_llm() -> None:
    agent, ctx, trail = setup()
    agent = ConciergeAgent(replace(agent.deps, users=FakeUserStore([])))
    with pytest.raises(LookupError, match="U-1"):
        await agent.run(ctx)
    assert not [e for e in trail.entries if e.kind is TrailKind.llm_call]
