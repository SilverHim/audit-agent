"""需求 R179 (slow): GEN-06 E01, E02 的 SQL 连库验收. Xero q1 按补造清单写进测试库 (R112 的 build_xero_project, 数据集
声明随之载入), Numbat 经数据集载入, 两个项目各建一次 (module 级 fixture), 各用例共用; 用真实的
PostgresFinancialDataStore 与 SqlRuleEngine 执行, 参数同 GraphNodes 用 build_run 解析. 缺省参数下 Xero 的行等于离线
派生 (XERO_RULES 的 GEN-06, 即 xero_gen06_rows), 再以参数覆盖断言到刚好能区分口径 (区间两端, 作废对, 数据截止日).
没有 TEST_DATABASE_URL 时跳过."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from backend.agents.base import AgentDeps
from backend.contracts.models import ClientProfile, Engagement, ProcedureRun
from backend.contracts.records import DatasetTraits
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState, initial_state
from backend.orchestration.nodes import build_run
from tests.external import snapshot_dir, supplements_dir
from tests.test_xero_rules_db import build_xero_project, database_url, new_owner, procedure_of
from tests.xero_fakes import SNAPSHOT_NAME, XERO_RULES

pytestmark = [pytest.mark.slow, pytest.mark.asyncio(loop_scope="module")]  # 用例与 module 级项目同一个事件循环
NUMBAT_MATERIALITY = {
    "materiality_cents": 12000000,
    "performance_materiality_cents": 9000000,
    "clearly_trivial_cents": 600000,
}


@dataclass
class Gen06Project:
    """测试库中的一个项目与 GEN-06 第 1 次执行的记录 (参数快照)."""

    deps: AgentDeps
    eid: str
    run: ProcedureRun


def gen06_run(
    deps: AgentDeps, engagement: Engagement, state: EngagementState, traits: DatasetTraits, profile: ClientProfile
) -> ProcedureRun:
    """GEN-06 第 1 次执行的记录: 参数由程序库按项目, 状态的重要性与数据集声明解析 (同 GraphNodes)."""
    template = deps.library.load("GEN-06")
    return build_run(
        deps.library,
        template,
        procedure_of(deps, "GEN-06"),
        engagement,
        state,
        datetime.now(UTC),
        traits=traits,
        profile=profile,
    )


async def gen06(project: Gen06Project, **overrides: object) -> RuleResult:
    """以参数快照 (叠加 overrides) 跑 GEN-06 的规则."""
    deps = project.deps
    return await deps.rules.run(
        project.eid, deps.library.load("GEN-06"), project.run, project.run.params_snapshot | overrides
    )


def entries(result: RuleResult) -> list[str]:
    """E01 的凭证号, 去重后按出现顺序."""
    return list(dict.fromkeys(str(r["entry_no"]) for r in result.rows["E01"]))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def xero(tmp_path_factory: pytest.TempPathFactory) -> Gen06Project:
    project = await build_xero_project(
        database_url(), snapshot_dir(SNAPSHOT_NAME), supplements_dir(SNAPSHOT_NAME), tmp_path_factory.mktemp("xero")
    )
    run = gen06_run(project.deps, project.engagement, project.state, project.traits, project.profile)
    return Gen06Project(project.deps, project.engagement.id, run)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def numbat(tmp_path_factory: pytest.TempPathFactory) -> Gen06Project:
    """Numbat (没有数据集声明, 取缺省; 重要性按 R55) 经数据集载入."""
    from backend.core.config import Settings
    from backend.main import build_deps

    evidence = tmp_path_factory.mktemp("numbat") / "evidence"
    deps = build_deps(Settings(DATABASE_URL=database_url(), EVIDENCE_ROOT=evidence, LLM_MODE="mock"))
    owner = await new_owner(deps.data.engine)  # type: ignore[attr-defined]
    profile = deps.datasets.client_profile("demo").model_copy(update={"id": "CLIENT-" + uuid4().hex})
    engagement = await deps.engagement_store.create(  # type: ignore[attr-defined]
        owner_id=owner, period_start=date(2025, 7, 1), period_end=date(2026, 6, 30), dataset="demo", profile=profile
    )
    await deps.datasets.load(engagement.id, "demo")
    state = initial_state(engagement, [], profile.version) | NUMBAT_MATERIALITY
    return Gen06Project(deps, engagement.id, gen06_run(deps, engagement, state, DatasetTraits(), profile))  # type: ignore[arg-type]


@pytest.mark.external
async def test_xero_gen06_rows_on_postgres(xero: Gen06Project) -> None:
    """验收 1, 5: Xero q1 上 E01, E02 的行与离线派生 (xero_gen06_rows) 逐行相同 (列, 值, 类型与顺序), 不出例外; 区间为
    07-01 至 10-17."""
    params = xero.run.params_snapshot
    assert (xero.run.id, params["period_start"], params["data_as_of"]) == (
        "RUN-GEN-06-1",
        date(2026, 7, 1),
        date(2026, 10, 17),
    )
    result = await gen06(xero)
    assert (result.exceptions, result.outcomes) == ([], [])
    assert result.rows == XERO_RULES["GEN-06"](xero.run).rows
    days = [r["posting_date"] for rule in ("E01", "E02") for r in result.rows[rule]]
    assert min(days) >= date(2026, 7, 1) and max(days) == date(2026, 10, 17)  # 快照本期第一笔分录在 07-16


@pytest.mark.external
async def test_xero_gen06_counter_examples(xero: Gen06Project) -> None:
    """验收 1, 6: 能区分口径的反例.
    - 作废对: XJ-548 在 E01, 同日的作废对 XJ-547/549 与期后的 XJ-728/744 不在; 不剔除作废对 (没有冲回格式) 时都会进来.
    - 区间: 上期的发票 (ORC0999, 2025-08-16) 不在 E02; 期间起日改到 2025-07-01 时进来.
    - 数据截止日: data_as_of 改为 10-14 时 10-16 的 XJ-720 与 10-17 的发票都在区间外; 为空时取期末加 31 天, 区间不变
      (快照数据只到 10-17)."""
    result = await gen06(xero)
    found = entries(result)
    assert "XJ-548" in found and {"XJ-547", "XJ-549", "XJ-728", "XJ-744"}.isdisjoint(found)
    unvoided = entries(await gen06(xero, reversal_reference_pattern=None, reversal_entry_no_template=None))
    assert {"XJ-547", "XJ-549", "XJ-728", "XJ-744"} <= set(unvoided)

    numbers = [r["invoice_no"] for r in result.rows["E02"]]
    assert "ORC0999" not in numbers
    earlier = await gen06(xero, period_start=date(2025, 7, 1))
    assert "ORC0999" in [r["invoice_no"] for r in earlier.rows["E02"]]

    early = await gen06(xero, data_as_of=date(2026, 10, 14))
    assert "XJ-720" in found and "XJ-720" not in entries(early)
    assert max(r["posting_date"] for r in early.rows["E02"]) <= date(2026, 10, 14)
    assert (await gen06(xero, data_as_of=None)).rows == result.rows


async def test_numbat_gen06_sql_runs(numbat: Gen06Project) -> None:
    """Numbat (没有数据集声明: data_as_of 不设上限, 没有冲回格式) 上 GEN-06 的 SQL 能执行不报错; 区间为本期起至期末
    后 31 天."""
    params = numbat.run.params_snapshot
    assert (params["data_as_of"], params["reversal_reference_pattern"]) == (None, None)
    result = await gen06(numbat)
    assert result.outcomes == [] and result.rows["E01"]
    assert {r["posting_date"] >= date(2025, 7, 1) for r in [*result.rows["E01"], *result.rows["E02"]]} == {True}
    assert max(r["posting_date"] for r in result.rows["E01"]) <= date(2026, 7, 31)
