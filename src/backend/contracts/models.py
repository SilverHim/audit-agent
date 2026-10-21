"""领域对象, 定义见 开发接口文档.md 第 2 节. 其他包只引用, 不另行定义."""

import re
import unicodedata
from collections.abc import Collection, Iterable
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self, get_args

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError


class Model(BaseModel):
    """所有领域对象的基类: 建好后不可修改, 不许出现未定义的字段."""

    model_config = ConfigDict(frozen=True, extra="forbid")  # frozen 不可修改, extra=forbid 不许多字段


BLANK = (
    "blank"  # 必填文字为空白时的校验错误类型; 人工结果按它报 MISSING_FIELD (decisions.check_manual_result, 需求 R161)
)


def not_blank(text: str) -> str:
    """必填文字去首尾空白后不能为空, 不过抛错误类型 BLANK; 通过时原样返回 (不改人填的文字)."""
    if not text.strip():
        raise PydanticCustomError(BLANK, "不能为空")
    return text


NonBlank = Annotated[str, AfterValidator(not_blank)]  # 必填文字: 去首尾空白后非空


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


class DowngradeCondition(StrEnum):
    """自主度动态降级的条件 (规格书 7.4, 需求 R168; 功能 autonomy). 前两条在装配时看, 其余在判断时看; 各条降几级或
    降到哪级见 knowledge/rules.json 的 autonomy 段 (模板 downgrade_rules 可按条件覆盖). 首次服务该客户, 财报层次风险,
    GEN-17 与按人机一致率的降级本版不做, 不在其中."""

    significant_risk = "significant_risk"  # 装配: 程序关联的审计目标中最高风险为 significant
    high_risk = "high_risk"  # 装配: 最高风险为 high
    amount_over_pm = "amount_over_pm"  # 判断: 判断金额的绝对值超过执行重要性 (执行重要性未定时不适用)
    low_confidence = "low_confidence"  # 判断: 置信度低于阈值 (rules.json 的 low_confidence_below)
    unsupported_steps = "unsupported_steps"  # 判断: 推理链有 unsupported 步骤 (规格书第 14 节)
    fraud_flag = "fraud_flag"  # 判断: 舞弊迹象 (FIN-03 认定 fraud_indicative, 或解释的例外所属规则标 fraud_flag)


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
    incremental = "incremental"  # 增量实例: 只扫更正通知载入的更正分录 (GEN-05, 需求 R194), 程序不出新版本


class RunStatus(StrEnum):
    """一次执行的状态."""

    running = "running"  # 运行中
    succeeded = "succeeded"  # 成功
    failed = "failed"  # 失败


class PlanStatus(StrEnum):
    """总体审计计划一个修订的状态 (需求 R203)."""

    draft = "draft"  # 草稿, 待 PLN-15 闸门批准
    approved = "approved"  # 已批准, 执行按它
    superseded = "superseded"  # 已被下一修订取代 (PLN-15 驳回后重拟, PLN-19 修订重要性)


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
    superseded = "superseded"  # 被取代: 重跑后旧的建议判断由新判断取代, superseded_by 记新判断 (ACC-08 重读, 需求 R200)


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
    manual = "manual"  # 人工程序 (engine manual 且不是闸门) 由人执行并提交结果 (需求 R92)


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


class CriticReasonCode(StrEnum):
    """独立复核跳过 (skipped) 或没做成 (not_applicable) 的原因代码 (需求 R309), 前端按界面语言显示;
    Judgment.critic_reason 照旧写中文原句."""

    critic_off = "critic_off"  # skipped: 功能开关 critic 关闭
    no_review_model = "no_review_model"  # skipped: 未配置复核模型 (只在测试)
    level_l1 = "level_l1"  # skipped: 程序运行时自主度 L1, 审计师逐条复核
    level_l2 = "level_l2"  # skipped: 程序运行时自主度 L2, 审计师逐条复核
    not_triggered = "not_triggered"  # skipped: 未命中触发规则
    no_evidence = "no_evidence"  # not_applicable: 判断没有能解析的证据指针
    first_round_invalid = "first_round_invalid"  # not_applicable: 第一轮输出重试后仍未通过校验
    second_round_invalid = "second_round_invalid"  # not_applicable: 第二轮输出重试后仍未通过校验
    chain_invalid = "chain_invalid"  # not_applicable: 判为一致, 但独立推理链没有通过校验
    review_error = "review_error"  # not_applicable: 复核出错 (调用或读取存储失败)


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
    """闸门. 基线四道; ACC-08 属功能 acceptance, PLN-15 属功能 planning, 登记表 (orchestration/gates.GATES) 的行
    分别由需求 R201, R205 加."""

    ACC_01 = "ACC-01"  # 是否承接
    ACC_08 = "ACC-08"  # 约定条款与委任 (需求 R198)
    PLN_01_03 = "PLN-01/03"  # 重要性与风险清单
    PLN_15 = "PLN-15"  # 总体审计计划 (需求 R203)
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


class GoingConcernOutcome(StrEnum):
    """持续经营结论 (FIN-02, 需求 R183; ASA 570 第 17, 18, 21-23 段)."""

    no_material_uncertainty = "no_material_uncertainty"  # 持续经营基础适当, 不存在重大不确定性
    material_uncertainty_disclosed = "material_uncertainty_disclosed"  # 存在重大不确定性, 披露充分 (第 22 段)
    material_uncertainty_not_disclosed = "material_uncertainty_not_disclosed"  # 存在重大不确定性, 披露不充分 (第 23 段)
    basis_inappropriate = "basis_inappropriate"  # 持续经营假设不恰当 (第 21 段, 否定意见); 闸门拒绝批准, 本版不支持


class MisstatementType(StrEnum):
    """错报类型."""

    factual = "factual"  # 事实错报
    judgmental = "judgmental"  # 判断错报
    projected = "projected"  # 推断错报, 由抽样外推


class MisstatementStatus(StrEnum):
    """错报的生命周期状态 (规格书 12.3 Misstatement 子状态机, 需求 R156; 迁移表见接口文档第 2.5 节). 功能 misstatement
    关闭时新建错报直接为 uncorrected (旧口径); evaluated 与 closed 本版不写."""

    identified = "identified"  # 已认定, 待分类与沟通
    communicated = "communicated"  # 已与管理层沟通, 等更正 (分录对不上时回到这里重新沟通)
    verifying = "verifying"  # 管理层已更正, 待验证更正分录
    corrected = "corrected"  # 验证通过且剩余额为零
    partially_corrected = "partially_corrected"  # 验证通过而剩余额大于零
    uncorrected = "uncorrected"  # 拒绝更正, 或审计师决定停止等待
    evaluated = "evaluated"  # 已作未更正错报评价 (本版不写)
    closed = "closed"  # 已结束 (本版不写)


# 错报来源: 本期发现, 或上期未更正的结转 (ASA 450 第 13 段)
MisstatementOrigin = Literal["current_period", "prior_period_uncorrected"]
UncorrectedBasis = Literal["refused", "stopped_waiting"]  # 未更正的依据: 管理层拒绝更正, 或审计师决定停止等待
# 更正通知的两种 (需求 R162): 管理层的更正分录已入账 (载入分录, 所列错报转 verifying), 或不再等 (所列错报记未更正)
CorrectionNoticeKind = Literal["adjustments_posted", "stop_waiting"]


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
    financial_statements = "financial_statements"  # 财务报表; 只分类, 不作证据, 不参与程序 (以账套为准, Issue #46)
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


# IndependenceDeclaration.status (S307C, 随报告签字) 也用它 (需求 R215): 给报告加只属于报告的状态时另起枚举
class ReportStatus(StrEnum):
    """报告版本状态."""

    draft = "draft"  # 草稿
    signed = "signed"  # 已签字


class RepresentationStatus(StrEnum):
    """管理层书面声明书的状态 (ASA 580, 需求 R215)."""

    requested = "requested"  # 已请求, 还没取得
    obtained = "obtained"  # 已取得
    refused = "refused"  # 管理层不提供 (第 19, 20 段)
    unreliable = "unreliable"  # 取得了, 但审计师认为不可靠 (第 16 至 18 段)


class ClauseStatus(StrEnum):
    """声明书中一项声明的状态."""

    present = "present"  # 声明书写了
    refused_accepted = "refused_accepted"  # 管理层不提供, 审计师接受, 写明对意见的影响 (理由选填, 需求 R358)
    missing = "missing"  # 声明书没写


class CheckItemStatus(StrEnum):
    """合规检查一项的结果 (FIN-10, FIN-18)."""

    passed = "passed"  # 通过
    failed = "failed"  # 不通过
    attention = "attention"  # 不判不通过, 须审计师关注


