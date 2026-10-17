"""领域对象, 定义见 开发接口文档.md 第 2 节. 其他包只引用, 不另行定义."""

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    """所有领域对象的基类: 建好后不可修改, 不许出现未定义的字段."""

    model_config = ConfigDict(frozen=True, extra="forbid")  # frozen 不可修改, extra=forbid 不许多字段


# ── 2.1 枚举 ──────────────────────────────────────────────


class Phase(StrEnum):
    """项目阶段."""

    P0_Acceptance = "P0_Acceptance"  # 承接
    P1_Planning = "P1_Planning"  # 计划
    P2_Executing = "P2_Executing"  # 执行
    P3_Completion = "P3_Completion"  # 完成
    P4_Reporting = "P4_Reporting"  # 报告
    Archived = "Archived"  # 终态: 签字归档
    Declined = "Declined"  # 终态: 拒绝承接


class Assertion(StrEnum):
    """审计认定."""

    Occurrence = "Occurrence"  # 发生
    Completeness = "Completeness"  # 完整性
    Accuracy = "Accuracy"  # 准确性
    Cutoff = "Cutoff"  # 截止
    Existence = "Existence"  # 存在
    Valuation = "Valuation"  # 计价
    Rights = "Rights"  # 权利


class RiskLevel(StrEnum):
    """风险等级."""

    low = "low"  # 低
    medium = "medium"  # 中
    high = "high"  # 高
    significant = "significant"  # 特别风险


class Engine(StrEnum):
    """程序的执行方式."""

    rule = "rule"  # 规则: 执行 SQL
    llm = "llm"  # 大语言模型
    hybrid = "hybrid"  # 规则筛选加模型判断
    manual = "manual"  # 人工, 用于闸门


class AutonomyLevel(StrEnum):
    """AI 自主度."""

    L1 = "L1"  # AI 建议, 人逐条决定
    L2 = "L2"  # AI 判断, 人逐条复核
    L3 = "L3"  # AI 推进, 人只看异常
    L4 = "L4"  # AI 全自主, 人事后抽查


class ProcedureStatus(StrEnum):
    """程序状态."""

    planned = "planned"  # 计划中
    executing = "executing"  # 执行中
    awaiting_human = "awaiting_human"  # 等人处理: 有待决复核任务
    completed = "completed"  # 已完成


class RunTrigger(StrEnum):
    """一次执行的起因."""

    initial = "initial"  # 首次执行
    rerun = "rerun"  # 按复核决定重跑


class RunStatus(StrEnum):
    """一次执行的状态."""

    running = "running"  # 运行中
    succeeded = "succeeded"  # 成功
    failed = "failed"  # 失败


class EvidenceType(StrEnum):
    """证据种类."""

    voucher = "voucher"  # 凭证, 如分录, 试算表
    invoice = "invoice"  # 发票
    delivery = "delivery"  # 发运单
    bank = "bank"  # 银行流水与账户
    contract = "contract"  # 合同
    external = "external"  # 外部来源, 如登记查询
    standard = "standard"  # 准则片段


class Reliability(StrEnum):
    """证据可靠性, 影响置信度权重."""

    external_original = "external_original"  # 外部原件, 最高
    external_copy = "external_copy"  # 外部副本
    internal_original = "internal_original"  # 内部原件
    internal_copy = "internal_copy"  # 内部副本, 最低


class StepKind(StrEnum):
    """推理步骤类型."""

    fact = "fact"  # 事实, 须带证据
    standard = "standard"  # 准则, 指向准则片段
    inference = "inference"  # 推理, 引用前序步骤
    assumption = "assumption"  # 假设
    limitation = "limitation"  # 限制


class StepStatus(StrEnum):
    """推理步骤有没有证据支撑."""

    supported = "supported"  # 有支撑
    unsupported = "unsupported"  # 证据指针解析不了


