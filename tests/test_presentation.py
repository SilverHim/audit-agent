"""判断呈递 (GEN-11, R152): POST review-tasks/{task_id}/presentations 按任务类型组装的视图与 presentation 留痕.

Numbat 争议变体 (功能 collab) 只跑一次, 依次呈递争议, normal, 带对象的失败与已决定的任务, 一次运行多处断言; 证据
并集与失败原因的取法用纯函数参数化, 带对象的失败另用真实判断引擎跑一次 REV-02 (不跑全流程).
"""

import json
import shutil
from datetime import UTC, datetime, timedelta
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
    ReviewRecord,
    ReviewTask,
    ReviewTier,
    TaskKind,
    TaskStatus,
    TrailEntry,
    TrailKind,
)
from backend.contracts.views import PresentedSide, TaskPresentation
from backend.core.config import get_settings
from backend.orchestration.nodes import fail_run
from backend.orchestration.runner import GraphRunner
from backend.services.presentation import (
    assemble,
    dwell_seconds,
    evidence_union,
    failure_of,
    presentation_entry,
    record_presentation,
)
from backend.services.procedure_library import ProcedureLibrary
from backend.services.review_queue import ReviewQueue
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_AUDITOR,
    FakeTrail,
    LayeredMockLLM,
    api_client,
    make_run,
    numbat_rev08_exception,
    wait_idle,
)
from tests.test_collab import CASH_DRAFT, COLLAB
from tests.test_concierge_chat_tools import JUDGMENT
from tests.test_decisions import Started
from tests.test_dispute import JID, TASK, VARIANTS, resolve
from tests.test_e2e_mock import start_demo, trail_of
from tests.test_fieldwork_rev02 import setup
from tests.test_flow import REVIEWS
from tests.test_ingest_doc06 import raw as doc06_raw
from tests.test_ingest_doc06 import scripted as doc06_scripted
from tests.test_ingest_doc06 import setup as doc06_setup

EID = ENGAGEMENT.id
PRESENT = f"/api/v1/engagements/{EID}/review-tasks/{{task}}/presentations"
QUEUE = ReviewQueue(ProcedureLibrary(get_settings().KNOWLEDGE_ROOT))
REV02_TASK = "T-J-REV-02-1-SI-2026-0612-v1"
FAILED_TASK = "T-RUN-REV-02-1-SI-2026-0618-v1"


async def present(http: httpx.AsyncClient, task_id: str) -> httpx.Response:
    return await http.post(PRESENT.format(task=task_id))


def presentations(s: Started) -> list[TrailEntry]:
    return [e for e in trail_of(s).entries if e.kind is TrailKind.presentation]


async def checkpoint_id(s: Started) -> Any:
    return (await s.runner.app.aget_state(GraphRunner.config(EID))).config["configurable"]["checkpoint_id"]