class CommunicationForm(StrEnum):
    """与治理层沟通的形式 (ASA 260 第 24 段)."""

    written = "written"  # 书面, 底稿留副本
    oral = "oral"  # 口头, 底稿记下内容, 时间与对象


class DeclarationForm(StrEnum):
    """s307C 独立性声明的写法."""

    no_contraventions = "no_contraventions"  # 没有违反独立性要求与职业守则 (s307C(3)(c))
    contraventions = "contraventions"  # 只有声明中列明的违规 (s307C(3)(d))


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
    run = "run"  # 程序执行的开始与结束, 每次执行各一行 (需求 R325, orchestration/trail)


class AgentId(StrEnum):
    """智能体编号."""

    A1 = "A1"  # Orchestrator: 编排, 闸门, 程序包装配, 调度
    A2 = "A2"  # ClientIntel: 客户背景与登记核查
    A3 = "A3"  # Ingest: 数据接入与文件抽取
    A4 = "A4"  # RiskAssessor: 分析程序, 重要性, 风险评估
    A5 = "A5"  # Evidence: 对外取证, 如函证发函
    A6 = "A6"  # Fieldwork: 实质性程序与判断
    A7 = "A7"  # Review: 判断独立复核
    A8 = "A8"  # Workpaper: 错报分类, 沟通与更正验证 (需求 R156 登记写入归属, 程序由错报闭环各需求加)
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
# 重要性候选本期不适用的原因 (需求 R351): 税前亏损 (以税前利润为基准时), 其余基准金额不为正
MaterialityInapplicable = Literal["loss", "not_positive"]
# 证据文件处理状态: 已接收, 已分类 (不需抽取的类型停在这里), 已抽取, 需人工处理; staged 为智能导入的批次里上传而
# 尚未确认 (需求 R277): 类型为 other, 不被程序使用, 启动时也不补做, 确认时按确认单改为对应的类型与状态;
# not_imported 为确认时没勾选的文件 (未导入, 终态): 类型仍为 other, 不被程序与体检使用, 要导入就重新上传
DocumentStatus = Literal["received", "classified", "extracted", "needs_manual", "staged", "not_imported"]
# 需人工处理时卡在哪一步: 分类 (判断不了类型), 读取 (读不出文字), 抽取 (抽取校验不过), 关联 (对不上业务对象)
DocumentFailedStep = Literal["classify", "read", "extract", "link"]
# 文件的来源渠道: 审计师上传, 银行直接回函 (函证平台), Xero 附件, 登记 (程序向登记机构查询后存档的摘录与人员检索
# 结果, 如 GEN-08; 需求 R306)
DocumentChannel = Literal["auditor_upload", "bank_direct", "xero", "registry"]


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
    # 科目代码 (需求 R156): AI 草稿能从上下文得到时填, 审计师提交认定时可填或改; 非空时须在本项目科目表中
    account_code: str | None = None

    @field_validator("account_code")
    @classmethod
    def blank_code_is_none(cls, value: str | None) -> str | None:
        """空串与全空白归一为 None (不填, 不查科目表), 其余原样 (第 1 轮审核 R156-12)."""
        if value is None or not value.strip():
            return None
        return value


# 错报更新载荷的两类 (需求 R156): 分类 (FIN-03) 或验证 (FIN-09); 模板的 misstatement_update_kind 取同一组值 (需求 R159)
MisstatementUpdateKind = Literal["classification", "verification"]
# 错报更新载荷两类各自能带的字段 (需求 R156): 分类 (FIN-03) 定类型, 舞弊迹象, 来源与审计目标; 验证 (FIN-09) 定更正结果
CLASSIFICATION_FIELDS = ("type", "fraud_indicative", "fraud_reason", "origin", "objective_id")
VERIFICATION_FIELDS = ("status", "adjustment_entry_nos", "corrected_cents", "residual_cents", "needs_recommunication")
# 验证能把错报改成的状态: 全部更正, 部分更正, 或分录对不上而回到 communicated 重新沟通 (与已验证更正分录取的
# corrected, partially_corrected 是两个口径, 见 verified_adjustment_entries)
VERIFICATION_STATUSES = (
    MisstatementStatus.corrected,
    MisstatementStatus.partially_corrected,
    MisstatementStatus.communicated,
)


class MisstatementUpdate(Model):
    """判断携带的对一条已有错报的更新 (需求 R156): FIN-03 的分类或 FIN-09 的验证; 判断确认后按它出错报新版本
    (review_queue.apply_misstatement_update, 需求 R159).
    base_version 为判断所依据的错报版本 (写入时须仍是当前版本). 没写的可选字段表示不改. kind 为 classification
    时只能带 CLASSIFICATION_FIELDS, 为 verification 时只能带 VERIFICATION_FIELDS, status 只许 VERIFICATION_STATUSES."""

    misstatement_id: str  # 更新哪条错报
    base_version: int  # 依据的错报版本
    kind: MisstatementUpdateKind  # 分类 (FIN-03) 或验证 (FIN-09)
    type: MisstatementType | None = None  # 分类: 错报类型
    fraud_indicative: bool | None = None  # 分类: 是否有舞弊迹象 (ASA 240 第 36 段)
    fraud_reason: str | None = None  # 分类: 舞弊迹象的理由; 人给的载荷选填 (需求 R358), 大模型一路须写
    origin: MisstatementOrigin | None = None  # 分类: 本期或上期未更正结转
    objective_id: str | None = None  # 分类: 所属审计目标
    status: MisstatementStatus | None = None  # 验证: corrected, partially_corrected 或 communicated
    adjustment_entry_nos: list[str] | None = None  # 验证: 已验证的更正分录凭证号
    corrected_cents: int | None = None  # 验证: 已更正的金额
    residual_cents: int | None = None  # 验证: 剩余未更正的金额
    needs_recommunication: bool | None = None  # 验证: 分录对不上, 须重新沟通

    @model_validator(mode="after")
    def fields_of_kind(self) -> Self:
        """每类只带自己的字段; 验证的状态只许 VERIFICATION_STATUSES."""
        allowed = CLASSIFICATION_FIELDS if self.kind == "classification" else VERIFICATION_FIELDS
        other = VERIFICATION_FIELDS if self.kind == "classification" else CLASSIFICATION_FIELDS
        stray = [name for name in other if getattr(self, name) is not None]
        if stray:
            raise ValueError(f"{self.kind} 的错报更新不能带 {', '.join(stray)} (只能带 {', '.join(allowed)})")
        if self.status is not None and self.status not in VERIFICATION_STATUSES:
            raise ValueError(f"验证只能把错报改为 {', '.join(VERIFICATION_STATUSES)}, 不能为 {self.status}")
        return self


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
    # 对已有错报的更新载荷 (需求 R156): FIN-03 分类与 FIN-09 验证的判断, 以及取代它们的人工判断携带, 判断置 confirmed
    # 时按它写出错报新版本 (复核决定的事务内, 或 A8 自动确认的片段中, 需求 R159); 其余为空
    misstatement_update: MisstatementUpdate | None = None
    exception_ids: list[str] = []  # 本判断解释的例外, 判断确认后这些例外置 attributed
    autonomy_level: AutonomyLevel  # 判断时的有效自主度
    # 由模板级别降到有效自主度所命中的条件 (需求 R168, 功能 autonomy): 装配时的在前, 判断时的在后;
    # 没有降级, 功能关闭, 人工建的判断与旧数据为空
    downgrade_reasons: list[DowngradeCondition] = []
    review_status: ReviewStatus = ReviewStatus.pending  # 复核状态
    superseded_by: str | None = None  # 被推翻时为取代它的人工判断 ID; 被取代 (superseded) 时为取代它的新判断 ID
    performed_by: str  # 用户 ID: AI 生成的记项目负责人, 人工建的记决定人
    created_at: datetime  # 生成时间
    trail_ref: str | None = None  # 生成它的那次大模型调用的留痕 ID
    # 独立复核 (A7, GEN-09) 的结果, 由 A6 在判断第 1 版写入前同步调用 A7 填写; 人工建的判断不经复核
    critic_verdict: CriticVerdict = CriticVerdict.skipped  # 复核结论
    critic_trigger: CriticTrigger | None = None  # 命中的触发规则 (功能 collab 打开时); 跳过, collab 关闭与旧数据为空
    critic_reason_type: CriticReasonType | None = None  # 不一致的理由类型, 其余为空
    critic_reason: str | None = None  # 理由: 一致或不一致的依据, 跳过或没做成的原因
    # 跳过或没做成时原因的代码与技术细节 (需求 R309): 前端按代码取界面语言的文案, 细节 (校验级别与错误, 异常信息)
    # 原样显示; 一致, 不一致 (理由由复核模型写), 人工建的判断与 R309 之前的数据为空, 前端照 critic_reason 原文显示
    critic_reason_code: CriticReasonCode | None = None
    critic_reason_detail: str | None = None
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


