"""需求 R274 (后端): 过期程序的判定 (程序读的数据与依赖), 重跑入口 (DecisionSubmit.rerun_stale), 当前结果
(FIN-04 与 FIN-01 材料不重复计数), 恢复补做与 RPT-03 重跑后的重拟.

数据变动用 FakeDataStore.record 模拟 (同真实实现: 数据版本 +1 并记下变了哪类数据); Numbat 流程用脚本 Agent
(test_flow). 连库与真实规则引擎的验收 1, 2 见 test_rerun_stale_postgres.py.
"""

import hashlib
import importlib.util
import json
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from langgraph.types import Command

from backend.adapters.llm import MockLLMClient
from backend.agents import evidence
from backend.agents.concierge.report import revision_inputs
from backend.agents.evidence import confirmation_key, legacy_key
from backend.agents.fieldwork import FieldworkAgent
from backend.contracts.data import DataChange, DataInput, ExtractionEdit, TableKind, table_changes
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AuditException,
    AuditReportVersion,
    Confirmation,
    Decision,
    DocumentType,
    ExceptionDisposition,
    GateVerdict,
    Judgment,
    JudgmentRef,
    Misstatement,
    OpinionType,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ReportSection,
    RerunRequest,
    ReviewTask,
    RunStatus,
    RunTrigger,
    TaskKind,
    TaskStatus,
    TrailKind,
    UncorrectedSchedule,
)
from backend.contracts.ports import LLMResponse
from backend.contracts.records import BankAccount, TrialBalanceRow
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState
from backend.contracts.views import CompletionMaterial, EngagementOverview, ReportMaterial
from backend.core.config import get_settings
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.trail import phase_transition
from backend.services.data_service import DataService
from backend.services.judgment_engine import DOCUMENT_EVIDENCE
from backend.services.procedure_library import ProcedureLibrary, document_read_problems
from backend.services.staleness import current_results, rerunnable, stale_procedures
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_CUTOFF_DRAFT,
    NUMBAT_FEATURES,
    NUMBAT_PROFILE,
    NUMBAT_RECORDS,
    NUMBAT_RULES,
    NUMBAT_S01_ROWS,
    NUMBAT_TRIAL_BALANCE,
    EngagementWrite,
    FakeConfirmationChannel,
    FakeDataStore,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    LayeredMockLLM,
    api_client,
    make_ctx,
    make_deps,
    make_procedure,
    make_runner,
    wait_idle,
)
from tests.test_concierge import REJECT, fin01, handed_to_human, report, revising
from tests.test_concierge import setup as concierge_setup
from tests.test_decisions import Started, acc01_decision, pln_decision
from tests.test_documents_upload import drain
from tests.test_documents_upload import setup as upload_setup
from tests.test_e2e_mock import real_agents, start_demo
from tests.test_fin_gate import CONFIG, at_fin_gate, fin_decision
from tests.test_flow import EID, REVIEWS, approve_materiality, start_numbat
from tests.test_p2_agents_demo import replies
from tests.test_review_decisions import record
from tests.test_rpt_gate import at_rpt_gate, rpt_decision

LIBRARY = make_deps(FakeRegistry(), FakeTrail()).library
# Numbat 流程中执行过的程序 (闸门程序 ACC-01, PLN-03, FIN-01, RPT-01 不执行)
EXECUTED = ["ACC-04", "DOC-04", "PLN-07", "PLN-01", "REV-08", "REV-03", "REV-02", "CSH-04", "CSH-05", "DOC-06"]
EXECUTED += ["CSH-02", "FIN-04", "RPT-03"]
PACK = [make_procedure(t.id, t.phase, status=ProcedureStatus.completed) for t in LIBRARY.all()]
SEEDED = [DataChange(data_version=1, input=i) for i in DataInput]  # 数据集载入: 版本 1 变了全部各类


def ran(procedure_id: str, run_no: int = 1, data_version: int | None = 1, minutes: int = 0) -> ProcedureRun:
    """某程序第 run_no 次成功执行, 记数据版本 data_version (承接阶段为 None), 开始于 NOW 之后 minutes 分钟."""
    if LIBRARY.load(procedure_id).phase is Phase.P0_Acceptance:
        data_version = None
    return ProcedureRun(
        id=f"RUN-{procedure_id}-{run_no}",
        procedure_id=procedure_id,
        run_no=run_no,
        trigger=RunTrigger.initial if run_no == 1 else RunTrigger.rerun,
        params_snapshot={},
        started_at=NOW + timedelta(minutes=minutes),
        ended_at=NOW + timedelta(minutes=minutes),
        status=RunStatus.succeeded,
        data_version=data_version,
    )


RUNS = [ran(pid) for pid in EXECUTED]


def changed(*inputs: DataInput, version: int = 2) -> list[DataChange]:
    return [*SEEDED, *(DataChange(data_version=version, input=i) for i in inputs)]


# ── 过期判定 (services.staleness) ──


@pytest.mark.parametrize(
    ("data_input", "expected"),
    [
        (DataInput.accounts, {"PLN-07", "PLN-01", "REV-02"}),  # REV-02 带发票分录行的科目名称与分类 (需求 R100)
        (DataInput.trial_balance, {"PLN-07", "PLN-01", "CSH-05", "CSH-02"}),
        # PLN-07 的 A03 月度数读总账 (需求 R103); REV-02 带发票过账分录的全部行 (需求 R100)
        (DataInput.journal_entries, {"PLN-07", "CSH-02", "REV-02"}),
        (DataInput.sales_invoices, {"REV-08", "REV-02"}),
        (DataInput.supplier_bills, set()),  # 应付账单 (需求 R178): Demo 程序库里还没有程序读它
        (DataInput.bank_accounts, {"CSH-04", "DOC-06", "CSH-05", "CSH-02"}),  # DOC-06 依赖 CSH-04
        (DataInput.bank_transactions, {"REV-08", "REV-02", "CSH-05", "CSH-02"}),  # REV-02 依赖 REV-08
        (DataInput.delivery_notes, {"REV-08", "REV-02"}),
        (DataInput.approval_log, set()),  # 没有程序直接读审批日志; 写审批日志另记 journal_entries (table_changes)
        (DataInput.employees, set()),  # 工资册 (需求 R96): Demo 程序库里还没有程序读它 (FIN-15 由 R220 做)
        (DataInput.contracts, {"REV-03", "REV-02"}),
        (DataInput.confirmations, {"CSH-02"}),
        (DataInput.documents, {"REV-03", "REV-02", "CSH-02"}),  # 补登记: 判断引用文件页的程序 (需求 R90)
        (DataInput.financial_statements, set()),  # 待审财报 (需求 R217): Demo 程序库里还没有程序读它 (FIN-12 写 reads)
    ],
)
def test_stale_by_what_each_program_reads(data_input: DataInput, expected: set[str]) -> None:
    """验收 1 的口径: 只有读到变动数据的程序 (模板 reads) 与依赖它们的程序过期; 结果按程序包顺序."""
    stale = stale_procedures(PACK, RUNS, changed(data_input), LIBRARY)
    assert set(stale) == expected
    assert stale == [p.id for p in PACK if p.id in expected]


# 带文件类型的文件登记变动 (需求 R304): 只有读该类文件登记的程序 (模板 document_reads) 过期; 依赖它们的随之
TYPED_DOCUMENT_CHANGES = [
    pytest.param([DocumentType.financial_statements], set(), id="financial_statements"),  # 待审财报: 没有程序读
    pytest.param([DocumentType.contract], {"REV-03", "REV-02"}, id="contract"),  # DOC-04 抽合同, 不读登记
    pytest.param([DocumentType.confirmation], {"CSH-02"}, id="confirmation"),  # DOC-06 抽回函, 不读登记
    pytest.param([DocumentType.contract, DocumentType.confirmation], {"REV-03", "REV-02", "CSH-02"}, id="both"),
    pytest.param([DocumentType.agreement, DocumentType.minutes], set(), id="others"),
]


@pytest.mark.parametrize(("types", "expected"), TYPED_DOCUMENT_CHANGES)
def test_document_changes_with_types_stale_only_their_readers(types: list[DocumentType], expected: set[str]) -> None:
    """需求 R304 验收 1: 文件登记的变动带了文件类型时, 只有 document_reads 含其中某类的程序与依赖它们的程序过期. 没带
    类型的旧变动 (上面 DataInput.documents 一行) 照旧让 reads 含 documents 的 REV-03, REV-02, CSH-02 都过期 (验收 3)."""
    change = DataChange(data_version=2, input=DataInput.documents, document_types=types)
    assert set(stale_procedures(PACK, RUNS, [*SEEDED, change], LIBRARY)) == expected


def test_programs_reading_registrations_declare_the_document_types() -> None:
    """需求 R304 验收 2 (取 "必须声明"): reads 含 documents 的程序都写了 document_reads, 两者须同时写, 否则程序库加载
    即报错 (产出侧 document_types 的对应校验见 test_merge)."""
    readers = {t.id: t.document_reads for t in LIBRARY.all(include_disabled=True) if t.document_reads}
    assert readers == {
        "ACC-08": [DocumentType.engagement_letter, DocumentType.minutes],  # 承接阶段, 不判过期
        "REV-03": [DocumentType.contract],
        "REV-02": [DocumentType.contract],
        "CSH-02": [DocumentType.confirmation],
        # 需求 R134: 合同与凭证关联的文件; 后者类型不定, 列全部可作证据的类型 (加了新的证据类型须跟上, 否则该类文件的
        # 补登记不让 GEN-02 过期)
        "GEN-02": list(DOCUMENT_EVIDENCE),
        # 需求 R138: 关系的证据可靠性取登记 PDF 的文件登记, 重建也按它取文件
        "GEN-08": [DocumentType.registry_extract],
    }
    rev03 = LIBRARY.load("REV-03")
    undeclared = rev03.model_copy(update={"document_reads": []})
    stray = LIBRARY.load("DOC-04").model_copy(update={"document_reads": [DocumentType.contract]})
    assert document_read_problems([rev03, undeclared, stray]) == [
        "REV-03: reads 含 documents 而没写 document_reads",
        "DOC-04: 写了 document_reads 而 reads 没有 documents",
    ]


def test_library_rejects_undeclared_document_reads(tmp_path: Path) -> None:
    """程序库加载时拒绝 reads 含 documents 而没写 document_reads 的模板 (知识目录的副本里去掉 REV-03 的那一行)."""
    root = tmp_path / "knowledge"
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    path = root / "procedures" / "REV-03.yaml"
    path.write_text(
        "".join(line for line in path.read_text(encoding="utf-8").splitlines(True) if "document_reads" not in line),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="REV-03: reads 含 documents 而没写 document_reads"):
        ProcedureLibrary(root)


def test_nothing_stale_without_later_changes() -> None:
    """数据集载入 (版本 1) 不让版本 1 的执行过期; 变动与执行同版本也不算 (执行时已读到)."""
    assert stale_procedures(PACK, RUNS, SEEDED, LIBRARY) == []
    assert stale_procedures(PACK, [ran(pid, data_version=2) for pid in EXECUTED], changed(*DataInput), LIBRARY) == []


def test_acceptance_runs_and_unexecuted_programs_never_stale() -> None:
    """承接阶段的执行不记数据版本, 没执行过的程序没有可比的版本: 都不算过期."""
    runs = [r for r in RUNS if r.procedure_id != "REV-08"]
    stale = stale_procedures(PACK, runs, changed(*DataInput), LIBRARY)
    assert "ACC-04" not in stale and "REV-08" not in stale
    assert "REV-02" in stale  # 自己读发票, 不靠 REV-08


def test_dependency_rerun_after_program_makes_it_stale() -> None:
    """依赖的程序在它之后又执行过 (如只重跑了 REV-08), 它也过期; 它自己之后再执行, 就不过期."""
    rerun_08 = ran("REV-08", run_no=2, data_version=2, minutes=5)
    assert stale_procedures(PACK, [*RUNS, rerun_08], changed(), LIBRARY) == ["REV-02"]
    rerun_02 = ran("REV-02", run_no=2, data_version=2, minutes=6)
    assert stale_procedures(PACK, [*RUNS, rerun_08, rerun_02], changed(), LIBRARY) == []
    # 依赖传播只看程序包中的程序
    without = [p for p in PACK if p.id != "REV-08"]
    assert stale_procedures(without, [*RUNS, rerun_08], changed(), LIBRARY) == []


def test_latest_run_decides() -> None:
    """按最近一次执行 (run_no 最大) 的数据版本判断."""
    runs = [*RUNS, ran("REV-08", run_no=2, data_version=2)]
    assert stale_procedures(PACK, runs, changed(DataInput.sales_invoices), LIBRARY) == ["REV-02"]


def test_table_changes_adds_journal_for_approval_log() -> None:
    """写审批日志会改总账分录的审批列, 另记 journal_entries; 其余账表只记自己, 按 DataInput 顺序. 读总账的程序
    (CSH-02, PLN-07 的 A03, REV-02 的发票分录行) 随之过期: 过期按表判断, 不细到列."""
    assert table_changes([TableKind.sales_invoices, TableKind.accounts]) == [
        DataInput.accounts,
        DataInput.sales_invoices,
    ]
    assert table_changes([TableKind.approval_log]) == [DataInput.journal_entries, DataInput.approval_log]
    stale = stale_procedures(PACK, RUNS, changed(*table_changes([TableKind.approval_log])), LIBRARY)
    assert stale == ["CSH-02", "PLN-07", "REV-02"]


