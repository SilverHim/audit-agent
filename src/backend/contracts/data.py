"""数据管理的领域类型 (数据接入, 导入, 体检, 补交资料), 定义见 开发接口文档.md 第 2.8 节.

这些类型不进编排状态 (EngagementState), 所以不必放在 models.py 的检查点白名单里.
"""

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from backend.contracts.models import Contract, DocumentType, Model
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
    # 试算表与报表草稿一致 {statements: 已提供的报表草稿文件数}; 报表数字的抽取与勾稽归 FIN-12 (还没实现), 届时另给
    # difference_cents
    tb_agrees_statements = "tb_agrees_statements"


# 体检结果: 通过, 不通过, 缺数据做不了, 还没做
CheckStatus = Literal["passed", "failed", "missing_data", "pending"]


class DataCheck(Model):
    """一项体检的结果."""

    id: CheckId  # 体检项
    status: CheckStatus  # 结果
    params: dict[str, Any] = {}  # 文案参数, 键见 CheckId
    request_id: str | None = None  # 不通过或缺数据时对应的补交请求, 前端据此链到工作区


class CheckRun(Model):
    """最近一次体检 (表 data_checks: 每项只存最近一次; 历次结果在 data 留痕里)."""

    data_version: int  # 体检时的数据版本
    checked_at: datetime  # 体检时间, 同一次体检的各项相同
    checks: list[DataCheck]  # 各项结果, 按 CheckId 顺序


class DataReadiness(Model):
    """项目财务数据的版本与就绪确认 (表 dataset_loads). 确认的人与时间记下后不改; 理由与未通过项随第一次确认记下,
    那时没有未通过项而再次确认时有, 补记那次的 (DataStore.confirm_ready)."""

    data_version: int  # 数据版本, 未接入任何数据为 0, 每次写入账表 +1
    loaded_at: datetime | None = None  # 第一次接入数据的时间
    ready_at: datetime | None = None  # 审计师确认数据已齐的时间, 未确认为空; 数据集载入视同确认
    ready_by: str | None = None  # 确认人: 用户 ID; 数据集载入为 "dataset"
    ready_reason: str | None = None  # 确认时写的理由; 最近一次体检有未通过项时必填, 没写为空
    ready_failed_checks: list[CheckId] = []  # 确认时最近一次体检不通过 (failed) 的项, 按 CheckId 顺序


class CheckInputs(Model):
    """体检要用的财务数据汇总 (DataStore.check_inputs, 由存储按 SQL 汇总); 体检规则见 services.data_quality."""

    journal_rows: int = 0  # 总账分录行数
    debit_cents: int = 0  # 全部分录的借方合计
    credit_cents: int = 0  # 全部分录的贷方合计
    movements: dict[str, int] = {}  # 审计期间内各科目分录的发生额 (借正), 期间内没有分录的科目不在其中
    preparer_rows: int = 0  # 有录入人的分录行数; 连接器的占位值 (如 Xero 的 "xero") 与空串不算
    approver_rows: int = 0  # 有审批人的分录行数; 空串不算
    tb_current: dict[str, int] = {}  # 本期试算表各科目余额 (借正)
    tb_prior: dict[str, int] = {}  # 上期试算表各科目余额
    account_types: dict[str, str] = {}  # 科目代码 → 科目类别 (asset, liability, equity, revenue, expense)
    account_categories: dict[str, AccountCategory | None] = {}  # 科目代码 → 标准分类, 未映射为空
    dataset: str | None = None  # 数据集载入时的数据集名 (dataset_loads.dataset), 如 demo, xero/20261017-q1


RequestKind = Literal["table", "document"]  # 补交的是账表 (走导入) 还是文件 (走上传)
# 提出的原因: 就绪必需的表缺失, 来源不提供某些字段, 客户没提供
RequestReason = Literal["missing_table", "field_unavailable", "not_provided"]
RequestStatus = Literal["open", "received", "withdrawn"]  # 待补交, 已收到, 已撤回 (暂无自动撤回)


