"""R06 P1 节点: p1_ingest 载入数据集后执行 DOC-04, p1_analytics 依次执行 PLN-07, PLN-01. A3, A4 用 Numbat 脚本 Agent."""

from backend.agents.base import RunContext
from backend.contracts.models import (
    AnalyticsResult,
    AuditException,
    AuditObjective,
    GateDecision,
    GateId,
    GateVerdict,
    MaterialityCandidates,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    RiskItem,
)
from backend.contracts.state import EngagementState, StatePatch, initial_state
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.nodes import GraphNodes, apply_patch, merge_patch
from backend.orchestration.runner import GraphRunner, fixed_procedures
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_CONTRACTS,
    NUMBAT_OBJECTIVES,
    NUMBAT_RISKS,
    DocumentWrite,
    FakeDatasetLoader,
    FakeEvidenceStore,
    FakeFinancialStore,
    FakeOutputStore,
    FakeRegistry,
    FakeTrail,
    ScriptA3,
    assert_pack_synced,
    make_deps,
    make_graph_nodes,
    make_runner,
    numbat_p1_agents,
    wait_idle,
)

EID = ENGAGEMENT.id


def p1_setup() -> tuple[GraphNodes, FakeOutputStore, EngagementState]:
    """ACC-01 通过后的项目: Store 已有固定集第 1 版. 返回节点, Store 与状态."""
    store = FakeOutputStore()
    deps = make_deps(
        FakeRegistry({NUMBAT_ABN.abn: NUMBAT_ABN}, {NUMBAT_ASIC.acn: NUMBAT_ASIC}), FakeTrail(), output_store=store
    )
    pack = fixed_procedures(deps.library)
    store.rows.extend(pack)
    return make_graph_nodes(deps), store, initial_state(ENGAGEMENT, pack, 1)


def by_id(pack: list[Procedure]) -> dict[str, Procedure]:
    return {p.id: p for p in pack}


# ── p1_ingest: 验收 1, 2 ──


async def test_p1_ingest_loads_dataset_before_doc04() -> None:
    nodes, _, state = p1_setup()
    loader = nodes.deps.datasets
    assert isinstance(loader, FakeDatasetLoader)
    seen_loads: list[list[tuple[str, str]]] = []
    doc04 = numbat_p1_agents(nodes.deps)["DOC-04"]

    def recording(ctx: RunContext) -> StatePatch:
        seen_loads.append(list(loader.loads))
        return doc04.script(ctx)  # type: ignore[attr-defined]

    nodes.agents["DOC-04"] = ScriptA3(nodes.deps, recording)

    await nodes.p1_ingest(state)

    assert loader.loads == [(EID, "demo")]
    assert seen_loads == [[(EID, "demo")]]  # A3 执行时数据集已载入


async def test_p1_ingest_writes_run_program_and_document_status_in_one_commit() -> None:
    nodes, store, state = p1_setup()
    evidence = nodes.deps.evidence_store
    financial = nodes.deps.financial_store
    assert isinstance(evidence, FakeEvidenceStore) and isinstance(financial, FakeFinancialStore)

    patch = await nodes.p1_ingest(state)

    [commit] = store.commits
    written = {(type(o).__name__, getattr(o, "id", None) or o.document_id) for o in commit}
    assert written == {
        ("ProcedureRun", "RUN-DOC-04-1"),
        ("Procedure", "DOC-04"),
        *(("DocumentWrite", d.id) for d in NUMBAT_CONTRACTS),
    }
    assert [w for w in commit if isinstance(w, DocumentWrite)] == [
        DocumentWrite(d.id, "extracted") for d in NUMBAT_CONTRACTS
    ]
    assert evidence.calls == [(d.id, "extracted", True) for d in NUMBAT_CONTRACTS]  # 3 次都在事务内
    assert [d.status for d in await financial.documents(EID)] == ["extracted"] * 3
    assert [(d.id, d.status) for d in patch["documents"]] == [(d.id, "extracted") for d in NUMBAT_CONTRACTS]
    doc04 = by_id(patch["procedure_pack"])["DOC-04"]
    assert (doc04.version, doc04.status) == (2, ProcedureStatus.completed)


# ── p1_analytics: 验收 3, 4 ──


