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
from backend.contracts.models import (
    DocumentRef,
    DocumentType,
    GateVerdict,
    Phase,
    Procedure,
    Reliability,
    TrailEntry,
    TrailKind,
    Versioned,
)
from backend.contracts.records import Account, JournalEntry, TrialBalanceRow
from backend.core.config import get_settings
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.persistence.data_store import stored_checks
from backend.persistence.stores import ADJUSTMENTS
from backend.services.data_quality import RETIRED_REQUESTS, reconcile, request_id, run_checks, summarize
from backend.services.data_service import DataService
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    SEEDED_STATS,
    FakeDatasetLoader,
    FakeDataStore,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeOutputStore,
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
from tests.xero_fakes import XERO_DATASET, XeroFakeDataStore, make_xero_deps, xero_adjustments, xero_traits

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


def test_numbat_dataset_passes_every_check() -> None:
    """Numbat 演示数据集: 借贷平衡, 分录与试算表按期初结转勾稽一致, 科目全部归类, 有录入人与审批人; 五项都通过,
    不提请求 (财务报表以账套为准, 不要报表草稿, Issue #46)."""
    result = run_checks(numbat_inputs(*PERIOD), journal_version=None, now=NOW)

    checks = {c.id: c for c in result.checks}
    assert [c.id for c in result.checks] == list(CheckId)
    assert statuses(result.checks) == {
        "debits_equal_credits": "passed",
        "journals_agree_tb": "passed",
        "count_agrees_source": "passed",
        "mapping_complete": "passed",
        "preparer_approver_available": "passed",
    }
    assert checks[CheckId.debits_equal_credits].params == {"debit_cents": 5271620000, "credit_cents": 5271620000}
    assert checks[CheckId.journals_agree_tb].params == {"accounts": 23, "differences": []}
    assert checks[CheckId.count_agrees_source].params == {
        "source": "journal_entries.csv",
        "expected": 2686,
        "received": 2686,
    }
    assert checks[CheckId.mapping_complete].params == {"mapped": 23, "total": 23}
    assert result.requests == []


@pytest.mark.external
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

    result = run_checks(inputs, journal_version=None, now=NOW)

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
    [approval] = result.requests
    assert (approval.id, approval.kind, approval.table_kind, approval.reason_code, approval.reason_params) == (
        "REQ-approval_log",
        "table",
        TableKind.approval_log,
        "field_unavailable",
        {"fields": ["created_by", "approved_by"], "source": "Xero"},
    )


