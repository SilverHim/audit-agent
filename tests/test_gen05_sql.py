"""需求 R131 (slow): GEN-05 原有规则 R01-R08 与 R13 的 SQL 连库验收. Xero q1 按补造清单写进测试库 (R112 的
build_xero_project), Numbat 经数据集载入, 两个项目各建一次 (module 级 fixture), 各用例共用; 用真实的
PostgresFinancialDataStore 与 SqlRuleEngine 执行, 参数同 GraphNodes 用 build_run 解析. 缺省参数下结果等于离线预置
(XERO_RULES, NUMBAT_RULES 的 GEN-05); 再以参数覆盖让缺省下 0 条或不执行的规则命中, 断言到刚好能区分口径 (审核
R131-01). 没有 TEST_DATABASE_URL 时跳过."""

from collections import Counter
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
from tests.fakes import NUMBAT_RULES
from tests.test_xero_rules_db import build_xero_project, database_url, new_owner, procedure_of
from tests.xero_fakes import SNAPSHOT_NAME, XERO_RULES

pytestmark = [pytest.mark.slow, pytest.mark.asyncio(loop_scope="module")]  # 用例与 module 级项目同一个事件循环
NUMBAT_MATERIALITY = {
    "materiality_cents": 12000000,
    "performance_materiality_cents": 9000000,
    "clearly_trivial_cents": 600000,
}


@dataclass
class Gen05Project:
    """测试库中的一个项目与 GEN-05 第 1 次执行的记录 (参数快照)."""

    deps: AgentDeps
    eid: str
    run: ProcedureRun


def gen05_run(
    deps: AgentDeps, engagement: Engagement, state: EngagementState, traits: DatasetTraits, profile: ClientProfile
) -> ProcedureRun:
    """GEN-05 第 1 次执行的记录: 参数由程序库按项目, 状态的重要性, 数据集声明与画像解析 (同 GraphNodes)."""
    template = deps.library.load("GEN-05")
    procedure = procedure_of(deps, "GEN-05")
    return build_run(
        deps.library, template, procedure, engagement, state, datetime.now(UTC), traits=traits, profile=profile
    )


