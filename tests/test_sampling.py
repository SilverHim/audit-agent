"""确定性抽样 (services/sampling.py): A7 独立复核的抽样, 与 R84 之前 agents/review.sampled 的公式逐值相同."""

import hashlib
import math

from backend.services.sampling import sample_point, sampled

IDS = [f"J-REV-02-1-X{n}" for n in range(2000)]


def old_sampled(judgment_id: str, rate: float) -> bool:
    """R84 之前 agents/review.sampled 的公式 (原样抄录): 判断 ID 的 sha256 前 8 位折成 [0, 1)."""
    return int(hashlib.sha256(judgment_id.encode()).hexdigest()[:8], 16) / 0x1_0000_0000 < rate


def test_keeps_previous_formula() -> None:
    """抽样点在 [0, 1) 内; 2000 个判断 ID 逐个与旧公式相同 (防删去用途参数时改了公式)."""
    assert all(0 <= sample_point(i) < 1 for i in IDS)
    assert [sampled(i, 0.05) for i in IDS] == [old_sampled(i, 0.05) for i in IDS]


def test_rate_bounds() -> None:
    """rate 为 0 或负数时一个不中, 为 1 或大于 1 (含 inf) 时全中, NaN 一个不中."""
    assert not any(sampled(i, rate) for i in IDS for rate in (0.0, -0.5, math.nan))
    assert all(sampled(i, rate) for i in IDS for rate in (1.0, 1.5, math.inf))


def test_known_points_and_counts() -> None:
    """R149 验收 1 与 R295 验收 7: A7 抽样的确切值 (防公式或键被改而抽中的对象悄悄变): 键只用判断 ID,
    J-CSH-02-1-BA-091 0.1453, J-CSH-02-1-BA-01 0.2221, J-GEN-06-1-XJ-626-2 0.8845; 2000 个 ID 在 0.05 下抽中 98 个."""
    points = {"J-CSH-02-1-BA-091": 0.1453, "J-CSH-02-1-BA-01": 0.2221, "J-GEN-06-1-XJ-626-2": 0.8845}
    assert {key: round(sample_point(key), 4) for key in points} == points
    assert sum(sampled(i, 0.05) for i in IDS) == 98
