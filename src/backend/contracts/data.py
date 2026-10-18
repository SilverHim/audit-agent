"""数据管理的领域类型 (数据接入, 导入, 体检, 补交资料), 定义见 开发接口文档.md 第 2.8 节.

这些类型不进编排状态 (EngagementState), 所以不必放在 models.py 的检查点白名单里.
"""

from datetime import date, datetime
from enum import StrEnum

from backend.contracts.models import Model


class TableKind(StrEnum):
    """账表种类: 七张财务表与审批日志. 审批日志不单独成表, 导入后按凭证号补进总账分录的录入人, 审批人与审批时间."""

    accounts = "accounts"  # 科目表
    trial_balance = "trial_balance"  # 试算表 (本期与上期)
    journal_entries = "journal_entries"  # 总账分录
    sales_invoices = "sales_invoices"  # 销售发票
    bank_accounts = "bank_accounts"  # 银行账户
    bank_transactions = "bank_transactions"  # 银行流水
    delivery_notes = "delivery_notes"  # 发运单
    approval_log = "approval_log"  # 审批日志


class TableStats(Model):
    """一张账表当前的行数与日期范围."""

    records: int  # 行数; 审批日志为最新一版的行数
    current: int = 0  # 试算表本期行数, 其他表为 0
    prior: int = 0  # 试算表上期行数, 其他表为 0
    period_from: date | None = None  # 日期列的最早值, 没有日期列或没有行时为空
    period_to: date | None = None  # 日期列的最晚值


class DataReadiness(Model):
    """项目财务数据的版本与就绪确认 (表 dataset_loads)."""

    data_version: int  # 数据版本, 未接入任何数据为 0, 每次写入账表 +1
    loaded_at: datetime | None = None  # 第一次接入数据的时间
    ready_at: datetime | None = None  # 审计师确认数据已齐的时间, 未确认为空; 数据集载入视同确认
    ready_by: str | None = None  # 确认人: 用户 ID; 数据集载入为 "dataset"
