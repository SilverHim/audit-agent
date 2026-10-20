"""程序编号 → 执行它的 Agent 实例. 闸门步骤 (ACC-01, PLN-01/03 的决定, FIN-01, RPT-01) 由 A1 的闸门节点处理;
人工程序 (engine manual 且不是闸门, 需求 R93) 由 A10 派发, 材料与结果整理由模板 owner 实现 (manual_owners, 组装时
经 with_manual_owners 放进 AgentDeps)."""

from dataclasses import replace
from typing import Any

from backend.agents.base import AgentDeps, ManualHandler, ProcedureAgent
from backend.agents.client_intel import ClientIntelAgent
from backend.agents.concierge import ConciergeAgent
from backend.agents.evidence import EvidenceAgent
from backend.agents.fieldwork import FieldworkAgent
from backend.agents.ingest import IngestAgent
from backend.agents.qc import QCAgent
from backend.agents.review import ReviewAgent
from backend.agents.risk_assessor import RiskAssessorAgent
from backend.agents.workpaper import WorkpaperAgent
from backend.services.procedure_library import manual_program

AGENT_CLASSES: tuple[type[ProcedureAgent[Any]], ...] = (
    ClientIntelAgent,
    IngestAgent,
    RiskAssessorAgent,
    EvidenceAgent,
    FieldworkAgent,
    ReviewAgent,
    WorkpaperAgent,
    QCAgent,
    ConciergeAgent,
)


def manual_owners(deps: AgentDeps) -> dict[str, ManualHandler]:
    """启用的人工程序 → 其模板 owner 的 ManualHandler: AGENT_CLASSES 中 agent_id 为模板 owner 且 manual_procedures 含
    该程序, 实现了 ManualHandler 的类 (每个类一个实例). 找不到抛 LookupError, 消息含程序编号与 owner. 只由
    with_manual_owners 在组装时调."""
    instances: dict[type[ProcedureAgent[Any]], ProcedureAgent[Any]] = {}
    owners: dict[str, ManualHandler] = {}
    for template in deps.library.all():
        if not manual_program(template):
            continue
        cls = next(
            (
                c
                for c in AGENT_CLASSES
                if c.agent_id is template.owner and template.id in c.manual_procedures and issubclass(c, ManualHandler)
            ),
            None,
        )
        if cls is None:
            raise LookupError(f"人工程序 {template.id} 的 owner {template.owner} 没有为它实现 ManualHandler")
        if cls not in instances:
            instances[cls] = cls(deps)
        owner = instances[cls]
        assert isinstance(owner, ManualHandler)  # issubclass 已查
        owners[template.id] = owner
    return owners


def with_manual_owners(deps: AgentDeps) -> AgentDeps:
    """组装的最后一步 (main.build_deps, 测试的 make_deps): 按 deps 的程序库找好人工程序的 owner (manual_owners), 放进
    deps.manual_owners, 之后 A10, 节点与 DecisionSubmit 共用. owner 没实现时在这里报错, 即启动即报错. owner 以返回的
    这个 deps 构造 (先放一个空表, 找好后填进去), 与各处用的是同一个; 之后再 replace 换端口须重新组装."""
    owners: dict[str, ManualHandler] = {}
    assembled = replace(deps, manual_owners=owners)
    owners.update(manual_owners(assembled))
    return assembled


def build_agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
    """每个 Agent 从程序库中选出自己负责的程序, 只映射启用的程序 (功能开关, services/features.py).
    声明了却没有模板的程序视为配置错误, 按全部模板 (含未启用的) 检查. 启用的人工程序都映射到 A10 (派发), owner 取
    deps.manual_owners (组装时由 with_manual_owners 找好); 有启用的人工程序不在其中 (换了程序库没重新组装) 时抛
    LookupError."""
    templates = deps.library.all(include_disabled=True)
    agents: dict[str, ProcedureAgent[Any]] = {}
    concierge: ConciergeAgent | None = None
    for cls in AGENT_CLASSES:
        instance = cls(deps)
        if isinstance(instance, ConciergeAgent):
            concierge = instance
        selected = instance.select_procedures(templates)
        missing = cls.procedures - {t.id for t in selected}
        if missing:
            raise LookupError(f"{cls.__name__} 负责的程序没有模板: {sorted(missing)}")
        for template in selected:
            if deps.library.enabled(template.id):
                agents[template.id] = instance
    manual = sorted(t.id for t in deps.library.all() if manual_program(t))
    unassembled = [pid for pid in manual if pid not in deps.manual_owners]
    if unassembled:
        raise LookupError(f"人工程序 {', '.join(unassembled)} 的 owner 没有组装 (registry.with_manual_owners)")
    if manual:
        assert concierge is not None  # A10 在 AGENT_CLASSES 中
        agents |= dict.fromkeys(manual, concierge)
    return agents
