"""验收: 真实 Agent, 判断引擎与 mock 大模型 (tests/fixtures/llm) 下离线跑完 P0-P4, 四道闸门与全部复核按
合成数据集设计第 7 节决定; 外部系统, 规则引擎与存储用假实现预置 Numbat 的数据."""

from collections import Counter
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.agents.concierge import ConciergeAgent
from backend.agents.ingest import IngestAgent
from backend.agents.risk_assessor import RiskAssessorAgent
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    Assertion,
    AuditException,
    AuditReportVersion,
    Confirmation,
    ConfirmationResult,
    Direction,
    DocumentType,
    ExceptionDisposition,
    GateVerdict,
    Judgment,
    JudgmentRef,
    MaterialityCandidates,
    Misstatement,
    MisstatementType,
    OpinionType,
    Phase,
    ProposedAction,
    ReportSection,
    ReportStatus,
    ReviewStatus,
    ReviewTask,
    TaskStatus,
    TrailKind,
    UncorrectedSchedule,
)
from backend.core.config import get_settings
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.runner import GraphRunner
from backend.services.judgment_engine import OutputValidator, PointerResolver
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_CANDIDATES,
    NUMBAT_CUTOFF_DRAFT,
    NUMBAT_PROFILE,
    NUMBAT_RECORDS,
    NUMBAT_TRIAL_BALANCE,
    FakeConfirmationChannel,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeTrail,
    ScriptAgent,
    make_runner,
    wait_idle,
)
from tests.test_decisions import Started, acc01_decision, pln_decision
from tests.test_flow import FIN_APPROVE, REVIEWS, RPT_APPROVE, RPT_REJECT, submit_gate
from tests.test_p2_agents_demo import replies

EID = ENGAGEMENT.id


def real_agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
    """make_graph_nodes 中仍为脚本的程序换成真实 Agent (其余本来就是注册表里的真实 Agent)."""
    risk = RiskAssessorAgent(deps)
    return {"DOC-04": IngestAgent(deps), "PLN-07": risk, "PLN-01": risk, "RPT-03": ConciergeAgent(deps)}


async def start_demo(
    agents: Callable[[AgentDeps], dict[str, ProcedureAgent[Any]]] = real_agents,
    trail: FakeTrail | None = None,
    confirmations: FakeConfirmationChannel | None = None,
) -> Started:
    """启动 → ACC-01 approve → PLN-01/03 approve (收入 1%, 75%, 5%); 返回时后台正在执行 P2 (不等待).
    财务数据预置 Numbat 的明细行, 试算表与两份回函的登记 (合同由数据集载入登记). 参数用于替换 Agent 或注入故障."""
    financial = FakeFinancialStore(replies(), dict(NUMBAT_RECORDS), trial_balance=NUMBAT_TRIAL_BALANCE)
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(
        engagement_store=engagement_store,
        agents=agents,
        financial_store=financial,
        trail=trail,
        confirmations=confirmations,
    )
    s = Started(runner, store, engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    assert await s.next_nodes() == ("gate_acc01",), runner.run_error(EID)
    await submit_gate(s, acc01_decision(GateVerdict.approve, id="D-ACC"), ("gate_p1_risk",))
    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    return s


async def finish_demo(s: Started) -> None:
    """等后台停下, 按第 7 节提交 6 条复核 (每次提交前停在 await_events), FIN-01 approve qualified,
    RPT-01 先退回再批准, 到归档."""
    await wait_idle(s.runner, EID)
    for decision in REVIEWS:
        assert await s.next_nodes() == ("await_events",), (decision.task_id, s.runner.run_error(EID))
        await s.submit.submit(EID, decision)
        await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)
    await submit_gate(s, FIN_APPROVE, ("gate_rpt01",))
    await submit_gate(s, RPT_REJECT, ("gate_rpt01",))
    await submit_gate(s, RPT_APPROVE, ())
    assert s.runner.run_error(EID) is None


async def run_demo() -> Started:
    s = await start_demo()
    await finish_demo(s)
    return s


@pytest.fixture
async def demo() -> Started:
    return await run_demo()


def trail_of(s: Started) -> FakeTrail:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return trail


async def test_results_match_dataset(demo: Started) -> None:
    await assert_results(demo)


