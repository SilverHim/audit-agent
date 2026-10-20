"""判断公共流程 (agents/judging.py): 插入点顺序, 缺省步骤, 判断的自主度取程序运行时级别 (开发接口文档第 6 节),
功能 autonomy 打开时 A7 之后的判断级降级 (需求 R170)."""

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.fieldwork import FieldworkAgent, Subject
from backend.agents.judging import (
    DEFAULT_STEPS,
    Judged,
    JudgingSteps,
    downgrade,
    judge_all,
    judgments_patch,
    unchanged,
)
from backend.contracts.knowledge import DowngradeRule, ProcedureTemplate
from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    CriticTrigger,
    CriticVerdict,
    DowngradeCondition,
    EvidenceType,
    Judgment,
    Misstatement,
    MisstatementUpdate,
    Phase,
    ProcedureRun,
    ReasoningStep,
    ReviewStatus,
    ReviewTask,
    StepKind,
    StepStatus,
    TaskKind,
)
from backend.contracts.records import DatasetTraits
from backend.contracts.services import JudgmentContext, JudgmentOutcome, RuleResult
from backend.services.features import BASELINE
from backend.services.review_queue import ReviewQueue
from tests.fakes import (
    NUMBAT_CONTRACTS,
    NUMBAT_OBJECTIVES,
    NUMBAT_RISKS,
    FakeFinancialStore,
    FakeJudgmentEngine,
    FakeRegistry,
    FakeRuleEngine,
    FakeTrail,
    ScriptedLLM,
    make_ctx,
    make_deps,
    make_run,
    numbat_contracts,
    numbat_csh05_exception,
    numbat_rev08_exception,
)
from tests.test_fieldwork_csh02 import setup as csh02_setup
from tests.test_fieldwork_judging import X1, failed, passed, setup
from tests.test_fieldwork_rev03 import EXTRACTED
from tests.test_review_agent import step, verdict

Call = tuple[str, ...]  # (步骤, 判断对象, 附加信息)


class RecordingEngine(FakeJudgmentEngine):
    """假判断引擎, 另把每次 judge 记进共用的调用表."""

    def __init__(self, outcomes: dict[str, Any], calls: list[Call]) -> None:
        super().__init__(outcomes)
        self.log = calls

    async def judge(
        self, eid: str, template: ProcedureTemplate, run: ProcedureRun, ctx: JudgmentContext, performed_by: str
    ) -> JudgmentOutcome:
        self.log.append(("judge", ctx.subject_id))
        return await super().judge(eid, template, run, ctx, performed_by)


class RecordingCritic:
    """A7 的替身: 记下调用, 写上给定的结论 (缺省 consistent)."""

    def __init__(self, calls: list[Call], verdict: CriticVerdict = CriticVerdict.consistent) -> None:
        self.calls = calls
        self.verdict = verdict
        self.findings: list[tuple[str, list[str]]] = []  # (判断对象, 收到的例外 ID)

    async def review(self, ctx: RunContext, judgment: Judgment, findings: Sequence[AuditException] = ()) -> Judgment:
        self.calls.append(("critic", judgment.subject_id))
        self.findings.append((judgment.subject_id, [x.id for x in findings]))
        return judgment.model_copy(update={"critic_verdict": self.verdict, "critic_reason": "same"})


def recording_step(name: str, calls: list[Call]) -> Any:
    """记下调用与收到的判断的 critic 结论, 原样返回."""

    async def run(ctx: RunContext, judgment: Judgment, findings: Sequence[AuditException]) -> Judgment:
        calls.append((name, judgment.subject_id, judgment.critic_verdict.value))
        return judgment

    return run


class RecordingQueue(ReviewQueue):
    """ReviewQueue 的替身: 记下 classify 与 make_task; 有判断的一律分为 normal, 以便走到 make_task."""

    def __init__(self, queue: ReviewQueue, calls: list[Call]) -> None:
        super().__init__(queue.library)
        self.calls = calls

    def classify(
        self, template: ProcedureTemplate, outcome: JudgmentOutcome, rule_findings: Any, *, key_evidence: bool = False
    ) -> TaskKind | None:
        subject = outcome.judgment.subject_id if outcome.judgment is not None else "-"
        self.calls.append(("classify", subject))
        if outcome.judgment is not None:
            return TaskKind.normal
        return super().classify(template, outcome, rule_findings, key_evidence=key_evidence)

    def make_task(
        self,
        kind: TaskKind,
        target: Judgment | AuditException | ProcedureRun,
        procedure_id: str,
        run_id: str,
        now: datetime,
        subject_id: str | None = None,
    ) -> ReviewTask:
        self.calls.append(("make_task", kind.value))
        return super().make_task(kind, target, procedure_id, run_id, now, subject_id)


