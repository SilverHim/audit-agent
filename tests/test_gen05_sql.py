"""需求 R131, R132 (slow): GEN-05 原有规则 R01-R08, R13 与新规则 R09-R12 的 SQL 连库验收. Xero q1 按补造清单写进
测试库 (R112 的 build_xero_project), Numbat 经数据集载入, 两个项目各建一次 (module 级 fixture), 各用例共用; 用真实的
PostgresFinancialDataStore 与 SqlRuleEngine 执行, 参数同 GraphNodes 用 build_run 解析. 缺省参数下结果等于离线预置
(XERO_RULES, NUMBAT_RULES 的 GEN-05); 再以参数覆盖让缺省下 0 条或不执行的规则命中, 断言到刚好能区分口径 (审核
R131-01). R132 另核对作废对的个数与删去一笔贷项凭证后 R10, R12 的变化. 没有 TEST_DATABASE_URL 时跳过."""

from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, select

from backend.agents.base import AgentDeps
from backend.contracts.models import ClientProfile, Engagement, ProcedureRun
from backend.contracts.records import DatasetTraits, JournalEntry
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState, initial_state
from backend.orchestration.nodes import build_run
from backend.persistence import models as m
from backend.persistence.db import bind_engagement
from tests.external import snapshot_dir, supplements_dir
from tests.fakes import NUMBAT_RULES, numbat_records
from tests.test_knowledge import voided_cte
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


# 作废对的个数 (需求 R132 验收 1): 接在 GEN-05 R02 的作废对共用段之后, 全部对数, 原凭证在期内的对数, 期内的作废凭证数
VOIDED_COUNTS = """
)
SELECT (SELECT count(*) FROM reversal) AS pairs,
       (SELECT count(*) FROM reversal r WHERE EXISTS (
          SELECT 1 FROM journal_entries o WHERE o.engagement_id=:eid AND o.entry_no=r.original_no
            AND o.posting_date BETWEEN CAST(:period_start AS date) AND CAST(:period_end AS date))) AS in_period,
       (SELECT count(DISTINCT j.entry_no) FROM journal_entries j JOIN voided v ON v.entry_no=j.entry_no
        WHERE j.engagement_id=:eid
          AND j.posting_date BETWEEN CAST(:period_start AS date) AND CAST(:period_end AS date)) AS vouchers
"""


async def voided_counts(project: Gen05Project) -> dict[str, object]:
    """按 GEN-05 的作废对共用段 (与 CSH-02 逐字相同) 数作废对."""
    [rule] = [r for r in project.deps.library.load("GEN-05").rules if r.id == "R02"]
    shared = voided_cte(rule.sql)
    assert shared is not None
    [row] = await project.deps.financial_store.query(project.eid, shared + VOIDED_COUNTS, project.run.params_snapshot)
    return row


