"""需求 R119 (设计稿 v2.1 第 7.3 节末段 "只开现有 17 条时"): 新功能全关, 现有 17 条程序在 Xero 演示数据上离线跑完
P0-P4. 第 1 级前的回归里程碑, 长期保留 (同 Numbat 回归 R55, tests/test_e2e_mock).

流程只经 GraphRunner, GraphDecisionSubmit 与 DataService 的接口, 模块里只跑一遍: 空项目
(tests/xero_fakes.new_xero_deps) 启动 → ACC-01 闸门处演示数据导入 acceptance → ACC-01 approve → 停在 p1_await_data
时导入 fieldwork → 项目负责人确认数据已齐 (tests/xero_flow) → 收尾 (tests/xero_fakes.finish_xero17: PLN-01/03, 10 条
复核, FIN-01 无保留意见, RPT-01 先退回再批准). 主模型与复核模型 (上传处理也用它) 为 LayeredMockLLMClient(roots=
[llm_xero_only17, 运行副本]), 规则引擎为 FakeRuleEngine(XERO_RULES). 断言函数以 Xero17 为参数, 连库变体 (R120)
共用. 读快照与补造目录, 标 external.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.data import DataInput
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AuditException,
    AuditReportVersion,
    Confirmation,
    ConfirmationResult,
    DocumentType,
    ExceptionDisposition,
    GateDecision,
    Judgment,
    MaterialityCandidates,
    Misstatement,
    OpinionType,
    Phase,
    ProposedAction,
    ReportSection,
    ReportStatus,
    ReviewStatus,
    ReviewTask,
    TaskStatus,
    TrailEntry,
    TrailKind,
    UncorrectedSchedule,
)
from backend.contracts.views import GateView, PlanningMaterial
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.services.data_service import DataService
from backend.services.features import BASELINE
from tests.fakes import ENGAGEMENT, FakeRuleEngine, FakeTrail, ScriptAgent, wait_idle
from tests.fixture_sets import XERO
from tests.test_decisions import Started
from tests.test_e2e_mock import assert_pointers_resolve, run_demo
from tests.xero_fakes import (
    XERO17_CONTRACTS,
    XERO17_FEATURES,
    XERO17_INVOICES,
    XERO_A01_CHANGES,
    XERO_RULES,
    finish_xero17,
    new_xero_deps,
    xero17_gates,
    xero17_reviews,
    xero_uploads,
)
from tests.xero_flow import approve_acceptance, confirm_ready, demo_source, import_stage, offline_runner
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


async def run_xero17() -> Xero17:
    """离线跑完整个流程 (见模块说明), 记下每次执行的 Agent; 前后各记一次原目录的逐文件哈希."""
    before = originals()
    XERO.ready()  # 运行副本经 tests.external 定位核对
    client = LayeredMockLLMClient([ONLY17, XERO.runtime])
    deps = new_xero_deps(
        trail=FakeTrail(),
        llm=client,
        review_llm=client,
        rules=FakeRuleEngine(dict(XERO_RULES)),
        features=XERO17_FEATURES,
    )
    runner = offline_runner(deps)
    service = DataService(deps, runner, demo=demo_source())
    agents: list[tuple[str, str, bool]] = []
    run_agent = GraphNodes.run_agent

    async def record_agent(self: GraphNodes, agent: ProcedureAgent[Any], ctx: RunContext) -> Any:
        agents.append((ctx.procedure.id, type(agent).__name__, isinstance(agent, ScriptAgent)))
        return await run_agent(self, agent, ctx)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(GraphNodes, "run_agent", record_agent)
        await runner.start(EID)
        await wait_idle(runner, EID)
        assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_acc01",), runner.run_error(EID)
        await import_stage(service, EID, "acceptance", OWNER)
        await approve_acceptance(runner, EID, OWNER)
        assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("p1_await_data",)
        query = GraphStateQuery(runner.app, deps, runner)
        awaiting = (await query.overview(EID)).awaiting_data
        await import_stage(service, EID, "fieldwork", OWNER)
        await confirm_ready(service, EID, OWNER)
        pln = await query.gate(EID)
        assert pln is not None, runner.run_error(EID)
        await finish_xero17(runner, EID, OWNER)
    return Xero17(runner, EID, OWNER, client, awaiting, pln, agents, changed(before, originals()))


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


async def assert_results(x: Xero17) -> None:
    """例外 11 条 (PLN-07 A01 九条 open, 科目与变动额同 R112; R03-1 与 D04-1 attributed); 判断 10 条最新为 v2
    confirmed (REV-03 三条与 REV-02 六条 accept, BA-091 misstatement); 任务 10 条 decided; 两户函证 agreed; 错报
    1 条."""
    store = x.runner.deps.output_store
    exceptions = {e.id: e for e in await store.latest(x.eid, AuditException)}
    a01 = {
        f"X-PLN-07-1-A01-{n}": ([code], int(current) - int(opening), ExceptionDisposition.open)
        for n, (code, _, current, _, opening, _) in enumerate(XERO_A01_CHANGES, 1)
    }
    assert {i: (e.source_ids, e.amount_cents, e.disposition) for i, e in exceptions.items()} == a01 | {
        "X-REV-08-1-R03-1": ([ORC1026], 330000, ExceptionDisposition.attributed),
        "X-CSH-02-1-D04-1": (["BA-091"], 4853, ExceptionDisposition.attributed),
    }
    judgments = {j.id: j for j in await store.latest(x.eid, Judgment)}
    expected = {
        **{f"J-REV-03-1-{c}": ProposedAction.accept for c in XERO17_CONTRACTS},
        **{f"J-REV-02-1-{i}": ProposedAction.accept for i in XERO17_INVOICES},
        "J-CSH-02-1-BA-091": ProposedAction.misstatement,
    }
    assert {i: (j.version, j.review_status, j.proposed_action) for i, j in judgments.items()} == {
        i: (2, ReviewStatus.confirmed, action) for i, action in expected.items()
    }
    assert exceptions["X-REV-08-1-R03-1"].attributed_by == f"J-REV-02-1-{ORC1026}"
    assert exceptions["X-CSH-02-1-D04-1"].attributed_by == "J-CSH-02-1-BA-091"
    tasks = await store.latest(x.eid, ReviewTask)
    assert sorted(t.target_id for t in tasks) == sorted(expected)
    assert {t.status for t in tasks} == {TaskStatus.decided}
    confirmations = {c.id: c for c in await store.latest(x.eid, Confirmation)}
    assert {
        cid: (c.result, c.confirmed_balance_cents, c.book_balance_cents, c.difference_cents, c.accrued_interest_cents)
        for cid, c in confirmations.items()
    } == {
        "C-BA-090": (ConfirmationResult.agreed, -1090044, -1090044, 0, 0),
        "C-BA-091": (ConfirmationResult.agreed, 1158123, 1158123, 0, 4853),
    }
    [misstatement] = await store.latest(x.eid, Misstatement)
    assert (
        misstatement.judgment_id,
        misstatement.type,
        misstatement.account,
        misstatement.assertion,
        misstatement.amount_cents,
        misstatement.direction,
        misstatement.status,
    ) == (
        "J-CSH-02-1-BA-091",
        "factual",
        "Interest income (270)",
        "Completeness",
        4853,
        "understated",
        "uncorrected",
    )


@shared_loop
async def test_materiality_and_schedule(xero17: Xero17) -> None:
    await assert_materiality(xero17)


async def assert_materiality(x: Xero17) -> None:
    """重要性 32431 / 16216 / 1622; 候选的三个基准 (收入, 总资产, 税前亏损); PLN-01/03 闸门材料没有未覆盖的目标;
    汇总表只有 BA-091 的错报 (4853), 未超过重要性, 候选无保留意见."""
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
    [misstatement] = await store.latest(x.eid, Misstatement)
    assert schedule is not None
    assert [(i.misstatement_id, i.amount_cents) for i in schedule.items] == [(misstatement.id, 4853)]
    assert (schedule.total_cents, schedule.materiality_cents, schedule.candidate_opinion) == (
        4853,
        32431,
        OpinionType.unqualified,
    )


# ── 验收 4: 报告, 归档与锁定 ──


@shared_loop
async def test_report_and_lock(xero17: Xero17) -> None:
    await assert_report(xero17)
    with pytest.raises(DecisionRejected) as e:  # 签字后提交任一决定
        rejected: GateDecision = xero17_gates(xero17.owner)["rpt_approve"].model_copy(update={"id": "D-AFTER"})
        await GraphDecisionSubmit(xero17.runner.app, xero17.runner.deps, xero17.runner).submit(xero17.eid, rejected)
    assert e.value.code == "ENGAGEMENT_LOCKED"


async def assert_report(x: Xero17) -> None:
    """REPORT-1 v1 draft; REPORT-2 v2 signed (负责人签字, 回应退回决定): 意见段以 "Opinion" 开头, 没有持续经营段,
    基础段写明 Simplified Disclosures, 签名块以 "Jordan Ellis\\n" 开头, 不引判断; 项目归档并锁定."""
    reports = {r.id: r for r in await x.runner.deps.output_store.latest(x.eid, AuditReportVersion)}
    assert (reports["REPORT-1"].version, reports["REPORT-1"].status) == (1, ReportStatus.draft)
    signed = reports["REPORT-2"]
    assert (signed.version, signed.status, signed.approved_by, signed.addresses_decision_id) == (
        2,
        ReportStatus.signed,
        x.owner,
        xero17_gates(x.owner)["rpt_reject"].id,
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
async def test_trail(xero17: Xero17) -> None:
    numbat = await run_demo()  # Numbat 回归 (R55) 的同一套状态迁移, 离线约半秒
    assert_trail(await entries_of(xero17), numbat.runner.deps.trail.entries)  # type: ignore[attr-defined]


def transitions(entries: list[TrailEntry]) -> Counter[tuple[str, str, str]]:
    """transition 留痕的 (对象, 原状态, 新状态) 多重集合."""
    return Counter(
        (e.payload["id"], e.payload["from"], e.payload["to"]) for e in entries if e.kind is TrailKind.transition
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


async def assert_pointers_and_reads(x: Xero17) -> None:
    """判断, 合同与函证的证据指针都能解析; 大模型读取: RPT-03 两次读自 llm_xero_only17 (r1, r2), 其余 36 次读自运行副本
    (DOC-CLASSIFY 21, DOC-04 3, DOC-06 2, REV-03 3, REV-02 6, CSH-02 1), 每个文件一次, 都是 attempt 1 且没有失败级别."""
    deps = x.runner.deps
    await assert_pointers_resolve(Started(x.runner, deps.output_store, deps.engagement_store), x.eid)  # type: ignore[arg-type]
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


# ── 验收 7: 文件, 数据版本与补交请求 ──


@shared_loop
async def test_documents_versions_and_requests(xero17: Xero17) -> None:
    await assert_documents(xero17)


async def assert_documents(x: Xero17) -> None:
    """文件 23 份: 审计师上传 21 份 (负责人上传, 类型, 可靠性, 文件日期与关联对象同补造清单; 合同 3 份 extracted, 其余
    18 份 classified), 银行直达回函 2 份 (external_original, extracted). 数据版本 10 (方案取舍见确认材料 R119): 承接
    文件 1, 快照七张 2, 补造四张 3 至 6, 上传后抽取的三份合同 7 至 9, 补登记 10. 补交请求中审批日志与待审财报仍 open,
    其余 received."""
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
        "REQ-financial_statements": "open",
    }


# ── 验收 8: 原目录不变 ──


@shared_loop
async def test_originals_unchanged(xero17: Xero17) -> None:
    """流程前后快照目录与补造目录的逐文件哈希相同 (同 R112 验收 11)."""
    assert xero17.moved == []