def recorded(procedure_id: str, subjects: list[Subject], outcomes: dict[str, Any]) -> tuple[Any, Any, list[Call]]:
    """test_fieldwork_judging.setup 的 A6, 判断引擎, A7, 降级步骤与 ReviewQueue 都换成记录调用的替身."""
    calls: list[Call] = []
    agent, ctx, _ = setup(procedure_id, subjects, outcomes)
    engine = RecordingEngine(outcomes, calls)
    agent.deps = replace(agent.deps, judgments=engine, queue=RecordingQueue(agent.deps.queue, calls))
    agent.reviewer = RecordingCritic(calls)
    agent.steps = JudgingSteps(downgrade=recording_step("downgrade", calls))
    return agent, ctx, calls


# ── 验收 1: 顺序 ──


async def test_steps_run_in_fixed_order() -> None:
    """一个 L3 accept 判断依次经过 judge, critic, downgrade, classify, make_task; 降级收到的判断已带 critic 字段."""
    agent, ctx, calls = recorded("REV-03", [Subject("A", {})], {"A": passed(autonomy_level=AutonomyLevel.L3)})
    await agent.run(ctx)
    assert calls == [
        ("judge", "A"),
        ("critic", "A"),
        ("downgrade", "A", "consistent"),
        ("classify", "A"),
        ("make_task", "normal"),
    ]


async def test_failed_subjects_skip_critic_and_downgrade() -> None:
    """判断失败的对象 (failed, unattributed) 只经 judge, classify, make_task; 同一执行里成功的对象照常走全程."""
    x1 = numbat_rev08_exception(make_run("REV-08"))
    subjects = [Subject("A", {}), Subject("F", {}), Subject("U", {}, [x1])]
    outcomes = {"A": passed(autonomy_level=AutonomyLevel.L3), "F": failed("failed"), "U": failed("unattributed")}
    agent, ctx, calls = recorded("REV-02", subjects, outcomes)
    ctx = RunContext(ctx.engagement, ctx.procedure, ctx.template, ctx.run, ctx.state | {"exceptions": [x1]})
    await agent.run(ctx)
    for subject in ("F", "U"):
        assert [c[0] for c in calls if c[1:2] == (subject,)] == ["judge"], subject
    assert [c[0] for c in calls if c[1:2] == ("A",)] == ["judge", "critic", "downgrade", "classify"]
    assert [c for c in calls if c[0] in ("classify", "make_task")] == [
        ("classify", "A"),
        ("make_task", "normal"),
        ("classify", "-"),
        ("make_task", "failed"),
        ("classify", "-"),
        ("make_task", "unattributed"),
    ]


async def test_critic_gets_the_subjects_rule_findings() -> None:
    """R150: A7 收到该判断对象的规则例外 (A6 判断时的同一组), 第一轮校验复核方错报草稿的金额用."""
    x1 = numbat_rev08_exception(make_run("REV-08"))
    subjects = [Subject("A", {}, [x1]), Subject("B", {})]
    level = AutonomyLevel.L3
    agent, ctx, _ = recorded("REV-02", subjects, {"A": passed(autonomy_level=level), "B": passed(autonomy_level=level)})
    ctx = RunContext(ctx.engagement, ctx.procedure, ctx.template, ctx.run, ctx.state | {"exceptions": [x1]})
    await agent.run(ctx)
    assert agent.reviewer.findings == [("A", [x1.id]), ("B", [])]


