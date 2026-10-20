"""Agent 片段合并前的校验 (A1). 依据: 开发接口文档第 3 节字段写入归属, 第 6 节 Agent 返回字段表.

P2 以外的节点在写 Store 之前对 Agent.run 的返回调用 check_patch, fan_in 对每个暂存结果调用.
不过时抛 MergeRejected, 由调用方把本次执行标 failed, 片段不写 Store 也不并入状态.
"""

from collections.abc import Collection
from enum import StrEnum
from typing import Any, get_args, get_origin, get_type_hints

from backend.contracts.models import (
    AcceptanceScorecard,
    AgentId,
    AnalyticsResult,
    AuditException,
    Judgment,
    ProcedureRun,
    ReviewTask,
    RunStatus,
    TargetType,
    TaskStatus,
    Versioned,
)
from backend.contracts.state import EngagementState, StatePatch
from backend.services.procedure_library import ProcedureLibrary

# 各 Agent 可写的状态字段, 即第 6 节返回字段表; 另外都可写 procedure_runs (本次执行的终态)
AGENT_FIELDS: dict[AgentId, frozenset[str]] = {
    AgentId.A2: frozenset({"acceptance_scorecard", "exceptions"}),
    AgentId.A3: frozenset({"documents", "confirmations", "review_tasks"}),
    AgentId.A4: frozenset({"analytics", "materiality_candidates", "audit_objectives", "risk_register", "exceptions"}),
    AgentId.A5: frozenset({"confirmations", "documents", "exceptions", "ownership_graph"}),
    AgentId.A6: frozenset({"exceptions", "judgments", "review_tasks", "confirmations"}),
    AgentId.A7: frozenset(),  # GEN-09 由 A6 同步调用, 结果写在 A6 的判断上, A7 自己不返回状态片段
    AgentId.A9: frozenset({"uncorrected_schedule"}),
    AgentId.A10: frozenset({"report_versions", "review_tasks"}),
}


class Replay(StrEnum):
    """重放时一个状态字段从 Store 取回的方式 (接口文档第 3 节重放规则)."""

    by_run = "by_run"  # 该 run_id 下各 ID 的最新版本 (对象带 run_id)
    latest_all = "latest_all"  # 全部对象的最新版本
    documents = "documents"  # FinancialDataStore.documents 中本程序声明类型 (document_types) 的文件
    singleton = "singleton"  # 固定 ID 的最新版本, 并取回本程序之后的执行; 模型带 run_id 时核对它属于本程序
    by_procedure = "by_procedure"  # 各 ID 最新版本中 run_id 属于本程序的, 并取回本程序之后的执行


# 可由 Agent 写的状态字段 → 重放取回方式. 新字段在此登记 (第 3 节新增状态字段的登记清单)
FIELD_REPLAY: dict[str, Replay] = {
    "exceptions": Replay.by_run,
    "judgments": Replay.by_run,
    "review_tasks": Replay.by_run,
    "confirmations": Replay.latest_all,
    "audit_objectives": Replay.latest_all,
    "risk_register": Replay.latest_all,
    "report_versions": Replay.latest_all,
    "misstatements": Replay.latest_all,
    "documents": Replay.documents,
    "acceptance_scorecard": Replay.singleton,
    "analytics": Replay.singleton,
    "materiality_candidates": Replay.singleton,
    "uncorrected_schedule": Replay.singleton,
    "ownership_graph": Replay.singleton,
}
SHAREABLE = frozenset(Replay) - {Replay.singleton}  # 两条以上程序声明同一字段时可用的取回方式
LATER_RUNS = frozenset({Replay.singleton, Replay.by_procedure})  # 取回本程序之后的执行的取回方式


