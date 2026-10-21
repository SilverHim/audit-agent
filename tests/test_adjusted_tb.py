"""需求 R174: 试算表加已验证更正分录 (services/adjusted_tb) 的两个纯函数.

verified_entry_nos 的判断条件由 test_contracts 的 test_verified_adjustment_entries_take_only_verified_corrections
守住, 这里只核验收 1 列的几种情形. adjusted_balances 的 Xero 用例取演示数据导入后的本期试算表与总账, 更正分录取补造的
11 行 (设计稿 v2.1 第 2.8 节). 连库的数据库函数 adjusted_trial_balance 见 test_persistence_postgres.
"""

import pytest

from backend.contracts.models import Misstatement
from backend.contracts.records import JournalEntry, TrialBalanceRow
from backend.contracts.state import initial_state
from backend.services.adjusted_tb import adjusted_balances, verified_entry_nos
from tests.fakes import ENGAGEMENT
from tests.test_contracts import lifecycle_misstatement
from tests.xero_fakes import xero_adjustments, xero_tables

ROUND_1 = ["AJE-X01", "AJE-X05"]
ROUND_2 = ["AJE-X01", "AJE-X02", "AJE-X03", "AJE-X04", "AJE-X05"]


def corrected(mid: str, entry_no: str, residual: int = 0) -> Misstatement:
    """FIN-09 验证过的更正 (凭证号 entry_no): 剩余额为 0 时 corrected, 否则 partially_corrected (错报金额 10000)."""
    return lifecycle_misstatement(
        id=mid,
        status="partially_corrected" if residual else "corrected",
        correction_notice_id="CN-1",
        adjustment_entry_nos=[entry_no],
        corrected_cents=10000 - residual,
        residual_cents=residual,
        verified_by_judgment_id=f"J-FIN-09-1-{mid}",
    )


FIRST = [corrected("M-1", "AJE-X01"), corrected("M-5", "AJE-X05")]


@pytest.mark.parametrize(
    ("misstatements", "expected"),
    [
        (FIRST, ROUND_1),  # 第 1 轮 (M-1, M-5 corrected)
        (  # 第 2 轮, 部分更正的也算
            [*FIRST, corrected("M-2", "AJE-X02"), corrected("M-3", "AJE-X03"), corrected("M-4", "AJE-X04", 100)],
            ROUND_2,
        ),
        (
            [lifecycle_misstatement(status="verifying", correction_notice_id="CN-1", adjustment_entry_nos=["AJE-X01"])],
            [],
        ),
        ([lifecycle_misstatement(status="uncorrected")], []),
        ([], []),  # 没有错报 (功能 misstatement 关闭, Numbat)
    ],
    ids=["round1", "round2", "verifying", "uncorrected", "none"],
)
def test_verified_entry_nos(misstatements: list[Misstatement], expected: list[str]) -> None:
    """验收 1: 状态 misstatements 中 corrected 或 partially_corrected 且有验证判断的错报的凭证号, 去重排序."""
    state = initial_state(ENGAGEMENT, [], 1) | {"misstatements": misstatements}
    assert verified_entry_nos(state) == expected


def xero_books() -> tuple[list[TrialBalanceRow], list[JournalEntry]]:
    """演示数据导入后的本期与上期试算表行, 总账加补造的更正分录 (分录行里不在请求中的凭证不该被算进去)."""
    tables = xero_tables()
    return list(tables["trial_balance"]), [*tables["journal_entries"], *xero_adjustments()]


def changes(entry_nos: list[str]) -> dict[str, tuple[int | None, int]]:
    """更正后与本期试算表不同的科目: 科目 → (试算表余额, 没有为 None; 更正后余额)."""
    trial, lines = xero_books()
    before = {r.account_code: r.balance_cents for r in trial if r.period == "current"}
    after = adjusted_balances(trial, lines, entry_nos)
    assert list(after) == sorted(after) and set(before) <= set(after)
    return {code: (before.get(code), cents) for code, cents in after.items() if before.get(code) != cents}


ROUND_1_CHANGES = {
    "270": (None, -4853),
    "620": (None, 4853),
    "710": (75000, 0),
    "711": (-82500, 0),
    "820": (-119197, -126697),
}


@pytest.mark.external
def test_adjusted_balances_by_round() -> None:
    """验收 3: 本期试算表加所列凭证的分录行 (借 - 贷), 按科目合计, 试算表没有的科目新出现; 上期的行不算."""
    assert changes(ROUND_1) == ROUND_1_CHANGES
    assert changes(ROUND_2) == ROUND_1_CHANGES | {
        "477": (3350000, 3819736),
        "478": (None, 426120),
        "804": (None, -201000),
        "805": (None, -268736),
        "826": (None, -426120),
    }
    assert changes([]) == {}  # 列表为空: 与本期试算表逐科目相等


def test_adjusted_balances_ignores_prior_period_and_other_entries() -> None:
    """合成: 上期的行不进余额; 凭证号不在列表中的分录行不算; 同一科目多行合计."""
    trial = [
        TrialBalanceRow(period="current", account_code="100", balance_cents=1000),
        TrialBalanceRow(period="prior", account_code="100", balance_cents=999),
        TrialBalanceRow(period="prior", account_code="900", balance_cents=5),
    ]

    def line(lid: str, entry_no: str, code: str, debit: int, credit: int) -> JournalEntry:
        return JournalEntry.model_construct(
            id=lid, entry_no=entry_no, account_code=code, debit_cents=debit, credit_cents=credit
        )

    lines = [line("1", "AJE-1", "100", 0, 300), line("2", "AJE-1", "200", 300, 0), line("3", "J-9", "100", 50, 0)]
    assert adjusted_balances(trial, lines, ["AJE-1"]) == {"100": 700, "200": 300}
    assert adjusted_balances(trial, lines, []) == {"100": 1000}
