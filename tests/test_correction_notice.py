"""需求 R162: 等待客户更正与更正通知 (后端).

Xero 演示上沿用 test_misstatement_communication 的装配: 检查点以 fan_in 身份写入 P2 已结清的状态, 两条错报 M-D-XJ499,
M-D-CSH02-091 已是第 1 轮登记之后的 v3 communicated; 分诊与沟通都空执行, 进 await_correction 在节点内等更正通知.
更正分录取 XeroFakeDatasetLoader 预置的补造 adjustments (只记调用, 不写财务存储; 写总账新版本与同一事务见
test_correction_notice_db). 主图在 verify_correction 之前停下; 部分通知的用例从停点再继续一次, 回到等待更正.
"""

from typing import Any

import pytest
import pytest_asyncio

from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AgentId,
    CorrectionNotice,
    Decision,
    Misstatement,
    MisstatementStatus,
    TaskKind,
    TrailEntry,
    TrailKind,
)
from backend.orchestration.decisions import check_notice_fields
from backend.orchestration.nodes import after_notice, awaiting_correction
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.recovery import catch_up_correction
from backend.orchestration.runner import GraphRunner, waiting_payloads
from tests.fakes import (
    FakeDatasetLoader,
    FakeTrail,
    entries,
    finished,
    latest,
    make_run,
    record_writers,
    submit_and_wait,
    wait_idle,
)
from tests.misstatement_fakes import CSH02, POSTED_AT, SUPERSEDED, XJ499, fin03_state, notice, round1_communicated
from tests.test_decisions import Started, assert_no_effect, assert_rejected, shared_loop, started
from tests.test_misstatement_communication import CONFIG, EID, at_communicate, round1
from tests.test_review_decisions import record

pytestmark = pytest.mark.external  # Xero 快照与补造

DATASET = "xero/20261017-q1"
STOP_REASON = "Management did not post the adjustment before the report deadline."


def stop(oid: str = "D-STOP", **fields: Any) -> CorrectionNotice:
    """停止等待 CSH02 的通知 (带理由); fields 覆盖任意字段."""
    base = {"kind": "stop_waiting", "misstatement_ids": [CSH02], "adjustment_entry_nos": [], "reason": STOP_REASON}
    return notice(oid, **(base | fields))


async def awaiting() -> Started:
    """停在 await_correction 等更正通知 (见模块 docstring); 闸门之外另在 verify_correction 之前停下."""
    return await at_communicate(round1_communicated(), stops=("verify_correction",))


def loader(s: Started) -> FakeDatasetLoader:
    datasets = s.runner.deps.datasets
    assert isinstance(datasets, FakeDatasetLoader)
    return datasets


def correction_payload(*ids: str) -> dict[str, Any]:
    return {"awaiting": "correction", "misstatement_ids": list(ids), "records": []}


async def test_waits_for_correction_notice() -> None:
    """验收 1: 两条 v3 communicated → 停在 await_correction, 载荷列这两条 (按 ID 排序); 总览 awaiting_correction 真,
    awaiting_correction_ids 为这两条, awaiting_review 假."""
    s = await awaiting()

    snapshot = await s.runner.app.aget_state(CONFIG)
    assert [t.name for t in snapshot.tasks if t.interrupts] == ["await_correction"]
    assert waiting_payloads(snapshot) == [correction_payload(CSH02, XJ499)]
    overview = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).overview(EID)
    assert (overview.awaiting_correction, overview.awaiting_correction_ids, overview.awaiting_review) == (
        True,
        [CSH02, XJ499],
        False,
    )


