"""需求 R119 (设计稿 v2.1 第 7.3 节末段 "只开现有 17 条时"): 新功能全关, 现有 17 条程序在 Xero 演示数据上离线跑完
P0-P4. 第 1 级前的回归里程碑, 长期保留 (同 Numbat 回归 R55, tests/test_e2e_mock).

流程只经 GraphRunner, GraphDecisionSubmit 与 DataService 的接口, 模块里只跑一遍: 空项目
(tests/xero_fakes.new_xero_deps) 启动 → ACC-01 闸门处演示数据导入 acceptance → ACC-01 approve → 停在 p1_await_data
时导入 fieldwork → 项目负责人确认数据已齐 (tests/xero_flow) → 收尾 (tests/xero_fakes.finish_xero17: PLN-01/03, 10 条
复核, FIN-01 无保留意见, RPT-01 先退回再批准). 主模型与复核模型 (上传处理也用它) 为 LayeredMockLLMClient(roots=
[llm_xero_only17, 运行副本]), 规则引擎为 FakeRuleEngine(XERO_RULES). 断言函数以 Xero17 为参数, 连库变体 (R120)
共用. 读快照与补造目录, 标 external.
"""

import json
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio

from backend.adapters.llm import SwitchableLLMClient
from backend.agents.ingest import DETAILS
from backend.contracts.data import XERO_TABLES, DataInput
from backend.contracts.errors import DecisionRejected, EngagementLocked
from backend.contracts.models import (
    AuditException,
    AuditReportVersion,
    Confirmation,
    ConfirmationResult,
    DocumentType,
    EvidencePointer,
    ExceptionDisposition,
    GateDecision,
    GateId,
    Judgment,
    MaterialityCandidates,
    Misstatement,
    OpinionType,
    Phase,
    Procedure,
    ProcedureRun,
    ProposedAction,
    ReportSection,
    ReportStatus,
    ReviewRecord,
    ReviewStatus,
    ReviewTask,
    RunPopulation,
    TaskStatus,
    TrailEntry,
    TrailKind,
    UncorrectedSchedule,
)
from backend.contracts.views import GateView, PlanningMaterial
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.orchestration.scheduler import build_schedule
from backend.services.data_service import DataService
from backend.services.features import BASELINE
from backend.services.populations import distinct_items, judged_subjects, total_cents
from backend.services.procedure_library import render_text
from tests.external import snapshot_dir, supplements_dir
from tests.fakes import ENGAGEMENT, FakeRuleEngine, FakeTrail, no_population, wait_idle
from tests.fixture_sets import XERO
from tests.test_decisions import Started
from tests.test_e2e_mock import Demo, assert_pointers_resolve, real_storage, trail_of
from tests.xero_fakes import (
    XERO17_CONTRACTS,
    XERO17_FEATURES,
    XERO17_INVOICES,
    XERO_A01_CHANGES,
    XERO_DATASET,
    XERO_JOURNAL_VOUCHERS,
    XERO_JOURNALS_FEATURES,
    XERO_PERIOD,
    XERO_RULES,
    finish_xero17,
    new_xero_deps,
    xero17_gates,
    xero17_reviews,
    xero_csh05_population,
    xero_journal_reviews,
    xero_pln07_a01_population,
    xero_rev08_populations,
    xero_uploads,
)
from tests.xero_flow import (
    approve_acceptance,
    confirm_ready,
    demo_source,
    import_stage,
    offline_runner,
    recorded_agents,
)
from tests.xero_harness import LayeredMockLLMClient
from tests.xero_injection import changed, originals

