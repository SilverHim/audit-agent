"""需求 R107: Xero 演示项目 (xero/20261017-q1, 期间 2026-07-01 至 09-30) 上规则程序与重要性闸门的结果. 规则引擎为预置
XERO_RULES 的 FakeRuleEngine (取自连库跑一次的结果, 连库一致见 R112); Agent, 参数解析, 编排与闸门校验都是真实的, 存储为
Xero 假存储 (演示数据导入之后的状态). 读快照与补造目录, 标 external.

整段只跑一遍 (模块级 fixture), 各用例看停点留下的状态: 启动 → ACC-01 approve → P1 (DOC-04 沿用上传时抽出的合同,
PLN-07, PLN-01) → 停在 PLN 闸门 → 以税前利润为基准的决定被拒 → 以收入为基准的决定通过 → 组好程序包后停在 schedule 前.
P2 只核规则程序: REV-08, CSH-05 以闸门之后的状态经 GraphNodes.perform 执行一次 (同 fieldwork 实例里的执行, 不写 Store);
REV-02 只建执行记录看参数快照 (它的判断要 R110 的固定输出). 其余 P2 程序 (REV-03, DOC-06, CSH-02) 要 Xero 固定输出
(R109, R111), 本条不跑.
"""

import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.adapters.llm import MockLLMClient
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AgentId,
    ExceptionDisposition,
    GateDecision,
    GateId,
    GateVerdict,
    MaterialityChoice,
    ProcedureRun,
    RunStatus,
)
from backend.contracts.state import StagedResult
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.gates import gate_nodes
from backend.orchestration.graph import build_graph
from backend.orchestration.nodes import GraphNodes, build_run
from backend.orchestration.runner import GraphRunner
from tests.fakes import ENGAGEMENT, NOW, FakeFinancialStore, FakeRuleEngine, wait_idle
from tests.xero_fakes import (
    ORC1026,
    REPO,
    XERO_DATASET,
    XERO_RULES,
    make_xero_deps,
    xero_financial_store,
    xero_presets,
)
from tests.xero_flow import approve_acceptance, to_acceptance_gate

pytestmark = pytest.mark.external
EID = ENGAGEMENT.id
ACTOR = ENGAGEMENT.owner_id
LLM_XERO = REPO / "tests" / "fixtures" / "llm_xero"
REVENUE = MaterialityChoice(basis="revenue", pct=0.01, performance_pct=0.5, clearly_trivial_pct=0.05)
LOSS = MaterialityChoice(basis="profit_before_tax", pct=0.05, performance_pct=0.5, clearly_trivial_pct=0.05)
PERIOD_END = date(2026, 9, 30)


def pln_decision(decision_id: str, choice: MaterialityChoice) -> GateDecision:
    """绑定 MATERIALITY 第 1 版的 PLN-01/03 approve."""
    return GateDecision(
        id=decision_id,
        gate=GateId.PLN_01_03,
        target_id="MATERIALITY",
        target_version=1,
        decision=GateVerdict.approve,
        materiality=choice,
        decided_by=ACTOR,
        decided_at=NOW,
    )


@dataclass
class Flow:
    at_gate: dict[str, Any]  # 停在 PLN 闸门时的状态
    rejected: DecisionRejected  # 以税前利润为基准的决定被拒
    after_gate: dict[str, Any]  # 收入基准通过, 组好程序包后的状态
    stopped_at: tuple[str, ...]  # 之后停在哪个节点前
    staged: dict[str, StagedResult]  # REV-08, CSH-05 各执行一次的结果
    rev02: ProcedureRun  # REV-02 的执行记录 (只建不执行)
    financial: FakeFinancialStore


