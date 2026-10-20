"""数据管理的领域类型 (数据接入, 导入, 体检, 补交资料), 定义见 开发接口文档.md 第 2.8 节.

这些类型不进编排状态 (EngagementState), 所以不必放在 models.py 的检查点白名单里.
"""

import re
from collections.abc import Iterable
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from backend.contracts.models import (
    Contract,
    DocumentFailedStep,
    DocumentStatus,
    DocumentType,
    Model,
    Reliability,
)
from backend.contracts.records import AccountCategory, CategoryBasis


class TableKind(StrEnum):
    """账表种类: 八张财务表 (含应付账单, 需求 R178), 审批日志与工资册. 审批日志不单独成表, 导入后按凭证号补进总账
    分录的录入人, 审批人与审批时间. 工资册 (需求 R96) 不随数据集载入, 由审计师导入或演示数据导入写入."""

    accounts = "accounts"  # 科目表
    trial_balance = "trial_balance"  # 试算表 (本期与上期)
    journal_entries = "journal_entries"  # 总账分录
    sales_invoices = "sales_invoices"  # 销售发票
    supplier_bills = "supplier_bills"  # 应付账单的期末未付明细 (需求 R178)
    bank_accounts = "bank_accounts"  # 银行账户
    bank_transactions = "bank_transactions"  # 银行流水
    delivery_notes = "delivery_notes"  # 发运单
    approval_log = "approval_log"  # 审批日志
    employees = "employees"  # 工资册 (需求 R96)


class DataInput(StrEnum):
    """程序读的一类数据 (程序 YAML 的 reads, 数据变动记录 data_changes 的 input): 十种账表 (取值同 TableKind),
    加审计师修改过的合同与回函抽取结果, 审计师补登记的文件登记, 以及上传的 ASIC 摘录的抽取结果. 改科目分类改的是
    科目表, 记 accounts."""

    accounts = "accounts"  # 科目表 (含科目分类)
    trial_balance = "trial_balance"  # 试算表
    journal_entries = "journal_entries"  # 总账分录
    sales_invoices = "sales_invoices"  # 销售发票
    supplier_bills = "supplier_bills"  # 应付账单 (需求 R178)
    bank_accounts = "bank_accounts"  # 银行账户
    bank_transactions = "bank_transactions"  # 银行流水
    delivery_notes = "delivery_notes"  # 发运单
    approval_log = "approval_log"  # 审批日志 (补进总账分录的审批人与审批时间)
    employees = "employees"  # 工资册 (需求 R96)
    contracts = "contracts"  # 合同抽取结果 (审计师修改)
    confirmations = "confirmations"  # 回函抽取结果 (审计师修改)
    documents = "documents"  # 文件登记: 审计师补登记的可靠性, 文件日期与关联对象 (需求 R90)
    registry_extracts = "registry_extracts"  # 上传的 ASIC 摘录的抽取结果: A3 抽取与审计师修改 (需求 R290)


def table_changes(kinds: Iterable[TableKind]) -> list[DataInput]:
    """写入这些账表变了哪些数据 (按 DataInput 顺序): 各表本身; 审批日志按凭证号补进总账分录, 另记 journal_entries."""
    found = {DataInput(kind.value) for kind in kinds}
    if DataInput.approval_log in found:
        found.add(DataInput.journal_entries)
    return [i for i in DataInput if i in found]


class DataChange(Model):
    """一次数据变动: 哪个数据版本变了哪类数据 (表 data_changes). 一个版本可变多类 (一次写入几张账表, 数据集载入)."""

    data_version: int  # 变动后的项目数据版本
    input: DataInput  # 变了哪类数据


# 账表的来源: Xero 同步, 审计师导入, 数据集载入 (种子与演示项目; 概览里显示为空)
DataSource = Literal["xero", "import", "dataset"]
# 导入字段的类型: 文本, 日期, 日期时间, 金额 (整数分), 是否, 整数 (如工资册的发薪周期天数)
FieldType = Literal["text", "date", "datetime", "amount", "bool", "integer"]
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
# 待补交, 已收到, 已撤回 (要它的程序都不再执行, 资料又没到), 客户无此类资料 (审计师标记, 需求 R280; 定论, 不再重开)
RequestStatus = Literal["open", "received", "withdrawn", "unavailable"]


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
    resolved_at: datetime | None = None  # 转为 received, withdrawn 或 unavailable 的时间
    resolved_by: str | None = None  # 标记客户无此类资料的审计师 (用户 ID); A3 自动转的为空
    unavailable_reason: str | None = None  # 客户无此类资料的理由 (标记时必填)