class ProposedAction(StrEnum):
    """判断的建议动作."""

    accept = "accept"  # 接受
    close = "close"  # 关闭事项
    misstatement = "misstatement"  # 认定为错报
    more_evidence = "more_evidence"  # 补充证据


class ReviewStatus(StrEnum):
    """判断的复核状态."""

    pending = "pending"  # 待复核
    confirmed = "confirmed"  # 已确认
    overridden = "overridden"  # 被推翻, 人另出结论


class ExceptionDisposition(StrEnum):
    """例外的归因状态."""

    open = "open"  # 未处理
    attributed = "attributed"  # 已被某条判断解释
    unattributed = "unattributed"  # 无法归因, 交人处理


class TaskKind(StrEnum):
    """复核任务类型."""

    normal = "normal"  # 复核一条判断
    unattributed = "unattributed"  # 例外没有判断解释
    failed = "failed"  # 执行或校验失败


class ReviewTier(StrEnum):
    """复核档位 (规格书 9.1 节): 复核任务按金额与置信度分档, 决定审计师要看到什么程度."""

    full_review = "full_review"  # 完整复核: 超过执行重要性, 或独立复核不一致
    check_evidence = "check_evidence"  # 查验证据: 置信度低于 0.70, 或目标不是判断
    read_reasoning = "read_reasoning"  # 阅读推理链: 置信度 0.70-0.90, 或高置信但不能批量
    batch_eligible = "batch_eligible"  # 可批量确认: 置信度高于 0.90 且金额不超过明显微小临界值


class TaskStatus(StrEnum):
    """复核任务状态."""

    pending = "pending"  # 待决
    decided = "decided"  # 已决定


class TargetType(StrEnum):
    """复核任务指向的对象类型."""

    Judgment = "Judgment"  # 判断
    AuditException = "AuditException"  # 例外
    ProcedureRun = "ProcedureRun"  # 失败的执行


class Decision(StrEnum):
    """复核决定."""

    confirm = "confirm"  # 确认
    override = "override"  # 推翻并另出结论
    misstatement = "misstatement"  # 认定错报
    rerun = "rerun"  # 重跑
    manual_result = "manual_result"  # 人工给出结果


class GateId(StrEnum):
    """四道闸门."""

    ACC_01 = "ACC-01"  # 是否承接
    PLN_01_03 = "PLN-01/03"  # 重要性与风险清单
    FIN_01 = "FIN-01"  # 错报与意见类型
    RPT_01 = "RPT-01"  # 报告批准与签字


class GateVerdict(StrEnum):
    """闸门决定."""

    approve = "approve"  # 通过
    reject = "reject"  # 驳回
    insufficient_info = "insufficient_info"  # 信息不足


class OpinionType(StrEnum):
    """审计意见类型."""

    unqualified = "unqualified"  # 无保留
    qualified = "qualified"  # 保留
    adverse = "adverse"  # 否定
    disclaimer = "disclaimer"  # 无法表示意见


class MisstatementType(StrEnum):
    """错报类型."""

    factual = "factual"  # 事实错报
    judgmental = "judgmental"  # 判断错报
    projected = "projected"  # 推断错报, 由抽样外推


class Direction(StrEnum):
    """错报方向."""

    overstated = "overstated"  # 高估
    understated = "understated"  # 低估


class ConfirmationResult(StrEnum):
    """函证结果."""

    pending = "pending"  # 未回函
    agreed = "agreed"  # 相符
    difference = "difference"  # 有差异


class ReceiptChannel(StrEnum):
    """回函渠道, 决定回函的可靠性."""

    direct_mail = "direct_mail"  # 银行直接寄给审计师
    direct_email = "direct_email"  # 银行直接发邮件给审计师
    portal_via_client = "portal_via_client"  # 经客户门户转交
    oral = "oral"  # 口头


class DocumentType(StrEnum):
    """证据文件类型."""

    contract = "contract"  # 合同
    confirmation = "confirmation"  # 询证函回函
    other = "other"  # 其他


