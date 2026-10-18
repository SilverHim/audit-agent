"""确定性抽样: 一个函数按用途加盐, 同一键在不同用途下的抽样点彼此独立 (critique C8).

键只用判断 ID, 不含项目号: 判断 ID 确定, 重放与恢复结果不变; 连库测试每次新建项目, 加项目号会使抽中的对象随项目号变,
计数不稳定 (接口文档第 6 节).
"""

import hashlib
from typing import Literal

# 抽样用途: critic 为 A7 独立复核 (GEN-09) 的抽样, blind 为盲测抽查; 新用途在此加一个取值, 拼错的由 mypy 挡住
Purpose = Literal["critic", "blind"]
UNSALTED: Purpose = "critic"  # A7 独立复核的抽样沿用加盐之前的公式, 与已有的抽样结果逐值相同


def sample_point(purpose: Purpose, key: str) -> float:
    """键在该用途下的抽样点, 落在 [0, 1): sha256 十六进制的前 8 位折成整数, 除以 2^32.

    critic 用途对键本身取哈希; 其余用途 (如盲测抽查 blind) 对 "{用途}:{键}" 取哈希.
    """
    text = key if purpose == UNSALTED else f"{purpose}:{key}"
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) / 0x1_0000_0000


def sampled(purpose: Purpose, key: str, rate: float) -> bool:
    """该键在该用途下是否抽中: 抽样点小于 rate. 抽样点在 [0, 1) 内, 所以 rate ≤ 0 恒不中, rate ≥ 1 恒中, NaN 恒不中."""
    return sample_point(purpose, key) < rate
