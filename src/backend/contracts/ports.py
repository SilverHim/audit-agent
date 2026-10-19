"""存储, 外部系统与 LLM 的接口, 定义见 开发接口文档.md 第 4, 5 节.

实现方: Store, DatasetLoader, RuleEngine, AuditTrail, mock 适配器由 B 实现; LLMClient 由 A 实现.
上层只依赖这里的 Protocol, 具体实现在组装根 (main.py) 注入.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel

from backend.contracts.conversation import ChatEvent, ChatMessage, ToolSpec
from backend.contracts.data import (
    CategoryVersion,
    CheckId,
    CheckInputs,
    CheckRun,
    ContractVersion,
    DataCheck,
    DataReadiness,
    DataRequest,
    DataSource,
    ExtractionRecord,
    ImportSession,
    TableKind,
    TableStats,
    TableVersion,
    XeroSource,
)
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import (
    AbnRecord,
    AgentId,
    AsicCompanyRecord,
    AuditObjective,
    ClientProfile,
    Contract,
    DocumentChannel,
    DocumentFailedStep,
    DocumentRef,
    DocumentStatus,
    DocumentType,
    Engagement,
    Judgment,
    LlmSettings,
    LlmSlot,
    Model,
    Phase,
    ProcedureRun,
    ReceiptChannel,
    Reliability,
    RiskItem,
    SourceType,
    TrailEntry,
    TrailKind,
    User,
    Versioned,
)
from backend.contracts.records import (
    Account,
    AccountCategory,
    AdjustmentEntrySummary,
    BankAccount,
    BankTransaction,
    DatasetTraits,
    DeliveryNote,
    JournalEntry,
    SalesInvoice,
    TrialBalanceRow,
)
from backend.contracts.services import RuleResult
from backend.contracts.views import JudgmentView, TrailEntryView

T = TypeVar("T", bound=Versioned)
T_out = TypeVar("T_out", bound=BaseModel)


class FinancialDataStore(Protocol):
    """财务数据的读取 (B 实现): 试算表, 分录, 发票, 银行流水, 合同等."""

    async def query(self, eid: str, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        """只读事务, 超时 10 s;:eid 自动绑定. 只给 RuleEngine 用."""
        ...

    async def trial_balance(self, eid: str, period: Literal["current", "prior"]) -> list[TrialBalanceRow]: ...

    async def gl_balance(self, eid: str, account_code: str, as_of: date) -> int: ...

    async def record(self, eid: str, source_type: SourceType, source_id: str) -> dict[str, Any] | None:
        """取一行明细; trial_balance 的 source_id 为 account_code."""
        ...

    async def journal_entries(
        self, eid: str, *, entry_nos: Sequence[str] | None = None, source_docs: Sequence[str] | None = None
    ) -> list[JournalEntry]:
        """凭证号在 entry_nos 内或原始单据号在 source_docs 内的分录行 (满足任一即取, 不重复), 按行 ID 升序.

        None 为不按该条件取, 空列表为该条件取不到行; 两个都为 None 抛 ValueError (不取全表)."""
        ...

    async def accounts(self, eid: str, codes: Sequence[str] | None = None) -> list[Account]:
        """代码在 codes 内的科目 (含标准分类, 按载入时所存), 按代码升序; codes 为 None 取全部."""
        ...

    async def sales_invoices(self, eid: str, ids: Sequence[str]) -> list[SalesInvoice]: ...

    async def delivery_notes(self, eid: str, invoice_ids: Sequence[str]) -> list[DeliveryNote]: ...

    async def bank_accounts(self, eid: str) -> list[BankAccount]: ...

    async def bank_transactions(
        self, eid: str, bank_account_id: str, date_from: date, date_to: date
    ) -> list[BankTransaction]: ...

    async def contracts(self, eid: str, ids: Sequence[str] | None = None) -> list[Contract]: ...

    async def save_contract(
        self,
        eid: str,
        contract: Contract,
        *,
        changed_by: str = AgentId.A3,
        field: str | None = None,
        reason: str | None = None,
    ) -> None:
        """DOC-04 写入与审计师修改共用: 按 id 覆盖合同与条款为这一版, 并在 extraction_versions 记这一版的完整内容
        (同版本已有则覆盖, 抽取重做时幂等). changed_by, field, reason 为修改人, 改的字段与理由, 抽取所得为 A3 与空."""
        ...

    async def contract_history(self, eid: str, contract_id: str) -> list[ContractVersion]:
        """合同的各版本, 按版本升序; 没有记录 (迁移 0004 之前抽取的) 为空."""
        ...

    async def documents(self, eid: str, type: DocumentType | None = None) -> list[DocumentRef]: ...

    async def data_version(self, eid: str) -> int: ...


class AuditOutputStore(Protocol):
    """审计产出的存储 (B 实现): 判断, 例外, 任务等按 id + version 追加, 旧版本保留."""

    async def put(self, eid: str, objs: Sequence[Versioned], *, actor: str) -> list[Versioned]:
        """按顺序追加新版本行, 返回存储中的对象.

        同 (id, version) 已存在: 不比较内容, 跳过并返回已存对象; 否则 version 须为已有最大值 +1
        (新 id 为 1), 不符抛 VersionConflict; 项目 locked 抛 EngagementLocked.
        """
        ...

    async def get(self, eid: str, kind: type[T], id: str, version: int | None = None) -> T | None: ...

    async def latest(self, eid: str, kind: type[T], **filters: Any) -> list[T]:
        """每个 id 的最新版本; filters 按顶层字段相等匹配."""
        ...

    async def history(self, eid: str, kind: type[T], id: str) -> list[T]: ...

    def transaction(self, eid: str) -> AbstractAsyncContextManager[None]:
        """块内的 put 共用一个事务; 嵌套调用加入外层事务."""
        ...


class EvidenceStore(Protocol):
    """证据原件的存储 (B 实现): 存 PDF, 登记文件, 读页面文本."""

    async def save(
        self,
        eid: str,
        file_name: str,
        data: bytes,
        *,
        type: DocumentType,
        reliability: Reliability,
        media_type: str = "application/pdf",
        channel: DocumentChannel = "auditor_upload",
        uploaded_by: str | None = None,
        status: DocumentStatus = "received",
    ) -> DocumentRef:
        """存原件, 算 sha256, 登记 documents; 同一哈希重复保存返回已有记录.

        PDF 按 pdfplumber 数页数, 读不出 (损坏) 记 0 照样登记; 其他媒体类型页数记 1. status 为登记时的处理状态
        (导入的账表原件已转换, 记 extracted).
        """
        ...

    async def path(self, eid: str, document_id: str) -> Path: ...

    async def pages(self, eid: str, document_id: str) -> list[str]:
        """pdfplumber 文本层, 下标 0 为第 1 页."""
        ...

    async def set_status(self, eid: str, document_id: str, status: DocumentStatus) -> None:
        """在 AuditOutputStore.transaction 块内调用时加入该事务."""
        ...

    async def update_document(
        self,
        eid: str,
        document_id: str,
        *,
        type: DocumentType,
        reliability: Reliability,
        status: DocumentStatus,
        failed_step: DocumentFailedStep | None,
    ) -> DocumentRef:
        """A3 处理上传文件时改登记的类型, 可靠性, 状态与卡住的步骤, 返回改后的登记; 没有该文件抛 FileNotFoundError."""
        ...


class DatasetLoader(Protocol):
    """合成数据集的载入 (B 实现): 财务数据入库, 提供预置的计划数据与客户画像."""

    async def load(self, eid: str, dataset: str) -> None:
        """PLN-10 替代: 载入财务表, 合同 PDF 经 EvidenceStore.save 登记; 回函 PDF 不登记. 已载入则跳过."""
        ...

    def planning_presets(self, dataset: str) -> tuple[list[AuditObjective], list[RiskItem]]: ...

    def client_profile(self, dataset: str) -> ClientProfile: ...

    async def load_adjustments(
        self, eid: str, dataset: str, entry_nos: Sequence[str] | None = None
    ) -> list[JournalEntry]:
        """按凭证号把数据集的更正分录 (adjustments/journal_entries.csv) 载入 journal_entries, 返回所请求分录的全部行,
        按 id 排序; entry_nos 为 None 时载入文件中全部, 为空列表时不载入, 返回 [] (要全部须传 None). 幂等: 已有的
        同 id 同内容行跳过, 有新行时 data_version 加一;
        试算表不动. 凭证号不在文件中抛 LookupError; 项目尚未载入, 数据集不同, 同 id 内容不同或某笔借贷不平抛
        ValueError; 项目锁定抛 EngagementLocked; 数据集没有更正分录文件抛 FileNotFoundError. 都不写入."""
        ...

    def adjustment_entries(self, dataset: str) -> list[AdjustmentEntrySummary]:
        """可载入的更正分录摘要, 按凭证号自然序; 没有更正分录文件时为空."""
        ...

    def adjustment_entry_nos(self, dataset: str) -> list[str]:
        """可载入的更正分录凭证号, 顺序同 adjustment_entries (更正通知提交时校验用)."""
        ...

    def dataset_traits(self, dataset: str) -> DatasetTraits:
        """数据集声明 (程序参数 {ref: dataset.<键>} 的取值); 没有声明的数据集为缺省值 DatasetTraits()."""
        ...

    def datasets(self) -> list[str]:
        """可以建项目的数据集名, 按名称排序; 新建项目时列给审计师选客户."""
        ...

    def period(self, dataset: str) -> tuple[date, date] | None:
        """数据集自带的审计期间 (起始日, 截止日), 建项目时期间须与之相同; 不自带时为 None (由审计师填).
        Xero 取快照的 dataset.json; CSV 数据集 (如 Numbat) 为 None."""
        ...


class DataStore(Protocol):
    """数据管理的存储 (B 实现): 数据版本与就绪确认, 账表统计. 表见开发接口文档第 9 节."""

    async def readiness(self, eid: str) -> DataReadiness:
        """数据版本与就绪确认; 没接入过数据时版本为 0, 未确认."""
        ...

    async def confirm_ready(
        self, eid: str, actor: str, *, reason: str | None = None, failed_checks: Sequence[CheckId] = ()
    ) -> DataReadiness:
        """记审计师确认数据已齐, 连同理由与当时最近一次体检不通过的项, 返回确认后的状态. 已确认的保留第一次的人与
        时间; 已记的未通过项为空而本次有时, 补记本次的理由与未通过项 (要求写的理由不丢), 否则理由与未通过项也不改.

        项目 locked 抛 EngagementLocked."""
        ...

    async def stats(self, eid: str) -> dict[TableKind, TableStats]:
        """各账表当前的行数与日期范围, 每种账表一项."""
        ...

    async def versions(self, eid: str) -> list[TableVersion]:
        """全部已写入的账表版本 (不含行快照), 按数据版本升序."""
        ...

    async def checks(self, eid: str) -> list[DataCheck]:
        """最近一次体检的各项结果, 按 CheckId 顺序; 没体检过为空."""
        ...

    async def open_requests(self, eid: str) -> int:
        """待补交 (status open) 的请求数."""
        ...

    async def check_inputs(self, eid: str, period_start: date, period_end: date) -> CheckInputs:
        """体检要用的财务数据汇总 (按 SQL 汇总, 口径见 services.data_quality.summarize); 发生额只算入账日期在
        [period_start, period_end] 内的分录."""
        ...

    async def check_run(self, eid: str) -> CheckRun | None:
        """最近一次体检 (各项结果, 体检时的数据版本与时间); 没体检过为 None."""
        ...

    async def requests(self, eid: str) -> list[DataRequest]:
        """全部补交请求 (含已收到的), 按提出时间升序, 同时提出的按 data_quality.REQUEST_ORDER."""
        ...

    async def table_rows(
        self, eid: str, kind: TableKind, q: str | None, offset: int, limit: int
    ) -> tuple[int, list[dict[str, Any]]]:
        """某张账表当前的行 (账表查看): 返回 (符合搜索的总行数, 这一页的行); 行为列名 → 值, 不含 engagement_id.
        q 为不分大小写的包含匹配: 科目表查代码与名称, 其余表查各文本列, 审批日志查各列. 排序: 科目表按代码, 试算表按
        科目, 期间, 总账按入账日期, 凭证号, 行 ID, 其余按日期与单号. 科目表的行另带 balance_current_cents,
        balance_prior_cents (试算表两期余额, 没有为空) 与 edited (分类依据为 auditor). 审批日志不成表, 取最新一版的
        行快照."""
        ...

    async def account(self, eid: str, code: str) -> Account | None:
        """科目表中的一个科目; 没有为 None."""
        ...

    async def category_history(self, eid: str) -> dict[str, list[CategoryVersion]]:
        """各科目的分类历史 (科目代码 → 各版, 按版本升序); 没有历史的科目 (数据集载入后没改过的) 不在其中."""
        ...

    async def set_category(
        self, eid: str, code: str, category: AccountCategory, *, actor: str, reason: str
    ) -> CategoryVersion:
        """审计师修改科目的标准分类, 一个事务内: 没有分类历史时先补记当前分类为第 1 版 (修改人 A3, 时间取首次载入);
        记新的一版 (依据 auditor, 修改人, 理由); 科目表的分类与依据改为这一版; 数据版本 +1. 返回新的一版.
        科目不存在抛 LookupError; 项目 locked 抛 EngagementLocked."""
        ...

    async def save_check_run(self, eid: str, run: CheckRun, requests: Sequence[DataRequest]) -> None:
        """一个事务内记下一次体检: data_checks 换成这次的各项结果, 并按 ID 写入 (新建或覆盖) 给出的补交请求.
        项目 locked 抛 EngagementLocked."""
        ...

    async def extraction_versions(
        self, eid: str, target_type: str | None = None, target_id: str | None = None
    ) -> list[ExtractionRecord]:
        """抽取结果的各版 (可按类型与对象筛), 按类型, 对象, 版本升序."""
        ...

    async def bump_version(self, eid: str) -> int:
        """数据版本 +1 (修改抽取结果, 科目分类等不经 write_tables 的数据变动), 返回新版本."""
        ...

    async def save_extraction(
        self,
        eid: str,
        target_type: str,
        target_id: str,
        version: int,
        data: Mapping[str, Any],
        *,
        changed_by: str,
        field: str | None = None,
        reason: str | None = None,
    ) -> None:
        """在 extraction_versions 记一版抽取结果 (同版本已有则覆盖). target_type: confirmation (函证, 审计师修改),
        reply (上传的回函副本的抽取结果, target_id 为文件 ID)."""
        ...

    async def write_tables(
        self,
        eid: str,
        tables: Mapping[TableKind, Sequence[BaseModel]],
        *,
        source: DataSource,
        actor: str,
        file_name: str | None = None,
        document_id: str | None = None,
        import_id: str | None = None,
        request_id: str | None = None,
    ) -> int:
        """新数据版本写入 (导入与 Xero 同步共用), 返回写入后的数据版本. 一个事务内: 数据版本 +1; 各表按 TableKind 顺序
        整表替换 (审批日志不成表, 按凭证号补进总账的录入人, 审批人与审批时间, 只改对应了的列; 写总账时再套用最新一版
        审批日志); 科目表对照分类历史 (审计师改过的沿用审计师的分类, 系统归类有变化的记一版); 每张表在 data_imports
        记一行版本 (含行快照), 有 import_id 时把该导入会话置 committed. 行模型: 科目表 Account, 审批日志
        ApprovalLogRow, 其余同 FINANCIAL 各表. 项目 locked 抛 EngagementLocked."""
        ...

    async def create_import(self, eid: str, session: ImportSession, data: bytes) -> None:
        """暂存上传的原文件并登记导入会话 (status parsed)."""
        ...

    async def import_session(self, eid: str, import_id: str) -> tuple[ImportSession, bytes] | None:
        """导入会话与暂存的原文件; 没有该会话为 None."""
        ...

    async def update_import(
        self,
        eid: str,
        import_id: str,
        *,
        sheet: str,
        date_format: str | None = None,
        mapping: Mapping[str, str | None] | None = None,
        row_count: int | None = None,
    ) -> None:
        """记导入会话当前的工作表; 提交时另记日期写法, 列对应与数据行数 (为空的不改). 只改未提交 (parsed) 的会话."""
        ...


class XeroConnections(Protocol):
    """项目的 Xero 连接记录 (需求 R12 提供实现), 数据概览的 sources.xero 取它."""

    async def source(self, eid: str) -> XeroSource:
        """连接状态, 组织名, 最近同步时间与同步覆盖的账表; 没连接过为 not_connected."""
        ...


class EngagementStore(Protocol):
    """项目与客户画像的存储 (B 实现). 跨项目表, 不带 eid 前缀参数."""

    async def create(
        self,
        *,
        owner_id: str,
        period_start: date,
        period_end: date,
        dataset: str,
        profile: ClientProfile,
    ) -> Engagement:
        """取号 ENG-{yyyy}-{nnn}; client_id = profile.id, client_profile_version = profile.version;
        画像写入 client_profiles; phase = P0_Acceptance."""
        ...

    async def get(self, eid: str) -> Engagement | None: ...

    async def update(
        self,
        eid: str,
        *,
        phase: Phase | None = None,
        locked: bool | None = None,
        version_snapshot: dict[str, str] | None = None,
        client_profile_version: int | None = None,
    ) -> None:
        """传 None 的不改, 同时刷新 updated_at; locked 置真后不可回退;
        在 AuditOutputStore.transaction 块内调用时加入该事务."""
        ...

    async def client_profile(self, client_id: str, version: int | None = None) -> ClientProfile | None: ...

    async def save_profile(self, profile: ClientProfile) -> ClientProfile:
        """客户画像出新版本, 返回存储中的画像. profile.version 须为已有最大值 +1, 不符抛 VersionConflict;
        在 AuditOutputStore.transaction 块内调用时加入该事务."""
        ...

    async def list(self, owner_id: str | None = None) -> list[Engagement]:
        """owner_id 为空列全部, 供 recover."""
        ...


class UserStore(Protocol):
    """用户的存储 (B 实现), 供登录校验与注册."""

    async def by_email(self, email: str) -> tuple[User, str] | None:
        """按登录名查找 (不区分大小写), 返回 (用户, password_hash), 登录用."""
        ...

    async def get(self, user_id: str) -> User | None: ...

    async def create(self, email: str, full_name: str, password_hash: str) -> User | None:
        """注册新用户; 登录名 (不区分大小写) 已被占用时返回 None, 不改原账号."""
        ...


class LlmSettingsStore(Protocol):
    """设置页的模型配置 (B 实现): 系统级表, 不按项目隔离, 访问规则同 users."""

    async def get(self, slot: LlmSlot) -> LlmSettings | None:
        """该套配置; 没保存过为 None."""
        ...

    async def save(self, settings: LlmSettings) -> LlmSettings:
        """按 slot 覆盖写入 (只存最新一行), 返回存储中的配置."""
        ...


class RegistrySource(Protocol):
    """ABN 与 ASIC 登记查询, 首参为项目 ID. mock 按项目的数据集读该数据集目录下的 registry/ (Numbat 为
    data/demo, Xero 为补造目录; 没有目录时查不到)."""

    async def abn(self, eid: str, abn: str) -> AbnRecord | None: ...

    async def asic_company(self, eid: str, acn: str) -> AsicCompanyRecord | None: ...


class ConfirmationRequest(Model):
    """发给函证平台的询证请求."""

    bank_account_id: str  # 被函证的账户 ID
    bank_name: str  # 开户行
    bsb: str  # BSB 号
    account_no: str  # 账号
    as_of: date  # 询证的余额截至日


class ConfirmationReceipt(Model):
    """函证平台返回的回执."""

    sent_at: datetime  # 发函时间
    replied_at: date | None  # 回函日期, 未回函为空
    reply_file_name: str | None  # 回函文件名
    reply_bytes: bytes | None  # 回函 PDF 内容
    receipt_channel: ReceiptChannel | None  # 回函渠道


class ConfirmationChannel(Protocol):
    """函证平台: 发询证函, 收回函. mock 按项目的数据集读该数据集目录下的 confirmations/{account_no}.pdf
    (没有目录或没有该账号的回函时为未回函)."""

    async def submit(self, eid: str, req: ConfirmationRequest, idempotency_key: str) -> ConfirmationReceipt:
        """同一幂等键重复调用返回同一回执."""
        ...


class LLMMessage(Model):
    """对话中的一条消息."""

    role: Literal["user", "assistant"]  # 发言方
    content: str  # 内容


class LLMRequest(Model):
    """一次大模型调用的请求."""

    procedure_id: str  # 发起调用的程序
    run_id: str  # 发起调用的那次执行
    system: str  # 系统提示词
    messages: list[LLMMessage]  # 对话消息, 重试时追加上次的输出与错误
    max_tokens: int  # 最多输出的 token 数
    temperature: float = 0  # 采样温度, 默认 0 求稳定
    fixture_key: str = "default"  # mock 模式下取固定输出用的键
    eid: str = (
        ""  # 所属项目; mock 模式按其数据集定 fixture 目录 (DatasetFixtureLLMClient), live 模式不用, 不进 prompt_hash
    )


class LLMResponse(Model, Generic[T_out]):
    """一次大模型调用的结果."""

    output: T_out | None  # 按输出模型解析出的结果, 解析失败为空
    raw: str  # 模型的原始输出文本
    model: str  # 实际使用的模型名
    input_tokens: int  # 输入 token 数
    output_tokens: int  # 输出 token 数
    error: str | None = None  # 调用或解析的错误信息


class LLMClient(Protocol):
    """大语言模型客户端: mock 按程序返回固定输出, live 调真实模型."""

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]: ...


class ChatModel(Protocol):
    """审计助理 (A10) 用的对话模型: 带工具调用的流式 Chat Completions.

    产出若干 TextDelta, 最后一个 AssistantTurn (文本, 工具调用, token 数). 只在产出第一个事件之前重试;
    调用方停止迭代 (aclose 或取消) 时关闭上游连接.
    """

    def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        tool_choice: Literal["auto", "none"] = "auto",
        max_tokens: int,
    ) -> AsyncIterator[ChatEvent]: ...


class ChatModelSource(Protocol):
    """每次提问时取当前生效的对话模型: 设置页切换主模型后, 下一次提问即用新的."""

    async def current(self) -> ChatModel: ...


class RuleEngine(Protocol):
    """规则引擎 (B 实现): 执行程序 YAML 里的 SQL, 结果包装成例外."""

    async def run(
        self, eid: str, template: ProcedureTemplate, run: ProcedureRun, params: Mapping[str, Any]
    ) -> RuleResult: ...


class AuditTrail(Protocol):
    """审计留痕 (B 实现), 只追加. 独立事务写入, 不随业务事务回滚."""

    async def record(self, entry: TrailEntry) -> str: ...

    async def get(self, eid: str, id: str) -> TrailEntry | None: ...

    async def find_success(self, eid: str, idempotency_key: str) -> TrailEntry | None: ...

    async def list(
        self, eid: str, kind: TrailKind | None = None, limit: int = 100, offset: int = 0
    ) -> list[TrailEntry]: ...


class BlindFilter(Protocol):
    """盲判过滤 (第 4 节): 判断与留痕对外输出前一律经它转成视图, 盲判时隐藏 AI 的结论 (规格书第 9.5 节, 未作独立判断前
    不得显示 AI 结论). 返回判断或模型留痕的视图接口与对话工具都调用它 (第 10 节). 隐藏的字段置空并列入 hidden_fields,
    不往 Judgment 里填占位值. 本版为空实现 (services/blind.py), 判定与遮盖规则由降级段实现."""

    async def judgment(self, eid: str, j: Judgment) -> JudgmentView: ...

    async def trail(self, eid: str, entry: TrailEntry) -> TrailEntryView: ...
