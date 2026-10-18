"""数据管理的领域类型 (数据接入, 导入, 体检, 补交资料), 定义见 开发接口文档.md 第 2.8 节.

这些类型不进编排状态 (EngagementState), 所以不必放在 models.py 的检查点白名单里.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

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


# 账表的来源: Xero 同步, 审计师导入, 数据集载入 (种子与演示项目; 概览里显示为空)
DataSource = Literal["xero", "import", "dataset"]


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


class TableVersion(Model):
    """某张账表已写入的一个版本 (data_imports 中已提交的一行, 不含行快照). 旧版本不删除."""

    id: str  # 导入会话 ID; 其他来源为 {来源大写}-{表}-v{数据版本}
    kind: TableKind  # 账表种类
    source: DataSource  # 来源
    file_name: str | None  # 原文件名, 导入时有
    document_id: str | None  # 原文件登记成的证据文件 ID, 导入时有
    records: int  # 行数
    data_version: int  # 写入后的项目数据版本
    request_id: str | None  # 对应的补交请求
    created_by: str  # 写入者: 用户 ID 或智能体编号
    created_at: datetime  # 写入时间


class CheckId(StrEnum):
    """数据质量体检项. 前端按编号本地化文案, params 的键见各项注释."""

    debits_equal_credits = "debits_equal_credits"  # 借贷平衡 {debit_cents, credit_cents}
    journals_agree_tb = (
        "journals_agree_tb"  # 分录汇总与试算表一致 {accounts, differences: [{account_code, difference_cents}]}
    )
    count_agrees_source = "count_agrees_source"  # 记录数与来源一致 {source, expected, received}
    mapping_complete = "mapping_complete"  # 科目都已归入标准分类 {mapped, total}
    preparer_approver_available = "preparer_approver_available"  # 有录入人与审批人 {source, missing_fields}
    tb_agrees_statements = "tb_agrees_statements"  # 试算表与报表草稿一致 {difference_cents}


# 体检结果: 通过, 不通过, 缺数据做不了, 还没做
CheckStatus = Literal["passed", "failed", "missing_data", "pending"]


class DataCheck(Model):
    """一项体检的结果."""

    id: CheckId  # 体检项
    status: CheckStatus  # 结果
    params: dict[str, Any] = {}  # 文案参数, 键见 CheckId
    request_id: str | None = None  # 不通过时提出的补交请求


XeroStatus = Literal["connected", "not_connected", "expired"]  # Xero 连接状态: 已连接, 未连接, 授权过期


class XeroSource(Model):
    """数据概览中的 Xero 连接, 由 Xero 连接记录提供 (端口 XeroConnections)."""

    status: XeroStatus  # 连接状态
    organisation: str | None = None  # 已连接的组织名
    last_synced_at: datetime | None = None  # 最近一次同步时间
    scope: list[TableKind] = []  # 同步覆盖的账表


class DataSources(Model):
    """数据概览中的外部数据源."""

    xero: XeroSource  # Xero 连接


class RecordsDetail(Model):
    """试算表两期各自的行数."""

    current: int  # 本期行数
    prior: int  # 上期行数


TableStatus = Literal["received", "converting", "not_received"]  # 账表状态: 已接收, 转换中, 未接收


class TableSummary(Model):
    """数据概览中的一张账表."""

    kind: TableKind  # 账表种类
    source: Literal["xero", "import"] | None  # 最新一版的来源; 数据集载入或未接收为空
    file_name: str | None  # 最新一版导入的原文件名
    records: int  # 行数; 审批日志为最新一版的行数
    records_detail: RecordsDetail | None  # 试算表两期各自的行数, 其他表为空
    period_from: date | None  # 日期列的最早值, 没有日期列为空
    period_to: date | None  # 日期列的最晚值
    status: TableStatus  # 转换中: 有进行中的写入作业; 已接收: 有行 (审批日志: 有版本)
    updated_at: datetime | None  # 最新一版的写入时间; 数据集载入的为载入时间


class DataSummary(Model):
    """数据管理页的概览: 数据源, 各账表, 体检结果, 进行中的处理数与待补交请求数."""

    sources: DataSources  # 外部数据源
    tables: list[TableSummary]  # 各账表, 按 TableKind 顺序
    checks: list[DataCheck]  # 最近一次体检的各项结果, 按 CheckId 顺序
    processing: int  # A3 正在处理的文件与导入数; 不为 0 时前端每 3 秒轮询
    open_requests: int  # 待补交的请求数
