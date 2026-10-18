"""需求 R16: 账表查看, 科目分类修改 (历史, 留痕, 数据版本), 程序过期标记; 存储用假实现."""

from datetime import UTC, datetime
from typing import Any

import pytest

from backend.contracts.data import ApprovalLogRow, TableKind
from backend.contracts.errors import DataRejected, DecisionRejected
from backend.contracts.models import (
    GateVerdict,
    Phase,
    ProcedureRun,
    RunStatus,
    RunTrigger,
    TrailEntry,
    TrailKind,
)
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.query import GraphStateQuery, stale
from backend.services.data_service import DataService
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    FakeDataStore,
    FakeEngagementStore,
    FakeTrail,
    api_client,
    make_runner,
    wait_idle,
)
from tests.test_data_checkup import service_for
from tests.test_decisions import acc01_decision

EID = ENGAGEMENT.id
BASE = f"/api/v1/engagements/{EID}/data"
REASON = "运费按合同由我方承担, 属于销售成本"


def edits(runner: Any) -> list[TrailEntry]:
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e for e in trail.entries if e.kind is TrailKind.data and e.payload["action"] == "edit"]


# ── 查看 ──


async def test_accounts_page_has_balances_category_and_search() -> None:
    """科目表: 按代码排序, 带两期余额, 分类与依据; 按代码或名称搜索 (不分大小写); 分页."""
    _, service, _ = service_for(FakeDataStore())

    page = await service.table_page(EID, TableKind.accounts, None, 0, 50)
    found = await service.table_page(EID, TableKind.accounts, "FREIGHT", 0, 50)
    second = await service.table_page(EID, TableKind.accounts, None, 20, 10)

    assert page.total == 23 and len(page.rows) == 23
    assert [c.name for c in page.columns] == [
        "code",
        "name",
        "type",
        "category",
        "category_basis",
        "balance_current_cents",
        "balance_prior_cents",
        "edited",
    ]
    assert [c.type for c in page.columns][-3:] == ["amount", "amount", "bool"]
    assert [r["code"] for r in page.rows] == sorted(r["code"] for r in page.rows)
    assert found.total == 1 and found.rows == [
        {
            "code": "6300",
            "name": "Freight outwards",
            "type": "expense",
            "category": "operating_expenses",
            "category_basis": "name",
            "balance_current_cents": 24000000,
            "balance_prior_cents": 20500000,
            "edited": False,
        }
    ]
    assert second.total == 23 and len(second.rows) == 3


async def test_journal_page_columns_follow_import_fields_and_limit_is_capped() -> None:
    """总账: 列同导入字段 (名称与类型), 按入账日期, 凭证号排序, 搜索文本列; limit 超过 200 为 422."""
    runner, service, _ = service_for(FakeDataStore())

    page = await service.table_page(EID, TableKind.journal_entries, "koala", 0, 5)
    async with api_client(runner) as client:
        client._transport.app.state.data_service = service  # type: ignore[attr-defined]
        ok = await client.get(f"{BASE}/tables/trial_balance", params={"limit": 200})
        too_many = await client.get(f"{BASE}/tables/trial_balance", params={"limit": 201})
        unknown = await client.get(f"{BASE}/tables/ledger")

    assert [(c.name, c.type) for c in page.columns][:4] == [
        ("id", "text"),
        ("entry_no", "text"),
        ("posting_date", "date"),
        ("created_at", "datetime"),
    ]
    assert page.total > 0 and all("KOALA" in str(r["description"]).upper() for r in page.rows)
    assert [(r["posting_date"], r["entry_no"]) for r in page.rows] == sorted(
        (r["posting_date"], r["entry_no"]) for r in page.rows
    )
    body = ok.json()
    assert (ok.status_code, body["total"], len(body["rows"])) == (200, 46, 46)
    assert body["rows"][0] == {"period": "current", "account_code": "1010", "balance_cents": 125840000}
    assert too_many.status_code == 422 and unknown.status_code == 422