class TermType(StrEnum):
    """合同条款类型."""

    payment = "payment"  # 付款
    control_transfer = "control_transfer"  # 控制权转移
    return_right = "return_right"  # 退货权
    rebate = "rebate"  # 返利
    termination = "termination"  # 终止
    guarantee = "guarantee"  # 担保
    related_party = "related_party"  # 关联方


class ReportSection(StrEnum):
    """审计报告段落."""

    opinion = "opinion"  # 意见
    basis_for_opinion = "basis_for_opinion"  # 形成意见的基础
    directors_responsibilities = "directors_responsibilities"  # 董事责任
    auditor_responsibilities = "auditor_responsibilities"  # 审计师责任
    other_legal_and_regulatory = "other_legal_and_regulatory"  # 其他法律法规要求
    signature = "signature"  # 签名块: 审计师姓名, 事务所, 地址


class ReportStatus(StrEnum):
    """报告版本状态."""

    draft = "draft"  # 草稿
    signed = "signed"  # 已签字


class TrailKind(StrEnum):
    """留痕类型."""

    llm_call = "llm_call"  # 大模型调用
    external_call = "external_call"  # 外部调用, 如登记查询, 发函
    decision = "decision"  # 人的决定
    transition = "transition"  # 状态迁移
    deferral = "deferral"  # 人工标记或取消稍后处理


class AgentId(StrEnum):
    """智能体编号. A7, A8 本版不实现, 只占位."""

    A1 = "A1"  # Orchestrator: 编排, 闸门, 程序包装配, 调度
    A2 = "A2"  # ClientIntel: 客户背景与登记核查
    A3 = "A3"  # Ingest: 数据接入与文件抽取
    A4 = "A4"  # RiskAssessor: 分析程序, 重要性, 风险评估
    A5 = "A5"  # Evidence: 对外取证, 如函证发函
    A6 = "A6"  # Fieldwork: 实质性程序与判断
    A7 = "A7"  # Review: 判断独立复核
    A8 = "A8"  # Workpaper: 错报沟通与底稿
    A9 = "A9"  # QC: 错报累计与质量控制
    A10 = "A10"  # Concierge: 审计师对话入口与报告生成


# 证据指针指向哪类存储: 财务数据行, 文件, 准则片段或留痕
PointerSource = Literal[
    "journal_entry",
    "sales_invoice",
    "delivery_note",
    "bank_transaction",
    "bank_account",
    "trial_balance",
    "document",
    "standard",
    "trail",
]
# 财务数据的行类型, PointerSource 的子集
SourceType = Literal[
    "journal_entry", "sales_invoice", "delivery_note", "bank_transaction", "bank_account", "trial_balance"
]
# 重要性基准: 收入, 总资产, 税前利润
MaterialityBasis = Literal["revenue", "total_assets", "profit_before_tax"]
# 证据文件处理状态: 已接收, 已抽取, 需人工处理
DocumentStatus = Literal["received", "extracted", "needs_manual"]


# ── 2.2 证据与判断 ─────────────────────────────────────────


class Versioned(Model):
    """存进审计产出表的对象的基类: 修改时出新版本, 旧版本保留."""

    id: str  # 对象 ID, 规则见开发接口文档第 0 节
    version: int = 1  # 版本号, 从 1 起, 每次修改 +1


class EvidencePointer(Model):
    """证据指针: 结论依据的是哪条证据 (source_id) 的哪一处 (locator), 前端据此打开原文."""

    type: EvidenceType  # 证据种类
    source_type: PointerSource  # 证据存在哪类存储, 决定到哪里解析
    source_id: str  # 那条证据的 ID: 行 ID, 文件 ID, 准则片段 ID 或留痕 ID
    locator: str  # 证据里的哪一处: 列名, 字段名, 条款号或页码描述
    page: int | None = None  # PDF 页码, 从 1 起, 前端据此跳页
    reliability: Reliability | None = None  # 证据可靠性, 准则类为空


