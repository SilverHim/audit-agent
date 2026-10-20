"""领域对象, 定义见 开发接口文档.md 第 2 节. 其他包只引用, 不另行定义."""

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    in_dispute = "in_dispute"  # 争议中: 有待决的争议任务 (功能 collab 打开时; 关闭时仍记 awaiting_human)
    completed = "completed"  # 已完成
    awaiting_data = "awaiting_data"  # 等待数据: 模板 inputs 声明的客户资料还没有, 没执行 (需求 R272)
    no_data = "no_data"  # 未执行 (无数据): 客户无此类资料, 或前置程序未执行 (无数据); 终态, 没有执行记录


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
    superseded = "superseded"  # 被取代: 重跑后旧的建议判断由新判断取代 (尚未使用)


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
    dispute = "dispute"  # 独立复核 (A7) 与 A6 结论不一致, 由审计师仲裁
    manual = "manual"  # 人工程序 (engine manual) 由人执行并提交结果 (尚未使用)


class CriticVerdict(StrEnum):
    """独立复核 (A7, GEN-09) 的结论."""

    consistent = "consistent"  # 从证据独立重推的结论与 A6 一致
    inconsistent = "inconsistent"  # 不一致: 建争议任务, 由审计师仲裁
    not_applicable = "not_applicable"  # 命中触发规则, 但复核没有完成 (如复核模型输出不合格), 转人工复核
    skipped = "skipped"  # 不复核: 自主度 L1, L2 由审计师逐条复核, 或未命中触发规则


class CriticTrigger(StrEnum):
    """独立复核 (A7, GEN-09) 命中的触发规则, 按判定顺序 (功能 collab 打开时记进 Judgment.critic_trigger)."""

    main = "main"  # 自主度 L3, L4 且建议 accept 或 close
    internal_copy = "internal_copy"  # 引用的非准则证据全为内部副本
    assumption = "assumption"  # 推理链有 assumption 步骤
    unsupported = "unsupported"  # 推理链有未获证据支撑的步骤
    sample = "sample"  # 按抽样比例抽中


class CriticReasonType(StrEnum):
    """独立复核判不一致的理由类型."""

    insufficient_evidence = "insufficient_evidence"  # 证据不足以支持结论
    different_conclusion = "different_conclusion"  # 同样的证据得出不同结论
    reasoning_error = "reasoning_error"  # 结论不能从其所依据的事实推出


class DisputeResolution(StrEnum):
    """争议任务的仲裁结果."""

    a6_upheld = "a6_upheld"  # 采用 A6 的结论
    a7_upheld = "a7_upheld"  # 采用独立复核的结论与推理链, 建新的判断
    neither = "neither"  # 都不采用, 由 neither_action 决定怎么办


# 仲裁时错报草稿随采纳的一方 (功能 collab, 需求 R150): 该方建议错报须带 (required), 建议其他动作不能带 (hidden),
# 该方没给建议动作与 neither 两可 (optional). services.review_queue.draft_rule 定, 校验与复核队列共用
DraftRule = Literal["required", "hidden", "optional"]


