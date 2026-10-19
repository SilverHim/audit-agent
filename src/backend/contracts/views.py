"""StateQuery 返回的视图模型, 定义见 开发接口文档.md 第 7 节; 判断与留痕的输出视图 (经盲判过滤, 第 4 节 BlindFilter)."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from backend.contracts.data import DataRequest, TableKind
from backend.contracts.models import (
    AcceptanceScorecard,
    AgentId,
    AnalyticsResult,
    AuditException,
    AuditObjective,
    AuditReportVersion,
    AutonomyLevel,
    ClientProfile,
    CriticReasonType,
    CriticTrigger,
    CriticVerdict,
    Engagement,
    Engine,
    EvidencePointer,
    GateDecision,
    GateId,
    MaterialityCandidates,
    Misstatement,
    MisstatementDraft,
    Model,
    OpinionType,
    Phase,
    ProcedureStatus,
    ProposedAction,
    ReasoningStep,
    ReviewStatus,
    RiskItem,
    TrailKind,
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
    # 过期 (需求 R274): 本程序读的数据 (模板 reads) 在最近一次执行之后变过, 或依赖的程序过期, 在它之后又执行过或
    # 已被重跑取代; 没执行过, 执行时没记数据版本 (承接阶段), 没有当前结果 (已被重跑取代而还没有新执行, 或在等数据,
    # 未执行) 为 false. 见 services.staleness
    stale: bool = False
    # 过期且能重跑: 执行阶段的程序, 不在等重跑, 不在等数据或未执行 (计划阶段的程序过期只标记)
    rerunnable: bool = False


class EngagementOverview(Model):
    """项目总览页的数据."""

    engagement: Engagement  # 项目
    client_name: str  # 客户画像的 legal_name, 供页面显示
    running: bool  # 后台是否正在推进
    run_error: str | None  # 最近一次后台执行的异常, 成功推进后清空; Store 领先检查点时为 RESUME_REQUIRED 提示;
    # 功能集与项目冻结的不同时为 FEATURES_MISMATCH (runner.features_mismatch)
    current_gate: GateId | None  # 正停在哪道闸门, 不在闸门为空
    awaiting_review: bool  # 是否停在节点内等复核决定 (不含等数据就绪)
    awaiting_data: bool  # 是否停在 P1 开始前等财务数据就绪 (p1_await_data), 审计师确认数据已齐后继续
    missing_tables: list[TableKind]  # 等数据就绪时还缺的必需表 (科目表, 本期与上期试算表, 总账分录), 现查; 不在等时为空
    procedures: list[ProcedureSummary]  # 各程序状态
    stale_procedure_ids: list[str] = []  # 过期的程序, 按 procedures 的顺序 (含不能重跑的)
    can_rerun: bool = False  # 现在能否重跑过期程序: 图停在等复核, FIN-01 或 RPT-01 闸门, 项目未锁定, 不在执行
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
    uncovered_objective_ids: list[str]  # 按当前风险等级没有程序覆盖的审计目标; 非空时不能通过, 须驳回或改风险等级


class NotExecuted(Model):
    """因客户无此类资料而未执行 (no_data) 的程序 (需求 R280), 列进 FIN-01 材料."""

    procedure_id: str  # 程序编号
    via: list[str] = []  # 因前置未执行而未执行时, 传来理由的前置: 同为 no_data 且请求有交集 (按 depends_on 顺序)
    requests: list[DataRequest]  # 客户无此类资料的补交请求 (Procedure.data_request_ids), 含理由, 标记人与时间


class CompletionMaterial(Model):
    """FIN-01 闸门的呈报材料."""

    kind: Literal["FIN-01"] = "FIN-01"  # 材料类型标记
    schedule: UncorrectedSchedule  # 未更正错报汇总表
    misstatements: list[Misstatement]  # 当前结果的错报 (过期重跑取代的执行引出的不在其中, services.staleness)
    not_executed: list[NotExecuted] = []  # 未执行 (无数据) 的程序, 按程序编号 (需求 R280)
    stale_procedure_ids: list[str] = []  # 过期而未重跑的程序 (提示审计师, 不拦截 FIN-01, Q5)


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


class JudgmentView(Model):
    """判断的输出视图: 字段同 Judgment, 盲判时可隐藏的字段可空, hidden_fields 列出被隐藏的字段 (未隐藏时为空).

    返回判断的接口与对话工具都经 BlindFilter.judgment 输出它, 不直接输出 Judgment (第 10 节). 不往 Judgment 里填占位值.
    可隐藏的: 结论, 置信度及其分项, 推理链, 反方论证, 建议动作, 错报草稿, 独立复核 (critic_) 的各字段.
    """

    id: str  # 判断 ID
    version: int  # 版本号
    procedure_id: str  # 产出它的程序
    run_id: str  # 产出它的那次执行
    subject_id: str  # 判断对象
    conclusion: str | None  # 结论
    confidence: float | None  # 置信度
    confidence_parts: dict[str, float] | None  # 置信度分项
    reasoning_chain: list[ReasoningStep] | None  # 推理链
    evidence: list[EvidencePointer]  # 结论引用的全部证据
    counter_argument: str | None  # 反方论证
    proposed_action: ProposedAction | None  # 建议动作
    misstatement_draft: MisstatementDraft | None = None  # 错报草稿
    exception_ids: list[str] = []  # 本判断解释的例外
    autonomy_level: AutonomyLevel  # 判断时的有效自主度
    review_status: ReviewStatus  # 复核状态
    superseded_by: str | None = None  # 被推翻时取代它的人工判断 ID
    performed_by: str  # 用户 ID
    created_at: datetime  # 生成时间
    trail_ref: str | None = None  # 生成它的那次大模型调用的留痕 ID
    critic_verdict: CriticVerdict | None  # 独立复核结论
    critic_trigger: CriticTrigger | None = None  # 命中的触发规则
    critic_reason_type: CriticReasonType | None = None  # 不一致的理由类型
    critic_reason: str | None = None  # 理由
    critic_conclusion: str | None = None  # 独立结论
    critic_reasoning_chain: list[ReasoningStep] | None  # 独立推理链
    critic_model: str | None = None  # 复核用的模型名
    critic_trail_ref: str | None = None  # 第二轮调用的留痕 ID
    hidden_fields: list[str] = []  # 被隐藏 (置空) 的字段名


class TrailEntryView(Model):
    """留痕的输出视图: 字段同 TrailEntry, payload 可空 (盲判时隐藏模型调用的原始输入输出), hidden_fields 列出被隐藏的
    字段. 返回留痕的接口与对话工具都经 BlindFilter.trail 输出它 (第 10 节)."""

    id: str | None = None  # 留痕 ID
    engagement_id: str  # 所属项目
    kind: TrailKind  # 留痕类型
    actor: str  # 谁做的
    procedure_id: str | None = None  # 相关程序
    run_id: str | None = None  # 相关的那次执行
    summary: str  # 一句话摘要
    model: str | None = None  # 大模型调用时的模型名
    prompt_hash: str | None = None  # 送给大模型的完整输入的 sha256
    input_tokens: int | None = None  # 输入 token 数
    output_tokens: int | None = None  # 输出 token 数
    idempotency_key: str | None = None  # 外部动作的防重复键
    payload: dict[str, Any] | None  # 明细
    created_at: datetime | None = None  # 写入时间
    hidden_fields: list[str] = []  # 被隐藏 (置空) 的字段名
