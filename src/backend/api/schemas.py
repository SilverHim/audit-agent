"""HTTP 层的请求与响应模型. 领域对象仍用 contracts; 这里只放接口文档第 10 节点名由 C 定义的形状."""

from datetime import date, datetime
from typing import Any, Literal

from backend.contracts.data import TableKind
from backend.contracts.models import (
    AcceptanceScorecard,
    Annotation,
    AuditException,
    AuditReportVersion,
    ClientProfile,
    Decision,
    DeclaredHolding,
    DeclaredParty,
    DisputeResolution,
    DocumentType,
    Fees,
    GateDecision,
    GateEdit,
    GateId,
    GateVerdict,
    Governance,
    KeyPerson,
    LlmProvider,
    ManualResult,
    MaterialityChoice,
    Misstatement,
    MisstatementDraft,
    Model,
    NeitherAction,
    OpinionType,
    Phase,
    PriorEstimateOutcome,
    PriorMisstatement,
    Procedure,
    ProcedureRun,
    ProfileSource,
    ReportingFramework,
    Retention,
    ReviewRecord,
    ReviewTask,
    ReviewTier,
    ServiceOrganisation,
    UncorrectedSchedule,
)
from backend.contracts.records import AccountCategory
from backend.contracts.views import JudgmentView


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
    """新建项目. dataset 与 client_id 二选一: 数据集客户传数据集名, 取数据集里的客户画像 (行为同 R288 之前); 新建的
    客户传 client_id, 取该客户最新一版画像, 项目没有数据集. 客户名称与 ABN 不在此传."""

    period_start: date
    period_end: date
    dataset: str | None = None
    client_id: str | None = None


class DatasetSummary(Model):
    """新建项目时可选的一个客户: 数据集名, 其客户画像的名称与 ABN, 自带的审计期间, 是否默认选中."""

    dataset: str  # 建项目时提交的数据集名, 如 demo, xero/20261017-q1
    legal_name: str
    abn: str
    period_start: date | None = None  # 数据集自带的审计期间, 建项目时须与之相同; 不自带 (如 Numbat) 为空
    period_end: date | None = None
    default: bool = False  # 数据集名等于配置项 DEMO_DATASET, 新建项目表单默认选它


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


class RerunIn(Model):
    """重跑过期程序的请求体: 要重跑的程序; 不填为全部可重跑的过期程序 (需求 R274)."""

    procedure_ids: list[str] | None = None


class ProfileFields(Model):
    """画像的可填字段: ClientProfile 去掉 id 与 version. 原有 6 项必填; notes 与扩展字段 (R127) 都可省略."""

    legal_name: str
    abn: str
    entity_type: str
    industry_anzsic: str
    listed: bool
    directors: list[str]
    notes: str = ""
    financial_year_end: str | None = None
    accounting_system: str | None = None
    engagements: list[str] = []
    approval_limit_cents: int | None = None
    key_personnel: list[KeyPerson] = []
    related_parties: list[DeclaredParty] = []
    ownership: list[DeclaredHolding] = []
    prior_risk_register: dict[str, list[str]] = {}
    prior_misstatements: list[PriorMisstatement] = []
    independence_flags: list[str] = []
    applicable_framework: ReportingFramework | None = None
    governance: Governance | None = None
    fees: Fees | None = None
    service_organisations: list[ServiceOrganisation] = []
    prior_estimate_outcomes: list[PriorEstimateOutcome] = []
    retention: Retention | None = None
    prior_period_audited: bool | None = None


class ClientProfileIn(ProfileFields):
    """更新客户资料. id 由服务端取项目的 client_id; version 为当前版本 +1. 原有 6 项必填; notes 与扩展字段 (R127)
    都可省略, 省略的沿用被替代的那一版画像 (第 version - 1 版), 给出 null 的可空字段即清空. 字段同 ClientProfile."""

    version: int


class ClientCreate(ProfileFields):
    """新建客户 (POST /clients): id 由服务端生成 CLIENT-M-{12 位十六进制}, 版本为 1, 来源 manual. 省略的扩展字段取
    ClientProfile 的默认值."""


class ClientDataset(Model):
    """数据集客户的一个数据集: 建项目时提交的数据集名与自带的审计期间."""

    dataset: str  # 如 demo, xero/20261017-q1
    period_start: date | None = None  # 数据集自带的审计期间; 不自带 (如 Numbat) 为空
    period_end: date | None = None
    default: bool = False  # 数据集名等于配置项 DEMO_DATASET


class ClientSummary(Model):
    """客户列表的一行 (GET /clients): 每个客户一行, 不按数据集."""

    client_id: str
    legal_name: str  # 库里有画像时取最新一版, 否则取数据集画像
    abn: str
    origin: ProfileSource | None  # 客户从哪里来: 第 1 版的来源 (manual, dataset); 老版本没记时按数据集画像判定
    latest_version: int | None  # 库里最新版本号; 还没进库的数据集客户为空
    datasets: list[ClientDataset]  # 该客户的数据集, 按数据集名排序; 新建的客户为空