XeroStatus = Literal["connected", "not_connected", "expired"]  # Xero 连接状态: 已连接, 未连接, 授权过期


# 后台同步失败的原因代码 (data 留痕 xero_sync_failed 的 reason): 授权失效, Xero 报错, 付费接口以外的资源无权限,
# 主密钥不可用, 响应转不了表, 写库失败
XeroSyncFailure = Literal["expired", "xero_error", "forbidden", "settings_key", "mapping", "write"]
# 同步没写某张表的原因 (留痕 xero_synced 的 skipped): 未取付费接口 (off, mock), live 下对账单接口 401, 403
XeroSkipReason = Literal["paid_off", "statements_forbidden"]


class XeroSyncResult(Model):
    """当前连接最近一次 Xero 同步的结果, 取本项目最近一条 data 留痕 xero_synced 或 xero_sync_failed (在最近一次
    xero_disconnect 之后的; 断开过而没再同步为空). 留痕里认不出的原因代码读成 unknown, 不让概览出错."""

    at: datetime  # 结束时间 (留痕写入时间)
    ok: bool  # 写入成功为真 (xero_synced), 失败为假 (xero_sync_failed)
    reason: XeroSyncFailure | Literal["unknown"] | None = None  # 失败的原因代码, 成功为空
    tables: list[TableKind] = []  # 写了的表, 失败为空
    skipped: dict[TableKind, XeroSkipReason | Literal["unknown"]] = {}  # 没写的表与原因, 失败为空


XeroProfileField = Literal["legal_name", "abn"]  # Xero 组织信息与客户画像核对的两项: 法定名称, ABN
XeroProfileDecision = Literal["keep", "update"]  # 审计师的选择: 保持原样 (写理由), 按 Xero 更新画像
XeroProfileResolution = Literal["kept", "updated"]  # 记下的选择 (data 留痕 xero_profile_kept, xero_profile_updated)
# 本项目相对承接决定 (ACC-01) 的位置: at 停在闸门 (按 Xero 更新时本项目采用新版本); before 承接阶段还没到闸门,
# after 已过承接 (这两种只给客户写新版本)
XeroProfileStage = Literal["before", "at", "after"]


class XeroProfileCheck(Model):
    """Xero 组织信息与本项目采用的客户画像的核对 (需求 R291). 组织信息取当前连接最近一次成功同步记下的 (data 留痕
    xero_synced 的 organisation_info), 须是当前选定的组织的; 画像取本项目采用的版本; 断开, 改选组织或换了组织后没再
    同步为空. differences 不为空且 resolution 为空时提示审计师. 选择与稍后处理都绑定 key: Xero 的组织, 两项取值或
    画像版本变了, key 跟着变, 再次提示."""

    key: str  # 本次核对的键 (XP- 加摘要): 组织 ID, Xero 的法定名称与 ABN, 画像版本
    xero_name: str  # 组织名 (Name)
    xero_legal_name: str  # 法定名称 (LegalName, 空时同 Name)
    xero_abn: str | None = None  # ABN (RegistrationNumber 去非数字后 11 位); Xero 没给时为空, 不比 ABN
    profile_version: int  # 本项目采用的画像版本
    profile_legal_name: str
    profile_abn: str
    differences: list[XeroProfileField] = []  # 对不上的项, 空即一致
    checked_at: datetime  # 记下组织信息的那次同步的时间
    next_version: int  # 按 Xero 更新时写出的画像版本 (该客户最新一版 +1, 可能高于本项目采用的版本 +1)
    stage: XeroProfileStage  # 本项目相对承接决定的位置, 定按 Xero 更新时本项目是否采用新版本
    resolution: XeroProfileResolution | None = None  # 审计师对这一 key 的选择
    reason: str | None = None  # 保持原样的理由
    resolved_by: str | None = None  # 作出选择的用户 ID
    resolved_at: datetime | None = None
    updated_version: int | None = None  # 更新时写出的画像版本
    adopted: bool | None = None  # 更新时本项目是否采用了新版本 (只在停在 ACC-01 闸门时采用)