async def test_approval_log_page_shows_latest_version() -> None:
    """审批日志不成表: 查看取最新一版的行快照."""
    data = FakeDataStore()
    _, service, _ = service_for(data)
    rows = [ApprovalLogRow(entry_no="E-1", approved_by="cfo"), ApprovalLogRow(entry_no="E-2", created_by="ap")]
    async with service.holding(EID, ["approval_log"]):
        await service.write_tables(EID, {TableKind.approval_log: rows}, source="import", actor="U-1")

    page = await service.table_page(EID, TableKind.approval_log, None, 0, 50)

    assert page.total == 2
    assert page.rows == [
        {"entry_no": "E-1", "created_by": None, "approved_by": "cfo", "approved_at": None},
        {"entry_no": "E-2", "created_by": "ap", "approved_by": None, "approved_at": None},
    ]


# ── 科目分类修改 ──


async def test_freight_reclassified_to_cost_of_sales() -> None:
    """验收: 把 6300 Freight outwards 从 operating_expenses 改为 cost_of_sales, 历史为两版, 留痕一条, 数据版本 +1;
    查看时为已改, 分类依据 auditor."""
    data = FakeDataStore()
    runner, service, _ = service_for(data)
    before = data.data_version
    async with api_client(runner) as client:
        client._transport.app.state.data_service = service  # type: ignore[attr-defined]
        changed = await client.put(f"{BASE}/accounts/6300", json={"category": "cost_of_sales", "reason": REASON})
        history = (await client.get(f"{BASE}/accounts/6300/history")).json()
        page = (await client.get(f"{BASE}/tables/accounts", params={"q": "6300"})).json()

    assert changed.status_code == 200
    assert {k: changed.json()[k] for k in ("version", "category", "basis", "changed_by")} == {
        "version": 2,
        "category": "cost_of_sales",
        "basis": "auditor",
        "changed_by": "U-1",
    }
    assert [(v["version"], v["category"], v["basis"], v["changed_by"], v["reason"]) for v in history] == [
        (1, "operating_expenses", "name", "A3", None),
        (2, "cost_of_sales", "auditor", "U-1", REASON),
    ]
    [entry] = edits(runner)
    assert (entry.actor, entry.idempotency_key) == ("U-1", f"{EID}:data:edit:account:6300:v2")
    assert entry.payload == {
        "action": "edit",
        "table_kind": "accounts",
        "account_code": "6300",
        "field": "category",
        "category": "cost_of_sales",
        "reason": REASON,
    }
    assert data.data_version == before + 1
    [row] = page["rows"]
    assert (row["category"], row["category_basis"], row["edited"]) == ("cost_of_sales", "auditor", True)
    assert data.last_run is not None and data.last_run.data_version == data.data_version  # 改完由 A3 体检


async def test_history_of_untouched_account_is_its_current_category() -> None:
    """数据集载入后没改过的科目: 历史以当前分类作唯一一版; 不存在的科目 404."""
    runner, service, _ = service_for(FakeDataStore())

    history = await service.account_history(EID, "1010")
    async with api_client(runner) as client:
        client._transport.app.state.data_service = service  # type: ignore[attr-defined]
        missing = await client.get(f"{BASE}/accounts/9999/history")

    assert [(v.version, v.category, v.basis, v.changed_by) for v in history] == [(1, "cash", "name", "A3")]
    assert missing.status_code == 404


async def test_category_edit_validation() -> None:
    """理由为空 MISSING_FIELD; 分类不属于该科目的类别, 或与当前相同 INVALID_VALUE; 科目不存在 404; 分类不认识 422."""
    runner, service, _ = service_for(FakeDataStore())
    with pytest.raises(DataRejected) as empty:
        await service.set_account_category(EID, "6300", "cost_of_sales", "  ", "U-1")
    with pytest.raises(DataRejected) as wrong_group:
        await service.set_account_category(EID, "6300", "cash", REASON, "U-1")
    with pytest.raises(DataRejected) as same:
        await service.set_account_category(EID, "6300", "operating_expenses", REASON, "U-1")
    with pytest.raises(LookupError):
        await service.set_account_category(EID, "9999", "cash", REASON, "U-1")
    async with api_client(runner) as client:
        client._transport.app.state.data_service = service  # type: ignore[attr-defined]
        bad = await client.put(f"{BASE}/accounts/6300", json={"category": "freight", "reason": REASON})

    assert (empty.value.code, wrong_group.value.code, same.value.code) == (
        "MISSING_FIELD",
        "INVALID_VALUE",
        "INVALID_VALUE",
    )
    assert bad.status_code == 422 and not edits(runner)