class NeitherAction(StrEnum):
    """仲裁为 neither 时的处理. 追加程序 (环 2) 本版不做."""

    human_conclusion = "human_conclusion"  # 审计师给出结论 (override_conclusion), 效果同推翻


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
    dispute_resolution = "dispute_resolution"  # 仲裁争议 (只用于 dispute 任务)


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
    """证据文件类型. 审计师上传的文件由 A3 分类; 证据种类见 judgment_engine.DOCUMENT_EVIDENCE."""

    contract = "contract"  # 与客户的收入合同, DOC-04 逐份抽取 (上传后由 A3 抽取)
    invoice = "invoice"  # 发票, 含供应商税务发票
    confirmation = "confirmation"  # 询证函回函
    bank_statement = "bank_statement"  # 银行对账单
    minutes = "minutes"  # 会议纪要与决议 (董事会, 股东会), 如委任审计师
    financial_statements = "financial_statements"  # 财务报表 (含报表草稿); 不作证据, 判断引报表行
    registry_extract = "registry_extract"  # 登记机构摘录, 如 ASIC 公司摘录与人员检索
    engagement_letter = "engagement_letter"  # 审计业务约定书
    representation_letter = "representation_letter"  # 管理层声明书
    correspondence = "correspondence"  # 往来函件与邮件, 含董事函
    governance_communication = "governance_communication"  # 与治理层的沟通
    agreement = "agreement"  # 收入合同以外的协议: 租约, 借款协议, 采购协议, 客户订单; 不进 DOC-04
    internal_record = "internal_record"  # 公司内部记录: 登记表, 报销单, 工资册, 政策, 备忘录与预测, 报价单
    other = "other"  # 其他, 不作证据


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
    conversation = "conversation"  # 审计助理的一轮问答
    conversation_summary = "conversation_summary"  # 审计助理对话历史的压缩摘要
    data = "data"  # 数据管理: 导入, 同步, 上传, 分类, 修改, 体检, 补交请求, 确认数据已齐
    presentation = "presentation"  # 判断呈递 (GEN-11): 审计师打开一条复核任务, 每次一行 (services/presentation)


class AgentId(StrEnum):
    """智能体编号. A8 本版不实现, 只占位."""

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


# 证据指针指向哪类存储: 财务数据行, 文件, 准则片段, 留痕, 或本项目采用版本的客户画像 (管理层申报, 需求 R128)
PointerSource = Literal[
    "journal_entry",
    "sales_invoice",
    "supplier_bill",  # 应付账单 (需求 R178)
    "delivery_note",
    "bank_transaction",
    "bank_account",
    "trial_balance",
    "document",
    "standard",
    "trail",
    "client_profile",
]
# 财务数据的行类型, PointerSource 的子集
SourceType = Literal[
    "journal_entry",
    "sales_invoice",
    "supplier_bill",  # 应付账单 (需求 R178)
    "delivery_note",
    "bank_transaction",
    "bank_account",
    "trial_balance",
]
# 重要性基准: 收入, 总资产, 税前利润
MaterialityBasis = Literal["revenue", "total_assets", "profit_before_tax"]
# 证据文件处理状态: 已接收, 已分类 (不需抽取的类型停在这里), 已抽取, 需人工处理; staged 为智能导入的批次里上传而
# 尚未确认 (需求 R277): 类型为 other, 不被程序使用, 启动时也不补做, 确认时按确认单改为对应的类型与状态;
# not_imported 为确认时没勾选的文件 (未导入, 终态): 类型仍为 other, 不被程序与体检使用, 要导入就重新上传
DocumentStatus = Literal["received", "classified", "extracted", "needs_manual", "staged", "not_imported"]
# 需人工处理时卡在哪一步: 分类 (判断不了类型), 读取 (读不出文字), 抽取 (抽取校验不过), 关联 (对不上业务对象)
DocumentFailedStep = Literal["classify", "read", "extract", "link"]
# 文件的来源渠道: 审计师上传, 银行直接回函 (函证平台), Xero 附件
DocumentChannel = Literal["auditor_upload", "bank_direct", "xero"]


# ── 2.2 证据与判断 ─────────────────────────────────────────


class Versioned(Model):
    """存进审计产出表的对象的基类: 修改时出新版本, 旧版本保留."""

    id: str  # 对象 ID, 规则见开发接口文档第 0 节
    version: int = 1  # 版本号, 从 1 起, 每次修改 +1


