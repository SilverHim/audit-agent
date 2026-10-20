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
    MisstatementType,
    MisstatementUpdate,
    OpinionType,
    Phase,
    RerunRequest,
    ReviewStatus,
    ReviewTask,
    RiskLevel,
    RunStatus,
    TargetType,
    TaskKind,
    TrailKind,
    UncorrectedSchedule,
)
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.gates import fin01_view
from backend.orchestration.merge import check_patch
from backend.orchestration.nodes import build_run
from backend.services.presentation import failure_of
from tests.fakes import NOW, FakeTrail, ScriptedLLM, finished, make_ctx, make_run, xero_document
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
    """验收 3: 上下文为错报, 来源判断 (结论与证据), 两次分开取的分录行, 来源判断其余指针所指的行, 所引文件的登记,
    舞弊相关风险; 各项的键同 FIN-03.md.j2 头注释. 分录行按凭证号与按原始单据各取一次. 不含别的程序的判断 (REV-03 对
    CON-X01 的判断在状态里, 不进提示词). 另造三处数据以区分对错:
    - 按原始单据另取到一行 (合成 XJ-101-1, source_doc XJ-499, 行 ID 排在前): 进 journal_lines 并排在最前;
    - BA-091 的来源判断另引一个不存在的试算表科目 999: cited_rows 不列它 (取不到的不列), 不报错;
    - BA-091 的回函不登记: documents 不列它 (没登记的不列), 不报错."""
    trail = FakeTrail()
    deps = fin03_deps(trail)
    deps.financial_store.document_refs.remove(xero_document(REPLY))  # type: ignore[attr-defined]
    recorder = RecordingJudgments(deps.judgments)
    deps = replace(deps, judgments=recorder)
    calls: list[dict[str, Any]] = []
    store = deps.financial_store
    original = store.journal_entries

    async def journal_entries(eid: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        if kwargs == {"source_docs": ["XJ-499"]}:
            [line] = [x for x in await original(eid, entry_nos=["XJ-601"]) if x.id == "XJ-601-2"]
            return [line.model_copy(update={"id": "XJ-101-1", "entry_no": "XJ-101", "source_doc": "XJ-499"})]
        return await original(eid, **kwargs)

    store.journal_entries = journal_entries  # type: ignore[method-assign]
    judgments = []
    for j in fin03_state()["judgments"]:
        if j.id == "J-CSH-02-1-BA-091":
            balance = next(p for p in j.evidence if p.source_type == "trial_balance")
            j = j.model_copy(update={"evidence": [*j.evidence, balance.model_copy(update={"source_id": "999"})]})
        judgments.append(j)
    state = fin03_state(judgments=judgments)
    await run_fin03(deps, state)

    assert calls == [
        {"entry_nos": ["XJ-499"]},
        {"source_docs": ["XJ-499"]},
        {"entry_nos": ["BA-091"]},
        {"source_docs": ["BA-091"]},
    ]
    xj499, ba091 = (c.data for c in recorder.contexts)
    assert [c.fixture_key for c in recorder.contexts] == FIXTURE_KEYS
    assert set(xj499["misstatement"]) == {
        "id", "version", "judgment_id", "type", "account", "account_code", "assertion", "amount_cents", "direction",
        "status",
    }  # fmt: skip
    assert (xj499["misstatement"]["id"], xj499["misstatement"]["amount_cents"]) == ("M-D-XJ499", 75000)
    source = next(j for j in state["judgments"] if j.id == "J-GEN-02-1-XJ-499")
    assert xj499["source_judgment"] == source.model_dump(
        mode="json", include={"id", "procedure_id", "subject_id", "conclusion", "evidence"}
    )
    assert [r["id"] for r in xj499["journal_lines"]] == ["XJ-101-1", "XJ-499-1", "XJ-499-2", "XJ-499-3"]
    assert {frozenset(r) for r in xj499["journal_lines"]} == {
        frozenset({
            "source_type", "id", "entry_no", "posting_date", "created_by", "approved_by", "account_code",
            "debit_cents", "credit_cents", "description", "source_doc",
        })
    }  # fmt: skip
    assert [(r["source_type"], r["account_code"]) for r in xj499["cited_rows"]] == [("trial_balance", "710")]
    register = xero_document("documents/evidence/fixed_asset_register.pdf")
    assert xj499["documents"] == [
        register.model_dump(mode="json", include={"id", "file_name", "type", "page_count", "reliability"})
    ]
    assert ba091["journal_lines"] == []
    assert [(r["source_type"], r.get("id") or r.get("account_code")) for r in ba091["cited_rows"]] == [
        ("trial_balance", "091"),
        ("journal_entry", "XJ-601-1"),
    ]
    assert ba091["documents"] == []
    risks = [r for r in state["risk_register"] if r.fraud_related]
    expected = [r.model_dump(mode="json", include={"id", "assertion", "level", "description"}) for r in risks]
    assert xj499["fraud_risks"] == ba091["fraud_risks"] == expected
    prompts = [m["content"] for e in llm_calls(trail) for m in e.payload["messages"]]
    assert prompts and not any("CON-X01" in p for p in prompts)


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


def objective(oid: str, account: str, assertion: Assertion) -> AuditObjective:
    return AuditObjective(id=oid, account=account, assertion=assertion, risk_level=RiskLevel.high, is_material=True)


REV_CUT = objective("OBJ-A", "Revenue", Assertion.Cutoff)
SALES_CUT = objective("OBJ-B", "Sales revenue", Assertion.Cutoff)


@pytest.mark.parametrize(
    ("account", "assertion", "objectives", "expected"),
    [
        ("Revenue", Assertion.Cutoff, OBJECTIVES, "OBJ-REV-CUT"),
        ("Sales revenue (200)", Assertion.Occurrence, OBJECTIVES, "OBJ-REV-OCC"),  # 整词出现, 不分大小写
        ("Revenue", Assertion.Existence, OBJECTIVES, None),  # 认定不同
        ("Revenues", Assertion.Cutoff, OBJECTIVES, None),  # 后面不是整词
        ("Noncash expenses", Assertion.Existence, OBJECTIVES, None),  # 前面不是整词
        ("Interest income (270)", Assertion.Completeness, OBJECTIVES, None),
        ("Petty cash", Assertion.Existence, OBJECTIVES, "OBJ-CASH-EXI"),
        ("Sales revenue", Assertion.Cutoff, [REV_CUT, SALES_CUT], "OBJ-A"),  # 都配得上时取状态顺序的第一条
        ("Sales revenue", Assertion.Cutoff, [SALES_CUT, REV_CUT], "OBJ-B"),
        # 科目名首尾是标点时也算整词, 括号按字面比 (第 1 轮审核 R158-08)
        ("Cash (bank)", Assertion.Existence, [objective("OBJ-X", "Cash (bank)", Assertion.Existence)], "OBJ-X"),
        ("Cash bank", Assertion.Existence, [objective("OBJ-X", "Cash (bank)", Assertion.Existence)], None),
    ],
)
def test_misstatement_objective_rule(
    account: str, assertion: Assertion, objectives: list[AuditObjective], expected: str | None
) -> None:
    """验收 2: 审计目标的科目名作整词出现在错报科目里 (不分大小写) 且认定相同的第一条; 配不上为空. 不取大模型.
    接进载荷见 test_payload_type_from_model_objective_from_rule."""
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
    assert misstatement_objective(misstatement, objectives) == expected


@pytest.mark.external
async def test_payload_type_from_model_objective_from_rule() -> None:
    """验收 2: 载荷的类型取模型 (XJ-499 认定时为 factual, 模型判 judgmental), 审计目标取规则 (状态另有一条配得上
    XJ-499 的目标 Office equipment / Existence, BA-091 仍配不上)."""
    raw = json.loads(fixture_raw("XJ-499"))
    raw["nature"] = {"type": "judgmental", "fraud_indicative": False}
    llm = ScriptedLLM({"XJ-499": [json.dumps(raw)], "BA-091": [fixture_raw("BA-091")]})
    state = fin03_state()
    ppe = objective("OBJ-PPE-EXI", "Office equipment", Assertion.Existence)
    state = fin03_state(audit_objectives=[*state["audit_objectives"], ppe])
    patch, _ = await run_fin03(fin03_deps(FakeTrail(), llm), state)

    assert state["misstatements"][0].type is MisstatementType.factual
    updates = [j.misstatement_update for j in patch["judgments"]]
    assert [(u.type, u.objective_id) for u in updates if u is not None] == [
        (MisstatementType.judgmental, "OBJ-PPE-EXI"),
        (MisstatementType.factual, None),
    ]


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


SUPERSEDED = RerunRequest(  # 过期重跑取代了 XJ-499 来源判断的那次执行
    id="RR-1",
    procedure_ids=["GEN-02"],
    superseded_run_ids=["RUN-GEN-02-1"],
    data_version=2,
    requested_by="U-1",
    requested_at=NOW,
)


def awaiting_review(state: EngagementState) -> dict[str, Any]:
    """M-D-XJ499 已交人处理: FIN-03 第 1 次执行对它的判断仍待复核 (normal 任务待决), 本次为第 2 次执行."""
    source = next(j for j in state["judgments"] if j.id == "J-GEN-02-1-XJ-499")
    judged = source.model_copy(
        update={
            "id": "J-FIN-03-1-M-D-XJ499",
            "version": 1,
            "procedure_id": FIN03,
            "run_id": "RUN-FIN-03-1",
            "subject_id": "M-D-XJ499",
            "review_status": ReviewStatus.pending,
        }
    )
    task = ReviewTask(
        id="T-J-FIN-03-1-M-D-XJ499-v1",
        kind=TaskKind.normal,
        procedure_id=FIN03,
        target_type=TargetType.Judgment,
        target_id=judged.id,
        target_version=1,
        run_id="RUN-FIN-03-1",
        created_at=NOW,
    )
    first = finished(make_run(FIN03))
    return {"judgments": [*state["judgments"], judged], "review_tasks": [task], "procedure_runs": [first]}


@pytest.mark.external
@pytest.mark.parametrize(
    ("changes", "rerun_subjects"),
    [
        (lambda state: {"rerun_requests": [SUPERSEDED]}, []),  # 来源判断不在当前结果中 (同 FIN-04)
        (lambda state: {}, ["M-D-CSH02-091"]),  # 只重判 BA-091 那条
        (awaiting_review, []),
    ],
    ids=["superseded", "rerun_subjects", "awaiting_review"],
)
async def test_subjects_filtered_before_fetching(changes: Any, rerun_subjects: list[str]) -> None:
    """判断对象只取当前结果, 并先按 rerun_subject_ids 与已交人处理的对象筛选再取数 (同 A6 各程序, 第 1 轮审核
    R158-04, R158-06, 第 2 轮 R158-12): 三种情形都只判 M-D-CSH02-091, 分录行也只为 BA-091 取."""
    trail = FakeTrail()
    deps = fin03_deps(trail)
    calls: list[dict[str, Any]] = []
    original = deps.financial_store.journal_entries

    async def journal_entries(eid: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return await original(eid, **kwargs)

    deps.financial_store.journal_entries = journal_entries  # type: ignore[method-assign]
    state = fin03_state()
    ctx = fin03_context(deps, state | changes(state))
    ctx = replace(ctx, procedure=ctx.procedure.model_copy(update={"rerun_subject_ids": rerun_subjects}))
    patch = await WorkpaperAgent(deps).run(ctx)

    assert [j.subject_id for j in patch["judgments"]] == ["M-D-CSH02-091"]
    assert [e.payload["fixture_key"] for e in llm_calls(trail)] == ["BA-091"]
    assert calls == [{"entry_nos": ["BA-091"]}, {"source_docs": ["BA-091"]}]


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
    ("replies", "attempts", "level"),
    [
        (["not json", "not json", "not json"], 3, "syntax"),  # 不是 JSON (语法级, 同结构级重试 2 次)
        (
            [
                json.dumps(
                    json.loads(fixture_raw("XJ-499")) | {"nature": {"type": "projected", "fraud_indicative": False}}
                )
            ],
            1,
            "semantic",
        ),
    ],
    ids=["syntax", "semantic"],
)
async def test_failed_classification_creates_failed_task_allowing_manual_result(
    replies: list[str], attempts: int, level: str
) -> None:
    """验收 7: 大模型连续 3 次语法或结构失败 (重试耗尽), 或引用, 语义失败 (只调 1 次, 如类型为 projected) 时, 该错报
    建带对象的 failed 任务 T-RUN-FIN-03-1-M-D-XJ499-v1, 允许 {rerun, manual_result} (FIN-03 开 manual_result_allowed,
    需求 R158); 另一条照常判断. 呈递的失败原因 (failure_of) 取该错报最后一次调用的级别, 次数与留痕."""
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
    [run] = patch["procedure_runs"]
    assert run.status is RunStatus.succeeded  # 个别对象失败不算整次失败
    # 失败卡片按任务的对象 (错报) 找到最后一次调用, 虽然 fixture_key 是来源判断的对象 (第 1 轮审核 R158-01)
    last = [e for e in llm_calls(trail) if e.payload["fixture_key"] == "XJ-499"][-1]
    failure = failure_of(failed, run, llm_calls(trail))
    assert failure is not None
    assert (failure.level_failed, failure.attempts, failure.trail_id) == (level, attempts, last.id)
