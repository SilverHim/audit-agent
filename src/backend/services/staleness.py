"""过期程序与当前结果 (需求 R274, 接口文档第 3 节).

过期: 程序最近一次执行记了数据版本 v, 而 (1) 它读的数据 (模板 reads, 见 reads_change) 在 v 之后有变动
(DataStore.changes; 最近一次是增量执行时 v 取最近一次全量执行的, 见 base_version; 更正通知载入的版本对所有程序都算
已覆盖, 见 covered_versions), 或
(2) 它依赖 (depends_on) 的程序过期, 或依赖的程序在它之后又执行过 (如只重跑了 REV-08, REV-02 随之过期; 增量执行不算,
它的例外交更正验证, 不派下游), 或依赖的程序最近一次执行已被过期重跑取代 (它所依据的前置结果已不是当前的).
过期只针对有当前结果的程序: 最近一次执行被过期重跑取代而之后没有新执行的 (重跑后转等数据, 未执行 (无数据) 或
待派发), 与在等数据或未执行 (无数据) 的程序, 没有当前结果, 不算过期, 也不可重跑, 由各自的状态说明 (FIN-01 材料的
"未执行" 与 "过期而未重跑" 两节不重叠). 承接阶段的程序与之前的执行不记数据版本, 不算过期; 没执行过的不算.

当前结果: 过期重跑 (RerunRequest) 以新结果为准, 被取代的执行 (superseded_run_ids) 的判断, 例外与错报留作历史,
不再计入: 汇总表 (FIN-04), FIN-01 材料, 以及后面的程序取前置程序的例外时都只看当前结果. 没有过期重跑时全部都是
当前结果.
"""

from collections.abc import Collection, Iterable, Sequence
from typing import NamedTuple

from backend.contracts.data import DataChange, DataInput
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import (
    AuditException,
    ExceptionDisposition,
    Judgment,
    Misstatement,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    RerunRequest,
    RunTrigger,
)
from backend.contracts.state import EngagementState
from backend.services.procedure_library import ProcedureLibrary


def reads_change(template: ProcedureTemplate, change: DataChange) -> bool:
    """程序是否读这次变动的数据: 按模板 reads; 文件登记 (documents) 的变动带了文件类型时 (需求 R304), 只有读其中某类
    文件登记的程序 (document_reads) 算读到, 如新登记一份财务报表不让读合同与回函登记的程序过期. 没带文件类型的
    (迁移 0020 之前记的, 数据集载入) 不知道是哪类文件, 照旧按 reads 含 documents 判."""
    if change.input is DataInput.documents and change.document_types is not None:
        return bool(set(change.document_types) & set(template.document_reads))
    return change.input in template.reads


def latest_runs(runs: Iterable[ProcedureRun]) -> dict[str, ProcedureRun]:
    """各程序最近一次执行 (run_no 最大的; 同一执行取版本最高的)."""
    latest: dict[str, ProcedureRun] = {}
    for run in runs:
        found = latest.get(run.procedure_id)
        if found is None or (run.run_no, run.version) > (found.run_no, found.version):
            latest[run.procedure_id] = run
    return latest


# 因资料不执行的程序状态 (需求 R272, R280): 没有当前结果, 由状态说明, 不判过期
STOPPED = frozenset({ProcedureStatus.awaiting_data, ProcedureStatus.no_data})


def stale_procedures(
    pack: Sequence[Procedure],
    runs: Iterable[ProcedureRun],
    changes: Sequence[DataChange],
    library: ProcedureLibrary,
    superseded: Collection[str] = frozenset(),
) -> list[str]:
    """程序包中过期的程序, 按程序包顺序. 判定见模块说明; 依赖只看程序包中有的程序. superseded 为被过期重跑取代的
    执行 (superseded_runs, 调用方取 Store 中的重跑请求); 程序状态取 pack."""
    runs = list(runs)
    latest = latest_runs(runs)
    full = latest_runs(r for r in runs if r.trigger is not RunTrigger.incremental)  # 依赖比先后用
    covered = covered_versions(runs, library)
    in_pack = {p.id for p in pack}
    status = {p.id: p.status for p in pack}
    memo: dict[str, bool] = {}

    def stale(pid: str) -> bool:
        if pid in memo:
            return memo[pid]
        memo[pid] = False  # 依赖成环由程序库加载时挡住, 这里只防重入
        run = latest.get(pid)
        if run is None or run.data_version is None or run.id in superseded or status.get(pid) in STOPPED:
            return False  # 没有当前结果 (没执行过, 承接阶段, 已被重跑取代, 等数据或未执行)
        template = library.load(pid)
        version = base_version(pid, run, runs)
        found = any(
            c.data_version > version and c.data_version not in covered and reads_change(template, c) for c in changes
        )
        for dep in template.depends_on:
            if found or dep not in in_pack:
                continue
            dep_run = full.get(dep)
            if dep_run is None:
                continue
            found = stale(dep) or dep_run.started_at > run.started_at or dep_run.id in superseded
        memo[pid] = found
        return found

    return [p.id for p in pack if stale(p.id)]


def base_version(pid: str, latest: ProcedureRun, runs: Iterable[ProcedureRun]) -> int:
    """判过期的基准数据版本 (审核 R194-02): 最近一次执行的数据版本; 它是增量执行 (GEN-05 的增量实例, 需求 R194) 时
    取最近一次非增量执行的, 全量执行之后与更正无关的变动仍判过期. latest 须记了数据版本."""
    assert latest.data_version is not None
    mine = [r for r in runs if r.procedure_id == pid]
    full = max((r for r in mine if r.trigger is not RunTrigger.incremental), key=lambda r: r.run_no, default=None)
    if latest.trigger is not RunTrigger.incremental or full is None or full.data_version is None:
        return latest.data_version
    return full.data_version


