"""测试替身自身的契约: 假存储须忠实于开发接口文档第 4 节, 否则依赖它们的测试会掩盖真实 Store 下的错误."""

import asyncio
import csv
from pathlib import Path

import pytest

from backend.contracts.errors import VersionConflict
from backend.contracts.models import Phase, ProfileSource
from backend.contracts.records import Account, JournalEntry
from backend.services.account_mapping import with_category
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_ACCOUNTS,
    NUMBAT_CONTRACTS,
    NUMBAT_PROFILE,
    NUMBAT_RECORDS,
    NUMBAT_ROWS,
    DocumentWrite,
    EngagementWrite,
    FakeEngagementStore,
    FakeEvidenceStore,
    FakeFinancialStore,
    FakeOutputStore,
    bind_stores,
    make_procedure,
)

EID = ENGAGEMENT.id
DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"
CLIENT = NUMBAT_PROFILE.id
PROFILE_V2 = NUMBAT_PROFILE.model_copy(update={"version": 2, "legal_name": "Numbat Electronics Pty Ltd"})


def bound_stores() -> tuple[FakeEngagementStore, FakeOutputStore]:
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    output_store = FakeOutputStore()
    bind_stores(engagement_store, output_store)
    return engagement_store, output_store


async def test_engagement_writes_staged_then_rolled_back() -> None:
    """事务块内改项目与存画像: 块内读得到, 块内抛错后项目, 画像与提交日志都恢复原样."""
    engagement_store, output_store = bound_stores()

    with pytest.raises(RuntimeError, match="提交前出错"):
        async with output_store.transaction(EID):
            await output_store.put(EID, [make_procedure("ACC-04")], actor="A1")
            await engagement_store.update(EID, phase=Phase.P1_Planning, locked=True)
            await engagement_store.save_profile(PROFILE_V2, source=ProfileSource.update, updated_by="U-1")
            staged = await engagement_store.get(EID)
            assert staged is not None and staged.phase is Phase.P1_Planning and staged.locked
            assert await engagement_store.client_profile(CLIENT) == PROFILE_V2
            raise RuntimeError("提交前出错")

    assert await engagement_store.get(EID) == ENGAGEMENT
    assert await engagement_store.client_profile(CLIENT) == NUMBAT_PROFILE
    assert engagement_store.profiles == [NUMBAT_PROFILE]
    assert output_store.rows == []
    assert output_store.commits == []


async def test_engagement_writes_committed_with_outputs() -> None:
    """块正常结束: 项目与画像改动生效, 与审计产出按调用顺序记进同一个提交."""
    engagement_store, output_store = bound_stores()
    procedure = make_procedure("ACC-04")

    async with output_store.transaction(EID):
        await output_store.put(EID, [procedure], actor="A1")
        await engagement_store.save_profile(PROFILE_V2, source=ProfileSource.update, updated_by="U-1")
        await engagement_store.update(EID, phase=Phase.P1_Planning)

    engagement = await engagement_store.get(EID)
    assert engagement is not None and engagement.phase is Phase.P1_Planning
    assert await engagement_store.client_profile(CLIENT) == PROFILE_V2
    assert output_store.commits == [
        [
            procedure,
            EngagementWrite("save_profile", CLIENT, {"version": 2}),
            EngagementWrite("update", EID, {"phase": Phase.P1_Planning}),
        ]
    ]


async def test_writes_outside_transaction_take_effect_at_once() -> None:
    engagement_store, output_store = bound_stores()

    await engagement_store.update(EID, phase=Phase.P1_Planning)

    engagement = await engagement_store.get(EID)
    assert engagement is not None and engagement.phase is Phase.P1_Planning
    assert output_store.commits == []  # 不在事务里的项目写入不记提交日志


async def test_child_task_is_not_in_parent_transaction() -> None:
    """事务块内新建的子任务继承了 contextvar, 但不在父任务的事务里: 读不到暂存, 自己的写入立即提交."""
    engagement_store, output_store = bound_stores()

    async def child() -> None:
        assert await output_store.get(EID, type(make_procedure("ACC-04")), "ACC-04") is None
        assert await engagement_store.client_profile(CLIENT) == NUMBAT_PROFILE
        await output_store.put(EID, [make_procedure("ACC-01")], actor="A1")

    async with output_store.transaction(EID):
        await output_store.put(EID, [make_procedure("ACC-04")], actor="A1")
        await engagement_store.save_profile(PROFILE_V2, source=ProfileSource.update, updated_by="U-1")
        await asyncio.create_task(child())
        assert [p.id for p in output_store.rows] == ["ACC-01"]  # 子任务的写入已提交, 父任务的还在暂存

    assert [p.id for p in output_store.rows] == ["ACC-01", "ACC-04"]


