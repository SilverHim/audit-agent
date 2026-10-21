"""测试用假实现与样例数据. B 的真实实现就绪前, Agent 与节点测试都注入这里的假工具."""

import asyncio
import copy
import csv
import functools
import hashlib
import json
import re
import shutil
import tempfile
import weakref
from collections import Counter
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, TypeVar, cast
from unittest import mock

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ValidationError

import backend.main
from backend.adapters.abn_lookup import AbnLookupClient
from backend.adapters.llm import MockLLMClient, SwitchableLLMClient, llm_version, make_llm_client
from backend.adapters.registry import UploadedExtracts
from backend.adapters.registry_mock import person_key
from backend.adapters.xero_dataset import Snapshot, build_tables, snapshot_period
from backend.adapters.xero_dataset import client_profile as xero_client_profile
from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.agents.evidence import confirmation_key, legacy_key
from backend.agents.ingest import build_contract
from backend.agents.registry import with_manual_owners
from backend.contracts.data import (
    ApprovalLogRow,
    CheckId,
    CheckInputs,
    CheckRun,
    ContractVersion,
    DataChange,
    DataCheck,
    DataInput,
    DataReadiness,
    DataRequest,
    DataSource,
    DocumentChange,
    ExtractionRecord,
    ImportBatch,
    ImportSession,
    TableKind,
    TableOrigin,
    TableStats,
    TableVersion,
    XeroConnection,
    table_changes,
)
from backend.contracts.errors import AbnTaken, EngagementLocked, VersionConflict
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.llm_outputs import ContractExtraction
from backend.contracts.models import (
    TERMS_ACKNOWLEDGEMENTS,
    TERMS_ELEMENTS,
    AbnRecord,
    AcceptanceScorecard,
    AgentId,
    AnalyticsResult,
    AsicCompanyRecord,
    AsicExtract,
    Assertion,
    AuditException,
    AuditObjective,
    AuditorAppointment,
    AuditorConsent,
    AuditPlan,
    AuditReportVersion,
    AutonomyLevel,
    CheckItem,
    CheckItemStatus,
    ClauseStatus,
    ClientEntry,
    ClientProfile,
    CommunicationForm,
    CommunicationMatter,
    ComplianceCheck,
    Confirmation,
    ConfirmationResult,
    Contract,
    Decision,
    DeclarationForm,
    Direction,
    DocumentChannel,
    DocumentFailedStep,
    DocumentRef,
    DocumentStatus,
    DocumentType,
    Engagement,
    EngagementTerms,
    EqrRecommendation,
    EvidencePointer,
    EvidenceType,
    FinancialYearChange,
    GateDecision,
    GateId,
    GateVerdict,
    GovernanceCommunication,
    IndependenceDeclaration,
    Judgment,
    JudgmentRef,
    LlmSettings,
    LlmSlot,
    ManualRequest,
    ManualResult,
    MaterialityCandidate,
    MaterialityCandidates,
    MaterialityChoice,
    Milestone,
    MisstatementDraft,
    MisstatementType,
    ObjectRef,
    OpinionType,
    OwnershipGraph,
    PartyLink,
    PartyNode,
    PersonSearch,
    Phase,
    PlanConfirmation,
    PlanItem,
    PlanSection,
    PlanStatus,
    PopulationItem,
    Procedure,
    ProcedureNote,
    ProcedureRun,
    ProcedureStatus,
    ProfileRecord,
    ProfileSource,
    ProposedAction,
    ReasoningStep,
    ReceiptChannel,
    Reliability,
    ReportSection,
    RepresentationClause,
    RepresentationStatus,
    ReviewRecord,
    ReviewTask,
    RiskItem,
    RuleOutcome,
    RulePopulation,
    RunPopulation,
    RunStatus,
    RunTrigger,
    ShareholderDirection,
    StepKind,
    TargetType,
    TaskKind,
    TaskStatus,
    TermsCheck,
    TermsCheckStatus,
    TermsItem,
    TrailEntry,
    TrailKind,
    UncorrectedItem,
    User,
    Versioned,
    WorkingLanguage,
    WrittenRepresentationLetter,
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
    Account,
    AdjustmentEntrySummary,
    BankAccount,
    BankTransaction,
    DatasetTraits,
    DeliveryNote,
    Employee,
    JournalEntry,
    SalesInvoice,
    TrialBalanceRow,
)
from backend.contracts.services import JudgmentContext, JudgmentOutcome, RuleResult
from backend.contracts.state import EngagementState, StatePatch, initial_state
from backend.core.config import get_settings
from backend.core.security import create_access_token
from backend.main import build_deps, create_app
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.decisions import GraphDecisionSubmit, rerun_group, rerun_procedure
from backend.orchestration.gates import gate_nodes
from backend.orchestration.graph import build_graph, compile_graph
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.persistence.stores import natural_key
from backend.services.abn import normalize_abn
from backend.services.account_mapping import with_category
from backend.services.autonomy_policy import AutonomyPolicy
from backend.services.data_service import DataService
from backend.services.features import BASELINE, PLANNING
from backend.services.judgment_engine import MAX_ATTEMPTS, JudgmentEngine, OutputValidator
from backend.services.llm_settings import LlmSettingsService
from backend.services.populations import total_cents
from backend.services.procedure_library import ProcedureLibrary
from backend.services.review_queue import ReviewQueue
from backend.services.rule_engine import rule_population
from backend.services.xero_link import XeroLink
from tests.external import snapshot_dir, supplements_dir

NOW = datetime(2026, 9, 30, tzinfo=UTC)

# ── 样例数据: 合成数据集的 Numbat 公司 (合成数据集设计.md 2.1, 2.2 节) ──

NUMBAT_FEATURES = BASELINE  # Numbat 回归测试的功能集: 新功能全关, 保留基线的 critic (services/features.py)
# 打开总体审计计划 (需求 R204, R205) 的功能集: 基线加 planning, 不带 misstatement (计划不依赖错报, R313); 各测试共用
PLANNING_FEATURES = NUMBAT_FEATURES | {"planning"}
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


@functools.cache
def xero_documents() -> dict[str, dict[str, Any]]:
    """Xero 补造目录登记的 PDF: 相对补造目录的路径 -> manifest 条目 (设计第 4 节类型表). 补造目录经
    tests.external.supplements_dir 定位 (仓库跟踪了却找不到时失败, 没有时跳过)."""
    manifest = json.loads((supplements_dir("20261017-q1") / "manifest.json").read_text(encoding="utf-8"))
    return {d["path"]: d for d in manifest["documents"]}


