"""智能体协作 (功能 collab) 下的 Numbat 全流程 (真实 Agent 与 mock 输出): 主流程结果与基线相同, 只多记下 A7 命中的
触发规则 (R149); 争议变体 (复核模型取 llm_variants/dispute) 中 CSH-02 为争议中, 争议任务在待办里单列在前 (R151).

全流程较慢, 同一次运行的结果供多条断言共用. 争议仲裁的基线行为见 test_dispute.py, 触发规则的单项见
test_review_agent.py.
"""

from dataclasses import replace
from typing import Any

import pytest

from backend.adapters.llm import MockLLMClient
from backend.agents.concierge_chat.tools import NoArgs, ToolEnv, list_open_items
from backend.contracts.models import (
    CriticTrigger,
    CriticVerdict,
    DisputeResolution,
    Judgment,
    Procedure,
    ProcedureStatus,
    TrailKind,
)
from backend.orchestration.query import GraphStateQuery
from backend.services.features import BASELINE
from tests.fakes import ENGAGEMENT, wait_idle
from tests.test_decisions import Started
from tests.test_dispute import TASK, VARIANTS, resolve
from tests.test_e2e_mock import assert_results, finish_demo, start_demo, trail_of

EID = ENGAGEMENT.id
COLLAB = BASELINE | {"collab"}


@pytest.mark.parametrize(("features", "hit"), [(COLLAB, CriticTrigger.main), (BASELINE, None)], ids=["on", "off"])
async def test_main_flow_keeps_results_and_records_triggers(
    features: frozenset[str], hit: CriticTrigger | None
) -> None:
    """R149 验收 4, 5: collab 打开与关闭时 Numbat 全流程的最终结果相同 (assert_results); J-CSH-02-1-BA-01 (CSH-02 L3,
    建议关闭) 置信度 0.65, critic_trigger 打开时为 main, 关闭时为空; REV-03, REV-02 的 5 条判断 (L2) skipped,
    critic_trigger 为空; GEN-09 留痕都是 2 行 (BA-01-r1, BA-01-r2)."""
    s = await start_demo(features=features)
    await finish_demo(s)
    await assert_results(s)
    judgments = {j.id: j for j in await s.output_store.latest(EID, Judgment)}
    ba01 = judgments.pop("J-CSH-02-1-BA-01")
    assert (ba01.critic_verdict, ba01.critic_trigger, ba01.confidence) == (
        CriticVerdict.consistent,
        hit,
        0.65,
    )
    assert sorted(judgments) == [
        "J-REV-02-1-SI-2026-0612",
        "J-REV-02-1-SI-2026-0618",
        "J-REV-03-1-CON-001",
        "J-REV-03-1-CON-002",
        "J-REV-03-1-CON-003",
    ]
    assert {(j.critic_verdict, j.critic_trigger) for j in judgments.values()} == {(CriticVerdict.skipped, None)}
    calls = [e for e in trail_of(s).entries if e.kind is TrailKind.llm_call and e.procedure_id == "GEN-09"]
    assert [e.payload["fixture_key"] for e in calls] == ["BA-01-r1", "BA-01-r2"]
    # R151 验收 2: 主流程 (BA-01 判一致) 没有争议, 不出现争议中
    assert all(p.status is not ProcedureStatus.in_dispute for p in s.output_store.rows if isinstance(p, Procedure))
    assert not [e for e in trail_of(s).entries if e.kind is TrailKind.transition and e.payload["to"] == "in_dispute"]


# ── R151: 争议中与争议单列 ──

REV03 = ["T-J-REV-03-1-CON-001-v1", "T-J-REV-03-1-CON-002-v1", "T-J-REV-03-1-CON-003-v1"]


def transitions(s: Started, oid: str | None = None) -> list[tuple[str, str]]:
    """程序状态的 transition 留痕 (from, to); oid 为空时取全部程序的."""
    return [
        (e.payload["from"], e.payload["to"])
        for e in trail_of(s).entries
        if e.kind is TrailKind.transition and e.payload["object"] == "procedure" and oid in (None, e.payload["id"])
    ]


async def disputed(features: frozenset[str]) -> tuple[Started, GraphStateQuery]:
    """Numbat 争议变体跑到首次等复核: BA-01 判为不一致, 建争议任务."""
    s = await start_demo(review_llm=MockLLMClient(VARIANTS / "dispute"), features=features)
    await wait_idle(s.runner, EID)
    return s, GraphStateQuery(s.runner.app, s.runner.deps, s.runner)


async def open_review_tasks(s: Started, query: GraphStateQuery) -> list[Any]:
    found = await list_open_items(ToolEnv(EID, s.runner.deps, query, 6000), NoArgs())
    return [item["task_id"] for item in found.data["items"] if item["type"] == "review_task"]


async def test_dispute_makes_program_in_dispute_and_listed_first() -> None:
    """R151 验收 2, 3, 5, 7: collab 打开时 CSH-02 所在那一层 (第 3 层: CSH-04 → DOC-06 → CSH-02) 汇合后为争议中,
    transition 一行 planned → in_dispute; 总览中 CSH-02 为 in_dispute, 待决任务 1; 对话待办的复核任务依次为争议任务,
    再按创建时间为 REV-03 的三条; a6_upheld 后 CSH-02 completed, transition in_dispute → completed.
    collab 关闭时照现状: awaiting_human, 待办按创建时间 (REV-03 三条在前); 两边 transition 总行数相同."""
    on, query = await disputed(COLLAB)
    assert (await on.output_store.get(EID, Procedure, "CSH-02")).status is ProcedureStatus.in_dispute
    assert transitions(on, "CSH-02") == [("planned", "in_dispute")]
    overview = await query.overview(EID)
    [summary] = [p for p in overview.procedures if p.id == "CSH-02"]
    assert (summary.status, summary.pending_tasks, overview.features) == (
        ProcedureStatus.in_dispute,
        1,
        ["collab", "critic"],
    )
    assert await open_review_tasks(on, query) == [TASK, *REV03]
    # 总览与待办按项目冻结的功能集, 不按当前进程的 (如进程换成基线功能集, 项目暂停推进时照样显示)
    baseline = replace(on.runner.deps, features=BASELINE)
    elsewhere = GraphStateQuery(on.runner.app, baseline, on.runner)
    assert (await elsewhere.overview(EID)).features == ["collab", "critic"]
    found = await list_open_items(ToolEnv(EID, baseline, elsewhere, 6000), NoArgs())
    assert [i["task_id"] for i in found.data["items"] if i["type"] == "review_task"] == [TASK, *REV03]
    await on.submit.submit(EID, resolve(DisputeResolution.a6_upheld))
    await wait_idle(on.runner, EID)
    assert on.runner.run_error(EID) is None
    assert (await on.output_store.get(EID, Procedure, "CSH-02")).status is ProcedureStatus.completed
    assert transitions(on, "CSH-02") == [("planned", "in_dispute"), ("in_dispute", "completed")]

    off, query = await disputed(BASELINE)
    assert (await off.output_store.get(EID, Procedure, "CSH-02")).status is ProcedureStatus.awaiting_human
    assert (await query.overview(EID)).features == ["critic"]
    assert await open_review_tasks(off, query) == [*REV03, TASK]
    await off.submit.submit(EID, resolve(DisputeResolution.a6_upheld))
    await wait_idle(off.runner, EID)
    assert transitions(off, "CSH-02") == [("planned", "awaiting_human"), ("awaiting_human", "completed")]
    assert len(transitions(on)) == len(transitions(off))