async def run_flow() -> Flow:
    financial = xero_financial_store()
    deps = make_xero_deps(financial_store=financial, rules=FakeRuleEngine(XERO_RULES), llm=MockLLMClient(LLM_XERO))
    nodes = GraphNodes(deps, 2)
    # 闸门之外另停在 schedule 前: P2 的判断程序要的 Xero 固定输出还没有 (见模块说明)
    app = build_graph(nodes, deps.features).compile(
        checkpointer=InMemorySaver(serde=checkpoint_serde()),
        interrupt_before=[*gate_nodes(deps.features), "schedule"],
    )
    runner = GraphRunner(app, deps)
    config = GraphRunner.config(EID)
    await to_acceptance_gate(runner, EID)
    await approve_acceptance(runner, EID, ACTOR)
    at_gate = await app.aget_state(config)
    assert at_gate.next == ("gate_p1_risk",)
    submit = GraphDecisionSubmit(app, deps, runner)
    with pytest.raises(DecisionRejected) as rejected:
        await submit.submit(EID, pln_decision("D-PLN-PBT", LOSS))
    await submit.submit(EID, pln_decision("D-PLN", REVENUE))
    await wait_idle(runner, EID)
    after = await app.aget_state(config)
    state = after.values
    engagement = await deps.engagement_store.get(EID)
    assert engagement is not None
    staged = {pid: await nodes.perform(state, engagement, pid, "fw-0-0") for pid in ("REV-08", "CSH-05")}
    procedure = next(p for p in state["procedure_pack"] if p.id == "REV-02")
    profile = await deps.engagement_store.client_profile(engagement.client_id, state["client_profile_version"])
    rev02 = build_run(
        deps.library,
        deps.library.load("REV-02"),
        procedure,
        engagement,
        state,
        NOW,
        traits=deps.datasets.dataset_traits(XERO_DATASET),
        profile=profile,
    )
    return Flow(at_gate.values, rejected.value, state, after.next, staged, rev02, financial)


@pytest.fixture(scope="module")
def flow() -> Flow:
    return asyncio.run(run_flow())


def run_of(state: dict[str, Any], procedure_id: str) -> ProcedureRun:
    [run] = [r for r in state["procedure_runs"] if r.procedure_id == procedure_id]
    return run


def test_pln07_analytics_and_changes(flow: Flow) -> None:
    """验收 1: 上期起始日取自数据集声明; 比率按 A02 (本期收入 3243100, 上期 420000, 两期都没有销售成本); 月度表 3 行;
    A01 的 9 条变动例外依次为各资产负债类科目, 期初为上期末余额 (090, 820), 上期没有余额的为 0."""
    run = run_of(flow.at_gate, "PLN-07")
    assert (run.id, run.status, run.params_snapshot["prior_period_start"]) == (
        "RUN-PLN-07-1",
        RunStatus.succeeded,
        date(2025, 7, 1),
    )
    analytics = flow.at_gate["analytics"]
    assert (analytics.id, analytics.version) == ("ANALYTICS", 1)
    assert analytics.ratios == {"gross_margin_current": 1.0, "gross_margin_prior": 1.0, "revenue_growth": 6.7217}
    assert [(m.month, m.revenue_cents, m.expense_cents, m.employee_expense_cents) for m in analytics.monthly] == [
        (date(2026, 7, 1), 460000, 331117, 0),
        (date(2026, 8, 1), 1803600, 2441870, 2010000),
        (date(2026, 9, 1), 979500, 1792809, 1340000),
    ]
    ids = [f"X-PLN-07-1-A01-{n}" for n in range(1, 10)]
    assert analytics.exception_ids == ids
    exceptions = {x.id: x for x in flow.at_gate["exceptions"] if x.procedure_id == "PLN-07"}
    assert sorted(exceptions) == sorted(ids)
    changes = [exceptions[i] for i in ids]
    assert [(x.source_ids, x.amount_cents, int(x.detail["opening_cents"])) for x in changes] == [
        (["090"], -219724, -870320),
        (["091"], 1158123, 0),
        (["610"], 1019950, 0),
        (["710"], 75000, 0),
        (["711"], -82500, 0),
        (["800"], -400516, 0),
        (["820"], -198317, 79120),
        (["825"], -753500, 0),
        (["840"], -1921221, 0),
    ]
    assert {(x.severity, x.disposition, x.run_id) for x in changes} == {
        ("medium", ExceptionDisposition.open, "RUN-PLN-07-1")
    }


