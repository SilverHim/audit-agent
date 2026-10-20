"""需求 R130: 规则的执行前检查 (RuleSpec.checks) 与执行情况记录 (RuleResult.outcomes, ProcedureRun.rule_outcomes).

规则引擎用按 SQL 文本返回预置行的假财务存储, 不连库 (连库见 tests/test_persistence_postgres.py 的
test_rule_checks_on_postgres). 各 Agent 把没有执行的规则写进执行记录终态, 用各自模块的上下文与假规则引擎.
"""

import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.data import DataInput
from backend.contracts.errors import RuleError
from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import ProcedureRun, RuleOutcome, RunStatus, RunTrigger
from backend.contracts.services import RuleResult
from backend.core.config import get_settings
from backend.orchestration.checkpoint import checkpoint_serde
from backend.services.procedure_library import ProcedureLibrary, read_problems
from backend.services.rule_engine import DEFAULT_REASON, SqlRuleEngine
from tests import test_fieldwork_csh02, test_fieldwork_rev02, test_fieldwork_rules, test_risk_assessor
from tests.fakes import ENGAGEMENT, FakeOutputStore, FakeRuleEngine, knowledge_library

EID = ENGAGEMENT.id
PARAMS = {"period_end": "2026-06-30", "limit_cents": None}
T_YAML = """id: T
name: 执行前检查自测
phase: P2_Executing
cycle: Journals
owner: A6
engine: rule
autonomy_level: L3
rules:
  - id: A
    name: 没有检查
    severity: high
    sql: rule a
  - id: B
    name: 检查返回 1 行
    severity: high
    checks:
      - {status: skipped, sql: check b}
    sql: rule b
  - id: C
    name: 检查返回 0 行
    severity: medium
    checks:
      - {status: not_executable, sql: check c}
    sql: rule c
"""


def journal_row(n: int) -> dict[str, Any]:
    return {"source_type": "journal_entry", "source_id": f"XJ-{n}", "amount_cents": 100 * n}