async def test_presentations_through_one_dispute_run(tmp_path: Path) -> None:
    """Numbat 争议变体 (功能 collab) 跑一次, 依次呈递各类任务. REV-02 SI-2026-0618 的主模型输出在测试临时目录里换成
    结构不合的 (SI-2026-0612 不受影响).

    1. 验收 1-3: 首次等复核时呈递 T-J-CSH-02-1-BA-01-v1 两次. 两方并排: 生成方为 A6 的结论, 7 步推理链, 反方论证,
       置信度 0.5625, close, 无草稿; 复核方为 A7 的独立结论, 7 步推理链, different_conclusion 与理由, misstatement
       与草稿. 证据为两方指针按比对键去重的并集 5 项 (回函第 1 页两个 locator 算一项), 都为两方引用. 每次呈递一行留痕
       (不带幂等键, 两次两行), 不写 Store, 不动检查点.
    2. 验收 4: 确认 REV-03 三条 (第 7 节的前三条决定) 后呈递 T-J-REV-02-1-SI-2026-0612-v1: 只有生成方 (反方论证,
       草稿 14200000 overstated), 目标的独立复核为 skipped, 金额 14200000, 档位 full_review, 证据只有生成方的; 标稍后
       处理后 deferred 为真, 这条任务决定之后再呈递为假 (已决定的不算稍后处理). 任务不存在 404 NOT_FOUND 且不写留痕.
    3. 验收 5 (经接口): 带对象的失败任务 T-RUN-REV-02-1-SI-2026-0618-v1 的失败原因读本次执行的 llm_call 留痕:
       structure, 尝试 3 次.
    4. 仲裁 (a7_upheld) 后已决定的争议任务照样可呈递: 任务第 2 版, 目标仍是任务绑定的第 1 版 (原判断已出 v2
       overridden). 项目锁被占 (主图在执行) 与项目已锁定时照常呈递并写留痕."""
    broken = tmp_path / "llm"
    (broken / "REV-02").mkdir(parents=True)
    (broken / "REV-02" / "SI-2026-0618.json").write_text(json.dumps({"conclusion": "Synthetic malformed output"}))
    llm = LayeredMockLLM(broken, get_settings().LLM_FIXTURES_ROOT)
    s = await start_demo(llm=llm, review_llm=MockLLMClient(VARIANTS / "dispute"), features=COLLAB)
    await wait_idle(s.runner, EID)
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

    for decision in REVIEWS[:3]:  # REV-02 判断要等 REV-03 的三条合同判断确认
        await s.submit.submit(EID, decision)
        await wait_idle(s.runner, EID)
    deferrals = f"/api/v1/engagements/{EID}/deferrals"
    async with api_client(s.runner) as http:
        opened = await present(http, REV02_TASK)
        assert opened.status_code == 200, opened.text
        normal = opened.json()
        mark = {"target_type": "review_task", "target_id": REV02_TASK, "deferred": True}
        assert (await http.post(deferrals, json=mark)).status_code == 202
        deferred = (await present(http, REV02_TASK)).json()
        missing = await present(http, "T-none")
        failed = (await present(http, FAILED_TASK)).json()
    assert list(normal["sides"]) == ["generator"]
    side = normal["sides"]["generator"]
    assert side["counter_argument"] and side["reason_type"] is None
    draft = side["misstatement_draft"]
    assert (draft["amount_cents"], draft["direction"]) == (14200000, "overstated")
    assert (normal["target"]["critic_verdict"], normal["amount_cents"], normal["tier"]) == (
        "skipped",
        14200000,
        "full_review",
    )
    assert normal["evidence"] and {e["origin"] for e in normal["evidence"]} == {"generator"}
    assert (normal["deferred"], deferred["deferred"], normal["failure"]) == (False, True, None)
    assert (missing.status_code, missing.json()["detail"]["code"]) == (404, "NOT_FOUND")
    failure = failed["failure"]
    assert (failure["source"], failure["subject_id"], failure["level_failed"], failure["attempts"]) == (
        "llm_call",
        "SI-2026-0618",
        "structure",
        3,
    )
    assert failure["errors"] and failed["sides"] == {} and failed["evidence"] == []

    await s.submit.submit(EID, REVIEWS[3])  # SI-2026-0612 认定错报: 稍后处理的标记随之失效
    await wait_idle(s.runner, EID)
    await s.submit.submit(EID, resolve(DisputeResolution.a7_upheld, misstatement_draft=CASH_DRAFT))
    await wait_idle(s.runner, EID)
    assert (await s.output_store.get(EID, Judgment, JID)).version == 2
    async with api_client(s.runner) as http:
        after = (await present(http, REV02_TASK)).json()
        decided = await present(http, TASK)
        s.runner.acquire(EID)
        try:
            busy = await present(http, TASK)
        finally:
            s.runner.release(EID)
        await s.engagement_store.update(EID, locked=True)
        locked = await present(http, TASK)
    assert (after["task"]["status"], after["deferred"]) == ("decided", False)
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert (body["task"]["version"], body["task"]["status"]) == (2, "decided")
    assert (body["target"]["version"], body["target"]["review_status"]) == (1, "pending")
    assert (busy.status_code, locked.status_code) == (200, 200), busy.text
    last = presentations(s)[-1]
    assert last.payload == {**entry.payload, "task_version": 2}
    opened_tasks = [e.payload["task_id"] for e in presentations(s)]  # 404 那次不写
    assert opened_tasks == [TASK, TASK, REV02_TASK, REV02_TASK, FAILED_TASK, REV02_TASK, TASK, TASK, TASK]


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


