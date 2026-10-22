"""需求 R330 (Issue #50): 底稿记录. 程序执行终态与决定之后刷新 (依据, 结论草稿, 缺项), 审计师改结论, 补充说明与签署,
依据变了改为须重新复核, 不阻塞 FIN-01 与 RPT-01, 锁定后不能改, 刷新失败只记日志.

两种走法: 演示配置的离线走通 (tests/test_demo_walkthrough.walk, 编排里的刷新照常发生) 看各底稿的版本历史; 假存储里
直接放 Xero 演示 REV-08 第 1 次执行的执行记录, 被测总体与例外 (tests.xero_fakes) 与 PLN-01/03 的通过决定, 经接口
做审计师的三种操作."""

from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
import pytest_asyncio

from backend.contracts.models import (
    ExceptionDisposition,
    GateDecision,
    GateId,
    GateVerdict,
    Judgment,
    ObjectRef,
    Phase,
    ProcedureRun,
    RunPopulation,
    TrailKind,
    Workpaper,
    WorkpaperStatus,
)
from backend.core.config import get_settings
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.runner import GraphRunner
from backend.services import workpapers
from backend.services.procedure_library import ProcedureLibrary
from backend.services.workpapers import SATISFIED, refresh
from tests.fakes import (
    DECIDED_AT,
    ENGAGEMENT,
    NUMBAT_AUDITOR,
    FakeEngagementStore,
    FakeOutputStore,
    FakeTrail,
    acc01_decision,
    api_client,
    finished,
    make_procedure,
    make_run,
    make_runner,
    wait_idle,
)
from tests.fixture_sets import XERO
from tests.test_demo_walkthrough import Session, Walk, walk
from tests.xero_fakes import XERO17_MATERIALITY, xero_rev08_exception, xero_rev08_populations
from tests.xero_flow import demo_offline
from tests.xero_harness import LayeredMockLLMClient

EID = ENGAGEMENT.id
BASE = f"/api/v1/engagements/{EID}/workpapers"
REV08_PARAMS = {"window_days": 20, "period_end": date(2026, 9, 30)}  # 特别风险的窗口 (同 tests.xero_fakes)
# 验收 1: REV-08 第 1 次执行之后的结论草稿 (Xero 演示: 窗口内 6 张发票, 其中 5 张找得到收款, ORC1026 收款不符)
D3_DRAFT = {
    "zh": "范围: 期末前 20 天 (2026-09-10 至 2026-09-30) 入账的销售发票. 被测对象 6 项, 合计 AUD 9,900.00. "
    "入账期间正确: 6 项均符合. 有服务完成记录: 6 项均符合. 收款金额相符: 5 项中 1 项不符, 1 项不作判断. "
    "例外 1 条, 待处理 1 条.",
    "en": "Scope: Sales invoices posted in the 20 days before period end (2026-09-10 to 2026-09-30). "
    "6 items tested, totalling AUD 9,900.00. Recorded in the correct period: all 6 agree. "
    "Service completion record exists: all 6 agree. "
    "Receipts agree to the invoice: 1 of 5 do not agree, 1 not assessed. 1 exception, 1 pending.",
}
UNSIGNED = ["reviewer", "reviewed_on", "review_scope"]  # 未签署时缺的通用三项 (ASA 230 第 9(c) 段)


def test_every_required_item_has_a_check() -> None:
    """底稿配置的每个必填项都有判断是否已记录的规则, 规则表里也没有配置之外的代号."""
    library = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
    assert {r.id for r in library.workpapers.required} == set(SATISFIED)


# ── 走通里的刷新 (验收 1, 2, 6, 7) ──

shared_loop = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def walked() -> AsyncIterator[tuple[Walk, httpx.AsyncClient]]:
    """演示配置离线走通一次 (同 tests/test_demo_walkthrough 的 walked, 工作语言 en), 走完不关接口客户端."""
    XERO.ready()
    llm = LayeredMockLLMClient([XERO.runtime])
    async with demo_offline(llm) as (runner, service, client):
        yield await walk(Session(client, runner, service), llm), client


async def history(w: Walk, ref: str) -> list[Workpaper]:
    return await w.x.runner.deps.output_store.history(w.x.eid, Workpaper, f"WP-{ref}")