def test_rerunnable_only_execution_phase_not_waiting_for_rerun() -> None:
    """能重跑的: 执行阶段的过期程序, 不在等重跑 (executing); 计划阶段的过期只标记 (Q2)."""
    pack = [
        make_procedure("PLN-07", Phase.P1_Planning, status=ProcedureStatus.completed),
        make_procedure("REV-08", Phase.P2_Executing, status=ProcedureStatus.completed),
        make_procedure("REV-03", Phase.P2_Executing, status=ProcedureStatus.awaiting_human),
        make_procedure("REV-02", Phase.P2_Executing, status=ProcedureStatus.executing),
    ]
    assert rerunnable(pack, ["REV-02", "PLN-07", "REV-03", "REV-08"]) == ["REV-08", "REV-03"]


# ── 当前结果 (services.staleness.current_results) ──


def judgment(jid: str, run_id: str, subject_id: str = "SI-2026-0612") -> Judgment:
    return Judgment.model_validate(
        {
            "id": jid,
            "procedure_id": run_id.split("-", 1)[1].rsplit("-", 1)[0],
            "run_id": run_id,
            "subject_id": subject_id,
            "conclusion": "x",
            "confidence": 0.9,
            "confidence_parts": {},
            "reasoning_chain": [],
            "evidence": [],
            "counter_argument": "",
            "proposed_action": "misstatement",
            "autonomy_level": "L2",
            "performed_by": "A6",
            "created_at": NOW,
        }
    )


def exception(xid: str, run_id: str) -> AuditException:
    return AuditException(
        id=xid,
        procedure_id="REV-08",
        run_id=run_id,
        rule_id="R01",
        severity="high",
        source_ids=["SI-2026-0612"],
        amount_cents=14200000,
        detail={},
        evidence=[],
    )


def misstatement(mid: str, judgment_id: str | None = None, exception_id: str | None = None) -> Misstatement:
    return NUMBAT_MISSTATEMENT.model_copy(update={"id": mid, "judgment_id": judgment_id, "exception_id": exception_id})


NUMBAT_MISSTATEMENT = Misstatement.model_validate(
    {
        "id": "M-0",
        "judgment_id": None,
        "exception_id": None,
        "review_record_id": "D-0",
        **NUMBAT_CUTOFF_DRAFT.model_dump(),
    }
)


def results_state(requests: list[RerunRequest]) -> EngagementState:
    """两轮 REV-08 例外与 REV-02 判断 (第 1 轮另有人工判断 -H1), 由各轮的判断与例外认定的错报, 外加查不到来源的."""
    judgments = [
        judgment("J-REV-02-1-SI-2026-0612", "RUN-REV-02-1"),
        judgment("J-REV-02-1-SI-2026-0612-H1", "RUN-REV-02-1"),
        judgment("J-REV-02-2-SI-2026-0612", "RUN-REV-02-2"),
    ]
    exceptions = [exception("X-REV-08-1-R01-1", "RUN-REV-08-1"), exception("X-REV-08-2-R01-1", "RUN-REV-08-2")]
    misstatements = [
        misstatement("M-1", judgment_id="J-REV-02-1-SI-2026-0612"),
        misstatement("M-1H", judgment_id="J-REV-02-1-SI-2026-0612-H1"),
        misstatement("M-1X", exception_id="X-REV-08-1-R01-1"),
        misstatement("M-2", judgment_id="J-REV-02-2-SI-2026-0612"),
        misstatement("M-2X", exception_id="X-REV-08-2-R01-1"),
        misstatement("M-LOST", judgment_id="J-GONE"),
    ]
    return {  # type: ignore[typeddict-item]
        "judgments": judgments,
        "exceptions": exceptions,
        "misstatements": misstatements,
        "rerun_requests": requests,
    }


def request(**changes: Any) -> RerunRequest:
    base = RerunRequest(
        id="RR-1",
        procedure_ids=["REV-08", "REV-02"],
        superseded_run_ids=["RUN-REV-02-1", "RUN-REV-08-1"],
        data_version=2,
        requested_by="U-1",
        requested_at=NOW,
    )
    return base.model_copy(update=changes)


def test_current_results_drop_superseded_runs() -> None:
    """被取代的执行的判断, 例外与由它们认定的错报 (含人工判断 -H, 其 run_id 是任务所属的执行) 不计;
    来源查不到的错报照旧计入, 不悄悄丢掉; 各保持原顺序."""
    current = current_results(results_state([request()]))
    assert [j.id for j in current.judgments] == ["J-REV-02-2-SI-2026-0612"]
    assert [x.id for x in current.exceptions] == ["X-REV-08-2-R01-1"]
    assert [m.id for m in current.misstatements] == ["M-2", "M-2X", "M-LOST"]


def test_current_results_are_everything_without_rerun_requests() -> None:
    state = results_state([])
    current = current_results(state)
    assert (current.judgments, current.exceptions, current.misstatements) == (
        state["judgments"],
        state["exceptions"],
        state["misstatements"],
    )


def test_rev02_contract_judgment_ignores_superseded_runs() -> None:
    """REV-02 取合同的 REV-03 判断只看当前结果: REV-03 重跑后 (新执行没有出这份合同的判断时) 不再取旧判断."""
    ctx = make_ctx(make_deps(FakeRegistry(), FakeTrail()), "REV-02")
    old = judgment("J-REV-03-1-CON-001", "RUN-REV-03-1", "CON-001")
    state = ctx.state | {"judgments": [old]}
    found = FieldworkAgent.contract_judgment(replace(ctx, state=state), "CON-001")  # type: ignore[arg-type]
    assert found is not None and found["id"] == old.id
    superseded = request(procedure_ids=["REV-03"], superseded_run_ids=["RUN-REV-03-1"])
    state = state | {"rerun_requests": [superseded]}
    assert FieldworkAgent.contract_judgment(replace(ctx, state=state), "CON-001") is None  # type: ignore[arg-type]


def test_reads_must_cover_tables_in_rule_sql(tmp_path: Path) -> None:
    """程序库加载时校验: 规则 SQL 读到的账表须在 reads 中 (漏了就标不出过期), 报错带程序编号与漏的表."""
    root = tmp_path / "knowledge"
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    path = root / "procedures" / "REV-08.yaml"
    text = path.read_text(encoding="utf-8")
    full = "reads: [sales_invoices, delivery_notes, bank_transactions]"
    assert full in text
    path.write_text(text.replace(full, "reads: [sales_invoices]"), encoding="utf-8")
    with pytest.raises(ValueError, match="REV-08 读 bank_transactions, delivery_notes"):
        ProcedureLibrary(root)


# ── 流程 (脚本 Agent 的 Numbat) ──

SECOND_REVIEWS = [  # 重跑后 REV-02 第 2 轮的两条 normal 任务, 决定同第 1 轮
    record(
        "T-J-REV-02-2-SI-2026-0612-v1",
        Decision.misstatement,
        id="D-R2-0612",
        misstatement_draft=NUMBAT_CUTOFF_DRAFT,
    ),
    record("T-J-REV-02-2-SI-2026-0618-v1", id="D-R2-0618"),
]


async def overview(s: Started) -> EngagementOverview:
    return await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).overview(EID)


def data_of(s: Started) -> FakeDataStore:
    data = s.runner.deps.data
    assert isinstance(data, FakeDataStore)
    return data


async def rerun(s: Started, procedure_ids: list[str] | None = None, request_id: str = "RR-1") -> RerunRequest:
    """提交重跑, 等后台停下."""
    found = await s.submit.rerun_stale(EID, request_id, procedure_ids, requested_by="U-1")
    await wait_idle(s.runner, EID)
    return found


async def review(s: Started, decisions: list[Any]) -> None:
    for decision in decisions:
        assert await s.next_nodes() == ("await_events",), (decision.task_id, s.runner.run_error(EID))
        await s.submit.submit(EID, decision)
        await wait_idle(s.runner, EID)


def latest(s: Started, model: type[Any], oid: str) -> Any:
    return max((r for r in s.output_store.rows if isinstance(r, model) and r.id == oid), key=lambda r: r.version)


def trail_keys(s: Started, kind: TrailKind) -> list[str | None]:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e.idempotency_key for e in trail.entries if e.kind is kind]


async def demo_at_fin_gate() -> Started:
    """真实 P2 Agent (规则引擎与大模型为假的, 见 test_e2e_mock.start_demo) 的 Numbat 走完全部复核, 停在 FIN-01 前."""
    s = await start_demo()
    await wait_idle(s.runner, EID)
    await review(s, REVIEWS)
    assert await s.next_nodes() == ("gate_fin",)
    return s


async def test_rerun_at_fin_gate_replaces_results_and_represents_fin01() -> None:
    """验收 1, 2 (假存储, 真实 P2 Agent): 停在 FIN-01 时发票表变了, 只有 REV-08, REV-02 过期; 全部重跑: 一个事务写
    请求, 两条程序的 executing 新版本与阶段回 P2; 两条用新数据版本新建执行 (run_no 2), 新例外与新任务进入复核, 新判断
    只引用当前的例外; 复核后 FIN-04 只计新结果的错报 (不重复计数), FIN-01 以 SCHEDULE v2 重新呈报, 过期程序清空."""
    s = await demo_at_fin_gate()
    before = await overview(s)
    assert before.stale_procedure_ids == [] and before.can_rerun

    data_of(s).record(DataInput.sales_invoices)
    stale = await overview(s)
    assert stale.stale_procedure_ids == ["REV-02", "REV-08"]
    assert {p.id: (p.stale, p.rerunnable) for p in stale.procedures if p.stale} == {
        "REV-02": (True, True),
        "REV-08": (True, True),
    }
    commits = len(s.output_store.commits)

    found = await rerun(s)

    assert found == RerunRequest(
        id="RR-1",
        procedure_ids=["REV-08", "REV-02"],
        superseded_run_ids=["RUN-REV-02-1", "RUN-REV-08-1"],
        closed_task_ids=[],
        data_version=2,
        report_revision=None,
        requested_by="U-1",
        requested_at=found.requested_at,
    )
    written = s.output_store.commits[commits]
    assert found in written
    assert {(o.id, o.status) for o in written if isinstance(o, Procedure)} == {
        ("REV-08", ProcedureStatus.executing),
        ("REV-02", ProcedureStatus.executing),
    }
    assert EngagementWrite("update", EID, {"phase": Phase.P2_Executing}) in written
    # 新执行: 新数据版本, trigger rerun; 新例外, 新判断只引用当前的 REV-08 例外; 新任务待复核
    for pid in ("REV-08", "REV-02"):
        run = latest(s, ProcedureRun, f"RUN-{pid}-2")
        assert (run.trigger, run.status, run.data_version) == (RunTrigger.rerun, RunStatus.succeeded, 2)
    assert latest(s, AuditException, "X-REV-08-2-R01-1").source_ids == ["SI-2026-0612"]
    assert latest(s, Judgment, "J-REV-02-2-SI-2026-0612").exception_ids == ["X-REV-08-2-R01-1"]
    pending = await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending)
    assert sorted(t.id for t in pending) == [d.task_id for d in SECOND_REVIEWS]
    assert (await s.runner.app.aget_state(CONFIG)).values["phase"] is Phase.P2_Executing
    assert (await overview(s)).stale_procedure_ids == []

    await review(s, SECOND_REVIEWS)

    assert await s.next_nodes() == ("gate_fin",)
    schedule = latest(s, UncorrectedSchedule, "SCHEDULE")
    assert schedule.version == 2
    assert [(i.misstatement_id, i.amount_cents) for i in schedule.items] == [("M-D-R2-0612", 14200000)]
    assert schedule.total_cents == 14200000  # 第 1 轮的错报 M-D-REV02-0612 留作历史, 不再计入
    view = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).gate(EID)
    assert view is not None and (view.target_id, view.target_version) == ("SCHEDULE", 2)
    assert isinstance(view.material, CompletionMaterial)
    assert [m.id for m in view.material.misstatements] == ["M-D-R2-0612"]
    assert view.material.stale_procedure_ids == []
    values = (await s.runner.app.aget_state(CONFIG)).values
    assert sorted(m.id for m in values["misstatements"]) == ["M-D-R2-0612", "M-D-REV02-0612"]  # 旧的仍在
    # 留痕: 一行 decision (键为请求 ID); 阶段回到执行与再进完成各一行, 键带重跑次数, 不与第一次的同键
    assert "RR-1" in trail_keys(s, TrailKind.decision)
    transitions = trail_keys(s, TrailKind.transition)
    for phase in ("P2_Executing", "P3_Completion"):
        assert transitions.count(f"{EID}:transition:engagement:{EID}:{phase}") == 1
        assert transitions.count(f"{EID}:transition:engagement:{EID}:{phase}:1") == 1


