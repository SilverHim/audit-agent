"""A9 执行 FIN-04: 按状态中当前结果的错报编汇总表 SCHEDULE 与候选意见 (FIN-04.yaml). R23 为原口径 (错报都未更正,
合成数据集设计第 7 节第 14 行); R166 按错报状态累计 (规格书 12.3 子状态机注, Xero 设计稿 v2.1 第 5.6 节 FIN-04 行;
重要性 32431, 明显微小 1622). 错报都是测试里合成的; 功能 misstatement 关闭时新建错报为 uncorrected, 这里显式写出."""

from datetime import UTC, datetime
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.qc import QCAgent
from backend.contracts.models import (
    Assertion,
    Decision,
    Direction,
    Misstatement,
    MisstatementStatus,
    MisstatementType,
    OpinionType,
    Phase,
    ProcedureStatus,
    RunStatus,
    TaskKind,
    UncorrectedItem,
    UncorrectedSchedule,
)
from backend.contracts.state import StatePatch, initial_state
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import ENGAGEMENT, FakeRegistry, FakeTrail, make_deps, make_procedure, make_run

MATERIALITY, TRIVIAL = 12000000, 600000  # 合成数据集设计第 7 节的整体重要性与明显微小门槛
XERO_MATERIALITY, XERO_TRIVIAL = 32431, 1622  # Xero 演示的整体重要性与明显微小门槛 (收入 1%, 5%)
NOW = datetime(2026, 11, 2, tzinfo=UTC)


def misstatement(n: int | str, amount: int, direction: Direction = Direction.overstated, **fields: Any) -> Misstatement:
    """一条错报 M-D-{n}: 缺省为未更正 (功能 misstatement 关闭时的旧口径); fields 另给状态及其字段."""
    return Misstatement(
        id=f"M-D-{n}",
        judgment_id=f"J-{n}",
        exception_id=None,
        review_record_id=f"D-{n}",
        type=MisstatementType.factual,
        account="Revenue",
        assertion=Assertion.Cutoff,
        amount_cents=amount,
        direction=direction,
        **{"status": MisstatementStatus.uncorrected, **fields},
    )


def corrected(n: str, amount: int, direction: Direction = Direction.overstated) -> Misstatement:
    """验证通过而全额更正的错报 (剩余额 0)."""
    return misstatement(
        n,
        amount,
        direction,
        status=MisstatementStatus.corrected,
        origin="current_period",
        correction_notice_id="CN-1",
        corrected_cents=amount,
        residual_cents=0,
        verified_by_judgment_id=f"J-FIN-09-1-M-D-{n}",
    )


def partially_corrected(n: str, amount: int, residual: int) -> Misstatement:
    """验证通过而剩余 residual 的错报."""
    return misstatement(
        n,
        amount,
        status=MisstatementStatus.partially_corrected,
        origin="current_period",
        correction_notice_id="CN-1",
        corrected_cents=amount - residual,
        residual_cents=residual,
        verified_by_judgment_id=f"J-FIN-09-1-M-D-{n}",
    )


def refused(n: str, amount: int, direction: Direction = Direction.overstated, **fields: Any) -> Misstatement:
    """管理层拒绝更正的错报."""
    refusal = {
        "origin": "current_period",
        "uncorrected_basis": "refused",
        "communicated_at": NOW,
        "communicated_to": "the director",
        "responded_at": NOW,
        "management_response": "We will not adjust the accounts for this item.",
    }
    return misstatement(n, amount, direction, **{**refusal, **fields})


