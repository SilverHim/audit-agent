"""测试用 Xero 演示数据集 (xero/20261017-q1) 的假存储与样例 (需求 R97), 供各程序段的 Agent 层测试使用.

预置的是演示数据导入 (R269) 之后的状态; 数据一律经载入器 (XeroDatasetLoader.tables), adapters.xero_supplements 与
演示导入清单读出, 这里不重写映射. 两组函数:
- 只需补造目录: xero_profile, xero_presets, xero_traits, xero_uploads, xero_documents, xero_pages, xero_adjustments,
  xero_employees, xero_registry, xero_confirmations, xero_contracts. 有快照时连快照一起核对 (Supplements.verify),
  没有快照时只核对补造文件 (verify_files).
- 需要快照: xero_tables, xero_financial_store, xero_engagement, XeroFakeDatasetLoader, XeroFakeDataStore,
  make_xero_deps. 快照按 tests.external.snapshot_dir 定位 (仓库跟踪了却找不到时失败, 没有时跳过).
读快照, 核对与文本层的结果按路径缓存, 一次测试运行里只算一次; 返回的列表与假存储每次新建, 测试可以改.
另有规则引擎结果的预置 XERO_RULES (需求 R107, 同 tests.fakes 的 NUMBAT_RULES), 取自连库跑一次的结果, 不读快照.

XeroFakeDatasetLoader.load 只记调用, 不登记文件, 与真实载入器只载快照 (R269) 的口径一致: 补造账表与文件都由演示
数据导入写入; 从空项目做演示导入的测试用 XeroFakeDataStore(ready=False) 与空的假财务存储 (tests/test_demo_import).
"""

import functools
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from backend.adapters.registry_mock import MockRegistrySource
from backend.adapters.xero_supplements import STAGES, DemoStage, DemoUpload, Supplements
from backend.agents.base import AgentDeps
from backend.agents.ingest import build_contract, contract_id
from backend.contracts.data import CheckInputs, TableKind, TableStats
from backend.contracts.llm_outputs import ContractExtraction
from backend.contracts.models import (
    AbnRecord,
    AsicCompanyRecord,
    AuditException,
    AuditObjective,
    ClientProfile,
    Contract,
    DocumentRef,
    DocumentType,
    Engagement,
    EvidencePointer,
    ProcedureRun,
    ReceiptChannel,
    RiskItem,
)
from backend.contracts.records import DatasetTraits, Employee, JournalEntry
from backend.contracts.services import RuleResult
from backend.persistence.stores import ADJUSTMENTS, XeroDatasetLoader, read_adjustments
from backend.services.judgment_engine import document_pointer
from tests.external import external_dir, require_xero_snapshot, snapshot_dir, supplements_dir
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    FakeConfirmationChannel,
    FakeDatasetLoader,
    FakeDataStore,
    FakeEngagementStore,
    FakeEvidenceStore,
    FakeFinancialStore,
    FakeOutputStore,
    FakeRegistry,
    FakeRuleEngine,
    FakeTrail,
    csh02_subsequent_rows,
    csh02_window_rows,
    make_deps,
)

REPO = Path(__file__).resolve().parents[1]
XERO_DATASET = "xero/20261017-q1"
SNAPSHOT_NAME = "20261017-q1"  # 快照目录名, data/external/xero/api/20261017-q1
PLANNING = REPO / "src" / "backend" / "knowledge" / "planning"  # 通用计划模板 (载入器的构造参数)
DOC04_FIXTURES = REPO / "tests" / "fixtures" / "llm_xero" / "DOC-04"  # xero_contracts 的来源
# 查快照用的仓库根 (require_xero_snapshot 的 repo); 测试把它指向空目录来模拟没有快照
SNAPSHOT_REPO = REPO
# 补造画像 id 须带的前缀 (快照组织 ID 推出, 见 Supplements.client_profile); 写成常量, 没有快照时也能读画像
CLIENT_PREFIX = "CLIENT-XERO-3C1332BB-"
# 行类型 (FinancialDataStore.record 的 source_type) ← 账表; 试算表另按科目代码取本期行, 同真实实现
RECORD_TYPES: dict[TableKind, str] = {
    TableKind.journal_entries: "journal_entry",
    TableKind.sales_invoices: "sales_invoice",
    TableKind.supplier_bills: "supplier_bill",
    TableKind.delivery_notes: "delivery_note",
    TableKind.bank_accounts: "bank_account",
    TableKind.bank_transactions: "bank_transaction",
}