@asynccontextmanager
async def without_rows(project: Gen05Project, table_name: str, column: str, value: str) -> AsyncIterator[int]:
    """在测试库的项目中暂时删去一张账表里某列等于 value 的行 (测试库是副本, 原文件不改), 给出删去的行数; 退出时原样
    写回, 共用项目的其余用例不受影响. 一行都没删到时不写回 (空列表的 insert 会变成 INSERT DEFAULT VALUES 而报错,
    盖住 with 体内断言的失败信息)."""
    engine = project.deps.data.engine  # type: ignore[attr-defined]
    table = m.FINANCIAL[table_name]
    where = (table.c.engagement_id == project.eid) & (table.c[column] == value)
    async with engine.begin() as conn:
        await bind_engagement(conn, project.eid)
        rows = [dict(row) for row in (await conn.execute(select(table).where(where))).mappings()]
        await conn.execute(delete(table).where(where))
    try:
        yield len(rows)
    finally:
        if rows:
            async with engine.begin() as conn:
                await bind_engagement(conn, project.eid)
                await conn.execute(insert(table), rows)


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
    """R131 验收 7, R132 验收 1, 7: Xero q1 上新规则 R09-R12 的 7 条例外, 原有规则 0 条, 不执行的规则与理由同离线
    预置 (录入时间不可靠的 4 条, 没有审批人的 R04, 只有一个录入人的 R07); R02, R03 (审批限额 500000), R05, R11 执行.
    作废对全部 65 对, 原凭证在期内的 33 对 (66 笔). 覆盖为录入时间可信时 R01 24 笔, R08 49 笔, R13 196 笔 (R06 仍因
    没有错报沟通不执行), 证明跳过改变了结果; 其中 R13-1 为 XJ-397 三行中金额最大的 XJ-397-3 (ID 最小的是 XJ-397-1),
    守 "取凭证内金额最大的行". 删去贷项凭证 XJ-430 的 3 行后 R10 只剩 XJ-449-2 (编号变为 R10-1), R12 的 ORC1003 没有
    全额冲减的凭证 (XJ-449 是 Bank West 的)."""
    assert xero.run.params_snapshot["approval_limit_cents"] == 500000
    assert await gen05(xero) == XERO_RULES["GEN-05"](xero.run)
    assert await voided_counts(xero) == {"pairs": 65, "in_period": 33, "vouchers": 66}
    reliable = await gen05(xero, entry_timestamps_reliable=True)
    assert Counter(x.rule_id for x in reliable.exceptions) == {
        "R01": 24,
        "R08": 49,
        "R13": 196,
        **{"R09": 2, "R10": 2, "R11": 2, "R12": 1},
    }
    assert [(o.rule_id, o.status) for o in reliable.outcomes] == [
        ("R04", "not_executable"),
        ("R06", "skipped"),
        ("R07", "not_executable"),
    ]
    [r13] = [x for x in reliable.exceptions if x.id == "X-GEN-05-1-R13-1"]
    assert (r13.source_ids, r13.amount_cents) == (["XJ-397-3"], 1921221)
    async with without_rows(xero, "journal_entries", "entry_no", "XJ-430") as removed:
        assert removed == 3
        after = await gen05(xero)
    assert [(x.id, x.source_ids) for x in after.exceptions if x.rule_id == "R10"] == [
        ("X-GEN-05-1-R10-1", ["XJ-449-2"])
    ]
    [r12] = [x for x in after.exceptions if x.rule_id == "R12"]
    assert (r12.source_ids, r12.detail["full_credits"]) == (["XJ-405-2"], [])


ORC1009, ORC1016 = "SI-76e3f056-479f-417c-a72b-f3d767899b87", "SI-b2c02d0b-41a8-4d4d-97d7-014c78b3547d"


@pytest.mark.external
@pytest.mark.parametrize(
    "invoice",
    [
        ORC1009,  # "同一客户": Yarra Transport 07-17 的 550.00, 与 ORC1003, ORC1008 同日同额而客户不同
        ORC1016,  # "同一开票日" (审核 R132-02): Hamilton Smith 08-17 的 550.00, 与 ORC1003, ORC1008 同客户同额而不同日
    ],
    ids=["other-customer", "other-date"],
)
async def test_xero_r12_groups_by_customer_and_date(xero: Gen05Project, invoice: str) -> None:
    """需求 R132 验收 6: 去掉一张发票的服务完成记录后, 它没有同组的发票, 不算重复开票; R12 仍只有 ORC1003. 防分组
    漏掉客户或开票日 ("同一金额" 在现有数据里造不出反例)."""
    async with without_rows(xero, "delivery_notes", "invoice_id", invoice) as removed:
        assert removed == 1
        assert hits(await gen05(xero), "R12") == ["XJ-405-2"]


@pytest.mark.external
async def test_xero_r12_skips_an_invoice_without_journal_lines(xero: Gen05Project) -> None:
    """审核 R132-03 的裁定: R12 只对挂了分录的发票出例外. 删去 ORC1003 所挂的分录 XJ-405 后, 程序照常完成 (不抛
    RuleError), R12 执行而不出 ORC1003 的例外 (GEN-05 的例外都指向分录, GEN-02 按 detail.entry_no 取行)."""
    async with without_rows(xero, "journal_entries", "entry_no", "XJ-405") as removed:
        assert removed == 3
        result = await gen05(xero)
    assert hits(result, "R12") == [] and "R12" not in [o.rule_id for o in result.outcomes]