def xero_document(path: str) -> DocumentRef:
    """Xero 补造目录下 path 那份 PDF 的登记: ID, 类型, 可靠性, 页数, 文件日期与关联对象取 manifest, 状态 received."""
    entry = xero_documents()[path]
    return DocumentRef(
        id=entry["document_id"],
        type=DocumentType(entry["type"]),
        file_name=Path(path).name,
        sha256=entry["sha256"],
        page_count=entry["page_count"],
        reliability=Reliability(entry["reliability"]),
        status="received",
        uploaded_at=NOW,
        document_date=date.fromisoformat(entry["document_date"]),
        related_ids=entry["related_ids"],
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
    # 第 5.2 节: 两张跨期发票与 SI-2026-0547 的销售分录 (借 1100 贷 4100), 同 data/demo/journal_entries.csv
    *(
        JournalEntry(
            id=f"SJ-2026-{n}-{line}",
            entry_no=f"SJ-2026-{n}",
            posting_date=day,
            created_at=created_at,
            created_by=created_by,
            approved_by="fin.controller",
            account_code=account,
            debit_cents=amount if line == 1 else 0,
            credit_cents=amount if line == 2 else 0,
            description=f"Sales invoice SI-2026-{n} - {customer}",
            source_doc=f"SI-2026-{n}",
        )
        for n, customer, day, created_at, created_by, amount in (
            (
                "0612",
                "Bilby Robotics Pty Ltd",
                date(2026, 6, 28),
                datetime(2026, 6, 28, 11, 14, tzinfo=UTC),
                "fin.controller",
                14200000,
            ),
            (
                "0618",
                "Quoll Automation Pty Ltd",
                date(2026, 6, 29),
                datetime(2026, 6, 29, tzinfo=UTC),
                "ar.clerk",
                9500000,
            ),
            (
                "0547",
                "Echidna Controls Pty Ltd",
                date(2026, 5, 29),
                datetime(2026, 5, 29, tzinfo=UTC),
                "ar.clerk",
                5000000,
            ),
        )
        for line, account in ((1, "1100"), (2, "4100"))
    ),
    # 第 5.2 节: SI-2026-0547 的收款分录 (借 1010 贷 1100)
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
    JournalEntry(
        id="CR-SI-2026-0547-2",
        entry_no="CR-SI-2026-0547",
        posting_date=date(2026, 6, 30),
        created_at=datetime(2026, 6, 30, 6, 40, tzinfo=UTC),
        created_by="ar.clerk",
        approved_by="fin.controller",
        account_code="1100",
        credit_cents=5000000,
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
NUMBAT_ACCOUNTS = [  # 销售分录行的两个科目 (第 3 节, data/demo/accounts.csv; 分类按名称补, 同载入); 其余用到时再补
    Account(code="1100", name="Trade receivables", type="asset", category="receivables", category_basis="name"),
    Account(code="4100", name="Sales revenue", type="revenue", category="revenue", category_basis="name"),
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


DECIDED_AT = datetime(2026, 10, 1, tzinfo=UTC)  # 测试里闸门决定的时间


def acc01_decision(verdict: GateVerdict, **changes: Any) -> GateDecision:
    """绑定评分卡第 1 版的 ACC-01 决定; reject 与 insufficient_info 带理由. changes 覆盖任意字段. 各流程测试与共用的流程
    辅助 (tests/xero_flow) 都从这里取."""
    decision = GateDecision(
        id="D-1",
        gate=GateId.ACC_01,
        target_id="SCORECARD",
        target_version=1,
        decision=verdict,
        reason=None if verdict is GateVerdict.approve else "test",
        decided_by="U-1",
        decided_at=DECIDED_AT,
    )
    return decision.model_copy(update=changes)


class FakeRegistry:
    """假登记系统: 从字典里查, 查不到返回 None. asic_queries 记下查过哪些 ACN (公司登记), eids 记下每次查询传的项目
    ID, queries 按顺序记下各类查询 (abn, asic, extract, person) 与所查的编号或姓名."""

    def __init__(
        self,
        abn_records: dict[str, AbnRecord] | None = None,
        asic_records: dict[str, AsicCompanyRecord] | None = None,
        extracts: dict[str, AsicExtract] | None = None,
        persons: Sequence[PersonSearch] = (),
    ) -> None:
        self.abn_records = abn_records or {}  # ABN -> 登记记录
        self.asic_records = asic_records or {}  # ACN -> 公司登记记录
        self.extracts = extracts or {}  # ACN -> 公司摘录 (需求 R129)
        self.persons = list(persons)  # 人员检索结果, 按姓, 名 (不分大小写) 与出生日期匹配
        self.asic_queries: list[str] = []  # 查过的 ACN (公司登记), 按查询顺序
        self.eids: list[str] = []  # 每次查询传的项目 ID, 按查询顺序
        self.queries: list[tuple[str, str]] = []  # (查询类别, 编号或 "姓 名 出生日期"), 按查询顺序

    async def abn(self, eid: str, abn: str) -> AbnRecord | None:
        self.eids.append(eid)
        self.queries.append(("abn", abn))
        return self.abn_records.get(abn)

    async def asic_company(self, eid: str, acn: str | None) -> AsicCompanyRecord | None:
        self.eids.append(eid)
        if acn is None:  # 没有 ACN 不查, 只确认有来源 (同真实实现)
            return None
        self.asic_queries.append(acn)
        self.queries.append(("asic", acn))
        return self.asic_records.get(acn)

    async def asic_extract(self, eid: str, acn: str) -> AsicExtract | None:
        self.eids.append(eid)
        self.queries.append(("extract", acn))
        return self.extracts.get(acn)

    async def asic_person(self, eid: str, family_name: str, given_name: str, birth_date: date) -> PersonSearch | None:
        self.eids.append(eid)
        self.queries.append(("person", f"{family_name} {given_name} {birth_date}"))
        wanted = person_key(family_name, given_name, birth_date)  # 同登记 mock: 去首尾空白, 不分大小写
        return next((p for p in self.persons if person_key(p.family_name, p.given_name, p.birth_date) == wanted), None)


ABR_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "abr"  # 合成的 ABN Lookup 响应 (按官方 XSD 手写)
FAKE_GUID = "fake-guid-7f3c9e21-not-real"  # 假 GUID: 测试里全文检索它, 确认不出现在日志, 留痕, 响应与异常里
ABR_URL = "https://abr.test/abrxmlsearch/AbrXmlSearch.asmx/SearchByABNv202001"  # 假端点 (不联网)


class FakeAbr:
    """假的 ABR ABN Lookup (httpx.MockTransport): 按请求表单里的 searchString 回放 tests/fixtures/abr/ 下的响应,
    把 __GUID__ 与 __ABN__ 换成请求里的值 (同真实接口回显请求, 含 GUID). replies: ABN → 响应文件名 (不带 .xml),
    或 HTTP 状态码 (正文也回显 GUID; 3xx 另带 Location 指向别的主机), "unreachable" (连接失败) 或 "bad_gzip" (声称
    gzip 而正文不是, 读响应时 httpx 抛 DecodingError); 没列的 ABN 回 not_found. requests 记下每次请求."""

    def __init__(self, replies: Mapping[str, str | int] | None = None) -> None:
        self.replies = dict(replies or {})
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        form = dict(httpx.QueryParams(request.content.decode()))
        guid, abn = form.get("authenticationGuid", ""), form.get("searchString", "")
        reply = self.replies.get(abn, "not_found")
        if reply == "unreachable":
            raise httpx.ConnectError(f"connection refused ({guid})", request=request)
        status, headers, body = self.reply_parts(reply, guid, abn)
        return httpx.Response(status, headers=headers, content=body)

    def reply_parts(self, reply: str | int, guid: str, abn: str) -> tuple[int, dict[str, str], bytes]:
        """一种回复的 (HTTP 状态, 响应头, 原始正文); 正文都回显 GUID."""
        if reply == "bad_gzip":
            return 200, {"Content-Encoding": "gzip"}, f"not gzip {guid}".encode()
        if isinstance(reply, int):
            moved = {"Location": "https://elsewhere.test/abr"} if 300 <= reply < 400 else {}
            return reply, moved, f"<error>server error for {guid}</error>".encode()
        return 200, {}, self.fixture(reply).replace(b"__GUID__", guid.encode()).replace(b"__ABN__", abn.encode())

    def echoed(self, abn: str, guid: str = FAKE_GUID) -> bytes:
        """这个 ABN 的回放内容 (原始正文, 连接失败时为异常文本), 不发请求: 测试据此确认假接口回显了 GUID."""
        reply = self.replies.get(abn, "not_found")
        if reply == "unreachable":
            return f"connection refused ({guid})".encode()
        return self.reply_parts(reply, guid, abn)[2]

    @staticmethod
    def fixture(name: str) -> bytes:
        """tests/fixtures/abr/ 下的响应模板 (未替换 __GUID__ 与 __ABN__)."""
        return (ABR_FIXTURES / f"{name}.xml").read_bytes()

    def client(self, guid: str = FAKE_GUID) -> AbnLookupClient:
        """接这个假接口的 AbnLookupClient (组装根同一个类)."""
        return AbnLookupClient(guid, ABR_URL, transport=self.transport)


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
        """同 PostgresAuditTrail: 按写入顺序取该键的第一条成功留痕; payload 带 error, success 为假或 status 为
        failed, error 的不算成功."""
        return next(
            (
                e
                for e in self.entries
                if e.engagement_id == eid
                and e.idempotency_key == idempotency_key
                and not e.payload.get("error")
                and e.payload.get("success") is not False
                and e.payload.get("status") not in {"failed", "error"}
            ),
            None,
        )

    async def list(
        self,
        eid: str,
        kind: TrailKind | Sequence[TrailKind] | None = None,
        limit: int = 100,
        offset: int = 0,
        *,
        newest: bool = False,
        before: str | None = None,
    ) -> list[TrailEntry]:
        """同真实实现, 写入顺序即排序 (假实现不比写入时间); kind 为一类或一组; before 不是本项目的 ID 时为空."""
        kinds = None if kind is None else {kind} if isinstance(kind, TrailKind) else set(kind)
        hits = [
            (n, e) for n, e in enumerate(self.entries) if e.engagement_id == eid and (kinds is None or e.kind in kinds)
        ]
        if before is not None:
            mark = next((n for n, e in enumerate(self.entries) if e.engagement_id == eid and e.id == before), -1)
            hits = [(n, e) for n, e in hits if n < mark]
        rows = [e for _, e in hits]
        if newest:
            end = max(len(rows) - offset, 0)
            return rows[max(end - limit, 0) : end]
        return rows[offset : offset + limit]

    async def last_of(self, eid: str, kind: TrailKind, actions: Sequence[str]) -> TrailEntry | None:
        """最近一条: 同真实实现按写入时间与 ID 取最大的; 写入时间都没有 (多数测试) 时按写入顺序取最后一条."""
        hits = [
            (n, e)
            for n, e in enumerate(self.entries)
            if e.engagement_id == eid and e.kind is kind and e.payload.get("action") in actions
        ]
        if not hits:
            return None
        return max(hits, key=lambda hit: (hit[1].created_at or datetime.min.replace(tzinfo=UTC), hit[0]))[1]


class FakeFinancialStore:
    """假财务数据存储: 目前只提供证据文件登记 (documents), 取一行明细 (record), 合同读写 (contracts, save_contract),
    银行账户与流水 (bank_accounts, bank_transactions), 发票与发运单 (sales_invoices, delivery_notes), 分录行
    (journal_entries), 科目 (accounts), 试算表 (trial_balance) 与工资册 (employees), 其余方法用到时再补. B 的 Postgres
    实现就绪前代替它."""

    def __init__(
        self,
        documents: list[DocumentRef] | None = None,
        records: dict[tuple[str, str], dict[str, Any]] | None = None,
        contracts: list[Contract] | None = None,
        trial_balance: list[TrialBalanceRow] | None = None,
        accounts: list[Account] | None = None,
        employees: list[Employee] | None = None,
    ) -> None:
        self.document_refs = list(documents or [])  # 已登记的证据文件, 各项目共用
        self.employee_rows = list(employees or [])  # 工资册 (可预置; FakeDataStore.write_tables 写入时替换), 各项目共用
        self.account_rows = list(accounts or [])  # 科目表, 各项目共用
        self.trial_balance_rows = list(trial_balance or [])  # 试算表的行, 两期混在一起, 各项目共用
        self.records = dict(records or {})  # 明细行, (行类型, 行 ID) -> 行, 各项目共用
        self.record_calls: list[tuple[str, str]] = []  # record 的每次调用 (行类型, 行 ID)
        # 合同, 合同编号 -> 合同, 各项目共用; 预置的与 save_contract 写入的都在这里
        self.saved_contracts: dict[str, Contract] = {c.id: c for c in contracts or []}
        self.save_calls: list[str] = []  # save_contract 的每次调用 (合同编号)
        self.contract_versions: dict[str, list[ContractVersion]] = {}  # 合同编号 → save_contract 记下的各版本

    async def record(self, eid: str, source_type: str, source_id: str) -> dict[str, Any] | None:
        """records 中的行."""
        self.record_calls.append((source_type, source_id))
        return self.records.get((source_type, source_id))

    async def trial_balance(self, eid: str, period: Literal["current", "prior"]) -> list[TrialBalanceRow]:
        return [r for r in self.trial_balance_rows if r.period == period]

    async def journal_entries(
        self, eid: str, *, entry_nos: Sequence[str] | None = None, source_docs: Sequence[str] | None = None
    ) -> list[JournalEntry]:
        """records 中凭证号在 entry_nos 内或单据号在 source_docs 内的 journal_entry 行, 按行 ID 升序, 同真实实现."""
        if entry_nos is None and source_docs is None:
            raise ValueError("journal_entries 须给 entry_nos 或 source_docs, 不取全表")
        # 先转集合, 同真实实现逐项拆开 (误传单个字符串时两边都按字符拆, 不做子串判断)
        numbers = set(entry_nos) if entry_nos is not None else set()
        documents = set(source_docs) if source_docs is not None else set()
        rows = [JournalEntry.model_validate(row) for (kind, _), row in self.records.items() if kind == "journal_entry"]
        return sorted((r for r in rows if r.entry_no in numbers or r.source_doc in documents), key=lambda r: r.id)

    async def accounts(self, eid: str, codes: Sequence[str] | None = None) -> list[Account]:
        """科目表中代码在 codes 内的科目 (None 取全部), 按代码升序, 同真实实现."""
        wanted = set(codes) if codes is not None else None  # 先转集合, 同 journal_entries
        return sorted((a for a in self.account_rows if wanted is None or a.code in wanted), key=lambda a: a.code)

    async def employees(self, eid: str) -> list[Employee]:
        """工资册, 按员工编号排序 (同真实实现)."""
        return sorted(self.employee_rows, key=lambda e: e.employee_id)

    async def bank_accounts(self, eid: str) -> list[BankAccount]:
        """records 中的 bank_account 行, 按写入顺序."""
        return [BankAccount.model_validate(row) for (kind, _), row in self.records.items() if kind == "bank_account"]

    async def bank_transactions(
        self, eid: str, bank_account_id: str, date_from: date, date_to: date
    ) -> list[BankTransaction]:
        """records 中该账户交易日期在 [date_from, date_to] 内 (含两端) 的 bank_transaction 行, 按 (交易日期, ID) 排序,
        同真实实现."""
        rows = [
            BankTransaction.model_validate(r) for (kind, _), r in self.records.items() if kind == "bank_transaction"
        ]
        found = [r for r in rows if r.bank_account_id == bank_account_id and date_from <= r.txn_date <= date_to]
        return sorted(found, key=lambda r: (r.txn_date, r.id))

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

    async def save_contract(
        self,
        eid: str,
        contract: Contract,
        *,
        changed_by: str = AgentId.A3,
        field: str | None = None,
        reason: str | None = None,
    ) -> None:
        """覆盖合同, 并按 (合同编号, 版本) 记一版 (同版本覆盖), 同真实实现."""
        self.save_calls.append(contract.id)
        self.saved_contracts[contract.id] = contract
        versions = [v for v in self.contract_versions.get(contract.id, []) if v.version != contract.version]
        version = ContractVersion(
            version=contract.version,
            contract=contract,
            field=field,
            reason=reason,
            changed_by=changed_by,
            changed_at=datetime.now(UTC),  # 同真实实现取写入时间
        )
        self.contract_versions[contract.id] = sorted([*versions, version], key=lambda v: v.version)

    async def contract_history(self, eid: str, contract_id: str) -> list[ContractVersion]:
        return list(self.contract_versions.get(contract_id, []))

    async def documents(
        self, eid: str, type: DocumentType | None = None, related_id: str | None = None
    ) -> list[DocumentRef]:
        """登记的文件 (按登记顺序); type 与 related_id 同真实实现, 都给时都须满足."""
        return [
            d
            for d in self.document_refs
            if (type is None or d.type is type) and (related_id is None or related_id in d.related_ids)
        ]


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
        # 画像各版的出处 (id, version) → (来源, 写入人, 写入时间); 构造时给的画像没有, 视同老版本 (未记录)
        self.profile_meta: dict[tuple[str, int], tuple[ProfileSource | None, str | None, datetime]] = {}
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
        dataset: str | None,
        profile: ClientProfile,
        working_language: WorkingLanguage = "en",
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
        self._write(eid, current.model_copy(update={**kept, "updated_at": datetime.now(UTC)}), kept)

    async def set_working_language(self, eid: str, language: WorkingLanguage) -> WorkingLanguage:
        """改工作语言, 返回之前的值; 相同时不改. 不存在 LookupError, 已锁定 EngagementLocked (同 Postgres 的 writable);
        在 output_store 的事务块内调用时加入该事务."""
        current = await self.get(eid)
        if current is None:
            raise LookupError(f"Engagement {eid} not found")
        if current.locked:
            raise EngagementLocked(eid)
        if current.working_language != language:
            changes = {"working_language": language}
            self._write(eid, current.model_copy(update={**changes, "updated_at": datetime.now(UTC)}), changes)
        return current.working_language

    def _write(self, eid: str, updated: Engagement, changes: dict[str, Any]) -> None:
        """写入改后的项目 (update 与 set_working_language 共用): 在 output_store 的事务块内时暂存进该事务, 记下改动
        changes, 块正常结束才生效; 否则立即生效."""
        txn = self._txn()
        if txn is not None:
            txn.engagements[eid] = updated
            txn.log.append(EngagementWrite("update", eid, changes))
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

    async def save_profile(
        self,
        profile: ClientProfile,
        *,
        source: ProfileSource,
        updated_by: str | None,
        dataset_profiles: Sequence[ClientProfile] | None = None,
    ) -> ClientProfile:
        """version 须为已有最大值 +1; 记下该版的出处. 给了 dataset_profiles 时另查 ABN 是否已是别的客户的 (同真实
        实现: 库里其他客户的最新一版加还没进库的数据集画像)."""
        if dataset_profiles is not None:
            await self.check_abn(profile, dataset_profiles)
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
        self.profile_meta[(profile.id, profile.version)] = (source, updated_by, datetime.now(UTC))
        return profile

    def record(self, profile: ClientProfile) -> ProfileRecord:
        source, updated_by, updated_at = self.profile_meta.get((profile.id, profile.version), (None, None, NOW))
        return ProfileRecord(profile=profile, source=source, updated_by=updated_by, updated_at=updated_at)

    async def profile_history(self, client_id: str) -> list[ProfileRecord]:
        """该客户画像的各版及出处, 按版本升序."""
        txn = self._txn()
        visible = self.profiles + (txn.profiles if txn is not None else [])
        return [self.record(p) for p in sorted((p for p in visible if p.id == client_id), key=lambda p: p.version)]

    async def clients(self) -> list[ClientEntry]:
        """各客户 (按 id 排序) 的最新一版与第 1 版的来源."""
        entries: list[ClientEntry] = []
        for client_id in sorted({p.id for p in self.profiles}):
            history = await self.profile_history(client_id)
            latest = history[-1]
            entries.append(ClientEntry(profile=latest.profile, first_source=history[0].source))
        return entries

    async def create_client(
        self, profile: ClientProfile, *, created_by: str, dataset_profiles: Sequence[ClientProfile]
    ) -> ClientProfile:
        """同 PostgresEngagementStore.create_client: 版本须为 1, ABN 不得已是别的客户的 (库里最新一版与还没进库的
        数据集画像)."""
        if profile.version != 1:
            raise VersionConflict(profile.id)
        return await self.save_profile(
            profile, source=ProfileSource.manual, updated_by=created_by, dataset_profiles=dataset_profiles
        )

    async def check_abn(self, profile: ClientProfile, others: Sequence[ClientProfile]) -> None:
        """同真实实现: 其他客户的最新一版加还没进库的 others, ABN 去空白后比较, 不含本客户自己; 撞上抛 AbnTaken."""
        stored = [entry.profile for entry in await self.clients()]
        known = {p.id for p in stored}
        for other in [*stored, *(p for p in others if p.id not in known)]:
            if other.id != profile.id and normalize_abn(other.abn) == normalize_abn(profile.abn):
                raise AbnTaken(profile.abn, other.id, other.legal_name)

    def commit(self, txn: FakeTxn) -> None:
        """FakeOutputStore 提交事务时调用: 暂存的项目改动与画像生效."""
        for engagement in txn.engagements.values():
            self._apply_engagement(engagement)
        self.profiles.extend(txn.profiles)

    async def list(self, owner_id: str | None = None) -> list[Engagement]:
        """owner_id 为空列全部 (供 recover), 否则只列该用户的项目."""
        return [e for e in self.engagements if owner_id is None or e.owner_id == owner_id]


class CreatingEngagementStore(FakeEngagementStore):
    """能建项目的假项目存储: 编号 ENG-2026-001 起顺排; 画像与该客户的各版比较内容, 有一版相同就沿用最新一版, 都不同
    才出新版本 (同 PostgresEngagementStore.create 的效果)."""

    async def create(
        self,
        *,
        owner_id: str,
        period_start: date,
        period_end: date,
        dataset: str | None,
        profile: ClientProfile,
        working_language: WorkingLanguage = "en",
    ) -> Engagement:
        if period_end < period_start:
            raise ValueError("period_end precedes period_start")
        versions = sorted((p for p in self.profiles if p.id == profile.id), key=lambda p: p.version)
        if not versions:
            await self.save_profile(profile, source=ProfileSource.dataset, updated_by=owner_id)
        elif any(v.model_copy(update={"version": profile.version}) == profile for v in versions):
            profile = versions[-1]
        else:
            profile = profile.model_copy(update={"version": versions[-1].version + 1})
            await self.save_profile(profile, source=ProfileSource.dataset, updated_by=owner_id)
        engagement = Engagement(
            id=f"ENG-2026-{len(self.engagements) + 1:03d}",
            client_id=profile.id,
            period_start=period_start,
            period_end=period_end,
            dataset=dataset,
            owner_id=owner_id,
            client_profile_version=profile.version,
            phase=Phase.P0_Acceptance,
            working_language=working_language,
            created_at=NOW,
            updated_at=NOW,
        )
        self.engagements.append(engagement)
        return engagement


async def evidence_pointers(
    deps: AgentDeps, eid: str = ENGAGEMENT.id, *, everything: bool = False
) -> list[EvidencePointer]:
    """项目里判断 (推理链与证据), 合同与函证的证据指针. everything 时另含例外, 承接评分卡的登记检查与复核决定的人工
    结果, 即存储中全部带指针的对象 (R56 验收 2; 假存储只预置了部分试算表行, 例外指针在假实现下解析不全). 走通的
    汇总 (tests/live_walk) 与 R56 的检查共用."""
    store = deps.output_store
    pointers: list[EvidencePointer] = []
    for j in await store.latest(eid, Judgment):
        pointers += [*j.evidence, *(p for step in j.reasoning_chain for p in step.evidence)]
    if everything:
        for x in await store.latest(eid, AuditException):
            pointers += x.evidence
        for card in await store.latest(eid, AcceptanceScorecard):
            pointers += [check.evidence for check in card.registry_checks]
        for r in await store.latest(eid, ReviewRecord):
            pointers += r.manual_result.evidence if r.manual_result is not None else []
    for c in await deps.financial_store.contracts(eid):
        pointers += c.evidence
    for c in await store.latest(eid, Confirmation):
        pointers += c.evidence
    return pointers


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
        self.contents: dict[str, bytes] = {}  # save 收到的原件, 文件 ID → 内容; path 据此写出
        # 文件名 → 固定的文件 ID: 让上传的假文件用固定输出里的占位 ID (如 CON-001.pdf → DOC-CON001000000)
        self.ids: dict[str, str] = {}
        self.directory: Path | None = None  # path 写原件的临时目录
        self.updates: list[tuple[str, DocumentType, str, str | None]] = []  # update_document 的每次调用
        self.detail_updates: list[tuple[str, bool]] = []  # update_details 的每次调用 (文件 ID, 是否在事务内)

    async def save(
        self,
        eid: str,
        file_name: str,
        data: bytes,
        *,
        type: DocumentType,
        reliability: Reliability,
        media_type: str = "application/pdf",
        channel: DocumentChannel = "auditor_upload",
        uploaded_by: str | None = None,
        status: DocumentStatus = "received",
    ) -> DocumentRef:
        """登记为 DOC-{sha256 前 12 位}, 状态取 status (缺省 received); 同一哈希已登记则返回已有记录 (接口文档第 4 节).

        页数取 page_counts, 缺省 2 (非 PDF 记 1). saves 记下每次调用的文件名, save_args 记下文件名, 内容哈希,
        类型与可靠性.
        """
        self.saves.append(file_name)
        sha256 = hashlib.sha256(data).hexdigest()
        self.save_args.append((file_name, sha256, type, reliability))
        refs = self.financial_store.document_refs
        existing = next((d for d in refs if d.sha256 == sha256), None)
        if existing is not None:
            return existing
        document_id = self.ids.get(file_name, f"DOC-{sha256[:12]}")
        self.contents[document_id] = data
        doc = DocumentRef(
            id=document_id,
            type=type,
            file_name=file_name,
            sha256=sha256,
            page_count=(self.page_counts.get(file_name, 2) if data.startswith(b"%PDF") else 0)
            if media_type == "application/pdf"
            else 1,  # 同真实实现: 读不出的 PDF (不以 %PDF 开头) 记 0 页, 非 PDF 记 1
            reliability=reliability,
            status=status,
            uploaded_at=NOW,
            media_type=media_type,
            channel=channel,
            uploaded_by=uploaded_by,
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

    async def update_document(
        self,
        eid: str,
        document_id: str,
        *,
        type: DocumentType,
        reliability: Reliability,
        status: DocumentStatus,
        failed_step: DocumentFailedStep | None,
    ) -> DocumentRef:
        """改登记的类型, 可靠性, 状态与卡住的步骤 (不加入事务), 记进 updates."""
        refs = self.financial_store.document_refs
        i = next((i for i, d in enumerate(refs) if d.id == document_id), None)
        if i is None:
            raise FileNotFoundError(document_id)
        refs[i] = refs[i].model_copy(
            update={"type": type, "reliability": reliability, "status": status, "failed_step": failed_step}
        )
        self.updates.append((document_id, type, status, failed_step))
        return refs[i]

    async def update_details(
        self,
        eid: str,
        document_id: str,
        *,
        reliability: Reliability | None,
        document_date: date | None,
        related_ids: Sequence[str] | None,
    ) -> DocumentRef:
        """补登记: 只改给了的项, 返回改后的登记; 在审计产出事务块内调用时加入该事务 (提交才生效, 同 set_status),
        记进 detail_updates. 没有该文件抛 FileNotFoundError."""
        refs = self.financial_store.document_refs
        i = next((i for i, d in enumerate(refs) if d.id == document_id), None)
        if i is None:
            raise FileNotFoundError(document_id)
        given = {"reliability": reliability, "document_date": document_date, "related_ids": related_ids}
        changes = {k: list(v) if k == "related_ids" else v for k, v in given.items() if v is not None}
        updated = refs[i].model_copy(update=changes)

        def apply() -> None:
            refs[i] = refs[i].model_copy(update=changes)

        txn = self.output_store.active_txn() if self.output_store is not None else None
        self.detail_updates.append((document_id, txn is not None))
        if txn is None:
            apply()
        else:
            txn.on_commit.append(apply)
        return updated

    async def path(self, eid: str, document_id: str) -> Path:
        """save 收到的原件写到临时目录 (每个实例一个, 实例回收或进程退出时删掉) 后返回路径; 没有内容 (预置的登记)
        抛 FileNotFoundError."""
        if document_id not in self.contents:
            raise FileNotFoundError(document_id)
        if self.directory is None:
            self.directory = Path(tempfile.mkdtemp(prefix="fake-evidence-"))
            weakref.finalize(self, shutil.rmtree, self.directory, ignore_errors=True)
        doc = next(d for d in self.financial_store.document_refs if d.id == document_id)
        path = self.directory / f"{document_id}{Path(doc.file_name).suffix.lower()}"
        path.write_bytes(self.contents[document_id])
        return path

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
# PLN-07 A03 的取数 (合成数据集设计第 3.2 节月度趋势): 本期按月的收入, 费用与员工费用, 各列合计同本期试算表
# (收入 1200000000, 费用 1096400000, 员工费用 118000000)
NUMBAT_A03_ROWS = [
    {"month": month, "revenue_cents": revenue, "expense_cents": expense, "employee_expense_cents": employee}
    for month, revenue, expense, employee in [
        (date(2025, 7, 1), 88000000, 77321600, 9566600),
        (date(2025, 8, 1), 90000000, 78721600, 9566600),
        (date(2025, 9, 1), 94000000, 81521600, 9566600),
        (date(2025, 10, 1), 98000000, 84321600, 9566600),
        (date(2025, 11, 1), 104000000, 88521600, 9566600),
        (date(2025, 12, 1), 86000000, 75921600, 9566600),
        (date(2026, 1, 1), 80000000, 71721600, 9566600),
        (date(2026, 2, 1), 94000000, 81521600, 9566600),
        (date(2026, 3, 1), 111000000, 93421600, 9566600),
        (date(2026, 4, 1), 97500000, 83971600, 9566600),
        (date(2026, 5, 1), 107500000, 90971600, 9566600),
        (date(2026, 6, 1), 150000000, 188462400, 12767400),
    ]
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
            "basis": "balance_sheet",
            "current_cents": 202500000,
            "prior_cents": 150000000,
            "opening_cents": 150000000,
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


def gen05_exception(
    run: ProcedureRun,
    rule_id: str,
    severity: Literal["low", "medium", "high"],
    source_id: str,
    amount_cents: int,
    detail: dict[str, Any],
    number: int = 1,
) -> AuditException:
    """GEN-05 一条规则的第 number 条例外, 字段同 SqlRuleEngine 按 GEN-05.yaml 的输出列组装: detail 为 source_type,
    source_id, amount_cents 之外的列 (entry_no, posting_date 与规则特有的列), 证据指针指向该分录行."""
    return AuditException(
        id=f"X-GEN-05-{run.run_no}-{rule_id}-{number}",
        version=1,
        procedure_id="GEN-05",
        run_id=run.id,
        rule_id=rule_id,
        severity=severity,
        source_ids=[source_id],
        amount_cents=amount_cents,
        detail=detail,
        evidence=[
            EvidencePointer(
                type="voucher",
                source_type="journal_entry",
                source_id=source_id,
                locator=", ".join(["amount_cents", *detail]),
                reliability="internal_original",
            )
        ],
    )


def numbat_gen05_result(run: ProcedureRun) -> RuleResult:
    """GEN-05 对 Numbat 的结果 (需求 R131 验收 8, R132 验收 8, 取自连库跑一次的结果): SJ-2026-0612 悉尼周日 21:14
    录入, 自审自批, 命中 R01 (收入行), R04 与 R08 (两行同额取 ID 小的); 画像没有审批限额, R03 不执行; 还没发出错报
    沟通, R06 不执行; 没有数据集声明, 也就没有作废冲回格式, R11 不执行. R09 (没有调整分录), R10, R12 (发票都有发运单)
    执行而 0 条."""
    entry = {"entry_no": "SJ-2026-0612", "posting_date": "2026-06-28"}
    local = {"created_at_local": "2026-06-28T21:14:00"}
    return RuleResult(
        exceptions=[
            gen05_exception(run, "R01", "high", "SJ-2026-0612-2", 14200000, entry | local),
            gen05_exception(
                run,
                "R04",
                "high",
                "SJ-2026-0612-1",
                14200000,
                entry | {"created_by": "fin.controller", "approved_by": "fin.controller"},
            ),
            gen05_exception(run, "R08", "medium", "SJ-2026-0612-1", 14200000, entry | local),
        ],
        rows={},
        outcomes=[
            RuleOutcome(rule_id="R03", status="not_executable", reason="客户画像没有审批限额"),
            RuleOutcome(rule_id="R06", status="skipped", reason="尚未发出错报沟通, 本规则只在增量实例执行"),
            RuleOutcome(rule_id="R11", status="skipped", reason="数据集没有声明作废冲回格式"),
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


def engine_result(procedure_id: str, result: RuleResult, recorded: Sequence[RulePopulation] = ()) -> RuleResult:
    """预置的规则结果补上被测总体 (需求 R324), 口径同 SqlRuleEngine: 按程序 YAML 的规则顺序, 没有执行的规则 (outcomes)
    不记; emit rows 的规则以预置的 rows 为总体 (rule_engine.rule_population); 写了总体 SQL 的规则取 recorded 中同规则
    编号的 (须给, 缺了抛 KeyError); 其余 emit exception 的规则记为未记录."""
    template = knowledge_library(KNOWLEDGE).load(procedure_id)
    skipped = {o.rule_id for o in result.outcomes}
    given = {p.rule_id: p for p in recorded}
    populations = []
    for rule in template.rules:
        if rule.id in skipped:
            continue
        if rule.emit == "rows":
            populations.append(rule_population(rule.id, result.rows[rule.id]))
        elif rule.population is not None:
            populations.append(given[rule.id])
        else:
            populations.append(RulePopulation(rule_id=rule.id))
    return result.model_copy(update={"populations": populations})


def receipt_matches(invoice_no: str, reference: str | None) -> bool:
    """流水参考号对得上发票号 (REV-08 R03 的收款对应, 测试里独立写一遍): 包含发票号; 或发票号为字母前缀加纯数字时,
    参考号里出现该数字串且前后都不是数字."""
    if reference is None:
        return False
    if invoice_no in reference:
        return True
    digits = re.fullmatch(r"[A-Za-z]+([0-9]+)", invoice_no)
    return digits is not None and re.search(rf"(^|[^0-9]){digits.group(1)}([^0-9]|$)", reference) is not None


def rev08_populations(
    invoices: Iterable[SalesInvoice],
    notes: Iterable[DeliveryNote],
    transactions: Iterable[BankTransaction],
    period_end: date,
    window_days: int,
) -> list[RulePopulation]:
    """REV-08 R01, R02, R03 的被测总体, 按 REV-08.yaml 总体 SQL 的口径由表独立算一遍: 期末前 window_days 天 (两端都含)
    入账的发票, 按发票 ID, 每张一项; R01 为有服务完成记录的, R02 为全部, records 为记录数组 (按记录 ID, 每条
    {delivery_no, signed_date, signed_by}, 没签收的为 None; 没有记录的为 None); R03 为找得到收款的 (正数流水, 见
    receipt_matches), 带收款流水 ID 与收款日 (逗号连接, 按流水 ID) 及收款合计."""
    start = period_end - timedelta(days=window_days)
    window = sorted((i for i in invoices if start <= i.posting_date <= period_end), key=lambda i: i.id)
    recorded: dict[str, list[DeliveryNote]] = {}
    for note in sorted(notes, key=lambda n: n.id):
        recorded.setdefault(note.invoice_id, []).append(note)

    def item(invoice: SalesInvoice, **extra: Any) -> PopulationItem:
        fields = {
            "invoice_no": invoice.invoice_no,
            "customer": invoice.customer,
            "contract_id": invoice.contract_id,
            "posting_date": invoice.posting_date.isoformat(),
            "journal_entry_id": invoice.journal_entry_id,
        }
        return PopulationItem(source_id=invoice.id, amount_cents=invoice.amount_cents, fields=fields | extra)

    def with_notes(invoice: SalesInvoice) -> PopulationItem:
        records = [
            {
                "delivery_no": n.delivery_no,
                "signed_date": n.signed_date.isoformat() if n.signed_date else None,
                "signed_by": n.signed_by,
            }
            for n in recorded.get(invoice.id, [])
        ]
        return item(invoice, records=records or None)

    r03 = []
    for invoice in window:
        paid = sorted(
            (t for t in transactions if t.amount_cents > 0 and receipt_matches(invoice.invoice_no, t.reference)),
            key=lambda t: t.id,
        )
        if paid:
            r03.append(
                item(
                    invoice,
                    bank_transaction_ids=",".join(t.id for t in paid),
                    received_dates=",".join(t.txn_date.isoformat() for t in paid),
                    received_cents=sum(t.amount_cents for t in paid),
                )
            )
    items = {
        "R01": [with_notes(i) for i in window if i.id in recorded],
        "R02": [with_notes(i) for i in window],
        "R03": r03,
    }
    return [
        RulePopulation(
            rule_id=rule_id,
            source_type="sales_invoice" if found else None,
            items=found,
            count=len(found),
            amount_cents=total_cents(i.amount_cents for i in found),
        )
        for rule_id, found in items.items()
    ]


def no_population(value: Any) -> bool:
    """片段或状态的值里没有被测总体 RunPopulation (需求 R324, 逐层看列表与字典): 总体只在 Store, 不进片段与状态."""
    if isinstance(value, RunPopulation):
        return False
    if isinstance(value, dict):
        return all(no_population(v) for v in value.values())
    if isinstance(value, list):
        return all(no_population(v) for v in value)
    return True


def csh05_population(
    accounts: Iterable[BankAccount],
    transactions: Iterable[BankTransaction],
    trial_balance: Iterable[TrialBalanceRow],
    period_end: date,
) -> RulePopulation:
    """CSH-05 B01 的被测总体, 按 CSH-05.yaml 总体 SQL 的口径由表独立算一遍 (需求 R327): 全部银行账户, 按 ID, 每户一项;
    金额为该户总账科目的本期末余额 (没有试算表行为空); 另带开户行, 账号, 总账科目, 期末对账单余额与最后一笔流水
    (流水日不晚于期末, 按 (流水日, ID) 取最后一笔; 没有为空)."""
    current = {r.account_code: r.balance_cents for r in trial_balance if r.period == "current"}
    lines = list(transactions)
    items = []
    for account in sorted(accounts, key=lambda a: a.id):
        own = [t for t in lines if t.bank_account_id == account.id and t.txn_date <= period_end]
        last = max(own, key=lambda t: (t.txn_date, t.id), default=None)
        fields = {
            "bank_name": account.bank_name,
            "account_no": account.account_no,
            "gl_account_code": account.gl_account_code,
            "statement_balance_cents": last.balance_after_cents if last else None,
            "last_bank_transaction_id": last.id if last else None,
        }
        items.append(
            PopulationItem(source_id=account.id, amount_cents=current.get(account.gl_account_code), fields=fields)
        )
    return RulePopulation(
        rule_id="B01",
        source_type="bank_account" if items else None,
        items=items,
        count=len(items),
        amount_cents=total_cents(i.amount_cents for i in items),
    )


def months_between(start: date, end: date) -> int:
    """start 到 end 的整月数 (同 PostgreSQL age() 的年 x 12 加月)."""
    return (end.year - start.year) * 12 + end.month - start.month - (1 if end.day < start.day else 0)


def add_months(day: date, months: int) -> date:
    """日期加整月, 落到月底之外时取当月最后一天 (同 PostgreSQL 的 date + interval)."""
    total = day.year * 12 + day.month - 1 + months
    year, month = divmod(total, 12)
    last = (date(year + (month + 1) // 12, (month + 1) % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month + 1, min(day.day, last))


def pln07_a01_population(
    accounts: Iterable[Account],
    trial_balance: Iterable[TrialBalanceRow],
    period_start: date,
    period_end: date,
    prior_period_start: date | None,
) -> RulePopulation:
    """PLN-07 A01 的被测总体, 按 PLN-07.yaml 总体 SQL 的口径由表独立算一遍 (需求 R327): 两期试算表出现过且在科目表里的
    科目, 资产负债类都比较 (基数为期初: 上期末余额, 留存收益分类中代码最小的科目另加上期全部损益), 损益类只在两期
    等长 (上期起始日加本期整月数等于本期起始日, 上期起始日为空视为等长) 时比较 (基数为上期). 每科目一项, 按代码;
    没有金额 (余额有借有贷, 合计没有意义), 识别字段为科目名称, 比较口径与本期, 上期, 期初余额."""
    chart = {a.code: a for a in accounts}
    current: dict[str, int] = {}
    prior: dict[str, int] = {}
    for row in trial_balance:
        if row.account_code in chart:
            side = current if row.period == "current" else prior
            side[row.account_code] = side.get(row.account_code, 0) + row.balance_cents
    codes = sorted(current.keys() | prior.keys())
    retained = [c for c in codes if chart[c].category == "retained_earnings"]
    carried = sum(prior.get(c, 0) for c in codes if chart[c].type in ("revenue", "expense"))
    same_length = (
        prior_period_start is None
        or add_months(prior_period_start, months_between(period_start, period_end + timedelta(days=1))) == period_start
    )
    items = []
    for code in codes:
        profit_or_loss = chart[code].type in ("revenue", "expense")
        if profit_or_loss and not same_length:
            continue
        opening = prior.get(code, 0) + (carried if retained and code == retained[0] else 0)
        fields = {
            "account_name": chart[code].name,
            "basis": "profit_or_loss" if profit_or_loss else "balance_sheet",
            "current_cents": current.get(code),
            "prior_cents": prior.get(code),
            "opening_cents": opening,
        }
        items.append(PopulationItem(source_id=code, amount_cents=None, fields=fields))
    return RulePopulation(
        rule_id="A01",
        source_type="trial_balance" if items else None,
        items=items,
        count=len(items),
        amount_cents=total_cents(i.amount_cents for i in items),
    )


@functools.cache
def numbat_csh05_population() -> RulePopulation:
    """Numbat CSH-05 B01 的被测总体 (期末 2026-06-30), 由演示数据集的银行账户, 流水与试算表算出."""
    return csh05_population(
        numbat_records("bank_accounts", BankAccount),
        numbat_bank_transactions(),
        numbat_records("trial_balance", TrialBalanceRow),
        ENGAGEMENT.period_end,
    )


@functools.cache
def numbat_pln07_a01_population() -> RulePopulation:
    """Numbat PLN-07 A01 的被测总体: 本期与上期等长 (没有数据集声明的上期起始日), 资产负债类与损益类都比较; 科目
    分类同载入 (按名称映射, account_mapping.with_category)."""
    return pln07_a01_population(
        [with_category(a) for a in numbat_records("accounts", Account)],
        numbat_records("trial_balance", TrialBalanceRow),
        ENGAGEMENT.period_start,
        ENGAGEMENT.period_end,
        None,
    )


@functools.cache
def numbat_rev08_populations() -> list[RulePopulation]:
    """Numbat REV-08 的被测总体 (期末 2026-06-30, 特别风险的窗口 20 天), 由演示数据集的发票, 发运单与流水算出."""
    return rev08_populations(
        numbat_records("sales_invoices", SalesInvoice),
        numbat_records("delivery_notes", DeliveryNote),
        numbat_bank_transactions(),
        ENGAGEMENT.period_end,
        20,
    )


NUMBAT_RULES: dict[str, Callable[[ProcedureRun], RuleResult]] = {  # 规则引擎对 Numbat 的结果, 按程序编号
    "PLN-07": lambda run: engine_result(
        "PLN-07",
        RuleResult(exceptions=[numbat_pln07_exception(run)], rows={"A02": NUMBAT_A02_ROWS, "A03": NUMBAT_A03_ROWS}),
        [numbat_pln07_a01_population()],
    ),
    "PLN-01": lambda run: engine_result("PLN-01", RuleResult(exceptions=[], rows={"M01": NUMBAT_M01_ROWS})),
    "REV-08": lambda run: engine_result(
        "REV-08", RuleResult(exceptions=[numbat_rev08_exception(run)], rows={}), numbat_rev08_populations()
    ),
    "REV-02": lambda run: engine_result("REV-02", RuleResult(exceptions=[], rows={"S01": NUMBAT_S01_ROWS})),
    "CSH-05": lambda run: engine_result(
        "CSH-05", RuleResult(exceptions=[numbat_csh05_exception(run)], rows={}), [numbat_csh05_population()]
    ),
    "CSH-02": lambda run: engine_result(
        "CSH-02",
        RuleResult(exceptions=[], rows={"D02": NUMBAT_D02_ROWS, "D03": NUMBAT_D03_ROWS, "D04": NUMBAT_D04_ROWS}),
    ),
    "GEN-05": lambda run: engine_result("GEN-05", numbat_gen05_result(run)),  # 功能 journals 打开时才执行
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


Outcome = Callable[[JudgmentContext, ProcedureRun], JudgmentOutcome]  # FakeJudgmentEngine 对一个对象的预置结果


def judgment(ctx: JudgmentContext, run: ProcedureRun, **changes: Any) -> Judgment:
    """判断引擎会给出的判断: 缺省 accept, L2, 解释对象的全部规则例外; changes 改其中的字段."""
    base = Judgment(
        id=f"J-{run.procedure_id}-{run.run_no}-{ctx.subject_id}",
        procedure_id=run.procedure_id,
        run_id=run.id,
        subject_id=ctx.subject_id,
        conclusion="ok",
        confidence=0.9,
        confidence_parts={"llm": 0.9},
        reasoning_chain=[],
        evidence=[],
        counter_argument="none",
        proposed_action=ProposedAction.accept,
        exception_ids=[x.id for x in ctx.rule_findings],
        autonomy_level=AutonomyLevel.L2,
        performed_by="U-1",
        created_at=NOW,
    )
    return base.model_copy(update=changes)


def passed(**changes: Any) -> Outcome:
    """判断成功 (judgment, 一次调用)."""
    return lambda ctx, run: JudgmentOutcome(
        judgment=judgment(ctx, run, **changes), failure=None, attempts=1, trail_ids=["TR-1"]
    )


FailureKind = Literal["failed", "unattributed"]


def failed(kind: FailureKind) -> Outcome:
    """判断失败 (failure 为 kind), 用满 MAX_ATTEMPTS 次调用."""
    trail = [f"TR-{n}" for n in range(1, MAX_ATTEMPTS + 1)]
    return lambda ctx, run: JudgmentOutcome(judgment=None, failure=kind, attempts=MAX_ATTEMPTS, trail_ids=trail)


def fails_first(times: int, kind: FailureKind = "unattributed") -> Outcome:
    """前 times 次判断失败 (failed(kind)), 之后成功 (passed())."""
    left = [times]

    def outcome(ctx: JudgmentContext, run: ProcedureRun) -> JudgmentOutcome:
        if left[0] > 0:
            left[0] -= 1
            return failed(kind)(ctx, run)
        return passed()(ctx, run)

    return outcome


class FakeConfirmationChannel:
    """假函证平台: 按账户返回预置回执 (回函内容为 "{账号} reply"), 同一幂等键返回第一次的同一回执; submits 记下每次调用.

    channel 为回函渠道; replies 为假时回执没有回函; body 非空时各账户的回函内容都取它 (可为 b""); bodies 按账号给
    各账户的回函内容 (如 Xero 演示的回函 PDF), 没给的账户照前两条.
    """

    def __init__(
        self,
        channel: ReceiptChannel = ReceiptChannel.direct_email,
        replies: bool = True,
        body: bytes | None = None,
        bodies: Mapping[str, bytes] | None = None,
    ) -> None:
        self.channel = channel  # 回函渠道
        self.replies = replies  # 是否回函
        self.body = body  # 统一的回函内容, 为空时按账号生成
        self.bodies = dict(bodies or {})  # 账号 → 回函内容, 优先于 body
        self.receipts: dict[str, ConfirmationReceipt] = {}  # 幂等键 -> 第一次的回执
        self.submits: list[tuple[str, ConfirmationRequest]] = []  # (幂等键, 请求), 按调用顺序

    async def submit(self, eid: str, req: ConfirmationRequest, idempotency_key: str) -> ConfirmationReceipt:
        self.submits.append((idempotency_key, req))
        if idempotency_key not in self.receipts:
            self.receipts[idempotency_key] = ConfirmationReceipt(
                sent_at=datetime(2026, 7, 1, 9, len(self.receipts), tzinfo=UTC),
                replied_at=date(2026, 7, 8) if self.replies else None,
                reply_file_name=f"{req.account_no}.pdf" if self.replies else None,
                reply_bytes=self.reply_body(req.account_no) if self.replies else None,
                receipt_channel=self.channel if self.replies else None,
            )
        return self.receipts[idempotency_key]

    def reply_body(self, account_no: str) -> bytes:
        """回函内容: bodies 中的, 否则 body, 否则 "{账号} reply"."""
        if account_no in self.bodies:
            return self.bodies[account_no]
        return self.body if self.body is not None else f"{account_no} reply".encode()


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
    """假规则引擎: 按程序编号返回预置结果 (按本次执行生成, 例外 ID 随 run_no), 没有预置的程序返回空结果. 不执行
    检查; 要模拟没有执行的规则时, 预置的 RuleResult 带上 outcomes (需求 R130). 被测总体 (需求 R324) 也取预置的,
    NUMBAT_RULES 与 XERO_RULES 经 engine_result 补齐; 没带总体的预置 (多为单个用例自己给的) 总体为空列表.

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


class FakeXeroConnectionStore:
    """假 Xero 连接存储 (XeroConnectionStore): 项目 → (连接, 令牌密文). calls 记下各写方法的调用 (方法名, 项目)."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[XeroConnection, str]] = {}
        self.calls: list[tuple[str, str]] = []

    async def get(self, eid: str) -> XeroConnection | None:
        found = self.rows.get(eid)
        return found[0] if found else None

    async def tokens(self, eid: str) -> str | None:
        found = self.rows.get(eid)
        return found[1] if found else None

    async def put(self, eid: str, connection: XeroConnection, tokens_encrypted: str) -> None:
        self.calls.append(("put", eid))
        self.rows[eid] = (connection, tokens_encrypted)

    async def update(self, eid: str, connection: XeroConnection) -> None:
        self.calls.append(("update", eid))
        if eid not in self.rows:
            raise LookupError(f"{eid} 没有 Xero 连接")
        self.rows[eid] = (connection, self.rows[eid][1])

    async def set_tokens(self, eid: str, tokens_encrypted: str, refreshed_at: datetime) -> None:
        self.calls.append(("set_tokens", eid))
        if eid not in self.rows:
            raise LookupError(f"{eid} 没有 Xero 连接")
        connection = self.rows[eid][0].model_copy(update={"refreshed_at": refreshed_at})
        self.rows[eid] = (connection, tokens_encrypted)

    async def delete(self, eid: str) -> bool:
        self.calls.append(("delete", eid))
        return self.rows.pop(eid, None) is not None


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
    """假数据集载入: load 记下调用, 把 Numbat 的 3 份合同登记为证据文件 (received); make_deps 建好即 seed 一次.

    预置计划数据取 Numbat 的. 更正分录取构造时预置的 adjustments: load_adjustments 记下调用, 返回所请求凭证的行
    (按 id 排序, 凭证号不在预置中抛 LookupError), 不写假财务存储; 没有预置 (None, 同数据集没有更正分录文件) 时抛
    FileNotFoundError. adjustment_entries 与 adjustment_entry_nos 按预置行, 凭证号自然序, 同真实实现.
    数据集声明取构造时预置的 traits (数据集名 → 声明), 没预置的数据集为缺省值; dataset_traits 记下调用的数据集名.
    """

    def __init__(
        self,
        financial_store: FakeFinancialStore,
        adjustments: list[JournalEntry] | None = None,
        traits: dict[str, DatasetTraits] | None = None,
    ) -> None:
        self.financial_store = financial_store  # 合同登记到这里
        self.loads: list[tuple[str, str]] = []  # (项目编号, 数据集名), 按调用顺序
        self.adjustments = adjustments  # 预置的更正分录行; None 表示数据集没有更正分录文件
        self.adjustment_loads: list[tuple[str, str, list[str] | None]] = []  # (项目编号, 数据集名, 凭证号)
        self.adjustment_actors: list[str] = []  # 每次 load_adjustments 收到的写入人 actor, 顺序同 adjustment_loads
        self.traits = dict(traits or {})  # 数据集名 → 声明, 可在测试中改
        self.traits_calls: list[str] = []  # dataset_traits 收到的数据集名, 按调用顺序

    async def load_adjustments(
        self, eid: str, dataset: str, entry_nos: Sequence[str] | None = None, *, actor: str = "A1"
    ) -> list[JournalEntry]:
        self.adjustment_loads.append((eid, dataset, list(entry_nos) if entry_nos is not None else None))
        self.adjustment_actors.append(actor)
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

    def dataset_traits(self, dataset: str) -> DatasetTraits:
        self.traits_calls.append(dataset)
        return self.traits.get(dataset, DatasetTraits())

    async def load(self, eid: str, dataset: str) -> None:
        self.loads.append((eid, dataset))
        self.seed()

    def seed(self) -> "FakeDatasetLoader":
        """模拟种子脚本已载入数据集 (P1 不再自动载入, 需求 R10): 把 Numbat 的 3 份合同登记为证据文件 (已登记的跳过),
        不记进 loads. 返回自身, 供 make_deps 链式调用."""
        known = {d.id for d in self.financial_store.document_refs}
        self.financial_store.document_refs += [d for d in NUMBAT_CONTRACTS if d.id not in known]
        return self

    def planning_presets(self, dataset: str) -> tuple[list[AuditObjective], list[RiskItem]]:
        return list(NUMBAT_OBJECTIVES), list(NUMBAT_RISKS)

    def client_profile(self, dataset: str) -> ClientProfile:
        return NUMBAT_PROFILE

    def datasets(self) -> list[str]:
        return ["demo"]

    def period(self, dataset: str) -> tuple[date, date] | None:
        return None  # 同 Numbat 的 CSV 数据集, 期间由审计师填


# 种子已载入整套数据集时各账表的行数 (Numbat 的量级, 只用于就绪判断与概览)
SEEDED_STATS: dict[TableKind, TableStats] = {
    TableKind.accounts: TableStats(records=23),
    TableKind.trial_balance: TableStats(records=46, current=23, prior=23),
    TableKind.journal_entries: TableStats(records=2686),
    TableKind.sales_invoices: TableStats(records=624),
    TableKind.bank_accounts: TableStats(records=2),
    TableKind.bank_transactions: TableStats(records=696),
    TableKind.delivery_notes: TableStats(records=624),
    TableKind.approval_log: TableStats(records=0),
}


DEMO_DATASET = Path(__file__).resolve().parents[1] / "data" / "demo"  # Numbat 演示数据集 (CSV)


@functools.cache
def numbat_tables() -> dict[TableKind, tuple[BaseModel, ...]]:
    """Numbat 演示数据集的科目表 (按名称归类), 试算表与总账 (读一次缓存), 体检汇总用."""
    from backend.contracts import records
    from backend.services.account_mapping import with_category

    kinds: dict[TableKind, type[BaseModel]] = {
        TableKind.accounts: records.Account,
        TableKind.trial_balance: records.TrialBalanceRow,
        TableKind.journal_entries: records.JournalEntry,
    }
    tables: dict[TableKind, tuple[BaseModel, ...]] = {}
    for kind, model in kinds.items():
        with (DEMO_DATASET / f"{kind}.csv").open(newline="", encoding="utf-8") as stream:
            rows = [model.model_validate({k: v or None for k, v in r.items()}) for r in csv.DictReader(stream)]
        tables[kind] = tuple(with_category(r) if isinstance(r, records.Account) else r for r in rows)
    return tables


@functools.cache
def numbat_records[M: BaseModel](name: str, model: type[M]) -> tuple[M, ...]:
    """Numbat 演示数据集的一张表 (data/demo/{name}.csv, 空串为空值, 读一次缓存)."""
    with (DEMO_DATASET / f"{name}.csv").open(newline="", encoding="utf-8") as stream:
        return tuple(model.model_validate({k: v or None for k, v in r.items()}) for r in csv.DictReader(stream))


def numbat_bank_transactions() -> tuple[BankTransaction, ...]:
    """Numbat 演示数据集的银行流水 (data/demo/bank_transactions.csv, 读一次缓存)."""
    return numbat_records("bank_transactions", BankTransaction)


def csh02_voided(lines: Iterable[JournalEntry], pattern: str | None, template: str | None) -> set[str]:
    """CSH-02 D03, D04 要剔除的作废对的凭证号, 口径同两条规则的 WITH 段: 冲回凭证为任一行说明匹配 pattern 的凭证,
    原凭证号为 template 中的 {1} 换成第一个捕获组 (pattern 没有捕获组时为整个匹配, 同 PostgreSQL 的 substring);
    pattern 为空时不剔除, template 为空时只剔除冲回凭证."""
    if pattern is None:
        return set()
    regex = re.compile(pattern)
    voided: set[str] = set()
    for line in lines:
        found = regex.search(line.description) if line.description is not None else None
        if found is None:
            continue
        voided.add(line.entry_no)
        part = found.group(1) if regex.groups else found.group(0)
        if template is not None and part is not None:
            voided.add(template.replace("{1}", part))
    return voided


def csh02_window_rows(
    lines: Iterable[JournalEntry],
    bank_accounts: Iterable[BankAccount],
    period_end: date,
    lookback_days: int,
    pattern: str | None = None,
    template: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """CSH-02 D03, D04 的行, 按两条规则 SQL 的口径由分录行派生: 窗口为 [期末 - lookback_days, 期末] (闭区间), 作废对
    (csh02_voided) 整张凭证剔除; D03 只取银行科目上的行 (按银行账户的总账科目), 按 (银行账户, 入账日, 行 ID), D04
    取全部科目, 按 (入账日, 行 ID). 列同 SQL. Numbat 与 Xero 的假规则引擎共用."""
    lines = list(lines)
    voided = csh02_voided(lines, pattern, template)
    start = period_end - timedelta(days=lookback_days)
    window = sorted(
        (j for j in lines if start <= j.posting_date <= period_end and j.entry_no not in voided),
        key=lambda j: (j.posting_date, j.id),
    )
    banks = {b.gl_account_code: b.id for b in bank_accounts}
    d03 = [
        {
            "source_type": "journal_entry",
            "id": j.id,
            "bank_account_id": banks[j.account_code],
            "entry_no": j.entry_no,
            "posting_date": j.posting_date,
            "debit_cents": j.debit_cents,
            "credit_cents": j.credit_cents,
            "description": j.description,
            "source_doc": j.source_doc,
        }
        for j in sorted(
            (j for j in window if j.account_code in banks), key=lambda j: (banks[j.account_code], j.posting_date, j.id)
        )
    ]
    d04 = [
        {
            "source_type": "journal_entry",
            "id": j.id,
            "entry_no": j.entry_no,
            "posting_date": j.posting_date,
            "account_code": j.account_code,
            "debit_cents": j.debit_cents,
            "credit_cents": j.credit_cents,
            "description": j.description,
        }
        for j in window
    ]
    return {"D03": d03, "D04": d04}


def csh02_subsequent_rows(
    transactions: Iterable[BankTransaction], period_end: date, subsequent_days: int
) -> list[dict[str, Any]]:
    """CSH-02 D02 的行, 按规则 SQL 的口径由银行流水派生: 交易日在 (期末, 期末 + subsequent_days] 内, 按 (银行账户,
    交易日, 行 ID). 列同 SQL."""
    end = period_end + timedelta(days=subsequent_days)
    return [
        {
            "source_type": "bank_transaction",
            "id": t.id,
            "bank_account_id": t.bank_account_id,
            "txn_date": t.txn_date,
            "amount_cents": t.amount_cents,
            "description": t.description,
            "reference": t.reference,
        }
        for t in sorted(
            (t for t in transactions if period_end < t.txn_date <= end),
            key=lambda t: (t.bank_account_id, t.txn_date, t.id),
        )
    ]


# CSH-02 D02, D03, D04 的行: 由 data/demo 的 CSV 按规则的口径派生 (Numbat 没有作废对), 天数同 CSH-02.yaml; D03, D04
# 与连库结果整行相同由 tests/test_persistence_postgres 核对. D02 中 BA-02 在期后 10 天没有流水
NUMBAT_D02_ROWS = csh02_subsequent_rows(numbat_bank_transactions(), ENGAGEMENT.period_end, 10)
NUMBAT_WINDOW = csh02_window_rows(
    cast(tuple[JournalEntry, ...], numbat_tables()[TableKind.journal_entries]),
    [r for r in NUMBAT_ROWS if isinstance(r, BankAccount)],
    ENGAGEMENT.period_end,
    5,
)
NUMBAT_D03_ROWS = NUMBAT_WINDOW["D03"]  # 窗口内银行科目上的分录行
NUMBAT_D04_ROWS = NUMBAT_WINDOW["D04"]  # 窗口内全部科目的分录行


@functools.cache
def numbat_inputs(period_start: date, period_end: date) -> CheckInputs:
    """Numbat 演示数据集的体检汇总 (按期间缓存)."""
    from backend.services.data_quality import summarize

    tables = numbat_tables()
    return summarize(
        cast(Any, tables[TableKind.accounts]),
        cast(Any, tables[TableKind.trial_balance]),
        cast(Any, tables[TableKind.journal_entries]),
        period_start,
        period_end,
        dataset="demo",
    )


class FakeDataStore:
    """假数据管理存储: 数据版本, 就绪确认与各账表行数, 各项目共用.

    缺省模拟种子已载入整套数据集 (数据版本 1, 视同已确认就绪, 各表有行), 现有流程照旧越过 P1 开始前的等待;
    测试等数据就绪时传 ready=False, 并按需给 stats (没给的表行数为 0).
    体检汇总 (check_inputs): 已就绪的以 Numbat 演示数据集为底, 未就绪的为空; write_tables 写过的科目表, 试算表,
    总账换成写入的行, 审批日志按凭证号补进总账 (同真实实现).
    """

    def __init__(self, ready: bool = True, stats: Mapping[TableKind, TableStats] | None = None) -> None:
        self.data_version = 1 if ready else 0  # 数据版本
        self.ready_at: datetime | None = datetime(2026, 1, 1, tzinfo=UTC) if ready else None  # 确认就绪的时间
        self.ready_by: str | None = "dataset" if ready else None  # 确认人
        self.ready_reason: str | None = None  # 确认时的理由
        self.ready_failed_checks: list[CheckId] = []  # 确认时最近一次体检不通过的项
        self.table_stats: dict[TableKind, TableStats] = dict(SEEDED_STATS if stats is None and ready else stats or {})
        # 证据文件按它数 (document_counts, 同真实实现数 documents 表); make_deps 连上项目用的假财务数据存储
        self.financial: FakeFinancialStore | None = None
        self.confirm_calls: list[tuple[str, str]] = []  # confirm_ready 的每次调用 (项目编号, 确认人)
        self.table_versions: list[TableVersion] = []  # 已写入的账表版本, 按数据版本升序
        self.check_results: list[DataCheck] = []  # 最近一次体检的结果
        self.last_run: CheckRun | None = None  # 最近一次体检
        self.data_requests: dict[str, DataRequest] = {}  # 补交请求 ID → 请求
        self.open_count = 0  # 另加的待补交请求数 (直接设概览用)
        self.seeded = ready  # 体检汇总是否以 Numbat 演示数据集为底
        self.tables: dict[TableKind, list[BaseModel]] = {}  # write_tables 写过的科目表, 试算表, 总账的当前行
        self.approvals: list[ApprovalLogRow] = []  # 最新一版审批日志
        self.categories: dict[str, list[Any]] = {}  # 科目代码 → 分类历史 (CategoryVersion)
        # write_tables 的每次调用: (各表的行, 其余关键字参数); 写入同时更新行数, 版本与数据版本
        self.written: list[tuple[dict[TableKind, list[BaseModel]], dict[str, Any]]] = []
        self.imports: dict[str, tuple[ImportSession, bytes]] = {}  # 导入会话 ID → (会话, 暂存的原文件)
        self.import_updates: list[tuple[str, dict[str, Any]]] = []  # update_import 的每次调用 (会话 ID, 改了的字段)
        self.extractions: dict[tuple[str, str, int], dict[str, Any]] = {}  # save_extraction 记下的 (类型, 对象, 版本)
        self.import_batches: dict[str, ImportBatch] = {}  # 智能导入的批次 ID → 批次 (save_batch 覆盖)
        # 数据变动 (同真实实现的 data_changes): 种子载入为版本 1 的全部各类数据
        self.data_changes: list[DataChange] = [DataChange(data_version=1, input=i) for i in DataInput] if ready else []

    async def changes(self, eid: str) -> list[DataChange]:
        order = list(DataInput)
        return sorted(self.data_changes, key=lambda c: (c.data_version, order.index(c.input)))

    def record(self, *inputs: DataInput, document_types: Sequence[DocumentType] | None = None) -> None:
        """数据版本 +1 并记下这次变了哪些数据; documents 另记文件类型 (同真实实现, 按 DocumentType 顺序去重)."""
        self.data_version += 1
        wanted = None if document_types is None else set(document_types)
        types = None if wanted is None else [t for t in DocumentType if t in wanted]
        self.data_changes += [
            DataChange(
                data_version=self.data_version, input=i, document_types=types if i is DataInput.documents else None
            )
            for i in dict.fromkeys(inputs)
        ]

    async def readiness(self, eid: str) -> DataReadiness:
        return DataReadiness(
            data_version=self.data_version,
            ready_at=self.ready_at,
            ready_by=self.ready_by,
            loaded_at=self.ready_at,
            ready_reason=self.ready_reason,
            ready_failed_checks=self.ready_failed_checks,
        )

    async def confirm_ready(
        self, eid: str, actor: str, *, reason: str | None = None, failed_checks: Sequence[CheckId] = ()
    ) -> DataReadiness:
        self.confirm_calls.append((eid, actor))
        if self.ready_at is None:
            self.ready_at, self.ready_by = datetime.now(UTC), actor
            self.ready_reason, self.ready_failed_checks = reason, list(failed_checks)
        elif failed_checks and not self.ready_failed_checks:  # 同真实实现: 补记本次的理由与未通过项
            self.ready_reason, self.ready_failed_checks = reason, list(failed_checks)
        return await self.readiness(eid)

    async def stats(self, eid: str) -> dict[TableKind, TableStats]:
        return {kind: self.table_stats.get(kind, TableStats(records=0)) for kind in TableKind}

    async def document_counts(self, eid: str) -> dict[DocumentType, int]:
        documents = self.financial.document_refs if self.financial is not None else []
        return dict(Counter(d.type for d in documents))

    async def versions(self, eid: str) -> list[TableVersion]:
        return list(self.table_versions)

    async def checks(self, eid: str) -> list[DataCheck]:
        return list(self.check_results)

    async def open_requests(self, eid: str) -> int:
        return self.open_count + sum(r.status == "open" for r in self.data_requests.values())

    async def check_inputs(
        self, eid: str, period_start: date, period_end: date, *, adjustment_prefix: str | None = None
    ) -> CheckInputs:
        from backend.services.data_quality import summarize

        if self.seeded and not self.tables and not self.approvals:
            return numbat_inputs(period_start, period_end)
        base: dict[TableKind, Sequence[BaseModel]] = dict(numbat_tables()) if self.seeded else {}
        current = {**base, **self.tables}
        journals = [cast(Any, row) for row in current.get(TableKind.journal_entries, ())]
        for approval in self.approvals:
            values = approval.model_dump(exclude_unset=True, exclude={"entry_no"})
            journals = [j.model_copy(update=values) if j.entry_no == approval.entry_no else j for j in journals]
        return summarize(
            cast(Any, current.get(TableKind.accounts, ())),
            cast(Any, current.get(TableKind.trial_balance, ())),
            journals,
            period_start,
            period_end,
            dataset="demo" if self.seeded else None,
            adjustment_prefix=adjustment_prefix,
        )

    async def check_run(self, eid: str) -> CheckRun | None:
        return self.last_run

    async def requests(self, eid: str) -> list[DataRequest]:
        from backend.services.data_quality import request_order

        return sorted(self.data_requests.values(), key=request_order)

    def current(self, kind: TableKind) -> list[Any]:
        """某张表当前的行: 写过的取写入的, 否则已就绪的取 Numbat 演示数据集 (只有科目表, 试算表, 总账)."""
        if kind in self.tables:
            return list(self.tables[kind])
        return list(numbat_tables().get(kind, ())) if self.seeded else []

    async def table_rows(
        self, eid: str, kind: TableKind, q: str | None, offset: int, limit: int
    ) -> tuple[int, list[dict[str, Any]]]:
        """同真实实现的搜索与排序 (只做测试用到的表)."""
        if kind is TableKind.approval_log:
            rows = [r.model_dump(mode="json", exclude_unset=True) for r in self.approvals]
        else:
            rows = [r.model_dump(mode="json") for r in self.current(kind)]
        if kind is TableKind.accounts:
            tb = self.current(TableKind.trial_balance)
            balances = {p: {r.account_code: r.balance_cents for r in tb if r.period == p} for p in ("current", "prior")}
            for row in rows:
                row |= {
                    "balance_current_cents": balances["current"].get(row["code"]),
                    "balance_prior_cents": balances["prior"].get(row["code"]),
                    "edited": row["category_basis"] == "auditor",
                }
            keys: tuple[str, ...] = ("code", "name")
        else:
            keys = tuple(k for k, v in (rows[0] if rows else {}).items() if isinstance(v, str))
        if q:
            needle = q.casefold()
            rows = [r for r in rows if any(needle in str(r.get(k) or "").casefold() for k in keys)]
        order = {
            TableKind.accounts: ("code",),
            TableKind.trial_balance: ("account_code", "period"),
            TableKind.journal_entries: ("posting_date", "entry_no", "id"),
        }.get(kind)
        if order:
            rows.sort(key=lambda r: tuple(str(r[k]) for k in order))
        return len(rows), rows[offset : offset + limit]

    async def account(self, eid: str, code: str) -> Any:
        return next((a for a in self.current(TableKind.accounts) if a.code == code), None)

    async def category_history(self, eid: str) -> dict[str, list[Any]]:
        return {code: list(versions) for code, versions in self.categories.items()}

    async def set_category(self, eid: str, code: str, category: Any, *, actor: str, reason: str) -> Any:
        from backend.contracts.data import CategoryVersion

        accounts = self.current(TableKind.accounts)
        account = next((a for a in accounts if a.code == code), None)
        if account is None:
            raise LookupError(code)
        history = self.categories.setdefault(code, [])
        if not history:
            history.append(
                CategoryVersion(
                    version=1,
                    category=account.category,
                    basis=account.category_basis,
                    changed_by=AgentId.A3,
                    changed_at=self.ready_at or NOW,
                )
            )
        changed = CategoryVersion(
            version=history[-1].version + 1,
            category=category,
            basis="auditor",
            changed_by=actor,
            changed_at=datetime.now(UTC),
            reason=reason,
        )
        history.append(changed)
        update = {"category": category, "category_basis": "auditor"}
        self.tables[TableKind.accounts] = [a.model_copy(update=update) if a.code == code else a for a in accounts]
        self.record(DataInput.accounts)
        return changed

    async def mark_unavailable(
        self, eid: str, request_id: str, *, reason: str, actor: str, at: datetime
    ) -> DataRequest | None:
        request = self.data_requests.get(request_id)
        if request is None or request.status != "open":
            return None
        update = {"status": "unavailable", "unavailable_reason": reason, "resolved_by": actor, "resolved_at": at}
        marked = request.model_copy(update=update)
        self.data_requests[request_id] = marked
        return marked

    async def save_check_run(self, eid: str, run: CheckRun, requests: Sequence[DataRequest]) -> None:
        self.last_run = run
        self.check_results = list(run.checks)
        for request in requests:
            self.data_requests[request.id] = request

    async def write_tables(
        self,
        eid: str,
        tables: Mapping[TableKind, Sequence[BaseModel]],
        *,
        source: DataSource,
        actor: str,
        file_name: str | None = None,
        document_id: str | None = None,
        import_id: str | None = None,
        request_id: str | None = None,
        origins: Mapping[TableKind, TableOrigin] | None = None,
    ) -> int:
        """记下调用; 数据版本 +1, 各表行数换成写入的行数 (试算表分两期, 审批日志记版本行数), 追加版本 (出处按
        origins, 同真实实现), 导入会话置 committed. 不做科目分类历史与审批日志补进 (见 PostgresDataStore)."""
        kwargs: dict[str, Any] = {
            "source": source,
            "actor": actor,
            "file_name": file_name,
            "document_id": document_id,
            "import_id": import_id,
            "request_id": request_id,
        }
        if origins is not None:  # 只在给了时记 (原有断言比较整个字典)
            kwargs["origins"] = dict(origins)
        self.written.append(({k: list(v) for k, v in tables.items()}, kwargs))
        for kind, rows in tables.items():
            if kind is TableKind.approval_log:
                self.approvals = [cast(ApprovalLogRow, r) for r in rows]
            elif kind in (TableKind.accounts, TableKind.trial_balance, TableKind.journal_entries):
                self.tables[kind] = list(rows)
            elif kind is TableKind.employees and self.financial is not None:  # 工资册整表替换, FakeFinancialStore 读出
                self.financial.employee_rows = [Employee.model_validate(r.model_dump()) for r in rows]
        self.record(*table_changes(tables))
        for kind, rows in tables.items():
            if kind is TableKind.trial_balance:
                current = sum(getattr(r, "period", None) == "current" for r in rows)
                self.table_stats[kind] = TableStats(records=len(rows), current=current, prior=len(rows) - current)
            else:
                self.table_stats[kind] = TableStats(records=len(rows))
            origin = (origins or {}).get(kind) or TableOrigin(
                file_name=file_name, document_id=document_id, request_id=request_id
            )
            self.table_versions.append(
                TableVersion(
                    id=import_id or f"{source.upper()}-{kind}-v{self.data_version}",
                    kind=kind,
                    source=source,
                    file_name=origin.file_name,
                    document_id=origin.document_id,
                    records=len(rows),
                    data_version=self.data_version,
                    request_id=origin.request_id,
                    created_by=actor,
                    created_at=NOW,
                )
            )
        if import_id in self.imports:
            session, raw = self.imports[import_id]
            [(kind, rows)] = tables.items()
            update = {"status": "committed", "kind": kind, "records": len(rows)}
            self.imports[import_id] = (session.model_copy(update=update), raw)
        return self.data_version

    async def bump_version(
        self, eid: str, input: DataInput, *, document_types: Sequence[DocumentType] | None = None
    ) -> int:
        self.record(input, document_types=document_types)
        return self.data_version

    async def save_batch(self, eid: str, batch: ImportBatch) -> None:
        self.import_batches[batch.id] = batch

    async def batch(self, eid: str, batch_id: str) -> ImportBatch | None:
        return self.import_batches.get(batch_id)

    async def batches(self, eid: str) -> list[ImportBatch]:
        return sorted(self.import_batches.values(), key=lambda b: (b.created_at, b.id))

    async def commit_batch(
        self,
        eid: str,
        batch: ImportBatch,
        tables: Mapping[TableKind, Sequence[BaseModel]],
        *,
        actor: str,
        origins: Mapping[TableKind, TableOrigin],
        documents: Sequence[DocumentChange],
    ) -> ImportBatch:
        """同真实实现一次写成: 批次已 committed 时什么都不写, 返回它; 有表时经 write_tables 写 (记进 written, 那里
        出错时什么都没改); 改文件登记 (财务数据存储的 document_refs), 依次写合同 (save_contract), 回函抽取 (reply,
        第 1 版) 与 ASIC 摘录抽取 (registry_extract, 接在已有版本之后); 写了合同或摘录时记数据变动 contracts 或
        registry_extracts (有表时记在写表的版本上, 否则数据版本 +1); 批次覆盖为 batch 并记数据版本. 批次或文件
        不存在时在改动之前抛出 (模拟回滚)."""
        stored = self.import_batches.get(batch.id)
        if stored is None:
            raise LookupError(batch.id)
        if stored.status == "committed":
            return stored
        assert self.financial is not None
        refs = self.financial.document_refs
        missing = [c.document_id for c in documents if all(d.id != c.document_id for d in refs)]
        if missing:
            raise FileNotFoundError(missing[0])
        version = (
            await self.write_tables(eid, tables, source="import", actor=actor, origins=origins) if tables else None
        )
        for change in documents:
            i = next(i for i, d in enumerate(refs) if d.id == change.document_id)
            refs[i] = refs[i].model_copy(
                update={
                    "type": change.type,
                    "reliability": change.reliability,
                    "status": change.status,
                    "failed_step": change.failed_step,
                }
            )
            for contract in change.contracts:
                await self.financial.save_contract(eid, contract)
            if change.reply is not None:
                await self.save_extraction(eid, "reply", change.document_id, 1, change.reply, changed_by=AgentId.A3)
            if change.registry_extract is not None:  # 接在已有版本之后 (需求 R290)
                known = [v for (t, i, v) in self.extractions if (t, i) == ("registry_extract", change.document_id)]
                number = max(known, default=0) + 1
                data = change.registry_extract
                document_id = change.document_id
                await self.save_extraction(eid, "registry_extract", document_id, number, data, changed_by=AgentId.A3)
        extracted = [DataInput.contracts] if any(change.contracts for change in documents) else []
        if any(change.registry_extract is not None for change in documents):
            extracted.append(DataInput.registry_extracts)
        if extracted:
            if version is None:
                self.record(*extracted)
                version = self.data_version
            else:
                self.data_changes += [DataChange(data_version=version, input=i) for i in extracted]
        committed = batch.model_copy(update={"data_version": version})
        self.import_batches[batch.id] = committed
        return committed

    async def extraction_versions(
        self, eid: str, target_type: str | None = None, target_id: str | None = None
    ) -> list[ExtractionRecord]:
        return [
            ExtractionRecord(target_type=t, target_id=i, version=v, changed_at=NOW, **record)
            for (t, i, v), record in sorted(self.extractions.items())
            if (target_type is None or t == target_type) and (target_id is None or i == target_id)
        ]

    async def save_extraction(
        self,
        eid: str,
        target_type: str,
        target_id: str,
        version: int,
        data: Mapping[str, Any],
        *,
        changed_by: str,
        field: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.extractions[(target_type, target_id, version)] = {
            "data": dict(data),
            "changed_by": changed_by,
            "field": field,
            "reason": reason,
        }

    async def create_import(self, eid: str, session: ImportSession, data: bytes) -> None:
        self.imports[session.id] = (session, data)

    async def import_session(self, eid: str, import_id: str) -> tuple[ImportSession, bytes] | None:
        return self.imports.get(import_id)

    async def update_import(
        self,
        eid: str,
        import_id: str,
        *,
        sheet: str,
        date_format: str | None = None,
        mapping: Mapping[str, str | None] | None = None,
        row_count: int | None = None,
    ) -> None:
        changes = {"sheet": sheet, "date_format": date_format, "mapping": mapping, "row_count": row_count}
        self.import_updates.append((import_id, {k: v for k, v in changes.items() if v is not None}))
        session, raw = self.imports[import_id]
        if session.status != "parsed":  # 同真实实现: 只改未提交的会话
            return
        update: dict[str, Any] = {"sheet": sheet}
        if date_format is not None:
            update["date_format"] = date_format
        if mapping is not None:
            update["mapping"] = dict(mapping)
        self.imports[import_id] = (session.model_copy(update=update), raw)


def bind_stores(engagement_store: FakeEngagementStore, output_store: FakeOutputStore) -> None:
    """让项目存储加入审计产出存储的事务, 同真实实现共用一个数据库事务."""
    engagement_store.bind(output_store)
    output_store.engagement_store = engagement_store


# ── 组装 ──


KNOWLEDGE = Path(__file__).resolve().parents[1] / "src" / "backend" / "knowledge"  # 仓库的 knowledge 目录


@functools.cache
def _knowledge_library(root: Path, features: frozenset[str]) -> ProcedureLibrary:
    return ProcedureLibrary(root, features)


def knowledge_library(root: Path, features: Iterable[str] = BASELINE) -> ProcedureLibrary:
    """程序库, 同 ProcedureLibrary(root, features), 但仓库的 knowledge 目录 (会话中不改; 要改的用例先复制到临时目录)
    每个功能集只加载一次: 每个用例建依赖都重新解析全部 YAML 约 0.1 秒, 是默认测试最大的一项重复开销. 返回浅拷贝,
    用例换它的属性 (如 commit, 或把 load 换成补了声明的) 不影响别的用例; 内部字典 (_templates, _all, _fragments,
    _report) 与模板对象各拷贝共用, 只可换属性, 不可原地改. 别的目录照常新建."""
    if Path(root).resolve() != KNOWLEDGE:
        return ProcedureLibrary(root, features)
    return copy.copy(_knowledge_library(Path(root), frozenset(features)))


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
    data: FakeDataStore | None = None,
) -> AgentDeps:
    """真实依赖里换掉登记系统, 留痕, 项目存储, 审计产出存储, 财务数据存储, 证据文件存储, 数据集载入, 规则引擎,
    函证平台与用户 (给了 library 时另换程序库).
    features: 启用的功能, 写入 AgentDeps.features. 没给 library 时缺省为基线, 按它建 knowledge 目录的程序库 (同
    组装根); 给了 library 时不重建, 缺省取 library.features, 给了而与之不同时抛 ValueError (两处功能集须一致).
    其余依赖只构造不连接. 判断引擎按替换后的大模型, 财务数据存储, 留痕与程序库重建, 复核队列与自主度降级的知识
    (AgentDeps.autonomy) 按替换后的程序库重建.

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
    if data is None:
        data = FakeDataStore()
    if data.financial is None:  # 证据文件数取同一个财务数据存储 (同真实实现共用 documents 表)
        data.financial = financial_store
    settings = get_settings()
    with mock.patch.object(backend.main, "ProcedureLibrary", knowledge_library):  # 组装根的程序库取会话缓存
        deps = build_deps(settings)

    def mock_client() -> SwitchableLLMClient:
        """同组装根的 mock 客户端, 但按假项目存储取项目数据集 (组装根的绑定在 Postgres 项目存储上)."""
        return SwitchableLLMClient(make_llm_client(settings, engagement_store), llm_version(settings)["model"])

    if review_llm is None and llm is None:
        review_llm = mock_client()
    llm = llm if llm is not None else mock_client()
    if library is None:
        features = BASELINE if features is None else features
        library = deps.library if deps.library.features == features else knowledge_library(deps.library.root, features)
    elif features is None:
        features = library.features
    elif features != library.features:
        raise ValueError(f"features {sorted(features)} 与程序库的 {sorted(library.features)} 不一致")
    judgments = JudgmentEngine(llm, OutputValidator(financial_store, library, trail), trail, library)
    deps = replace(
        deps,
        library=library,
        queue=ReviewQueue(library),
        llm=llm,
        judgments=judgments,
        registry=registry,
        trail=trail,
        engagement_store=engagement_store,
        output_store=output_store,
        financial_store=financial_store,
        evidence_store=FakeEvidenceStore(financial_store, output_store),
        datasets=FakeDatasetLoader(financial_store),
        data=data,
        rules=rules if rules is not None else FakeRuleEngine(),
        confirmations=confirmations if confirmations is not None else FakeConfirmationChannel(),
        users=FakeUserStore(),
        review_llm=review_llm,
        features=features,
        autonomy=AutonomyPolicy.load(library),
    )
    return with_manual_owners(deps)  # 同组装根: 按换好的程序库与假实现重找人工程序的 owner


def profile_update(profile: ClientProfile) -> tuple[int, dict[str, Any]]:
    """DecisionSubmit.update_client_profile 的 (version, changes): 以整份画像为请求给出的字段 (含 id)."""
    return profile.version, profile.model_dump(exclude={"version"})


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


# 约定文件的别名: 委任文件 appointment.pdf 与约定书 engagement_letter.pdf (需求 R199)
DOC_APPT, DOC_EL = "DOC-APPT", "DOC-EL"


def engagement_terms(run_id: str = "RUN-ACC-08-1", version: int = 1) -> EngagementTerms:
    """合成的约定条款与委任核验 TERMS (需求 R198, 只填契约用到的): 事实取 Xero 演示的两份约定文件 (Xero演示数据集
    设计第 4.1, 4.2 节; 页码同 R199 验收 2), T01-T07 都通过, 没有例外, 建议判断 J-ACC-08-{run_no}-TERMS."""

    def page(document: str, number: int, locator: str) -> list[EvidencePointer]:
        letter = document == DOC_EL
        return [
            EvidencePointer(
                type=EvidenceType.contract if letter else EvidenceType.voucher,
                source_type="document",
                source_id=document,
                locator=locator,
                page=number,
                reliability=Reliability.external_copy if letter else Reliability.internal_copy,
            )
        ]

    run_no = run_id.rsplit("-", 1)[1]
    return EngagementTerms(
        id="TERMS",
        version=version,
        run_id=run_id,
        framework="tier2_gpfs",
        framework_evidence=page(DOC_EL, 2, "framework"),
        elements=[
            TermsItem(item=item, present=True, evidence=page(DOC_EL, n, item))
            for item, n in zip(TERMS_ELEMENTS, (1, 1, 2, 2, 3), strict=True)  # 要素所在页
        ],
        acknowledgements=[
            TermsItem(item=item, present=True, evidence=page(DOC_EL, 2, item)) for item in TERMS_ACKNOWLEDGEMENTS
        ],
        letter_dated_on=date(2026, 8, 28),
        letter_signed_by="Alex Morgan",
        letter_signed_on=date(2026, 9, 2),
        letter_evidence=[  # 出具日在第 1 页, 签回人与签回日在第 3 页 (R199 验收 2)
            *page(DOC_EL, 1, "letter_dated_on"),
            *page(DOC_EL, 3, "letter_signed_by"),
            *page(DOC_EL, 3, "letter_signed_on"),
        ],
        direction=ShareholderDirection(
            signed_by="Sam Whitfield",
            shares_held=15,
            total_shares=100,
            votes_pct=15.0,
            made_on=date(2026, 8, 20),
            requires_audit=True,
            evidence=page(DOC_APPT, 2, "direction"),
        ),
        appointment=AuditorAppointment(
            appointed_by="Alex Morgan",
            resolution_on=date(2026, 8, 27),
            auditor_name="Tessellate Audit Partners",
            evidence=page(DOC_APPT, 4, "appointment"),
        ),
        consent=AuditorConsent(
            given_on=date(2026, 8, 25),
            signed_by="Jordan Ellis",
            firm_name="Tessellate Audit Partners",
            registered_auditor_no="300471",
            firm_name_signed=True,
            own_name_signed=True,
            evidence=page(DOC_APPT, 3, "consent"),
        ),
        year_change=FinancialYearChange(
            resolved_on=date(2026, 8, 14),
            previous_year_end=date(2026, 6, 30),
            new_year_end=date(2026, 9, 30),
            prior_shortened_within_5_years=False,
            good_faith_reason="Meets the shareholders agreement of 15 June 2026; aligns with the October renewals.",
            evidence=page(DOC_APPT, 1, "year_change"),
        ),
        checks=[
            TermsCheck(check=f"T0{n}", status=TermsCheckStatus.passed, blocking=n in (1, 3), basis="b", detail="d")
            for n in range(1, 8)
        ],
        exception_ids=[],
        judgment_id=f"J-ACC-08-{run_no}-TERMS",
        document_ids=[DOC_APPT, DOC_EL],
    )


def ownership_graph(run_id: str = "RUN-GEN-08-1", version: int = 1) -> OwnershipGraph:
    """合成的股权与任职关系图 (需求 R136, 只填契约用到的): 报告主体 ACN-900043021, Alex Morgan 任董事并持 85 股,
    Sam Whitfield 只持 15 股 (没有出生日期). 关系的证据为一份摘录 (高管第 1 页, 股东第 2 页)."""

    def row(page: int, locator: str) -> EvidencePointer:
        return EvidencePointer(
            type=EvidenceType.external,
            source_type="document",
            source_id="DOC-0123456789ab",
            locator=locator,
            page=page,
            reliability=Reliability.external_original,
        )

    entity, alex, sam = "ACN-900043021", "PER-MORGAN-ALEX-19790412", "PER-WHITFIELD-SAM-NODOB"
    return OwnershipGraph(
        id="OWNERSHIP",
        version=version,
        run_id=run_id,
        reporting_entity_id=entity,
        nodes=[
            PartyNode(id=entity, kind="company", name="Demo Company (AU) Pty Ltd", acn="900043021", abn="25900043021"),
            PartyNode(id=alex, kind="person", name="Alex Morgan", birth_date=date(1979, 4, 12), searched=True),
            PartyNode(id=sam, kind="person", name="Sam Whitfield"),
        ],
        links=[
            PartyLink(
                from_id=alex,
                to_id=entity,
                relation="director",
                appointed=date(2015, 3, 2),
                evidence=row(1, "officeholders[0]"),
            ),
            PartyLink(
                from_id=alex,
                to_id=entity,
                relation="shareholder",
                shares=85,
                pct=0.85,
                evidence=row(2, "shareholders[0]"),
            ),
            PartyLink(
                from_id=sam,
                to_id=entity,
                relation="shareholder",
                shares=15,
                pct=0.15,
                evidence=row(2, "shareholders[1]"),
            ),
        ],
    )


# ── 签字前提的四类审计产出 (需求 R215): 合成的 Numbat 样例, 只填契约用到的 ──

NUMBAT_DIRECTORS = NUMBAT_PROFILE.directors  # 声明书签署人与沟通对象 (合成数据集设计第 2.1 节)


def representation_letter(run_id: str = "RUN-FIN-07-1", version: int = 1) -> WrittenRepresentationLetter:
    """REPLETTER: 已取得, 两位董事签署; 两条核心声明都写了, 诉讼声明不提供而审计师接受; 附表一项未更正错报."""
    return WrittenRepresentationLetter(
        id="REPLETTER",
        version=version,
        status=RepresentationStatus.obtained,
        representations=[
            RepresentationClause(
                code="FR_PREPARATION", kind="core", standard_ref="ASA 580.10", status=ClauseStatus.present
            ),
            RepresentationClause(
                code="INFORMATION_ACCESS", kind="core", standard_ref="ASA 580.11", status=ClauseStatus.present
            ),
            RepresentationClause(
                code="LITIGATION",
                kind="specific",
                standard_ref="ASA 501.12",
                status=ClauseStatus.refused_accepted,
                acceptance_reason="No litigation is recorded; legal expense ledger reviewed and confirmed nil.",
                opinion_effect="None; the matter is covered by other audit evidence.",
            ),
        ],
        attached_schedule=[
            UncorrectedItem(misstatement_id="M-D-1", amount_cents=1_500_000, direction=Direction.overstated)
        ],
        schedule_ref=ObjectRef(kind="UncorrectedSchedule", id="SCHEDULE", version=1),
        signed_by=list(NUMBAT_DIRECTORS),
        dated_at=date(2026, 8, 20),
        period_end=ENGAGEMENT.period_end,
        document_id="DOC-5e1f00c0ffee",
        run_id=run_id,
    )


def compliance_check(
    procedure_id: str = "FIN-10", run_no: int = 1, version: int = 1, exception_ids: list[str] | None = None
) -> ComplianceCheck:
    """CHECK-{procedure_id}: E01 独立性标记通过, E02 或有收费不通过 (exception_ids 为它的例外), E05 待关注;
    依据画像第 1 版."""
    return ComplianceCheck(
        id=f"CHECK-{procedure_id}",
        version=version,
        procedure_id=procedure_id,
        run_id=f"RUN-{procedure_id}-{run_no}",
        items=[
            CheckItem(
                code="E01",
                description="No independence flags",
                status=CheckItemStatus.passed,
                standard_ref="APES 110 R400",
            ),
            CheckItem(
                code="E02",
                description="Contingent fee",
                status=CheckItemStatus.failed,
                standard_ref="APES 110 R410.9",
                detail={"contingent": True},
                requires_disposition=True,
            ),
            CheckItem(
                code="E05",
                description="Ongoing monitoring records",
                status=CheckItemStatus.attention,
                standard_ref="ASA 220.17",
            ),
        ],
        exception_ids=exception_ids or [],
        basis={"client_profile_version": 1},
    )


def governance_communication(run_no: int = 1, version: int = 1) -> GovernanceCommunication:
    """GC-RPT-07-{run_no}: 书面, 致两位董事; 重大会计实务已沟通, 未更正错报一项, 声明书事项豁免 (治理层都参与管理)."""
    return GovernanceCommunication(
        id=f"GC-RPT-07-{run_no}",
        version=version,
        form=CommunicationForm.written,
        communicated_at=date(2026, 8, 21),
        recipients=list(NUMBAT_DIRECTORS),
        matters=[
            CommunicationMatter(
                code="SIG_PRACTICES",
                standard_ref="ASA 260.16(a)",
                required=True,
                covered=True,
                text="Revenue recognition policy for distributor sales.",
            ),
            CommunicationMatter(
                code="UNCORRECTED",
                standard_ref="ASA 450.12",
                required=True,
                covered=True,
                text="One uncorrected misstatement: revenue overstated by AUD 15,000.00.",
            ),
            CommunicationMatter(
                code="REPRESENTATIONS",
                standard_ref="ASA 260.16(c)(ii)",
                required=False,
                exempt_reason="All those charged with governance are involved in managing the entity.",
                covered=False,
                text="",
            ),
        ],
        uncorrected_misstatement_ids=["M-D-1"],
        letter_text="Dear Directors, ...",
        run_id=f"RUN-RPT-07-{run_no}",
    )


def independence_declaration(run_id: str = "RUN-RPT-08-1", version: int = 1) -> IndependenceDeclaration:
    """S307C 草稿: 无违规, 依据 CHECK-FIN-10 v1, 项目负责人 U-1 声明, 须附入董事报告."""
    return IndependenceDeclaration(
        id="S307C",
        version=version,
        form=DeclarationForm.no_contraventions,
        based_on=ObjectRef(kind="ComplianceCheck", id="CHECK-FIN-10", version=1),
        text="I declare that, to the best of my knowledge and belief, there have been no contraventions ...",
        declarant_id=NUMBAT_AUDITOR.id,
        directors_report_copy_required=True,
        run_id=run_id,
    )


PLAN_REJECT_ID = "D-P15R"  # 合成计划第 2 修订起回应的 PLN-15 驳回决定
PLAN_REJECT_REASON = "Add the planned timing of bank confirmations"  # 演示的驳回理由 (需求 R206 验收 3, R317)
PLAN_EQR_BASIS = (
    "Not a listed entity; no engagement quality review is required by law or regulation, and the firm has not "
    "determined one is needed (ASQM 1 para 34(f))"
)
# PLN-15 通过时的确认 (需求 R203): 不需要项目质量复核, 不需要审计师的专家
PLAN_CONFIRMATION = PlanConfirmation(eqr_required=False, eqr_basis=PLAN_EQR_BASIS, auditor_expert_not_required=True)


def audit_plan(
    revision: int = 1, version: int = 1, status: PlanStatus = PlanStatus.draft, run_id: str | None = None
) -> AuditPlan:
    """合成的总体审计计划 PLAN-{revision} (需求 R203, 只填契约用到的): Numbat 期间, 收入基准 1% 的重要性三项, 两条
    程序 (REV-08 关联两个收入目标, DOC-06 为只经依赖加入的), 叙述与时间表各两项. 执行缺省为 RUN-PLN-15-{revision};
    已批准的带三项确认与批准人 (U-1, NOW), 已取代的按驳回前的草稿 (都为空); 第 2 修订起回应驳回决定 D-P15R."""
    confirmed = status is PlanStatus.approved
    return AuditPlan(
        id=f"PLAN-{revision}",
        version=version,
        revision=revision,
        status=status,
        run_id=run_id or f"RUN-PLN-15-{revision}",
        period_start=ENGAGEMENT.period_start,
        period_end=ENGAGEMENT.period_end,
        applicable_framework="tier2_gpfs",
        engagement_letter_document_id="DOC-EL",
        materiality_choice=MaterialityChoice(basis="revenue", pct=0.01, performance_pct=0.75, clearly_trivial_pct=0.05),
        materiality_cents=12_000_000,
        performance_materiality_cents=9_000_000,
        clearly_trivial_cents=600_000,
        significant_risk_objective_ids=["OBJ-REV-OCC", "OBJ-REV-CUT"],
        items=[
            PlanItem(
                procedure_id="DOC-06", name="银行回函要素抽取", template_commit="test", objective_ids=[], params={}
            ),
            PlanItem(
                procedure_id="REV-08",
                name="三单匹配",
                template_commit="test",
                objective_ids=["OBJ-REV-OCC", "OBJ-REV-CUT"],
                params={"window_days": 20, "period_end": "2026-06-30"},
            ),
        ],
        narrative=[
            PlanSection(section="overall_strategy", text="Substantive audit of revenue and cash."),
            PlanSection(section="scope", text="Year ended 30 June 2026."),
            PlanSection(section="significant_risks", text="Presumed fraud risk in revenue recognition."),
            PlanSection(section="materiality", text="1% of revenue."),
        ],
        procedure_notes=[
            ProcedureNote(procedure_id="DOC-06", nature_timing_extent="Extract every bank confirmation reply."),
            ProcedureNote(procedure_id="REV-08", nature_timing_extent="Match all invoices in the last 20 days."),
        ],
        timetable=[
            Milestone(milestone="Fieldwork complete", due=date(2026, 8, 14)),
            Milestone(milestone="Auditor's report signed", due=date(2026, 8, 28)),
        ],
        eqr_recommendation=EqrRecommendation(required=False, basis=PLAN_EQR_BASIS),
        expert_assessment="No auditor's expert is needed: no specialised valuations.",
        standards_cited=["ASA300-P07", "ASQM1-P34f"],
        eqr_required=False if confirmed else None,
        eqr_basis=PLAN_EQR_BASIS if confirmed else None,
        auditor_expert_not_required=True if confirmed else None,
        addresses_decision_id=PLAN_REJECT_ID if revision > 1 else None,
        revision_reason=PLAN_REJECT_REASON if revision > 1 else None,
        approved_by=NUMBAT_AUDITOR.id if confirmed else None,
        approved_at=NOW if confirmed else None,
    )


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


class ScriptA8(ScriptAgent):
    """以 A8 (底稿, 错报闭环) 身份写片段的脚本 Agent, 可写判断, 复核任务, 错报 (需求 R158)."""

    agent_id: ClassVar[AgentId] = AgentId.A8


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


def numbat_plan_agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
    """PLN-15 的脚本 Agent (需求 R205, 功能 planning; 真实的 A4 见 agents/audit_plan): 起草下一修订 PLAN-{n} (合成计划
    audit_plan, 记本次执行); 有上一修订时把它的最新版本置 superseded (经 model_validate 构造, 记本次执行)."""

    def pln15(ctx: RunContext) -> StatePatch:
        plans = ctx.state["audit_plans"]
        revision = max((p.revision for p in plans), default=0) + 1
        run = finished(ctx.run)
        written = [audit_plan(revision, run_id=run.id)]
        previous = max((p for p in plans if p.revision == revision - 1), key=lambda p: p.version, default=None)
        if previous is not None and previous.status is not PlanStatus.superseded:
            retired = {"version": previous.version + 1, "status": PlanStatus.superseded, "run_id": run.id}
            written.append(AuditPlan.model_validate({**previous.model_dump(), **retired}))
        return {"audit_plans": written, "procedure_runs": [run]}

    return {"PLN-15": ScriptA4(deps, pln15)}


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
                idempotency_key=numbat_confirmation_key(ctx.engagement.id, account),
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
            run_id=ctx.run.id,
        )
        return {"procedure_runs": [finished(ctx.run)], "report_versions": [report]}

    return {"RPT-03": ScriptA10(deps, rpt03)}


def numbat_all_p2_agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
    """P2 七条程序的 Numbat 脚本 Agent, 产出按合成数据集设计第 7 节第 7-13 行 (第 0 层四条, 现金线两条, REV-02)."""
    return {**numbat_p2_agents(deps), **numbat_cash_agents(deps), **numbat_rev02_agents(deps)}


def make_graph_nodes(deps: AgentDeps, parallel_cap: int = 2, presets: bool = True) -> GraphNodes:
    """主图节点: 已实装的 Agent 用真实的, P1 三条程序与 RPT-03 用 Numbat 脚本 Agent (见 numbat_p1_agents,
    numbat_report_agents); 功能 planning 打开时 PLN-15 也用脚本 (numbat_plan_agents).

    数据集载入换成假实现时 (make_deps 的缺省), 按种子已载入 Numbat 处理, 先登记 3 份合同: P1 不再自动载入
    数据集 (需求 R10), 新项目的数据由审计师接入或种子脚本预先载入."""
    if isinstance(deps.datasets, FakeDatasetLoader):
        deps.datasets.seed()
    nodes = GraphNodes(deps, parallel_cap)
    nodes.agents.update(numbat_p1_agents(deps, presets))
    nodes.agents.update(numbat_report_agents(deps))
    if PLANNING in deps.features:
        nodes.agents.update(numbat_plan_agents(deps))
    return nodes


async def preset_rerun(
    store: FakeOutputStore,
    task: ReviewTask,
    record_id: str = "D-1",
    rerun_params: dict[str, Any] | None = None,
    eid: str = ENGAGEMENT.id,
) -> ReviewRecord:
    """模拟审计师对可重跑任务 (failed, unattributed) 提交 rerun 后 DecisionSubmit 写入 Store 的对象 (接口文档第 7 节
    决定的效果 rerun 行), 在一个事务内: 决定, 同程序同一次执行全部待决的可重跑任务 decided 新版本 (一轮失败算一组,
    Q17), 程序 executing 新版本 (params 合并 rerun_params, rerun_subject_ids 取整组对象). 组与程序新版本用
    DecisionSubmit 的同一套函数算. 返回写入的决定.
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


def numbat_confirmation_key(eid: str, account: str) -> str:
    """Numbat 银行账户 (NUMBAT_RECORDS) 的发函幂等键, 同 CSH-04 (含识别信息摘要, evidence.confirmation_key); 不在
    银行账户表中的账户 (用例造的, 如 BA-09) 没有识别信息, 摘要位写 not-in-table."""
    row = NUMBAT_RECORDS.get(("bank_account", account))
    if row is None:
        return f"{eid}:CSH-04:{account}:not-in-table"
    return confirmation_key(eid, BankAccount.model_validate(row))


async def legacy_sends(deps: AgentDeps, eid: str, accounts: Sequence[str], data_version: int | None = 1) -> None:
    """识别信息进键之前发的函: 每个账户一条旧写法键 ({eid}:CSH-04:{账户}) 的成功 external_call 留痕 (没有
    confirmation_id), 所属的 CSH-04 第 1 次执行 (记数据版本 data_version) 存进 Store."""
    run = finished(make_run("CSH-04")).model_copy(update={"data_version": data_version})
    await deps.output_store.put(eid, [run], actor=AgentId.A5)
    for account in accounts:
        entry = TrailEntry(
            engagement_id=eid,
            kind=TrailKind.external_call,
            actor=AgentId.A5,
            procedure_id="CSH-04",
            run_id=run.id,
            summary=f"Bank confirmation {account} sent",
            idempotency_key=legacy_key(eid, account),
            payload={"bank_account_id": account},
            created_at=NOW,
        )
        await deps.trail.record(entry)


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


def no_uploads() -> UploadedExtracts:
    """组合登记来源 (CompositeRegistrySource) 里没有上传任何 ASIC 摘录的一侧: 新建的客户的 ASIC 一律 no_extract
    (需求 R290), 测不涉及摘录的用例用."""
    financial = FakeFinancialStore()
    return UploadedExtracts(financial, FakeDataStore(), FakeEvidenceStore(financial))


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
    data: FakeDataStore | None = None,
    llm: LLMClient | None = None,
) -> tuple[GraphRunner, FakeOutputStore]:
    """新项目的 runner: 登记系统缺省有 Numbat 的记录, Store 缺省为空 (由 start 写入固定集). 返回 runner 与审计产出存储.

    agents: 按依赖造出要替换的 Agent, 如 lambda deps: {"ACC-04": ScriptAgent(deps, script)};
    其余程序同 make_graph_nodes.
    interrupt_before: 闸门之外另要停下的节点, 如 ["fan_in"] 停在汇合前查看暂存结果; 主图与闸门同样按功能集取.
    library: 替换程序库 (如改过 trigger 的), 缺省为 knowledge 目录.
    financial_store, trail, confirmations: 财务数据存储, 留痕与函证平台, 缺省见 make_deps (留痕缺省为新的空留痕).
    review_llm: 复核模型 (A7), 缺省为 mock 模式的固定输出; 给了时如争议用的变体 (tests/fixtures/llm_variants).
    features: 启用的功能, 缺省为基线, 给了 library 时缺省取它的 (见 make_deps).
    data: 数据管理存储, 缺省为种子已载入 (视同已确认就绪, P1 前不等数据).
    llm: 主模型, 缺省为 mock 模式的固定输出 (见 make_deps; 给了时 review_llm 也要给, 否则 A7 不复核).
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
        data=data,
        llm=llm,
    )
    nodes = make_graph_nodes(deps)
    if agents is not None:
        nodes.agents.update(agents(deps))
    if interrupt_before:
        app = stopping_graph(nodes, interrupt_before)
    else:
        app = compile_graph(nodes, InMemorySaver(serde=checkpoint_serde()))
    return GraphRunner(app, deps), output_store


def stopping_graph(nodes: GraphNodes, stops: Sequence[str]) -> CompiledStateGraph[EngagementState]:
    """nodes 上的主图 (内存检查点), 闸门之外另在 stops 各节点之前停下; 主图与闸门按 nodes 的功能集取, 同 compile_graph
    (planning 打开时有 PLN-15 的节点). make_runner 与 start_at_fan_in 共用."""
    features = nodes.deps.features
    return build_graph(nodes, features).compile(
        checkpointer=InMemorySaver(serde=checkpoint_serde()), interrupt_before=[*gate_nodes(features), *stops]
    )


async def wait_idle(runner: GraphRunner, eid: str) -> None:
    """等该项目的后台任务结束; 没有后台任务时直接返回."""
    task = runner.tasks.get(eid)
    if task is not None:
        await task


def fin08_registered(
    run_no: int, rows: Sequence[Mapping[str, Any]], decided_at: datetime = NOW
) -> dict[str, list[Any]]:
    """FIN-08 第 run_no 次执行已登记的状态片段 (需求 R161): 已决定的 manual 任务 T-RUN-FIN-08-{run_no}-v1 (第 2 版)
    与对它的 manual_result 决定 D-FIN08-{run_no}, details 为 ManagementResponses 的 rows (JSON 形式)."""
    run_id = f"RUN-FIN-08-{run_no}"
    request = ManualRequest(instructions="", result_model="ManagementResponses", min_evidence=0, material={})
    task = ReviewTask(
        id=f"T-{run_id}-v1",
        version=2,
        kind=TaskKind.manual,
        procedure_id="FIN-08",
        target_type=TargetType.ProcedureRun,
        target_id=run_id,
        target_version=1,
        run_id=run_id,
        subject_id="RESULT",
        status=TaskStatus.decided,
        created_at=decided_at,
        manual_request=request,
    )
    result = ManualResult(conclusion="已登记", performed_on=decided_at.date(), details={"responses": list(rows)})
    record = ReviewRecord(
        id=f"D-FIN08-{run_no}",
        task_id=task.id,
        target_version=1,
        decision=Decision.manual_result,
        manual_result=result,
        reviewed_by=ENGAGEMENT.owner_id,
        decided_at=decided_at,
    )
    return {"review_tasks": [task], "review_records": [record]}


class Session(Protocol):
    """在后台跑着主图的用例 (tests.test_decisions.Started): runner, 假审计产出存储与决定入口."""

    runner: GraphRunner
    output_store: FakeOutputStore
    submit: GraphDecisionSubmit


def latest(s: Session, model: type[T], oid: str) -> T:
    """假 Store (默认项目) 中 oid 的最新版本; 没有抛 ValueError."""
    return max((r for r in s.output_store.rows if isinstance(r, model) and r.id == oid), key=lambda r: r.version)


async def submit_and_wait(s: Session, decision: Any, eid: str = ENGAGEMENT.id) -> None:
    """提交决定, 等后台停下, 后台没有出错."""
    await s.submit.submit(eid, decision)
    await wait_idle(s.runner, eid)
    assert s.runner.run_error(eid) is None


def entries(s: Session, kind: TrailKind) -> list[TrailEntry]:
    """runner 的假留痕中 kind 类的行, 按写入顺序."""
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e for e in trail.entries if e.kind is kind]


def record_writers(monkeypatch: pytest.MonkeyPatch, store: FakeOutputStore) -> dict[tuple[str, int], str]:
    """之后经 store.put 写入的各 (ID, 版本) 的写入者 (假存储不记写入者): 记首次写入的那个, 重放时再写同一版本不改."""
    writers: dict[tuple[str, int], str] = {}
    put = store.put

    async def recording(eid: str, objs: Sequence[Versioned], *, actor: str) -> list[Versioned]:
        for o in objs:
            writers.setdefault((o.id, o.version), actor)
        return await put(eid, objs, actor=actor)

    monkeypatch.setattr(store, "put", recording)
    return writers


async def start_at_fan_in(
    deps: AgentDeps,
    state: EngagementState,
    pack: Sequence[Procedure],
    stops: Sequence[str],
    stored: Sequence[Versioned] = (),
) -> GraphRunner:
    """把主图放在 P2 结清处起跑 (真实 Agent, 闸门之外另在 stops 各节点之前停下, 错报闭环的用例只看其中一段): 程序包与
    stored (如错报的各版本) 先写进 Store (写入者 A1), 检查点以 fan_in 身份写入 state 与程序包, 后台推进到下一个停点
    或等待; 返回 runner (后台已停下, 没有出错)."""
    eid = state["engagement_id"]
    async with deps.output_store.transaction(eid):
        await deps.output_store.put(eid, [*pack, *stored], actor=AgentId.A1)
    runner = GraphRunner(stopping_graph(GraphNodes(deps, 2), stops), deps)
    await runner.app.aupdate_state(GraphRunner.config(eid), state | {"procedure_pack": list(pack)}, as_node="fan_in")
    runner.acquire(eid)
    runner.launch(eid, None)
    await wait_idle(runner, eid)
    assert runner.run_error(eid) is None
    return runner


# ── 经 HTTP 调接口: 依赖取自 make_runner 的 runner ──


@asynccontextmanager
async def api_client(
    runner: GraphRunner,
    user_id: str = NUMBAT_AUDITOR.id,
    deps: AgentDeps | None = None,
    llm_settings: LlmSettingsService | None = None,
    xero: XeroLink | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """在测试的事件循环里直接调 ASGI 应用 (不触发 lifespan), app.state 换成 runner 的依赖, 带 user_id 的登录令牌.

    与 runner 同一个事件循环, 后台推进与请求可以交替 await (TestClient 另开线程, 两边的锁与任务会分属两个循环).
    deps: 给了时接口取它 (如换掉数据集载入), 编排仍用 runner 的. llm_settings: 设置页接口用的服务.
    xero: Xero 网页连接的服务 (需求 R279), 给了时数据接口用它的 DataService, 概览的 sources.xero 读它的连接存储;
    缺省为假连接存储上的一个 (一律未连接).
    """
    app = create_app()
    app.state.deps = deps if deps is not None else runner.deps
    app.state.runner = runner
    app.state.query = GraphStateQuery(runner.app, runner.deps, runner)
    app.state.decisions = GraphDecisionSubmit(runner.app, runner.deps, runner)
    if xero is None:  # Xero 应用与主密钥一律置空, 不用开发者 .env 里的值 (connect 报 XERO_NOT_CONFIGURED)
        blank = get_settings().model_copy(
            update={"XERO_CLIENT_ID": "", "XERO_CLIENT_SECRET": "", "SETTINGS_SECRET_KEY": ""}
        )
        xero = XeroLink(runner.deps, DataService(runner.deps, runner), FakeXeroConnectionStore(), blank)
    xero.data.xero = xero.connections
    app.state.data_service = xero.data
    app.state.xero = xero
    app.state.llm_settings = llm_settings
    headers = {"Authorization": f"Bearer {create_access_token(user_id)}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        yield client


# ── Xero 演示公司的财务报表 fs_v1 的各行 (需求 R216 的表; 测试里只此一份) ──
# data/xero_demo 的 financial_statements/fs_v1.json 由补造脚本生成并入库; Issue #46 起只作审计师自己上传报表的样例, 系统
# 不读. test_xero_demo_data 核对它与本表逐行相同

# 17 个损益科目
PL_ACCOUNTS = [
    *("200", "270", "400", "404", "408", "412", "420", "429", "445"),
    *("449", "453", "461", "469", "477", "478", "489", "493"),
]
OPERATING_ACCOUNTS = [*PL_ACCOUNTS, "610", "620", "800", "801", "860", "820", "825", "804", "805", "826"]
FS_V1_LINES: list[tuple[str, str, str, list[str], int, bool, int, int]] = [
    # (行代码, 报表, 名称, 映射科目, sign, 小计, 本期金额, 比较数), 金额为分
    ("SOFP-CASH", "financial_position", "Cash and cash equivalents", ["091"], 1, False, 1158123, 1508123),
    ("SOFP-REC", "financial_position", "Trade receivables", ["610"], 1, False, 1019950, 0),
    ("SOFP-OREC", "financial_position", "Other receivables (accrued interest)", ["620"], 1, False, 4853, 0),
    ("SOFP-PPE", "financial_position", "Office equipment", ["710", "711"], 1, False, 0, 0),
    ("SOFP-TA", "financial_position", "Total assets", ["091", "610", "620", "710", "711"], 1, True, 2182926, 1508123),
    ("SOFP-OD", "financial_position", "Bank overdraft", ["090"], -1, False, 1090044, 457222),
    ("SOFP-PAY", "financial_position", "Trade and other payables", ["800", "801", "860"], -1, False, 400507, 0),
    ("SOFP-TAX", "financial_position", "GST and PAYG withholding", ["820", "825"], -1, False, 880197, -79120),
    ("SOFP-EMP", "financial_position", "Employee benefit liabilities", ["804", "805", "826"], -1, False, 895856, 0),
    ("SOFP-DLOAN", "financial_position", "Loan from director", ["840"], -1, False, 1921221, 1921221),
    (
        "SOFP-TL",
        "financial_position",
        "Total liabilities",
        ["090", "800", "801", "860", "820", "825", "804", "805", "826", "840"],
        -1,
        True,
        5187825,
        2299323,
    ),
    ("SOFP-EQ", "financial_position", "Accumulated losses", ["960", *PL_ACCOUNTS], -1, False, -3004899, -791200),
    ("PL-REV", "profit_or_loss", "Revenue", ["200"], -1, False, 3243100, 420000),
    ("PL-OI", "profit_or_loss", "Interest income", ["270"], -1, False, 4853, 0),
    ("PL-EMP", "profit_or_loss", "Employee benefits expense", ["477", "478"], 1, False, 4245856, 0),
    ("PL-OCC", "profit_or_loss", "Occupancy expenses", ["408", "445", "469"], 1, False, 635600, 0),
    (
        "PL-OTH",
        "profit_or_loss",
        "Other expenses",
        ["400", "404", "412", "420", "429", "449", "453", "461", "489", "493"],
        1,
        False,
        580196,
        1211200,
    ),
    ("PL-NP", "profit_or_loss", "Loss for the period", PL_ACCOUNTS, -1, True, -2213699, -791200),
    (
        "CE-OPEN",
        "changes_in_equity",
        "Balance at the beginning of the period",
        ["960", *PL_ACCOUNTS],
        -1,
        False,
        -791200,
        0,
    ),
    ("CE-NP", "changes_in_equity", "Loss for the period", PL_ACCOUNTS, -1, False, -2213699, -791200),
    ("CE-OCI", "changes_in_equity", "Other comprehensive income", [], -1, False, 0, 0),
    (
        "CE-TCI",
        "changes_in_equity",
        "Total comprehensive loss for the period",
        PL_ACCOUNTS,
        -1,
        True,
        -2213699,
        -791200,
    ),
    (
        "CE-CLOSE",
        "changes_in_equity",
        "Balance at the end of the period",
        ["960", *PL_ACCOUNTS],
        -1,
        True,
        -3004899,
        -791200,
    ),
    ("CF-NP", "cash_flows", "Loss for the period", PL_ACCOUNTS, 1, False, -2213699, -791200),
    ("CF-REC", "cash_flows", "Increase in trade receivables", ["610"], 1, False, -1019950, 0),
    ("CF-OREC", "cash_flows", "Increase in other receivables", ["620"], 1, False, -4853, 0),
    ("CF-PAY", "cash_flows", "Increase in trade and other payables", ["800", "801", "860"], 1, False, 400507, 0),
    ("CF-TAX", "cash_flows", "Change in GST and PAYG withholding", ["820", "825"], 1, False, 959317, -79120),
    (
        "CF-EMP",
        "cash_flows",
        "Increase in employee benefit liabilities",
        ["804", "805", "826"],
        1,
        False,
        895856,
        0,
    ),
    (
        "CF-OP",
        "cash_flows",
        "Net cash used in operating activities",
        OPERATING_ACCOUNTS,
        1,
        True,
        -982822,
        -870320,
    ),
    ("CF-INV", "cash_flows", "Net cash from investing activities", ["710", "711"], 1, False, 0, 0),
    ("CF-FIN", "cash_flows", "Net cash from financing activities", ["840"], 1, False, 0, 0),
    (
        "CF-NET",
        "cash_flows",
        "Net decrease in cash and cash equivalents",
        [*OPERATING_ACCOUNTS, "710", "711", "840"],
        1,
        True,
        -982822,
        -870320,
    ),
    ("CF-OPEN", "cash_flows", "Cash and cash equivalents at 1 July", ["090", "091"], 1, False, 1050901, 1921221),
    ("CF-CLOSE", "cash_flows", "Cash and cash equivalents at end of period", ["090", "091"], 1, False, 68079, 1050901),
]


# ── Xero Demo Company 快照: xero pull 的输出 (本仓库已带); 找不到时跳过用到它的测试, 仓库跟踪了却找不到时失败 ──

XERO_SNAPSHOT = "20261017-mock"  # 带付费接口模拟 (总账分录, 银行对账单) 的快照, 数据集名 xero/20261017-mock


def xero_snapshot() -> Snapshot:
    """Xero 快照 20261017-mock, 按 tests.external.snapshot_dir 定位与核对 (需求 R114, R294)."""
    return Snapshot(snapshot_dir(XERO_SNAPSHOT))


def xero_snapshots() -> Path:
    """Xero 快照目录的上级 (data/external/xero/api, 载入器与假 Xero 的根), 经 20261017-mock 快照定位."""
    return snapshot_dir(XERO_SNAPSHOT).parent


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
    """Xero 快照映射出的财务数据装进假财务存储: 发票, 分录与银行流水按行可取 (record), 另有试算表与科目表."""
    tables = build_tables(snapshot)
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for source, rows in (
        ("sales_invoice", tables["sales_invoices"]),
        ("journal_entry", tables["journal_entries"]),
        ("bank_transaction", tables["bank_transactions"]),
    ):
        records |= {(source, row.id): row.model_dump() for row in rows}
    return FakeFinancialStore(
        records=records, trial_balance=list(tables["trial_balance"]), accounts=list(tables["accounts"])
    )


# ── 上传文件处理 (需求 R14) 用的大模型: 先读变体目录, 再读主固定输出 ──

UPLOAD_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm_variants" / "upload"


class LayeredMockLLM:
    """按顺序在几个固定输出目录里找 {procedure_id}/{fixture_key}.json, 都没有再按顺序找 default.json.

    上传后的分类 (DOC-CLASSIFY) 与回函副本抽取 (DOC-06 按文件名) 的固定输出放在 llm_variants/upload,
    不进主固定输出 (主流程测试要求主固定输出各用恰好一次); 合同抽取 (DOC-04) 仍读主固定输出."""

    def __init__(self, *roots: Path) -> None:
        self.clients = [MockLLMClient(root) for root in roots]

    def locate(self, req: LLMRequest) -> tuple[MockLLMClient, str] | None:
        """要读的固定输出: (所在根的客户端, 相对根的文件); 都没有为 None."""
        for name in (f"{req.procedure_id}/{req.fixture_key}.json", f"{req.procedure_id}/default.json"):
            for client in self.clients:
                if (client.root / name).exists():
                    return client, name
        return None

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        found = self.locate(req)  # 选中的根里 {fixture_key}.json 要么就是它, 要么不存在, MockLLMClient 读的即它
        return await (found[0] if found is not None else self.clients[0]).complete(req, output_model)
