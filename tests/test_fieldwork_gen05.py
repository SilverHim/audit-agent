"""需求 R131, R132: A6 执行 GEN-05 全量分录扫描 (原有规则 R01-R08, 期末后补记 R13 与新规则 R09-R12). 规则引擎的例外
原样返回, 不作判断, 不建复核任务; 没有执行的规则 (执行前检查命中) 按 YAML 顺序记进执行记录. 规则引擎为预置结果的
FakeRuleEngine (Xero 与 Numbat 的预置取自连库跑一次的结果, 连库一致见 tests/test_gen05_sql.py); 例外原样返回的通用
写法见 test_fieldwork_rules."""

from datetime import date

import pytest

from backend.agents.fieldwork import FieldworkAgent
from backend.contracts.errors import RuleError
from backend.contracts.models import AgentId, Decision, ProcedureRun, RunStatus, TaskKind
from backend.contracts.services import RuleResult
from backend.orchestration.nodes import GraphNodes
from tests.fakes import FakeRegistry, FakeRuleEngine, FakeTrail, make_ctx, make_deps, outside_populations
from tests.test_knowledge import design_traits
from tests.test_xero_fixtures import xero_context
from tests.xero_fakes import XERO_RULES, make_xero_deps, xero_state


@pytest.mark.external
async def test_xero_records_the_rules_not_run_in_yaml_order() -> None:
    """R131 验收 5, R132 验收 9: Xero 演示上新规则的 7 条例外原样返回 (原有规则没有例外); 不执行的 6 条按 YAML 顺序
    记进执行记录 (R09-R12 都执行), 看录入时间的规则的理由为设计稿第 5.2 节的 entry_timestamps_note. 规则引擎收到的
    参数就是参数快照 (按状态的重要性, 画像的审批限额与数据集声明解析); 片段只有例外与执行记录, 没有判断与复核任务."""
    rules = FakeRuleEngine(XERO_RULES)
    deps = make_xero_deps(rules=rules)
    ctx = xero_context(deps, xero_state(), "GEN-05", [])
    patch = await FieldworkAgent(deps).run(ctx)
    [run] = patch["procedure_runs"]
    assert (run.id, run.status) == ("RUN-GEN-05-1", RunStatus.succeeded) and run.ended_at is not None
    assert [(o.rule_id, o.status) for o in run.rule_outcomes] == [
        ("R01", "skipped"),
        ("R04", "not_executable"),
        ("R06", "skipped"),
        ("R07", "not_executable"),
        ("R08", "skipped"),
        ("R13", "skipped"),
    ]
    assert run.rule_outcomes[0].reason == design_traits().entry_timestamps_note
    exceptions = XERO_RULES["GEN-05"](run).exceptions
    assert [x.id for x in exceptions] == [
        *(f"X-GEN-05-1-{rule}-{n}" for rule in ("R09", "R10", "R11") for n in (1, 2)),
        "X-GEN-05-1-R12-1",
    ]
    assert patch == {"exceptions": exceptions, "procedure_runs": [run]}
    assert rules.calls == [("GEN-05", run.id, run.params_snapshot)]
    params = run.params_snapshot
    assert (
        params["approval_limit_cents"],
        params["performance_materiality_cents"],
        params["clearly_trivial_cents"],
        params["period_end"],
        params["entry_timestamps_reliable"],
    ) == (500000, 16216, 1622, date(2026, 9, 30), False)


@pytest.mark.external
async def test_xero_populations_are_period_lines() -> None:
    """需求 R328 验收 1: Xero 上执行了的 7 条规则各记总体, 跳过的时间类规则与不可执行的 R04, R07 不记; 分录类规则一行
    一项, 全部为本期 582 行 (215 笔, Xero演示数据集设计第 1 节), 剔除作废对的 R02, R09-R11 为 424 行 (少 33 对共 66
    笔); R12 为本期 26 张发票, 一张一项. 例外仍为 7 条, 都落在其规则的总体里."""
    deps = make_xero_deps(rules=FakeRuleEngine(XERO_RULES))
    ctx = xero_context(deps, xero_state(), "GEN-05", [])
    output = await FieldworkAgent(deps).outcome(ctx)
    [run] = output.patch["procedure_runs"]
    populations = output.populations or []
    counts = {p.rule_id: (p.source_type, p.count) for p in populations}
    lines, unvoided = ("journal_entry", 582), ("journal_entry", 424)
    assert counts == {
        "R02": unvoided,
        "R03": lines,
        "R05": lines,
        "R09": unvoided,
        "R10": unvoided,
        "R11": unvoided,
        "R12": ("sales_invoice", 26),
    }
    entries = {p.rule_id: len({i.fields["entry_no"] for i in p.items}) for p in populations if p.rule_id != "R12"}
    assert entries == {"R02": 149, "R03": 215, "R05": 215, "R09": 149, "R10": 149, "R11": 149}
    result = XERO_RULES["GEN-05"](run)
    assert (len(result.exceptions), outside_populations(result)) == (7, [])
    # 守卫本身: 清空 R09 的总体后, 它的两条例外都在总体之外
    emptied = [p.model_copy(update={"items": []}) if p.rule_id == "R09" else p for p in result.populations]
    outside = outside_populations(result.model_copy(update={"populations": emptied}))
    assert outside == ["X-GEN-05-1-R09-1", "X-GEN-05-1-R09-2"]


async def test_rule_error_becomes_failed_run_and_rerun_task_by_a1() -> None:
    """验收 6: 规则 SQL 出错时 RuleError 原样上抛; 编排节点由 A1 代写失败的执行记录与 failed 任务, 只能 rerun."""
    error = RuleError("GEN-05/R01: statement timeout")

    def fail(run: ProcedureRun) -> RuleResult:
        raise error

    deps = make_deps(FakeRegistry(), FakeTrail(), rules=FakeRuleEngine({"GEN-05": fail}))
    agent, ctx = FieldworkAgent(deps), make_ctx(deps, "GEN-05")
    with pytest.raises(RuleError) as caught:
        await agent.run(ctx)
    assert caught.value is error
    patch, writer, _ = await GraphNodes(deps, parallel_cap=2).run_agent(agent, ctx)
    assert writer is AgentId.A1
    [run] = patch["procedure_runs"]
    [task] = patch["review_tasks"]
    assert (run.id, run.status, run.failure_reason) == (
        "RUN-GEN-05-1",
        RunStatus.failed,
        "RuleError: GEN-05/R01: statement timeout",
    )
    assert (task.id, task.kind, task.target_id) == ("T-RUN-GEN-05-1-v1", TaskKind.failed, run.id)
    assert deps.queue.allowed(task) == {Decision.rerun}
