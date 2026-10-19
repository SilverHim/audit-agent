"""R04 重放: 节点写 Store 后, 写检查点前中断, 恢复时重跑. Store 中本次执行已为终态时不调 Agent, 产出从 Store 取回."""

from typing import Any

import pytest
from langgraph.types import Command

from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import (
    AbnRecord,
    AcceptanceScorecard,
    AnalyticsResult,
    AuditException,
    AutonomyLevel,
    Confirmation,
    Decision,
    DocumentRef,
    DocumentType,
    ExceptionDisposition,
    Judgment,
    MaterialityCandidates,
    ProcedureRun,
    ProcedureStatus,
    ProposedAction,
    Reliability,
    ReviewRecord,
    ReviewStatus,
    RiskItem,
    TaskKind,
    TaskStatus,
    TrailKind,
    Versioned,
)
from backend.orchestration import merge
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.merge import Replay, check_refs
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
    profile_update,
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

    async def abn(self, eid: str, abn: str) -> AbnRecord | None:
        self.abn_calls += 1
        return await super().abn(eid, abn)


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
    """失败任务已被决定 (decided, Store 中有关闭它的 rerun 决定 D-1) 后重放: 有尚未读回的决定, 仍走到同一 interrupt(),
    取回 D-1 去读它写出的程序版本 (这里没有预置 executing 版本, 读回报错, 说明确实取回了 D-1)."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    failure = fail_run(deps.queue, make_run(), "RuntimeError: boom", NOW)
    task = failure["review_tasks"][0]
    decided = task.model_copy(update={"version": 2, "status": TaskStatus.decided})
    waiting = update_procedure(p0_pack(), "ACC-04", status=ProcedureStatus.awaiting_human)[0]
    record = ReviewRecord(
        id="D-1",
        task_id=task.id,
        target_version=task.target_version,
        decision=Decision.rerun,
        reason="重跑",
        reviewed_by="U-001",
        decided_at=NOW,
    )
    store = FakeOutputStore([*p0_pack(), *failure["procedure_runs"], task, decided, waiting, record])
    nodes, _, _ = counted_nodes(store)
    app = single_node(nodes, "ACC-04")
    config = GraphRunner.config(EID)
    await app.ainvoke(p0_state(), config)

    with pytest.raises(ValueError, match="D-1 没有写出 ACC-04 v3 executing"):
        await app.ainvoke(Command(resume="D-1"), config)


async def test_replay_with_task_decided_but_no_decision_is_a_node_error() -> None:
    """Store 对不上: 失败任务已 decided, 却没有针对它的决定, 程序最新版本仍为 awaiting_human. 节点不再等 (没有待决任务,
    也没有未读回的决定), 但不静默结束, 报错说明不一致, 不前进. DecisionSubmit 同一事务写决定与任务, 正常走不到."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    failure = fail_run(deps.queue, make_run(), "RuntimeError: boom", NOW)
    task = failure["review_tasks"][0]
    decided = task.model_copy(update={"version": 2, "status": TaskStatus.decided})
    waiting = update_procedure(p0_pack(), "ACC-04", status=ProcedureStatus.awaiting_human)[0]
    store = FakeOutputStore([*p0_pack(), *failure["procedure_runs"], task, decided, waiting])
    rows = list(store.rows)
    nodes, registry, _ = counted_nodes(store)
    app = single_node(nodes, "ACC-04")

    mismatch = "ACC-04 没有待决任务和未读回的决定, Store 中最新版本 v2 却是 awaiting_human"
    with pytest.raises(RuntimeError, match=mismatch):
        await app.ainvoke(p0_state(), GraphRunner.config(EID))
    assert registry.abn_calls == 0  # 重放取回失败执行, 不再调 Agent
    assert store.rows == rows


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
    attributed) 也取最新版本; 别的执行的判断与例外不取; REV-02 不声明函证, 不取 (R85)."""
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

    patch = await stored_outputs(deps, EID, deps.library.load("REV-02"), run)

    assert patch == {
        "procedure_runs": [run],
        "review_tasks": [],
        "exceptions": [rev08_attributed],
        "judgments": [j1_confirmed],
    }


def a4_store(run: ProcedureRun) -> tuple[FakeOutputStore, AnalyticsResult, RiskItem]:
    """A4 的产出都在 Store 中: 分析结果, 重要性候选 v1 与 v2, 审计目标与风险."""
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
    return store, analytics, risk


async def test_pln07_replay_takes_only_its_outputs() -> None:
    """PLN-07 只声明分析结果与例外: 重放只含这两项 (R85 之前按 A4 的写入范围, 还带重要性候选, 审计目标与风险)."""
    run = finished(make_run("PLN-07"))
    store, analytics, _ = a4_store(run)
    deps = make_deps(FakeRegistry(), FakeTrail(), output_store=store)

    patch = await stored_outputs(deps, EID, deps.library.load("PLN-07"), run)

    assert patch == {"procedure_runs": [run], "analytics": analytics, "exceptions": []}


async def test_pln01_replay_takes_latest_singleton_and_planning_lists() -> None:
    """PLN-01 声明重要性候选, 审计目标与风险清单: 候选取最新 v2, 两个列表取全部最新版本; 不含分析结果与例外."""
    run = finished(make_run("PLN-01"))
    store, _, risk = a4_store(run)
    deps = make_deps(FakeRegistry(), FakeTrail(), output_store=store)

    patch = await stored_outputs(deps, EID, deps.library.load("PLN-01"), run)

    assert set(patch) == {"procedure_runs", "materiality_candidates", "audit_objectives", "risk_register"}
    assert patch["materiality_candidates"].version == 2
    assert patch["audit_objectives"] == NUMBAT_OBJECTIVES
    assert patch["risk_register"] == [risk]


def stored_document(n: str, type: DocumentType, file_name: str, **fields: Any) -> DocumentRef:
    """财务存储中登记的一份文件 (ID DOC-{n}), fields 覆盖其余字段."""
    values: dict[str, Any] = {
        "id": f"DOC-{n}",
        "type": type,
        "file_name": file_name,
        "sha256": n * 64,
        "page_count": 3,
        "reliability": Reliability.external_copy,
        "status": "extracted",
        "uploaded_at": NOW,
    }
    return DocumentRef.model_validate(values | fields)


CONTRACT = stored_document("1", DocumentType.contract, "CON-001.pdf")
REPLY = stored_document("2", DocumentType.confirmation, "BA-01-reply.pdf", channel="bank_direct", status="received")
UPLOADED = stored_document(  # 审计师上传, A3 分类为发票
    "3",
    DocumentType.invoice,
    "INV-77.pdf",
    reliability=Reliability.internal_copy,
    status="classified",
    uploaded_by="U-1",
)
IMPORTED = stored_document(  # 导入账表的原件
    "4",
    DocumentType.other,
    "gl.csv",
    page_count=1,
    reliability=Reliability.internal_copy,
    media_type="text/csv",
    uploaded_by="U-1",
)


@pytest.mark.parametrize(("procedure_id", "kept"), [("DOC-04", CONTRACT), ("DOC-06", REPLY), ("CSH-04", REPLY)])
async def test_a3_a5_documents_replay_only_declared_types(procedure_id: str, kept: DocumentRef) -> None:
    """文件取自 FinancialDataStore.documents, 只取本程序声明的类型 (document_types), 与第一次执行交回的同类:
    上传后分类为别的类型的文件与导入账表的原件不进重放片段."""
    run = finished(make_run(procedure_id))
    deps = make_deps(
        FakeRegistry(),
        FakeTrail(),
        output_store=FakeOutputStore([run]),
        financial_store=FakeFinancialStore([CONTRACT, REPLY, UPLOADED, IMPORTED]),
    )
    template = deps.library.load(procedure_id)

    patch = await stored_outputs(deps, EID, template, run)

    assert set(patch) == {"procedure_runs", *template.outputs}
    assert patch["documents"] == [kept]


async def test_failed_run_returns_only_run_and_tasks() -> None:
    """执行失败时 Agent 的片段当初已丢弃: 即使 Store 里有同程序别的产出 (如上一次成功的评分卡), 也只取执行记录与任务."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    failure = fail_run(deps.queue, make_run(run_no=2), "RuntimeError: boom", NOW)
    earlier = finished(make_run(run_no=1))
    store = FakeOutputStore([earlier, *failure["procedure_runs"], *failure["review_tasks"]])
    deps = make_deps(FakeRegistry(), FakeTrail(), output_store=store)

    patch = await stored_outputs(deps, EID, deps.library.load("ACC-04"), failure["procedure_runs"][0])

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
    await submit.update_client_profile(
        EID, *profile_update(TYPO_PROFILE.model_copy(update={"version": 2})), updated_by="U-001"
    )
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

    patch = await stored_outputs(deps, EID, deps.library.load("REV-02"), run)

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


