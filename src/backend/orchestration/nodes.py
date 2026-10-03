"""主图节点 (A 负责). 标 TODO 的节点在 M2 按 Demo需求文档.md 4.1 节与开发接口文档.md 第 3, 7 节实现.

闸门节点不做业务处理: 决定由 DecisionSubmit 写入, 并以 aupdate_state(as_node=闸门节点) 直接越过闸门.
"""

from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.types import interrupt

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.agents.registry import build_agents
from backend.contracts.models import (
    AgentId,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    RunStatus,
    RunTrigger,
    Versioned,
)
from backend.contracts.ports import AuditOutputStore
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.scheduler import build_schedule


class FieldworkInput(TypedDict):
    """派给一个 fieldwork 实例的输入, 经 Send 发出."""

    instance: str  # 实例名 fw-{layer}-{k}
    procedure_ids: list[str]  # 本轮要执行的程序
    state: EngagementState  # 派发时的状态快照, 只读


def next_run(procedure_id: str, runs: list[ProcedureRun], params: dict[str, Any], started_at: datetime) -> ProcedureRun:
    """生成某条程序的下一次执行记录, 状态为 running. run_no 为状态中该程序已有执行记录数 +1."""
    run_no = 1
    for run in runs:
        if run.procedure_id == procedure_id:
            run_no += 1

    return ProcedureRun(
        run_no=run_no,
        id=f"RUN-{procedure_id}-{run_no}",
        procedure_id=procedure_id,
        trigger=RunTrigger.initial if run_no == 1 else RunTrigger.rerun,
        params_snapshot=params,
        started_at=started_at,
        status=RunStatus.running,
    )


def update_procedure(pack: list[Procedure], procedure_id: str, **changes: Any) -> list[Procedure]:
    """修改程序包中指定程序的字段并生成新版本 (version +1), 返回新的完整程序包, 原列表与原对象不变.

    changes: 待修改字段, 如 status=ProcedureStatus.completed. 字段名或取值不合法时抛 ValidationError.
    """
    result = []
    for procedure in pack:
        if procedure.id == procedure_id:
            fields = {**procedure.model_dump(), **changes, "version": procedure.version + 1}
            result.append(Procedure.model_validate(fields))
        else:
            result.append(procedure)
    return result


async def save_outputs(output_store: AuditOutputStore, eid: str, patch: StatePatch, actor: str) -> StatePatch:
    """将状态片段中的审计产出写入 Store, 返回以 Store 返回对象替换后的片段, 供节点写入状态.

    按值的类型处理:
    - Versioned 对象 (如评分卡): 写入, 取 Store 返回的对象;
    - 元素均为 Versioned 的列表 (如例外): 整体写入, 取 Store 返回的列表;
    - 其他值 (如 phase, 调度表): 不写入, 原样保留.
    Store 返回的通常即写入的对象; 重放时同 (id, version) 已存在, 不重复写入并返回已存对象, 使状态与 Store 一致.
    事务由调用方开启.
    """
    result: StatePatch = {}
    for key, value in patch.items():
        if isinstance(value, Versioned):
            stored_list = await output_store.put(eid=eid, objs=[value], actor=actor)
            result[key] = stored_list[0]
        elif isinstance(value, list) and all(isinstance(v, Versioned) for v in value):
            result[key] = await output_store.put(eid=eid, objs=value, actor=actor)
        else:
            result[key] = value
    return result