@pytest.mark.external
async def test_xero_r10_and_r12_share_the_credit_definition(xero: Gen05Project) -> None:
    """R10 与 R12 的贷项凭证口径相同 (审核 R132-07): 把 GST 科目也当作银行科目时, 两笔贷项凭证都涉及银行科目, R10 不再
    命中, R12 的 full_credits 也为空."""
    result = await gen05(xero, cash_categories=["cash", "tax_payable"])
    assert hits(result, "R10") == []
    [r12] = [x for x in result.exceptions if x.rule_id == "R12"]
    assert (r12.source_ids, r12.detail["full_credits"]) == (["XJ-405-2"], [])


async def test_numbat_gen05_on_postgres(numbat: Gen05Project) -> None:
    """R131 验收 8, R132 验收 1, 8: Numbat 上只有 SJ-2026-0612 命中 R01, R04, R08, 其余同离线预置 (R09, R10, R12 执行
    而 0 条; 没有作废冲回格式, R11 不执行, 也没有作废对). 时区反例: SJ-2026-0612 录入于 11:14Z
    (悉尼周日 21:14) 而命中 R08, CP-AP-2025-07-A 录入于 06:01Z (悉尼 16:01) 而不命中; 不换算时区时两者正好相反.
    GJ-202606-ECL-1 借 1000000 (不是付款), 供 R03 的限额覆盖 (test_numbat_overrides_make_rules_hit) 区分只取付款."""
    assert await gen05(numbat) == NUMBAT_RULES["GEN-05"](numbat.run)
    assert await voided_counts(numbat) == {"pairs": 0, "in_period": 0, "vouchers": 0}
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
    ("overrides", "rule_id", "expected", "outcome"),
    [
        # R02: 整数单位改为 100, 门槛 0: 调整分录中只剩手工折旧 XJ-499 (三行都是 100 的倍数, 取最大的贷方行);
        # 作废对 XJ-401, XJ-495, XJ-498 不剔除时会多出来
        ({"round_unit_cents": 100, "performance_materiality_cents": 0}, "R02", ["XJ-499-2"], None),
        # R05: 期末改为 07-17, 前三日内的三笔作废冲回, 摘要以大写 "Reversed" 开头 (ILIKE)
        ({"period_end": "2026-07-17"}, "R05", ["XJ-399-1", "XJ-400-1", "XJ-409-1"], None),
        # R09 (审核 R132-01): 审计更正分录的前缀改为 XJ-39, 转换余额 XJ-397 按审计更正剔除, 只剩 XJ-499
        ({"audit_adjustment_entry_prefix": "XJ-39"}, "R09", ["XJ-499-2"], None),
        # R12 (需求 R132): 期间只取 07-31, 期内只有没有服务完成记录的 ORC1005, 无从区分, 不执行
        ({"period_start": "2026-07-31", "period_end": "2026-07-31"}, "R12", [], "not_executable"),
    ],
    ids=["R02-round-unit", "R05-period-end", "R09-audit-prefix", "R12-no-records"],
)
async def test_xero_overrides_make_rules_hit(
    xero: Gen05Project, overrides: dict[str, object], rule_id: str, expected: list[str], outcome: str | None
) -> None:
    """审核 R131-01: Xero 上缺省 0 条的 R02, R05 以参数覆盖造出命中, 断言全部命中行; R09 剔除审计更正分录的前缀; R12
    的执行前检查在期内发票都没有记录时命中 (outcome 为该规则的执行记录状态, None 为执行了)."""
    result = await gen05(xero, **overrides)
    assert hits(result, rule_id) == expected
    assert next((o.status for o in result.outcomes if o.rule_id == rule_id), None) == outcome


# ── 需求 R194: 增量实例 (entry_nos 只取更正通知载入的凭证) ──


@asynccontextmanager
async def with_rows(project: Gen05Project, table_name: str, rows: list[dict[str, object]]) -> AsyncIterator[None]:
    """在测试库的项目中暂时插入账表的行 (测试库是副本, 原文件不改), 退出时按 ID 删去, 共用项目的其余用例不受影响."""
    engine = project.deps.data.engine  # type: ignore[attr-defined]
    table = m.FINANCIAL[table_name]
    ids = [row["id"] for row in rows]
    async with engine.begin() as conn:
        await bind_engagement(conn, project.eid)
        await conn.execute(insert(table), [{"engagement_id": project.eid, **row} for row in rows])
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await bind_engagement(conn, project.eid)
            await conn.execute(delete(table).where((table.c.engagement_id == project.eid) & table.c.id.in_(ids)))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def xero_corrected(tmp_path_factory: pytest.TempPathFactory) -> Gen05Project:
    """另建一个 Xero 项目, 载入补造的全部更正分录 (AJE-X01 至 X05, 11 行; DatasetLoader.load_adjustments)."""
    project = await build_xero_project(
        database_url(), snapshot_dir(SNAPSHOT_NAME), supplements_dir(SNAPSHOT_NAME), tmp_path_factory.mktemp("xero")
    )
    eid = project.engagement.id
    loaded = await project.deps.datasets.load_adjustments(eid, project.engagement.dataset, None, actor=project.owner)  # type: ignore[arg-type]
    assert len(loaded) == 11
    run = gen05_run(project.deps, project.engagement, project.state, project.traits, project.profile)
    return Gen05Project(project.deps, eid, run)