class EvidencePointer(Model):
    """证据指针: 结论依据的是哪条证据 (source_id) 的哪一处 (locator), 前端据此打开原文."""

    type: EvidenceType  # 证据种类
    source_type: PointerSource  # 证据存在哪类存储, 决定到哪里解析
    source_id: str  # 那条证据的 ID: 行 ID, 文件 ID, 准则片段 ID, 留痕 ID 或客户画像 ID (client_id)
    locator: str  # 证据里的哪一处: 列名, 字段名 (客户画像为顶层字段名), 条款号或页码描述
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
    # 独立复核 (A7, GEN-09) 的结果, 由 A6 在判断第 1 版写入前同步调用 A7 填写; 人工建的判断不经复核
    critic_verdict: CriticVerdict = CriticVerdict.skipped  # 复核结论
    critic_trigger: CriticTrigger | None = None  # 命中的触发规则 (功能 collab 打开时); 跳过, collab 关闭与旧数据为空
    critic_reason_type: CriticReasonType | None = None  # 不一致的理由类型, 其余为空
    critic_reason: str | None = None  # 理由: 一致或不一致的依据, 跳过或没做成的原因
    critic_conclusion: str | None = None  # 第一轮只看证据得出的独立结论, 争议时与 A6 结论并排
    critic_reasoning_chain: list[ReasoningStep] = []  # 第一轮的独立推理链, 无效时仲裁不提供 a7_upheld
    # 第一轮的建议动作与错报草稿 (功能 collab 打开时写回, 需求 R150); 草稿没通过第一轮校验 (建议错报而缺, 金额对不上,
    # 不建议错报却给了) 时为空. 仲裁 a7_upheld 时据此要求或不许带错报草稿, 前端预填
    critic_proposed_action: ProposedAction | None = None
    critic_misstatement_draft: MisstatementDraft | None = None
    critic_model: str | None = None  # 复核用的模型名 (响应里的)
    critic_trail_ref: str | None = None  # 第二轮 (比对) 调用的留痕 ID


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


# 客户画像中申报的个人或公司
PartyKind = Literal["person", "company"]
# 适用的财务报告框架: 第 1 层或第 2 层通用目的财报 (AASB 1053), 特殊目的财报
ReportingFramework = Literal["tier1_gpfs", "tier2_gpfs", "spfr"]


def digits_problem(value: str | None, length: int, name: str) -> str | None:
    """value 不是 length 位 ASCII 数字时的说明, 合格或为空时为 None (只查格式, 不查校验位)."""
    if value is None or re.fullmatch(rf"[0-9]{{{length}}}", value):
        return None
    return f"{name} 须为 {length} 位数字: {value!r}"


class KeyPerson(Model):
    """客户画像中的关键人员 (规格书第 4.3 节 key_personnel)."""

    name: str  # 姓名
    role: str  # 职务, 如 Director and Company Secretary
    since: str | None = None  # 任职起始, 按申报原样记录, 如 2018-09-03 或 2021


class DeclaredParty(Model):
    """管理层申报的关联方 (ASA 550 第 13 段询问的结果), 不经核实."""

    name: str  # 名称或姓名
    kind: PartyKind  # 个人或公司
    relationship: str  # 与本公司的关系, 如 sole director and 85% shareholder
    abn: str | None = None  # ABN, 11 位数字
    acn: str | None = None  # ACN, 9 位数字
    notes: str = ""  # 申报的往来, 如 director loan

    @model_validator(mode="after")
    def numbers(self) -> Self:
        """ABN, ACN 只查位数."""
        problem = digits_problem(self.abn, 11, "abn") or digits_problem(self.acn, 9, "acn")
        if problem:
            raise ValueError(problem)
        return self


class DeclaredHolding(Model):
    """申报的股权: 谁持有本公司多少股, 占多少."""

    holder: str  # 持有人
    kind: PartyKind  # 个人或公司
    shares: int | None = Field(default=None, ge=0)  # 股数
    pct: float | None = Field(default=None, ge=0, le=1)  # 持股比例, 0 至 1


class GovernanceMember(Model):
    """治理层的一名成员."""

    name: str  # 姓名
    also_management: bool  # 是否同时任管理层