async def test_p1_analytics_runs_pln07_then_pln01_on_its_results() -> None:
    nodes, store, state = p1_setup()
    seen: list[RunContext] = []
    agents = numbat_p1_agents(nodes.deps)
    for pid in ("PLN-07", "PLN-01"):
        script = agents[pid].script  # type: ignore[attr-defined]

        def recording(ctx: RunContext, script: object = script) -> StatePatch:
            seen.append(ctx)
            return script(ctx)  # type: ignore[operator]

        nodes.agents[pid] = type(agents[pid])(nodes.deps, recording)

    patch = await nodes.p1_analytics(state)

    assert [ctx.run.id for ctx in seen] == ["RUN-PLN-07-1", "RUN-PLN-01-1"]
    second = seen[1].state  # PLN-01 看到 PLN-07 的结果
    assert by_id(second["procedure_pack"])["PLN-07"].status is ProcedureStatus.completed
    assert [r.id for r in second["procedure_runs"]] == ["RUN-PLN-07-1"]
    assert second["analytics"] is not None and second["analytics"].version == 1

    pack = by_id(patch["procedure_pack"])
    assert (pack["PLN-07"].version, pack["PLN-07"].status) == (2, ProcedureStatus.completed)
    assert (pack["PLN-01"].version, pack["PLN-01"].status) == (2, ProcedureStatus.planned)  # Q5: 闸门批准才 completed
    assert (pack["PLN-03"].version, pack["PLN-03"].status) == (1, ProcedureStatus.planned)
    assert [r.id for r in patch["procedure_runs"]] == ["RUN-PLN-07-1", "RUN-PLN-01-1"]
    assert [x.id for x in patch["exceptions"]] == ["X-PLN-07-1-A01-1"]
    assert patch["analytics"].exception_ids == ["X-PLN-07-1-A01-1"]
    assert patch["materiality_candidates"].id == "MATERIALITY"
    assert patch["audit_objectives"] == NUMBAT_OBJECTIVES
    assert patch["risk_register"] == NUMBAT_RISKS

    assert await store.get(EID, AnalyticsResult, "ANALYTICS") == patch["analytics"]
    assert await store.get(EID, MaterialityCandidates, "MATERIALITY") == patch["materiality_candidates"]
    assert [x.id for x in await store.latest(EID, AuditException)] == ["X-PLN-07-1-A01-1"]
    assert await store.latest(EID, AuditObjective) == NUMBAT_OBJECTIVES
    assert await store.latest(EID, RiskItem) == NUMBAT_RISKS
    assert [r.id for r in await store.latest(EID, ProcedureRun)] == ["RUN-PLN-07-1", "RUN-PLN-01-1"]
    await assert_pack_synced(patch["procedure_pack"], store)


async def test_pln01_rerun_with_risk_register_keeps_presets_out() -> None:
    """状态已有风险清单时 PLN-01 不再带出审计目标与风险 (闸门可能已改过等级), 候选出下一版."""
    nodes, _, state = p1_setup()
    first = await nodes.p1_analytics(state)
    later = {**state, **{k: first[k] for k in ("procedure_pack", "analytics", "materiality_candidates")}}
    later["procedure_runs"] = first["procedure_runs"]
    later["risk_register"] = first["risk_register"]

    patch = await nodes.execute(later, "PLN-01")  # type: ignore[arg-type]

    assert "risk_register" not in patch and "audit_objectives" not in patch
    assert patch["materiality_candidates"].version == 2


# ── 图: 验收 5 ──


async def test_acc01_approve_runs_p1_and_stops_at_pln_gate() -> None:
    runner, store = make_runner()
    await runner.start(EID)
    await wait_idle(runner, EID)
    submit = GraphDecisionSubmit(runner.app, runner.deps, runner)

    await submit.submit(
        EID,
        GateDecision(
            id="D-ACC",
            gate=GateId.ACC_01,
            target_id="SCORECARD",
            target_version=1,
            decision=GateVerdict.approve,
            decided_by="U-001",
            decided_at=NOW,
        ),
    )
    await wait_idle(runner, EID)

    assert runner.run_error(EID) is None
    snapshot = await runner.app.aget_state(GraphRunner.config(EID))
    assert snapshot.next == ("gate_p1_risk",)
    values = snapshot.values
    assert [d.status for d in values["documents"]] == ["extracted"] * 3
    assert values["materiality_candidates"].version == 1
    assert [x.id for x in values["exceptions"]] == ["X-PLN-07-1-A01-1"]
    assert len(values["audit_objectives"]) == 3 and len(values["risk_register"]) == 3
    pack = by_id(values["procedure_pack"])
    assert pack["PLN-01"].status is ProcedureStatus.planned
    await assert_pack_synced(values["procedure_pack"], store)


# ── 节点内依次执行几条程序: 片段并入状态与累计 ──


def test_apply_patch_uses_state_reducers() -> None:
    """带 reducer 的字段按 reducer 合并 (执行记录追加, 同 ID 升版本替换), 其余字段整体替换, 原状态不变."""
    _, _, state = p1_setup()
    old_run = ProcedureRun(
        id="RUN-ACC-04-1",
        procedure_id="ACC-04",
        run_no=1,
        trigger="initial",
        params_snapshot={},
        started_at=NOW,
        status="succeeded",
        ended_at=NOW,
    )
    state["procedure_runs"] = [old_run]
    new_run = old_run.model_copy(update={"id": "RUN-PLN-07-1", "procedure_id": "PLN-07"})

    merged = apply_patch(state, {"procedure_runs": [new_run], "risk_register": NUMBAT_RISKS[:1]})

    assert [r.id for r in merged["procedure_runs"]] == ["RUN-ACC-04-1", "RUN-PLN-07-1"]
    assert merged["risk_register"] == NUMBAT_RISKS[:1]
    assert [r.id for r in state["procedure_runs"]] == ["RUN-ACC-04-1"]


def test_merge_patch_concatenates_reducer_fields_and_replaces_full_lists() -> None:
    """执行记录等带 reducer 的字段拼接; 程序包, 审计目标, 风险清单这类每次传完整列表的字段取后者."""
    result: StatePatch = {}
    merge_patch(result, {"procedure_runs": ["a"], "procedure_pack": ["p1"], "risk_register": ["r1"]})
    merge_patch(result, {"procedure_runs": ["b"], "procedure_pack": ["p2"]})

    assert result == {"procedure_runs": ["a", "b"], "procedure_pack": ["p2"], "risk_register": ["r1"]}
