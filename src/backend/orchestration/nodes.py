"""主图节点（A 负责）。标 TODO 的节点在 M2 按 Demo需求文档.md 4.1 节与开发接口文档.md 第 3、7 节实现。

闸门节点不做业务处理：决定由 DecisionSubmit 写入，并以 aupdate_state(as_node=闸门节点) 直接越过闸门。
"""

from typing import TypedDict

from langgraph.types import interrupt

from backend.agents.base import AgentDeps, ProcedureAgent
from backend.agents.registry import build_agents
from backend.contracts.models import Phase, Procedure
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.scheduler import build_schedule


class FieldworkInput(TypedDict):
    instance: str
    procedure_ids: list[str]
    state: EngagementState


class GraphNodes:
    def __init__(self, deps: AgentDeps, parallel_cap: int) -> None:
        self.deps = deps
        self.parallel_cap = parallel_cap
        self.agents: dict[str, ProcedureAgent] = build_agents(deps)

    # ── P0 ──
    async def p0_intel(self, state: EngagementState) -> StatePatch:
        return {}  # TODO(M2): A2 执行 ACC-04，产出 SCORECARD

    async def gate(self, state: EngagementState) -> StatePatch:
        return {}  # 四个闸门共用：只由出边按 GateDecision 路由

    # ── P1 ──
    async def p1_ingest(self, state: EngagementState) -> StatePatch:
        return {}  # TODO(M2): DatasetLoader.load，再由 A3 执行 DOC-04

    async def p1_analytics(self, state: EngagementState) -> StatePatch:
        return {}  # TODO(M2): A4 执行 PLN-07、PLN-01，读数据集预置的审计目标与风险清单

    async def assemble_pack(self, state: EngagementState) -> StatePatch:
        """按审计目标匹配 trigger，补齐依赖闭包，只把尚不在程序包中的 P2 程序加入。"""
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
        """一个 A6 实例（也执行 A3 的 DOC-06、A5 的 CSH-04）。不写 Store，只返回 staged_results。"""
        raise NotImplementedError("TODO(M2): 建 ProcedureRun → agents[pid].run(ctx)；异常由 A1 转为 failed Run 与任务")

    async def fan_in(self, state: EngagementState) -> StatePatch:
        # TODO(M2): 校验 staged_results → 在一个事务内 put → 并入正式字段 → 重算本轮程序状态
        return {"staged_results": None}

    async def await_events(self, state: EngagementState) -> StatePatch:
        record_id = interrupt({"awaiting": "review"})
        del record_id  # TODO(M2): 按 record_id 从 Store 读回 DecisionSubmit 写入的对象返回
        return {}

    # ── P3、P4 ──
    async def completion(self, state: EngagementState) -> StatePatch:
        return {}  # TODO(M2): A9 执行 FIN-04，产出 SCHEDULE

    async def report_draft(self, state: EngagementState) -> StatePatch:
        return {}  # TODO(M2): A10 执行 RPT-03，出下一 revision

    async def archive(self, state: EngagementState) -> StatePatch:
        return {}  # 项目已由 DecisionSubmit locked，这里只写留痕

    async def declined(self, state: EngagementState) -> StatePatch:
        return {}