class DataRequest(Model):
    """A3 提出的补交资料请求 (表 data_requests). 同一项资料只有一个请求, ID 固定为 REQ-{表种类或文件类型};
    收到后又缺了时重新打开 (status 回到 open, created_at 改为重新打开的时间, resolved_at 清空)."""

    id: str  # REQ-{table_kind} 或 REQ-{document_type}, 如 REQ-approval_log
    kind: RequestKind  # 账表或文件
    table_kind: TableKind | None = None  # 账表请求的表种类
    document_type: DocumentType | None = None  # 文件请求的文件类型
    title: str  # 中文名称; 前端按 table_kind 或 document_type 本地化, 这里只作缺省
    used_for: list[str] = []  # 要用这项资料的程序编号
    reason_code: RequestReason  # 原因
    reason_params: dict[str, Any] = {}  # 原因的参数: field_unavailable 为 {fields: [...], source}, 其余为空
    format_hint: str | None = None  # 可接受的格式, 如 "csv, xlsx, xls"
    status: RequestStatus = "open"  # 状态
    raised_by: str = "A3"  # 提出者
    created_at: datetime  # 提出 (或重新打开) 的时间
    resolved_at: datetime | None = None  # 转为 received 的时间


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


class TableColumn(Model):
    """账表查看的一列."""

    name: str  # 列名; 与导入字段名一致 (前端按它本地化)
    type: FieldType  # 字段类型, 决定显示方式


class TablePage(Model):
    """账表查看的一页 (GET /data/tables/{kind}): 符合搜索条件的总行数, 各列与这一页的行."""

    total: int  # 符合搜索条件的总行数
    columns: list[TableColumn]  # 各列, 按导入字段的顺序; 科目表另有分类, 依据, 两期余额与是否改过
    rows: list[dict[str, Any]]  # 这一页的行: 列名 → 值 (金额为整数分, 日期与时间为 ISO 文本, 没有为 null)


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


# ── 规整方案 (需求 R275): 把原样读出的工作表规整成某种账表的标准行, 由 services/table_plan 套用 ──

AmountUnit = Literal["dollars", "cents"]  # 金额单位: 元 (四舍五入到分) 或整数分


def _compiles(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"pattern {pattern!r} is not a valid regular expression: {exc}") from exc
    return pattern


class RowRule(Model):
    """按一列的文本认出一类行: 该列单元格的文本 (去首尾空白; 数值, 日期按导入的写法转成文本) 中有匹配 pattern 的部分
    (re.search, 要整格匹配就写 ^ 与 $)."""

    column: str  # 列名, 即表头的显示名 (重名的带 " (2)" 等后缀)
    pattern: str  # 正则

    @field_validator("pattern")
    @classmethod
    def pattern_compiles(cls, value: str) -> str:
        return _compiles(value)


class GroupTitle(RowRule):
    """分组标题行: 只有 column 一列有值, 且其文本匹配 pattern 的行. pattern 的命名捕获组是标题里的值, 向下填充到之后的
    数据行, 直到下一个分组标题 (没匹配上的可选组为空); 字段经 FieldSource.group 取用. 标题行本身不出行."""

    @field_validator("pattern")
    @classmethod
    def has_named_group(cls, value: str) -> str:
        if not re.compile(value).groupindex:
            raise ValueError(f"group title pattern {value!r} needs at least one named group, e.g. (?P<number>...)")
        return value


class FieldSource(Model):
    """一个字段的取值, 三选一: 一列 (column); 借贷两列之差 (debit 减 credit, 两列都空为 0, 只用于金额字段); 分组标题的
    命名捕获组 (group)."""

    column: str | None = None  # 取这一列
    debit: str | None = None  # 借方列, 与 credit 同给
    credit: str | None = None  # 贷方列
    group: str | None = None  # 分组标题里的命名捕获组
    # 一列带正负号拆借贷 (只用于取一列的金额字段, 不与 values 同给): positive 只取正数 (负数记 0), negative 取负数的
    # 绝对值 (正数记 0)
    part: Literal["positive", "negative"] | None = None
    # 取值映射 (只用于 column 与 group): 原文 → 系统取值, 原文按导入列名的规范化 (小写, 只留字母与数字) 比较, 规范化后
    # 不能重复; 映射表外的非空取值算解析失败, 不静默丢弃; 为空表示不映射
    values: dict[str, str] = {}

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if (self.debit is None) != (self.credit is None):
            raise ValueError("debit and credit must be given together")
        given = [self.column is not None, self.debit is not None, self.group is not None]
        if sum(given) != 1:
            raise ValueError("give exactly one of column, debit/credit, group")
        if self.part is not None and self.column is None:
            raise ValueError("part only applies to a single column")
        if self.part is not None and self.values:
            raise ValueError("part and values cannot be given together")
        if self.values and self.debit is not None:
            raise ValueError("values only apply to column or group")
        return self


