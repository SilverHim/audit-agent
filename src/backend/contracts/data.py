"""数据管理的领域类型 (数据接入, 导入, 体检, 补交资料), 定义见 开发接口文档.md 第 2.8 节.

这些类型不进编排状态 (EngagementState), 所以不必放在 models.py 的检查点白名单里.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from backend.contracts.models import Model
from backend.contracts.records import AccountCategory, CategoryBasis


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
# 导入字段的类型: 文本, 日期, 日期时间, 金额 (整数分), 是否
FieldType = Literal["text", "date", "datetime", "amount", "bool"]
# 导入时文本日期的写法: 日月年, 年月日, 月日年
DateFormat = Literal["dmy", "ymd", "mdy"]


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


class TableField(Model):
    """导入时可对应的一个字段."""

    name: str  # 字段名, 与 persistence/models.py 中 FINANCIAL 各表的列名一致
    required: bool  # 是否必须对应到文件的某一列
    type: FieldType  # 字段类型, 决定解析方式


class TableSchema(Model):
    """一种账表的导入字段 (GET /data/schemas)."""

    kind: TableKind  # 账表种类
    fields: list[TableField]  # 可对应的字段, 按表的列顺序


class ImportSession(Model):
    """一次账表导入: 上传解析后为 parsed, 提交写入后为 committed. 暂存的原文件由存储保管.
    提交时记下的选择与结果 (kind 到 records) 在提交前为空, 用于识别同一提交的重试."""

    id: str  # 导入会话 ID: IMP-{uuid4 前 12 位}
    file_name: str  # 原文件名
    sheet: str  # 当前选中的工作表
    request_id: str | None = None  # 上传时带的补交请求
    status: Literal["parsed", "committed"] = "parsed"  # 已解析或已提交
    created_by: str  # 上传人的用户 ID
    created_at: datetime  # 上传时间
    kind: TableKind | None = None  # 提交时的表种类
    date_format: DateFormat | None = None  # 提交时的日期写法
    mapping: dict[str, str | None] | None = None  # 提交时的列对应
    records: int | None = None  # 写入的行数


class ImportPreview(Model):
    """上传账表文件后的解析结果: 工作表, 列, 样例行, 以及系统建议的表种类, 列对应与日期写法."""

    import_id: str  # 导入会话 ID, 换工作表与提交时带回
    file_name: str  # 原文件名
    sheets: list[str]  # 全部工作表名; CSV 只有一个, 名为文件名主干
    sheet: str  # 当前解析的工作表
    row_count: int  # 数据行数, 不含表头与空行
    columns: list[str]  # 表头各列 (重复的列名加 " (2)" 区分)
    sample: dict[str, str]  # 第一行数据, 列名 → 原值文本
    suggested_kind: TableKind | None  # 按列名与文件名猜的表种类, 必须字段对应不全时为空
    suggested_mapping: dict[str, str | None]  # 建议的对应: 字段 → 列, 没有合适的列为空; 表种类为空时为空字典
    suggested_date_format: DateFormat  # 按样例日期猜的写法, 定不了时取澳洲习惯 dmy


class ImportCommit(Model):
    """确认导入: 表种类, 工作表, 日期写法与列对应."""

    kind: TableKind  # 导入成哪种账表
    sheet: str  # 用哪个工作表
    date_format: DateFormat  # 文本日期的写法
    mapping: dict[str, str | None]  # 字段 → 列; 为空或不出现表示不对应


class ImportResult(Model):
    """导入提交后的结果."""

    kind: TableKind  # 账表种类
    records: int  # 写入的行数


class ParseFailure(Model):
    """一处解析失败; IMPORT_PARSE_ERROR 附带前 20 处."""

    row: int  # 文件中的实际行号 (第 1 行为 1, 含表头与空行)
    column: str  # 列名; 唯一键重复时为键的最后一列
    value: str  # 原值文本; 唯一键重复时为键值, 用 / 连接


class ApprovalLogRow(Model):
    """审批日志的一行: 某张凭证的录入人, 审批人与审批时间. 只含导入时对应了的字段 (model_dump(exclude_unset=True)),
    补进总账分录时只改这些列."""

    entry_no: str  # 凭证号, 对应 journal_entries.entry_no
    created_by: str | None = None  # 录入人
    approved_by: str | None = None  # 审批人, 空表示未审批
    approved_at: datetime | None = None  # 审批时间


class CategoryVersion(Model):
    """科目标准分类的一个版本: 第 1 版为写入科目表时系统的归类, 之后每次审计师修改, 或重新写入科目表时系统归类变了,
    各一版 (表 account_category_history)."""

    version: int  # 版本号, 从 1 起
    category: AccountCategory | None  # 标准分类, 未映射为空
    basis: CategoryBasis | None  # 依据: Xero 科目类型, 报告代码, 名称关键词, 审计师; 未映射为空
    changed_by: str  # 用户 ID 或智能体编号 (系统归类为 A3)
    changed_at: datetime  # 时间
    reason: str | None = None  # 审计师修改时的理由
