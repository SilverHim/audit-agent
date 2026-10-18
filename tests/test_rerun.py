"""R05 P2 以外程序失败等待时, 收到 rerun 决定后在节点内重跑.

决定的效果由 tests/fakes.py 的 preset_rerun 预置进 Store.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from langgraph.types import Command

from backend.agents.base import RunContext
from backend.contracts.models import (
    AbnRecord,
    Decision,
    Phase,
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
from backend.contracts.state import StatePatch, initial_state
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    FakeOutputStore,
    FakeRegistry,
    ScriptA3,
    ScriptA4,
    assert_pack_synced,
    finished,
    make_procedure,
    make_runner,
    preset_rerun,
    wait_idle,
)
from tests.test_nodes import make_nodes, single_node

EID = ENGAGEMENT.id
CONFIG = GraphRunner.config(EID)


class FlakyRegistry(FakeRegistry):
    """前 failures 次查 ABN 抛错, 之后正常. calls 为查询次数."""

    def __init__(self, failures: int = 1) -> None:
        super().__init__({NUMBAT_ABN.abn: NUMBAT_ABN}, {NUMBAT_ASIC.acn: NUMBAT_ASIC})
        self.failures = failures  # 前几次失败
        self.calls = 0  # ABN 查询次数

    async def abn(self, eid: str, abn: str) -> AbnRecord | None:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(f"登记系统第 {self.calls} 次不可用")
        return await super().abn(eid, abn)


async def failed_at_p0(failures: int = 1) -> tuple[GraphRunner, FakeOutputStore, FlakyRegistry]:
    """新项目启动, ACC-04 第 1 次失败, 停在 p0_intel 节点内等待."""
    registry = FlakyRegistry(failures)
    runner, store = make_runner(registry=registry)
    await runner.start(EID)
    await wait_idle(runner, EID)
    return runner, store, registry


async def pending_task(store: FakeOutputStore) -> ReviewTask:
    [task] = await store.latest(EID, ReviewTask, status=TaskStatus.pending)
    return task


def versions(rows: list[object], procedure_id: str) -> list[tuple[int, ProcedureStatus]]:
    return [(r.version, r.status) for r in rows if isinstance(r, Procedure) and r.id == procedure_id]


# ── 验收 1, 2: 一次 rerun 决定后重跑成功 ──


async def test_rerun_decision_reruns_acc04_and_reaches_gate() -> None:
    runner, store, registry = await failed_at_p0()
    await preset_rerun(store, await pending_task(store))

    await runner.app.ainvoke(Command(resume="D-1"), CONFIG)

    snapshot = await runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_acc01",)
    values = snapshot.values
    runs = values["procedure_runs"]
    assert [(r.id, r.status, r.trigger) for r in runs] == [
        ("RUN-ACC-04-1", RunStatus.failed, RunTrigger.initial),
        ("RUN-ACC-04-2", RunStatus.succeeded, RunTrigger.rerun),
    ]
    assert [r.id for r in values["review_records"]] == ["D-1"]
    assert [(t.id, t.version, t.status) for t in values["review_tasks"]] == [
        ("T-RUN-ACC-04-1-v1", 2, TaskStatus.decided)
    ]
    assert values["acceptance_scorecard"].version == 1
    assert versions(store.rows, "ACC-04") == [
        (1, ProcedureStatus.planned),
        (2, ProcedureStatus.awaiting_human),
        (3, ProcedureStatus.executing),
        (4, ProcedureStatus.completed),
    ]
    await assert_pack_synced(values["procedure_pack"], store)
    assert registry.calls == 2  # 第 1 次失败的执行按重放取 Store, 恢复时不再查 (验收 4)


# ── 验收 3: 重跑再失败, 再决定 ──


async def test_rerun_fails_again_then_second_decision() -> None:
    runner, store, registry = await failed_at_p0(failures=2)
    await preset_rerun(store, await pending_task(store), "D-1")

    await runner.app.ainvoke(Command(resume="D-1"), CONFIG)

    run2 = await store.get(EID, ProcedureRun, "RUN-ACC-04-2")
    assert run2 is not None and run2.status is RunStatus.failed
    second = await pending_task(store)
    assert (second.id, second.run_id) == ("T-RUN-ACC-04-2-v1", "RUN-ACC-04-2")
    snapshot = await runner.app.aget_state(CONFIG)
    assert [i.value["run_id"] for t in snapshot.tasks for i in t.interrupts] == ["RUN-ACC-04-2"]

    await preset_rerun(store, second, "D-2")
    await runner.app.ainvoke(Command(resume="D-2"), CONFIG)

    snapshot = await runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_acc01",)
    assert [r.id for r in snapshot.values["procedure_runs"]] == ["RUN-ACC-04-1", "RUN-ACC-04-2", "RUN-ACC-04-3"]
    assert [r.id for r in snapshot.values["review_records"]] == ["D-1", "D-2"]
    # 恢复 D-2 时节点从头重放, 经过第 1 次等待时程序版本取 D-1 写出的 v3, 不取最新: 不多出版本
    assert versions(store.rows, "ACC-04") == [
        (1, ProcedureStatus.planned),
        (2, ProcedureStatus.awaiting_human),
        (3, ProcedureStatus.executing),
        (4, ProcedureStatus.awaiting_human),
        (5, ProcedureStatus.executing),
        (6, ProcedureStatus.completed),
    ]
    await assert_pack_synced(snapshot.values["procedure_pack"], store)
    assert registry.calls == 3


# ── 验收 4: 节点从头重放 ──


class CrashAfterCommit(FakeOutputStore):
    """crash_run 那次执行的事务提交后抛一次错, 模拟进程在写 Store 之后, 写检查点之前被杀."""

    def __init__(self, crash_run: str) -> None:
        super().__init__()
        self.crash_run = crash_run  # 写到这次执行时出事
        self.crashed = False

    @asynccontextmanager
    async def transaction(self, eid: str) -> AsyncIterator[None]:
        async with super().transaction(eid):
            yield
        if not self.crashed and any(getattr(o, "id", None) == self.crash_run for o in self.commits[-1]):
            self.crashed = True
            raise RuntimeError("进程在写检查点前被杀")


async def test_crash_after_rerun_committed_replays_both_runs_from_store() -> None:
    """重跑的结果已写 Store, 检查点没跟上; 再继续时节点从头执行: 两次执行都取 Store, 第一次 interrupt() 直接取回
    已提交的 D-1, 登记不再查, Store 无新增, 走到 ACC-01 闸门."""
    registry = FlakyRegistry()
    store = CrashAfterCommit("RUN-ACC-04-2")
    runner, _ = make_runner(registry=registry, output_store=store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await preset_rerun(store, await pending_task(store))
    with pytest.raises(RuntimeError, match="被杀"):
        await runner.app.ainvoke(Command(resume="D-1"), CONFIG)
    assert await store.get(EID, ProcedureRun, "RUN-ACC-04-2") is not None
    rows, calls = list(store.rows), registry.calls

    await runner.app.ainvoke(None, CONFIG)

    snapshot = await runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_acc01",)
    assert registry.calls == calls == 2
    assert store.rows == rows
    assert [r.id for r in snapshot.values["review_records"]] == ["D-1"]
    await assert_pack_synced(snapshot.values["procedure_pack"], store)


# ── 验收 5: 重跑参数与只重判的对象 ──


async def test_rerun_params_merged_into_snapshot() -> None:
    """PLN-07 第 1 次失败, rerun 决定改 change_pct: 第 2 次的参数快照用新值, 其余参数照模板."""
    pack = [make_procedure("PLN-07", Phase.P1_Planning)]
    store = FakeOutputStore(list(pack))
    nodes = make_nodes(output_store=store)
    seen: list[RunContext] = []

    def fail_then_pass(ctx: RunContext) -> StatePatch:
        seen.append(ctx)
        if len(seen) == 1:
            raise RuntimeError("试算表还没载入")
        return {"procedure_runs": [finished(ctx.run)]}

    nodes.agents["PLN-07"] = ScriptA4(nodes.deps, fail_then_pass)
    app = single_node(nodes, "PLN-07")
    await app.ainvoke(initial_state(ENGAGEMENT, pack, 1), CONFIG)
    await preset_rerun(store, await pending_task(store), rerun_params={"change_pct": 0.1})

    await app.ainvoke(Command(resume="D-1"), CONFIG)

    first, second = (ctx.run.params_snapshot for ctx in seen)
    assert first["change_pct"] == 0.2
    assert second == {**first, "change_pct": 0.1}
    assert seen[1].procedure.params == {"change_pct": 0.1}


async def test_rerun_subject_ids_passed_to_agent_then_cleared() -> None:
    """DOC-04 第 1 次 CON-002 抽取失败: 第 2 次 ctx.procedure.rerun_subject_ids 为 [CON-002], 成功后的新版本清空."""
    pack = [make_procedure("DOC-04", Phase.P1_Planning)]
    store = FakeOutputStore(list(pack))
    nodes = make_nodes(output_store=store)
    seen: list[RunContext] = []

    def one_contract_fails_once(ctx: RunContext) -> StatePatch:
        seen.append(ctx)
        run = finished(ctx.run)
        if len(seen) > 1:
            return {"procedure_runs": [run]}
        task = nodes.deps.queue.make_task(TaskKind.failed, run, "DOC-04", run.id, NOW, subject_id="CON-002")
        return {"procedure_runs": [run], "review_tasks": [task]}

    nodes.agents["DOC-04"] = ScriptA3(nodes.deps, one_contract_fails_once)
    app = single_node(nodes, "DOC-04")
    await app.ainvoke(initial_state(ENGAGEMENT, pack, 1), CONFIG)
    await preset_rerun(store, await pending_task(store))

    result = await app.ainvoke(Command(resume="D-1"), CONFIG)

    assert [ctx.procedure.rerun_subject_ids for ctx in seen] == [[], ["CON-002"]]
    final = result["procedure_pack"][0]
    assert (final.version, final.status, final.rerun_subject_ids) == (4, ProcedureStatus.completed, [])
    await assert_pack_synced(result["procedure_pack"], store)


# ── 验收 6: 决定不对 ──


@pytest.mark.parametrize(
    ("record", "error", "match"),
    [
        (None, LookupError, "D-1 not found"),
        ({"decision": Decision.manual_result}, ValueError, "只能 rerun"),
        ({"task_id": "T-OTHER-v1"}, ValueError, "不是 ACC-04 待处理的失败任务"),
    ],
    ids=["missing", "not_rerun", "other_task"],
)
async def test_wrong_decision_is_a_node_error(
    record: dict[str, object] | None, error: type[Exception], match: str
) -> None:
    """读回的决定不存在, 不是 rerun, 或不针对这次失败的任务: 节点出错, 不重跑."""
    runner, store, registry = await failed_at_p0()
    if record is not None:
        written = await preset_rerun(store, await pending_task(store))
        store.rows_of(EID).remove(written)
        store.rows_of(EID).append(written.model_copy(update=record))

    with pytest.raises(error, match=match):
        await runner.app.ainvoke(Command(resume="D-1"), CONFIG)

    assert registry.calls == 1
    assert await store.get(EID, ProcedureRun, "RUN-ACC-04-2") is None


@pytest.mark.parametrize("v3", ["missing", "completed"])
async def test_rerun_without_executing_version_is_a_node_error(v3: str) -> None:
    """决定写了, 但程序没出 executing 新版本 (缺失, 或那一版不是 executing): 决定的效果没写对, 不重跑."""
    runner, store, registry = await failed_at_p0()
    await preset_rerun(store, await pending_task(store))
    rows = store.rows_of(EID)
    i = next(i for i, r in enumerate(rows) if isinstance(r, Procedure) and r.version == 3)
    if v3 == "missing":
        del rows[i]
    else:
        rows[i] = rows[i].model_copy(update={"status": ProcedureStatus.completed})

    with pytest.raises(ValueError, match="v3 executing"):
        await runner.app.ainvoke(Command(resume="D-1"), CONFIG)
    assert registry.calls == 1


# ── Q17: 一次失败留下多个任务算一组, 对其一 rerun 即整组关闭, 整组重跑 (原 Codex R05-01 为逐个决定) ──


def doc04_node(script_for_run: dict[int, list[str]]) -> tuple[FakeOutputStore, object, list[RunContext]]:
    """DOC-04 单节点图: 第 n 次执行按 script_for_run[n] 列出的合同失败 (单对象 failed 任务), 其余成功.

    返回 Store, 图与各次执行收到的上下文.
    """
    pack = [make_procedure("DOC-04", Phase.P1_Planning)]
    store = FakeOutputStore(list(pack))
    nodes = make_nodes(output_store=store)
    seen: list[RunContext] = []

    def script(ctx: RunContext) -> StatePatch:
        seen.append(ctx)
        run = finished(ctx.run)
        tasks = [
            nodes.deps.queue.make_task(TaskKind.failed, run, "DOC-04", run.id, NOW, subject_id=subject)
            for subject in script_for_run.get(ctx.run.run_no, [])
        ]
        return {"procedure_runs": [run], "review_tasks": tasks}

    nodes.agents["DOC-04"] = ScriptA3(nodes.deps, script)
    return store, single_node(nodes, "DOC-04"), seen


async def test_two_contracts_failed_rerun_together() -> None:
    """第 1 次 CON-001, CON-002 都失败. 对 CON-001 决定 D-1: 同一次提交里两个任务都 decided, 程序 executing,
    重判对象为两份合同; 恢复后一起重跑, 都成功, completed, 不再等待."""
    store, app, seen = doc04_node({1: ["CON-001", "CON-002"]})
    await app.ainvoke(initial_state(ENGAGEMENT, [make_procedure("DOC-04", Phase.P1_Planning)], 1), CONFIG)
    first, _ = sorted(await store.latest(EID, ReviewTask, status=TaskStatus.pending), key=lambda t: t.id)
    commits = len(store.commits)
    await preset_rerun(store, first, "D-1")
    [commit] = store.commits[commits:]
    assert sorted((o.id, o.status) for o in commit if isinstance(o, ReviewTask)) == [
        ("T-RUN-DOC-04-1-CON-001-v1", TaskStatus.decided),
        ("T-RUN-DOC-04-1-CON-002-v1", TaskStatus.decided),
    ]

    result = await app.ainvoke(Command(resume="D-1"), CONFIG)

    assert "__interrupt__" not in result
    assert [ctx.procedure.rerun_subject_ids for ctx in seen] == [[], ["CON-001", "CON-002"]]
    assert [r.id for r in result["review_records"]] == ["D-1"]
    assert versions(store.rows, "DOC-04") == [
        (1, ProcedureStatus.planned),
        (2, ProcedureStatus.awaiting_human),
        (3, ProcedureStatus.executing),
        (4, ProcedureStatus.completed),
    ]
    await assert_pack_synced(result["procedure_pack"], store)


async def test_each_decision_targets_the_latest_run() -> None:
    """第 1 次 CON-001, CON-002 失败, D-1 整组重跑, 第 2 次只有 CON-001 又失败: 唯一待决任务属 RUN-DOC-04-2;
    D-2 只重跑 CON-001, 成功后 completed. 第 k 个决定针对第 k 次执行."""
    store, app, seen = doc04_node({1: ["CON-001", "CON-002"], 2: ["CON-001"]})
    await app.ainvoke(initial_state(ENGAGEMENT, [make_procedure("DOC-04", Phase.P1_Planning)], 1), CONFIG)
    first, _ = sorted(await store.latest(EID, ReviewTask, status=TaskStatus.pending), key=lambda t: t.id)
    await preset_rerun(store, first, "D-1")
    await app.ainvoke(Command(resume="D-1"), CONFIG)

    [second] = await store.latest(EID, ReviewTask, status=TaskStatus.pending)
    assert (second.run_id, second.subject_id) == ("RUN-DOC-04-2", "CON-001")
    await preset_rerun(store, second, "D-2")
    result = await app.ainvoke(Command(resume="D-2"), CONFIG)

    assert [ctx.procedure.rerun_subject_ids for ctx in seen] == [[], ["CON-001", "CON-002"], ["CON-001"]]
    assert result["procedure_pack"][0].status is ProcedureStatus.completed
    await assert_pack_synced(result["procedure_pack"], store)


async def test_decision_on_earlier_round_task_is_a_node_error() -> None:
    """第 2 次等待时送来针对第 1 次执行任务的 rerun 决定 (绕过正式入口): 不是这一轮的失败任务, 节点出错, 不重跑."""
    store, app, _ = doc04_node({1: ["CON-001", "CON-002"], 2: ["CON-001"]})
    await app.ainvoke(initial_state(ENGAGEMENT, [make_procedure("DOC-04", Phase.P1_Planning)], 1), CONFIG)
    first, second = sorted(await store.latest(EID, ReviewTask, status=TaskStatus.pending), key=lambda t: t.id)
    await preset_rerun(store, first, "D-1")
    await app.ainvoke(Command(resume="D-1"), CONFIG)
    stale = ReviewRecord(
        id="D-OLD",
        task_id=second.id,
        target_version=second.target_version,
        decision=Decision.rerun,
        reason="x",
        reviewed_by="U-1",
        decided_at=NOW,
    )
    await store.put(EID, [stale], actor="U-1")

    with pytest.raises(ValueError, match="T-RUN-DOC-04-1-CON-002-v1 不是 DOC-04 待处理的失败任务"):
        await app.ainvoke(Command(resume="D-OLD"), CONFIG)

    assert await store.get(EID, ProcedureRun, "RUN-DOC-04-3") is None


async def test_rerun_not_covering_whole_group_is_a_node_error() -> None:
    """决定写出的程序版本只重判组内一个对象 (没有整组关闭): 节点出错, 不重跑, 以免只重跑一部分就置 completed."""
    store, app, _ = doc04_node({1: ["CON-001", "CON-002"]})
    await app.ainvoke(initial_state(ENGAGEMENT, [make_procedure("DOC-04", Phase.P1_Planning)], 1), CONFIG)
    first, _ = sorted(await store.latest(EID, ReviewTask, status=TaskStatus.pending), key=lambda t: t.id)
    await preset_rerun(store, first, "D-1")
    rows = store.rows_of(EID)
    i = next(i for i, r in enumerate(rows) if isinstance(r, Procedure) and r.version == 3)
    rows[i] = rows[i].model_copy(update={"rerun_subject_ids": ["CON-001"]})

    with pytest.raises(ValueError, match="应覆盖整组"):
        await app.ainvoke(Command(resume="D-1"), CONFIG)

    assert await store.get(EID, ProcedureRun, "RUN-DOC-04-2") is None


async def test_reused_decision_at_second_wait_is_skipped() -> None:
    """第 2 次等待时以 ainvoke(None) 继续, LangGraph 把 D-1 再给下一个 interrupt(): D-1 已读回过, 节点跳过它接着等,
    不重跑, Store 无新增, 仍等第 2 次失败的决定; 之后提交 D-2 照常重跑 (R31)."""
    runner, store, registry = await failed_at_p0(failures=2)
    await preset_rerun(store, await pending_task(store), "D-1")
    await runner.app.ainvoke(Command(resume="D-1"), CONFIG)
    rows = list(store.rows)

    await runner.app.ainvoke(None, CONFIG)

    snapshot = await runner.app.aget_state(CONFIG)
    assert [i.value for t in snapshot.tasks for i in t.interrupts] == [
        {"awaiting": "human", "procedure_id": "ACC-04", "run_id": "RUN-ACC-04-2", "records": ["D-1"]}
    ]
    assert registry.calls == 2 and store.rows == rows
    await preset_rerun(store, await pending_task(store), "D-2")
    await runner.app.ainvoke(Command(resume="D-2"), CONFIG)
    assert (await runner.app.aget_state(CONFIG)).next == ("gate_acc01",) and registry.calls == 3


async def test_later_round_decision_at_earlier_wait_is_a_node_error() -> None:
    """ACC-04 失败两次, D-1, D-2 依次决定后, 复制检查点让节点从头执行, 却先送来 D-2: 第 1 次等待只认第 1 轮的
    失败任务, D-2 针对第 2 轮, 节点出错, 不多执行."""
    runner, store, registry = await failed_at_p0(failures=2)
    await preset_rerun(store, await pending_task(store), "D-1")
    await runner.app.ainvoke(Command(resume="D-1"), CONFIG)
    await preset_rerun(store, await pending_task(store), "D-2")
    await runner.app.aupdate_state(CONFIG, None, as_node="__copy__")

    with pytest.raises(ValueError, match="T-RUN-ACC-04-2-v1 不是 ACC-04 待处理的失败任务"):
        await runner.app.ainvoke(Command(resume="D-2"), CONFIG)

    assert registry.calls == 2