# 项目的工作语言 (需求 R339, Issue #51): 底稿与回答用的语言. en 为英文 (缺省, 与之前的输出一致), zh 为中文
WorkingLanguage = Literal["zh", "en"]


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
    # 工作语言 (需求 R339): 建项目时选, 之后可改 (PUT working-language, 留痕); 每次执行记下当时的值
    # (ProcedureRun.language), 已生成的内容不随之改写. 之前建的项目为 en
    working_language: WorkingLanguage = "en"
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
    autonomy_level: AutonomyLevel  # 自主度 (运行时级别: 功能 autonomy 打开时为装配时降级后的)
    # 装配时由模板级别降下来所命中的条件 (需求 R168, 功能 autonomy); 没有降级, 功能关闭与旧数据为空
    downgrade_reasons: list[DowngradeCondition] = []
    status: ProcedureStatus = ProcedureStatus.planned  # 程序状态
    rerun_subject_ids: list[str] = []  # 重跑时只重判的对象 (rerun 关闭的那组失败任务的), 为空则整条重跑; 重跑后清空
    # 缺的资料对应的补交请求 ID (REQ-{表种类或文件类型}): awaiting_data 时为还缺的, no_data 时为客户无此类资料的;
    # 其余状态为空 (需求 R272)
    data_request_ids: list[str] = []


# 规则没有执行的原因 (需求 R130): skipped 为按设计对本项目不适用 (如数据集声明录入时间不可靠), not_executable 为本该
# 执行而缺数据或参数 (如画像没有审批限额), 是要让审计师知道的缺口. 本版两者处理相同: 不出例外, 不建复核任务
RuleOutcomeStatus = Literal["skipped", "not_executable"]


class RuleOutcome(Model):
    """一条没有执行的规则 (执行前检查命中, 需求 R130): 规则编号, 原因与理由. 执行了的规则不记."""

    rule_id: str  # 规则编号, 如 R03
    status: RuleOutcomeStatus  # 没有执行的原因
    reason: str  # 理由: 命中的检查 SQL 返回的第一行的 reason 列, 为空时为 "前置检查未通过"


class PopulationItem(Model):
    """被测总体中的一个被测项目 (需求 R324, ASA 230 第 9(a) 段的识别特征): SQL 返回的一行."""

    source_id: str | None  # 行 ID; 返回行没有行 ID (如汇总行) 时为空
    amount_cents: int | None  # 金额, 单位分; 返回行没有金额列时为空
    fields: dict[str, Any]  # 其余列 (识别字段, 如发票号, 客户, 入账日), JSON 形式


class RulePopulation(Model):
    """一条执行了的规则的被测总体 (需求 R324): 对象类型, 各被测项目, 项目数与金额合计. 执行前检查命中而没有执行的
    规则不记.

    emit exception 而没写总体 SQL 的规则记为未记录: count 与 amount_cents 为空, 没有项目; 记录了而一个项目都没有时
    count 与 amount_cents 为 0."""

    rule_id: str  # 规则编号, 如 R01
    # 对象类型; 未记录, 没有项目, 返回行没有 source_type 列, 各行不同或不是认识的行类型时为空 (原值留在识别字段里)
    source_type: SourceType | None = None
    items: list[PopulationItem] = []  # 各被测项目, 按 SQL 返回的顺序
    count: int | None = None  # 项目数, 未记录为空
    amount_cents: int | None = None  # 金额合计: 各项目都有金额时为其和, 否则 (含未记录) 为空


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
    # 规则引擎没有执行的规则 (需求 R130), 按 YAML 顺序; 全部执行或不经规则引擎为空. 旧记录读回时为空
    rule_outcomes: list[RuleOutcome] = []
    # 建执行记录时项目的工作语言 (需求 R339, nodes.build_run; 上传即处理的执行为 ingest.upload_run): 要求的生成语言,
    # 提示词从 R340 起按它输出, mock 由 R343 照实记 en; 页面据此标注. 之前的执行记录 (Store 与检查点里没有这个键)
    # 读回为 en: 那时的输出都是英文
    language: WorkingLanguage = "en"


class RunPopulation(Versioned):
    """规则程序一次成功执行的被测总体 (需求 R324), ID 为 POP-{run_id}. 只在 Store (不是编排状态的字段), 与执行记录
    终态同一事务写入; 失败的执行不写, 重放不重写. 判断与抽取程序的对象清单由 Store 推出 (services.populations), 不写."""

    run_id: str  # 所属执行
    procedure_id: str  # 所属程序
    rules: list[RulePopulation]  # 各执行了的规则的总体, 按 YAML 顺序


class ScheduleRow(Model):
    """P2 调度表的一行: 某一层的某个 fieldwork 实例负责哪些程序."""

    layer: int  # 依赖分层的层号, 从 0 起
    instance: str  # fieldwork 实例名 fw-{layer}-{k}
    procedure_ids: list[str]  # 该实例负责的程序


class PlanItem(Model):
    """总体审计计划中的一条程序: 该修订组装时程序包里的那一版 (旧修订不随之后的改动变)."""

    procedure_id: str  # 程序编号
    name: str  # 程序名称
    template_commit: str  # 程序库 commit
    objective_ids: list[str] = []  # 关联的审计目标; 固定集与只经依赖加入的程序为空
    params: dict[str, Any] = {}  # 参数 (按风险等级取值后的)


class ProcedureNote(Model):
    """一条程序计划的性质, 时间与范围 (ASA 300 第 9(c) 段: 认定层次的进一步审计程序)."""

    # 程序编号. 草稿 (AuditPlanDraft) 的说明可多于计划的程序 (校验只查缺与重复); 合成后的计划 (AuditPlan) 只含 items
    # 中程序的说明, 由 A4 过滤 (需求 R316)
    procedure_id: str
    nature_timing_extent: str  # 性质, 时间与范围的说明


class Milestone(Model):
    """计划时间表的一项, 如外勤完成, 报告签署, 送达股东."""

    milestone: str  # 事项
    due: date  # 预定日期


class EqrRecommendation(Model):
    """是否需要项目质量复核的建议 (ASQM 1 第 34(f) 段); 审计师在 PLN-15 批准时另行确认 (PlanConfirmation)."""

    required: bool  # 建议是否需要
    basis: str  # 依据


class PlanSection(Model):
    """计划叙述的一段. 用列表保段序: 存进 JSONB 的字典不保留键序."""

    section: str  # 段名, 如 overall_strategy, scope, significant_risks
    text: str  # 正文