async def test_rerun_cash_line_takes_current_csh05_exceptions() -> None:
    """银行流水变了: REV-08, REV-02, CSH-05, CSH-02 过期, 一起重跑 (按依赖分层派发); CSH-02 的新判断只引用当前的
    CSH-05 例外与本轮的 D01; 复核后汇总表合计不变."""
    s = await demo_at_fin_gate()
    data_of(s).record(DataInput.bank_transactions)
    assert (await overview(s)).stale_procedure_ids == ["CSH-02", "CSH-05", "REV-02", "REV-08"]

    found = await rerun(s)

    assert sorted(found.procedure_ids) == ["CSH-02", "CSH-05", "REV-02", "REV-08"]
    pending = sorted(t.id for t in await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending))
    assert pending == ["T-J-CSH-02-2-BA-01-v1", *(d.task_id for d in SECOND_REVIEWS)]
    assert latest(s, Judgment, "J-CSH-02-2-BA-01").exception_ids == ["X-CSH-05-2-B01-1", "X-CSH-02-2-D01-1"]
    await review(s, [*SECOND_REVIEWS, record("T-J-CSH-02-2-BA-01-v1", id="D-R2-CSH02")])
    assert await s.next_nodes() == ("gate_fin",)
    assert latest(s, UncorrectedSchedule, "SCHEDULE").total_cents == 14200000


async def test_rerun_cash_line_while_awaiting_review_drops_open_old_findings() -> None:
    """在等复核处 (CSH-02 的任务待决, CSH-05 的例外尚未归因) 银行流水变了: REV-08, CSH-05, CSH-02 重跑, CSH-02 的
    待决任务一并关闭; 新判断只引用第 2 轮的 CSH-05 例外, 不带上第 1 轮未归因的 X-CSH-05-1-B01-1."""
    s = await start_demo()
    await wait_idle(s.runner, EID)
    assert latest(s, AuditException, "X-CSH-05-1-B01-1").attributed_by is None
    data_of(s).record(DataInput.bank_transactions)
    assert (await overview(s)).stale_procedure_ids == ["CSH-02", "CSH-05", "REV-08"]

    found = await rerun(s)

    assert found.closed_task_ids == ["T-J-CSH-02-1-BA-01-v1"]
    assert latest(s, Judgment, "J-CSH-02-2-BA-01").exception_ids == ["X-CSH-05-2-B01-1", "X-CSH-02-2-D01-1"]
    cash = record("T-J-CSH-02-2-BA-01-v1", id="D-R2-CSH02")
    await review(s, [*REVIEWS[:5], cash])
    assert await s.next_nodes() == ("gate_fin",)
    assert latest(s, AuditException, "X-CSH-05-2-B01-1").attributed_by == "J-CSH-02-2-BA-01"


async def test_rerun_while_awaiting_review_feeds_current_exceptions_to_rev02() -> None:
    """在等复核处重跑 (REV-02 尚未执行): 只重跑 REV-08, 回到等复核; 之后 REV-02 只取当前的 REV-08 例外
    (第 1 轮的 X-REV-08-1-R01-1 已被取代), 全部复核后汇总表合计不变."""
    s = await start_demo()
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("await_events",)
    data_of(s).record(DataInput.sales_invoices)
    assert (await overview(s)).stale_procedure_ids == ["REV-08"]  # REV-02 还没执行

    found = await rerun(s)

    assert (found.procedure_ids, found.superseded_run_ids, found.closed_task_ids) == (["REV-08"], ["RUN-REV-08-1"], [])
    assert await s.next_nodes() == ("await_events",)
    assert latest(s, ProcedureRun, "RUN-REV-08-2").data_version == 2
    await review(s, REVIEWS)
    assert await s.next_nodes() == ("gate_fin",)
    assert latest(s, Judgment, "J-REV-02-1-SI-2026-0612").exception_ids == ["X-REV-08-2-R01-1"]
    assert latest(s, UncorrectedSchedule, "SCHEDULE").total_cents == 14200000
    assert latest(s, AuditException, "X-REV-08-2-R01-1").attributed_by == "J-REV-02-1-SI-2026-0612"
    assert latest(s, AuditException, "X-REV-08-1-R01-1").attributed_by is None  # 旧例外留作历史


async def test_rerun_closes_pending_tasks_of_rerun_programs() -> None:
    """重跑的程序在等复核的任务一并关闭 (decided, 列入请求与留痕), 不能再决定; 新执行出新任务; REV-02 取当前的
    REV-03 判断 (第 2 轮的)."""
    s = await start_numbat()
    await approve_materiality(s)
    await wait_idle(s.runner, EID)
    old = sorted(t.id for t in await s.output_store.latest(EID, ReviewTask, procedure_id="REV-03"))
    assert len(old) == 3
    data_of(s).record(DataInput.contracts)
    assert (await overview(s)).stale_procedure_ids == ["REV-03"]

    found = await rerun(s, ["REV-03"])

    assert found.closed_task_ids == old
    assert all(latest(s, ReviewTask, tid).status is TaskStatus.decided for tid in old)
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    [entry] = [e for e in trail.entries if e.idempotency_key == "RR-1"]
    assert entry.payload["closed_task_ids"] == old and entry.kind is TrailKind.decision
    with pytest.raises(DecisionRejected) as closed:
        await s.submit.submit(EID, REVIEWS[0])
    assert closed.value.code == "TASK_NOT_PENDING"
    new = sorted(t.id for t in await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending))
    assert [t for t in new if "REV-03" in t] == [t.replace("REV-03-1", "REV-03-2") for t in old]
    second_round = [r.model_copy(update={"task_id": r.task_id.replace("REV-03-1", "REV-03-2")}) for r in REVIEWS[:3]]
    await review(s, [*second_round, *REVIEWS[3:]])
    assert await s.next_nodes() == ("gate_fin",)
    assert latest(s, UncorrectedSchedule, "SCHEDULE").total_cents == 14200000


async def test_rerun_at_rpt_gate_redrafts_report_without_reject() -> None:
    """停在 RPT-01 时重跑: 流程回到执行阶段, FIN-01 重新呈报 (旧的通过决定绑旧版本, 不再放行); 通过后 RPT-03 按
    非退回的重拟起草 REPORT-2 (不针对哪个退回决定), 再停在 RPT-01; 阶段每次经过都有留痕."""
    s = await at_rpt_gate()
    data_of(s).record(DataInput.delivery_notes)
    assert (await overview(s)).stale_procedure_ids == ["REV-02", "REV-08"]

    found = await rerun(s)

    assert found.report_revision == 1
    await review(s, SECOND_REVIEWS)
    assert await s.next_nodes() == ("gate_fin",)  # 此前的 FIN-01 决定绑 SCHEDULE v1, 闸门重新呈报
    await s.submit.submit(EID, fin_decision(GateVerdict.approve, id="D-FIN-2", target_version=2))
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_rpt01",), s.runner.run_error(EID)
    report_2 = latest(s, AuditReportVersion, "REPORT-2")
    assert (report_2.revision, report_2.addresses_decision_id) == (2, None)
    view = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).gate(EID)
    assert view is not None and isinstance(view.material, ReportMaterial)
    assert (view.material.report.id, view.material.previous_decisions) == ("REPORT-2", [])
    transitions = trail_keys(s, TrailKind.transition)
    for phase in ("P2_Executing", "P3_Completion", "P4_Reporting"):
        assert transitions.count(f"{EID}:transition:engagement:{EID}:{phase}:1") == 1, phase

    # 签字后锁定, 不能再重跑 (验收 3)
    data_of(s).record(DataInput.sales_invoices)
    await s.submit.submit(EID, rpt_decision(GateVerdict.approve, id="D-RPT-2", target_id="REPORT-2"))
    await wait_idle(s.runner, EID)
    assert (await s.engagement_store.get(EID)).locked  # type: ignore[union-attr]
    assert not (await overview(s)).can_rerun
    await assert_rerun_rejected(s, None, "ENGAGEMENT_LOCKED")


async def test_fin01_lists_stale_programs_not_rerun_and_still_approves() -> None:
    """只重跑 REV-08: 没有新任务, 回到完成阶段出 SCHEDULE v2 再停在 FIN-01; 材料列出过期而未重跑的 REV-02 (依赖的
    REV-08 在它之后重跑过), 仍可通过 (Q5); 旧的 FIN-01 决定绑 v1, 不放行."""
    s = await at_fin_gate()
    data_of(s).record(DataInput.sales_invoices)

    await rerun(s, ["REV-08"])

    assert await s.next_nodes() == ("gate_fin",)
    view = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).gate(EID)
    assert view is not None and isinstance(view.material, CompletionMaterial)
    assert (view.target_version, view.material.stale_procedure_ids) == (2, ["REV-02"])
    with pytest.raises(DecisionRejected) as old:
        await s.submit.submit(EID, fin_decision(GateVerdict.approve))
    assert old.value.code == "VERSION_MISMATCH"
    await s.submit.submit(EID, fin_decision(GateVerdict.approve, id="D-FIN-2", target_version=2))
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_rpt01",)


# ── 拒绝 ──


async def assert_rerun_rejected(
    s: Started, procedure_ids: list[str] | None, code: str, request_id: str = "RR-X"
) -> str:
    """重跑被拒: 错误码对, Store 无新写入, 项目锁已释放, 图不动. 返回错误消息."""
    rows, next_before = list(s.output_store.rows), await s.next_nodes()
    with pytest.raises(DecisionRejected) as e:
        await s.submit.rerun_stale(EID, request_id, procedure_ids, requested_by="U-1")
    assert e.value.code == code, e.value
    assert s.output_store.rows == rows
    assert not s.runner.is_running(EID)
    assert await s.next_nodes() == next_before
    return str(e.value)


async def test_locked_engagement_cannot_rerun() -> None:
    """验收 3: 项目锁定 (签字或拒绝承接) 时不能重跑, 先于其他校验."""
    s = await at_fin_gate()
    data_of(s).record(DataInput.sales_invoices)
    await s.engagement_store.update(EID, locked=True)
    assert not (await overview(s)).can_rerun
    await assert_rerun_rejected(s, None, "ENGAGEMENT_LOCKED")


async def test_busy_engagement_cannot_rerun() -> None:
    s = await at_fin_gate()
    data_of(s).record(DataInput.sales_invoices)
    s.runner.acquire(EID)
    try:
        assert not (await overview(s)).can_rerun
        with pytest.raises(DecisionRejected) as e:
            await s.submit.rerun_stale(EID, "RR-X", None, requested_by="U-1")
        assert e.value.code == "ENGAGEMENT_BUSY"
    finally:
        s.runner.release(EID)


async def test_rerun_only_at_review_or_fin_or_rpt_gate() -> None:
    """位置不对 (PLN-01/03 闸门前, 计划阶段的程序过期也不能在这里重跑) 报 NOT_RERUNNABLE."""
    from tests.test_decisions import at_pln_gate

    s = await at_pln_gate()
    data_of(s).record(DataInput.accounts)
    found = await overview(s)
    assert found.stale_procedure_ids == ["PLN-01", "PLN-07"] and not found.can_rerun
    message = await assert_rerun_rejected(s, None, "NOT_RERUNNABLE")
    assert "等复核, FIN-01 或 RPT-01" in message


async def test_wrong_or_not_stale_programs_rejected() -> None:
    """没有过期程序, 所列程序不过期, 是计划阶段的, 或已在等重跑, 都报 INVALID_VALUE; 请求 ID 用过也报."""
    s = await at_fin_gate()
    assert "没有可重跑的过期程序" in await assert_rerun_rejected(s, None, "INVALID_VALUE")
    data_of(s).record(DataInput.accounts, DataInput.sales_invoices)  # PLN-07, PLN-01 过期但不能重跑
    assert "CSH-05" in await assert_rerun_rejected(s, ["REV-08", "CSH-05"], "INVALID_VALUE")
    assert "PLN-07" in await assert_rerun_rejected(s, ["PLN-07"], "INVALID_VALUE")
    assert "没有可重跑的过期程序" in await assert_rerun_rejected(s, [], "INVALID_VALUE")
    assert "已用过" in await assert_rerun_rejected(s, ["REV-08"], "INVALID_VALUE", request_id="D-REV03-1")


async def test_program_waiting_for_rerun_cannot_be_requested_again() -> None:
    """已有重跑请求而未执行的程序 (executing) 不能再请求: 合同改了, REV-03 与 REV-02 一起重跑; REV-03 第 2 轮等复核时
    REV-02 仍在等它 (executing). 合同再改一次: REV-03 过期能重跑; REV-02 的执行都已被取代, 还没有新执行 (没有当前
    结果, 第 3 轮 R274-15), 不算过期, 单列 REV-02 报 INVALID_VALUE."""
    s = await start_numbat()
    await approve_materiality(s)
    await wait_idle(s.runner, EID)
    await review(s, REVIEWS[:3])  # REV-03 复核完, REV-02 执行, 等它的两条任务
    data_of(s).record(DataInput.contracts)
    assert (await overview(s)).stale_procedure_ids == ["REV-02", "REV-03"]
    await rerun(s)
    assert await s.next_nodes() == ("await_events",)
    assert latest(s, Procedure, "REV-02").status is ProcedureStatus.executing

    data_of(s).record(DataInput.contracts)
    found = await overview(s)
    assert found.stale_procedure_ids == ["REV-03"] and found.can_rerun
    assert [p.id for p in found.procedures if p.rerunnable] == ["REV-03"]
    assert "不在等重跑" in await assert_rerun_rejected(s, ["REV-02"], "INVALID_VALUE")
    second = await rerun(s, None, "RR-2")
    assert second.procedure_ids == ["REV-03"]
    assert second.closed_task_ids == sorted(t.task_id.replace("REV-03-1", "REV-03-2") for t in REVIEWS[:3])