def test_empty_project_requests_required_tables() -> None:
    """什么都没接入: 各项缺资料, 必需的三张表各一份请求, 各项链到它要用的第一张缺的表."""
    result = run_checks(CheckInputs(), journal_version=None, now=NOW)

    assert set(statuses(result.checks).values()) == {"missing_data"}
    assert [(r.id, r.reason_code) for r in result.requests] == [
        ("REQ-accounts", "missing_table"),
        ("REQ-trial_balance", "missing_table"),
        ("REQ-journal_entries", "missing_table"),
    ]
    assert {c.id.value: c.request_id for c in result.checks} == {
        "debits_equal_credits": "REQ-journal_entries",
        "journals_agree_tb": "REQ-journal_entries",
        "count_agrees_source": "REQ-journal_entries",
        "mapping_complete": "REQ-accounts",
        "preparer_approver_available": "REQ-journal_entries",
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

    checks = {c.id: c for c in run_checks(inputs, journal_version=version, now=NOW).checks}

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
        base.model_copy(update={"account_categories": categories}), journal_version=None, now=NOW
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


@pytest.mark.parametrize(("prefix", "movement"), [("AJE-", 100), (None, 200), ("", 200)])
def test_summarize_leaves_out_audit_adjustments_by_prefix(prefix: str | None, movement: int) -> None:
    """需求 R271: 发生额不计凭证号以前缀开头的审计更正分录; 没有前缀或前缀为空串时都计 (防空串把全部分录当作更正).
    借贷合计与行数照算全部分录."""
    rows = [
        JournalEntry(
            id=f"{no}-1",
            entry_no=no,
            posting_date=date(2026, 6, 1),
            created_at=None,
            created_by=None,
            approved_by=None,
            account_code="1010",
            debit_cents=100,
            description=None,
            source_doc=None,
        )
        for no in ("AJE-1", "E-1")
    ]
    inputs = summarize(
        [Account(code="1010", name="Cash", type="asset", category="cash")],
        [],
        rows,
        date(2025, 7, 1),
        date(2026, 6, 30),
        adjustment_prefix=prefix,
    )
    assert (inputs.journal_rows, inputs.debit_cents, inputs.movements) == (2, 200, {"1010": movement})


@pytest.mark.external
@pytest.mark.parametrize("prefix", [None, "ADJ-"], ids=["declared", "other_prefix"])
async def test_checkup_leaves_out_declared_audit_adjustments(prefix: str | None) -> None:
    """需求 R271 验收 2: Xero 演示两轮更正 (11 行) 写成总账新版本 (同 load_adjustments 的整表写入) 之后, A3 体检按
    数据集声明的前缀 (q1 声明 AJE-) 不计更正分录, 借贷平衡, 分录与试算表, 行数与来源都通过. 声明换成别的前缀时更正
    分录照计, 分录与试算表报差异 (含 620 +4853 与 710 -75000): 前缀取自数据集声明, 不是写死的缺省值."""
    deps = make_xero_deps()
    data, datasets = deps.data, deps.datasets
    assert isinstance(data, XeroFakeDataStore) and isinstance(datasets, FakeDatasetLoader)
    if prefix is not None:
        declared = xero_traits().model_copy(update={"audit_adjustment_entry_prefix": prefix})
        datasets.traits[XERO_DATASET] = declared
    journals = [*data.tables[TableKind.journal_entries], *xero_adjustments()]
    await data.write_tables(
        EID, {TableKind.journal_entries: journals}, source="xero", actor="A1", file_name=ADJUSTMENTS.as_posix()
    )

    run = await DataCheckup(deps).run(EID)

    checks = {c.id: c for c in run.checks}
    assert checks[CheckId.count_agrees_source].params == {"source": "Xero", "expected": 1028, "received": 1028}
    assert checks[CheckId.debits_equal_credits].status == "passed"
    agree = checks[CheckId.journals_agree_tb]
    if prefix is None:
        assert (agree.status, agree.params["differences"]) == ("passed", [])
    else:
        assert agree.status == "failed"
        differences = agree.params["differences"]
        assert {"account_code": "620", "difference_cents": 4853} in differences
        assert {"account_code": "710", "difference_cents": -75000} in differences
    assert checks[CheckId.count_agrees_source].status == "passed"


async def test_checkup_without_dataset_leaves_out_default_prefix() -> None:
    """没有数据集的项目 (新建的客户) 取数据集声明的缺省前缀 AJE- (审核 R271-03): Numbat 总账加一笔 AJE- 开头的
    平衡更正 (借 4100, 贷 1100), 分录与试算表仍一致; 不排除时 4100 与 1100 各差出这笔金额."""
    data = FakeDataStore()
    adjustment = [
        JournalEntry(
            id=f"AJE-T01-{n}",
            entry_no="AJE-T01",
            posting_date=date(2026, 6, 30),
            created_at=None,
            created_by=None,
            approved_by=None,
            account_code=code,
            debit_cents=debit,
            credit_cents=credit,
            description="Audit adjustment",
            source_doc=None,
        )
        for n, (code, debit, credit) in enumerate([("4100", 5000, 0), ("1100", 0, 5000)], start=1)
    ]
    data.tables[TableKind.journal_entries] = [*numbat(TableKind.journal_entries), *adjustment]
    engagements = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT.model_copy(update={"dataset": None})])
    runner, _ = make_runner(engagement_store=engagements, data=data)

    run = await DataCheckup(runner.deps).run(EID)

    agree = next(c for c in run.checks if c.id is CheckId.journals_agree_tb)
    assert (agree.status, agree.params["differences"]) == ("passed", [])


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
        request("REQ-contract", kind="document"),
    ]

    changes = reconcile(existing, wanted, NOW)

    assert [r.id for r in changes.raised] == ["REQ-trial_balance", "REQ-contract"]
    assert [r.id for r in changes.received] == ["REQ-journal_entries"]
    assert changes.received[0].resolved_at == NOW and changes.received[0].created_at == earlier
    saved = {r.id: r for r in changes.saved}
    assert set(saved) == {"REQ-approval_log", "REQ-trial_balance", "REQ-contract", "REQ-journal_entries"}
    assert (saved["REQ-approval_log"].created_at, saved["REQ-approval_log"].reason_params) == (
        earlier,
        {"fields": ["b"]},
    )
    assert (saved["REQ-trial_balance"].status, saved["REQ-trial_balance"].created_at) == ("open", NOW)
    assert saved["REQ-trial_balance"].resolved_at is None