class Governance(Model):
    """治理层的构成 (ASA 260 识别治理层, ACC-09)."""

    body: str  # 治理机构, 如 board
    members: list[GovernanceMember] = []  # 成员
    all_in_management: bool = False  # 治理层成员是否全部参与管理


Share = Annotated[float, Field(ge=0, le=1)]  # 比例, 0 至 1


class Fees(Model):
    """事务所对本客户的收费 (APES 110 R410 的收费依赖与欠费), 金额为分."""

    audit_fee_cents: int = Field(default=0, ge=0)  # 本期审计费
    non_audit_fee_cents: int = Field(default=0, ge=0)  # 本期非审计服务收费
    firm_total_fee_cents: int = Field(default=0, ge=0)  # 事务所当年总收费
    dependency_pct_by_year: dict[str, Share] = {}  # 各年本客户收费占事务所总收费的比例 (0 至 1), 键为年度
    overdue_cents: int = Field(default=0, ge=0)  # 逾期未收的收费
    overdue_days: int = Field(default=0, ge=0)  # 逾期天数
    contingent: bool = False  # 是否有或有收费
    referral_source: str | None = None  # 介绍来源
    referral_source_fees_pct_by_year: dict[str, Share] = {}  # 各年介绍来源带来的收费占比 (0 至 1)


class PriorMisstatement(Model):
    """以前年度审计发现的错报."""

    engagement: str  # 所在项目编号
    area: str  # 领域, 如 cutoff
    amount_cents: int  # 金额
    corrected: bool  # 是否已更正


class ServiceOrganisation(Model):
    """客户使用的服务机构 (ASA 402)."""

    name: str  # 名称
    service: str  # 提供的服务, 如 payroll
    report_type: Literal["type1", "type2"] | None = None  # 服务机构控制报告的类型, 没有为空
    report_ref: str | None = None  # 报告的引用


class PriorEstimateOutcome(Model):
    """以前年度会计估计的结果 (ASA 540 回顾性复核)."""

    engagement: str  # 所在项目编号
    estimate: str  # 估计项目, 如 ecl
    estimated_cents: int  # 当时的估计数
    actual_cents: int  # 实际结果


class Retention(Model):
    """客户资料的留存安排 (APP 11.2)."""

    created: date  # 建档日
    review_by: date  # 留存复核的截止日
    basis: str  # 依据, 如 APP 11.2


class ClientProfile(Versioned):
    """客户画像: 客户申报的资料, 如法定名称, ABN, 主体类型, 行业, 董事. ACN 从 ABN 登记记录取. id 即 client_id.

    notes 之后的扩展字段 (规格书第 4.3 节, R127) 都有默认值, 旧画像照常读出; 程序参数以 {ref: profile.<字段>} 引用.
    """

    legal_name: str  # 法定名称
    abn: str  # ABN, 11 位数字
    entity_type: str  # 主体类型, 如 small_proprietary
    industry_anzsic: str  # ANZSIC 行业代码
    listed: bool  # 是否上市
    directors: list[str]  # 董事名单
    notes: str = ""  # 背景备注
    financial_year_end: str | None = None  # 财年末, "MM-DD"
    accounting_system: str | None = None  # 会计系统, 如 Xero
    engagements: list[str] = []  # 本所对该客户的项目编号 (项目历史), 为空即首次服务
    approval_limit_cents: int | None = Field(default=None, ge=0)  # 付款审批限额, 为空即没有申报
    key_personnel: list[KeyPerson] = []  # 关键人员
    related_parties: list[DeclaredParty] = []  # 管理层申报的关联方
    ownership: list[DeclaredHolding] = []  # 申报的股权
    prior_risk_register: dict[str, list[str]] = {}  # 以前项目的风险清单, 项目编号 → ["领域:等级", ...]
    prior_misstatements: list[PriorMisstatement] = []  # 以前年度的错报
    independence_flags: list[str] = []  # 独立性事项
    applicable_framework: ReportingFramework | None = None  # 适用的财务报告框架
    governance: Governance | None = None  # 治理层
    fees: Fees | None = None  # 收费
    service_organisations: list[ServiceOrganisation] = []  # 服务机构
    prior_estimate_outcomes: list[PriorEstimateOutcome] = []  # 以前年度会计估计的结果
    retention: Retention | None = None  # 留存安排
    prior_period_audited: bool | None = None  # 上期财报是否经审计, 为空即没有申报

    @model_validator(mode="after")
    def year_end(self) -> Self:
        """financial_year_end 须为合法的月日 "MM-DD" (2 月 29 日也可)."""
        value = self.financial_year_end
        if value is None:
            return self
        if not re.fullmatch(r"[0-9]{2}-[0-9]{2}", value):
            raise ValueError(f"financial_year_end 须为 MM-DD: {value!r}")
        try:
            date(2000, int(value[:2]), int(value[3:]))
        except ValueError:
            raise ValueError(f"financial_year_end 不是合法的月日: {value!r}") from None
        return self


