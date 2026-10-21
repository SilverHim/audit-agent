"""LLM 结构化输出模型, 定义见 开发接口文档.md 第 5 节."""

from datetime import date
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from backend.contracts.data import Confidence, TablePlan
from backend.contracts.models import (
    CriticReasonType,
    DocumentType,
    EqrRecommendation,
    EvidencePointer,
    GoingConcernOutcome,
    JudgmentRef,
    Milestone,
    MisstatementDraft,
    MisstatementType,
    Model,
    Officeholder,
    ProcedureNote,
    ProposedAction,
    ReasoningStep,
    ReceiptChannel,
    Shareholding,
    TermsFacts,
    TermType,
)


class JudgmentDraft(Model):
    """LLM 输出的判断草稿, 用于 REV-03, REV-02, CSH-02; 校验后由 JudgmentEngine 补全为 Judgment."""

    conclusion: str  # 结论
    reasoning_chain: list[ReasoningStep]  # 推理链
    evidence: list[EvidencePointer]  # 引用的证据
    counter_argument: str  # 反方论证
    proposed_action: ProposedAction  # 建议动作
    misstatement_draft: MisstatementDraft | None = None  # 建议认定错报时必填
    self_confidence: float = Field(ge=0, le=1)  # 模型自报的置信度 0-1, 计入置信度的 llm 分项


class MisstatementNature(Model):
    """错报的性质 (FIN-03): 类型 (ASA 450 第 A6 段) 与舞弊迹象 (ASA 240 第 36 段). 本系统不做审计抽样, 类型只取
    factual 或 judgmental, 为 projected 判语义失败 (judgment_engine.nature_semantics); 舞弊迹象为真须写理由."""

    type: MisstatementType  # 错报类型: factual 无疑问的, judgmental 管理层判断 (含会计估计) 不合理的
    fraud_indicative: bool  # 是否有舞弊迹象
    fraud_reason: str | None = None  # 舞弊迹象的理由; fraud_indicative 为真时必填


class MisstatementClassificationDraft(JudgmentDraft):
    """FIN-03 对一条错报的分类草稿 (需求 R157): 判断草稿的各项 (结论, 推理链, 证据, 建议动作等, 规则同 JudgmentDraft)
    加错报的性质. 校验后由判断引擎补全为 Judgment, 性质由 A8 写进判断的 misstatement_update (需求 R158)."""

    nature: MisstatementNature  # 错报的性质


class CorrectionVerificationDraft(JudgmentDraft):
    """FIN-09 对一条错报的更正验证草稿 (需求 R164): 判断草稿的各项 (规则同 JudgmentDraft) 加是否完整更正与更正分录
    影响的其他科目. 语义校验 (judgment_engine.verification_semantics) 按规则算出的剩余额与匹配凭证的科目核对; 更新
    载荷由 A8 写进判断 (需求 R165)."""

    fully_corrected: bool  # 更正分录是否完整更正了该错报; 须与规则算出的剩余额为 0 一致
    other_accounts: list[str]  # 更正分录涉及的错报科目以外的科目代码, 不重复, 没有为空; 须在匹配凭证的科目内


class ExtractedTerm(Model):
    """从合同中抽出的一个条款."""

    term_type: TermType  # 条款类型
    text: str  # 条款全文
    page: int  # 所在页码


class ContractExtraction(Model):
    """DOC-04 从合同 PDF 抽取的要素."""

    counterparty: str  # 合同对方
    counterparty_abn: str | None  # 对方 ABN, 11 位数字
    amount_cents: int | None  # 合同金额, 单位分
    sign_date: date | None  # 签订日
    evidence: list[EvidencePointer]  # 上述字段的页码指针
    terms: list[ExtractedTerm]  # 抽出的条款


class EngagementTermsExtraction(TermsFacts):
    """ACC-08 第一次调用从约定书与委任文件 (多份) 抽取的事实, 字段即 models.TermsFacts (EngagementTerms 的抽取部分,
    子模型定义在 models.py, 两边共用). 指针指向各份文件的页; 引用与语义校验见 OutputValidator (需求 R199). 第二次
    调用的建议判断用 JudgmentDraft."""


