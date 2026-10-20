"""需求 R177: A5 GEN-07 期后监控 (规则 S01-S03). 程序定义与功能开关, 参数快照, A5 按程序编号分派, 例外原样返回,
规则出错由 A1 代写失败, 重放按程序取回. 规则引擎为预置结果的 FakeRuleEngine (Xero 的预置取自连库跑一次的结果, 连库
一致见 tests/test_rule_sql_gen07.py)."""

from datetime import UTC, date, datetime

import pytest

from backend.agents.evidence import EvidenceAgent
from backend.agents.registry import build_agents
from backend.contracts.errors import RuleError
from backend.contracts.models import (
    AgentId,
    Decision,
    EvidenceType,
    Phase,
    ProcedureRun,
    RunStatus,
    TaskKind,
    TrailKind,
)
from backend.contracts.records import DatasetTraits
from backend.contracts.services import RuleResult
from backend.contracts.state import initial_state
from backend.orchestration.merge import check_patch
from backend.orchestration.nodes import GraphNodes, build_run, stored_outputs
from backend.services.features import BASELINE
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_PROFILE,
    FakeOutputStore,
    FakeRegistry,
    FakeRuleEngine,
    FakeTrail,
    finished,
    make_ctx,
    make_deps,
    make_run,
)
from tests.test_xero_fixtures import xero_context, xero_state
from tests.xero_fakes import XERO_RULES, make_xero_deps, xero_gen07_exceptions

GOING_CONCERN = BASELINE | {"going_concern"}  # 基线加持续经营 (GEN-07)
XERO_PARAMS = {
    "period_end": date(2026, 9, 30),
    "subsequent_days": 31,
    "data_as_of": date(2026, 10, 17),
    "materiality_cents": 32431,
    "clearly_trivial_cents": 1622,
    "match_days": 10,
    "reversal_reference_pattern": r"Reversal of ID (\d+)",
    "reversal_entry_no_template": "XJ-{1}",
}


# ── 程序定义, 功能开关与参数 ──


def test_gen07_template_and_dispatch() -> None:
    """验收 1, 4, 8: GEN-07.yaml 的各项; 只在功能 going_concern 打开时启用, 进 P2 固定集, 映射到 EvidenceAgent (与
    CSH-04 同一实例); 关闭时 (基线) 不启用, 不装配, 注册表不映射."""
    deps = make_deps(FakeRegistry(), FakeTrail(), features=GOING_CONCERN)
    t = deps.library.load("GEN-07")
    assert (t.phase, t.cycle, t.owner, t.engine, t.autonomy_level, t.fixed, t.feature) == (
        Phase.P2_Executing,
        "Pervasive",
        AgentId.A5,
        "rule",
        "L4",
        True,
        "going_concern",
    )
    assert (t.triggers, t.depends_on, t.outputs) == ([], [], ["exceptions"])
    assert t.params == {
        "period_end": {"ref": "period_end"},
        "subsequent_days": 31,
        "data_as_of": {"ref": "dataset.data_as_of"},
        "materiality_cents": {"ref": "materiality_cents"},
        "clearly_trivial_cents": {"ref": "clearly_trivial_cents"},
        "match_days": 10,
        "reversal_reference_pattern": {"ref": "dataset.reversal_reference_pattern"},
        "reversal_entry_no_template": {"ref": "dataset.reversal_entry_no_template"},
    }
    assert (t.standards, t.evidence_required) == (
        ["ASA560-P06", "ASA560-P07"],
        [EvidenceType.bank, EvidenceType.voucher],
    )
    assert [(r.id, r.severity, r.emit) for r in t.rules] == [
        ("S01", "medium", "exception"),
        ("S02", "high", "exception"),
        ("S03", "medium", "exception"),
    ]
    assert EvidenceAgent.procedures == {"CSH-04", "GEN-07", "GEN-08"}
    agents = build_agents(deps)
    assert isinstance(agents["GEN-07"], EvidenceAgent) and agents["GEN-07"] is agents["CSH-04"]
    assert "GEN-07" in {x.id for x in deps.library.fixed()}
    baseline = make_deps(FakeRegistry(), FakeTrail())
    assert not baseline.library.enabled("GEN-07") and "GEN-07" not in build_agents(baseline)
    assert "GEN-07" not in {x.id for x in baseline.library.fixed()}