@pytest.mark.parametrize("lost", ["trail", "checkpoint"])
async def test_store_ahead_of_checkpoint_requires_resume_then_recovers(lost: str) -> None:
    """请求写入 Store 后留痕写失败 (检查点也没写), 或留痕写了而检查点写失败: 再提交报 RESUME_REQUIRED; resume 补做
    (以 fan_in 身份写入同样的字段, 补写缺的留痕), 之后与在线相同."""
    s = await at_fin_gate()
    data_of(s).record(DataInput.sales_invoices)
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    app = s.runner.app
    original, record_trail = app.aupdate_state, trail.record

    async def broken_checkpoint(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("检查点写失败")

    async def lose_rerun_trail(entry: Any) -> Any:
        if entry.idempotency_key == "RR-1":
            raise RuntimeError("留痕写失败")
        return await record_trail(entry)

    if lost == "trail":
        trail.record = lose_rerun_trail  # type: ignore[method-assign]
    else:
        app.aupdate_state = broken_checkpoint  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="写失败"):
        await s.submit.rerun_stale(EID, "RR-1", None, requested_by="U-1")
    app.aupdate_state, trail.record = original, record_trail  # type: ignore[method-assign]
    assert await s.output_store.get(EID, RerunRequest, "RR-1") is not None
    assert await s.next_nodes() == ("gate_fin",)
    assert trail_keys(s, TrailKind.decision).count("RR-1") == (0 if lost == "trail" else 1)
    await assert_rerun_rejected(s, None, "RESUME_REQUIRED", request_id="RR-2")

    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)

    assert s.runner.run_error(EID) is None
    assert trail_keys(s, TrailKind.decision).count("RR-1") == 1
    assert await s.next_nodes() == ("await_events",)
    values = (await app.aget_state(CONFIG)).values
    assert [r.id for r in values["rerun_requests"]] == ["RR-1"] and values["phase"] is Phase.P2_Executing
    assert latest(s, ProcedureRun, "RUN-REV-02-2").data_version == 2
    transitions = trail_keys(s, TrailKind.transition)
    assert transitions.count(f"{EID}:transition:engagement:{EID}:P2_Executing:1") == 1
    await review(s, SECOND_REVIEWS)
    assert latest(s, UncorrectedSchedule, "SCHEDULE").total_cents == 14200000


async def test_recover_at_review_wait_brings_closed_tasks_into_checkpoint() -> None:
    """在等复核处重跑 REV-03 (关闭它的 3 个待决任务) 后留痕写失败: resume 补做把关闭的任务 (decided) 一并写进检查点,
    之后只等新任务, 与在线相同."""
    s = await start_numbat()
    await approve_materiality(s)
    await wait_idle(s.runner, EID)
    data_of(s).record(DataInput.contracts)
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    record_trail = trail.record

    async def lose_rerun_trail(entry: Any) -> Any:
        if entry.idempotency_key == "RR-1":
            raise RuntimeError("留痕写失败")
        return await record_trail(entry)

    trail.record = lose_rerun_trail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="留痕写失败"):
        await s.submit.rerun_stale(EID, "RR-1", ["REV-03"], requested_by="U-1")
    trail.record = record_trail  # type: ignore[method-assign]
    old = [d.task_id for d in REVIEWS[:3]]
    tasks = {t.id: t for t in (await s.runner.app.aget_state(CONFIG)).values["review_tasks"]}
    assert all(tasks[tid].status is TaskStatus.pending for tid in old)  # 检查点没跟上

    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)

    assert s.runner.run_error(EID) is None and await s.next_nodes() == ("await_events",)
    tasks = {t.id: t for t in (await s.runner.app.aget_state(CONFIG)).values["review_tasks"]}
    assert all(tasks[tid].status is TaskStatus.decided for tid in old)
    second_round = [r.model_copy(update={"task_id": r.task_id.replace("REV-03-1", "REV-03-2")}) for r in REVIEWS[:3]]
    await review(s, [*second_round, *REVIEWS[3:]])
    assert await s.next_nodes() == ("gate_fin",)


async def test_recover_refuses_rerun_request_when_graph_moved() -> None:
    """Store 有检查点里没有的重跑请求, 图却不在可重跑的位置: 补做报错, 不写检查点."""
    from backend.orchestration.recovery import catch_up_rerun

    s = await at_fin_gate()
    await s.output_store.put(EID, [request()], actor="U-1")
    snapshot = await s.runner.app.aget_state(CONFIG)
    moved = snapshot._replace(next=("gate_p1_risk",))  # 检查点停在 gate_fin; 换成别处的快照交给补做
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None
    with pytest.raises(RuntimeError, match="RR-1 未进检查点"):
        await catch_up_rerun(s.runner.app, s.runner.deps, engagement, moved)
    assert await s.next_nodes() == ("gate_fin",)


# ── RPT-03: 重跑后的重拟 ──


async def test_redraft_after_rerun_has_no_annotations() -> None:
    """上一稿之后有过期重跑 (请求的 report_revision 为上一稿): 第 2 稿照给上一稿, 没有批注, 不针对退回决定;
    提示词说明不是退回, 不列批注."""
    first = report(1)
    state = {
        "report_versions": [first],
        "gate_decisions": [fin01(OpinionType.qualified)],
        # 重跑的是现金线, 不取代报告引用的 REV-02 判断 (重跑 REV-02 后的引用由 test_mock_report_* 两条流程测试守着)
        "rerun_requests": [request(report_revision=1, procedure_ids=["CSH-02"], superseded_run_ids=["RUN-CSH-02-1"])],
    }
    agent, ctx, trail = revising(state, run_no=2)
    previous, annotations, addresses = revision_inputs(ctx, 2)
    assert previous == {
        "opinion": first.sections[ReportSection.opinion],
        "basis_for_opinion": first.sections[ReportSection.basis_for_opinion],
    }
    assert (annotations, addresses) == ([], None)
    [report_2] = (await agent.run(ctx))["report_versions"]
    assert (report_2.id, report_2.revision, report_2.addresses_decision_id) == ("REPORT-2", 2, None)
    [entry] = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    prompt = entry.payload["messages"][0]["content"]
    assert "## Revision 2" in prompt and first.sections[ReportSection.basis_for_opinion] in prompt
    assert "The previous draft was not rejected" in prompt and "Reviewer annotations" not in prompt


async def test_reject_wins_and_rerun_of_other_report_does_not_count() -> None:
    """有针对上一稿的退回决定时按退回; 重跑请求的 report_revision 不是上一稿时不算, 照旧报错."""
    first = report(1)
    _, ctx, _ = revising(
        {"report_versions": [first], "gate_decisions": [REJECT], "rerun_requests": [request(report_revision=1)]}, 2
    )
    assert revision_inputs(ctx, 2)[2] == REJECT.id
    _, ctx, _ = revising({"report_versions": [first], "rerun_requests": [request(report_revision=None)]}, 2)
    with pytest.raises(LookupError, match="也没有其后的过期重跑"):
        revision_inputs(ctx, 2)


def test_phase_transition_key_counts_reruns() -> None:
    """阶段留痕的防重复键: 没有重跑为原样, 第 n 次重跑之后带 :n, 重跑后再经过同一阶段也能留痕."""
    [first] = phase_transition(EID, Phase.P2_Executing, Phase.P3_Completion, "FIN-04")
    [again] = phase_transition(EID, Phase.P2_Executing, Phase.P3_Completion, "FIN-04", reruns=2)
    assert first.idempotency_key == f"{EID}:transition:engagement:{EID}:P3_Completion"
    assert again.idempotency_key == f"{EID}:transition:engagement:{EID}:P3_Completion:2"
    assert phase_transition(EID, Phase.P2_Executing, Phase.P2_Executing, reruns=1) == []


# ── HTTP ──


async def test_rerun_api_returns_request_id_and_maps_errors() -> None:
    """POST /engagements/{eid}/procedures/rerun: 202 带请求 ID; 不在可重跑的位置 409 NOT_RERUNNABLE, 程序不对 422."""
    s = await at_fin_gate()
    data_of(s).record(DataInput.sales_invoices)
    async with api_client(s.runner) as client:
        wrong = await client.post(f"/api/v1/engagements/{EID}/procedures/rerun", json={"procedure_ids": ["CSH-05"]})
        assert (wrong.status_code, wrong.json()["detail"]["code"]) == (422, "INVALID_VALUE")
        accepted = await client.post(f"/api/v1/engagements/{EID}/procedures/rerun", json={"procedure_ids": ["REV-08"]})
        assert accepted.status_code == 202
        request_id = accepted.json()["id"]
        assert request_id.startswith("RR-")
        await wait_idle(s.runner, EID)
        stored = await s.output_store.get(EID, RerunRequest, request_id)
        assert stored is not None and stored.procedure_ids == ["REV-08"]
        detail = (await client.get(f"/api/v1/engagements/{EID}/procedures/REV-08")).json()
        assert (detail["stale"], detail["rerunnable"], detail["superseded_run_ids"]) == (False, False, ["RUN-REV-08-1"])
        rev02 = (await client.get(f"/api/v1/engagements/{EID}/procedures/REV-02")).json()
        assert (rev02["stale"], rev02["rerunnable"], rev02["superseded_run_ids"]) == (True, True, [])  # REV-08 重跑过
        assert await s.next_nodes() == ("gate_fin",)  # REV-02 没重跑, 没有新任务, 直接回到完成阶段
        rest = await client.post(f"/api/v1/engagements/{EID}/procedures/rerun", json={})
        assert rest.status_code == 202
        await wait_idle(s.runner, EID)
        later = await s.output_store.get(EID, RerunRequest, rest.json()["id"])
        assert later is not None and later.procedure_ids == ["REV-02"]
    s2 = await start_numbat()  # 停在 ACC-01 之后的 PLN-01/03 闸门前
    async with api_client(s2.runner) as client:
        response = await client.post(f"/api/v1/engagements/{EID}/procedures/rerun", json={})
        assert (response.status_code, response.json()["detail"]["code"]) == (409, "NOT_RERUNNABLE")


# ── 第 1 轮审核补的用例 ──


def second_rev03() -> list[Any]:
    """REV-03 第 2 轮三条任务的 confirm (决定 ID 另取, 第 1 轮的已用过)."""
    return [
        r.model_copy(update={"task_id": r.task_id.replace("REV-03-1", "REV-03-2"), "id": f"{r.id}-R2"})
        for r in REVIEWS[:3]
    ]


def submitted(s: Started) -> int:
    channel = s.runner.deps.confirmations
    assert isinstance(channel, FakeConfirmationChannel)
    return len(channel.submits)


def versions_of(s: Started, model: type[Any], oid: str) -> list[int]:
    return [r.version for r in s.output_store.rows if isinstance(r, model) and r.id == oid]


async def review_pending(s: Started, prefix: str) -> None:
    """按任务 ID 顺序把待决任务都 confirm (决定 ID 带 prefix)."""
    pending = sorted(t.id for t in await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending))
    await review(s, [record(task_id, id=f"{prefix}-{n}") for n, task_id in enumerate(pending, start=1)])


async def test_rerun_csh04_reuses_stored_confirmations_and_reaches_fin01() -> None:
    """R274-01: 银行账户表变了, CSH-04, DOC-06, CSH-05, CSH-02 一起重跑. CSH-04 对已发过函的账户沿用 Store 中函证的
    最新版本 (不再发函, 不退回第 1 版, 也不出新版本), DOC-06, CSH-02 在其上各出一版; 复核后回到 FIN-01."""
    s = await demo_at_fin_gate()
    sent = submitted(s)
    before = {cid: latest(s, Confirmation, cid).version for cid in ("C-BA-01", "C-BA-02")}
    data_of(s).record(DataInput.bank_accounts)
    assert (await overview(s)).stale_procedure_ids == ["CSH-02", "CSH-04", "CSH-05", "DOC-06"]

    found = await rerun(s)

    assert sorted(found.procedure_ids) == ["CSH-02", "CSH-04", "CSH-05", "DOC-06"]
    for pid in found.procedure_ids:
        assert latest(s, ProcedureRun, f"RUN-{pid}-2").status is RunStatus.succeeded, pid
    assert submitted(s) == sent  # 没有再发函
    for cid, version in before.items():
        assert versions_of(s, Confirmation, cid).count(1) == 1  # 第 1 版只写过一次
        assert latest(s, Confirmation, cid).version == version + 2  # DOC-06, CSH-02 各一版; CSH-04 不出新版本
    await review_pending(s, "D-BA")
    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)
    assert latest(s, UncorrectedSchedule, "SCHEDULE").total_cents == 14200000