async def test_late_child_does_not_join_finished_transaction() -> None:
    """子任务等外层事务结束后才开事务: 开的是自己的新事务, 写入生效, 旧事务的提交日志不变."""
    _, output_store = bound_stores()
    outer_done = asyncio.Event()

    async def late_child() -> None:
        await outer_done.wait()
        async with output_store.transaction(EID):
            await output_store.put(EID, [make_procedure("ACC-01")], actor="A1")

    async with output_store.transaction(EID):
        await output_store.put(EID, [make_procedure("ACC-04")], actor="A1")
        child = asyncio.create_task(late_child())
    outer_commit = list(output_store.commits[0])
    outer_done.set()
    await child

    assert [p.id for p in output_store.rows] == ["ACC-04", "ACC-01"]
    assert output_store.commits[0] == outer_commit
    assert len(output_store.commits) == 2


async def test_nested_transaction_joins_outer() -> None:
    _, output_store = bound_stores()

    async with output_store.transaction(EID):
        await output_store.put(EID, [make_procedure("ACC-04")], actor="A1")
        async with output_store.transaction(EID):
            await output_store.put(EID, [make_procedure("ACC-01")], actor="A1")

    assert len(output_store.commits) == 1
    assert [p.id for p in output_store.commits[0]] == ["ACC-04", "ACC-01"]


async def test_outputs_isolated_by_engagement() -> None:
    """两个项目各有同 ID 的程序: 互不可见, 版本号各自从 1 起."""
    output_store = FakeOutputStore()

    await output_store.put("ENG-A", [make_procedure("ACC-04")], actor="A1")
    await output_store.put("ENG-B", [make_procedure("ACC-04")], actor="A1")

    assert [p.id for p in output_store.rows_of("ENG-A")] == ["ACC-04"]
    assert [p.id for p in output_store.rows_of("ENG-B")] == ["ACC-04"]
    assert await output_store.get("ENG-C", type(make_procedure("ACC-04")), "ACC-04") is None
    with pytest.raises(VersionConflict):
        await output_store.put("ENG-A", [make_procedure("ACC-04", version=3)], actor="A1")


async def test_writing_other_engagement_inside_transaction_is_an_error() -> None:
    _, output_store = bound_stores()

    with pytest.raises(AssertionError, match="ENG-OTHER"):
        async with output_store.transaction(EID):
            await output_store.put("ENG-OTHER", [make_procedure("ACC-04")], actor="A1")


async def test_document_status_joins_transaction() -> None:
    """证据文件状态在事务块内写入: 块正常结束才生效并记进同一次提交, 块内抛错则丢弃."""
    financial = FakeFinancialStore(list(NUMBAT_CONTRACTS))
    output_store = FakeOutputStore()
    evidence = FakeEvidenceStore(financial, output_store)
    doc = NUMBAT_CONTRACTS[0].id

    with pytest.raises(RuntimeError, match="提交前出错"):
        async with output_store.transaction(EID):
            await evidence.set_status(EID, doc, "extracted")
            raise RuntimeError("提交前出错")
    assert [d.status for d in await financial.documents(EID)] == ["received"] * 3

    async with output_store.transaction(EID):
        await evidence.set_status(EID, doc, "extracted")
        assert (await financial.documents(EID))[0].status == "received"  # 提交前不生效
    assert (await financial.documents(EID))[0].status == "extracted"
    assert output_store.commits == [[DocumentWrite(doc, "extracted")]]
    assert evidence.calls == [(doc, "extracted", True), (doc, "extracted", True)]


def demo_rows(name: str) -> list[dict[str, str | None]]:
    """data/demo 下一张表的行, 空单元格为 None (同 CsvDatasetLoader 的解析)."""
    with (DEMO / f"{name}.csv").open(newline="", encoding="utf-8") as stream:
        return [{k: (v if v != "" else None) for k, v in r.items()} for r in csv.DictReader(stream)]


