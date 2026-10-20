"""测试用 Xero 演示数据集 (xero/20261017-q1) 的假存储与样例 (需求 R97), 供各程序段的 Agent 层测试使用.

预置的是演示数据导入 (R269) 之后的状态; 数据一律经载入器 (XeroDatasetLoader.tables), adapters.xero_supplements 与
演示导入清单读出, 这里不重写映射. 两组函数:
- 只需补造目录: xero_profile, xero_presets, xero_traits, xero_uploads, xero_documents, xero_pages, xero_adjustments,
  xero_employees, xero_registry, xero_confirmations, xero_contracts. 有快照时连快照一起核对 (Supplements.verify),
  没有快照时只核对补造文件 (verify_files).
- 需要快照: xero_tables, xero_financial_store, xero_engagement, XeroFakeDatasetLoader, XeroFakeDataStore,
  make_xero_deps. 快照按 tests.external.snapshot_dir 定位 (仓库跟踪了却找不到时失败, 没有时跳过).
读快照, 核对与文本层的结果按路径缓存, 一次测试运行里只算一次; 返回的列表与假存储每次新建, 测试可以改.
两组函数都可传 loader (不连库的 XeroDatasetLoader, 如注入副本 XeroCase.loader, 需求 R118): 数据改取它指向的快照与
补造目录 (经 loader.supplement 核对, 不缓存); 缺省为原目录.
另有规则引擎结果的预置 XERO_RULES (需求 R107, 同 tests.fakes 的 NUMBAT_RULES), 取自连库跑一次的结果, 不读快照.

XeroFakeDatasetLoader.load 只记调用, 不登记文件, 与真实载入器只载快照 (R269) 的口径一致: 补造账表与文件都由演示
数据导入写入; 从空项目做演示导入的测试用 XeroFakeDataStore(ready=False) 与空的假财务存储 (tests/test_demo_import).
"""

import functools
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
from backend.contracts.data import XERO_TABLES, CheckInputs, TableKind, TableStats
from backend.contracts.llm_outputs import ContractExtraction, JudgmentDraft
from backend.contracts.models import (
    AbnRecord,
    Annotation,
    AsicCompanyRecord,
    AuditException,
    AuditObjective,
    AutonomyLevel,
    ClientProfile,
    Contract,
    Decision,
    DocumentRef,
    DocumentType,
    Engagement,
    EvidencePointer,
    GateDecision,
    GateId,
    GateVerdict,
    Judgment,
    MaterialityChoice,
    Misstatement,
    MisstatementDraft,
    OpinionType,
    ProcedureRun,
    ReceiptChannel,
    ReportSection,
    ReviewRecord,
    ReviewStatus,
    RiskItem,
    RuleOutcome,
)
from backend.contracts.records import DatasetTraits, Employee, JournalEntry
from backend.contracts.services import RuleResult
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.runner import GraphRunner
from backend.persistence.stores import ADJUSTMENTS, XeroDatasetLoader, read_adjustments
from backend.services.features import BASELINE, parse_features
from backend.services.judgment_engine import document_pointer
from tests.external import external_dir, require_xero_snapshot, snapshot_dir, supplements_dir
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    PLAN_CONFIRMATION,
    PLAN_REJECT_REASON,
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
    gen05_exception,
    make_deps,
    wait_idle,
)
from tests.fixture_sets import XERO

REPO = Path(__file__).resolve().parents[1]
XERO_DATASET = "xero/20261017-q1"
XERO_PERIOD = (date(2026, 7, 1), date(2026, 9, 30))  # 快照的本期 (dataset.json)
SNAPSHOT_NAME = "20261017-q1"  # 快照目录名, data/external/xero/api/20261017-q1
# 演示配置的功能集: 取 .env.example 的 FEATURES (演示走通, 真实模型变体与 PLN-15 固定输出的守卫共用这一处)
DEMO_FEATURES = parse_features(
    next(
        line
        for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.startswith("FEATURES=")
    ).split("=", 1)[1]
)
# 演示配置走一遍各程序读固定输出 (演示运行副本) 的次数: 上传文件分类 21 份 (承接 2, 外勤 19), 合同 3, 回函 2, REV-03
# 三份合同, REV-02 六张发票, CSH-02 一户, GEN-02 七笔凭证 (需求 R146), RPT-03 两稿, PLN-15 两个修订 (需求 R317); 每次都
# 一次通过
FIXTURE_READS = {
    "DOC-CLASSIFY": 21,
    "DOC-04": 3,
    "DOC-06": 2,
    "REV-03": 3,
    "REV-02": 6,
    "CSH-02": 1,
    "GEN-02": 7,
    "RPT-03": 2,
    "PLN-15": 2,
}
FIXED_CALLS = sum(FIXTURE_READS.values())  # 固定输出走一遍的大模型调用数
# 演示的两次 PLN-15 决定的请求体: 先退回 (要求写明银行函证的时间), 再批准 (确认同 R205; 需求 R317)
PLAN_DECISIONS = (
    {"decision": "reject", "reason": PLAN_REJECT_REASON},
    {"decision": "approve", "plan_confirmation": PLAN_CONFIRMATION.model_dump(mode="json")},
)
PLANNING = REPO / "src" / "backend" / "knowledge" / "planning"  # 通用计划模板 (载入器的构造参数)
DOC04_FIXTURES = REPO / "tests" / "fixtures" / "llm_xero" / "DOC-04"  # xero_contracts 的来源
# 查快照用的仓库根 (require_xero_snapshot 的 repo); 测试把它指向空目录来模拟没有快照
SNAPSHOT_REPO = REPO
# 补造画像 id 须带的前缀 (快照组织 ID 推出, 见 Supplements.client_profile); 写成常量, 没有快照时也能读画像
CLIENT_PREFIX = "CLIENT-XERO-3C1332BB-"
EXTRACTED = {DocumentType.contract}  # 演示导入后已抽取的文件类型
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


def supplement(loader: XeroDatasetLoader | None = None) -> Supplements:
    """已核对的补造目录: 缺省为原补造目录; 给了 loader 时取它的 (副本带 injected, 由 loader 的 allow_injected 放行)."""
    if loader is None:
        return verified(SNAPSHOT_REPO)[0]
    found = loader.supplement(XERO_DATASET)
    assert found is not None, "loader 没有补造目录"
    return found