@pytest.mark.external
@shared_loop
async def test_rule_workpaper_after_first_run(walked: tuple[Walk, httpx.AsyncClient]) -> None:
    """验收 1: REV-08 第 1 次执行之后 WP-D-3 第 1 版为 draft, 依据为执行记录, 被测总体与例外, 结论草稿按模板; 之后
    ORC1026 的判断确认, 例外归因 (第 2 版), 草稿改写为已由判断解释."""
    w, _ = walked
    first, second, *rest = await history(w, "D-3")
    assert (first.version, first.status, first.procedure_id, first.area) == (1, WorkpaperStatus.draft, "REV-08", "D")
    assert first.basis == [
        ObjectRef(kind="AuditException", id="X-REV-08-1-R03-1", version=1),
        ObjectRef(kind="ProcedureRun", id="RUN-REV-08-1", version=1),
        ObjectRef(kind="RunPopulation", id="POP-RUN-REV-08-1", version=1),
    ]
    assert first.conclusion_draft is not None and first.conclusion_draft.model_dump() == D3_DRAFT
    assert first.missing == UNSIGNED
    assert ObjectRef(kind="AuditException", id="X-REV-08-1-R03-1", version=2) in second.basis
    assert second.conclusion_draft is not None
    assert second.conclusion_draft.zh.endswith("例外 1 条, 待处理 0 条, 已由判断解释 1 条.")
    assert rest == []


@pytest.mark.external
@shared_loop
async def test_judgment_workpaper_follows_reviews(walked: tuple[Walk, httpx.AsyncClient]) -> None:
    """验收 2: REV-03 的 WP-D-2 第 1 版列 3 份合同的建议与置信度, 待复核 3 项, 末句同 Q27; 三个复核决定各出一版,
    最后一版待复核 0 项."""
    w, _ = walked
    versions = await history(w, "D-2")
    drafts = [v.conclusion_draft.zh if v.conclusion_draft else "" for v in versions]
    assert drafts[0] == (
        "判断对象 3 个: CON-X01 建议接受, 置信度 0.84, 待复核; CON-X02 建议接受, 置信度 0.84, 待复核; "
        "CON-X03 建议接受, 置信度 0.82, 待复核. 待复核 3 项. 最终结论待审计师复核后确定."
    )
    assert [d.split("待复核 ")[-1][0] for d in drafts] == ["3", "2", "1", "0"]
    assert all(d.endswith("最终结论待审计师复核后确定.") for d in drafts)


@pytest.mark.external
@shared_loop
async def test_unsigned_workpapers_do_not_block_and_lock_after_archive(walked: tuple[Walk, httpx.AsyncClient]) -> None:
    """验收 6, 7: 走通时没有签署任何底稿, FIN-01 与 RPT-01 照常通过到归档; 列表按档案顺序, 各程序底稿都有记录且都是
    draft; 归档 (锁定) 后三种操作都报 423 ENGAGEMENT_LOCKED, 刷新不再写."""
    w, client = walked
    base = f"/api/v1/engagements/{w.x.eid}/workpapers"
    listed = (await client.get(base)).json()
    assert [r["ref"] for r in listed] == [
        *("A-1", "A-2", "A-3", "A-4", "B-1", "B-2", "C-1", "C-2", "C-3", "C-4", "D-1", "D-2", "D-3", "D-4"),
        *("G-1", "G-2", "X", "X-A", "X-B", "X-C", "Z-1", "Z-2", "Z-3"),
    ]
    assert {r["status"] for r in listed} == {"draft"}
    posts = [
        ("D-3/conclusion", {"text": "x"}),
        ("D-3/notes", {"text": "x"}),
        ("D-3/signoff", {"scope": "full"}),
    ]
    for path, body in posts:
        response = await client.post(f"{base}/{path}", json=body)
        assert (response.status_code, response.json()["detail"]["code"]) == (423, "ENGAGEMENT_LOCKED")
    assert await refresh(w.x.runner.deps, w.x.eid) == []


# ── 审计师的三种操作 (验收 3, 4, 5, 7) ──


def rev08_run(run_no: int) -> ProcedureRun:
    return finished(make_run("REV-08", run_no, dict(REV08_PARAMS)))


