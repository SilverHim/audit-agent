"""LLM 结构化输出模型, 定义见 开发接口文档.md 第 5 节."""

from datetime import date
from typing import Literal, Self

from pydantic import Field, model_validator

from backend.contracts.models import (
    CriticReasonType,
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


class CriticIndependent(Model):
    """GEN-09 第一轮: 独立复核只看证据得出的结论与推理链 (不看 A6 的结论). 推理链的规则同 JudgmentDraft."""

    conclusion: str  # 独立结论
    reasoning_chain: list[ReasoningStep]  # 独立推理链, fact 只能引用第一轮给出的证据指针


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
