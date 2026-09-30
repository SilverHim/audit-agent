"""Store 与 DatasetLoader 的 PostgreSQL / 文件系统实现 (B 负责), 方法签名与 contracts/ports.py 一致."""

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import date
from pathlib import Path
from typing import Any, Literal, TypeVar

from backend.contracts.models import (
    AuditObjective,
    ClientProfile,
    Contract,
    DocumentRef,
    DocumentStatus,
    DocumentType,
    Engagement,
    Phase,
    Reliability,
    RiskItem,
    SourceType,
    User,
    Versioned,
)
from backend.contracts.records import BankAccount, BankTransaction, DeliveryNote, SalesInvoice, TrialBalanceRow

T = TypeVar("T", bound=Versioned)


class PostgresFinancialDataStore:
    """FinancialDataStore 的 PostgreSQL 实现."""

    async def query(self, eid: str, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def trial_balance(self, eid: str, period: Literal["current", "prior"]) -> list[TrialBalanceRow]:
        raise NotImplementedError

    async def gl_balance(self, eid: str, account_code: str, as_of: date) -> int:
        raise NotImplementedError

    async def record(self, eid: str, source_type: SourceType, source_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    async def sales_invoices(self, eid: str, ids: Sequence[str]) -> list[SalesInvoice]:
        raise NotImplementedError

    async def delivery_notes(self, eid: str, invoice_ids: Sequence[str]) -> list[DeliveryNote]:
        raise NotImplementedError

    async def bank_accounts(self, eid: str) -> list[BankAccount]:
        raise NotImplementedError

    async def bank_transactions(
        self, eid: str, bank_account_id: str, date_from: date, date_to: date
    ) -> list[BankTransaction]:
        raise NotImplementedError

    async def contracts(self, eid: str, ids: Sequence[str] | None = None) -> list[Contract]:
        raise NotImplementedError

    async def save_contract(self, eid: str, contract: Contract) -> None:
        raise NotImplementedError

    async def documents(self, eid: str, type: DocumentType | None = None) -> list[DocumentRef]:
        raise NotImplementedError

    async def data_version(self, eid: str) -> int:
        raise NotImplementedError


class PostgresAuditOutputStore:
    """AuditOutputStore 的 PostgreSQL 实现."""

    async def put(self, eid: str, objs: Sequence[Versioned], *, actor: str) -> list[Versioned]:
        raise NotImplementedError

    async def get(self, eid: str, kind: type[T], id: str, version: int | None = None) -> T | None:
        raise NotImplementedError

    async def latest(self, eid: str, kind: type[T], **filters: Any) -> list[T]:
        raise NotImplementedError

    async def history(self, eid: str, kind: type[T], id: str) -> list[T]:
        raise NotImplementedError

    def transaction(self, eid: str) -> AbstractAsyncContextManager[None]:
        raise NotImplementedError


class FileEvidenceStore:
    """EvidenceStore 的文件系统实现: 原件存在 root/{eid}/evidence/ 下."""

    def __init__(self, root: Path) -> None:
        self.root = root  # 原件根目录, 即 EVIDENCE_ROOT

    async def save(
        self, eid: str, file_name: str, data: bytes, *, type: DocumentType, reliability: Reliability
    ) -> DocumentRef:
        raise NotImplementedError

    async def path(self, eid: str, document_id: str) -> Path:
        raise NotImplementedError

    async def pages(self, eid: str, document_id: str) -> list[str]:
        raise NotImplementedError

    async def set_status(self, eid: str, document_id: str, status: DocumentStatus) -> None:
        raise NotImplementedError


class CsvDatasetLoader:
    """DatasetLoader 的实现: 读数据集目录下的 CSV 与 JSON."""

    def __init__(self, root: Path) -> None:
        self.root = root  # 数据集根目录, 即 DATASET_ROOT

    async def load(self, eid: str, dataset: str) -> None:
        raise NotImplementedError

    def planning_presets(self, dataset: str) -> tuple[list[AuditObjective], list[RiskItem]]:
        raise NotImplementedError

    def client_profile(self, dataset: str) -> ClientProfile:
        raise NotImplementedError


class PostgresEngagementStore:
    """EngagementStore 的 PostgreSQL 实现."""

    async def create(
        self,
        *,
        owner_id: str,
        period_start: date,
        period_end: date,
        dataset: str,
        profile: ClientProfile,
    ) -> Engagement:
        raise NotImplementedError

    async def get(self, eid: str) -> Engagement | None:
        raise NotImplementedError

    async def update(
        self,
        eid: str,
        *,
        phase: Phase | None = None,
        locked: bool | None = None,
        version_snapshot: dict[str, str] | None = None,
    ) -> None:
        raise NotImplementedError

    async def client_profile(self, client_id: str, version: int | None = None) -> ClientProfile | None:
        raise NotImplementedError

    async def save_profile(self, profile: ClientProfile) -> ClientProfile:
        raise NotImplementedError

    async def list(self, owner_id: str | None = None) -> list[Engagement]:
        raise NotImplementedError


class PostgresUserStore:
    """UserStore 的 PostgreSQL 实现."""

    async def by_email(self, email: str) -> tuple[User, str] | None:
        raise NotImplementedError

    async def get(self, user_id: str) -> User | None:
        raise NotImplementedError
