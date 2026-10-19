"""确定性抽样 (A7 独立复核 GEN-09 的抽样触发规则, 接口文档第 6 节): 键的 sha256 折成 [0, 1) 的抽样点.

键只用判断 ID, 不含项目号: 判断 ID 确定, 重放与恢复结果不变; 连库测试每次新建项目, 加项目号会使抽中的对象随项目号变,
计数不稳定 (接口文档第 6 节).
"""

import hashlib


def sample_point(key: str) -> float:
    """键的抽样点, 落在 [0, 1): sha256 十六进制的前 8 位折成整数, 除以 2^32."""
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0x1_0000_0000


def sampled(key: str, rate: float) -> bool:
    """该键是否抽中: 抽样点小于 rate. 抽样点在 [0, 1) 内, 所以 rate ≤ 0 恒不中, rate ≥ 1 恒中, NaN 恒不中."""
    return sample_point(key) < rate