class ReasoningStep(Model):
    """推理链中的一步."""

    kind: StepKind  # 步骤类型
    text: str  # 这一步的内容
    evidence: list[EvidencePointer] = []  # 支撑证据, fact 与 standard 必填
    from_steps: list[int] = []  # inference 引用的前序步骤下标
    status: StepStatus = StepStatus.supported  # 有无证据支撑


class MisstatementDraft(Model):
    """判断建议认定错报时附带的错报草稿."""

    type: MisstatementType  # 错报类型
    account: str  # 涉及的科目
    assertion: Assertion  # 涉及的认定
    amount_cents: int  # 金额, 单位分
    direction: Direction  # 高估或低估


class Judgment(Versioned):
    """AI 或人对一个对象作出的判断: 结论, 推理链, 证据, 反方论证, 置信度, 建议动作."""

    procedure_id: str  # 产出它的程序
    run_id: str  # 产出它的那次执行
    subject_id: str  # 判断对象: 合同, 发票或银行账户 ID
    conclusion: str  # 结论
    confidence: float  # 置信度 0-1, 由 JudgmentEngine 计算
    confidence_parts: dict[str, float]  # 置信度分项: evidence, rule, llm
    reasoning_chain: list[ReasoningStep]  # 推理链
    evidence: list[EvidencePointer]  # 结论引用的全部证据
    counter_argument: str  # 反方论证: 结论可能错在哪里
    proposed_action: ProposedAction  # 建议动作
    misstatement_draft: MisstatementDraft | None = None  # 建议认定错报时必填
    exception_ids: list[str] = []  # 本判断解释的例外, 判断确认后这些例外置 attributed
    autonomy_level: AutonomyLevel  # 判断时的有效自主度
    review_status: ReviewStatus = ReviewStatus.pending  # 复核状态
    superseded_by: str | None = None  # 被推翻时, 取代它的人工判断 ID
    performed_by: str  # 用户 ID: AI 生成的记项目负责人, 人工建的记决定人
    created_at: datetime  # 生成时间
    trail_ref: str | None = None  # 生成它的那次大模型调用的留痕 ID


class AuditException(Versioned):
    """例外: 规则程序查出的异常, 如跨期发票, 余额不符. 规格书中叫 Exception, 改名避开 Python 内置名."""

    procedure_id: str  # 查出它的程序
    run_id: str  # 查出它的那次执行
    rule_id: str  # 命中的规则编号, 如 R01
    severity: Literal["low", "medium", "high"]  # 严重程度
    source_ids: list[str]  # 涉及的数据行 ID
    amount_cents: int | None = None  # 涉及金额, 单位分
    detail: dict[str, Any] = {}  # 其他明细, 如规则 SQL 的输出列
    evidence: list[EvidencePointer]  # 证据指针
    disposition: ExceptionDisposition = ExceptionDisposition.open  # 归因状态
    attributed_by: str | None = None  # 解释它的判断 ID


# ── 2.3 计划与程序 ─────────────────────────────────────────


class ClientProfile(Versioned):
    """客户画像: 客户申报的资料, 如法定名称, ABN, 主体类型, 行业, 董事. ACN 从 ABN 登记记录取. id 即 client_id."""

    legal_name: str  # 法定名称
    abn: str  # ABN, 11 位数字
    entity_type: str  # 主体类型, 如 small_proprietary
    industry_anzsic: str  # ANZSIC 行业代码
    listed: bool  # 是否上市
    directors: list[str]  # 董事名单
    notes: str = ""  # 背景备注


