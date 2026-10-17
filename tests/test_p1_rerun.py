"""R07 P1 节点内失败重跑: 节点里已成功的程序按重放取 Store, 多次 interrupt() 依次取回决定.

经 runner 与 DecisionSubmit 走到 P1; rerun 决定的效果由 preset_rerun 预置进 Store, 再以 Command(resume=决定 ID) 恢复.
"""

from typing import Any

from langgraph.types import Command

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.contracts.errors import RuleError
from backend.contracts.models import (
    AnalyticsResult,
    GateDecision,
    GateId,
    GateVerdict,
    MaterialityCandidates,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    ReviewTask,
    RunStatus,
    RunTrigger,
    TaskKind,
    TaskStatus,
)
from backend.contracts.state import StatePatch
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_CONTRACTS,
    DocumentWrite,
    FakeEvidenceStore,
    FakeFinancialStore,
    FakeOutputStore,
    ScriptA3,
    ScriptAgent,
    assert_pack_synced,
    finished,
    make_runner,
    numbat_p1_agents,
    preset_rerun,
    wait_idle,
)

EID = ENGAGEMENT.id
CONFIG = GraphRunner.config(EID)


def failing_first(failures: dict[str, int]) -> Any:
    """按 Numbat 脚本执行, 但 failures 中的程序前 n 次抛 RuleError. 返回给 make_runner 的 agents 工厂;
    造出的 Agent 记在工厂的 made 属性里, 以便测试读调用次数."""

    def factory(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
        made: dict[str, ProcedureAgent[Any]] = {}
        for pid, agent in numbat_p1_agents(deps).items():
            assert isinstance(agent, ScriptAgent)
            if pid not in failures:
                continue

            def script(ctx: RunContext, pid: str = pid, inner: Any = agent.script) -> StatePatch:
                if made[pid].calls <= failures[pid]:  # type: ignore[attr-defined]
                    raise RuleError(f"{pid}: SQL 超时")
                result: StatePatch = inner(ctx)
                return result

            made[pid] = type(agent)(deps, script)
        factory.made = made  # type: ignore[attr-defined]
        return made

    return factory


async def approved_into_p1(agents: Any) -> tuple[GraphRunner, FakeOutputStore]:
    """新项目跑到 ACC-01, 提交 approve, 后台跑 P1 直到停下."""
    runner, store = make_runner(agents=agents)
    await runner.start(EID)
    await wait_idle(runner, EID)
    submit = GraphDecisionSubmit(runner.app, runner.deps, runner)
    approve = GateDecision(
        id="D-ACC",
        gate=GateId.ACC_01,
        target_id="SCORECARD",
        target_version=1,
        decision=GateVerdict.approve,
        decided_by="U-001",
        decided_at=NOW,
    )
    await submit.submit(EID, approve)
    await wait_idle(runner, EID)
    assert runner.run_error(EID) is None
    return runner, store


async def waiting_on(runner: GraphRunner) -> list[tuple[str, str]]:
    """图停在哪个节点内等待, 等的是哪次执行: [(节点名, 执行记录 ID)]."""
    snapshot = await runner.app.aget_state(CONFIG)
    return [(t.name, i.value["run_id"]) for t in snapshot.tasks for i in t.interrupts]


async def only_pending(store: FakeOutputStore) -> ReviewTask:
    [task] = await store.latest(EID, ReviewTask, status=TaskStatus.pending)
    return task


# ── 验收 1: PLN-01 失败, PLN-07 已提交 ──


async def test_pln01_fails_then_rerun_without_rerunning_pln07() -> None:
    agents = failing_first({"PLN-01": 1, "PLN-07": 0})
    runner, store = await approved_into_p1(agents)
    pln07, pln01 = agents.made["PLN-07"], agents.made["PLN-01"]

    assert await waiting_on(runner) == [("p1_analytics", "RUN-PLN-01-1")]
    run07 = await store.get(EID, ProcedureRun, "RUN-PLN-07-1")
    assert run07 is not None and run07.status is RunStatus.succeeded
    assert (await store.get(EID, AnalyticsResult, "ANALYTICS")) is not None
    run01 = await store.get(EID, ProcedureRun, "RUN-PLN-01-1")
    assert run01 is not None and run01.status is RunStatus.failed
    assert run01.failure_reason == "RuleError: PLN-01: SQL 超时"

    await preset_rerun(store, await only_pending(store), "D-1")
    await runner.app.ainvoke(Command(resume="D-1"), CONFIG)

    snapshot = await runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_p1_risk",)
    assert (pln07.calls, pln01.calls) == (1, 2)  # PLN-07 恢复时走重放
    rerun = await store.get(EID, ProcedureRun, "RUN-PLN-01-2")
    assert rerun is not None and (rerun.trigger, rerun.status) == (RunTrigger.rerun, RunStatus.succeeded)
    values = snapshot.values
    assert (values["analytics"].id, values["analytics"].version) == ("ANALYTICS", 1)
    assert (values["materiality_candidates"].id, values["materiality_candidates"].version) == ("MATERIALITY", 1)
    assert await store.get(EID, MaterialityCandidates, "MATERIALITY") == values["materiality_candidates"]
    assert [r.id for r in values["review_records"]] == ["D-1"]
    await assert_pack_synced(values["procedure_pack"], store)


# ── 验收 2: DOC-04 单份合同失败 ──


async def test_doc04_one_contract_fails_then_rerun_only_that_contract() -> None:
    seen: list[list[str]] = []

    def factory(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
        def doc04(ctx: RunContext) -> StatePatch:
            subjects = ctx.procedure.rerun_subject_ids
            seen.append(list(subjects))
            run = finished(ctx.run)
            if not subjects:  # 第 1 次: CON-002 抽取失败, 另两份抽出
                docs = [
                    d.model_copy(update={"status": "extracted"})
                    for d in NUMBAT_CONTRACTS
                    if d.file_name != "CON-002.pdf"
                ]
                task = deps.queue.make_task(TaskKind.failed, run, "DOC-04", run.id, NOW, subject_id="CON-002")
                return {"documents": docs, "procedure_runs": [run], "review_tasks": [task]}
            docs = [
                d.model_copy(update={"status": "extracted"}) for d in NUMBAT_CONTRACTS if d.file_name == "CON-002.pdf"
            ]
            return {"documents": docs, "procedure_runs": [run]}

        return {"DOC-04": ScriptA3(deps, doc04)}

    runner, store = await approved_into_p1(factory)

    assert await waiting_on(runner) == [("p1_ingest", "RUN-DOC-04-1")]
    task = await only_pending(store)
    assert (task.id, task.subject_id) == ("T-RUN-DOC-04-1-CON-002-v1", "CON-002")
    run1 = await store.get(EID, ProcedureRun, "RUN-DOC-04-1")
    assert run1 is not None and run1.status is RunStatus.succeeded  # 单对象失败不算整次失败
    financial, evidence = runner.deps.financial_store, runner.deps.evidence_store
    assert isinstance(financial, FakeFinancialStore) and isinstance(evidence, FakeEvidenceStore)
    con1, con2, con3 = (d.id for d in NUMBAT_CONTRACTS)
    assert {d.id: d.status for d in await financial.documents(EID)} == {
        con1: "extracted",
        con2: "received",
        con3: "extracted",
    }
    evidence.calls.clear()

    await preset_rerun(store, task, "D-1")
    await runner.app.ainvoke(Command(resume="D-1"), CONFIG)

    assert seen == [[], ["CON-002"]]
    snapshot = await runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_p1_risk",)  # 已进入 p1_analytics 并走完
    doc04 = await store.get(EID, Procedure, "DOC-04")
    assert doc04 is not None and (doc04.status, doc04.rerun_subject_ids) == (ProcedureStatus.completed, [])
    assert await store.get(EID, ProcedureRun, "RUN-DOC-04-2") is not None
    assert sorted((d.id, d.status) for d in snapshot.values["documents"]) == sorted(
        (d.id, "extracted") for d in NUMBAT_CONTRACTS
    )
    # 存储中的文件状态与图状态一致. 恢复时: 重放第 1 次取回全部文件, 对已 extracted 的再写一次同值 (幂等);
    # 第 2 次只写 CON-002; 全部在事务内
    assert [(d.id, d.status) for d in await financial.documents(EID)] == [(d.id, "extracted") for d in NUMBAT_CONTRACTS]
    assert evidence.calls == [
        (con1, "extracted", True),
        (con2, "received", True),
        (con3, "extracted", True),
        (con2, "extracted", True),
    ]
    [rerun_commit] = [c for c in store.commits if any(getattr(o, "id", None) == "RUN-DOC-04-2" for o in c)]
    assert DocumentWrite(con2, "extracted") in rerun_commit
    await assert_pack_synced(snapshot.values["procedure_pack"], store)


# ── 验收 3: 同一节点先后两条程序失败, 两次等待 ──


async def test_pln07_then_pln01_fail_two_waits_in_one_node() -> None:
    agents = failing_first({"PLN-07": 1, "PLN-01": 1})
    runner, store = await approved_into_p1(agents)
    pln07, pln01 = agents.made["PLN-07"], agents.made["PLN-01"]
    assert await waiting_on(runner) == [("p1_analytics", "RUN-PLN-07-1")]

    await preset_rerun(store, await only_pending(store), "D-1")
    await runner.app.ainvoke(Command(resume="D-1"), CONFIG)

    assert await waiting_on(runner) == [("p1_analytics", "RUN-PLN-01-1")]  # PLN-07 重跑成功, PLN-01 又失败
    assert (pln07.calls, pln01.calls) == (2, 1)

    await preset_rerun(store, await only_pending(store), "D-2")
    await runner.app.ainvoke(Command(resume="D-2"), CONFIG)

    # 恢复 D-2 时节点从头执行: PLN-07 两次, PLN-01 第 1 次都走重放, 两个 interrupt() 依次取回 D-1, D-2
    assert (pln07.calls, pln01.calls) == (2, 2)
    snapshot = await runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_p1_risk",)
    values = snapshot.values
    runs = {r.id: r for r in values["procedure_runs"]}
    assert {"RUN-PLN-07-1", "RUN-PLN-07-2", "RUN-PLN-01-1", "RUN-PLN-01-2"} <= set(runs)
    assert runs["RUN-PLN-07-2"].trigger is RunTrigger.rerun and runs["RUN-PLN-01-2"].trigger is RunTrigger.rerun
    assert [r.id for r in values["review_records"]] == ["D-1", "D-2"]
    assert [x.id for x in values["exceptions"]] == ["X-PLN-07-2-A01-1"]  # 第 2 次 PLN-07 的例外
    assert values["analytics"].exception_ids == ["X-PLN-07-2-A01-1"]
    await assert_pack_synced(values["procedure_pack"], store)
