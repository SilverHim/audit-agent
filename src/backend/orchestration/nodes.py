"""主图节点 (A 负责). 标 TODO 的节点在 M2 按 Demo需求文档.md 4.1 节与开发接口文档.md 第 3, 7 节实现.

闸门节点不做业务处理: 决定由 DecisionSubmit 写入, 并以 aupdate_state(as_node=闸门节点) 直接越过闸门.
"""

import copy
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict, cast, get_args, get_origin, get_type_hints

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
    AuditObjective,
    Decision,
    Engagement,
    Judgment,
    Misstatement,
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
from backend.contracts.ports import AuditOutputStore, EvidenceStore
from backend.contracts.state import EngagementState, StagedResult, StatePatch
from backend.orchestration.merge import (
    AGENT_FIELDS,
    TARGET_FIELDS,
    MergeRejected,
    check_ids,
    check_patch,
    check_refs,
    field_type,
)
from backend.orchestration.scheduler import build_schedule
from backend.orchestration.trail import phase_transition, procedure_transitions, record_transitions
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


def rerun_subjects(group: list[ReviewTask]) -> list[str]:
    """rerun 决定关闭的一组 failed 任务要重判的对象 (Q17 一轮失败算一组, 整组重跑): 各任务的 subject_id 去重排序;
    其中有整次失败的任务 (subject_id 为空) 时为空, 整条重跑."""
    if any(t.subject_id is None for t in group):
        return []
    return sorted({t.subject_id for t in group if t.subject_id is not None})


# 各图节点依次执行的 P2 以外程序 (接口文档第 3 节). 节点按它执行 (只取程序包中有的, 见 node_procedures),
# 恢复补做按它确定同一节点内复核决定的先后
NODE_PROCEDURES: dict[str, tuple[str, ...]] = {
    "p0_intel": ("ACC-04",),
    "p1_ingest": ("DOC-04",),
    "p1_analytics": ("PLN-07", "PLN-01"),
    "completion": ("FIN-04",),
    "report_draft": ("RPT-03",),
}


def node_procedures(node: str, pack: Iterable[Procedure], library: ProcedureLibrary) -> list[str]:
    """该图节点要执行的程序: 按 NODE_PROCEDURES 的顺序, 只取程序包中有的. 挂了功能名的程序 (模板 feature 不为空)
    不在程序包中时跳过: 功能未启用时不在固定集里 (services/features.py), 旧项目的程序包也可能缺后来新增的程序.
    基线程序 (feature 为空) 不在程序包中是程序包出错, 抛 LookupError, 不静默跳过."""
    present = {p.id for p in pack}
    missing = [pid for pid in NODE_PROCEDURES[node] if pid not in present and library.load(pid).feature is None]
    if missing:
        raise LookupError(f"{node}: 程序包中没有基线程序 {', '.join(missing)}")
    return [pid for pid in NODE_PROCEDURES[node] if pid in present]


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
    取最新版本 (如 REV-02 把 REV-08 的例外置 attributed, 例外的 run_id 仍是 REV-08 那次); 以片段中对象为目标的
    复核任务也取最新版本 (取回的例外是决定改出的新版本时, 以其旧版本为目标的任务在同一事务里已置 decided, 要一并取回,
    否则状态中的旧任务看上去过期); 片段中对象所属的其他执行记录也取回 (如回退检查点后重放第 1 次, 最新评分卡来自
    第 2 次, 其例外属于第 2 次执行).
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

    # 项目里只有一份的产出 (评分卡, 分析结果, 重要性候选, 汇总表) 取的是最新版本, 回退到较早的检查点重放时可能来自
    # 本程序之后的执行 (如 FIN-01 驳回后 FIN-04 的第 2 次); 把之后的执行一并取回, 下一次执行才会接着取号,
    # 不再撞上已有的执行而重放出同一版. 写这类产出的程序 (ACC-04, PLN-07, PLN-01, FIN-04) 不建单对象 failed 任务,
    # 成功的执行不会在节点内等待, 之后的执行只可能来自之后的节点执行, 正常重放时没有.
    # 之后的执行还有待决任务 (那次节点正在等人) 时, 重放恢复不了那次等待, 报错要求从最新的检查点恢复
    if any(field in patch and not field_type(field)[1] for field in fields):
        later = await store.latest(eid, ProcedureRun, procedure_id=run.procedure_id)
        later = sorted((r for r in later if r.run_no > run.run_no), key=lambda r: r.run_no)
        waiting = [
            t.id
            for r in later
            for t in await store.latest(eid, ReviewTask, run_id=r.id)
            if t.status is TaskStatus.pending
        ]
        if waiting:
            raise RuntimeError(
                f"重放 {run.id}: Store 中本程序之后的执行还有待决任务 {', '.join(waiting)} "
                f"(检查点回退到了那次等待之前), 重放恢复不了那次等待; 须从最新的检查点恢复"
            )
        patch["procedure_runs"] += later

    if "exceptions" in fields:
        await add_referenced_exceptions(store, eid, patch)
    await add_target_tasks(store, eid, patch)
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


