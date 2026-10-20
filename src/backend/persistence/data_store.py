"""DataStore 的 PostgreSQL 实现: 数据版本与就绪确认, 账表的版本写入, 导入会话, 统计.

方法签名见 contracts/ports.py, 表见开发接口文档第 9 节. 账表写入 (write_tables) 是 "新数据版本写入" 的唯一入口,
导入 (R13) 与 Xero 同步 (R12) 共用.
"""

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import ColumnElement, Text, delete, false, func, insert, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from backend.contracts.data import (
    ApprovalLogRow,
    CategoryVersion,
    CheckId,
    CheckInputs,
    CheckRun,
    DataChange,
    DataCheck,
    DataInput,
    DataReadiness,
    DataRequest,
    DataSource,
    DocumentChange,
    ExtractionRecord,
    ImportBatch,
    ImportSession,
    TableKind,
    TableOrigin,
    TableStats,
    TableVersion,
    table_changes,
)
from backend.contracts.models import AgentId, DocumentType
from backend.contracts.records import Account, AccountCategory, JournalEntry
from backend.persistence import models as m
from backend.persistence.db import transaction, writable
from backend.persistence.stores import (
    Store,
    now,
    record_changes,
    safe_child,
    upsert_extraction,
    write_contract,
    write_document,
)
from backend.services.data_quality import PLACEHOLDER_PREPARERS, request_order

# 各表的日期列, 概览据此给日期范围; 科目表, 试算表, 银行账户与工资册 (入职日不是数据期间) 没有
DATE_COLUMNS: dict[TableKind, str] = {
    TableKind.journal_entries: "posting_date",
    TableKind.sales_invoices: "invoice_date",
    TableKind.supplier_bills: "bill_date",
    TableKind.bank_transactions: "txn_date",
    TableKind.delivery_notes: "shipped_date",
}


SYSTEM = AgentId.A3  # 系统归类科目时分类历史记的修改人: 数据管理智能体
# 账表查看的排序列, 末尾都是唯一键, 分页不重不漏 (审批日志按行快照的顺序)
TABLE_ORDER: dict[TableKind, tuple[str, ...]] = {
    TableKind.accounts: ("code",),
    TableKind.trial_balance: ("account_code", "period"),
    TableKind.journal_entries: ("posting_date", "entry_no", "id"),
    TableKind.sales_invoices: ("invoice_date", "invoice_no", "id"),
    TableKind.supplier_bills: ("bill_date", "id"),
    TableKind.bank_accounts: ("id",),
    TableKind.bank_transactions: ("txn_date", "id"),
    TableKind.delivery_notes: ("delivery_no", "id"),
    TableKind.employees: ("employee_id",),
}
SEARCH_COLUMNS: dict[TableKind, tuple[str, ...]] = {TableKind.accounts: ("code", "name")}  # 其余表查各文本列