# ── 只需补造目录 ──


def located_snapshot(repo: Path) -> Path | None:
    """repo 下 (或 EXTERNAL_DATA_ROOT, 主工作副本) 的快照目录; 没有为 None, 不跳过."""
    found = external_dir("xero", repo, [f"api/{SNAPSHOT_NAME}/organisation.json"])
    return found / "api" / SNAPSHOT_NAME if found is not None else None


@functools.cache
def verified(repo: Path) -> tuple[Supplements, dict[str, Any]]:
    """已核对的补造目录与它的 manifest: 有快照时 verify (补造文件与快照), 没有时 verify_files (只核补造文件)."""
    root = supplements_dir(SNAPSHOT_NAME)
    snapshot = located_snapshot(repo)
    found = Supplements(root, snapshot if snapshot is not None else root / "<没有快照>", XERO_DATASET)
    return found, found.verify() if snapshot is not None else found.verify_files()


def supplement() -> Supplements:
    return verified(SNAPSHOT_REPO)[0]


def manifest_documents() -> dict[str, dict[str, Any]]:
    """manifest.documents: 文件 ID → 条目."""
    return {d["document_id"]: d for d in verified(SNAPSHOT_REPO)[1]["documents"]}


def xero_profile() -> ClientProfile:
    """补造的客户画像 CLIENT-XERO-3C1332BB-Q1 v1."""
    profile = supplement().client_profile(CLIENT_PREFIX)
    assert profile is not None
    return profile


def xero_presets() -> tuple[list[AuditObjective], list[RiskItem]]:
    """补造的计划预置: 3 个审计目标, 3 项风险."""
    presets = supplement().planning_presets()
    assert presets is not None
    return presets


def xero_traits() -> DatasetTraits:
    traits = supplement().dataset_traits()
    assert traits is not None
    return traits


def xero_uploads(stage: DemoStage | None = None) -> list[DemoUpload]:
    """演示导入清单的上传文件 (Supplements.uploads, 同 plan): stage 为空取全部 23 份, 否则只取该阶段的."""
    return [u for u in supplement().uploads() if stage is None or u.stage == stage]


def xero_documents(stage: DemoStage) -> list[DocumentRef]:
    """到 stage 为止 (含) 各阶段上传并补登记后的文件登记, 按清单顺序: 渠道 auditor_upload, 上传人为项目负责人,
    类型, 可靠性, 文件日期与关联对象同清单, 哈希与页数取 manifest; 合同已抽取 (extracted), 其余 classified.
    份数: acceptance 2, fieldwork 21, statements 22, manual 23."""
    done = STAGES[: STAGES.index(stage) + 1]
    entries = manifest_documents()
    return [
        DocumentRef(
            id=u.document_id,
            type=u.document_type,
            file_name=u.file_name,
            sha256=entries[u.document_id]["sha256"],
            page_count=entries[u.document_id]["page_count"],
            reliability=u.reliability,
            status="extracted" if u.document_type is DocumentType.contract else "classified",
            uploaded_at=NOW,
            channel="auditor_upload",
            uploaded_by=ENGAGEMENT.owner_id,
            document_date=u.document_date,
            related_ids=u.related_ids,
        )
        for u in xero_uploads()
        if u.stage in done
    ]


@functools.cache
def pdf_pages(path: Path, sha256: str) -> tuple[str, ...]:
    """PDF 文本层各页 (pdfplumber, 同 FileEvidenceStore.pages), 按哈希缓存."""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return tuple(page.extract_text() or "" for page in pdf.pages)


def xero_pages(document_id: str) -> list[str]:
    """manifest 登记的一份 PDF 的文本层; 不在 manifest 抛 LookupError."""
    entry = manifest_documents().get(document_id)
    if entry is None:
        raise LookupError(f"document {document_id} 不在 Xero 补造目录的 manifest 中")
    return list(pdf_pages(supplement().root / entry["path"], entry["sha256"]))


def xero_adjustments(entry_nos: Sequence[str] | None = None) -> list[JournalEntry]:
    """补造的更正分录 (11 行), 按行 ID; entry_nos 给了时只取这些凭证."""
    rows = read_adjustments(supplement().root / ADJUSTMENTS)
    return sorted((r for r in rows if entry_nos is None or r.entry_no in entry_nos), key=lambda r: r.id)


