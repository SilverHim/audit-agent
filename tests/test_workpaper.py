"""需求 R158: A8 WorkpaperAgent 与 FIN-03 错报性质分类.

Xero 演示上直接调 A8 (不经主图: FIN-03 的节点由 R160 接入): 状态为进入 P2 时的 (xero_state) 加已认定而未分类的两条
错报与其来源判断 (xero_identified_misstatements); 真实判断引擎, 主模型读 FIN-03 的 Xero fixture 正本 (R157), 假财务
存储另登记 BA-091 的回函 (CSH-04 存档). 判断带分类载荷, 确认后写错报新版本由 R159 做.
"""

import json
from dataclasses import replace
from typing import Any

import pytest

from backend.adapters.llm import MockLLMClient
from backend.agents.base import AgentDeps, RunContext
from backend.agents.workpaper import WorkpaperAgent, misstatement_objective
from backend.contracts.models import (
    AgentId,
    Assertion,
    AuditObjective,
    AutonomyLevel,
    Decision,
    Misstatement,
    MisstatementStatus,
    MisstatementUpdate,
    OpinionType,
    Phase,
    ReviewStatus,
    RiskLevel,
    RunStatus,
    TaskKind,
    TrailKind,
    UncorrectedSchedule,
)
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.gates import fin01_view
from backend.orchestration.merge import check_patch
from backend.orchestration.nodes import build_run
from tests.fakes import NOW, FakeTrail, ScriptedLLM, make_ctx, xero_document
from tests.fixture_sets import XERO
from tests.test_xero_fixtures import RecordingJudgments, xero_state
from tests.xero_fakes import make_xero_deps, xero_engagement, xero_identified_misstatements, xero_profile, xero_traits

FIN03 = "FIN-03"
REPLY = "confirmations/12345679.pdf"  # BA-091 的回函 (CSH-04 存档, 不在外勤阶段的上传里)
SUBJECTS = ["M-D-XJ499", "M-D-CSH02-091"]  # 按认定的先后
FIXTURE_KEYS = ["XJ-499", "BA-091"]  # 来源判断的对象


def fin03_deps(trail: FakeTrail, llm: Any = None) -> AgentDeps:
    """Xero 假实现, 主模型缺省读 Xero fixture 正本 (复核模型同; FIN-03 为 L2, A7 按跳过规则不调), 判断引擎外包一层
    记下上下文; 财务存储另登记 BA-091 的回函."""
    client = llm if llm is not None else MockLLMClient(XERO.fixtures)
    deps = make_xero_deps(trail=trail, llm=client, review_llm=client)
    deps.financial_store.document_refs.append(xero_document(REPLY))  # type: ignore[attr-defined]
    return deps


def fin03_state(**changes: Any) -> EngagementState:
    """FIN-03 前的状态: xero_state 加两条错报与来源判断 (另有 REV-03 对 CON-X01 的判断); changes 覆盖任意键."""
    preset = xero_identified_misstatements()
    state = xero_state() | {"judgments": preset["judgments"], "misstatements": preset["misstatements"]}
    return state | changes  # type: ignore[return-value]


def fin03_context(deps: AgentDeps, state: EngagementState) -> RunContext:
    """FIN-03 第 1 次执行: 程序为完成阶段的第 1 版 (L2), 执行记录同 GraphNodes.perform 用 build_run 建."""
    engagement = xero_engagement()
    ctx = make_ctx(deps, FIN03, engagement=engagement)
    procedure = ctx.procedure.model_copy(update={"phase": Phase.P3_Completion})
    run = build_run(
        deps.library, ctx.template, procedure, engagement, state, NOW, traits=xero_traits(), profile=xero_profile()
    )
    return RunContext(engagement, procedure, ctx.template, run, state | {"procedure_pack": [procedure]})  # type: ignore[arg-type]


async def run_fin03(deps: AgentDeps, state: EngagementState) -> tuple[StatePatch, RunContext]:
    ctx = fin03_context(deps, state)
    return await WorkpaperAgent(deps).run(ctx), ctx


def llm_calls(trail: FakeTrail) -> list[Any]:
    return [e for e in trail.entries if e.kind is TrailKind.llm_call]