class AuditPlan(Versioned):
    """总体审计计划 (需求 R203; 规格书 12.2 AuditPlan). ID 为 PLAN-{revision}. A4 在 PLN-15 中起草 (第 1 修订与
    PLN-15 驳回后的重拟); A1 写闸门效果: PLN-15 批准出 approved 新版本 (R205), PLN-19 带 materiality 的通过直接建出
    下一修订 (approved, R210). 内容修订一律出下一修订 PLAN-{n+1}, 旧修订出新版本置 superseded; 同一修订内只有状态
    变化出新版本 (draft → approved → superseded). 当前计划为 status 不是 superseded 的修订中 revision 最大的.
    计划骨架 (期间, 重要性, 特别风险目标, items) 由 A4 在 PLN-15 中确定性组装, 叙述类各项取 AuditPlanDraft.
    出新版本或新修订须经校验构造, 如 AuditPlan.model_validate({**plan.model_dump(), **update}): model_copy 不跑
    approval_fields, 不合规的版本会在之后读回时才报错."""

    revision: int = Field(ge=1)  # 第几次修订, 从 1 起
    status: PlanStatus = PlanStatus.draft  # 状态
    # 内容来自哪次执行: A4 写出的版本 (起草新修订, 把旧修订置 superseded) 记本次执行 (合并时须为本次执行,
    # merge.check_own_run); A1 写出的 (PLN-15 批准的新版本, PLN-19 修订建出的新修订与旧修订的 superseded 版)
    # 沿用所依据的那一版的 run_id, 不另起执行
    run_id: str
    period_start: date  # 审计期间起
    period_end: date  # 审计期间止
    # 适用的财务报告框架, 取约定书核对结果 TERMS 的 framework (同为 ReportingFramework); 没有 TERMS 或约定书没写明为空
    applicable_framework: ReportingFramework | None = None
    engagement_letter_document_id: str | None = None  # 约定书的文件 ID, 同取 TERMS; 没有为空
    materiality_choice: "MaterialityChoice"  # PLN-01/03 闸门选定的基准与比例; PLN-19 修订出的修订为修订后的
    materiality_cents: int  # 整体重要性, 单位分
    performance_materiality_cents: int  # 执行重要性, 单位分
    clearly_trivial_cents: int  # 明显微小门槛, 单位分
    significant_risk_objective_ids: list[str] = []  # 风险等级为 significant 的审计目标
    items: list[PlanItem]  # 计划的程序, 按程序编号排序
    narrative: list[PlanSection]  # 叙述各段, 按显示顺序: AuditPlanDraft 的 overall_strategy 在首, 其后为 sections
    procedure_notes: list[ProcedureNote]  # 各程序的性质, 时间与范围
    timetable: list[Milestone]  # 时间表
    eqr_recommendation: EqrRecommendation  # 项目质量复核的建议
    expert_assessment: str  # 是否需要审计师的专家 (ASA 620) 的评估
    standards_cited: list[str]  # 计划引用的准则片段 ID
    # 审计师在 PLN-15 批准时确认的三项 (PlanConfirmation) 与批准人, 批准时间: 草稿时都为空, 批准时都填 (模型校验);
    # 置 superseded 时沿用取代前的
    eqr_required: bool | None = None
    eqr_basis: str | None = None
    auditor_expert_not_required: bool | None = None
    # 本修订回应的决定: PLN-15 驳回 (A4 重拟), 或 PLN-19 带 materiality 的通过 (A1 建修订, 如 D-RE19); 第 1 修订为空
    addresses_decision_id: str | None = None
    revision_reason: str | None = None  # 修订的理由, 取所回应决定的 reason; 第 1 修订为空
    approved_by: str | None = None  # 批准人的用户 ID
    approved_at: datetime | None = None  # 批准时间

    @model_validator(mode="after")
    def approval_fields(self) -> Self:
        """ID 为 PLAN-{revision}; 草稿时确认三项与批准人, 批准时间都为空, 已批准时都填, 已取代的沿用取代前的, 不查;
        叙述的段名不重复."""
        if self.id != f"PLAN-{self.revision}":
            raise ValueError(f"{self.id}: 计划的 ID 须为 PLAN-{self.revision} (第 {self.revision} 修订)")
        values = (
            self.eqr_required,
            self.eqr_basis,
            self.auditor_expert_not_required,
            self.approved_by,
            self.approved_at,
        )
        if self.status is PlanStatus.draft and any(v is not None for v in values):
            raise ValueError(f"{self.id}: 草稿的批准确认与批准人须为空")
        if self.status is PlanStatus.approved and any(v is None for v in values):
            raise ValueError(f"{self.id}: 已批准的计划须有三项确认, 批准人与批准时间")
        names = [s.section for s in self.narrative]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.id}: 叙述的段名重复: {sorted({n for n in names if names.count(n) > 1})}")
        return self


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


# ACC-08 约定条款与委任核验 (功能 acceptance, 需求 R198): A2 从约定书与委任文件抽出事实 (TermsFacts), 按
# services/engagement_terms.py 的 T01-T07 核对 (R199), 再给出建议判断 (R200); 结果 EngagementTerms 为 ACC-08 闸门的材料
TERMS_ID = "TERMS"  # EngagementTerms 在每个项目里的固定 ID
TermsElement = Literal[  # 约定书须写明的五项 (ASA 210 第 10(a)-(e) 段)
    "objective_scope",  # (a) 审计的目标与范围
    "auditor_responsibilities",  # (b) 审计师的责任
    "management_responsibilities",  # (c) 管理层的责任
    "framework",  # (d) 适用的财务报告框架
    "report_form",  # (e) 报告的预期形式与内容, 及报告可能不同于预期的说明
]
TermsAcknowledgement = Literal[  # 管理层须确认的五项责任 (ASA 210 第 6(b) 段)
    "prepare_fr",  # (i) 按适用框架编制财务报告
    "internal_control",  # (ii) 管理层认为必要的内部控制
    "access_information",  # (iii)a 提供与编制财务报告有关的全部信息
    "additional_information",  # (iii)b 提供审计师要求的其他信息
    "unrestricted_access",  # (iii)c 不受限制地接触审计师认为必要的人员
]
TERMS_ELEMENTS: tuple[TermsElement, ...] = get_args(TermsElement)
TERMS_ACKNOWLEDGEMENTS: tuple[TermsAcknowledgement, ...] = get_args(TermsAcknowledgement)
TermsCheckId = Literal["T01", "T02", "T03", "T04", "T05", "T06", "T07"]  # 核对项, 口径见 services/engagement_terms.py


class TermsItem(Model):
    """约定书中的一项要素 (TermsElement) 或管理层的一项确认 (TermsAcknowledgement): 有没有写明, 写在哪."""

    item: TermsElement | TermsAcknowledgement  # 哪一项
    present: bool  # 文件是否写明
    evidence: list[EvidencePointer] = []  # 写明之处的页指针; present 为真时须有 (语义级, 需求 R199)


class ShareholderDirection(Model):
    """股东依 s293 给小型私人公司的指示 (委任文件中): 谁签, 持多少股, 何时, 是否要求审计."""

    signed_by: str | None  # 签署指示的股东
    shares_held: int | None  # 其持股数; 文件没写为空
    total_shares: int | None  # 公司已发行的股数; 文件没写为空
    votes_pct: float | None  # 其表决权比例, 百分数 (100 股中 15 股为 15.0); 不在 0 至 100 内由校验判语义失败 (R199)
    made_on: date | None  # 指示日期
    requires_audit: bool  # 指示是否要求财务报告经审计 (s293(3)(c))
    evidence: list[EvidencePointer] = []  # 上述事实的页指针


class AuditorAppointment(Model):
    """依 s325(1) 委任审计师的决议."""

    appointed_by: str | None  # 作出委任的人或机构, 如唯一董事
    resolution_on: date | None  # 决议日期
    auditor_name: str | None  # 被委任的事务所或个人
    evidence: list[EvidencePointer] = []  # 上述事实的页指针


class AuditorConsent(Model):
    """审计师依 s328A 给公司的书面同意."""

    given_on: date | None  # 同意书日期
    signed_by: str | None  # 签署人 (事务所中的注册公司审计师)
    firm_name: str | None  # 事务所名称
    registered_auditor_no: str | None  # 签署人的注册公司审计师编号; 文件没写为空
    firm_name_signed: bool  # 是否以事务所名义签署 (s328A(2)(a))
    own_name_signed: bool  # 是否以本人名义签署 (s328A(2)(b))
    evidence: list[EvidencePointer] = []  # 上述事实的页指针


class FinancialYearChange(Model):
    """董事依 s323D(2A) 缩短财年的决议."""

    resolved_on: date | None  # 决议日期
    previous_year_end: date | None  # 上一财年的结束日
    new_year_end: date | None  # 缩短后本财年的结束日
    prior_shortened_within_5_years: bool | None  # 前 5 个财年内是否已依本款缩短过; 文件没写为空
    good_faith_reason: str | None  # 决议写明的改动理由 (s323D(2A)(c)); 没写为空
    evidence: list[EvidencePointer] = []  # 上述事实的页指针


class TermsCheckStatus(StrEnum):
    """ACC-08 一项核对的结果."""

    passed = "passed"  # 通过
    failed = "failed"  # 不通过
    not_applicable = "not_applicable"  # 不适用, 如本期满 12 个月时的缩短财年核对


class TermsCheck(Model):
    """ACC-08 的一项核对 (T01-T07)."""

    check: TermsCheckId  # 核对项
    status: TermsCheckStatus  # 结果
    blocking: bool  # 不通过时是否阻止通过闸门 (哪几项阻止见 services/engagement_terms.py, 需求 R199)
    basis: str  # 依据的条文, 如 "ASA 210 para 6(b), 8(b)"
    detail: str  # 给审计师看的说明


class TermsFacts(Model):
    """约定书与委任文件中的事实: ACC-08 第一次大模型调用抽出的部分 (llm_outputs.EngagementTermsExtraction 即此),
    EngagementTerms 在此之上加核对与关联. elements 只放 TermsElement 的项, acknowledgements 只放 TermsAcknowledgement
    的项; 各项恰好出现一次由 OutputValidator 查 (语义级, 需求 R199)."""

    framework: ReportingFramework | None  # 约定书写明的财务报告框架 (特殊目的为 spfr); 没写明为空
    framework_evidence: list[EvidencePointer] = []  # 写明框架之处的页指针
    elements: list[TermsItem]  # ASA 210 第 10 段的五项要素
    acknowledgements: list[TermsItem]  # ASA 210 第 6(b) 段的五项确认
    letter_dated_on: date | None  # 约定书出具日
    letter_signed_by: str | None  # 代表公司签回约定书的人
    letter_signed_on: date | None  # 签回日期
    letter_evidence: list[EvidencePointer] = []  # 出具日, 签回人与签回日所在页的指针, locator 写字段名
    direction: ShareholderDirection | None  # 股东指示 (s293); 文件中没有为空
    appointment: AuditorAppointment | None  # 委任审计师的决议 (s325(1)); 没有为空
    consent: AuditorConsent | None  # 审计师的同意书 (s328A); 没有为空
    year_change: FinancialYearChange | None  # 缩短财年的决议 (s323D(2A)); 没有为空

    @model_validator(mode="after")
    def item_kinds(self) -> Self:
        """elements 与 acknowledgements 各只放自己那一类的项."""
        wrong = [i.item for i in self.elements if i.item not in TERMS_ELEMENTS] + [
            i.item for i in self.acknowledgements if i.item not in TERMS_ACKNOWLEDGEMENTS
        ]
        if wrong:
            raise ValueError(f"约定条款的项放错了类: {', '.join(wrong)}")
        return self


