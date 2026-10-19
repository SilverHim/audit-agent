"""需求 R11: 数据概览 GET /data/summary (数据源, 各账表, 体检, 进行中的作业数, 待补交请求数)."""

import asyncio
from datetime import UTC, date, datetime

import pytest

from backend.contracts.data import (
    CheckId,
    DataCheck,
    TableKind,
    TableStats,
    TableVersion,
    XeroSource,
)
from backend.services.data_service import DataService
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    FakeDataStore,
    FakeEngagementStore,
    api_client,
    make_runner,
)

EID = ENGAGEMENT.id
AT = datetime(2026, 10, 18, 9, tzinfo=UTC)


def version(
    kind: TableKind, source: str, data_version: int, records: int, file_name: str | None = None
) -> TableVersion:
    return TableVersion.model_validate(
        {
            "id": f"V-{kind}-{data_version}",
            "kind": kind,
            "source": source,
            "file_name": file_name,
            "document_id": None,
            "records": records,
            "data_version": data_version,
            "request_id": None,
            "created_by": "U-1",
            "created_at": AT.replace(hour=data_version),
        }
    )


class FixedXero:
    async def source(self, eid: str) -> XeroSource:
        return XeroSource(
            status="connected",
            organisation="Demo Company (AU)",
            last_synced_at=AT,
            scope=[TableKind.accounts, TableKind.journal_entries],
        )


def service(data: FakeDataStore, xero: FixedXero | None = None) -> DataService:
    runner, _ = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]), data=data)
    return DataService(runner.deps, runner, xero)


async def test_seeded_dataset_tables_have_no_source() -> None:
    """种子载入的项目: 有行的表为 received, 来源与文件名为空, 更新时间取首次载入时间; Xero 未连接."""
    data = FakeDataStore()

    summary = await service(data).summary(EID)

    assert summary.sources.xero.status == "not_connected"
    by_kind = {t.kind: t for t in summary.tables}
    assert list(by_kind) == list(TableKind)
    accounts = by_kind[TableKind.accounts]
    assert (accounts.source, accounts.file_name, accounts.status, accounts.updated_at) == (
        None,
        None,
        "received",
        data.ready_at,
    )
    assert by_kind[TableKind.trial_balance].records_detail is not None
    assert by_kind[TableKind.trial_balance].records_detail.model_dump() == {"current": 23, "prior": 23}
    assert all(by_kind[k].records_detail is None for k in TableKind if k is not TableKind.trial_balance)
    log = by_kind[TableKind.approval_log]
    assert (log.status, log.records, log.updated_at) == ("not_received", 0, None)
    assert (summary.checks, summary.processing, summary.open_requests) == ([], 0, 0)


async def test_latest_version_gives_source_file_and_time() -> None:
    data = FakeDataStore(
        ready=False,
        stats={
            TableKind.accounts: TableStats(records=24),
            TableKind.journal_entries: TableStats(
                records=900, period_from=date(2026, 7, 1), period_to=date(2026, 9, 30)
            ),
            TableKind.approval_log: TableStats(records=40),
        },
    )
    data.table_versions = [
        version(TableKind.accounts, "import", 1, 23, "coa-v1.csv"),
        version(TableKind.journal_entries, "xero", 2, 900),
        version(TableKind.accounts, "import", 3, 24, "coa-v2.xlsx"),
        version(TableKind.approval_log, "import", 4, 40, "approvals.csv"),
    ]

    summary = await service(data, FixedXero()).summary(EID)

    by_kind = {t.kind: t for t in summary.tables}
    accounts = by_kind[TableKind.accounts]
    assert (accounts.source, accounts.file_name, accounts.records, accounts.updated_at) == (
        "import",
        "coa-v2.xlsx",
        24,
        AT.replace(hour=3),
    )
    journals = by_kind[TableKind.journal_entries]
    assert (journals.source, journals.file_name, journals.period_from, journals.period_to) == (
        "xero",
        None,
        date(2026, 7, 1),
        date(2026, 9, 30),
    )
    log = by_kind[TableKind.approval_log]
    assert (log.status, log.records, log.source) == ("received", 40, "import")
    tb = by_kind[TableKind.trial_balance]
    assert (tb.status, tb.updated_at, tb.records_detail and tb.records_detail.current) == ("not_received", None, 0)
    assert summary.sources.xero.organisation == "Demo Company (AU)"
    assert summary.sources.xero.scope == [TableKind.accounts, TableKind.journal_entries]