@pytest.mark.parametrize(
    ("procedure_id", "make_exception"), [("REV-08", numbat_rev08_exception), ("CSH-05", numbat_csh05_exception)]
)
async def test_rule_programs_do_not_enter_judging(
    procedure_id: str, make_exception: Callable[[ProcedureRun], AuditException]
) -> None:
    """规则程序 (REV-08, CSH-05 的 B01) 只调规则引擎: 判断引擎, A7, 降级与 classify 都不调用."""
    calls: list[Call] = []
    x1 = make_exception(make_run(procedure_id))
    rules = FakeRuleEngine({procedure_id: lambda run: RuleResult(exceptions=[x1], rows={})})
    deps = make_deps(FakeRegistry(), FakeTrail(), rules=rules)
    deps = replace(deps, judgments=RecordingEngine({}, calls), queue=RecordingQueue(deps.queue, calls))
    agent = FieldworkAgent(deps)
    agent.reviewer = RecordingCritic(calls)
    agent.steps = JudgingSteps(downgrade=recording_step("downgrade", calls))
    patch = await agent.run(make_ctx(deps, procedure_id))
    assert patch["exceptions"] == [x1] and calls == []


# ── 验收 2: 缺省步骤 ──


async def test_default_steps_return_judgment_unchanged() -> None:
    """缺省的降级原样返回 (同一对象); A6 缺省用它."""
    _, ctx, _ = setup("REV-03", [], {})
    judgment = passed()(JudgmentContext(subject_id="A", task={}, data={}, fixture_key="A"), ctx.run).judgment
    assert JudgingSteps(downgrade=unchanged) == DEFAULT_STEPS
    assert await DEFAULT_STEPS.downgrade(ctx, judgment, []) is judgment
    agent, _, _ = setup("REV-03", [], {})
    assert agent.steps is DEFAULT_STEPS


# ── 验收 3: 判断的自主度取程序运行时级别 ──


def critic_script() -> ScriptedLLM:
    """CON-001 的两轮独立复核: 第一轮只引用判断给出的合同第 2 页与 REV-03 的准则片段, 第二轮 consistent."""
    page2 = {
        "type": "contract",
        "source_type": "document",
        "source_id": NUMBAT_CONTRACTS[0].id,
        "locator": "CON-001-T1",
        "page": 2,
    }
    standard = {"type": "standard", "source_type": "standard", "source_id": "AASB15-P31", "locator": "para 31"}
    independent = {
        "conclusion": "Control passes when the customer signs the delivery note.",
        "reasoning_chain": [
            step("fact", "Clause 5.1 passes title on the signed delivery note.", [page2]),
            step("standard", "Revenue is recognised when control transfers.", [standard]),
            step("inference", "Revenue is recognised on the signed delivery note.", from_steps=[0, 1]),
        ],
    }
    return ScriptedLLM({"CON-001-r1": [json.dumps(independent)], "CON-001-r2": [verdict()]})


def rev03(level: AutonomyLevel, review: ScriptedLLM | None) -> tuple[FieldworkAgent, RunContext]:
    """REV-03 只判 CON-001: 真实判断引擎 (mock 读 tests/fixtures/llm/REV-03), 模板 L2, Procedure 为 level."""
    financial = FakeFinancialStore(list(NUMBAT_CONTRACTS), contracts=numbat_contracts()[:1])
    deps = make_deps(FakeRegistry(), FakeTrail(), financial_store=financial, review_llm=review)
    ctx = make_ctx(deps, "REV-03")
    assert ctx.template.autonomy_level is AutonomyLevel.L2
    procedure = ctx.procedure.model_copy(
        update={"phase": Phase.P2_Executing, "objective_ids": ["OBJ-REV-OCC", "OBJ-REV-CUT"], "autonomy_level": level}
    )
    state = ctx.state | {
        "procedure_pack": [procedure],
        "audit_objectives": NUMBAT_OBJECTIVES,
        "risk_register": NUMBAT_RISKS,
        "documents": EXTRACTED,
    }
    params = deps.library.resolve_params(
        ctx.template, ctx.engagement, ctx.state, None, traits=DatasetTraits(), profile=None
    )
    run = ctx.run.model_copy(update={"params_snapshot": params})
    return FieldworkAgent(deps), RunContext(ctx.engagement, procedure, ctx.template, run, state)  # type: ignore[arg-type]