# 演示数据导入 (需求 R269): 补造清单的阶段 (Xero演示数据集设计第 9.5 节, adapters.xero_supplements.plan); 导入只收
# 前三个, manual (管理层声明书) 由审计师在 FIN-07 的人工任务页上传
DemoStage = Literal["acceptance", "fieldwork", "statements", "manual"]
DEMO_IMPORT_STAGES: tuple[DemoStage, ...] = ("acceptance", "fieldwork", "statements")
DemoImportStatus = Literal["started", "imported", "already_imported", "failed"]


class DemoImportResult(Model):
    """演示数据导入一个阶段的结果 (需求 R269). 发起时返回 started, 内容为要写的表与要上传的文件; 做完时为 imported
    (有写入, 另记一条 data 留痕 demo_import, payload 即本对象) 或 already_imported (这一阶段都已导入过, 什么都没写,
    不记留痕); 作业中途出错为 failed (需求 R270: 同样记一条 demo_import 留痕, 只有阶段, 错误类别与数据版本, 已写的
    保留, 再执行接着做)."""

    stage: DemoStage  # 阶段
    status: DemoImportStatus
    tables: list[TableKind] = []  # 写了的表, 按写入顺序 (快照的同一版里按 TableKind 顺序); started 时为要写的
    uploaded: list[str] = []  # 新登记的上传文件名; started 时为要上传的
    retyped: list[str] = []  # 分类与清单不符, 按清单指定了类型的文件名
    needs_manual: list[str] = []  # 处理完仍待人工的文件名 (如抽取没通过)
    data_version: int | None = None  # 做完后的数据版本; started 时为空
    error: str | None = None  # failed 时的错误类别 (异常类名, 不带原始错误文本)


class DemoSummary(Model):
    """数据管理页的演示数据导入入口 (需求 R270). available 只对带补造目录的 Xero 演示数据集为真 (只查目录在不在,
    不核对哈希; 篡改由发起导入时的核对报出); 其余各项只在 available 时给."""

    available: bool = False
    stages_done: list[DemoStage] = []  # 已导入过的阶段 (有 imported 的 demo_import 留痕), 按 DEMO_IMPORT_STAGES 顺序
    # 按流程停的位置该导入的阶段: 停在 ACC-01 闸门为 acceptance, 图在等数据就绪为 fieldwork,
    # REQ-financial_statements 为 open 为 statements; 该阶段已导入过或不在这几个位置时为空
    suggested: DemoStage | None = None
    last: DemoImportResult | None = None  # 最近一次导入的结果 (最近一条 demo_import 留痕)


class XeroSource(Model):
    """数据概览中的 Xero 连接, 由 Xero 连接记录提供 (端口 XeroConnections)."""

    status: XeroStatus  # 连接状态
    organisation: str | None = None  # 已连接的组织名
    tenant_id: str | None = None  # 选定的组织 ID (多个组织未选时为空); 前端按它匹配组织列表
    last_synced_at: datetime | None = None  # 最近一次同步 (写入成功) 的时间
    scope: list[TableKind] = []  # 同步覆盖的账表
    syncing: bool = False  # 本项目的 Xero 同步作业在跑 (登记到结束, 期间再同步或导入报 ENGAGEMENT_BUSY)
    last_sync: XeroSyncResult | None = None  # 最近一次同步的结果 (成功或失败); 没有同步过为空
    profile_check: XeroProfileCheck | None = None  # 组织信息与客户画像的核对 (需求 R291); 没有成功同步过为空


class XeroTenant(Model):
    """授权给到的一个 Xero 组织."""

    id: str  # 组织 ID (tenantId)
    name: str  # 组织名称


# 同步写的七张表 (需求 R279 Q7; 应付账单为 R178 加的, 同步, 演示数据导入与数据集载入同一张表单): Xero 没有发运单
# 与审批日志, 不写, 已导入的保留; 没取到银行对账单时银行账户与银行流水这一对都不写 (services.xero_link)
XERO_TABLES: tuple[TableKind, ...] = (
    TableKind.accounts,
    TableKind.trial_balance,
    TableKind.journal_entries,
    TableKind.sales_invoices,
    TableKind.supplier_bills,
    TableKind.bank_accounts,
    TableKind.bank_transactions,
)


