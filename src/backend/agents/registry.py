"""程序编号 → 执行它的 Agent 实例. 闸门步骤 (ACC-01, PLN-01/03 的决定, FIN-01, RPT-01) 由 A1 的闸门节点处理."""

from typing import Any

from backend.agents.base import AgentDeps, ProcedureAgent
from backend.agents.client_intel import ClientIntelAgent
from backend.agents.concierge import ConciergeAgent
from backend.agents.evidence import EvidenceAgent
from backend.agents.fieldwork import FieldworkAgent
from backend.agents.ingest import IngestAgent
from backend.agents.qc import QCAgent
from backend.agents.review import ReviewAgent
from backend.agents.risk_assessor import RiskAssessorAgent

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


def build_agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
    """每个 Agent 从程序库中选出自己负责的程序, 只映射启用的程序 (功能开关, services/features.py).
    声明了却没有模板的程序视为配置错误, 按全部模板 (含未启用的) 检查."""
    templates = deps.library.all(include_disabled=True)
    agents: dict[str, ProcedureAgent[Any]] = {}
    for cls in AGENT_CLASSES:
        instance = cls(deps)
        selected = instance.select_procedures(templates)
        missing = cls.procedures - {t.id for t in selected}
        if missing:
            raise LookupError(f"{cls.__name__} 负责的程序没有模板: {sorted(missing)}")
        for template in selected:
            if deps.library.enabled(template.id):
                agents[template.id] = instance
    return agents