def test_stored_checks_skip_retired_items() -> None:
    """旧项目的 data_checks 还存着 Issue #46 删去的 tb_agrees_statements: 读出时略过, 其余按 CheckId 顺序 (防删去
    枚举值后读旧库报错)."""
    rows = [
        {"id": "tb_agrees_statements", "status": "pending", "params": {"statements": 1}, "request_id": None},
        *({"id": c.value, "status": "passed", "params": {}, "request_id": None} for c in reversed(CheckId)),
    ]

    assert [c.id for c in stored_checks(rows)] == list(CheckId)


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


# ── 验收: Numbat 经 Xero 同步, 审批日志请求, 补交后转 received ──

CONTRACT_REQUEST = "REQ-contract"  # 按请求上传的用例对着的文件请求: 程序包里还没执行的 DOC-04 要合同 (需求 R272)


def service_for(
    data: FakeDataStore, *, contract_wanted: bool = False
) -> tuple[GraphRunner, DataService, FakeFinancialStore]:
    """Numbat 项目的数据服务, 证据文件为空. contract_wanted 时程序包里有一条还没执行的 DOC-04, 体检提出合同请求
    (CONTRACT_REQUEST)."""
    financial = FakeFinancialStore()
    pack: list[Versioned] = (
        [Procedure(id="DOC-04", phase=Phase.P1_Planning, template_commit="x", autonomy_level="L2")]
        if contract_wanted
        else []
    )
    runner, _ = make_runner(
        engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]),
        output_store=FakeOutputStore(pack),
        data=data,
        financial_store=financial,
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