async def test_runtime_l3_above_template_l2_is_reviewed_and_confirmed() -> None:
    """Procedure L3, 模板 L2: 判断为 L3; A7 按 L3 复核, 第二轮 consistent, 判断 v1 即 confirmed, 不建任务."""
    review = critic_script()
    agent, ctx = rev03(AutonomyLevel.L3, review)
    patch = await agent.run(ctx)
    [j] = patch["judgments"]
    assert (j.id, j.version, j.autonomy_level) == ("J-REV-03-1-CON-001", 1, AutonomyLevel.L3)
    assert (j.critic_verdict, j.review_status) == (CriticVerdict.consistent, ReviewStatus.confirmed)
    assert patch["review_tasks"] == []
    assert [r.fixture_key for r in review.requests] == ["CON-001-r1", "CON-001-r2"]


async def test_runtime_l1_below_template_l2_skips_critic_and_gets_task() -> None:
    """Procedure L1, 模板 L2: 判断为 L1, 独立复核跳过, 建 normal 任务."""
    review = critic_script()
    agent, ctx = rev03(AutonomyLevel.L1, review)
    patch = await agent.run(ctx)
    [j] = patch["judgments"]
    assert (j.autonomy_level, j.critic_verdict, j.review_status) == (
        AutonomyLevel.L1,
        CriticVerdict.skipped,
        ReviewStatus.pending,
    )
    assert [(t.id, t.kind) for t in patch["review_tasks"]] == [("T-J-REV-03-1-CON-001-v1", TaskKind.normal)]
    assert review.requests == []


async def test_context_carries_runtime_level() -> None:
    """judge_all 把 Procedure 的运行时级别放进 JudgmentContext.autonomy_level."""
    agent, ctx, engine = setup("REV-03", [Subject("A", {})], {"A": passed()})
    procedure = ctx.procedure.model_copy(update={"autonomy_level": AutonomyLevel.L4})
    await agent.run(RunContext(ctx.engagement, procedure, ctx.template, ctx.run, ctx.state))
    [(_, jctx, _)] = engine.calls
    assert jctx.autonomy_level is AutonomyLevel.L4


# ── 需求 R170: 功能 autonomy 打开时的判断级降级 ──

AUTONOMY = BASELINE | {"autonomy"}
L1, L2, L3 = AutonomyLevel.L1, AutonomyLevel.L2, AutonomyLevel.L3
HIGH, OVER_PM = DowngradeCondition.high_risk, DowngradeCondition.amount_over_pm
LOW, UNSUPPORTED = DowngradeCondition.low_confidence, DowngradeCondition.unsupported_steps
FRAUD = DowngradeCondition.fraud_flag


@pytest.mark.parametrize(
    ("level", "assembled", "critic", "confidence", "expected"),
    [
        # 运行时 L3: A7 按程序级别照常复核 (主规则), 判一致后置信度计入复核分项再降级
        (L3, [], (CriticVerdict.consistent, CriticTrigger.main), 0.65, (L1, [OVER_PM, LOW])),
        # 运行时 L2 (装配时 high_risk −1): A7 跳过, 置信度只有 A6 的分项; 理由接着装配时的
        (L2, [HIGH], (CriticVerdict.skipped, None), 0.5625, (L1, [HIGH, OVER_PM, LOW])),
    ],
)
async def test_downgrade_takes_the_reviewed_judgment(
    level: AutonomyLevel,
    assembled: list[DowngradeCondition],
    critic: tuple[CriticVerdict, CriticTrigger | None],
    confidence: float,
    expected: tuple[AutonomyLevel, list[DowngradeCondition]],
) -> None:
    """验收 4: Numbat 的 J-CSH-02-1-BA-01 (真实判断引擎与 A7, mock; 功能 collab 记触发规则), 两条例外都是 -50,000 澳元,
    执行重要性设为 40,000 澳元. 判断级降级在 A7 之后: 收到的置信度是复核后的 (L3 时 0.65, 不是 0.5625), 金额超过
    执行重要性降到 L1, 置信度低于 0.7 另记 low_confidence; 降级不影响 A7 读的程序级别. CSH-02 always_review, 任务
    为 normal."""
    agent, ctx, _ = csh02_setup(real_engine=True, features=AUTONOMY | {"collab"})
    procedure = ctx.procedure.model_copy(update={"autonomy_level": level, "downgrade_reasons": assembled})
    state = ctx.state | {"performance_materiality_cents": 4_000_000}
    patch = await agent.run(RunContext(ctx.engagement, procedure, ctx.template, ctx.run, state))  # type: ignore[arg-type]
    [j] = patch["judgments"]
    assert j.id == "J-CSH-02-1-BA-01" and (j.critic_verdict, j.critic_trigger) == critic
    assert (j.confidence, j.autonomy_level, j.downgrade_reasons) == (confidence, *expected)
    assert [(t.target_id, t.kind) for t in patch["review_tasks"]] == [(j.id, TaskKind.normal)]


