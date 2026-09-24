"""Agent 抽象基类与依赖。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import AgentId, Engagement, Procedure, ProcedureRun
from backend.contracts.ports import (
    AuditOutputStore,
    AuditTrail,
    ConfirmationChannel,
    DatasetLoader,
    EngagementStore,
    EvidenceStore,
    FinancialDataStore,
    LLMClient,
    RegistrySource,
    RuleEngine,
)
from backend.contracts.state import EngagementState, StatePatch
from backend.services.judgment_engine import JudgmentEngine
from backend.services.procedure_library import ProcedureLibrary
from backend.services.review_queue import ReviewQueue


@dataclass(frozen=True)
class AgentDeps:
    fin: FinancialDataStore
    outputs: AuditOutputStore  # Agent 只读，写入经 StatePatch
    evidence: EvidenceStore
    datasets: DatasetLoader
    engagements: EngagementStore  # 只读
    llm: LLMClient
    rules: RuleEngine
    judgments: JudgmentEngine
    queue: ReviewQueue
    library: ProcedureLibrary
    trail: AuditTrail
    registry: RegistrySource
    confirmations: ConfirmationChannel


@dataclass(frozen=True)
class RunContext:
    engagement: Engagement
    procedure: Procedure
    template: ProcedureTemplate
    run: ProcedureRun
    state: EngagementState  # 只读；按需取切片，不整体送 LLM


class ProcedureAgent(ABC):
    agent_id: ClassVar[AgentId]
    procedures: ClassVar[frozenset[str]]

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    @abstractmethod
    async def run(self, ctx: RunContext) -> StatePatch:
        """返回的 procedure_runs 须含 ctx.run 的终态（succeeded 或 failed）。"""
