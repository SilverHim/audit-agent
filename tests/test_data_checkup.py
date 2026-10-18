"""需求 R15: A3 数据体检与补交请求 (规则, 时机, 自动转 received, 稍后处理, 留痕补写); 存储用假实现."""

import asyncio
import csv
import io
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from backend.adapters.xero_dataset import CREATED_BY as XERO_PREPARER
from backend.adapters.xero_dataset import build_tables, snapshot_period
from backend.agents.checkup import DataCheckup, check_entry, request_entry
from backend.contracts.data import (
    CheckId,
    CheckInputs,
    DataRequest,
    ImportCommit,
    TableKind,
    TableVersion,
)
from backend.contracts.models import DocumentType, GateVerdict, TrailEntry, TrailKind
from backend.contracts.records import Account, JournalEntry, TrialBalanceRow
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.services.data_quality import reconcile, run_checks, summarize
from backend.services.data_service import DataService
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    SEEDED_STATS,
    FakeDataStore,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeTrail,
    api_client,
    make_runner,
    numbat_inputs,
    numbat_tables,
    wait_idle,
    xero_snapshot,
)
from tests.test_data_ready import waiting_for_data
from tests.test_decisions import acc01_decision

EID = ENGAGEMENT.id
NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
PERIOD = (ENGAGEMENT.period_start, ENGAGEMENT.period_end)
CHECKUP_ACTIONS = {"check", "request_raised", "request_received"}


def statuses(checks: list[Any]) -> dict[str, str]:
    return {c.id.value: c.status for c in checks}


def numbat(kind: TableKind) -> list[Any]:
    return list(numbat_tables()[kind])


def xero_journals() -> list[JournalEntry]:
    """Numbat 总账经 Xero 同步: API 不提供录入人与审批人 (适配器记 "xero", 审批人为空)."""
    return [
        cast(JournalEntry, j).model_copy(update={"created_by": XERO_PREPARER, "approved_by": None})
        for j in numbat(TableKind.journal_entries)
    ]


def checkup_trail(runner: GraphRunner) -> list[TrailEntry]:
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e for e in trail.entries if e.kind is TrailKind.data and e.payload["action"] in CHECKUP_ACTIONS]


# ── 规则 (纯函数) ──


def test_numbat_dataset_passes_and_only_lacks_statements() -> None:
    """Numbat 演示数据集: 借贷平衡, 分录与试算表按期初结转勾稽一致, 科目全部归类, 有录入人与审批人;
    只缺报表草稿 (缺资料, 提出补交请求)."""
    result = run_checks(numbat_inputs(*PERIOD), statements=0, journal_version=None, now=NOW)

    checks = {c.id: c for c in result.checks}
    assert [c.id for c in result.checks] == list(CheckId)
    assert statuses(result.checks) == {
        "debits_equal_credits": "passed",
        "journals_agree_tb": "passed",
        "count_agrees_source": "passed",
        "mapping_complete": "passed",
        "preparer_approver_available": "passed",
        "tb_agrees_statements": "missing_data",
    }
    assert checks[CheckId.debits_equal_credits].params == {"debit_cents": 5271620000, "credit_cents": 5271620000}
    assert checks[CheckId.journals_agree_tb].params == {"accounts": 23, "differences": []}
    assert checks[CheckId.count_agrees_source].params == {
        "source": "journal_entries.csv",
        "expected": 2686,
        "received": 2686,
    }
    assert checks[CheckId.mapping_complete].params == {"mapped": 23, "total": 23}
    assert checks[CheckId.tb_agrees_statements].request_id == "REQ-financial_statements"
    [draft] = result.requests
    assert (draft.id, draft.kind, draft.document_type, draft.reason_code, draft.used_for, draft.status) == (
        "REQ-financial_statements",
        "document",
        DocumentType.financial_statements,
        "not_provided",
        ["FIN-12"],
        "open",
    )


