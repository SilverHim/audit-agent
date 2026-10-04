"""LLM 结构化输出模型, 定义见 开发接口文档.md 第 5 节."""

from datetime import date

from pydantic import Field

from backend.contracts.models import (
    EvidencePointer,
    JudgmentRef,
    MisstatementDraft,
    Model,
    ProposedAction,
    ReasoningStep,
    ReceiptChannel,
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


class ConfirmationExtraction(Model):
    """DOC-06 从银行回函 PDF 抽取的要素."""

    confirming_party: str  # 回函银行
    account_no: str  # 账号
    confirmed_balance_cents: int  # 回函余额, 单位分
    balance_date: date  # 余额截至日
    receipt_channel: ReceiptChannel | None  # 回函渠道, 只用于核对
    evidence: list[EvidencePointer]  # 上述字段的页码指针


class ReportDraft(Model):
    """RPT-03 生成的报告草稿: LLM 只写意见段与形成意见的基础段."""

    opinion: str  # 意见段正文
    basis_for_opinion: str  # 形成意见的基础段正文
    judgment_refs: list[JudgmentRef]  # 事实断言引用的已确认判断
