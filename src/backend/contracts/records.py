"""财务数据的行模型, 与 开发接口文档.md 第 9 节财务表逐列对应 (去掉 engagement_id); 另有数据集声明 DatasetTraits."""

import re
from datetime import date, datetime
from typing import Literal, Self

from pydantic import Field, model_validator

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


# 科目标准分类的依据: Xero 科目类型, Xero 报告代码, 名称关键词 (services.account_mapping), 审计师修改
CategoryBasis = Literal["xero_type", "reporting_code", "name", "auditor"]


class Account(Model):
    """科目表的一行."""

    code: str  # 科目代码
    name: str  # 科目名称
    type: Literal["asset", "liability", "equity", "revenue", "expense"]  # 科目类别: 资产, 负债, 权益, 收入, 费用
    category: AccountCategory | None = None  # 标准分类, 载入时映射; None 为未映射
    category_basis: CategoryBasis | None = None  # 分类依据; 未映射为空


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
    created_at: datetime | None  # 录入时间; 导入的总账可能没有, 为空 (不编造)
    created_by: str | None  # 录入人; 同上, 也可由审批日志补进
    approved_by: str | None  # 审批人, 未审批为空
    approved_at: datetime | None = None  # 审批时间, 由审批日志补进
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


class SupplierBill(Model):
    """应付账单的期末未付明细 (需求 R178): 截至审计期末仍有未付余额的供应商账单. Xero 由 ACCPAY 账单连同付款,
    贷项, 预付与多付的分配算出 (adapters.xero_dataset.map_supplier_bills); Numbat 没有, 表为空."""

    id: str  # 账单 ID, Xero 为 PB-{InvoiceID}
    bill_no: str  # 账单号; Xero 账单没有编号时取短 ID (同销售发票)
    supplier: str  # 供应商名称 (Xero 取账单上的 Contact.Name)
    bill_date: date  # 账单日期
    due_date: date  # 到期日
    amount_cents: int  # 含税总额, 单位分
    outstanding_cents: int  # 期末未付余额, 单位分 (总额减期末日及以前的付款与分配)
    journal_entry_id: str | None  # 对应的应付分录行 (贷记应付科目的那一行)


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


class Employee(Model):
    """工资册的一行 (账表 employees, 需求 R96): 一位员工的入职, 雇佣类型与每期税前工资 (FIN-15 F03 至 F05 用)."""

    employee_id: str  # 员工编号, 如 E001
    name: str  # 姓名
    start_date: date  # 入职日
    employment_type: str | None = None  # 雇佣类型, 如 full_time; 没有为空
    pay_period_days: int = Field(gt=0, le=366)  # 发薪周期的天数, 如 14; 至多一年 (366 天), 也在库里 int4 的范围内
    gross_per_period_cents: int = Field(ge=0)  # 每期税前工资, 单位分


class PayrollAccounts(Model):
    """数据集声明的薪酬科目对照, 各为科目代码列表 (FIN-15 F03 至 F05 用)."""

    wages_expense: list[str]  # 工资费用
    super_expense: list[str]  # 养老金费用
    super_payable: list[str]  # 应付养老金
    wages_payable: list[str]  # 应付工资
    leave_provision: list[str]  # 年假准备


class DatasetTraits(Model):
    """数据集声明: 跟数据来源有关的事实 (Xero 演示数据集设计第 5.2 节), 程序参数以 {ref: dataset.<键>} 引用.
    没有声明文件的数据集 (Numbat) 取缺省值."""

    dataset: str | None = None  # 声明所属的数据集名
    data_as_of: date | None = None  # 数据截止日; 为空不设上限
    prior_period_start: date | None = None  # 上期起始日; 为空视为与本期等长
    entry_timestamps_reliable: bool = True  # 分录的录入时间是否可信
    entry_timestamps_note: str | None = None  # 录入时间不可信的说明
    reversal_reference_pattern: str | None = None  # 作废冲回的说明格式, 恰好一个捕获组取出原凭证的编号
    reversal_entry_no_template: str | None = None  # 由编号拼出原凭证号的模板, 含 "{1}"
    counterparty_prefixes: list[str] = ["payment to", "payment from", "payment:"]  # 对方名称前的说明前缀
    audit_adjustment_entry_prefix: str = "AJE-"  # 审计更正分录的凭证号前缀
    payroll_accounts: PayrollAccounts | None = None  # 薪酬科目对照; 为空时薪酬程序不适用
    payroll_register: str | None = None  # 工资册文件 (相对补造目录)

    @model_validator(mode="after")
    def reversal_format(self) -> Self:
        """作废冲回的格式与模板同给或同空; 格式须能编译且恰好一个捕获组, 模板须含 "{1}"."""
        pattern, template = self.reversal_reference_pattern, self.reversal_entry_no_template
        if (pattern is None) != (template is None):
            raise ValueError("reversal_reference_pattern 与 reversal_entry_no_template 须同给或同空")
        if pattern is not None:
            try:
                groups = re.compile(pattern).groups
            except re.error as exc:
                raise ValueError(f"reversal_reference_pattern 不能编译: {exc}") from exc
            if groups != 1:
                raise ValueError(f"reversal_reference_pattern 须恰好一个捕获组, 现有 {groups} 个")
        if template is not None and "{1}" not in template:
            raise ValueError('reversal_entry_no_template 须含 "{1}"')
        return self
