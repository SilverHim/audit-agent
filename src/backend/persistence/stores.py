"""Store 与 DatasetLoader 的 PostgreSQL / 文件系统实现, 方法签名与 contracts/ports.py 一致."""

import asyncio
import csv
import hashlib
import io
import json
import re
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime
from pathlib import Path, PurePath
from typing import Any, Literal, TypeVar, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import pdfplumber
from pydantic import BaseModel
from sqlalchemy import delete, exists, func, null, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from backend.adapters.xero_dataset import Snapshot, build_tables, snapshot_period
from backend.adapters.xero_dataset import client_profile as xero_client_profile
from backend.adapters.xero_supplements import DemoTable, Supplements
from backend.contracts import records
from backend.contracts.data import ContractVersion, DataInput, TableKind
from backend.contracts.errors import AbnTaken, VersionConflict
from backend.contracts.models import (
    AgentId,
    AuditObjective,
    ClientEntry,
    ClientProfile,
    Contract,
    DocumentChannel,
    DocumentFailedStep,
    DocumentRef,
    DocumentStatus,
    DocumentType,
    Engagement,
    LlmSettings,
    LlmSlot,
    Phase,
    ProfileRecord,
    ProfileSource,
    Reliability,
    RiskItem,
    SourceType,
    User,
    Versioned,
    WorkingLanguage,
)
from backend.contracts.ports import DatasetLoader
from backend.contracts.records import (
    Account,
    AdjustmentEntrySummary,
    BankAccount,
    BankTransaction,
    DatasetTraits,
    DeliveryNote,
    Employee,
    JournalEntry,
    SalesInvoice,
    TrialBalanceRow,
)
from backend.core.config import get_settings
from backend.persistence import models as m
from backend.persistence.db import default_engine, transaction, writable
from backend.services.abn import normalize_abn
from backend.services.account_mapping import classify_by_name, with_category
from backend.services.extraction_edits import term_position
from backend.services.ledger import PROFIT_AND_LOSS, opening_balances

T = TypeVar("T", bound=Versioned)


def now() -> datetime:
    return datetime.now(UTC)


def subdirectories(root: Path, *required: str) -> list[str]:
    """root 下含 required 中全部文件的子目录名, 按名称排序; 以 . 开头的 (如拉取中的 .{名}.partial) 不算.
    root 不存在时为空."""
    if not root.is_dir():
        return []
    return sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".") and all((d / name).is_file() for name in required)
    )


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
            "supplier_bill": "supplier_bills",
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

    async def journal_entries(
        self, eid: str, *, entry_nos: Sequence[str] | None = None, source_docs: Sequence[str] | None = None
    ) -> list[JournalEntry]:
        if entry_nos is None and source_docs is None:
            raise ValueError("journal_entries 须给 entry_nos 或 source_docs, 不取全表")
        t = m.FINANCIAL["journal_entries"]
        conditions = []
        if entry_nos is not None:
            conditions.append(t.c.entry_no.in_(list(entry_nos)))
        if source_docs is not None:
            conditions.append(t.c.source_doc.in_(list(source_docs)))
        return [
            JournalEntry.model_validate({k: v for k, v in r.items() if k != "engagement_id"})
            for r in await self.rows(eid, select(t).where(or_(*conditions)).order_by(t.c.id))
        ]

    async def accounts(self, eid: str, codes: Sequence[str] | None = None) -> list[Account]:
        t = m.FINANCIAL["accounts"]
        stmt = select(t).order_by(t.c.code)
        if codes is not None:
            stmt = stmt.where(t.c.code.in_(list(codes)))
        return [
            Account.model_validate({k: v for k, v in r.items() if k != "engagement_id"})
            for r in await self.rows(eid, stmt)
        ]

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

    async def employees(self, eid: str) -> list[Employee]:
        t = m.FINANCIAL["employees"]
        return [
            Employee.model_validate({k: v for k, v in r.items() if k != "engagement_id"})
            for r in await self.rows(eid, select(t).order_by(t.c.employee_id))
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
                    {k: v for k, v in r.items() if k not in {"engagement_id", "contract_id"}}
                    for r in sorted(terms, key=lambda r: term_position(r["id"]))
                ]
                result.append(Contract.model_validate({k: v for k, v in row.items() if k in Contract.model_fields}))
        return result

    async def save_contract(
        self,
        eid: str,
        contract: Contract,
        *,
        changed_by: str = AgentId.A3,
        field: str | None = None,
        reason: str | None = None,
    ) -> None:
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            await write_contract(conn, eid, contract, changed_by=changed_by, field=field, reason=reason)

    async def contract_history(self, eid: str, contract_id: str) -> list[ContractVersion]:
        t = m.extraction_versions
        statement = (
            select(t.c.version, t.c.data, t.c.field, t.c.reason, t.c.changed_by, t.c.changed_at)
            .where(t.c.target_type == "contract", t.c.target_id == contract_id)
            .order_by(t.c.version)
        )
        return [
            ContractVersion.model_validate({**{k: v for k, v in r.items() if k != "data"}, "contract": r["data"]})
            for r in await self.rows(eid, statement)
        ]

    async def documents(
        self, eid: str, type: DocumentType | None = None, related_id: str | None = None
    ) -> list[DocumentRef]:
        t = m.documents
        stmt = select(t).order_by(t.c.file_name, t.c.id)
        if type is not None:
            stmt = stmt.where(t.c.type == type)
        if related_id is not None:
            stmt = stmt.where(t.c.related_ids.contains([related_id]))  # JSONB 包含 (@>): 数组里有这个 ID
        return [document_ref(r) for r in await self.rows(eid, stmt)]

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
        safe_child(self.root, file_name)
        digest = hashlib.sha256(data).hexdigest()
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            existing = [d for d in await PostgresFinancialDataStore(self.engine).documents(eid) if d.sha256 == digest]
            if existing:
                return existing[0]
            pdf = media_type == "application/pdf"

            def count_pages() -> int:
                """PDF 页数; 读不出 (文件损坏) 记 0: 原件照样登记, 由读取的一方报错 (上传的文件转人工处理)."""
                try:
                    with pdfplumber.open(io.BytesIO(data)) as document:
                        return len(document.pages)
                except Exception:
                    return 0

            count = await asyncio.to_thread(count_pages) if pdf else 1  # 非 PDF 不分页, 记 1
            doc = DocumentRef(
                id=f"DOC-{digest[:12]}",
                type=type,
                file_name=file_name,
                sha256=digest,
                page_count=count,
                reliability=reliability,
                status=status,
                uploaded_at=now(),
                media_type=media_type,
                channel=channel,
                uploaded_by=uploaded_by,
            )
            directory = safe_child(safe_child(self.root, eid), "evidence")
            directory.mkdir(parents=True, exist_ok=True)
            suffix = ".pdf" if pdf else PurePath(file_name).suffix.lower()  # 存档文件名为哈希加原扩展名
            path = safe_child(directory, digest + suffix)
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
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            return await write_document(
                conn, eid, document_id, type=type, reliability=reliability, status=status, failed_step=failed_step
            )

    async def update_details(
        self,
        eid: str,
        document_id: str,
        *,
        reliability: Reliability | None,
        document_date: date | None,
        related_ids: Sequence[str] | None,
    ) -> DocumentRef:
        values: dict[str, Any] = {}
        if reliability is not None:
            values["reliability"] = reliability
        if document_date is not None:
            values["document_date"] = document_date
        if related_ids is not None:
            values["related_ids"] = list(related_ids)
        t = m.documents
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            where = (t.c.engagement_id == eid) & (t.c.id == document_id)
            if values:
                result = await conn.execute(update(t).where(where).values(**values))
                if not result.rowcount:
                    raise FileNotFoundError(document_id)
            row = (await conn.execute(select(t).where(where))).mappings().first()
            if row is None:
                raise FileNotFoundError(document_id)
            return document_ref(row)


