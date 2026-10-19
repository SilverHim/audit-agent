"""确定性抽样 (services/sampling.py): 按用途加盐, critic 用途与加盐之前的公式逐值相同."""

import hashlib
import math

from backend.services.sampling import sample_point, sampled

IDS = [f"J-REV-02-1-X{n}" for n in range(2000)]


def old_sampled(judgment_id: str, rate: float) -> bool:
    """R84 之前 agents/review.sampled 的公式 (原样抄录): 判断 ID 的 sha256 前 8 位折成 [0, 1)."""
    return int(hashlib.sha256(judgment_id.encode()).hexdigest()[:8], 16) / 0x1_0000_0000 < rate


def test_same_input_same_point_and_purposes_differ() -> None:
    """同输入同值; 同一键在 critic 与 blind 两种用途下的抽样点不同."""
    assert sample_point("critic", "J-REV-02-1-X1") == sample_point("critic", "J-REV-02-1-X1")
    assert sample_point("critic", "J-REV-02-1-X1") != sample_point("blind", "J-REV-02-1-X1")
    assert all(0 <= sample_point(p, i) < 1 for p in ("critic", "blind") for i in IDS)


def test_rate_and_independence_between_purposes() -> None:
    """2000 个判断 ID 在 rate 0.05 下各用途抽中 60-140 个; 两种用途抽中的集合交集不超过 20 个."""
    critic = {i for i in IDS if sampled("critic", i, 0.05)}
    blind = {i for i in IDS if sampled("blind", i, 0.05)}
    assert 60 <= len(critic) <= 140 and 60 <= len(blind) <= 140
    assert len(critic & blind) <= 20


def test_critic_purpose_keeps_previous_formula() -> None:
    """critic 用途不加盐: 已知值 0.1453; 2000 个判断 ID 逐个与旧公式相同; 其余用途加 "{用途}:" 前缀."""
    assert round(sample_point("critic", "J-CSH-02-1-BA-091"), 4) == 0.1453
    assert [sampled("critic", i, 0.05) for i in IDS] == [old_sampled(i, 0.05) for i in IDS]
    salted = int(hashlib.sha256(b"blind:J-REV-02-1-X1").hexdigest()[:8], 16) / 0x1_0000_0000
    assert sample_point("blind", "J-REV-02-1-X1") == salted


def test_rate_bounds() -> None:
    """抽样点在 [0, 1) 内: rate 为 0 或负数时一个不中, 为 1 或大于 1 (含 inf) 时全中, NaN 一个不中."""
    for purpose in ("critic", "blind"):
        assert not any(sampled(purpose, i, rate) for i in IDS for rate in (0.0, -0.5, math.nan))
        assert all(sampled(purpose, i, rate) for i in IDS for rate in (1.0, 1.5, math.inf))
