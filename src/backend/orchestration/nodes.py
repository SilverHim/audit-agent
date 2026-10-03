"""主图节点 (A 负责). 标 TODO 的节点在 M2 按 Demo需求文档.md 4.1 节与开发接口文档.md 第 3, 7 节实现.

闸门节点不做业务处理: 决定由 DecisionSubmit 写入, 并以 aupdate_state(as_node=闸门节点) 直接越过闸门.
"""

import copy
from datetime import UTC, datetime
from typing import Any, TypedDict, cast

from langgraph.errors import GraphBubbleUp
from langgraph.types import interrupt

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.agents.registry import build_agents
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import (
    AgentId,
    Engagement,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ReviewTask,
    RunStatus,
    RunTrigger,
    TaskKind,
    Versioned,
)
from backend.contracts.ports import AuditOutputStore
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.merge import MergeRejected, check_patch
from backend.orchestration.scheduler import build_schedule
from backend.services.procedure_library import ProcedureLibrary, highest_risk
from backend.services.review_queue import ReviewQueue


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


def build_run(
    library: ProcedureLibrary,
    template: ProcedureTemplate,
    procedure: Procedure,
    engagement: Engagement,
    state: EngagementState,
    started_at: datetime,
) -> ProcedureRun:
    """建某条程序的下一次执行记录, execute 与 fieldwork 共用.

    参数 = 模板参数的解析结果, 再叠加 Procedure.params (rerun 决定改的参数, 同名键以它为准).
    解析用的风险等级取程序关联审计目标的最高等级. 参数解析不了 (如重要性尚未确定) 时抛错, 不建执行记录.
    Procedure.params 深拷贝后叠加: 其中的列表与字典若与快照共用, 改一处会连带改到程序和历次执行的快照.
    """
    risk_level = highest_risk(state["audit_objectives"], procedure.objective_ids)
    resolved = library.resolve_params(template, engagement, state, risk_level)
    params = {**resolved, **copy.deepcopy(procedure.params)}
    return next_run(procedure.id, state["procedure_runs"], params, started_at)


def fail_run(queue: ReviewQueue, run: ProcedureRun, reason: str, ended_at: datetime) -> StatePatch:
    """整次执行失败时 A1 代写的片段: 执行记录置 failed, 建一条 failed 任务 (目标为该执行记录, subject_id 为空).

    用于 Agent 抛异常 (含 RuleError) 或片段校验不过, 见接口文档第 5 节需要人表第 1 行. execute 与 fieldwork 共用.
    run: 本次执行记录 (running); reason: 写入 failure_reason, 如 "RuntimeError: boom"; ended_at: 失败时间.
    例: fail_run(deps.queue, ctx.run, "RuntimeError: boom", now) ->
        {"procedure_runs": [RUN-ACC-04-1 failed], "review_tasks": [T-RUN-ACC-04-1-v1 failed pending]}
    """
    failed = run.model_copy(update={"status": RunStatus.failed, "ended_at": ended_at, "failure_reason": reason})
    task = queue.make_task(TaskKind.failed, failed, procedure_id=run.procedure_id, run_id=run.id, now=ended_at)
    return {"procedure_runs": [failed], "review_tasks": [task]}