def test_xero_snapshot_lacks_preparer_and_approver() -> None:
    """Xero Demo 快照: 分录与试算表勾稽一致; Xero API 不提供录入人与审批人, 请求审批日志 (来源 Xero);
    有一个科目没归入标准分类."""
    snapshot = xero_snapshot()
    period = snapshot_period(snapshot)
    tables = build_tables(snapshot)
    inputs = summarize(
        tables["accounts"],
        tables["trial_balance"],
        tables["journal_entries"],
        period.start,
        period.end,
        dataset="xero/20261017-mock",
    )

    result = run_checks(inputs, statements=1, journal_version=None, now=NOW)

    checks = {c.id: c for c in result.checks}
    assert checks[CheckId.journals_agree_tb].status == "passed"
    assert checks[CheckId.debits_equal_credits].status == "passed"
    assert checks[CheckId.mapping_complete].status == "failed"
    assert checks[CheckId.mapping_complete].request_id is None  # 归类由审计师在科目表里改 (R16), 不向客户要
    preparer = checks[CheckId.preparer_approver_available]
    assert (preparer.status, preparer.params, preparer.request_id) == (
        "failed",
        {"source": "Xero", "missing_fields": ["created_by", "approved_by"]},
        "REQ-approval_log",
    )
    assert checks[CheckId.tb_agrees_statements].status == "pending"  # 报表草稿已有, 勾稽归 FIN-12
    [approval] = result.requests
    assert (approval.id, approval.kind, approval.table_kind, approval.reason_code, approval.reason_params) == (
        "REQ-approval_log",
        "table",
        TableKind.approval_log,
        "field_unavailable",
        {"fields": ["created_by", "approved_by"], "source": "Xero"},
    )


def test_empty_project_requests_required_tables_and_statements() -> None:
    """什么都没接入: 各项缺资料, 必需的三张表与报表草稿各一份请求, 各项链到它要用的第一张缺的表."""
    result = run_checks(CheckInputs(), statements=0, journal_version=None, now=NOW)

    assert set(statuses(result.checks).values()) == {"missing_data"}
    assert [(r.id, r.reason_code) for r in result.requests] == [
        ("REQ-accounts", "missing_table"),
        ("REQ-trial_balance", "missing_table"),
        ("REQ-journal_entries", "missing_table"),
        ("REQ-financial_statements", "not_provided"),
    ]
    assert {c.id.value: c.request_id for c in result.checks} == {
        "debits_equal_credits": "REQ-journal_entries",
        "journals_agree_tb": "REQ-journal_entries",
        "count_agrees_source": "REQ-journal_entries",
        "mapping_complete": "REQ-accounts",
        "preparer_approver_available": "REQ-journal_entries",
        "tb_agrees_statements": "REQ-financial_statements",
    }


def test_failures_report_amounts_differences_and_counts() -> None:
    """借贷不平, 某科目发生额与试算表变动不一致, 行数与来源记下的不一致: 各自不通过并带参数."""
    base = numbat_inputs(*PERIOD)
    moved = dict(base.movements)
    moved["6300"] += 12345  # 运费多出一笔期间内的发生额
    inputs = base.model_copy(update={"movements": moved, "debit_cents": base.debit_cents + 12345})
    version = TableVersion(
        id="XERO-journal_entries-v2",
        kind=TableKind.journal_entries,
        source="xero",
        file_name=None,
        document_id=None,
        records=2690,  # Xero 交付了 2690 行, 表里只有 2686
        data_version=2,
        request_id=None,
        created_by="U-1",
        created_at=NOW,
    )

    checks = {c.id: c for c in run_checks(inputs, statements=1, journal_version=version, now=NOW).checks}

    assert checks[CheckId.debits_equal_credits].status == "failed"
    assert checks[CheckId.journals_agree_tb].params == {
        "accounts": 23,
        "differences": [{"account_code": "6300", "difference_cents": 12345}],
    }
    assert (checks[CheckId.count_agrees_source].status, checks[CheckId.count_agrees_source].params) == (
        "failed",
        {"source": "Xero", "expected": 2690, "received": 2686},
    )


def test_without_retained_earnings_the_prior_result_shows_as_difference() -> None:
    """留存收益科目没归类时上期损益无处结转: 不报错, 差异落在留存收益科目 (3200) 上."""
    base = numbat_inputs(*PERIOD)
    categories = {c: (None if v == "retained_earnings" else v) for c, v in base.account_categories.items()}

    checks = run_checks(
        base.model_copy(update={"account_categories": categories}), statements=1, journal_version=None, now=NOW
    ).checks

    journals = next(c for c in checks if c.id is CheckId.journals_agree_tb)
    assert journals.status == "failed"
    assert journals.params["differences"] == [{"account_code": "3200", "difference_cents": 89180000}]