def manifest_documents(loader: XeroDatasetLoader | None = None) -> dict[str, dict[str, Any]]:
    """manifest.documents: 文件 ID → 条目."""
    manifest = verified(SNAPSHOT_REPO)[1] if loader is None else supplement(loader).verify_files()
    return {d["document_id"]: d for d in manifest["documents"]}


def xero_profile(loader: XeroDatasetLoader | None = None) -> ClientProfile:
    """补造的客户画像 CLIENT-XERO-3C1332BB-Q1 v1."""
    profile = supplement(loader).client_profile(CLIENT_PREFIX)
    assert profile is not None
    return profile


def xero_presets(loader: XeroDatasetLoader | None = None) -> tuple[list[AuditObjective], list[RiskItem]]:
    """补造的计划预置: 3 个审计目标, 3 项风险."""
    presets = supplement(loader).planning_presets()
    assert presets is not None
    return presets


def xero_traits(loader: XeroDatasetLoader | None = None) -> DatasetTraits:
    traits = supplement(loader).dataset_traits()
    assert traits is not None
    return traits


def xero_uploads(stage: DemoStage | None = None, loader: XeroDatasetLoader | None = None) -> list[DemoUpload]:
    """演示导入清单的上传文件 (Supplements.uploads, 同 plan): stage 为空取全部 22 份, 否则只取该阶段的."""
    return [u for u in supplement(loader).uploads() if stage is None or u.stage == stage]


def xero_documents(stage: DemoStage, loader: XeroDatasetLoader | None = None) -> list[DocumentRef]:
    """到 stage 为止 (含) 各阶段上传并补登记后的文件登记, 按清单顺序: 渠道 auditor_upload, 上传人为项目负责人,
    类型, 可靠性, 文件日期与关联对象同清单, 哈希与页数取 manifest; 合同已抽取 (extracted), 其余 classified.
    份数: acceptance 2, fieldwork 21, manual 22."""
    done = STAGES[: STAGES.index(stage) + 1]
    entries = manifest_documents(loader)
    return [
        DocumentRef(
            id=u.document_id,
            type=u.document_type,
            file_name=u.file_name,
            sha256=entries[u.document_id]["sha256"],
            page_count=entries[u.document_id]["page_count"],
            reliability=u.reliability,
            status="extracted" if u.document_type in EXTRACTED else "classified",
            uploaded_at=NOW,
            channel="auditor_upload",
            uploaded_by=ENGAGEMENT.owner_id,
            document_date=u.document_date,
            related_ids=u.related_ids,
        )
        for u in xero_uploads(loader=loader)
        if u.stage in done
    ]


@functools.cache
def pdf_pages(path: Path, sha256: str) -> tuple[str, ...]:
    """PDF 文本层各页 (pdfplumber, 同 FileEvidenceStore.pages), 按哈希缓存."""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return tuple(page.extract_text() or "" for page in pdf.pages)


def xero_pages(document_id: str, loader: XeroDatasetLoader | None = None) -> list[str]:
    """manifest 登记的一份 PDF 的文本层; 不在 manifest 抛 LookupError."""
    entry = manifest_documents(loader).get(document_id)
    if entry is None:
        raise LookupError(f"document {document_id} 不在 Xero 补造目录的 manifest 中")
    return list(pdf_pages(supplement(loader).root / entry["path"], entry["sha256"]))


def xero_adjustments(
    entry_nos: Sequence[str] | None = None, loader: XeroDatasetLoader | None = None
) -> list[JournalEntry]:
    """补造的更正分录 (11 行), 按行 ID; entry_nos 给了时只取这些凭证."""
    rows = read_adjustments(supplement(loader).root / ADJUSTMENTS)
    return sorted((r for r in rows if entry_nos is None or r.entry_no in entry_nos), key=lambda r: r.id)


def xero_v01_rows(
    entry_nos: Sequence[str] | None = None, loader: XeroDatasetLoader | None = None
) -> list[dict[str, Any]]:
    """FIN-09 V01 的行 (需求 R165): 补造的更正分录 (xero_adjustments, entry_nos 给了时只取这些凭证) 接科目表的名称,
    类型与标准分类, 列同 FIN-09.yaml 的 SQL, 按凭证号与行 ID; 同载入之后连库跑 V01 (test_rules_fin09 断言 SQL)."""
    accounts = {a.code: a for a in xero_tables(loader)["accounts"]}
    rows = []
    for line in sorted(xero_adjustments(entry_nos, loader), key=lambda r: (r.entry_no, r.id)):
        account = accounts.get(line.account_code)
        rows.append(
            {
                "source_type": "journal_entry",
                "id": line.id,
                "entry_no": line.entry_no,
                "posting_date": line.posting_date,
                "account_code": line.account_code,
                "account_name": account.name if account else None,
                "account_type": account.type if account else None,
                "account_category": account.category if account else None,
                "debit_cents": line.debit_cents,
                "credit_cents": line.credit_cents,
                "description": line.description,
                "source_doc": line.source_doc,
            }
        )
    return rows


def with_adjustments(
    store: FakeFinancialStore, entry_nos: Sequence[str], loader: XeroDatasetLoader | None = None
) -> None:
    """假财务存储另记补造的更正分录 entry_nos 的各行 (经 record 与 journal_entries 取得到, 同载入之后)."""
    store.records |= {("journal_entry", r.id): r.model_dump() for r in xero_adjustments(entry_nos, loader)}


def xero_employees(loader: XeroDatasetLoader | None = None) -> list[Employee]:
    """补造的工资册 (3 行)."""
    return supplement(loader).employees()


def xero_registry(loader: XeroDatasetLoader | None = None) -> FakeRegistry:
    """登记 mock 的记录装进假登记系统: 补造目录 registry/ 的 ABN 与 ASIC 登记 (各 3 条), 公司摘录 (3 份) 与人员检索
    (1 份, 需求 R129); 摘录与检索经 MockRegistrySource 读出 (带 PDF 原件)."""
    root = supplement(loader).root / "registry"
    abn = json.loads((root / "abn.json").read_text(encoding="utf-8"))
    asic = json.loads((root / "asic.json").read_text(encoding="utf-8"))
    mock = MockRegistrySource(supplement(loader).root)
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


