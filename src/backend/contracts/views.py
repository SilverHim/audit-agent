"""StateQuery 返回的视图模型, 定义见 开发接口文档.md 第 7 节."""

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
    """项目总览中的一行程序状态."""

    id: str  # 程序编号
    name: str  # 程序名称
    phase: Phase  # 所属阶段
    owner: AgentId  # 执行它的智能体
    engine: Engine  # 执行方式
    autonomy_level: AutonomyLevel  # 自主度
    status: ProcedureStatus  # 按 AuditOutputStore 中 Procedure 的最新版本
    pending_tasks: int  # 按 AuditOutputStore 中 pending 任务计


class EngagementOverview(Model):
    """项目总览页的数据."""

    engagement: Engagement  # 项目
    client_name: str  # 客户画像的 legal_name, 供页面显示
    running: bool  # 后台是否正在推进
    run_error: str | None  # 最近一次后台执行的异常, 成功推进后清空
    current_gate: GateId | None  # 正停在哪道闸门, 不在闸门为空
    awaiting_review: bool  # 是否停在节点内等复核决定
    procedures: list[ProcedureSummary]  # 各程序状态
    pending_tasks: int  # 待决复核任务数
    materiality_cents: int | None  # 整体重要性, 单位分
    performance_materiality_cents: int | None  # 执行重要性, 单位分
    clearly_trivial_cents: int | None  # 明显微小门槛, 单位分
    opinion_route: OpinionType | None  # FIN-01 确认的意见类型


class AcceptanceMaterial(Model):
    """ACC-01 闸门的呈报材料."""

    kind: Literal["ACC-01"] = "ACC-01"  # 材料类型标记, 用于区分四种材料
    scorecard: AcceptanceScorecard  # 承接评分卡
    exceptions: list[AuditException]  # 未通过的检查生成的例外
    # 客户画像的最新版本 (可能由同客户的其他项目更新), ACC-01 页面据此编辑客户资料;
    # 评分卡依据的版本见 scorecard.client_profile_version
    client_profile: ClientProfile


class PlanningMaterial(Model):
    """PLN-01/03 闸门的呈报材料."""

    kind: Literal["PLN-01/03"] = "PLN-01/03"  # 材料类型标记
    analytics: AnalyticsResult  # 初步分析结果
    analytics_exceptions: list[AuditException]  # 初步分析的例外
    candidates: MaterialityCandidates  # 重要性候选
    objectives: list[AuditObjective]  # 审计目标
    risks: list[RiskItem]  # 风险清单


class CompletionMaterial(Model):
    """FIN-01 闸门的呈报材料."""

    kind: Literal["FIN-01"] = "FIN-01"  # 材料类型标记
    schedule: UncorrectedSchedule  # 未更正错报汇总表
    misstatements: list[Misstatement]  # 全部错报


class ReportMaterial(Model):
    """RPT-01 闸门的呈报材料."""

    kind: Literal["RPT-01"] = "RPT-01"  # 材料类型标记
    report: AuditReportVersion  # 当前报告版本
    previous_decisions: list[GateDecision]  # 此前的 RPT-01 退回决定与批注


# 四种闸门材料之一, 按 kind 区分
GateMaterial = Annotated[
    AcceptanceMaterial | PlanningMaterial | CompletionMaterial | ReportMaterial,
    Field(discriminator="kind"),
]


class GateView(Model):
    """闸门页的数据: 当前闸门与要呈报的材料."""

    gate: GateId  # 哪道闸门
    target_id: str  # 材料 ID, 决定须绑定它
    target_version: int  # 材料当前版本, 决定须绑定它
    material: GateMaterial  # 呈报材料
