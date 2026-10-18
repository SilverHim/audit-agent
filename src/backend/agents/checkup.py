"""A3 的数据体检与补交请求 (需求 R15).

每次账表写入 (导入, Xero 同步), 上传文件处理完, 人工指定类型后, 以及 P1 等数据时, A3 跑一次体检: 结果存库
(data_checks 只留最近一次) 并写 data 留痕; 按结果提出补交请求, 资料补齐的请求转 received. 规则在
services.data_quality, 这里只读汇总, 写存储与留痕.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from backend.agents.base import AgentDeps
from backend.contracts.data import CheckId, CheckRun, DataRequest, TableKind
from backend.contracts.models import AgentId, DocumentType, TrailEntry, TrailKind
from backend.orchestration.trail import record_transitions
from backend.services.data_quality import reconcile, run_checks

STATEMENT_READY = frozenset({"classified", "extracted"})  # 财务报表文件算已提供的状态 (分类完成)


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


def request_entry(eid: str, request: DataRequest, action: str) -> TrailEntry:
    """补交请求的 data 留痕: action 为 request_raised (提出或重新打开) 或 request_received (资料补齐);
    payload 带 request_id 与 table_kind 或 document_type, 提出时另带 reason_code, reason_params.
    防重复键 {eid}:data:{action}:{请求 ID}:{提出或收到的时间}."""
    raised = action == "request_raised"
    moment = request.created_at if raised else request.resolved_at
    assert moment is not None  # 已收到的请求都有 resolved_at
    subject = (
        {"table_kind": request.table_kind.value}
        if request.table_kind is not None
        else {"document_type": request.document_type.value if request.document_type is not None else None}
    )
    payload: dict[str, object] = {"action": action, "request_id": request.id, **subject}
    if raised:
        payload |= {"reason_code": request.reason_code, "reason_params": request.reason_params}
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.data,
        actor=AgentId.A3,
        summary=f"需要客户补交{request.title}" if raised else f"已收到{request.title}",
        idempotency_key=f"{eid}:data:{action}:{request.id}:{stamp(moment)}",
        payload=payload,
        created_at=moment,
    )


def checkup_entries(eid: str, run: CheckRun | None, requests: Sequence[DataRequest]) -> list[TrailEntry]:
    """补写用: 最近一次体检, 各请求最近一次提出, 已收到的最近一次收到. 按时间排."""
    entries = [check_entry(eid, run)] if run is not None else []
    for request in requests:
        entries.append(request_entry(eid, request, "request_raised"))
        if request.status == "received":
            entries.append(request_entry(eid, request, "request_received"))
    return sorted(entries, key=lambda e: e.created_at or datetime.min.replace(tzinfo=UTC))


class DataCheckup:
    """A3 跑一次体检并处理补交请求. 调用方持项目锁 (数据写操作的作业里, 或图的节点里)."""

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps  # 外部依赖

    async def run(self, eid: str) -> CheckRun:
        """体检 (services.data_quality.run_checks), 对照已有请求 (reconcile), 一个事务内存下体检结果与请求的变化,
        提交后写留痕: 体检一条, 每个新提出 (或重新打开) 与转 received 的请求各一条. 留痕没写成的由下次取锁时的
        DataService.backfill_trails 补写. 项目不存在抛 LookupError."""
        engagement = await self.deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(f"Engagement {eid} not found")
        readiness = await self.deps.data.readiness(eid)
        inputs = await self.deps.data.check_inputs(eid, engagement.period_start, engagement.period_end)
        journals = [v for v in await self.deps.data.versions(eid) if v.kind is TableKind.journal_entries]
        statements = await self.statements(eid)
        now = datetime.now(UTC)
        result = run_checks(inputs, statements=statements, journal_version=journals[-1] if journals else None, now=now)
        changes = reconcile(await self.deps.data.requests(eid), result.requests, now)
        run = CheckRun(data_version=readiness.data_version, checked_at=now, checks=result.checks)
        await self.deps.data.save_check_run(eid, run, changes.saved)
        entries = [check_entry(eid, run)]
        entries += [request_entry(eid, r, "request_raised") for r in changes.raised]
        entries += [request_entry(eid, r, "request_received") for r in changes.received]
        await record_transitions(self.deps.trail, entries)
        return run

    async def statements(self, eid: str) -> int:
        """已提供的财务报表文件数 (类型 financial_statements, 分类完成)."""
        documents = await self.deps.financial_store.documents(eid, DocumentType.financial_statements)
        return sum(d.status in STATEMENT_READY for d in documents)

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