def xero_employees() -> list[Employee]:
    """补造的工资册 (3 行)."""
    return supplement().employees()


def xero_registry() -> FakeRegistry:
    """登记 mock 的记录装进假登记系统: 补造目录 registry/ 的 ABN 与 ASIC 登记 (各 3 条), 公司摘录 (3 份) 与人员检索
    (1 份, 需求 R129); 摘录与检索经 MockRegistrySource 读出 (带 PDF 原件)."""
    root = supplement().root / "registry"
    abn = json.loads((root / "abn.json").read_text(encoding="utf-8"))
    asic = json.loads((root / "asic.json").read_text(encoding="utf-8"))
    mock = MockRegistrySource(supplement().root)
    extracts = {p.stem: mock.extract(p.stem) for p in sorted((root / "extracts").glob("*.json"))}
    persons = []
    for path in sorted((root / "persons").glob("*.json")):
        keys = json.loads(path.read_text(encoding="utf-8"))
        persons.append(mock.person(keys["family_name"], keys["given_name"], date.fromisoformat(keys["birth_date"])))
    return FakeRegistry(
        {k: AbnRecord.model_validate(v) for k, v in abn.items()},
        {k: AsicCompanyRecord.model_validate(v) for k, v in asic.items()},
        {k: v for k, v in extracts.items() if v is not None},
        [p for p in persons if p is not None],
    )


def xero_confirmations() -> FakeConfirmationChannel:
    """假函证平台: 两个银行账户 (12345678, 12345679) 回函, 内容为补造的回函 PDF, 渠道取补造目录的
    confirmations/receipts.json (同回函 mock). 假平台只有一个渠道, 各账户的渠道不同时抛 ValueError."""
    root = supplement().root
    replies = [Path(d["path"]) for d in manifest_documents().values() if d["registration"] == "confirmation"]
    receipts = json.loads((root / "confirmations" / "receipts.json").read_text(encoding="utf-8"))
    channels = {receipts[p.stem]["receipt_channel"] for p in replies}
    if len(channels) != 1:
        raise ValueError(f"回函渠道不止一种: {sorted(channels)}")
    return FakeConfirmationChannel(
        ReceiptChannel(channels.pop()), bodies={p.stem: (root / p).read_bytes() for p in sorted(replies)}
    )


def xero_contracts() -> list[Contract]:
    """3 份收入合同第 1 版: 按 DOC-04 的 fixture (tests/fixtures/llm_xero/DOC-04) 组装, 同上传后 A3 的抽取 (指针的证据
    种类与可靠性按文件登记改写, 同输出校验的 document_pointer); 执行 ID 为 UPLOAD-{文件 ID}-JOB. 按合同编号."""
    contracts = []
    for d in sorted((d for d in xero_documents("fieldwork") if d.type is DocumentType.contract), key=contract_id):
        extraction = ContractExtraction.model_validate_json((DOC04_FIXTURES / f"{contract_id(d)}.json").read_bytes())
        evidence = [document_pointer(p, d)[0] or p for p in extraction.evidence]
        validated = extraction.model_copy(update={"evidence": evidence})
        contracts.append(build_contract(contract_id(d), d, validated, f"UPLOAD-{d.id}-JOB"))
    return contracts


# ── 需要快照 ──


def snapshot_root(repo: Path) -> Path:
    """repo 下的快照目录: 本仓库经 tests.external.snapshot_dir (会话缓存); 自测把 SNAPSHOT_REPO 换成别的目录时
    直接定位."""
    return snapshot_dir(SNAPSHOT_NAME) if repo == REPO else require_xero_snapshot(SNAPSHOT_NAME, repo)


def xero_loader() -> XeroDatasetLoader:
    """不连库的 Xero 载入器 (只用它的读取方法)."""
    api, supplements = snapshot_root(SNAPSHOT_REPO).parent, supplements_dir(SNAPSHOT_NAME).parent
    return XeroDatasetLoader(api, PLANNING, cast(Any, object()), cast(Any, object()), supplements=supplements)


@functools.cache
def loaded_tables(api: Path) -> dict[str, tuple[Any, ...]]:
    supplements = supplements_dir(SNAPSHOT_NAME).parent
    loader = XeroDatasetLoader(api, PLANNING, cast(Any, object()), cast(Any, object()), supplements=supplements)
    return {name: tuple(rows) for name, rows in loader.tables(XERO_DATASET).items()}