async def test_xero_sync_raises_approval_request_received_after_import() -> None:
    """验收: Numbat 经 Xero 同步后出现审批日志一条请求 (不再要报表草稿, Issue #46); 导入审批日志后转为 received."""
    data = FakeDataStore(ready=False)
    runner, service, _ = service_for(data)
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
    ]
    assert statuses(data.check_results) == {
        "debits_equal_credits": "passed",
        "journals_agree_tb": "passed",
        "count_agrees_source": "passed",
        "mapping_complete": "passed",
        "preparer_approver_available": "failed",
    }
    assert (await service.summary(EID)).open_requests == 1
    async with api_client(runner) as client:
        listed = (await client.get(f"/api/v1/engagements/{EID}/data/requests")).json()
    assert [(r["id"], r["kind"], r["status"], r["raised_by"]) for r in listed] == [
        ("REQ-approval_log", "table", "open", "A3"),
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
    assert (await service.summary(EID)).open_requests == 0

    events = [
        (e.payload["action"], e.payload.get("request_id"))
        for e in checkup_trail(runner)
        if e.payload["action"] != "check"
    ]
    assert events == [("request_raised", "REQ-approval_log"), ("request_received", "REQ-approval_log")]
    received = [e for e in checkup_trail(runner) if e.payload["action"] == "request_received"]
    assert received[0].payload == {
        "action": "request_received",
        "request_id": "REQ-approval_log",
        "table_kind": "approval_log",
    }
    assert [e.payload["action"] for e in checkup_trail(runner)].count("check") == 2  # 同步, 导入各一次


async def test_upload_with_unknown_or_table_request_is_classified_by_a3() -> None:
    """上传带的 request_id 不是待补交的文件请求 (不存在, 或是账表请求) 时照常由 A3 分类, request_id 只记在留痕里."""
    data = FakeDataStore(ready=False)
    _, service, _ = service_for(data, contract_wanted=True)
    await DataCheckup(service.deps).run(EID)  # 提出缺表与合同的请求

    hint = await service._requested(EID, "REQ-accounts")
    missing = await service._requested(EID, "REQ-nope")
    contract = await service._requested(EID, CONTRACT_REQUEST)

    assert (hint, missing) == (None, None)
    assert contract is not None and contract.document_type is DocumentType.contract


# ── 时机: P1 等数据, 写入后, 稍后处理, 留痕补写 ──


async def test_waiting_for_data_raises_missing_table_requests_once() -> None:
    """P1 等数据: A3 体检, 为每张缺的必需表提请求; 接入账表后转 received; 确认后恢复不再重复体检."""
    runner, _, data = await waiting_for_data()
    assert [r.id for r in await data.requests(EID)] == ["REQ-accounts", "REQ-trial_balance", "REQ-journal_entries"]
    assert len([e for e in checkup_trail(runner) if e.payload["action"] == "check"]) == 1

    service = DataService(runner.deps, runner)
    tables = {k: numbat(k) for k in (TableKind.accounts, TableKind.trial_balance, TableKind.journal_entries)}
    async with service.holding(EID, [k.value for k in tables]):
        await service.write_tables(EID, tables, source="import", actor="U-1")
    assert {r.id: r.status for r in await data.requests(EID)} == {
        "REQ-accounts": "received",
        "REQ-trial_balance": "received",
        "REQ-journal_entries": "received",
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
    """数据集载入 (视同已确认) 的项目: 进 P1 时第一次体检, 什么都不缺, 不提请求."""
    runner, _ = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]))
    await runner.start(EID)
    await wait_idle(runner, EID)
    await GraphDecisionSubmit(runner.app, runner.deps, runner).submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(runner, EID)

    data = runner.deps.data
    assert isinstance(data, FakeDataStore)
    assert await data.requests(EID) == []
    assert statuses(data.check_results)["journals_agree_tb"] == "passed"
    assert len([e for e in checkup_trail(runner) if e.payload["action"] == "check"]) == 1


STATEMENTS_REQUEST = "REQ-financial_statements"  # Issue #46 前体检提出的报表草稿请求