class GraphNodes:
    """主图的全部节点函数, 持有依赖与各 Agent 实例."""

    def __init__(self, deps: AgentDeps, parallel_cap: int) -> None:
        self.deps = deps  # 外部依赖
        self.parallel_cap = parallel_cap  # fieldwork 实例并行上限
        self.agents: dict[str, ProcedureAgent[Any]] = build_agents(deps)  # 程序编号 -> 负责的 Agent 实例

    async def execute(self, state: EngagementState, procedure_id: str) -> StatePatch:
        """执行一条 P2 以外的程序: 生成执行记录, 调用负责的 Agent, 程序置为 completed 新版本, 同一事务内写入 Store.

        执行期间不生成 executing 版本, 程序包中的原对象直接传给 Agent. 返回写入后的 Agent 片段与更新后的完整程序包.
        Agent 抛出的异常暂不处理, 直接向上传播.
        """
        procedure = next(p for p in state["procedure_pack"] if p.id == procedure_id)
        engagement = await self.deps.engagement_store.get(state["engagement_id"])
        if engagement is None:
            raise LookupError(f"Engagement {state['engagement_id']} not found")
        procedure_run = next_run(
            procedure_id=procedure_id,
            runs=state["procedure_runs"],
            params=procedure.params,
            started_at=datetime.now(UTC),
        )
        ctx = RunContext(
            engagement=engagement,
            procedure=procedure,
            template=self.deps.library.load(procedure_id),
            run=procedure_run,
            state=state,
        )
        agent = self.agents[procedure_id]
        patch = await agent.run(ctx)
        pack = update_procedure(state["procedure_pack"], procedure_id, status=ProcedureStatus.completed)
        completed = next(p for p in pack if p.id == procedure_id)  # 本程序的 completed 新版本
        # Agent 产出与程序新版本在同一事务内写入, 保证原子性.
        # 程序新版本由状态确定生成, 重放时与已存版本一致, 故直接使用 pack, 不取 put 返回值.
        async with self.deps.output_store.transaction(engagement.id):
            stored = await save_outputs(self.deps.output_store, engagement.id, patch, actor=agent.agent_id)
            await self.deps.output_store.put(engagement.id, [completed], actor=AgentId.A1)

        return {**stored, "procedure_pack": pack}

    # ── P0 ──
    async def p0_intel(self, state: EngagementState) -> StatePatch:
        """A2 执行 ACC-04, 产出承接评分卡."""
        return await self.execute(state=state, procedure_id="ACC-04")

    async def gate(self, state: EngagementState) -> StatePatch:
        return {}  # 四道闸门共用, 不处理业务; 出口由路由函数按 GateDecision 决定

    # ── P1 ──
    async def p1_ingest(self, state: EngagementState) -> StatePatch:
        return {}  # TODO(M2): DatasetLoader.load, 再由 A3 执行 DOC-04

    async def p1_analytics(self, state: EngagementState) -> StatePatch:
        return {}  # TODO(M2): A4 执行 PLN-07, PLN-01, 读数据集预置的审计目标与风险清单

    async def assemble_pack(self, state: EngagementState) -> StatePatch:
        """按审计目标匹配 trigger, 补齐依赖闭包, 只把尚不在程序包中的 P2 程序加入."""
        library = self.deps.library
        matched = {t.id: obj.id for obj in state["audit_objectives"] for t in library.match(obj)}
        existing = {p.id for p in state["procedure_pack"]}
        added = [
            Procedure(
                id=t.id,
                phase=t.phase,
                template_commit=library.commit,
                objective_ids=[o.id for o in state["audit_objectives"] if t in library.match(o)],
                autonomy_level=t.autonomy_level,
            )
            for t in library.closure(matched)
            if t.phase is Phase.P2_Executing and t.id not in existing
        ]
        if not added:
            return {}
        return {"procedure_pack": [*state["procedure_pack"], *added], "phase": Phase.P2_Executing}

    # ── P2 ──
    async def schedule(self, state: EngagementState) -> StatePatch:
        return {"schedule_table": build_schedule(state["procedure_pack"], self.deps.library, self.parallel_cap)}

    async def fieldwork(self, payload: FieldworkInput) -> StatePatch:
        """一个 A6 实例 (也执行 A3 的 DOC-06, A5 的 CSH-04). 不写 Store, 只返回 staged_results."""
        raise NotImplementedError("TODO(M2): 建 ProcedureRun → agents[pid].run(ctx); 异常由 A1 转为 failed Run 与任务")

    async def fan_in(self, state: EngagementState) -> StatePatch:
        # TODO(M2): 校验 staged_results → 在一个事务内 put → 并入正式字段 → 重算本轮程序状态
        return {"staged_results": None}

    async def await_events(self, state: EngagementState) -> StatePatch:
        record_id = interrupt({"awaiting": "review"})
        del record_id  # TODO(M2): 按 record_id 从 Store 读回 DecisionSubmit 写入的对象返回
        return {}

    # ── P3, P4 ──
    async def completion(self, state: EngagementState) -> StatePatch:
        return {}  # TODO(M2): A9 执行 FIN-04, 产出 SCHEDULE

    async def report_draft(self, state: EngagementState) -> StatePatch:
        return {}  # TODO(M2): A10 执行 RPT-03, 出下一 revision

    async def archive(self, state: EngagementState) -> StatePatch:
        return {}  # 项目已由 DecisionSubmit 锁定, 此处只写留痕

    async def declined(self, state: EngagementState) -> StatePatch:
        return {}