def xero_confirmations(loader: XeroDatasetLoader | None = None) -> FakeConfirmationChannel:
    """假函证平台: 两个银行账户 (12345678, 12345679) 回函, 内容为补造的回函 PDF, 渠道取补造目录的
    confirmations/receipts.json (同回函 mock). 假平台只有一个渠道, 各账户的渠道不同时抛 ValueError."""
    root = supplement(loader).root
    replies = [Path(d["path"]) for d in manifest_documents(loader).values() if d["registration"] == "confirmation"]
    receipts = json.loads((root / "confirmations" / "receipts.json").read_text(encoding="utf-8"))
    channels = {receipts[p.stem]["receipt_channel"] for p in replies}
    if len(channels) != 1:
        raise ValueError(f"回函渠道不止一种: {sorted(channels)}")
    return FakeConfirmationChannel(
        ReceiptChannel(channels.pop()), bodies={p.stem: (root / p).read_bytes() for p in sorted(replies)}
    )


def xero_contracts(loader: XeroDatasetLoader | None = None) -> list[Contract]:
    """3 份收入合同第 1 版: 按 DOC-04 的 fixture (tests/fixtures/llm_xero/DOC-04) 组装, 同上传后 A3 的抽取 (指针的证据
    种类与可靠性按文件登记改写, 同输出校验的 document_pointer); 执行 ID 为 UPLOAD-{文件 ID}-JOB. 按合同编号."""
    contracts = []
    for d in sorted(
        (d for d in xero_documents("fieldwork", loader) if d.type is DocumentType.contract), key=contract_id
    ):
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


def xero_tables(loader: XeroDatasetLoader | None = None) -> dict[str, list[Any]]:
    """演示数据导入之后的八张表 (XeroDatasetLoader.tables, 含应付账单)."""
    if loader is not None:
        return loader.tables(XERO_DATASET)
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


def xero_financial_store(
    stage: DemoStage = "fieldwork", contracts: bool = True, loader: XeroDatasetLoader | None = None
) -> FakeFinancialStore:
    """装好演示数据的假财务存储: 八张表 (put_rows) 与工资册, 到 stage 为止上传的文件; contracts 为真时预置
    xero_contracts() (合同在 fieldwork 上传, stage 为 acceptance 时抛 ValueError)."""
    if contracts and stage == "acceptance":
        raise ValueError("合同在 fieldwork 阶段才上传, acceptance 阶段不能预置合同")
    store = FakeFinancialStore(
        documents=xero_documents(stage, loader), contracts=xero_contracts(loader) if contracts else None
    )
    for name, rows in xero_tables(loader).items():
        put_rows(store, TableKind(name), rows)
    put_rows(store, TableKind.employees, xero_employees(loader))
    return store


def xero_engagement(loader: XeroDatasetLoader | None = None) -> Engagement:
    """以补造画像为客户的项目: 编号与负责人同 ENGAGEMENT, 期间取快照 (2026-07-01 至 2026-09-30)."""
    period = (loader if loader is not None else xero_loader()).period(XERO_DATASET)
    assert period is not None
    profile = xero_profile(loader)
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

    def __init__(self, financial_store: FakeFinancialStore, loader: XeroDatasetLoader | None = None) -> None:
        traits = {XERO_DATASET: xero_traits(loader)}
        super().__init__(financial_store, adjustments=xero_adjustments(loader=loader), traits=traits)
        self.loader = loader
        self.span = (loader if loader is not None else xero_loader()).period(XERO_DATASET)

    def seed(self) -> "XeroFakeDatasetLoader":
        return self  # 文件由演示数据导入上传, 不由载入登记

    def planning_presets(self, dataset: str) -> tuple[list[AuditObjective], list[RiskItem]]:
        return xero_presets(self.loader)

    def client_profile(self, dataset: str) -> ClientProfile:
        return xero_profile(self.loader)

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

    def __init__(
        self,
        financial_store: FakeFinancialStore,
        output_store: FakeOutputStore | None = None,
        loader: XeroDatasetLoader | None = None,
    ) -> None:
        super().__init__(financial_store, output_store)
        self.loader = loader
        self.page_counts.update({Path(d["path"]).name: d["page_count"] for d in manifest_documents(loader).values()})

    async def pages(self, eid: str, document_id: str) -> list[str]:
        registered = any(d.id == document_id for d in self.financial_store.document_refs)
        if registered and document_id in manifest_documents(self.loader):
            return xero_pages(document_id, self.loader)
        return await super().pages(eid, document_id)