async def gen05(project: Gen05Project, **overrides: object) -> RuleResult:
    """以参数快照 (叠加 overrides) 跑 GEN-05 的规则."""
    deps = project.deps
    return await deps.rules.run(
        project.eid, deps.library.load("GEN-05"), project.run, project.run.params_snapshot | overrides
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def xero(tmp_path_factory: pytest.TempPathFactory) -> Gen05Project:
    project = await build_xero_project(
        database_url(), snapshot_dir(SNAPSHOT_NAME), supplements_dir(SNAPSHOT_NAME), tmp_path_factory.mktemp("xero")
    )
    run = gen05_run(project.deps, project.engagement, project.state, project.traits, project.profile)
    return Gen05Project(project.deps, project.engagement.id, run)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def numbat(tmp_path_factory: pytest.TempPathFactory) -> Gen05Project:
    """Numbat (没有数据集声明, 取缺省; 参数按 R55: 执行重要性 9000000, 明显微小 600000) 经数据集载入."""
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
    return Gen05Project(deps, engagement.id, gen05_run(deps, engagement, state, DatasetTraits(), profile))  # type: ignore[arg-type]


@pytest.mark.external
async def test_xero_gen05_on_postgres(xero: Gen05Project) -> None:
    """验收 7: Xero q1 上 9 条规则 0 条例外, 不执行的规则与理由同离线预置 (录入时间不可靠的 4 条, 没有审批人的 R04,
    只有一个录入人的 R07); R02, R03 (审批限额 500000), R05 执行而没有命中. 覆盖为录入时间可信时 R01 24 笔, R08 49 笔,
    R13 196 笔 (R06 仍因没有错报沟通不执行), 证明跳过改变了结果; 其中 R13-1 为 XJ-397 三行中金额最大的 XJ-397-3
    (ID 最小的是 XJ-397-1), 守 "取凭证内金额最大的行"."""
    assert xero.run.params_snapshot["approval_limit_cents"] == 500000
    assert await gen05(xero) == XERO_RULES["GEN-05"](xero.run)
    reliable = await gen05(xero, entry_timestamps_reliable=True)
    assert Counter(x.rule_id for x in reliable.exceptions) == {"R01": 24, "R08": 49, "R13": 196}
    assert [(o.rule_id, o.status) for o in reliable.outcomes] == [
        ("R04", "not_executable"),
        ("R06", "skipped"),
        ("R07", "not_executable"),
    ]
    [r13] = [x for x in reliable.exceptions if x.id == "X-GEN-05-1-R13-1"]
    assert (r13.source_ids, r13.amount_cents) == (["XJ-397-3"], 1921221)


async def test_numbat_gen05_on_postgres(numbat: Gen05Project) -> None:
    """验收 8: Numbat 上只有 SJ-2026-0612 命中 R01, R04, R08, 其余同离线预置. 时区反例: SJ-2026-0612 录入于 11:14Z
    (悉尼周日 21:14) 而命中 R08, CP-AP-2025-07-A 录入于 06:01Z (悉尼 16:01) 而不命中; 不换算时区时两者正好相反.
    GJ-202606-ECL-1 借 1000000 (不是付款), 供 R03 的限额覆盖 (test_numbat_overrides_make_rules_hit) 区分只取付款."""
    assert await gen05(numbat) == NUMBAT_RULES["GEN-05"](numbat.run)
    lines = {
        line.id: line
        for line in await numbat.deps.financial_store.journal_entries(
            numbat.eid, entry_nos=["SJ-2026-0612", "CP-AP-2025-07-A", "GJ-202606-ECL"]
        )
    }
    assert lines["SJ-2026-0612-1"].created_at == datetime(2026, 6, 28, 11, 14, tzinfo=UTC)
    assert lines["CP-AP-2025-07-A-1"].created_at == datetime(2025, 7, 15, 6, 1, tzinfo=UTC)
    assert lines["GJ-202606-ECL-1"].debit_cents == 1000000


def hits(result: RuleResult, rule_id: str) -> list[str]:
    """一条规则的例外按编号的 source_id."""
    return [x.source_ids[0] for x in result.exceptions if x.rule_id == rule_id]


MONTHS = [(2025, m) for m in range(7, 13)] + [(2026, m) for m in range(1, 7)]
CP_OPEX = [f"CP-OPEX-{y}-{m:02d}-1" for y, m in MONTHS]  # Numbat 每月的经营费用付款, 费用行 1555000


@pytest.mark.parametrize(
    ("overrides", "rule_id", "count", "leading"),
    [
        # R07: 上限放到 78 (不超过即算), ap.clerk (恰 78 笔) 与 fin.controller (43 笔) 算非常规, ar.clerk (1222 笔)
        # 不算; 上限不含等号时只剩 fin.controller 的 43 笔
        ({"rare_user_max": 78}, "R07", 121, ["CP-RENT-2025-07-1", "CP-AP-2025-07-A-1"]),
        # R06: 错报沟通时间设为 06-01 悉尼零点, 其后录入的期内分录 (按录入时刻比, 不按业务日期)
        ({"audit_adjustment_start": "2026-06-01T00:00:00+10:00"}, "R06", 117, ["CP-RENT-2026-06-1"]),
        # R03 只取付款: GJ-202606-ECL-1 借 1000000 落在 [0.95 L, L) 内, 但它不是付款 (不贷记现金)
        ({"approval_limit_cents": 1000001}, "R03", 0, []),
        # R03 上限不含等号: 付款的费用行恰为 1555000
        ({"approval_limit_cents": 1555000}, "R03", 0, []),
        ({"approval_limit_cents": 1555001}, "R03", 12, CP_OPEX),
    ],
    ids=["R07-rare-user", "R06-after-communication", "R03-payments-only", "R03-limit-excluded", "R03-just-below"],
)
async def test_numbat_overrides_make_rules_hit(
    numbat: Gen05Project, overrides: dict[str, object], rule_id: str, count: int, leading: list[str]
) -> None:
    """审核 R131-01: 缺省参数下 0 条或不执行的规则, 以参数覆盖造出命中 (或卡在口径边界上不命中), 断言条数与开头的
    命中行; 一条返回空的 SQL, 或上限含等号, 或不限付款的写法, 至少在一组上失败."""
    found = hits(await gen05(numbat, **overrides), rule_id)
    assert (len(found), found[: len(leading)]) == (count, leading)


@pytest.mark.external
@pytest.mark.parametrize(
    ("overrides", "rule_id", "expected"),
    [
        # R02: 整数单位改为 100, 门槛 0: 调整分录中只剩手工折旧 XJ-499 (三行都是 100 的倍数, 取最大的贷方行);
        # 作废对 XJ-401, XJ-495, XJ-498 不剔除时会多出来
        ({"round_unit_cents": 100, "performance_materiality_cents": 0}, "R02", ["XJ-499-2"]),
        # R05: 期末改为 07-17, 前三日内的三笔作废冲回, 摘要以大写 "Reversed" 开头 (ILIKE)
        ({"period_end": "2026-07-17"}, "R05", ["XJ-399-1", "XJ-400-1", "XJ-409-1"]),
    ],
    ids=["R02-round-unit", "R05-period-end"],
)
async def test_xero_overrides_make_rules_hit(
    xero: Gen05Project, overrides: dict[str, object], rule_id: str, expected: list[str]
) -> None:
    """审核 R131-01: Xero 上缺省 0 条的 R02, R05 以参数覆盖造出命中, 断言全部命中行."""
    assert hits(await gen05(xero, **overrides), rule_id) == expected