class ProfileSource(StrEnum):
    """客户画像一版的来源 (client_profiles.source)."""

    manual = "manual"  # 新建客户时手工录入 (第 1 版)
    dataset = "dataset"  # 建项目时采用数据集带的画像
    update = "update"  # 审计师在 ACC-01 更新客户资料
    xero = "xero"  # 按 Xero 组织信息更新 (需求 R291): 停在 ACC-01 时本项目采用, 其余时候只给客户写新版本


class ProfileRecord(Model):
    """存储中的一版客户画像及其出处: 谁, 何时, 来源. R288 之前写入的版本 source 与 updated_by 为空."""

    profile: ClientProfile  # 画像
    source: ProfileSource | None  # 来源; 老版本为空 (未记录)
    updated_by: str | None  # 写入这一版的用户 ID; 老版本为空
    updated_at: datetime  # 写入时间


class ClientEntry(Model):
    """存储中的一个客户: 最新一版画像与第 1 版的来源 (客户从哪里来)."""

    profile: ClientProfile  # 最新一版
    first_source: ProfileSource | None  # 第 1 版的来源; 老版本为空


class Engagement(Model):
    """审计项目: 一个客户一个财年的一次审计, 记录所处阶段与是否已锁定. 客户名称, ABN 从客户画像取."""

    id: str  # 项目编号 ENG-{yyyy}-{nnn}, 也是图的 thread_id
    client_id: str  # 引用的客户画像 ClientProfile.id
    period_start: date  # 财年起始日
    period_end: date  # 财年截止日
    dataset: str | None  # 客户来自数据集时为数据集名 (Numbat 为 "demo"); 新建的客户为 None (不读任何数据集)
    owner_id: str  # 项目负责人的用户 ID
    client_profile_version: int  # 本项目采用的客户画像版本: 建项目时的版本, 本项目更新客户资料后为新版本
    phase: Phase  # 当前阶段
    locked: bool = False  # 签字或拒绝承接后锁定, 拒绝一切写入
    version_snapshot: dict[str, str] = {}  # 启动时冻结: 程序库 commit, LLM 模式与模型, prompt 摘要, 功能集, 冻结时刻
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
    # 缺的资料对应的补交请求 ID (REQ-{表种类或文件类型}): awaiting_data 时为还缺的, no_data 时为客户无此类资料的;
    # 其余状态为空 (需求 R272)
    data_request_ids: list[str] = []


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
    # 执行时的项目数据版本 (需求 R16, 总览据此标过期); 承接阶段的程序 (不读财务数据) 与之前的执行为空
    data_version: int | None = None


class ScheduleRow(Model):
    """P2 调度表的一行: 某一层的某个 fieldwork 实例负责哪些程序."""

    layer: int  # 依赖分层的层号, 从 0 起
    instance: str  # fieldwork 实例名 fw-{layer}-{k}
    procedure_ids: list[str]  # 该实例负责的程序