def xero_tables() -> dict[str, list[Any]]:
    """演示数据导入之后的八张表 (XeroDatasetLoader.tables, 含应付账单)."""
    return {name: list(rows) for name, rows in loaded_tables(snapshot_root(SNAPSHOT_REPO).parent).items()}


def put_rows(store: FakeFinancialStore, kind: TableKind, rows: Sequence[BaseModel]) -> None:
    """一张表的行整表放进假财务存储 (按 FinancialDataStore 各读取方法取得到的位置): 明细行进 records, 试算表另进
    trial_balance 并按科目代码记本期行, 科目表进 accounts, 工资册进 employees."""
    if kind is TableKind.accounts:
        store.account_rows = list(cast(Any, rows))
    elif kind is TableKind.employees:
        store.employee_rows = [Employee.model_validate(r.model_dump()) for r in rows]
    elif kind is TableKind.trial_balance:
        store.trial_balance_rows = list(cast(Any, rows))
        store.records = {key: row for key, row in store.records.items() if key[0] != "trial_balance"}
        store.records |= {
            ("trial_balance", r.account_code): r.model_dump() for r in cast(Any, rows) if r.period == "current"
        }
    elif kind in RECORD_TYPES:
        source = RECORD_TYPES[kind]
        store.records = {key: row for key, row in store.records.items() if key[0] != source}
        store.records |= {(source, cast(Any, r).id): r.model_dump() for r in rows}


def xero_financial_store(stage: DemoStage = "fieldwork", contracts: bool = True) -> FakeFinancialStore:
    """装好演示数据的假财务存储: 八张表 (put_rows) 与工资册, 到 stage 为止上传的文件; contracts 为真时预置
    xero_contracts() (合同在 fieldwork 上传, stage 为 acceptance 时抛 ValueError)."""
    if contracts and stage == "acceptance":
        raise ValueError("合同在 fieldwork 阶段才上传, acceptance 阶段不能预置合同")
    store = FakeFinancialStore(documents=xero_documents(stage), contracts=xero_contracts() if contracts else None)
    for name, rows in xero_tables().items():
        put_rows(store, TableKind(name), rows)
    put_rows(store, TableKind.employees, xero_employees())
    return store


def xero_engagement() -> Engagement:
    """以补造画像为客户的项目: 编号与负责人同 ENGAGEMENT, 期间取快照 (2026-07-01 至 2026-09-30)."""
    period = xero_loader().period(XERO_DATASET)
    assert period is not None
    profile = xero_profile()
    return ENGAGEMENT.model_copy(
        update={
            "client_id": profile.id,
            "client_profile_version": profile.version,
            "dataset": XERO_DATASET,
            "period_start": period[0],
            "period_end": period[1],
        }
    )


class XeroFakeDatasetLoader(FakeDatasetLoader):
    """Xero 演示的假数据集载入: load 只记调用, 不登记文件 (演示数据导入之后的状态); 更正分录, 计划预置, 客户画像与
    数据集声明取补造目录."""

    def __init__(self, financial_store: FakeFinancialStore) -> None:
        super().__init__(financial_store, adjustments=xero_adjustments(), traits={XERO_DATASET: xero_traits()})
        self.span = xero_loader().period(XERO_DATASET)

    def seed(self) -> "XeroFakeDatasetLoader":
        return self  # 文件由演示数据导入上传, 不由载入登记

    def planning_presets(self, dataset: str) -> tuple[list[AuditObjective], list[RiskItem]]:
        return xero_presets()

    def client_profile(self, dataset: str) -> ClientProfile:
        return xero_profile()

    def datasets(self) -> list[str]:
        return [XERO_DATASET]

    def period(self, dataset: str) -> tuple[date, date] | None:
        return self.span


def table_stats(tables: Mapping[str, Sequence[Any]]) -> dict[TableKind, TableStats]:
    stats = {TableKind(name): TableStats(records=len(rows)) for name, rows in tables.items()}
    current = sum(r.period == "current" for r in tables["trial_balance"])
    prior = len(tables["trial_balance"]) - current
    stats[TableKind.trial_balance] = TableStats(records=current + prior, current=current, prior=prior)
    return stats