class EngagementTerms(TermsFacts, Versioned):
    """约定条款与委任核验 (ACC-08): 抽出的事实, T01-T07 的核对, 例外与 AI 建议判断, ACC-08 闸门的呈报材料. 项目里
    只有一份, ID 固定为 TERMS (TERMS_ID); 重读出新版本."""

    run_id: str  # 产出它的那次执行
    checks: list[TermsCheck]  # T01-T07 的核对结果, 按编号
    exception_ids: list[str]  # 不通过的核对生成的例外
    judgment_id: str | None  # AI 建议判断 (J-ACC-08-{run_no}-TERMS); 判断调用失败, 没有建议时为空
    document_ids: list[str]  # 读的约定文件
    addresses_decision_id: str | None = None  # 重读所回应的 insufficient_info 决定; 首次执行为空


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
    # 本期不适用的原因: 基准金额不为正时有值 (loss 依据 ASA 320 第 A5 段; not_positive 为不为正的基准乘比例得不出
    # 重要性), 候选照常呈报, 不能选为重要性基准 (需求 R351)
    not_applicable: MaterialityInapplicable | None = None


class MaterialityCandidates(Versioned):
    """PLN-01 算出的全部重要性候选, PLN-01/03 闸门的呈报材料."""

    candidates: list[MaterialityCandidate]  # 各基准的候选区间
    performance_pct_range: tuple[float, float] = (0.5, 0.75)  # 执行重要性占整体重要性的比例范围
    clearly_trivial_pct: float = 0.05  # 明显微小门槛占整体重要性的默认比例


class UncorrectedItem(Model):
    """未更正错报汇总表中的一行."""

    misstatement_id: str  # 对应的错报
    amount_cents: int  # 累计的金额, 单位分: uncorrected 取错报金额, partially_corrected 取剩余额 (需求 R166)
    direction: Direction  # 高估或低估
    origin: MisstatementOrigin = "current_period"  # 本期或上期未更正结转, 取错报的 origin; 没分类的 (旧口径) 为本期


class UncorrectedSchedule(Versioned):
    """未更正错报汇总表: FIN-04 产出, FIN-01 闸门的呈报材料. 规格书中叫 UncorrectedMisstatementSchedule."""

    items: list[UncorrectedItem]  # 高于明显微小门槛的未更正 (含部分更正的剩余额) 错报
    excluded_trivial_ids: list[str]  # 低于门槛, 不累计的错报
    corrected_ids: list[str] = []  # 已全额更正 (corrected) 的错报, 不累计 (需求 R166); 旧汇总表没有, 读出为空
    total_cents: int  # items 金额合计, 不按方向抵销
    materiality_cents: int  # 整体重要性, 单位分
    candidate_opinion: OpinionType  # 合计不低于整体重要性为保留, 否则无保留


class ObjectRef(Model):
    """对某个对象某一版本的引用 (需求 R215): 声明书附表对应的汇总表, 检查所针对的对象, 独立性声明所依据的检查."""

    kind: str  # 对象的类名, 如 UncorrectedSchedule, ComplianceCheck, WrittenRepresentationLetter
    id: str  # 对象 ID, 如 SCHEDULE, CHECK-FIN-10, REPLETTER
    version: int = Field(ge=1)  # 引用时的版本


class CheckItem(Model):
    """合规检查的一项."""

    code: str  # 检查项代号, 如 W01, E03; 逐行核对的检查每行一项, 同一代号有多项
    description: str  # 检查什么
    status: CheckItemStatus  # 结果
    standard_ref: str  # 依据的准则或法条, 如 ASA 580.14, s307C(3)
    detail: dict[str, Any] = {}  # 明细, 如勾稽的两边金额 (分), 不一致的行代码
    requires_disposition: bool = False  # 是否须审计师处置后才能签字
    evidence: list[EvidencePointer] = []  # 证据指针


class ComplianceCheck(Versioned):
    """签字前提的合规检查: FIN-10 职业道德与独立性, FIN-18 声明书校验共用 (A9 写,
    需求 R215). 每条程序一份, ID 为 CHECK-{procedure_id}; 再执行出新版本."""

    procedure_id: str  # 做检查的程序
    run_id: str  # 做检查的那次执行
    items: list[CheckItem]  # 各项结果
    exception_ids: list[str] = []  # 不通过的项生成的例外
    must_disclaim: bool = False  # 检查结果要求出具无法表示意见, 如管理层不提供第 10, 11 段的声明 (ASA 580 第 20 段)
    subject_ref: ObjectRef | None = None  # 被检查的对象, 如声明书 REPLETTER 的版本; 没有单一对象时为空
    # 所依据的输入及其版本, 如 {client_profile_version: 3}; 节点再入时与当前输入比对, 判断检查是否过期. 只放 JSON
    # 原生值 (整数, 字符串, 布尔, 由它们组成的列表与字典): 日期, 元组等经 Store 读回会变成字符串与列表, 与检查点读回的
    # 不相等
    basis: dict[str, Any] = {}

    @property
    def failed_codes(self) -> list[str]:
        """不通过 (failed) 的项的代号, 按 items 中首次出现的顺序, 同一代号只列一次."""
        return list(dict.fromkeys(item.code for item in self.items if item.status is CheckItemStatus.failed))


# ── 2.5 复核与决定 ─────────────────────────────────────────


class ManualRequest(Model):
    """manual 任务交给人的请求 (需求 R92): 派发时抄自程序模板的 manual 段, 加上派发时生成的材料. 提交 manual_result
    时按它核对证据数与结构化结果 (decisions.check_manual_result)."""

    instructions: str  # 给执行人的说明
    result_model: str | None  # 结构化结果的模型名 (contracts/manual_results 登记), 为空时 details 须为空
    min_evidence: int = Field(ge=0)  # 至少要附的证据文件指针数, 不小于 0
    material: dict[str, Any]  # 派发时生成的材料 (需求 R93 由程序 owner 的 prepare 生成), 原样给人看


class ReviewTask(Versioned):
    """复核任务: 需要人处理的一件事, 进复核队列."""

    kind: TaskKind  # 任务类型
    procedure_id: str  # 所属程序
    target_type: TargetType  # 要处理的对象类型
    target_id: str  # 要处理的对象 ID
    target_version: int  # 建任务时对象的版本, 提交决定时校验
    run_id: str  # 发现问题的那次执行
    subject_id: str | None = (
        None  # failed, unattributed 任务的判断对象, manual 任务为模板 manual.subject; 整次执行失败时为空
    )
    status: TaskStatus = TaskStatus.pending  # 待决或已决定
    created_at: datetime  # 创建时间
    manual_request: ManualRequest | None = None  # manual 任务的请求, 其余类型为空

    @model_validator(mode="after")
    def request_matches_kind(self) -> Self:
        """manual 任务须带请求, 其余类型不能带 (需求 R92)."""
        if (self.kind is TaskKind.manual) != (self.manual_request is not None):
            raise ValueError(f"{self.id}: manual 任务须带 manual_request, 其余类型不能带 ({self.kind})")
        return self


class ManualResult(Model):
    """人工直接给出的结果, 用于 manual_result 决定 (failed 任务的人工执行, manual 任务的人工程序结果)."""

    conclusion: str  # 人给出的结论
    evidence: list[EvidencePointer] = []  # 人给出的证据
    performed_on: date | None = None  # 人执行的日期, manual 任务必填 (不晚于决定当天的悉尼日期)
    details: dict[str, Any] = {}  # 按任务请求的结果模型给的结构化结果, 原样存下; 没有结果模型时为空


