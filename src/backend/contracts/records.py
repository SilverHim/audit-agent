"""财务数据的行模型, 与 开发接口文档.md 第 9 节财务表逐列对应 (去掉 engagement_id)."""

from datetime import date, datetime
from typing import Literal

from backend.contracts.models import Model

# 标准科目分类: 程序按分类取科目, 不写死各公司, 各 ERP 的科目代码. 载入数据时由连接器映射, 认不出为 None (未映射)
AccountCategory = Literal[
    # 资产
    "cash",  # 货币资金
    "receivables",  # 应收款项 (含坏账准备)
    "inventory",  # 存货
    "prepayments",  # 预付款项
    "other_current_assets",  # 其他流动资产
    "property_plant_equipment",  # 固定资产 (含累计折旧)
    "intangible_assets",  # 无形资产
    "other_non_current_assets",  # 其他非流动资产
    # 负债
    "payables",  # 应付账款
    "accruals",  # 预提费用
    "tax_payable",  # 应交税费 (所得税, GST, PAYG)
    "employee_provisions",  # 应付职工薪酬与员工准备
    "borrowings",  # 借款与透支
    "other_current_liabilities",  # 其他流动负债
    "other_non_current_liabilities",  # 其他非流动负债
    # 权益
    "share_capital",  # 股本
    "retained_earnings",  # 留存收益
    "other_equity",  # 其他权益
    # 损益
    "revenue",  # 主营收入
    "other_income",  # 其他收入 (利息, 股利, 处置收益等)
    "cost_of_sales",  # 销售成本
    "employee_expenses",  # 职工薪酬费用
    "depreciation_amortisation",  # 折旧与摊销
    "finance_costs",  # 财务费用
    "operating_expenses",  # 其他经营费用
    "income_tax",  # 所得税费用
]


class Account(Model):
    """科目表的一行."""

    code: str  # 科目代码
    name: str  # 科目名称
    type: Literal["asset", "liability", "equity", "revenue", "expense"]  # 科目类别: 资产, 负债, 权益, 收入, 费用
    category: AccountCategory | None = None  # 标准分类, 载入时映射; None 为未映射


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


class AdjustmentEntrySummary(Model):
    """数据集中一笔可载入的更正分录的摘要 (DatasetLoader.adjustment_entries): 取该凭证首行的日期与摘要."""

    entry_no: str  # 凭证号, 如 AJE-X01
    posting_date: date  # 入账日期
    description: str | None  # 摘要
    line_count: int  # 行数


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
