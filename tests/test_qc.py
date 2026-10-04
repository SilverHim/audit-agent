"""R23 A9 执行 FIN-04: 按状态中的未更正错报编汇总表 SCHEDULE 与候选意见 (FIN-04.yaml, 合成数据集设计第 7 节第 14 行)."""

from backend.agents.base import RunContext
from backend.agents.qc import QCAgent
from backend.contracts.models import (
    Assertion,
    Direction,
    Misstatement,
    MisstatementType,
    OpinionType,
    Phase,
    RunStatus,
    UncorrectedItem,
    UncorrectedSchedule,
)
from backend.contracts.state import StatePatch, initial_state
from tests.fakes import ENGAGEMENT, FakeRegistry, FakeTrail, make_deps, make_procedure, make_run

MATERIALITY, TRIVIAL = 12000000, 600000  # 合成数据集设计第 7 节的整体重要性与明显微小门槛


def misstatement(n: int, amount: int, direction: Direction = Direction.overstated) -> Misstatement:
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
    )


async def run_fin04(
    misstatements: list[Misstatement], previous: UncorrectedSchedule | None = None, materiality: int = MATERIALITY
) -> StatePatch:
    """以给定错报 (与已有汇总表) 执行一次 FIN-04, 参数快照为重要性与明显微小门槛."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    procedure = make_procedure("FIN-04", Phase.P3_Completion)
    state = initial_state(ENGAGEMENT, [procedure], 1)
    state.update(misstatements=misstatements, uncorrected_schedule=previous)  # type: ignore[typeddict-item]
    run = make_run("FIN-04", params={"materiality_cents": materiality, "clearly_trivial_cents": TRIVIAL})
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
    """已有汇总表 (FIN-01 驳回后重做) 时出下一版."""
    first = (await run_fin04([misstatement(1, 14200000)]))["uncorrected_schedule"]

    second = (await run_fin04([misstatement(1, 14200000)], previous=first))["uncorrected_schedule"]

    assert (second.id, second.version) == ("SCHEDULE", 2)
