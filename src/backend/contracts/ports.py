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
from backend.contracts.data import DataCheck, DataReadiness, TableKind, TableStats, TableVersion, XeroSource
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import (
    AbnRecord,
    AsicCompanyRecord,
    AuditObjective,
    ClientProfile,
    Contract,
    DocumentRef,
    DocumentStatus,
    DocumentType,
    Engagement,
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
    BankAccount,
    BankTransaction,
    DeliveryNote,
    SalesInvoice,
    TrialBalanceRow,
)
from backend.contracts.services import RuleResult

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

    async def sales_invoices(self, eid: str, ids: Sequence[str]) -> list[SalesInvoice]: ...

    async def delivery_notes(self, eid: str, invoice_ids: Sequence[str]) -> list[DeliveryNote]: ...

    async def bank_accounts(self, eid: str) -> list[BankAccount]: ...

    async def bank_transactions(
        self, eid: str, bank_account_id: str, date_from: date, date_to: date
    ) -> list[BankTransaction]: ...

    async def contracts(self, eid: str, ids: Sequence[str] | None = None) -> list[Contract]: ...

    async def save_contract(self, eid: str, contract: Contract) -> None:
        """DOC-04 写入, 按 id 覆盖合同与条款."""
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
        self, eid: str, file_name: str, data: bytes, *, type: DocumentType, reliability: Reliability
    ) -> DocumentRef:
        """存原件, 算 sha256, 登记 documents; 同一哈希重复保存返回已有记录."""
        ...

    async def path(self, eid: str, document_id: str) -> Path: ...

    async def pages(self, eid: str, document_id: str) -> list[str]:
        """pdfplumber 文本层, 下标 0 为第 1 页."""
        ...

    async def set_status(self, eid: str, document_id: str, status: DocumentStatus) -> None:
        """在 AuditOutputStore.transaction 块内调用时加入该事务."""
        ...


class DatasetLoader(Protocol):
    """合成数据集的载入 (B 实现): 财务数据入库, 提供预置的计划数据与客户画像."""

    async def load(self, eid: str, dataset: str) -> None:
        """PLN-10 替代: 载入财务表, 合同 PDF 经 EvidenceStore.save 登记; 回函 PDF 不登记. 已载入则跳过."""
        ...

    def planning_presets(self, dataset: str) -> tuple[list[AuditObjective], list[RiskItem]]: ...

    def client_profile(self, dataset: str) -> ClientProfile: ...

    def datasets(self) -> list[str]:
        """可以建项目的数据集名, 按名称排序; 新建项目时列给审计师选客户."""
        ...


class DataStore(Protocol):
    """数据管理的存储 (B 实现): 数据版本与就绪确认, 账表统计. 表见开发接口文档第 9 节."""

    async def readiness(self, eid: str) -> DataReadiness:
        """数据版本与就绪确认; 没接入过数据时版本为 0, 未确认."""
        ...

    async def confirm_ready(self, eid: str, actor: str) -> DataReadiness:
        """记审计师确认数据已齐 (已确认的不改, 保留第一次的人与时间), 返回确认后的状态.

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
    """ABN 与 ASIC 登记查询. mock 读数据集目录下的 registry/, 目录在构造时注入."""

    async def abn(self, abn: str) -> AbnRecord | None: ...

    async def asic_company(self, acn: str) -> AsicCompanyRecord | None: ...


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
    """函证平台: 发询证函, 收回函. mock 读数据集目录下的 confirmations/{account_no}.pdf."""

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
