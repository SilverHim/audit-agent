"""DataStore 的 PostgreSQL 实现: 数据版本与就绪确认, 账表的版本写入, 导入会话, 统计.

方法签名见 contracts/ports.py, 表见开发接口文档第 9 节. 账表写入 (write_tables) 是 "新数据版本写入" 的唯一入口,
导入 (R13) 与 Xero 同步 (R12) 共用.
"""

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from backend.contracts.data import (
    ApprovalLogRow,
    CategoryVersion,
    CheckId,
    DataCheck,
    DataReadiness,
    DataSource,
    ExtractionRecord,
    ImportSession,
    TableKind,
    TableStats,
    TableVersion,
)
from backend.contracts.models import AgentId
from backend.contracts.records import Account
from backend.persistence import models as m
from backend.persistence.db import transaction, writable
from backend.persistence.stores import Store, now, safe_child, upsert_extraction

# 各表的日期列, 概览据此给日期范围; 科目表, 试算表, 银行账户没有
DATE_COLUMNS: dict[TableKind, str] = {
    TableKind.journal_entries: "posting_date",
    TableKind.sales_invoices: "invoice_date",
    TableKind.bank_transactions: "txn_date",
    TableKind.delivery_notes: "shipped_date",
}


SYSTEM = AgentId.A3  # 系统归类科目时分类历史记的修改人: 数据管理智能体


def version_id(source: DataSource, kind: TableKind, data_version: int) -> str:
    """没有导入会话的版本 (Xero 同步等) 的 ID: {来源大写}-{表}-v{数据版本}."""
    return f"{source.upper()}-{kind}-v{data_version}"


async def latest_categories(conn: AsyncConnection, eid: str) -> dict[str, CategoryVersion]:
    """每个科目分类历史的最新一版."""
    h = m.account_category_history
    statement = (
        select(h.c.version, h.c.category, h.c.basis, h.c.changed_by, h.c.changed_at, h.c.reason, h.c.account_code)
        .where(h.c.engagement_id == eid)
        .distinct(h.c.account_code)
        .order_by(h.c.account_code, h.c.version.desc())
    )
    rows = (await conn.execute(statement)).mappings()
    return {
        r["account_code"]: CategoryVersion.model_validate({k: v for k, v in r.items() if k != "account_code"})
        for r in rows
    }


async def record_categories(conn: AsyncConnection, eid: str, accounts: Sequence[Account]) -> list[Account]:
    """写科目表前对照分类历史, 返回实际写入的科目: 审计师改过的 (最新一版依据为 auditor) 沿用审计师的分类;
    其余与最新一版的分类或依据不同 (或还没有历史) 时记一版, 修改人为 A3, 没有理由."""
    latest = await latest_categories(conn, eid)
    result = []
    for account in accounts:
        previous = latest.get(account.code)
        if previous is not None and previous.basis == "auditor":
            result.append(account.model_copy(update={"category": previous.category, "category_basis": "auditor"}))
            continue
        if previous is None or (previous.category, previous.basis) != (account.category, account.category_basis):
            await conn.execute(
                insert(m.account_category_history).values(
                    engagement_id=eid,
                    account_code=account.code,
                    version=previous.version + 1 if previous else 1,
                    category=account.category,
                    basis=account.category_basis,
                    changed_by=SYSTEM,
                    changed_at=now(),
                    reason=None,
                )
            )
        result.append(account)
    return result