BA03 = BankAccount(
    id="BA-03", bank_name="Wattle Bank Limited", bsb="999-303", account_no="34567803", gl_account_code="1030"
)
BA03_BOOK = 12345600  # 合成: 新账户总账科目 1030 的期末余额, 回函与之相符
BA03_REPLY = "DOC-BA03REPLY01"  # 合成: 新账户回函的文件 ID


def ba03_reply_fixture(root: Path) -> None:
    """合成的 DOC-06 固定输出: 新账户 BA-03 的回函抽取, 证据指向预先登记的回函 BA03_REPLY."""
    base = json.loads((get_settings().LLM_FIXTURES_ROOT / "DOC-06" / "BA-02.json").read_text(encoding="utf-8"))
    document_id = BA03_REPLY
    base |= {"confirming_party": BA03.bank_name, "account_no": BA03.account_no, "confirmed_balance_cents": BA03_BOOK}
    base["evidence"] = [{**p, "source_id": document_id} for p in base["evidence"]]
    (root / "DOC-06").mkdir(parents=True)
    (root / "DOC-06" / "BA-03.json").write_text(json.dumps(base), encoding="utf-8")


async def started_with(llm: Any) -> tuple[Started, FakeFinancialStore]:
    """真实 P2 Agent 的 Numbat (同 start_demo), 主模型换成 llm (复核模型仍为主固定输出), PLN-01/03 通过后等后台停下;
    另返回假财务存储, 供用例登记新文件与明细行."""
    financial = FakeFinancialStore(replies(), dict(NUMBAT_RECORDS), trial_balance=NUMBAT_TRIAL_BALANCE)
    runner, store = make_runner(
        engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]),
        agents=real_agents,
        financial_store=financial,
        llm=llm,
        review_llm=MockLLMClient(get_settings().LLM_FIXTURES_ROOT),
        features=NUMBAT_FEATURES,
    )
    s = Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]
    await runner.start(EID)
    await wait_idle(runner, EID)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    return s, financial


async def layered_at_fin_gate(root: Path, wrap: Any = None) -> tuple[Started, FakeFinancialStore]:
    """同 demo_at_fin_gate, 但大模型先找 root 下的固定输出 (LayeredMockLLM), 找不到再用主固定输出 (wrap 给了时再包
    一层, 如让某次调用失败); 另返回假财务存储."""
    llm: Any = LayeredMockLLM(root, get_settings().LLM_FIXTURES_ROOT)
    s, financial = await started_with(wrap(llm) if wrap is not None else llm)
    await review(s, REVIEWS)
    assert await s.next_nodes() == ("gate_fin",)
    return s, financial