def make_xero_deps(
    registry: FakeRegistry | None = None,
    trail: FakeTrail | None = None,
    engagement_store: FakeEngagementStore | None = None,
    output_store: FakeOutputStore | None = None,
    financial_store: FakeFinancialStore | None = None,
    loader: XeroDatasetLoader | None = None,
    **kwargs: Any,
) -> AgentDeps:
    """同 make_deps (其余参数原样转交), 缺省换成 Xero 演示的假实现: 登记 xero_registry(), 项目存储只有 Xero 画像与
    xero_engagement(), 财务存储 xero_financial_store(), 函证 xero_confirmations(), 数据管理 XeroFakeDataStore;
    证据文件存储与数据集载入换成 XeroFakeEvidenceStore 与 XeroFakeDatasetLoader. 规则引擎缺省没有预置结果 (各程序返回
    空结果, 不沿用 make_deps 缺省的 Numbat 例外), 要例外的测试自己给 rules. 给了 loader 时以上数据都取它指向的目录."""
    financial = financial_store if financial_store is not None else xero_financial_store(loader=loader)
    outputs = output_store if output_store is not None else FakeOutputStore()
    kwargs.setdefault("confirmations", xero_confirmations(loader))
    kwargs.setdefault("rules", FakeRuleEngine({}))
    kwargs.setdefault("data", XeroFakeDataStore(financial, xero_tables(loader)))
    deps = make_deps(
        registry if registry is not None else xero_registry(loader),
        trail if trail is not None else FakeTrail(),
        engagement_store
        if engagement_store is not None
        else FakeEngagementStore([xero_profile(loader)], [xero_engagement(loader)]),
        outputs,
        financial,
        **kwargs,
    )
    return replace(
        deps,
        evidence_store=XeroFakeEvidenceStore(financial, outputs, loader),
        datasets=XeroFakeDatasetLoader(financial, loader),
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


def xero_csh02_rows(loader: XeroDatasetLoader | None = None) -> dict[str, list[dict[str, Any]]]:
    """CSH-02 D02, D03, D04 的行: 由演示数据导入之后的表 (xero_tables) 按规则的口径派生, 同 Numbat
    (tests.fakes 的 csh02_subsequent_rows, csh02_window_rows); 期末取快照, 作废对按数据集声明的格式与模板剔除.
    读快照, 规则引擎取 CSH-02 时才算, 每次新建. 结果: D02 为 BA-090 期后 21 行 (BA-091 期后没有流水), D03 中
    BA-091 只剩 XJ-601-1, D04 里没有 4853 (BA-091 回函的应计利息) 的行. 给了 loader 时取它指向的副本 (注入场景)."""
    tables = xero_tables(loader)
    traits = xero_traits(loader)
    period = (loader if loader is not None else xero_loader()).period(XERO_DATASET)
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


# GEN-05 R10 两笔贷项凭证配到的同额发票 (07-17 的 550.00 五张, ORC1003, 1008, 1009, 1010, 1011), 按 (开票日, ID)
XERO_SAME_AMOUNT_INVOICES = [
    "SI-1d1ba340-afa2-4f4c-8ff7-a147bda9a47b",  # ORC1008
    "SI-33e4123e-7cdd-4f05-9a0a-eb8adeb2b868",  # ORC1011
    "SI-4fad1af2-b871-4ac5-a15a-3c5e32d2e2c4",  # ORC1003
    "SI-5613938b-9e27-472e-92ae-3b038b669d10",  # ORC1010
    "SI-76e3f056-479f-417c-a72b-f3d767899b87",  # ORC1009
]


def xero_gen05_exceptions(run: ProcedureRun) -> list[AuditException]:
    """GEN-05 新规则对 Xero 演示的 7 条例外 (需求 R132 验收 3-6, 设计稿第 5.3 节): R09 转换余额与手工折旧; R10 两笔
    全额冲减 550.00 的贷项凭证 (CR1003, CR1005; 07-31 的 XJ-449 另配上同日的 ORC1005); R11 作废后重记而对方名称改了的
    两笔; R12 ORC1003 与 ORC1008 同为 Hamilton Smith 07-17 的 550.00, ORC1003 没有服务完成记录, 冲减它的是 XJ-430."""

    def entry(no: str, day: str) -> dict[str, Any]:
        return {"entry_no": no, "posting_date": day}

    return [
        gen05_exception(
            run,
            "R09",
            "high",
            "XJ-397-3",
            1921221,
            entry("XJ-397", "2026-07-16") | {"description": "Conversion Balance", "source_doc": "Conversion Balance"},
        ),
        gen05_exception(
            run,
            "R09",
            "high",
            "XJ-499-2",
            82500,
            entry("XJ-499", "2026-08-16")
            | {
                "description": "Office equipment depreciation",
                "source_doc": "Being depreciation on office equipment - Manual",
            },
            number=2,
        ),
        gen05_exception(
            run,
            "R10",
            "medium",
            "XJ-430-2",
            55000,
            entry("XJ-430", "2026-07-26") | {"credited_cents": 55000, "matched_invoices": XERO_SAME_AMOUNT_INVOICES},
        ),
        gen05_exception(
            run,
            "R10",
            "medium",
            "XJ-449-2",
            55000,
            entry("XJ-449", "2026-07-31")
            | {
                "credited_cents": 55000,
                "matched_invoices": [*XERO_SAME_AMOUNT_INVOICES, "SI-b75b3928-ab72-4424-8b93-9cdbbde4cd72"],  # ORC1005
            },
            number=2,
        ),
        gen05_exception(
            run,
            "R11",
            "medium",
            "XJ-575-1",
            29695,
            entry("XJ-575", "2026-09-16")
            | {
                "original_entry_no": "XJ-560",
                "reversal_entry_no": "XJ-567",
                "original_name": "Payment to Odette Grainger",
                "repost_name": "Payment to Odette Garrison",
            },
        ),
        gen05_exception(
            run,
            "R11",
            "medium",
            "XJ-601-1",
            350000,
            entry("XJ-601", "2026-09-26")
            | {
                "original_entry_no": "XJ-599",
                "reversal_entry_no": "XJ-602",
                "original_name": "Bank Transfer from Ridgeway Savings to NAB Business Account",
                "repost_name": "Bank Transfer from Business Savings Account to Business Bank Account",
            },
            number=2,
        ),
        gen05_exception(
            run,
            "R12",
            "high",
            "XJ-405-2",
            55000,
            entry("XJ-405", "2026-07-17")
            | {
                "invoice_id": "SI-4fad1af2-b871-4ac5-a15a-3c5e32d2e2c4",
                "invoice_no": "ORC1003",
                "customer": "Hamilton Smith Pty",
                "paired_invoices": [
                    {
                        "invoice_id": "SI-1d1ba340-afa2-4f4c-8ff7-a147bda9a47b",
                        "invoice_no": "ORC1008",
                        "journal_entry_id": "XJ-408-2",
                    }
                ],
                "full_credits": ["XJ-430"],
            },
        ),
    ]


def xero_gen05_result(run: ProcedureRun) -> RuleResult:
    """GEN-05 对 Xero 演示的结果 (需求 R131 验收 5, 7 与 R132 验收 7, 取自连库跑一次的结果): 新规则 R09-R12 的 7 条
    例外 (xero_gen05_exceptions), 原有规则 0 条; 录入时间不可靠, 看录入时间的 R01, R06, R08, R13 不执行 (理由为数据集
    声明的说明); 分录都没有审批人, R04 不可执行; 录入人只有一个, R07 不可执行. R02, R03 (审批限额 500000), R05 执行
    而没有命中."""
    note = xero_traits().entry_timestamps_note or ""
    return RuleResult(
        exceptions=xero_gen05_exceptions(run),
        rows={},
        outcomes=[
            RuleOutcome(rule_id="R01", status="skipped", reason=note),
            RuleOutcome(rule_id="R04", status="not_executable", reason="本期分录都没有审批人 (approved_by 全为空)"),
            RuleOutcome(rule_id="R06", status="skipped", reason=note),
            RuleOutcome(rule_id="R07", status="not_executable", reason="本期分录的录入人少于 2 个, 无从比较"),
            RuleOutcome(rule_id="R08", status="skipped", reason=note),
            RuleOutcome(rule_id="R13", status="skipped", reason=note),
        ],
    )


# GEN-03 C01 对 Xero 演示的命中 (需求 R139, 取自连库跑一次 SqlRuleEngine 的结果, 期间同上, 期后 30 天): 名称键 →
# (凭证号, 银行流水 ID); 没有发票命中. VPA 的作废对 XJ-415/417 已剔除. SMART Agency 只在 T6 同构用例的关系图里
# (tests/test_fieldwork_gen03.py), 缺省关系图的 Morgan Advisory 没有命中. C02 没有行 (窗口内没有权益科目的分录).
XERO_GEN03_HITS = {
    "smart agency": (("XJ-548", "XJ-641"), ("BT-090-0097",)),
    "victoria property agency": (("XJ-420", "XJ-518", "XJ-593"), ("BT-090-0004", "BT-090-0033", "BT-090-0058")),
}
GEN03_ENTRY_COLUMNS = {  # C01 分录行 record 的列 (另加科目名称与分类), 同 C02 的列
    "id",
    "entry_no",
    "posting_date",
    "account_code",
    "debit_cents",
    "credit_cents",
    "description",
    "source_doc",
}
GEN03_BANK_COLUMNS = {"id", "bank_account_id", "txn_date", "amount_cents", "description", "reference"}


def xero_gen03_rows(keys: Sequence[str], loader: XeroDatasetLoader | None = None) -> list[dict[str, Any]]:
    """GEN-03 C01 对这些名称键的行, 列与顺序同 GEN-03.yaml 的 SQL ({source_type, party_key, record}, 按名称键, 来源,
    日期与 ID): 由演示数据导入之后的表 (xero_tables) 按 XERO_GEN03_HITS 组装, 凭证取全部行, 科目名称与分类取科目表.
    读快照, 每次新建."""
    tables = xero_tables(loader)
    accounts = {a.code: a.model_dump(mode="json") for a in tables["accounts"]}
    rows: list[dict[str, Any]] = []
    for key in sorted(keys):
        entry_nos, bank_ids = XERO_GEN03_HITS[key]
        bank = sorted((b for b in tables["bank_transactions"] if b.id in bank_ids), key=lambda b: (b.txn_date, b.id))
        rows += [
            {
                "source_type": "bank_transaction",
                "party_key": key,
                "record": b.model_dump(mode="json", include=GEN03_BANK_COLUMNS),
            }
            for b in bank
        ]
        lines = sorted(
            (j for j in tables["journal_entries"] if j.entry_no in entry_nos), key=lambda j: (j.posting_date, j.id)
        )
        rows += [
            {
                "source_type": "journal_entry",
                "party_key": key,
                "record": {
                    **j.model_dump(mode="json", include=GEN03_ENTRY_COLUMNS),
                    "account_name": accounts[j.account_code]["name"],
                    "account_category": accounts[j.account_code]["category"],
                },
            }
            for j in lines
        ]
    return rows


DIISR_SERVICES = "DIISR - Small Business Services"  # XJ-720 (贷项凭证 1026C) 的摘要
GEN07_WINDOW_END = "2026-10-17"  # GEN-07 期后窗口的截止日: 期末加 31 天 (10-31) 截到数据截止日 (dataset_traits)
GEN07_POINTERS = {  # GEN-07 例外的行类型 → (证据种类, 可靠性), 同 SqlRuleEngine 按 judgment_engine.ROW_EVIDENCE
    "bank_transaction": ("bank", "external_copy"),
    "journal_entry": ("voucher", "internal_original"),
}


def gen07_exception(
    run: ProcedureRun,
    rule_id: str,
    number: int,
    source_type: str,
    source_id: str,
    amount_cents: int,
    detail: dict[str, Any],
) -> AuditException:
    """GEN-07 一条规则的第 number 条例外, 字段同 SqlRuleEngine 按 GEN-07.yaml 的输出列组装: detail 为 source_type,
    source_id, amount_cents 之外的列 (最后一列为 window_end), 证据指针指向该行, locator 为这些列名."""
    detail = detail | {"window_end": GEN07_WINDOW_END}
    kind, reliability = GEN07_POINTERS[source_type]
    return AuditException(
        id=f"X-GEN-07-{run.run_no}-{rule_id}-{number}",
        version=1,
        procedure_id="GEN-07",
        run_id=run.id,
        rule_id=rule_id,
        severity={"S01": "medium", "S02": "high", "S03": "medium"}[rule_id],
        source_ids=[source_id],
        amount_cents=amount_cents,
        detail=detail,
        evidence=[
            EvidencePointer.model_validate(
                {
                    "type": kind,
                    "source_type": source_type,
                    "source_id": source_id,
                    "locator": ", ".join(["amount_cents", *detail]),
                    "reliability": reliability,
                }
            )
        ],
    )


def xero_gen07_exceptions(run: ProcedureRun) -> list[AuditException]:
    """GEN-07 对 Xero 演示的 9 条例外 (需求 R177 验收 5, 设计稿第 5.4 节; 取自连库跑一次的结果, 窗口 10-01 至 10-17):
    S01 六笔期后大额流水在总账银行科目 ±10 天内没有同额行 (BT-090-0097 只有作废对 XJ-728/XJ-744 的同额行); S02 一笔
    非常规分录, 即 DIISR 的贷项凭证 1026C (借收入, 贷应收, 不是常规赊销); S03 两笔期后购入的办公家具与电脑."""

    def txn(account: str, day: int, payee: str, reference: str | None) -> dict[str, Any]:
        day_text = f"2026-10-{day:02d}"
        return {"bank_account_id": account, "txn_date": day_text, "description": payee, "reference": reference}

    def line(no: str, day: int, account: str, description: str, source_doc: str) -> dict[str, Any]:
        return {
            "entry_no": no,
            "posting_date": f"2026-10-{day:02d}",
            "account_code": account,
            "description": description,
            "source_doc": source_doc,
        }

    s01 = [
        ("BT-090-0092", 618750, txn("BA-090", 10, "Ridgeway University", "ORC01025")),
        ("BT-090-0097", -450000, txn("BA-090", 11, "SMART Agency", "70135 70209")),
        ("BT-090-0099", 200000, txn("BA-090", 11, "Jakaranda Maple Systems", "DEPOSIT ADV")),
        ("BT-090-0107", 100000, txn("BA-090", 12, "e-Bank dep", None)),
        ("BT-090-0130", 150000, txn("BA-090", 15, "e-Bank dep", None)),
        ("BT-090-0131", 45000, txn("BA-090", 15, "Majestic Contracting", "Sublet RM2031")),
    ]
    s03 = [
        ("XJ-626-2", 630000, line("XJ-626", 4, "710", "Desks, chairs, reception furniture for office", "MKT-08123")),
        ("XJ-676-2", 196999, line("XJ-676", 11, "720", "Laptop (Tracy)", "PC Complete")),
    ]
    return [
        *(gen07_exception(run, "S01", n, "bank_transaction", *row) for n, row in enumerate(s01, 1)),
        gen07_exception(
            run, "S02", 1, "journal_entry", "XJ-720-2", 115500, line("XJ-720", 16, "610", DIISR_SERVICES, "1026C")
        ),
        *(gen07_exception(run, "S03", n, "journal_entry", *row) for n, row in enumerate(s03, 1)),
    ]


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
    "GEN-05": xero_gen05_result,  # 功能 journals 打开时才执行
    # 功能 related_parties 打开时才执行; 缺省关系图 (GEN-08 的 VPA 与 Morgan Advisory) 只有 VPA 有往来
    "GEN-03": lambda run: RuleResult(
        exceptions=[], rows={"C01": xero_gen03_rows(["victoria property agency"]), "C02": []}
    ),
    "GEN-07": lambda run: RuleResult(exceptions=xero_gen07_exceptions(run), rows={}),  # 功能 going_concern 打开时才执行
}