class Engagement(Model):
    """审计项目: 一个客户一个财年的一次审计, 记录所处阶段与是否已锁定. 客户名称, ABN 从客户画像取."""

    id: str  # 项目编号 ENG-{yyyy}-{nnn}, 也是图的 thread_id
    client_id: str  # 引用的客户画像 ClientProfile.id
    period_start: date  # 财年起始日
    period_end: date  # 财年截止日
    dataset: str  # 载入的合成数据集名, demo 为 "demo"
    owner_id: str  # 项目负责人的用户 ID
    client_profile_version: int  # 本项目采用的客户画像版本: 建项目时的版本, 本项目更新客户资料后为新版本
    phase: Phase  # 当前阶段
    locked: bool = False  # 签字或拒绝承接后锁定, 拒绝一切写入
    version_snapshot: dict[str, str] = {}  # 启动时冻结: 程序库 commit, LLM 模式与模型, prompt 摘要, 冻结时刻
    created_at: datetime  # 创建时间
    updated_at: datetime  # 最后更新时间


class AuditObjective(Versioned):
    """审计目标: 某科目的某项认定, 风险等级取其下风险项的最高等级."""

    account: str  # 科目, 如 Revenue, Cash
    assertion: Assertion  # 认定
    risk_level: RiskLevel  # 取其下风险项的最高等级, 闸门修改后重算
    is_material: bool  # 是否重大科目


class RiskItem(Versioned):
    """风险清单中的一项, 挂在某个审计目标下, 可在 PLN-01/03 闸门改等级."""

    objective_id: str  # 所属审计目标
    assertion: Assertion  # 认定
    level: RiskLevel  # 风险等级
    fraud_related: bool  # 是否与舞弊相关
    description: str  # 风险描述
    revision_reason: str | None = None  # 闸门改等级时填的理由
    approved_by: str | None = None  # 确认它的审计师


class Procedure(Versioned):
    """本项目中一条审计程序的实例, 如 REV-02. 模板见 knowledge/procedures/."""

    phase: Phase  # 所属阶段
    template_commit: str  # 实例化时程序库的 git commit
    objective_ids: list[str] = []  # 覆盖的审计目标
    params: dict[str, Any] = {}  # rerun 决定改的参数; 建执行记录时叠加在模板参数的解析结果上, 同名键以它为准
    autonomy_level: AutonomyLevel  # 自主度
    status: ProcedureStatus = ProcedureStatus.planned  # 程序状态
    rerun_subject_ids: list[str] = []  # 重跑时只重判的对象 (rerun 关闭的那组失败任务的), 为空则整条重跑; 重跑后清空


class ProcedureRun(Versioned):
    """程序的一次执行记录: 第几次, 起因, 起止时间, 成败. 重跑会产生新记录."""

    procedure_id: str  # 所属程序
    run_no: int  # 第几次执行, 从 1 起
    trigger: RunTrigger  # 首次执行还是重跑
    instance: str | None = None  # 执行它的 fieldwork 实例, P2 以外为空
    params_snapshot: dict[str, Any]  # 执行时的参数快照: 模板参数解析结果叠加 Procedure.params
    started_at: datetime  # 开始时间
    ended_at: datetime | None = None  # 结束时间, 运行中为空
    status: RunStatus  # 运行中, 成功或失败
    failure_reason: str | None = None  # 失败原因


class ScheduleRow(Model):
    """P2 调度表的一行: 某一层的某个 fieldwork 实例负责哪些程序."""

    layer: int  # 依赖分层的层号, 从 0 起
    instance: str  # fieldwork 实例名 fw-{layer}-{k}
    procedure_ids: list[str]  # 该实例负责的程序


# ── 2.4 闸门材料 ───────────────────────────────────────────


class RegistryCheck(Model):
    """ACC-04 的一项登记检查结果."""

    check: Literal["abn_active", "name_match", "company_registered"]  # 检查项
    passed: bool  # 是否通过
    detail: str  # 给审计师看的说明
    evidence: EvidencePointer  # 指向那次登记查询的留痕