async def test_rerun_csh04_sends_only_to_new_bank_account(tmp_path: Path) -> None:
    """R274-01: 重新导入的银行账户表多了 BA-03: CSH-04 重跑只对它发函 (出第 1 版), 已发过函的沿用 Store 中的函证;
    DOC-06 抽取新回函, CSH-02 比对相符 (没有 D01), 复核后回到 FIN-01."""
    ba03_reply_fixture(tmp_path)
    s, financial = await layered_at_fin_gate(tmp_path)
    sent = submitted(s)
    # 新账户的回函先按假函证平台的内容登记 (同 replies() 对 Numbat 两份回函的做法), CSH-04 存档时按哈希认出
    content = f"{BA03.account_no} reply".encode()
    financial.document_refs.append(
        replies()[1].model_copy(
            update={
                "id": BA03_REPLY,
                "file_name": f"{BA03.account_no}.pdf",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    )
    financial.records[("bank_account", BA03.id)] = BA03.model_dump()
    financial.trial_balance_rows.append(TrialBalanceRow(period="current", account_code="1030", balance_cents=BA03_BOOK))
    data_of(s).record(DataInput.bank_accounts, DataInput.trial_balance)

    found = await rerun(s)

    assert {"CSH-04", "DOC-06", "CSH-05", "CSH-02"} <= set(found.procedure_ids)
    channel = s.runner.deps.confirmations
    assert isinstance(channel, FakeConfirmationChannel)
    assert [request.bank_account_id for _, request in channel.submits[sent:]] == ["BA-03"]
    assert versions_of(s, Confirmation, "C-BA-03") == [1, 2, 3]  # CSH-04, DOC-06, CSH-02
    ba03 = latest(s, Confirmation, "C-BA-03")
    assert (ba03.confirmed_balance_cents, ba03.difference_cents) == (BA03_BOOK, 0)
    assert not [x for x in s.output_store.rows if isinstance(x, AuditException) and "BA-03" in x.source_ids]
    await review_pending(s, "D-BA3")
    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)


@pytest.mark.parametrize("where", ["fin", "review"])
async def test_rerun_csh02_uses_confirmation_edited_while_stopped(where: str) -> None:
    """R274-02: 停在 FIN-01 或等复核时审计师改回函余额 (函证新版本只写 Store), CSH-02 过期并重跑: 新执行用的是改后的
    函证, 新 D01 金额随之变, 函证在审计师的版本之上再出一版 (不撞号); R274-03: 新判断仍引用 CSH-05 的例外."""
    if where == "fin":
        s = await demo_at_fin_gate()
    else:
        s = await start_demo()
        await wait_idle(s.runner, EID)
        assert await s.next_nodes() == ("await_events",)
    service = DataService(s.runner.deps, s.runner)
    before = latest(s, Confirmation, "C-BA-01")
    d01 = latest(s, AuditException, "X-CSH-02-1-D01-1")
    edit = ExtractionEdit(
        field="confirmed_balance_cents", value=before.confirmed_balance_cents + 1000, reason="按回函原文更正"
    )
    edited = await service.edit_confirmation(EID, "C-BA-01", edit, "U-1")
    assert edited.version == before.version + 1
    assert (await overview(s)).stale_procedure_ids == ["CSH-02"]

    await rerun(s)

    new = latest(s, AuditException, "X-CSH-02-2-D01-1")
    assert new.amount_cents == d01.amount_cents + 1000
    after = latest(s, Confirmation, "C-BA-01")
    assert (after.version, after.confirmed_balance_cents, after.difference_cents) == (
        edited.version + 1,
        edited.confirmed_balance_cents,
        d01.amount_cents + 1000,
    )
    values = (await s.runner.app.aget_state(CONFIG)).values
    assert next(c for c in values["confirmations"] if c.id == "C-BA-01") == after
    assert latest(s, Judgment, "J-CSH-02-2-BA-01").exception_ids == ["X-CSH-05-1-B01-1", "X-CSH-02-2-D01-1"]


async def test_downstream_rerun_reclaims_upstream_exception_from_superseded_judgment() -> None:
    """R274-03: 合同变了, 只重跑 REV-03, REV-02 (REV-08 不重跑): X-REV-08-1-R01-1 原由已取代的 J-REV-02-1 归因, 当前结果
    按未归因给出, 新判断引用它; 复核认定错报后由新判断归因, 汇总表合计不变."""
    s = await demo_at_fin_gate()
    data_of(s).record(DataInput.contracts)
    assert (await overview(s)).stale_procedure_ids == ["REV-02", "REV-03"]
    await rerun(s)
    await review(s, second_rev03())
    assert latest(s, Judgment, "J-REV-02-2-SI-2026-0612").exception_ids == ["X-REV-08-1-R01-1"]
    await review(s, SECOND_REVIEWS)
    assert await s.next_nodes() == ("gate_fin",)
    reclaimed = latest(s, AuditException, "X-REV-08-1-R01-1")
    assert (reclaimed.version, reclaimed.attributed_by) == (3, "J-REV-02-2-SI-2026-0612")
    assert latest(s, UncorrectedSchedule, "SCHEDULE").total_cents == 14200000


async def test_subject_reached_only_through_upstream_exception_is_judged_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """R274-03: S01 不选 SI-2026-0612 时, 它只经 REV-08 的例外进入 REV-02. 合同变动后只重跑 REV-03, REV-02: 被取代的
    判断做的归因失效, 这张发票照样再判断, 错报照样计入, 汇总表不少计."""
    only_0618 = [row for row in NUMBAT_S01_ROWS if row["id"] != "SI-2026-0612"]
    monkeypatch.setitem(NUMBAT_RULES, "REV-02", lambda run: RuleResult(exceptions=[], rows={"S01": only_0618}))
    s = await demo_at_fin_gate()
    assert latest(s, UncorrectedSchedule, "SCHEDULE").total_cents == 14200000
    data_of(s).record(DataInput.contracts)
    await rerun(s)
    await review(s, [*second_rev03(), *SECOND_REVIEWS])
    assert await s.next_nodes() == ("gate_fin",)
    assert latest(s, Judgment, "J-REV-02-2-SI-2026-0612").exception_ids == ["X-REV-08-1-R01-1"]
    schedule = latest(s, UncorrectedSchedule, "SCHEDULE")
    assert [(i.misstatement_id, i.amount_cents) for i in schedule.items] == [("M-D-R2-0612", 14200000)]


class FailFirstLLM:
    """REV-02 第 1 次执行对 SI-2026-0618 的调用一律失败 (单对象 failed 任务), 其余照固定输出."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def complete(self, req: Any, output_model: Any) -> Any:
        if (req.run_id, req.fixture_key) == ("RUN-REV-02-1", "SI-2026-0618"):
            return LLMResponse[output_model](  # type: ignore[valid-type]
                output=None, raw="", model="mock", input_tokens=0, output_tokens=0, error="fixture_not_found"
            )
        return await self.inner.complete(req, output_model)


async def test_superseded_runs_include_earlier_partial_rerun() -> None:
    """R274-04: REV-02 先因 SI-2026-0618 失败按 R21 部分重跑过 (两次执行, 错报出自第 1 次), 再过期重跑: 两次执行都
    被取代, 汇总表只计第 3 次的错报, 不重复计数."""
    main = MockLLMClient(get_settings().LLM_FIXTURES_ROOT)
    financial = FakeFinancialStore(replies(), dict(NUMBAT_RECORDS), trial_balance=NUMBAT_TRIAL_BALANCE)
    runner, store = make_runner(
        engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]),
        agents=real_agents,
        financial_store=financial,
        llm=FailFirstLLM(main),
        review_llm=main,
        features=NUMBAT_FEATURES,
    )
    s = Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]
    await runner.start(EID)
    await wait_idle(runner, EID)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    await review(s, REVIEWS[:3])
    [failed] = [t for t in await store.latest(EID, ReviewTask, status=TaskStatus.pending) if t.kind is TaskKind.failed]
    await review(s, [record(failed.id, Decision.rerun, id="D-R21")])
    await review(s, [REVIEWS[3], record("T-J-REV-02-2-SI-2026-0618-v1", id="D-0618-2"), REVIEWS[5]])
    assert await s.next_nodes() == ("gate_fin",)
    assert latest(s, UncorrectedSchedule, "SCHEDULE").total_cents == 14200000
    data_of(s).record(DataInput.sales_invoices)

    found = await rerun(s)

    assert found.superseded_run_ids == ["RUN-REV-02-1", "RUN-REV-02-2", "RUN-REV-08-1"]
    third = [
        record(
            "T-J-REV-02-3-SI-2026-0612-v1", Decision.misstatement, id="D-R3", misstatement_draft=NUMBAT_CUTOFF_DRAFT
        ),
        record("T-J-REV-02-3-SI-2026-0618-v1", id="D-R3-0618"),
    ]
    await review(s, third)
    assert await s.next_nodes() == ("gate_fin",)
    schedule = latest(s, UncorrectedSchedule, "SCHEDULE")
    assert [(i.misstatement_id, i.amount_cents) for i in schedule.items] == [("M-D-R3", 14200000)]


async def test_rerun_request_id_cannot_be_reused_by_other_decisions() -> None:
    """R274-04: 重跑请求与决定同在 decisions 表, 用过的重跑请求 ID 再交复核决定报 INVALID_VALUE."""
    s = await demo_at_fin_gate()
    data_of(s).record(DataInput.sales_invoices)
    await rerun(s, request_id="RR-1")
    with pytest.raises(DecisionRejected) as reused:
        await s.submit.submit(EID, SECOND_REVIEWS[1].model_copy(update={"id": "RR-1"}))
    assert reused.value.code == "INVALID_VALUE" and "已用过" in str(reused.value)


async def test_review_wait_with_error_is_not_a_rerun_point() -> None:
    """R274-04: 等复核的节点上次恢复时出错 (交回了 Store 里没有的决定) 时不算可重跑的位置: 重跑报 NOT_RERUNNABLE,
    总览 can_rerun 为假."""
    s = await start_demo()
    await wait_idle(s.runner, EID)
    with pytest.raises(LookupError):
        await s.runner.app.ainvoke(Command(resume="D-NOPE"), CONFIG)
    data_of(s).record(DataInput.sales_invoices)
    found = await overview(s)
    assert found.stale_procedure_ids == ["REV-08"] and not found.can_rerun
    await assert_rerun_rejected(s, None, "NOT_RERUNNABLE")


def test_migration_backfill_applies_table_changes() -> None:
    """R274-05: 补记迁移 (0008) 按 data_imports 的表种类套 table_changes (导入审批日志另记总账分录), 查不到种类的
    版本记全部各类."""
    path = Path(__file__).resolve().parents[1] / "src/backend/migrations/versions/0008_data_changes.py"
    spec = importlib.util.spec_from_file_location("migration_0008", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = module.backfill_rows("E-1", 3, {2: ["approval_log"], 3: ["sales_invoices", "accounts"]})
    by_version = {v: [r["input"] for r in rows if r["version"] == v] for v in (1, 2, 3)}
    assert by_version == {
        1: [i.value for i in DataInput],
        2: ["journal_entries", "approval_log"],
        3: ["accounts", "sales_invoices"],
    }
    assert {r["eid"] for r in rows} == {"E-1"}


async def test_uploaded_contract_records_contracts_change_but_reply_copy_does_not() -> None:
    """R274-06: 程序执行之外写进的合同新版本 (上传后 A3 抽取) 同审计师修改合同, 记一次数据变动 contracts; 回函副本的
    抽取只留查看, 不进函证, 不记."""
    _, service, financial, _, data = upload_setup()
    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    await drain(service)
    assert financial.saved_contracts["CON-001"].version == 1
    assert [(c.data_version, c.input) for c in await data.changes(EID)] == [(1, DataInput.contracts)]
    await service.upload_documents(EID, [("12345601.pdf", b"%PDF reply")], actor="U-1")
    await drain(service)
    assert [(c.data_version, c.input) for c in await data.changes(EID)] == [(1, DataInput.contracts)]


async def test_rpt03_validation_ignores_superseded_judgments() -> None:
    """R274-07: RPT-03 的校验只认当前结果的判断: 引用的判断所属的执行被过期重跑取代时, 未通过引用校验."""
    agent, ctx, _ = concierge_setup()
    superseded = request(procedure_ids=["REV-02"], superseded_run_ids=["RUN-REV-02-1"])
    ctx = replace(ctx, state=ctx.state | {"rerun_requests": [superseded]})
    result = await handed_to_human(agent, ctx)
    assert "未通过 reference 校验" in result["failure_reason"]


def test_reads_check_sees_comma_joined_tables(tmp_path: Path) -> None:
    """R274-08: 加载校验按账表名整词扫描规则 SQL, 逗号连接的表也认得出: REV-08 的 R03 改成逗号连接银行流水 (只有
    这条规则读它), reads 漏了 bank_transactions 时报错."""
    root = tmp_path / "knowledge"
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    path = root / "procedures" / "REV-08.yaml"
    text = path.read_text(encoding="utf-8")
    join = "FROM sales_invoices i JOIN bank_transactions t ON t.engagement_id=i.engagement_id"
    assert text.count(join) == 1
    text = text.replace(join, "FROM sales_invoices i, bank_transactions t WHERE t.engagement_id=i.engagement_id")
    text = text.replace(
        "reads: [sales_invoices, delivery_notes, bank_transactions]", "reads: [sales_invoices, delivery_notes]"
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="REV-08 读 bank_transactions"):
        ProcedureLibrary(root)


async def test_recovered_rerun_reads_edited_confirmation() -> None:
    """R274-02: 改回函后的重跑请求写入 Store 而留痕与检查点都没跟上: resume 补做时同样读回 Store 中的函证, CSH-02 用改后
    的余额."""
    s = await demo_at_fin_gate()
    before = latest(s, Confirmation, "C-BA-01")
    d01 = latest(s, AuditException, "X-CSH-02-1-D01-1")
    edit = ExtractionEdit(
        field="confirmed_balance_cents", value=before.confirmed_balance_cents + 1000, reason="按回函原文更正"
    )
    await DataService(s.runner.deps, s.runner).edit_confirmation(EID, "C-BA-01", edit, "U-1")
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    record_trail = trail.record

    async def lose_rerun_trail(entry: Any) -> Any:
        if entry.idempotency_key == "RR-1":
            raise RuntimeError("留痕写失败")
        return await record_trail(entry)

    trail.record = lose_rerun_trail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="留痕写失败"):
        await s.submit.rerun_stale(EID, "RR-1", None, requested_by="U-1")
    trail.record = record_trail  # type: ignore[method-assign]

    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)

    assert s.runner.run_error(EID) is None
    assert latest(s, AuditException, "X-CSH-02-2-D01-1").amount_cents == d01.amount_cents + 1000


# ── 第 1 轮后补定: 审计师改过的回函沿用, 原件换了才重新抽取 ──


def keep_entries(s: Started) -> list[Any]:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e for e in trail.entries if e.kind is TrailKind.data and e.payload.get("action") == "keep_edit"]


def llm_runs(s: Started) -> list[str | None]:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e.run_id for e in trail.entries if e.kind is TrailKind.llm_call]


async def edit_ba01_balance(s: Started, delta: int) -> Confirmation:
    """停着时审计师把 C-BA-01 的回函余额改大 delta 分 (按回函原文更正)."""
    before = latest(s, Confirmation, "C-BA-01")
    edit = ExtractionEdit(
        field="confirmed_balance_cents", value=before.confirmed_balance_cents + delta, reason="按回函原文更正"
    )
    return await DataService(s.runner.deps, s.runner).edit_confirmation(EID, "C-BA-01", edit, "U-1")


async def test_doc06_rerun_keeps_auditor_edited_reply() -> None:
    """第 1 轮后补定: 停在 FIN-01 时审计师改了 C-BA-01 的回函余额, 之后银行账户表变了, CSH-04, DOC-06, CSH-05,
    CSH-02 一起重跑. 回函原件没变: DOC-06 对 C-BA-01 沿用 Store 中审计师那一版 (不调大模型, 不出新版本, 留痕写明
    沿用审计师修改), CSH-02 按审计师的值比对; 没改过的 C-BA-02 照常重新抽取. 复核后回到 FIN-01."""
    s = await demo_at_fin_gate()
    ba02 = latest(s, Confirmation, "C-BA-02").version
    d01 = latest(s, AuditException, "X-CSH-02-1-D01-1")
    edited = await edit_ba01_balance(s, 1000)
    data_of(s).record(DataInput.bank_accounts)
    calls = len(llm_runs(s))

    found = await rerun(s)

    assert {"CSH-04", "DOC-06", "CSH-02"} <= set(found.procedure_ids)
    assert latest(s, ProcedureRun, "RUN-DOC-06-2").status is RunStatus.succeeded
    assert versions_of(s, Confirmation, "C-BA-01")[-2:] == [edited.version, edited.version + 1]  # 只有 CSH-02 出一版
    after = latest(s, Confirmation, "C-BA-01")
    assert (after.confirmed_balance_cents, after.difference_cents) == (
        edited.confirmed_balance_cents,
        d01.amount_cents + 1000,
    )
    assert latest(s, AuditException, "X-CSH-02-2-D01-1").amount_cents == d01.amount_cents + 1000
    assert latest(s, Confirmation, "C-BA-02").version == ba02 + 2  # DOC-06, CSH-02 各一版
    assert llm_runs(s)[calls:].count("RUN-DOC-06-2") == 1  # 只为 C-BA-02 调了大模型
    kept = keep_entries(s)
    assert [(e.run_id, e.payload["confirmation_id"], e.payload["version"]) for e in kept] == [
        ("RUN-DOC-06-2", "C-BA-01", edited.version)
    ]
    assert "沿用审计师修改" in kept[0].summary
    await review_pending(s, "D-KE")
    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)


async def test_doc06_rerun_extracts_replaced_reply(tmp_path: Path) -> None:
    """第 1 轮后补定: 审计师改过 C-BA-01 之后银行给了新回函 (原件换了), 重跑时 DOC-06 按新原件重新抽取, 新版本取代审计师
    那一版, CSH-02 按新抽取的值比对, 不写沿用留痕. 本版没有换回函原件的入口 (CSH-04 每个账户只发一次函, 上传的回函
    副本不关联函证), 这里在 Store 写一版指向新存档回函的函证代替; CSH-04 重跑沿用它, 回函取新文件."""
    s, financial = await layered_at_fin_gate(tmp_path)
    d01 = latest(s, AuditException, "X-CSH-02-1-D01-1")
    edited = await edit_ba01_balance(s, 1000)
    old = next(d for d in replies() if d.id == edited.reply_document_id)
    content = b"12345601 reply, second letter"  # 合成: 银行第二封回函的内容
    new = old.model_copy(
        update={
            "id": "DOC-BA01REPLY02",
            "file_name": "12345601-second.pdf",
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    )
    financial.document_refs.append(new)
    replaced = edited.model_copy(update={"version": edited.version + 1, "reply_document_id": new.id})
    await s.output_store.put(EID, [replaced], actor="A5")
    # 合成的 DOC-06 固定输出: 新回函的余额比审计师改的值再多 4000 分, 证据指向新回函
    fixture = json.loads((get_settings().LLM_FIXTURES_ROOT / "DOC-06" / "BA-01.json").read_text(encoding="utf-8"))
    fixture["confirmed_balance_cents"] = edited.confirmed_balance_cents + 4000
    fixture["evidence"] = [{**p, "source_id": new.id} for p in fixture["evidence"]]
    (tmp_path / "DOC-06").mkdir()
    (tmp_path / "DOC-06" / "BA-01.json").write_text(json.dumps(fixture), encoding="utf-8")
    data_of(s).record(DataInput.bank_accounts)

    found = await rerun(s)

    assert {"CSH-04", "DOC-06", "CSH-02"} <= set(found.procedure_ids)
    assert versions_of(s, Confirmation, "C-BA-01")[-4:] == [edited.version + n for n in range(4)]  # DOC-06, CSH-02
    after = latest(s, Confirmation, "C-BA-01")
    assert (after.reply_document_id, after.confirmed_balance_cents, after.difference_cents) == (
        new.id,
        edited.confirmed_balance_cents + 4000,
        d01.amount_cents + 5000,
    )
    assert {p.source_id for p in after.evidence} == {new.id}
    assert keep_entries(s) == []
    values = (await s.runner.app.aget_state(CONFIG)).values
    assert new.id in {d.id for d in values["documents"]}
    await review_pending(s, "D-NR")
    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)


# ── 第 2 轮审核补的用例 ──


class FailFor:
    """主模型对某次执行的某个对象 (run_id, fixture_key) 的调用一律失败, 其余照内层; calls 记下每次调用."""

    def __init__(self, inner: Any, run_id: str, fixture_key: str) -> None:
        self.inner = inner
        self.target = (run_id, fixture_key)
        self.calls: list[tuple[str | None, str | None]] = []

    async def complete(self, req: Any, output_model: Any) -> Any:
        self.calls.append((req.run_id, req.fixture_key))
        if (req.run_id, req.fixture_key) == self.target:
            return LLMResponse[output_model](  # type: ignore[valid-type]
                output=None, raw="", model="mock", input_tokens=0, output_tokens=0, error="fixture_not_found"
            )
        return await self.inner.complete(req, output_model)


@pytest.mark.parametrize(
    ("field", "value"),
    [("confirming_party", "Harbourline Bank Ltd (corrected)"), ("confirmed_balance_cents", 120840000)],
)
async def test_doc06_extracts_when_edit_came_before_any_extraction(field: str, value: Any) -> None:
    """R274-09: DOC-06 第 1 次对 BA-01 抽取失败 (函证仍是 CSH-04 的第 1 版, 没有余额与证据), 审计师停着时改了这份函证,
    再对失败任务提交 rerun (R21): 修改之前没有成功抽取过, 不沿用, 照常调大模型抽取; 函证带上抽取的余额, 日期与指向
    回函的证据, 不写沿用留痕, CSH-02 照常比对."""
    llm = FailFor(MockLLMClient(get_settings().LLM_FIXTURES_ROOT), "RUN-DOC-06-1", "BA-01")
    s, _ = await started_with(llm)
    assert await s.next_nodes() == ("await_events",), s.runner.run_error(EID)
    pending = await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending)
    [failed] = [t for t in pending if t.kind is TaskKind.failed and t.procedure_id == "DOC-06"]
    edit = ExtractionEdit(field=field, value=value, reason="按回函原文更正")
    edited = await DataService(s.runner.deps, s.runner).edit_confirmation(EID, "C-BA-01", edit, "U-1")
    calls = len(llm.calls)

    await review(s, [record(failed.id, Decision.rerun, id="D-DOC06-R21")])

    assert ("RUN-DOC-06-2", "BA-01") in llm.calls[calls:]
    extracted = next(c for c in s.output_store.rows if isinstance(c, Confirmation) and c.version == edited.version + 1)
    assert extracted.id == "C-BA-01" and extracted.balance_date is not None
    assert extracted.confirmed_balance_cents == 120840000
    assert {p.source_id for p in extracted.evidence} == {extracted.reply_document_id}
    assert keep_entries(s) == []
    assert latest(s, ProcedureRun, "RUN-CSH-02-1").status is RunStatus.succeeded


async def test_doc06_keeps_latest_edit_after_original_replaced_and_edited_again(tmp_path: Path) -> None:
    """R274-12 (N10): 审计师改 → 换了原件 → 重跑按新原件抽取 → 审计师再改 → 再重跑: 沿用的是最后一次修改 (它之前
    对新原件抽取过, 之后原件没换), 不调大模型, CSH-02 按第二次修改的值比对."""
    s, financial = await layered_at_fin_gate(tmp_path)
    d01 = latest(s, AuditException, "X-CSH-02-1-D01-1")
    first = await edit_ba01_balance(s, 1000)
    content = b"12345601 reply, second letter"  # 合成: 银行第二封回函的内容
    new = next(d for d in replies() if d.id == first.reply_document_id).model_copy(
        update={
            "id": "DOC-BA01REPLY02",
            "file_name": "12345601-second.pdf",
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    )
    financial.document_refs.append(new)
    await s.output_store.put(
        EID, [first.model_copy(update={"version": first.version + 1, "reply_document_id": new.id})], actor="A5"
    )
    fixture = json.loads((get_settings().LLM_FIXTURES_ROOT / "DOC-06" / "BA-01.json").read_text(encoding="utf-8"))
    fixture["confirmed_balance_cents"] = first.confirmed_balance_cents + 4000  # 合成: 新回函的余额
    fixture["evidence"] = [{**p, "source_id": new.id} for p in fixture["evidence"]]
    (tmp_path / "DOC-06").mkdir()
    (tmp_path / "DOC-06" / "BA-01.json").write_text(json.dumps(fixture), encoding="utf-8")
    data_of(s).record(DataInput.bank_accounts)
    await rerun(s)
    await review_pending(s, "D-N10A")
    assert latest(s, Confirmation, "C-BA-01").confirmed_balance_cents == first.confirmed_balance_cents + 4000
    second = await edit_ba01_balance(s, 3000)
    data_of(s).record(DataInput.bank_accounts)
    calls = len(llm_runs(s))

    await rerun(s, request_id="RR-2")

    assert llm_runs(s)[calls:].count("RUN-DOC-06-3") == 1  # 只为 C-BA-02 调了大模型
    after = latest(s, Confirmation, "C-BA-01")
    assert (after.version, after.confirmed_balance_cents) == (second.version + 1, second.confirmed_balance_cents)
    assert after.difference_cents == d01.amount_cents + 1000 + 4000 + 3000
    assert [e.payload["version"] for e in keep_entries(s)] == [second.version]


async def test_unattributed_after_downstream_rerun_clears_superseded_attribution(tmp_path: Path) -> None:
    """R274-12 (N7): 合同变了只重跑 REV-03, REV-02, 新的 REV-02 对 SI-2026-0612 判断失败 (hybrid 有例外, 转
    unattributed): X-REV-08-1-R01-1 原由被取代的 J-REV-02-1 归因, 出的新版本为 unattributed, 不再记着旧判断."""
    s, _ = await layered_at_fin_gate(tmp_path, wrap=lambda llm: FailFor(llm, "RUN-REV-02-2", "SI-2026-0612"))
    data_of(s).record(DataInput.contracts)
    await rerun(s)
    await review(s, second_rev03())

    marked = latest(s, AuditException, "X-REV-08-1-R01-1")
    assert (marked.version, marked.disposition.value, marked.attributed_by) == (3, "unattributed", None)
    tasks = await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending)
    assert [(t.kind, t.target_id) for t in tasks if t.procedure_id == "REV-02" and t.subject_id == "SI-2026-0612"] == [
        (TaskKind.unattributed, "X-REV-08-1-R01-1")
    ]


async def test_uploaded_contract_and_its_change_share_one_transaction() -> None:
    """R274-12 (N14): 上传后抽出的合同与它的 contracts 数据变动在同一个审计产出事务里写 (一起提交或一起丢弃)."""
    _, service, financial, _, data = upload_setup()
    store = service.deps.output_store
    seen: list[tuple[str, object]] = []
    save, bump = financial.save_contract, data.bump_version

    async def save_contract(eid: str, contract: Any, **kwargs: Any) -> None:
        seen.append(("contract", store.active_txn()))  # type: ignore[attr-defined]
        await save(eid, contract, **kwargs)

    async def bump_version(eid: str, input: DataInput) -> int:
        seen.append(("change", store.active_txn()))  # type: ignore[attr-defined]
        return await bump(eid, input)

    financial.save_contract = save_contract  # type: ignore[method-assign]
    data.bump_version = bump_version  # type: ignore[method-assign]
    await service.upload_documents(EID, [("CON-001.pdf", b"%PDF CON-001")], actor="U-1")
    await drain(service)
    assert [kind for kind, _ in seen] == ["contract", "change"]
    assert seen[0][1] is not None and seen[0][1] is seen[1][1]


async def test_removed_bank_account_confirmation_is_history_and_reaches_fin01(tmp_path: Path) -> None:
    """R274-10: 重新导入的银行账户表少了已发过函的 BA-02: 全部重跑时 CSH-04, DOC-06, CSH-02 只处理 BA-01, BA-02 的
    函证不再是当前的 (留作历史, 不出新版本, 不建失败任务), 复核后回到 FIN-01, 汇总表不变."""
    s, financial = await layered_at_fin_gate(tmp_path)
    ba02 = versions_of(s, Confirmation, "C-BA-02")
    del financial.records[("bank_account", "BA-02")]
    data_of(s).record(DataInput.bank_accounts)

    found = await rerun(s)

    assert {"CSH-04", "DOC-06", "CSH-02"} <= set(found.procedure_ids)
    for pid in ("CSH-04", "DOC-06", "CSH-02"):
        assert latest(s, ProcedureRun, f"RUN-{pid}-2").status is RunStatus.succeeded, pid
    assert versions_of(s, Confirmation, "C-BA-02") == ba02
    assert not [
        t for t in await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending) if t.kind is TaskKind.failed
    ]
    await review_pending(s, "D-RM")
    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)
    assert latest(s, UncorrectedSchedule, "SCHEDULE").total_cents == 14200000


async def test_changed_account_number_sends_new_confirmation_and_reaches_fin01(tmp_path: Path) -> None:
    """R274-10: 重新导入的银行账户表改正了 BA-01 的账号 (合成: 99999901): 识别信息变了按新账户, CSH-04 用新的幂等键
    重新发函, 出 C-BA-01-2 (旧的 C-BA-01 留作历史, 不出新版本); DOC-06 抽取新回函, CSH-02 比对相符, 复核后回到
    FIN-01. BA-02 照旧沿用."""
    s, financial = await layered_at_fin_gate(tmp_path)
    old = versions_of(s, Confirmation, "C-BA-01")
    sent = submitted(s)
    row = dict(financial.records[("bank_account", "BA-01")]) | {"account_no": "99999901"}
    financial.records[("bank_account", "BA-01")] = row
    content = b"99999901 reply"  # 假函证平台按账号回函, 先按其内容登记 (同 replies() 的做法)
    reply = replies()[0].model_copy(
        update={"id": "DOC-BA01NEWNO", "file_name": "99999901.pdf", "sha256": hashlib.sha256(content).hexdigest()}
    )
    financial.document_refs.append(reply)
    fixture = json.loads((get_settings().LLM_FIXTURES_ROOT / "DOC-06" / "BA-01.json").read_text(encoding="utf-8"))
    fixture |= {"account_no": "99999901", "confirmed_balance_cents": 125840000}  # 合成: 与账面相符
    fixture["evidence"] = [{**p, "source_id": reply.id} for p in fixture["evidence"]]
    (tmp_path / "DOC-06").mkdir()
    (tmp_path / "DOC-06" / "BA-01.json").write_text(json.dumps(fixture), encoding="utf-8")
    data_of(s).record(DataInput.bank_accounts)

    await rerun(s)

    channel = s.runner.deps.confirmations
    assert isinstance(channel, FakeConfirmationChannel)
    [(key, request_)] = channel.submits[sent:]
    assert (request_.bank_account_id, request_.account_no) == ("BA-01", "99999901")
    assert key == confirmation_key(EID, BankAccount.model_validate(row))
    assert versions_of(s, Confirmation, "C-BA-01") == old
    new = latest(s, Confirmation, "C-BA-01-2")
    assert (new.version, new.idempotency_key, new.reply_document_id, new.difference_cents) == (3, key, reply.id, 0)
    await review_pending(s, "D-AN")
    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)
    # 再一次银行账户表变动 (识别信息不变): CSH-04 按留痕里的函证 ID 重建 C-BA-01-2, 不再发函, DOC-06, CSH-02 接着出版本
    data_of(s).record(DataInput.bank_accounts)
    await rerun(s, request_id="RR-2")
    assert channel.submits[sent + 1 :] == [] and versions_of(s, Confirmation, "C-BA-01") == old
    assert latest(s, Confirmation, "C-BA-01-2").version == 5
    await review_pending(s, "D-AN2")
    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)


async def test_mock_report_cites_rerun_judgment_after_fin01() -> None:
    """R274-11: mock 模式 (Demo 的缺省) 下停在 FIN-01 时发票表变了, 重跑 REV-08, REV-02, 复核后 FIN-01 通过: RPT-03 的
    固定输出按引用键 REV-02:SI-2026-0612 引用, 换成重跑后的当前判断, 通过校验, 停在 RPT-01."""
    s = await demo_at_fin_gate()
    data_of(s).record(DataInput.sales_invoices)
    await rerun(s)
    await review(s, SECOND_REVIEWS)
    schedule = latest(s, UncorrectedSchedule, "SCHEDULE")
    await s.submit.submit(EID, fin_decision(GateVerdict.approve, id="D-FIN-2", target_version=schedule.version))
    await wait_idle(s.runner, EID)

    assert await s.next_nodes() == ("gate_rpt01",), s.runner.run_error(EID)
    cited = latest(s, Judgment, "J-REV-02-2-SI-2026-0612")
    assert latest(s, AuditReportVersion, "REPORT-1").judgment_refs == [JudgmentRef(id=cited.id, version=cited.version)]


async def test_mock_report_redraft_after_rerun_at_rpt01() -> None:
    """R274-11: mock 模式下停在 RPT-01 时发票表变了, 重跑后复核, FIN-01 再通过: RPT-03 按非退回的重拟起草 REPORT-2,
    引用重跑后的判断, 停在 RPT-01, 可以签字."""
    s = await demo_at_fin_gate()
    await s.submit.submit(EID, fin_decision(GateVerdict.approve))
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_rpt01",), s.runner.run_error(EID)
    first = latest(s, AuditReportVersion, "REPORT-1")
    assert first.judgment_refs == [JudgmentRef(id="J-REV-02-1-SI-2026-0612", version=2)]
    data_of(s).record(DataInput.sales_invoices)
    found = await rerun(s)
    assert found.report_revision == 1
    await review(s, SECOND_REVIEWS)
    schedule = latest(s, UncorrectedSchedule, "SCHEDULE")
    await s.submit.submit(EID, fin_decision(GateVerdict.approve, id="D-FIN-2", target_version=schedule.version))
    await wait_idle(s.runner, EID)

    assert await s.next_nodes() == ("gate_rpt01",), s.runner.run_error(EID)
    second = latest(s, AuditReportVersion, "REPORT-2")
    cited = latest(s, Judgment, "J-REV-02-2-SI-2026-0612")
    assert (second.addresses_decision_id, second.judgment_refs) == (
        None,
        [JudgmentRef(id=cited.id, version=cited.version)],
    )
    await s.submit.submit(EID, rpt_decision(GateVerdict.approve, id="D-RPT-2", target_id="REPORT-2"))
    await wait_idle(s.runner, EID)
    assert (await s.engagement_store.get(EID)).locked  # type: ignore[union-attr]


# ── 第 3 轮审核补的用例 ──


def test_programs_without_current_result_are_not_stale() -> None:
    """R274-15: 过期只针对有当前结果的程序. 最近一次执行已被过期重跑取代而之后没有新执行的 (重跑后转等数据, 未执行
    或待派发) 不算过期; 在等数据或未执行 (无数据) 的也不算, 都不可重跑. 依赖的程序最近一次执行被取代 (只重跑了
    前置, 它还没有新结果) 时, 本程序的结果所依据的已不是当前的, 照旧过期."""
    both = {"RUN-REV-08-1", "RUN-REV-02-1"}
    assert set(stale_procedures(PACK, RUNS, changed(DataInput.delivery_notes), LIBRARY)) == {"REV-08", "REV-02"}
    assert stale_procedures(PACK, RUNS, changed(DataInput.delivery_notes), LIBRARY, both) == []
    assert stale_procedures(PACK, RUNS, changed(), LIBRARY, {"RUN-REV-08-1"}) == ["REV-02"]
    for status in (ProcedureStatus.no_data, ProcedureStatus.awaiting_data):
        pack = [p.model_copy(update={"status": status}) if p.id in ("REV-08", "REV-02") else p for p in PACK]
        stale = stale_procedures(pack, RUNS, changed(DataInput.delivery_notes), LIBRARY)
        assert stale == [], status
        assert rerunnable(pack, ["REV-08", "REV-02"]) == [], status


async def at_fin_without_delivery_notes() -> Started:
    """停在 FIN-01 时重新导入的发运单表没有数据行 (合成), REV-08, REV-02 过期."""
    s = await demo_at_fin_gate()
    data_of(s).table_stats.pop(TableKind.delivery_notes)
    data_of(s).record(DataInput.delivery_notes)
    return s


async def test_rerun_into_no_data_is_listed_once_and_not_rerunnable() -> None:
    """R274-15 (第 3 轮探针的情形): FIN-01 处发运单表换成空表, 全部重跑: REV-08 缺发运单转等数据 (不是失败), 图停在
    await_data, 此时两条都不算过期; 标客户无此类资料后 REV-08, REV-02 记未执行 (无数据), 回到 FIN-01: 材料的 "未执行"
    列出两条, "过期而未重跑" 不再列 (两节不重叠); 总览与程序详情不标过期, 不可重跑, 再请求全部重跑被拒."""
    s = await at_fin_without_delivery_notes()
    found = await rerun(s)
    assert found.procedure_ids == ["REV-08", "REV-02"]
    assert await s.next_nodes() == ("await_data",), s.runner.run_error(EID)
    assert latest(s, Procedure, "REV-08").status is ProcedureStatus.awaiting_data
    assert not [r for r in s.output_store.rows if isinstance(r, ProcedureRun) and r.status is RunStatus.failed]
    waiting = await overview(s)
    assert waiting.stale_procedure_ids == [] and not [p.id for p in waiting.procedures if p.rerunnable]

    await DataService(s.runner.deps, s.runner).mark_unavailable(
        EID, "REQ-delivery_notes", "合成: 本年没有发运单", "U-1"
    )
    await wait_idle(s.runner, EID)

    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)
    assert {latest(s, Procedure, pid).status for pid in ("REV-08", "REV-02")} == {ProcedureStatus.no_data}
    view = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).gate(EID)
    assert view is not None and isinstance(view.material, CompletionMaterial)
    assert [n.procedure_id for n in view.material.not_executed] == ["REV-02", "REV-08"]
    assert view.material.stale_procedure_ids == []
    ov = await overview(s)
    assert ov.stale_procedure_ids == [] and not [p.id for p in ov.procedures if p.stale or p.rerunnable]
    async with api_client(s.runner) as client:
        detail = (await client.get(f"/api/v1/engagements/{EID}/procedures/REV-08")).json()
    assert (detail["stale"], detail["rerunnable"], detail["superseded_run_ids"]) == (False, False, ["RUN-REV-08-1"])
    with pytest.raises(DecisionRejected) as rejected:
        await rerun(s, request_id="RR-2")
    assert rejected.value.code == "INVALID_VALUE"


async def test_rerun_into_awaiting_data_at_review_wait_is_not_rerunnable() -> None:
    """R274-15: 等复核处发运单表换成空表, 重跑 REV-08 (REV-02 还没执行): REV-08 转等数据, 其余待决任务照常; 此时
    REV-08 不标过期, 不可重跑 (再请求重跑什么也不会改变). 复核完转 await_data, 标无资料后到 FIN-01."""
    s = await start_demo()
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("await_events",)
    data_of(s).table_stats.pop(TableKind.delivery_notes)
    data_of(s).record(DataInput.delivery_notes)
    assert (await rerun(s)).procedure_ids == ["REV-08"]
    assert latest(s, Procedure, "REV-08").status is ProcedureStatus.awaiting_data
    ov = await overview(s)
    assert ov.stale_procedure_ids == [] and not [p.id for p in ov.procedures if p.rerunnable]
    pending = {t.id for t in await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending)}
    await review(s, [d for d in REVIEWS if d.task_id in pending])
    assert await s.next_nodes() == ("await_data",), s.runner.run_error(EID)
    await DataService(s.runner.deps, s.runner).mark_unavailable(EID, "REQ-delivery_notes", "合成理由", "U-1")
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)


async def test_legacy_key_send_resent_after_account_changed_reaches_fin01(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R274-16 (第 3 轮探针的情形): 走到 FIN-01 时的函是识别信息进键之前发的 (旧键, 留痕没有 confirmation_id), 之后
    BA-01 的账号改了 (合成: 99999901). 发函之后银行账户表变过, 旧键不再成立: 全部重跑时 CSH-04 按新键给两个账户各重发
    一次 (C-BA-01-2, C-BA-02-2; BA-02 没改也重发), 旧函证留作历史; DOC-06 只处理新函证 (不拿旧回函与新账号比, 不再
    失败), CSH-02 比对相符, 复核后回到 FIN-01."""
    monkeypatch.setattr(evidence, "confirmation_key", lambda eid, account: legacy_key(eid, account.id))
    s, financial = await layered_at_fin_gate(tmp_path)
    monkeypatch.undo()
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    for i, e in enumerate(trail.entries):  # 旧留痕没有 confirmation_id
        if e.idempotency_key in (legacy_key(EID, "BA-01"), legacy_key(EID, "BA-02")):
            trail.entries[i] = e.model_copy(
                update={"payload": {k: v for k, v in e.payload.items() if k != "confirmation_id"}}
            )
    assert latest(s, Confirmation, "C-BA-01").idempotency_key == legacy_key(EID, "BA-01")
    old = {cid: versions_of(s, Confirmation, cid) for cid in ("C-BA-01", "C-BA-02")}
    sent = submitted(s)
    row = dict(financial.records[("bank_account", "BA-01")]) | {"account_no": "99999901"}  # 合成
    financial.records[("bank_account", "BA-01")] = row
    content = b"99999901 reply"  # 假函证平台按账号回函, 先按其内容登记 (同 replies() 的做法)
    reply = replies()[0].model_copy(
        update={"id": "DOC-BA01NEWNO", "file_name": "99999901.pdf", "sha256": hashlib.sha256(content).hexdigest()}
    )
    financial.document_refs.append(reply)
    # mock 的 DOC-06 固定输出按账户取: 新回函另给一份 (证据指向新回函, 账号为新账号), 否则仍抽出旧账号与旧证据
    fixture = json.loads((get_settings().LLM_FIXTURES_ROOT / "DOC-06" / "BA-01.json").read_text(encoding="utf-8"))
    fixture |= {"account_no": "99999901", "confirmed_balance_cents": 125840000}  # 合成: 与账面相符
    fixture["evidence"] = [{**p, "source_id": reply.id} for p in fixture["evidence"]]
    (tmp_path / "DOC-06").mkdir()
    (tmp_path / "DOC-06" / "BA-01.json").write_text(json.dumps(fixture), encoding="utf-8")
    data_of(s).record(DataInput.bank_accounts)

    await rerun(s)

    channel = s.runner.deps.confirmations
    assert isinstance(channel, FakeConfirmationChannel)
    accounts = [
        BankAccount.model_validate(row),
        BankAccount.model_validate(financial.records[("bank_account", "BA-02")]),
    ]
    assert [key for key, _ in channel.submits[sent:]] == [confirmation_key(EID, a) for a in accounts]
    assert {cid: versions_of(s, Confirmation, cid) for cid in old} == old
    assert latest(s, Confirmation, "C-BA-01-2").difference_cents == 0
    assert (
        latest(s, Confirmation, "C-BA-02-2").reply_document_id == latest(s, Confirmation, "C-BA-02").reply_document_id
    )
    assert latest(s, ProcedureRun, "RUN-DOC-06-2").status is RunStatus.succeeded
    assert not [
        t for t in await s.output_store.latest(EID, ReviewTask, status=TaskStatus.pending) if t.kind is TaskKind.failed
    ]
    await review_pending(s, "D-LEG")
    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)


