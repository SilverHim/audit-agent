"""P2 调度: 依赖图分层, 就绪判定, 按循环分组. 只用程序包, 依赖图, 并行上限三个输入, 不调 LLM."""

from collections import defaultdict

from backend.contracts.models import Phase, Procedure, ProcedureStatus, ScheduleRow
from backend.services.procedure_library import ProcedureLibrary


def p2_procedures(pack: list[Procedure]) -> list[Procedure]:
    return [p for p in pack if p.phase is Phase.P2_Executing]


def build_schedule(pack: list[Procedure], library: ProcedureLibrary, cap: int) -> list[ScheduleRow]:
    """拓扑分层: 只依赖 P2 以外程序的在第 0 层; 同层按 cycle 分组, 组数超上限时合并最小两组."""
    procs = {p.id: p for p in p2_procedures(pack)}
    layer: dict[str, int] = {}

    def depth(pid: str) -> int:
        if pid not in layer:
            deps = [d for d in library.load(pid).depends_on if d in procs]
            layer[pid] = 0 if not deps else 1 + max(depth(d) for d in deps)
        return layer[pid]

    rows: list[ScheduleRow] = []
    by_layer: dict[int, list[str]] = defaultdict(list)
    for pid in sorted(procs):
        by_layer[depth(pid)].append(pid)
    for n in sorted(by_layer):
        groups: dict[str, list[str]] = defaultdict(list)
        for pid in by_layer[n]:
            groups[library.load(pid).cycle].append(pid)
        merged = sorted(groups.values(), key=len)
        while len(merged) > cap:
            a, b = merged.pop(0), merged.pop(0)
            merged.append(sorted(a + b))
            merged.sort(key=len)
        for k, grp in enumerate(sorted(merged)):
            rows.append(ScheduleRow(layer=n, instance=f"fw-{n}-{k}", procedure_ids=sorted(grp)))
    return rows


def ready_procedures(pack: list[Procedure], library: ProcedureLibrary) -> list[str]:
    """待执行 (planned 或 executing) 且程序包中的前置都已 completed 的 P2 程序. 前置看整个程序包: P2 以外的前置
    (REV-03 的 DOC-04) 等数据时也不派发 (需求 R272)."""
    status = {p.id: p.status for p in pack}
    return [
        p.id
        for p in p2_procedures(pack)
        if p.status in (ProcedureStatus.planned, ProcedureStatus.executing)
        and all(status[d] is ProcedureStatus.completed for d in library.load(p.id).depends_on if d in status)
    ]