@pytest.mark.external
async def test_fin03_classifies_identified_misstatements() -> None:
    """验收 1 至 4: 两条 identified 而未分类的错报各一条判断 (第 1 版, 待复核) 与 normal 任务, 执行成功; 片段只有
    执行记录, 判断与任务 (不写错报与例外), 经 A8 的合并校验通过. 判断带分类载荷: 类型与舞弊迹象取模型, 审计目标与
    期间来源取规则 (两条都配不上审计目标, 一律本期). 置信度 evidence 0.7, rule 1.0, llm 0.85, 合为 0.85; 自主度 L2.
    2 行 llm_call, actor A8, fixture_key 为来源判断的对象. 上下文见 test_fin03_context_per_misstatement."""
    trail = FakeTrail()
    deps = fin03_deps(trail)
    patch, ctx = await run_fin03(deps, fin03_state())

    assert set(patch) == {"procedure_runs", "judgments", "review_tasks"}
    check_patch(
        patch, writer=AgentId.A8, procedure_id=FIN03, run_id=ctx.run.id, state=ctx.state, outputs=ctx.template.outputs
    )
    [run] = patch["procedure_runs"]
    assert (run.id, run.status) == ("RUN-FIN-03-1", RunStatus.succeeded)
    judgments = patch["judgments"]
    assert [(j.id, j.version, j.review_status) for j in judgments] == [
        (f"J-FIN-03-1-{m}", 1, ReviewStatus.pending) for m in SUBJECTS
    ]
    assert [(t.id, t.kind, t.target_id) for t in patch["review_tasks"]] == [
        (f"T-J-FIN-03-1-{m}-v1", TaskKind.normal, f"J-FIN-03-1-{m}") for m in SUBJECTS
    ]
    assert [j.misstatement_update for j in judgments] == [
        MisstatementUpdate(
            misstatement_id=m,
            base_version=1,
            kind="classification",
            type="factual",
            fraud_indicative=False,
            fraud_reason=None,
            origin="current_period",
            objective_id=None,
        )
        for m in SUBJECTS
    ]
    for j in judgments:
        assert (j.confidence_parts, j.confidence, j.autonomy_level) == (
            {"evidence": 0.7, "rule": 1.0, "llm": 0.85},
            0.85,
            AutonomyLevel.L2,
        )
    calls = llm_calls(trail)
    assert [(e.actor, e.procedure_id, e.run_id, e.payload["fixture_key"]) for e in calls] == [
        (AgentId.A8, FIN03, "RUN-FIN-03-1", key) for key in FIXTURE_KEYS
    ]