def output_problems(library: ProcedureLibrary) -> list[str]:
    """程序模板 outputs 声明的问题 (含未启用的模板), 每条含程序编号与字段: 字段不在 owner 的写入范围内, 没有登记
    取回方式, 或两条以上程序声明同一字段而其取回方式为 singleton (项目里只有一份, 分不清是谁写的);
    声明了 documents 而没写 document_types (重放取不回文件), 或写了 document_types 而没声明 documents."""
    problems: list[str] = []
    declared: dict[str, list[str]] = {}
    for t in library.all(include_disabled=True):
        if "documents" in t.outputs and not t.document_types:
            problems.append(f"{t.id}: 声明产出 documents 而没写 document_types")
        if t.document_types and "documents" not in t.outputs:
            problems.append(f"{t.id}: 写了 document_types 而没声明产出 documents")
        for field in t.outputs:
            declared.setdefault(field, []).append(t.id)
            if field not in AGENT_FIELDS.get(t.owner, frozenset()):
                problems.append(f"{t.id}: {field} 不在 {t.owner} 的写入范围内")
            if field not in FIELD_REPLAY:
                problems.append(f"{t.id}: {field} 没有登记重放取回方式 (merge.FIELD_REPLAY)")
    for field, ids in declared.items():
        if len(ids) > 1 and FIELD_REPLAY.get(field) is Replay.singleton:
            problems.append(f"{', '.join(ids)}: 都声明 {field}, 而其取回方式为 singleton")
    return problems


def check_outputs(library: ProcedureLibrary) -> None:
    """启动校验 (GraphNodes 构造时): output_problems 非空时抛 ValueError."""
    problems = output_problems(library)
    if problems:
        raise ValueError("程序模板的 outputs 不对: " + "; ".join(problems))


# 复核任务指向的对象存在状态的哪个字段
TARGET_FIELDS: dict[TargetType, str] = {
    TargetType.Judgment: "judgments",
    TargetType.AuditException: "exceptions",
    TargetType.ProcedureRun: "procedure_runs",
}

STATE_HINTS = get_type_hints(EngagementState)  # 状态各字段的类型注解, 已去掉 reducer (Annotated)


class MergeRejected(Exception):
    """Agent 片段校验不过. reasons 为全部原因; 异常文本以 merge_rejected 开头, 可直接作失败 Run 的 failure_reason."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("merge_rejected: " + "; ".join(reasons))
        self.reasons = reasons  # 每条一个原因, 含字段名或对象 ID


def field_type(field: str) -> tuple[type, bool]:
    """状态字段的元素类型, 以及它是否为列表. 取自 EngagementState 的注解.

    例: judgments 注解为 list[Judgment], 返回 (Judgment, True); acceptance_scorecard 注解为
    AcceptanceScorecard | None, 返回 (AcceptanceScorecard, False).
    """
    hint = STATE_HINTS[field]
    if get_origin(hint) is list:
        return get_args(hint)[0], True
    return next(arg for arg in get_args(hint) if arg is not type(None)), False


def objects(value: Any) -> list[Any]:
    """片段或状态中一个字段的值统一成对象列表: 列表原样返回, 单个对象包成一项, None 为空列表."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def known(patch: StatePatch, state: EngagementState, field: str) -> dict[str, Any]:
    """某字段在状态与片段中的全部对象, 按 ID 索引; 同一 ID 以片段中的为准 (片段是更新的版本)."""
    found = {item.id: item for item in objects(state.get(field))}
    found.update({item.id: item for item in objects(patch.get(field))})
    return found


def check_fields(
    patch: StatePatch, writer: AgentId, procedure_id: str = "", outputs: Collection[str] | None = None
) -> list[str]:
    """字段归属与类型: 每个字段都在该 Agent 的写入范围内, 且在本程序声明的产出 (outputs) 之内;
    值的类型与状态注解一致 (列表逐项查). outputs 为 None 时不查声明 (A1 代写的失败片段).
    文件的类型不查 (document_types 只管重放): CSH-04 存档按哈希去重, 可能取回审计师先上传的同内容文件, 其类型
    不一定是 confirmation, 合并时不因此判失败."""
    allowed = AGENT_FIELDS.get(writer, frozenset()) | {"procedure_runs"}
    reasons = []
    for field, value in patch.items():
        if field not in allowed:
            reasons.append(f"{writer} 不能写字段 {field}")
            continue
        if outputs is not None and field != "procedure_runs" and field not in outputs:
            reasons.append(f"{procedure_id} 未声明产出 {field}")
            continue
        model, is_list = field_type(field)
        if not is_list:
            if not isinstance(value, model):
                reasons.append(f"{field} 应为 {model.__name__}, 实为 {type(value).__name__}")
        elif not isinstance(value, list):
            reasons.append(f"{field} 应为 {model.__name__} 列表, 实为 {type(value).__name__}")
        else:
            for i, item in enumerate(value):
                if not isinstance(item, model):
                    reasons.append(f"{field}[{i}] 应为 {model.__name__}, 实为 {type(item).__name__}")
    return reasons