def test_summarize_ignores_placeholder_preparer_and_out_of_period_journals() -> None:
    """汇总口径: 录入人为连接器占位值或空串的不算; 发生额只算期间内的分录."""
    rows = [
        JournalEntry(
            id=f"J-{n}",
            entry_no=f"E-{n}",
            posting_date=date(2026, 7, 1) if n else date(2025, 6, 30),
            created_at=None,
            created_by=by,
            approved_by=None,
            account_code="1010",
            debit_cents=100,
            description=None,
            source_doc=None,
        )
        for n, by in enumerate([XERO_PREPARER, "", None])
    ]
    inputs = summarize(
        [Account(code="1010", name="Cash", type="asset", category="cash")],
        [TrialBalanceRow(period="current", account_code="1010", balance_cents=300)],
        rows,
        date(2025, 7, 1),
        date(2026, 6, 30),
    )
    assert (inputs.journal_rows, inputs.preparer_rows, inputs.movements) == (3, 0, {})


def request(rid: str, *, status: str = "open", at: datetime = NOW, **values: Any) -> DataRequest:
    base = {"kind": "table", "title": rid, "reason_code": "missing_table", "status": status, "created_at": at}
    return DataRequest.model_validate({"id": rid, **base, **values})


def test_reconcile_raises_keeps_updates_receives_and_reopens() -> None:
    """对照已有请求: 新的提出; 仍要的保留原提出时间 (参数变了只更新); 不再要的转 received; 收到后又要的重新打开."""
    earlier = NOW - timedelta(days=1)
    existing = [
        request("REQ-accounts", at=earlier),
        request("REQ-approval_log", at=earlier, reason_code="field_unavailable", reason_params={"fields": ["a"]}),
        request("REQ-journal_entries", at=earlier),
        request("REQ-trial_balance", status="received", at=earlier, resolved_at=earlier),
    ]
    wanted = [
        request("REQ-accounts"),
        request("REQ-approval_log", reason_code="field_unavailable", reason_params={"fields": ["b"]}),
        request("REQ-trial_balance"),
        request("REQ-financial_statements", kind="document"),
    ]

    changes = reconcile(existing, wanted, NOW)

    assert [r.id for r in changes.raised] == ["REQ-trial_balance", "REQ-financial_statements"]
    assert [r.id for r in changes.received] == ["REQ-journal_entries"]
    assert changes.received[0].resolved_at == NOW and changes.received[0].created_at == earlier
    saved = {r.id: r for r in changes.saved}
    assert set(saved) == {"REQ-approval_log", "REQ-trial_balance", "REQ-financial_statements", "REQ-journal_entries"}
    assert (saved["REQ-approval_log"].created_at, saved["REQ-approval_log"].reason_params) == (
        earlier,
        {"fields": ["b"]},
    )
    assert (saved["REQ-trial_balance"].status, saved["REQ-trial_balance"].created_at) == ("open", NOW)
    assert saved["REQ-trial_balance"].resolved_at is None


def test_trail_keys_do_not_depend_on_time_zone() -> None:
    """留痕防重复键里的时间统一成 UTC: 库里读回的时区不同也是同一条."""
    from backend.contracts.data import CheckRun

    sydney = NOW.astimezone(ZoneInfo("Australia/Sydney"))
    run = CheckRun(data_version=1, checked_at=NOW, checks=[])
    assert (
        check_entry(EID, run).idempotency_key
        == check_entry(EID, run.model_copy(update={"checked_at": sydney})).idempotency_key
    )
    raised = request("REQ-accounts")
    assert (
        request_entry(EID, raised, "request_raised").idempotency_key
        == request_entry(EID, raised.model_copy(update={"created_at": sydney}), "request_raised").idempotency_key
    )


# ── 验收: Numbat 经 Xero 同步, 两条请求, 补交后转 received ──


def service_for(data: FakeDataStore) -> tuple[GraphRunner, DataService, FakeFinancialStore]:
    financial = FakeFinancialStore()
    runner, _ = make_runner(
        engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]), data=data, financial_store=financial
    )
    financial.document_refs.clear()
    return runner, DataService(runner.deps, runner), financial


def approval_log_csv() -> bytes:
    """Numbat 总账各凭证的录入人与审批人 (客户从 ERP 导出的审批日志)."""
    seen: dict[str, tuple[str, str]] = {}
    for row in numbat(TableKind.journal_entries):
        seen.setdefault(row.entry_no, (row.created_by, row.approved_by))
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["entry_no", "created_by", "approved_by"])
    writer.writerows([entry, *people] for entry, people in seen.items())
    return stream.getvalue().encode()