# ── 只开现有 17 条的全流程 (需求 R119; 连库变体 R120 共用): 空项目的启动与确认数据已齐之后的收尾 ──
# 启动到确认数据已齐见 tests/xero_flow (to_ready). 决定按设计稿第 7.3 节末段 "只开现有 17 条时"; 决定人 actor 缺省为
# ENGAGEMENT 的项目负责人, 连库时传新建的用户.

XERO17_FEATURES = BASELINE  # 新功能全关 (同 Numbat 回归, 只留基线的 critic), 测试显式传给依赖
XERO17_MATERIALITY = MaterialityChoice(basis="revenue", pct=0.01, performance_pct=0.5, clearly_trivial_pct=0.05)
# 审计师对 BA-091 判断认定错报所附草稿: 未计提的应计利息 48.53, 科目写到代码
XERO17_BA091_DRAFT = MisstatementDraft(
    type="factual",
    account="Interest income (270)",
    assertion="Completeness",
    amount_cents=4853,
    direction="understated",
)
# RPT-01 第一次退回的批注 (需求 R119 来由: 本路径没有持续经营段, 挂在形成意见的基础段)
XERO17_ANNOTATION = Annotation(
    section=ReportSection.basis_for_opinion,
    comment=(
        "Name the financial reporting framework (Australian Accounting Standards - Simplified Disclosures) "
        "in this paragraph."
    ),
)
XERO17_CONTRACTS = ["CON-X01", "CON-X02", "CON-X03"]  # REV-03 的判断对象, 按编号
# 停在 PLN-15 闸门时审计计划页的程序表 (需求 R314 验收 2): 按审计目标在状态中的顺序, 组内按程序编号; 都是全量
XERO_PLAN_ROWS = [
    ("OBJ-REV-OCC", "REV-03"),
    ("OBJ-REV-OCC", "REV-08"),
    ("OBJ-REV-CUT", "REV-02"),
    ("OBJ-REV-CUT", "REV-03"),
    ("OBJ-REV-CUT", "REV-08"),
    ("OBJ-CASH-EXI", "CSH-02"),
    ("OBJ-CASH-EXI", "CSH-04"),
    ("OBJ-CASH-EXI", "CSH-05"),
    (None, "DOC-06"),
]
XERO17_INVOICES = [str(row["id"]) for row in XERO_S01_ROWS]  # REV-02 的判断对象 (S01 六张), 按发票 ID