def test_numbat_sales_entries_and_accounts_match_dataset() -> None:
    """假存储里的 Numbat 分录行与科目同 data/demo 载入所得, 默认测试与连库测试 (Postgres) 取到同样的行."""
    entries = {r["id"]: JournalEntry.model_validate(r) for r in demo_rows("journal_entries")}
    accounts = {r["code"]: with_category(Account.model_validate(r)) for r in demo_rows("accounts")}
    fake_entries = [r for r in NUMBAT_ROWS if isinstance(r, JournalEntry)]

    assert [r.id for r in fake_entries] == [
        "SJ-2026-0612-1",
        "SJ-2026-0612-2",
        "SJ-2026-0618-1",
        "SJ-2026-0618-2",
        "SJ-2026-0547-1",
        "SJ-2026-0547-2",
        "CR-SI-2026-0547-1",
        "CR-SI-2026-0547-2",
    ]
    assert [entries[r.id] for r in fake_entries] == fake_entries
    # 每笔都是整笔: data/demo 中同凭证号的行都在, 借贷平衡
    numbers = {r.entry_no for r in fake_entries}
    assert sorted(r.id for r in fake_entries) == sorted(i for i, r in entries.items() if r.entry_no in numbers)
    assert sum(r.debit_cents for r in fake_entries) == sum(r.credit_cents for r in fake_entries)
    assert [accounts["1100"], accounts["4100"]] == NUMBAT_ACCOUNTS


async def test_journal_entries_by_entry_no_or_source_doc() -> None:
    """分录行按凭证号或原始单据号取 (开发接口文档第 4 节): 满足任一即取, 不重复, 按行 ID 升序; 两个都不给抛错."""
    financial = FakeFinancialStore(records=dict(reversed(list(NUMBAT_RECORDS.items()))))  # 存入顺序不影响结果

    sale = await financial.journal_entries(EID, entry_nos=["SJ-2026-0612"])
    assert [(r.id, r.account_code, r.debit_cents, r.credit_cents) for r in sale] == [
        ("SJ-2026-0612-1", "1100", 14200000, 0),
        ("SJ-2026-0612-2", "4100", 0, 14200000),
    ]
    assert await financial.journal_entries(EID, source_docs=["SI-2026-0612"]) == sale
    assert await financial.journal_entries(EID, entry_nos=["SJ-2026-0612"], source_docs=["SI-2026-0612"]) == sale
    union = await financial.journal_entries(
        EID, entry_nos=["SJ-2026-0618", "SJ-2026-0612"], source_docs=["SI-2026-0547", "SI-2026-0612"]
    )
    assert [r.id for r in union] == [
        "CR-SI-2026-0547-1",
        "CR-SI-2026-0547-2",
        "SJ-2026-0547-1",
        "SJ-2026-0547-2",
        "SJ-2026-0612-1",
        "SJ-2026-0612-2",
        "SJ-2026-0618-1",
        "SJ-2026-0618-2",
    ]
    receipt = await financial.journal_entries(EID, entry_nos=["CR-SI-2026-0547"])  # 整笔收款分录: 借 1010 贷 1100
    assert [(r.account_code, r.debit_cents, r.credit_cents) for r in receipt] == [
        ("1010", 5000000, 0),
        ("1100", 0, 5000000),
    ]
    # 两个条件各管各的列: 单据号不当凭证号查, 反之亦然; 给了空列表就取不到行
    assert await financial.journal_entries(EID, entry_nos=["SI-2026-0612"]) == []
    assert await financial.journal_entries(EID, source_docs=["SJ-2026-0612"]) == []
    assert await financial.journal_entries(EID, entry_nos=[]) == []
    assert await financial.journal_entries(EID, source_docs=[]) == []
    assert await financial.journal_entries(EID, entry_nos=[], source_docs=[]) == []
    # 误传单个字符串时同真实实现逐字符拆开, 取不到行 (不做子串判断)
    assert await financial.journal_entries(EID, entry_nos="SJ-2026-0612") == []
    with pytest.raises(ValueError, match="不取全表"):
        await financial.journal_entries(EID)


async def test_accounts_by_code() -> None:
    """科目按代码取 (开发接口文档第 4 节): 按代码升序, 带标准分类; codes 为 None 取全部, 空列表取不到."""
    financial = FakeFinancialStore(accounts=list(reversed(NUMBAT_ACCOUNTS)))

    picked = await financial.accounts(EID, ["4100", "1100"])
    assert [(a.code, a.name, a.type, a.category) for a in picked] == [
        ("1100", "Trade receivables", "asset", "receivables"),
        ("4100", "Sales revenue", "revenue", "revenue"),
    ]
    assert await financial.accounts(EID) == picked
    assert await financial.accounts(EID, ["4100"]) == picked[1:]
    assert await financial.accounts(EID, []) == []
    assert await financial.accounts(EID, ["9999"]) == []
    assert await financial.accounts(EID, "1100") == []  # 单个字符串逐字符拆开, 同真实实现
