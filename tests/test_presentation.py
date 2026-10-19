"""判断呈递 (GEN-11, R152): POST review-tasks/{task_id}/presentations 按任务类型组装的视图与 presentation 留痕.

Numbat 争议变体 (功能 collab) 与主流程各跑一次到首次等复核, 一次运行多处断言; 证据并集与失败原因的取法用纯函数
参数化, 带对象的失败用真实判断引擎跑一次 REV-02 (不跑全流程).
"""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.adapters.llm import MockLLMClient
from backend.contracts.models import (
    AuditException,
    DisputeResolution,
    EvidencePointer,
    EvidenceType,
    ExceptionDisposition,
    Judgment,
    ProcedureRun,
    ReviewTask,
    TaskKind,
    TrailEntry,
    TrailKind,
)
from backend.contracts.views import PresentedSide, TaskPresentation
from backend.core.config import get_settings
from backend.orchestration.nodes import fail_run
from backend.orchestration.runner import GraphRunner
from backend.services.presentation import evidence_union, failure_of, presentation_entry
from backend.services.procedure_library import ProcedureLibrary
from backend.services.review_queue import ReviewQueue
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_AUDITOR,
    FakeTrail,
    api_client,
    make_run,
    numbat_rev08_exception,
    wait_idle,
)
from tests.test_collab import CASH_DRAFT, COLLAB, disputed
from tests.test_decisions import Started
from tests.test_dispute import JID, TASK, resolve
from tests.test_e2e_mock import start_demo, trail_of
from tests.test_fieldwork_rev02 import setup
from tests.test_flow import REVIEWS

EID = ENGAGEMENT.id
PRESENT = f"/api/v1/engagements/{EID}/review-tasks/{{task}}/presentations"
QUEUE = ReviewQueue(ProcedureLibrary(get_settings().KNOWLEDGE_ROOT))
REV02_TASK = "T-J-REV-02-1-SI-2026-0612-v1"


async def present(http: httpx.AsyncClient, task_id: str) -> httpx.Response:
    return await http.post(PRESENT.format(task=task_id))


def presentations(s: Started) -> list[TrailEntry]:
    return [e for e in trail_of(s).entries if e.kind is TrailKind.presentation]


async def checkpoint_id(s: Started) -> Any:
    return (await s.runner.app.aget_state(GraphRunner.config(EID))).config["configurable"]["checkpoint_id"]