def rev08_outputs(run: ProcedureRun) -> list[Any]:
    """REV-08 一次执行的执行记录, 被测总体与例外 (Xero 演示)."""
    population = RunPopulation(id=f"POP-{run.id}", run_id=run.id, procedure_id="REV-08", rules=xero_rev08_populations())
    return [run, population, xero_rev08_exception(run)]


PLN_APPROVE = GateDecision(
    id="D-PLN",
    gate=GateId.PLN_01_03,
    target_id="MATERIALITY",
    target_version=1,
    decision=GateVerdict.approve,
    materiality=XERO17_MATERIALITY,
    decided_by=NUMBAT_AUDITOR.id,
    decided_at=DECIDED_AT,
)


async def seeded() -> tuple[GraphRunner, FakeOutputStore]:
    """程序包有 REV-08 与 PLN-01, Store 里有 REV-08 第 1 次执行的产出与 PLN-01/03 的通过决定 (没写理由), 刷新一次:
    WP-D-3 与 WP-A-2 各第 1 版."""
    runner, store = make_runner()
    pack = [make_procedure("REV-08", Phase.P2_Executing), make_procedure("PLN-01", Phase.P1_Planning)]
    await store.put(EID, [*pack, *rev08_outputs(rev08_run(1)), PLN_APPROVE], actor="A6")
    assert [w.id for w in await refresh(runner.deps, EID)] == ["WP-A-2", "WP-D-3"]
    return runner, store


def decisions(runner: GraphRunner) -> list[dict[str, Any]]:
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e.payload | {"procedure_id": e.procedure_id} for e in trail.entries if e.kind is TrailKind.decision]


async def test_conclusion_and_notes() -> None:
    """验收 3: 改结论出新版本 (草稿照留), decision 留痕一行; 补充说明补齐 A-2 的选择理由 (materiality_factors, 可由
    补充说明补齐) 后它从 missing 去掉; 不可补齐的项, 不认识的项与空文字被拒, 不写."""
    runner, store = await seeded()
    async with api_client(runner) as client:
        before = await store.get(EID, Workpaper, "WP-A-2")
        assert before is not None and "materiality_factors" in before.missing
        concluded = await client.post(f"{BASE}/D-3/conclusion", json={"text": " Agreed, see D-4 for ORC1026. "})
        noted = await client.post(
            f"{BASE}/A-2/notes", json={"text": "Revenue is the stable benchmark.", "item": "materiality_factors"}
        )
        rejected = [
            await client.post(f"{BASE}/A-2/notes", json={"text": "x", "item": "reviewer"}),  # 签署才能补
            await client.post(f"{BASE}/A-2/notes", json={"text": "x", "item": "nope"}),
            await client.post(f"{BASE}/A-2/notes", json={"text": "  "}),
            await client.post(f"{BASE}/D-3/conclusion", json={"text": ""}),
        ]
    assert concluded.status_code == 200, concluded.text
    body = concluded.json()
    assert (body["version"], body["conclusion"], body["conclusion_by"]) == (2, "Agreed, see D-4 for ORC1026.", "U-1")
    assert body["conclusion_draft"]["en"] == D3_DRAFT["en"]
    assert noted.status_code == 200, noted.text
    assert (
        noted.json()["notes"][0]["item"] == "materiality_factors"
        and "materiality_factors" not in noted.json()["missing"]
    )
    codes = [(r.status_code, r.json()["detail"]["code"]) for r in rejected]
    assert codes == [(422, "INVALID_VALUE"), (422, "INVALID_VALUE"), (422, "MISSING_FIELD"), (422, "MISSING_FIELD")]
    assert [(d["action"], d["procedure_id"], d["version"]) for d in decisions(runner)] == [
        ("workpaper_conclusion", "REV-08", 2),
        ("workpaper_note", "PLN-01", 2),
    ]


