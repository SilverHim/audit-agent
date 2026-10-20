"""A3 的数据体检与补交请求 (需求 R15, R272).

每次账表写入 (导入, Xero 同步), 上传文件处理完, 人工指定类型后, P1 等数据时, 以及有程序开始等数据时, A3 跑一次
体检: 结果存库 (data_checks 只留最近一次) 并写 data 留痕; 按体检结果与程序要的资料提出补交请求, 资料补齐的请求
转 received. 规则在 services.data_quality, 这里只读汇总, 写存储与留痕.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from backend.agents.base import AgentDeps
from backend.contracts.data import CheckId, CheckRun, DataRequest, TableKind
from backend.contracts.models import (
    AgentId,
    DocumentRef,
    DocumentType,
    Procedure,
    ProcedureStatus,
    TrailEntry,
    TrailKind,
)
from backend.contracts.records import DatasetTraits
from backend.orchestration.trail import record_transitions
from backend.services.data_quality import (
    OnHand,
    merge_requests,
    on_hand,
    program_requests,
    reconcile,
    run_checks,
    with_requested,
)

STATEMENT_READY = frozenset({"classified", "extracted"})  # 财务报表文件算已提供的状态 (分类完成)


def statement_provided(document: DocumentRef) -> bool:
    """一份 financial_statements 类文件算不算已提供: 分类完成 (STATEMENT_READY), 或类型已定而抽取没通过转了人工
    (needs_manual, failed_step extract; 需求 R217 起 PDF 报表上传后要抽取): 客户已交, 抽取由审计师处理, 不再向客户要."""
    return document.status in STATEMENT_READY or (
        document.status == "needs_manual" and document.failed_step == "extract"
    )


# 还没执行完的程序状态: 这些程序 inputs 中还没有的资料才去要 (需求 R272)
UNDONE = frozenset({ProcedureStatus.planned, ProcedureStatus.executing, ProcedureStatus.awaiting_data})


async def resources(deps: AgentDeps, eid: str) -> OnHand:
    """项目现有的资料 (DataStore.stats 与 document_counts) 与标了客户无此类资料的 (DataStore.requests), 口径见
    data_quality.on_hand; 程序等数据的判断, 程序要的补交请求与请求的对照共用."""
    return on_hand(await deps.data.stats(eid), await deps.data.document_counts(eid), await deps.data.requests(eid))


def stamp(moment: datetime) -> str:
    """留痕防重复键里的时间: 统一成 UTC 的 ISO 写法 (库里读回的时区可能不同)."""
    return moment.astimezone(UTC).isoformat()


def check_entry(eid: str, run: CheckRun) -> TrailEntry:
    """一次体检的 data 留痕: action check, payload 带数据版本与各项结果; 防重复键 {eid}:data:check:{体检时间}."""
    passed = sum(c.status == "passed" for c in run.checks)
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.data,
        actor=AgentId.A3,
        summary=f"数据体检: {len(run.checks)} 项中 {passed} 项通过",
        idempotency_key=f"{eid}:data:check:{stamp(run.checked_at)}",
        payload={
            "action": "check",
            "data_version": run.data_version,
            "results": {c.id.value: c.status for c in run.checks},
        },
        created_at=run.checked_at,
    )


# 补交请求留痕的动作 → 摘要的写法 (请求的标题接在后面)
REQUEST_ACTIONS = {
    "request_raised": "需要客户补交",  # 提出或重新打开
    "request_received": "已收到",  # 资料补齐
    "request_withdrawn": "不再需要",  # 要它的程序都不再执行, 资料又没到 (需求 R280)
    "request_unavailable": "客户无此类资料: ",  # 审计师标记 (需求 R280)
}


def request_entry(eid: str, request: DataRequest, action: str) -> TrailEntry:
    """补交请求的 data 留痕, action 见 REQUEST_ACTIONS; payload 带 request_id 与 table_kind 或 document_type, 提出时
    另带 reason_code, reason_params; 客户无此类资料时另带 reason 与 used_for, actor 为标记的审计师 (其余为 A3).
    防重复键 {eid}:data:{action}:{请求 ID}:{提出或转出的时间}."""
    raised = action == "request_raised"
    moment = request.created_at if raised else request.resolved_at
    assert moment is not None  # 已转出的请求都有 resolved_at
    subject = (
        {"table_kind": request.table_kind.value}
        if request.table_kind is not None
        else {"document_type": request.document_type.value if request.document_type is not None else None}
    )
    payload: dict[str, object] = {"action": action, "request_id": request.id, **subject}
    if raised:
        payload |= {"reason_code": request.reason_code, "reason_params": request.reason_params}
    unavailable = action == "request_unavailable"
    if unavailable:
        payload |= {"reason": request.unavailable_reason, "used_for": request.used_for}
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.data,
        actor=(request.resolved_by or AgentId.A3) if unavailable else AgentId.A3,
        summary=f"{REQUEST_ACTIONS[action]}{request.title}",
        idempotency_key=f"{eid}:data:{action}:{request.id}:{stamp(moment)}",
        payload=payload,
        created_at=moment,
    )


def checkup_entries(eid: str, run: CheckRun | None, requests: Sequence[DataRequest]) -> list[TrailEntry]:
    """补写用: 最近一次体检, 各请求最近一次提出, 已收到, 已撤回与客户无此类资料的最近一次转出. 按时间排."""
    entries = [check_entry(eid, run)] if run is not None else []
    for request in requests:
        entries.append(request_entry(eid, request, "request_raised"))
        if request.status != "open":
            entries.append(request_entry(eid, request, f"request_{request.status}"))
    return sorted(entries, key=lambda e: e.created_at or datetime.min.replace(tzinfo=UTC))


class DataCheckup:
    """A3 跑一次体检并处理补交请求. 调用方持项目锁 (数据写操作的作业里, 或图的节点里)."""

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps  # 外部依赖

    async def run(self, eid: str) -> CheckRun:
        """体检 (services.data_quality.run_checks), 并上程序要的请求 (program_requests), 对照已有请求 (reconcile),
        一个事务内存下体检结果与请求的变化, 提交后写留痕: 体检一条, 每个新提出 (或重新打开), 转 received 与
        转 withdrawn 的请求各一条. 留痕没写成的由下次取锁时的 DataService.backfill_trails 补写. 分录与试算表一项不计
        审计更正分录 (凭证号前缀取数据集声明 audit_adjustment_entry_prefix, 没有数据集取缺省, 需求 R271).
        项目不存在抛 LookupError."""
        engagement = await self.deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(f"Engagement {eid} not found")
        readiness = await self.deps.data.readiness(eid)
        dataset = engagement.dataset
        traits = self.deps.datasets.dataset_traits(dataset) if dataset is not None else DatasetTraits()
        inputs = await self.deps.data.check_inputs(
            eid, engagement.period_start, engagement.period_end, adjustment_prefix=traits.audit_adjustment_entry_prefix
        )
        journals = [v for v in await self.deps.data.versions(eid) if v.kind is TableKind.journal_entries]
        statements = await self.statements(eid)
        now = datetime.now(UTC)
        result = run_checks(inputs, statements=statements, journal_version=journals[-1] if journals else None, now=now)
        have = await resources(self.deps, eid)
        wanted = merge_requests(result.requests, await self.program_requests(eid, have, now))
        changes = reconcile(await self.deps.data.requests(eid), wanted, now, have)
        run = CheckRun(data_version=readiness.data_version, checked_at=now, checks=result.checks)
        await self.deps.data.save_check_run(eid, run, changes.saved)
        entries = [check_entry(eid, run)]
        entries += [request_entry(eid, r, "request_raised") for r in changes.raised]
        entries += [request_entry(eid, r, "request_received") for r in changes.received]
        entries += [request_entry(eid, r, "request_withdrawn") for r in changes.withdrawn]
        await record_transitions(self.deps.trail, entries)
        return run

    async def program_requests(self, eid: str, have: OnHand, now: datetime) -> list[DataRequest]:
        """程序要的补交请求 (需求 R272): 程序包 (Store 最新版本) 中还没执行完的程序声明而现在还没有的资料
        (data_quality.program_requests), 等数据的程序另并上它记下的请求所要的资料 (with_requested: ACC-04 等 ASIC
        摘录, 需求 R290); have 为现有的资料 (resources)."""
        procedures = await self.deps.output_store.latest(eid, Procedure)
        library = self.deps.library
        needs = {
            p.id: with_requested(library.load(p.id).inputs, p.data_request_ids)
            if p.status is ProcedureStatus.awaiting_data
            else library.load(p.id).inputs
            for p in procedures
            if p.status in UNDONE
        }
        return program_requests(needs, have, now)

    async def statements(self, eid: str) -> int:
        """已提供的财务报表文件数 (类型 financial_statements, 见 statement_provided)."""
        documents = await self.deps.financial_store.documents(eid, DocumentType.financial_statements)
        return sum(statement_provided(d) for d in documents)

    async def run_if_stale(self, eid: str) -> CheckRun | None:
        """最近一次体检已不对应当前数据时跑一次, 否则不跑, 返回 None: 没体检过, 之后又写过数据 (数据版本变了), 或已
        提供的报表草稿文件数变了 (文件上传与改类型不改数据版本; 体检只有报表草稿一项看文件). 用于 P1 等数据节点 (恢复
        与重放不重复体检), 启动补做与已提交导入的重试 (上次体检失败或被跳过时补做)."""
        run = await self.deps.data.check_run(eid)
        if run is not None and run.data_version == (await self.deps.data.readiness(eid)).data_version:
            draft = next((c for c in run.checks if c.id is CheckId.tb_agrees_statements), None)
            recorded = draft.params.get("statements") if draft is not None else None
            if recorded == await self.statements(eid):
                return None
        return await self.run(eid)
