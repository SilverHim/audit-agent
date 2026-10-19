"""智能体协作 (功能 collab) 下的 Numbat 全流程 (真实 Agent 与 mock 输出): 结果与基线相同, 只多记下 A7 命中的触发规则.

全流程较慢, 同一次运行的结果供多条断言共用. 争议仲裁见 test_dispute.py, 触发规则的单项见 test_review_agent.py.
"""

import pytest

from backend.contracts.models import CriticTrigger, CriticVerdict, Judgment, TrailKind
from backend.services.features import BASELINE
from tests.fakes import ENGAGEMENT
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