FINANCIAL_ROWS: dict[str, type[BaseModel]] = {
    "accounts": records.Account,
    "trial_balance": TrialBalanceRow,
    "journal_entries": records.JournalEntry,
    "sales_invoices": SalesInvoice,
    "supplier_bills": records.SupplierBill,
    "delivery_notes": DeliveryNote,
    "bank_accounts": BankAccount,
    "bank_transactions": BankTransaction,
}  # 数据集载入的财务表 → 行模型, 与 m.FINANCIAL 同序; 工资册不随数据集载入, 不在其中
OPTIONAL_CSV = frozenset({"supplier_bills"})  # CSV 数据集可以没有的表 (应付账单, 需求 R178), 其余缺文件报错


Tables = tuple[Mapping[str, Sequence[BaseModel]], Sequence[Path]]  # (财务表 → 行, 合同 PDF 路径)


DATASET_READY_BY = "dataset"  # 数据集载入视同确认就绪时, dataset_loads.ready_by 记的值


class TableLoader(Store):
    """各数据源载入器的公共部分: 一个事务内写入财务表并登记合同 PDF, 数据版本 +1 并视同已确认就绪; 已载入则跳过.

    已载入看该项目有没有财务表行 (数据集载入的八张表, 需求 R268, R178), 不看 dataset_loads 有没有行: 导入, 确认
    就绪, 抽取结果修改与补登记都会先建版本 0 的 dataset_loads 行, 之前按 "有行" 判断时种子载入会被静默跳过.
    - 没有 dataset_loads 行: 建行, 数据版本 1, 视同就绪.
    - 有行而还没有财务表行: 在该行上数据版本 +1; 还没确认就绪的视同就绪, 已由审计师确认的保留原记录 (确认的人,
      时间, 理由与未通过项都不改); loaded_at (第一次接入数据的时间) 不改.
    - 已有财务表行: 不写 (幂等). 行上的数据集与要载入的不同时抛 ValueError.
    源数据在确认尚未载入之后才读取与转换 (build), 已载入的项目不再依赖源文件.
    """

    def __init__(self, engine: AsyncEngine | None = None, evidence_store: FileEvidenceStore | None = None) -> None:
        super().__init__(engine)
        self.evidence_store = evidence_store or FileEvidenceStore(get_settings().EVIDENCE_ROOT, self.engine)

    async def insert(self, eid: str, dataset: str, build: Callable[[], Tables]) -> None:
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            existing, loaded = await self.load_state(conn, eid)
            if existing is not None and existing["dataset"] != dataset:
                raise ValueError("Engagement already has a different dataset")
            if loaded:
                return
            tables, contracts = build()
            t = m.dataset_loads
            for name, table in m.FINANCIAL.items():
                rows = [dict(engagement_id=eid, **row.model_dump()) for row in tables.get(name, ())]
                if rows:
                    await conn.execute(insert(table), rows)
            for path in contracts:
                await self.evidence_store.save(
                    eid, path.name, path.read_bytes(), type=DocumentType.contract, reliability=Reliability.external_copy
                )
            # 数据集是整套预置数据 (种子, 测试与演示项目), 载入即视同审计师已确认数据已齐, P1 不再等待
            loaded_at = now()
            version = 1
            if existing is None:
                await conn.execute(
                    insert(t).values(
                        engagement_id=eid,
                        dataset=dataset,
                        data_version=version,
                        loaded_at=loaded_at,
                        ready_at=loaded_at,
                        ready_by=DATASET_READY_BY,
                    )
                )
            else:
                values: dict[str, Any] = {"data_version": t.c.data_version + 1}
                if existing["ready_at"] is None:
                    values |= {"ready_at": loaded_at, "ready_by": DATASET_READY_BY}
                bumped = update(t).where(t.c.engagement_id == eid).values(**values).returning(t.c.data_version)
                version = (await conn.execute(bumped)).scalar_one()
            await record_changes(conn, eid, version, DataInput)  # 整套载入: 这一版变了全部各类数据

    async def load_state(self, conn: AsyncConnection, eid: str) -> tuple[Mapping[str, Any] | None, bool]:
        """项目的 dataset_loads 行 (没有为 None) 与是否已有数据集载入的财务表行 (已载入的判断, 见类说明)."""
        t = m.dataset_loads
        row = (await conn.execute(select(t).where(t.c.engagement_id == eid))).mappings().first()
        tables = [exists().where(m.FINANCIAL[name].c.engagement_id == eid) for name in FINANCIAL_ROWS]
        return (dict(row) if row is not None else None), bool((await conn.execute(select(or_(*tables)))).scalar_one())

    async def insert_adjustments(
        self, eid: str, dataset: str, path: Path, entry_nos: Sequence[str] | None, actor: str
    ) -> list[JournalEntry]:
        """更正分录载入的公共部分 (DatasetLoader.load_adjustments): 读 path, 取所请求的凭证 (同 id 同内容的已有行
        跳过); 有新行时在同一事务里经 DataStore.write_tables 把总账整表换一版 (现有行加新行): 来源 xero (客户在 Xero
        过账更正, 等同重新同步总账), 原文件名 adjustments/journal_entries.csv, 写入人 actor; 数据版本加一, 记版本行
        与数据变动. data 留痕由调用方在提交后随即写 (见协议说明). 试算表不动. 写入总账时会套用最新一版审批日志, 所以
        "同 id 同内容" 比的是请求的行按同一版审批日志补过之后的样子 (data_store.with_approvals), 返回的也是它, 即
        库里的行. 拒绝的情况见协议说明, 都不写入."""
        if isinstance(entry_nos, str):
            raise TypeError("entry_nos 须为凭证号列表, 不能是单个字符串")
        lines = read_adjustments(path)
        wanted = (
            list(dict.fromkeys(entry_nos)) if entry_nos is not None else list(dict.fromkeys(x.entry_no for x in lines))
        )
        missing = [no for no in wanted if no not in {x.entry_no for x in lines}]
        if missing:
            raise LookupError(f"更正分录中没有 {', '.join(missing)}")
        chosen = sorted((x for x in lines if x.entry_no in wanted), key=lambda x: x.id)
        unbalanced = [no for no in wanted if sum(x.debit_cents - x.credit_cents for x in chosen if x.entry_no == no)]
        if unbalanced:
            raise ValueError(f"更正分录借贷不平衡: {', '.join(unbalanced)}")
        # data_store 引用本模块, 用时再导入
        from backend.persistence.data_store import PostgresDataStore, with_approvals

        journal = m.FINANCIAL["journal_entries"]
        writer = PostgresDataStore(self.evidence_store.root, self.engine)  # 同一引擎, 并入本事务
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            existing, loaded = await self.load_state(conn, eid)  # 已载入的口径同 insert (看财务表有没有行)
            if existing is None or not loaded:
                raise ValueError("尚未载入数据集")
            if existing["dataset"] != dataset:
                raise ValueError("Engagement already has a different dataset")
            approvals = await writer.latest_rows(conn, eid, TableKind.approval_log)
            expected = with_approvals(chosen, approvals)  # 写进总账后的样子
            stored = {
                r["id"]: JournalEntry.model_validate({k: v for k, v in r.items() if k != "engagement_id"})
                for r in await self.rows(eid, select(journal).where(journal.c.id.in_([x.id for x in chosen])))
            }
            changed = [x.id for x in expected if x.id in stored and stored[x.id] != x]
            if changed:
                raise ValueError(f"更正分录与已载入的同 ID 行内容不同: {', '.join(changed)}")
            new = [x for x in chosen if x.id not in stored]
            if new:
                current = [
                    JournalEntry.model_validate({k: v for k, v in r.items() if k != "engagement_id"})
                    for r in await self.rows(eid, select(journal))
                ]
                rows = sorted([*current, *new], key=lambda x: x.id)
                await writer.write_tables(  # 写总账后套用最新一版审批日志 (新行同 expected)
                    eid,
                    {TableKind.journal_entries: rows},
                    source="xero",
                    actor=actor,
                    file_name=ADJUSTMENTS.as_posix(),
                )
        return expected


