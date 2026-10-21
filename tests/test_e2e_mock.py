"""验收: 真实 Agent, 判断引擎与 mock 大模型 (tests/fixtures/llm) 下离线跑完 P0-P4, 四道闸门与全部复核按
合成数据集设计第 7 节决定; 外部系统, 规则引擎与存储用假实现预置 Numbat 的数据."""

import json
import os
import shutil
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from backend.adapters.llm import llm_version
from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.agents.concierge import ConciergeAgent
from backend.agents.ingest import IngestAgent
from backend.agents.risk_assessor import RiskAssessorAgent
from backend.contracts.errors import DecisionRejected, EngagementLocked
from backend.contracts.models import (
    AcceptanceScorecard,
    Assertion,
    AuditException,
    AuditReportVersion,
    Confirmation,
    ConfirmationResult,
    Direction,
    DocumentType,
    EvidencePointer,
    ExceptionDisposition,
    GateVerdict,
    Judgment,
    JudgmentRef,
    MaterialityCandidates,
    Misstatement,
    MisstatementType,
    OpinionType,
    Phase,
    ProcedureRun,
    ProposedAction,
    ReportSection,
    ReportStatus,
    ReviewRecord,
    ReviewStatus,
    ReviewTask,
    TaskStatus,
    TrailEntry,
    TrailKind,
    UncorrectedSchedule,
)
from backend.contracts.ports import LLMClient
from backend.core.config import Settings, get_settings
from backend.main import build_deps
from backend.orchestration.checkpoint import open_checkpointer
from backend.orchestration.graph import compile_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.runner import GraphRunner
from backend.persistence import models as m
from backend.persistence.stores import RoutingDatasetLoader, now
from backend.services.data_service import DataService
from backend.services.judgment_engine import OutputValidator, PointerResolver
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_CANDIDATES,
    NUMBAT_CUTOFF_DRAFT,
    NUMBAT_FEATURES,
    NUMBAT_PROFILE,
    NUMBAT_RECORDS,
    NUMBAT_TRIAL_BALANCE,
    FakeConfirmationChannel,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    ScriptAgent,
    evidence_pointers,
    make_deps,
    make_runner,
    wait_idle,
)
from tests.test_decisions import Started, acc01_decision, pln_decision
from tests.test_flow import FIN_APPROVE, REVIEWS, RPT_APPROVE, RPT_REJECT, submit_gate
from tests.test_p2_agents_demo import replies

EID = ENGAGEMENT.id
REPO = Path(__file__).resolve().parents[1]
shared_loop = pytest.mark.asyncio(loop_scope="session")  # 读会话共用的演示 (conftest 的 demo_run) 的用例与它同一个循环


def real_agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
    """make_graph_nodes 中仍为脚本的程序换成真实 Agent (其余本来就是注册表里的真实 Agent)."""
    risk = RiskAssessorAgent(deps)
    return {"DOC-04": IngestAgent(deps), "PLN-07": risk, "PLN-01": risk, "RPT-03": ConciergeAgent(deps)}


