"""需求 R273: 确认数据已齐 (POST /data/ready) 时的理由与当时最近一次体检的未通过项记入就绪确认与 data 留痕. 理由
选填 (Issue #61; R273 原为有未通过项时必填, 报 DATA_CHECKS_FAILED): 没写的记为空, 未通过项照记. 存储用假实现 (连库的
见 test_data_store_postgres)."""

import csv
import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.contracts.data import CheckId, CheckInputs, DataReadiness
from backend.contracts.models import TrailEntry, TrailKind
from backend.orchestration.runner import GraphRunner, awaiting_data
from backend.orchestration.trail import data_ready_entry
from backend.services.data_service import DataService
from tests.fakes import FakeDataStore, FakeTrail, api_client, wait_idle
from tests.test_data_ready import EID, waiting_for_data

DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"
READY = f"/api/v1/engagements/{EID}/data/ready"
IMPORTS = f"/api/v1/engagements/{EID}/data/imports"
REASON = "差额为客户录入错误, 已要求期后更正; 先按现有数据做计划阶段分析"
FAILED = ["debits_equal_credits", "journals_agree_tb"]


def unbalanced_journals() -> bytes:
    """Numbat 总账的副本 (只在内存里改), 第一行 CP-AP-2025-07-A-1 (借 2100 应付账款) 的借方改大 1 元."""
    reader = csv.DictReader(io.StringIO((DEMO / "journal_entries.csv").read_text(encoding="utf-8")))
    rows = list(reader)
    assert (rows[0]["id"], rows[0]["account_code"], rows[0]["debit_cents"]) == ("CP-AP-2025-07-A-1", "2100", "35500000")
    rows[0]["debit_cents"] = "35500100"
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(reader.fieldnames or ()), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


async def import_csv(client: httpx.AsyncClient, name: str, content: bytes) -> None:
    """经导入接口上传并按系统建议的表种类, 工作表, 日期写法与列对应提交."""
    preview = (await client.post(IMPORTS, files={"files": (name, content, "text/csv")})).json()
    body = {
        "kind": preview["suggested_kind"],
        "sheet": preview["sheet"],
        "date_format": preview["suggested_date_format"],
        "mapping": preview["suggested_mapping"],
    }
    committed = await client.post(f"{IMPORTS}/{preview['import_id']}/commit", json=body)
    assert committed.status_code == 202, committed.text


async def import_numbat(client: httpx.AsyncClient, journals: bytes) -> None:
    """导入 Numbat 的三张必需表: 科目表与试算表照原文件, 总账用给的内容."""
    for name in ("accounts.csv", "trial_balance.csv"):
        await import_csv(client, name, (DEMO / name).read_bytes())
    await import_csv(client, "journal_entries.csv", journals)


def statuses(data: FakeDataStore) -> dict[str, str]:
    return {c.id.value: c.status for c in data.check_results}


def ready_entries(runner: GraphRunner) -> list[TrailEntry]:
    """确认数据已齐的 data 留痕 (键 {eid}:data:ready 与补记理由时的 {eid}:data:ready:checks_failed), 按写入顺序."""
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e for e in trail.entries if e.kind is TrailKind.data and e.payload["action"] == "ready"]


@pytest.mark.parametrize(
    ("body", "reason", "summary"),
    [
        ({"reason": f" {REASON}\n"}, REASON, "体检 2 项不通过, 写明理由后确认数据已齐, 开始计划阶段分析"),
        ({"reason": "  \n "}, None, "体检 2 项不通过, 未填理由, 确认数据已齐, 开始计划阶段分析"),
        (None, None, "体检 2 项不通过, 未填理由, 确认数据已齐, 开始计划阶段分析"),
    ],
    ids=["given", "blank", "no_body"],
)
async def test_failed_checkup_confirms_with_or_without_reason(
    body: dict[str, str] | None, reason: str | None, summary: str
) -> None:
    """验收 1: 总账一行借方改大 1 元后导入, 借贷平衡不通过 (该科目发生额随之不同, 分录与试算表一致也不通过).
    理由选填 (Issue #61): 带理由, 理由为空白或不带请求体都照常确认, 继续到 PLN-01/03 闸门; 理由 (去首尾空白, 空的
    记为空) 与未通过项记入就绪确认与留痕, 摘要写明有没有填理由. 原文件不变."""
    original = hashlib.sha256((DEMO / "journal_entries.csv").read_bytes()).hexdigest()
    runner, _, data = await waiting_for_data()

    async with api_client(runner) as client:
        await import_numbat(client, unbalanced_journals())
        assert statuses(data) == {
            "debits_equal_credits": "failed",
            "journals_agree_tb": "failed",
            "count_agrees_source": "passed",
            "mapping_complete": "passed",
            "preparer_approver_available": "passed",
        }
        balance = next(c for c in data.check_results if c.id is CheckId.debits_equal_credits)
        assert balance.params == {"debit_cents": 5271620100, "credit_cents": 5271620000}
        confirmed = await client.post(READY, json=body)
        await wait_idle(runner, EID)

    assert confirmed.status_code == 202, confirmed.text
    readiness = confirmed.json()
    assert (readiness["ready_by"], readiness["ready_reason"], readiness["ready_failed_checks"]) == (
        "U-1",
        reason,
        FAILED,
    )
    assert data.confirm_calls == [(EID, "U-1")]
    assert (data.ready_reason, data.ready_failed_checks) == (reason, [CheckId(c) for c in FAILED])
    [entry] = ready_entries(runner)
    assert entry.actor == "U-1"
    assert entry.payload == {"action": "ready", **({"reason": reason} if reason else {}), "failed_checks": FAILED}
    assert entry.summary == summary
    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)
    assert hashlib.sha256((DEMO / "journal_entries.csv").read_bytes()).hexdigest() == original