async def run_fin04(
    misstatements: list[Misstatement],
    previous: UncorrectedSchedule | None = None,
    materiality: int = MATERIALITY,
    trivial: int = TRIVIAL,
) -> StatePatch:
    """以给定错报 (与已有汇总表) 执行一次 FIN-04, 参数快照为重要性与明显微小门槛."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    procedure = make_procedure("FIN-04", Phase.P3_Completion)
    state = initial_state(ENGAGEMENT, [procedure], 1)
    state.update(misstatements=misstatements, uncorrected_schedule=previous)  # type: ignore[typeddict-item]
    run = make_run("FIN-04", params={"materiality_cents": materiality, "clearly_trivial_cents": trivial})
    ctx = RunContext(
        engagement=ENGAGEMENT, procedure=procedure, template=deps.library.load("FIN-04"), run=run, state=state
    )
    return await QCAgent(deps).run(ctx)


async def test_numbat_cutoff_misstatement_gives_qualified() -> None:
    """验收 1: 一条 14200000 高估的错报, 重要性 12000000: SCHEDULE v1 一行, 合计 14200000, 候选保留意见."""
    m = misstatement(1, 14200000)

    patch = await run_fin04([m])

    assert patch["uncorrected_schedule"] == UncorrectedSchedule(
        id="SCHEDULE",
        items=[UncorrectedItem(misstatement_id=m.id, amount_cents=14200000, direction=Direction.overstated)],
        excluded_trivial_ids=[],
        total_cents=14200000,
        materiality_cents=MATERIALITY,
        candidate_opinion=OpinionType.qualified,
    )
    [run] = patch["procedure_runs"]
    assert (run.id, run.status) == ("RUN-FIN-04-1", RunStatus.succeeded) and run.ended_at is not None


async def test_trivial_threshold_and_no_netting() -> None:
    """验收 2: 等于明显微小门槛的不计入 (只列 ID), 高出 1 分的计入; 高估与低估不抵销, 合计为两者之和."""
    at, above = misstatement(1, TRIVIAL), misstatement(2, TRIVIAL + 1)
    over, under = misstatement(3, 3000000), misstatement(4, 2000000, Direction.understated)

    schedule = (await run_fin04([at, above, over, under]))["uncorrected_schedule"]

    assert schedule.excluded_trivial_ids == [at.id]
    assert [i.misstatement_id for i in schedule.items] == [above.id, over.id, under.id]
    assert schedule.total_cents == TRIVIAL + 1 + 3000000 + 2000000


async def test_opinion_boundary_and_no_misstatements() -> None:
    """验收 3: 合计等于整体重要性为保留, 少 1 分为无保留; 没有错报时无 items, 合计 0, 无保留."""
    assert (await run_fin04([misstatement(1, MATERIALITY)]))["uncorrected_schedule"].candidate_opinion is (
        OpinionType.qualified
    )
    assert (await run_fin04([misstatement(1, MATERIALITY - 1)]))["uncorrected_schedule"].candidate_opinion is (
        OpinionType.unqualified
    )
    empty = (await run_fin04([]))["uncorrected_schedule"]
    assert (empty.items, empty.total_cents, empty.candidate_opinion) == ([], 0, OpinionType.unqualified)


async def test_next_version_when_schedule_exists() -> None:
    """已有汇总表 (FIN-01 驳回后重做) 时出下一版; R166 验收 8: 功能关闭 (错报都未更正) 时两版的 corrected_ids 都为空."""
    first = (await run_fin04([misstatement(1, 14200000)]))["uncorrected_schedule"]

    second = (await run_fin04([misstatement(1, 14200000)], previous=first))["uncorrected_schedule"]

    assert (second.id, second.version) == ("SCHEDULE", 2)
    assert first.corrected_ids == second.corrected_ids == []


# ── R166 按错报状态累计 (Xero 演示的五条错报, 重要性 32431, 明显微小 1622) ──

XJ499, CSH091 = "XJ499", "CSH02-091"  # 办公设备 (高估 750.00) 与应计利息 (低估 48.53)
PAYROLL = [("F03", 426120), ("F04", 201000), ("F05", 268736)]  # 第 2 轮的三条薪酬错报


async def xero_fin04(
    misstatements: list[Misstatement], previous: UncorrectedSchedule | None = None
) -> UncorrectedSchedule:
    schedule = (await run_fin04(misstatements, previous, XERO_MATERIALITY, XERO_TRIVIAL))["uncorrected_schedule"]
    assert schedule is not None
    return schedule


async def test_corrected_misstatements_are_listed_not_accumulated() -> None:
    """验收 1, 2: 第 1 轮两条都已更正: SCHEDULE v1 没有 items, corrected_ids 为两条, 合计 0, 无保留; 第 2 轮再加三条
    已更正的 (状态已有 v1): v2, corrected_ids 五条, 合计 0, 无保留."""
    first_round = [corrected(XJ499, 75000), corrected(CSH091, 4853, Direction.understated)]
    v1 = await xero_fin04(first_round)
    assert (v1.version, v1.items, v1.excluded_trivial_ids, set(v1.corrected_ids)) == (
        1,
        [],
        [],
        {"M-D-XJ499", "M-D-CSH02-091"},
    )
    assert (v1.total_cents, v1.materiality_cents, v1.candidate_opinion) == (0, 32431, OpinionType.unqualified)

    v2 = await xero_fin04([*first_round, *(corrected(n, amount) for n, amount in PAYROLL)], previous=v1)
    assert (v2.version, len(v2.corrected_ids), v2.items, v2.total_cents) == (2, 5, [], 0)
    assert v2.candidate_opinion is OpinionType.unqualified


@pytest.mark.parametrize("origin", ["current_period", "prior_period_uncorrected"])
async def test_refused_misstatement_is_accumulated_with_its_origin(origin: str) -> None:
    """验收 3: 应计利息一条被拒绝更正, 其余已更正: items 只有它 (金额, 方向, origin 取错报的分类), 合计 4853, 无保留."""
    schedule = await xero_fin04([corrected(XJ499, 75000), refused(CSH091, 4853, Direction.understated, origin=origin)])
    assert schedule.items == [
        UncorrectedItem(
            misstatement_id="M-D-CSH02-091", amount_cents=4853, direction=Direction.understated, origin=origin
        )
    ]
    assert (schedule.total_cents, schedule.corrected_ids, schedule.candidate_opinion) == (
        4853,
        ["M-D-XJ499"],
        OpinionType.unqualified,
    )


@pytest.mark.parametrize(
    ("residual", "accumulated"),
    [(7500, True), (XERO_TRIVIAL, False), (XERO_TRIVIAL + 1, True)],
    ids=["residual-7500", "at-trivial", "above-trivial"],
)
async def test_partially_corrected_accumulates_the_residual(residual: int, accumulated: bool) -> None:
    """验收 4: 部分更正的按剩余额累计, 不按错报金额: 剩余 7500 进 items (高估, 本期); 剩余额等于明显微小门槛的只列
    excluded_trivial_ids, 高出 1 分的进 items."""
    schedule = await xero_fin04([partially_corrected(XJ499, 75000, residual)])
    item = UncorrectedItem(misstatement_id="M-D-XJ499", amount_cents=residual, direction=Direction.overstated)
    assert (schedule.items, schedule.excluded_trivial_ids) == (([item], []) if accumulated else ([], ["M-D-XJ499"]))
    assert schedule.total_cents == (residual if accumulated else 0)


@pytest.mark.parametrize(
    ("amount", "opinion"),
    [
        (75000, OpinionType.qualified),
        (XERO_MATERIALITY - 1, OpinionType.unqualified),
        (XERO_MATERIALITY, OpinionType.qualified),
    ],
)
async def test_uncorrected_amount_against_materiality(amount: int, opinion: OpinionType) -> None:
    """验收 5: 办公设备一条未更正 75000, 合计不低于 32431 为保留; 32430 为无保留, 32431 为保留."""
    schedule = await xero_fin04([refused(XJ499, amount)])
    assert (schedule.total_cents, schedule.candidate_opinion) == (amount, opinion)


UNSCHEDULABLE = {  # 不能累计的错报: 未结清的三种与本版不写的两种, 状态与该状态须有的字段
    "identified": {"status": MisstatementStatus.identified},
    "communicated": {"status": MisstatementStatus.communicated, "communicated_at": NOW},
    "verifying": {"status": MisstatementStatus.verifying, "communicated_at": NOW, "correction_notice_id": "CN-1"},
    "evaluated": {"status": MisstatementStatus.evaluated},
    "closed": {"status": MisstatementStatus.closed},
}


@pytest.mark.parametrize("status", UNSCHEDULABLE)
async def test_unschedulable_misstatement_fails_fin04_for_a_human(status: str) -> None:
    """验收 6 (兜底, 正常拓扑下不会出现): 还有未结清 (identified, communicated, verifying) 或本版不写 (evaluated,
    closed) 的错报时 A9 抛错, 不出汇总表; A1 记 RUN-FIN-04-1 failed (理由写明哪条错报与其状态), 建 failed 任务 (只能
    rerun), FIN-04 等人 (awaiting_human)."""
    from tests.test_assemble import after_pln_approve

    nodes, _, engagements, state = after_pln_approve()
    state["misstatements"] = [corrected(XJ499, 75000), misstatement(CSH091, 4853, **UNSCHEDULABLE[status])]
    engagement = await engagements.get(ENGAGEMENT.id)
    assert engagement is not None

    patch, pack = await nodes.attempt(state, engagement, "FIN-04")

    [run] = patch["procedure_runs"]
    assert (run.id, run.status) == ("RUN-FIN-04-1", RunStatus.failed)
    assert run.failure_reason is not None and f"M-D-CSH02-091 ({status})" in run.failure_reason
    [task] = patch["review_tasks"]
    assert (task.id, task.kind, nodes.deps.queue.allowed(task)) == (
        "T-RUN-FIN-04-1-v1",
        TaskKind.failed,
        {Decision.rerun},
    )
    assert "uncorrected_schedule" not in patch
    assert {p.id: p.status for p in pack}["FIN-04"] is ProcedureStatus.awaiting_human


def test_fin04_depends_on_classification_and_verification_only_when_misstatement_is_on() -> None:
    """验收 7 (FIN-09 由 R164 加入): 基线下 FIN-03, FIN-09 未启用, FIN-04 的有效依赖为 [] (程序库丢弃对已关闭功能的
    程序的依赖), 构造不报错; 功能 misstatement 打开时为 [FIN-03, FIN-09]."""
    from backend.core.config import get_settings

    root = get_settings().KNOWLEDGE_ROOT
    base = ProcedureLibrary(root)
    assert base.load("FIN-04").depends_on == []
    assert [t.id for t in base.closure(["FIN-04"])] == ["FIN-04"]
    on = ProcedureLibrary(root, frozenset({"critic", "misstatement"}))
    assert on.load("FIN-04").depends_on == ["FIN-03", "FIN-09"]
    assert [t.id for t in on.closure(["FIN-04"])] == ["FIN-03", "FIN-09", "FIN-04"]


async def test_string_status_from_checkpoint_fallback_fails_whole_run() -> None:
    """审核 R166-05: 检查点读回校验不过时错报退回 model_construct, 状态成了字符串. 能否累计与编表的分支都按枚举成员判断,
    这时整次失败, 不把已更正的按全额累计, 也不把部分更正的按错报金额累计."""
    settled = [corrected(XJ499, 75000), partially_corrected(CSH091, 75000, 7500)]
    as_strings = [Misstatement.model_construct(**{**m.model_dump(), "status": m.status.value}) for m in settled]
    with pytest.raises(ValueError, match=r"M-D-XJ499 \(corrected\), M-D-CSH02-091 \(partially_corrected\)"):
        await xero_fin04(as_strings)