class XeroConnection(Model):
    """项目的 Xero 网页连接 (表 xero_connections 的一行, 需求 R279), 不含令牌: 令牌只以密文另存
    (XeroConnectionStore.tokens), 不进这个对象. 一个项目最多一个连接, 断开即删除."""

    tenants: list[XeroTenant] = []  # 授权给到的组织 (连接时取自 connections 接口)
    tenant_id: str | None = None  # 选定的组织; 只有一个组织时连接即选定, 多个时为空, 等审计师选
    tenant_name: str | None = None  # 选定的组织名称
    scope: str = ""  # 授权实际给到的 scope, 空格分隔
    status: Literal["connected", "expired"] = "connected"  # 刷新令牌失效 (invalid_grant) 后为 expired, 须重新连接
    connected_by: str  # 连接人的用户 ID
    connected_at: datetime  # 连接 (或重新连接) 时间
    refreshed_at: datetime  # 最近一次换得刷新令牌的时间 (连接或刷新); 超过 60 天按过期
    last_synced_at: datetime | None = None  # 最近一次同步成功的时间; 重新连接时保留
    synced_tables: list[TableKind] = []  # 最近一次同步写入的表


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
    """数据管理页的概览: 数据源, 各账表, 体检结果, 进行中的处理数, 待补交请求数与演示数据导入的入口."""

    sources: DataSources  # 外部数据源
    tables: list[TableSummary]  # 各账表, 按 TableKind 顺序
    checks: list[DataCheck]  # 最近一次体检的各项结果, 按 CheckId 顺序
    processing: int  # A3 正在处理的文件与导入数; 不为 0 时前端每 3 秒轮询
    open_requests: int  # 待补交的请求数
    demo: DemoSummary  # 演示数据导入的入口 (需求 R270)


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


Confidence = Literal["high", "low"]  # 识别的把握: 高或低


class SheetReading(Model):
    """A3 对上传的一个工作表的识别结果 (需求 R276): 大模型按开头的样本行给出 (DOC-SHEET), 大模型不可用时按列名匹配.

    方案由 services/table_plan.apply_plan 套到全表; 识别只看样本行, 不出数据行. 公司名与期间是表头里读到的, 与项目
    是否相符由确认单判断 (R277).
    """

    file_name: str  # 文件名
    sheet: str  # 工作表名; CSV 为文件名主干
    plan: TablePlan  # 规整方案; plan.kind 为空表示不是账表 (或按列名认不出)
    company: str | None = None  # 标题里读到的公司名, 没有为空
    period_start: date | None = None  # 读到的期间起日, 没有为空 (余额表只写截至日)
    period_end: date | None = None  # 读到的期间止日或余额截至日, 没有为空
    confidence: Confidence  # 把握; 按列名匹配时为低
    reason: str  # 理由; 按列名匹配时以 "按列名匹配, 请核对" 开头并写明原因
    method: Literal["llm", "column_names"]  # 识别方式: 大模型, 或退回按列名匹配
    trail_ids: list[str] = []  # 本次调大模型的各次 llm_call 留痕 (退回时为失败的那几次, 调用出错为空)


class TableOrigin(Model):
    """一张表这次写入的出处 (DataStore.write_tables 的 origins, 智能导入一次写入几个文件的表时逐表给)."""

    file_name: str | None = None  # 原文件名
    document_id: str | None = None  # 原文件登记成的证据文件 ID
    request_id: str | None = None  # 对应的补交请求


# ── 智能导入的批次与确认单 (需求 R277): 上传一批, A3 逐项识别, 审计师看确认单调整后一次写入 ──


