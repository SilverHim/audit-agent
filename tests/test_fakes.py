"""测试替身自身的契约: 假存储须忠实于开发接口文档第 4 节, 否则依赖它们的测试会掩盖真实 Store 下的错误."""

import asyncio
import csv
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from backend.contracts.data import TableKind
from backend.contracts.errors import VersionConflict
from backend.contracts.models import Phase, ProfileSource, TrailEntry, TrailKind
from backend.contracts.ports import AuditTrail
from backend.contracts.records import Account, BankAccount, JournalEntry
from backend.services.account_mapping import with_category
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_ACCOUNTS,
    NUMBAT_CONTRACTS,
    NUMBAT_D03_ROWS,
    NUMBAT_D04_ROWS,
    NUMBAT_PROFILE,
    NUMBAT_RECORDS,
    NUMBAT_ROWS,
    DocumentWrite,
    EngagementWrite,
    FakeEngagementStore,
    FakeEvidenceStore,
    FakeFinancialStore,
    FakeOutputStore,
    FakeTrail,
    bind_stores,
    csh02_window_rows,
    make_procedure,
    numbat_tables,
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


async def test_workflow_commits_exclude_only_independent_workpaper_refreshes() -> None:
    """主流程原子性视图只排除纯底稿事务, 混合事务须保留完整内容; 原始日志仍包含每次快照刷新."""
    from backend.contracts.models import Workpaper

    store = FakeOutputStore()
    paper = Workpaper(id="WP-D-3", ref="D-3", area="D", procedure_id="REV-08", basis=[])
    procedure = make_procedure("REV-08")
    await store.put(EID, [paper], actor="A6")
    await store.put(EID, [procedure], actor="A1")
    changed = paper.model_copy(update={"version": 2})
    async with store.transaction(EID):
        await store.put(EID, [changed], actor="A6")
        await store.put(EID, [procedure.model_copy(update={"version": 2})], actor="A1")
    assert len(store.commits) == 3
    assert store.commits[0] == [paper]
    assert store.workflow_commits == store.commits[1:]
    assert store.workflow_commits[-1][0] == changed
    expected = [procedure, procedure.model_copy(update={"version": 2})]
    assert store.workflow_rows == store.workflow_rows_of(EID) == expected
    assert store.rows == [paper, procedure, changed, expected[-1]]
    store.workflow_rows.clear()
    assert store.workflow_rows == expected
    assert store.workflow_rows_of("other-project") == []


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


def test_csh02_window_rows_drop_whole_voided_entries() -> None:
    """csh02_window_rows (Numbat 与 Xero 的假规则引擎共用) 剔除作废对的口径同 CSH-02 的 D03, D04 SQL (连库用例
    test_csh02_d03_d04_drop_voided_pairs 同一组行): Numbat 的分录加 VX-1 (06-29, 1010 借 700000 / 4200 贷 700000),
    冲回它的 VX-2 (只有 4200 一行说明为 "Reversal of ID 1", 1010 一行不匹配: 整张凭证剔除, 不按行) 与原凭证不存在的
    VX-3; 给格式与模板时 D03, D04 与不加行时整行相同, 不给时都含 (D03 只有 1010 的两行)."""
    added = [
        JournalEntry(
            id=line_id,
            entry_no=line_id.rsplit("-", 1)[0],
            posting_date=date(2026, 6, 29),
            created_at=None,
            created_by=None,
            approved_by=None,
            account_code=account,
            debit_cents=debit,
            credit_cents=credit,
            description=description,
            source_doc=None,
        )
        for line_id, account, debit, credit, description in (
            ("VX-1-1", "1010", 700000, 0, "Accrued interest"),
            ("VX-1-2", "4200", 0, 700000, "Accrued interest"),
            ("VX-2-1", "4200", 700000, 0, "Reversal of ID 1"),
            ("VX-2-2", "1010", 0, 700000, "Accrued interest reversal"),
            ("VX-3-1", "4200", 100, 0, "Reversal of ID 9"),  # 模板拼出的 VX-9 不存在: 只剔除它自己
        )
    ]
    lines = [*numbat_tables()[TableKind.journal_entries], *added]
    banks = [r for r in NUMBAT_ROWS if isinstance(r, BankAccount)]
    end = ENGAGEMENT.period_end
    voids = csh02_window_rows(lines, banks, end, 5, r"Reversal of ID (\d+)", "VX-{1}")  # type: ignore[arg-type]
    assert (voids["D03"], voids["D04"]) == (NUMBAT_D03_ROWS, NUMBAT_D04_ROWS)
    plain = csh02_window_rows(lines, banks, end, 5)  # type: ignore[arg-type]
    inserted = {line.id for line in added}
    assert {r["id"] for r in plain["D03"]} & inserted == {"VX-1-1", "VX-2-2"}
    assert {r["id"] for r in plain["D04"]} & inserted == inserted


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


