"""EngagementRunner (A 负责, M2 实现): 项目锁, 后台推进, recover 规则见 开发接口文档.md 第 7 节."""

import asyncio

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from backend.agents.base import AgentDeps
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import AgentId, Phase, Procedure
from backend.contracts.state import EngagementState, initial_state
from backend.services.procedure_library import ProcedureLibrary


def fixed_procedures(library: ProcedureLibrary) -> list[Procedure]:
    """固定集: P2 以外的程序在项目启动时实例化; P2 程序由 assemble_pack 按风险加入."""
    return [
        Procedure(id=t.id, phase=t.phase, template_commit=library.commit, autonomy_level=t.autonomy_level)
        for t in library.all()
        if t.phase is not Phase.P2_Executing
    ]


class GraphRunner:
    """EngagementRunner 的实现: 在后台推进图, 管项目锁与重启恢复.

    同一项目同时只允许一个图执行. 项目锁在 start, resume 与 DecisionSubmit 入口取得, 图停在中断或结束时释放.
    """

    def __init__(self, app: CompiledStateGraph[EngagementState], deps: AgentDeps) -> None:
        self.app = app  # 编译好的主图
        self.deps = deps  # 外部依赖
        self.busy: set[str] = set()  # 持有项目锁的项目编号
        self.tasks: dict[str, asyncio.Task[None]] = {}  # 项目编号 -> 后台任务; 保留引用, 防止任务未完成即被回收
        self.errors: dict[str, str] = {}  # 项目编号 -> 最近一次后台执行的异常, 成功推进后清除

    @staticmethod
    def config(eid: str) -> RunnableConfig:
        return {"configurable": {"thread_id": eid}}

    def acquire(self, eid: str) -> None:
        """取得项目锁; 已被持有时抛 DecisionRejected("ENGAGEMENT_BUSY")."""
        if eid in self.busy:
            raise DecisionRejected("ENGAGEMENT_BUSY", f"{eid} 正在执行")
        self.busy.add(eid)

    def release(self, eid: str) -> None:
        """释放项目锁."""
        self.busy.discard(eid)

    def is_running(self, eid: str) -> bool:
        """以项目锁是否被持有为准."""
        return eid in self.busy

    def run_error(self, eid: str) -> str | None:
        """最近一次后台执行的异常, 没有为 None."""
        return self.errors.get(eid)

    def launch(self, eid: str, graph_input: EngagementState | None) -> None:
        """在后台推进图并立即返回. 调用前须已取得项目锁, 由后台任务结束时释放; 任务建不起来时在此释放后抛出.

        graph_input: 新项目为初始状态; 闸门决定写入后继续执行为 None.
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

    async def _advance(self, eid: str, graph_input: EngagementState | None) -> None:
        """后台任务: 推进图到下一个中断或结束. 异常记入 errors 不再抛出; 无论成败, 最后释放项目锁."""
        try:
            await self.app.ainvoke(graph_input, self.config(eid))  # 执行到下一个中断或结束才返回
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

    async def start(self, eid: str) -> None:
        """新项目启动: 取得项目锁, 固定集程序第 1 版写入 Store, 以初始状态在后台推进到第一个中断.

        返回时 is_running 已为真. 启动准备阶段出错时释放项目锁并抛出. 只在项目新建后调用一次.
        """
        self.acquire(eid)
        try:
            engagement = await self.deps.engagement_store.get(eid)
            if engagement is None:
                raise LookupError(f"Engagement {eid} not found")
            # 图状态记本项目采用的画像版本 (建项目时的版本), 不取全局最新: 同客户的其他项目可能已改过画像
            version = engagement.client_profile_version
            profile = await self.deps.engagement_store.client_profile(engagement.client_id, version)
            if profile is None:
                raise LookupError(f"client profile {engagement.client_id} v{version} not found")
            # 固定集第 1 版先入库: 节点写程序新版本时, put 要求已有上一版
            pack = fixed_procedures(self.deps.library)
            await self.deps.output_store.put(eid, pack, actor=AgentId.A1)
            graph_input = initial_state(engagement, pack, profile.version)
        except BaseException:
            # 后台任务尚未启动, 不会由 _advance 释放, 须在此释放
            self.release(eid)
            raise
        self.launch(eid, graph_input)

    async def resume(self, eid: str) -> None:
        raise NotImplementedError

    async def recover(self) -> None:
        raise NotImplementedError