async def test_xero_sync_raises_two_requests_received_after_uploads() -> None:
    """验收: Numbat 经 Xero 同步后出现审批日志, 报表草稿两条请求; 导入审批日志, 上传报表草稿后都转为 received."""
    data = FakeDataStore(ready=False)
    runner, service, financial = service_for(data)
    tables = {
        TableKind.accounts: numbat(TableKind.accounts),
        TableKind.trial_balance: numbat(TableKind.trial_balance),
        TableKind.journal_entries: xero_journals(),
    }
    async with service.holding(EID, [k.value for k in tables]):
        await service.write_tables(EID, tables, source="xero", actor="U-1")

    requests = await data.requests(EID)
    assert [(r.id, r.reason_code, r.reason_params, r.status) for r in requests] == [
        ("REQ-approval_log", "field_unavailable", {"fields": ["created_by", "approved_by"], "source": "Xero"}, "open"),
        ("REQ-financial_statements", "not_provided", {}, "open"),
    ]
    assert statuses(data.check_results) == {
        "debits_equal_credits": "passed",
        "journals_agree_tb": "passed",
        "count_agrees_source": "passed",
        "mapping_complete": "passed",
        "preparer_approver_available": "failed",
        "tb_agrees_statements": "missing_data",
    }
    assert (await service.summary(EID)).open_requests == 2
    async with api_client(runner) as client:
        listed = (await client.get(f"/api/v1/engagements/{EID}/data/requests")).json()
    assert [(r["id"], r["kind"], r["status"], r["raised_by"]) for r in listed] == [
        ("REQ-approval_log", "table", "open", "A3"),
        ("REQ-financial_statements", "document", "open", "A3"),
    ]

    preview = await service.start_import(
        EID, "approval_log.csv", approval_log_csv(), actor="U-1", request_id="REQ-approval_log"
    )
    mapping = {"entry_no": "entry_no", "created_by": "created_by", "approved_by": "approved_by"}
    body = ImportCommit(kind=TableKind.approval_log, sheet=preview.sheet, date_format="ymd", mapping=mapping)
    await service.commit_import(EID, preview.import_id, body, "U-1")
    approval = next(r for r in await data.requests(EID) if r.id == "REQ-approval_log")
    assert (approval.status, approval.resolved_at is not None) == ("received", True)
    assert statuses(data.check_results)["preparer_approver_available"] == "passed"

    [document] = await service.upload_documents(
        EID,
        [("FY26 draft financial statements.pdf", b"%PDF draft")],
        actor="U-1",
        request_id="REQ-financial_statements",
    )
    await asyncio.gather(*list(service.tasks))
    statement = next(d for d in financial.document_refs if d.id == document.id)
    assert (statement.type, statement.status) == (DocumentType.financial_statements, "classified")
    assert {r.id: r.status for r in await data.requests(EID)} == {
        "REQ-approval_log": "received",
        "REQ-financial_statements": "received",
    }
    assert statuses(data.check_results)["tb_agrees_statements"] == "pending"
    assert (await service.summary(EID)).open_requests == 0

    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    classify = next(e for e in trail.entries if e.payload.get("action") == "classify")
    assert (classify.actor, classify.payload["reason"]) == ("U-1", "按补交请求 REQ-financial_statements 上传")
    events = [
        (e.payload["action"], e.payload.get("request_id"))
        for e in checkup_trail(runner)
        if e.payload["action"] != "check"
    ]
    assert events == [
        ("request_raised", "REQ-approval_log"),
        ("request_raised", "REQ-financial_statements"),
        ("request_received", "REQ-approval_log"),
        ("request_received", "REQ-financial_statements"),
    ]
    received = [e for e in checkup_trail(runner) if e.payload["action"] == "request_received"]
    assert received[0].payload == {
        "action": "request_received",
        "request_id": "REQ-approval_log",
        "table_kind": "approval_log",
    }
    assert received[1].payload["document_type"] == "financial_statements"
    assert [e.payload["action"] for e in checkup_trail(runner)].count("check") == 3  # 同步, 导入, 上传各一次


async def test_upload_with_unknown_or_table_request_is_classified_by_a3() -> None:
    """上传带的 request_id 不是待补交的文件请求 (不存在, 或是账表请求) 时照常由 A3 分类, request_id 只记在留痕里."""
    data = FakeDataStore(ready=False)
    _, service, _ = service_for(data)
    await DataCheckup(service.deps).run(EID)  # 提出缺表与报表草稿的请求

    hint = await service._requested(EID, "REQ-accounts")
    missing = await service._requested(EID, "REQ-nope")
    draft = await service._requested(EID, "REQ-financial_statements")

    assert (hint, missing) == (None, None)
    assert draft is not None and draft.document_type is DocumentType.financial_statements


