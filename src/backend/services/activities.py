"""系统正在做的事 (需求 R361): 数据作业与程序执行都登记在 ActivityBoard, 项目总览只读它 (EngagementOverview.activities,
last_failure; PhaseProgress.running 也取它).

- 数据作业: DataService 登记作业时开始一项 (_register), 注销时结束 (_unregister); 逐份处理时推进 done, total 与当前
  文件名 (DataService.progress); 后台作业漏出异常, 批次识别出错, Xero 同步失败时记最近一次失败 (fail).
- 程序执行: 节点真正调 Agent 时开始一项 (键为执行 ID, 写开始的 run 留痕的同一处), 写结束的 run 留痕时结束; 后台任务
  结束时 (GraphRunner) 清掉该项目残留的程序执行 (节点出错没走到结束留痕).

当前活动只在内存中, 进程重启后为空; 配置 trail 后数据作业步骤同时持久化, 供执行过程重放.
单 worker, 同项目锁. 组装根建一个, 挂在 AgentDeps.activities.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from backend.contracts.models import AgentId, TrailEntry, TrailKind
from backend.contracts.ports import AuditTrail
from backend.contracts.views import Activity, ActivityActor, ActivityFailure, ActivityKind, RetryKind

# 数据作业各种类的执行者与缺省描述; 程序执行另给 (执行者为模板 owner, 描述含程序名与进度)
DATA_JOBS: dict[ActivityKind, tuple[ActivityActor, str]] = {
    "demo_import": (AgentId.A3, "载入演示资料"),
    "document_intake": (AgentId.A3, "识别与抽取上传的文件"),
    "import_batch": (AgentId.A3, "识别智能导入的文件"),
    "table_import": ("system", "导入账表"),
    "xero_sync": ("system", "从 Xero 同步账表"),
    "data_edit": ("system", "保存数据修改"),
}
# 失败时现成的重试入口 (RetryKind), 按作业种类; 没有的不在其中
RETRY: dict[ActivityKind, RetryKind] = {
    "demo_import": "demo_import",
    "import_batch": "import_batch",
    "xero_sync": "xero_sync",
}


def failure_reason(exc: BaseException) -> str:
    """作业出错的原因原文: 异常类型与消息, 截断到 300 字 (智能导入批次的 error 同此)."""
    return f"{type(exc).__name__}: {exc}"[:300]


class ActivityBoard:
    """各项目正在进行的活动与最近一次失败的数据作业. 键由登记方给 (数据作业为 DataService 的作业键, 程序执行为执行
    ID), 同键再开始即替换."""

    def __init__(self, now: Callable[[], datetime] | None = None, *, trail: AuditTrail | None = None) -> None:
        self.trail = trail
        self.writes: dict[str, asyncio.Task[None]] = {}
        self.event_ids: dict[tuple[str, object], str] = {}
        self.failed_keys: set[tuple[str, object]] = set()
        self.now = now or (lambda: datetime.now(UTC))
        self.active: dict[str, dict[object, Activity]] = {}  # 项目编号 → 键 → 活动, 按开始的先后
        self.failures: dict[str, ActivityFailure] = {}  # 项目编号 → 最近一次失败的数据作业

    def _record(self, eid: str, key: object, status: str) -> None:
        activity = self.get(eid, key)
        if self.trail is None or activity is None or activity.kind == "procedure":
            return
        identity = (eid, key)
        event_id = self.event_ids.setdefault(identity, f"ACT-{uuid4()}")
        entry = TrailEntry(
            engagement_id=eid,
            kind=TrailKind.execution,
            actor=activity.actor,
            run_id=event_id,
            summary=f"{activity.kind}: {status}",
            payload={
                "step_id": event_id,
                "stage": "data",
                "job": activity.kind,
                "status": status,
                "started_at": activity.started_at.isoformat(),
                "current": activity.current,
                "done": activity.done,
                "total": activity.total,
                "elapsed_ms": int((self.now() - activity.started_at).total_seconds() * 1000),
            },
        )
        previous = self.writes.get(eid)
        trail = self.trail

        async def persist() -> None:
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            try:
                await trail.record(entry)
            except Exception:
                logging.getLogger(__name__).exception("数据作业执行事件写入失败: %s", eid)

        task = asyncio.create_task(persist())
        self.writes[eid] = task

        def done(finished: asyncio.Task[None]) -> None:
            if self.writes.get(eid) is finished:
                del self.writes[eid]

        task.add_done_callback(done)

    async def settle(self) -> None:
        if self.writes:
            await asyncio.gather(*list(self.writes.values()))

    def start(
        self,
        eid: str,
        key: object,
        kind: ActivityKind,
        *,
        actor: ActivityActor | None = None,
        description: str | None = None,
        done: int | None = None,
        total: int | None = None,
        current: str | None = None,
        subject_id: str | None = None,
    ) -> None:
        """开始一项活动; 执行者与描述没给时取 DATA_JOBS. 同种作业此前的失败随之清除 (重试或重做)."""
        default_actor, default_description = DATA_JOBS.get(kind, ("system", kind))
        at = self.now()
        self.active.setdefault(eid, {})[key] = Activity(
            kind=kind,
            actor=actor or default_actor,
            description=description or default_description,
            done=done,
            total=total,
            current=current,
            subject_id=subject_id,
            started_at=at,
            updated_at=at,
        )
        self._record(eid, key, "running")
        failure = self.failures.get(eid)
        if failure is not None and failure.kind == kind:
            del self.failures[eid]

    def update(self, eid: str, key: object, **changes: object) -> None:
        """改一项活动的字段 (如 done, total, current, kind, description); 进度 (done, total, current) 变了时
        updated_at 为现在. 没有这一项时不做什么."""
        activity = self.active.get(eid, {}).get(key)
        if activity is None:
            return
        updated = activity.model_copy(update=changes)
        if (updated.done, updated.total, updated.current) != (activity.done, activity.total, activity.current):
            updated = updated.model_copy(update={"updated_at": self.now()})
        self.active[eid][key] = updated
        if updated != activity:
            self._record(eid, key, "running")

    def get(self, eid: str, key: object) -> Activity | None:
        return self.active.get(eid, {}).get(key)

    def finish(self, eid: str, key: object) -> None:
        """结束一项活动; 没有这一项时不做什么."""
        self._record(eid, key, "failed" if (eid, key) in self.failed_keys else "completed")
        self.failed_keys.discard((eid, key))
        self.event_ids.pop((eid, key), None)
        found = self.active.get(eid, {})
        found.pop(key, None)
        if not found:
            self.active.pop(eid, None)

    def clear(self, eid: str, kind: ActivityKind) -> None:
        """结束该项目 kind 类的全部活动 (后台推进结束时清掉残留的程序执行)."""
        for key, activity in list(self.active.get(eid, {}).items()):
            if activity.kind == kind:
                self.finish(eid, key)

    def fail(
        self,
        eid: str,
        kind: ActivityKind,
        reason: str,
        *,
        actor: ActivityActor | None = None,
        subject_id: str | None = None,
    ) -> None:
        """记最近一次失败的数据作业, 取代此前的; 执行者没给时取 DATA_JOBS, 重试入口按种类 (RETRY)."""
        for key, activity in self.active.get(eid, {}).items():
            if activity.kind == kind:
                self.failed_keys.add((eid, key))
        self.failures[eid] = ActivityFailure(
            kind=kind,
            actor=actor or DATA_JOBS.get(kind, ("system", kind))[0],
            reason=reason,
            subject_id=subject_id,
            retry=RETRY.get(kind),
            failed_at=self.now(),
        )

    def activities(self, eid: str) -> list[Activity]:
        """该项目正在进行的活动, 按开始的先后; 空闲为空."""
        return list(self.active.get(eid, {}).values())

    def last_failure(self, eid: str) -> ActivityFailure | None:
        return self.failures.get(eid)