class ConfirmationExtraction(Model):
    """DOC-06 从银行回函 PDF 抽取的要素."""

    confirming_party: str  # 回函银行
    account_no: str  # 账号
    confirmed_balance_cents: int  # 回函余额, 单位分
    balance_date: date  # 余额截至日
    receipt_channel: ReceiptChannel | None  # 回函渠道, 只用于核对
    evidence: list[EvidencePointer]  # 上述字段的页码指针
    # 回函写明的截至余额日已产生, 尚未记入账户的应计利息, 单位分: 银行欠客户为正, 客户欠银行 (利息与费用) 为负, 写
    # nil 为 0, 没有提到为空. 截至余额日已计的就取, 不论何时支付 (写明以后某日支付或记入, 属跨余额日的计息期都照取,
    # 需求 R354); 不取已入账的利息, 也不取余额日之后才产生的利息 (如下期利息); 只写整期利息, 没写截至余额日已计的
    # 部分时为空, 不自行计算. 漏写按空处理 (不算结构失败); 非空时须有 locator 为 accrued_interest_cents 的本文档指针
    # (引用级, 需求 R104)
    accrued_interest_cents: int | None = None


# 引用级要求有指针的摘录字段 (需求 R290): ACC-04 用它们核对公司已注册, 名称与董事名单
REGISTRY_POINTED = ("acn", "name", "status", "officeholders")


class RegistryExtraction(Model):
    """A3 从上传的 ASIC 公司摘录 PDF 抽取的要素 (DOC-REGISTRY, 需求 R290): AsicExtract 的登记信息, 高管与股东,
    加 ABN 与注册地址 (摘录没写为空). ACN 与 ABN 去掉空白后须为 9 位与 11 位数字 (结构级). evidence 的 locator 为
    字段名, REGISTRY_POINTED 中的各字段须有指针 (引用级)."""

    acn: str = Field(pattern=r"^\d{9}$")  # ACN, 9 位数字
    abn: str | None = Field(default=None, pattern=r"^\d{11}$")  # ABN, 11 位数字; 摘录没写为空
    name: str  # 公司名称
    type: str  # 公司类型, 如 APTY
    company_class: str  # 公司类别, 如 LMSH
    subclass: str  # 子类, 如 PROP
    status: Literal["Registered", "Deregistered"]  # 登记状态
    registered_date: date  # 注册日期
    registered_address: str | None = None  # 注册地址; 摘录没写为空
    officeholders: list[Officeholder]  # 董事与秘书 (同一人任两职为两项)
    shareholders: list[Shareholding]  # 股东
    paid_up_cents: int | None = None  # 实缴股本 (分); 摘录没写为空
    evidence: list[EvidencePointer]  # 各字段的页码指针, locator 为字段名

    @field_validator("acn", "abn", mode="before")
    @classmethod
    def digits_only(cls, value: object) -> object:
        """ACN 与 ABN 常按 3 位一组写 (900 043 030), 去掉空白再校验."""
        return "".join(value.split()) if isinstance(value, str) else value

    def unpointed(self) -> list[str]:
        """REGISTRY_POINTED 中没有 locator 为该字段的指针的字段 (引用级校验与 ACC-04 取摘录共用)."""
        pointed = {p.locator for p in self.evidence}
        return [name for name in REGISTRY_POINTED if name not in pointed]


class DocumentClassification(Model):
    """A3 按文件名与首页文字给上传的文件分类."""

    document_type: DocumentType | None  # 文件类型; 判断不了为空, 文件转人工指定类型
    reason: str  # 一句话依据


class SheetRecognition(Model):
    """A3 按工作表开头的样本行识别账表 (DOC-SHEET): 规整方案, 标题里读到的公司名与期间, 把握与理由. 不输出数据行."""

    plan: TablePlan  # 规整方案; kind 为空表示不是账表
    company: str | None  # 标题里的公司名, 没有为空
    period_start: date | None  # 标题里的期间起日, 没有为空
    period_end: date | None  # 标题里的期间止日或余额截至日, 没有为空
    confidence: Confidence  # high: 表种类与各字段的来源都清楚; low: 有拿不准的
    reason: str  # 一两句理由

    @model_validator(mode="after")
    def period_in_order(self) -> Self:
        if self.period_start is not None and self.period_end is not None and self.period_start > self.period_end:
            raise ValueError("period_start is after period_end")
        return self