# ── 时机: P1 等数据, 写入后, 稍后处理, 留痕补写 ──


async def test_waiting_for_data_raises_missing_table_requests_once() -> None:
    """P1 等数据: A3 体检, 为每张缺的必需表与报表草稿提请求; 接入账表后缺表的请求转 received; 确认后恢复不再重复体检."""
    runner, _, data = await waiting_for_data()
    assert [r.id for r in await data.requests(EID)] == [
        "REQ-accounts",
        "REQ-trial_balance",
        "REQ-journal_entries",
        "REQ-financial_statements",
    ]
    assert len([e for e in checkup_trail(runner) if e.payload["action"] == "check"]) == 1

    service = DataService(runner.deps, runner)
    tables = {k: numbat(k) for k in (TableKind.accounts, TableKind.trial_balance, TableKind.journal_entries)}
    async with service.holding(EID, [k.value for k in tables]):
        await service.write_tables(EID, tables, source="import", actor="U-1")
    assert {r.id: r.status for r in await data.requests(EID)} == {
        "REQ-accounts": "received",
        "REQ-trial_balance": "received",
        "REQ-journal_entries": "received",
        "REQ-financial_statements": "open",
    }
    data.table_stats = dict(SEEDED_STATS)
    await service.confirm_ready(EID, "U-1")
    await wait_idle(runner, EID)

    assert (
        len([e for e in checkup_trail(runner) if e.payload["action"] == "check"]) == 2
    )  # 等待时与写入后, 恢复时不再体检
    overview = await GraphStateQuery(runner.app, runner.deps, runner).overview(EID)
    assert overview.awaiting_data is False


async def test_ready_dataset_project_is_checked_at_p1_start() -> None:
    """数据集载入 (视同已确认) 的项目: 进 P1 时第一次体检, 只缺报表草稿."""
    runner, _ = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]))
    await runner.start(EID)
    await wait_idle(runner, EID)
    await GraphDecisionSubmit(runner.app, runner.deps, runner).submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(runner, EID)

    data = runner.deps.data
    assert isinstance(data, FakeDataStore)
    assert [r.id for r in await data.requests(EID)] == ["REQ-financial_statements"]
    assert statuses(data.check_results)["journals_agree_tb"] == "passed"
    assert len([e for e in checkup_trail(runner) if e.payload["action"] == "check"]) == 1


async def test_data_request_can_be_deferred_until_received() -> None:
    """稍后处理补交请求: 不存在 404, 不是待补交 409 REQUEST_NOT_OPEN; 收到后标记失效; 重新打开后旧标记也不算."""
    runner, _, data = await waiting_for_data()
    base = f"/api/v1/engagements/{EID}/deferrals"
    async with api_client(runner) as client:
        marked = await client.post(
            base, json={"target_type": "data_request", "target_id": "REQ-accounts", "deferred": True}
        )
        missing = await client.post(
            base, json={"target_type": "data_request", "target_id": "REQ-nope", "deferred": True}
        )
        listed = (await client.get(base)).json()
        accounts = data.data_requests["REQ-accounts"]
        data.data_requests["REQ-accounts"] = accounts.model_copy(update={"status": "received", "resolved_at": NOW})
        closed = await client.post(
            base, json={"target_type": "data_request", "target_id": "REQ-accounts", "deferred": False}
        )
        after_receipt = (await client.get(base)).json()
        reopened = accounts.model_copy(update={"created_at": datetime.now(UTC) + timedelta(seconds=1)})
        data.data_requests["REQ-accounts"] = reopened
        after_reopen = (await client.get(base)).json()

    assert marked.status_code == 202
    assert (missing.status_code, missing.json()["detail"]["code"]) == (404, "NOT_FOUND")
    assert [(d["target_type"], d["target_id"]) for d in listed] == [("data_request", "REQ-accounts")]
    assert (closed.status_code, closed.json()["detail"]["code"]) == (409, "REQUEST_NOT_OPEN")
    assert after_receipt == [] and after_reopen == []