pytestmark = pytest.mark.external
shared_loop = pytest.mark.asyncio(loop_scope="module")  # 用模块共用的流程 (xero17) 的用例与它同一个事件循环
EID = ENGAGEMENT.id
OWNER = ENGAGEMENT.owner_id  # 项目负责人 (假用户存储里为 Jordan Ellis)
ONLY17 = XERO.fixtures.with_name("llm_xero_only17")  # 本路径的 RPT-03 两稿 (需求 R117), 排在运行副本之前
ORC1026 = "SI-77b338ef-ecc0-4b95-a0d7-2617b0054611"
# 每条程序由哪个真实 Agent 执行 (同 R55 探针的 13 条程序表)
AGENTS = {
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


@dataclass
class Xero17:
    """跑完的一次流程: 主图 runner, 项目 eid, 项目负责人 owner, 大模型客户端 (记了读取与请求), 停在 p1_await_data 时
    总览的 awaiting_data, PLN-01/03 闸门的材料, 每次执行的 (程序, Agent 类名, 是否脚本 Agent), 原目录被改动的文件."""

    runner: GraphRunner
    eid: str
    owner: str
    client: LayeredMockLLMClient
    awaiting_data: bool
    pln: GateView
    agents: list[tuple[str, str, bool]]
    moved: list[str]


def xero17_client() -> LayeredMockLLMClient:
    """本路径的大模型客户端: llm_xero_only17 (RPT-03 两稿) 排在运行副本之前; 运行副本经 tests.external 定位核对."""
    XERO.ready()
    return LayeredMockLLMClient([ONLY17, XERO.runtime])


async def run_xero17(
    features: frozenset[str] = XERO17_FEATURES,
    before_review: Callable[[GraphRunner, str, ReviewRecord], Awaitable[None]] | None = None,
    reviews: Sequence[ReviewRecord] | None = None,
    stop_at_fin: bool = False,
) -> Xero17:
    """离线: 空的假存储, 规则引擎 FakeRuleEngine(XERO_RULES), 主模型与复核模型为 xero17_client(); 跑完整个流程.
    features 为功能集 (缺省新功能全关), before_review 见 finish_xero17 (需求 R155 打开 collab 的变体用); reviews 与
    stop_at_fin 见 finish_xero17 (需求 R146 打开 journals 的变体用)."""
    client = xero17_client()
    deps = new_xero_deps(
        trail=FakeTrail(),
        llm=client,
        review_llm=client,
        rules=FakeRuleEngine(dict(XERO_RULES)),
        features=features,
    )
    runner = offline_runner(deps)
    service = DataService(deps, runner, demo=demo_source())
    return await drive_xero17(runner, service, EID, OWNER, client, before_review, reviews, stop_at_fin)


async def drive_xero17(
    runner: GraphRunner,
    service: DataService,
    eid: str,
    owner: str,
    client: LayeredMockLLMClient,
    before_review: Callable[[GraphRunner, str, ReviewRecord], Awaitable[None]] | None = None,
    reviews: Sequence[ReviewRecord] | None = None,
    stop_at_fin: bool = False,
) -> Xero17:
    """从新建的项目跑完整个流程 (见模块说明; 假存储与真实库都用这一个), 记下每次执行的 Agent; 前后各记一次原目录的
    逐文件哈希. client 须已是 runner.deps 的主模型与复核模型; before_review, reviews 与 stop_at_fin 见 finish_xero17."""
    before = originals()
    deps = runner.deps
    with recorded_agents() as agents:
        await runner.start(eid)
        await wait_idle(runner, eid)
        assert (await runner.app.aget_state(GraphRunner.config(eid))).next == ("gate_acc01",), runner.run_error(eid)
        await import_stage(service, eid, "acceptance", owner)
        await approve_acceptance(runner, eid, owner)
        assert (await runner.app.aget_state(GraphRunner.config(eid))).next == ("p1_await_data",)
        query = GraphStateQuery(runner.app, deps, runner)
        awaiting = (await query.overview(eid)).awaiting_data
        await import_stage(service, eid, "fieldwork", owner)
        await confirm_ready(service, eid, owner)
        pln = await query.gate(eid)
        assert pln is not None, runner.run_error(eid)
        await finish_xero17(runner, eid, owner, before_review, reviews, stop_at_fin)
    return Xero17(runner, eid, owner, client, awaiting, pln, agents, changed(before, originals()))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def xero17() -> Xero17:
    return await run_xero17()


async def entries_of(x: Xero17) -> list[TrailEntry]:
    return await x.runner.deps.trail.list(x.eid, limit=10_000)


# ── 验收 1: 开关, 步骤与决定 ──


@shared_loop
async def test_switches_and_steps(xero17: Xero17) -> None:
    await assert_steps(xero17)


async def assert_steps(x: Xero17) -> None:
    """新功能全关 (显式传入基线功能集, 项目冻结同一份); 停在 p1_await_data 时总览 awaiting_data 为真; 由项目负责人确认
    数据已齐; 闸门 5 次与复核 10 次的决定都已写入 (decision 留痕的键即决定 ID, 按提交顺序), RPT-01 的退回批注挂在
    基础段 (需求 R119 来由); 流程结束且没有后台错误 (每次复核前停在 await_events 由 finish_xero17 断言)."""
    deps = x.runner.deps
    assert XERO17_FEATURES == deps.features == BASELINE == {"critic"}  # 基线只有 A7 独立复核
    engagement = await deps.engagement_store.get(x.eid)
    assert engagement is not None and engagement.version_snapshot is not None
    assert engagement.version_snapshot["features"] == "critic"
    assert x.awaiting_data
    readiness = await deps.data.readiness(x.eid)
    assert readiness.ready_at is not None and readiness.ready_by == x.owner
    gates = xero17_gates(x.owner)
    decided = [e.idempotency_key for e in await entries_of(x) if e.kind is TrailKind.decision]
    assert decided == [
        "D-ACC-01",
        gates["pln"].id,
        *(r.id for r in xero17_reviews(x.owner)),
        gates["fin"].id,
        gates["rpt_reject"].id,
        gates["rpt_approve"].id,
    ]
    reject = await deps.output_store.get(x.eid, GateDecision, gates["rpt_reject"].id)
    assert reject is not None and [(a.section, a.comment) for a in reject.annotations] == [
        (
            ReportSection.basis_for_opinion,
            "Name the financial reporting framework (Australian Accounting Standards - Simplified Disclosures) "
            "in this paragraph.",
        )
    ]
    assert (await x.runner.app.aget_state(GraphRunner.config(x.eid))).next == ()
    assert x.runner.run_error(x.eid) is None


# ── 验收 2, 3: 结果, 重要性与汇总表 ──


@shared_loop
async def test_results(xero17: Xero17) -> None:
    await assert_results(xero17)


# 打开 journals 时多出的 GEN-05 例外 (需求 R146 验收 1, 3): 例外 ID → (所指分录行, 金额, 归因于的判断)
JOURNAL_EXCEPTIONS = {
    "X-GEN-05-1-R09-1": ("XJ-397-3", 1921221, "J-GEN-02-1-XJ-397"),
    "X-GEN-05-1-R09-2": ("XJ-499-2", 82500, "J-GEN-02-1-XJ-499"),
    "X-GEN-05-1-R10-1": ("XJ-430-2", 55000, "J-GEN-02-1-XJ-430"),
    "X-GEN-05-1-R10-2": ("XJ-449-2", 55000, "J-GEN-02-1-XJ-449"),
    "X-GEN-05-1-R11-1": ("XJ-575-1", 29695, "J-GEN-02-1-XJ-575-H1"),  # 推翻后由人工判断归因
    "X-GEN-05-1-R11-2": ("XJ-601-1", 350000, "J-GEN-02-1-XJ-601"),
    "X-GEN-05-1-R12-1": ("XJ-405-2", 55000, "J-GEN-02-1-XJ-405"),
}
# 多出的判断 → (版本, 复核状态, 建议动作): 7 条 AI 判断与推翻 XJ-575 建的人工判断 -H1
JOURNAL_JUDGMENTS = {
    **{f"J-GEN-02-1-{v}": (2, ReviewStatus.confirmed, ProposedAction.accept) for v in XERO_JOURNAL_VOUCHERS},
    "J-GEN-02-1-XJ-499": (2, ReviewStatus.confirmed, ProposedAction.misstatement),
    "J-GEN-02-1-XJ-575": (2, ReviewStatus.overridden, ProposedAction.more_evidence),
    "J-GEN-02-1-XJ-575-H1": (1, ReviewStatus.confirmed, ProposedAction.accept),
}
# 多出的错报 (来源判断, 类型, 科目, 认定, 金额, 方向, 状态): 错报闭环未接入, 按未更正累计
JOURNAL_MISSTATEMENT = (
    "J-GEN-02-1-XJ-499",
    "factual",
    "Office equipment (710)",
    "Existence",
    75000,
    "overstated",
    "uncorrected",
)


async def assert_results(x: Xero17, journals: bool = False) -> None:
    """例外 11 条 (PLN-07 A01 九条 open, 科目与变动额同 R112; R03-1 与 D04-1 attributed); 判断 10 条最新为 v2
    confirmed (REV-03 三条与 REV-02 六条 accept, BA-091 misstatement); 任务 10 条 decided; 两户函证 agreed; 错报
    1 条. journals 为真时 (需求 R146 验收 3) 只多本组增量: 例外多 GEN-05 的 7 条 (都 attributed, R11-1 归因于人工判断),
    判断多 8 条 (JOURNAL_JUDGMENTS), 任务多 GEN-02 的 7 条 decided, 错报多 XJ-499 的 1 条; 其余不变."""
    store = x.runner.deps.output_store
    exceptions = {e.id: e for e in await store.latest(x.eid, AuditException)}
    a01 = {
        f"X-PLN-07-1-A01-{n}": ([code], int(current) - int(opening), ExceptionDisposition.open)
        for n, (code, _, current, _, opening, _) in enumerate(XERO_A01_CHANGES, 1)
    }
    gen05 = {
        i: ([line], amount, ExceptionDisposition.attributed) for i, (line, amount, _) in JOURNAL_EXCEPTIONS.items()
    }
    assert {i: (e.source_ids, e.amount_cents, e.disposition) for i, e in exceptions.items()} == a01 | {
        "X-REV-08-1-R03-1": ([ORC1026], 330000, ExceptionDisposition.attributed),
        "X-CSH-02-1-D04-1": (["BA-091"], 4853, ExceptionDisposition.attributed),
    } | (gen05 if journals else {})
    judgments = {j.id: j for j in await store.latest(x.eid, Judgment)}
    expected = {
        **{f"J-REV-03-1-{c}": ProposedAction.accept for c in XERO17_CONTRACTS},
        **{f"J-REV-02-1-{i}": ProposedAction.accept for i in XERO17_INVOICES},
        "J-CSH-02-1-BA-091": ProposedAction.misstatement,
    }
    assert {i: (j.version, j.review_status, j.proposed_action) for i, j in judgments.items()} == {
        i: (2, ReviewStatus.confirmed, action) for i, action in expected.items()
    } | (JOURNAL_JUDGMENTS if journals else {})
    assert exceptions["X-REV-08-1-R03-1"].attributed_by == f"J-REV-02-1-{ORC1026}"
    assert exceptions["X-CSH-02-1-D04-1"].attributed_by == "J-CSH-02-1-BA-091"
    if journals:
        assert {i: exceptions[i].attributed_by for i in JOURNAL_EXCEPTIONS} == {
            i: judgment for i, (_, _, judgment) in JOURNAL_EXCEPTIONS.items()
        }
        assert judgments["J-GEN-02-1-XJ-575"].superseded_by == "J-GEN-02-1-XJ-575-H1"
    tasks = await store.latest(x.eid, ReviewTask)
    reviewed = [*expected, *(f"J-GEN-02-1-{v}" for v in XERO_JOURNAL_VOUCHERS if journals)]
    assert sorted(t.target_id for t in tasks) == sorted(reviewed)
    assert {t.status for t in tasks} == {TaskStatus.decided}
    confirmations = {c.id: c for c in await store.latest(x.eid, Confirmation)}
    assert {
        cid: (c.result, c.confirmed_balance_cents, c.book_balance_cents, c.difference_cents, c.accrued_interest_cents)
        for cid, c in confirmations.items()
    } == {
        "C-BA-090": (ConfirmationResult.agreed, -1090044, -1090044, 0, 0),
        "C-BA-091": (ConfirmationResult.agreed, 1158123, 1158123, 0, 4853),
    }
    misstatements = await store.latest(x.eid, Misstatement)
    assert sorted(
        (m.judgment_id, m.type, m.account, m.assertion, m.amount_cents, m.direction, m.status) for m in misstatements
    ) == [
        ("J-CSH-02-1-BA-091", "factual", "Interest income (270)", "Completeness", 4853, "understated", "uncorrected"),
        *([JOURNAL_MISSTATEMENT] if journals else []),
    ]


@shared_loop
async def test_materiality_and_schedule(xero17: Xero17) -> None:
    await assert_materiality(xero17)


async def assert_materiality(x: Xero17, journals: bool = False) -> None:
    """重要性 32431 / 16216 / 1622; 候选的三个基准 (收入, 总资产, 税前亏损); PLN-01/03 闸门材料没有未覆盖的目标;
    汇总表只有 BA-091 的错报 (4853), 未超过重要性, 候选无保留意见. journals 为真时 (需求 R146 验收 4) 汇总表多 XJ-499
    的错报 (75000): 错报闭环未接入, 按未更正累计, 合计 79853 超过重要性, 候选为保留意见."""
    store = x.runner.deps.output_store
    values = (await x.runner.app.aget_state(GraphRunner.config(x.eid))).values
    assert (values["materiality_cents"], values["performance_materiality_cents"], values["clearly_trivial_cents"]) == (
        32431,
        16216,
        1622,
    )
    candidates = await store.get(x.eid, MaterialityCandidates, "MATERIALITY")
    assert candidates is not None
    assert {c.basis: c.base_amount_cents for c in candidates.candidates} == {
        "revenue": 3243100,
        "total_assets": 1080529,
        "profit_before_tax": -1322696,
    }
    assert isinstance(x.pln.material, PlanningMaterial) and x.pln.material.uncovered_objective_ids == []
    schedule = await store.get(x.eid, UncorrectedSchedule, "SCHEDULE")
    misstatements = {m.judgment_id: m.id for m in await store.latest(x.eid, Misstatement)}
    assert schedule is not None
    expected = [(misstatements["J-CSH-02-1-BA-091"], 4853)]
    if journals:
        expected.append((misstatements["J-GEN-02-1-XJ-499"], 75000))
    assert sorted((i.misstatement_id, i.amount_cents) for i in schedule.items) == sorted(expected)
    assert (schedule.total_cents, schedule.materiality_cents, schedule.candidate_opinion) == (
        (79853, 32431, OpinionType.qualified) if journals else (4853, 32431, OpinionType.unqualified)
    )


# ── 验收 4: 报告, 归档与锁定 ──


@shared_loop
async def test_report_and_lock(xero17: Xero17) -> None:
    await assert_report(xero17)
    with pytest.raises(DecisionRejected) as e:  # 签字后提交任一决定
        rejected: GateDecision = xero17_gates(xero17.owner)["rpt_approve"].model_copy(update={"id": "D-AFTER"})
        await GraphDecisionSubmit(xero17.runner.app, xero17.runner.deps, xero17.runner).submit(xero17.eid, rejected)
    assert e.value.code == "ENGAGEMENT_LOCKED"


async def assert_report(x: Xero17, reject_id: str | None = None) -> None:
    """REPORT-1 v1 draft; REPORT-2 v2 signed (负责人签字, 回应退回决定): 意见段以 "Opinion" 开头, 没有持续经营段,
    基础段写明 Simplified Disclosures, 签名块以 "Jordan Ellis\\n" 开头, 不引判断; 项目归档并锁定. reject_id 为
    RPT-01 退回决定的 ID, 缺省为本路径决定表的 (经接口提交时由接口生成, 需求 R287 传入)."""
    reports = {r.id: r for r in await x.runner.deps.output_store.latest(x.eid, AuditReportVersion)}
    assert (reports["REPORT-1"].version, reports["REPORT-1"].status) == (1, ReportStatus.draft)
    signed = reports["REPORT-2"]
    assert (signed.version, signed.status, signed.approved_by, signed.addresses_decision_id) == (
        2,
        ReportStatus.signed,
        x.owner,
        reject_id or xero17_gates(x.owner)["rpt_reject"].id,
    )
    assert (signed.opinion_type, signed.judgment_refs) == (OpinionType.unqualified, [])
    sections = signed.sections
    assert sections[ReportSection.opinion].startswith("Opinion\n")
    assert "going_concern" not in sections
    assert "Simplified Disclosures" in sections[ReportSection.basis_for_opinion]
    assert "Simplified Disclosures" not in reports["REPORT-1"].sections[ReportSection.basis_for_opinion]
    assert sections[ReportSection.signature].startswith("Jordan Ellis\n")
    engagement = await x.runner.deps.engagement_store.get(x.eid)
    assert engagement is not None and (engagement.phase, engagement.locked) == (Phase.Archived, True)


# ── 验收 5: 留痕 ──


@shared_loop
async def test_trail(xero17: Xero17, demo_run: Demo) -> None:
    """状态迁移与 Numbat 回归 R55 的同一套比 (会话共用的那一次, conftest 的 demo_run)."""
    assert_trail(await entries_of(xero17), trail_of(demo_run.s).entries)


def transitions(entries: list[TrailEntry]) -> Counter[tuple[str, str, str]]:
    """transition 留痕的 (对象, 原状态, 新状态) 多重集合: 程序按编号, 项目阶段记为 engagement (项目编号随存储而不同)."""
    return Counter(
        (
            e.payload["id"] if e.payload["object"] == "procedure" else e.payload["object"],
            e.payload["from"],
            e.payload["to"],
        )
        for e in entries
        if e.kind is TrailKind.transition
    )


def assert_trail(entries: list[TrailEntry], numbat: list[TrailEntry]) -> None:
    """llm_call 38 行: 编排内 14 行 (DOC-06 2, REV-03 3, REV-02 6, CSH-02 1, RPT-03 2; DOC-04 没有, 合同已在上传时
    抽取), 上传处理 24 行 (执行 ID 以 UPLOAD- 开头: DOC-CLASSIFY 21, DOC-04 3); GEN-09 没有 (BA-091 的建议不触发主规则,
    抽样未中, REV-03 与 REV-02 为 L2 跳过); decision 15 行; external_call 4 行 (ACC-04 2, CSH-04 2); data 留痕含
    upload 21, classify 21, sync 7, import 4, edit 21, ready 1; transition 的多重集合与 Numbat R55 相同."""
    calls = [e for e in entries if e.kind is TrailKind.llm_call]
    assert Counter((e.procedure_id, (e.run_id or "").startswith("UPLOAD-")) for e in calls) == {
        ("DOC-06", False): 2,
        ("REV-03", False): 3,
        ("REV-02", False): 6,
        ("CSH-02", False): 1,
        ("RPT-03", False): 2,
        ("DOC-CLASSIFY", True): 21,
        ("DOC-04", True): 3,
    }
    assert len([e for e in entries if e.kind is TrailKind.decision]) == 15
    assert Counter(e.procedure_id for e in entries if e.kind is TrailKind.external_call) == {"ACC-04": 2, "CSH-04": 2}
    actions = Counter(e.payload.get("action") for e in entries if e.kind is TrailKind.data)
    assert {a: actions[a] for a in ("upload", "classify", "sync", "import", "edit", "ready")} == {
        "upload": 21,
        "classify": 21,
        "sync": 7,
        "import": 4,
        "edit": 21,
        "ready": 1,
    }
    assert transitions(entries) == transitions(numbat)


# ── 验收 6: 证据指针, 固定输出的读取与真实 Agent ──


@shared_loop
async def test_pointers_fixtures_and_agents(xero17: Xero17) -> None:
    await assert_pointers_and_reads(xero17)
    assert [(pid, name) for pid, name, script in xero17.agents if script] == []
    assert {pid: name for pid, name, _ in xero17.agents} == AGENTS
    assert Counter(pid for pid, _, _ in xero17.agents) == {pid: 2 if pid == "RPT-03" else 1 for pid in AGENTS}


async def assert_pointers_and_reads(x: Xero17, *, everything: bool = False) -> list[EvidencePointer]:
    """判断, 合同与函证的证据指针都能解析 (everything 时另含例外, 承接评分卡与人工结果; 返回检查过的指针); 大模型
    读取: RPT-03 两次读自 llm_xero_only17 (r1, r2), 其余 36 次读自运行副本 (DOC-CLASSIFY 21, DOC-04 3, DOC-06 2,
    REV-03 3, REV-02 6, CSH-02 1), 每个文件一次, 都是 attempt 1 且没有失败级别."""
    deps = x.runner.deps
    started = Started(x.runner, deps.output_store, deps.engagement_store)  # type: ignore[arg-type]
    pointers = await assert_pointers_resolve(started, x.eid, everything=everything)
    reads = [(r.procedure_id, r.fixture_key, r.root, r.file) for r in x.client.reads]
    assert sorted(r for r in reads if r[0] == "RPT-03") == [
        ("RPT-03", "r1", ONLY17, "RPT-03/r1.json"),
        ("RPT-03", "r2", ONLY17, "RPT-03/r2.json"),
    ]
    rest = [r for r in reads if r[0] != "RPT-03"]
    assert {(root, file == f"{pid}/{key}.json") for pid, key, root, file in rest} == {(XERO.runtime, True)}
    assert Counter(pid for pid, *_ in rest) == {
        "DOC-CLASSIFY": 21,
        "DOC-04": 3,
        "DOC-06": 2,
        "REV-03": 3,
        "REV-02": 6,
        "CSH-02": 1,
    }
    assert len(set(reads)) == len(reads) == 38
    calls = [e for e in await entries_of(x) if e.kind is TrailKind.llm_call]
    assert {(e.payload["attempt"], e.payload["level_failed"]) for e in calls} == {(1, None)}
    return pointers


# ── 验收 7: 文件, 数据版本与补交请求 ──


@shared_loop
async def test_documents_versions_and_requests(xero17: Xero17) -> None:
    await assert_documents(xero17)


async def assert_documents(x: Xero17) -> None:
    """文件 23 份: 审计师上传 21 份 (负责人上传, 类型, 可靠性, 文件日期与关联对象同补造清单; 合同 3 份 extracted, 其余
    18 份 classified), 银行直达回函 2 份 (external_original, extracted). 数据版本 10 (方案取舍见确认材料 R119): 承接
    文件 1, 快照七张 2, 补造四张 3 至 6, 上传后抽取的三份合同 7 至 9, 补登记 10. 补交请求中审批日志仍 open, 其余
    received."""
    deps = x.runner.deps
    documents = {d.id: d for d in await deps.financial_store.documents(x.eid)}
    planned = {u.document_id: u for u in xero_uploads() if u.stage in ("acceptance", "fieldwork")}
    uploaded = {i: d for i, d in documents.items() if d.channel == "auditor_upload"}
    assert sorted(uploaded) == sorted(planned)
    for i, d in uploaded.items():
        u = planned[i]
        assert (d.type, d.reliability, d.document_date, d.related_ids, d.uploaded_by) == (
            u.document_type,
            u.reliability,
            u.document_date,
            u.related_ids,
            x.owner,
        ), d.file_name
        assert d.status == ("extracted" if d.type is DocumentType.contract else "classified"), d.file_name
    assert Counter(d.type.value for d in uploaded.values()) == {
        "contract": 3,
        "minutes": 1,
        "engagement_letter": 1,
        "agreement": 5,
        "correspondence": 4,
        "internal_record": 6,
        "invoice": 1,
    }
    replies = [d for d in documents.values() if d.channel != "auditor_upload"]
    assert (
        sorted((d.channel, d.type, d.reliability, d.status) for d in replies)
        == [("bank_direct", DocumentType.confirmation, "external_original", "extracted")] * 2
    )
    changes = await deps.data.changes(x.eid)
    by_version: dict[int, set[DataInput]] = {}
    for change in changes:
        by_version.setdefault(change.data_version, set()).add(change.input)
    snapshot = {"accounts", "trial_balance", "journal_entries", "sales_invoices", "supplier_bills"}
    assert {v: sorted(i.value for i in inputs) for v, inputs in by_version.items()} == {
        1: ["documents"],
        2: sorted(snapshot | {"bank_accounts", "bank_transactions"}),
        3: ["sales_invoices"],
        4: ["bank_accounts"],
        5: ["delivery_notes"],
        6: ["employees"],
        7: ["contracts"],
        8: ["contracts"],
        9: ["contracts"],
        10: ["documents"],
    }
    assert (await deps.data.readiness(x.eid)).data_version == 10
    assert {r.id: r.status for r in await deps.data.requests(x.eid)} == {
        "REQ-accounts": "received",
        "REQ-trial_balance": "received",
        "REQ-journal_entries": "received",
        "REQ-contract": "received",
        "REQ-approval_log": "open",
    }


# ── 需求 R324: 被测总体 (验收 1, 3, 4) ──

ORC1026_FIELDS = {  # ORC1026 的识别字段: 发票号, 客户, 合同, 入账日, 分录 (金额在项目的 amount_cents)
    "invoice_no": "ORC1026",
    "customer": "DIISR - Small Business Services",
    "contract_id": "CON-X01",
    "posting_date": "2026-09-17",
    "journal_entry_id": "XJ-580-2",
}


@shared_loop
async def test_populations(xero17: Xero17) -> None:
    """需求 R324 验收 1: POP-RUN-REV-08-1 只有第 1 版, R01, R02 为窗口内六张 (990000), R03 为找得到收款的五张
    (935000, 没有 ORC1024), 去重后六张 990000; ORC1026 在 R01 带服务完成记录, 签收日与签收人, 在 R03 带收款流水,
    收款日与收款额; 例外仍只有 X-REV-08-1-R03-1. 验收 3: 只有执行过的三条规则程序 (PLN-07, REV-08, CSH-05) 写了总体,
    判断程序 (REV-02, CSH-02) 与闸门程序 PLN-01 没有; 流程结束时检查点状态里没有总体. 验收 4: REV-03 的对象清单为三份
    合同."""
    store = xero17.runner.deps.output_store
    populations = {p.id: p for p in await store.latest(xero17.eid, RunPopulation)}
    assert sorted(populations) == ["POP-RUN-CSH-05-1", "POP-RUN-PLN-07-1", "POP-RUN-REV-08-1"]
    rev08 = populations["POP-RUN-REV-08-1"]
    assert await store.history(xero17.eid, RunPopulation, rev08.id) == [rev08]
    assert (rev08.run_id, rev08.procedure_id, rev08.rules) == ("RUN-REV-08-1", "REV-08", xero_rev08_populations())
    assert [(r.rule_id, r.source_type, r.count, r.amount_cents) for r in rev08.rules] == [
        ("R01", "sales_invoice", 6, 990000),
        ("R02", "sales_invoice", 6, 990000),
        ("R03", "sales_invoice", 5, 935000),
    ]
    window = ["ORC1020", "ORC1021", "ORC1022", "ORC1023", "ORC1024", "ORC1026"]
    numbers = [sorted(i.fields["invoice_no"] for i in r.items) for r in rev08.rules]
    assert numbers == [window, window, [n for n in window if n != "ORC1024"]]
    items = distinct_items(rev08)
    assert items is not None and (len(items), total_cents(i.amount_cents for i in items)) == (6, 990000)
    r01, _, r03 = ({i.source_id: i for i in r.items}[ORC1026] for r in rev08.rules)
    assert (r01.amount_cents, r01.fields) == (
        715000,
        ORC1026_FIELDS
        | {"records": [{"delivery_no": "SR-ORC1026", "signed_date": "2026-09-17", "signed_by": "Priya Natarajan"}]},
    )
    receipt = {"bank_transaction_ids": "BT-090-0124", "received_dates": "2026-10-14", "received_cents": 385000}
    assert (r03.amount_cents, r03.fields) == (715000, ORC1026_FIELDS | receipt)
    exceptions = await store.latest(xero17.eid, AuditException, run_id="RUN-REV-08-1")
    assert [(x.id, x.amount_cents) for x in exceptions] == [("X-REV-08-1-R03-1", 330000)]
    values = (await xero17.runner.app.aget_state(GraphRunner.config(xero17.eid))).values
    assert values["staged_results"] == [] and no_population(values)
    judgments = await store.latest(xero17.eid, Judgment, procedure_id="REV-03")
    tasks = await store.latest(xero17.eid, ReviewTask, procedure_id="REV-03")
    assert judged_subjects("RUN-REV-03-1", judgments, tasks) == XERO17_CONTRACTS


@shared_loop
async def test_existing_procedure_populations_and_scopes(xero17: Xero17) -> None:
    """需求 R327 验收 1-3: CSH-05 B01 的总体为 090, 091 两个银行账户 (金额为总账余额, 合计 68079; 两户都没有例外);
    PLN-07 A01 为参与期初比较的 12 个资产负债类科目 (本期三个月, 与上期不等长, 不比损益类), 没有金额合计, 9 条变动
    例外都在其中, A02, A03 以返回行为总体 (两期, 三个月). 每次执行的 scope 按其参数快照都渲染得出."""
    store = xero17.runner.deps.output_store
    populations = {p.procedure_id: p for p in await store.latest(xero17.eid, RunPopulation)}
    [b01] = populations["CSH-05"].rules
    assert b01 == xero_csh05_population()
    assert ([i.source_id for i in b01.items], [i.amount_cents for i in b01.items], b01.amount_cents) == (
        ["BA-090", "BA-091"],
        [-1090044, 1158123],
        68079,
    )
    assert [i.fields["statement_balance_cents"] for i in b01.items] == [i.amount_cents for i in b01.items]
    a01, a02, a03 = populations["PLN-07"].rules
    assert a01 == xero_pln07_a01_population()
    assert (a01.count, a01.amount_cents, {i.fields["basis"] for i in a01.items}) == (12, None, {"balance_sheet"})
    assert {row[0] for row in XERO_A01_CHANGES} < {i.source_id for i in a01.items}
    assert [(r.rule_id, r.count) for r in (a02, a03)] == [("A02", 2), ("A03", 3)]
    library = xero17.runner.deps.library
    for run in await store.latest(xero17.eid, ProcedureRun):  # 快照缺参数时 render_text 抛 UndefinedError
        scope = library.load(run.procedure_id).scope
        assert scope is not None, run.procedure_id
        render_text(scope, run.params_snapshot)


# ── 验收 8: 原目录不变 ──


@shared_loop
async def test_originals_unchanged(xero17: Xero17) -> None:
    """流程前后快照目录与补造目录的逐文件哈希相同 (同 R112 验收 11)."""
    assert xero17.moved == []


# ── R146: 打开功能 journals (只断言本组增量; 意见与报告不在本条, 停在 FIN-01 闸门) ──


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def journals() -> Xero17:
    """同 R119 的流程打开 journals: 复核在 10 条之后再提交 GEN-02 的 7 条 (xero_journal_reviews), 停在 FIN-01 闸门."""
    reviews = [*xero17_reviews(OWNER), *xero_journal_reviews(OWNER)]
    return await run_xero17(XERO_JOURNALS_FEATURES, reviews=reviews, stop_at_fin=True)


async def p2_pack(x: Xero17) -> dict[str, Procedure]:
    values = (await x.runner.app.aget_state(GraphRunner.config(x.eid))).values
    return {p.id: p for p in values["procedure_pack"] if p.phase is Phase.P2_Executing}


@shared_loop
async def test_journals_assemble_gen05_then_gen02(journals: Xero17, xero17: Xero17) -> None:
    """R146 验收 1: 项目冻结的功能集为 critic, journals; P2 程序包比基线只多 GEN-05 (第 0 层) 与 GEN-02 (第 1 层),
    两者都不对应审计目标; GEN-05 不执行的 6 条规则同 R131 验收 5 (看录入时间的 4 条 skipped, R04, R07 not_executable).
    流程停在 FIN-01 闸门, 没有后台错误."""
    deps = journals.runner.deps
    assert deps.features == XERO_JOURNALS_FEATURES == {"critic", "journals"}
    engagement = await deps.engagement_store.get(journals.eid)
    assert engagement is not None and engagement.version_snapshot is not None
    assert engagement.version_snapshot["features"] == "critic,journals"
    pack, base = await p2_pack(journals), await p2_pack(xero17)
    assert sorted(pack.keys() - base.keys()) == ["GEN-02", "GEN-05"] and base.keys() <= pack.keys()
    layers = {
        pid: row.layer for row in build_schedule(list(pack.values()), deps.library, 4) for pid in row.procedure_ids
    }
    assert (layers["GEN-05"], layers["GEN-02"]) == (0, 1)
    assert (pack["GEN-05"].objective_ids, pack["GEN-02"].objective_ids) == ([], [])
    run = await deps.output_store.get(journals.eid, ProcedureRun, "RUN-GEN-05-1")
    assert run is not None
    assert [(o.rule_id, o.status) for o in run.rule_outcomes] == [
        ("R01", "skipped"),
        ("R04", "not_executable"),
        ("R06", "skipped"),
        ("R07", "not_executable"),
        ("R08", "skipped"),
        ("R13", "skipped"),
    ]
    assert (await journals.runner.app.aget_state(GraphRunner.config(journals.eid))).next == ("gate_fin",)
    assert journals.runner.run_error(journals.eid) is None


@shared_loop
async def test_journals_results_and_schedule(journals: Xero17) -> None:
    """R146 验收 1, 3, 4: 例外, 判断, 任务与错报只多本组增量 (assert_results 的 journals); 汇总表多 XJ-499 的错报,
    合计 79853, 候选保留意见 (assert_materiality 的 journals)."""
    await assert_results(journals, journals=True)
    await assert_materiality(journals, journals=True)


@shared_loop
async def test_journals_decisions_and_calls(journals: Xero17, xero17: Xero17) -> None:
    """R146 验收 2, 3: decision 留痕比基线多 GEN-02 的 7 行 (按提交顺序, 排在 10 条复核之后), 少停在 FIN-01 之后的
    3 行; llm_call 比基线多 GEN-02 的 7 行, 少 RPT-03 的 2 行 (报告不在本条), 都第 1 次通过."""
    decided = {
        name: [e.idempotency_key for e in await entries_of(x) if e.kind is TrailKind.decision]
        for name, x in (("journals", journals), ("base", xero17))
    }
    after_fin = [d.id for d in xero17_gates(OWNER).values() if d.gate is not GateId.PLN_01_03]
    assert decided["journals"] == [
        *(k for k in decided["base"] if k not in after_fin),
        *(r.id for r in xero_journal_reviews(OWNER)),
    ]

    async def calls(x: Xero17) -> Counter[tuple[str | None, bool]]:
        entries = [e for e in await entries_of(x) if e.kind is TrailKind.llm_call]
        assert {(e.payload["attempt"], e.payload["level_failed"]) for e in entries} == {(1, None)}
        return Counter((e.procedure_id, (e.run_id or "").startswith("UPLOAD-")) for e in entries)

    journal_calls, base_calls = await calls(journals), await calls(xero17)
    assert (journal_calls - base_calls, base_calls - journal_calls) == (
        Counter({("GEN-02", False): 7}),
        Counter({("RPT-03", False): 2}),
    )


# ── R120: 连库变体 ──


@pytest.mark.slow
async def test_real_storage_variant(tmp_path: Path, demo_run: Demo) -> None:
    """需求 R120: 同 R119 的步骤与决定, 换用真实 Postgres 存储与检查点, 规则 SQL, 组装根的数据集载入 (快照与补造目录),
    函证与登记 mock 读补造目录, 真实 DataService 做演示数据导入与确认数据已齐; 大模型仍为 xero17_client()
    (SwitchableLLMClient 换内部客户端). 项目为 xero/20261017-q1, 期间 07-01 至 09-30, 画像 CLIENT-XERO-3C1332BB-Q1
    (不换 ID). 结果与留痕同 R119 验收 2-7; 全部证据指针 (含例外, 承接评分卡与人工结果) 经真实存储解析, document
    指针都指向补造 manifest 中的文件; 签字后 put 抛 EngagementLocked; 账表版本行 11 张表次 (快照七张在 v2, 补造四张
    v3 至 v6), 确认人为负责人, 抽取版本表中文件补登记 42 行, 合同 3 行; 同库随后用 xero/20261017-mock 建项目不报
    VersionConflict."""
    snapshot_dir("20261017-q1")  # 先核对数据: 跟踪了却找不到时失败, 不因没配库而跳过
    snapshot_dir("20261017-mock")
    supplements = supplements_dir("20261017-q1")
    features = ",".join(sorted(XERO17_FEATURES))
    async with real_storage(
        tmp_path, dataset=XERO_DATASET, period=XERO_PERIOD, load_dataset=False, fresh_profile=False, FEATURES=features
    ) as run:
        deps = run.deps
        client = xero17_client()
        for switchable in (deps.llm, deps.review_llm):
            cast(SwitchableLLMClient, switchable).reload(client, "mock")
        x = await drive_xero17(run.s.runner, run.data_service(), run.eid, run.owner, client)
        engagement = await deps.engagement_store.get(run.eid)
        assert engagement is not None
        assert (engagement.client_id, engagement.dataset, engagement.period_start, engagement.period_end) == (
            "CLIENT-XERO-3C1332BB-Q1",
            XERO_DATASET,
            *XERO_PERIOD,
        )
        await assert_steps(x)
        await assert_results(x)
        await assert_materiality(x)
        await assert_report(x)
        assert_trail(await entries_of(x), trail_of(demo_run.s).entries)
        pointers = await assert_pointers_and_reads(x, everything=True)
        assert [(pid, name) for pid, name, script in x.agents if script] == []
        assert {pid: name for pid, name, _ in x.agents} == AGENTS
        await assert_documents(x)
        assert x.moved == []
        manifest = json.loads((supplements / "manifest.json").read_text(encoding="utf-8"))
        listed = {d["document_id"] for d in manifest["documents"]}
        assert {p.source_id for p in pointers if p.source_type == "document"} <= listed
        assert {d.id for d in await deps.financial_store.documents(run.eid)} <= listed

        judgment = (await deps.output_store.latest(run.eid, Judgment))[0]
        with pytest.raises(EngagementLocked):
            newer = judgment.model_copy(update={"version": judgment.version + 1})
            await deps.output_store.put(run.eid, [newer], actor=run.owner)

        versions = await deps.data.versions(run.eid)
        assert sorted((v.data_version, v.kind.value, v.source, v.file_name) for v in versions) == sorted(
            [(2, k.value, "xero", None) for k in XERO_TABLES]
            + [
                (3, "sales_invoices", "import", "contract_links.csv"),
                (4, "bank_accounts", "import", "bank_accounts.csv"),
                (5, "delivery_notes", "import", "delivery_notes.csv"),
                (6, "employees", "import", "payroll/employees.csv"),
            ]
        )
        assert (await deps.data.readiness(run.eid)).ready_by == run.owner
        records = Counter(r.target_type for r in await deps.data.extraction_versions(run.eid))
        assert (records[DETAILS], records["contract"]) == (42, 3)

        mock = deps.datasets.client_profile("xero/20261017-mock")
        other = await deps.engagement_store.create(  # type: ignore[attr-defined]
            owner_id=run.owner,
            period_start=XERO_PERIOD[0],
            period_end=XERO_PERIOD[1],
            dataset="xero/20261017-mock",
            profile=mock,
        )
        assert (other.client_id, other.dataset) == (mock.id, "xero/20261017-mock")
        assert mock.id != engagement.client_id
