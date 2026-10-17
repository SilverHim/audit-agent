"""Store 与 DatasetLoader 的 PostgreSQL / 文件系统实现, 方法签名与 contracts/ports.py 一致."""

import asyncio
import csv
import hashlib
import io
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar
from uuid import uuid4
from zoneinfo import ZoneInfo

import pdfplumber
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.adapters.xero_dataset import Snapshot, build_tables
from backend.adapters.xero_dataset import client_profile as xero_client_profile
from backend.contracts import records
from backend.contracts.errors import VersionConflict
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
from backend.contracts.ports import DatasetLoader
from backend.contracts.records import BankAccount, BankTransaction, DeliveryNote, SalesInvoice, TrialBalanceRow
from backend.core.config import get_settings
from backend.persistence import models as m
from backend.persistence.db import default_engine, transaction, writable
from backend.services.account_mapping import classify_by_name, with_category
from backend.services.ledger import PROFIT_AND_LOSS, opening_balances

T = TypeVar("T", bound=Versioned)


def now() -> datetime:
    return datetime.now(UTC)


def safe_child(root: Path, name: str) -> Path:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("Expected a single path component")
    result = (root / name).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("Path escapes storage root")
    return result


class Store:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self.engine = engine if engine is not None else default_engine()

    async def rows(self, eid: str, statement: Any) -> list[dict[str, Any]]:
        async with transaction(self.engine, eid) as conn:
            return [dict(row) for row in (await conn.execute(statement)).mappings()]


