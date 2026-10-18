"""P2 七条程序的 Agent 层集成: 真实 Agent, 判断引擎与 mock 大模型 (fixture), 假存储预置 Numbat 的 P2 数据,
按层直接调用, 片段经 reducer 并入状态 (合成数据集设计第 7 节第 7-13 行)."""

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.agents.evidence import EvidenceAgent
from backend.agents.fieldwork import FieldworkAgent
from backend.agents.ingest import IngestAgent
from backend.contracts.models import (
    ConfirmationResult,
    DocumentType,
    ExceptionDisposition,
    Judgment,
    Phase,
    Procedure,
    ProposedAction,
    ReceiptChannel,
    Reliability,
    ReviewStatus,
    RunStatus,
    TaskKind,
    TaskStatus,
    TrailKind,
)
from backend.contracts.records import DatasetTraits
from backend.contracts.state import EngagementState, initial_state
from backend.orchestration.nodes import apply_patch
from backend.services.procedure_library import highest_risk
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_CONTRACTS,
    NUMBAT_CUTOFF_DRAFT,
    NUMBAT_D02_ROWS,
    NUMBAT_D03_ROWS,
    NUMBAT_OBJECTIVES,
    NUMBAT_PROFILE,
    NUMBAT_RECORDS,
    NUMBAT_REPLIES,
    NUMBAT_RISKS,
    NUMBAT_TRIAL_BALANCE,
    FakeConfirmationChannel,
    FakeEvidenceStore,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    make_deps,
    make_run,
    numbat_contracts,
)

EID = ENGAGEMENT.id
LAYERS = [["REV-08", "REV-03", "CSH-04", "CSH-05"], ["REV-02", "DOC-06"], ["CSH-02"]]  # Demo需求文档第 2 节 P2 分层
OBJECTIVES = {  # 各程序直接匹配的审计目标 (assemble_pack 的 objective_ids); DOC-06 只经依赖加入
    "REV-08": ["OBJ-REV-OCC", "OBJ-REV-CUT"],
    "REV-03": ["OBJ-REV-OCC", "OBJ-REV-CUT"],
    "REV-02": ["OBJ-REV-CUT"],
    "CSH-04": ["OBJ-CASH-EXI"],
    "CSH-05": ["OBJ-CASH-EXI"],
    "DOC-06": [],
    "CSH-02": ["OBJ-CASH-EXI"],
}
JUDGMENT_IDS = [
    "J-REV-03-1-CON-001",
    "J-REV-03-1-CON-002",
    "J-REV-03-1-CON-003",
    "J-REV-02-1-SI-2026-0612",
    "J-REV-02-1-SI-2026-0618",
    "J-CSH-02-1-BA-01",
]


@dataclass
class Demo:
    """P2 跑完后的状态与依赖."""

    state: EngagementState  # 并入各层片段后的状态
    deps: AgentDeps  # 依赖 (假存储, 真实判断引擎)
    trail: FakeTrail  # 留痕


def replies() -> list[Any]:
    """Numbat 两份回函的登记, 哈希取假函证平台回函内容 ("{账号} reply") 的: CSH-04 存档时按哈希认出已登记的同一文件,
    返回预置的文件 ID (DOC-06, CSH-02 的 fixture 引用的就是它), 相当于真实环境中 manifest 预先算好的 ID."""
    return [
        d.model_copy(
            update={"sha256": hashlib.sha256(f"{d.file_name.removesuffix('.pdf')} reply".encode()).hexdigest()}
        )
        for d in NUMBAT_REPLIES
    ]


def agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
    fieldwork, evidence, ingest = FieldworkAgent(deps), EvidenceAgent(deps), IngestAgent(deps)
    return {pid: fieldwork for pid in ("REV-08", "REV-03", "REV-02", "CSH-05", "CSH-02")} | {
        "CSH-04": evidence,
        "DOC-06": ingest,
    }


def confirm_judgments(state: EngagementState, procedure_id: str) -> EngagementState:
    """模拟审计师确认某程序的全部判断: 判断出 v2 confirmed (任务仍 pending, 只为下一层取到已确认的结论)."""
    confirmed: list[Judgment] = [
        j.model_copy(update={"version": j.version + 1, "review_status": ReviewStatus.confirmed})
        for j in state["judgments"]
        if j.procedure_id == procedure_id
    ]
    return apply_patch(state, {"judgments": confirmed})