async def test_passed_checkup_confirms_without_reason() -> None:
    """验收 2: 原样导入 Numbat 三张必需表, 体检五项都通过: 不带理由照常确认, 记录与留痕同以前."""
    runner, _, data = await waiting_for_data()

    async with api_client(runner) as client:
        await import_numbat(client, (DEMO / "journal_entries.csv").read_bytes())
        assert "failed" not in statuses(data).values()
        response = await client.post(READY)
        await wait_idle(runner, EID)

    assert response.status_code == 202
    assert (response.json()["ready_reason"], response.json()["ready_failed_checks"]) == (None, [])
    assert [(e.actor, e.payload) for e in ready_entries(runner)] == [("U-1", {"action": "ready"})]
    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)


async def test_retry_keeps_first_reason_and_still_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """确认已记而后台没启动: 再确认时照常校验 (理由选填, 不带也通过, Issue #61); 带了别的理由也保留第一次的人, 理由
    与未通过项, 留痕只有一条."""
    runner, _, data = await waiting_for_data()
    async with api_client(runner) as client:
        await import_numbat(client, unbalanced_journals())
    launch = runner.launch

    def fail_launch(eid: str, graph_input: object) -> None:
        runner.release(eid)
        raise RuntimeError("后台启动失败")

    monkeypatch.setattr(runner, "launch", fail_launch)
    service = DataService(runner.deps, runner)
    with pytest.raises(RuntimeError):
        await service.confirm_ready(EID, "U-1", REASON)
    monkeypatch.setattr(runner, "launch", launch)

    again = await service.confirm_ready(EID, "U-2", "另一个理由")
    await wait_idle(runner, EID)

    assert (again.ready_by, again.ready_reason, again.ready_failed_checks) == ("U-1", REASON, FAILED)
    assert data.confirm_calls == [(EID, "U-1"), (EID, "U-2")]
    [entry] = ready_entries(runner)
    assert (entry.actor, entry.payload["reason"]) == ("U-1", REASON)
    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)


async def test_recovery_backfills_reason_into_ready_trail(monkeypatch: pytest.MonkeyPatch) -> None:
    """确认 (带理由) 已写入而留痕没写成: recover 补写的留痕取存下的理由与未通过项."""
    runner, _, _ = await waiting_for_data()
    async with api_client(runner) as client:
        await import_numbat(client, unbalanced_journals())
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    original = trail.record

    async def fail_data(entry: TrailEntry) -> str:
        if entry.kind is TrailKind.data:
            raise OSError("留痕不可用")
        return await original(entry)

    monkeypatch.setattr(trail, "record", fail_data)
    with pytest.raises(OSError):
        await DataService(runner.deps, runner).confirm_ready(EID, "U-1", REASON)
    monkeypatch.setattr(trail, "record", original)
    assert awaiting_data(await runner.app.aget_state(GraphRunner.config(EID)))

    restarted = GraphRunner(runner.app, runner.deps)
    await restarted.recover()
    await wait_idle(restarted, EID)

    assert [(e.actor, e.payload) for e in ready_entries(runner)] == [
        ("U-1", {"action": "ready", "reason": REASON, "failed_checks": FAILED})
    ]
    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)


def test_dataset_ready_entry_is_unchanged() -> None:
    """数据集载入视同确认: 没有理由与未通过项, 留痕同以前."""
    readiness = DataReadiness(data_version=1, ready_at=datetime(2026, 1, 1, tzinfo=UTC), ready_by="dataset")
    entry = data_ready_entry(EID, readiness)
    assert (entry.actor, entry.summary, entry.payload) == (
        "dataset",
        "数据集已载入, 视同确认数据已齐",
        {"action": "ready"},
    )


# ── 审核 R273 第 1 轮后补充: 体检过期时先补做, 重复确认补记理由, 缺表先于体检 ──


