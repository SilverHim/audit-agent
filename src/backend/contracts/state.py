"""编排状态与 reducer, 定义见 开发接口文档.md 第 3 节."""

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
    """新 ID 追加; version 更高的替换; version 相同的忽略; version 更低抛 VersionConflict."""
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
    """按 id 追加, 同 id 后到者替换."""
    merged = {item.id: item for item in left}
    order = [item.id for item in left]
    for item in right or []:
        if item.id not in merged:
            order.append(item.id)
        merged[item.id] = item
    return [merged[i] for i in order]


def append_unique(left: list[R], right: list[R] | None) -> list[R]:
    """按 ID 追加, 已存在的忽略."""
    seen = {item.id for item in left}
    out = list(left)
    for item in right or []:
        if item.id not in seen:
            seen.add(item.id)
            out.append(item)
    return out


def staged(left: list[StatePatch], right: list[StatePatch] | None) -> list[StatePatch]:
    """追加; 收到 None 时清空, 只由 fan_in 发出."""
    if right is None:
        return []
    return left + right


class EngagementState(TypedDict):
    """LangGraph 主图的状态: 一个审计项目全流程共享的数据, 每步存检查点."""

    engagement_id: str  # 项目编号, 也是图的 thread_id
    phase: Phase  # 当前阶段
    version_snapshot: dict[str, str]  # 程序库 commit, 模型, prompt 的版本快照
    acceptance_scorecard: AcceptanceScorecard | None  # 承接评分卡, A2 写
    client_profile_version: int  # 最新客户画像版本; 更新客户资料后写入, ACC-01 路由据此判断评分卡是否过期
    analytics: AnalyticsResult | None  # 初步分析结果, A4 写
    materiality_candidates: MaterialityCandidates | None  # 重要性候选, A4 写
    materiality_cents: int | None  # 整体重要性, PLN-01/03 闸门确定; 只在图状态中
    performance_materiality_cents: int | None  # 执行重要性, 同上
    clearly_trivial_cents: int | None  # 明显微小门槛, 同上
    audit_objectives: list[AuditObjective]  # 审计目标, 读数据集预置
    risk_register: list[RiskItem]  # 风险清单, 读数据集预置, 闸门可改等级
    procedure_pack: list[Procedure]  # 程序包: 全部程序实例, 每次写入传完整列表
    schedule_table: list[ScheduleRow]  # P2 调度表
    documents: Annotated[list[DocumentRef], replace_by_id]  # 证据文件, 同 id 后到者替换
    staged_results: Annotated[list[StatePatch], staged]  # fieldwork 实例的返回, fan_in 校验后并入并清空
    procedure_runs: Annotated[list[ProcedureRun], upsert_by_id]  # 执行记录, 按 id 与版本合并
    exceptions: Annotated[list[AuditException], upsert_by_id]  # 例外, 按 id 与版本合并
    judgments: Annotated[list[Judgment], upsert_by_id]  # 判断, 按 id 与版本合并
    review_tasks: Annotated[list[ReviewTask], upsert_by_id]  # 复核任务, 按 id 与版本合并
    confirmations: Annotated[list[Confirmation], upsert_by_id]  # 函证, 按 id 与版本合并
    report_versions: Annotated[list[AuditReportVersion], upsert_by_id]  # 报告版本, 按 id 与版本合并
    review_records: Annotated[list[ReviewRecord], append_unique]  # 复核决定, 只追加
    gate_decisions: Annotated[list[GateDecision], append_unique]  # 闸门决定, 只追加
    misstatements: Annotated[list[Misstatement], append_unique]  # 错报, 只追加
    uncorrected_schedule: UncorrectedSchedule | None  # 未更正错报汇总表, A9 写
    opinion_route: OpinionType | None  # FIN-01 确认的意见类型; 只在图状态中


def initial_state(engagement: Engagement, procedures: list[Procedure], client_profile_version: int) -> EngagementState:
    """runner.start 的图输入: 固定集程序在此实例化, 客户画像取项目创建时的版本, 其余字段为空."""
    return EngagementState(
        engagement_id=engagement.id,
        phase=engagement.phase,
        version_snapshot=dict(engagement.version_snapshot),
        acceptance_scorecard=None,
        client_profile_version=client_profile_version,
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