async def test_checkup_trail_failure_is_backfilled_on_next_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """体检结果已存而留痕没写成: 写入照常成功; 下次取锁时补写体检与请求的留痕, 不重复."""
    data = FakeDataStore(ready=False)
    runner, service, _ = service_for(data)
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    write = trail.record

    async def no_checkup_trail(entry: TrailEntry) -> str:
        if entry.kind is TrailKind.data and entry.payload["action"] in CHECKUP_ACTIONS:
            raise RuntimeError("体检留痕写失败")
        return await write(entry)

    monkeypatch.setattr(trail, "record", no_checkup_trail)
    async with service.holding(EID, ["accounts"]):
        await service.write_tables(EID, {TableKind.accounts: numbat(TableKind.accounts)}, source="import", actor="U-1")
    assert data.last_run is not None and not checkup_trail(runner)
    monkeypatch.setattr(trail, "record", write)

    async with service.holding(EID, []):
        pass
    async with service.holding(EID, []):
        pass

    actions = [(e.payload["action"], e.payload.get("request_id")) for e in checkup_trail(runner)]
    assert actions.count(("check", None)) == 1
    assert sorted(rid for action, rid in actions if action == "request_raised") == [
        "REQ-financial_statements",
        "REQ-journal_entries",
        "REQ-trial_balance",
    ]


async def test_checkup_failure_does_not_fail_the_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """体检出错只记日志: 账表已写入, 导入照常返回."""
    data = FakeDataStore(ready=False)
    _, service, _ = service_for(data)

    async def broken(eid: str, start: date, end: date) -> CheckInputs:
        raise RuntimeError("汇总失败")

    monkeypatch.setattr(data, "check_inputs", broken)
    async with service.holding(EID, ["accounts"]):
        version = await service.write_tables(
            EID, {TableKind.accounts: numbat(TableKind.accounts)}, source="import", actor="U-1"
        )
    assert version == 1 and data.last_run is None


# ── 审核 FD-R15 第 1 轮 ──


def test_retained_earnings_carried_from_prior_year_is_compared() -> None:
    """留存收益科目两期试算表与分录都没有, 却承接了上期损益: 也参与勾稽, 差异落在它上面."""
    inputs = CheckInputs(
        journal_rows=2,
        debit_cents=10,
        credit_cents=10,
        movements={"1000": 10, "4000": -10},
        preparer_rows=2,
        approver_rows=2,
        tb_prior={"1000": 100, "4000": -100},
        tb_current={"1000": 110, "4000": -10},
        account_types={"1000": "asset", "3200": "equity", "4000": "revenue"},
        account_categories={"1000": "cash", "3200": "retained_earnings", "4000": "revenue"},
    )

    journals = next(
        c
        for c in run_checks(inputs, statements=1, journal_version=None, now=NOW).checks
        if c.id is CheckId.journals_agree_tb
    )

    assert journals.status == "failed"
    assert journals.params == {"accounts": 3, "differences": [{"account_code": "3200", "difference_cents": -100}]}


async def test_upload_against_request_persists_type_before_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """对着报表草稿请求上传: 在请求里就定好类型落库, 作业开始前关停也不丢; 启动补做后体检, 请求转 received.
    定类型那一步失败时 (文件已登记, 上传留痕带着请求), 启动补做从上传留痕取回请求照样定类型 (审核 FD-R15 第 1, 2 轮)."""
    data = FakeDataStore(ready=False)
    runner, service, financial = service_for(data)
    async with service.holding(EID, []):
        await DataCheckup(service.deps).run(EID)
    evidence = runner.deps.evidence_store
    update = evidence.update_document
    failed = False

    async def fail_once(eid: str, document_id: str, **values: Any) -> Any:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("登记改不了")
        return await update(eid, document_id, **values)

    monkeypatch.setattr(evidence, "update_document", fail_once)
    with pytest.raises(RuntimeError):
        await service.upload_documents(
            EID, [("first.png", b"draft-one")], actor="U-1", request_id="REQ-financial_statements"
        )
    monkeypatch.setattr(evidence, "update_document", update)
    [first] = financial.document_refs
    assert first.status == "received"

    [second] = await service.upload_documents(
        EID, [("second.png", b"draft-two")], actor="U-1", request_id="REQ-financial_statements"
    )
    assert (second.type, second.status) == (DocumentType.financial_statements, "classified")  # 已落库
    await service.shutdown()  # 作业还没开始就被取消

    restarted = DataService(service.deps, runner)
    await restarted.recover()
    await asyncio.gather(*list(restarted.tasks))

    assert {(d.file_name, d.type, d.status) for d in financial.document_refs} == {
        ("first.png", DocumentType.financial_statements, "classified"),
        ("second.png", DocumentType.financial_statements, "classified"),
    }
    assert data.data_requests["REQ-financial_statements"].status == "received"