async def add_target_tasks(store: AuditOutputStore, eid: str, patch: StatePatch) -> None:
    """把以片段中对象 (判断, 例外, 执行记录) 为目标而片段里还没有的复核任务 (Store 最新版本)
    追加进 patch["review_tasks"]."""
    have = {task.id for task in patch.get("review_tasks", [])}
    found: list[ReviewTask] = []
    for field in TARGET_FIELDS.values():
        for target in patch.get(field, []):
            for task in await store.latest(eid, ReviewTask, target_id=target.id):
                if task.id not in have:
                    found.append(task)
                    have.add(task.id)
    if found:
        patch["review_tasks"] = [*patch.get("review_tasks", []), *found]


async def add_referenced_runs(store: AuditOutputStore, eid: str, patch: StatePatch) -> None:
    """把片段中例外, 判断, 任务所属而片段里还没有的执行记录 (Store 中的) 追加进 patch["procedure_runs"]."""
    referenced = [item.run_id for field in ("exceptions", "judgments", "review_tasks") for item in patch.get(field, [])]
    have = {run.id for run in patch["procedure_runs"]}
    for run_id in dict.fromkeys(referenced):
        run = None if run_id in have else await store.get(eid, ProcedureRun, run_id)
        if run is not None:
            patch["procedure_runs"].append(run)
            have.add(run_id)


def uncovered_objectives(library: ProcedureLibrary, objectives: list[AuditObjective]) -> list[str]:
    """按各目标当前的风险等级, 没有任何程序 trigger 匹配到的审计目标 ID, 按 objectives 顺序.

    装配时有这样的目标即阻断 (Demo需求文档第 4.1 节); PLN-01/03 闸门呈报缺口, 有缺口时不能通过.
    """
    return [o.id for o in objectives if not library.match(o)]


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


async def save_outputs(
    output_store: AuditOutputStore,
    eid: str,
    patch: StatePatch,
    actor: str,
    evidence_store: EvidenceStore | None = None,
) -> StatePatch:
    """将状态片段中的审计产出写入 Store, 返回以 Store 返回对象替换后的片段, 供节点写入状态.

    按值的类型处理:
    - Versioned 对象 (如评分卡): 写入, 取 Store 返回的对象;
    - 元素均为 Versioned 的列表 (如例外): 整体写入, 取 Store 返回的列表;
    - documents (证据文件登记): 逐份调 evidence_store.set_status 写入文件的处理状态 (如 extracted), 原样保留;
    - 其他值 (如 phase, 调度表): 不写入, 原样保留.
    Store 返回的通常即写入的对象; 重放时同 (id, version) 已存在, 不重复写入并返回已存对象, 使状态与 Store 一致.
    事务由调用方开启; set_status 在事务块内调用时加入该事务 (接口文档第 4 节).
    """
    result: StatePatch = {}
    for key, value in patch.items():
        if isinstance(value, Versioned):
            stored_list = await output_store.put(eid=eid, objs=[value], actor=actor)
            result[key] = stored_list[0]
        elif key == "documents":
            if evidence_store is None:
                raise ValueError("片段含 documents, 须传 evidence_store")
            for document in value:
                await evidence_store.set_status(eid, document.id, document.status)
            result[key] = value
        elif isinstance(value, list) and all(isinstance(v, Versioned) for v in value):
            result[key] = await output_store.put(eid=eid, objs=value, actor=actor)
        else:
            result[key] = value
    return result