def covered_versions(runs: Iterable[ProcedureRun], library: ProcedureLibrary) -> set[int]:
    """对所有程序都算已覆盖的数据版本 (需求 R195): 增量执行与更正验证 (模板 misstatement_update_kind 为
    verification, 即 FIN-09) 记下的, 即紧挨在前的更正通知载入的审计调整分录. 执行阶段的结论按调整前的账面作出,
    审计调整由更正验证与增量扫描查验, 不因客户过账而过期; 之后其他的数据变动照旧判过期."""
    return {
        r.data_version
        for r in runs
        if r.data_version is not None
        and (
            r.trigger is RunTrigger.incremental
            or library.load(r.procedure_id).misstatement_update_kind == "verification"
        )
    }


def rerunnable(pack: Sequence[Procedure], stale: Iterable[str]) -> list[str]:
    """过期程序中能重跑的, 按程序包顺序: 执行阶段的程序 (Q2), 且不在等重跑 (executing: 已有重跑请求或待派发), 也
    不在等数据或未执行 (STOPPED; 这两种本不判过期, 这里再挡一次). 计划阶段的程序过期只标记, 要重做在 PLN-01/03
    闸门前驳回."""
    found = set(stale)
    return [
        p.id
        for p in pack
        if p.id in found
        and p.phase is Phase.P2_Executing
        and p.status is not ProcedureStatus.executing
        and p.status not in STOPPED
    ]


def superseded_runs(requests: Iterable[RerunRequest]) -> set[str]:
    """被过期重跑取代的执行."""
    return {run_id for request in requests for run_id in request.superseded_run_ids}


def current_exceptions(exceptions: Iterable[AuditException], superseded: set[str]) -> list[AuditException]:
    """不属于被取代执行的例外, 保持原顺序."""
    return [x for x in exceptions if x.run_id not in superseded]


def current_misstatements(
    misstatements: Iterable[Misstatement],
    judgments: Iterable[Judgment],
    exceptions: Iterable[AuditException],
    superseded: set[str],
) -> list[Misstatement]:
    """来源判断或例外不属于被取代执行的错报, 保持原顺序. 人工判断 (-H) 的 run_id 是其任务所属的执行, 随之取代.
    来源查不到 (状态与 Store 对不上) 的照旧计入, 不悄悄丢掉."""
    if not superseded:
        return list(misstatements)
    runs = {j.id: j.run_id for j in judgments} | {x.id: x.run_id for x in exceptions}
    return [m for m in misstatements if runs.get(m.judgment_id or m.exception_id or "") not in superseded]


class CurrentResults(NamedTuple):
    """状态中的当前结果 (见 current_results), 各保持状态中的顺序."""

    judgments: list[Judgment]  # 不属于被取代执行的判断
    exceptions: list[AuditException]  # 不属于被取代执行的例外
    misstatements: list[Misstatement]  # 来源判断或例外不属于被取代执行的错报


def superseded_judgments(state: EngagementState) -> set[str]:
    """被过期重跑取代的执行的判断 (含人工判断 -H, 其 run_id 是任务所属的执行)."""
    superseded = superseded_runs(state["rerun_requests"])
    return {j.id for j in state["judgments"] if j.run_id in superseded}


def released(exceptions: Iterable[AuditException], judgments: set[str]) -> list[AuditException]:
    """由 judgments 中的判断归因的例外按未归因给出 (同 ID 同版本, disposition open, attributed_by 空), 其余原样, 保持
    原顺序. 被取代的判断做的归因随之失效: 上游程序没重跑时, 它的例外仍是当前结果, 却挂在已取代的旧判断名下,
    下游新判断 (只取未归因的) 要能重新拿到它们 (current_results; DecisionSubmit 归因时同口径). 只是视图, 不写新版本."""
    if not judgments:
        return list(exceptions)
    return [
        x.model_copy(update={"disposition": ExceptionDisposition.open, "attributed_by": None})
        if x.disposition is ExceptionDisposition.attributed and x.attributed_by in judgments
        else x
        for x in exceptions
    ]


def current_results(state: EngagementState) -> CurrentResults:
    """状态中的当前结果: 去掉过期重跑 (state 的 rerun_requests) 取代的执行的判断, 例外与它们引出的错报; 留下的例外中
    由被取代的判断归因的, 按未归因给出 (见 released). 汇总表 (FIN-04), FIN-01 材料, RPT-03 的校验与后面的程序取前置
    程序的例外与判断 (REV-02, CSH-02) 共用. 没有过期重跑时即状态中的全部.
    R21 的部分重跑 (rerun_subject_ids) 不在其中: 只重判上一轮失败的对象, 那些对象上一轮没有判断, 旧执行的其余
    对象仍是当前结果."""
    superseded = superseded_runs(state["rerun_requests"])
    return CurrentResults(
        judgments=[j for j in state["judgments"] if j.run_id not in superseded],
        exceptions=released(current_exceptions(state["exceptions"], superseded), superseded_judgments(state)),
        misstatements=current_misstatements(
            state["misstatements"], state["judgments"], state["exceptions"], superseded
        ),
    )
