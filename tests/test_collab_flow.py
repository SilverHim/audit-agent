"""需求 R155 接入: 打开智能体协作开关 (功能 collab, 其余新功能关闭) 的 mock 全流程, 只断言本组的增量.

- Numbat 主流程 (决定同 R55; 提交 D-CSH02 与 D-REV02-0612 之前, 复核人分别在决定时间前 95 秒与 40 秒呈递其任务):
  结果与留痕计数同 R55, 另有 2 行呈递留痕与两条复核记录的停留时长; A7 记下命中的触发规则 (R149).
- Numbat 争议变体 (复核模型取 llm_variants/dispute): CSH-02 经争议中到完成; 呈递争议任务后裁决 a6_upheld 并记系统外
  咨询, 之后同主流程.
- Xero 只开现有 17 条 (设计稿第 7.3 节末段): A7 都跳过 (REV-03, REV-02 为 L2, BA-091 未命中触发规则), 呈递 BA-091 的
  任务后认定错报, 记录带停留时长; 结果同 R119.
状态迁移的参照为会话共用的 R55 一次 (collab 关闭, conftest 的 demo_run, 只取留痕). 全部接入后的 GEN-09 口径 (验收 4)
由合并终验 X-10 断言, 本条的算法与依据见确认材料. Numbat 回归 (R55, R56, R57, R59) 照旧显式关闭 collab
(tests/fakes.NUMBAT_FEATURES). 呈递一律经 presenting (三个流程与连库变体共用).
"""

from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta

import pytest

from backend.adapters.llm import MockLLMClient
from backend.agents.review import ReviewAgent, internal_copy_only
from backend.contracts.models import (
    AuditException,
    CriticTrigger,
    CriticVerdict,
    DisputeResolution,
    ExceptionDisposition,
    Judgment,
    Misstatement,
    Procedure,
    ProcedureStatus,
    ReviewRecord,
    ReviewStatus,
    ReviewTask,
    StepKind,
    StepStatus,
    TrailEntry,
    TrailKind,
)
from backend.orchestration.runner import GraphRunner
from backend.services.features import BASELINE
from backend.services.presentation import PROCEDURE_ID as PRESENTATION
from backend.services.presentation import record_presentation
from backend.services.sampling import sample_point
from tests.fakes import ENGAGEMENT
from tests.test_decisions import Started
from tests.test_dispute import JID, TASK, VARIANTS, resolve
from tests.test_e2e_mock import (
    Demo,
    assert_fixtures_used_once,
    assert_results,
    assert_trail_counts,
    enabled_programs,
    finish_demo,
    start_demo,
    trail_of,
)
from tests.test_e2e_xero17 import (
    assert_materiality,
    assert_report,
    assert_trail,
    entries_of,
    run_xero17,
)
from tests.test_e2e_xero17 import assert_results as assert_xero17_results
from tests.test_flow import REVIEWS

EID = ENGAGEMENT.id
COLLAB = BASELINE | {"collab"}  # 本组开关打开, 其余新功能关闭
REV02_TASK = "T-J-REV-02-1-SI-2026-0612-v1"
SHOWN = {TASK: 95, REV02_TASK: 40}  # 任务 → 复核人在决定时间前多少秒呈递它
CONSULTATION = "Discussed the bank cut-off with the engagement quality reviewer; the receipt was banked on 30 June 2026"
L2_SKIP = "L2: 审计师逐条复核, 不经独立复核 (GEN-09 跳过规则)"
BA091_TASK = "T-J-CSH-02-1-BA-091-v1"


def reference(demo_run: Demo) -> list[TrailEntry]:
    """状态迁移的参照: 会话共用的 R55 一次 (collab 关闭) 的留痕."""
    return trail_of(demo_run.s).entries


def presenting(shown: Mapping[str, int]) -> Callable[[GraphRunner, str, ReviewRecord], Awaitable[None]]:
    """finish_demo, finish_xero17 的 before_review (连库变体的提交循环也用): 提交 shown 中任务的决定之前, 由复核人在
    决定时间前若干秒呈递该任务 (呈递接口写留痕用的同一函数, 时间注入)."""

    async def before_review(runner: GraphRunner, eid: str, record: ReviewRecord) -> None:
        seconds = shown.get(record.task_id)
        if seconds is None:
            return
        task = await runner.deps.output_store.get(eid, ReviewTask, record.task_id)
        assert task is not None
        shown_at = record.decided_at - timedelta(seconds=seconds)
        await record_presentation(runner.deps.trail, eid, task, record.reviewed_by, shown_at)

    return before_review


def transitions(entries: list[TrailEntry]) -> Counter[tuple[str, str, str]]:
    """transition 留痕的 (对象, 原状态, 新状态) 多重集合."""
    return Counter(
        (e.payload["id"], e.payload["from"], e.payload["to"]) for e in entries if e.kind is TrailKind.transition
    )