def has_failed_task(patch: StatePatch) -> bool:
    """片段里有 failed 任务 (整次失败时 A1 建的, 或 Agent 建的单对象失败任务): 程序要在节点内等人处理.

    按任务类型判断, 不看任务是否待决: 审计师决定后任务置 decided, 恢复时节点从头执行, 取回的仍是这些任务,
    仍须走到同一个 interrupt() 取回那个决定.
    """
    return any(task.kind is TaskKind.failed for task in patch.get("review_tasks", []))


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

    async def run_agent(self, agent: ProcedureAgent[Any], ctx: RunContext) -> tuple[StatePatch, AgentId]:
        """调 Agent 并校验它的片段, 返回 (要写入的片段, 写入者).

        正常时为 Agent 的片段与 Agent 编号. Agent 抛异常 (含 RuleError) 或片段校验不过时, Agent 的片段整体丢弃,
        返回 A1 代写的失败片段 (fail_run) 与 A1. failure_reason 为 "异常类名: 异常文本",
        校验不过为 "merge_rejected: 原因".
        LangGraph 的中断等控制流信号 (GraphBubbleUp) 不算失败, 照常抛出.
        """
        try:
            patch = await agent.run(ctx)
            check_patch(
                patch, writer=agent.agent_id, procedure_id=ctx.run.procedure_id, run_id=ctx.run.id, state=ctx.state
            )
        except GraphBubbleUp:
            raise
        except MergeRejected as e:
            reason = str(e)
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
        else:
            return patch, agent.agent_id
        return fail_run(self.deps.queue, ctx.run, reason, datetime.now(UTC)), AgentId.A1

    async def stored_failure(self, eid: str, run_id: str) -> StatePatch | None:
        """恢复保护: Store 中本次执行已是终态且带 failed 任务时, 返回 Store 中的该执行与它的全部任务; 否则返回 None.

        上次执行在此失败并等人, 恢复时节点从头执行; 若再调 Agent 而这次成功了, 会写出与 Store 矛盾的结果
        (Store 里已是失败执行与 awaiting_human). 所以不调 Agent, 照 Store 里的结果再走一次等待.
        成功执行的完整取回 (评分卡等产出) 见 R04.
        """
        stored = await self.deps.output_store.get(eid, ProcedureRun, run_id)
        if stored is None or stored.status is RunStatus.running:
            return None
        patch: StatePatch = {
            "procedure_runs": [stored],
            "review_tasks": await self.deps.output_store.latest(eid, ReviewTask, run_id=run_id),
        }
        return patch if has_failed_task(patch) else None

    async def execute(self, state: EngagementState, procedure_id: str) -> StatePatch:
        """执行一条 P2 以外的程序: 建执行记录, 调 Agent 并校验片段, 程序出新版本, 同一事务内写入 Store.

        程序新版本的状态: 片段有 failed 任务 (执行失败时 A1 建的, 或 Agent 建的单对象失败任务) 为 awaiting_human,
        写入后在节点内 interrupt() 等人处理; 否则为 completed, 返回写入后的片段与更新后的完整程序包.
        执行期间不生成 executing 版本, 程序包中的原对象直接传给 Agent.
        Agent 的异常与片段校验不过不向上抛, 转成失败执行与 failed 任务 (见 run_agent); 节点自身的错误 (项目缺失,
        参数解析不了, 写 Store 失败) 照常抛出, 由 runner 记入 run_error.
        恢复时本次执行已失败并在等人的, 不再调 Agent (见 stored_failure).
        """
        procedure = next(p for p in state["procedure_pack"] if p.id == procedure_id)
        engagement = await self.deps.engagement_store.get(state["engagement_id"])
        if engagement is None:
            raise LookupError(f"Engagement {state['engagement_id']} not found")
        template = self.deps.library.load(procedure_id)
        procedure_run = build_run(self.deps.library, template, procedure, engagement, state, datetime.now(UTC))
        ctx = RunContext(engagement=engagement, procedure=procedure, template=template, run=procedure_run, state=state)
        replayed = await self.stored_failure(engagement.id, procedure_run.id)
        if replayed is not None:
            patch, writer = replayed, AgentId.A1
        else:
            patch, writer = await self.run_agent(self.agents[procedure_id], ctx)
        waiting = has_failed_task(patch)
        status = ProcedureStatus.awaiting_human if waiting else ProcedureStatus.completed
        pack = update_procedure(state["procedure_pack"], procedure_id, status=status)
        updated = next(p for p in pack if p.id == procedure_id)  # 本程序的新版本
        # 片段与程序新版本在同一事务内写入, 保证原子性. 程序新版本取 put 的返回: 重放时为 Store 中已存的那一版
        async with self.deps.output_store.transaction(engagement.id):
            stored = await save_outputs(self.deps.output_store, engagement.id, patch, actor=writer)
            [saved] = await self.deps.output_store.put(engagement.id, [updated], actor=AgentId.A1)
        pack = [cast(Procedure, saved) if p.id == procedure_id else p for p in pack]

        if waiting:
            # 节点在此暂停, 本次返回值不进状态, 失败执行与任务只在 Store 中; 总览按检查点里的中断显示"等复核".
            # 恢复时节点从头执行, interrupt() 返回审计师决定的 ID
            record_id = interrupt({"awaiting": "human", "procedure_id": procedure_id, "run_id": procedure_run.id})
            raise NotImplementedError(f"收到决定 {record_id} 后重跑 {procedure_id} 尚未实现 (R05)")
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
