"""需求 R13: 账表导入接口 (先解析, 再提交) 与导入字段定义; 存储用假实现."""

import asyncio
import io
from pathlib import Path
from typing import Any

import openpyxl
import pytest

from backend.contracts.data import ImportCommit, ImportResult, TableKind
from backend.contracts.errors import DataRejected, DecisionRejected
from backend.contracts.models import DocumentType, TrailEntry, TrailKind
from backend.contracts.records import Account
from backend.orchestration.runner import GraphRunner
from backend.services import data_import
from backend.services.data_service import DataService
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    FakeDataStore,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeTrail,
    api_client,
    make_runner,
)

EID = ENGAGEMENT.id
DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"
BASE = f"/api/v1/engagements/{EID}/data/imports"


def setup(data: FakeDataStore | None = None) -> tuple[GraphRunner, FakeDataStore]:
    data = data if data is not None else FakeDataStore(ready=False)
    runner, _ = make_runner(
        engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]),
        data=data,
        financial_store=FakeFinancialStore(),
    )
    return runner, data


def csv_file(name: str) -> dict[str, tuple[str, bytes, str]]:
    return {"files": (name, (DEMO / name).read_bytes(), "text/csv")}


def workbook() -> bytes:
    book = openpyxl.Workbook()
    first = book.active
    assert first is not None
    first.title = "Cover"
    first.append(["Prepared for audit"])
    sheet = book.create_sheet("TB")
    sheet.append(["Period", "Account", "Balance"])
    sheet.append(["current", 1010, 1258400.0])
    sheet.append(["prior", 1010, 1000000])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


async def test_upload_returns_preview_with_suggestions() -> None:
    runner, data = setup()
    async with api_client(runner) as client:
        response = await client.post(BASE, files=csv_file("journal_entries.csv"), data={"request_id": "REQ-x"})

    assert response.status_code == 200
    body = response.json()
    assert body["import_id"].startswith("IMP-")
    assert (body["file_name"], body["sheets"], body["sheet"]) == (
        "journal_entries.csv",
        ["journal_entries"],
        "journal_entries",
    )
    assert body["row_count"] == 2686
    assert body["suggested_kind"] == "journal_entries"
    assert body["suggested_mapping"]["posting_date"] == "posting_date"
    assert body["suggested_mapping"]["approved_at"] is None
    assert body["sample"]["entry_no"] == "CP-AP-2025-07-A"
    assert body["suggested_date_format"] == "ymd"
    [session] = data.imports.values()
    assert (session[0].request_id, session[0].created_by, session[0].status) == ("REQ-x", "U-1", "parsed")


