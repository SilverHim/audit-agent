"""DataStore 的 PostgreSQL 实现: 数据版本与就绪确认, 账表统计.

方法签名见 contracts/ports.py, 表见开发接口文档第 9 节.
"""

from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from backend.contracts.data import CheckId, DataCheck, DataReadiness, TableKind, TableStats, TableVersion
from backend.persistence import models as m
from backend.persistence.db import transaction, writable
from backend.persistence.stores import Store, now

# 各表的日期列, 概览据此给日期范围; 科目表, 试算表, 银行账户没有
DATE_COLUMNS: dict[TableKind, str] = {
    TableKind.journal_entries: "posting_date",
    TableKind.sales_invoices: "invoice_date",
    TableKind.bank_transactions: "txn_date",
    TableKind.delivery_notes: "shipped_date",
}


class PostgresDataStore(Store):
    """数据管理的存储."""

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
