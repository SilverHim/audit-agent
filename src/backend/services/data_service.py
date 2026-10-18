"""数据管理的服务 (需求 R10-R16): 数据管理接口的写操作都经这里, 取项目锁, 写存储, 记 data 留痕.

项目锁与编排共用 (GraphRunner.acquire): 同一项目同时只有一个写者, 图在后台推进时写数据报 ENGAGEMENT_BUSY.
"""

from langgraph.types import Command

from backend.agents.base import AgentDeps
from backend.contracts.data import DataReadiness
from backend.contracts.errors import DataRejected
from backend.orchestration.nodes import DATA_READY
from backend.orchestration.runner import GraphRunner, awaiting_data
from backend.orchestration.trail import data_ready_entry, record_transitions
from backend.services.data_quality import missing_tables


class DataService:
    """数据管理的写操作与查询. 组装根建一个, 挂在 app.state.data_service."""

    def __init__(self, deps: AgentDeps, runner: GraphRunner) -> None:
        self.deps = deps  # 外部依赖
        self.runner = runner  # 项目锁与后台推进; 图取 runner.app

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
