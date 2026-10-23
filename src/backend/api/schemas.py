"""HTTP 层的请求与响应模型. 领域对象仍用 contracts; 这里只放接口文档第 10 节点名由 C 定义的形状."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel

from backend.contracts.data import DocumentDetailsEdit, ExtractionRecord, TableKind, XeroProfileDecision
from backend.contracts.errors import AbnLookupReason
from backend.contracts.llm_outputs import RegistryExtraction
from backend.contracts.models import (
    AbnRecord,
    AcceptanceScorecard,
    AgentId,
    Annotation,
    AuditException,
    AuditReportVersion,
    AutonomyLevel,
    ClientProfile,
    Confirmation,
    CorrectionNotice,
    CorrectionNoticeKind,
    Decision,
    DeclaredHolding,
    DeclaredParty,
    DisputeResolution,
    DocumentRef,
    DocumentType,
    DraftRule,
    Engine,
    EvidenceType,
    Fees,
    GateDecision,
    GateEdit,
    GateId,
    GateVerdict,
    Governance,
    Judgment,
    KeyPerson,
    LlmProvider,
    ManualRequest,
    ManualResult,
    MaterialityChoice,
    Misstatement,
    MisstatementDraft,
    MisstatementUpdate,
    MisstatementUpdateKind,
    Model,
    NeitherAction,
    OpinionType,
    Phase,
    PlanConfirmation,
    PriorEstimateOutcome,
    PriorMisstatement,
    Procedure,
    ProcedureRun,
    ProfileSource,
    ReportingFramework,
    Retention,
    ReviewRecord,
    ReviewScope,
    ReviewTask,
    ReviewTier,
    ServiceOrganisation,
    UncorrectedSchedule,
)
from backend.contracts.records import AccountCategory


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
    # 工作语言 (需求 R339): 不带为 en, 与之前的输出一致; 前端表单缺省取界面语言. 取值在路由里查
    # (engagements.parse_language), 不在 zh, en 之中报 422 INVALID_VALUE, 不走请求体校验的通用 422
    working_language: str = "en"


class WorkingLanguageIn(Model):
    """改项目的工作语言 (需求 R339). 取值同 EngagementCreate.working_language, 在路由里查
    (engagements.parse_language)."""

    language: str


class CurrencyIn(Model):
    """审计师确认或改项目的记账币种 (需求 R366): ISO 4217 代码, 三个大写字母. 在路由里查 (engagements.parse_currency),
    写法不对报 422 INVALID_VALUE."""

    currency: str


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
    plan_confirmation: PlanConfirmation | None = None  # PLN-15 通过时必填 (需求 R203)
    reason: str | None = None


class CorrectionNoticeIn(Model):
    """更正通知请求体 (需求 R163): CorrectionNotice 去掉 id, version, decided_by, decided_at, 由服务端填写."""

    kind: CorrectionNoticeKind  # 更正分录已入账, 或停止等待
    misstatement_ids: list[str]  # 所列的错报, 须在等待更正的错报中
    adjustment_entry_nos: list[str] = []  # 更正分录的凭证号 (只用于 adjustments_posted; 为空时载入全部未载入的)
    reason: str | None = None  # 理由, 选填 (需求 R358)


class AdjustmentEntryView(Model):
    """可载入的一笔更正分录 (需求 R163, GET adjustment-entries): DatasetLoader.adjustment_entries 的摘要, 另带是否
    已载入."""

    entry_no: str  # 凭证号, 如 AJE-X01
    posting_date: date  # 入账日期
    description: str | None  # 摘要
    line_count: int  # 行数
    loaded: bool  # 总账里已有这个凭证号的行 (已由更正通知载入)


class ReviewRecordIn(Model):
    """复核决定请求体: 去掉 id, version, reviewed_by, decided_at, 以及服务端算的 dwell_seconds, presentation_id
    (需求 R153)."""

    task_id: str
    target_version: int
    decision: Decision
    reason: str | None = None
    override_conclusion: str | None = None
    misstatement_draft: MisstatementDraft | None = None
    # 人给的错报更新载荷 (需求 R159): 带载荷的程序 (FIN-03, FIN-09) 中推翻, 人工给结果与对例外认定错报时必填
    misstatement_update: MisstatementUpdate | None = None
    rerun_params: dict[str, Any] | None = None
    manual_result: ManualResult | None = None
    dispute_resolution: DisputeResolution | None = None
    neither_action: NeitherAction | None = None
    consulted: bool = False  # 仲裁时是否在系统外咨询过 (需求 R150)
    consultation_record: str | None = None  # 咨询对象与结论, consulted 为真时必填 (功能 collab 打开时校验)


class ReviewWithdrawalIn(Model):
    """撤回复核决定的请求体 (需求 R363): 理由选填; 撤回 ID, 撤回人与时间由服务端填写."""

    reason: str | None = None


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
    demo: bool = False  # 带演示资料 (补造目录), 可在数据管理页按阶段导入 (需求 R270, R287); 前端据此标 "演示"


class ClientSummary(Model):
    """客户列表的一行 (GET /clients): 每个客户一行, 不按数据集."""

    client_id: str
    legal_name: str  # 库里有画像时取最新一版, 否则取数据集画像
    abn: str
    origin: ProfileSource | None  # 客户从哪里来: 第 1 版的来源 (manual, dataset); 老版本没记时按数据集画像判定
    latest_version: int | None  # 库里最新版本号; 还没进库的数据集客户为空
    datasets: list[ClientDataset]  # 该客户的数据集, 按数据集名排序; 新建的客户为空


class AbnLookupView(Model):
    """ABN 在线核对 (GET /clients/abn-lookup) 的结果: ABR ABN Lookup 的登记信息, 不写留痕."""

    status: Literal["found", "not_found", "unavailable"]  # 查到, 查不到, 查不了
    record: AbnRecord | None = None  # 查到时的登记信息 (名称不公开时 entity_name 为空串)
    reason: AbnLookupReason | None = None  # 查不了的原因类别 (not_configured, unreachable, rejected, service_error)
    # 给了 legal_name 且查到, 登记名称公开时: 与 ACC-04 同一规则 (client_intel.normalize_name) 比较是否一致; 否则为空
    name_matches: bool | None = None


class ConfirmationView(Confirmation):
    """证据页的函证 (GET /confirmations): 函证各字段, 另加 reply_extracted (当前回函经 DOC-06 成功抽取过,
    agents/ingest.extracted_from 判定, 需求 R104). 应计利息为空时, 抽取过的是回函没提, 没抽取过的是还没读回函
    (如 DOC-06 失败后审计师手填了余额)."""

    reply_extracted: bool


class RegistryExtractView(Model):
    """上传的一份 ASIC 摘录的抽取结果的一版 (GET /registry-extracts 取各份的当前版, history 取全部版本, 需求 R290):
    文件 ID, 版本号 (1 为 A3 抽取, 之后每次重抽或审计师修改加一), 内容, 谁, 何时, 改了哪个字段与理由 (抽取所得为空)."""

    document_id: str
    version: int
    extraction: RegistryExtraction
    changed_by: str
    changed_at: datetime
    field: str | None
    reason: str | None

    @classmethod
    def of(cls, record: ExtractionRecord) -> "RegistryExtractView":
        return cls(
            document_id=record.target_id,
            version=record.version,
            extraction=RegistryExtraction.model_validate(record.data),
            changed_by=record.changed_by,
            changed_at=record.changed_at,
            field=record.field,
            reason=record.reason,
        )


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
    target: Judgment | AuditException | ProcedureRun
    allowed_decisions: list[Decision]
    amount_cents: int | None  # 涉及金额, 保留正负号; 取值顺序见 services.review_queue.task_amount
    tier: ReviewTier  # 复核档位, 按金额绝对值与置信度判定 (services.review_queue.review_tier)
    # 争议任务可选的仲裁结果 (独立复核的推理链无效时没有 a7_upheld); 其余任务为空
    allowed_resolutions: list[DisputeResolution] = []
    # 各可选仲裁的错报草稿规则 (功能 collab 打开时的争议任务, ReviewQueue.draft_rules, 与校验同一规则); 其余为空
    draft_rules: dict[DisputeResolution, DraftRule] = {}
    # 所属程序模板的错报更新类别 (FIN-03 classification, FIN-09 verification, 需求 R163), 前端按它给分类或验证表单;
    # 其余程序为空
    misstatement_update_kind: MisstatementUpdateKind | None = None
    # allowed_decisions 中须填 misstatement_update 的决定 (orchestration.decisions.update_decisions: 推翻, 对例外认定
    # 错报, 失败任务给手工结果, 需求 R159); 模板没有载荷类别时为空. 前端只看它, 不另抄规则
    update_decisions: list[Decision] = []
    # 现在可以撤回的决定 (需求 R363): 已决定的任务上最近一条没撤回的决定, 能撤回 (decisions.withdraw_problem) 且总览
    # can_withdraw 为真时为它的 ID, 否则为空. 前端据此给撤回入口 (POST review-records/{id}/withdraw)
    withdrawable_record_id: str | None = None


class ManualTaskView(Model):
    """人工队列中的一项 (需求 R94): manual 任务, 程序名与模板 owner, 交给人的请求, 结果模型的 JSON schema (没有结果
    模型时为空), 本项目可作证据的已登记文件 (选证据用, 每项相同) 与各文件作证据时的指针类型 (按文件 ID, 判断引擎的
    文件指针映射). EvidencePointer.type 必填, 前端据此交真实的种类, 不填等后端改写的占位值, 也不另抄一份映射;
    写入时后端照旧按文件登记改写 type 与 reliability (decisions.check_manual_result), 以后端为准."""

    task: ReviewTask
    procedure_name: str
    owner: AgentId
    request: ManualRequest
    result_schema: dict[str, Any] | None
    documents: list[DocumentRef]
    evidence_types: dict[str, EvidenceType]  # documents 中每份文件都有, 前端缺了即报错, 不兜底


class ProcedureCatalogEntry(Model):
    """系统已实现程序的公开说明, 不受项目程序包和功能开关过滤, 不含 SQL 或提示词."""

    id: str
    name: str
    phase: Phase
    owner: AgentId
    engine: Engine
    autonomy_level: AutonomyLevel
    feature: str | None
    depends_on: list[str]
    standards: list[str]


class ProcedureDetail(Model):
    """一条程序的执行记录, 例外与判断."""

    procedure: Procedure
    name: str
    depends_on: list[str]
    standards: list[str]
    runs: list[ProcedureRun]
    exceptions: list[AuditException]
    judgments: list[Judgment]
    stale: bool = False  # 过期, 口径同总览 (services.staleness)
    rerunnable: bool = False  # 过期且能重跑 (执行阶段, 不在等重跑, 不在等数据或未执行), 同总览
    superseded_run_ids: list[str] = []  # 被过期重跑取代的执行: 其例外与判断留作历史, 不再计入


class JudgmentDetail(Model):
    """一条判断的当前版本, 历史版本与相关复核决定."""

    judgment: Judgment
    versions: list[Judgment]  # 各版本
    review_records: list[ReviewRecord]


class MisstatementReport(Model):
    """错报列表, 未更正汇总, 以及总览里的重要性与意见."""

    items: list[Misstatement]
    schedule: UncorrectedSchedule | None
    materiality_cents: int | None
    opinion_route: OpinionType | None
    correction_notices: list[CorrectionNotice] = []  # 全部更正通知, 按提交时间 (需求 R163)


class ReportBundle(Model):
    """报告各修订版, 以及全部 RPT-01 决定 (含批注)."""

    versions: list[AuditReportVersion]
    decisions: list[GateDecision]


# 稍后处理的对象: 复核任务, 闸门 (阶段决定), 补交资料请求或 Xero 组织信息核对 (需求 R291)
DeferralTarget = Literal["review_task", "gate", "data_request", "xero_profile"]


class DeferralIn(Model):
    """标记或取消稍后处理."""

    target_type: DeferralTarget
    # 复核任务填任务 ID, 阶段决定填闸门编号 (如 ACC-01), 补交请求填请求 ID (如 REQ-approval_log), Xero 核对填其 key
    target_id: str
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
    reason: str = ""  # 理由, 选填 (Issue #61)


class DocumentDetailsIn(DocumentDetailsEdit):
    """补登记一份文件 (PUT /documents/{id}/details, 需求 R90): 字段同 DocumentDetailsEdit, 只给要改的."""


class AccountCategoryIn(Model):
    """审计师修改科目的标准分类."""

    category: AccountCategory  # 新的标准分类, 须属于该科目的类别
    reason: str = ""  # 理由, 选填 (Issue #61)


class DemoImportIn(Model):
    """演示数据导入的请求体 (需求 R270): 阶段为 acceptance, fieldwork 之一; 用字符串, 不认识的 (含 Issue #46 删去的
    statements) 由服务报 422 INVALID_VALUE (同其他数据接口的错误结构)."""

    stage: str


class DataReadyIn(Model):
    """确认数据已齐 (POST /data/ready) 的请求体, 可不带."""

    reason: str | None = None  # 理由, 选填 (Issue #61; 体检有不通过的项时也可不写)


class RequestUnavailableIn(Model):
    """补交请求标 "客户无此类资料" (需求 R280)."""

    reason: str = ""  # 理由, 选填 (Issue #61)


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

    # 计划写的表: 取付费接口 (live) 时七张, 不取时五张 (没有银行这一对); 期间概览里为 converting
    tables: list[TableKind]


class XeroDisconnected(Model):
    """断开 Xero (DELETE /data/xero)."""

    revoked: bool  # Xero 侧是否已撤销授权; 为 false 时本地令牌已删, 须到 Xero 后台的 Connected apps 手动移除


class WorkpaperConclusionIn(Model):
    """审计师改写底稿结论 (POST /workpapers/{ref}/conclusion, 需求 R330)."""

    text: str  # 结论, 去首尾空白后不能为空 (MISSING_FIELD)


class WorkpaperNoteIn(Model):
    """审计师补充底稿说明 (POST /workpapers/{ref}/notes, 需求 R330)."""

    text: str  # 说明, 去首尾空白后不能为空 (MISSING_FIELD)
    item: str | None = None  # 补齐的必填项代号 (须是该底稿可由补充说明补齐的), 只是补充时不给


class WorkpaperAmendmentIn(Model):
    """归档后补充: 服务层将缺失或空白理由映射为 MISSING_FIELD."""

    text: str = ""
    reason: str = ""


class WorkpaperSignoffIn(Model):
    """审计师签署底稿 (POST /workpapers/{ref}/signoff, 需求 R330)."""

    scope: ReviewScope  # 复核范围: full 全部明细, sample 抽查, conclusion_only 只看结论
    opinion: str = ""  # 复核意见, 选填 (Issue #61)


class XeroProfileCheckIn(Model):
    """审计师对 Xero 组织信息与客户画像核对的选择 (POST /data/xero/profile-check, 需求 R291)."""

    key: str  # 看到的核对的 key (sources.xero.profile_check.key); 已不是当前的报 409 PROFILE_CHECK_STALE
    decision: XeroProfileDecision  # keep 保持原样, update 按 Xero 更新画像
    reason: str | None = None  # 保持原样的理由, 选填 (Issue #61)


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


class EvidenceRequestIn(BaseModel):
    """修改补充证据请求 (需求 R369): 只给要改的项, 审计师在发出前改."""

    description: str | None = None
    requested_from: Literal["client", "bank", "other", "management"] | None = None
    related_object_ids: list[str] | None = None


class EvidenceUnavailableIn(BaseModel):
    """补充证据无法取得 (需求 R369): 理由选填."""

    reason: str | None = None


class ManagementRepresentationIn(BaseModel):
    """登记管理层说明为证据 (需求 R369)."""

    attachment_ids: list[str] = []  # 本项目已登记的可选附件
    related_object_ids: list[str] = []  # 对象关联, 供判断读取
    person: str  # 说明人
    stated_on: str  # 说明的日期或时间 (原文)
    content: str  # 说明内容
