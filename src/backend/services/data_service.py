"""数据管理的服务 (需求 R10-R16): 数据管理接口的写操作都经这里, 取项目锁, 写存储, 记 data 留痕.

项目锁与编排共用 (GraphRunner.acquire): 同一项目同时只有一个写者, 图在后台推进时写数据报 ENGAGEMENT_BUSY.
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
from backend.contracts.data import (
    DataReadiness,
    DataSource,
    DataSources,
    DataSummary,
    ImportCommit,
    ImportPreview,
    ImportResult,
    ImportSession,
    RecordsDetail,
    TableKind,
    TableSchema,
    TableSummary,
    TableVersion,
    XeroSource,
)
from backend.contracts.errors import DataRejected
from backend.contracts.models import DocumentType, Reliability, TrailEntry, TrailKind
from backend.contracts.ports import XeroConnections
from backend.contracts.records import Account
from backend.orchestration.nodes import DATA_READY
from backend.orchestration.runner import GraphRunner, awaiting_data
from backend.orchestration.trail import data_ready_entry, record_transitions
from backend.services.account_mapping import with_category
from backend.services.data_import import (
    MAX_FAILURES,
    SCHEMAS,
    MappingError,
    UnreadableFile,
    convert,
    media_type,
    preview,
    read_sheet,
    sheet_names,
)
from backend.services.data_quality import missing_tables

logger = logging.getLogger(__name__)
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

    async def _check_unlocked(self, eid: str) -> None:
        engagement = await self.deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(f"Engagement {eid} not found")
        if engagement.locked:
            raise DataRejected("ENGAGEMENT_LOCKED", eid)

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
        (涉及的账表种类或文件 ID) 登记为进行中的作业, 补写以前没写成的 data 留痕 (backfill_trails); 离开时 (成败)
        注销并释放锁. 数据提交在注销之前, 概览据此判断."""
        await self._check_unlocked(eid)
        self.runner.acquire(eid)
        key = self._register(eid, labels)
        try:
            await self.backfill_trails(eid)
            yield
        finally:
            self._unregister(eid, key)
            self.runner.release(eid)

    async def launch(self, eid: str, labels: Iterable[str], job: Callable[[], Awaitable[None]]) -> None:
        """在后台做一件数据写操作 (如 Xero 同步, 文件处理): 在调用方这里查锁定, 取项目锁并登记作业 (不过立即抛出,
        同 holding), 后台执行 job; job 结束 (成败) 时注销并释放锁. job 自己处理业务失败 (如把文件置 needs_manual);
        漏出的异常只记日志."""
        names = frozenset(labels)
        await self._check_unlocked(eid)
        self.runner.acquire(eid)
        key = self._register(eid, names)
        done = False

        def finish(_: object = None) -> None:
            """注销作业并释放锁, 只做一次: 作业结束时由任务的完成回调调用, 任务还没开始就被取消也会调用."""
            nonlocal done
            if not done:
                done = True
                self._unregister(eid, key)
                self.runner.release(eid)

        async def run() -> None:
            try:
                await self.backfill_trails(eid)
                await job()
            except Exception:
                logger.exception("数据作业失败: %s %s", eid, sorted(names))

        try:
            task = asyncio.create_task(run())
        except BaseException:
            finish()
            raise
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(finish)

    async def backfill_trails(self, eid: str) -> None:
        """补写已提交而 data 留痕没写成的账表版本 (写入后留痕失败, 或进程在两者之间中断); 同键已有的跳过.
        数据集载入的版本不记留痕. 在取得项目锁之后调用."""
        versions = [v for v in await self.deps.data.versions(eid) if v.source != "dataset"]
        await record_transitions(self.deps.trail, [version_entry(eid, v) for v in versions])

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
        await record_transitions(self.deps.trail, [version_entry(eid, item) for item in written])
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
        工作表不存在 INVALID_VALUE, 对应不可用 INVALID_VALUE 或 MISSING_FIELD (见 data_import.check_mapping),
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
        """数据管理页的概览 (GET /data/summary): Xero 连接, 各账表, 最近一次体检, 进行中的作业数, 待补交请求数.

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
            processing=len(jobs),
            open_requests=await data.open_requests(eid),
        )

    async def confirm_ready(self, eid: str, actor: str) -> DataReadiness:
        """审计师确认数据已齐 (POST /data/ready): 越过 P1 开始前的等待, 后台继续 DOC-04 及后续节点.

        按顺序校验, 第一条不过即拒绝 (DataRejected): 项目锁定 ENGAGEMENT_LOCKED; 取项目锁失败 ENGAGEMENT_BUSY;
        图不在等数据就绪 NOT_AWAITING_DATA; 就绪条件不满足 DATA_NOT_READY (附 missing_tables).
        通过后记确认 (已确认的保留第一次的人与时间), 写 data 留痕 (action ready, 同键已有的跳过), 以
        Command(resume=DATA_READY) 在后台恢复; 项目锁交给后台任务. 确认已记而图仍在等 (上次留痕或恢复没成) 时
        再确认一次照常补写留痕并恢复; recover 与 resume 也会补做 (GraphRunner._proceed).
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
            readiness = await self.deps.data.confirm_ready(eid, actor)
            await record_transitions(self.deps.trail, [data_ready_entry(eid, readiness)])
        except BaseException:
            self.runner.release(eid)  # 后台任务尚未启动, 须在此释放
            raise
        self.runner.launch(eid, Command(resume=DATA_READY))
        return readiness