# ── 2.4 闸门材料 ───────────────────────────────────────────


class RegistryCheck(Model):
    """ACC-04 的一项登记检查结果."""

    # 检查项; directors_match (画像董事与 ASIC 摘录的董事一致) 只对新建的客户 (需求 R290)
    check: Literal["abn_active", "name_match", "company_registered", "directors_match"]
    passed: bool | None  # 是否通过; None 为没有可查的登记来源 (新建的客户), 待审计师人工确认, 不出例外
    detail: str  # 给审计师看的说明
    evidence: EvidencePointer  # 指向那次登记查询的留痕


class AcceptanceScorecard(Versioned):
    """承接评分卡: ACC-04 各项检查的汇总 (数据集客户三项, 新建的客户四项, 需求 R290), ACC-01 闸门的呈报材料."""

    registry_checks: list[RegistryCheck]  # 各项检查结果
    exception_ids: list[str]  # 未通过的检查生成的例外
    client_profile_version: int  # 核查时用的客户画像版本


class MonthlyFigures(Model):
    """PLN-07 A03 的一个月: 本期内该月总账分录的收入类, 费用类合计与员工费用 (需求 R103)."""

    month: date  # 月初日期
    revenue_cents: int  # 收入: revenue_categories 的贷减借
    expense_cents: int  # 费用: 类型 expense 的借减贷
    employee_expense_cents: int  # 员工费用: employee_expense_categories 的借减贷 (含在 expense_cents 里)


class AnalyticsResult(Versioned):
    """PLN-07 初步分析结果: 毛利率, 收入增长率等比率, 超阈值例外与月度趋势."""

    ratios: dict[str, float]  # 比率: gross_margin_current, gross_margin_prior, revenue_growth, 四位小数
    exception_ids: list[str]  # 变动超阈值的例外
    monthly: list[MonthlyFigures] = []  # A03 的月度数, 审计期间每月一行 (没有分录的月份为 0), 按月升序; 没有 A03 行为空


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
    dispute_resolution: DisputeResolution | None = None  # dispute_resolution 时的仲裁结果
    neither_action: NeitherAction | None = (
        None  # 仲裁为 neither 时必填, 本版只有 human_conclusion (附 override_conclusion)
    )
    consulted: bool = False  # 仲裁时是否在系统外咨询过 (ASQM 1); 已咨询本身不是处置, 裁决仍是三值之一 (需求 R150)
    consultation_record: str | None = None  # 咨询对象与结论; consulted 为真时必填 (功能 collab 打开时校验)
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


class RerunRequest(Versioned):
    """审计师要求用新数据重跑过期的 P2 程序 (需求 R274, 第 7 节). 重跑以新结果为准: superseded_run_ids 中各次执行的
    判断, 例外与错报留作历史, 不再计入 (services.staleness); 这些程序当时的待决任务一并关闭."""

    procedure_ids: list[str]  # 重跑的程序, 按程序包顺序
    superseded_run_ids: list[str]  # 被取代的执行: 这些程序在请求时已有的全部执行
    closed_task_ids: list[str] = []  # 一并关闭的待决任务
    data_version: int  # 请求时的项目数据版本
    report_revision: int | None = None  # 请求时最新报告的修订号, 没有报告为空; RPT-03 据此按非退回的重拟起草下一稿
    requested_by: str  # 请求人的用户 ID
    requested_at: datetime  # 请求时间


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
    idempotency_key: str  # 防重复发函: {eid}:CSH-04:{bank_account_id}:{识别信息摘要} (evidence.confirmation_key)
    sent_at: datetime  # 发函时间
    reply_document_id: str | None = None  # 回函 PDF 的文件 ID, A5 存档后写入
    receipt_channel: ReceiptChannel | None = None  # 回函渠道, A5 按回执写入
    replied_at: date | None = None  # 回函日期, A5 按回执写入
    balance_date: date | None = None  # 回函余额的截至日, DOC-06 抽取
    confirmed_balance_cents: int | None = None  # 回函余额, 单位分, DOC-06 抽取
    # 回函写明的截至余额日尚未入账的应计利息, 单位分 (银行欠客户为正, 客户欠银行为负, nil 为 0), DOC-06 抽取; 回函
    # 没提到, 还没抽取或旧版本为空 (需求 R104)
    accrued_interest_cents: int | None = None
    book_balance_cents: int | None = None  # 账面余额, 单位分, CSH-02 写入
    difference_cents: int | None = None  # 回函减账面, 单位分, CSH-02 写入
    result: ConfirmationResult = ConfirmationResult.pending  # 函证结果
    evidence: list[EvidencePointer] = []  # DOC-06 抽取字段的页码指针