class AcceptanceScorecard(Versioned):
    """承接评分卡: ACC-04 三项检查的汇总, ACC-01 闸门的呈报材料."""

    registry_checks: list[RegistryCheck]  # 三项检查结果
    exception_ids: list[str]  # 未通过的检查生成的例外
    client_profile_version: int  # 核查时用的客户画像版本


class AnalyticsResult(Versioned):
    """PLN-07 初步分析结果: 毛利率, 收入增长率等比率及超阈值例外."""

    ratios: dict[str, float]  # 比率: gross_margin_current, gross_margin_prior, revenue_growth, 四位小数
    exception_ids: list[str]  # 变动超阈值的例外


class MaterialityCandidate(Model):
    """按一个基准 (收入, 总资产或税前利润) 算出的重要性候选区间."""

    basis: MaterialityBasis  # 基准
    base_amount_cents: int  # 基准金额, 单位分
    pct_low: float  # 比例下限
    pct_high: float  # 比例上限
    low_cents: int  # 候选下限 = 基准金额 × pct_low
    high_cents: int  # 候选上限 = 基准金额 × pct_high


class MaterialityCandidates(Versioned):
    """PLN-01 算出的全部重要性候选, PLN-01/03 闸门的呈报材料."""

    candidates: list[MaterialityCandidate]  # 各基准的候选区间
    performance_pct_range: tuple[float, float] = (0.5, 0.75)  # 执行重要性占整体重要性的比例范围
    clearly_trivial_pct: float = 0.05  # 明显微小门槛占整体重要性的默认比例


class UncorrectedItem(Model):
    """未更正错报汇总表中的一行."""

    misstatement_id: str  # 对应的错报
    amount_cents: int  # 金额, 单位分
    direction: Direction  # 高估或低估


class UncorrectedSchedule(Versioned):
    """未更正错报汇总表: FIN-04 产出, FIN-01 闸门的呈报材料. 规格书中叫 UncorrectedMisstatementSchedule."""

    items: list[UncorrectedItem]  # 高于明显微小门槛的错报
    excluded_trivial_ids: list[str]  # 低于门槛, 不累计的错报
    total_cents: int  # items 金额合计, 不按方向抵销
    materiality_cents: int  # 整体重要性, 单位分
    candidate_opinion: OpinionType  # 合计不低于整体重要性为保留, 否则无保留


# ── 2.5 复核与决定 ─────────────────────────────────────────


class ReviewTask(Versioned):
    """复核任务: 需要人处理的一件事, 进复核队列."""

    kind: TaskKind  # 任务类型
    procedure_id: str  # 所属程序
    target_type: TargetType  # 要处理的对象类型
    target_id: str  # 要处理的对象 ID
    target_version: int  # 建任务时对象的版本, 提交决定时校验
    run_id: str  # 发现问题的那次执行
    subject_id: str | None = None  # failed, unattributed 任务的判断对象; 整次执行失败时为空
    status: TaskStatus = TaskStatus.pending  # 待决或已决定
    created_at: datetime  # 创建时间


class ManualResult(Model):
    """人工直接给出的结果, 用于 manual_result 决定."""

    conclusion: str  # 人给出的结论
    evidence: list[EvidencePointer] = []  # 人给出的证据


class ReviewRecord(Versioned):
    """审计师对一个复核任务作出的决定及理由."""

    task_id: str  # 处理的任务
    target_version: int  # 从任务复制, 提交时校验
    decision: Decision  # 决定
    reason: str | None = None  # 理由, confirm 以外必填
    override_conclusion: str | None = None  # override 时的新结论
    misstatement_draft: MisstatementDraft | None = None  # misstatement 时的错报草稿
    rerun_params: dict[str, Any] | None = None  # rerun 时要改的参数
    manual_result: ManualResult | None = None  # manual_result 时人给的结果
    reviewed_by: str  # 决定人的用户 ID
    decided_at: datetime  # 决定时间