@pytest.mark.external
def test_xero_params_snapshot() -> None:
    """验收 1: Xero 项目 (重要性 32431 / 16216 / 1622) 的第 1 次执行 RUN-GEN-07-1, 参数按项目期间, 状态的重要性与数据集
    声明解析."""
    deps = make_xero_deps(features=GOING_CONCERN)
    ctx = xero_context(deps, xero_state(), "GEN-07", [])
    assert (ctx.run.id, ctx.run.params_snapshot) == ("RUN-GEN-07-1", XERO_PARAMS)


def test_numbat_takes_dataset_defaults_and_needs_materiality() -> None:
    """验收 1: 没有数据集声明 (Numbat) 时三个 dataset 参数取缺省: data_as_of 不设上限, 没有作废冲回格式; 重要性还没
    定时 build_run 抛 ValueError."""
    deps = make_deps(FakeRegistry(), FakeTrail(), features=GOING_CONCERN)
    ctx = make_ctx(deps, "GEN-07")
    procedure = ctx.procedure.model_copy(update={"phase": Phase.P2_Executing})
    state = initial_state(ENGAGEMENT, [], NUMBAT_PROFILE.version)

    def run(materiality: dict[str, int]) -> ProcedureRun:
        return build_run(
            deps.library,
            ctx.template,
            procedure,
            ENGAGEMENT,
            state | materiality,  # type: ignore[arg-type]
            datetime.now(UTC),
            traits=DatasetTraits(),
            profile=NUMBAT_PROFILE,
        )

    params = run({"materiality_cents": 12000000, "clearly_trivial_cents": 600000}).params_snapshot
    assert (params["data_as_of"], params["reversal_reference_pattern"], params["reversal_entry_no_template"]) == (
        None,
        None,
        None,
    )
    assert (params["period_end"], params["materiality_cents"]) == (ENGAGEMENT.period_end, 12000000)
    with pytest.raises(ValueError, match="materiality_cents 尚未确定"):
        run({})


# ── A5 执行: 例外原样返回 ──


@pytest.mark.external
async def test_xero_returns_the_nine_exceptions_as_is() -> None:
    """验收 3: 规则引擎预置 Xero 的 9 条 (连库结果), 直接调 A5: 片段恰为这 9 条 (v1, open) 与成功的执行记录, 没有判断
    与复核任务, 经 A5 的合并校验; 规则引擎收到的参数就是参数快照; 不调大模型, 也没有外部调用."""
    rules = FakeRuleEngine(XERO_RULES)
    trail = FakeTrail()
    deps = make_xero_deps(trail=trail, rules=rules, features=GOING_CONCERN)
    ctx = xero_context(deps, xero_state(), "GEN-07", [])
    patch = await EvidenceAgent(deps).run(ctx)
    [run] = patch["procedure_runs"]
    assert (run.id, run.status, run.rule_outcomes) == ("RUN-GEN-07-1", RunStatus.succeeded, [])
    assert run.ended_at is not None
    exceptions = xero_gen07_exceptions(run)
    assert patch == {"exceptions": exceptions, "procedure_runs": [run]}
    assert [(x.id, x.source_ids, x.amount_cents) for x in exceptions] == [
        ("X-GEN-07-1-S01-1", ["BT-090-0092"], 618750),
        ("X-GEN-07-1-S01-2", ["BT-090-0097"], -450000),
        ("X-GEN-07-1-S01-3", ["BT-090-0099"], 200000),
        ("X-GEN-07-1-S01-4", ["BT-090-0107"], 100000),
        ("X-GEN-07-1-S01-5", ["BT-090-0130"], 150000),
        ("X-GEN-07-1-S01-6", ["BT-090-0131"], 45000),
        ("X-GEN-07-1-S02-1", ["XJ-720-2"], 115500),
        ("X-GEN-07-1-S03-1", ["XJ-626-2"], 630000),
        ("X-GEN-07-1-S03-2", ["XJ-676-2"], 196999),
    ]
    assert {(x.version, x.disposition.value, x.detail["window_end"]) for x in exceptions} == {(1, "open", "2026-10-17")}
    check_patch(
        patch, writer=AgentId.A5, procedure_id="GEN-07", run_id=run.id, state=ctx.state, outputs=ctx.template.outputs
    )
    assert rules.calls == [("GEN-07", run.id, XERO_PARAMS)]
    assert [e for e in trail.entries if e.kind in (TrailKind.llm_call, TrailKind.external_call)] == []