class ReportDraft(Model):
    """RPT-03 生成的报告草稿: LLM 只写意见段与形成意见的基础段. 引用的判断写引用键, 不写判断 ID (重跑后判断 ID
    会变, 键不变); 由 RPT-03 换成当前判断的 ID 与版本 (见 services.judgment_engine.citation_keys)."""

    opinion: str  # 意见段正文
    basis_for_opinion: str  # 形成意见的基础段正文
    judgment_keys: list[str]  # 事实断言引用的已确认判断的引用键, 如 REV-02:SI-2026-0612; 取提示词列出的键


class AuditPlanDraft(Model):
    """PLN-15 由大模型写的计划叙述 (需求 R203): 计划骨架 (程序与参数, 重要性, 特别风险, 适用框架) 由 A4 确定性组装,
    不在这里; 校验后与骨架合成 AuditPlan (overall_strategy 与 sections 进 narrative)."""

    overall_strategy: str  # 总体审计策略: 范围, 时间与方向 (ASA 300 第 7 段)
    sections: dict[str, str]  # 段名 → 正文, 如 scope, significant_risks, materiality, timing
    procedure_notes: list[ProcedureNote]  # 各程序的性质, 时间与范围 (ASA 300 第 9 段), 应覆盖计划的全部程序
    timetable: list[Milestone]  # 时间表
    eqr_recommendation: EqrRecommendation  # 是否需要项目质量复核的建议 (ASQM 1 第 34(f) 段)
    expert_assessment: str  # 是否需要审计师的专家 (ASA 620) 的评估
    standards_cited: list[str]  # 引用的准则片段 ID, 取提示词列出的


class GoingConcernAdvice(Model):
    """FIN-02 由大模型写的持续经营结论建议 (需求 R183): 汇总持续经营评价与期后事项的判断, 建议结论, 审计师在闸门
    FIN-02 定结论. 推理链的规则同 JudgmentDraft; judgment_refs 须为当前版且已确认的判断, 含当前有效的持续经营评价判断
    (ValidationContext.required_refs); 推理链须有引用该建议所需准则的 standard 步骤 (basis_inappropriate 为
    ASA570-P17, 两种存在重大不确定性的为 ASA570-P18, judgment_engine.OUTCOME_STANDARDS). 校验见
    judgment_engine.check_going_concern."""

    proposed_outcome: GoingConcernOutcome  # 建议的结论
    conclusion: str  # 结论
    reasoning_chain: list[ReasoningStep]  # 推理链
    evidence: list[EvidencePointer]  # 引用的证据
    counter_argument: str  # 反方论证
    judgment_refs: list[JudgmentRef]  # 引用的判断 (ID 与版本)
    self_confidence: float = Field(ge=0, le=1)  # 模型自报的置信度 0-1


class CriticIndependent(Model):
    """GEN-09 第一轮: 独立复核只看证据得出的结论与推理链 (不看 A6 的结论). 推理链的规则同 JudgmentDraft.
    建议动作与错报草稿缺省为空 (旧的输出没有这两键照样通过); 建议 misstatement 时应给草稿 (需求 R150)."""

    conclusion: str  # 独立结论
    reasoning_chain: list[ReasoningStep]  # 独立推理链, fact 只能引用第一轮给出的证据指针
    proposed_action: ProposedAction | None = None  # 建议动作
    misstatement_draft: MisstatementDraft | None = None  # 建议认定错报时的错报草稿


class CriticVerdictOut(Model):
    """GEN-09 第二轮: 独立结论与 A6 结论的比对."""

    critic_verdict: Literal["consistent", "inconsistent"]  # 一致或不一致
    critic_reason_type: CriticReasonType | None = None  # 不一致时必填, 一致时为空
    critic_reason: str  # 一致或不一致之处

    @model_validator(mode="after")
    def reason_type_with_verdict(self) -> Self:
        """不一致必须给理由类型, 一致不能给 (结构级, 不合时回传模型重试)."""
        if (self.critic_verdict == "inconsistent") != (self.critic_reason_type is not None):
            raise ValueError("critic_reason_type is required when inconsistent and must be null when consistent")
        return self