def contains(q: str) -> str:
    """ILIKE 的包含匹配模式 (转义 %, _ 与反斜杠, 配合 escape="\\")."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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


def approval_values(raw: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """审批日志一行 (行快照) 要补进总账分录的: (凭证号, 导入时对应了的列: 录入人, 审批人, 审批时间). apply_approvals
    与 with_approvals 共用, 库里与内存中按同一口径补."""
    row = ApprovalLogRow.model_validate(raw)
    return row.entry_no, row.model_dump(exclude_unset=True, exclude={"entry_no"})


async def apply_approvals(conn: AsyncConnection, eid: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """审批日志按凭证号补进总账分录: 每行只改导入时对应了的列 (录入人, 审批人, 审批时间); 总账里没有的凭证号忽略."""
    j = m.FINANCIAL["journal_entries"]
    for raw in rows:
        entry_no, values = approval_values(raw)
        if values:
            statement = update(j).where(j.c.engagement_id == eid, j.c.entry_no == entry_no).values(**values)
            await conn.execute(statement)


def with_approvals(lines: Sequence[JournalEntry], rows: Sequence[Mapping[str, Any]]) -> list[JournalEntry]:
    """总账分录行按审批日志 (行快照, 按日志顺序, 同 apply_approvals) 补上录入人, 审批人与审批时间后的样子, 不读写库.
    更正分录载入比较 "同 id 同内容" 时用: 写入总账时会套用最新一版审批日志 (需求 R271 审核 R271-01)."""
    result = list(lines)
    for raw in rows:
        entry_no, values = approval_values(raw)
        if values:
            result = [x.model_copy(update=values) if x.entry_no == entry_no else x for x in result]
    return result


class PostgresDataStore(Store):
    """数据管理的存储. root 为暂存导入原文件的根目录 (组装根传证据根目录), 文件放 {root}/{eid}/imports/{导入 ID}."""

    def __init__(self, root: Path, engine: AsyncEngine | None = None) -> None:
        super().__init__(engine)
        self.root = root

    async def readiness(self, eid: str) -> DataReadiness:
        t = m.dataset_loads
        rows = await self.rows(eid, select(*(t.c[name] for name in DataReadiness.model_fields)))
        return DataReadiness.model_validate(rows[0]) if rows else DataReadiness(data_version=0)

    async def confirm_ready(
        self, eid: str, actor: str, *, reason: str | None = None, failed_checks: Sequence[CheckId] = ()
    ) -> DataReadiness:
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            await self.loads_row(conn, eid)
            t = m.dataset_loads
            failed = [check.value for check in failed_checks]
            await conn.execute(
                update(t)
                .where(t.c.engagement_id == eid, t.c.ready_at.is_(None))
                .values(ready_at=now(), ready_by=actor, ready_reason=reason, ready_failed_checks=failed)
            )
            if failed:  # 第一次确认时没有未通过项: 补记本次的理由与未通过项, 人与时间不改
                empty = func.jsonb_array_length(t.c.ready_failed_checks) == 0
                await conn.execute(
                    update(t)
                    .where(t.c.engagement_id == eid, empty)
                    .values(ready_reason=reason, ready_failed_checks=failed)
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

    async def document_counts(self, eid: str) -> dict[DocumentType, int]:
        t = m.documents
        rows = await self.rows(eid, select(t.c.type, func.count().label("n")).group_by(t.c.type))
        return {DocumentType(r["type"]): int(r["n"]) for r in rows}

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

    # ── 体检与补交请求 (需求 R15) ──

    async def check_inputs(
        self, eid: str, period_start: date, period_end: date, *, adjustment_prefix: str | None = None
    ) -> CheckInputs:
        j = m.FINANCIAL[TableKind.journal_entries]
        tb = m.FINANCIAL[TableKind.trial_balance]
        a = m.FINANCIAL[TableKind.accounts]
        totals = select(
            func.count(),
            func.coalesce(func.sum(j.c.debit_cents), 0),
            func.coalesce(func.sum(j.c.credit_cents), 0),
            func.count().filter(j.c.created_by.is_not(None), j.c.created_by.not_in(sorted(PLACEHOLDER_PREPARERS))),
            func.count().filter(j.c.approved_by.is_not(None), j.c.approved_by != ""),
        )
        counted: list[ColumnElement[bool]] = [j.c.posting_date.between(period_start, period_end)]
        if adjustment_prefix:  # 审计更正分录不计发生额 (见 DataStore.check_inputs)
            counted.append(~j.c.entry_no.startswith(adjustment_prefix, autoescape=True))
        movements = (
            select(j.c.account_code, func.sum(j.c.debit_cents - j.c.credit_cents))
            .where(*counted)
            .group_by(j.c.account_code)
        )
        balances = select(tb.c.period, tb.c.account_code, func.sum(tb.c.balance_cents)).group_by(
            tb.c.period, tb.c.account_code
        )
        async with transaction(self.engine, eid) as conn:
            rows, debit, credit, preparers, approvers = (await conn.execute(totals)).one()
            moved = {code: int(value) for code, value in (await conn.execute(movements)).tuples()}
            periods: dict[str, dict[str, int]] = {"current": {}, "prior": {}}
            for period, code, value in (await conn.execute(balances)).tuples():
                periods[period][code] = int(value)
            chart = (await conn.execute(select(a.c.code, a.c.type, a.c.category))).tuples().all()
            dataset = (await conn.execute(select(m.dataset_loads.c.dataset))).scalar()
        return CheckInputs(
            journal_rows=int(rows),
            debit_cents=int(debit),
            credit_cents=int(credit),
            movements=moved,
            preparer_rows=int(preparers),
            approver_rows=int(approvers),
            tb_current=periods["current"],
            tb_prior=periods["prior"],
            account_types={code: kind for code, kind, _ in chart},
            account_categories={code: category for code, _, category in chart},
            dataset=dataset,
        )

    async def check_run(self, eid: str) -> CheckRun | None:
        t = m.data_checks
        rows = await self.rows(eid, select(t))
        if not rows:
            return None
        order = list(CheckId)
        checks = sorted(
            (DataCheck.model_validate({k: r[k] for k in DataCheck.model_fields}) for r in rows),
            key=lambda c: order.index(c.id),
        )
        return CheckRun(data_version=rows[0]["data_version"], checked_at=rows[0]["checked_at"], checks=checks)

    async def requests(self, eid: str) -> list[DataRequest]:
        t = m.data_requests
        rows = await self.rows(eid, select(t))
        found = [DataRequest.model_validate({k: r[k] for k in DataRequest.model_fields}) for r in rows]
        return sorted(found, key=request_order)

    # ── 账表查看与科目分类 (需求 R16) ──

    async def table_rows(
        self, eid: str, kind: TableKind, q: str | None, offset: int, limit: int
    ) -> tuple[int, list[dict[str, Any]]]:
        if kind is TableKind.approval_log:
            return await self.approval_rows(eid, q, offset, limit)
        table = m.FINANCIAL[kind]
        columns = [c for c in table.columns if c.name != "engagement_id"]
        names = SEARCH_COLUMNS.get(kind) or tuple(c.name for c in columns if isinstance(c.type, Text))
        condition = or_(*(table.c[n].ilike(contains(q), escape="\\") for n in names)) if q else true()
        statement = select(*columns)
        if kind is TableKind.accounts:
            tb = m.FINANCIAL[TableKind.trial_balance]

            def balance(period: str) -> Any:
                total = func.sum(tb.c.balance_cents).label("cents")
                grouped = select(tb.c.account_code, total).where(tb.c.period == period).group_by(tb.c.account_code)
                return grouped.subquery()

            current, prior = balance("current"), balance("prior")
            edited = func.coalesce(table.c.category_basis == "auditor", false()).label("edited")
            joined = table.outerjoin(current, current.c.account_code == table.c.code).outerjoin(
                prior, prior.c.account_code == table.c.code
            )
            statement = select(
                *columns,
                current.c.cents.label("balance_current_cents"),
                prior.c.cents.label("balance_prior_cents"),
                edited,
            ).select_from(joined)
        order = [table.c[n] for n in TABLE_ORDER[kind]]
        async with transaction(self.engine, eid) as conn:
            total = (await conn.execute(select(func.count()).select_from(table).where(condition))).scalar_one()
            found = await conn.execute(statement.where(condition).order_by(*order).offset(offset).limit(limit))
            rows = [dict(r) for r in found.mappings()]
        for row in rows:  # SUM 读回为 Decimal
            for key in ("balance_current_cents", "balance_prior_cents"):
                if row.get(key) is not None:
                    row[key] = int(row[key])
        return int(total), rows

    async def approval_rows(self, eid: str, q: str | None, offset: int, limit: int) -> tuple[int, list[dict[str, Any]]]:
        """审批日志最新一版的行快照, 按 q 查各列 (不分大小写的包含), 再分页."""
        t = m.data_imports
        statement = (
            select(t.c.rows)
            .where(t.c.kind == TableKind.approval_log, t.c.status == "committed")
            .order_by(t.c.data_version.desc())
            .limit(1)
        )
        found = await self.rows(eid, statement)
        rows: list[dict[str, Any]] = list(found[0]["rows"] or []) if found else []
        if q:
            needle = q.casefold()
            rows = [r for r in rows if any(needle in str(v).casefold() for v in r.values() if v is not None)]
        return len(rows), rows[offset : offset + limit]

    async def account(self, eid: str, code: str) -> Account | None:
        a = m.FINANCIAL[TableKind.accounts]
        found = await self.rows(eid, select(a).where(a.c.code == code))
        return Account.model_validate({k: v for k, v in found[0].items() if k != "engagement_id"}) if found else None

    async def category_history(self, eid: str) -> dict[str, list[CategoryVersion]]:
        h = m.account_category_history
        found = await self.rows(eid, select(h).order_by(h.c.account_code, h.c.version))
        history: dict[str, list[CategoryVersion]] = {}
        for r in found:
            version = CategoryVersion.model_validate({k: r[k] for k in CategoryVersion.model_fields})
            history.setdefault(r["account_code"], []).append(version)
        return history

    async def set_category(
        self, eid: str, code: str, category: AccountCategory, *, actor: str, reason: str
    ) -> CategoryVersion:
        a, h = m.FINANCIAL[TableKind.accounts], m.account_category_history
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            loads = await self.loads_row(conn, eid)  # 行锁: 同一项目的数据写入排队
            row = (await conn.execute(select(a).where(a.c.code == code).with_for_update())).mappings().first()
            if row is None:
                raise LookupError(f"科目 {code} 不存在")
            latest = (await latest_categories(conn, eid)).get(code)
            version = latest.version + 1 if latest is not None else 2
            if latest is None:  # 数据集载入的科目没有历史: 先补记系统的归类
                base = {"category": row["category"], "basis": row["category_basis"], "changed_by": SYSTEM}
                when = loads["loaded_at"] or now()
                await conn.execute(
                    insert(h).values(engagement_id=eid, account_code=code, version=1, changed_at=when, **base)
                )
            changed = CategoryVersion(
                version=version, category=category, basis="auditor", changed_by=actor, changed_at=now(), reason=reason
            )
            await conn.execute(insert(h).values(engagement_id=eid, account_code=code, **changed.model_dump()))
            await conn.execute(update(a).where(a.c.code == code).values(category=category, category_basis="auditor"))
            t = m.dataset_loads
            version = int(loads["data_version"]) + 1
            await conn.execute(update(t).where(t.c.engagement_id == eid).values(data_version=version))
            await record_changes(conn, eid, version, [DataInput.accounts])  # 科目分类在科目表上
        return changed

    async def mark_unavailable(
        self, eid: str, request_id: str, *, reason: str, actor: str, at: datetime
    ) -> DataRequest | None:
        t = m.data_requests
        values = {"status": "unavailable", "unavailable_reason": reason, "resolved_by": actor, "resolved_at": at}
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            row = (
                (
                    await conn.execute(
                        update(t)
                        .where(t.c.engagement_id == eid, t.c.id == request_id, t.c.status == "open")
                        .values(values)
                        .returning(*t.c)
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else DataRequest.model_validate({k: row[k] for k in DataRequest.model_fields})

    async def save_check_run(self, eid: str, run: CheckRun, requests: Sequence[DataRequest]) -> None:
        checks, table = m.data_checks, m.data_requests
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            await conn.execute(delete(checks).where(checks.c.engagement_id == eid))
            if run.checks:
                values = [
                    {
                        "engagement_id": eid,
                        **check.model_dump(mode="json"),
                        "data_version": run.data_version,
                        "checked_at": run.checked_at,
                    }
                    for check in run.checks
                ]
                await conn.execute(insert(checks), values)
            for request in requests:
                row = request.model_dump()
                statement = upsert(table).values(engagement_id=eid, **row)
                await conn.execute(statement.on_conflict_do_update(index_elements=["engagement_id", "id"], set_=row))

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
        origins: Mapping[TableKind, TableOrigin] | None = None,
    ) -> int:
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            return await self._write_tables(
                conn,
                eid,
                tables,
                source=source,
                actor=actor,
                file_name=file_name,
                document_id=document_id,
                import_id=import_id,
                request_id=request_id,
                origins=origins,
            )

    async def _write_tables(
        self,
        conn: AsyncConnection,
        eid: str,
        tables: Mapping[TableKind, Sequence[BaseModel]],
        *,
        source: DataSource,
        actor: str,
        file_name: str | None = None,
        document_id: str | None = None,
        import_id: str | None = None,
        request_id: str | None = None,
        origins: Mapping[TableKind, TableOrigin] | None = None,
    ) -> int:
        """write_tables 在已开的事务里的写入 (智能导入的确认在同一事务里另写文件与批次), 返回新的数据版本."""
        version = await self._bump(conn, eid, table_changes(tables))
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
            origin = (origins or {}).get(kind) or TableOrigin(
                file_name=file_name, document_id=document_id, request_id=request_id
            )
            values: dict[str, Any] = {
                "kind": kind.value,
                "source": source,
                "document_id": origin.document_id,
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
                        file_name=origin.file_name,
                        request_id=origin.request_id,
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

    # ── 智能导入的批次 (需求 R277) ──

    async def save_batch(self, eid: str, batch: ImportBatch) -> None:
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            await self._save_batch(conn, eid, batch)

    @staticmethod
    async def _save_batch(conn: AsyncConnection, eid: str, batch: ImportBatch) -> None:
        """在已开的事务里登记或覆盖一次智能导入."""
        data = batch.model_dump(mode="json")
        values: dict[str, Any] = {c.name: data[c.name] for c in m.import_batches.c if c.name != "engagement_id"}
        values["created_at"], values["committed_at"] = batch.created_at, batch.committed_at
        statement = upsert(m.import_batches).values(engagement_id=eid, **values)
        await conn.execute(statement.on_conflict_do_update(index_elements=["engagement_id", "id"], set_=values))

    async def batch(self, eid: str, batch_id: str) -> ImportBatch | None:
        b = m.import_batches
        rows = await self.rows(eid, select(b).where(b.c.id == batch_id))
        return ImportBatch.model_validate({k: v for k, v in rows[0].items() if k != "engagement_id"}) if rows else None

    async def batches(self, eid: str) -> list[ImportBatch]:
        b = m.import_batches
        return [
            ImportBatch.model_validate({k: v for k, v in r.items() if k != "engagement_id"})
            for r in await self.rows(eid, select(b).order_by(b.c.created_at, b.c.id))
        ]

    async def commit_batch(
        self,
        eid: str,
        batch: ImportBatch,
        tables: Mapping[TableKind, Sequence[BaseModel]],
        *,
        actor: str,
        origins: Mapping[TableKind, TableOrigin],
        documents: Sequence[DocumentChange],
    ) -> ImportBatch:
        b = m.import_batches
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            # 行锁: 同一批次的确认排队; 已 committed 的是重试, 什么都不写
            row = (await conn.execute(select(b).where(b.c.id == batch.id).with_for_update())).mappings().first()
            if row is None:
                raise LookupError(f"批次 {batch.id} 不存在")
            stored = ImportBatch.model_validate({k: v for k, v in row.items() if k != "engagement_id"})
            if stored.status == "committed":
                return stored
            version = (
                await self._write_tables(conn, eid, tables, source="import", actor=actor, origins=origins)
                if tables
                else None
            )
            # 程序执行之外写进的合同新版本与 ASIC 摘录的抽取结果, 同上传后的抽取: 记数据变动 contracts 与
            # registry_extracts (需求 R274, R290); 有表时记在同一版本上
            extracted = [DataInput.contracts] if any(change.contracts for change in documents) else []
            if any(change.registry_extract is not None for change in documents):
                extracted.append(DataInput.registry_extracts)
            if extracted:
                if version is None:
                    version = await self._bump(conn, eid, extracted)
                else:
                    await record_changes(conn, eid, version, extracted)
            for change in documents:
                await write_document(
                    conn,
                    eid,
                    change.document_id,
                    type=change.type,
                    reliability=change.reliability,
                    status=change.status,
                    failed_step=change.failed_step,
                )
                for contract in change.contracts:
                    await write_contract(conn, eid, contract, changed_by=SYSTEM)
                if change.reply is not None:
                    reply = change.reply
                    await upsert_extraction(
                        conn, eid, "reply", change.document_id, 1, reply, changed_by=SYSTEM, field=None, reason=None
                    )
                if change.registry_extract is not None:  # 接在该文件已有的抽取版本之后 (同上传后的重抽)
                    t = m.extraction_versions
                    latest = select(func.max(t.c.version)).where(
                        t.c.engagement_id == eid,
                        t.c.target_type == "registry_extract",
                        t.c.target_id == change.document_id,
                    )
                    version_no = int((await conn.execute(latest)).scalar() or 0) + 1
                    await upsert_extraction(
                        conn,
                        eid,
                        "registry_extract",
                        change.document_id,
                        version_no,
                        change.registry_extract,
                        changed_by=SYSTEM,
                        field=None,
                        reason=None,
                    )
            committed = batch.model_copy(update={"data_version": version})
            await self._save_batch(conn, eid, committed)
        return committed

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

    async def bump_version(self, eid: str, input: DataInput) -> int:
        async with transaction(self.engine, eid) as conn:
            await writable(conn, eid)
            return await self._bump(conn, eid, [input])

    async def _bump(self, conn: AsyncConnection, eid: str, inputs: Iterable[DataInput]) -> int:
        """在已开的事务里把数据版本 +1 并记下这次变了哪些数据, 返回新版本 (行锁: 同一项目的写入排队, 版本号连续)."""
        loads = await self.loads_row(conn, eid)
        version = int(loads["data_version"]) + 1
        t = m.dataset_loads
        await conn.execute(update(t).where(t.c.engagement_id == eid).values(data_version=version))
        await record_changes(conn, eid, version, inputs)
        return version

    async def changes(self, eid: str) -> list[DataChange]:
        t = m.data_changes
        rows = await self.rows(eid, select(t.c.data_version, t.c.input).order_by(t.c.data_version))
        order = list(DataInput)
        found = [DataChange.model_validate(r) for r in rows]
        return sorted(found, key=lambda c: (c.data_version, order.index(c.input)))

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