async def test_no_hits_is_a_succeeded_run_without_exceptions() -> None:
    """验收 3: 没有命中时例外为空, 执行成功."""
    deps = make_deps(FakeRegistry(), FakeTrail(), rules=FakeRuleEngine({}), features=GOING_CONCERN)
    patch = await EvidenceAgent(deps).run(make_ctx(deps, "GEN-07"))
    [run] = patch["procedure_runs"]
    assert (patch["exceptions"], run.status) == ([], RunStatus.succeeded)


async def test_rule_error_becomes_failed_run_and_rerun_task_by_a1() -> None:
    """验收 3: 规则 SQL 出错时 RuleError 原样上抛; 编排节点由 A1 代写失败的执行记录与 failed 任务, 只能 rerun."""
    error = RuleError("GEN-07/S01: statement timeout")

    def fail(run: ProcedureRun) -> RuleResult:
        raise error

    deps = make_deps(FakeRegistry(), FakeTrail(), rules=FakeRuleEngine({"GEN-07": fail}), features=GOING_CONCERN)
    agent, ctx = EvidenceAgent(deps), make_ctx(deps, "GEN-07")
    with pytest.raises(RuleError) as caught:
        await agent.run(ctx)
    assert caught.value is error
    patch, writer = await GraphNodes(deps, parallel_cap=2).run_agent(agent, ctx)
    assert writer is AgentId.A1
    [run] = patch["procedure_runs"]
    [task] = patch["review_tasks"]
    assert (run.id, run.status, run.failure_reason) == (
        "RUN-GEN-07-1",
        RunStatus.failed,
        "RuleError: GEN-07/S01: statement timeout",
    )
    assert (task.id, task.kind, task.target_id) == ("T-RUN-GEN-07-1-v1", TaskKind.failed, run.id)
    assert deps.queue.allowed(task) == {Decision.rerun}


async def test_unregistered_procedure_is_not_implemented() -> None:
    """验收 4: A5 只执行登记了的程序 (CSH-04, GEN-07, GEN-08), 其余抛 NotImplementedError, 不再落到函证."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    with pytest.raises(NotImplementedError, match="CSH-05"):
        await EvidenceAgent(deps).run(make_ctx(deps, "CSH-05"))


async def test_csh04_replay_does_not_take_gen07_exceptions() -> None:
    """验收 4: 重放按程序取回. CSH-04 只声明函证与回函, 片段不带例外 (Store 里有 GEN-07 的例外也不串进来); GEN-07 的
    重放只取本次执行的例外."""
    csh04, gen07 = finished(make_run("CSH-04")), finished(make_run("GEN-07"))
    exceptions = xero_gen07_exceptions(gen07)
    deps = make_deps(FakeRegistry(), FakeTrail(), output_store=FakeOutputStore([csh04, gen07, *exceptions]))
    replayed = await stored_outputs(deps, ENGAGEMENT.id, deps.library.load("CSH-04"), csh04)
    assert replayed.get("exceptions", []) == [] and set(replayed) == {"procedure_runs", "confirmations", "documents"}
    replayed = await stored_outputs(deps, ENGAGEMENT.id, deps.library.load("GEN-07"), gen07)
    assert replayed == {"procedure_runs": [gen07], "exceptions": exceptions}