async def test_doc06_extracts_original_replaced_before_any_edit(tmp_path: Path) -> None:
    """R274-17 (K1): 对旧原件抽取过 → 换了原件 (银行第二封回函) → 审计师改 → 重跑: 修改之前从没对新原件抽取过
    (之前的抽取证据指向旧原件), 不沿用审计师的修改, 按新原件重新抽取, 不写沿用留痕."""
    s, financial = await layered_at_fin_gate(tmp_path)
    before = latest(s, Confirmation, "C-BA-01")
    assert before.evidence  # DOC-06 对旧原件抽取过
    content = b"12345601 reply, second letter"  # 合成: 银行第二封回函的内容
    new = next(d for d in replies() if d.id == before.reply_document_id).model_copy(
        update={
            "id": "DOC-BA01REPLY02",
            "file_name": "12345601-second.pdf",
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    )
    financial.document_refs.append(new)
    replaced = before.model_copy(update={"version": before.version + 1, "reply_document_id": new.id})
    await s.output_store.put(EID, [replaced], actor="A5")
    edited = await edit_ba01_balance(s, 1000)
    assert edited.reply_document_id == new.id
    fixture = json.loads((get_settings().LLM_FIXTURES_ROOT / "DOC-06" / "BA-01.json").read_text(encoding="utf-8"))
    fixture["confirmed_balance_cents"] = before.confirmed_balance_cents + 4000  # 合成: 新回函的余额
    fixture["evidence"] = [{**p, "source_id": new.id} for p in fixture["evidence"]]
    (tmp_path / "DOC-06").mkdir()
    (tmp_path / "DOC-06" / "BA-01.json").write_text(json.dumps(fixture), encoding="utf-8")
    data_of(s).record(DataInput.bank_accounts)
    calls = len(llm_runs(s))

    await rerun(s)

    assert llm_runs(s)[calls:].count("RUN-DOC-06-2") == 2  # BA-01 与 BA-02 都调了大模型
    after = latest(s, Confirmation, "C-BA-01")
    assert (after.reply_document_id, after.confirmed_balance_cents) == (new.id, before.confirmed_balance_cents + 4000)
    assert {p.source_id for p in after.evidence} == {new.id}
    assert keep_entries(s) == []


def test_released_keeps_attribution_by_current_judgment() -> None:
    """R274-17 (S2): 只重跑了 REV-02 (REV-08 没重跑, 例外仍是当前的): 被取代的旧判断归因的例外按未归因给出, 当前判断
    归因的例外仍为 attributed, 归因者不变."""
    state = results_state([request(procedure_ids=["REV-02"], superseded_run_ids=["RUN-REV-02-1"])])
    by_old, by_new = (
        exception("X-REV-08-1-R01-1", "RUN-REV-08-1"),
        exception("X-REV-08-1-R02-1", "RUN-REV-08-1"),
    )
    attributed = ExceptionDisposition.attributed
    state["exceptions"] = [
        by_old.model_copy(update={"disposition": attributed, "attributed_by": "J-REV-02-1-SI-2026-0612"}),
        by_new.model_copy(update={"disposition": attributed, "attributed_by": "J-REV-02-2-SI-2026-0612"}),
    ]
    current = {x.id: (x.disposition, x.attributed_by) for x in current_results(state).exceptions}
    assert current == {
        "X-REV-08-1-R01-1": (ExceptionDisposition.open, None),
        "X-REV-08-1-R02-1": (attributed, "J-REV-02-2-SI-2026-0612"),
    }


async def test_dependent_of_program_rerun_into_no_data_stays_stale_until_rerun() -> None:
    """R274-15 的另一面: FIN-01 处银行账户表换成空表 (合成), 只重跑 CSH-04: 它缺银行账户表转等数据, 标客户无此类资料后
    记未执行, 回到 FIN-01. 没重跑的 DOC-06 自己不读账表, 但它依据的 CSH-04 结果已被取代, 仍算过期 (总览, 程序详情,
    FIN-01 材料, 重跑校验同口径), CSH-04 不算; 重跑 DOC-06 后它随前置记未执行, 不再过期, 两节仍不重叠."""
    s = await demo_at_fin_gate()
    data_of(s).table_stats.pop(TableKind.bank_accounts)
    data_of(s).record(DataInput.bank_accounts)
    assert (await rerun(s, ["CSH-04"])).procedure_ids == ["CSH-04"]
    assert await s.next_nodes() == ("await_data",), s.runner.run_error(EID)
    await DataService(s.runner.deps, s.runner).mark_unavailable(EID, "REQ-bank_accounts", "合成理由", "U-1")
    await wait_idle(s.runner, EID)
    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)

    async def material() -> CompletionMaterial:
        view = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).gate(EID)
        assert view is not None and isinstance(view.material, CompletionMaterial)
        return view.material

    first = await material()
    assert [n.procedure_id for n in first.not_executed] == ["CSH-04"]
    assert "DOC-06" in first.stale_procedure_ids and "CSH-04" not in first.stale_procedure_ids
    assert "DOC-06" in (await overview(s)).stale_procedure_ids
    async with api_client(s.runner) as client:
        details = {
            pid: (await client.get(f"/api/v1/engagements/{EID}/procedures/{pid}")).json()
            for pid in ("CSH-04", "DOC-06")
        }
    assert [(d["stale"], d["rerunnable"]) for d in details.values()] == [(False, False), (True, True)]

    await rerun(s, ["DOC-06"], request_id="RR-2")

    assert await s.next_nodes() == ("gate_fin",), s.runner.run_error(EID)
    assert latest(s, Procedure, "DOC-06").status is ProcedureStatus.no_data
    second = await material()
    assert [n.procedure_id for n in second.not_executed] == ["CSH-04", "DOC-06"]
    assert not {"CSH-04", "DOC-06"} & set(second.stale_procedure_ids)