def check_ids(patch: StatePatch, state: EngagementState) -> list[str]:
    """ID 唯一, 版本不倒退, 同版本内容一致.

    同一字段内 ID 不重复; 状态中已有同 ID 的更高版本时拒绝: 有 upsert_by_id 的字段并入时会抛 VersionConflict,
    整体替换的字段 (如 risk_register, acceptance_scorecard) 会被旧版本覆盖.
    状态中已有同 ID 同版本而内容不同时拒绝: 写 Store 时同号跳过并返回已存对象, 片段的修改会悄悄丢失.
    """
    reasons = []
    for field, value in patch.items():
        items = objects(value)
        ids = [item.id for item in items]
        for dup in sorted({i for i in ids if ids.count(i) > 1}):
            reasons.append(f"{field} 中 ID {dup} 重复")
        current = {item.id: item for item in objects(state.get(field))}
        for item in items:
            old = current.get(item.id)
            if not (isinstance(item, Versioned) and isinstance(old, Versioned)):
                continue
            if item.version < old.version:
                reasons.append(f"{field} 中 {item.id} v{item.version} 低于状态中已有的 v{old.version}")
            elif item.version == old.version and item != old:
                reasons.append(f"{field} 中 {item.id} v{item.version} 与状态中已有的同版本内容不同")
    return reasons


def check_refs(patch: StatePatch, state: EngagementState) -> list[str]:
    """外键: 引用的程序, 执行记录, 例外与复核目标都在程序包, 片段或状态中.

    - Judgment, AuditException, ReviewTask: procedure_id 在程序包中; run_id 在片段或状态的执行记录中,
      且该记录属于同一程序.
    - Judgment, AcceptanceScorecard, AnalyticsResult: exception_ids 的每一项在片段或状态的例外中.
    - ReviewTask: target_type 对应字段中有 target_id. 待决任务的 target_version 须等于目标当前版本 (否则提交决定时
      必报版本不符); 已决定任务 (重放取回的) 记的是复核时的版本, 目标之后可能被决定改出新版本, 只须不高于当前版本.
    - 反向: 片段把某对象改出新版本, 而状态中有以它的原版本为目标的待决任务 (片段没有一并更新该任务), 任务随之过期.
    """
    pack = {procedure.id for procedure in state["procedure_pack"]}
    runs = known(patch, state, "procedure_runs")
    exceptions = known(patch, state, "exceptions")
    reasons = []
    for value in patch.values():
        for item in objects(value):
            name = f"{type(item).__name__} {item.id}"
            if isinstance(item, Judgment | AuditException | ReviewTask):
                if item.procedure_id not in pack:
                    reasons.append(f"{name} 的 procedure_id {item.procedure_id} 不在程序包中")
                run = runs.get(item.run_id)
                if run is None:
                    reasons.append(f"{name} 的 run_id {item.run_id} 不在执行记录中")
                elif run.procedure_id != item.procedure_id:
                    reasons.append(f"{name} 的 run_id {item.run_id} 属于 {run.procedure_id}, 与 procedure_id 不符")
            if isinstance(item, Judgment | AcceptanceScorecard | AnalyticsResult):
                for exception_id in item.exception_ids:
                    if exception_id not in exceptions:
                        reasons.append(f"{name} 引用的例外 {exception_id} 不存在")
            if isinstance(item, ReviewTask):
                target = known(patch, state, TARGET_FIELDS[item.target_type]).get(item.target_id)
                if target is None:
                    reasons.append(f"{name} 的目标 {item.target_type} {item.target_id} 不存在")
                elif item.status is TaskStatus.pending and target.version != item.target_version:
                    reasons.append(
                        f"{name} 待决, 其 target_version {item.target_version} 与目标当前版本 v{target.version} 不符"
                    )
                elif target.version < item.target_version:
                    reasons.append(f"{name} 的 target_version {item.target_version} 高于目标当前版本 v{target.version}")
    reasons += stale_tasks(patch, state)
    return reasons


