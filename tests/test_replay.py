"""R04 重放: 节点写 Store 后, 写检查点前中断, 恢复时重跑. Store 中本次执行已为终态时不调 Agent, 产出从 Store 取回."""

import pytest
from langgraph.types import Command

from backend.contracts.models import (
    AbnRecord,
    AcceptanceScorecard,
    AgentId,
    AnalyticsResult,
    AuditException,
    AutonomyLevel,
    Confirmation,
    DocumentRef,
    DocumentType,
    ExceptionDisposition,
    Judgment,
    MaterialityCandidates,
    ProcedureRun,
    ProcedureStatus,
    ProposedAction,
    Reliability,
    ReviewStatus,
    RiskItem,
    TaskKind,
    TaskStatus,
    TrailKind,
)
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.merge import check_refs
from backend.orchestration.nodes import GraphNodes, apply_patch, fail_run, stored_outputs, update_procedure
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_OBJECTIVES,
    NUMBAT_PROFILE,
    TYPO_PROFILE,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeOutputStore,
    FakeRegistry,
    FakeTrail,
    ScriptA6,
    ScriptAgent,
    finished,
    make_deps,
    make_run,
    make_runner,
    wait_idle,
)
from tests.test_fieldwork import assembled, dispatch
from tests.test_nodes import p0_pack, p0_state, single_node

EID = ENGAGEMENT.id


class CountingRegistry(FakeRegistry):
    """Numbat 的登记记录, 记下 ABN 查询次数."""

    def __init__(self) -> None:
        super().__init__({NUMBAT_ABN.abn: NUMBAT_ABN}, {NUMBAT_ASIC.acn: NUMBAT_ASIC})
        self.abn_calls = 0  # ABN 查询次数

    async def abn(self, abn: str) -> AbnRecord | None:
        self.abn_calls += 1
        return await super().abn(abn)


def counted_nodes(store: FakeOutputStore) -> tuple[GraphNodes, CountingRegistry, FakeTrail]:
    """名称写错的画像 (ACC-04 出一条 name_match 例外), 登记与留痕都计数. 返回节点, 登记系统与留痕."""
    registry, trail = CountingRegistry(), FakeTrail()
    engagements = FakeEngagementStore([TYPO_PROFILE], [ENGAGEMENT])
    return GraphNodes(make_deps(registry, trail, engagements, store), parallel_cap=2), registry, trail


# ── 验收 1, 3, 4: ACC-04 ──


async def test_replay_acc04_takes_store_objects_without_calling_agent() -> None:
    """第一次真跑 (验收 3: Run 不在 Store, 正常执行), 第二次以同一状态重跑: 不查登记, 不写外部调用留痕,
    片段与第一次逐字段相等 (含 ended_at), 评分卡, 例外, 执行记录就是 Store 中的对象, Store 无新增."""
    store = FakeOutputStore(list(p0_pack()))
    first_nodes, first_registry, _ = counted_nodes(store)
    first = await first_nodes.p0_intel(p0_state())
    assert first_registry.abn_calls == 1
    rows = list(store.rows)

    nodes, registry, trail = counted_nodes(store)
    second = await nodes.p0_intel(p0_state())

    assert (registry.abn_calls, registry.asic_queries) == (0, [])
    # 不重做外部调用, 也不写其留痕; 只有程序状态的 transition (这里换了新的留痕存储, 同一存储中同键跳过, 见 test_trail)
    assert [e.kind for e in trail.entries] == [TrailKind.transition]
    assert second == first
    assert second["acceptance_scorecard"] is next(r for r in rows if r.id == "SCORECARD")
    assert [x.id for x in second["exceptions"]] == ["X-ACC-04-1-name_match-1"]
    assert second["procedure_runs"][0] is next(r for r in rows if r.id == "RUN-ACC-04-1")
    assert "review_tasks" not in second  # A2 不写任务, 也没有 A1 代写的失败任务
    assert store.rows == rows


