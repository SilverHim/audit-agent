"""主图节点 (A 负责). 标 TODO 的节点在 M2 按 Demo需求文档.md 4.1 节与开发接口文档.md 第 3, 7 节实现.

闸门节点不做业务处理: 决定由 DecisionSubmit 写入, 并以 aupdate_state(as_node=闸门节点) 直接越过闸门.
"""

import copy
from datetime import UTC, datetime
from typing import Any, TypedDict, cast

from langgraph.errors import GraphBubbleUp
from langgraph.types import interrupt
from pydantic import BaseModel

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.agents.registry import build_agents
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import (
    AcceptanceScorecard,
    AgentId,
    AnalyticsResult,
    AuditException,
    Decision,
    Engagement,
    Judgment,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ReviewRecord,
    ReviewTask,
    RunStatus,
    RunTrigger,
    TargetType,
    TaskKind,
    TaskStatus,
    Versioned,
)
from backend.contracts.ports import AuditOutputStore
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.merge import AGENT_FIELDS, MergeRejected, check_patch, check_refs, field_type
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


def failed_tasks(patch: StatePatch) -> list[ReviewTask]:
    """片段里的 failed 任务 (整次失败时 A1 建的, 或 Agent 建的单对象失败任务), 不论是否已决定."""
    return [task for task in patch.get("review_tasks", []) if task.kind is TaskKind.failed]


async def stored_outputs(deps: AgentDeps, eid: str, writer: AgentId, run: ProcedureRun) -> StatePatch:
    """重放: 取回某次已是终态的执行在 Store 中的产出, 组成片段. 不调 Agent, LLM 与外部接口, 也不重取当前时间.

    节点写 Store 之后, 写检查点之前中断, 恢复时会重跑; 本次执行已在 Store 中且为终态时, 用它代替调 Agent.
    run: Store 中该执行的终态记录; writer: 执行它的 Agent, 按其写入范围 (merge.AGENT_FIELDS) 决定取哪些字段:
    - 带 run_id 的对象 (例外, 判断): 取该 run_id 下各对象的最新版本;
    - 不带 run_id 的产出 (评分卡, 分析结果, 重要性候选, 审计目标, 风险清单, 汇总表, 报告, 函证): 取 Store 最新版本,
      单个对象的字段 (如评分卡) 各只有一个固定 ID;
    - documents: 取 FinancialDataStore.documents.
    另取该 run_id 下的复核任务 (含 A1 代写的 failed 任务).
    再补齐引用, 使片段自成一体: 片段中对象 (判断, 评分卡, 分析结果) 的 exception_ids 与任务的例外目标所指的例外,
    取最新版本 (如 REV-02 把 REV-08 的例外置 attributed, 例外的 run_id 仍是 REV-08 那次); 片段中对象所属的
    其他执行记录也取回 (如回退检查点后重放第 1 次, 最新评分卡来自第 2 次, 其例外属于第 2 次执行).
    执行失败时 Agent 的片段当初已丢弃, 只取执行记录与任务. execute 与 fieldwork 共用.
    """
    store = deps.output_store
    fields = AGENT_FIELDS.get(writer, frozenset())
    patch: StatePatch = {"procedure_runs": [run]}
    tasks = await store.latest(eid, ReviewTask, run_id=run.id)
    if tasks or "review_tasks" in fields:
        patch["review_tasks"] = tasks
    if run.status is RunStatus.failed:
        return patch

    for field in sorted(fields - {"review_tasks"}):
        model, is_list = field_type(field)
        if field == "documents":
            patch[field] = await deps.financial_store.documents(eid)
        elif issubclass(model, BaseModel) and "run_id" in model.model_fields:
            patch[field] = await store.latest(eid, model, run_id=run.id)
        elif is_list:
            patch[field] = await store.latest(eid, model)
        else:
            found: list[Any] = await store.latest(eid, model)
            if found:
                patch[field] = found[0]

    if "exceptions" in fields:
        await add_referenced_exceptions(store, eid, patch)
    await add_referenced_runs(store, eid, patch)
    return patch


async def add_referenced_exceptions(store: AuditOutputStore, eid: str, patch: StatePatch) -> None:
    """把片段中对象引用而片段里还没有的例外 (Store 最新版本) 追加进 patch["exceptions"]."""
    referenced: list[str] = []
    for value in patch.values():
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, Judgment | AcceptanceScorecard | AnalyticsResult):
                referenced += item.exception_ids
            elif isinstance(item, ReviewTask) and item.target_type is TargetType.AuditException:
                referenced.append(item.target_id)
    have = {x.id for x in patch["exceptions"]}
    for exception_id in dict.fromkeys(referenced):  # 去重并保持顺序
        exception = None if exception_id in have else await store.get(eid, AuditException, exception_id)
        if exception is not None:
            patch["exceptions"].append(exception)
            have.add(exception_id)