def presentations(entries: list[TrailEntry]) -> list[tuple[str | None, str, str]]:
    """呈递留痕的 (程序, 呈递人, 任务), 按写入顺序."""
    return [(e.procedure_id, e.actor, e.payload["task_id"]) for e in entries if e.kind is TrailKind.presentation]


async def dwell(s: Started) -> dict[str, tuple[int | None, str | None]]:
    """各复核记录的 (停留时长, 呈递留痕 ID)."""
    return {r.id: (r.dwell_seconds, r.presentation_id) for r in await s.output_store.latest(EID, ReviewRecord)}


def presentation_ids(entries: list[TrailEntry]) -> dict[str, str]:
    """任务 → 它的呈递留痕 ID."""
    return {e.payload["task_id"]: e.id or "" for e in entries if e.kind is TrailKind.presentation}


async def test_main_flow_records_presentations_and_dwell(demo_run: Demo) -> None:
    """验收 1: 例外, 判断, 任务, 函证, 错报 (1 条 14200000 overstated), 意见与 REPORT-2 同 R55 (assert_results);
    llm_call 15 行 (GEN-09 2 行), decision 11 行, external_call 4 行; 状态迁移同 R55; 新增 presentation 2 行 (GEN-11,
    U-1, 按提交顺序先 REV-02 后 CSH-02); D-CSH02 停留 95 秒, D-REV02-0612 40 秒, 各带那次呈递的留痕 ID, 其余 4 条为空;
    J-CSH-02-1-BA-01 (CSH-02 L3, 建议关闭) 置信度 0.65, critic_trigger main; REV-03, REV-02 的 5 条 (L2) skipped 且
    critic_trigger 为空; GEN-09 两轮读 BA-01-r1, -r2; 每份 fixture 读一次且一次通过. 没有争议中."""
    s = await start_demo(features=COLLAB)
    await finish_demo(s, before_review=presenting(SHOWN))

    await assert_results(s)
    entries = trail_of(s).entries
    assert_trail_counts(entries)
    assert transitions(entries) == transitions(reference(demo_run))
    assert presentations(entries) == [(PRESENTATION, "U-1", REV02_TASK), (PRESENTATION, "U-1", TASK)]
    shown = presentation_ids(entries)
    assert await dwell(s) == {
        "D-REV03-1": (None, None),
        "D-REV03-2": (None, None),
        "D-REV03-3": (None, None),
        "D-REV02-0612": (40, shown[REV02_TASK]),
        "D-REV02-0618": (None, None),
        "D-CSH02": (95, shown[TASK]),
    }
    judgments = {j.id: j for j in await s.output_store.latest(EID, Judgment)}
    ba01 = judgments.pop(JID)
    assert (ba01.critic_verdict, ba01.critic_trigger, ba01.confidence) == (
        CriticVerdict.consistent,
        CriticTrigger.main,
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
    calls = [e for e in entries if e.kind is TrailKind.llm_call and e.procedure_id == "GEN-09"]
    assert [e.payload["fixture_key"] for e in calls] == ["BA-01-r1", "BA-01-r2"]
    assert_fixtures_used_once(entries, enabled_programs(s.runner.deps))
    assert all(p.status is not ProcedureStatus.in_dispute for p in s.output_store.rows if isinstance(p, Procedure))


async def test_dispute_variant_resolved_for_a6(demo_run: Demo) -> None:
    """验收 2: 复核模型取 llm_variants/dispute, BA-01 判为不一致. CSH-02 经 planned → in_dispute → completed; 呈递
    争议任务 95 秒后裁决 a6_upheld (D-ARB, consulted 真, 带咨询记录): 判断 v2 confirmed, 两条例外由它归因, 不建错报;
    decision 留痕带 dispute_resolution, consulted, 咨询记录与 dwell_seconds 95. 之后同主流程: 结果同 R55 (错报只有
    REV-02 那 1 条, 意见与报告同), 留痕计数同, 状态迁移只有 CSH-02 那两步不同 (行数同 R55)."""
    s = await start_demo(review_llm=MockLLMClient(VARIANTS / "dispute"), features=COLLAB)
    arbitration = resolve(DisputeResolution.a6_upheld, consulted=True, consultation_record=CONSULTATION)
    reviews = [arbitration if r.task_id == TASK else r for r in REVIEWS]
    await finish_demo(s, reviews, presenting(SHOWN))

    entries, before = trail_of(s).entries, transitions(reference(demo_run))
    moved = transitions(entries)
    assert [(old, new) for (pid, old, new) in moved.elements() if pid == "CSH-02"] == [
        ("planned", "in_dispute"),
        ("in_dispute", "completed"),
    ]
    assert moved - before == Counter({("CSH-02", "planned", "in_dispute"): 1, ("CSH-02", "in_dispute", "completed"): 1})
    assert moved.total() == before.total()
    judgment = await s.output_store.get(EID, Judgment, JID)
    assert judgment is not None
    assert (judgment.version, judgment.review_status, judgment.critic_verdict) == (
        2,
        ReviewStatus.confirmed,
        CriticVerdict.inconsistent,
    )
    explained = [await s.output_store.get(EID, AuditException, x) for x in judgment.exception_ids]
    assert [(x.id, x.disposition, x.attributed_by) for x in explained if x is not None] == [
        ("X-CSH-05-1-B01-1", ExceptionDisposition.attributed, JID),
        ("X-CSH-02-1-D01-1", ExceptionDisposition.attributed, JID),
    ]
    assert [m.review_record_id for m in await s.output_store.latest(EID, Misstatement)] == ["D-REV02-0612"]
    [trail] = [e for e in entries if e.kind is TrailKind.decision and e.idempotency_key == "D-ARB"]
    payload = trail.payload
    assert (payload["dispute_resolution"], payload["consulted"], payload["consultation_record"]) == (
        "a6_upheld",
        True,
        CONSULTATION,
    )
    assert (payload["dwell_seconds"], payload["presentation_id"]) == (95, presentation_ids(entries)[TASK])
    await assert_results(s)
    assert_trail_counts(entries)
    assert presentations(entries) == [(PRESENTATION, "U-1", REV02_TASK), (PRESENTATION, "U-1", TASK)]


@pytest.mark.external
async def test_xero17_path_skips_a7_and_records_dwell(demo_run: Demo) -> None:
    """验收 3 (设计稿第 7.3 节末段 "只开现有 17 条时", 本组开关打开): GEN-09 llm_call 0 行; 10 条判断 critic_verdict
    都是 skipped, critic_trigger 为空: REV-03 三条与 REV-02 六条为 L2 跳过, J-CSH-02-1-BA-091 为未命中触发规则
    (CSH-02 L3, 建议 misstatement 不中主规则; 第一轮证据不全是内部复印件; 推理链没有 assumption 与 unsupported 步骤;
    抽样点 0.1453 不小于 GEN-09 的 0.05). 复核任务仍 10 条; 复核人在决定前 60 秒呈递 T-J-CSH-02-1-BA-091-v1 后认定
    错报, 错报 M-D-CSH02 (4853) 的复核记录 dwell_seconds 60. 结果, 重要性, 报告与留痕同 R119 (状态迁移同 R55)."""
    x = await run_xero17(COLLAB, presenting({BA091_TASK: 60}))
    deps = x.runner.deps
    engagement = await deps.engagement_store.get(x.eid)
    assert engagement is not None and engagement.version_snapshot["features"] == "collab,critic"
    await assert_xero17_results(x)
    await assert_materiality(x)
    await assert_report(x)
    entries = await entries_of(x)
    assert_trail(entries, reference(demo_run))
    assert [e for e in entries if e.kind is TrailKind.llm_call and e.procedure_id == "GEN-09"] == []

    judgments = {j.id: j for j in await deps.output_store.latest(x.eid, Judgment)}
    critic = {jid: (j.critic_verdict, j.critic_trigger, j.critic_reason) for jid, j in judgments.items()}
    cash = critic.pop("J-CSH-02-1-BA-091")
    assert cash == (CriticVerdict.skipped, None, "未命中独立复核的触发规则")
    assert len(critic) == 9 and {jid.split("-1-")[0] for jid in critic} == {"J-REV-03", "J-REV-02"}
    assert set(critic.values()) == {(CriticVerdict.skipped, None, L2_SKIP)}
    judgment = judgments["J-CSH-02-1-BA-091"]
    steps = judgment.reasoning_chain
    assert not [s for s in steps if s.kind is StepKind.assumption or s.status is StepStatus.unsupported]
    assert not internal_copy_only(await ReviewAgent(deps).given_evidence(x.eid, judgment))
    rate = float(deps.library.load("GEN-09").params["sample_rate"])
    assert (round(sample_point(judgment.id), 4), rate) == (0.1453, 0.05)

    assert len(await deps.output_store.latest(x.eid, ReviewTask)) == 10
    [shown] = [e for e in entries if e.kind is TrailKind.presentation]
    assert (shown.procedure_id, shown.actor, shown.payload["task_id"]) == (PRESENTATION, x.owner, BA091_TASK)
    [misstatement] = await deps.output_store.latest(x.eid, Misstatement)
    record = await deps.output_store.get(x.eid, ReviewRecord, misstatement.review_record_id)
    assert record is not None
    assert (misstatement.id, misstatement.amount_cents) == ("M-D-CSH02", 4853)
    assert (record.dwell_seconds, record.presentation_id) == (60, shown.id)
