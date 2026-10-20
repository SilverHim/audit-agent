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
from tests.fakes import FakeRegistry, FakeRuleEngine, FakeTrail, make_ctx, make_deps
from tests.test_knowledge import design_traits
from tests.test_xero_fixtures import xero_context, xero_state
from tests.xero_fakes import XERO_RULES, make_xero_deps


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
    patch, writer = await GraphNodes(deps, parallel_cap=2).run_agent(agent, ctx)
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
