"""EngagementRunner (A 负责, M2 实现): 项目锁, 后台推进, recover 规则见 开发接口文档.md 第 7 节."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from backend.adapters.llm import llm_version
from backend.agents.base import AgentDeps
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import AgentId, Engagement, GateDecision, GateId, Phase, Procedure, ReviewRecord
from backend.contracts.state import EngagementState, initial_state
from backend.core.config import get_settings
from backend.orchestration.graph import GATE_NODES
from backend.orchestration.nodes import AWAITING_DATA, DATA_READY
from backend.orchestration.trail import data_ready_entry, record_transitions
from backend.services.procedure_library import ProcedureLibrary


def fixed_procedures(library: ProcedureLibrary) -> list[Procedure]:
    """固定集: P2 以外的程序在项目启动时实例化; P2 程序由 assemble_pack 按风险加入."""
    return [
        Procedure(id=t.id, phase=t.phase, template_commit=library.commit, autonomy_level=t.autonomy_level)
        for t in library.all()
        if t.phase is not Phase.P2_Executing
    ]


def waiting_payloads(snapshot: StateSnapshot) -> list[Any]:
    """图停在节点内 interrupt() 时各等待的载荷 (await_events 为 {"awaiting": "review"}; P2 以外程序失败后的等待为
    {"awaiting": "human", "procedure_id", "run_id", "records"}); 不在节点内等待时为空.
    看待执行任务的 interrupts, 不看 next: 节点第二次及以后停下等待时 next 为空 (接口文档第 7 节恢复机制)."""
    return [waiting.value for task in snapshot.tasks for waiting in task.interrupts]


def is_data_wait(payload: Any) -> bool:
    """等待载荷是不是等数据就绪 (不是等复核或等人决定)."""
    return isinstance(payload, dict) and payload.get("awaiting") == AWAITING_DATA


def awaiting_data(snapshot: StateSnapshot) -> bool:
    """图停在 p1_await_data 等数据就绪."""
    return any(is_data_wait(waiting) for waiting in waiting_payloads(snapshot))


def review_waits(snapshot: StateSnapshot) -> list[Any]:
    """节点内等审计师复核决定的等待载荷 (await_events 与 P2 以外程序失败后的等待), 不含等数据就绪."""
    return [waiting for waiting in waiting_payloads(snapshot) if not is_data_wait(waiting)]


def waiting_for_human(snapshot: StateSnapshot) -> bool:
    """图停在闸门前 (静态中断, 不带 interrupts) 或节点内 interrupt() 等人 (含等待中出错的节点)."""
    gates = set(GATE_NODES.values())
    return any(node in gates for node in snapshot.next) or any(task.interrupts for task in snapshot.tasks)


async def resume_required(deps: AgentDeps, engagement: Engagement, snapshot: StateSnapshot) -> str | None:
    """Store 已写入, 检查点却没跟上 (上次写完 Store 后中断) 的内容, 须先补做; 没有返回 None.

    两种情况: Store 中有检查点里没有的决定; 项目采用的画像版本 (Engagement.client_profile_version) 高于检查点的.
    比的是本项目采用的版本, 不是全局最新画像: 同客户的其他项目改画像不应让本项目须补做.
    节点内等待时, 本节点已读回的决定在节点返回前不进状态, 列在等待载荷的 records 中 (如 ACC-04 重跑后又失败,
    第一个 rerun 决定已用过), 也算检查点里有.
    """
    eid = engagement.id
    values = snapshot.values
    stored = {d.id for d in await deps.output_store.latest(eid, GateDecision)}
    stored |= {r.id for r in await deps.output_store.latest(eid, ReviewRecord)}
    in_state = {d.id for d in values.get("gate_decisions", [])} | {r.id for r in values.get("review_records", [])}
    for waiting in waiting_payloads(snapshot):
        if isinstance(waiting, dict):
            in_state.update(waiting.get("records", []))
    missing = stored - in_state
    if missing:
        return ", ".join(sorted(missing))
    version_in_state = values.get("client_profile_version")  # 未启动的项目没有
    if version_in_state is not None and engagement.client_profile_version > version_in_state:
        return f"本项目采用的客户画像第 {engagement.client_profile_version} 版未写入检查点"
    return None


class GraphRunner:
    """EngagementRunner 的实现: 在后台推进图, 管项目锁与重启恢复.

    同一项目同时只允许一个图执行. 项目锁在 start, resume 与 DecisionSubmit 入口取得, 图停在中断或结束时释放.
    """

    def __init__(
        self,
        app: CompiledStateGraph[EngagementState],
        deps: AgentDeps,
        llm: dict[str, str] | Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self.app = app  # 编译好的主图
        self.deps = deps  # 外部依赖
        # LLM 模式与模型名, 项目启动时冻结进 version_snapshot: 固定的字典, 或每次冻结时调用的函数 (设置页可热切换模型,
        # 组装根传 LlmSettingsService.versions); 缺省按当前配置 (见 adapters.llm.llm_version)
        self.llm = llm if llm is not None else llm_version(get_settings())
        self.busy: set[str] = set()  # 持有项目锁的项目编号
        self.tasks: dict[str, asyncio.Task[None]] = {}  # 项目编号 -> 后台任务; 保留引用, 防止任务未完成即被回收
        self.errors: dict[str, str] = {}  # 项目编号 -> 最近一次后台执行的异常, 成功推进后清除
        self.acquired: dict[str, int] = {}  # 项目编号 -> 取得项目锁的累计次数, 读者据此判断期间有没有写入

    @staticmethod
    def config(eid: str) -> RunnableConfig:
        return {"configurable": {"thread_id": eid}}

    def acquire(self, eid: str) -> None:
        """取得项目锁; 已被持有时抛 DecisionRejected("ENGAGEMENT_BUSY")."""
        if eid in self.busy:
            raise DecisionRejected("ENGAGEMENT_BUSY", f"{eid} 正在执行")
        self.busy.add(eid)
        self.acquired[eid] = self.acquired.get(eid, 0) + 1

    def lock_count(self, eid: str) -> int:
        """该项目取得项目锁的累计次数. 写 Store 与检查点都在持锁期间: 读前读后次数相同且读前未持锁, 说明读取期间
        没有写入, 读到的 Store 与检查点是同一时刻的."""
        return self.acquired.get(eid, 0)

    def release(self, eid: str) -> None:
        """释放项目锁."""
        self.busy.discard(eid)

    def is_running(self, eid: str) -> bool:
        """以项目锁是否被持有为准."""
        return eid in self.busy

    def run_error(self, eid: str) -> str | None:
        """最近一次后台执行的异常, 没有为 None."""
        return self.errors.get(eid)

    def launch(self, eid: str, graph_input: EngagementState | Command[Any] | None) -> None:
        """在后台推进图并立即返回. 调用前须已取得项目锁, 由后台任务结束时释放; 任务建不起来时在此释放后抛出.

        graph_input: 新项目为初始状态; 闸门决定写入后继续执行为 None; 复核决定写入后为 Command(resume=决定 ID),
        交给停在节点内 interrupt() 的节点.
        """
        # self._advance(...) 不加 await, 得到尚未执行的协程; create_task 将其放入后台执行并立即返回任务对象
        coro = self._advance(eid, graph_input)
        try:
            task = asyncio.create_task(coro)
        except BaseException:
            coro.close()  # 协程不会再执行, 关掉以免回收时告警"从未 await"
            self.release(eid)
            raise
        self.tasks[eid] = task  # 须保留引用, 否则任务可能在完成前被回收
        # 兜底: 任务在开始执行前就被取消时, _advance 的 finally 不会执行, 由结束回调释放锁
        task.add_done_callback(lambda t: self._forget(eid, t))

    def _forget(self, eid: str, task: asyncio.Task[None]) -> None:
        """后台任务结束后清理项目锁与任务引用. 只在登记的仍是这个任务时清理, 不误删之后为同一项目新建的任务."""
        if self.tasks.get(eid) is task:
            del self.tasks[eid]
            self.release(eid)

    async def _advance(self, eid: str, graph_input: EngagementState | Command[Any] | None) -> None:
        """后台任务: 推进图到下一个中断或结束. 异常记入 errors 不再抛出; 无论成败, 最后释放项目锁."""
        try:
            # 执行到下一个中断或结束才返回. durability="sync": 每步的检查点写完才进入下一步, 进程被杀时
            # 已执行完的节点都在检查点里, recover 不会重跑它们 (默认 async 下最后一步可能还没落盘)
            await self.app.ainvoke(graph_input, self.config(eid), durability="sync")
            self.errors.pop(eid, None)  # 成功推进, 清除上次的错误
        except Exception as e:
            # 后台任务无人等待, 异常抛出即丢失; 记入 errors, 由总览的 run_error 展示给前端
            self.errors[eid] = f"{type(e).__name__}: {e}"
        finally:
            # 无论成败都释放项目锁, 否则该项目此后的所有操作都会被拒绝 (ENGAGEMENT_BUSY).
            # 在任务内释放而不只靠结束回调: 等待任务的一方醒来时锁已释放
            task = asyncio.current_task()
            if task is not None:
                self._forget(eid, task)

    async def shutdown(self) -> None:
        """应用退出前调用: 取消全部后台任务并等它们结束. 返回时没有运行中的任务, 项目锁都已释放.

        被取消的执行停在最近的检查点, 下次启动由 recover 继续.
        """
        running = list(self.tasks.items())
        for _, task in running:
            task.cancel()
        await asyncio.gather(*(task for _, task in running), return_exceptions=True)
        for eid, task in running:
            self._forget(eid, task)

    def freeze_versions(self) -> dict[str, str]:
        """项目启动时冻结的版本快照: 程序库 commit, LLM 模式与模型名, prompt 摘要, 冻结时刻 (UTC, ISO 8601)."""
        return {
            "procedure_library_commit": self.deps.library.commit,
            **(self.llm() if callable(self.llm) else self.llm),
            "prompts_sha256": self.deps.library.prompts_sha256(),
            "frozen_at": datetime.now(UTC).isoformat(),
        }

    async def _initial_input(self, engagement: Engagement) -> EngagementState:
        """新项目的初始状态. 一个事务内: 固定集程序第 1 版入库 (节点写程序新版本时, put 要求已有上一版; 已入库时
        put 跳过, 状态取 Store 中的那一版), 项目还没有版本快照时冻结并写入 engagements 表 (已有的不覆盖).
        初始状态的版本快照与项目表一致."""
        # 图状态记本项目采用的画像版本 (建项目时的版本), 不取全局最新: 同客户的其他项目可能已改过画像
        version = engagement.client_profile_version
        profile = await self.deps.engagement_store.client_profile(engagement.client_id, version)
        if profile is None:
            raise LookupError(f"client profile {engagement.client_id} v{version} not found")
        snapshot = engagement.version_snapshot or self.freeze_versions()
        async with self.deps.output_store.transaction(engagement.id):
            pack = await self.deps.output_store.put(
                engagement.id, fixed_procedures(self.deps.library), actor=AgentId.A1
            )
            if not engagement.version_snapshot:
                await self.deps.engagement_store.update(engagement.id, version_snapshot=snapshot)
        frozen = engagement.model_copy(update={"version_snapshot": snapshot})
        return initial_state(frozen, [p for p in pack if isinstance(p, Procedure)], profile.version)

    async def _engagement(self, eid: str) -> Engagement:
        engagement = await self.deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(f"Engagement {eid} not found")
        return engagement

    async def start(self, eid: str) -> None:
        """新项目启动: 取得项目锁, 固定集程序第 1 版写入 Store, 以初始状态在后台推进到第一个中断.

        返回时 is_running 已为真. 启动准备阶段出错时释放项目锁并抛出. 只在项目新建后调用一次.
        """
        self.acquire(eid)
        try:
            graph_input = await self._initial_input(await self._engagement(eid))
        except BaseException:
            # 后台任务尚未启动, 不会由 _advance 释放, 须在此释放
            self.release(eid)
            raise
        self.launch(eid, graph_input)

    async def _proceed(self, eid: str) -> None:
        """已持项目锁时按第 7 节 recover 规则处理一个项目: 要继续执行则交给后台任务 (锁随之交出), 否则释放锁.

        只看检查点, 不看项目阶段 (已归档的项目若检查点仍停在闸门, 照样处理):
        - 没有检查点 (start 写了固定集, 第一步还没落盘就中断): 以初始状态重新推进.
        - 已结束 (next 与待执行任务都为空): 不动.
        - Store 有检查点里没有的决定或画像版本: 先补做 (见 _catch_up), 再按补做后的检查点往下判断.
        - 停在闸门前或节点内等人 (任一待执行任务带 interrupts, 含等待中出错的节点): 不动, 等人时没有新决定可交.
          例外: 停在 p1_await_data 等数据, 而 Store 中已记审计师确认数据已齐 (确认写入后, 恢复前中断), 先补写
          确认的 data 留痕 (同键已有的跳过; 写不成则报错, 留在等待), 再以 Command(resume=DATA_READY) 继续.
        - 升级前的旧检查点 (需求 R10 之前 ACC-01 通过后直接到 p1_ingest, 由它载入数据集): 待执行 p1_ingest 而数据
          未确认就绪时, 以 gate_acc01 身份重写检查点, 按新拓扑转入 p1_await_data 等数据 (见 _legacy_ingest).
        - 其余 (进程中途被杀, 节点出错): 从检查点继续 ainvoke(None).
        出错时释放锁后抛出.
        """
        try:
            snapshot = await self.app.aget_state(self.config(eid))
            engagement = await self._engagement(eid)
            graph_input: EngagementState | Command[Any] | None = None
            if snapshot.metadata is None:
                graph_input = await self._initial_input(engagement)
            else:
                if not snapshot.next and not snapshot.tasks:
                    self.release(eid)
                    return
                if await resume_required(self.deps, engagement, snapshot) is not None:
                    snapshot, graph_input = await self._catch_up(engagement)
                if graph_input is None and await self._legacy_ingest(eid, snapshot):
                    snapshot = await self._reroute_to_data_wait(eid)
                if graph_input is None and await self._data_confirmed(eid, snapshot):
                    graph_input = Command(resume=DATA_READY)
                elif graph_input is None and waiting_for_human(snapshot):
                    self.release(eid)
                    return
        except BaseException:
            self.release(eid)
            raise
        self.launch(eid, graph_input)

    async def _data_confirmed(self, eid: str, snapshot: StateSnapshot) -> bool:
        """图停在 p1_await_data 等数据, 而 Store 中已记确认数据已齐 (确认写入后, 留痕或恢复前中断): 补写确认的 data
        留痕 (同键已有的跳过) 后返回真, 由调用方恢复; 留痕写失败照常抛出, 图留在等待, 下次再补."""
        if not awaiting_data(snapshot):
            return False
        readiness = await self.deps.data.readiness(eid)
        if readiness.ready_at is None:
            return False
        await record_transitions(self.deps.trail, [data_ready_entry(eid, readiness)])
        return True

    async def _legacy_ingest(self, eid: str, snapshot: StateSnapshot) -> bool:
        """升级前的旧检查点: 待执行 p1_ingest (不在节点内等待) 而数据未确认就绪.

        新拓扑里 p1_ingest 只能经 p1_await_data 到达, 那时已确认; 未确认却排着 p1_ingest, 只能是旧拓扑在 ACC-01 通过
        之后, p1_ingest 载入数据集之前中断留下的 (已载入的项目迁移时已补确认)."""
        return (
            snapshot.next == ("p1_ingest",)
            and not any(task.interrupts for task in snapshot.tasks)
            and (await self.deps.data.readiness(eid)).ready_at is None
        )

    async def _reroute_to_data_wait(self, eid: str) -> StateSnapshot:
        """以 gate_acc01 身份重写检查点 (不改状态): ACC-01 的路由按已有的通过决定重新选出口, 新拓扑为 p1_await_data."""
        await self.app.aupdate_state(self.config(eid), {}, as_node=GATE_NODES[GateId.ACC_01])
        return await self.app.aget_state(self.config(eid))

    async def _catch_up(self, engagement: Engagement) -> tuple[StateSnapshot, Command[Any] | None]:
        """补做 Store 已提交而检查点没跟上的内容 (见 recovery.catch_up). 返回补做后的检查点与继续执行的输入:
        复核决定为 Command(resume=决定 ID), 交给等待中的节点读回; 其余为 None. 后者补做后检查点仍未跟上时抛
        RuntimeError."""
        # 在此导入: recovery 依赖 decisions, decisions 依赖本模块, 放在模块开头会循环导入
        from backend.orchestration.recovery import catch_up

        resume = await catch_up(self.app, self.deps, engagement)
        snapshot = await self.app.aget_state(self.config(engagement.id))
        if resume is None:
            missing = await resume_required(self.deps, engagement, snapshot)
            if missing is not None:
                raise RuntimeError(f"补做后检查点仍未跟上 Store ({missing})")
        return snapshot, resume

    async def resume(self, eid: str) -> None:
        """手动重试一个项目 (后台执行出错后, 由 POST /resume 调用): 取项目锁 (取不到抛 ENGAGEMENT_BUSY),
        再按 recover 规则处理 (见 _proceed). 继续执行时返回时 is_running 为真, 成功推进后 run_error 清空;
        停在闸门或等人, 或已结束时不改状态, 不持锁."""
        self.acquire(eid)
        await self._proceed(eid)

    async def recover(self) -> None:
        """应用启动时对全部项目按 recover 规则处理 (见 _proceed). 要继续执行的项目返回时 is_running 为真.

        一个项目出错记入其 run_error, 不影响其他项目.
        """
        for engagement in await self.deps.engagement_store.list():
            try:
                self.acquire(engagement.id)
                await self._proceed(engagement.id)
            except Exception as e:
                self.errors[engagement.id] = f"{type(e).__name__}: {e}"