class ReviewRecord(Versioned):
    """审计师对一个复核任务作出的决定及理由."""

    task_id: str  # 处理的任务
    target_version: int  # 从任务复制, 提交时校验
    decision: Decision  # 决定
    reason: str | None = None  # 理由, 选填 (需求 R358)
    override_conclusion: str | None = None  # override 时的新结论
    misstatement_draft: MisstatementDraft | None = None  # misstatement 时的错报草稿
    # 人给的错报更新载荷 (需求 R159): 模板 misstatement_update_kind 非空时, 以人工判断取代 AI 判断的决定 (override,
    # manual_result, 对例外认定错报) 必填, 其余决定不能填 (decisions.review_fields)
    misstatement_update: MisstatementUpdate | None = None
    rerun_params: dict[str, Any] | None = None  # rerun 时要改的参数
    manual_result: ManualResult | None = None  # manual_result 时人给的结果
    dispute_resolution: DisputeResolution | None = None  # dispute_resolution 时的仲裁结果
    neither_action: NeitherAction | None = (
        None  # 仲裁为 neither 时必填, 本版只有 human_conclusion (附 override_conclusion)
    )
    consulted: bool = False  # 仲裁时是否在系统外咨询过 (ASQM 1); 已咨询本身不是处置, 裁决仍是三值之一 (需求 R150)
    consultation_record: str | None = None  # 咨询对象与结论; consulted 为真时必填 (功能 collab 打开时校验)
    # 复核停留时长 (需求 R153, 功能 collab 打开时): 服务端在校验通过后, 写入前按复核人对该任务最近一次呈递算
    # (services/presentation.dwell_seconds), 不收请求体的; 没有呈递或 collab 关闭时两项都为空. presentation_id 为起算的
    # 那行 presentation 留痕, 与秒数一起进 decision 留痕; 恢复补写留痕时取这两项, 不重算
    dwell_seconds: int | None = None
    presentation_id: str | None = None
    reviewed_by: str  # 决定人的用户 ID
    decided_at: datetime  # 决定时间


class GateEdit(Model):
    """PLN-01/03 闸门中对一个风险项等级的修改."""

    target_id: str  # 被改的风险项 ID
    target_version: int  # 被改时的版本, 提交时校验
    field: Literal["level"]  # 只能改等级
    new_value: RiskLevel  # 新等级
    reason: str = ""  # 修改理由, 选填 (需求 R358)


class MaterialityChoice(Model):
    """审计师在 PLN-01/03 闸门选定的重要性基准与比例."""

    basis: MaterialityBasis  # 选定的基准
    pct: float  # 整体重要性占基准的比例, 须在候选区间内
    performance_pct: float  # 执行重要性占整体重要性的比例
    clearly_trivial_pct: float  # 明显微小门槛占整体重要性的比例


AuditPlan.model_rebuild()  # AuditPlan (第 2.3 节) 引用了本节的 MaterialityChoice


class PlanConfirmation(Model):
    """审计师在 PLN-15 批准时的确认 (需求 R203), 写进所批准的计划: 是否需要项目质量复核及依据 (ASQM 1 第 34(f) 段),
    以及确认不需要审计师的专家 (ASA 620)."""

    eqr_required: bool  # 是否需要项目质量复核
    eqr_basis: str  # 依据
    auditor_expert_not_required: bool  # 是否确认不需要审计师的专家


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
    plan_confirmation: PlanConfirmation | None = None  # PLN-15 通过时必填 (需求 R203; 只有它的登记行允许, 需求 R205)
    reason: str | None = None  # 理由, 选填 (需求 R358)
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


class CorrectionNotice(Versioned):
    """更正通知 (需求 R162): 停在等待更正 (await_correction) 时审计师告诉系统管理层的更正结果, 是 DecisionSubmit 收的
    第三种决定 (与闸门决定, 复核决定同一张 decisions 表, 共用 ID 空间, 第 0 节). adjustments_posted: 按凭证号载入
    更正分录 (为空时载入全部未载入的), 所列错报转 verifying; stop_waiting: 所列错报记未更正 (stopped_waiting), 须写
    理由, 不带凭证号."""

    kind: CorrectionNoticeKind  # 通知的种类
    misstatement_ids: list[str]  # 所列的错报, 须在等待更正的错报中
    adjustment_entry_nos: list[str] = []  # 更正分录的凭证号 (只用于 adjustments_posted)
    reason: str | None = None  # 理由, 选填 (需求 R358)
    decided_by: str  # 提交通知的审计师 (用户 ID); 更正分录的写入人
    decided_at: datetime  # 提交时间


class Misstatement(Versioned):
    """已认定的错报, 来自某条判断或例外; 规格书 12.2 Misstatement 与 12.3 的生命周期 (需求 R156, 迁移表见接口文档
    第 2.5 节). 人认定时建第 1 版, 功能 misstatement 打开为 identified, 关闭为 uncorrected (旧口径, 不经沟通, basis
    为空); 之后的分类, 沟通与验证各出新版本 (model_copy, version +1). 新字段都有缺省, Store 中的旧行照常读回."""

    judgment_id: str | None  # 来源判断, 与 exception_id 二选一
    exception_id: str | None  # 来源例外, 与 judgment_id 二选一
    review_record_id: str  # 认定它的复核决定
    type: MisstatementType  # 错报类型
    account: str  # 科目
    assertion: Assertion  # 认定
    amount_cents: int  # 金额, 单位分
    direction: Direction  # 高估或低估
    account_code: str | None = None  # 科目代码, 由认定时的草稿带入
    status: MisstatementStatus = MisstatementStatus.identified  # 生命周期状态
    # 分类 (A8, FIN-03): 所属审计目标, 舞弊迹象与理由, 本期或上期未更正结转, 作分类的判断
    objective_id: str | None = None
    fraud_indicative: bool | None = None
    fraud_reason: str | None = None
    origin: MisstatementOrigin | None = None
    classification_judgment_id: str | None = None
    # 沟通 (A8, FIN-08): 沟通时间与对象, 管理层回复的时间与原文; 验证发现分录对不上时置 needs_recommunication
    communicated_at: datetime | None = None
    communicated_to: str | None = None
    responded_at: datetime | None = None
    management_response: str | None = None
    needs_recommunication: bool = False
    uncorrected_basis: UncorrectedBasis | None = None  # status 为 uncorrected 时的依据; 旧口径 (未经沟通) 为空
    # 更正与验证 (A1 等待更正, A8 验证, FIN-09): 更正通知, 已验证的更正分录凭证号, 已更正与剩余金额, 作验证的判断
    correction_notice_id: str | None = None
    adjustment_entry_nos: list[str] = []
    corrected_cents: int = 0
    residual_cents: int | None = None
    verified_by_judgment_id: str | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        """状态与字段一致 (接口文档第 2.5 节): communicated 须有沟通时间; verifying 须有更正通知; corrected 与
        partially_corrected 须有验证判断, 且已更正额加剩余额等于错报金额, corrected 剩余额为 0, partially_corrected
        剩余额在 (0, amount_cents) 内; 未更正的依据只随 uncorrected, 因拒绝而 uncorrected 须有沟通时间与管理层回复,
        停止等待须有更正通知; 重新沟通的标记只在 communicated 时为真. uncorrected 而 basis 为空合法 (功能关闭时的旧
        口径). 依据随 evaluated, closed 的放开留给它们落地的需求."""
        problems = []
        status, residual = self.status, self.residual_cents
        if status is MisstatementStatus.communicated and self.communicated_at is None:
            problems.append("communicated 须有沟通时间 (communicated_at)")
        if status is MisstatementStatus.verifying and self.correction_notice_id is None:
            problems.append("verifying 须有更正通知 (correction_notice_id)")
        if status in (MisstatementStatus.corrected, MisstatementStatus.partially_corrected):
            if self.verified_by_judgment_id is None:
                problems.append(f"{status} 须有验证判断 (verified_by_judgment_id)")
            if residual is None or self.corrected_cents + residual != self.amount_cents:
                amounts = f"{self.corrected_cents} + {residual}, 金额 {self.amount_cents}"
                problems.append(f"{status} 的已更正额加剩余额须等于错报金额: {amounts}")
        if status is MisstatementStatus.corrected and residual != 0:
            problems.append(f"corrected 须剩余额为 0, 现为 {residual}")
        if status is MisstatementStatus.partially_corrected and not (
            residual is not None and 0 < residual < self.amount_cents
        ):
            problems.append(f"partially_corrected 的剩余额 {residual} 须在 (0, {self.amount_cents}) 内")
        basis = self.uncorrected_basis
        if basis is not None and status is not MisstatementStatus.uncorrected:
            problems.append(f"未更正的依据 (uncorrected_basis) 只随 uncorrected, 现为 {status}")
        if (
            status is MisstatementStatus.uncorrected
            and basis == "refused"
            and (self.communicated_at is None or self.management_response is None)
        ):
            problems.append("因管理层拒绝而未更正, 须有沟通时间 (communicated_at) 与管理层回复 (management_response)")
        if basis == "stopped_waiting" and self.correction_notice_id is None:
            problems.append("停止等待更正须有更正通知 (correction_notice_id)")
        if self.needs_recommunication and status is not MisstatementStatus.communicated:
            problems.append(f"needs_recommunication 只在 communicated 时为真, 现为 {status}")
        if problems:
            raise ValueError(f"错报 {self.id} 的状态与字段不一致: {'; '.join(problems)}")
        return self