ADJUSTMENTS = Path("adjustments") / "journal_entries.csv"  # 数据集目录下的更正分录 (表头同 journal_entries.csv)


def read_adjustments(path: Path) -> list[JournalEntry]:
    """读更正分录 CSV (空串按 None); 文件不存在抛 FileNotFoundError, 消息含路径. created_at 须带时区 (时间存 UTC),
    否则抛 ValueError 并列出行 ID: 无时区的值写库后读回带时区, 再次载入时会被误判为内容不同."""
    if not path.is_file():
        raise FileNotFoundError(f"没有更正分录文件 {path}")
    with path.open(newline="", encoding="utf-8") as stream:
        rows = [
            JournalEntry.model_validate({k: (v if v != "" else None) for k, v in r.items()})
            for r in csv.DictReader(stream)
        ]
    # created_at 可空 (导入的总账可能没有); 有值时须带时区
    naive = [x.id for x in rows if x.created_at is not None and x.created_at.tzinfo is None]
    if naive:
        raise ValueError(f"更正分录的 created_at 没有时区: {', '.join(naive)}")
    return rows


def adjustment_summaries(path: Path | None) -> list[AdjustmentEntrySummary]:
    """更正分录的凭证摘要, 按凭证号自然序 (AJE-X2 在 AJE-X10 之前); 文件不存在为空."""
    if path is None or not path.is_file():
        return []
    groups: dict[str, list[JournalEntry]] = {}
    for line in read_adjustments(path):
        groups.setdefault(line.entry_no, []).append(line)
    return [
        AdjustmentEntrySummary(
            entry_no=no, posting_date=lines[0].posting_date, description=lines[0].description, line_count=len(lines)
        )
        for no, lines in sorted(groups.items(), key=lambda item: natural_key(item[0]))
    ]


