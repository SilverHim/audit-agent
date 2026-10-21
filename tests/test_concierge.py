"""A10 RPT-03 报告首稿: 模型写意见段与基础段, 其余段落取 report.yaml 固定文本 (合成数据集设计第 7 节第 15, 16 行)."""

import json
from dataclasses import replace
from datetime import date
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest
from langgraph.types import Command

from backend.agents.base import RunContext
from backend.agents.concierge import ConciergeAgent
from backend.agents.concierge.report import DATE_EXAMPLE, fin01_reason, misstatement_inputs, revision_inputs
from backend.contracts.llm_outputs import ReportDraft
from backend.contracts.models import (
    AgentId,
    Annotation,
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
    MisstatementStatus,
    MisstatementType,
    OpinionType,
    Phase,
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
from backend.contracts.records import DatasetTraits
from backend.core.config import get_settings
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.services.judgment_engine import MAX_ATTEMPTS
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
    make_run,
    numbat_all_p2_agents,
    preset_rerun,
    wait_idle,
)
from tests.test_fin_gate import REASON, fin_decision
from tests.test_flow import EID, approve_materiality, review_all, start_numbat
from tests.test_nodes import single_node
from tests.test_rpt_gate import rpt_decision

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
    status=MisstatementStatus.uncorrected,  # 功能 misstatement 关闭时的旧口径 (FIN-04 按状态累计, 需求 R166)
)
# 生命周期之前错报的字段 (R19 加 status), 写死不取 report.MISSTATEMENT_FIELDS
R19_MISSTATEMENT_KEYS = {
    "id",
    "version",
    "judgment_id",
    "exception_id",
    "review_record_id",
    "type",
    "account",
    "assertion",
    "amount_cents",
    "direction",
    "status",
}
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
    params = deps.library.resolve_params(
        ctx.template, ctx.engagement, ctx.state, None, traits=DatasetTraits(), profile=None
    )
    run = ctx.run.model_copy(update={"params_snapshot": params})
    return ConciergeAgent(deps), RunContext(ctx.engagement, ctx.procedure, ctx.template, run, state), trail  # type: ignore[arg-type]