def test_generator_evidence_includes_conclusion_pointers() -> None:
    """生成方的证据含结论引用的证据, 不只推理链各步的: 只在结论里引用的指针 (这里是另一张发票的行) 也进并集; 发票与
    发运单在结论与推理链里都有, 各算一项; 准则只在推理链里. normal 任务只有生成方."""
    extra = pointer("sales_invoice", "SI-2026-0618", locator="amount_cents")
    judgment = JUDGMENT.model_copy(update={"evidence": [*JUDGMENT.evidence, extra]})
    task = QUEUE.make_task(TaskKind.normal, judgment, "REV-02", "RUN-REV-02-1", NOW)
    view = assemble(
        task,
        judgment,
        presentation_id="TR-1",
        procedure_name="收入截止性判断",
        allowed_decisions=[],
        allowed_resolutions=[],
        amount_cents=None,
        tier=ReviewTier.read_reasoning,
        deferred=False,
    )
    assert [(e.pointer.source_id, e.origin) for e in view.evidence] == [
        ("SI-2026-0612", "generator"),
        ("DN-2026-0612", "generator"),
        ("SI-2026-0618", "generator"),
        ("AASB15-P38", "generator"),
    ]


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
    T-RUN-REV-02-1-SI-2026-0618-v1, 失败原因取本次执行里对象为 SI-2026-0618 的最后一行 llm_call (structure,
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
    # 需求 R158 之前的旧行没有 subject_id, 按 fixture_key 找, 结果相同
    legacy = [e.model_copy(update={"payload": {k: v for k, v in e.payload.items() if k != "subject_id"}}) for e in mine]
    assert failure_of(task, run, legacy) == failure


@pytest.mark.parametrize("recorded", [False, True], ids=["no_key", "none"])
async def test_non_judge_failed_subject_found_by_fixture_key(recorded: bool) -> None:
    """判断引擎以外的调用方 (A3 的 DOC-06 回函抽取, fixture_key 即账户) 不记对象: BA-01 账号不符 (语义失败, 回传
    重试三次仍不过, 需求 R348) 时失败卡片按 fixture_key 找到这几次调用; 留痕记了 subject_id 而为空时同样回落 (第 2 轮
    审核 R158-11)."""
    agent, ctx, trail = doc06_setup(doc06_scripted(BA_01=[doc06_raw("BA-01", account_no="99999999")] * 3))
    patch = await agent.run(ctx)
    [task] = patch["review_tasks"]
    [run] = patch["procedure_runs"]
    calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    if recorded:
        calls = [e.model_copy(update={"payload": {**e.payload, "subject_id": None}}) for e in calls]
    failure = failure_of(task, run, calls)
    assert failure is not None
    assert (failure.subject_id, failure.level_failed, failure.attempts) == ("BA-01", "semantic", 3)


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


# ── R153 复核停留时长: dwell_seconds 取该复核人对该任务在决定之前最近一次呈递 ──


def shown(at: str, actor: str = "U-1", task_id: str = TASK) -> TrailEntry:
    """10 月 1 日 at (时:分:秒, UTC) 的一次呈递留痕 (字段同 presentation_entry 写的), ID 为 P-{actor}-{at}."""
    return TrailEntry(
        id=f"P-{actor}-{at}",
        engagement_id=EID,
        kind=TrailKind.presentation,
        actor=actor,
        procedure_id="GEN-11",
        summary=f"呈递复核任务 {task_id}",
        payload={"task_id": task_id},
        created_at=datetime.fromisoformat(f"2026-10-01T{at}+00:00"),
    )


DECIDED = datetime(2026, 10, 1, 10, 1, 35, tzinfo=UTC)


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([shown("10:00:00")], (95, "P-U-1-10:00:00")),
        ([shown("10:00:00"), shown("10:01:00")], (35, "P-U-1-10:01:00")),  # 两次呈递取最近一次
        ([shown("10:00:00"), shown("10:02:00")], (95, "P-U-1-10:00:00")),  # 晚于决定的不算
        ([shown("10:00:00"), shown("10:01:00", actor="U-2")], (95, "P-U-1-10:00:00")),  # 别人的不算
        ([shown("10:01:00", task_id="T-J-REV-03-1-CON-001-v1")], (None, None)),  # 别的任务的不算
        ([], (None, None)),
        ([shown("10:01:34.400000")], (0, "P-U-1-10:01:34.400000")),  # 不足 1 秒向下取整
        ([shown("10:01:35")], (0, "P-U-1-10:01:35")),  # 与决定同一时刻也算
    ],
)
def test_dwell_seconds(entries: list[TrailEntry], expected: tuple[int | None, str | None]) -> None:
    """R153 验收 2, 4: 决定 10:01:35 提交, 取 U-1 对该任务不晚于决定的最近一次呈递, 秒数向下取整."""
    noise = shown("10:01:30").model_copy(update={"id": "D-OTHER", "kind": TrailKind.decision})  # 不是呈递的不算
    assert dwell_seconds([noise, *entries], TASK, "U-1", DECIDED) == expected