class BatchIssueCode(StrEnum):
    """确认单一项的问题 (标红; 有任一项时缺省不勾). 前端按编号本地化文案, params 的键见各项注释."""

    low_confidence = "low_confidence"  # 识别把握低 (含大模型不可用而按列名匹配); 无参数
    not_a_table = "not_a_table"  # 工作表不是账表 (或按列名认不出); 无参数
    plan_invalid = "plan_invalid"  # 方案套不到工作表: {code: INVALID_VALUE 或 MISSING_FIELD, message}
    parse_failed = "parse_failed"  # 解析失败: {count: 失败处数}; 前 20 处见 BatchItem.failures
    company_mismatch = "company_mismatch"  # 读到的公司名与项目不符: {company, expected}
    # 读到的期间与项目不符: {period_start, period_end, expected_start, expected_end} (日期为 ISO 文本, 没读到为空)
    period_mismatch = "period_mismatch"
    duplicate_table = "duplicate_table"  # 同一批里同一张表出现两次: {kind, items: [同表其他项的 ID]}
    replaces_data = "replaces_data"  # 将替换已有数据: {kind, existing: 现有行数}
    unclassified = "unclassified"  # 文件判断不了类型: {step: classify 或 read (读不出文字)}
    extraction_failed = "extraction_failed"  # 合同或回函的抽取没通过校验; 无参数
    already_registered = "already_registered"  # 同一文件之前已登记 (内容相同): {document_id, type, status}


class BatchIssue(Model):
    """确认单一项的一个问题."""

    code: BatchIssueCode  # 问题
    params: dict[str, Any] = {}  # 参数, 见 BatchIssueCode 各项


class BatchFile(Model):
    """批次里的一个文件: 上传时登记为暂存的证据文件 (status staged), A3 处理完一个文件记 done (重启后接着做)."""

    name: str  # 文件名
    document_id: str  # 登记的证据文件 ID; 同一文件之前已登记时为已有的那份
    done: bool = False  # A3 是否已处理完 (确认单里有它的各项)
    not_imported: bool = False  # 确认时没勾选, 这份文件转为未导入 (文件状态 not_imported)


class BatchItem(Model):
    """确认单的一项: 表格文件的一个工作表, 或一份非表格文件 (含没有一个工作表是账表的表格文件)."""

    id: str  # {批次 ID}-{n}, n 从 1 起, 按文件与工作表的顺序
    file_name: str  # 文件名
    document_id: str  # 文件的登记 ID
    item_kind: Literal["table", "document"]  # 工作表或文件
    sheet: str | None = None  # 表格: 工作表名 (CSV 为文件名主干); 文件为空
    reading: SheetReading | None = None  # 表格: A3 的识别结果原样 (R276)
    plan: TablePlan | None = None  # 表格: 当前的规整方案, 识别所得或审计师调整后的; plan.kind 即识别为哪张表
    # 表格: 方案表头行的各列 (显示名, 同 Sheet.columns, 取遮蔽后的文本), 调整时列对应界面的可选列; 表头行不在表里或
    # 是空行时为空
    columns: list[str] = []
    # 表格: 各列的样例, 取表头之后该列第一个非空值. 列名与样例都从第 1 行起整体遮蔽 (mask_sample, 人名列与邮箱) 后再切
    sample: dict[str, str] = {}
    records: int | None = None  # 表格: 将写入的行数; 方案套不上或有解析失败时为空
    failure_count: int = 0  # 表格: 解析失败的处数
    failures: list[ParseFailure] = []  # 表格: 解析失败的前 20 处
    existing_records: int | None = None  # 表格: 该表现有的行数 (将被替换), 没有为空
    document_type: DocumentType | None = None  # 文件: 识别为哪类文件; 判断不了为空
    type_reason: str | None = None  # 文件: 分类理由 (A3), 按请求定类型的 "按补交请求 X 上传", 或审计师调整的理由
    job: str | None = None  # 文件: 定下类型的那条 classify 留痕 ID, 确认时抽取结果按它的作业写入
    contract: Contract | None = None  # 文件: 合同的抽取要素 (尚未写入)
    reply: dict[str, Any] | None = None  # 文件: 回函副本的抽取结果 (ConfirmationExtraction 的 JSON, 尚未写入)
    issues: list[BatchIssue] = []  # 问题 (标红)
    selected: bool = False  # 是否勾选写入
    chosen_by: str | None = None  # 审计师手动勾选或取消时的用户 ID; 为空时勾选随问题 (没有问题即勾)
    adjusted_by: str | None = None  # 审计师调整过方案或类型时的用户 ID


