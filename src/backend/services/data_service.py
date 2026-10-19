"""数据管理的服务 (需求 R10-R16): 数据管理接口的写操作都经这里, 取项目锁, 写存储, 记 data 留痕.

项目锁与编排共用 (GraphRunner.acquire): 同一项目同时只有一个写者, 图在后台推进时写数据报 ENGAGEMENT_BUSY.
数据写操作 (holding, launch) 结束时图若停在执行阶段等程序要的数据 (await_data), 锁交给后台恢复, 由节点重查 (需求 R272).
"新数据版本写入" 的入口是 DataService.write_tables (导入与 Xero 同步共用), 须在 holding 块内或 launch 的作业里调用.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import PurePath
from uuid import uuid4

from langgraph.types import Command
from pydantic import BaseModel

from backend.agents.base import AgentDeps
from backend.agents.checkup import DataCheckup, checkup_entries, request_entry
from backend.agents.ingest import RELIABILITY, DocumentIntake, Job, Requested, retype, unfinished
from backend.contracts.data import (
    CategoryVersion,
    ContractVersion,
    DataInput,
    DataReadiness,
    DataRequest,
    DataSource,
    DataSources,
    DataSummary,
    ExtractionEdit,
    ExtractionRecord,
    ImportCommit,
    ImportPreview,
    ImportResult,
    ImportSession,
    RecordsDetail,
    TableColumn,
    TableKind,
    TablePage,
    TableSchema,
    TableSummary,
    TableVersion,
    XeroSource,
)
from backend.contracts.errors import DataRejected
from backend.contracts.models import (
    AgentId,
    Confirmation,
    Contract,
    DocumentRef,
    DocumentType,
    Engagement,
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
    read_sheet,
    sheet_names,
)
from backend.services.data_quality import REQUIRED_TABLES, missing_tables
from backend.services.extraction_edits import apply_confirmation_edit, apply_contract_edit, ordered_terms
from backend.services.table_plan import convert
from backend.services.trail_pages import all_of_kind

logger = logging.getLogger(__name__)
TARGET_NAMES = {DocumentType.contract: "合同", DocumentType.confirmation: "回函"}  # 留痕摘要里抽取结果的名称
# 可上传的文件 (POST /documents), 单个不超过 data_import.MAX_BYTES
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".png", ".jpg", ".jpeg"}
TABLE_NAMES: dict[TableKind, str] = {  # 留痕摘要里的表名
    TableKind.accounts: "科目表",
    TableKind.trial_balance: "试算表",
    TableKind.journal_entries: "总账分录",
    TableKind.sales_invoices: "销售发票",
    TableKind.bank_accounts: "银行账户",
    TableKind.bank_transactions: "银行流水",
    TableKind.delivery_notes: "发运单",
    TableKind.approval_log: "审批日志",
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
    """一次抽取结果修改的 data 留痕: action edit, payload 带 document_type, field, reason 与文件 ID (合同取
    document_id, 函证取 reply_document_id); 防重复键 {eid}:data:edit:{类型}:{对象}:v{版本}, 写入与补写共用."""
    kind = DocumentType.contract if record.target_type == "contract" else DocumentType.confirmation
    payload: dict[str, object] = {
        "action": "edit",
        "document_type": kind.value,
        "field": record.field,
        "reason": record.reason,
    }
    document_id = record.data.get("document_id" if kind is DocumentType.contract else "reply_document_id")
    if document_id:
        payload["document_id"] = document_id
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.data,
        actor=record.changed_by,
        summary=f"修改{TARGET_NAMES[kind]} {record.target_id} 的抽取结果 {record.field}, 第 {record.version} 版",
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


class NoXeroConnections:
    """还没有 Xero 连接记录时的缺省实现 (需求 R12 前): 一律未连接."""

    async def source(self, eid: str) -> XeroSource:
        return XeroSource(status="not_connected")


class DataService:
    """数据管理的写操作与查询. 组装根建一个, 挂在 app.state.data_service."""

    def __init__(self, deps: AgentDeps, runner: GraphRunner, xero: XeroConnections | None = None) -> None:
        self.deps = deps  # 外部依赖
        self.runner = runner  # 项目锁与后台推进; 图取 runner.app
        self.xero: XeroConnections = xero if xero is not None else NoXeroConnections()  # Xero 连接记录
        # 进行中的作业 (导入, 同步, 文件处理): 项目编号 → 作业 → 作业涉及的账表或文件. 单 worker, 同 runner 的项目锁
        self.jobs: dict[str, dict[object, frozenset[str]]] = {}
        self.tasks: set[asyncio.Task[None]] = set()  # launch 起的后台作业; 保留引用, 防止完成前被回收

    async def _check_unlocked(self, eid: str) -> Engagement:
        """项目 (未锁定); 不存在抛 LookupError, 已锁定 ENGAGEMENT_LOCKED."""
        engagement = await self.deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(f"Engagement {eid} not found")
        if engagement.locked:
            raise DataRejected("ENGAGEMENT_LOCKED", eid)
        return engagement

    def _register(self, eid: str, labels: Iterable[str]) -> object:
        key = object()
        self.jobs.setdefault(eid, {})[key] = frozenset(labels)
        return key

    def _unregister(self, eid: str, key: object) -> None:
        jobs = self.jobs.get(eid, {})
        jobs.pop(key, None)
        if not jobs:
            self.jobs.pop(eid, None)

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
    async def holding(self, eid: str, labels: Iterable[str]) -> AsyncIterator[None]:
        """在块内做一件数据写操作: 进入时查锁定 (ENGAGEMENT_LOCKED), 取项目锁 (取不到 ENGAGEMENT_BUSY), 把 labels
        (涉及的账表种类或文件 ID) 登记为进行中的作业, 补写以前没写成的 data 留痕 (backfill_trails); 离开时注销,
        成功时锁交给 continue_waiting (图在等程序要的数据就交给后台恢复, 否则释放), 出错时释放. 数据提交在注销之前,
        概览据此判断."""
        await self._check_unlocked(eid)
        self.runner.acquire(eid)
        key = self._register(eid, labels)
        try:
            await self.backfill_trails(eid)
            yield
        except BaseException:
            self._unregister(eid, key)
            self.runner.release(eid)
            raise
        self._unregister(eid, key)
        await self.continue_waiting(eid)

    async def continue_waiting(self, eid: str) -> None:
        """持项目锁的数据写操作结束时调用, 锁由此交出或释放, 调用方不再管锁: 图停在 await_data 等程序要的数据
        (需求 R272) 时交给后台, 以 Command(resume=INPUTS_CHANGED) 恢复, 由节点按现有资料重查, 后台任务结束时释放;
        否则释放. 读检查点出错只记日志, 释放; 读检查点时被取消 (请求取消, 应用退出) 也释放, 再照常抛出; 后台任务
        建不起来时 runner.launch 已释放锁, 只记日志."""
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

    async def launch(self, eid: str, labels: Iterable[str], job: Callable[[], Awaitable[None]]) -> None:
        """在后台做一件数据写操作 (如 Xero 同步, 文件处理): 在调用方这里查锁定, 取项目锁并登记作业 (不过立即抛出,
        同 holding), 后台补写缺的留痕后执行 job; job 结束 (成败) 时注销, 锁交给 continue_waiting (见 _spawn). job
        自己处理业务失败 (如把文件置 needs_manual); 漏出的异常只记日志."""
        names = frozenset(labels)
        await self._check_unlocked(eid)
        self.runner.acquire(eid)
        key = self._register(eid, names)

        async def run() -> None:
            await self.backfill_trails(eid)
            await job()

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
        """补写已提交而 data 留痕没写成的账表版本, 抽取结果修改, 最近一次体检与补交请求的提出和收到 (写入后留痕
        失败, 或进程在两者之间中断); 同键已有的跳过. 数据集载入的版本不记留痕. 在取得项目锁之后调用."""
        versions = [v for v in await self.deps.data.versions(eid) if v.source != "dataset"]
        edits = [r for r in await self.deps.data.extraction_versions(eid) if r.field is not None]
        entries = [version_entry(eid, v) for v in versions] + [edit_entry(eid, r) for r in edits]
        entries += checkup_entries(eid, await self.deps.data.check_run(eid), await self.deps.data.requests(eid))
        for code, history in (await self.deps.data.category_history(eid)).items():
            entries += [category_entry(eid, code, v) for v in history if v.basis == "auditor"]
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
    ) -> int:
        """新数据版本写入 (导入与 Xero 同步共用), 返回写入后的数据版本. 须在 holding 块内或 launch 的作业里调用
        (持项目锁), 否则抛 RuntimeError. 写入规则见 DataStore.write_tables; 提交后每张表写一条 data 留痕
        (见 version_entry). 留痕写失败时数据已提交: 下次取项目锁做数据写操作时补写 (见 backfill_trails)."""
        if not self.runner.is_running(eid):
            raise RuntimeError(f"{eid} 写入账表须先取项目锁 (DataService.holding 或 launch)")
        version = await self.deps.data.write_tables(
            eid,
            tables,
            source=source,
            actor=actor,
            file_name=file_name,
            document_id=document_id,
            import_id=import_id,
            request_id=request_id,
        )
        written = [
            TableVersion(
                id=import_id or "",
                kind=kind,
                source=source,
                file_name=file_name,
                document_id=document_id,
                records=len(tables[kind]),
                data_version=version,
                request_id=request_id,
                created_by=actor,
                created_at=datetime.now(UTC),
            )
            for kind in sorted(tables, key=list(TableKind).index)
        ]
        try:
            await record_transitions(self.deps.trail, [version_entry(eid, item) for item in written])
        finally:  # 留痕写失败 (数据已提交) 也照常体检
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
        )

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
            requested = await self._requested(eid, request_id)
            documents: list[DocumentRef] = []
            fresh: list[DocumentRef] = []
            for name, (_, data) in zip(names, files, strict=True):
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
                pending = unfinished(document) or any(
                    r is not None and retype(document, r) for r in (requested, wanted.get(document.id))
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
            self.jobs[eid][key] = frozenset(d.id for d in fresh)
        except BaseException:
            self._unregister(eid, key)
            self.runner.release(eid)
            raise
        if not fresh and requested is None:  # 都是已处理完的文件: 不再处理
            self._unregister(eid, key)
            self.runner.release(eid)
            return documents
        self._spawn(eid, key, self._intake(eid, engagement, fresh, jobs, wanted))
        return documents

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
            intake = DocumentIntake(self.deps)
            for document in documents:
                await intake.resume(eid, document, engagement, jobs.get(document.id), requested.get(document.id))
            await self.checkup(eid)

        return process

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
        失败). 项目已锁定的跳过; 项目锁正被占用的这次跳过, 等下次上传或指定类型时补做.
        补做的文件处理在后台进行, 做完后锁交给 runner.proceed, 按 recover 规则带动图继续 (需求 R272: 图在等程序要的
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
                if not documents:
                    try:
                        await self.checkup_if_stale(eid)
                    finally:
                        self.runner.release(eid)
                    continue
                key = self._register(eid, (d.id for d in documents))
                try:
                    await record_transitions(self.deps.trail, [upload_entry(eid, d) for d in documents])
                    jobs, requested = await self._resume_info(eid, documents)
                except BaseException:
                    self._unregister(eid, key)
                    self.runner.release(eid)
                    raise
                self._spawn(eid, key, self._intake(eid, engagement, documents, jobs, requested), self.runner.proceed)
                taken.add(eid)
            except Exception:
                logger.exception("补做文件处理失败: %s", eid)
        return taken

    async def set_document_type(
        self, eid: str, document_id: str, kind: DocumentType, reason: str, actor: str
    ) -> DocumentRef:
        """人工指定文件类型 (PUT /documents/{id}/type): 改类型与可靠性, 置 classified, 写 data 留痕 (action classify,
        actor 为审计师, 带理由), 返回改后的登记; 合同与回函随即由 A3 在后台抽取 (同上传后的处理), 是一次新的作业
        (作业号为这条留痕的 ID): 合同重抽接在已有版本之后, 不覆盖之前的抽取与修改. 理由为空 MISSING_FIELD;
        文件不存在 404; 锁定 423; 忙 409."""
        if not reason.strip():
            raise DataRejected("MISSING_FIELD", "须填写指定类型的理由")
        document = next((d for d in await self.deps.financial_store.documents(eid) if d.id == document_id), None)
        if document is None:
            raise LookupError(f"文件 {document_id} 不存在")
        engagement = await self._check_unlocked(eid)
        self.runner.acquire(eid)
        key = self._register(eid, {document_id})
        intake = DocumentIntake(self.deps)
        try:
            await self.backfill_trails(eid)
            job = await intake.record(eid, actor, document, kind, reason.strip())
            updated = await self.deps.evidence_store.update_document(
                eid, document_id, type=kind, reliability=RELIABILITY[kind], status="classified", failed_step=None
            )
        except BaseException:
            self._unregister(eid, key)
            self.runner.release(eid)
            raise

        async def extract() -> None:
            await intake.classified(eid, updated, kind, engagement, job)
            await self.checkup(eid)

        self._spawn(eid, key, extract)
        return updated

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
        重跑时, 状态中的函证比库里旧; 但之后不再有写函证或按状态函证计算的节点, 不影响结果. CSH-02 已比对过的差额与
        结果不随之重算, 记数据变动 confirmations 标出过期 (需求 R274), 审计师可在等复核, FIN-01 或 RPT-01 处重跑它.
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

    @staticmethod
    def _reason(edit: ExtractionEdit) -> str:
        if not edit.reason.strip():
            raise DataRejected("MISSING_FIELD", "须填写修改理由")
        return edit.reason.strip()