@pytest.mark.parametrize(
    ("status", "documents", "rerun"),
    [("open", 0, True), ("open", 1, True), ("received", 1, False), ("unavailable", 0, False)],
    ids=["open", "open_with_statements", "received", "unavailable"],
)
async def test_retired_statements_request_is_withdrawn_on_next_checkup(
    status: str, documents: int, rerun: bool
) -> None:
    """Issue #46 验收 3: 旧项目的最近一次体检对应当前数据, 存着报表草稿请求. 请求仍待补交时 run_if_stale 重跑体检:
    请求转 withdrawn (resolved_at 为这次体检时间, 客户交过报表也撤回, 不按资料到了转 received), 留痕 request_withdrawn
    一条带理由 "报表以账套为准"; 再调一次不再重跑. 已收到与标了客户无此类资料的不算过期, 不重跑; 之后再写数据时的
    完整体检 (走到 reconcile) 也不改它们 (审核 R318-01)."""
    data = FakeDataStore()
    _, service, financial = service_for(data)
    checkup = DataCheckup(service.deps)
    await checkup.run(EID)
    earlier = NOW - timedelta(days=30)
    resolved = {} if status == "open" else {"resolved_at": earlier}
    old = request(
        STATEMENTS_REQUEST,
        kind="document",
        document_type="financial_statements",
        reason_code="not_provided",
        used_for=["FIN-12"],
        status=status,
        at=earlier,
        **resolved,
    )
    data.data_requests[STATEMENTS_REQUEST] = old
    financial.document_refs = [
        DocumentRef(
            id="DOC-fs",
            type=DocumentType.financial_statements,
            file_name="draft.pdf",
            sha256="0" * 64,
            page_count=6,
            reliability=Reliability.internal_copy,
            status="classified",
            uploaded_at=earlier,
        )
    ][:documents]

    run = await checkup.run_if_stale(EID)

    current = data.data_requests[STATEMENTS_REQUEST]
    trail = service.deps.trail
    assert isinstance(trail, FakeTrail)
    withdrawn = [e for e in trail.entries if e.payload.get("action") == "request_withdrawn"]
    if not rerun:
        assert (run, current, withdrawn) == (None, old, [])
        await checkup.run(EID)
        after = [e for e in trail.entries if e.payload.get("action") == "request_withdrawn"]
        assert (data.data_requests[STATEMENTS_REQUEST], after) == (old, [])
        return
    assert run is not None
    assert (current.status, current.resolved_at, current.created_at) == ("withdrawn", run.checked_at, earlier)
    assert [e.payload for e in withdrawn] == [
        {
            "action": "request_withdrawn",
            "request_id": STATEMENTS_REQUEST,
            "document_type": "financial_statements",
            "reason": "报表以账套为准",
        }
    ]
    assert await checkup.run_if_stale(EID) is None


def test_no_procedure_wants_a_retired_request() -> None:
    """守卫 (审核 R318-02): 程序库 (含未启用的) 没有程序的模板 inputs 要退役请求的资料 (RETIRED_REQUESTS). 以后有程序
    要它时这条失败, 提醒同时把它移出退役表: 否则请求被程序要而不撤回, run_if_stale 见它 open 就每次重跑体检."""
    library = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
    wanted = {
        request_id(subject)
        for template in library.all(include_disabled=True)
        for subject in [*template.inputs.tables, *template.inputs.documents]
    }
    assert wanted and wanted.isdisjoint(RETIRED_REQUESTS)


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
        "REQ-journal_entries",
        "REQ-trial_balance",
    ]


async def test_checkup_failure_does_not_fail_the_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """体检出错只记日志: 账表已写入, 导入照常返回."""
    data = FakeDataStore(ready=False)
    _, service, _ = service_for(data)

    async def broken(eid: str, start: date, end: date, **kwargs: Any) -> CheckInputs:
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
        c for c in run_checks(inputs, journal_version=None, now=NOW).checks if c.id is CheckId.journals_agree_tb
    )

    assert journals.status == "failed"
    assert journals.params == {"accounts": 3, "differences": [{"account_code": "3200", "difference_cents": -100}]}


async def test_upload_against_request_persists_type_before_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """对着合同请求上传: 在请求里就定好类型落库, 作业开始前关停也不丢; 启动补做后抽取 (固定输出里没有这几份, 抽取没通过
    转人工) 并体检, 请求转 received. 定类型那一步失败时 (文件已登记, 上传留痕带着请求), 启动补做从上传留痕取回请求照样
    定类型 (审核 FD-R15 第 1, 2 轮)."""
    data = FakeDataStore(ready=False)
    runner, service, financial = service_for(data, contract_wanted=True)
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
        await service.upload_documents(EID, [("first.png", b"draft-one")], actor="U-1", request_id=CONTRACT_REQUEST)
    monkeypatch.setattr(evidence, "update_document", update)
    [first] = financial.document_refs
    assert first.status == "received"

    [second] = await service.upload_documents(
        EID, [("second.png", b"draft-two")], actor="U-1", request_id=CONTRACT_REQUEST
    )
    assert (second.type, second.status) == (DocumentType.contract, "classified")  # 已落库
    await service.shutdown()  # 作业还没开始就被取消

    restarted = DataService(service.deps, runner)
    await restarted.recover()
    await asyncio.gather(*list(restarted.tasks))

    assert {(d.file_name, d.type, d.status) for d in financial.document_refs} == {
        ("first.png", DocumentType.contract, "needs_manual"),
        ("second.png", DocumentType.contract, "needs_manual"),
    }
    assert data.data_requests[CONTRACT_REQUEST].status == "received"