# ── 打开功能 journals 的增量 (需求 R146; 设计稿第 7.2 节第 12, 13 步): GEN-05 七条例外, GEN-02 七条判断与其决定 ──

XERO_JOURNALS_FEATURES = BASELINE | {"journals"}  # 基线加分录测试 (GEN-05, GEN-02)
XERO_JOURNAL_VOUCHERS = ["XJ-397", "XJ-405", "XJ-430", "XJ-449", "XJ-499", "XJ-575", "XJ-601"]  # GEN-02 的判断对象
# 审计师对 XJ-499 判断认定错报所附草稿: 取判断的草稿 (设计稿第 6.3 节; 科目代码由 R156 回填)
XERO_XJ499_DRAFT = MisstatementDraft(
    type="factual",
    account="Office equipment (710)",
    assertion="Existence",
    amount_cents=75000,
    direction="overstated",
)
# 审计师调阅报销单 E003 后推翻 XJ-575 的判断 (设计稿第 4 节第 17 行, 第 7.2 节第 13 步): 录入时把姓写错, 改为接受
XERO_XJ575_REASON = (
    "Expense claim E003 was inspected: it is Odette Garrison's approved claim for AUD 296.95, and the surname on "
    "XJ-560 was entered wrongly."
)
XERO_XJ575_CONCLUSION = (
    "XJ-575 pays Odette Garrison's approved expense claim E003 of AUD 296.95; the payee name on the voided entry "
    "XJ-560 was a data entry error, so the repost is supported and no misstatement arises."
)


