"""重要性金额的确定性计算, 闸门与底稿共用同一舍入口径."""

from decimal import ROUND_HALF_UP, Decimal

from backend.contracts.models import MaterialityCandidates, MaterialityChoice


def to_cents(amount: Decimal) -> int:
    """金额四舍五入到整数分 (0.5 进位)."""
    return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def materiality_amounts(candidates: MaterialityCandidates, choice: MaterialityChoice) -> tuple[int, int, int]:
    """按审计师的选择算出 (整体重要性, 执行重要性, 明显微小门槛), 单位分.

    整体重要性 = 所选基准金额 × pct; 执行重要性 = 整体重要性 × performance_pct; 明显微小 = 整体重要性 ×
    clearly_trivial_pct. 比例按十进制计算 (Decimal(str(x)), 避免二进制浮点误差), 每项各自四舍五入到整数分.
    例: 收入基准 1200000000, {0.01, 0.75, 0.05} → (12000000, 9000000, 600000). basis 须在候选中 (check_materiality).
    """
    candidate = next(c for c in candidates.candidates if c.basis == choice.basis)
    overall = to_cents(Decimal(candidate.base_amount_cents) * Decimal(str(choice.pct)))
    performance = to_cents(Decimal(overall) * Decimal(str(choice.performance_pct)))
    trivial = to_cents(Decimal(overall) * Decimal(str(choice.clearly_trivial_pct)))
    return overall, performance, trivial