async def assert_results(s: Started) -> None:
    """验收 2: 例外, 判断, 任务, 函证, 错报, 重要性, 汇总表, 报告, 项目与文件的最终状态."""
    store = s.output_store
    exceptions = await store.latest(EID, AuditException)
    assert sorted((x.id, x.disposition) for x in exceptions) == [
        ("X-CSH-02-1-D01-1", ExceptionDisposition.attributed),
        ("X-CSH-05-1-B01-1", ExceptionDisposition.attributed),
        ("X-PLN-07-1-A01-1", ExceptionDisposition.open),
        ("X-REV-08-1-R01-1", ExceptionDisposition.attributed),
    ]
    judgments = await store.latest(EID, Judgment)
    assert sorted((j.id, j.version, j.review_status) for j in judgments) == [
        (jid, 2, ReviewStatus.confirmed)
        for jid in sorted(
            [
                "J-CSH-02-1-BA-01",
                "J-REV-02-1-SI-2026-0612",
                "J-REV-02-1-SI-2026-0618",
                "J-REV-03-1-CON-001",
                "J-REV-03-1-CON-002",
                "J-REV-03-1-CON-003",
            ]
        )
    ]
    assert {x.id: (x.source_ids, x.amount_cents) for x in exceptions} == {
        "X-PLN-07-1-A01-1": (["1100"], 52500000),
        "X-REV-08-1-R01-1": (["SI-2026-0612"], 14200000),
        "X-CSH-05-1-B01-1": (["BA-01"], -5000000),
        "X-CSH-02-1-D01-1": (["BA-01"], -5000000),
    }
    by_id = {j.id: j for j in judgments}
    cutoff = by_id["J-REV-02-1-SI-2026-0612"]
    assert (cutoff.proposed_action, cutoff.misstatement_draft) == (ProposedAction.misstatement, NUMBAT_CUTOFF_DRAFT)
    assert cutoff.exception_ids == ["X-REV-08-1-R01-1"]
    cash = by_id["J-CSH-02-1-BA-01"]
    assert (cash.proposed_action, cash.exception_ids) == (
        ProposedAction.close,
        ["X-CSH-05-1-B01-1", "X-CSH-02-1-D01-1"],
    )
    tasks = await store.latest(EID, ReviewTask)
    assert sorted(t.target_id for t in tasks) == sorted(by_id) and all(t.status is TaskStatus.decided for t in tasks)
    confirmations = {c.id: c for c in await store.latest(EID, Confirmation)}
    assert {
        cid: (c.result, c.confirmed_balance_cents, c.balance_date, c.book_balance_cents, c.difference_cents)
        for cid, c in confirmations.items()
    } == {
        "C-BA-01": (ConfirmationResult.difference, 120840000, date(2026, 6, 30), 125840000, -5000000),
        "C-BA-02": (ConfirmationResult.agreed, 40000000, date(2026, 6, 30), 40000000, 0),
    }
    [misstatement] = await store.latest(EID, Misstatement)
    assert (
        misstatement.judgment_id,
        misstatement.type,
        misstatement.account,
        misstatement.assertion,
        misstatement.amount_cents,
        misstatement.direction,
        misstatement.status,
    ) == (
        "J-REV-02-1-SI-2026-0612",
        MisstatementType.factual,
        "Revenue",
        Assertion.Cutoff,
        14200000,
        Direction.overstated,
        "uncorrected",
    )
    contracts = await s.runner.deps.financial_store.contracts(EID)
    assert sorted(c.id for c in contracts) == ["CON-001", "CON-002", "CON-003"]
    assert sum(len(c.terms) for c in contracts) == 11  # 第 6.6 节: 3 份合同共 11 条条款
    values = (await s.runner.app.aget_state(GraphRunner.config(EID))).values
    assert (values["materiality_cents"], values["performance_materiality_cents"], values["clearly_trivial_cents"]) == (
        12000000,
        9000000,
        600000,
    )
    candidates = await store.get(EID, MaterialityCandidates, "MATERIALITY")
    assert candidates is not None
    assert [(c.basis, c.base_amount_cents) for c in candidates.candidates] == [
        (c.basis, c.base_amount_cents) for c in NUMBAT_CANDIDATES
    ]
    schedule = await store.get(EID, UncorrectedSchedule, "SCHEDULE")
    assert schedule is not None
    assert (schedule.total_cents, schedule.materiality_cents, schedule.candidate_opinion) == (
        14200000,
        12000000,
        OpinionType.qualified,
    )
    assert [(i.misstatement_id, i.amount_cents) for i in schedule.items] == [(misstatement.id, 14200000)]
    reports = {r.id: r for r in await store.latest(EID, AuditReportVersion)}
    assert (reports["REPORT-1"].version, reports["REPORT-1"].status) == (1, ReportStatus.draft)
    signed = reports["REPORT-2"]
    assert (signed.version, signed.status, signed.addresses_decision_id, signed.approved_by) == (
        2,
        ReportStatus.signed,
        RPT_REJECT.id,
        "U-1",
    )
    assert signed.judgment_refs == [JudgmentRef(id="J-REV-02-1-SI-2026-0612", version=2)]
    assert signed.sections[ReportSection.opinion].startswith("Qualified Opinion")
    assert "Clause 5.1 of contract CON-001" in signed.sections[ReportSection.basis_for_opinion]
    assert signed.sections[ReportSection.signature].startswith("Jordan Ellis\n")
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None and (engagement.phase, engagement.locked) == (Phase.Archived, True)
    documents = await s.runner.deps.financial_store.documents(EID)
    assert sorted((d.type, d.status) for d in documents) == [
        *[(DocumentType.confirmation, "extracted")] * 2,
        *[(DocumentType.contract, "extracted")] * 3,
    ]


async def test_trail_counts(demo: Started) -> None:
    assert_trail_counts(demo)