async def test_signoff() -> None:
    """验收 4: 签署出新版本 status signed, signoff 记签署人, 时间, 范围与意见, 未签署缺的三项随之去掉, decision 留痕;
    意见选填; 范围不认识 422 (请求体校验); 同一人同样再签不出新版本."""
    runner, _ = await seeded()
    async with api_client(runner) as client:
        signed = await client.post(
            f"{BASE}/D-3/signoff", json={"scope": "full", "opinion": "Agreed to supporting records."}
        )
        again = await client.post(
            f"{BASE}/D-3/signoff", json={"scope": "full", "opinion": "Agreed to supporting records."}
        )
        plain = await client.post(f"{BASE}/A-2/signoff", json={"scope": "conclusion_only"})
        bad = await client.post(f"{BASE}/D-3/signoff", json={"scope": "glance"})
    assert signed.status_code == 200, signed.text
    body = signed.json()
    assert (body["version"], body["status"], body["missing"]) == (2, "signed", [])
    signoff = body["signoff"]
    assert (signoff["signed_by"], signoff["scope"], signoff["opinion"]) == (
        "U-1",
        "full",
        "Agreed to supporting records.",
    )
    assert again.json()["version"] == 2
    assert (plain.status_code, plain.json()["signoff"]["opinion"]) == (200, "")
    assert bad.status_code == 422
    assert [(d["action"], d["scope"], d["version"]) for d in decisions(runner)] == [
        ("workpaper_signoff", "full", 2),
        ("workpaper_signoff", "conclusion_only", 2),
    ]


def rerun(run: ProcedureRun) -> list[Any]:
    return rev08_outputs(rev08_run(2))


def attributed(run: ProcedureRun) -> list[Any]:
    exception = xero_rev08_exception(run)
    update = {"version": 2, "disposition": ExceptionDisposition.attributed, "attributed_by": "J-REV-02-1-ORC1026"}
    return [exception.model_copy(update=update)]


@pytest.mark.parametrize("change", [rerun, attributed], ids=["rerun", "exception_decided"])
async def test_signed_workpaper_needs_rereview_when_basis_changes(change: Any) -> None:
    """验收 5: 已签署的 WP-D-3 在 REV-08 重跑 (RUN-REV-08-2) 或其例外有新决定 (归因, 第 2 版) 之后刷新出新版本,
    status needs_rereview, last_signoff 为原签署, signoff 清空; 依据没变的刷新不出新版本."""
    runner, store = await seeded()
    async with api_client(runner) as client:
        signed = (await client.post(f"{BASE}/D-3/signoff", json={"scope": "sample"})).json()
    assert await refresh(runner.deps, EID) == []  # 依据没变
    await store.put(EID, change(rev08_run(1)), actor="A6")
    [refreshed] = await refresh(runner.deps, EID)
    assert (refreshed.id, refreshed.version, refreshed.status) == ("WP-D-3", 3, WorkpaperStatus.needs_rereview)
    assert refreshed.signoff is None and refreshed.last_signoff is not None
    assert refreshed.last_signoff.model_dump(mode="json") == signed["signoff"]
    assert refreshed.missing == UNSIGNED
    assert await refresh(runner.deps, EID) == []


async def test_edit_after_signoff_needs_rereview() -> None:
    """签署后改结论也算内容变了: 新版本 needs_rereview, 签署移到 last_signoff."""
    runner, _ = await seeded()
    async with api_client(runner) as client:
        await client.post(f"{BASE}/D-3/signoff", json={"scope": "full"})
        edited = (await client.post(f"{BASE}/D-3/conclusion", json={"text": "Revised."})).json()
    assert (edited["status"], edited["signoff"], edited["last_signoff"]["scope"]) == ("needs_rereview", None, "full")


@pytest.mark.parametrize(
    ("setup", "path", "code"),
    [
        ({"locked": True}, "D-3/conclusion", (423, "ENGAGEMENT_LOCKED")),
        ({"locked": True}, "D-3/notes", (423, "ENGAGEMENT_LOCKED")),
        ({"locked": True}, "D-3/signoff", (423, "ENGAGEMENT_LOCKED")),
        ({}, "C-3/conclusion", (404, "NOT_FOUND")),  # CSH-05 没有执行, 没有记录
        ({"busy": True}, "D-3/conclusion", (409, "ENGAGEMENT_BUSY")),
    ],
)
async def test_operations_rejected(setup: dict[str, bool], path: str, code: tuple[int, str]) -> None:
    """验收 7: 项目锁定 (签字或拒绝承接) 后三种操作 423; 没有记录的底稿 404; 项目锁被占 409. 都不写."""
    runner, store = await seeded()
    engagements = runner.deps.engagement_store
    assert isinstance(engagements, FakeEngagementStore)
    if setup.get("locked"):
        await engagements.update(EID, locked=True)
    if setup.get("busy"):
        runner.acquire(EID)
    body = {"scope": "full"} if path.endswith("signoff") else {"text": "x"}
    async with api_client(runner) as client:
        response = await client.post(f"{BASE}/{path}", json=body)
    assert (response.status_code, response.json()["detail"]["code"]) == code
    assert {w.version for w in await store.latest(EID, Workpaper)} == {1}


