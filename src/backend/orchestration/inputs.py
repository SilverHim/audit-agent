"""程序要的客户资料 (需求 R272): 模板 inputs 中要看图节点的启动校验, 以及按现有资料定程序是否等数据.

程序执行前 (含审计师要求重跑的) 看模板 inputs 声明的资料: 缺了不执行, 记 awaiting_data (data_request_ids 为还缺的
资料的补交请求), 资料到了回到 planned. 执行阶段在 schedule 与 await_data 节点按这里的结果出程序新版本, P2 以外的
程序每次执行前 (首次与 rerun) 同样判断 (见 nodes.GraphNodes.refresh_inputs 与 inputs_before_run). "有资料" 的口径见
services.data_quality.on_hand.
"""

from collections.abc import Collection, Iterable, Sequence

from backend.contracts.knowledge import ProcedureInputs
from backend.contracts.models import Phase, Procedure, ProcedureStatus
from backend.services.data_quality import OnHand, missing_inputs, request_id
from backend.services.procedure_library import ProcedureLibrary

# 按资料重定状态的程序状态: 还没执行的 (planned, awaiting_data) 与审计师要求重跑的 (executing, rerun 决定写出)
WAITABLE = frozenset({ProcedureStatus.planned, ProcedureStatus.executing, ProcedureStatus.awaiting_data})


def declared(inputs: ProcedureInputs) -> bool:
    """模板写了 inputs (要等资料)."""
    return bool(inputs.tables or inputs.documents)


def input_problems(library: ProcedureLibrary, late: Collection[str]) -> list[str]:
    """要看图节点才能判定的 inputs 写法问题 (含未启用的模板), 每条含程序编号: P2 以外的程序写了 inputs 却不在 late
    中. late 为执行阶段能补跑的 P2 以外程序 (nodes.NODE_PROCEDURES["p1_ingest"]): 只有它们等到数据后有地方执行.
    只看模板就能判定的 (取值, 就绪必需的表, other) 在程序库加载时查 (procedure_library.inputs_problems)."""
    return [
        f"{t.id}: P2 以外的程序只有 {', '.join(sorted(late))} 能写 inputs (等到数据后才有地方执行)"
        for t in library.all(include_disabled=True)
        if declared(t.inputs) and t.phase is not Phase.P2_Executing and t.id not in late
    ]


def check_inputs(library: ProcedureLibrary, late: Collection[str]) -> None:
    """启动校验 (GraphNodes 构造时): input_problems 非空时抛 ValueError."""
    problems = input_problems(library, late)
    if problems:
        raise ValueError("程序模板的 inputs 不对: " + "; ".join(problems))


def input_versions(
    pack: Iterable[Procedure],
    library: ProcedureLibrary,
    have: OnHand,
    ids: Sequence[str],
    replayed: Collection[str] = (),
) -> list[Procedure]:
    """ids 中状态为 WAITABLE (planned, executing, awaiting_data), 声明了 inputs 的程序按现有资料该有的新版本: 还缺资料
    为 awaiting_data, data_request_ids 为还缺的资料的补交请求 ID (按声明顺序); 不缺时 awaiting_data 回到 planned,
    planned 与 executing (要求重跑的) 不变, data_request_ids 为空. 只返回状态或 data_request_ids 有变化的 (version +1),
    按 pack 的顺序; 都不变时为空.
    replayed: 节点重放时这次要执行的那次执行已在 Store 中的程序 (见 nodes.GraphNodes.inputs_before_run). 当时的判断是
    资料齐了, 照此按不缺处理, 不按现在的资料重查: 两次之间资料变了 (如合同改了类型) 时, 新算的版本会撞上 Store 中
    执行写出的版本."""
    wanted = set(ids)
    changed = []
    for procedure in pack:
        if procedure.id not in wanted or procedure.status not in WAITABLE:
            continue
        inputs = library.load(procedure.id).inputs
        if not declared(inputs):
            continue
        missing = [] if procedure.id in replayed else [request_id(subject) for subject in missing_inputs(inputs, have)]
        if missing:
            status = ProcedureStatus.awaiting_data
        elif procedure.status is ProcedureStatus.awaiting_data:
            status = ProcedureStatus.planned
        else:
            status = procedure.status
        if (status, missing) != (procedure.status, procedure.data_request_ids):
            update = {"status": status, "data_request_ids": missing, "version": procedure.version + 1}
            changed.append(procedure.model_copy(update=update))
    return changed