async def failing_check_inputs(*args: Any, **kwargs: Any) -> CheckInputs:
    raise RuntimeError("汇总失败")


async def test_contract_on_hand_after_failed_checkup_is_rechecked(monkeypatch: pytest.MonkeyPatch) -> None:
    """需求 R344 验收 1: 程序要合同 (REQ-contract 待补交), 对着请求上传的合同抽取转人工 (needs_manual, extract), 文件
    作业之后的那次体检失败. 数据版本没变 (没抽出合同), 但请求要的类型已有: run_if_stale 判为过期并重算, 请求转
    received (request_received 留痕一条); 再调不重跑."""
    data = FakeDataStore(ready=False)
    runner, service, financial = service_for(data, contract_wanted=True)
    checkup = DataCheckup(service.deps)
    await checkup.run(EID)
    assert data.data_requests[CONTRACT_REQUEST].status == "open"
    inputs = data.check_inputs
    monkeypatch.setattr(data, "check_inputs", failing_check_inputs)
    await service.upload_documents(EID, [("contract.png", b"contract-scan")], actor="U-1", request_id=CONTRACT_REQUEST)
    await asyncio.gather(*list(service.tasks))
    monkeypatch.setattr(data, "check_inputs", inputs)
    [document] = financial.document_refs
    assert (document.type, document.status, document.failed_step) == (DocumentType.contract, "needs_manual", "extract")
    last = data.last_run
    assert last is not None and last.data_version == (await data.readiness(EID)).data_version
    assert data.data_requests[CONTRACT_REQUEST].status == "open"

    run = await checkup.run_if_stale(EID)

    assert run is not None and data.data_requests[CONTRACT_REQUEST].status == "received"
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    received = [e for e in trail.entries if e.payload.get("action") == "request_received"]
    assert [e.payload["request_id"] for e in received] == [CONTRACT_REQUEST]
    assert await checkup.run_if_stale(EID) is None


async def test_open_document_request_without_the_file_is_not_rechecked(monkeypatch: pytest.MonkeyPatch) -> None:
    """需求 R344 验收 2: 合同请求待补交而合同还没有时不算过期. 体检之后什么都没变, 或只上传了分不了类的文件 (转人工,
    类型为 other) 而那次体检失败, run_if_stale 都不重跑 (不写体检留痕)."""
    data = FakeDataStore(ready=False)
    _, service, financial = service_for(data, contract_wanted=True)
    checkup = DataCheckup(service.deps)
    await checkup.run(EID)
    assert await checkup.run_if_stale(EID) is None

    monkeypatch.setattr(data, "check_inputs", failing_check_inputs)
    await service.upload_documents(EID, [("scan.png", b"draft-image")], actor="U-1")
    await asyncio.gather(*list(service.tasks))
    [document] = financial.document_refs
    assert (document.type, document.status) == (DocumentType.other, "needs_manual")
    trail = service.deps.trail
    assert isinstance(trail, FakeTrail)
    checks = [e for e in trail.entries if e.payload.get("action") == "check"]
    assert await checkup.run_if_stale(EID) is None
    assert [e for e in trail.entries if e.payload.get("action") == "check"] == checks
    assert data.data_requests[CONTRACT_REQUEST].status == "open"