async def test_dispute_shows_both_sides_evidence_union_and_records_each_opening() -> None:
    """验收 1-3: 争议变体 (collab) 首次等复核时呈递 T-J-CSH-02-1-BA-01-v1 两次.

    两方并排: 生成方为 A6 的结论, 7 步推理链, 反方论证, 置信度 0.5625, close, 无草稿; 复核方为 A7 的独立结论, 7 步
    推理链, different_conclusion 与理由, misstatement 与草稿. 证据为两方指针按比对键去重的并集 5 项 (回函第 1 页两个
    locator 算一项), 都为两方引用. 每次呈递一行留痕 (不带幂等键, 两次两行), 不写 Store, 不动检查点. 仲裁 (a7_upheld)
    后已决定的任务照样可呈递: 任务第 2 版, 目标仍是任务绑定的第 1 版 (原判断已出 v2 overridden)."""
    s, _ = await disputed(COLLAB)
    rows, commits, checkpoint = list(s.output_store.rows), len(s.output_store.commits), await checkpoint_id(s)
    async with api_client(s.runner) as http:
        first, second = await present(http, TASK), await present(http, TASK)
    assert (first.status_code, second.status_code) == (200, 200), first.text
    view = first.json()
    judgment = await s.output_store.get(EID, Judgment, JID)
    generator, reviewer = view["sides"]["generator"], view["sides"]["reviewer"]
    assert (generator["conclusion"], generator["counter_argument"]) == (judgment.conclusion, judgment.counter_argument)
    assert (len(generator["reasoning_chain"]), generator["confidence"], generator["proposed_action"]) == (
        7,
        0.5625,
        "close",
    )
    assert generator["misstatement_draft"] is None
    assert (reviewer["conclusion"], reviewer["reason"]) == (judgment.critic_conclusion, judgment.critic_reason)
    assert (len(reviewer["reasoning_chain"]), reviewer["reason_type"], reviewer["proposed_action"]) == (
        7,
        "different_conclusion",
        "misstatement",
    )
    assert reviewer["misstatement_draft"] == CASH_DRAFT.model_dump(mode="json")
    assert [
        (e["pointer"]["source_type"], e["pointer"]["source_id"], e["pointer"]["page"]) for e in view["evidence"]
    ] == [
        ("document", "DOC-ca18bcc6fa2d", 1),
        ("trial_balance", "1010", None),
        ("journal_entry", "CR-SI-2026-0547-1", None),
        ("bank_transaction", "BT-01-20260702-01", None),
        ("standard", "ASA505-P14", None),
    ]
    assert {e["origin"] for e in view["evidence"]} == {"both"}
    assert (view["allowed_decisions"], view["allowed_resolutions"], view["tier"]) == (
        ["dispute_resolution"],
        ["a6_upheld", "a7_upheld", "neither"],
        "full_review",
    )
    assert (view["deferred"], view["failure"], view["target"]["version"]) == (False, None, 1)
    # 视图里只有两方各自的内容: 防日后加上比较两方或推荐决定的字段 (规格书第 9.5 节争议项不评判谁更有理)
    assert set(TaskPresentation.model_fields) == {
        "presentation_id",
        "task",
        "procedure_name",
        "target",
        "allowed_decisions",
        "allowed_resolutions",
        "amount_cents",
        "tier",
        "deferred",
        "sides",
        "evidence",
        "failure",
    }
    assert set(PresentedSide.model_fields) == {
        "conclusion",
        "reasoning_chain",
        "proposed_action",
        "misstatement_draft",
        "counter_argument",
        "confidence",
        "reason_type",
        "reason",
    }

    entries = presentations(s)
    assert [e.id for e in entries] == [first.json()["presentation_id"], second.json()["presentation_id"]]
    assert entries[0].id != entries[1].id
    entry = entries[0]
    assert (entry.actor, entry.procedure_id, entry.run_id, entry.summary, entry.idempotency_key) == (
        NUMBAT_AUDITOR.id,
        "GEN-11",
        "RUN-CSH-02-1",
        f"呈递复核任务 {TASK}",
        None,
    )
    assert entry.payload == {
        "task_id": TASK,
        "task_version": 1,
        "target_type": "Judgment",
        "target_id": JID,
        "target_version": 1,
    }
    assert entry.created_at is not None
    assert (list(s.output_store.rows), len(s.output_store.commits)) == (rows, commits)
    assert await checkpoint_id(s) == checkpoint

    await s.submit.submit(EID, resolve(DisputeResolution.a7_upheld, misstatement_draft=CASH_DRAFT))
    await wait_idle(s.runner, EID)
    assert (await s.output_store.get(EID, Judgment, JID)).version == 2
    async with api_client(s.runner) as http:
        decided = await present(http, TASK)
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert (body["task"]["version"], body["task"]["status"]) == (2, "decided")
    assert (body["target"]["version"], body["target"]["review_status"]) == (1, "pending")
    assert presentations(s)[-1].payload == {**entry.payload, "task_version": 2}