class XeroFakeDataStore(FakeDataStore):
    """Xero 演示的假数据管理存储: 数据已就绪, 行数与体检汇总按 Xero 的七张表 (数据集 xero/20261017-q1), 工资册的
    行数按假财务存储里预置的;
    write_tables 照 FakeDataStore 记版本, 并把行转交给同一个假财务存储 (put_rows). ready 为假时是还没有数据的新项目
    (数据版本 0, 未确认就绪), 供演示数据导入 (需求 R269) 从空写起; tables 随之给空表."""

    def __init__(self, financial: FakeFinancialStore, tables: Mapping[str, Sequence[Any]], ready: bool = True) -> None:
        stats = table_stats(tables) | {TableKind.employees: TableStats(records=len(financial.employee_rows))}
        super().__init__(ready=ready, stats=stats)
        self.seeded = False  # 不以 Numbat 为底
        self.financial = financial
        kinds = (TableKind.accounts, TableKind.trial_balance, TableKind.journal_entries)
        self.tables = {kind: list(tables[kind]) for kind in kinds}  # 体检汇总读这三张

    async def check_inputs(
        self, eid: str, period_start: date, period_end: date, *, adjustment_prefix: str | None = None
    ) -> CheckInputs:
        inputs = await super().check_inputs(eid, period_start, period_end, adjustment_prefix=adjustment_prefix)
        return inputs.model_copy(update={"dataset": XERO_DATASET})

    async def write_tables(self, eid: str, tables: Mapping[TableKind, Sequence[BaseModel]], **kwargs: Any) -> int:
        version = await super().write_tables(eid, tables, **kwargs)
        assert self.financial is not None
        for kind, rows in tables.items():
            if kind is not TableKind.approval_log:
                put_rows(self.financial, kind, rows)
        return version


class XeroFakeEvidenceStore(FakeEvidenceStore):
    """pages 对 manifest 登记的 Xero 文件返回真实文本层 (xero_pages); 上传 (save) 补造文件时页数取 manifest (同真实
    存储数 PDF 的页数, 抽取的页码校验要用); 其余同 FakeEvidenceStore."""

    def __init__(self, financial_store: FakeFinancialStore, output_store: FakeOutputStore | None = None) -> None:
        super().__init__(financial_store, output_store)
        self.page_counts.update({Path(d["path"]).name: d["page_count"] for d in manifest_documents().values()})

    async def pages(self, eid: str, document_id: str) -> list[str]:
        if document_id in manifest_documents() and any(d.id == document_id for d in self.financial_store.document_refs):
            return xero_pages(document_id)
        return await super().pages(eid, document_id)


def make_xero_deps(
    registry: FakeRegistry | None = None,
    trail: FakeTrail | None = None,
    engagement_store: FakeEngagementStore | None = None,
    output_store: FakeOutputStore | None = None,
    financial_store: FakeFinancialStore | None = None,
    **kwargs: Any,
) -> AgentDeps:
    """同 make_deps (其余参数原样转交), 缺省换成 Xero 演示的假实现: 登记 xero_registry(), 项目存储只有 Xero 画像与
    xero_engagement(), 财务存储 xero_financial_store(), 函证 xero_confirmations(), 数据管理 XeroFakeDataStore;
    证据文件存储与数据集载入换成 XeroFakeEvidenceStore 与 XeroFakeDatasetLoader. 规则引擎缺省没有预置结果 (各程序返回
    空结果, 不沿用 make_deps 缺省的 Numbat 例外), 要例外的测试自己给 rules."""
    financial = financial_store if financial_store is not None else xero_financial_store()
    outputs = output_store if output_store is not None else FakeOutputStore()
    kwargs.setdefault("confirmations", xero_confirmations())
    kwargs.setdefault("rules", FakeRuleEngine({}))
    kwargs.setdefault("data", XeroFakeDataStore(financial, xero_tables()))
    deps = make_deps(
        registry if registry is not None else xero_registry(),
        trail if trail is not None else FakeTrail(),
        engagement_store
        if engagement_store is not None
        else FakeEngagementStore([xero_profile()], [xero_engagement()]),
        outputs,
        financial,
        **kwargs,
    )
    return replace(
        deps, evidence_store=XeroFakeEvidenceStore(financial, outputs), datasets=XeroFakeDatasetLoader(financial)
    )


