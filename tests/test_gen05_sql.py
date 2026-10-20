"""需求 R131 (slow): GEN-05 原有规则 R01-R08 与 R13 的 SQL 连库验收. Xero q1 按补造清单写进测试库 (R112 的
build_xero_project), Numbat 经数据集载入; 用真实的 PostgresFinancialDataStore 与 SqlRuleEngine 执行, 参数同 GraphNodes
用 build_run 解析. 结果等于离线预置 (XERO_RULES, NUMBAT_RULES 的 GEN-05); 再以参数覆盖证明执行前检查与门槛确实改变结果.
没有 TEST_DATABASE_URL 时跳过."""

from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from backend.agents.base import AgentDeps
from backend.contracts.models import ClientProfile, Engagement, ProcedureRun
from backend.contracts.records import DatasetTraits
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState, initial_state
from backend.orchestration.nodes import build_run
from tests.external import snapshot_dir, supplements_dir
from tests.fakes import NUMBAT_RULES
from tests.test_xero_rules_db import build_xero_project, database_url, new_owner, procedure_of
from tests.xero_fakes import SNAPSHOT_NAME, XERO_RULES

pytestmark = pytest.mark.slow
NUMBAT_MATERIALITY = {
    "materiality_cents": 12000000,
    "performance_materiality_cents": 9000000,
    "clearly_trivial_cents": 600000,
}


def gen05_run(
    deps: AgentDeps, engagement: Engagement, state: EngagementState, traits: DatasetTraits, profile: ClientProfile
) -> ProcedureRun:
    """GEN-05 第 1 次执行的记录: 参数由程序库按项目, 状态的重要性, 数据集声明与画像解析 (同 GraphNodes)."""
    template = deps.library.load("GEN-05")
    procedure = procedure_of(deps, "GEN-05")
    return build_run(
        deps.library, template, procedure, engagement, state, datetime.now(UTC), traits=traits, profile=profile
    )


async def gen05(deps: AgentDeps, eid: str, run: ProcedureRun, **overrides: object) -> RuleResult:
    """以参数快照 (叠加 overrides) 跑 GEN-05 的规则."""
    return await deps.rules.run(eid, deps.library.load("GEN-05"), run, run.params_snapshot | overrides)


@pytest.mark.external
async def test_xero_gen05_on_postgres(tmp_path: Path) -> None:
    """验收 7: Xero q1 上 9 条规则 0 条例外, 不执行的规则与理由同离线预置 (录入时间不可靠的 4 条, 没有审批人的 R04,
    只有一个录入人的 R07); R02, R03 (审批限额 500000), R05 执行而没有命中. 覆盖为录入时间可信时 R01 24 笔, R08 49 笔,
    R13 196 笔 (R06 仍因没有错报沟通不执行), 证明跳过改变了结果."""
    project = await build_xero_project(
        database_url(), snapshot_dir(SNAPSHOT_NAME), supplements_dir(SNAPSHOT_NAME), tmp_path
    )
    deps, eid = project.deps, project.engagement.id
    run = gen05_run(deps, project.engagement, project.state, project.traits, project.profile)
    assert run.params_snapshot["approval_limit_cents"] == 500000
    assert await gen05(deps, eid, run) == XERO_RULES["GEN-05"](run)
    reliable = await gen05(deps, eid, run, entry_timestamps_reliable=True)
    assert Counter(x.rule_id for x in reliable.exceptions) == {"R01": 24, "R08": 49, "R13": 196}
    assert [(o.rule_id, o.status) for o in reliable.outcomes] == [
        ("R04", "not_executable"),
        ("R06", "skipped"),
        ("R07", "not_executable"),
    ]


async def test_numbat_gen05_on_postgres(tmp_path: Path) -> None:
    """验收 8: Numbat (没有数据集声明, 取缺省) 上只有 SJ-2026-0612 命中 R01, R04, R08, 其余同离线预置. 时区反例:
    SJ-2026-0612 录入于 11:14Z (悉尼周日 21:14) 而命中 R08, CP-AP-2025-07-A 录入于 06:01Z (悉尼 16:01) 而不命中;
    不换算时区时两者正好相反. 覆盖审批限额 1000000 时 R03 执行而 0 条: GJ-202606-ECL-1 借 1000000, 不是付款且等于
    限额, 不命中."""
    from backend.core.config import Settings
    from backend.main import build_deps

    deps = build_deps(Settings(DATABASE_URL=database_url(), EVIDENCE_ROOT=tmp_path / "evidence", LLM_MODE="mock"))
    owner = await new_owner(deps.data.engine)  # type: ignore[attr-defined]
    profile = deps.datasets.client_profile("demo").model_copy(update={"id": "CLIENT-" + uuid4().hex})
    engagement = await deps.engagement_store.create(  # type: ignore[attr-defined]
        owner_id=owner, period_start=date(2025, 7, 1), period_end=date(2026, 6, 30), dataset="demo", profile=profile
    )
    await deps.datasets.load(engagement.id, "demo")
    state = initial_state(engagement, [], profile.version) | NUMBAT_MATERIALITY
    run = gen05_run(deps, engagement, state, DatasetTraits(), profile)  # type: ignore[arg-type]
    assert await gen05(deps, engagement.id, run) == NUMBAT_RULES["GEN-05"](run)
    lines = {
        line.id: line
        for line in await deps.financial_store.journal_entries(
            engagement.id, entry_nos=["SJ-2026-0612", "CP-AP-2025-07-A", "GJ-202606-ECL"]
        )
    }
    assert lines["SJ-2026-0612-1"].created_at == datetime(2026, 6, 28, 11, 14, tzinfo=UTC)
    assert lines["CP-AP-2025-07-A-1"].created_at == datetime(2025, 7, 15, 6, 1, tzinfo=UTC)
    assert lines["GJ-202606-ECL-1"].debit_cents == 1000000
    limited = await gen05(deps, engagement.id, run, approval_limit_cents=1000000)
    assert [o.rule_id for o in limited.outcomes] == ["R06"]
    assert [x.rule_id for x in limited.exceptions] == ["R01", "R04", "R08"]