def natural_key(text: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


class CsvDatasetLoader(TableLoader):
    """CSV 数据集 (如 Numbat): 每张财务表一个 CSV, 只有应付账单可以没有文件, 没有时为空 (Numbat 没有账单, 需求
    R178), 其余缺文件照旧报错; 科目没有 category 列时按名称映射标准分类."""

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
                if name in OPTIONAL_CSV and not (directory / f"{name}.csv").is_file():
                    continue
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

    def supplement_dir(self, dataset: str) -> Path | None:
        """登记与回函 mock 读的目录: 数据集目录本身 (如 data/demo); 不存在时为 None."""
        directory = safe_child(self.root, dataset)
        return directory if directory.is_dir() else None

    async def load_adjustments(
        self, eid: str, dataset: str, entry_nos: Sequence[str] | None = None, *, actor: str = "A1"
    ) -> list[JournalEntry]:
        path = safe_child(self.root, dataset) / ADJUSTMENTS
        return await self.insert_adjustments(eid, dataset, path, entry_nos, actor)

    def adjustment_entries(self, dataset: str) -> list[AdjustmentEntrySummary]:
        return adjustment_summaries(safe_child(self.root, dataset) / ADJUSTMENTS)

    def adjustment_entry_nos(self, dataset: str) -> list[str]:
        return [x.entry_no for x in self.adjustment_entries(dataset)]

    def dataset_traits(self, dataset: str) -> DatasetTraits:
        """CSV 数据集 (如 Numbat) 没有数据集声明, 取缺省值."""
        return DatasetTraits()

    def datasets(self) -> list[str]:
        """root 下带 client_profile.json 的子目录 (如 demo); external 这类没有画像的目录不在其中."""
        return subdirectories(self.root, "client_profile.json")

    def period(self, dataset: str) -> tuple[date, date] | None:
        """CSV 数据集不自带期间 (Numbat 由审计师填)."""
        return None


class XeroDatasetLoader(TableLoader):
    """Xero 快照 (xero pull 的输出目录): dataset 为 "xero/{快照目录名}", 对应 {root}/{快照目录名}.

    补造目录 {supplements}/{快照目录名} (Xero 演示数据集, 见 adapters.xero_supplements) 存在时, 载入与取数前先核对
    它与快照; 有补造画像与计划预置时以它们为准. supplements 为空或该目录不存在时: 规划预设取通用模板 (planning
    目录), 客户画像取快照的组织信息. 测试副本 (manifest 带 injected) 只在 allow_injected 下载入. 映射见
    adapters.xero_dataset.
    """

    PREFIX = "xero/"

    def __init__(
        self,
        root: Path,
        planning: Path,
        engine: AsyncEngine | None = None,
        evidence_store: FileEvidenceStore | None = None,
        *,
        supplements: Path | None = None,
        allow_injected: bool = False,
    ) -> None:
        super().__init__(engine, evidence_store)
        self.root = root  # 快照目录的上级, 如 data/external/xero/api
        self.planning = planning  # 通用规划预设目录
        self.supplements = supplements  # 补造目录的上级, 如 data/xero_demo
        self.allow_injected = allow_injected

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

    def supplement(self, dataset: str) -> Supplements | None:
        """数据集的补造目录, 已核对; 没有配置或该目录不存在时为 None. 不匹配抛 SupplementMismatch."""
        snapshot = self.snapshot(dataset)
        if self.supplements is None:
            return None
        directory = safe_child(self.supplements, dataset.removeprefix(self.PREFIX))
        if not directory.is_dir():
            return None
        found = Supplements(directory, snapshot.root, dataset, self.allow_injected)
        found.verify()
        return found

    def tables(self, dataset: str) -> dict[str, list[Any]]:
        """演示数据导入之后的八张财务表 (不连库的纯函数, 测试用; load 只载快照, 需求 R269): 快照映射出
        八张表 (adapters.xero_dataset, 含应付账单); 有补造目录时先核对, 再并入合同关联, 服务完成记录与开户行
        (Supplements.apply). 没有补造目录时与纯快照相同."""
        return self.merged(dataset, self.supplement(dataset))

    def merged(self, dataset: str, found: Supplements | None) -> dict[str, list[Any]]:
        """快照映射出的八张表, 有 (已核对的) 补造目录 found 时并入补造表."""
        tables = build_tables(self.snapshot(dataset))
        return found.apply(tables) if found is not None else tables

    def has_supplement(self, dataset: str) -> bool:
        """数据集有没有补造目录 (需求 R270): 名称合法, 快照与同名补造目录都在; 不核对哈希 (概览轮询时用, 篡改由
        supplement 的核对报出)."""
        if self.supplements is None:
            return False
        try:
            snapshot = self.directory(dataset)
            directory = safe_child(self.supplements, dataset.removeprefix(self.PREFIX))
        except ValueError:
            return False
        return snapshot.is_dir() and (directory / "manifest.json").is_file()

    def supplement_tables(self, dataset: str) -> list[DemoTable]:
        """补造覆盖的账表, 同演示导入清单 (xero_supplements.plan) 的 tables; 没有补造目录时为空."""
        found = self.supplement(dataset)
        return found.demo_tables() if found is not None else []

    async def load(self, eid: str, dataset: str) -> None:
        """只载快照映射出的八张表 (需求 R269, 含应付账单 R178): 不并补造账表, 不登记文件; 补造部分 (账表与文件)
        由演示数据导入 (services.demo_import) 分阶段写入与上传. 有补造目录时仍先核对 (与快照不符抛
        SupplementMismatch, 不写)."""
        self.directory(dataset)  # 名称先校验; 快照是否存在与补造核对等确认未载入后再做

        def build() -> Tables:
            self.supplement(dataset)
            return self.merged(dataset, None), ()

        await self.insert(eid, dataset, build)

    def planning_presets(self, dataset: str) -> tuple[list[AuditObjective], list[RiskItem]]:
        found = self.supplement(dataset)
        presets = found.planning_presets() if found else None
        return presets if presets is not None else read_planning(self.planning)

    def supplement_dir(self, dataset: str) -> Path | None:
        """登记与回函 mock 读的目录: 已核对的补造目录; 没有时为 None (快照里没有登记与回函数据)."""
        found = self.supplement(dataset)
        return found.root if found else None

    def adjustments_path(self, dataset: str) -> Path:
        """补造目录的更正分录文件 (已核对); 没有补造目录时抛 FileNotFoundError, 消息含应有的路径."""
        found = self.supplement(dataset)
        if found is None:
            base = (
                self.supplements / dataset.removeprefix(self.PREFIX) if self.supplements else Path("<未配置补造目录>")
            )
            raise FileNotFoundError(f"没有更正分录文件 {base / ADJUSTMENTS}")
        return found.root / ADJUSTMENTS

    async def load_adjustments(
        self, eid: str, dataset: str, entry_nos: Sequence[str] | None = None, *, actor: str = "A1"
    ) -> list[JournalEntry]:
        return await self.insert_adjustments(eid, dataset, self.adjustments_path(dataset), entry_nos, actor)

    def adjustment_entries(self, dataset: str) -> list[AdjustmentEntrySummary]:
        found = self.supplement(dataset)
        return adjustment_summaries(found.root / ADJUSTMENTS if found else None)

    def adjustment_entry_nos(self, dataset: str) -> list[str]:
        return [x.entry_no for x in self.adjustment_entries(dataset)]

    def dataset_traits(self, dataset: str) -> DatasetTraits:
        """数据集声明: 已核对的补造目录的 dataset_traits.json (Supplements.dataset_traits); 没有补造目录或没有这个
        文件 (如 20261017-mock) 取缺省值."""
        found = self.supplement(dataset)
        traits = found.dataset_traits() if found else None
        return traits if traits is not None else DatasetTraits()

    def client_profile(self, dataset: str) -> ClientProfile:
        found = self.supplement(dataset)  # 先核对: organisation.json 被改坏时报不匹配, 不报数据集不存在
        profile = xero_client_profile(self.snapshot(dataset))
        supplied = found.client_profile(f"{profile.id}-") if found else None
        return supplied or profile

    def datasets(self) -> list[str]:
        """root 下能载入的快照 (有组织信息与期间 dataset.json), 名为 xero/{快照目录}; 拉取中的临时目录不算."""
        return [self.PREFIX + name for name in subdirectories(self.root, "organisation.json", "dataset.json")]

    def period(self, dataset: str) -> tuple[date, date] | None:
        """快照 dataset.json 的本期起止 (载入器按它取试算表, 项目期间须与之相同); 快照不存在或没有 dataset.json 时抛
        FileNotFoundError, dataset.json 不是 JSON, 缺键, 不是对象或日期写错时抛 ValueError (消息不带路径与文件内容)."""
        period = snapshot_period(self.snapshot(dataset))
        return period.start, period.end


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

    async def load_adjustments(
        self, eid: str, dataset: str, entry_nos: Sequence[str] | None = None, *, actor: str = "A1"
    ) -> list[JournalEntry]:
        return await self.pick(dataset).load_adjustments(eid, dataset, entry_nos, actor=actor)

    def adjustment_entries(self, dataset: str) -> list[AdjustmentEntrySummary]:
        return self.pick(dataset).adjustment_entries(dataset)

    def adjustment_entry_nos(self, dataset: str) -> list[str]:
        return self.pick(dataset).adjustment_entry_nos(dataset)

    def dataset_traits(self, dataset: str) -> DatasetTraits:
        return self.pick(dataset).dataset_traits(dataset)

    def supplement(self, dataset: str) -> Supplements | None:
        """演示数据导入的清单来源 (具体方法, 不在 DatasetLoader 协议里, 需求 R269): 所选载入器已核对的补造目录;
        载入器没有这个方法 (Numbat 的 CSV 载入器) 时为 None."""
        method: Callable[[str], Supplements | None] | None = getattr(self.pick(dataset), "supplement", None)
        return method(dataset) if method is not None else None

    def has_supplement(self, dataset: str) -> bool:
        """数据集有没有补造目录, 不核对哈希 (具体方法, 需求 R270); 载入器没有这个方法时为假."""
        method: Callable[[str], bool] | None = getattr(self.pick(dataset), "has_supplement", None)
        return method(dataset) if method is not None else False

    def supplement_dir(self, dataset: str) -> Path | None:
        """登记与回函 mock 读的目录 (具体方法, 不在 DatasetLoader 协议里): 所选载入器的 supplement_dir; 载入器没有
        这个方法时为 None."""
        method: Callable[[str], Path | None] | None = getattr(self.pick(dataset), "supplement_dir", None)
        return method(dataset) if method is not None else None

    def period(self, dataset: str) -> tuple[date, date] | None:
        return self.pick(dataset).period(dataset)

    def datasets(self) -> list[str]:
        """各载入器的数据集合在一起, 按名称排序."""
        return sorted(name for loader in (self.default, *self.by_prefix.values()) for name in loader.datasets())


async def record_changes(
    conn: AsyncConnection,
    eid: str,
    version: int,
    inputs: Iterable[DataInput],
    document_types: Sequence[DocumentType] | None = None,
) -> None:
    """在当前事务里记下数据版本 version 变了哪些数据 (表 data_changes); 已记的跳过. document_types 为登记变了的文件的
    类型 (需求 R304), 记在 documents 那一行 (按 DocumentType 顺序去重); 不给时与其余各行一样存 SQL NULL (不写 JSON
    null), 即不知道是哪类文件."""
    wanted = None if document_types is None else set(document_types)
    types = None if wanted is None else [t.value for t in DocumentType if t in wanted]
    rows = [
        {
            "engagement_id": eid,
            "data_version": version,
            "input": i.value,
            "document_types": types if i is DataInput.documents and types is not None else null(),
        }
        for i in dict.fromkeys(inputs)
    ]
    if rows:
        await conn.execute(insert(m.data_changes).values(rows).on_conflict_do_nothing())


async def write_contract(
    conn: AsyncConnection,
    eid: str,
    contract: Contract,
    *,
    changed_by: str = AgentId.A3,
    field: str | None = None,
    reason: str | None = None,
) -> None:
    """在已开的事务里写一版合同 (FinancialDataStore.save_contract 与智能导入的确认共用): 覆盖合同与条款, 在
    extraction_versions 记这一版."""
    data = contract.model_dump(exclude={"terms", "evidence"})
    data["evidence"] = contract.model_dump(mode="json")["evidence"]
    stmt = insert(m.contracts).values(engagement_id=eid, **data)
    await conn.execute(stmt.on_conflict_do_update(index_elements=["engagement_id", "id"], set_=data))
    await upsert_extraction(
        conn,
        eid,
        "contract",
        contract.id,
        contract.version,
        contract.model_dump(mode="json"),
        changed_by=changed_by,
        field=field,
        reason=reason,
    )
    await conn.execute(
        delete(m.contract_terms).where(
            m.contract_terms.c.engagement_id == eid, m.contract_terms.c.contract_id == contract.id
        )
    )
    for term in contract.terms:
        await conn.execute(
            insert(m.contract_terms).values(engagement_id=eid, contract_id=contract.id, **term.model_dump())
        )


async def write_document(
    conn: AsyncConnection,
    eid: str,
    document_id: str,
    *,
    type: DocumentType,
    reliability: Reliability,
    status: DocumentStatus,
    failed_step: DocumentFailedStep | None,
) -> DocumentRef:
    """在已开的事务里改一份文件的登记 (EvidenceStore.update_document 与智能导入的确认共用), 返回改后的登记;
    文件不存在抛 FileNotFoundError."""
    t = m.documents
    result = await conn.execute(
        update(t)
        .where(t.c.engagement_id == eid, t.c.id == document_id)
        .values(type=type, reliability=reliability, status=status, failed_step=failed_step)
    )
    if not result.rowcount:
        raise FileNotFoundError(document_id)
    row = (await conn.execute(select(t).where(t.c.id == document_id))).mappings().one()
    return document_ref(row)


def document_ref(row: Mapping[Any, Any]) -> DocumentRef:
    """documents 表的一行转成登记 (去掉 engagement_id, file_path 等登记以外的列)."""
    return DocumentRef.model_validate({k: v for k, v in row.items() if k in DocumentRef.model_fields})


async def upsert_extraction(
    conn: AsyncConnection,
    eid: str,
    target_type: str,
    target_id: str,
    version: int,
    data: Mapping[str, Any],
    *,
    changed_by: str,
    field: str | None,
    reason: str | None,
) -> None:
    """在 extraction_versions 记一版抽取结果; 同 (类型, 对象, 版本) 已有则覆盖 (抽取重做时幂等)."""
    values = {
        "data": dict(data),
        "field": field,
        "reason": reason,
        "changed_by": changed_by,
        "changed_at": now(),
    }
    statement = insert(m.extraction_versions).values(
        engagement_id=eid, target_type=target_type, target_id=target_id, version=version, **values
    )
    await conn.execute(
        statement.on_conflict_do_update(
            index_elements=["engagement_id", "target_type", "target_id", "version"], set_=values
        )
    )


def read_planning(directory: Path) -> tuple[list[AuditObjective], list[RiskItem]]:
    """读目录下的 audit_objectives.json 与 risk_register.json."""
    return (
        [AuditObjective.model_validate(r) for r in json.loads((directory / "audit_objectives.json").read_text())],
        [RiskItem.model_validate(r) for r in json.loads((directory / "risk_register.json").read_text())],
    )


class PostgresEngagementStore(Store):
    async def create(
        self,
        *,
        owner_id: str,
        period_start: date,
        period_end: date,
        dataset: str | None,
        profile: ClientProfile,
        working_language: WorkingLanguage = "en",
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
            adopted = await self.adopt_profile(profile, updated_by=owner_id)
            item = Engagement(
                id=f"ENG-{year}-{number:03d}",
                client_id=profile.id,
                period_start=period_start,
                period_end=period_end,
                dataset=dataset,
                owner_id=owner_id,
                client_profile_version=adopted.version,
                phase=Phase.P0_Acceptance,
                working_language=working_language,
                created_at=now(),
                updated_at=now(),
            )
            await conn.execute(insert(m.engagements).values(**item.model_dump()))
            return item

    async def adopt_profile(self, profile: ClientProfile, *, updated_by: str) -> ClientProfile:
        """建项目时采用的画像 (改一律出新版本, 旧版本不覆盖). profile 是数据集带的画像 (新建的客户为其最新一版), 与
        该客户已存的各版比较内容 (不看版本号): 库里没有该客户时按 profile 写入; 有一版相同, 说明数据集没变, 沿用最新
        一版 (含审计师在 ACC-01 改过的); 都不同, 说明数据集本身改了 (如补造画像改了内容), 才写新一版 (最新版本 +1).
        写入的版本 source 为 dataset, updated_by 为建项目的人."""
        versions = [r.profile for r in await self.profile_history(profile.id)]
        if not versions:
            return await self.save_profile(profile, source=ProfileSource.dataset, updated_by=updated_by)
        if any(v.model_copy(update={"version": profile.version}) == profile for v in versions):
            return versions[-1]
        return await self.save_profile(
            profile.model_copy(update={"version": versions[-1].version + 1}),
            source=ProfileSource.dataset,
            updated_by=updated_by,
        )

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

    async def set_working_language(self, eid: str, language: WorkingLanguage) -> WorkingLanguage:
        """writable 锁住项目行 (FOR NO KEY UPDATE) 后读旧值再改, 并发的修改排队; 与旧值相同时不改."""
        t = m.engagements
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            previous = cast(
                WorkingLanguage, (await conn.execute(select(t.c.working_language).where(t.c.id == eid))).scalar_one()
            )
            if previous != language:
                await conn.execute(update(t).where(t.c.id == eid).values(working_language=language, updated_at=now()))
            return previous

    async def client_profile(self, client_id: str, version: int | None = None) -> ClientProfile | None:
        eid = self._joined_eid()  # 画像是跨项目表, 并入当前项目事务而不改其 RLS 范围
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

    def _joined_eid(self) -> str:
        """当前任务里已开的本引擎事务的项目 ID (画像是跨项目表, 读写都并入该事务而不改其 RLS 范围); 没有时为空."""
        from backend.persistence.db import _active

        active = _active.get()
        return active[1] if active and active[0] is self.engine and active[3] is asyncio.current_task() else ""

    async def profile_history(self, client_id: str) -> list[ProfileRecord]:
        """该客户画像的各版及出处, 按版本号从低到高; 没有时为空列表. 与 client_profile 一样并入当前项目事务."""
        t = m.client_profiles
        stmt = (
            select(t.c.data, t.c.source, t.c.updated_by, t.c.updated_at)
            .where(t.c.id == client_id)
            .order_by(t.c.version)
        )
        return [
            ProfileRecord(
                profile=ClientProfile.model_validate(row["data"]),
                source=row["source"],
                updated_by=row["updated_by"],
                updated_at=row["updated_at"],
            )
            for row in await self.rows(self._joined_eid(), stmt)
        ]

    async def clients(self) -> list[ClientEntry]:
        """各客户 (按 id 排序) 的最新一版与第 1 版的来源."""
        t = m.client_profiles
        latest = select(t.c.id, t.c.data).distinct(t.c.id).order_by(t.c.id, t.c.version.desc())
        first = select(t.c.id, t.c.source).distinct(t.c.id).order_by(t.c.id, t.c.version)
        eid = self._joined_eid()
        sources = {row["id"]: row["source"] for row in await self.rows(eid, first)}
        return [
            ClientEntry(profile=ClientProfile.model_validate(row["data"]), first_source=sources.get(row["id"]))
            for row in await self.rows(eid, latest)
        ]

    async def create_client(
        self, profile: ClientProfile, *, created_by: str, dataset_profiles: Sequence[ClientProfile]
    ) -> ClientProfile:
        """新建客户 (见 EngagementStore.create_client): 咨询锁 clients 内查 ABN 是否已被占用, 再写第 1 版."""
        if profile.version != 1:
            raise VersionConflict(profile.id)
        return await self.save_profile(
            profile, source=ProfileSource.manual, updated_by=created_by, dataset_profiles=dataset_profiles
        )

    async def _check_abn(self, conn: AsyncConnection, profile: ClientProfile, others: Sequence[ClientProfile]) -> None:
        """在已开的事务里取咨询锁 clients (新建客户与改 ABN 共用, 查重与写入不交错), 查 profile 的 ABN 是否已是别的
        客户的: 库里其他客户的最新一版, 加上 others 中还没进库的数据集画像; 撞上抛 AbnTaken."""
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('clients', 0))"))
        stored = [entry.profile for entry in await self.clients()]
        known = {p.id for p in stored}
        for other in [*stored, *(p for p in others if p.id not in known)]:
            if other.id != profile.id and normalize_abn(other.abn) == normalize_abn(profile.abn):
                raise AbnTaken(profile.abn, other.id, other.legal_name)

    async def save_profile(
        self,
        profile: ClientProfile,
        *,
        source: ProfileSource,
        updated_by: str | None,
        dataset_profiles: Sequence[ClientProfile] | None = None,
    ) -> ClientProfile:
        eid = self._joined_eid()
        async with transaction(self.engine, eid) as conn:
            if eid:
                await writable(conn, eid)
            if dataset_profiles is not None:
                await self._check_abn(conn, profile, dataset_profiles)
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "profile:" + profile.id}
            )
            previous = await self.client_profile(profile.id)
            if profile.version != (previous.version + 1 if previous else 1):
                raise VersionConflict(profile.id)
            await conn.execute(
                insert(m.client_profiles).values(
                    id=profile.id,
                    version=profile.version,
                    data=profile.model_dump(mode="json"),
                    source=source.value,
                    updated_by=updated_by,
                    updated_at=now(),
                )
            )
        return profile.model_copy(deep=True)

    async def list(self, owner_id: str | None = None) -> list[Engagement]:
        stmt = select(m.engagements).order_by(m.engagements.c.id)
        if owner_id is not None:
            stmt = stmt.where(m.engagements.c.owner_id == owner_id)
        return [Engagement.model_validate(row) for row in await self.rows("", stmt)]


