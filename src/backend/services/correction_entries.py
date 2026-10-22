"""更正分录建议的校验 (需求 R367, Issue #66).

AI 判为错报时附更正分录: 每行科目 (代码与名称), 借方或贷方金额, 摘要与说明. 这里只用科目表校验, 不查项目与存储:
借贷平衡; 科目在科目表且名称一致 (调用方先按代码取到科目, 查不到的由调用方按引用级报); 错报科目 (草稿带科目代码时)
上的净额等于错报金额, 且方向与错报方向相反 (高估要减, 低估要加; 借增还是贷增按科目的借贷性质). 判断引擎要求
AI 判为错报时必须给更正分录; 人工作出的认定可以不带 (前端从 AI 建议带出, 审计师可改).
"""

from backend.contracts.models import CorrectionEntry, Direction, MisstatementDraft
from backend.contracts.records import Account

# 借方余额的科目类别: 这类科目借记为增加; 其余 (负债, 权益, 收入) 贷记为增加
DEBIT_NORMAL = frozenset({"asset", "expense"})


def net_cents(entries: list[CorrectionEntry], code: str) -> int:
    """这些更正分录在科目 code 上的净额: 借方为正, 贷方为负."""
    return sum(e.debit_cents - e.credit_cents for e in entries if e.account_code == code)


def correction_problems(draft: MisstatementDraft, accounts: list[Account], *, required: bool = False) -> list[str]:
    """错报草稿所附更正分录的语义问题 (空为通过). required 为真时没有更正分录也报 (AI 输出必须带).

    accounts 为调用方按草稿的科目代码与更正分录的科目代码从科目表取到的行: 代码不在其中的由调用方按引用级报,
    这里不再重复.
    """
    entries = draft.correction
    if not entries:
        return ["判为错报, 但没有更正分录建议"] if required else []
    chart = {a.code: a for a in accounts}
    problems: list[str] = []
    total_debit = sum(e.debit_cents for e in entries)
    total_credit = sum(e.credit_cents for e in entries)
    if total_debit != total_credit:
        problems.append(f"更正分录借贷不平: 借方 {total_debit} 分, 贷方 {total_credit} 分")
    for entry in entries:
        account = chart.get(entry.account_code)
        if account is not None and account.name != entry.account_name:
            problems.append(
                f"更正分录的科目名称 {entry.account_name} 与科目表 {entry.account_code} 的 {account.name} 不符"
            )
    code = draft.account_code
    if code is not None and code in chart and draft.amount_cents:
        net = net_cents(entries, code)
        if abs(net) != abs(draft.amount_cents):
            problems.append(
                f"更正分录在错报科目 {code} 上的净额 {abs(net)} 分不等于错报金额 {abs(draft.amount_cents)} 分"
            )
        else:
            debit_normal = chart[code].type in DEBIT_NORMAL
            increase = net > 0
            expected_increase = (not debit_normal) if draft.direction is Direction.overstated else debit_normal
            if increase != expected_increase:
                side = "借" if increase else "贷"
                problems.append(f"更正分录在错报科目 {code} 上的方向与错报方向不符: 净方向为{side}, 应为相反方向")
    return problems