class GateEdit(Model):
    """PLN-01/03 闸门中对一个风险项等级的修改."""

    target_id: str  # 被改的风险项 ID
    target_version: int  # 被改时的版本, 提交时校验
    field: Literal["level"]  # 只能改等级
    new_value: RiskLevel  # 新等级
    reason: str  # 修改理由


class MaterialityChoice(Model):
    """审计师在 PLN-01/03 闸门选定的重要性基准与比例."""

    basis: MaterialityBasis  # 选定的基准
    pct: float  # 整体重要性占基准的比例, 须在候选区间内
    performance_pct: float  # 执行重要性占整体重要性的比例
    clearly_trivial_pct: float  # 明显微小门槛占整体重要性的比例


class Annotation(Model):
    """RPT-01 退回报告时对某一段的批注."""

    section: ReportSection  # 批注的段落
    quote: str | None = None  # 被批注的原文片段
    comment: str  # 批注内容


class GateDecision(Versioned):
    """审计师在闸门作出的决定, 附该闸门特有的内容 (重要性, 意见类型, 批注等)."""

    gate: GateId  # 哪道闸门
    target_id: str  # 呈报材料 ID, 如 SCORECARD
    target_version: int  # 决定时材料的版本, 旧版本上的决定无效
    decision: GateVerdict  # 通过, 驳回或信息不足
    materiality: MaterialityChoice | None = None  # PLN-01/03 通过时必填
    edits: list[GateEdit] = []  # PLN-01/03 通过时对风险等级的修改
    disposition: OpinionType | None = None  # FIN-01 通过时必填, 只能是无保留或保留
    annotations: list[Annotation] = []  # RPT-01 驳回时必填
    reason: str | None = None  # 理由, 通过以外必填; FIN-01 一律必填
    decided_by: str  # 决定人的用户 ID
    decided_at: datetime  # 决定时间


class Misstatement(Versioned):
    """已认定的错报, 来自某条判断或例外. 本版一律按未更正处理."""

    judgment_id: str | None  # 来源判断, 与 exception_id 二选一
    exception_id: str | None  # 来源例外, 与 judgment_id 二选一
    review_record_id: str  # 认定它的复核决定
    type: MisstatementType  # 错报类型
    account: str  # 科目
    assertion: Assertion  # 认定
    amount_cents: int  # 金额, 单位分
    direction: Direction  # 高估或低估
    status: Literal["uncorrected"] = "uncorrected"  # 本版不做更正流程, 固定为未更正


# ── 2.6 函证, 文件与报告 ───────────────────────────────────


class Confirmation(Versioned):
    """一份银行询证函: 发函, 回函, 回函余额与账面的比对结果."""

    type: Literal["bank"] = "bank"  # 本版只有银行函证
    bank_account_id: str  # 函证的银行账户
    confirming_party: str  # 回函方, 即开户银行
    idempotency_key: str  # 防重复发函: {eid}:CSH-04:{bank_account_id}
    sent_at: datetime  # 发函时间
    reply_document_id: str | None = None  # 回函 PDF 的文件 ID, A5 存档后写入
    receipt_channel: ReceiptChannel | None = None  # 回函渠道, A5 按回执写入
    replied_at: date | None = None  # 回函日期, A5 按回执写入
    balance_date: date | None = None  # 回函余额的截至日, DOC-06 抽取
    confirmed_balance_cents: int | None = None  # 回函余额, 单位分, DOC-06 抽取
    book_balance_cents: int | None = None  # 账面余额, 单位分, CSH-02 写入
    difference_cents: int | None = None  # 回函减账面, 单位分, CSH-02 写入
    result: ConfirmationResult = ConfirmationResult.pending  # 函证结果
    evidence: list[EvidencePointer] = []  # DOC-06 抽取字段的页码指针


