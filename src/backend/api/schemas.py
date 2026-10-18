"""HTTP 层的请求与响应模型. 领域对象仍用 contracts; 这里只放接口文档第 10 节点名由 C 定义的形状."""

from datetime import date, datetime
from typing import Any, Literal

from backend.contracts.models import (
    AcceptanceScorecard,
    Annotation,
    AuditException,
    AuditReportVersion,
    ClientProfile,
    Decision,
    DisputeResolution,
    GateDecision,
    GateEdit,
    GateId,
    GateVerdict,
    Judgment,
    LlmProvider,
    ManualResult,
    MaterialityChoice,
    Misstatement,
    MisstatementDraft,
    Model,
    NeitherAction,
    OpinionType,
    Phase,
    Procedure,
    ProcedureRun,
    ReviewRecord,
    ReviewTask,
    ReviewTier,
    UncorrectedSchedule,
)


class Token(Model):
    """登录成功返回的访问令牌."""

    access_token: str
    token_type: str = "bearer"


class RegisterIn(Model):
    """注册: 账号即登录名, 不限邮箱格式. 长度等规则由 handler 校验, 不合要求报 INVALID_VALUE."""

    username: str
    full_name: str
    password: str


class EngagementCreate(Model):
    """新建项目. 客户名称与 ABN 不在此传, 取数据集里的客户画像."""

    period_start: date
    period_end: date
    dataset: str


class DatasetSummary(Model):
    """新建项目时可选的一个客户: 数据集名与其客户画像的名称, ABN."""

    dataset: str  # 建项目时提交的数据集名, 如 demo, xero/20261017-mock
    legal_name: str
    abn: str


class EngagementSummary(Model):
    """项目列表中的一行."""

    id: str
    client_name: str
    period_start: date  # 审计期间起始日
    period_end: date  # 审计期间截止日
    phase: Phase
    current_gate: GateId | None
    pending_tasks: int
    updated_at: datetime


class IdBody(Model):
    """202 响应: 刚写入的决定 ID."""

    id: str


class GateDecisionIn(Model):
    """闸门决定请求体: 去掉 id, version, decided_by, decided_at, 由服务端填写."""

    gate: GateId
    target_id: str
    target_version: int
    decision: GateVerdict
    materiality: MaterialityChoice | None = None
    edits: list[GateEdit] = []
    disposition: OpinionType | None = None
    annotations: list[Annotation] = []
    reason: str | None = None


class ReviewRecordIn(Model):
    """复核决定请求体: 去掉 id, version, reviewed_by, decided_at."""

    task_id: str
    target_version: int
    decision: Decision
    reason: str | None = None
    override_conclusion: str | None = None
    misstatement_draft: MisstatementDraft | None = None
    rerun_params: dict[str, Any] | None = None
    manual_result: ManualResult | None = None
    dispute_resolution: DisputeResolution | None = None
    neither_action: NeitherAction | None = None


class ClientProfileIn(Model):
    """更新客户资料. id 由服务端取项目的 client_id; version 为当前版本 +1."""

    version: int
    legal_name: str
    abn: str
    entity_type: str
    industry_anzsic: str
    listed: bool
    directors: list[str]
    notes: str = ""


class ClientProfileView(Model):
    """客户资料页: 客户画像的最新版与历史, 承接评分卡及其例外, 能否编辑."""

    profile: ClientProfile  # 最新版, 口径同 ACC-01 闸门材料 (可能由同客户的其他项目更新)
    history: list[ClientProfile]  # 第 1 版到最新版, 按版本升序
    engagement_profile_version: int  # 本项目采用的画像版本 (Engagement.client_profile_version)
    scorecard: AcceptanceScorecard | None  # 版本最高的承接评分卡, ACC-04 还没跑过时为空
    exceptions: list[AuditException]  # 评分卡 exception_ids 引用的例外
    editable: bool  # 与 PUT /client-profile 的可用条件一致: 停在 ACC-01, 未锁定, 后台没在运行, 无待恢复的错误


class ReviewTaskView(Model):
    """复核队列中的一项: 任务, 程序名, 目标对象, 允许的决定, 涉及金额与复核档位."""

    task: ReviewTask
    procedure_name: str
    target: Judgment | AuditException | ProcedureRun
    allowed_decisions: list[Decision]
    amount_cents: int | None  # 涉及金额, 保留正负号; 取值顺序见 services.review_queue.task_amount
    tier: ReviewTier  # 复核档位, 按金额绝对值与置信度判定 (services.review_queue.review_tier)
    # 争议任务可选的仲裁结果 (独立复核的推理链无效时没有 a7_upheld); 其余任务为空
    allowed_resolutions: list[DisputeResolution] = []


class ProcedureDetail(Model):
    """一条程序的执行记录, 例外与判断."""

    procedure: Procedure
    name: str
    depends_on: list[str]
    standards: list[str]
    runs: list[ProcedureRun]
    exceptions: list[AuditException]
    judgments: list[Judgment]


class JudgmentDetail(Model):
    """一条判断的当前版本, 历史版本与相关复核决定."""

    judgment: Judgment
    versions: list[Judgment]
    review_records: list[ReviewRecord]


class MisstatementReport(Model):
    """错报列表, 未更正汇总, 以及总览里的重要性与意见."""

    items: list[Misstatement]
    schedule: UncorrectedSchedule | None
    materiality_cents: int | None
    opinion_route: OpinionType | None


class ReportBundle(Model):
    """报告各修订版, 以及全部 RPT-01 决定 (含批注)."""

    versions: list[AuditReportVersion]
    decisions: list[GateDecision]


# 稍后处理的对象: 复核任务或闸门 (阶段决定)
DeferralTarget = Literal["review_task", "gate"]


class DeferralIn(Model):
    """标记或取消稍后处理."""

    target_type: DeferralTarget
    target_id: str  # 复核任务填任务 ID, 阶段决定填闸门编号, 如 ACC-01
    deferred: bool  # true 标记, false 取消


class DeferralView(Model):
    """一项有效的稍后处理标记."""

    target_type: DeferralTarget
    target_id: str
    deferred_by: str  # 标记人的用户 ID
    deferred_at: datetime


class ImportSheet(Model):
    """导入时换工作表."""

    sheet: str  # 工作表名, 取解析结果的 sheets 之一


class LlmSlotView(Model):
    """设置页的一套模型配置. 不含密钥原文; 没保存过时为回退后的生效值, updated_at 为空."""

    provider: LlmProvider
    base_url: str
    model: str
    has_key: bool
    key_last4: str | None
    updated_at: datetime | None
    updated_by: str | None


class LlmSettingsView(Model):
    """两套模型配置: 主模型给审计助理与各审计智能体, 复核模型给独立复核智能体."""

    primary: LlmSlotView
    review: LlmSlotView


class LlmSlotIn(Model):
    """保存或测试一套配置. api_key 为空表示沿用当前生效的密钥 (GET 显示的那把; 服务商与接口地址须不变)."""

    provider: LlmProvider
    base_url: str
    model: str
    api_key: str | None = None


class LlmTestResult(Model):
    """测试连接的结果. message 已脱敏."""

    ok: bool
    model: str | None
    latency_ms: int
    message: str