async def test_normal_task_deferral_missing_busy_and_locked() -> None:
    """验收 1, 2, 4: 主流程确认 REV-03 三条 (第 7 节的前三条决定) 后呈递 T-J-REV-02-1-SI-2026-0612-v1: 只有生成方
    (反方论证, 草稿 14200000 overstated), 目标的独立复核为 skipped, 金额 14200000, 档位 full_review (超过执行重要性
    9000000), 证据只有生成方的; 标稍后处理后 deferred 为真. 任务不存在 404 NOT_FOUND 且不写留痕; 项目锁被占
    (主图在执行) 与项目已锁定时照常呈递并写留痕."""
    s = await start_demo()
    await wait_idle(s.runner, EID)
    for decision in REVIEWS[:3]:  # REV-02 判断要等 REV-03 的三条合同判断确认
        await s.submit.submit(EID, decision)
        await wait_idle(s.runner, EID)
    deferrals = f"/api/v1/engagements/{EID}/deferrals"
    async with api_client(s.runner) as http:
        opened = await present(http, REV02_TASK)
        assert opened.status_code == 200, opened.text
        view = opened.json()
        mark = {"target_type": "review_task", "target_id": REV02_TASK, "deferred": True}
        assert (await http.post(deferrals, json=mark)).status_code == 202
        deferred = (await present(http, REV02_TASK)).json()
        missing = await present(http, "T-none")
        s.runner.acquire(EID)
        try:
            busy = await present(http, REV02_TASK)
        finally:
            s.runner.release(EID)
        await s.engagement_store.update(EID, locked=True)
        locked = await present(http, REV02_TASK)
    assert list(view["sides"]) == ["generator"]
    side = view["sides"]["generator"]
    assert side["counter_argument"] and side["reason_type"] is None
    draft = side["misstatement_draft"]
    assert (draft["amount_cents"], draft["direction"]) == (14200000, "overstated")
    assert (view["target"]["critic_verdict"], view["amount_cents"], view["tier"]) == (
        "skipped",
        14200000,
        "full_review",
    )
    assert view["evidence"] and {e["origin"] for e in view["evidence"]} == {"generator"}
    assert (view["deferred"], deferred["deferred"], view["failure"]) == (False, True, None)
    assert (missing.status_code, missing.json()["detail"]["code"]) == (404, "NOT_FOUND")
    assert (busy.status_code, locked.status_code) == (200, 200), busy.text
    assert [e.payload["task_id"] for e in presentations(s)] == [REV02_TASK] * 4  # 404 那次不写


def pointer(
    source_type: str, source_id: str, page: int | None = None, locator: str = "amount_cents"
) -> EvidencePointer:
    kind = EvidenceType.standard if source_type == "standard" else EvidenceType.voucher
    return EvidencePointer(type=kind, source_type=source_type, source_id=source_id, locator=locator, page=page)


DOC_P1 = pointer("document", "DOC-1", 1, "confirmed_balance_cents")
DOC_P1_DATE = pointer("document", "DOC-1", 1, "balance_date")
DOC_P2 = pointer("document", "DOC-1", 2, "confirmed_balance_cents")
JE = pointer("journal_entry", "JE-1", locator="debit_cents")
JE_DATE = pointer("journal_entry", "JE-1", locator="date")
STD = pointer("standard", "ASA505-P14", locator="para 14")


@pytest.mark.parametrize(
    ("generator", "reviewer", "expected"),
    [
        # 文件同一页的不同 locator 算一项, 保留先出现的指针; 不同页各一项
        ([DOC_P1, DOC_P1_DATE, DOC_P2], [], [(DOC_P1, "generator"), (DOC_P2, "generator")]),
        # 行按来源比, 不看 locator; 两方都引用为 both, 保留生成方的指针
        ([JE], [JE_DATE], [(JE, "both")]),
        # 只有复核方引用的接在生成方的之后
        ([JE, DOC_P1], [STD, DOC_P1_DATE], [(JE, "generator"), (DOC_P1, "both"), (STD, "reviewer")]),
        ([], [STD, STD], [(STD, "reviewer")]),
    ],
)
def test_evidence_union(
    generator: list[EvidencePointer], reviewer: list[EvidencePointer], expected: list[tuple[EvidencePointer, str]]
) -> None:
    assert [(e.pointer, e.origin) for e in evidence_union(generator, reviewer)] == expected


def whole_run_failure() -> tuple[ReviewTask, ProcedureRun]:
    """整次执行失败: A1 代写的执行记录 (failed) 与不带 subject_id 的 failed 任务."""
    patch = fail_run(QUEUE, make_run("REV-02"), "RuntimeError: boom", NOW)
    return patch["review_tasks"][0], patch["procedure_runs"][0]