def verified_adjustment_entries(misstatements: Iterable[Misstatement]) -> list[str]:
    """已验证更正分录 (需求 R156, 接口文档第 2.5 节; 计划段 AP-51 的取数口径): status 为 corrected 或
    partially_corrected 且 verified_by_judgment_id 非空的错报的 adjustment_entry_nos 并集, 按凭证号排序. 纯函数."""
    verified = (MisstatementStatus.corrected, MisstatementStatus.partially_corrected)
    return sorted(
        {
            no
            for m in misstatements
            if m.status in verified and m.verified_by_judgment_id is not None
            for no in m.adjustment_entry_nos
        }
    )


class ManagementResponse(Model):
    """管理层对一条错报的回复 (FIN-08 的人工结果载荷, 需求 R156): 沟通的时间与对象, 回复的时间与原文, 是否同意更正.
    base_version 为沟通时的错报版本. 回复不能早于沟通."""

    misstatement_id: str  # 回复的错报
    base_version: int  # 沟通时的错报版本
    communicated_at: AwareDatetime  # 沟通时间 (须带时区: 与决定时刻比较, 第 1 轮审核 R161-01)
    communicated_to: NonBlank  # 沟通对象 (管理层或治理层中的人与职务)
    responded_at: AwareDatetime  # 回复时间 (须带时区)
    response: NonBlank  # 回复原文
    agrees_to_correct: bool  # 是否同意更正

    @model_validator(mode="after")
    def responded_after_communication(self) -> Self:
        if self.responded_at < self.communicated_at:
            raise ValueError(f"回复时间 {self.responded_at} 早于沟通时间 {self.communicated_at}")
        return self


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


OWNERSHIP_GRAPH_ID = "OWNERSHIP"  # 股权与任职关系图在每个项目里的固定 ID (需求 R136)
PartyRelation = Literal["director", "secretary", "shareholder"]  # 关系图中人或公司对一家公司的关系


class PartyNode(Model):
    """股权与任职关系图中的一个人或一家公司. ID 规则见接口文档第 0 节 (company_node_id, person_node_id)."""

    id: str  # 公司 ACN-{acn}, 没有 ACN 的 ORG-{名称}; 个人 PER-{姓}-{名}-{yyyymmdd}, 没有出生日期时 PER-{姓}-{名}-NODOB
    kind: PartyKind  # 个人或公司
    name: str  # 名称或姓名, 按登记摘录原样
    acn: str | None = None  # ACN, 9 位数字; 个人为空
    abn: str | None = None  # ABN, 11 位数字; 只有报告主体取画像的 ABN, 其余为空
    birth_date: date | None = None  # 出生日期 (个人, 摘录高管栏有); 只持股不任职的没有
    # 是否取得了人员检索结果 (个人) 或公司摘录 (公司); 不检索的人员, 不穿透的公司, 查了而查不到的都为假 (需求 R137)
    searched: bool = False

    @model_validator(mode="after")
    def numbers(self) -> Self:
        """ACN, ABN 只查位数, 同 DeclaredParty."""
        problem = digits_problem(self.acn, 9, "acn") or digits_problem(self.abn, 11, "abn")
        if problem:
            raise ValueError(problem)
        return self


class PartyLink(Model):
    """关系图中的一条关系: from_id 对 to_id (公司) 的任职或持股, 证据为登记摘录中对应的那一行."""

    from_id: str  # 任职或持股的人或公司 (PartyNode.id)
    to_id: str  # 被任职或被持股的公司 (PartyNode.id)
    relation: PartyRelation  # 董事, 秘书或股东
    shares: int | None = Field(default=None, ge=0)  # 持股数 (股东); 任职为空
    pct: Share | None = None  # 持股比例, 0 至 1 (股东); 任职为空
    appointed: date | None = None  # 任职起始日 (高管)
    ceased: date | None = None  # 离任日 (高管); 在任为空
    evidence: EvidencePointer  # 登记摘录中这一行的 document 指针


class OwnershipGraph(Versioned):
    """股权与任职关系图 (A5 GEN-08 建, 需求 R136): 报告主体向上穿透的法人股东, 控制或关键人员, 以及他们任职或持股的
    其他公司. 项目里只有一份, ID 固定为 OWNERSHIP; 重建出新版本."""

    run_id: str  # 建它的那次执行; 合并时须为本次执行 (merge.check_own_run)
    reporting_entity_id: str  # 报告主体的节点 ID (ACN-{acn}), 须在 nodes 中
    nodes: list[PartyNode]  # 节点, ID 不重复
    links: list[PartyLink]  # 关系, 端点都在 nodes 中

    @model_validator(mode="after")
    def closed(self) -> Self:
        """节点 ID 不重复; 报告主体与每条关系的两端都在 nodes 中."""
        ids = [node.id for node in self.nodes]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"节点 ID 重复: {', '.join(duplicated)}")
        ends = {self.reporting_entity_id, *(end for link in self.links for end in (link.from_id, link.to_id))}
        missing = sorted(ends - set(ids))
        if missing:
            raise ValueError(f"不在 nodes 中的节点: {', '.join(missing)}")
        return self


def node_name_part(text: str) -> str:
    """节点 ID 中的姓或名: 先做 Unicode 分解 (NFKD, 带附加符的字母拆成字母与附加符), 转大写, 再去掉 A-Z 与 0-9
    以外的字符 (附加符, 空格, 撇号, 连字符, 非拉丁字母等). 例 "O'Connor" → OCONNOR, "José" → JOSE."""
    return re.sub(r"[^A-Z0-9]", "", unicodedata.normalize("NFKD", text).upper())


# 带出生日期的个人节点 ID (person_node_id 的写法, 名可为空): 前面不接字母数字 (下划线, 冒号, 汉字等可以), 8 位日期后
# 不再接数字 (后面接字母也照样遮, 宁可多遮); 末段为 NODOB 的不匹配. 前面是 JSON 转义 (\n, \t, \uXXXX 等, 末字符是字母
# 数字) 时也算边界, 已序列化的文本误交进来时不漏 (正常用法是在序列化之前遮, 见 mask_person_node_ids)
PERSON_ID_WITH_BIRTH = re.compile(
    r"(?:(?<![A-Za-z0-9])|(?<=\\[bfnrt])|(?<=\\u[0-9A-Fa-f]{4}))(PER-[A-Z0-9]+-[A-Z0-9]*)-[0-9]{8}(?![0-9])"
)


def mask_person_text(text: str) -> str:
    """一段原文里的个人节点 ID 去掉出生日期一段: PER-MORGAN-ALEX-19790412 → PER-MORGAN-ALEX; 末段为 NODOB 的与其余
    文本不变. 给错误文本这类纯字符串用; 结构化数据用 mask_person_node_ids."""
    return PERSON_ID_WITH_BIRTH.sub(r"\1", text)