async def test_stale_checkup_is_redone_before_judging(monkeypatch: pytest.MonkeyPatch) -> None:
    """R273-01: 写入不平的总账后体检出错, data_checks 还是上一版的全过. 确认时先补做体检, 记下的是补做出的未通过项
    (不带理由照常确认, Issue #61)."""
    runner, _, data = await waiting_for_data()
    async with api_client(runner) as client:
        await import_numbat(client, (DEMO / "journal_entries.csv").read_bytes())
        inputs = data.check_inputs

        async def broken(*args: Any, **kwargs: Any) -> CheckInputs:
            raise RuntimeError("汇总失败")

        monkeypatch.setattr(data, "check_inputs", broken)
        await import_csv(client, "journal_entries.csv", unbalanced_journals())  # 写入成功, 体检出错只记日志
        monkeypatch.setattr(data, "check_inputs", inputs)
        assert "failed" not in statuses(data).values()  # 最近一次体检仍是上一版总账的
        plain = await client.post(READY)
        await wait_idle(runner, EID)

    assert plain.status_code == 202, plain.text
    assert (plain.json()["ready_reason"], plain.json()["ready_failed_checks"]) == (None, FAILED)
    assert statuses(data)["debits_equal_credits"] == "failed"  # 已补做
    assert data.confirm_calls == [(EID, "U-1")]


@pytest.mark.parametrize(("body", "reason"), [({"reason": REASON}, REASON), (None, None)], ids=["given", "none"])
async def test_retry_after_clean_confirmation_records_failed_checks(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, str] | None, reason: str | None
) -> None:
    """R273-02: 第一次确认时体检全过而留痕没写成, 之后导入不平的总账. 再确认时补记这次的未通过项与理由 (选填, Issue
    #61; 没写的记为空) 进确认记录 (人与时间仍是第一次的) 与补写的留痕, 不静默丢弃."""
    runner, _, data = await waiting_for_data()
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    original = trail.record

    async def fail_ready(entry: TrailEntry) -> str:
        if entry.kind is TrailKind.data and entry.payload["action"] == "ready":
            raise OSError("留痕不可用")
        return await original(entry)

    async with api_client(runner) as client:
        await import_numbat(client, (DEMO / "journal_entries.csv").read_bytes())
        monkeypatch.setattr(trail, "record", fail_ready)
        with pytest.raises(OSError):
            await client.post(READY)  # 确认已记 (没有理由与未通过项), 留痕没写成
        monkeypatch.setattr(trail, "record", original)
        first = data.ready_at
        await import_csv(client, "journal_entries.csv", unbalanced_journals())  # 图仍在等, 还能导入
        confirmed = await client.post(READY, json=body)
        await wait_idle(runner, EID)

    assert confirmed.status_code == 202, confirmed.text
    readiness = confirmed.json()
    assert (readiness["ready_by"], readiness["ready_reason"], readiness["ready_failed_checks"]) == (
        "U-1",
        reason,
        FAILED,
    )
    assert (data.ready_at, data.ready_reason, data.ready_failed_checks) == (first, reason, [CheckId(c) for c in FAILED])
    [entry] = ready_entries(runner)
    assert entry.payload == {"action": "ready", **({"reason": reason} if reason else {}), "failed_checks": FAILED}
    assert entry.idempotency_key == f"{EID}:data:ready:checks_failed"
    assert (await runner.app.aget_state(GraphRunner.config(EID))).next == ("gate_p1_risk",)


async def test_supplemented_reason_gets_its_own_trail_entry() -> None:
    """R273-02 续: 第一次确认的留痕已写 (体检全过), 后台没启动; 补记理由后另记一条带理由的留痕, 第一条不变."""
    data = FakeDataStore(ready=False)
    entry = data_ready_entry(EID, await data.confirm_ready(EID, "U-1"))
    again = await data.confirm_ready(EID, "U-2", reason=REASON, failed_checks=[CheckId.debits_equal_credits])
    supplement = data_ready_entry(EID, again)

    assert (entry.idempotency_key, entry.payload) == (f"{EID}:data:ready", {"action": "ready"})
    assert supplement.idempotency_key == f"{EID}:data:ready:checks_failed"
    assert (supplement.actor, supplement.payload["reason"]) == ("U-1", REASON)
    # 已记了未通过项的不再改
    third = await data.confirm_ready(EID, "U-3", reason="别的", failed_checks=[CheckId.journals_agree_tb])
    assert (third.ready_by, third.ready_reason, third.ready_failed_checks) == (
        "U-1",
        REASON,
        [CheckId.debits_equal_credits],
    )


async def test_missing_tables_are_reported_before_failed_checks() -> None:
    """R273-03: 缺试算表而总账不平时先报 DATA_NOT_READY, 不记确认."""
    runner, _, data = await waiting_for_data()
    async with api_client(runner) as client:
        await import_csv(client, "accounts.csv", (DEMO / "accounts.csv").read_bytes())
        await import_csv(client, "journal_entries.csv", unbalanced_journals())
        assert statuses(data)["debits_equal_credits"] == "failed"
        response = await client.post(READY)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert (detail["code"], detail["missing_tables"]) == ("DATA_NOT_READY", ["trial_balance"])
    assert data.confirm_calls == []