SHOWN_AT = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)


async def presented_ba01(features: frozenset[str]) -> tuple[Started, str]:
    """Numbat 全流程停在等复核, 先写 501 行别的任务的呈递 (多于 all_of_kind 的一页), 再于 10:00:00 由 U-1 呈递
    BA-01 的任务. 返回运行与这次呈递的留痕 ID."""
    s = await start_demo(features=features)
    await wait_idle(s.runner, EID)
    task = await s.output_store.get(EID, ReviewTask, TASK)
    assert task is not None and task.status is TaskStatus.pending
    trail = trail_of(s)
    for n in range(501):
        await record_presentation(trail, EID, task.model_copy(update={"id": f"T-OTHER-{n}"}), "U-1", SHOWN_AT)
    return s, await record_presentation(trail, EID, task, "U-1", SHOWN_AT)


async def test_dwell_seconds_from_last_presentation() -> None:
    """R153 验收 3, 5, 7 (collab 打开, 全流程只此一处): 10:01:35 提交 confirm D-CSH02, 存入的记录 dwell_seconds 95,
    presentation_id 为 10:00:00 那次呈递 (排在 501 行别的任务的呈递之后, 须翻页才读到), decision 留痕 payload 带这
    两项; 提交时调用方给的值不用. 闸门决定的留痕不带停留时长. collab 关闭见 test_review_decisions 的 timed 用例."""
    s, presentation_id = await presented_ba01(COLLAB)
    [decision] = [r for r in REVIEWS if r.id == "D-CSH02"]  # confirm T-J-CSH-02-1-BA-01-v1
    decision = decision.model_copy(update={"decided_at": SHOWN_AT + timedelta(seconds=95)})
    await s.submit.submit(EID, decision.model_copy(update={"dwell_seconds": 1, "presentation_id": "TR-FORGED"}))
    await wait_idle(s.runner, EID)

    stored = await s.output_store.get(EID, ReviewRecord, "D-CSH02")
    assert stored is not None and s.runner.run_error(EID) is None
    entries = {e.idempotency_key: e for e in trail_of(s).entries if e.kind is TrailKind.decision}
    payload = entries["D-CSH02"].payload
    assert (stored.dwell_seconds, stored.presentation_id) == (95, presentation_id)
    assert (payload["dwell_seconds"], payload["presentation_id"]) == (95, presentation_id)
    assert not {"dwell_seconds", "presentation_id"} & set(entries["D-ACC"].payload)  # 验收 7
