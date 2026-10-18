"""数据管理智能体 (A3) 的数据规则: 就绪条件 (需求 R10). 纯函数, 不读写存储."""

from collections.abc import Mapping

from backend.contracts.data import TableKind, TableStats

# P1 开始前必须接入的表: 科目表, 本期与上期试算表, 总账分录
REQUIRED_TABLES: tuple[TableKind, ...] = (TableKind.accounts, TableKind.trial_balance, TableKind.journal_entries)


def missing_tables(stats: Mapping[TableKind, TableStats]) -> list[TableKind]:
    """就绪必需而还缺的表, 按 REQUIRED_TABLES 顺序. 试算表本期与上期任一期没有行都算缺."""
    missing = []
    for kind in REQUIRED_TABLES:
        item = stats.get(kind)
        if kind is TableKind.trial_balance:
            absent = item is None or item.current == 0 or item.prior == 0
        else:
            absent = item is None or item.records == 0
        if absent:
            missing.append(kind)
    return missing