async def run_p2() -> Demo:
    """按层执行 7 条 P2 程序; 进入第 1 层前 REV-03 的 3 条判断置 confirmed v2."""
    trail = FakeTrail()
    financial = FakeFinancialStore(
        [*NUMBAT_CONTRACTS, *replies()], NUMBAT_RECORDS, numbat_contracts(), NUMBAT_TRIAL_BALANCE
    )
    deps = make_deps(FakeRegistry(), trail, financial_store=financial, confirmations=FakeConfirmationChannel())
    by_id = agents(deps)
    pack = [
        Procedure(
            id=pid,
            phase=Phase.P2_Executing,
            template_commit=deps.library.commit,
            objective_ids=OBJECTIVES[pid],
            autonomy_level=deps.library.load(pid).autonomy_level,
        )
        for layer in LAYERS
        for pid in layer
    ]
    state = initial_state(ENGAGEMENT, pack, NUMBAT_PROFILE.version) | {
        "phase": Phase.P2_Executing,
        "audit_objectives": NUMBAT_OBJECTIVES,
        "risk_register": NUMBAT_RISKS,
        "documents": [d.model_copy(update={"status": "extracted"}) for d in NUMBAT_CONTRACTS],  # P1 DOC-04 已抽取
        "materiality_cents": 12000000,
        "performance_materiality_cents": 9000000,
        "clearly_trivial_cents": 600000,
    }
    for n, layer in enumerate(LAYERS):
        if n == 1:
            state = confirm_judgments(state, "REV-03")
        patches = []
        for pid in layer:  # 同层的程序都以本层开始时的状态执行, 再一起并入 (同 fan_in)
            procedure = next(p for p in state["procedure_pack"] if p.id == pid)
            template = deps.library.load(pid)
            risk = highest_risk(state["audit_objectives"], procedure.objective_ids)
            params = deps.library.resolve_params(template, ENGAGEMENT, state, risk, traits=DatasetTraits())
            ctx = RunContext(ENGAGEMENT, procedure, template, make_run(pid, params=params), state)
            patches.append(await by_id[pid].run(ctx))
        for patch in patches:
            state = apply_patch(state, patch)
    return Demo(state, deps, trail)


async def test_runs_succeed_and_each_judgment_in_one_attempt() -> None:
    """验收 1, 2: 7 条程序各一次执行都成功; 8 次判断与抽取, 以及 A7 对 BA-01 的 2 次独立复核都一次通过
    (大模型调用全部 attempt 1, 无失败)."""
    demo = await run_p2()
    assert sorted((r.id, r.status) for r in demo.state["procedure_runs"]) == sorted(
        (f"RUN-{pid}-1", RunStatus.succeeded) for layer in LAYERS for pid in layer
    )
    calls = [e for e in demo.trail.entries if e.kind is TrailKind.llm_call]
    assert sorted((e.procedure_id, e.payload["fixture_key"]) for e in calls) == sorted(
        [("REV-03", c) for c in ("CON-001", "CON-002", "CON-003")]
        + [("REV-02", i) for i in ("SI-2026-0612", "SI-2026-0618")]
        + [("DOC-06", a) for a in ("BA-01", "BA-02")]
        + [("CSH-02", "BA-01")]
        + [("GEN-09", "BA-01-r1"), ("GEN-09", "BA-01-r2")]  # A7 对 BA-01 (L3, 建议关闭) 的两轮独立复核
    )
    assert all(e.payload["attempt"] == 1 and e.payload["level_failed"] is None for e in calls)