def test_pln01_candidates_and_presets(flow: Flow) -> None:
    """验收 2: 三基准的候选 (税前亏损也照常列出), 比例范围; 状态里的审计目标与风险清单是 Xero 预置的 3 条与 3 条."""
    assert run_of(flow.at_gate, "PLN-01").status is RunStatus.succeeded
    candidates = flow.at_gate["materiality_candidates"]
    assert (candidates.id, candidates.version) == ("MATERIALITY", 1)
    assert [(c.basis, c.base_amount_cents, c.low_cents, c.high_cents) for c in candidates.candidates] == [
        ("revenue", 3243100, 16216, 32431),
        ("total_assets", 1080529, 10805, 21611),
        ("profit_before_tax", -1322696, -66135, -66135),
    ]
    assert (candidates.performance_pct_range, candidates.clearly_trivial_pct) == ((0.5, 0.75), 0.05)
    objectives, risks = xero_presets()
    assert (flow.at_gate["audit_objectives"], flow.at_gate["risk_register"]) == (objectives, risks)
    assert (len(objectives), len(risks)) == (3, 3)


def test_materiality_gate_rejects_loss_basis(flow: Flow) -> None:
    """验收 3 (Q18): 税前亏损时以税前利润为基准报 INVALID_VALUE, 消息含基准名; {revenue, 0.01, 0.5, 0.05} 通过,
    重要性 (32431, 16216, 1622), 图越过闸门组好程序包."""
    assert flow.rejected.code == "INVALID_VALUE" and "profit_before_tax" in flow.rejected.message
    after = flow.after_gate
    assert (after["materiality_cents"], after["performance_materiality_cents"], after["clearly_trivial_cents"]) == (
        32431,
        16216,
        1622,
    )
    assert flow.stopped_at == ("schedule",)
    assert {"REV-08", "REV-02", "CSH-05"} <= {p.id for p in after["procedure_pack"]}


def test_rev08_flags_the_short_receipt_on_orc1026(flow: Flow) -> None:
    """验收 4: REV-08 (目标 significant, 窗口 20 天) 由 A6 执行, 只有 ORC1026 的 R03-1 (发票 715000, 收到 385000),
    没有 R01, R02; 执行成功."""
    result = flow.staged["REV-08"]
    assert result["writer"] is AgentId.A6
    [x] = result["patch"]["exceptions"]
    assert (x.id, x.rule_id, x.source_ids, x.amount_cents) == ("X-REV-08-1-R03-1", "R03", [ORC1026], 330000)
    assert x.detail == {
        "invoice_amount_cents": 715000,
        "received_cents": "385000",
        "bank_transaction_ids": "BT-090-0124",
    }
    [run] = result["patch"]["procedure_runs"]
    assert (run.id, run.status, run.params_snapshot) == (
        "RUN-REV-08-1",
        RunStatus.succeeded,
        {"window_days": 20, "period_end": PERIOD_END},
    )


def test_csh05_has_no_exception(flow: Flow) -> None:
    """验收 5: CSH-05 由 A6 执行, 没有例外, 执行成功. 预置的 "无例外" 与假存储里的数据一致: 两个账户期末前最后一笔流水的
    余额都等于本期试算表 (BT-090-0072 为 -1090044, BT-091-0001 为 1158123), 同 B01 的口径."""
    result = flow.staged["CSH-05"]
    [run] = result["patch"]["procedure_runs"]
    assert (result["writer"], result["patch"]["exceptions"], run.id, run.status) == (
        AgentId.A6,
        [],
        "RUN-CSH-05-1",
        RunStatus.succeeded,
    )
    records = flow.financial.records
    accounts = [row for (kind, _), row in records.items() if kind == "bank_account"]
    last = {}
    for account in accounts:
        rows = [
            row
            for (kind, _), row in records.items()
            if kind == "bank_transaction" and row["bank_account_id"] == account["id"] and row["txn_date"] <= PERIOD_END
        ]
        latest = max(rows, key=lambda row: (row["txn_date"], row["id"]))
        book = records[("trial_balance", account["gl_account_code"])]["balance_cents"]
        last[account["id"]] = (latest["id"], latest["balance_after_cents"], book)
    assert last == {"BA-090": ("BT-090-0072", -1090044, -1090044), "BA-091": ("BT-091-0001", 1158123, 1158123)}


def test_rev02_params_follow_the_gate(flow: Flow) -> None:
    """验收 6: REV-02 的参数快照: threshold_cents 取闸门定的执行重要性 (16216, 整体重要性的 50%), window_days 按目标
    significant 为 20."""
    assert (flow.rev02.id, flow.rev02.params_snapshot) == (
        "RUN-REV-02-1",
        {"window_days": 20, "threshold_cents": 16216, "period_end": PERIOD_END},
    )