async def test_reupload_against_request_survives_shutdown_before_job() -> None:
    """已转人工的文件对着请求重传后, 作业开始前关停: 类型已落库, 启动补做后请求转 received (审核 FD-R15 第 2 轮探针)."""
    data = FakeDataStore(ready=False)
    runner, service, financial = service_for(data)
    await service.upload_documents(EID, [("scan.png", b"draft-image")], actor="U-1")
    await asyncio.gather(*list(service.tasks))
    assert financial.document_refs[0].status == "needs_manual"

    await service.upload_documents(
        EID, [("scan.png", b"draft-image")], actor="U-1", request_id="REQ-financial_statements"
    )
    await service.shutdown()
    restarted = DataService(service.deps, runner)
    await restarted.recover()
    await asyncio.gather(*list(restarted.tasks))

    current = financial.document_refs[0]
    assert (current.type, current.status) == (DocumentType.financial_statements, "classified")
    assert data.data_requests["REQ-financial_statements"].status == "received"


async def test_reupload_against_request_retypes_document_once() -> None:
    """先普通上传而分不了类 (转人工), 再对着报表草稿请求上传同一文件: 按请求定类型, 请求转 received;
    再传一次不再处理."""
    data = FakeDataStore(ready=False)
    runner, service, financial = service_for(data)
    await service.upload_documents(EID, [("scan.png", b"draft-image")], actor="U-1")
    await asyncio.gather(*list(service.tasks))
    assert financial.document_refs[0].status == "needs_manual"
    assert data.data_requests["REQ-financial_statements"].status == "open"

    for _ in range(2):
        await service.upload_documents(
            EID, [("scan.png", b"draft-image")], actor="U-1", request_id="REQ-financial_statements"
        )
        await asyncio.gather(*list(service.tasks))

    current = financial.document_refs[0]
    assert (current.type, current.status) == (DocumentType.financial_statements, "classified")
    assert data.data_requests["REQ-financial_statements"].status == "received"
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    assigned = [e for e in trail.entries if e.payload.get("action") == "classify" and e.actor == "U-1"]
    assert len(assigned) == 1


async def test_import_retry_redoes_checkup_skipped_after_trail_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """提交后的版本留痕写失败: 当时照常体检; 体检也失败时, 同一提交的重试补做体检."""
    data = FakeDataStore(ready=False)
    _, service, _ = service_for(data)
    preview = await service.start_import(EID, "accounts.csv", b"code,name,type\n1000,Cash,asset\n", actor="U-1")
    mapping = {"code": "code", "name": "name", "type": "type"}
    body = ImportCommit(kind=TableKind.accounts, sheet=preview.sheet, date_format="ymd", mapping=mapping)
    trail = service.deps.trail
    write = trail.record
    inputs = data.check_inputs

    async def no_import_trail(entry: TrailEntry) -> str:
        if entry.payload.get("action") == "import":
            raise RuntimeError("留痕写失败")
        return await write(entry)

    async def no_inputs(*args: Any) -> CheckInputs:
        raise RuntimeError("汇总失败")

    monkeypatch.setattr(trail, "record", no_import_trail)
    monkeypatch.setattr(data, "check_inputs", no_inputs)
    with pytest.raises(RuntimeError):
        await service.commit_import(EID, preview.import_id, body, "U-1")
    assert data.data_version == 1 and data.last_run is None
    monkeypatch.setattr(trail, "record", write)
    monkeypatch.setattr(data, "check_inputs", inputs)

    await service.commit_import(EID, preview.import_id, body, "U-1")

    assert data.last_run is not None and data.last_run.data_version == 1


async def test_failed_checkup_after_upload_is_redone_when_p1_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """报表草稿上传分类成功而随后的体检失败 (数据版本没变): 确认就绪后 P1 恢复时看出已提供的报表数变了, 补做体检."""
    runner, _, data = await waiting_for_data()
    service = DataService(runner.deps, runner)
    tables = {k: numbat(k) for k in (TableKind.accounts, TableKind.trial_balance, TableKind.journal_entries)}
    async with service.holding(EID, [k.value for k in tables]):
        await service.write_tables(EID, tables, source="import", actor="U-1")
    inputs = data.check_inputs

    async def no_inputs(*args: Any) -> CheckInputs:
        raise RuntimeError("汇总失败")

    monkeypatch.setattr(data, "check_inputs", no_inputs)
    await service.upload_documents(
        EID, [("scan.png", b"draft-image")], actor="U-1", request_id="REQ-financial_statements"
    )
    await asyncio.gather(*list(service.tasks))
    assert data.data_requests["REQ-financial_statements"].status == "open"
    monkeypatch.setattr(data, "check_inputs", inputs)
    data.table_stats = dict(SEEDED_STATS)

    await service.confirm_ready(EID, "U-1")
    await wait_idle(runner, EID)

    assert data.data_requests["REQ-financial_statements"].status == "received"
    tb = next(c for c in data.check_results if c.id is CheckId.tb_agrees_statements)
    assert (tb.status, tb.params) == ("pending", {"statements": 1})


