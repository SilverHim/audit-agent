"""存储、外部系统与 LLM 的接口，定义见 开发接口文档.md 第 4、5 节。

实现方：Store、DatasetLoader、RuleEngine、AuditTrail、mock 适配器由 B 实现；LLMClient 由 A 实现。
上层只依赖这里的 Protocol，具体实现在组装根（main.py）注入。
"""

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel

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
    async def query(self, eid: str, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        """只读事务、超时 10 s；:eid 自动绑定。只给 RuleEngine 用。"""
        ...

    async def trial_balance(self, eid: str, period: Literal["current", "prior"]) -> list[TrialBalanceRow]: ...

    async def gl_balance(self, eid: str, account_code: str, as_of: date) -> int: ...

    async def record(self, eid: str, source_type: SourceType, source_id: str) -> dict[str, Any] | None:
        """取一行明细；trial_balance 的 source_id 为 account_code。"""
        ...

    async def sales_invoices(self, eid: str, ids: Sequence[str]) -> list[SalesInvoice]: ...

    async def delivery_notes(self, eid: str, invoice_ids: Sequence[str]) -> list[DeliveryNote]: ...

    async def bank_accounts(self, eid: str) -> list[BankAccount]: ...

    async def bank_transactions(
        self, eid: str, bank_account_id: str, date_from: date, date_to: date
    ) -> list[BankTransaction]: ...

    async def contracts(self, eid: str, ids: Sequence[str] | None = None) -> list[Contract]: ...

    async def save_contract(self, eid: str, contract: Contract) -> None:
        """DOC-04 写入，按 id 覆盖合同与条款。"""
        ...

    async def documents(self, eid: str, type: DocumentType | None = None) -> list[DocumentRef]: ...

    async def data_version(self, eid: str) -> int: ...


class AuditOutputStore(Protocol):
    async def put(self, eid: str, objs: Sequence[Versioned], *, actor: str) -> list[Versioned]:
        """按顺序追加新版本行，返回存储中的对象。

        同 (id, version) 已存在：不比较内容，跳过并返回已存对象；否则 version 须为已有最大值 +1
        （新 id 为 1），不符抛 VersionConflict；项目 locked 抛 EngagementLocked。
        """
        ...

    async def get(self, eid: str, kind: type[T], id: str, version: int | None = None) -> T | None: ...

    async def latest(self, eid: str, kind: type[T], **filters: Any) -> list[T]:
        """每个 id 的最新版本；filters 按顶层字段相等匹配。"""
        ...

    async def history(self, eid: str, kind: type[T], id: str) -> list[T]: ...

    def transaction(self, eid: str) -> AbstractAsyncContextManager[None]:
        """块内的 put 共用一个事务；嵌套调用加入外层事务。"""
        ...


class EvidenceStore(Protocol):
    async def save(
        self, eid: str, file_name: str, data: bytes, *, type: DocumentType, reliability: Reliability
    ) -> DocumentRef:
        """存原件、算 sha256、登记 documents；同一哈希重复保存返回已有记录。"""
        ...

    async def path(self, eid: str, document_id: str) -> Path: ...

    async def pages(self, eid: str, document_id: str) -> list[str]:
        """pdfplumber 文本层，下标 0 为第 1 页。"""
        ...

    async def set_status(self, eid: str, document_id: str, status: DocumentStatus) -> None:
        """在 AuditOutputStore.transaction 块内调用时加入该事务。"""
        ...


class DatasetLoader(Protocol):
    async def load(self, eid: str, dataset: str) -> None:
        """PLN-10 替代：载入财务表，合同 PDF 经 EvidenceStore.save 登记；回函 PDF 不登记。已载入则跳过。"""
        ...

    def planning_presets(self, dataset: str) -> tuple[list[AuditObjective], list[RiskItem]]: ...

    def client_profile(self, dataset: str) -> ClientProfile: ...


class EngagementStore(Protocol):
    """跨项目表，不带 eid 前缀参数。"""

    async def create(
        self,
        *,
        owner_id: str,
        client_name: str,
        abn: str,
        period_start: date,
        period_end: date,
        dataset: str,
        profile: ClientProfile,
    ) -> Engagement:
        """取号 ENG-{yyyy}-{nnn}；client_id = profile.id；画像写入 client_profiles；phase = P0_Acceptance。"""
        ...

    async def get(self, eid: str) -> Engagement | None: ...

    async def update(
        self,
        eid: str,
        *,
        phase: Phase | None = None,
        locked: bool | None = None,
        version_snapshot: dict[str, str] | None = None,
    ) -> None:
        """同时刷新 updated_at；locked 置真后不可回退；在 AuditOutputStore.transaction 块内调用时加入该事务。"""
        ...

    async def client_profile(self, client_id: str, version: int | None = None) -> ClientProfile | None: ...

    async def list(self, owner_id: str | None = None) -> list[Engagement]:
        """owner_id 为空列全部，供 recover。"""
        ...


class UserStore(Protocol):
    async def by_email(self, email: str) -> tuple[User, str] | None:
        """返回 (用户, password_hash)，登录用。"""
        ...

    async def get(self, user_id: str) -> User | None: ...


class RegistrySource(Protocol):
    """mock 读数据集目录下的 registry/，目录在构造时注入。"""

    async def abn(self, abn: str) -> AbnRecord | None: ...

    async def asic_company(self, acn: str) -> AsicCompanyRecord | None: ...


class ConfirmationRequest(Model):
    bank_account_id: str
    bank_name: str
    bsb: str
    account_no: str
    as_of: date


class ConfirmationReceipt(Model):
    sent_at: datetime
    replied_at: date | None
    reply_file_name: str | None
    reply_bytes: bytes | None
    receipt_channel: ReceiptChannel | None


class ConfirmationChannel(Protocol):
    """mock 读数据集目录下的 confirmations/{account_no}.pdf。"""

    async def submit(self, eid: str, req: ConfirmationRequest, idempotency_key: str) -> ConfirmationReceipt:
        """同一幂等键重复调用返回同一回执。"""
        ...


class LLMMessage(Model):
    role: Literal["user", "assistant"]
    content: str


class LLMRequest(Model):
    procedure_id: str
    run_id: str
    system: str
    messages: list[LLMMessage]
    max_tokens: int
    temperature: float = 0
    fixture_key: str = "default"


class LLMResponse(Model, Generic[T_out]):
    output: T_out | None
    raw: str
    model: str
    input_tokens: int
    output_tokens: int
    error: str | None = None


class LLMClient(Protocol):
    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]: ...


class RuleEngine(Protocol):
    async def run(
        self, eid: str, template: ProcedureTemplate, run: ProcedureRun, params: Mapping[str, Any]
    ) -> RuleResult: ...


class AuditTrail(Protocol):
    """独立事务写入，不随业务事务回滚。"""

    async def record(self, entry: TrailEntry) -> str: ...

    async def get(self, eid: str, id: str) -> TrailEntry | None: ...

    async def find_success(self, eid: str, idempotency_key: str) -> TrailEntry | None: ...

    async def list(
        self, eid: str, kind: TrailKind | None = None, limit: int = 100, offset: int = 0
    ) -> list[TrailEntry]: ...
