"""需求 R324: 被测总体. 规则引擎为执行了的规则各记一份总体 (RuleResult.populations), 节点把规则程序成功执行的总体
(RunPopulation) 与执行记录终态同一事务写入 Store; 去重, 合计与判断, 抽取程序的对象清单为 services.populations 的纯函数.

规则引擎用按 SQL 文本返回预置行的假财务存储 (同 R130 的 tests/test_rule_engine_checks), 连库见
tests/test_xero_rules_db.py (Xero 的 REV-08 等于离线预置, 含总体). 节点用 Numbat 装配后的项目 (tests/test_fieldwork),
REV-08 换成真实的 A6, 规则引擎为预置 NUMBAT_RULES 的 FakeRuleEngine.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any, ClassVar

import pytest
import yaml

from backend.agents.base import ProcedureAgent, RunContext
from backend.agents.fieldwork import FieldworkAgent
from backend.agents.risk_assessor import RiskAssessorAgent
from backend.contracts.errors import RuleError
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import (
    AgentId,
    DocumentType,
    PopulationItem,
    ProcedureRun,
    RuleOutcome,
    RulePopulation,
    RunPopulation,
    RunStatus,
    TaskKind,
)
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.checkpoint import checkpoint_serde
from backend.orchestration.nodes import GraphNodes, run_population
from backend.services.populations import distinct_items, extracted_documents, judged_subjects, total_cents
from backend.services.rule_engine import SqlRuleEngine, rule_population
from tests.fakes import (
    ENGAGEMENT,
    KNOWLEDGE,
    NUMBAT_CONTRACTS,
    NUMBAT_PROFILE,
    NUMBAT_REPLIES,
    NUMBAT_RULES,
    FakeOutputStore,
    FakeRuleEngine,
    finished,
    knowledge_library,
    make_run,
    no_population,
    numbat_rev08_exception,
    numbat_rev08_populations,
)
from tests.test_fieldwork import assembled, dispatch
from tests.test_merge import judgment, task
from tests.test_rule_engine_checks import PARAMS, ScriptedStore, journal_row

EID = ENGAGEMENT.id
T_YAML = """id: T
name: 被测总体自测
phase: P2_Executing
cycle: Revenue
owner: A6
engine: rule
autonomy_level: L3
rules:
  - id: A
    name: 写了总体
    severity: high
    sql: rule a
    population: {sql: pop a, description: {zh: 甲, en: a}}
  - {id: B, name: 没写总体, severity: medium, sql: rule b}
  - {id: C, name: 返回行, severity: low, emit: rows, sql: rule c}
  - id: D
    name: 检查命中
    severity: high
    checks: [{status: skipped, sql: check d}]
    sql: rule d
    population: {sql: pop d, description: {zh: 丁, en: d}}