async def test_adjustments_posted_loads_entries_and_moves_to_verifying(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 2: 提交 D-CN1 (两条, AJE-X01 与 AJE-X05): 写通知 (写入者为提交人) 与 1 行 decision 留痕 (FIN-08, 防重复键
    D-CN1); 恢复后节点按凭证号调 load_adjustments 一次, 写入人为提交人, 调用时在写错报的事务内; 同一提交写出两条
    v4 verifying (correction_notice_id D-CN1, 写入者 A1); 状态 correction_notices 为 [D-CN1]; 下一节点
    verify_correction."""
    s = await awaiting()
    writers = record_writers(monkeypatch, s.output_store)
    datasets, inside = loader(s), []
    load = datasets.load_adjustments

    async def checking(*args: Any, **kwargs: Any) -> Any:
        inside.append(s.output_store.active_txn() is not None)
        return await load(*args, **kwargs)

    monkeypatch.setattr(datasets, "load_adjustments", checking)
    await submit_and_wait(s, notice())

    assert writers[("D-CN1", 1)] == "U-1"
    assert [(e.idempotency_key, e.procedure_id, e.actor) for e in entries(s, TrailKind.decision)] == [
        ("D-CN1", "FIN-08", "U-1")
    ]
    assert (loader(s).adjustment_loads, loader(s).adjustment_actors, inside) == (
        [(EID, DATASET, ["AJE-X01", "AJE-X05"])],
        ["U-1"],
        [True],
    )
    [commit] = [c for c in s.output_store.commits if any(getattr(o, "id", None) == XJ499 and o.version == 4 for o in c)]
    assert sorted((o.id, o.version, o.status, o.correction_notice_id) for o in commit) == [
        (CSH02, 4, MisstatementStatus.verifying, "D-CN1"),
        (XJ499, 4, MisstatementStatus.verifying, "D-CN1"),
    ]
    assert writers[(XJ499, 4)] == AgentId.A1
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert [n.id for n in snapshot.values["correction_notices"]] == ["D-CN1"]
    assert snapshot.next == ("verify_correction",)


async def test_partial_notice_waits_again_for_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 3: 只列 M-D-XJ499 与 AJE-X01 → XJ499 v4 verifying, CSH02 仍为 v3 communicated; 经 verify_correction (程序包
    没有 FIN-09, 空操作; 停点之后再继续) 后 after_verify 回到 await_correction, 载荷只列 CSH02. 其余一条的通知 D-CN2
    写进 Store 后检查点未写: 重启补做只交回 D-CN2 (D-CN1 已在状态中), CSH02 v4 verifying, 停在 verify_correction 前."""
    s = await awaiting()
    await submit_and_wait(s, notice(misstatement_ids=[XJ499], adjustment_entry_nos=["AJE-X01"]))
    assert (latest(s, Misstatement, XJ499).status, latest(s, Misstatement, CSH02).version) == (
        MisstatementStatus.verifying,
        3,
    )
    await s.runner.app.ainvoke(None, CONFIG)
    assert waiting_payloads(await s.runner.app.aget_state(CONFIG)) == [correction_payload(CSH02)]

    with monkeypatch.context() as m:
        m.setattr(s.runner, "launch", lambda eid, graph_input: s.runner.release(eid))
        await s.submit.submit(EID, notice("D-CN2", misstatement_ids=[CSH02], adjustment_entry_nos=["AJE-X05"]))
    restarted = GraphRunner(s.runner.app, s.runner.deps)
    await restarted.recover()
    await wait_idle(restarted, EID)

    assert restarted.run_error(EID) is None
    written = latest(s, Misstatement, CSH02)
    assert (written.version, written.status, written.correction_notice_id) == (4, MisstatementStatus.verifying, "D-CN2")
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert [n.id for n in snapshot.values["correction_notices"]] == ["D-CN1", "D-CN2"]
    assert snapshot.next == ("verify_correction",)


@pytest.mark.parametrize("reason", [STOP_REASON, None, ""], ids=["given", "omitted", "empty"])
async def test_stop_waiting_records_uncorrected(reason: str | None) -> None:
    """验收 4: D-STOP 停止等待 CSH02 → v4 uncorrected (stopped_waiting, correction_notice_id D-STOP); 不载入更正分录.
    需求 R358: 理由选填, 不填 (缺省或空串) 也能提交; 通知照原样存, decision 留痕照记理由 (空的记空)."""
    s = await awaiting()
    decided = stop(reason=reason)
    await submit_and_wait(s, decided)

    assert await s.output_store.get(EID, CorrectionNotice, "D-STOP") == decided
    [entry] = [e for e in entries(s, TrailKind.decision) if e.idempotency_key == "D-STOP"]
    assert entry.payload["reason"] == reason

    written = latest(s, Misstatement, CSH02)
    assert (written.version, written.status, written.uncorrected_basis, written.correction_notice_id) == (
        4,
        MisstatementStatus.uncorrected,
        "stopped_waiting",
        "D-STOP",
    )
    assert latest(s, Misstatement, XJ499).version == 3
    assert loader(s).adjustment_loads == []


async def test_empty_entry_nos_load_all() -> None:
    """验收 8: 凭证号为空时按 DA-13 载入全部未载入的更正分录 (entry_nos 传 None)."""
    s = await awaiting()
    await submit_and_wait(s, notice(adjustment_entry_nos=[]))

    assert loader(s).adjustment_loads == [(EID, DATASET, None)]


REJECTED = [  # (说明, 通知, 错误码, 消息含)
    ("empty", notice(misstatement_ids=[]), "INVALID_VALUE", "不重复的错报"),
    ("twice", notice(misstatement_ids=[XJ499, XJ499]), "INVALID_VALUE", "不重复的错报"),
    ("not_waiting", notice(misstatement_ids=[XJ499, "M-D-OTHER"]), "INVALID_VALUE", "M-D-OTHER 不在等更正"),
    ("unknown_entry", notice(adjustment_entry_nos=["AJE-X01", "AJE-X09"]), "INVALID_VALUE", "更正分录中没有 AJE-X09"),
    ("entry_on_stop", stop(adjustment_entry_nos=["AJE-X05"]), "FIELD_NOT_ALLOWED", "adjustment_entry_nos"),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def waiting_once() -> Started:
    """停在 await_correction 的一次运行, 由只提交被拒通知的用例共用 (每次核对存储与图都没动)."""
    return await awaiting()


@shared_loop
@pytest.mark.parametrize(("decided", "code", "detail"), [r[1:] for r in REJECTED], ids=[r[0] for r in REJECTED])
async def test_notice_checked_before_writing(
    waiting_once: Started, decided: CorrectionNotice, code: str, detail: str
) -> None:
    """验收 5: 校验不过时 Store, 留痕与检查点不变, 锁释放 (assert_no_effect). 带重新沟通标记的错报不进等待载荷
    (awaiting_correction), 由 "不在等更正的错报中" 这一条拒绝; 主图上停在等待更正时构造不出带标记的错报 (after_verify
    先去 communicate), 由载荷保证."""
    await assert_no_effect(waiting_once, lambda: waiting_once.submit.submit(EID, decided), code, detail)


async def test_dataset_without_adjustments_rejected() -> None:
    """验收 5: 数据集没有可载入的更正分录 (同 Numbat, 预置为 None) → INVALID_VALUE; 项目没有数据集时同样 (另一句消息,
    直接调校验); 停止等待照常收."""
    s = await awaiting()
    loader(s).adjustments = None
    await assert_no_effect(s, lambda: s.submit.submit(EID, notice()), "INVALID_VALUE", f"数据集 {DATASET} 没有可载入")
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None
    snapshot = await s.runner.app.aget_state(CONFIG)
    with pytest.raises(DecisionRejected, match="项目没有数据集"):
        await s.submit.check_correction_notice(engagement.model_copy(update={"dataset": None}), notice(), snapshot)
    await submit_and_wait(s, stop())


async def test_review_record_id_rejected_as_notice_id() -> None:
    """验收 5: 决定 ID 已被复核决定用过 (第 1 轮的登记 D-M1, 之后停在等待更正) → INVALID_VALUE."""
    s = await at_communicate(stops=("verify_correction",))
    await submit_and_wait(s, round1())
    await assert_no_effect(s, lambda: s.submit.submit(EID, notice("D-M1")), "INVALID_VALUE", "D-M1 已用过")


async def test_used_notice_id_rejected() -> None:
    """验收 5: 决定 ID 已被更正通知用过 → INVALID_VALUE (同一张 decisions 表, 共用 ID 空间)."""
    s = await awaiting()
    await submit_and_wait(s, notice(misstatement_ids=[XJ499], adjustment_entry_nos=["AJE-X01"]))
    await s.runner.app.ainvoke(None, CONFIG)  # 经 verify_correction 回到等待更正
    await assert_no_effect(s, lambda: s.submit.submit(EID, stop("D-CN1")), "INVALID_VALUE", "D-CN1 已用过")


@pytest.mark.parametrize(
    ("changes", "code"),
    [({"locked": True}, "ENGAGEMENT_LOCKED"), ({"version_snapshot": {"features": "critic"}}, "FEATURES_MISMATCH")],
    ids=["locked", "features"],
)
async def test_notice_rejected_on_engagement_state(changes: dict[str, Any], code: str) -> None:
    """验收 5: 项目已锁定 → ENGAGEMENT_LOCKED; 项目冻结的功能集与当前不同 → FEATURES_MISMATCH (校验顺序同其他决定)."""
    s = await awaiting()
    await s.engagement_store.update(EID, **changes)
    await assert_no_effect(s, lambda: s.submit.submit(EID, notice()), code)


async def test_notice_rejected_while_busy() -> None:
    """验收 5: 后台运行中 (项目锁已被取得) → ENGAGEMENT_BUSY."""
    s = await awaiting()
    s.runner.acquire(EID)
    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, notice())
    assert e.value.code == "ENGAGEMENT_BUSY"


async def test_notice_rejected_when_not_awaiting_correction() -> None:
    """验收 5, 9: 图不在 await_correction (停在 communicate 等 FIN-08 的登记) → NOT_AWAITING_CORRECTION, 先于字段
    校验 (带凭证号的停止等待也报它); 功能 misstatement 关闭 (基线, 停在 ACC-01 闸门) 时同样."""
    s = await at_communicate()
    await assert_no_effect(s, lambda: s.submit.submit(EID, notice()), "NOT_AWAITING_CORRECTION")
    entry_on_stop = stop(adjustment_entry_nos=["AJE-X05"])
    await assert_no_effect(s, lambda: s.submit.submit(EID, entry_on_stop), "NOT_AWAITING_CORRECTION")
    baseline = await started()
    await assert_no_effect(baseline, lambda: baseline.submit.submit(EID, notice()), "NOT_AWAITING_CORRECTION")


async def test_review_decision_rejected_while_awaiting_correction() -> None:
    """验收 6: 停在 await_correction 时提交复核决定 → NOT_AWAITING_REVIEW (等待更正不收复核决定)."""
    s = await awaiting()
    run = finished(make_run("FIN-03", 2))
    task = s.runner.deps.queue.make_task(TaskKind.failed, run, "FIN-03", run.id, POSTED_AT)
    async with s.output_store.transaction(EID):
        await s.output_store.put(EID, [task], actor=AgentId.A1)
    await assert_rejected(s, record(task.id, Decision.rerun, id="D-R"), "NOT_AWAITING_REVIEW", "不在等复核")


async def test_notice_recovered_after_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 7: D-CN1 写进 Store 后进程退出 (检查点未写, 决定留痕也没写成): 再提交任一决定报 RESUME_REQUIRED (列出
    D-CN1); 新 runner recover 补写留痕, 以 Command(resume=D-CN1) 交给 await_correction, 结果同验收 2; 再补做一次不改
    Store 与检查点."""
    s = await awaiting()
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    with monkeypatch.context() as m:
        m.setattr(s.runner, "launch", lambda eid, graph_input: s.runner.release(eid))
        record_entry = trail.record

        async def lose(entry: TrailEntry) -> str:
            if entry.idempotency_key == "D-CN1":
                raise OSError("进程退出")
            return await record_entry(entry)

        m.setattr(trail, "record", lose)
        with pytest.raises(OSError):
            await s.submit.submit(EID, notice())
    assert entries(s, TrailKind.decision) == []
    await assert_no_effect(s, lambda: s.submit.submit(EID, stop("D-CN2")), "RESUME_REQUIRED", "D-CN1")

    restarted = GraphRunner(s.runner.app, s.runner.deps)
    await restarted.recover()
    await wait_idle(restarted, EID)

    assert restarted.run_error(EID) is None
    assert [e.idempotency_key for e in entries(s, TrailKind.decision)] == ["D-CN1"]
    assert [latest(s, Misstatement, m).status for m in (XJ499, CSH02)] == [MisstatementStatus.verifying] * 2
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert snapshot.next == ("verify_correction",)
    rows, values = list(s.output_store.rows), snapshot.values
    await restarted.recover()
    await wait_idle(restarted, EID)
    assert list(s.output_store.rows) == rows
    assert (await s.runner.app.aget_state(CONFIG)).values == values


async def put_notices(s: Started, *notices: CorrectionNotice) -> None:
    """通知直接写进 Store (不经 DecisionSubmit, 检查点不知道)."""
    async with s.output_store.transaction(EID):
        await s.output_store.put(EID, list(notices), actor="U-1")


async def test_catch_up_refuses_unrecoverable_notices() -> None:
    """恢复补做的两个防御分支: Store 有检查点里没有的通知, 而图不在等待更正 (停在 communicate), 或缺的不止一份 →
    RuntimeError (由 recover 记入 run_error), 不交给节点."""
    s = await at_communicate()
    await put_notices(s, notice())
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None
    with pytest.raises(RuntimeError, match="D-CN1 未进检查点"):
        await catch_up_correction(s.runner.deps, engagement, await s.runner.app.aget_state(CONFIG))
    s = await awaiting()
    await put_notices(s, notice(), stop())
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None
    with pytest.raises(RuntimeError, match="D-CN1, D-STOP 未进检查点"):
        await catch_up_correction(s.runner.deps, engagement, await s.runner.app.aget_state(CONFIG))


# ── 纯函数 ──


@pytest.mark.parametrize(
    ("decided", "status", "basis"),
    [(notice(), MisstatementStatus.verifying, None), (stop(), MisstatementStatus.uncorrected, "stopped_waiting")],
    ids=["posted", "stop"],
)
def test_after_notice(decided: CorrectionNotice, status: MisstatementStatus, basis: str | None) -> None:
    """通知写出的错报新版本: 状态与依据按通知的种类, 记通知 ID, 版本 +1, 不须重新沟通."""
    communicated = round1_communicated()[1]
    written = after_notice(communicated, decided)
    expected = {"version": 4, "status": status, "uncorrected_basis": basis, "correction_notice_id": decided.id}
    assert written == communicated.model_copy(update=expected)


def test_awaiting_correction_lists_communicated_only() -> None:
    """等更正的错报: 当前结果中 communicated 且不须重新沟通的, 按 ID 排序 (来源执行被过期重跑取代的不算)."""
    xj499, csh02 = round1_communicated()
    flagged = csh02.model_copy(update={"needs_recommunication": True})
    assert awaiting_correction(fin03_state(misstatements=[xj499, csh02])) == [CSH02, XJ499]
    assert awaiting_correction(fin03_state(misstatements=[xj499, csh02], rerun_requests=[SUPERSEDED])) == [CSH02]
    assert awaiting_correction(fin03_state(misstatements=[xj499, flagged])) == [XJ499]
    verifying = xj499.model_copy(update={"status": MisstatementStatus.verifying, "correction_notice_id": "D-CN1"})
    assert awaiting_correction(fin03_state(misstatements=[verifying, flagged])) == []


@pytest.mark.parametrize("reason", [None, "", "  "], ids=["omitted", "empty", "blank"])
@pytest.mark.parametrize("built", [notice, stop], ids=["adjustments_posted", "stop_waiting"])
def test_notice_reason_optional(built: Any, reason: str | None) -> None:
    """两种通知的理由都选填 (需求 R358; 缺, 空串与空白都过字段校验); adjustments_posted 的凭证号可为空 (载入全部)."""
    check_notice_fields(built(reason=reason, adjustment_entry_nos=[]))