async def test_recover_redoes_stale_checkup_without_unfinished_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    """启动补做: 没有要补做的文件, 但上次体检失败 (结果不对应当前数据), 补做体检; 已是最新的不再体检."""
    data = FakeDataStore(ready=False)
    runner, service, _ = service_for(data)
    inputs = data.check_inputs

    async def no_inputs(*args: Any) -> CheckInputs:
        raise RuntimeError("汇总失败")

    monkeypatch.setattr(data, "check_inputs", no_inputs)
    async with service.holding(EID, ["accounts"]):
        await service.write_tables(EID, {TableKind.accounts: numbat(TableKind.accounts)}, source="import", actor="U-1")
    monkeypatch.setattr(data, "check_inputs", inputs)
    assert data.last_run is None

    await DataService(service.deps, runner).recover()
    first = data.last_run
    await DataService(service.deps, runner).recover()

    assert first is not None and first.data_version == 1 and data.last_run is first
    assert not runner.is_running(EID)


@pytest.mark.parametrize("initial", ["received", "needs_manual"])
async def test_recover_finishes_request_typing_when_record_update_failed(
    monkeypatch: pytest.MonkeyPatch, initial: str
) -> None:
    """已有文件 (还没处理或已转人工) 对着请求重传, 写完 classify 留痕后改登记失败: classify 留痕带着 request_id,
    启动补做据此接着按请求定类型, 请求转 received (审核 FD-R15 第 3 轮探针)."""
    data = FakeDataStore(ready=False)
    runner, service, financial = service_for(data)
    async with service.holding(EID, []):
        await service.checkup(EID)
    await service.upload_documents(EID, [("scan.png", b"draft-image")], actor="U-1")
    if initial == "received":
        await service.shutdown()
    else:
        await asyncio.gather(*list(service.tasks))
    assert financial.document_refs[0].status == initial
    evidence = runner.deps.evidence_store
    update = evidence.update_document

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("登记改不了")

    monkeypatch.setattr(evidence, "update_document", broken)
    with pytest.raises(RuntimeError):
        await service.upload_documents(
            EID, [("scan.png", b"draft-image")], actor="U-1", request_id="REQ-financial_statements"
        )
    monkeypatch.setattr(evidence, "update_document", update)

    restarted = DataService(service.deps, runner)
    await restarted.recover()
    await asyncio.gather(*list(restarted.tasks))

    current = financial.document_refs[0]
    assert (current.type, current.status) == (DocumentType.financial_statements, "classified")
    assert data.data_requests["REQ-financial_statements"].status == "received"


async def test_later_manual_type_wins_over_earlier_request_upload() -> None:
    """对着报表草稿请求上传后, 审计师又把它改成会议纪要 (请求随之重新打开): 启动补做不按旧请求改回去."""
    data = FakeDataStore(ready=False)
    runner, service, financial = service_for(data)
    async with service.holding(EID, []):
        await service.checkup(EID)  # 提出报表草稿请求
    [document] = await service.upload_documents(
        EID, [("minutes.pdf", b"%PDF minutes")], actor="U-1", request_id="REQ-financial_statements"
    )
    await asyncio.gather(*list(service.tasks))
    assert data.data_requests["REQ-financial_statements"].status == "received"
    await service.set_document_type(EID, document.id, DocumentType.minutes, "其实是董事会纪要", "U-1")
    await asyncio.gather(*list(service.tasks))
    assert data.data_requests["REQ-financial_statements"].status == "open"  # 重新打开

    restarted = DataService(service.deps, runner)
    await restarted.recover()
    await asyncio.gather(*list(restarted.tasks))

    current = next(d for d in financial.document_refs if d.id == document.id)
    assert (current.type, current.status) == (DocumentType.minutes, "classified")
    assert data.data_requests["REQ-financial_statements"].status == "open"