async def start_demo(
    agents: Callable[[AgentDeps], dict[str, ProcedureAgent[Any]]] = real_agents,
    trail: FakeTrail | None = None,
    confirmations: FakeConfirmationChannel | None = None,
    review_llm: LLMClient | None = None,
    features: frozenset[str] = NUMBAT_FEATURES,
    llm: LLMClient | None = None,
    library: ProcedureLibrary | None = None,
    registry: FakeRegistry | None = None,
) -> Started:
    """启动 → ACC-01 approve → PLN-01/03 approve (收入 1%, 75%, 5%); 返回时后台正在执行 P2 (不等待).
    财务数据预置 Numbat 的明细行, 试算表与两份回函的登记 (合同由数据集载入登记). 参数用于替换 Agent 或注入故障;
    llm 与 library 换主模型与程序库 (如改了某程序自主度的副本, 程序库的功能集须同 features); registry 换登记系统
    (缺省为 make_runner 的 Numbat 记录, 如 GEN-08 的穿透用例另带摘录)."""
    financial = FakeFinancialStore(replies(), dict(NUMBAT_RECORDS), trial_balance=NUMBAT_TRIAL_BALANCE)
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(
        registry=registry,
        engagement_store=engagement_store,
        agents=agents,
        financial_store=financial,
        trail=trail,
        confirmations=confirmations,
        review_llm=review_llm,
        features=features,
        llm=llm,
        library=library,
    )
    s = Started(runner, store, engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    assert await s.next_nodes() == ("gate_acc01",), runner.run_error(EID)
    await submit_gate(s, acc01_decision(GateVerdict.approve, id="D-ACC"), ("gate_p1_risk",))
    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    return s


async def finish_demo(
    s: Started,
    reviews: Sequence[ReviewRecord] = REVIEWS,
    before_review: Callable[[GraphRunner, str, ReviewRecord], Awaitable[None]] | None = None,
) -> None:
    """等后台停下, 按第 7 节提交 6 条复核 (reviews, 每次提交前停在 await_events), FIN-01 approve qualified,
    RPT-01 先退回再批准, 到归档. before_review 在提交每条复核之前以 (runner, eid, 复核记录) 调 (如先呈递它的任务,
    需求 R155; 同 xero_fakes.finish_xero17)."""
    await wait_idle(s.runner, EID)
    for decision in reviews:
        assert await s.next_nodes() == ("await_events",), (decision.task_id, s.runner.run_error(EID))
        if before_review is not None:
            await before_review(s.runner, EID, decision)
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


@dataclass
class Demo:
    """会话共用的一次演示 (conftest 的 demo_run): 流程入口 s; 每次执行程序的 (程序, Agent 类名, 是否脚本 Agent); 每次
    四级校验的 (判断所属程序, 失败级别, 错误数, 无支撑步骤数)."""

    s: Started
    agents: list[tuple[str, str, bool]]
    validations: list[tuple[str, int | None, int, int]]


async def record_demo() -> Demo:
    """跑一次演示 (run_demo), 同时记下每次执行的 Agent 与每次四级校验的结果. 会话里只跑一次, 见 conftest 的 demo_run:
    用例只读存储, 留痕与图状态; 签字后再提交的决定在写入前就被拒绝, 也不改状态."""
    agents: list[tuple[str, str, bool]] = []
    validations: list[tuple[str, int | None, int, int]] = []
    run_agent, validate = GraphNodes.run_agent, OutputValidator.validate

    async def record_agent(self: GraphNodes, agent: ProcedureAgent[Any], ctx: RunContext) -> Any:
        agents.append((ctx.procedure.id, type(agent).__name__, isinstance(agent, ScriptAgent)))
        return await run_agent(self, agent, ctx)

    async def record_validate(self: OutputValidator, *args: Any) -> Any:
        report = await validate(self, *args)
        validations.append((args[1].id, report.level_failed, len(report.errors), len(report.unsupported_steps)))
        return report

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(GraphNodes, "run_agent", record_agent)
        patch.setattr(OutputValidator, "validate", record_validate)
        s = await run_demo()
    return Demo(s, agents, validations)


@pytest.fixture
def demo(demo_run: Demo) -> Started:
    return demo_run.s


def trail_of(s: Started) -> FakeTrail:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return trail


@shared_loop
async def test_results_match_dataset(demo: Started) -> None:
    await assert_results(demo)
    # 功能 collab 关闭 (R149 验收 5): A7 不记命中的触发规则
    assert {j.critic_trigger for j in await demo.output_store.latest(EID, Judgment)} == {None}


async def assert_results(s: Started, eid: str = EID, approver: str = "U-1") -> None:
    """验收 2: 例外, 判断, 任务, 函证, 错报, 重要性, 汇总表, 报告, 项目与文件的最终状态.
    eid 为项目编号, approver 为签字人 (项目负责人) 的用户 ID; 存储可以是假实现或真实实现."""
    store = s.output_store
    exceptions = await store.latest(eid, AuditException)
    assert sorted((x.id, x.disposition) for x in exceptions) == [
        ("X-CSH-02-1-D01-1", ExceptionDisposition.attributed),
        ("X-CSH-05-1-B01-1", ExceptionDisposition.attributed),
        ("X-PLN-07-1-A01-1", ExceptionDisposition.open),
        ("X-REV-08-1-R01-1", ExceptionDisposition.attributed),
    ]
    judgments = await store.latest(eid, Judgment)
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
    tasks = await store.latest(eid, ReviewTask)
    assert sorted(t.target_id for t in tasks) == sorted(by_id) and all(t.status is TaskStatus.decided for t in tasks)
    confirmations = {c.id: c for c in await store.latest(eid, Confirmation)}
    assert {
        cid: (c.result, c.confirmed_balance_cents, c.balance_date, c.book_balance_cents, c.difference_cents)
        for cid, c in confirmations.items()
    } == {
        "C-BA-01": (ConfirmationResult.difference, 120840000, date(2026, 6, 30), 125840000, -5000000),
        "C-BA-02": (ConfirmationResult.agreed, 40000000, date(2026, 6, 30), 40000000, 0),
    }
    [misstatement] = await store.latest(eid, Misstatement)
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
    contracts = await s.runner.deps.financial_store.contracts(eid)
    assert sorted(c.id for c in contracts) == ["CON-001", "CON-002", "CON-003"]
    assert sum(len(c.terms) for c in contracts) == 11  # 第 6.6 节: 3 份合同共 11 条条款
    values = (await s.runner.app.aget_state(GraphRunner.config(eid))).values
    assert (values["materiality_cents"], values["performance_materiality_cents"], values["clearly_trivial_cents"]) == (
        12000000,
        9000000,
        600000,
    )
    candidates = await store.get(eid, MaterialityCandidates, "MATERIALITY")
    assert candidates is not None
    assert [(c.basis, c.base_amount_cents) for c in candidates.candidates] == [
        (c.basis, c.base_amount_cents) for c in NUMBAT_CANDIDATES
    ]
    schedule = await store.get(eid, UncorrectedSchedule, "SCHEDULE")
    assert schedule is not None
    assert (schedule.total_cents, schedule.materiality_cents, schedule.candidate_opinion) == (
        14200000,
        12000000,
        OpinionType.qualified,
    )
    assert [(i.misstatement_id, i.amount_cents) for i in schedule.items] == [(misstatement.id, 14200000)]
    reports = {r.id: r for r in await store.latest(eid, AuditReportVersion)}
    assert (reports["REPORT-1"].version, reports["REPORT-1"].status) == (1, ReportStatus.draft)
    signed = reports["REPORT-2"]
    assert (signed.version, signed.status, signed.addresses_decision_id, signed.approved_by) == (
        2,
        ReportStatus.signed,
        RPT_REJECT.id,
        approver,
    )
    assert signed.judgment_refs == [JudgmentRef(id="J-REV-02-1-SI-2026-0612", version=2)]
    assert signed.sections[ReportSection.opinion].startswith("Qualified Opinion")
    assert "Clause 5.1 of contract CON-001" in signed.sections[ReportSection.basis_for_opinion]
    assert signed.sections[ReportSection.signature].startswith("Jordan Ellis\n")
    engagement = await s.engagement_store.get(eid)
    assert engagement is not None and (engagement.phase, engagement.locked) == (Phase.Archived, True)
    documents = await s.runner.deps.financial_store.documents(eid)
    assert sorted((d.type, d.status) for d in documents) == [
        *[(DocumentType.confirmation, "extracted")] * 2,
        *[(DocumentType.contract, "extracted")] * 3,
    ]


@shared_loop
async def test_trail_counts(demo: Started) -> None:
    assert_trail_counts(trail_of(demo).entries)


@shared_loop
async def test_no_rule_is_left_unexecuted(demo: Started) -> None:
    await assert_all_rules_ran(demo)


async def assert_all_rules_ran(s: Started, eid: str = EID) -> None:
    """需求 R130 零影响: 现有程序都没有执行前检查, 跑完后存储里每条执行记录的 rule_outcomes 都为空 (连库的变体用
    真实规则引擎与 Postgres 存储). 防给现有程序记出未执行的规则."""
    runs = await s.output_store.latest(eid, ProcedureRun)
    assert {r.procedure_id for r in runs} >= {"PLN-07", "PLN-01", "REV-08", "CSH-05", "REV-02", "CSH-02"}
    assert [r.id for r in runs if r.rule_outcomes] == []


def assert_trail_counts(entries: Sequence[TrailEntry]) -> None:
    """验收 3: llm_call 15 行 (DOC-04 3, DOC-06 2, REV-03 3, REV-02 2, CSH-02 1, GEN-09 2, RPT-03 2), decision 11 行,
    external_call 4 行 (ACC-04 2, CSH-04 2). GEN-09 两行是 A7 对 BA-01 (L3, 建议关闭) 的两轮独立复核.
    entries 为该项目的全部留痕."""
    llm = Counter(e.procedure_id for e in entries if e.kind is TrailKind.llm_call)
    assert llm == {"DOC-04": 3, "DOC-06": 2, "REV-03": 3, "REV-02": 2, "CSH-02": 1, "GEN-09": 2, "RPT-03": 2}
    assert len([e for e in entries if e.kind is TrailKind.decision]) == 11
    external = Counter(e.procedure_id for e in entries if e.kind is TrailKind.external_call)
    assert external == {"ACC-04": 2, "CSH-04": 2}


@shared_loop
async def test_every_evidence_pointer_resolves(demo: Started) -> None:
    await assert_pointers_resolve(demo)


async def assert_pointers_resolve(s: Started, eid: str = EID, *, everything: bool = False) -> list[EvidencePointer]:
    """验收 4: 判断 (推理链与证据), 合同与函证的证据指针都能解析 (行存在, 页码不超过页数, 准则已注册, 留痕存在);
    everything 同 evidence_pointers. 返回检查过的指针."""
    resolver = PointerResolver(s.runner.deps.judgments.validator, eid, None)
    pointers = await evidence_pointers(s.runner.deps, eid, everything=everything)
    assert len(pointers) > 30
    for p in pointers:
        resolved, reason = await resolver.resolve(p)
        assert resolved is not None, (p, reason)
    return pointers


@shared_loop
async def test_everything_adds_exception_and_scorecard_pointers(demo: Started) -> None:
    """(Codex R56 探针) everything 时在判断, 合同, 函证之外另收例外与承接评分卡的全部指针, 一条不漏."""
    base = await evidence_pointers(demo.runner.deps)
    full = await evidence_pointers(demo.runner.deps, everything=True)
    exceptions = await demo.output_store.latest(EID, AuditException)
    [card] = await demo.output_store.latest(EID, AcceptanceScorecard)
    assert len(exceptions) == 4 and all(x.evidence for x in exceptions) and len(card.registry_checks) == 3
    extra = [p for x in exceptions for p in x.evidence] + [check.evidence for check in card.registry_checks]
    assert len(full) == len(base) + len(extra)
    assert all(p in full for p in [*base, *extra])


# R55 读到的 15 个 fixture 文件, 按程序计数
FIXTURE_COUNTS = {"CSH-02": 1, "DOC-04": 3, "DOC-06": 2, "GEN-09": 2, "REV-02": 2, "REV-03": 3, "RPT-03": 2}


@shared_loop
async def test_every_fixture_used_once_and_passes(demo: Started) -> None:
    files = assert_fixtures_used_once(trail_of(demo).entries, enabled_programs(demo.runner.deps))
    assert Counter(pid for pid, _ in files) == FIXTURE_COUNTS


def enabled_programs(deps: AgentDeps) -> set[str]:
    """本次启用的程序 (程序库的启用视图)."""
    return {t.id for t in deps.library.all()}


def assert_fixtures_used_once(
    entries: Sequence[TrailEntry], enabled: Iterable[str], root: Path | None = None
) -> list[tuple[str, str]]:
    """验收 5: fixture 根 (缺省为 LLM_FIXTURES_ROOT, 即 tests/fixtures/llm) 下, 启用程序的每个文件都在流程中被读到
    一次, 一次通过四级校验 (attempt 1, 无失败级别). 未启用程序的目录不比对 (后接入的程序可以先放 fixture).
    entries 为该项目的全部留痕; enabled 为本次启用的程序编号 (见 enabled_programs). 返回比对的 (程序, fixture_key)."""
    ids = set(enabled)
    root = root if root is not None else get_settings().LLM_FIXTURES_ROOT
    files = sorted((p.parent.name, p.stem) for p in root.glob("*/*.json") if p.parent.name in ids)
    calls = [e for e in entries if e.kind is TrailKind.llm_call]
    assert sorted((e.procedure_id, e.payload["fixture_key"]) for e in calls) == files
    assert all(e.payload["attempt"] == 1 and e.payload["level_failed"] is None for e in calls)
    return files


def test_fixture_guard_compares_enabled_programs_only(tmp_path: Path) -> None:
    """assert_fixtures_used_once 只比对启用程序的目录: 临时 fixture 根多放未启用程序的 FIN-03/default.json 仍过;
    启用的 REV-03 多一个文件 (CON-999.json) 时失败."""
    root = tmp_path / "llm"
    shutil.copytree(get_settings().LLM_FIXTURES_ROOT, root)
    enabled = enabled_programs(make_deps(FakeRegistry(), FakeTrail()))
    entries = [
        TrailEntry(
            engagement_id=EID,
            kind=TrailKind.llm_call,
            actor="A6",
            procedure_id=p.parent.name,
            summary="synthetic",
            payload={"fixture_key": p.stem, "attempt": 1, "level_failed": None},
        )
        for p in sorted(root.glob("*/*.json"))
        if p.parent.name in enabled  # 流程只调启用的程序; 真实根里先放的未启用程序 fixture 不会被读到
    ]
    assert Counter(pid for pid, _ in assert_fixtures_used_once(entries, enabled, root)) == FIXTURE_COUNTS
    (root / "FIN-03").mkdir(exist_ok=True)
    (root / "FIN-03" / "default.json").write_text("{}", encoding="utf-8")
    assert len(assert_fixtures_used_once(entries, enabled, root)) == 15
    (root / "REV-03" / "CON-999.json").write_text("{}", encoding="utf-8")
    with pytest.raises(AssertionError):
        assert_fixtures_used_once(entries, enabled, root)


@shared_loop
async def test_locked_after_signing(demo: Started) -> None:
    """验收 6: 签字后提交任一决定报 ENGAGEMENT_LOCKED."""
    with pytest.raises(DecisionRejected) as e:
        await demo.submit.submit(EID, RPT_APPROVE.model_copy(update={"id": "D-AFTER"}))
    assert e.value.code == "ENGAGEMENT_LOCKED"


@shared_loop
async def test_every_program_runs_with_a_real_agent(demo_run: Demo) -> None:
    """(Codex R55 探针) 流程中每条程序都由真实 Agent 执行 (没有脚本 Agent), 各一次 (RPT-03 两稿两次);
    大模型请求的输出模型与次数, 每次校验都无错误也无无支撑步骤."""
    assert [(pid, name) for pid, name, script in demo_run.agents if script] == []
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
    assert {pid: name for pid, name, _ in demo_run.agents} == agents
    assert Counter(pid for pid, _, _ in demo_run.agents) == {pid: 2 if pid == "RPT-03" else 1 for pid in agents}
    assert [v for v in demo_run.validations if v[1:] != (None, 0, 0)] == []
    assert Counter(pid for pid, *_ in demo_run.validations) == {
        "DOC-04": 3,
        "DOC-06": 2,
        "REV-03": 3,
        "REV-02": 2,
        "CSH-02": 1,
        "GEN-09": 2,
        "RPT-03": 2,
    }


# ── R56: 真实存储变体 ──


@dataclass
class RealRun:
    """真实存储下新建的一个项目: 流程入口 s, 全部真实依赖 deps, 项目编号 eid, 项目负责人 owner."""

    s: Started
    deps: AgentDeps
    eid: str
    owner: str

    def data_service(self) -> DataService:
        """数据管理服务 (同组装根: 项目锁取 s.runner, 演示数据导入的清单来源为 deps.datasets). 每次新建, 调用方留
        一个用."""
        return DataService(self.deps, self.s.runner, demo=cast(RoutingDatasetLoader, self.deps.datasets))


@asynccontextmanager
async def real_storage(
    tmp_path: Path,
    *,
    load_dataset: bool = True,
    dataset: str = "demo",
    period: tuple[date, date] = (date(2025, 7, 1), date(2026, 6, 30)),
    fresh_profile: bool = True,
    **overrides: Any,
) -> AsyncIterator[RealRun]:
    """组装真实依赖 (Postgres 各存储与检查点, 规则 SQL, 数据集载入 data/ 下的 dataset, 证据文件库 tmp_path, mock 函证与
    登记查询按数据集读目录), 新建用户 Jordan Ellis (报告签字块取项目负责人姓名, 第 7 节第 17 行) 与项目 (期间 period,
    缺省为 Numbat 的), 退出时停后台任务并释放连接. load_dataset 为假时不载入数据集 (财务数据由测试自己接入, 如 Xero
    的演示数据导入). fresh_profile 为真时画像换随机 ID (同库反复建不撞已有的), 为假时取数据集画像原样 (如 Xero 的
    CLIENT-XERO-3C1332BB-Q1, 需求 R120; 内容相同的已有画像沿用). overrides 覆盖配置 (缺省大模型为 mock).
    未设 TEST_DATABASE_URL 时跳过; 测试库不清理, 每次新建用户与项目."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("需设 TEST_DATABASE_URL: 已迁移并建好检查点表的独立测试库")
    config: dict[str, Any] = {
        "DATABASE_URL": url,
        "CHECKPOINT_BACKEND": "postgres",
        "DATASET_ROOT": REPO / "data",
        "EVIDENCE_ROOT": tmp_path,
        "LLM_MODE": "mock",
        "FEATURES": ",".join(sorted(NUMBAT_FEATURES)),  # Numbat 回归: 只开基线功能, 不受 .env 影响
    }
    settings = Settings(**(config | overrides))
    owner = "USR-" + uuid4().hex
    engine = create_async_engine(settings.sqlalchemy_url)
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=owner, email=f"{owner}@example.com", full_name="Jordan Ellis", password_hash="-", created_at=now()
            )
        )
    await engine.dispose()
    deps = build_deps(settings)
    profile = deps.datasets.client_profile(dataset)
    if fresh_profile:
        profile = profile.model_copy(update={"id": "CLIENT-" + uuid4().hex})
    engagement = await deps.engagement_store.create(
        owner_id=owner, period_start=period[0], period_end=period[1], dataset=dataset, profile=profile
    )
    # 建项目不载入财务数据 (需求 R10): 同种子脚本的 load 载入数据集, 视同已确认数据已齐, P1 不停下等数据
    if load_dataset:
        await deps.datasets.load(engagement.id, dataset)
    async with AsyncExitStack() as stack:
        saver = await stack.enter_async_context(open_checkpointer(settings))
        runner = GraphRunner(compile_graph(GraphNodes(deps, settings.PARALLEL_CAP), saver), deps, llm_version(settings))
        s = Started(runner, deps.output_store, deps.engagement_store)  # type: ignore[arg-type]
        try:
            yield RealRun(s, deps, engagement.id, owner)
        finally:
            await runner.shutdown()
            await deps.financial_store.engine.dispose()  # type: ignore[attr-defined]


@pytest.mark.slow
async def test_real_storage_variant(tmp_path: Path) -> None:
    """R56: 同 R55 的决定, 换用真实 Postgres 存储与检查点, 规则 SQL, 数据集载入 (data/demo), 证据文件与 mock 函证平台,
    大模型仍为 mock (tests/fixtures/llm, 文档 ID 已对齐 manifest): 结果, 留痕与证据指针同 R55; 文件 ID 与 document 指针
    都是 manifest 的文档 ID; 签字后 AuditOutputStore.put 抛 EngagementLocked."""
    async with real_storage(tmp_path) as run:
        s, deps, eid, owner = run.s, run.deps, run.eid, run.owner
        await s.runner.start(eid)
        await wait_idle(s.runner, eid)
        for decision in [
            acc01_decision(GateVerdict.approve, id="D-ACC"),
            pln_decision(GateVerdict.approve),
            *REVIEWS,
            FIN_APPROVE,
            RPT_REJECT,
            RPT_APPROVE,
        ]:
            actor = "reviewed_by" if isinstance(decision, ReviewRecord) else "decided_by"
            await s.submit.submit(eid, decision.model_copy(update={actor: owner}))
            await wait_idle(s.runner, eid)
            assert s.runner.run_error(eid) is None, (decision.id, s.runner.run_error(eid))

        await assert_results(s, eid, approver=owner)
        await assert_all_rules_ran(s, eid)
        entries = await deps.trail.list(eid, limit=10_000)
        assert_trail_counts(entries)
        files = assert_fixtures_used_once(entries, enabled_programs(deps))
        assert Counter(pid for pid, _ in files) == FIXTURE_COUNTS
        pointers = await assert_pointers_resolve(s, eid, everything=True)
        assert {"trail", "bank_account"} <= {p.source_type for p in pointers}  # 评分卡的登记留痕, CSH-05 例外的账户
        manifest = json.loads((REPO / "data" / "demo" / "manifest.json").read_text(encoding="utf-8"))
        document_ids = {d["document_id"] for d in manifest["documents"]}
        assert {d.id for d in await deps.financial_store.documents(eid)} == document_ids
        assert {p.source_id for p in pointers if p.source_type == "document"} == document_ids

        judgment = (await deps.output_store.latest(eid, Judgment))[0]
        with pytest.raises(EngagementLocked):
            newer = judgment.model_copy(update={"version": judgment.version + 1})
            await deps.output_store.put(eid, [newer], actor=owner)