class PeriodSource(Model):
    """试算表的一期: 本期与上期分列的报表, 每个数据行出一行该期的余额."""

    period: Literal["current", "prior"]  # 本期或上期
    balance: FieldSource  # 余额 (借正): 一列 (可带 part) 或借贷两列; 不取分组标题

    @model_validator(mode="after")
    def balance_not_from_group(self) -> Self:
        if self.balance.group is not None:
            raise ValueError("a period balance must come from a column or debit/credit columns")
        return self


class TablePlan(Model):
    """规整方案: 表种类, 表头行, 要跳过的行, 分组标题, 字段与取值, 试算表分期, 去重, 日期写法与金额单位.

    方案只说怎么读, 不含数据行; 由 services/table_plan.apply_plan 确定性地套到全表. 账表导入的按列对应
    (ImportCommit.mapping) 是只有列来源的方案. 读到的公司名, 期间, 把握与理由不在方案里.
    """

    kind: TableKind | None  # 规整成哪种账表; 为空表示不是账表, 不能套用
    header_row: int = Field(default=1, ge=1)  # 表头所在行, 文件中的实际行号 (第 1 行为 1); 之前的行 (标题) 不读
    skip: list[RowRule] = []  # 要跳过的数据行 (合计行等), 任一条匹配即跳过; 空行总是跳过
    group_title: GroupTitle | None = None  # 分组标题行, 标题里的值向下填充
    fields: dict[str, FieldSource] = {}  # 字段 → 取值, 字段名同导入字段 (TableSchema); 不出现的字段不对应
    # 本期与上期分列的试算表: 每个数据行每期出一行 (只用于试算表, 此时 fields 不写 period 与 balance_cents)
    periods: list[PeriodSource] = []
    # 同一唯一键的行内容完全相同时只留第一行 (如每个明细行都带发票合计的发票明细); 内容不同仍算解析失败
    dedupe: bool = False
    date_format: DateFormat = "dmy"  # 文本日期的写法
    amount_unit: AmountUnit | None = None  # 金额单位; 为空时按列的原始表头 (以 cents 结尾按分, 否则按元)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.kind is None and (self.fields or self.periods):
            raise ValueError("a plan for a non-table (kind null) has no fields or periods")
        names = set(re.compile(self.group_title.pattern).groupindex) if self.group_title else set()
        unknown = sorted({s.group for s in self.fields.values() if s.group is not None} - names)
        if unknown:
            raise ValueError(f"fields use groups {unknown} that the group title pattern does not capture")
        if len({p.period for p in self.periods}) != len(self.periods):
            raise ValueError("each period may appear only once in periods")
        return self


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


class ContractVersion(Model):
    """合同抽取结果的一个版本 (表 extraction_versions): 第 1 版为抽取所得, 之后每次审计师修改一版."""

    version: int  # 版本号
    contract: Contract  # 该版的完整内容
    field: str | None = None  # 这一版改的字段, 如 terms[0].text; 抽取所得为空
    reason: str | None = None  # 修改理由; 抽取所得为空
    changed_by: str  # 用户 ID; 抽取所得为 A3
    changed_at: datetime  # 时间


class ExtractionEdit(Model):
    """修改一个抽取字段 (PUT /contracts/{id}, PUT /confirmations/{id})."""

    field: str  # 字段, 如 counterparty, terms[0].text, confirmed_balance_cents
    value: (
        StrictStr | StrictInt | None
    )  # 新值: 文本, 整数分, YYYY-MM-DD 日期; 可空的字段可为 null. 严格类型: true 不当成 1
    reason: str  # 修改理由, 必填


class ExtractionRecord(Model):
    """抽取结果的一版 (表 extraction_versions 的一行)."""

    target_type: str  # contract (合同), confirmation (函证, 审计师修改), reply (上传的回函副本)
    target_id: str  # 合同编号, 函证 ID 或文件 ID
    version: int  # 版本号
    data: dict[str, Any]  # 该版完整内容
    field: str | None = None  # 改的字段; 抽取所得为空
    reason: str | None = None  # 修改理由
    changed_by: str  # 用户 ID 或 A3
    changed_at: datetime  # 时间