async def test_rewind_checkpoint_replays_p0_intel_from_store() -> None:
    """验收 4: 跑到 ACC-01 闸门后回退到 p0_intel 之前的检查点再跑: 登记不再查, Store 无新增, 状态与第一次相同."""
    registry = CountingRegistry()
    runner, store = make_runner(registry=registry)
    config = GraphRunner.config(EID)
    await runner.start(EID)
    await wait_idle(runner, EID)
    first = (await runner.app.aget_state(config)).values
    rows = list(store.rows)
    before = [s async for s in runner.app.aget_state_history(config) if s.next == ("p0_intel",)]

    await runner.app.ainvoke(None, before[0].config)

    after = await runner.app.aget_state(config)
    assert registry.abn_calls == 1
    assert store.rows == rows
    assert after.next == ("gate_acc01",)
    for key in ("acceptance_scorecard", "exceptions", "procedure_runs", "procedure_pack"):
        assert after.values[key] == first[key]


# ── 验收 2: 失败的执行 ──


async def test_replay_failed_run_waits_without_calling_agent() -> None:
    """Store 已有 RUN-ACC-04-1 failed, 其 failed 任务与 ACC-04 v2 awaiting_human: 不调 Agent, 照 Store 等待."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    failure = fail_run(deps.queue, make_run(), "RuntimeError: boom", NOW)
    waiting = update_procedure(p0_pack(), "ACC-04", status=ProcedureStatus.awaiting_human)[0]
    store = FakeOutputStore([*p0_pack(), *failure["procedure_runs"], *failure["review_tasks"], waiting])
    nodes, _, _ = counted_nodes(store)
    agent = ScriptAgent(nodes.deps, lambda ctx: {"procedure_runs": [finished(ctx.run)]})  # 若被调用会成功
    nodes.agents["ACC-04"] = agent
    rows = list(store.rows)

    result = await single_node(nodes, "ACC-04").ainvoke(p0_state(), GraphRunner.config(EID))

    assert agent.calls == 0
    assert [i.value["run_id"] for i in result["__interrupt__"]] == ["RUN-ACC-04-1"]
    assert store.rows == rows


async def test_replay_waits_even_after_task_decided() -> None:
    """失败任务已被决定 (decided) 后重放: 仍走到同一 interrupt(), 取回决定 ID."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    failure = fail_run(deps.queue, make_run(), "RuntimeError: boom", NOW)
    task = failure["review_tasks"][0]
    decided = task.model_copy(update={"version": 2, "status": TaskStatus.decided})
    waiting = update_procedure(p0_pack(), "ACC-04", status=ProcedureStatus.awaiting_human)[0]
    store = FakeOutputStore([*p0_pack(), *failure["procedure_runs"], task, decided, waiting])
    nodes, _, _ = counted_nodes(store)
    app = single_node(nodes, "ACC-04")
    config = GraphRunner.config(EID)
    await app.ainvoke(p0_state(), config)

    with pytest.raises(LookupError, match="D-1"):  # interrupt() 取回了 D-1, 去 Store 读它 (这里没有预置)
        await app.ainvoke(Command(resume="D-1"), config)


# ── 验收 5: 各类产出的取回规则 ──


def judgment(procedure_id: str, subject_id: str, version: int = 1, exception_ids: list[str] | None = None) -> Judgment:
    """某程序第 1 次执行对 subject_id 的判断; 第 2 版起为已确认."""
    return Judgment(
        id=f"J-{procedure_id}-1-{subject_id}",
        version=version,
        procedure_id=procedure_id,
        run_id=f"RUN-{procedure_id}-1",
        subject_id=subject_id,
        conclusion="ok",
        confidence=0.9,
        confidence_parts={},
        reasoning_chain=[],
        evidence=[],
        counter_argument="",
        proposed_action=ProposedAction.accept,
        exception_ids=exception_ids or [],
        autonomy_level=AutonomyLevel.L3,
        review_status=ReviewStatus.confirmed if version > 1 else ReviewStatus.pending,
        performed_by="U-001",
        created_at=NOW,
    )