async def trail_pages_hold(trail: AuditTrail, eid: str, other: str) -> None:
    """AuditTrail.list 的分页 (开发接口文档第 4 节), 假实现与真实实现 (tests/test_persistence_postgres.py) 共用:
    不带新参数时仍从最早一条起按写入顺序取; newest 取最近一页, 最新一条在内; 以上一页最早一条为 before 往前翻到
    最早一条, 翻页途中写入新留痕也不重不漏; kind 可为一类或一组 (如时间线只取显示的几类), 与 offset, 游标同用,
    游标可以是别类的留痕; before 是别的项目或不存在的 ID 时为空. 防时间线只取得到最早的若干条, 翻页重复或跳过,
    游标串到别的项目."""
    base = datetime(2026, 12, 15, 1, 0, tzinfo=UTC)

    async def write(target: str, n: int, minute: int) -> str:
        kind = (TrailKind.llm_call, TrailKind.decision, TrailKind.deferral)[n % 3]
        at = base + timedelta(minutes=minute)
        return await trail.record(
            TrailEntry(engagement_id=target, kind=kind, actor="A6", summary=f"#{n}", created_at=at)
        )

    for n, minute in enumerate([1, 2, 3, 4, 4, 5, 6], 1):  # #4 与 #5 同一时刻, 真实实现按 ID 排先后, 正好跨页
        await write(eid, n, minute)
    foreign = await write(other, 0, 3)  # 别的项目的, 写在后面: 当游标时不能取到本项目写在它之前的
    full = await trail.list(eid)  # 原有调用: 从最早一条起, 缺省 100 条
    assert sorted(e.summary for e in full) == [f"#{n}" for n in range(1, 8)]
    assert [e.created_at for e in full] == sorted(e.created_at for e in full if e.created_at)
    assert await trail.list(eid, limit=3, offset=2) == full[2:5]

    page = await trail.list(eid, limit=3, newest=True)
    assert page == full[-3:] and page[-1].summary == "#7"
    await write(eid, 8, 7)  # 翻页途中写入的新留痕
    pages = [page]
    while len(pages[0]) == 3:
        pages.insert(0, await trail.list(eid, limit=3, newest=True, before=pages[0][0].id))
    assert [len(p) for p in pages] == [1, 3, 3]
    assert [e for p in pages for e in p] == full  # 不重不漏, 新写入的不混进往前翻的页
    assert [e.summary for e in await trail.list(eid, limit=1, newest=True)] == ["#8"]
    assert await trail.list(eid, limit=2, before=full[4].id) == full[:2]  # before 不带 newest 时从最早一条起

    everything = await trail.list(eid)
    assert await trail.list(eid, TrailKind.llm_call) == [e for e in everything if e.kind is TrailKind.llm_call]
    shown = [TrailKind.decision, TrailKind.deferral]  # 一组: #1, #2, #4, #5, #7, #8
    visible = [e for e in everything if e.kind in shown]
    pages = [await trail.list(eid, shown, limit=2, newest=True)]
    while len(pages[0]) == 2:
        pages.insert(0, await trail.list(eid, shown, limit=2, newest=True, before=pages[0][0].id))
    assert ([len(p) for p in pages], [e for p in pages for e in p]) == ([0, 2, 2, 2], visible)
    assert await trail.list(eid, shown, limit=2, offset=1, newest=True) == visible[-3:-1]
    sixth = next(e.id for e in full if e.summary == "#6")  # llm_call, 不在这一组里, 照样作游标
    assert await trail.list(eid, shown, newest=True, before=sixth) == visible[:4]
    assert await trail.list(eid, []) == []
    for newest in (False, True):
        assert await trail.list(eid, newest=newest, before=foreign) == []
        assert await trail.list(eid, newest=newest, before="TR-none") == []


async def test_fake_trail_pages() -> None:
    await trail_pages_hold(FakeTrail(), EID, "ENG-OTHER")
