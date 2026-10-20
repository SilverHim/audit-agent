"""A6 判断程序的公共流程: 取判断对象, 逐个交判断引擎, 按需要人表产出判断, 任务与例外新版本 (接口文档第 5 节)."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.fieldwork import FieldworkAgent, Subject, Subjects
from backend.contracts.models import (
    AgentId,
    AuditException,
    AutonomyLevel,
    Decision,
    ExceptionDisposition,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ReviewStatus,
    ReviewTask,
    RunStatus,
    TargetType,
    TaskKind,
    TaskStatus,
)
from backend.contracts.services import JudgmentContext, JudgmentOutcome
from backend.orchestration.nodes import GraphNodes, apply_patch
from tests.fakes import (
    NUMBAT_OBJECTIVES,
    NUMBAT_RISKS,
    FakeJudgmentEngine,
    FakeRegistry,
    FakeTrail,
    failed,
    finished,
    judgment,
    make_ctx,
    make_deps,
    make_procedure,
    make_run,
    numbat_p2_agents,
    numbat_rev08_exception,
    passed,
    wait_idle,
)
from tests.test_fieldwork import p2_agents
from tests.test_rerun_decision import EID, at_review, rerun, run
from tests.test_review_decisions import record

NOW = datetime(2026, 10, 4, tzinfo=UTC)
X1 = numbat_rev08_exception(make_run("REV-08"))  # X-REV-08-1-R01-1
X2 = X1.model_copy(update={"id": "X-REV-08-1-R02-1", "rule_id": "R02"})


def setup(
    procedure_id: str,
    subjects: list[Subject],
    outcomes: dict[str, Any],
    rerun: list[str] | None = None,
    exceptions: list[AuditException] | None = None,
    features: frozenset[str] | None = None,
) -> tuple[FieldworkAgent, RunContext, FakeJudgmentEngine]:
    """A6 与某判断程序第 1 次执行的上下文: 程序关联收入两条目标, 状态有 Numbat 风险清单与给定例外.

    subjects 经 subject_sources 登记 (各判断程序接入前, 用它代替程序自己的取对象逻辑). features 缺省为基线.
    """
    engine = FakeJudgmentEngine(outcomes)
    deps = make_deps(FakeRegistry(), FakeTrail(), features=features)
    deps = replace(deps, judgments=engine, review_llm=None)  # type: ignore[arg-type]
    ctx = make_ctx(deps, procedure_id)
    procedure = ctx.procedure.model_copy(
        update={"objective_ids": ["OBJ-REV-OCC", "OBJ-REV-CUT"], "rerun_subject_ids": rerun or []}
    )
    state = ctx.state | {
        "procedure_pack": [procedure],
        "audit_objectives": NUMBAT_OBJECTIVES,
        "risk_register": NUMBAT_RISKS,
        "exceptions": exceptions or [],
        "procedure_runs": [],
    }
    run = ctx.run.model_copy(update={"params_snapshot": {"period_end": "2026-06-30", "window_days": 20}})
    agent = FieldworkAgent(deps)

    async def source(c: RunContext) -> Subjects:
        return Subjects(subjects)

    agent.subject_sources[procedure_id] = source
    return agent, RunContext(ctx.engagement, procedure, ctx.template, run, state), engine  # type: ignore[arg-type]


ABC = [Subject("A", {"n": 1}), Subject("B", {"n": 2}), Subject("C", {"n": 3})]


# ── 判断对象与上下文 ─────────────────────────────────────────


async def test_rerun_subject_ids_filter() -> None:
    outcomes = {s: passed() for s in "ABC"}
    agent, ctx, engine = setup("REV-03", ABC, outcomes, rerun=["B"])
    await agent.run(ctx)
    assert [c.subject_id for _, c, _ in engine.calls] == ["B"]
    agent, ctx, engine = setup("REV-03", ABC, outcomes)
    await agent.run(ctx)
    assert [c.subject_id for _, c, _ in engine.calls] == ["A", "B", "C"]


async def test_context_task_fixture_key_and_performer() -> None:
    agent, ctx, engine = setup("REV-03", [Subject("CON-001", {"contract": {"id": "CON-001"}})], {"CON-001": passed()})
    await agent.run(ctx)
    [(procedure_id, jctx, performed_by)] = engine.calls
    assert procedure_id == "REV-03" and performed_by == ctx.engagement.owner_id == "U-1"
    assert jctx.task["params"] == ctx.run.params_snapshot
    assert [r["id"] for r in jctx.task["risks"]] == ["RISK-REV-OCC", "RISK-REV-CUT"]
    assert jctx.task["risks"][0] == NUMBAT_RISKS[0].model_dump(mode="json")
    assert jctx.fixture_key == jctx.subject_id == "CON-001" and jctx.data == {"contract": {"id": "CON-001"}}


# ── 需要人表 ─────────────────────────────────────────


async def test_l2_judgment_gets_normal_task() -> None:
    agent, ctx, _ = setup("REV-03", [Subject("CON-001", {})], {"CON-001": passed()})
    patch = await agent.run(ctx)
    [j] = patch["judgments"]
    assert j.id == "J-REV-03-1-CON-001" and j.review_status is ReviewStatus.pending and j.version == 1
    [task] = patch["review_tasks"]
    assert task.id == "T-J-REV-03-1-CON-001-v1" and task.kind is TaskKind.normal
    assert (task.target_type, task.target_id, task.target_version) == (TargetType.Judgment, j.id, 1)
    assert (task.run_id, task.subject_id) == ("RUN-REV-03-1", None)
    assert patch["exceptions"] == []


async def test_auto_close_confirms_and_attributes_exceptions() -> None:
    """L3 accept, 模板 always_review 为假 (REV-02): 不建任务, 判断第 1 版即 confirmed, 例外出新版本 attributed."""
    subject = Subject("SI-2026-0618", {}, [X1, X2])
    agent, ctx, _ = setup(
        "REV-02", [subject], {"SI-2026-0618": passed(autonomy_level=AutonomyLevel.L3)}, exceptions=[X1, X2]
    )
    patch = await agent.run(ctx)
    [j] = patch["judgments"]
    assert j.review_status is ReviewStatus.confirmed and j.version == 1
    assert patch["review_tasks"] == []
    assert [(x.id, x.version, x.disposition, x.attributed_by) for x in patch["exceptions"]] == [
        (X1.id, 2, ExceptionDisposition.attributed, j.id),
        (X2.id, 2, ExceptionDisposition.attributed, j.id),
    ]


async def test_always_review_template_gets_task_even_at_l3() -> None:
    subject = Subject("BA-01", {}, [])
    agent, ctx, _ = setup("CSH-02", [subject], {"BA-01": passed(autonomy_level=AutonomyLevel.L3)})
    patch = await agent.run(ctx)
    assert [t.kind for t in patch["review_tasks"]] == [TaskKind.normal]
    assert patch["judgments"][0].review_status is ReviewStatus.pending


async def test_hybrid_failure_with_findings_marks_first_exception_unattributed() -> None:
    subject = Subject("SI-2026-0612", {}, [X1, X2])
    agent, ctx, _ = setup("REV-02", [subject], {"SI-2026-0612": failed("unattributed")}, exceptions=[X1, X2])
    patch = await agent.run(ctx)
    assert patch["judgments"] == []
    [marked] = patch["exceptions"]
    assert (marked.id, marked.version, marked.disposition) == (X1.id, 2, ExceptionDisposition.unattributed)
    [task] = patch["review_tasks"]
    assert task.id == f"T-{X1.id}-v2" and task.kind is TaskKind.unattributed
    assert (task.target_type, task.target_version) == (TargetType.AuditException, 2)
    assert (task.run_id, task.subject_id) == ("RUN-REV-02-1", "SI-2026-0612")


async def test_failure_without_findings_gets_failed_task() -> None:
    agent, ctx, _ = setup("REV-02", [Subject("SI-2026-0618", {})], {"SI-2026-0618": failed("unattributed")})
    patch = await agent.run(ctx)
    [task] = patch["review_tasks"]
    assert task.id == "T-RUN-REV-02-1-SI-2026-0618-v1" and task.kind is TaskKind.failed
    assert (task.target_type, task.target_id, task.target_version) == (TargetType.ProcedureRun, "RUN-REV-02-1", 1)
    assert task.subject_id == "SI-2026-0618" and patch["exceptions"] == []


async def test_llm_program_failure_gets_failed_task() -> None:
    agent, ctx, _ = setup("REV-03", [Subject("CON-002", {})], {"CON-002": failed("failed")})
    patch = await agent.run(ctx)
    [task] = patch["review_tasks"]
    assert task.id == "T-RUN-REV-03-1-CON-002-v1" and task.kind is TaskKind.failed


async def test_one_failure_does_not_affect_others() -> None:
    outcomes = {"A": passed(), "B": failed("failed"), "C": passed(autonomy_level=AutonomyLevel.L3)}
    agent, ctx, _ = setup("REV-03", ABC, outcomes)
    patch = await agent.run(ctx)
    assert [(j.subject_id, j.review_status) for j in patch["judgments"]] == [
        ("A", ReviewStatus.pending),
        ("C", ReviewStatus.confirmed),
    ]
    assert [(t.kind, t.subject_id) for t in patch["review_tasks"]] == [(TaskKind.normal, None), (TaskKind.failed, "B")]
    [run] = patch["procedure_runs"]
    assert run.id == "RUN-REV-03-1" and run.status is RunStatus.succeeded and run.ended_at is not None


async def test_engine_exception_waits_for_other_subjects() -> None:
    """某对象的引擎调用抛异常 (如留痕写失败) 时, 等其余对象结束再上抛, 由 A1 记整次失败."""
    release = asyncio.Event()
    finished_subjects: list[str] = []
    agent, ctx, engine = setup("REV-03", ABC, {s: passed() for s in "ABC"})

    async def judge(eid: str, template: Any, run: ProcedureRun, jctx: JudgmentContext, performed_by: str) -> Any:
        if jctx.subject_id == "A":
            await release.wait()
        if jctx.subject_id == "B":
            raise RuntimeError("trail unavailable")
        finished_subjects.append(jctx.subject_id)
        return engine.outcomes[jctx.subject_id](jctx, run)

    engine.judge = judge  # type: ignore[method-assign]
    running = asyncio.create_task(agent.run(ctx))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not running.done() and finished_subjects == ["C"]
    release.set()
    with pytest.raises(RuntimeError, match="trail unavailable"):
        await running
    assert sorted(finished_subjects) == ["A", "C"]


# ── 已交人处理的对象 ─────────────────────────────────────────


def pending_task(**fields: Any) -> ReviewTask:
    base = ReviewTask(
        id="T-x",
        kind=TaskKind.unattributed,
        procedure_id="REV-02",
        target_type=TargetType.AuditException,
        target_id=X1.id,
        target_version=2,
        run_id="RUN-REV-02-1",
        created_at=NOW,
    )
    return base.model_copy(update=fields)


async def test_subjects_awaiting_review_are_not_judged_again() -> None:
    """A 的 unattributed 任务, B 的判断的 normal 任务仍待决: 整条重跑只判 C; 已决定的与别的程序的任务不算."""
    b_judgment = judgment(JudgmentContext(subject_id="B", task={}, data={}, fixture_key="B"), make_run("REV-02"))
    tasks = [
        pending_task(id="T-A", subject_id="A"),
        pending_task(
            id="T-B", kind=TaskKind.normal, target_type=TargetType.Judgment, target_id=b_judgment.id, target_version=1
        ),
        pending_task(id="T-C-old", subject_id="C", status=TaskStatus.decided),
        pending_task(id="T-other", procedure_id="REV-03", subject_id="C"),
    ]
    agent, ctx, engine = setup("REV-02", ABC, {s: passed() for s in "ABC"})
    state = ctx.state | {"review_tasks": tasks, "judgments": [b_judgment]}
    await agent.run(RunContext(ctx.engagement, ctx.procedure, ctx.template, ctx.run, state))  # type: ignore[arg-type]
    assert [jctx.subject_id for _, jctx, _ in engine.calls] == ["C"]


def two_failures(third: str, engines: list[FakeJudgmentEngine], a: str, b: str) -> Callable[[Any], dict[str, Any]]:
    """REV-02 判 a, b 两个对象 (a 带 X1, 由真实 A6 与假引擎): 第 1 次执行 a 失败转 unattributed, b 失败为 failed;
    第 2 次执行判 b 时引擎抛错 (整次失败); 之后 b 成功, a 按 third (unattributed 仍失败, auto_close 为 L3 自动关闭,
    normal 为 L2 待复核)."""

    def agents(deps: Any) -> dict[str, Any]:
        def for_a(jctx: JudgmentContext, current: ProcedureRun) -> JudgmentOutcome:
            if current.run_no == 1 or third == "unattributed":
                return failed("unattributed")(jctx, current)  # type: ignore[no-any-return]
            level = AutonomyLevel.L3 if third == "auto_close" else AutonomyLevel.L2
            return passed(autonomy_level=level)(jctx, current)  # type: ignore[no-any-return]

        def for_b(jctx: JudgmentContext, current: ProcedureRun) -> JudgmentOutcome:
            if current.run_no == 1:
                return failed("unattributed")(jctx, current)  # type: ignore[no-any-return]
            if current.run_no == 2:
                raise RuntimeError("transient trail failure")
            return passed()(jctx, current)  # type: ignore[no-any-return]

        engine = FakeJudgmentEngine({a: for_a, b: for_b})
        engines.append(engine)
        agent = FieldworkAgent(replace(deps, judgments=engine, review_llm=None))

        async def source(ctx: RunContext) -> Subjects:
            current_x = next(x for x in ctx.state["exceptions"] if x.id == X1.id)
            return Subjects([Subject(a, {}, [current_x]), Subject(b, {})])

        agent.subject_sources["REV-02"] = source
        return {**p2_agents(deps), "REV-08": numbat_p2_agents(deps)["REV-08"], "REV-02": agent}

    return agents


@pytest.mark.parametrize("third", ["unattributed", "auto_close", "normal"])
async def test_rerun_takes_the_failed_round_together(third: str) -> None:
    """实际图三轮 (需求 R347, Q17 一轮失败算一组): A 失败交人 (X v2 unattributed, 任务 T-X-v2), B 失败 (failed). 对 B
    rerun: 同一轮的失败对象一并重跑, A 的 unattributed 任务也 decided, 重判对象为 A, B; 第 2 次执行判 B 时引擎抛错,
    整次失败; 再整条重跑, 第 3 次执行 A, B 都判, 成功. A 的结果按 third: 仍失败时又是 unattributed (目标 X v3),
    自动关闭时 X 随判断归因 (v3 attributed), 待复核时为 normal 任务."""
    a, b = "SI-2026-0612", "SI-2026-0618"
    engines: list[FakeJudgmentEngine] = []
    s = await at_review(two_failures(third, engines, a, b))
    first = await s.output_store.latest(EID, ReviewTask, procedure_id="REV-02", status=TaskStatus.pending)
    old = next(t for t in first if t.kind is TaskKind.unattributed)
    retry_b = next(t for t in first if t.kind is TaskKind.failed)
    assert (old.subject_id, old.target_version, retry_b.subject_id) == (a, 2, b)
    await s.submit.submit(EID, rerun(retry_b.id, "D-RETRY-B"))
    await wait_idle(s.runner, EID)
    closed = await s.output_store.get(EID, ReviewTask, old.id)
    assert closed is not None and closed.status is TaskStatus.decided  # 同一轮, 随 B 一并关闭
    second = await run(s, "RUN-REV-02-2")
    assert second.status is RunStatus.failed
    await s.submit.submit(EID, rerun("T-RUN-REV-02-2-v1", "D-RETRY-ALL"))
    await wait_idle(s.runner, EID)
    third_run = await run(s, "RUN-REV-02-3")
    assert s.runner.run_error(EID) is None
    assert third_run.status is RunStatus.succeeded, third_run.failure_reason
    assert sorted(jctx.subject_id for _, jctx, _ in engines[0].calls) == [a, a, a, b, b, b]
    x = await s.output_store.get(EID, AuditException, X1.id)
    assert x is not None
    pending = await s.output_store.latest(EID, ReviewTask, procedure_id="REV-02", status=TaskStatus.pending)
    of_a = sorted((t.id, t.kind) for t in pending if t.subject_id == a or t.target_id in (X1.id, f"J-REV-02-3-{a}"))
    if third == "unattributed":
        assert (x.version, x.disposition) == (3, ExceptionDisposition.unattributed)
        assert of_a == [(f"T-{X1.id}-v3", TaskKind.unattributed)]
    elif third == "auto_close":
        assert (x.version, x.disposition, x.attributed_by) == (3, ExceptionDisposition.attributed, f"J-REV-02-3-{a}")
        assert of_a == []
    else:
        assert (x.version, x.disposition) == (2, ExceptionDisposition.unattributed)  # 确认判断时才归因
        assert of_a == [(f"T-J-REV-02-3-{a}-v1", TaskKind.normal)]


async def test_subject_decided_by_auditor_is_not_rerun() -> None:
    """需求 R347: 审计师想自己定 A, 先对 A 的 unattributed 任务作出决定 (推翻) 即可; 之后对 B rerun 只带还待决的,
    A 不重判 (判断引擎只多判 B)."""
    a, b = "SI-2026-0612", "SI-2026-0618"
    engines: list[FakeJudgmentEngine] = []
    s = await at_review(two_failures("normal", engines, a, b))
    first = await s.output_store.latest(EID, ReviewTask, procedure_id="REV-02", status=TaskStatus.pending)
    old = next(t for t in first if t.kind is TaskKind.unattributed)
    retry_b = next(t for t in first if t.kind is TaskKind.failed)
    override = record(
        old.id,
        Decision.override,
        id="D-OVERRIDE-A",
        target_version=old.target_version,
        override_conclusion="The invoice was recorded in the correct period.",
    )
    await s.submit.submit(EID, override)
    await wait_idle(s.runner, EID)
    before = len(s.output_store.commits)
    await s.submit.submit(EID, rerun(retry_b.id, "D-RETRY-B"))
    await wait_idle(s.runner, EID)
    [procedure] = [o for o in s.output_store.commits[before] if isinstance(o, Procedure)]
    assert procedure.rerun_subject_ids == [b]
    assert [jctx.subject_id for _, jctx, _ in engines[0].calls].count(a) == 1
    assert [jctx.subject_id for _, jctx, _ in engines[0].calls].count(b) == 2


@pytest.mark.parametrize("auto_close", [True, False])
async def test_fan_in_status_follows_outcome(auto_close: bool) -> None:
    """片段经 fan_in 并入: 自动关闭时程序 completed, 两条例外都 attributed; 交人时 awaiting_human, 只标第一条."""
    outcome = passed(autonomy_level=AutonomyLevel.L3) if auto_close else failed("unattributed")
    agent, ctx, _ = setup("REV-02", [Subject("A", {}, [X1, X2])], {"A": outcome}, exceptions=[X1, X2])
    rev08 = make_procedure("REV-08", Phase.P2_Executing, status=ProcedureStatus.completed)
    state = ctx.state | {
        "procedure_pack": [*ctx.state["procedure_pack"], rev08],
        "procedure_runs": [finished(make_run("REV-08"))],
    }
    ctx = RunContext(ctx.engagement, ctx.procedure, ctx.template, ctx.run, state)  # type: ignore[arg-type]
    await agent.deps.output_store.put(
        ctx.engagement.id, [*state["procedure_pack"], *state["procedure_runs"], X1, X2], actor=AgentId.A1
    )
    nodes = GraphNodes(agent.deps, parallel_cap=2)
    patch, writer = await nodes.run_agent(agent, ctx)
    assert writer is AgentId.A6
    staged = {"procedure_id": ctx.procedure.id, "run_id": ctx.run.id, "writer": writer, "patch": patch}
    merged = apply_patch(state, await nodes.fan_in(state | {"staged_results": [staged]}))  # type: ignore[arg-type]
    status = next(p.status for p in merged["procedure_pack"] if p.id == ctx.procedure.id)
    first, second = merged["exceptions"]
    if auto_close:
        assert status is ProcedureStatus.completed
        assert [(x.version, x.disposition) for x in (first, second)] == [(2, ExceptionDisposition.attributed)] * 2
    else:
        assert status is ProcedureStatus.awaiting_human
        assert (first.version, first.disposition) == (2, ExceptionDisposition.unattributed)
        assert second == X2


async def test_same_exception_for_two_subjects_rejected_by_merge_check() -> None:
    """两个对象共用同一例外时片段里会有同一例外的两个新版本, A1 合并校验拒绝 (现有程序的对象与例外一一对应)."""
    outcomes = {"A": passed(autonomy_level=AutonomyLevel.L3), "B": passed(autonomy_level=AutonomyLevel.L3)}
    agent, ctx, _ = setup("REV-02", [Subject("A", {}, [X1]), Subject("B", {}, [X1])], outcomes, exceptions=[X1])
    rev08 = make_procedure("REV-08", Phase.P2_Executing, status=ProcedureStatus.completed)
    state = ctx.state | {
        "procedure_pack": [*ctx.state["procedure_pack"], rev08],
        "procedure_runs": [finished(make_run("REV-08"))],
    }
    ctx = RunContext(ctx.engagement, ctx.procedure, ctx.template, ctx.run, state)  # type: ignore[arg-type]
    patch, writer = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
    assert writer is AgentId.A1 and "merge_rejected" in (patch["procedure_runs"][0].failure_reason or "")


# ── 合并校验 ─────────────────────────────────────────


async def test_patch_passes_merge_check() -> None:
    """A1 的合并校验接受这些片段: 写入者为 A6, 判断与任务的外键成立, 例外版本递增."""
    subjects = [Subject("SI-2026-0612", {}, [X1]), Subject("SI-2026-0618", {}), Subject("SI-0", {})]
    outcomes = {
        "SI-2026-0612": failed("unattributed"),
        "SI-2026-0618": passed(autonomy_level=AutonomyLevel.L3),
        "SI-0": passed(),
    }
    agent, ctx, _ = setup("REV-02", subjects, outcomes, exceptions=[X1])
    rev08 = make_procedure("REV-08", Phase.P2_Executing, status=ProcedureStatus.completed)
    state = ctx.state | {
        "procedure_pack": [*ctx.state["procedure_pack"], rev08],
        "procedure_runs": [finished(make_run("REV-08"))],
    }
    ctx = RunContext(ctx.engagement, ctx.procedure, ctx.template, ctx.run, state)  # type: ignore[arg-type]
    patch, writer = await GraphNodes(agent.deps, parallel_cap=2).run_agent(agent, ctx)
    assert writer is AgentId.A6, patch["procedure_runs"][0].failure_reason