def exception(n: int, version: int = 1) -> AuditException:
    """REV-08 第 1 次执行 R01 规则的第 n 条例外; 第 2 版起为已归因."""
    return AuditException(
        id=f"X-REV-08-1-R01-{n}",
        version=version,
        procedure_id="REV-08",
        run_id="RUN-REV-08-1",
        rule_id="R01",
        severity="high",
        source_ids=["SI-2026-0612"],
        evidence=[],
        disposition=ExceptionDisposition.attributed if version > 1 else ExceptionDisposition.open,
    )


def confirmation(bank_account_id: str, version: int = 1) -> Confirmation:
    return Confirmation(
        id=f"C-{bank_account_id}",
        version=version,
        bank_account_id=bank_account_id,
        confirming_party="Westpac",
        idempotency_key=f"{EID}:CSH-04:{bank_account_id}",
        sent_at=NOW,
    )


async def test_a6_outputs_by_run_id_latest_versions_and_referenced_exceptions() -> None:
    """A6 的 REV-02 第 1 次执行: 判断, 任务按 run_id 取各自最新版本; 判断引用的 REV-08 例外 (别的执行查出, 已被置
    attributed) 也取最新版本; 函证不带 run_id 取最新版本; 别的执行的判断与例外不取."""
    run = finished(make_run("REV-02"))
    j1 = judgment("REV-02", "SI-2026-0612", exception_ids=["X-REV-08-1-R01-1"])
    j1_confirmed = judgment("REV-02", "SI-2026-0612", version=2, exception_ids=["X-REV-08-1-R01-1"])
    other_judgment = judgment("REV-03", "CON-001")
    rev08, rev08_attributed, unrelated = exception(1), exception(1, version=2), exception(2)
    store = FakeOutputStore(
        [
            run,
            rev08,
            unrelated,
            j1,
            other_judgment,
            confirmation("BA-01"),
            rev08_attributed,
            j1_confirmed,
            confirmation("BA-01", version=2),
        ]
    )
    deps = make_deps(FakeRegistry(), FakeTrail(), output_store=store)

    patch = await stored_outputs(deps, EID, AgentId.A6, run)

    assert patch == {
        "procedure_runs": [run],
        "review_tasks": [],
        "confirmations": [confirmation("BA-01", version=2)],
        "exceptions": [rev08_attributed],
        "judgments": [j1_confirmed],
    }


async def test_a4_singletons_and_planning_lists_take_latest() -> None:
    run = finished(make_run("PLN-01"))
    analytics = AnalyticsResult(id="ANALYTICS", ratios={"gross_margin": 0.3}, exception_ids=[])
    candidates = MaterialityCandidates(id="MATERIALITY", candidates=[])
    risk = RiskItem(
        id="RISK-REV-CUT",
        objective_id="OBJ-REV-CUT",
        assertion="Cutoff",
        level="significant",
        fraud_related=False,
        description="期末前后收入跨期",
    )
    store = FakeOutputStore(
        [run, analytics, candidates, candidates.model_copy(update={"version": 2}), *NUMBAT_OBJECTIVES, risk]
    )
    deps = make_deps(FakeRegistry(), FakeTrail(), output_store=store)

    patch = await stored_outputs(deps, EID, AgentId.A4, run)

    assert patch["analytics"] == analytics
    assert patch["materiality_candidates"].version == 2
    assert patch["audit_objectives"] == NUMBAT_OBJECTIVES
    assert patch["risk_register"] == [risk]
    assert patch["exceptions"] == []
    assert "review_tasks" not in patch