# ── 规则结果预置 (需求 R107) ──
# 规则引擎对 Xero 演示项目 (演示数据导入 acceptance 与 fieldwork 之后) 的结果, 供 FakeRuleEngine(XERO_RULES).
# PLN-07, PLN-01, REV-08, REV-02 S01 与 CSH-05 为常量, 取自连库跑一次 SqlRuleEngine 的结果: 测试库, 期间 2026-07-01
# 至 09-30, 上期起始日 2025-07-01 (数据集声明); 重要性取 {revenue, 0.01, 0.5, 0.05} 即 32431 / 16216 / 1622 分;
# REV-08, REV-02 关联的目标为 significant. 值与类型同库: SUM 得 numeric (Decimal), 例外 detail 里的 numeric 经
# to_jsonable_python 成字符串, 证据指针的 locator 为 SqlRuleEngine 拼的列名. 连库结果与它们一致由 R112 断言.
# CSH-02 的 D02, D03, D04 是对表的筛选, 由假存储的表按规则口径派生 (xero_csh02_rows), 不写常量.
XERO_A01_LOCATOR = "amount_cents, account_name, basis, current_cents, prior_cents, opening_cents, change_pct"
# PLN-07 A01 命中的 9 个资产负债类科目, 按代码: (代码, 名称, 本期, 上期, 期初, 变动比例); 上期没有余额的科目期初为 0,
# 变动比例为空 (除数为 0)
XERO_A01_CHANGES = [
    ("090", "Business Bank Account", "-1090044", "-870320", "-870320", "-0.25246346171523117934"),
    ("091", "Business Savings Account", "1158123", None, "0", None),
    ("610", "Accounts Receivable", "1019950", None, "0", None),
    ("710", "Office Equipment", "75000", None, "0", None),
    ("711", "Less Accumulated Depreciation on Office Equipment", "-82500", None, "0", None),
    ("800", "Accounts Payable", "-400516", None, "0", None),
    ("820", "GST", "-119197", "79120", "79120", "-2.5065343781597573"),
    ("825", "PAYG Withholdings Payable", "-753500", None, "0", None),
    ("840", "Historical Adjustment", "-1921221", None, "0", None),
]
XERO_A02_ROWS = [  # PLN-07 A02: 收入与销售成本, 本期与上期 (两期都没有销售成本)
    {"period": "current", "revenue_cents": Decimal("3243100"), "cost_of_sales_cents": Decimal("0")},
    {"period": "prior", "revenue_cents": Decimal("420000"), "cost_of_sales_cents": Decimal("0")},
]
XERO_A03_ROWS = [  # PLN-07 A03: 本期按月的收入, 费用与员工费用
    {
        "month": date(2026, month, 1),
        "revenue_cents": Decimal(revenue),
        "expense_cents": Decimal(expense),
        "employee_expense_cents": Decimal(employee),
    }
    for month, revenue, expense, employee in [
        (7, "460000", "331117", "0"),
        (8, "1803600", "2441870", "2010000"),
        (9, "979500", "1792809", "1340000"),
    ]
]
XERO_M01_ROWS = [  # PLN-01 M01: 三个基准的金额, 按 basis 排序 (税前亏损)
    {"basis": "profit_before_tax", "base_amount_cents": Decimal("-1322696")},
    {"basis": "revenue", "base_amount_cents": Decimal("3243100")},
    {"basis": "total_assets", "base_amount_cents": Decimal("1080529")},
]
ORC1026 = "SI-77b338ef-ecc0-4b95-a0d7-2617b0054611"  # DIISR 的发票 ORC1026 (合同 CON-X01)
XERO_S01_ROWS = [  # REV-02 S01: 期末前 20 天内不低于执行重要性 (16216) 的发票, 按 ID; 列按 REV-02.yaml
    {
        "id": sid,
        "invoice_no": number,
        "contract_id": contract,
        "posting_date": date(2026, 9, day),
        "amount_cents": amount,
    }
    for sid, number, contract, day, amount in (
        ("SI-3b28bf11-ed2f-4cf4-8e9e-fcae730cc292", "ORC1021", None, 17, 55000),
        ("SI-5aadcd34-01a9-4b8d-a2bb-d7cc1de9fa45", "ORC1024", None, 16, 55000),
        ("SI-5f6deadf-36a2-495a-9980-ceb11e8af9a9", "ORC1020", "CON-X02", 17, 55000),
        ("SI-77b338ef-ecc0-4b95-a0d7-2617b0054611", "ORC1026", "CON-X01", 17, 715000),
        ("SI-a9f765e6-b9bc-4505-a47b-fb3ecb327e7b", "ORC1022", None, 17, 55000),
        ("SI-bcd8a71f-aa31-4d0f-8a01-13ea26363ddf", "ORC1023", None, 17, 55000),
    )
]
CSH02_LOOKBACK_DAYS = 5  # CSH-02 D03, D04 的窗口与 D02 的期后天数, 同 CSH-02.yaml
CSH02_SUBSEQUENT_DAYS = 10