class DocumentRef(Model):
    """已存档的证据文件 (合同, 回函等 PDF) 的登记信息."""

    id: str  # DOC-{sha256 前 12 位}
    type: DocumentType  # 文件类型
    file_name: str  # 原文件名
    sha256: str  # 文件哈希, 用于去重与防篡改
    page_count: int  # 页数
    reliability: Reliability  # 可靠性
    status: DocumentStatus  # 处理状态
    uploaded_at: datetime  # 存档时间


class ContractTerm(Model):
    """合同中的一个条款."""

    id: str  # {contract_id}-T{n}
    term_type: TermType  # 条款类型
    text: str  # 条款全文
    page: int  # 所在页码


class Contract(Model):
    """DOC-04 从合同 PDF 抽取出的合同要素与条款."""

    id: str  # 合同编号, 取 PDF 文件名主干
    document_id: str  # 合同 PDF 的文件 ID
    counterparty: str  # 合同对方
    counterparty_abn: str | None  # 对方 ABN
    type: Literal["sales"] = "sales"  # 本版只有销售合同
    amount_cents: int | None  # 合同金额, 单位分
    sign_date: date | None  # 签订日
    terms: list[ContractTerm]  # 抽取出的条款
    evidence: list[EvidencePointer] = []  # 各要素的页码指针, locator 为字段名
    extracted_by_run: str  # 抽取它的那次 DOC-04 执行


class JudgmentRef(Model):
    """报告中引用的一条判断."""

    id: str  # 判断 ID
    version: int  # 引用时的版本


class AuditReportVersion(Versioned):
    """审计报告的一个修订版本, 签字后锁定."""

    revision: int  # 第几轮修订, 从 1 起
    opinion_type: OpinionType  # 意见类型
    sections: dict[ReportSection, str]  # 各段落正文
    judgment_refs: list[JudgmentRef]  # 事实断言引用的已确认判断
    addresses_decision_id: str | None = None  # 本轮修订回应的 RPT-01 退回决定
    status: ReportStatus = ReportStatus.draft  # 草稿或已签字
    report_date: date | None = None  # 报告日, 签字当天
    approved_by: str | None = None  # 签字人的用户 ID
    approved_at: datetime | None = None  # 签字时间


# ── 2.7 其他 ───────────────────────────────────────────────


class User(Model):
    """系统用户, 即审计师."""

    id: str  # 用户 ID
    email: str  # 登录邮箱
    full_name: str  # 姓名


class AbnRecord(Model):
    """ABN Lookup 按 ABN 查到的登记记录."""

    abn: str  # ABN
    acn: str | None  # ACN, 非公司主体为空
    entity_name: str  # 登记的主体名称
    status: Literal["Active", "Cancelled"]  # 有效或已注销
    entity_type: str  # 主体类型


class AsicCompanyRecord(Model):
    """ASIC 按 ACN 查到的公司登记记录."""

    acn: str  # ACN
    name: str  # 登记的公司名称
    status: Literal["Registered", "Deregistered"]  # 已注册或已注销
    registered_date: date  # 注册日期


class TrailEntry(Model):
    """审计留痕的一行: 一次大模型调用, 外部调用, 人的决定或状态迁移. 只追加不修改."""

    id: str | None = None  # 写入时生成 TR-{uuid4}
    engagement_id: str  # 所属项目
    kind: TrailKind  # 留痕类型
    actor: str  # 谁做的: 用户 ID 或智能体编号
    procedure_id: str | None = None  # 相关程序
    run_id: str | None = None  # 相关的那次执行
    summary: str  # 一句话摘要
    model: str | None = None  # 大模型调用时的模型名
    prompt_hash: str | None = None  # 送给大模型的完整输入的 sha256
    input_tokens: int | None = None  # 输入 token 数
    output_tokens: int | None = None  # 输出 token 数
    idempotency_key: str | None = None  # 外部动作的防重复键
    payload: dict[str, Any] = {}  # 明细, 如查询参数与返回结果
    created_at: datetime | None = None  # 写入时间