async def test_reupload_against_request_survives_shutdown_before_job() -> None:
    """已转人工的文件对着请求重传后, 作业开始前关停: 类型已落库, 启动补做后请求转 received (审核 FD-R15 第 2 轮探针)."""
    data = FakeDataStore(ready=False)
    runner, service, financial = service_for(data, contract_wanted=True)
    await service.upload_documents(EID, [("scan.png", b"draft-image")], actor="U-1")
    await asyncio.gather(*list(service.tasks))
    assert (financial.document_refs[0].status, financial.document_refs[0].failed_step) == ("needs_manual", "classify")

    await service.upload_documents(EID, [("scan.png", b"draft-image")], actor="U-1", request_id=CONTRACT_REQUEST)
    await service.shutdown()
    restarted = DataService(service.deps, runner)
    await restarted.recover()
    await asyncio.gather(*list(restarted.tasks))

    current = financial.document_refs[0]
    assert (current.type, current.status, current.failed_step) == (DocumentType.contract, "needs_manual", "extract")
    assert data.data_requests[CONTRACT_REQUEST].status == "received"


async def test_reupload_against_request_retypes_document_once() -> None:
    """先普通上传而分不了类 (转人工), 再对着合同请求上传同一文件: 按请求定类型, 请求转 received; 再传一次不再处理."""
    data = FakeDataStore(ready=False)
    runner, service, financial = service_for(data, contract_wanted=True)
    await service.upload_documents(EID, [("scan.png", b"draft-image")], actor="U-1")
    await asyncio.gather(*list(service.tasks))
    assert financial.document_refs[0].status == "needs_manual"
    assert data.data_requests[CONTRACT_REQUEST].status == "open"

    for _ in range(2):
        await service.upload_documents(EID, [("scan.png", b"draft-image")], actor="U-1", request_id=CONTRACT_REQUEST)
        await asyncio.gather(*list(service.tasks))

    current = financial.document_refs[0]
    assert (current.type, current.failed_step) == (DocumentType.contract, "extract")
    assert data.data_requests[CONTRACT_REQUEST].status == "received"
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

    async def no_inputs(*args: Any, **kwargs: Any) -> CheckInputs:
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


async def test_recover_redoes_stale_checkup_without_unfinished_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    """启动补做: 没有要补做的文件, 但上次体检失败 (结果不对应当前数据), 补做体检; 已是最新的不再体检."""
    data = FakeDataStore(ready=False)
    runner, service, _ = service_for(data)
    inputs = data.check_inputs

    async def no_inputs(*args: Any, **kwargs: Any) -> CheckInputs:
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
    runner, service, financial = service_for(data, contract_wanted=True)
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
        await service.upload_documents(EID, [("scan.png", b"draft-image")], actor="U-1", request_id=CONTRACT_REQUEST)
    monkeypatch.setattr(evidence, "update_document", update)

    restarted = DataService(service.deps, runner)
    await restarted.recover()
    await asyncio.gather(*list(restarted.tasks))

    current = financial.document_refs[0]
    assert (current.type, current.failed_step) == (DocumentType.contract, "extract")
    assert data.data_requests[CONTRACT_REQUEST].status == "received"


async def test_later_manual_type_wins_over_earlier_request_upload() -> None:
    """对着合同请求上传后, 审计师又把它改成会议纪要 (请求随之重新打开): 启动补做不按旧请求改回去."""
    data = FakeDataStore(ready=False)
    runner, service, financial = service_for(data, contract_wanted=True)
    async with service.holding(EID, []):
        await service.checkup(EID)  # 提出合同请求
    [document] = await service.upload_documents(
        EID, [("minutes.pdf", b"%PDF minutes")], actor="U-1", request_id=CONTRACT_REQUEST
    )
    await asyncio.gather(*list(service.tasks))
    assert data.data_requests[CONTRACT_REQUEST].status == "received"
    await service.set_document_type(EID, document.id, DocumentType.minutes, "其实是董事会纪要", "U-1")
    await asyncio.gather(*list(service.tasks))
    assert data.data_requests[CONTRACT_REQUEST].status == "open"  # 重新打开

    restarted = DataService(service.deps, runner)
    await restarted.recover()
    await asyncio.gather(*list(restarted.tasks))

    current = next(d for d in financial.document_refs if d.id == document.id)
    assert (current.type, current.status) == (DocumentType.minutes, "classified")
    assert data.data_requests[CONTRACT_REQUEST].status == "open"