async def test_upload_rejects_bad_files(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _ = setup()
    monkeypatch.setattr(data_import, "MAX_BYTES", 10)
    async with api_client(runner) as client:
        big = await client.post(BASE, files=csv_file("accounts.csv"))
        monkeypatch.setattr(data_import, "MAX_BYTES", 50 * 1024 * 1024)
        pdf = await client.post(BASE, files={"files": ("tb.pdf", b"%PDF-1.4", "application/pdf")})
        pair = [("files", csv_file("accounts.csv")["files"]), ("files", csv_file("trial_balance.csv")["files"])]
        two = await client.post(BASE, files=pair)
        broken = await client.post(BASE, files={"files": ("tb.xlsx", b"not a workbook", "application/octet-stream")})
    assert (big.status_code, big.json()["detail"]["code"]) == (413, "FILE_TOO_LARGE")
    assert (pdf.status_code, pdf.json()["detail"]["code"]) == (422, "INVALID_VALUE")
    assert (two.status_code, two.json()["detail"]["code"]) == (422, "INVALID_VALUE")
    assert (broken.status_code, broken.json()["detail"]["code"]) == (422, "INVALID_VALUE")


async def test_change_sheet_and_commit_workbook() -> None:
    runner, data = setup()
    async with api_client(runner) as client:
        started = (await client.post(BASE, files={"files": ("tb.xlsx", workbook(), "application/vnd.ms-excel")})).json()
        assert (started["sheets"], started["sheet"], started["suggested_kind"]) == (["Cover", "TB"], "Cover", None)
        iid = started["import_id"]
        changed = await client.put(f"{BASE}/{iid}/sheet", json={"sheet": "TB"})
        missing = await client.put(f"{BASE}/{iid}/sheet", json={"sheet": "Nope"})
        body = {
            "kind": "trial_balance",
            "sheet": "TB",
            "date_format": "dmy",
            "mapping": {"period": "Period", "account_code": "Account", "balance_cents": "Balance"},
        }
        committed = await client.post(f"{BASE}/{iid}/commit", json=body)
        again = await client.post(f"{BASE}/{iid}/commit", json=body)  # 同一提交的重试: 不再写, 返回上次结果
        other = await client.post(f"{BASE}/{iid}/commit", json={**body, "date_format": "mdy"})

    assert changed.status_code == 200
    assert (changed.json()["sheet"], changed.json()["suggested_kind"], changed.json()["row_count"]) == (
        "TB",
        "trial_balance",
        2,
    )
    assert (missing.status_code, missing.json()["detail"]["code"]) == (422, "INVALID_VALUE")
    assert (committed.status_code, committed.json()) == (202, {"kind": "trial_balance", "records": 2})
    [(kind, rows)] = data.written[-1][0].items()
    assert kind is TableKind.trial_balance
    assert [(r.period, r.account_code, r.balance_cents) for r in rows] == [  # type: ignore[attr-defined]
        ("current", "1010", 125840000),
        ("prior", "1010", 100000000),
    ]
    assert (again.status_code, again.json()) == (202, {"kind": "trial_balance", "records": 2})
    assert (other.status_code, other.json()["detail"]["code"]) == (422, "INVALID_VALUE")
    assert len(data.written) == 1


async def test_commit_registers_original_writes_version_and_trail() -> None:
    runner, data = setup()
    async with api_client(runner) as client:
        iid = (await client.post(BASE, files=csv_file("accounts.csv"), data={"request_id": "REQ-a"})).json()[
            "import_id"
        ]
        response = await client.post(
            f"{BASE}/{iid}/commit",
            json={
                "kind": "accounts",
                "sheet": "accounts",
                "date_format": "dmy",
                "mapping": {"code": "code", "name": "name", "type": "type"},
            },
        )
        summary = (await client.get(f"/api/v1/engagements/{EID}/data/summary")).json()

    assert response.json() == {"kind": "accounts", "records": 23}
    tables, kwargs = data.written[-1]
    accounts = [r for r in tables[TableKind.accounts] if isinstance(r, Account)]
    freight = next(a for a in accounts if a.code == "6300")
    assert (freight.category, freight.category_basis) == ("operating_expenses", "name")  # 按名称归类
    [doc] = [d for d in await runner.deps.financial_store.documents(EID) if d.file_name == "accounts.csv"]
    assert (doc.file_name, doc.type, doc.media_type, doc.channel, doc.uploaded_by, doc.status) == (
        "accounts.csv",
        DocumentType.other,
        "text/csv",
        "auditor_upload",
        "U-1",
        "extracted",
    )
    assert kwargs == {
        "source": "import",
        "actor": "U-1",
        "file_name": "accounts.csv",
        "document_id": doc.id,
        "import_id": iid,
        "request_id": "REQ-a",
    }
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    [entry] = [e for e in trail.entries if e.kind is TrailKind.data and e.payload["action"] == "import"]
    assert entry.payload == {
        "action": "import",
        "table_kind": "accounts",
        "records": 23,
        "file_name": "accounts.csv",
        "document_id": doc.id,
        "request_id": "REQ-a",
    }
    accounts_row = next(t for t in summary["tables"] if t["kind"] == "accounts")
    assert (accounts_row["source"], accounts_row["file_name"], accounts_row["records"]) == (
        "import",
        "accounts.csv",
        23,
    )
    assert not runner.is_running(EID)  # 项目锁已释放


async def test_commit_reports_parse_failures_and_mapping_problems() -> None:
    runner, data = setup()
    text = "period,account,balance\n" + "".join(f"current,{n},x{n}\n" for n in range(25))
    async with api_client(runner) as client:
        iid = (await client.post(BASE, files={"files": ("tb.csv", text.encode(), "text/csv")})).json()["import_id"]
        mapping = {"period": "period", "account_code": "account", "balance_cents": "balance"}
        bad = await client.post(
            f"{BASE}/{iid}/commit",
            json={"kind": "trial_balance", "sheet": "tb", "date_format": "dmy", "mapping": mapping},
        )
        short = await client.post(
            f"{BASE}/{iid}/commit",
            json={
                "kind": "trial_balance",
                "sheet": "tb",
                "date_format": "dmy",
                "mapping": {**mapping, "balance_cents": None},
            },
        )
        unknown = await client.post(
            f"{BASE}/IMP-nope/commit", json={"kind": "accounts", "sheet": "x", "date_format": "dmy", "mapping": {}}
        )
    assert bad.status_code == 422
    detail = bad.json()["detail"]
    assert detail["code"] == "IMPORT_PARSE_ERROR"
    assert len(detail["errors"]) == 20
    assert detail["errors"][0] == {"row": 2, "column": "balance", "value": "x0"}
    assert (short.status_code, short.json()["detail"]["code"]) == (422, "MISSING_FIELD")
    assert unknown.status_code == 404
    assert data.written == [] and not runner.is_running(EID)


async def test_commit_rejected_when_busy_or_locked() -> None:
    runner, data = setup()
    engagements = runner.deps.engagement_store
    assert isinstance(engagements, FakeEngagementStore)
    body = {
        "kind": "accounts",
        "sheet": "accounts",
        "date_format": "dmy",
        "mapping": {"code": "code", "name": "name", "type": "type"},
    }
    async with api_client(runner) as client:
        iid = (await client.post(BASE, files=csv_file("accounts.csv"))).json()["import_id"]
        runner.acquire(EID)
        busy = await client.post(f"{BASE}/{iid}/commit", json=body)
        runner.release(EID)
        await engagements.update(EID, locked=True)
        locked = await client.post(f"{BASE}/{iid}/commit", json=body)
        upload = await client.post(BASE, files=csv_file("accounts.csv"))
    assert (busy.status_code, busy.json()["detail"]["code"]) == (409, "ENGAGEMENT_BUSY")
    assert (locked.status_code, locked.json()["detail"]["code"]) == (423, "ENGAGEMENT_LOCKED")
    assert (upload.status_code, upload.json()["detail"]["code"]) == (423, "ENGAGEMENT_LOCKED")
    assert data.written == []


async def test_schemas_endpoint() -> None:
    runner, _ = setup()
    async with api_client(runner) as client:
        response = await client.get("/api/v1/data/schemas")
    assert response.status_code == 200
    schemas = {s["kind"]: s["fields"] for s in response.json()}
    assert list(schemas) == [k.value for k in TableKind]
    assert schemas["approval_log"] == [
        {"name": "entry_no", "required": True, "type": "text"},
        {"name": "created_by", "required": False, "type": "text"},
        {"name": "approved_by", "required": False, "type": "text"},
        {"name": "approved_at", "required": False, "type": "datetime"},
    ]
    assert {"name": "balance_cents", "required": True, "type": "amount"} in schemas["trial_balance"]


async def test_launch_holds_lock_until_job_ends(caplog: pytest.LogCaptureFixture) -> None:
    """后台作业 (Xero 同步, 文件处理用): 调用时取锁并登记, 作业期间别的写操作 ENGAGEMENT_BUSY, 结束 (含出错) 后释放."""
    runner, data = setup()
    service = DataService(runner.deps, runner)
    gate = asyncio.Event()

    async def sync() -> None:
        await gate.wait()
        await service.write_tables(EID, {TableKind.bank_accounts: []}, source="xero", actor="U-1")

    await service.launch(EID, ["bank_accounts"], sync)
    assert runner.is_running(EID) and service.processing(EID) == [frozenset({"bank_accounts"})]
    with pytest.raises(DecisionRejected) as busy:
        await service.launch(EID, ["accounts"], sync)
    assert busy.value.code == "ENGAGEMENT_BUSY"
    gate.set()
    await asyncio.gather(*service.tasks)
    assert not runner.is_running(EID) and service.processing(EID) == []
    assert data.written[-1][1]["source"] == "xero"
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    actions = [e.payload["action"] for e in trail.entries if e.kind is TrailKind.data]
    assert actions.count("sync") == 1 and actions[0] == "sync"  # 其后是 A3 体检

    async def broken() -> None:
        raise RuntimeError("Xero 超时")

    await service.launch(EID, ["accounts"], broken)
    await asyncio.gather(*service.tasks)
    assert not runner.is_running(EID) and "Xero 超时" in caplog.text


async def test_write_tables_requires_project_lock() -> None:
    runner, data = setup()
    service = DataService(runner.deps, runner)
    with pytest.raises(RuntimeError):
        await service.write_tables(EID, {TableKind.accounts: []}, source="import", actor="U-1")
    assert data.written == []


async def test_shutdown_before_job_starts_releases_lock() -> None:
    """后台作业还没开始就被取消 (应用退出): 也注销并释放锁 (Codex 审核 FD-R13 的探针)."""
    runner, _ = setup()
    service = DataService(runner.deps, runner)

    async def job() -> None:
        await asyncio.Event().wait()

    await service.launch(EID, ["accounts"], job)
    await service.shutdown()
    assert not runner.is_running(EID) and service.processing(EID) == []


async def test_commit_retry_after_trail_failure_backfills_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """写入后 data 留痕写失败 (接口报错, 数据已提交): 同一提交重试时补写一条留痕, 不再写入
    (Codex 审核 FD-R13 的探针)."""
    runner, data = setup()
    service = DataService(runner.deps, runner)
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    preview = await service.start_import(EID, "accounts.csv", b"code,name,type\n1010,Cash,asset\n", actor="U-1")
    body = ImportCommit(
        kind=TableKind.accounts, sheet=preview.sheet, date_format="dmy", mapping=preview.suggested_mapping
    )
    original = trail.record

    async def unavailable(entry: TrailEntry) -> str:
        raise RuntimeError("留痕暂时不可用")

    monkeypatch.setattr(trail, "record", unavailable)
    with pytest.raises(RuntimeError):
        await service.commit_import(EID, preview.import_id, body, "U-1")
    assert data.imports[preview.import_id][0].status == "committed" and not runner.is_running(EID)
    monkeypatch.setattr(trail, "record", original)

    result = await service.commit_import(EID, preview.import_id, body, "U-1")

    assert result == ImportResult(kind=TableKind.accounts, records=1)
    entries = [e for e in trail.entries if e.kind is TrailKind.data and e.payload["action"] == "import"]
    assert [(e.idempotency_key, e.payload["records"]) for e in entries] == [(f"{EID}:data:v1:accounts", 1)]
    assert data.data_version == 1 and len(data.written) == 1
    # 留痕不可用时体检照常存下 (留痕在重试时补写, 不重复)
    assert data.last_run is not None and data.last_run.data_version == 1
    checks = [e for e in trail.entries if e.kind is TrailKind.data and e.payload["action"] == "check"]
    assert len(checks) == 1


async def test_sheet_change_cannot_touch_committed_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """换表与提交互斥 (都取项目锁), 提交后换表报已提交, 记下的选择不变 (Codex 审核 FD-R13 第 2 轮)."""
    runner, data = setup()
    service = DataService(runner.deps, runner)
    started = await service.start_import(EID, "tb.xlsx", workbook(), actor="U-1")
    await service.change_sheet(EID, started.import_id, "TB")
    body = ImportCommit(
        kind=TableKind.trial_balance,
        sheet="TB",
        date_format="dmy",
        mapping={"period": "Period", "account_code": "Account", "balance_cents": "Balance"},
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = data.write_tables

    async def slow_write(*args: Any, **kwargs: Any) -> int:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(data, "write_tables", slow_write)
    commit = asyncio.create_task(service.commit_import(EID, started.import_id, body, "U-1"))
    await entered.wait()
    with pytest.raises(DecisionRejected) as busy:  # 提交进行中: 换表拿不到项目锁
        await service.change_sheet(EID, started.import_id, "Cover")
    assert busy.value.code == "ENGAGEMENT_BUSY"
    release.set()
    await commit
    with pytest.raises(DataRejected) as committed:
        await service.change_sheet(EID, started.import_id, "Cover")
    assert committed.value.code == "INVALID_VALUE"
    session = data.imports[started.import_id][0]
    assert (session.status, session.sheet, session.date_format) == ("committed", "TB", "dmy")
    assert await service.commit_import(EID, started.import_id, body, "U-1") == ImportResult(
        kind=TableKind.trial_balance, records=2
    )  # 原提交的重试仍认得出