async def add_referenced_runs(store: AuditOutputStore, eid: str, patch: StatePatch) -> None:
    """把片段中例外, 判断, 任务所属而片段里还没有的执行记录 (Store 中的) 追加进 patch["procedure_runs"]."""
    referenced = [item.run_id for field in ("exceptions", "judgments", "review_tasks") for item in patch.get(field, [])]
    have = {run.id for run in patch["procedure_runs"]}
    for run_id in dict.fromkeys(referenced):
        run = None if run_id in have else await store.get(eid, ProcedureRun, run_id)
        if run is not None:
            patch["procedure_runs"].append(run)
            have.add(run_id)


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

    async def attempt(
        self, state: EngagementState, engagement: Engagement, procedure_id: str, outstanding: list[ReviewTask]
    ) -> tuple[StatePatch, list[Procedure]]:
        """执行一次程序: 建执行记录, 调 Agent (或重放取 Store), 程序出新版本, 同一事务内写入 Store.

        state: 本次执行看到的状态, 其 procedure_runs 决定 run_no, procedure_pack 中本程序的版本为执行依据.
        outstanding: 本节点之前各次执行留下, 尚未决定的 failed 任务 (如 DOC-04 两份合同失败, 只重跑了一份).
        返回 (写入后的片段, 更新后的完整程序包). 程序新版本: 本次片段或 outstanding 中有待决的 failed 任务为
        awaiting_human, 否则为 completed; rerun_subject_ids 都清空 (只重判的对象只对紧接着的这一次执行有效).
        重放 (接口文档第 3 节): 本次执行已在 Store 中且为终态时不调 Agent, 产出从 Store 取回 (见 stored_outputs),
        程序新版本也取 Store 中已存的那一版, 其状态即当时是否等人, 不受之后的决定影响.
        """
        procedure = next(p for p in state["procedure_pack"] if p.id == procedure_id)
        template = self.deps.library.load(procedure_id)
        procedure_run = build_run(self.deps.library, template, procedure, engagement, state, datetime.now(UTC))
        ctx = RunContext(engagement=engagement, procedure=procedure, template=template, run=procedure_run, state=state)
        agent = self.agents[procedure_id]
        stored_run = await self.deps.output_store.get(engagement.id, ProcedureRun, procedure_run.id)
        if stored_run is not None and stored_run.status is not RunStatus.running:
            patch, writer = await stored_outputs(self.deps, engagement.id, agent.agent_id, stored_run), agent.agent_id
            # 取回的对象各自当初校验过, 拼成片段后再查一遍外键; 不成立说明 Store 与检查点对不上, 作节点错误
            problems = check_refs(patch, state)
            if problems:
                raise RuntimeError(f"重放 {stored_run.id} 取回的产出引用不完整: {'; '.join(problems)}")
        else:
            patch, writer = await self.run_agent(agent, ctx)
        pending = [t for t in [*failed_tasks(patch), *outstanding] if t.status is TaskStatus.pending]
        status = ProcedureStatus.awaiting_human if pending else ProcedureStatus.completed
        pack = update_procedure(state["procedure_pack"], procedure_id, status=status, rerun_subject_ids=[])
        updated = next(p for p in pack if p.id == procedure_id)  # 本程序的新版本
        # 片段与程序新版本在同一事务内写入, 保证原子性. 程序新版本取 put 的返回: 重放时为 Store 中已存的那一版
        async with self.deps.output_store.transaction(engagement.id):
            stored = await save_outputs(self.deps.output_store, engagement.id, patch, actor=writer)
            [saved] = await self.deps.output_store.put(engagement.id, [updated], actor=AgentId.A1)
        return stored, [cast(Procedure, saved) if p.id == procedure_id else p for p in pack]

    async def read_rerun(
        self, eid: str, record_id: str, failed: dict[str, ReviewTask], waiting: Procedure
    ) -> tuple[ReviewRecord, Procedure]:
        """读回审计师对某个 failed 任务的决定, 以及决定写出的程序版本. 返回 (决定, 程序版本).

        failed: 本节点各次执行建的, 还没有读回过决定的 failed 任务, 决定须针对其中之一; 已读回过决定的任务不在其中,
        同一决定 ID 被再次送来 (如在第二次等待时误用 ainvoke(None) 继续, LangGraph 沿用上次的恢复值) 会被拒绝.
        waiting: 等待时写出的 awaiting_human 程序版本. 程序版本取 waiting.version + 1, 即 rerun 决定在同一事务里写出的
        那一版 (executing, 含合并后的 params 与 rerun_subject_ids), 不取最新版本: 节点重放时会依次经过多次等待,
        最新版本可能是之后的决定写出的.
        决定不存在抛 LookupError; 不是 rerun, 不针对 failed 中的任务, 或其程序版本缺失, 不是 executing 时抛 ValueError.
        """
        store = self.deps.output_store
        record = await store.get(eid, ReviewRecord, record_id)
        if record is None:
            raise LookupError(f"ReviewRecord {record_id} not found")
        if record.decision is not Decision.rerun:
            raise ValueError(f"{record_id} 是 {record.decision}, {waiting.id} 失败后只能 rerun")
        if record.task_id not in failed:
            raise ValueError(f"{record_id} 处理的任务 {record.task_id} 不是 {waiting.id} 待处理的失败任务")
        procedure = await store.get(eid, Procedure, waiting.id, waiting.version + 1)
        if procedure is None or procedure.status is not ProcedureStatus.executing:
            raise ValueError(f"{record_id} 没有写出 {waiting.id} v{waiting.version + 1} executing")
        return record, procedure

    async def execute(self, state: EngagementState, procedure_id: str) -> StatePatch:
        """执行一条 P2 以外的程序, 失败时在节点内等人决定 rerun 后重跑, 直到不再有待决的失败 (接口文档第 7 节恢复机制).

        每次执行见 attempt. 执行后程序为 awaiting_human (本次或之前留下的 failed 任务还有待决的) 时, interrupt() 暂停;
        审计师对其中一个任务提交 rerun 决定后以 Command(resume=决定 ID) 恢复, 节点从头执行: 已结束的执行按重放规则
        取 Store, 各次 interrupt() 依次取回已提交的决定 ID. 取回决定后读回它写出的程序版本 (见 read_rerun),
        以 run_no + 1 再执行一次. 一次失败留下多个任务时, 逐个决定, 逐个重跑.
        返回历次执行写入的片段 (含失败的执行与已决定的任务), 读回的决定, 以及最终的完整程序包.
        执行期间不生成 executing 版本, 程序包中的原对象直接传给 Agent.
        Agent 的异常与片段校验不过转成失败执行与 failed 任务 (见 run_agent); 节点自身的错误 (项目缺失,
        参数解析不了, 写 Store 失败, 决定不对) 照常抛出, 由 runner 记入 run_error.
        """
        engagement = await self.deps.engagement_store.get(state["engagement_id"])
        if engagement is None:
            raise LookupError(f"Engagement {state['engagement_id']} not found")
        current = state  # 每次执行看到的状态: 带上之前各次的执行记录与程序新版本
        result: StatePatch = {}  # 历次执行写入的对象与读回的决定, 节点结束时一并并入状态
        failed: dict[str, ReviewTask] = {}  # 本节点各次执行建的, 还没有读回过决定的 failed 任务
        while True:
            patch, pack = await self.attempt(current, engagement, procedure_id, list(failed.values()))
            for key, value in patch.items():
                result[key] = [*result.get(key, []), *value] if isinstance(value, list) else value
            failed.update({task.id: task for task in failed_tasks(patch)})
            waiting = next(p for p in pack if p.id == procedure_id)
            if waiting.status is not ProcedureStatus.awaiting_human:
                return {**result, "procedure_pack": pack}

            run_id = patch["procedure_runs"][0].id
            # 节点在此暂停, 返回值不进状态, 失败执行与任务只在 Store 中; 总览按检查点里的中断显示"等复核".
            # 恢复时节点从头执行, 走到这里时 interrupt() 返回审计师决定的 ID
            record_id = interrupt({"awaiting": "human", "procedure_id": procedure_id, "run_id": run_id})
            record, procedure = await self.read_rerun(engagement.id, record_id, failed, waiting)
            # 被决定的任务已在 result 中: 节点能越过 interrupt() 时一定是在重放, 取回的是任务的最新版本 (decided)
            del failed[record.task_id]
            result["review_records"] = [*result.get("review_records", []), record]
            current = cast(
                EngagementState,
                {
                    **current,
                    "procedure_runs": [*current["procedure_runs"], *patch["procedure_runs"]],
                    "procedure_pack": [procedure if p.id == procedure_id else p for p in pack],
                },
            )

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