async def apply_approvals(conn: AsyncConnection, eid: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """审批日志按凭证号补进总账分录: 每行只改导入时对应了的列 (录入人, 审批人, 审批时间); 总账里没有的凭证号忽略."""
    j = m.FINANCIAL["journal_entries"]
    for raw in rows:
        row = ApprovalLogRow.model_validate(raw)
        values = row.model_dump(exclude_unset=True, exclude={"entry_no"})
        if values:
            statement = update(j).where(j.c.engagement_id == eid, j.c.entry_no == row.entry_no).values(**values)
            await conn.execute(statement)


class PostgresDataStore(Store):
    """数据管理的存储. root 为暂存导入原文件的根目录 (组装根传证据根目录), 文件放 {root}/{eid}/imports/{导入 ID}."""

    def __init__(self, root: Path, engine: AsyncEngine | None = None) -> None:
        super().__init__(engine)
        self.root = root

    async def readiness(self, eid: str) -> DataReadiness:
        t = m.dataset_loads
        rows = await self.rows(eid, select(t.c.data_version, t.c.loaded_at, t.c.ready_at, t.c.ready_by))
        return DataReadiness.model_validate(rows[0]) if rows else DataReadiness(data_version=0)

    async def confirm_ready(self, eid: str, actor: str) -> DataReadiness:
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            await self.loads_row(conn, eid)
            t = m.dataset_loads
            await conn.execute(
                update(t).where(t.c.engagement_id == eid, t.c.ready_at.is_(None)).values(ready_at=now(), ready_by=actor)
            )
        return await self.readiness(eid)

    async def loads_row(self, conn: AsyncConnection, eid: str) -> dict[str, Any]:
        """本项目的 dataset_loads 行, 加行锁 (同一项目的数据写入排队); 没有时以数据版本 0 新建,
        dataset 取项目的数据集名 (客户来源)."""
        t = m.dataset_loads
        row = (await conn.execute(select(t).where(t.c.engagement_id == eid).with_for_update())).mappings().first()
        if row is not None:
            return dict(row)
        dataset = (await conn.execute(select(m.engagements.c.dataset).where(m.engagements.c.id == eid))).scalar_one()
        values: dict[str, Any] = {"engagement_id": eid, "dataset": dataset, "data_version": 0, "loaded_at": now()}
        await conn.execute(insert(t).values(**values))
        return {**values, "ready_at": None, "ready_by": None}

    async def stats(self, eid: str) -> dict[TableKind, TableStats]:
        result: dict[TableKind, TableStats] = {}
        async with transaction(self.engine, eid) as conn:
            for kind in TableKind:
                if kind is TableKind.approval_log:
                    result[kind] = TableStats(records=await self.approval_records(conn, eid))
                    continue
                table = m.FINANCIAL[kind]
                if kind is TableKind.trial_balance:
                    statement = select(table.c.period, func.count()).group_by(table.c.period)
                    counts = {period: int(n) for period, n in (await conn.execute(statement)).tuples()}
                    current, prior = counts.get("current", 0), counts.get("prior", 0)
                    result[kind] = TableStats(records=current + prior, current=current, prior=prior)
                    continue
                column = DATE_COLUMNS.get(kind)
                if column is None:
                    count = (await conn.execute(select(func.count()).select_from(table))).scalar_one()
                    result[kind] = TableStats(records=int(count))
                    continue
                row = (
                    await conn.execute(select(func.count(), func.min(table.c[column]), func.max(table.c[column])))
                ).one()
                result[kind] = TableStats(records=int(row[0]), period_from=row[1], period_to=row[2])
        return result

    async def approval_records(self, conn: AsyncConnection, eid: str) -> int:
        """最新一版审批日志的行数; 没有为 0."""
        t = m.data_imports
        statement = (
            select(t.c.records)
            .where(t.c.engagement_id == eid, t.c.kind == TableKind.approval_log, t.c.status == "committed")
            .order_by(t.c.data_version.desc())
            .limit(1)
        )
        return int((await conn.execute(statement)).scalar() or 0)

    async def versions(self, eid: str) -> list[TableVersion]:
        t = m.data_imports
        columns = [t.c[name] for name in TableVersion.model_fields if name != "created_at"]
        statement = (
            select(*columns, func.coalesce(t.c.committed_at, t.c.created_at).label("created_at"))
            .where(t.c.status == "committed")
            .order_by(t.c.data_version, t.c.id)
        )
        return [TableVersion.model_validate(r) for r in await self.rows(eid, statement)]

    async def checks(self, eid: str) -> list[DataCheck]:
        t = m.data_checks
        rows = await self.rows(eid, select(t.c.id, t.c.status, t.c.params, t.c.request_id))
        order = list(CheckId)
        return sorted((DataCheck.model_validate(r) for r in rows), key=lambda c: order.index(c.id))

    async def open_requests(self, eid: str) -> int:
        t = m.data_requests
        rows = await self.rows(eid, select(func.count().label("n")).where(t.c.status == "open"))
        return int(rows[0]["n"])

    # ── 账表写入 (导入, Xero 同步共用) ──

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
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            loads = await self.loads_row(conn, eid)  # 行锁: 同一项目的写入排队, 版本号连续
            version = int(loads["data_version"]) + 1
            t = m.dataset_loads
            await conn.execute(update(t).where(t.c.engagement_id == eid).values(data_version=version))
            for kind in sorted(tables, key=list(TableKind).index):  # 总账在审批日志之前
                rows: Sequence[BaseModel] = list(tables[kind])
                if kind is TableKind.accounts:
                    rows = await record_categories(conn, eid, [Account.model_validate(r.model_dump()) for r in rows])
                dumped = [r.model_dump(mode="json", exclude_unset=kind is TableKind.approval_log) for r in rows]
                if kind is TableKind.approval_log:
                    await apply_approvals(conn, eid, dumped)
                else:
                    table = m.FINANCIAL[kind]
                    await conn.execute(delete(table).where(table.c.engagement_id == eid))
                    if rows:
                        await conn.execute(insert(table), [dict(engagement_id=eid, **r.model_dump()) for r in rows])
                    if kind is TableKind.journal_entries:  # 新总账套用最新一版审批日志
                        await apply_approvals(conn, eid, await self.latest_rows(conn, eid, TableKind.approval_log))
                values: dict[str, Any] = {
                    "kind": kind.value,
                    "source": source,
                    "document_id": document_id,
                    "records": len(rows),
                    "rows": dumped,
                    "status": "committed",
                    "data_version": version,
                    "committed_at": now(),
                }
                d = m.data_imports
                updated = 0
                if import_id is not None:  # 导入会话: 置 committed, 记版本
                    result = await conn.execute(
                        update(d).where(d.c.engagement_id == eid, d.c.id == import_id).values(**values)
                    )
                    updated = result.rowcount
                if not updated:
                    await conn.execute(
                        insert(d).values(
                            engagement_id=eid,
                            id=import_id or version_id(source, kind, version),
                            file_name=file_name,
                            request_id=request_id,
                            created_by=actor,
                            created_at=now(),
                            **values,
                        )
                    )
        return version

    async def latest_rows(self, conn: AsyncConnection, eid: str, kind: TableKind) -> list[dict[str, Any]]:
        """某表最新一版的行快照; 没有版本为空."""
        d = m.data_imports
        statement = (
            select(d.c.rows)
            .where(d.c.engagement_id == eid, d.c.kind == kind, d.c.status == "committed")
            .order_by(d.c.data_version.desc())
            .limit(1)
        )
        row = (await conn.execute(statement)).first()
        return list(row.rows or []) if row is not None else []

    # ── 导入会话 ──

    def staged(self, eid: str, import_id: str) -> Path:
        """导入会话暂存原文件的路径."""
        return safe_child(safe_child(safe_child(self.root, eid), "imports"), import_id)

    async def create_import(self, eid: str, session: ImportSession, data: bytes) -> None:
        path = self.staged(eid, session.id)
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, data)
            await conn.execute(
                insert(m.data_imports).values(
                    engagement_id=eid,
                    id=session.id,
                    source="import",
                    file_name=session.file_name,
                    file_path=str(path),
                    sheet=session.sheet,
                    request_id=session.request_id,
                    status=session.status,
                    created_by=session.created_by,
                    created_at=session.created_at,
                )
            )

    async def import_session(self, eid: str, import_id: str) -> tuple[ImportSession, bytes] | None:
        d = m.data_imports
        columns = [d.c[name] for name in ImportSession.model_fields]  # 列名与字段名一致
        rows = await self.rows(eid, select(*columns, d.c.file_path).where(d.c.id == import_id, d.c.source == "import"))
        if not rows or rows[0]["file_path"] is None:
            return None
        row = rows[0]
        path = self.staged(eid, import_id)
        if Path(row.pop("file_path")).resolve() != path.resolve():
            raise ValueError("Staged import path mismatch")
        return ImportSession.model_validate(row), await asyncio.to_thread(path.read_bytes)

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
        changes: dict[str, Any] = {"sheet": sheet}
        if date_format is not None:
            changes["date_format"] = date_format
        if mapping is not None:
            changes["mapping"] = dict(mapping)
        if row_count is not None:
            changes["row_count"] = row_count
        d = m.data_imports
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            # 只改未提交的会话: 提交后记下的选择不再变 (换表与提交在服务层另以项目锁互斥)
            statement = update(d).where(d.c.engagement_id == eid, d.c.id == import_id, d.c.status == "parsed")
            await conn.execute(statement.values(**changes))

    # ── 数据版本与抽取结果 (需求 R14) ──

    async def extraction_versions(
        self, eid: str, target_type: str | None = None, target_id: str | None = None
    ) -> list[ExtractionRecord]:
        t = m.extraction_versions
        statement = select(*[t.c[name] for name in ExtractionRecord.model_fields])
        if target_type is not None:
            statement = statement.where(t.c.target_type == target_type)
        if target_id is not None:
            statement = statement.where(t.c.target_id == target_id)
        rows = await self.rows(eid, statement.order_by(t.c.target_type, t.c.target_id, t.c.version))
        return [ExtractionRecord.model_validate(r) for r in rows]

    async def bump_version(self, eid: str) -> int:
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            loads = await self.loads_row(conn, eid)
            version = int(loads["data_version"]) + 1
            t = m.dataset_loads
            await conn.execute(update(t).where(t.c.engagement_id == eid).values(data_version=version))
        return version

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
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            await upsert_extraction(
                conn, eid, target_type, target_id, version, data, changed_by=changed_by, field=field, reason=reason
            )