class PostgresFinancialDataStore(Store):
    async def query(self, eid: str, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        async with self.engine.begin() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(text("SET LOCAL statement_timeout = '10s'"))
            from backend.persistence.db import bind_engagement

            await bind_engagement(conn, eid)
            return [dict(r) for r in (await conn.execute(text(sql), {**params, "eid": eid})).mappings()]

    async def trial_balance(self, eid: str, period: Literal["current", "prior"]) -> list[TrialBalanceRow]:
        t = m.FINANCIAL["trial_balance"]
        return [
            TrialBalanceRow.model_validate({k: v for k, v in r.items() if k != "engagement_id"})
            for r in await self.rows(eid, select(t).where(t.c.period == period).order_by(t.c.account_code))
        ]

    async def gl_balance(self, eid: str, account_code: str, as_of: date) -> int:
        accounts = m.FINANCIAL["accounts"]
        async with transaction(self.engine, eid):
            account = await self.rows(eid, select(accounts).where(accounts.c.code == account_code))
            if not account:
                raise LookupError(account_code)
            prior = {r.account_code: r.balance_cents for r in await self.trial_balance(eid, "prior")}
            rows = await self.rows(eid, select(accounts))
            kinds = {r["code"]: r["type"] for r in rows}
            # 旧项目的科目没有标准分类 (迁移 0002 之前载入), 按名称补上
            categories = {r["code"]: r.get("category") or classify_by_name(r["name"], r["type"]) for r in rows}
            opening = 0 if kinds[account_code] in PROFIT_AND_LOSS else prior.get(account_code, 0)
            if categories[account_code] == "retained_earnings":  # 上期损益转入留存收益
                opening = opening_balances(prior, kinds, categories).get(account_code, 0)
            engagement = await PostgresEngagementStore(self.engine).get(eid)
            if engagement is None:
                raise LookupError(eid)
            j = m.FINANCIAL["journal_entries"]
            rows = await self.rows(
                eid,
                select(func.coalesce(func.sum(j.c.debit_cents - j.c.credit_cents), 0).label("amount")).where(
                    j.c.account_code == account_code, j.c.posting_date.between(engagement.period_start, as_of)
                ),
            )
            return opening + int(rows[0]["amount"])

    async def record(self, eid: str, source_type: SourceType, source_id: str) -> dict[str, Any] | None:
        names = {
            "trial_balance": "trial_balance",
            "journal_entry": "journal_entries",
            "sales_invoice": "sales_invoices",
            "delivery_note": "delivery_notes",
            "bank_account": "bank_accounts",
            "bank_transaction": "bank_transactions",
        }
        t = m.FINANCIAL[names[source_type]]
        stmt = (
            select(t).where(t.c.account_code == source_id, t.c.period == "current")
            if source_type == "trial_balance"
            else select(t).where(t.c.id == source_id)
        )
        rows = await self.rows(eid, stmt)
        return {k: v for k, v in rows[0].items() if k != "engagement_id"} if rows else None

    async def sales_invoices(self, eid: str, ids: Sequence[str]) -> list[SalesInvoice]:
        t = m.FINANCIAL["sales_invoices"]
        return [
            SalesInvoice.model_validate({k: v for k, v in r.items() if k != "engagement_id"})
            for r in await self.rows(eid, select(t).where(t.c.id.in_(ids)).order_by(t.c.id))
        ]

    async def delivery_notes(self, eid: str, invoice_ids: Sequence[str]) -> list[DeliveryNote]:
        t = m.FINANCIAL["delivery_notes"]
        return [
            DeliveryNote.model_validate({k: v for k, v in r.items() if k != "engagement_id"})
            for r in await self.rows(eid, select(t).where(t.c.invoice_id.in_(invoice_ids)).order_by(t.c.id))
        ]

    async def bank_accounts(self, eid: str) -> list[BankAccount]:
        t = m.FINANCIAL["bank_accounts"]
        return [
            BankAccount.model_validate({k: v for k, v in r.items() if k != "engagement_id"})
            for r in await self.rows(eid, select(t).order_by(t.c.id))
        ]

    async def bank_transactions(
        self, eid: str, bank_account_id: str, date_from: date, date_to: date
    ) -> list[BankTransaction]:
        t = m.FINANCIAL["bank_transactions"]
        return [
            BankTransaction.model_validate({k: v for k, v in r.items() if k != "engagement_id"})
            for r in await self.rows(
                eid,
                select(t)
                .where(t.c.bank_account_id == bank_account_id, t.c.txn_date.between(date_from, date_to))
                .order_by(t.c.txn_date, t.c.id),
            )
        ]

    async def contracts(self, eid: str, ids: Sequence[str] | None = None) -> list[Contract]:
        stmt = select(m.contracts).order_by(m.contracts.c.id)
        if ids is not None:
            stmt = stmt.where(m.contracts.c.id.in_(ids))
        result = []
        async with transaction(self.engine, eid):
            for row in await self.rows(eid, stmt):
                row.pop("engagement_id")
                terms = await self.rows(
                    eid,
                    select(m.contract_terms)
                    .where(m.contract_terms.c.contract_id == row["id"])
                    .order_by(m.contract_terms.c.id),
                )
                row["terms"] = [
                    {k: v for k, v in r.items() if k not in {"engagement_id", "contract_id"}} for r in terms
                ]
                result.append(Contract.model_validate(row))
        return result

    async def save_contract(self, eid: str, contract: Contract) -> None:
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            data = contract.model_dump(exclude={"terms", "evidence"})
            data["evidence"] = contract.model_dump(mode="json")["evidence"]
            stmt = insert(m.contracts).values(engagement_id=eid, **data)
            await conn.execute(stmt.on_conflict_do_update(index_elements=["engagement_id", "id"], set_=data))
            await conn.execute(
                delete(m.contract_terms).where(
                    m.contract_terms.c.engagement_id == eid, m.contract_terms.c.contract_id == contract.id
                )
            )
            for term in contract.terms:
                await conn.execute(
                    insert(m.contract_terms).values(engagement_id=eid, contract_id=contract.id, **term.model_dump())
                )

    async def documents(self, eid: str, type: DocumentType | None = None) -> list[DocumentRef]:
        stmt = select(m.documents).order_by(m.documents.c.file_name, m.documents.c.id)
        if type is not None:
            stmt = stmt.where(m.documents.c.type == type)
        return [
            DocumentRef.model_validate({k: v for k, v in r.items() if k not in {"engagement_id", "file_path"}})
            for r in await self.rows(eid, stmt)
        ]

    async def data_version(self, eid: str) -> int:
        rows = await self.rows(eid, select(m.dataset_loads.c.data_version))
        return int(rows[0]["data_version"]) if rows else 0


class PostgresAuditOutputStore(Store):
    async def put(self, eid: str, objs: Sequence[Versioned], *, actor: str) -> list[Versioned]:
        result = []
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            for obj in objs:
                t = m.OUTPUTS[type(obj).__name__]
                previous = await self.get(eid, type(obj), obj.id, obj.version)
                if previous is not None:
                    result.append(previous)
                    continue
                latest = await self.get(eid, type(obj), obj.id)
                if obj.version != (latest.version + 1 if latest else 1):
                    raise VersionConflict(f"{obj.id}: expected {(latest.version + 1) if latest else 1}")
                await conn.execute(
                    insert(t).values(
                        engagement_id=eid,
                        id=obj.id,
                        version=obj.version,
                        kind=type(obj).__name__,
                        data=obj.model_dump(mode="json"),
                        created_by=actor,
                        created_at=now(),
                    )
                )
                result.append(obj.model_copy(deep=True))
        return result

    async def get(self, eid: str, kind: type[T], id: str, version: int | None = None) -> T | None:
        t = m.OUTPUTS[kind.__name__]
        stmt = select(t.c.data).where(t.c.id == id, t.c.kind == kind.__name__).order_by(t.c.version.desc()).limit(1)
        if version is not None:
            stmt = stmt.where(t.c.version == version)
        rows = await self.rows(eid, stmt)
        return kind.model_validate(rows[0]["data"]) if rows else None

    async def latest(self, eid: str, kind: type[T], **filters: Any) -> list[T]:
        t = m.OUTPUTS[kind.__name__]
        rows = await self.rows(
            eid, select(t.c.data).where(t.c.kind == kind.__name__).distinct(t.c.id).order_by(t.c.id, t.c.version.desc())
        )
        objs = [kind.model_validate(r["data"]) for r in rows]
        return [obj for obj in objs if all(getattr(obj, key) == value for key, value in filters.items())]

    async def history(self, eid: str, kind: type[T], id: str) -> list[T]:
        t = m.OUTPUTS[kind.__name__]
        return [
            kind.model_validate(r["data"])
            for r in await self.rows(
                eid, select(t.c.data).where(t.c.id == id, t.c.kind == kind.__name__).order_by(t.c.version)
            )
        ]

    @asynccontextmanager
    async def transaction(self, eid: str) -> AsyncIterator[None]:
        async with transaction(self.engine, eid):
            yield


class FileEvidenceStore(Store):
    def __init__(self, root: Path, engine: AsyncEngine | None = None) -> None:
        super().__init__(engine)
        self.root = root

    async def save(
        self, eid: str, file_name: str, data: bytes, *, type: DocumentType, reliability: Reliability
    ) -> DocumentRef:
        safe_child(self.root, file_name)
        digest = hashlib.sha256(data).hexdigest()
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            existing = [d for d in await PostgresFinancialDataStore(self.engine).documents(eid) if d.sha256 == digest]
            if existing:
                return existing[0]

            def count_pages() -> int:
                with pdfplumber.open(io.BytesIO(data)) as pdf:
                    return len(pdf.pages)

            count = await asyncio.to_thread(count_pages)
            if count == 0:
                raise ValueError("Empty PDF")
            doc = DocumentRef(
                id=f"DOC-{digest[:12]}",
                type=type,
                file_name=file_name,
                sha256=digest,
                page_count=count,
                reliability=reliability,
                status="received",
                uploaded_at=now(),
            )
            directory = safe_child(safe_child(self.root, eid), "evidence")
            directory.mkdir(parents=True, exist_ok=True)
            path = safe_child(directory, digest + ".pdf")
            # Publish complete immutable content; a rolled-back registration leaves a reusable orphan.
            temporary = directory / f".{uuid4()}.tmp"
            temporary.write_bytes(data)
            try:
                with suppress(FileExistsError):
                    path.hardlink_to(temporary)
            finally:
                temporary.unlink()
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Evidence hash mismatch")
            await conn.execute(insert(m.documents).values(engagement_id=eid, file_path=str(path), **doc.model_dump()))
            return doc

    async def path(self, eid: str, document_id: str) -> Path:
        rows = await self.rows(eid, select(m.documents).where(m.documents.c.id == document_id))
        if not rows:
            raise FileNotFoundError(document_id)
        row = rows[0]
        path = Path(row["file_path"]).resolve()
        if not path.is_relative_to(safe_child(self.root, eid)):
            raise ValueError("Evidence path escapes project")
        if hashlib.sha256(await asyncio.to_thread(path.read_bytes)).hexdigest() != row["sha256"]:
            raise ValueError("Evidence hash mismatch")
        return path

    async def pages(self, eid: str, document_id: str) -> list[str]:
        path = await self.path(eid, document_id)

        def extract() -> list[str]:
            with pdfplumber.open(path) as pdf:
                return [page.extract_text() or "" for page in pdf.pages]

        return await asyncio.to_thread(extract)

    async def set_status(self, eid: str, document_id: str, status: DocumentStatus) -> None:
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            result = await conn.execute(
                update(m.documents)
                .where(m.documents.c.engagement_id == eid, m.documents.c.id == document_id)
                .values(status=status)
            )
            if not result.rowcount:
                raise FileNotFoundError(document_id)


FINANCIAL_ROWS: dict[str, type[BaseModel]] = {
    "accounts": records.Account,
    "trial_balance": TrialBalanceRow,
    "journal_entries": records.JournalEntry,
    "sales_invoices": SalesInvoice,
    "delivery_notes": DeliveryNote,
    "bank_accounts": BankAccount,
    "bank_transactions": BankTransaction,
}  # 财务表 → 行模型, 与 m.FINANCIAL 同序


Tables = tuple[Mapping[str, Sequence[BaseModel]], Sequence[Path]]  # (财务表 → 行, 合同 PDF 路径)


class TableLoader(Store):
    """各数据源载入器的公共部分: 一个事务内写入财务表并登记合同 PDF, 记 dataset_loads; 已载入则跳过.

    源数据在确认尚未载入之后才读取与转换 (build), 已载入的项目不再依赖源文件.
    """

    def __init__(self, engine: AsyncEngine | None = None, evidence_store: FileEvidenceStore | None = None) -> None:
        super().__init__(engine)
        self.evidence_store = evidence_store or FileEvidenceStore(get_settings().EVIDENCE_ROOT, self.engine)

    async def insert(self, eid: str, dataset: str, build: Callable[[], Tables]) -> None:
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            existing = await self.rows(eid, select(m.dataset_loads))
            if existing:
                if existing[0]["dataset"] != dataset:
                    raise ValueError("Engagement already has a different dataset")
                return
            tables, contracts = build()
            for name, table in m.FINANCIAL.items():
                rows = [dict(engagement_id=eid, **row.model_dump()) for row in tables.get(name, ())]
                if rows:
                    await conn.execute(insert(table), rows)
            for path in contracts:
                await self.evidence_store.save(
                    eid, path.name, path.read_bytes(), type=DocumentType.contract, reliability=Reliability.external_copy
                )
            await conn.execute(
                insert(m.dataset_loads).values(engagement_id=eid, dataset=dataset, data_version=1, loaded_at=now())
            )


class CsvDatasetLoader(TableLoader):
    """CSV 数据集 (如 Numbat): 每张财务表一个 CSV; 科目没有 category 列时按名称映射标准分类."""

    def __init__(
        self, root: Path, engine: AsyncEngine | None = None, evidence_store: FileEvidenceStore | None = None
    ) -> None:
        super().__init__(engine, evidence_store)
        self.root = root

    async def load(self, eid: str, dataset: str) -> None:
        directory = safe_child(self.root, dataset)

        def build() -> Tables:
            tables: dict[str, list[BaseModel]] = {}
            for name, kind in FINANCIAL_ROWS.items():
                with (directory / f"{name}.csv").open(newline="", encoding="utf-8") as stream:
                    rows = [
                        kind.model_validate({k: (v if v != "" else None) for k, v in r.items()})
                        for r in csv.DictReader(stream)
                    ]
                tables[name] = [with_category(r) if isinstance(r, records.Account) else r for r in rows]
            return tables, sorted((directory / "contracts").glob("*.pdf"))

        await self.insert(eid, dataset, build)

    def planning_presets(self, dataset: str) -> tuple[list[AuditObjective], list[RiskItem]]:
        return read_planning(safe_child(self.root, dataset) / "planning")

    def client_profile(self, dataset: str) -> ClientProfile:
        return ClientProfile.model_validate_json((safe_child(self.root, dataset) / "client_profile.json").read_text())


class XeroDatasetLoader(TableLoader):
    """Xero 快照 (xero pull 的输出目录): dataset 为 "xero/{快照目录名}", 对应 {root}/{快照目录名}.

    规划预设取通用模板 (planning 目录), 客户画像取快照的组织信息. 映射见 adapters.xero_dataset.
    """

    PREFIX = "xero/"

    def __init__(
        self,
        root: Path,
        planning: Path,
        engine: AsyncEngine | None = None,
        evidence_store: FileEvidenceStore | None = None,
    ) -> None:
        super().__init__(engine, evidence_store)
        self.root = root  # 快照目录的上级, 如 data/external/xero/api
        self.planning = planning  # 通用规划预设目录

    def directory(self, dataset: str) -> Path:
        """数据集名 → 快照目录 (不查是否存在); 名称不合法抛 ValueError."""
        if not dataset.startswith(self.PREFIX):
            raise ValueError(f"不是 Xero 数据集: {dataset}")
        return safe_child(self.root, dataset.removeprefix(self.PREFIX))

    def snapshot(self, dataset: str) -> Snapshot:
        """快照; 目录不存在抛 FileNotFoundError (建项目接口据此报 NOT_FOUND)."""
        directory = self.directory(dataset)
        if not directory.is_dir():
            raise FileNotFoundError(f"没有 Xero 快照 {dataset}")
        return Snapshot(directory)

    async def load(self, eid: str, dataset: str) -> None:
        self.directory(dataset)  # 名称先校验; 快照是否存在等确认未载入后再查
        await self.insert(eid, dataset, lambda: (build_tables(self.snapshot(dataset)), ()))

    def planning_presets(self, dataset: str) -> tuple[list[AuditObjective], list[RiskItem]]:
        return read_planning(self.planning)

    def client_profile(self, dataset: str) -> ClientProfile:
        return xero_client_profile(self.snapshot(dataset))


class RoutingDatasetLoader:
    """按数据集名的前缀选载入器 (如 "xero/" 走 Xero), 其余走默认的 CSV 载入器."""

    def __init__(self, default: DatasetLoader, by_prefix: Mapping[str, DatasetLoader]) -> None:
        self.default = default
        self.by_prefix = dict(by_prefix)

    def pick(self, dataset: str) -> DatasetLoader:
        return next((loader for prefix, loader in self.by_prefix.items() if dataset.startswith(prefix)), self.default)

    async def load(self, eid: str, dataset: str) -> None:
        await self.pick(dataset).load(eid, dataset)

    def planning_presets(self, dataset: str) -> tuple[list[AuditObjective], list[RiskItem]]:
        return self.pick(dataset).planning_presets(dataset)

    def client_profile(self, dataset: str) -> ClientProfile:
        return self.pick(dataset).client_profile(dataset)


def read_planning(directory: Path) -> tuple[list[AuditObjective], list[RiskItem]]:
    """读目录下的 audit_objectives.json 与 risk_register.json."""
    return (
        [AuditObjective.model_validate(r) for r in json.loads((directory / "audit_objectives.json").read_text())],
        [RiskItem.model_validate(r) for r in json.loads((directory / "risk_register.json").read_text())],
    )


class PostgresEngagementStore(Store):
    async def create(
        self, *, owner_id: str, period_start: date, period_end: date, dataset: str, profile: ClientProfile
    ) -> Engagement:
        if period_end < period_start:
            raise ValueError("period_end precedes period_start")
        async with transaction(self.engine, "") as conn:
            await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('engagement_ids', 0))"))
            year = now().astimezone(ZoneInfo("Australia/Sydney")).year
            rows = (
                (await conn.execute(select(m.engagements.c.id).where(m.engagements.c.id.like(f"ENG-{year}-%"))))
                .scalars()
                .all()
            )
            number = max((int(r.rsplit("-", 1)[1]) for r in rows), default=0) + 1
            existing = await self.client_profile(profile.id, profile.version)
            if existing is None:
                await self.save_profile(profile)
            elif existing != profile:
                raise VersionConflict(profile.id)
            item = Engagement(
                id=f"ENG-{year}-{number:03d}",
                client_id=profile.id,
                period_start=period_start,
                period_end=period_end,
                dataset=dataset,
                owner_id=owner_id,
                client_profile_version=profile.version,
                phase=Phase.P0_Acceptance,
                created_at=now(),
                updated_at=now(),
            )
            await conn.execute(insert(m.engagements).values(**item.model_dump()))
            return item

    async def get(self, eid: str) -> Engagement | None:
        rows = await self.rows(eid, select(m.engagements).where(m.engagements.c.id == eid))
        return Engagement.model_validate(rows[0]) if rows else None

    async def update(
        self,
        eid: str,
        *,
        phase: Phase | None = None,
        locked: bool | None = None,
        version_snapshot: dict[str, str] | None = None,
        client_profile_version: int | None = None,
    ) -> None:
        changes = {
            k: v
            for k, v in dict(
                phase=phase,
                locked=locked,
                version_snapshot=version_snapshot,
                client_profile_version=client_profile_version,
            ).items()
            if v is not None
        }
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            await conn.execute(
                update(m.engagements).where(m.engagements.c.id == eid).values(**changes, updated_at=now())
            )

    async def client_profile(self, client_id: str, version: int | None = None) -> ClientProfile | None:
        # Global profiles join the current project transaction without changing its RLS scope.
        from backend.persistence.db import _active

        active = _active.get()
        eid = active[1] if active and active[0] is self.engine and active[3] is asyncio.current_task() else ""
        stmt = (
            select(m.client_profiles.c.data)
            .where(m.client_profiles.c.id == client_id)
            .order_by(m.client_profiles.c.version.desc())
            .limit(1)
        )
        if version is not None:
            stmt = stmt.where(m.client_profiles.c.version == version)
        rows = await self.rows(eid, stmt)
        return ClientProfile.model_validate(rows[0]["data"]) if rows else None

    async def save_profile(self, profile: ClientProfile) -> ClientProfile:
        from backend.persistence.db import _active

        active = _active.get()
        eid = active[1] if active and active[0] is self.engine and active[3] is asyncio.current_task() else ""
        async with transaction(self.engine, eid) as conn:
            if eid:
                await writable(conn, eid)
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "profile:" + profile.id}
            )
            previous = await self.client_profile(profile.id)
            if profile.version != (previous.version + 1 if previous else 1):
                raise VersionConflict(profile.id)
            await conn.execute(
                insert(m.client_profiles).values(
                    id=profile.id, version=profile.version, data=profile.model_dump(mode="json"), updated_at=now()
                )
            )
        return profile.model_copy(deep=True)

    async def list(self, owner_id: str | None = None) -> list[Engagement]:
        stmt = select(m.engagements).order_by(m.engagements.c.id)
        if owner_id is not None:
            stmt = stmt.where(m.engagements.c.owner_id == owner_id)
        return [Engagement.model_validate(row) for row in await self.rows("", stmt)]


class PostgresUserStore(Store):
    async def by_email(self, email: str) -> tuple[User, str] | None:
        rows = await self.rows("", select(m.users).where(m.users.c.email == email))
        if not rows:
            return None
        row = rows[0]
        return User(id=row["id"], email=row["email"], full_name=row["full_name"]), row["password_hash"]

    async def get(self, user_id: str) -> User | None:
        rows = await self.rows("", select(m.users).where(m.users.c.id == user_id))
        return User(**{k: rows[0][k] for k in ("id", "email", "full_name")}) if rows else None