def stale_tasks(patch: StatePatch, state: EngagementState) -> list[str]:
    """片段把某对象改出新版本, 使状态中以它原版本为目标的待决任务过期 (审计师提交决定时必报版本不符).

    只看原本有效的任务 (target_version 等于状态中目标的版本); 片段自带同 ID 任务的不算, 由 check_refs 查片段中的任务.
    """
    own = {task.id for task in objects(patch.get("review_tasks"))}
    reasons = []
    for task in objects(state.get("review_tasks")):
        if task.status is not TaskStatus.pending or task.id in own:
            continue
        field = TARGET_FIELDS[task.target_type]
        before = {o.id: o for o in objects(state.get(field))}.get(task.target_id)
        after = {o.id: o for o in objects(patch.get(field))}.get(task.target_id)
        if before is not None and after is not None and before.version == task.target_version != after.version:
            reasons.append(
                f"{task.target_type} {task.target_id} 改为 v{after.version}, "
                f"待决任务 {task.id} 记的 v{task.target_version} 随之过期"
            )
    return reasons


def check_run(patch: StatePatch, procedure_id: str, run_id: str) -> list[str]:
    """本次执行的终态: procedure_runs 恰含本次执行一条, 属于本程序, 状态为 succeeded 或 failed 且有结束时间."""
    runs: list[ProcedureRun] = patch.get("procedure_runs", [])
    reasons = []
    others = [run.id for run in runs if run.id != run_id]
    if others:
        reasons.append(f"procedure_runs 只能含本次执行 {run_id}, 多出 {', '.join(others)}")
    run = next((r for r in runs if r.id == run_id), None)
    if run is None:
        reasons.append(f"procedure_runs 缺本次执行 {run_id}")
        return reasons
    if run.procedure_id != procedure_id:
        reasons.append(f"本次执行 {run_id} 的 procedure_id 为 {run.procedure_id}, 应为 {procedure_id}")
    if run.status is RunStatus.running:
        reasons.append(f"本次执行 {run_id} 仍为 running, 应为 succeeded 或 failed")
    elif run.ended_at is None:
        reasons.append(f"本次执行 {run_id} 已为 {run.status} 但缺 ended_at")
    return reasons


def check_own_run(patch: StatePatch, run_id: str) -> list[str]:
    """片段中带 run_id 而不带 procedure_id 的对象 (如关系图) 只能是本次执行写的: run_id 须等于本次执行, 否则重放
    (按 run_id 或核对所属程序取回) 对不上第一次合并的结果. 按模型字段判断, 不按类名. 带 procedure_id 的 (判断, 例外,
    任务) 可以是本程序或别的程序之前的执行写的, 由本次改出新版本 (如 REV-02 把 REV-08 的例外置 attributed),
    由 check_refs 核对所属程序."""
    reasons = []
    for field, value in patch.items():
        for item in objects(value):
            fields = type(item).model_fields
            if "run_id" in fields and "procedure_id" not in fields and item.run_id != run_id:
                reasons.append(f"{field} 中 {item.id} 的 run_id {item.run_id} 不是本次执行 {run_id}")
    return reasons


def check_patch(
    patch: StatePatch,
    *,
    writer: AgentId,
    procedure_id: str,
    run_id: str,
    state: EngagementState,
    outputs: Collection[str],
) -> None:
    """校验一个 Agent 片段能否合并, 不过抛 MergeRejected (含全部原因).

    patch: Agent.run 的返回; fan_in 时为暂存结果中的片段.
    writer: 产出片段的 Agent, 决定可写哪些字段, 如 AgentId.A2.
    procedure_id, run_id: 本次执行的程序与执行记录 ID, 即 ctx.run.procedure_id 与 ctx.run.id.
    state: 执行时的编排状态, 只读, 用来查外键与已有版本.
    outputs: 本程序模板声明的产出字段 (template.outputs); 片段只能含这些与 procedure_runs.
    先查字段归属, 声明与类型, 不过就不再往下查 (对象类型不对时, 后面的检查无从谈起); 再一并查 ID, 外键, 本次执行的终态
    与只能由本次执行写的对象 (check_own_run).
    例: check_patch(patch, writer=agent.agent_id, procedure_id=ctx.run.procedure_id, run_id=ctx.run.id,
    state=ctx.state, outputs=ctx.template.outputs)
    """
    reasons = check_fields(patch, writer, procedure_id, outputs)
    if not reasons:
        reasons = [
            *check_ids(patch, state),
            *check_refs(patch, state),
            *check_run(patch, procedure_id, run_id),
            *check_own_run(patch, run_id),
        ]
    if reasons:
        raise MergeRejected(reasons)
