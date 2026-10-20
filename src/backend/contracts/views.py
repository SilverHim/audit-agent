"""StateQuery 与判断呈递 (services/presentation) 返回的视图模型, 定义见 开发接口文档.md 第 7 节."""

from typing import Annotated, Any, Literal

from pydantic import Field

from backend.contracts.data import DataRequest, TableKind
from backend.contracts.models import (
    AcceptanceScorecard,
    AgentId,
    AnalyticsResult,
    AuditException,
    AuditObjective,
    AuditPlan,
    AuditReportVersion,
    AutonomyLevel,
    ClientProfile,
    CriticReasonType,
    Decision,
    DisputeResolution,
    Engagement,
    EngagementTerms,
    Engine,
    EvidencePointer,
    GateDecision,
    GateId,
    Judgment,
    MaterialityCandidates,
    Misstatement,
    MisstatementDraft,
    Model,
    OpinionType,
    Phase,
    ProcedureRun,
    ProcedureStatus,
    ProposedAction,
    ReasoningStep,
    ReviewTask,
    ReviewTier,
    RiskItem,
    UncorrectedSchedule,
    WorkingLanguage,
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


class PhaseProgress(Model):
    """当前阶段的进度 (需求 R325): 工作区顶部 "正在执行 n/m" 与审计助理 "现在在做什么" 用."""

    phase: Phase  # 当前阶段 (项目的 phase)
    done: int  # 本阶段已结清的程序数: completed 或 no_data (未执行, 无数据), 同 P2 结清的口径 (graph.SETTLED)
    total: int  # 程序包中本阶段的程序数
    # 正在运行的程序: 本项目 run 留痕中有开始而没有结束的执行 (按执行 ID 配对) 所属的程序, 按开始的先后; 只在后台正为
    # 本项目推进时给, 否则为空 (进程中断后残留的开始不算)
    running: list[str]


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
    # 是否停在 await_correction 等更正通知 (需求 R162), 与等更正的错报 (等待载荷的 misstatement_ids, 按 ID 排序)
    awaiting_correction: bool = False
    awaiting_correction_ids: list[str] = []
    missing_tables: list[TableKind]  # 等数据就绪时还缺的必需表 (科目表, 本期与上期试算表, 总账分录), 现查; 不在等时为空
    procedures: list[ProcedureSummary]  # 各程序状态
    progress: PhaseProgress  # 当前阶段的进度与正在运行的程序 (需求 R325)
    stale_procedure_ids: list[str] = []  # 过期的程序, 按 procedures 的顺序 (含不能重跑的)
    can_rerun: bool = False  # 现在能否重跑过期程序: 图停在等复核, FIN-01 或 RPT-01 闸门, 项目未锁定, 不在执行
    pending_tasks: int  # 待决复核任务数
    materiality_cents: int | None  # 整体重要性, 单位分
    performance_materiality_cents: int | None  # 执行重要性, 单位分
    clearly_trivial_cents: int | None  # 明显微小门槛, 单位分
    opinion_route: OpinionType | None  # FIN-01 确认的意见类型
    # 项目的功能集 (services/features.frozen_features: 已启动取冻结的, 未启动取当前进程的), 按名称排序; 前端据此
    # 决定按哪个功能的规则显示 (如 collab 打开时复核队列争议在前)
    features: list[str] = []
    # 生成语言与当前工作语言 (engagement.working_language) 不同的执行 (执行 ID → ProcedureRun.language, 需求 R339):
    # 判断, 计划与报告按其 run_id 取, 取到即标注 "以英文生成" 或 "以中文生成"; 查不到 (语言相同, 没有 run_id) 不标
    run_languages: dict[str, WorkingLanguage] = {}


class AcceptanceMaterial(Model):
    """ACC-01 闸门的呈报材料."""

    kind: Literal["ACC-01"] = "ACC-01"  # 材料类型标记, 用于区分各种材料
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


class PlanMaterial(Model):
    """PLN-15 闸门的呈报材料 (需求 R205, 功能 planning)."""

    kind: Literal["PLN-15"] = "PLN-15"  # 材料类型标记
    plan: AuditPlan  # 当前计划 (status 不是 superseded 的修订中 revision 最大的), 决定绑定它
    terms: EngagementTerms | None  # 约定书核对结果 TERMS 的当前版本; 功能 acceptance 关闭或没有时为空
    materiality_cents: int  # 整体重要性 (PLN-01/03 选定), 单位分
    performance_materiality_cents: int  # 执行重要性, 单位分
    clearly_trivial_cents: int  # 明显微小门槛, 单位分
    previous_decisions: list[GateDecision]  # 此前的 PLN-15 驳回决定 (含理由; 信息不足的不在其中), 首次为空


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


# 闸门材料之一, 按 kind 区分
GateMaterial = Annotated[
    AcceptanceMaterial | PlanningMaterial | PlanMaterial | CompletionMaterial | ReportMaterial,
    Field(discriminator="kind"),
]


class GateView(Model):
    """闸门页的数据: 当前闸门与要呈报的材料."""

    gate: GateId  # 哪道闸门
    target_id: str  # 材料 ID, 决定须绑定它
    target_version: int  # 材料当前版本, 决定须绑定它
    material: GateMaterial  # 呈报材料


# ── 审计计划页 (需求 R314, 功能 planning) ─────────────────────────────

PlanExtent = Literal["full", "sample"]  # 程序的范围: 全量, 或抽样


class PlanRow(Model):
    """审计计划页程序表的一行: 一个审计目标下的一条程序. 针对几个目标的程序每个目标一行. 程序名称, 方法, 负责智能体
    与状态前端取总览的程序行 (同一来源), 这里不重复给."""

    objective_id: str | None  # 审计目标; 为空为不针对单一认定的程序 (固定集或只经依赖装配的)
    procedure_id: str  # 程序编号
    extent: PlanExtent  # 范围: 计划项 params 里有整数 sample_size 为抽样, 否则全量
    sample_size: int | None = None  # 抽样时的样本量, 全量为空


class PlanView(Model):
    """审计计划页的数据 (GET /engagements/{eid}/plan): 读检查点状态, 不随闸门消失, 批准后与执行中照样可读.
    功能 planning 关闭或还没起草计划时 current_plan_id 为空, 各列表为空."""

    current_plan_id: str | None  # 当前计划 (status 不是 superseded 的修订中 revision 最大的); 没有为空
    plans: list[AuditPlan]  # 各修订的最新版本, 按 revision 升序
    decisions: list[GateDecision]  # 全部 PLN-15 决定, 按决定时间
    objectives: list[AuditObjective]  # 当前计划各项涉及的审计目标, 按状态中的顺序
    # 特别风险: 当前计划特别风险目标 (significant_risk_objective_ids) 下等级为 significant 的风险项, 按风险清单的顺序
    risks: list[RiskItem]
    rows: list[PlanRow]  # 程序表: 按审计目标在状态中的顺序分组, 组内按程序编号; 没有目标的排最后


# ── 判断呈递 (GEN-11, 需求 R152) ─────────────────────────────

SideRole = Literal["generator", "reviewer"]  # 生成方 (产出判断的智能体, 如 A6) 与独立复核方 (A7)
EvidenceOrigin = Literal["generator", "reviewer", "both"]  # 证据由哪一方引用: 只有生成方, 只有复核方, 两方都引用
FailureSource = Literal["run", "llm_call", "exception"]  # 失败原因的出处: 整次执行, 某个对象的大模型调用, 无归因的例外
ValidationLevel = Literal["syntax", "structure", "reference", "semantic"]  # 输出校验的级别, 同 ValidationReport


class PresentedSide(Model):
    """呈递中的一方: 结论, 推理链与建议. 只有两方各自的内容, 没有比较两方或推荐决定的字段."""

    conclusion: str | None  # 结论; 复核方为第一轮只看证据得出的独立结论 (critic_conclusion)
    reasoning_chain: list[ReasoningStep]  # 推理链; 复核方为 critic_reasoning_chain
    proposed_action: ProposedAction | None  # 建议动作; 复核方为 critic_proposed_action (collab 之前的判断为空)
    misstatement_draft: MisstatementDraft | None = None  # 错报草稿; 复核方为 critic_misstatement_draft
    counter_argument: str | None = None  # 反方论证, 只有生成方
    confidence: float | None = None  # 置信度, 只有生成方
    reason_type: CriticReasonType | None = None  # 不一致的理由类型 (critic_reason_type), 只有复核方
    reason: str | None = None  # 复核的理由 (critic_reason), 只有复核方


class PresentedEvidence(Model):
    """两方证据的并集中的一项: 按 judgment_engine.evidence_key 去重 (文件按来源与页, 客户画像按来源与字段,
    其余按来源)."""

    pointer: EvidencePointer  # 同一比对键先出现的那个指针 (生成方在前: 结论证据, 再推理链各步; 复核方按推理链)
    origin: EvidenceOrigin  # 哪一方引用了它


class PresentedFailure(Model):
    """失败类与无归因类任务的失败原因. 按出处填对应的几项, 其余为空: run 填 reason; llm_call 填 subject_id 与调用的
    四项 (level_failed, errors, attempts, trail_id); exception 填规则三项 (rule_id, amount_cents, detail), 另填该对象
    最后一次调用的四项 (需求 R355; 留痕里找不到该对象的调用时只有规则三项)."""

    source: FailureSource  # 出处
    reason: str | None = None  # run: 执行记录的 failure_reason
    subject_id: str | None = None  # llm_call: 失败的判断对象 (任务的 subject_id)
    level_failed: ValidationLevel | None = None  # llm_call, exception: 最后一次没通过的校验级别
    errors: list[str] = []  # llm_call, exception: 最后一次的校验错误
    attempts: int | None = None  # llm_call, exception: 调用次数 (最后一次的 attempt)
    trail_id: str | None = None  # llm_call, exception: 取的那行 llm_call 留痕
    rule_id: str | None = None  # exception: 命中的规则
    amount_cents: int | None = None  # exception: 例外金额, 单位分
    detail: dict[str, Any] = {}  # exception: 规则输出的明细


class TaskPresentation(Model):
    """判断呈递 (POST review-tasks/{task_id}/presentations): 审计师打开一条复核任务时按任务类型组装的视图.

    任务, 程序名, 允许的决定, 仲裁结果, 金额与档位同复核队列 (ReviewTaskView) 的取法; 另给两方 (sides), 两方证据的
    并集 (evidence) 与失败原因 (failure). 规则见 开发接口文档.md 第 10 节 "判断呈递".
    """

    presentation_id: str  # 这次呈递写的 presentation 留痕 ID
    task: ReviewTask  # 任务的最新版
    procedure_name: str  # 程序名称
    target: Judgment | AuditException | ProcedureRun  # 任务绑定的那一版 (task.target_version)
    allowed_decisions: list[Decision]  # 同复核队列
    allowed_resolutions: list[DisputeResolution] = []  # 争议任务可选的仲裁结果, 其余为空
    amount_cents: int | None  # 涉及金额, 同复核队列
    tier: ReviewTier  # 复核档位, 同复核队列
    deferred: bool  # 是否标了稍后处理 (已决定的任务为假)
    # 两方: 目标是判断时有 generator; dispute 任务另有 reviewer. 目标是例外或执行记录时为空
    sides: dict[SideRole, PresentedSide] = {}
    evidence: list[PresentedEvidence] = []  # 两方证据的并集; 没有 sides 时为空 (例外的证据在 target.evidence)
    failure: PresentedFailure | None = None  # failed 与 unattributed 任务的失败原因, 其余为空
