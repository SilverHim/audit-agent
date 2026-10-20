"""试算表加已验证更正分录 (需求 R174): "更正后余额" 的唯一取数口径.

系统试算表 (trial_balance 表) 不随更正改版 (设计稿 v2.1 第 7.2 节 "更正传导"); 要更正后的余额时, 在本期试算表上加
已验证更正分录的分录行. 已验证更正分录的定义见接口文档第 2.5 节 (需求 R156 验收 1, contracts.models 的
verified_adjustment_entries): status 为 corrected 或 partially_corrected 且 verified_by_judgment_id 非空的错报的
adjustment_entry_nos 并集.

- verified_entry_nos: 由状态取已验证更正分录的凭证号, 程序参数经派生引用 {ref: verified_adjustment_entry_nos} 取它.
- adjusted_balances: Python 一侧的余额; 规则 SQL 调数据库函数 adjusted_trial_balance(:eid, :entry_nos) (迁移 0022),
  两者口径相同. FIN-15 F01, FIN-02 与导引表的 "已更正调整" (R335) 都用这里, 不另写.
纯函数, 不读写存储.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any

from backend.contracts.models import verified_adjustment_entries
from backend.contracts.records import JournalEntry, TrialBalanceRow


def verified_entry_nos(state: Mapping[str, Any]) -> list[str]:
    """已验证更正分录的凭证号: 状态 misstatements (各错报的当前版本) 按 verified_adjustment_entries 取, 去重排序.
    没有错报 (含功能 misstatement 关闭, Numbat) 为 [].
    例: Xero 第 1 轮 M-D-XJ499 与 M-D-CSH02-091 验证为 corrected → [AJE-X01, AJE-X05]."""
    return verified_adjustment_entries(state.get("misstatements") or [])


def adjusted_balances(
    trial_balance: Sequence[TrialBalanceRow], lines: Iterable[JournalEntry], entry_nos: Collection[str]
) -> dict[str, int]:
    """更正后余额 (科目代码 → 余额, 借方为正, 按科目代码排序): 本期试算表的余额 (上期的行不算) 加凭证号在 entry_nos
    中的分录行的借方减贷方, 按科目合计; 试算表没有的科目随分录行新出现. entry_nos 为空时与本期试算表逐科目相等.
    口径同数据库函数 adjusted_trial_balance. 例: Xero 本期 820 为 -119197, 加 AJE-X01 后为 -126697."""
    balances: dict[str, int] = {}
    for row in trial_balance:
        if row.period == "current":
            balances[row.account_code] = balances.get(row.account_code, 0) + row.balance_cents
    wanted = set(entry_nos)
    for line in lines:
        if line.entry_no in wanted:
            balances[line.account_code] = balances.get(line.account_code, 0) + line.debit_cents - line.credit_cents
    return dict(sorted(balances.items()))