FIRST_ROUND, SECOND_ROUND = ["AJE-X01", "AJE-X05"], ["AJE-X02", "AJE-X03", "AJE-X04"]
NOT_RUN = {  # Xero 上不执行的规则 (录入时间不可靠, 没有审批人, 录入人不足 2 个)
    ("R01", "skipped"),
    ("R06", "skipped"),
    ("R08", "skipped"),
    ("R13", "skipped"),
    ("R04", "not_executable"),
    ("R07", "not_executable"),
}


@pytest.mark.external
@pytest.mark.parametrize("entry_nos", [FIRST_ROUND, SECOND_ROUND], ids=["round1", "round2"])
async def test_xero_increment_finds_nothing(xero_corrected: Gen05Project, entry_nos: list[str]) -> None:
    """验收 4: 两轮更正通知的增量实例各 0 条例外 (R09 剔除 AJE- 前缀, R02 不是 10,000 的整数倍, R03 不是付款, R05
    说明不含 revers); 不执行的规则同初始实例, R11 照常执行."""
    result = await gen05(xero_corrected, entry_nos=entry_nos)
    assert result.exceptions == []
    assert {(o.rule_id, o.status) for o in result.outcomes} == NOT_RUN


def adjustment_line(lid: str, entry_no: str, account: str, debit: int, credit: int) -> dict[str, object]:
    """一行手工调整分录 (09-30, is_adjustment 真, 录入人与审批人为空, 录入时间为空)."""
    return {
        "id": lid,
        "entry_no": entry_no,
        "posting_date": date(2026, 9, 30),
        "account_code": account,
        "debit_cents": debit,
        "credit_cents": credit,
        "description": f"Manual journal {entry_no}",
        "source_doc": entry_no,
        "is_adjustment": True,
    }


EXTRA = [  # 验收 5: 同额同科目的两笔, 一笔是审计更正分录 (前缀 AJE-)
    adjustment_line("XJ-9001-1", "XJ-9001", "477", 1000000, 0),
    adjustment_line("XJ-9001-2", "XJ-9001", "804", 0, 1000000),
    adjustment_line("AJE-X09-1", "AJE-X09", "477", 1000000, 0),
    adjustment_line("AJE-X09-2", "AJE-X09", "804", 0, 1000000),
]


@pytest.mark.external
async def test_increment_limits_rules_to_entry_nos(xero_corrected: Gen05Project) -> None:
    """验收 5: 插入 XJ-9001 与 AJE-X09 (借 477 / 贷 804 10,000.00): entry_nos 含两者时 R09 只命中 XJ-9001 (剔除审计
    更正分录), R02 两笔都命中; entry_nos 不含它们时两条规则都不命中."""
    async with with_rows(xero_corrected, "journal_entries", EXTRA):
        both = await gen05(xero_corrected, entry_nos=["AJE-X01", "AJE-X09", "XJ-9001"])
        others = await gen05(xero_corrected, entry_nos=FIRST_ROUND)
    assert (hits(both, "R09"), hits(both, "R02")) == (["XJ-9001-1"], ["AJE-X09-1", "XJ-9001-1"])
    assert (hits(others, "R09"), hits(others, "R02")) == ([], [])