def xero_journal_reviews(actor: str = ENGAGEMENT.owner_id) -> list[ReviewRecord]:
    """GEN-02 七条判断的复核决定, 按凭证号: XJ-499 认定错报 (草稿 XERO_XJ499_DRAFT), XJ-575 推翻为接受 (理由与新结论
    见上), 其余 5 条 confirm. 都绑定判断第 1 版. 七条任务与 REV-03, CSH-02 的同在第一轮出现, 排在 xero17_reviews 之后
    提交时仍待决."""

    def record(voucher: str, decision: Decision = Decision.confirm, **fields: Any) -> ReviewRecord:
        return ReviewRecord(
            id=f"D-GEN02-{voucher}",
            task_id=f"T-J-GEN-02-1-{voucher}-v1",
            target_version=1,
            decision=decision,
            reviewed_by=actor,
            decided_at=NOW,
            **fields,
        )

    special = {
        "XJ-499": record(
            "XJ-499",
            Decision.misstatement,
            reason="The fixed asset register shows no office equipment, so the AUD 750.00 asset has no basis.",
            misstatement_draft=XERO_XJ499_DRAFT,
        ),
        "XJ-575": record(
            "XJ-575", Decision.override, reason=XERO_XJ575_REASON, override_conclusion=XERO_XJ575_CONCLUSION
        ),
    }
    return [special.get(voucher) or record(voucher) for voucher in XERO_JOURNAL_VOUCHERS]


def new_xero_deps(**kwargs: Any) -> AgentDeps:
    """还没有数据的 Xero 演示项目 (演示数据导入从空写起, 需求 R269): 空的假财务存储, 数据管理存储未就绪 (数据版本 0,
    各表为空), 其余同 make_xero_deps (kwargs 原样转交, 如 llm, trail, rules, features)."""
    financial = FakeFinancialStore()
    empty = {kind.value: [] for kind in (*XERO_TABLES, TableKind.delivery_notes)}
    return make_xero_deps(financial_store=financial, data=XeroFakeDataStore(financial, empty, ready=False), **kwargs)


def xero17_reviews(actor: str = ENGAGEMENT.owner_id) -> list[ReviewRecord]:
    """10 条复核决定, 按提交顺序 (各层的任务依次出现): REV-03 三条 confirm, REV-02 六条 confirm, BA-091 认定错报
    (草稿 XERO17_BA091_DRAFT). 都绑定判断第 1 版."""

    def record(task_id: str, rid: str, decision: Decision = Decision.confirm, **fields: Any) -> ReviewRecord:
        return ReviewRecord(
            id=rid, task_id=task_id, target_version=1, decision=decision, reviewed_by=actor, decided_at=NOW, **fields
        )

    return [
        *(record(f"T-J-REV-03-1-{c}-v1", f"D-REV03-{n}") for n, c in enumerate(XERO17_CONTRACTS, 1)),
        *(record(f"T-J-REV-02-1-{i}-v1", f"D-REV02-{n}") for n, i in enumerate(XERO17_INVOICES, 1)),
        record(
            "T-J-CSH-02-1-BA-091-v1",
            "D-CSH02",
            Decision.misstatement,
            reason="Accrued interest of AUD 48.53 confirmed by the bank was not recorded.",
            misstatement_draft=XERO17_BA091_DRAFT,
        ),
    ]


def xero17_gates(actor: str = ENGAGEMENT.owner_id) -> dict[str, GateDecision]:
    """闸门决定 (ACC-01 由 xero_flow.approve_acceptance 提交): pln approve 收入 1%, 50%, 5%; fin approve 无保留意见;
    rpt_reject 退回 REPORT-1 (批注 XERO17_ANNOTATION); rpt_approve 批准 REPORT-2."""
    return {
        "pln": GateDecision(
            id="D-PLN",
            gate=GateId.PLN_01_03,
            target_id="MATERIALITY",
            target_version=1,
            decision=GateVerdict.approve,
            materiality=XERO17_MATERIALITY,
            decided_by=actor,
            decided_at=NOW,
        ),
        "fin": GateDecision(
            id="D-FIN",
            gate=GateId.FIN_01,
            target_id="SCHEDULE",
            target_version=1,
            decision=GateVerdict.approve,
            disposition=OpinionType.unqualified,
            reason="The only uncorrected misstatement, AUD 48.53, is below overall materiality of AUD 324.31.",
            decided_by=actor,
            decided_at=NOW,
        ),
        "rpt_reject": GateDecision(
            id="D-RPT-REJECT",
            gate=GateId.RPT_01,
            target_id="REPORT-1",
            target_version=1,
            decision=GateVerdict.reject,
            annotations=[XERO17_ANNOTATION],
            reason="形成意见的基础段须写明编制框架",
            decided_by=actor,
            decided_at=NOW,
        ),
        "rpt_approve": GateDecision(
            id="D-RPT-APPROVE",
            gate=GateId.RPT_01,
            target_id="REPORT-2",
            target_version=1,
            decision=GateVerdict.approve,
            decided_by=actor,
            decided_at=NOW,
        ),
    }