# ── R85: 带 run_id 的单对象与按程序取回 (测试内定义的产出模型) ──


class Gauge(Versioned):
    """测试用的带 run_id 的产出 (如以后的持续经营结论)."""

    run_id: str
    value: int


def gauge_field(monkeypatch: pytest.MonkeyPatch, field: str, hint: Any, replay: Replay) -> ProcedureTemplate:
    """登记一个测试字段 (类型与取回方式), 返回只声明它的 FIN-04 模板 (owner A9)."""
    monkeypatch.setitem(merge.STATE_HINTS, field, hint)
    monkeypatch.setitem(merge.FIELD_REPLAY, field, replay)
    return make_deps(FakeRegistry(), FakeTrail()).library.load("FIN-04").model_copy(update={"outputs": [field]})


async def test_singleton_with_run_id_takes_latest_and_checks_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """R85 验收 6: Store 有 G v1 (RUN-FIN-04-1), v2 (RUN-FIN-04-2): 重放第 1 次取 v2 并带回第 2 次执行 (同评分卡规则),
    片段中为对象而不是列表; 另有别的程序写的 v3 时节点错误, 消息含 v3 与那次执行."""
    template = gauge_field(monkeypatch, "gauge", Gauge | None, Replay.singleton)
    run1, run2 = finished(make_run("FIN-04")), finished(make_run("FIN-04", run_no=2))
    g1 = Gauge(id="GAUGE", run_id=run1.id, value=1)
    g2 = g1.model_copy(update={"version": 2, "run_id": run2.id, "value": 2})
    store = FakeOutputStore([run1, g1, run2, g2])
    deps = make_deps(FakeRegistry(), FakeTrail(), output_store=store)

    patch = await stored_outputs(deps, EID, template, run1)

    assert patch == {"procedure_runs": [run1, run2], "gauge": g2}
    other = finished(make_run("REV-08"))
    await store.put(EID, [other, g1.model_copy(update={"version": 3, "run_id": other.id})], actor="A9")
    with pytest.raises(RuntimeError, match=r"GAUGE v3 由 RUN-REV-08-1 写"):
        await stored_outputs(deps, EID, template, run1)


async def test_by_procedure_takes_only_this_programs_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """by_procedure: 各 ID 的最新版本中 run_id 属于本程序的才取 (别的程序写的不取), 并带回本程序之后的执行."""
    template = gauge_field(monkeypatch, "gauges", list[Gauge], Replay.by_procedure)
    run1, run2, other = (
        finished(make_run("FIN-04")),
        finished(make_run("FIN-04", run_no=2)),
        finished(make_run("REV-08")),
    )
    mine = Gauge(id="G-1", run_id=run1.id, value=1)
    later = Gauge(id="G-2", run_id=run2.id, value=2)
    theirs = Gauge(id="G-3", run_id=other.id, value=3)
    deps = make_deps(
        FakeRegistry(), FakeTrail(), output_store=FakeOutputStore([run1, run2, other, mine, later, theirs])
    )

    patch = await stored_outputs(deps, EID, template, run1)

    assert patch == {"procedure_runs": [run1, run2], "gauges": [mine, later]}