async def test_dispute_wins_over_downgrade() -> None:
    """验收 6: 争议优先. 合成 L3 程序 (REV-03 的模板改为 L3, unsupported_steps 覆盖为 −2) 的判断置信度 0.6, 推理链有
    unsupported 步骤: L3 −1 −2 越过下限, 截在 L1, 理由按规则表顺序 [low_confidence, unsupported_steps]; A7 判
    inconsistent, classify 先看争议, 任务为 dispute (降级不改任务类型)."""
    chain = [ReasoningStep(kind=StepKind.fact, text="x", status=StepStatus.unsupported)]
    outcomes = {"A": passed(confidence=0.6, reasoning_chain=chain)}
    agent, ctx, _ = setup("REV-03", [Subject("A", {})], outcomes, features=AUTONOMY)
    agent.reviewer = RecordingCritic([], CriticVerdict.inconsistent)
    rule = DowngradeRule(condition=UNSUPPORTED, at="judgment", delta=-2)
    template = ctx.template.model_copy(update={"autonomy_level": L3, "downgrade_rules": [rule]})
    procedure = ctx.procedure.model_copy(update={"autonomy_level": L3})
    patch = await agent.run(RunContext(ctx.engagement, procedure, template, ctx.run, ctx.state))
    [j] = patch["judgments"]
    assert (j.critic_verdict, j.autonomy_level, j.downgrade_reasons) == (
        CriticVerdict.inconsistent,
        L1,
        [LOW, UNSUPPORTED],
    )
    assert [(t.id, t.kind) for t in patch["review_tasks"]] == [("T-J-REV-03-1-A-v1", TaskKind.dispute)]


NEW = X1.model_copy(update={"id": "X-CSH-02-1-D04-1", "amount_cents": 50_000})  # 本次执行刚建的例外, 不在状态中
UPDATE = MisstatementUpdate(misstatement_id="M-1", base_version=1, kind="classification")
MISSTATEMENT = Misstatement.model_construct(id="M-1", version=1, amount_cents=-50_000)


FLAGGED = X1.model_copy(update={"amount_cents": None})  # 出自 REV-08 R01, 不带金额
FRAUD_RULES = frozenset({("REV-08", "R01")})  # 给策略标上 fraud_flag 的规则


@pytest.mark.parametrize(
    ("findings", "update", "pm", "fraud_rules", "expected"),
    [
        ([NEW], None, 16216, frozenset(), (L1, [OVER_PM])),  # 金额取该对象的规则例外 (本次执行刚建的也算)
        ([], UPDATE, 16216, frozenset(), (L1, [OVER_PM])),  # 错报更新所指的错报取状态中的当前版本, 按绝对值比
        ([NEW], None, None, frozenset(), (L3, [])),  # 执行重要性未定 (PLN-01/03 之前): amount_over_pm 不适用
        ([], None, 16216, frozenset(), (L3, [])),  # 没有涉及金额
        ([FLAGGED], None, None, FRAUD_RULES, (L1, [FRAUD])),  # 规则例外出自标了 fraud_flag 的规则: 命中 fraud_flag
    ],
)
async def test_downgrade_amount_sources(
    findings: list[AuditException],
    update: MisstatementUpdate | None,
    pm: int | None,
    fraud_rules: frozenset[tuple[str, str]],
    expected: tuple[AutonomyLevel, list[DowngradeCondition]],
) -> None:
    """downgrade 的判断金额与复核档位同一口径 (judgment_amount): 例外取收到的规则例外, 所指错报取状态, 执行重要性取
    状态; 收到的规则例外也交给 judgment_level 判舞弊规则. 运行时 L3, 置信度 0.9."""
    agent, ctx, _ = setup("REV-03", [], {}, features=AUTONOMY)
    agent.deps = replace(agent.deps, autonomy=replace(agent.deps.autonomy, fraud_rules=fraud_rules))
    jctx = JudgmentContext(subject_id="A", task={}, data={}, fixture_key="A", rule_findings=findings)
    judgment = passed(misstatement_update=update)(jctx, ctx.run).judgment
    procedure = ctx.procedure.model_copy(update={"autonomy_level": L3})
    state = ctx.state | {"performance_materiality_cents": pm, "misstatements": [MISSTATEMENT]}
    run_ctx = RunContext(ctx.engagement, procedure, ctx.template, ctx.run, state)  # type: ignore[arg-type]
    result = await downgrade(agent.deps, run_ctx, judgment, findings)
    assert (result.id, result.version, result.autonomy_level, result.downgrade_reasons) == (judgment.id, 1, *expected)