async def test_exceptions_and_judgments_match_dataset() -> None:
    """验收 3: 例外 3 条; 判断 6 条, ID 与第 7 节一致; 截止错报与函证差异的判断解释了对应例外."""
    demo = await run_p2()
    assert sorted(x.id for x in demo.state["exceptions"]) == [
        "X-CSH-02-1-D01-1",
        "X-CSH-05-1-B01-1",
        "X-REV-08-1-R01-1",
    ]
    judgments = {j.id: j for j in demo.state["judgments"]}
    assert sorted(judgments) == sorted(JUDGMENT_IDS)
    cutoff = judgments["J-REV-02-1-SI-2026-0612"]
    assert cutoff.proposed_action is ProposedAction.misstatement and cutoff.misstatement_draft == NUMBAT_CUTOFF_DRAFT
    assert cutoff.exception_ids == ["X-REV-08-1-R01-1"]
    assert judgments["J-REV-02-1-SI-2026-0618"].proposed_action is ProposedAction.accept
    cash = judgments["J-CSH-02-1-BA-01"]
    assert cash.proposed_action is ProposedAction.close
    assert cash.exception_ids == ["X-CSH-05-1-B01-1", "X-CSH-02-1-D01-1"]
    assert all(judgments[f"J-REV-03-1-{c}"].version == 2 for c in ("CON-001", "CON-002", "CON-003"))
    rev02 = [j for j in demo.state["judgments"] if j.procedure_id in ("REV-02", "CSH-02")]
    assert all(j.review_status is ReviewStatus.pending for j in rev02)


async def test_rev02_sees_confirmed_contract_judgment() -> None:
    """层间: REV-02 取到的 REV-03 结论是确认后的 v2."""
    demo = await run_p2()
    prompts = [
        e.payload["messages"][0]["content"]
        for e in demo.trail.entries
        if e.kind is TrailKind.llm_call and e.procedure_id == "REV-02"
    ]
    assert all("J-REV-03-1-CON-00" in p and "(confirmed)" in p for p in prompts)


async def test_tasks_confirmations_documents_and_external_calls() -> None:
    """验收 4: pending normal 任务 6 条; C-BA-01 有差异, C-BA-02 相符; 2 份回函 extracted (CSH-04 存档时认出预置的
    同一文件); 2 行 external_call."""
    demo = await run_p2()
    tasks = demo.state["review_tasks"]
    assert all((t.kind, t.status) == (TaskKind.normal, TaskStatus.pending) for t in tasks)
    assert sorted(t.target_id for t in tasks) == sorted(JUDGMENT_IDS)
    confirmations = {c.id: c for c in demo.state["confirmations"]}
    assert (confirmations["C-BA-01"].result, confirmations["C-BA-01"].difference_cents) == (
        ConfirmationResult.difference,
        -5000000,
    )
    assert (confirmations["C-BA-02"].result, confirmations["C-BA-02"].difference_cents) == (
        ConfirmationResult.agreed,
        0,
    )
    reply_ids = {d.id for d in NUMBAT_REPLIES}
    assert {c.reply_document_id for c in confirmations.values()} == reply_ids
    documents = {d.id: d.status for d in demo.state["documents"]}  # 文件状态由节点写入存储, 这里看状态
    assert {documents[i] for i in reply_ids} == {"extracted"}
    assert len([e for e in demo.trail.entries if e.kind is TrailKind.external_call]) == 2


# ── Codex R49-1, R49-2: 关键数值, 上下文与存档参数 ─────────────────────────────────────────


async def test_exceptions_sources_and_amounts() -> None:
    """第 7 节第 7, 12, 13 行: 三条例外的来源与金额, 函证差异例外的明细; 判断都待复核, 例外仍 open."""
    demo = await run_p2()
    exceptions = {x.id: x for x in demo.state["exceptions"]}
    assert {i: (x.source_ids, x.amount_cents) for i, x in exceptions.items()} == {
        "X-REV-08-1-R01-1": (["SI-2026-0612"], 14200000),
        "X-CSH-05-1-B01-1": (["BA-01"], -5000000),
        "X-CSH-02-1-D01-1": (["BA-01"], -5000000),
    }
    assert exceptions["X-CSH-02-1-D01-1"].detail == {
        "confirmed_balance_cents": 120840000,
        "book_balance_cents": 125840000,
        "gl_account_code": "1010",
    }
    assert {x.disposition for x in exceptions.values()} == {ExceptionDisposition.open}


async def test_confirmations_balances_dates_and_receipts() -> None:
    """第 10, 11, 13 行: 逐账户的回函余额与截至日, 账面余额与差额, 回函文件, 幂等键与回函渠道."""
    demo = await run_p2()
    confirmations = {c.id: c for c in demo.state["confirmations"]}
    expected = {
        "C-BA-01": (120840000, date(2026, 6, 30), 125840000, -5000000, NUMBAT_REPLIES[0].id, f"{EID}:CSH-04:BA-01"),
        "C-BA-02": (40000000, date(2026, 6, 30), 40000000, 0, NUMBAT_REPLIES[1].id, f"{EID}:CSH-04:BA-02"),
    }
    assert {
        cid: (
            c.confirmed_balance_cents,
            c.balance_date,
            c.book_balance_cents,
            c.difference_cents,
            c.reply_document_id,
            c.idempotency_key,
        )
        for cid, c in confirmations.items()
    } == expected
    assert {c.receipt_channel for c in confirmations.values()} == {ReceiptChannel.direct_email}