# 批次状态: A3 识别中, 待确认, 识别作业出错 (见 ImportBatch.error, 可重新识别没做完的文件), 已写入
BatchStatus = Literal["processing", "ready", "failed", "committed"]


class ImportBatch(Model):
    """一次智能导入 (表 import_batches): 上传的文件, A3 识别出的确认单与写入结果."""

    id: str  # BAT-{uuid4 前 12 位}
    status: BatchStatus  # 状态
    request_id: str | None = None  # 上传时带的补交请求 (补交卡片上的上传)
    created_by: str  # 上传人的用户 ID
    created_at: datetime  # 上传时间
    files: list[BatchFile]  # 上传的文件, 按上传顺序
    items: list[BatchItem] = []  # 确认单, 按文件与工作表的顺序
    committed_at: datetime | None = None  # 写入时间
    committed_by: str | None = None  # 确认人的用户 ID
    data_version: int | None = None  # 确认后的数据版本 (写了账表或合同时 +1); 都没写为空
    error: str | None = None  # 识别作业出错 (failed) 时的原因; 重新识别时清空


class DocumentChange(Model):
    """确认导入时一份文件要写的 (DataStore.commit_batch 在写表的同一事务里写): 登记改成的类型, 可靠性, 状态与卡住的
    步骤; 合同要依次写的各版 (版本规则见 ingest.contract_writes: 已有合同没有版本记录时先补记它) 与回函副本的抽取结果
    (reply, 第 1 版)."""

    document_id: str  # 证据文件 ID
    type: DocumentType  # 确认的类型
    reliability: Reliability  # 可靠性, 按类型
    status: DocumentStatus  # 写入后的状态: extracted, classified, 抽取没通过为 needs_manual
    failed_step: DocumentFailedStep | None = None  # 卡住的步骤 (needs_manual 时为 extract)
    contracts: list[Contract] = []  # 合同: 依次写入的各版 (save_contract 的写法, 记 A3)
    reply: dict[str, Any] | None = None  # 回函副本的抽取结果 (ConfirmationExtraction 的 JSON)
    registry_extract: dict[str, Any] | None = None  # ASIC 摘录的抽取结果 (RegistryExtraction 的 JSON, 需求 R290)


class BatchItemEdit(Model):
    """调整确认单的一项: 只给要改的. 改了方案或类型时按新结果重新判断问题; 同时给了 selected 的以它为准, 没给的
    按有没有问题重定勾选."""

    selected: bool | None = None  # 勾选或不勾
    plan: TablePlan | None = None  # 表格: 新的规整方案 (前端沿用列对应界面, 预填当前方案)
    document_type: DocumentType | None = None  # 文件: 改为哪类文件
    reason: str | None = None  # 改文件类型的理由, 改类型时必填


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


REGISTRY_TARGET = "registry_extract"  # 抽取版本表里 ASIC 摘录抽取结果的 target_type, target_id 为文件 ID (需求 R290)


class ExtractionRecord(Model):
    """抽取结果的一版 (表 extraction_versions 的一行)."""

    # contract (合同), confirmation (函证, 审计师修改), reply (上传的回函副本), document (文件登记), registry_extract
    # (上传的 ASIC 摘录: A3 抽取与审计师修改, 需求 R290)
    target_type: str
    target_id: str  # 合同编号, 函证 ID 或文件 ID
    version: int  # 版本号
    data: dict[str, Any]  # 该版完整内容
    field: str | None = None  # 改的字段; 抽取所得为空
    reason: str | None = None  # 修改理由
    changed_by: str  # 用户 ID 或 A3
    changed_at: datetime  # 时间


class DocumentDetailsEdit(Model):
    """补登记一份文件 (需求 R90): 可靠性, 文件日期与关联对象, 只给要改的, None 为不改; 本版不支持把文件日期清空.
    关联对象为业务对象的 ID (如公司 ACN, 分录号, 银行流水 ID, 客户编号), 不校验是否存在."""

    reliability: Reliability | None = None  # 可靠性 (原件或副本, 内部或外部)
    document_date: date | None = None  # 文件日期, 如签署或发出日
    related_ids: list[str] | None = None  # 关联对象 ID, 整体替换; 不能有空串或重复
    reason: str  # 补登记的理由, 必填