async def test_category_edit_retry_returns_same_version_and_lock_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """留痕写失败后同一修改重试: 返回那一版, 补写留痕一条, 不多出版本; 换理由不算重试. 项目忙 409, 锁定 423."""
    data = FakeDataStore()
    runner, service, _ = service_for(data)
    trail = runner.deps.trail
    write = trail.record

    async def no_edit_trail(entry: TrailEntry) -> str:
        if entry.payload.get("action") == "edit":
            raise RuntimeError("留痕写失败")
        return await write(entry)

    monkeypatch.setattr(trail, "record", no_edit_trail)
    with pytest.raises(RuntimeError):
        await service.set_account_category(EID, "6300", "cost_of_sales", REASON, "U-1")
    monkeypatch.setattr(trail, "record", write)
    assert data.last_run is not None  # 留痕失败也照常体检

    again = await service.set_account_category(EID, "6300", "cost_of_sales", REASON, "U-1")
    with pytest.raises(DataRejected):
        await service.set_account_category(EID, "6300", "cost_of_sales", "另一理由", "U-1")

    assert again.version == 2 and [v.version for v in (await service.account_history(EID, "6300"))] == [1, 2]
    assert len(edits(runner)) == 1
    runner.acquire(EID)
    with pytest.raises(DecisionRejected) as busy:
        await service.set_account_category(EID, "6310", "cost_of_sales", REASON, "U-1")
    runner.release(EID)
    assert busy.value.code == "ENGAGEMENT_BUSY"
    engagements = runner.deps.engagement_store
    assert isinstance(engagements, FakeEngagementStore)
    await engagements.update(EID, locked=True)
    with pytest.raises(DataRejected) as locked:
        await service.set_account_category(EID, "6400", "cost_of_sales", REASON, "U-1")
    assert locked.value.code == "ENGAGEMENT_LOCKED"


# ── 过期标记 ──


def run(version: int | None, run_no: int = 1) -> ProcedureRun:
    return ProcedureRun(
        id=f"RUN-PLN-07-{run_no}",
        procedure_id="PLN-07",
        run_no=run_no,
        trigger=RunTrigger.initial,
        params_snapshot={},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        status=RunStatus.succeeded,
        data_version=version,
    )


def test_stale_rule() -> None:
    """过期: 最近一次执行记了数据版本且比当前旧; 没执行过, 执行时没记版本的不算."""
    assert stale(run(1), 2) is True
    assert stale(run(2), 2) is False
    assert stale(run(None), 2) is False
    assert stale(None, 2) is False


async def test_procedures_become_stale_after_category_edit() -> None:
    """进 P1 后各程序的执行记下数据版本; 改科目分类 (数据版本 +1) 后, 总览里已执行的程序标 stale, 没执行的不标."""
    runner, store = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]))
    await runner.start(EID)
    await wait_idle(runner, EID)
    await GraphDecisionSubmit(runner.app, runner.deps, runner).submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    query = GraphStateQuery(runner.app, runner.deps, runner)
    before = {p.id: p.stale for p in (await query.overview(EID)).procedures}
    runs = [r for r in store.rows if isinstance(r, ProcedureRun)]
    library = runner.deps.library
    p0 = {r.procedure_id for r in runs if library.load(r.procedure_id).phase is Phase.P0_Acceptance}
    assert p0 and {r.data_version for r in runs if r.procedure_id in p0} == {None}  # 承接阶段不读财务数据, 不记
    assert {r.data_version for r in runs if r.procedure_id not in p0} == {1}

    service = DataService(runner.deps, runner)
    await service.set_account_category(EID, "6300", "cost_of_sales", REASON, "U-1")
    after = {p.id: p.stale for p in (await query.overview(EID)).procedures}

    executed = {r.procedure_id for r in runs} - p0
    assert not any(before.values())
    assert {pid for pid, flag in after.items() if flag} == executed
