"""程序编号 → 执行它的 Agent 实例. 闸门步骤 (ACC-01, PLN-01/03 的决定, FIN-01, RPT-01) 由 A1 的闸门节点处理."""

from backend.agents.base import AgentDeps, ProcedureAgent
from backend.agents.client_intel import ClientIntelAgent
from backend.agents.concierge import ConciergeAgent
from backend.agents.evidence import EvidenceAgent
from backend.agents.fieldwork import FieldworkAgent
from backend.agents.ingest import IngestAgent
from backend.agents.qc import QCAgent
from backend.agents.risk_assessor import RiskAssessorAgent

AGENT_CLASSES: tuple[type[ProcedureAgent], ...] = (
    ClientIntelAgent,
    IngestAgent,
    RiskAssessorAgent,
    EvidenceAgent,
    FieldworkAgent,
    QCAgent,
    ConciergeAgent,
)


def build_agents(deps: AgentDeps) -> dict[str, ProcedureAgent]:
    agents: dict[str, ProcedureAgent] = {}
    for cls in AGENT_CLASSES:
        instance = cls(deps)
        for pid in cls.procedures:
            agents[pid] = instance
    return agents