def mask_person_node_ids(value: Any) -> Any:
    """发给模型的数据在序列化之前遮蔽个人节点 ID 的出生日期 (接口文档第 0 节 "个人信息"): 递归处理字典 (键与值),
    列表与元组, 每个字符串经 mask_person_text, 其余原样; 返回新对象, 不改入参. 审计助理的工具结果 (需求 R305) 与
    GEN-03, GEN-01 进提示词前 (R139, R141, 先遮蔽数据再渲染) 共用这一个. 同名而出生日期不同的两人遮蔽后相同 (已知
    局限; 两人的 ID 作字典的键时遮蔽后撞键, 后一项覆盖前一项, 所以发给模型的数据不以人员节点 ID 作键)."""
    if isinstance(value, str):
        return mask_person_text(value)
    if isinstance(value, dict):
        return {mask_person_node_ids(k): mask_person_node_ids(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_person_node_ids(item) for item in value]
    if isinstance(value, tuple):
        return tuple(mask_person_node_ids(item) for item in value)
    return value


def drop_fields(value: Any, fields: Collection[str]) -> Any:
    """递归去掉字典中名为 fields 的键 (列表与元组逐项处理), 其余原样; 返回新对象, 不改入参. 发给模型的数据在序列化
    之前去掉出生日期字段 (birth_date, 接口文档第 0 节 "个人信息", 需求 R310) 等用, 与 mask_person_node_ids 配合."""
    if isinstance(value, dict):
        return {k: drop_fields(v, fields) for k, v in value.items() if k not in fields}
    if isinstance(value, list):
        return [drop_fields(item, fields) for item in value]
    if isinstance(value, tuple):
        return tuple(drop_fields(item, fields) for item in value)
    return value


def company_node_id(acn: str) -> str:
    """公司节点的 ID: ACN-{acn}. ACN 先去掉空白 ("900 043 021" 同 "900043021"), 之后不是 9 位数字时抛 ValueError."""
    digits = "".join(acn.split())
    problem = digits_problem(digits, 9, "acn")
    if problem:
        raise ValueError(problem)
    return f"ACN-{digits}"


def organisation_node_id(name: str) -> str:
    """没有 ACN 的公司 (外国法人股东等, 需求 R137) 的节点 ID: ORG-{名称}, 名称经 node_name_part (例 "Galah Holdings
    (NZ) Limited" → ORG-GALAHHOLDINGSNZLIMITED). 规范化后为空 (如只有非拉丁字母) 时抛 ValueError."""
    part = node_name_part(name)
    if not part:
        raise ValueError(f"名称规范化后为空, 建不出节点 ID: {name!r}")
    return f"ORG-{part}"


def person_node_id(family_name: str, given_names: str, birth_date: date | None) -> str:
    """个人节点的 ID: PER-{姓}-{名}-{yyyymmdd}; 没有出生日期 (只持股不任职, 摘录股东栏没有) 时末段为 NODOB.
    姓与名各自经 node_name_part; 姓规范化后为空 (如只有非拉丁字母) 时抛 ValueError, 不同的人不并成一个 ID.
    名可以为空 (单名). 例: PER-MORGAN-ALEX-19790412, PER-WHITFIELD-SAM-NODOB."""
    family = node_name_part(family_name)
    if not family:
        raise ValueError(f"姓规范化后为空, 建不出节点 ID: {family_name!r}")
    born = birth_date.strftime("%Y%m%d") if birth_date is not None else "NODOB"
    return f"PER-{family}-{node_name_part(given_names)}-{born}"


def split_full_name(full_name: str) -> tuple[str, str]:
    """摘录中只有全名时拆成 (姓, 名). 含逗号时按 "姓, 名" 在第一个逗号处拆 ("MORGAN, Alex" → MORGAN, Alex); 否则
    姓取最后一个词, 名取其余词 (只有一个词时为空). 各段多余的空白合成一个空格."""
    if "," in full_name:
        family, given = full_name.split(",", 1)
        return " ".join(family.split()), " ".join(given.split())
    words = full_name.split()
    return (words[-1], " ".join(words[:-1])) if words else ("", "")


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
    uploaded_by: str | None = None  # 上传人的用户 ID; 系统存档 (如数据集载入, 函证回函, GEN-08 的登记存档) 为空
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
    """引用的一条判断 (ID 与引用时的版本): 审计报告, 持续经营结论建议."""

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
    # 起草它的那次 RPT-03 执行 (需求 R339, 写法同 AuditPlan.run_id): RPT-03 起草的版本记本次执行 (合并时须为本次执行,
    # merge.check_own_run), 签字等后续版本沿用; 页面按它取执行记录的 language 标生成语言. 之前的版本读回为空, 不标
    run_id: str | None = None


class RepresentationClause(Model):
    """声明书中的一项声明."""

    code: str  # 声明的代号, 如 FR_PREPARATION
    kind: Literal["core", "specific"]  # core: ASA 580 第 10, 11 段要求的; specific: 其他准则或审计师另要的 (第 13 段)
    standard_ref: str  # 要求这项声明的准则段落, 如 ASA 580.10, ASA 450.14
    status: ClauseStatus  # 写了, 不提供而审计师接受, 或没写
    acceptance_reason: str | None = None  # refused_accepted 时审计师接受的理由, 选填 (需求 R358)
    opinion_effect: str | None = None  # refused_accepted 时必填: 对审计意见的影响

    @model_validator(mode="after")
    def accepted_refusal(self) -> Self:
        """refused_accepted 须写明对意见的影响 (接受的理由选填, 需求 R358)."""
        if self.status is ClauseStatus.refused_accepted and not self.opinion_effect:
            raise ValueError(f"{self.code}: refused_accepted 须填 opinion_effect")
        return self


class WrittenRepresentationLetter(Versioned):
    """管理层书面声明书 (ASA 580; A5 FIN-07 收取, FIN-18 校验, 需求 R215). 项目里只有一份, ID 固定为 REPLETTER;
    状态或内容变化出新版本."""

    status: RepresentationStatus  # 已请求, 已取得, 拒绝提供, 不可靠
    representations: list[RepresentationClause]  # 各项声明
    attached_schedule: list[UncorrectedItem] = []  # 声明书所附的未更正错报汇总 (ASA 450 第 14 段), 没有未更正错报为空
    schedule_ref: ObjectRef | None = None  # 附表对应的未更正错报汇总表 (SCHEDULE) 版本
    signed_by: list[str] = []  # 签署人姓名, 按声明书原样 (ASA 580 第 9 段: 对财报负责且了解情况的管理层)
    dated_at: date | None = None  # 声明书日期, 须尽量接近而不晚于报告日 (第 14 段); 未取得为空
    period_end: date  # 声明涵盖的期间截至日, 须涵盖报告所涉的全部期间 (第 14 段)
    document_id: str | None = None  # 声明书原件的文件 ID (DocumentType.representation_letter)
    inconsistencies: list[str] = []  # 与其他审计证据不一致之处 (第 17 段)
    run_id: str  # 写出这一版的那次执行


class CommunicationMatter(Model):
    """与治理层沟通的一个事项."""

    code: str  # 事项代号, 如 SIG_PRACTICES, DIFFICULTIES, UNCORRECTED
    standard_ref: str  # 要求沟通的准则段落, 如 ASA 260.16(a)
    required: bool  # 准则是否要求沟通
    exempt_reason: str | None = None  # 要求沟通而不适用时的理由, 如没有未更正错报
    covered: bool  # 这次沟通是否已涵盖
    text: str  # 沟通的内容


class GovernanceCommunication(Versioned):
    """与治理层的一次沟通 (ASA 260; A10 RPT-07 收取, 需求 R215). ID 为 GC-{procedure_id}-{run_no}, 如 GC-RPT-07-1;
    本版只有完成阶段的沟通."""

    kind: Literal["completion"] = "completion"  # 沟通的阶段, 本版只有完成阶段
    form: CommunicationForm  # 书面或口头
    communicated_at: date  # 沟通日期
    recipients: list[str]  # 沟通对象, 如董事姓名
    matters: list[CommunicationMatter]  # 各事项
    corrected_misstatement_ids: list[str] = []  # 沟通到的已更正错报
    uncorrected_misstatement_ids: list[str] = []  # 沟通到的未更正错报
    letter_text: str | None = None  # 书面沟通的副本全文 (ASA 260 第 24 段); 口头沟通为空
    document_id: str | None = None  # 沟通函原件的文件 ID (DocumentType.governance_communication)
    response: str | None = None  # 治理层的回复, 没有为空
    run_id: str  # 写出它的那次执行


class Contravention(Model):
    """s307C 独立性声明中列明的一项违规."""

    item_code: str  # 对应的检查项代号 (CHECK-FIN-10 的 CheckItem.code)
    text: str  # 违规的说明


class IndependenceDeclaration(Versioned):
    """审计师独立性声明 (公司法 s307C; A10 RPT-08 起草, RPT-01 签字时由 A1 置 signed, 需求 R215). 项目里只有一份,
    ID 固定为 S307C."""

    form: DeclarationForm  # 无违规, 或列明违规
    contraventions: list[Contravention] = []  # 列明的违规; form 为 contraventions 时非空, 否则为空
    based_on: ObjectRef  # 所依据的独立性检查 (CHECK-FIN-10) 的版本
    text: str  # 声明全文
    declarant_id: str  # 作出声明的审计师 (项目负责人) 的用户 ID
    directors_report_copy_required: bool  # 是否须附入董事报告 (s298(1AA)(c))
    status: ReportStatus = ReportStatus.draft  # 草稿或已签字, 取值同报告版本; 随报告签字 (s307C(5)(b))
    declaration_date: date | None = None  # 声明日期, 签字当天; 草稿为空
    report_id: str | None = None  # 随之交给董事的报告 (s307C(5)(a)(i)), 如 REPORT-1; 草稿为空
    signed_by: str | None = None  # 签字人的用户 ID; 草稿为空
    run_id: str  # 起草它的那次执行

    @model_validator(mode="after")
    def form_matches_list(self) -> Self:
        """form 与列明的违规一致: contraventions 时须列明, no_contraventions 时不能有."""
        listed = bool(self.contraventions)
        if self.form is DeclarationForm.contraventions and not listed:
            raise ValueError("form 为 contraventions 时须列明违规")
        if self.form is DeclarationForm.no_contraventions and listed:
            raise ValueError("form 为 no_contraventions 时不能列明违规")
        return self


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