async def assert_limited(
    project: Gen05Project, overrides: dict[str, object], rule_ids: tuple[str, ...], extra: Sequence[str] = ()
) -> None:
    """参数覆盖下这些规则命中; entry_nos 为命中的全部凭证 (再加上 extra 中全期不命中的凭证) 时这些规则的例外连同明细
    都不变 (extra 仍不命中), 为不相干的凭证号时都不命中."""
    full = await gen05(project, **overrides)
    picked = set(rule_ids)
    entries = sorted({x.detail["entry_no"] for x in full.exceptions if x.rule_id in picked})
    assert {x.rule_id for x in full.exceptions} >= picked
    same = await gen05(project, entry_nos=[*entries, *extra], **overrides)
    others = await gen05(project, entry_nos=["NOT-AN-ENTRY"], **overrides)
    assert [x for x in same.exceptions if x.rule_id in picked] == [x for x in full.exceptions if x.rule_id in picked]
    assert [x for x in others.exceptions if x.rule_id in picked] == []


@pytest.mark.parametrize(
    ("overrides", "rule_ids", "regular"),
    [
        ({}, ("R01", "R04", "R08"), None),  # SJ-2026-0612
        ({"approval_limit_cents": 1555001}, ("R03",), None),
        ({"rare_user_max": 78}, ("R07",), "ar.clerk"),  # 录入人计数按全期, 只在最后一步筛
    ],
    ids=["R01-R04-R08", "R03", "R07"],
)
async def test_numbat_entry_nos_limits_rules(
    numbat: Gen05Project, overrides: dict[str, object], rule_ids: tuple[str, ...], regular: str | None
) -> None:
    """R06 以外各规则的 entry_nos 筛选 (R02, R09 至 R12 见上面两条用例), Numbat 上命中的规则 (同
    test_numbat_gen05_on_postgres, test_numbat_overrides_make_rules_hit): 见 assert_limited. R07 的 entry_nos 另加
    一笔常规录入人 (本期凭证多于 rare_user_max) 的凭证: 计数若只按 entry_nos 算, 这一笔会命中, 命中的计数也会变."""
    params = numbat.run.params_snapshot
    extra = [
        j.entry_no
        for j in numbat_records("journal_entries", JournalEntry)
        if j.created_by == regular and str(params["period_start"]) <= str(j.posting_date) <= str(params["period_end"])
    ][:1]
    assert len(extra) == (regular is not None)
    await assert_limited(numbat, overrides, rule_ids, extra)


@pytest.mark.external
@pytest.mark.parametrize(
    ("overrides", "rule_ids"),
    [({"period_end": "2026-07-17"}, ("R05",)), ({"entry_timestamps_reliable": True}, ("R13",)), ({}, ("R11",))],
    ids=["R05", "R13", "R11"],
)
async def test_xero_entry_nos_limits_rules(
    xero: Gen05Project, overrides: dict[str, object], rule_ids: tuple[str, ...]
) -> None:
    """同上, Xero 上以参数覆盖命中的 R05, R13 (同 test_xero_overrides_make_rules_hit, test_xero_gen05_on_postgres) 与
    缺省参数下命中的 R11: entry_nos 只有命中的重记凭证 (原凭证与冲回凭证都不在其中, 由全期的作废对找到) 时仍命中."""
    await assert_limited(xero, overrides, rule_ids)


async def test_numbat_r06_runs_after_first_communication(numbat: Gen05Project) -> None:
    """验收 6: 录入时间可信的 Numbat 上, R06 在首次错报沟通时间为空时 skipped; 为 2026-10-18T23:00:00Z 时执行, 插入
    一笔其后录入的非更正分录即命中这一笔 (R06 不受 entry_nos 限制)."""
    unset = await gen05(numbat)
    assert next(o.status for o in unset.outcomes if o.rule_id == "R06") == "skipped"
    late = [
        adjustment_line("GJ-LATE-1", "GJ-LATE", "1100", 50000, 0) | {"posting_date": date(2026, 6, 30)},
        adjustment_line("GJ-LATE-2", "GJ-LATE", "4100", 0, 50000) | {"posting_date": date(2026, 6, 30)},
    ]
    late = [row | {"created_at": datetime(2026, 10, 19, tzinfo=UTC)} for row in late]
    async with with_rows(numbat, "journal_entries", late):
        result = await gen05(numbat, audit_adjustment_start="2026-10-18T23:00:00Z", entry_nos=["AJE-2026-001"])
    assert "R06" not in {o.rule_id for o in result.outcomes}
    assert hits(result, "R06") == ["GJ-LATE-1"]