"""
T = ProcedureTemplate.model_validate(yaml.safe_load(T_YAML))


def item(source_id: str | None, amount: int | None, **fields: Any) -> PopulationItem:
    return PopulationItem(source_id=source_id, amount_cents=amount, fields=fields)


def recorded(rule_id: str, *items: PopulationItem, source_type: Any = "sales_invoice") -> RulePopulation:
    """记录了的总体, 项目数与合计按各项算 (合计同 total_cents)."""
    return RulePopulation(
        rule_id=rule_id,
        source_type=source_type,
        items=list(items),
        count=len(items),
        amount_cents=total_cents(i.amount_cents for i in items),
    )


# ── 规则引擎 (验收 2) ──


async def test_populations_follow_the_rule_kind() -> None:
    """验收 2: A 写了总体 SQL, 规则 SQL 之后执行它, 总体取其行 (例外照旧只来自规则 SQL); B 没写, 记为未记录 (count
    为空); C emit rows, 以返回行为总体 (行 ID 取 id 列), 不另执行 SQL; D 的检查命中, 规则与总体 SQL 都不执行, 不记
    总体. 防总体顶替例外, 未记录记成 0, rows 规则多查一次, 或跳过的规则也记总体."""
    pop_a = [
        journal_row(1) | {"entry_no": "XJ-1", "posting_date": date(2026, 6, 1)},
        journal_row(2) | {"entry_no": "XJ-2"},
    ]
    rows_c = [
        {"source_type": "bank_transaction", "id": "BT-1", "amount_cents": Decimal("700"), "txn_date": date(2026, 6, 2)}
    ]
    store = ScriptedStore(
        {
            "rule a": [journal_row(1)],
            "pop a": pop_a,
            "rule b": [journal_row(3)],
            "rule c": rows_c,
            "check d": [{"reason": "不适用"}],
        }
    )
    result = await SqlRuleEngine(store).run(EID, T, make_run("T"), PARAMS)  # type: ignore[arg-type]
    assert result.populations == [
        recorded(
            "A",
            item("XJ-1", 100, entry_no="XJ-1", posting_date="2026-06-01"),
            item("XJ-2", 200, entry_no="XJ-2"),
            source_type="journal_entry",
        ),
        RulePopulation(rule_id="B"),
        recorded("C", item("BT-1", 700, txn_date="2026-06-02"), source_type="bank_transaction"),
    ]
    assert result.populations[1].count is None and result.populations[1].amount_cents is None
    assert [sql for sql, _ in store.calls] == ["rule a", "pop a", "rule b", "rule c", "check d"]
    assert all(params == PARAMS for _, params in store.calls)
    assert [x.id for x in result.exceptions] == ["X-T-1-A-1", "X-T-1-B-1"]
    assert result.rows == {"C": rows_c} and result.outcomes == [
        RuleOutcome(rule_id="D", status="skipped", reason="不适用")
    ]


async def test_population_sql_error_is_a_rule_error() -> None:
    """总体 SQL 出错同规则 SQL 出错, 抛 RuleError, 消息含程序与规则编号."""
    store = ScriptedStore({"rule a": [], "pop a": TimeoutError("statement timeout")})
    with pytest.raises(RuleError, match=r"T/A: statement timeout"):
        await SqlRuleEngine(store).run(EID, T, make_run("T"), PARAMS)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # 行 ID 取 source_id 列, 金额取 amount_cents, 两列与 source_type 不进识别字段
        (
            [{"source_type": "sales_invoice", "source_id": "SI-1", "id": "x", "amount_cents": 5, "invoice_no": "N1"}],
            recorded("R", item("SI-1", 5, id="x", invoice_no="N1")),
        ),
        # 没有 source_id 列时取 id 列 (emit rows 的规则); 没有金额列, 合计为空
        ([{"id": "SI-1", "invoice_no": "N1"}], recorded("R", item("SI-1", None, invoice_no="N1"), source_type=None)),
        # 汇总行: 没有行 ID; numeric 的值成文本
        (
            [{"period": "current", "revenue_cents": Decimal("3243100")}],
            recorded("R", item(None, None, period="current", revenue_cents="3243100"), source_type=None),
        ),
        # 各行类型不同: 对象类型为空, 各行的 source_type 留在识别字段里
        (
            [
                {"source_type": "journal_entry", "id": "XJ-1", "amount_cents": 1},
                {"source_type": "bank_transaction", "id": "BT-1", "amount_cents": 2},
            ],
            recorded(
                "R",
                item("XJ-1", 1, source_type="journal_entry"),
                item("BT-1", 2, source_type="bank_transaction"),
                source_type=None,
            ),
        ),
        # 各行类型相同而不是认识的行类型: 同各行不同, 对象类型为空, 原值留在识别字段里, 不报错 (审核 R324-04)
        (
            [{"source_type": "contract", "id": "CON-1"}],
            recorded("R", item("CON-1", None, source_type="contract"), source_type=None),
        ),
        # 有一项没有金额, 合计为空 (合计不全)
        (
            [
                {"source_type": "sales_invoice", "source_id": "a", "amount_cents": 1},
                {"source_type": "sales_invoice", "source_id": "b", "amount_cents": None},
            ],
            RulePopulation(
                rule_id="R",
                source_type="sales_invoice",
                items=[item("a", 1), item("b", None)],
                count=2,
                amount_cents=None,
            ),
        ),
        # 没有行: 记录了, 项目数与合计都是 0
        ([], RulePopulation(rule_id="R", items=[], count=0, amount_cents=0)),
    ],
)
def test_rule_population_reads_standard_columns(rows: list[dict[str, Any]], expected: RulePopulation) -> None:
    assert rule_population("R", rows) == expected


# ── 纯函数: 合计, 去重与对象清单 (验收 1, 4) ──


@pytest.mark.parametrize(("amounts", "expected"), [([], 0), ([1, 2], 3), ([1, None], None), ([None], None)])
def test_total_cents(amounts: list[int | None], expected: int | None) -> None:
    assert total_cents(amounts) == expected


A, B, C = item("SI-A", 100, n=1), item("SI-B", 200, n=2), item("SI-C", 300, n=3)


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        # 按 (对象类型, 行 ID) 去重, 留先出现的 (R02 的 SI-B 识别字段不同也算同一对象)
        ([recorded("R01", A, B), recorded("R02", item("SI-B", 200, n=9), C)], [A, B, C]),
        # 同一行 ID 而对象类型不同, 不是同一对象
        ([recorded("R01", A), recorded("R02", A, source_type="delivery_note")], [A, A]),
        # 没有行 ID 的汇总行各算一项
        (
            [recorded("A02", item(None, None, p=1), item(None, None, p=1), source_type=None)],
            [item(None, None, p=1)] * 2,
        ),
        ([recorded("R01", A), RulePopulation(rule_id="R02")], None),  # 有规则未记录: 去重数不全, 不给
        ([], []),  # 规则都没有执行
    ],
)
def test_distinct_items_dedupe_across_rules(rules: list[RulePopulation], expected: list[PopulationItem] | None) -> None:
    population = RunPopulation(id="POP-RUN-T-1", run_id="RUN-T-1", procedure_id="T", rules=rules)
    assert distinct_items(population) == expected


def test_judged_subjects_from_judgments_and_failed_tasks() -> None:
    """验收 4: 本次执行 (RUN-REV-03-1) 的判断对象加单个对象失败的 failed 任务对象, 去重按 ID; 别的执行的判断, 整次
    失败 (没有 subject_id) 与 failed 以外的任务不算."""
    judgments = [
        judgment("J-1", run_id="RUN-REV-03-1").model_copy(update={"subject_id": "CON-X02"}),
        judgment("J-2", run_id="RUN-REV-03-1").model_copy(update={"subject_id": "CON-X01"}),
        judgment("J-3", run_id="RUN-REV-03-1").model_copy(update={"subject_id": "CON-X01", "version": 2}),
        judgment("J-4", run_id="RUN-REV-03-2").model_copy(update={"subject_id": "CON-X09"}),
    ]
    failed = task(run_id="RUN-REV-03-1").model_copy(update={"kind": TaskKind.failed})
    tasks = [
        failed.model_copy(update={"id": "T-F1", "subject_id": "CON-X03"}),
        failed.model_copy(update={"id": "T-F2"}),  # 整次失败: 没有 subject_id
        failed.model_copy(update={"id": "T-U", "kind": TaskKind.unattributed, "subject_id": "SI-1"}),
        failed.model_copy(update={"id": "T-F3", "run_id": "RUN-REV-03-2", "subject_id": "CON-X08"}),
    ]
    assert judged_subjects("RUN-REV-03-1", judgments, tasks) == ["CON-X01", "CON-X02", "CON-X03"]


def test_extracted_documents_by_type_and_status() -> None:
    """抽取程序的对象: 程序文件类型中抽取成功与抽取失败转人工的文件, 按 ID; 还没抽取的与别的类型的不算."""
    contracts = [
        NUMBAT_CONTRACTS[0].model_copy(update={"status": "extracted"}),
        NUMBAT_CONTRACTS[1].model_copy(update={"status": "needs_manual"}),
        NUMBAT_CONTRACTS[2].model_copy(update={"status": "classified"}),
    ]
    replies = [d.model_copy(update={"status": "extracted"}) for d in NUMBAT_REPLIES]
    documents = [*contracts, *replies]
    assert extracted_documents([DocumentType.contract], documents) == sorted(d.id for d in contracts[:2])
    assert extracted_documents([DocumentType.confirmation], documents) == sorted(d.id for d in replies)


# ── 节点: 写入时机 (验收 3) ──


ONE = [RulePopulation(rule_id="R01")]


@pytest.mark.parametrize(
    ("procedure_id", "populations", "status", "written"),
    [
        ("REV-08", ONE, RunStatus.succeeded, True),
        ("REV-08", [], RunStatus.succeeded, True),  # 规则都没执行 (检查命中) 也写, 总体为空列表
        ("REV-08", ONE, RunStatus.failed, False),  # 失败的执行 (含 A1 代写的失败片段) 不写
        ("REV-08", None, RunStatus.succeeded, False),  # 没经规则引擎 (如脚本 Agent)
        ("REV-02", ONE, RunStatus.succeeded, False),  # 判断程序: 对象清单由 Store 推出, 不另存
        ("PLN-01", ONE, RunStatus.succeeded, False),  # 闸门程序 (engine manual) 取数用规则引擎, 也不写
    ],
)
def test_run_population_only_for_succeeded_rule_programs(
    procedure_id: str, populations: list[RulePopulation] | None, status: RunStatus, written: bool
) -> None:
    run = finished(make_run(procedure_id), status)
    population = run_population(knowledge_library(KNOWLEDGE).load(procedure_id), run, populations)
    if not written:
        assert population is None
        return
    assert population == RunPopulation(
        id=f"POP-RUN-{procedure_id}-1", run_id=f"RUN-{procedure_id}-1", procedure_id=procedure_id, rules=populations
    )


class FixedFindings(ProcedureAgent[Any]):
    """Agent 的 execute 交回给定的原始发现, interpret 只把执行置成功."""

    agent_id: ClassVar[AgentId] = AgentId.A6
    procedures: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, deps: Any, findings: Any) -> None:
        super().__init__(deps)
        self.findings = findings

    async def execute(self, ctx: RunContext) -> Any:
        return self.findings

    async def interpret(self, ctx: RunContext, findings: Any) -> StatePatch:
        return {"procedure_runs": [finished(ctx.run)]}


@pytest.mark.parametrize(
    ("findings", "expected"),
    [
        (RuleResult(exceptions=[], rows={}, populations=ONE), ONE),
        (RuleResult(exceptions=[], rows={}), []),  # 规则都没执行: 空列表, 不是空值
        ({"judged": []}, None),  # 原始发现不是规则引擎的结果 (判断, 抽取, 外部查询)
    ],
    ids=["rules", "none_executed", "not_rules"],
)
async def test_outcome_hands_back_rule_populations(findings: Any, expected: list[RulePopulation] | None) -> None:
    """ProcedureAgent.outcome 交回片段, 原始发现为规则引擎的结果 (RuleResult) 时另交回其被测总体; run 给同一个片段."""
    nodes, _, state = await assembled()
    agent = FixedFindings(nodes.deps, findings)
    procedure = state["procedure_pack"][0]
    ctx = RunContext(ENGAGEMENT, procedure, nodes.deps.library.load(procedure.id), make_run(procedure.id), state)
    output = await agent.outcome(ctx)
    assert output.populations == expected
    assert await agent.run(ctx) == output.patch


async def staged_rev08(rules: FakeRuleEngine | None = None) -> tuple[GraphNodes, FakeOutputStore, EngagementState]:
    """Numbat 装配后, REV-08 由真实的 A6 执行 (规则引擎缺省为 NUMBAT_RULES), 第 0 层一个实例只派 REV-08, 暂存进
    状态; fan_in 尚未执行."""
    nodes, store, state = await assembled()
    deps = nodes.deps if rules is None else replace(nodes.deps, rules=rules)
    nodes.agents["REV-08"] = FieldworkAgent(deps)
    state["staged_results"] = (await nodes.fieldwork(dispatch(state, "REV-08", instance="fw-0-0")))["staged_results"]
    return nodes, store, state


def numbat_population(run_id: str = "RUN-REV-08-1") -> RunPopulation:
    return RunPopulation(id=f"POP-{run_id}", run_id=run_id, procedure_id="REV-08", rules=numbat_rev08_populations())


def population_writers(store: FakeOutputStore) -> list[tuple[str, str]]:
    """之后经 store.put 写入的被测总体的 (ID, 写入者); 假存储不记写入者, 这里记下 put 的参数."""
    seen: list[tuple[str, str]] = []
    put = store.put

    async def recording(eid: str, objs: Any, *, actor: str) -> list[Any]:
        seen.extend((o.id, actor) for o in objs if isinstance(o, RunPopulation))
        return await put(eid, objs, actor=actor)

    store.put = recording  # type: ignore[method-assign]
    return seen


async def test_p2_population_rides_the_staged_item_into_fan_in() -> None:
    """验收 3 (P2): 暂存项带 POP-RUN-REV-08-1 (Numbat 的窗口六十张发票, R03 没有找得到收款的), 片段里没有; 暂存项
    经检查点读回后总体不变; fan_in 与执行记录终态同一次提交写入, 写入者为执行它的 A6; 返回的片段不带总体, 暂存区清空."""
    nodes, store, state = await staged_rev08()
    [staged] = state["staged_results"]
    assert staged["population"] == numbat_population()
    assert [(r.rule_id, r.count, r.amount_cents) for r in staged["population"].rules] == [
        ("R01", 60, 121372000),
        ("R02", 60, 121372000),
        ("R03", 0, 0),
    ]
    assert no_population(staged["patch"])
    serde = checkpoint_serde()
    [back] = serde.loads_typed(serde.dumps_typed({"staged_results": [staged]}))["staged_results"]
    assert back == staged and isinstance(back["population"], RunPopulation)
    assert isinstance(back["population"].rules[0].items[0], PopulationItem)
    commits, writers = len(store.commits), population_writers(store)

    patch = await nodes.fan_in(state)

    [commit] = store.commits[commits:]
    assert [o for o in commit if isinstance(o, RunPopulation)] == [numbat_population()]
    assert writers == [("POP-RUN-REV-08-1", AgentId.A6)]
    assert [o.id for o in commit if isinstance(o, ProcedureRun)] == ["RUN-REV-08-1"]
    assert await store.get(EID, RunPopulation, "POP-RUN-REV-08-1") == numbat_population()
    assert no_population(patch) and patch["staged_results"] is None


async def test_replay_does_not_write_population_again() -> None:
    """验收 3 (重放): fan_in 写入后同一状态再走一遍 fieldwork, 终态执行已在 Store, 按重放取回, 暂存项不带总体; fan_in
    重做 (Store 已写, 检查点没跟上) 时总体同号跳过. Store 中始终只有第 1 版."""
    nodes, store, state = await staged_rev08()
    await nodes.fan_in(state)
    again = await nodes.fieldwork(dispatch(state, "REV-08", instance="fw-0-0"))
    [replayed] = again["staged_results"]
    assert "population" not in replayed
    commits = len(store.commits)
    await nodes.fan_in(state)  # 带着原暂存项重做
    assert all(not isinstance(o, RunPopulation) for c in store.commits[commits:] for o in c)
    assert await store.history(EID, RunPopulation, "POP-RUN-REV-08-1") == [numbat_population()]


async def test_failed_executions_write_no_population() -> None:
    """验收 3 (失败): 规则引擎返回了 (总体已记下) 而片段校验不过 (例外挂在别的执行上), A1 代写失败执行, 暂存项不带
    总体; fan_in 时才查出外键不对而改为失败项的, 也不写."""
    elsewhere = numbat_rev08_exception(make_run("REV-08")).model_copy(update={"run_id": "RUN-REV-08-9"})
    rules = FakeRuleEngine(
        {"REV-08": lambda run: NUMBAT_RULES["REV-08"](run).model_copy(update={"exceptions": [elsewhere]})}
    )
    _, _, state = await staged_rev08(rules)
    [staged] = state["staged_results"]
    assert staged["writer"] is AgentId.A1 and "population" not in staged

    nodes, store, state = await staged_rev08()
    [staged] = state["staged_results"]
    staged["patch"]["exceptions"] = [
        x.model_copy(update={"run_id": "RUN-REV-08-9"}) for x in staged["patch"]["exceptions"]
    ]
    patch = await nodes.fan_in(state)
    [run] = patch["procedure_runs"]
    assert run.status is RunStatus.failed
    assert await store.get(EID, RunPopulation, "POP-RUN-REV-08-1") is None


async def test_attempt_writes_population_with_the_terminal_run() -> None:
    """验收 3 (P2 以外): PLN-07 由真实的 A4 执行 (attempt), 总体 POP-RUN-PLN-07-1 与执行记录终态, 程序新版本同一次
    提交, 写入者为执行它的 A4; A01 没写总体 SQL 记为未记录, A02, A03 以返回行为总体; 返回的片段不带总体."""
    nodes, store, state = await assembled()
    nodes.agents["PLN-07"] = RiskAssessorAgent(nodes.deps)
    engagement = ENGAGEMENT.model_copy(update={"client_profile_version": NUMBAT_PROFILE.version})
    commits, writers = len(store.commits), population_writers(store)

    patch, _ = await nodes.attempt(state, engagement, "PLN-07")

    [commit] = store.commits[commits:]
    [population] = [o for o in commit if isinstance(o, RunPopulation)]
    assert (population.id, population.run_id) == ("POP-RUN-PLN-07-1", "RUN-PLN-07-1")
    assert writers == [("POP-RUN-PLN-07-1", AgentId.A4)]
    assert [(r.rule_id, r.count) for r in population.rules] == [("A01", None), ("A02", 2), ("A03", 12)]
    assert {type(o).__name__ for o in commit} >= {"ProcedureRun", "Procedure", "AnalyticsResult"}
    assert no_population(patch)