class DocumentRef(Model):
    """已存档的证据文件 (合同, 回函, 导入的账表原件等) 的登记信息."""

    id: str  # DOC-{sha256 前 12 位}
    type: DocumentType  # 文件类型
    file_name: str  # 原文件名
    sha256: str  # 文件哈希, 用于去重与防篡改
    page_count: int  # 页数; 非 PDF 记 1
    reliability: Reliability  # 可靠性
    status: DocumentStatus  # 处理状态
    uploaded_at: datetime  # 存档时间
    media_type: str = "application/pdf"  # 媒体类型, 如 text/csv
    channel: DocumentChannel = "auditor_upload"  # 来源渠道
    uploaded_by: str | None = None  # 上传人的用户 ID; 系统存档 (如数据集载入, 函证回函) 为空
    failed_step: DocumentFailedStep | None = None  # 需人工处理时卡在哪一步, 其余状态为空
    document_date: date | None = None  # 文件日期, 如签署或发出日; 审计师补登记 (需求 R90), 没登记为空
    related_ids: list[str] = []  # 关联对象 ID, 如公司 ACN, 分录号, 银行流水; 审计师补登记, 没登记为空


class ContractTerm(Model):
    """合同中的一个条款."""

    id: str  # {contract_id}-T{n}
    term_type: TermType  # 条款类型
    text: str  # 条款全文
    page: int  # 所在页码


class Contract(Model):
    """DOC-04 从合同 PDF 抽取出的合同要素与条款. 审计师修改抽取结果时出新版本, 各版本见 extraction_versions."""

    id: str  # 合同编号, 取 PDF 文件名主干
    version: int = 1  # 版本号: 抽取所得为第 1 版 (重新抽取接在已有版本后), 每次审计师修改 +1
    document_id: str  # 合同 PDF 的文件 ID
    counterparty: str  # 合同对方
    counterparty_abn: str | None  # 对方 ABN
    type: Literal["sales"] = "sales"  # 本版只有销售合同
    amount_cents: int | None  # 合同金额, 单位分
    sign_date: date | None  # 签订日
    terms: list[ContractTerm]  # 抽取出的条款
    evidence: list[EvidencePointer] = []  # 各要素的页码指针, locator 为字段名
    extracted_by_run: str  # 抽取它的那次 DOC-04 执行; 上传后由 A3 直接抽取的为 UPLOAD-{文件 ID}


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


# 两套模型配置: 主模型给审计助理与各审计智能体, 复核模型给独立复核智能体 (A7)
LlmSlot = Literal["primary", "review"]
# 模型服务商, 都走 OpenAI 兼容的 Chat Completions 接口; 各家参数差异见 adapters/llm.PROVIDERS
LlmProvider = Literal["anthropic", "openai", "deepseek", "qwen", "ollama", "custom"]