class ProfileVersionInfo(Model):
    """客户资料页版本历史中一版的出处: 谁, 何时, 来源."""

    version: int
    source: ProfileSource | None  # 老版本为空 (未记录)
    updated_by: str | None  # 写入的用户 ID; 老版本为空
    updated_by_name: str | None  # 写入的用户姓名; 查不到或老版本为空
    updated_at: datetime


class ClientProfileView(Model):
    """客户资料页: 客户画像的最新版与历史, 承接评分卡及其例外, 能否编辑."""

    profile: ClientProfile  # 最新版, 口径同 ACC-01 闸门材料 (可能由同客户的其他项目更新)
    history: list[ClientProfile]  # 第 1 版到最新版, 按版本升序
    versions: list[ProfileVersionInfo]  # 与 history 逐项对应的出处
    engagement_profile_version: int  # 本项目采用的画像版本 (Engagement.client_profile_version)
    scorecard: AcceptanceScorecard | None  # 版本最高的承接评分卡, ACC-04 还没跑过时为空
    exceptions: list[AuditException]  # 评分卡 exception_ids 引用的例外
    editable: bool  # 与 PUT /client-profile 的可用条件一致: 停在 ACC-01, 未锁定, 后台没在运行, 无待恢复的错误


class ReviewTaskView(Model):
    """复核队列中的一项: 任务, 程序名, 目标对象, 允许的决定, 涉及金额与复核档位."""

    task: ReviewTask
    procedure_name: str
    target: JudgmentView | AuditException | ProcedureRun  # 判断经盲判过滤 (BlindFilter) 输出
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
    judgments: list[JudgmentView]  # 经盲判过滤
    stale: bool = False  # 过期, 口径同总览 (services.staleness)
    rerunnable: bool = False  # 过期且能重跑 (执行阶段, 不在等重跑, 不在等数据或未执行), 同总览
    superseded_run_ids: list[str] = []  # 被过期重跑取代的执行: 其例外与判断留作历史, 不再计入


class JudgmentDetail(Model):
    """一条判断的当前版本, 历史版本与相关复核决定."""

    judgment: JudgmentView  # 经盲判过滤
    versions: list[JudgmentView]  # 各版本, 经盲判过滤
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


# 稍后处理的对象: 复核任务, 闸门 (阶段决定) 或补交资料请求
DeferralTarget = Literal["review_task", "gate", "data_request"]


class DeferralIn(Model):
    """标记或取消稍后处理."""

    target_type: DeferralTarget
    target_id: str  # 复核任务填任务 ID, 阶段决定填闸门编号 (如 ACC-01), 补交请求填请求 ID (如 REQ-approval_log)
    deferred: bool  # true 标记, false 取消


class DeferralView(Model):
    """一项有效的稍后处理标记."""

    target_type: DeferralTarget
    target_id: str
    deferred_by: str  # 标记人的用户 ID
    deferred_at: datetime


class DocumentTypeIn(Model):
    """人工指定文件类型."""

    type: DocumentType  # 文件类型
    reason: str  # 理由, 必填


class AccountCategoryIn(Model):
    """审计师修改科目的标准分类."""

    category: AccountCategory  # 新的标准分类, 须属于该科目的类别
    reason: str  # 理由, 必填


class DataReadyIn(Model):
    """确认数据已齐 (POST /data/ready) 的请求体, 可不带."""

    reason: str | None = None  # 理由; 最近一次体检有不通过的项时必填


class RequestUnavailableIn(Model):
    """补交请求标 "客户无此类资料" (需求 R280)."""

    reason: str  # 理由, 必填 (去首尾空白后不能为空)


class ImportSheet(Model):
    """导入时换工作表."""

    sheet: str  # 工作表名, 取解析结果的 sheets 之一


class XeroAuthorize(Model):
    """连接 Xero (GET /data/xero/connect): 前端跳去的授权地址."""

    authorize_url: str  # Xero 授权页地址, 带本次的 state; 10 分钟内有效, 只能用一次


class XeroTenantIn(Model):
    """选定 Xero 组织 (PUT /data/xero/tenant)."""

    tenant_id: str  # 组织 ID, 取 GET /data/xero/tenants 的 id


class XeroSyncStarted(Model):
    """开始同步 (POST /data/xero/sync, 202): 后台计划写的账表. 实际写了哪些表看完成后概览的 scope 与留痕
    xero_synced (live 下银行对账单接口拒绝时银行账户与银行流水都不写)."""

    # 计划写的表: 取付费接口 (live) 时六张, 不取时四张 (没有银行这一对); 期间概览里为 converting
    tables: list[TableKind]


class XeroDisconnected(Model):
    """断开 Xero (DELETE /data/xero)."""

    revoked: bool  # Xero 侧是否已撤销授权; 为 false 时本地令牌已删, 须到 Xero 后台的 Connected apps 手动移除


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