class PostgresUserStore(Store):
    """用户表; email 列即登录名. 查找与判重都不区分大小写, 早于注册功能存入的含大写账号照常可用."""

    async def by_email(self, email: str) -> tuple[User, str] | None:
        """按登录名查找, 不区分大小写. 库里若有只差大小写的多个账号 (注册功能之前可能存在), 只认完全相同的那个."""
        rows = await self.rows("", select(m.users).where(func.lower(m.users.c.email) == email.lower()))
        exact = [row for row in rows if row["email"] == email]
        if len(rows) == 1:
            row = rows[0]
        elif len(exact) == 1:
            row = exact[0]
        else:
            return None
        return User(id=row["id"], email=row["email"], full_name=row["full_name"]), row["password_hash"]

    async def get(self, user_id: str) -> User | None:
        rows = await self.rows("", select(m.users).where(m.users.c.id == user_id))
        return User(**{k: rows[0][k] for k in ("id", "email", "full_name")}) if rows else None

    async def create(self, email: str, full_name: str, password_hash: str) -> User | None:
        """登录名 (不区分大小写) 未被占用时插入一行, 否则返回 None, 不改原账号.

        先按小写登录名取事务级咨询锁, 再查重再插入: 只差大小写的并发注册排队执行, 只成功一个.
        唯一约束 (email 列, 区分大小写) 仍在, 作为最后一道保险.
        """
        async with transaction(self.engine, "") as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "user:" + email.lower()}
            )
            taken = await conn.execute(select(m.users.c.id).where(func.lower(m.users.c.email) == email.lower()))
            if taken.first() is not None:
                return None
            created = (
                await conn.execute(
                    insert(m.users)
                    .values(
                        id=f"USR-{uuid4()}",
                        email=email,
                        full_name=full_name,
                        password_hash=password_hash,
                        created_at=now(),
                    )
                    .on_conflict_do_nothing(index_elements=[m.users.c.email])
                    .returning(m.users.c.id)
                )
            ).scalar_one_or_none()
        return None if created is None else User(id=created, email=email, full_name=full_name)


class PostgresLlmSettingsStore(Store):
    """设置页的模型配置, 表 llm_settings (系统级, 不绑定项目, 事务 eid 为空串)."""

    async def get(self, slot: LlmSlot) -> LlmSettings | None:
        rows = await self.rows("", select(m.llm_settings).where(m.llm_settings.c.slot == slot))
        return LlmSettings.model_validate(rows[0]) if rows else None

    async def save(self, settings: LlmSettings) -> LlmSettings:
        values = settings.model_dump()
        statement = insert(m.llm_settings).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[m.llm_settings.c.slot], set_={k: v for k, v in values.items() if k != "slot"}
        )
        async with transaction(self.engine, "") as conn:
            await conn.execute(statement)
        return settings
