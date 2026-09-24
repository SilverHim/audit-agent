"""StateQuery 返回的视图模型，定义见 开发接口文档.md 第 7 节。"""

from typing import Annotated, Literal

from pydantic import Field

from backend.contracts.models import (
    AcceptanceScorecard,
    AgentId,
    AnalyticsResult,
    AuditException,
    AuditObjective,
    AuditReportVersion,
    AutonomyLevel,
    ClientProfile,
    Engagement,
    Engine,
    GateDecision,
    GateId,
    MaterialityCandidates,
    Misstatement,
    Model,
    OpinionType,
    Phase,
    ProcedureStatus,
    RiskItem,
    UncorrectedSchedule,
)


class ProcedureSummary(Model):
    id: str
    name: str
    phase: Phase
    owner: AgentId
    engine: Engine
    autonomy_level: AutonomyLevel
    status: ProcedureStatus  # 按 AuditOutputStore 中 Procedure 的最新版本
    pending_tasks: int  # 按 AuditOutputStore 中 pending 任务计


class EngagementOverview(Model):
    engagement: Engagement
    running: bool
    run_error: str | None
    current_gate: GateId | None
    awaiting_review: bool
    procedures: list[ProcedureSummary]
    pending_tasks: int
    materiality_cents: int | None
    performance_materiality_cents: int | None
    clearly_trivial_cents: int | None
    opinion_route: OpinionType | None


class AcceptanceMaterial(Model):
    kind: Literal["ACC-01"] = "ACC-01"
    scorecard: AcceptanceScorecard
    exceptions: list[AuditException]
    client_profile: ClientProfile


class PlanningMaterial(Model):
    kind: Literal["PLN-01/03"] = "PLN-01/03"
    analytics: AnalyticsResult
    analytics_exceptions: list[AuditException]
    candidates: MaterialityCandidates
    objectives: list[AuditObjective]
    risks: list[RiskItem]


class CompletionMaterial(Model):
    kind: Literal["FIN-01"] = "FIN-01"
    schedule: UncorrectedSchedule
    misstatements: list[Misstatement]


class ReportMaterial(Model):
    kind: Literal["RPT-01"] = "RPT-01"
    report: AuditReportVersion
    previous_decisions: list[GateDecision]


GateMaterial = Annotated[
    AcceptanceMaterial | PlanningMaterial | CompletionMaterial | ReportMaterial,
    Field(discriminator="kind"),
]


class GateView(Model):
    gate: GateId
    target_id: str
    target_version: int
    material: GateMaterial