@pytest.mark.parametrize(("features", "kind"), [(AUTONOMY, TaskKind.normal), (BASELINE, None)])
def test_judgments_patch_routes_missing_key_evidence(features: frozenset[str], kind: TaskKind | None) -> None:
    """judgments_patch 的 classify 按 judging_steps 传 key_evidence: 模板要 bank 证据 (REV-03 改出, 不复核), 判断 L3
    accept 置信度 0.9 而推理链没有指针. 功能 autonomy 打开时关键证据缺失交人 (normal 任务, 判断待复核), 关闭时
    自动关闭 (判断 confirmed, 不建任务)."""
    agent, ctx, _ = setup("REV-03", [], {}, features=features)
    template = ctx.template.model_copy(update={"evidence_required": [EvidenceType.bank], "always_review": False})
    ctx = RunContext(ctx.engagement, ctx.procedure, template, ctx.run, ctx.state)
    jctx = JudgmentContext(subject_id="A", task={}, data={}, fixture_key="A", rule_findings=[])
    outcome = passed(autonomy_level=L3, reasoning_chain=[])(jctx, ctx.run)
    patch = judgments_patch(agent.deps, ctx, [Judged(jctx, outcome)])
    assert [t.kind for t in patch["review_tasks"]] == ([kind] if kind else [])
    expected = ReviewStatus.pending if kind else ReviewStatus.confirmed
    assert [j.review_status for j in patch["judgments"]] == [expected]


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"conclusion": "annotated"}, None),  # 补程序自己的字段
        ({"id": "J-OTHER"}, "id"),
        ({"autonomy_level": AutonomyLevel.L1}, "autonomy_level"),
        ({"critic_verdict": CriticVerdict.consistent}, "critic_verdict"),
    ],
)
async def test_annotate_only_fills_successful_judgments(change: dict[str, Any], error: str | None) -> None:
    """judge_all 的 annotate (需求 R170, FIN-03 写分类载荷用): 判断引擎之后, A7 之前调用, 只对成功的判断 (失败的对象
    不调); 补的字段之后的复核, 降级与分类都看得到. 改了 ID, 版本, 级别或 critic 字段时断言失败."""
    subjects = [Subject("A", {}), Subject("F", {})]
    agent, ctx, calls = recorded("REV-03", subjects, {"A": passed(autonomy_level=L3), "F": failed("failed")})

    def annotate(outcome: JudgmentOutcome) -> Judgment:
        assert outcome.judgment is not None
        calls.append(("annotate", outcome.judgment.subject_id))
        return outcome.judgment.model_copy(update=change)

    if error is not None:
        with pytest.raises(AssertionError, match=error):
            await judge_all(agent.deps, ctx, subjects, agent.reviewer, agent.steps, annotate=annotate)
        return
    judged = await judge_all(agent.deps, ctx, subjects, agent.reviewer, agent.steps, annotate=annotate)
    assert [c[0] for c in calls if c[1:2] == ("A",)] == ["judge", "annotate", "critic", "downgrade"]
    assert [c[0] for c in calls if c[1:2] == ("F",)] == ["judge"]
    [annotated] = [item.outcome.judgment for item in judged if item.outcome.judgment is not None]
    assert annotated.conclusion == "annotated"
