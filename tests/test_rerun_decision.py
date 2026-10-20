"""R21 rerun 决定: failed 任务的 rerun 把程序置 executing (合并 rerun_params, 记 rerun_subject_ids);
P2 程序由下一轮派发重跑, P2 以外的程序由所在节点重跑."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import pytest

from backend.agents.fieldwork import FieldworkAgent
from backend.contracts.errors import DecisionRejected, RuleError
from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    Decision,
    ExceptionDisposition,
    GateVerdict,
    Judgment,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ProposedAction,
    ReviewRecord,
    ReviewTask,
    RunStatus,
    RunTrigger,
    TaskKind,
    TaskStatus,
)
from backend.contracts.services import JudgmentContext, JudgmentOutcome
from backend.contracts.state import StatePatch
from backend.orchestration.decisions import rerun_group, rerun_procedure
from backend.orchestration.nodes import failed_tasks
from backend.orchestration.recovery import closed_by
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_CONTRACTS,
    NUMBAT_PROFILE,
    NUMBAT_RECORDS,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeJudgmentEngine,
    FakeTrail,
    ScriptA6,
    ScriptAgent,
    assert_pack_synced,
    make_procedure,
    make_runner,
    numbat_contracts,
    numbat_p2_agents,
    wait_idle,
)
from tests.test_decisions import Started, acc01_decision, pln_decision
from tests.test_fieldwork import p2_agents
from tests.test_rerun import failed_at_p0, pending_task
from tests.test_review_decisions import CONFIG, record

EID = ENGAGEMENT.id
Agents = Callable[[Any], dict[str, Any]]


def flaky_rev08(failures: int) -> Agents:
    """REV-08 前 failures 次执行抛 RuleError, 之后照 Numbat 脚本产出."""

    def agents(deps: Any) -> dict[str, Any]:
        numbat = numbat_p2_agents(deps)
        script = cast(ScriptAgent, numbat["REV-08"]).script

        def rev08(ctx: Any) -> StatePatch:
            if ctx.run.run_no <= failures:
                raise RuleError(f"第 {ctx.run.run_no} 次: missing column source_id")
            return script(ctx)

        return {**p2_agents(deps), **numbat, "REV-08": ScriptA6(deps, rev08)}

    return agents


def rev03_cannot_read(seen: list[list[str]], failing: tuple[str, ...] = ("CON-003",)) -> Agents:
    """REV-03 第 1 次对 failing 中的合同抽取失败 (各一个带 subject_id 的 failed 任务), 其余照常出判断与任务;
    重跑时只判 rerun_subject_ids 中的合同. seen 记下每次执行时 ctx.procedure.rerun_subject_ids."""

    def agents(deps: Any) -> dict[str, Any]:
        numbat = numbat_p2_agents(deps)
        script = cast(ScriptAgent, numbat["REV-03"]).script

        def rev03(ctx: Any) -> StatePatch:
            seen.append(list(ctx.procedure.rerun_subject_ids))
            patch = script(ctx)
            run = patch["procedure_runs"][0]
            only = set(ctx.procedure.rerun_subject_ids)
            judgments = [
                j for j in patch["judgments"] if (j.subject_id in only if only else j.subject_id not in failing)
            ]
            tasks = [t for t in patch["review_tasks"] if t.target_id in {j.id for j in judgments}]
            if not only:
                tasks += [
                    deps.queue.make_task(TaskKind.failed, run, "REV-03", run.id, now=NOW, subject_id=subject)
                    for subject in failing
                ]
            return {**patch, "judgments": judgments, "review_tasks": tasks}

        return {**p2_agents(deps), **numbat, "REV-03": ScriptA6(deps, rev03)}

    return agents


def rev03_cannot_read_con003(seen: list[list[str]]) -> Agents:
    """REV-03 第 1 次只有 CON-003 抽取失败, 见 rev03_cannot_read."""
    return rev03_cannot_read(seen)


async def at_review(agents: Agents, **runner_options: Any) -> Started:
    """Numbat 跑过两道闸门, P2 派完可派发的程序后停在 await_events. runner_options 原样转交 make_runner."""
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(engagement_store=engagement_store, agents=agents, **runner_options)
    s = Started(runner, store, engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await s.submit.submit(EID, pln_decision(GateVerdict.approve))
    await wait_idle(runner, EID)
    assert await s.next_nodes() == ("await_events",)
    return s


def rerun(task_id: str, decision_id: str, **params: Any) -> ReviewRecord:
    return record(task_id, Decision.rerun, id=decision_id, rerun_params=params or None)


async def run(s: Started, run_id: str) -> ProcedureRun:
    found = await s.output_store.get(EID, ProcedureRun, run_id)
    assert found is not None, run_id
    return found


async def test_rerun_with_params_writes_executing_version() -> None:
    """验收 1: REV-08 首次抛 RuleError; rerun 带 {window_days: 25}: 同一提交里 REV-08 新版本 executing,
    params {window_days: 25}, rerun_subject_ids 为空 (整次失败, 整条重跑), 任务 decided."""
    s = await at_review(flaky_rev08(1))
    before = len(s.output_store.commits)

    await s.submit.submit(EID, rerun("T-RUN-REV-08-1-v1", "D-R1", window_days=25))
    await wait_idle(s.runner, EID)

    commit = s.output_store.commits[before]
    [rev08] = [o for o in commit if isinstance(o, Procedure)]
    assert (rev08.id, rev08.status, rev08.params, rev08.rerun_subject_ids) == (
        "REV-08",
        ProcedureStatus.executing,
        {"window_days": 25},
        [],
    )
    [task] = [o for o in commit if isinstance(o, ReviewTask)]
    assert (task.id, task.status) == ("T-RUN-REV-08-1-v1", TaskStatus.decided)


async def test_rerun_dispatched_at_once_then_completes() -> None:
    """验收 2: 恢复后立即派发 RUN-REV-08-2 (trigger rerun, 实例 fw-0-1, 窗口 25 天), 不等 REV-03 的 3 个待决任务;
    汇合后 REV-08 completed, rerun_subject_ids 为空; 程序包与 Store 一致; 仍停在 await_events 等 REV-03."""
    s = await at_review(flaky_rev08(1))

    await s.submit.submit(EID, rerun("T-RUN-REV-08-1-v1", "D-R1", window_days=25))
    await wait_idle(s.runner, EID)

    second = await run(s, "RUN-REV-08-2")
    assert (second.trigger, second.instance, second.params_snapshot["window_days"], second.status) == (
        RunTrigger.rerun,
        "fw-0-1",
        25,
        RunStatus.succeeded,
    )
    snapshot = await s.runner.app.aget_state(CONFIG)
    pack = {p.id: p for p in snapshot.values["procedure_pack"]}
    assert (pack["REV-08"].status, pack["REV-08"].rerun_subject_ids) == (ProcedureStatus.completed, [])
    pending = [t.id for t in snapshot.values["review_tasks"] if t.status is TaskStatus.pending]
    assert sorted(pending) == [f"T-J-REV-03-1-CON-00{n}-v1" for n in (1, 2, 3)]
    assert snapshot.next == ("await_events",)
    await assert_pack_synced(snapshot.values["procedure_pack"], s.output_store)


async def test_rerun_of_one_subject() -> None:
    """验收 3: REV-03 对 CON-003 的 failed 任务 rerun: 新版本 executing, rerun_subject_ids [CON-003]
    (另两份合同的任务仍待决也是); 重跑时 Agent 看到 [CON-003], 执行记录 RUN-REV-03-2;
    汇合后清空, 按剩余待决任务 (另两份与 CON-003 的新判断) 重算为 awaiting_human."""
    seen: list[list[str]] = []
    s = await at_review(rev03_cannot_read_con003(seen))
    before = len(s.output_store.commits)

    await s.submit.submit(EID, rerun("T-RUN-REV-03-1-CON-003-v1", "D-R3"))
    await wait_idle(s.runner, EID)

    [rev03] = [o for o in s.output_store.commits[before] if isinstance(o, Procedure)]
    assert (rev03.status, rev03.rerun_subject_ids) == (ProcedureStatus.executing, ["CON-003"])
    assert seen == [[], ["CON-003"]]
    assert (await run(s, "RUN-REV-03-2")).trigger is RunTrigger.rerun
    snapshot = await s.runner.app.aget_state(CONFIG)
    final = {p.id: p for p in snapshot.values["procedure_pack"]}["REV-03"]
    assert (final.status, final.rerun_subject_ids) == (ProcedureStatus.awaiting_human, [])
    pending = sorted(t.id for t in snapshot.values["review_tasks"] if t.status is TaskStatus.pending)
    assert pending == ["T-J-REV-03-1-CON-001-v1", "T-J-REV-03-1-CON-002-v1", "T-J-REV-03-2-CON-003-v1"]
    await assert_pack_synced(snapshot.values["procedure_pack"], s.output_store)


async def test_rerun_closes_whole_group_in_p2() -> None:
    """Q17: REV-03 第 1 次 CON-002, CON-003 都抽取失败. 对 CON-003 的任务 rerun: 同一次提交里两个 failed 任务都
    decided, REV-03 executing, 重判对象为两份合同; 留痕列出一并关闭的任务; 重跑时 Agent 看到两份;
    之后对已关闭的 CON-002 任务再提交报 TASK_NOT_PENDING."""
    seen: list[list[str]] = []
    s = await at_review(rev03_cannot_read(seen, ("CON-002", "CON-003")))
    before = len(s.output_store.commits)

    await s.submit.submit(EID, rerun("T-RUN-REV-03-1-CON-003-v1", "D-R3"))
    await wait_idle(s.runner, EID)

    commit = s.output_store.commits[before]
    assert sorted((o.id, o.status) for o in commit if isinstance(o, ReviewTask)) == [
        ("T-RUN-REV-03-1-CON-002-v1", TaskStatus.decided),
        ("T-RUN-REV-03-1-CON-003-v1", TaskStatus.decided),
    ]
    [rev03] = [o for o in commit if isinstance(o, Procedure)]
    assert (rev03.status, rev03.rerun_subject_ids) == (ProcedureStatus.executing, ["CON-002", "CON-003"])
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    [entry] = [e for e in trail.entries if e.idempotency_key == "D-R3"]
    assert entry.payload["closed_task_ids"] == ["T-RUN-REV-03-1-CON-002-v1"]
    assert seen == [[], ["CON-002", "CON-003"]]
    snapshot = await s.runner.app.aget_state(CONFIG)
    pending = sorted(t.id for t in snapshot.values["review_tasks"] if t.status is TaskStatus.pending)
    assert pending == ["T-J-REV-03-1-CON-001-v1", "T-J-REV-03-2-CON-002-v1", "T-J-REV-03-2-CON-003-v1"]
    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, rerun("T-RUN-REV-03-1-CON-002-v1", "D-R2"))
    assert e.value.code == "TASK_NOT_PENDING"


async def test_rerun_fails_again_then_rerun_again() -> None:
    """验收 4: 重跑再失败, 得 T-RUN-REV-08-2-v1; 对它再 rerun, 得 RUN-REV-08-3 并完成."""
    s = await at_review(flaky_rev08(2))
    await s.submit.submit(EID, rerun("T-RUN-REV-08-1-v1", "D-R1"))
    await wait_idle(s.runner, EID)
    assert (await run(s, "RUN-REV-08-2")).status is RunStatus.failed
    task = await s.output_store.get(EID, ReviewTask, "T-RUN-REV-08-2-v1")
    assert task is not None and task.status is TaskStatus.pending

    await s.submit.submit(EID, rerun("T-RUN-REV-08-2-v1", "D-R2"))
    await wait_idle(s.runner, EID)

    assert (await run(s, "RUN-REV-08-3")).status is RunStatus.succeeded
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert {p.id: p for p in snapshot.values["procedure_pack"]}["REV-08"].status is ProcedureStatus.completed


async def test_rerun_in_p0_node_reaches_gate() -> None:
    """验收 5: 停在 p0_intel 等 ACC-04 失败的决定时经 submit 提交 rerun: 节点重跑得 RUN-ACC-04-2,
    图停在 ACC-01 闸门前."""
    runner, store, _ = await failed_at_p0()
    s = Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]

    await s.submit.submit(EID, rerun((await pending_task(store)).id, "D-ACC04"))
    await wait_idle(runner, EID)

    assert runner.run_error(EID) is None
    assert (await run(s, "RUN-ACC-04-2")).status is RunStatus.succeeded
    assert await s.next_nodes() == ("gate_acc01",)


def test_rerun_procedure() -> None:
    """rerun 写出的程序版本 (Q17 整组重跑): executing, 合并参数, 重判对象为整组任务的对象 (去重排序);
    组内有整次失败的任务 (无对象) 时整条重跑."""
    decision = rerun("T", "D", window_days=25)
    awaiting = make_procedure("REV-03", status=ProcedureStatus.awaiting_human).model_copy(
        update={"params": {"window_days": 20, "x": 1}, "rerun_subject_ids": []}
    )

    def tasks(*subjects: str | None) -> list[ReviewTask]:
        return [ReviewTask.model_construct(subject_id=s) for s in subjects]

    one = rerun_procedure(awaiting, decision, tasks("CON-003"))
    assert (one.version, one.status, one.params, one.rerun_subject_ids) == (
        awaiting.version + 1,
        ProcedureStatus.executing,
        {"window_days": 25, "x": 1},
        ["CON-003"],
    )
    assert rerun_procedure(awaiting, decision, tasks("CON-003", "CON-002", "CON-003")).rerun_subject_ids == [
        "CON-002",
        "CON-003",
    ]
    assert rerun_procedure(awaiting, decision, tasks(None)).rerun_subject_ids == []
    assert rerun_procedure(awaiting, decision, tasks("CON-003", None)).rerun_subject_ids == []


def test_rerun_group() -> None:
    """rerun 关闭的组: 被决定的任务在前, 其后为同程序其余待决的可重跑任务 (failed 与 unattributed, 需求 R347; 按 ID);
    别的程序的, normal 等不可重跑的不在内."""

    def task(tid: str, kind: TaskKind = TaskKind.failed, procedure_id: str = "REV-03") -> ReviewTask:
        return ReviewTask.model_construct(id=tid, kind=kind, procedure_id=procedure_id)

    decided = task("T-3")
    pending = [
        task("T-2"),
        decided,
        task("T-1"),
        task("T-U", TaskKind.unattributed),
        task("T-J", TaskKind.normal),
        task("T-D", TaskKind.dispute),
        task("T-X", procedure_id="REV-08"),
    ]

    assert [t.id for t in rerun_group(decided, pending)] == ["T-3", "T-1", "T-2", "T-U"]
    unattributed = task("T-U", TaskKind.unattributed)
    assert [t.id for t in rerun_group(unattributed, pending)] == ["T-U", "T-1", "T-2", "T-3"]


# ── 需求 R347: 带规则例外的对象判断失败 (unattributed) 也能重跑 ──

CUTOFF = "X-REV-08-1-R01-1"  # REV-08 的跨期例外, REV-02 判断 SI-2026-0612 的规则例外
SI0612 = "SI-2026-0612"


def passed(ctx: JudgmentContext, run: ProcedureRun) -> JudgmentOutcome:
    """判断引擎给出的判断: L2 accept, 解释对象的全部规则例外."""
    judgment = Judgment(
        id=f"J-{run.procedure_id}-{run.run_no}-{ctx.subject_id}",
        procedure_id=run.procedure_id,
        run_id=run.id,
        subject_id=ctx.subject_id,
        conclusion="ok",
        confidence=0.9,
        confidence_parts={"llm": 0.9},
        reasoning_chain=[],
        evidence=[],
        counter_argument="none",
        proposed_action=ProposedAction.accept,
        exception_ids=[x.id for x in ctx.rule_findings],
        autonomy_level=AutonomyLevel.L2,
        performed_by="U-1",
        created_at=NOW,
    )
    return JudgmentOutcome(judgment=judgment, failure=None, attempts=1, trail_ids=["TR-1"])


def fails_first(times: int) -> Callable[[JudgmentContext, ProcedureRun], JudgmentOutcome]:
    """前 times 次判断失败 (该对象有规则例外, 转 unattributed), 之后给出判断 (passed)."""
    left = [times]

    def outcome(ctx: JudgmentContext, run: ProcedureRun) -> JudgmentOutcome:
        if left[0] > 0:
            left[0] -= 1
            return JudgmentOutcome(judgment=None, failure="unattributed", attempts=3, trail_ids=["TR-1"])
        return passed(ctx, run)

    return outcome


def rev02_judged_by(engine: FakeJudgmentEngine) -> Agents:
    """REV-02 由真实的 A6 执行 (判断公共流程, 判断引擎为 engine), 其余同 Numbat 脚本."""

    def agents(deps: Any) -> dict[str, Any]:
        return {**p2_agents(deps), **numbat_p2_agents(deps), "REV-02": FieldworkAgent(replace(deps, judgments=engine))}

    return agents


async def unattributed_at_review(times: int) -> tuple[Started, FakeJudgmentEngine, ReviewTask]:
    """Numbat 停在 await_events, 复核完 REV-03 (三个 confirm) 后 REV-02 执行: 对 SI-2026-0612 的判断失败 times 次中
    的第 1 次已发生, 该对象有 REV-08 的跨期例外, 复核队列建以例外 v2 (unattributed) 为目标的任务."""
    engine = FakeJudgmentEngine({SI0612: fails_first(times), "SI-2026-0618": passed})
    financial = FakeFinancialStore(list(NUMBAT_CONTRACTS), NUMBAT_RECORDS, numbat_contracts())  # 发票, 发运单与合同
    s = await at_review(rev02_judged_by(engine), financial_store=financial)
    for n in (1, 2, 3):
        await s.submit.submit(EID, record(f"T-J-REV-03-1-CON-00{n}-v1", id=f"D-{n}"))
        await wait_idle(s.runner, EID)
    snapshot = await s.runner.app.aget_state(CONFIG)
    [task] = [
        t
        for t in snapshot.values["review_tasks"]
        if t.procedure_id == "REV-02" and t.kind is TaskKind.unattributed and t.status is TaskStatus.pending
    ]
    return s, engine, task


def judged(engine: FakeJudgmentEngine) -> list[str]:
    return [ctx.subject_id for pid, ctx, _ in engine.calls if pid == "REV-02"]


async def test_rerun_of_unattributed_subject_judges_it_again() -> None:
    """需求 R347 验收 1, 2: 带规则例外的对象判断失败, 任务为 unattributed (目标 X-REV-08-1-R01-1 v2), 允许
    override, misstatement 与 rerun. 对它 rerun: 同一提交里任务 decided, REV-02 executing, 重判对象只有 SI-2026-0612;
    重跑只判这一个对象 (判断引擎多 1 次调用), 成功后出 RUN-REV-02-2 的判断与 normal 任务. 确认这条判断时, 例外
    按判断归因出新版本 (v3 attributed, attributed_by 为新判断)."""
    s, engine, task = await unattributed_at_review(1)
    assert (task.id, task.target_id, task.target_version, task.subject_id) == (f"T-{CUTOFF}-v2", CUTOFF, 2, SI0612)
    assert s.runner.deps.queue.allowed(task) == frozenset({Decision.override, Decision.misstatement, Decision.rerun})
    assert sorted(judged(engine)) == [SI0612, "SI-2026-0618"]
    before = len(s.output_store.commits)

    await s.submit.submit(EID, rerun(task.id, "D-RU").model_copy(update={"target_version": task.target_version}))
    await wait_idle(s.runner, EID)

    commit = s.output_store.commits[before]
    assert [(o.id, o.status) for o in commit if isinstance(o, ReviewTask)] == [(task.id, TaskStatus.decided)]
    [rev02] = [o for o in commit if isinstance(o, Procedure)]
    assert (rev02.status, rev02.rerun_subject_ids) == (ProcedureStatus.executing, [SI0612])
    assert sorted(judged(engine)) == [SI0612, SI0612, "SI-2026-0618"]  # 只重判这一个对象
    assert (await run(s, "RUN-REV-02-2")).trigger is RunTrigger.rerun
    snapshot = await s.runner.app.aget_state(CONFIG)
    pending = sorted(
        (t.id, t.kind)
        for t in snapshot.values["review_tasks"]
        if t.procedure_id == "REV-02" and t.status is TaskStatus.pending
    )
    assert pending == [
        ("T-J-REV-02-1-SI-2026-0618-v1", TaskKind.normal),
        (f"T-J-REV-02-2-{SI0612}-v1", TaskKind.normal),
    ]
    judgment = await s.output_store.get(EID, Judgment, f"J-REV-02-2-{SI0612}")
    assert judgment is not None and judgment.exception_ids == [CUTOFF]

    confirm = record(f"T-J-REV-02-2-{SI0612}-v1", Decision.confirm, id="D-OK")
    await s.submit.submit(EID, confirm)
    await wait_idle(s.runner, EID)
    exception = await s.output_store.get(EID, AuditException, CUTOFF)
    assert exception is not None
    assert (exception.version, exception.disposition, exception.attributed_by) == (
        3,
        ExceptionDisposition.attributed,
        f"J-REV-02-2-{SI0612}",
    )


async def test_unattributed_rerun_fails_again_then_reruns_again() -> None:
    """需求 R347 验收 2 后半: 重跑后又失败, 又是一个 unattributed 任务 (目标为例外 v3), 同样可以 rerun; 再重跑成功时
    出 RUN-REV-02-3 的判断与 normal 任务. 后端不限重跑次数, 同 failed 任务 (每个对象至多重跑一次是演示驱动的规则)."""
    s, engine, task = await unattributed_at_review(2)
    await s.submit.submit(EID, rerun(task.id, "D-R1").model_copy(update={"target_version": 2}))
    await wait_idle(s.runner, EID)

    snapshot = await s.runner.app.aget_state(CONFIG)
    [again] = [
        t
        for t in snapshot.values["review_tasks"]
        if t.procedure_id == "REV-02" and t.kind is TaskKind.unattributed and t.status is TaskStatus.pending
    ]
    assert (again.id, again.target_version, again.run_id) == (f"T-{CUTOFF}-v3", 3, "RUN-REV-02-2")
    assert Decision.rerun in s.runner.deps.queue.allowed(again)

    await s.submit.submit(EID, rerun(again.id, "D-R2").model_copy(update={"target_version": 3}))
    await wait_idle(s.runner, EID)

    assert judged(engine).count(SI0612) == 3
    task3 = await s.output_store.get(EID, ReviewTask, f"T-J-REV-02-3-{SI0612}-v1")
    assert task3 is not None and (task3.kind, task3.status) == (TaskKind.normal, TaskStatus.pending)


def test_rerunnable_tasks_close_and_wait_as_one_group() -> None:
    """需求 R347: unattributed 与 failed 同属一轮失败的一组. 恢复补写留痕时 (recovery.closed_by) 随 rerun 一并关闭的
    同组任务含两类; 节点内等待 (P2 以外的程序) 时这一轮的组 (nodes.failed_tasks) 也含两类."""

    def task(
        tid: str, kind: TaskKind, run_id: str = "RUN-REV-02-1", status: TaskStatus = TaskStatus.decided
    ) -> ReviewTask:
        return ReviewTask.model_construct(id=tid, kind=kind, procedure_id="REV-02", run_id=run_id, status=status)

    decided = task("T-U", TaskKind.unattributed)
    tasks = [
        decided,
        task("T-F", TaskKind.failed),
        task("T-U2", TaskKind.unattributed),
        task("T-N", TaskKind.normal),
        task("T-OLD", TaskKind.failed, run_id="RUN-REV-02-0"),
        task("T-OWN", TaskKind.failed),
    ]
    records = [rerun("T-U", "D-1"), rerun("T-OWN", "D-2")]
    assert closed_by(records[0], decided, tasks, records) == ["T-F", "T-U2"]
    assert [t.id for t in failed_tasks({"review_tasks": tasks})] == ["T-U", "T-F", "T-U2", "T-OLD", "T-OWN"]