def unattributed() -> tuple[ReviewTask, AuditException]:
    """无归因: SI-2026-0612 判断失败, REV-08 的跨期例外出 v2 unattributed 并建任务 (T-X-REV-08-1-R01-1-v2)."""
    first = numbat_rev08_exception(make_run("REV-08"))
    marked = first.model_copy(update={"version": 2, "disposition": ExceptionDisposition.unattributed})
    task = QUEUE.make_task(TaskKind.unattributed, marked, "REV-02", "RUN-REV-02-1", NOW, subject_id="SI-2026-0612")
    return task, marked


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (whole_run_failure, {"source": "run", "reason": "RuntimeError: boom"}),
        (
            unattributed,
            {
                "source": "exception",
                "rule_id": "R01",
                "amount_cents": 14200000,
                "detail": {"posting_date": "2026-06-28", "signed_date": "2026-07-03"},
            },
        ),
    ],
)
def test_failure_of_run_and_exception(case: Any, expected: dict[str, Any]) -> None:
    task, target = case()
    failure = failure_of(task, target)
    assert failure is not None and failure.model_dump(exclude_defaults=True) == expected


async def test_failed_subject_takes_last_llm_call_of_that_subject(tmp_path: Path) -> None:
    """验收 5: 真实判断引擎跑 REV-02, SI-2026-0618 的输出连续 3 次结构不合 (测试临时目录里的固定输出): 建
    T-RUN-REV-02-1-SI-2026-0618-v1, 失败原因取本次执行里 fixture_key 为 SI-2026-0618 的最后一行 llm_call (structure,
    errors, 3 次); 在它之后写的别的行 (同一执行的独立复核, 别的执行里同一对象) 不被取到. 正常的判断任务没有失败原因."""
    fixtures = tmp_path / "REV-02"
    fixtures.mkdir()
    source = Path(__file__).resolve().parent / "fixtures" / "llm" / "REV-02" / "SI-2026-0612.json"
    shutil.copy(source, fixtures / "SI-2026-0612.json")
    (fixtures / "SI-2026-0618.json").write_text(json.dumps({"conclusion": "Synthetic malformed output"}))
    agent, ctx, _ = setup(real=True)
    agent.deps.judgments.llm = MockLLMClient(tmp_path)  # type: ignore[attr-defined]
    patch = await agent.run(ctx)
    tasks = {t.id: t for t in patch["review_tasks"]}
    task, run = tasks["T-RUN-REV-02-1-SI-2026-0618-v1"], patch["procedure_runs"][0]
    trail = agent.deps.trail
    assert isinstance(trail, FakeTrail)
    calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    mine = [e for e in calls if e.payload["fixture_key"] == "SI-2026-0618"]
    assert [e.payload["attempt"] for e in mine] == [1, 2, 3]
    later = [  # 之后才写的: 同一执行里 GEN-09 的行, 第 2 次执行里同一对象的行
        mine[-1].model_copy(update={"id": "TR-critic", "procedure_id": "GEN-09"}),
        mine[-1].model_copy(update={"id": "TR-rerun", "run_id": "RUN-REV-02-2"}),
    ]
    failure = failure_of(task, run, [*calls, *later])
    assert failure is not None
    assert (failure.source, failure.subject_id, failure.level_failed, failure.attempts, failure.trail_id) == (
        "llm_call",
        "SI-2026-0618",
        "structure",
        3,
        mine[-1].id,
    )
    assert failure.errors == mine[-1].payload["errors"] and failure.errors
    assert failure_of(tasks["T-J-REV-02-1-SI-2026-0612-v1"], run, calls) is None


def test_presentation_entry_time_is_injected() -> None:
    """呈递时间由调用方给 (复核停留时长按它算): created_at 即给的时间, payload 的 target_version 为任务绑定的版本."""
    task, _ = unattributed()
    at = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    entry = presentation_entry(EID, task, NUMBAT_AUDITOR.id, at)
    assert (entry.kind, entry.created_at, entry.payload["target_version"], entry.run_id) == (
        TrailKind.presentation,
        at,
        2,
        "RUN-REV-02-1",
    )
