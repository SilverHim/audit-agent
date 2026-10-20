"""程序编号 → 执行它的 Agent 实例. 闸门步骤 (ACC-01, PLN-01/03 的决定, FIN-01, RPT-01) 由 A1 的闸门节点处理;
人工程序 (engine manual 且不是闸门, 需求 R93) 由 A10 派发, 材料与结果整理由模板 owner 实现 (manual_owners)."""

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
from backend.services.procedure_library import manual_program

AGENT_CLASSES: tuple[type[ProcedureAgent[Any]], ...] = (
    ClientIntelAgent,
    IngestAgent,
    RiskAssessorAgent,
    EvidenceAgent,
    FieldworkAgent,
    ReviewAgent,
    QCAgent,
    ConciergeAgent,
)


def manual_owners(deps: AgentDeps) -> dict[str, ManualHandler]:
    """启用的人工程序 → 其模板 owner 的 ManualHandler: AGENT_CLASSES 中 agent_id 为模板 owner 且 manual_procedures 含
    该程序, 实现了 ManualHandler 的类 (每个类一个实例). 找不到抛 LookupError, 消息含程序编号与 owner."""
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
        owner = instances.setdefault(cls, cls(deps))
        assert isinstance(owner, ManualHandler)  # issubclass 已查
        owners[template.id] = owner
    return owners


def build_agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
    """每个 Agent 从程序库中选出自己负责的程序, 只映射启用的程序 (功能开关, services/features.py).
    声明了却没有模板的程序视为配置错误, 按全部模板 (含未启用的) 检查. 启用的人工程序都映射到 A10 (派发), 其 owner
    的 ManualHandler 填进 A10 的 owners (见 manual_owners, owner 没实现时启动即报错)."""
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
    owners = manual_owners(deps)
    if owners:
        assert concierge is not None  # A10 在 AGENT_CLASSES 中
        concierge.owners = owners
        agents |= dict.fromkeys(owners, concierge)
    return agents
