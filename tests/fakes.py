"""测试用假实现与样例数据. B 的真实实现就绪前, Agent 与节点测试都注入这里的假工具."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, TypeVar, cast

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from backend.adapters.llm import SwitchableLLMClient, llm_version, make_llm_client
from backend.adapters.xero_dataset import Snapshot, build_tables, snapshot_period
from backend.adapters.xero_dataset import client_profile as xero_client_profile
from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.agents.ingest import build_contract
from backend.contracts.errors import EngagementLocked, VersionConflict
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.llm_outputs import ContractExtraction
from backend.contracts.models import (
    AbnRecord,
    AgentId,
    AnalyticsResult,
    AsicCompanyRecord,
    Assertion,
    AuditException,
    AuditObjective,
    AuditReportVersion,
    AutonomyLevel,
    ClientProfile,
    Confirmation,
    ConfirmationResult,
    Contract,
    Decision,
    Direction,
    DocumentRef,
    DocumentStatus,
    DocumentType,
    Engagement,
    EvidencePointer,
    GateId,
    GateVerdict,
    Judgment,
    JudgmentRef,
    LlmSettings,
    LlmSlot,
    MaterialityCandidate,
    MaterialityCandidates,
    MisstatementDraft,
    MisstatementType,
    OpinionType,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ProposedAction,
    ReasoningStep,
    ReceiptChannel,
    Reliability,
    ReportSection,
    ReviewRecord,
    ReviewTask,
    RiskItem,
    RunStatus,
    RunTrigger,
    StepKind,
    TaskKind,
    TaskStatus,
    TrailEntry,
    TrailKind,
    User,
    Versioned,
)
from backend.contracts.ports import (
    ConfirmationReceipt,
    ConfirmationRequest,
    LLMClient,
    LLMRequest,
    LLMResponse,
    T_out,
)
from backend.contracts.records import (
    AdjustmentEntrySummary,
    BankAccount,
    BankTransaction,
    DeliveryNote,
    JournalEntry,
    SalesInvoice,
    TrialBalanceRow,
)
from backend.contracts.services import JudgmentContext, JudgmentOutcome, RuleResult
from backend.contracts.state import StatePatch, initial_state
from backend.core.config import get_settings
from backend.core.security import create_access_token
from backend.main import build_deps, create_app
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.decisions import GraphDecisionSubmit, rerun_group, rerun_procedure
from backend.orchestration.graph import GATES, build_graph, compile_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.persistence.stores import natural_key
from backend.services.features import BASELINE
from backend.services.judgment_engine import JudgmentEngine, OutputValidator
from backend.services.llm_settings import LlmSettingsService
from backend.services.procedure_library import ProcedureLibrary

NOW = datetime(2026, 9, 30, tzinfo=UTC)

# ── 样例数据: 合成数据集的 Numbat 公司 (合成数据集设计.md 2.1, 2.2 节) ──

NUMBAT_FEATURES = BASELINE  # Numbat 回归测试的功能集: 新功能全关, 保留基线的 critic (services/features.py)
NUMBAT_ABN = AbnRecord(
    abn="68900042015",
    acn="900042015",
    entity_name="Numbat Electronic Components Pty Ltd",
    status="Active",
    entity_type="Australian Private Company",
)
NUMBAT_ASIC = AsicCompanyRecord(
    acn="900042015",
    name="Numbat Electronic Components Pty Ltd",
    status="Registered",
    registered_date=date(2011, 3, 14),
)
NUMBAT_PROFILE = ClientProfile(
    id="CLIENT-001",
    legal_name="Numbat Electronic Components Pty Ltd",
    abn="68900042015",
    entity_type="small_proprietary",
    industry_anzsic="3494",
    listed=False,
    directors=["Jordan Avery", "Priya Raman"],
)
# 客户申报的名称写错了: name_match 不通过, 审计师要客户更正
TYPO_PROFILE = NUMBAT_PROFILE.model_copy(update={"legal_name": "Numbat Electronics Pty Ltd"})
ENGAGEMENT = Engagement(
    id="ENG-2026-001",
    client_id="CLIENT-001",
    period_start=date(2025, 7, 1),
    period_end=date(2026, 6, 30),
    dataset="demo",
    owner_id="U-1",
    client_profile_version=1,
    phase=Phase.P0_Acceptance,
    created_at=NOW,
    updated_at=NOW,
)
# 数据集预置的审计目标 (合成数据集设计.md 2.4 节): 收入两条为特别风险, 货币资金为高风险
NUMBAT_OBJECTIVES = [
    AuditObjective(
        id="OBJ-REV-OCC", account="Revenue", assertion="Occurrence", risk_level="significant", is_material=True
    ),
    AuditObjective(id="OBJ-REV-CUT", account="Revenue", assertion="Cutoff", risk_level="significant", is_material=True),
    AuditObjective(id="OBJ-CASH-EXI", account="Cash", assertion="Existence", risk_level="high", is_material=True),
]
NUMBAT_RISKS = [  # 合成数据集设计第 2.4 节
    RiskItem(
        id="RISK-REV-OCC",
        objective_id="OBJ-REV-OCC",
        assertion="Occurrence",
        level="significant",
        fraud_related=True,
        description="Presumed fraud risk that revenue is recorded for sales that did not occur.",
    ),
    RiskItem(
        id="RISK-REV-CUT",
        objective_id="OBJ-REV-CUT",
        assertion="Cutoff",
        level="significant",
        fraud_related=True,
        description="Risk that June sales are recognised although control passes to customers after year end.",
    ),
    RiskItem(
        id="RISK-CASH-EXI",
        objective_id="OBJ-CASH-EXI",
        assertion="Existence",
        level="high",
        fraud_related=False,
        description="Risk that bank balances recorded in the general ledger do not exist or are overstated.",
    ),
]
NUMBAT_AUDITOR = User(id="U-1", email="j.ellis@tessellate-audit.example", full_name="Jordan Ellis")  # 项目负责人, 合成
DEMO_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "demo" / "manifest.json"  # 合成数据集设计第 1 节
MANIFEST_DOCUMENTS: dict[str, dict[str, Any]] = {  # manifest 登记的 PDF, 相对 data/demo 的路径 -> 条目
    d["path"]: d for d in json.loads(DEMO_MANIFEST.read_text(encoding="utf-8"))["documents"]
}


def manifest_document(path: str, reliability: Reliability) -> DocumentRef:
    """data/demo 下 path 那份 PDF 的登记: ID, 类型, 哈希与页数取 manifest, 文件名取路径末段, 状态 received."""
    entry = MANIFEST_DOCUMENTS[path]
    return DocumentRef(
        id=entry["document_id"],
        type=DocumentType(entry["type"]),
        file_name=Path(path).name,
        sha256=entry["sha256"],
        page_count=entry["page_count"],
        reliability=reliability,
        status="received",
        uploaded_at=NOW,
    )


NUMBAT_CONTRACTS = [  # DatasetLoader.load 登记的 3 份合同
    manifest_document(f"contracts/CON-00{n}.pdf", Reliability.external_copy) for n in (1, 2, 3)
]
NUMBAT_REPLIES = [  # CSH-04 存档的 2 份银行回函 (合成数据集设计第 7 节第 10 行), 文件名为账号
    manifest_document(f"confirmations/{account_no}.pdf", Reliability.external_original)
    for account_no in ("12345601", "23456702")
]


def numbat_contracts() -> list[Contract]:
    """DOC-04 由 3 份合同抽出的合同与 11 条条款 (合成数据集设计第 6.6 节), 取自 tests/fixtures/llm/DOC-04 的模型输出."""
    root = get_settings().LLM_FIXTURES_ROOT / "DOC-04"
    return [
        build_contract(
            document.file_name.removesuffix(".pdf"),
            document,
            ContractExtraction.model_validate_json((root / document.file_name.replace(".pdf", ".json")).read_text()),
            "RUN-DOC-04-1",
        )
        for document in NUMBAT_CONTRACTS
    ]


def reply_pages(
    bank: str,
    address: str,
    ref: str,
    account: str,
    kind: str,
    bsb: str,
    number: str,
    balance: str,
    interest: str,
    officer: str,
) -> list[str]:
    """一份银行回函两页的文本层 (合成数据集设计第 6.4 节版式), 页脚含页码."""
    footer = f"{bank} | Ref {ref} | Page {{n}} of 2"
    page1 = f"""{bank.upper()}
{address}

BANK CONFIRMATION - AUDIT REPLY
Our reference: {ref}

To: The Auditor
Tessellate Audit Partners
Level 9, 1 Illustration Place, Sydney NSW 2000
confirmations@tessellate-audit.example

Customer: Numbat Electronic Components Pty Ltd (ABN 68 900 042 015)
Confirmation date: 30 June 2026

In reply to your request, we confirm the following information recorded in our books
for the customer at close of business on 30 June 2026.

Section 1 - Accounts
Account name:    Numbat Electronic Components Pty Ltd - {account}
Account type:    {kind}
BSB:             {bsb}
Account number:  {number}
Currency:        AUD
{balance}
{interest}
{footer.format(n=1)}"""
    page2 = f"""Section 2 - Facilities, guarantees and security
None held for the customer at 30 June 2026.

Section 3 - Other accounts
The customer held no other accounts with {bank} at 30 June 2026.

Section 4 - Delivery of this reply
This reply is sent by email directly to the auditor named on page 1. It has not been
sent to the customer.

This information is given without liability on the part of the bank or its officers.