def xero_csh02_rows() -> dict[str, list[dict[str, Any]]]:
    """CSH-02 D02, D03, D04 的行: 由演示数据导入之后的表 (xero_tables) 按规则的口径派生, 同 Numbat
    (tests.fakes 的 csh02_subsequent_rows, csh02_window_rows); 期末取快照, 作废对按数据集声明的格式与模板剔除.
    读快照, 规则引擎取 CSH-02 时才算, 每次新建. 结果: D02 为 BA-090 期后 21 行 (BA-091 期后没有流水), D03 中
    BA-091 只剩 XJ-601-1, D04 里没有 4853 (BA-091 回函的应计利息) 的行."""
    tables = xero_tables()
    traits = xero_traits()
    period = xero_loader().period(XERO_DATASET)
    assert period is not None
    window = csh02_window_rows(
        tables["journal_entries"],
        tables["bank_accounts"],
        period[1],
        CSH02_LOOKBACK_DAYS,
        traits.reversal_reference_pattern,
        traits.reversal_entry_no_template,
    )
    return {"D02": csh02_subsequent_rows(tables["bank_transactions"], period[1], CSH02_SUBSEQUENT_DAYS), **window}


def xero_pln07_exceptions(run: ProcedureRun) -> list[AuditException]:
    """PLN-07 A01 的 9 条变动例外 X-PLN-07-{run_no}-A01-1 至 -9 (按科目代码), 金额为本期减期初."""
    return [
        AuditException(
            id=f"X-PLN-07-{run.run_no}-A01-{n}",
            procedure_id="PLN-07",
            run_id=run.id,
            rule_id="A01",
            severity="medium",
            source_ids=[code],
            amount_cents=int(current) - int(opening),
            detail={
                "account_name": name,
                "basis": "balance_sheet",
                "current_cents": current,
                "prior_cents": prior,
                "opening_cents": opening,
                "change_pct": change,
            },
            evidence=[
                EvidencePointer(
                    type="voucher",
                    source_type="trial_balance",
                    source_id=code,
                    locator=XERO_A01_LOCATOR,
                    reliability="internal_original",
                )
            ],
        )
        for n, (code, name, current, prior, opening, change) in enumerate(XERO_A01_CHANGES, 1)
    ]


def xero_rev08_exception(run: ProcedureRun) -> AuditException:
    """REV-08 R03 对 ORC1026 的收款不符例外: 发票 715000, 期后收到 385000 (BT-090-0124), 差 330000."""
    return AuditException(
        id=f"X-REV-08-{run.run_no}-R03-1",
        procedure_id="REV-08",
        run_id=run.id,
        rule_id="R03",
        severity="medium",
        source_ids=[ORC1026],
        amount_cents=330000,
        detail={"invoice_amount_cents": 715000, "received_cents": "385000", "bank_transaction_ids": "BT-090-0124"},
        evidence=[
            EvidencePointer(
                type="invoice",
                source_type="sales_invoice",
                source_id=ORC1026,
                locator="amount_cents, invoice_amount_cents, received_cents, bank_transaction_ids",
                reliability="internal_original",
            )
        ],
    )


XERO_RULES: dict[
    str, Callable[[ProcedureRun], RuleResult]
] = {  # 规则引擎对 Xero 演示的结果, 按程序编号 (同 NUMBAT_RULES)
    "PLN-07": lambda run: RuleResult(
        exceptions=xero_pln07_exceptions(run), rows={"A02": XERO_A02_ROWS, "A03": XERO_A03_ROWS}
    ),
    "PLN-01": lambda run: RuleResult(exceptions=[], rows={"M01": XERO_M01_ROWS}),
    "REV-08": lambda run: RuleResult(exceptions=[xero_rev08_exception(run)], rows={}),
    "REV-02": lambda run: RuleResult(exceptions=[], rows={"S01": XERO_S01_ROWS}),
    "CSH-05": lambda run: RuleResult(exceptions=[], rows={}),  # 两个账户对账单的期末余额都等于试算表
    "CSH-02": lambda run: RuleResult(exceptions=[], rows=xero_csh02_rows()),
}
