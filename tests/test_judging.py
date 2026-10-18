"""判断公共流程 (agents/judging.py): 插入点顺序, 缺省步骤, 判断的自主度取程序运行时级别 (开发接口文档第 6 节)."""

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.fieldwork import FieldworkAgent, Subject
from backend.agents.judging import DEFAULT_STEPS, JudgingSteps, unchanged
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    CriticVerdict,
    Judgment,
    Phase,
    ProcedureRun,
    ReviewStatus,
    ReviewTask,
    TaskKind,
)
from backend.contracts.records import DatasetTraits
from backend.contracts.services import JudgmentContext, JudgmentOutcome, RuleResult
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
from tests.test_fieldwork_judging import failed, passed, setup
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
    """A7 的替身: 记下调用, 写上 consistent."""

    def __init__(self, calls: list[Call]) -> None:
        self.calls = calls

    async def review(self, ctx: RunContext, judgment: Judgment) -> Judgment:
        self.calls.append(("critic", judgment.subject_id))
        return judgment.model_copy(update={"critic_verdict": CriticVerdict.consistent, "critic_reason": "same"})


def recording_step(name: str, calls: list[Call]) -> Any:
    """记下调用与收到的判断的 critic 结论, 原样返回."""

    async def run(ctx: RunContext, judgment: Judgment) -> Judgment:
        calls.append((name, judgment.subject_id, judgment.critic_verdict.value))
        return judgment

    return run


class RecordingQueue(ReviewQueue):
    """ReviewQueue 的替身: 记下 classify 与 make_task; 有判断的一律分为 normal, 以便走到 make_task."""

    def __init__(self, queue: ReviewQueue, calls: list[Call]) -> None:
        super().__init__(queue.library)
        self.calls = calls

    def classify(self, template: ProcedureTemplate, outcome: JudgmentOutcome, rule_findings: Any) -> TaskKind | None:
        subject = outcome.judgment.subject_id if outcome.judgment is not None else "-"
        self.calls.append(("classify", subject))
        return TaskKind.normal if outcome.judgment is not None else super().classify(template, outcome, rule_findings)

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
    """test_fieldwork_judging.setup 的 A6, 判断引擎, A7, 两个步骤与 ReviewQueue 都换成记录调用的替身."""
    calls: list[Call] = []
    agent, ctx, _ = setup(procedure_id, subjects, outcomes)
    engine = RecordingEngine(outcomes, calls)
    agent.deps = replace(agent.deps, judgments=engine, queue=RecordingQueue(agent.deps.queue, calls))
    agent.reviewer = RecordingCritic(calls)
    agent.steps = JudgingSteps(downgrade=recording_step("downgrade", calls), blind=recording_step("blind", calls))
    return agent, ctx, calls


# ── 验收 1: 顺序 ──


async def test_steps_run_in_fixed_order() -> None:
    """一个 L3 accept 判断依次经过 judge, critic, downgrade, blind, classify, make_task; 降级与抽样收到的判断
    已带 critic 字段."""
    agent, ctx, calls = recorded("REV-03", [Subject("A", {})], {"A": passed(autonomy_level=AutonomyLevel.L3)})
    await agent.run(ctx)
    assert calls == [
        ("judge", "A"),
        ("critic", "A"),
        ("downgrade", "A", "consistent"),
        ("blind", "A", "consistent"),
        ("classify", "A"),
        ("make_task", "normal"),
    ]


async def test_failed_subjects_skip_critic_downgrade_and_blind() -> None:
    """判断失败的对象 (failed, unattributed) 只经 judge, classify, make_task; 同一执行里成功的对象照常走全程."""
    x1 = numbat_rev08_exception(make_run("REV-08"))
    subjects = [Subject("A", {}), Subject("F", {}), Subject("U", {}, [x1])]
    outcomes = {"A": passed(autonomy_level=AutonomyLevel.L3), "F": failed("failed"), "U": failed("unattributed")}
    agent, ctx, calls = recorded("REV-02", subjects, outcomes)
    ctx = RunContext(ctx.engagement, ctx.procedure, ctx.template, ctx.run, ctx.state | {"exceptions": [x1]})
    await agent.run(ctx)
    for subject in ("F", "U"):
        assert [c[0] for c in calls if c[1:2] == (subject,)] == ["judge"], subject
    assert [c[0] for c in calls if c[1:2] == ("A",)] == ["judge", "critic", "downgrade", "blind", "classify"]
    assert [c for c in calls if c[0] in ("classify", "make_task")] == [
        ("classify", "A"),
        ("make_task", "normal"),
        ("classify", "-"),
        ("make_task", "failed"),
        ("classify", "-"),
        ("make_task", "unattributed"),
    ]


@pytest.mark.parametrize(
    ("procedure_id", "make_exception"), [("REV-08", numbat_rev08_exception), ("CSH-05", numbat_csh05_exception)]
)
async def test_rule_programs_do_not_enter_judging(
    procedure_id: str, make_exception: Callable[[ProcedureRun], AuditException]
) -> None:
    """规则程序 (REV-08, CSH-05 的 B01) 只调规则引擎: 判断引擎, A7, 两个步骤与 classify 都不调用."""
    calls: list[Call] = []
    x1 = make_exception(make_run(procedure_id))
    rules = FakeRuleEngine({procedure_id: lambda run: RuleResult(exceptions=[x1], rows={})})
    deps = make_deps(FakeRegistry(), FakeTrail(), rules=rules)
    deps = replace(deps, judgments=RecordingEngine({}, calls), queue=RecordingQueue(deps.queue, calls))
    agent = FieldworkAgent(deps)
    agent.reviewer = RecordingCritic(calls)
    agent.steps = JudgingSteps(downgrade=recording_step("downgrade", calls), blind=recording_step("blind", calls))
    patch = await agent.run(make_ctx(deps, procedure_id))
    assert patch["exceptions"] == [x1] and calls == []


# ── 验收 2: 缺省步骤 ──


async def test_default_steps_return_judgment_unchanged() -> None:
    """缺省的降级与抽样原样返回 (同一对象); A6 缺省用它们."""
    _, ctx, _ = setup("REV-03", [], {})
    judgment = passed()(JudgmentContext(subject_id="A", task={}, data={}, fixture_key="A"), ctx.run).judgment
    assert JudgingSteps(downgrade=unchanged, blind=unchanged) == DEFAULT_STEPS
    assert await DEFAULT_STEPS.downgrade(ctx, judgment) is judgment
    assert await DEFAULT_STEPS.blind(ctx, judgment) is judgment
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
    params = deps.library.resolve_params(ctx.template, ctx.engagement, ctx.state, None, traits=DatasetTraits())
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