async def finish_xero17(
    runner: GraphRunner,
    eid: str,
    actor: str = ENGAGEMENT.owner_id,
    before_review: Callable[[GraphRunner, str, ReviewRecord], Awaitable[None]] | None = None,
    reviews: Sequence[ReviewRecord] | None = None,
    stop_at_fin: bool = False,
) -> None:
    """确认数据已齐之后 (停在 PLN-01/03 闸门) 推到签字归档: PLN-01/03 approve, 复核 (缺省为 xero17_reviews 的 10 条;
    每次提交前须停在 await_events, 没有后台错误), FIN-01 approve 无保留意见, RPT-01 先退回再批准. 每步等后台停下.
    before_review 在提交每条复核之前以 (runner, eid, 决定) 调 (如先呈递它的任务, 需求 R155); stop_at_fin 为真时停在
    FIN-01 闸门, 不作 FIN-01 与 RPT-01 的决定 (需求 R146: 意见与报告不在本组)."""
    submit = GraphDecisionSubmit(runner.app, runner.deps, runner)
    gates = xero17_gates(actor)

    async def at() -> tuple[str, ...]:
        return (await runner.app.aget_state(GraphRunner.config(eid))).next

    async def send(decision: ReviewRecord | GateDecision) -> None:
        await submit.submit(eid, decision)
        await wait_idle(runner, eid)
        assert runner.run_error(eid) is None, (decision.id, runner.run_error(eid))

    assert await at() == ("gate_p1_risk",), runner.run_error(eid)
    await send(gates["pln"])
    for review in xero17_reviews(actor) if reviews is None else reviews:
        assert await at() == ("await_events",), (review.task_id, runner.run_error(eid))
        if before_review is not None:
            await before_review(runner, eid, review)
        await send(review)
    assert await at() == ("gate_fin",), runner.run_error(eid)
    if stop_at_fin:
        return
    await send(gates["fin"])
    assert await at() == ("gate_rpt01",)
    await send(gates["rpt_reject"])
    assert await at() == ("gate_rpt01",)
    await send(gates["rpt_approve"])
    assert await at() == ()


# ── 错报闭环 (需求 R158): 已认定而未分类的两条错报与其来源判断, FIN-03 的判断对象 ──


def fixture_judgment(procedure_id: str, subject_id: str, autonomy_level: AutonomyLevel) -> Judgment:
    """某程序第 1 次执行对 subject_id 的判断, 内容取该程序 Xero fixture 正本的草稿 (结论, 推理链, 证据, 建议动作与
    错报草稿), 已由审计师确认 (第 2 版 confirmed). 置信度给定值, 不重算."""
    raw = (XERO.fixtures / procedure_id / f"{subject_id}.json").read_text(encoding="utf-8")
    draft = JudgmentDraft.model_validate_json(raw)
    return Judgment(
        id=f"J-{procedure_id}-1-{subject_id}",
        version=2,
        procedure_id=procedure_id,
        run_id=f"RUN-{procedure_id}-1",
        subject_id=subject_id,
        conclusion=draft.conclusion,
        confidence=0.85,
        confidence_parts={"evidence": 0.7, "rule": 1.0, "llm": draft.self_confidence},
        reasoning_chain=draft.reasoning_chain,
        evidence=draft.evidence,
        counter_argument=draft.counter_argument,
        proposed_action=draft.proposed_action,
        misstatement_draft=draft.misstatement_draft,
        autonomy_level=autonomy_level,
        review_status=ReviewStatus.confirmed,
        performed_by=ENGAGEMENT.owner_id,
        created_at=NOW,
    )


def xero_identified_misstatements() -> dict[str, list[Any]]:
    """FIN-03 前的状态片段 (需求 R158 验收 1): M-D-XJ499 v1 (来源 GEN-02 对 XJ-499 的判断, 科目代码 710) 与
    M-D-CSH02-091 v1 (来源 CSH-02 对 BA-091 的判断, 草稿 XERO17_BA091_DRAFT 加科目代码 270), 都是 identified 且
    未分类, 按认定的先后; 来源判断为第 2 版 confirmed; 另有 REV-03 对 CON-X01 的判断 (别的程序的, FIN-03 不给模型)."""
    sources = [
        fixture_judgment("GEN-02", "XJ-499", AutonomyLevel.L2),
        fixture_judgment("CSH-02", "BA-091", AutonomyLevel.L3),
    ]
    drafts = [
        XERO_XJ499_DRAFT.model_copy(update={"account_code": "710"}),
        XERO17_BA091_DRAFT.model_copy(update={"account_code": "270"}),
    ]
    misstatements = [
        Misstatement(
            id=f"M-D-{record}",
            judgment_id=source.id,
            exception_id=None,
            review_record_id=f"D-{record}",
            **draft.model_dump(),
        )
        for record, source, draft in zip(("XJ499", "CSH02-091"), sources, drafts, strict=True)
    ]
    return {
        "judgments": [*sources, fixture_judgment("REV-03", "CON-X01", AutonomyLevel.L2)],
        "misstatements": misstatements,
    }


# 第 2 轮的三条薪酬错报 (FIN-15 第 1 次对科目的判断认定, Xero演示数据集设计第 2.8 节): (科目代码, 决定 ID 后缀, 科目,
# 金额), 都是 Completeness 低估
XERO_ROUND2 = [
    ("826", "F03", "Superannuation payable (826)", 426120),
    ("804", "F04", "Wages payable (804)", 201000),
    ("805", "F05", "Annual leave provision (805)", 268736),
]


def round_file(n: int) -> dict[str, Any]:
    """补造的 FIN-08 第 n 轮沟通记录 (中性格式, manual/fin08_round{n}.json)."""
    path = supplements_dir(SNAPSHOT_NAME) / "manual" / f"fin08_round{n}.json"
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def registration(
    n: int, misstatements: Sequence[Misstatement], agrees: Mapping[str, bool] | None = None
) -> list[dict[str, Any]]:
    """第 n 轮记录映射成 FIN-08 结果模型 ManagementResponses 的行 (JSON 形式, 同前端提交): misstatement_sources 为
    错报的来源判断, 每条一行, 依据的版本为给定错报的版本; 沟通对象取 recipient; agrees 缺省都同意 (补造的两轮都同意)."""
    data = round_file(n)
    by_source = {m.judgment_id: m for m in misstatements}
    return [
        {
            "misstatement_id": by_source[source].id,
            "base_version": by_source[source].version,
            "communicated_at": data["communicated_at"],
            "communicated_to": data["recipient"],
            "responded_at": data["responded_at"],
            "response": data["response"],
            "agrees_to_correct": (agrees or {}).get(by_source[source].id, True),
        }
        for source in data["misstatement_sources"]
    ]