def fixed(opinion: OpinionType, language: Literal["zh", "en"] = "en") -> dict[ReportSection, str]:
    """report.yaml 中该语言, 该意见类型的固定段落, 代入 Numbat 的值."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    values = {
        "entity_name": "Numbat Electronic Components Pty Ltd",
        "period_end": deps.library.report_date(date(2026, 6, 30), language),
        "auditor_name": NUMBAT_AUDITOR.full_name,
    }
    return {s: t.format(**values) for s, t in deps.library.report_sections(opinion, language).items()}


# ── 分派 (需求 R91) ─────────────────────────────────────────


async def test_dispatch_covers_only_registered_programs() -> None:
    """A10 按程序编号分派: 负责的程序恰为分派表登记的 (现只有 RPT-03); 没登记的程序 (如以后的 RPT-08) 执行时抛
    NotImplementedError, 不会落到 RPT-03 的逻辑上去起草报告."""
    agent, ctx, trail = setup()
    assert agent.procedures == frozenset({"RPT-03"})
    other = replace(ctx, procedure=ctx.procedure.model_copy(update={"id": "RPT-08"}))
    with pytest.raises(NotImplementedError, match="RPT-08"):
        await agent.execute(other)
    assert trail.entries == []


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
    assert report.run_id == run.id  # 需求 R339 验收 4: 报告页按起草它的执行记录标生成语言


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
    assert "key `REV-02:SI-2026-0612` (REV-02, subject SI-2026-0612)" in prompt and REASON in prompt
    assert "Total uncorrected misstatements: 14200000 cents." in prompt
    assert "- ASA705-P21 (ASA 705 para 21): If there is a material misstatement" in prompt
    assert "## Revision" not in prompt
    # 需求 R156 (第 1 轮审核 R156-01): 错报块只有生命周期之前的字段 (R19 加 status), 新字段不进提示词
    block = prompt.split("Uncorrected misstatements:\n```json\n", 1)[1].split("\n```", 1)[0]
    assert [set(m) for m in json.loads(block)] == [R19_MISSTATEMENT_KEYS]


async def test_unqualified_uses_unqualified_template_and_no_misstatements() -> None:
    draft = ReportDraft(opinion="Opinion\n\nclean.", basis_for_opinion="Basis for Opinion\n\nok.", judgment_keys=[])
    llm = ScriptedLLM({"r1": [draft.model_dump_json()]})
    agent, ctx, _ = setup(OpinionType.unqualified, llm)
    [report] = (await agent.run(ctx))["report_versions"]
    assert report.opinion_type is OpinionType.unqualified
    expected = fixed(OpinionType.unqualified)
    assert report.sections[ReportSection.auditor_responsibilities] == expected[ReportSection.auditor_responsibilities]
    prompt = llm.requests[0].messages[0].content
    assert "Uncorrected misstatements:" not in prompt and CUTOFF.id not in prompt


# 中文执行记录下模型写的两段 (需求 R341): 首行为验收写明的中文标题, 正文为中文, 引用键同 r1
ZH_R1 = ReportDraft(
    opinion="保留意见\n\n我们审计了 Numbat Electronic Components Pty Ltd (以下简称公司) 的财务报告. 我们认为, "
    "除形成保留意见的基础部分所述事项的影响外, 后附的财务报告符合《2001年公司法》的规定.",
    basis_for_opinion="形成保留意见的基础\n\n收入包括公司于2026年6月28日入账的销售发票 SI-2026-0612, 金额 $142,000; "
    "客户于2026年7月3日签收, 截至2026年6月30日止年度的收入因此高估 $142,000.",
    judgment_keys=["REV-02:SI-2026-0612"],
)


@pytest.mark.parametrize(
    ("language", "draft", "passes"),
    [("zh", ZH_R1, True), ("zh", None, False), ("en", None, True)],
    ids=["zh", "zh_with_english_output", "en"],
)
async def test_report_follows_the_language_of_its_run(
    language: Literal["zh", "en"], draft: ReportDraft | None, passes: bool
) -> None:
    """需求 R341 验收 1, 2, 4: 报告按执行记录的语言出 (draft 为 None 时是 mock 的英文 r1). 提示词写明该语言的两段标题,
    审计师责任段的标题与日期写法, 末尾的输出语言一节随之; 固定段落取 report.yaml 中该语言的一份 (zh 的签名块同 en:
    姓名, 事务所与地址不译). 中文执行记录下英文标题的输出判 semantic, 重试用完执行失败; 英文执行记录照旧通过."""
    raw = draft.model_dump_json() if draft is not None else r1_raw()
    llm = ScriptedLLM({"r1": [raw] * MAX_ATTEMPTS})
    agent, ctx, _ = setup(llm=llm)
    ctx = RunContext(
        ctx.engagement, ctx.procedure, ctx.template, ctx.run.model_copy(update={"language": language}), ctx.state
    )
    library = agent.deps.library
    opinion_heading, basis_heading = library.report_headings(language)[OpinionType.qualified]
    responsibilities = library.report_sections(OpinionType.qualified, language)[ReportSection.auditor_responsibilities]
    if not passes:
        result = await handed_to_human(agent, ctx)
        assert (
            "未通过 semantic 校验" in result["failure_reason"]
            and f"意见段首行应为 {opinion_heading}" in result["failure_reason"]
        )
        return
    [report] = (await agent.run(ctx))["report_versions"]
    prompt = llm.requests[0].messages[0].content
    assert f'the heading "{opinion_heading}"' in prompt and f'the heading "{basis_heading}"' in prompt
    assert f"described in the {responsibilities.splitlines()[0]} section" in prompt
    assert f'Dates as "{library.report_date(DATE_EXAMPLE, language)}"' in prompt
    assert all(f'"{text}"' in prompt for text in library.report_terms(language).values())  # 名称按固定段落的写法
    assert f"Working language: {'Simplified Chinese' if language == 'zh' else 'Australian English'}." in prompt
    expected = fixed(OpinionType.qualified, language)
    assert {s: report.sections[s] for s in expected} == expected
    written = draft or ReportDraft.model_validate_json(r1_raw())
    assert (report.sections[ReportSection.opinion], report.sections[ReportSection.basis_for_opinion]) == (
        written.opinion,
        written.basis_for_opinion,
    )
    assert report.sections[ReportSection.signature] == fixed(OpinionType.qualified, "en")[ReportSection.signature]


async def test_same_state_same_report() -> None:
    first_agent, first_ctx, _ = setup()
    second_agent, second_ctx, _ = setup()
    first = (await first_agent.run(first_ctx))["report_versions"]
    second = (await second_agent.run(second_ctx))["report_versions"]
    assert first == second


REJECT = GateDecision(  # 合成数据集设计第 7 节第 17 行的第一次退回
    id="D-r",
    gate=GateId.RPT_01,
    target_id="REPORT-1",
    target_version=1,
    decision=GateVerdict.reject,
    annotations=[
        Annotation(
            section=ReportSection.basis_for_opinion,
            comment="Cite the contract clause that sets the point at which control transfers.",
        )
    ],
    reason="Basis section needs the contract term",
    decided_by="U-1",
    decided_at=NOW,
)


def report(revision: int, basis: str = "Basis for Qualified Opinion\n\nold basis.") -> AuditReportVersion:
    return AuditReportVersion(
        id=f"REPORT-{revision}",
        revision=revision,
        opinion_type=OpinionType.qualified,
        sections={
            ReportSection.opinion: f"Qualified Opinion\n\nrevision {revision}.",
            ReportSection.basis_for_opinion: basis,
        },
        judgment_refs=[JudgmentRef(id=CUTOFF.id, version=2)],
    )


def revising(state: dict[str, Any], run_no: int, llm: Any = None) -> tuple[ConciergeAgent, RunContext, FakeTrail]:
    """RPT-01 退回后的再一次 RPT-03 执行 (trigger rerun)."""
    agent, ctx, trail = setup(llm=llm)
    run = make_run("RPT-03", run_no=run_no, params=ctx.run.params_snapshot)
    return agent, RunContext(ctx.engagement, ctx.procedure, ctx.template, run, ctx.state | state), trail  # type: ignore[arg-type]


@pytest.mark.parametrize("language", ["en", "zh"], ids=["same_language", "en_to_zh"])
async def test_second_revision_addresses_reject(language: Literal["zh", "en"]) -> None:
    """退回后的第 2 稿带上一稿与批注. 上一稿 (没有 run_id 的旧稿, 按 en) 与本次同语言时要求其余措辞不变; 本次为 zh
    (退回后项目改了工作语言) 时写明上一稿是英文, 按中文重写全文, 保持意思, 不再要求措辞不变 (R341 审核 01)."""
    first = report(1)
    llm = ScriptedLLM({"r2": [ZH_R1.model_dump_json()]}) if language == "zh" else None
    agent, ctx, trail = revising(
        {"report_versions": [first], "gate_decisions": [fin01(OpinionType.qualified), REJECT]}, run_no=2, llm=llm
    )
    ctx = replace(ctx, run=ctx.run.model_copy(update={"language": language}))
    patch = await agent.run(ctx)
    [report2] = patch["report_versions"]
    assert (report2.id, report2.version, report2.revision, report2.addresses_decision_id) == ("REPORT-2", 1, 2, "D-r")
    assert patch["procedure_runs"][0].id == "RUN-RPT-03-2" and patch["procedure_runs"][0].trigger is RunTrigger.rerun
    assert ctx.state["report_versions"] == [first]  # REPORT-1 不变
    [entry] = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    assert entry.payload["fixture_key"] == "r2"
    prompt = entry.payload["messages"][0]["content"]
    assert "## Revision 2" in prompt
    assert first.sections[ReportSection.opinion] in prompt and first.sections[ReportSection.basis_for_opinion] in prompt
    assert "- [basis_for_opinion]: Cite the contract clause that sets the point at which control transfers." in prompt
    rewrite = (
        "The previous text is in Australian English, not Simplified Chinese: write both sections in full in Simplified "
        "Chinese, keeping their meaning; do not keep its wording."
    )
    keep = "Keep all other wording unchanged."
    assert (rewrite in prompt, keep in prompt) == ((True, False) if language == "zh" else (False, True))


async def test_r2_fixture_cites_contract_clause() -> None:
    agent, ctx, _ = revising(
        {"report_versions": [report(1)], "gate_decisions": [fin01(OpinionType.qualified), REJECT]}, 2
    )
    [report2] = (await agent.run(ctx))["report_versions"]
    basis = report2.sections[ReportSection.basis_for_opinion]
    assert "Clause 5.1 of contract CON-001" in basis and "signs the delivery note" in basis
    assert report2.judgment_refs == [JudgmentRef(id=CUTOFF.id, version=2)]


async def test_third_revision_uses_latest_reject_only() -> None:
    second_reject = REJECT.model_copy(
        update={
            "id": "D-r2",
            "target_id": "REPORT-2",
            "annotations": [Annotation(section=ReportSection.opinion, comment="Shorten the opinion.")],
        }
    )
    pending = REJECT.model_copy(
        update={"id": "D-info", "decision": GateVerdict.insufficient_info, "annotations": [], "target_id": "REPORT-2"}
    )
    report2 = report(2, "Basis for Qualified Opinion\n\nsecond basis.")
    state = {
        "report_versions": [report(1), report2],
        "gate_decisions": [fin01(OpinionType.qualified), REJECT, second_reject, pending],
    }
    llm = ScriptedLLM({"r3": [ReportDraft.model_validate_json(r1_raw()).model_dump_json()]})
    agent, ctx, _ = revising(state, run_no=3, llm=llm)
    [report3] = (await agent.run(ctx))["report_versions"]
    assert (report3.id, report3.revision, report3.addresses_decision_id) == ("REPORT-3", 3, "D-r2")
    prompt = llm.requests[0].messages[0].content
    assert "second basis." in prompt and "old basis." not in prompt
    assert "- [opinion]: Shorten the opinion." in prompt and "Cite the contract clause" not in prompt


async def test_latest_reject_of_previous_report_wins() -> None:
    """同一稿有两条退回决定时取后一条的批注."""
    later = REJECT.model_copy(
        update={"id": "D-r-later", "annotations": [Annotation(section=ReportSection.opinion, comment="Later note.")]}
    )
    state = {"report_versions": [report(1)], "gate_decisions": [fin01(OpinionType.qualified), REJECT, later]}
    llm = ScriptedLLM({"r2": [r1_raw()]})
    agent, ctx, _ = revising(state, run_no=2, llm=llm)
    [report2] = (await agent.run(ctx))["report_versions"]
    assert report2.addresses_decision_id == "D-r-later"
    prompt = llm.requests[0].messages[0].content
    assert "- [opinion]: Later note." in prompt and "Cite the contract clause" not in prompt


@pytest.mark.parametrize(
    "decisions",
    [[], [REJECT.model_copy(update={"target_id": "REPORT-0"})]],
    ids=["no_reject", "reject_of_other_report"],
)
async def test_revision_without_reject_of_previous_report_raises(decisions: list[GateDecision]) -> None:
    """修订时须有针对上一稿的退回决定, 否则报错 (不拿别的报告的批注来改)."""
    agent, ctx, _ = revising({"report_versions": [report(1)], "gate_decisions": decisions}, run_no=2)
    with pytest.raises(LookupError, match="REPORT-1 的退回决定"):
        await agent.run(ctx)


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
    patch, writer, _ = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
    assert writer is AgentId.A1 and "report_versions" not in patch
    [run] = patch["procedure_runs"]
    assert run.id == "RUN-RPT-03-1" and run.status is RunStatus.failed
    [task] = patch["review_tasks"]
    assert (task.id, task.kind, task.subject_id) == ("T-RUN-RPT-03-1-v1", TaskKind.failed, None)
    assert agent.deps.queue.allowed(task) == frozenset({Decision.rerun})
    return {"failure_reason": run.failure_reason}


@pytest.mark.parametrize(
    ("bad", "level", "error"),
    [
        ("x", "syntax", "Output is not valid JSON"),
        (r1_raw(judgment_keys=["REV-02:SI-2026-0618"]), "reference", "REV-02:SI-2026-0618"),
        (r1_raw(opinion="Opinion\n\nclean"), "semantic", "Qualified Opinion"),
    ],
    ids=["syntax", "reference", "semantic"],
)
async def test_failures_of_any_level_fail_the_run(bad: str, level: str, error: str) -> None:
    """任一级不过都回传重试 (语法与结构 R34, 引用与语义 R348); MAX_ATTEMPTS 次都不过即执行失败, 失败原因带级别与
    错误, 每次一行留痕, 备着的合格稿不再调用."""
    llm = ScriptedLLM({"r1": [*[bad] * MAX_ATTEMPTS, r1_raw()]})
    agent, ctx, trail = setup(llm=llm)
    result = await handed_to_human(agent, ctx)
    assert f"未通过 {level} 校验" in result["failure_reason"] and error in result["failure_reason"]
    assert len(llm.requests) == MAX_ATTEMPTS
    assert len([e for e in trail.entries if e.kind is TrailKind.llm_call]) == MAX_ATTEMPTS


async def test_report_node_failure_waits_then_rerun_drafts_report() -> None:
    """(Codex R52 探针) report_draft 节点中校验失败 (意见段标题不对, 语义级重试用完): 写失败的 Run, 任务与
    awaiting_human, 节点内等待, 不出报告; 重放不重调模型; 审计师 rerun 后第 2 次执行出 REPORT-1."""
    bad_replies = [r1_raw(opinion="## Qualified Opinion\n\nbody")] * MAX_ATTEMPTS
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
    """错报按汇总表顺序; 来源判断只取已确认且在状态中的 (各取当前版本), 字段为引用键加提示词要的六个."""
    _, ctx, _ = setup()
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

    values = misstatement_inputs(replace(ctx, state=state))

    assert [m["id"] for m in values["misstatements"]] == [m.id for m in ordered]
    assert [(j["key"], j["id"], j["version"]) for j in values["judgments"]] == [
        ("REV-02:SI-2026-0612:J-SECOND", second_j.id, 7),
        ("REV-02:SI-2026-0612", CUTOFF.id, 2),
    ]
    fields = {"key", "id", "version", "procedure_id", "subject_id", "conclusion", "evidence"}
    assert all(set(j) == fields for j in values["judgments"])


def test_schedule_misstatement_missing_from_state_raises() -> None:
    _, ctx, _ = setup()
    with pytest.raises(KeyError, match=MISSTATEMENT.id):
        misstatement_inputs(replace(ctx, state=ctx.state | {"misstatements": []}))


def test_fin01_reason_is_last_approval() -> None:
    """FIN-01 理由取最后一条 FIN-01 通过决定的, 不取驳回或其他闸门的."""
    _, ctx, _ = setup()
    early = fin01(OpinionType.qualified).model_copy(update={"id": "D-EARLY", "reason": "first approval"})
    last = early.model_copy(update={"id": "D-LAST", "reason": "last approval"})
    reject = early.model_copy(update={"id": "D-REJECT", "reason": "reject", "decision": GateVerdict.reject})
    other = early.model_copy(update={"id": "D-OTHER", "gate": GateId.ACC_01, "reason": "different gate"})
    state = ctx.state | {"gate_decisions": [early, last, reject, other]}
    assert fin01_reason(replace(ctx, state=state)) == "last approval"


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


# ── Codex R53 探针: 主图上的退回与修订 ─────────────────────────────────────────


@pytest.mark.parametrize("crash_after_store", [False, True], ids=["normal", "crash_after_store"])
async def test_reject_revision_and_sign_on_main_graph(monkeypatch: pytest.MonkeyPatch, crash_after_store: bool) -> None:
    """主图: RPT-01 退回 → A10 出第 2 稿 → 再停在 RPT-01, 呈报 REPORT-2 → 通过后签字归档. crash_after_store 时
    第 2 稿写入 Store 后, 检查点更新前中断, resume 后重放取 Store, 不再调模型."""
    original = GraphNodes.attempt
    crashed = False

    async def attempt(self: GraphNodes, *args: Any, **kwargs: Any) -> Any:
        nonlocal crashed
        result = await original(self, *args, **kwargs)
        if crash_after_store and not crashed and any(r.id == "REPORT-2" for r in result[0].get("report_versions", [])):
            crashed = True
            raise RuntimeError("R53: 第二稿写 Store 后, 检查点更新前中断")
        return result

    monkeypatch.setattr(GraphNodes, "attempt", attempt)
    s = await start_numbat(lambda deps: {**numbat_all_p2_agents(deps), "RPT-03": ConciergeAgent(deps)})
    await approve_materiality(s)
    await wait_idle(s.runner, EID)
    await review_all(s)
    await s.submit.submit(EID, fin_decision(GateVerdict.approve))
    await wait_idle(s.runner, EID)
    config = GraphRunner.config(EID)
    [first] = (await s.runner.app.aget_state(config)).values["report_versions"]

    reject = rpt_decision(GateVerdict.reject, id="D-R53-REJECT")
    await s.submit.submit(EID, reject)
    await wait_idle(s.runner, EID)
    if crash_after_store:
        assert crashed and "R53" in (s.runner.run_error(EID) or "")
        assert (await s.runner.app.aget_state(config)).values["report_versions"] == [first]
        await s.runner.resume(EID)
        await wait_idle(s.runner, EID)

    assert s.runner.run_error(EID) is None
    assert await s.next_nodes() == ("gate_rpt01",)
    values = (await s.runner.app.aget_state(config)).values
    assert [r.id for r in values["report_versions"]] == ["REPORT-1", "REPORT-2"] and values["report_versions"][
        0
    ] == first
    second = values["report_versions"][1]
    assert (second.revision, second.version, second.addresses_decision_id) == (2, 1, reject.id)
    assert "Clause 5.1 of contract CON-001" in second.sections[ReportSection.basis_for_opinion]
    runs = [r for r in values["procedure_runs"] if r.procedure_id == "RPT-03"]
    assert [(r.id, r.trigger, r.status) for r in runs] == [
        ("RUN-RPT-03-1", RunTrigger.initial, RunStatus.succeeded),
        ("RUN-RPT-03-2", RunTrigger.rerun, RunStatus.succeeded),
    ]
    calls = [e for e in s.runner.deps.trail.entries if e.kind is TrailKind.llm_call and e.procedure_id == "RPT-03"]
    assert [e.payload["fixture_key"] for e in calls] == ["r1", "r2"]
    assert first.sections[ReportSection.opinion] in calls[1].payload["messages"][0]["content"]
    view = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).gate(EID)
    assert view is not None and (view.target_id, view.target_version) == ("REPORT-2", 1)

    await s.submit.submit(EID, rpt_decision(GateVerdict.approve, id="D-R53-APPROVE", target_id="REPORT-2"))
    await wait_idle(s.runner, EID)

    values = (await s.runner.app.aget_state(config)).values
    assert values["phase"] is Phase.Archived and values["report_versions"][0] == first
    assert values["report_versions"][1].status is ReportStatus.signed
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None and engagement.locked


def test_previous_draft_is_current_version_of_previous_revision() -> None:
    _, ctx, _ = setup()
    old = report(1, "old version")
    current = old.model_copy(
        update={
            "version": 2,
            "sections": {ReportSection.opinion: "current opinion", ReportSection.basis_for_opinion: "current basis"},
        }
    )
    state = ctx.state | {"report_versions": [current, old], "gate_decisions": [REJECT]}
    inputs = revision_inputs(replace(ctx, state=state), 2)
    assert inputs.previous == {"opinion": "current opinion", "basis_for_opinion": "current basis"}
    assert inputs.annotations[0]["comment"] == REJECT.annotations[0].comment and inputs.addresses == REJECT.id


def test_r2_fixture_only_adds_contract_clause() -> None:
    """r2 与 r1 只差基础段补写的第 5.1 条一句, 意见段与引用不变."""
    root = get_settings().LLM_FIXTURES_ROOT / "RPT-03"
    first = json.loads((root / "r1.json").read_text())
    second = json.loads((root / "r2.json").read_text())
    insertion = (
        "Clause 5.1 of contract CON-001 with Bilby Robotics Pty Ltd provides that title to and risk in the goods pass "
        "to the customer when its authorised representative signs the delivery note. "
    )
    assert second["opinion"] == first["opinion"] and second["judgment_keys"] == first["judgment_keys"]
    assert second["basis_for_opinion"].replace(insertion, "") == first["basis_for_opinion"]
