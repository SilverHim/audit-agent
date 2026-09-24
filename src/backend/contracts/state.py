"""编排状态与 reducer，定义见 开发接口文档.md 第 3 节。"""

from typing import Annotated, Any, TypedDict, TypeVar

from backend.contracts.errors import VersionConflict
from backend.contracts.models import (
    AcceptanceScorecard,
    AnalyticsResult,
    AuditException,
    AuditObjective,
    AuditReportVersion,
    Confirmation,
    DocumentRef,
    Engagement,
    GateDecision,
    Judgment,
    MaterialityCandidates,
    Misstatement,
    OpinionType,
    Phase,
    Procedure,
    ProcedureRun,
    ReviewRecord,
    ReviewTask,
    RiskItem,
    ScheduleRow,
    UncorrectedSchedule,
    Versioned,
)

StatePatch = dict[str, Any]

V = TypeVar("V", bound=Versioned)
R = TypeVar("R", DocumentRef, ReviewRecord, GateDecision, Misstatement)


def upsert_by_id(left: list[V], right: list[V] | None) -> list[V]:
    """新 ID 追加；version 更高的替换；version 相同的忽略；version 更低抛 VersionConflict。"""
    merged = {item.id: item for item in left}
    order = [item.id for item in left]
    for item in right or []:
        current = merged.get(item.id)
        if current is None:
            order.append(item.id)
            merged[item.id] = item
        elif item.version > current.version:
            merged[item.id] = item
        elif item.version < current.version:
            raise VersionConflict(f"{item.id}: v{item.version} 低于已有 v{current.version}")
    return [merged[i] for i in order]


def replace_by_id(left: list[R], right: list[R] | None) -> list[R]:
    """按 id 追加，同 id 后到者替换。"""
    merged = {item.id: item for item in left}
    order = [item.id for item in left]
    for item in right or []:
        if item.id not in merged:
            order.append(item.id)
        merged[item.id] = item
    return [merged[i] for i in order]


def append_unique(left: list[R], right: list[R] | None) -> list[R]:
    """按 ID 追加，已存在的忽略。"""
    seen = {item.id for item in left}
    out = list(left)
    for item in right or []:
        if item.id not in seen:
            seen.add(item.id)
            out.append(item)
    return out


def staged(left: list[StatePatch], right: list[StatePatch] | None) -> list[StatePatch]:
    """追加；收到 None 时清空，只由 fan_in 发出。"""
    if right is None:
        return []
    return left + right


class EngagementState(TypedDict):
    engagement_id: str
    phase: Phase
    version_snapshot: dict[str, str]
    acceptance_scorecard: AcceptanceScorecard | None
    analytics: AnalyticsResult | None
    materiality_candidates: MaterialityCandidates | None
    materiality_cents: int | None
    performance_materiality_cents: int | None
    clearly_trivial_cents: int | None
    audit_objectives: list[AuditObjective]
    risk_register: list[RiskItem]
    procedure_pack: list[Procedure]
    schedule_table: list[ScheduleRow]
    documents: Annotated[list[DocumentRef], replace_by_id]
    staged_results: Annotated[list[StatePatch], staged]
    procedure_runs: Annotated[list[ProcedureRun], upsert_by_id]
    exceptions: Annotated[list[AuditException], upsert_by_id]
    judgments: Annotated[list[Judgment], upsert_by_id]
    review_tasks: Annotated[list[ReviewTask], upsert_by_id]
    confirmations: Annotated[list[Confirmation], upsert_by_id]
    report_versions: Annotated[list[AuditReportVersion], upsert_by_id]
    review_records: Annotated[list[ReviewRecord], append_unique]
    gate_decisions: Annotated[list[GateDecision], append_unique]
    misstatements: Annotated[list[Misstatement], append_unique]
    uncorrected_schedule: UncorrectedSchedule | None
    opinion_route: OpinionType | None


def initial_state(engagement: Engagement, procedures: list[Procedure]) -> EngagementState:
    """runner.start 的图输入：固定集程序在此实例化，其余字段为空。"""
    return EngagementState(
        engagement_id=engagement.id,
        phase=engagement.phase,
        version_snapshot=dict(engagement.version_snapshot),
        acceptance_scorecard=None,
        analytics=None,
        materiality_candidates=None,
        materiality_cents=None,
        performance_materiality_cents=None,
        clearly_trivial_cents=None,
        audit_objectives=[],
        risk_register=[],
        procedure_pack=procedures,
        schedule_table=[],
        documents=[],
        staged_results=[],
        procedure_runs=[],
        exceptions=[],
        judgments=[],
        review_tasks=[],
        confirmations=[],
        report_versions=[],
        review_records=[],
        gate_decisions=[],
        misstatements=[],
        uncorrected_schedule=None,
        opinion_route=None,
    )
