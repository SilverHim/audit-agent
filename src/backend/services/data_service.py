"""数据管理的服务 (需求 R10-R16): 数据管理接口的写操作都经这里, 取项目锁, 写存储, 记 data 留痕.

项目锁与编排共用 (GraphRunner.acquire): 同一项目同时只有一个写者, 图在后台推进时写数据报 ENGAGEMENT_BUSY.
数据写操作 (holding, launch) 结束时图若停在执行阶段等程序要的数据 (await_data), 锁交给后台恢复, 由节点重查 (需求 R272).
"新数据版本写入" 的入口是 DataService.write_tables (导入与 Xero 同步共用), 须在 holding 块内或 launch 的作业里调用.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any
from uuid import uuid4

from langgraph.types import Command
from pydantic import BaseModel, ValidationError

from backend.adapters.registry import latest_extractions
from backend.adapters.xero_supplements import STAGE_REQUESTS, DemoPlan, DemoSource, plan
from backend.agents.base import AgentDeps
from backend.agents.checkup import DataCheckup, checkup_entries, request_entry
from backend.agents.ingest import (
    DETAIL_FIELDS,
    DETAILS,
    HELD,
    DocumentIntake,
    Job,
    Requested,
    details_data,
    details_entry,
    next_statements_version,
    record_registration,
    registered_reliability,
    retype,
    unfinished,
)
from backend.contracts.data import (
    DEMO_IMPORT_STAGES,
    FS_TARGET,
    REGISTRY_TARGET,
    BatchFile,
    BatchItem,
    BatchItemEdit,
    CategoryVersion,
    ContractVersion,
    DataInput,
    DataReadiness,
    DataRequest,
    DataSource,
    DataSources,
    DataSummary,
    DemoImportResult,
    DemoStage,
    DemoSummary,
    DocumentChange,
    DocumentDetailsEdit,
    ExtractionEdit,
    ExtractionRecord,
    ImportBatch,
    ImportCommit,
    ImportPreview,
    ImportResult,
    ImportSession,
    RecordsDetail,
    TableColumn,
    TableKind,
    TableOrigin,
    TablePage,
    TableSchema,
    TableSummary,
    TableVersion,
    XeroSource,
)
from backend.contracts.errors import DataRejected
from backend.contracts.llm_outputs import RegistryExtraction
from backend.contracts.models import (
    AgentId,
    Confirmation,
    Contract,
    DocumentRef,
    DocumentType,
    Engagement,
    FinancialStatements,
    GateId,
    Reliability,
    TrailEntry,
    TrailKind,
)
from backend.contracts.ports import XeroConnections
from backend.contracts.records import Account, AccountCategory
from backend.contracts.state import fill_defaults
from backend.orchestration.gates import current_gate
from backend.orchestration.nodes import DATA_READY, INPUTS_CHANGED
from backend.orchestration.runner import GraphRunner, awaiting_data, awaiting_inputs, review_waits
from backend.orchestration.trail import data_ready_entry, record_transitions
from backend.services.account_mapping import CATEGORY_GROUPS, with_category
from backend.services.data_import import (
    MAX_FAILURES,
    SCHEMAS,
    MappingError,
    UnreadableFile,
    media_type,
    preview,
    raw_rows,
    read_sheet,
    sheet_names,
)
from backend.services.data_quality import REQUIRED_TABLES, missing_tables
from backend.services.demo_import import DEMO_JOB, NO_DEMO, DemoImport, stage_statuses
from backend.services.extraction_edits import (
    apply_confirmation_edit,
    apply_contract_edit,
    apply_registry_edit,
    apply_statements_edit,
    ordered_terms,
)
from backend.services.import_batches import (
    TABLE_EXTENSIONS,
    BatchIntake,
    carried,
    chosen_of,
    finalize,
    item_number,
    masked_failures,
    staged_of,
    unreadable_item,
)
from backend.services.table_plan import apply_plan, convert
from backend.services.trail_pages import all_of_kind

logger = logging.getLogger(__name__)
# 留痕摘要里抽取结果的名称
TARGET_NAMES = {
    DocumentType.contract: "合同",
    DocumentType.confirmation: "回函",
    DocumentType.registry_extract: "ASIC 摘录",
    DocumentType.financial_statements: "待审财报",
}
EDITED_TYPES = {  # 抽取版本表的 target_type → 文件类型; 其余为函证
    "contract": DocumentType.contract,
    REGISTRY_TARGET: DocumentType.registry_extract,
    FS_TARGET: DocumentType.financial_statements,
}
# 可上传的文件 (POST /documents), 单个不超过 data_import.MAX_BYTES
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".png", ".jpg", ".jpeg"}
TABLE_NAMES: dict[TableKind, str] = {  # 留痕摘要里的表名
    TableKind.accounts: "科目表",
    TableKind.trial_balance: "试算表",
    TableKind.journal_entries: "总账分录",
    TableKind.sales_invoices: "销售发票",
    TableKind.supplier_bills: "应付账单",
    TableKind.bank_accounts: "银行账户",
    TableKind.bank_transactions: "银行流水",
    TableKind.delivery_notes: "发运单",
    TableKind.approval_log: "审批日志",
    TableKind.employees: "工资册",
}


def version_entry(eid: str, version: TableVersion) -> TrailEntry:
    """一个账表版本的 data 留痕: action 为 import (来源为导入) 或 sync, payload 带 table_kind, records 与有值的
    file_name, document_id, request_id; 防重复键 {eid}:data:v{数据版本}:{表}, 写入与补写共用."""
    action = "import" if version.source == "import" else "sync"
    payload: dict[str, object] = {"action": action, "table_kind": version.kind.value, "records": version.records}
    extra = {"file_name": version.file_name, "document_id": version.document_id, "request_id": version.request_id}
    payload |= {k: v for k, v in extra.items() if v is not None}
    verb = "导入" if action == "import" else "同步"
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.data,
        actor=version.created_by,
        summary=f"{verb}{TABLE_NAMES[version.kind]} {version.records} 行, 数据版本 {version.data_version}",
        idempotency_key=f"{eid}:data:v{version.data_version}:{version.kind}",
        payload=payload,
        created_at=datetime.now(UTC),
    )


def edit_entry(eid: str, record: ExtractionRecord) -> TrailEntry:
    """一次抽取结果修改或文件补登记的 data 留痕: action edit, payload 带 document_type, field, reason 与文件 ID (合同与
    待审财报取 document_id, 函证取 reply_document_id, ASIC 摘录与补登记即该文件, 补登记的类型取该版登记的); 防重复键
    {eid}:data:edit:{类型}:{对象}:v{版本}, 写入与补写共用."""
    if record.target_type == DETAILS:
        return details_entry(eid, record)
    kind = EDITED_TYPES.get(record.target_type, DocumentType.confirmation)
    payload: dict[str, object] = {
        "action": "edit",
        "document_type": kind.value,
        "field": record.field,
        "reason": record.reason,
    }
    if kind is DocumentType.registry_extract:  # ASIC 摘录的抽取结果按文件记 (需求 R290)
        document_id: object = record.target_id
    else:
        document_id = record.data.get("reply_document_id" if kind is DocumentType.confirmation else "document_id")
    if document_id:
        payload["document_id"] = document_id
    summary = f"修改{TARGET_NAMES[kind]} {record.target_id} 的抽取结果 {record.field}, 第 {record.version} 版"
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.data,
        actor=record.changed_by,
        summary=summary,
        idempotency_key=f"{eid}:data:edit:{record.target_type}:{record.target_id}:v{record.version}",
        payload=payload,
        created_at=datetime.now(UTC),
    )


def upload_entry(
    eid: str, document: DocumentRef, request_id: str | None = None, actor: str | None = None
) -> TrailEntry:
    """一份上传文件的 data 留痕: action upload, payload 带 file_name, document_id (有 request_id 时带上), actor 为
    登记的上传人 (系统存档的文件取这次上传的 actor); 防重复键 {eid}:data:upload:{文件 ID}, 上传, 重传与启动补做
    共用 (同一文件只记一条)."""
    payload = {"action": "upload", "file_name": document.file_name, "document_id": document.id}
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.data,
        actor=document.uploaded_by or actor or AgentId.A3,
        summary=f"上传 {document.file_name}",
        idempotency_key=f"{eid}:data:upload:{document.id}",
        payload=payload | ({"request_id": request_id} if request_id else {}),
        created_at=datetime.now(UTC),
    )


def not_imported_entry(eid: str, batch: ImportBatch) -> TrailEntry | None:
    """确认时没勾选而转为未导入的文件的 data 留痕 (一批一条): action not_imported, payload 带 batch_id, document_ids
    与 file_name (各文件名, 逗号分隔), actor 为确认人; 防重复键 {eid}:data:not_imported:{批次 ID}, 确认与补写共用.
    这批没有未导入的文件为 None."""
    files = [f for f in batch.files if f.not_imported]
    if not files:
        return None
    names = ", ".join(f.name for f in files)
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.data,
        actor=batch.committed_by or AgentId.A3,
        summary=f"智能导入 {batch.id} 确认时没勾选, 未导入 {len(files)} 个文件: {names}",
        idempotency_key=f"{eid}:data:not_imported:{batch.id}",
        payload={
            "action": "not_imported",
            "batch_id": batch.id,
            "document_ids": [f.document_id for f in files],
            "file_name": names,
        },
        created_at=batch.committed_at or datetime.now(UTC),
    )


def same_edit(
    record: ExtractionRecord | ContractVersion, version: int, edit: ExtractionEdit, actor: str, reason: str
) -> bool:
    """这一版是否就是当前版本, 且是同一人以同一理由改同一字段得出的."""
    return record.version == version and (record.field, record.changed_by, record.reason) == (edit.field, actor, reason)


def contract_retry(
    history: Sequence[ContractVersion], current: Contract, edit: ExtractionEdit, actor: str, reason: str
) -> bool:
    """当前合同版本是否正是这次修改上次写入的结果: 最新一版同一人以同一理由改同一字段, 把修改套在前一版上正得到它."""
    if len(history) < 2 or not same_edit(history[-1], current.version, edit, actor, reason):
        return False
    try:
        return apply_contract_edit(history[-2].contract, edit.field, edit.value) == ordered_terms(current)
    except DataRejected:
        return False


HELD_NOTES = {  # 暂存与未导入的文件不能补登记 (需求 R90): 确认导入时按确认单改类型与可靠性, 补的会被盖掉
    "staged": "文件 {} 还在导入确认单里, 确认导入后再补登记",
    "not_imported": "文件 {} 没有导入, 需要时重新上传",
}


def details_given(edit: DocumentDetailsEdit, file_name: str) -> dict[str, Any]:
    """补登记给了的各项 (None 为不改), 按 DETAIL_FIELDS 顺序; 关联对象各 ID 去首尾空白, 有空串或重复
    INVALID_VALUE."""
    given: dict[str, Any] = {k: getattr(edit, k) for k in DETAIL_FIELDS if getattr(edit, k) is not None}
    if edit.related_ids is not None:
        ids = [i.strip() for i in edit.related_ids]
        if any(not i for i in ids):
            raise DataRejected("INVALID_VALUE", f"文件 {file_name} 的关联对象有空的 ID")
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            raise DataRejected("INVALID_VALUE", f"文件 {file_name} 的关联对象重复: {', '.join(repeated)}")
        given["related_ids"] = ids
    return given


def details_retry(history: Sequence[ExtractionRecord], document: DocumentRef, actor: str, reason: str) -> bool:
    """没有要改的一次补登记是否就是上次的重试: 最新一版是同一人以同一理由补登记的, 且当前登记的三项正是那一版的."""
    if not history or (history[-1].changed_by, history[-1].reason) != (actor, reason) or history[-1].field is None:
        return False
    current = details_data(document)
    return all(history[-1].data.get(k) == current[k] for k in DETAIL_FIELDS)


def category_entry(eid: str, code: str, version: CategoryVersion) -> TrailEntry:
    """科目分类修改的 data 留痕: action edit, payload 带 table_kind accounts, account_code, field category,
    新分类与理由, actor 为修改人; 防重复键 {eid}:data:edit:account:{科目}:v{版本}, 写入与补写共用."""
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.data,
        actor=version.changed_by,
        summary=f"修改科目 {code} 的标准分类为 {version.category}, 第 {version.version} 版",
        idempotency_key=f"{eid}:data:edit:account:{code}:v{version.version}",
        payload={
            "action": "edit",
            "table_kind": TableKind.accounts.value,
            "account_code": code,
            "field": "category",
            "category": version.category,
            "reason": version.reason,
        },
        created_at=version.changed_at,
    )


def written_versions(
    tables: Mapping[TableKind, Sequence[BaseModel]],
    version: int,
    source: DataSource,
    actor: str,
    origins: Mapping[TableKind, TableOrigin],
    default: TableOrigin,
    version_id: str = "",
) -> list[TableVersion]:
    """一次写入的各表版本 (按 TableKind 顺序), 写版本留痕用 (见 version_entry); 出处取 origins, 没给的取 default."""
    return [
        TableVersion(
            id=version_id,
            kind=kind,
            source=source,
            file_name=origin.file_name,
            document_id=origin.document_id,
            records=len(tables[kind]),
            data_version=version,
            request_id=origin.request_id,
            created_by=actor,
            created_at=datetime.now(UTC),
        )
        for kind in sorted(tables, key=list(TableKind).index)
        for origin in [origins.get(kind) or default]
    ]


# 批次不在待确认时调整与确认的拒绝说明, 待确认或已写入时重新识别的拒绝说明
BATCH_NOT_READY: dict[str, str] = {"processing": "还在识别", "failed": "识别出错, 先重新识别", "committed": "已写入"}
BATCH_DONE: dict[str, str] = {"ready": "已识别完", "committed": "已写入"}


def failure_reason(exc: BaseException) -> str:
    """识别作业出错的原因 (ImportBatch.error): 异常类型与消息, 截断到 300 字."""
    return f"{type(exc).__name__}: {exc}"[:300]


def table_columns(kind: TableKind) -> list[TableColumn]:
    """账表查看的各列: 导入字段 (名称与类型); 科目表另有分类, 依据, 两期余额与是否改过."""
    columns = [TableColumn(name=f.name, type=f.type) for f in SCHEMAS[kind].fields]
    if kind is TableKind.accounts:
        columns += [
            TableColumn(name="category", type="text"),
            TableColumn(name="category_basis", type="text"),
            TableColumn(name="balance_current_cents", type="amount"),
            TableColumn(name="balance_prior_cents", type="amount"),
            TableColumn(name="edited", type="bool"),
        ]
    return columns


def schemas() -> list[TableSchema]:
    """各账表的导入字段 (GET /data/schemas), 按 TableKind 顺序; 不随项目变化."""
    return [SCHEMAS[kind] for kind in TableKind]


@dataclass(frozen=True)
class Uploaded:
    """上传的登记结果 (DataService.register_uploads): 各文件的登记 (按上传顺序; 按请求定了类型的为改后的), 要处理或
    补做的文件, 补做要接上的作业与各文件对应的待补交请求 (见 _resume_info), 上传带的待补交文件请求 (不是为空)."""

    documents: list[DocumentRef]
    fresh: list[DocumentRef]
    jobs: dict[str, Job]
    wanted: dict[str, Requested]
    requested: Requested | None


class NoXeroConnections:
    """没有接 Xero 连接存储时的缺省实现 (测试与只读场景): 一律未连接. 组装根用 services.xero_link 的实现."""

    async def source(self, eid: str) -> XeroSource:
        return XeroSource(status="not_connected")


class DataService:
    """数据管理的写操作与查询. 组装根建一个, 挂在 app.state.data_service."""

    def __init__(
        self,
        deps: AgentDeps,
        runner: GraphRunner,
        xero: XeroConnections | None = None,
        *,
        demo: DemoSource | None = None,
    ) -> None:
        self.deps = deps  # 外部依赖
        self.runner = runner  # 项目锁与后台推进; 图取 runner.app
        self.xero: XeroConnections = xero if xero is not None else NoXeroConnections()  # Xero 连接记录
        # 演示数据导入的清单来源 (需求 R269; 组装根为 RoutingDatasetLoader): 按数据集取已核对的补造目录; 为空时都没有
        # 演示资料
        self.demo = demo
        # 进行中的作业 (导入, 同步, 文件处理): 项目编号 → 作业 → 作业涉及的账表或文件. 单 worker, 同 runner 的项目锁
        self.jobs: dict[str, dict[object, frozenset[str]]] = {}
        self.kinds: dict[object, str] = {}  # 作业 → 作业种类 (launch 的 kind, 如 Xero 同步), 没给的不在其中
        self.tasks: set[asyncio.Task[None]] = set()  # launch 起的后台作业; 保留引用, 防止完成前被回收

    async def gate_now(self, eid: str) -> GateId | None:
        """检查点当前停在哪道闸门前 (未启动, 在执行或停在节点内等待时为空). 只读, 不取项目锁; Xero 组织信息核对
        (需求 R291) 据此判断按 Xero 更新时本项目是否采用新版本."""
        snapshot = await self.runner.app.aget_state(GraphRunner.config(eid))
        return current_gate(snapshot.next, fill_defaults(snapshot.values))

    def require_lock(self, eid: str, what: str) -> None:
        """持锁内函数 (write_tables, register_uploads, process_uploads, assign_type, save_details, 演示数据导入) 的守卫:
        没持项目锁时抛 RuntimeError, 不让锁外的调用与图或别的作业并发写."""
        if not self.runner.is_running(eid):
            raise RuntimeError(f"{eid} {what}须先取项目锁 (DataService.holding, locked 或 launch)")

    async def unlocked(self, eid: str) -> Engagement:
        """项目 (未锁定); 不存在抛 LookupError, 已锁定 ENGAGEMENT_LOCKED. 供不取项目锁的写前检查 (如 Xero 授权地址)."""
        return await self._check_unlocked(eid)

    async def _check_unlocked(self, eid: str) -> Engagement:
        """项目 (未锁定); 不存在抛 LookupError, 已锁定 ENGAGEMENT_LOCKED."""
        engagement = await self.deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(f"Engagement {eid} not found")
        if engagement.locked:
            raise DataRejected("ENGAGEMENT_LOCKED", eid)
        return engagement

    def _register(self, eid: str, labels: Iterable[str], kind: str | None = None) -> object:
        key = object()
        self.jobs.setdefault(eid, {})[key] = frozenset(labels)
        if kind is not None:
            self.kinds[key] = kind
        return key

    def _relabel(self, eid: str, key: object, labels: Iterable[str]) -> None:
        """改已登记作业涉及的账表或文件 (持项目锁时)."""
        self.jobs[eid][key] = frozenset(labels)

    def _unregister(self, eid: str, key: object) -> None:
        jobs = self.jobs.get(eid, {})
        jobs.pop(key, None)
        self.kinds.pop(key, None)
        if not jobs:
            self.jobs.pop(eid, None)

    def running(self, eid: str, kind: str) -> bool:
        """该项目有没有登记着的 kind 类作业 (launch 时给的种类): 从登记 (取项目锁之后, prepare 之前) 到作业结束注销,
        期间项目锁被它占着, 再同步或导入报 ENGAGEMENT_BUSY."""
        return any(self.kinds.get(key) == kind for key in self.jobs.get(eid, {}))

    @asynccontextmanager
    async def locked(self, eid: str) -> AsyncIterator[None]:
        """在块内做一件不登记为作业的数据写操作 (如登记导入会话, 换工作表): 查锁定, 取项目锁, 离开时释放.
        与提交 (holding) 互斥, 换表不会插进提交的写入之间."""
        await self._check_unlocked(eid)
        self.runner.acquire(eid)
        try:
            yield
        finally:
            self.runner.release(eid)

    @asynccontextmanager
    async def holding(self, eid: str, labels: Iterable[str]) -> AsyncIterator[object]:
        """在块内做一件数据写操作: 进入时查锁定 (ENGAGEMENT_LOCKED), 取项目锁 (取不到 ENGAGEMENT_BUSY), 把 labels
        (涉及的账表种类或文件 ID) 登记为进行中的作业, 补写以前没写成的 data 留痕 (backfill_trails); 离开时注销,
        成功时锁交给 continue_waiting (图在等程序要的数据就交给后台恢复, 否则释放), 出错时释放. 数据提交在注销之前,
        概览据此判断. 块内得到作业的登记键, 取锁后才知道涉及什么的 (如确认导入) 用 _relabel 改登记."""
        await self._check_unlocked(eid)
        self.runner.acquire(eid)
        key = self._register(eid, labels)
        try:
            await self.backfill_trails(eid)
            yield key
        except BaseException:
            self._unregister(eid, key)
            self.runner.release(eid)
            raise
        self._unregister(eid, key)
        await self.continue_waiting(eid)

    async def continue_waiting(self, eid: str) -> None:
        """持项目锁的数据写操作结束时调用, 锁由此交出或释放, 调用方不再管锁: 图停在 await_data 或 p0_inputs 等程序
        要的数据 (需求 R272, R290) 时交给后台, 以 Command(resume=INPUTS_CHANGED) 恢复, 由节点按现有资料重查, 后台
        任务结束时释放; 否则释放. 读检查点出错只记日志, 释放; 读检查点时被取消 (请求取消, 应用退出) 也释放, 再照常
        抛出; 后台任务建不起来时 runner.launch 已释放锁, 只记日志."""
        handed = False  # 锁已交给 runner.launch
        try:
            try:
                waiting = awaiting_inputs(await self.runner.app.aget_state(GraphRunner.config(eid)))
            except Exception:
                logger.exception("数据写入后读检查点失败: %s", eid)
                return
            if waiting:
                handed = True
                try:
                    self.runner.launch(eid, Command(resume=INPUTS_CHANGED))
                except Exception:
                    logger.exception("数据写入后恢复等数据的程序失败: %s", eid)
        finally:
            if not handed:
                self.runner.release(eid)

    async def launch(
        self,
        eid: str,
        labels: Iterable[str],
        job: Callable[[], Awaitable[None]],
        prepare: Callable[[], Awaitable[None]] | None = None,
        kind: str | None = None,
        failed: Callable[[BaseException], Awaitable[None]] | None = None,
    ) -> None:
        """在后台做一件数据写操作 (如 Xero 同步, 文件处理): 在调用方这里查锁定, 取项目锁并登记作业 (不过立即抛出,
        同 holding), 再执行 prepare (有时; 起作业前要当场报错的检查, 如 Xero 连接是否可用), 它抛错时注销, 释放锁并
        上抛; 后台补写缺的留痕后执行 job; job 结束 (成败) 时注销, 锁交给 continue_waiting (见 _spawn). job 自己处理
        业务失败 (如把文件置 needs_manual); 漏出的异常只记日志. kind: 作业种类, 登记期间 running(eid, kind) 为真.
        failed: 补写留痕或 job 出错时以异常调用 (如记一条失败留痕, 仍在锁内), 之后异常照常交 _spawn 记日志."""
        names = frozenset(labels)
        await self._check_unlocked(eid)
        self.runner.acquire(eid)
        key = self._register(eid, names, kind)
        if prepare is not None:
            try:
                await prepare()
            except BaseException:
                self._unregister(eid, key)
                self.runner.release(eid)
                raise

        async def run() -> None:
            try:
                await self.backfill_trails(eid)
                await job()
            except Exception as error:
                if failed is not None:
                    await failed(error)
                raise

        self._spawn(eid, key, run)

    def _spawn(
        self,
        eid: str,
        key: object,
        job: Callable[[], Awaitable[None]],
        then: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """在后台执行已登记的作业 (调用方已持项目锁并登记 key); 漏出的异常只记日志. 作业结束 (成败) 后注销, 锁交给
        then (缺省 continue_waiting: 图在等程序要的数据就交给后台恢复, 否则释放), 由它交出或释放, 完成回调不再释放;
        作业没跑完就被取消 (含还没开始) 时由任务的完成回调注销并释放锁, 只做一次. then 须保证锁交出或释放 (含取消)."""
        done = False
        handed = False  # 锁已交给 then

        def finish(_: object = None) -> None:
            nonlocal done
            if not done:
                done = True
                self._unregister(eid, key)
                if not handed:
                    self.runner.release(eid)

        async def run() -> None:
            nonlocal handed
            try:
                await job()
            except Exception:
                logger.exception("数据作业失败: %s %s", eid, sorted(self.jobs.get(eid, {}).get(key, ())))
            self._unregister(eid, key)
            handed = True  # 从这里到 then 内部第一次等待之间不会被取消打断
            await (then or self.continue_waiting)(eid)

        try:
            task = asyncio.create_task(run())
        except BaseException:
            finish()
            raise
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(finish)

    async def backfill_trails(self, eid: str) -> None:
        """补写已提交而 data 留痕没写成的账表版本, 抽取结果修改, 最近一次体检与补交请求的提出和收到, 智能导入确认时
        未导入的文件 (写入后留痕失败, 或进程在两者之间中断); 同键已有的跳过. 数据集载入的版本不记留痕. 在取得项目锁
        之后调用."""
        versions = [v for v in await self.deps.data.versions(eid) if v.source != "dataset"]
        edits = [r for r in await self.deps.data.extraction_versions(eid) if r.field is not None]
        entries = [version_entry(eid, v) for v in versions] + [edit_entry(eid, r) for r in edits]
        entries += checkup_entries(eid, await self.deps.data.check_run(eid), await self.deps.data.requests(eid))
        for code, history in (await self.deps.data.category_history(eid)).items():
            entries += [category_entry(eid, code, v) for v in history if v.basis == "auditor"]
        committed = [b for b in await self.deps.data.batches(eid) if b.status == "committed"]
        entries += [e for b in committed if (e := not_imported_entry(eid, b)) is not None]
        await record_transitions(self.deps.trail, entries)

    async def checkup_if_stale(self, eid: str) -> None:
        """最近一次体检已不对应当前数据时补做 (DataCheckup.run_if_stale); 持项目锁时调用, 出错只记日志."""
        try:
            await DataCheckup(self.deps).run_if_stale(eid)
        except Exception:
            logger.exception("数据体检失败: %s", eid)

    async def checkup(self, eid: str) -> None:
        """A3 跑一次体检并处理补交请求 (agents.checkup.DataCheckup); 在数据写操作或文件处理作业的最后调用 (持项目锁).
        体检出错只记日志: 数据已提交, 不因体检失败报错; 下次写入或处理文件时再体检."""
        try:
            await DataCheckup(self.deps).run(eid)
        except Exception:
            logger.exception("数据体检失败: %s", eid)

    async def shutdown(self) -> None:
        """应用退出前取消后台作业并等它们结束 (锁随之释放, 见 launch 的完成回调)."""
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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
        checkup: bool = True,
    ) -> int:
        """新数据版本写入 (导入与 Xero 同步共用; 智能导入的确认另经 DataStore.commit_batch, 写法相同), 返回写入后的数据
        版本. 须在 holding 块内或 launch 的作业里
        调用 (持项目锁), 否则抛 RuntimeError. 写入规则见 DataStore.write_tables (origins 为各表的出处); 提交后每张表
        写一条 data 留痕 (见 version_entry). 留痕写失败时数据已提交: 下次取项目锁做数据写操作时补写 (见
        backfill_trails). checkup 为假时写完不体检 (演示数据导入写完各步后只体检一次)."""
        self.require_lock(eid, "写入账表")
        version = await self.deps.data.write_tables(
            eid,
            tables,
            source=source,
            actor=actor,
            file_name=file_name,
            document_id=document_id,
            import_id=import_id,
            request_id=request_id,
            **({"origins": origins} if origins is not None else {}),
        )
        default = TableOrigin(file_name=file_name, document_id=document_id, request_id=request_id)
        written = written_versions(tables, version, source, actor, origins or {}, default, import_id or "")
        try:
            await record_transitions(self.deps.trail, [version_entry(eid, item) for item in written])
        finally:  # 留痕写失败 (数据已提交) 也照常体检
            if checkup:
                await self.checkup(eid)
        return version

    # ── 账表查看与科目分类修改 (需求 R16) ──

    async def table_page(self, eid: str, kind: TableKind, q: str | None, offset: int, limit: int) -> TablePage:
        """账表查看 (GET /data/tables/{kind}): 一页的行与符合搜索的总行数, 列见 table_columns; 搜索与排序见
        DataStore.table_rows. 账表数值不能在这里改, 只能经导入写新版本 (需求 R13)."""
        total, rows = await self.deps.data.table_rows(eid, kind, (q or "").strip() or None, offset, limit)
        columns = table_columns(kind)
        return TablePage(
            total=total, columns=columns, rows=[{c.name: row.get(c.name) for c in columns} for row in rows]
        )

    async def account_history(self, eid: str, code: str) -> list[CategoryVersion]:
        """科目标准分类的各版 (GET /data/accounts/{code}/history), 按版本升序; 科目不存在 404. 数据集载入后没改过的
        科目没有历史, 以当前分类作唯一一版返回 (修改人 A3, 时间取首次载入)."""
        account = await self.deps.data.account(eid, code)
        if account is None:
            raise LookupError(f"科目 {code} 不存在")
        history = (await self.deps.data.category_history(eid)).get(code)
        if history:
            return history
        loaded = (await self.deps.data.readiness(eid)).loaded_at or datetime.now(UTC)
        return [
            CategoryVersion(
                version=1,
                category=account.category,
                basis=account.category_basis,
                changed_by=AgentId.A3,
                changed_at=loaded,
            )
        ]

    async def set_account_category(
        self, eid: str, code: str, category: AccountCategory, reason: str, actor: str
    ) -> CategoryVersion:
        """审计师修改科目的标准分类 (PUT /data/accounts/{code}): 存储规则见 DataStore.set_category (新的一版, 科目表
        改为它, 数据版本 +1, 一个事务); 提交后写 data 留痕 (category_entry; 没写成的下次取锁时补写), 再由 A3 体检
        (分类影响科目映射与留存收益结转). 之后重新导入科目表时保留审计师的分类.
        持项目锁 (锁定 423, 忙 409); 科目不存在 404; 理由为空 MISSING_FIELD; 分类不属于该科目的类别或与当前相同
        INVALID_VALUE. 同一修改的重试 (当前分类正是同一人以同一理由改出的) 返回那一版, 不再写."""
        reason = reason.strip()
        if not reason:
            raise DataRejected("MISSING_FIELD", "须填写修改分类的理由")
        async with self.holding(eid, ()):
            account = await self.deps.data.account(eid, code)
            if account is None:
                raise LookupError(f"科目 {code} 不存在")
            if category not in CATEGORY_GROUPS[account.type]:
                raise DataRejected("INVALID_VALUE", f"{category} 不是{account.type}类科目的标准分类")
            if account.category == category:
                history = (await self.deps.data.category_history(eid)).get(code, [])
                latest = history[-1] if history else None
                if latest is not None and (latest.basis, latest.changed_by, latest.reason) == (
                    "auditor",
                    actor,
                    reason,
                ):
                    return latest  # 同一修改的重试; 留痕已由 holding 补写
                raise DataRejected("INVALID_VALUE", f"科目 {code} 的分类已是 {category}")
            version = await self.deps.data.set_category(eid, code, category, actor=actor, reason=reason)
            try:
                await record_transitions(self.deps.trail, [category_entry(eid, code, version)])
            finally:  # 留痕写失败 (修改已提交) 也照常体检
                await self.checkup(eid)
        return version

    # ── 账表导入 (需求 R13) ──

    async def start_import(
        self, eid: str, file_name: str, data: bytes, *, actor: str, request_id: str | None = None
    ) -> ImportPreview:
        """解析上传的账表文件 (POST /data/imports): 暂存原文件, 登记导入会话, 返回第一个工作表的解析结果与建议.
        不写账表与留痕. 文件类型不支持或读不了 INVALID_VALUE; 登记会话时取项目锁 (锁定 423, 忙 409)."""
        await self._check_unlocked(eid)
        name = PurePath(file_name.replace("\\", "/")).name or "upload"
        try:
            names, sheet = await asyncio.to_thread(lambda: (sheet_names(name, data), read_sheet(name, data)))
        except UnreadableFile as exc:
            raise DataRejected("INVALID_VALUE", str(exc)) from exc
        session = ImportSession(
            id=f"IMP-{uuid4().hex[:12]}",
            file_name=name,
            sheet=sheet.name,
            request_id=request_id,
            created_by=actor,
            created_at=datetime.now(UTC),
        )
        async with self.locked(eid):
            await self.deps.data.create_import(eid, session, data)
        return preview(session.id, name, names, sheet)

    async def _session(self, eid: str, import_id: str) -> tuple[ImportSession, bytes]:
        """导入会话与原文件; 没有 NOT_FOUND (LookupError)."""
        found = await self.deps.data.import_session(eid, import_id)
        if found is None:
            raise LookupError(f"导入 {import_id} 不存在")
        return found

    async def _parsed_session(self, eid: str, import_id: str) -> tuple[ImportSession, bytes]:
        """未提交的导入会话与原文件; 没有 NOT_FOUND (LookupError), 已提交 INVALID_VALUE."""
        session, raw = await self._session(eid, import_id)
        if session.status != "parsed":
            raise DataRejected("INVALID_VALUE", f"导入 {import_id} 已提交")
        return session, raw

    async def change_sheet(self, eid: str, import_id: str, sheet: str) -> ImportPreview:
        """换工作表 (PUT /data/imports/{id}/sheet), 返回该表的解析结果与建议. 持项目锁做 (与提交互斥, 锁定 423,
        忙 409); 导入不存在 404, 已提交或工作表不存在 INVALID_VALUE."""
        async with self.locked(eid):
            session, raw = await self._parsed_session(eid, import_id)
            try:
                names, parsed = await asyncio.to_thread(
                    lambda: (sheet_names(session.file_name, raw), read_sheet(session.file_name, raw, sheet))
                )
            except UnreadableFile as exc:
                raise DataRejected("INVALID_VALUE", str(exc)) from exc
            await self.deps.data.update_import(eid, import_id, sheet=parsed.name)
        return preview(import_id, session.file_name, names, parsed)

    async def commit_import(self, eid: str, import_id: str, body: ImportCommit, actor: str) -> ImportResult:
        """提交导入 (POST /data/imports/{id}/commit): 按表种类, 工作表, 日期写法与列对应解析全表并转换类型 (不做业务
        校验), 原文件登记为证据文件 (类型 other, 内部副本, 已转换记 extracted), 以新数据版本整表替换该表.

        持项目锁做完 (holding, 作业涉及该表). 已提交的会话以相同的表种类, 工作表, 日期写法与列对应再提交 (重试) 时
        不再写入, 返回上次的结果 (上次没写成的留痕已在取锁时补写). 按顺序拒绝: 锁定 423, 忙 409, 会话不存在 404,
        已提交而选择不同 INVALID_VALUE,
        工作表不存在 INVALID_VALUE, 对应不可用 INVALID_VALUE 或 MISSING_FIELD (见 table_plan.check_plan),
        有解析失败 IMPORT_PARSE_ERROR (detail.errors 为前 20 处 {row, column, value}). 科目表按名称归类,
        记 category_basis (services.account_mapping).
        """
        async with self.holding(eid, {body.kind.value}):  # 进入时补写以前没写成的 data 留痕
            session, raw = await self._session(eid, import_id)
            if session.status == "committed":
                # 同一提交的重试 (如上次写入后留痕失败而报错): 不再写, 返回上次的结果; 选择不同则拒绝
                chosen = (session.kind, session.sheet, session.date_format, session.mapping)
                if chosen != (body.kind, body.sheet, body.date_format, body.mapping) or session.records is None:
                    raise DataRejected("INVALID_VALUE", f"导入 {import_id} 已提交")
                await self.checkup_if_stale(eid)  # 上次提交后的体检失败或被跳过时补做
                return ImportResult(kind=body.kind, records=session.records)
            try:
                sheet = await asyncio.to_thread(read_sheet, session.file_name, raw, body.sheet)
                rows, failures = await asyncio.to_thread(convert, body.kind, sheet, body.mapping, body.date_format)
            except UnreadableFile as exc:
                raise DataRejected("INVALID_VALUE", str(exc)) from exc
            except MappingError as exc:
                raise DataRejected(exc.code, exc.message) from exc
            if failures:
                raise DataRejected(
                    "IMPORT_PARSE_ERROR",
                    f"{len(failures)} 处解析失败",
                    errors=[f.model_dump() for f in failures[:MAX_FAILURES]],
                )
            if body.kind is TableKind.accounts:
                rows = [with_category(r) if isinstance(r, Account) else r for r in rows]
            await self.deps.data.update_import(
                eid,
                import_id,
                sheet=sheet.name,
                date_format=body.date_format,
                mapping=body.mapping,
                row_count=len(sheet.rows),
            )
            document = await self.deps.evidence_store.save(
                eid,
                session.file_name,
                raw,
                type=DocumentType.other,
                reliability=Reliability.internal_copy,
                media_type=media_type(session.file_name),
                uploaded_by=actor,
                status="extracted",
            )
            await self.write_tables(
                eid,
                {body.kind: rows},
                source="import",
                actor=actor,
                file_name=session.file_name,
                document_id=document.id,
                import_id=import_id,
                request_id=session.request_id,
            )
        return ImportResult(kind=body.kind, records=len(rows))

    # ── 智能导入的批次 (需求 R277) ──

    async def start_batch(
        self, eid: str, files: Sequence[tuple[str, bytes]], *, actor: str, request_id: str | None = None
    ) -> ImportBatch:
        """上传一批文件 (POST /data/batches): 每个文件登记为暂存的证据文件 (类型 other, 内部副本, status staged, 来源
        auditor_upload; 确认前不被程序使用, 启动时也不补做), 写上传留痕 (upload_entry), 登记批次 (processing), 由 A3
        在后台逐个文件识别 (process_batch, 持项目锁, 作业涉及这些文件), 返回刚登记的批次. 内容相同的文件只算一份;
        之前已登记的同一文件沿用已有的登记 (表格照常识别, 其他文件记 already_registered). 文件类型不支持
        INVALID_VALUE; 锁定 423; 忙 409."""
        names = [PurePath(name.replace("\\", "/")).name or "upload" for name, _ in files]
        supported = DOCUMENT_EXTENSIONS | TABLE_EXTENSIONS
        unsupported = [n for n in names if PurePath(n).suffix.lower() not in supported]
        if not files or unsupported:
            raise DataRejected(
                "INVALID_VALUE", f"只支持 CSV, Excel, PDF, Word, PNG, JPG 文件: {', '.join(unsupported)}"
            )
        await self._check_unlocked(eid)
        self.runner.acquire(eid)
        key = self._register(eid, ())
        try:
            await self.backfill_trails(eid)
            batch_files: list[BatchFile] = []
            for name, (_, data) in zip(names, files, strict=True):
                document = await self.deps.evidence_store.save(
                    eid,
                    name,
                    data,
                    type=DocumentType.other,
                    reliability=Reliability.internal_copy,
                    media_type=media_type(name),
                    uploaded_by=actor,
                    status="staged",
                )
                if document.status == "not_imported":  # 之前确认时没勾选的同一文件: 重新上传即回到暂存, 再识别
                    document = await self.deps.evidence_store.update_document(
                        eid,
                        document.id,
                        type=DocumentType.other,
                        reliability=Reliability.internal_copy,
                        status="staged",
                        failed_step=None,
                    )
                await record_transitions(self.deps.trail, [upload_entry(eid, document, request_id, actor)])
                if all(f.document_id != document.id for f in batch_files):
                    batch_files.append(BatchFile(name=name, document_id=document.id))
            batch = ImportBatch(
                id=f"BAT-{uuid4().hex[:12]}",
                status="processing",
                request_id=request_id,
                created_by=actor,
                created_at=datetime.now(UTC),
                files=batch_files,
            )
            await self.deps.data.save_batch(eid, batch)
            self.jobs[eid][key] = frozenset(f.document_id for f in batch_files)
        except BaseException:
            self._unregister(eid, key)
            self.runner.release(eid)
            raise
        self._spawn(eid, key, lambda: self.process_batch(eid, batch.id))
        return batch

    async def process_batch(self, eid: str, batch_id: str) -> None:
        """A3 处理批次里还没处理完的文件 (持项目锁, 在上传, 改文件类型, 重新识别或启动补做的作业里调用): 每处理完
        一个文件存一次 (该文件的各项, done), 重启后从没做完的文件接着做. 文件已有的项 (改了类型的, 或中途退出留下的)
        先去掉, 新项沿用它们的序号与手动勾选, 审计师改定的类型照用 (chosen_of). 全部做完后整张确认单收尾 (finalize),
        置 ready. 表格项的问题按项目当时的数据与期间判断. 一个文件出错只记该文件读不出; 逐文件之外出错 (查项目, 画像,
        补交请求, 存批次) 时批次置 failed 并记原因, 由审计师重新识别 (retry_batch)."""
        batch = await self.deps.data.batch(eid, batch_id)
        if batch is None or batch.status != "processing":
            return
        try:
            engagement = await self.deps.engagement_store.get(eid)
            if engagement is None:
                raise LookupError(f"Engagement {eid} not found")
            intake = BatchIntake(self.deps)
            ctx = await intake.context(eid, engagement)
            requested = await self._requested(eid, batch.request_id)
            for index, file in enumerate(batch.files):
                if file.done:
                    continue
                previous = [i for i in batch.items if i.document_id == file.document_id]
                items = [i for i in batch.items if i.document_id != file.document_id]
                after = max((item_number(i.id) for i in items), default=0) + 1
                first = min((item_number(i.id) for i in previous), default=after)
                try:
                    new = await intake.process_file(
                        eid, file, first, batch.id, engagement, ctx, requested, chosen_of(previous)
                    )
                except Exception:  # 一个文件出错 (如原件读不了) 不卡住整批: 记为读不出, 留给审计师处理
                    logger.exception("识别批次文件出错: %s %s %s", eid, batch_id, file.document_id)
                    new = [unreadable_item(f"{batch.id}-{first}", file)]
                files = [f.model_copy(update={"done": True}) if n == index else f for n, f in enumerate(batch.files)]
                merged = sorted([*items, *carried(previous, new)], key=lambda i: item_number(i.id))
                batch = batch.model_copy(update={"items": merged, "files": files})
                await self.deps.data.save_batch(eid, batch)
            batch = batch.model_copy(update={"status": "ready", "items": finalize(batch.items)})
            await self.deps.data.save_batch(eid, batch)
        except Exception as exc:
            logger.exception("识别批次出错: %s %s", eid, batch_id)
            failed = batch.model_copy(update={"status": "failed", "error": failure_reason(exc)})
            await self.deps.data.save_batch(eid, failed)

    async def retry_batch(self, eid: str, batch_id: str) -> ImportBatch:
        """重新识别 (POST /data/batches/{id}/retry): 识别作业出错 (failed) 或进程退出后停在识别中的批次, 清掉原因,
        置回 processing, 由 A3 在后台从没做完的文件接着做 (process_batch, 与启动补做同一段代码), 返回置回的批次.
        按顺序拒绝: 锁定 423; 忙 409 (识别中的批次由识别作业持锁, 也是 409); 批次不存在 404; 待确认或已写入
        INVALID_VALUE."""
        await self._check_unlocked(eid)
        self.runner.acquire(eid)
        key = self._register(eid, ())
        try:
            batch = await self.batch(eid, batch_id)
            if batch.status not in ("failed", "processing"):
                raise DataRejected("INVALID_VALUE", f"批次 {batch_id} {BATCH_DONE[batch.status]}, 不用重新识别")
            batch = batch.model_copy(update={"status": "processing", "error": None})
            await self.deps.data.save_batch(eid, batch)
            self._relabel(eid, key, (f.document_id for f in batch.files if not f.done))
        except BaseException:
            self._unregister(eid, key)
            self.runner.release(eid)
            raise
        self._spawn(eid, key, lambda: self.process_batch(eid, batch_id))
        return batch

    async def batch(self, eid: str, batch_id: str) -> ImportBatch:
        """一次智能导入与它的确认单 (GET /data/batches/{id}); 不存在 404. processing 时前端轮询, failed 时停止轮询并
        给重新识别的入口."""
        found = await self.deps.data.batch(eid, batch_id)
        if found is None:
            raise LookupError(f"批次 {batch_id} 不存在")
        return found

    async def open_batches(self, eid: str) -> list[ImportBatch]:
        """还没写入的智能导入 (GET /data/batches): 识别中, 待确认与识别出错的, 按上传时间升序. 审计师关掉确认单后从
        这里回到确认单; 文件在确认前一直暂存."""
        return [b for b in await self.deps.data.batches(eid) if b.status != "committed"]

    async def edit_batch_item(
        self, eid: str, batch_id: str, item_id: str, edit: BatchItemEdit, actor: str
    ) -> ImportBatch:
        """调整确认单的一项 (PUT /data/batches/{id}/items/{item_id}), 返回整张确认单.

        表格项给 plan: 换方案 (记调整人), 按新方案重算行数, 解析失败与问题 (调整后不再标把握低). 文件项给
        document_type: 改类型, 须给理由 (MISSING_FIELD); 之前已登记的文件不能改. 改了类型的文件回到识别中: 批次置
        processing, 该文件记没做完, 由 A3 在后台按新类型重新识别与抽取 (process_batch; 写审计师的 classify 留痕, 是新的
        作业), 抽取要素随之出现在确认单上, 做完置 ready. 给 selected: 手动勾选或取消 (记勾选人, 之后不再随问题变).
        没有改类型时整张确认单收尾 (重复的表等问题跨项重算). 先取项目锁再读批次: 锁定 423; 忙 409 (识别中的批次由识别
        作业持锁, 调整得到 409); 批次或该项不存在 404; 批次不在待确认 (进程退出后遗留的识别中, 识别出错, 已写入), 给的
        字段不适用于该项 INVALID_VALUE."""
        engagement = await self._check_unlocked(eid)
        self.runner.acquire(eid)
        key = self._register(eid, ())
        try:
            batch = await self.batch(eid, batch_id)
            if batch.status != "ready":
                raise DataRejected("INVALID_VALUE", f"批次 {batch_id} {BATCH_NOT_READY[batch.status]}")
            item = next((i for i in batch.items if i.id == item_id), None)
            if item is None:
                raise LookupError(f"批次 {batch_id} 没有 {item_id}")
            item, restage = await self._edited(eid, engagement, item, edit, actor)
            items = [item if i.id == item_id else i for i in batch.items]
            if restage:
                files = [
                    f.model_copy(update={"done": False}) if f.document_id == item.document_id else f
                    for f in batch.files
                ]
                batch = batch.model_copy(update={"status": "processing", "items": items, "files": files})
            else:
                batch = batch.model_copy(update={"items": finalize(items)})
            await self.deps.data.save_batch(eid, batch)
        except BaseException:
            self._unregister(eid, key)
            self.runner.release(eid)
            raise
        if restage:
            self._relabel(eid, key, {item.document_id})
            self._spawn(eid, key, lambda: self.process_batch(eid, batch_id))
        else:
            self._unregister(eid, key)
            self.runner.release(eid)
        return batch

    async def _edited(
        self, eid: str, engagement: Engagement, item: BatchItem, edit: BatchItemEdit, actor: str
    ) -> tuple[BatchItem, bool]:
        """按 edit 改好的一项 (见 edit_batch_item), 与是否要按新类型重新识别."""
        restage = False
        if edit.plan is not None:
            if item.item_kind != "table":
                raise DataRejected("INVALID_VALUE", f"{item.id} 是文件, 没有规整方案")
            intake = BatchIntake(self.deps)
            ctx = await intake.context(eid, engagement)
            item = await intake.reevaluate(eid, item.model_copy(update={"plan": edit.plan, "adjusted_by": actor}), ctx)
        if edit.document_type is not None and edit.document_type is not item.document_type:
            if item.item_kind != "document":
                raise DataRejected("INVALID_VALUE", f"{item.id} 是工作表, 不能改文件类型")
            if any(i.code == "already_registered" for i in item.issues):
                raise DataRejected("INVALID_VALUE", f"{item.file_name} 之前已登记, 在文件列表里改类型")
            reason = (edit.reason or "").strip()
            if not reason:
                raise DataRejected("MISSING_FIELD", "须填写改文件类型的理由")
            # 新类型的识别结果由 process_batch 填回; 在那之前确认单上显示新类型, 没有抽取要素
            item = item.model_copy(
                update={
                    "document_type": edit.document_type,
                    "type_reason": reason,
                    "adjusted_by": actor,
                    "job": None,
                    "contract": None,
                    "reply": None,
                    "issues": [],
                }
            )
            restage = True
        if edit.selected is not None:
            item = item.model_copy(update={"selected": edit.selected, "chosen_by": actor})
        return item, restage

    async def confirm_batch(self, eid: str, batch_id: str, actor: str) -> ImportBatch:
        """确认导入 (POST /data/batches/{id}/confirm): 一次写入所勾选的全部, 返回写入后的批次 (committed).

        持项目锁做 (holding, 取锁后作业登记为勾选的表). 勾选的表按方案重新套用全表; 文件按确认单换算成要写的登记与
        抽取结果 (写入表的原件为类型 other 置 extracted, 同账表导入; 文件项按 DocumentIntake.staged_change, 合同与回函
        用确认单上的抽取要素, 不再调大模型; 这批里一项都没勾的暂存文件转为未导入, 批次的 files 里记 not_imported);
        再由 DataStore.commit_batch 在一个事务里写表 (有表时一个新数据版本, 每张表一个版本行, 出处取各自的文件, 补交
        请求为账表请求且同表时记上), 改文件登记, 写合同与回函, 置批次 committed: 全部写成或全不写. 事务提交之后写各表
        的版本留痕与未导入文件的留痕 (写失败只记日志, 下次取项目锁时补写, 见 backfill_trails), 再体检一次 (没有表也
        体检). 批次 ID 是幂等键: 已写入的批次再确认 (如上次提交后进程退出) 不再写, 补做过期的体检后返回原结果. 已不是
        暂存的文件 (之前已登记, 或之后经 "上传文件" 重传处理了) 不改. 先取项目锁再读批次, 按顺序拒绝: 锁定 423; 忙
        409 (识别中的批次由识别作业持锁, 确认得到 409); 批次不存在 404; 不在待确认 (进程退出后遗留的识别中, 识别出错)
        INVALID_VALUE; 勾选了不是账表的项, 同一张表勾了两项 INVALID_VALUE; 勾选的文件没有类型 MISSING_FIELD; 方案套
        不上 INVALID_VALUE 或 MISSING_FIELD; 解析失败 IMPORT_PARSE_ERROR (detail.errors 为前 20 处)."""
        async with self.holding(eid, ()) as key:  # 进入时补写以前没写成的 data 留痕
            batch = await self.batch(eid, batch_id)
            if batch.status == "committed":
                await self.checkup_if_stale(eid)  # 上次提交后的体检失败或没做时补做
                return batch
            if batch.status != "ready":
                raise DataRejected("INVALID_VALUE", f"批次 {batch_id} {BATCH_NOT_READY[batch.status]}")
            selected = [i for i in batch.items if i.selected]
            sheets = [i for i in selected if i.item_kind == "table"]
            documents = [i for i in selected if i.item_kind == "document"]
            not_tables = [i.id for i in sheets if i.plan is None or i.plan.kind is None]
            if not_tables:
                raise DataRejected("INVALID_VALUE", f"勾选的工作表不是账表: {', '.join(not_tables)}")
            chosen = [i.plan.kind for i in sheets if i.plan is not None and i.plan.kind is not None]
            doubled = sorted({k.value for k in chosen if chosen.count(k) > 1})
            if doubled:
                raise DataRejected("INVALID_VALUE", f"同一张表勾了两项: {', '.join(doubled)}")
            untyped = [i.file_name for i in documents if i.document_type is None]
            if untyped:
                raise DataRejected("MISSING_FIELD", f"须指定文件类型: {', '.join(untyped)}")
            self._relabel(eid, key, (k.value for k in chosen))  # 概览: 这些表转换中
            tables, origins = await self._batch_tables(eid, batch, sheets)
            changes = await self._batch_documents(eid, batch, sheets, documents)
            dropped = {c.document_id for c in changes if c.status == "not_imported"}
            files = [f.model_copy(update={"not_imported": f.document_id in dropped}) for f in batch.files]
            committed = batch.model_copy(
                update={"status": "committed", "committed_at": datetime.now(UTC), "committed_by": actor, "files": files}
            )
            stored = await self.deps.data.commit_batch(
                eid, committed, tables, actor=actor, origins=origins, documents=changes
            )
            entries: list[TrailEntry] = []
            if stored.data_version is not None and tables:
                written = written_versions(tables, stored.data_version, "import", actor, origins, TableOrigin())
                entries = [version_entry(eid, v) for v in written]
            dropped_entry = not_imported_entry(eid, stored)
            try:
                await record_transitions(self.deps.trail, [*entries, *([dropped_entry] if dropped_entry else [])])
            except Exception:  # 数据已提交: 下次取项目锁时补写 (backfill_trails)
                logger.exception("智能导入的留痕写失败: %s %s", eid, batch_id)
            await self.checkup(eid)
        return stored

    async def _batch_tables(
        self, eid: str, batch: ImportBatch, sheets: Sequence[BatchItem]
    ) -> tuple[dict[TableKind, Sequence[BaseModel]], dict[TableKind, TableOrigin]]:
        """勾选的工作表按方案重新套用全表 (不用确认单上记的行数), 返回各表的行与出处; 科目表按名称归类.
        方案套不上 INVALID_VALUE 或 MISSING_FIELD, 解析失败 IMPORT_PARSE_ERROR (前 20 处)."""
        request = next((r for r in await self.deps.data.requests(eid) if r.id == batch.request_id), None)
        intake = BatchIntake(self.deps)
        tables: dict[TableKind, Sequence[BaseModel]] = {}
        origins: dict[TableKind, TableOrigin] = {}
        for item in sheets:
            assert item.plan is not None and item.plan.kind is not None and item.sheet is not None
            data = await intake.content(eid, item.document_id)
            try:
                name, rows = await asyncio.to_thread(raw_rows, item.file_name, data, item.sheet)
                converted, failures = await asyncio.to_thread(apply_plan, item.plan, name, rows)
            except UnreadableFile as exc:
                raise DataRejected("INVALID_VALUE", str(exc)) from exc
            except MappingError as exc:
                raise DataRejected(exc.code, f"{item.file_name} {item.sheet}: {exc.message}") from exc
            if failures:  # 失败处的取值同确认单上的一样遮蔽
                raise DataRejected(
                    "IMPORT_PARSE_ERROR",
                    f"{item.file_name} {item.sheet}: {len(failures)} 处解析失败",
                    errors=[f.model_dump() for f in masked_failures(rows, failures[:MAX_FAILURES])],
                )
            kind = item.plan.kind
            if kind is TableKind.accounts:
                converted = [with_category(r) if isinstance(r, Account) else r for r in converted]
            tables[kind] = converted
            for_request = request is not None and request.table_kind is kind
            origins[kind] = TableOrigin(
                file_name=item.file_name,
                document_id=item.document_id,
                request_id=batch.request_id if for_request else None,
            )
        return tables, origins

    async def _batch_documents(
        self, eid: str, batch: ImportBatch, sheets: Sequence[BatchItem], documents: Sequence[BatchItem]
    ) -> list[DocumentChange]:
        """按确认单换算各文件要写的登记与抽取结果 (只读): 写入表的原件为类型 other, 内部副本, 置 extracted (同账表
        导入); 勾选的文件项按确认单上的结果 (DocumentIntake.staged_change); 这批里一项都没勾的暂存文件转为未导入
        (not_imported, 类型 other; 同一文件还在别的没写入的批次里时不动, 留给那一批). 已不是暂存的文件 (之前已登记,
        或之后经 "上传文件" 重传处理了) 不改. 同一批里两份文件抽出同一编号的合同时, 后一份接在前一份之后 (版本 +1)."""
        current = {d.id: d for d in await self.deps.financial_store.documents(eid)}
        changes = [
            DocumentChange(
                document_id=document_id,
                type=DocumentType.other,
                reliability=Reliability.internal_copy,
                status="extracted",
            )
            for document_id in dict.fromkeys(i.document_id for i in sheets)
            if current[document_id].status == "staged"
        ]
        intake = DocumentIntake(self.deps)
        written: dict[str, int] = {}  # 本批已排定的合同编号 (与待审财报 ID) → 版本
        for item in documents:
            document = current[item.document_id]
            if document.status != "staged" or item.document_type is None:
                continue
            change = await intake.staged_change(eid, document, staged_of(item))
            if change.contracts:
                last = change.contracts[-1]
                if last.id in written:
                    last = last.model_copy(update={"version": written[last.id] + 1})
                    change = change.model_copy(update={"contracts": [last]})
                written[last.id] = last.version
            if change.financial_statements is not None:  # 同一批里两份财务报表: 后一份接在前一份之后
                statements = change.financial_statements
                if statements.id in written:
                    statements = statements.model_copy(update={"version": written[statements.id] + 1})
                    change = change.model_copy(update={"financial_statements": statements})
                written[statements.id] = statements.version
            changes.append(change)
        chosen = {i.document_id for i in [*sheets, *documents]}
        elsewhere = {
            f.document_id
            for b in await self.deps.data.batches(eid)
            if b.status != "committed" and b.id != batch.id
            for f in b.files
        }
        changes += [
            DocumentChange(
                document_id=f.document_id,
                type=DocumentType.other,
                reliability=Reliability.internal_copy,
                status="not_imported",
            )
            for f in batch.files
            if f.document_id not in chosen | elsewhere and current[f.document_id].status == "staged"
        ]
        return changes

    def processing(self, eid: str) -> list[frozenset[str]]:
        """该项目进行中的作业, 各作业涉及的账表种类或文件 ID."""
        return list(self.jobs.get(eid, {}).values())

    async def summary(self, eid: str) -> DataSummary:
        """数据管理页的概览 (GET /data/summary): Xero 连接, 各账表, 最近一次体检, 进行中的作业涉及的账表与文件数,
        待补交请求数.

        各账表按 TableKind 顺序: 来源, 原文件名与更新时间取最新一版 (data_imports 已提交的行, 数据版本最大者);
        数据集载入的表没有版本行, 来源为空, 更新时间取首次载入时间. 行数与日期范围取当前数据.
        状态: 有进行中的作业涉及该表为 converting; 否则有行 (审批日志: 有版本) 为 received, 没有为 not_received.
        作业在登记期间提交数据, 提交后才注销, 所以先取作业快照再读数据 (见下).
        """
        # 作业快照先于读数据取, converting 与 processing 都按它: 读数据期间结束的作业仍计入 processing, 前端会再轮询
        # 一次; processing 为 0 的响应里, 快照前结束的作业已提交, 读到的是完成后的数据
        jobs = self.processing(eid)
        busy = {label for job in jobs for label in job}
        data = self.deps.data
        stats = await data.stats(eid)
        latest: dict[TableKind, TableVersion] = {}
        for written in await data.versions(eid):  # 按数据版本升序, 后者覆盖前者
            latest[written.kind] = written
        readiness = await data.readiness(eid)
        tables = []
        for kind in TableKind:
            item, version = stats[kind], latest.get(kind)
            received = version is not None if kind is TableKind.approval_log else item.records > 0
            status = "converting" if kind.value in busy else "received" if received else "not_received"
            tables.append(
                TableSummary(
                    kind=kind,
                    source=version.source if version is not None and version.source != "dataset" else None,
                    file_name=version.file_name if version is not None else None,
                    records=item.records,
                    records_detail=RecordsDetail(current=item.current, prior=item.prior)
                    if kind is TableKind.trial_balance
                    else None,
                    period_from=item.period_from,
                    period_to=item.period_to,
                    status=status,
                    updated_at=version.created_at if version is not None else readiness.loaded_at if received else None,
                )
            )
        return DataSummary(
            sources=DataSources(xero=await self.xero.source(eid)),
            tables=tables,
            checks=await data.checks(eid),
            processing=sum(len(job) for job in jobs),
            open_requests=await data.open_requests(eid),
            demo=await self.demo_summary(eid),
        )

    async def demo_summary(self, eid: str) -> DemoSummary:
        """数据管理页的演示数据导入入口 (需求 R270, 见 DemoSummary). 不是带补造目录的数据集 (只查目录在不在, 不核对
        哈希) 时只给 available 为假, 不读留痕与检查点. 已导入的阶段取全部 demo_import 留痕 (各阶段最近一条为
        imported 的, stage_statuses), 最近一次结果取 AuditTrail.last_of; 流程停的位置见 _demo_stage_now."""
        engagement = await self.deps.engagement_store.get(eid)
        dataset = engagement.dataset if engagement is not None else None
        if self.demo is None or dataset is None or not await asyncio.to_thread(self.demo.has_supplement, dataset):
            return DemoSummary()
        trail = self.deps.trail
        statuses = stage_statuses(await all_of_kind(trail, eid, TrailKind.data))
        done = [stage for stage in DEMO_IMPORT_STAGES if statuses.get(stage) == "imported"]
        latest = await trail.last_of(eid, TrailKind.data, [DEMO_JOB])
        last = None
        if latest is not None:
            try:
                last = DemoImportResult.model_validate({k: v for k, v in latest.payload.items() if k != "action"})
            except ValidationError:  # 认不出的旧留痕不让概览出错
                last = None
        suggested = await self._demo_stage_now(eid)
        return DemoSummary(
            available=True, stages_done=done, suggested=suggested if suggested not in done else None, last=last
        )

    async def _demo_stage_now(self, eid: str) -> DemoStage | None:
        """按流程停的位置该导入的阶段 (不看做过没有): ACC-01 闸门, 等数据就绪, 待审财报请求 open; 检查点只读一次.
        图在后台跑 (持项目锁而本服务没有登记着的数据作业) 时流程没停在任何位置, 为空 (这时导入报 ENGAGEMENT_BUSY);
        数据作业 (如这次导入本身) 持锁时照常判断, 页面按 processing 不让再点."""
        if self.runner.is_running(eid) and not self.jobs.get(eid):
            return None
        snapshot = await self.runner.app.aget_state(GraphRunner.config(eid))
        if current_gate(snapshot.next, fill_defaults(snapshot.values)) is GateId.ACC_01:
            return "acceptance"
        if awaiting_data(snapshot):
            return "fieldwork"
        requests = {r.id: r.status for r in await self.deps.data.requests(eid)}
        return "statements" if requests.get(STAGE_REQUESTS["statements"]) == "open" else None

    async def confirm_ready(self, eid: str, actor: str, reason: str | None = None) -> DataReadiness:
        """审计师确认数据已齐 (POST /data/ready): 越过 P1 开始前的等待, 后台继续 DOC-04 及后续节点.

        按顺序校验, 第一条不过即拒绝 (DataRejected): 项目锁定 ENGAGEMENT_LOCKED; 取项目锁失败 ENGAGEMENT_BUSY;
        图不在等数据就绪 NOT_AWAITING_DATA; 就绪条件不满足 DATA_NOT_READY (附 missing_tables); 最近一次体检有不通过
        (failed) 的项而理由为空 (去首尾空白后) DATA_CHECKS_FAILED (附 failed_checks, 需求 R273). 判断体检前先补做
        已不对应当前数据的体检 (checkup_if_stale; 补做出错只记日志, 按已有的最近一次结果判断).
        通过后记确认, 连同理由与这些未通过项 (规则见 DataStore.confirm_ready: 人与时间保留第一次的, 第一次没有未通过项
        而本次有时补记本次的), 写 data 留痕 (action ready, 同键已有的跳过), 以 Command(resume=DATA_READY) 在后台恢复;
        项目锁交给后台任务. 确认已记而图仍在等 (上次留痕或恢复没成) 时再确认一次照常校验, 补写留痕并恢复; recover 与
        resume 也会补做 (GraphRunner._proceed).
        """
        engagement = await self.deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(f"Engagement {eid} not found")
        if engagement.locked:
            raise DataRejected("ENGAGEMENT_LOCKED", eid)
        self.runner.acquire(eid)  # 取不到抛 ENGAGEMENT_BUSY
        try:
            snapshot = await self.runner.app.aget_state(GraphRunner.config(eid))
            if not awaiting_data(snapshot):
                raise DataRejected("NOT_AWAITING_DATA", "当前不在等数据就绪")
            missing = missing_tables(await self.deps.data.stats(eid))
            if missing:
                raise DataRejected(
                    "DATA_NOT_READY", f"还缺: {', '.join(missing)}", missing_tables=[k.value for k in missing]
                )
            await self.checkup_if_stale(eid)  # 写入后的体检出错时, data_checks 还是上一版数据的结果
            failed = [check.id for check in await self.deps.data.checks(eid) if check.status == "failed"]
            reason = (reason or "").strip() or None
            if failed and reason is None:
                raise DataRejected(
                    "DATA_CHECKS_FAILED",
                    f"体检不通过: {', '.join(failed)}; 须写明理由才能确认",
                    failed_checks=[c.value for c in failed],
                )
            readiness = await self.deps.data.confirm_ready(eid, actor, reason=reason, failed_checks=failed)
            await record_transitions(self.deps.trail, [data_ready_entry(eid, readiness)])
        except BaseException:
            self.runner.release(eid)  # 后台任务尚未启动, 须在此释放
            raise
        self.runner.launch(eid, Command(resume=DATA_READY))
        return readiness

    async def mark_unavailable(self, eid: str, request_id: str, reason: str, actor: str) -> DataRequest:
        """审计师在补交卡片上标 "客户无此类资料" (POST /data/requests/{id}/unavailable, 需求 R280): 请求转 unavailable,
        记理由, 标记人与时间, 写 data 留痕 (action request_unavailable, 带理由与 used_for). 在 holding 内做 (不计进
        处理中的作业), 结束时图若在等程序要的数据就交锁恢复: await_data 重查, 等这项资料的程序与其下游记 no_data.
        标记是定论, 之后的体检不重开也不改 (data_quality.reconcile).

        按顺序拒绝: 锁定 423, 忙 409; 请求不存在 404; 理由去首尾空白后为空 MISSING_FIELD; 不是 open
        REQUEST_NOT_OPEN; 就绪必需的表 (P1 等数据已保证有) INVALID_VALUE."""
        async with self.holding(eid, []):
            request = next((r for r in await self.deps.data.requests(eid) if r.id == request_id), None)
            if request is None:
                raise LookupError(f"补交请求 {request_id} 不存在")
            reason = reason.strip()
            if not reason:
                raise DataRejected("MISSING_FIELD", "须写明客户没有这类资料的理由")
            if request.status != "open":
                raise DataRejected("REQUEST_NOT_OPEN", f"{request_id} 已是 {request.status}")
            if request.table_kind in REQUIRED_TABLES:
                raise DataRejected("INVALID_VALUE", f"{request.title}是就绪必需的表, 不能标客户无此类资料")
            marked = await self.deps.data.mark_unavailable(
                eid, request_id, reason=reason, actor=actor, at=datetime.now(UTC)
            )
            if marked is None:  # 读过之后被改了 (存储层只改仍为 open 的)
                raise DataRejected("REQUEST_NOT_OPEN", f"{request_id} 已不是 open")
            await record_transitions(self.deps.trail, [request_entry(eid, marked, "request_unavailable")])
        return marked

    # ── 演示数据导入 (需求 R269) ──

    async def demo_import(self, eid: str, stage: str, actor: str) -> DemoImportResult:
        """导入演示资料的一个阶段 (DEMO_IMPORT_STAGES): 当场核对后在后台做 (DemoImport.run, 持项目锁一次做完, 做完体检
        一次), 返回 status started 的结果 (要写的表与要上传的文件). 阶段不认识, 或为 manual (管理层声明书由审计师在
        FIN-07 的人工任务页上传) INVALID_VALUE; 项目不存在 LookupError; 数据集没有补造目录 (Numbat, 20261017-mock,
        没有数据集的新客户) INVALID_VALUE "该数据集没有演示资料", 补造目录与快照不符 INVALID_VALUE (消息只列文件名);
        锁定 423; 忙 409. 作业期间概览的 processing 计入, 涉及这一阶段的账表与文件."""
        if stage == "manual":
            raise DataRejected("INVALID_VALUE", "管理层声明书由审计师在 FIN-07 的人工任务页上传, 不经演示数据导入")
        if stage not in DEMO_IMPORT_STAGES:
            raise DataRejected("INVALID_VALUE", f"演示数据导入没有阶段 {stage}, 可选 {', '.join(DEMO_IMPORT_STAGES)}")
        chosen: DemoStage = stage
        engagement = await self._check_unlocked(eid)
        importer = DemoImport(self, await self._demo_plan(engagement))

        async def job() -> None:
            await importer.run(eid, chosen, actor)

        async def failed(error: BaseException) -> None:  # 记一条 failed 的留痕, 页面据此提示再导入一次
            await importer.record_failure(eid, chosen, actor, error)

        await self.launch(eid, importer.labels(chosen), job, kind=DEMO_JOB, failed=failed)
        return importer.started(chosen)

    async def _demo_plan(self, engagement: Engagement) -> DemoPlan:
        """项目数据集的演示导入清单 (xero_supplements.plan, 在线程里核对与读补造目录); 没有时 INVALID_VALUE."""
        if self.demo is None or engagement.dataset is None:
            raise DataRejected("INVALID_VALUE", NO_DEMO)
        try:
            return await asyncio.to_thread(plan, self.demo, engagement.dataset)
        except FileNotFoundError:
            raise DataRejected("INVALID_VALUE", NO_DEMO) from None
        except ValueError as e:  # 补造与快照不符 (SupplementMismatch), 清单不合规
            raise DataRejected("INVALID_VALUE", str(e)) from None

    # ── 文件上传, 分类与抽取结果修改 (需求 R14) ──

    async def upload_documents(
        self, eid: str, files: Sequence[tuple[str, bytes]], *, actor: str, request_id: str | None = None
    ) -> list[DocumentRef]:
        """上传文件 (POST /documents): 登记为证据文件 (先记 other, 内部副本, received, 来源 auditor_upload),
        每份写 data 留痕 (upload_entry; 同一文件只记一条, 重传时补上以前没写成的), 返回各文件的登记.
        处理没做完的文件 (ingest.unfinished: 新登记的; 以前登记了却没处理成, 如上次上传在留痕或批内后面的文件处失败;
        分类后抽取前作业被取消) 随即由 A3 在后台处理或补做 (DocumentIntake), 作业持项目锁, 涉及这些文件, 做完体检;
        已处理完的同一文件重复上传只返回已有的登记. request_id 是待补交的文件请求时, 在请求里就按请求定类型
        (DocumentIntake.mark: classify 留痕与登记先落库, 后台只做抽取; ingest.retype: 含以前上传过而分不了类或类型不同的
        同一文件), 返回改后的登记, 作业做完体检 (没有要处理的文件也体检); 补做没做完的文件时从上传留痕取回当时带的请求.
        文件类型不支持 INVALID_VALUE; 锁定 423; 忙 409."""
        names = [PurePath(name.replace("\\", "/")).name or "upload" for name, _ in files]
        unsupported = [n for n in names if PurePath(n).suffix.lower() not in DOCUMENT_EXTENSIONS]
        if not files or unsupported:
            raise DataRejected("INVALID_VALUE", f"只支持 PDF, Word, Excel, PNG, JPG 文件: {', '.join(unsupported)}")
        engagement = await self._check_unlocked(eid)
        self.runner.acquire(eid)
        key = self._register(eid, ())
        try:
            await self.backfill_trails(eid)
            saved = await self.register_uploads(eid, names, [data for _, data in files], actor, request_id)
            self.jobs[eid][key] = frozenset(d.id for d in saved.fresh)
        except BaseException:
            self._unregister(eid, key)
            self.runner.release(eid)
            raise
        if not saved.fresh and saved.requested is None:  # 都是已处理完的文件: 不再处理
            self._unregister(eid, key)
            self.runner.release(eid)
            return saved.documents
        self._spawn(eid, key, self._intake(eid, engagement, saved.fresh, saved.jobs, saved.wanted))
        return saved.documents

    async def register_uploads(
        self, eid: str, names: Sequence[str], contents: Sequence[bytes], actor: str, request_id: str | None
    ) -> "Uploaded":
        """上传的登记部分 (持项目锁时调用, upload_documents 与演示数据导入共用): 逐份存档 (先记 other, 内部副本,
        received, 来源 auditor_upload, 上传人 actor), 写 upload 留痕; 算出要处理或补做的文件与补做要接上的信息; 对着
        待补交的文件请求上传时按请求定类型先落库. 不处理文件, 不体检. 未持项目锁抛 RuntimeError."""
        self.require_lock(eid, "上传文件")
        requested = await self._requested(eid, request_id)
        documents: list[DocumentRef] = []
        fresh: list[DocumentRef] = []
        for name, data in zip(names, contents, strict=True):
            document = await self.deps.evidence_store.save(
                eid,
                name,
                data,
                type=DocumentType.other,
                reliability=Reliability.internal_copy,
                media_type=media_type(name),
                uploaded_by=actor,
            )
            documents.append(document)
            await record_transitions(self.deps.trail, [upload_entry(eid, document, request_id, actor)])
        jobs, wanted = await self._resume_info(eid, documents)
        for document in documents:
            pending = (
                unfinished(document)
                or document.status in HELD  # 批次里暂存或未导入的同一文件经这里重传: 照新上传处理
                or any(r is not None and retype(document, r) for r in (requested, wanted.get(document.id)))
            )
            if pending and all(d.id != document.id for d in fresh):
                fresh.append(document)
        if requested is not None:  # 按请求定类型先落库, 作业开始前中断也不丢 (补做按已定类型接着做)
            intake = DocumentIntake(self.deps)
            for index, document in enumerate(fresh):
                if retype(document, requested):
                    fresh[index], jobs[document.id] = await intake.mark(eid, document, requested)
                    documents = [fresh[index] if d.id == document.id else d for d in documents]
            wanted = {}
        return Uploaded(documents, fresh, jobs, wanted, requested)

    def _intake(
        self,
        eid: str,
        engagement: Engagement,
        documents: Sequence[DocumentRef],
        jobs: Mapping[str, Job],
        requested: Mapping[str, Requested],
    ) -> Callable[[], Awaitable[None]]:
        """后台逐份处理或补做文件的作业, 做完跑一次体检; jobs 为各文件定下当前类型的 classify 留痕, requested 为各文件
        上传时对应的待补交文件请求 (见 _resume_info, DocumentIntake.resume)."""

        async def process() -> None:
            await self.process_uploads(eid, engagement, documents, jobs, requested)
            await self.checkup(eid)

        return process

    async def process_uploads(
        self,
        eid: str,
        engagement: Engagement,
        documents: Sequence[DocumentRef],
        jobs: Mapping[str, Job],
        requested: Mapping[str, Requested],
    ) -> None:
        """逐份处理或补做文件 (DocumentIntake.resume; 持项目锁时调用), 不体检; 参数同 _intake. 未持项目锁抛
        RuntimeError."""
        self.require_lock(eid, "处理文件")
        intake = DocumentIntake(self.deps)
        for document in documents:
            await intake.resume(eid, document, engagement, jobs.get(document.id), requested.get(document.id))

    def _resumed(
        self, eid: str, intake: Callable[[], Awaitable[None]] | None, batch_ids: Sequence[str]
    ) -> Callable[[], Awaitable[None]]:
        """启动补做的作业: 先补做文件处理 (intake, 没有为空), 再接着识别停在 processing 的批次."""

        async def job() -> None:
            if intake is not None:
                await intake()
            for batch_id in batch_ids:
                await self.process_batch(eid, batch_id)

        return job

    async def _requested(self, eid: str, request_id: str | None) -> Requested | None:
        """上传带的 request_id 是待补交的文件请求时, 返回它的 ID 与文件类型; 不是 (没给, 不存在, 已收到, 是账表请求)
        为 None, request_id 照样记在上传留痕里."""
        if request_id is None:
            return None
        found = next((r for r in await self.deps.data.requests(eid) if r.id == request_id), None)
        if found is None or found.status != "open" or found.document_type is None:
            return None
        return Requested(found.id, found.document_type)

    async def _resume_info(
        self, eid: str, documents: Sequence[DocumentRef]
    ) -> tuple[dict[str, Job], dict[str, Requested]]:
        """补做要接上的信息, 从 data 留痕里找: (1) 已分类而没抽取完的文件 → 定下它当前类型的那条 classify 留痕 (最近
        一条同文件同类型的), 补做抽取时沿用它的 ID 作作业号, 以它的时间判断合同之后有没有被改动过 (见
        DocumentIntake.contract); (2) 各文件最近一次类型决定所对应的待补交文件请求: 有 classify 留痕的看最近一条带不带
        request_id (按请求定类型时带; 之后审计师或 A3 另定的类型不带, 以后来的为准), 没有的看上传留痕带的 request_id;
        仍为 open 的文件请求才算. 调用方再用 ingest.retype 判断这次决定是否还没落到登记上 (没落上的照样按请求定类型).
        找不到的不在其中."""
        if not documents:
            return {}, {}
        pending = {d.id: d.type.value for d in documents if d.status == "classified"}
        ids = {d.id for d in documents}
        jobs: dict[str, Job] = {}
        uploads: dict[str, str] = {}
        decided: dict[str, str | None] = {}  # 最近一条 classify 留痕带的 request_id
        for entry in await all_of_kind(self.deps.trail, eid, TrailKind.data):
            document_id = entry.payload.get("document_id")
            action = entry.payload.get("action")
            if action == "upload" and document_id in ids and entry.payload.get("request_id"):
                uploads[document_id] = str(entry.payload["request_id"])
            if action == "classify" and document_id in ids:
                found = entry.payload.get("request_id")
                decided[document_id] = str(found) if found else None
            if (
                action == "classify"
                and document_id in pending
                and entry.payload.get("document_type") == pending[document_id]
                and entry.id is not None
                and entry.created_at is not None
            ):
                jobs[document_id] = Job(entry.id, entry.created_at)  # 按写入顺序, 后写的为准
        open_types = {r.id: r.document_type for r in await self.deps.data.requests(eid) if r.status == "open"}
        latest = {doc: decided[doc] if doc in decided else uploads.get(doc) for doc in ids}
        requested = {
            doc: Requested(rid, kind)
            for doc, rid in latest.items()
            if rid is not None and (kind := open_types.get(rid)) is not None
        }
        return jobs, requested

    async def recover(self) -> set[str]:
        """应用启动时 (runner.recover 之前) 补做各项目没做完的文件处理 (见 ingest.unfinished: 只含审计师上传的文件;
        上次作业被取消或进程退出时留下; 另含按请求定类型没落到登记上的, 见 _resume_info), 处理前补写缺的上传留痕
        (upload_entry), 按请求定类型, 做完体检; 没有要补做的文件时, 体检结果已不对应当前数据的补做体检 (上次体检
        失败). 停在识别中 (processing) 的智能导入批次接着识别 (process_batch; 有要补做的文件时在同一个作业里, 排在
        其后). 项目已锁定的跳过; 项目锁正被占用的这次跳过, 等下次上传或指定类型时补做.
        补做的作业在后台进行, 做完后锁交给 runner.proceed, 按 recover 规则带动图继续 (需求 R272: 图在等程序要的
        数据时, 文件处理完才重查); 返回这些项目的编号, 由调用方交给 runner.recover(skip=...), 同一项目不重复拉起.
        一个项目出错只记日志, 不影响其他项目 (不在返回值中, 由 runner.recover 照常处理)."""
        taken: set[str] = set()
        for engagement in await self.deps.engagement_store.list():
            eid = engagement.id
            if engagement.locked or self.runner.is_running(eid):
                continue
            try:
                uploads = [
                    d
                    for d in await self.deps.financial_store.documents(eid)
                    if d.channel == "auditor_upload" and d.uploaded_by is not None
                ]
                self.runner.acquire(eid)
                try:
                    _, decided = await self._resume_info(eid, uploads)
                except BaseException:
                    self.runner.release(eid)
                    raise
                documents = [d for d in uploads if unfinished(d) or (d.id in decided and retype(d, decided[d.id]))]
                try:
                    batches = [b for b in await self.deps.data.batches(eid) if b.status == "processing"]
                except BaseException:
                    self.runner.release(eid)
                    raise
                if not documents and not batches:
                    try:
                        await self.checkup_if_stale(eid)
                    finally:
                        self.runner.release(eid)
                    continue
                labels = {d.id for d in documents} | {f.document_id for b in batches for f in b.files}
                key = self._register(eid, labels)
                try:
                    await record_transitions(self.deps.trail, [upload_entry(eid, d) for d in documents])
                    jobs, requested = await self._resume_info(eid, documents)
                except BaseException:
                    self._unregister(eid, key)
                    self.runner.release(eid)
                    raise
                intake = self._intake(eid, engagement, documents, jobs, requested) if documents else None
                self._spawn(eid, key, self._resumed(eid, intake, [b.id for b in batches]), self.runner.proceed)
                taken.add(eid)
            except Exception:
                logger.exception("补做文件处理失败: %s", eid)
        return taken

    async def set_document_type(
        self, eid: str, document_id: str, kind: DocumentType, reason: str, actor: str
    ) -> DocumentRef:
        """人工指定文件类型 (PUT /documents/{id}/type): 改类型与可靠性 (补登记过可靠性的保留, 否则取新类型的缺省,
        见 ingest.registered_reliability), 置 classified, 写 data 留痕 (action classify,
        actor 为审计师, 带理由), 返回改后的登记; 合同, 回函与 ASIC 摘录随即由 A3 在后台抽取 (同上传后的处理), 是一次
        新的作业 (作业号为这条留痕的 ID): 合同与 ASIC 摘录重抽接在已有版本之后, 不覆盖之前的抽取与修改. 理由为空
        MISSING_FIELD; 文件不存在 404; 文件还在智能导入的确认单里 (暂存, 类型在确认单上改), 或确认时没勾选而未导入
        (要导入就重新上传) INVALID_VALUE; 锁定 423; 忙 409."""
        if not reason.strip():
            raise DataRejected("MISSING_FIELD", "须填写指定类型的理由")
        document = next((d for d in await self.deps.financial_store.documents(eid) if d.id == document_id), None)
        if document is None:
            raise LookupError(f"文件 {document_id} 不存在")
        if document.status == "staged":
            raise DataRejected("INVALID_VALUE", f"文件 {document.file_name} 还在导入确认单里, 在确认单上改类型")
        if document.status == "not_imported":
            raise DataRejected("INVALID_VALUE", f"文件 {document.file_name} 没有导入, 需要时重新上传")
        engagement = await self._check_unlocked(eid)
        self.runner.acquire(eid)
        key = self._register(eid, {document_id})
        intake = DocumentIntake(self.deps)
        try:
            await self.backfill_trails(eid)
            updated, job = await self.assign_type(intake, eid, document, kind, reason.strip(), actor)
        except BaseException:
            self._unregister(eid, key)
            self.runner.release(eid)
            raise

        async def extract() -> None:
            await intake.classified(eid, updated, kind, engagement, job)
            await self.checkup(eid)

        self._spawn(eid, key, extract)
        return updated

    async def assign_type(
        self, intake: DocumentIntake, eid: str, document: DocumentRef, kind: DocumentType, reason: str, actor: str
    ) -> tuple[DocumentRef, str]:
        """人工指定类型的登记部分 (持项目锁时调用, set_document_type 与演示数据导入共用): 写 classify 留痕 (actor
        与理由), 改类型与可靠性 (ingest.registered_reliability), 置 classified, 补登记过的文件另记一版; 返回改后的登记与
        作业号 (那条留痕的 ID). 不抽取, 不体检. 未持项目锁抛 RuntimeError."""
        self.require_lock(eid, "指定文件类型")
        job = await intake.record(eid, actor, document, kind, reason)
        reliability = await registered_reliability(self.deps, eid, document, kind)
        updated = await self.deps.evidence_store.update_document(
            eid, document.id, type=kind, reliability=reliability, status="classified", failed_step=None
        )
        await record_registration(self.deps, eid, document, updated, actor, reason)
        return updated, job

    async def set_document_details(
        self, eid: str, edits: Mapping[str, DocumentDetailsEdit], actor: str
    ) -> list[DocumentRef]:
        """补登记文件的可靠性, 文件日期与关联对象 (PUT /documents/{id}/details, 需求 R90), 返回改后的登记 (按 edits 的
        顺序). 持项目锁 (锁定 423, 忙 409), 先逐份校验, 有一份不过就都不写: 文件不存在 LookupError (404); 理由去空白后
        为空, 或三项都没给 MISSING_FIELD; 暂存或未导入的文件, 给出的都与当前相同, 关联对象有空串或重复 INVALID_VALUE.
        给出的都与当前相同而当前登记正是同一人以同一理由补登记出的 (重试), 返回当前登记, 不再写.

        一个事务内: 各文件改登记 (EvidenceStore.update_details), 抽取版本表记一版 (target_type document, data 为
        details_data, field 为改了的字段按 DETAIL_FIELDS 顺序以逗号连接; 该文件第一次补登记先补记修改前的一版, 记 A3,
        field 为空), 数据版本 +1 (一批一次, 记数据变动 documents 并带这一批各文件的类型: 读该类文件登记的程序随之
        过期, 需求 R304). 提交后每份写一条 data 留痕 (edit_entry; 没写成的下次取锁时补写). 关联对象不校验是否存在."""
        async with self.locked(eid):
            await self.backfill_trails(eid)
            return await self.save_details(eid, edits, actor)

    async def save_details(self, eid: str, edits: Mapping[str, DocumentDetailsEdit], actor: str) -> list[DocumentRef]:
        """补登记的校验与写入 (持项目锁时调用, set_document_details 与演示数据导入共用), 规则同 set_document_details.
        未持项目锁抛 RuntimeError."""
        self.require_lock(eid, "补登记")
        documents = {d.id: d for d in await self.deps.financial_store.documents(eid)}
        results: dict[str, DocumentRef] = {}
        planned: list[tuple[DocumentRef, dict[str, Any], str, list[ExtractionRecord]]] = []
        for document_id, edit in edits.items():
            document = documents.get(document_id)
            if document is None:
                raise LookupError(f"文件 {document_id} 不存在")
            reason = edit.reason.strip()
            if not reason:
                raise DataRejected("MISSING_FIELD", "须填写补登记的理由")
            if document.status in HELD_NOTES:
                raise DataRejected("INVALID_VALUE", HELD_NOTES[document.status].format(document.file_name))
            given = details_given(edit, document.file_name)
            if not given:
                raise DataRejected("MISSING_FIELD", f"文件 {document.file_name} 的补登记没有给出任何一项")
            changes = {k: v for k, v in given.items() if getattr(document, k) != v}
            history = await self.deps.data.extraction_versions(eid, DETAILS, document_id)
            if changes:
                planned.append((document, changes, reason, history))
            elif details_retry(history, document, actor, reason):
                results[document_id] = document
            else:
                raise DataRejected("INVALID_VALUE", f"文件 {document.file_name} 给出的各项都与当前相同")
        written: list[ExtractionRecord] = []
        if planned:
            async with self.deps.output_store.transaction(eid):
                for document, changes, reason, history in planned:
                    if not history:  # 第一次补登记: 先补记修改前的一版
                        await self.deps.data.save_extraction(
                            eid, DETAILS, document.id, 1, details_data(document), changed_by=AgentId.A3
                        )
                    updated = await self.deps.evidence_store.update_details(
                        eid,
                        document.id,
                        reliability=changes.get("reliability"),
                        document_date=changes.get("document_date"),
                        related_ids=changes.get("related_ids"),
                    )
                    record = ExtractionRecord(
                        target_type=DETAILS,
                        target_id=document.id,
                        version=(history[-1].version if history else 1) + 1,
                        data=details_data(updated),
                        field=",".join(k for k in DETAIL_FIELDS if k in changes),
                        reason=reason,
                        changed_by=actor,
                        changed_at=datetime.now(UTC),
                    )
                    await self.deps.data.save_extraction(
                        eid,
                        DETAILS,
                        document.id,
                        record.version,
                        record.data,
                        changed_by=actor,
                        field=record.field,
                        reason=reason,
                    )
                    results[document.id] = updated
                    written.append(record)
                # 记下补登记了哪几类文件: 只让读这几类文件登记的程序过期 (需求 R304)
                kinds = [document.type for document, *_ in planned]
                await self.deps.data.bump_version(eid, DataInput.documents, document_types=kinds)
            await record_transitions(self.deps.trail, [edit_entry(eid, r) for r in written])
        return [results[document_id] for document_id in edits]

    async def document_history(self, eid: str, document_id: str) -> list[ExtractionRecord]:
        """文件登记的各版 (GET /documents/{id}/history, 需求 R90), 按版本升序; 文件不存在 404. 没补登记过的以当前登记
        作唯一一版返回 (记 A3, 时间取存档时间)."""
        document = next((d for d in await self.deps.financial_store.documents(eid) if d.id == document_id), None)
        if document is None:
            raise LookupError(f"文件 {document_id} 不存在")
        records = await self.deps.data.extraction_versions(eid, DETAILS, document_id)
        if records:
            return records
        return [
            ExtractionRecord(
                target_type=DETAILS,
                target_id=document.id,
                version=1,
                data=details_data(document),
                changed_by=AgentId.A3,
                changed_at=document.uploaded_at,
            )
        ]

    async def contract_history(self, eid: str, contract_id: str) -> list[ContractVersion]:
        """合同的各版本 (GET /contracts/{id}/history), 按版本升序; 合同不存在 404. 迁移 0004 之前抽取而没改过的
        合同没有版本记录, 以当前内容作唯一一版返回 (时间取文件登记时间)."""
        contract = await self._contract(eid, contract_id)
        history = await self.deps.financial_store.contract_history(eid, contract_id)
        if history:
            return history
        document = next(
            (d for d in await self.deps.financial_store.documents(eid) if d.id == contract.document_id), None
        )
        changed_at = document.uploaded_at if document is not None else datetime.now(UTC)
        return [
            ContractVersion(version=contract.version, contract=contract, changed_by=AgentId.A3, changed_at=changed_at)
        ]

    async def _contract(self, eid: str, contract_id: str) -> Contract:
        found = await self.deps.financial_store.contracts(eid, [contract_id])
        if not found:
            raise LookupError(f"合同 {contract_id} 不存在")
        return found[0]

    async def edit_contract(self, eid: str, contract_id: str, edit: ExtractionEdit, actor: str) -> Contract:
        """修改合同抽取结果 (PUT /contracts/{id}): 生成新版本 (version +1), 记修改人, 字段与理由, 数据版本 +1, 三者
        在一个事务内; 提交后写 data 留痕 (action edit, 见 edit_entry; 没写成的下次取锁时补写). 字段见
        extraction_edits.CONTRACT_FIELDS (条款下标从 0 起). 持项目锁 (锁定 423, 忙 409); 合同不存在 404; 理由为空
        MISSING_FIELD; 字段不认识, 下标越界, 值不合法或与当前相同 INVALID_VALUE. 同一修改的重试 (当前版本正是同一人
        以同一理由改同一字段得出的) 返回当前版本, 不再写. 迁移 0004 之前抽取的合同第一次修改时先补记修改前的那一版."""
        reason = self._reason(edit)
        async with self.locked(eid):
            await self.backfill_trails(eid)
            contract = await self._contract(eid, contract_id)
            history = await self.deps.financial_store.contract_history(eid, contract_id)
            try:
                updated = apply_contract_edit(contract, edit.field, edit.value)
            except DataRejected:
                if contract_retry(history, contract, edit, actor, reason):
                    return contract
                raise
            async with self.deps.output_store.transaction(eid):
                if not history:
                    await self.deps.financial_store.save_contract(eid, contract)  # 补记修改前的一版
                await self.deps.financial_store.save_contract(
                    eid, updated, changed_by=actor, field=edit.field, reason=reason
                )
                await self.deps.data.bump_version(eid, DataInput.contracts)
            record = ExtractionRecord(
                target_type="contract",
                target_id=updated.id,
                version=updated.version,
                data=updated.model_dump(mode="json"),
                field=edit.field,
                reason=reason,
                changed_by=actor,
                changed_at=datetime.now(UTC),
            )
            await record_transitions(self.deps.trail, [edit_entry(eid, record)])
        return updated

    async def edit_confirmation(self, eid: str, confirmation_id: str, edit: ExtractionEdit, actor: str) -> Confirmation:
        """修改回函抽取结果 (PUT /confirmations/{id}): 函证出新版本 (version +1) 写入审计产出, 抽取版本表记修改人,
        字段与理由, 数据版本 +1, 三者在一个事务内; 提交后写 data 留痕 (同 edit_contract). 字段: confirming_party,
        balance_date, confirmed_balance_cents.

        函证在编排状态里, 只在图停在闸门或等复核 (P2 的等复核, 或 P2 以外节点等人; 且没有出错的节点) 时可改,
        否则 409 NOT_EDITABLE. 改后只写 Store; 从停着的位置回到写函证的节点 (DOC-06 重跑, CSH-02) 之前都从库读回
        最新版本 (nodes.confirmations_from_store): 等复核恢复 (await_events), 过期重跑写检查点 (rerun_stale) 与其恢复
        补做; DOC-06 重跑时回函原件没换的沿用这一版, 不重抽 (IngestAgent.kept_edit). P2 以外节点等人或停在闸门而不
        重跑时, 状态中的函证比库里旧: 之后按状态函证计算的只有人工程序 owner 的整理 (需求 R93), 它给函证出的版本与
        这一版撞号时节点报错 (nodes.GraphNodes.collect, Store 已有内容不同的同版本), 不静默丢掉. CSH-02 已比对过的
        差额与结果不随之重算, 记数据变动 confirmations 标出过期 (需求 R274), 审计师可在等复核, FIN-01 或 RPT-01
        处重跑它.
        持项目锁 (锁定 423, 忙 409); 函证不存在 404; 理由为空 MISSING_FIELD; 字段或值不合法, 与当前相同
        INVALID_VALUE; 同一修改的重试返回当前版本 (同 edit_contract)."""
        reason = self._reason(edit)
        async with self.locked(eid):
            await self.backfill_trails(eid)
            confirmation = await self.deps.output_store.get(eid, Confirmation, confirmation_id)
            if confirmation is None:
                raise LookupError(f"函证 {confirmation_id} 不存在")
            snapshot = await self.runner.app.aget_state(GraphRunner.config(eid))
            broken = any(task.error for task in snapshot.tasks)
            at_gate = current_gate(snapshot.next, fill_defaults(snapshot.values)) is not None
            if broken or not (at_gate or review_waits(snapshot)):
                raise DataRejected("NOT_EDITABLE", "只能在图停在闸门或等复核时修改回函抽取结果")
            try:
                updated = apply_confirmation_edit(confirmation, edit.field, edit.value)
            except DataRejected:
                if await self._confirmation_retry(eid, confirmation, edit, actor, reason):
                    return confirmation
                raise
            record = ExtractionRecord(
                target_type="confirmation",
                target_id=updated.id,
                version=updated.version,
                data=updated.model_dump(mode="json"),
                field=edit.field,
                reason=reason,
                changed_by=actor,
                changed_at=datetime.now(UTC),
            )
            async with self.deps.output_store.transaction(eid):
                await self.deps.output_store.put(eid, [updated], actor=actor)
                await self.deps.data.save_extraction(
                    eid,
                    "confirmation",
                    updated.id,
                    updated.version,
                    record.data,
                    changed_by=actor,
                    field=edit.field,
                    reason=reason,
                )
                await self.deps.data.bump_version(eid, DataInput.confirmations)
            await record_transitions(self.deps.trail, [edit_entry(eid, record)])
        return updated

    async def _confirmation_retry(
        self, eid: str, current: Confirmation, edit: ExtractionEdit, actor: str, reason: str
    ) -> bool:
        """当前函证版本是否正是这次修改上次写入的结果: 最新一版同一人以同一理由改同一字段,
        把修改套在前一版上正得到它."""
        records = await self.deps.data.extraction_versions(eid, "confirmation", current.id)
        previous = await self.deps.output_store.get(eid, Confirmation, current.id, current.version - 1)
        if not records or previous is None or not same_edit(records[-1], current.version, edit, actor, reason):
            return False
        try:
            return apply_confirmation_edit(previous, edit.field, edit.value) == current
        except DataRejected:
            return False

    # ── ASIC 摘录的抽取结果 (需求 R290) ──

    async def registry_extracts(self, eid: str) -> list[ExtractionRecord]:
        """各份 ASIC 摘录的当前抽取结果 (GET /registry-extracts): 现在登记为 registry_extract 的文件中有抽取版本的,
        各取最新一版, 按文件 ID; 与 ACC-04 读的同一份 (adapters.registry.latest_extractions)."""
        return [record for _, record in await latest_extractions(self.deps.financial_store, self.deps.data, eid)]

    async def registry_history(self, eid: str, document_id: str) -> list[ExtractionRecord]:
        """一份 ASIC 摘录抽取结果的各版 (GET /registry-extracts/{document_id}/history), 按版本升序; 文件不是 ASIC
        摘录或还没有抽取结果 404."""
        return await self._registry_records(eid, document_id)

    async def edit_registry_extract(
        self, eid: str, document_id: str, edit: ExtractionEdit, actor: str
    ) -> ExtractionRecord:
        """修改 ASIC 摘录的抽取结果 (PUT /registry-extracts/{document_id}): 抽取版本表记新一版 (修改人, 字段与理由),
        数据版本 +1 记数据变动 registry_extracts, 一个事务内; 提交后写 data 留痕 (同 edit_contract). 已执行的 ACC-04 不
        随之过期 (承接阶段的执行不记数据版本, R274), 评分卡不自动重算; 要重跑经 ACC-01 闸门更新客户资料 (rerun).
        字段见 extraction_edits.REGISTRY_FIELDS (高管下标从 0 起). 持项目锁 (锁定 423, 忙 409); 文件不是 ASIC 摘录或还
        没有抽取结果 404; 理由为空 MISSING_FIELD; 字段不认识, 下标越界, 值不合法或与当前相同 INVALID_VALUE. 同一修改的
        重试 (最新一版正是同一人以同一理由改同一字段得出的) 返回当前版本, 不再写."""
        reason = self._reason(edit)
        async with self.locked(eid):
            await self.backfill_trails(eid)
            records = await self._registry_records(eid, document_id)
            current = records[-1]
            extraction = RegistryExtraction.model_validate(current.data)
            try:
                updated = apply_registry_edit(extraction, edit.field, edit.value)
            except DataRejected:
                if len(records) > 1 and same_edit(current, current.version, edit, actor, reason):
                    previous = RegistryExtraction.model_validate(records[-2].data)
                    try:
                        if apply_registry_edit(previous, edit.field, edit.value) == extraction:
                            return current
                    except DataRejected:
                        pass
                raise
            record = ExtractionRecord(
                target_type=REGISTRY_TARGET,
                target_id=document_id,
                version=current.version + 1,
                data=updated.model_dump(mode="json"),
                field=edit.field,
                reason=reason,
                changed_by=actor,
                changed_at=datetime.now(UTC),
            )
            async with self.deps.output_store.transaction(eid):
                await self.deps.data.save_extraction(
                    eid,
                    REGISTRY_TARGET,
                    document_id,
                    record.version,
                    record.data,
                    changed_by=actor,
                    field=edit.field,
                    reason=reason,
                )
                await self.deps.data.bump_version(eid, DataInput.registry_extracts)
            await record_transitions(self.deps.trail, [edit_entry(eid, record)])
        return record

    async def _registry_documents(self, eid: str) -> set[str]:
        """现在登记为 ASIC 摘录 (registry_extract) 的文件 ID."""
        return {d.id for d in await self.deps.financial_store.documents(eid, DocumentType.registry_extract)}

    async def _registry_records(self, eid: str, document_id: str) -> list[ExtractionRecord]:
        """一份 ASIC 摘录抽取结果的各版, 按版本升序; 文件不是 ASIC 摘录或还没有抽取结果抛 LookupError."""
        records = (
            await self.deps.data.extraction_versions(eid, REGISTRY_TARGET, document_id)
            if document_id in await self._registry_documents(eid)
            else []
        )
        if not records:
            raise LookupError(f"文件 {document_id} 不是已抽取的 ASIC 摘录")
        return records

    # ── 待审财报 (需求 R217) ──

    async def statements_history(self, eid: str) -> list[ExtractionRecord]:
        """待审财报的各版 (GET /financial-statements/history), 按版本升序; 还没有抽取过 404."""
        history = await self.deps.financial_store.statements_history(eid)
        if not history:
            raise LookupError("还没有待审财报 (没有抽取过财务报表文件)")
        return history

    async def edit_financial_statements(self, eid: str, edit: ExtractionEdit, actor: str) -> FinancialStatements:
        """修改待审财报的抽取结果 (PUT /financial-statements): 当前版本 (FinancialDataStore.financial_statements) 改一个
        字段出新一版 (版本接在已有各版之后), extraction_versions 记修改人, 字段与理由, 数据版本 +1 记数据变动
        financial_statements (读财报的程序据此标过期, 同修改合同), 一个事务内; 提交后写 data 留痕 (同 edit_contract).
        字段见 extraction_edits.STATEMENTS_FIELDS. 不受闸门限制 (财报是客户数据, 不在编排状态里). 持项目锁 (锁定 423,
        忙 409); 没有当前版本 404; 理由为空 MISSING_FIELD; 字段不认识, 没有该行, 值不合法或与当前相同 INVALID_VALUE.
        同一修改的重试 (最新一版正是同一人以同一理由改同一字段, 由前一版改得) 返回当前版本, 不再写."""
        reason = self._reason(edit)
        async with self.locked(eid):
            await self.backfill_trails(eid)
            current = await self.deps.financial_store.financial_statements(eid)
            if current is None:
                raise LookupError("还没有待审财报 (没有已抽取的财务报表文件)")
            history = await self.deps.financial_store.statements_history(eid)
            try:
                changed = apply_statements_edit(current, edit.field, edit.value)
            except DataRejected:
                if self._statements_retry(history, current, edit, actor, reason):
                    return current
                raise
            updated = changed.model_copy(update={"version": next_statements_version(history)})
            async with self.deps.output_store.transaction(eid):
                await self.deps.financial_store.save_financial_statements(
                    eid, updated, changed_by=actor, field=edit.field, reason=reason
                )
                await self.deps.data.bump_version(eid, DataInput.financial_statements)
            record = ExtractionRecord(
                target_type=FS_TARGET,
                target_id=updated.id,
                version=updated.version,
                data=updated.model_dump(mode="json"),
                field=edit.field,
                reason=reason,
                changed_by=actor,
                changed_at=datetime.now(UTC),
            )
            await record_transitions(self.deps.trail, [edit_entry(eid, record)])
        return updated

    @staticmethod
    def _statements_retry(
        history: Sequence[ExtractionRecord], current: FinancialStatements, edit: ExtractionEdit, actor: str, reason: str
    ) -> bool:
        """当前版本是否正是这次修改上次写入的结果: 最新一版即当前版本, 同一人以同一理由改同一字段, 把修改套在前一版上
        正得到它 (不比版本号)."""
        if len(history) < 2 or not same_edit(history[-1], current.version, edit, actor, reason):
            return False
        previous = FinancialStatements.model_validate(history[-2].data)
        try:
            changed = apply_statements_edit(previous, edit.field, edit.value)
        except DataRejected:
            return False
        return changed.model_copy(update={"version": current.version}) == current

    @staticmethod
    def _reason(edit: ExtractionEdit) -> str:
        if not edit.reason.strip():
            raise DataRejected("MISSING_FIELD", "须填写修改理由")
        return edit.reason.strip()