@pytest.mark.external
async def test_fin03_context_per_misstatement() -> None:
    """验收 3: 上下文为错报, 来源判断 (结论与证据), XJ-499 的整笔分录行 XJ-499-1..3 (BA-091 不是凭证号, 两次取数都
    为空), 来源判断其余指针所指的行 (XJ-499: 试算表 710; BA-091: 试算表 091 与分录行 XJ-601-1), 来源判断所引文件的
    登记 (固定资产登记表; 回函), 舞弊相关风险; 分录行按凭证号与按原始单据各取一次. 不含别的程序的判断 (REV-03 对
    CON-X01 的判断在状态里, 不进提示词)."""
    trail = FakeTrail()
    deps = fin03_deps(trail)
    recorder = RecordingJudgments(deps.judgments)
    deps = replace(deps, judgments=recorder)
    calls: list[dict[str, Any]] = []
    store = deps.financial_store
    original = store.journal_entries

    async def journal_entries(eid: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return await original(eid, **kwargs)

    store.journal_entries = journal_entries  # type: ignore[method-assign]
    await run_fin03(deps, fin03_state())

    assert calls == [
        {"entry_nos": ["XJ-499"]},
        {"source_docs": ["XJ-499"]},
        {"entry_nos": ["BA-091"]},
        {"source_docs": ["BA-091"]},
    ]
    xj499, ba091 = (c.data for c in recorder.contexts)
    assert [c.fixture_key for c in recorder.contexts] == FIXTURE_KEYS
    assert (xj499["misstatement"]["id"], xj499["misstatement"]["account_code"]) == ("M-D-XJ499", "710")
    assert (xj499["source_judgment"]["id"], xj499["source_judgment"]["subject_id"]) == ("J-GEN-02-1-XJ-499", "XJ-499")
    assert [r["id"] for r in xj499["journal_lines"]] == ["XJ-499-1", "XJ-499-2", "XJ-499-3"]
    assert [(r["source_type"], r["account_code"]) for r in xj499["cited_rows"]] == [("trial_balance", "710")]
    assert [d["id"] for d in xj499["documents"]] == [xero_document("documents/evidence/fixed_asset_register.pdf").id]
    assert ba091["journal_lines"] == []
    assert [(r["source_type"], r.get("id") or r.get("account_code")) for r in ba091["cited_rows"]] == [
        ("trial_balance", "091"),
        ("journal_entry", "XJ-601-1"),
    ]
    assert [d["id"] for d in ba091["documents"]] == [xero_document(REPLY).id]
    risks = [r for r in fin03_state()["risk_register"] if r.fraud_related]
    expected = [r.model_dump(mode="json", include={"id", "assertion", "level", "description"}) for r in risks]
    assert xj499["fraud_risks"] == ba091["fraud_risks"] == expected
    prompts = [m["content"] for e in llm_calls(trail) for m in e.payload["messages"]]
    assert prompts and not any("J-REV-03-1-CON-X01" in p or "CON-X01" in p for p in prompts)


@pytest.mark.external
async def test_update_bases_on_the_misstatement_version_judged() -> None:
    """载荷的 base_version 为判断时错报的当前版本 (如未分类而已出第 2 版的), 写入时据此核对 (R159)."""
    state = fin03_state()
    first, second = state["misstatements"]
    patch, _ = await run_fin03(
        fin03_deps(FakeTrail()), state | {"misstatements": [first.model_copy(update={"version": 2}), second]}
    )  # type: ignore[arg-type]
    assert [j.misstatement_update.base_version for j in patch["judgments"]] == [2, 1]  # type: ignore[union-attr]


OBJECTIVES = [
    AuditObjective(
        id="OBJ-REV-OCC",
        account="Revenue",
        assertion=Assertion.Occurrence,
        risk_level=RiskLevel.significant,
        is_material=True,
    ),
    AuditObjective(
        id="OBJ-REV-CUT",
        account="Revenue",
        assertion=Assertion.Cutoff,
        risk_level=RiskLevel.significant,
        is_material=True,
    ),
    AuditObjective(
        id="OBJ-CASH-EXI", account="Cash", assertion=Assertion.Existence, risk_level=RiskLevel.high, is_material=True
    ),
]


@pytest.mark.parametrize(
    ("account", "assertion", "objective"),
    [
        ("Revenue", Assertion.Cutoff, "OBJ-REV-CUT"),
        ("Sales revenue (200)", Assertion.Occurrence, "OBJ-REV-OCC"),  # 整词出现, 不分大小写
        ("Revenue", Assertion.Existence, None),  # 认定不同
        ("Revenues", Assertion.Cutoff, None),  # 不是整词
        ("Interest income (270)", Assertion.Completeness, None),
        ("Petty cash", Assertion.Existence, "OBJ-CASH-EXI"),
    ],
)
def test_misstatement_objective_rule(account: str, assertion: Assertion, objective: str | None) -> None:
    """验收 2: 审计目标的科目名作整词出现在错报科目里 (不分大小写) 且认定相同的第一条; 配不上为空. 不取大模型."""
    misstatement = Misstatement(
        id="M-D-1",
        judgment_id="J-1",
        exception_id=None,
        review_record_id="D-1",
        type="factual",
        account=account,
        assertion=assertion,
        amount_cents=100,
        direction="overstated",
    )
    assert misstatement_objective(misstatement, OBJECTIVES) == objective


def classified(m: Misstatement) -> Misstatement:
    return m.model_copy(update={"version": 2, "classification_judgment_id": f"J-FIN-03-1-{m.id}"})


@pytest.mark.external
@pytest.mark.parametrize(
    "misstatements",
    [
        lambda ms: [],
        lambda ms: [classified(m) for m in ms],  # 都已分类
        lambda ms: [m.model_copy(update={"status": MisstatementStatus.uncorrected}) for m in ms],  # 不是 identified
    ],
    ids=["none", "classified", "not_identified"],
)
async def test_fin03_without_unclassified_misstatements_calls_no_model(misstatements: Any) -> None:
    """验收 5: 没有未分类的 identified 错报 (没有错报, 都已分类, 或状态不是 identified): 不调大模型, 执行成功, 片段只有
    procedure_runs."""
    trail = FakeTrail()
    deps = fin03_deps(trail)
    state = fin03_state()
    patch, _ = await run_fin03(deps, state | {"misstatements": misstatements(state["misstatements"])})  # type: ignore[arg-type]

    assert llm_calls(trail) == []
    assert list(patch) == ["procedure_runs"]
    assert patch["procedure_runs"][0].status is RunStatus.succeeded


FRAUD_REASON = "The entry records an asset that does not exist and was posted by the sole director without review."


def fixture_raw(name: str) -> str:
    return (XERO.fixtures / FIN03 / f"{name}.json").read_text(encoding="utf-8")


@pytest.mark.external
async def test_fraud_indicative_sample_is_flagged_and_visible_at_fin01() -> None:
    """验收 6 (合成舞弊样例, 按协调者裁定拆分): 模型给 XJ-499 有舞弊迹象与理由时, 判断的载荷 fraud_indicative 为真并带
    理由; 按载荷出的错报新版本 (确认时写出它由 R159 做, 这里按载荷构造) 在 FIN-01 闸门材料中带着标记, 材料照常生成,
    不阻断流程 (规格书 12.2 "为真则进环 3 阻断" 本版不做)."""
    fraud = json.loads(fixture_raw("XJ-499"))
    fraud["nature"] = {"type": "factual", "fraud_indicative": True, "fraud_reason": FRAUD_REASON}
    llm = ScriptedLLM({"XJ-499": [json.dumps(fraud)], "BA-091": [fixture_raw("BA-091")]})
    trail = FakeTrail()
    deps = fin03_deps(trail, llm)
    state = fin03_state()
    patch, _ = await run_fin03(deps, state)

    judgment = patch["judgments"][0]
    update = judgment.misstatement_update
    assert update is not None and (update.fraud_indicative, update.fraud_reason) == (True, FRAUD_REASON)
    source = state["misstatements"][0]
    flagged = Misstatement.model_validate(
        source.model_dump()
        | update.model_dump(include={"type", "fraud_indicative", "fraud_reason", "origin", "objective_id"})
        | {"version": 2, "classification_judgment_id": judgment.id}
    )
    schedule = UncorrectedSchedule(
        id="SCHEDULE",
        items=[],
        excluded_trivial_ids=[],
        total_cents=0,
        materiality_cents=32431,
        candidate_opinion=OpinionType.unqualified,
    )
    values = state | {"misstatements": [flagged, state["misstatements"][1]], "uncorrected_schedule": schedule}
    _, material = await fin01_view(deps, xero_engagement().id, values)  # type: ignore[arg-type]
    assert [(m.id, m.version, m.fraud_indicative, m.fraud_reason) for m in material.misstatements] == [  # type: ignore[union-attr]
        ("M-D-XJ499", 2, True, FRAUD_REASON),
        ("M-D-CSH02-091", 1, None, None),
    ]


@pytest.mark.external
@pytest.mark.parametrize(
    ("replies", "attempts"),
    [
        (["not json", "not json", "not json"], 3),  # 结构失败, 重试 2 次
        (
            [
                json.dumps(
                    json.loads(fixture_raw("XJ-499")) | {"nature": {"type": "projected", "fraud_indicative": False}}
                )
            ],
            1,
        ),
    ],
    ids=["structure", "semantic"],
)
async def test_failed_classification_creates_failed_task_allowing_manual_result(
    replies: list[str], attempts: int
) -> None:
    """验收 7: 大模型连续 3 次结构失败, 或引用, 语义失败 (只调 1 次, 如类型为 projected) 时, 该错报建带对象的 failed
    任务 T-RUN-FIN-03-1-M-D-XJ499-v1, 允许 {rerun, manual_result} (FIN-03 开 manual_result_allowed, 需求 R158);
    另一条照常判断."""
    llm = ScriptedLLM({"XJ-499": replies, "BA-091": [fixture_raw("BA-091")]})
    trail = FakeTrail()
    deps = fin03_deps(trail, llm)
    patch, _ = await run_fin03(deps, fin03_state())

    assert len(llm.requests) == attempts + 1
    assert [j.id for j in patch["judgments"]] == ["J-FIN-03-1-M-D-CSH02-091"]
    failed = next(t for t in patch["review_tasks"] if t.kind is TaskKind.failed)
    assert (failed.id, failed.subject_id, failed.target_id) == (
        "T-RUN-FIN-03-1-M-D-XJ499-v1",
        "M-D-XJ499",
        "RUN-FIN-03-1",
    )
    assert deps.queue.allowed(failed) == {Decision.rerun, Decision.manual_result}
    assert patch["procedure_runs"][0].status is RunStatus.succeeded  # 个别对象失败不算整次失败