Authorised officer: {officer}
{bank}
{footer.format(n=2)}"""
    return [page1, page2]


NUMBAT_REPLY_PAGES = {  # 两份回函的文本层 (合成数据集设计第 6.4, 6.5 节), 文件 ID -> 各页
    NUMBAT_REPLIES[0].id: reply_pages(
        "Harbourline Bank Limited",
        "ABN 82 920 069 414\nAudit Confirmations Unit, Level 3, 100 Example Street, Sydney NSW 2000",
        "HBL-AC-2026-004512",
        "Operating Account",
        "Business Transaction Account",
        "999-101",
        "12345601",
        "Balance at 30 June 2026:  AUD 1,208,400.00 (credit)",
        "Accrued interest at 30 June 2026:  nil",
        "Karen Whitfield, Audit Confirmations Unit",
    ),
    NUMBAT_REPLIES[1].id: reply_pages(
        "Bluegum Savings Bank Limited",
        "ABN 30 921 070 788\nConfirmations Team, 55 Sample Lane, Melbourne VIC 3000",
        "BGS-AUD-26-00871",
        "Business Saver",
        "Business Savings Account",
        "999-202",
        "23456702",
        "Balance at 30 June 2026:  AUD 400,000.00 (credit)",
        "Interest of AUD 1,190.00 was credited on 30 June 2026.",
        "Marcus Hale, Confirmations Team",
    ),
}


NUMBAT_BANKS = {"BA-01": "Harbourline Bank Limited", "BA-02": "Bluegum Savings Bank Limited"}  # 第 2.3 节
NUMBAT_ROWS: list[SalesInvoice | DeliveryNote | JournalEntry | BankAccount | BankTransaction] = [
    # 合成数据集设计第 5.1 节: 两张跨期场景发票与 SI-2026-0547 (期后收款)
    *(
        SalesInvoice(
            id=f"SI-2026-{n}",
            invoice_no=f"SI-2026-{n}",
            customer=customer,
            customer_abn=abn,
            contract_id=contract,
            invoice_date=day,
            posting_date=day,
            amount_cents=amount,
            journal_entry_id=f"SJ-2026-{n}-2",
        )
        for n, customer, abn, contract, day, amount in (
            ("0612", "Bilby Robotics Pty Ltd", "11901043383", "CON-001", date(2026, 6, 28), 14200000),
            ("0618", "Quoll Automation Pty Ltd", "46902044759", "CON-002", date(2026, 6, 29), 9500000),
            ("0547", "Echidna Controls Pty Ltd", "52904047490", None, date(2026, 5, 29), 5000000),
        )
    ),
    *(
        DeliveryNote(
            id=f"DN-2026-{n}",
            delivery_no=f"DN-2026-{n}",
            invoice_id=f"SI-2026-{n}",
            shipped_date=shipped,
            signed_date=signed,
            signed_by=by,
        )
        for n, shipped, signed, by in (
            ("0612", date(2026, 7, 1), date(2026, 7, 3), "Daniel Price"),
            ("0618", date(2026, 6, 29), date(2026, 6, 30), "Ravi Okafor"),
            ("0547", date(2026, 5, 29), date(2026, 6, 1), "Tom Nguyen"),
        )
    ),
    # 第 5.2 节: SI-2026-0547 的收款分录 (借 1010)
    JournalEntry(
        id="CR-SI-2026-0547-1",
        entry_no="CR-SI-2026-0547",
        posting_date=date(2026, 6, 30),
        created_at=datetime(2026, 6, 30, 6, 40, tzinfo=UTC),
        created_by="ar.clerk",
        approved_by="fin.controller",
        account_code="1010",
        debit_cents=5000000,
        description="Receipt SI-2026-0547 - Echidna Controls Pty Ltd (remittance advice 30 June 2026)",
        source_doc="SI-2026-0547",
    ),
    # 第 5.4 节: 两个银行账户, BA-01 期末前后的两笔流水
    BankAccount(
        id="BA-01", bank_name="Harbourline Bank Limited", bsb="999-101", account_no="12345601", gl_account_code="1010"
    ),
    BankAccount(
        id="BA-02",
        bank_name="Bluegum Savings Bank Limited",
        bsb="999-202",
        account_no="23456702",
        gl_account_code="1020",
    ),
    BankTransaction(
        id="BT-01-20260630-03",
        bank_account_id="BA-01",
        txn_date=date(2026, 6, 30),
        amount_cents=-1555000,
        balance_after_cents=120840000,
        description="EFT DEBIT SUNDRY SUPPLIERS",
        reference="OPEX-2026-06",
    ),
    BankTransaction(
        id="BT-01-20260702-01",
        bank_account_id="BA-01",
        txn_date=date(2026, 7, 2),
        amount_cents=5000000,
        balance_after_cents=126385000,
        description="EFT CREDIT ECHIDNA CONTROLS PTY LTD",
        reference="SI-2026-0547",
    ),
]
NUMBAT_TRIAL_BALANCE = [  # 本期试算表中两个银行账户的总账科目 (合成数据集设计第 3.1 节); 其余科目用到时再补
    TrialBalanceRow(period="current", account_code="1010", balance_cents=125840000),
    TrialBalanceRow(period="current", account_code="1020", balance_cents=40000000),
]
NUMBAT_RECORDS = {  # FinancialDataStore.record 取得到的行, (行类型, 行 ID) -> 行
    (source_type, row.id): row.model_dump()
    for row in NUMBAT_ROWS
    for source_type in [
        {
            SalesInvoice: "sales_invoice",
            DeliveryNote: "delivery_note",
            JournalEntry: "journal_entry",
            BankAccount: "bank_account",
            BankTransaction: "bank_transaction",
        }[type(row)]
    ]
}
NUMBAT_RECORDS |= {  # 试算表行: source_id 为科目代码, 取本期
    ("trial_balance", r.account_code): r.model_dump() for r in NUMBAT_TRIAL_BALANCE
}
NUMBAT_CANDIDATES = [  # 合成数据集设计第 3.2 节重要性候选
    MaterialityCandidate(
        basis="revenue",
        base_amount_cents=1200000000,
        pct_low=0.005,
        pct_high=0.01,
        low_cents=6000000,
        high_cents=12000000,
    ),
    MaterialityCandidate(
        basis="total_assets",
        base_amount_cents=800000000,
        pct_low=0.01,
        pct_high=0.02,
        low_cents=8000000,
        high_cents=16000000,
    ),
    MaterialityCandidate(
        basis="profit_before_tax",
        base_amount_cents=150000000,
        pct_low=0.05,
        pct_high=0.05,
        low_cents=7500000,
        high_cents=7500000,
    ),
]


# ── 假工具 ──


class FakeRegistry:
    """假登记系统: 从字典里查, 查不到返回 None. asic_queries 记下查过哪些 ACN, eids 记下每次查询传的项目 ID."""

    def __init__(
        self,
        abn_records: dict[str, AbnRecord] | None = None,
        asic_records: dict[str, AsicCompanyRecord] | None = None,
    ) -> None:
        self.abn_records = abn_records or {}  # ABN -> 登记记录
        self.asic_records = asic_records or {}  # ACN -> 公司登记记录
        self.asic_queries: list[str] = []  # 查过的 ACN, 按查询顺序
        self.eids: list[str] = []  # 每次查询 (ABN 与 ASIC) 传的项目 ID, 按查询顺序

    async def abn(self, eid: str, abn: str) -> AbnRecord | None:
        self.eids.append(eid)
        return self.abn_records.get(abn)

    async def asic_company(self, eid: str, acn: str) -> AsicCompanyRecord | None:
        self.eids.append(eid)
        self.asic_queries.append(acn)
        return self.asic_records.get(acn)


class FakeTrail:
    """假留痕: 存在内存列表里, 按写入顺序发号 TR-1, TR-2, ... 读取按项目过滤. 独立于业务事务, 写入即生效."""

    def __init__(self) -> None:
        self.entries: list[TrailEntry] = []  # 已写入的留痕, 各项目混在一起, 按写入顺序

    async def record(self, entry: TrailEntry) -> str:
        trail_id = f"TR-{len(self.entries) + 1}"
        self.entries.append(entry.model_copy(update={"id": trail_id}))
        return trail_id

    async def get(self, eid: str, id: str) -> TrailEntry | None:
        return next((e for e in self.entries if e.engagement_id == eid and e.id == id), None)

    async def find_success(self, eid: str, idempotency_key: str) -> TrailEntry | None:
        return next((e for e in self.entries if e.engagement_id == eid and e.idempotency_key == idempotency_key), None)

    async def list(
        self, eid: str, kind: TrailKind | None = None, limit: int = 100, offset: int = 0
    ) -> list[TrailEntry]:
        hits = [e for e in self.entries if e.engagement_id == eid and (kind is None or e.kind is kind)]
        return hits[offset : offset + limit]


class FakeFinancialStore:
    """假财务数据存储: 目前只提供证据文件登记 (documents), 取一行明细 (record), 合同读写 (contracts, save_contract),
    银行账户 (bank_accounts), 发票与发运单 (sales_invoices, delivery_notes) 与试算表 (trial_balance),
    其余方法用到时再补. B 的 Postgres 实现就绪前代替它."""

    def __init__(
        self,
        documents: list[DocumentRef] | None = None,
        records: dict[tuple[str, str], dict[str, Any]] | None = None,
        contracts: list[Contract] | None = None,
        trial_balance: list[TrialBalanceRow] | None = None,
    ) -> None:
        self.document_refs = list(documents or [])  # 已登记的证据文件, 各项目共用
        self.trial_balance_rows = list(trial_balance or [])  # 试算表的行, 两期混在一起, 各项目共用
        self.records = dict(records or {})  # 明细行, (行类型, 行 ID) -> 行, 各项目共用
        self.record_calls: list[tuple[str, str]] = []  # record 的每次调用 (行类型, 行 ID)
        # 合同, 合同编号 -> 合同, 各项目共用; 预置的与 save_contract 写入的都在这里
        self.saved_contracts: dict[str, Contract] = {c.id: c for c in contracts or []}
        self.save_calls: list[str] = []  # save_contract 的每次调用 (合同编号)

    async def record(self, eid: str, source_type: str, source_id: str) -> dict[str, Any] | None:
        self.record_calls.append((source_type, source_id))
        return self.records.get((source_type, source_id))

    async def trial_balance(self, eid: str, period: Literal["current", "prior"]) -> list[TrialBalanceRow]:
        return [r for r in self.trial_balance_rows if r.period == period]

    async def bank_accounts(self, eid: str) -> list[BankAccount]:
        """records 中的 bank_account 行, 按写入顺序."""
        return [BankAccount.model_validate(row) for (kind, _), row in self.records.items() if kind == "bank_account"]

    async def sales_invoices(self, eid: str, ids: Sequence[str]) -> list[SalesInvoice]:
        """records 中 ID 在 ids 内的 sales_invoice 行, 按写入顺序 (真实实现的顺序没有约定, 调用方自行排序)."""
        return [
            SalesInvoice.model_validate(row)
            for (kind, rid), row in self.records.items()
            if kind == "sales_invoice" and rid in ids
        ]

    async def delivery_notes(self, eid: str, invoice_ids: Sequence[str]) -> list[DeliveryNote]:
        """records 中发票在 invoice_ids 内的 delivery_note 行, 按写入顺序的倒序 (调用方须自行排序)."""
        notes = [DeliveryNote.model_validate(row) for (kind, _), row in self.records.items() if kind == "delivery_note"]
        return [n for n in reversed(notes) if n.invoice_id in invoice_ids]

    async def contracts(self, eid: str, ids: Sequence[str] | None = None) -> list[Contract]:
        """按写入顺序返回 (真实实现的顺序没有约定, 调用方自行排序)."""
        return [c for cid, c in self.saved_contracts.items() if ids is None or cid in ids]

    async def save_contract(self, eid: str, contract: Contract) -> None:
        self.save_calls.append(contract.id)
        self.saved_contracts[contract.id] = contract

    async def documents(self, eid: str, type: DocumentType | None = None) -> list[DocumentRef]:
        return [d for d in self.document_refs if type is None or d.type is type]


@dataclass(frozen=True)
class DocumentWrite:
    """证据文件处理状态的一次写入 (EvidenceStore.set_status), 记进 FakeOutputStore.commits, 用来检查是否在同一事务里."""

    document_id: str  # 文件 ID
    status: str  # 新状态, 如 extracted


@dataclass(frozen=True)
class EngagementWrite:
    """项目存储的一次写入, 与审计产出一起记进 FakeOutputStore.commits, 用来检查它们是否在同一事务里."""

    op: str  # "update" 或 "save_profile"
    target: str  # update 为项目编号, save_profile 为客户编号
    changes: dict[str, Any]  # update 为改了的字段, save_profile 为 {"version": 版本号}


@dataclass
class FakeTxn:
    """一个进行中的假事务: 块内的写入先暂存在这里, 块正常结束才一起生效, 块内抛错则全部丢弃."""

    eid: str  # 事务所属项目
    owner: asyncio.Task[Any] | None  # 开事务的任务; 只有它 (含其中的嵌套调用) 在事务里
    active: bool = True  # 块结束后置假, 之后不再有人能加入
    objs: list[Versioned] = field(default_factory=list)  # 暂存的审计产出
    log: list[Versioned | EngagementWrite | DocumentWrite] = field(default_factory=list)  # 全部写入, 按调用顺序
    engagements: dict[str, Engagement] = field(default_factory=dict)  # 暂存的项目改动, 项目编号 -> 改后的项目
    profiles: list[ClientProfile] = field(default_factory=list)  # 暂存的新画像
    on_commit: list[Callable[[], None]] = field(default_factory=list)  # 提交时依次执行的生效动作 (如文件状态)


class FakeEngagementStore:
    """假项目存储: 实现读项目, 列项目, 改项目状态, 读写客户画像; create 调用即抛 NotImplementedError.

    bind 到 FakeOutputStore 后, 在它的 transaction 块内调用 update 与 save_profile 会加入该事务 (第 4 节):
    块内读得到暂存的改动, 块正常结束才生效并与审计产出记进同一个 commits 项, 块内抛错一并丢弃.
    """

    def __init__(
        self, profiles: list[ClientProfile] | None = None, engagements: list[Engagement] | None = None
    ) -> None:
        self.profiles = profiles or []  # 已生效的客户画像, 同一客户可有多个版本
        self.engagements = engagements or []  # 已生效的审计项目
        self.output_store: FakeOutputStore | None = None  # 共用事务的审计产出存储, bind 后才有

    def bind(self, output_store: "FakeOutputStore") -> None:
        """之后在 output_store 的事务块内写入时加入该事务."""
        self.output_store = output_store

    def _txn(self) -> FakeTxn | None:
        return self.output_store.active_txn() if self.output_store is not None else None

    async def create(
        self,
        *,
        owner_id: str,
        period_start: date,
        period_end: date,
        dataset: str,
        profile: ClientProfile,
    ) -> Engagement:
        raise NotImplementedError

    async def get(self, eid: str) -> Engagement | None:
        txn = self._txn()
        if txn is not None and eid in txn.engagements:
            return txn.engagements[eid]
        return next((e for e in self.engagements if e.id == eid), None)

    async def update(
        self,
        eid: str,
        *,
        phase: Phase | None = None,
        locked: bool | None = None,
        version_snapshot: dict[str, str] | None = None,
        client_profile_version: int | None = None,
    ) -> None:
        """改项目的阶段, 锁定, 版本快照与采用的画像版本, 传 None 的不改; 同时刷新 updated_at. locked 置真后不可回退."""
        current = await self.get(eid)
        if current is None:
            raise LookupError(f"Engagement {eid} not found")
        if current.locked and locked is False:
            raise ValueError(f"{eid} 已锁定, 不可解锁")
        changes = {
            "phase": phase,
            "locked": locked,
            "version_snapshot": version_snapshot,
            "client_profile_version": client_profile_version,
        }
        kept = {k: v for k, v in changes.items() if v is not None}
        updated = current.model_copy(update={**kept, "updated_at": datetime.now(UTC)})
        txn = self._txn()
        if txn is not None:
            txn.engagements[eid] = updated
            txn.log.append(EngagementWrite("update", eid, kept))
        else:
            self._apply_engagement(updated)

    def _apply_engagement(self, engagement: Engagement) -> None:
        self.engagements = [engagement if e.id == engagement.id else e for e in self.engagements]

    async def client_profile(self, client_id: str, version: int | None = None) -> ClientProfile | None:
        """version 为空取最新版本; 查不到返回 None. 事务块内读得到本事务暂存的画像."""
        txn = self._txn()
        visible = self.profiles + (txn.profiles if txn is not None else [])
        hits = [p for p in visible if p.id == client_id and (version is None or p.version == version)]
        return max(hits, key=lambda p: p.version, default=None)

    async def save_profile(self, profile: ClientProfile) -> ClientProfile:
        """version 须为已有最大值 +1."""
        latest = await self.client_profile(profile.id)
        expected = latest.version + 1 if latest is not None else 1
        if profile.version != expected:
            raise VersionConflict(f"{profile.id}: v{profile.version}, 应为 v{expected}")
        txn = self._txn()
        if txn is not None:
            txn.profiles.append(profile)
            txn.log.append(EngagementWrite("save_profile", profile.id, {"version": profile.version}))
        else:
            self.profiles.append(profile)
        return profile

    def commit(self, txn: FakeTxn) -> None:
        """FakeOutputStore 提交事务时调用: 暂存的项目改动与画像生效."""
        for engagement in txn.engagements.values():
            self._apply_engagement(engagement)
        self.profiles.extend(txn.profiles)

    async def list(self, owner_id: str | None = None) -> list[Engagement]:
        """owner_id 为空列全部 (供 recover), 否则只列该用户的项目."""
        return [e for e in self.engagements if owner_id is None or e.owner_id == owner_id]


T = TypeVar("T", bound=Versioned)


class FakeOutputStore:
    """假审计产出存储, 按项目分开存. 规则同开发接口文档第 4 节 AuditOutputStore.

    transaction 块内的 put 先暂存, 块正常结束才一起提交, 块内抛错则全部丢弃; 块外的每次 put 各自提交.
    事务记在 contextvar 里, 只对开事务的任务有效: 与真实连接一样, 别的任务 (含事务块内新建的子任务) 不在事务里.
    """

    def __init__(self, rows: list[Versioned] | None = None, eid: str = ENGAGEMENT.id) -> None:
        self.default_eid = eid  # rows 属性对应的项目, 单项目测试不必传
        self.by_eid: dict[str, list[Versioned]] = {eid: list(rows or [])}  # 项目编号 -> 已提交对象, 按写入顺序
        self.commits: list[list[Versioned | EngagementWrite]] = []  # 每次提交写入的内容, 各项目按提交顺序
        self.current: ContextVar[FakeTxn | None] = ContextVar(f"fake_txn_{id(self)}", default=None)  # 当前事务
        self.engagement_store: FakeEngagementStore | None = None  # 共用事务的项目存储

    @property
    def rows(self) -> list[Versioned]:
        """默认项目的已提交对象."""
        return self.rows_of(self.default_eid)

    def rows_of(self, eid: str) -> list[Versioned]:
        """某个项目的已提交对象."""
        return self.by_eid.setdefault(eid, [])

    def active_txn(self) -> FakeTxn | None:
        """当前任务自己开的, 尚未结束的事务; 不在事务中为 None.

        contextvar 会被事务块内新建的子任务继承, 事务结束后也可能还被引用, 所以只认开事务的任务与未结束的事务.
        """
        txn = self.current.get()
        if txn is None or not txn.active or txn.owner is not asyncio.current_task():
            return None
        return txn

    def _txn(self, eid: str) -> FakeTxn | None:
        """当前任务所在的本项目事务; 不在事务中为 None. 在别的项目的事务里写入属于调用方错误, 抛 AssertionError."""
        txn = self.active_txn()
        if txn is not None and txn.eid != eid:
            raise AssertionError(f"在 {txn.eid} 的事务里写 {eid}")
        return txn

    def _visible(self, eid: str) -> list[Versioned]:
        """读写可见的对象: 本项目已提交对象与本任务事务暂存的本项目对象."""
        txn = self.active_txn()
        staged = txn.objs if txn is not None and txn.eid == eid else []
        return self.rows_of(eid) + staged

    async def put(self, eid: str, objs: Sequence[Versioned], *, actor: str) -> list[Versioned]:
        """同 (类型, id, version) 已存在则跳过并返回已存对象; 否则 version 须为已有最大值 +1, 不符抛 VersionConflict.

        绑定的项目存储中该项目已 locked (含本事务刚置的) 时抛 EngagementLocked.
        """
        txn = self._txn(eid)
        engagement = await self.engagement_store.get(eid) if self.engagement_store is not None else None
        if engagement is not None and engagement.locked:
            raise EngagementLocked(eid)
        result: list[Versioned] = []
        new: list[Versioned] = []
        for obj in objs:
            same_id = [r for r in self._visible(eid) + new if type(r) is type(obj) and r.id == obj.id]
            stored = next((r for r in same_id if r.version == obj.version), None)
            if stored is not None:
                result.append(stored)
                continue
            expected = max((r.version for r in same_id), default=0) + 1
            if obj.version != expected:
                raise VersionConflict(f"{type(obj).__name__} {obj.id}: v{obj.version}, 应为 v{expected}")
            new.append(obj)
            result.append(obj)
        if txn is not None:
            txn.objs.extend(new)
            txn.log.extend(new)
        else:
            self.rows_of(eid).extend(new)
            self.commits.append(list(new))
        return result

    async def get(self, eid: str, kind: type[T], id: str, version: int | None = None) -> T | None:
        """version 为空取最新版本; 查不到返回 None."""
        hits = [r for r in self._visible(eid) if isinstance(r, kind) and r.id == id]
        if version is not None:
            hits = [r for r in hits if r.version == version]
        return max(hits, key=lambda r: r.version, default=None)

    async def latest(self, eid: str, kind: type[T], **filters: Any) -> list[T]:
        """每个 id 的最新版本, 再按顶层字段相等过滤."""
        newest: dict[str, T] = {}
        for r in self._visible(eid):
            if isinstance(r, kind) and (r.id not in newest or r.version > newest[r.id].version):
                newest[r.id] = r
        return [r for r in newest.values() if all(getattr(r, k) == v for k, v in filters.items())]

    async def history(self, eid: str, kind: type[T], id: str) -> list[T]:
        """某个 id 的全部版本, 按版本号升序."""
        return sorted((r for r in self._visible(eid) if isinstance(r, kind) and r.id == id), key=lambda r: r.version)

    @asynccontextmanager
    async def transaction(self, eid: str) -> AsyncIterator[None]:
        """块内的 put 与绑定的项目存储写入共用一个事务; 嵌套调用加入外层事务."""
        if self._txn(eid) is not None:
            yield
            return
        txn = FakeTxn(eid, owner=asyncio.current_task())
        token = self.current.set(txn)
        try:
            yield
            self.rows_of(eid).extend(txn.objs)
            if self.engagement_store is not None:
                self.engagement_store.commit(txn)
            for action in txn.on_commit:
                action()
            self.commits.append(txn.log)
        finally:
            txn.active = False
            self.current.reset(token)


class NoTaskAsyncio:
    """替换 runner 模块里的 asyncio: 建后台任务即失败, 其余照常. 用来测任务建不起来时项目锁是否释放.

    收到的协程记在 coros 里, 不替调用方关闭: 由测试检查 launch 自己有没有关.
    """

    def __init__(self) -> None:
        self.coros: list[Coroutine[Any, Any, Any]] = []  # create_task 收到的协程

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    def create_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        self.coros.append(coro)
        raise RuntimeError("事件循环已关闭")


class FakeEvidenceStore:
    """假证据文件存储: 实现 save, pages 与 set_status, 读写的是 FakeFinancialStore 里登记的文件.

    set_status 在审计产出事务块内调用时加入该事务: 提交才生效, 抛错则丢弃, 并记进该次提交.
    calls 记下每次调用 (文件 ID, 状态, 是否在事务内)."""

    def __init__(self, financial_store: FakeFinancialStore, output_store: FakeOutputStore | None = None) -> None:
        self.financial_store = financial_store  # 文件登记所在
        self.output_store = output_store  # 共用其事务
        self.calls: list[tuple[str, str, bool]] = []  # (文件 ID, 状态, 是否在事务内)
        self.saves: list[str] = []  # save 的每次调用 (文件名)
        # save 的每次调用的 (文件名, 内容 sha256, 文件类型, 可靠性), 检查存档参数
        self.save_args: list[tuple[str, str, DocumentType, Reliability]] = []
        self.page_counts: dict[str, int] = {}  # save 登记的页数, 文件名 -> 页数; 缺省 2

    async def save(
        self, eid: str, file_name: str, data: bytes, *, type: DocumentType, reliability: Reliability
    ) -> DocumentRef:
        """登记为 DOC-{sha256 前 12 位}, received; 同一哈希已登记则返回已有记录 (接口文档第 4 节).

        页数取 page_counts, 缺省 2. saves 记下每次调用的文件名, save_args 记下文件名, 内容哈希, 类型与可靠性.
        """
        self.saves.append(file_name)
        sha256 = hashlib.sha256(data).hexdigest()
        self.save_args.append((file_name, sha256, type, reliability))
        refs = self.financial_store.document_refs
        existing = next((d for d in refs if d.sha256 == sha256), None)
        if existing is not None:
            return existing
        doc = DocumentRef(
            id=f"DOC-{sha256[:12]}",
            type=type,
            file_name=file_name,
            sha256=sha256,
            page_count=self.page_counts.get(file_name, 2),
            reliability=reliability,
            status="received",
            uploaded_at=NOW,
        )
        refs.append(doc)
        return doc

    async def pages(self, eid: str, document_id: str) -> list[str]:
        """Numbat 回函取 NUMBAT_REPLY_PAGES; 其余每页一行短文本 "{文件名} page {n}", 页数取文件登记的 page_count.

        未登记抛 LookupError.
        """
        doc = next((d for d in self.financial_store.document_refs if d.id == document_id), None)
        if doc is None:
            raise LookupError(f"document {document_id} not found")
        if document_id in NUMBAT_REPLY_PAGES:
            return list(NUMBAT_REPLY_PAGES[document_id])
        return [f"{doc.file_name} page {n}" for n in range(1, doc.page_count + 1)]

    async def set_status(self, eid: str, document_id: str, status: DocumentStatus) -> None:
        refs = self.financial_store.document_refs
        i = next((i for i, d in enumerate(refs) if d.id == document_id), None)
        if i is None:
            raise LookupError(f"document {document_id} not found")

        def apply() -> None:
            refs[i] = refs[i].model_copy(update={"status": status})

        txn = self.output_store.active_txn() if self.output_store is not None else None
        self.calls.append((document_id, status, txn is not None))
        if txn is None:
            apply()
        else:
            txn.log.append(DocumentWrite(document_id, status))
            txn.on_commit.append(apply)


NUMBAT_A02_ROWS = [  # PLN-07 A02 的取数 (合成数据集设计第 3.2 节): 收入与销售成本, 本期与上期
    {"period": "current", "revenue_cents": 1200000000, "cost_of_sales_cents": 840000000},
    {"period": "prior", "revenue_cents": 1040000000, "cost_of_sales_cents": 722800000},
]
NUMBAT_M01_ROWS = [  # PLN-01 M01 的取数 (第 3.2 节), 按 basis 排序
    {"basis": "profit_before_tax", "base_amount_cents": 150000000},
    {"basis": "revenue", "base_amount_cents": 1200000000},
    {"basis": "total_assets", "base_amount_cents": 800000000},
]


def numbat_pln07_exception(run: ProcedureRun) -> AuditException:
    """PLN-07 A01 对应收账款 1100 的变动例外 (合成数据集设计第 7 节第 4 行), 字段按 PLN-07.yaml 的输出列."""
    return AuditException(
        id=f"X-PLN-07-{run.run_no}-A01-1",
        procedure_id="PLN-07",
        run_id=run.id,
        rule_id="A01",
        severity="medium",
        source_ids=["1100"],
        amount_cents=52500000,
        detail={
            "account_name": "Trade receivables",
            "current_cents": 202500000,
            "prior_cents": 150000000,
            "change_pct": 0.35,
        },
        evidence=[
            EvidencePointer(
                type="voucher",
                source_type="trial_balance",
                source_id="1100",
                locator="balance_cents",
                reliability="internal_original",
            )
        ],
    )


NUMBAT_D02_ROWS = [  # CSH-02 D02: 期后 10 天的银行流水 (合成数据集设计第 5.4 节), BA-02 在此期间无流水
    {
        "source_type": "bank_transaction",
        "id": tid,
        "bank_account_id": "BA-01",
        "txn_date": day,
        "amount_cents": amount,
        "description": description,
        "reference": reference,
    }
    for tid, day, amount, description, reference in (
        ("BT-01-20260701-01", date(2026, 7, 1), 3145000, "EFT CREDIT WALLABY INSTRUMENTS PTY LTD", "SI-2026-0519"),
        (
            "BT-01-20260701-02",
            date(2026, 7, 1),
            -2600000,
            "EFT DEBIT EXAMPLE INDUSTRIAL ESTATES PTY LTD",
            "RENT-2026-07",
        ),
        ("BT-01-20260702-01", date(2026, 7, 2), 5000000, "EFT CREDIT ECHIDNA CONTROLS PTY LTD", "SI-2026-0547"),
        ("BT-01-20260703-01", date(2026, 7, 3), 1892000, "EFT CREDIT BANKSIA POWER SYSTEMS PTY LTD", "SI-2026-0520"),
        ("BT-01-20260706-01", date(2026, 7, 6), 2467550, "EFT CREDIT PLATYPUS SENSOR WORKS PTY LTD", "SI-2026-0521"),
        ("BT-01-20260708-01", date(2026, 7, 8), 1238000, "EFT CREDIT LORIKEET LIGHTING PTY LTD", "SI-2026-0522"),
        ("BT-01-20260709-01", date(2026, 7, 9), 2711025, "EFT CREDIT KOOKABURRA TELECOM PTY LTD", "SI-2026-0523"),
        ("BT-01-20260710-01", date(2026, 7, 10), 1596425, "EFT CREDIT MAGPIE SECURITY SYSTEMS PTY LTD", "SI-2026-0524"),
    )
]
NUMBAT_D03_ROWS = [  # CSH-02 D03: 期末前 5 天两个银行科目上的分录行 (第 4.4, 5.4 节); 付款行的摘要为示意
    {
        "source_type": "journal_entry",
        "id": jid,
        "bank_account_id": account,
        "entry_no": jid.rsplit("-", 1)[0],
        "posting_date": date(2026, 6, 30),
        "debit_cents": debit,
        "credit_cents": credit,
        "description": description,
        "source_doc": source_doc,
    }
    for jid, account, debit, credit, description, source_doc in (
        (
            "CR-SI-2026-0547-1",
            "BA-01",
            5000000,
            0,
            "Receipt SI-2026-0547 - Echidna Controls Pty Ltd (remittance advice 30 June 2026)",
            "SI-2026-0547",
        ),
        ("CP-AP-2026-06-B-2", "BA-01", 0, 35500000, "Payment AP-2026-06-B - Tasman Passive Components", "AP-2026-06-B"),
        ("CP-FRT-2026-06-2", "BA-01", 0, 2000000, "Payment FRT-2026-06 - Redgum Freight", "FRT-2026-06"),
        ("CP-OPEX-2026-06-2", "BA-01", 0, 1555000, "Payment OPEX-2026-06 - sundry suppliers", "OPEX-2026-06"),
        ("GJ-202606-INT-1", "BA-02", 119000, 0, "Interest credited June 2026", "INT-2026-06"),
    )
]


def numbat_rev08_exception(run: ProcedureRun) -> AuditException:
    """REV-08 R01 对 SI-2026-0612 的跨期例外 (合成数据集设计第 7 节第 7 行), 字段按 REV-08.yaml 的输出列."""
    return AuditException(
        id=f"X-REV-08-{run.run_no}-R01-1",
        procedure_id="REV-08",
        run_id=run.id,
        rule_id="R01",
        severity="high",
        source_ids=["SI-2026-0612"],
        amount_cents=14200000,
        detail={"posting_date": "2026-06-28", "signed_date": "2026-07-03"},
        evidence=[
            EvidencePointer(
                type="invoice",
                source_type="sales_invoice",
                source_id="SI-2026-0612",
                locator="posting_date",
                reliability="internal_original",
            )
        ],
    )


def numbat_csh05_exception(run: ProcedureRun) -> AuditException:
    """CSH-05 B01 对 BA-01 的余额不符例外 (第 7 节第 12 行): 对账单比总账少 50,000.00, 字段按 CSH-05.yaml 的输出列."""
    return AuditException(
        id=f"X-CSH-05-{run.run_no}-B01-1",
        procedure_id="CSH-05",
        run_id=run.id,
        rule_id="B01",
        severity="medium",
        source_ids=["BA-01"],
        amount_cents=-5000000,
        detail={
            "statement_balance_cents": 120840000,
            "book_balance_cents": 125840000,
            "last_bank_transaction_id": "BT-01-20260630-03",
        },
        evidence=[
            EvidencePointer(
                type="bank",
                source_type="bank_account",
                source_id="BA-01",
                locator="balance",
                reliability="external_copy",
            )
        ],
    )


NUMBAT_S01_ROWS = [  # REV-02 规则 S01 选中的发票 (合成数据集设计第 7 节第 9 行), 列按 REV-02.yaml
    {
        "id": f"SI-2026-{n}",
        "invoice_no": f"SI-2026-{n}",
        "contract_id": contract,
        "posting_date": day,
        "amount_cents": amount,
    }
    for n, contract, day, amount in (
        ("0612", "CON-001", date(2026, 6, 28), 14200000),
        ("0618", "CON-002", date(2026, 6, 29), 9500000),
    )
]
NUMBAT_RULES: dict[str, Callable[[ProcedureRun], RuleResult]] = {  # 规则引擎对 Numbat 的结果, 按程序编号
    "PLN-07": lambda run: RuleResult(exceptions=[numbat_pln07_exception(run)], rows={"A02": NUMBAT_A02_ROWS}),
    "PLN-01": lambda run: RuleResult(exceptions=[], rows={"M01": NUMBAT_M01_ROWS}),
    "REV-08": lambda run: RuleResult(exceptions=[numbat_rev08_exception(run)], rows={}),
    "REV-02": lambda run: RuleResult(exceptions=[], rows={"S01": NUMBAT_S01_ROWS}),
    "CSH-05": lambda run: RuleResult(exceptions=[numbat_csh05_exception(run)], rows={}),
    "CSH-02": lambda run: RuleResult(exceptions=[], rows={"D02": NUMBAT_D02_ROWS, "D03": NUMBAT_D03_ROWS}),
}


class ScriptedLLM:
    """假大模型: 按 fixture_key 依次返回预置的原文, 用请求的输出模型解析 (同 MockLLMClient); 记下每次请求.

    token 数按请求消息条数与原文长度给出, 用来核对留痕取的是响应里的值. 每次调用先让出一次事件循环, 并发时交错执行.
    """

    def __init__(self, replies: Mapping[str, Sequence[str]], model: str = "scripted") -> None:
        self.replies = {key: list(raws) for key, raws in replies.items()}  # fixture_key -> 待返回的原文, 按顺序
        self.model = model  # 响应里的模型名
        self.requests: list[LLMRequest] = []  # 收到的请求, 按调用顺序

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        self.requests.append(req)
        await asyncio.sleep(0)
        raw = self.replies[req.fixture_key].pop(0)
        try:
            output: T_out | None = output_model.model_validate_json(raw)
            error = None
        except ValidationError as e:
            output, error = None, str(e)
        return LLMResponse(
            output=output,
            raw=raw,
            model=self.model,
            input_tokens=100 * len(req.messages),
            output_tokens=len(raw),
            error=error,
        )


class FakeJudgmentEngine:
    """假判断引擎: 按 subject_id 返回预置结果 (按上下文与执行记录生成), 记下每次收到的程序, 上下文与 performed_by.

    没有预置的对象抛 KeyError. 真实引擎 (调大模型) 见 services/judgment_engine.py.
    """

    def __init__(self, outcomes: Mapping[str, Callable[[JudgmentContext, ProcedureRun], JudgmentOutcome]]) -> None:
        self.outcomes = dict(outcomes)  # 判断对象 ID -> 生成结果
        self.calls: list[tuple[str, JudgmentContext, str]] = []  # (程序编号, 上下文, performed_by), 按调用顺序

    async def judge(
        self, eid: str, template: ProcedureTemplate, run: ProcedureRun, ctx: JudgmentContext, performed_by: str
    ) -> JudgmentOutcome:
        self.calls.append((template.id, ctx, performed_by))
        return self.outcomes[ctx.subject_id](ctx, run)


class FakeConfirmationChannel:
    """假函证平台: 按账户返回预置回执 (回函内容为 "{账号} reply"), 同一幂等键返回第一次的同一回执; submits 记下每次调用.

    channel 为回函渠道; replies 为假时回执没有回函; body 非空时各账户的回函内容都取它 (可为 b"").
    """

    def __init__(
        self, channel: ReceiptChannel = ReceiptChannel.direct_email, replies: bool = True, body: bytes | None = None
    ) -> None:
        self.channel = channel  # 回函渠道
        self.replies = replies  # 是否回函
        self.body = body  # 统一的回函内容, 为空时按账号生成
        self.receipts: dict[str, ConfirmationReceipt] = {}  # 幂等键 -> 第一次的回执
        self.submits: list[tuple[str, ConfirmationRequest]] = []  # (幂等键, 请求), 按调用顺序

    async def submit(self, eid: str, req: ConfirmationRequest, idempotency_key: str) -> ConfirmationReceipt:
        self.submits.append((idempotency_key, req))
        if idempotency_key not in self.receipts:
            self.receipts[idempotency_key] = ConfirmationReceipt(
                sent_at=datetime(2026, 7, 1, 9, len(self.receipts), tzinfo=UTC),
                replied_at=date(2026, 7, 8) if self.replies else None,
                reply_file_name=f"{req.account_no}.pdf" if self.replies else None,
                reply_bytes=(self.body if self.body is not None else f"{req.account_no} reply".encode())
                if self.replies
                else None,
                receipt_channel=self.channel if self.replies else None,
            )
        return self.receipts[idempotency_key]


class FakeUserStore:
    """假用户存储: 只实现 get, 按 ID 取预置用户."""

    def __init__(self, users: list[User] | None = None) -> None:
        self.users = {u.id: u for u in (users if users is not None else [NUMBAT_AUDITOR])}  # 用户 ID -> 用户

    async def by_email(self, email: str) -> tuple[User, str] | None:
        raise NotImplementedError

    async def get(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    async def create(self, email: str, full_name: str, password_hash: str) -> User | None:
        raise NotImplementedError


class FakeRuleEngine:
    """假规则引擎: 按程序编号返回预置结果 (按本次执行生成, 例外 ID 随 run_no), 没有预置的程序返回空结果.

    calls 记下每次调用 (程序编号, Run ID, 参数). B 的 SqlRuleEngine 就绪前代替它.
    """

    def __init__(self, results: Mapping[str, Callable[[ProcedureRun], RuleResult]] | None = None) -> None:
        self.results = dict(NUMBAT_RULES if results is None else results)  # 程序编号 -> 按执行生成结果
        self.calls: list[tuple[str, str, dict[str, Any]]] = []  # (程序编号, Run ID, 参数)

    async def run(
        self, eid: str, template: ProcedureTemplate, run: ProcedureRun, params: Mapping[str, Any]
    ) -> RuleResult:
        self.calls.append((template.id, run.id, dict(params)))
        make = self.results.get(template.id)
        return make(run) if make is not None else RuleResult(exceptions=[], rows={})


class FakeLlmSettingsStore:
    """假模型设置存储: 按 slot 存最新一行. fail 为真时读写都抛 OSError (模拟库不可用)."""

    def __init__(self, rows: Sequence[LlmSettings] = ()) -> None:
        self.rows: dict[str, LlmSettings] = {r.slot: r for r in rows}  # slot -> 配置
        self.fail = False

    async def get(self, slot: LlmSlot) -> LlmSettings | None:
        if self.fail:
            raise OSError("database unavailable")
        return self.rows.get(slot)

    async def save(self, settings: LlmSettings) -> LlmSettings:
        if self.fail:
            raise OSError("database unavailable")
        self.rows[settings.slot] = settings
        return settings


class FakeDatasetLoader:
    """假数据集载入: load 记下调用, 首次载入时把 Numbat 的 3 份合同登记为证据文件 (received).

    预置计划数据取 Numbat 的. 更正分录取构造时预置的 adjustments: load_adjustments 记下调用, 返回所请求凭证的行
    (按 id 排序, 凭证号不在预置中抛 LookupError), 不写假财务存储; 没有预置 (None, 同数据集没有更正分录文件) 时抛
    FileNotFoundError. adjustment_entries 与 adjustment_entry_nos 按预置行, 凭证号自然序, 同真实实现.
    """

    def __init__(self, financial_store: FakeFinancialStore, adjustments: list[JournalEntry] | None = None) -> None:
        self.financial_store = financial_store  # 合同登记到这里
        self.loads: list[tuple[str, str]] = []  # (项目编号, 数据集名), 按调用顺序
        self.adjustments = adjustments  # 预置的更正分录行; None 表示数据集没有更正分录文件
        self.adjustment_loads: list[tuple[str, str, list[str] | None]] = []  # (项目编号, 数据集名, 凭证号)

    async def load_adjustments(
        self, eid: str, dataset: str, entry_nos: Sequence[str] | None = None
    ) -> list[JournalEntry]:
        self.adjustment_loads.append((eid, dataset, list(entry_nos) if entry_nos is not None else None))
        if self.adjustments is None:
            raise FileNotFoundError(f"没有更正分录文件 ({dataset})")
        known = {x.entry_no for x in self.adjustments}
        missing = [no for no in entry_nos or () if no not in known]
        if missing:
            raise LookupError(f"更正分录中没有 {', '.join(missing)}")
        wanted = set(entry_nos) if entry_nos is not None else known
        return sorted((x for x in self.adjustments if x.entry_no in wanted), key=lambda x: x.id)

    def adjustment_entries(self, dataset: str) -> list[AdjustmentEntrySummary]:
        groups: dict[str, list[JournalEntry]] = {}
        for line in self.adjustments or []:
            groups.setdefault(line.entry_no, []).append(line)
        return [
            AdjustmentEntrySummary(
                entry_no=no, posting_date=rows[0].posting_date, description=rows[0].description, line_count=len(rows)
            )
            for no, rows in sorted(groups.items(), key=lambda item: natural_key(item[0]))
        ]

    def adjustment_entry_nos(self, dataset: str) -> list[str]:
        return [x.entry_no for x in self.adjustment_entries(dataset)]

    async def load(self, eid: str, dataset: str) -> None:
        self.loads.append((eid, dataset))
        known = {d.id for d in self.financial_store.document_refs}
        self.financial_store.document_refs += [d for d in NUMBAT_CONTRACTS if d.id not in known]

    def planning_presets(self, dataset: str) -> tuple[list[AuditObjective], list[RiskItem]]:
        return list(NUMBAT_OBJECTIVES), list(NUMBAT_RISKS)

    def client_profile(self, dataset: str) -> ClientProfile:
        return NUMBAT_PROFILE

    def datasets(self) -> list[str]:
        return ["demo"]

    def period(self, dataset: str) -> tuple[date, date] | None:
        return None  # 同 Numbat 的 CSV 数据集, 期间由审计师填


def bind_stores(engagement_store: FakeEngagementStore, output_store: FakeOutputStore) -> None:
    """让项目存储加入审计产出存储的事务, 同真实实现共用一个数据库事务."""
    engagement_store.bind(output_store)
    output_store.engagement_store = engagement_store


# ── 组装 ──


def make_deps(
    registry: FakeRegistry,
    trail: FakeTrail,
    engagement_store: FakeEngagementStore | None = None,
    output_store: FakeOutputStore | None = None,
    financial_store: FakeFinancialStore | None = None,
    rules: FakeRuleEngine | None = None,
    llm: LLMClient | None = None,
    confirmations: FakeConfirmationChannel | None = None,
    library: ProcedureLibrary | None = None,
    review_llm: LLMClient | None = None,
    features: frozenset[str] | None = None,
) -> AgentDeps:
    """真实依赖里换掉登记系统, 留痕, 项目存储, 审计产出存储, 财务数据存储, 证据文件存储, 数据集载入, 规则引擎,
    函证平台与用户 (给了 library 时另换程序库).
    features: 启用的功能, 写入 AgentDeps.features. 没给 library 时缺省为基线, 按它建 knowledge 目录的程序库 (同
    组装根); 给了 library 时不重建, 缺省取 library.features, 给了而与之不同时抛 ValueError (两处功能集须一致).
    其余依赖只构造不连接. 判断引擎按替换后的大模型, 财务数据存储, 留痕与程序库重建.

    项目存储缺省只有 Numbat 的画像与 ENGAGEMENT; 审计产出存储与财务数据存储缺省为空; 规则引擎缺省给 Numbat 的结果;
    大模型缺省为 mock 模式的固定输出客户端 (按假项目存储中项目的数据集选 fixture 目录); 复核模型缺省同样,
    但给了 llm 而没给 review_llm 时为空 (A7 不复核, 免得注入的假大模型之外又读固定输出);
    函证平台缺省为假平台 (直达邮件回函).
    项目存储与证据文件存储的写入加入审计产出存储的事务.
    """
    if engagement_store is None:
        engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    if output_store is None:
        output_store = FakeOutputStore()
    bind_stores(engagement_store, output_store)
    if financial_store is None:
        financial_store = FakeFinancialStore()
    settings = get_settings()
    deps = build_deps(settings)

    def mock_client() -> SwitchableLLMClient:
        """同组装根的 mock 客户端, 但按假项目存储取项目数据集 (组装根的绑定在 Postgres 项目存储上)."""
        return SwitchableLLMClient(make_llm_client(settings, engagement_store), llm_version(settings)["model"])

    if review_llm is None and llm is None:
        review_llm = mock_client()
    llm = llm if llm is not None else mock_client()
    if library is None:
        features = BASELINE if features is None else features
        library = deps.library if deps.library.features == features else ProcedureLibrary(deps.library.root, features)
    elif features is None:
        features = library.features
    elif features != library.features:
        raise ValueError(f"features {sorted(features)} 与程序库的 {sorted(library.features)} 不一致")
    judgments = JudgmentEngine(llm, OutputValidator(financial_store, library, trail), trail, library)
    return replace(
        deps,
        library=library,
        llm=llm,
        judgments=judgments,
        registry=registry,
        trail=trail,
        engagement_store=engagement_store,
        output_store=output_store,
        financial_store=financial_store,
        evidence_store=FakeEvidenceStore(financial_store, output_store),
        datasets=FakeDatasetLoader(financial_store),
        rules=rules if rules is not None else FakeRuleEngine(),
        confirmations=confirmations if confirmations is not None else FakeConfirmationChannel(),
        users=FakeUserStore(),
        review_llm=review_llm,
        features=features,
    )


def make_procedure(
    procedure_id: str,
    phase: Phase = Phase.P0_Acceptance,
    version: int = 1,
    status: ProcedureStatus = ProcedureStatus.planned,
) -> Procedure:
    """程序包里的一条程序, 状态缺省为 planned."""
    return Procedure(
        id=procedure_id,
        phase=phase,
        template_commit="abc123",
        autonomy_level=AutonomyLevel.L3,
        version=version,
        status=status,
    )


def make_run(procedure_id: str = "ACC-04", run_no: int = 1, params: dict[str, Any] | None = None) -> ProcedureRun:
    """某条程序第 run_no 次执行的记录, 状态为 running."""
    return ProcedureRun(
        id=f"RUN-{procedure_id}-{run_no}",
        procedure_id=procedure_id,
        run_no=run_no,
        trigger=RunTrigger.initial if run_no == 1 else RunTrigger.rerun,
        params_snapshot=params or {},
        started_at=NOW,
        status=RunStatus.running,
    )


def finished(run: ProcedureRun, status: RunStatus = RunStatus.succeeded) -> ProcedureRun:
    """执行记录的终态: 置 succeeded (或给定状态), 结束时间为 NOW."""
    return run.model_copy(update={"status": status, "ended_at": NOW})


class ScriptAgent(ProcedureAgent[None]):
    """脚本 Agent: 不查数据也不调 LLM, 片段由 script(ctx) 给出 (也可在其中抛异常), 编排测试用它代替真实 Agent.

    以 A2 的身份写片段; 要以别的 Agent 身份写 (如 A3 才能写 review_tasks) 时用子类改 agent_id. calls 为被调用次数.
    """

    agent_id: ClassVar[AgentId] = AgentId.A2  # 智能体编号, 决定片段可写哪些字段
    procedures: ClassVar[frozenset[str]] = frozenset()  # 不经注册表, 由测试直接放进 GraphNodes.agents

    def __init__(self, deps: AgentDeps, script: Callable[[RunContext], StatePatch]) -> None:
        super().__init__(deps)
        self.script = script  # 收到上下文, 返回片段
        self.calls = 0  # 被调用次数

    async def execute(self, ctx: RunContext) -> None:
        self.calls += 1

    async def interpret(self, ctx: RunContext, findings: None) -> StatePatch:
        return self.script(ctx)


class ScriptA3(ScriptAgent):
    """以 A3 (文件抽取) 身份写片段的脚本 Agent, 可写 documents, confirmations, review_tasks."""

    agent_id: ClassVar[AgentId] = AgentId.A3


class ScriptA4(ScriptAgent):
    """以 A4 (风险评估) 身份写片段的脚本 Agent, 可写分析结果, 重要性候选, 审计目标, 风险清单, 例外."""

    agent_id: ClassVar[AgentId] = AgentId.A4


class ScriptA5(ScriptAgent):
    """以 A5 (外部证据) 身份写片段的脚本 Agent, 可写 confirmations, documents."""

    agent_id: ClassVar[AgentId] = AgentId.A5


class ScriptA6(ScriptAgent):
    """以 A6 (实质性程序) 身份写片段的脚本 Agent, 可写例外, 判断, 复核任务, 函证."""

    agent_id: ClassVar[AgentId] = AgentId.A6


class ScriptA10(ScriptAgent):
    """以 A10 (报告) 身份写片段的脚本 Agent, 可写 report_versions, review_tasks."""

    agent_id: ClassVar[AgentId] = AgentId.A10


def numbat_p1_agents(deps: AgentDeps, presets: bool = True) -> dict[str, ProcedureAgent[Any]]:
    """P1 三条程序的脚本 Agent, 产出按合成数据集设计第 7 节第 3-5 行. 真实的 A3, A4 由 R37, R38 实现.

    DOC-04: 3 份合同置 extracted. PLN-07: ANALYTICS 与例外 X-PLN-07-{run_no}-A01-1 (应收账款 1100 变动 35%).
    PLN-01: MATERIALITY (第 3.2 节三个基准); 状态中风险清单为空且 presets 为真时带出第 2.4 节 3 条目标与 3 条风险.
    presets 为假时 PLN-01 不带审计目标, 装配不出 P2 程序, 图可直接走到 FIN-01 闸门.
    分析结果与重要性候选在状态已有时出下一版.
    """

    def doc04(ctx: RunContext) -> StatePatch:
        extracted = [d.model_copy(update={"status": "extracted"}) for d in NUMBAT_CONTRACTS]
        return {"documents": extracted, "procedure_runs": [finished(ctx.run)]}

    def pln07(ctx: RunContext) -> StatePatch:
        run = finished(ctx.run)
        exception = AuditException(
            id=f"X-PLN-07-{run.run_no}-A01-1",
            procedure_id="PLN-07",
            run_id=run.id,
            rule_id="A01",
            severity="medium",
            source_ids=["1100"],
            amount_cents=52500000,
            detail={"account_code": "1100", "current_cents": 202500000, "prior_cents": 150000000, "change_pct": 0.35},
            evidence=[],
        )
        previous = ctx.state["analytics"]
        analytics = AnalyticsResult(
            id="ANALYTICS",
            version=previous.version + 1 if previous else 1,
            ratios={"gross_margin_current": 0.3, "gross_margin_prior": 0.305, "revenue_growth": 0.1538},
            exception_ids=[exception.id],
        )
        return {"analytics": analytics, "exceptions": [exception], "procedure_runs": [run]}

    def pln01(ctx: RunContext) -> StatePatch:
        previous = ctx.state["materiality_candidates"]
        candidates = MaterialityCandidates(
            id="MATERIALITY", version=previous.version + 1 if previous else 1, candidates=NUMBAT_CANDIDATES
        )
        patch: StatePatch = {"materiality_candidates": candidates, "procedure_runs": [finished(ctx.run)]}
        if presets and not ctx.state["risk_register"]:
            patch["audit_objectives"] = list(NUMBAT_OBJECTIVES)
            patch["risk_register"] = list(NUMBAT_RISKS)
        return patch

    return {"DOC-04": ScriptA3(deps, doc04), "PLN-07": ScriptA4(deps, pln07), "PLN-01": ScriptA4(deps, pln01)}


def numbat_pln01_with(
    objective: AuditObjective, risk: RiskItem
) -> Callable[[AgentDeps], dict[str, ProcedureAgent[Any]]]:
    """make_runner 的 agents: PLN-01 同 numbat_p1_agents, 带出预置目标与风险时另加 objective 与 risk."""

    def agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
        numbat = cast(ScriptAgent, numbat_p1_agents(deps)["PLN-01"]).script

        def pln01(ctx: RunContext) -> StatePatch:
            patch = numbat(ctx)
            if "audit_objectives" in patch:
                patch["audit_objectives"] = [*patch["audit_objectives"], objective]
                patch["risk_register"] = [*patch["risk_register"], risk]
            return patch

        return {"PLN-01": ScriptA4(deps, pln01)}

    return agents


def numbat_p2_agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
    """P2 第 0 层四条程序的脚本 Agent, 产出按合成数据集设计第 7 节第 7, 8, 10, 12 行. 真实 Agent 由 M3 实现.

    REV-08: 例外 X-REV-08-{n}-R01-1 (SI-2026-0612 跨期, 14200000). REV-03: 3 份合同各一条 accept 判断与一条 normal 任务.
    CSH-04: C-BA-01, C-BA-02 第 1 版与 2 份回函 (执行时登记进财务数据存储, 代替 EvidenceStore.save).
    CSH-05: 例外 X-CSH-05-{n}-B01-1 (BA-01 对账单比总账少 5000000). {n} 为 run_no.
    """

    def rev08(ctx: RunContext) -> StatePatch:
        run = finished(ctx.run)
        return {"procedure_runs": [run], "exceptions": [numbat_rev08_exception(run)]}

    def rev03(ctx: RunContext) -> StatePatch:
        run = finished(ctx.run)
        judgments = []
        for contract in NUMBAT_CONTRACTS:
            subject = contract.file_name.removesuffix(".pdf")
            clause = EvidencePointer(
                type="contract",
                source_type="document",
                source_id=contract.id,
                locator="5.1",
                page=2,
                reliability=contract.reliability,
            )
            judgments.append(
                Judgment(
                    id=f"J-REV-03-{run.run_no}-{subject}",
                    procedure_id="REV-03",
                    run_id=run.id,
                    subject_id=subject,
                    conclusion="控制权在客户签收时转移, 按签收时点确认收入",
                    confidence=0.9,
                    confidence_parts={"evidence": 0.9, "rule": 1.0, "llm": 0.85},
                    reasoning_chain=[ReasoningStep(kind=StepKind.fact, text="合同约定签收时转移", evidence=[clause])],
                    evidence=[clause],
                    counter_argument="退货权或返利可能使控制权转移时点推后",
                    proposed_action=ProposedAction.accept,
                    autonomy_level=ctx.procedure.autonomy_level,
                    performed_by=ctx.engagement.owner_id,
                    created_at=NOW,
                )
            )
        tasks = [deps.queue.make_task(TaskKind.normal, j, "REV-03", run.id, now=NOW) for j in judgments]
        return {"procedure_runs": [run], "judgments": judgments, "review_tasks": tasks}

    def csh04(ctx: RunContext) -> StatePatch:
        registered = deps.financial_store.document_refs  # type: ignore[attr-defined]
        registered.extend(d for d in NUMBAT_REPLIES if d not in registered)
        confirmations = [
            Confirmation(
                id=f"C-{account}",
                bank_account_id=account,
                confirming_party=bank,
                idempotency_key=f"{ctx.engagement.id}:CSH-04:{account}",
                sent_at=NOW,
                reply_document_id=reply.id,
                receipt_channel=ReceiptChannel.direct_email,
            )
            for (account, bank), reply in zip(NUMBAT_BANKS.items(), NUMBAT_REPLIES, strict=True)
        ]
        return {"procedure_runs": [finished(ctx.run)], "confirmations": confirmations, "documents": NUMBAT_REPLIES}

    def csh05(ctx: RunContext) -> StatePatch:
        run = finished(ctx.run)
        return {"procedure_runs": [run], "exceptions": [numbat_csh05_exception(run)]}

    return {
        "REV-08": ScriptA6(deps, rev08),
        "REV-03": ScriptA6(deps, rev03),
        "CSH-04": ScriptA5(deps, csh04),
        "CSH-05": ScriptA6(deps, csh05),
    }


def numbat_cash_agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
    """现金线后两条程序的脚本 Agent, 产出按合成数据集设计第 7 节第 11, 13 行. 真实 Agent 由 M3 实现.

    DOC-06: 两份函证写入回函余额 (BA-01 120840000, BA-02 40000000, 截至 2026-06-30), 两份回函置 extracted.
    CSH-02: BA-01 账面 125840000, 差额 -5000000, 结果 difference, 例外 X-CSH-02-{n}-D01-1, 判断 J-CSH-02-{n}-BA-01
    (在途存款造成的时间性差异, 建议关闭, 解释 CSH-05 与本程序的两条例外) 与一条 normal 任务; BA-02 相符, 不出判断.
    """
    replied = {"BA-01": 120840000, "BA-02": 40000000}
    booked = {"BA-01": 125840000, "BA-02": 40000000}

    def doc06(ctx: RunContext) -> StatePatch:
        confirmations = [
            c.model_copy(
                update={
                    "version": c.version + 1,
                    "confirmed_balance_cents": replied[c.bank_account_id],
                    "balance_date": date(2026, 6, 30),
                }
            )
            for c in ctx.state["confirmations"]
        ]
        documents = [d.model_copy(update={"status": "extracted"}) for d in NUMBAT_REPLIES]
        return {"procedure_runs": [finished(ctx.run)], "confirmations": confirmations, "documents": documents}

    def csh02(ctx: RunContext) -> StatePatch:
        run = finished(ctx.run)
        confirmations = []
        for c in ctx.state["confirmations"]:
            difference = replied[c.bank_account_id] - booked[c.bank_account_id]
            confirmations.append(
                c.model_copy(
                    update={
                        "version": c.version + 1,
                        "book_balance_cents": booked[c.bank_account_id],
                        "difference_cents": difference,
                        "result": ConfirmationResult.difference if difference else ConfirmationResult.agreed,
                    }
                )
            )
        exception = AuditException(
            id=f"X-CSH-02-{run.run_no}-D01-1",
            procedure_id="CSH-02",
            run_id=run.id,
            rule_id="D01",
            severity="medium",
            source_ids=["BA-01"],
            amount_cents=-5000000,
            evidence=[EvidencePointer(type="bank", source_type="bank_account", source_id="BA-01", locator="balance")],
        )
        cash05 = next(x for x in ctx.state["exceptions"] if x.procedure_id == "CSH-05" and x.source_ids == ["BA-01"])
        pointer = EvidencePointer(
            type="bank", source_type="bank_transaction", source_id="BT-01-20260702-01", locator="amount"
        )
        judgment = Judgment(
            id=f"J-CSH-02-{run.run_no}-BA-01",
            procedure_id="CSH-02",
            run_id=run.id,
            subject_id="BA-01",
            conclusion="在途存款造成的时间性差异, 不认定错报",
            confidence=0.9,
            confidence_parts={"evidence": 0.9, "rule": 1.0, "llm": 0.85},
            reasoning_chain=[ReasoningStep(kind=StepKind.fact, text="2026-07-02 到账 50,000.00", evidence=[pointer])],
            evidence=[pointer],
            counter_argument="期后到账也可能是另一笔款项",
            proposed_action=ProposedAction.close,
            exception_ids=[cash05.id, exception.id],
            autonomy_level=ctx.procedure.autonomy_level,
            performed_by=ctx.engagement.owner_id,
            created_at=NOW,
        )
        task = deps.queue.make_task(TaskKind.normal, judgment, "CSH-02", run.id, now=NOW)
        return {
            "procedure_runs": [run],
            "confirmations": confirmations,
            "exceptions": [exception],
            "judgments": [judgment],
            "review_tasks": [task],
        }

    return {"DOC-06": ScriptA3(deps, doc06), "CSH-02": ScriptA6(deps, csh02)}


NUMBAT_CUTOFF_DRAFT = MisstatementDraft(  # 合成数据集设计第 7 节第 9 行: SI-2026-0612 收入高估
    type=MisstatementType.factual,
    account="Revenue",
    assertion=Assertion.Cutoff,
    amount_cents=14200000,
    direction=Direction.overstated,
)


def numbat_rev02_agents(deps: AgentDeps, attributes: bool = True) -> dict[str, ProcedureAgent[Any]]:
    """REV-02 收入截止判断的脚本 Agent, 产出按合成数据集设计第 7 节第 9 行. 真实 Agent 由 M3 实现.

    SI-2026-0612: 控制权 2026-07-03 转移, 属下期, 建议认定错报 (NUMBAT_CUTOFF_DRAFT), 解释 REV-08 的跨期例外;
    SI-2026-0618: 2026-06-30 签收, 期间正确, accept. 各一条 normal 任务.
    attributes 为假时模拟未能解释该例外: SI-2026-0612 不出判断, 改建以例外为目标的 unattributed 任务.
    """

    def rev02(ctx: RunContext) -> StatePatch:
        run = finished(ctx.run)
        cutoff = next(x for x in ctx.state["exceptions"] if x.procedure_id == "REV-08" and x.rule_id == "R01")
        invoice = EvidencePointer(type="invoice", source_type="sales_invoice", source_id="SI-2026-0612", locator="date")

        def judgment(subject: str, conclusion: str, **fields: Any) -> Judgment:
            return Judgment(
                id=f"J-REV-02-{run.run_no}-{subject}",
                procedure_id="REV-02",
                run_id=run.id,
                subject_id=subject,
                conclusion=conclusion,
                confidence=0.9,
                confidence_parts={"evidence": 0.9, "rule": 1.0, "llm": 0.85},
                reasoning_chain=[ReasoningStep(kind=StepKind.fact, text="发运单签收日期", evidence=[invoice])],
                evidence=[invoice],
                counter_argument="签收单日期可能晚于实际交付",
                autonomy_level=ctx.procedure.autonomy_level,
                performed_by=ctx.engagement.owner_id,
                created_at=NOW,
                **fields,
            )

        judgments = [judgment("SI-2026-0618", "2026-06-30 签收, 期间正确", proposed_action=ProposedAction.accept)]
        tasks = []
        if attributes:
            judgments.insert(
                0,
                judgment(
                    "SI-2026-0612",
                    "控制权 2026-07-03 转移, 属下期, 本期收入高估",
                    proposed_action=ProposedAction.misstatement,
                    misstatement_draft=NUMBAT_CUTOFF_DRAFT,
                    exception_ids=[cutoff.id],
                ),
            )
        else:
            tasks.append(
                deps.queue.make_task(
                    TaskKind.unattributed, cutoff, "REV-02", run.id, now=NOW, subject_id="SI-2026-0612"
                )
            )
        tasks = [*(deps.queue.make_task(TaskKind.normal, j, "REV-02", run.id, now=NOW) for j in judgments), *tasks]
        return {"procedure_runs": [run], "judgments": judgments, "review_tasks": tasks}

    return {"REV-02": ScriptA6(deps, rev02)}


def numbat_report_agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
    """RPT-03 的脚本 Agent, 产出按合成数据集设计第 7 节第 16 行. 真实 Agent 由 M3 实现.

    出 REPORT-{n} 第 1 版, n 为状态中已有报告数 + 1; 意见类型取 FIN-01 选定的 opinion_route; 意见段与形成意见的基础段
    写明收入高估, 引用已确认的 J-REV-02-1-SI-2026-0612 (状态中没有时不引用). 第 2 轮起回应最近一次 RPT-01 退回,
    基础段补写合同 CON-001 第 5.1 条.
    """

    def rpt03(ctx: RunContext) -> StatePatch:
        state = ctx.state
        revision = len(state["report_versions"]) + 1
        opinion = state["opinion_route"] or OpinionType.unqualified
        fact = "收入高估 AUD 142,000: 发票 SI-2026-0612 于 2026-06-28 入账, 客户 2026-07-03 签收."
        basis = fact
        rejects = [d for d in state["gate_decisions"] if d.gate is GateId.RPT_01 and d.decision is GateVerdict.reject]
        if revision > 1:
            basis += " 合同 CON-001 第 5.1 条约定签收时转移控制权."
        cited = [j for j in state["judgments"] if j.id == "J-REV-02-1-SI-2026-0612"]
        report = AuditReportVersion(
            id=f"REPORT-{revision}",
            revision=revision,
            opinion_type=opinion,
            sections={
                ReportSection.opinion: f"{opinion.value} opinion. {fact}",
                ReportSection.basis_for_opinion: basis,
                ReportSection.directors_responsibilities: "董事责任",
                ReportSection.auditor_responsibilities: "审计师责任",
                ReportSection.other_legal_and_regulatory: "",
                ReportSection.signature: "签名",
            },
            judgment_refs=[JudgmentRef(id=j.id, version=j.version) for j in cited],
            addresses_decision_id=rejects[-1].id if revision > 1 and rejects else None,
        )
        return {"procedure_runs": [finished(ctx.run)], "report_versions": [report]}

    return {"RPT-03": ScriptA10(deps, rpt03)}


def numbat_all_p2_agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
    """P2 七条程序的 Numbat 脚本 Agent, 产出按合成数据集设计第 7 节第 7-13 行 (第 0 层四条, 现金线两条, REV-02)."""
    return {**numbat_p2_agents(deps), **numbat_cash_agents(deps), **numbat_rev02_agents(deps)}


def make_graph_nodes(deps: AgentDeps, parallel_cap: int = 2, presets: bool = True) -> GraphNodes:
    """主图节点: 已实装的 Agent 用真实的, P1 三条程序与 RPT-03 用 Numbat 脚本 Agent (见 numbat_p1_agents,
    numbat_report_agents)."""
    nodes = GraphNodes(deps, parallel_cap)
    nodes.agents.update(numbat_p1_agents(deps, presets))
    nodes.agents.update(numbat_report_agents(deps))
    return nodes


async def preset_rerun(
    store: FakeOutputStore,
    task: ReviewTask,
    record_id: str = "D-1",
    rerun_params: dict[str, Any] | None = None,
    eid: str = ENGAGEMENT.id,
) -> ReviewRecord:
    """模拟审计师对 failed 任务提交 rerun 后 DecisionSubmit 写入 Store 的对象 (接口文档第 7 节决定的效果 rerun 行),
    在一个事务内: 决定, 同程序全部待决的 failed 任务 decided 新版本 (一轮失败算一组, Q17), 程序 executing 新版本
    (params 合并 rerun_params, rerun_subject_ids 取整组对象). 组与程序新版本用 DecisionSubmit 的同一套函数算.
    返回写入的决定.
    """
    record = ReviewRecord(
        id=record_id,
        task_id=task.id,
        target_version=task.target_version,
        decision=Decision.rerun,
        reason="外部系统已恢复, 重跑",
        rerun_params=rerun_params,
        reviewed_by="U-001",
        decided_at=NOW,
    )
    procedure = await store.get(eid, Procedure, task.procedure_id)
    assert procedure is not None
    pending = await store.latest(eid, ReviewTask, procedure_id=task.procedure_id, status=TaskStatus.pending)
    group = rerun_group(task, pending)
    decided = [t.model_copy(update={"version": t.version + 1, "status": TaskStatus.decided}) for t in group]
    async with store.transaction(eid):
        await store.put(eid, [record, *decided, rerun_procedure(procedure, record, group)], actor="A1")
    return record


async def assert_pack_synced(pack: list[Procedure], store: FakeOutputStore, eid: str = ENGAGEMENT.id) -> None:
    """程序包每条与 Store 中该程序的最新版本逐字段相等: 写 Procedure 新版本的节点与决定都须保持这一点."""
    for procedure in pack:
        assert await store.get(eid, Procedure, procedure.id) == procedure, procedure.id


def make_ctx(deps: AgentDeps, procedure_id: str = "ACC-04", engagement: Engagement = ENGAGEMENT) -> RunContext:
    """某条程序第 1 次执行的上下文: Procedure 为 planned 第 1 版, Run 置 running."""
    template = deps.library.load(procedure_id)
    procedure = Procedure(
        id=procedure_id,
        phase=template.phase,
        template_commit=deps.library.commit,
        autonomy_level=template.autonomy_level,
    )
    return RunContext(
        engagement=engagement,
        procedure=procedure,
        template=template,
        run=make_run(procedure_id, params=dict(template.params)),
        state=initial_state(engagement, [procedure], NUMBAT_PROFILE.version),
    )


def make_runner(
    registry: FakeRegistry | None = None,
    engagement_store: FakeEngagementStore | None = None,
    output_store: FakeOutputStore | None = None,
    agents: Callable[[AgentDeps], dict[str, ProcedureAgent[Any]]] | None = None,
    interrupt_before: Sequence[str] = (),
    library: ProcedureLibrary | None = None,
    financial_store: FakeFinancialStore | None = None,
    trail: FakeTrail | None = None,
    confirmations: FakeConfirmationChannel | None = None,
    review_llm: LLMClient | None = None,
    features: frozenset[str] | None = None,
) -> tuple[GraphRunner, FakeOutputStore]:
    """新项目的 runner: 登记系统缺省有 Numbat 的记录, Store 缺省为空 (由 start 写入固定集). 返回 runner 与审计产出存储.

    agents: 按依赖造出要替换的 Agent, 如 lambda deps: {"ACC-04": ScriptAgent(deps, script)};
    其余程序同 make_graph_nodes.
    interrupt_before: 闸门之外另要停下的节点, 如 ["fan_in"] 停在汇合前查看暂存结果.
    library: 替换程序库 (如改过 trigger 的), 缺省为 knowledge 目录.
    financial_store, trail, confirmations: 财务数据存储, 留痕与函证平台, 缺省见 make_deps (留痕缺省为新的空留痕).
    review_llm: 复核模型 (A7), 缺省为 mock 模式的固定输出; 给了时如争议用的变体 (tests/fixtures/llm_variants).
    features: 启用的功能, 缺省为基线, 给了 library 时缺省取它的 (见 make_deps).
    """
    if registry is None:
        registry = FakeRegistry({NUMBAT_ABN.abn: NUMBAT_ABN}, {NUMBAT_ASIC.acn: NUMBAT_ASIC})
    if output_store is None:
        output_store = FakeOutputStore()
    deps = make_deps(
        registry,
        trail if trail is not None else FakeTrail(),
        engagement_store,
        output_store,
        financial_store,
        confirmations=confirmations,
        library=library,
        review_llm=review_llm,
        features=features,
    )
    nodes = make_graph_nodes(deps)
    if agents is not None:
        nodes.agents.update(agents(deps))
    saver = InMemorySaver(serde=checkpoint_serde())
    if interrupt_before:
        app = build_graph(nodes).compile(checkpointer=saver, interrupt_before=[*GATES, *interrupt_before])
    else:
        app = compile_graph(nodes, saver)
    return GraphRunner(app, deps), output_store


async def wait_idle(runner: GraphRunner, eid: str) -> None:
    """等该项目的后台任务结束; 没有后台任务时直接返回."""
    task = runner.tasks.get(eid)
    if task is not None:
        await task


# ── 经 HTTP 调接口: 依赖取自 make_runner 的 runner ──


@asynccontextmanager
async def api_client(
    runner: GraphRunner,
    user_id: str = NUMBAT_AUDITOR.id,
    deps: AgentDeps | None = None,
    llm_settings: LlmSettingsService | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """在测试的事件循环里直接调 ASGI 应用 (不触发 lifespan), app.state 换成 runner 的依赖, 带 user_id 的登录令牌.

    与 runner 同一个事件循环, 后台推进与请求可以交替 await (TestClient 另开线程, 两边的锁与任务会分属两个循环).
    deps: 给了时接口取它 (如换掉数据集载入), 编排仍用 runner 的. llm_settings: 设置页接口用的服务.
    """
    app = create_app()
    app.state.deps = deps if deps is not None else runner.deps
    app.state.runner = runner
    app.state.query = GraphStateQuery(runner.app, runner.deps, runner)
    app.state.decisions = GraphDecisionSubmit(runner.app, runner.deps, runner)
    app.state.llm_settings = llm_settings
    headers = {"Authorization": f"Bearer {create_access_token(user_id)}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        yield client


# ── Xero Demo Company 快照: 本地 xero pull 的输出, 不入库; 本机没有时跳过用到它的测试 ──

XERO_SNAPSHOTS = Path(__file__).resolve().parents[1] / "data" / "external" / "xero" / "api"  # 快照目录的上级
XERO_SNAPSHOT = "20261017-mock"  # 带付费接口模拟 (总账分录, 银行对账单) 的快照, 数据集名 xero/20261017-mock


def xero_snapshot() -> Snapshot:
    """本机的 Xero 快照; 没有时 pytest.skip."""
    root = XERO_SNAPSHOTS / XERO_SNAPSHOT
    if not (root / "organisation.json").exists():
        pytest.skip(f"本机没有 Xero 快照 {root} (不入库, 见 data/external/xero/README.md)")
    return Snapshot(root)


def xero_engagement(snapshot: Snapshot) -> tuple[ClientProfile, Engagement]:
    """Xero 快照的客户画像, 以及以它为客户的项目 (编号, 负责人同 ENGAGEMENT, 期间与数据集取快照)."""
    profile = xero_client_profile(snapshot)
    period = snapshot_period(snapshot)
    engagement = ENGAGEMENT.model_copy(
        update={
            "client_id": profile.id,
            "client_profile_version": profile.version,
            "dataset": f"xero/{XERO_SNAPSHOT}",
            "period_start": period.start,
            "period_end": period.end,
        }
    )
    return profile, engagement


def xero_financial_store(snapshot: Snapshot) -> FakeFinancialStore:
    """Xero 快照映射出的财务数据装进假财务存储: 发票, 分录与银行流水按行可取 (record), 另有试算表."""
    tables = build_tables(snapshot)
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for source, rows in (
        ("sales_invoice", tables["sales_invoices"]),
        ("journal_entry", tables["journal_entries"]),
        ("bank_transaction", tables["bank_transactions"]),
    ):
        records |= {(source, row.id): row.model_dump() for row in rows}
    return FakeFinancialStore(records=records, trial_balance=list(tables["trial_balance"]))