class LlmSettings(Model):
    """设置页保存的一套模型配置 (表 llm_settings, 系统级, 不按项目隔离). 密钥只存密文."""

    slot: LlmSlot  # 哪一套
    provider: LlmProvider  # 服务商
    base_url: str  # 接口地址, http 或 https
    model: str  # 模型名
    api_key_encrypted: str | None = None  # 密钥密文 (Fernet, 主密钥 SETTINGS_SECRET_KEY); 本地 Ollama 可为空
    key_last4: str | None = None  # 密钥最后四位, 给界面显示
    updated_by: str  # 保存人的用户 ID
    updated_at: datetime  # 保存时间


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


class Officeholder(Model):
    """ASIC 公司摘录上的一名高管 (同一人任两职时为两项)."""

    name: str  # 姓名
    birth_date: date | None  # 出生日期, 摘录没有时为空
    role: Literal["director", "secretary"]  # 董事或秘书
    appointed: date  # 任职日


class Shareholding(Model):
    """ASIC 公司摘录上的一名股东."""

    name: str  # 股东名称
    kind: Literal["person", "company"]  # 自然人或公司
    acn: str | None  # 公司股东的 ACN, 自然人为空
    share_class: str  # 股份类别, 如 ORD
    shares: int  # 持股数
    pct: float  # 持股比例, 0 至 1


class AsicExtract(Model):
    """ASIC 公司摘录 (需求 R129): 登记信息, 高管与股东, 回执带 PDF 原件 (登记 mock 读同名 PDF). 字段同补造目录
    registry/extracts/{acn}.json; JSON 键 class 是 Python 保留字, 模型字段为 company_class (别名 class).
    新建的客户的摘录由审计师上传, A3 抽取 (需求 R290): 另有 ABN, 注册地址, 文件 ID 与各字段的页码指针; 登记 mock
    的 JSON 没有这几项, 读出为缺省值."""

    # populate_by_name: 也认字段名 company_class. 检查点按字段名重建 Pydantic 对象 (cls(**字段)), 没有它时
    # company_class 在往返中悄悄丢失; 按字段名 dump 的 payload 读回也要靠它
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    acn: str  # ACN
    name: str  # 公司名称
    type: str  # 公司类型, 如 APTY
    company_class: str = Field(alias="class")  # 公司类别, 如 LMSH
    subclass: str  # 子类, 如 PROP
    status: Literal["Registered", "Deregistered"]
    registered_date: date  # 注册日期
    officeholders: list[Officeholder]
    shareholders: list[Shareholding]
    paid_up_cents: int | None  # 实缴股本 (分), 摘录没有时为空
    file_name: str  # 摘录 PDF 的文件名
    file_bytes: bytes  # 摘录 PDF 原件
    abn: str | None = None  # ABN, 摘录没有时为空 (需求 R290)
    registered_address: str | None = None  # 注册地址, 摘录没有时为空 (需求 R290)
    document_id: str | None = None  # 上传摘录的证据文件 ID; 登记 mock 为空 (需求 R290)
    evidence: list[EvidencePointer] = []  # 上传摘录各字段的页码指针 (抽取所得, locator 为字段名); 登记 mock 为空


class PersonRole(Model):
    """ASIC 人员检索的一项: 在某家公司的一个身份."""

    acn: str  # 公司 ACN
    company_name: str  # 公司名称
    role: Literal["director", "secretary", "shareholder"]
    appointed: date | None  # 任职日, 股东为空
    shares: int | None  # 持股数, 高管为空


class PersonSearch(Model):
    """ASIC 人员检索 (需求 R129): 按姓, 名与出生日期查到的各项身份, 回执带 PDF 原件. 字段同补造目录
    registry/persons/{姓}-{名}-{yyyymmdd}.json."""

    family_name: str  # 姓
    given_name: str  # 名
    birth_date: date  # 出生日期
    roles: list[PersonRole]  # 各项身份, 按检索结果的顺序
    file_name: str  # 检索结果 PDF 的文件名
    file_bytes: bytes  # 检索结果 PDF 原件


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
