"""R21 rerun 决定: failed 任务的 rerun 把程序置 executing (合并 rerun_params, 记 rerun_subject_ids);
P2 程序由下一轮派发重跑, P2 以外的程序由所在节点重跑."""

from collections.abc import Callable
from typing import Any, cast

import pytest

from backend.contracts.errors import DecisionRejected, RuleError
from backend.contracts.models import (
    Decision,
    GateVerdict,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ReviewRecord,
    ReviewTask,
    RunStatus,
    RunTrigger,
    TaskKind,
    TaskStatus,
)
from backend.contracts.state import StatePatch
from backend.orchestration.decisions import rerun_group, rerun_procedure
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeTrail,
    ScriptA6,
    ScriptAgent,
    assert_pack_synced,
    make_procedure,
    make_runner,
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


async def at_review(agents: Agents) -> Started:
    """Numbat 跑过两道闸门, P2 派完可派发的程序后停在 await_events."""
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(engagement_store=engagement_store, agents=agents)
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
    """rerun 关闭的组: 被决定的任务在前, 其后为同程序其余待决的 failed 任务 (按 ID); 别的程序的, 非 failed 的不在内."""

    def task(tid: str, kind: TaskKind = TaskKind.failed, procedure_id: str = "REV-03") -> ReviewTask:
        return ReviewTask.model_construct(id=tid, kind=kind, procedure_id=procedure_id)

    decided = task("T-3")
    pending = [task("T-2"), decided, task("T-1"), task("T-J", TaskKind.normal), task("T-X", procedure_id="REV-08")]

    assert [t.id for t in rerun_group(decided, pending)] == ["T-3", "T-1", "T-2"]