class ScriptedStore:
    """假财务存储: query 按 SQL 文本返回预置的行 (值为异常时抛出), 记下每次调用 (SQL, 参数)."""

    def __init__(self, replies: Mapping[str, list[dict[str, Any]] | Exception]) -> None:
        self.replies = dict(replies)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query(self, eid: str, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((sql.strip(), dict(params)))
        reply = self.replies[sql.strip()]
        if isinstance(reply, Exception):
            raise reply
        return reply


def make_run(procedure_id: str = "T") -> ProcedureRun:
    return ProcedureRun(
        id=f"RUN-{procedure_id}-1",
        procedure_id=procedure_id,
        run_no=1,
        trigger=RunTrigger.initial,
        params_snapshot=dict(PARAMS),
        started_at=datetime.now(UTC),
        status=RunStatus.running,
    )


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> ProcedureTemplate:
    """临时 knowledge 目录 (仓库的副本加 T.yaml) 读出的程序 T: 三条规则, B, C 带执行前检查."""
    root = tmp_path_factory.mktemp("r130") / "knowledge"
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    (root / "procedures" / "T.yaml").write_text(T_YAML, encoding="utf-8")
    return ProcedureLibrary(root).load("T")


def with_checks(template: ProcedureTemplate, rule_id: str, checks: Sequence[dict[str, str]]) -> ProcedureTemplate:
    """只留 rule_id 一条规则并换掉它的检查."""
    [rule] = [r for r in template.rules if r.id == rule_id]
    replaced = rule.model_validate({**rule.model_dump(), "checks": list(checks)})
    return template.model_copy(update={"rules": [replaced]})


# ── 规则引擎 (验收 1 至 3) ──


async def test_checks_decide_which_rules_run(template: ProcedureTemplate) -> None:
    """验收 2: A 没有检查 (2 行), B 的检查返回 1 行 {reason "x"} (skipped), C 的检查返回 0 行 (规则 1 行): 例外
    X-T-1-A-1, X-T-1-A-2, X-T-1-C-1; outcomes 只记 B; B 的规则 SQL 不执行, 检查与规则拿到的参数都是同一份. 防
    跳过的规则仍执行或出例外, 执行了的规则也记 outcome, 或检查用了别的参数."""
    store = ScriptedStore(
        {
            "rule a": [journal_row(1), journal_row(2)],
            "check b": [{"reason": "x"}, {"reason": "y"}],
            "check c": [],
            "rule c": [journal_row(3)],
        }
    )
    result = await SqlRuleEngine(store).run(EID, template, make_run(), PARAMS)  # type: ignore[arg-type]
    assert [x.id for x in result.exceptions] == ["X-T-1-A-1", "X-T-1-A-2", "X-T-1-C-1"]
    assert result.outcomes == [RuleOutcome(rule_id="B", status="skipped", reason="x")]
    assert [sql for sql, _ in store.calls] == ["rule a", "check b", "check c", "rule c"]
    assert all(params == PARAMS for _, params in store.calls)


@pytest.mark.parametrize(
    ("first", "second", "expected", "calls"),
    [
        ([{"reason": "缺审批人"}], [{"reason": "不该用到"}], ("not_executable", "缺审批人"), ["c1"]),
        ([], [{"reason": "y"}], ("skipped", "y"), ["c1", "c2"]),
        ([{"reason": ""}], [], ("not_executable", DEFAULT_REASON), ["c1"]),  # reason 为空
        ([{"reason": "  "}], [], ("not_executable", "  "), ["c1"]),  # 只有空白不算空, 理由原样
        ([], [{"other": 1}], ("skipped", DEFAULT_REASON), ["c1", "c2"]),  # 没有 reason 列
        ([], [{"reason": None}], ("skipped", DEFAULT_REASON), ["c1", "c2"]),
    ],
)
async def test_first_check_returning_rows_decides(
    template: ProcedureTemplate,
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
    expected: tuple[str, str],
    calls: list[str],
) -> None:
    """验收 1: 检查按顺序执行, 第一个返回行的决定 status, 理由取第一行的 reason 列, 为空或没有时为 "前置检查未通过";
    之后的检查与规则 SQL 都不执行. 防按最后一个检查定, 或理由丢了."""
    checks = [{"status": "not_executable", "sql": "c1"}, {"status": "skipped", "sql": "c2"}]
    store = ScriptedStore({"c1": first, "c2": second})
    result = await SqlRuleEngine(store).run(EID, with_checks(template, "B", checks), make_run(), PARAMS)  # type: ignore[arg-type]
    assert [(o.rule_id, o.status, o.reason) for o in result.outcomes] == [("B", *expected)]
    assert [sql for sql, _ in store.calls] == calls and result.exceptions == []


async def test_skipped_rows_rule_has_no_rows(template: ProcedureTemplate) -> None:
    """emit rows 的规则没有执行时 RuleResult.rows 不放这个键 (裁定 Q5), 执行了的照常放."""
    rows = template.model_copy(update={"rules": [r.model_copy(update={"emit": "rows"}) for r in template.rules]})
    store = ScriptedStore({"rule a": [{"n": 1}], "check b": [{"reason": "x"}], "check c": [], "rule c": []})
    result = await SqlRuleEngine(store).run(EID, rows, make_run(), PARAMS)  # type: ignore[arg-type]
    assert result.rows == {"A": [{"n": 1}], "C": []}


async def test_check_error_is_a_rule_error(template: ProcedureTemplate) -> None:
    """验收 3: 检查 SQL 出错同规则 SQL 出错, 抛 RuleError, 消息含程序与规则编号."""
    store = ScriptedStore({"rule a": [], "check b": TimeoutError("statement timeout")})
    with pytest.raises(RuleError, match=r"T/B: statement timeout"):
        await SqlRuleEngine(store).run(EID, template, make_run(), PARAMS)  # type: ignore[arg-type]


@pytest.mark.parametrize("procedure_id", ["PLN-07", "PLN-01", "REV-08", "CSH-05", "REV-02", "CSH-02"])
async def test_rules_without_checks_run_exactly_as_before(procedure_id: str) -> None:
    """零影响 (裁定 Q4 (b)): 现有带规则的 6 条程序都没有执行前检查, 引擎对每条规则只执行它的 SQL 一次, 按 YAML 顺序,
    参数原样, 不多一次查询; outcomes 为空. 防给现有程序多跑查询或记出未执行的规则."""
    template = knowledge_library(get_settings().KNOWLEDGE_ROOT).load(procedure_id)
    assert template.rules and all(not r.checks for r in template.rules)
    store = AsyncMock()
    store.query.return_value = []
    result = await SqlRuleEngine(store).run(EID, template, make_run(procedure_id), PARAMS)
    assert [c.args for c in store.query.await_args_list] == [(EID, r.sql, PARAMS) for r in template.rules]
    assert result.outcomes == []


def test_reads_include_check_sql(template: ProcedureTemplate) -> None:
    """检查 SQL 读到的账表也须在 reads 中 (过期判断按 reads 看数据变动). 防检查读了总账而数据变了程序不过期."""
    reading = with_checks(template, "B", [{"status": "not_executable", "sql": "SELECT 1 FROM journal_entries"}])
    assert read_problems([reading]) == ["T 读 journal_entries"]
    assert read_problems([reading.model_copy(update={"reads": [DataInput.journal_entries]})]) == []


# ── 执行记录终态 (验收 4, 5) ──

OUTCOMES = [
    RuleOutcome(rule_id="R03", status="not_executable", reason="客户画像没有审批限额"),
    RuleOutcome(rule_id="R08", status="skipped", reason="录入时间不可靠"),
]


def agent_context(procedure_id: str) -> tuple[ProcedureAgent[Any], RunContext, FakeRuleEngine]:
    """各模块的上下文 (A4 的 PLN-07, PLN-01; A6 的纯规则 REV-08, CSH-05, 混合程序 REV-02, CSH-02) 与其假规则引擎."""
    if procedure_id in ("PLN-07", "PLN-01"):
        agent, ctx, rules = test_risk_assessor.setup(procedure_id)
        return agent, ctx, rules
    if procedure_id in ("REV-08", "CSH-05"):
        fieldwork, ctx = test_fieldwork_rules.setup(procedure_id)
    elif procedure_id == "REV-02":
        fieldwork, ctx, _ = test_fieldwork_rev02.setup()
    else:
        fieldwork, ctx, _ = test_fieldwork_csh02.setup()
    rules = fieldwork.deps.rules
    assert isinstance(rules, FakeRuleEngine)
    return fieldwork, ctx, rules


@pytest.mark.parametrize("procedure_id", ["PLN-07", "PLN-01", "REV-08", "CSH-05", "REV-02", "CSH-02"])
@pytest.mark.parametrize("outcomes", [OUTCOMES, []], ids=["checked", "all_run"])
async def test_run_records_the_rules_not_run(procedure_id: str, outcomes: list[RuleOutcome]) -> None:
    """验收 4 (裁定 Q3 A): 凡经规则引擎的程序, 执行记录终态的 rule_outcomes 等于 RuleResult.outcomes; 全部执行时为
    空. 用各程序的 Numbat 预置结果, 只另带 outcomes. 防混合程序与 A4 的程序丢掉未执行的规则."""
    agent, ctx, rules = agent_context(procedure_id)
    preset = rules.results.get(procedure_id)

    def with_outcomes(run: ProcedureRun) -> RuleResult:
        base = preset(run) if preset is not None else RuleResult(exceptions=[], rows={})
        return base.model_copy(update={"outcomes": list(outcomes)})

    rules.results[procedure_id] = with_outcomes
    patch = await agent.run(ctx)
    [run] = [r for r in patch["procedure_runs"] if r.id == ctx.run.id]
    assert (run.status, run.rule_outcomes) == (RunStatus.succeeded, outcomes)


async def test_rev02_without_subjects_still_records_the_rules_not_run() -> None:
    """REV-02 的选样规则没有执行 (没有行) 而也没有前置程序的例外时没有判断对象, 未执行的规则照样记进终态. 防取对象
    提前返回时丢掉记录."""
    agent, ctx, _ = test_fieldwork_rev02.setup(rows=[], exceptions=[])
    rules = agent.deps.rules
    assert isinstance(rules, FakeRuleEngine)
    rules.results["REV-02"] = lambda run: RuleResult(exceptions=[], rows={}, outcomes=OUTCOMES)
    patch = await agent.run(ctx)
    assert (patch["judgments"], patch["procedure_runs"][0].rule_outcomes) == ([], OUTCOMES)


async def test_rule_outcomes_survive_store_and_checkpoint() -> None:
    """验收 5: 带 rule_outcomes 的执行记录经 FakeOutputStore, JSON (Postgres 的 data 列) 与检查点序列化读回不丢;
    旧记录 (没有这个键) 读回为空. 防新字段在存储或检查点里丢失."""
    run = make_run().model_copy(update={"status": RunStatus.succeeded, "rule_outcomes": OUTCOMES})
    store = FakeOutputStore()
    await store.put(EID, [run], actor="A6")
    assert await store.get(EID, ProcedureRun, run.id) == run
    assert ProcedureRun.model_validate_json(run.model_dump_json()) == run
    serde = checkpoint_serde()
    back = serde.loads_typed(serde.dumps_typed({"procedure_runs": [run]}))["procedure_runs"][0]
    assert back == run and isinstance(back.rule_outcomes[0], RuleOutcome)
    old = {k: v for k, v in run.model_dump(mode="json").items() if k != "rule_outcomes"}
    assert ProcedureRun.model_validate(old).rule_outcomes == []