async def test_a3_documents_from_financial_store() -> None:
    run = finished(make_run("DOC-04"))
    doc = DocumentRef(
        id="DOC-0123456789ab",
        type=DocumentType.contract,
        file_name="CON-001.pdf",
        sha256="0" * 64,
        page_count=3,
        reliability=Reliability.external_copy,
        status="extracted",
        uploaded_at=NOW,
    )
    deps = make_deps(
        FakeRegistry(), FakeTrail(), output_store=FakeOutputStore([run]), financial_store=FakeFinancialStore([doc])
    )

    patch = await stored_outputs(deps, EID, AgentId.A3, run)

    assert patch == {"procedure_runs": [run], "review_tasks": [], "confirmations": [], "documents": [doc]}


async def test_failed_run_returns_only_run_and_tasks() -> None:
    """执行失败时 Agent 的片段当初已丢弃: 即使 Store 里有同程序别的产出 (如上一次成功的评分卡), 也只取执行记录与任务."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    failure = fail_run(deps.queue, make_run(run_no=2), "RuntimeError: boom", NOW)
    earlier = finished(make_run(run_no=1))
    store = FakeOutputStore([earlier, *failure["procedure_runs"], *failure["review_tasks"]])
    deps = make_deps(FakeRegistry(), FakeTrail(), output_store=store)

    patch = await stored_outputs(deps, EID, AgentId.A2, failure["procedure_runs"][0])

    assert patch == failure


async def test_replay_does_not_read_current_time_or_rebuild_run() -> None:
    """取回的执行记录就是 Store 中那条 (started_at, ended_at 不变), 不是本次新建的."""
    stored = finished(make_run()).model_copy(update={"started_at": NOW.replace(year=2025)})
    nodes, _, _ = counted_nodes(FakeOutputStore([*p0_pack(), stored]))

    patch = await nodes.p0_intel(p0_state())

    assert patch["procedure_runs"] == [stored]
    assert patch["procedure_pack"][0].status is ProcedureStatus.completed


# ── Codex R04-01: 补齐引用 (回退到更早的检查点时, 最新产出可能来自后一次执行) ──


async def test_rewind_before_first_of_two_runs_keeps_gate_material_consistent() -> None:
    """ACC-04 跑了两次 (第 2 次因画像更新, 出 name_match 例外), 回退到第 1 次之前重跑: 重放第 1 次取到最新评分卡 v2,
    其引用的第 2 次的例外与第 2 次执行记录一并取回, 闸门材料完整; 登记不再查, Store 无新增."""
    registry = CountingRegistry()
    runner, store = make_runner(registry=registry, engagement_store=FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT]))
    config = GraphRunner.config(EID)
    await runner.start(EID)
    await wait_idle(runner, EID)
    before_first = [s async for s in runner.app.aget_state_history(config) if s.next == ("p0_intel",)][-1]
    submit = GraphDecisionSubmit(runner.app, runner.deps, runner)
    await submit.update_client_profile(EID, TYPO_PROFILE.model_copy(update={"version": 2}), updated_by="U-001")
    await wait_idle(runner, EID)
    assert registry.abn_calls == 2
    rows = list(store.rows)

    await runner.app.ainvoke(None, before_first.config)

    assert registry.abn_calls == 2
    assert store.rows == rows
    gate = await GraphStateQuery(runner.app, runner.deps, runner).gate(EID)
    assert gate is not None and gate.target_version == 2
    assert [x.id for x in gate.material.exceptions] == ["X-ACC-04-2-name_match-1"]
    runs = (await runner.app.aget_state(config)).values["procedure_runs"]
    assert [r.id for r in runs] == ["RUN-ACC-04-1", "RUN-ACC-04-2"]


async def test_referenced_exception_brings_its_run() -> None:
    """A6 判断引用 REV-08 的例外: 例外所属的 RUN-REV-08-1 也取回, 片段的 run_id 都能找到."""
    run, prior = finished(make_run("REV-02")), finished(make_run("REV-08"))
    j1 = judgment("REV-02", "SI-2026-0612", exception_ids=["X-REV-08-1-R01-1"])
    store = FakeOutputStore([prior, exception(1), run, j1])
    deps = make_deps(FakeRegistry(), FakeTrail(), output_store=store)

    patch = await stored_outputs(deps, EID, AgentId.A6, run)

    assert patch["procedure_runs"] == [run, prior]
    assert patch["exceptions"] == [exception(1)]


async def test_replay_with_broken_references_is_a_node_error() -> None:
    """Store 中评分卡引用的例外不存在 (数据不一致): 重放不悄悄并入状态, 作节点错误抛出."""
    broken = AcceptanceScorecard(
        id="SCORECARD", registry_checks=[], exception_ids=["X-ACC-04-1-abn_active-1"], client_profile_version=1
    )
    nodes, _, _ = counted_nodes(FakeOutputStore([*p0_pack(), finished(make_run()), broken]))

    with pytest.raises(RuntimeError, match="X-ACC-04-1-abn_active-1"):
        await nodes.p0_intel(p0_state())


async def test_replay_takes_decided_task_of_older_run_with_new_exception() -> None:
    """REV-02 第 1 次执行留下指向例外 v1 的待决任务, 第 2 次执行 (重跑, 判断引用该例外) 已写入 Store; 之后审计师决定
    该任务, 同一事务写出例外 v2 与任务 decided. 回退到第 2 次派发前的检查点重放: 取回例外 v2 时须一并取回已决定的
    任务, 否则检查点中的待决任务看上去过期, 重放报错. 重放不调 Agent, Store 不新增."""
    nodes, store, state = await assembled()
    x = exception(1)
    run1 = finished(make_run("REV-02", 1))
    pending = nodes.deps.queue.make_task(TaskKind.unattributed, x, "REV-02", run1.id, now=NOW)
    rev02 = next(p for p in state["procedure_pack"] if p.id == "REV-02")
    executing = rev02.model_copy(update={"version": 2, "status": ProcedureStatus.executing})
    await store.put(EID, [finished(make_run("REV-08")), x, run1, pending, executing], actor="A1")
    state.update(  # type: ignore[typeddict-item]
        procedure_runs=[finished(make_run("REV-08")), run1],
        exceptions=[x],
        review_tasks=[pending],
        procedure_pack=[executing if p.id == "REV-02" else p for p in state["procedure_pack"]],
    )

    def rerun(ctx: object) -> dict[str, object]:
        run = finished(ctx.run)  # type: ignore[attr-defined]
        j = judgment("REV-02", "SI-2026-0612", exception_ids=[x.id]).model_copy(
            update={"id": "J-REV-02-2-SI-2026-0612", "run_id": run.id}
        )
        task = nodes.deps.queue.make_task(TaskKind.normal, j, "REV-02", run.id, now=NOW)
        return {"procedure_runs": [run], "judgments": [j], "review_tasks": [task]}

    agent = ScriptA6(nodes.deps, rerun)  # type: ignore[arg-type]
    nodes.agents["REV-02"] = agent
    staged = apply_patch(state, await nodes.fieldwork(dispatch(state, "REV-02", instance="fw-1-1")))
    await nodes.fan_in(staged)
    decided = pending.model_copy(update={"version": 2, "status": TaskStatus.decided})
    attributed = exception(1, version=2)
    async with store.transaction(EID):
        await store.put(EID, [attributed, decided], actor="A1")
    rows = list(store.rows)

    output = await nodes.fieldwork(dispatch(state, "REV-02", instance="fw-1-1"))

    [item] = output["staged_results"]
    assert agent.calls == 1
    assert decided in item["patch"]["review_tasks"] and attributed in item["patch"]["exceptions"]
    staged = apply_patch(state, output)
    patch = await nodes.fan_in(staged)
    final = apply_patch(staged, patch)
    assert store.rows == rows
    assert next(t for t in final["review_tasks"] if t.id == pending.id) == decided
    assert check_refs(patch, final) == []
    assert await store.get(EID, ProcedureRun, "RUN-REV-02-2") in final["procedure_runs"]