async def test_replies_archived_with_expected_arguments() -> None:
    """CSH-04 存档回函的参数: 文件名为账号, 内容为平台回函, 类型 confirmation, 直达邮件为 external_original
    (预置登记只用来对上文件 ID, 参数仍逐个核对)."""
    demo = await run_p2()
    store = demo.deps.evidence_store
    assert isinstance(store, FakeEvidenceStore)
    assert store.save_args == [
        (
            f"{n}.pdf",
            hashlib.sha256(f"{n} reply".encode()).hexdigest(),
            DocumentType.confirmation,
            Reliability.external_original,
        )
        for n in ("12345601", "23456702")
    ]


async def test_judgment_conclusions_and_evidence() -> None:
    """第 8, 9, 13 行: REV-03 都 accept 并引用各自的条款; SI-2026-0618 不解释任何例外; 截止错报与函证差异判断的证据."""
    demo = await run_p2()
    judgments = {j.id: j for j in demo.state["judgments"]}
    terms = {
        "CON-001": ["CON-001-T1"],
        "CON-002": ["CON-002-T1"],
        "CON-003": ["CON-003-T2", "CON-003-T3", "CON-003-T4"],
    }
    for contract, ids in terms.items():
        j = judgments[f"J-REV-03-1-{contract}"]
        assert j.proposed_action is ProposedAction.accept
        assert [p.locator for p in j.evidence if p.source_type == "document"] == ids
        assert all(i in j.conclusion for i in ids)
    assert judgments["J-REV-02-1-SI-2026-0618"].exception_ids == []
    assert [(p.source_type, p.source_id, p.locator) for p in judgments["J-REV-02-1-SI-2026-0612"].evidence] == [
        ("sales_invoice", "SI-2026-0612", "posting_date"),
        ("delivery_note", "DN-2026-0612", "signed_date"),
        ("document", NUMBAT_CONTRACTS[0].id, "CON-001-T1"),
        ("standard", "AASB15-P38", "para 38"),
        ("standard", "AASB15-P31", "para 31"),
    ]
    assert [(p.source_type, p.source_id, p.locator) for p in judgments["J-CSH-02-1-BA-01"].evidence] == [
        ("document", NUMBAT_REPLIES[0].id, "confirmed_balance_cents"),
        ("document", NUMBAT_REPLIES[0].id, "balance_date"),
        ("trial_balance", "1010", "balance_cents"),
        ("journal_entry", "CR-SI-2026-0547-1", "debit_cents"),
        ("bank_transaction", "BT-01-20260702-01", "amount_cents"),
        ("standard", "ASA505-P14", "para 14"),
    ]


async def test_csh02_prompt_carries_reconciling_items() -> None:
    """第 13 行: CSH-02 的提示词带 BA-01 的期后流水 (含 7 月 2 日 Echidna 的 50,000.00 回款) 与期末前分录
    (含 6 月 30 日 CR-SI-2026-0547 收款), 不带 BA-02 的行."""
    demo = await run_p2()
    [call] = [e for e in demo.trail.entries if e.kind is TrailKind.llm_call and e.procedure_id == "CSH-02"]
    prompt = call.payload["messages"][0]["content"]
    for row in [*NUMBAT_D02_ROWS, *NUMBAT_D03_ROWS]:
        assert (row["id"] in prompt) is (row["bank_account_id"] == "BA-01"), row["id"]
    receipt = next(r for r in NUMBAT_D02_ROWS if r["id"] == "BT-01-20260702-01")
    assert (receipt["txn_date"], receipt["amount_cents"], receipt["reference"]) == (
        date(2026, 7, 2),
        5000000,
        "SI-2026-0547",
    )
    for text in (
        '"txn_date": "2026-07-02"',
        '"reference": "SI-2026-0547"',
        '"posting_date": "2026-06-30"',
        "125840000",
    ):
        assert text in prompt, text