# 状态中带合并规则 (reducer) 的字段 -> 合并函数, 取自 EngagementState 的 Annotated 注解
REDUCERS = {
    name: get_args(hint)[1]
    for name, hint in get_type_hints(EngagementState, include_extras=True).items()
    if get_origin(hint) is Annotated
}


def apply_patch(state: EngagementState, patch: StatePatch) -> EngagementState:
    """按状态的合并规则把片段并入状态的副本, 原状态不变. 节点内依次执行几条程序时, 后一条据此看到前一条的结果.

    带 reducer 的字段 (如 procedure_runs 的 upsert_by_id) 用 reducer 合并, 其余字段直接替换, 与 LangGraph 一致.
    """
    merged: dict[str, Any] = dict(state)
    for key, value in patch.items():
        reducer = REDUCERS.get(key)
        merged[key] = reducer(state[key], value) if reducer else value  # type: ignore[literal-required]
    return cast(EngagementState, merged)


def merge_patch(result: StatePatch, patch: StatePatch) -> None:
    """把一个片段并进节点的累计结果. 带 reducer 的列表字段拼接 (并入状态时由 reducer 按 ID 与版本去重);
    其余字段后者覆盖, 包括每次都传完整列表的 procedure_pack, audit_objectives, risk_register."""
    for key, value in patch.items():
        result[key] = [*result.get(key, []), *value] if key in REDUCERS else value


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

    async def perform(
        self, state: EngagementState, engagement: Engagement, procedure_id: str, instance: str | None = None
    ) -> StagedResult:
        """执行一条程序一次, 不写 Store: 建执行记录, 调 Agent (见 run_agent). attempt 与 fieldwork 共用.

        state: 本次执行看到的状态, 其 procedure_runs 决定 run_no, procedure_pack 中本程序的版本为执行依据.
        instance: fieldwork 实例名, 记入执行记录; P2 以外为空.
        返回程序编号, 执行记录 ID, 写入者与要写入的片段.
        重放 (接口文档第 3 节): 本次执行已在 Store 中且为终态时不调 Agent, 产出从 Store 取回 (见 stored_outputs);
        取回的执行是失败的, 写入者为 A1.
        """
        procedure = next(p for p in state["procedure_pack"] if p.id == procedure_id)
        template = self.deps.library.load(procedure_id)
        procedure_run = build_run(self.deps.library, template, procedure, engagement, state, datetime.now(UTC))
        procedure_run = procedure_run.model_copy(update={"instance": instance})
        agent = self.agents[procedure_id]
        stored_run = await self.deps.output_store.get(engagement.id, ProcedureRun, procedure_run.id)
        if stored_run is None or stored_run.status is RunStatus.running:
            ctx = RunContext(
                engagement=engagement, procedure=procedure, template=template, run=procedure_run, state=state
            )
            patch, writer = await self.run_agent(agent, ctx)
        else:
            patch = await stored_outputs(self.deps, engagement.id, agent.agent_id, stored_run)
            # 取回的对象各自当初校验过, 拼成片段后再查一遍外键; 不成立说明 Store 与检查点对不上, 作节点错误
            problems = check_refs(patch, state)
            if problems:
                raise RuntimeError(f"重放 {stored_run.id} 取回的产出引用不完整: {'; '.join(problems)}")
            writer = AgentId.A1 if stored_run.status is RunStatus.failed else agent.agent_id
        return StagedResult(procedure_id=procedure_id, run_id=procedure_run.id, writer=writer, patch=patch)

    async def attempt(
        self,
        state: EngagementState,
        engagement: Engagement,
        procedure_id: str,
        phase: Phase | None = None,
    ) -> tuple[StatePatch, list[Procedure]]:
        """执行一次程序 (见 perform), 程序出新版本, 同一事务内写入 Store. 执行 P2 以外的程序用.

        state: 本次执行看到的状态, 见 perform.
        phase: 节点同时推进的阶段 (如 completion 置 P3_Completion), 在同一事务内写入 engagements 表; 为空不写.
        返回 (写入后的片段, 更新后的完整程序包). 程序新版本: 本次片段中有待决的 failed 任务为 awaiting_human, 否则为
        completed (闸门程序为 planned); rerun_subject_ids 都清空 (只重判的对象只对紧接着的这一次执行有效).
        之前各次执行留下的 failed 任务都已由读回的 rerun 决定整组关闭 (Q17), 不再拖住程序.
        重放时程序新版本也取 Store 中已存的那一版, 其状态即当时是否等人, 不受之后的决定影响.
        提交后写程序状态与阶段的 transition 留痕 (原阶段取 state, 重放时同键跳过).
        """
        result = await self.perform(state, engagement, procedure_id)
        patch, writer = result["patch"], result["writer"]
        template = self.deps.library.load(procedure_id)
        pending = [t for t in failed_tasks(patch) if t.status is TaskStatus.pending]
        # 闸门程序 (如 PLN-01) 执行成功后保持 planned, 由 DecisionSubmit 在闸门 approve 时置 completed (Q5)
        done = ProcedureStatus.planned if template.gate is not None else ProcedureStatus.completed
        status = ProcedureStatus.awaiting_human if pending else done
        pack = update_procedure(state["procedure_pack"], procedure_id, status=status, rerun_subject_ids=[])
        updated = next(p for p in pack if p.id == procedure_id)  # 本程序的新版本
        # 新执行 (不是重放) 时 Store 中本程序可能已有更新的版本 (回退到较早的检查点后, 之前的执行写过), 接在其后取号,
        # 不撞已有版本
        if await self.deps.output_store.get(engagement.id, ProcedureRun, result["run_id"]) is None:
            latest = await self.deps.output_store.get(engagement.id, Procedure, procedure_id)
            if latest is not None and latest.version >= updated.version:
                updated = updated.model_copy(update={"version": latest.version + 1})
                pack = [updated if p.id == procedure_id else p for p in pack]
        # 片段与程序新版本在同一事务内写入, 保证原子性. 程序新版本取 put 的返回: 重放时为 Store 中已存的那一版
        async with self.deps.output_store.transaction(engagement.id):
            stored = await save_outputs(
                self.deps.output_store, engagement.id, patch, actor=writer, evidence_store=self.deps.evidence_store
            )
            [saved] = await self.deps.output_store.put(engagement.id, [updated], actor=AgentId.A1)
            if phase is not None:
                await self.deps.engagement_store.update(engagement.id, phase=phase)
        entries = await procedure_transitions(self.deps.output_store, engagement.id, [saved])
        if phase is not None:
            entries += phase_transition(engagement.id, state["phase"], phase, procedure_id)
        await record_transitions(self.deps.trail, entries)
        return stored, [cast(Procedure, saved) if p.id == procedure_id else p for p in pack]

    async def read_rerun(
        self, eid: str, record_id: str, failed: dict[str, ReviewTask], waiting: Procedure
    ) -> tuple[ReviewRecord, Procedure]:
        """读回审计师对某个 failed 任务的决定, 以及决定写出的程序版本. 返回 (决定, 程序版本).

        failed: 正在等待的这一轮执行建的 failed 任务, 同属一组 (Q17), 决定须针对其中之一: 第 k 次等待只接受针对
        第 k 轮的决定. 已读回过的决定 ID 被再次送来 (LangGraph 沿用上次的恢复值) 时, execute 在调用前就跳过, 不到这里.
        waiting: 等待时写出的 awaiting_human 程序版本. 程序版本取 waiting.version + 1, 即 rerun 决定在同一事务里写出的
        那一版 (executing, 含合并后的 params 与 rerun_subject_ids), 不取最新版本: 节点重放时会依次经过多次等待,
        最新版本可能是之后的决定写出的. 这一版的 rerun_subject_ids 须覆盖整组 (见 rerun_subjects), 否则决定没有
        关闭整组, 只重跑一部分会把程序置 completed 而留下待决的任务.
        决定不存在抛 LookupError; 不是 rerun, 不针对 failed 中的任务, 或其程序版本缺失, 不是 executing, 没覆盖整组
        时抛 ValueError.
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
        expected = rerun_subjects(list(failed.values()))
        if procedure.rerun_subject_ids != expected:
            raise ValueError(
                f"{record_id} 写出的 {waiting.id} v{procedure.version} 重判对象为 {procedure.rerun_subject_ids}, "
                f"应覆盖整组 {expected or '(整条重跑)'}"
            )
        return record, procedure

    async def execute(self, state: EngagementState, procedure_id: str, phase: Phase | None = None) -> StatePatch:
        """执行一条 P2 以外的程序, 失败时在节点内等人决定 rerun 后重跑, 直到不再有待决的失败 (接口文档第 7 节恢复机制).

        每次执行见 attempt. 执行后程序为 awaiting_human (本次执行留下待决的 failed 任务) 时, interrupt() 暂停;
        审计师对其中一个任务提交 rerun 决定后以 Command(resume=决定 ID) 恢复, 节点从头执行: 已结束的执行按重放规则
        取 Store, 各次 interrupt() 依次取回已提交的决定 ID. 取回决定后读回它写出的程序版本 (见 read_rerun),
        以 run_no + 1 再执行一次. 一次失败留下多个任务时算一组 (Q17): 对其中任一个 rerun 即整组关闭, 下一轮一起重跑
        这组的全部对象; 第 k 次等待只接受针对第 k 轮任务的决定.
        返回历次执行写入的片段 (含失败的执行与已决定的任务), 读回的决定, 以及最终的完整程序包.
        phase: 节点同时推进的阶段 (见 attempt), 不为空时返回的片段带上它.
        执行期间不生成 executing 版本, 程序包中的原对象直接传给 Agent.
        Agent 的异常与片段校验不过转成失败执行与 failed 任务 (见 run_agent); 节点自身的错误 (项目缺失,
        参数解析不了, 写 Store 失败, 决定不对) 照常抛出, 由 runner 记入 run_error.
        """
        engagement = await self.deps.engagement_store.get(state["engagement_id"])
        if engagement is None:
            raise LookupError(f"Engagement {state['engagement_id']} not found")
        current = state  # 每次执行看到的状态: 带上之前各次的执行记录与程序新版本
        result: StatePatch = {}  # 历次执行写入的对象与读回的决定, 节点结束时一并并入状态
        while True:
            patch, pack = await self.attempt(current, engagement, procedure_id, phase)
            merge_patch(result, patch)
            waiting = next(p for p in pack if p.id == procedure_id)
            if waiting.status is not ProcedureStatus.awaiting_human:
                return {**result, "procedure_pack": pack, **({"phase": phase} if phase is not None else {})}

            run_id = patch["procedure_runs"][0].id
            # 这一轮执行建的 failed 任务, 同属一组 (Q17); 重放时取回的是最新版本 (可能已 decided), 也算在内.
            # 按 run_id 过滤作防御: 单份产出程序的成功执行重放时, stored_outputs 会把之后各轮的执行与任务一并取回
            # (目前 P2 以外的单份产出程序只会整次失败, 失败执行重放只取回自己的任务)
            failed = {task.id: task for task in failed_tasks(patch) if task.run_id == run_id}
            # 节点在此暂停, 返回值不进状态, 失败执行与任务只在 Store 中; 总览按检查点里的中断显示"等复核".
            # 恢复时节点从头执行, 走到这里时 interrupt() 返回审计师决定的 ID.
            # records 列出已读回的决定: 本次调用读回的, 加上传入状态里的 (同一图节点先执行的程序读回的, 如 p1_analytics
            # 中 PLN-07 重跑用过的决定, 已在传给 PLN-01 的状态里). 它们在图节点返回前不进检查点,
            # DecisionSubmit 据此判断它们不是"须先恢复"的; 列出检查点里已有的也无妨
            read = [*state["review_records"], *result.get("review_records", [])]
            applied = list(dict.fromkeys(r.id for r in read))
            payload = {"awaiting": "human", "procedure_id": procedure_id, "run_id": run_id, "records": applied}
            record_id = interrupt(payload)
            # 送来的是已读回过的决定, 不是新决定, 再等一次. 节点读回决定后出错 (下一次等待还没登记) 时, LangGraph
            # 在之后的每次运行里都会把那次的恢复值再交给一个 interrupt(): 重放时第一次等待取到它, 下一次等待又取到它.
            # 每次 interrupt() 各占一个序号, 跳过的也一样, 重放时按同样的顺序跳过
            while record_id in applied:
                record_id = interrupt(payload)
            record, procedure = await self.read_rerun(engagement.id, record_id, failed, waiting)
            # 整组任务已在 result 中: 节点能越过 interrupt() 时一定是在重放, 取回的是任务的最新版本 (decided)
            result["review_records"] = [*result.get("review_records", []), record]
            current = cast(
                EngagementState,
                {
                    **current,
                    "procedure_runs": [*current["procedure_runs"], *patch["procedure_runs"]],
                    "procedure_pack": [procedure if p.id == procedure_id else p for p in pack],
                },
            )

    async def run_node(self, state: EngagementState, node: str, phase: Phase | None = None) -> StatePatch:
        """依次执行该图节点的程序 (见 node_procedures), 后一条在前一条的结果之上执行 (程序包, 执行记录等按状态的
        合并规则并入). 返回各次的片段合并, 程序包为最终的. phase 见 execute, 每条都带 (同值不重复留痕)."""
        result: StatePatch = {}
        for procedure_id in node_procedures(node, state["procedure_pack"], self.deps.library):
            patch = await self.execute(state, procedure_id, phase)
            merge_patch(result, patch)
            state = apply_patch(state, patch)
        return result

    # ── P0 ──
    async def p0_intel(self, state: EngagementState) -> StatePatch:
        """A2 执行 ACC-04, 产出承接评分卡."""
        return await self.run_node(state, "p0_intel")

    async def gate(self, state: EngagementState) -> StatePatch:
        return {}  # 四道闸门共用, 不处理业务; 出口由路由函数按 GateDecision 决定

    # ── P1 ──
    async def p1_ingest(self, state: EngagementState) -> StatePatch:
        """载入合成数据集 (财务表入库, 合同 PDF 登记为证据文件), 再由 A3 执行 DOC-04 抽取合同要素.

        DatasetLoader.load 已载入则跳过, 节点重放时再调一次无妨.
        """
        engagement = await self.deps.engagement_store.get(state["engagement_id"])
        if engagement is None:
            raise LookupError(f"Engagement {state['engagement_id']} not found")
        await self.deps.datasets.load(engagement.id, engagement.dataset)
        return await self.run_node(state, "p1_ingest")

    async def p1_analytics(self, state: EngagementState) -> StatePatch:
        """A4 依次执行 PLN-07 (初步分析) 与 PLN-01 (重要性候选, 首次带出数据集预置的审计目标与风险清单).

        PLN-01 在 PLN-07 的结果之上执行 (见 run_node). 返回两次的片段合并, 程序包为最终的.
        """
        return await self.run_node(state, "p1_analytics")

    async def assemble_pack(self, state: EngagementState) -> StatePatch:
        """装配 P2 程序包: 按审计目标匹配 trigger, 补齐依赖闭包, 只把尚不在程序包中的 P2 程序加入.

        每条审计目标至少要被一条程序的 trigger 匹配到, 否则抛 ValueError 阻断 (Demo需求文档第 4.1 节), 什么都不写.
        新加入的程序第 1 版写入 Store, 阶段 P2_Executing 写入 engagements 表, 两者同一事务 (第 3 节).
        objective_ids 为该程序直接匹配的审计目标; 只经依赖加入的 (如 DOC-06) 为空.
        没有新程序时不写也不改阶段. 节点重放时 Store 已有这些第 1 版, put 跳过, 阶段同值再写一次.
        提交后写阶段的 transition 留痕 (新程序是第 1 版, 不写).
        """
        library = self.deps.library
        objectives = state["audit_objectives"]
        uncovered = uncovered_objectives(library, objectives)
        if uncovered:
            raise ValueError(f"审计目标没有程序覆盖, 不能装配: {', '.join(uncovered)}")
        matched = {t.id for o in objectives for t in library.match(o)}
        existing = {p.id for p in state["procedure_pack"]}
        added = [
            Procedure(
                id=t.id,
                phase=t.phase,
                template_commit=library.commit,
                objective_ids=[o.id for o in objectives if t in library.match(o)],
                autonomy_level=t.autonomy_level,
            )
            for t in library.closure(sorted(matched))
            if t.phase is Phase.P2_Executing and t.id not in existing
        ]
        if not added:
            return {}
        eid = state["engagement_id"]
        async with self.deps.output_store.transaction(eid):
            stored = await self.deps.output_store.put(eid, added, actor=AgentId.A1)
            await self.deps.engagement_store.update(eid, phase=Phase.P2_Executing)
        await record_transitions(self.deps.trail, phase_transition(eid, state["phase"], Phase.P2_Executing))
        return {"procedure_pack": [*state["procedure_pack"], *stored], "phase": Phase.P2_Executing}

    # ── P2 ──
    async def schedule(self, state: EngagementState) -> StatePatch:
        return {"schedule_table": build_schedule(state["procedure_pack"], self.deps.library, self.parallel_cap)}

    async def fieldwork(self, payload: FieldworkInput) -> StatePatch:
        """一个 fieldwork 实例: 依次执行派给它的程序各一次, 结果放进 staged_results, 不写 Store (接口文档第 3 节).

        程序由负责的 Agent 执行 (多为 A6, 另有 A3 的 DOC-06, A5 的 CSH-04), 执行记录带实例名.
        各程序都以派发时的状态快照执行: 同一实例的程序在调度表的同一层, 互不依赖.
        每条程序一个暂存项 (见 perform): Agent 抛异常或片段校验不过时为 A1 代写的失败执行与 failed 任务,
        同实例的其余程序照常执行; 只拦 Exception, 取消 (asyncio.CancelledError) 等照常抛出.
        Store 中已有本次执行的终态时按重放规则取回, 不调 Agent.
        写入 Store, 并入状态与程序新版本都由 fan_in 完成.
        """
        state = payload["state"]
        engagement = await self.deps.engagement_store.get(state["engagement_id"])
        if engagement is None:
            raise LookupError(f"Engagement {state['engagement_id']} not found")
        staged = [await self.perform(state, engagement, pid, payload["instance"]) for pid in payload["procedure_ids"]]
        return {"staged_results": staged}

    async def fan_in(self, state: EngagementState) -> StatePatch:
        """汇合本轮各 fieldwork 实例的暂存结果: 一个事务内写入 Store, 以 Store 返回的对象并入状态, 清空 staged_results.

        各暂存项依次处理. 先对已并入本轮前面各项的状态再查一遍 ID, 版本与外键 (merge.check_ids, check_refs):
        各项在 fieldwork 中都按派发时的状态校验过, 但同轮几项合起来可能冲突, 如前一项把某例外改出 v2, 后一项仍交回
        v1, 或建了指向 v1 的待决任务. 不过的项丢弃片段, 改为 A1 写的失败执行与 failed 任务 (结束时间沿用原执行的),
        其余项照常写入. 字段归属与本次执行的终态只在 fieldwork 查: A1 的失败片段与重放取回的片段按 Agent 的规则会被误拒.
        片段按写入者 put, 含 documents 时在同一事务内写文件状态 (见 save_outputs).
        本轮执行过的程序在同一事务内各出一个新版本 (Demo需求文档第 4.1 节): 该程序还有待决的复核任务
        (状态中已有的与本轮新建的, 同 ID 取新版本) 为 awaiting_human, 否则为 completed; rerun_subject_ids 清空.
        重放: 对象已在 Store 中时 put 跳过并返回已存对象, 程序新版本同理, 返回与首次相同.
        提交后写程序状态的 transition 留痕.
        没有暂存结果 (如 await_events 之后回到这里) 时只清空, 不写.
        """
        staged = state["staged_results"]
        if not staged:
            return {"staged_results": None}
        eid = state["engagement_id"]
        store = self.deps.output_store
        executed = list(dict.fromkeys(item["procedure_id"] for item in staged))
        result: StatePatch = {}
        current = state  # 已并入本轮前面各项的状态, 用来校验后面的项
        async with store.transaction(eid):
            for item in staged:
                patch, writer = item["patch"], item["writer"]
                problems = [*check_ids(patch, current), *check_refs(patch, current)]
                if problems:
                    run = next(r for r in patch["procedure_runs"] if r.id == item["run_id"])
                    ended_at = run.ended_at or datetime.now(UTC)
                    patch, writer = fail_run(self.deps.queue, run, str(MergeRejected(problems)), ended_at), AgentId.A1
                saved = await save_outputs(store, eid, patch, actor=writer, evidence_store=self.deps.evidence_store)
                merge_patch(result, saved)
                current = apply_patch(current, saved)
            tasks = current["review_tasks"]
            pack = state["procedure_pack"]
            for procedure_id in executed:
                pending = any(t.procedure_id == procedure_id and t.status is TaskStatus.pending for t in tasks)
                status = ProcedureStatus.awaiting_human if pending else ProcedureStatus.completed
                pack = update_procedure(pack, procedure_id, status=status, rerun_subject_ids=[])
            stored = await store.put(eid, [p for p in pack if p.id in executed], actor=AgentId.A1)
        await record_transitions(self.deps.trail, await procedure_transitions(store, eid, stored))
        saved_versions = {p.id: cast(Procedure, p) for p in stored}
        pack = [saved_versions.get(p.id, p) for p in pack]
        return {**result, "procedure_pack": pack, "staged_results": None}

    async def await_events(self, state: EngagementState) -> StatePatch:
        """等审计师处理 P2 的复核任务: interrupt() 暂停, 决定写入 Store 后以 Command(resume=决定 ID) 恢复.

        恢复后从 Store 读回复核决定, 任务, 判断, 例外, 错报与程序的最新版本, 整体并入状态 (接口文档第 7 节),
        之后回 fan_in 重新判断去向. 决定写入的对象都在其中, 不必按决定类型逐一推断;
        程序包按状态中的顺序换成 Store 的最新版本.
        恢复值在 Store 中没有对应的决定 (如绕过 DecisionSubmit 传入未写入的 ID) 时抛 LookupError, 写明原因与恢复办法:
        LangGraph 把恢复值交给 interrupt() 后, 节点出错再恢复仍取回同一个值, 须以 fan_in 身份重写检查点,
        再让图继续执行一步, 重新停到等复核.
        """
        eid = state["engagement_id"]
        store = self.deps.output_store
        record_id = interrupt({"awaiting": "review"})
        if await store.get(eid, ReviewRecord, record_id) is None:
            raise LookupError(
                f"复核决定 {record_id} 不在 Store 中, 等复核的节点读不回它: 复核决定须先经 DecisionSubmit 写入 Store, "
                f"再以其 ID 恢复. 这个 ID 已交给节点, 之后的恢复仍会取回它并再次报错; "
                f'恢复办法: 先以 fan_in 身份重写检查点 (aupdate_state(config, {{}}, as_node="fan_in")), '
                f"再让图继续执行 (ainvoke(None, config)), 图重新停到等复核后再提交决定"
            )
        latest = {p.id: p for p in await store.latest(eid, Procedure)}
        return {
            "review_records": await store.latest(eid, ReviewRecord),
            "review_tasks": await store.latest(eid, ReviewTask),
            "judgments": await store.latest(eid, Judgment),
            "exceptions": await store.latest(eid, AuditException),
            "misstatements": await store.latest(eid, Misstatement),
            "procedure_pack": [latest.get(p.id, p) for p in state["procedure_pack"]],
        }

    # ── P3, P4 ──
    async def completion(self, state: EngagementState) -> StatePatch:
        """P2 结清后进入完成阶段: A9 执行 FIN-04, 产出未更正错报汇总表 SCHEDULE (下一版) 与候选意见.

        阶段置 P3_Completion, 与 FIN-04 的执行写入同一事务 (第 3 节). FIN-01 驳回后回到这里时以 rerun 再执行一次,
        出 SCHEDULE 下一版.
        """
        return await self.run_node(state, "completion", phase=Phase.P3_Completion)

    async def report_draft(self, state: EngagementState) -> StatePatch:
        """A10 执行 RPT-03, 出审计报告的下一修订 (REPORT-{revision}, 第 1 版); RPT-01 退回后回到这里, 按批注修订,
        以 rerun 再执行一次. 执行与写入见 execute."""
        return await self.run_node(state, "report_draft")

    async def archive(self, state: EngagementState) -> StatePatch:
        """终点: 项目已由 RPT-01 签字锁定, 不再写 Store. 补写进入 Archived 的阶段留痕 (只能由报告阶段进入;
        DecisionSubmit 提交后已写过, 同键跳过)."""
        eid = state["engagement_id"]
        await record_transitions(self.deps.trail, phase_transition(eid, Phase.P4_Reporting, Phase.Archived, "RPT-01"))
        return {}

    async def declined(self, state: EngagementState) -> StatePatch:
        """终点: 项目已由 ACC-01 拒绝承接锁定, 不再写 Store. 补写进入 Declined 的阶段留痕 (只能由承接阶段进入;
        DecisionSubmit 提交后已写过, 同键跳过)."""
        eid = state["engagement_id"]
        await record_transitions(self.deps.trail, phase_transition(eid, Phase.P0_Acceptance, Phase.Declined, "ACC-01"))
        return {}