# ── 刷新失败只记日志 (验收 8) ──


async def test_refresh_failure_only_logs(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """刷新出错 (构造的异常) 时程序执行与图照常推进, 只记日志; ACC-01 决定之后的刷新补上 B-2 (ACC-04) 与 B-1."""
    runner, store = make_runner()
    real = workpapers.load

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("构造的刷新失败")

    monkeypatch.setattr(workpapers, "load", broken)
    await runner.start(EID)
    await wait_idle(runner, EID)
    assert runner.run_error(EID) is None and await store.latest(EID, Workpaper) == []
    assert "刷新底稿记录失败" in caplog.text
    monkeypatch.setattr(workpapers, "load", real)
    await GraphDecisionSubmit(runner.app, runner.deps, runner).submit(EID, acc01_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    assert {"WP-B-1", "WP-B-2"} <= {w.id for w in await store.latest(EID, Workpaper)}


@pytest.mark.external
@shared_loop
async def test_latest_judgment_uses_numeric_run_order(walked: tuple[Walk, httpx.AsyncClient]) -> None:
    """第 10 次执行的判断取代第 9 次, 不按执行 ID 的字符串大小选旧判断, 也不混入人工判断."""
    w, _ = walked
    [original, *_] = await w.x.runner.deps.output_store.latest(w.x.eid, Judgment)
    ninth, tenth = finished(make_run(original.procedure_id, 9)), finished(make_run(original.procedure_id, 10))
    old = original.model_copy(update={"id": "J-9", "run_id": ninth.id, "version": 3})
    new = original.model_copy(update={"id": "J-10", "run_id": tenth.id})
    human = new.model_copy(update={"id": "J-10-H", "version": 2})
    assert workpapers.model_judgments([new, human, old], [tenth, ninth]) == [new]


async def test_currency_change_requires_rereview_and_preserves_prior_draft() -> None:
    """币种改后新底稿显示新代码并要求重新复核, 旧版与原始金额不改."""
    runner, store = await seeded()
    original = await store.get(EID, Workpaper, "WP-D-3")
    async with api_client(runner) as client:
        await client.post(f"{BASE}/D-3/signoff", json={"scope": "full"})
        response = await client.put(f"/api/v1/engagements/{EID}/currency", json={"currency": "NZD"})
        assert response.status_code == 200
    [changed] = [paper for paper in await refresh(runner.deps, EID) if paper.ref == "D-3"]
    assert changed.status is WorkpaperStatus.needs_rereview and changed.last_signoff is not None
    assert changed.conclusion_draft is not None and "NZD 9,900.00" in changed.conclusion_draft.zh
    assert await store.get(EID, Workpaper, "WP-D-3", 1) == original
    assert await refresh(runner.deps, EID) == []


@pytest.mark.external
@shared_loop
async def test_generic_workpaper_snapshots(walked: tuple[Walk, httpx.AsyncClient]) -> None:
    """共用走通核对普通规则与登记程序, 识别字段和检查结果进入快照, 不复制原始录入人员."""
    w, _ = walked
    papers = {ref: (await history(w, ref))[0] for ref in ("C-3", "A-4", "G-1", "B-2")}
    tables = {
        ref: next(s for s in paper.content.sections if s.id == "details").tables[0] for ref, paper in papers.items()
    }
    assert len(tables["C-3"].rows) == 2
    assert len(tables["B-2"].rows) == 3
    assert {row["status"] for row in tables["B-2"].rows} == {"✓"}
    assert len(tables["G-1"].rows) == 608
    assert {row["source_id"] for row in tables["A-4"].rows} >= {"090", "091", "840"}
    for paper in papers.values():
        snapshot = paper.content.model_dump_json()
        assert "{{" not in snapshot
    for data in tables.values():
        assert all(not ({"created_by", "approved_by", "posted_by"} & row.keys()) for row in data.rows)