def assert_trail_counts(s: Started) -> None:
    """验收 3: llm_call 13 行 (DOC-04 3, DOC-06 2, REV-03 3, REV-02 2, CSH-02 1, RPT-03 2), decision 11 行,
    external_call 4 行 (ACC-04 2, CSH-04 2)."""
    entries = trail_of(s).entries
    llm = Counter(e.procedure_id for e in entries if e.kind is TrailKind.llm_call)
    assert llm == {"DOC-04": 3, "DOC-06": 2, "REV-03": 3, "REV-02": 2, "CSH-02": 1, "RPT-03": 2}
    assert len([e for e in entries if e.kind is TrailKind.decision]) == 11
    external = Counter(e.procedure_id for e in entries if e.kind is TrailKind.external_call)
    assert external == {"ACC-04": 2, "CSH-04": 2}


async def test_every_evidence_pointer_resolves(demo: Started) -> None:
    await assert_pointers_resolve(demo)


async def assert_pointers_resolve(s: Started) -> None:
    """验收 4: 判断 (推理链与证据), 合同与函证的证据指针都能解析 (行存在, 页码不超过页数, 准则已注册, 留痕存在)."""
    deps = s.runner.deps
    resolver = PointerResolver(deps.judgments.validator, EID)
    pointers = []
    for j in await s.output_store.latest(EID, Judgment):
        pointers += [*j.evidence, *(p for step in j.reasoning_chain for p in step.evidence)]
    for c in await deps.financial_store.contracts(EID):
        pointers += c.evidence
    for c in await s.output_store.latest(EID, Confirmation):
        pointers += c.evidence
    assert len(pointers) > 30
    for p in pointers:
        resolved, reason = await resolver.resolve(p)
        assert resolved is not None, (p, reason)


async def test_every_fixture_used_once_and_passes(demo: Started) -> None:
    assert_fixtures_used_once(demo)


def assert_fixtures_used_once(s: Started) -> None:
    """验收 5: tests/fixtures/llm 下每个文件都在流程中被读到一次, 一次通过四级校验 (attempt 1, 无失败级别)."""
    root = get_settings().LLM_FIXTURES_ROOT
    files = sorted((p.parent.name, p.stem) for p in root.glob("*/*.json"))
    calls = [e for e in trail_of(s).entries if e.kind is TrailKind.llm_call]
    assert sorted((e.procedure_id, e.payload["fixture_key"]) for e in calls) == files
    assert all(e.payload["attempt"] == 1 and e.payload["level_failed"] is None for e in calls)


async def test_locked_after_signing(demo: Started) -> None:
    """验收 6: 签字后提交任一决定报 ENGAGEMENT_LOCKED."""
    with pytest.raises(DecisionRejected) as e:
        await demo.submit.submit(EID, RPT_APPROVE.model_copy(update={"id": "D-AFTER"}))
    assert e.value.code == "ENGAGEMENT_LOCKED"


async def test_every_program_runs_with_a_real_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """(Codex R55 探针) 流程中每条程序都由真实 Agent 执行 (没有脚本 Agent), 各一次 (RPT-03 两稿两次);
    大模型请求的输出模型与次数, 每次校验都无错误也无无支撑步骤."""
    calls: list[tuple[str, str]] = []
    validations: list[str] = []
    run_agent, validate = GraphNodes.run_agent, OutputValidator.validate

    async def record_agent(self: GraphNodes, agent: ProcedureAgent[Any], ctx: RunContext) -> Any:
        assert not isinstance(agent, ScriptAgent), ctx.procedure.id
        calls.append((ctx.procedure.id, type(agent).__name__))
        return await run_agent(self, agent, ctx)

    async def record_validate(self: OutputValidator, *args: Any) -> Any:
        report = await validate(self, *args)
        assert report.level_failed is None and not report.errors and not report.unsupported_steps
        validations.append(args[1].id)
        return report

    monkeypatch.setattr(GraphNodes, "run_agent", record_agent)
    monkeypatch.setattr(OutputValidator, "validate", record_validate)
    await run_demo()

    agents = {
        "ACC-04": "ClientIntelAgent",
        "DOC-04": "IngestAgent",
        "PLN-07": "RiskAssessorAgent",
        "PLN-01": "RiskAssessorAgent",
        "REV-08": "FieldworkAgent",
        "REV-03": "FieldworkAgent",
        "REV-02": "FieldworkAgent",
        "CSH-04": "EvidenceAgent",
        "CSH-05": "FieldworkAgent",
        "CSH-02": "FieldworkAgent",
        "DOC-06": "IngestAgent",
        "FIN-04": "QCAgent",
        "RPT-03": "ConciergeAgent",
    }
    assert dict(calls) == agents
    assert Counter(pid for pid, _ in calls) == {pid: 2 if pid == "RPT-03" else 1 for pid in agents}
    assert Counter(validations) == {"DOC-04": 3, "DOC-06": 2, "REV-03": 3, "REV-02": 2, "CSH-02": 1, "RPT-03": 2}
