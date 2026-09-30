"""财务数据的行模型, 与 开发接口文档.md 第 9 节财务表逐列对应 (去掉 engagement_id)."""

from datetime import date, datetime
from typing import Literal

from backend.contracts.models import Model


class Account(Model):
    """科目表的一行."""

    code: str  # 科目代码
    name: str  # 科目名称
    type: Literal["asset", "liability", "equity", "revenue", "expense"]  # 科目类别: 资产, 负债, 权益, 收入, 费用


class TrialBalanceRow(Model):
    """试算表的一行: 某一期某科目的余额."""

    period: Literal["current", "prior"]  # 本期或上期
    account_code: str  # 科目代码
    balance_cents: int  # 借方为正


class JournalEntry(Model):
    """总账分录的一行: 一个科目的借方或贷方."""

    id: str  # 分录行 ID
    entry_no: str  # 凭证号, 同一笔分录的各行共用
    posting_date: date  # 入账日期
    created_at: datetime  # 录入时间
    created_by: str  # 录入人
    approved_by: str | None  # 审批人, 未审批为空
    account_code: str  # 科目代码
    debit_cents: int = 0  # 借方金额, 单位分
    credit_cents: int = 0  # 贷方金额, 单位分
    description: str | None  # 摘要
    source_doc: str | None  # 原始单据号, 如发票号
    is_adjustment: bool = False  # 是否调整分录


class SalesInvoice(Model):
    """销售发票."""

    id: str  # 发票 ID
    invoice_no: str  # 发票号
    customer: str  # 客户名称
    customer_abn: str | None  # 客户 ABN
    contract_id: str | None  # 对应的销售合同
    invoice_date: date  # 开票日期
    posting_date: date  # 入账日期
    amount_cents: int  # 发票金额, 单位分
    journal_entry_id: str | None  # 对应的收入分录


class DeliveryNote(Model):
    """发运单: 货物发出与客户签收记录."""

    id: str  # 发运单 ID
    delivery_no: str  # 发运单号
    invoice_id: str  # 对应的发票
    shipped_date: date | None  # 发货日期
    signed_date: date | None  # 客户签收日期
    signed_by: str | None  # 签收人


class BankAccount(Model):
    """银行账户."""

    id: str  # 账户 ID, 如 BA-01
    bank_name: str  # 开户行
    bsb: str  # BSB 号
    account_no: str  # 账号
    gl_account_code: str  # 对应的总账科目


class BankTransaction(Model):
    """银行流水中的一笔."""

    id: str  # 流水 ID
    bank_account_id: str  # 所属账户
    txn_date: date  # 交易日期
    amount_cents: int  # 存入为正
    balance_after_cents: int  # 交易后余额, 单位分
    description: str | None  # 摘要
    reference: str | None  # 参考号, 含发票号