async def test_jobs_mark_tables_converting_and_count_processing() -> None:
    data = FakeDataStore()
    svc = service(data)
    svc.jobs[EID] = {object(): frozenset({"journal_entries"}), object(): frozenset({"DOC-123"})}
    data.check_results = [DataCheck(id=CheckId.debits_equal_credits, status="passed", params={"debit_cents": 1})]
    data.open_count = 2

    summary = await svc.summary(EID)

    status = {t.kind: t.status for t in summary.tables}
    assert status[TableKind.journal_entries] == "converting"
    assert status[TableKind.accounts] == "received"
    assert (summary.processing, summary.open_requests) == (2, 2)
    assert [c.id for c in summary.checks] == [CheckId.debits_equal_credits]


async def test_summary_endpoint() -> None:
    runner, _ = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]))
    async with api_client(runner) as client:
        ok = await client.get(f"/api/v1/engagements/{EID}/data/summary")
        missing = await client.get("/api/v1/engagements/ENG-2026-999/data/summary")
    assert ok.status_code == 200
    body = ok.json()
    assert body["sources"]["xero"] == {
        "status": "not_connected",
        "organisation": None,
        "tenant_id": None,
        "last_synced_at": None,
        "scope": [],
        "syncing": False,
        "last_sync": None,
        "profile_check": None,
    }
    assert [t["kind"] for t in body["tables"]] == [k.value for k in TableKind]
    assert missing.status_code == 404


@pytest.mark.parametrize("finish_during", ["stats", "versions", "checks"])
async def test_job_finishing_mid_read_keeps_polling(finish_during: str) -> None:
    """读概览途中作业提交并注销 (Codex 审核 FD-R11 的探针): 要么 processing 仍计入它 (前端再轮询一次),
    要么返回的已是完成后的数据; 不能 processing 为 0 却给旧数据或 converting."""
    reached, committed = asyncio.Event(), asyncio.Event()

    class PausingStore(FakeDataStore):
        async def pause(self, step: str) -> None:
            if finish_during == step:
                reached.set()
                await committed.wait()

        async def stats(self, eid: str) -> dict[TableKind, TableStats]:
            await self.pause("stats")
            return await super().stats(eid)

        async def versions(self, eid: str) -> list[TableVersion]:
            await self.pause("versions")
            return await super().versions(eid)

        async def checks(self, eid: str) -> list[DataCheck]:
            await self.pause("checks")
            return await super().checks(eid)

    data = PausingStore(ready=False, stats={TableKind.accounts: TableStats(records=1)})
    svc = service(data)
    svc.jobs[EID] = {object(): frozenset({"accounts"})}

    async def finish_job() -> None:
        await reached.wait()
        data.table_stats[TableKind.accounts] = TableStats(records=42)
        data.table_versions = [version(TableKind.accounts, "import", 3, 42, "new.csv")]
        svc.jobs[EID].clear()
        committed.set()

    task = asyncio.create_task(finish_job())
    summary = await svc.summary(EID)
    await task

    accounts = next(t for t in summary.tables if t.kind is TableKind.accounts)
    assert summary.processing == 1  # 快照时作业还在: 前端会再取一次
    assert accounts.status == "converting"
    again = await svc.summary(EID)
    accounts = next(t for t in again.tables if t.kind is TableKind.accounts)
    assert (again.processing, accounts.records, accounts.status, accounts.file_name) == (0, 42, "received", "new.csv")
